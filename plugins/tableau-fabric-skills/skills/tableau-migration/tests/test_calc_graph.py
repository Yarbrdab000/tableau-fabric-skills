"""Tests for calc_graph: the calc dependency DAG, roots-first topological
layering, and conservative row-level form inference.

This module is the FOUNDATION for the roots-first resolver -- it decides the
order calcs must be built in (walls before doors) and which calcs are faithfully
row-level (calculated columns) versus aggregate (measures). It never emits DAX;
it only analyzes structure, so these tests pin pure graph/form behavior.
"""
import pytest

from calc_graph import (
    build_calc_graph,
    topological_layers,
    infer_row_level,
    calc_key,
)


def _c(name, formula, internal_name=None, role="measure"):
    d = {"name": name, "formula": formula, "role": role}
    if internal_name:
        d["internal_name"] = internal_name
    return d


# --------------------------------------------------------------------------- #
# Reference classification
# --------------------------------------------------------------------------- #
def test_build_graph_classifies_calc_param_physical_refs():
    calcs = [
        _c("Base", "[Sales] - [Cost]"),                       # two physical refs
        _c("Marked Up", "[Base] * [Parameters].[Markup]"),    # 1 calc dep + 1 param
    ]
    g = build_calc_graph(calcs)
    base = g.nodes[calc_key(_c("Base", ""))]
    marked = g.nodes[calc_key(_c("Marked Up", ""))]
    assert base["calc_deps"] == set()
    assert base["phys_refs"] == {"sales", "cost"}
    # Marked Up depends on the Base calc, references a parameter, no physical
    assert marked["calc_deps"] == {"base"}
    assert marked["param_refs"] == {"markup"}
    assert marked["phys_refs"] == set()


def test_graph_resolves_dep_by_internal_name():
    # A calc references another by its Tableau INTERNAL name, not its caption.
    calcs = [
        _c("Current Year Quantity", "IF [flag] THEN [Quantity] END",
           internal_name="Current Year Quantity (copy)_123"),
        _c("Doubled", "[Current Year Quantity (copy)_123] * 2"),
    ]
    g = build_calc_graph(calcs)
    doubled = g.nodes[calc_key(_c("Doubled", ""))]
    # the internal-name reference canonicalizes to the target calc's key
    assert doubled["calc_deps"] == {calc_key(calcs[0])}


def test_field_named_like_aggregation_is_not_an_aggregation():
    # A field/calc literally named "Count of Contacts" must NOT be read as COUNT().
    calcs = [_c("Ratio", "[Count of Contacts] / [Total]")]
    g = build_calc_graph(calcs)
    node = g.nodes[calc_key(_c("Ratio", ""))]
    # both are unresolved-as-calc here => physical refs, and no aggregation detected
    assert node["phys_refs"] == {"count of contacts", "total"}
    assert node["has_aggregation"] is False


# --------------------------------------------------------------------------- #
# Topological layering (roots-first)
# --------------------------------------------------------------------------- #
def test_topological_layers_roots_first_chain():
    calcs = [
        _c("C", "[B] + 1"),
        _c("A", "[Sales]"),
        _c("B", "[A] + 1"),
    ]
    g = build_calc_graph(calcs)
    layers, unresolved = topological_layers(g)
    assert unresolved == []
    # A is the only root; then B; then C
    assert layers[0] == [calc_key(_c("A", ""))]
    assert layers[1] == [calc_key(_c("B", ""))]
    assert layers[2] == [calc_key(_c("C", ""))]


def test_topological_layers_tolerate_cycle():
    calcs = [
        _c("X", "[Y] + 1"),
        _c("Y", "[X] + 1"),
    ]
    g = build_calc_graph(calcs)
    layers, unresolved = topological_layers(g)
    # a cycle resolves to NOTHING placed and both flagged unresolved (fail-closed)
    assert set(unresolved) == {calc_key(_c("X", "")), calc_key(_c("Y", ""))}
    assert all(k not in sum(layers, []) for k in unresolved)


def test_roots_include_param_and_physical_only_calcs():
    calcs = [
        _c("P", "[Parameters].[Threshold]"),
        _c("Q", "[Sales] * 2"),
        _c("R", "[P] + [Q]"),
    ]
    g = build_calc_graph(calcs)
    layers, _ = topological_layers(g)
    assert set(layers[0]) == {calc_key(_c("P", "")), calc_key(_c("Q", ""))}
    assert layers[1] == [calc_key(_c("R", ""))]


# --------------------------------------------------------------------------- #
# Row-level form inference (conservative dichotomy)
# --------------------------------------------------------------------------- #
def test_infer_row_level_pure_rowlevel_true():
    calcs = [_c("Diff", "[Sales] - [Cost]")]
    g = build_calc_graph(calcs)
    forms = infer_row_level(g)
    assert forms[calc_key(_c("Diff", ""))] is True


def test_infer_row_level_aggregation_false():
    calcs = [_c("Total", "SUM([Sales])")]
    g = build_calc_graph(calcs)
    forms = infer_row_level(g)
    assert forms[calc_key(_c("Total", ""))] is False


def test_infer_row_level_lod_false_conservative():
    calcs = [_c("MaxDate", "{FIXED : MAX([Delivery Date])}")]
    g = build_calc_graph(calcs)
    forms = infer_row_level(g)
    assert forms[calc_key(_c("MaxDate", ""))] is False


def test_infer_row_level_tablecalc_false():
    calcs = [_c("Head", "WINDOW_MAX([x]) * 1.2")]
    g = build_calc_graph(calcs)
    forms = infer_row_level(g)
    assert forms[calc_key(_c("Head", ""))] is False


def test_infer_row_level_propagates_through_calc_deps():
    # Row-level conditional over a row-level sibling boolean => still row-level.
    calcs = [
        _c("IsCY", "DATEPART('year',[d]) = 2024", role="dimension"),
        _c("CY Qty", "IF [IsCY] THEN [Quantity] END"),
    ]
    g = build_calc_graph(calcs)
    forms = infer_row_level(g)
    assert forms[calc_key(_c("IsCY", ""))] is True
    assert forms[calc_key(_c("CY Qty", ""))] is True


def test_infer_row_level_false_when_dep_is_aggregate():
    # Referencing an aggregate calc makes the parent aggregate (Tableau: any part aggregate).
    calcs = [
        _c("Agg", "SUM([Sales])"),
        _c("Minus", "[Agg] - 1"),
    ]
    g = build_calc_graph(calcs)
    forms = infer_row_level(g)
    assert forms[calc_key(_c("Agg", ""))] is False
    assert forms[calc_key(_c("Minus", ""))] is False


def test_infer_row_level_false_for_cyclic_or_unresolved():
    calcs = [
        _c("X", "[Y] + 1"),
        _c("Y", "[X] + 1"),
    ]
    g = build_calc_graph(calcs)
    forms = infer_row_level(g)
    # cycle members can't be confirmed row-level -> conservative False (stay as-is / stub)
    assert forms[calc_key(_c("X", ""))] is False
    assert forms[calc_key(_c("Y", ""))] is False
