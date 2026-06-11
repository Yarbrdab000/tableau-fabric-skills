"""Tests for the Tier-0 -> Tier-1 TRANSLATION HANDOFF (the deterministic engine's honest report
of what it could and could NOT faithfully translate, plus a structured request a second compiler
consumes).

By design the deterministic tier owns only the provably-1:1 safe subset; the hard, varied tail
(argmax / INCLUDE-EXCLUDE / nested LODs, regex, ...) is HANDED OFF rather than force-fit into
fragile bespoke DAX. These tests lock:
  * ``field_references`` -- distinct, ordered, bare-vs-qualified, tolerant of un-tokenizable input;
  * ``translation_handoff_artifact`` -- the summary counts, the failover-check-in ``needs_review``
    list, and the per-calc structured ``requests`` (resolved field types, cross-calc refs,
    parameters), emitting NO DAX and NO model objects;
  * that it is purely additive on the assembled report.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

import assemble_model as A  # noqa: E402
from calc_to_dax import field_references  # noqa: E402
from test_connection_to_m import LIVE_SQLSERVER  # noqa: E402


# Shared resolver: caption -> (table_display_name, clean_col, tmdl_type).
_FIELDS = {
    "Sales": ("Orders", "Sales", "double"),
    "Profit": ("Orders", "Profit", "double"),
    "State": ("Orders", "State", "string"),
    "City": ("Orders", "City", "string"),
}


def _resolver(caption):
    return _FIELDS.get(caption)


# --------------------------------------------------------------------------- field_references
def test_field_references_distinct_in_order():
    refs = field_references("SUM([Sales]) / SUM([Profit]) + [Sales]")
    assert [r["caption"] for r in refs] == ["Sales", "Profit"]
    assert all(r["qualified"] is False for r in refs)
    assert refs[0]["parts"] == ["Sales"]


def test_field_references_qualified_parameter_chain():
    refs = field_references("[Sales] * [Parameters].[Growth Rate]")
    assert refs[0] == {"caption": "Sales", "qualified": False, "parts": ["Sales"]}
    q = refs[1]
    assert q["qualified"] is True
    assert q["parts"] == ["Parameters", "Growth Rate"]
    assert q["caption"] == "[Parameters].[Growth Rate]"


def test_field_references_tolerates_garbage():
    # an unterminated reference cannot be tokenized -> empty, never raises
    assert field_references("SUM([Sales") == []
    assert field_references("") == []
    assert field_references(None) == []


# --------------------------------------------------------------------------- handoff artifact
_MEASURES = [
    {"measure": "Profit Ratio", "status": "translated", "reason": "ok",
     "dax": "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
     "tableau_formula": "SUM([Profit])/SUM([Sales])"},
    {"measure": "Top City", "status": "assisted-suggested", "reason": "fallback",
     "tableau_formula": "IF [MaxCalc] = {fixed [State],[City]:SUM([Sales])} THEN [City] END",
     "assisted_suggestion": {"pattern": "argmax-dimension", "dax": "CONCATENATEX(...)"}},
    {"measure": "Mystery", "status": "stub", "reason": "unsupported function FOO",
     "tableau_formula": "FOO([Bar]) + [Parameters].[Region]"},
]
_COLUMNS = [
    {"column": "Top City Name", "table": "Orders", "status": "stub",
     "reason": "LOD expression not valid in a row-level column calc",
     "tableau_formula": "IF {fixed [State],[City]:SUM([Sales])} = [MaxCalc] THEN [City] END"},
]
_LOOKUP = {"maxcalc": "{fixed [State] : MAX({fixed [State],[City]:SUM([Sales])})}"}


def _artifact():
    return A.translation_handoff_artifact(_MEASURES, _COLUMNS, _resolver, calc_lookup=_LOOKUP)


def test_handoff_summary_counts_live_vs_review():
    s = _artifact()["summary"]
    assert s["total"] == 4
    assert s["live"] == 1            # only the deterministically-translated measure
    assert s["needs_review"] == 3    # 1 assisted-suggested + 2 stubs
    assert (s["translated"], s["assisted_approved"], s["assisted_suggested"], s["stub"]) == (1, 0, 1, 2)
    assert s["coverage_pct"] == 25.0


def test_handoff_coverage_pct_none_when_empty():
    s = A.translation_handoff_artifact([], [], _resolver)["summary"]
    assert s["total"] == 0
    assert s["coverage_pct"] is None


def test_handoff_needs_review_lists_every_fallback():
    nr = {r["name"]: r for r in _artifact()["needs_review"]}
    assert set(nr) == {"Top City", "Mystery", "Top City Name"}
    assert nr["Top City"]["role"] == "measure" and nr["Top City"]["has_suggestion"] is True
    assert nr["Top City Name"]["role"] == "dimension" and nr["Top City Name"]["has_suggestion"] is False
    assert nr["Mystery"]["fallback_reason"] == "unsupported function FOO"


def test_handoff_request_resolves_field_types_and_target():
    reqs = {r["name"]: r for r in _artifact()["requests"]}
    col = reqs["Top City Name"]
    assert col["role"] == "dimension" and col["target_table"] == "Orders"
    by_cap = {f["caption"]: f for f in col["fields"]}
    assert by_cap["State"] == {"caption": "State", "kind": "field",
                               "table": "Orders", "column": "State", "type": "string"}
    assert by_cap["City"]["kind"] == "field" and by_cap["City"]["type"] == "string"
    # the cross-calc reference resolves to the OTHER calc's formula via calc_lookup
    assert by_cap["MaxCalc"]["kind"] == "calc"
    assert "MAX" in by_cap["MaxCalc"]["references_formula"]


def test_handoff_request_marks_parameters_and_unresolved():
    reqs = {r["name"]: r for r in _artifact()["requests"]}
    myst = reqs["Mystery"]
    by_cap = {f["caption"]: f for f in myst["fields"]}
    assert by_cap["Bar"]["kind"] == "unresolved"
    assert by_cap["[Parameters].[Region]"]["kind"] == "parameter"


def test_handoff_passes_through_existing_suggestion():
    reqs = {r["name"]: r for r in _artifact()["requests"]}
    top = reqs["Top City"]
    assert top["target_table"] == "_Measures"
    assert top["has_suggestion"] is True
    assert top["suggestion"]["pattern"] == "argmax-dimension"


def test_handoff_is_additive_on_assembled_report():
    # a real assembled report carries translation_handoff alongside every pre-existing key
    out = A.migrate_tds_to_semantic_model(
        LIVE_SQLSERVER, model_name="HandoffSmoke",
        calcs=[{"name": "Total Sales", "formula": "SUM([Sales])"}],
    )
    report = out["report"]
    for key in ("measures", "calc_columns", "assisted_suggestions",
                "calc_coverage", "calc_column_coverage"):
        assert key in report
    ho = report["translation_handoff"]
    assert set(ho) == {"summary", "needs_review", "requests"}
    assert ho["summary"]["total"] >= 1
