"""
CCIRS - Stage 6 batch validation ("Backtest" tab). No Streamlit here, so everything is unit-testable.

  default_set()        - healthy / stressed pairs by sector from config.BACKTEST_SET
  score_company()      - one company: default weights, saved analyst decisions (else pending, uncapped),
                         a second score with every pending override confirmed, the pre-fix score (exceptional
                         items not removed), sensitivity summary; a failed load becomes an error result
  direction_check()    - per pair: does the healthy firm score above the stressed one? explains each fail
  spearman()           - rank correlation (average ranks for ties), no scipy needed
  batch_sensitivity()  - rank stability across the +/-10% weight scenarios and pair results that change
  results_table() / export_xlsx() / export_csv()

Weights are never tuned to force a result: every company is scored with the brief's default weights.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

import pandas as pd

import config
import data as D
import ratios as R
import scoring as S


def default_set() -> list[dict]:
    return [dict(c) for c in config.BACKTEST_SET]


def pairs(companies: list[dict]) -> list[tuple[str, dict | None, dict | None]]:
    """(sector, healthy, stressed) in the order sectors first appear."""
    out = []
    for sector in dict.fromkeys(c["sector"] for c in companies):
        h = next((c for c in companies if c["sector"] == sector and c["role"] == "healthy"), None)
        s = next((c for c in companies if c["sector"] == sector and c["role"] == "stressed"), None)
        out.append((sector, h, s))
    return out


@dataclass
class BTResult:
    company: dict
    error: str = ""
    fd: object = None
    book: object = None
    ccirs: object = None                 # saved decisions, else pending (uncapped)
    ccirs_confirmed: object = None       # every pending override confirmed
    ccirs_before: object = None          # exceptional items NOT removed (pre-fix)
    year: str = ""
    year_type: str = ""
    decisions: dict = field(default_factory=dict)
    sens: object = None
    sens_summary: dict = field(default_factory=dict)
    data_quality: list = field(default_factory=list)
    ccirs_alt: object = None             # robustness: Earnings Integrity = 50 when "limited"
    inputs_used: str = "not entered"
    cash_flags: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and self.ccirs is not None and self.ccirs.score is not None


def _pending_labels(r: S.CCIRSResult) -> list[str]:
    out = []
    for p in r.pending:
        if "awaiting" in p:
            out.append(("CFO cap" if p.startswith("CFO") else "Net-worth cap" if "net worth" in p else "Override")
                       + " pending")
    return out


def score_company(c: dict, loader, year: str | None = None, upload: D.FinancialData | None = None,
                  saved: dict | None = None, settings: dict | None = None, inputs: dict | None = None) -> BTResult:
    """saved: {(ticker, year): {"cfo_decision": .., "neq_decision": ..}} - the analyst's override decisions.
    inputs: {ticker: {"manual": {(item, fy): value}, "governance": {...}}} - related-party and governance inputs
    the analyst entered for that company in this session (same as on the company's own page)."""
    try:
        raw = loader(c["ticker"])
        merged = D.merge_sources(raw, upload, "upload") if upload is not None else raw
        own = (inputs or {}).get(c["ticker"], {})
        manual, gov = own.get("manual") or {}, own.get("governance")
        used = [x for x, v in (("governance", gov), ("related-party", manual)) if v]
        fd = D.apply_manual(D.apply_derivations(merged), manual)
        if not fd.years:
            raise D.DataFetchError("no usable years")
        yr = year if year in fd.years else fd.years[-1]
        dec = dict((saved or {}).get((c["ticker"], yr), {}))
        dec.setdefault("cfo_decision", S.CFO_PENDING)
        dec.setdefault("neq_decision", S.NEQ_PENDING)
        book = R.compute_all(fd, settings)
        r = S.score(book, fd, yr, None, gov, **dec)
        conf = {"cfo_decision": S.CFO_CONFIRM if dec["cfo_decision"] == S.CFO_PENDING else dec["cfo_decision"],
                "neq_decision": S.NEQ_CONFIRM if dec["neq_decision"] == S.NEQ_PENDING else dec["neq_decision"]}
        rc = S.score(book, fd, yr, None, gov, **conf)
        ra = S.score(book, fd, yr, None, gov, alt_ei_neutral=True, **dec)
        fd_b = D.apply_manual(D.apply_derivations(merged, exclude_exceptional=False), manual)
        rb = S.score(R.compute_all(fd_b, settings), fd_b, yr, None, gov, **dec)
        sens, summ = (S.sensitivity(book, fd, yr, None, gov, dec["cfo_decision"], neq_decision=dec["neq_decision"])
                      if r.score is not None else (None, {}))
        rep = D.validate(fd, yahoo=raw, upload=upload)
        dq = [m for _, m in D.one_off_checks(fd)] + [f"{i.check} ({i.year})" for i in rep.errors]
        cash = [m for fy, m in D.cash_one_off_checks(fd) if fy == yr]
        return BTResult(c, "", fd, book, r, rc, rb, yr, "current year" if yr == fd.years[-1] else "pre-stress year",
                        dec, sens, summ, dq, ra, ("used: " + ", ".join(used)) if used else "not entered", cash)
    except Exception as e:      # network, bad ticker, empty statements - never stops the batch
        return BTResult(c, f"Could not load {c['ticker']}: {type(e).__name__}"
                           f"{(' - ' + str(e)[:120]) if str(e) else ''}")


def run_batch(companies: list[dict], loader, years: dict | None = None, uploads: dict | None = None,
              saved: dict | None = None, settings: dict | None = None, inputs: dict | None = None) -> list[BTResult]:
    years, uploads = years or {}, uploads or {}
    return [score_company(c, loader, years.get(c["ticker"]), uploads.get(c["ticker"]), saved, settings, inputs)
            for c in companies]


# --------------------------------------------------------------------------- table
def results_table(results: list[BTResult]) -> pd.DataFrame:
    rows = []
    for b in results:
        c = b.company
        base = {"Company": c["name"], "Ticker": c["ticker"], "Sector": c["sector"], "Role": c["role"]}
        if not b.ok:
            rows.append({**base, "Status": b.error or "No score (insufficient data)"})
            continue
        r = b.ccirs
        pill = {p.key: (None if p.score is None else round(p.score, 1)) for p in r.pillars}
        caveats = list(r.caveats) + [f"{p.name} limited" for p in r.pillars if p.limited] + list(b.cash_flags)
        if r.indicative_only:
            caveats.insert(0, "Indicative only")
        pend = _pending_labels(r)
        rows.append({**base, "Status": "OK", "Scoring year": b.year, "Year type": b.year_type,
                     "CCIRS": S.display_score(r.score), "Band": r.band, "Verdict": r.verdict,
                     "A": pill["A"], "B": pill["B"], "C": pill["C"], "D": pill["D"],
                     "Coverage": f"{r.coverage:.0%}", "Analyst inputs": b.inputs_used,
                     "Overrides": "; ".join([o.split(":")[0] + " (applied)" for o in r.overrides] + pend) or "-",
                     "Score basis": "uncapped - overrides pending" if pend else "decisions applied" if r.overrides
                     else "no override",
                     "CCIRS (pending confirmed)": S.display_score(b.ccirs_confirmed.score),
                     "CCIRS before exceptional fix": (S.display_score(b.ccirs_before.score)
                                                      if b.ccirs_before and b.ccirs_before.score is not None else None),
                     "Caveats": "; ".join(caveats) or "-",
                     "Data-quality flags": "; ".join(b.data_quality) or "-",
                     "Data years": f"{len(b.fd.years)} ({b.fd.years[0]}-{b.fd.years[-1]})",
                     "Source per year": " · ".join(f"{fy}: {s}" for fy, s in b.fd.year_sources().items()),
                     "Score range (±10%)": (f"{b.sens_summary['min']:.1f}-{b.sens_summary['max']:.1f}"
                                            if b.sens_summary else "-"),
                     "Verdict flips": len(b.sens_summary.get("flips", [])) if b.sens_summary else None})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- direction check
def _explain(h: BTResult, s: BTResult) -> str:
    """Which pillars and ratios let the stressed firm score at or above the healthy one."""
    parts = []
    for ph, ps in zip(h.ccirs.pillars, s.ccirs.pillars):
        if ph.score is None or ps.score is None:
            parts.append(f"{ph.key}: {'n/a' if ph.score is None else f'{ph.score:.0f}'} vs "
                         f"{'n/a' if ps.score is None else f'{ps.score:.0f}'}")
        elif ps.score > ph.score:
            parts.append(f"{ph.key} ({ph.name}): healthy {ph.score:.0f} vs stressed {ps.score:.0f}")
    hc = {c.key: c for c in h.ccirs.components}
    diffs = []
    for cs in s.ccirs.components:
        ch = hc[cs.key]
        d = cs.contribution - ch.contribution
        if d > 0.05:
            diffs.append((d, f"{cs.label}: stressed {cs.value} (sub-score "
                             f"{'n/a' if cs.sub_score is None else f'{cs.sub_score:.0f}'}, {cs.contribution:.1f} pts) vs "
                             f"healthy {ch.value} ({'n/a' if ch.sub_score is None else f'{ch.sub_score:.0f}'}, "
                             f"{ch.contribution:.1f} pts) [{cs.status if cs.status != S.SCORED else ch.status}]"))
    diffs.sort(reverse=True)
    txt = "Pillars where the stressed firm scores higher: " + ("; ".join(parts) if parts else "none")
    if diffs:
        txt += ". Ratios driving the gap: " + " | ".join(t for _, t in diffs[:3])
    return txt


def direction_check(results: list[BTResult], companies: list[dict] | None = None,
                    use_confirmed: bool = False, use_alt: bool = False) -> pd.DataFrame:
    by = {b.company["ticker"]: b for b in results}
    companies = companies or [b.company for b in results]
    rows = []
    for sector, h, s in pairs(companies):
        hb, sb = (by.get(h["ticker"]) if h else None), (by.get(s["ticker"]) if s else None)
        row = {"Sector": sector, "Healthy": h["name"] if h else "-", "Stressed": s["name"] if s else "-"}
        if not (hb and sb and hb.ok and sb.ok):
            missing = [x["name"] for x, bb in ((h, hb), (s, sb)) if x and not (bb and bb.ok)]
            rows.append({**row, "Result": "n/a", "Explanation": "Missing: " + (", ".join(missing) or "pair incomplete")})
            continue
        pick = (lambda x: x.ccirs_alt) if use_alt else (lambda x: x.ccirs_confirmed) if use_confirmed else \
            (lambda x: x.ccirs)
        hs, ss = pick(hb).score, pick(sb).score
        ok = hs > ss
        rows.append({**row, "Healthy CCIRS": S.display_score(hs), "Stressed CCIRS": S.display_score(ss), "Gap": round(hs - ss, 1),
                     "Result": "PASS" if ok else "FAIL", "Explanation": "-" if ok else _explain(hb, sb)})
    return pd.DataFrame(rows)


def direction_summary(df: pd.DataFrame) -> str:
    tested = df[df["Result"].isin(["PASS", "FAIL"])]
    return f"{(tested['Result'] == 'PASS').sum()} of {len(tested)} pairs pass" + (
        f" ({(df['Result'] == 'n/a').sum()} not testable)" if (df["Result"] == "n/a").any() else "")


def alt_spec_table(results: list[BTResult]) -> pd.DataFrame:
    """Robustness check: default CCIRS beside the alternative with Earnings Integrity = 50 for loss-makers."""
    rows = []
    for b in results:
        if not b.ok:
            continue
        ei = next(p for p in b.ccirs.pillars if p.key == "C")
        rows.append({"Company": b.company["name"], "Role": b.company["role"], "EI limited (loss-maker)": ei.limited,
                     "EI pillar (default)": None if ei.score is None else round(ei.score, 1),
                     "CCIRS (default)": S.display_score(b.ccirs.score), "Band (default)": b.ccirs.band,
                     "CCIRS (alternative)": S.display_score(b.ccirs_alt.score), "Band (alternative)": b.ccirs_alt.band,
                     "Change": round(b.ccirs_alt.score - b.ccirs.score, 1)})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- rank stability
def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    """Spearman rank correlation = Pearson correlation of the ranks (ties get average ranks)."""
    if len(x) != len(y) or len(x) < 2:
        return None
    rx, ry = _ranks(list(x)), _ranks(list(y))
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def batch_sensitivity(results: list[BTResult], companies: list[dict] | None = None) -> dict:
    ok = [b for b in results if b.ok and b.sens is not None]
    if len(ok) < 2:
        return {"scenarios": 0, "spearman_min": None, "spearman_avg": None, "pair_changes": 0, "per_scenario": None,
                "flipping_pairs": {}}
    base = {b.company["ticker"]: b.ccirs.score for b in ok}
    mat = pd.DataFrame({b.company["ticker"]: b.sens.set_index("Weight changed")["CCIRS"] for b in ok})
    companies = companies or [b.company for b in results]
    prs = [(sec, h["ticker"], s["ticker"]) for sec, h, s in pairs(companies)
           if h and s and h["ticker"] in base and s["ticker"] in base]
    base_pairs = {sec: base[h] > base[s] for sec, h, s in prs}
    rows, flipping = [], {}
    tick = list(base)
    for scen, sc in mat.iterrows():
        rho = spearman([base[t] for t in tick], [sc[t] for t in tick])
        changed = [sec for sec, h, s in prs if (sc[h] > sc[s]) != base_pairs[sec]]
        for sec in changed:
            flipping[sec] = flipping.get(sec, 0) + 1
        rows.append({"Scenario": scen, "Spearman vs default ranking": None if rho is None else round(rho, 3),
                     "Pair results changed": ", ".join(changed) or "-"})
    per = pd.DataFrame(rows)
    rhos = per["Spearman vs default ranking"].dropna()
    return {"scenarios": len(per), "spearman_min": rhos.min() if len(rhos) else None,
            "spearman_avg": rhos.mean() if len(rhos) else None,
            "pair_changes": int((per["Pair results changed"] != "-").sum()), "per_scenario": per,
            "flipping_pairs": flipping}


# --------------------------------------------------------------------------- exports
def export_csv(results: list[BTResult]) -> bytes:
    return ("# Backtest - default CCIRS weights (no tuning). " + config.YAHOO_YEARS_NOTE + "\n").encode() + \
        results_table(results).to_csv(index=False).encode()


def export_xlsx(results: list[BTResult], companies: list[dict] | None = None) -> bytes:
    from openpyxl.styles import Alignment, Font, PatternFill
    buf = io.BytesIO()
    sens = batch_sensitivity(results, companies)
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        results_table(results).to_excel(xw, sheet_name="Backtest", index=False)
        direction_check(results, companies).to_excel(xw, sheet_name="Direction check", index=False)
        direction_check(results, companies, use_confirmed=True).to_excel(
            xw, sheet_name="Direction (pending confirmed)", index=False)
        if sens["per_scenario"] is not None:
            sens["per_scenario"].to_excel(xw, sheet_name="Rank stability", index=False)
        alt = alt_spec_table(results)
        if len(alt):
            alt.to_excel(xw, sheet_name="Alt spec (robustness)", index=False)
        pd.DataFrame({"Note": ["Every company is scored with the brief's default weights - no tuning.",
                               "Overrides use the analyst's saved decisions; otherwise they are pending and the "
                               "uncapped score is used. A second column confirms every pending override.",
                               config.YAHOO_YEARS_NOTE,
                               "Screening tool - not proof of manipulation or default. For academic purposes only."]}
                     ).to_excel(xw, sheet_name="Notes", index=False)
        for ws in xw.book.worksheets:
            for cell in ws[1]:
                cell.font = Font(name="Arial", bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F3864")
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            for col in ws.columns:
                width = min(60, max(10, max(len(str(c.value or "")) for c in col) + 2))
                ws.column_dimensions[col[0].column_letter].width = width
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    cell.font = Font(name="Arial", size=10)
            ws.freeze_panes = "B2"
    return buf.getvalue()
