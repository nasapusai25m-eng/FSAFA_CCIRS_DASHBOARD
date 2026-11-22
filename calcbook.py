"""
CCIRS - Stage 7 calculation workbook (download from the CCIRS score tab) for the loaded company and scoring year.

Sheets
  Inputs      raw line items for the scoring year and the prior year (blue), source and units, the EBITDA/EBIT
              bridge (live formulas), and the analyst inputs / settings the app used (blue)
  Ratios      LIVE formulas for every CCIRS component, each with a status (OK / n/a / distress / no debt) and a
              check against the app's value
  Thresholds  the bands from config.py (lower bound, upper bound, sub-score, label, source: Brief / Analyst /
              PROPOSED - review), model coefficients and the governance lookup tables
  Weights     editable pillar and sub-weights (blue) with a check that each group sums to 100%
  Scoring     sub-scores by formula (band lookup; sliding scale for Beneish; passed/assessable for Piotroski),
              re-weighting for unavailable ratios, pillar scores, CCIRS, overrides, band and verdict - each checked
              against the app
  Notes       how to use it
Changing any blue cell recalculates the CCIRS. Conventions: Arial; blue = input; black = formula;
green = link to another sheet; denominators guarded; no array formulas.
"""
from __future__ import annotations

import io
import math
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

import config
import data as D
import ratios as R
import scoring as S

BLUE, BLACK, GREEN, WHITE, GREY = "0000FF", "000000", "008000", "FFFFFF", "808080"
HEAD = PatternFill("solid", fgColor="1F3864")
SUB = PatternFill("solid", fgColor="D9E1F2")
INP = PatternFill("solid", fgColor="FFF2CC")
THIN = Side(style="thin", color="BFBFBF")
NUM, PCT, MULT, DAYS, DEC, SCORE = ('#,##0.0;(#,##0.0);"-"', '0.00%;(0.00%);"-"', '0.00"x";(0.00"x");"-"',
                                    '#,##0.0" d";(#,##0.0" d");"-"', '0.0000;(0.0000);"-"', '0.00')
BIG = 1e99

INPUT_KEYS = ["revenue", "cogs", "sga", "ebitda", "ebit", "depreciation", "other_income", "associate_share",
              "exceptional_items", "interest_expense", "pbt", "pat", "cash_opex", "cash", "marketable_securities",
              "noncurrent_liquid_investments", "receivables", "inventory", "current_assets", "ppe", "total_assets",
              "accounts_payable", "current_liabilities", "current_debt", "long_term_debt", "total_debt",
              "lease_liabilities", "total_liabilities", "equity", "retained_earnings", "shares_outstanding", "cfo",
              "capex", "related_party_advances"]

# threshold provenance shown on the Thresholds sheet
THRESHOLD_SOURCE = {"cce": "Brief (slide wording for labels)", "sloan": "Brief, 5-10% band edited by analyst",
                    "altman_z2": "Brief", "altman_z1": "Altman (1983) zones", "beneish_m": "Brief (sliding scale)",
                    "dir": "Analyst-specified", "wc_days": "Analyst-specified",
                    "bs_accrual": "PROPOSED - review (5-10% band edited by analyst)"}


def _f(color=BLACK, bold=False, size=10, italic=False):
    return Font(name="Arial", color=color, bold=bold, size=size, italic=italic)


def _head(ws, row, labels, start=1):
    for j, h in enumerate(labels, start):
        c = ws.cell(row=row, column=j, value=h)
        c.font, c.fill = _f(WHITE, True), HEAD
        c.alignment = Alignment(wrap_text=True, vertical="center")


def _num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


# =========================================================================== builder
class CalcBook:
    def __init__(self, fd, book, ccirs, weights, governance, decisions, settings):
        self.fd, self.book, self.res = fd, book, ccirs
        self.fy = ccirs.year
        self.py = R.prev_fy(fd, self.fy)
        self.weights = weights or S.default_weights()
        self.gov = governance or S.default_governance()
        self.dec = {"cfo_decision": S.CFO_PENDING, "neq_decision": S.NEQ_PENDING, **(decisions or {})}
        self.settings = {**R.default_settings(), **(settings or {})}
        self.wb = Workbook()
        self.rows: dict[str, int] = {}
        self.thr: dict[str, tuple[int, int]] = {}

    # ----------------------------------------------------------------- refs
    def I(self, key, cur=True):
        return f"Inputs!${'D' if cur else 'C'}${self.rows[key]}"

    def S_(self, name):                      # a single setting / analyst input on the Inputs sheet
        return f"Inputs!$C${self.rows['set_' + name]}"

    # ----------------------------------------------------------------- Inputs
    def inputs(self):
        ws = self.wb.active
        ws.title = "Inputs"
        fd, fy, py = self.fd, self.fy, self.py
        ws["A1"] = f"{fd.meta.get('name')} ({fd.meta.get('ticker')}) - CCIRS calculation workbook, scoring year {fy}"
        ws["A1"].font = _f(bold=True, size=13)
        ws["A2"] = ("Rs crore. BLUE cells are inputs (replace with annual-report figures if you wish); black cells are "
                    "formulas. Blank = data unavailable. Values marked '(derived)' were rebuilt by the app from an "
                    "accounting identity; '(manual)' were entered by the analyst.")
        ws["A2"].font = _f(italic=True, size=9)
        ws["A3"] = f"Generated {datetime.now():%d %b %Y %H:%M}. Weights: {S.weights_description(self.weights)}"
        ws["A3"].font = _f(size=9)
        _head(ws, 5, ["Key", "Line item", f"{py or 'Prior year'} (prior)", f"{fy} (scoring year)", "Source - prior",
                      "Source - scoring year", "Units"])
        r = 6
        for key in INPUT_KEYS:
            ws.cell(row=r, column=1, value=key).font = _f(GREY, size=9)
            ws.cell(row=r, column=2, value=D.ITEM_LABELS[key]).font = _f()
            for col, year in ((3, py), (4, fy)):
                v = fd.get(key, year) if year else float("nan")
                c = ws.cell(row=r, column=col, value=None if math.isnan(v) else round(v, 6))
                c.font, c.number_format, c.fill = _f(BLUE), NUM, INP
                src = fd.sources.at[key, year] if year and fd.sources.at[key, year] else "data unavailable"
                ws.cell(row=r, column=col + 2, value=src).font = _f(size=9)
            ws.cell(row=r, column=7, value="crore shares" if key == "shares_outstanding" else "Rs crore").font = _f(size=9)
            self.rows[key] = r
            r += 1
        r += 1
        ws.cell(row=r, column=2, value="Operating EBITDA / EBIT bridge (formulas)").font = _f(bold=True)
        ws.cell(row=r, column=2).fill = SUB
        r += 1
        for base in ("ebitda", "ebit"):
            ws.cell(row=r, column=1, value=f"operating_{base}").font = _f(GREY, size=9)
            lab = ws.cell(row=r, column=2, value=f"Operating {base.upper()} = {base.upper()} reported - other income - "
                                                 f"associates - exceptional")
            lab.font, lab.alignment = _f(), Alignment(wrap_text=True)
            for col in (3, 4):
                L = "CD"[col - 3]
                b, oi, asc, exc = (f"{L}{self.rows[k]}" for k in (base, "other_income", "associate_share",
                                                                   "exceptional_items"))
                excl = "" if self.fd.meta.get("exceptional_excluded", True) else "+N(" + exc + ")"
                c = ws.cell(row=r, column=col, value=f'=IF(ISNUMBER({b}),{b}-N({oi})-N({asc})-N({exc}){excl},"n/a")')
                c.font, c.number_format = _f(), NUM
            self.rows[f"operating_{base}"] = r
            r += 1
        r += 1
        ws.cell(row=r, column=2, value="Settings and analyst inputs used by the app (blue - editable)").font = _f(bold=True)
        ws.cell(row=r, column=2).fill = SUB
        r += 1
        app_key = self.book.altman_key
        settings = [
            ("include_noncurrent", "Include non-current liquid investments in DIR and net debt (TRUE/FALSE)",
             bool(self.settings["include_noncurrent"])),
            ("altman_variant", "Altman variant used (Z'' or Z')", "Z'" if app_key == "altman_z1" else "Z''"),
            ("gov_reviewed", "Governance inputs reviewed (TRUE/FALSE)", bool(self.gov.get("reviewed"))),
            ("auditor_event", "Auditor change", self.gov.get("auditor_event")),
            ("opinion", "Audit opinion", self.gov.get("opinion")),
            ("pledge", "Promoter pledge (% of promoter holding)", float(self.gov.get("pledge_pct") or 0)),
            ("cfo_decision", "Negative-CFO override decision", self.dec["cfo_decision"]),
            ("neq_decision", "Negative-net-worth override decision", self.dec["neq_decision"]),
        ]
        for key, label, val in settings:
            ws.cell(row=r, column=1, value=key).font = _f(GREY, size=9)
            ws.cell(row=r, column=2, value=label).font = _f()
            c = ws.cell(row=r, column=3, value=val)
            c.font, c.fill = _f(BLUE), INP
            self.rows["set_" + key] = r
            r += 1
        for col, w in zip("ABCDEFG", (26, 60, 16, 16, 18, 18, 11)):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "C6"

    # ----------------------------------------------------------------- Thresholds
    def thresholds(self):
        ws = self.wb.create_sheet("Thresholds")
        ws["A1"] = "Thresholds, coefficients and lookup tables (from config.py)"
        ws["A1"].font = _f(bold=True, size=13)
        ws["A2"] = ("Sub-score = score of the band whose LOWER bound is the largest one <= the ratio (an upper bound "
                    "belongs to the next band, as in the app). Blue = editable. Source shows where each band came from.")
        ws["A2"].font = _f(italic=True, size=9)
        r = 4
        keys = ["cce", "ocf_ratio", "dir", "wc_days", "ccc_trend", "interest_coverage", "de_total", "net_debt_ebitda",
                "dscr", "sloan", "bs_accrual", "dsri", "rec_vs_sales", "altman_z2", "altman_z1", "rp_advances_revenue"]
        for key in keys:
            bands = config.SCORE_THRESHOLDS[key]
            src = THRESHOLD_SOURCE.get(key, "PROPOSED - review")
            ws.cell(row=r, column=1, value=key).font = _f(bold=True)
            ws.cell(row=r, column=2, value=src).font = _f(color="C00000" if src.startswith("PROPOSED") else BLACK,
                                                          size=9, bold=src.startswith("PROPOSED"))
            r += 1
            _head(ws, r, ["Lower bound (>=)", "Upper bound (<)", "Sub-score", "Label"])
            r += 1
            first = r
            lower = -BIG
            for upper, score, label in bands:
                for col, val in ((1, lower), (2, BIG if upper == math.inf else upper), (3, score), (4, label)):
                    c = ws.cell(row=r, column=col, value=val)
                    c.font = _f(BLUE if col in (1, 2, 3) else BLACK)
                    if col in (1, 2):
                        c.number_format = "General"
                lower = upper
                r += 1
            self.thr[key] = (first, r - 1)
            r += 1
        # Beneish coefficients and sliding scale, Altman coefficients
        ws.cell(row=r, column=1, value="Beneish M-Score coefficients").font = _f(bold=True)
        r += 1
        self.ben = {}
        for k in ["const"] + R.BENEISH_PARTS:
            ws.cell(row=r, column=1, value=k.upper()).font = _f()
            c = ws.cell(row=r, column=3, value=R.BENEISH_COEF[k])
            c.font, c.number_format = _f(BLUE), "0.000"
            self.ben[k] = f"Thresholds!$C${r}"
            r += 1
        lo, hi = config.BENEISH_LINEAR
        ws.cell(row=r, column=1, value="Sliding scale: 100 below").font = _f()
        ws.cell(row=r, column=3, value=lo).font = _f(BLUE)
        self.ben_lo = f"Thresholds!$C${r}"
        ws.cell(row=r, column=4, value="Brief").font = _f(size=9)
        r += 1
        ws.cell(row=r, column=1, value="Sliding scale: 0 above").font = _f()
        ws.cell(row=r, column=3, value=hi).font = _f(BLUE)
        self.ben_hi = f"Thresholds!$C${r}"
        r += 2
        self.alt = {}
        for key in ("altman_z2", "altman_z1"):
            ws.cell(row=r, column=1, value=f"{R.ALTMAN_SHORT[key]} coefficients").font = _f(bold=True)
            r += 1
            for x in ("X1", "X2", "X3", "X4", "X5"):
                ws.cell(row=r, column=1, value=x).font = _f()
                c = ws.cell(row=r, column=3, value=R.ALTMAN_COEF[key].get(x, 0))
                c.font, c.number_format = _f(BLUE), "0.000"
                self.alt[(key, x)] = f"Thresholds!$C${r}"
                r += 1
            r += 1
        # governance lookups
        self.gov_tab = {}
        for name, table, caps in (("Auditor change", config.AUDITOR_EVENTS, config.AUDITOR_CAP_EVENTS),
                                  ("Audit opinion", config.AUDIT_OPINIONS, config.AUDITOR_CAP_OPINIONS)):
            ws.cell(row=r, column=1, value=f"{name} -> sub-score (PROPOSED - review)").font = _f(bold=True)
            r += 1
            _head(ws, r, ["Option", "Sub-score", "Triggers the cap at 50 (1 = yes)"])
            r += 1
            first = r
            for opt, sc in table.items():
                ws.cell(row=r, column=1, value=opt).font = _f()
                ws.cell(row=r, column=2, value=sc).font = _f(BLUE)
                ws.cell(row=r, column=3, value=1 if opt in caps else 0).font = _f(BLUE)
                r += 1
            self.gov_tab[name] = (first, r - 1)
            r += 1
        ws.cell(row=r, column=1, value="Promoter pledge % -> sub-score (PROPOSED - review)").font = _f(bold=True)
        r += 1
        _head(ws, r, ["Lower bound (>=)", "Upper bound (<)", "Sub-score"])
        r += 1
        first, lower = r, -BIG
        for upper, sc in config.PLEDGE_BANDS:
            ws.cell(row=r, column=1, value=lower).font = _f(BLUE)
            ws.cell(row=r, column=2, value=BIG if upper == math.inf else upper).font = _f(BLUE)
            ws.cell(row=r, column=3, value=sc).font = _f(BLUE)
            lower = upper
            r += 1
        self.pledge = (first, r - 1)
        r += 1
        ws.cell(row=r, column=1, value="Bands and verdicts (brief)").font = _f(bold=True)
        r += 1
        _head(ws, r, ["Score >=", "Band", "Verdict"])
        r += 1
        self.band_rows = []
        for lower_b, band, verdict, _c in config.BANDS:
            ws.cell(row=r, column=1, value=lower_b).font = _f(BLUE)
            ws.cell(row=r, column=2, value=band).font = _f()
            ws.cell(row=r, column=3, value=verdict).font = _f()
            self.band_rows.append(r)
            r += 1
        r += 1
        ws.cell(row=r, column=1, value="Override caps").font = _f(bold=True)
        r += 1
        self.caps = {}
        for k, label, v in (("cfo", "Negative CFO (2 consecutive years), if confirmed", config.CFO_NEGATIVE_CAP),
                            ("neq", "Negative net worth, if confirmed", config.NEG_EQUITY_CAP),
                            ("aud", "Auditor rule", config.AUDITOR_CAP),
                            ("rp", "Related-party advances / revenue above -> Escalate", config.RP_ESCALATE_THRESHOLD)):
            ws.cell(row=r, column=1, value=label).font = _f()
            ws.cell(row=r, column=3, value=v).font = _f(BLUE)
            self.caps[k] = f"Thresholds!$C${r}"
            r += 1
        for col, w in zip("ABCD", (44, 34, 14, 36)):
            ws.column_dimensions[col].width = w

    def band_score(self, key, value_cell):
        a, b = self.thr[key]
        return f"INDEX(Thresholds!$C${a}:$C${b},MATCH({value_cell},Thresholds!$A${a}:$A${b},1))"

    # ----------------------------------------------------------------- Weights
    def weights_sheet(self):
        ws = self.wb.create_sheet("Weights")
        ws["A1"] = "Weights (blue = editable). Each group must sum to 100%; the CCIRS recalculates when you change them."
        ws["A1"].font = _f(bold=True, size=12)
        _head(ws, 3, ["Pillar", "Name", "Weight (input)", "Normalised %"])
        r = 4
        self.pw = {}
        first = r
        for p, spec in config.PILLARS.items():
            ws.cell(row=r, column=1, value=p).font = _f()
            ws.cell(row=r, column=2, value=spec["name"]).font = _f()
            c = ws.cell(row=r, column=3, value=self.weights["pillars"][p])
            c.font, c.fill = _f(BLUE), INP
            self.pw[p] = r
            r += 1
        last = r - 1
        for p, row in self.pw.items():
            c = ws.cell(row=row, column=4, value=f"=IF(SUM($C${first}:$C${last})>0,C{row}/SUM($C${first}:$C${last})*100,0)")
            c.number_format = "0.0"
        ws.cell(row=r, column=2, value="Total / check").font = _f(bold=True)
        ws.cell(row=r, column=3, value=f"=SUM(C{first}:C{last})").font = _f(bold=True)
        ws.cell(row=r, column=4, value=f'=IF(ABS(SUM(C{first}:C{last})-100)<0.0001,"OK - sums to 100%",'
                                       f'"CHECK - sums to "&TEXT(SUM(C{first}:C{last}),"0.0")&"% (normalised)")')
        self.pw_check = f"Weights!$D${r}"
        r += 2
        _head(ws, r, ["Pillar", "Component", "Sub-weight (input)", "Normalised % in pillar"])
        r += 1
        self.sw = {}
        for p, spec in config.PILLARS.items():
            gfirst = r
            for k in spec["ratios"]:
                ws.cell(row=r, column=1, value=p).font = _f()
                ws.cell(row=r, column=2, value=S.component_label(k, self.book)).font = _f()
                c = ws.cell(row=r, column=3, value=self.weights["ratios"][p][k])
                c.font, c.fill = _f(BLUE), INP
                self.sw[k] = r
                r += 1
            glast = r - 1
            for k in spec["ratios"]:
                row = self.sw[k]
                ws.cell(row=row, column=4, value=f"=IF(SUM($C${gfirst}:$C${glast})>0,C{row}/SUM($C${gfirst}:$C${glast})*100,0)"
                        ).number_format = "0.0"
            ws.cell(row=r, column=2, value=f"Pillar {p} total / check").font = _f(bold=True)
            ws.cell(row=r, column=3, value=f"=SUM(C{gfirst}:C{glast})").font = _f(bold=True)
            ws.cell(row=r, column=4, value=f'=IF(ABS(SUM(C{gfirst}:C{glast})-100)<0.0001,"OK - sums to 100%",'
                                           f'"CHECK - sums to "&TEXT(SUM(C{gfirst}:C{glast}),"0.0")&"% (normalised)")')
            r += 2
        for col, w in zip("ABCD", (8, 40, 18, 34)):
            ws.column_dimensions[col].width = w

    # ----------------------------------------------------------------- Ratios
    def ratios_sheet(self):
        ws = self.wb.create_sheet("Ratios")
        self.ws_r = ws
        ws["A1"] = f"Ratios - live formulas for every CCIRS component ({self.fy}; prior year {self.py or 'n/a'})"
        ws["A1"].font = _f(bold=True, size=13)
        ws["A2"] = ("Status: OK; n/a (missing input or not meaningful - re-weighted); distress (negative EBITDA or "
                    "equity - scored 0); no debt (nothing to service - scored 100). App value pasted for comparison.")
        ws["A2"].font = _f(italic=True, size=9)
        ws["B4"] = "Tolerance - values (|Excel - app|)"
        ws["B4"].font = _f(bold=True)
        c = ws["D4"]
        c.value, c.font, c.fill, c.number_format = 0.0005, _f(BLUE), INP, "0.0000"
        self.tol = "Ratios!$D$4"
        _head(ws, 6, ["Component", "Metric", "Excel value (formula)", "Status (formula)", "App value", "Difference",
                      "Check", "Formula in words"])
        self.r = 7
        self.cells: dict[str, tuple[str, str]] = {}      # name -> (value cell, status cell)
        I, isn = self.I, "ISNUMBER"
        cur, pri = (lambda k: I(k)), (lambda k: I(k, False))
        opE, opEp = "Inputs!$D$%d" % self.rows["operating_ebitda"], "Inputs!$C$%d" % self.rows["operating_ebitda"]
        opB, opBp = "Inputs!$D$%d" % self.rows["operating_ebit"], "Inputs!$C$%d" % self.rows["operating_ebit"]
        incl = self.S_("include_noncurrent")
        b, fy = self.book, self.fy

        def bv(key, attr=None):
            res = b.get(key, fy)
            if not res.ok:
                return None
            return _num(res.inputs.get(attr)) if attr else _num(res.value)

        def liquid(year_cur=True):
            g = cur if year_cur else pri
            return f"({g('cash')}+{g('marketable_securities')}+IF(AND({incl},{isn}({g('noncurrent_liquid_investments')})),{g('noncurrent_liquid_investments')},0))"

        # A. Cash & Liquidity
        self.row("cce", "CCE = CFO / operating EBITDA",
                 f'=IF(AND({isn}({cur("cfo")}),{isn}({opE})),IF({opE}>0,{cur("cfo")}/{opE},"n/a"),"n/a")',
                 f'=IF(AND({isn}({cur("cfo")}),{isn}({opE})),IF({opE}>0,"OK",IF({opE}<0,"distress","n/a")),"n/a")',
                 bv("cce"), PCT, "Negative operating EBITDA = distress (scored 0).")
        self.row("ocf_ratio", "OCF ratio = CFO / current liabilities",
                 f'=IF(AND({isn}({cur("cfo")}),{isn}({cur("current_liabilities")})),IF({cur("current_liabilities")}>0,'
                 f'{cur("cfo")}/{cur("current_liabilities")},"n/a"),"n/a")', None, bv("ocf_ratio"), MULT, "")
        dir_in = [cur(k) for k in ("cash", "marketable_securities", "receivables", "cash_opex")]
        self.row("dir", "DIR (version that feeds the score), days",
                 f'=IF(COUNT({",".join(dir_in)})=4,IF({cur("cash_opex")}>0,({liquid()}+{cur("receivables")})/'
                 f'({cur("cash_opex")}/365),"n/a"),"n/a")', None, bv("dir"), DAYS,
                 "(Cash + liquid investments* + receivables) / (cash opex / 365); *non-current only if the setting is "
                 "TRUE and the figure is reported.")
        self.row("wc_days", "Working-capital days",
                 f'=IF(COUNT({cur("current_assets")},{cur("current_liabilities")},{cur("revenue")})=3,'
                 f'IF({cur("revenue")}>0,({cur("current_assets")}-{cur("current_liabilities")})/{cur("revenue")}*365,'
                 f'"n/a"),"n/a")', None, bv("wc_days"), DAYS, "")
        for tag, g in (("ccc_t", cur), ("ccc_p", pri)):
            ins = [g(k) for k in ("inventory", "cogs", "receivables", "revenue", "accounts_payable")]
            self.row(tag, f"CCC {'scoring' if tag == 'ccc_t' else 'prior'} year = DIO + DSO - DPO",
                     f'=IF(COUNT({",".join(ins)})=5,IF(AND({g("cogs")}>0,{g("revenue")}>0),{g("inventory")}/{g("cogs")}*365'
                     f'+{g("receivables")}/{g("revenue")}*365-{g("accounts_payable")}/{g("cogs")}*365,"n/a"),"n/a")',
                     None, bv("ccc") if tag == "ccc_t" else (_num(b.get("ccc", self.py).value) if self.py and
                                                             b.get("ccc", self.py).ok else None), DAYS, "", component=False)
        ct, cp = self.cells["ccc_t"][0], self.cells["ccc_p"][0]
        app_ct = bv("ccc")
        app_cp = _num(b.get("ccc", self.py).value) if self.py and b.get("ccc", self.py).ok else None
        self.row("ccc_trend", "CCC trend = CCC t - CCC t-1 (days)",
                 f'=IF(AND({isn}({ct}),{isn}({cp})),{ct}-{cp},"n/a")', None,
                 None if app_ct is None or app_cp is None else app_ct - app_cp, DAYS, "")
        # B. Solvency
        self.row("interest_coverage", "Interest coverage = operating EBIT / interest",
                 f'=IF(AND({isn}({opB}),{isn}({cur("interest_expense")})),IF({cur("interest_expense")}<>0,'
                 f'{opB}/{cur("interest_expense")},"n/a"),"n/a")',
                 f'=IF(AND({isn}({opB}),{isn}({cur("interest_expense")})),IF({cur("interest_expense")}<>0,"OK","no debt"),"n/a")',
                 bv("interest_coverage"), MULT, "No interest expense = no debt to service (scored 100).")
        self.row("de_total", "Debt/Equity (incl. leases)",
                 f'=IF(AND({isn}({cur("total_debt")}),{isn}({cur("equity")})),IF({cur("equity")}>0,'
                 f'{cur("total_debt")}/{cur("equity")},"n/a"),"n/a")',
                 f'=IF(AND({isn}({cur("total_debt")}),{isn}({cur("equity")})),IF({cur("equity")}>0,"OK",'
                 f'IF({cur("equity")}<0,"distress","n/a")),"n/a")', bv("de_total"), MULT, "Negative equity = distress.")
        nd_in = [cur(k) for k in ("total_debt", "cash", "marketable_securities")]
        self.row("net_debt_ebitda", "Net debt (incl. leases) / operating EBITDA",
                 f'=IF(AND(COUNT({",".join(nd_in)})=3,{isn}({opE})),IF({opE}>0,({cur("total_debt")}-{liquid()})/{opE},'
                 f'"n/a"),"n/a")',
                 f'=IF(AND(COUNT({",".join(nd_in)})=3,{isn}({opE})),IF({opE}>0,"OK",IF({opE}<0,"distress","n/a")),"n/a")',
                 bv("net_debt_ebitda"), MULT, "")
        ds_in = [cur(k) for k in ("cfo", "capex", "interest_expense", "current_debt")]
        self.row("dscr", "DSCR proxy = (CFO - capex) / (interest + current debt)",
                 f'=IF(COUNT({",".join(ds_in)})=4,IF({cur("interest_expense")}+{cur("current_debt")}>0,'
                 f'({cur("cfo")}-{cur("capex")})/({cur("interest_expense")}+{cur("current_debt")}),"n/a"),"n/a")',
                 f'=IF(COUNT({",".join(ds_in)})=4,IF({cur("interest_expense")}+{cur("current_debt")}>0,"OK","no debt"),"n/a")',
                 bv("dscr"), MULT, "Nothing falling due = scored 100.")
        # C. Earnings integrity
        ta, tap = cur("total_assets"), pri("total_assets")
        self.row("sloan", "Sloan = (PAT - CFO) / average total assets",
                 f'=IF(COUNT({cur("pat")},{cur("cfo")},{ta},{tap})=4,IF(({ta}+{tap})/2>0,({cur("pat")}-{cur("cfo")})/'
                 f'(({ta}+{tap})/2),"n/a"),"n/a")', None, bv("sloan"), PCT, "")
        noa = lambda g: f"(({g('total_assets')}-{g('cash')}-{g('marketable_securities')})-({g('total_liabilities')}-{g('total_debt')}))"
        noa_in = [g(k) for g in (cur, pri) for k in ("total_assets", "cash", "marketable_securities", "total_liabilities",
                                                     "total_debt")]
        self.row("bs_accrual", "Balance-sheet accruals = change in NOA / average NOA",
                 f'=IF(COUNT({",".join(noa_in)})=10,IF(({noa(cur)}+{noa(pri)})/2>0,({noa(cur)}-{noa(pri)})/'
                 f'(({noa(cur)}+{noa(pri)})/2),"n/a"),"n/a")', None, bv("bs_accrual"), PCT,
                 "NOA = (TA - cash - current liquid investments) - (TL - total debt).")
        self._beneish(cur, pri, bv)
        rv, rvp, rc, rcp = cur("revenue"), pri("revenue"), cur("receivables"), pri("receivables")
        self.row("rec_vs_sales", "Receivables growth - revenue growth",
                 f'=IF(COUNT({rv},{rvp},{rc},{rcp})=4,IF(AND({rcp}>0,{rvp}>0),({rc}/{rcp}-1)-({rv}/{rvp}-1),"n/a"),"n/a")',
                 None, bv("rec_vs_sales"), PCT, "", component=False)
        # D. Distress & governance
        self._altman(cur, opB, bv)
        self._piotroski(cur, pri)
        self.row("rp_advances", "Related-party advances / revenue",
                 f'=IF(AND({isn}({cur("related_party_advances")}),{isn}({rv})),IF({rv}>0,{cur("related_party_advances")}/'
                 f'{rv},"n/a"),"n/a")', None, bv("rp_advances_revenue"), PCT, "Optional analyst input.")
        a1, a2 = self.gov_tab["Auditor change"]
        o1, o2 = self.gov_tab["Audit opinion"]
        p1, p2 = self.pledge
        gov_app = next((c.sub_score for c in self.res.components if c.key == "governance"), None)
        self.row("governance", "Auditor & promoter flags = MIN(auditor, opinion, pledge sub-scores)",
                 f'=IF({self.S_("gov_reviewed")},MIN(INDEX(Thresholds!$B${a1}:$B${a2},MATCH({self.S_("auditor_event")},'
                 f'Thresholds!$A${a1}:$A${a2},0)),INDEX(Thresholds!$B${o1}:$B${o2},MATCH({self.S_("opinion")},'
                 f'Thresholds!$A${o1}:$A${o2},0)),INDEX(Thresholds!$C${p1}:$C${p2},MATCH({self.S_("pledge")},'
                 f'Thresholds!$A${p1}:$A${p2},1))),"n/a")', None, gov_app, SCORE,
                 "n/a until the analyst ticks 'reviewed' (component re-weighted out).")
        for col, w in zip("ABCDEFGH", (18, 46, 18, 14, 16, 13, 16, 60)):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "C7"

    def row(self, name, label, formula, status_formula, app_value, fmt, words, component=True):
        ws, r = self.ws_r, self.r
        ws.cell(row=r, column=1, value=name).font = _f(GREY if not component else BLACK, size=9)
        ws.cell(row=r, column=2, value=label).font = _f()
        c = ws.cell(row=r, column=3, value=formula)
        c.font, c.number_format = _f(), fmt
        st = status_formula or f'=IF(ISNUMBER(C{r}),"OK","n/a")'
        ws.cell(row=r, column=4, value=st).font = _f()
        a = ws.cell(row=r, column=5, value=app_value if app_value is not None else "n/a")
        a.number_format = fmt
        ws.cell(row=r, column=6, value=f'=IF(AND(ISNUMBER(C{r}),ISNUMBER(E{r})),C{r}-E{r},"n/a")').number_format = fmt
        ws.cell(row=r, column=7, value=(f'=IF(AND(ISNUMBER(C{r}),ISNUMBER(E{r})),IF(ABS(C{r}-E{r})<={self.tol},"PASS",'
                                        f'"FAIL"),IF(AND(NOT(ISNUMBER(C{r})),NOT(ISNUMBER(E{r}))),"PASS (both n/a)",'
                                        f'"CHECK"))')).font = _f(bold=True)
        ws.cell(row=r, column=8, value=words).font = _f(size=9)
        for j in range(1, 9):
            ws.cell(row=r, column=j).border = Border(bottom=THIN)
        self.cells[name] = (f"Ratios!$C${r}", f"Ratios!$D${r}")
        self.r += 1

    def _beneish(self, cur, pri, bv):
        isn = "ISNUMBER"
        rv, rvp = cur("revenue"), pri("revenue")
        comp = {
            "dsri": (f'=IF(COUNT({cur("receivables")},{rv},{pri("receivables")},{rvp})=4,IF(AND({rv}>0,{rvp}>0,'
                     f'{pri("receivables")}<>0),({cur("receivables")}/{rv})/({pri("receivables")}/{rvp}),"n/a"),"n/a")'),
            "gmi": (f'=IF(COUNT({rv},{cur("cogs")},{rvp},{pri("cogs")})=4,IF(AND({rv}>0,{rvp}>0),IF(({rv}-{cur("cogs")})/'
                    f'{rv}>0,(({rvp}-{pri("cogs")})/{rvp})/(({rv}-{cur("cogs")})/{rv}),"n/a"),"n/a"),"n/a")'),
            "aqi": (f'=IF(COUNT({cur("current_assets")},{cur("ppe")},{cur("total_assets")},{pri("current_assets")},'
                    f'{pri("ppe")},{pri("total_assets")})=6,IF(AND({cur("total_assets")}>0,{pri("total_assets")}>0),'
                    f'IF(1-({pri("current_assets")}+{pri("ppe")})/{pri("total_assets")}<>0,(1-({cur("current_assets")}+'
                    f'{cur("ppe")})/{cur("total_assets")})/(1-({pri("current_assets")}+{pri("ppe")})/{pri("total_assets")}),'
                    f'"n/a"),"n/a"),"n/a")'),
            "sgi": f'=IF(COUNT({rv},{rvp})=2,IF({rvp}>0,{rv}/{rvp},"n/a"),"n/a")',
            "depi": (f'=IF(COUNT({cur("depreciation")},{cur("ppe")},{pri("depreciation")},{pri("ppe")})=4,'
                     f'IF(AND({cur("depreciation")}+{cur("ppe")}>0,{pri("depreciation")}+{pri("ppe")}>0,'
                     f'{cur("depreciation")}<>0),({pri("depreciation")}/({pri("depreciation")}+{pri("ppe")}))/'
                     f'({cur("depreciation")}/({cur("depreciation")}+{cur("ppe")})),"n/a"),"n/a")'),
            "sgai": (f'=IF(COUNT({cur("sga")},{rv},{pri("sga")},{rvp})=4,IF(AND({rv}>0,{rvp}>0,{pri("sga")}<>0),'
                     f'({cur("sga")}/{rv})/({pri("sga")}/{rvp}),"n/a"),"n/a")'),
            "tata": (f'=IF(COUNT({cur("pat")},{cur("cfo")},{cur("total_assets")})=3,IF({cur("total_assets")}>0,'
                     f'({cur("pat")}-{cur("cfo")})/{cur("total_assets")},"n/a"),"n/a")'),
            "lvgi": (f'=IF(COUNT({cur("current_liabilities")},{cur("long_term_debt")},{cur("total_assets")},'
                     f'{pri("current_liabilities")},{pri("long_term_debt")},{pri("total_assets")})=6,'
                     f'IF(AND({cur("total_assets")}>0,{pri("total_assets")}>0,({pri("current_liabilities")}+'
                     f'{pri("long_term_debt")})/{pri("total_assets")}<>0),(({cur("current_liabilities")}+'
                     f'{cur("long_term_debt")})/{cur("total_assets")})/(({pri("current_liabilities")}+'
                     f'{pri("long_term_debt")})/{pri("total_assets")}),"n/a"),"n/a")'),
        }
        for k, f in comp.items():
            self.row(k, f"Beneish {k.upper()}", f, None, bv(k), DEC, R.METRICS[k][3], component=False)
        cells = [self.cells[k][0] for k in R.BENEISH_PARTS]
        terms = "+".join(f"{self.ben[k]}*{cells[i]}" for i, k in enumerate(R.BENEISH_PARTS))
        self.row("beneish_m", "Beneish M-Score (8-variable)",
                 f'=IF(COUNT({",".join(cells)})=8,{self.ben["const"]}+{terms},"n/a")', None, bv("beneish_m"), DEC,
                 "Reported only when all eight components compute; otherwise re-weighted.")

    def _altman(self, cur, opB, bv):
        isn = "ISNUMBER"
        ta, tl = cur("total_assets"), cur("total_liabilities")
        guard = f'COUNT({cur("current_assets")},{cur("current_liabilities")},{cur("retained_earnings")},{ta},{tl},' \
                f'{cur("equity")},{opB})=7'
        xf = {"X1": f'({cur("current_assets")}-{cur("current_liabilities")})/{ta}', "X2": f'{cur("retained_earnings")}/{ta}',
              "X3": f"{opB}/{ta}", "X4": f'{cur("equity")}/{tl}', "X5": f'{cur("revenue")}/{ta}'}
        src = self.book.altman_key
        for x, expr in xf.items():
            g = guard if x != "X5" else f"AND({guard},{isn}({cur('revenue')}))"
            self.row(f"altman_{x}", f"Altman {x}", f'=IF({g},IF(AND({ta}>0,{tl}>0),{expr},"n/a"),"n/a")', None,
                     bv("altman_z1" if x == "X5" else src, x), DEC, "", component=False)
        var = self.S_("altman_variant")
        z2 = "+".join(f'{self.alt[("altman_z2", x)]}*{self.cells["altman_" + x][0]}' for x in ("X1", "X2", "X3", "X4"))
        z1 = "+".join(f'{self.alt[("altman_z1", x)]}*{self.cells["altman_" + x][0]}' for x in ("X1", "X2", "X3", "X4", "X5"))
        x4 = ",".join(self.cells["altman_" + x][0] for x in ("X1", "X2", "X3", "X4"))
        x5 = ",".join(self.cells["altman_" + x][0] for x in ("X1", "X2", "X3", "X4", "X5"))
        self.row("altman", S.component_label("altman", self.book),
                 f'=IF({var}="Z\'",IF(COUNT({x5})=5,{z1},"n/a"),IF(COUNT({x4})=4,{z2},"n/a"))', None, bv("altman"), DEC,
                 "Z'' = 6.56X1+3.26X2+6.72X3+1.05X4 (no +3.25); Z' = 0.717X1+0.847X2+3.107X3+0.420X4+0.998X5.")

    def _piotroski(self, cur, pri):
        isn = "ISNUMBER"
        g = {k: cur(k) for k in ("pat", "cfo", "total_assets", "long_term_debt", "current_assets", "current_liabilities",
                                 "shares_outstanding", "revenue", "cogs")}
        p = {k: pri(k) for k in ("pat", "total_assets", "long_term_debt", "current_assets", "current_liabilities",
                                 "shares_outstanding", "revenue", "cogs")}

        def ok(*cells):
            return "AND(" + ",".join(f"{isn}({c})" for c in cells) + ")"

        roa, roap = f'{g["pat"]}/{g["total_assets"]}', f'{p["pat"]}/{p["total_assets"]}'
        lev, levp = f'{g["long_term_debt"]}/{g["total_assets"]}', f'{p["long_term_debt"]}/{p["total_assets"]}'
        cr, crp = f'{g["current_assets"]}/{g["current_liabilities"]}', f'{p["current_assets"]}/{p["current_liabilities"]}'
        gm, gmp = f'({g["revenue"]}-{g["cogs"]})/{g["revenue"]}', f'({p["revenue"]}-{p["cogs"]})/{p["revenue"]}'
        ato, atop = f'{g["revenue"]}/{g["total_assets"]}', f'{p["revenue"]}/{p["total_assets"]}'
        crit = [
            ("ROA > 0", ok(g["pat"], g["total_assets"]) + f'*({g["total_assets"]}<>0)', f"{roa}>0"),
            ("CFO > 0", ok(g["cfo"]), f'{g["cfo"]}>0'),
            ("ROA improved", ok(g["pat"], g["total_assets"], p["pat"], p["total_assets"])
             + f'*({g["total_assets"]}<>0)*({p["total_assets"]}<>0)', f"{roa}>{roap}"),
            ("CFO > net income", ok(g["cfo"], g["pat"]), f'{g["cfo"]}>{g["pat"]}'),
            ("Long-term debt / TA fell", ok(g["long_term_debt"], g["total_assets"], p["long_term_debt"], p["total_assets"])
             + f'*({g["total_assets"]}<>0)*({p["total_assets"]}<>0)', f"OR({lev}<{levp},AND({lev}=0,{levp}=0))"),
            ("Current ratio improved", ok(g["current_assets"], g["current_liabilities"], p["current_assets"],
                                          p["current_liabilities"]) + f'*({g["current_liabilities"]}<>0)*({p["current_liabilities"]}<>0)',
             f"{cr}>{crp}"),
            ("No new shares", ok(g["shares_outstanding"], p["shares_outstanding"]),
             f'{g["shares_outstanding"]}<={p["shares_outstanding"]}'),
            ("Gross margin improved", ok(g["revenue"], g["cogs"], p["revenue"], p["cogs"])
             + f'*({g["revenue"]}<>0)*({p["revenue"]}<>0)', f"{gm}>{gmp}"),
            ("Asset turnover improved", ok(g["revenue"], g["total_assets"], p["revenue"], p["total_assets"])
             + f'*({g["total_assets"]}<>0)*({p["total_assets"]}<>0)', f"{ato}>{atop}"),
        ]
        first = self.r
        res = self.book.get("piotroski", self.fy)
        app_c = {d["Criterion"][:2]: d["Result"] for d in res.details}
        for i, (name, cond_ok, test) in enumerate(crit, 1):
            app = app_c.get(f"{i}.")
            appv = 1 if app == "Pass" else 0 if app == "Fail" else None
            self.row(f"piotroski_{i}", f"Piotroski {i}. {name} (1 = pass)", f'=IF({cond_ok},IF({test},1,0),"n/a")',
                     None, appv, "0", "", component=False)
        rng = f"Ratios!$C${first}:$C${self.r - 1}"
        app_score = res.extra.get("score_0_100") if res.ok and res.extra else None
        self.row("piotroski", "Piotroski score = passed / assessable x 100 (needs >= "
                 f"{config.PIOTROSKI_MIN_ASSESSABLE} assessable)",
                 f'=IF(COUNT({rng})>={config.PIOTROSKI_MIN_ASSESSABLE},SUM({rng})/COUNT({rng})*100,"n/a")', None,
                 app_score, SCORE, "Missing criteria are excluded, not counted as failures.")

    # ----------------------------------------------------------------- Scoring
    def scoring_sheet(self):
        ws = self.wb.create_sheet("Scoring")
        ws["A1"] = "Scoring - sub-scores, re-weighting, pillars, CCIRS, overrides, band and verdict (all formulas)"
        ws["A1"].font = _f(bold=True, size=13)
        ws["B3"] = "Tolerance - scores (|Excel - app|)"
        ws["B3"].font = _f(bold=True)
        c = ws["D3"]
        c.value, c.font, c.fill, c.number_format = 0.005, _f(BLUE), INP, "0.000"
        tol = "Scoring!$D$3"
        heads = ["Pillar", "Component", "Included (applicability)", "Value", "Status", "Sub-score (formula)",
                 "App sub-score", "Check", "Sub-weight %", "Usable weight", "Weight in pillar %", "Pillar contribution",
                 "CCIRS points"]
        _head(ws, 5, heads)
        r = 6
        comp_rows = {}
        app_sub = {c.key: c for c in self.res.components}
        first = r
        for p, spec in config.PILLARS.items():
            for k in spec["ratios"]:
                comp_rows[k] = r
                r += 1
        last = r - 1
        for p, spec in config.PILLARS.items():
            for k in spec["ratios"]:
                row = comp_rows[k]
                comp = app_sub[k]
                ws.cell(row=row, column=1, value=p).font = _f()
                ws.cell(row=row, column=2, value=S.component_label(k, self.book)).font = _f()
                inc = ws.cell(row=row, column=3, value=comp.status != S.EXCLUDED)
                inc.font, inc.fill = _f(BLUE), INP
                if k == "dsri_rec":
                    dv, ds = self.cells["dsri"]
                    rv_, rs_ = self.cells["rec_vs_sales"]
                    ws.cell(row=row, column=4, value=f"=IF(ISNUMBER({dv}),{dv},{rv_})").font = _f(GREEN)
                    ws.cell(row=row, column=5, value=f'=IF(OR(ISNUMBER({dv}),ISNUMBER({rv_})),"OK","n/a")')
                    s1 = f'IF(ISNUMBER({dv}),{self.band_score("dsri", dv)},"")'
                    s2 = f'IF(ISNUMBER({rv_}),{self.band_score("rec_vs_sales", rv_)},"")'
                    sub = f'=IF(COUNT({dv},{rv_})=0,"n/a",(N({s1})+N({s2}))/COUNT({dv},{rv_}))'
                else:
                    vcell, scell = self.cells[k]
                    ws.cell(row=row, column=4, value=f"={vcell}").font = _f(GREEN)
                    ws.cell(row=row, column=5, value=f"={scell}").font = _f(GREEN)
                    V, ST = f"D{row}", f"E{row}"
                    if k == "beneish_m":
                        core = f"IF({V}<{self.ben_lo},100,IF({V}>{self.ben_hi},0,100*({self.ben_hi}-{V})/({self.ben_hi}-{self.ben_lo})))"
                    elif k in ("piotroski", "governance"):
                        core = V
                    elif k == "altman":
                        core = (f'IF({self.S_("altman_variant")}="Z\'",{self.band_score("altman_z1", V)},'
                                f'{self.band_score("altman_z2", V)})')
                    elif k == "rp_advances":
                        core = self.band_score("rp_advances_revenue", V)
                    else:
                        core = self.band_score(k, V)
                    sub = (f'=IF({ST}="distress",{config.DISTRESS_NM_SCORE},IF({ST}="no debt",100,'
                           f'IF(ISNUMBER({V}),{core},"n/a")))')
                ws.cell(row=row, column=6, value=f'=IF(NOT(C{row}),"excluded",{sub[1:]})').font = _f(bold=True)
                app_v = "excluded" if comp.status == S.EXCLUDED else (comp.sub_score if comp.sub_score is not None else "n/a")
                ws.cell(row=row, column=7, value=app_v).number_format = SCORE
                ws.cell(row=row, column=8, value=(f'=IF(AND(ISNUMBER(F{row}),ISNUMBER(G{row})),IF(ABS(F{row}-G{row})<={tol},'
                                                  f'"PASS","FAIL"),IF(F{row}=G{row},"PASS (both "&F{row}&")","CHECK"))')
                        ).font = _f(bold=True)
                ws.cell(row=row, column=9, value=f"=Weights!$D${self.sw[k]}").font = _f(GREEN)
                ws.cell(row=row, column=10, value=f"=IF(ISNUMBER(F{row}),I{row},0)")
                ws.cell(row=row, column=11, value=f'=IF(SUMIF($A${first}:$A${last},A{row},$J${first}:$J${last})>0,'
                                                  f'J{row}/SUMIF($A${first}:$A${last},A{row},$J${first}:$J${last})*100,0)')
                ws.cell(row=row, column=12, value=f"=IF(ISNUMBER(F{row}),F{row}*K{row}/100,0)")
                for j in (6, 7, 9, 10, 11, 12):
                    ws.cell(row=row, column=j).number_format = SCORE
        # pillars
        r = last + 2
        _head(ws, r, ["Pillar", "Name", "Pillar weight %", "Pillar score (formula)", "App pillar score", "Check",
                      "Weight if available", "Effective weight %", "Score x weight"])
        r += 1
        pfirst = r
        prow = {}
        app_p = {p.key: p for p in self.res.pillars}
        for p, spec in config.PILLARS.items():
            prow[p] = r
            ws.cell(row=r, column=1, value=p).font = _f()
            ws.cell(row=r, column=2, value=spec["name"]).font = _f()
            ws.cell(row=r, column=3, value=f"=Weights!$D${self.pw[p]}").font = _f(GREEN)
            ws.cell(row=r, column=4, value=f'=IF(SUMIF($A${first}:$A${last},A{r},$J${first}:$J${last})>0,'
                                           f'SUMIF($A${first}:$A${last},A{r},$L${first}:$L${last}),"n/a")').font = _f(bold=True)
            ap = app_p[p].score
            if app_p[p].limited and app_p[p].note and "alternative" in app_p[p].note:
                ap = None
            ws.cell(row=r, column=5, value=ap if ap is not None else "n/a").number_format = SCORE
            ws.cell(row=r, column=6, value=(f'=IF(AND(ISNUMBER(D{r}),ISNUMBER(E{r})),IF(ABS(D{r}-E{r})<={tol},"PASS",'
                                            f'"FAIL"),IF(D{r}=E{r},"PASS (both n/a)","CHECK"))')).font = _f(bold=True)
            ws.cell(row=r, column=7, value=f"=IF(ISNUMBER(D{r}),C{r},0)")
            for j in (3, 4, 5, 7):
                ws.cell(row=r, column=j).number_format = SCORE
            r += 1
        plast = r - 1
        for p, row in prow.items():
            ws.cell(row=row, column=8, value=f"=IF(SUM($G${pfirst}:$G${plast})>0,G{row}/SUM($G${pfirst}:$G${plast})*100,0)"
                    ).number_format = SCORE
            ws.cell(row=row, column=9, value=f"=IF(ISNUMBER(D{row}),D{row}*H{row}/100,0)").number_format = SCORE
        # CCIRS points per component
        for k, row in comp_rows.items():
            p = next(pp for pp, spec in config.PILLARS.items() if k in spec["ratios"])
            ws.cell(row=row, column=13, value=f"=L{row}*$H${prow[p]}/100").number_format = SCORE
        # totals and overrides
        r = plast + 2
        lines = []

        def line(label, formula, app_value, fmt=SCORE, check=True, text=False):
            nonlocal r
            ws.cell(row=r, column=2, value=label).font = _f(bold=True)
            ws.cell(row=r, column=4, value=formula).font = _f(bold=True)
            ws.cell(row=r, column=4).number_format = fmt
            if app_value is not None or check:
                ws.cell(row=r, column=5, value=app_value if app_value is not None else "n/a").number_format = fmt
            if check:
                f = (f'=IF(D{r}=E{r},"PASS","FAIL")' if text else
                     f'=IF(AND(ISNUMBER(D{r}),ISNUMBER(E{r})),IF(ABS(D{r}-E{r})<={tol},"PASS","FAIL"),'
                     f'IF(D{r}=E{r},"PASS (both n/a)","CHECK"))')
                ws.cell(row=r, column=6, value=f).font = _f(bold=True)
            lines.append(r)
            r += 1
            return f"D{r - 1}"

        raw = line("Weighted score before overrides", f'=IF(SUM($G${pfirst}:$G${plast})>0,SUM($I${pfirst}:$I${plast}),"n/a")',
                   self.res.raw_score)
        line("Sum of CCIRS points (check: equals the line above)", f"=SUM(M{first}:M{last})", self.res.raw_score)
        cfo_t, cfo_p, eq_t = self.I("cfo"), self.I("cfo", False), self.I("equity")
        cfo_cond = line("Override trigger: CFO negative in scoring and prior year",
                        f'=AND(ISNUMBER({cfo_t}),ISNUMBER({cfo_p}),N({cfo_t})<0,N({cfo_p})<0)', None, "General", False)
        neq_cond = line("Override trigger: negative net worth in scoring year",
                        f'=AND(ISNUMBER({eq_t}),N({eq_t})<0)', None, "General", False)
        a1, a2 = self.gov_tab["Auditor change"]
        o1, o2 = self.gov_tab["Audit opinion"]
        aud_cond = line("Override trigger: auditor rule (reviewed AND cap event or opinion)",
                        f'=AND({self.S_("gov_reviewed")},OR(INDEX(Thresholds!$C${a1}:$C${a2},MATCH({self.S_("auditor_event")},'
                        f'Thresholds!$A${a1}:$A${a2},0))=1,INDEX(Thresholds!$C${o1}:$C${o2},MATCH({self.S_("opinion")},'
                        f'Thresholds!$A${o1}:$A${o2},0))=1))', None, "General", False)
        cfo_apply = f'AND({cfo_cond},LEFT({self.S_("cfo_decision")},7)="Confirm")'
        neq_apply = f'AND({neq_cond},LEFT({self.S_("neq_decision")},7)="Confirm")'
        final = line("CCIRS (after overrides)",
                     f'=IF(ISNUMBER({raw}),MIN({raw},IF({cfo_apply},{self.caps["cfo"]},1E+99),'
                     f'IF({neq_apply},{self.caps["neq"]},1E+99),IF({aud_cond},{self.caps["aud"]},1E+99)),"n/a")',
                     self.res.score)
        br = self.band_rows
        band_pre = (f'IF({final}>=Thresholds!$A${br[0]},Thresholds!$B${br[0]},IF({final}>=Thresholds!$A${br[1]},'
                    f'Thresholds!$B${br[1]},IF({final}>=Thresholds!$A${br[2]},Thresholds!$B${br[2]},Thresholds!$B${br[3]})))')
        verd_pre = (f'IF({final}>=Thresholds!$A${br[0]},Thresholds!$C${br[0]},IF({final}>=Thresholds!$A${br[1]},'
                    f'Thresholds!$C${br[1]},IF({final}>=Thresholds!$A${br[2]},Thresholds!$C${br[2]},Thresholds!$C${br[3]})))')
        rp = self.cells["rp_advances"][0]
        rp_cond = line("Related-party advances above the Escalate threshold",
                       f'=AND(ISNUMBER({rp}),N({rp})>{self.caps["rp"]})', None, "General", False)
        esc = f'AND({rp_cond},OR({verd_pre}="Approve",{verd_pre}="Approve with conditions"))'
        line("Band", f'=IF(ISNUMBER({final}),IF({esc},"Orange",{band_pre}),"n/a")', self.res.band, "General", True, True)
        line("Verdict", f'=IF(ISNUMBER({final}),IF({esc},"Escalate for forensic review",{verd_pre}),"Insufficient data")',
             self.res.verdict, "General", True, True)
        ws.cell(row=r + 1, column=2, value=("Override decisions and governance inputs are on the Inputs sheet (blue). "
                                            "'Confirm...' applies a cap only when its trigger is TRUE.")).font = _f(size=9)
        for col, w in zip("ABCDEFGHIJKLM", (7, 52, 12, 16, 14, 14, 13, 14, 12, 12, 13, 13, 12)):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "C6"
        self.final_cell = f"Scoring!{final}"
        self.raw_cell = f"Scoring!{raw}"

    def notes(self):
        ws = self.wb.create_sheet("Notes")
        text = [
            ("How to use this workbook", True),
            ("1. Inputs: blue cells hold the figures and analyst inputs the app used. Overwrite them with annual-report "
             "figures (Rs crore, consolidated) to see how the CCIRS changes.", False),
            ("2. Ratios: every CCIRS component is a live formula. Status 'distress' scores 0, 'no debt' scores 100, "
             "'n/a' is re-weighted within its pillar.", False),
            ("3. Thresholds: bands from config.py. Rows marked 'PROPOSED - review' are the author's proposals; 'Brief' "
             "and 'Analyst-specified' come from the project brief or the analyst.", False),
            ("4. Weights: change any blue weight; each group is normalised and must sum to 100% (check cells).", False),
            ("5. Scoring: sub-scores -> re-weighting -> pillar scores -> CCIRS -> overrides -> band and verdict. "
             "Check columns compare every Excel value with the app's value (tolerance on the sheet).", False),
            ("Differences from the app are expected only if you change an input or if the app used a figure supplied "
             "directly (for example an uploaded operating EBITDA).", False),
            ("Screening tool - not proof of manipulation or default. Weights are judgement-based. For academic "
             "purposes only.", False),
        ]
        for i, (t, bold) in enumerate(text, 1):
            c = ws.cell(row=i, column=1, value=t)
            c.font, c.alignment = _f(bold=bold, size=12 if i == 1 else 10), Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions["A"].width = 130

    def build(self) -> bytes:
        self.inputs()
        self.thresholds()
        self.weights_sheet()
        self.ratios_sheet()
        self.scoring_sheet()
        self.notes()
        order = ["Inputs", "Ratios", "Thresholds", "Weights", "Scoring", "Notes"]
        self.wb._sheets = [self.wb[n] for n in order]
        buf = io.BytesIO()
        self.wb.save(buf)
        return buf.getvalue()


def build_calc_workbook(fd, book, ccirs, weights=None, governance=None, decisions=None, settings=None) -> bytes:
    return CalcBook(fd, book, ccirs, weights, governance, decisions, settings).build()
