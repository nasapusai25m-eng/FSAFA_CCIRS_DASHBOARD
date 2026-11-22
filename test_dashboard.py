"""Stage 4 dashboard + backtest fixes: negative-equity override, inventory materiality, captive caveat,
loss-maker note, low-coverage years, lease consistency, Altman note, peers (one failing), smoke tests."""
import pandas as pd
import pytest

import config
import dashboard as DB
import data as D
import ratios as R
import scoring as S
from test_ratios import FULL, make_fd

T = pd.Timestamp
GOV_OK = {"reviewed": True, "auditor_event": "None", "opinion": "Unmodified (clean)", "pledge_pct": 0}


# ------------------------------------------------------------------ a) negative net worth override
def _neg_equity():
    return make_fd({"FY25": {"cfo": 100, "ebitda": 100, "current_liabilities": 50, "equity": -500,
                             "total_debt": 2000, "pat": -300}})


def test_negative_equity_override_confirm_dismiss():
    fd = _neg_equity()
    bk = R.compute_all(fd)
    pending = S.score(bk, fd, "FY25", governance=GOV_OK)
    assert any("Negative net worth" in p and "awaiting" in p for p in pending.pending)
    assert not any("Negative net worth" in o for o in pending.overrides)
    confirmed = S.score(bk, fd, "FY25", governance=GOV_OK, neq_decision=S.NEQ_CONFIRM)
    assert confirmed.score <= config.NEG_EQUITY_CAP and confirmed.band == "Red" and confirmed.verdict == "Decline"
    assert any("Negative net worth" in o and "34" in o for o in confirmed.overrides)
    dismissed = S.score(bk, fd, "FY25", governance=GOV_OK, neq_decision=S.NEQ_DISMISS)
    assert dismissed.score == pending.score and any("dismissed" in p for p in dismissed.pending)


def test_positive_equity_no_override():
    fd = make_fd({"FY25": {"cfo": 100, "ebitda": 100, "equity": 500}})
    r = S.score(R.compute_all(fd), fd, "FY25")
    assert not any("net worth" in x for x in r.pending + r.overrides)


# ------------------------------------------------------------------ b) inventory materiality
def test_inventory_flag_material_is_red():
    fd = make_fd({"FY24": {"inventory": 100, "revenue": 1000, "total_assets": 2000},
                  "FY25": {"inventory": 150, "revenue": 1100, "total_assets": 2100}})
    f = [x for x in R.red_flags(fd, R.compute_all(fd)) if x.code.startswith("inventory")]
    assert f and f[0].code == "inventory_build" and f[0].severity == "red"


def test_inventory_flag_tiny_base_is_info():          # Vodafone Idea style: +200% on a tiny base
    fd = make_fd({"FY24": {"inventory": 1, "revenue": 40000, "total_assets": 200000},
                  "FY25": {"inventory": 3, "revenue": 42000, "total_assets": 190000}})
    f = [x for x in R.red_flags(fd, R.compute_all(fd)) if x.code.startswith("inventory")]
    assert f and f[0].code == "inventory_build_immaterial" and f[0].severity == "info"
    assert "shown as information" in f[0].text


# ------------------------------------------------------------------ c) captive-finance caveat (score unchanged)
def test_captive_caveat_shown_without_changing_score():
    fd = make_fd({"FY24": {"cfo": -500, "ebit": 900, "ebitda": 1000},
                  "FY25": {"cfo": -700, "ebit": 1000, "ebitda": 1100}})
    bk = R.compute_all(fd)
    r = S.score(bk, fd, "FY25", cfo_decision=S.CFO_DISMISS)
    assert r.caveats == [config.CAPTIVE_CAVEAT]
    assert r.score == pytest.approx(r.raw_score)      # caveat never moves the score


# ------------------------------------------------------------------ d) earnings integrity for loss-makers
def test_earnings_integrity_limited_for_loss_maker():
    years = {fy: dict(d) for fy, d in FULL.items()}
    years["FY25"]["pat"] = -50
    fd = make_fd(years)
    ei = next(p for p in S.score(R.compute_all(fd), fd, "FY25").pillars if p.key == "C")
    assert ei.limited and ei.note == config.LOSS_MAKER_EI_NOTE
    ok = next(p for p in S.score(R.compute_all(make_fd(FULL)), make_fd(FULL), "FY25").pillars if p.key == "C")
    assert not ok.limited


# ------------------------------------------------------------------ e) related-party receivables wording
def test_rp_receivables_flag_text():
    fd = D.apply_manual(make_fd({"FY25": {"revenue": 1000, "receivables": 200}}),
                        {("related_party_receivables", "FY25"): 120})
    f = next(x for x in R.red_flags(fd, R.compute_all(fd)) if x.code == "rp_receivables_share")
    assert config.RP_RECEIVABLES_NOTE in f.text


# ------------------------------------------------------------------ f) low-coverage years
def test_low_coverage_years_not_comparable():
    fd = D.apply_derivations(D.demo_data())
    t = S.score_all_years(R.compute_all(fd), fd)
    for _, row in t.iterrows():
        cov = float(row["Coverage"].rstrip("%")) / 100
        assert row["Comparable"] == (cov >= config.LOW_COVERAGE_YEAR)
        if not row["Comparable"]:
            assert row["Band"] == config.LOW_COVERAGE_LABEL and row["Verdict"] == "-"


def test_ccirs_chart_hollow_unjoined_low_coverage():
    df = pd.DataFrame({"Year": ["FY23", "FY24", "FY25"], "CCIRS": [40.0, 55.0, 60.0],
                       "Band": [config.LOW_COVERAGE_LABEL, "Amber", "Amber"], "Verdict": ["-", "x", "x"],
                       "Coverage": ["40%", "90%", "95%"], "Comparable": [False, True, True], "Overrides": "-"})
    fig = DB.fig_ccirs(df)
    line = next(t for t in fig.data if t.name == "CCIRS")
    low = next(t for t in fig.data if t.name == config.LOW_COVERAGE_LABEL)
    assert list(line.x) == ["FY24", "FY25"] and "lines" in line.mode
    assert list(low.x) == ["FY23"] and low.mode == "markers" and low.marker.color == "rgba(0,0,0,0)"
    assert "earliest year lacks prior-year data" in low.hovertemplate


# ------------------------------------------------------------------ g) lease consistency + Altman note
def test_net_debt_includes_leases():
    fd = make_fd({"FY25": {"total_debt": 600, "lease_liabilities": 200, "cash": 50, "marketable_securities": 50,
                           "ebitda": 250}})
    r = R.compute_all(fd, {"include_noncurrent": False}).get("net_debt_ebitda", "FY25")
    assert r.value == pytest.approx((600 - 100) / 250)               # total debt already carries the leases
    assert "incl. leases" in r.name and "Net debt incl. leases FY25" in r.inputs
    assert "incl. leases" in config.MAIN_TABLE_LABELS["net_debt_ebitda"]


def test_derived_total_debt_adds_leases_for_upload():
    fd = D.apply_derivations(make_fd({"FY25": {"current_debt": 100, "long_term_debt": 300,
                                               "lease_liabilities": 50}}))
    assert fd.get("total_debt", "FY25") == 450


def test_derived_total_debt_no_double_count_for_lease_inclusive_yahoo_labels():
    bs = pd.DataFrame({T("2025-03-31"): [1000e7, 100e7, 300e7, 50e7]},
                      index=["Total Assets", "Current Debt And Capital Lease Obligation",
                             "Long Term Debt And Capital Lease Obligation", "Capital Lease Obligations"])
    fd = D.apply_derivations(D.build_from_yahoo_frames(None, bs, None, {}, "X.NS"))
    assert fd.get("total_debt", "FY25") == pytest.approx(400)


def test_altman_z2_asset_heavy_note():
    years = {fy: dict(d, ppe=900) for fy, d in FULL.items()}
    fd = make_fd(years, {"industry": "Telecom Services", "sector": "Communication Services"})
    r = R.compute_all(fd).get("altman", "FY25")
    assert config.ALTMAN_Z2_ASSET_HEAVY_NOTE in r.note
    fd2 = make_fd(FULL, {"industry": "Auto Manufacturers"})              # Z' variant: no note
    assert config.ALTMAN_Z2_ASSET_HEAVY_NOTE not in R.compute_all(fd2).get("altman", "FY25").note


# ------------------------------------------------------------------ dashboard helpers
def _raw(name="Peer Ltd", cfo=150.0):
    inc = pd.DataFrame({T("2025-03-31"): [1000e7, 200e7, 150e7, 100e7, 10e7],
                        T("2024-03-31"): [900e7, 180e7, 135e7, 90e7, 9e7]},
                       index=["Total Revenue", "EBITDA", "EBIT", "Net Income", "Interest Expense"])
    bs = pd.DataFrame({T("2025-03-31"): [2000e7, 800e7, 1200e7, 300e7],
                       T("2024-03-31"): [1800e7, 700e7, 1100e7, 280e7]},
                      index=["Total Assets", "Total Liabilities Net Minority Interest", "Stockholders Equity",
                             "Total Debt"])
    cf = pd.DataFrame({T("2025-03-31"): [cfo * 1e7], T("2024-03-31"): [130e7]}, index=["Operating Cash Flow"])
    fd = D.build_from_yahoo_frames(inc, bs, cf, {"longName": name, "industry": "Auto Parts"}, "X.NS")
    fd.meta["fetched_at"] = "now"
    return fd


def test_peer_rows_one_failing_peer_does_not_break():
    def loader(t):
        if t == "BAD.NS":
            raise ConnectionError("blocked")
        return _raw(t)
    rows = DB.peer_rows([{"name": "Good", "ticker": "GOOD.NS"}, {"name": "Bad", "ticker": "BAD.NS"}], loader)
    assert rows[0]["error"] == "" and rows[0]["CCIRS"] is not None
    assert "Could not load BAD.NS" in rows[1]["error"]
    tbl = DB.peer_table(rows)
    assert list(tbl["Company"]) == ["Good"]
    assert len(DB.fig_radar(rows).data) == 1


def test_peer_rows_capped_at_max():
    peers = [{"name": f"P{i}", "ticker": f"P{i}.NS"} for i in range(6)]
    assert len(DB.peer_rows(peers, lambda t: _raw(t))) == config.MAX_PEERS


def test_dashboard_flags_sorted_and_include_scores():
    fd = make_fd(FULL, {"industry": "Telecom Services"})
    bk = R.compute_all(fd)
    r = S.score(bk, fd, "FY25")
    rows = DB.dashboard_flags(fd, bk, r, R.red_flags(fd, bk), bk.distress_summary())
    order = [DB.SEV_ORDER[x["severity"]] for x in rows]
    assert order == sorted(order)
    text = " ".join(x["text"] for x in rows)
    assert "Beneish M-Score" in text and "Altman" in text
    assert len({x["text"] for x in rows}) == len(rows)            # de-duplicated


def test_decision_box():
    fd = D.apply_derivations(D.demo_data())
    bk = R.compute_all(fd)
    r = S.score(bk, fd, "FY26")
    d = DB.decision(r, R.red_flags(fd, bk))
    assert d["recommendation"].startswith("Escalate")
    assert "Weakest pillar" in d["rationale"][0] and len(d["rationale"]) == 2
    assert config.COVENANTS["cce"] in d["covenants"]               # CCE 20% -> minimum CCE covenant
    assert config.COVENANT_ALWAYS["Orange"] in d["covenants"]
    assert [i for i, _ in d["investigate"]] and sorted(i for i, _ in d["investigate"]) == sorted(config.INVESTIGATE_NEXT)


# ------------------------------------------------------------------ app smoke tests
def _app(monkeypatch, fetch=None, query=None, results=None):
    from streamlit.testing.v1 import AppTest
    if fetch:
        monkeypatch.setattr(D, "fetch_yahoo", fetch)
    if results is not None:
        monkeypatch.setattr(D, "search_companies", results)
    at = AppTest.from_file("../app.py", default_timeout=90).run()
    if query:
        at.sidebar.text_input[0].set_value(query).run()
    else:
        at.sidebar.radio[0].set_value("Demo mode (hypothetical sample)").run()
    at.sidebar.button[0].click().run()
    assert not at.exception, at.exception
    return at


def _dash_text(at):
    return " ".join(m.value for m in at.markdown)


def test_dashboard_demo(monkeypatch):
    at = _app(monkeypatch)
    txt = _dash_text(at)
    for s in ("Apex Auto Components", "Escalate for forensic review", "Red flags", "Screening recommendation", "What drove the score",
              "What-if: weights", "Peer comparison", "screening tool, not proof of manipulation"):
        assert s in txt or s in " ".join(c.value for c in at.caption), s
    assert at.tabs[0].label == "🏠 Dashboard"


def test_dashboard_normal_company_with_peers(monkeypatch):
    names = {"DASHCO.NS": "Dash Co Ltd", "DASHPEER.NS": "Good Peer Ltd"}

    def fetch(t):
        if t == "DASHFAIL.NS":
            raise ConnectionError("peer blocked")
        return _raw(names[t])

    def search(q):
        return {"dashco": [{"name": "Dash Co Ltd", "ticker": "DASHCO.NS", "exchange": "NSE"}],
                "goodpeer": [{"name": "Good Peer Ltd", "ticker": "DASHPEER.NS", "exchange": "NSE"}],
                "badpeer": [{"name": "Bad Peer Ltd", "ticker": "DASHFAIL.NS", "exchange": "NSE"}]}.get(q, [])
    at = _app(monkeypatch, fetch, "dashco", search)
    for q in ("goodpeer", "badpeer"):
        at.text_input(key="peer_query").set_value(q).run()
        at.button(key="add_peer").click().run()
        assert not at.exception
    assert [p["ticker"] for p in at.session_state["peers"]] == ["DASHPEER.NS", "DASHFAIL.NS"]
    assert any("Could not load DASHFAIL.NS" in w.value for w in at.warning)
    tables = [d.value for d in at.dataframe if "Company" in getattr(d.value, "columns", [])]
    assert tables and list(tables[0]["Company"]) == ["Dash Co Ltd", "Good Peer Ltd"]


def test_dashboard_financial_company(monkeypatch):
    def fetch(t):
        inc = pd.DataFrame({T("2025-03-31"): [3000e7, 450e7], T("2024-03-31"): [2800e7, 400e7]},
                           index=["Total Revenue", "Net Income"])
        bs = pd.DataFrame({T("2025-03-31"): [40000e7, 35000e7, 4400e7], T("2024-03-31"): [36000e7, 31600e7, 4000e7]},
                          index=["Total Assets", "Total Liabilities Net Minority Interest", "Stockholders Equity"])
        fd = D.build_from_yahoo_frames(inc, bs, None, {"longName": "Dash Bank Ltd", "sector": "Financial Services",
                                                       "industry": "Banks - Regional"}, t)
        fd.meta["fetched_at"] = "now"
        return fd
    at = _app(monkeypatch, fetch, "dashbank",
              lambda q: [{"name": "Dash Bank Ltd", "ticker": "DASHBANK.NS", "exchange": "NSE"}])
    assert "Indicative only" in _dash_text(at)


def test_sidebar_has_no_ticker_box(monkeypatch):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    assert not any("ticker" in e.label.lower() for e in at.sidebar.expander)
    assert {t.label for t in at.sidebar.text_input} <= {"Company name", "Company name (press Enter)"}


def test_search_failure_message(monkeypatch):
    from streamlit.testing.v1 import AppTest

    def boom(q):
        raise D.SearchError("x")
    monkeypatch.setattr(D, "search_companies", boom)
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    at.sidebar.text_input[0].set_value("zzzz").run()
    assert any("No NSE/BSE listing found for that name, or search failed" in w.value for w in at.sidebar.warning)
