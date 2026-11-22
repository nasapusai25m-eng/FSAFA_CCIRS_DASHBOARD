"""Simplified Ratios tab: pillar table, status dots, More detail contents, RP flags, HDFC validation fix."""
import pandas as pd
import pytest

import config
import data as D
import ratios as R
from test_ratios import FULL, make_fd


@pytest.fixture(scope="module")
def apex():
    return R.compute_all(D.apply_derivations(D.demo_data()))


# ------------------------------------------------------------------ 1. main table rows
def test_main_rows_two_blocks_exact_ratios(apex):
    rows = apex.main_rows("FY26")
    by = {}
    for r in rows:
        by.setdefault((r["block"], r["pillar"]), []).append(r["key"])
    assert by == {
        ("CCIRS inputs", "Liquidity"): ["cce", "ocf_ratio", "dir", "wc_days", "ccc", "current_ratio"],
        ("CCIRS inputs", "Solvency"): ["interest_coverage", "de_total", "net_debt_ebitda", "dscr"],
        ("CCIRS inputs", "Earnings integrity"): ["sloan", "bs_accrual", "rec_vs_sales"],
        ("Performance and valuation context", "Profitability"): ["op_ebit_margin", "net_margin", "roe", "roce", "roic"],
        ("Performance and valuation context", "Efficiency"): ["asset_turnover", "dio", "dso", "dpo"],
        ("Performance and valuation context", "Market perception"): ["pe", "peg", "div_yield", "p_ocf"],
        ("Performance and valuation context", "Growth and payout"): ["payout", "retention", "internal_growth",
                                                                     "sustainable_growth"],
    }
    scored = {r["key"] for r in rows if r["scored"]}
    assert "wc_days" in scored and "bs_accrual" in scored
    assert "current_ratio" not in scored and "roe" not in scored          # context only
    assert set(rows[0]["values"]) == {"FY25", "FY26"}


def test_status_uses_thresholds_table(apex):
    s = {r["key"]: (r["status"], r["colour"]) for r in apex.main_rows("FY26")}
    assert s["cce"] == ("major concern", "red")              # 20%
    assert s["dir"] == ("comfortable", "amber")              # 116 days  (brief check: amber)
    assert s["wc_days"] == ("stretched", "amber")            # 73 days   (brief check: amber)
    assert s["current_ratio"] == ("comfortable", "green")    # 1.80
    assert s["sloan"] == ("low accruals", "green")           # 4.4%
    assert s["interest_coverage"] == ("no data", "grey")
    assert {r["key"]: r["status"] for r in apex.main_rows("FY25")}["cce"] == "acceptable"   # 75%


def test_threshold_edits_from_part2():
    import config as C
    assert C.SCORE_THRESHOLDS["sloan"][2] == (0.10, 60, "moderate")
    assert C.SCORE_THRESHOLDS["bs_accrual"][1] == (0.10, 60, "moderate")
    assert [b[2] for b in C.SCORE_THRESHOLDS["cce"]] == ["negative", "major concern", "weak", "acceptable",
                                                         "strong", "outstanding"]


def test_status_for_not_meaningful():
    bk = R.compute_all(make_fd({"FY25": {"total_debt": 100, "equity": -50}}))
    assert R.status(bk.get("de_total", "FY25")) == ("not meaningful", "red")         # distress
    bk2 = R.compute_all(make_fd({"FY25": {"ebit": 10, "interest_expense": 0}}))
    assert R.status(bk2.get("interest_coverage", "FY25")) == ("not meaningful", "grey")


def test_every_main_ratio_has_bands():
    for group, keys in config.MAIN_TABLE:
        for k in keys:
            table = config.NEUTRAL_LABELS if group in config.NEUTRAL_GROUPS else config.SCORE_THRESHOLDS
            assert k in table, k


def test_new_construct_bands_marked_proposed():
    full = open(config.__file__).read()
    text = full[full.index("SCORE_THRESHOLDS = {"):]                   # threshold / neutral-label tables only
    for k in ("op_ebit_margin", "net_margin", "roe", "roce", "roic", "asset_turnover", "pe", "peg", "div_yield",
              "p_ocf", "payout", "retention", "internal_growth", "sustainable_growth"):
        line = next(l for l in text.splitlines() if l.strip().startswith(f'"{k}": [('))
        block = text[text.index(line): text.index(line) + 400]
        assert "PROPOSED - review" in block, k


# ------------------------------------------------------------------ 3. More detail contents
def test_detail_section_contains_required_ratios():
    keys = R.compute_all(make_fd(FULL)).detail_keys()
    for k in ("wc_days", "dio", "dso", "dpo", "bs_accrual", "dsri", "cce_reported", "interest_coverage_reported",
              "net_debt_ebitda_reported", "dir_current", "dir_total", "de_borrowings", "dscr_ebit"):
        assert k in keys, k
    for k in ("rp_revenue_share", "rp_receivables_share", "rp_advances_revenue", "gmi", "altman_z1"):
        assert k not in keys, k                    # RP only when data exists; components in breakdowns


def test_rp_detail_rows_appear_when_data_exists():
    fd = make_fd({"FY25": {"revenue": 1000, "related_party_revenue": 50}})
    assert "rp_revenue_share" in R.compute_all(fd).detail_keys()


# ------------------------------------------------------------------ 5. RP signals as flags only when data exists
def test_rp_flags_only_with_data():
    fd = make_fd({"FY25": {"revenue": 1000, "receivables": 200}})
    assert not [f for f in R.red_flags(fd, R.compute_all(fd)) if f.code.startswith("rp_")]
    fd2 = D.apply_manual(fd, {("related_party_revenue", "FY25"): 50})
    f = [x for x in R.red_flags(fd2, R.compute_all(fd2)) if x.code == "rp_revenue_share"]
    assert f and f[0].severity == "info" and "below the 20% threshold" in f[0].text


# ------------------------------------------------------------------ 7. HDFC: never a blocking error for financials
def _bank(items, src=D.SRC_YAHOO):
    fd = make_fd(items, {"is_financial": True})
    fd.sources.loc[:, :] = fd.sources.where(fd.sources == "", src)
    return fd


def test_bank_size_not_flagged_implausible():
    rep = D.validate(_bank({"FY25": {"revenue": 400000, "total_assets": 6_500_000}}))   # SBI-sized
    assert not [i for i in rep.issues if i.check == "Units"]


def test_any_financial_error_is_proceed_anyway():
    rep = D.validate(_bank({"FY25": {"revenue": 4e7, "total_assets": 9e7}}))            # absurd -> Units error
    units = [i for i in rep.errors if i.check == "Units"]
    assert units and units[0].overridable and not rep.blocking_errors
    rep2 = D.validate(_bank({"FY25": {"cfo": 10}}))                                     # Missing-data error
    assert rep2.errors and not rep2.blocking_errors and rep2.can_score(proceed_anyway=True)


def test_lender_detected_from_statement_structure_when_profile_missing():
    T = pd.Timestamp("2025-03-31")
    inc = pd.DataFrame({T: [3000e7, 1200e7]}, index=["Total Revenue", "Net Interest Income"])
    bs = pd.DataFrame({T: [40000e7, 35000e7, 4500e7]},
                      index=["Total Assets", "Total Liabilities Net Minority Interest", "Stockholders Equity"])
    fd = D.build_from_yahoo_frames(inc, bs, None, {}, "HDFCBANK.NS")        # info empty: profile failed
    assert fd.meta["is_financial"] and "lender" in fd.meta["financial_reason"]


def test_industrial_not_mistaken_for_lender():
    T = pd.Timestamp("2025-03-31")
    inc = pd.DataFrame({T: [3000e7, -20e7]}, index=["Total Revenue", "Net Interest Income"])
    bs = pd.DataFrame({T: [4000e7, 1500e7]}, index=["Total Assets", "Current Assets"])
    assert not D.build_from_yahoo_frames(inc, bs, None, {}, "X.NS").meta["is_financial"]


def test_app_hdfc_with_missing_profile_shows_banner_and_no_block(monkeypatch):
    from streamlit.testing.v1 import AppTest
    T = pd.Timestamp

    def fake_fetch(t):     # profile failed AND structure not recognisable: only the search name identifies it
        inc = pd.DataFrame({T("2025-03-31"): [3000e7]}, index=["Total Revenue"])
        bs = pd.DataFrame({T("2025-03-31"): [9e14, 300e7, 400e7]},          # absurd assets -> Units error
                          index=["Total Assets", "Total Liabilities Net Minority Interest", "Stockholders Equity"])
        fd = D.build_from_yahoo_frames(inc, bs, None, {}, t)
        fd.meta["fetched_at"] = "now"
        return fd
    monkeypatch.setattr(D, "fetch_yahoo", fake_fetch)
    monkeypatch.setattr(D, "search_companies",
                        lambda q: [{"name": "HDFC Bank Limited", "ticker": "HDFCBANK.NS", "exchange": "NSE"}])
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    at.sidebar.text_input[0].set_value("hdfc").run()
    at.sidebar.button[0].click().run()
    assert not at.exception
    errors = " ".join(e.value for e in at.error)
    assert "Financial company" in errors                              # banner
    assert "Fix these before scoring" not in errors                   # no hard block
    assert any("Proceed anyway" in c.label for c in at.checkbox)
    assert "App." in " ".join(m.value for m in at.markdown)           # applicability icons for financials


def test_app_nonfinancial_hides_applicability():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    at.sidebar.radio[0].set_value("Demo mode (hypothetical sample)").run()
    at.sidebar.button[0].click().run()
    md = " ".join(m.value for m in at.markdown)
    for heading in ("CCIRS inputs", "Performance and valuation context", "Liquidity", "Solvency",
                    "Earnings integrity", "Profitability", "Efficiency", "Market perception", "Growth and payout"):
        assert heading in md, heading
    assert "shown for context, not part of the CCIRS score" in " ".join(c.value for c in at.caption)
    assert "App." not in md and "Reason / note" not in md
