"""Tests for the formula-authored table-calc -> nested Visual-Calculation compiler.

Proves the faithful happy paths (the real Acme composit + Rank chain, non-blend and blend
shapes) and the fail-closed boundary (anything outside the subset -> a review reason, never a
guessed translation).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from formula_table_calc_to_visual_calc import (  # noqa: E402
    compile_chain, compile_expression, formula_is_table_calc)


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


# -- TOTAL -> COLLAPSEALL ------------------------------------------------------
# Tableau's TOTAL returns the partition total, RE-EVALUATING the aggregate over the partition's
# underlying rows. COLLAPSEALL is the view-side counterpart: "retrieves a context at the highest
# level ... returns its value in the new context". Microsoft's own doc example,
# `TotalValue = COLLAPSEALL([SalesAmount], ROWS)`, is exactly Tableau's percent-of-total idiom.
def test_total_of_an_aggregate_compiles_to_collapseall():
    expr, deps, reason = compile_expression(
        "TOTAL(SUM([Sales]))", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert reason is None
    assert expr == "COLLAPSEALL([Sum of Sales], ROWS)"
    assert deps == []


def test_total_of_a_calc_reference_nests_and_records_the_dependency():
    """The real corpus shape: Total([Some Calc]) -- the argument is another calc, not an aggregate."""
    expr, deps, reason = compile_expression(
        "TOTAL([Open Intakes])", axis="ROWS",
        resolve_aggregate=_agg_primary,
        resolve_reference=lambda n, ds: ("calc", "Open Intakes"))
    assert reason is None
    assert expr == "COLLAPSEALL([Open Intakes], ROWS)"
    assert deps == ["Open Intakes"]            # nested VC dependency recorded


def test_total_of_a_base_measure_reference():
    expr, deps, reason = compile_expression(
        "TOTAL([Referrals])", axis="ROWS",
        resolve_aggregate=_agg_primary,
        resolve_reference=lambda n, ds: ("measure", "Referrals"))
    assert reason is None
    assert expr == "COLLAPSEALL([Referrals], ROWS)"
    assert deps == []                          # a base measure is not a nested-VC dependency


def test_total_threads_the_axis():
    expr, _, reason = compile_expression(
        "TOTAL(SUM([Sales]))", axis="COLUMNS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert reason is None
    assert expr == "COLLAPSEALL([Sum of Sales], COLUMNS)"


def test_total_composes_the_percent_of_total_idiom():
    """The canonical use, and the exact shape of Microsoft's COLLAPSEALL doc example."""
    expr, _, reason = compile_expression(
        "SUM([Sales]) / TOTAL(SUM([Sales]))", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert reason is None
    assert expr == "[Sum of Sales] / COLLAPSEALL([Sum of Sales], ROWS)"


def test_total_is_case_insensitive():
    """The corpus writes it as `Total([x])`, not `TOTAL([x])`."""
    expr, _, reason = compile_expression(
        "Total(SUM([Sales]))", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert reason is None
    assert expr == "COLLAPSEALL([Sum of Sales], ROWS)"


def test_total_rejects_a_direction_argument():
    """Tableau's TOTAL takes exactly one argument -- same guard the model-layer seam draws."""
    expr, _, reason = compile_expression(
        "TOTAL(SUM([Sales]), 'asc')", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert expr is None
    assert reason


def test_total_rejects_an_inline_expression_argument():
    expr, _, reason = compile_expression(
        "TOTAL(SUM([Sales]) + 1)", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert expr is None
    assert reason


def test_total_of_an_unresolvable_reference_falls_back():
    expr, _, reason = compile_expression(
        "TOTAL([Nope])", axis="ROWS",
        resolve_aggregate=_agg_primary, resolve_reference=lambda n, ds: None)
    assert expr is None
    assert reason


# -- the semantic line COLLAPSEALL must NOT be pushed across ---------------------------------------
def test_window_family_has_its_own_form_and_never_becomes_collapseall():
    """COLLAPSEALL is total-level RE-EVALUATION; the WINDOW_* family is per-mark aggregation.

    They coincide for SUM but diverge for a non-additive inner (Tableau's TOTAL(AVG([x])) averages
    all underlying rows; WINDOW_AVG(AVG([x])) averages the per-mark averages). Mapping a WINDOW_*
    head to COLLAPSEALL would therefore ship silently wrong numbers.

    The window family now HAS its own verified form -- ``<X>(WINDOW(1, ABS, -1, ABS, axis), base)``,
    the model seam's certified whole-partition frame transposed to the visual-calc dialect -- so this
    asserts that form positively while keeping the COLLAPSEALL line exactly where it was.
    """
    cases = {
        "WINDOW_MAX(SUM([Sales]))": "MAXX(WINDOW(1, ABS, -1, ABS, ROWS), [Sum of Sales])",
        "WINDOW_SUM(SUM([Sales]))": "SUMX(WINDOW(1, ABS, -1, ABS, ROWS), [Sum of Sales])",
        "WINDOW_AVG(SUM([Sales]))": "AVERAGEX(WINDOW(1, ABS, -1, ABS, ROWS), [Sum of Sales])",
        "WINDOW_STDEV(SUM([Sales]))": "STDEVX.S(WINDOW(1, ABS, -1, ABS, ROWS), [Sum of Sales])",
        "WINDOW_MAX([Some Calc]) * 1.2":
            "MAXX(WINDOW(1, ABS, -1, ABS, ROWS), [Some Calc]) * 1.2",
    }
    for formula, expected in cases.items():
        expr, _, reason = compile_expression(
            formula, axis="ROWS", resolve_aggregate=_agg_primary,
            resolve_reference=lambda n, ds: ("calc", "Some Calc"))
        assert reason is None, formula
        assert expr == expected, formula
        assert "COLLAPSEALL" not in (expr or ""), formula


def test_window_moving_and_multi_argument_forms_still_fail_closed():
    """Only the WHOLE-PARTITION window form is certified; bounds / extra args keep failing closed.

    A moving frame (``WINDOW_AVG(x, -2, 0)``) and the two-argument heads (``WINDOW_PERCENTILE``,
    ``WINDOW_CORR``) are outside the transposed model mapping, so they must still route to review
    rather than silently collapse to the whole-partition frame.
    """
    for formula in ("WINDOW_AVG(SUM([Sales]), -2, 0)",
                    "WINDOW_PERCENTILE(SUM([Sales]), 0.9)",
                    "WINDOW_CORR(SUM([Sales]), SUM([Profit]))"):
        expr, _, reason = compile_expression(
            formula, axis="ROWS", resolve_aggregate=_agg_primary,
            resolve_reference=lambda n, ds: None)
        assert expr is None, formula
        assert reason, formula


def test_table_calc_detector_covers_the_canonical_formula_head_catalog():
    """``formula_is_table_calc`` must recognise every head ``table_calc_to_dax`` catalogs.

    The two vocabularies are maintained apart, and a head missing HERE is the dangerous direction:
    a real table calc would be mistaken for an ordinary calc and bound to a model measure instead of
    rebuilding as a nested Visual Calculation.
    """
    from table_calc_to_dax import _FORMULA_INTENT

    for head, _intent in _FORMULA_INTENT:
        probe = head + ("SUM" if head.endswith("_") else "") + "(SUM([Sales]))"
        assert formula_is_table_calc(probe), probe
    assert not formula_is_table_calc("COUNTD(IF [Status] = \"Closed\" THEN [Case ID] END)")
    assert not formula_is_table_calc("SUM([Profit]) / SUM([Sales])")
    assert not formula_is_table_calc("")
    assert not formula_is_table_calc(None)