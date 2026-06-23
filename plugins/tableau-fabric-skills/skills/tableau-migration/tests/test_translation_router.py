"""Tests for the deterministic Tier-0 -> Tier-1 ROUTER (``translation_router.classify_fallback``).

The router turns the deterministic engine's honest free-text ``fallback_reason`` into a STABLE
charter category + concrete agent guidance, so the second compiler acts on a fixed vocabulary
instead of re-parsing prose. These tests lock:
  * each real Tier-0 reason string -> its expected category (using the actual messages the engine
    emits, harvested from ``calc_to_dax``);
  * the structural parameter signal (a ``[Parameters].[x]`` field) taking precedence;
  * graceful handling of empty/None/unknown reasons;
  * that classification is purely additive on the assembled handoff manifest (every request +
    needs_review entry gains a ``category`` and the summary gains a ``categories`` count map).
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

import assemble_model as A  # noqa: E402
import translation_router as R  # noqa: E402


# --------------------------------------------------------------------------- category mapping
def _cat(reason, **kw):
    return R.classify_fallback(reason, **kw)["category"]


def test_real_table_calc_function_reasons_route_to_addressing():
    for fn in ("WINDOW_SUM", "RUNNING_AVG", "RANK", "INDEX", "SIZE", "FIRST", "LAST",
               "LOOKUP", "PREVIOUS_VALUE", "TOTAL", "RANK_PERCENTILE", "WINDOW_PERCENTILE"):
        assert _cat("unsupported function " + fn) == R.MISSING_ADDRESSING_INTENT, fn


def test_table_calc_seam_reasons_route_to_addressing():
    assert _cat("table calc requires an explicit order-by spec") == R.MISSING_ADDRESSING_INTENT
    assert _cat("unresolved/ambiguous partition field [Foo]") == R.MISSING_ADDRESSING_INTENT
    assert _cat("not a table calculation") == R.MISSING_ADDRESSING_INTENT


def test_lod_reasons_route_to_outer_aggregation():
    for reason in (
        "only FIXED LOD is translated (INCLUDE/EXCLUDE fall back)",
        "SUM cannot re-aggregate a FIXED LOD",
        "re-aggregating a table-scoped LOD is not supported",
        "nested FIXED LOD does not fix a superset of the enclosing LOD",
        "AVG over an LOD requires a numeric inner expression",
        "LOD expression not valid in a row-level column calc",
    ):
        assert _cat(reason) == R.MISSING_OUTER_AGGREGATION, reason


def test_regex_and_no_native_functions_route_to_language_gap():
    for fn in ("REGEXP_MATCH", "REGEXP_EXTRACT", "REGEXP_EXTRACT_NTH", "REGEXP_REPLACE",
               "DATEPARSE", "ISDATE", "SPLIT", "FINDNTH",
               # no faithful native DAX target -- deliberately fail-closed in the compiler
               "TRIM", "LTRIM", "RTRIM", "WEEK", "ISOQUARTER",
               "MAKETIME", "MAKEDATETIME", "HEXBINX", "HEXBINY", "STR"):
        assert _cat("unsupported function " + fn) == R.DAX_LANGUAGE_GAP, fn
    assert _cat("ordered text comparison is case-sensitive in Tableau; no faithful DAX form") \
        == R.DAX_LANGUAGE_GAP
    assert _cat("unsupported DATEPART part 'iso-week'") == R.DAX_LANGUAGE_GAP
    assert _cat("unsupported DATEADD part 'fortnight'") == R.DAX_LANGUAGE_GAP


def test_unsupported_table_calculation_prefix_is_classified():
    # the table-calc path reports "unsupported table calculation <NAME>" (vs "unsupported function
    # <NAME>" in measure/column mode); both prefixes must classify the same way.
    # RANK_UNIQUE's tie-break follows Tableau's internal addressing order -> a hard DAX gap.
    assert _cat("unsupported table calculation RANK_UNIQUE") == R.DAX_LANGUAGE_GAP
    assert _cat("unsupported function RANK_UNIQUE") == R.DAX_LANGUAGE_GAP
    # PREVIOUS_VALUE is recoverable once addressing is supplied -> addressing intent (both prefixes).
    assert _cat("unsupported table calculation PREVIOUS_VALUE") == R.MISSING_ADDRESSING_INTENT
    assert _cat("unsupported table calculation TOTAL") == R.MISSING_ADDRESSING_INTENT


def test_parameter_reason_and_structural_signal_route_to_model_object():
    assert _cat("parameter reference [Parameters].[Growth Rate] (unmodeled)") \
        == R.MODEL_OBJECT_PARAMETER
    # structural: a [Parameters].[x] field wins even when the reason text is generic
    fields = [{"caption": "[Parameters].[Region]", "kind": "parameter"},
              {"caption": "Sales", "kind": "field"}]
    assert _cat("unsupported function FOO", fields=fields) == R.MODEL_OBJECT_PARAMETER


def test_type_and_shape_reasons_route_to_type_mismatch():
    for reason in (
        "IF branches return inconsistent types",
        "incomparable types in comparison",
        "4-arg IIF (unknown branch) not supported",
        "booleans support only = and <> comparison",
        "aggregation SUM not valid in a row-level column calc",
        "PERCENTILE not valid in a row-level column calc",
        # the mirror case: a row-level expression with no aggregation used as a measure
        # (e.g. IF [Region]="east" THEN [Sales] END) -- wrap in an aggregation or emit as a column
        "bare row-level field [..] not valid in a measure",
        "expected a numeric expression",
        "unterminated field reference",
        "unsupported character ']'",
    ):
        assert _cat(reason) == R.TYPE_OR_SHAPE_MISMATCH, reason


def test_unresolved_reference_reasons_route_to_reference():
    for reason in (
        "unresolved/ambiguous field [Bar]",
        "cross-table terms (fields span multiple tables)",
        "cross-table FIXED LOD dimensions not supported",
        "unsupported field type geography for [Region]",
        "ROUND requires a numeric field, got string for [City]",
    ):
        assert _cat(reason) == R.UNRESOLVED_REFERENCE, reason


def test_unknown_and_empty_reasons_fall_through_safely():
    assert _cat("unsupported function CORR") == R.UNSUPPORTED_OTHER  # has a closed form -> other
    assert _cat("some brand new reason text") == R.UNSUPPORTED_OTHER
    assert _cat("") == R.UNSUPPORTED_OTHER
    assert _cat(None) == R.UNSUPPORTED_OTHER


def test_every_category_has_guidance():
    for cat in R.CATEGORIES:
        # classify never returns a category without matching guidance
        assert cat in R._GUIDANCE and R._GUIDANCE[cat].strip()


def test_classify_returns_guidance_with_category():
    out = R.classify_fallback("unsupported function WINDOW_SUM")
    assert out["category"] == R.MISSING_ADDRESSING_INTENT
    assert "Compute Using" in out["guidance"] or "addressing" in out["guidance"]


def test_precedence_parameter_beats_addressing():
    # a parameter-bearing table-calc formula is still a model-object decision first
    fields = [{"caption": "[Parameters].[Window]", "kind": "parameter"}]
    assert _cat("unsupported function WINDOW_SUM", fields=fields) == R.MODEL_OBJECT_PARAMETER


# --------------------------------------------------------------------------- additive on artifact
_MEASURES = [
    {"measure": "Profit Ratio", "status": "translated", "reason": "ok",
     "dax": "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
     "tableau_formula": "SUM([Profit])/SUM([Sales])"},
    {"measure": "Running Sales", "status": "stub",
     "reason": "unsupported function RUNNING_SUM",
     "tableau_formula": "RUNNING_SUM(SUM([Sales]))"},
    {"measure": "Region Pick", "status": "stub",
     "reason": "parameter reference [Parameters].[Region] (unmodeled)",
     "tableau_formula": "IF [Parameters].[Region] = [Region] THEN [Sales] END"},
    {"measure": "Regex Flag", "status": "stub",
     "reason": "unsupported function REGEXP_MATCH",
     "tableau_formula": "REGEXP_MATCH([City], '^San')"},
]


def _fields(_caption):
    return None  # nothing resolves -> exercises the reason-text path end to end


def test_artifact_requests_carry_category_and_guidance():
    art = A.translation_handoff_artifact(_MEASURES, [], _fields)
    reqs = {r["name"]: r for r in art["requests"]}
    assert reqs["Running Sales"]["category"] == R.MISSING_ADDRESSING_INTENT
    assert reqs["Region Pick"]["category"] == R.MODEL_OBJECT_PARAMETER
    assert reqs["Regex Flag"]["category"] == R.DAX_LANGUAGE_GAP
    assert all(r["category_guidance"].strip() for r in art["requests"])


def test_artifact_needs_review_and_summary_categories():
    art = A.translation_handoff_artifact(_MEASURES, [], _fields)
    nr = {r["name"]: r for r in art["needs_review"]}
    assert nr["Running Sales"]["category"] == R.MISSING_ADDRESSING_INTENT
    cats = art["summary"]["categories"]
    assert cats[R.MISSING_ADDRESSING_INTENT] == 1
    assert cats[R.MODEL_OBJECT_PARAMETER] == 1
    assert cats[R.DAX_LANGUAGE_GAP] == 1
    assert sum(cats.values()) == art["summary"]["needs_review"]


# --------------------------------------------------------------------------- candidate-DAX gate
def test_gate_accepts_well_formed_candidate():
    out = R.check_candidate_dax('DIVIDE(SUM(\'Orders\'[Profit]), SUM(\'Orders\'[Sales]))')
    assert out["ok"] is True
    assert out["issues"] == []


def test_gate_accepts_string_literal_with_delimiters_inside():
    # parens/brackets that live INSIDE a quoted string must not count toward the balance
    out = R.check_candidate_dax('"a) [b] (c"')
    assert out["ok"] is True
    assert out["issues"] == []


def test_gate_rejects_empty_and_whitespace():
    for bad in ("", "   ", None):
        out = R.check_candidate_dax(bad)
        assert out["ok"] is False
        assert any("empty" in i for i in out["issues"])


def test_gate_rejects_inert_stub_candidate():
    for stub in ("0", " BLANK() ", "blank()"):
        out = R.check_candidate_dax(stub)
        assert out["ok"] is False
        assert any("inert stub" in i for i in out["issues"])


def test_gate_rejects_unbalanced_parens():
    out = R.check_candidate_dax("DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales])")
    assert out["ok"] is False
    assert any("unclosed" in i for i in out["issues"])


def test_gate_rejects_extra_closer():
    out = R.check_candidate_dax("SUM('Orders'[Sales]))")
    assert out["ok"] is False
    assert any("unbalanced" in i for i in out["issues"])


def test_gate_rejects_mismatched_delimiters():
    out = R.check_candidate_dax("SUM('Orders'[Sales)]")
    assert out["ok"] is False
    assert any("mismatched" in i or "unclosed" in i or "unbalanced" in i for i in out["issues"])


def test_gate_rejects_unterminated_string():
    out = R.check_candidate_dax('CONCATENATE("a, "b")')
    assert out["ok"] is False
    assert any("unterminated string" in i for i in out["issues"])


def test_gate_rejects_leftover_tableau_lod_brace():
    out = R.check_candidate_dax("CALCULATE(SUM('Orders'[Sales]), {FIXED [Region]})")
    assert out["ok"] is False
    assert any("Tableau idiom" in i for i in out["issues"])


def test_gate_rejects_leftover_parameters_reference():
    out = R.check_candidate_dax("IF([Parameters].[Region] = 1, 1, 0)")
    assert out["ok"] is False
    assert any("Tableau idiom" in i for i in out["issues"])


def test_gate_warns_on_language_gap_approximation():
    req = {"category": R.DAX_LANGUAGE_GAP}
    out = R.check_candidate_dax("LEFT('Orders'[City], 3)", request=req)
    assert out["ok"] is True
    assert any("oracle-verified" in w for w in out["warnings"])


def test_gate_no_warning_when_no_request():
    out = R.check_candidate_dax("LEFT('Orders'[City], 3)")
    assert out["ok"] is True
    assert out["warnings"] == []


def test_gate_never_raises_on_garbage():
    for junk in (123, [], {}, object()):
        out = R.check_candidate_dax(junk)
        assert "ok" in out and "issues" in out
