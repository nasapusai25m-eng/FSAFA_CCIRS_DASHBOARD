"""Context constructs (profitability, efficiency, market, growth), DuPont, ROIIC, DSCR pre-capex, applicability."""
import math

import pytest

import data as D
import ratios as R

Y = {
    "FY22": dict(pat=60, shares_outstanding=10),
    "FY23": dict(revenue=900, pat=70, total_assets=1100, equity=550, shares_outstanding=10),
    "FY24": dict(revenue=1000, ebit=150, pbt=130, pat=100, total_assets=1200, equity=600, total_debt=300,
                 cash=50, marketable_securities=50, shares_outstanding=10),
    "FY25": dict(revenue=1200, ebit=200, pbt=180, pat=135, total_assets=1400, equity=700, total_debt=350,
                 cash=60, marketable_securities=40, cfo=150, capex=80, interest_expense=20, current_debt=30,
                 dividends_paid=40.5, shares_outstanding=10),
}


def fd(meta=None):
    vals, srcs = D.empty_frame(Y.keys())
    for y, items in Y.items():
        for k, v in items.items():
            vals.at[k, y], srcs.at[k, y] = float(v), D.SRC_UPLOAD
    return D.FinancialData(vals, srcs, {"price": 270.0, "market_cap_cr": 2700.0, **(meta or {})}, [])


@pytest.fixture(scope="module")
def bk():
    return R.compute_all(fd(), {"include_noncurrent": False})


def test_profitability(bk):
    assert bk.value("net_margin", "FY25") == pytest.approx(135 / 1200)
    assert bk.value("op_ebit_margin", "FY25") == pytest.approx(200 / 1200)       # falls back to reported EBIT
    assert bk.value("roe", "FY25") == pytest.approx(135 / 650)
    assert bk.value("roce", "FY25") == pytest.approx(200 / 1050)
    t = 45 / 180                                                                  # 25%
    assert bk.value("roic", "FY25") == pytest.approx(200 * (1 - t) / (700 + 350 - 100))


def test_tax_rate_clip_and_loss():
    assert R._tax_rate(100, 30)[0] == pytest.approx(0.40)        # 70% clipped to 40%
    assert R._tax_rate(-10, -12)[0] == 0.0


def test_roiic(bk):
    n1, n0 = 200 * 0.75, 150 * (1 - 30 / 130)
    ic1, ic0 = 950, 800
    assert bk.value("roiic", "FY25") == pytest.approx((n1 - n0) / (ic1 - ic0))


def test_efficiency(bk):
    assert bk.value("asset_turnover", "FY25") == pytest.approx(1200 / 1300)


def test_market_perception_latest_year_only(bk):
    eps = 13.5
    assert bk.value("pe", "FY25") == pytest.approx(270 / eps)
    cagr = (13.5 / 6.0) ** (1 / 3) - 1
    assert bk.value("peg", "FY25") == pytest.approx((270 / eps) / (cagr * 100))
    assert bk.value("div_yield", "FY25") == pytest.approx(40.5 / 2700)
    assert bk.value("p_ocf", "FY25") == pytest.approx(2700 / 150)
    assert bk.get("pe", "FY24").status == R.NA and "latest year only" in bk.get("pe", "FY24").reason
    assert "P/OCF below P/E" in bk.get("p_ocf", "FY25").note       # 18.0x < 20.0x


def test_peg_not_meaningful_when_growth_negative():
    f = fd()
    f.values.at["pat", "FY22"] = 200.0                         # EPS fell over 3 years
    assert R.compute_all(f).get("peg", "FY25").status == R.NM


def test_growth_and_payout(bk):
    payout = 40.5 / 135
    assert bk.value("payout", "FY25") == pytest.approx(payout)
    assert bk.value("retention", "FY25") == pytest.approx(1 - payout)
    assert bk.value("internal_growth", "FY25") == pytest.approx(135 / 1300 * (1 - payout))
    assert bk.value("sustainable_growth", "FY25") == pytest.approx(135 / 650 * (1 - payout))


def test_dupont_multiplies_back_to_roe(bk):
    assert bk.value("dupont", "FY25") == pytest.approx(bk.value("roe", "FY25"))
    assert bk.value("dupont_mod", "FY25") == pytest.approx(bk.value("roe", "FY25"))
    assert "net margin" in bk.get("dupont", "FY25").note


def test_dscr_precapex_and_growth_capex_note(bk):
    assert bk.value("dscr_precapex", "FY25") == pytest.approx(150 / 50)
    assert bk.value("dscr", "FY25") == pytest.approx(70 / 50)
    assert "penalises growth capex" in bk.get("dscr", "FY25").note


def test_neutral_status_for_valuation(bk):
    rows = {r["key"]: r for r in bk.main_rows("FY25")}
    for k in ("pe", "peg", "div_yield", "p_ocf", "payout", "retention", "internal_growth", "sustainable_growth"):
        assert rows[k]["colour"] == "grey", k
    assert rows["pe"]["status"] == "premium valuation"        # 20.0x sits on the 20x boundary -> next band
    assert rows["roe"]["colour"] in ("green", "amber", "red")


def test_new_rows_have_interpretation(bk):
    for k in ("op_ebit_margin", "net_margin", "roe", "roce", "roic", "roiic", "asset_turnover", "pe", "peg",
              "div_yield", "p_ocf", "payout", "retention", "internal_growth", "sustainable_growth", "dupont",
              "dupont_mod", "dscr_precapex"):
        assert bk.get(k, "FY25").interpretation, k


def test_financial_applicability_for_new_constructs():
    b = R.compute_all(fd({"is_financial": True}))
    tag = lambda k: b.get(k, "FY25").applicability
    for k in ("roe", "pe", "div_yield", "payout"):
        assert tag(k) in ("Valid", "Limited"), k
    for k in ("p_ocf", "roce", "roic", "asset_turnover", "dio", "dso", "dpo", "op_ebit_margin"):
        assert tag(k) == "Not valid", k


def test_missing_market_data_stays_unavailable():
    f = fd()
    f.meta.pop("price")
    r = R.compute_all(f).get("pe", "FY25")
    assert r.status == R.NA and r.value is None


def test_readme_notes_llcr():
    import pathlib
    assert "LLCR" in (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()
