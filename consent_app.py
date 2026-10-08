"""
Air Discharge Consent Analytics (Streamlit)

Run:  pip install streamlit pdfplumber pandas plotly pyproj openpyxl reportlab
      streamlit run consent_app.py

Every run starts empty: upload the <ID>_Consent.pdf and <ID>_Memo.pdf files (any number of pairs).
Nothing is cached between runs and nothing is looked up online - a value that is not in the
uploaded PDFs is reported as "Not found" (and listed on the Data quality tab), never guessed.

Sources
  * Consent PDF only  -> every condition (grouped under the consent's own headings), the
                         grant date (decision date) and the expiry condition.
  * Memo PDF          -> everything else: years of consent granted ("Duration of consent"),
                         the rules that trigger consent ("Reason for application"),
                         applicant, site address, air quality area, specialist, GHG figures.
"""
import json
import math
import re
import time
import urllib.parse
import urllib.request
import io
from collections import Counter
from datetime import date, datetime, timedelta

import pandas as pd
import pdfplumber
import streamlit as st

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_TXT = rf"(\d{{1,2}})\s*(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTHS})\s+(\d{{4}})"   # 3rd November 2017 / 18th of August 2036
DATE_NUM = r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4}|\d{2})\b"                         # 29 / 10 / 2018, 4/4/24
NOT_FOUND = "Not found"
CLASS_RANK = {"Controlled": 1, "Restricted discretionary": 2, "Discretionary": 3, "Non-complying": 4, "Prohibited": 5}

# Keyword themes used only to group conditions on the analytics tab (a condition goes to the
# theme with the most keyword hits; ties go to the earlier theme).
THEMES = [
    ("Expiry, lapse & review", r"\bexpire|\blapse|section 128|\breview\b|\bsurrender"),
    ("Fees & access", r"monitoring charge|access to the|\binspections?\b|pay the council"),
    ("Management plans", r"management plan|\bAQMP\b|emissions plan|odour management"),
    ("Monitoring & testing", r"monitor|sampl|\btest(?:s|ing|ed)?\b|measurement|detector|\bsurvey"),
    ("Reporting, records & notification", r"\breport|\brecords?\b|notif|complaint|\blogs?\b|kept for|certif"),
    ("Emission & effect limits", r"\blimits?\b|not exceed|beyond the boundary|objectionable|noxious|opacity|mg/m|g/hr|g/s\b|concentration"),
    ("Process & equipment controls", r"operat|maintain|equipment|ducting|baghouse|filter|\bstack|silo|afterburner|temperature|boiler|burn|scrubber|cyclone"),
]
OTHER_THEME = "General compliance"


# ------------------------------------------------------------------ PDF text
FOOTER = re.compile(
    r"^(?:DIS\d{8}.*Page \d+|Page \d+(?: of \d+)?(?:\s+DIS\d{8})?|Consent:.*|Address:.*|"
    r"Air (?:quality|discharge) review.*|BUN\d+.*Air discharge.*|.*\(Air discharge:.*\)\s*\d*)\s*$")


def read_pdf(file) -> list[str]:
    """Text lines of a PDF with page headers/footers removed."""
    with pdfplumber.open(file) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    return [ln.strip() for ln in text.splitlines() if not FOOTER.match(ln.strip())]


def flat(lines) -> str:
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def first_date(s):
    cands = []
    m = re.search(DATE_TXT, s)
    if m:
        cands.append((m.start(), datetime.strptime(f"{int(m.group(1))} {m.group(2)} {m.group(3)}", "%d %B %Y").date()))
    n = re.search(DATE_NUM, s)
    if n:
        try:
            y = int(n.group(3))
            cands.append((n.start(), date(y + 2000 if y < 100 else y, int(n.group(2)), int(n.group(1)))))
        except ValueError:
            pass
    return min(cands, key=lambda c: c[0])[1] if cands else None


def add_years(d: date, years: float) -> date:
    whole = int(years)
    try:
        d2 = d.replace(year=d.year + whole)
    except ValueError:                       # 29 Feb
        d2 = d.replace(year=d.year + whole, day=28)
    return d2 + timedelta(days=round((years - whole) * 365.25))


# ------------------------------------------------------------------ memo
HEAD = re.compile(r"^(\d{1,2})(?:\.(\d{1,2}))?\.?\s+([A-Z][^\n]{2,90})$")


def headings(lines):
    """[(line_index, major, minor, title)] for numbered section headings."""
    out = []
    for i, ln in enumerate(lines):
        m = HEAD.match(ln)
        if m and not re.match(MONTHS, m.group(3)) and not m.group(3).rstrip().endswith((".", ",", ";")):
            out.append((i, int(m.group(1)), int(m.group(2)) if m.group(2) else None, m.group(3)))
    return out


def section(lines, title_pat):
    """Lines under the first heading matching title_pat. A major heading (4) runs to the next major heading;
    a sub heading (6.5) runs to the next heading of any level."""
    hs = headings(lines)
    for k, (i, major, minor, title) in enumerate(hs):
        if re.search(title_pat, title, re.I):
            end = next((j for j, mj, mn, _ in hs[k + 1:] if minor is not None or mj != major), len(lines))
            return lines[i + 1:end]
    return []


def subsections(lines):
    """Split a major section into [(title, lines)] using its x.y sub headings (first part has title '')."""
    parts, cur_t, cur = [], "", []
    for ln in lines:
        m = HEAD.match(ln)
        if m and m.group(2) and not re.match(MONTHS, m.group(3)):
            parts.append((cur_t, cur))
            cur_t, cur = m.group(3), []
        else:
            cur.append(ln)
    parts.append((cur_t, cur))
    return parts


RULE = re.compile(
    r"(?:Rule\s+)?(?:E14\.4\.1\s*)?\((A\d{1,3})\)\s*[:\-–]?\s*(?=[A-Z])(.{5,450}?)\s*"
    r"(?:\[([^\]]*?[Aa]ctivity[^\]]*)\]|[–-]\s*((?:Restricted Discretionary|Discretionary|Controlled|Permitted|Non-complying)\s+Activity)\b)")
GROUP = re.compile(r"into air from ([a-z][a-z ,&\-]*?)\s*[.:]?\s+(?:Rule\s+)?(?:E14\.4\.1\s*)?\(A\d")


def norm_class(s: str) -> str:
    m = re.search(r"(restricted discretionary|discretionary|controlled|permitted|non-complying|prohibited)", s, re.I)
    return m.group(1).capitalize() if m else "Unknown"


def triggered_rules(lines) -> list[dict]:
    """Rules that make this application need consent, from the memo's 'Reason for application' section."""
    reasons = section(lines, r"reasons? for (?:application|consent)")
    rules = []
    if reasons:
        parts = [(t, ls) for t, ls in subsections(reasons) if not re.match(r"other\b", t, re.I)]
        txt = flat([ln for _, ls in parts for ln in ls])
        if not re.search(r"no additional resource consent", txt):        # pre-existing, already-consented activities
            seen = set()
            for m in RULE.finditer(txt):
                grp = GROUP.findall(txt[:m.start() + 3])
                group = grp[-1].strip() if grp else None
                key = (group, m.group(1))
                if key in seen:
                    continue
                seen.add(key)
                rules.append(dict(rule=f"E14.4.1 ({m.group(1)})", group=group, description=m.group(2).strip(" ."),
                                  activity_class=norm_class(m.group(3) or m.group(4))))
    whole = flat(lines)
    for m in re.finditer(r"[Cc]onsent is required for [^.]{0,120}? under Regulation (\d+) of the (NES[:\-]?\s?IGHG)", whole):
        c = re.search(rf"Regulation {m.group(1)} of the {re.escape(m.group(2))} classifies[^.]*? as an? ([A-Za-z ]+?) Activity", whole)
        rules.append(dict(rule=f"{m.group(2).replace('-', ':')} Reg. {m.group(1)}", group="greenhouse gas emissions from industrial process heat",
                          description="Discharge of greenhouse gases from a new fossil-fuel process heat device",
                          activity_class=norm_class(c.group(1)) if c else "Unknown"))
    return rules


def memo_duration(lines):
    """(years granted, years requested) from the memo's 'Duration of consent' section."""
    txt = flat(section(lines, r"duration"))
    granted = None
    for pat in (r"set a term of (\d+(?:\.\d+)?)[ -]years?", r"approx(?:\.|imately)?\s*(\d+(?:\.\d+)?)[ -]years?",
                r"duration of approximately (\d+(?:\.\d+)?) years", r"(\d+(?:\.\d+)?)[ -]years? consent duration",
                r"(\d+(?:\.\d+)?)[ -]years? term will allow"):
        m = re.search(pat, txt)
        if m:
            granted = float(m.group(1))
            break
    r = re.search(r"requested an? (\d+(?:\.\d+)?)[ -]year", txt) or re.search(r"term of consent of at least (\d+(?:\.\d+)?) years", txt)
    return granted, float(r.group(1)) if r else None


def norm_aq(txt: str) -> str:
    """'Medium Air Quality - dust and odour area (Industry)' -> 'Medium air quality – dust and odour area (industry)'."""
    t = re.sub(r"\s+", " ", txt).strip().lower().replace(" - ", " – ").replace(": ", " – ")
    t = re.sub(r"\s*\((industry|rural)\)$", lambda m: "" if m.group(1) in t[:m.start()] else m.group(0), t)
    return t[:1].upper() + t[1:]


def parse_memo(lines) -> dict:
    whole = flat(lines)
    g = lambda pat: (m.group(1).strip() if (m := re.search(pat, "\n".join(lines))) else None)
    granted, requested = memo_duration(lines)
    aq = re.search(r"(low|medium|high) air quality\s*[–:\-]\s*(?:dust and odour|odour and dust)[^.\[\];,’']{0,25}?area(?:\s*\((?:industry|rural)\))?", whole, re.I)
    ghg = re.search(r"[Ss]ite total of ([\d,]+) tonnes\.?\s*CO\S*", whole)
    frm = re.search(r"From:\s*([^,\n]+),\s*([^\n]+)", "\n".join(lines))
    return dict(
        applicant=g(r"Applicant(?:'s|’s)? ?(?:name)?:\s*(.+)"),
        site_address=re.sub(r"\s*\(.*?\)\s*$", "", g(r"Site address:\s*(.+)") or "") or None,
        years_granted=granted, years_requested=requested,
        rules=triggered_rules(lines),
        air_quality_area=norm_aq(aq.group(0)) if aq else None,
        ghg_site_total=float(ghg.group(1).replace(",", "")) if ghg else None,
        specialist=f"{frm.group(1).strip()} ({frm.group(2).strip()})" if frm else None,
    )


# ------------------------------------------------------------------ consent
def parse_conditions(lines) -> list[dict]:
    """Every numbered condition between 'subject to the following conditions' and 'Advice notes'."""
    text = "\n".join(lines)
    m = re.search(r"subject to (?:the )?following\s+conditions\s*[:.]?", text)
    if not m:
        return []
    body = text[m.end():]
    end = re.search(r"(?m)^(?:\d+\.\s*)?Advice notes?\s*$|^Delegated decision maker", body)
    body = body[:end.start()] if end else body
    blines = [ln for ln in body.splitlines() if ln.strip()]
    conds, heading, expected = [], "General", 1
    for k, ln in enumerate(blines):
        mm = re.match(r"^(\d{1,3})[.)]\s*(.*)", ln)
        if mm and int(mm.group(1)) == expected:
            conds.append(dict(number=expected, section=heading, lines=[mm.group(2)]))
            expected += 1
            continue
        nxt = blines[k + 1] if k + 1 < len(blines) else ""
        is_heading = (re.match(rf"^{expected}[.)]\s", nxt) and len(ln) <= 70 and len(ln.split()) <= 8
                      and ln[0].isupper() and not re.search(r"\d", ln) and not ln.endswith((".", ";", ",", ":")))
        if is_heading:
            heading = re.sub(r"\s+conditions?$", "", ln, flags=re.I).strip()
            heading = heading[:1].upper() + heading[1:]
        elif conds:
            conds[-1]["lines"].append(ln)
    for c in conds:
        c["text"] = re.sub(r"\s+", " ", " ".join(c.pop("lines"))).strip()
        scores = [(len(re.findall(p, c["text"], re.I)), -i, name) for i, (name, p) in enumerate(THEMES)]
        best = max(scores)
        c["theme"] = best[2] if best[0] > 0 else OTHER_THEME
    return conds


def parse_consent(lines) -> dict:
    conds = parse_conditions(lines)
    text = "\n".join(lines)
    i = text.rfind("Delegated decision maker")
    m = re.search(r"Date:\s*(.{0,40})", text[i:]) if i >= 0 else None
    expiry_txt = next((c["text"] for c in conds if re.search(r"\bexpires?\b", c["text"])), None)
    exp = re.search(r"expires?\s+on\s+(?:the\s+)?(.{0,45})", expiry_txt) if expiry_txt else None
    return dict(conditions=conds, date_granted=first_date(m.group(1)) if m else None,
                expiry_condition=expiry_txt, expiry_stated=first_date(exp.group(1)) if exp else None)


def find_nztm(*line_lists):
    """(easting, northing) if the PDFs print an NZTM reference, e.g. '1751734E; 5931450N'."""
    m = re.search(r"NZTM[^0-9]{0,40}?(\d{7})\s*m?E?\s*[;,]?\s*(\d{7})", flat([ln for ls in line_lists for ln in ls]))
    return (int(m.group(1)), int(m.group(2))) if m else None


# ------------------------------------------------------------------ combine
def build_row(cid, consent_lines, memo_lines) -> dict:
    c = parse_consent(consent_lines) if consent_lines else dict(conditions=[], date_granted=None, expiry_condition=None, expiry_stated=None)
    m = parse_memo(memo_lines) if memo_lines else {}
    years, granted = m.get("years_granted"), c["date_granted"]
    expiry, basis = c["expiry_stated"], "Consent condition"
    if expiry is None and years and granted:                      # expiry condition is relative ("10 years after issue")
        expiry, basis = add_years(granted, years), "Grant date + memo duration"
    elif expiry is None:
        basis = None
    rules = m.get("rules", [])
    classes = [r["activity_class"] for r in rules if r["activity_class"] in CLASS_RANK]
    overall = max(classes, key=CLASS_RANK.get) if classes else None
    conds = c["conditions"]
    missing = [k for k, v in {
        "consent PDF": bool(consent_lines), "memo PDF": bool(memo_lines), "years granted (memo)": years,
        "triggered rules (memo)": rules, "conditions (consent)": conds, "grant date (consent)": granted,
        "expiry date": expiry, "air quality area (memo)": m.get("air_quality_area"),
        "applicant (memo)": m.get("applicant"), "site address (memo)": m.get("site_address")}.items() if not v]
    return dict(
        consent_id=cid, applicant=m.get("applicant"), site_address=m.get("site_address"),
        years_granted=years, years_requested=m.get("years_requested"),
        date_granted=granted, date_expiry=expiry, expiry_basis=basis,
        air_quality_area=m.get("air_quality_area"), activity_status=overall,
        rules_triggered=len(rules), rules=rules, rule_text="; ".join(r["rule"] for r in rules),
        rule_groups="; ".join(dict.fromkeys(r["group"] for r in rules if r["group"])),
        n_conditions=len(conds), conditions=conds, ghg_site_total=m.get("ghg_site_total"),
        specialist=m.get("specialist"), nztm=find_nztm(consent_lines or [], memo_lines or []), missing=missing,
    )


def analyse(files) -> list[dict]:
    docs, bar, note = {}, st.progress(0.0), st.empty()
    for i, f in enumerate(files, 1):
        note.text(f"Reading {f.name} ({i}/{len(files)})")
        lines = read_pdf(f)
        cid = (re.search(r"DIS\d{8}", f.name) or re.search(r"DIS\d{8}", " ".join(lines[:60])) or [None])[0]
        if not cid:
            st.warning(f"No consent number (DISxxxxxxxx) in {f.name} - skipped.")
            continue
        n = f.name.lower()
        kind = "memo" if "memo" in n else "consent" if "consent" in n else (
            "consent" if "Decision on application" in " ".join(lines[:80]) else "memo")
        docs.setdefault(cid, {})[kind] = lines
        bar.progress(i / (len(files) + 1))
    rows = []
    for cid, d in sorted(docs.items()):
        note.text(f"Analysing {cid}")
        rows.append(build_row(cid, d.get("consent"), d.get("memo")))
    bar.progress(1.0)
    note.text(f"Done - {len(files)} file(s), {len(rows)} consent(s).")
    return rows


# ------------------------------------------------------------------ register chatbot
REG_COLS = ["consent_id", "applicant", "site_address", "status", "date_granted", "date_expiry", "years_requested",
            "years_granted", "activity_status", "air_quality_area", "rule_text", "rules_triggered", "n_conditions"]
BOT_STOP = set("""a an the of for in on at to is are was were be been what which who whom whose how many much do does did show me list
give tell about with and or all any consent consents data from that this these those there their it its has have had by as
can you please find search i we us our when where why not no than then also into per each every more less over under below above least most fewer longer shorter before after during year years yrs condition conditions rule rules site total ghg greenhouse specialist reviewer reviewed applicant address basis requested expiry expire expires granted status zone""".split())
FIELD_WORDS = [  # (regex, column, label)
    (r"expir|ends?\b|end date|lapse", "date_expiry", "expires on"),
    (r"granted|issued|grant date", "date_granted", "was granted on"),
    (r"years|duration|term\b|how long", "years_granted", "was granted for (years)"),
    (r"applicant|company|owner|who applied", "applicant", "applicant is"),
    (r"address|where|located|location|site\b", "site_address", "site address is"),
    (r"air quality|zone|zoning", "air_quality_area", "air quality area is"),
    (r"\brules?\b|trigger|e14", "rule_text", "triggering rules are"),
    (r"activity (status|class)|activity type", "activity_status", "overall activity status is"),
    (r"how many conditions|number of conditions|conditions count", "n_conditions", "number of conditions is"),
    (r"specialist|reviewer|reviewed|author", "specialist", "specialist is"),
    (r"ghg|greenhouse|co2", "ghg_site_total", "GHG site total (t CO2e/yr) is"),
    (r"requested|asked for|applied for", "years_requested", "years requested were"),
    (r"basis", "expiry_basis", "expiry basis is"),
]
CMP = {"more than": ">", "over": ">", "greater than": ">", "above": ">", "longer than": ">", "at least": ">=", "minimum of": ">=",
       "less than": "<", "under": "<", "below": "<", "shorter than": "<", "fewer than": "<", "at most": "<=", "no more than": "<="}


BOT_LABEL = {"date_expiry": "expiry date", "n_conditions": "number of conditions", "rules_triggered": "number of rules triggered", "years_granted": "years granted"}


def _fmt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return NOT_FOUND
    if isinstance(v, date):
        return f"{v.day} {v:%B %Y}"
    return f"{v:g}" if isinstance(v, float) else str(v)


def register_view(df: pd.DataFrame) -> pd.DataFrame:
    """The Register tab table (dates as text)."""
    out = df[REG_COLS].copy()
    for c in ("date_granted", "date_expiry"):
        out[c] = out[c].map(lambda d: d.isoformat() if isinstance(d, date) else NOT_FOUND)
    return out.fillna(NOT_FOUND)


def summary_table(r: dict) -> pd.DataFrame:
    """The Field / Value table shown for one consent on the Consent summaries tab."""
    rows_ = [
        ("Consent number", r["consent_id"]),
        ("Applicant (memo)", _fmt(r["applicant"])),
        ("Site address (memo)", _fmt(r["site_address"])),
        ("Status", _fmt(r["status"])),
        ("Years of consent granted (memo)", _fmt(r["years_granted"])),
        ("Years requested (memo)", _fmt(r["years_requested"])),
        ("Granted (consent)", _fmt(r["date_granted"])),
        ("Expires", f"{_fmt(r['date_expiry'])} (basis: {_fmt(r['expiry_basis'])})"),
        ("Air quality area (memo)", _fmt(r["air_quality_area"])),
        ("Overall activity status (memo)", _fmt(r["activity_status"])),
        ("Rules that trigger consent (memo)", _fmt(r["rule_text"] or None)),
        ("Number of conditions (consent)", str(r["n_conditions"])),
        ("GHG site total, t CO2e/yr (memo)", _fmt(r["ghg_site_total"])),
        ("Specialist (memo)", _fmt(r["specialist"])),
    ]
    return pd.DataFrame(rows_, columns=["Field", "Value"])


def ask(q: str, df: pd.DataFrame):
    """Answer a question from everything extracted (Register tab, Consent summaries tab, conditions). Returns (markdown, table or None)."""
    ql = q.lower().strip()
    text = (df.consent_id + " " + df.applicant.fillna("") + " " + df.site_address.fillna("") + " " + df.status + " " +
            df.activity_status.fillna("") + " " + df.air_quality_area.fillna("") + " " + df.rule_text.fillna("") + " " +
            df.rules.map(lambda rs: " ".join(f"{r['description']} {r['group'] or ''}" for r in rs)) + " " +
            df.date_granted.map(str) + " " + df.date_expiry.map(str) + " " + df.specialist.fillna("") + " " +
            df.expiry_basis.fillna("") + " " + df.rule_groups.fillna("") + " " +
            df.conditions.map(lambda cs: " ".join(f"{c['section']} {c['text']}" for c in cs))).str.lower()
    mask, notes = pd.Series(True, index=df.index), []

    def narrow(m, note):
        nonlocal mask
        mask, _ = mask & m, notes.append(note)

    for cid in re.findall(r"dis\d{8}", ql):
        narrow(df.consent_id.str.lower() == cid, cid.upper())
    if re.search(r"\bexpired\b", ql):
        narrow(df.status == "Expired", "expired")
    elif re.search(r"\bactive\b|\bcurrent\b|\bvalid\b|\bin force\b", ql):
        narrow(df.status == "Active", "active")
    if "restricted" in ql:
        narrow(df.activity_status == "Restricted discretionary", "restricted discretionary")
    elif "discretionary" in ql:
        narrow(df.activity_status == "Discretionary", "discretionary")
    elif "controlled" in ql:
        narrow(df.activity_status == "Controlled", "controlled")
    for code in dict.fromkeys(re.findall(r"\ba(\d{1,3})\b", ql)):
        narrow(df.rule_text.fillna("").str.contains(rf"\(A{code}\)", regex=True), f"rule A{code}")
    if "rural" in ql:
        narrow(df.air_quality_area.fillna("").str.contains("rural", case=False), "rural area")
    if re.search(r"industry|industrial", ql):
        narrow(df.air_quality_area.fillna("").str.contains("industry", case=False), "industry area")
    for m in re.finditer(r"(?:(" + "|".join(CMP) + r")\s+)?(\d+(?:\.\d+)?)[ -]?(years?|yrs?|conditions?|rules?)\b", ql):
        unit, n = m.group(3), float(m.group(2))
        col = "years_granted" if unit.startswith("y") else "n_conditions" if unit.startswith("c") else "rules_triggered"
        op = CMP.get(m.group(1), "==")
        res = {">": df[col] > n, ">=": df[col] >= n, "<": df[col] < n, "<=": df[col] <= n, "==": df[col] == n}[op]
        narrow(res, f"{col.replace('_', ' ')} {op} {n:g}")
    m = re.search(r"(expir\w*|granted|issued)\s+(in|before|after|by|during|on or before)?\s*(\d{4})", ql)
    if m:
        col = "date_expiry" if m.group(1).startswith("expir") else "date_granted"
        yr, how = int(m.group(3)), (m.group(2) or "in")
        ys = df[col].map(lambda d: d.year if isinstance(d, date) else None)
        narrow({"before": ys < yr, "after": ys > yr, "by": ys <= yr, "on or before": ys <= yr}.get(how, ys == yr), f"{col.replace('_', ' ')} {how} {yr}")
    vocab = set(re.findall(r"[a-z]{3,}", " ".join(text)))
    tokens = [t for t in dict.fromkeys(re.findall(r"[a-z]+", ql)) if t in vocab and t not in BOT_STOP and len(t) > 2]
    tokens = [t for t in tokens if not any(t in n.lower() for n in notes)]
    tokens += [d for d in re.findall(r"\b(\d{5,})\b", ql)]
    exact = mask & pd.Series([all(t in x for t in tokens) for x in text], index=df.index)
    closest = False
    if tokens and not exact.any():
        hits = pd.Series([sum(t in x for t in tokens) for x in text], index=df.index).where(mask, 0)
        if hits.max() > 0:
            exact, closest = hits == hits.max(), True
    sel = df[exact]
    label = ", ".join(notes + [t for t in tokens]) or "all consents"
    view = lambda d: register_view(d)
    if not notes and not tokens and not re.search(r"\b(list|show|all|everything|register|table|every|average|mean|longest|highest|most|maximum|max|largest|biggest|latest|last|shortest|lowest|fewest|minimum|min|least|smallest|earliest|soonest|first|how many|number of|count|total)\b", ql):
        return ("Ask me about the register, e.g. *when does DIS60308433 expire*, *which consents are active*, *consents in Henderson*, "
                "*more than 15 years*, *rule A54*, *longest duration*, *average years granted*."), None
    if sel.empty:
        return ("I couldn't find anything in the register for that. Try a consent number (e.g. DIS60308433), an applicant or suburb, a rule "
                "such as A54, or a filter like *active*, *expires before 2035*, *more than 15 years*."), None

    # aggregates
    if re.search(r"average", ql):
        col = "n_conditions" if "condition" in ql else "rules_triggered" if "rule" in ql else "years_granted"
        return f"The average {BOT_LABEL[col]} across {len(sel)} consent(s) ({label}) is **{sel[col].mean():.1f}**.", view(sel)
    ext = re.search(r"(longest|highest|most|maximum|max|largest|biggest|latest|last)|(shortest|lowest|fewest|minimum|min|least|smallest|earliest|soonest|first)", ql)
    if ext and not re.search(r"how many", ql):
        big = ext.group(1) is not None
        if re.search(r"expir|latest|earliest|soonest", ql) and sel.date_expiry.notna().any():
            col = "date_expiry"
        else:
            col = "n_conditions" if "condition" in ql else "rules_triggered" if "rule" in ql else "years_granted"
        s2 = sel[sel[col].notna()]
        if not s2.empty:
            ref = s2[col].max() if big else s2[col].min()
            top = s2[s2[col] == ref]
            word = ("Latest" if big else "Earliest") if col == "date_expiry" else ("Highest" if big else "Lowest")
            return (f"{word} {BOT_LABEL[col]}: **{_fmt(ref)}** - " +
                    ", ".join(f"{r.consent_id} ({r.applicant})" for r in top.itertuples()) + "."), view(top)
    if re.search(r"how many|number of|count\b|total number", ql) and not re.search(r"how many conditions|number of conditions", ql):
        return f"**{len(sel)}** consent(s) match ({label}).", view(sel)

    codes = [f"(A{c})" for c in dict.fromkeys(re.findall(r"\ba(\d{1,3})\b", ql))]
    if codes and re.search(r"mean|describe|description|explain|what is|what are|about", ql):
        rr = [dict(consent_id=r.consent_id, **x) for r in sel.itertuples() for x in r.rules if any(c in x["rule"] for c in codes)]
        if rr:
            return f"Rule {', '.join(codes).replace('(', '').replace(')', '')} as written in the memo(s):", pd.DataFrame(rr).fillna(NOT_FOUND)

    if re.search(r"condition", ql) and not re.search(r"how many|number of|count|total|\d+\s*conditions?|most conditions|fewest conditions|average", ql):
        kw = [t for t in tokens if not t.isdigit()]
        hits = []
        for r in sel.itertuples():
            for c in r.conditions:
                blob = f"{c['section']} {c['text']}".lower()
                if all(t in blob for t in kw):
                    hits.append(dict(consent_id=r.consent_id, number=c["number"], section=c["section"], condition=c["text"]))
        if len(sel) > 1 and not kw:
            return f"{len(sel)} consents match ({label}). Name one consent (or add a keyword such as *odour* or *monitoring*) to see its conditions:", view(sel)
        if hits:
            return (f"**{len(hits)}** condition(s) from the consent PDF(s)" + (f" matching *{', '.join(kw)}*" if kw else "") + ":"), pd.DataFrame(hits)
        return f"No condition text matches ({label}).", None

    fields = [(c, lab) for pat, c, lab in FIELD_WORDS if re.search(pat, ql)]
    if len(sel) == 1 and not closest:
        r = sel.iloc[0]
        card = summary_table(r.to_dict())
        if fields:
            parts = [f"{lab} **{_fmt(r[c])}**" for c, lab in fields]
            return f"For **{r.consent_id}** ({_fmt(r.applicant)}): " + "; ".join(parts) + ".", card
        return f"Summary for **{r.consent_id}** ({_fmt(r.applicant)}):", card
    if fields and len(sel) > 1 and any(c not in REG_COLS for c, _ in fields):
        cols = ["consent_id"] + [c for c, _ in fields]
        t = sel[cols].copy()
        for c in cols:
            t[c] = t[c].map(_fmt)
        return f"{', '.join(c.replace('_', ' ') for c, _ in fields)} for the {len(sel)} matching consent(s):", t
    head = f"Closest match for ({label}):" if closest else f"Found **{len(sel)}** consent(s) ({label}):"
    return head, view(sel)


# ------------------------------------------------------------------ map locations
def nztm_to_latlon(e, n):
    """NZTM2000 -> (lat, lon); None if pyproj is not installed."""
    try:
        from pyproj import Transformer
        lon, lat = Transformer.from_crs(2193, 4326, always_xy=True).transform(float(e), float(n))
        return lat, lon
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def geocode(address: str):
    """OpenStreetMap (Nominatim) lookup of the memo's site address; needs internet. Returns (lat, lon) or None."""
    variants = list(dict.fromkeys([address, re.sub(r"^\d+\s*/\s*", "", address)]))   # '3/4 Amokura St' -> '4 Amokura St'
    for q in variants:
        try:
            url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=nz&q="
                   + urllib.parse.quote(f"{q}, Auckland, New Zealand"))
            req = urllib.request.Request(url, headers={"User-Agent": "consent-analytics/1.0 (student project)"})
            with urllib.request.urlopen(req, timeout=8) as r:
                hit = json.load(r)
            time.sleep(1.1)                                                              # Nominatim: max 1 request/second
            if hit:
                return float(hit[0]["lat"]), float(hit[0]["lon"])
        except Exception:
            continue
    return None


def locate(row):
    """(lat, lon, basis): NZTM printed in the PDFs first, otherwise the memo's site address looked up online."""
    if row["nztm"]:
        ll = nztm_to_latlon(*row["nztm"])
        if ll:
            return ll[0], ll[1], "NZTM reference in the PDFs"
    if row["site_address"]:
        ll = geocode(row["site_address"])
        if ll:
            return ll[0], ll[1], "Memo site address (OpenStreetMap lookup)"
    return None, None, "Not located"


AUCKLAND = {"lat": -36.85, "lon": 174.76}   # default map centre


# ------------------------------------------------------------------ dashboard helpers
def expiring_soon(df: pd.DataFrame, as_at: date, years: int) -> pd.DataFrame:
    """Consents whose expiry falls between `as_at` and `years` years later."""
    d = df[df.date_expiry.map(lambda x: isinstance(x, date))].copy()
    limit = add_years(as_at, years)
    d = d[(d.date_expiry >= as_at) & (d.date_expiry <= limit)]
    d["days_left"] = d.date_expiry.map(lambda x: (x - as_at).days)
    return d.sort_values("days_left")


def shortfall(df: pd.DataFrame) -> pd.DataFrame:
    """Consents where the memo states both years requested and years granted and fewer were granted."""
    d = df[df.years_requested.notna() & df.years_granted.notna()].copy()
    d = d[d.years_granted < d.years_requested]
    d["short_by"] = d.years_requested - d.years_granted
    return d.sort_values("short_by", ascending=False)


def build_excel(df: pd.DataFrame) -> bytes:
    """Workbook with the register, the triggered rules and every condition (needs openpyxl)."""
    rules = pd.DataFrame([dict(consent_id=r.consent_id, **x) for r in df.itertuples() for x in r.rules])
    conds = pd.DataFrame([dict(consent_id=r.consent_id, **x) for r in df.itertuples() for x in r.conditions])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, t in (("Register", register_view(df)), ("Triggered rules", rules.fillna(NOT_FOUND)), ("Conditions", conds)):
            t.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col[:60] if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(width + 2, 60)
            ws.freeze_panes = "A2"
    return buf.getvalue()


def build_pdf(df: pd.DataFrame, as_at: date) -> bytes:
    """Landscape summary table of the register (needs reportlab)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=7.5, leading=9)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    story = [Paragraph("Air Discharge Consent Summary", styles["Title"]),
             Paragraph(f"Status as of {as_at.day} {as_at:%B %Y} - {len(df)} consent(s). Conditions are from the consent PDFs; "
                       "everything else is from the memo PDFs.", styles["Normal"]), Spacer(1, 10)]
    head = ["Consent", "Applicant", "Site address", "Status", "Granted", "Expires", "Years (req.)", "Activity class", "Rules triggered", "Conditions"]
    data = [head]
    for r in df.itertuples():
        yrs = f"{_fmt(r.years_granted)} ({_fmt(r.years_requested)})"
        data.append([Paragraph(str(x), cell) for x in (
            r.consent_id, _fmt(r.applicant), _fmt(r.site_address), r.status, _fmt(r.date_granted), _fmt(r.date_expiry), yrs,
            _fmt(r.activity_status), _fmt(r.rule_text or None), r.n_conditions)])
    t = Table(data, repeatRows=1, colWidths=[62, 90, 105, 44, 62, 62, 52, 66, 190, 40])
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("FONTSIZE", (0, 0), (-1, 0), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
             ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")])]
    for k, st_ in enumerate(df.status, start=1):
        style.append(("BACKGROUND", (3, k), (3, k), colors.HexColor({"Active": "#dcfce7", "Expired": "#fee2e2"}.get(st_, "#e5e7eb"))))
    t.setStyle(TableStyle(style))
    story.append(t)
    doc.build(story)
    return buf.getvalue()


# ------------------------------------------------------------------ UI
STATUS_COLOURS = {"Active": "#16a34a", "Expired": "#dc2626", NOT_FOUND: "#9ca3af"}
PALETTE = ["#0f766e", "#2563eb", "#f59e0b", "#7c3aed", "#db2777", "#0891b2", "#64748b"]
# Auckland Council logo (embedded so the app stays a single file)
LOGO_B64 = "iVBORw0KGgoAAAANSUhEUgAAAIoAAACgCAIAAABc0mNjAAAMTWlDQ1BJQ0MgUHJvZmlsZQAAeJyVVwdYU8kWnltSIQQIREBK6E0QkRJASggtgPQuKiEJEEqMCUHFjiy7gmsXEazoKoiCqysgiw11bSyKvS8WVJR1cV3sypsQQJd95XvzfXPnv/+c+eecc+feOwMAvYsvleaimgDkSfJlMcH+rKTkFBbpGaACHGgCNtDhC+RSTlRUOIBluP17eX0NIMr2soNS65/9/7VoCUVyAQBIFMTpQrkgD+KfAMBbBVJZPgBEKeTNZ+VLlXgtxDoy6CDENUqcqcKtSpyuwhcHbeJiuBA/AoCszufLMgHQ6IM8q0CQCXXoMFrgJBGKJRD7QeyTlzdDCPEiiG2gDZyTrtRnp3+lk/k3zfQRTT4/cwSrYhks5ACxXJrLn/N/puN/l7xcxfAc1rCqZ8lCYpQxw7w9ypkRpsTqEL+VpEdEQqwNAIqLhYP2SszMUoTEq+xRG4GcC3MGmBBPkufG8ob4GCE/IAxiQ4gzJLkR4UM2RRniIKUNzB9aIc7nxUGsB3GNSB4YO2RzTDYjZnjeaxkyLmeIf8qXDfqg1P+syInnqPQx7SwRb0gfcyzMikuEmApxQIE4IQJiDYgj5DmxYUM2qYVZ3IhhG5kiRhmLBcQykSTYX6WPlWfIgmKG7HfnyYdjx45liXkRQ/hSflZciCpX2CMBf9B/GAvWJ5Jw4od1RPKk8OFYhKKAQFXsOFkkiY9V8bieNN8/RjUWt5PmRg3Z4/6i3GAlbwZxnLwgdnhsQT5cnCp9vESaHxWn8hOvzOaHRqn8wfeBcMAFAYAFFLCmgxkgG4g7ept64Z2qJwjwgQxkAhFwGGKGRyQO9kjgNRYUgt8hEgH5yDj/wV4RKID8p1GskhOPcKqrA8gY6lOq5IDHEOeBMJAL7xWDSpIRDxLAI8iI/+ERH1YBjCEXVmX/v+eH2S8MBzLhQ4xieEYWfdiSGEgMIIYQg4i2uAHug3vh4fDqB6szzsY9huP4Yk94TOgkPCBcJXQRbk4XF8lGeTkZdEH9oKH8pH+dH9wKarri/rg3VIfKOBM3AA64C5yHg/vCmV0hyx3yW5kV1ijtv0Xw1RMasqM4UVDKGIofxWb0SA07DdcRFWWuv86Pytf0kXxzR3pGz8/9KvtC2IaNtsS+ww5gp7Hj2FmsFWsCLOwo1oy1Y4eVeGTFPRpcccOzxQz6kwN1Rq+ZL09WmUm5U51Tj9NHVV++aHa+8mXkzpDOkYkzs/JZHPjHELF4EoHjOJazk7MbAMr/j+rz9ip68L+CMNu/cEt+A8D76MDAwM9fuNCjAPzoDj8Jh75wNmz4a1ED4MwhgUJWoOJw5YUAvxx0+PbpA2NgDmxgPM7ADXgBPxAIQkEkiAPJYBr0PguucxmYBeaBxaAElIGVYB2oBFvAdlAD9oL9oAm0guPgF3AeXARXwW24errBc9AHXoMPCIKQEBrCQPQRE8QSsUecETbigwQi4UgMkoykIZmIBFEg85AlSBmyGqlEtiG1yI/IIeQ4chbpRG4i95Ee5E/kPYqh6qgOaoRaoeNRNspBw9A4dCqaic5EC9FidDlagVaje9BG9Dh6Hr2KdqHP0X4MYGoYEzPFHDA2xsUisRQsA5NhC7BSrByrxuqxFvicL2NdWC/2DifiDJyFO8AVHILH4wJ8Jr4AX4ZX4jV4I34Sv4zfx/vwzwQawZBgT/Ak8AhJhEzCLEIJoZywk3CQcAq+S92E10QikUm0JrrDdzGZmE2cS1xG3ERsIB4jdhIfEvtJJJI+yZ7kTYok8Un5pBLSBtIe0lHSJVI36S1ZjWxCdiYHkVPIEnIRuZy8m3yEfIn8hPyBokmxpHhSIilCyhzKCsoOSgvlAqWb8oGqRbWmelPjqNnUxdQKaj31FPUO9ZWampqZmodatJpYbZFahdo+tTNq99XeqWur26lz1VPVFerL1XepH1O/qf6KRqNZ0fxoKbR82nJaLe0E7R7trQZDw1GDpyHUWKhRpdGocUnjBZ1Ct6Rz6NPohfRy+gH6BXqvJkXTSpOryddcoFmleUjzuma/FkNrglakVp7WMq3dWme1nmqTtK20A7WF2sXa27VPaD9kYAxzBpchYCxh7GCcYnTrEHWsdXg62TplOnt1OnT6dLV1XXQTdGfrVuke1u1iYkwrJo+Zy1zB3M+8xnw/xmgMZ4xozNIx9WMujXmjN1bPT0+kV6rXoHdV770+Sz9QP0d/lX6T/l0D3MDOINpglsFmg1MGvWN1xnqNFYwtHbt/7C1D1NDOMMZwruF2w3bDfiNjo2AjqdEGoxNGvcZMYz/jbOO1xkeMe0wYJj4mYpO1JkdNnrF0WRxWLquCdZLVZ2poGmKqMN1m2mH6wczaLN6syKzB7K451ZxtnmG+1rzNvM/CxGKyxTyLOotblhRLtmWW5XrL05ZvrKytEq2+tWqyemqtZ82zLrSus75jQ7PxtZlpU21zxZZoy7bNsd1ke9EOtXO1y7Krsrtgj9q72YvtN9l3jiOM8xgnGVc97rqDugPHocChzuG+I9Mx3LHIscnxxXiL8SnjV40/Pf6zk6tTrtMOp9sTtCeETiia0DLhT2c7Z4FzlfOVibSJQRMXTmye+NLF3kXkstnlhivDdbLrt65trp/c3N1kbvVuPe4W7mnuG92vs3XYUexl7DMeBA9/j4UerR7vPN088z33e/7h5eCV47Xb6+kk60miSTsmPfQ28+Z7b/Pu8mH5pPls9enyNfXl+1b7PvAz9xP67fR7wrHlZHP2cF74O/nL/A/6v+F6cudzjwVgAcEBpQEdgdqB8YGVgfeCzIIyg+qC+oJdg+cGHwshhISFrAq5zjPiCXi1vL5Q99D5oSfD1MNiwyrDHoTbhcvCWyajk0Mnr5l8J8IyQhLRFAkieZFrIu9GWUfNjPo5mhgdFV0V/ThmQsy8mNOxjNjpsbtjX8f5x62Iux1vE6+Ib0ugJ6Qm1Ca8SQxIXJ3YlTQ+aX7S+WSDZHFycwopJSFlZ0r/lMAp66Z0p7qmlqRem2o9dfbUs9MMpuVOOzydPp0//UAaIS0xbXfaR34kv5rfn85L35jeJ+AK1gueC/2Ea4U9Im/RatGTDO+M1RlPM70z12T2ZPlmlWf1irniSvHL7JDsLdlvciJzduUM5CbmNuSR89LyDkm0JTmSkzOMZ8ye0Sm1l5ZIu2Z6zlw3s08WJtspR+RT5c35OnCj366wUXyjuF/gU1BV8HZWwqwDs7VmS2a3z7Gbs3TOk8Kgwh/m4nMFc9vmmc5bPO/+fM78bQuQBekL2haaLyxe2L0oeFHNYurinMW/FjkVrS76a0nikpZio+JFxQ+/Cf6mrkSjRFZy/Vuvb7d8h38n/q5j6cSlG5Z+LhWWnitzKisv+7hMsOzc9xO+r/h+YHnG8o4Vbis2rySulKy8tsp3Vc1qrdWFqx+umbymcS1rbenav9ZNX3e23KV8y3rqesX6rorwiuYNFhtWbvhYmVV5tcq/qmGj4calG99sEm66tNlvc/0Woy1lW95vFW+9sS14W2O1VXX5duL2gu2PdyTsOP0D+4fanQY7y3Z+2iXZ1VUTU3Oy1r22drfh7hV1aJ2irmdP6p6LewP2Ntc71G9rYDaU7QP7FPue/Zj247X9YfvbDrAP1P9k+dPGg4yDpY1I45zGvqaspq7m5ObOQ6GH2lq8Wg7+7PjzrlbT1qrDuodXHKEeKT4ycLTwaP8x6bHe45nHH7ZNb7t9IunElZPRJztOhZ0680vQLydOc04fPeN9pvWs59lD59jnms67nW9sd20/+Kvrrwc73DoaL7hfaL7ocbGlc1LnkUu+l45fDrj8yxXelfNXI652Xou/duN66vWuG8IbT2/m3nx5q+DWh9uL7hDulN7VvFt+z/Be9W+2vzV0uXUdvh9wv/1B7IPbDwUPnz+SP/rYXfyY9rj8icmT2qfOT1t7gnouPpvyrPu59PmH3pLftX7f+MLmxU9/+P3R3pfU1/1S9nLgz2Wv9F/t+svlr7b+qP57r/Nef3hT+lb/bc079rvT7xPfP/kw6yPpY8Un208tn8M+3xnIGxiQ8mX8wa0ABpRHmwwA/twFAC0ZAAY8N1KnqM6HgwVRnWkHEfhPWHWGHCxw51IP9/TRvXB3cx2AfTsAsIL69FQAomgAxHkAdOLEkTp8lhs8dyoLEZ4NtgZ/Ss9LB/+mqM6kX/k9ugVKVRcwuv0XRvmDFy2WstEAAIiCSURBVHja7P1nlF3XdSaKfnOutfY+oXICCoVCzoEkCDBnUqIkS7KCFSxLlnXtbsfu137d7nbfMfr6+ro92m0/h7bdfdtqh7YtW5ZtBUuiEsUoZhA5xwJQQBUq5xP23mvN+X7sUwWQIinxPVvt66EzMDgwQKDOOXulub75BVJV/CN+KUQhrAxhMFLVTBUkDjAiBgHi/dj06KkLs1dGtJbCmmJ3e8/G1U2rVyAueECNrftAbGJjrYIAkCgJwATGP+6X/UcxBq+cIkR07feNv0EKKBCYqlm9YIkyb8DJ6XPDTz934pnnKhculxNvRRMJtciYZV1rbtix9q0Pdd66W4qxJYgxAgWRBhAREV41NG/wGf4XvugfxerRpXH49v+jUIEwQMKokaShXhaJF+ojLxx44ZN/WD95vAjqcnGbcxAvoKroRJpUJfhlvdvf/0M7P/heWtaeWgMTRUoQAEok4G8bon98r/81w7P0pgoF6NpUVX3VzFUoVEkIoGCQIBj1cbU+9uhzT/zW70VjV1eUi6rBKEgVUMvGqwRVZ6PZVC7W0x0feP8t//Kf+84WiWNLxggADawEYlz/Ro2Plb+7qgJKiwv4f9Vi+h5tbqLgxVWiBFUlgEDEDK/wCUBghmMQqQpASgRRYigBRPnuZiAuyNzBky//wR+XR6+ua2nVkCYCD3gFiIOoIy4yG9G2QtGKnPjil4o9nds+8WF1BoaFSAgCZVybFBCBKBERMdIUCrIWlgGoCKBKJh+/ayNK/1SGJ4MGhauJl2CLkZKyYfJpMnBl4sTZhfMD6cRYULWd7aW1/R2b1rdt3piyyYRITUzMjjKG8fAiajzPzl7+i78pnD/T2lyeyypOiYgMEQO6uFvVxTMoyeplZ1f67Nxn/mrV7dtb77xNvQQ2nihSUUVNWSWzRuMgMjY+cfr8yIkzfmIKWUCh2NS/qmfbxtYNa7SpkCIjF0lARIRUwMqx+ScyPKIIBGLlyAafOUFyZfT01x8999iTlcFLqM5Zn4EIzmVRobSsd9u99695ywPFrRuyEnujjgja2BIjuOkTp0YPHWmKjQFSAET5vKbFCZ1vUQIQgUBlaxZmZ049/vRtN98MG5EirzIUYlS57u185eJTzx7/2tenz5zNJiebjGUN9UC+UIq6Ovp37drxrre37tou7a5GIZCzhNiafzpnTwCqqTcEhsZeq0fOvvh7nxx66YWVcQStO2tBRCADcsrey1wgWrX6pp/9ic633p7GZFxsxCDAk0SaHfjt/zb0x5/qbyp4CT4ES290vBO7VPxMvTazZvX7/vvvFdatC14zB4LnkJlUdXDyyB//5alHHimn9dbYNscRiTBUwInqfJpOJVmht3/7Rz6w/iPvnm2NPEclsSXD/3Q2N1Y4AVgipvkjJ5/4ld+w587d0BRH5EUpaBDYVL0FNbEplVyn0PmLZ7/xG//5HaV/33HPnmAdCUPJMMlcfeb0pZJqEAFg2eD1p1dQIRWotEXxwtjM/KWhwvp1JCCFkhqIH7r67H/55NTjT+8ol+MYQUMIiWNjALC4kMYOywrFyYmh/Z/8w2Bk849/qGbJiEoQtt+Lqu97UlkGsVlwCn91Yu//+DN/6kR/kS3SRJK6JECwBCJ4hIqkk9nCQqg1l9hMjn3rd363fmbAEsEHQAkU5mthei4m8uJFRTW8wdsatkxsiAqE5oBkau7abqvKteTlT3/mzJOPLysYp3WCTxGqCLOhXpGk7uup+pqmCr+qpbwyJIf+9C+vPvFSMVUbhL9Xhdz3pvAnqJo0XPzGUyPPP7++uRRTqEm6oN4TFBAVqAASIDWVCrwg9JRiOTdw5vNfQTWRLFUVAJIFiAQIEWchozfcYvJ15dhYZqhKlimgIhzECU/vPz7wlW/0Go40BWRBsnnxFQRPEEBVRTUhnQlJNav0FG1xYvT4X34uXB4DVEj/6QyPkFDByOzCwBPPtGaJg8/EC0AgAwYoU7FEJeIiUYnZqbKGJqDT0sWnn50+etpERkmJYctlLZU9DIgIyvQdLruqSqpB1EcRNxUlhBDEgE1dLnz9qdaJqV5nWqx11qYaBAoFKRlwkV2JnYMJQE0kC2lfuTR/8uTV/YdhKXvDVfv/tNVDMJEbO3l64vTptigiIICYyME6OIURUH4NMkROUQTKoALQEhdqQ1fH9h+FSGCIqGmK45U9c6ICcsbkS+r1hye/WmoSZK5km/tXQNU4ZuaFwdGJw8d7oyhWDcFXs7oBO1ABbMEKFhgL10SuAJNBqxJKzsn0xNXDxyhL8U9p9TAUQUbPDNhqveicghXkYA1cps6rMeCgVFHMCzJQbFyBXRAB2yYy9YHLmmXCFERQcKt376RiIYh3bPEdzwBVy1z3vmfHpqY1vUxElkGYvTg0c3VUDXtQjagahIlj4hiGYBLlaTEVcQVEZbICqgGphHZjsktDMjnriP/RD49eQ2hUFfpK0EZV0ThSNCjmqlSrw2eiQQn5WhFwVdkrG6gHKqrz0AVFTTRVDTBeyYH83DwZUlIiqKG+3TdF/SunagkRR2QABVFjr4PkdyQFmIihCs2UF6zd/vCDprUpiHgvSCXMVyvVShVYgM6FIMZ6VQKBIIBXzAvNKykoAhMoAzLJSsZgasbPVBiAvPLrikKgCkF+dOWHaeM5yXXb7T/Q8CgQII1LnQZoBniIwosGrxqgAhGVIKoiGgKQqSZBVAkiIfiEkUJJxYIEGqAKDQgZPBBiQgnswJlSXaEQB8+kcAbwhBTqJc1c37J1P/rhoWLzQh1lE1uwhyYITKHJkCGpIdTVO6DV2HJUOD2/EN19Z/eddwixGIIG1FIiG5uoyEYAIRZoUMk0JOoFWZF9E4sFMlUPZYBUQCz50yWoiAQPVQECELxKqhogipqgpggCTVUSzTIkQAYEVfUBPsObObfsmxgdVSUVIl6aBAwhzYwIqwEMUZAMJMysCiMUwYhXKNDWhNamjJjATOo1qCKCRA0sDQxigPOhVwXIMhkyiXpqLuePhNQwGYl5y7veVjl/6cxf/I3W09Y4oqxGEDFcVU1UWFHgiNguEF+YnY83b9790Q+bjlYBK7NxhAimuRgXS0jqlsgSQcSxkcWTTBUFUkIA+QQhQApkHFBV2NYW29akzCTsVQEYIjGUkQgLM7FoEE2InCOjxAAFEIEUYAQCvxl49c1dSwOpQklBDDEk8OqDgRgGKOMAy+QlZAIQixfDbEApCIZ7NqznQlGzBAYe6qGO1EEDkKoxUAC8OFoKIqUgukBUWrMKccwgJqNAIhLF5uaPfSA29uW/+HTrzPSycjEiFlAKMLjMrGSGa8mlLGvZseOhf/kzrTdsSzMv1okqMaNgmvu7456O7OJkwRFUmHkRJCUBpaASQkShBl9TL9AiUQReCL5r5XLqaPGgCAwflEQsp5qJUUOGNDgv4Bx4pWAMw1iBBgVBiQMRQObvfXiEEACFGhUCAkmAGIILgmqCSsVX5yHQyEYtTVFLWSOTOQQRWOM9fJBlGzb2bdpSO7RfnVEgg4qKkiiMggHKAXwGlCgoiHghy0or+np23aDGsDLBKAOGMkW0omvbj3+41L/i1Oe/OHryVCdJMbKQwKBMdCStV3q6t7z1wd0f/kCht7vOagplURWV/JRsWtXdvWPL+JmT/YXmSL0yL/iaIcNgr5SCPAIhJBAPiUExIQikuXX5zTdI5DzUGJAwNANCwYBqdZ2tyEJNkoyj2LU0oa1JYyQhEbKOLQSkxEpvqjdhv/u9zRNZVZJABA2ps8zTlbGXjlx5fv/4qVNUqwSmlKllxYq+G3euvH1Paf3qrMg1QmakCGPbW9fecceRw0c6lBiWkHqoVzEgRy6vIRqnLKBk68QTmay87daOLRuyoET5PZM0iBhTM1pob1n3gR9YeetNVx55/OxnP6szU51xnAY/Y6K+h+/d8NH3td64PcTFekiJLBsbAT54kqAi3BRvfPuDlx5/ciZN2wwQAoEM4EBEpMqCUENIJDBRiawhd7WeRju39+6+kTw4Mp6UxTvDOj0zsv/w5Wdfmjxxmmt1S+yta1q5YvnNO1fedUth/cqMRMgbGFUyQsRvohlhfvmXf/m7HR7xDuRDBkldmlWPnH3h9/5o3598av7Icb0yGE2NZ+Nj6ejYwsCF4X0HL794ENVs2bo1vkCeyMCxSFtX9/Tg0NDZs+2FQgRlIiUwsYMFKXFeqARVChwPVmp2zdrbf/Yno/X9QjBkCJRfZgwzGbbGBRHT0dxx07bYuEsvHyhBJ+tp99333vxvfy7eviFh9mzIOkNsBQxYJstEDGGUujpmhsfOHjneFhdEgwEiJscMUQURBUEIokWOiiaeqGfjTa03/dQn2m+5CcqGmXxqq7XJvYdf+v0/Ovqnf1k7eJiHh3hygicn6yPDMxcuDu49cO6lfUVyXX0rKHKBQMwISkx//8MDBUl+sovx/tKj33r2P/9+fe++VcYUKXSVSh2xLUVRcxx1ONdjHE9OXjl4bHZ0YuXm9a65rMal4uO2pubuzstnBuZGJ9qiqGDYAF4kNJrZQlAiFfBkJgtd3bf95I/33HVriK0ScQ5rI98KGcSsZODSELRompe1Dx86NjM45FtaVn/kfW137VEYMk6JQYjyBoOCKD8DCAQ429m/cvjcxakrwwWSIrMhElUlEpUCG6gwRcZEk9VkxBZ3/dRPrP3QO9NiBBjWYLLsypcee/E3fg9Hj/YztUemHEdNUdwaRUVnm51tZ85GRs8/tzebqy3fuCHEBs75IMxETH/fwwNiZvjMeJnef+yx//y7pUuD21pbiiw+JEE9QF7Ea7CQEmlXHMcIZ0+cSOcXVt14oxZL7GwmWcuK5f2btwwPXrl8aZDIJCKFYksKrYWUGUBwZJKg03C7Pv6jGz7w7qwYqcm/jQYS5Ua/UolYgES8oO602BKPvHTw6pET8fJl697/zlL/KqgxxAQhgskb5qSN5hCIwJ7hWlv6+lZdfPlgtFB1yAKhTkgBZQOyiZqZQJcWalnPilv++U+s/8h754qUEpOQ8+mVx5964j//Vt/s7LrmotGsLj5TgSqrKoIlLWhYViyWFKcOHSHY3ht3ajESQyA23/Xx892ePQEqpDYL2cDwS//1T+3gleXlYiaZSOoYChKFBwI0U9TgvfeRjbsjGnjkK70btqz80Y+mWebiOBhfvmnjA//xF89+5dFzjz4+Pzjo5mZMqJNmbdaUHJfjZk19U1fP6rvvTEqxdyZShaqSeBIGGTYAPIMErGScJVLJpGBjB+KgRg08iZAaMVY4b9k1akIiJdacTAUffOvWzRtvu/3yZ/+ms8lVQjqZJF7BgkxMhZztXb7ytj3bHn5Lz203J0VHREV1DMoGhvf+wZ8U5+dam8upr2X5XVuJke/VmoWUjA2+3mSjlVF84HNf7Ny+re8H7q8gKxbKf/+lgUCTNI18OPeNJ6uHjq4tl5lCVbyILxjD4FRAUCg8VIGq+Ii4aE2HT0783Zfbb7mtuHmViIiyRuQ29O345z+y7oG7x46drF4YwNS4ycLC5cuTA+cozVS1ublMRadMUOYcq14iBjA8IEAgcExK4AD2zHVfZnZeJFEiK6RsCZR3dxCgAiKozdkOACuzjWFCsViA+Ezj0SRw/+q1mzYV4zgtNEVr1nZu3di1fSPaWxJVhYkF7L0N9vRnH+Gz53rKxYW0VjYctMFcIKiqMiM2Lg2B2bCiYOHmpo78zed7tm0sbFzxploR3+3wOLA1LlQnL+39VovMF7kUQigwJ2wTCYBYsk7J5LQWAOQQJGKKosLIhfNT+15eedPKDETBOmXKgnBU2r55zc5tyBLUFmSeQiU7+8jXTn3qj3poNpu+UpsYbN20VoIaw4r81kO0+KAjgBnEsCpMjiDQIJRmFlQow8CYQGxE7RJRq1G8L/I4gioztDKTnj9VMDLpbWHXg3f+/M+1rlpGsaCpCBshR6SUnEANpTYQaXpxeOzgwVaWMpQAL6qwhMaumzUuh1AyGVgkM/BrS2bi5NGJg0d7N/TLPwSoQwAbMzM8ko5PlJkU4vOL5HUktQb5BkQgJmZiqDIRBz926jSSzJE1nLeyOKc0BRGxRtuaQ0cLrVy29aM/tPGht2RKlenJs08+S15gSKmBfBJAOc0TS9WpEikR5XMiB7rADAJBFaqU1xvIb1Tc4DRCCERkVWfPnBsdOA92aWvbnf/sx1u3bpLWZmlvziJOkYmExixQhWrQYK2dPH124spg5Byp5CwrLH6A/Dl4VQUMMSkUQkBzFKFaGTt3AUJvCnXj7/reowBmhsazqZmStaJIgaBgkKE3KuQVYNXxS1e07g1YRYWgFjAAkxgWIHjVks3Yo6vc//A99XITfJh88VB6aVANPKlwgwCX8zsswA067ne4DMj1lefiXxeCANaA03D+iRfHR0cXgq556N62PZuSkKBAdclSCIwh5hwkJWYFGExB56+MVGfnmAn62u8vgEJtY2siAYlIBJ69clUr9QZU+/c7PJJzZWqpy3zJWFFNFR5gkGmsmNd/DyLjRVMPVVIIQZjApAzNDxdlBbRo61Zbbt7atvsmhskGhy4+/axVnxMGrtFJlYw2CuXvDBNe9z3zf9JYT1BDqJy7OPT8Pg6qbe39D9wpbUXXFIkqWwtiAiuTMCQv+UQsGwQxaYjY6BuXUYAFHOVgGwGI2FA9k1pG/xCbm4hogDXWmZjJChBAIT9jc8bSG7QcQFEcIwiCMqGB+VODkUhgJmsDJNPghbvbVz50r0RlTusXn34mjE1aIMfrGyROgOS7mn+09Ou6gRJA8n9cqw8/92I6fLVgi81bt7bs2JRChIDMU+YbrSRCIAoEUYEoqYIZhkVFcX0TRb9t9YABAxAgqsZEdZ+xcWwj1TeBGnzXm5uoqMYtLaEQV0KQxVNHFk+CVzwXIhCCCqBMFADT0szFIuTVH43zdiaIg5YUMYwnWn7bLYW16wCunjg1+dw+4wNJphK8BCXSpQPou2A4sIDD4rpRBNUAVRHKxF8Zvfjk03GWkStueOg+09lmAGPI2sgZ58DmOpqxKgikImDijhaxhhR5vUL0bfT5xUECYECGuOJ9ndh1tFMcv6mmz3c7PNYYJurcsDrtah1NakRsQQB5JQVetXpe1ZlLgba1/VQughmASE4ZVOQNNGhgKBEFkCLzcL19K+6/r84cVxauPPmMTMxS6plZkXe8rj+I9TvR+NDoUQFBxaukWeZAXA+zh07Vz54FQmHFyt47blViK/n30Ly7kU+E67doAglC5+Y1HX19wYfIOqbGyH37M/WAgCyIiRPiWWfbtm6Ec4vf4O91eJhIMon6e3rvvnXEexAXiQnIAP221ZN3SxlExFmWtXR29t26Ww0U6rPAOd8aje4KK4ihBmD1DB8ggpX33MbLew3T6OGjc6cuGCUNokReFumgjdvLd7vBKQFMymyYNQ00n1564mmdm9ao2PfAPdGalaKLbU7KG5+LsIrC5MsHIKZMQ/PG1cs3rkvTJAmpqL5yl8svK0RAqhoUhghKo7WKW7Nyxa5tym9ibN5cM1sB73jzO+5rX7u6Xk9K3Ojyhm+bPkuPhgjeZ6WOjvLK5QGqQQA1RCx67WkAJi87IqmzGBeBXfPq1b237p5DmB69ev6ZF1FLQsiUSRfrUmEIvsvSoAHnCZMAFsyZTp25ePno8YLleHnPyoduE8tCRgESFdbGM6fG7DFyXc+aQM2FlmXLCjYmIiIKEl61ueW7YgpkUEtsSD2b7W+5r2nT6kD6pmi5/O3fZ7Fb3uifNwRqQUVDReotm9bc8M53JOwyUcMGmkNiSzMo70+L5EcSETNVawvp/IwhIoazttEQRt50FYIYBUMzBghBNWOgGK+5747Q3gaRS3tfrA+PGhAtirCEQPpdzSzNCwmE/BoEVYgYlaG9e9Op8apI543bmresSUPmRYQaA5lPcAVpXngJoAiAzx9HliSV+dSnaMBDbJgZypS3E1WhhpgAwyyEsUp15Y037fiBt4XIcgP708VHtcTIeO1R4+tJeyLwQApNIT6IpkFTkVTTTBMNsN4p+7ip/yMfXPPRj11WO5/Uy5ZAQVgNGYWAoMwpUw06H7KKT+MoTkZGBr78NZqd8yEJLCIQhSgzlCiBplAQcZPaMlnLJEaTArpv2N604QYQ6eVTYy/tszXlQCHAQwMFiHJAPmCNizG98iZERGADJc6IEqOZU7JCjkwyPjz53FdK6UK1qbPvrfdpS1lFC8SGIJaJjLIJoKAQkDLgg2Q+kKQ+Kfow88L+yy+/VCwV6iqTIalBEskcU8wEBIWCOGZbZlbxA/MLuuOGXT//s2bNKpCB5ARHUe/Ve0gQDamGuogXbXQ8r7sS8CtwAYEJcEGdF4I2SndLEciBiWxRClYL2tm54ad+dPO/+KnRZSvOzaeTNZ3LsBBE4UhtPclmFpLZTDNXqmQ6VU2abHThy984+T/+ylSriSQZiQooqM1U1QQ2+YfJm6ZMxEQgMq1NWx64L0Sx1moDTz/nx6aMqLOmIfh5/Sp+CbqhV1aTIpqlnoErL+0fP38mg/becEPPji1BJIrjpf25gf0wK4GCUiZgltjUs1op83Pf2n/wv3wSI2OOCwu1LLXFmiuNVNPxalLJNBNS5aB2rJZcrKXHQW3vfPu9v/Rvm27ekYoSMTFRppRBNSczCIMi5TiQ8VB/7WvQa2BupAQlDUKSwqcqzkQxLHuloHA2EMAmiLfNTZs/+sEVu24+99Vvjh04PHp50GZ1EniC61nRvLZ/ze6buvr6auPT55978crLL3cEPfDnn06ZbvzJj6bNBDgjLDD5SXTtgeZnKQA2Gknf7bs6Nm7wJ49OnTg7efhkz+pe8WlsLCmUv3sqoAFIiYXgmMPI1MWvPxkHrRVLO++703S2pyqkygRaPGZ0cS2yVwg0otSnRcLC84ef/0+/XRy+bGHny51b331v+01b0pAMnjh95eDR8SsjhQBLPCuJXdHXsWHdjofuX/3g3dzZGgAmoyzKglQ0UIjIM0QkVorIgSCEDDCLR9erIVElFc5Pa1H1lmHBVgJnHqnCMzyHkkkJzhhk6oHyjs27Nm6sXxmtDg2F6YksTbi5qbSyp9jb6bpafPBNsC0P3ZX+tz+affTJdsixP/yfUci2/rMP+9amYEoB1gSwQgxYoZLLCXMZHAehQn/P6jtuO3H8WDw7O/jUc8seuj1qK5AqKQt/56Yw5VcWMBEFsJLGlkdePlg9cTJipnWre+/craxCBqINScgiYkpQEgIbkKr4Zmvnnz/y9K/9trtyCazVFf03/+RP9b71Lh+H5qJrrz2wZXSmOjiajEwYAbUWiit7Wnv7UG7yRCEwWWOsMAIBMAxlKz5SFSYQRDwbI4a84lWNOvtKNEKYQN5bwF+dnD17ceLM+cnhqyFIc6GlrWN5283bWresoVLRpylHERnjjbgd/S3b+y18Q4AoIYivQ4K1DLgNq+7933/+QDG+8IVHVsXR+U/9FWrVLT/1I2mvFSUWIwYecI2zGPn2LSpqLVuz6p47B7/yqLs6On3o8Pyp08133ChBRClwLknVN7r5qJKSEOXiVIJibv7S40+6hfkqybp77iitXxUoGLIg25Br6eLqCUpKalWDGJHxrz+997d+v3D1Sma5tnrlnb/4C+133+GN1jQLolGhWF7TWl6ztvGuRjLxiTfkidkYMAIYqA8MThw/NTl0dWFu3tfrrW1tXf19bZvWNW1ch9gRkWXD+TpaFJLZ61A1lRBsFnhqbvDpF0586SuVswOozmmWCjBhY3bFpKlpxV23bnn/O7r2bK/DuxAbMgtpwpEzahp4ExlYw5qz96CZaEfrzf/yfzNsz//t55Y7c+Fvv1DPspv+9c9IRxRIM1VGvr+JihhDykTgoCrELds2d+/aNTnyjXR8eOiFF7fs2qKuEERFNJjG+iACmKFQVX7F6AhEG/QYVUuonr+wcPhITJwu6113/93qHAkZsku4T15c5EB0UPE+K6hOPvHCC//pN5snx2sSkvWr3vor/7Z1960ZLAgRLBkYJRVVVRCDkaYS2JAxAbCqRVA6NHrqK49e/Obj9fMDIa1AglWdNuacsbanZ9X9925599tK2zbAQnJqDBTGqMi1ZrYgGKPh0shLv//Hh//nn0dXrnQhtBtaVohXFuIOywX4Um1h4tixi/sPawg9G9aoRYAqkzGGmJe6CbrUVhAyyhRES275nh3V+erQsZPNjOFjp7Lp+ood21F0XsWyyRGtvCrNuUZCTCAmompy8aWXbTJXqdf6b7vFtrcTW2tYkIt+GMQUZOSbT8yePkmtHf1vfbjUt0xJiEiDgJgMq6SmXrnw+S9PfuuZmkr3A/eu/6F3+zhmZVbWJeovNK+tvfiQ1ePgR558/qXf/L3S+FiAVteueuCXf7H9tj2JMsgyyCqZnLpABOZGe4PZe4VhZFkZvHD0zHO/+fvn/vbzxdGryw31x7bLaXtkWwy1Enhudujg0UtHT3T09jT1dpCNmF2OalNui6GqImJU9er4U7/3Byc/94UtLu4vx45VDTKEIKlIEiRpcdjW2rRsZOj47/3hxU8/YtMAR8YZCSEsNjTza0MOdhLnGxYrxb6tZc+/+5mNH//InC10xsXjf/vZl37j93noagFqvDIAJraspJTD0iBWUjbL7twTbV+/kFUXzpwefW4viaZZoAASfWOFAqCUwwQi8Fk6OHTpqadDqNXL5VUP3Ect7UEIajTXy+VXPWjOhbXii6JXvvrkU7/6G/HEeEV8tmnd2379l1t335TUhdTm/aV8cxVCIAg1noAQmdiZzJehCwcOP/V//ceFbz25Lca65pKhAMkiMplqHSBCV+xuaG/LTp742n/6/4w9v48F4oOKEBMRLw4PQPP1U5/89NhjT21sLpUpM+qDpKmEqviFIDVpgImsfnmx1B3C3v/5V+PPHnEZIxAZQxBudFJkcXQEpGABMUvEEiWlwg3/4sc2/cj7J3zWE0cXv/yVfb//Sb0yTkLBhyxLc9F2fl9jzTFjY5Z1rn34blcuNmfp4DMvhEpindFMKITvMDxEZDiXOTpnpw4fr126CKKOzZuW77k5zRkIi2jp4tAoREnIer362DMv/PZ/7Z6dzZJqafuW+//PXyxuWpd5TmpAAKsSqZIqqzbgucYiJIXJxInK2MQLf/o/K2ePr20tNJkgIQEhU1MPnAgnQokqgTSrb25tL10ePvDbf7hw/DzFNv9AaPQBRQzz7KFTw3/3yGpGuzOABA0RmyKxAwRg4yJ2qqgrZoMvxoV4fu7An33GD05wAINcDgM06nPlxgVCF1cSsVqLgm8pb/1nP7zjR344M1GzMwOPfPXA7/6Bv3TFkMLBE1QBIRVanDcqkHV33tq+ap1RvnLk2NVDR5yBhrB4bLxmvdbowGkQTT2Thomp0088Zep1jeM1997FPd2pCNG1Qzh3rVCoqmeRi4889vRv/G48N5uptO7cede//ZflnZuV49gUbByrqlGQKCGfSdewPYbaAJMEk8nJr339/AvPtRds6usLIRVSR7lwgR2ZArEFCygoWPzW1o7iwKUTf/eVdGYeBBEFlDULgGK6duFrT6IyXbLQEBIJWX4JUSqQLbFhVVay7GpCNQ1F1DcUwtyxvROnjlkmIyChHP4JqqExDYF8dlnSWIlh4EiL2taz9Wd+Yv0nPj7rCh0mGv36Vw789m/o8AWyWV2yzDOCEWVPCAiCzEMLK9b33fLWeW4OU9OD33gE6RRxQt4Zia5vhuKV9i8ERiDynkM2dejExMFjBBOtWLXygbuEyaoaFhhRFgkqqagilTqH2rm/+Isjv/FbHVOTM5n3e2655Vd/qXTbrWLKxhXJcFy0NrKiIDAJsXBuMLL0X4KStenE2MDTz6xIs2XEDixwmSggTIGQOfgY4kiDeI+Qqk9D6hxGnnw6PXNWOahKIOKgAqB2dfTSwUORYdIc3IGAZREdu7Yq0PAKEpXImshngy/vZy/UaJKxMAUmzxyIPDd+aX6bya+bYGKrLaXNH33fLT/60ekoJmPPPfb4E//xd/TC1ZIKUxaswsF6NTBEhpQkdr333RV1dDUbd3XfoYWzA+RI8RrK3+swHagoOaMOWKidfexpnZlPvPTedmuhfzkHscwaQgiSkoohZqWQFurh7J9/4cB/+xNXTyZFl99x20O/8LPx+n4lEDEWfVyE4A15RjAkpvGuBOTMdw8wYfrC5Ylz59ujOAbnV6lGn+JVCHdjf4VIsMZkE+PjR0+yseyMElgNiFEZGqmMjETG5N090Ws9o1zCIg3vITUEC8pUq6JOdezEaZmdAzfOnEAUiAW5PJ2glCO+vAj4kUBBwRrfVlz/sffv+bmfrpRbOqOmiWf3Pf/L/0VPD1oTMq4LPDEhEHti4kyy5hvWrrh5lxFkgyOXHnuRDMMtsgpfl+OAzGch4uql4fH9R4uCpmW9K99yD4oxVI2A2YDZEsMIReLqyfk///yh//6nXbVsQWjZWx58y3/4N4UtayqaprpI5FqqTRVCSAkJISWEpTuwQglQzA8M2pm5JusylZwizpQ/lsWmLXLs8VrfwzJzPRk7cx4h5JsQkyFiymZmm5ScsaoaVK4/czVHrKF5h8aAGJSCalDLZKbnsskpguaf7NqvvFMi4GukgMYO7VUT4po1vqO89gPvuOXHPxFKzZ0uyl46sPdXf8ufOl+goPCpBlLAq4h4IOss991zpys2d6m5+sQL9UtDbKH8hpQDgpA41uFnX9LhkYjMyltub9u5OQQPBryQEDFDvWZVW62c+rPPHPnkH3d6X/Wh9+677v6Ff4H+njnyiWEhDqQgIQipGFWrYIVZxOhoEYQnBRtSn2WjEz3GGZVUZAlLC7rUHcwrkmvoJxEYVCbSuXnUEpEgqvkOppwX1qoKCjnPGEs/ZbHFAFXNFxtlCjHWEsppSvNViIcoKYwXm4nLxKRqfOOyl7MvFolPEFUVNSYObNOm0poPv2vHj38sFMsRy/y+/S//6n+pHjxhSUW9MsQRWcNgz9y8e1vTxk1FYrk0NL73CFEQ9m/QhwsQQxqGRs8/8VQpZFJsat9zM7W1C0zemtBUABFk8ez8mT/69KE//JNWzeZ92n7fPbf+ws9Fa/o8GWMKhqw2+A2N56lQFbWCKGjsJQpqgqgEIfEUQkjIpzI9XSAIQkCORNOroFtdrBnzJwtAIJZgFQjeGMMgFpGgUmhrTQ0lktsysWGjIvRKItXSjsnEBEiQknNhZmp8735OEnE+SE1DSj6lNKPMa+ZF1RtkTFnDkQtQOOYCcUEoMpGNCtJWXv2x9+z6+Z+ebWkDoXLgyN5f/W/1g2dsmlSlljoRSATDAdH6lcvvuyOBiZL68JMv6PQMKEfnG+2SxppvfFJSUpPJ6LMHFs6fVZXmbVs6br1JMhiyAmnUWxqiSv3I//jLU3/0qeWqo0lt2Q88fPu//zm3fmUgE0flEheKFJvrepxKyKB505cAEi8+lZCKZkGSEGrWZ/NHT145fswyWbbSKJHzBsc1l7JrPRCiXHLoVTwb19QEY0lgmJhARCZe0c3dXXWgGjI2NvOZM5ZzNhFdY4gRITdOK7CzEorGckie//Rfnvzrv0uHrljJTAxY712WReILlHII2mgTS76UBCwwkhcSMAImk8TU9cG33PKL/6rSuVwE4eTZvb/yu7UTZ2OTAnUGGbDJKGHT+9DdtGKlKib2HZg5dsrkR5q+NuDGRDRXH378WVutzBssu/+2aP2qtOIhDFKPVMmbqfkjv/dnJz7118XgR9N01Xveeev//rPU3ytqWJmEjLAFOeTowOLmzSZjTgipkcQEz5lyZo04hLhSqx8+9+In/3Tq4gAbVLO6ITZ5V++VACGBGMhFk0ScKzXmjWnesBbFAoKywPyH//AfwHAuGj99duz4oVKhwAqGOmKm62yeoIBaMgALWCAWqiomLk5Xqmf3Hxzbf9hfHY8EcWuTbS4GDmnwhtiRoUV84hXKlsbviQCPUGPt2Laxo6vv8pGzOj9fm5oaPHNq+ZrlLb3LEZwGMsakRuJyoXplePzECa3XQ+SW33oz4oKCOMjIY0/MnDrBbZ2r3vLW4srloMBwMy8euvTpz0h9LvT33viTn7DLlyMwGyJkzGLGZw7/tz87/unPdEfxNLD6fe+8/d/9XOhsgxpWk9NZc9Qon+kNkhCREZggIj5AhHxs2QRJLg6PPf78wU/99ZG/+Nz82fNtBeNIHTMUhLxceiXrFmACVAxzGgIZW/eh0tW98xMfKfT3aZILbJV8CNRS3HTfHbNPfVMlEBEL+ZA500DZc2RYG9RkZNCIDamfS+oLSWrELBdxBw6dOHjw5Gd7evfsWXP3Hd037mhavoyaIu89VGxkQJBcvrc443N/u5B5qzaIzgTf/fC9d8XNz/7ab5mJ4fT4ief+46/f++9/sfmmuwIzRTCBuVxc9ba7Lzz6KE2MX31p/5YLQ8UbOyHhNQUvOl8999i3dHam7KIVD9zWtGl1PaiJDUSJDU1NH/zkn579zBd6Izebhc0feO+N/+rH044WVrY50ZGuUZB0sbedaxvUgwSxc+SDTNWmT5+9/MzzV154MbsybKoLLYgFVA/VSaRl5o64DKUAEqgD0eKTzCerqEYceU0DUS1NNt55e/OWtYlkNhAB5pd+6f+EYSW0dXaFK6PnT5yKmB1pRDBMQb0smgWIiiUGTCBayLIamc5tN6x98KFl226QoFJb0CQrVNP6+cuDz+4dfvFwfWjcqJaaS7apiYihCKrCrNSwXwMQAMOkqSdla+LEoHVdf/fKlVcOHNSF6WhudvDMhba1G5r7V3hAvapBXIrmj52bv3TBVxda1qxpv+lGDcLQkW8+OXf6FLd29r/l4WLfMhhNL1499id/aaeucktp+098NFq/uS7koZElmhjZ9/ufPPm3X2i1BmTW/+C7tv3Mx2VZm8IQQDkvyJhF9KHhDQdReDXMzMS1Wn3g8vBXnzr2h3957tOfn927X8ZGrc8KxWKha/na2+9ac99d5ZW9kxPTUguRsQoYIkcQzeEKNYRcF55pSMiM1mrlFSt3/+xP0cZViQ8uMDGZX/mVXzFkLBmxtmX1msHB4ctnB5ost0QOkCwnNpEC5FUtccEWppN0PCqv+9BHtvyrf9Hy8IPtD9637O47mzdtCU3tC3M1VOulNMHU+OSJo+effmLuyOEwvWCi1qhQ4tgJkSiJAMKkrEFZlVmtdZaJmTNkLet621cuHzx7wU/P+snJgSMHeteuaurpDkwVpqjURHO1kQMHdX7cK/Xu2mPKTcQ0/OhT9TMDtqWj96GHSsu7EWqXvvjlymOPGc1Kt96y5oc/UC80Kxk2iGZHD//mr1/83BdaXGHG2o0ffP+2n/4YetuCyVF3H5AwGQomQJXIi6g0kAyu+WxsZvSZp05/6k+P//lfjH7zcbp0oZxlAlMtt5R337L2h963/p/92LKPvq90640999zbvnrj2VMDs+OTZWsKhjLJApQITLCsBt4YysgMVOvzK1bu+dmf7rj/LnIuMpadIUvml3/5l5dOF9fZ1r9xk6/Uhi5cXKjOM1PBRhHbGBSbSGDmMx1O/XzPsm0f//C2T3xQl7WrM2StaW5q3tDXd9uuFXfc7Jb3zKrOV6tSqxeS1F8eGnvpwOTzB+onzupcJS5EcWuBLIsR1WDBUFU2OSORlQ2bVEPTmr7e/rUjB041VxMzM3tu35HlK9Y0rVqRGi8U2puaL+8/uDA1OTU1171hS+u2jQh++JtPVU6fsa3ty976QHn18uzq2N4//vOFwUEplDZ//MPNt+8S0YJnNzL58n/+L6NfeqTblabE7PzYR7b/7I9m3W0ezogzsKQW+cWGOYOkkpKFJaHpmZnDxwc/98jZP/ury1/88syRo+n0rAjqhVK2sq/nwXtv+mcf2/7R93fdeVO8fJlaS5GRgm1a3duzcd2V4eGRy8PIvCVqj5oLxhEQROqicwGX6qmuWXvvz/3UqrfcI3HEJtclAXSdeFGYF0JW6upctW1ba3f3TKU2Njs/n/j5ul9IZSaV6WCqrR3td9+++2d+bN373pq0RCmUyVAqkCBGteAKK7q6du9Ye/+dPTu3m3JzrZKmtayQ1YuzI9NnT1564dmxA4f91fEok0KhYEsROaQiAcYE1lRUCY6CsUG13NHZvWb96LlLbmpGZucHj5xsW9nbsX5FmiwUWprSibnL+4+ZSsKFcv9Dd6n60W88kQ0MZKXyine8pdzfO/rNZ859/stxoMKGTds/8SNZa2Qs84Whl3/td64+9tSyqDznmjZ+5Ie3/dgP++72CiyRM8FwYAWLscIkmsVMhTTzA4NXH/3Wmc987tRffWb4W0/KyKCmWU0d2ju7b79144fes/0TH1r7vofLW9ejFAUFPMNTMKhqmhk0r1y2bs/uqKlldmphYSGbqdSm69lE4qeCjhMvdHSufutb7v4XP919962+VICx15vFvbJbSi4RiZZ1rPnh9664/86pk2cnzg3MjU1Q5qO42N6/qmPLhtat62hZax1Z1XvLlphhAwmgDkwB8JSaztauB+/ouvXm+uDI6L7Dwy89N3fywNzoOM0v8JGFs8dPXvj8V9tv2LHsntt6bt0Zr1yBqCCqql7IeDKJhqKxWtDWB27Z3Vra9+u/Y84OyOjVR3/t1+9JfnL12+4R8itu3tW7bB0PXx55ed/40VPdOzcaFQNRCkDA2OyVx58pz8+TKfTdebdbsYIo4NKVl37zdyrPvtwWFcdc89aP/8jaD/2AdLRlQT2bTEWJigEQIqYoeJ2cmjk5cPXZl4afezG9cpnTWpCUHKflQnHdls277+jfc1PrtvXc2RycqauaEBxFJEGUTMSZirGRqMynteKy7u0f+9C6e+6ZOnF+8tLl6txcGrLmrrbO1X3tG9c1remnlubM2HpAZNTQa3ENjFKZjBiTMYfIuo1rlm9cs1wUmUcIMBY2AhpMdOcKRc3tWKCOG2RChYoaRAgSVLgUx9tWr9zSv/K9d1UGz116dv/g4y+m568UK3UaHRl9bOzS8y8W+nt799y8+q7b27dttD0dYkMKL8GnQjYu+Cwr37Jl1//+L1/81d9PTp+IJ8df/M3/WiTqefv9zevXrtpzx6UvXQljVwe+9mT3ulWRs5mk1pAVWjh2bvbokZjIrli56i33oVTU8xee//XfmX726SbiOVve9OMfX/OR92uT9UCmUPhU0wI7E0eYmK8PDl/ev/fyC89Pnjpvp2aaiWJCLSpEXSuW7dq28o6be26/PepfDVKIBBERtmQIFAIUrI5gYZWtMKCeARFpjgo3bO67aWufLqI5hsEM0iwLbCxAsYd9Jb3l2uaWd86YwYZhjIACsyfy1mbOemeVSZlgDRtLYMvWksm79IE4ZVKCIbJggiFlEAnIM2uhFC1b3nvzzevuuadl/aZ6XK4mWVapuVqlMDk9e+zYxeefGztyOJmZLMWmqRCVIseGgyiIPaTQ09G3bs3UlaHq0OUu5dMvH7QtLV033ZCROf/C8+WsWp2rr9q4efLQ/pFzx225ZfnaLVN7D08cfLEiofuh+9d+6J21wasv/affnX322cjqfLm45yd/cv1HPpiVCtZxMMSkZUazT83Y+NizLwx85rMX/vQvrn7zG9ngRapVxZiktS3asX3Vu9658xMfW/eed7fcsJPb2wLlIgRDavNLkoC8oSzKjZVAAqNgJQNjrBVjveWMqc5IDYJhIVaQgGAsgawSg/iV/KPrnHgVGgBeFCgJEJQX3b3yO+6S9O46Ar+C4EEBsApzvTqCGoaRqpDcTIDIQFGpVc4OXH1+39gL++ZOn01mRoEaQK5UKi7rbd+6rfe+uztuu9n19SJQCD5oGlmun73wzC//Rv3A4di54Sh64F//6/677jz4m789+dijodjZ1Ntbmb6M6ozYcnPXGp1fyOaG6uXm2/+PXyr2L3/hN/97Zf++yPjptsLtP/fja9/73rorh5SK1nJsMD01f+zU2HN7pw4cnL10sT47qyELgBTKhZV93XtuXnP/3e07ttqODu/iLACkdlGpulh0L4JxhCVef6OPrwDBG3hepGsTFEpCJKpEtKipzDFlvMHwQCGsIceXRCkVznUTiz8iGPhFZp8Ji6he3geRRV+1RWi98eEZ4kFByZAAICFWUqE0lbGZ2VPnruzbN/Lyi8mly8VK1YDmVJPm5tYNG1bsuaXvjltaNm4wrU1aFKivHzz9/K/+VnbynCPOSk03ffiDE6ePDj/9RLnQWU1TtpmxArg0ZQfEnCZtXTve9yODLz03dexoFpLZjtZbfv4nN7znBzJlF5WQZPWrY1NHTlx86lvThw678YmizxQ8yZz2tC/bs2vN7t3Ld2wrrF2JpmLKnJBVGFWQogjkTWTlRQZxTsXOVVn56ZGDygYZw5MSYAU25GJPwEMDFCBHaiENwFtf5bhz/fAo4APBgxVsFByUhJRIGEQwJEo5Ywsm78GChOAZDDUSQKTMi7Iy5MIfBURUvVrDRJAgAcGrh0oUOUNWF5Lkwsj0oaNXXnj+ypEDyfhoUcUp4GLT09W8cefqux7q2LW+uLYbRrPLV1/4j7/t9x9pdWaaac7PdTF3u66aeKBikCVqvRRKxIGqsxSFYFu0VvfV+e6e3f/+F3rf9rBWKFydmBo4O3Rg78iBQ7PnzxdrNSc+ANTcWlq3oevWPX1339a+bRM3N+dzzIsEJhiz+L3IEjEacglZAiRVSaShiwRRAKBkkFDDFM4qbIA3UMAEYmEoiYNwPo5icibm9czq64ZHRDNhagwPYEAQ9YRExRAVwflfJhAJVFQJgRGYAGHNrltolMMEuY5AoEJKCp95ZjbGpMEHBOucqlBGkVpyQLUycezY+Av7Zl7aN336JC3MNzNXpLAQdbau6uvavWXFA7u6blgfhqdP/Nr/nRw9BOcWQr3JmIJECbTAmYOf95Ki0GbjapgLFBnEQebQ0rzx53+68x0PL5yZuPrs8aF9L4+fOUrTowXJAoAoKi/r7dx107L77uzac2O0rBtkRRb1skz53qAiUJ+LgXMdyuI0zCHynAmWo+XUgOm0Qf3zjYVERlFnH0giMREcEwVCAAA1ENZAZF97eAI0FW9yomIDLFelvA2fWXIONm/p0rUeklz3G1k8klQCkRoIqYAESspGQSyiIMpF9cq5K2RO1bM5xycCUSb+yvj4S/uHn39++sSR6ctDJXVlloVQz3pam7as2bLn1nBi8Pw3v9ldaIJKoumil92SN3Lez5SIjDN2tFbF2tXr3/v22fOXK/tOzFwdyUJKwXuj1NbSvnFL1y239d91V9PmDWiOPBBC4IZDHVGDzMIaVHNfDbCEACVaRPKVBaTMDSpjQLCv6NJx3sbM0bsQNNUELAbWIN/UGkJPpcDMFq8wmKfrslqCIGMYESVh5lzfK0q5f02qqCZaX6gsVKrzaVoL4lVFxIsEYo5cFLu4VGgqFcuFqORQoFwbrxyCaoC1EbGha6ErOdNK1JOmDAM1UM4JFaBqhoVK9fLgxRf2Tu09nJ4952cnEiQLkrXFxTKoiSPAZCG1/LpyJkckElIyM6opEKUhBuZCqBYKratWrbhlV9+emzq2beOVK5Q5I1JmUmUoa8hpZo1KCfBeiMhYCiGo5PCOMnMuuhH4ROqV2kKlulBPa94nmldZOemPTDEuFYvlQqFQjptjNDFsgIpXDcog56yoBHjDzGTpNVePiocmxLmfHgskQNlQNVTGxkfOXD56eeZUpTI/MztbrcxnWSriQVAI5ZIWGGtcIS4WC6XW5vZl3b2dHd29PX0drR3lqN2hJW/XkloGGzINlxtAvWiqMERG4YwHJN8qRJmIVXRiYmrvkYmX9l85tH/2ymApS9uYi64wliURc8vr+zjkYm/L1pFJMl8Vztram3du77371r5bbyz0r9ZiMUBD7isOMiAj+fVNQUyLroVKCEBQDwrEgaAJ5mo6V6ksXB0fvjoyPDE9Nl+ZW6gsVCoLqU+C1kEND9J8RcdRsVAsFoulzo6u5U1r1vZuWtW3uiluZRh4IrKshtg01I78mqtHRDNPzDDqkSj7mdrEkVP7zpw/PjpxdbI6WqEZ66y1eVW/6NTBSiAVkkAaVERFNPctcMY1lVubm5tXdK/ZuPqGZd29PS29MUoEa9QyLMNQnm3VYI0IGF617oMSOReplwTBWZRVaaZSu3hx8qWDJz7z2Xh8vGzduK8VjG15fbKBsARICBqTzdT03P9gzwd+oHn7JmprCYYyclngLHhiWDYWsCButP+JG6lBUNJAIhQUXpHOphNXRi8NDB2/PHZhfGJsembah4yMgpQNMTNbIhMahQEIoOBzNR/SNJMgph412dbl3b2b1m7buuHGVb0bityqmSNxpCBH1ytIXllYB3gEtmHGX913/Ll9R565OnGx7ueionHFyHPD7ZUWdWiaKxxzuaQaa0xu95m78jJYBD7NgidH5dbm1lUr1m3fdOP6FVs6i8ssCgCzGlGjeS8X+bdSAUBGBCrwhACNSJnFiDcjs4//m19KX963pqVlwSce4l5fRWI5ZCGxthACVUxx1y/9u6b3PbigGotjctqYVWIdM4FERXKjEmSKCOpUBAIOgJ/XmcHR8yfOHTl78dT45Eg9LKj1xhhjmQ0kl9ExKSSIEAmxXuN1KS3G1ZGKchAOVK96zUxbuefGLbfdduP9a7u2arAM+yozvmugTtCQIWWrFyZPf/1bnz9ydq/amitTIXaqPpWEgluURBLREomJGdrg7GoQWcrNgaiAyRWsVQGqc2l139FLx0/v72lfceOW3Xt23NFW7jYUBYlUShZqGnc9NawCMUzEHAWLwGBkCCCTpn5uodIELkCZqSoIr0+kiomajPOgzNgpL/OValktRIw3FqwMYoVlFSWikAUCZ0FEIcbUpQ4TgDCbjJ0aOHLo1MsDl8/M1aajouWIIsNkIlVRDYsUEQRRNLbsBst1UTy+eH/NjcoMAgfTwqy24me/dfibR88cectd77jthruMxBFaDOLXGh4kYufODp38qy/+2ej8laau2FMs5L2IwrIyq3mF8wXpNdofKV2jK9IifTH/WCrwoMwVo9ZC7Ov1kamB0Wcuv7DvW9u33HTLzbevat9qUYAICyEg33qJVKmBNXCjmGAiMnExM64Omg/eIwPbBlHstZg6tSBko4rPFDYRSH7mEQvll0kRzgAm4YbwRIIzKipGUm+qo/WrJ88eO3D4haGxC4Fq5KTUZpRDfvPUYJbUQEtNu4bFgxoSAr7d8yfXpbAaJUZQqPFc4oUw9sVvfmp2fuitd701QoTXGB4Fkx4/u//vvvq3Y7PDLcvKmSSw7EwRSiEVDeEah/rVjExQTgWiV5kNNJADY5iYfZYGeOOsswZBqmH6mQOPHhnYd+PGW+7Ydv/qnnWkljiCsArBNJT14FzhkDMYYZuair3LZ48eqwKqaiBvkHNUV6oHYUQLmZ8rFcq9ywWaAWJzUqAKSUOjAjLWqCaCunVhLpl5+eTefadeHL56OckWbAzrSDmfDNRwj7l2u2io+XMlBpYUYfSqomVRu6pWA4EFBoY1SLAR1yvpV7/2SKj7d97zYyXXqpSDaUvDQ4BwdS5ta+outZVH54eT4D1SZnLGOHJsWU1oxBMuTZSla8a1RXM9yblBgAxemDiOY5GQZZ6NEisXqFR005WxJ/Y+cubMkTt33Xfrzns7Cit9JgYRCXIiSkOmoGCCKBC7NTu27X/yKVUU2XiRV2oxX3n2kBHRonWTdV9e1d+8aiW8WMOqKtRgNpHmuCWCBuM0Re300OFnX37i6IWjEoVSU9GpkZCJIRH1WTB55/B6AiEtUsxoiTck1yzOXulks6gr0yA+rWdJ6lkcjFvetabY2VKfLyws+FLHNZ8Iq6JKAhJP9Tt2333L7ltn/cx0ZeLK+MXR8aHZmamp8bGZqenawkKIPBGMs9YZECmUDBQqElSJkec+iGpY5GU3HALYMEF9JiBiYyVHW0VVUSizaTZjC4Ofe/rTxy+efOCOd25euTNGGcEYsbToSwMB5x5cjN4dW21rWzI/1RFDmCoiOfZowEwNn+WcEF0gjmFSRU3Qv2O76+xQjzI4KFkDBGKKJC/wKMD6K/Pnntn/jX3Hn1vwM3GLiR0nfkGC5vYEYKNBfRDDBCZRoUXRyJJFcL4JC6kiF7/mrAoCSEPw3nvvnY8ixKVyS3tvZ0dnT0/nilXLN7bEXa1xd0vUIYoMdQhbsgSikAZv6xP1y8++/E3VrKuzp6N9WXdHb3Ox1cIaUDWdHRsZGZq4dGHs9NXR4amZiXqoCWUmJnYgB5CIJ5KCtRwkFfHcQH+YYKBGOYCy/MJHjYtezoQJgIA5C7DGVefTUtR2361vvW3HfV3xSk6LkcQKmAKJz/3IWVl1aubZ3/jd4c999qa2sg1ZNWgFlIoWiJrYAFRXr1BHXGJjTWFgvjras+xdv/lrLbu3I/dSAMDwNQ2pIhYfV1OeOzm0/9FnvjQ4cr7YZEFCLEpCaoIskmo091sKBBEyUIOG9ZiwNiwQDBmGkYhS9aQkmZCQpBJSiThqa2ldvqx3deeOvs4Ny1b0tLW0MtjD10JlamZqYmpyfn5+aGxk55bdO/pvjkLRoMEg4StXB5967vG6zJWK5ULUVCy0NBdbuzq6+1f29fb0dPa2rVx19614uOIXRieGLw2fv3Tl/NWxwUoyW1+oCMRGISrXVY0KQwsqVgGQV3ilBAA1RKzcoG82NkMDsIi6yPgsxEWnkj765Fcvnb/89nves753e8jAJgq5qVBjMhKKxd0feu/ksaPnzp/d0NRkNTNBmMiwyTNPLRmCqoQKeD6kV6y558c+1rp1kwhRbvtCICg7KItESV1nn9779Wf2P7aQThXLEZGIKiRPUL2OhNmIeDQgtiIkXpVVjcApExkEBE8ZKLUhDhX2qUSmUC61d/UuX9G9at2qjSt6V7WXOmJQFXNTM1OXBgZGx69eHro8Oz9drVcWqgtJllQW6obtjWt2kxIpWZAIfK1WgQ1NHQWC1P3sQm1meCacHoI7aa3ltrbmFZ2r+7u29q/sX97Tu3r56nDzfbO1+ZHR0fMXBi4PXpmYv1idH9KcbmiJLRlLAlJlIiN56EiDKSaL0FiOl7KKVyFrrc/ExsbCnLlwbG565gcefM9NG3d7RCRxhEhzH+WgZGx507r7f+H/9dhv/PbxswOrSiVjrJEQVDOQiihgTSQmGknTqzHf/PEfXv+eh716o7FqDhirlaCmri6Zqo595Ym/O3DyJVuSclOhllaYoRAJFEUFhRA1gl60kSJgVElDRuKJKJ8WId++hOs+iKAcmnsKfX0b+jas29i3YlV3y7IiSgnS8amJA+cPDU2fujx+ZmxsPEnqqU8UaiNrneEyFQxzHE/OjNR0vtkUNTBlvi6m/vTBr3/hiT/X9moIYoyzbDUAIMPks8wHzxlMypajttau/pXrVvVvXLFsbVf7iiK3JuqvTgyduXj08vD5q6MDC9Vxj5qNyThDxhBxCCQhNxL2DTp4fqSoFWXn2Puq98EYGzKJXcEiqs4kBSo+cO9D997ytjK6XCiwKQZAAgga0prz6ezR0/s/+WfzBw9pUmu2pslaC4nYRLY4VF0YSxK7bs3Oj7xvy7vfoS0tPhgiRzFlrITMiVdeODt19Itf+cL5y2dLbbFar+SDhizLSqUCU5wmAezB10EAalQN1DB5w0JEqioeSc3Du2LU1tm+Ys2qjev6N65a3t9abA7IpubHRsaHr45cvjQ4MDY+Uq1VUlsNNjPGxoWogeNCgwSQWmtrU9nGFTf81If+TcF3RVS0+dnljCNi5kbARNCgCmJmIlsgzQjsXUkJyWR9cOT4pX1HnynFLT1dK1b1rV23dt3K5VsevuUHazo3PjU4OHzqwuDJwasDc5UZH5IojqJCUY31IYEISMiogkRCXsxkWWaMKcQ2y7yNSClLfVbuKNTm5x997ovVeuUH7/pIZKBqsmCgbAiuWNLMtN9xy/19/UOPPnHm8Sdnjx+j4JuNSURGFuZrPcs23Xf3hnc/1LZ1rXcRrCM2ZMhDWTNojZn2XXz+i09+enR8rKmrHCiFEQneGLa2EHwQzQi52lxFw5J7I8PmkQOJr9fnaxSoKW5e1bV64+rt29bdtKJjTXOhfQFjI7MXjp4ZuDB4fnjkyvTcZJA6W0QFa9uZCDARCCKZSG5Ny7kJtog3BtZyFrKSye3Bg/dcPTN0+I8/+3uheU4gwXt+pcdfoyQhQwRSJWjIPMQboqReL8Sup3XVqs4b1q3bvLJvTXO5ncFj81fPDBw7N3BseOTSfG3BQ+KCsY6UgnXkQ5Cl9pWADRNBr7UniHInfkE6i7u2P/SDD72/xJ1pGrEpFGKXm7YEVVbhhQUZHnv5d/9g+Ikn+4vF6Xot61151y/8fOudu9FWDAgsFnBCyEihGWcLNs72nd3/19/4k5l0qKmpTCbPTm2UWAQWUSajIqKpMZrXoqTG2SLEVSqJBI2iQmfbsvVrNm/dcMOa5WtKKHqtj40MnTt7+uzowcvTA/MLFeuM5m4SzuaGWkEUKosRPg1HZSYOQZgZoGTW37P7be+//8ei0GGpYFVIwd0dy7vblw9MTJSbS0yGNORTZrE4JiEOMNech50zZI2hcjGq12qXRi4MX76y7+iT7W09K/vWr1uzZc3qTXfe+JY7b3zr8MTwhcsnTl/ce+HChcp8EhWdWibDznEmGUPYcm7ZcL0PXW4HRAbFtvhb+79pY37fQx+OTYuKV7gGbMFUF0UpKm5cue1HPzh78lRtdNTbwqb3vrv13luyYuQVji1goBCG18xpPYrDsfP7/+qrf75A083tZR9C7lTakAHn3hUEkYwAQyxeIltkMkmSLczWmUJnR+fWVXu2r9+zavWqmKPZdHrwysDFobNnzx2/Oj6YZXWxCouoqck6k3qvEL9kdmZgAi+S4YlAlk2WesNOM3XOKaI1vRsLaMoyMUYtKSNwZ7Hnph17zn3zhHhSytsD1gd/belAmMKSEbuCg1CWgWCNLdrmQJJISMdrg6OnLh06/Vx7a2//ii2b1+9as3L73btW3rpr2/DVkZNnz506c2ZialyQmSi4gjWu4YfdwH8WL2S6+AfBSrk3eurQY4HkXfd9oGy6lBzB5F77bGwqXNV605a1XRvWjw9elu6Wpq3rUCyQcQbCoo1KkWA0WCOHz+39zNc+tcBTpfaCZonmcfLG5H6p16601pBAhQ1bX0NIUYxb1q9duWP7jk0bNnS59aLx0Mj5E+cPnr50ZGTyUqJVtUARptlajY3GYE29BxgcckO5RY3CItqvuYcuIhcn9ZTZVheS1T1bt6y+ieAsDAFWA4xxBOzYcuOLZ1ddvXo5KhhVpXwUGk8qF3GHJZgvCAAuxLEE+CyoES4ywI4ZQr7ux+euDI8PHTz2QmfH8vX9a2/aeMOqVVu29N45cevsuYHzZy8eH7h8bGp20LjUlR1bq7kpSMNW8pojWCYZWbEd8TdefrRQbnvnbT8Ua06sYCJxgdMUWrBUcLapnCmCtaZUUGWpibNEhoQoEFRDke3poSOfefzz03ae45BlgQI1dho1eRofNVBeyp2Hk2ogcatWbNqx6abN6zf3tfcywvD44DcGvnT+8vmhq5cWajPkxBU4KsQw8BpAbFQ0q3kfADWGCaqNyxPnSkQlaXifKLz3JnJkTAghSdObb7y1vbBcPFlygFpVZTJBfHdpxe7tdzwyOMIFA6N1X2MDZpO7Cxi63h1SLalCfMiIyEQQRgYVr56cZSdMXDQtLayhNlk9Pn7ozPHDx3q6+9av375t6w03bLvhpm07h6cunBk4dPbciSvjl+r1Ght2kSPCUoKhEuf2rkEya01LV+HRZx5pK/Q8eOMPiObTzmvgWK2KgRrvM6+ZNUZhiA2HoKxivFcLNhY0NDHwd4/+7Xh9lFuUmcQrkxFqvJvhBvoqQWv1BJnraFl+ww2btm68aV3/liKX55LZvSdePnfm+KVL58eSi1JOoqhQbC6Q2sx775WCigqzCieCjJmMcQQWAQlTzsMBe0nZGWJI8ERETFmWWBMnSdi84aZbdtyjwVADSlRrCgSCRcxqHtj6g9XR7BvPfr6p17rYCjKCcOCQClkjDd/zXKBDlNv85H8k7DRHMUUlYQJZ8hqU2BabEHMtzJybnTz9woHnj7X3dq+8ccvubRt3Pbzngw/cjFMXjhw6+9y5gdML8zNqMxuTjY0Y8iELxOphUUQ9LVh1MX/zyc+1lntu3HBn3aeWNYo4MiRqwM4YZUoUmeEYClNgsdlCfV4RR4ViXWb+7um/vDB+qNhKPhBRU+AAIwwJaUYG4iWrexLLYvt719yw/q4dG27pam/3SAaGTh8+ve/ClbOjk1eUvI2oULLGxKqiIah6Xjw3LbEqRBjsGlWU5HtQbkUABRxH3qsiEU2tgTXOajw3kS1r3/iO2z/ewn1OXYOKBWNxnSA8NsW33vuOmcrYS8eebl1eUGvSNFUVV4gy37A3Zs3dDbTBkYBer7teOt6vNWHzzd9JIWIuRUkyd+7isUsXz33r2ac2r99xw7Y9m9dv37b+5sHJ00dOv3T6/KGRiUGpJXEpdswAsy14EWKfar3QZBemLz327Kd6l3d0lNaZUOA8puCa1iyHZQ3yAMLgnG+l2Hsde+Klvz1+4amoTb0BqBgEBiSVinPWqtZmK5JRe/OyNSu37dhy69YNu4qwU5WhF488efjYvssjFzOtUhTiEpExeVaqBHkFD/EVuLRBPllz8Hip5sgfoIEGbwwxnAZxtpAsoL3U/c63vGfDik2vKpHsK02fueRaPvADHyXD+44+HzfbYilKfRIQxIiyIUBkkcdBS27UjXCs17dVE4jAEBkTlTmKOa0n0/Xh54+MvHz8mdV9G27cumf9+o3vuPN99936ttPnThw4/NLglTNBa6BES96VnBojautB4pZocOzko0/9zQ+97SesdjPHcs3TlaHcALBz5EaoYJkjOXBh32PPfNE0E8eWSRVCqDufwadSkyx1y9o33LDntp3b96xo7/fwg1cuHTv7wqnz+2dmZ5SDcVQqGi8hwKvmWKd5I5tmzT9JQ8Od18+6eKp6XysUozQJrLboSrWZrDnq+sC7fnTn6j2ibF8Zy2ivG3YWD5Atmo4Pvf0TbeWub7302EJ1odDmRFO21PAD5kXmneZGTrTYm3gD5yEwM4AkS4hhjeEiVDMWTkJ6avDFgUt7ezrXbFq/58atd+/Z8vAtW95y/vLRvfseG7x8eq46kviqjcombg4IYhG1mgMnn1u/esM9O98RVECFJavqXJqLhqmbwmTM9bH5c19/+gtUYi7EQfOqJ1W/4Cuh4Mvdnat3brt7x7Y7u5v6pmsT+469cGbgwPmBY7PJpClxub3ExiZJ3WuWd/GNtaIiQej1GxmLuN5il4EW7Xfyec2apgnEhszMTKVrlm/6wbd+cN2KLRRsTDHhdZrZgBpyIZBFZKnw7vs+tKxzxde+9fmp2auFVkekQXxeexpDuc9FA2IE6Ru7sSuFBrneikoaPBsTxJM1tmARW5uF0blLQy8O7z+yd8PqHTdt27N+zcaN/T83MTO278gTR8++MDQ6g5ijFpdRNS4iqdWeeOnrq1dv62vZDBHNaXRgItbG/pYjySFD/emDj18YO9u8rFzPAnwUEmiSNRWb1q/aefOOB9et3VCwpZHpkUee+/SpMwfHpwYzmbWxljtcMKhLFQJjjfeiqswm+NzP6Y1mZKOGJlEEJVEIM+ti68qwkUwlpVCzt+y8++G73t3TutJKbBHBa8PW/DU3N2ucY0fKCg1wd+y8f9mynq8+/bnTl45yQV3RglQRGBokM8YQlJgbydhv5CvNgGk4i1PIeR+5ZN+rkEYgx8UQF9N6GDx4duDImW+uXXnj7p0Pbd5wyzvu/d/uuvmtLx89eOD0y6MLF7ypiIRyR/PQ6Pgzh/e/5+51kTKpyasgEW3kXQGC4EHHhwZeOnbQlo1yFryXOpVN9/Ytd95+4+5V/TdnKF0eOX7w2OdPDbw0M3+VnUTNhZIrhcCBvFCWW+kIiIwlXTrbiK4hh685PiFnluY0TZFARBKE2URxHCpSn02625fff987dm29q9UuNxo5clBSo69K/bOvmQ9PIBYjcOt7tn7i/T/98rHnnn7p8fHJ4ahgC6ViCGlsKUgQCWzwugEk39751qUEqsU2sCrUeRQJjRjLQmyhcu7qwXOXTy9ftm7Xxj27N9384B0P7tpz08EzLx498/KVqxfnqxIVyi8ffmbnxh3blt2owcOQ93ksmprI5YhQRSZfOPL4xOxUXI4XJn1by/KtW2/as+PONZ1rQ8iOnNl74MzRi5eOVtMRW0ziVlbmAARVUQYZQPI4QFXKw2x0KRqAFK9LQkFuybVoT0WGIwjHJkrrvjKTlk3rXTfcc++dD/W1bCTErM4iakSFfNtztK+VgEm577dDnAYpov3eG9++sX/H3kPPHTy6b2FymhzbEoPgIicaQggKfoNkACXFIuJAi90EbpQoqghEdUBVogAnCMyIW+B9ZWh23/BzR17Y//Vt23buuuHW27fft2f7vacHTr+497mro5emZgeff/4bm35wi2XHhlTEsMkkiE8gni2dOv/ysePfik3cXe7bsnHHLbtv6Sl3VmX6pVOPHzly6NSlY8GlpVIpiohMiXKJb04koGxxMi26Ji/dzuk1EmG+7aw1nH89IiKDwLWFLAuITeu6lWvv3v3QtnU3xFwUYQrGMNHScYlXByzYV01vIBcxEFSzNDPsnIvFh3UdbSsf3HDLznsPHn/p4MkXJuau2iIHERtZIv5OroQCCoth7bkBY6NfqiAmr5RCjebNanIiIQsJGbiSkRiT2fhzxx47cvrFTf037NnxwO51d+5ad8epc/v3H3vqzLGTJ7ccv2nbbSGkxXIx/7kSMhDm6zP7975UdsX77njXnp33thRbpuaGXjz06P4Tzw+ND3uCafZxpECq6sQzyBKgmhF5znUiStedJY28h0UzvzccHjaE/N6uPpNQk4iaN67dceuNd6/t39Tmejhz1nIIXoOYmIGgi+eVeWVeiH2tTaix87goyrVYFpGG4Nis6d6y6v61t+2+Y9/xF06dP3xl9FK9lsYFRxzICnMDgl1cISSN3CqlBoZBAlHRRu465SwZBfJLlaoyWBqOkWpJDDuHiE1Ik3Tu8KlvnT97em3vjXt23bVz403bN2w9seW4T3zqM5Nl3qfEMA2bGZ6Zntm0dvv73vv+zuKK8dmJx/c9cfjEc6OzF7hMpikWNcxMyFTJZykTrIFKw5zJ5OSQHIZrqA4k51wvsQryvGIQVFRF8+5DIy/Gk2SU1DKCbS13rd+2dffO29f0bmqyHVBDqSMxkpG1Bix4w4g1+8oC2FwfHvLKYpFJlWAJ1N+8dcXt6++84cGBK6dOnTt66cr5yfnhOtfiOOKGP56KChkOuWMwOfIm72M2bgWaMyRUoQS1aq6ZX4kwkSVICERsjObsRoqsYZmrjx8Zevrc2OHVxzffcvNd27fcWER75o2zUQjeMCkQfEBAb3dfR2/z0MzA1w9/4dCJ/ROzV21BTUdsLGXeO1YmVm8I5HLujk9zy2UogtfFTK/cYjc/JRueq8YYWHj1IQRrrOGGEEO8ZmmWZZ6TqDXqWt+3ZvP6nRvXbl/RvbaAJm0wvxix/ba0kkZZZf5/DsZcAsAZRlRFbVtp+Z5N3Vs37piYHh2eGDx3+eSlwUvjk6NeM7DCaFzi2MQiedSyKGledWiDFreIDwsHdUsbOueOWAxSDSFoEGOFWAleSaKIXcFm1YVDh188evTw5r4b33bXBzZs2L6Y4xgk/7GWK3O1pw8++tTLj0zPTTS1F20s4GAsqQbDwqzeGxG21ub8ctFXUvTEgwLRkvUzGs1jUBChjCxZUtaMsrrP6sJqmVxzubt3Ze/qnnUb+rf0r1hbtK2EmBF7ZZMrnl5tqPn3lFsKWqTbLUafMJzXNKbOvo62FR0bd266q1KZH50cvjB4Zmjk0lxlcnZhYmF2TtSTBdySJAnGGutsw7VWJRcGXDumJITgjTV5UpaKhjokqAZPADzqacVxedWKVd3dfcua18ZxURVgts4RyBhDuVE4cUtzy46tO2YXpsenRyZnx4SzvMXOhr0qSI1Fbl8XxDOztfY6iMrkaAwRE9hnIYgYZlHNvFCq8CCYOCq1Frqau9tXLFu9un/D8q6+7q7lZS7luLEEy3BkLDUMsvPd4h8gVnaxmuTFVgwBxnFREUMlSGC0NJeXLy+v3bnqlhQLC7XJiZnRsfGh8YmR8dmx6crkQqVSq1VBGup+IUsa7o0EcEb8qspCU4VqnhFcKlB7wblyKW5tauls6+rqWNbTsXLl8rWt5U6HokU58yRJIt7npnPMDEVLc+vtu++/DXfVw8LlkYtXxy9PzoxNzkzMLcwuVBZqtVqi88LVfNEw51V1ep36xEBZVVVElax1RE5AhUKxs7W5LW7tLHd0dvYs61rR09nXVO4subYimgPIi6j3udpTlQwblusL5jcT7PdmhkeVcl6taSh+JYdQGGBSAyUiqDCzcRyViy09xVU7end7ZAt+vpos+BAqtYW5+ZnZuen5yoIPaZommU8TX/eSXEMujI3i2FlbKBQKxbjkuttKq1qbW8rFUhzFpagUo8RwgA0qiiAq1sQcx0F84hMTPFsLA1W1aCYYx53b+lbu6JOArK71ar1SqVUqlepMbWi+PlKp1mrVqoomaZKm6dKRa2GdiZx1URRbG5WKTc3NLeWm5qZSs3NxS9TUGrc6xIAlGIVRNRKYQDEZsMtBYWaSpbOfFqM5/0E2t8UGcsM1A4vJhA1dJC1eC3LhvVG4vCSzoHbT3l4WgNCiWJbbmwaB5GkWAUauOxQJxOBFry5iWAO3FGQIXWIkkIUBs+eQy4utMdZYZm78HWILQ0KqBmQBMBUcN7cUu1EkdJAiCNJFzQUEQa4rlw08QxZd6BjI9RN5YhdxLnq+5k5MlENyuM7Digmahxk2xPKLxp/89796GnHtDTuzJX23Xusl6PVJoXw9DKchx+qWPGeVcrUDCQD7SrmevjprWITSPJkASpR3T5Yut3leuX5bjOxi8gWuJenli7/Bh25gASjQdUrH64tV0dBQtl+LBsylIYtcabruJpJ/KSwuDloiz+CaXSG9mTDmN796rk+sXsRjrldxNU6mpXxqWrpB5fKja5gFXZcvnS9KTV9D9rDE2SFtuNU3ot0U0IbDb57ZeP1TeAVwHNgAeJVtdAM5zvMoXuW8+grUGe7VCVrXfT561YPJHXZzQ4jFHIlrh40u6Uz4TSUvvqnhecUcv74EafQWsEgQgoAaOqTGxFr8H68UdDdWAF9/U9ZXZROS5G+do+O0NOy6+CBeF9XPXS60oX3P462wZEZg6FU22K/ye1gyWX99BHFpHBelGnot3ex6M3q6Dgmif5Dh+TY9xOvleS+Nmi7mJOZznK/BI9fFgmrjwkuvdkB9lURo6f11KZucgfBtjUq6tqs0YhNNY4cj0jesaV+dfJgDUd9hw78W+3x9Xh3htVbYmyvZ3nxpQN++0l9vHF//j6mRbUDfzT/K/48BfdvfvGYZ0EhbJ1ZiwfUsH+HGqf4aU8y8VlrJtyXOme9iyr6Z3efNvxjff/0jfn1/eL4/PN9/fX94vj883399f3i+//r+8Hx/eL7/+nt42e/BeyzitDltipG38akR0IjcIQwkosx5SzHkqpscyTbf4WcvYZyLl9LFTKc3vgsKoBDTgKdICEGVGaR5k1RVgiiRsboU2LeEitA/oeEJQAaJVeAVigDylrxKRLBZkJCRM7BxPYgjcshIa1BVE9epaEBvODyLoTgNdiAvgZMSlPmNQGIPCZA4z9+GCYyUlBBYfQTSkIpkwrGIipI1llUNQIsubd+bEbLfkw1ULdDIlc9zUnNWKbFosNYoEESdM0yAWmgEgio7eINXUYtevXoazjGMQOqhRBBD380DZOR9qTz60GvwLnJeyFAMCcKFFBGUDLElkPprPRMY+l6tnu/F2cNK1ucCVRIWNoJQjxgKnQs6ODFLJsoyheS0fhbEghhgJ5nR7DtunA0SVm6+QyQmb+IqvSEbzQgZIYCEFZwZIw5walVY1EzV+WrVJCZKghoKRhOjKeX9wzf+uf/PKw3ypGKCcAAF1hCzUVFv6KnjA7//118/N1njmC1B0rqGLM80JOVF76c3+tFKkp8JVhADFotRVfQdSBe56z0QlILkpLXUc4CIppb/7JHn/+/PPDmTkbINITA8NCMJwKtiR/9JVG5KEATAsyoyRWAPPj5e+8yTBx4/Ofo3z56dVnh4+CqHOiPPR2dQRPRG269ANe+pCZxQkUyEhshSIPIduKtQAyHJo241k5BkopI4eubc+DcPXHj2yPBTh69mzD43hpKgwBJV65/O8ATkwaukAvWQYGHdWEJ/+bWXL81I1L3pKy+c++yzAzV2FDliIU0JqsRK7o0rA1H1ouIFmYSgHiRKvp7CQxQBbzQ8GSRjAbOIhiTVzAusd3x8PPmTrzw3I2VvO/76y8+9PDCbmLiuRaXYZwoBL1nx/xNZPQwlFQVznCLKCtFQSn/ypX17jw278oq4tS+zLZ99dP+X9w9OczmhyLNRY/IA+/CGU1VUg6ohBtsZwoVabVJFwIsWSW/07YQ01RBAyB1/TYTmwvGp9A8+99zAaN2Wewrty8fm5H/81eMvDczUXbHCZY1KxjmC8PeqNLiWQPIPu7cRQSkJSB0PVuWTX3zxiX1nudBDplnhNCALOHb2gim19K/qcmR9CASyTERKr18nBYghE5GRLExNDGcxd+zYsXLPbcXWDuXAzPz6iy83LGIwgTxMVogOT9R/96+ePH15ttjc6zNHHEWFwvTc/Onzlzv7VnR2FAAmVaMpkGeF/IO/6Dsevv//vySIevVEPuIDQ7N/8qVnj1+atIUeUHtWZRsZMqmIiE+czr/vrTvfd8fq/iKT1A3ldGt+/bPHq5DUGBE4m0Qyx1wGd4YEphkU0xtcakUCaZBMOIrnmb51YepPv/TchSvzcXm5UpMGGOdCVrdWFqauLGu3H37nnrfvXt1OIZYKqSNT/F4MT86BFqJMYEgNLaVg5rsHFMz8iq75q8JRG3kj+dW/4a4hRNcCFySoV6ooPXn44mcee+lyRVy5RyRSieGd+jRIYgvNxM6n85SO3bat8yMP7di1ss1ohkDGWqhcU3Fc62RDIFBoSuIAUxdUI5SQFDQRKpGSEpuGiAi54pdAFBrXLoj35KKxhD///OnPP31wLhTiYof3LBmHLHXFWIVUNSrYZO5qkWbfdufmDz20q79sGMHmYuMcTLjes1OXqnx8m6/qovfJkmxakRO880xBFQEZ1cAUBCSw5JOEJU0KTRVFSYMlkGSsgZlUKagRcspElJOOl2J7rrsWEtQDWU5BDMSiBGUSoSBadK4KHBlNHnny0FMvn6hRudi2AiYmYg2Z+CQkgTjmqEgmspH16Wx1+srKdn7vvTsfvGFdXyubnJAeUpM/DQEZp7C5ezez5sZ4i/6eeU1NgtxejqAqPjUkxBp88Aq2kRKpMVU1L58e+eIzJw+dm061KS63B1VCpiGFCrFROLYFMpaRZZUpXxnZsqbjXW+55b5ty3qMZBIgufSrwerPPbzhlEx+V258gEViU0O9rqqkaNyeJQTv2bJAVVW4YCgYncsUGTVTVq8ZChcWaEHM9q5YAfGBVZhVNYCNh/GgPHMTiyZ5SwS2nCuZqaaqDiCfOjCBA7FY4wkD45UnDw9889Cl4aER45rZNZEpiRhjIybO6nWQAB7EJsq3i6BZVZJ5I/UN6/t/8O7td2/s7CjCB+UQnBFRjzwCHsRgB0Ye9Y5rnDJpROTkXK4AnzCU2aRePGwURylwfCR95IUTL+47PlMlW+xg1xxErWWfVkkz4mKWUlQqkOGQ1VWzUF+IY04qc5Glu7avfuftW2/Y0l4CWNRSBpU0wMOqsY6Qx4iwvmLtaIPrJw3hFXOu3BCiTNU6ZvBYTednptb2RJaRaYGCT9iYP3/s0KMvnbv/zhtu2rF2RVehSIgUJviIoMGrCDsrxuE6thIWOWOqCAiZZhZsyBiyCkwFHLuy8PKpi3uPnBocr6htc67IphACNCgUhpBU5uPIRsXC/Px0FBdAxntPZNlaZpYQsqBFWtixqnznLdt2bupd0cJlwEggSRnCBoBTiug1PxWA4DVkhpmYPZsArgKTKc4Pz+09eHrfyZGhqWBdZGxRyeYFiGT1KOI0qRh1cVRKfEbG5I4BxlkJHqLEyCrTrbHs3Np3/63rb1jT3RNzEVBIluu3GQTlHLZqmKhoYy8jwCdQUYFXozbybDNGHZhckHMXhr71wtFYaz//o2/vKlmvbIkUMAvBHb4wOzB7oOul8xtX99y8fdWO1d19LbaNYI1FloEoqNC1CbpoHEUgIgcbk/XATMCV6dqZ4am9Jy4dOzs8PltnV4xK/cxNuT29iSKVFH6hNjvUVtJ3PXznug19X3v8+L6XD7tSk3UliiKFNa4gSRYZCLuXL0wcvPj8iu7mm7f137pt5YZlLV2FYisByDXhSq/kgS6dBIaJOAZxAkwGXJioHR4YO3By8PylsblKnWxL3NRFbKGsEiR4DYnlUF+YaW5p+uDb9/R1NP3Nl545MXC12LFSuEw2Zg4aUiBELZ0VrT93amTvyYsb+7tv3bF25+plG5Y1d8QoAAJ4qELzOHempQkjeViYwMDFArMAzKQYGKkcPj108uzli5eHx8dn7tq1IeFiFjIDoiytwBX/+Jkrf/h3R6NSIcuqycJMU5H7elrWrejYum7Z+pWdHS1N7WXbahsESL7uOaRAtZ7NVpKR6eTslfHD5y4NjM5M16SawLomF7VIYPUcUpioYAoF8XWfzYVkbF1f/OEfuOWebX0WOlqjLz529KtPvezjFltoZ1fOkgwCgoYs2LgASZPqtGSV5gJWdjdvWd27bV1fb2fLsvZCZ8u1zWTpUwUgA+bqMj6XjM9UzlwaO3LuyuDo/ExV0gyu0BwVy6IQAbMTUWIykKw2lVUmdm7uf8+777pzdaEDcno6+9RXDz217xKKy41rZsOiKTQlI0RqrEtrSVJdYEk6yrx2eXnjitbNa5etWt7eUi6WS1GJGx/sWtEAzACj89n0fHVseuHs4MT5y5MXr0xMzyXsSlGhKU2zGzZ0/NIn7lxOmaae0nROXNP/fG7kDz93JCqVwEoq3teDrxNSoqxUdG2tTd2thZ6yK0RxuVQqlQoEqlSq9Xp9vlKdmpqcmE+nErdQqQVYV2g2NgacilWBCjE7ovzQzLLaVNFW775l4/se3LG+My7JvFUERFMhfvb01U9/5YXB8Vpc7iIuiAcRJBBRJOKh3hj4rO59TbMkimxzc3lZm+luonKp2NrS0tzUxIaDD0mWzS8szM3Ojc4l4wu+Uk3qiQgi5igqlImdBFWFBG8KjsAqCk2TymTZVN9x74533bWjt0mbUTUh0UL3SMaP7R/6u8cPXZ1OTdzCLlKwCEGImAE2zohPJVRDNheyuWLBtMZoLdjOzo6u9tZSIS6XSoapXq/V67U0S8cW/NhMbWZ2br5S98JKjk3BuiKZiI2tLizs2tL9f3x8z0oEoyAfKoFLf/StwT/8wuFSe/uiIpkXqcgafJLVqyFLc+lezhFmNiB47/NdlV3sCk2GLbETrz71BMq9vNkaG0WATysTsjCyeUXTD9636/6b17dYiA/WskOq2UxNY4nazk8lf/vNQy8cvTRXZ1tsY1cMvqH5wmK+IBtW8RK8SKaaSkglBFIy1hEgEhrqIwnEkXEla52xjpgkhJzMKyHk3gDGGCJJFqYpzG/sb/+ht+66d1tPOVRcNm8LLZUQeR9M5IT4xNDs5x9/+YXDg1VticpdJmoRgYQUFJgRvGc2JiqIF1WSUPHJfPAeitycI0/NUQmiyjY2NjbOGmeJGMyUF56iRFSrzO7e1P0fPnZrvxUEtcjD5EKdUSPTqsqkRGRUWYKQKsFEzlHB2EJBRUVE8yA1IG6JiElFRBQKFRIPFRh2YCHKyAi74P1sfXa0vYSH37rjPXduX9tWdCJJhtSZfecXhq9cvO/WNa3FIoLf0hH/vz942z03rf/yM0cPnhmtVmzc1GpsRCaSkBvhcvB5NpiLXJy7CbE1UITMSwiWmZ2l/A6hDLXBh+ADKDd1E7BYZ1QyBKnNjEt9Zt2q9gdv333/7rV9zdaEYDimQvzNY1eG5vS+PWtbRZzU9/Q1r/nIg09sGfj682fODA7Vk/lCS4d1CFmSJakKwRaQiiICnI2KrtCuQSSIeE9scn/6vG9F+UVMc0BKGxKXhlkVGQ7kqw5QH4KIXbydZj6p1GfmbVRiGymrqiCIIBA8TABp8HXN5QNBiZmNFZ+E4NlwHoNBxgBEbEBK5CFJdXqC4HvaoodvW/fA7Tu29rW0ENT71No5xhMHx/7mKy9cvHDx6RPbPv7B+zd0OU1qzZw9tLlt16YHnjk+8vVnTp6+MjNfVWPjYrktqOTRhESq6rMkY2ds5CSkGnJvuRC8KiwZbkByArAhMoDJNUaGja9UkvqCQ7KqK7pnz60P3/L/7epMnys5r/N+lvft7rsCuMAAg2Uw+8p1uEoiKYtmtFKOU1YpTirlSpyU/4hUpZzKf5GkXC4nH1KOVbYc27FsUpQskSLFfZnRcMjBcDAzwAAY7Lhbd7/vOScf+kKkcusW8BG3APTbfZ7zPL/n9NmJVEMvDM1qrdUS/v5n1/7y5TdK5OWtve89/8iZViax6Dj3u0+defKBU6+9s/Tjd5c+21jtF+KztkubQAkimYpp10wkMgKrCgCycwZx1P8BCAgiYFEMFKsGXwBTRURTKIvQ399lS5wBmLFzWOZdl2Tvre7/7Rs3P765u7a51yuE0ianTWSPo/injRJkhxUwpqqq7LgauUwZNeWERQrRQmWgZTejsNBpfOXxh555cPHCdC1hiCGy40B4Y7v4m59fe+WNG/2Y1lozeXd/foq+/62HvvnY0Q7kEHPjWsR0YwjvLm2/fvXutU9ubu4PlTKXNV1SJ2YwiaEEVeLDkRSA2CGixjjqcUBCYuIUkE0sFnks+lb26t7OnJp/6sHFpy7NnZ9O2mAW9xWp5PYHm8UPXvnktbc/U06Jo+Rbj5yb+f1vPfXEyak2QCyFPAPC7YP49icbr797/dOV7d2BCSYurfksZUaJVRCTTc0MK2r8iHQ3ckF4BAdoYGJSqgWLRSz6ZDI1OX5yvv6NxxZffPQkF4Wyw7LXI1LM3BCzu7vllRt3rt3a+HRl7+7mYFCyUjUTEDty/teaxCG41kxNzUwK0BLNguOi1bDJcTp7rPPEhfmHT8wttLMUQPIghJa4XYG3P13/y39859qtXa7No++4tK6hXww2mkn3q5cXf++Fx85NeAeiZXRMyH7P4MbqwftL9z5Y2vj09mYvN/L1CplEQMiuCqgcljoiqBgIqI5iCyISg4U8oXJxtvXQqalHz85cOjU3U6NUYyVwC7ktpZfeufnDVz5c28GkMYuUScydL/LeynhDfue3nvjmExdONGi0eCc0wr0In65uvvvp6kc37q1uDfb7WgiZMbGSQ8IUKUVKwXhEwEcFrE48VQkmATRPXTzSdqdmxx48M3vx9MKF+c44lBRLEVBfw3IwIILgSIgTcIrQM1jbj3c2e3e3ems7w9X1nZ3dvd6gGAbQqtvEtPp3ZXaJT5hcPaUj47XJ8eax+YkTc1Nzndpcm8YQWIBjMBBM0x7g1bXe37328evvLw1C6tI2YALAxKBSOkaJwzDYPTE79sJXHvr644sLGTiNEAYAgmkrB7dRwvXVgxsrO8v3du+sbu31+oMhFGWVNyOralUrJYeqvHXpOY61Gsdmp07MdU5Mty8udE5O+RYASSkxGDn16Y7BB8u9H795/dU3r0aq1ZqTUcDX66YmZQRTLQcWuudOTb343MWvXZyfZrAYwJMokcMCYKuA9f3i1vr+rXv3793fub/X7+cWghWlhAAiBgBEwASIljpr1tzExPjRqbFO0y9Oj5+cHVucrI878AC5GFtMLSC5AA7LIrDjLqIhZAJRhB0pgJp5pgAwjNAfxm4v7/UKAYiqUa1aw2Te12tJ6pxPudbyNQ+16iYnlhBoUAT0DgeIn273f/TaR//0zs3tgUubM742JjGCFqBFyHtJvaHKYI5dMujuOO2dO97+5tPnnn3o+HydE+hHMQAnRgGJ2PUANrtwf2d/Y2ew0ysHw/yg2+8PhnmeI0Cz2Rgfazbq9U4zme3UpsbHptqu7sAZpKiVCJJ4DOB3Il25s/PyW0tvXVnd60OtNUXMgAKosey7rC6lM0k4qZkWRb7hZfeRk53vfOmBZ87NH2k4UQigCiBYleCAAgjAXjf2hqGI1hsU/WGpCszEBJ6QEGoZNuq+Ua+36q6GQABRVEUr1mzOSAAtUzTLBXCYB0zch3e3dnr5E+fn6pVbJpQJ2iHAgQAIyH2RxlS9v1C/raIDiUDozVgAIeEcYLeEW6trb16789oHa2ub+74+6WsdUQcASCJFzzR3iXM+CyXEEtnXEdQsj/mOh8Glc4vPPHz8idMz81NZHcBrRAkVPAaZAVwALg6jkFWJ2+jIA0CADCABqSxTpmZIQj4iB8C9IXx4Z/vVj269f2Vpu6tJY5r9WIwAYMRKpBL6UhbsG5S0ADwxmxZxsB3y7XoKl0+MPf/I6YcfOD3TdimAqZEqWAkWvAMzp+bJ+S+K+/T/bUKkukFiWQZwifepAOQAH97eHRz0n7u0kKKVAlgEBYf/48fv/O+/e+3ihTNPXH7g7LEj85PN6RTcYbtiFRwVU/hCQpZGQpKNoFggSB7RDQG2cri7279ye+Pdazdv3lne66XojnufknMSA4KaFs6JhH6SUL3V3ry/Reiz5mQozIDYe0SQchjKIUh/frL28IXFy+ePPnxiar4ONTCEUjUAgIJTcPBrpRqxqlA0U0B0JmxRTcGxYVoCbwosrfU/vr3z/pXbHy+v5+qdrxGlyFkVrpdyiCRlORyvUcqyMxiiS5EzgCzmQsQucQJRBvdqcDAz1Xn04qnLFxdOzkzMtKgBVY1vYeDUWA1GOPDDQCMetoMhIOGo1bkE6BlsHoSlle0r12++/eH1+U7rP/3R92YaHASwjKqM//PnH//XH7xuyThYOTmenT7WOTM/cWZ+emF6bHKsljhICeqHis6vE4ICVUESFAB7BRz0w8rG/vVbazdXt5fX9zb3B5jUknoToa2xbWaIpnEAsYfWl3xzbqb93W8/M7849fevfPDWWx9hMu7TjlgGmCAyEoJFlVJiHvL9mitPzjXPL3ZOz08dP3rkaKdVz7DB0KgQ9Ycfyb4AdggAuUAusN0r72zt31hZv3b7/s27+3s9I9/yaR2UVIldpooSCuciWL/obc7PH/net56e6yR//dIb7370acR6Wp9WSQEzNXBpihYk9EM5iGW30eITC5OL060TR8bPL0wvTLU6dUppNEXTb543BpAD5AAxwsEgru8ObtxeX1rdubW2f2/jIC9EwsGzDx//43//zSmnaogxhODcn716409++H7SmjGNKnkMQ4hF6nFsvDk1OTE+MTE94WfHwDNl3qfOoUFRlmWM3eGw1+vtDHR1R+7f39k/GEZzRilxzWdt5BTIWQApIgAgmUkehjssu196+Ni/fvHJ87NNBNgO8OO3Pv3bn3y4shmy1pxL2moAKIiRmEFJyoGEnsQhWOkZGvXa5OTk9NGZhXGerYVGPc3SJPG+Yt5EkWFR5nlxv3Ar+3J//f7m1la3PyhKVWVyDeYmgAeKSKriADPnU9MiDO9z3PrKw4vf//ZXTk2nDHpQ4Mu/uP6DH/1yL/ikNQWYGDgTxaTp0iZYtFiE4V4suiZ54qTVSFvN2mzHT4+nY63meLteT733DgjKqGWI0Wz9gDf348Hewfb27s7ufl5KFARMklrLJWm/u/nkuSP/+d99dcaJGqCEPLj0T1777E9/eDUdmwUDMDFTRAGTGAYScoklgnlfNaQgWhWLR9EoEg3M2JOrE3t2CfsEkSvslxmpmBQ5Q2AHRX8XtD871fj2Vx/+xpdOz6bAZQ4xos8K79690/uLl95/7/pqxFqtOa6qakrsNZqZujRDZI1Rq5dECdHikCE4R8yUpp7ZVf1sIVaQfWfkiIjYEXvnPSKJRI0CAERiJohM5GKZh8HOXMd/7+uXX3zq1DhJKIfAjikNhO98dvAXP37zg6UVoYypbsKUTQI3RkV5o+pbMYkSSxWRUKiUhMpkziFRRc8wMTBDMUB27FMiD+CIRhs/VSCiwcHa4+fG/8u/fXbWBVV1YBEgBSJEQs1FsTr+ARMzYed90jINpoiuXrX86iHiA8EcIjObgQkgk5mqAjsECLEYIBIzu7oLJfQO1ifH9LeePPf1py9enGmxllACoUNCT4hBn1psnvyD515597OfvHP9k9t3FVu11jSYAZSI3tQrOqQMQVByx+AZgCbJ1zWGEIoQtSrVM0RM2XHiIEcLSKmoA3RqjKNduCIrUsrIxXC37K9MjfEzz51+8blHL01nXiKWRZ3rAQhALcpjp5on/8PXf/rezf/7s6vLG6VR21tA2au6fYgZkQ0ZKGFfJ7a02SLyEgemOUCwUQ8AkpEZMgzZKVKqxsjJaGmnJqFQEDOrFp+oSiZOq71rLIv+FjvyWR1cKtFEhMiZVfQN92t2DPFvLNHNTFVMDZGRCAxNopgRETNqzMsQJIbxRvrMc5eef3LxsePjHcwp7KKrFUn28vt3lpfvvfDM5cXpFEuZ9/JvvnL8q48u/uTDlZd/+dnttX0Fj2YuTUa1gEhIigxEACaqpYpDAudI1RCRHY/INxpMdCRII/g0BSCNgdBVcYSQByn3j07SE09e+PoTpx5Z7NRBo4iyOzD301dv9fv6jRdOd2oYY3eK+feePP34pdOvvL/6+ns3767djwo+qROmiK4qIgIgJCCHImUMAUkP9a3RDt6MzBCUAUENDQCRzdQkEIJjBAXJ+3HYNx1lJ1yBrKDHxvyJqWStO4gxkMtc2vJJZgZGaBoRItCozHpEkT9kOFYGB3JGVAI6BGdKKlLkQym6Gcuxmc5Dpye/dvnY+RMTbQSOgUQ4bWzG5Iev3/rhK+9vbh68eaf8ly9efu5My1vJ+e7ZemP2yye+fGnxg8+23nh/ZWl5Z7+3PYyaNtouywDAJYiUxHK0GLARYogBQPWQkWtm6k29yxyyaeybRY1lORySQeqSxSPJ04+cf+bhuQeP1sagGwf3zLfAj13fkx+8dOXnb90tc/xgdfsPvvvgw/NtjQOy4ZlGdvzZ+W8/OPHqlZU3r63fvL2+2+9S0vZpi5wnRoCosQQj+hwIdbjIRQM0AhMBiUgJMxNWBXuIYTgMw5wMJjM+0clMLaABAhbaU9Oe1pe3w9Vbm+/96rNPb2/u5hghQU4JgVDYIlBiPHZYQVj1g1bXNaiqlD20HrIvSzUxxzDR4AvHJ566OH/53NzChE/B4rDLpi5r9MhfXSv+/OUrb19ZpqSF6Id7exNt/PZvXfzOM+eONwlkSIbOeQDeUbixVny8tPLh9eXP7m3tD2IprHB4dpMn9iNkYaU4IphW8qhpFI1BJZgVYIXD2Mzc3NT4pXMnHji3eH6uOd+CBEwliEbzyZ6616/d/+tXrty8u5s0G8Cxu7l5an7q+y889fylozOpSRGMGVLuA+wUsLSy89aV5as319e3h72hiJFLMkRAqrGrk6tcS3K4hBm1PVjRNSmAWBFVTcLQYWwlMN9pPnD2+OWLC5eOtae8ehwaA4psSTkQ11HXUIB+qdv7/U9u3/vo0zvL9wf7Rbrdw+7AQlAAwarRpMLamIJVqqN3FGq+bNV4etyfO3bk0smjp2cnjo7XWh4JDLWA0Cdy6ls3D+ylD1Z/9Mtba9t5WmsSGgBrsFgexHzn4Yvz3/naQ5dPdRZScLHr4gA4Q982wB2B5fsHy+t7t9Z2b93bWd/s9oahF5JcUlBVq+ruDKkqemFATGlQo0EjS2emWrOTrZNzk2ePHVmcHp9ukAej2IOYl5DEtLWPtLQV/+G1j3/xzlLAGnHdsMtJBKgP9rp1D09fWPgXXz7/2PGxJCiICQZKkV3aB1g/kNub+9dvb3+yvLa+090/GO4PfRETq5Y/1YwD1Ug2kioJYuLieI0msjg7llw6Pf/A6eNHO+3OWMIALgqXA+IYHWMs7zFZwPFCa6SRSb1zBlgAdiOsdeXOZr62O+x28zDoF2WZD4fD4RAM0ixLs7TVbDZajUbGnVY6N9WcHXcTI2mnlFAWZSB27D2yXx/Sa9c3/+GXNz76dN1lY0m9GWMJoAhInBJiyHvD3s542z109ugLT5x++ux0xwevAaMiMxIrcgQuAA4Edvq6udPbOsh7pRZFHA6GeZ7HEIkozdJavZamvpXRZDudGmtOtX3TQQ0gq2xFUqqUhhxcVmByYzu8/M7tX360fG+zS67mszoQSZkjAicZIokUMe+Op/aNJ89896mzJyeS1FRjXhkH2PuAnAMNADZ7tr7V3dgddgfa7w/63X5eFHmei2qSeO8Tdtwca7bbtalmutCpz42l7YSanhxA0KBmquxUOfSIURKPsbiPzm0U9YjpdPZr76ECAtLIn3RYaQEKIAoqVjUpOBqNXVXxh4GBWmXiByJkFjABWu/aG9c3f/ruzV/d2i21lmQZYiAnpgLAFR5aBdQgrdfDoBuKgxqVj148/qXHzz96srPYAgfgQFEjqAKgEQNxVUztfnPogy9AdSqwb4ULJ0WrYG4OAEiB7itcvZu/+f6t967dWd8ZGNVcWoNqbgBBbACkseghmUs8EsY4lHxroYNfe/L0849cOD5RqyGQieRDAnHeKSVKXr8AN6qAgqIAVXsNjjBiXAVdAEQhBmVGIjNCBOwZxEGcdECo4BglDoST//Xqtbeu3n72sUuXzi4sdJI6VB4GVTVRiwaEmHDVRl0tLkxVcUR6GlVvmgoysk8EqAewF2BlN7z70fI71+7dWC3KYFlz3ABMSqKAGFQE2QMSk4tRAJk4NQMwBS2LQc8xnVxoPnCyfeH0sXML43NtHgOoQJCVCUHEVJUQiQirrGIFna6eXKgEigaOKEFwAtAH2A6wfH/wyWd3ri33r33W2987cGmdODEkTpyZAAihxYASMU08EkiIBggYfQ3KfLssdxc6nUdOHX/swePn5sdmW9wCcBaxwj8bjBythwGjkcftMGSpaApVT7AzQ0FUghJgY6Cr93d/8e4VF+IfffeFqQyNAMthGTP/33569U//6u1me3FyzF84Pv7YmclLxzqLU7UajeKN+vlPGgVrVJWICLDiQoVDvXa/H9d2u7+6vf3mtdWbq4ODIRo1fNpgjKZFDAU5B4gIWvT3xtpZvdHe2NxHRARGn7FLQ1kyeyaOoYyhZ9LPPMx0GqfmOmfn28enW7Pj7ZlOs5UBfeFywS/Q2e0wuCIAhcH2QdzcH6xs9Zbu7d24u7WyvtMd5mINxIZzHtmNYqkawSIRFPlwaqqZOFvf2ENInE8BwSwaAPsMKCn6+1r2s8SOTTceOnP0oVMzp2cnjo4lNQY+1L3sc8DmyNiFv3llR4MAcL8rS5sHH9za/vDG2vrmwe723acvLv7xH35n2oQQHQEZACaJS1ilv3KvWFndeO0d12nX5o+2Zzv1UwudxaPtqVbS8oSIRBX2nww4xliWhYQwiLK6n99e3b2zsb+61d/YHuz3Yynks0ZSrwF6kRDCALRANFQRESkHJ6Yb//yfPXry9MyPf7H0s5+/PYjI0FZrEDpTDaIImNTayGNSFne3BrdWl3/+Tmwk0Exhcrw2MdaYnJxqjU1479MsSbxjR2BWllKUIYY4GAz29ne3dw+2d3q9XA4GMigEXMY+S7Lx1GcxSAzRE6qpFYX3Fsv9fLB/4cKZ33/x8YmG+5ufffSLd66XhU+yunNZKCxEZF/3aYJZMxbDGysHN+786h9/8asjY+n0RH1hpnPqxNzxTtLymCZJlmVpklQ1btXqEsz2C9nqh/Xtwa17O2s7/dWN/Y3t7kF/YGa1Rp18ZkktMlg0MMPYHYZm9t9fvfJn/+ftWvOYqkNM1SiUQcIQNfdc1hJo1ZN2rc6OkyTx3jOzmeV53u/1hsNhHmI/SF5GwZRcjX3NeQ+mSKZaaqmAKSep874c9srBzlQLn3/y7O8+e+n4uAOQEvjdmzt//g9vfri0aX4ybRwh8rEsEQ0gAEZAYk4BWEJUiaZBpVAJpqVZZGZix44rF7mqmlQ+DAZIiB0xIyESOp+y9xpVoqiZT1JAkmhEFPMDyTcns/Jbzz74rWcfXhhHsHBg2evXN/7qpfeXVrpK7VpjAoxjWVJiSNEMEYmZQlnEYqBSoIbEU4aSstXr9UajWUsz550ZxDKEEFTiQV728pCXMgwgRpxkzqdMiAhEWBRrj5yd/o//6rePYyCMDhkBwBGmWROTjhViiEjoE/CewFLV0Neye8ArewkyESGSqkSNERCJWkhjCJF8TBsJEpkqmCGBRDVRRM9OkFQlH/R3WjX+8uUTLz57/smTrRaAhpKsbBA8d7qz+Iff+qcPV3761tKd9Y1+QUnWIpcagIFaNLOIRECeXQKGI6288t4DaBUuVAVESqh6GThTBjAgJCIzNdNQiqkye8IKUx0sDmMcTNTt8UdPv/jMhUeOteoSTIYCkjH+zqWZBxe+8dLbKz9589b67rYxMQGB16iqgJwgMfsGu7qZgonG0DfXM97squ6pakkUAaoHEwBlx54dA6GvUcKMVasFsgEbuRjWY1kiA6BJ2XdAGAEkQtEbpjQk9uRQQqlSVoUIDhIzb4YICTIjooqAc1RPRv4YBCQPgCoCMVbWKhBk5pgHBUSMsbfVrrmvPnriG09fuHxyosMVmxfZJyogEhOTk3We/vKxZx6Y/eWV2298dOf6Zxu9A0hbHfIp+yqmMFQVpEOkIhMgmyQV3/JwHsNqLFUFQEUK1dJKgoEdSlKIIhHMQt4lHU6P41NPHvvtJ85enJuYYLBQAgC6tLpbkMmFNs8/v/i1h2Zfv3r3Z+/euH1vKy/q5OvMVbalVKs0i8qUwoSETGAEyefa1+ffsUCISFx520bsZYumESigmUNSgEjGDK5iFrc9txLoFTuIxC4hTpgZqYp2EhKjBdO+KZiZSlXOMQJ3EiJYCtBwFfNeSoWhSV4MhxqHtXptbqr5yOkHnnzk7MXF1jhCWikOVe2cIaMHdgjoTOuAx9vu6DOnv/z46as3t977aOna8s76/iAvCnaJ8wlRwi4F9ACkogABqZQQq4G04r/bqOsQEGuILQAzi0wisbQYDSTkA1Nt1+GhM2OPPnDp0YvzJ48kbQAPGssiISR2ZtWzoCKoaawBnZv2C7996kuPLX5wffmNKys3V7vdbq/M0SUN4gwoI19DYiNDLIgKrcRj+5xPO2KOYh3QAwqiIiqiqJQihWkUMAzdTn06QyAwYkYJvcDZnZ5+dPfg45W9m7dW7q5sHPTKUhgo5aRFvkboASPi4JA4i6PwX+X/i0ECAiQqpcShxT5DPtFOjs1MnDk5d/Hc4rm58bkmZQAmkTSmjIzVbOAMuPqTAyiSEGFUKwFKQCRfGNzcLq7ePbixtHJ3dfP+9sF+LwRh5MxlTaLErEQIxHwoAyoAsncIKDGIeKLMQELe06JHUNZTHG9n80cnz58/8cDxiUtzzYlk5HpyIA7NVBEZgMwq148ARAABBDGKFQwB6F4BN+8Plm6tfbx0d3l1e2tnUIhDrgNnRCknyhR/Dd21EXJh9EuTmKo5hCBxKGUftGAo6xkdnZk8Ojv50JmZyyemznTqtdjPEkKVg2C+pCwiRIBeDvd3+rfWtm+t7qzcP9julrsHw7yUCBSNRaKZIVIltTEzEQNiQtJOpdnIpqfGZiebM+PZqdnJU7Pj4ymyAWvAKIjKDolGljQDBnMVdUUBCJQgIIipIKEAiiI5r+BLgAJgq6vLa7t37++ubvbXd/p73Xy/O+zlEitGvIpU5WRERKNjzqE0U2o20rFGMjvVnpusLxwZOzU/OTOe1CrOP6jGCBYZAUCJnQJFHdkDHRiCGkYEEYkqSuQJE4kWiMU5QOhGWNnKV7f217b7d9Z3761vd/v5fslDYVMRERU9hJ5Xchg5NDZJHLabfrzpZzvNxdmJk/NTx2bGm3XnGTKEmqrTgplQrYvgoyZghjo0RHQpAA8A9nIYBNkfFnsHxW5P9nKNoiKCh6sEYkrTtFZPJxo408JGlrQzV2dIARIAB2qhMAlIDtABKjAa4giUDoyHsPiqy4dATCOYEiIAjR4wYgQL5BIlb+AiQA+gV8KwlIN+sdmLeznmeciHeQihMroBQuJdkqXtFGbaPN7KGolv1bAJ4AEYAkK0WAI4JK8qYIpU8a6dERtwNGOzpEpxYtUqoKAGioweDTUWKhE5QZ8YkwKUAH2FXi55GVa7utnTUMpwUBRlAQZIhDD62s5woplMtLOJZtJIuJVh43MRIJaKiJSioomR+392JqV8Y0ClMgAAAABJRU5ErkJggg=="

CSS = """
<style>
.block-container {padding-top: 1.4rem;}
.hero {background: linear-gradient(120deg, #0f172a 0%, #0f766e 100%); color: #fff; padding: 26px 32px; border-radius: 18px; margin-bottom: 14px;}
.hero-row {display: flex; align-items: center; gap: 22px;}
.hero-logo {height: 84px; background: #fff; border-radius: 14px; padding: 7px 10px; flex: none;}
.hero h1 {color: #fff; margin: 0 0 4px 0; font-size: 2rem;}
.hero p {color: #cbd5e1; margin: 0; font-size: 0.98rem;}
.chip {display: inline-block; background: rgba(255,255,255,.14); color: #fff; padding: 3px 12px; border-radius: 999px; font-size: .8rem; margin: 10px 8px 0 0;}
.kpi {border-radius: 16px; padding: 16px 18px; color: #fff; box-shadow: 0 4px 14px rgba(15,23,42,.18); min-height: 118px;}
.kpi-icon {font-size: 1.4rem; opacity: .95;}
.kpi-label {font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; opacity: .9; margin-top: 2px;}
.kpi-value {font-size: 2rem; font-weight: 700; line-height: 1.15;}
.kpi-note {font-size: .78rem; opacity: .85;}
.badge {display: inline-block; padding: 2px 12px; border-radius: 999px; font-size: .8rem; font-weight: 600;}
.b-active {background: #dcfce7; color: #166534;} .b-expired {background: #fee2e2; color: #991b1b;} .b-nf {background: #e5e7eb; color: #374151;}
h2, h3, h4 {letter-spacing: -.01em;}
</style>
"""


def badge(status: str) -> str:
    cls = {"Active": "b-active", "Expired": "b-expired"}.get(status, "b-nf")
    return f'<span class="badge {cls}">{status}</span>'


def kpi(col, icon, label, value, note, c1, c2):
    col.markdown(f'<div class="kpi" style="background:linear-gradient(135deg,{c1},{c2})"><div class="kpi-icon">{icon}</div>'
                 f'<div class="kpi-label">{label}</div><div class="kpi-value">{value}</div><div class="kpi-note">{note}</div></div>',
                 unsafe_allow_html=True)


def show_fig(fig, title=None, height=340, container=st):
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=44 if title else 10, b=10), legend_title_text="",
                      title=dict(text=title, font=dict(size=15)) if title else None)
    container.plotly_chart(fig, width="stretch")


def count_df(series: pd.Series, name="consents") -> pd.DataFrame:
    return series.value_counts().rename_axis("name").reset_index(name=name)


def status_style(v):
    return {"Active": "background-color:#dcfce7;color:#166534;font-weight:600",
            "Expired": "background-color:#fee2e2;color:#991b1b;font-weight:600"}.get(v, "")


# ------------------------------------------------------------------ header: time, location, weather
HDR_LAT, HDR_LON, HDR_CITY, HDR_TZ = -36.8485, 174.7633, "Auckland, NZ", "Pacific/Auckland"
WMO = {0: ("Clear", "☀️"), 1: ("Mostly clear", "🌤️"), 2: ("Partly cloudy", "⛅"), 3: ("Overcast", "☁️"),
       45: ("Fog", "🌫️"), 48: ("Fog", "🌫️"), 51: ("Drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Drizzle", "🌦️"),
       61: ("Rain", "🌧️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"), 80: ("Showers", "🌦️"),
       81: ("Showers", "🌦️"), 82: ("Heavy showers", "⛈️"), 95: ("Thunderstorm", "⛈️"),
       96: ("Thunderstorm", "⛈️"), 99: ("Thunderstorm", "⛈️")}


@st.cache_data(ttl=600, show_spinner=False)
def get_weather():
    """Current Auckland weather from Open-Meteo (free, no key). Returns (icon, temp, label, wind); placeholders if offline."""
    try:
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&timezone=%s"
               "&current=temperature_2m,weather_code,wind_speed_10m" % (HDR_LAT, HDR_LON, urllib.parse.quote(HDR_TZ)))
        with urllib.request.urlopen(url, timeout=5) as r:
            cur = json.load(r)["current"]
        label, icon = WMO.get(cur["weather_code"], ("Weather", "🌡️"))
        return icon, f'{round(cur["temperature_2m"])}°C', label, f'{round(cur["wind_speed_10m"])} km/h'
    except Exception:
        return "🌡️", "--", "Weather unavailable", ""


def render_header():
    """Hero banner: logo + title on the left; live clock, location and weather on the right."""
    import streamlit.components.v1 as components
    icon, temp, label, wind = get_weather()
    wind_txt = f" · 💨 {wind}" if wind else ""
    html = f"""
    <style>
      body {{ margin:0; font-family:"Source Sans Pro", "Source Sans 3", sans-serif; }}
      .hero {{ background:linear-gradient(120deg,#0f172a 0%,#0f766e 100%); color:#fff; padding:26px 32px;
               border-radius:18px; box-sizing:border-box; display:flex; align-items:center;
               justify-content:space-between; gap:24px; }}
      .main {{ min-width:0; }}
      .left {{ display:flex; align-items:center; gap:22px; min-width:0; }}
      .logo {{ height:84px; background:#fff; border-radius:14px; padding:7px 10px; flex:none; }}
      h1 {{ margin:0 0 4px 0; font-size:2rem; }}
      .sub {{ margin:0; color:#cbd5e1; font-size:.98rem; }}
      .right {{ text-align:right; background:rgba(255,255,255,.12); border-radius:16px; padding:12px 20px;
                min-width:215px; flex:none; }}
      .time {{ font-size:2rem; font-weight:700; line-height:1.1; font-variant-numeric:tabular-nums; }}
      .date {{ color:#cbd5e1; font-size:.85rem; margin-bottom:8px; }}
      .loc {{ font-size:.92rem; }}
      .wx {{ font-size:1rem; margin-top:3px; }}
      .chip {{ display:inline-block; background:rgba(255,255,255,.14); color:#fff; padding:3px 12px;
               border-radius:999px; font-size:.8rem; margin:10px 8px 0 0; }}
      @media (max-width:760px) {{ .hero {{ flex-direction:column; align-items:flex-start; }} .right {{ text-align:left; }} }}
    </style>
    <div class="hero">
      <div class="main">
        <div class="left">
          <img class="logo" src="data:image/png;base64,{LOGO_B64}" alt="Auckland Council">
          <div><h1>Air Discharge Consent Analytics</h1>
          <p class="sub">Auckland Unitary Plan air discharge consents - read straight from the decision and memo PDFs.</p></div>
        </div>
        <span class="chip">Conditions: consent PDF</span><span class="chip">Years, rules, details: memo PDF</span>
        <span class="chip">Nothing is guessed</span>
      </div>
      <div class="right">
        <div class="time" id="t">--:--:-- --</div>
        <div class="date" id="d"></div>
        <div class="loc">📍 {HDR_CITY}</div>
        <div class="wx">{icon} {temp} · {label}{wind_txt}</div>
      </div>
    </div>
    <script>
      function tick() {{
        const n = new Date();
        document.getElementById('t').textContent = n.toLocaleTimeString('en-NZ',
          {{timeZone:'{HDR_TZ}', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:true}}).toUpperCase();
        document.getElementById('d').textContent = n.toLocaleDateString('en-NZ',
          {{timeZone:'{HDR_TZ}', weekday:'long', day:'numeric', month:'long'}});
      }}
      tick(); setInterval(tick, 1000);
    </script>
    """
    components.html(html, height=210)


st.set_page_config(page_title="Air Discharge Consent Analytics", page_icon=None, layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
render_header()
try:
    import plotly.express as px
except ImportError:
    st.error("This dashboard needs plotly: run `pip install plotly` and restart the app.")
    st.stop()

files = st.file_uploader("Upload consent and memo PDFs (pairs are matched on the DIS number)", type="pdf", accept_multiple_files=True)
if not files:
    st.info("Upload the consent and memo PDFs to begin. Nothing is kept between runs.")
    st.stop()

key = tuple((f.name, f.size) for f in files)
if st.session_state.get("key") != key:
    st.session_state["rows"], st.session_state["key"] = analyse(files), key
rows = st.session_state["rows"]
if not rows:
    st.stop()

# ---- sidebar: status date + filters
st.sidebar.header("Filters")
as_at = st.sidebar.date_input("Status as of", date.today())
df = pd.DataFrame(rows)
df["status"] = df.date_expiry.map(lambda d: NOT_FOUND if d is None or pd.isna(d) else ("Active" if d >= as_at else "Expired"))
df["expiry_year"] = df.date_expiry.map(lambda d: str(d.year) if isinstance(d, date) else NOT_FOUND)
df["class_label"] = df.activity_status.fillna(NOT_FOUND)
df["zone_label"] = df.air_quality_area.fillna(NOT_FOUND)
with st.spinner("Locating consents on the map..."):
    loc_cache = st.session_state.setdefault("loc", {})
    for r in rows:
        loc_cache.setdefault(r["consent_id"], locate(r))
df["lat"], df["lon"], df["location_basis"] = zip(*[loc_cache[i] for i in df.consent_id])
show = lambda v: NOT_FOUND if v is None or (isinstance(v, float) and pd.isna(v)) else v

f_status = st.sidebar.multiselect("Status", sorted(df.status.unique()), default=sorted(df.status.unique()))
f_class = st.sidebar.multiselect("Activity class", sorted(df.class_label.unique()), default=sorted(df.class_label.unique()))
f_zone = st.sidebar.multiselect("Air quality area", sorted(df.zone_label.unique()), default=sorted(df.zone_label.unique()))
exp_years = [d.year for d in df.date_expiry if isinstance(d, date)]
keep = df.status.isin(f_status) & df.class_label.isin(f_class) & df.zone_label.isin(f_zone)
if exp_years and min(exp_years) < max(exp_years):
    lo, hi = st.sidebar.slider("Expiry year", min(exp_years), max(exp_years), (min(exp_years), max(exp_years)))
    keep &= df.date_expiry.map(lambda d: lo <= d.year <= hi if isinstance(d, date) else True)
fdf = df[keep].reset_index(drop=True)
st.sidebar.caption(f"Showing {len(fdf)} of {len(df)} consents. The chatbot always searches all {len(df)}.")
if fdf.empty:
    st.warning("No consent matches the sidebar filters.")
    st.stop()
rows_f = [r for r in rows if r["consent_id"] in set(fdf.consent_id)]

st.sidebar.divider()
st.sidebar.subheader("Export")
try:
    st.sidebar.download_button("⬇️ Excel summary", build_excel(fdf), "consent_summary.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
except ImportError:
    st.sidebar.caption("For the Excel export run: pip install openpyxl")
try:
    st.sidebar.download_button("⬇️ PDF summary", build_pdf(fdf, as_at), "consent_summary.pdf", "application/pdf")
except ImportError:
    st.sidebar.caption("For the PDF export run: pip install reportlab")

# ---- KPI cards
k = st.columns(5)
kpi(k[0], "📄", "Consents", len(fdf), f"of {len(df)} uploaded", "#0f766e", "#0e7490")
kpi(k[1], "✅", "Active", int((fdf.status == "Active").sum()), f"as of {as_at:%d %b %Y}", "#15803d", "#16a34a")
kpi(k[2], "⛔", "Expired", int((fdf.status == "Expired").sum()), "past expiry date", "#b91c1c", "#dc2626")
kpi(k[3], "📅", "Avg years granted", f"{fdf.years_granted.mean():.1f}" if fdf.years_granted.notna().any() else NOT_FOUND, "from the memos", "#1d4ed8", "#2563eb")
kpi(k[4], "📋", "Conditions", int(fdf.n_conditions.sum()), f"{fdf.n_conditions.mean():.1f} per consent", "#6d28d9", "#7c3aed")
st.write("")

# ---- insights
i1, i2 = st.columns(2)
with i1:
    st.markdown("#### ⏳ Expiring soon")
    horizon = st.slider("Look ahead (years)", 1, 20, 5)
    soon = expiring_soon(fdf, as_at, horizon)
    if soon.empty:
        st.success(f"No consent expires within {horizon} year(s) of {as_at:%d %B %Y}.")
    else:
        st.warning(f"**{len(soon)}** consent(s) expire within {horizon} year(s):")
        t = soon[["consent_id", "applicant", "date_expiry", "days_left"]].copy()
        t["date_expiry"] = t.date_expiry.map(lambda d: d.isoformat())
        st.dataframe(t.fillna(NOT_FOUND), width="stretch", hide_index=True)
with i2:
    st.markdown("#### ⚖️ Granted fewer years than requested")
    sf = shortfall(fdf)
    if sf.empty:
        st.success("No consent was granted fewer years than requested (where both are stated in the memo).")
    else:
        st.warning(f"**{len(sf)}** consent(s) were granted fewer years than the applicant asked for:")
        t = sf[["consent_id", "applicant", "years_requested", "years_granted", "short_by"]].fillna(NOT_FOUND)
        st.dataframe(t, width="stretch", hide_index=True)
    st.caption("Only consents where the memo states both the requested and the granted years.")

# ---- Map (own section, above the tabs)
st.subheader("🗺️ Consent map")
mdf = fdf[fdf.lat.notna()].copy()
if mdf.empty:
    st.warning("No consent could be located: the PDFs print no NZTM reference and the address lookup found nothing "
               "(it needs an internet connection).")
else:
    mdf["air_quality_zone"] = mdf.air_quality_area.fillna(NOT_FOUND)
    mdf["site"] = mdf.site_address.fillna(NOT_FOUND)
    fig = px.scatter_map(
        mdf, lat="lat", lon="lon", color="status", hover_name="consent_id", color_discrete_map=STATUS_COLOURS,
        hover_data={"site": True, "air_quality_zone": True, "status": True, "location_basis": True, "lat": False, "lon": False},
        labels={"site": "Location", "air_quality_zone": "Air quality zone", "status": "Status", "location_basis": "Position from"},
        center=AUCKLAND, zoom=9, height=560)
    fig.update_traces(marker=dict(size=16))
    fig.update_layout(map_style="carto-positron", margin={"r": 0, "t": 0, "l": 0, "b": 0}, legend_title_text="")
    st.plotly_chart(fig, width="stretch")
    st.caption("Centred on Auckland (zoom out/in or drag to explore). Green = active, red = expired. Hover a dot for the location, status and air quality zone.")
miss = fdf[fdf.lat.isna()].consent_id.tolist()
if miss:
    st.warning("Could not be placed on the map (no NZTM in the PDFs and no address match): " + ", ".join(miss))

t_over, t_reg, t_rules, t_cond, t_det, t_dq = st.tabs(
    ["📊 Overview", "📑 Register", "📐 Triggered rules", "📋 Conditions", "🗂️ Consent summaries", "🔎 Data quality"])

with t_over:
    tl = fdf.dropna(subset=["date_granted", "date_expiry"]).copy()
    if not tl.empty:
        tl["start"], tl["end"] = pd.to_datetime(tl.date_granted), pd.to_datetime(tl.date_expiry)
        fig = px.timeline(tl, x_start="start", x_end="end", y="consent_id", color="status", color_discrete_map=STATUS_COLOURS,
                          hover_data=["applicant"])
        fig.update_yaxes(autorange="reversed", title=None)
        fig.update_xaxes(title=None)
        fig.add_shape(type="line", x0=str(as_at), x1=str(as_at), y0=0, y1=1, yref="paper", line=dict(color="#475569", dash="dash", width=2))
        fig.add_annotation(x=str(as_at), y=1, yref="paper", text="Status date", showarrow=False, yshift=10, font=dict(color="#475569"))
        show_fig(fig, "Consent timeline - grant to expiry", height=90 + 44 * len(tl))
    a, b = st.columns(2)
    fig = px.bar(fdf, x="consent_id", y="years_granted", color="status", color_discrete_map=STATUS_COLOURS, text_auto=True)
    fig.update_xaxes(title=None); fig.update_yaxes(title="years")
    show_fig(fig, "Years of consent granted (memo)", container=a)
    fig = px.bar(fdf, x="consent_id", y="n_conditions", color="status", color_discrete_map=STATUS_COLOURS, text_auto=True)
    fig.update_xaxes(title=None); fig.update_yaxes(title="conditions")
    show_fig(fig, "Number of conditions (consent)", container=b)
    a, b = st.columns(2)
    long = fdf.melt(id_vars="consent_id", value_vars=["years_requested", "years_granted"], var_name="measure", value_name="years")
    long["measure"] = long.measure.map({"years_requested": "Requested", "years_granted": "Granted"})
    fig = px.bar(long, x="consent_id", y="years", color="measure", barmode="group", color_discrete_sequence=["#f59e0b", "#0f766e"])
    fig.update_xaxes(title=None)
    show_fig(fig, "Years requested vs granted (memo)", container=a)
    ey = count_df(fdf.expiry_year).sort_values("name")
    fig = px.bar(ey, x="name", y="consents", text_auto=True, color_discrete_sequence=[PALETTE[1]])
    fig.update_xaxes(title="expiry year", type="category")
    show_fig(fig, "Consents by expiry year", container=b)
    a, b = st.columns(2)
    fig = px.pie(count_df(fdf.class_label), names="name", values="consents", hole=0.55, color_discrete_sequence=PALETTE)
    show_fig(fig, "Overall activity status (memo)", container=a)
    fig = px.pie(count_df(fdf.zone_label), names="name", values="consents", hole=0.55, color_discrete_sequence=PALETTE[2:] + PALETTE[:2])
    show_fig(fig, "Air quality area (memo)", container=b)
    d = fdf[["years_granted", "n_conditions", "rules_triggered"]].astype(float)
    if len(d) > 2 and d.nunique().min() > 1:
        fig = px.imshow(d.corr().round(2), text_auto=True, color_continuous_scale="Teal", zmin=-1, zmax=1, aspect="auto")
        show_fig(fig, "Correlation: years granted, conditions, rules triggered", height=300)
    else:
        st.caption("Correlations need at least three consents with varying values.")

with t_reg:
    reg = register_view(fdf)
    st.dataframe(reg.style.map(status_style, subset=["status"]), width="stretch", hide_index=True)
    st.download_button("Download register CSV", reg.to_csv(index=False).encode(), "consent_register.csv", "text/csv")

with t_rules:
    rr = pd.DataFrame([dict(consent_id=r["consent_id"], **x) for r in rows_f for x in r["rules"]])
    if rr.empty:
        st.info("No triggered rules were found in the memos.")
    else:
        a, b = st.columns(2)
        fig = px.pie(count_df(rr.activity_class, "rules"), names="name", values="rules", hole=0.55, color_discrete_sequence=PALETTE)
        show_fig(fig, "Triggered rules by activity class", container=a)
        grp = count_df(rr.group.fillna("Group not stated in memo"), "rules")
        fig = px.bar(grp, x="rules", y="name", orientation="h", text_auto=True, color_discrete_sequence=[PALETTE[0]])
        fig.update_yaxes(title=None, autorange="reversed")
        show_fig(fig, "Triggered rules by rule group", container=b)
        st.caption("Most frequently triggered rules")
        st.dataframe(rr.groupby(["rule", "description"]).consent_id.agg(["count", lambda s: ", ".join(s)])
                       .rename(columns={"count": "consents", "<lambda_0>": "consent ids"})
                       .sort_values("consents", ascending=False), width="stretch")
        st.caption("All triggered rules")
        st.dataframe(rr.fillna(NOT_FOUND), width="stretch", hide_index=True)

cc = pd.DataFrame([dict(consent_id=r["consent_id"], **x) for r in rows_f for x in r["conditions"]])
with t_cond:
    if cc.empty:
        st.info("No conditions were found in the consent PDFs.")
    else:
        by_theme = cc.groupby(["consent_id", "theme"]).size().reset_index(name="conditions")
        fig = px.bar(by_theme, x="consent_id", y="conditions", color="theme", color_discrete_sequence=PALETTE + ["#a3a3a3"])
        fig.update_xaxes(title=None)
        show_fig(fig, "Conditions by theme (keyword-based grouping of the condition text)", height=380)
        heat = cc.pivot_table(index="consent_id", columns="theme", values="number", aggfunc="count", fill_value=0)
        fig = px.imshow(heat, text_auto=True, aspect="auto", color_continuous_scale="Teal", labels=dict(color="conditions"))
        fig.update_xaxes(title=None, tickangle=-30); fig.update_yaxes(title=None)
        show_fig(fig, "Heatmap: which themes each consent's conditions cover", height=120 + 40 * len(heat))
        words = cc.assign(words=cc.text.str.split().str.len()).groupby("consent_id").words.mean().round(0).reset_index()
        fig = px.bar(words, x="consent_id", y="words", text_auto=True, color_discrete_sequence=[PALETTE[3]])
        fig.update_xaxes(title=None)
        show_fig(fig, "Average condition length (words)")
        st.caption("All conditions")
        q = st.text_input("Filter conditions (text)")
        view = cc[cc.text.str.contains(q, case=False, regex=False)] if q else cc
        st.dataframe(view, width="stretch", hide_index=True)
        st.download_button("Download conditions CSV", cc.to_csv(index=False).encode(), "consent_conditions.csv", "text/csv")

with t_det:
    for r, s in zip(fdf.to_dict("records"), fdf.status):
        dot = {"Active": "🟢", "Expired": "🔴"}.get(s, "⚪")
        with st.expander(f"{dot} {r['consent_id']} - {show(r['applicant'])}", expanded=len(fdf) == 1):
            st.markdown(badge(s), unsafe_allow_html=True)
            st.dataframe(summary_table({**r, "status": s}), width="stretch", hide_index=True)
            if r["rules"]:
                st.markdown("**Rules that trigger consent (memo)**")
                st.dataframe(pd.DataFrame(r["rules"]).rename(columns={"rule": "Rule", "group": "Rule group", "description": "Description",
                                                                      "activity_class": "Activity class"}).fillna(NOT_FOUND),
                             width="stretch", hide_index=True)
            st.markdown(f"**Conditions ({r['n_conditions']}, from the consent)**")
            cur = None
            for x in r["conditions"]:
                if x["section"] != cur:
                    cur = x["section"]
                    st.markdown(f"*{cur}*")
                st.markdown(f"{x['number']}. {x['text']}")

with t_dq:
    dq = pd.DataFrame([dict(consent_id=r["consent_id"], missing_or_unreadable=", ".join(r["missing"]) or "-",
                            expiry_basis=show(r["expiry_basis"])) for r in rows])
    st.dataframe(dq, width="stretch", hide_index=True)
    st.caption("'Grant date + memo duration' means the consent's expiry condition is relative (e.g. '10 years after issue'), "
               "so the expiry is the consent's decision date plus the years in the memo.")

# ---- Chatbot (own section, below the tabs)
st.divider()
st.subheader("💬 Consent chatbot")
st.caption("Ask about anything in the Register or Consent summaries tabs, e.g. *tell me about DIS60308433*, *who is the specialist for DIS60361208*, "
           "*which consents are active*, *anything in Henderson*, *more than 15 years*, *what does rule A54 mean*, "
           "*conditions about odour for DIS60316131*, *average years granted*. "
           "Answers are computed from the extracted data - no external AI service is used.")
if st.session_state.get("chat_key") != key:
    st.session_state["chat"], st.session_state["chat_key"] = [], key
for who, msg, tbl in st.session_state["chat"]:
    with st.chat_message(who):
        st.markdown(msg)
        if tbl is not None:
            st.dataframe(tbl, width="stretch", hide_index=True)
if q := st.chat_input("Ask about the consents..."):
    ans, tbl = ask(q, df)
    st.session_state["chat"] += [("user", q, None), ("assistant", ans, tbl)]
    with st.chat_message("user"):
        st.markdown(q)
    with st.chat_message("assistant"):
        st.markdown(ans)
        if tbl is not None:
            st.dataframe(tbl, width="stretch", hide_index=True)
