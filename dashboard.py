"""
CCIRS - Stage 4 dashboard helpers (no Streamlit here, so everything is unit-testable).

  dashboard_flags()  - plain-English alerts with numbers, sorted red -> amber -> info
  company_summary()  - one comparable row (CCIRS, band, pillars, key ratios) for the main company or a peer
  peer_rows()        - summaries for peers; a peer that fails to load becomes an error row, never an exception
  decision()         - rule-based recommendation, two-line rationale, covenants, investigate-next checklist
  fig_*()            - plotly figures (transparent backgrounds so they read in light and dark themes)
"""
from __future__ import annotations

import math

import pandas as pd
import plotly.graph_objects as go

import config
import data as D
import ratios as R
import scoring as S

SEV_ORDER = {"red": 0, "amber": 1, "info": 2}
BAND_HEX = {"Green": "#1E8E3E", "Amber": "#E37400", "Orange": "#D35400", "Red": "#C5221F"}
# Categorical slots 1-5 of the dataviz reference palette (validated light + dark; fixed order, never cycled).
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
SERIES = {"CFO": CATEGORICAL[0], "EBITDA": CATEGORICAL[1], "PAT": CATEGORICAL[2]}
GRID = "rgba(128,128,128,0.25)"


# --------------------------------------------------------------------------- red-flag panel
def dashboard_flags(fd: D.FinancialData, book: R.RatioBook, ccirs: S.CCIRSResult | None,
                    flags: list, distress_lines: list[str]) -> list[dict]:
    """[{severity, text}] sorted by severity (red, amber, info), de-duplicated."""
    out: list[tuple[str, str]] = []
    fy = ccirs.year if ccirs is not None else (fd.years[-1] if fd.years else None)

    if ccirs is not None:
        for o in ccirs.overrides:
            out.append(("red", f"Override applied - {o}"))
        for p in ccirs.pending:
            out.append(("amber" if "awaiting" in p else "info",
                        ("Needs your decision (CCIRS score tab) - " if "awaiting" in p else "") + p))
    for line in distress_lines:
        out.append(("red", f"Distress: {line}"))
    pending_text = " ".join(ccirs.pending + ccirs.overrides) if ccirs is not None else ""
    for f in flags:
        if f.code == "cfo_negative" and "CFO negative" in pending_text:
            continue                          # already shown as an override trigger
        out.append((f.severity, f.text))

    if fy:
        cfo = fd.get("cfo", fy)
        if not math.isnan(cfo) and cfo < 0 and "CFO negative" not in pending_text:
            out.append(("amber", f"{fy}: operating cash flow is negative (Rs {cfo:,.0f} cr)."))
        m = book.get("beneish_m", fy)
        if m.ok and m.applicability != "Not valid":
            lo, hi = config.BENEISH_LINEAR
            if m.value > hi:
                out.append(("red", f"{fy}: Beneish M-Score {m.value:.2f} is above {hi} - manipulation pattern; "
                                   f"warrants investigation."))
            elif m.value > lo:
                out.append(("amber", f"{fy}: Beneish M-Score {m.value:.2f} is in the grey zone ({lo} to {hi})."))
            elif m.value > lo - config.MNEAR_BAND:
                out.append(("amber", f"{fy}: Beneish M-Score {m.value:.2f} is near the {lo} threshold."))
        a = book.get("altman", fy)
        if a.ok and a.applicability != "Not valid":
            zone, colour = R.score_zone(book.altman_key, a.value)
            sev = {"red": "red", "amber": "amber"}.get(colour, "info")
            extra = (" " + config.ALTMAN_Z2_ASSET_HEAVY_NOTE
                     if book.altman_key == "altman_z2" and R.is_capital_intensive(fd, fy) else "")
            out.append((sev, f"{fy}: Altman {R.ALTMAN_SHORT[book.altman_key]} = {a.value:.2f} - {zone} zone.{extra}"))
    if ccirs is not None:
        for c in ccirs.caveats:
            out.append(("info", f"Caveat: {c}"))

    seen, rows = set(), []
    for sev, text in out:
        if text not in seen:
            seen.add(text)
            rows.append({"severity": sev, "text": text})
    return sorted(rows, key=lambda r: SEV_ORDER[r["severity"]])


# --------------------------------------------------------------------------- peers
def company_summary(name: str, fd: D.FinancialData, weights: dict | None = None,
                    settings: dict | None = None, governance: dict | None = None,
                    cfo_decision: str = S.CFO_PENDING, neq_decision: str = S.NEQ_PENDING,
                    fy: str | None = None) -> dict:
    """Comparable row: latest year (or fy), current weights, analyst inputs at their defaults for peers."""
    book = R.compute_all(fd, settings)
    fy = fy or fd.years[-1]
    r = S.score(book, fd, fy, weights, governance, cfo_decision, neq_decision)
    pill = {p.key: (None if p.score is None else round(p.score, 0)) for p in r.pillars}
    notes = []
    if r.indicative_only:
        notes.append("Indicative only")
    if R.captive_finance_years(fd):
        notes.append("captive finance")
    eq = fd.get("equity", fy)
    if not math.isnan(eq) and eq < 0:
        notes.append("negative net worth (" + ("cap applied" if neq_decision == S.NEQ_CONFIRM else
                                               "dismissed" if neq_decision == S.NEQ_DISMISS else "pending") + ")")
    if any(p.key == "C" and p.limited for p in r.pillars):
        notes.append("limited Earnings Integrity")
    if not (governance or {}).get("reviewed"):
        notes.append("analyst inputs not reviewed")
    if any("awaiting" in p for p in r.pending):
        notes.append("provisional")
    if r.coverage < config.MIN_COVERAGE_WARN:
        notes.append(f"low coverage {r.coverage:.0%}")
    return {"Company": name, "Year": fy, "CCIRS": S.display_score(r.score),
            "Band": r.band, "Verdict": r.verdict, "A": pill["A"], "B": pill["B"], "C": pill["C"], "D": pill["D"],
            "CCE": book.get("cce", fy).display(), "Interest cover": book.get("interest_coverage", fy).display(),
            "Debt/Equity": book.get("de_total", fy).display(), "Sloan": book.get("sloan", fy).display(),
            "Notes": ", ".join(notes) or "-", "error": ""}


def peer_rows(peers: list[dict], loader, weights=None, settings=None) -> list[dict]:
    """peers: [{name, ticker}]. loader(ticker) -> FinancialData (raw); any exception becomes an error row."""
    rows = []
    for p in peers[: config.MAX_PEERS]:
        try:
            fd = D.apply_derivations(loader(p["ticker"]))
            rows.append(company_summary(p["name"], fd, weights, settings))
        except Exception as e:  # network, bad ticker, empty statements...
            rows.append({"Company": p["name"], "error": f"Could not load {p['ticker']}: {type(e).__name__}"
                                                        f"{(' - ' + str(e)[:120]) if str(e) else ''}"})
    return rows


def peer_table(rows: list[dict]) -> pd.DataFrame:
    ok = [r for r in rows if not r.get("error")]
    cols = ["Company", "Year", "CCIRS", "Band", "Verdict", "A", "B", "C", "D", "CCE", "Interest cover",
            "Debt/Equity", "Sloan", "Notes"]
    return pd.DataFrame(ok, columns=cols)


# --------------------------------------------------------------------------- decision box
def decision(ccirs: S.CCIRSResult, flags: list) -> dict:
    """Rule-based screening aid: recommendation, two-line rationale, covenants, investigate-next list."""
    provisional = any("awaiting" in p for p in ccirs.pending)
    rec = ccirs.verdict + (" (provisional)" if provisional else "")
    scored = [p for p in ccirs.pillars if p.score is not None]
    weakest = min(scored, key=lambda p: p.score) if scored else None
    line1 = (f"CCIRS {S.fmt_score(ccirs.score)}/100 ({ccirs.band}). Weakest pillar: {weakest.name} at {weakest.score:.0f}"
             + (" (limited: " + weakest.note + ")" if weakest and weakest.limited else "") + "."
             if weakest else f"CCIRS {S.fmt_score(ccirs.score)}/100 ({ccirs.band}).")
    serious = [f.text for f in flags if f.severity == "red"] + ccirs.overrides
    line2 = ("Main concerns: " + "; ".join(t if len(t) <= 110 else t[:107] + "..." for t in serious[:2]) + "."
             if serious else "No red flags raised by the automatic checks.")

    covenants = []
    for c in ccirs.components:
        if c.sub_score is not None and c.sub_score < config.COVENANT_TRIGGER_SCORE and c.key in config.COVENANTS:
            covenants.append(config.COVENANTS[c.key])
    codes = {f.code for f in flags}
    if "inventory_build" in codes:
        covenants.append(config.COVENANTS["inventory_build"])
    if codes & {"rp_advances_revenue", "rp_revenue_share", "rp_receivables_share"}:
        covenants.append(config.COVENANTS["rp_advances"])
    if ccirs.band in config.COVENANT_ALWAYS:
        covenants.append(config.COVENANT_ALWAYS[ccirs.band])
    if not covenants:          # nothing weak enough to trigger: tie maintenance covenants to the weakest components
        for d in drivers(ccirs)["weakest"]:
            if d["key"] in config.COVENANTS:
                covenants.append(f"{config.COVENANTS[d['key']]} (weakest area: {d['label']}, {d['value']})")
            if len(covenants) == 2:
                break
        covenants.append("Annual review on audited consolidated accounts.")
    covenants = list(dict.fromkeys(covenants))

    weak = {c.key for c in ccirs.components if c.sub_score is not None and c.sub_score < config.COVENANT_TRIGGER_SCORE}
    priority = {
        0: bool(weak & {"dsri_rec", "wc_days", "ccc_trend"}) or "rp_receivables_share" in codes,
        1: bool(weak & {"sloan", "bs_accrual", "beneish_m"}),
        2: bool(weak & {"cce", "ocf_ratio", "dscr"}),
        3: bool(codes & {"rp_advances_revenue", "rp_revenue_share", "rp_receivables_share"}),
        4: bool(weak & {"cce", "ocf_ratio"}) or "captive_finance" in codes,
        5: any(c.key == "governance" and c.sub_score is None for c in ccirs.components)
           or any("Auditor rule" in o for o in ccirs.overrides),
    }
    investigate = [(item, priority.get(i, False)) for i, item in enumerate(config.INVESTIGATE_NEXT)]
    investigate.sort(key=lambda x: not x[1])
    return {"recommendation": rec, "rationale": [line1, line2], "covenants": covenants,
            "investigate": investigate}


# --------------------------------------------------------------------------- figures
SOURCE_MARK = {D.SRC_UPLOAD: "U", D.SRC_MANUAL: "M"}     # plain Yahoo (and demo) cells carry no marker


def marked_table(fd: D.FinancialData) -> pd.DataFrame:
    """Numbers as text; '(U)' uploaded, '(M)' manual entry, '(D)' derived - plain Yahoo cells carry no marker."""
    table = fd.display_table()
    src = fd.sources.copy()
    src.index = table.index
    out = table.copy().astype(object)
    for r in table.index:
        for c in table.columns:
            v, s = table.at[r, c], str(src.at[r, c] or "")
            if pd.isna(v):
                out.at[r, c] = "data unavailable"
                continue
            mark = "D" if s.endswith(D.DERIVED_SUFFIX) else SOURCE_MARK.get(s, "")
            out.at[r, c] = f"{v:,.1f}" + (f" ({mark})" if mark else "")
    return out


def drivers(ccirs: S.CCIRSResult, n: int = 3) -> dict:
    """Strongest and weakest scored components from the existing breakdown.
    gained = points earned (the breakdown's contribution); lost = points short of a perfect sub-score."""
    pw = {p.key: p.eff_weight for p in ccirs.pillars}
    rows = []
    for c in ccirs.components:
        if c.sub_score is None or c.eff_weight <= 0:
            continue
        share = c.eff_weight / 100 * pw.get(c.pillar, 0) / 100
        rows.append({"key": c.key, "label": c.label, "pillar": f"{c.pillar}. {config.PILLARS[c.pillar]['name']}",
                     "value": str(c.value).replace(" *", "").replace(" ⚠", "").strip(),
                     "band": c.band or f"sub-score {c.sub_score:.0f}", "sub_score": c.sub_score,
                     "gained": c.contribution, "lost": (100 - c.sub_score) * share})
    weak = sorted((r for r in rows if r["sub_score"] < config.WEAK_SUB_SCORE), key=lambda r: -r["lost"])[:n]
    strongest = sorted((r for r in rows if r["sub_score"] >= config.WEAK_SUB_SCORE), key=lambda r: -r["gained"])[:n]
    if weak:
        return {"strongest": strongest, "weakest": weak, "any_weak": True}
    top = {r["key"] for r in strongest}
    smallest = sorted((r for r in rows if r["lost"] > 0.005 and r["key"] not in top), key=lambda r: -r["lost"])[:n]
    return {"strongest": strongest, "weakest": smallest, "any_weak": False}


def band_rule(score: float) -> str:
    """The band rule applied, in words, e.g. 'CCIRS 79.0 is at or above 75 -> Approve'."""
    s = f"CCIRS {S.fmt_score(score)}"
    bands = config.BANDS
    for i, (lower, _band, verdict, _c) in enumerate(bands):
        if score >= lower:
            if i == 0:
                return f"{s} is at or above {lower} -> {verdict}"
            if lower == 0:
                return f"{s} is below {bands[i - 1][0]} -> {verdict}"
            return f"{s} is between {lower} and {bands[i - 1][0]} -> {verdict}"
    return f"{s} -> {bands[-1][2]}"


def screening_recommendation(ccirs: S.CCIRSResult, flags: list) -> dict:
    """Evidence-led version of decision(): band rule, drivers with numbers, overrides, caveats, then actions."""
    dec = decision(ccirs, flags)
    drv = drivers(ccirs)
    evidence = [band_rule(ccirs.score) + "."]
    if ccirs.raw_score is not None and abs(ccirs.raw_score - ccirs.score) > 0.05:
        evidence.append(f"Weighted score before overrides was {ccirs.raw_score:.1f}; the override below set it "
                        f"to {S.fmt_score(ccirs.score)}.")
    elif ccirs.verdict != S.band_for(ccirs.score)[1]:
        evidence.append(f"The verdict is set to '{ccirs.verdict}' by the override below, not by the band.")
    fmt = lambda d, sign, pts: f"{d['label']} {d['value']} ({d['band']}; {sign}{pts:.1f} pts)"
    if drv["strongest"]:
        evidence.append("Supports the score: " + "; ".join(fmt(d, "+", d["gained"]) for d in drv["strongest"][:2]) + ".")
    if drv["any_weak"]:
        evidence.append("Holds it back: " + "; ".join(fmt(d, "-", d["lost"]) for d in drv["weakest"][:3]) + ".")
    elif drv["weakest"]:
        evidence.append("No weak components. Smallest contributions: "
                        + "; ".join(fmt(d, "-", d["lost"]) for d in drv["weakest"][:3]) + ".")
    else:
        evidence.append("No weak components.")
    reds = [f.text for f in flags if f.severity == "red"]
    adverse = ccirs.verdict in config.ADVERSE_VERDICTS
    return {"recommendation": dec["recommendation"], "evidence": evidence,
            "overrides": [f"Applied: {o}" for o in ccirs.overrides] + [f"Pending: {p}" for p in ccirs.pending],
            "caveats": list(ccirs.caveats) + list(ccirs.warnings), "red_flags": reds[:3],
            "red_flags_title": "Red flags behind it" if adverse else "Red flags to address",
            "red_flags_note": "" if adverse else config.RED_FLAGS_NOTE,
            "covenants": dec["covenants"], "investigate": dec["investigate"]}


def _layout(fig: go.Figure, title: str, height: int = 260) -> go.Figure:
    fig.update_layout(title=dict(text=title, x=0, font=dict(size=14)), height=height,
                      margin=dict(l=10, r=10, t=40, b=10), paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h", y=-0.2))
    fig.update_xaxes(showgrid=False, type="category")
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=GRID)
    return fig


def fig_gauge(value: float | None, title: str) -> go.Figure:
    """Pillar gauge 0-100 with the CCIRS band colours. No number is drawn when the pillar has no data."""
    fig = go.Figure(go.Indicator(
        mode="gauge+number" if value is not None else "gauge", value=value if value is not None else 0,
        number=dict(font=dict(size=26), valueformat=".0f"),
        title=dict(text=title, font=dict(size=12)),
        gauge=dict(axis=dict(range=[0, 100], tickvals=[35, 55, 75], tickfont=dict(size=9)),
                   bar=dict(color="#444" if value is not None else "rgba(0,0,0,0)", thickness=0.3),
                   steps=[dict(range=[0, 35], color="#F4C7C3"), dict(range=[35, 55], color="#F9D7B5"),
                          dict(range=[55, 75], color="#FCE8B2"), dict(range=[75, 100], color="#CEEAD6")])))
    if value is None:
        fig.add_annotation(text="n/a", x=0.5, y=0.18, showarrow=False, font=dict(size=20, color="#9AA0A6"))
    fig.update_layout(height=170, margin=dict(l=28, r=28, t=35, b=5), paper_bgcolor="rgba(0,0,0,0)")
    return fig


def fig_cash(fd: D.FinancialData) -> go.Figure:
    fig = go.Figure()
    op = [fd.get("operating_ebitda", y) for y in fd.years]
    use_op = any(not math.isnan(v) for v in op)
    series = {"CFO": [fd.get("cfo", y) for y in fd.years],
              "EBITDA": op if use_op else [fd.get("ebitda", y) for y in fd.years],
              "PAT": [fd.get("pat", y) for y in fd.years]}
    for name, vals in series.items():
        label = "Operating EBITDA" if name == "EBITDA" and use_op else name
        fig.add_trace(go.Scatter(x=fd.years, y=[None if math.isnan(v) else v for v in vals], name=label,
                                 mode="lines+markers", line=dict(color=SERIES[name], width=2), marker=dict(size=8),
                                 hovertemplate=label + " %{x}: Rs %{y:,.0f} cr<extra></extra>"))
    fig.update_layout(hovermode="x unified")
    return _layout(fig, "CFO vs EBITDA vs PAT (Rs crore)")


def fig_dso(book: R.RatioBook) -> go.Figure:
    vals = [book.value("dso", y) for y in book.years]
    fig = go.Figure(go.Bar(x=book.years, y=vals, marker_color=CATEGORICAL[0], marker_line_width=0,
                           hovertemplate="%{x}: %{y:.0f} days<extra></extra>",
                           text=[f"{v:.0f}" if v is not None else "n/a" for v in vals], textposition="outside"))
    return _layout(fig, "Receivable days (DSO)")


def fig_ccirs(by_year: pd.DataFrame) -> go.Figure:
    """Comparable years: joined line coloured by band. Low-coverage years: hollow grey, not joined."""
    fig = go.Figure()
    for lo, hi, colour in ((0, 35, "#C5221F"), (35, 55, "#D35400"), (55, 75, "#E37400"), (75, 100, "#1E8E3E")):
        fig.add_hrect(y0=lo, y1=hi, fillcolor=colour, opacity=0.08, line_width=0)
    comp = by_year[by_year["Comparable"]]
    low = by_year[~by_year["Comparable"] & by_year["CCIRS"].notna()]
    if len(comp):
        fig.add_trace(go.Scatter(x=comp["Year"], y=comp["CCIRS"], mode="lines+markers+text", name="CCIRS",
                                 text=[f"{v:.1f}" for v in comp["CCIRS"]], textposition="top center",
                                 line=dict(color=CATEGORICAL[0], width=2),
                                 marker=dict(size=9, color=[BAND_HEX.get(b, "#888") for b in comp["Band"]]),
                                 hovertemplate="%{x}: %{y:.1f}<extra></extra>"))
    if len(low):
        fig.add_trace(go.Scatter(x=low["Year"], y=low["CCIRS"], mode="markers", name=config.LOW_COVERAGE_LABEL,
                                 marker=dict(size=11, color="rgba(0,0,0,0)", line=dict(color="#9AA0A6", width=2)),
                                 hovertemplate="%{x}: %{y:.1f} - " + config.LOW_COVERAGE_LABEL + "<br>"
                                 + config.LOW_COVERAGE_TOOLTIP.replace(", ", ",<br>", 1) + "<extra></extra>"))
    fig = _layout(fig, "CCIRS by year")
    fig.update_xaxes(categoryorder="array", categoryarray=list(by_year["Year"]))
    fig.update_yaxes(range=[0, 100])
    return fig


def fig_radar(rows: list[dict]) -> go.Figure:
    cats = ["A. Cash & Liquidity", "B. Solvency", "C. Earnings Integrity", "D. Distress & Gov."]
    fig = go.Figure()
    ok = [r for r in rows if not r.get("error")]
    for i, r in enumerate(ok[: len(CATEGORICAL)]):        # slot order = row order (main company first)
        vals = [r[k] for k in "ABCD"]
        fig.add_trace(go.Scatterpolar(r=vals + [vals[0]], theta=cats + [cats[0]], name=r["Company"],
                                      connectgaps=False, fill="toself", opacity=0.45,
                                      line=dict(color=CATEGORICAL[i], width=2), fillcolor=CATEGORICAL[i],
                                      hovertemplate=r["Company"] + " - %{theta}: %{r:.0f}<extra></extra>"))
    fig.update_layout(polar=dict(radialaxis=dict(range=[0, 100], gridcolor=GRID), angularaxis=dict(gridcolor=GRID),
                                 bgcolor="rgba(0,0,0,0)"),
                      height=360, margin=dict(l=30, r=30, t=30, b=30), paper_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", y=-0.1))
    return fig
