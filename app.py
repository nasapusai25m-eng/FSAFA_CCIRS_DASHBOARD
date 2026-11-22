"""
CCIRS - Credit Cash-Integrity & Resilience Score
Streamlit entry point.  Run:  streamlit run app.py

Tabs: Dashboard | CCIRS score | Ratios | Financial data | Data checks.
config.ENABLE_DEMO, ENABLE_BACKTEST and ENABLE_AI_MEMO switch on the demo company, the Backtest tab and the
AI memo tab (all off for the public deployment). Display only - no flag changes any score.
"""
from __future__ import annotations

import html
import re

import pandas as pd
import streamlit as st

import config
import data as D
import ratios as R
import scoring as S
import dashboard as DB
import ai_memo as AI
import backtest as BT
import handcheck as HC
import calcbook as CB
import os

try:                                           # live suggestions (optional component)
    from streamlit_searchbox import st_searchbox
except Exception:                              # package missing -> Enter-to-search only
    st_searchbox = None
LIVE_SEARCH = (st_searchbox is not None and config.LIVE_SEARCH_DEFAULT
               and os.environ.get("CCIRS_LIVE_SEARCH", "1") != "0")

st.set_page_config(page_title="CCIRS - Credit Risk Screen", page_icon="🏦", layout="wide")

FLAG_ICON = {"red": "🔴", "amber": "🟠", "info": "ℹ️"}
RP_KEYS = ["related_party_revenue", "related_party_receivables", "related_party_advances"]
SEVERITY_ICON = {"error": "⛔", "warning": "⚠️", "info": "ℹ️"}
DEMO_LABEL = "Demo mode (hypothetical sample)"


@st.cache_data(ttl=3600, show_spinner=False)
def cached_yahoo(ticker: str) -> D.FinancialData:
    return D.fetch_yahoo(ticker)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def cached_search(query: str) -> list[dict]:
    # st.text_input only reruns on Enter / focus-out, so this is not hit on every keystroke;
    # the cache stops repeat queries from reaching Yahoo at all.
    return D.search_companies(query)


NO_MATCH_MSG = ("No NSE/BSE listing found for that name, or search failed. Try a different spelling or "
                "try again.")


def _live_options(term: str) -> list[tuple[str, str]]:
    """Search function for the live searchbox: starts after LIVE_SEARCH_MIN_CHARS, uses the 6-hour cache."""
    if len((term or "").strip()) < config.LIVE_SEARCH_MIN_CHARS:
        return []
    try:
        matches = cached_search(term.strip().lower())
    except Exception:
        return []
    return [(f"{m['name']}  ·  {m['exchange']}  ({m['ticker']})", f"{m['ticker']}|{m['name']}") for m in matches]


def _enter_search(prefix: str, label: str) -> tuple[str | None, str | None]:
    query = st.text_input(label, placeholder="e.g. maruti, bajaj auto, larsen & toubro", key=f"{prefix}_query",
                          help="Type a name and press Enter. Only NSE and BSE listings are shown.")
    q = query.strip().lower()
    if not q:
        return None, None
    try:
        matches = cached_search(q)
    except ValueError as e:                  # fewer than 2 characters
        st.caption(str(e))
        return None, None
    except D.SearchError:
        matches = []
    if not matches:
        st.warning(NO_MATCH_MSG)
        return None, None
    if len(matches) > 1:
        st.caption(f"{len(matches)} matches - pick the right company.")
    pick = st.selectbox("Select company", range(len(matches)), key=f"{prefix}_pick",
                        format_func=lambda i: f"{matches[i]['name']}  ·  {matches[i]['exchange']}")
    return matches[pick]["ticker"], matches[pick]["name"]


def company_picker(prefix: str = "main", label: str = "Company name") -> tuple[str | None, str | None]:
    """Name search -> (ticker, display name); the ticker is shown only as small grey text.
    Live suggestions while typing (streamlit-searchbox, 300 ms debounce, from 3 characters) with
    Enter-to-search as the fallback. Reused for peers with a different widget-key prefix."""
    ticker = name = None
    if LIVE_SEARCH:
        picked = st_searchbox(_live_options, placeholder="Start typing a company name...", label=label,
                              key=f"{prefix}_live", debounce=config.LIVE_SEARCH_DEBOUNCE_MS,
                              min_execution_time=0, clear_on_submit=False)
        if picked:
            ticker, name = picked.split("|", 1)
        with st.expander("No suggestions? Search with Enter instead"):
            t2, n2 = _enter_search(prefix, "Company name (press Enter)")
        if t2:
            ticker, name = t2, n2
    else:
        ticker, name = _enter_search(prefix, label)
    if ticker:
        st.markdown(f"<div style='font-size:0.8rem;opacity:0.7;margin-top:-0.4rem'>{html.escape(ticker)}</div>",
                    unsafe_allow_html=True)
    return ticker, name


# ------------------------------------------------------------------ sidebar: inputs
with st.sidebar:
    st.header("1 · Company")
    _modes = ["Search company (Yahoo Finance)"] + ([DEMO_LABEL] if config.ENABLE_DEMO else []) + ["Upload only"]
    mode = st.radio("Load from", _modes,
                    help="Upload only is for unlisted borrowers: fill the template below."
                         + (" Demo mode loads the hypothetical Apex Auto Components case." if config.ENABLE_DEMO
                            else ""))
    ticker = picked_name = None
    if mode.startswith("Search"):
        ticker, picked_name = company_picker()

    st.header("2 · Extra years (optional)")
    st.caption(D.CONSOLIDATED_NOTE)
    c1, c2 = st.columns(2)
    c1.download_button("Template .xlsx", D.make_template_xlsx(), "ccirs_upload_template.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    c2.download_button("Template .csv", D.make_template_csv(), "ccirs_upload_template.csv", mime="text/csv")
    up_file = st.file_uploader("Upload filled template", type=["xlsx", "csv"])
    up_unit = st.selectbox("Units used in the upload", list(config.UPLOAD_UNITS),
                           index=list(config.UPLOAD_UNITS).index(config.DEFAULT_UPLOAD_UNIT))
    prefer = st.radio("On overlapping years, use", ["User upload", "Yahoo"], index=0, horizontal=True)
    if mode == "Upload only":
        up_name = st.text_input("Company name (upload only)", "")
        up_sector = st.text_input("Sector (upload only)", "")
        up_industry = st.text_input("Industry (upload only; drives Altman variant)", "")
        up_is_fin = st.checkbox("This is a bank / NBFC / insurer")

    st.header("3 · Settings")
    include_nc = st.toggle("Include non-current liquid investments (DIR and net debt)",
                           value=config.LIQUIDITY_INCLUDE_NONCURRENT,
                           help="Both DIR versions are always shown; this picks the one that feeds the score "
                                "and the liquid funds deducted in net debt. Default in config.py.")
    year_slot = st.container()                # "Scoring year" - filled after the data is loaded

    load = st.button("Load & validate", type="primary", width="stretch",
                     disabled=mode.startswith("Search") and not ticker)

# ------------------------------------------------------------------ load
if load:
    for k in ("fd", "report", "proceed_anyway", "manual", "score_year", "cfo_decision", "neq_decision", "gov_reviewed",
              "gov_auditor", "gov_opinion", "gov_pledge"):
        st.session_state.pop(k, None)
    yahoo_fd = upload_fd = None
    errors: list[str] = []

    if mode.startswith("Search"):
        with st.spinner("Fetching statements from Yahoo Finance..."):
            try:
                yahoo_fd = cached_yahoo(ticker)
                if not yahoo_fd.meta.get("is_financial") and picked_name:
                    fin, why = D.is_financial_company({"longName": picked_name})
                    if fin:
                        yahoo_fd = D.FinancialData(yahoo_fd.values, yahoo_fd.sources,
                                                   {**yahoo_fd.meta, "is_financial": True,
                                                    "financial_reason": why + " (from the search result)"},
                                                   yahoo_fd.notes)
            except (D.DataFetchError, ValueError) as e:
                errors.append(str(e))
            except Exception as e:  # network / throttling
                errors.append(f"Yahoo fetch failed ({type(e).__name__}: {e}). Try again, or upload the "
                              f"figures with the template.")
    elif config.ENABLE_DEMO and mode == DEMO_LABEL:
        yahoo_fd = D.demo_data()

    if up_file is not None:
        res = D.parse_upload(up_file.getvalue(), up_file.name, up_unit)
        for w in res.warnings:
            st.warning(f"Upload: {w}")
        for e in res.errors:
            st.error(f"Upload: {e}")
        if not res.errors:
            upload_fd = res.data

    if mode == "Upload only":
        if upload_fd is not None:
            upload_fd.meta.update({"is_financial": up_is_fin,
                                   "financial_reason": "marked by the analyst" if up_is_fin else "","name": up_name or "Uploaded company", "sector": up_sector or "Unknown",
                                   "industry": up_industry or "Unknown", "ticker": "-", "currency": "INR",
                                   "price": None, "market_cap_cr": None, "labels_used": {}})

    for e in errors:
        st.error(e)
    if yahoo_fd is None and upload_fd is None:
        st.info("Nothing loaded. Fix the issue above, or upload the figures with the template.")
        st.stop()

    pref_key = "upload" if prefer == "User upload" else "yahoo"
    merged = D.apply_derivations(D.merge_sources(yahoo_fd, upload_fd, pref_key))
    merged.meta.setdefault("fetched_at", pd.Timestamp.now().strftime("%d %b %Y %H:%M"))
    report = D.validate(merged, yahoo=yahoo_fd if mode.startswith("Search") else None, upload=upload_fd)
    st.session_state.update(fd=merged, report=report)

# ------------------------------------------------------------------ display
st.title("🏦 CCIRS · Credit Cash-Integrity & Resilience Score")
st.caption(config.APP_SUBTITLE)

fd: D.FinancialData | None = st.session_state.get("fd")
if fd is None:
    st.info("Search for a company in the sidebar (or choose 'Upload only' for an unlisted borrower) and press "
            "**Load & validate**.")
    st.stop()

report: D.ValidationReport = st.session_state["report"]
base_fd = fd
fd = D.apply_manual(base_fd, st.session_state.get("manual", {}))   # analyst-entered related-party figures
book: R.RatioBook = R.compute_all(fd, {"include_noncurrent": include_nc})
flags = R.red_flags(fd, book)
meta = fd.meta
depth = D.data_depth(fd)

if meta.get("is_financial"):
    st.error(f"🏦 **{config.FINANCIAL_BANNER}**  \n_Detected because: {meta.get('financial_reason', '-')}. "
             f"Ratios tagged 'Not valid' will be excluded from any score, and the score will read "
             f"'Indicative only'._")
if meta.get("is_demo"):
    st.warning("🧪 DEMO MODE - hypothetical sample data (Apex Auto Components). Not a real company.")

# ---------- header (custom HTML so price / market cap never truncate)
badge_bg = {"green": "#1E8E3E", "blue": "#1A73E8", "orange": "#E37400", "red": "#C5221F"}[depth["colour"]]
price = f"Rs {meta['price']:,.2f}" if meta.get("price") else "n/a"
mcap = f"Rs {meta['market_cap_cr']:,.0f} cr" if meta.get("market_cap_cr") else "n/a"


def stat(label, value):
    return (f"<div style='min-width:150px'><div style='font-size:0.78rem;opacity:0.7'>{label}</div>"
            f"<div style='font-size:1.25rem;font-weight:600;white-space:nowrap'>{value}</div></div>")


st.markdown(
    f"""<div style='display:flex;flex-wrap:wrap;gap:28px;align-items:center;margin-bottom:0.6rem'>
    <div style='flex:1 1 280px'>
      <div style='font-size:1.6rem;font-weight:700'>{html.escape(str(meta.get('name', '-')))}</div>
      <div style='font-size:0.8rem;opacity:0.7'>{html.escape(str(meta.get('ticker', '-')))} ·
        {html.escape(str(meta.get('sector', '-')))} / {html.escape(str(meta.get('industry', '-')))}</div>
    </div>
    {stat("Latest fiscal year", meta.get("latest_fy") or "-")}
    {stat("Share price", price)}
    {stat("Market cap", mcap)}
    <div style='background:{badge_bg};color:#fff;padding:10px 16px;border-radius:8px;font-weight:600;
      white-space:nowrap'>Data depth: {depth['badge']}</div>
    </div>""", unsafe_allow_html=True)

# ---------- validation status + Yahoo override
if report.ok_to_score:
    st.success(f"✅ Validation passed: ready for scoring ({len(report.warnings)} warning(s) to review).")
elif report.blocking_errors:
    where = D.ValidationReport.describe_sources(report.blocking_errors)
    checks = ", ".join(sorted({f"{i.check} ({i.year})" for i in report.blocking_errors}))
    st.error(f"⛔ {len(report.blocking_errors)} validation error(s) in {where}: {checks}. Fix these before "
             f"scoring - see the Data checks tab.")
else:
    where = D.ValidationReport.describe_sources(report.overridable_errors)
    why = ("Bank/NBFC/insurer statements on Yahoo do not reconcile like industrial ones."
           if meta.get("is_financial") else "Yahoo data for Indian firms is sometimes mislabelled.")
    st.warning(f"⚠️ Balance sheet in {where} does not reconcile in {len(report.overridable_errors)} year(s) "
               f"(beyond {config.BALANCE_TOLERANCE_ERROR:.0%}). {why}")
    st.checkbox("Proceed anyway with a warning (I will verify against the annual report)",
                key="proceed_anyway")
st.session_state["can_score"] = report.can_score(st.session_state.get("proceed_anyway", False))

# ---------- CCIRS inputs live in session state so the Ratios-tab card and the CCIRS tab agree
_defaults = {"cfo_decision": S.CFO_PENDING, "neq_decision": S.NEQ_PENDING, "peers": [], "gov_reviewed": False, "gov_auditor": "None",
             "gov_opinion": "Unmodified (clean)", "gov_pledge": 0.0}
for _p, _spec in config.PILLARS.items():
    _defaults[f"w_p_{_p}"] = _spec["weight"]
    for _k, _w in _spec["ratios"].items():
        _defaults[f"w_r_{_p}_{_k}"] = _w
for _k, _v in _defaults.items():
    st.session_state.setdefault(_k, _v)


def _weight_keys(group: str) -> dict:
    """group 'P' = pillar weights; a pillar letter = that pillar's sub-weights. item -> session key."""
    if group == "P":
        return {p: f"w_p_{p}" for p in config.PILLARS}
    return {k: f"w_r_{group}_{k}" for k in config.PILLARS[group]["ratios"]}


def _on_weight_change(group: str, item: str):
    """on_change callback: runs BEFORE the rerun, so writing to the sliders' keys here is allowed."""
    keys = _weight_keys(group)
    current = {i: st.session_state[k] for i, k in keys.items()}      # moved slider already holds its new value
    for i, v in S.rebalance(current, item, current[item]).items():
        st.session_state[keys[i]] = v


def _reset_group(group: str):
    defaults = ({p: spec["weight"] for p, spec in config.PILLARS.items()} if group == "P"
                else config.PILLARS[group]["ratios"])
    for i, k in _weight_keys(group).items():
        st.session_state[k] = defaults[i]


def _reset_all_weights():
    _reset_group("P")
    for p in config.PILLARS:
        _reset_group(p)


# every group must start at exactly 100 (whole numbers) - fixed here, before the sliders exist
for _g in ["P", *config.PILLARS]:
    _keys = _weight_keys(_g)
    _vals = {i: st.session_state[k] for i, k in _keys.items()}
    if sum(_vals.values()) != 100 or any(not float(v).is_integer() for v in _vals.values()):
        for _i, _v in S.to_whole_100(_vals).items():
            st.session_state[_keys[_i]] = _v
if st.session_state.get("score_year") not in fd.years:
    st.session_state["score_year"] = fd.years[-1]
weights = {"pillars": {p: st.session_state[f"w_p_{p}"] for p in config.PILLARS},
           "ratios": {p: {k: st.session_state[f"w_r_{p}_{k}"] for k in spec["ratios"]}
                      for p, spec in config.PILLARS.items()}}
governance = {"reviewed": st.session_state["gov_reviewed"], "auditor_event": st.session_state["gov_auditor"],
              "opinion": st.session_state["gov_opinion"], "pledge_pct": st.session_state["gov_pledge"]}
weights_ok = sum(weights["pillars"].values()) > 0 and all(sum(v.values()) > 0 for v in weights["ratios"].values())
decisions = {"cfo_decision": st.session_state["cfo_decision"], "neq_decision": st.session_state["neq_decision"]}
custom_weights = not S.is_default_weights(weights)
by_year = (S.score_all_years(book, fd, weights, governance, **decisions)
           if st.session_state["can_score"] and weights_ok else None)
comparable = dict(zip(by_year["Year"], by_year["Comparable"])) if by_year is not None else {}
with year_slot:
    st.selectbox("Scoring year", list(reversed(fd.years)), key="score_year",
                 format_func=lambda y: y + ("" if comparable.get(y, True) else "  (Indicative)"),
                 help="Applies to every tab. 'Indicative' years have under "
                      f"{config.LOW_COVERAGE_YEAR:.0%} data coverage. {config.LOW_COVERAGE_TOOLTIP}")
year_indicative = not comparable.get(st.session_state["score_year"], True)
ccirs = (S.score(book, fd, st.session_state["score_year"], weights, governance, **decisions)
         if st.session_state["can_score"] and weights_ok else None)
_inputs_store = st.session_state.setdefault("saved_inputs", {})      # per ticker, reused by the Backtest tab
_own = {"manual": dict(st.session_state.get("manual", {})),
        "governance": dict(governance) if governance.get("reviewed") else None}
if _own["manual"] or _own["governance"]:
    _inputs_store[meta.get("ticker")] = _own
else:
    _inputs_store.pop(meta.get("ticker"), None)
_saved = st.session_state.setdefault("saved_decisions", {})
_dkey = (meta.get("ticker"), st.session_state["score_year"])
if any(v != S.CFO_PENDING for v in decisions.values()):
    _saved[_dkey] = dict(decisions)                 # reused by the Backtest tab
else:
    _saved.pop(_dkey, None)

distress_lines = book.distress_summary()
dflags = DB.dashboard_flags(fd, book, ccirs, flags, distress_lines)

TAB_DASH, TAB_SCORE, TAB_RATIOS, TAB_DATA, TAB_CHECKS = ("🏠 Dashboard", "🎯 CCIRS score", "🧮 Ratios",
                                                          "📊 Financial data", "🔎 Data checks")
TAB_MEMO, TAB_BT = "🤖 AI memo", "📈 Backtest"
_tab_names = [TAB_DASH, TAB_SCORE, TAB_RATIOS, TAB_DATA, TAB_CHECKS]
if config.ENABLE_AI_MEMO:                      # research prototype - hidden unless switched on in config.py
    _tab_names.append(TAB_MEMO)
if config.ENABLE_BACKTEST:                     # validation work - hidden for the public deployment
    _tab_names.append(TAB_BT)
_tabs = dict(zip(_tab_names, st.tabs(_tab_names)))
tab_dash, tab_score, tab_ratios, tab_data, tab_checks = (_tabs[n] for n in _tab_names[:5])
tab_memo, tab_bt = _tabs.get(TAB_MEMO), _tabs.get(TAB_BT)

# keep the prompt log tidy even when the AI memo tab is hidden (removes duplicate rows once per session)
if not st.session_state.get("_log_deduped"):
    try:
        AI.dedupe_log()
    except Exception:
        pass
    st.session_state["_log_deduped"] = True

with tab_data:
    legend = (config.SOURCE_LEGEND if not meta.get("is_demo") else
              "Demo mode: all figures are hypothetical (Apex Auto Components). Marked cells: (U) uploaded, "
              "(M) manual entry, (D) derived from accounting identities.")
    st.markdown(f"All amounts in **{meta.get('unit_label', 'Rs crore')}**. {legend} "
                "Blank years read *data unavailable* (never filled with zero).")
    table = fd.display_table()
    st.caption("Year source: " + " · ".join(f"**{fy}**: {s}" for fy, s in fd.year_sources().items()))
    st.dataframe(DB.marked_table(fd), width="stretch", height=min(38 * (len(table) + 1), 1250))
    st.download_button("Download merged data (CSV)", table.to_csv().encode(), "ccirs_merged_data.csv")

ZONE_COLOURS = {"green": ("#1E8E3E", "#E6F4EA"), "amber": ("#B06000", "#FEF3E0"), "orange": ("#C0490F", "#FDEBD0"),
                "red": ("#C5221F", "#FCE8E6"), "grey": ("#5F6368", "#F1F3F4")}
BAND_COLOUR_KEY = {"Green": "green", "Amber": "amber", "Orange": "orange", "Red": "red"}


def score_card(title: str, value: str, zone: str, colour: str, sub: str = "") -> str:
    fg, bg = ZONE_COLOURS[colour]
    return (f"<div style='flex:1 1 200px;background:{bg};border-left:6px solid {fg};border-radius:8px;"
            f"padding:10px 14px;color:#111'>"
            f"<div style='font-size:0.8rem;opacity:0.75'>{html.escape(title)}</div>"
            f"<div style='font-size:1.6rem;font-weight:700;color:{fg}'>{html.escape(value)}</div>"
            f"<div style='font-weight:600'>{html.escape(zone)}</div>"
            f"<div style='font-size:0.75rem;opacity:0.75;margin-top:2px'>{html.escape(sub)}</div></div>")


def card_for(key: str, title: str, fy: str) -> str:
    r = book.get(key, fy)
    if r.applicability == "Not valid":
        return score_card(title, "not valid", "Not valid for this company type", "grey", r.applicability_reason)
    if not r.ok:
        reason = r.reason if len(r.reason) <= 140 else r.reason[:137] + "..."
        return score_card(title, "not available", r.status, "grey", reason)
    zone_key = book.altman_key if key == "altman" else key
    if key == "piotroski" and r.extra:
        e = r.extra
        zone, colour = R.score_zone("piotroski", e["scaled_9"])
        return score_card(title, f"{e['passed']}/{e['assessable']}", zone, colour,
                          f"{e['passed']} of {e['assessable']} assessable criteria passed - score "
                          f"{e['score_0_100']:.0f}/100" + ("" if e["assessable"] == 9 else
                                                         "; missing criteria are not counted as failures"))
    zone, colour = R.score_zone(zone_key, r.value)
    return score_card(title, r.display().replace(" *", ""), zone, colour)


DOT = {"green": "#1E8E3E", "amber": "#E37400", "red": "#C5221F", "grey": "#9AA0A6"}
APP_ICON = {"Valid": "✅", "Limited": "◐", "Not valid": "⛔"}


def main_table_html(rows: list[dict], years: list[str], status_year: str, show_app: bool) -> str:
    cols = len(years) + 2 + (1 if show_app else 0)
    th = "padding:6px 10px;text-align:right;border-bottom:2px solid rgba(128,128,128,0.4);white-space:nowrap"
    head = (f"<tr><th style='{th};text-align:left'>Ratio</th>"
            + ("<th style='" + th + ";text-align:center' title='Applicability for banks/NBFCs/insurers'>App.</th>"
               if show_app else "")
            + "".join(f"<th style='{th}'>{fy}</th>" for fy in years)
            + f"<th style='{th};text-align:left'>Status ({status_year})</th></tr>")
    body, pillar = [], None
    td = "padding:5px 10px;border-bottom:1px solid rgba(128,128,128,0.18)"
    for r in rows:
        if r["pillar"] != pillar:
            pillar = r["pillar"]
            body.append(f"<tr><td colspan='{cols}' style='padding:10px 10px 4px;font-weight:700;"
                        f"font-size:0.95rem;border-bottom:1px solid rgba(128,128,128,0.35)'>{html.escape(pillar)}</td></tr>")
        app = (f"<td style='{td};text-align:center' title='{html.escape(r['applicability'])}: "
               f"{html.escape(r['applicability_reason'])}'>{APP_ICON[r['applicability']]}</td>" if show_app else "")
        if r["pillar"] == "Market perception":
            vals = (f"<td colspan='{len(years)}' style='{td};text-align:right;white-space:nowrap'>"
                    f"{html.escape(r['values'][years[-1]])} <span style='font-size:0.75rem;opacity:0.65'>"
                    f"current price (vs {years[-1]})</span></td>")
        else:
            vals = "".join(f"<td style='{td};text-align:right;white-space:nowrap'>{html.escape(r['values'][fy])}</td>"
                           for fy in years)
        dot = (f"<span style='display:inline-block;width:10px;height:10px;border-radius:50%;"
               f"background:{DOT[r['colour']]};margin-right:6px'></span>")
        body.append(f"<tr><td style='{td}'>{html.escape(r['label'])}</td>{app}{vals}"
                    f"<td style='{td};white-space:nowrap'>{dot}{html.escape(r['status'])}</td></tr>")
    return (f"<table style='border-collapse:collapse;width:100%;font-size:0.92rem'>"
            f"<thead>{head}</thead><tbody>{''.join(body)}</tbody></table>")


def ccirs_card() -> str:
    if ccirs is None:
        why = ("validation errors must be resolved first" if not st.session_state["can_score"]
               else "weights sum to zero")
        return score_card("CCIRS", "blocked", "Scoring not available", "grey", why)
    if ccirs.score is None:
        return score_card("CCIRS", "n/a", "Insufficient data", "grey", "; ".join(ccirs.warnings))
    title = "CCIRS" + (" - Indicative only" if ccirs.indicative_only else "")
    sub = []
    if ccirs.overrides:
        sub.append("Override: " + ccirs.overrides[0])
    if any("awaiting analyst" in p for p in ccirs.pending):
        sub.append("Provisional - CFO flag awaiting your decision (CCIRS tab)")
    sub.append(f"Data coverage {ccirs.coverage:.0%}")
    return score_card(title, S.fmt_score(ccirs.score), f"{ccirs.band} - {ccirs.verdict}",
                      BAND_COLOUR_KEY.get(ccirs.band, "grey"), " · ".join(sub))


def render_weights_panel():
    """Linked weight sliders (single instance - shown on the Dashboard what-if panel)."""
    st.caption("Linked sliders: each group always totals exactly 100%. Moving one slider rescales the others in "
               "proportion (whole numbers; any rounding difference goes to the largest of the others).")
    pc = st.columns(4)
    for c, (p, spec) in zip(pc, config.PILLARS.items()):
        c.slider(f"{p}. {spec['name']}", 0, 100, key=f"w_p_{p}", format="%d%%",
                 on_change=_on_weight_change, args=("P", p))
    st.markdown(f"**Total: {sum(st.session_state[f'w_p_{p}'] for p in config.PILLARS)}%**")
    with st.expander("Sub-weights within each pillar (each pillar totals 100%)"):
        sc = st.columns(4)
        for c, (p, spec) in zip(sc, config.PILLARS.items()):
            c.markdown(f"**{p}. {spec['name']}**")
            for k in spec["ratios"]:
                c.slider(config.COMPONENT_LABELS[k], 0, 100, key=f"w_r_{p}_{k}", format="%d%%",
                         on_change=_on_weight_change, args=(p, k))
            c.markdown(f"Total: {sum(st.session_state[f'w_r_{p}_{k}'] for k in spec['ratios'])}%")
            c.button(f"Reset {p} sub-weights", key=f"reset_sub_{p}", on_click=_reset_group, args=(p,))
    st.button("Reset all weights to the brief's defaults (30/25/25/20 and original sub-weights)",
              key="reset_weights", on_click=_reset_all_weights)



def ratio_expander(key: str, title: str | None = None):
    name, _group, _unit, formula = R.METRICS[key]
    r = book.get(key, card_year)
    with st.expander(f"{title or name} — {card_year}: {r.display()}"):
        st.markdown(f"**Formula:** `{formula}`")
        st.markdown(f"**What it means ({card_year}):** {r.explanation}")
        st.markdown(f"**Applicability:** {APP_ICON[r.applicability]} {r.applicability} - {r.applicability_reason}")
        frame = book.detail_frame(key).drop(columns=["Applicability"])
        st.dataframe(frame, hide_index=True, width="stretch")


def _add_peer(ticker: str, name: str):
    peers = st.session_state["peers"]
    if ticker and ticker != fd.meta.get("ticker") and all(p["ticker"] != ticker for p in peers) \
            and len(peers) < config.MAX_PEERS:
        st.session_state["peers"] = peers + [{"ticker": ticker, "name": name}]


def _remove_peer(ticker: str):
    st.session_state["peers"] = [p for p in st.session_state["peers"] if p["ticker"] != ticker]


SEV_ICON = {"red": "🔴", "amber": "🟠", "info": "ℹ️"}

def _drivers_table(rows: list[dict], gained: bool) -> pd.DataFrame:
    return pd.DataFrame([{"Component": f"{d['label']} ({d['pillar'][0]})", "Value": d["value"], "Band": d["band"],
                          ("Points gained" if gained else "Points lost"):
                              round(d["gained"] if gained else -d["lost"], 1)} for d in rows])


with tab_dash:
    yr = st.session_state["score_year"]
    # ---------- a) score card
    meta_line = (f"{html.escape(str(meta.get('sector', '-')))} · {html.escape(str(meta.get('industry', '-')))}"
                 f" · Scoring year {yr} · data loaded {html.escape(str(meta.get('fetched_at', '-')))}")
    if ccirs is not None and ccirs.score is not None:
        fg, bg = ZONE_COLOURS[BAND_COLOUR_KEY[ccirs.band]]
        tags = [f"data coverage {ccirs.coverage:.0%}"]
        if ccirs.indicative_only:
            tags.insert(0, "<b>Indicative only</b>")
        if any("awaiting" in p for p in ccirs.pending):
            tags.append("<b>provisional</b> - decision pending")
        if year_indicative:
            tags.append(f"<b>{config.LOW_COVERAGE_LABEL}</b>")
        if custom_weights:
            tags.insert(0, f"<b>{config.CUSTOM_WEIGHTS_LABEL}</b>")
        body = (f"<div style='font-size:2.6rem;font-weight:800;color:{fg};line-height:1.1'>{S.fmt_score(ccirs.score)}"
                f"<span style='font-size:1rem;opacity:0.7'> / 100</span></div>"
                f"<div style='font-size:1.15rem;font-weight:700'>{ccirs.band} - {html.escape(ccirs.verdict)}</div>"
                f"<div style='font-size:0.78rem;opacity:0.8;margin-top:4px'>{' · '.join(tags)}</div>")
    else:
        fg, bg = ZONE_COLOURS["grey"]
        body = ("<div style='font-size:1.6rem;font-weight:700'>Score not available</div><div style='font-size:"
                "0.85rem'>" + ("Resolve validation errors (or tick 'Proceed anyway')." if ccirs is None else
                               "Insufficient data: no scoreable ratios remain.") + "</div>")
    if book.is_financial and not (ccirs is not None and ccirs.score is not None):
        body += ("<div style='font-size:0.78rem;margin-top:4px'><b>Indicative only</b> - financial company; "
                 "ratios tagged Not valid are excluded.</div>")
    st.markdown(f"<div style='background:{bg};border-left:8px solid {fg};border-radius:10px;padding:14px 18px;"
                f"color:#111'><div style='font-size:1.25rem;font-weight:700'>"
                f"{html.escape(str(meta.get('name', '-')))}</div><div style='font-size:0.78rem;opacity:0.75;"
                f"margin-bottom:6px'>{meta_line}</div>{body}<div style='font-size:0.78rem;opacity:0.75;"
                f"margin-top:8px'>{html.escape(config.CCIRS_TAGLINE)}</div></div>", unsafe_allow_html=True)
    if custom_weights:
        st.button("Reset to the default CCIRS weights", key="reset_dash", on_click=_reset_all_weights)

    # ---------- b) what is CCIRS
    with st.expander("What is CCIRS?", expanded=True):
        st.markdown("\n".join(f"- {line}" for line in config.WHAT_IS_CCIRS))

    # ---------- c) pillar gauges, then what drove the score
    gcols = st.columns(4)
    for col, p in zip(gcols, config.PILLARS):
        ps = next((x for x in ccirs.pillars if x.key == p), None) if ccirs is not None else None
        title = f"{p}. {config.PILLARS[p]['name']}" + (" (limited)" if ps is not None and ps.limited else "")
        col.plotly_chart(DB.fig_gauge(None if ps is None else ps.score, title), width="stretch",
                         config={"displayModeBar": False}, key=f"gauge_{p}")
        if ps is not None and ps.limited:
            col.caption(ps.note)
    if ccirs is not None and ccirs.score is not None:
        drv = DB.drivers(ccirs)
        st.markdown("#### What drove the score")
        st.caption("From the score breakdown (CCIRS score tab). Points gained = the component's contribution to the "
                   "CCIRS; points lost = how far it fell short of a perfect sub-score, in CCIRS points. "
                   "Letter = pillar (A Cash & Liquidity, B Solvency, C Earnings Integrity, D Distress & Governance).")
        dl, dr = st.columns(2)
        with dl:
            st.markdown("**Strongest components**")
            if drv["strongest"]:
                st.dataframe(_drivers_table(drv["strongest"], True), hide_index=True, width="stretch")
            else:
                st.caption("No component scored 50 or more.")
        with dr:
            st.markdown("**Weakest components**" if drv["any_weak"] else "**Smallest contributions**")
            if not drv["any_weak"]:
                st.caption("No weak components (none below a sub-score of "
                           f"{config.WEAK_SUB_SCORE}); these lost the most points.")
            if drv["weakest"]:
                st.dataframe(_drivers_table(drv["weakest"], False), hide_index=True, width="stretch")
            else:
                st.caption("Every component is at its maximum sub-score.")

    # ---------- d) red flags, then trend charts
    with st.container(border=True):
        st.markdown("**Red flags**" + (f" ({len(dflags)})" if dflags else ""))
        if not dflags:
            st.markdown("✅ No flags raised by the automatic checks.")
        show_all = len(dflags) > config.DASHBOARD_MAX_FLAGS and st.toggle(f"Show all {len(dflags)}",
                                                                          key="show_all_flags")
        for f in (dflags if show_all else dflags[: config.DASHBOARD_MAX_FLAGS]):
            st.markdown(f"{SEV_ICON[f['severity']]} {f['text']}")

    c1, c2, c3 = st.columns(3)
    c1.plotly_chart(DB.fig_cash(fd), width="stretch", config={"displayModeBar": False}, key="chart_cash")
    c2.plotly_chart(DB.fig_dso(book), width="stretch", config={"displayModeBar": False}, key="chart_dso")
    if by_year is not None:
        c3.plotly_chart(DB.fig_ccirs(by_year), width="stretch", config={"displayModeBar": False},
                        key="chart_ccirs")
        if (~by_year["Comparable"]).any():
            c3.caption(f"Hollow grey = {config.LOW_COVERAGE_LABEL}. {config.LOW_COVERAGE_TOOLTIP}")
    else:
        c3.info("CCIRS by year appears once scoring is allowed.")

    # ---------- e) decision last: screening recommendation with the evidence
    with st.container(border=True):
        st.markdown("### Screening recommendation")
        st.caption("Screening aid for the credit analyst, not a credit decision.")
        if ccirs is not None and ccirs.score is not None:
            rec = DB.screening_recommendation(ccirs, flags)
            fgr, _ = ZONE_COLOURS[BAND_COLOUR_KEY[ccirs.band]]
            st.markdown(f"<div style='font-size:1.4rem;font-weight:800;color:{fgr}'>{html.escape(rec['recommendation'])}"
                        f"</div>", unsafe_allow_html=True)
            st.markdown("**Why**\n" + "\n".join(f"- {e}" for e in rec["evidence"]))
            if rec["overrides"]:
                st.markdown("**Overrides**\n" + "\n".join(f"- {o}" for o in rec["overrides"]))
            else:
                st.markdown("**Overrides:** none triggered.")
            if rec["red_flags"]:
                st.markdown(f"**{rec['red_flags_title']}**\n" + "\n".join(f"- {r}" for r in rec["red_flags"]))
                if rec["red_flags_note"]:
                    st.caption(rec["red_flags_note"])
            if rec["caveats"]:
                st.markdown("**Caveats**\n" + "\n".join(f"- {c}" for c in rec["caveats"]))
            st.markdown("**Suggested covenants**\n" + "\n".join(f"- {c}" for c in rec["covenants"]))
            st.markdown("**Investigate next**\n" + "\n".join(
                f"- {'**' + i + '** (priority)' if pr else i}" for i, pr in rec["investigate"]))
        else:
            st.info("No recommendation until a score is available.")

    # ---------- f) what-if weights and peers, below the decision
    with st.container(border=True):
        st.markdown("**What-if: weights**")
        if ccirs is not None and ccirs.score is not None:
            fgw, _ = ZONE_COLOURS[BAND_COLOUR_KEY[ccirs.band]]
            st.markdown(f"Live score: <span style='font-size:1.5rem;font-weight:800;color:{fgw}'>"
                        f"{S.fmt_score(ccirs.score)}</span> - {ccirs.band}, {html.escape(ccirs.verdict)}",
                        unsafe_allow_html=True)
        render_weights_panel()


    # ---------- peer comparison
    with st.container(border=True):
        st.markdown(f"**Peer comparison** (up to {config.MAX_PEERS} peers, latest year, same weights)")
        pc1, pc2 = st.columns([2, 1])
        with pc1:
            p_ticker, p_name = company_picker("peer", "Add a peer by name")
        with pc2:
            st.write("")
            st.button("Add peer", key="add_peer", on_click=_add_peer, args=(p_ticker, p_name),
                      disabled=not p_ticker or len(st.session_state["peers"]) >= config.MAX_PEERS)
        peers = st.session_state["peers"]
        if peers:
            rc = st.columns(len(peers))
            for c, p in zip(rc, peers):
                c.button(f"✕ {p['name'][:28]}", key=f"rm_{p['ticker']}", on_click=_remove_peer, args=(p["ticker"],))
            main_row = DB.company_summary(meta.get("name", "This company"), fd, weights, {"include_noncurrent": include_nc},
                                          governance, fy=yr, **decisions) if st.session_state["can_score"] else None
            with st.spinner("Loading peers..."):
                rows = DB.peer_rows(peers, cached_yahoo, weights, {"include_noncurrent": include_nc})
            st.session_state["peer_rows"] = rows
            for r in rows:
                if r.get("error"):
                    st.warning(f"Peer not loaded - {r['error']}. The rest of the comparison is unaffected.")
            all_rows = ([main_row] if main_row else []) + rows
            tbl = DB.peer_table(all_rows)
            if len(tbl):
                st.dataframe(tbl, hide_index=True, width="stretch")
                st.plotly_chart(DB.fig_radar(all_rows), width="stretch", config={"displayModeBar": False},
                                key="chart_radar")
                st.caption("Why the same company can score differently here: peers are scored with DEFAULT "
                           "analyst inputs (governance not reviewed, so that component is re-weighted out; "
                           "overrides pending), while the first row uses the inputs you entered for this company. "
                           "That is why, for example, Maruti can show 93.4 as a peer and 94.0 on its own page. ")
                st.caption("Peers are scored on their latest year with default analyst inputs (governance not "
                           "reviewed, overrides pending), so treat their scores as provisional.")
        else:
            st.session_state["peer_rows"] = []
            st.caption("Search for a peer above and press 'Add peer'.")

    # ---------- 7. footer
    st.divider()
    src_line = " · ".join(f"{fy}: {s}" for fy, s in fd.year_sources().items())
    warn = report.warnings + report.overridable_errors
    st.caption(f"**Sources by year:** {src_line}  \n**Last refresh:** {meta.get('fetched_at', '-')}  \n"
               f"**Data quality:** {len(warn)} warning(s)"
               + (" - " + "; ".join(f"{i.check} ({i.year})" for i in warn[:4]) + (" ..." if len(warn) > 4 else "")
                  if warn else "") + " - see the Data checks tab.  \n"
               "**Disclaimer:** screening tool, not proof of manipulation or default; not valid for banks, NBFCs and "
               "insurers; weights are judgement-based; for academic purposes only - not investment or lending advice.")

with tab_ratios:
    if flags or distress_lines:
        with st.container(border=True):
            st.markdown("**Automatic flags**")
            for line in distress_lines:
                st.markdown(f"🔴 {line}")
            for f in flags:
                st.markdown(f"{FLAG_ICON[f.severity]} {f.text}" + ("  _(decide on the CCIRS score tab)_"
                                                                   if f.confirmable else ""))
    if not st.session_state["can_score"]:
        st.info("Ratios are shown for review; scoring stays blocked until validation errors are resolved.")

    # ---------- score cards
    card_year = st.session_state["score_year"]
    st.markdown(f"#### Scores - {card_year}" + ("  (Indicative)" if year_indicative else ""))
    st.caption("Change the year with 'Scoring year' in the sidebar (applies to every tab).")
    cards = [
        card_for("beneish_m", "Beneish M-Score", card_year),
        card_for("altman", f"Altman {R.ALTMAN_SHORT[book.altman_key]}", card_year),
        card_for("piotroski", "Piotroski F-Score", card_year),
        ccirs_card(),
    ]
    st.markdown(f"<div style='display:flex;flex-wrap:wrap;gap:12px;margin-bottom:14px'>{''.join(cards)}</div>",
                unsafe_allow_html=True)

    # ---------- two always-visible blocks
    for blk in config.MAIN_BLOCKS:
        st.markdown(f"#### {blk['title']}")
        st.caption(blk["caption"])
        st.markdown(main_table_html(book.main_rows(card_year, blk["title"]), fd.years, card_year,
                                    show_app=book.is_financial), unsafe_allow_html=True)
        if blk is config.MAIN_BLOCKS[-1]:
            st.caption("Performance and valuation context: shown for context, not part of the CCIRS score. "
                       "Market perception and growth-and-payout use neutral grey dots because valuation is not a "
                       "credit-risk signal. Valuation uses the current price and market cap against the latest "
                       "fiscal year only.")
    st.caption("Status dots use config.SCORE_THRESHOLDS (the table the CCIRS score will use); bands for the context "
               "ratios are marked PROPOSED - review. 'no data' = an input is missing; '*' = see More detail. "
               + ("App.: ✅ valid · ◐ limited · ⛔ not valid for a financial company - hover for the reason. "
                  if book.is_financial else "")
               + ("DIR, net debt and ROIC include non-current liquid investments (sidebar setting)." if include_nc
                  else "DIR, net debt and ROIC exclude non-current liquid investments (sidebar setting)."))
    st.download_button("Download full ratio table (CSV)", book.table().to_csv().encode(), "ccirs_ratios.csv")

    # ---------- details on demand
    if st.toggle("More detail", value=False, key="more_detail",
                 help="Formula, inputs by year, interpretation and applicability for every ratio, plus the "
                      "Beneish, Altman and Piotroski breakdowns."):
        st.markdown("##### Table ratios - formula and inputs")
        main_keys = [k for _, ks in config.MAIN_TABLE for k in ks]
        for k in main_keys:
            ratio_expander(k, config.MAIN_TABLE_LABELS[k])
        st.markdown("##### Supporting and secondary ratios")
        for k in book.detail_keys():
            if k not in main_keys:
                ratio_expander(k)
        st.markdown("##### Return decomposition")
        for k in ("dupont", "dupont_mod", "roiic"):
            ratio_expander(k)
        st.markdown("##### Score breakdowns")
        with st.expander("Beneish M-Score components"):
            m = book.get("beneish_m", card_year)
            st.markdown(f"**M-Score {card_year}:** {m.display()} - {m.explanation}")
            st.markdown("Contribution = weight x component value. M-Score = constant + sum of contributions; "
                        "reported only when all eight components compute.")
            st.dataframe(book.beneish_components_table(), width="stretch")
            st.markdown("**Component formulas**\n" + "\n".join(
                f"- {R.METRICS[k][0]}: `{R.METRICS[k][3]}`" for k in R.BENEISH_PARTS))
        with st.expander("Altman details"):
            st.markdown(f"{book.altman_reason}  \n{R.ALTMAN_X_TEXT}.  \nZones - Z'': safe > 2.60, grey 1.10-2.60, "
                        f"distress < 1.10. Z': safe > 2.90, grey 1.23-2.90, distress < 1.23.")
            st.dataframe(book.altman_details_table(), width="stretch")
        with st.expander("Piotroski criteria"):
            st.caption("ROA and asset turnover use year-end total assets so the score works with 2 years.")
            st.dataframe(book.piotroski_table(), width="stretch")

with tab_score:
    yr = st.session_state["score_year"]
    st.caption(f"Scoring year: **{yr}**{' (Indicative - limited data)' if year_indicative else ''} - change it in "
               f"the sidebar. Profitability, efficiency and market perception are context only and are not part "
               f"of the score.")
    scored = ccirs is not None and ccirs.score is not None
    # ---------- 1. score card and pillar scores
    if ccirs is None:
        st.warning("Scoring is blocked: " + ("resolve the validation errors (or tick 'Proceed anyway' where "
                                             "allowed)." if not st.session_state["can_score"] else
                                             "every weight in a group is zero."))
    elif ccirs.score is None:
        st.warning("Insufficient data to score. " + " ".join(ccirs.warnings))
    else:
        if custom_weights:
            cw1, cw2 = st.columns([3, 1])
            cw1.warning(f"**{config.CUSTOM_WEIGHTS_LABEL}.** {S.weights_description(weights)}")
            cw2.button("Reset to default weights", key="reset_score_tab", on_click=_reset_all_weights)
        fg, bg = ZONE_COLOURS[BAND_COLOUR_KEY[ccirs.band]]
        st.markdown(
            f"<div style='background:{bg};border-left:8px solid {fg};border-radius:8px;padding:14px 18px;color:#111'>"
            f"<div style='font-size:0.85rem;opacity:0.75'>CCIRS {yr}"
            f"{' - INDICATIVE ONLY (financial company)' if ccirs.indicative_only else ''}</div>"
            f"<div style='font-size:2.4rem;font-weight:800;color:{fg}'>{S.fmt_score(ccirs.score)} / 100</div>"
            f"<div style='font-size:1.1rem;font-weight:700'>{ccirs.band} - {html.escape(ccirs.verdict)}</div>"
            f"<div style='font-size:0.8rem;opacity:0.75'>Weighted score before overrides {ccirs.raw_score:.1f} · "
            f"data coverage {ccirs.coverage:.0%}</div></div>", unsafe_allow_html=True)
        for w in ccirs.warnings:
            st.info(w)
        for c in ccirs.caveats:
            st.caption(f"⚠️ Caveat: {c}")
        cols = st.columns(len(ccirs.pillars))
        for c, ps in zip(cols, ccirs.pillars):
            c.metric(f"{ps.key}. {ps.name}" + (" (limited)" if ps.limited else ""),
                     "n/a" if ps.score is None else f"{ps.score:.0f}",
                     help=f"Weight {ps.eff_weight:.1f}% after re-weighting (base {ps.base_weight:.1f}%); "
                          f"{ps.coverage:.0%} of the pillar's sub-weight had data."
                          + (f" Limited: {ps.note}." if ps.limited else ""))
            if ps.limited:
                c.caption(f"Limited - {ps.note}")

        # ---------- 2. how the score was built
        st.markdown("#### How the score was built")
        st.caption("Contribution = sub-score x weight in pillar x pillar weight. Contributions add up to the "
                   "weighted score before overrides. Unavailable ratios are re-weighted within their pillar; "
                   "distress 'not meaningful' scores 0.")
        bd = ccirs.breakdown_frame()
        st.dataframe(bd, hide_index=True, width="stretch",
                     column_config={"Contribution (points)": st.column_config.ProgressColumn(
                         min_value=0, max_value=max(30.0, float(bd["Contribution (points)"].max())), format="%.2f"),
                         "Note": st.column_config.TextColumn(width="large")})

    # ---------- 3. overrides
    st.markdown("#### Overrides")
    st.caption("Overrides act after the weighted score. The two below need your decision because a valid business "
               "reason can explain the trigger.")
    run = S.cfo_negative_run(fd, yr)
    if run:
        st.radio(f"CFO negative in {run[0]}-{run[-1]} ({len(run)} consecutive years). Apply the cap of "
                 f"{config.CFO_NEGATIVE_CAP}?", [S.CFO_PENDING, S.CFO_CONFIRM, S.CFO_DISMISS], key="cfo_decision",
                 horizontal=True, help="Dismiss when a captive finance arm's loan-book growth explains the negative "
                                       "consolidated CFO (see the automatic flags).")
        st.caption("Why it matters: the business has consumed cash for two years running, so a working-capital "
                   "line may end up funding losses. Dismiss only if a captive finance arm's loan growth explains it.")
    else:
        st.caption(f"CFO override: not triggered ({config.CFO_NEGATIVE_YEARS} consecutive negative-CFO years ending "
                   f"{yr} are needed).")
    eq_now = fd.get("equity", yr)
    if eq_now == eq_now and eq_now < 0:
        st.radio(f"Negative net worth in {yr} (equity Rs {eq_now:,.0f} cr). Cap the CCIRS at {config.NEG_EQUITY_CAP} "
                 f"(Red, Decline)?", [S.NEQ_PENDING, S.NEQ_CONFIRM, S.NEQ_DISMISS], key="neq_decision",
                 horizontal=True)
        st.caption("Why it matters: liabilities exceed assets, so there is no equity cushion to absorb losses "
                   "before the lender. Dismiss only with evidence of a committed capital infusion or restructuring.")
    else:
        st.caption("Negative-net-worth override: not triggered (equity is positive or unavailable).")
    if scored:
        for o in ccirs.overrides:
            st.error(f"Override applied: {o}")
        for p in ccirs.pending:
            st.warning(p)
    st.caption("Automatic rules (no decision needed): a qualified / adverse / disclaimer audit opinion, a missing "
               "audit report or an auditor resigning over disagreement caps the score at 50; related-party "
               "advances above 50% of revenue force 'Escalate'. Enter these below.")

    # ---------- 4. analyst inputs (related party + auditor / opinion / pledge)
    with st.expander(config.ANALYST_INPUTS_TITLE, expanded=False):
        st.markdown(config.ANALYST_INPUTS_WHY)
        st.markdown("**Related-party figures (Rs crore)** - from the annual report's related-party note")
        st.caption("Leave blank if not known. Values typed here override Yahoo/upload and are marked (M). "
                   "Each feeds a separate signal: RP revenue / revenue, RP receivables / receivables, "
                   "RP advances / revenue.")
        seed = pd.DataFrame({fy: [base_fd.get(k, fy) for k in RP_KEYS] for fy in fd.years},
                            index=[D.ITEM_LABELS[k] for k in RP_KEYS])
        for (k, fy), v in st.session_state.get("manual", {}).items():
            seed.at[D.ITEM_LABELS[k], fy] = v
        edited = st.data_editor(seed, width="stretch", key="rp_editor")
        if st.button("Apply related-party inputs", key="rp_apply"):
            manual = {}
            for k in RP_KEYS:
                for fy in fd.years:
                    v = edited.at[D.ITEM_LABELS[k], fy]
                    if pd.notna(v) and (pd.isna(base_fd.get(k, fy)) or float(v) != base_fd.get(k, fy)):
                        manual[(k, fy)] = float(v)
            st.session_state["manual"] = manual
            st.rerun()
        st.markdown("**Auditor and promoter flags** - from the auditor's report and the shareholding pattern")
        g1, g2, g3 = st.columns(3)
        g1.selectbox("Auditor change", list(config.AUDITOR_EVENTS), key="gov_auditor",
                     help="Routine rotation does NOT trigger the override; a resignation citing disagreement does.")
        g2.selectbox("Audit opinion", list(config.AUDIT_OPINIONS), key="gov_opinion")
        g3.number_input("Promoter pledge (% of promoter holding)", 0.0, 100.0, step=1.0, key="gov_pledge")
        st.checkbox("I have reviewed these for the scoring year (otherwise the governance component is "
                    "re-weighted out)", key="gov_reviewed")

    # ---------- 5. downloads
    if scored:
        st.markdown("#### Downloads")
        dl1, dl2 = st.columns(2)
        dl1.download_button("Download score breakdown (CSV)", ccirs.breakdown_csv(weights),
                            f"ccirs_breakdown_{yr}.csv")
        dl2.download_button("Download calculation workbook (.xlsx, live formulas)",
                            CB.build_calc_workbook(fd, book, ccirs, weights, governance, decisions,
                                                   {"include_noncurrent": include_nc}),
                            f"ccirs_calculation_{meta.get('ticker')}_{yr}.xlsx", key="dl_calcbook",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            help="Inputs, Ratios, Thresholds, Weights and Scoring sheets with live Excel formulas and "
                                 "a PASS/FAIL check against the app for every value.")

    # ---------- 6. technical detail (collapsed)
    st.markdown("#### Technical detail")
    with st.expander("Weights and sub-weights"):
        wt = pd.DataFrame([{"Pillar": f"{p}. {spec['name']}", "Pillar weight %": st.session_state[f"w_p_{p}"],
                            "Sub-weights %": ", ".join(f"{config.COMPONENT_LABELS[k]} "
                                                       f"{st.session_state[f'w_r_{p}_{k}']}" for k in spec["ratios"])}
                           for p, spec in config.PILLARS.items()])
        st.dataframe(wt, hide_index=True, width="stretch")
        st.caption(f"Weights used: {S.weights_description(weights)}. Change them in the **What-if** panel on the "
                   "Dashboard tab (one set of linked sliders drives every score in the app).")
    if scored:
        with st.expander(f"Sensitivity scenarios (each weight ±{config.SENSITIVITY_STEP:.0%})"):
            sens, summ = S.sensitivity(book, fd, yr, weights, governance, st.session_state["cfo_decision"],
                                       neq_decision=st.session_state["neq_decision"])
            s1, s2, s3 = st.columns(3)
            s1.metric("Score range", f"{summ['min']:.1f} - {summ['max']:.1f}", help=f"Base {summ['base']:.1f}")
            s2.metric("Verdict unchanged", f"{summ['verdict_stable_pct']:.0f}% of {summ['scenarios']} scenarios")
            s3.metric("Verdict flips", len(summ["flips"]))
            if summ["flips"]:
                st.warning("Verdict changes when: " + "; ".join(summ["flips"]) + ". The score sits close to a band "
                           "boundary - treat the verdict as judgement-sensitive.")
            else:
                st.success("The verdict is stable to ±10% changes in every weight.")
            if weights != S.default_weights():
                _, dsum = S.sensitivity(book, fd, yr, S.default_weights(), governance,
                                        st.session_state["cfo_decision"], neq_decision=st.session_state["neq_decision"])
                st.caption(f"Sensitivity above uses your current weights. Around the default weights: score "
                           f"{dsum['base']:.1f} ({dsum['base_verdict']}), range {dsum['min']:.1f}-{dsum['max']:.1f}, "
                           f"verdict unchanged in {dsum['verdict_stable_pct']:.0f}% of {dsum['scenarios']} scenarios.")
            st.dataframe(sens, hide_index=True, width="stretch")
        with st.expander("CCIRS by year (same weights and analyst inputs)"):
            st.dataframe(S.score_all_years(book, fd, weights, governance, **decisions), hide_index=True,
                         width="stretch", column_config={"Comparable": st.column_config.CheckboxColumn(
                             help=config.LOW_COVERAGE_TOOLTIP), "Band": st.column_config.TextColumn(
                             help=config.LOW_COVERAGE_TOOLTIP)})
            st.caption(f"Years below {config.LOW_COVERAGE_YEAR:.0%} coverage: {config.LOW_COVERAGE_LABEL}. "
                       f"{config.LOW_COVERAGE_TOOLTIP}")
    with st.expander(f"Altman details ({R.ALTMAN_SHORT[book.altman_key]})"):
        st.markdown(f"{book.altman_reason}  \n{R.ALTMAN_X_TEXT}.  \nZones - Z'': safe > 2.60, grey 1.10-2.60, "
                    f"distress < 1.10. Z': safe > 2.90, grey 1.23-2.90, distress < 1.23.")
        st.dataframe(book.altman_details_table(), width="stretch")


if tab_memo is not None:
  with tab_memo:
    st.warning(config.AI_MEMO_BANNER)
    st.markdown("#### AI credit memo")
    st.caption("Drafted from the computed figures only, for the credit approver. Every number must cite a figure ID; "
               "the reliability check below re-verifies each one against the payload. Screening aid - not a credit "
               "decision.")
    if ccirs is None or ccirs.score is None:
        st.info("The memo needs a CCIRS score - resolve validation errors or data gaps first.")
    else:
        dec_box = DB.decision(ccirs, flags)
        payload = AI.build_payload(fd, book, ccirs, by_year, dflags, weights, governance, decisions,
                                   st.session_state.get("peer_rows") or [], dec_box)
        sys_p, usr_p = AI.system_prompt(), AI.user_prompt(payload)
        phash = AI.payload_hash(sys_p, usr_p)
        try:
            _secrets = st.secrets
        except Exception:
            _secrets = None
        api_key = AI.find_api_key(_secrets)
        modes = ["Manual (copy the prompt into Claude)"] + (["API (Anthropic)"] if api_key else [])
        mode = st.radio("Mode", modes, horizontal=True, key="memo_mode")
        if not api_key:
            st.caption("API mode is off: no ANTHROPIC_API_KEY in the environment or st.secrets. Manual mode needs "
                       "no key.")
        st.caption(f"Weights used: {S.weights_description(weights)}"
                   + (f"  ·  **{config.CUSTOM_WEIGHTS_LABEL}**" if custom_weights else ""))

        memo_key = f"{meta.get('ticker')}_{ccirs.year}_{phash}"     # box + results tied to company/year/inputs
        memos = st.session_state.setdefault("memos", {})

        def _save(text: str, mode_label: str, model: str, tokens=(None, None)):
            chk = AI.check_memo(text, payload, ccirs.verdict)
            AI.log_call({"company": meta.get("name"), "ticker": meta.get("ticker"), "scoring_year": ccirs.year,
                         "mode": mode_label, "model": model, "system_prompt": sys_p, "user_prompt": usr_p,
                         "response": text, "input_tokens": tokens[0], "output_tokens": tokens[1],
                         "weights_used": S.weights_description(weights), "reliability_summary": chk.badge,
                         "payload_hash": phash})
            memos[memo_key] = {"text": text, "hash": phash, "model": model, "mode": mode_label}

        if mode.startswith("Manual"):
            full = AI.full_prompt_text(sys_p, usr_p)
            st.markdown("**1. Copy the prompt** (copy icon at the top right of the box) and paste it into Claude.")
            with st.expander(f"Prompt - system prompt + JSON payload ({len(payload.registry)} figure IDs)",
                             expanded=False):
                st.code(full, language="text")
            st.download_button("Download prompt (.txt)", full.encode(), f"ccirs_prompt_{meta.get('ticker')}_"
                               f"{ccirs.year}.txt", key="dl_prompt")
            st.markdown("**2. Paste Claude's response** and press Check and save.")
            model_name = st.text_input("Model used", value=config.AI_MANUAL_MODEL_DEFAULT, key="memo_model")
            pasted = st.text_area("Response", height=220, key=f"memo_paste_{memo_key}",
                                  placeholder="Paste the memo here (markdown)...")
            if st.button("Check and save", type="primary", key="memo_check", disabled=not pasted.strip()):
                _save(pasted.strip(), "manual", model_name or config.AI_MANUAL_MODEL_DEFAULT)
        else:
            cache = st.session_state.setdefault("memo_api_cache", {})
            st.caption(f"Model: {config.AI_MODEL} (config.py). The API is called only when you press the button; "
                       f"identical inputs reuse the cached response.")
            if st.button("Generate memo", type="primary", key="memo_generate"):
                if phash not in cache:
                    with st.spinner("Calling the API..."):
                        try:
                            cache[phash] = AI.call_api(sys_p, usr_p, api_key)
                        except Exception as e:
                            st.error(f"API call failed: {type(e).__name__}: {str(e)[:200]}")
                if phash in cache:
                    res = cache[phash]
                    _save(res["text"], "API", res["model"], (res["input_tokens"], res["output_tokens"]))

        memo = memos.get(memo_key)     # never shows a memo saved for another company, year or set of weights
        if memo:
            chk = AI.check_memo(memo["text"], payload, ccirs.verdict)
            (st.success if chk.passed else st.error)(f"AI reliability check: {chk.badge}")
            st.markdown("##### Memo" + (" (failing numbers highlighted)" if not chk.passed else ""))
            with st.container(border=True):
                shown = re.sub(r"(?m)^#{1,3}\s", "##### ", chk.highlighted)     # keep memo headings small
                st.markdown(shown.replace("\n", "  \n"), unsafe_allow_html=True)
            st.markdown("##### AI reliability check")
            r1, r2, r3 = st.columns(3)
            r1.metric("Figures verified", f"{chk.counts[AI.VERIFIED]} / {len(chk.figures)}")
            r2.metric("Verdict consistent", "Yes" if chk.verdict_ok else "No", help=chk.verdict_message)
            r3.metric("Sections present", f"{len(config.MEMO_SECTIONS) - len(chk.sections_missing)} / "
                                          f"{len(config.MEMO_SECTIONS)}")
            if not chk.verdict_ok:
                st.error(chk.verdict_message)
            if chk.sections_missing:
                st.warning("Missing sections: " + ", ".join(chk.sections_missing))
            if chk.banned:
                st.error("Banned phrases: " + ", ".join(sorted({b["text"] for b in chk.banned})))
            if chk.figures:
                st.dataframe(chk.table(), hide_index=True, width="stretch")
            wd = S.weights_description(weights)
            e1, e2 = st.columns(2)
            e1.download_button("Download memo (.md)", AI.memo_markdown(memo["text"], meta, ccirs.year, ccirs, wd, chk,
                                                                       memo["model"]).encode(),
                               f"ccirs_memo_{meta.get('ticker')}_{ccirs.year}.md", key="dl_md")
            e2.download_button("Download memo (.docx)", AI.memo_docx(memo["text"], meta, ccirs.year, ccirs, wd, chk,
                                                                     memo["model"]),
                               f"ccirs_memo_{meta.get('ticker')}_{ccirs.year}.docx", key="dl_docx",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        lp = AI.log_path()
        if lp.exists():
            st.download_button("Download prompt log (CSV)", lp.read_bytes(), "ai_calls.csv", key="dl_log")

def _bt_add(ticker, name):
    sector = st.session_state.get("bt_new_sector", "").strip() or "Other"
    role = st.session_state.get("bt_new_role", "healthy")
    cos = st.session_state["bt_companies"]
    if ticker and all(c["ticker"] != ticker for c in cos):
        st.session_state["bt_companies"] = cos + [{"sector": sector, "role": role, "name": name, "ticker": ticker}]


def _bt_remove(ticker):
    st.session_state["bt_companies"] = [c for c in st.session_state["bt_companies"] if c["ticker"] != ticker]


def _bt_reset():
    st.session_state["bt_companies"] = BT.default_set()
    st.session_state.pop("bt_results", None)


if tab_bt is not None:
  with tab_bt:
    st.markdown("#### Backtest - does CCIRS rank healthy firms above stressed ones?")
    st.caption("Every company is scored with the brief's DEFAULT weights - no tuning to force a result. Related-party "
               "and governance inputs you entered on a company's own page this session are reused. Overrides use "
               "your saved decisions where they exist; otherwise they are pending and the uncapped score is used "
               "(labelled), with a second column that confirms every pending override. " + config.YAHOO_YEARS_NOTE)
    st.session_state.setdefault("bt_companies", BT.default_set())
    cos = st.session_state["bt_companies"]

    with st.expander(f"Company set ({len(cos)} companies, {len(BT.pairs(cos))} sectors) - add or remove", expanded=False):
        st.dataframe(pd.DataFrame(cos)[["sector", "role", "name", "ticker"]], hide_index=True, width="stretch")
        rm = st.columns(4)
        for i, c in enumerate(cos):
            rm[i % 4].button(f"✕ {c['name'][:26]}", key=f"bt_rm_{c['ticker']}", on_click=_bt_remove, args=(c["ticker"],))
        a1, a2, a3 = st.columns([2, 1, 1])
        with a1:
            bt_t, bt_n = company_picker("bt", "Add a company by name")
        a2.text_input("Sector (pairs are formed by sector)", key="bt_new_sector", placeholder="e.g. Steel")
        a3.radio("Role", ["healthy", "stressed"], key="bt_new_role", horizontal=True)
        b1, b2 = st.columns(2)
        b1.button("Add company", key="bt_add", on_click=_bt_add, args=(bt_t, bt_n), disabled=not bt_t)
        b2.button("Reset to the default set (config.py)", key="bt_reset", on_click=_bt_reset)

    prev = {b.company["ticker"]: b for b in st.session_state.get("bt_results", [])}
    years, uploads = {}, {}
    with st.expander("Hindsight bias: scoring year and extra (pre-FY23) years per company", expanded=False):
        st.caption("Default = current (latest) year. To score a stressed firm BEFORE its stress, upload the earlier "
                   "years with the template (Rs crore, consolidated) and pick that year. Years appear after the first "
                   "run.")
        for c in cos:
            h1, h2 = st.columns([1, 2])
            b = prev.get(c["ticker"])
            opts = ["latest"] + (list(reversed(b.fd.years)) if b is not None and b.fd is not None else [])
            ychoice = h1.selectbox(c["name"][:40], opts, key=f"bt_year_{c['ticker']}")
            if ychoice != "latest":
                years[c["ticker"]] = ychoice
            up = h2.file_uploader(f"Extra years for {c['name'][:30]}", type=["xlsx", "csv"],
                                  key=f"bt_up_{c['ticker']}", label_visibility="collapsed")
            if up is not None:
                res = D.parse_upload(up.getvalue(), up.name)
                if res.errors:
                    h2.error("; ".join(res.errors))
                else:
                    uploads[c["ticker"]] = res.data

    if st.button("Run backtest", type="primary", key="bt_run"):
        with st.spinner(f"Scoring {len(cos)} companies (Yahoo fetches are cached)..."):
            st.session_state["bt_results"] = BT.run_batch(cos, cached_yahoo, years, uploads,
                                                          st.session_state.get("saved_decisions"),
                                                          {"include_noncurrent": include_nc},
                                                          st.session_state.get("saved_inputs"))
    results = st.session_state.get("bt_results")
    if not results:
        st.info("Press 'Run backtest' to score the company set.")
    else:
        for b in results:
            if b.error:
                st.warning(f"{b.company['name']}: {b.error}. The rest of the batch is unaffected.")
        dc = BT.direction_check(results, cos)
        dcc = BT.direction_check(results, cos, use_confirmed=True)
        sens = BT.batch_sensitivity(results, cos)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Direction check", BT.direction_summary(dc))
        m2.metric("With pending overrides confirmed", BT.direction_summary(dcc))
        m3.metric("Rank stability (Spearman)", "-" if sens["spearman_min"] is None else
                  f"min {sens['spearman_min']:.2f} · avg {sens['spearman_avg']:.2f}",
                  help="Spearman correlation between the default ranking and each of the ±10% weight scenarios.")
        m4.metric("Scenarios that change a pair result", f"{sens['pair_changes']} of {sens['scenarios']}")

        st.markdown("##### Results (default weights)")
        st.dataframe(BT.results_table(results), hide_index=True, width="stretch")
        st.markdown("##### Direction check by pair")
        st.dataframe(dc, hide_index=True, width="stretch",
                     column_config={"Explanation": st.column_config.TextColumn(width="large")})
        for _, row in dc[dc["Result"] == "FAIL"].iterrows():
            st.error(f"**{row['Sector']}: {row['Stressed']} scores at or above {row['Healthy']}.** {row['Explanation']}")
        with st.expander("Direction check with every pending override confirmed"):
            st.dataframe(dcc, hide_index=True, width="stretch")
        with st.expander("Alternative specification: Earnings Integrity neutral (50) for loss-makers"):
            st.warning(config.ALT_SPEC_LABEL + ". The default CCIRS above is unchanged.")
            st.caption("Why: for loss-makers, low accruals reflect cash losses rather than earnings quality, so the "
                       "Earnings Integrity pillar can flatter stressed firms. Here that pillar is set to "
                       f"{config.ALT_EI_NEUTRAL_SCORE} whenever it is marked 'limited'.")
            st.dataframe(BT.alt_spec_table(results), hide_index=True, width="stretch")
            dca = BT.direction_check(results, cos, use_alt=True)
            st.markdown(f"**Pair results under the alternative:** {BT.direction_summary(dca)} "
                        f"(default: {BT.direction_summary(dc)})")
            st.dataframe(dca[[c for c in dca.columns if c != "Explanation"]], hide_index=True, width="stretch")
        with st.expander("Rank stability per weight scenario"):
            if sens["per_scenario"] is not None:
                st.dataframe(sens["per_scenario"], hide_index=True, width="stretch")
                if sens["flipping_pairs"]:
                    st.caption("Pairs whose result changes in some scenario: " + ", ".join(
                        f"{k} ({v} scenarios)" for k, v in sens["flipping_pairs"].items()))
        d1, d2, d3 = st.columns(3)
        d1.download_button("Download backtest (.xlsx)", BT.export_xlsx(results, cos), "ccirs_backtest.xlsx",
                           key="bt_xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        d2.download_button("Download backtest (.csv)", BT.export_csv(results), "ccirs_backtest.csv", key="bt_csv")
        hc = HC.entries_from_results(results)
        if len(hc) == 2:
            d3.download_button(f"Download hand-check workbook ({hc[0]['name'].split()[0]} vs {hc[1]['name'].split()[0]})",
                               HC.build_workbook(hc), "ccirs_handcheck.xlsx", key="bt_hc",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        else:
            d3.caption("The hand-check workbook needs one healthy and one stressed company that scored.")

with tab_checks:
    # ---------- top: data coverage and warnings
    n_err = len(report.blocking_errors) + len(report.overridable_errors)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Data depth", depth["badge"].split(" - ")[0], help=depth["badge"])
    k2.metric("Usable years", depth["years"], help="Consecutive years with revenue and total assets present: "
                                                   + (", ".join(depth["usable_years"]) or "none"))
    k3.metric("Errors", n_err)
    k4.metric("Warnings", len(report.warnings))
    if ccirs is not None and ccirs.score is not None:
        st.caption(f"Score data coverage for {ccirs.year}: {ccirs.coverage:.0%} of the CCIRS weight had data "
                   f"(missing ratios are re-weighted, never filled with zero).")
    if not report.issues:
        st.success("No issues found.")
    for sev, title in (("error", "Errors"), ("warning", "Warnings - review"), ("info", "Information")):
        items = [i for i in report.issues if i.severity == sev]
        if items:
            st.subheader(f"{SEVERITY_ICON[sev]} {title} ({len(items)})")
            for i in items:
                tag = f" *({i.source} - may proceed with warning)*" if i.overridable else ""
                st.markdown(f"- **{i.check}** · {i.year}: {i.message}{tag}")

    st.markdown("#### Data coverage")
    st.markdown(f"**{depth['years']} consecutive usable year(s)** (revenue and total assets present): "
                f"{', '.join(depth['usable_years']) or 'none'}")
    st.markdown("**Unlocked**\n" + "\n".join(f"- ✅ {u}" for u in depth["unlocked"]))
    if depth["locked"]:
        st.markdown("**Locked**\n" + "\n".join(f"- 🔒 {l}" for l in depth["locked"]))
    st.caption("Yahoo gives about 4 annual periods. Upload more years with the template to unlock trend and "
               "full-cycle analysis.")
    st.caption(f"Tolerances (config.py): balance-sheet warning > {config.BALANCE_TOLERANCE_WARN:.0%}, error > "
               f"{config.BALANCE_TOLERANCE_ERROR:.0%}; YoY jump flag at {config.YOY_JUMP_FACTOR:.0f}x and only a "
               f"warning when the amount exceeds {config.MATERIALITY_PCT:.0%} of total assets or revenue.")

    # ---------- source mapping (collapsed)
    with st.expander(config.SOURCE_MAPPING_TITLE, expanded=False):
        if meta.get("labels_used"):
            st.markdown("**Yahoo label used for each row** (first non-empty label wins each year)")
            st.dataframe(pd.DataFrame({"Row": [D.ITEM_LABELS[k] for k in meta["labels_used"]],
                                       "Yahoo label(s)": list(meta["labels_used"].values())}),
                         hide_index=True, width="stretch")
            unmapped = [D.ITEM_LABELS[k] for k in D.ITEM_KEYS if k not in meta["labels_used"]]
            if unmapped:
                st.caption("Not available from Yahoo for this company: " + ", ".join(unmapped))
        else:
            st.caption("No Yahoo mapping for this load (uploaded or demo data).")
        st.markdown("**Merge notes**")
        for n in fd.notes or ["(none)"]:
            st.markdown(f"- {n}")
