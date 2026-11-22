"""Tests for the post-Stage-2 revisions A-K (operating EBITDA/EBIT, revenue label, liquid investments,
minority interest, financial companies, interpretations, distress grouping, flags)."""
import pandas as pd
import pytest

import config
import data as D
import ratios as R

T = pd.Timestamp("2025-03-31")


def make_fd(years: dict, meta=None, src=D.SRC_UPLOAD) -> D.FinancialData:
    vals, srcs = D.empty_frame(years.keys())
    for fy, items in years.items():
        for k, x in items.items():
            vals.at[k, fy], srcs.at[k, fy] = float(x), src
    return D.FinancialData(vals, srcs, meta or {}, [])


def yahoo(inc_rows: dict, bs_rows: dict | None = None, cf_rows: dict | None = None, info=None):
    def frame(rows):
        return pd.DataFrame({T: [v * 1e7 for v in rows.values()]}, index=list(rows)) if rows else None
    return D.apply_derivations(D.build_from_yahoo_frames(frame(inc_rows), frame(bs_rows), frame(cf_rows),
                                                         info or {}, "X.NS"))


# ------------------------------------------------------------------ A1 / G operating EBITDA and EBIT
@pytest.fixture
def maruti_like():
    return yahoo({"Total Revenue": 10000, "EBITDA": 1000, "EBIT": 800, "Interest Expense": 20,
                  "Interest Income Non Operating": 100, "Other Non Operating Income Expenses": 50,
                  "Earnings From Equity Interest": 30},
                 {"Total Assets": 9000, "Current Debt": 30},
                 {"Operating Cash Flow": 730, "Capital Expenditure": -300})


def test_operating_ebitda_derived_from_yahoo_parts(maruti_like):
    fd = maruti_like
    assert fd.get("other_income", "FY25") == pytest.approx(150)
    assert fd.get("associate_share", "FY25") == pytest.approx(30)
    assert fd.get("operating_ebitda", "FY25") == pytest.approx(820)
    assert fd.get("operating_ebit", "FY25") == pytest.approx(620)
    assert fd.sources.at["operating_ebitda", "FY25"].endswith(D.DERIVED_SUFFIX)
    assert "Interest Income Non Operating" in fd.meta["labels_used"]["other_income"]


def test_cce_uses_operating_ebitda_and_keeps_reported(maruti_like):
    bk = R.compute_all(maruti_like)
    assert bk.value("cce", "FY25") == pytest.approx(730 / 820)          # ~89%
    assert bk.value("cce_reported", "FY25") == pytest.approx(0.73)      # 73%
    assert "Operating EBITDA = EBITDA less other income 150.0" in bk.get("cce", "FY25").note


def test_interest_coverage_and_dscr_use_operating_ebit(maruti_like):
    bk = R.compute_all(maruti_like)
    assert bk.value("interest_coverage", "FY25") == pytest.approx(620 / 20)
    assert bk.value("interest_coverage_reported", "FY25") == pytest.approx(800 / 20)
    assert bk.value("dscr_ebit", "FY25") == pytest.approx(620 / 50)     # operating EBIT, not EBITDA
    assert bk.value("dscr", "FY25") == pytest.approx((730 - 300) / 50)  # cash version unchanged


def test_fallback_to_reported_ebitda_is_labelled():
    fd = D.apply_derivations(make_fd({"FY25": {"cfo": 50, "ebitda": 100}}))
    r = R.compute_all(fd).get("cce", "FY25")
    assert r.value == pytest.approx(0.5) and "reported EBITDA used" in r.note


def test_operating_ebitda_notes_missing_adjustment():
    fd = D.apply_derivations(make_fd({"FY25": {"ebitda": 100, "other_income": 10}}))
    assert fd.get("operating_ebitda", "FY25") == 90
    log = fd.meta["op_adjustments"]["FY25"]["operating_ebitda"]
    assert "share of associates/JVs" in log and "not reported - not adjusted" in log


# ------------------------------------------------------------------ A2 revenue label
def test_revenue_label_shown_and_narrower_flagged():
    fd = yahoo({"Total Revenue": 950, "Operating Revenue": 1000}, {"Total Assets": 2000})
    assert "[Yahoo: Total Revenue]" in fd.display_table().index[0]
    rep = D.validate(fd)
    assert any(i.check == "Revenue definition" and "narrower" in i.message for i in rep.issues)


def test_revenue_fallback_label_flagged():
    fd = yahoo({"Operating Revenue": 1000}, {"Total Assets": 2000})
    msgs = D.revenue_label_check(fd)
    assert msgs and "Operating Revenue" in msgs[0][1]


# ------------------------------------------------------------------ A3 / H liquid investments
LIQ = {"FY25": {"cash": 100, "marketable_securities": 200, "noncurrent_liquid_investments": 700,
                "receivables": 100, "cash_opex": 3650, "total_debt": 500, "ebitda": 1000}}


def test_dir_both_ways_and_setting_controls_score_version():
    fd = make_fd(LIQ)
    inc = R.compute_all(fd, {"include_noncurrent": True})
    exc = R.compute_all(fd, {"include_noncurrent": False})
    assert inc.value("dir_current", "FY25") == pytest.approx(400 / 10)
    assert inc.value("dir_total", "FY25") == pytest.approx(1100 / 10)
    assert inc.value("dir", "FY25") == pytest.approx(110) and "Total version" in inc.get("dir", "FY25").note
    assert exc.value("dir", "FY25") == pytest.approx(40) and "Current-only" in exc.get("dir", "FY25").note


def test_net_debt_uses_same_setting():
    fd = make_fd(LIQ)
    assert R.compute_all(fd, {"include_noncurrent": True}).value("net_debt_ebitda", "FY25") == pytest.approx(-0.5)
    r = R.compute_all(fd, {"include_noncurrent": False}).get("net_debt_ebitda", "FY25")
    assert r.value == pytest.approx(0.2) and "exclude non-current" in r.note


def test_default_setting_comes_from_config():
    assert R.default_settings()["include_noncurrent"] == config.LIQUIDITY_INCLUDE_NONCURRENT


# ------------------------------------------------------------------ A4 minority interest
def test_blank_minority_zero_when_reconciles():
    fd = D.apply_derivations(make_fd({"FY25": {"total_assets": 1000, "total_liabilities": 600, "equity": 395}}))
    assert fd.get("minority_interest", "FY25") == 0
    assert not any("minority interest not available" in i.message for i in D.validate(fd).issues)


def test_blank_minority_stays_blank_when_gap():
    fd = D.apply_derivations(make_fd({"FY25": {"total_assets": 1000, "total_liabilities": 600, "equity": 300}}))
    assert pd.isna(fd.get("minority_interest", "FY25"))


# ------------------------------------------------------------------ B financial companies
def bank_fd():
    return make_fd({"FY24": {"revenue": 3000, "total_assets": 40000, "pat": 400},
                    "FY25": {"revenue": 3300, "total_assets": 44000, "pat": 450, "cfo": -900}},
                   {"is_financial": True, "industry": "Banks - Regional", "sector": "Financial Services"})


def test_financial_applicability_tags():
    bk = R.compute_all(bank_fd())
    for k in ("current_ratio", "wc_days", "dir", "ccc", "dio", "dso", "dpo", "ocf_ratio", "cce", "sloan",
              "bs_accrual", "interest_coverage", "net_debt_ebitda", "de_total", "dscr", "beneish_m", "altman"):
        assert bk.get(k, "FY25").applicability == "Not valid", k
        assert not bk.is_scoreable(k)
    assert bk.get("piotroski", "FY25").applicability == "Limited"
    assert bk.score_label == "Indicative only"
    assert bk.table()["Applicability"].isin(["Valid", "Limited", "Not valid"]).all()


def test_financial_company_missing_inputs_do_not_crash():
    bk = R.compute_all(bank_fd())
    assert bk.get("current_ratio", "FY25").status == R.NA
    assert R.red_flags(bank_fd(), bk) is not None


def test_nonfinancial_ratios_valid_by_default():
    bk = R.compute_all(make_fd(LIQ))
    assert bk.get("cce", "FY25").applicability == "Valid" and bk.score_label == ""


# ------------------------------------------------------------------ C interpretations
def test_ok_rows_always_have_interpretation():
    bk = R.compute_all(D.apply_derivations(D.demo_data()))
    for (k, fy), r in bk.results.items():
        assert r.explanation and r.explanation != "-", (k, fy)
        if r.ok:
            assert r.interpretation, (k, fy)
    assert bk.get("cce", "FY26").interpretation == "below 40%: major concern - most EBITDA is not reaching cash"
    assert bk.get("cce", "FY25").interpretation == "60-80%: acceptable"


@pytest.mark.parametrize("v,text", [(-0.1, "CFO negative"), (0.9, "strong range (80-100%)"),
                                    (1.2, "Ind AS 116")])
def test_cce_interpretation_bands(v, text):
    assert text in R.interpret("cce", v)


# ------------------------------------------------------------------ D grouped distress
def test_distress_summary_grouped_by_years():
    years = {f"FY{y}": {"total_debt": 2000, "equity": -500, "lease_liabilities": 100} for y in (23, 24, 25, 26)}
    lines = R.compute_all(make_fd(years)).distress_summary()
    assert len(lines) == 1
    assert lines[0].startswith("FY23-FY26: shareholders' equity negative")
    assert "Debt/Equity (excl. leases) and Debt/Equity (incl. leases) are not meaningful" in lines[0]


# ------------------------------------------------------------------ E captive finance + confirmable CFO flag
def test_captive_finance_note_and_confirmable_cfo_flag():
    fd = make_fd({"FY24": {"cfo": -500, "ebit": 900}, "FY25": {"cfo": -700, "ebit": 1000}})
    flags = {f.code: f for f in R.red_flags(fd, R.compute_all(fd))}
    assert "captive finance subsidiary" in flags["captive_finance"].text
    assert flags["cfo_negative"].confirmable and flags["cfo_negative"].years == "FY24-FY25"


# ------------------------------------------------------------------ I lease note
def test_cce_lease_note_when_lease_heavy():
    fd = make_fd({"FY25": {"cfo": 90, "ebitda": 100, "total_debt": 100, "lease_liabilities": 60}})
    assert R.LEASE_NOTE in R.compute_all(fd).get("cce", "FY25").note
    fd2 = make_fd({"FY25": {"cfo": 90, "ebitda": 100, "total_debt": 100, "lease_liabilities": 10}})
    assert R.LEASE_NOTE not in R.compute_all(fd2).get("cce", "FY25").note


# ------------------------------------------------------------------ J inventory build flag
def test_inventory_growth_flag_with_numbers():
    fd = make_fd({"FY24": {"inventory": 100, "revenue": 1000}, "FY25": {"inventory": 150, "revenue": 1100}})
    f = [x for x in R.red_flags(fd, R.compute_all(fd)) if x.code == "inventory_build"]
    assert f and "+50.0%" in f[0].text and "+10.0%" in f[0].text and "40.0pp" in f[0].text


# ------------------------------------------------------------------ K related-party signals, separate
def test_related_party_flags_are_separate_and_manual_entry_works():
    fd = make_fd({"FY25": {"revenue": 1000, "receivables": 200}})
    fd = D.apply_manual(fd, {("related_party_revenue", "FY25"): 250, ("related_party_receivables", "FY25"): 80,
                             ("related_party_advances", "FY25"): 600})
    assert fd.sources.at["related_party_revenue", "FY25"] == D.SRC_MANUAL
    codes = [f.code for f in R.red_flags(fd, R.compute_all(fd))]
    assert {"rp_revenue_share", "rp_receivables_share", "rp_advances_revenue"} <= set(codes)


def test_template_has_new_rows():
    csv = D.make_template_csv().decode()
    for k in ("other_income", "associate_share", "noncurrent_liquid_investments", "related_party_revenue",
              "related_party_receivables", "lease_liabilities"):
        assert k in csv
