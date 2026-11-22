"""Stage 2 ratio tests: Apex reference case, edge cases, Beneish/Altman/Piotroski mechanics."""
import math

import numpy as np
import pytest

import data as D
import ratios as R


def make_fd(years: dict, meta=None) -> D.FinancialData:
    vals, srcs = D.empty_frame(years.keys())
    for fy, items in years.items():
        for k, x in items.items():
            vals.at[k, fy], srcs.at[k, fy] = float(x), D.SRC_UPLOAD
    return D.FinancialData(vals, srcs, meta or {}, [])


# ------------------------------------------------------------------ Apex (brief) - hypothetical sample
@pytest.fixture(scope="module")
def apex():
    return R.compute_all(D.apply_derivations(D.demo_data()))


def test_apex_current_ratio(apex):
    assert apex.value("current_ratio", "FY26") == pytest.approx(1.80)


def test_apex_wc_days(apex):
    assert apex.value("wc_days", "FY26") == pytest.approx(73.0, abs=0.5)


def test_apex_ocf_ratio(apex):
    assert apex.value("ocf_ratio", "FY26") == pytest.approx(0.10)


def test_apex_cce_both_years(apex):
    assert apex.value("cce", "FY26") == pytest.approx(0.20)
    assert apex.value("cce", "FY25") == pytest.approx(0.75)


def test_apex_sloan_uses_average_assets(apex):
    r = apex.get("sloan", "FY26")
    assert r.inputs["Average total assets"] == pytest.approx(1350)
    assert r.value == pytest.approx(0.0444, abs=0.0005)


def test_apex_dir(apex):
    r = apex.get("dir_current", "FY26")
    assert r.value == pytest.approx(116, abs=0.5)
    assert r.note == ""          # cash opex was supplied, not proxied
    sel = apex.get("dir", "FY26")    # setting 'include' but Apex has no non-current figure -> current used
    assert sel.value == pytest.approx(116, abs=0.5) and "current-only version used" in sel.note


def test_apex_first_year_needs_prior(apex):
    r = apex.get("sloan", "FY25")
    assert r.status == R.NA and "prior year not in data" in r.reason


def test_apex_is_labelled_demo():
    fd = D.demo_data()
    assert fd.meta["is_demo"] and "HYPOTHETICAL" in fd.meta["name"]


# ------------------------------------------------------------------ formula text and inputs present
def test_every_result_has_formula_and_inputs(apex):
    for (key, fy), r in apex.results.items():
        assert r.formula and r.status in (R.OK, R.NA, R.NM)
        if r.status == R.OK:
            assert r.inputs, key


# ------------------------------------------------------------------ edge cases
def test_negative_equity_is_not_meaningful_and_distress():
    fd = make_fd({"FY25": {"total_debt": 2000, "equity": -500, "lease_liabilities": 100}})
    for key in ("de_total", "de_borrowings"):
        r = R.compute_all(fd).get(key, "FY25")
        assert r.status == R.NM and r.distress and r.value is None


def test_negative_ebitda_is_not_meaningful_and_distress():
    fd = make_fd({"FY25": {"cfo": 50, "ebitda": -100, "total_debt": 500, "cash": 10,
                           "marketable_securities": 0}})
    bk = R.compute_all(fd)
    for key in ("cce", "net_debt_ebitda"):
        r = bk.get(key, "FY25")
        assert r.status == R.NM and r.distress and r.value is None


def test_zero_denominators():
    fd = make_fd({"FY25": {"ebit": 100, "interest_expense": 0, "current_assets": 100,
                           "current_liabilities": 0, "cfo": 10, "capex": 5, "current_debt": 0}})
    bk = R.compute_all(fd)
    for key in ("interest_coverage", "current_ratio", "ocf_ratio", "dscr"):
        r = bk.get(key, "FY25")
        assert r.status == R.NM and not r.distress and r.value is None, key


def test_missing_inputs_never_become_zero():
    fd = make_fd({"FY25": {"current_assets": 500}})       # current liabilities missing
    r = R.compute_all(fd).get("current_ratio", "FY25")
    assert r.status == R.NA and r.value is None
    assert r.inputs["Total current liabilities FY25"] is None
    assert "Total current liabilities" in r.reason


def test_net_loss_and_negative_cfo_still_compute():
    fd = make_fd({"FY24": {"total_assets": 1000}, "FY25": {"pat": -200, "cfo": -50, "total_assets": 900,
                                                           "ebitda": 100, "current_liabilities": 300}})
    bk = R.compute_all(fd)
    assert bk.value("sloan", "FY25") == pytest.approx((-200 + 50) / 950)
    assert bk.value("cce", "FY25") == pytest.approx(-0.5)        # negative CFO, positive EBITDA: meaningful


def test_debt_equity_two_ways_and_missing_lease_split():
    fd = make_fd({"FY25": {"total_debt": 600, "lease_liabilities": 100, "equity": 1000},
                  "FY24": {"total_debt": 600, "equity": 1000}})
    bk = R.compute_all(fd)
    assert bk.value("de_total", "FY25") == pytest.approx(0.6)
    assert bk.value("de_borrowings", "FY25") == pytest.approx(0.5)
    r = bk.get("de_borrowings", "FY24")
    assert r.status == R.NA and "Lease split not available" in r.reason


def test_dir_proxy_label():
    fd = D.apply_derivations(make_fd({"FY25": {"cash": 10, "marketable_securities": 0, "receivables": 90,
                                               "revenue": 1000, "ebitda": 270}}))
    r = R.compute_all(fd).get("dir", "FY25")
    assert r.value == pytest.approx(100 / (730 / 365))
    assert "PROXY" in r.note


def test_ccc_components():
    fd = make_fd({"FY25": {"inventory": 100, "cogs": 730, "receivables": 200, "revenue": 1460,
                           "accounts_payable": 60}})
    bk = R.compute_all(fd)
    assert bk.value("ccc", "FY25") == pytest.approx(50 + 50 - 30)


def test_net_debt_cash_rich_note():
    fd = make_fd({"FY25": {"total_debt": 10, "cash": 500, "marketable_securities": 100, "ebitda": 200}})
    r = R.compute_all(fd).get("net_debt_ebitda", "FY25")
    assert r.value == pytest.approx(-2.95) and "Net cash" in r.note


# ------------------------------------------------------------------ Beneish
FULL = {
    "FY24": dict(revenue=1000, cogs=600, sga=150, depreciation=50, ppe=500, receivables=150, current_assets=400,
                 total_assets=1200, current_liabilities=300, long_term_debt=200, pat=80, cfo=100,
                 retained_earnings=300, ebit=120, equity=600, total_liabilities=600, shares_outstanding=10,
                 interest_expense=20, cash=50, marketable_securities=10, total_debt=250),
    "FY25": dict(revenue=1200, cogs=750, sga=170, depreciation=55, ppe=520, receivables=260, current_assets=500,
                 total_assets=1400, current_liabilities=380, long_term_debt=220, pat=110, cfo=40,
                 retained_earnings=380, ebit=150, equity=680, total_liabilities=720, shares_outstanding=10,
                 interest_expense=22, cash=40, marketable_securities=10, total_debt=270),
}


def test_beneish_full_computation_matches_hand_formula():
    bk = R.compute_all(make_fd(FULL))
    c = {k: bk.value(k, "FY25") for k in R.BENEISH_PARTS}
    assert all(v is not None for v in c.values())
    assert c["dsri"] == pytest.approx((260 / 1200) / (150 / 1000))
    assert c["gmi"] == pytest.approx(0.40 / 0.375)
    assert c["sgi"] == pytest.approx(1.2)
    assert c["tata"] == pytest.approx((110 - 40) / 1400)
    expected = -4.84 + sum(R.BENEISH_COEF[k] * c[k] for k in R.BENEISH_PARTS)
    m = bk.get("beneish_m", "FY25")
    assert m.value == pytest.approx(expected)
    assert m.value > -1.78 and "warrants investigation" in m.interpretation   # receivables jump + low CFO


def test_beneish_names_missing_component():
    years = {fy: {k: v for k, v in d.items() if k != "sga"} for fy, d in FULL.items()}
    m = R.compute_all(make_fd(years)).get("beneish_m", "FY25")
    assert m.value is None and "SGAI" in m.reason


# ------------------------------------------------------------------ Altman
def test_altman_z2_and_z1_hand_values():
    bk = R.compute_all(make_fd(FULL, {"industry": "Telecom Services", "sector": "Communication Services"}))
    x1, x2, x3, x4, x5 = (500 - 380) / 1400, 380 / 1400, 150 / 1400, 680 / 720, 1200 / 1400
    assert bk.value("altman_z2", "FY25") == pytest.approx(6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4)
    assert bk.value("altman_z1", "FY25") == pytest.approx(
        0.717 * x1 + 0.847 * x2 + 3.107 * x3 + 0.420 * x4 + 0.998 * x5)
    assert bk.altman_key == "altman_z2"
    assert bk.value("altman", "FY25") == bk.value("altman_z2", "FY25")


@pytest.mark.parametrize("industry,sector,key", [
    ("Auto Manufacturers", "Consumer Cyclical", "altman_z1"),
    ("Steel", "Basic Materials", "altman_z1"),
    ("Engineering & Construction", "Industrials", "altman_z2"),
    ("Utilities - Independent Power Producers", "Utilities", "altman_z2"),
])
def test_altman_variant_selection(industry, sector, key):
    assert R.altman_variant({"industry": industry, "sector": sector})[0] == key


def test_altman_negative_equity_still_computes():
    years = {fy: dict(d) for fy, d in FULL.items()}
    years["FY25"]["equity"] = -300
    r = R.compute_all(make_fd(years)).get("altman_z2", "FY25")
    assert r.status == R.OK and "Negative equity" in r.note


# ------------------------------------------------------------------ Piotroski
def test_piotroski_criteria_pass_fail():
    r = R.compute_all(make_fd(FULL)).get("piotroski", "FY25")
    assert r.status == R.OK and len(r.details) == 9
    res = {d["Criterion"][:2]: d["Result"] for d in r.details}
    assert res["1."] == "Pass" and res["2."] == "Pass"
    assert res["4."] == "Fail"                  # CFO 40 < NI 110
    assert res["7."] == "Pass"                  # shares unchanged
    assert r.value == sum(1 for d in r.details if d["Result"] == "Pass")


def test_piotroski_scored_on_assessable_criteria():
    years = {fy: {k: v for k, v in d.items() if k != "shares_outstanding"} for fy, d in FULL.items()}
    r = R.compute_all(make_fd(years)).get("piotroski", "FY25")
    assert r.status == R.OK and r.extra["assessable"] == 8
    passed = sum(1 for d in r.details if d["Result"] == "Pass")
    assert r.extra["passed"] == passed and r.extra["score_0_100"] == pytest.approx(passed / 8 * 100)
    assert r.display().startswith(f"{passed}/8")
    assert "not counted as failures" in r.note


def test_piotroski_below_minimum_assessable_is_unavailable():
    years = {fy: {k: d[k] for k in ("pat", "cfo", "total_assets")} for fy, d in FULL.items()}
    r = R.compute_all(make_fd(years)).get("piotroski", "FY25")
    assert r.status == R.NA and "minimum" in r.reason
