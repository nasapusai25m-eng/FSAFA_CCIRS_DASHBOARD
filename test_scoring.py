"""Stage 3: CCIRS engine - weights, sub-scores, re-weighting, distress, overrides, bands, sensitivity."""
import pytest

import config
import data as D
import ratios as R
import scoring as S
from test_ratios import FULL, make_fd


@pytest.fixture(scope="module")
def apex():
    fd = D.apply_derivations(D.demo_data())
    return fd, R.compute_all(fd)


# ------------------------------------------------------------------ weights
def test_default_weights_match_brief():
    w = S.default_weights()
    assert w["pillars"] == {"A": 30, "B": 25, "C": 25, "D": 20}
    assert w["ratios"]["A"] == {"cce": 35, "ocf_ratio": 20, "dir": 15, "wc_days": 15, "ccc_trend": 15}
    assert w["ratios"]["D"] == {"altman": 40, "piotroski": 20, "rp_advances": 20, "governance": 20}
    for p in w["ratios"].values():
        assert sum(p.values()) == 100


def test_normalise():
    assert S.normalise({"a": 1, "b": 3}) == {"a": 25.0, "b": 75.0}
    with pytest.raises(ValueError):
        S.normalise({"a": 0})


# ------------------------------------------------------------------ Apex worked example (hand arithmetic)
def test_apex_score_by_hand(apex):
    fd, bk = apex
    r = S.score(bk, fd, "FY26")
    # A: CCE 20% -> 15, OCF 0.10 -> 25, DIR 116 -> 65, WC 73 -> 55; CCC trend unavailable -> re-weight over 85
    a = (15 * 35 + 25 * 20 + 65 * 15 + 55 * 15) / 85
    # B: no interest/debt data -> pillar dropped
    # C: Sloan 4.4% -> 100 (w30); DSRI 1.30 -> 50 and rec gap 36pp -> 10 => 30 (w20); BS accrual, M-Score unavailable
    c = (100 * 30 + 30 * 20) / 50
    # D: Piotroski 2 of 6 -> 33.3 only
    d = 2 / 6 * 100
    expected = (a * 30 + c * 25 + d * 20) / 75
    assert r.score == pytest.approx(expected)
    assert r.band == "Orange" and r.verdict == "Escalate for forensic review"
    assert sum(x.contribution for x in r.components) == pytest.approx(r.raw_score)
    assert any("Pillar B" in w for w in r.warnings) and any("M-Score unavailable" in w for w in r.warnings)


# ------------------------------------------------------------------ sub-score rules
@pytest.mark.parametrize("m,score", [(-2.5, 100), (-2.22, 100), (-2.0, 50), (-1.78, 0), (-1.5, 0)])
def test_beneish_sliding_scale(m, score):
    assert S.beneish_score(m) == pytest.approx(score)


def test_altman_bands_from_brief():
    assert R.threshold_band("altman_z2", 3.0)[0] == 100
    assert R.threshold_band("altman_z2", 2.0)[0] == 50
    assert R.threshold_band("altman_z2", 0.5)[0] == 0


def test_distress_scores_zero_not_reweighted():
    fd = make_fd({"FY25": {"total_debt": 500, "equity": -100}})
    r = S.score(R.compute_all(fd), fd, "FY25")
    de = next(c for c in r.components if c.key == "de_total")
    assert de.status == S.DISTRESS_ZERO and de.sub_score == 0 and de.eff_weight == 100


def test_no_interest_is_good_not_dropped():
    fd = make_fd({"FY25": {"ebit": 100, "interest_expense": 0}})
    c = next(c for c in S.score(R.compute_all(fd), fd, "FY25").components if c.key == "interest_coverage")
    assert c.status == S.NM_GOOD and c.sub_score == 100


def test_piotroski_component_uses_assessable_share():
    fd = make_fd(FULL)
    bk = R.compute_all(fd)
    c = next(c for c in S.score(bk, fd, "FY25").components if c.key == "piotroski")
    e = bk.get("piotroski", "FY25").extra
    assert c.sub_score == pytest.approx(e["passed"] / e["assessable"] * 100)


def test_ccc_trend_component():
    base = {"inventory": 100, "cogs": 730, "receivables": 200, "revenue": 1460, "accounts_payable": 60}
    fd = make_fd({"FY24": base, "FY25": {**base, "receivables": 280}})          # CCC 70 -> 90 days (+20)
    c = next(c for c in S.score(R.compute_all(fd), fd, "FY25").components if c.key == "ccc_trend")
    assert c.sub_score == 35 and c.band == "deteriorating"


def test_financial_company_excludes_not_valid_and_is_indicative():
    fd = make_fd({"FY24": {"revenue": 3000, "total_assets": 40000, "pat": 400, "equity": 4000},
                  "FY25": {"revenue": 3300, "total_assets": 44000, "pat": 450, "equity": 4400}},
                 {"is_financial": True})
    r = S.score(R.compute_all(fd), fd, "FY25", governance={"reviewed": True, "auditor_event": "None",
                                                           "opinion": "Unmodified (clean)", "pledge_pct": 0})
    excluded = {c.key for c in r.components if c.status == S.EXCLUDED}
    assert {"cce", "de_total", "sloan", "beneish_m", "altman", "dscr"} <= excluded
    assert r.indicative_only and r.label == "Indicative only"
    assert all(c.contribution == 0 for c in r.components if c.status == S.EXCLUDED)


# ------------------------------------------------------------------ overrides
def _cfo_neg():
    return make_fd({"FY24": {"cfo": -50, "ebitda": 100, "current_liabilities": 100},
                    "FY25": {"cfo": -60, "ebitda": 120, "current_liabilities": 100, "current_assets": 400}})


def test_cfo_override_only_when_confirmed():
    fd = _cfo_neg()
    bk = R.compute_all(fd)
    gov = {"reviewed": True, "auditor_event": "None", "opinion": "Unmodified (clean)", "pledge_pct": 0}
    pending = S.score(bk, fd, "FY25", governance=gov)
    assert pending.pending and "awaiting analyst decision" in pending.pending[0] and not pending.overrides
    confirmed = S.score(bk, fd, "FY25", governance=gov, cfo_decision=S.CFO_CONFIRM)
    assert confirmed.score <= config.CFO_NEGATIVE_CAP and confirmed.overrides
    dismissed = S.score(bk, fd, "FY25", governance=gov, cfo_decision=S.CFO_DISMISS)
    assert dismissed.score == pending.score and "dismissed" in dismissed.pending[0]


def test_auditor_rule_only_for_listed_events():
    fd = make_fd({"FY25": {"cfo": 100, "ebitda": 100, "current_liabilities": 50}})     # CCE 100% -> strong
    bk = R.compute_all(fd)
    base = {"reviewed": True, "auditor_event": "None", "opinion": "Unmodified (clean)", "pledge_pct": 0}
    routine = S.score(bk, fd, "FY25", governance={**base, "auditor_event": "Routine rotation (term ended)"})
    assert not routine.overrides and routine.score > config.AUDITOR_CAP
    for change in ({"auditor_event": "Resigned - stated disagreement or concerns"}, {"opinion": "Qualified"},
                   {"opinion": "Adverse"}, {"opinion": "Disclaimer of opinion"},
                   {"opinion": "Audit report missing / not filed"}):
        r = S.score(bk, fd, "FY25", governance={**base, **change})
        assert r.score <= config.AUDITOR_CAP and any("Auditor rule" in o for o in r.overrides), change
    no_reason = S.score(bk, fd, "FY25", governance={**base, "auditor_event": "Resigned - no reason stated"})
    assert not no_reason.overrides


def test_related_party_advances_force_escalate():
    fd = make_fd({"FY25": {"cfo": 100, "ebitda": 100, "current_liabilities": 50, "revenue": 1000,
                           "related_party_advances": 600}})
    r = S.score(R.compute_all(fd), fd, "FY25")
    assert r.verdict == "Escalate for forensic review" and any("forced to Escalate" in o for o in r.overrides)


@pytest.mark.parametrize("s,band", [(75, "Green"), (74.9, "Amber"), (55, "Amber"), (54.9, "Orange"),
                                    (35, "Orange"), (34.9, "Red")])
def test_band_boundaries(s, band):
    assert S.band_for(s)[0] == band


def test_governance_not_reviewed_is_reweighted():
    fd = make_fd(FULL)
    c = next(c for c in S.score(R.compute_all(fd), fd, "FY25").components if c.key == "governance")
    assert c.sub_score is None and c.status == S.REWEIGHTED


def test_custom_weights_change_score(apex):
    fd, bk = apex
    w = S.default_weights()
    w["pillars"]["C"] = 0
    r = S.score(bk, fd, "FY26", weights=w)
    assert r.score != pytest.approx(S.score(bk, fd, "FY26").score)


# ------------------------------------------------------------------ sensitivity
def test_sensitivity(apex):
    fd, bk = apex
    df, s = S.sensitivity(bk, fd, "FY26")
    assert len(df) == 2 * (4 + sum(len(p["ratios"]) for p in config.PILLARS.values()))     # 42
    assert s["min"] <= s["base"] <= s["max"]
    assert 0 <= s["verdict_stable_pct"] <= 100
    assert s["verdict_stable_pct"] == 100 and not s["flips"]


def test_score_all_years(apex):
    fd, bk = apex
    t = S.score_all_years(bk, fd)
    assert list(t["Year"]) == ["FY25", "FY26"]


# ------------------------------------------------------------------ market perception display fix
def test_market_rows_show_current_price_only(monkeypatch):
    import pandas as pd
    from streamlit.testing.v1 import AppTest
    T = pd.Timestamp

    def fake(t):
        inc = pd.DataFrame({T("2025-03-31"): [1000e7, 100e7], T("2024-03-31"): [900e7, 80e7]},
                           index=["Total Revenue", "Net Income"])
        bs = pd.DataFrame({T("2025-03-31"): [2000e7, 10e7], T("2024-03-31"): [1800e7, 10e7]},
                          index=["Total Assets", "Ordinary Shares Number"])
        fd = D.build_from_yahoo_frames(inc, bs, None, {"longName": "Test Ltd", "currentPrice": 250.0,
                                                       "marketCap": 2500e7}, t)
        fd.meta["fetched_at"] = "now"
        return fd
    monkeypatch.setattr(D, "fetch_yahoo", fake)
    monkeypatch.setattr(D, "search_companies", lambda q: [{"name": "Test Ltd", "ticker": "TEST.NS", "exchange": "NSE"}])
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    at.sidebar.text_input[0].set_value("test").run()
    at.sidebar.button[0].click().run()
    assert not at.exception
    md = " ".join(m.value for m in at.markdown)
    pe_row = md[md.index(">P/E<"): md.index("</tr>", md.index(">P/E<"))]
    assert "current price (vs FY25)" in pe_row and "25.00x" in pe_row        # 250 / (100/10)
    assert pe_row.count("<td") == 2       # after the label: one current-price cell + status (no year columns)
