# CCIRS - Credit Cash-Integrity & Resilience Score

A Streamlit dashboard that screens a listed Indian company's ability to repay a **working-capital facility**. It looks behind reported growth, EBITDA and the current ratio at cash conversion, debt service, earnings integrity and distress.

Built for the MBA course project "Working with AI" (Financial Statement Analysis & Forensic Auditing).
**For academic purposes only. This is not investment or lending advice.**

---

## 1. Purpose and stakeholder

| | |
|---|---|
| **User** | Credit Risk Analyst, corporate banking team of a commercial bank |
| **Problem** | Management shows sales growth, EBITDA growth and a comfortable current ratio. Can the borrower actually repay? |
| **Decision** | Renew or enhance a working-capital facility: **Approve / Approve with conditions / Escalate for forensic review / Decline** |
| **Scope** | Non-financial listed Indian companies. Banks, NBFCs and insurers load with an "Indicative only" banner, and ratios that are not valid for them are excluded. |

The **CCIRS** (0-100) combines four pillars:
- A. Cash & Liquidity: 30%.
- B. Solvency & Debt Service: 25%.
- C. Earnings Integrity: 25%.
- D. Distress & Governance: 20%.

Overrides then apply, and the result maps to a band and a verdict. It is a **screening tool**, not proof of manipulation or default.

## 2. Supported Python versions

| Python | Status |
|---|---|
| 3.10 - 3.13 | **Supported and tested** (developed on 3.13). All tests run, including the numeric recalculation of the Excel workbooks with `pycel`. |
| 3.14+ | The app runs. `pycel` 1.0b30 uses `ast.Str`, which was removed in Python 3.14, so the **numeric workbook recalculation tests are skipped** with a clear reason. The static workbook checks still run, and the workbooks themselves are unaffected (open them in Excel to recalculate). |

## 3. Install and run

**Windows (PowerShell or Command Prompt)**
```bat
cd ccirs
py -3.13 -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
py -m pytest -q
py -m streamlit run app.py
```

**macOS / Linux**
```bash
cd ccirs
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
streamlit run app.py
```
The app opens at http://localhost:8501. The sidebar offers **Search company** (Yahoo Finance, NSE/BSE) and **Upload only** (for unlisted borrowers: fill the template). The offline demo company (Apex Auto Components) is hidden in the public deployment; set `ENABLE_DEMO = True` in `config.py` to show it. `.streamlit/config.toml` sets `toolbarMode = "minimal"`, which hides Streamlit's Deploy button and developer menu.

## 4. What each tab does

Tabs, in order: **Dashboard | CCIRS score | Ratios | Financial data | Data checks**.

The CCIRS is shown to one decimal everywhere (score cards, What-if, recommendation, charts, peer table, exports). The band and verdict always come from the unrounded score, and a shown score never crosses a band boundary: 74.96 shows as **74.9 Amber**, not 75.0. Pillar gauges show whole numbers. A component is "weak" below a sub-score of 60 (`WEAK_SUB_SCORE`, display only).

| Tab | What it shows |
|---|---|
| 🏠 **Dashboard** | Explains, then decides. (a) Score card: company, CCIRS, band and verdict, with a one-line definition of CCIRS. (b) **What is CCIRS?** (open on first load): pillars and default weights, why liquidity comes first, the bands and the overrides. (c) Four pillar gauges, then **What drove the score**: the 3 strongest and 3 weakest components with value, band and points gained or lost. (d) Red flags, then trend charts (CFO vs EBITDA vs PAT, receivable days, CCIRS by year). (e) **Screening recommendation**: the band rule applied, the drivers with numbers, overrides applied or pending, caveats, covenants tied to the weakest components, and "Investigate next". (f) What-if weight sliders and peer comparison, below the decision. |
| 🎯 **CCIRS score** | Score card and pillar scores; **How the score was built** (every component's sub-score, weights and contribution); **Overrides** (Confirm / Dismiss for negative CFO and negative net worth, with the reason each matters); **Analyst inputs (from the annual report - optional)**: related-party figures, auditor change, audit opinion, promoter pledge and the "I have reviewed these" tick; downloads (breakdown CSV, **calculation workbook** with live formulas). **Technical detail** (collapsed): weights and sub-weights, ±10% sensitivity scenarios, CCIRS by year, Altman details. |
| 🧮 **Ratios** | Score cards (Beneish, Altman, Piotroski, CCIRS). **CCIRS inputs** block with status dots, and a **Performance and valuation context** block (not part of the score). "More detail" holds the formula, inputs, interpretation and applicability for every ratio, plus DuPont, ROIIC and the Beneish / Altman / Piotroski breakdowns. |
| 📊 **Financial data** | Every line item in Rs crore. One legend: all figures are from Yahoo Finance (consolidated) unless marked **(U)** uploaded, **(M)** manual entry or **(D)** derived from accounting identities; the header line gives each year's source. |
| 🔎 **Data checks** | Data depth, usable years, error and warning counts at the top; then every validation issue (balance-sheet check, gaps, unit errors, 10x jumps, revenue definition, one-off EBITDA and one-off cash flow); data coverage (what the usable years unlock); and, collapsed, **Source mapping (which Yahoo line feeds each item)** with the merge notes. |
| 🤖 **AI memo** | *Hidden* (`ENABLE_AI_MEMO = False`). A research prototype that drafts a credit memo from the computed figures and checks every number it cites. |
| 📈 **Backtest** | *Hidden* (`ENABLE_BACKTEST = False`). Scores healthy / stressed pairs with the default weights: direction check, rank stability under ±10% weights, alternative specification (robustness check), hindsight-bias controls; downloads the backtest .xlsx / .csv and the **hand-check workbook**. |

## 5. Data

- **Yahoo Finance** (via `yfinance`). The sidebar searches by company name, with live suggestions and an Enter-to-search fallback; results are NSE / BSE listings only. The app loads about **four annual periods** of consolidated statements, plus the current price and market cap.
- **Upload template** (`ccirs_upload_template.xlsx`, also downloadable in the sidebar). Adds earlier years (for example, pre-stress years for the backtest) or corrects Yahoo figures, in Rs crore and consolidated only. On overlapping years the upload wins by default.
- **Conventions:**
  - Everything is in Rs crore, and Mar-2025 = FY25.
  - Missing data stays blank ("data unavailable") and is never set to zero.
  - A figure is derived only from an accounting identity (tagged "derived").
  - Total debt includes lease liabilities.
- **Operating EBITDA / EBIT** = reported − other income − share of associates / JVs − **exceptional items**. Yahoo has no single other-income line, so the app adds up the non-operating parts Yahoo reports.
- **Limits of the data:**
  - Yahoo labels for Indian companies are sometimes missing or mislabelled. The flags and the hand-check workbook exist to catch this.
  - Four years is a short window.
  - Market data is current only, so valuation ratios cover the latest year only.

## 6. Settings in `config.py`

Every judgement-based number lives in `config.py`.

| Setting | What it controls |
|---|---|
| `PILLARS` | Pillar weights (30/25/25/20) and sub-weights from the brief |
| `SCORE_THRESHOLDS` | Bands that turn each ratio into a 0-100 sub-score (also the Ratios-tab status dots). Context bands are marked `PROPOSED - review`. |
| `BANDS`, `CFO_NEGATIVE_CAP`, `NEG_EQUITY_CAP`, `AUDITOR_CAP`, `RP_ESCALATE_THRESHOLD` | Verdict bands and overrides |
| `FIN_APPLICABILITY`, `NONFIN_APPLICABILITY` | Valid / Limited / Not valid tags (banks, NBFCs, insurers) |
| `LIQUIDITY_INCLUDE_NONCURRENT` | Whether DIR, net debt and ROIC count non-current liquid investments (also a sidebar toggle) |
| `EXCLUDE_EXCEPTIONAL` | Remove exceptional items from operating EBITDA / EBIT (`False` reproduces pre-fix figures) |
| `ONE_OFF_*`, `CASH_ONE_OFF_*` | Data-quality flags for one-off EBITDA and one-off cash flow |
| `UTILITY_CAVEAT`, `CAPTIVE_CAVEAT`, `ALTMAN_Z2_ASSET_HEAVY_NOTE` | Caveats shown without changing the score |
| `ENABLE_DEMO` | `False` (public deployment) hides "Demo mode (hypothetical sample)" from the sidebar. The Apex demo data and its tests are kept. |
| `ENABLE_BACKTEST` | `False` (public deployment) hides the Backtest tab and its hand-check download. The code and tests are kept. |
| `APP_SUBTITLE`, `CCIRS_TAGLINE`, `WHAT_IS_CCIRS`, `SOURCE_LEGEND`, `ANALYST_INPUTS_*` | Display text only. |
| `ENABLE_AI_MEMO` | `False` (default) hides the AI memo tab; `True` shows it with a research-prototype banner. The code, tests and `logs/ai_calls.csv` are kept either way. |
| `AI_MODEL` | Model used only in API mode (when `ANTHROPIC_API_KEY` is set). Never commit a key. |
| `BACKTEST_SET` | Backtest pairs: Airtel / Vodafone Idea, Maruti / Ashok Leyland, KEC / HCC, NTPC / Reliance Power. Edit here, or add or remove companies in the tab for the session. |
| `HANDCHECK_COMPANIES`, `HANDCHECK_TOLERANCE` | Companies and tolerances for the hand-check workbook |
| `ALT_EI_NEUTRAL_SCORE` | Robustness check: Earnings Integrity = 50 for loss-makers (never the default) |

## 7. Tests

```bash
python -m pytest -q          # Windows: py -m pytest -q
```
About 330 offline tests; Yahoo and the Anthropic API are always mocked. They cover:
- **Data and ratios:** the data layer and every ratio formula. The Apex Auto Components demo reproduces the brief's expected values.
- **Scoring:** the CCIRS engine and overrides.
- **App:** the dashboard and each tab, as Streamlit smoke tests. `tests/conftest.py` switches the demo and the Backtest tab on for these tests; `tests/test_deploy.py` checks the deployed layout with both off.
- **Score identity:** `tests/golden_scores.json` holds the CCIRS of four synthetic stand-ins (shaped like Maruti, Vodafone Idea, Airtel and HUL) and the Apex demo; any change to a score fails the suite. For the real companies, run `python tools/score_snapshot.py > before.json` before a change and `> after.json` after, then `python tools/score_snapshot.py --compare before.json after.json`.
- **Backtest:** the direction check and Spearman rank stability.
- **AI memo:** the reliability checker.
- **Workbooks:** both Excel workbooks are checked for formulas, errors and app-match.

## 8. Folder structure

```
ccirs/
├── app.py                  Streamlit UI (all tabs)
├── config.py               every weight, threshold, band, flag and setting
├── data.py                 Yahoo fetch and name search, label mapping, upload template, merge, derivations, validation
├── ratios.py               every ratio with formula, inputs, status, applicability, interpretation; automatic flags
├── scoring.py              CCIRS engine: sub-scores, re-weighting, overrides, bands, sensitivity
├── dashboard.py            red-flag panel, peers, decision box, plotly figures
├── backtest.py             batch scoring, direction check, Spearman, alternative specification, exports
├── handcheck.py            Excel hand-check workbook (Maruti vs Vodafone Idea) + static workbook checker
├── calcbook.py             Excel calculation workbook (loaded company) with live formulas
├── ai_memo.py              AI memo payload, prompt, reliability check, prompt log (tab hidden by default)
├── ccirs_upload_template.xlsx
├── requirements.txt, pytest.ini
├── tests/                  offline test suite
├── docs/                   methodology note (.md, .docx)
├── evidence/               prompt log and saved memos (see evidence/README.md)
├── .streamlit/config.toml  hides the Deploy button and developer menu
├── tools/package.py        builds a clean submission zip
└── tools/score_snapshot.py prints / compares CCIRS scores before and after a change
```

## 9. Known limitations

- **Yahoo data quality:** some line items are missing or mislabelled for Indian companies. Verify key figures against the annual report; the hand-check workbook makes this quick.
- **Four-year window and hindsight bias:** stressed firms should be scored on pre-stress years, which you upload with the template.
- **Weights are judgement-based.** The sensitivity test and the backtest rank stability show how much the result depends on them.
- **Models calibrated on US data:** Beneish, Altman and Sloan were calibrated on US firms and are sensitive to growth and industry norms. Altman Z'' penalises businesses with negative working capital and low retained earnings (e.g. telecom).
- **Loss-makers:** low accruals reflect cash losses, not earnings quality, so Earnings Integrity is marked "limited". An alternative specification is in the Backtest tab (switch it on with `ENABLE_BACKTEST = True`).
- **Regulated utilities and captive finance arms:** these get caveats rather than score changes.
- **Peers are scored with default analyst inputs** (governance not reviewed, overrides pending).
- **Not built from the original brief:** the Benford first-digit test, the Behavioural Velocity score and the Peer Anomaly Index.
- **LLCR (loan life coverage ratio) is not computed:** it needs projected cash flows over the loan life.

## 10. Disclaimer

CCIRS is a screening aid for a credit analyst. It is **not proof of manipulation or default**, not a credit rating and not a lending decision. It is **not valid for banks, NBFCs and insurers**. Weights and several thresholds are judgement-based (those marked `PROPOSED - review` in `config.py`). For academic purposes only - not investment or lending advice.
