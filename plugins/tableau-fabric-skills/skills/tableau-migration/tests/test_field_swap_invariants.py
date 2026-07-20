"""Defect-1 invariant lock (v1.80.0): one representation per swap calc.

Background. A Tableau ``CASE/IF [Parameters].[X] WHEN <lit> THEN [field] ...`` swap calc is
translated through *exactly one* mode: a measure-value swap becomes a Power BI **field parameter**
(a disconnected NAMEOF table + synthesized aggregating measures), a dimension swap becomes its own
field-parameter table, and anything not representable that way falls through to a measure/column.
``assemble_model`` derives a single ``consumed`` set from ``emit_field_parameters`` and threads that
same set into BOTH the calc-column path and the measure path, so a consumed swap is never *also*
emitted as a data-model measure or calculated column.

The agent post-mortem alleged the opposite -- that one swap could be emitted as a field-parameter
table AND a measure/column in the same run. That contradiction is already impossible in the code
(verified 2026-07-20), but it was only enforced implicitly by the shared ``consumed`` plumbing. These
are **property** tests over the generator: for a class of swap-bearing workbooks, no name in
``report["field_parameters"]["consumed"]`` may appear as a ``measure`` anywhere or as a ``column`` on
any table other than its own field-parameter table. Pass once and the whole class of double-emit
regressions stays unrepresentable, independent of any single fixture.
"""
import re

import pytest

from assemble_model import assemble_import_model
from connection_to_m import parse_tds
from test_connection_to_m import LIVE_SQLSERVER


# -- TMDL declaration scan (quoted or bare object names) ------------------------------------
def _declared(text, keyword):
    """Return the set of object names declared with ``keyword`` (measure/column) in TMDL ``text``.

    Matches ``<keyword> 'Quoted Name' = ...``, ``<keyword> "Name"`` and ``<keyword> Bare Name``
    forms up to a ``=`` or end of line; only lines whose first token is the keyword qualify, so
    indented property lines (``dataType:`` etc.) and DAX bodies are ignored.
    """
    names = set()
    pat = re.compile(rf"^\s*{keyword}\s+(?:'([^']+)'|\"([^\"]+)\"|(.+?))\s*(?:=|$)")
    for raw in text.splitlines():
        m = pat.match(raw.rstrip())
        if not m:
            continue
        nm = m.group(1) or m.group(2) or (m.group(3) or "").strip()
        if nm:
            names.add(nm)
    return names


def _num_param(caption="Sales Multiplier", internal="[sm]", default="1.0",
               mn="0.0", mx="2.0", step="0.1", fmt=None):
    return {"caption": caption, "internal_name": internal, "datatype": "real", "domain": "range",
            "default": default, "format": fmt, "range": {"min": mn, "max": mx, "step": step},
            "members": [], "aliases": {}}


# -- scenario matrix: a class of swap-bearing workbooks -------------------------------------
_MEASURE_PICKER = {"caption": "Measure Picker", "internal_name": "[mp]", "datatype": "string",
                   "domain": "list", "default": "1", "format": None, "range": None,
                   "members": ["1", "2"], "aliases": {"1": "Total Sales", "2": "Units"}}
_DIM_SELECTOR = {"caption": "Dim Selector", "internal_name": "[ds]", "datatype": "string",
                 "domain": "list", "default": "1", "format": None, "range": None,
                 "members": ["1", "2"], "aliases": {"1": "By Order", "2": "By Sales"}}

_MEASURE_SWAP = {"name": "Measure Swap",
                 "formula": "CASE [Parameters].[Measure Picker] WHEN 1 THEN [Sales] "
                            "WHEN 2 THEN [Quantity] END"}
_DIM_SWAP = {"name": "Dim Swap", "role": "dimension",
             "formula": "CASE [Parameters].[Dim Selector] WHEN 1 THEN [Order ID] "
                        "WHEN 2 THEN [Sales] END"}

_SCENARIOS = {
    "measure-swap-only": {
        "params": [_MEASURE_PICKER],
        "calcs": [_MEASURE_SWAP],
        "dim_calcs": [],
        "expect_consumed": {"Measure Swap"},
    },
    "dim-swap-only": {
        "params": [_DIM_SELECTOR],
        "calcs": [],
        "dim_calcs": [_DIM_SWAP],
        "expect_consumed": {"Dim Swap"},
    },
    "measure-and-dim-swap-with-neighbors": {
        # both swaps consumed, alongside a legitimate what-if measure (Boost), a plain measure,
        # and a row-level parameter flag that must stay a stub -- the invariant must exclude the
        # consumed swaps WITHOUT collateral-dropping the real measures around them.
        "params": [_MEASURE_PICKER, _DIM_SELECTOR,
                   _num_param(caption="Sales Multiplier", internal="[sm]")],
        "calcs": [_MEASURE_SWAP,
                  {"name": "Boost", "formula": "SUM([Sales]) * [Parameters].[Sales Multiplier]"},
                  {"name": "Profit Ratio", "formula": "SUM([Sales]) / SUM([Quantity])"}],
        "dim_calcs": [_DIM_SWAP,
                      {"name": "Seg Flag", "role": "dimension",
                       "formula": "[Parameters].[Sales Multiplier] > 1"}],
        "expect_consumed": {"Dim Swap", "Measure Swap"},
        "expect_measures_present": {"Boost", "Profit Ratio"},
    },
}


@pytest.mark.parametrize("case", list(_SCENARIOS), ids=list(_SCENARIOS))
def test_consumed_swap_has_exactly_one_representation(case):
    spec = _SCENARIOS[case]
    out = assemble_import_model(
        parse_tds(LIVE_SQLSERVER), model_name="Superstore",
        calcs=spec["calcs"], dim_calcs=spec["dim_calcs"], parameters=spec["params"])
    parts, report = out["parts"], out["report"]

    consumed = set(report["field_parameters"]["consumed"])
    # non-vacuous: the scenario really did consume the expected swaps
    assert consumed == spec["expect_consumed"], f"{case}: consumed={consumed}"

    own_param_tables = {f"definition/tables/{c}.tmdl" for c in consumed}
    for part_name, text in parts.items():
        if not part_name.startswith("definition/tables/"):
            continue
        # PROPERTY: a consumed swap is never emitted as a data-model measure (anywhere).
        also_measure = consumed & _declared(text, "measure")
        assert not also_measure, (
            f"{case}: {part_name} emits consumed swap(s) as a measure: {sorted(also_measure)}")
        # PROPERTY: a consumed swap is never a calculated column on any table other than its own
        # field-parameter table (whose display column legitimately shares the swap's name).
        if part_name in own_param_tables:
            continue
        also_column = consumed & _declared(text, "column")
        assert not also_column, (
            f"{case}: {part_name} emits consumed swap(s) as a column: {sorted(also_column)}")

    # positive guard: excluding the consumed swaps must not have dropped legitimate measures.
    for want in spec.get("expect_measures_present", set()):
        measures_tmdl = parts["definition/tables/_Measures.tmdl"]
        assert want in _declared(measures_tmdl, "measure"), (
            f"{case}: expected measure {want!r} to still be emitted")
