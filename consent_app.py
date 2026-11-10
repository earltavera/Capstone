"""
Air Discharge Consent Analytics (Streamlit)

Run:  pip install streamlit pdfplumber pandas plotly pyproj openpyxl reportlab anthropic
      streamlit run consent_app.py

Every run starts empty: upload the <ID>_Consent.pdf and <ID>_Memo.pdf files (any number of pairs).
Nothing is kept between runs. A value that is not in the uploaded PDFs is reported as "Not found"
(and listed on the Data quality tab), never guessed. Online calls: the map (NZTM in the PDFs, else the memo's site address
is geocoded with Google, falling back to OpenStreetMap).

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
import os
from collections import Counter
from datetime import date, datetime, timedelta

import pandas as pd
import pdfplumber
import streamlit as st

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_TXT = rf"(\d{{1,2}})\s*(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTHS})\s+(\d{{4}})"    # 3rd November 2017 / 18th of August 2036
DATE_NUM = r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4}|\d{2})\b"                         # 29 / 10 / 2018, 4/4/24
# ---- PASTE YOUR GOOGLE MAPS API KEY BETWEEN THE QUOTES (Maps JavaScript API + Geocoding API must be enabled) ----
GOOGLE_MAPS_API_KEY = "AIzaSyBl2_jHtCPQBaZ372QKgiPt39p-_6wYlW4"
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
    except ValueError:                    # 29 Feb
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
NUM = rf"(?:\d+(?:\.\d+)?|(?:{_W})(?:[- ]?(?:{_W}))?)"        # 15 / 18.5 / fifteen / thirty-five


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


def clean_site_address(raw):
    """'Site address:' value. Handles '(generator located at 21 Shaddock St)' (label empty, address in brackets on the next line)
    and drops a trailing '(note)' from 'Street, Suburb (note)'."""
    raw = (raw or "").strip()
    m = re.match(r"^\(.*?\bat\s+(.+?)\)?$", raw)
    return (m.group(1) if m else re.sub(r"\s*\(.*?\)\s*$", "", raw)).strip(" .,") or None


def parse_memo(lines) -> dict:
    whole = flat(lines)
    g = lambda pat: (m.group(1).strip() if (m := re.search(pat, "\n".join(lines))) else None)
    granted, requested = memo_duration(lines)
    aq = re.search(r"(low|medium|high) air quality\s*[–:\-]\s*(?:dust and odour|odour and dust)[^.\[\];,’']{0,25}?area(?:\s*\((?:industry|rural)\))?", whole, re.I)
    ghg = re.search(r"[Ss]ite total of ([\d,]+) tonnes\.?\s*CO\S*", whole)
    frm = re.search(r"From:\s*([^,\n]+),\s*([^\n]+)", "\n".join(lines))
    return dict(
        applicant=g(r"Applicant(?:'s|’s)? ?(?:name)?:\s*(.+)") or fallback_applicant(lines),
        site_address=clean_site_address(g(r"Site address:\s*(.+)")) or fallback_address(lines),
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
    if expiry is None and granted and (c["expiry_years"] or years):    # expiry condition is relative ("10 years after issue")
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
    for r_ in rules:                                                # discharge type of every triggered rule (keyword-based)
        r_["discharge_type"] = classify_discharge(f"{r_.get('group') or ''} {r_['description']}")
    dtypes = [r_["discharge_type"] for r_ in rules]
    for c_ in conds:                                                # mitigation measures each condition requires (keyword-based)
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
FIELD_WORDS = [   # (regex, column, label)
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


# ------------------------------------------------------------------ AI assistant (hybrid chatbot)
# The rule-based bot above answers exact lookups instantly and offline. Questions it cannot handle (comparisons,
# summaries, "why", open-ended wording) go to Claude, which sees ONLY the data extracted from the uploaded PDFs.
LLM_MODELS = ["claude-sonnet-5-5", "claude-haiku-5-5"]
LLM_HINT = re.compile(r"\b(compar\w*|summar\w*|why|explain|differen\w*|similar|stricter|strictest|stricter|less strict|recommend\w*|"
                     r"trend\w*|common|typical|pattern\w*|insight\w*|versus|vs|relationship|themes?|requirements?|overview|"
                     r"what do(?:es)?|how do(?:es)?|tell me more|key|main|biggest|risks?)\b", re.I)
LLM_INSTRUCTIONS = """You answer questions about Auckland air discharge resource consents using ONLY the JSON data supplied below.
The data was extracted from consent PDFs (conditions, grant date, expiry) and memo PDFs (everything else: years granted/requested,
triggered rules, applicant, site address, air quality area, specialist, GHG figures).
Rules:
- Never use outside knowledge about these consents. If a value is null or missing, say "Not found" - do not guess or infer.
- Cite consent numbers (DIS########) and, for conditions, the condition number, e.g. "DIS60308433, condition 7".
- For counts, rankings or comparisons, name the consents you counted so the answer can be checked.
- 'status' is Active/Expired as of the date given below. Discharge types and mitigation measures are keyword-based groupings,
  so describe them as such when you use them.
- Be concise. Use a short table only when comparing several consents. Plain language, no preamble."""


def _clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, date):
        return v.isoformat()
    return v


def llm_context(df: pd.DataFrame, cond_chars: int = 700) -> str:
    """Compact JSON of everything extracted, for the AI assistant."""
    recs = []
    for r in df.to_dict("records"):
        recs.append(dict(
            consent_id=r["consent_id"], applicant=_clean(r["applicant"]), site_address=_clean(r["site_address"]),
            status=_clean(r.get("status")), date_granted=_clean(r["date_granted"]), date_expiry=_clean(r["date_expiry"]),
            expiry_basis=_clean(r["expiry_basis"]), years_granted_memo=_clean(r["years_granted"]),
            years_requested_memo=_clean(r["years_requested"]), term_in_expiry_condition=_clean(r.get("years_condition")),
            overall_activity_status=_clean(r["activity_status"]), air_quality_area=_clean(r["air_quality_area"]),
            ghg_site_total_t_co2e_per_year=_clean(r["ghg_site_total"]), specialist=_clean(r["specialist"]),
            rules=[dict(rule=x["rule"], rule_group=x["group"], description=x["description"], activity_class=x["activity_class"],
                        discharge_type=x.get("discharge_type")) for x in r["rules"]],
            discharge_types=r["discharge_types"], mitigation_measures=r["mitigation"],
            conditions=[dict(number=c["number"], section=c["section"], text=c["text"][:cond_chars]) for c in r["conditions"]]))
    return json.dumps(recs, ensure_ascii=False, default=str, separators=(",", ":"))


def ask_llm(q: str, df: pd.DataFrame, history, api_key: str, model: str, as_at: date) -> str:
    """Answer with Claude, grounded in the extracted data. The data block is cached so follow-up questions are cheap."""
    import anthropic                                           # pip install anthropic
    ctx = llm_context(df)
    if len(ctx) > 600_000:                                        # very large uploads: shorten the condition text
        ctx = llm_context(df, 300)
    system = [{"type": "text", "text": LLM_INSTRUCTIONS + f"\nStatus date: {as_at.isoformat()}."},
              {"type": "text", "text": "EXTRACTED DATA (JSON):\n" + ctx, "cache_control": {"type": "ephemeral"}}]
    msgs = [{"role": who, "content": msg} for who, msg, _ in history[-8:]] + [{"role": "user", "content": q}]
    resp = anthropic.Anthropic(api_key=api_key).messages.create(model=model, max_tokens=1500, system=system, messages=msgs)
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def rule_confident(q: str, ans: str) -> bool:
    """False when the rule-based bot gave up / only found a loose match, or the question needs reasoning, not lookup."""
    gave_up = ans.startswith(("Ask me about", "I couldn't find", "Closest match", "No condition text"))
    return not gave_up and not LLM_HINT.search(q)


def answer(q: str, df: pd.DataFrame, history, mode: str, api_key: str, model: str, as_at: date):
    """Hybrid chatbot. Returns (markdown, table or None, source label)."""
    ans, tbl = ask(q, df)
    if mode == "Rule-based only" or (mode.startswith("Hybrid") and rule_confident(q, ans)):
        return ans, tbl, "rule-based lookup"
    if not api_key:
        return ans + "\n\n*Add an Anthropic API key in the sidebar to let the AI assistant answer this kind of question.*", tbl, "rule-based lookup"
    try:
        return ask_llm(q, df, history, api_key, model, as_at), None, f"AI assistant ({model})"
    except ImportError as e:
        import traceback
        traceback.print_exc()                                   # full traceback goes to the Streamlit logs
        if getattr(e, "name", "") == "anthropic":             # the package itself is missing
            return ans + "\n\n*The AI assistant needs the `anthropic` package: add `anthropic` to requirements.txt (or `pip install anthropic`).*", tbl, "rule-based lookup"
        return ans + f"\n\n*The AI assistant could not load the `anthropic` package: {type(e).__name__}: {e}*", tbl, "rule-based lookup"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return ans + f"\n\n*The AI assistant is unavailable ({type(e).__name__}: {str(e)[:300]}); showing the rule-based answer.*", tbl, "rule-based lookup"


# ------------------------------------------------------------------ map locations
def nztm_to_latlon(e, n):
    """NZTM2000 -> (lat, lon); None if pyproj is not installed."""
    try:
        from pyproj import Transformer
        lon, lat = Transformer.from_crs(2193, 4326, always_xy=True).transform(float(e), float(n))
        return lat, lon
    except Exception:
        return None


def address_variants(address: str) -> list[str]:
    a = re.sub(r"\s+", " ", re.sub(r"\(.*?\)", " ", address or "")).strip(" ,.")
    no_unit = re.sub(r"^(?:unit|suite|level|lot)?\s*\w*\s*/\s*", "", a, flags=re.I)      # '3/4 Amokura St' -> '4 Amokura St'
    first_no = re.sub(r"^(\d+)[A-Za-z]?\s*[-\u2013]\s*\d+[A-Za-z]?\b", r"\1", no_unit)        # '11-19 Wairau Road' -> '11 Wairau Road'
    street_only = re.sub(r"^\d+[A-Za-z]?\s*", "", first_no)                                    # last resort: the street + suburb
    return [v for v in dict.fromkeys([a, no_unit, first_no, street_only]) if v]


def geocode_google(address: str, key: str):
    """Google Geocoding API. Returns ((lat, lon) or None, error text or None)."""
    url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode(
        {"address": f"{address}, Auckland, New Zealand", "components": "country:NZ", "region": "nz", "key": key})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            js = json.load(r)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if js.get("status") == "OK":
        loc = js["results"][0]["geometry"]["location"]
        return (loc["lat"], loc["lng"]), None
    return None, f'{js.get("status")}: {js.get("error_message", "")}'.strip()


def geocode_osm(address: str):
    """OpenStreetMap (Nominatim) backup. Returns ((lat, lon) or None, error text or None)."""
    try:
        url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=nz&q="
               + urllib.parse.quote(f"{address}, Auckland, New Zealand"))
        req = urllib.request.Request(url, headers={"User-Agent": "consent-analytics/1.0 (contact: your-email@example.com)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            hit = json.load(r)
        time.sleep(1.1)                                                     # Nominatim: max 1 request/second
        return ((float(hit[0]["lat"]), float(hit[0]["lon"])), None) if hit else (None, "no match")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def locate(row, gkey: str = "", manual: str = ""):
    """(lat, lon, basis, note). Order: manual entry, NZTM in the PDFs, Google Geocoding, OpenStreetMap."""
    manual = (manual or "").strip()
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*", manual)
    if m:
        return float(m.group(1)), float(m.group(2)), "Entered manually (lat, lon)", ""
    notes = []
    if not manual and row["nztm"]:
        ll = nztm_to_latlon(*row["nztm"])
        if ll:
            return ll[0], ll[1], "NZTM reference in the PDFs", ""
        notes.append("NZTM found but pyproj is not installed")
    addr = manual or row["site_address"]
    if not addr:
        return None, None, "Not located", "; ".join(notes + ["no site address found in the memo and no NZTM in the PDFs"])
    google_ok = bool(gkey)
    for a in address_variants(addr):
        if google_ok:
            ll, err = geocode_google(a, gkey)
            if ll:
                return ll[0], ll[1], "Manual address (Google Geocoding)" if manual else "Memo site address (Google Geocoding)", ""
            notes.append(f"Google '{a}': {err}")
            if err and err.startswith(("REQUEST_DENIED", "OVER_", "INVALID_REQUEST")):
                google_ok = False                                           # key problem: no point retrying variants
    for a in address_variants(addr):
        ll, err = geocode_osm(a)
        if ll:
            return ll[0], ll[1], "Site address (OpenStreetMap lookup)", ""
        notes.append(f"OSM '{a}': {err}")
    return None, None, "Not located", "; ".join(notes)


AUCKLAND = {"lat": -36.85, "lon": 174.76}    # default map centre

GMAP_HTML = r"""
<style>html,body{margin:0;height:100%;font-family:sans-serif} #map{height:100%;width:100%}
#legend{position:absolute;top:10px;right:10px;z-index:5;background:#fff;border-radius:8px;padding:8px 12px;box-shadow:0 2px 8px rgba(0,0,0,.25);font-size:13px}
#legend label{display:block;margin:3px 0;cursor:pointer} #legend i{display:inline-block;width:11px;height:11px;border-radius:50%;margin:0 6px -1px 4px}</style>
<div id="map"></div><div id="legend"></div>
<script>
const POINTS = __POINTS__, COLOURS = __COLOURS__, CENTRE = __CENTRE__;
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const info = p => '<b>' + esc(p.id) + '</b><br>' + esc(p.site) + '<br>Status: ' + esc(p.status) +
                  '<br>Air quality zone: ' + esc(p.zone) + (p.basis ? '<br><i>Position from: ' + esc(p.basis) + '</i>' : '');
function initMap() {
  const map = new google.maps.Map(document.getElementById('map'), {center: CENTRE, zoom: 9, streetViewControl: false});
  const win = new google.maps.InfoWindow(), bounds = new google.maps.LatLngBounds(); let n = 0;
  const ORDER = ['Expired', 'Active', 'Unknown'], markers = {Expired: [], Active: [], Unknown: []}, shown = {Expired: true, Active: true, Unknown: true};
  const total = s => POINTS.filter(p => p.status === s).length;
  document.getElementById('legend').innerHTML = '<b>Status</b>' + ORDER.map(s => '<label><input type="checkbox" checked data-s="' + s + '">' +
    '<i style="background:' + COLOURS[s] + '"></i>' + s + ' (' + total(s) + ')</label>').join('');
  document.querySelectorAll('#legend input').forEach(cb => cb.addEventListener('change', () => {
    shown[cb.dataset.s] = cb.checked; markers[cb.dataset.s].forEach(m => m.setVisible(cb.checked)); }));
  function add(p, pos) {
    const mk = new google.maps.Marker({position: pos, map, title: p.id, icon: {path: google.maps.SymbolPath.CIRCLE, scale: 10,
      fillColor: COLOURS[p.status] || '#9ca3af', fillOpacity: .95, strokeColor: '#fff', strokeWeight: 2}});
    mk.addListener('click', () => { win.setContent(info(p)); win.open(map, mk); });
    (markers[p.status] || markers.Unknown).push(mk); bounds.extend(pos); n++;
  }
  function fit() { if (n > 1) map.fitBounds(bounds, 60); else if (n === 1) { map.setCenter(bounds.getCenter()); map.setZoom(15); } }
  POINTS.forEach(p => add(p, {lat: p.lat, lng: p.lon}));
  fit();
}
</script>
<script async src="https://maps.googleapis.com/maps/api/js?key=__KEY__&callback=initMap&loading=async"></script>
"""


def google_map(mdf: pd.DataFrame, key: str, height: int = 560):
    """Google Map with one coloured marker per consent."""
    import streamlit.components.v1 as components
    safe = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    disp = lambda v: "Unknown" if v in (NOT_FOUND, None) or (isinstance(v, float) and pd.isna(v)) else v
    colours = {"Active": STATUS_COLOURS["Active"], "Expired": STATUS_COLOURS["Expired"], "Unknown": STATUS_COLOURS[NOT_FOUND]}
    points = [dict(id=r.consent_id, lat=float(r.lat), lon=float(r.lon), status=disp(r.status), site=show(r.site_address),
                   zone=show(r.air_quality_area), basis=r.location_basis) for r in mdf.itertuples()]
    html = (GMAP_HTML.replace("__POINTS__", safe(points))
            .replace("__COLOURS__", safe(colours)).replace("__CENTRE__", safe({"lat": AUCKLAND["lat"], "lng": AUCKLAND["lon"]}))
            .replace("__KEY__", str(key)))
    components.html(html, height=height)
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
# Auckland Council logo (embedded cleanly as a single-line string to prevent syntax errors)
LOGO_B64 = "iVBORw0KGgoAAAANSUhEUgAAAIoAAACgCAIAAABc0mNjAAAMTWlDQ1BJQ0MgUHJvZmlsZQAAeJyVVwdYU8kWnltSIQQIREBK6E0QkRJASggtgPQuKIEJEEqMCUHFjiy7gmsXEazoKlqIOCqysgiw11bSyKvS8WVJR1cV3sypsQQJd95XvzfXPnv/+c+eecc+feOwMAvYsvleaimgDkSfJlMoH+rKTkFBbpGaACHGgCNtDhC+RSTlRUOIBluP17eX0NIMr2soNS65/9/7VoCUVyQABIFMTpQrkgD+KfAMBbBVJZPgBEKeTNZ+VLlXgtxDoy6CDENUqcqcKtSpyuwhcHbeJiuBA/AoCszufLMgHQ6IM8q0CQCXXoMFrgJBGKJRD7QeyTlzdDlgPEiiG2gDZyTrtRnp3+lk/k3zfQRTT4/cwSrYhks5ACxXJrLn/N/puN/l7xcxfAc1rCqZ8lCYpQxw7w9ypkRpsTqEL+VpEdEQqwNAIqLhYP2SszMUoTEq+xRG4GcC3MGmBBPkufG8ob4GCE/IAxiQ4gzJLkR4UM2RRniIKUNzB9aIc7nxUGsB3GNSB4YO2RzTDYjZnjeaxkyLmeIf8qXDfqg1P+syInnqPQx7SwRb0gfcyzMikuEmApxQIE4IQJiDYgj5DmxYUM2qYVZ3IhhG5kiRhmLBcQykSTYX6WPlWfIgmKG7HfnyYdjx45liXkRQ/hSflZciCpX2CMBf9B/GAtWJ5Jw4od1RPKk8OFYhKKAQFXsOFkkiY9V8bieNN8/RjUWt5PmRg3Z4/6i3GAlbwZxnLwgdnhsQT5cnCp9vESaHxWn8hOvzOaHRqn8wfeBcMAFAYAFFLCmgxkgG4g7ept64Z2qJwjwgQxiAhFwGGKGRyQO9kjgNRYUgt8hEgH5yDj/wV4RKID8p1GskhOPcKqrA8gY6lOq5IDHEOeBMJAL7xWDSpIRDxLAI8iI/+ERH1YBjCEXVmX/v+eH2S8MBzLhQ4xieEYWfdiSGEgMIIYQg4i2uAHug3vh4fDqB6szzsY9huP4Yk94TOgkPCBcJXQRbk4XF8lGeTkZdEH9oKH8pH+dH9wKarri/rg3VIfKOBM3AA64C5yHg/vCmV0hyx3yW5kV1ijtv0Xw1RMasqM4UVDKGIofxWb0SA07DDCxFWWuv86Pytf0kXxzR3pGz8/9KvtC2IaNtsS+ww5gp7Hj2FmsFWsCLOwo1oy1Y4eVeGTFPRpcccOzxQz6kwN1Rq+ZL09WmUn5U51Tj9NHVV++aHa+8mXkzpDOkYkzs/JZHPjHELF4EoHjOJazk7MbAMr/j+rz9ip68L+CMNu/cEt+A8D76MDAwM9fuNCjAPzoDj8Jh75wNmz4a1ED4MwhgUJWoOJw5YUAvxx0+PbpA2NgDmxgPM7ADXgBPxAIQkEkiAPJYBr0PguucxmYBeaBxaAElIGVYB2oBFvAdlAD9oL9oAm0guPgF3AeXARXwW24errBc9AHXoMPCIKQEBrCQPQRE8QSsUecETbigwQi4UgMkoykIZmIBFEg85AlSBmyGqlEtiG1yI/IIeQ4chbpRG4i95Ee5E/kPYqh6qgOaoRaoeNRNspBw9A4dCqaic5EC9FidDlagVaje9BG9Dh6Hr2KdqHP0X4MYGoYEzPFHDA2xsUisRQsA5NhC7BSrByrxuqxFvicL2NdWC/2DifiDJyFO8AVHILH4wJ8Jr4AX4ZX4jV4I34Sv4zfx/vwzwQawZBgT/Ak8AhJhEzCLEIJoZywk3CQcAq+S92E10QikUm0JrpDdzGZmE2cS1xG3ERsIB4jdhIfEvtJJJI+yZ7kTYok8Un5pBLSBtIe0lHSJVI36S1ZjWxCdtYHkVPIEnIRuZy8m3yEfIn8hPyBokmxpHhSIilCyhzKCsoOSgvlAqWb8oGqRbWmelPjqNnUxdQKaj31FPUO9ZWampqZmodatJpYbDZAhlaqVp7WNq"

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
gmaps_key = GOOGLE_MAPS_API_KEY.strip()
df = pd.DataFrame(rows)
df["status"] = df.date_expiry.map(lambda d: NOT_FOUND if d is None or pd.isna(d) else ("Active" if d >= as_at else "Expired"))
df["expiry_year"] = df.date_expiry.map(lambda d: str(d.year) if isinstance(d, date) else NOT_FOUND)
df["class_label"] = df.activity_status.fillna(NOT_FOUND)
df["zone_label"] = df.air_quality_area.fillna(NOT_FOUND)
manual = st.session_state.setdefault("manual_loc", {})
loc_cache = st.session_state.setdefault("loc", {})
with st.spinner("Locating consents on the map..."):
    for r in rows:
        ck = (r["consent_id"], gmaps_key, manual.get(r["consent_id"], ""))
        if ck not in loc_cache:
            loc_cache[ck] = locate(r, gmaps_key, ck[2])
df["lat"], df["lon"], df["location_basis"], df["location_note"] = zip(
    *[loc_cache[(i, gmaps_key, manual.get(i, ""))] for i in df.consent_id])
show = lambda v: NOT_FOUND if v is None or (isinstance(v, float) and pd.isna(v)) else v

f_class = st.sidebar.multiselect("Activity class", sorted(df.class_label.unique()), default=sorted(df.class_label.unique()))
f_zone = st.sidebar.multiselect("Air quality area", sorted(df.zone_label.unique()), default=sorted(df.zone_label.unique()))
exp_years = [d.year for d in df.date_expiry if isinstance(d, date)]
keep = df.class_label.isin(f_class) & df.zone_label.isin(f_zone)
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

st.sidebar.divider()
st.sidebar.subheader("Chatbot")
chat_mode = st.sidebar.radio("Mode", ["Hybrid (recommended)", "Rule-based only", "AI only"],
                            help="Hybrid: exact lookups are answered instantly from the data; comparisons, summaries and open questions go to the AI assistant.")
try:
    _secret_key = st.secrets.get("ANTHROPIC_API_KEY", "")
except Exception:
    _secret_key = ""
api_key = _secret_key or os.environ.get("ANTHROPIC_API_KEY", "") or st.sidebar.text_input(
    "Anthropic API key", type="password", help="Or set ANTHROPIC_API_KEY in .streamlit/secrets.toml or the environment.")
llm_model = st.sidebar.selectbox("AI model", LLM_MODELS)
st.sidebar.caption("The AI assistant sends the extracted consent data and your question to Anthropic's API. "
                   "Rule-based answers stay on this machine.")

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
located = fdf[fdf.lat.notna()].copy()
if gmaps_key:
    google_map(located, gmaps_key)
    st.caption("Google Map. Green = active, red = expired. Click a marker for location, status and air quality zone.")
elif located.empty:
    st.info("Add a Google Maps API key in the sidebar to show the map (none of the consents could be placed without it).")
else:
    st.info("No Google Maps API key set, so this is the OpenStreetMap fallback. Add a key in the sidebar for Google Maps.")
    located["air_quality_zone"] = located.air_quality_area.fillna(NOT_FOUND)
    located["site"] = located.site_address.fillna(NOT_FOUND)
    located["status"] = located.status.replace({NOT_FOUND: "Unknown"})
    fig = px.scatter_map(located, lat="lat", lon="lon", color="status", hover_name="consent_id",
                         color_discrete_map={**STATUS_COLOURS, "Unknown": STATUS_COLOURS[NOT_FOUND]},
                         category_orders={"status": ["Expired", "Active", "Unknown"]},
                         hover_data={"site": True, "air_quality_zone": True, "location_basis": True, "lat": False, "lon": False},
                         center=AUCKLAND, zoom=9, height=560)
    fig.update_traces(marker=dict(size=16))
    fig.update_layout(map_style="carto-positron", margin={"r": 0, "t": 0, "l": 0, "b": 0}, legend_title_text="")
    st.plotly_chart(fig, width="stretch")

if st.button("🔄 Retry failed map lookups", type="primary", help="Clears the saved lookup results and tries every address again."):
    st.session_state["loc"] = {}
    st.rerun()
st.caption("Consents by status in the current filter: " + ", ".join(f"{k}: {v}" for k, v in fdf.status.value_counts().items()) +
           " | placed by the server lookup: " + ", ".join(f"{k}: {v}" for k, v in located.status.value_counts().items()) + ".")
unplaced = fdf[fdf.lat.isna()]
if not unplaced.empty:
    with st.expander(f"{len(unplaced)} consent(s) not placed by the server lookup - see why / enter a location", expanded=not gmaps_key):
        st.dataframe(unplaced[["consent_id", "status", "site_address", "location_note"]].fillna(NOT_FOUND), width="stretch", hide_index=True)
        with st.form("manual_loc_form"):
            vals = {cid: st.text_input(cid, value=manual.get(cid, ""), placeholder="Street address, or lat, lon  e.g. -36.93, 174.80")
                    for cid in unplaced.consent_id}
            if st.form_submit_button("Apply"):
                manual.update({k: v.strip() for k, v in vals.items() if v.strip()})
                st.rerun()

t_over, t_reg, t_rules, t_mit, t_cond, t_det, t_dq = st.tabs(
    ["📊 Overview", "📑 Register", "📐 Triggered rules", "🏭 Discharges & mitigation", "📋 Conditions",
     "🗂️ Consent summaries", "🔎 Data quality"])

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
    # ---- distribution of consent terms (1-30 years)
    dur = fdf.dropna(subset=["years_granted"]).copy()
    if dur.empty:
        st.caption("No consent term was found in the memos, so there is no duration distribution.")
    else:
        bands = ["1-5", "6-10", "11-15", "16-20", "21-25", "26-30", "30+"]
        dur["term_band"] = pd.cut(dur.years_granted, [0, 5, 10, 15, 20, 25, 30, float("inf")], labels=bands).astype(str)
        hist = dur.groupby(["term_band", "class_label"]).size().reset_index(name="consents")
        a, b = st.columns(2)
        fig = px.bar(hist, x="term_band", y="consents", color="class_label", text_auto=True, color_discrete_sequence=PALETTE,
                     category_orders={"term_band": bands})
        fig.update_xaxes(title="years granted")
        show_fig(fig, "Distribution of consent terms by activity class", container=a)
        stats = dur.groupby("class_label").years_granted.agg(consents="count", min="min", median="median", mean="mean", max="max").round(1)
        b.markdown("**Consent term (years) by activity class**")
        b.dataframe(stats.reset_index().rename(columns={"class_label": "activity class"}), width="stretch", hide_index=True)
        b.caption("Terms come from the memos. Bands: 1-5, 6-10, ... 26-30 years.")
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

with t_mit:
    st.caption("Discharge types and mitigation measures are grouped with keyword lists (DISCHARGE_TYPES and MITIGATION_MEASURES near the "
               "top of this file - edit them to suit).")
    rr = pd.DataFrame([dict(consent_id=r["consent_id"], years_granted=r["years_granted"], **x) for r in rows_f for x in r["rules"]])
    st.markdown("#### Consented industrial air discharges")
    if rr.empty:
        st.info("No triggered rules were found in the memos, so discharges cannot be categorised.")
    else:
        per_type = rr.groupby("discharge_type").agg(consents=("consent_id", "nunique"), rules=("rule", "count"),
                                                    avg_years_granted=("years_granted", "mean")).round(1).reset_index()
        per_type = per_type.sort_values(["consents", "rules"], ascending=False)
        a, b = st.columns(2)
        fig = px.bar(per_type, x="consents", y="discharge_type", orientation="h", text_auto=True, color_discrete_sequence=[PALETTE[0]])
        fig.update_yaxes(title=None, autorange="reversed")
        show_fig(fig, "Consents by discharge type", container=a)
        by_cls = rr.groupby(["discharge_type", "activity_class"]).size().reset_index(name="rules")
        fig = px.bar(by_cls, x="rules", y="discharge_type", color="activity_class", orientation="h", text_auto=True,
                     color_discrete_sequence=PALETTE)
        fig.update_yaxes(title=None, autorange="reversed")
        show_fig(fig, "Triggered rules by discharge type and activity class", container=b)
        st.dataframe(per_type.rename(columns={"discharge_type": "discharge type", "avg_years_granted": "avg years granted"}),
                     width="stretch", hide_index=True)
        other = rr[rr.discharge_type == OTHER_DISCHARGE]
        if not other.empty:
            with st.expander(f"{len(other)} rule(s) not classified - add keywords for them to DISCHARGE_TYPES"):
                st.dataframe(other[["consent_id", "rule", "group", "description"]].fillna(NOT_FOUND), width="stretch", hide_index=True)

    st.markdown("#### Primary mitigation measures (from the consent conditions)")
    mm = pd.DataFrame([dict(consent_id=r["consent_id"], measure=m, discharge_type=t)
                       for r in rows_f for m in r["mitigation"] for t in (r["discharge_types"] or [NOT_FOUND])])
    if mm.empty:
        st.info("No mitigation measures were recognised in the conditions.")
    else:
        n_cons = len(rows_f)
        cond_n = cc.assign(measure=cc.mitigation.str.split("; ")).explode("measure")
        cond_n = cond_n[cond_n.measure.fillna("") != ""].groupby("measure").size().rename("conditions")
        example = cc.assign(measure=cc.mitigation.str.split("; ")).explode("measure")
        example = example[example.measure.fillna("") != ""].groupby("measure").text.first().str.slice(0, 160).rename("example condition")
        rank = (mm.drop_duplicates(["consent_id", "measure"]).groupby("measure").consent_id
                 .agg(consents="nunique", consent_ids=lambda s: ", ".join(sorted(s))))
        rank = rank.join(cond_n).join(example)
        rank["% of consents"] = (100 * rank.consents / n_cons).round(0)
        rank = rank.sort_values(["consents", "conditions"], ascending=False).reset_index()
        a, b = st.columns(2)
        fig = px.bar(rank, x="consents", y="measure", orientation="h", text_auto=True, color_discrete_sequence=[PALETTE[1]])
        fig.update_yaxes(title=None, autorange="reversed")
        show_fig(fig, f"Consents requiring each measure (of {n_cons})", container=a, height=max(300, 34 * len(rank) + 60))
        heat = mm.drop_duplicates(["consent_id", "measure", "discharge_type"]).pivot_table(
            index="measure", columns="discharge_type", values="consent_id", aggfunc="nunique", fill_value=0)
        fig = px.imshow(heat, text_auto=True, aspect="auto", color_continuous_scale="Teal", labels=dict(color="consents"))
        fig.update_xaxes(title=None, tickangle=-30); fig.update_yaxes(title=None)
        show_fig(fig, "Measures by discharge type", container=b, height=max(300, 34 * len(heat) + 120))
        st.dataframe(rank[["measure", "consents", "% of consents", "conditions", "consent_ids", "example condition"]],
                     width="stretch", hide_index=True)
        st.download_button("Download mitigation ranking CSV", rank.to_csv(index=False).encode(), "mitigation_measures.csv", "text/csv")
        st.caption("Keyword-based: a measure is counted when a condition mentions it, not when its wording is judged.")

with t_det:
    for r, s in zip(fdf.to_dict("records"), fdf.status):
        dot = {"Active": "🟢", "Expired": "🔴"}.get(s, "⚪")
        with st.expander(f"{dot} {r['consent_id']} - {show(r['applicant'])}", expanded=len(fdf) == 1):
            st.markdown(badge(s), unsafe_allow_html=True)
            st.dataframe(summary_table({**r, "status": s}), width="stretch", hide_index=True)
            if r["rules"]:
                st.markdown("**Rules that trigger consent (memo)**")
                st.dataframe(pd.DataFrame(r["rules"]).rename(columns={"rule": "Rule", "group": "Rule group", "description": "Description",
                                                                    "activity_class": "Activity class", "discharge_type": "Discharge type"}).fillna(NOT_FOUND),
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
           "Exact lookups are computed from the extracted data on this machine; comparisons, summaries and open questions "
           "go to the AI assistant (needs an API key in the sidebar), which only sees the extracted data.")
if st.session_state.get("chat_key") != key:
    st.session_state["chat"], st.session_state["chat_key"] = [], key
for who, msg, tbl in st.session_state["chat"]:
    with st.chat_message(who):
        st.markdown(msg)
        if tbl is not None:
            st.dataframe(tbl, width="stretch", hide_index=True)
if q := st.chat_input("Ask about the consents..."):
    with st.spinner("Thinking..."):
        ans, tbl, src = answer(q, df, st.session_state["chat"], chat_mode, api_key, llm_model, as_at)
    ans = f"{ans}\n\n*Answered by: {src}*"
    st.session_state["chat"] += [("user", q, None), ("assistant", ans, tbl)]
    with st.chat_message("user"):
        st.markdown(q)
    with st.chat_message("assistant"):
        st.markdown(ans)
        if tbl is not None:
            st.dataframe(tbl, width="stretch", hide_index=True)
