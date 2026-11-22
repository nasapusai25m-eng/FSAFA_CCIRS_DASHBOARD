"""
CCIRS central configuration.

Stage 1 holds the data-layer settings. Later stages add the ratio threshold
tables, pillar weights and override rules to this same file, so every
judgement-based number in the project is editable in one place.
"""

# ---------------------------------------------------------------- units
CRORE = 1e7  # 1 crore = 10,000,000

# Divisors that convert a value in the stated unit INTO Rs crore.
UPLOAD_UNITS = {
    "Rs crore": 1.0,
    "Rs lakh": 100.0,            # 100 lakh = 1 crore
    "Rs million": 10.0,          # 10 million = 1 crore
    "Rs (absolute)": CRORE,
}
DEFAULT_UPLOAD_UNIT = "Rs crore"

# ---------------------------------------------------------- validation
BALANCE_TOLERANCE_WARN = 0.02    # |TA - (TL + Equity + Minority)| / TA above 2% -> warning
BALANCE_TOLERANCE_ERROR = 0.10   # above 10% -> error (scoring blocked for that data set)
YOY_JUMP_FACTOR = 10.0           # YoY change above 10x (or below 1/10x) -> likely unit/typing error
MATERIALITY_PCT = 0.01          # a 10x jump is a warning only if the amount > 1% of total assets or revenue
YOY_MIN_BASE = 1.0               # ignore jumps where the base value is below Rs 1 crore (noise)
UNIT_RATIO_OK = (0.5, 2.0)       # median upload/Yahoo ratio outside this band -> unit mismatch error
CELL_DIFF_INFO = 0.05            # upload vs Yahoo difference above 5% on a cell -> listed for review
MAX_PLAUSIBLE_CRORE = 5_000_000  # Rs 50 lakh crore; larger usually means absolute rupees typed as crore

# Key rows a year must have to count towards "data depth".
DEPTH_KEY_ITEMS = ("revenue", "total_assets")

# ------------------------------------------------- unsupported sectors
# Banks, NBFCs and insurers: Beneish, Altman and Sloan are not meaningful.
FINANCIAL_SECTORS = {"financial services", "financial"}
FINANCIAL_INDUSTRY_KEYWORDS = (
    "bank", "insurance", "credit services", "capital markets", "asset management",
    "mortgage", "financial conglomerate", "financial data", "shell companies",
    "nbfc", "housing finance",
)
FINANCIAL_NAME_KEYWORDS = (
    " bank", "bank ", "insurance", "assurance", "finance ltd", "finance limited",
    "financial services", "housing finance", "capital ltd", "fincorp", "finserv",
)

# --------------------------------------------------- Altman variant choice
# Z' (manufacturing, 5 factors incl. Sales/TA) when the Yahoo industry matches one of these keywords;
# otherwise Z'' (non-manufacturing, 4 factors, no asset-turnover term, no +3.25 constant).
MANUFACTURING_INDUSTRY_KEYWORDS = (
    "auto manufacturers", "auto parts", "steel", "aluminum", "copper", "metal fabrication", "chemicals",
    "specialty chemicals", "agricultural inputs", "building materials", "cement", "paper", "packaging",
    "textile", "apparel manufacturing", "footwear", "furniture", "household & personal products",
    "packaged foods", "beverages", "tobacco", "confectioners", "drug manufacturers", "medical devices",
    "electrical equipment", "electronic components", "specialty industrial machinery", "farm & heavy",
    "aerospace & defense", "tools & accessories", "consumer electronics", "semiconductors",
    "oil & gas refining", "lumber", "glass", "rubber", "plastics", "tyres", "tires",
)
MANUFACTURING_SECTORS = {"basic materials"}   # whole sector treated as manufacturing

BENEISH_FLAG = -2.22          # M above this -> "likely manipulator" zone (8-variable model)

# =========================================================== Stage 2 revisions
# ---------------------------------------------------- liquidity settings
# True: DIR and net debt include NON-CURRENT liquid investments (e.g. debt mutual funds held long term).
# Both DIR versions are always computed; this decides which one is labelled "feeds the score".
LIQUIDITY_INCLUDE_NONCURRENT = True
# If EBITDA cannot be adjusted to operating EBITDA (other income not reported), fall back to reported
# EBITDA and say so in the note.
FALLBACK_TO_REPORTED_EBITDA = True

LEASE_HEAVY_SHARE = 0.30          # lease liabilities > 30% of total debt -> CCE lease note
INVENTORY_GROWTH_GAP = 0.20       # inventory growth - revenue growth > 20pp -> red flag
CAPTIVE_FINANCE_MIN_YEARS = 2     # CFO < 0 while operating EBIT > 0 in >= 2 years -> captive-finance note
CFO_NEGATIVE_YEARS = 2            # consecutive negative-CFO years that trigger the (confirmable) override

# related-party signals (kept separate)
RP_REVENUE_FLAG = 0.20            # related-party revenue / revenue
RP_RECEIVABLES_FLAG = 0.25        # related-party receivables / trade receivables
RP_ADVANCES_FLAG = 0.50           # related-party loans & advances / revenue (brief: escalate above 50%)

# ---------------------------------------------------- applicability tags
# Every ratio is Valid for non-financial companies unless listed here.
NONFIN_APPLICABILITY = {
    "piotroski": ("Limited", "Simplified: year-end assets; designed for value-stock screening."),
    "cce_reported": ("Limited", "Secondary figure: reported EBITDA includes other income and associates."),
    "net_debt_ebitda_reported": ("Limited", "Secondary figure: reported EBITDA includes non-operating income."),
    "interest_coverage_reported": ("Limited", "Secondary figure: reported EBIT includes non-operating income."),
}
NONFIN_DEFAULT = ("Valid", "Standard measure for non-financial companies.")

FIN_NOT_VALID_REASON = {
    "liquidity": "Banks/NBFCs have no operating working-capital cycle; liquidity is regulated (LCR/ALM).",
    "cash": "Lending and deposit flows run through CFO, so CFO-based ratios are not comparable.",
    "accrual": "Loan-book growth dominates accruals; the model was not built for lenders.",
    "leverage": "Debt is the raw material of a lender; leverage is judged by capital adequacy, not D/E.",
    "coverage": "Interest is a cost of goods sold for a lender; use net interest margin instead.",
    "model": "Model excludes financial companies by design (Beneish/Altman samples).",
}
FIN_APPLICABILITY = {
    **{k: ("Not valid", FIN_NOT_VALID_REASON["liquidity"]) for k in (
        "current_ratio", "wc_days", "dir", "dir_current", "dir_total", "ccc", "dio", "dso", "dpo",
        "inv_vs_sales")},
    **{k: ("Not valid", FIN_NOT_VALID_REASON["cash"]) for k in (
        "ocf_ratio", "cce", "cce_reported", "dscr", "dscr_ebit")},
    **{k: ("Not valid", FIN_NOT_VALID_REASON["accrual"]) for k in ("sloan", "bs_accrual", "tata")},
    **{k: ("Not valid", FIN_NOT_VALID_REASON["leverage"]) for k in (
        "de_total", "de_borrowings", "net_debt_ebitda", "net_debt_ebitda_reported", "lvgi")},
    **{k: ("Not valid", FIN_NOT_VALID_REASON["coverage"]) for k in (
        "interest_coverage", "interest_coverage_reported")},
    **{k: ("Not valid", FIN_NOT_VALID_REASON["model"]) for k in (
        "beneish_m", "dsri", "gmi", "aqi", "sgi", "depi", "sgai", "altman", "altman_z1", "altman_z2")},
    "piotroski": ("Limited", "Profitability tests work; leverage, liquidity and margin tests do not suit lenders."),
    # new context constructs
    "roe": ("Valid", "Core profitability measure for lenders."),
    "pe": ("Valid", "Standard valuation measure for listed banks."),
    "peg": ("Valid", "EPS-based; works for lenders."),
    "div_yield": ("Valid", "Dividend yield is comparable across sectors."),
    "payout": ("Valid", "Payout is meaningful; regulators cap bank payouts."),
    "retention": ("Valid", "Retention funds capital adequacy for lenders."),
    "sustainable_growth": ("Limited", "ROE x retention; bank growth is capped by capital adequacy, not just retention."),
    "internal_growth": ("Limited", "ROA-based; lender ROA is structurally low."),
    "net_margin": ("Limited", "Revenue for a bank is interest + fee income; margins are not comparable to industrials."),
    "p_ocf": ("Not valid", "Operating cash flow is not meaningful for banks (loan and deposit flows sit in CFO)."),
    "dupont": ("Limited", "Equity multiplier for a bank reflects regulated leverage."),
    **{k: ("Not valid", "Operating EBIT, capital employed and asset turnover do not describe a lender's economics.")
       for k in ("op_ebit_margin", "roce", "roic", "asset_turnover", "roiic", "dupont_mod", "dscr_precapex")},
    "rec_vs_sales": ("Limited", "For a lender 'receivables' are mostly loans; growth gap is only indicative."),
}
FIN_DEFAULT = ("Limited", "Not designed for financial companies; indicative only.")
FINANCIAL_BANNER = ("Financial company: several ratios are not valid for banks/NBFCs/insurers. This view is "
                    "indicative only and not suitable for credit decisions.")

# ---------------------------------------------------- interpretation texts
# key -> list of (upper bound, text). The first band whose upper bound is ABOVE the value applies.
INF = float("inf")
INTERPRETATION = {
    "current_ratio": [(1.0, "below 1.0x: current liabilities exceed current assets"),
                      (1.33, "1.0-1.33x: thin cushion, below the usual bank comfort of 1.33x"),
                      (2.0, "1.33-2.0x: comfortable"),
                      (INF, "above 2.0x: strong, but check for slow-moving current assets")],
    "cce": [(0.0, "CFO negative: profits not converting to cash"),
            (0.40, "below 40%: major concern - most EBITDA is not reaching cash"),
            (0.60, "40-60%: weak cash conversion"),
            (0.80, "60-80%: acceptable"),
            (1.00, "within the strong range (80-100%)"),
            (INF, "above 100%: old receivables collected, or lease accounting (Ind AS 116)")],
    "ocf_ratio": [(0.0, "CFO negative: current liabilities not covered by operating cash"),
                  (0.20, "below 0.2x: weak"), (0.40, "0.2-0.4x: moderate"), (1.0, "0.4-1.0x: healthy"),
                  (INF, "above 1.0x: CFO alone covers current liabilities")],
    "dir": [(30, "under 30 days: very thin liquid buffer"), (60, "30-60 days: thin"),
            (90, "60-90 days: adequate"), (150, "90-150 days: comfortable"),
            (INF, "over 150 days: large liquid buffer")],
    "wc_days": [(0, "negative: funded by suppliers or customer advances (normal for OEMs/FMCG; risky if sales fall)"),
                (30, "0-30 days: lean"), (60, "30-60 days: typical"), (90, "60-90 days: stretched"),
                (INF, "over 90 days: very heavy - check receivable and inventory ageing")],
    "dio": [(30, "under 30 days: lean inventory"), (60, "30-60 days: normal"), (120, "60-120 days: high"),
            (INF, "over 120 days: obsolescence or channel-stuffing risk")],
    "dso": [(30, "under 30 days: fast collection"), (60, "30-60 days: normal"), (90, "60-90 days: slow"),
            (120, "90-120 days: very slow - ask for ageing"), (INF, "over 120 days: collection risk")],
    "dpo": [(30, "under 30 days: pays suppliers quickly"), (90, "30-90 days: normal"),
            (150, "90-150 days: stretched"), (INF, "over 150 days: very stretched - possible liquidity strain")],
    "ccc": [(0, "negative: suppliers fund operations"), (45, "0-45 days: efficient"),
            (90, "45-90 days: moderate"), (150, "90-150 days: long"), (INF, "over 150 days: very long")],
    "interest_coverage": [(1.0, "below 1x: operating profit does not cover interest"),
                          (1.5, "1-1.5x: weak"), (3.0, "1.5-3x: adequate"), (6.0, "3-6x: comfortable"),
                          (INF, "above 6x: strong")],
    "de": [(0.5, "below 0.5x: low leverage"), (1.0, "0.5-1.0x: moderate"), (2.0, "1-2x: elevated"),
           (3.0, "2-3x: high"), (INF, "above 3x: very high")],
    "net_debt_ebitda": [(0.0, "net cash: liquid funds exceed debt"), (1.0, "0-1x: low"),
                        (2.5, "1-2.5x: moderate"), (4.0, "2.5-4x: elevated (typical covenant 3-3.5x)"),
                        (INF, "above 4x: high")],
    "dscr": [(0.0, "negative: CFO after capex does not cover anything - relies on refinancing"),
             (1.0, "below 1x: free cash does not cover debt service"), (1.25, "1-1.25x: thin"),
             (2.0, "1.25-2x: adequate"), (INF, "above 2x: strong")],
    "sloan": [(0.0, "negative: cash earnings exceed reported profit (high quality)"),
              (0.05, "0-5%: low accruals"), (0.10, "5-10%: moderate accruals - watch"),
              (0.20, "10-20%: profits running ahead of cash"),
              (INF, "above 20%: very high accruals - warrants investigation")],
    "bs_accrual": [(0.0, "net operating assets shrinking"), (0.05, "0-5%: low"), (0.10, "5-10%: moderate"),
                   (0.20, "10-20%: high balance-sheet accruals"),
                   (INF, "above 20%: very high - operating assets growing fast; check capitalisation")],
    "dsri": [(0.9, "receivables falling relative to sales"), (1.1, "stable receivables-to-sales"),
             (1.465, "rising - watch"), (INF, "above 1.465 (Beneish manipulator mean): receivables outpacing sales")],
    "rec_vs_sales": [(0.0, "receivables growing slower than sales"), (0.10, "slightly faster than sales"),
                     (0.20, "10-20pp faster than sales - watch"),
                     (INF, "over 20pp faster than sales - check ageing and channel stuffing")],
    "inv_vs_sales": [(0.0, "inventory growing slower than sales"), (0.10, "slightly faster than sales"),
                     (0.20, "10-20pp faster than sales - watch"),
                     (INF, "over 20pp faster than sales - obsolescence or stocking risk")],
    "beneish_m": [(-2.22, "below -2.22: no manipulation pattern"),
                  (-1.78, "grey zone (-2.22 to -1.78): partial concern"),
                  (INF, "above -1.78: manipulation pattern - warrants investigation")],
    "gmi": [(1.0, "gross margin improved"), (1.193, "margin slipped slightly"),
            (INF, "above 1.193 (manipulator mean): margin deterioration")],
    "aqi": [(1.0, "fewer soft assets"), (1.254, "soft assets rising slightly"),
            (INF, "above 1.254 (manipulator mean): more costs possibly capitalised")],
    "sgi": [(1.0, "sales shrinking"), (1.2, "moderate growth"), (1.607, "strong growth"),
            (INF, "above 1.607 (manipulator mean): very rapid growth - pressure to sustain")],
    "depi": [(1.0, "depreciation rate rising or stable"), (1.077, "depreciation slowing slightly"),
             (INF, "above 1.077 (manipulator mean): depreciation slowed - check useful-life changes")],
    "sgai": [(1.0, "SG&A falling relative to sales"), (1.041, "SG&A stable"),
             (INF, "above 1.041 (manipulator mean): SG&A rising faster than sales")],
    "tata": [(0.0, "CFO exceeds profit"), (0.031, "low accruals"),
             (INF, "above 0.031 (manipulator mean): high accruals")],
    "lvgi": [(1.0, "leverage falling"), (1.111, "leverage stable"),
             (INF, "above 1.111 (manipulator mean): leverage rising")],
    "altman_z2": [(1.10, "below 1.10: distress zone"), (2.60, "1.10-2.60: grey zone"),
                  (INF, "above 2.60: safe zone")],
    "altman_z1": [(1.23, "below 1.23: distress zone"), (2.90, "1.23-2.90: grey zone"),
                  (INF, "above 2.90: safe zone")],
    "piotroski": [(3.5, "0-3: weak fundamentals"), (6.5, "4-6: average"), (INF, "7-9: strong fundamentals")],
    # context constructs (PROPOSED - review)
    "op_ebit_margin": [(0.0, "operating loss"), (0.05, "under 5%: thin operating margin"),
                       (0.10, "5-10%: moderate"), (0.20, "10-20%: healthy"), (INF, "above 20%: strong pricing power")],
    "net_margin": [(0.0, "net loss"), (0.03, "under 3%: thin"), (0.08, "3-8%: moderate"), (0.15, "8-15%: healthy"),
                   (INF, "above 15%: strong")],
    "roe": [(0.0, "negative return to shareholders"), (0.08, "under 8%: below typical cost of equity"),
            (0.12, "8-12%: moderate"), (0.20, "12-20%: good"), (INF, "above 20%: strong (check leverage via DuPont)")],
    "roce": [(0.0, "negative"), (0.08, "under 8%: below cost of capital"), (0.12, "8-12%: moderate"),
             (0.20, "12-20%: good"), (INF, "above 20%: strong")],
    "roic": [(0.0, "negative"), (0.08, "under 8%: destroying value vs typical cost of capital"),
             (0.12, "8-12%: around cost of capital"), (0.20, "12-20%: value-creating"), (INF, "above 20%: strong moat")],
    "roiic": [(0.0, "new capital earned negative returns"), (0.08, "under 8%: new capital earns below cost of capital"),
              (0.20, "8-20%: new capital earns reasonable returns"), (INF, "above 20%: new capital is highly productive")],
    "asset_turnover": [(0.5, "under 0.5x: asset-heavy"), (1.0, "0.5-1.0x: moderate"), (1.5, "1.0-1.5x: good"),
                       (INF, "above 1.5x: asset-light / high turnover")],
    "pe": [(0, "negative earnings"), (10, "under 10x: market prices it at a discount"), (20, "10-20x: moderate valuation"),
           (35, "20-35x: premium valuation"), (INF, "above 35x: rich valuation - high growth expected")],
    "peg": [(1.0, "under 1: cheap relative to EPS growth"), (2.0, "1-2: fair for its growth"),
            (INF, "above 2: expensive relative to growth")],
    "div_yield": [(0.005, "under 0.5%: minimal"), (0.02, "0.5-2%: modest"), (0.04, "2-4%: good"), (INF, "above 4%: high")],
    "p_ocf": [(10, "under 10x cash flow"), (20, "10-20x cash flow"), (35, "20-35x cash flow"), (INF, "above 35x cash flow")],
    "payout": [(0.0001, "no dividend"), (0.30, "under 30%: retains most profit"), (0.60, "30-60%: balanced"),
               (1.0, "60-100%: high payout"), (INF, "above 100%: paying out more than it earns")],
    "retention": [(0.0, "negative: dividends exceed profit"), (0.40, "under 40% retained"),
                  (0.70, "40-70% retained"), (INF, "above 70% retained")],
    "internal_growth": [(0.0, "no self-funded growth"), (0.05, "under 5%: slow"), (0.12, "5-12%: moderate"),
                        (INF, "above 12%: fast self-funded growth")],
    "sustainable_growth": [(0.0, "no self-funded growth"), (0.05, "under 5%: slow"), (0.12, "5-12%: moderate"),
                           (INF, "above 12%: fast growth without new equity")],
    "rp_revenue_share": [(0.10, "under 10% of revenue: low"), (RP_REVENUE_FLAG, "10-20%: notable - check pricing"),
                         (INF, "above 20%: concentration on related parties - red flag")],
    "rp_receivables_share": [(0.10, "under 10% of receivables: low"),
                             (RP_RECEIVABLES_FLAG, "10-25%: notable"),
                             (INF, "above 25%: related parties owe a large share of receivables - red flag")],
    "rp_advances_revenue": [(0.20, "under 20% of revenue: low"), (RP_ADVANCES_FLAG, "20-50%: notable"),
                            (INF, "above 50%: escalate - possible fund diversion")],
}
# ratio keys that share a band table
INTERPRETATION_ALIAS = {"dupont": "roe", "dupont_mod": "roe", "dscr_precapex": "dscr", "cce_reported": "cce", "dir_current": "dir", "dir_total": "dir",
                        "de_total": "de", "de_borrowings": "de", "net_debt_ebitda_reported": "net_debt_ebitda",
                        "interest_coverage_reported": "interest_coverage", "dscr_ebit": "dscr"}

# ---------------------------------------------------- distress texts (grouped box)
DISTRESS_TEXT = {
    "neg_equity": "shareholders' equity negative, so {ratios} {verb} not meaningful",
    "neg_ebitda": "EBITDA negative, so {ratios} {verb} not meaningful",
    "neg_margin": "gross margin zero or negative, so {ratios} {verb} not meaningful",
}

# ---------------------------------------------------- score cards (Ratios tab summary panel)
# key -> list of (upper bound, zone label, colour). First band whose upper bound is ABOVE the value applies.
SCORE_ZONES = {
    "beneish_m": [(-2.22, "No manipulation pattern", "green"), (-1.78, "Grey zone", "amber"),
                  (INF, "Manipulation pattern - warrants investigation", "red")],
    "altman_z2": [(1.10, "Distress", "red"), (2.60, "Grey", "amber"), (INF, "Safe", "green")],
    "altman_z1": [(1.23, "Distress", "red"), (2.90, "Grey", "amber"), (INF, "Safe", "green")],
    "piotroski": [(3.5, "Weak", "red"), (6.5, "Average", "amber"), (INF, "Strong", "green")],
}

# ---------------------------------------------------- validation for financial companies
# Banks are far larger than industrials (SBI > Rs 60 lakh crore of assets), so the "implausibly large"
# ceiling is higher, and for financial companies every validation error is a proceed-anyway warning.
MAX_PLAUSIBLE_CRORE_FINANCIAL = 20_000_000

# =========================================================== thresholds table (Ratios tab + Stage 3)
# ONE table used for the status dot on the Ratios tab and, in Stage 3, for the 0-100 sub-scores.
# key -> list of (upper bound, sub-score 0-100, status label). First band whose upper bound is ABOVE
# the value applies. Stage 3 may interpolate between band scores; the bands themselves live here only.
SCORE_THRESHOLDS = {
    # A. Cash & Liquidity
    "current_ratio": [(1.0, 15, "weak"), (1.33, 50, "thin"), (2.0, 80, "comfortable"), (INF, 100, "strong")],
    "cce": [(0.0, 0, "negative"), (0.40, 15, "major concern"), (0.60, 40, "weak"), (0.80, 65, "acceptable"),
            (1.00, 85, "strong"), (INF, 100, "outstanding")],                       # brief + slide wording
    "ocf_ratio": [(0.0, 0, "negative"), (0.20, 25, "weak"), (0.40, 55, "moderate"), (1.0, 85, "healthy"),
                  (INF, 100, "strong")],
    "dir": [(30, 10, "very thin"), (60, 30, "thin"), (90, 50, "adequate"), (150, 65, "comfortable"),
            (INF, 100, "strong")],
    "wc_days": [(0, 85, "supplier-funded"), (30, 100, "lean"), (60, 75, "typical"), (90, 55, "stretched"),
                (INF, 20, "very heavy")],
    "ccc": [(0, 100, "supplier-funded"), (45, 85, "efficient"), (90, 60, "moderate"), (150, 35, "long"),
            (INF, 10, "very long")],
    # B. Solvency & Debt Service
    "interest_coverage": [(1.0, 0, "uncovered"), (1.5, 20, "weak"), (3.0, 50, "adequate"), (6.0, 80, "comfortable"),
                          (INF, 100, "strong")],
    "de_total": [(0.5, 100, "low"), (1.0, 80, "moderate"), (2.0, 50, "elevated"), (3.0, 25, "high"),
                 (INF, 5, "very high")],
    "net_debt_ebitda": [(0.0, 100, "net cash"), (1.0, 90, "low"), (2.5, 65, "moderate"), (4.0, 35, "elevated"),
                        (INF, 10, "high")],
    "dscr": [(0.0, 0, "negative"), (1.0, 15, "uncovered"), (1.25, 45, "thin"), (2.0, 75, "adequate"),
             (INF, 100, "strong")],
    # C. Earnings Integrity
    "sloan": [(0.0, 100, "cash-backed"), (0.05, 100, "low accruals"), (0.10, 60, "moderate"), (0.20, 35, "high"),
              (INF, 0, "very high")],                                                 # brief's Sloan table
    "bs_accrual": [(0.05, 100, "low"), (0.10, 60, "moderate"), (0.20, 35, "high"), (INF, 0, "very high")],
    "rec_vs_sales": [(0.0, 100, "in line"), (0.10, 75, "slightly ahead"), (0.20, 45, "watch"), (INF, 10, "red flag")],
    "dsri": [(0.9, 100, "falling"), (1.1, 90, "stable"), (1.465, 50, "rising"), (INF, 10, "red flag")],
    "beneish_m": [(-2.22, 100, "no pattern"), (-1.78, 50, "grey zone"), (INF, 0, "pattern")],  # Stage 3: linear
    # D. Distress
    "altman_z2": [(1.10, 0, "distress"), (2.60, 50, "grey"), (INF, 100, "safe")],      # brief's Z'' table
    "altman_z1": [(1.23, 0, "distress"), (2.90, 50, "grey"), (INF, 100, "safe")],
    # Piotroski is scored on criteria passed / criteria assessable, rescaled to 0-9 before these bands.
    "piotroski": [(3.5, 20, "weak"), (6.5, 60, "average"), (INF, 100, "strong")],

    # ---------- CONTEXT ONLY (not part of CCIRS) - every band below is PROPOSED - review ----------
    # Profitability
    "op_ebit_margin": [(0.0, 0, "loss-making"), (0.05, 25, "thin"), (0.10, 50, "moderate"),      # PROPOSED - review
                       (0.20, 80, "healthy"), (INF, 100, "strong")],
    "net_margin": [(0.0, 0, "loss"), (0.03, 25, "thin"), (0.08, 55, "moderate"), (0.15, 80, "healthy"),  # PROPOSED - review
                   (INF, 100, "strong")],
    "roe": [(0.0, 0, "negative"), (0.08, 25, "weak"), (0.12, 50, "moderate"), (0.20, 80, "good"),       # PROPOSED - review
            (INF, 100, "strong")],
    "roce": [(0.0, 0, "negative"), (0.08, 25, "below cost of capital"), (0.12, 50, "moderate"),         # PROPOSED - review
             (0.20, 80, "good"), (INF, 100, "strong")],
    "roic": [(0.0, 0, "negative"), (0.08, 25, "below cost of capital"), (0.12, 55, "near cost of capital"),  # PROPOSED - review
             (0.20, 80, "value-creating"), (INF, 100, "strong")],
    # Efficiency
    "asset_turnover": [(0.5, 25, "low"), (1.0, 55, "moderate"), (1.5, 80, "good"), (INF, 100, "high")],  # PROPOSED - review
    "dio": [(30, 100, "lean"), (60, 80, "normal"), (120, 45, "high"), (INF, 15, "very high")],          # PROPOSED - review
    "dso": [(30, 100, "fast"), (60, 80, "normal"), (90, 50, "slow"), (120, 25, "very slow"),             # PROPOSED - review
            (INF, 10, "collection risk")],
    "dpo": [(30, 60, "quick payer"), (90, 90, "normal"), (150, 55, "stretched"), (INF, 20, "very stretched")],  # PROPOSED - review
}

# Neutral (grey-dot) descriptive labels for valuation and payout - not credit-risk signals.
# key -> list of (upper bound, label). Every band here is PROPOSED - review.
NEUTRAL_LABELS = {
    "pe": [(10, "discount valuation"), (20, "moderate valuation"), (35, "premium valuation"),
           (INF, "rich valuation")],                                                     # PROPOSED - review
    "peg": [(1.0, "cheap for its growth"), (2.0, "fair for its growth"), (INF, "expensive for its growth")],  # PROPOSED - review
    "div_yield": [(0.005, "minimal yield"), (0.02, "modest yield"), (0.04, "good yield"), (INF, "high yield")],  # PROPOSED - review
    "p_ocf": [(10, "low cash multiple"), (20, "moderate cash multiple"), (35, "high cash multiple"),
              (INF, "very high cash multiple")],                                         # PROPOSED - review
    "payout": [(0.0001, "no payout"), (0.30, "low payout"), (0.60, "moderate payout"), (1.0, "high payout"),
               (INF, "paying out more than profit")],                                    # PROPOSED - review
    "retention": [(0.0, "paying out more than profit"), (0.40, "low retention"), (0.70, "moderate retention"),
                  (INF, "high retention")],                                              # PROPOSED - review
    "internal_growth": [(0.0, "no self-funded growth"), (0.05, "slow"), (0.12, "moderate"), (INF, "fast")],  # PROPOSED - review
    "sustainable_growth": [(0.0, "no self-funded growth"), (0.05, "slow"), (0.12, "moderate"), (INF, "fast")],  # PROPOSED - review
}
PIOTROSKI_MIN_ASSESSABLE = 5      # PROPOSED - review: fewer assessable criteria -> "not available"
STATUS_COLOUR_CUTS = (70, 40)      # sub-score >= 70 green, 40-69 amber, < 40 red

# Ratios tab: two always-visible blocks. Block 1 feeds CCIRS (coloured dots); block 2 is context only.
MAIN_BLOCKS = [
    {"title": "CCIRS inputs", "caption": "Ratios that feed the CCIRS score (Stage 3).",
     "groups": [("Liquidity", ["cce", "ocf_ratio", "dir", "wc_days", "ccc", "current_ratio"]),
                ("Solvency", ["interest_coverage", "de_total", "net_debt_ebitda", "dscr"]),
                ("Earnings integrity", ["sloan", "bs_accrual", "rec_vs_sales"])]},
    {"title": "Performance and valuation context", "caption": "shown for context, not part of the CCIRS score",
     "groups": [("Profitability", ["op_ebit_margin", "net_margin", "roe", "roce", "roic"]),
                ("Efficiency", ["asset_turnover", "dio", "dso", "dpo"]),
                ("Market perception", ["pe", "peg", "div_yield", "p_ocf"]),
                ("Growth and payout", ["payout", "retention", "internal_growth", "sustainable_growth"])]},
]
MAIN_TABLE = [g for b in MAIN_BLOCKS for g in b["groups"]]
NEUTRAL_GROUPS = {"Market perception", "Growth and payout"}      # grey dots, descriptive labels only
CONTEXT_IN_SCORED_BLOCK = {"current_ratio"}                      # shown in block 1 but not scored
MAIN_TABLE_LABELS = {
    "current_ratio": "Current ratio (context, not scored)", "cce": "CCE (operating EBITDA)", "ocf_ratio": "Operating cash flow ratio",
    "dir": "Defensive interval ratio", "ccc": "Cash conversion cycle",
    "interest_coverage": "Interest coverage (operating EBIT)", "de_total": "Debt/Equity (incl. leases)",
    "net_debt_ebitda": "Net debt (incl. leases) / operating EBITDA", "dscr": "DSCR proxy (cash)",
    "wc_days": "Working-capital days", "sloan": "Sloan accrual ratio", "bs_accrual": "Balance-sheet accrual ratio",
    "rec_vs_sales": "Receivables growth minus sales growth",
    "op_ebit_margin": "Operating EBIT margin", "net_margin": "Net profit margin", "roe": "ROE", "roce": "ROCE",
    "roic": "ROIC", "asset_turnover": "Asset turnover", "dio": "DIO (inventory days)", "dso": "DSO (receivable days)",
    "dpo": "DPO (payable days)", "pe": "P/E", "peg": "PEG", "div_yield": "Dividend yield", "p_ocf": "P/OCF",
    "payout": "Dividend payout", "retention": "Retention ratio", "internal_growth": "Internal growth rate",
    "sustainable_growth": "Sustainable growth rate",
}

# =========================================================== STAGE 3: CCIRS scoring engine
# Pillar weights and sub-weights from the project brief (adjustable in the UI; normalised to 100%).
PILLARS = {
    "A": {"name": "Cash & Liquidity", "weight": 30,
          "ratios": {"cce": 35, "ocf_ratio": 20, "dir": 15, "wc_days": 15, "ccc_trend": 15}},
    "B": {"name": "Solvency & Debt Service", "weight": 25,
          "ratios": {"interest_coverage": 30, "de_total": 20, "net_debt_ebitda": 20, "dscr": 30}},
    "C": {"name": "Earnings Integrity", "weight": 25,
          "ratios": {"sloan": 30, "bs_accrual": 20, "beneish_m": 30, "dsri_rec": 20}},
    "D": {"name": "Distress & Governance Flags", "weight": 20,
          "ratios": {"altman": 40, "piotroski": 20, "rp_advances": 20, "governance": 20}},
}
COMPONENT_LABELS = {
    "cce": "CCE (operating EBITDA)", "ocf_ratio": "Operating cash flow ratio", "dir": "Defensive interval ratio",
    "wc_days": "Working-capital days", "ccc_trend": "CCC trend (change vs prior year)",
    "interest_coverage": "Interest coverage (operating EBIT)", "de_total": "Debt/Equity (incl. leases)",
    "net_debt_ebitda": "Net debt (incl. leases) / operating EBITDA", "dscr": "DSCR proxy (cash)",
    "sloan": "Sloan accrual ratio", "bs_accrual": "Balance-sheet accrual ratio", "beneish_m": "Beneish M-Score",
    "dsri_rec": "DSRI / receivables vs sales", "altman": "Altman Z (variant used)", "piotroski": "Piotroski F-Score",
    "rp_advances": "Related-party advances / revenue", "governance": "Auditor & promoter flags",
}
# Ratio whose applicability tag governs each score component (for financial companies).
COMPONENT_APPLICABILITY_KEY = {"ccc_trend": "ccc", "dsri_rec": "dsri", "rp_advances": "rp_advances_revenue"}

# Extra score bands for components not in the Ratios table - PROPOSED - review
SCORE_THRESHOLDS["ccc_trend"] = [(-10, 100, "improving"), (0, 85, "stable"), (10, 65, "slightly worse"),  # PROPOSED - review
                                 (30, 35, "deteriorating"), (INF, 10, "sharply worse")]       # change in CCC days
SCORE_THRESHOLDS["rp_advances_revenue"] = [(0.10, 100, "low"), (0.25, 70, "notable"),                 # PROPOSED - review
                                           (0.50, 35, "high"), (INF, 0, "escalate")]
BENEISH_LINEAR = (-2.22, -1.78)   # brief: 100 below -2.22, 0 above -1.78, linear in between
# Altman, CCE and Sloan use the brief's tables in SCORE_THRESHOLDS; Piotroski = passed / assessable x 100.

# "Not meaningful" that is GOOD news (no debt to service) scores 100 instead of being dropped.
NM_GOOD_SCORE = {"interest_coverage": 100, "dscr": 100}
# "Not meaningful" caused by distress (negative equity / EBITDA) scores 0 - never re-weighted away.
DISTRESS_NM_SCORE = 0

# Governance component (auditor & promoter flags), entered by the analyst - PROPOSED - review
AUDITOR_EVENTS = {"None": 100, "Routine rotation (term ended)": 100,
                  "Resigned - no reason stated": 60,
                  "Resigned - stated disagreement or concerns": 0}
AUDIT_OPINIONS = {"Unmodified (clean)": 100, "Unmodified with emphasis of matter": 75,
                  "Qualified": 20, "Adverse": 0, "Disclaimer of opinion": 0, "Audit report missing / not filed": 0}
PLEDGE_BANDS = [(10, 100), (25, 70), (50, 40), (INF, 10)]      # promoter pledge % of promoter holding
# Auditor override (brief): cap at 50 ONLY for these - not for routine rotation.
AUDITOR_CAP_EVENTS = {"Resigned - stated disagreement or concerns"}
AUDITOR_CAP_OPINIONS = {"Qualified", "Adverse", "Disclaimer of opinion", "Audit report missing / not filed"}

# Overrides (brief)
CFO_NEGATIVE_CAP = 40             # applied only if the analyst CONFIRMS the flag
AUDITOR_CAP = 50
RP_ESCALATE_THRESHOLD = RP_ADVANCES_FLAG   # 50% of revenue -> verdict forced to Escalate

# Bands and verdicts (brief): score >= lower bound
BANDS = [(75, "Green", "Approve", "#1E8E3E"), (55, "Amber", "Approve with conditions", "#E37400"),
         (35, "Orange", "Escalate for forensic review", "#D35400"), (0, "Red", "Decline", "#C5221F")]
MIN_COVERAGE_WARN = 0.50          # warn if less than half of the base weight had data
SENSITIVITY_STEP = 0.10           # each weight x (1 +/- 10%), then renormalised

# =========================================================== backtest fixes (Stage 3 revision)
NEG_EQUITY_CAP = 34               # negative net worth in the scoring year -> cap (Red, Decline) if analyst confirms
INVENTORY_MATERIALITY_PCT = 0.01  # inventory flag is red only if inventory > 1% of revenue OR of total assets
CAPTIVE_CAVEAT = ("Consolidated cash flow and leverage may include a captive finance arm; the score may understate "
                  "this company's industrial credit quality.")
LOSS_MAKER_EI_NOTE = "low accruals reflect cash losses, not earnings quality"
RP_RECEIVABLES_NOTE = ("mostly a concentration signal; check whether the counterparty is the parent or a group "
                       "company")
LOW_COVERAGE_YEAR = 0.70          # CCIRS by year: below this coverage the year is not comparable
LOW_COVERAGE_LABEL = "Indicative - limited data (no prior-year comparators)"
LOW_COVERAGE_TOOLTIP = ("The earliest year lacks prior-year data, so averages, the M-Score, DSRI, Piotroski and "
                        "accrual measures cannot be computed; its score rests on fewer ratios and is not comparable "
                        "with later years.")
CAPITAL_INTENSIVE_PPE_SHARE = 0.40   # PP&E / total assets above this = capital-intensive (Altman Z'' note)
CAPITAL_INTENSIVE_INDUSTRY_KEYWORDS = ("telecom", "utilities", "power", "infrastructure", "oil & gas", "airlines",
                                       "marine shipping", "railroads")
ALTMAN_Z2_ASSET_HEAVY_NOTE = ("Z'' penalises asset-heavy, low-retained-earnings businesses (e.g. telecom); read with "
                              "the cash and solvency pillars.")

# =========================================================== Stage 4: dashboard decision box
# Covenant suggested when a component's sub-score is below COVENANT_TRIGGER_SCORE (or the flag fires).
COVENANT_TRIGGER_SCORE = 50
COVENANTS = {
    "cce": "Minimum CCE (CFO / operating EBITDA) of 60%, tested annually on audited accounts.",
    "ocf_ratio": "Quarterly cash-flow statements; minimum OCF ratio of 0.25x.",
    "dir": "Maintain liquid funds covering at least 60 days of cash operating costs.",
    "wc_days": "Drawing power linked to stock and receivable statements; review working-capital days half-yearly.",
    "ccc_trend": "Monthly stock and book-debt statements; cap the cash conversion cycle at the sanctioned level.",
    "dsri_rec": "Monthly customer-wise receivables ageing; exclude receivables over 90 days from drawing power.",
    "interest_coverage": "Minimum interest coverage (operating EBIT / interest) of 2.0x.",
    "de_total": "Maximum Debt/Equity (incl. leases) of 2.0x; no further borrowing without lender consent.",
    "net_debt_ebitda": "Maximum net debt / operating EBITDA of 3.0x, tested half-yearly.",
    "dscr": "Minimum DSCR of 1.25x; cash sweep of surplus cash above an agreed threshold.",
    "sloan": "Annual reconciliation of profit to operating cash flow, certified by the statutory auditor.",
    "bs_accrual": "Annual review of capitalised costs and growth in net operating assets.",
    "beneish_m": "Forensic review of revenue recognition and receivables before renewal.",
    "altman": "Quarterly monitoring with an early-warning trigger on covenant headroom.",
    "rp_advances": "Restrict related-party loans and advances; prior lender consent above an agreed limit.",
    "governance": "Immediate notification of auditor change or qualification; cap on promoter pledge.",
    "inventory_build": "Inventory limits and ageing-based stock statements; exclude slow-moving stock from drawing power.",
}
COVENANT_ALWAYS = {"Amber": "Quarterly monitoring of financial covenants and stock / book-debt statements.",
                   "Orange": "Quarterly monitoring; no enhancement until the forensic review is closed.",
                   "Red": "No new exposure; review the existing facility for exit or additional security."}
INVESTIGATE_NEXT = [
    "Customer-wise receivables ageing (top 10 customers, over 90 / 180 days)",
    "GST returns (GSTR-1 / 3B) reconciled with reported revenue",
    "Bank statement trail: inflows vs reported sales, round-tripping between group accounts",
    "Related-party notes: loans, advances, guarantees and sales to group companies",
    "Composition of operating cash flow (working-capital release, one-offs, captive finance)",
    "Auditor's report, key audit matters, CARO remarks and any emphasis of matter",
]
DASHBOARD_MAX_FLAGS = 8
MNEAR_BAND = 0.25                 # M-Score within 0.25 below -2.22 -> "near the threshold" (amber)
MAX_PEERS = 4

# =========================================================== Stage 4 fixes
CUSTOM_WEIGHTS_LABEL = "Custom weights - not the default CCIRS"
LIVE_SEARCH_DEFAULT = True        # streamlit-searchbox suggestions; env CCIRS_LIVE_SEARCH=0 switches them off
LIVE_SEARCH_MIN_CHARS = 3
LIVE_SEARCH_DEBOUNCE_MS = 300
SEARCH_MAX_RESULTS = 25
# Common abbreviations Yahoo does not resolve well (lower-case key -> query sent)
SEARCH_ALIASES = {"m&m": "mahindra and mahindra", "m and m": "mahindra and mahindra", "l&t": "larsen toubro",
                  "l and t": "larsen toubro", "hul": "hindustan unilever", "sbi": "state bank of india",
                  "ril": "reliance industries", "ongc": "oil and natural gas", "bhel": "bharat heavy electricals"}
SEARCH_STOPWORDS = {"ltd", "limited", "and", "the", "india", "co", "company", "corp", "corporation", "inc"}

# =========================================================== Stage 5: AI memo
AI_MODEL = "claude-sonnet-5-5"           # used only in API mode (ANTHROPIC_API_KEY in env or st.secrets)
AI_MAX_TOKENS = 2500
AI_MANUAL_MODEL_DEFAULT = "Claude (claude.ai)"
AI_LOG_PATH = "logs/ai_calls.csv"        # relative to the app folder; env CCIRS_AI_LOG overrides
MEMO_SECTIONS = ["Recommendation", "Executive summary", "Key strengths", "Key risks", "Anomaly explanations",
                 "Proposed conditions and covenants", "Investigate next", "Data limitations"]
BANNED_PHRASES = ["fraud", "fraudulent", "manipulation proven", "proven manipulation", "manipulated the accounts",
                  "is manipulating", "cooked the books", "will default", "certain to default", "is bound to default",
                  "guaranteed to"]
# Text masked before number-checking (not figures): standards, year labels, model names
RELIABILITY_IGNORE = [r"Ind\s?AS\s?\d+", r"\bFY\s?\d{2,4}\b", r"\b(?:19|20)\d{2}-\d{2}\b", r"\bZ''", r"\bZ'",
                      r"\bF-Score\b", r"\b\d+\s?(?:of|/)\s?9 criteria\b", r"\bpillar [A-D]\b", r"\bGSTR-\d[A-Z]?\b",
                      r"\b(?:top|over|within)\s\d+\s(?:customers|days)\b", r"\bCARO\s\d+\b"]

# =========================================================== Stage 6 revisions
# Exceptional / unusual items are removed from operating EBITDA and operating EBIT (with other income and
# the share of associates/JVs). Set False only to reproduce pre-fix scores ("before" comparison).
EXCLUDE_EXCEPTIONAL = True
ONE_OFF_EBIT_MARGIN = 0.60        # operating EBIT margin above this -> data-quality flag
ONE_OFF_EBITDA_SWING = 0.50       # EBITDA moves more than 50% YoY ...
ONE_OFF_REVENUE_STABLE = 0.15     # ... while revenue moves less than 15% -> data-quality flag
ONE_OFF_TEXT = "Possible one-off or mislabelled item - verify against the annual report."

# AI memo tab (research prototype). False hides the tab; the code, tests and logs stay.
ENABLE_AI_MEMO = True
AI_MEMO_BANNER = ("Research prototype - in a bank this would run only through an approved model under a data "
                  "agreement; do not paste client data into public AI tools.")
SIGN_WORDS = ["negative", "outflow", "outflows", "loss", "losses", "deficit", "decline", "declined", "minus",
              "below zero", "fell to minus", "net cash outflow"]
SIGN_WORD_WINDOW = 5              # words either side of the number
LOG_DEDUPE_SECONDS = 10

# =========================================================== Stage 6: backtest company set (editable)
# Healthy / stressed pairs by sector. Edit here; the Backtest tab can also add or remove companies per session.
BACKTEST_SET = [
    {"sector": "Telecom", "role": "healthy", "name": "Bharti Airtel Limited", "ticker": "BHARTIARTL.NS"},
    {"sector": "Telecom", "role": "stressed", "name": "Vodafone Idea Limited", "ticker": "IDEA.NS"},
    {"sector": "Auto", "role": "healthy", "name": "Maruti Suzuki India Limited", "ticker": "MARUTI.NS"},
    {"sector": "Auto", "role": "stressed", "name": "Ashok Leyland Limited", "ticker": "ASHOKLEY.NS"},
    {"sector": "Construction/EPC", "role": "healthy", "name": "KEC International Limited", "ticker": "KEC.NS"},
    {"sector": "Construction/EPC", "role": "stressed", "name": "Hindustan Construction Company Limited",
     "ticker": "HCC.NS"},
    {"sector": "Power", "role": "healthy", "name": "NTPC Limited", "ticker": "NTPC.NS"},
    {"sector": "Power", "role": "stressed", "name": "Reliance Power Limited", "ticker": "RPOWER.NS"},
]
HANDCHECK_COMPANIES = ("MARUTI.NS", "IDEA.NS")     # healthy, stressed - Excel hand-check workbook
HANDCHECK_TOLERANCE = {"ratio": 0.0005, "score": 0.005}   # |Excel - app| allowed: 0.05pp on ratios, 0.005 on Z
YAHOO_YEARS_NOTE = ("Yahoo Finance covers only about four annual periods. To score a stressed firm on a year BEFORE "
                    "its stress (e.g. FY20 or earlier), upload those years with the template.")

# =========================================================== Stage 7 revisions
CASH_ONE_OFF_CCE = 1.50           # CCE above 150% -> data-quality flag (no score change)
CASH_ONE_OFF_MULTIPLE = 2.0       # CFO more than 2x operating EBITDA (also flags positive CFO with EBITDA <= 0)
CASH_ONE_OFF_TEXT = ("Cash flow may include one-offs (arbitration receipts, working-capital release, asset sales) - "
                     "verify against the cash flow statement.")
UTILITY_CAVEAT = ("Regulated utility: leverage thresholds are general-purpose; regulated returns and state "
                  "ownership are not captured.")
UTILITY_KEYWORDS = ("utilities", "power")
ALT_EI_NEUTRAL_SCORE = 50         # robustness check only: Earnings Integrity set to 50 when the pillar is "limited"
ALT_SPEC_LABEL = "Alternative specification (robustness check, not the CCIRS): Earnings Integrity = 50 for loss-makers"

# =========================================================== public deployment (display only - no scoring effect)
ENABLE_DEMO = False               # True -> sidebar offers "Demo mode (hypothetical sample)" (Apex Auto Components)
ENABLE_BACKTEST = False           # True -> Backtest tab (with the hand-check workbook download) is shown
APP_SUBTITLE = ("Credit screening for working-capital lending decisions | For academic purposes only - "
                "not investment or lending advice.")
CCIRS_TAGLINE = ("CCIRS - Credit Cash-Integrity & Resilience Score: my own composite score (0-100) for this "
                 "project.")
SOURCE_LEGEND = ("All figures from Yahoo Finance (consolidated) unless marked: (U) uploaded, (M) manual entry, "
                 "(D) derived from accounting identities.")
ANALYST_INPUTS_TITLE = "Analyst inputs (from the annual report - optional)"
ANALYST_INPUTS_WHY = ("Related-party loans and advances can divert lender funds; they feed the Distress & "
                      "Governance pillar and the Escalate override.")
SOURCE_MAPPING_TITLE = "Source mapping (which Yahoo line feeds each item)"
WHAT_IS_CCIRS = [
    "**Four pillars, default weights:** Cash & Liquidity **30** · Solvency & Debt Service **25** · "
    "Earnings Integrity **25** · Distress & Governance **20**. Each pillar averages 0-100 sub-scores of its ratios.",
    "**Why liquidity first:** a working-capital line is repaid from the operating cycle, so whether profit turns "
    "into cash (CFO / EBITDA, OCF ratio, liquid-funds cover) matters more than long-run solvency.",
    "**Bands:** 75+ **Green - Approve** · 55-75 **Amber - Approve with conditions** · 35-55 **Orange - Escalate "
    "for forensic review** · below 35 **Red - Decline**.",
    "**Overrides** (applied after the weighted score): CFO negative for 2 years caps at 40 and negative net worth "
    "caps at 34 (both need your confirmation); a qualified/adverse audit opinion or an auditor resigning over "
    "disagreement caps at 50; related-party advances above 50% of revenue force Escalate.",
    "Missing ratios are re-weighted within their pillar, never filled with zero. Screening aid only - "
    "not proof of manipulation or default.",
]

# Altman component label shown in the breakdown, drivers, recommendation and exports (display only)
ALTMAN_LABEL = {"altman_z1": "Altman Z' (manufacturing)", "altman_z2": "Altman Z'' (non-manufacturing)"}
WEAK_SUB_SCORE = 60              # display: a component below this sub-score is "weak" ("Holds it back")
ADVERSE_VERDICTS = ("Escalate for forensic review", "Decline")
RED_FLAGS_NOTE = "Red flags do not change the band; they are addressed through the covenants and checks below."
