"""Stage 1 tests - run with:  python -m pytest -q   (no internet needed)."""
import io
import math

import numpy as np
import pandas as pd
import pytest

import config
import data as D


# ------------------------------------------------------------------ helpers
def yahoo_like(values_by_date: dict, labels: list[str]) -> pd.DataFrame:
    """Build a frame shaped like yfinance output: rows = labels, columns = period-end Timestamps."""
    cols = {pd.Timestamp(d): vals for d, vals in values_by_date.items()}
    return pd.DataFrame(cols, index=labels)


def sample_yahoo():
    dates = ["2025-03-31", "2024-03-31", "2023-03-31"]
    inc = yahoo_like({d: v for d, v in zip(dates, [
        [1000e7, 600e7, 150e7, 120e7, 30e7, 90e7, 20e7],
        [900e7, 540e7, 135e7, 110e7, 28e7, 80e7, 18e7],
        [800e7, 480e7, 120e7, 100e7, 25e7, 70e7, np.nan]])},
        ["Total Revenue", "Cost Of Revenue", "EBITDA", "EBIT", "Reconciled Depreciation",
         "Pretax Income", "Interest Expense"])
    bs = yahoo_like({d: v for d, v in zip(dates, [
        [1200e7, 500e7, 700e7, 400e7, 50e7],
        [1100e7, 480e7, 620e7, 380e7, 45e7],
        [1000e7, 470e7, 530e7, 360e7, np.nan]])},
        ["Total Assets", "Total Liabilities Net Minority Interest", "Stockholders Equity",
         "Current Assets", "Cash And Cash Equivalents"])
    cf = yahoo_like({d: v for d, v in zip(dates, [
        [110e7, -60e7], [95e7, -55e7], [85e7, -50e7]])},
        ["Operating Cash Flow", "Capital Expenditure"])
    info = {"longName": "Test Industries Ltd", "sector": "Industrials", "industry": "Engineering",
            "financialCurrency": "INR", "marketCap": 5000e7}
    return D.build_from_yahoo_frames(inc, bs, cf, info, "TEST.NS")


# ------------------------------------------------------------------ fiscal years
@pytest.mark.parametrize("d,expected", [
    ("2025-03-31", "FY25"), ("2024-03-31", "FY24"), ("2024-12-31", "FY25"),
    ("2025-01-15", "FY25"), ("2024-04-01", "FY25"), ("1999-03-31", "FY99"),
])
def test_fy_label(d, expected):
    assert D.fy_label(d) == expected


@pytest.mark.parametrize("h,year", [
    ("FY25", 2025), ("FY 2025", 2025), ("FY'25", 2025), ("2024-25", 2025), ("2024/2025", 2025),
    ("1999-00", 2000), ("Mar-25", 2025), ("March 2025", 2025), ("2025", 2025),
    (pd.Timestamp("2025-03-31"), 2025), ("Line item", None), ("Unnamed: 7", None),
])
def test_parse_fy_header(h, year):
    assert D.parse_fy_header(h) == year


def test_fy_sort_across_century():
    assert D.sort_fy(["FY01", "FY99", "FY25"]) == ["FY99", "FY01", "FY25"]


# ------------------------------------------------------------------ numbers
@pytest.mark.parametrize("x,expected", [
    ("1,234.5", 1234.5), ("(120)", -120.0), ("", np.nan), ("NA", np.nan), ("-", np.nan), (42, 42.0),
])
def test_clean_number(x, expected):
    got = D.clean_number(x)
    assert (math.isnan(got) and math.isnan(expected)) or got == expected


def test_clean_number_rejects_text():
    with pytest.raises(ValueError):
        D.clean_number("approx 50")


# ------------------------------------------------------------------ Yahoo mapping
def test_yahoo_mapping_crore_and_labels():
    fd = sample_yahoo()
    assert fd.years == ["FY23", "FY24", "FY25"]
    assert fd.get("revenue", "FY25") == pytest.approx(1000.0)           # absolute -> crore
    assert fd.get("capex", "FY25") == pytest.approx(60.0)               # outflow stored positive
    assert fd.sources.at["revenue", "FY25"] == D.SRC_YAHOO
    assert math.isnan(fd.get("interest_expense", "FY23"))               # missing stays NaN, not 0
    assert fd.sources.at["interest_expense", "FY23"] == ""
    assert fd.meta["market_cap_cr"] == pytest.approx(5000.0)
    assert fd.meta["labels_used"]["revenue"] == "Total Revenue"


def test_yahoo_label_fallback_per_year():
    inc = yahoo_like({"2025-03-31": [np.nan, 500e7], "2024-03-31": [450e7, np.nan]},
                     ["Total Revenue", "Operating Revenue"])
    bs = yahoo_like({"2025-03-31": [900e7], "2024-03-31": [800e7]}, ["Total Assets"])
    fd = D.build_from_yahoo_frames(inc, bs, None, {}, "X.NS")
    assert fd.get("revenue", "FY25") == pytest.approx(500)
    assert fd.get("revenue", "FY24") == pytest.approx(450)


def test_yahoo_drops_stub_year():
    inc = yahoo_like({"2025-03-31": [500e7], "2021-03-31": [np.nan]}, ["Total Revenue"])
    fd = D.build_from_yahoo_frames(inc, None, None, {}, "X.NS")
    assert fd.years == ["FY25"]


def test_financial_company_detection():
    assert D.is_financial_company({"sector": "Financial Services", "industry": "Banks - Regional"})[0]
    assert D.is_financial_company({"longName": "Bajaj Housing Finance Limited"})[0]
    assert not D.is_financial_company({"sector": "Consumer Cyclical", "industry": "Auto Manufacturers",
                                       "longName": "Maruti Suzuki India Limited"})[0]


def test_normalize_ticker():
    assert D.normalize_ticker("maruti")[0] == "MARUTI.NS"
    assert D.normalize_ticker("500570.BO") == ("500570.BO", None)


# ------------------------------------------------------------------ template + upload
def test_template_roundtrip_xlsx():
    df = D.template_dataframe(n_years=2, last_fy=2026)
    df.loc[df.Key == "revenue", ["FY25", "FY26"]] = [1500, 1800]
    df.loc[df.Key == "capex", ["FY25", "FY26"]] = [-40, 50]          # negative capex -> made positive
    buf = io.BytesIO()
    df.to_excel(buf, sheet_name="Data", index=False)
    res = D.parse_upload(buf.getvalue(), "t.xlsx")
    assert not res.errors
    assert res.data.get("revenue", "FY26") == 1800
    assert res.data.get("capex", "FY25") == 40
    assert res.data.sources.at["revenue", "FY26"] == D.SRC_UPLOAD


def test_template_file_builds():
    assert D.make_template_xlsx()[:2] == b"PK"   # valid zip/xlsx
    assert b"related_party_advances" in D.make_template_csv()


def test_upload_units_lakh():
    csv = "Key,FY25\nrevenue,150000\n".encode()
    res = D.parse_upload(csv, "t.csv", unit="Rs lakh")
    assert res.data.get("revenue", "FY25") == pytest.approx(1500)


def test_upload_bad_cell_and_alias_labels():
    csv = "Particulars,2023-24,2024-25\nNet Sales,100,abc\nTrade receivables,20,25\nMystery row,1,2\n".encode()
    res = D.parse_upload(csv, "t.csv")
    assert any("Non-numeric" in e for e in res.errors)
    assert res.data.get("receivables", "FY25") == 25
    assert any("Mystery row" in w for w in res.warnings)


# ------------------------------------------------------------------ merge
def _upload(years_vals: dict) -> D.FinancialData:
    vals, srcs = D.empty_frame(years_vals.keys())
    for fy, items in years_vals.items():
        for k, x in items.items():
            vals.at[k, fy], srcs.at[k, fy] = x, D.SRC_UPLOAD
    return D.FinancialData(vals, srcs, {}, [])


def test_merge_prefers_upload_by_default_and_tags_sources():
    y = sample_yahoo()
    u = _upload({"FY25": {"revenue": 1010.0}, "FY22": {"revenue": 700.0, "total_assets": 950.0}})
    m = D.merge_sources(y, u)
    assert m.years == ["FY22", "FY23", "FY24", "FY25"]
    assert m.get("revenue", "FY25") == 1010.0 and m.sources.at["revenue", "FY25"] == D.SRC_UPLOAD
    assert m.get("total_assets", "FY25") == 1200.0 and m.sources.at["total_assets", "FY25"] == D.SRC_YAHOO
    m2 = D.merge_sources(y, u, prefer="yahoo")
    assert m2.get("revenue", "FY25") == 1000.0


def test_derivations_are_tagged():
    u = _upload({"FY25": {"pbt": 80.0, "interest_expense": 20.0, "depreciation": 30.0, "revenue": 1000.0}})
    d = D.apply_derivations(u)
    assert d.get("ebit", "FY25") == 100 and d.get("ebitda", "FY25") == 130
    assert d.get("cash_opex", "FY25") == 870
    assert d.sources.at["ebitda", "FY25"] == D.SRC_UPLOAD + D.DERIVED_SUFFIX


# ------------------------------------------------------------------ validation
def test_validation_clean_sample_has_no_errors():
    rep = D.validate(sample_yahoo(), yahoo=sample_yahoo())
    assert rep.ok_to_score


def test_validation_flags_imbalance_gap_jump_units():
    u = _upload({
        "FY21": {"revenue": 100.0, "total_assets": 200.0, "total_liabilities": 100.0, "equity": 100.0},
        "FY23": {"revenue": 1500.0, "total_assets": 300.0, "total_liabilities": 100.0, "equity": 100.0},
        "FY24": {"revenue": 140.0, "total_assets": 300.0, "total_liabilities": 150.0, "equity": 145.0},
    })
    rep = D.validate(u, upload=u)
    checks = {(i.severity, i.check) for i in rep.issues}
    assert ("error", "Balance sheet") in checks          # FY23 off by 33%
    assert ("warning", "Year gaps") in checks             # FY22 missing
    assert ("warning", "YoY jump") in checks              # 1500 -> 140 is below 0.1x
    assert not rep.ok_to_score


def test_validation_unit_mismatch_vs_yahoo():
    y = sample_yahoo()
    u = _upload({"FY25": {"revenue": 100000.0, "total_assets": 120000.0, "cfo": 11000.0}})  # 100x
    m = D.merge_sources(y, u)
    rep = D.validate(m, yahoo=y, upload=u)
    assert any(i.check == "Units" and i.severity == "error" for i in rep.issues)


# ------------------------------------------------------------------ depth + demo
def test_data_depth_badges():
    assert D.data_depth(sample_yahoo())["years"] == 3
    assert D.data_depth(D.demo_data())["years"] == 2


def test_demo_data_matches_brief():
    fd = D.demo_data()
    assert fd.get("receivables", "FY26") == 390 and fd.get("cfo", "FY25") == 135
    assert math.isnan(fd.get("total_debt", "FY26"))     # not given in brief -> stays unavailable
    assert set(fd.sources.loc["revenue"]) == {D.SRC_DEMO}
