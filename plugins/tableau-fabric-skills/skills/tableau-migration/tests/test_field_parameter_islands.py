"""Per-island field-parameter correctness.

Three defects this covers, all of which produced a SILENTLY WRONG report rather than an error:

* a swap authored in one datasource island resolved its branch fields against the POOLED field
  index, which fails closed on the caption collision every consolidated workbook has -- so the
  whole swap was declined and the calc stubbed to a blank visual;
* ``extract_calculations`` deduped calcs by caption GLOBALLY, so the first island's formula
  answered for every island (Tableau scopes calculated fields per datasource);
* a field parameter bound to a dimension axis as a plain column projection makes Power BI group
  by the parameter's OPTION LABELS, repeating one total per label, instead of swapping the field.
"""

import json

import pytest

import migrate_estate as me
import parameters as P
import twb_to_pbir as tp


# -- island-scoped calc extraction ----------------------------------------------------------
def _wb(*datasources):
    parts = []
    for ds_name, cols in datasources:
        cs = "".join(
            '<column caption="%s" role="%s" datatype="real">'
            '<calculation class="tableau" formula="%s"/></column>' % (cap, role, formula)
            for cap, role, formula in cols)
        parts.append('<datasource name="%s" caption="%s">%s</datasource>' % (ds_name, ds_name, cs))
    return "<workbook><datasources>%s</datasources></workbook>" % "".join(parts)


def test_same_caption_different_formula_is_kept_per_island():
    xml = _wb(("Intake", [("Score", "measure", "SUM([A])")]),
              ("Assessments", [("Score", "measure", "SUM([B])")]))
    calcs, skipped = me.extract_calculations(xml)
    names = [c["name"] for c in calcs]
    assert names == ["Score", "Score (Assessments)"]
    assert [c["formula"] for c in calcs] == ["SUM([A])", "SUM([B])"]
    # the island copy still remembers the caption the workbook used
    assert calcs[1]["base_name"] == "Score"
    assert not [s for s in skipped if "Score" in str(s)]


def test_same_caption_same_formula_still_collapses():
    # the common case: worksheet-local <datasource-dependencies> copies of ONE field. Keeping
    # these would emit a duplicate model object for every worksheet that touches the calc.
    xml = _wb(("Intake", [("Score", "measure", "SUM([A])")]),
              ("Assessments", [("Score", "measure", "SUM([A])")]))
    calcs, _ = me.extract_calculations(xml)
    assert [c["name"] for c in calcs] == ["Score"]


def test_single_island_extraction_is_unchanged():
    xml = _wb(("Intake", [("Score", "measure", "SUM([A])"), ("Rate", "measure", "AVG([B])")]))
    calcs, _ = me.extract_calculations(xml)
    assert [c["name"] for c in calcs] == ["Score", "Rate"]
    assert all("base_name" not in c for c in calcs)


def test_island_qualified_name_fails_closed_without_a_datasource():
    # no datasource tag -> nothing to qualify WITH, so fall back to the historical duplicate drop
    # rather than inventing a name that no report reference could ever reconstruct.
    assert me._island_qualified_calc_name("Score", None, set()) is None
    assert me._island_qualified_calc_name("Score", "", set()) is None
    assert me._island_qualified_calc_name("Score", "Intake", {"score (intake)"}) is None
    assert me._island_qualified_calc_name("Score", "Intake", set()) == "Score (Intake)"


# -- island-scoped report binding -----------------------------------------------------------
def test_island_binding_key_is_tried_before_the_ambiguous_token():
    # two islands routinely SHARE a Tableau internal name (one datasource duplicated from the
    # other), so the token is ambiguous while the island is exact.
    keys = tp._measure_binding_candidate_keys(
        "tok1", "Calculation_123", "Score", "Sheet 1", island="Assessments")
    assert keys[0] == "Score (Assessments)"
    assert "Calculation_123" in keys and "tok1" in keys


def test_island_binding_key_absent_leaves_historical_priority():
    with_island = tp._measure_binding_candidate_keys(
        "tok1", "Calculation_123", "Score", "Sheet 1", island=None)
    without = tp._measure_binding_candidate_keys(
        "tok1", "Calculation_123", "Score", "Sheet 1")
    assert with_island == without
    assert with_island[0] == "tok1"


def test_island_binding_key_requires_both_parts():
    assert tp._island_binding_key("Score", None) is None
    assert tp._island_binding_key(None, "Intake") is None
    assert tp._island_binding_key("Score", "Intake") == "Score (Intake)"


# -- field-parameter axis expansion ---------------------------------------------------------
_SPEC = {
    "calc_name": "Show by Dimension",
    "table_name": "Show by Dimension",
    "display_col": "Show by Dimension",
    "role": "dimension",
    "default_index": 1,
    "entries": [
        {"label": "Issue Area", "table": "Program", "column": "IssueArea",
         "is_measure": False, "order": 0},
        {"label": "Program Name", "table": "Program", "column": "Name",
         "is_measure": False, "order": 1},
    ],
}


def _model_parts():
    return {
        "definition/tables/Show by Dimension.tmdl":
            "table 'Show by Dimension'\n\tcolumn 'Show by Dimension'\n\t\tdataType: string\n",
        "definition/tables/Program.tmdl":
            "table Program\n\tcolumn IssueArea\n\t\tdataType: string\n"
            "\tcolumn Name\n\t\tdataType: string\n",
    }


def _visual(visual_type, entity, prop):
    return json.dumps({"visual": {"visualType": visual_type, "query": {"queryState": {
        "Category": {"projections": [{
            "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}},
                                 "Property": prop}},
            "queryRef": "%s.%s" % (entity, prop)}]}}}}})


def _category(out, path):
    j = json.loads(out[path])
    return ((j["visual"]["query"]["queryState"]) or {}).get("Category") or {}


def test_field_parameter_on_a_chart_axis_is_expanded():
    parts = {"p/visuals/v1/visual.json":
             _visual("barChart", "Show by Dimension", "Show by Dimension")}
    out, drops, rebinds = me._crosscheck_report_refs(parts, _model_parts(), swap_specs=[_SPEC])
    cat = _category(out, "p/visuals/v1/visual.json")
    # the projection becomes a SEED on a concrete field ...
    col = cat["projections"][0]["field"]["Column"]
    assert col["Expression"]["SourceRef"]["Entity"] == "Program"
    assert col["Property"] == "Name"          # spec's default_index, not entries[0]
    # ... plus a sibling fieldParameters block naming the parameter's display column
    fp = cat["fieldParameters"][0]
    assert fp["parameterExpr"]["Column"]["Property"] == "Show by Dimension"
    assert fp["index"] == 0 and fp["length"] == 1
    assert not drops and rebinds


def test_field_parameter_on_a_slicer_is_left_alone():
    # a plain projection on the display column IS the correct picker shape -- expanding it would
    # destroy the parameter's own selector.
    parts = {"p/visuals/v1/visual.json":
             _visual("slicer", "Show by Dimension", "Show by Dimension")}
    before = dict(parts)
    out, drops, rebinds = me._crosscheck_report_refs(parts, _model_parts(), swap_specs=[_SPEC])
    assert out["p/visuals/v1/visual.json"] == before["p/visuals/v1/visual.json"]
    assert not drops and not rebinds


def test_ordinary_column_projection_is_untouched():
    parts = {"p/visuals/v1/visual.json": _visual("barChart", "Program", "IssueArea")}
    before = dict(parts)
    out, drops, rebinds = me._crosscheck_report_refs(parts, _model_parts(), swap_specs=[_SPEC])
    assert out["p/visuals/v1/visual.json"] == before["p/visuals/v1/visual.json"]
    assert not drops and not rebinds


def test_expansion_needs_no_swap_specs_to_stay_inert():
    parts = {"p/visuals/v1/visual.json":
             _visual("barChart", "Show by Dimension", "Show by Dimension")}
    before = dict(parts)
    out, _, rebinds = me._crosscheck_report_refs(parts, _model_parts(), swap_specs=None)
    assert out["p/visuals/v1/visual.json"] == before["p/visuals/v1/visual.json"]
    assert not rebinds


def test_default_entry_helper_is_bounds_checked():
    assert tp._fp_default_entry(_SPEC)["column"] == "Name"
    assert tp._fp_default_entry(dict(_SPEC, default_index=99))["column"] == "IssueArea"
    assert tp._fp_default_entry(dict(_SPEC, default_index=None))["column"] == "IssueArea"
    assert tp._fp_default_entry({"entries": []}) is None


# -- per-island field locator for swaps -----------------------------------------------------
def test_emit_field_parameters_uses_the_per_calc_locator():
    # the POOLED locator sees the caption in two island copies and fails closed; the island-scoped
    # one resolves it. Without field_locator_for the swap is declined and the calc stubs.
    calc = {"name": "By Dim", "role": "dimension", "datasource": "Intake",
            "formula": 'case [Parameters].[p] when 1 then [Segment] when 2 then [Region] END'}

    def pooled(field):
        return None  # ambiguous across islands

    def scoped(field):
        return ("Orders (Intake)", field, False)

    declined = P.emit_field_parameters([calc], field_locator=pooled, existing_tables=[])
    assert declined["specs"] == []

    rescued = P.emit_field_parameters([calc], field_locator=pooled, existing_tables=[],
                                      field_locator_for=lambda c: scoped)
    assert [e["table"] for e in rescued["specs"][0]["entries"]] == \
        ["Orders (Intake)", "Orders (Intake)"]


def test_per_calc_locator_returning_none_falls_back_to_the_pooled_one():
    calc = {"name": "By Dim", "role": "dimension",
            "formula": 'case [Parameters].[p] when 1 then [Segment] when 2 then [Region] END'}
    out = P.emit_field_parameters([calc], field_locator=lambda f: ("Orders", f, False),
                                  existing_tables=[], field_locator_for=lambda c: None)
    assert [e["table"] for e in out["specs"][0]["entries"]] == ["Orders", "Orders"]
