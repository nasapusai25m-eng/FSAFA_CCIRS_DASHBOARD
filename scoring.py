"""
CCIRS - Stage 3 scoring engine: Credit Cash-Integrity & Resilience Score (0-100).

Pipeline for one fiscal year
  1. Each component ratio -> 0-100 sub-score from config.SCORE_THRESHOLDS (step bands), except
     Beneish (linear between -2.22 and -1.78) and Piotroski (passed / assessable x 100).
  2. Unavailable ratios are dropped and the remaining sub-weights in that pillar are re-weighted.
     "Not meaningful" caused by distress scores 0 (never dropped); "not meaningful" because there is no
     debt to service scores 100; other "not meaningful" results are dropped like unavailable ones.
  3. Ratios tagged "Not valid" (banks/NBFCs/insurers) are excluded; the score is then "Indicative only".
  4. Pillar score = weighted mean of its sub-scores; CCIRS = weighted mean of available pillars.
  5. Overrides: CFO negative 2 consecutive years -> cap 40 only if the analyst confirms; auditor event
     (stated disagreement, qualified/adverse/disclaimer opinion, missing report) -> cap 50;
     related-party advances > 50% of revenue -> verdict forced to Escalate (Decline stays Decline).
  6. Band and verdict from the final score.
Scores are screening tools, not proof of manipulation or default.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

import pandas as pd

import config
import ratios as R
from data import FinancialData, fy_label_from_year, fy_to_year

SCORED = "scored"
REWEIGHTED = "data unavailable - re-weighted"
NM_DROPPED = "not meaningful - re-weighted"
DISTRESS_ZERO = "distress - scored 0"
NM_GOOD = "no debt service - scored 100"
EXCLUDED = "not valid for this company type - excluded"

CFO_PENDING, CFO_CONFIRM, CFO_DISMISS = "Pending", "Confirm (apply cap)", "Dismiss (e.g. captive finance)"
NEQ_PENDING, NEQ_CONFIRM, NEQ_DISMISS = "Pending", "Confirm (cap at 34)", "Dismiss"


# --------------------------------------------------------------------------- weights
def default_weights() -> dict:
    return {"pillars": {p: v["weight"] for p, v in config.PILLARS.items()},
            "ratios": {p: dict(v["ratios"]) for p, v in config.PILLARS.items()}}


def is_default_weights(weights: dict | None) -> bool:
    return weights is None or weights == default_weights()


def weights_description(weights: dict | None) -> str:
    """One line stating which weights were used - for exports, the memo and the prompt log."""
    if is_default_weights(weights):
        p = default_weights()["pillars"]
        return "Default CCIRS weights (brief): " + "/".join(str(p[k]) for k in "ABCD") + " for pillars A/B/C/D"
    pw = normalise(weights["pillars"])
    subs = "; ".join(f"{p}: " + ", ".join(f"{k} {v:.0f}" for k, v in normalise(weights["ratios"][p]).items())
                     for p in weights["ratios"])
    return (f"{config.CUSTOM_WEIGHTS_LABEL}. Pillars A/B/C/D = " + "/".join(f"{pw[k]:.0f}" for k in "ABCD")
            + f". Sub-weights - {subs}")


def normalise(d: dict) -> dict:
    total = sum(max(v, 0) for v in d.values())
    if total <= 0:
        raise ValueError("Weights sum to zero - set at least one weight above 0.")
    return {k: max(v, 0) / total * 100 for k, v in d.items()}


def rebalance(values: dict, changed: str, new_value: float) -> dict:
    """Linked sliders: set `changed` to new_value (clipped 0-100, whole number) and rescale the others
    proportionally so the group sums to exactly 100. If the others are all zero, the remainder is split
    equally. Rounding difference goes to the largest of the others."""
    new = int(round(min(max(float(new_value), 0.0), 100.0)))
    others = [k for k in values if k != changed]
    if not others:
        return {changed: 100}
    remainder = 100 - new
    base = {k: max(float(values[k]), 0.0) for k in others}
    tot = sum(base.values())
    raw = ({k: remainder / len(others) for k in others} if tot <= 0
           else {k: base[k] / tot * remainder for k in others})
    out = {k: int(round(v)) for k, v in raw.items()}
    diff = remainder - sum(out.values())
    largest = max(others, key=lambda k: raw[k])          # first one wins ties (stable order)
    out[largest] += diff
    # safety: never below 0 (can only happen with pathological rounding); push excess to the next largest
    for k in sorted(others, key=lambda k: -raw[k]):
        if out[k] < 0:
            short, out[k] = -out[k], 0
            for j in sorted(others, key=lambda k: -out[k]):
                take = min(short, out[j])
                out[j] -= take
                short -= take
                if short == 0:
                    break
    out[changed] = new
    return {k: out[k] for k in values}


def to_whole_100(values: dict) -> dict:
    """Integer weights summing to exactly 100 (largest-remainder method); equal split if all zero."""
    keys = list(values)
    tot = sum(max(float(v), 0.0) for v in values.values())
    raw = ({k: 100 / len(keys) for k in keys} if tot <= 0 else {k: max(float(values[k]), 0.0) / tot * 100 for k in keys})
    out = {k: int(math.floor(v)) for k, v in raw.items()}
    for k in sorted(keys, key=lambda k: raw[k] - out[k], reverse=True)[: 100 - sum(out.values())]:
        out[k] += 1
    return out


def default_governance() -> dict:
    return {"reviewed": False, "auditor_event": "None", "opinion": "Unmodified (clean)", "pledge_pct": 0.0}


# --------------------------------------------------------------------------- results
@dataclass
class ComponentScore:
    pillar: str
    key: str
    label: str
    value: str                 # display text
    sub_score: float | None
    band: str
    base_weight: float         # normalised within pillar (before re-weighting)
    eff_weight: float = 0.0    # % within pillar after re-weighting
    contribution: float = 0.0  # points of CCIRS
    status: str = SCORED
    note: str = ""


@dataclass
class PillarScore:
    key: str
    name: str
    base_weight: float
    eff_weight: float
    score: float | None
    coverage: float            # share of the pillar's base sub-weight that had a usable score
    limited: bool = False      # e.g. Earnings Integrity for loss-makers
    note: str = ""


@dataclass
class CCIRSResult:
    year: str
    score: float | None
    raw_score: float | None
    band: str
    verdict: str
    colour: str
    pillars: list
    components: list
    overrides: list = field(default_factory=list)     # applied
    pending: list = field(default_factory=list)       # need analyst action / not applied
    warnings: list = field(default_factory=list)
    indicative_only: bool = False
    coverage: float = 0.0
    caveats: list = field(default_factory=list)       # shown under the score; never change it

    @property
    def label(self) -> str:
        return "Indicative only" if self.indicative_only else ""

    def breakdown_csv(self, weights: dict | None) -> bytes:
        """Breakdown export that states the weights used on its first line."""
        return (f"# {weights_description(weights)}\n".encode() + self.breakdown_frame().to_csv(index=False).encode())

    def breakdown_frame(self) -> pd.DataFrame:
        rows = []
        pw = {p.key: p for p in self.pillars}
        for c in self.components:
            rows.append({"Pillar": f"{c.pillar}. {config.PILLARS[c.pillar]['name']}",
                         "Component": c.label, "Value": c.value,
                         "Sub-score (0-100)": None if c.sub_score is None else round(c.sub_score, 1),
                         "Band": c.band, "Weight in pillar %": round(c.eff_weight, 1),
                         "Pillar weight %": round(pw[c.pillar].eff_weight, 1),
                         "Contribution (points)": round(c.contribution, 2), "Status": c.status, "Note": c.note})
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------- helpers
def band_for(score: float) -> tuple[str, str, str]:
    for lower, band, verdict, colour in config.BANDS:
        if score >= lower:
            return band, verdict, colour
    return config.BANDS[-1][1:]


def display_score(score: float | None) -> float | None:
    """CCIRS for display: one decimal, never contradicting its band. The band and verdict always come from the
    unrounded score; if rounding would push the shown value up into the next band (74.96 -> 75.0 while the band is
    Amber), the value is truncated instead (74.9)."""
    if score is None:
        return None
    shown = round(score, 1)
    if band_for(shown)[0] != band_for(score)[0]:
        shown = math.floor(score * 10) / 10
    return shown


def fmt_score(score: float | None) -> str:
    return "n/a" if score is None else f"{display_score(score):.1f}"


def component_label(key: str, book: R.RatioBook | None = None) -> str:
    """Component label; the Altman component names the variant actually used for this company."""
    if key == "altman" and book is not None:
        return config.ALTMAN_LABEL.get(book.altman_key, config.COMPONENT_LABELS[key])
    return config.COMPONENT_LABELS[key]


def beneish_score(m: float) -> float:
    lo, hi = config.BENEISH_LINEAR
    if m < lo:
        return 100.0
    if m > hi:
        return 0.0
    return 100.0 * (hi - m) / (hi - lo)


def _band(key: str, value: float) -> tuple[float, str]:
    s, label = R.threshold_band(key, value)
    return float(s), label


def _from_ratio(book: R.RatioBook, key: str, fy: str, thr_key: str | None = None):
    """(sub_score, band, display, status, note) for a plain ratio."""
    r = book.get(key, fy)
    if r.ok:
        s, label = _band(thr_key or key, r.value)
        return s, label, r.display(), SCORED, ""
    if r.status == R.NM and r.distress:
        return float(config.DISTRESS_NM_SCORE), "distress", r.display(), DISTRESS_ZERO, r.reason
    if r.status == R.NM and key in config.NM_GOOD_SCORE:
        return float(config.NM_GOOD_SCORE[key]), "no debt service", r.display(), NM_GOOD, r.reason
    if r.status == R.NM:
        return None, "", r.display(), NM_DROPPED, r.reason
    return None, "", r.display(), REWEIGHTED, r.reason


def governance_score(gov: dict) -> tuple[float | None, str, str]:
    if not gov.get("reviewed"):
        return None, "", "Governance inputs not entered by the analyst."
    a = config.AUDITOR_EVENTS[gov["auditor_event"]]
    o = config.AUDIT_OPINIONS[gov["opinion"]]
    pledge = float(gov.get("pledge_pct") or 0)
    p = next(s for upper, s in config.PLEDGE_BANDS if pledge < upper)
    score = min(a, o, p)
    worst = min((a, "auditor event"), (o, "audit opinion"), (p, "promoter pledge"))[1]
    return float(score), f"driven by {worst}", (f"Auditor: {gov['auditor_event']}; opinion: {gov['opinion']}; "
                                                 f"promoter pledge {pledge:.0f}%.")


def _component(book: R.RatioBook, fd: FinancialData, key: str, fy: str, gov: dict):
    """(sub_score, band, display, status, note) for any CCIRS component."""
    if key in ("cce", "ocf_ratio", "dir", "wc_days", "interest_coverage", "de_total", "net_debt_ebitda", "dscr",
               "sloan", "bs_accrual"):
        return _from_ratio(book, key, fy)
    if key == "rp_advances":
        return _from_ratio(book, "rp_advances_revenue", fy)
    if key == "beneish_m":
        r = book.get("beneish_m", fy)
        if r.ok:
            s = beneish_score(r.value)
            return s, R.score_zone("beneish_m", r.value)[0].lower(), r.display(), SCORED, "Sliding scale -2.22 to -1.78."
        return None, "", "M-Score unavailable", REWEIGHTED, r.reason
    if key == "altman":
        r = book.get("altman", fy)
        if r.ok:
            s, label = _band(book.altman_key, r.value)
            return s, label, r.display(), SCORED, f"Variant: {R.ALTMAN_SHORT[book.altman_key]}."
        return _from_ratio(book, "altman", fy)
    if key == "piotroski":
        r = book.get("piotroski", fy)
        if r.ok and r.extra:
            e = r.extra
            return (e["score_0_100"], R.status(r)[0], f"{e['passed']} of {e['assessable']} assessable", SCORED,
                    "Passed / assessable x 100; missing criteria not counted as failures.")
        return None, "", r.display(), REWEIGHTED, r.reason
    if key == "dsri_rec":
        parts = []
        for k in ("dsri", "rec_vs_sales"):
            s, label, disp, st, note = _from_ratio(book, k, fy)
            if s is not None:
                parts.append((k, s, disp))
        if not parts:
            return None, "", "data unavailable", REWEIGHTED, "Neither DSRI nor receivables-vs-sales available."
        s = sum(p[1] for p in parts) / len(parts)
        disp = " / ".join(f"{'DSRI' if k == 'dsri' else 'gap'} {d}" for k, _, d in parts)
        return s, "", disp, SCORED, "Average of the available DSRI and receivables-vs-sales sub-scores."
    if key == "ccc_trend":
        cur = book.get("ccc", fy)
        p = fy_label_from_year(fy_to_year(fy) - 1)
        prev = book.get("ccc", p) if p in book.years else None
        if cur.ok and prev is not None and prev.ok:
            change = cur.value - prev.value
            s, label = _band("ccc_trend", change)
            return s, label, f"{prev.value:,.0f} -> {cur.value:,.0f} d ({change:+,.0f})", SCORED, ""
        why = cur.reason if not cur.ok else "CCC for the prior year is not available."
        return None, "", "data unavailable", REWEIGHTED, why
    if key == "governance":
        s, label, note = governance_score(gov)
        return s, label, ("entered" if s is not None else "not entered"), (SCORED if s is not None else REWEIGHTED), note
    raise KeyError(key)


def cfo_negative_run(fd: FinancialData, fy: str, n: int | None = None) -> list[str]:
    """Years ending at fy with CFO < 0, if the run is at least n long (default config)."""
    n = n or config.CFO_NEGATIVE_YEARS
    run = []
    y = fy_to_year(fy)
    while True:
        lab = fy_label_from_year(y)
        if lab not in fd.years:
            break
        v = fd.get("cfo", lab)
        if math.isnan(v) or v >= 0:
            break
        run.insert(0, lab)
        y -= 1
    return run if len(run) >= n else []


# --------------------------------------------------------------------------- main
def score(book: R.RatioBook, fd: FinancialData, fy: str, weights: dict | None = None,
          governance: dict | None = None, cfo_decision: str = CFO_PENDING,
          neq_decision: str = NEQ_PENDING, alt_ei_neutral: bool = False) -> CCIRSResult:
    weights = weights or default_weights()
    gov = governance or default_governance()
    pw_base = normalise(weights["pillars"])
    comps: list[ComponentScore] = []
    pillars: list[PillarScore] = []
    warnings: list[str] = []

    for p, spec in config.PILLARS.items():
        sub_base = normalise(weights["ratios"][p])
        items = []
        for key in spec["ratios"]:
            app_key = config.COMPONENT_APPLICABILITY_KEY.get(key, key)
            app = R.applicability(app_key, book.is_financial)[0] if key != "governance" else "Valid"
            if app == "Not valid":
                items.append(ComponentScore(p, key, component_label(key, book), "-", None, "", sub_base[key],
                                            status=EXCLUDED, note=R.applicability(app_key, True)[1]))
                continue
            s, band, disp, st, note = _component(book, fd, key, fy, gov)
            items.append(ComponentScore(p, key, component_label(key, book), disp, s, band, sub_base[key],
                                        status=st, note=note))
        usable = [c for c in items if c.sub_score is not None and sub_base[c.key] > 0]
        tot = sum(sub_base[c.key] for c in usable)
        for c in usable:
            c.eff_weight = sub_base[c.key] / tot * 100
        pscore = sum(c.sub_score * c.eff_weight / 100 for c in usable) if usable else None
        pillars.append(PillarScore(p, spec["name"], pw_base[p], 0.0, pscore, tot / 100))
        comps += items

    pat, eq = fd.get("pat", fy), fd.get("equity", fy)
    if (not math.isnan(pat) and pat < 0) or (not math.isnan(eq) and eq < 0):
        ei = next(ps for ps in pillars if ps.key == "C")
        ei.limited, ei.note = True, config.LOSS_MAKER_EI_NOTE
        if alt_ei_neutral and ei.score is not None:     # robustness check only - never the default CCIRS
            ei.score = float(config.ALT_EI_NEUTRAL_SCORE)
            ei.note += f" (alternative specification: set to {config.ALT_EI_NEUTRAL_SCORE})"

    avail = [ps for ps in pillars if ps.score is not None and pw_base[ps.key] > 0]
    if not avail:
        return CCIRSResult(fy, None, None, "-", "Insufficient data", "#5F6368", pillars, comps,
                           warnings=["No pillar has enough data to score."], indicative_only=book.is_financial)
    ptot = sum(pw_base[ps.key] for ps in avail)
    for ps in avail:
        ps.eff_weight = pw_base[ps.key] / ptot * 100
    for ps in pillars:
        if ps.score is None:
            warnings.append(f"Pillar {ps.key} ({ps.name}) has no usable ratios - its weight was spread over the "
                            f"other pillars.")
    for c in comps:
        ps = next(x for x in pillars if x.key == c.pillar)
        c.contribution = (c.sub_score * c.eff_weight / 100 * ps.eff_weight / 100) if c.sub_score is not None \
            and ps.score is not None else 0.0
    raw = sum(ps.score * ps.eff_weight / 100 for ps in avail)
    coverage = sum(pw_base[ps.key] / 100 * ps.coverage for ps in pillars)
    if coverage < config.MIN_COVERAGE_WARN:
        warnings.append(f"Low data coverage: only {coverage:.0%} of the scoring weight had usable data. "
                        f"Treat the score with caution.")
    if any(c.key == "beneish_m" and c.sub_score is None and c.status != EXCLUDED for c in comps):
        warnings.append("M-Score unavailable: Earnings Integrity re-weighted across the remaining ratios.")

    # ---------------- overrides
    final, overrides, pending = raw, [], []
    run = cfo_negative_run(fd, fy)
    if run:
        txt = f"CFO negative in {run[0]}-{run[-1]} ({len(run)} consecutive years)"
        if cfo_decision == CFO_CONFIRM:
            if final > config.CFO_NEGATIVE_CAP:
                final = config.CFO_NEGATIVE_CAP
            overrides.append(f"{txt}: confirmed by analyst - score capped at {config.CFO_NEGATIVE_CAP}.")
        elif cfo_decision == CFO_DISMISS:
            pending.append(f"{txt}: dismissed by analyst - cap not applied.")
        else:
            pending.append(f"{txt}: awaiting analyst decision - cap of {config.CFO_NEGATIVE_CAP} NOT applied yet "
                           f"(verdict is provisional).")
    if not math.isnan(eq) and eq < 0:
        txt = f"Negative net worth in {fy} (shareholders' equity Rs {eq:,.0f} cr)"
        if neq_decision == NEQ_CONFIRM:
            if final > config.NEG_EQUITY_CAP:
                final = config.NEG_EQUITY_CAP
            overrides.append(f"{txt}: confirmed by analyst - score capped at {config.NEG_EQUITY_CAP} (Red, Decline).")
        elif neq_decision == NEQ_DISMISS:
            pending.append(f"{txt}: dismissed by analyst - cap not applied.")
        else:
            pending.append(f"{txt}: awaiting analyst decision - cap of {config.NEG_EQUITY_CAP} NOT applied yet "
                           f"(verdict is provisional).")
    if gov.get("reviewed") and (gov["auditor_event"] in config.AUDITOR_CAP_EVENTS
                                or gov["opinion"] in config.AUDITOR_CAP_OPINIONS):
        why = gov["auditor_event"] if gov["auditor_event"] in config.AUDITOR_CAP_EVENTS else gov["opinion"]
        if final > config.AUDITOR_CAP:
            final = config.AUDITOR_CAP
        overrides.append(f"Auditor rule ({why}): score capped at {config.AUDITOR_CAP}.")
    band, verdict, colour = band_for(final)
    rp = book.value("rp_advances_revenue", fy)
    if rp is not None and rp > config.RP_ESCALATE_THRESHOLD:
        if verdict in ("Approve", "Approve with conditions"):
            band, verdict, colour = "Orange", "Escalate for forensic review", "#D35400"
            overrides.append(f"Related-party advances = {rp:.0%} of revenue (> {config.RP_ESCALATE_THRESHOLD:.0%}): "
                             f"verdict forced to Escalate.")
        else:
            overrides.append(f"Related-party advances = {rp:.0%} of revenue: Escalate rule triggered; the verdict is "
                             f"already {verdict}.")
    if book.is_financial:
        warnings.insert(0, "Financial company: ratios tagged Not valid are excluded. Indicative only - not "
                           "suitable for credit decisions.")
    caveats = [config.CAPTIVE_CAVEAT] if R.captive_finance_years(fd) else []
    if R.is_regulated_utility(fd.meta):
        caveats.append(config.UTILITY_CAVEAT)
    return CCIRSResult(fy, final, raw, band, verdict, colour, pillars, comps, overrides, pending, warnings,
                       book.is_financial, coverage, caveats)


def score_all_years(book, fd, weights=None, governance=None, cfo_decision=CFO_PENDING,
                    neq_decision=NEQ_PENDING) -> pd.DataFrame:
    """One row per year. Years with coverage below config.LOW_COVERAGE_YEAR are marked not comparable:
    no band or verdict is shown for them (typically the earliest year, which lacks prior-year comparators)."""
    rows = []
    for fy in fd.years:
        r = score(book, fd, fy, weights, governance, cfo_decision, neq_decision)
        comparable = r.score is not None and r.coverage >= config.LOW_COVERAGE_YEAR
        rows.append({"Year": fy, "CCIRS": display_score(r.score),
                     "Band": r.band if comparable else config.LOW_COVERAGE_LABEL,
                     "Verdict": r.verdict if comparable else "-", "Coverage": f"{r.coverage:.0%}",
                     "Comparable": comparable, "Overrides": "; ".join(r.overrides) or "-"})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- sensitivity
def sensitivity(book, fd, fy, weights=None, governance=None, cfo_decision=CFO_PENDING,
                step: float | None = None, neq_decision=NEQ_PENDING) -> tuple[pd.DataFrame, dict]:
    """Vary each pillar weight and each sub-weight by +/-step (relative), renormalise, rescore."""
    step = config.SENSITIVITY_STEP if step is None else step
    weights = weights or default_weights()
    base = score(book, fd, fy, weights, governance, cfo_decision, neq_decision)
    rows = []

    def run(label, w):
        r = score(book, fd, fy, w, governance, cfo_decision, neq_decision)
        delta = None if r.score is None or base.score is None else r.score - base.score
        rows.append({"Weight changed": label, "CCIRS": display_score(r.score),
                     "Change": None if delta is None else round(delta, 2), "Verdict": r.verdict,
                     "Verdict changed": r.verdict != base.verdict})

    for p in config.PILLARS:
        for sign in (+1, -1):
            w = copy.deepcopy(weights)
            w["pillars"][p] = w["pillars"][p] * (1 + sign * step)
            run(f"Pillar {p} {'+' if sign > 0 else '-'}{step:.0%}", w)
    for p, spec in config.PILLARS.items():
        for k in spec["ratios"]:
            for sign in (+1, -1):
                w = copy.deepcopy(weights)
                w["ratios"][p][k] = w["ratios"][p][k] * (1 + sign * step)
                run(f"{p}: {config.COMPONENT_LABELS[k]} {'+' if sign > 0 else '-'}{step:.0%}", w)
    df = pd.DataFrame(rows)
    scores = df["CCIRS"].dropna()
    summary = {"base": base.score, "base_verdict": base.verdict, "scenarios": len(df),
               "min": scores.min() if len(scores) else None, "max": scores.max() if len(scores) else None,
               "verdict_stable_pct": 100 * (1 - df["Verdict changed"].mean()) if len(df) else None,
               "flips": df[df["Verdict changed"]]["Weight changed"].tolist()}
    return df, summary
