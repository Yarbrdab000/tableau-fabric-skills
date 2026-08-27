"""A PBIR binding that names the WRONG TABLE is disclosed at build time (#166 Case A).

THE REPORT. A 12-workbook Snowflake estate. Tableau writes a colliding base name as
``<Field> (<Object>)``; the MODEL layer resolves that disambiguation and the REPORT layer did not,
so a slicer and a chart bound to ``'Custom SQL Query'[NEW_TECHNOLOGY]`` when the column belongs to
``'Custom SQL Query (Upgrade Aircraft Installs)'`` -- a table whose column set contains neither
field. Power BI reports ``Missing_References``, but only once real data exists: while the partition
is a stub there is nothing to separate *"wrong table, still empty"* from *"right table, still
empty"*, which is why it survived to a customer.

WHY NOTHING CAUGHT IT. ``_crosscheck_report_refs`` already validates every projection against the
emitted model, and it passed this. It validates against ``_model_object_names``, which flattens
every column in the model into ONE set -- right for its actual job (dropping an optimistic
``_Measures[caption]`` bind that names nothing at all) and **table-blind**, so a reference to the
wrong entity resolves as long as some other table owns a column of that name. The gate could not
express the defect, so being more careful with it would not have helped.

DISCLOSED, NOT DROPPED, and the reason is measured rather than cautious. Across the 34-workbook
corpus at 2.332.0 -- 544 visual query bindings, each report resolved through its OWN
``definition.pbir`` byPath pointer -- there are **0** entity mismatches. The cross-check's contract
is *drop rather than mis-bind*; changing it on a path with no measured subject would be a
behaviour change justified by nothing.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from migrate_estate import _entity_binding_mismatches  # noqa: E402

BASE = ("table 'Custom SQL Query'\n"
        "\tcolumn TAIL\n\t\tdataType: string\n\t\tsourceColumn: TAIL\n"
        "\tcolumn TECHNOLOGY\n\t\tdataType: string\n\t\tsourceColumn: TECHNOLOGY\n")
DISAMB = ("table 'Custom SQL Query (Upgrade Aircraft Installs)'\n"
          "\tcolumn NEW_TECHNOLOGY\n\t\tdataType: string\n\t\tsourceColumn: NEW_TECHNOLOGY\n"
          "\tcolumn OLD_TECHNOLOGY\n\t\tdataType: string\n\t\tsourceColumn: OLD_TECHNOLOGY\n")
MODEL = {"definition/tables/Custom SQL Query.tmdl": BASE,
         "definition/tables/Custom SQL Query (Upgrade Aircraft Installs).tmdl": DISAMB}


def _visual(entity, prop):
    return {"visual": {"query": {"queryState": {"Category": {"projections": [
        {"field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}},
                              "Property": prop}},
         "queryRef": "%s.%s" % (entity, prop)}]}}}}}


def _parts(entity, prop, path="definition/pages/p/visuals/v/visual.json"):
    return {path: json.dumps(_visual(entity, prop))}


def test_the_reported_defect_is_disclosed_with_the_real_owner():
    out = _entity_binding_mismatches(_parts("Custom SQL Query", "NEW_TECHNOLOGY"), MODEL)
    assert len(out) == 1, out
    assert out[0]["entity"] == "Custom SQL Query"
    assert out[0]["column"] == "NEW_TECHNOLOGY"
    # Naming the OWNER is the whole value: "this is wrong" without "it lives there" leaves the
    # reader exactly where they were.
    assert out[0]["owned_by"] == ["custom sql query (upgrade aircraft installs)"]


def test_a_correct_binding_discloses_nothing():
    assert _entity_binding_mismatches(
        _parts("Custom SQL Query (Upgrade Aircraft Installs)", "NEW_TECHNOLOGY"), MODEL) == []
    assert _entity_binding_mismatches(_parts("Custom SQL Query", "TAIL"), MODEL) == []


def test_a_column_that_dangles_EVERYWHERE_is_left_to_the_cross_check():
    """Not this function's business, and double-reporting one defect as two teaches a reader that
    a hit might be either."""
    assert _entity_binding_mismatches(_parts("Custom SQL Query", "NO_SUCH_COLUMN"), MODEL) == []


def test_an_unknown_entity_is_left_to_the_cross_check():
    assert _entity_binding_mismatches(_parts("No Such Table", "NEW_TECHNOLOGY"), MODEL) == []


def test_case_insensitive_like_the_engine():
    out = _entity_binding_mismatches(_parts("CUSTOM SQL QUERY", "new_technology"), MODEL)
    assert len(out) == 1, out


def test_a_measure_reference_is_not_a_column_binding():
    """Measures live on a hidden table and are legitimately projected from anywhere.

    The Property here is deliberately a name that IS a column on another table. An earlier version
    used ``Total`` -- which is a column nowhere -- so a build that mistakenly treated Measure nodes
    as column bindings still filtered it out on the *no-owner* branch and the test passed. The
    injection "treats a Measure ref as a column binding" went undetected until the fixture was made
    able to distinguish the two paths.
    """
    v = {"visual": {"query": {"queryState": {"Y": {"projections": [
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Custom SQL Query"}},
                               "Property": "NEW_TECHNOLOGY"}}}]}}}}}
    assert _entity_binding_mismatches({"definition/x/visual.json": json.dumps(v)}, MODEL) == []


def test_only_real_query_bindings_are_examined():
    """A field reference outside a ``query`` block is metadata, not a binding. Scanning everything
    is how a probe reports on a population it was never asked about."""
    v = {"config": {"someMetadata": {"Column": {
        "Expression": {"SourceRef": {"Entity": "Custom SQL Query"}},
        "Property": "NEW_TECHNOLOGY"}}}}
    assert _entity_binding_mismatches({"definition/x/visual.json": json.dumps(v)}, MODEL) == []


def test_no_model_and_malformed_parts_are_survived():
    assert _entity_binding_mismatches(_parts("Custom SQL Query", "NEW_TECHNOLOGY"), {}) == []
    assert _entity_binding_mismatches({"definition/x/visual.json": "{not json"}, MODEL) == []
    assert _entity_binding_mismatches(None, MODEL) == []


def test_non_visual_parts_are_ignored():
    """report.json / pages.json carry no visual bindings; reading them widens the population."""
    p = {"definition/report.json": json.dumps(_visual("Custom SQL Query", "NEW_TECHNOLOGY"))}
    assert _entity_binding_mismatches(p, MODEL) == []
