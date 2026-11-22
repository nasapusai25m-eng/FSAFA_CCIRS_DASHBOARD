"""Ratios-tab display changes: summary-card zones, headline table, expander tables, validation wording."""
import pandas as pd
import pytest

import data as D
import ratios as R
from test_ratios import FULL, make_fd


@pytest.fixture(scope="module")
def book():
    return R.compute_all(make_fd(FULL, {"industry": "Auto Manufacturers"}))


# ------------------------------------------------------------------ 1. card zones
@pytest.mark.parametrize("key,v,label,colour", [
    ("beneish_m", -2.5, "No manipulation pattern", "green"), ("beneish_m", -2.0, "Grey zone", "amber"),
    ("beneish_m", -1.5, "Manipulation pattern - warrants investigation", "red"),
    ("altman_z2", 0.9, "Distress", "red"), ("altman_z2", 2.0, "Grey", "amber"), ("altman_z2", 3.0, "Safe", "green"),
    ("altman_z1", 1.0, "Distress", "red"), ("altman_z1", 2.95, "Safe", "green"),
    ("piotroski", 2, "Weak", "red"), ("piotroski", 5, "Average", "amber"), ("piotroski", 8, "Strong", "green"),
])
def test_score_zones(key, v, label, colour):
    assert R.score_zone(key, v) == (label, colour)


def test_score_zone_unavailable_is_grey():
    assert R.score_zone("beneish_m", None) == ("", "grey")


# ------------------------------------------------------------------ 2-4. main table
def test_main_table_headline_rows_only(book):
    main = book.table(main=True)
    names = [r for _, r in main.index]
    for comp in ("GMI", "AQI", "SGI", "DEPI", "SGAI", "TATA", "LVGI"):
        assert not any(n.startswith(comp) for n in names), comp
    assert any(n.startswith("DSRI") for n in names)                 # scored directly by CCIRS pillar C
    assert any(n.startswith("Beneish M-Score") for n in names)
    altman_rows = [n for n in names if n.startswith("Altman")]
    assert altman_rows == ["Altman Z' (manufacturing) - variant used"]
    assert any(n.startswith("Piotroski") for n in names)


def test_main_table_keeps_applicability_and_reason_on_every_row(book):
    main = book.table(main=True, note_year="FY25")
    assert "Applicability" in main.columns and "Reason / note (FY25)" in main.columns
    assert main["Applicability"].isin(["Valid", "Limited", "Not valid"]).all()
    assert (main["Reason / note (FY25)"].str.len() > 1).all()


def test_full_table_still_has_components(book):
    names = [r for _, r in book.table().index]
    assert any(n.startswith("GMI") for n in names) and any(n.startswith("Altman Z''") for n in names)


def test_beneish_components_table_sums_to_mscore(book):
    t = book.beneish_components_table()
    assert list(t.index[:8]) == [R.METRICS[k][0] for k in R.BENEISH_PARTS]
    contrib = t.loc[t.index[:9], "FY25 contribution"].astype(float).sum()        # 8 parts + constant
    assert contrib == pytest.approx(book.value("beneish_m", "FY25"), abs=0.002)
    assert t.at["M-Score (sum)", "FY24 contribution"] == "not computed"          # first year: no prior
    assert t.at[R.METRICS["gmi"][0], "Weight"] == "+0.528"


def test_altman_details_table(book):
    t = book.altman_details_table()
    assert {"X1", "X2", "X3", "X4", "X5"} <= set(t.index)
    assert "Altman Z' (manufacturing)  ◀ used" in t.index and "Altman Z'' (non-manufacturing, 4-factor)" in t.index
    assert float(t.at["X5", "FY25"]) == pytest.approx(1200 / 1400, abs=1e-4)


def test_piotroski_table(book):
    t = book.piotroski_table()
    assert len(t) == 10 and t.index[-1] == "F-Score"
    assert t.at["4. CFO > Net income (quality of earnings)", "FY25"].startswith("Fail")


# ------------------------------------------------------------------ 5. validation wording
BAD = {"FY25": {"revenue": 1000, "total_assets": 1000, "total_liabilities": 400, "equity": 300}}


def _fd(src, fin=False):
    fd = make_fd(BAD, {"is_financial": fin})
    fd.sources.loc[:, :] = fd.sources.where(fd.sources == "", src)
    return fd


def test_yahoo_errors_say_yahoo_data():
    rep = D.validate(_fd(D.SRC_YAHOO))
    err = rep.errors[0]
    assert err.source == "Yahoo data" and "uploaded" not in err.message.lower()
    assert D.ValidationReport.describe_sources(rep.errors) == "Yahoo data"


def test_upload_errors_say_uploaded_data_and_block():
    rep = D.validate(_fd(D.SRC_UPLOAD))
    assert rep.blocking_errors and rep.blocking_errors[0].source == "uploaded data"


def test_financial_company_balance_never_hard_blocks():
    for src in (D.SRC_YAHOO, D.SRC_UPLOAD):
        rep = D.validate(_fd(src, fin=True))
        assert not rep.blocking_errors and rep.overridable_errors
        assert rep.can_score(proceed_anyway=True)
        assert "do not reconcile like industrial ones" in rep.overridable_errors[0].message


def test_yahoo_unit_error_wording():
    fd = make_fd({"FY25": {"revenue": 9e7, "total_assets": 1e8}})
    fd.sources.loc[:, :] = fd.sources.where(fd.sources == "", D.SRC_YAHOO)
    err = [i for i in D.validate(fd).errors if i.check == "Units"][0]
    assert err.source == "Yahoo data" and "entered as crore" not in err.message


# ------------------------------------------------------------------ app smoke test
def test_ratios_tab_renders_cards_in_demo_mode():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    at.sidebar.radio[0].set_value("Demo mode (hypothetical sample)").run()
    at.sidebar.button[0].click().run()
    assert not at.exception
    html_blocks = " ".join(m.value for m in at.markdown)
    for title in ("Beneish M-Score", "Altman Z&#x27; (manufacturing)", "Piotroski F-Score", "CCIRS",
                  "Orange - Escalate for forensic review"):
        assert title in html_blocks, title
    assert "not available" in html_blocks                       # Apex lacks inputs for these scores
    assert not any(e.label == "Beneish M-Score components" for e in at.expander)   # collapsed by default
    at.toggle(key="more_detail").set_value(True).run()
    expanders = [e.label for e in at.expander]
    for lab in ("Beneish M-Score components", "Altman details", "Piotroski criteria"):
        assert lab in expanders
