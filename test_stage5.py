"""Stage 4 fixes (A1-A6) and Stage 5 AI memo layer: payload, prompt, manual + mocked API modes,
reliability parser, banned phrases, verdict consistency, sections, log, exports."""
import csv
import re
import types

import pandas as pd
import pytest

import ai_memo as AI
import config
import dashboard as DB
import data as D
import ratios as R
import scoring as S
from test_ratios import FULL, make_fd

T = pd.Timestamp


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setenv("CCIRS_AI_LOG", str(tmp_path / "ai_calls.csv"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _demo():
    fd = D.apply_derivations(D.demo_data())
    bk = R.compute_all(fd)
    return fd, bk


# =================================================================== A1 custom weights label
def test_custom_weights_detection_and_description():
    w = S.default_weights()
    assert S.is_default_weights(w) and "Default CCIRS weights" in S.weights_description(w)
    w["pillars"]["A"] = 50
    assert not S.is_default_weights(w)
    assert S.weights_description(w).startswith(config.CUSTOM_WEIGHTS_LABEL)


def test_breakdown_export_states_weights():
    fd, bk = _demo()
    r = S.score(bk, fd, "FY26")
    assert r.breakdown_csv(S.default_weights()).decode().startswith("# Default CCIRS weights")


def _app(monkeypatch, fetch=None, query=None, results=None, timeout=90):
    from streamlit.testing.v1 import AppTest
    if fetch:
        monkeypatch.setattr(D, "fetch_yahoo", fetch)
    if results is not None:
        monkeypatch.setattr(D, "search_companies", results)
    at = AppTest.from_file("../app.py", default_timeout=timeout).run()
    if query:
        at.sidebar.text_input[0].set_value(query).run()
    else:
        at.sidebar.radio[0].set_value("Demo mode (hypothetical sample)").run()
    at.sidebar.button[0].click().run()
    assert not at.exception, at.exception
    return at


def _md(at):
    return " ".join(m.value for m in at.markdown)


def test_custom_weights_label_and_reset_in_app(monkeypatch):
    at = _app(monkeypatch)
    assert config.CUSTOM_WEIGHTS_LABEL not in _md(at)
    at.slider(key="w_p_A").set_value(50).run()
    assert config.CUSTOM_WEIGHTS_LABEL in _md(at)                                   # dashboard card
    assert any(config.CUSTOM_WEIGHTS_LABEL in w.value for w in at.warning)          # CCIRS score tab
    at.button(key="reset_dash").click().run()
    assert not at.exception and config.CUSTOM_WEIGHTS_LABEL not in _md(at)


# =================================================================== A2 negative net worth (three states, in the app)
def _neg_equity_fetch(t):
    inc = pd.DataFrame({T("2025-03-31"): [40000e7, 5000e7, 3000e7, -2000e7, 2500e7],
                        T("2024-03-31"): [38000e7, 4800e7, 2800e7, -2500e7, 2400e7]},
                       index=["Total Revenue", "EBITDA", "EBIT", "Net Income", "Interest Expense"])
    bs = pd.DataFrame({T("2025-03-31"): [200000e7, 260000e7, -60000e7], T("2024-03-31"): [210000e7, 255000e7, -45000e7]},
                      index=["Total Assets", "Total Liabilities Net Minority Interest", "Stockholders Equity"])
    cf = pd.DataFrame({T("2025-03-31"): [15000e7], T("2024-03-31"): [14000e7]}, index=["Operating Cash Flow"])
    fd = D.build_from_yahoo_frames(inc, bs, cf, {"longName": "Neg Worth Telecom Ltd", "industry": "Telecom Services"}, t)
    fd.meta["fetched_at"] = "now"
    return fd


def test_negative_net_worth_three_states_in_app(monkeypatch):
    at = _app(monkeypatch, _neg_equity_fetch, "negworth",
              lambda q: [{"name": "Neg Worth Telecom Ltd", "ticker": "NEGW5.NS", "exchange": "NSE"}])
    pat = re.compile(r"([\d.]+) / 100")
    grab = lambda: float(next(pat.search(m.value).group(1) for m in at.markdown if pat.search(m.value)))
    pending = grab()
    assert any("Negative net worth" in w.value and "awaiting" in w.value for w in at.warning)
    radio = next(r for r in at.radio if "Negative net worth" in r.label)
    radio.set_value(S.NEQ_CONFIRM).run()
    assert grab() == pytest.approx(min(pending, config.NEG_EQUITY_CAP))
    next(r for r in at.radio if "Negative net worth" in r.label).set_value(S.NEQ_DISMISS).run()
    assert grab() == pytest.approx(pending)
    next(r for r in at.radio if "Negative net worth" in r.label).set_value(S.NEQ_PENDING).run()
    assert grab() == pytest.approx(pending)


# =================================================================== A3 sidebar scoring year
def test_sidebar_scoring_year_applies_everywhere(monkeypatch):
    at = _app(monkeypatch)
    sel = at.sidebar.selectbox(key="score_year")
    assert sel.label == "Scoring year" and sel.value == "FY26"
    assert all(getattr(s, "key", None) != "score_year" for s in at.main.selectbox)     # no second selector
    assert any("(Indicative)" in o for o in sel.options)                            # Apex years < 70% coverage
    sel.set_value("FY25").run()
    assert not at.exception
    assert "Scoring year FY25" in _md(at) and "Scores - FY25" in _md(at)


# =================================================================== A4 name search
LISTINGS = [("BAJAJ-AUTO.NS", "Bajaj Auto Limited"), ("BAJAJ-AUTO.BO", "Bajaj Auto Limited"),
            ("BAJFINANCE.NS", "Bajaj Finance Limited"), ("BAJAJFINSV.NS", "Bajaj Finserv Ltd."),
            ("M&M.NS", "Mahindra & Mahindra Limited"), ("HEROMOTOCO.NS", "Hero MotoCorp Limited"),
            ("LT.NS", "Larsen & Toubro Limited"), ("DRREDDY.NS", "Dr. Reddy's Laboratories Limited"),
            ("BHARTIARTL.NS", "Bharti Airtel Limited"), ("IDEA.NS", "Vodafone Idea Limited"),
            ("MARUTI.NS", "Maruti Suzuki India Limited"), ("ASHOKLEY.NS", "Ashok Leyland Limited"),
            ("BAJAJ.F", "Bajaj Auto Ltd (Frankfurt)")]
# Simulated Yahoo phrase-match misses (the reported failure mode): these exact queries return nothing.
YAHOO_MISSES = {"bajaj auto", "hero motocorp", "m&m", "dr. reddy's", "larsen & toubro"}


def fake_searcher(text, fuzzy):
    t = text.lower()
    if t in YAHOO_MISSES and not fuzzy:
        return []
    if fuzzy and t in YAHOO_MISSES:
        return []
    return [{"symbol": s, "longname": n, "quoteType": "EQUITY"} for s, n in LISTINGS
            if t in n.lower() or t in s.lower()]


@pytest.mark.parametrize("query,ticker", [
    ("Bajaj Auto", "BAJAJ-AUTO.NS"), ("M&M", "M&M.NS"), ("Mahindra", "M&M.NS"), ("Hero MotoCorp", "HEROMOTOCO.NS"),
    ("Larsen & Toubro", "LT.NS"), ("Dr. Reddy's", "DRREDDY.NS"), ("Bharti Airtel", "BHARTIARTL.NS"),
    ("Vodafone Idea", "IDEA.NS"), ("Maruti", "MARUTI.NS"), ("Ashok Leyland", "ASHOKLEY.NS"),
])
def test_name_search_finds_listed_companies(query, ticker):
    rows = D.search_companies(query, searcher=fake_searcher)
    assert rows and rows[0]["ticker"] == ticker, rows


def test_hyphenated_symbol_survives_filter_and_nse_preferred():
    rows = D.filter_search_quotes([{"symbol": "BAJAJ-AUTO.BO", "longname": "Bajaj Auto Limited"},
                                   {"symbol": "BAJAJ-AUTO.NS", "longname": "Bajaj Auto Limited"}])
    assert rows == [{"name": "Bajaj Auto Limited", "ticker": "BAJAJ-AUTO.NS", "exchange": "NSE"}]


def test_bajaj_token_fallback_excludes_other_bajaj_companies():
    rows = D.search_companies("bajaj auto", searcher=fake_searcher)
    assert [r["ticker"] for r in rows] == ["BAJAJ-AUTO.NS"]


def test_search_all_calls_fail_raises():
    def boom(text, fuzzy):
        raise ConnectionError("x")
    with pytest.raises(D.SearchError):
        D.search_companies("bajaj auto", searcher=boom)


def test_query_variants():
    v = D.query_variants("Dr. Reddy's")
    assert "Dr Reddys" in v
    assert D.query_variants("M&M")[0] == "mahindra and mahindra"


# =================================================================== A5 peer notes
def test_peer_notes_cover_all_caveats():
    cap = make_fd({"FY24": {"cfo": -500, "ebit": 900, "ebitda": 1000}, "FY25": {"cfo": -700, "ebit": 1000,
                                                                                "ebitda": 1100}})
    assert "captive finance" in DB.company_summary("Ashok", cap)["Notes"]
    neg = make_fd({"FY25": {"cfo": 100, "ebitda": 100, "equity": -50, "pat": -20}})
    notes = DB.company_summary("Neg", neg)["Notes"]
    assert "negative net worth (pending)" in notes and "limited Earnings Integrity" in notes
    assert "analyst inputs not reviewed" in notes
    fin = make_fd({"FY25": {"revenue": 100, "total_assets": 1000, "pat": 10}}, {"is_financial": True})
    assert "Indicative only" in DB.company_summary("Bank", fin)["Notes"]
    gov = {"reviewed": True, "auditor_event": "None", "opinion": "Unmodified (clean)", "pledge_pct": 0}
    assert "analyst inputs not reviewed" not in DB.company_summary("Main", make_fd(FULL), governance=gov)["Notes"]


# =================================================================== A6 Altman label + contributions
def test_altman_label_and_contribution_rows():
    assert R.ALTMAN_SHORT["altman_z2"] == "Z'' (non-manufacturing, 4-factor)"
    assert "EM" not in R.ALTMAN_SHORT["altman_z2"]
    bk = R.compute_all(make_fd(FULL, {"industry": "Telecom Services"}))
    t = bk.altman_details_table()
    x1 = bk.get("altman_z2", "FY25").inputs["X1"]
    assert float(t.at["Contribution X1 (6.56 x X1)", "FY25"]) == pytest.approx(6.56 * x1, abs=1e-3)
    total = sum(float(t.at[f"Contribution {x} ({c:g} x {x})", "FY25"]) for x, c in R.ALTMAN_COEF["altman_z2"].items())
    assert total == pytest.approx(bk.value("altman_z2", "FY25"), abs=0.005)


# =================================================================== B payload
def _payload(gov=None):
    fd, bk = _demo()
    r = S.score(bk, fd, "FY26")
    by_year = S.score_all_years(bk, fd)
    flags = DB.dashboard_flags(fd, bk, r, R.red_flags(fd, bk), bk.distress_summary())
    p = AI.build_payload(fd, bk, r, by_year, flags, S.default_weights(), gov, {"cfo_decision": "Pending"}, [],
                         DB.decision(r, R.red_flags(fd, bk)))
    return fd, bk, r, p


def test_payload_contains_only_computed_figures():
    import json
    import math
    fd, bk, r, p = _payload()
    text = json.dumps(p.data)
    assert "NaN" not in text and "Infinity" not in text
    for row in p.data["trends"]:
        item = {"Revenue": "revenue", "PAT": "pat", "EBITDA": "ebitda", "Operating EBITDA": "operating_ebitda",
                "CFO": "cfo"}.get(row["metric"])
        src = fd.get(item, row["year"]) if item else bk.value("dso", row["year"])
        src = None if src is None or (isinstance(src, float) and math.isnan(src)) else round(src, 1)
        assert row["value"] == src, row
    comps = {c.label: c for c in r.components}
    for row in p.data["ratios"]:
        if row["in_ccirs"]:
            c = comps[row["name"]]
            assert row["sub_score"] == (None if c.sub_score is None else round(c.sub_score, 1))
    assert p.data["headline"][0]["value"] == round(r.score, 1)
    for fid, entry in p.registry.items():
        assert all(isinstance(v, (int, float)) and math.isfinite(v) for v in entry["values"]), fid
    assert p.data["rule_based_verdict"]["verdict"] == r.verdict
    assert set(p.data) >= {"company", "headline", "weights_used", "pillars", "ratios", "overrides", "flags",
                           "caveats", "analyst_inputs", "trends", "ccirs_by_year", "peers"}


def test_prompt_guardrails_present():
    sp = AI.system_prompt()
    for must in ("Use ONLY the numbers", "square brackets", "insufficient data", "warrants investigation",
                 "Analyst consideration", "rule-based verdict"):
        assert must in sp
    for s in config.MEMO_SECTIONS:
        assert f"## {s}" in sp


# =================================================================== B reliability parser
REG = AI.Payload({})
for _id, _vals in {"R01": [89.4], "T01": [177094.0], "T02": [-45.0], "S01": [46.2], "P1": [33.2]}.items():
    REG.add(_id, _id, _vals)
REG.add("F01", "flag", [50.0, 20.0], "inventory grew +50.0% vs revenue +20.0%")


@pytest.mark.parametrize("text,status", [
    ("CCE of 89.4% [R01]", AI.VERIFIED),
    ("CCE of 89% [R01]", AI.VERIFIED),                 # rounding within the precision written
    ("CCE of 89.0% [R01]", AI.MISMATCH),               # written to 1dp, so 89.0 != 89.4
    ("CCE of 75.0% [R01]", AI.MISMATCH),
    ("CCE of 89.4%", AI.UNCITED),
    ("CCE of 89.4% [R99]", AI.NOT_IN_PAYLOAD),
    ("revenue of Rs 1,77,094 crore [T01]", AI.VERIFIED),   # Indian commas
    ("revenue of Rs 177,094 crore [T01]", AI.VERIFIED),    # international commas
    ("CFO of −45 crore [T02]", AI.VERIFIED),          # unicode minus
    ("negative CFO of Rs 45 crore [T02]", AI.VERIFIED),    # sign stated in words
    ("CFO of Rs 45 crore [T02]", AI.SIGN_MISMATCH),        # negative source, no sign word nearby
    ("an outflow of 45 crore [T02] in the year", AI.VERIFIED),
    ("CFO was 45 crore [T02], a decline", AI.VERIFIED),       # sign word just after
    ("CFO of +45 crore [T02]", AI.MISMATCH),               # explicit wrong sign
    ("inventory grew 50.0% [F01]", AI.VERIFIED),           # number inside a flag's text
])
def test_reliability_parser_cases(text, status):
    figs = AI.check_memo(text, REG, "Decline").figures
    assert len(figs) == 1 and figs[0]["status"] == status, figs


def test_parser_ignores_years_ids_and_standards():
    nums = AI.parse_numbers("In FY25 and 2024, under Ind AS 116, Z'' and pillar A [R01]:\n1. Check ageing")
    assert nums == []


def test_banned_phrases_detected():
    hits = AI.banned_check("This looks like fraud and the company will default; manipulation proven.")
    assert {h["phrase"] for h in hits} >= {"fraud", "will default", "manipulation proven"}
    assert AI.banned_check("This warrants investigation and is consistent with stress.") == []


@pytest.mark.parametrize("line,verdict,ok", [
    ("Recommendation: Escalate for forensic review", "Escalate for forensic review", True),
    ("Recommendation: Escalate (provisional)", "Escalate for forensic review", True),
    ("Recommendation: Approve with conditions", "Approve with conditions", True),
    ("Recommendation: Approve with conditions", "Approve", False),
    ("Recommendation: Approve", "Escalate for forensic review", False),
    ("Recommendation: Decline\nAnalyst consideration: recommendation: Approve with conditions", "Decline", True),
])
def test_verdict_consistency(line, verdict, ok):
    assert AI.verdict_check(line, verdict)[0] is ok


def test_sections_check():
    memo = "\n".join(f"## {s}\ntext" for s in config.MEMO_SECTIONS[:-1])
    assert AI.sections_check(memo) == ["Data limitations"]


def _good_memo(p, r):
    ids = {row["name"]: row for row in p.data["ratios"]}
    sloan = ids["Sloan accrual ratio"]
    cce = ids["CCE (operating EBITDA)"]
    return f"""## Recommendation
Recommendation: {r.verdict}
CCIRS of {r.score:.1f} [S01] on default weights.
## Executive summary
Cash conversion is weak, with CCE at {cce['value'].replace(' *', '')} [{cce['id']}].
## Key strengths
- Sloan accruals of {sloan['value'].replace(' *', '')} [{sloan['id']}] are low.
## Key risks
- Profit rose while CFO fell; this warrants investigation.
## Anomaly explanations
- Receivables outpaced sales.
## Proposed conditions and covenants
- Minimum CCE covenant.
## Investigate next
- Customer-wise receivables ageing.
## Data limitations
- Data coverage is {p.data['headline'][2]['value']:.0f}% [S03].
"""


def test_full_memo_check_passes_and_highlights_failures():
    fd, bk, r, p = _payload()
    memo = _good_memo(p, r)
    chk = AI.check_memo(memo, p, r.verdict)
    assert chk.passed, (chk.badge, chk.table())
    assert chk.badge.startswith(f"{len(chk.figures)} of {len(chk.figures)} figures verified")
    bad = memo.replace(f"{r.score:.1f} [S01]", "71.0 [S01]") + "\nThis is fraud."
    chk2 = AI.check_memo(bad, p, r.verdict)
    assert not chk2.passed and "1 mismatch" in chk2.badge and "banned" in chk2.badge
    assert "<mark" in chk2.highlighted and "71.0 [S01]" in chk2.highlighted


# =================================================================== B log, exports, API mock
def test_log_writing_and_no_api_key(tmp_path):
    path = tmp_path / "log.csv"
    fake = "sk-" + "ant-" + "abc123XYZ"                 # built at runtime so no key-shaped text sits in the source
    AI.log_call({"company": "X", "mode": "manual", "model": "Claude (claude.ai)", "response": "key " + fake,
                 "weights_used": "Default", "reliability_summary": "1 of 1 figures verified", "api_key": fake},
                path)
    AI.log_call({"company": "Y", "mode": "API"}, path)
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 2 and rows[0]["company"] == "X" and rows[0]["timestamp"]
    assert "api_key" not in rows[0] and "sk-ant" not in path.read_text()
    assert list(rows[0]) == AI.LOG_FIELDS


def test_exports_state_weights_and_check():
    fd, bk, r, p = _payload()
    memo = _good_memo(p, r)
    chk = AI.check_memo(memo, p, r.verdict)
    md = AI.memo_markdown(memo, fd.meta, "FY26", r, S.weights_description(None), chk, "Claude (claude.ai)")
    assert "Weights used: Default CCIRS weights" in md and "AI reliability check" in md
    docx = AI.memo_docx(memo, fd.meta, "FY26", r, S.weights_description(None), chk, "Claude (claude.ai)")
    assert docx[:2] == b"PK"


class FakeClient:
    def __init__(self, text):
        self.calls = 0
        self.text = text
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        assert kw["system"] and kw["messages"][0]["role"] == "user"
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=self.text)], model=kw["model"],
                                     usage=types.SimpleNamespace(input_tokens=1200, output_tokens=650))


def test_call_api_with_fake_client():
    c = FakeClient("## Recommendation\nRecommendation: Decline")
    res = AI.call_api("sys", "user", api_key="test", client=c)
    assert res["text"].startswith("## Recommendation") and res["input_tokens"] == 1200 and c.calls == 1


def test_api_key_lookup(monkeypatch):
    assert AI.find_api_key({}) is None
    assert AI.find_api_key({"ANTHROPIC_API_KEY": "k1"}) == "k1"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k2")
    assert AI.find_api_key(None) == "k2"


# =================================================================== B app: manual + mocked API
def _memo_tab_widgets(at):
    box = next(a for a in at.text_area if str(a.key).startswith("memo_paste_"))
    return box, at.button(key="memo_check")


def test_manual_mode_paste_check_and_log(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_AI_MEMO", True)
    at = _app(monkeypatch)
    assert [r.value for r in at.radio if r.key == "memo_mode"] == ["Manual (copy the prompt into Claude)"]
    assert any("=== SYSTEM PROMPT ===" in c.value for c in at.code)
    fd, bk, r, p = _payload()
    box, btn = _memo_tab_widgets(at)
    box.set_value(_good_memo(p, r)).run()
    at.button(key="memo_check").click().run()
    assert not at.exception
    assert any("AI reliability check:" in s.value and "figures verified" in s.value for s in at.success)
    rows = list(csv.DictReader((tmp_path / "ai_calls.csv").open(encoding="utf-8")))
    assert rows[-1]["mode"] == "manual" and rows[-1]["model"] == "Claude (claude.ai)"
    assert "Default CCIRS weights" in rows[-1]["weights_used"] and rows[-1]["system_prompt"].startswith("You are")


def test_api_mode_mocked_generate_once(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    fd, bk, r, p = _payload()
    calls = {"n": 0}

    def fake_call(system, user, api_key, model=None, client=None):
        calls["n"] += 1
        assert api_key == "test-key-not-real"
        return {"text": _good_memo(p, r), "model": "claude-test", "input_tokens": 10, "output_tokens": 20}
    monkeypatch.setattr(AI, "call_api", fake_call)
    monkeypatch.setattr(config, "ENABLE_AI_MEMO", True)
    at = _app(monkeypatch)
    at.radio(key="memo_mode").set_value("API (Anthropic)").run()
    at.button(key="memo_generate").click().run()
    at.button(key="memo_generate").click().run()                      # same payload -> cached, no second call
    assert not at.exception and calls["n"] == 1
    rows = list(csv.DictReader((tmp_path / "ai_calls.csv").open(encoding="utf-8")))
    assert rows[-1]["mode"] == "API" and rows[-1]["input_tokens"] == "10"
    assert "test-key-not-real" not in (tmp_path / "ai_calls.csv").read_text()
