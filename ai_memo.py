"""
CCIRS - Stage 5 AI memo layer.

  build_payload()      - JSON payload of COMPUTED figures only, each with a short ID and unit
  SYSTEM_PROMPT / user_prompt() - guardrailed prompt (cite IDs, no new numbers, screening language)
  call_api()           - optional Anthropic API call (client injectable, so tests never hit the network)
  check_memo()         - reliability check: every number vs its cited figure, banned phrases,
                          verdict consistency, required sections; summary badge + highlighted text
  log_call()           - append to logs/ai_calls.csv (API key never logged)
  memo_markdown() / memo_docx() - exports that state the weights used and the reliability result

The memo is a screening aid written from the app's numbers; it is not a credit decision.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config
import data as D
import ratios as R
import scoring as S

UNIT = {"%": "%", "x": "x", "days": "days", "score": "score", "F": "criteria", "Fn": "criteria"}


# =========================================================================== numbers
_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-−–/])"                    # not part of a word, ID, decimal, range or ratio
    r"(?P<neg>[-−–+]|\()?"
    r"(?P<cur>(?:Rs\.?|INR|₹)\s?)?"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<close>\))?"
    r"(?P<unit>\s?(?:%|pp\b|bps\b|x\b|×|days?\b|d\b|crore\b|cr\b|points?\b|pts\b))?"
    r"(?P<cite>\s*\[(?P<id>[A-Z]{1,2}\d{1,3})\])?")


def _mask(text: str) -> str:
    """Blank out non-figure tokens (Ind AS 116, FY25, Z'') keeping offsets."""
    for pat in config.RELIABILITY_IGNORE:
        text = re.sub(pat, lambda m: " " * len(m.group(0)), text)
    return text


def parse_numbers(text: str, mask: bool = True) -> list[dict]:
    """Every number in text: value (signed float), decimals, unit, cited ID, span. Handles Indian and
    international thousands separators, %, x, days, Rs crore, negatives, (brackets) and the unicode minus."""
    src = _mask(text) if mask else text
    out = []
    for m in _NUM_RE.finditer(src):
        raw_num = m.group("num")
        value = float(raw_num.replace(",", ""))
        neg = m.group("neg")
        if neg in ("-", "−", "–") or (neg == "(" and m.group("close")):
            value = -value
        dec = len(raw_num.split(".")[1]) if "." in raw_num else 0
        unit = (m.group("unit") or "").strip().lower()
        line_start = src.rfind("\n", 0, m.start()) + 1
        prefix = src[line_start:m.start()].strip()
        after = src[m.end():m.end() + 2]
        if not m.group("id") and not unit:
            if dec == 0 and 1990 <= value <= 2100:            # a year
                continue
            if prefix in ("", "-", "*") and after[:1] in (".", ")"):   # ordered-list marker "1."
                continue
        out.append({"start": m.start(), "end": m.end(), "text": text[m.start():m.end()].strip(),
                    "value": value, "decimals": dec, "unit": unit, "id": m.group("id"),
                    "signed": bool(neg) and neg != "("})
    return out


def _r(v, nd=2):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), nd)


# =========================================================================== payload
@dataclass
class Payload:
    data: dict
    registry: dict = field(default_factory=dict)    # id -> {"label", "values": [floats], "text"}

    def add(self, fid: str, label: str, values=(), text: str = ""):
        self.registry[fid] = {"label": label, "values": [v for v in values if v is not None], "text": text}


def _display_values(display: str) -> list[float]:
    return [n["value"] for n in parse_numbers(display, mask=False)]


def build_payload(fd: D.FinancialData, book: R.RatioBook, ccirs: S.CCIRSResult, by_year=None,
                  flags: list | None = None, weights: dict | None = None, governance: dict | None = None,
                  decisions: dict | None = None, peers: list | None = None, decision_box: dict | None = None
                  ) -> Payload:
    """Only figures the app computed. Every number has an ID; missing figures are null with their status."""
    fy, meta = ccirs.year, fd.meta
    p = Payload({})
    pdat = p.data
    pdat["company"] = {"name": meta.get("name"), "ticker": meta.get("ticker"), "sector": meta.get("sector"),
                       "industry": meta.get("industry"), "scoring_year": fy, "units": "Rs crore unless stated",
                       "financial_company": bool(meta.get("is_financial")), "data_loaded": meta.get("fetched_at")}
    provisional = any("awaiting" in x for x in ccirs.pending)
    pdat["rule_based_verdict"] = {"band": ccirs.band, "verdict": ccirs.verdict, "provisional": provisional,
                                  "indicative_only": ccirs.indicative_only}
    head = [("S01", "CCIRS score (after overrides)", S.display_score(ccirs.score), "points"),
            ("S02", "Weighted score before overrides", _r(ccirs.raw_score, 1), "points"),
            ("S03", "Data coverage", _r(ccirs.coverage * 100, 0), "%")]
    pdat["headline"] = [{"id": i, "label": l, "value": v, "unit": u} for i, l, v, u in head]
    for i, l, v, _u in head:
        p.add(i, l, [v])
    pdat["weights_used"] = {"description": S.weights_description(weights),
                            "custom": not S.is_default_weights(weights),
                            "pillars_pct": (S.normalise(weights["pillars"]) if weights else
                                            S.default_weights()["pillars"])}
    p.add("W1", "Pillar weights", [_r(v, 0) for v in pdat["weights_used"]["pillars_pct"].values()])

    pdat["pillars"] = []
    for k, ps in enumerate(ccirs.pillars, 1):
        fid = f"P{k}"
        pdat["pillars"].append({"id": fid, "pillar": f"{ps.key}. {ps.name}", "score": _r(ps.score, 1),
                                "weight_pct": _r(ps.eff_weight, 1), "limited": ps.limited, "note": ps.note or None,
                                "status": "scored" if ps.score is not None else "insufficient data"})
        p.add(fid, ps.name, [_r(ps.score, 1), _r(ps.eff_weight, 1)])

    # ratios: CCIRS components first, then the context ratios shown on the Ratios tab
    pdat["ratios"] = []
    n = 0
    for c in ccirs.components:
        n += 1
        fid = f"R{n:02d}"
        vals = _display_values(c.value) if c.value not in ("-",) else []
        pdat["ratios"].append({"id": fid, "name": c.label, "in_ccirs": True, "pillar": c.pillar,
                               "value": c.value if vals else None, "sub_score": _r(c.sub_score, 1),
                               "band": c.band or None, "status": c.status, "note": c.note or None})
        p.add(fid, c.label, vals + [_r(c.sub_score, 1)])
    in_score = {"cce", "ocf_ratio", "dir", "wc_days", "interest_coverage", "de_total", "net_debt_ebitda", "dscr",
                "sloan", "bs_accrual"}
    for _block, keys in config.MAIN_TABLE:
        for key in keys:
            if key in in_score:
                continue
            r = book.get(key, fy)
            n += 1
            fid = f"R{n:02d}"
            disp = r.display()
            vals = _display_values(disp) if r.ok else []
            pdat["ratios"].append({"id": fid, "name": config.MAIN_TABLE_LABELS.get(key, r.name), "in_ccirs": False,
                                   "group": _block, "value": disp if r.ok else None,
                                   "status": "OK" if r.ok else r.status,
                                   "note": (r.interpretation if r.ok else r.reason) or None,
                                   "applicability": r.applicability})
            p.add(fid, r.name, vals)

    pdat["overrides"] = []
    for k, o in enumerate(ccirs.overrides + ccirs.pending, 1):
        status = "applied" if o in ccirs.overrides else ("dismissed" if "dismissed" in o else "pending")
        pdat["overrides"].append({"id": f"O{k:02d}", "status": status, "text": o})
        p.add(f"O{k:02d}", "override", _display_values(o), o)
    pdat["flags"] = []
    for k, f in enumerate(flags or [], 1):
        txt, sev = (f["text"], f["severity"]) if isinstance(f, dict) else (f.text, f.severity)
        pdat["flags"].append({"id": f"F{k:02d}", "severity": sev, "text": txt})
        p.add(f"F{k:02d}", "flag", _display_values(txt), txt)
    caveats = list(ccirs.caveats)
    for ps in ccirs.pillars:
        if ps.limited:
            caveats.append(f"{ps.name} is limited: {ps.note}.")
    if book.altman_key == "altman_z2" and R.is_capital_intensive(fd, fy):
        caveats.append(config.ALTMAN_Z2_ASSET_HEAVY_NOTE)
    if ccirs.indicative_only:
        caveats.append("Indicative only: financial company; ratios tagged Not valid are excluded.")
    for w in ccirs.warnings:
        caveats.append(w)
    pdat["caveats"] = [{"id": f"V{k:02d}", "text": t} for k, t in enumerate(dict.fromkeys(caveats), 1)]
    for c in pdat["caveats"]:
        p.add(c["id"], "caveat", _display_values(c["text"]), c["text"])
    gov = governance or S.default_governance()
    pdat["analyst_inputs"] = {"governance_reviewed": gov.get("reviewed"), "auditor_change": gov.get("auditor_event"),
                              "audit_opinion": gov.get("opinion"), "promoter_pledge_pct": gov.get("pledge_pct"),
                              **(decisions or {})}

    # trends (last 4 years)
    years = fd.years[-4:]
    op = any(not math.isnan(fd.get("operating_ebitda", y)) for y in years)
    series = [("Revenue", "revenue", "Rs crore"), ("PAT", "pat", "Rs crore"),
              ("Operating EBITDA" if op else "EBITDA", "operating_ebitda" if op else "ebitda", "Rs crore"),
              ("CFO", "cfo", "Rs crore")]
    pdat["trends"] = []
    t = 0
    for label, item, unit in series:
        for y in years:
            t += 1
            v = _r(fd.get(item, y), 1)
            pdat["trends"].append({"id": f"T{t:02d}", "metric": label, "year": y, "value": v, "unit": unit})
            p.add(f"T{t:02d}", f"{label} {y}", [v])
    for y in years:
        t += 1
        v = _r(book.value("dso", y), 1)
        pdat["trends"].append({"id": f"T{t:02d}", "metric": "Receivable days (DSO)", "year": y, "value": v,
                               "unit": "days"})
        p.add(f"T{t:02d}", f"DSO {y}", [v])
    pdat["ccirs_by_year"] = []
    if by_year is not None:
        for k, row in enumerate(by_year.to_dict("records"), 1):
            fid = f"Y{k:02d}"
            pdat["ccirs_by_year"].append({"id": fid, "year": row["Year"], "value": _r(row["CCIRS"], 1),
                                          "unit": "points", "comparable": bool(row["Comparable"]),
                                          "note": None if row["Comparable"] else config.LOW_COVERAGE_LABEL})
            p.add(fid, f"CCIRS {row['Year']}", [_r(row["CCIRS"], 1)])
    pdat["peers"] = []
    for k, row in enumerate([r for r in (peers or []) if not r.get("error")], 1):
        fid = f"Q{k}"
        pdat["peers"].append({"id": fid, "company": row["Company"], "year": row["Year"], "ccirs": row["CCIRS"],
                              "band": row["Band"], "pillars": {x: row[x] for x in "ABCD"}, "notes": row["Notes"]})
        p.add(fid, row["Company"], [row["CCIRS"]] + [row[x] for x in "ABCD"])
    if decision_box:
        pdat["suggested_covenants"] = [{"id": f"K{k:02d}", "text": c} for k, c in
                                       enumerate(decision_box["covenants"], 1)]
        for c in pdat["suggested_covenants"]:
            p.add(c["id"], "covenant", _display_values(c["text"]), c["text"])
        pdat["investigate_next"] = [i for i, _ in decision_box["investigate"]]
    return p


# =========================================================================== prompt
def system_prompt() -> str:
    sections = "\n".join(f"## {s}" for s in config.MEMO_SECTIONS)
    banned = ", ".join(f'"{b}"' for b in config.BANNED_PHRASES)
    return f"""You are drafting a credit memo for a credit approver in the corporate banking team of an Indian commercial bank.
The decision is to renew or enhance a working-capital facility. The analyst's screening tool (CCIRS) has computed every figure you need; they are in the JSON payload.

RULES - follow all of them:
1. Use ONLY the numbers in the payload. Do not calculate, estimate, convert or round new figures. Do not bring in outside knowledge about the company.
2. Put the figure ID in square brackets straight after EVERY number you cite, e.g. "CCE of 89.4% [R01]" or "revenue of Rs 1,52,913 crore [T04]". Numbers that appear inside a flag, override, caveat or covenant text are cited with that item's ID (e.g. [F03]).
3. Where a figure is null, marked "insufficient data", "data unavailable", or comes from a year marked "Indicative - limited data", write "insufficient data" instead of a number.
4. Use screening language: "warrants investigation", "is consistent with", "suggests", "could indicate". Never use: {banned}. The tool screens; it does not prove anything.
5. The recommendation MUST be exactly the rule-based verdict in "rule_based_verdict.verdict" (add "(provisional)" if "provisional" is true; add "Indicative only" if "indicative_only" is true). If you believe a different verdict is warranted, keep the verdict unchanged and add one separate line starting "Analyst consideration:".
6. Reason across signals: link related figures (e.g. profit up while CFO falls and receivable days rise) into one coherent risk narrative, rather than listing ratios one by one.
7. About one page. Plain, professional English for a credit approver.

STRUCTURE - use exactly these headings, in this order:
{sections}

Under "## Recommendation" the first line must be: "Recommendation: <verdict>". Then one line stating the CCIRS score, band and the weights used.
"## Executive summary": three lines.
"## Key strengths" and "## Key risks": short bullets; key risks must use cross-signal reasoning.
"## Anomaly explanations": explain flagged years, distress signals and gaps against peers (if peers are supplied).
"## Proposed conditions and covenants": draw on "suggested_covenants".
"## Investigate next": draw on "investigate_next".
"## Data limitations": coverage, indicative years, missing figures, captive-finance or loss-maker caveats.
"""


def user_prompt(payload: Payload) -> str:
    return ("Write the credit memo for the company in this payload. Every figure carries an ID; cite it in "
            "brackets after each number.\n\nPAYLOAD (JSON):\n" + json.dumps(payload.data, indent=1, ensure_ascii=False))


def full_prompt_text(system: str, user: str) -> str:
    return f"=== SYSTEM PROMPT ===\n{system}\n\n=== USER PROMPT ===\n{user}\n"


def payload_hash(system: str, user: str) -> str:
    return hashlib.sha256((system + "\n" + user).encode()).hexdigest()[:16]


# =========================================================================== API mode
def find_api_key(secrets=None) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key and secrets is not None:
        try:
            key = secrets.get("ANTHROPIC_API_KEY")
        except Exception:   # st.secrets raises when no secrets file exists
            key = None
    return key or None


def call_api(system: str, user: str, api_key: str, model: str | None = None, client=None) -> dict:
    """One Messages API call. `client` is injectable (tests pass a fake; no network in tests)."""
    model = model or config.AI_MODEL
    if client is None:
        import anthropic  # imported only in API mode
        client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(model=model, max_tokens=config.AI_MAX_TOKENS, system=system,
                                  messages=[{"role": "user", "content": user}])
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "text") == "text")
    usage = getattr(resp, "usage", None)
    return {"text": text, "model": getattr(resp, "model", model),
            "input_tokens": getattr(usage, "input_tokens", None), "output_tokens": getattr(usage, "output_tokens", None)}


# =========================================================================== reliability check
VERIFIED, MISMATCH, UNCITED, NOT_IN_PAYLOAD = "Verified", "Mismatch", "Uncited", "Not in payload"
SIGN_MISMATCH = "Sign mismatch"
VERDICTS = ["Approve with conditions", "Escalate for forensic review", "Approve", "Decline"]


@dataclass
class CheckResult:
    figures: list
    banned: list
    verdict_ok: bool
    verdict_message: str
    sections_missing: list
    highlighted: str

    @property
    def counts(self) -> dict:
        c = {VERIFIED: 0, MISMATCH: 0, SIGN_MISMATCH: 0, UNCITED: 0, NOT_IN_PAYLOAD: 0}
        for f in self.figures:
            c[f["status"]] += 1
        return c

    @property
    def badge(self) -> str:
        c = self.counts
        parts = [f"{c[VERIFIED]} of {len(self.figures)} figures verified"]
        for k, word in ((MISMATCH, "mismatch"), (SIGN_MISMATCH, "sign mismatch"), (UNCITED, "uncited"),
                        (NOT_IN_PAYLOAD, "not in payload")):
            if c[k]:
                parts.append(f"{c[k]} {word}" + ("es" if word == "mismatch" and c[k] > 1 else ""))
        if self.banned:
            parts.append(f"{len(self.banned)} banned phrase(s)")
        if not self.verdict_ok:
            parts.append("verdict inconsistent")
        if self.sections_missing:
            parts.append(f"{len(self.sections_missing)} section(s) missing")
        return "; ".join(parts)

    @property
    def passed(self) -> bool:
        c = self.counts
        return (c[MISMATCH] == 0 and c[SIGN_MISMATCH] == 0 and c[NOT_IN_PAYLOAD] == 0 and c[UNCITED] == 0
                and not self.banned
                and self.verdict_ok and not self.sections_missing)

    def table(self):
        import pandas as pd
        return pd.DataFrame([{"Text in memo": f["text"], "Cited ID": f["id"] or "-", "Source value": f["source"],
                              "Status": f["status"]} for f in self.figures])


def _sign_word_near(memo: str, start: int, end: int) -> bool:
    """A sign word (negative, outflow, loss, deficit, decline, minus, below zero) within a few words."""
    w = config.SIGN_WORD_WINDOW
    before = " ".join(re.findall(r"[A-Za-z']+", memo[:start])[-w:]).lower()
    after_txt = re.sub(r"\[[A-Z]{1,2}\d{1,3}\]", " ", memo[end:end + 120])
    after = " ".join(re.findall(r"[A-Za-z']+", after_txt)[:w]).lower()
    ctx = f" {before} | {after} "
    return any(re.search(r"\b" + re.escape(sw) + r"\b", ctx) for sw in config.SIGN_WORDS)


def _status(n: dict, values: list[float], memo: str) -> str:
    """Verified / Sign mismatch / Mismatch. An unsigned number may match a NEGATIVE source only when a sign
    word sits within a few words of it ('negative CFO of Rs 45 crore'); otherwise it is a sign mismatch."""
    tol = 0.5 * 10 ** (-n["decimals"]) + 1e-9
    sign_only = False
    for v in values:
        if abs(n["value"] - v) <= tol:
            return VERIFIED
        if not n["signed"] and v < 0 and abs(abs(n["value"]) - abs(v)) <= tol:
            if _sign_word_near(memo, n["start"], n["end"]):
                return VERIFIED
            sign_only = True
    return SIGN_MISMATCH if sign_only else MISMATCH


def verdict_check(memo: str, verdict: str) -> tuple[bool, str]:
    lines = [l for l in memo.splitlines() if re.search(r"recommendation\s*:", l, re.I)
             and not l.lower().lstrip(" -*").startswith("analyst consideration")]
    if not lines:
        return False, "No 'Recommendation:' line found."
    line = lines[0].lower()
    found, rest = [], line
    for v in VERDICTS:                       # longest names first, so 'Approve with conditions' is not 'Approve'
        if v.lower() in rest:
            found.append(v)
            rest = rest.replace(v.lower(), " ")
    if not found:
        short = {"escalate": "Escalate for forensic review"}
        found = [full for word, full in short.items() if word in line]
    if found == [verdict]:
        return True, f"Recommendation matches the rule-based verdict ({verdict})."
    if not found:
        return False, f"The recommendation line does not state a verdict; expected '{verdict}'."
    return False, f"Memo recommends {', '.join(found)} but the rule-based verdict is '{verdict}'."


def banned_check(memo: str) -> list[dict]:
    hits = []
    for phrase in config.BANNED_PHRASES:
        for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", memo, re.I):
            hits.append({"phrase": phrase, "start": m.start(), "end": m.end(), "text": memo[m.start():m.end()]})
    return hits


def sections_check(memo: str) -> list[str]:
    heads = [l.strip().lstrip("#*").strip().rstrip(":*").strip().lower() for l in memo.splitlines()
             if l.strip().startswith(("#", "**"))]
    return [s for s in config.MEMO_SECTIONS if not any(h.startswith(s.lower()) for h in heads)]


def check_memo(memo: str, payload: Payload, verdict: str) -> CheckResult:
    figures = []
    for n in parse_numbers(memo):
        entry = payload.registry.get(n["id"]) if n["id"] else None
        if not n["id"]:
            status, source = UNCITED, "-"
        elif entry is None:
            status, source = NOT_IN_PAYLOAD, "-"
        else:
            source = ", ".join(f"{v:g}" for v in entry["values"]) or "no value (insufficient data)"
            status = _status(n, entry["values"], memo)
        figures.append({**n, "status": status, "source": source})
    banned = banned_check(memo)
    ok, msg = verdict_check(memo, verdict)
    missing = sections_check(memo)
    spans = [(f["start"], f["end"], f["status"]) for f in figures if f["status"] != VERIFIED]
    spans += [(b["start"], b["end"], "Banned phrase") for b in banned]
    out = html.escape(memo)
    # rebuild with marks from the original string (escape segment by segment so offsets stay right)
    pieces, pos = [], 0
    for s, e, why in sorted(spans):
        if s < pos:
            continue
        pieces.append(html.escape(memo[pos:s]))
        pieces.append(f"<mark title='{why}' style='background:#FAD2CF;color:#111;padding:0 2px'>"
                      f"{html.escape(memo[s:e])}</mark>")
        pos = e
    pieces.append(html.escape(memo[pos:]))
    out = "".join(pieces)
    return CheckResult(figures, banned, ok, msg, missing, out)


# =========================================================================== log
LOG_FIELDS = ["timestamp", "company", "ticker", "scoring_year", "mode", "model", "system_prompt", "user_prompt",
              "response", "input_tokens", "output_tokens", "weights_used", "reliability_summary", "payload_hash"]
_SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def log_path() -> Path:
    p = os.environ.get("CCIRS_AI_LOG")
    return Path(p) if p else Path(__file__).resolve().parent / config.AI_LOG_PATH


def _is_duplicate(row: dict, other: dict) -> bool:
    """Same payload + same response, or same payload saved again within LOG_DEDUPE_SECONDS."""
    same_payload = row.get("payload_hash") and row.get("payload_hash") == other.get("payload_hash")
    same_call = (row.get("company"), row.get("scoring_year"), row.get("mode")) == \
                (other.get("company"), other.get("scoring_year"), other.get("mode"))
    if (same_payload or same_call) and row.get("response") == other.get("response"):
        return True
    try:
        gap = abs((datetime.fromisoformat(row["timestamp"]) - datetime.fromisoformat(other["timestamp"])).total_seconds())
    except (KeyError, ValueError, TypeError):
        return False
    return bool(same_payload) and gap <= config.LOG_DEDUPE_SECONDS


def read_log(path: Path | None = None) -> list[dict]:
    path = path or log_path()
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def dedupe_log(path: Path | None = None) -> int:
    """Remove duplicate rows from an existing log (keeps the first). Returns how many were removed."""
    path = path or log_path()
    rows = read_log(path)
    kept: list[dict] = []
    for r in rows:
        if not any(_is_duplicate(r, k) for k in kept):
            kept.append(r)
    removed = len(rows) - len(kept)
    if removed:
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=LOG_FIELDS, quoting=csv.QUOTE_ALL, extrasaction="ignore")
            w.writeheader()
            w.writerows(kept)
    return removed


def log_call(record: dict, path: Path | None = None) -> bool:
    """Append one AI call unless it duplicates an earlier one (same payload and response, or a repeat
    'Check and save' within LOG_DEDUPE_SECONDS). Unknown keys are ignored; API keys are redacted.
    Returns True if a row was written."""
    path = path or log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {k: _SECRET.sub("[redacted]", str(record.get(k, "") if record.get(k) is not None else "")) for k in LOG_FIELDS}
    row["timestamp"] = row["timestamp"] or datetime.now().isoformat(timespec="seconds")
    if any(_is_duplicate(row, old) for old in read_log(path)[-50:]):
        return False
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS, quoting=csv.QUOTE_ALL)
        if new:
            w.writeheader()
        w.writerow(row)
    return True


# =========================================================================== exports
def _header_lines(meta: dict, fy: str, ccirs: S.CCIRSResult, weights_desc: str, check: CheckResult | None,
                  model: str) -> list[str]:
    return [f"Company: {meta.get('name')} ({meta.get('ticker')}) - scoring year {fy}",
            f"CCIRS: {S.fmt_score(ccirs.score)} / 100 - {ccirs.band} - {ccirs.verdict}"
            + (" - Indicative only" if ccirs.indicative_only else ""),
            f"Weights used: {weights_desc}",
            f"Drafted by: {model} on {datetime.now():%d %b %Y %H:%M}",
            f"AI reliability check: {check.badge if check else 'not run'}",
            "Screening aid for the credit analyst - not a credit decision, not proof of manipulation or default; "
            "for academic purposes only."]


def memo_markdown(memo: str, meta: dict, fy: str, ccirs, weights_desc: str, check=None, model: str = "") -> str:
    head = "\n".join(f"- {l}" for l in _header_lines(meta, fy, ccirs, weights_desc, check, model))
    return f"# Credit memo - {meta.get('name')}\n\n{head}\n\n---\n\n{memo.strip()}\n"


def memo_docx(memo: str, meta: dict, fy: str, ccirs, weights_desc: str, check=None, model: str = "") -> bytes:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(11)
    doc.add_heading(f"Credit memo - {meta.get('name')}", level=0)
    for line in _header_lines(meta, fy, ccirs, weights_desc, check, model):
        para = doc.add_paragraph(style="List Bullet")
        para.add_run(line).font.size = Pt(9)

    def add_runs(par, text):
        for i, part in enumerate(re.split(r"\*\*", text)):
            if part:
                par.add_run(part).bold = bool(i % 2)

    for raw in memo.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            level = min(len(line) - len(line.lstrip("#")), 3)
            doc.add_heading(line.lstrip("#").strip(), level=max(1, level - 1) if level > 1 else 1)
        elif re.match(r"^\s*[-*•]\s+", line):
            add_runs(doc.add_paragraph(style="List Bullet"), re.sub(r"^\s*[-*•]\s+", "", line))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            add_runs(doc.add_paragraph(style="List Number"), re.sub(r"^\s*\d+[.)]\s+", "", line))
        else:
            add_runs(doc.add_paragraph(), line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
