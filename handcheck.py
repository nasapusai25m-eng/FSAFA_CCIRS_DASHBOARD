"""
CCIRS - Stage 6 Excel hand-check workbook.

For one healthy and one stressed company it writes:
  * "Inputs - <company>"  raw line items for the scoring year and the prior year (blue = input), with source and
                           units, plus the EBITDA / EBIT bridge (exceptional items, other income, associates) as
                           LIVE formulas;
  * "Calculations"         LIVE formulas for operating EBITDA/EBIT, CCE, Sloan, interest coverage and Altman Z''
                           (each factor, coefficient and contribution) plus Z' (manufacturing); beside each: the
                           app's value, the difference, a stated tolerance and PASS / FAIL;
  * "Notes"                what each formula does, where each input came from, how to replace inputs with
                           annual-report figures.
Conventions: Arial; blue font = inputs; black = formulas; green = links to another sheet; denominators guarded.
"""
from __future__ import annotations

import io
import math
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import config
import data as D
import ratios as R

BLUE, BLACK, GREEN, WHITE = "0000FF", "000000", "008000", "FFFFFF"
HEAD_FILL = PatternFill("solid", fgColor="1F3864")
SUB_FILL = PatternFill("solid", fgColor="D9E1F2")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")
THIN = Side(style="thin", color="BFBFBF")
NUM = '#,##0.0;(#,##0.0);"-"'
PCT = '0.00%;(0.00%);"-"'
MULT = '0.00"x";(0.00"x");"-"'
DEC = '0.0000;(0.0000);"-"'

INPUT_ROWS = [  # key, label
    ("revenue", "Revenue from operations"),
    ("ebitda", "EBITDA as reported"),
    ("ebit", "EBIT as reported"),
    ("other_income", "Other income (non-operating)"),
    ("associate_share", "Share of profit of associates/JVs"),
    ("exceptional_items", "Exceptional / unusual items (gain +, loss -)"),
    ("interest_expense", "Interest expense (finance cost)"),
    ("pat", "Profit after tax (net income)"),
    ("cfo", "Cash flow from operations (CFO)"),
    ("total_assets", "Total assets"),
    ("current_assets", "Total current assets"),
    ("current_liabilities", "Total current liabilities"),
    ("retained_earnings", "Retained earnings (other equity)"),
    ("equity", "Shareholders' equity (book)"),
    ("total_liabilities", "Total liabilities"),
]
FIRST_INPUT_ROW = 6


def _font(color=BLACK, bold=False, size=10, italic=False):
    return Font(name="Arial", color=color, bold=bold, size=size, italic=italic)


def _short(name: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]", " ", name).split()
    stop = {"limited", "ltd", "india", "the", "company", "co"}
    short = " ".join(w for w in words if w.lower() not in stop)[:20].strip()
    return short or "Company"


def _q(sheet: str) -> str:
    return "'" + sheet.replace("'", "''") + "'"


def _app_num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


# --------------------------------------------------------------------------- inputs sheet
def _inputs_sheet(wb: Workbook, entry: dict) -> dict:
    """Writes one company's inputs; returns {key: (col_prior, col_cur, row)} and bridge cell addresses."""
    fd, fy = entry["fd"], entry["year"]
    py = R.prev_fy(fd, fy)
    title = f"Inputs - {entry['short']}"
    ws = wb.create_sheet(title)
    ws["A1"] = f"{entry['name']} ({entry['ticker']}) - inputs for the CCIRS hand check"
    ws["A1"].font = _font(bold=True, size=13)
    ws["A2"] = ("Units: Rs crore. BLUE cells are inputs - replace them with annual-report figures (consolidated). "
                "Black cells are formulas. Blank = data unavailable.")
    ws["A2"].font = _font(italic=True, size=9)
    ws["A3"] = f"Scoring year {fy}; prior year {py or 'not available'}. Data loaded {fd.meta.get('fetched_at', '-')}."
    ws["A3"].font = _font(size=9)
    heads = ["Key", "Line item", f"{py or 'Prior year'} (prior)", f"{fy} (scoring year)", "Source - prior",
             "Source - scoring year", "Units"]
    for j, h in enumerate(heads, 1):
        c = ws.cell(row=5, column=j, value=h)
        c.font, c.fill = _font(WHITE, bold=True), HEAD_FILL
        c.alignment = Alignment(wrap_text=True, vertical="center")
    cells = {}
    for i, (key, label) in enumerate(INPUT_ROWS):
        row = FIRST_INPUT_ROW + i
        ws.cell(row=row, column=1, value=key).font = _font(size=9, color="808080")
        ws.cell(row=row, column=2, value=label).font = _font()
        for col, year in ((3, py), (4, fy)):
            v = fd.get(key, year) if year else float("nan")
            c = ws.cell(row=row, column=col, value=None if math.isnan(v) else round(v, 4))
            c.font, c.number_format, c.fill = _font(BLUE), NUM, INPUT_FILL
            src = fd.sources.at[key, year] if year and fd.sources.at[key, year] else "data unavailable"
            ws.cell(row=row, column=col + 2, value=src).font = _font(size=9)
        ws.cell(row=row, column=7, value="Rs crore").font = _font(size=9)
        cells[key] = row
    # EBITDA / EBIT bridge (formulas)
    r0 = FIRST_INPUT_ROW + len(INPUT_ROWS) + 1
    ws.cell(row=r0, column=2, value="Operating EBITDA / EBIT bridge (formulas - do not overwrite)").font = \
        _font(bold=True)
    ws.cell(row=r0, column=2).fill = SUB_FILL
    bridge = {}
    for k, (name, base) in enumerate((("Operating EBITDA", "ebitda"), ("Operating EBIT", "ebit"))):
        row = r0 + 1 + k
        ws.cell(row=row, column=1, value=f"operating_{base}").font = _font(size=9, color="808080")
        lc = ws.cell(row=row, column=2, value=f"{name} = {base.upper()} reported - other income - associates - "
                                              f"exceptional")
        lc.font, lc.alignment = _font(), Alignment(wrap_text=True, vertical="top")
        for col in (3, 4):
            L = get_column_letter(col)
            b, oi, asc, exc = (f"{L}{cells[x]}" for x in (base, "other_income", "associate_share", "exceptional_items"))
            c = ws.cell(row=row, column=col, value=f'=IF(ISNUMBER({b}),{b}-N({oi})-N({asc})-N({exc}),"n/a")')
            c.font, c.number_format = _font(), NUM
        bridge[base] = row
    if not fd.meta.get("exceptional_excluded", True):
        ws.cell(row=r0 + 3, column=2, value="Note: the app was run WITHOUT removing exceptional items.").font = \
            _font(color="C00000", size=9)
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 66
    for col in "CD":
        ws.column_dimensions[col].width = 16
    for col in "EF":
        ws.column_dimensions[col].width = 18
    ws.column_dimensions["G"].width = 10
    ws.freeze_panes = "C6"
    return {"sheet": title, "rows": cells, "bridge": bridge}


# --------------------------------------------------------------------------- calculations sheet
def _calc_block(ws, start: int, entry: dict, ref: dict) -> int:
    fd, book, fy = entry["fd"], entry["book"], entry["year"]
    sh = _q(ref["sheet"])

    def inp(key, cur=True):
        return f"{sh}!${'D' if cur else 'C'}${ref['rows'][key]}"

    def br(base):
        return f"{sh}!$D${ref['bridge'][base]}"

    ws.cell(row=start, column=1, value=f"{entry['name']} ({entry['ticker']}) - scoring year {fy}").font = \
        _font(bold=True, size=12)
    ws.cell(row=start, column=1).fill = SUB_FILL
    for j in range(2, 8):
        ws.cell(row=start, column=j).fill = SUB_FILL
    heads = ["Metric", "Excel (live formula)", "App value", "Difference", "Tolerance", "Check", "Formula in words"]
    for j, h in enumerate(heads, 1):
        c = ws.cell(row=start + 1, column=j, value=h)
        c.font, c.fill = _font(WHITE, bold=True), HEAD_FILL
    row = start + 2
    out = {}

    def app(key, attr=None):
        r = book.get(key, fy)
        if attr:
            return _app_num(r.inputs.get(attr)) if r.ok else None
        return _app_num(r.value) if r.ok else None

    def put(label, formula, app_value, fmt, tol, words, key=None):
        nonlocal row
        ws.cell(row=row, column=1, value=label).font = _font()
        c = ws.cell(row=row, column=2, value=formula)
        c.font = _font(GREEN if formula.startswith(f"={sh}!") else BLACK)
        c.number_format = fmt
        a = ws.cell(row=row, column=3, value=app_value if app_value is not None else "n/a")
        a.font, a.number_format = _font(), fmt
        ws.cell(row=row, column=4, value=f'=IF(AND(ISNUMBER(B{row}),ISNUMBER(C{row})),B{row}-C{row},"n/a")'
                ).number_format = fmt
        t = ws.cell(row=row, column=5, value=f"={tol}")
        t.number_format = fmt
        ws.cell(row=row, column=6, value=(
            f'=IF(AND(ISNUMBER(B{row}),ISNUMBER(C{row})),IF(ABS(B{row}-C{row})<=E{row},"PASS","FAIL"),'
            f'IF(AND(NOT(ISNUMBER(B{row})),NOT(ISNUMBER(C{row}))),"PASS (both n/a)","CHECK"))')).font = _font(bold=True)
        ws.cell(row=row, column=7, value=words).font = _font(size=9)
        for j in range(1, 8):
            ws.cell(row=row, column=j).border = Border(bottom=THIN)
        if key:
            out[key] = row
        row += 1

    tr, ts = "$C$4", "$C$5"     # tolerance cells (ratios, scores)
    oe_app = _app_num(fd.get("operating_ebitda", fy))
    ob_app = _app_num(fd.get("operating_ebit", fy))
    put("Operating EBITDA (Rs crore)", f"={br('ebitda')}", oe_app if oe_app is not None else _app_num(fd.get("ebitda", fy)),
        NUM, ts, "EBITDA as reported - other income - associates - exceptional items (Inputs sheet bridge)", "oe")
    put("Operating EBIT (Rs crore)", f"={br('ebit')}", ob_app if ob_app is not None else _app_num(fd.get("ebit", fy)),
        NUM, ts, "EBIT as reported - other income - associates - exceptional items", "ob")
    oe, ob = f"B{out['oe']}", f"B{out['ob']}"
    put("CCE = CFO / operating EBITDA",
        f'=IF(AND(ISNUMBER({inp("cfo")}),ISNUMBER({oe})),IF({oe}>0,{inp("cfo")}/{oe},"n/a"),"n/a")',
        app("cce"), PCT, tr, "Not meaningful (n/a) when operating EBITDA is zero or negative.", "cce")
    put("Sloan = (PAT - CFO) / average total assets",
        f'=IF(COUNT({inp("pat")},{inp("cfo")},{inp("total_assets")},{inp("total_assets", False)})=4,'
        f'IF(AVERAGE({inp("total_assets")},{inp("total_assets", False)})>0,({inp("pat")}-{inp("cfo")})/'
        f'AVERAGE({inp("total_assets")},{inp("total_assets", False)}),"n/a"),"n/a")',
        app("sloan"), PCT, tr, "Average of scoring-year and prior-year total assets.", "sloan")
    put("Interest coverage = operating EBIT / interest",
        f'=IF(AND(ISNUMBER({ob}),ISNUMBER({inp("interest_expense")})),IF({inp("interest_expense")}<>0,'
        f'{ob}/{inp("interest_expense")},"n/a"),"n/a")',
        app("interest_coverage"), MULT, tr, "n/a when there is no interest expense (debt-free).", "ic")

    # Altman factors (shared) + both variants
    ta, tl = inp("total_assets"), inp("total_liabilities")
    guard = f'COUNT({inp("current_assets")},{inp("current_liabilities")},{inp("retained_earnings")},{ta},{tl},' \
            f'{inp("equity")},{ob})=7'
    xsrc = "altman_z1" if book.get("altman_z1", fy).ok else "altman_z2"
    xs = {
        "X1": (f'=IF({guard},IF({ta}>0,({inp("current_assets")}-{inp("current_liabilities")})/{ta},"n/a"),"n/a")',
               "(Current assets - current liabilities) / total assets"),
        "X2": (f'=IF({guard},IF({ta}>0,{inp("retained_earnings")}/{ta},"n/a"),"n/a")', "Retained earnings / total assets"),
        "X3": (f'=IF({guard},IF({ta}>0,{ob}/{ta},"n/a"),"n/a")', "Operating EBIT / total assets"),
        "X4": (f'=IF({guard},IF({tl}>0,{inp("equity")}/{tl},"n/a"),"n/a")', "Book equity / total liabilities"),
        "X5": (f'=IF(AND({guard},ISNUMBER({inp("revenue")})),IF({ta}>0,{inp("revenue")}/{ta},"n/a"),"n/a")',
               "Revenue / total assets (Z' only)"),
    }
    xrow = {}
    for x, (f, words) in xs.items():
        put(f"Altman {x}", f, app(xsrc if x != "X5" else "altman_z1", x), DEC, ts, words, f"x{x}")
        xrow[x] = row - 1
    for key, title in (("altman_z2", "Altman Z'' (non-manufacturing, 4-factor)"),
                       ("altman_z1", "Altman Z' (manufacturing, 5-factor)")):
        used = " - variant used by the app" if key == book.altman_key else ""
        ws.cell(row=row, column=1, value=title + used).font = _font(bold=True)
        row += 1
        contrib_cells = []
        for x, coef in R.ALTMAN_COEF[key].items():
            ws.cell(row=row, column=1, value=f"  {x} coefficient").font = _font(size=9)
            cc = ws.cell(row=row, column=2, value=coef)
            cc.font, cc.number_format, cc.fill = _font(BLUE), "0.000", INPUT_FILL
            ws.cell(row=row, column=7, value="Model coefficient (input - Altman)").font = _font(size=9)
            row += 1
            app_x = app(key, x)
            put(f"  {x} contribution (coefficient x {x})",
                f'=IF(ISNUMBER(B{xrow[x]}),B{row - 1}*B{xrow[x]},"n/a")',
                None if app_x is None else coef * app_x, DEC, ts, f"{coef:g} x {x}")
            contrib_cells.append(f"B{row - 1}")
        put(f"  {title.split(' (')[0]} = sum of contributions",
            f'=IF(COUNT({",".join(contrib_cells)})={len(contrib_cells)},SUM({",".join(contrib_cells)}),"n/a")',
            app(key), DEC, ts, "Zones: Z'' safe > 2.60, distress < 1.10; Z' safe > 2.90, distress < 1.23.", key)
    return row + 1


def build_workbook(entries: list[dict]) -> bytes:
    """entries: [{"name", "ticker", "fd", "book", "year"}] - one healthy and one stressed company."""
    wb = Workbook()
    calc = wb.active
    calc.title = "Calculations"
    refs = []
    used_titles = set()
    for e in entries:
        short = _short(e["name"])
        while f"Inputs - {short}" in used_titles:
            short = short[:18] + "2"
        used_titles.add(f"Inputs - {short}")
        e = {**e, "short": short}
        refs.append((e, _inputs_sheet(wb, e)))
    calc["A1"] = "CCIRS hand check - live Excel formulas vs the app"
    calc["A1"].font = _font(bold=True, size=13)
    calc["A2"] = ("Black = formula, green = link to an Inputs sheet, blue = input. Change the blue cells on the Inputs "
                  "sheets and every formula here recalculates. The app value is a pasted number for comparison.")
    calc["A2"].font = _font(italic=True, size=9)
    for r, (label, v, note) in enumerate((("Tolerance - ratios (|Excel - app|)", config.HANDCHECK_TOLERANCE["ratio"],
                                           "0.0005 = 0.05 percentage points on %, 0.0005x on multiples"),
                                          ("Tolerance - amounts and Z-scores", config.HANDCHECK_TOLERANCE["score"],
                                           "absolute difference allowed")), start=4):
        calc.cell(row=r, column=1, value=label).font = _font(bold=True)
        c = calc.cell(row=r, column=3, value=v)
        c.font, c.fill, c.number_format = _font(BLUE), INPUT_FILL, "0.0000"
        calc.cell(row=r, column=7, value=note).font = _font(size=9)
    row = 7
    for e, ref in refs:
        row = _calc_block(calc, row, e, ref)
    widths = {"A": 46, "B": 18, "C": 16, "D": 14, "E": 12, "F": 16, "G": 70}
    for col, w in widths.items():
        calc.column_dimensions[col].width = w
    calc.freeze_panes = "B7"
    _notes(wb, entries)
    wb.move_sheet("Calculations", offset=len(entries))      # Inputs sheets first, then Calculations, then Notes
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _notes(wb: Workbook, entries: list[dict]):
    ws = wb.create_sheet("Notes")
    lines = [
        ("CCIRS hand-check workbook - notes", True),
        ("Purpose: recompute four CCIRS inputs by hand and confirm the app reproduces them.", False),
        ("", False),
        ("Inputs", True),
        ("Each 'Inputs - <company>' sheet holds raw line items for the scoring year and the prior year in Rs crore "
         "(consolidated). Blue cells are inputs; the Source columns say where each came from (Yahoo, User upload, "
         "Demo, Manual entry, or '(derived)' when the app rebuilt it from an accounting identity).", False),
        ("To use annual-report figures: overwrite the blue cells with figures from the consolidated statement of "
         "profit and loss, balance sheet and cash-flow statement. Keep the units in Rs crore; enter losses and "
         "exceptional losses with a minus sign; leave a cell blank if the figure is not reported. The Calculations "
         "sheet recalculates; the 'App value' column will then show how far the app's Yahoo-based figures are from "
         "the annual report.", False),
        ("EBITDA bridge: operating EBITDA (and EBIT) = as reported - other income - share of associates/JVs - "
         "exceptional items. Exceptional items (e.g. an AGR reassessment gain, impairment, restructuring) are "
         "removed so one-offs cannot inflate cash conversion, coverage or Altman X3. A blank adjustment counts as "
         "zero (N() function), matching the app, which only subtracts adjustments that are reported.", False),
        ("", False),
        ("Formulas (Calculations sheet)", True),
        ("CCE = CFO / operating EBITDA. n/a if operating EBITDA <= 0 (the app reports 'not meaningful').", False),
        ("Sloan accrual ratio = (PAT - CFO) / average of scoring-year and prior-year total assets.", False),
        ("Interest coverage = operating EBIT / interest expense. n/a if interest is zero.", False),
        ("Altman Z'' (non-manufacturing, 4-factor, no +3.25 constant) = 6.56 X1 + 3.26 X2 + 6.72 X3 + 1.05 X4.", False),
        ("Altman Z' (manufacturing) = 0.717 X1 + 0.847 X2 + 3.107 X3 + 0.420 X4 + 0.998 X5.", False),
        ("X1 = (current assets - current liabilities) / total assets; X2 = retained earnings / total assets; "
         "X3 = operating EBIT / total assets; X4 = book equity / total liabilities; X5 = revenue / total assets.", False),
        ("Each factor's contribution = coefficient x factor, so you can see which factor drives the score.", False),
        ("", False),
        ("Check columns", True),
        ("Difference = Excel - app. Check = PASS when |difference| <= tolerance (cells C4:C5 on Calculations), "
         "'PASS (both n/a)' when neither side can compute the ratio, FAIL otherwise, CHECK when only one side has a "
         "value (usually a missing input).", False),
        ("Percentages are stored as fractions (0.25 shows as 25.00%).", False),
        ("", False),
        ("Companies in this workbook", True),
    ] + [(f"{e['name']} ({e['ticker']}) - scoring year {e['year']}; Altman variant used by the app: "
          f"{R.ALTMAN_SHORT[e['book'].altman_key]}", False) for e in entries] + [
        ("", False),
        ("Screening tool - not proof of manipulation or default. For academic purposes only.", False),
    ]
    for i, (text, bold) in enumerate(lines, 1):
        c = ws.cell(row=i, column=1, value=text)
        c.font = _font(bold=bold, size=12 if i == 1 else 10)
        c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 130


def entries_from_results(results, tickers=None) -> list[dict]:
    """Pick the hand-check companies from backtest results: configured tickers first, else the first healthy and
    first stressed company that scored."""
    tickers = tickers or config.HANDCHECK_COMPANIES
    ok = {b.company["ticker"]: b for b in results if b.ok}
    chosen = [ok[t] for t in tickers if t in ok]
    if len(chosen) < 2:
        for role in ("healthy", "stressed"):
            if not any(b.company["role"] == role for b in chosen):
                b = next((b for b in ok.values() if b.company["role"] == role and b not in chosen), None)
                if b:
                    chosen.append(b)
    return [{"name": b.company["name"], "ticker": b.company["ticker"], "fd": b.fd, "book": b.book, "year": b.year}
            for b in chosen[:2]]


# --------------------------------------------------------------------------- static checks (no recalculation)
_REF = re.compile(r"(?:'((?:[^']|'')+)'|([A-Za-z_][A-Za-z0-9_]*))!\$?([A-Z]{1,3})\$?(\d+)"
                  r"(?::\$?([A-Z]{1,3})\$?(\d+))?")          # optional second cell of a sheet range
_LOCAL = re.compile(r"(?<![A-Za-z0-9_!'\$])\$?([A-Z]{1,3})\$?(\d+)(?![\(A-Za-z0-9_])")
ERROR_LITERALS = ("#REF!", "#DIV/0!", "#NAME?", "#VALUE!", "#N/A", "#NUM!", "#NULL!")


def static_issues(wb) -> dict:
    """Formula count and problems found without recalculating: references to missing sheets or to cells
    outside the referenced sheet's used range, and error literals. Works on any Python version."""
    from openpyxl.utils import column_index_from_string
    sheets = {ws.title: ws for ws in wb.worksheets}
    n, problems = 0, []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if isinstance(v, str) and any(e in v for e in ERROR_LITERALS):
                    problems.append(f"{ws.title}!{c.coordinate}: error literal in '{v[:40]}'")
                if not (isinstance(v, str) and v.startswith("=")):
                    continue
                n += 1
                body = re.sub(r'"[^"]*"', '""', v)                     # ignore text inside quotes
                for m in _REF.finditer(body):
                    name = (m.group(1) or m.group(2)).replace("''", "'")
                    if name not in sheets:
                        problems.append(f"{ws.title}!{c.coordinate}: missing sheet '{name}'")
                        continue
                    tgt = sheets[name]
                    for cg, rg in ((3, 4), (5, 6)):
                        if not m.group(cg):
                            continue
                        col, r = column_index_from_string(m.group(cg)), int(m.group(rg))
                        if r > tgt.max_row or col > tgt.max_column:
                            problems.append(f"{ws.title}!{c.coordinate}: {name}!{m.group(cg)}{r} is outside the sheet")
                local = _REF.sub("", body)
                for m in _LOCAL.finditer(local):
                    col, r = column_index_from_string(m.group(1)), int(m.group(2))
                    if r > ws.max_row or col > ws.max_column:
                        problems.append(f"{ws.title}!{c.coordinate}: {m.group(1)}{r} is outside the sheet")
    return {"formulas": n, "problems": problems}
