"""
CCIRS - Stage 2 ratio engine (revised).

Every metric is computed per fiscal year from the merged FinancialData and returned as a RatioResult:
value, formula text, the inputs used (with values), a status, an applicability tag and an interpretation.
    OK                 - computed
    "data unavailable" - an input is missing (inputs are NEVER filled with zero)
    "not meaningful"   - zero or negative denominator; no misleading number is returned.
                         Economic-stress causes (negative equity / EBITDA) carry a distress code.
Applicability (Valid / Limited / Not valid) comes from config.py and depends on whether the company is a
bank/NBFC/insurer. Stage 3 must exclude "Not valid" ratios from the score.

Percent-type ratios are stored as fractions (0.20 = 20%). Days are days. Multiples are "x".
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

import config
from data import (DERIVED_SUFFIX, ITEM_LABELS, FinancialData, fy_label_from_year, fy_to_year)
from data import one_off_checks as D_one_off
from data import cash_one_off_checks as D_cash_one_off

OK = "OK"
NA = "data unavailable"
NM = "not meaningful"

LIQ, SOLV, EARN, BEN, DIST, GOV = ("Liquidity & cash", "Solvency & debt service", "Earnings integrity",
                                    "Beneish components", "Distress", "Governance (related parties)")
PROF, EFF, MKT, GROW = "Profitability", "Efficiency", "Market perception", "Growth and payout"

# key: (display name, group, unit, formula)
METRICS: dict[str, tuple[str, str, str, str]] = {
    # Liquidity & cash
    "current_ratio": ("Current ratio", LIQ, "x", "Current assets / Current liabilities"),
    "cce": ("CCE - on operating EBITDA", LIQ, "%",
            "CFO / Operating EBITDA (EBITDA - other income - share of associates/JVs)"),
    "cce_reported": ("CCE - on reported EBITDA (secondary)", LIQ, "%", "CFO / Reported EBITDA"),
    "ocf_ratio": ("Operating cash flow ratio", LIQ, "x", "CFO / Current liabilities"),
    "dir": ("Defensive interval ratio - version that feeds the score", LIQ, "days",
            "Current or total version, per the liquid-investments setting"),
    "dir_current": ("DIR - current liquid investments only", LIQ, "days",
                    "(Cash + Current liquid investments + Receivables) / (Cash operating expenses / 365)"),
    "dir_total": ("DIR - incl. non-current liquid investments", LIQ, "days",
                  "(Cash + Current + Non-current liquid investments + Receivables) / (Cash operating expenses / 365)"),
    "wc_days": ("Working-capital days", LIQ, "days", "(Current assets - Current liabilities) / Revenue x 365"),
    "dio": ("Days inventory outstanding (DIO)", LIQ, "days", "Inventory / COGS x 365"),
    "dso": ("Days sales outstanding (DSO)", LIQ, "days", "Receivables / Revenue x 365"),
    "dpo": ("Days payables outstanding (DPO)", LIQ, "days", "Trade payables / COGS x 365"),
    "ccc": ("Cash conversion cycle (CCC)", LIQ, "days", "DIO + DSO - DPO"),
    "inv_vs_sales": ("Inventory growth minus revenue growth", LIQ, "%",
                     "(Inventory t / Inventory t-1 - 1) - (Revenue t / Revenue t-1 - 1)"),
    # Solvency & debt service
    "interest_coverage": ("Interest coverage - on operating EBIT", SOLV, "x",
                          "Operating EBIT (EBIT - other income - share of associates/JVs) / Interest expense"),
    "interest_coverage_reported": ("Interest coverage - on reported EBIT (secondary)", SOLV, "x",
                                   "Reported EBIT / Interest expense"),
    "de_total": ("Debt/Equity - total debt incl. leases (Yahoo definition)", SOLV, "x",
                 "Total debt (incl. lease liabilities) / Shareholders' equity"),
    "de_borrowings": ("Debt/Equity - borrowings excl. leases", SOLV, "x",
                      "(Total debt - Lease liabilities) / Shareholders' equity"),
    "net_debt_ebitda": ("Net debt (incl. leases) / operating EBITDA", SOLV, "x",
                        "(Total debt incl. lease liabilities - Cash - Liquid investments*) / Operating EBITDA; "
                        "*per the setting. Leases are included because operating EBITDA is post-Ind AS 116."),
    "net_debt_ebitda_reported": ("Net debt (incl. leases) / reported EBITDA (secondary)", SOLV, "x",
                                 "(Total debt incl. lease liabilities - Cash - Liquid investments*) / Reported EBITDA"),
    "dscr": ("DSCR proxy (cash)", SOLV, "x", "(CFO - Capex) / (Interest expense + Current portion of debt)"),
    "dscr_ebit": ("DSCR - operating EBIT version", SOLV, "x",
                  "Operating EBIT / (Interest expense + Current portion of debt)"),
    "dscr_precapex": ("DSCR - pre-capex (cash)", SOLV, "x", "CFO / (Interest expense + Current portion of debt)"),
    # Earnings integrity
    "sloan": ("Sloan accrual ratio", EARN, "%", "(Net income - CFO) / Average total assets [(TA t + TA t-1) / 2]"),
    "bs_accrual": ("Balance-sheet accrual ratio", EARN, "%",
                   "(NOA t - NOA t-1) / Average NOA; NOA = (Total assets - Cash - Current liquid investments) "
                   "- (Total liabilities - Total debt)"),
    "dsri": ("DSRI (days sales in receivables index)", EARN, "x",
             "(Receivables t / Revenue t) / (Receivables t-1 / Revenue t-1)"),
    "rec_vs_sales": ("Receivables growth minus sales growth", EARN, "%",
                     "(Receivables t / Receivables t-1 - 1) - (Revenue t / Revenue t-1 - 1)"),
    "beneish_m": ("Beneish M-Score (8-variable)", EARN, "score",
                  "-4.84 + 0.920 DSRI + 0.528 GMI + 0.404 AQI + 0.892 SGI + 0.115 DEPI - 0.172 SGAI "
                  "+ 4.679 TATA - 0.327 LVGI  (flag if > -2.22)"),
    "gmi": ("GMI (gross margin index)", BEN, "x",
            "Gross margin t-1 / Gross margin t; Gross margin = (Revenue - COGS) / Revenue"),
    "aqi": ("AQI (asset quality index)", BEN, "x",
            "[1 - (Current assets t + PP&E t) / TA t] / [1 - (Current assets t-1 + PP&E t-1) / TA t-1]"),
    "sgi": ("SGI (sales growth index)", BEN, "x", "Revenue t / Revenue t-1"),
    "depi": ("DEPI (depreciation index)", BEN, "x", "[D&A t-1 / (D&A t-1 + PP&E t-1)] / [D&A t / (D&A t + PP&E t)]"),
    "sgai": ("SGAI (SG&A index)", BEN, "x", "(SG&A t / Revenue t) / (SG&A t-1 / Revenue t-1)"),
    "tata": ("TATA (total accruals to total assets)", BEN, "x", "(Net income t - CFO t) / Total assets t"),
    "lvgi": ("LVGI (leverage index)", BEN, "x",
             "[(Current liabilities t + Long-term debt t) / TA t] / [(CL t-1 + LTD t-1) / TA t-1]"),
    # Distress
    "altman_z2": ("Altman Z'' (non-manufacturing, 4-factor)", DIST, "score",
                  "6.56 X1 + 3.26 X2 + 6.72 X3 + 1.05 X4"),
    "altman_z1": ("Altman Z' (manufacturing, private-firm version)", DIST, "score",
                  "0.717 X1 + 0.847 X2 + 3.107 X3 + 0.420 X4 + 0.998 X5"),
    "altman": ("Altman Z - variant used", DIST, "score", "Z' for manufacturing, Z'' otherwise"),
    "piotroski": ("Piotroski F-Score", DIST, "F", "Sum of 9 pass/fail criteria (0-9)"),
    # Profitability (context, not in CCIRS)
    "op_ebit_margin": ("Operating EBIT margin", PROF, "%", "Operating EBIT / Revenue"),
    "net_margin": ("Net profit margin", PROF, "%", "PAT / Revenue"),
    "roe": ("ROE", PROF, "%", "PAT / Average shareholders' equity"),
    "roce": ("ROCE", PROF, "%", "Operating EBIT / Capital employed; Capital employed = Equity + Total debt"),
    "roic": ("ROIC", PROF, "%",
             "NOPAT / Invested capital; NOPAT = Operating EBIT x (1 - tax rate), tax rate = (PBT - PAT) / PBT "
             "clipped to 0-40%; Invested capital = Equity + Total debt - Cash - Liquid investments*"),
    "roiic": ("ROIIC (return on incremental invested capital)", PROF, "%",
              "(NOPAT t - NOPAT t-1) / (Invested capital t - Invested capital t-1)"),
    "dupont": ("DuPont ROE (3-step)", PROF, "%",
               "Net margin (PAT/Revenue) x Asset turnover (Revenue/Avg TA) x Equity multiplier (Avg TA/Avg equity)"),
    "dupont_mod": ("Modified DuPont ROE (5-step)", PROF, "%",
                   "EBIT margin (EBIT/Revenue) x Interest burden (PBT/EBIT) x Tax burden (PAT/PBT) "
                   "x Asset turnover x Equity multiplier"),
    # Efficiency (context)
    "asset_turnover": ("Asset turnover", EFF, "x", "Revenue / Average total assets"),
    # Market perception (context; current price and market cap vs latest fiscal year)
    "pe": ("P/E", MKT, "x", "Share price / EPS; EPS = PAT / Shares outstanding"),
    "peg": ("PEG", MKT, "x", "P/E / (3-year EPS CAGR in %)"),
    "div_yield": ("Dividend yield", MKT, "%", "Dividends paid / Market capitalisation"),
    "p_ocf": ("P/OCF", MKT, "x", "Market capitalisation / CFO"),
    # Growth and payout (context)
    "payout": ("Dividend payout", GROW, "%", "Dividends paid / PAT"),
    "retention": ("Retention ratio", GROW, "%", "1 - Dividend payout"),
    "internal_growth": ("Internal growth rate", GROW, "%", "ROA x Retention; ROA = PAT / Average total assets"),
    "sustainable_growth": ("Sustainable growth rate", GROW, "%", "ROE x Retention"),
    # Governance
    "rp_revenue_share": ("Related-party revenue / revenue", GOV, "%", "Revenue from related parties / Revenue"),
    "rp_receivables_share": ("Related-party receivables / receivables", GOV, "%",
                             "Receivables from related parties / Trade receivables"),
    "rp_advances_revenue": ("Related-party advances / revenue", GOV, "%",
                            "Related-party loans & advances / Revenue"),
}
SHORT = {"roe": "ROE", "dupont": "DuPont ROE", "dupont_mod": "Modified DuPont ROE",
         "de_total": "Debt/Equity (incl. leases)", "de_borrowings": "Debt/Equity (excl. leases)",
         "cce": "CCE", "cce_reported": "CCE (reported)", "net_debt_ebitda": "Net debt/EBITDA",
         "net_debt_ebitda_reported": "Net debt/EBITDA (reported)", "gmi": "GMI", "beneish_m": "M-Score"}
BENEISH_COEF = {"const": -4.84, "dsri": 0.920, "gmi": 0.528, "aqi": 0.404, "sgi": 0.892, "depi": 0.115,
                "sgai": -0.172, "tata": 4.679, "lvgi": -0.327}
BENEISH_PARTS = ["dsri", "gmi", "aqi", "sgi", "depi", "sgai", "tata", "lvgi"]
ALTMAN_X_TEXT = ("X1 = (Current assets - Current liabilities) / TA; X2 = Retained earnings / TA; "
                 "X3 = EBIT / TA; X4 = Book equity / Total liabilities; X5 = Revenue / TA")
LEASE_NOTE = ("CFO/EBITDA is overstated for lease-heavy firms because lease payments sit in financing cash "
              "flow (Ind AS 116).")


@dataclass
class RatioResult:
    key: str
    fy: str
    value: float | None
    status: str
    inputs: dict = field(default_factory=dict)       # label -> value (None = unavailable)
    reason: str = ""                                 # why NA / NM
    distress: bool = False
    note: str = ""                                   # definitional notes (proxy, fallback, lease)
    details: list = field(default_factory=list)
    distress_code: str = ""
    applicability: str = "Valid"
    applicability_reason: str = ""
    interpretation: str = ""
    extra: dict = field(default_factory=dict)        # Piotroski: assessable / passed / score_0_100 / scaled_9

    @property
    def name(self) -> str:
        return METRICS[self.key][0]

    @property
    def group(self) -> str:
        return METRICS[self.key][1]

    @property
    def unit(self) -> str:
        return METRICS[self.key][2]

    @property
    def formula(self) -> str:
        return METRICS[self.key][3]

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def explanation(self) -> str:
        """Reason / note column: never blank."""
        main = self.interpretation if self.ok else self.reason
        return " ".join(x for x in (main, self.note) if x) or "-"

    def display(self) -> str:
        if self.status == NA:
            return "data unavailable"
        if self.status == NM:
            return "not meaningful ⚠" if self.distress else "not meaningful"
        if self.key == "piotroski" and self.extra:
            return f"{self.extra['passed']}/{self.extra['assessable']}" + (" *" if self.note else "")
        return format_value(self.value, self.unit) + (" *" if self.note else "")


def format_value(v: float | None, unit: str) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    if unit == "%":
        return f"{v * 100:,.1f}%"
    if unit == "days":
        return f"{v:,.0f} d"
    if unit == "x":
        return f"{v:,.2f}x"
    if unit == "F":
        return f"{int(v)}/9"
    if unit == "Fn":
        return v
    return f"{v:,.2f}"


def interpret(key: str, value: float | None) -> str:
    if value is None:
        return ""
    table = config.INTERPRETATION.get(config.INTERPRETATION_ALIAS.get(key, key))
    if not table:
        return ""
    for upper, text in table:
        if value < upper:
            return text
    return table[-1][1]


def score_zone(key: str, value: float | None) -> tuple[str, str]:
    """(zone label, colour) for the summary cards; colour is green / amber / red."""
    bands = config.SCORE_ZONES.get(key)
    if value is None or not bands:
        return "", "grey"
    for upper, label, colour in bands:
        if value < upper:
            return label, colour
    return bands[-1][1], bands[-1][2]


def threshold_band(key: str, value: float | None):
    """(sub-score, label) from config.SCORE_THRESHOLDS - the same table Stage 3 scores from."""
    bands = config.SCORE_THRESHOLDS.get(key)
    if value is None or not bands:
        return None, ""
    for upper, score, label in bands:
        if value < upper:
            return score, label
    return bands[-1][1], bands[-1][2]


def status(r: "RatioResult", key: str | None = None) -> tuple[str, str]:
    """(label, colour) for the status dot. colour: green / amber / red / grey."""
    if r.status == NA:
        return "no data", "grey"
    if r.status == NM:
        return ("not meaningful", "red") if r.distress else ("not meaningful", "grey")
    key = key or r.key
    if key in config.NEUTRAL_LABELS:
        for upper, label in config.NEUTRAL_LABELS[key]:
            if r.value < upper:
                return label, "grey"
        return config.NEUTRAL_LABELS[key][-1][1], "grey"
    value = r.extra.get("scaled_9", r.value) if key == "piotroski" else r.value
    score, label = threshold_band(key, value)
    if score is None:
        return "-", "grey"
    hi, lo = config.STATUS_COLOUR_CUTS
    return label, ("green" if score >= hi else "amber" if score >= lo else "red")


# Rows shown only in expanders, not in the main ratios table.
# DSRI stays as a headline row because CCIRS pillar C scores it directly (DSRI / receivables-vs-sales).
MAIN_TABLE_EXCLUDE = {"gmi", "aqi", "sgi", "depi", "sgai", "tata", "lvgi", "altman_z1", "altman_z2"}
ALTMAN_SHORT = {"altman_z1": "Z' (manufacturing)", "altman_z2": "Z'' (non-manufacturing, 4-factor)"}
# Z'' here is the 4-factor non-manufacturing model WITHOUT the +3.25 emerging-markets constant.
ALTMAN_COEF = {"altman_z1": {"X1": 0.717, "X2": 0.847, "X3": 3.107, "X4": 0.420, "X5": 0.998},
               "altman_z2": {"X1": 6.56, "X2": 3.26, "X3": 6.72, "X4": 1.05}}


def applicability(key: str, is_financial: bool) -> tuple[str, str]:
    if is_financial:
        return config.FIN_APPLICABILITY.get(key, config.FIN_DEFAULT)
    return config.NONFIN_APPLICABILITY.get(key, config.NONFIN_DEFAULT)


# --------------------------------------------------------------------------- helpers
def prev_fy(fd: FinancialData, fy: str) -> str | None:
    p = fy_label_from_year(fy_to_year(fy) - 1)
    return p if p in fd.years else None


def _is_nan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


class _B:
    """Collects inputs for one ratio-year and builds the RatioResult."""

    def __init__(self, fd: FinancialData, fy: str, key: str):
        self.fd, self.fy, self.key = fd, fy, key
        self.py = prev_fy(fd, fy)
        self.inputs: dict = {}
        self.missing: list[str] = []

    def v(self, item: str, prior: bool = False, required: bool = True) -> float:
        year = self.py if prior else self.fy
        if year is None:
            want = fy_label_from_year(fy_to_year(self.fy) - 1)
            lab = f"{ITEM_LABELS[item]} {want}"
            self.inputs[lab] = None
            if required:
                self.missing.append(f"{lab} (prior year not in data)")
            return float("nan")
        lab = f"{ITEM_LABELS[item]} {year}"
        x = self.fd.get(item, year)
        self.inputs[lab] = None if math.isnan(x) else x
        if math.isnan(x) and required:
            self.missing.append(lab)
        return x

    def add(self, label: str, value):
        self.inputs[label] = value

    def na(self, reason: str | None = None) -> RatioResult:
        return RatioResult(self.key, self.fy, None, NA, self.inputs,
                           reason or "Missing: " + ", ".join(dict.fromkeys(self.missing)))

    def nm(self, reason: str, distress_code: str = "") -> RatioResult:
        return RatioResult(self.key, self.fy, None, NM, self.inputs, reason, bool(distress_code),
                           distress_code=distress_code)

    def ok(self, value: float, note: str = "", details=None) -> RatioResult:
        return RatioResult(self.key, self.fy, float(value), OK, self.inputs, note=note, details=details or [])


NEG_EQUITY = ("Shareholders' equity is negative (accumulated losses exceed capital). Debt/Equity is not "
              "meaningful. Treat as a distress signal.")
NEG_EBITDA = ("EBITDA is negative (operating loss before D&A). The ratio would flip sign and mislead. "
              "Treat as a distress signal.")


def _simple(fd, fy, key, num_items, den_item, num_fn=None, mult=1.0, zero_den_reason=None):
    b = _B(fd, fy, key)
    nums = [b.v(i) for i in num_items]
    den = b.v(den_item)
    if b.missing:
        return b.na()
    num = num_fn(*nums) if num_fn else nums[0]
    if den == 0:
        return b.nm(zero_den_reason or f"{ITEM_LABELS[den_item]} is zero - ratio undefined.")
    if den < 0:
        return b.nm(f"{ITEM_LABELS[den_item]} is negative - ratio has no economic meaning.")
    return b.ok(num / den * mult)


def _profit(b: _B, operating: bool, kind: str) -> tuple[float, str]:
    """kind 'ebitda' or 'ebit'. Operating figure if available, else reported (fallback, noted)."""
    op_key, rep_key = f"operating_{kind}", kind
    label = "EBITDA" if kind == "ebitda" else "EBIT"
    if not operating:
        return b.v(rep_key), ""
    x = b.v(op_key, required=False)
    if not math.isnan(x):
        adj = (b.fd.meta.get("op_adjustments", {}).get(b.fy, {}) or {}).get(op_key, "")
        if adj and adj != "supplied":
            # show the bridge as input rows: reported -> less adjustments -> operating
            op_label = f"{ITEM_LABELS[op_key]} {b.fy}"
            b.inputs.pop(op_label, None)
            rep = b.fd.get(rep_key, b.fy)
            b.add(f"{label} as reported {b.fy}", None if math.isnan(rep) else rep)
            excl = b.fd.meta.get("exceptional_excluded", True)
            for item, txt in (("other_income", "less other income"), ("associate_share", "less share of associates/JVs"),
                              ("exceptional_items", "less exceptional items")):
                if item == "exceptional_items" and not excl:
                    continue
                v = b.fd.get(item, b.fy)
                b.add(f"{txt} {b.fy}", None if math.isnan(v) else v)
            b.add(f"Operating {label} {b.fy}", x)
        return x, (f"Operating {label} = {adj}." if adj and adj != "supplied" else "")
    if config.FALLBACK_TO_REPORTED_EBITDA:
        y = b.v(rep_key)
        return y, (f"Operating {label} unavailable (other income / associates not reported) - reported "
                   f"{label} used, which may include non-operating income." if not math.isnan(y) else "")
    b.missing.append(f"{ITEM_LABELS[op_key]} {b.fy}")
    return float("nan"), ""


def _liquid(b: _B, include_noncurrent: bool) -> tuple[float, str]:
    cash, ms = b.v("cash"), b.v("marketable_securities")
    if not include_noncurrent:
        return cash + ms, "Liquid funds = cash + current liquid investments (setting: exclude non-current)."
    ncli = b.v("noncurrent_liquid_investments", required=False)
    if math.isnan(ncli):
        return cash + ms, ("Liquid funds = cash + current liquid investments only: non-current liquid "
                           "investments not reported, so the total version could not be used.")
    return cash + ms + ncli, "Liquid funds = cash + current + non-current liquid investments (setting: include)."


def _lease_heavy(fd, fy) -> bool:
    lease, td = fd.get("lease_liabilities", fy), fd.get("total_debt", fy)
    return not math.isnan(lease) and not math.isnan(td) and td > 0 and lease / td > config.LEASE_HEAVY_SHARE


def _join(*notes) -> str:
    return " ".join(n for n in notes if n)


# --------------------------------------------------------------------------- liquidity
def current_ratio(fd, fy, s):
    return _simple(fd, fy, "current_ratio", ["current_assets"], "current_liabilities")


def _cce(fd, fy, operating):
    key = "cce" if operating else "cce_reported"
    b = _B(fd, fy, key)
    cfo = b.v("cfo")
    den, pnote = _profit(b, operating, "ebitda")
    if b.missing:
        return b.na()
    if den == 0:
        return b.nm("EBITDA is zero - CFO/EBITDA undefined.")
    if den < 0:
        return b.nm(NEG_EBITDA, "neg_ebitda")
    return b.ok(cfo / den, note=_join(pnote, LEASE_NOTE if _lease_heavy(fd, fy) else ""))


def cce(fd, fy, s):
    return _cce(fd, fy, True)


def cce_reported(fd, fy, s):
    return _cce(fd, fy, False)


def ocf_ratio(fd, fy, s):
    return _simple(fd, fy, "ocf_ratio", ["cfo"], "current_liabilities")


def _opex_note(fd, fy) -> str:
    src = fd.sources.at["cash_opex", fy] if fy in fd.sources.columns else ""
    if not str(src).endswith(DERIVED_SUFFIX):
        return ""
    base = "operating EBITDA" if not math.isnan(fd.get("operating_ebitda", fy)) else "reported EBITDA"
    return f"Cash operating expenses is a PROXY (Revenue - {base}), not a reported figure."


def _dir(fd, fy, key, with_noncurrent):
    b = _B(fd, fy, key)
    cash, ms, rec = b.v("cash"), b.v("marketable_securities"), b.v("receivables")
    ncli = b.v("noncurrent_liquid_investments") if with_noncurrent else 0.0
    opex = b.v("cash_opex")
    if b.missing:
        extra = (" Cash operating expenses can be derived as Revenue - EBITDA if both are available."
                 if any("Cash operating expenses" in m for m in b.missing) else "")
        return b.na("Missing: " + ", ".join(b.missing) + "." + extra)
    if opex <= 0:
        return b.nm("Cash operating expenses is zero or negative - DIR undefined.")
    return b.ok((cash + ms + rec + ncli) / (opex / 365), note=_opex_note(fd, fy))


def dir_current(fd, fy, s):
    return _dir(fd, fy, "dir_current", False)


def dir_total(fd, fy, s):
    return _dir(fd, fy, "dir_total", True)


def dir_selected(fd, fy, s, cur: RatioResult, tot: RatioResult) -> RatioResult:
    include = s["include_noncurrent"]
    if include and tot.ok:
        src, why = tot, "Total version (incl. non-current liquid investments) feeds the score - setting: include."
    elif include:
        src, why = cur, ("Setting is 'include non-current', but non-current liquid investments are not reported "
                         "- current-only version used.")
    else:
        src, why = cur, "Current-only version feeds the score - setting: exclude non-current."
    return RatioResult("dir", fy, src.value, src.status, src.inputs, src.reason, src.distress,
                       _join(why, src.note), distress_code=src.distress_code)


def wc_days(fd, fy, s):
    return _simple(fd, fy, "wc_days", ["current_assets", "current_liabilities"], "revenue",
                   num_fn=lambda ca, cl: ca - cl, mult=365)


def dio(fd, fy, s=None):
    return _simple(fd, fy, "dio", ["inventory"], "cogs", mult=365)


def dso(fd, fy, s=None):
    return _simple(fd, fy, "dso", ["receivables"], "revenue", mult=365)


def dpo(fd, fy, s=None):
    return _simple(fd, fy, "dpo", ["accounts_payable"], "cogs", mult=365)


def ccc(fd, fy, s):
    parts = {"DIO": dio(fd, fy), "DSO": dso(fd, fy), "DPO": dpo(fd, fy)}
    inputs = {}
    for name, r in parts.items():
        inputs.update(r.inputs)
        inputs[f"{name} {fy} (days)"] = r.value
    bad = {n: r for n, r in parts.items() if not r.ok}
    if bad:
        status = NA if any(r.status == NA for r in bad.values()) else NM
        return RatioResult("ccc", fy, None, status, inputs,
                           "; ".join(f"{n}: {r.status} ({r.reason})" for n, r in bad.items()))
    return RatioResult("ccc", fy, parts["DIO"].value + parts["DSO"].value - parts["DPO"].value, OK, inputs)


def _growth_gap(fd, fy, key, item, item_word):
    b = _B(fd, fy, key)
    x, sales, xp, sp = b.v(item), b.v("revenue"), b.v(item, True), b.v("revenue", True)
    if b.missing:
        return b.na()
    if xp <= 0 or sp <= 0:
        return b.nm(f"Prior-year {item_word} or revenue is zero or negative - growth undefined.")
    xg, sg = x / xp - 1, sales / sp - 1
    b.add(f"{item_word.capitalize()} growth", xg)
    b.add("Revenue growth", sg)
    return b.ok(xg - sg, note=f"{item_word.capitalize()} grew {xg:+.1%} vs revenue {sg:+.1%}.")


def inv_vs_sales(fd, fy, s):
    return _growth_gap(fd, fy, "inv_vs_sales", "inventory", "inventory")


# --------------------------------------------------------------------------- solvency
def _coverage(fd, fy, operating):
    key = "interest_coverage" if operating else "interest_coverage_reported"
    b = _B(fd, fy, key)
    ebit, pnote = _profit(b, operating, "ebit")
    i = b.v("interest_expense")
    if b.missing:
        return b.na()
    if i == 0:
        return b.nm("No interest expense reported - coverage not meaningful (effectively debt-free or "
                    "interest capitalised).")
    return b.ok(ebit / i, note=pnote)


def interest_coverage(fd, fy, s):
    return _coverage(fd, fy, True)


def interest_coverage_reported(fd, fy, s):
    return _coverage(fd, fy, False)


def de_total(fd, fy, s):
    b = _B(fd, fy, "de_total")
    td, eq = b.v("total_debt"), b.v("equity")
    if b.missing:
        return b.na()
    if eq == 0:
        return b.nm("Equity is zero - Debt/Equity undefined.")
    if eq < 0:
        return b.nm(NEG_EQUITY, "neg_equity")
    return b.ok(td / eq)


def de_borrowings(fd, fy, s):
    b = _B(fd, fy, "de_borrowings")
    td, lease, eq = b.v("total_debt"), b.v("lease_liabilities"), b.v("equity")
    if b.missing:
        if any("Lease liabilities" in m for m in b.missing) and not math.isnan(td) and not math.isnan(eq):
            return b.na("Lease split not available: lease liabilities were not reported (Yahoo/upload), so "
                        "borrowings excluding leases cannot be isolated. Use the total-debt version.")
        return b.na()
    borrow = td - lease
    b.add(f"Borrowings excl. leases {fy}", borrow)
    if borrow < 0:
        return b.nm("Lease liabilities exceed total debt - the two figures are inconsistent; check the data.")
    if eq == 0:
        return b.nm("Equity is zero - Debt/Equity undefined.")
    if eq < 0:
        return b.nm(NEG_EQUITY, "neg_equity")
    return b.ok(borrow / eq)


def _net_debt(fd, fy, s, operating):
    key = "net_debt_ebitda" if operating else "net_debt_ebitda_reported"
    b = _B(fd, fy, key)
    td = b.v("total_debt")
    liquid, lnote = _liquid(b, s["include_noncurrent"])
    e, pnote = _profit(b, operating, "ebitda")
    if b.missing:
        return b.na()
    nd = td - liquid
    b.add(f"Liquid funds {fy}", liquid)
    b.add(f"Net debt incl. leases {fy}", nd)
    if e == 0:
        return b.nm("EBITDA is zero - Net debt/EBITDA undefined.")
    if e < 0:
        return b.nm(NEG_EBITDA, "neg_ebitda")
    return b.ok(nd / e, note=_join(lnote, pnote, "Net cash position." if nd < 0 else ""))


def net_debt_ebitda(fd, fy, s):
    return _net_debt(fd, fy, s, True)


def net_debt_ebitda_reported(fd, fy, s):
    return _net_debt(fd, fy, s, False)


def dscr(fd, fy, s):
    b = _B(fd, fy, "dscr")
    cfo, capex, i, cd = b.v("cfo"), b.v("capex"), b.v("interest_expense"), b.v("current_debt")
    if b.missing:
        return b.na()
    den = i + cd
    b.add(f"Debt service (interest + current debt) {fy}", den)
    if den <= 0:
        return b.nm("No interest or current debt falling due - DSCR not meaningful.")
    return b.ok((cfo - capex) / den, note="Cash version after capex: it penalises growth capex, so a company "
                "investing heavily can look weak - compare the pre-capex version in More detail.")


def dscr_precapex(fd, fy, s):
    b = _B(fd, fy, "dscr_precapex")
    cfo, i, cd = b.v("cfo"), b.v("interest_expense"), b.v("current_debt")
    if b.missing:
        return b.na()
    den = i + cd
    b.add(f"Debt service (interest + current debt) {fy}", den)
    if den <= 0:
        return b.nm("No interest or current debt falling due - DSCR not meaningful.")
    return b.ok(cfo / den, note="Before capex: shows debt-service capacity if all investment were paused.")


def dscr_ebit(fd, fy, s):
    b = _B(fd, fy, "dscr_ebit")
    ebit, pnote = _profit(b, True, "ebit")
    i, cd = b.v("interest_expense"), b.v("current_debt")
    if b.missing:
        return b.na()
    den = i + cd
    b.add(f"Debt service (interest + current debt) {fy}", den)
    if den <= 0:
        return b.nm("No interest or current debt falling due - DSCR not meaningful.")
    return b.ok(ebit / den, note=pnote)


# --------------------------------------------------------------------------- earnings integrity
def sloan(fd, fy, s):
    b = _B(fd, fy, "sloan")
    ni, cfo, ta, ta_p = b.v("pat"), b.v("cfo"), b.v("total_assets"), b.v("total_assets", prior=True)
    if b.missing:
        return b.na()
    avg = (ta + ta_p) / 2
    b.add("Average total assets", avg)
    if avg <= 0:
        return b.nm("Average total assets is not positive.")
    return b.ok((ni - cfo) / avg)


def _noa(b: _B, prior: bool) -> float:
    ta, cash, ms = b.v("total_assets", prior), b.v("cash", prior), b.v("marketable_securities", prior)
    tl, td = b.v("total_liabilities", prior), b.v("total_debt", prior)
    return (ta - cash - ms) - (tl - td)


def bs_accrual(fd, fy, s):
    b = _B(fd, fy, "bs_accrual")
    noa, noa_p = _noa(b, False), _noa(b, True)
    if b.missing:
        return b.na()
    b.add(f"NOA {fy}", noa)
    b.add(f"NOA {b.py}", noa_p)
    avg = (noa + noa_p) / 2
    b.add("Average NOA", avg)
    if avg <= 0:
        return b.nm("Average net operating assets is zero or negative (operating liabilities exceed operating "
                    "assets) - ratio not meaningful.")
    return b.ok((noa - noa_p) / avg)


def dsri(fd, fy, s=None):
    b = _B(fd, fy, "dsri")
    r, sl, rp, sp = b.v("receivables"), b.v("revenue"), b.v("receivables", True), b.v("revenue", True)
    if b.missing:
        return b.na()
    if sl <= 0 or sp <= 0:
        return b.nm("Revenue is zero or negative in one of the years.")
    if rp == 0:
        return b.nm("Prior-year receivables are zero - index undefined.")
    return b.ok((r / sl) / (rp / sp))


def rec_vs_sales(fd, fy, s):
    return _growth_gap(fd, fy, "rec_vs_sales", "receivables", "receivables")


def gmi(fd, fy, s=None):
    b = _B(fd, fy, "gmi")
    sl, c, sp, cp = b.v("revenue"), b.v("cogs"), b.v("revenue", True), b.v("cogs", True)
    if b.missing:
        return b.na()
    if sl <= 0 or sp <= 0:
        return b.nm("Revenue is zero or negative.")
    gm, gmp = (sl - c) / sl, (sp - cp) / sp
    b.add(f"Gross margin {fy}", gm)
    b.add(f"Gross margin {b.py}", gmp)
    if gm <= 0:
        return b.nm("Current gross margin is zero or negative - index not meaningful.", "neg_margin")
    return b.ok(gmp / gm)


def aqi(fd, fy, s=None):
    b = _B(fd, fy, "aqi")
    ca, ppe, ta = b.v("current_assets"), b.v("ppe"), b.v("total_assets")
    cap, ppep, tap = b.v("current_assets", True), b.v("ppe", True), b.v("total_assets", True)
    if b.missing:
        return b.na()
    if ta <= 0 or tap <= 0:
        return b.nm("Total assets not positive.")
    q, qp = 1 - (ca + ppe) / ta, 1 - (cap + ppep) / tap
    b.add(f"Soft-asset share {fy}", q)
    b.add(f"Soft-asset share {b.py}", qp)
    if qp == 0:
        return b.nm("Prior-year soft-asset share is zero - index undefined.")
    return b.ok(q / qp)


def sgi(fd, fy, s=None):
    b = _B(fd, fy, "sgi")
    sl, sp = b.v("revenue"), b.v("revenue", True)
    if b.missing:
        return b.na()
    if sp <= 0:
        return b.nm("Prior-year revenue is zero or negative.")
    return b.ok(sl / sp)


def depi(fd, fy, s=None):
    b = _B(fd, fy, "depi")
    d, p, dp, pp = b.v("depreciation"), b.v("ppe"), b.v("depreciation", True), b.v("ppe", True)
    if b.missing:
        return b.na()
    if (d + p) <= 0 or (dp + pp) <= 0:
        return b.nm("D&A + PP&E not positive.")
    rate, rate_p = d / (d + p), dp / (dp + pp)
    if rate == 0:
        return b.nm("Current depreciation rate is zero - index undefined.")
    return b.ok(rate_p / rate)


def sgai(fd, fy, s=None):
    b = _B(fd, fy, "sgai")
    g, sl, gp, sp = b.v("sga"), b.v("revenue"), b.v("sga", True), b.v("revenue", True)
    if b.missing:
        return b.na()
    if sl <= 0 or sp <= 0:
        return b.nm("Revenue is zero or negative.")
    if gp == 0:
        return b.nm("Prior-year SG&A is zero - index undefined.")
    return b.ok((g / sl) / (gp / sp))


def tata(fd, fy, s=None):
    b = _B(fd, fy, "tata")
    ni, cfo, ta = b.v("pat"), b.v("cfo"), b.v("total_assets")
    if b.missing:
        return b.na()
    if ta <= 0:
        return b.nm("Total assets not positive.")
    return b.ok((ni - cfo) / ta)


def lvgi(fd, fy, s=None):
    b = _B(fd, fy, "lvgi")
    cl, ltd, ta = b.v("current_liabilities"), b.v("long_term_debt"), b.v("total_assets")
    clp, ltdp, tap = b.v("current_liabilities", True), b.v("long_term_debt", True), b.v("total_assets", True)
    if b.missing:
        return b.na()
    if ta <= 0 or tap <= 0:
        return b.nm("Total assets not positive.")
    lev, levp = (cl + ltd) / ta, (clp + ltdp) / tap
    if levp == 0:
        return b.nm("Prior-year leverage is zero - index undefined.")
    return b.ok(lev / levp)


COMPONENT_FN = {"dsri": dsri, "gmi": gmi, "aqi": aqi, "sgi": sgi, "depi": depi, "sgai": sgai,
                "tata": tata, "lvgi": lvgi}


def beneish(fd, fy, comps: dict | None = None):
    comps = comps or {k: fn(fd, fy) for k, fn in COMPONENT_FN.items()}
    inputs = {k.upper(): (r.value if r.ok else None) for k, r in comps.items()}
    bad = {k: r for k, r in comps.items() if not r.ok}
    if bad:
        status = NA if any(r.status == NA for r in bad.values()) else NM
        reason = ("M-Score not reported because these components cannot be computed: "
                  + "; ".join(f"{k.upper()} - {r.status} ({r.reason})" for k, r in bad.items()))
        return RatioResult("beneish_m", fy, None, status, inputs, reason)
    m = BENEISH_COEF["const"] + sum(BENEISH_COEF[k] * comps[k].value for k in BENEISH_PARTS)
    contrib = [{"Component": k.upper(), "Value": comps[k].value, "Coefficient": BENEISH_COEF[k],
                "Contribution": BENEISH_COEF[k] * comps[k].value} for k in BENEISH_PARTS]
    return RatioResult("beneish_m", fy, m, OK, inputs, details=contrib)


# --------------------------------------------------------------------------- distress
def altman_variant(meta: dict) -> tuple[str, str]:
    industry = str(meta.get("industry") or "").lower()
    sector = str(meta.get("sector") or "").lower()
    for kw in config.MANUFACTURING_INDUSTRY_KEYWORDS:
        if kw in industry:
            return "altman_z1", f"Industry '{meta.get('industry')}' is manufacturing -> Z' (5-factor)."
    if sector in config.MANUFACTURING_SECTORS:
        return "altman_z1", f"Sector '{meta.get('sector')}' is manufacturing -> Z' (5-factor)."
    return "altman_z2", (f"Industry '{meta.get('industry') or 'unknown'}' is not manufacturing -> Z'' "
                         f"(non-manufacturing, 4-factor; no +3.25 emerging-markets constant).")


def altman(fd, fy, key):
    b = _B(fd, fy, key)
    ca, cl, ta = b.v("current_assets"), b.v("current_liabilities"), b.v("total_assets")
    re_, eq, tl = b.v("retained_earnings"), b.v("equity"), b.v("total_liabilities")
    ebit, pnote = _profit(b, True, "ebit")         # X3 on operating EBIT (excl. other income and exceptional items)
    s = b.v("revenue") if key == "altman_z1" else None
    if b.missing:
        return b.na()
    if ta <= 0:
        return b.nm("Total assets not positive.")
    if tl <= 0:
        return b.nm("Total liabilities not positive - X4 undefined.")
    x = {"X1": (ca - cl) / ta, "X2": re_ / ta, "X3": ebit / ta, "X4": eq / tl}
    if key == "altman_z1":
        x["X5"] = s / ta
        z = 0.717 * x["X1"] + 0.847 * x["X2"] + 3.107 * x["X3"] + 0.420 * x["X4"] + 0.998 * x["X5"]
    else:
        z = 6.56 * x["X1"] + 3.26 * x["X2"] + 6.72 * x["X3"] + 1.05 * x["X4"]
    for k, v in x.items():
        b.add(k, v)
    note = "Book equity used in X4; operating EBIT used in X3 (excludes other income and exceptional items)."
    if eq < 0:
        note += " Negative equity makes X4 negative (valid input, distress signal)."
    return b.ok(z, note=note, details=[{"Factor": k, "Value": v} for k, v in x.items()])


def piotroski(fd, fy, s=None):
    b = _B(fd, fy, "piotroski")
    g = {k: b.v(k) for k in ("pat", "cfo", "total_assets", "long_term_debt", "current_assets",
                             "current_liabilities", "shares_outstanding", "revenue", "cogs")}
    p = {k: b.v(k, prior=True) for k in ("pat", "total_assets", "long_term_debt", "current_assets",
                                         "current_liabilities", "shares_outstanding", "revenue", "cogs")}

    def ratio(a, c):
        return float("nan") if c == 0 or math.isnan(a) or math.isnan(c) else a / c

    roa, roa_p = ratio(g["pat"], g["total_assets"]), ratio(p["pat"], p["total_assets"])
    lev, lev_p = ratio(g["long_term_debt"], g["total_assets"]), ratio(p["long_term_debt"], p["total_assets"])
    cr, cr_p = (ratio(g["current_assets"], g["current_liabilities"]),
                ratio(p["current_assets"], p["current_liabilities"]))
    gm, gm_p = ratio(g["revenue"] - g["cogs"], g["revenue"]), ratio(p["revenue"] - p["cogs"], p["revenue"])
    ato, ato_p = ratio(g["revenue"], g["total_assets"]), ratio(p["revenue"], p["total_assets"])

    def cmp(a, c, fn):
        return None if math.isnan(a) or math.isnan(c) else bool(fn(a, c))

    crit = [
        ("1. ROA > 0 (Net income / TA)", None if math.isnan(roa) else roa > 0, f"ROA {roa:.2%}"),
        ("2. CFO > 0", None if math.isnan(g["cfo"]) else g["cfo"] > 0, f"CFO {g['cfo']:,.1f}"),
        ("3. ROA improved", cmp(roa, roa_p, lambda a, c: a > c), f"{roa_p:.2%} -> {roa:.2%}"),
        ("4. CFO > Net income (quality of earnings)",
         None if math.isnan(g["cfo"]) or math.isnan(g["pat"]) else g["cfo"] > g["pat"],
         f"CFO {g['cfo']:,.1f} vs NI {g['pat']:,.1f}"),
        ("5. Long-term debt / TA fell", cmp(lev, lev_p, lambda a, c: a < c or (a == 0 and c == 0)),
         f"{lev_p:.2%} -> {lev:.2%}"),
        ("6. Current ratio improved", cmp(cr, cr_p, lambda a, c: a > c), f"{cr_p:.2f} -> {cr:.2f}"),
        ("7. No new shares issued", cmp(g["shares_outstanding"], p["shares_outstanding"], lambda a, c: a <= c),
         f"{p['shares_outstanding']:,.2f} -> {g['shares_outstanding']:,.2f} crore shares"),
        ("8. Gross margin improved", cmp(gm, gm_p, lambda a, c: a > c), f"{gm_p:.2%} -> {gm:.2%}"),
        ("9. Asset turnover improved", cmp(ato, ato_p, lambda a, c: a > c), f"{ato_p:.2f} -> {ato:.2f}"),
    ]
    details = [{"Criterion": n, "Result": "Pass" if r else ("Fail" if r is False else NA),
                "Detail": d.replace("nan%", "n/a").replace("nan", "n/a")} for n, r, d in crit]
    assessable = [r for _, r, _ in crit if r is not None]
    passes = sum(1 for r in assessable if r)
    note = "Simplification: ROA and asset turnover use year-end total assets so the score works with 2 years."
    n = len(assessable)
    if n < config.PIOTROSKI_MIN_ASSESSABLE:
        res = b.na(f"Only {n} of 9 criteria assessable (minimum {config.PIOTROSKI_MIN_ASSESSABLE}). "
                   f"Missing: " + ", ".join(dict.fromkeys(b.missing)))
        res.details, res.note = details, note
        return res
    res = b.ok(passes, note=(note + (f" {9 - n} criteria not assessable - excluded, not counted as failures."
                                     if n < 9 else "")), details=details)
    res.extra = {"assessable": n, "passed": passes, "score_0_100": passes / n * 100, "scaled_9": passes / n * 9}
    return res


# --------------------------------------------------------------------------- profitability / efficiency
def _avg(b: _B, item: str) -> float:
    return (b.v(item) + b.v(item, prior=True)) / 2


def op_ebit_margin(fd, fy, s):
    b = _B(fd, fy, "op_ebit_margin")
    ebit, pnote = _profit(b, True, "ebit")
    rev = b.v("revenue")
    if b.missing:
        return b.na()
    if rev <= 0:
        return b.nm("Revenue is zero or negative.")
    return b.ok(ebit / rev, note=pnote)


def net_margin(fd, fy, s):
    b = _B(fd, fy, "net_margin")
    pat, rev = b.v("pat"), b.v("revenue")
    if b.missing:
        return b.na()
    if rev <= 0:
        return b.nm("Revenue is zero or negative.")
    return b.ok(pat / rev)


def roe(fd, fy, s):
    b = _B(fd, fy, "roe")
    pat, avg_eq = b.v("pat"), _avg(b, "equity")
    if b.missing:
        return b.na()
    b.add("Average equity", avg_eq)
    if avg_eq <= 0:
        return b.nm("Average equity is zero or negative - ROE not meaningful.", "neg_equity")
    return b.ok(pat / avg_eq)


def roce(fd, fy, s):
    b = _B(fd, fy, "roce")
    ebit, pnote = _profit(b, True, "ebit")
    eq, td = b.v("equity"), b.v("total_debt")
    if b.missing:
        return b.na()
    ce = eq + td
    b.add(f"Capital employed {fy}", ce)
    if ce <= 0:
        return b.nm("Capital employed is zero or negative.", "neg_equity" if eq < 0 else "")
    return b.ok(ebit / ce, note=pnote)


def _tax_rate(pbt: float, pat: float) -> tuple[float, str]:
    if pbt <= 0:
        return 0.0, "PBT not positive: tax rate set to 0% (clip rule)."
    t = (pbt - pat) / pbt
    clipped = min(max(t, 0.0), 0.40)
    return clipped, ("" if clipped == t else f"Effective tax rate {t:.1%} clipped to {clipped:.0%}.")


def _nopat_ic(b: _B, s, prior: bool):
    """(NOPAT, invested capital, notes) for the year (prior=True: previous year)."""
    year = b.py if prior else b.fy
    sub = _B(b.fd, year, b.key) if year else None
    if sub is None:
        b.v("pbt", prior=True)          # records 'prior year not in data'
        return float("nan"), float("nan"), ""
    ebit, pnote = _profit(sub, True, "ebit")
    pbt, pat, eq, td = sub.v("pbt"), sub.v("pat"), sub.v("equity"), sub.v("total_debt")
    liquid, lnote = _liquid(sub, s["include_noncurrent"])
    b.inputs.update(sub.inputs)
    b.missing += sub.missing
    if sub.missing:
        return float("nan"), float("nan"), ""
    t, tnote = _tax_rate(pbt, pat)
    nopat, ic = ebit * (1 - t), eq + td - liquid
    b.add(f"Tax rate used {year}", t)
    b.add(f"NOPAT {year}", nopat)
    b.add(f"Invested capital {year}", ic)
    return nopat, ic, _join(pnote, tnote, lnote)


def roic(fd, fy, s):
    b = _B(fd, fy, "roic")
    nopat, ic, note = _nopat_ic(b, s, prior=False)
    if b.missing:
        return b.na()
    if ic <= 0:
        return b.nm("Invested capital is zero or negative (cash and liquid investments exceed equity + debt) - "
                    "ROIC not meaningful.")
    return b.ok(nopat / ic, note=note)


def roiic(fd, fy, s):
    b = _B(fd, fy, "roiic")
    n1, ic1, note = _nopat_ic(b, s, prior=False)
    n0, ic0, _ = _nopat_ic(b, s, prior=True)
    if b.missing:
        return b.na()
    d_ic = ic1 - ic0
    b.add("Change in NOPAT", n1 - n0)
    b.add("Change in invested capital", d_ic)
    if abs(d_ic) < 1e-9:
        return b.nm("Invested capital unchanged - ROIIC undefined.")
    if d_ic < 0:
        return b.nm("Invested capital fell - ROIIC sign is not interpretable.")
    return b.ok((n1 - n0) / d_ic, note=note)


def asset_turnover(fd, fy, s):
    b = _B(fd, fy, "asset_turnover")
    rev, avg_ta = b.v("revenue"), _avg(b, "total_assets")
    if b.missing:
        return b.na()
    b.add("Average total assets", avg_ta)
    if avg_ta <= 0:
        return b.nm("Average total assets is not positive.")
    return b.ok(rev / avg_ta)


def dupont(fd, fy, s):
    b = _B(fd, fy, "dupont")
    pat, rev = b.v("pat"), b.v("revenue")
    avg_ta, avg_eq = _avg(b, "total_assets"), _avg(b, "equity")
    if b.missing:
        return b.na()
    if rev <= 0 or avg_ta <= 0:
        return b.nm("Revenue or average total assets is not positive.")
    if avg_eq <= 0:
        return b.nm("Average equity is zero or negative - DuPont not meaningful.", "neg_equity")
    nm_, at, em = pat / rev, rev / avg_ta, avg_ta / avg_eq
    for k, v in (("Net margin", nm_), ("Asset turnover", at), ("Equity multiplier", em)):
        b.add(k, v)
    return b.ok(nm_ * at * em, note=f"ROE {nm_ * at * em:.1%} = net margin {nm_:.1%} x asset turnover {at:.2f}x "
                                    f"x equity multiplier {em:.2f}x.")


def dupont_mod(fd, fy, s):
    b = _B(fd, fy, "dupont_mod")
    ebit, pbt, pat, rev = b.v("ebit"), b.v("pbt"), b.v("pat"), b.v("revenue")
    avg_ta, avg_eq = _avg(b, "total_assets"), _avg(b, "equity")
    if b.missing:
        return b.na()
    if rev <= 0 or avg_ta <= 0:
        return b.nm("Revenue or average total assets is not positive.")
    if avg_eq <= 0:
        return b.nm("Average equity is zero or negative - DuPont not meaningful.", "neg_equity")
    if ebit <= 0 or pbt <= 0:
        return b.nm("EBIT or PBT is not positive - interest and tax burdens are not interpretable.")
    parts = {"EBIT margin": ebit / rev, "Interest burden (PBT/EBIT)": pbt / ebit, "Tax burden (PAT/PBT)": pat / pbt,
             "Asset turnover": rev / avg_ta, "Equity multiplier": avg_ta / avg_eq}
    for k, v in parts.items():
        b.add(k, v)
    val = math.prod(parts.values())
    return b.ok(val, note="Uses reported EBIT so the five factors multiply back to ROE. " + " x ".join(
        f"{k.split(' (')[0].lower()} {v:.2f}" for k, v in parts.items()) + f" = {val:.1%}.")


# --------------------------------------------------------------------------- market perception
def _market(b: _B, field_: str, label: str) -> float:
    """Current price / market cap: only meaningful against the latest fiscal year."""
    if b.fy != b.fd.years[-1]:
        b.missing.append(f"{label} for {b.fy} (only the current market value is available, so valuation is "
                         f"shown for the latest year only)")
        return float("nan")
    v = b.fd.meta.get(field_)
    v = float(v) if v not in (None, "") else float("nan")
    b.add(f"{label} (at data load)", None if math.isnan(v) else v)
    if math.isnan(v):
        b.missing.append(f"{label} (not available from Yahoo)")
    return v


def _eps(b: _B, prior_n: int = 0) -> float:
    year = b.fy if prior_n == 0 else fy_label_from_year(fy_to_year(b.fy) - prior_n)
    if year not in b.fd.years:
        lab = f"EPS {year}"
        b.inputs[lab] = None
        b.missing.append(f"{lab} (year not in data)")
        return float("nan")
    pat, sh = b.fd.get("pat", year), b.fd.get("shares_outstanding", year)
    for k, v in ((f"PAT {year}", pat), (f"Shares outstanding (crore) {year}", sh)):
        b.inputs[k] = None if math.isnan(v) else v
        if math.isnan(v):
            b.missing.append(k)
    if math.isnan(pat) or math.isnan(sh) or sh <= 0:
        return float("nan")
    eps = pat / sh
    b.add(f"EPS {year} (Rs)", eps)
    return eps


def pe(fd, fy, s):
    b = _B(fd, fy, "pe")
    price, eps = _market(b, "price", "Share price"), _eps(b)
    if b.missing:
        return b.na()
    if eps <= 0:
        return b.nm("EPS is zero or negative (loss-making) - P/E not meaningful.")
    return b.ok(price / eps, note="Current price vs latest fiscal-year EPS (not trailing twelve months).")


def peg(fd, fy, s):
    b = _B(fd, fy, "peg")
    price, eps, eps3 = _market(b, "price", "Share price"), _eps(b), _eps(b, 3)
    if b.missing:
        return b.na()
    if eps <= 0 or eps3 <= 0:
        return b.nm("EPS is zero or negative in the start or end year - growth rate undefined.")
    cagr = (eps / eps3) ** (1 / 3) - 1
    b.add("3-year EPS CAGR", cagr)
    if cagr <= 0:
        return b.nm(f"3-year EPS growth is {cagr:.1%} (zero or negative) - PEG not meaningful.")
    pe_ = price / eps
    b.add("P/E", pe_)
    return b.ok(pe_ / (cagr * 100))


def div_yield(fd, fy, s):
    b = _B(fd, fy, "div_yield")
    mcap, div = _market(b, "market_cap_cr", "Market cap (Rs cr)"), b.v("dividends_paid")
    if b.missing:
        return b.na()
    if mcap <= 0:
        return b.nm("Market cap not positive.")
    return b.ok(div / mcap, note="Latest fiscal-year dividends paid vs current market cap.")


def p_ocf(fd, fy, s):
    b = _B(fd, fy, "p_ocf")
    mcap, cfo = _market(b, "market_cap_cr", "Market cap (Rs cr)"), b.v("cfo")
    if b.missing:
        return b.na()
    if cfo <= 0:
        return b.nm("CFO is zero or negative - P/OCF not meaningful.")
    return b.ok(mcap / cfo)


# --------------------------------------------------------------------------- growth and payout
def payout(fd, fy, s):
    b = _B(fd, fy, "payout")
    div, pat = b.v("dividends_paid"), b.v("pat")
    if b.missing:
        return b.na()
    if pat <= 0:
        return b.nm("PAT is zero or negative - payout not meaningful.")
    return b.ok(div / pat)


def retention(fd, fy, s):
    r = payout(fd, fy, s)
    if not r.ok:
        return RatioResult("retention", fy, None, r.status, r.inputs, r.reason)
    return RatioResult("retention", fy, 1 - r.value, OK, {**r.inputs, "Dividend payout": r.value})


def _growth(fd, fy, s, key, base_fn, base_label):
    base, ret = base_fn(fd, fy, s), retention(fd, fy, s)
    inputs = {**base.inputs, **ret.inputs, base_label: base.value, "Retention": ret.value}
    bad = [r for r in (base, ret) if not r.ok]
    if bad:
        return RatioResult(key, fy, None, NA if any(r.status == NA for r in bad) else NM, inputs,
                           "; ".join(f"{r.name}: {r.reason}" for r in bad))
    return RatioResult(key, fy, base.value * ret.value, OK, inputs)


def _roa(fd, fy, s):
    b = _B(fd, fy, "internal_growth")
    pat, avg_ta = b.v("pat"), _avg(b, "total_assets")
    if b.missing:
        return b.na()
    if avg_ta <= 0:
        return b.nm("Average total assets is not positive.")
    return b.ok(pat / avg_ta)


def internal_growth(fd, fy, s):
    return _growth(fd, fy, s, "internal_growth", _roa, "ROA")


def sustainable_growth(fd, fy, s):
    return _growth(fd, fy, s, "sustainable_growth", roe, "ROE")


# --------------------------------------------------------------------------- governance
def _rp(fd, fy, key, num_item, den_item):
    return _simple(fd, fy, key, [num_item], den_item)


def rp_revenue_share(fd, fy, s):
    return _rp(fd, fy, "rp_revenue_share", "related_party_revenue", "revenue")


def rp_receivables_share(fd, fy, s):
    return _rp(fd, fy, "rp_receivables_share", "related_party_receivables", "receivables")


def rp_advances_revenue(fd, fy, s):
    return _rp(fd, fy, "rp_advances_revenue", "related_party_advances", "revenue")


# --------------------------------------------------------------------------- book
SIMPLE_FNS = {
    "current_ratio": current_ratio, "cce": cce, "cce_reported": cce_reported, "ocf_ratio": ocf_ratio,
    "dir_current": dir_current, "dir_total": dir_total, "wc_days": wc_days,
    "dio": dio, "dso": dso, "dpo": dpo, "ccc": ccc, "inv_vs_sales": inv_vs_sales,
    "interest_coverage": interest_coverage, "interest_coverage_reported": interest_coverage_reported,
    "de_total": de_total, "de_borrowings": de_borrowings,
    "net_debt_ebitda": net_debt_ebitda, "net_debt_ebitda_reported": net_debt_ebitda_reported,
    "dscr": dscr, "dscr_ebit": dscr_ebit, "dscr_precapex": dscr_precapex,
    "op_ebit_margin": op_ebit_margin, "net_margin": net_margin, "roe": roe, "roce": roce, "roic": roic,
    "roiic": roiic, "dupont": dupont, "dupont_mod": dupont_mod, "asset_turnover": asset_turnover,
    "pe": pe, "peg": peg, "div_yield": div_yield, "p_ocf": p_ocf,
    "payout": payout, "retention": retention, "internal_growth": internal_growth,
    "sustainable_growth": sustainable_growth,
    "sloan": sloan, "bs_accrual": bs_accrual, "rec_vs_sales": rec_vs_sales,
    "piotroski": piotroski,
    "rp_revenue_share": rp_revenue_share, "rp_receivables_share": rp_receivables_share,
    "rp_advances_revenue": rp_advances_revenue,
}


def default_settings() -> dict:
    return {"include_noncurrent": config.LIQUIDITY_INCLUDE_NONCURRENT}


def _fy_ranges(years: list[str]) -> str:
    ys = sorted(fy_to_year(y) for y in years)
    runs, start = [], ys[0]
    for a, b in zip(ys, ys[1:] + [None]):
        if b is None or b != a + 1:
            runs.append(fy_label_from_year(start) if start == a
                        else f"{fy_label_from_year(start)}-{fy_label_from_year(a)}")
            start = b
    return ", ".join(runs)


@dataclass
class RatioBook:
    years: list[str]
    results: dict                     # (key, fy) -> RatioResult
    altman_key: str
    altman_reason: str
    is_financial: bool = False
    settings: dict = field(default_factory=dict)

    def get(self, key: str, fy: str) -> RatioResult:
        return self.results[(key, fy)]

    def value(self, key: str, fy: str) -> float | None:
        r = self.results.get((key, fy))
        return r.value if r is not None and r.ok else None

    def is_scoreable(self, key: str) -> bool:
        """Stage 3 must skip ratios tagged Not valid."""
        return applicability(key, self.is_financial)[0] != "Not valid"

    @property
    def score_label(self) -> str:
        return "Indicative only" if self.is_financial else ""

    def table(self, groups: list[str] | None = None, main: bool = False, note_year: str | None = None) -> pd.DataFrame:
        """Year-by-year display table.
        main=True: headline rows only (no Beneish components, only the Altman variant used) plus a
        'Reason / note' column for note_year (default: latest year)."""
        note_year = note_year or (self.years[-1] if self.years else None)
        rows = {}
        for key, (name, group, *_rest) in METRICS.items():
            if groups and group not in groups:
                continue
            if main and key in MAIN_TABLE_EXCLUDE:
                continue
            if key == "altman":
                label = f"Altman {ALTMAN_SHORT[self.altman_key]} - variant used"
            else:
                label = name + ("  ◀ used" if key == self.altman_key else "")
            app = applicability(key, self.is_financial)[0]
            row = [app] + [self.results[(key, fy)].display() for fy in self.years]
            if main:
                row.append(self.results[(key, note_year)].explanation if note_year else "-")
            rows[(group, label)] = row
        cols = ["Applicability"] + self.years + ([f"Reason / note ({note_year})"] if main else [])
        df = pd.DataFrame.from_dict(rows, orient="index", columns=cols)
        df.index = pd.MultiIndex.from_tuples(df.index, names=["Group", "Ratio"])
        return df

    def main_rows(self, year: str, block: str | None = None) -> list[dict]:
        """Rows for the Ratios tab, grouped (config.MAIN_BLOCKS). block: restrict to one block title."""
        out = []
        for b in config.MAIN_BLOCKS:
            if block and b["title"] != block:
                continue
            for pillar, keys in b["groups"]:
                out += self._rows(b["title"], pillar, keys, year)
        return out

    def _rows(self, block_title, pillar, keys, year):
        out = []
        if True:
            for key in keys:
                r = self.results[(key, year)]
                label, colour = status(r, key)
                if pillar in config.NEUTRAL_GROUPS:
                    colour = "grey"
                out.append({"block": block_title, "pillar": pillar, "key": key,
                            "label": config.MAIN_TABLE_LABELS[key],
                            "scored": block_title == config.MAIN_BLOCKS[0]["title"]
                            and key not in config.CONTEXT_IN_SCORED_BLOCK,
                            "values": {fy: self.results[(key, fy)].display() for fy in self.years},
                            "status": label, "colour": colour,
                            "applicability": r.applicability, "applicability_reason": r.applicability_reason})
        return out

    def detail_keys(self) -> list[str]:
        """Keys for the 'More detail' expanders (everything except breakdown-only rows)."""
        main = [k for _, ks in config.MAIN_TABLE for k in ks]
        breakdown = set(BENEISH_PARTS) - {"dsri"} | {"beneish_m", "altman", "altman_z1", "altman_z2", "piotroski",
                                                     "dupont", "dupont_mod", "roiic"}
        rp = {"rp_revenue_share", "rp_receivables_share", "rp_advances_revenue"}
        extra = [k for k in METRICS if k not in main and k not in breakdown
                 and (k not in rp or any(self.results[(k, fy)].ok for fy in self.years))]
        return main + extra

    # ----------------------------------------------------------------- expander tables
    def beneish_components_table(self) -> pd.DataFrame:
        """Rows = components (+ constant and M-Score); per year: value and contribution (weight x value)."""
        rows = []
        for k in BENEISH_PARTS:
            row = {"Component": METRICS[k][0], "Weight": f"{BENEISH_COEF[k]:+.3f}"}
            for fy in self.years:
                r = self.results[(k, fy)]
                row[f"{fy} value"] = f"{r.value:.3f}" if r.ok else r.status
                row[f"{fy} contribution"] = f"{BENEISH_COEF[k] * r.value:+.3f}" if r.ok else "-"
            rows.append(row)
        const = {"Component": "Constant", "Weight": f"{BENEISH_COEF['const']:+.2f}"}
        total = {"Component": "M-Score (sum)", "Weight": ""}
        for fy in self.years:
            m = self.results[("beneish_m", fy)]
            const[f"{fy} value"], const[f"{fy} contribution"] = "", f"{BENEISH_COEF['const']:+.3f}"
            total[f"{fy} value"] = ""
            total[f"{fy} contribution"] = f"{m.value:+.3f}" if m.ok else "not computed"
        return pd.DataFrame(rows + [const, total]).set_index("Component")

    def altman_details_table(self) -> pd.DataFrame:
        """Both variants and the X1-X5 inputs, year by year."""
        rows = {}
        for x in ("X1", "X2", "X3", "X4", "X5"):
            rows[x] = {}
            for fy in self.years:
                val = None
                for key in ("altman_z1", "altman_z2"):
                    r = self.results[(key, fy)]
                    if r.ok and x in r.inputs:
                        val = r.inputs[x]
                        break
                rows[x][fy] = f"{val:.4f}" if val is not None else "data unavailable"
        for key in ("altman_z1", "altman_z2"):
            label = f"Altman {ALTMAN_SHORT[key]}" + ("  ◀ used" if key == self.altman_key else "")
            rows[label] = {fy: self.results[(key, fy)].display() for fy in self.years}
        # contribution of each factor to the variant used (coefficient x value) - shows what drives the score
        for x, coef in ALTMAN_COEF[self.altman_key].items():
            rows[f"Contribution {x} ({coef:g} x {x})"] = {
                fy: (f"{coef * self.results[(self.altman_key, fy)].inputs[x]:+.3f}"
                     if self.results[(self.altman_key, fy)].ok else "-") for fy in self.years}
        return pd.DataFrame(rows).T

    def piotroski_table(self) -> pd.DataFrame:
        """Nine criteria x years, 'Pass (detail)' / 'Fail (detail)' / 'data unavailable'."""
        names, data = [], {}
        for fy in self.years:
            r = self.results[("piotroski", fy)]
            for d in r.details:
                if d["Criterion"] not in names:
                    names.append(d["Criterion"])
                data.setdefault(fy, {})[d["Criterion"]] = (
                    d["Result"] if d["Result"] == NA else f"{d['Result']} ({d['Detail']})")
        df = pd.DataFrame(data, index=names).fillna(NA)
        df.loc["F-Score"] = [self.results[("piotroski", fy)].display() for fy in df.columns]
        return df

    def numeric_table(self) -> pd.DataFrame:
        return pd.DataFrame({fy: {k: self.value(k, fy) for k in METRICS} for fy in self.years})

    def distress_signals(self) -> list[RatioResult]:
        return [r for r in self.results.values() if r.distress]

    def distress_summary(self) -> list[str]:
        """Grouped, de-duplicated lines, e.g. 'FY23-FY26: shareholders' equity negative, so Debt/Equity ...'."""
        lines = []
        codes = sorted({r.distress_code for r in self.distress_signals() if r.distress_code})
        for code in codes:
            per_year = {}
            for fy in self.years:
                keys = tuple(sorted(k for k in METRICS if (k, fy) in self.results
                                    and self.results[(k, fy)].distress_code == code and k != "dir"))
                if keys:
                    per_year[fy] = keys
            # one line per run of consecutive years; the affected ratios are the union over the run
            # (e.g. Debt/Equity in the first year, plus ROE and DuPont once a prior year exists)
            groups: list[tuple[list[str], tuple]] = []
            for fy in self.years:
                if fy not in per_year:
                    continue
                if groups and fy_to_year(fy) - fy_to_year(groups[-1][0][-1]) == 1:
                    groups[-1][0].append(fy)
                    groups[-1] = (groups[-1][0], tuple(sorted(set(groups[-1][1]) | set(per_year[fy]))))
                else:
                    groups.append(([fy], per_year[fy]))
            for yrs, keys in groups:
                names = [SHORT.get(k, METRICS[k][0]) for k in keys]
                joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
                text = config.DISTRESS_TEXT.get(code, code + ": {ratios} {verb} not meaningful").format(
                    ratios=joined, verb="is" if len(names) == 1 else "are")
                lines.append(f"{_fy_ranges(yrs)}: {text}")
        return lines

    def detail_frame(self, key: str) -> pd.DataFrame:
        rows = []
        for fy in self.years:
            r = self.results[(key, fy)]
            rows.append({"Year": fy, "Value": r.display(), "Status": r.status,
                         "Applicability": f"{r.applicability} - {r.applicability_reason}",
                         "Inputs": "; ".join(f"{k} = {_fmt_in(v)}" for k, v in r.inputs.items()) or "-",
                         "Reason / note": r.explanation})
        return pd.DataFrame(rows)


def _fmt_in(v):
    if v is None:
        return "data unavailable"
    if isinstance(v, (int, float)) and abs(v) < 10 and v != int(v):
        return f"{v:,.4f}"
    return f"{v:,.2f}"


def compute_all(fd: FinancialData, settings: dict | None = None) -> RatioBook:
    s = {**default_settings(), **(settings or {})}
    is_fin = bool(fd.meta.get("is_financial"))
    results = {}
    for fy in fd.years:
        for key, fn in SIMPLE_FNS.items():
            results[(key, fy)] = fn(fd, fy, s)
        results[("dir", fy)] = dir_selected(fd, fy, s, results[("dir_current", fy)], results[("dir_total", fy)])
        comps = {k: fn(fd, fy) for k, fn in COMPONENT_FN.items()}
        for k, r in comps.items():
            results[(k, fy)] = r
        results[("beneish_m", fy)] = beneish(fd, fy, comps)
        results[("altman_z1", fy)] = altman(fd, fy, "altman_z1")
        results[("altman_z2", fy)] = altman(fd, fy, "altman_z2")
    akey, areason = altman_variant(fd.meta)
    for fy in fd.years:
        src = results[(akey, fy)]
        note = (src.note + " " + areason).strip()
        if akey == "altman_z2" and is_capital_intensive(fd, fy):
            note += " " + config.ALTMAN_Z2_ASSET_HEAVY_NOTE
            if src.ok:
                src.note = (src.note + " " + config.ALTMAN_Z2_ASSET_HEAVY_NOTE).strip()
        results[("altman", fy)] = RatioResult("altman", fy, src.value, src.status, src.inputs, src.reason,
                                              src.distress, note, src.details, src.distress_code)
    # P/E vs P/OCF cross-check on the P/OCF detail row
    for fy in fd.years:
        p1, p2 = results[("pe", fy)], results[("p_ocf", fy)]
        if p1.ok and p2.ok:
            verdict = ("P/OCF above P/E: profits are not converting to cash." if p2.value > p1.value else
                       "P/OCF below P/E: cash earnings exceed accounting profit.")
            p2.note = _join(p2.note, f"P/E {p1.value:.1f}x vs P/OCF {p2.value:.1f}x - {verdict}")
    # applicability + interpretation for every result
    for (key, fy), r in results.items():
        r.applicability, r.applicability_reason = applicability(key, is_fin)
        interp_key = akey if key == "altman" else ("dir_total" if key == "dir" else key)
        v = r.extra.get("scaled_9", r.value) if key == "piotroski" else r.value
        r.interpretation = interpret(interp_key, v) if r.ok else ""
    return RatioBook(list(fd.years), results, akey, areason, is_fin, s)


# --------------------------------------------------------------------------- automatic flags
@dataclass
class Flag:
    code: str
    severity: str          # "red" | "amber" | "info"
    years: str
    text: str
    confirmable: bool = False   # Stage 3: analyst confirms or dismisses before an override applies


def captive_finance_years(fd: FinancialData) -> list[str]:
    """Years with consolidated CFO < 0 while operating profit > 0 (empty if fewer than the configured minimum)."""
    years = []
    for fy in fd.years:
        cfo = fd.get("cfo", fy)
        op = fd.get("operating_ebit", fy)
        op = fd.get("ebit", fy) if math.isnan(op) else op
        if not math.isnan(cfo) and cfo < 0 and not math.isnan(op) and op > 0:
            years.append(fy)
    return years if len(years) >= config.CAPTIVE_FINANCE_MIN_YEARS else []


def is_regulated_utility(meta: dict) -> bool:
    sector = str(meta.get("sector") or "").lower()
    industry = str(meta.get("industry") or "").lower()
    return sector == "utilities" or any(k in industry for k in config.UTILITY_KEYWORDS)


def is_capital_intensive(fd: FinancialData, fy: str) -> bool:
    ind = str(fd.meta.get("industry") or "").lower() + " " + str(fd.meta.get("sector") or "").lower()
    if any(k in ind for k in config.CAPITAL_INTENSIVE_INDUSTRY_KEYWORDS):
        return True
    ppe, ta = fd.get("ppe", fy), fd.get("total_assets", fy)
    return not math.isnan(ppe) and not math.isnan(ta) and ta > 0 and ppe / ta > config.CAPITAL_INTENSIVE_PPE_SHARE


def red_flags(fd: FinancialData, book: RatioBook) -> list[Flag]:
    flags: list[Flag] = []
    years = fd.years

    # J. inventory growth vs revenue growth
    if book.is_scoreable("inv_vs_sales"):
        hits = [(fy, book.get("inv_vs_sales", fy)) for fy in years if book.get("inv_vs_sales", fy).ok
                and book.value("inv_vs_sales", fy) > config.INVENTORY_GROWTH_GAP]
        for fy, r in hits:
            inv, rev, ta = fd.get("inventory", fy), fd.get("revenue", fy), fd.get("total_assets", fy)
            pct = config.INVENTORY_MATERIALITY_PCT
            material = any(not math.isnan(base) and base > 0 and inv > pct * base for base in (rev, ta))
            text = (f"{fy}: inventory grew {r.inputs['Inventory growth']:+.1%} vs revenue "
                    f"{r.inputs['Revenue growth']:+.1%} (gap {r.value * 100:.1f}pp; threshold "
                    f"{config.INVENTORY_GROWTH_GAP * 100:.0f}pp)")
            if material:
                flags.append(Flag("inventory_build", "red", fy, text + f"; inventory Rs {inv:,.0f} cr is material - "
                                  "check for slow-moving stock."))
            else:
                flags.append(Flag("inventory_build_immaterial", "info", fy, text + f"; inventory Rs {inv:,.1f} cr is "
                                  f"under {pct:.0%} of revenue and of total assets, so this is shown as information."))

    # K. related-party signals, kept separate
    rp = [("rp_revenue_share", config.RP_REVENUE_FLAG, "related-party revenue", "of revenue"),
          ("rp_receivables_share", config.RP_RECEIVABLES_FLAG, "related-party receivables", "of trade receivables"),
          ("rp_advances_revenue", config.RP_ADVANCES_FLAG, "related-party loans & advances", "of revenue")]
    for key, thr, what, base in rp:
        for fy in years:
            v = book.value(key, fy)
            if v is None:
                continue          # signal shown only when related-party data exists
            if v > thr:
                flags.append(Flag(key, "red", fy, f"{fy}: {what} = {v:.1%} {base} (threshold {thr:.0%})."
                                  + (" Brief rule: escalate for forensic review." if key == "rp_advances_revenue"
                                     else "")
                                  + (f" This is {config.RP_RECEIVABLES_NOTE}." if key == "rp_receivables_share"
                                     else "")))
            else:
                flags.append(Flag(key, "info", fy, f"{fy}: {what} = {v:.1%} {base} - below the {thr:.0%} "
                                                   f"threshold."))

    # E. CFO negative while operating profit positive (captive finance arms)
    cfo = {fy: fd.get("cfo", fy) for fy in years}
    op = {fy: (fd.get("operating_ebit", fy) if not math.isnan(fd.get("operating_ebit", fy))
               else fd.get("ebit", fy)) for fy in years}
    captive_years = captive_finance_years(fd)
    if captive_years:
        flags.append(Flag("captive_finance", "info", _fy_ranges(captive_years),
                          f"{_fy_ranges(captive_years)}: CFO negative while operating profit is positive. "
                          f"Consolidated CFO may include a captive finance subsidiary's loan-book growth "
                          f"(e.g. vehicle makers)."))

    # Stage 3 override trigger, as a confirmable flag (never a silent cap)
    run: list[str] = []
    best: list[str] = []
    for fy in years:
        if not math.isnan(cfo[fy]) and cfo[fy] < 0 and (not run or fy_to_year(fy) - fy_to_year(run[-1]) == 1):
            run.append(fy)
        elif not math.isnan(cfo[fy]) and cfo[fy] < 0:
            run = [fy]
        else:
            run = []
        if len(run) > len(best):
            best = list(run)
    if len(best) >= config.CFO_NEGATIVE_YEARS:
        extra = " A captive-finance effect may explain it (see note)." if captive_years else ""
        flags.append(Flag("cfo_negative", "amber", _fy_ranges(best),
                          f"CFO negative in {_fy_ranges(best)} ({len(best)} consecutive years). The Stage 3 "
                          f"score cap (40) will apply only if you confirm it.{extra}", confirmable=True))

    # Data quality: possible one-off or mislabelled item (after the exceptional-items adjustment)
    for fy, msg in D_one_off(fd):
        flags.append(Flag("one_off", "amber", fy, msg))
    for fy, msg in D_cash_one_off(fd):
        flags.append(Flag("one_off_cash", "amber", fy, msg))

    # M-Score unavailable (Stage 3 re-weights Earnings Integrity)
    latest = years[-1] if years else None
    if latest and book.is_scoreable("beneish_m") and not book.get("beneish_m", latest).ok:
        missing = [k for k, v in book.get("beneish_m", latest).inputs.items() if v is None]
        flags.append(Flag("mscore_unavailable", "info", latest,
                          f"M-Score unavailable for {latest}: {', '.join(missing) or 'some components'} cannot be "
                          f"computed (missing inputs or prior year). Earnings Integrity is re-weighted across the "
                          f"remaining ratios."))
    return flags
