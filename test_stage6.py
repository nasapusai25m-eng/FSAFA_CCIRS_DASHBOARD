"""Stage 6: exceptional items, AI-memo flag, memo fixes (sign rule, keyed box, log dedupe), weights reset,
backtest (direction check, failing load, Spearman, overrides, sensitivity) and the hand-check workbook."""
import csv
import io
import re
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import ai_memo as AI
import backtest as BT
import config
import data as D
import handcheck as HC
import ratios as R
import scoring as S

T = pd.Timestamp


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setenv("CCIRS_AI_LOG", str(tmp_path / "ai_calls.csv"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# =================================================================== A1 exceptional items
def _vi_like(unusual_label="Total Unusual Items"):
    inc = pd.DataFrame({T("2026-03-31"): [44789e7, 76591e7, 54500e7, 58607e7, 1200e7, 26000e7],
                        T("2025-03-31"): [43571e7, 17500e7, -4500e7, 0.0, 1100e7, 25000e7]},
                       index=["Total Revenue", "EBITDA", "EBIT", unusual_label, "Interest Income Non Operating",
                              "Interest Expense"])
    return D.build_from_yahoo_frames(inc, None, None, {"longName": "VI-like"}, "VIL.NS")


def test_exceptional_gain_removed_from_operating_ebitda_and_ebit():
    fd = D.apply_derivations(_vi_like())
    assert fd.get("exceptional_items", "FY26") == pytest.approx(58607)
    assert fd.get("operating_ebitda", "FY26") == pytest.approx(76591 - 1200 - 58607)
    assert fd.get("operating_ebit", "FY26") == pytest.approx(54500 - 1200 - 58607)
    before = D.apply_derivations(_vi_like(), exclude_exceptional=False)
    assert before.get("operating_ebitda", "FY26") == pytest.approx(76591 - 1200)


def test_bridge_rows_shown_in_ratio_inputs():
    r = R.compute_all(D.apply_derivations(_vi_like())).get("interest_coverage", "FY26")
    keys = list(r.inputs)
    assert keys[:5] == ["EBIT as reported FY26", "less other income FY26", "less share of associates/JVs FY26",
                        "less exceptional items FY26", "Operating EBIT FY26"]
    assert r.inputs["less exceptional items FY26"] == pytest.approx(58607)


@pytest.mark.parametrize("label", ["Total Unusual Items Excluding Goodwill", "Special Income Charges"])
def test_other_unusual_labels(label):
    assert D.apply_derivations(_vi_like(label)).get("exceptional_items", "FY26") == pytest.approx(58607)


def test_every_ebit_ratio_uses_adjusted_figure():
    fd = D.apply_derivations(_vi_like())
    bk = R.compute_all(fd)
    op_ebit = fd.get("operating_ebit", "FY26")
    assert bk.value("interest_coverage", "FY26") == pytest.approx(op_ebit / 26000)
    assert bk.value("op_ebit_margin", "FY26") == pytest.approx(op_ebit / 44789)


def test_normal_case_unchanged():
    inc = pd.DataFrame({T("2025-03-31"): [1000e7, 200e7, 150e7, 20e7]},
                       index=["Total Revenue", "EBITDA", "EBIT", "Interest Income Non Operating"])
    fd = D.build_from_yahoo_frames(inc, None, None, {}, "N.NS")
    a, b = D.apply_derivations(fd), D.apply_derivations(fd, exclude_exceptional=False)
    assert a.get("operating_ebitda", "FY25") == b.get("operating_ebitda", "FY25") == pytest.approx(180)
    assert D.one_off_checks(a) == []


def test_gain_on_securities_not_subtracted_twice():
    inc = pd.DataFrame({T("2025-03-31"): [1000e7, 300e7, 50e7, 50e7]},
                       index=["Total Revenue", "EBITDA", "Gain On Sale Of Security", "Total Unusual Items"])
    fd = D.apply_derivations(D.build_from_yahoo_frames(inc, None, None, {}, "G.NS"))
    assert fd.get("operating_ebitda", "FY25") == pytest.approx(250)


def test_one_off_flag_fires_before_fix_not_after():
    before = D.apply_derivations(_vi_like(), exclude_exceptional=False)
    msgs = [m for _, m in D.one_off_checks(before)]
    assert any("exceeds revenue" in m for m in msgs) and any("EBIT margin" in m for m in msgs)
    assert all(config.ONE_OFF_TEXT in m for m in msgs)
    assert D.one_off_checks(D.apply_derivations(_vi_like())) == []


def test_one_off_flag_swing_rule_and_red_flag():
    fd = D.apply_derivations(_vi_like(), exclude_exceptional=False)
    assert any("moved" in m for _, m in D.one_off_checks(fd))
    assert any(f.code == "one_off" for f in R.red_flags(fd, R.compute_all(fd)))
    assert any(i.check == "Possible one-off" for i in D.validate(fd).issues)


# =================================================================== app helpers
def _app(monkeypatch, fetch=None, query=None, results=None):
    from streamlit.testing.v1 import AppTest
    if fetch:
        monkeypatch.setattr(D, "fetch_yahoo", fetch)
    if results is not None:
        monkeypatch.setattr(D, "search_companies", results)
    at = AppTest.from_file("../app.py", default_timeout=120).run()
    if query:
        at.sidebar.text_input[0].set_value(query).run()
    else:
        at.sidebar.radio[0].set_value("Demo mode (hypothetical sample)").run()
    at.sidebar.button[0].click().run()
    assert not at.exception, at.exception
    return at


# =================================================================== A2 AI memo flag
def test_ai_memo_tab_hidden_by_default(monkeypatch):
    from conftest import DEPLOY_FLAGS
    assert DEPLOY_FLAGS["ENABLE_AI_MEMO"] is False
    at = _app(monkeypatch)
    labels = [t.label for t in at.tabs]
    assert "🤖 AI memo" not in labels and "📈 Backtest" in labels
    assert "AI memo" not in " ".join(m.value for m in at.markdown)


def test_ai_memo_tab_shown_with_banner_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_AI_MEMO", True)
    at = _app(monkeypatch)
    assert "🤖 AI memo" in [t.label for t in at.tabs]
    assert any(config.AI_MEMO_BANNER in w.value for w in at.warning)


# =================================================================== A3 response box keyed to company/year/weights
def _boxes(at):
    return [a for a in at.text_area if str(a.key).startswith("memo_paste_")]


def test_response_box_resets_on_year_weights_and_company(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_AI_MEMO", True)
    at = _app(monkeypatch)
    _boxes(at)[0].set_value("## Recommendation\nRecommendation: Escalate for forensic review").run()
    at.button(key="memo_check").click().run()
    assert any("AI reliability check" in x.value for x in list(at.error) + list(at.success))
    key_before = _boxes(at)[0].key
    at.sidebar.selectbox(key="score_year").set_value("FY25").run()              # year change
    box = _boxes(at)[0]
    assert box.key != key_before and not box.value
    assert not any("AI reliability check" in x.value for x in list(at.error) + list(at.success))
    at.sidebar.selectbox(key="score_year").set_value("FY26").run()
    at.slider(key="w_p_A").set_value(61).run()                                    # weights change
    assert _boxes(at)[0].key != key_before and not _boxes(at)[0].value


def test_response_box_resets_on_company_change(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_AI_MEMO", True)
    at = _app(monkeypatch)
    _boxes(at)[0].set_value("Maruti memo text").run()
    old = _boxes(at)[0].key
    monkeypatch.setattr(D, "fetch_yahoo", lambda t: _company(t, "healthy"))
    monkeypatch.setattr(D, "search_companies", lambda q: [{"name": "Other Co", "ticker": "OTHER6.NS",
                                                           "exchange": "NSE"}])
    at.sidebar.radio[0].set_value("Search company (Yahoo Finance)").run()
    at.sidebar.text_input[0].set_value("other").run()
    at.sidebar.button[0].click().run()
    assert not at.exception
    new = _boxes(at)[0]
    assert new.key != old and "OTHER6.NS" in new.key and not new.value


# =================================================================== A3 log de-duplication
def test_log_dedupe_on_write_and_existing(tmp_path):
    p = tmp_path / "log.csv"
    rec = {"company": "Maruti", "scoring_year": "FY26", "mode": "manual", "response": "memo A", "payload_hash": "h1"}
    assert AI.log_call(rec, p) is True
    assert AI.log_call(rec, p) is False                                  # identical response, same payload
    assert AI.log_call({**rec, "response": "memo B"}, p) is False        # repeat within 10 s, same payload
    assert AI.log_call({**rec, "payload_hash": "h2", "response": "memo C"}, p) is True
    assert len(AI.read_log(p)) == 2
    # existing file with duplicates (the 20:42:58 / 20:43:02 case)
    q = tmp_path / "old.csv"
    with q.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=AI.LOG_FIELDS)
        w.writeheader()
        for ts in ("2026-11-20T20:42:58", "2026-11-20T20:43:02"):
            w.writerow({"timestamp": ts, "company": "Maruti", "scoring_year": "FY26", "mode": "manual",
                        "response": "same", "payload_hash": "h9"})
        w.writerow({"timestamp": "2026-11-20T21:00:00", "company": "Vodafone Idea", "scoring_year": "FY26",
                    "mode": "manual", "response": "other", "payload_hash": "h8"})
    assert AI.dedupe_log(q) == 1
    assert [r["company"] for r in AI.read_log(q)] == ["Maruti", "Vodafone Idea"]


# =================================================================== A4 weights reset clears everything
def test_reset_all_weights_clears_custom_label_everywhere(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_AI_MEMO", True)
    at = _app(monkeypatch)
    at.slider(key="w_p_A").set_value(61).run()
    at.slider(key="w_r_B_dscr").set_value(70).run()
    assert {p: at.session_state[f"w_p_{p}"] for p in "ABCD"} == {"A": 61, "B": 14, "C": 14, "D": 11}
    at.button(key="reset_weights").click().run()
    assert not at.exception
    for p, spec in config.PILLARS.items():
        assert at.session_state[f"w_p_{p}"] == spec["weight"]
        for k, w in spec["ratios"].items():
            assert at.session_state[f"w_r_{p}_{k}"] == w
    md = " ".join(m.value for m in at.markdown)
    caps = " ".join(c.value for c in at.caption)
    assert config.CUSTOM_WEIGHTS_LABEL not in md
    assert not any(config.CUSTOM_WEIGHTS_LABEL in w.value for w in at.warning)
    assert "Weights used: Default CCIRS weights" in caps                       # CCIRS tab export line + memo tab


# =================================================================== B backtest (mocked)
def _company(ticker, kind):
    """Synthetic Yahoo-shaped statements. kind: healthy | stressed | stressed_strong (beats its healthy pair)
    | cfo_neg."""
    good = dict(rev=10000, ebitda=2000, ebit=1600, pat=1100, cfo=1900, int_=60, ta=12000, ca=5000, cl=3000,
                re_=5000, eq=7000, tl=5000, debt=1000)
    bad = dict(rev=8000, ebitda=600, ebit=200, pat=-300, cfo=150, int_=500, ta=12000, ca=2500, cl=4500,
               re_=-2000, eq=1500, tl=10500, debt=7000)
    p = {"healthy": good, "stressed": bad, "stressed_strong": {**good, "cfo": 2100, "pat": 1000},
         "cfo_neg": {**good, "cfo": -400}}[kind]
    weak_healthy = kind == "healthy" and ticker == "WEAKH.NS"
    if weak_healthy:
        p = {**good, "cfo": 300, "int_": 600, "debt": 6000}
    cols = [T("2025-03-31"), T("2024-03-31")]
    prior = {**p, "cfo": p["cfo"] if kind != "cfo_neg" else -300, "ta": p["ta"] * 0.95}
    inc = pd.DataFrame({c: [d["rev"] * 1e7, d["ebitda"] * 1e7, d["ebit"] * 1e7, d["pat"] * 1e7, d["int_"] * 1e7]
                        for c, d in zip(cols, (p, prior))},
                       index=["Total Revenue", "EBITDA", "EBIT", "Net Income", "Interest Expense"])
    bs = pd.DataFrame({c: [d["ta"] * 1e7, d["ca"] * 1e7, d["cl"] * 1e7, d["re_"] * 1e7, d["eq"] * 1e7,
                           d["tl"] * 1e7, d["debt"] * 1e7] for c, d in zip(cols, (p, prior))},
                      index=["Total Assets", "Current Assets", "Current Liabilities", "Retained Earnings",
                             "Stockholders Equity", "Total Liabilities Net Minority Interest", "Total Debt"])
    cf = pd.DataFrame({c: [d["cfo"] * 1e7] for c, d in zip(cols, (p, prior))}, index=["Operating Cash Flow"])
    fd = D.build_from_yahoo_frames(inc, bs, cf, {"longName": ticker, "industry": "Auto Parts"}, ticker)
    fd.meta["fetched_at"] = "test"
    return fd


SET = [{"sector": "Auto", "role": "healthy", "name": "Good Auto", "ticker": "GOODA.NS"},
       {"sector": "Auto", "role": "stressed", "name": "Bad Auto", "ticker": "BADA.NS"},
       {"sector": "Steel", "role": "healthy", "name": "Weak Healthy Steel", "ticker": "WEAKH.NS"},
       {"sector": "Steel", "role": "stressed", "name": "Strong Stressed Steel", "ticker": "STRONGS.NS"},
       {"sector": "Power", "role": "healthy", "name": "Good Power", "ticker": "GOODP.NS"},
       {"sector": "Power", "role": "stressed", "name": "Broken Feed Power", "ticker": "FAIL.NS"}]


def loader(t):
    if t == "FAIL.NS":
        raise ConnectionError("Yahoo throttled")
    return _company(t, {"BADA.NS": "stressed", "STRONGS.NS": "stressed_strong"}.get(t, "healthy"))


@pytest.fixture(scope="module")
def batch():
    return BT.run_batch(SET, loader)


def test_direction_check_with_failing_pair(batch):
    dc = BT.direction_check(batch, SET).set_index("Sector")
    assert dc.at["Auto", "Result"] == "PASS"
    assert dc.at["Steel", "Result"] == "FAIL"
    assert "Pillars where the stressed firm scores higher" in dc.at["Steel", "Explanation"]
    assert "Ratios driving the gap" in dc.at["Steel", "Explanation"]
    assert dc.at["Power", "Result"] == "n/a"
    assert BT.direction_summary(dc.reset_index()) == "1 of 2 pairs pass (1 not testable)"


def test_failing_company_does_not_stop_batch(batch):
    failed = [b for b in batch if b.error]
    assert [b.company["ticker"] for b in failed] == ["FAIL.NS"] and "ConnectionError" in failed[0].error
    t = BT.results_table(batch)
    assert len(t) == 6 and t.loc[t["Ticker"] == "FAIL.NS", "Status"].iloc[0].startswith("Could not load")
    assert (t.loc[t["Ticker"] != "FAIL.NS", "Status"] == "OK").all()


def test_results_table_columns(batch):
    t = BT.results_table(batch)
    for col in ("Sector", "Scoring year", "Year type", "CCIRS", "Band", "Verdict", "A", "B", "C", "D", "Coverage",
                "Overrides", "CCIRS (pending confirmed)", "Caveats", "Data-quality flags", "Data years",
                "Source per year", "Score range (±10%)", "Verdict flips", "CCIRS before exceptional fix"):
        assert col in t.columns, col
    assert set(t["Year type"].dropna()) == {"current year"}


@pytest.mark.parametrize("x,y", [([1, 2, 3, 4], [1, 2, 3, 4]), ([1, 2, 3, 4], [4, 3, 2, 1]),
                                 ([1, 2, 2, 3], [1, 3, 2, 4]), ([10, 20, 30, 40, 50], [12, 18, 40, 35, 60])])
def test_spearman(x, y):
    def ranks(v):
        return pd.Series(v).rank(method="average").to_numpy()
    expected = np.corrcoef(ranks(x), ranks(y))[0, 1]
    assert BT.spearman(x, y) == pytest.approx(expected)
    assert BT.spearman([1, 1, 1], [1, 2, 3]) is None


def test_batch_sensitivity(batch):
    s = BT.batch_sensitivity(batch, SET)
    assert s["scenarios"] == 42
    assert -1 <= s["spearman_min"] <= s["spearman_avg"] <= 1
    assert isinstance(s["pair_changes"], int)


def test_overrides_in_batch_saved_vs_pending():
    cos = [{"sector": "Auto", "role": "healthy", "name": "Captive Auto", "ticker": "CAPT.NS"}]
    ld = lambda t: _company(t, "cfo_neg")
    b = BT.run_batch(cos, ld)[0]
    row = BT.results_table([b]).iloc[0]
    assert "CFO cap pending" in row["Overrides"] and row["Score basis"] == "uncapped - overrides pending"
    assert row["CCIRS (pending confirmed)"] <= config.CFO_NEGATIVE_CAP < row["CCIRS"]
    saved = {("CAPT.NS", b.year): {"cfo_decision": S.CFO_DISMISS, "neq_decision": S.NEQ_PENDING}}
    b2 = BT.run_batch(cos, ld, saved=saved)[0]
    row2 = BT.results_table([b2]).iloc[0]
    assert "pending" not in row2["Overrides"] and row2["CCIRS (pending confirmed)"] == row2["CCIRS"]


def test_hindsight_year_choice(batch):
    b = BT.run_batch(SET[:1], loader, years={"GOODA.NS": "FY24"})[0]
    assert b.year == "FY24" and b.year_type == "pre-stress year"


def test_exports(batch):
    assert BT.export_xlsx(batch, SET)[:2] == b"PK"
    assert BT.export_csv(batch).decode().startswith("# Backtest - default CCIRS weights")


# =================================================================== C hand-check workbook
def test_handcheck_workbook_formulas_compute_and_match(batch, tmp_path):
    from wbcheck import evaluator_or_skip, static_or_fail
    entries = HC.entries_from_results(batch, ("GOODA.NS", "BADA.NS"))
    assert [e["ticker"] for e in entries] == ["GOODA.NS", "BADA.NS"]
    raw = HC.build_workbook(entries)
    wb = static_or_fail(raw, 100)                       # runs on every Python version
    assert wb.sheetnames == ["Inputs - Good Auto", "Inputs - Bad Auto", "Calculations", "Notes"]
    assert wb["Inputs - Good Auto"]["D6"].font.color.rgb.endswith("0000FF")      # inputs are blue
    path = str(tmp_path / "handcheck.xlsx")
    with open(path, "wb") as fh:
        fh.write(raw)
    ws = wb["Calculations"]
    checks = [c.coordinate for row in ws.iter_rows() for c in row
              if c.column == 6 and isinstance(c.value, str) and c.value.startswith("=IF(")]
    xl = evaluator_or_skip(path, f"Calculations!{checks[0]}")   # numeric recalculation (skips on Python 3.14+)
    errors = []
    for name in wb.sheetnames:
        for row in wb[name].iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    v = xl.evaluate(f"'{name}'!{c.coordinate}")
                    if isinstance(v, str) and v.startswith("#"):
                        errors.append((name, c.coordinate, v))
    assert not errors, errors
    vals = [xl.evaluate(f"Calculations!{a}") for a in checks]
    assert vals and all(v in ("PASS", "PASS (both n/a)") for v in vals), vals


def test_backtest_tab_smoke(monkeypatch):
    monkeypatch.setattr(config, "BACKTEST_SET", SET)
    monkeypatch.setattr(D, "fetch_yahoo", loader)
    at = _app(monkeypatch)
    at.button(key="bt_run").click().run()
    assert not at.exception, at.exception
    assert any("Broken Feed Power" in w.value and "Could not load FAIL.NS" in w.value for w in at.warning)
    assert any("Steel: Strong Stressed Steel scores at or above Weak Healthy Steel" in e.value for e in at.error)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Direction check"] == "1 of 2 pairs pass (1 not testable)"
    assert "min" in metrics["Rank stability (Spearman)"]
    labels = [b.label for b in at.get("download_button")]
    assert any("hand-check workbook" in str(l) for l in labels)
