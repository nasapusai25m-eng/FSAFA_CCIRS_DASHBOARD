"""Public-deployment clean-up: flags, tab order, analyst inputs, explain-then-decide dashboard, source markers,
CCIRS-tab layout - and proof that no score changed."""
import json
import sys
import tomllib
from pathlib import Path

import pandas as pd
import pytest

import config
import dashboard as DB
import data as D
import ratios as R
import scoring as S
from conftest import DEPLOY_FLAGS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import score_snapshot as SS  # noqa: E402

FIVE_TABS = ["🏠 Dashboard", "🎯 CCIRS score", "🧮 Ratios", "📊 Financial data", "🔎 Data checks"]


def _raw(name="Deploy Co Ltd"):
    T = pd.Timestamp
    inc = pd.DataFrame({T("2025-03-31"): [1000e7, 200e7, 150e7, 100e7, 10e7],
                        T("2024-03-31"): [900e7, 180e7, 135e7, 90e7, 9e7]},
                       index=["Total Revenue", "EBITDA", "EBIT", "Net Income", "Interest Expense"])
    bs = pd.DataFrame({T("2025-03-31"): [2000e7, 800e7, 1200e7, 300e7], T("2024-03-31"): [1800e7, 700e7, 1100e7, 280e7]},
                      index=["Total Assets", "Total Liabilities Net Minority Interest", "Stockholders Equity",
                             "Total Debt"])
    cf = pd.DataFrame({T("2025-03-31"): [150e7], T("2024-03-31"): [130e7]}, index=["Operating Cash Flow"])
    fd = D.build_from_yahoo_frames(inc, bs, cf, {"longName": name, "industry": "Auto Parts"}, "DEPLOY.NS")
    fd.meta["fetched_at"] = "now"
    return fd


def _app(monkeypatch, demo=False, backtest=False):
    """App as deployed (demo and backtest off) with a mocked Yahoo company, or via the demo when demo=True."""
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(config, "ENABLE_DEMO", demo)
    monkeypatch.setattr(config, "ENABLE_BACKTEST", backtest)
    monkeypatch.setattr(D, "fetch_yahoo", lambda t: _raw())
    monkeypatch.setattr(D, "search_companies",
                        lambda q: [{"name": "Deploy Co Ltd", "ticker": "DEPLOY.NS", "exchange": "NSE"}])
    at = AppTest.from_file("../app.py", default_timeout=120).run()
    if demo:
        at.sidebar.radio[0].set_value("Demo mode (hypothetical sample)").run()
    else:
        at.sidebar.text_input[0].set_value("deploy").run()
    at.sidebar.button[0].click().run()
    assert not at.exception, at.exception
    return at


def _texts(el):
    return [m.value for m in el.markdown] + [c.value for c in el.caption]


# =================================================================== 1-2 flags
def test_deploy_flags_off_in_config():
    assert DEPLOY_FLAGS == {"ENABLE_DEMO": False, "ENABLE_BACKTEST": False, "ENABLE_AI_MEMO": False}


def test_demo_hidden_and_tabs_in_order(monkeypatch):
    at = _app(monkeypatch)
    opts = list(at.sidebar.radio[0].options)
    assert opts == ["Search company (Yahoo Finance)", "Upload only"]
    assert [t.label for t in at.tabs] == FIVE_TABS                       # no Backtest, AI memo, Validation...
    assert not any(str(b.key) == "bt_hc" for b in at.get("download_button"))
    everything = " ".join(_texts(at) + [w.value for w in at.warning] + [i.value for i in at.info])
    assert "not for submission" not in everything.lower() and "Demo mode" not in everything


def test_demo_and_backtest_shown_when_enabled(monkeypatch):
    at = _app(monkeypatch, demo=True, backtest=True)
    assert "Demo mode (hypothetical sample)" in at.sidebar.radio[0].options
    labels = [t.label for t in at.tabs]
    assert labels[:5] == FIVE_TABS and "📈 Backtest" in labels


def test_no_submission_wording_in_source():
    assert "not for submission" not in (ROOT / "app.py").read_text().lower()


# =================================================================== 3 subtitle and toolbar
def test_subtitle_and_toolbar(monkeypatch):
    at = _app(monkeypatch)
    caps = [c.value for c in at.caption]
    assert config.APP_SUBTITLE in caps and not any("Stages 1-6" in c for c in caps)
    cfg = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text())
    assert cfg["client"]["toolbarMode"] == "minimal"


# =================================================================== 4 data checks tab
def test_data_checks_tab_merges_validation_depth_and_mapping(monkeypatch):
    at = _app(monkeypatch)
    tab = at.tabs[4]
    assert [m.label for m in tab.metric][:4] == ["Data depth", "Usable years", "Errors", "Warnings"]
    exp = [e for e in tab.expander if e.label == config.SOURCE_MAPPING_TITLE]
    assert len(exp) == 1
    assert any("Data coverage" in m for m in _texts(tab))


# =================================================================== 5 analyst inputs inside the CCIRS tab
def test_analyst_inputs_in_one_collapsed_expander_on_score_tab(monkeypatch):
    at = _app(monkeypatch)
    score_tab = at.tabs[1]
    exp = [e for e in score_tab.expander if e.label == config.ANALYST_INPUTS_TITLE]
    assert len(exp) == 1
    inner = exp[0]
    assert config.ANALYST_INPUTS_WHY in _texts(inner)
    assert {w.key for w in inner.selectbox} >= {"gov_auditor", "gov_opinion"}
    assert [c.key for c in inner.checkbox] == ["gov_reviewed"]
    assert [n.key for n in inner.number_input] == ["gov_pledge"]
    assert any(b.key == "rp_apply" for b in inner.button)
    # nothing above the tabs any more
    assert not any(e.label.startswith("Related-party inputs") for e in at.expander)


def test_scores_unchanged_by_moving_inputs(monkeypatch):
    """Governance entered in the new expander still feeds the score exactly as before."""
    at = _app(monkeypatch)
    before = at.session_state["gov_reviewed"]
    at.checkbox(key="gov_reviewed").check().run()
    assert not at.exception and before is False and at.session_state["gov_reviewed"] is True


# =================================================================== 6 dashboard: explain, then decide
def test_dashboard_order(monkeypatch):
    at = _app(monkeypatch)
    dash = at.tabs[0]
    md = [m.value for m in dash.markdown]
    joined = "\n".join(md)
    assert config.CCIRS_TAGLINE.replace("&", "&amp;") in joined or config.CCIRS_TAGLINE in joined
    exp = [e for e in dash.expander if e.label == "What is CCIRS?"]
    assert len(exp) == 1 and exp[0].proto.expanded is True
    order = ["#### What drove the score", "**Red flags**", "### Screening recommendation", "**What-if: weights**",
             "**Peer comparison**"]
    pos = [next(i for i, m in enumerate(md) if m.startswith(s)) for s in order]
    assert pos == sorted(pos), pos
    assert "Standard covenants and annual review" not in joined
    assert any("is at or above 75 -> Approve" in m or "is between" in m or "is below" in m for m in md)


def test_what_is_ccirs_text():
    txt = " ".join(config.WHAT_IS_CCIRS)
    for s in ("Cash & Liquidity **30**", "Solvency & Debt Service **25**", "Earnings Integrity **25**",
              "Distress & Governance **20**", "75+", "55-75", "35-55", "below 35", "Overrides", "liquidity"):
        assert s.lower() in txt.lower(), s
    assert 5 <= len(config.WHAT_IS_CCIRS) <= 6


@pytest.mark.parametrize("score,text", [(79.0, "CCIRS 79.0 is at or above 75 -> Approve"),
                                        (75.0, "CCIRS 75.0 is at or above 75 -> Approve"),
                                        (62.4, "CCIRS 62.4 is between 55 and 75 -> Approve with conditions"),
                                        (40.0, "CCIRS 40.0 is between 35 and 55 -> Escalate for forensic review"),
                                        (20.0, "CCIRS 20.0 is below 35 -> Decline")])
def test_band_rule(score, text):
    assert DB.band_rule(score) == text


def _scored(make):
    fd = D.apply_derivations(make())
    book = R.compute_all(fd, {})
    return fd, book, S.score(book, fd, fd.years[-1], S.default_weights(), {"reviewed": False})


def test_drivers_come_from_breakdown():
    _, _, r = _scored(D.demo_data)
    drv = DB.drivers(r)
    bd = r.breakdown_frame().set_index("Component")
    assert 1 <= len(drv["strongest"]) <= 3 and 1 <= len(drv["weakest"]) <= 3
    for d in drv["strongest"]:
        assert d["gained"] == pytest.approx(bd.at[d["label"], "Contribution (points)"], abs=0.01)
    for d in drv["weakest"]:
        expected = (100 - d["sub_score"]) * bd.at[d["label"], "Weight in pillar %"] / 100 \
            * bd.at[d["label"], "Pillar weight %"] / 100
        assert d["lost"] == pytest.approx(expected, abs=0.1)        # breakdown weights are rounded to 0.1
    assert not {d["key"] for d in drv["strongest"]} & {d["key"] for d in drv["weakest"]}


def test_screening_recommendation_cites_evidence_and_ties_covenants():
    fd, book, r = _scored(SS.OFFLINE["Airtel-like (synthetic)"])
    rec = DB.screening_recommendation(r, R.red_flags(fd, book))
    assert rec["evidence"][0] == DB.band_rule(r.score) + "."
    assert any(e.startswith("Holds it back:") and "pts" in e for e in rec["evidence"])
    assert all("Standard covenants" not in c for c in rec["covenants"])
    assert any("weakest area" in c for c in rec["covenants"])


def test_screening_recommendation_reports_pending_override():
    fd, book, r = _scored(SS.OFFLINE["Vodafone-Idea-like (synthetic)"])
    rec = DB.screening_recommendation(r, R.red_flags(fd, book))
    assert any(o.startswith("Pending:") and "net worth" in o for o in rec["overrides"])


# =================================================================== 7 source markers
def test_source_markers_only_on_non_yahoo_cells():
    fd = D.apply_derivations(_raw())
    fd = D.apply_manual(fd, {("related_party_advances", fd.years[-1]): 5.0})
    tbl = DB.marked_table(fd)
    src = fd.sources.copy()
    src.index = tbl.index
    for r in tbl.index:
        for c in tbl.columns:
            v, s = str(tbl.at[r, c]), str(src.at[r, c] or "")
            if v == "data unavailable":
                continue
            if s == D.SRC_YAHOO:
                assert "(" not in v, (r, c, v)
            elif s == D.SRC_MANUAL:
                assert v.endswith("(M)")
            elif s.endswith(D.DERIVED_SUFFIX):
                assert v.endswith("(D)")
    assert any(str(v).endswith("(M)") for v in tbl.values.ravel())


def test_upload_marker_and_legend(monkeypatch):
    fd = D.apply_derivations(_raw())
    fd.sources.iloc[0, 0] = D.SRC_UPLOAD
    assert DB.marked_table(fd).iloc[0, 0].endswith("(U)")
    at = _app(monkeypatch)
    data_tab = at.tabs[3]
    assert any(config.SOURCE_LEGEND in m for m in _texts(data_tab))
    assert any(c.startswith("Year source:") for c in _texts(data_tab))


# =================================================================== 8 CCIRS tab layout
def test_score_tab_order_and_technical_detail(monkeypatch):
    at = _app(monkeypatch)
    tab = at.tabs[1]
    md = [m.value for m in tab.markdown]
    order = ["#### How the score was built", "#### Overrides", "#### Downloads", "#### Technical detail"]
    pos = [md.index(s) for s in order]
    assert pos == sorted(pos)
    labels = [e.label for e in tab.expander]
    assert labels.index(config.ANALYST_INPUTS_TITLE) < len(labels)
    for s in ("Weights and sub-weights", "Sensitivity scenarios", "Altman details"):
        assert any(l.startswith(s) for l in labels), s
    assert {b.key for b in tab.get("download_button")} >= {"dl_calcbook"}


# =================================================================== 9 scores identical
def test_scores_identical_to_golden_snapshot():
    golden = json.loads((ROOT / "tests" / "golden_scores.json").read_text())
    assert SS.compare(golden, SS.snapshot(offline=True)) == []


def test_snapshot_compare_reports_changes():
    a = {"X": {"ccirs": 80.0}}
    assert SS.compare(a, a) == [] and SS.compare(a, {"X": {"ccirs": 79.0}})
