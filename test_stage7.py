"""Stage 7: A1 pycel fallback, A2 read_excel, A3 backtest analyst inputs, A4 cash one-off flag, A5 utility caveat,
A6 alternative specification, and the calculation workbook."""
import builtins
import io
import warnings

import pandas as pd
import pytest

import backtest as BT
import calcbook as CB
import config
import data as D
import ratios as R
import scoring as S
from test_ratios import FULL, make_fd
from test_stage6 import SET, _company, loader
from wbcheck import PYCEL_SKIP, evaluator_or_skip, static_or_fail

T = pd.Timestamp


# =================================================================== A1 pycel fallback
def test_pycel_unavailable_skips_only_recalculation(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("pycel"):
            raise AttributeError("module 'ast' has no attribute 'Str'")
        return real_import(name, *a, **k)
    fd = D.apply_derivations(D.demo_data())
    bk = R.compute_all(fd)
    raw = CB.build_calc_workbook(fd, bk, S.score(bk, fd, "FY26"))
    static_or_fail(raw, 300)                                   # static checks still run and pass
    path = tmp_path / "c.xlsx"
    path.write_bytes(raw)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(pytest.skip.Exception) as exc:
        evaluator_or_skip(str(path), "Scoring!D3")
    assert PYCEL_SKIP in str(exc.value)


def test_static_checker_catches_bad_references():
    from openpyxl import Workbook
    import handcheck as HC
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "=Missing!B2+1"
    ws["A2"] = "=Sheet!Z999"
    ws["A3"] = "#DIV/0!"
    problems = HC.static_issues(wb)["problems"]
    assert any("missing sheet" in p for p in problems)
    assert any("outside the sheet" in p for p in problems)
    assert any("error literal" in p for p in problems)


# =================================================================== A2 no pandas warning
def test_parse_upload_xlsx_has_no_warning():
    df = D.template_dataframe(n_years=2, last_fy=2026)
    df.loc[df.Key == "revenue", ["FY25", "FY26"]] = [1500, 1800]
    buf = io.BytesIO()
    df.to_excel(buf, sheet_name="Data", index=False)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        res = D.parse_upload(buf.getvalue(), "t.xlsx")
    assert not res.errors and res.data.get("revenue", "FY26") == 1800


# =================================================================== A3 backtest uses analyst inputs
def test_backtest_uses_saved_governance_and_related_party_inputs():
    cos = SET[:1]
    gov = {"reviewed": True, "auditor_event": "None", "opinion": "Unmodified (clean)", "pledge_pct": 0}
    plain = BT.run_batch(cos, loader)[0]
    fy = plain.year
    used = BT.run_batch(cos, loader, inputs={"GOODA.NS": {"governance": gov,
                                                          "manual": {("related_party_advances", fy): 100.0}}})[0]
    assert used.ccirs.coverage > plain.ccirs.coverage
    gov_c = next(c for c in used.ccirs.components if c.key == "governance")
    rp_c = next(c for c in used.ccirs.components if c.key == "rp_advances")
    assert gov_c.sub_score == 100 and rp_c.sub_score is not None
    t1, t2 = BT.results_table([plain]).iloc[0], BT.results_table([used]).iloc[0]
    assert t1["Analyst inputs"] == "not entered" and t2["Analyst inputs"] == "used: governance, related-party"
    assert "Coverage" in t2.index


def test_app_saves_inputs_for_backtest(monkeypatch):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("../app.py", default_timeout=120).run()
    at.sidebar.radio[0].set_value("Demo mode (hypothetical sample)").run()
    at.sidebar.button[0].click().run()
    at.checkbox(key="gov_reviewed").check().run()
    assert not at.exception
    saved = at.session_state["saved_inputs"]
    assert saved["DEMO"]["governance"]["reviewed"] is True


# =================================================================== A4 one-off cash flow flag
def _hcc_like():
    inc = pd.DataFrame({T("2025-03-31"): [5000e7, 600e7, 400e7, 100e7], T("2024-03-31"): [4800e7, 580e7, 380e7, 90e7]},
                       index=["Total Revenue", "EBITDA", "EBIT", "Net Income"])
    cf = pd.DataFrame({T("2025-03-31"): [1668e7], T("2024-03-31"): [300e7]}, index=["Operating Cash Flow"])
    fd = D.build_from_yahoo_frames(inc, None, cf, {"longName": "HCC-like", "industry": "Engineering & Construction"},
                                   "HCCL.NS")
    return D.apply_derivations(fd)


def test_cash_one_off_flag_cce_above_150():
    fd = _hcc_like()
    msgs = D.cash_one_off_checks(fd)
    assert len(msgs) == 1 and msgs[0][0] == "FY25" and "278%" in msgs[0][1] and config.CASH_ONE_OFF_TEXT in msgs[0][1]
    assert "more than 2x" in msgs[0][1]
    assert any(i.check == "Possible one-off cash flow" for i in D.validate(fd).issues)
    bk = R.compute_all(fd)
    assert any(f.code == "one_off_cash" for f in R.red_flags(fd, bk))
    import dashboard as DB
    r = S.score(bk, fd, "FY25")
    assert any(config.CASH_ONE_OFF_TEXT in x["text"] for x in DB.dashboard_flags(fd, bk, r, R.red_flags(fd, bk), []))


def test_cash_one_off_positive_cfo_with_negative_ebitda_and_no_flag_normally():
    fd = D.apply_derivations(make_fd({"FY25": {"cfo": 100, "ebitda": -50}}))
    assert D.cash_one_off_checks(fd)
    assert D.cash_one_off_checks(D.apply_derivations(make_fd({"FY25": {"cfo": 90, "ebitda": 100}}))) == []
    assert D.cash_one_off_checks(D.apply_derivations(make_fd({"FY25": {"cfo": -90, "ebitda": -100}}))) == []


def test_cash_flag_in_backtest_caveats_and_no_score_change():
    def ld(t):
        return _hcc_like() if t == "HCCL.NS" else loader(t)
    cos = [{"sector": "EPC", "role": "stressed", "name": "HCC-like", "ticker": "HCCL.NS"}]
    b = BT.run_batch(cos, lambda t: D.build_from_yahoo_frames(
        pd.DataFrame({T("2025-03-31"): [5000e7, 600e7, 400e7, 100e7], T("2024-03-31"): [4800e7, 580e7, 380e7, 90e7]},
                     index=["Total Revenue", "EBITDA", "EBIT", "Net Income"]), None,
        pd.DataFrame({T("2025-03-31"): [1668e7], T("2024-03-31"): [300e7]}, index=["Operating Cash Flow"]),
        {"longName": "HCC-like"}, t))[0]
    row = BT.results_table([b]).iloc[0]
    assert config.CASH_ONE_OFF_TEXT in row["Caveats"]
    assert b.ccirs.score == pytest.approx(b.ccirs.raw_score)       # flag never changes the score


# =================================================================== A5 regulated utility caveat
@pytest.mark.parametrize("sector,industry,expected", [
    ("Utilities", "Utilities - Regulated Electric", True), ("Industrials", "Utilities - Independent Power Producers", True),
    ("Energy", "Power generation", True), ("Consumer Cyclical", "Auto Manufacturers", False)])
def test_utility_caveat(sector, industry, expected):
    fd = make_fd(FULL, {"sector": sector, "industry": industry})
    r = S.score(R.compute_all(fd), fd, "FY25")
    assert (config.UTILITY_CAVEAT in r.caveats) is expected
    assert r.score == pytest.approx(r.raw_score)


# =================================================================== A6 alternative specification
def test_alt_spec_sets_limited_ei_to_50_and_default_unchanged():
    years = {fy: dict(d) for fy, d in FULL.items()}
    years["FY25"]["pat"] = -50
    fd = make_fd(years)
    bk = R.compute_all(fd)
    default = S.score(bk, fd, "FY25")
    alt = S.score(bk, fd, "FY25", alt_ei_neutral=True)
    ei_d = next(p for p in default.pillars if p.key == "C")
    ei_a = next(p for p in alt.pillars if p.key == "C")
    assert ei_d.limited and ei_a.score == config.ALT_EI_NEUTRAL_SCORE and ei_d.score != config.ALT_EI_NEUTRAL_SCORE
    assert S.score(bk, fd, "FY25").score == pytest.approx(default.score)            # default unaffected
    healthy = S.score(R.compute_all(make_fd(FULL)), make_fd(FULL), "FY25", alt_ei_neutral=True)
    assert healthy.score == pytest.approx(S.score(R.compute_all(make_fd(FULL)), make_fd(FULL), "FY25").score)


def test_alt_spec_table_and_pair_results():
    res = BT.run_batch(SET, loader)
    t = BT.alt_spec_table(res)
    assert {"CCIRS (default)", "CCIRS (alternative)", "EI limited (loss-maker)"} <= set(t.columns)
    stressed = t[t["Company"] == "Bad Auto"].iloc[0]
    assert bool(stressed["EI limited (loss-maker)"]) is True
    dca = BT.direction_check(res, SET, use_alt=True)
    assert set(dca["Result"]) <= {"PASS", "FAIL", "n/a"}
    assert "ALT_SPEC_LABEL" in dir(config) and "not the CCIRS" in config.ALT_SPEC_LABEL


# =================================================================== B2 calculation workbook
def _calc_case(kind="healthy", decisions=None, governance=None):
    fd = D.apply_derivations(_company(f"CALC{kind}.NS", kind))
    bk = R.compute_all(fd)
    r = S.score(bk, fd, "FY25", None, governance, **(decisions or {}))
    return fd, bk, r, CB.build_calc_workbook(fd, bk, r, None, governance, decisions)


@pytest.mark.parametrize("kind,dec,gov", [
    ("healthy", None, None),
    ("stressed", None, {"reviewed": True, "auditor_event": "None", "opinion": "Qualified", "pledge_pct": 30}),
    ("cfo_neg", {"cfo_decision": S.CFO_CONFIRM, "neq_decision": S.NEQ_PENDING}, None)])
def test_calc_workbook_formulas_errors_and_values(kind, dec, gov, tmp_path):
    fd, bk, r, raw = _calc_case(kind, dec, gov)
    wb = static_or_fail(raw, 300)
    assert wb.sheetnames == ["Inputs", "Ratios", "Thresholds", "Weights", "Scoring", "Notes"]
    thr_text = " ".join(str(c.value) for row in wb["Thresholds"].iter_rows() for c in row if c.value)
    assert "PROPOSED - review" in thr_text and "Brief" in thr_text
    path = tmp_path / "calc.xlsx"
    path.write_bytes(raw)
    sc = wb["Scoring"]
    final_row = next(i for i in range(1, sc.max_row + 1) if sc.cell(i, 2).value == "CCIRS (after overrides)")
    xl = evaluator_or_skip(str(path), f"Scoring!D{final_row}")
    assert xl.evaluate(f"Scoring!D{final_row}") == pytest.approx(r.score, abs=0.005)
    checks = []
    for sheet, col in (("Ratios", "G"), ("Scoring", "H"), ("Scoring", "F")):
        ws = wb[sheet]
        for i in range(1, ws.max_row + 1):
            f = ws[f"{col}{i}"].value
            if isinstance(f, str) and f.startswith("=IF(") and "PASS" in f:
                checks.append(str(xl.evaluate(f"{sheet}!{col}{i}")))
    assert len(checks) > 50 and all(c.startswith("PASS") for c in checks), [c for c in checks if not c.startswith("PASS")]
    band_row = final_row + 2
    while sc.cell(band_row, 2).value != "Band":
        band_row += 1
    assert xl.evaluate(f"Scoring!D{band_row}") == r.band


def test_changing_a_weight_changes_the_ccirs(tmp_path):
    fd, bk, r, raw = _calc_case("healthy")
    wb = static_or_fail(raw, 300)
    path = tmp_path / "calc.xlsx"
    path.write_bytes(raw)
    sc, wt = wb["Scoring"], wb["Weights"]
    final_row = next(i for i in range(1, sc.max_row + 1) if sc.cell(i, 2).value == "CCIRS (after overrides)")
    a_row = next(i for i in range(1, wt.max_row + 1) if wt.cell(i, 1).value == "A" and wt.cell(i, 2).value ==
                 "Cash & Liquidity")
    xl = evaluator_or_skip(str(path), f"Scoring!D{final_row}")
    before = xl.evaluate(f"Scoring!D{final_row}")
    xl.set_value(f"Weights!C{a_row}", 60)
    after = xl.evaluate(f"Scoring!D{final_row}")
    w = S.default_weights()
    w["pillars"]["A"] = 60
    assert after != pytest.approx(before)
    assert after == pytest.approx(S.score(bk, fd, "FY25", w).score, abs=0.005)
    check_cell = next(i for i in range(1, wt.max_row + 1) if wt.cell(i, 2).value == "Total / check")
    assert "CHECK" in str(xl.evaluate(f"Weights!D{check_cell}"))          # pillars now sum to 130%


def test_calc_workbook_download_on_score_tab(monkeypatch):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("../app.py", default_timeout=120).run()
    at.sidebar.radio[0].set_value("Demo mode (hypothetical sample)").run()
    at.sidebar.button[0].click().run()
    assert not at.exception
    assert any("calculation workbook" in str(b.label) for b in at.get("download_button"))


# =================================================================== B4 packaging
def test_package_excludes_caches_secrets_and_collects_evidence(tmp_path):
    import importlib.util
    import zipfile
    spec = importlib.util.spec_from_file_location("package", __import__("pathlib").Path(__file__).parents[1] / "tools" / "package.py")
    pkg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pkg)
    root = tmp_path / "proj"
    for rel, text in {"app.py": "print(1)", "config.py": "X = 1", "tests/test_a.py": "def test(): pass",
                      "__pycache__/app.cpython-313.pyc": "x", ".pytest_cache/v": "x", "logs/ai_calls.csv": "timestamp\n1",
                      "memos/maruti_memo.md": "# memo", ".streamlit/secrets.toml": "ANTHROPIC_API_KEY='abc'",
                      "evidence/README.md": "# evidence", ".venv/lib/x.py": "x"}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    out = pkg.build(root, tmp_path / "out.zip")
    names = zipfile.ZipFile(out).namelist()
    assert "ccirs/app.py" in names and "ccirs/tests/test_a.py" in names
    assert "ccirs/evidence/ai_calls.csv" in names and "ccirs/evidence/memos/maruti_memo.md" in names
    assert not any("__pycache__" in n or ".pytest_cache" in n or "secrets.toml" in n or ".venv" in n or
                   n.startswith("ccirs/logs/") for n in names)
    (root / "config.py").write_text("KEY = '" + "sk-" + "ant-" + "abcdefghijklmnop" + "'")
    with pytest.raises(SystemExit, match="possible API key"):
        pkg.build(root, tmp_path / "out2.zip")
