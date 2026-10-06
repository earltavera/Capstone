"""
Air Discharge Consent Analytics (Streamlit)

Run:  pip install streamlit pdfplumber pandas plotly pyproj
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


# ------------------------------------------------------------------ UI
st.set_page_config(page_title="Air Discharge Consent Analytics", layout="wide")
st.title("Air Discharge Consent Analytics")
st.caption("Conditions come from the consent PDFs only; years granted, triggered rules and all other details come from the memo PDFs.")
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

as_at = st.sidebar.date_input("Status as at", date.today())
df = pd.DataFrame(rows)
df["status"] = df.date_expiry.map(lambda d: NOT_FOUND if d is None or pd.isna(d) else ("Active" if d >= as_at else "Expired"))
df["expiry_year"] = df.date_expiry.map(lambda d: str(d.year) if isinstance(d, date) else NOT_FOUND)
with st.spinner("Locating consents on the map..."):
    loc_cache = st.session_state.setdefault("loc", {})
    for r in rows:
        loc_cache.setdefault(r["consent_id"], locate(r))
df["lat"], df["lon"], df["location_basis"] = zip(*[loc_cache[i] for i in df.consent_id]) if len(df) else ([], [], [])
show = lambda v: NOT_FOUND if v is None or (isinstance(v, float) and pd.isna(v)) else v

c = st.columns(5)
c[0].metric("Consents", len(df))
c[1].metric("Active / expired", f"{(df.status == 'Active').sum()} / {(df.status == 'Expired').sum()}")
c[2].metric("Avg years granted", f"{df.years_granted.mean():.1f}" if df.years_granted.notna().any() else NOT_FOUND)
c[3].metric("Total conditions", int(df.n_conditions.sum()))
c[4].metric("Avg conditions / consent", f"{df.n_conditions.mean():.1f}")

# ---- Map (own section, above the tabs)
st.subheader("Consent map")

try:
    import plotly.express as px
except ImportError:
    st.error("The map needs plotly: run `pip install plotly` and restart the app.")
    px = None
if px:
    mdf = df[df.lat.notna()].copy()
    if mdf.empty:
        st.warning("No consent could be located: the PDFs print no NZTM reference and the address lookup found nothing "
                   "(it needs an internet connection).")
    else:
        mdf["air_quality_zone"] = mdf.air_quality_area.fillna(NOT_FOUND)
        mdf["site"] = mdf.site_address.fillna(NOT_FOUND)
        fig = px.scatter_map(
            mdf, lat="lat", lon="lon", color="status", hover_name="consent_id",
            color_discrete_map={"Active": "#00CC96", "Expired": "#EF553B", NOT_FOUND: "#999999"},
            hover_data={"site": True, "air_quality_zone": True, "status": True, "location_basis": True, "lat": False, "lon": False},
            labels={"site": "Location", "air_quality_zone": "Air quality zone", "status": "Status", "location_basis": "Position from"},
            center=AUCKLAND, zoom=9, height=560)
        fig.update_traces(marker=dict(size=16))
        fig.update_layout(map_style="carto-positron", margin={"r": 0, "t": 0, "l": 0, "b": 0})
        st.plotly_chart(fig, width="stretch")
        st.caption("Centred on Auckland (zoom out/in or drag to explore). Green = active, red = expired. Hover a dot for the location, status and air quality zone.")
    miss = df[df.lat.isna()].consent_id.tolist()
    if miss:
        st.warning("Could not be placed on the map (no NZTM in the PDFs and no address match): " + ", ".join(miss))

t_over, t_reg, t_rules, t_cond, t_det, t_dq = st.tabs(
    ["Overview", "Register", "Triggered rules", "Conditions", "Consent summaries", "Data quality"])

with t_over:
    a, b = st.columns(2)
    a.caption("Years of consent granted (memo)")
    a.bar_chart(df.set_index("consent_id").years_granted)
    b.caption("Number of conditions (consent)")
    b.bar_chart(df.set_index("consent_id").n_conditions)
    a, b = st.columns(2)
    a.caption("Years requested vs granted (memo)")
    a.bar_chart(df.set_index("consent_id")[["years_requested", "years_granted"]])
    b.caption("Consents by expiry year")
    b.bar_chart(df.expiry_year.value_counts().sort_index())
    a, b = st.columns(2)
    a.caption("Consents by overall activity status (memo)")
    a.bar_chart(df.activity_status.fillna(NOT_FOUND).value_counts())
    b.caption("Consents by air quality area (memo)")
    b.bar_chart(df.air_quality_area.fillna(NOT_FOUND).value_counts())
    d = df[["years_granted", "n_conditions", "rules_triggered"]].astype(float)
    if len(d) > 2 and d.nunique().min() > 1:
        st.caption("Correlation between years granted, number of conditions and rules triggered")
        st.dataframe(d.corr().round(2), width="stretch")
    else:
        st.caption("Correlations need at least three consents with varying values.")

with t_reg:
    reg = register_view(df)
    st.dataframe(reg, width="stretch", hide_index=True)
    st.download_button("Download register CSV", reg.to_csv(index=False).encode(), "consent_register.csv", "text/csv")

with t_rules:
    rr = pd.DataFrame([dict(consent_id=r["consent_id"], **x) for r in rows for x in r["rules"]])
    if rr.empty:
        st.info("No triggered rules were found in the memos.")
    else:
        a, b = st.columns(2)
        a.caption("Triggered rules by activity class")
        a.bar_chart(rr.activity_class.value_counts())
        b.caption("Triggered rules by rule group")
        b.bar_chart(rr.group.fillna("Group not stated in memo").value_counts())
        st.caption("Most frequently triggered rules")
        st.dataframe(rr.groupby(["rule", "description"]).consent_id.agg(["count", lambda s: ", ".join(s)])
                       .rename(columns={"count": "consents", "<lambda_0>": "consent ids"})
                       .sort_values("consents", ascending=False), width="stretch")
        st.caption("All triggered rules")
        st.dataframe(rr.fillna(NOT_FOUND), width="stretch", hide_index=True)

cc = pd.DataFrame([dict(consent_id=r["consent_id"], **x) for r in rows for x in r["conditions"]])
with t_cond:
    if cc.empty:
        st.info("No conditions were found in the consent PDFs.")
    else:
        st.caption("Conditions by theme and consent (keyword-based grouping of the condition text)")
        st.bar_chart(cc.pivot_table(index="consent_id", columns="theme", values="number", aggfunc="count", fill_value=0))
        st.caption("Average condition length (words) by consent")
        st.bar_chart(cc.assign(words=cc.text.str.split().str.len()).groupby("consent_id").words.mean().round(0))
        st.caption("All conditions")
        q = st.text_input("Filter conditions (text)")
        view = cc[cc.text.str.contains(q, case=False, regex=False)] if q else cc
        st.dataframe(view, width="stretch", hide_index=True)
        st.download_button("Download conditions CSV", cc.to_csv(index=False).encode(), "consent_conditions.csv", "text/csv")

with t_det:
    fmt = lambda d: f"{d.day} {d:%B %Y}" if isinstance(d, date) else NOT_FOUND
    for r, s in zip(df.to_dict("records"), df.status):
        with st.expander(f"{r['consent_id']} - {show(r['applicant'])}", expanded=len(df) == 1):
            info = summary_table({**r, "status": s})
            st.dataframe(info, width="stretch", hide_index=True)
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
st.subheader("Consent chatbot")
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