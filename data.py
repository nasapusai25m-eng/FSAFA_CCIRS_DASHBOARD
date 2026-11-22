"""
CCIRS - Stage 1 data layer.

Responsibilities
  * Pull annual statements, price and market cap from Yahoo Finance (yfinance).
  * Map Yahoo's varying line-item labels onto one canonical set of rows.
  * Convert to Rs crore and label every period with an Indian fiscal year (Mar-2025 -> FY25).
  * Build / parse the upload template for extra years, and merge it with Yahoo by fiscal year.
  * Tag every number with its source, and validate the merged data before scoring.

This module never imports streamlit, so it can be unit-tested on its own.
Missing values stay NaN ("data unavailable") and are never filled with zero.
"""
from __future__ import annotations

import io
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

import numpy as np
import pandas as pd

import config

# --------------------------------------------------------------------------- sources
SRC_YAHOO = "Yahoo"
SRC_UPLOAD = "User upload"
SRC_DEMO = "Demo (hypothetical)"
SRC_MANUAL = "Manual entry"
DERIVED_SUFFIX = " (derived)"

CONSOLIDATED_NOTE = "All figures are consolidated. Uploaded years must also be consolidated."

# --------------------------------------------------------------------------- canonical rows
# key, display label, statement, template status, note
LINE_ITEMS: list[tuple[str, str, str, str, str]] = [
    ("revenue", "Revenue from operations", "P&L", "Core", ""),
    ("cogs", "Cost of goods sold", "P&L", "Core", "Materials consumed + purchases +/- change in inventory"),
    ("sga", "SG&A expenses", "P&L", "Core", "Employee cost + other expenses (excl. D&A)"),
    ("ebitda", "EBITDA", "P&L", "Core", "Derived as EBIT + D&A if left blank"),
    ("depreciation", "Depreciation & amortisation", "P&L", "Core", ""),
    ("ebit", "EBIT", "P&L", "Core", "Derived as PBT + interest if left blank"),
    ("interest_expense", "Interest expense (finance cost)", "P&L", "Core", "Enter as a positive number"),
    ("pbt", "Profit before tax", "P&L", "Core", ""),
    ("pat", "Profit after tax (net income)", "P&L", "Core", ""),
    ("other_income", "Other income (non-operating)", "P&L", "Core",
     "Interest, dividend, treasury gains etc. Used to strip non-operating income from EBITDA/EBIT"),
    ("associate_share", "Share of profit of associates/JVs", "P&L", "Optional", "Enter losses as negative"),
    ("exceptional_items", "Exceptional / unusual items (gain +, loss -)", "P&L", "Optional",
     "E.g. AGR reassessment gain, impairment, restructuring, profit on sale of a business. Removed from operating "
     "EBITDA and operating EBIT"),
    ("operating_ebitda", "Operating EBITDA (excl. other income, associates/JVs and exceptional items)", "P&L",
     "Derived", "Leave blank: derived as EBITDA - other income - share of associates/JVs - exceptional items"),
    ("operating_ebit", "Operating EBIT (excl. other income, associates/JVs and exceptional items)", "P&L", "Derived",
     "Leave blank: derived as EBIT - other income - share of associates/JVs - exceptional items"),
    ("cash_opex", "Cash operating expenses", "P&L", "Optional", "For DIR; derived as Revenue - EBITDA if blank"),
    ("cash", "Cash & cash equivalents", "Balance sheet", "Core", ""),
    ("marketable_securities", "Current liquid investments (marketable securities)", "Balance sheet", "Core",
     "Current investments: liquid/debt mutual funds, T-bills, bonds due within a year"),
    ("noncurrent_liquid_investments", "Non-current liquid investments", "Balance sheet", "Optional",
     "Non-current debt mutual funds, bonds, G-secs. EXCLUDE strategic equity stakes in subsidiaries/associates"),
    ("receivables", "Trade receivables", "Balance sheet", "Core", ""),
    ("inventory", "Inventory", "Balance sheet", "Core", ""),
    ("current_assets", "Total current assets", "Balance sheet", "Core", ""),
    ("ppe", "Net PP&E", "Balance sheet", "Core", ""),
    ("total_assets", "Total assets", "Balance sheet", "Core", ""),
    ("accounts_payable", "Trade payables", "Balance sheet", "Optional", "Needed for DPO / cash conversion cycle"),
    ("current_liabilities", "Total current liabilities", "Balance sheet", "Core", ""),
    ("current_debt", "Current portion of debt / short-term borrowings", "Balance sheet", "Optional",
     "Needed for DSCR proxy"),
    ("long_term_debt", "Long-term borrowings", "Balance sheet", "Optional", "Needed for Piotroski leverage test"),
    ("total_debt", "Total debt (incl. lease liabilities)", "Balance sheet", "Core",
     "Borrowings + lease liabilities (Yahoo definition). Derived as current + long-term debt if blank"),
    ("lease_liabilities", "Lease liabilities", "Balance sheet", "Optional",
     "Ind AS 116 lease liabilities; lets the app show Debt/Equity excluding leases"),
    ("total_liabilities", "Total liabilities", "Balance sheet", "Core", "Excluding equity and minority interest"),
    ("minority_interest", "Minority (non-controlling) interest", "Balance sheet", "Optional",
     "Used only in the balance check"),
    ("equity", "Shareholders' equity", "Balance sheet", "Core", "Attributable to owners"),
    ("retained_earnings", "Retained earnings (other equity)", "Balance sheet", "Core", ""),
    ("shares_outstanding", "Shares outstanding (crore)", "Balance sheet", "Optional",
     "Number of shares in crore; Piotroski dilution test"),
    ("cfo", "Cash flow from operations (CFO)", "Cash flow", "Core", ""),
    ("capex", "Capital expenditure", "Cash flow", "Core", "Enter as a positive number"),
    ("dividends_paid", "Dividends paid", "Cash flow", "Core", "Enter as a positive number"),
    ("related_party_revenue", "Revenue from related parties", "Notes", "Optional",
     "Sales to related parties (related-party note); flag above 20% of revenue"),
    ("related_party_receivables", "Receivables from related parties", "Notes", "Optional",
     "Trade receivables due from related parties; flag above 25% of receivables"),
    ("related_party_advances", "Related-party loans & advances", "Notes", "Optional",
     "From related-party note; flag above 50% of revenue"),
]
ITEM_KEYS = [r[0] for r in LINE_ITEMS]
ITEM_LABELS = {r[0]: r[1] for r in LINE_ITEMS}

# Yahoo labels in priority order; per year the first non-empty label wins.
# (statement, label) where statement is "inc", "bs" or "cf".
YAHOO_MAP: dict[str, list[tuple[str, str]]] = {
    "revenue": [("inc", "Total Revenue"), ("inc", "Operating Revenue")],
    "cogs": [("inc", "Cost Of Revenue"), ("inc", "Reconciled Cost Of Revenue")],
    "sga": [("inc", "Selling General And Administration"), ("inc", "General And Administrative Expense")],
    "ebitda": [("inc", "EBITDA"), ("inc", "Normalized EBITDA")],
    "depreciation": [("inc", "Reconciled Depreciation"),
                     ("inc", "Depreciation And Amortization In Income Statement"),
                     ("inc", "Depreciation Amortization Depletion Income Statement"),
                     ("cf", "Depreciation And Amortization"),
                     ("cf", "Depreciation Amortization Depletion")],
    "ebit": [("inc", "EBIT"), ("inc", "Operating Income")],
    "interest_expense": [("inc", "Interest Expense"), ("inc", "Interest Expense Non Operating")],
    "pbt": [("inc", "Pretax Income")],
    "associate_share": [("inc", "Earnings From Equity Interest"), ("inc", "Earnings From Equity Interest Net Of Tax")],
    # Yahoo's EBITDA/EBIT include unusual items ("Normalized EBITDA" = EBITDA - Total Unusual Items)
    "exceptional_items": [("inc", "Total Unusual Items"), ("inc", "Total Unusual Items Excluding Goodwill"),
                          ("inc", "Special Income Charges")],
    "pat": [("inc", "Net Income"), ("inc", "Net Income Common Stockholders"),
            ("inc", "Net Income From Continuing Operation Net Minority Interest")],
    "cash": [("bs", "Cash And Cash Equivalents"), ("bs", "Cash Financial")],
    "marketable_securities": [("bs", "Other Short Term Investments"), ("bs", "Short Term Investments")],
    "noncurrent_liquid_investments": [("bs", "Investmentin Financial Assets")],
    "receivables": [("bs", "Accounts Receivable"), ("bs", "Gross Accounts Receivable"), ("bs", "Receivables")],
    "inventory": [("bs", "Inventory")],
    "current_assets": [("bs", "Current Assets")],
    "ppe": [("bs", "Net PPE")],
    "total_assets": [("bs", "Total Assets")],
    "accounts_payable": [("bs", "Accounts Payable"), ("bs", "Payables"), ("bs", "Payables And Accrued Expenses")],
    "current_liabilities": [("bs", "Current Liabilities")],
    "current_debt": [("bs", "Current Debt"), ("bs", "Current Debt And Capital Lease Obligation")],
    "long_term_debt": [("bs", "Long Term Debt"), ("bs", "Long Term Debt And Capital Lease Obligation")],
    "total_debt": [("bs", "Total Debt")],
    "lease_liabilities": [("bs", "Capital Lease Obligations")],
    "total_liabilities": [("bs", "Total Liabilities Net Minority Interest")],
    "minority_interest": [("bs", "Minority Interest")],
    "equity": [("bs", "Stockholders Equity"), ("bs", "Common Stock Equity")],
    "retained_earnings": [("bs", "Retained Earnings")],
    "shares_outstanding": [("bs", "Ordinary Shares Number"), ("bs", "Share Issued")],
    "cfo": [("cf", "Operating Cash Flow"), ("cf", "Cash Flow From Continuing Operating Activities")],
    "capex": [("cf", "Capital Expenditure"), ("cf", "Purchase Of PPE")],
    "dividends_paid": [("cf", "Cash Dividends Paid"), ("cf", "Common Stock Dividend Paid")],
    # related_party_advances: not on Yahoo -> upload only
}
# Fallback when the total label is missing: sum of component labels (all must be present).
YAHOO_SUMS: dict[str, list[tuple[str, str]]] = {
    "lease_liabilities": [("bs", "Current Capital Lease Obligation"), ("bs", "Long Term Capital Lease Obligation")],
}
# Yahoo has no single "other income" line. Sum the non-operating income parts that ARE reported
# (at least one must be present); the parts used are logged per year.
YAHOO_PARTIAL_SUMS: dict[str, list[tuple[str, str]]] = {
    "other_income": [("inc", "Interest Income Non Operating"), ("inc", "Other Non Operating Income Expenses"),
                     ("inc", "Gain On Sale Of Security")],
}
REVENUE_LABELS = ["Total Revenue", "Operating Revenue"]
# Yahoo reports outflows as negatives; we store these as positive amounts.
ABS_KEYS = {"capex", "dividends_paid", "interest_expense"}


class DataFetchError(Exception):
    """Raised when Yahoo returns nothing usable."""


# --------------------------------------------------------------------------- container
@dataclass
class FinancialData:
    values: pd.DataFrame              # index = ITEM_KEYS, columns = FY labels (ascending), Rs crore
    sources: pd.DataFrame             # same shape, source tag per cell ("" where value is NaN)
    meta: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)   # mapping/derivation/merge log

    @property
    def years(self) -> list[str]:
        return list(self.values.columns)

    def get(self, key: str, fy: str) -> float:
        try:
            v = self.values.at[key, fy]
        except KeyError:
            return float("nan")
        return float(v) if pd.notna(v) else float("nan")

    def series(self, key: str) -> pd.Series:
        return self.values.loc[key] if key in self.values.index else pd.Series(dtype=float)

    def display_table(self) -> pd.DataFrame:
        """Values with human labels; NaN stays NaN (the UI renders 'data unavailable').
        Revenue carries the exact Yahoo label used."""
        df = self.values.copy()
        rev_label = self.meta.get("labels_used", {}).get("revenue")
        labels = []
        for k in df.index:
            lab = ITEM_LABELS.get(k, k)
            if k == "revenue" and rev_label:
                lab += f"  [Yahoo: {rev_label}]"
            labels.append(lab)
        df.index = labels
        return df

    def year_sources(self) -> pd.Series:
        """Dominant source per year (for the year-level tag in the UI)."""
        out = {}
        for fy in self.years:
            tags = [base_source(s) for s in self.sources[fy] if s]
            out[fy] = " + ".join(sorted(set(tags))) if tags else "-"
        return pd.Series(out)


def empty_frame(years: Iterable[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    years = sort_fy(list(years))
    vals = pd.DataFrame(np.nan, index=ITEM_KEYS, columns=years, dtype=float)
    srcs = pd.DataFrame("", index=ITEM_KEYS, columns=years, dtype=object)
    return vals, srcs


def base_source(tag: str) -> str:
    return tag.replace(DERIVED_SUFFIX, "")


# --------------------------------------------------------------------------- fiscal years
def fy_year_from_date(d) -> int:
    """Indian FY: Apr(Y-1)-Mar(Y) is FY(Y). Period ending Jan-Mar of Y -> Y; Apr-Dec of Y -> Y+1."""
    ts = pd.Timestamp(d)
    return ts.year if ts.month <= 3 else ts.year + 1


def fy_label_from_year(y: int) -> str:
    return f"FY{y % 100:02d}"


def fy_label(d) -> str:
    return fy_label_from_year(fy_year_from_date(d))


def fy_to_year(label: str) -> int:
    """'FY25' -> 2025 (two-digit years 50-99 are read as 19xx)."""
    m = re.fullmatch(r"FY(\d{2})", str(label).strip().upper())
    if not m:
        raise ValueError(f"Not a FY label: {label!r}")
    yy = int(m.group(1))
    return 1900 + yy if yy >= 50 else 2000 + yy


def sort_fy(labels: list[str]) -> list[str]:
    return sorted(labels, key=fy_to_year)


def _norm_year(s: str) -> int:
    y = int(s)
    if y < 100:
        y = 1900 + y if y >= 50 else 2000 + y
    return y


def _split_year(first: str, second: str) -> int:
    """'2024','25' -> 2025; '1999','00' -> 2000; '2024','2025' -> 2025."""
    a = int(first)
    if len(second) == 4:
        return int(second)
    b = (a // 100) * 100 + int(second)
    return b + 100 if b <= a else b


def parse_fy_header(h) -> int | None:
    """Accepts FY25, FY 2025, FY'25, 2024-25, 2024/2025, Mar-25, March 2025, 2025, or a date."""
    if isinstance(h, (pd.Timestamp, datetime, date)):
        return fy_year_from_date(h)
    s = str(h).strip()
    if not s or s.lower().startswith("unnamed"):
        return None
    s_up = s.upper().replace("’", "'")
    patterns = [
        (r"FY\s*'?\s*(\d{2}|\d{4})", lambda m: _norm_year(m.group(1))),
        (r"(\d{4})\s*[-/]\s*(\d{2}|\d{4})", lambda m: _split_year(m.group(1), m.group(2))),
        (r"MAR(?:CH)?[\s\-'.]*(\d{2}|\d{4})", lambda m: _norm_year(m.group(1))),
        (r"(\d{4})", lambda m: int(m.group(1))),
    ]
    for pat, fn in patterns:
        m = re.fullmatch(pat, s_up)
        if m:
            y = fn(m)
            return y if 1980 <= y <= 2100 else None
    # last resort: a date string such as 2025-03-31
    try:
        return fy_year_from_date(pd.Timestamp(s))
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- numbers
_NA_STRINGS = {"", "-", "--", "na", "n/a", "nan", "none", "null", "data unavailable", "nil"}


def clean_number(x) -> float:
    """'1,234.5' -> 1234.5; '(120)' -> -120; blanks/NA -> NaN. Raises ValueError if not numeric."""
    if x is None:
        return float("nan")
    if isinstance(x, (int, float, np.integer, np.floating)):
        return float(x) if not (isinstance(x, float) and math.isnan(x)) else float("nan")
    s = str(x).strip()
    if s.lower() in _NA_STRINGS:
        return float("nan")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("₹", "").replace("Rs", "").replace(" ", "")
    v = float(s)  # raises ValueError for text
    return -v if neg else v


# --------------------------------------------------------------------------- Yahoo
def normalize_ticker(t: str) -> tuple[str, str | None]:
    t = (t or "").strip().upper()
    if not t:
        raise ValueError("Enter a ticker, e.g. MARUTI.NS")
    if "." not in t:
        return t + ".NS", f"No exchange suffix given; assumed NSE ({t}.NS). Use .BO for BSE."
    return t, None


class SearchError(Exception):
    """Yahoo search failed (network / throttling)."""


def _norm_company(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]", " ", str(name).lower())
    n = re.sub(r"\b(limited|ltd|the|india|inc|co|company|corporation|corp)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def filter_search_quotes(quotes: list[dict]) -> list[dict]:
    """Keep NSE/BSE equities only, one row per company, preferring the NSE listing.
    Returns [{name, ticker, exchange}] in Yahoo's relevance order."""
    rows = []
    for q in quotes or []:
        sym = str(q.get("symbol") or "").upper()
        if not sym.endswith((".NS", ".BO")):
            continue
        if q.get("quoteType") not in (None, "", "EQUITY"):
            continue
        name = q.get("longname") or q.get("shortname") or sym
        rows.append({"name": name, "ticker": sym, "exchange": "NSE" if sym.endswith(".NS") else "BSE",
                     "sector": q.get("sector") or q.get("sectorDisp") or "",
                     "industry": q.get("industry") or q.get("industryDisp") or ""})
    best: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        k = _norm_company(r["name"]) or r["ticker"]
        if k not in best:
            best[k] = r
            order.append(k)
        elif best[k]["exchange"] == "BSE" and r["exchange"] == "NSE":
            best[k] = r
    return [best[k] for k in order]


def _search_tokens(text: str) -> list[str]:
    t = str(text).lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return [w for w in t.split() if w not in config.SEARCH_STOPWORDS]


def query_variants(query: str) -> list[str]:
    """Spellings to try, in order. Yahoo's phrase-match search misses some multi-word Indian names
    (e.g. 'Bajaj Auto' -> BAJAJ-AUTO.NS) and punctuation ('Dr. Reddy's', 'Larsen & Toubro')."""
    q = re.sub(r"\s+", " ", (query or "").strip())
    alias = config.SEARCH_ALIASES.get(q.lower())
    no_punct = re.sub(r"[.'’]", "", q)
    variants = [alias] if alias else []
    variants += [q, no_punct, no_punct.replace("&", " and "), no_punct.replace("&", ""),
                 no_punct.replace(" ", "-"), no_punct.replace(" ", "")]
    out = []
    for v in variants:
        v = re.sub(r"\s+", " ", v or "").strip()
        if v and v.lower() not in [o.lower() for o in out]:
            out.append(v)
    return out


def _token_match(row: dict, tokens: list[str]) -> bool:
    hay = " ".join(_search_tokens(row["name"])) + " " + row["ticker"].lower().replace("-", " ").replace(".", " ")
    hay_joined = hay.replace(" ", "")
    return all(t in hay or t in hay_joined for t in tokens)


def search_companies(query: str, max_results: int | None = None, searcher=None) -> list[dict]:
    """Company-name search via yfinance with fallbacks. Raises ValueError for too-short queries and
    SearchError only if EVERY Yahoo call fails. `searcher(q, fuzzy)` -> raw quotes (injectable for tests)."""
    q = (query or "").strip()
    if len(q) < 2:
        raise ValueError("Type at least 2 characters of the company name.")
    max_results = max_results or config.SEARCH_MAX_RESULTS

    def default_searcher(text: str, fuzzy: bool) -> list:
        import yfinance as yf
        return yf.Search(text, max_results=max_results, news_count=0, lists_count=0,
                         enable_fuzzy_query=fuzzy).quotes or []

    searcher = searcher or default_searcher
    errors, calls = [], 0

    def run(text, fuzzy=False):
        nonlocal calls
        calls += 1
        try:
            return filter_search_quotes(searcher(text, fuzzy))
        except Exception as e:  # network / throttling - try the next strategy
            errors.append(e)
            return []

    variants = query_variants(q)
    for v in variants:                                  # 1. exact spellings
        rows = run(v)
        if rows:
            return rows
    for v in variants[:2]:                              # 2. Yahoo fuzzy matching
        rows = run(v, fuzzy=True)
        if rows:
            return rows
    tokens = _search_tokens(config.SEARCH_ALIASES.get(q.lower(), q))
    if len(tokens) > 1:                                 # 3. first word, then keep names containing every word
        seen, keep = set(), []
        for v in (tokens[0], tokens[0] + " " + tokens[1][:3]):
            for row in run(v):
                if row["ticker"] not in seen and _token_match(row, tokens):
                    seen.add(row["ticker"])
                    keep.append(row)
        if keep:
            return keep
    if errors and len(errors) == calls:
        raise SearchError(f"Yahoo search failed ({type(errors[-1]).__name__}).") from errors[-1]
    return []


def is_financial_company(info: dict) -> tuple[bool, str]:
    sector = str(info.get("sector") or "").lower()
    industry = str(info.get("industry") or "").lower()
    name = " " + str(info.get("longName") or info.get("shortName") or "").lower() + " "
    if sector in config.FINANCIAL_SECTORS:
        return True, f"Yahoo sector is '{info.get('sector')}'"
    for kw in config.FINANCIAL_INDUSTRY_KEYWORDS:
        if kw in industry:
            return True, f"Yahoo industry is '{info.get('industry')}'"
    for kw in config.FINANCIAL_NAME_KEYWORDS:
        if kw in name:
            return True, f"Company name contains '{kw.strip()}'"
    return False, ""


FINANCIAL_BANNER = config.FINANCIAL_BANNER


def looks_like_lender(inc: pd.DataFrame | None, bs: pd.DataFrame | None) -> tuple[bool, str]:
    """Fallback when Yahoo's company profile (sector/industry) fails to load: a lender's statements report
    net interest income and have no current assets / current liabilities split."""
    if inc is None or bs is None or inc.empty or bs.empty:
        return False, ""
    has_nii = "Net Interest Income" in inc.index and inc.loc["Net Interest Income"].notna().any()
    no_current = not any(l in bs.index for l in ("Current Assets", "Current Liabilities"))
    if has_nii and no_current:
        return True, "statement structure looks like a lender's (net interest income; no current assets split)"
    return False, ""


def _frame_by_fy(raw: pd.DataFrame | None, stmt: str, notes: list[str]) -> dict[int, pd.Series]:
    """Turn a yfinance statement (rows=labels, cols=period-end dates) into {FY year: column}."""
    out: dict[int, pd.Series] = {}
    if raw is None or raw.empty:
        return out
    stamps = {}
    for col in raw.columns:
        try:
            ts = pd.Timestamp(col)
        except (ValueError, TypeError):
            continue
        y = fy_year_from_date(ts)
        if ts.month != 3:
            notes.append(f"{stmt}: period ending {ts:%b %Y} is not a March year-end; mapped to "
                         f"{fy_label_from_year(y)}.")
        if y in stamps and stamps[y] > ts:
            notes.append(f"{stmt}: two periods map to {fy_label_from_year(y)}; kept the later one.")
            continue
        stamps[y] = ts
        out[y] = pd.to_numeric(raw[col], errors="coerce")
    return out


def build_from_yahoo_frames(inc: pd.DataFrame | None, bs: pd.DataFrame | None, cf: pd.DataFrame | None,
                            info: dict | None = None, ticker: str = "") -> FinancialData:
    """Pure function (no network): map raw yfinance frames to a FinancialData in Rs crore."""
    info = info or {}
    notes: list[str] = []
    frames = {"inc": _frame_by_fy(inc, "Income statement", notes),
              "bs": _frame_by_fy(bs, "Balance sheet", notes),
              "cf": _frame_by_fy(cf, "Cash flow", notes)}
    all_years = sorted(set().union(*[set(f) for f in frames.values()]))
    if not all_years:
        raise DataFetchError(f"Yahoo returned no annual statements for {ticker or 'this ticker'}.")

    currency = (info.get("financialCurrency") or "INR").upper()
    if currency != "INR":
        notes.append(f"Statements are reported in {currency}, not INR. Figures are shown in crore of "
                     f"{currency}; ratios are unaffected but absolute amounts are not in rupees.")

    vals, srcs = empty_frame(fy_label_from_year(y) for y in all_years)
    labels_used: dict[str, str] = {}
    for key, candidates in YAHOO_MAP.items():
        used = []
        for y in all_years:
            fy = fy_label_from_year(y)
            for stmt, label in candidates:
                col = frames[stmt].get(y)
                if col is None or label not in col.index:
                    continue
                v = col.get(label)
                if isinstance(v, pd.Series):  # duplicated label in raw frame
                    v = v.dropna().iloc[0] if v.notna().any() else np.nan
                if pd.notna(v):
                    v = float(v)
                    if key in ABS_KEYS:
                        v = abs(v)
                    vals.at[key, fy] = v / config.CRORE
                    srcs.at[key, fy] = SRC_YAHOO
                    used.append(label)
                    break
        if used:
            labels_used[key] = ", ".join(dict.fromkeys(used))

    for key, parts in YAHOO_SUMS.items():
        summed = False
        for y in all_years:
            fy = fy_label_from_year(y)
            if pd.notna(vals.at[key, fy]):
                continue
            xs = []
            for stmt, label in parts:
                col = frames[stmt].get(y)
                v = col.get(label) if col is not None and label in col.index else np.nan
                xs.append(float(v) if pd.notna(v) else np.nan)
            if all(pd.notna(x) for x in xs):
                vals.at[key, fy] = sum(xs) / config.CRORE
                srcs.at[key, fy] = SRC_YAHOO
                summed = True
        if summed and key not in labels_used:
            labels_used[key] = " + ".join(label for _, label in parts)

    partial_log: dict[str, dict[str, list[str]]] = {}
    for key, parts in YAHOO_PARTIAL_SUMS.items():
        used_any = set()
        for y in all_years:
            fy = fy_label_from_year(y)
            if pd.notna(vals.at[key, fy]):
                continue
            total, used = 0.0, []
            for stmt, label in parts:
                # Yahoo counts gains on sale of securities inside Total Unusual Items: never subtract it twice
                if label == "Gain On Sale Of Security" and pd.notna(vals.at["exceptional_items", fy]):
                    continue
                col = frames[stmt].get(y)
                v = col.get(label) if col is not None and label in col.index else np.nan
                if pd.notna(v):
                    total += float(v)
                    used.append(label)
            if used:
                vals.at[key, fy] = total / config.CRORE
                srcs.at[key, fy] = SRC_YAHOO
                partial_log.setdefault(key, {})[fy] = used
                used_any.update(used)
        if used_any:
            labels_used[key] = " + ".join(l for _, l in parts if l in used_any) + " (parts reported by Yahoo)"

    # every revenue-type label Yahoo reports, per year (for the revenue-definition check)
    revenue_candidates: dict[str, dict[str, float]] = {}
    for y in all_years:
        col = frames["inc"].get(y)
        if col is None:
            continue
        found = {l: float(col.get(l)) / config.CRORE for l in REVENUE_LABELS
                 if l in col.index and pd.notna(col.get(l))}
        if found:
            revenue_candidates[fy_label_from_year(y)] = found

    # Drop stub years where Yahoo has neither revenue nor total assets.
    empty = [fy for fy in vals.columns
             if all(pd.isna(vals.at[k, fy]) for k in config.DEPTH_KEY_ITEMS)]
    if empty:
        notes.append(f"Dropped Yahoo period(s) with no revenue and no total assets: {', '.join(empty)}.")
        vals, srcs = vals.drop(columns=empty), srcs.drop(columns=empty)

    period_ends = []
    for f in frames.values():
        period_ends += [y for y in f]
    meta = {
        "ticker": ticker,
        "name": info.get("longName") or info.get("shortName") or ticker,
        "sector": info.get("sector") or "Unknown",
        "industry": info.get("industry") or "Unknown",
        "currency": currency,
        "unit_label": f"{'Rs' if currency == 'INR' else currency} crore",
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "market_cap_cr": (info["marketCap"] / config.CRORE) if info.get("marketCap") else None,
        "labels_used": labels_used,
        "partial_sums": partial_log,
        "revenue_candidates": revenue_candidates,
        "latest_fy": vals.columns[-1] if len(vals.columns) else None,
    }
    is_fin, reason = is_financial_company(info)
    lender = looks_like_lender(inc, bs)
    if not is_fin:
        is_fin, reason = lender
    meta.update(is_financial=is_fin, financial_reason=reason, lender_by_statements=lender,
                yahoo_profile={k: info.get(src) for k, src in (("name", "longName"), ("sector", "sector"),
                                                                ("industry", "industry")) if info.get(src)})
    if not info.get("longName") and info.get("shortName"):
        meta["yahoo_profile"]["name"] = info["shortName"]
    meta["profile_source"] = {k: "yahoo" for k in meta["yahoo_profile"]}
    fd = FinancialData(vals, srcs, meta, notes)
    return fd


# --------------------------------------------------------------------------- company profile
_PROFILE_CACHE: dict[str, tuple[float, dict]] = {}


def _profile_ok(info: dict) -> bool:
    return bool(info) and bool(info.get("sector") or info.get("industry"))


def get_profile(ticker: str, retries: int | None = None, delay: float | None = None, ticker_factory=None,
                sleep=time.sleep, now=time.time) -> dict:
    """Yahoo company profile (tk.info). Retries once after a short delay; a successful profile is cached for
    config.PROFILE_CACHE_SECONDS. Returns {} when the profile is still missing (that result is not cached)."""
    retries = config.PROFILE_RETRIES if retries is None else retries
    delay = config.PROFILE_RETRY_DELAY if delay is None else delay
    hit = _PROFILE_CACHE.get(ticker)
    if hit and now() - hit[0] < config.PROFILE_CACHE_SECONDS:
        return dict(hit[1])
    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker
    for attempt in range(retries + 1):
        try:
            info = dict(ticker_factory(ticker).info or {})
        except Exception:      # throttling / blocked profile endpoint
            info = {}
        if _profile_ok(info):
            _PROFILE_CACHE[ticker] = (now(), info)
            return dict(info)
        if attempt < retries:
            sleep(delay)
    return {}


def builtin_profile(ticker: str) -> dict:
    base = str(ticker or "").upper().split(".")[0]
    row = config.KNOWN_PROFILES.get(base)
    return {} if row is None else {"name": row[0], "sector": row[1], "industry": row[2]}


def apply_profile(fd: FinancialData, profile: dict | None = None, hint: dict | None = None) -> FinancialData:
    """Fill name, sector and industry: Yahoo profile -> search result picked -> built-in table -> 'Unknown'.
    Recomputes the financial-company check from the resolved profile (statement-based lender test as before).
    `profile` is a raw Yahoo info dict (e.g. from a retried get_profile); `hint` is the picked search row."""
    meta = dict(fd.meta)
    yp = dict(meta.get("yahoo_profile") or {})
    if profile:
        yp.update({k: profile.get(src) for k, src in (("name", "longName"), ("sector", "sector"),
                                                       ("industry", "industry")) if profile.get(src)})
        if "name" not in yp and profile.get("shortName"):
            yp["name"] = profile["shortName"]
    hint = {k: (hint or {}).get(k) for k in ("name", "sector", "industry") if (hint or {}).get(k)}
    table = builtin_profile(meta.get("ticker"))
    resolved, source = {}, {}
    for k in ("name", "sector", "industry"):
        for tag, src in (("yahoo", yp), ("search", hint), ("table", table)):
            if src.get(k):
                resolved[k], source[k] = src[k], tag
                break
        else:
            resolved[k] = meta.get("ticker") if k == "name" else "Unknown"
            source[k] = "unknown"
    if profile:
        for k in ("marketCap", "currentPrice"):
            if profile.get(k) and not meta.get({"marketCap": "market_cap_cr", "currentPrice": "price"}[k]):
                if k == "marketCap":
                    meta["market_cap_cr"] = profile[k] / config.CRORE
                else:
                    meta["price"] = profile[k]
    meta.update(resolved, yahoo_profile=yp, profile_source=source)
    is_fin, reason = is_financial_company({"longName": resolved["name"], "sector": resolved["sector"],
                                           "industry": resolved["industry"]})
    if is_fin and source["sector"] != "yahoo" and source["industry"] != "yahoo":
        reason += f" ({config.PROFILE_SOURCE_LABELS[source['industry']]})"
    if not is_fin:
        is_fin, reason = meta.get("lender_by_statements") or (meta.get("is_financial", False),
                                                             meta.get("financial_reason", ""))
    meta.update(is_financial=is_fin, financial_reason=reason)
    notes = [n for n in fd.notes if not n.startswith("Company profile:")]
    used = sorted({config.PROFILE_SOURCE_LABELS[s] for s in source.values() if s != "yahoo"})
    if used:
        notes.insert(0, "Company profile: Yahoo profile unavailable for " + ", ".join(
            k for k in ("name", "sector", "industry") if source[k] != "yahoo") + "; used " + " / ".join(used) + ".")
    return FinancialData(fd.values, fd.sources, meta, notes)


def apply_industry_type(fd: FinancialData, choice: str | None) -> FinancialData:
    """Analyst's 'Industry type' when the industry is Unknown: sets the Altman variant and the
    financial-company check. 'Not set' (or None) leaves the data unchanged."""
    if not choice or choice == config.INDUSTRY_TYPES[0]:
        return fd
    meta = dict(fd.meta)
    is_fin = choice == "Bank / NBFC / insurer"
    meta.update(industry_type=choice, altman_override=config.INDUSTRY_TYPE_ALTMAN[choice], is_financial=is_fin,
                financial_reason="industry type chosen by the analyst" if is_fin else "")
    return FinancialData(fd.values, fd.sources, meta, fd.notes)


def industry_unknown(meta: dict) -> bool:
    return str(meta.get("industry") or "Unknown") == "Unknown" and not meta.get("is_demo")


def industry_display(meta: dict) -> str:
    ind = str(meta.get("industry") or "Unknown")
    return f"{ind} (industry type: {meta['industry_type']}, chosen by you)" if meta.get("industry_type") else ind


def fetch_yahoo(ticker: str) -> FinancialData:
    """Network call. Flags banks/NBFCs/insurers in meta (no hard stop). Raises DataFetchError if nothing usable."""
    import yfinance as yf  # imported lazily so tests run without network

    ticker, note = normalize_ticker(ticker)
    tk = yf.Ticker(ticker)

    info: dict = get_profile(ticker, ticker_factory=lambda _t: tk)     # retried once, cached 24 h when found
    try:  # fast_info is more reliable for price / market cap
        fi = tk.fast_info
        info.setdefault("marketCap", getattr(fi, "market_cap", None))
        info.setdefault("currentPrice", getattr(fi, "last_price", None))
    except Exception:
        pass

    def _get(*attrs):
        for a in attrs:
            try:
                df = getattr(tk, a)
                if df is not None and not df.empty:
                    return df
            except Exception:
                continue
        return None

    inc = _get("income_stmt", "financials")
    bs = _get("balance_sheet", "balancesheet")
    cf = _get("cashflow", "cash_flow")
    if inc is None and bs is None and cf is None:
        raise DataFetchError(f"Yahoo returned no annual statements for {ticker}. Check the ticker "
                             f"(NSE ends in .NS, BSE in .BO) or use Demo mode / an upload.")
    fd = build_from_yahoo_frames(inc, bs, cf, info, ticker)
    if note:
        fd.notes.insert(0, note)
    fd.meta["fetched_at"] = datetime.now().strftime("%d %b %Y %H:%M")
    return apply_profile(fd)                                          # search-result / table fallbacks


# --------------------------------------------------------------------------- template
def template_dataframe(n_years: int = 5, last_fy: int | None = None) -> pd.DataFrame:
    last_fy = last_fy or fy_year_from_date(pd.Timestamp.today()) - 1
    years = [fy_label_from_year(y) for y in range(last_fy - n_years + 1, last_fy + 1)]
    rows = [{"Key": k, "Line item": lbl, "Statement": st, "Status": status, "Notes": note,
             **{fy: None for fy in years}} for k, lbl, st, status, note in LINE_ITEMS]
    return pd.DataFrame(rows)


def make_template_csv(n_years: int = 5) -> bytes:
    header = f"# {CONSOLIDATED_NOTE} Amounts in Rs crore unless another unit is selected in the app.\n"
    return (header + template_dataframe(n_years).to_csv(index=False)).encode("utf-8")


def make_template_xlsx(n_years: int = 5) -> bytes:
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    df = template_dataframe(n_years)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Data", index=False)
        instr = pd.DataFrame({"Instructions": [
            "CCIRS upload template - one column per Indian fiscal year (FY25 = year ending March 2025).",
            "Enter all amounts in Rs crore (or pick the unit you used in the app before uploading).",
            "Do not edit the 'Key' column - it is how the app recognises each row.",
            "Leave a cell blank if the figure is not available. Never type 0 for 'unknown'.",
            "Capex, dividends paid and interest expense: enter as positive numbers.",
            "Total debt must INCLUDE lease liabilities; enter lease liabilities separately as well if known.",
            "Negative equity, negative EBITDA and losses are valid - enter them with a minus sign.",
            CONSOLIDATED_NOTE,
            "Years must be consecutive (no gaps). You may add or remove year columns.",
            "Accepted year headers: FY25, FY 2025, 2024-25, Mar-25, March 2025.",
            "Core rows feed the CCIRS score; Optional rows unlock extra checks (CCC, DSCR, Piotroski, "
            "related-party flag).",
        ]})
        instr.to_excel(xw, sheet_name="Instructions", index=False)

        ws = xw.sheets["Data"]
        head_fill = PatternFill("solid", fgColor="1F3864")
        input_fill = PatternFill("solid", fgColor="FFF2CC")
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
            c.alignment = Alignment(horizontal="center")
        widths = {"A": 24, "B": 44, "C": 14, "D": 10, "E": 52}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w
        first_year_col = 6
        for j in range(first_year_col, first_year_col + n_years):
            ws.column_dimensions[get_column_letter(j)].width = 12
            for i in range(2, len(df) + 2):
                ws.cell(row=i, column=j).fill = input_fill
                ws.cell(row=i, column=j).number_format = "#,##0.00"
        ws.freeze_panes = "F2"
        xw.sheets["Instructions"].column_dimensions["A"].width = 110
    return buf.getvalue()


# --------------------------------------------------------------------------- upload parsing
def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


_LABEL_LOOKUP = {}
for _k, _lbl, *_ in LINE_ITEMS:
    _LABEL_LOOKUP[_norm_label(_k)] = _k
    _LABEL_LOOKUP[_norm_label(_lbl)] = _k
# a few common aliases from annual reports
for _alias, _k in {
    "sales": "revenue", "netsales": "revenue", "totalincomefromoperations": "revenue",
    "costofrevenue": "cogs", "costofmaterialsconsumed": "cogs", "depreciation": "depreciation",
    "financecost": "interest_expense", "financecosts": "interest_expense", "interest": "interest_expense",
    "netprofit": "pat", "netincome": "pat", "profitaftertax": "pat",
    "cashandcashequivalents": "cash", "currentinvestments": "marketable_securities",
    "tradereceivables": "receivables", "debtors": "receivables", "inventories": "inventory",
    "tradepayables": "accounts_payable", "creditors": "accounts_payable", "borrowings": "total_debt",
    "networth": "equity", "otherequity": "retained_earnings", "netcashfromoperatingactivities": "cfo",
    "operatingcashflow": "cfo", "purchaseofppe": "capex", "relatedpartyadvances": "related_party_advances",
}.items():
    _LABEL_LOOKUP[_alias] = _k


@dataclass
class UploadResult:
    data: FinancialData | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_upload(file_bytes: bytes, filename: str, unit: str = config.DEFAULT_UPLOAD_UNIT) -> UploadResult:
    res = UploadResult(None)
    name = filename.lower()
    try:
        if name.endswith((".xlsx", ".xlsm", ".xls")):
            with pd.ExcelFile(io.BytesIO(file_bytes)) as xl:
                sheet = "Data" if "Data" in xl.sheet_names else xl.sheet_names[0]
            raw = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet, header=0)
        elif name.endswith(".csv"):
            raw = pd.read_csv(io.BytesIO(file_bytes), comment="#")
        else:
            res.errors.append("Upload must be .xlsx or .csv.")
            return res
    except Exception as e:
        res.errors.append(f"Could not read the file: {e}")
        return res

    divisor = config.UPLOAD_UNITS.get(unit)
    if divisor is None:
        res.errors.append(f"Unknown unit '{unit}'.")
        return res

    # identify row-key column
    key_col = next((c for c in raw.columns if _norm_label(c) == "key"), None)
    label_col = next((c for c in raw.columns if _norm_label(c) in ("lineitem", "item", "particulars")), None)
    if key_col is None and label_col is None:
        key_col = raw.columns[0]

    # identify year columns
    year_cols: dict[str, int] = {}
    for c in raw.columns:
        if c in (key_col, label_col):
            continue
        y = parse_fy_header(c)
        if y is not None:
            fy = fy_label_from_year(y)
            if y in year_cols.values():
                res.errors.append(f"Two columns map to {fy} (column '{c}'). Keep one column per year.")
                continue
            year_cols[c] = y
    if not year_cols:
        res.errors.append("No fiscal-year columns found. Use headers like FY25 or 2024-25.")
        return res

    fys = [fy_label_from_year(y) for y in year_cols.values()]
    vals, srcs = empty_frame(fys)
    unknown, bad_cells, seen = [], [], set()
    for _, row in raw.iterrows():
        raw_key = row.get(key_col) if key_col is not None else None
        k = _LABEL_LOOKUP.get(_norm_label(raw_key)) if pd.notna(raw_key) else None
        if k is None and label_col is not None and pd.notna(row.get(label_col)):
            k = _LABEL_LOOKUP.get(_norm_label(row.get(label_col)))
        if k is None:
            shown = raw_key if pd.notna(raw_key) else row.get(label_col)
            if pd.notna(shown) and str(shown).strip():
                unknown.append(str(shown))
            continue
        if k in seen:
            res.warnings.append(f"Row '{ITEM_LABELS[k]}' appears twice; the first occurrence was used.")
            continue
        seen.add(k)
        for c, y in year_cols.items():
            fy = fy_label_from_year(y)
            try:
                v = clean_number(row[c])
            except ValueError:
                bad_cells.append(f"{ITEM_LABELS[k]} / {fy}: '{row[c]}'")
                continue
            if pd.notna(v):
                if k in ABS_KEYS and v < 0:
                    v = abs(v)
                vals.at[k, fy] = v / divisor
                srcs.at[k, fy] = SRC_UPLOAD

    if unknown:
        res.warnings.append("Rows not recognised and ignored: " + ", ".join(unknown[:15])
                            + (" ..." if len(unknown) > 15 else ""))
    if bad_cells:
        res.errors.append("Non-numeric cells (fix and re-upload): " + "; ".join(bad_cells[:15])
                          + (" ..." if len(bad_cells) > 15 else ""))
    # drop fully empty year columns (e.g. untouched template columns)
    blank = [fy for fy in vals.columns if vals[fy].isna().all()]
    if blank:
        vals, srcs = vals.drop(columns=blank), srcs.drop(columns=blank)
        res.warnings.append(f"Ignored empty year column(s): {', '.join(blank)}.")
    if vals.empty or vals.shape[1] == 0:
        res.errors.append("The upload contains no numbers.")
        return res

    meta = {"unit_entered": unit, "unit_label": "Rs crore", "filename": filename}
    res.data = FinancialData(vals, srcs, meta, [f"Upload '{filename}' read in {unit}, converted to Rs crore."])
    return res


# --------------------------------------------------------------------------- merge + derivations
def merge_sources(yahoo: FinancialData | None, upload: FinancialData | None,
                  prefer: str = "upload") -> FinancialData:
    """Merge by fiscal year. prefer='upload' (default) or 'yahoo' decides overlapping cells.
    A cell present in only one source is always taken from that source."""
    if yahoo is None and upload is None:
        raise ValueError("Nothing to merge.")
    if upload is None:
        return _copy(yahoo)
    if yahoo is None:
        return _copy(upload)

    years = sort_fy(list(set(yahoo.years) | set(upload.years)))
    vals, srcs = empty_frame(years)
    primary, secondary = (upload, yahoo) if prefer == "upload" else (yahoo, upload)
    conflicts = 0
    for fy in years:
        for k in ITEM_KEYS:
            pv, sv = primary.get(k, fy), secondary.get(k, fy)
            if pd.notna(pv):
                vals.at[k, fy], srcs.at[k, fy] = pv, primary.sources.at[k, fy]
                if pd.notna(sv) and not math.isclose(pv, sv, rel_tol=1e-3, abs_tol=0.01):
                    conflicts += 1
            elif pd.notna(sv):
                vals.at[k, fy], srcs.at[k, fy] = sv, secondary.sources.at[k, fy]

    meta = dict(yahoo.meta)
    meta["merge_preference"] = "User upload" if prefer == "upload" else "Yahoo"
    meta["latest_fy"] = years[-1]
    overlap = sorted(set(yahoo.years) & set(upload.years), key=fy_to_year)
    notes = yahoo.notes + upload.notes + [
        f"Merged Yahoo ({', '.join(yahoo.years)}) with upload ({', '.join(upload.years)}). "
        f"Overlapping years: {', '.join(overlap) or 'none'}. {conflicts} overlapping cell(s) differed; "
        f"{meta['merge_preference']} figures were used for those."]
    return FinancialData(vals, srcs, meta, notes)


def _copy(fd: FinancialData) -> FinancialData:
    return FinancialData(fd.values.copy(), fd.sources.copy(), dict(fd.meta), list(fd.notes))


def _derive(fd: FinancialData, target: str, inputs: list[str], fn, desc: str):
    for fy in fd.years:
        if pd.notna(fd.values.at[target, fy]):
            continue
        xs = [fd.values.at[i, fy] for i in inputs]
        if all(pd.notna(x) for x in xs):
            fd.values.at[target, fy] = fn(*xs)
            src = base_source(fd.sources.at[inputs[0], fy])
            fd.sources.at[target, fy] = src + DERIVED_SUFFIX
            fd.notes.append(f"{fy}: {ITEM_LABELS[target]} derived as {desc}.")


def _derive_operating(fd: FinancialData, target: str, base: str, exclude_exceptional: bool = True):
    """Operating figure = reported figure - other income - share of associates/JVs - exceptional items, using
    whichever adjustments are reported. Needs at least one adjustment; logs what was and was not applied."""
    log = fd.meta.setdefault("op_adjustments", {})
    for fy in fd.years:
        if pd.notna(fd.values.at[target, fy]):
            log.setdefault(fy, {})[target] = "supplied"
            continue
        v = fd.values.at[base, fy]
        if pd.isna(v):
            continue
        applied, missing = [], []
        adjustments = [("other_income", "other income"), ("associate_share", "share of associates/JVs")]
        if exclude_exceptional:
            adjustments.append(("exceptional_items", "exceptional items"))
        for item, txt in adjustments:
            x = fd.values.at[item, fy]
            if pd.notna(x):
                v -= x
                applied.append(f"{txt} {x:,.1f}")
            else:
                missing.append(txt)
        if not applied:
            continue
        fd.values.at[target, fy] = v
        fd.sources.at[target, fy] = base_source(fd.sources.at[base, fy]) + DERIVED_SUFFIX
        text = f"{ITEM_LABELS[base]} less " + ", ".join(applied)
        if missing:
            text += f" ({', '.join(missing)} not reported - not adjusted)"
        log.setdefault(fy, {})[target] = text
        fd.notes.append(f"{fy}: {ITEM_LABELS[target]} = {text}.")


def apply_derivations(fd: FinancialData, exclude_exceptional: bool | None = None) -> FinancialData:
    """Fill a missing row ONLY from an accounting identity, and tag it '(derived)'.
    exclude_exceptional=False reproduces the pre-fix operating figures (for before/after comparisons)."""
    ex = config.EXCLUDE_EXCEPTIONAL if exclude_exceptional is None else exclude_exceptional
    fd = _copy(fd)
    fd.meta = dict(fd.meta)
    fd.meta["op_adjustments"] = {}
    fd.meta["exceptional_excluded"] = ex
    _derive(fd, "ebit", ["pbt", "interest_expense"], lambda a, b: a + b, "PBT + interest")
    _derive(fd, "ebitda", ["ebit", "depreciation"], lambda a, b: a + b, "EBIT + D&A")
    _derive_operating(fd, "operating_ebitda", "ebitda", ex)
    _derive_operating(fd, "operating_ebit", "ebit", ex)
    _derive_total_debt(fd)
    # cash opex should exclude non-operating income -> prefer operating EBITDA
    _derive(fd, "cash_opex", ["revenue", "operating_ebitda"], lambda a, b: a - b,
            "Revenue - operating EBITDA (proxy for cash operating costs)")
    _derive(fd, "cash_opex", ["revenue", "ebitda"], lambda a, b: a - b,
            "Revenue - reported EBITDA (proxy for cash operating costs; understates costs by other income)")
    _zero_minority_if_reconciles(fd)
    return fd


LEASE_INCLUSIVE_LABELS = ("Current Debt And Capital Lease Obligation", "Long Term Debt And Capital Lease Obligation")


def _derive_total_debt(fd: FinancialData):
    """Total debt must INCLUDE lease liabilities (operating EBITDA is post-Ind AS 116, so net debt must carry
    leases too). When it is rebuilt from components, add lease liabilities unless the component labels used
    already include leases."""
    used = fd.meta.get("labels_used", {})
    comp_labels = " ".join(used.get(k, "") for k in ("current_debt", "long_term_debt"))
    components_include_leases = any(l in comp_labels for l in LEASE_INCLUSIVE_LABELS)
    for fy in fd.years:
        if pd.notna(fd.values.at["total_debt", fy]):
            continue
        cd, ltd = fd.values.at["current_debt", fy], fd.values.at["long_term_debt", fy]
        if pd.isna(cd) or pd.isna(ltd):
            continue
        lease = fd.values.at["lease_liabilities", fy]
        total, desc = cd + ltd, "current debt + long-term debt"
        if components_include_leases:
            desc += " (Yahoo component labels already include lease liabilities)"
        elif pd.notna(lease):
            total += lease
            desc += " + lease liabilities"
        else:
            desc += " (lease liabilities not reported - total may exclude leases)"
        fd.values.at["total_debt", fy] = total
        fd.sources.at["total_debt", fy] = base_source(fd.sources.at["current_debt", fy]) + DERIVED_SUFFIX
        fd.notes.append(f"{fy}: Total debt (incl. lease liabilities) derived as {desc}.")


def _zero_minority_if_reconciles(fd: FinancialData):
    """Blank minority interest -> 0 when total assets = liabilities + equity within tolerance."""
    for fy in fd.years:
        if pd.notna(fd.values.at["minority_interest", fy]):
            continue
        ta, tl, eq = (fd.values.at[k, fy] for k in ("total_assets", "total_liabilities", "equity"))
        if any(pd.isna(x) for x in (ta, tl, eq)) or ta == 0:
            continue
        if abs(ta - tl - eq) / abs(ta) <= config.BALANCE_TOLERANCE_WARN:
            fd.values.at["minority_interest", fy] = 0.0
            fd.sources.at["minority_interest", fy] = base_source(fd.sources.at["total_assets", fy]) + DERIVED_SUFFIX
            fd.notes.append(f"{fy}: minority interest blank; set to 0 because liabilities + equity already "
                            f"reconcile to total assets.")


def apply_manual(fd: FinancialData, entries: dict[tuple[str, str], float]) -> FinancialData:
    """Overlay analyst-entered values {(item, fy): value} tagged 'Manual entry'. NaN entries are ignored."""
    out = _copy(fd)
    for (k, fy), v in entries.items():
        if fy in out.years and k in out.values.index and v is not None and pd.notna(v):
            out.values.at[k, fy] = float(v)
            out.sources.at[k, fy] = SRC_MANUAL
    return out


# --------------------------------------------------------------------------- validation
@dataclass
class Issue:
    severity: str        # "error" | "warning" | "info"
    check: str
    year: str
    message: str
    overridable: bool = False   # True: the analyst may proceed with a warning (Yahoo balance mismatch)
    source: str = "data"        # "Yahoo data" | "uploaded data" | "data": used in UI wording


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    def add(self, severity, check, year, message, overridable=False, source="data"):
        self.issues.append(Issue(severity, check, year, message, overridable, source))

    @staticmethod
    def describe_sources(issues) -> str:
        """'Yahoo data', 'uploaded data' or 'Yahoo data and uploaded data' for UI messages."""
        srcs = sorted({i.source for i in issues if i.source and i.source != "data"}) or ["the data"]
        return " and ".join(srcs)

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def blocking_errors(self):
        return [i for i in self.errors if not i.overridable]

    @property
    def overridable_errors(self):
        return [i for i in self.errors if i.overridable]

    @property
    def ok_to_score(self) -> bool:
        """Strict: no errors at all."""
        return not self.errors

    def can_score(self, proceed_anyway: bool = False) -> bool:
        """Scoring allowed if there are no errors, or only overridable ones and the analyst chose to proceed."""
        return not self.blocking_errors and (proceed_anyway or not self.overridable_errors)

    def to_frame(self) -> pd.DataFrame:
        order = {"error": 0, "warning": 1, "info": 2}
        rows = sorted(self.issues, key=lambda i: (order[i.severity], i.check, i.year))
        return pd.DataFrame([vars(i) for i in rows],
                            columns=["severity", "check", "year", "message", "overridable", "source"])


def _cells_source(fd: FinancialData, fy: str, keys) -> str:
    """Wording for where a set of cells came from."""
    tags = {base_source(fd.sources.at[k, fy]) for k in keys if fd.sources.at[k, fy]}
    if SRC_UPLOAD in tags or SRC_MANUAL in tags:
        return "uploaded data"
    if SRC_YAHOO in tags:
        return "Yahoo data"
    if SRC_DEMO in tags:
        return "demo data"
    return "data"


def _is_material(fd: FinancialData, amount: float, fy: str) -> tuple[bool, str]:
    """Material if the amount exceeds MATERIALITY_PCT of total assets OR of revenue for that year.
    If neither base is available, treat as material (cannot prove otherwise)."""
    bases = {k: fd.values.at[k, fy] for k in ("total_assets", "revenue")}
    bases = {k: abs(x) for k, x in bases.items() if pd.notna(x) and x != 0}
    if not bases:
        return True, "no total assets or revenue to judge materiality"
    pct = config.MATERIALITY_PCT
    smallest_key = min(bases, key=bases.get)
    threshold = bases[smallest_key] * pct
    txt = f"{amount:,.1f} vs {pct:.0%} of {ITEM_LABELS[smallest_key].lower()} = {threshold:,.1f}"
    return amount > threshold, txt


def one_off_checks(fd: FinancialData) -> list[tuple[str, str]]:
    """Data-quality screen run AFTER the exceptional-items adjustment."""
    out = []
    years = fd.years
    for i, fy in enumerate(years):
        rev = fd.get("revenue", fy)
        e = fd.get("operating_ebitda", fy)
        e = fd.get("ebitda", fy) if math.isnan(e) else e
        b = fd.get("operating_ebit", fy)
        b = fd.get("ebit", fy) if math.isnan(b) else b
        if not math.isnan(e) and not math.isnan(rev) and rev > 0 and e > rev:
            out.append((fy, f"{fy}: operating EBITDA (Rs {e:,.0f} cr) exceeds revenue (Rs {rev:,.0f} cr). "
                             + config.ONE_OFF_TEXT))
        if not math.isnan(b) and not math.isnan(rev) and rev > 0 and b / rev > config.ONE_OFF_EBIT_MARGIN:
            out.append((fy, f"{fy}: operating EBIT margin is {b / rev:.0%} (above {config.ONE_OFF_EBIT_MARGIN:.0%}). "
                             + config.ONE_OFF_TEXT))
        if i:
            p = years[i - 1]
            if fy_to_year(fy) - fy_to_year(p) == 1:
                ep = fd.get("operating_ebitda", p)
                ep = fd.get("ebitda", p) if math.isnan(ep) else ep
                rp = fd.get("revenue", p)
                if all(not math.isnan(x) for x in (e, ep, rev, rp)) and ep > 0 and rp > 0:
                    de, dr = e / ep - 1, rev / rp - 1
                    if abs(de) > config.ONE_OFF_EBITDA_SWING and abs(dr) < config.ONE_OFF_REVENUE_STABLE:
                        out.append((fy, f"{p}->{fy}: operating EBITDA moved {de:+.0%} while revenue moved {dr:+.0%}. "
                                         + config.ONE_OFF_TEXT))
    return out


def cash_one_off_checks(fd: FinancialData) -> list[tuple[str, str]]:
    """CCE above 150%, CFO more than 2x operating EBITDA, or positive CFO with operating EBITDA <= 0."""
    out = []
    for fy in fd.years:
        cfo = fd.get("cfo", fy)
        e = fd.get("operating_ebitda", fy)
        e = fd.get("ebitda", fy) if math.isnan(e) else e
        if math.isnan(cfo) or math.isnan(e):
            continue
        if e > 0 and cfo / e > config.CASH_ONE_OFF_CCE:
            out.append((fy, f"{fy}: CFO (Rs {cfo:,.0f} cr) is {cfo / e:.0%} of operating EBITDA (Rs {e:,.0f} cr)"
                            + (f", more than {config.CASH_ONE_OFF_MULTIPLE:g}x" if cfo > config.CASH_ONE_OFF_MULTIPLE * e
                               else "") + ". " + config.CASH_ONE_OFF_TEXT))
        elif e <= 0 < cfo:
            out.append((fy, f"{fy}: CFO is positive (Rs {cfo:,.0f} cr) while operating EBITDA is Rs {e:,.0f} cr. "
                            + config.CASH_ONE_OFF_TEXT))
    return out


def revenue_label_check(fd: FinancialData, upload: "FinancialData | None" = None) -> list[tuple[str, str]]:
    """Flag when the revenue figure looks narrower than total revenue from operations."""
    out = []
    used = fd.meta.get("labels_used", {}).get("revenue")
    if used and used != "Total Revenue":
        out.append(("all", f"Revenue taken from Yahoo label '{used}', not 'Total Revenue' - it may exclude other "
                           f"operating revenue (scrap sales, export incentives). Compare with 'Revenue from "
                           f"operations' in the annual report."))
    for fy, cands in (fd.meta.get("revenue_candidates") or {}).items():
        if fy not in fd.years or len(cands) < 2:
            continue
        cur = fd.values.at["revenue", fy]
        top = max(cands.values())
        if pd.notna(cur) and top > 0 and cur < top * 0.995:
            other = max(cands, key=cands.get)
            out.append((fy, f"Revenue used ({cur:,.1f}) is {1 - cur / top:.1%} below Yahoo '{other}' "
                            f"({top:,.1f}) - looks narrower than total revenue from operations."))
    if upload is not None:
        for fy in upload.years:
            u = upload.get("revenue", fy)
            ysrc = fd.meta.get("revenue_candidates", {}).get(fy, {})
            yv = ysrc.get("Total Revenue") or (max(ysrc.values()) if ysrc else None)
            if yv and pd.notna(u) and u > 0 and yv < u * 0.99:
                out.append((fy, f"Yahoo revenue ({yv:,.1f}) is {1 - yv / u:.1%} below the uploaded revenue from "
                                f"operations ({u:,.1f}) - Yahoo label looks narrower."))
    return out


def validate(fd: FinancialData, yahoo: FinancialData | None = None,
             upload: FinancialData | None = None) -> ValidationReport:
    rep = ValidationReport()
    v = fd.values

    # 1. Balance sheet balances
    for fy in fd.years:
        ta, tl, eq = v.at["total_assets", fy], v.at["total_liabilities", fy], v.at["equity", fy]
        mi = v.at["minority_interest", fy]
        if any(pd.isna(x) for x in (ta, tl, eq)) or ta == 0:
            rep.add("info", "Balance sheet", fy, "Balance check skipped - total assets, total liabilities "
                    "or equity is data unavailable.")
            continue
        rhs = tl + eq + (mi if pd.notna(mi) else 0.0)
        gap = abs(ta - rhs) / abs(ta)
        mi_note = "" if pd.notna(mi) else " (minority interest not available, treated as absent)"
        msg = (f"Total assets {ta:,.1f} vs liabilities + equity {rhs:,.1f}: off by {gap:.1%}{mi_note}.")
        src = _cells_source(fd, fy, ("total_assets", "total_liabilities", "equity"))
        if gap > config.BALANCE_TOLERANCE_ERROR:
            if fd.meta.get("is_financial"):
                rep.add("error", "Balance sheet", fy, msg + " Financial company: bank/NBFC/insurer statements on "
                        "Yahoo do not reconcile like industrial ones, so you may proceed anyway with a warning.",
                        overridable=True, source=src)
            elif src == "uploaded data":
                rep.add("error", "Balance sheet", fy, msg + " Uploaded figures must balance - fix and re-upload.",
                        source=src)
            else:
                rep.add("error", "Balance sheet", fy, msg + f" Source is {src}: you may proceed anyway with a "
                        "warning, but check the annual report.", overridable=True, source=src)
        elif gap > config.BALANCE_TOLERANCE_WARN:
            rep.add("warning", "Balance sheet", fy, msg, source=src)

    # 1b. internal consistency (sub-totals cannot exceed totals)
    pairs = [("current_assets", "total_assets"), ("cash", "current_assets"),
             ("receivables", "current_assets"), ("inventory", "current_assets"),
             ("current_liabilities", "total_liabilities"), ("ppe", "total_assets")]
    for fy in fd.years:
        for part, whole in pairs:
            a, b = v.at[part, fy], v.at[whole, fy]
            if pd.notna(a) and pd.notna(b) and b > 0 and a > b * 1.01:
                rep.add("warning", "Consistency", fy,
                        f"{ITEM_LABELS[part]} ({a:,.1f}) exceeds {ITEM_LABELS[whole]} ({b:,.1f}).")

    # 2. No gaps between years
    ys = [fy_to_year(f) for f in fd.years]
    if ys:
        missing = sorted(set(range(min(ys), max(ys) + 1)) - set(ys))
        if missing:
            rep.add("warning", "Year gaps", ", ".join(fy_label_from_year(m) for m in missing),
                    "Missing year(s) between the first and last period. Growth, average-balance and "
                    "velocity ratios across the gap will be skipped.")

    # 3. Unit consistency
    for fy in fd.years:
        ceiling = config.MAX_PLAUSIBLE_CRORE_FINANCIAL if fd.meta.get("is_financial") else config.MAX_PLAUSIBLE_CRORE
        big = [k for k in ITEM_KEYS if k != "shares_outstanding"
               and pd.notna(v.at[k, fy]) and abs(v.at[k, fy]) > ceiling]
        if big:
            src = _cells_source(fd, fy, big)
            hint = "Were absolute rupees entered as crore?" if src == "uploaded data" else \
                "Check the currency and units Yahoo reports for this company."
            rep.add("error", "Units", fy, f"Implausibly large values in crore for: "
                    f"{', '.join(ITEM_LABELS[k] for k in big)}. {hint}", source=src)
    if yahoo is not None and upload is not None:
        overlap = set(yahoo.years) & set(upload.years)
        ratios, diffs = [], []
        for fy in overlap:
            for k in ITEM_KEYS:
                y_, u_ = yahoo.get(k, fy), upload.get(k, fy)
                if pd.notna(y_) and pd.notna(u_) and abs(y_) > config.YOY_MIN_BASE and y_ * u_ > 0:
                    r = u_ / y_
                    ratios.append(r)
                    if abs(r - 1) > config.CELL_DIFF_INFO:
                        diffs.append(f"{ITEM_LABELS[k]} {fy}: upload {u_:,.1f} vs Yahoo {y_:,.1f}")
        if ratios:
            med = float(np.median(ratios))
            lo, hi = config.UNIT_RATIO_OK
            if not lo <= med <= hi:
                rep.add("error", "Units", ", ".join(sort_fy(list(overlap))), source="uploaded data", message=
                        f"Uploaded figures are about {med:,.2f}x Yahoo's on overlapping years - likely a unit "
                        f"mismatch (e.g. lakh vs crore). Check the unit selected for the upload.")
            elif diffs:
                rep.add("info", "Source differences", ", ".join(sort_fy(list(overlap))),
                        f"{len(diffs)} overlapping figure(s) differ by more than "
                        f"{config.CELL_DIFF_INFO:.0%}: " + "; ".join(diffs[:6])
                        + (" ..." if len(diffs) > 6 else ""))

    # 4. Year-on-year jumps above 10x
    for k in ITEM_KEYS:
        row = v.loc[k]
        for prev, cur in zip(fd.years, fd.years[1:]):
            if fy_to_year(cur) - fy_to_year(prev) != 1:
                continue
            a, b = row[prev], row[cur]
            if pd.isna(a) or pd.isna(b) or abs(a) < config.YOY_MIN_BASE or abs(b) < config.YOY_MIN_BASE:
                continue
            ratio = abs(b) / abs(a)
            if ratio >= config.YOY_JUMP_FACTOR or ratio <= 1 / config.YOY_JUMP_FACTOR:
                text = f"{ITEM_LABELS[k]} moved from {a:,.1f} to {b:,.1f} ({ratio:,.1f}x)."
                if k == "shares_outstanding":
                    rep.add("info", "YoY jump", f"{prev}->{cur}", text + " Share count - usually a split or "
                            "bonus issue; check corporate actions.")
                    continue
                material, base_txt = _is_material(fd, max(abs(a), abs(b)), cur)
                if material:
                    rep.add("warning", "YoY jump", f"{prev}->{cur}", text + f" Material ({base_txt}). Likely "
                            "a unit or typing error, or a genuine event - confirm against the annual report.")
                else:
                    rep.add("info", "YoY jump (immaterial)", f"{prev}->{cur}", text + f" Immaterial "
                            f"({base_txt}); no action needed.")

    # 4a. One-off / mislabelled items (after the exceptional-items adjustment)
    for fy, msg in one_off_checks(fd):
        rep.add("warning", "Possible one-off", fy, msg, source=_cells_source(fd, fy, ("revenue",)))

    for fy, msg in cash_one_off_checks(fd):
        rep.add("warning", "Possible one-off cash flow", fy, msg, source=_cells_source(fd, fy, ("cfo",)))

    # 4b. Revenue definition
    for msg in revenue_label_check(fd, upload):
        rep.add("warning", "Revenue definition", msg[0], msg[1])

    # 5. Fixed note (no option to change it)
    rep.add("info", "Consolidated figures", "all", CONSOLIDATED_NOTE)

    # Key rows missing entirely
    for k in ("revenue", "total_assets", "cfo", "ebitda"):
        if v.loc[k].isna().all():
            rep.add("warning", "Missing data", "all", f"{ITEM_LABELS[k]} is data unavailable in every year; "
                    "dependent ratios will be skipped and their pillar re-weighted.")
    if v.loc["revenue"].isna().all() and v.loc["total_assets"].isna().all():
        rep.add("error", "Missing data", "all", "Neither revenue nor total assets is available - cannot score.")

    # Financial companies: Yahoo bank/NBFC/insurer statements do not follow the industrial template, so no
    # check may hard-block - every error becomes a proceed-anyway warning.
    if fd.meta.get("is_financial"):
        for i in rep.issues:
            if i.severity == "error" and not i.overridable:
                i.overridable = True
                i.message += (" Financial company: shown as a warning - you may proceed anyway, but verify "
                              "against the annual report.")
    return rep


# --------------------------------------------------------------------------- data depth
DEPTH_LEVELS = [
    (1, "Core ratios (point-in-time liquidity, solvency, distress)"),
    (2, "Beneish M-Score, Sloan and balance-sheet accrual ratios, growth checks"),
    (3, "Behavioural Velocity flag"),
    (4, "CCIRS score trend over time"),
    (7, "Full business-cycle analysis"),
]


def data_depth(fd: FinancialData) -> dict:
    usable = [fy for fy in fd.years if all(pd.notna(fd.values.at[k, fy]) for k in config.DEPTH_KEY_ITEMS)]
    # longest consecutive run counts for multi-year analytics
    ys = sorted(fy_to_year(f) for f in usable)
    best = run = 1 if ys else 0
    for a, b in zip(ys, ys[1:]):
        run = run + 1 if b - a == 1 else 1
        best = max(best, run)
    n = best
    unlocked = [d for need, d in DEPTH_LEVELS if n >= need]
    locked = [f"{d} (needs {need}+ years)" for need, d in DEPTH_LEVELS if n < need]
    if n >= 7:
        badge, colour = f"{n} yrs - full cycle", "green"
    elif n >= 4:
        badge, colour = f"{n} yrs - trend ready", "green"
    elif n == 3:
        badge, colour = "3 yrs - velocity ready", "blue"
    elif n == 2:
        badge, colour = "2 yrs - forensic ratios", "orange"
    elif n == 1:
        badge, colour = "1 yr - core ratios only", "red"
    else:
        badge, colour = "No usable years", "red"
    return {"years": n, "usable_years": usable, "badge": badge, "colour": colour,
            "unlocked": unlocked, "locked": locked}


# --------------------------------------------------------------------------- demo data
def demo_data() -> FinancialData:
    """Hypothetical 'Apex Auto Components' (two years) for formula tests and offline demo.
    NOT a real company. Only the figures given in the brief are filled; everything else is NaN."""
    raw = {
        "FY25": dict(revenue=1500, ebitda=180, pat=90, cfo=135, cash=40, marketable_securities=20,
                     receivables=250, inventory=200, current_assets=630, current_liabilities=350,
                     total_assets=1200, cash_opex=1095),
        "FY26": dict(revenue=1800, ebitda=225, pat=105, cfo=45, cash=30, marketable_securities=10,
                     receivables=390, inventory=300, current_assets=810, current_liabilities=450,
                     total_assets=1500, cash_opex=1350),
    }
    vals, srcs = empty_frame(raw.keys())
    for fy, items in raw.items():
        for k, x in items.items():
            vals.at[k, fy] = float(x)
            srcs.at[k, fy] = SRC_DEMO
    meta = {"ticker": "DEMO", "name": "Apex Auto Components (HYPOTHETICAL SAMPLE)",
            "sector": "Consumer Cyclical", "industry": "Auto Parts", "currency": "INR",
            "unit_label": "Rs crore", "price": None, "market_cap_cr": None,
"labels_used": {}, "latest_fy": "FY26",
            "fetched_at": datetime.now().strftime("%d %b %Y %H:%M"), "is_demo": True}
    return FinancialData(vals, srcs, meta, ["Demo mode: hypothetical sample data from the project brief. "
                                            "Not for submission."])
