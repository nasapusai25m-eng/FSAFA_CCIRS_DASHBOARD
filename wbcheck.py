"""Shared workbook checks for the tests.

static_or_fail(): always runs (any Python): >min formulas, no missing sheets/cells, no error literals.
evaluator_or_skip(): returns a pycel ExcelCompiler, or calls pytest.skip with a clear reason when pycel cannot run
(pycel 1.0b30 uses ast.Str, removed in Python 3.14+)."""
import io

import pytest
from openpyxl import load_workbook

import handcheck as HC

PYCEL_SKIP = "pycel not compatible with Python 3.14+; open the workbook in Excel to recalculate"


def static_or_fail(raw: bytes, min_formulas: int = 100):
    wb = load_workbook(io.BytesIO(raw))
    res = HC.static_issues(wb)
    assert res["formulas"] > min_formulas, res["formulas"]
    assert not res["problems"], res["problems"][:10]
    return wb


def evaluator_or_skip(path: str, probe: str):
    """probe: a formula cell evaluated once to prove pycel works on this Python."""
    try:
        from pycel import ExcelCompiler
        xl = ExcelCompiler(filename=path)
        xl.evaluate(probe)
        return xl
    except (ImportError, AttributeError, SyntaxError, TypeError) as e:   # e.g. "module 'ast' has no attribute 'Str'"
        pytest.skip(f"{PYCEL_SKIP} ({type(e).__name__}: {str(e)[:80]})")
