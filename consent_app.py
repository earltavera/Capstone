"""
Air Discharge Consent Analytics (Streamlit)

Run:  pip install streamlit pdfplumber pandas plotly pyproj openpyxl reportlab
      streamlit run consent_app.py

Every run starts empty: upload the <ID>_Consent.pdf and <ID>_Memo.pdf files (any number of pairs).
Nothing is cached between runs. A value that is not in the uploaded PDFs is reported as "Not found"
(and listed on the Data quality tab), never guessed. The only online call is the map: when the PDFs print
no NZTM reference, the memo's site address is looked up on OpenStreetMap.

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
    r"Air (?:quality|discharge) review.*|BUN\d+.*Air discharge.*|.*\(Air discharge:.*\)\s*\d*|"
    r"DIS\d{8}\s*:?\s*Air (?:quality|discharge) review(?: memo)?\s*\d*|DIS\d{8}\s+\d{1,3}|"
    r"Page ?\d+ of \d+\s*DIS\d{8}|Condition # .*|[A-Z][A-Za-z ]+ v Auckland Council)\s*$")


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
    except ValueError:                        # 29 Feb
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


_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                   "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
_W = "|".join(sorted({*_UNITS, *_TENS} - {"zero"}, key=len, reverse=True))
NUM = rf"(?:\d+(?:\.\d+)?|(?:{_W})(?:[- ]?(?:{_W}))?)"          # 15 / 18.5 / fifteen / thirty-five


def num_val(s):
    s = s.lower().strip()
    try:
        return float(s)
    except ValueError:
        return float(sum({**_UNITS, **_TENS}[t] for t in re.split(r"[- ]+", s) if t))


def memo_duration(lines):
    """(years granted, years requested): the 'Duration of consent' section if the memo has numbered headings,
    otherwise sentences found anywhere in the memo (unnumbered headings, number words, 'a 35-year term')."""
    g, r = _memo_duration_section(lines)
    if g is not None and r is not None:
        return g, r
    txt = re.sub(r"(?<=[a-z])- (?=[a-z])", "-", flat(lines))
    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z\u2022])", txt):
        if not re.search(r"year", sent, re.I):
            continue
        for clause in re.split(r"[;,]|\bbut\b|\bwhereas\b|\bhowever\b", sent):
            c = clause.lower()
            if re.search(r"request|applied for|seeks?|seeking|sought", c):
                m = re.search(rf"(?:request\w*|applied for|seeks?|seeking|sought)\s+(?:an?\s+|for\s+(?:an?\s+)?)?(?:consent\s+)?(?:term\s+of\s+)?(?:at least\s+)?({NUM})[ -]?years?", c)
                if m and r is None:
                    r = num_val(m.group(1))
                continue
            if g is None and re.search(r"\bterm\b|duration", c) and not re.search(r"previous|expired|existing|current|former|too long|\bwould\b|typically", c):
                for pat in (rf"term of (?:approx\w*\.?\s*)?({NUM})[ -]?years?", rf"duration of (?:approx\w*\.?\s*)?({NUM})[ -]?years?",
                            rf"({NUM})[ -]?years?\s+(?:consent\s+)?(?:term|duration)", rf"approx\w*\.?\s*({NUM})[ -]?years?"):
                    m = re.search(pat, c)
                    if m:
                        g = num_val(m.group(1))
                        break
    return g, r


def _memo_duration_section(lines):
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


CLASS_PAT = r"(Controlled|Restricted discretionary|Discretionary|Non-complying|Prohibited|Permitted)"


def fallback_rules(lines) -> list[dict]:
    """Memos with no 'Reason for application' section state the rule in the introduction:
    '... is a Discretionary activity pursuant to Rule E14.4.1(A2).'"""
    out, seen = [], set()
    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z\u2022])", flat(lines)):
        for m in re.finditer(rf"{CLASS_PAT}\s+activity\s+(?:pursuant to|under|in terms of)\s+(?:Rule\s+)?E14\.4\.1\s*\(?(A\d{{1,3}})\)?", sent, re.I):
            if m.group(2).upper() in seen:
                continue
            seen.add(m.group(2).upper())
            desc = re.split(r"\s+(?:is|are|will be|would be)\s+an?\s+" + CLASS_PAT, sent, flags=re.I)[0]
            out.append(dict(rule=f"E14.4.1 ({m.group(2).upper()})", group=None, description=re.sub(r"^[\u2022\s]*", "", desc).strip(" ."),
                            activity_class=m.group(1).capitalize()))
    return out


def fallback_applicant(lines):
    """Memos without an 'Applicant's name:' label: the first 'Some Name Limited (Short name)' in the text."""
    m = re.search(r"((?:[A-Z\u014c][\w&'\u2019.-]*\s+){1,6}(?:Limited|Ltd|Incorporated|Inc|Corporation|Holdings|Trust|Partnership|Company))"
                  r"\s*\(\s*[\"\u201c\u2018']?[A-Z][\w ]{1,25}[\"\u201d\u2019']?\s*\)", flat(lines))
    return m.group(1).strip() if m else None


def fallback_address(lines):
    """Memos without a 'Site address:' label: '... Recycling at 263 James Fletcher Drive, Favona, Auckland' in the Subject paragraph."""
    for i, ln in enumerate(lines):
        if re.match(r"Subject:", ln):
            parts = [ln]
            for nxt in lines[i + 1:i + 3]:
                if re.match(r"(I |The |This |To:|From:|\u2022)", nxt) or parts[-1].endswith("."):
                    break
                parts.append(nxt)
            m = re.search(r"\bat\s+(\d+[A-Za-z]?(?:\s*/\s*\d+)?\s+[A-Z].+)$", flat(parts))
            return m.group(1).strip(" .") if m else None
    return None


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
        applicant=g(r"Applicant(?:'s|’s)? ?(?:name)?:\s*(.+)") or fallback_applicant(lines),
        site_address=re.sub(r"\s*\(.*?\)\s*$", "", g(r"Site address:\s*(.+)") or "") or fallback_address(lines),
        years_granted=granted, years_requested=requested,
        rules=triggered_rules(lines) or fallback_rules(lines),
        air_quality_area=norm_aq(aq.group(0)) if aq else None,
        ghg_site_total=float(ghg.group(1).replace(",", "")) if ghg else None,
        specialist=f"{frm.group(1).strip()} ({frm.group(2).strip()})" if frm else None,
    )


# ------------------------------------------------------------------ consent
def parse_conditions(lines) -> list[dict]:
    """Every numbered condition between 'subject to the following conditions' (or an 'Appendix A - Consent conditions' /
    'Conditions' heading) and 'Advice notes'. Handles '5.' and court-order style '5 ' / '13A ' numbering."""
    text = "\n".join(lines)
    m = (re.search(r"subject to (?:the )?following\s+conditions\s*[:.]?", text)
         or re.search(r"(?mi)^\s*appendix [a-z]\s*[-\u2013\u2014]\s*(?:consent )?conditions\s*$", text)
         or re.search(r"(?mi)^\s*conditions\s*$", text))
    if not m:
        return []
    body = text[m.end():]
    end = re.search(r"(?m)^(?:\d+\.\s*)?(?:Advice notes?|Advice Notes)(?:\s*[-\u2013]\s*General)?\s*$|^Delegated decision maker", body)
    body = body[:end.start()] if end else body
    blines = [ln for ln in body.splitlines() if ln.strip() and not re.fullmatch(r"\s*\d{1,3}\s*", ln)]
    conds = _split_conditions(blines, dotted=True)
    if len(conds) < 3:
        conds = _split_conditions(blines, dotted=False) or conds
    for c in conds:
        c["text"] = re.sub(r"\s+", " ", " ".join(c.pop("lines"))).strip()
        scores = [(len(re.findall(p, c["text"], re.I)), -i, name) for i, (name, p) in enumerate(THEMES)]
        best = max(scores)
        c["theme"] = best[2] if best[0] > 0 else OTHER_THEME
    return conds


def _split_conditions(blines, dotted):
    sep = r"[.)]\s*" if dotted else r"\s+(?=[A-Z])"
    conds, heading, expected, last_int = [], "General", 1, 0

    def start(ln):
        """label of the condition starting on this line, else None. 13A is accepted right after 13 (court-order style)."""
        mm = re.match(rf"^(\d{{1,3}})([A-Z]?){sep}(.*)", ln)
        if not mm:
            return None
        n, suffix = int(mm.group(1)), mm.group(2)
        if not suffix and n == expected:
            return n, mm.group(3)
        if suffix and n == last_int:
            return f"{n}{suffix}", mm.group(3)
        return None

    def heading_like(ln):
        return (len(ln) <= 70 and len(ln.split()) <= 8 and ln[0].isupper() and not re.search(r"\d", ln)
                and not ln.endswith((".", ";", ",", ":")))

    for k, ln in enumerate(blines):
        st_ = start(ln)
        if st_:
            label, rest = st_
            conds.append(dict(number=label, section=heading, lines=[rest]))
            if isinstance(label, int):
                expected, last_int = label + 1, label
            continue
        j = k + 1                                                   # skip over stacked headings to the next condition
        while j < len(blines) and j < k + 3 and heading_like(blines[j]) and not start(blines[j]):
            j += 1
        nxt = blines[j] if j < len(blines) else ""
        if heading_like(ln) and (start(nxt) or re.match(rf"^{expected}[.)]?\s", nxt)):
            heading = re.sub(r"\s+conditions?$", "", ln, flags=re.I).strip()
            heading = heading[:1].upper() + heading[1:]
        elif conds:
            conds[-1]["lines"].append(ln)
    return conds


def _only_date(s):
    return re.fullmatch(rf"\s*(?:{DATE_TXT}|{DATE_NUM})\s*", s) is not None


def consent_date(lines):
    """Decision date. Tried in order: 'Delegated decision maker ... Date:', an Environment Court 'Date of Issue/Order',
    a commissioner's sign-off (name / title / date on its own line), then any 'Date: <date>'."""
    text = "\n".join(lines)
    i = text.rfind("Delegated decision maker")
    if i >= 0:
        m = re.search(r"Date:\s*(.{0,40})", text[i:i + 400])
        d = first_date(m.group(1)) if m else None
        if d:
            return d
    for lab in ("Date of Issue", "Date of Order", "Date of decision", "Decision date"):
        m = re.search(rf"{lab}\s*:\s*(.{{0,40}})", text, re.I)
        d = first_date(m.group(1)) if m else None
        if d:
            return d
    signed = [first_date(m.group(2)) for m in re.finditer(
        r"(?m)^([^\n]*(?:Commissioner|Chairperson|decision maker|Delegate|Planner|Manager)[^\n]*)\n([^\n]{6,40})$", text)
        if _only_date(m.group(2))]
    if signed:
        return signed[-1]
    dates = [first_date(m.group(1)) for m in re.finditer(r"Date:\s*(.{0,40})", text)]
    dates = [d for d in dates if d]
    return dates[-1] if dates else None


def parse_consent(lines) -> dict:
    conds = parse_conditions(lines)
    expiry_txt = next((c["text"] for c in conds if re.search(r"\bexpires?\b", c["text"])), None)
    exp = re.search(r"expires?\s+on\s+(?:the\s+)?(.{0,45})", expiry_txt) if expiry_txt else None
    rel = re.search(rf"expires?\s+(?:on\s+)?(?:the\s+)?({NUM})[ -]?years?\s+(?:following|after|from)", expiry_txt or "", re.I)
    return dict(conditions=conds, date_granted=consent_date(lines),
                expiry_condition=expiry_txt, expiry_stated=first_date(exp.group(1)) if exp else None,
                expiry_years=num_val(rel.group(1)) if rel else None,
                expiry_from_commencement=bool(expiry_txt and re.search(r"commencement", expiry_txt, re.I)))


def find_nztm(*line_lists):
    """(easting, northing) if the PDFs print an NZTM reference, e.g. '1751734E; 5931450N'."""
    m = re.search(r"NZTM[^0-9]{0,40}?(\d{7})\s*m?E?\s*[;,]?\s*(\d{7})", flat([ln for ls in line_lists for ln in ls]))
    return (int(m.group(1)), int(m.group(2))) if m else None


# ------------------------------------------------------------------ combine
def build_row(cid, consent_lines, memo_lines) -> dict:
    c = parse_consent(consent_lines) if consent_lines else dict(conditions=[], date_granted=None, expiry_condition=None, expiry_stated=None,
                                                              expiry_years=None, expiry_from_commencement=False)
    m = parse_memo(memo_lines) if memo_lines else {}
    years, granted = m.get("years_granted"), c["date_granted"]
    expiry, basis = c["expiry_stated"], "Consent condition"
    if expiry is None and granted and (c["expiry_years"] or years):   # expiry condition is relative ("10 years after issue")
        if c["expiry_years"]:
            expiry = add_years(granted, c["expiry_years"])
            basis = "Grant date + term in consent condition" + (" (condition counts from commencement)" if c["expiry_from_commencement"] else "")
        else:
            expiry, basis = add_years(granted, years), "Grant date + memo duration"
    elif expiry is None:
        basis = None
    rules = m.get("rules", [])
    classes = [r["activity_class"] for r in rules if r["activity_class"] in CLASS_RANK]
    overall = max(classes, key=CLASS_RANK.get) if classes else None
    conds = c["conditions"]
    for r_ in rules:                                                 # discharge type of every triggered rule (keyword-based)
        r_["discharge_type"] = classify_discharge(f"{r_.get('group') or ''} {r_['description']}")
    dtypes = [r_["discharge_type"] for r_ in rules]
    for c_ in conds:                                                 # mitigation measures each condition requires (keyword-based)
        c_["mitigation"] = "; ".join(mitigation_in(c_["text"]))
    missing = [k for k, v in {
        "consent PDF": bool(consent_lines), "memo PDF": bool(memo_lines), "years granted (memo)": years,
        "triggered rules (memo)": rules, "conditions (consent)": conds, "grant date (consent)": granted,
        "expiry date": expiry, "air quality area (memo)": m.get("air_quality_area"),
        "applicant (memo)": m.get("applicant"), "site address (memo)": m.get("site_address")}.items() if not v]
    return dict(
        consent_id=cid, applicant=m.get("applicant"), site_address=m.get("site_address"),
        years_granted=years, years_requested=m.get("years_requested"), years_condition=c["expiry_years"],
        date_granted=granted, date_expiry=expiry, expiry_basis=basis,
        air_quality_area=m.get("air_quality_area"), activity_status=overall,
        rules_triggered=len(rules), rules=rules, rule_text="; ".join(r["rule"] for r in rules),
        rule_groups="; ".join(dict.fromkeys(r["group"] for r in rules if r["group"])),
        n_conditions=len(conds), conditions=conds, ghg_site_total=m.get("ghg_site_total"),
        specialist=m.get("specialist"), nztm=find_nztm(consent_lines or [], memo_lines or []), missing=missing,
        discharge_types=list(dict.fromkeys(dtypes)), discharge_primary=Counter(dtypes).most_common(1)[0][0] if dtypes else None,
        mitigation=sorted({x for c_ in conds for x in c_["mitigation"].split("; ") if x}),
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


# ------------------------------------------------------------------ discharge types & mitigation measures
# Keyword lists (edit freely). A rule / condition goes to the entry with the most keyword hits; ties go to the earlier entry.
DISCHARGE_TYPES = [
    ("Fuel combustion (boilers, heaters, generators)", r"combust|boiler|burner|fuel|heater|generator|process heat|furnace|diesel|coal|biomass"),
    ("Spray painting & coating", r"spray|paint|coating|lacquer|powder coat"),
    ("Metal processing & foundries", r"foundr|smelt|metal|galvani|anodis|steel|alumin|casting|forging|welding"),
    ("Asphalt, concrete & quarrying", r"asphalt|concrete|cement|quarr|aggregate|crush|bitumen"),
    ("Wood & fibre processing", r"timber|wood|sawmill|particle ?board|mdf|fibre|sanding|kiln"),
    ("Food, rendering & odour industries", r"food|render|meat|fish|bakery|brew|coffee|roast|smok|abattoir|dairy|tallow"),
    ("Chemical, plastics & fibreglass", r"chemical|plastic|resin|fibreglass|solvent|styrene|polymer|fertili[sz]|pharmaceut"),
    ("Waste, composting & incineration", r"waste|compost|incinerat|crematori|landfill|biosolid|sewage|wastewater"),
    ("Storage, handling & abrasive blasting", r"storage|handling|bulk|silo|abrasive|blasting|grain|stockpile|loading"),
]
OTHER_DISCHARGE = "Other / not classified"
DISCHARGE_LABELS = [n for n, _ in DISCHARGE_TYPES] + [OTHER_DISCHARGE]

MITIGATION_MEASURES = [
    ("Fabric filter / baghouse", r"baghouse|bag house|fabric filter|bag filter"),
    ("Cyclone / dust collector", r"cyclone|dust collector|dust extraction"),
    ("Wet scrubber", r"scrubber"),
    ("Afterburner / thermal oxidiser", r"afterburner|thermal oxidi[sz]er"),
    ("Carbon filter / biofilter / odour control", r"activated carbon|carbon filter|biofilter|odour control|odour abatement"),
    ("Stack / discharge point requirements", r"discharged? (?:through|via|from|to) (?:a |the )?(?:stack|vent|flue)|stack height|height of the stack|metres above|discharge point|\bflue\b"),
    ("Enclosure, hooding & extraction", r"enclos|extraction system|ducting|hood|capture"),
    ("Management plan", r"management plan|\bAQMP\b|emissions? plan"),
    ("Monitoring & testing", r"monitor|stack test|emission test|sampl|opacity|detector|measurement"),
    ("Fuel type / quality restriction", r"fuel (?:type|quality|sulphur)|sulphur content|natural gas|\blpg\b|untreated wood|only (?:burn|use)"),
    ("Operating hours / throughput limit", r"hours of operation|operating hours|throughput|maximum (?:rate|quantity|capacity)"),
    ("Maintenance & inspection", r"maintain|maintenance|inspect|servic"),
    ("Water suppression / dust control", r"water (?:spray|suppress|cart)|sprinkler|dampen|wetting|sealed|stabilis"),
    ("Complaint & incident response", r"complaint|incident|breakdown|upset"),
]
MIT_LABELS = [n for n, _ in MITIGATION_MEASURES]


def classify_discharge(text: str) -> str:
    scores = [(len(re.findall(p, text or "", re.I)), -i, name) for i, (name, p) in enumerate(DISCHARGE_TYPES)]
    best = max(scores)
    return best[2] if best[0] > 0 else OTHER_DISCHARGE


def mitigation_in(text: str) -> list[str]:
    return [name for name, p in MITIGATION_MEASURES if re.search(p, text or "", re.I)]


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
        ("Term written in expiry condition (consent)", _fmt(r.get("years_condition"))),
        ("Granted (consent)", _fmt(r["date_granted"])),
        ("Expires", f"{_fmt(r['date_expiry'])} (basis: {_fmt(r['expiry_basis'])})"),
        ("Air quality area (memo)", _fmt(r["air_quality_area"])),
        ("Overall activity status (memo)", _fmt(r["activity_status"])),
        ("Rules that trigger consent (memo)", _fmt(r["rule_text"] or None)),
        ("Discharge type (keyword-based)", _fmt("; ".join(r["discharge_types"]) or None)),
        ("Mitigation measures in conditions (keyword-based)", _fmt("; ".join(r["mitigation"]) or None)),
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
            time.sleep(1.1)                                                        # Nominatim: max 1 request/second
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

# Official Auckland Council logo (Base64 encoded)
LOGO_B64 = f"{logo_b64}"

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
      .left {{ display:flex; align-items:center
