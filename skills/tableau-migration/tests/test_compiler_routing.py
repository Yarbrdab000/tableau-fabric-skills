"""Compiler-tier ROUTING tests: does each calc / datasource / worksheet take the *right* path?

This suite deliberately spans the whole compiler tier and asserts on *routing decisions* rather
than on the exact DAX/TMDL bytes (those are pinned elsewhere). The routing surfaces exercised:

  * ``calc_to_dax`` -- the Tier-0 translate-or-fall-back decision (measure mode + column mode), and
    that a fallback's honest ``reason`` then routes through ``translation_router.classify_fallback``
    to the expected Tier-1 charter category. This locks the end-to-end Tier-0 -> Tier-1 pipe.
  * ``translation_router`` -- ``classify_fallback`` reason -> category (every branch + precedence +
    case/bracket/prefix normalisation) and the ``check_candidate_dax`` syntactic gate.
  * ``twb_to_pbir`` -- the additive nested-Visual-Calculation emit-site router
    (``_view_only_field_chain_index`` selecting ONLY nested calc-references-calc chains,
    ``_resolved_value_fields``, and ``_apply_formula_table_calc_chain``'s fail-closed guards).
  * ``storage_mode`` -- the per-datasource Import / DirectQuery / needs-decision selector.
  * ``table_calc_to_dax`` -- the table-calc translate-vs-handoff decision.

Every expected value here was harvested from the real engines (not guessed): the fallback reasons
come from ``translate_tableau_calc_to_dax`` / ``..._column_dax`` themselves, so the tests move in
lock-step with the deterministic tier and fail loudly if a routing edge is silently re-cut.
"""
import pytest

from calc_to_dax import (
    translate_tableau_calc_to_dax,
    translate_tableau_calc_to_column_dax,
)
import translation_router as R
from twb_to_pbir import (
    _apply_formula_table_calc_chain,
    _resolved_value_fields,
    _view_only_field_chain_index,
)
from workbook_table_calcs import Pill, TableCalcUsage
from storage_mode import select_storage_mode
from table_calc_to_dax import translate_table_calc_usage


# --------------------------------------------------------------------------- shared calc resolver
# caption -> (table_display_name, clean_column, tmdl_type). Two tables so a cross-table term is a
# genuine reference failure; a boolean + string column so type/shape gaps are reachable.
_FIELDS = {
    "Profit": ("Orders", "Profit", "decimal"),
    "Sales": ("Orders", "Sales", "decimal"),
    "Quantity": ("Orders", "Quantity", "int64"),
    "Order Date": ("Orders", "Order_Date", "dateTime"),
    "Region": ("Orders", "Region", "string"),
    "Returned": ("Orders", "Returned", "boolean"),
    "City": ("Orders", "City", "string"),
    "People Count": ("People", "People_Count", "int64"),
}


def _resolver(caption):
    return _FIELDS.get(caption)


# ===========================================================================================
# Section A -- Tier-0 (MEASURE mode) fallback reason -> Tier-1 router category (end to end)
# ===========================================================================================
# (formula, expected_category). The formula MUST fall back in measure mode and its honest reason
# MUST route to the given category. Harvested from the live engine + router.
MEASURE_FALLBACK_ROUTES = [
    ("WINDOW_SUM(SUM([Sales]))", R.MISSING_ADDRESSING_INTENT),
    ("RUNNING_SUM(SUM([Sales]))", R.MISSING_ADDRESSING_INTENT),
    ("RANK(SUM([Sales]))", R.MISSING_ADDRESSING_INTENT),
    ("INDEX()", R.MISSING_ADDRESSING_INTENT),
    ("SIZE()", R.MISSING_ADDRESSING_INTENT),
    ("FIRST()", R.MISSING_ADDRESSING_INTENT),
    ("LAST()", R.MISSING_ADDRESSING_INTENT),
    ("LOOKUP(SUM([Sales]),-1)", R.MISSING_ADDRESSING_INTENT),
    ("PREVIOUS_VALUE(0)", R.MISSING_ADDRESSING_INTENT),
    ("TOTAL(SUM([Sales]))", R.MISSING_ADDRESSING_INTENT),
    ("RANK_PERCENTILE(SUM([Sales]))", R.MISSING_ADDRESSING_INTENT),
    ("RANK_UNIQUE(SUM([Sales]))", R.DAX_LANGUAGE_GAP),
    ("REGEXP_MATCH([City],'^San')", R.DAX_LANGUAGE_GAP),
    ("DATEPARSE('yyyy',[City])", R.DAX_LANGUAGE_GAP),
    ("SPLIT([City],'-',1)", R.DAX_LANGUAGE_GAP),
    ("TRIM([City])", R.DAX_LANGUAGE_GAP),
    ("STR([Sales])", R.DAX_LANGUAGE_GAP),
    ("SUM([Sales])/SUM([People Count])", R.UNRESOLVED_REFERENCE),
    ("IF SUM([Sales])>0 THEN 'a' ELSE 1 END", R.TYPE_OR_SHAPE_MISMATCH),
    ("{ INCLUDE [Region] : SUM([Sales]) }", R.MISSING_OUTER_AGGREGATION),
    ("CORR(SUM([Sales]),SUM([Profit]))", R.UNSUPPORTED_OTHER),
    ("[Parameters].[Growth Rate] * SUM([Sales])", R.MODEL_OBJECT_PARAMETER),
]


@pytest.mark.parametrize("formula,category", MEASURE_FALLBACK_ROUTES)
def test_measure_mode_fallback_routes_to_category(formula, category):
    dax, reason, _tables = translate_tableau_calc_to_dax(formula, _resolver)
    assert dax is None, "expected %r to fall back in measure mode" % formula
    assert reason and reason != "ok"
    assert R.classify_fallback(reason)["category"] == category


# Formulas that MUST translate in measure mode -> they never reach the fallback router at all.
MEASURE_TRANSLATED = [
    "SUM([Profit])/SUM([Sales])",
    "{ FIXED [Region] : SUM([Sales]) }",
    "{ EXCLUDE [Region] : SUM([Sales]) }",
    "SUM({FIXED [Region]:SUM([Sales])})",
]


@pytest.mark.parametrize("formula", MEASURE_TRANSLATED)
def test_measure_mode_translates_and_does_not_route(formula):
    dax, reason, _tables = translate_tableau_calc_to_dax(formula, _resolver)
    assert dax is not None, "expected %r to translate in measure mode" % formula
    assert reason == "ok"


# ===========================================================================================
# Section B -- Tier-0 (COLUMN mode) fallback reason -> Tier-1 router category (end to end)
# ===========================================================================================
COLUMN_FALLBACK_ROUTES = [
    ("REGEXP_MATCH([City],'^San')", R.DAX_LANGUAGE_GAP),
    ("REGEXP_REPLACE([City],'a','b')", R.DAX_LANGUAGE_GAP),
    ("DATEPARSE('yyyy-MM',[City])", R.DAX_LANGUAGE_GAP),
    ("SPLIT([City],'-',1)", R.DAX_LANGUAGE_GAP),
    ("FINDNTH([City],'a',2)", R.DAX_LANGUAGE_GAP),
    ("TRIM([City])", R.DAX_LANGUAGE_GAP),
    ("LTRIM([City])", R.DAX_LANGUAGE_GAP),
    ("RTRIM([City])", R.DAX_LANGUAGE_GAP),
    ("ISOQUARTER([Order Date])", R.DAX_LANGUAGE_GAP),
    ("MAKETIME(1,2,3)", R.DAX_LANGUAGE_GAP),
    ("MAKEDATETIME([Order Date],[Order Date])", R.DAX_LANGUAGE_GAP),
    ("STR([Sales])", R.DAX_LANGUAGE_GAP),
    ("ISDATE([City])", R.DAX_LANGUAGE_GAP),
    ("HEXBINX([Sales],[Profit])", R.DAX_LANGUAGE_GAP),
    ("HEXBINY([Sales],[Profit])", R.DAX_LANGUAGE_GAP),
    ("DATEPART('iso-week',[Order Date])", R.DAX_LANGUAGE_GAP),
    ("DATEADD('fortnight',1,[Order Date])", R.DAX_LANGUAGE_GAP),
    ("DATETRUNC('fortnight',[Order Date])", R.DAX_LANGUAGE_GAP),
    ("DATEDIFF('fortnight',[Order Date],[Order Date])", R.DAX_LANGUAGE_GAP),
    ("[Parameters].[Region]", R.MODEL_OBJECT_PARAMETER),
    ("[Datasource].[Sales]", R.UNSUPPORTED_OTHER),
    ("SUM([Sales])", R.TYPE_OR_SHAPE_MISMATCH),
    ("[Nonexistent Field] + 1", R.UNRESOLVED_REFERENCE),
]


@pytest.mark.parametrize("formula,category", COLUMN_FALLBACK_ROUTES)
def test_column_mode_fallback_routes_to_category(formula, category):
    dax, reason, _tables = translate_tableau_calc_to_column_dax(formula, _resolver)
    assert dax is None, "expected %r to fall back in column mode" % formula
    assert reason and reason != "ok"
    assert R.classify_fallback(reason)["category"] == category


COLUMN_TRANSLATED = [
    "WEEK([Order Date])",
    "IF [Region]='east' THEN [Sales] END",
]


@pytest.mark.parametrize("formula", COLUMN_TRANSLATED)
def test_column_mode_translates_and_does_not_route(formula):
    dax, reason, _tables = translate_tableau_calc_to_column_dax(formula, _resolver)
    assert dax is not None, "expected %r to translate in column mode" % formula
    assert reason == "ok"


# A cross-cutting invariant: EVERY reason the engine can emit routes to a REAL charter category.
def test_every_engine_fallback_reason_maps_into_the_taxonomy():
    seen = set()
    for formula, _cat in MEASURE_FALLBACK_ROUTES:
        _dax, reason, _ = translate_tableau_calc_to_dax(formula, _resolver)
        seen.add(R.classify_fallback(reason)["category"])
    for formula, _cat in COLUMN_FALLBACK_ROUTES:
        _dax, reason, _ = translate_tableau_calc_to_column_dax(formula, _resolver)
        seen.add(R.classify_fallback(reason)["category"])
    assert seen <= set(R.CATEGORIES)
    # the harvested corpus alone exercises every category except the pure catch-all edge cases
    assert R.MISSING_ADDRESSING_INTENT in seen
    assert R.DAX_LANGUAGE_GAP in seen
    assert R.UNRESOLVED_REFERENCE in seen
    assert R.TYPE_OR_SHAPE_MISMATCH in seen
    assert R.MODEL_OBJECT_PARAMETER in seen


# ===========================================================================================
# Section C -- classify_fallback: direct reason -> category over every branch
# ===========================================================================================
REASON_ROUTES = [
    # model-object parameter
    ("parameter reference [Parameters].[Growth Rate] (unmodeled)", R.MODEL_OBJECT_PARAMETER),
    # missing outer aggregation (LOD grain)
    ("only FIXED LOD is translated (INCLUDE/EXCLUDE fall back)", R.MISSING_OUTER_AGGREGATION),
    ("SUM cannot re-aggregate a FIXED LOD", R.MISSING_OUTER_AGGREGATION),
    ("re-aggregating a table-scoped LOD is not supported", R.MISSING_OUTER_AGGREGATION),
    ("nested FIXED LOD does not fix a superset of the enclosing LOD", R.MISSING_OUTER_AGGREGATION),
    ("bare INCLUDE LOD requires an enclosing aggregation", R.MISSING_OUTER_AGGREGATION),
    ("AVG over an LOD requires a numeric inner expression", R.MISSING_OUTER_AGGREGATION),
    ("LOD expression not valid in a row-level column calc", R.MISSING_OUTER_AGGREGATION),
    # addressing intent (table-calc seam)
    ("unsupported function WINDOW_SUM", R.MISSING_ADDRESSING_INTENT),
    ("unsupported table calculation TOTAL", R.MISSING_ADDRESSING_INTENT),
    ("table calc requires an explicit order-by spec", R.MISSING_ADDRESSING_INTENT),
    ("unresolved/ambiguous partition field [Foo]", R.MISSING_ADDRESSING_INTENT),
    ("not a table calculation", R.MISSING_ADDRESSING_INTENT),
    # dax language gap
    ("unsupported function REGEXP_MATCH", R.DAX_LANGUAGE_GAP),
    ("unsupported table calculation RANK_UNIQUE", R.DAX_LANGUAGE_GAP),
    ("unsupported DATEPART part 'iso-week'", R.DAX_LANGUAGE_GAP),
    ("unsupported DATEADD part 'fortnight'", R.DAX_LANGUAGE_GAP),
    ("unsupported DATETRUNC part 'fortnight'", R.DAX_LANGUAGE_GAP),
    ("no faithful DAX form for this construct", R.DAX_LANGUAGE_GAP),
    # unresolved / cross-table reference
    ("unresolved/ambiguous field [Bar]", R.UNRESOLVED_REFERENCE),
    ("cross-table terms (fields span multiple tables)", R.UNRESOLVED_REFERENCE),
    ("unsupported field type geography for [Region]", R.UNRESOLVED_REFERENCE),
    ("ROUND requires a numeric field, got string for [City]", R.UNRESOLVED_REFERENCE),
    # type / shape mismatch
    ("IF/ELSE branches return inconsistent types", R.TYPE_OR_SHAPE_MISMATCH),
    ("incomparable types in comparison", R.TYPE_OR_SHAPE_MISMATCH),
    ("aggregation SUM not valid in a row-level column calc", R.TYPE_OR_SHAPE_MISMATCH),
    ("bare row-level field [..] not valid in a measure", R.TYPE_OR_SHAPE_MISMATCH),
    ("4-arg IIF (unknown branch) not supported", R.TYPE_OR_SHAPE_MISMATCH),
    ("booleans support only = and <> comparison", R.TYPE_OR_SHAPE_MISMATCH),
    ("expected a numeric expression", R.TYPE_OR_SHAPE_MISMATCH),
    ("unterminated field reference", R.TYPE_OR_SHAPE_MISMATCH),
    ("unsupported character ']'", R.TYPE_OR_SHAPE_MISMATCH),
    ("ordered text comparison is case-sensitive", R.TYPE_OR_SHAPE_MISMATCH),
    # catch-all
    ("CORR supports only two bare [field] arguments", R.UNSUPPORTED_OTHER),
    ("some brand new reason text nobody has seen", R.UNSUPPORTED_OTHER),
    ("", R.UNSUPPORTED_OTHER),
    (None, R.UNSUPPORTED_OTHER),
]


@pytest.mark.parametrize("reason,category", REASON_ROUTES)
def test_classify_fallback_reason_routes(reason, category):
    assert R.classify_fallback(reason)["category"] == category


def test_parameter_field_signal_beats_addressing_reason():
    # a structural [Parameters] field routes to the model-object playbook even when the free-text
    # reason is a table-calc addressing message.
    fields = [{"caption": "[Parameters].[Window]", "kind": "parameter"}]
    assert R.classify_fallback("unsupported function WINDOW_SUM",
                               fields=fields)["category"] == R.MODEL_OBJECT_PARAMETER


def test_parameter_reason_beats_lod_reason():
    # the parameter check is step 1, ahead of the LOD grain check.
    reason = "parameter reference [Parameters].[x]; also INCLUDE/EXCLUDE grain issue"
    assert R.classify_fallback(reason)["category"] == R.MODEL_OBJECT_PARAMETER


def test_classify_is_case_insensitive():
    assert R.classify_fallback("UNSUPPORTED FUNCTION WINDOW_SUM")["category"] \
        == R.MISSING_ADDRESSING_INTENT


def test_classify_strips_brackets_around_function_name():
    assert R.classify_fallback("unsupported function [RANK_UNIQUE]")["category"] \
        == R.DAX_LANGUAGE_GAP


def test_both_unsupported_prefixes_route_identically():
    a = R.classify_fallback("unsupported function TOTAL")["category"]
    b = R.classify_fallback("unsupported table calculation TOTAL")["category"]
    assert a == b == R.MISSING_ADDRESSING_INTENT


def test_trailing_text_after_function_name_is_ignored():
    assert R.classify_fallback("unsupported function WINDOW_SUM used in a measure")["category"] \
        == R.MISSING_ADDRESSING_INTENT


def test_every_category_has_nonempty_guidance():
    for cat in R.CATEGORIES:
        assert cat in R._GUIDANCE and R._GUIDANCE[cat].strip()


@pytest.mark.parametrize("reason", [
    "unsupported function WINDOW_SUM", "parameter reference [Parameters].[x]",
    "", None, "totally novel reason", 12345,
])
def test_classify_always_returns_valid_shape(reason):
    out = R.classify_fallback(reason)
    assert set(out) >= {"category", "guidance"}
    assert out["category"] in R.CATEGORIES
    assert out["guidance"].strip()


# ===========================================================================================
# Section D -- check_candidate_dax: the syntactic gate that routes a candidate ok / not-ok
# ===========================================================================================
@pytest.mark.parametrize("dax", [
    "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
    "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Region] = \"East\")",
    "\"a) [b] (c\"",                       # delimiters live inside a string literal -> balanced
    "VAR x = SUM('Orders'[Sales]) RETURN x + 1",
])
def test_gate_accepts_well_formed(dax):
    out = R.check_candidate_dax(dax)
    assert out["ok"] is True and out["issues"] == []


@pytest.mark.parametrize("dax", ["", "   ", None])
def test_gate_rejects_empty(dax):
    out = R.check_candidate_dax(dax)
    assert out["ok"] is False and any("empty" in i for i in out["issues"])


@pytest.mark.parametrize("dax", ["0", "blank()", " BLANK() "])
def test_gate_rejects_inert_stub(dax):
    out = R.check_candidate_dax(dax)
    assert out["ok"] is False and any("inert stub" in i for i in out["issues"])


@pytest.mark.parametrize("dax,needle", [
    ("DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales])", "unclosed"),
    ("SUM('Orders'[Sales]))", "unbalanced"),
    ("SUM('Orders'[Sales)]", "mismatched"),
    ('CONCATENATE("a, "b")', "unterminated string"),
])
def test_gate_rejects_malformed_delimiters(dax, needle):
    out = R.check_candidate_dax(dax)
    assert out["ok"] is False and any(needle in i for i in out["issues"])


@pytest.mark.parametrize("dax", [
    "CALCULATE(SUM('Orders'[Sales]), {FIXED [Region]})",
    "IF([Parameters].[Region] = 1, 1, 0)",
    "SUMX({INCLUDE [Region]}, 1)",
])
def test_gate_rejects_leftover_tableau_idiom(dax):
    out = R.check_candidate_dax(dax)
    assert out["ok"] is False and any("Tableau idiom" in i for i in out["issues"])


def test_gate_warns_on_language_gap_but_stays_ok():
    out = R.check_candidate_dax("LEFT('Orders'[City], 3)",
                                request={"category": R.DAX_LANGUAGE_GAP})
    assert out["ok"] is True and any("oracle-verified" in w for w in out["warnings"])


def test_gate_no_warning_without_request():
    out = R.check_candidate_dax("LEFT('Orders'[City], 3)")
    assert out["ok"] is True and out["warnings"] == []


@pytest.mark.parametrize("junk", [123, [], {}, object()])
def test_gate_never_raises_on_garbage(junk):
    out = R.check_candidate_dax(junk)
    assert "ok" in out and "issues" in out


# ===========================================================================================
# Section E -- twb_to_pbir emit-site: _view_only_field_chain_index selects ONLY nested chains
# ===========================================================================================
_CID_A = "Calculation_a"
_CID_B = "Calculation_b"


def _field_usage(ws, col, caption, formula, scope_formulas, scope_captions):
    return TableCalcUsage(
        worksheet=ws, instance="[i_%s]" % col, column=col, caption=caption,
        kind="field", formula=formula,
        scope_formulas=scope_formulas, scope_captions=scope_captions)


def _nested(ws="Sheet3", outer_col=_CID_B, outer_cap="Rank"):
    """A nested chain: outer calc references the inner calc field."""
    return _field_usage(
        ws, outer_col, outer_cap,
        formula="RANK([%s])" % _CID_A,
        scope_formulas={_CID_A: "RUNNING_SUM(SUM([Sales]))", outer_col: "RANK([%s])" % _CID_A},
        scope_captions={_CID_A: "composit", outer_col: outer_cap})


def test_chain_index_includes_a_nested_calc_reference_chain():
    idx = _view_only_field_chain_index([_nested()])
    assert "Sheet3" in idx and len(idx["Sheet3"]) == 1


def test_chain_index_excludes_single_level_formula_calc():
    u = _field_usage("S", _CID_A, "Run Sales", "RUNNING_SUM(SUM([Sales]))",
                     {_CID_A: "RUNNING_SUM(SUM([Sales]))"}, {_CID_A: "Run Sales"})
    assert _view_only_field_chain_index([u]) == {}


def test_chain_index_excludes_quick_table_calc():
    u = TableCalcUsage(worksheet="S", instance="[i]", column="[Profit]", caption="Profit",
                       kind="quick", calc_type="RunningSum")
    assert _view_only_field_chain_index([u]) == {}


def test_chain_index_excludes_value_kind_usage():
    u = TableCalcUsage(worksheet="S", instance="[i]", column="[Profit]", caption="Profit",
                       kind="value")
    assert _view_only_field_chain_index([u]) == {}


def test_chain_index_excludes_when_scope_missing():
    u = _field_usage("S", _CID_B, "Rank", "RANK([%s])" % _CID_A, None, None)
    assert _view_only_field_chain_index([u]) == {}


def test_chain_index_excludes_when_formula_missing():
    u = TableCalcUsage(worksheet="S", instance="[i]", column=_CID_B, caption="Rank",
                       kind="field", formula=None,
                       scope_formulas={_CID_A: "x", _CID_B: "y"},
                       scope_captions={_CID_A: "a", _CID_B: "b"})
    assert _view_only_field_chain_index([u]) == {}


def test_chain_index_excludes_self_reference_only():
    # a formula that references ONLY its own id is not a calc-references-calc chain.
    u = _field_usage("S", _CID_A, "Self", "IF [%s] > 0 THEN 1 END" % _CID_A,
                     {_CID_A: "IF [%s] > 0 THEN 1 END" % _CID_A}, {_CID_A: "Self"})
    assert _view_only_field_chain_index([u]) == {}


def test_chain_index_none_input_is_empty():
    assert _view_only_field_chain_index(None) == {}


def test_chain_index_empty_list_is_empty():
    assert _view_only_field_chain_index([]) == {}


def test_chain_index_groups_by_worksheet():
    idx = _view_only_field_chain_index([_nested(ws="A"), _nested(ws="B")])
    assert set(idx) == {"A", "B"}


def test_chain_index_collects_multiple_chains_on_one_worksheet():
    idx = _view_only_field_chain_index([
        _nested(ws="A", outer_col=_CID_B, outer_cap="Rank1"),
        _nested(ws="A", outer_col="Calculation_c", outer_cap="Rank2"),
    ])
    assert set(idx) == {"A"} and len(idx["A"]) == 2


# ===========================================================================================
# Section F -- _resolved_value_fields + _apply_formula_table_calc_chain fail-closed routing
# ===========================================================================================
def _agg_value(caption, prop, agg="Sum", entity="Orders"):
    return {"kind": "value", "binding": "aggregation", "aggregation": agg,
            "entity": entity, "property": prop, "caption": caption}


def test_resolved_value_fields_indexes_aggregation_by_caption_and_property():
    ws = {"name": "S", "rows": [_agg_value("Total Sales", "Sales")], "cols": [], "encodings": {}}
    out = _resolved_value_fields(ws)
    assert "total sales" in out and "sales" in out
    assert out["sales"]["property"] == "Sales"


def test_resolved_value_fields_excludes_dimension_columns():
    ws = {"name": "S",
          "rows": [{"kind": "category", "binding": "column", "property": "Region",
                    "caption": "Region"}],
          "cols": [], "encodings": {}}
    assert _resolved_value_fields(ws) == {}


def test_resolved_value_fields_excludes_non_aggregation_values():
    ws = {"name": "S",
          "rows": [{"kind": "value", "binding": "measure", "property": "Rank", "caption": "Rank"}],
          "cols": [], "encodings": {}}
    assert _resolved_value_fields(ws) == {}


def test_resolved_value_fields_reads_encoding_lists_and_scalars():
    ws = {"name": "S", "rows": [], "cols": [],
          "encodings": {"detail": [_agg_value("Sales", "Sales")],
                        "color": _agg_value("Profit", "Profit")}}
    out = _resolved_value_fields(ws)
    assert "sales" in out and "profit" in out


def test_resolved_value_fields_empty_worksheet_is_empty():
    assert _resolved_value_fields({"name": "S", "rows": [], "cols": [], "encodings": {}}) == {}


# --- _apply_formula_table_calc_chain routing guards (the success + review paths are pinned in
#     test_twb_to_pbir.py; here we lock the cheap fail-closed guards + one review branch). ---
def _chain_ws_state(composit_formula):
    ws = {"name": "Sheet3", "visual_type": "table",
          "rows": [{"kind": "category", "binding": "column", "aggregation": None,
                    "entity": "Orders", "property": "Order_ID", "caption": "Order ID"}],
          "cols": [],
          "encodings": {"detail": [_agg_value("Sales", "Sales"),
                                   _agg_value("Quantity", "Quantity")]}}
    state = {"Values": {"projections": [
        {"field": {"Aggregation": {"Expression": {"Column": {
            "Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Sales"}},
            "Function": 0}}, "queryRef": "Sum(Orders.Sales)", "nativeQueryRef": "Sum of Sales"},
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}},
                               "Property": "Rank"}}, "queryRef": "_Measures.Rank",
         "nativeQueryRef": "Rank"}]},
        "Rows": {"projections": [{"field": {"Column": {
            "Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Order_ID"}},
            "queryRef": "Orders.Order_ID", "nativeQueryRef": "Order ID"}]}}
    return ws, state


def _chain_usage(composit_formula):
    return TableCalcUsage(
        worksheet="Sheet3", instance="[rank_inst]", column=_CID_B, caption="Rank",
        kind="field", formula="RANK([%s])" % _CID_A,
        scope_formulas={_CID_A: composit_formula, _CID_B: "RANK([%s])" % _CID_A},
        scope_captions={_CID_A: "composit Calc", _CID_B: "Rank"})


def test_apply_chain_no_chain_index_is_a_noop():
    ws, state = _chain_ws_state("RUNNING_SUM(SUM([Sales]))")
    assert _apply_formula_table_calc_chain(ws, state, {}, "Orders", {}, []) == (False, None)


def test_apply_chain_worksheet_not_in_index_is_a_noop():
    ws, state = _chain_ws_state("RUNNING_SUM(SUM([Sales]))")
    idx = {"OtherSheet": [_chain_usage("RUNNING_SUM(SUM([Sales]))")]}
    assert _apply_formula_table_calc_chain(ws, state, idx, "Orders", {}, []) == (False, None)


def test_apply_chain_no_value_projections_is_a_noop():
    ws, _ = _chain_ws_state("RUNNING_SUM(SUM([Sales]))")
    state = {"Values": {"projections": []}}
    idx = _view_only_field_chain_index([_chain_usage("RUNNING_SUM(SUM([Sales]))")])
    assert _apply_formula_table_calc_chain(ws, state, idx, "Orders", {}, []) == (False, None)


def test_apply_chain_blend_secondary_base_routes_to_review():
    composit = ("RUNNING_SUM(SUM([Sample - Superstore (copy)].[Sales])) * .15 "
                "+ RUNNING_SUM(SUM([Quantity])) * 15")
    ws, state = _chain_ws_state(composit)
    idx = _view_only_field_chain_index([_chain_usage(composit)])
    warnings = []
    handled, fact = _apply_formula_table_calc_chain(ws, state, idx, "Orders", {}, warnings)
    assert handled is False
    assert fact["status"] == "review" and fact["family"] == "FORMULA_TABLE_CALC"
    assert any("routed to review" in w["reason"] for w in warnings)


def test_apply_chain_displayed_calc_not_shown_routes_to_review():
    # remove the plain "Rank" value projection: the displayed calc is no longer the shown value,
    # so the router must fail closed (never ADD a stray column) and disclose a review.
    composit = "RUNNING_SUM(SUM([Sales])) * .15 + RUNNING_SUM(SUM([Quantity])) * 15"
    ws, state = _chain_ws_state(composit)
    state["Values"]["projections"] = [
        p for p in state["Values"]["projections"] if p.get("nativeQueryRef") != "Rank"]
    idx = _view_only_field_chain_index([_chain_usage(composit)])
    warnings = []
    handled, fact = _apply_formula_table_calc_chain(ws, state, idx, "Orders", {}, warnings)
    assert handled is False and fact["status"] == "review"


def test_apply_chain_success_emits_and_replaces_displayed_measure():
    composit = "RUNNING_SUM(SUM([Sales])) * .15 + RUNNING_SUM(SUM([Quantity])) * 15"
    ws, state = _chain_ws_state(composit)
    idx = _view_only_field_chain_index([_chain_usage(composit)])
    warnings = []
    handled, fact = _apply_formula_table_calc_chain(ws, state, idx, "Orders", {}, warnings)
    assert handled is True and warnings == []
    nrefs = {p["nativeQueryRef"] for p in state["Values"]["projections"]}
    assert "composit Calc" in nrefs and "Rank" in nrefs   # inner + outer VCs emitted
    assert fact["status"] == "emitted"


# ===========================================================================================
# Section G -- storage_mode: per-datasource Import / DirectQuery / needs-decision routing
# ===========================================================================================
def _desc(**kw):
    base = {
        "connection_class": "sqlserver", "server": "srv", "database": "db",
        "is_extract": False, "named_connection_count": 1,
        "relations": [{"kind": "table", "name": "Orders", "item": "Orders",
                       "columns": [{"model_name": "Sales", "tmdl_type": "double"}]}],
        "unsupported_reasons": [],
    }
    base.update(kw)
    return base


@pytest.mark.parametrize("cls", ["sqlserver", "azure_sqldb", "postgres", "snowflake", "oracle"])
def test_live_relational_routes_to_directquery(cls):
    assert select_storage_mode(_desc(connection_class=cls))["mode"] == "DirectQuery"


def test_extract_routes_to_import_even_on_live_connector():
    assert select_storage_mode(_desc(connection_class="sqlserver", is_extract=True))["mode"] \
        == "Import"


def test_flat_file_routes_to_import():
    assert select_storage_mode(
        _desc(connection_class="excel-direct", server=None, database=None))["mode"] == "Import"


def test_join_relation_tree_routes_to_needs_decision():
    d = select_storage_mode(_desc(relations=[{"kind": "join", "name": "Orders+People"}]))
    assert d["mode"] is None


def test_multi_named_connection_routes_to_needs_decision():
    assert select_storage_mode(_desc(named_connection_count=2))["mode"] is None


def test_unknown_connector_routes_to_needs_decision():
    assert select_storage_mode(_desc(connection_class="saphana"))["mode"] is None


def test_needs_decision_recommends_import_direct_to_source():
    d = select_storage_mode(_desc(relations=[{"kind": "join", "name": "Orders+People"}]))
    assert d["mode"] is None and d["recommended_mode"] == "Import"


# ===========================================================================================
# Section H -- table_calc_to_dax: the table-calc translate-vs-handoff decision
# ===========================================================================================
_TC_MEAS = {"Sales", "Profit"}
_TC_DATES = {"Order Date"}


def _tc_resolver(caption):
    col = caption.replace(" ", "_")
    if caption in _TC_MEAS:
        return ("Orders", col, "double")
    if caption in _TC_DATES:
        return ("Orders", col, "dateTime")
    return ("Orders", col, "string")


def _tc_pill(column, derivation="None"):
    return Pill(instance="%s:%s" % (derivation, column), column=column, derivation=derivation)


def _tc_usage(**kw):
    defaults = dict(
        worksheet="WS", instance="i", column="Profit", caption="Profit", kind="quick",
        calc_type="CumTotal", aggregation="Sum", ordering_type="Field",
        rows=[_tc_pill("Category"), _tc_pill("Sub-Category")], cols=[_tc_pill("Profit", "Sum")])
    defaults.update(kw)
    return TableCalcUsage(**defaults)


def test_field_scope_single_dim_cumtotal_translates():
    t = translate_table_calc_usage(_tc_usage(ordering_fields=["Category"]), _tc_resolver)
    assert t.status == "translated"
    assert t.translated_by == "deterministic (workbook addressing)"


@pytest.mark.parametrize("kw,label", [
    (dict(calc_type="Rank"), "rank"),
    (dict(ordering_type="Pane"), "pane-relative scope"),
    (dict(ordering_type="Columns"), "columns scope"),
    (dict(calc_type="TotalPercent"), "percent-of-total"),
    (dict(calc_type="WindowAvg"), "moving window"),
])
def test_scope_relative_or_windowed_calc_hands_off(kw, label):
    t = translate_table_calc_usage(_tc_usage(**kw), _tc_resolver)
    assert t.status == "handoff", label
