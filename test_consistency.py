"""One-decimal CCIRS that never contradicts its band, Altman variant labels, merged negative-equity flags,
red-flag wording and the weak-component rule. None of these change a score, band or verdict."""
import sys
from pathlib import Path

import pytest

import config
import dashboard as DB
import data as D
import ratios as R
import scoring as S

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import score_snapshot as SS  # noqa: E402


def _scored(make):
    fd = D.apply_derivations(make())
    book = R.compute_all(fd, {})
    return fd, book, S.score(book, fd, fd.years[-1], S.default_weights(), {"reviewed": False})


# =================================================================== 1 one decimal, consistent with the band
@pytest.mark.parametrize("raw,shown,band", [
    (74.96, "74.9", "Amber"), (74.99, "74.9", "Amber"), (74.94, "74.9", "Amber"), (75.0, "75.0", "Green"),
    (54.96, "54.9", "Orange"), (54.95, "54.9", "Orange"), (55.0, "55.0", "Amber"),
    (34.97, "34.9", "Red"), (94.94, "94.9", "Green"), (95.26, "95.3", "Green"), (47.66, "47.7", "Orange")])
def test_display_score_never_contradicts_band(raw, shown, band):
    assert S.band_for(raw)[0] == band
    assert S.fmt_score(raw) == shown
    assert S.band_for(S.display_score(raw))[0] == band


def test_display_score_whole_range():
    for i in range(0, 100001):
        s = i / 1000
        assert S.band_for(S.display_score(s))[0] == S.band_for(s)[0], s
        assert abs(S.display_score(s) - s) <= 0.1


@pytest.mark.parametrize("raw,text", [
    (74.96, "CCIRS 74.9 is between 55 and 75 -> Approve with conditions"),
    (54.98, "CCIRS 54.9 is between 35 and 55 -> Escalate for forensic review")])
def test_band_rule_just_below_boundaries(raw, text):
    assert DB.band_rule(raw) == text


def test_tables_use_one_decimal():
    fd, book, r = _scored(D.demo_data)
    by_year = S.score_all_years(book, fd)
    assert by_year["CCIRS"].dropna().iloc[-1] == S.display_score(r.score)
    row = DB.company_summary("Apex", D.apply_derivations(D.demo_data()))
    assert row["CCIRS"] == S.display_score(r.score)
    fig = DB.fig_ccirs(by_year)
    assert all("." in t for tr in fig.data if tr.text is not None for t in tr.text)


def test_app_shows_one_decimal_everywhere(monkeypatch):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("../app.py", default_timeout=120).run()
    at.sidebar.radio[0].set_value("Demo mode (hypothetical sample)").run()
    at.sidebar.button[0].click().run()
    assert not at.exception
    _, _, r = _scored(D.demo_data)
    shown = S.fmt_score(r.score)                                       # 46.2
    dash = "\n".join(m.value for m in at.tabs[0].markdown)
    assert f">{shown}<span" in dash                                    # score card
    assert f">{shown}</span> - Orange" in dash                         # what-if live score
    assert f"CCIRS {shown} is between" in dash                         # screening recommendation
    assert f"{shown} / 100" in "\n".join(m.value for m in at.tabs[1].markdown)   # CCIRS tab


# =================================================================== 2 Altman variant label
@pytest.mark.parametrize("make,label", [("Vodafone-Idea-like (synthetic)", "Altman Z'' (non-manufacturing)"),
                                        ("Maruti-like (synthetic)", "Altman Z' (manufacturing)")])
def test_altman_label_names_variant(make, label):
    fd, book, r = _scored(SS.OFFLINE[make])
    bd = r.breakdown_frame()
    assert label in set(bd["Component"])
    assert not bd["Component"].str.contains("variant used").any()
    rec = DB.screening_recommendation(r, R.red_flags(fd, book))
    drv = DB.drivers(r)
    assert "variant used" not in " ".join(rec["evidence"])
    assert all("variant used" not in d["label"] for d in drv["strongest"] + drv["weakest"])


def test_calc_workbook_uses_variant_label():
    import io
    from openpyxl import load_workbook
    import calcbook as CB
    fd, book, r = _scored(SS.OFFLINE["Maruti-like (synthetic)"])
    wb = load_workbook(io.BytesIO(CB.build_calc_workbook(fd, book, r)))
    vals = {c.value for ws in wb for row in ws.iter_rows() for c in row if isinstance(c.value, str)}
    assert "Altman Z' (manufacturing)" in vals and "Altman Z (variant used)" not in vals


# =================================================================== 3 one flag per run of negative-equity years
def test_negative_equity_run_is_one_flag():
    fd, book, r = _scored(SS.OFFLINE["Vodafone-Idea-like (synthetic)"])
    lines = [l for l in book.distress_summary() if "equity negative" in l]
    assert len(lines) == 1
    assert lines[0].startswith(f"{fd.years[0]}-{fd.years[-1]}: shareholders' equity negative")
    for name in ("Debt/Equity", "DuPont ROE", "ROE"):
        assert name in lines[0]
    flags = DB.dashboard_flags(fd, book, r, R.red_flags(fd, book), book.distress_summary())
    assert sum("equity negative" in f["text"] for f in flags) == 1


# =================================================================== 4 red-flag heading depends on the verdict
def test_red_flags_title_for_approve_and_escalate():
    fd, book, r = _scored(SS.OFFLINE["Vodafone-Idea-like (synthetic)"])     # Approve with conditions (pending)
    rec = DB.screening_recommendation(r, R.red_flags(fd, book))
    assert rec["red_flags_title"] == "Red flags to address" and rec["red_flags_note"] == config.RED_FLAGS_NOTE
    fd, book, r = _scored(D.demo_data)                                       # Escalate
    rec = DB.screening_recommendation(r, R.red_flags(fd, book))
    assert rec["red_flags_title"] == "Red flags behind it" and rec["red_flags_note"] == ""


# =================================================================== 5 weak components only
def test_holds_it_back_lists_only_weak_components():
    fd, book, r = _scored(D.demo_data)
    drv = DB.drivers(r)
    assert drv["any_weak"] and all(d["sub_score"] < 60 for d in drv["weakest"])
    assert all(d["sub_score"] >= 60 for d in drv["strongest"])
    ev = DB.screening_recommendation(r, R.red_flags(fd, book))["evidence"]
    assert any(e.startswith("Holds it back:") for e in ev)


def test_no_weak_components_lists_smallest_contributions():
    fd, book, r = _scored(SS.OFFLINE["HUL-like (synthetic)"])
    drv = DB.drivers(r)
    assert not drv["any_weak"] and 1 <= len(drv["weakest"]) <= 3
    lost = sorted((c for c in r.components if c.sub_score is not None), key=lambda c: c.sub_score)
    assert all(c.sub_score >= 60 for c in lost)
    ev = DB.screening_recommendation(r, R.red_flags(fd, book))["evidence"]
    assert any(e.startswith("No weak components. Smallest contributions:") for e in ev)
    assert not any(e.startswith("Holds it back") for e in ev)
