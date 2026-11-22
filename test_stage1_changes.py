"""Tests for the Stage 1 revisions: name search, no basis option, materiality, Yahoo override, bank stop."""
import pathlib
import re
import sys
import types

import pandas as pd
import pytest

import data as D

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _fd(years: dict, src=D.SRC_UPLOAD) -> D.FinancialData:
    vals, srcs = D.empty_frame(years.keys())
    for fy, items in years.items():
        for k, x in items.items():
            vals.at[k, fy], srcs.at[k, fy] = float(x), src
    return D.FinancialData(vals, srcs, {}, [])


# ------------------------------------------------------------------ search
QUOTES = [
    {"symbol": "MARUTI.BO", "longname": "Maruti Suzuki India Limited", "quoteType": "EQUITY"},
    {"symbol": "MARUTI.NS", "longname": "Maruti Suzuki India Limited", "quoteType": "EQUITY"},
    {"symbol": "MSZ.F", "longname": "Maruti Suzuki India Ltd", "quoteType": "EQUITY"},        # Frankfurt
    {"symbol": "MARUTI-FUT.NS", "shortname": "Maruti future", "quoteType": "FUTURE"},
    {"symbol": "IDEA.NS", "longname": "Vodafone Idea Limited", "quoteType": "EQUITY"},
    {"symbol": "532822.BO", "longname": "Vodafone Idea Ltd", "quoteType": "EQUITY"},
]


def test_search_filters_to_nse_bse_and_prefers_nse():
    out = D.filter_search_quotes(QUOTES)
    assert [r["ticker"] for r in out] == ["MARUTI.NS", "IDEA.NS"]
    assert out[0]["exchange"] == "NSE"


def test_search_keeps_bse_only_listing():
    out = D.filter_search_quotes([{"symbol": "500000.BO", "longname": "Some BSE Only Ltd"}])
    assert out == [{"name": "Some BSE Only Ltd", "ticker": "500000.BO", "exchange": "BSE"}]


def test_search_no_results_and_short_query():
    assert D.filter_search_quotes([]) == []
    with pytest.raises(ValueError):
        D.search_companies("m")


def test_search_failure_raises_search_error(monkeypatch):
    fake = types.ModuleType("yfinance")

    def boom(*a, **k):
        raise ConnectionError("blocked")
    fake.Search = boom
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    with pytest.raises(D.SearchError):
        D.search_companies("maruti")


# ------------------------------------------------------------------ bank stopped
def test_bank_ticker_loads_with_financial_flag(monkeypatch):
    fake = types.ModuleType("yfinance")

    class T:
        def __init__(self, t):
            self.info = {"longName": "HDFC Bank Limited", "sector": "Financial Services",
                         "industry": "Banks - Regional"}
            self.fast_info = types.SimpleNamespace(market_cap=None, last_price=None)
            self.income_stmt = pd.DataFrame({pd.Timestamp("2025-03-31"): [3000e7]}, index=["Total Revenue"])
            self.balance_sheet = pd.DataFrame({pd.Timestamp("2025-03-31"): [40000e7]}, index=["Total Assets"])
            self.cashflow = pd.DataFrame()
    fake.Ticker = T
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    fd = D.fetch_yahoo("HDFCBANK.NS")            # no hard stop any more
    assert fd.meta["is_financial"] and "Financial Services" in fd.meta["financial_reason"]
    assert fd.get("revenue", "FY25") == pytest.approx(3000)


# ------------------------------------------------------------------ basis option removed
def test_no_basis_option_anywhere():
    files = ["app.py", "data.py", "config.py", "ratios.py", "README.md"]
    pat = re.compile(r"\bbasis\b|standalone|upload_basis", re.IGNORECASE)
    hits = [f"{f}:{i}" for f in files for i, line in enumerate((ROOT / f).read_text().splitlines(), 1)
            if pat.search(line)]
    assert not hits, f"basis/standalone references remain: {hits}"
    assert not pat.search(D.make_template_csv().decode())


def test_consolidated_note_in_template_and_validation():
    assert D.CONSOLIDATED_NOTE in D.make_template_csv().decode()
    rep = D.validate(_fd({"FY25": {"revenue": 100, "total_assets": 200}}))
    assert any(i.message == D.CONSOLIDATED_NOTE for i in rep.issues)


def test_csv_template_with_note_line_still_parses():
    raw = D.make_template_csv(n_years=1).decode().replace("revenue,Revenue from operations,P&L,Core,,",
                                                         "revenue,Revenue from operations,P&L,Core,,123")
    res = D.parse_upload(raw.encode(), "t.csv")
    assert not res.errors and res.data.get("revenue", res.data.years[0]) == 123


# ------------------------------------------------------------------ materiality of YoY jumps
def test_immaterial_jump_is_info_not_warning():
    fd = _fd({"FY24": {"total_debt": 5, "revenue": 140000, "total_assets": 150000},
              "FY25": {"total_debt": 80, "revenue": 150000, "total_assets": 160000}})
    rep = D.validate(fd)
    jumps = [i for i in rep.issues if i.check.startswith("YoY jump")]
    assert jumps and all(i.severity == "info" for i in jumps)


def test_material_jump_is_warning():
    fd = _fd({"FY24": {"total_debt": 500, "revenue": 10000, "total_assets": 12000},
              "FY25": {"total_debt": 6000, "revenue": 10500, "total_assets": 17000}})
    rep = D.validate(fd)
    assert any(i.check == "YoY jump" and i.severity == "warning" for i in rep.issues)


# ------------------------------------------------------------------ Yahoo balance override vs upload block
BAD_BS = {"FY25": {"revenue": 1000, "total_assets": 1000, "total_liabilities": 400, "equity": 300}}


def test_yahoo_balance_mismatch_is_overridable():
    rep = D.validate(_fd(BAD_BS, D.SRC_YAHOO))
    assert rep.overridable_errors and not rep.blocking_errors
    assert not rep.can_score() and rep.can_score(proceed_anyway=True)


def test_upload_balance_mismatch_blocks():
    rep = D.validate(_fd(BAD_BS, D.SRC_UPLOAD))
    assert rep.blocking_errors and not rep.can_score(proceed_anyway=True)


# ------------------------------------------------------------------ stressed companies are valid data
def test_negative_equity_ebitda_losses_are_not_validation_errors():
    fd = _fd({
        "FY24": {"revenue": 42000, "ebitda": -500, "pat": -29000, "total_assets": 200000,
                 "total_liabilities": 270000, "equity": -70000, "cfo": 17000},
        "FY25": {"revenue": 43000, "ebitda": -800, "pat": -27000, "total_assets": 190000,
                 "total_liabilities": 280000, "equity": -90000, "cfo": 15000},
    }, D.SRC_YAHOO)
    rep = D.validate(fd)
    assert rep.ok_to_score, rep.to_frame()


def test_lease_liabilities_from_yahoo_components():
    inc = pd.DataFrame({pd.Timestamp("2025-03-31"): [1000e7]}, index=["Total Revenue"])
    bs = pd.DataFrame({pd.Timestamp("2025-03-31"): [2000e7, 30e7, 70e7]},
                      index=["Total Assets", "Current Capital Lease Obligation",
                             "Long Term Capital Lease Obligation"])
    fd = D.build_from_yahoo_frames(inc, bs, None, {}, "X.NS")
    assert fd.get("lease_liabilities", "FY25") == pytest.approx(100)
