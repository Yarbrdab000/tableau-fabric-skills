"""Tests for the formula-authored table-calc -> nested Visual-Calculation compiler.

Proves the faithful happy paths (the real Acme composit + Rank chain, non-blend and blend
shapes) and the fail-closed boundary (anything outside the subset -> a review reason, never a
guessed translation).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from formula_table_calc_to_visual_calc import compile_chain, compile_expression  # noqa: E402


# -- resolver stubs -----------------------------------------------------------
def _agg_primary(agg, field, ds):
    """Resolve primary (unqualified) aggregates to a base-measure name; secondary -> unresolved."""
    if ds is not None:
        return None
    return {("SUM", "Sales"): "Sum of Sales",
            ("SUM", "Quantity"): "Sum of Quantity"}.get((agg, field))


def _agg_with_blend(agg, field, ds):
    """Also resolve the secondary blend source (simulates Feature B having landed a base measure)."""
    if ds and "copy" in ds.lower():
        return {("SUM", "Sales"): "Sum of Sales (2)"}.get((agg, field))
    return {("SUM", "Sales"): "Sum of Sales",
            ("SUM", "Quantity"): "Sum of Quantity"}.get((agg, field))


def _no_measure(name, ds):
    return None


# -- happy path: the real chain, non-blend ------------------------------------
def test_composite_and_rank_chain_nests_faithfully():
    calcs = {
        "composit Calc": "RUNNING_SUM(SUM([Sales])) * .15 + RUNNING_SUM(SUM([Quantity])) * 15",
        "Rank": "RANK([composit Calc])",
    }
    defs, reason = compile_chain(
        "Rank", calcs, axis="ROWS",
        resolve_aggregate=_agg_primary,
        summaries={"composit Calc": calcs["composit Calc"], "Rank": calcs["Rank"]})
    assert reason is None, reason
    assert [d.name for d in defs] == ["composit Calc", "Rank"]      # inner-before-outer

    inner, outer = defs
    assert inner.hidden is True and inner.is_inner is True
    assert inner.expression == (
        "RUNNINGSUM([Sum of Sales], ROWS) * 0.15 + RUNNINGSUM([Sum of Quantity], ROWS) * 15")
    assert inner.tableau_summary == calcs["composit Calc"]

    assert outer.hidden is False and outer.is_inner is False
    assert outer.expression == "RANK(SKIP, ORDERBY([composit Calc], DESC))"
    assert outer.family == "FORMULA_TABLE_CALC"


# -- happy path: the REAL workbook formula (blend qfield) parses + drives the resolver ----------
def test_real_blend_formula_parses_and_requests_secondary_measure():
    calcs = {
        "composit Calc":
            "RUNNING_SUM(SUM([Sample - Superstore (copy)].[Sales])) * .15 "
            "+ RUNNING_SUM(SUM([Quantity])) *15",
        "Rank": "RANK([composit Calc])",
    }
    seen = []

    def _agg(agg, field, ds):
        seen.append((agg, field, ds))
        return _agg_with_blend(agg, field, ds)

    defs, reason = compile_chain("Rank", calcs, resolve_aggregate=_agg)
    assert reason is None, reason
    # the compiler correctly parsed the dotted blend reference and asked the resolver for it
    assert ("SUM", "Sales", "Sample - Superstore (copy)") in seen
    inner = defs[0]
    assert inner.expression == (
        "RUNNINGSUM([Sum of Sales (2)], ROWS) * 0.15 + RUNNINGSUM([Sum of Quantity], ROWS) * 15")


def test_blend_source_unresolved_fails_closed():
    """Without Feature B the secondary base measure is unknown -> review, not a guess."""
    calcs = {
        "composit Calc":
            "RUNNING_SUM(SUM([Sample - Superstore (copy)].[Sales])) * .15 "
            "+ RUNNING_SUM(SUM([Quantity])) *15",
        "Rank": "RANK([composit Calc])",
    }
    defs, reason = compile_chain("Rank", calcs, resolve_aggregate=_agg_primary)
    assert defs is None
    assert "Sales" in reason


# -- RANK variants ------------------------------------------------------------
def test_rank_dense_and_ascending_direction():
    expr, deps, reason = compile_expression(
        "RANK_DENSE([m], 'asc')", axis="ROWS",
        resolve_aggregate=_agg_primary,
        resolve_reference=lambda n, ds: ("measure", "My Measure"))
    assert reason is None
    assert expr == "RANK(DENSE, ORDERBY([My Measure], ASC))"


def test_number_leading_dot_normalized():
    expr, _, reason = compile_expression(
        "RUNNING_SUM(SUM([Sales])) * .15", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert reason is None
    assert expr == "RUNNINGSUM([Sum of Sales], ROWS) * 0.15"


def test_axis_columns_threads_through():
    expr, _, reason = compile_expression(
        "RUNNING_SUM(SUM([Sales]))", axis="COLUMNS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert reason is None
    assert expr == "RUNNINGSUM([Sum of Sales], COLUMNS)"


# -- fail-closed boundary -----------------------------------------------------
def test_running_avg_not_supported():
    expr, _, reason = compile_expression(
        "RUNNING_AVG(SUM([Sales]))", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert expr is None and "RUNNING_AVG" in reason


def test_running_sum_of_inline_expression_fails_closed():
    """PBI RUNNINGSUM takes a column, not an inline expression -> review."""
    expr, _, reason = compile_expression(
        "RUNNING_SUM(SUM([Sales]) + SUM([Quantity]))", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert expr is None and "single aggregate or field" in reason


def test_unknown_function_fails_closed():
    expr, _, reason = compile_expression(
        "WINDOW_CORR(SUM([Sales]), SUM([Quantity]))", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert expr is None and "WINDOW_CORR" in reason


def test_lod_brace_fails_closed():
    expr, _, reason = compile_expression(
        "RUNNING_SUM(SUM({FIXED [Region] : SUM([Sales])}))", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert expr is None and reason


def test_reference_cycle_fails_closed():
    calcs = {"A": "RANK([B])", "B": "RANK([A])"}
    defs, reason = compile_chain("A", calcs, resolve_aggregate=_agg_primary)
    assert defs is None and "cycle" in reason


def test_unresolved_bare_reference_fails_closed():
    expr, _, reason = compile_expression(
        "RANK([Mystery])", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert expr is None and "Mystery" in reason
