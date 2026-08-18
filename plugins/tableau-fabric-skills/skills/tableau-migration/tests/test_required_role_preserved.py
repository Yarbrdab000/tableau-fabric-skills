"""#143 -- a visual that loses a REQUIRED role must not ship as structurally invalid PBIR.

The reported symptom is real: `powerbi-report-author validate` fails pristine engine output with
``PBIR_ROLE_REQUIRED_MISSING`` for a clusteredColumnChart that carries ``Category`` and no ``Y``.

THE REPORTED CAUSE IS NOT THE MECHANISM, and that matters for the fix. The issue states the rule as
*"when a calc falls back to a stub, the engine drops its projection from the visual instead of
binding it"*. Measured against ``_crosscheck_report_refs``: a stub measure that IS emitted --
``measure 'Regional Revenue (FIXED)' = BLANK()`` -- binds normally and is NOT dropped. Only a
reference the model did not emit at all is dropped. So the reporter's preferred fix ("bind the stub
measure into the required role") cannot apply on this path: a projection only reaches the drop
branch when there is nothing in the model to bind it to.

What IS general, and is fixed here, is the OUTCOME rather than the cause. Dropping a projection
deleted its role, and a visual was emptied to a placeholder only when it lost EVERY role. Losing
just one required role left a partial ``queryState`` -- valid JSON, invalid PBIR, broken in Desktop.
This is the reporter's own second option ("drop the whole visual and record it as dropped -- lossier,
but still valid PBIR"), applied wherever a required role goes missing regardless of why.

``_REQUIRED_ROLES`` is HARVESTED from ``powerbi-report-author catalog describe`` (v0.1.4), the same
tool that raises the diagnostic, so it encodes what the validator enforces rather than what we
believe it enforces.
"""

import copy
import json

import pytest

import migrate_estate as M

MODEL = {
    "definition/tables/_Measures.tmdl": (
        "table _Measures\n"
        "\tmeasure Revenue = SUM('FACT_ORDERS'[Amount])\n"
        "\tmeasure 'Regional Revenue (FIXED)' = BLANK()\n"
    ),
    "definition/tables/FACT_ORDERS.tmdl": (
        "table FACT_ORDERS\n"
        "\tcolumn Nation\n"
        "\t\tdataType: string\n"
        "\tcolumn Amount\n"
        "\t\tdataType: double\n"
    ),
}


def _visual(measure_name, visual_type="clusteredColumnChart"):
    return {
        "name": "v-RegionalShare",
        "visual": {
            "visualType": visual_type,
            "query": {"queryState": {
                "Category": {"projections": [
                    {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "FACT_ORDERS"}},
                                          "Property": "Nation"}},
                     "queryRef": "FACT_ORDERS.Nation"}]},
                "Y": {"projections": [
                    {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}},
                                           "Property": measure_name}},
                     "queryRef": "_Measures.%s" % measure_name}]},
            }},
        },
    }


def _run(vis):
    parts = {"definition/pages/p1/visuals/v1/visual.json": json.dumps(vis)}
    out, drops, rebinds = M._crosscheck_report_refs(copy.deepcopy(parts), MODEL)
    after = json.loads(out["definition/pages/p1/visuals/v1/visual.json"])
    return after, drops, rebinds


def test_losing_a_required_role_empties_the_visual_instead_of_shipping_invalid_pbir():
    after, drops, _ = _run(_visual("Definitely Not Emitted"))
    assert len(drops) == 1
    assert drops[0]["emptied"] is True, (
        "a clusteredColumnChart that lost its required Y must be emptied, not shipped role-less")
    assert "query" not in after["visual"], "the partial queryState must not survive"


def test_a_stub_measure_still_binds_and_is_never_dropped():
    """Disproves the reported cause, and pins it so the fix cannot drift into over-dropping.

    An emitted ``= BLANK()`` stub is a real model object. Dropping it would lose a projection the
    author asked for, and the preserved TableauFormula annotation is what tells them what to restore.
    """
    after, drops, _ = _run(_visual("Regional Revenue (FIXED)"))
    assert drops == []
    y = after["visual"]["query"]["queryState"]["Y"]
    assert len(y["projections"]) == 1
    assert y["projections"][0]["field"]["Measure"]["Property"] == "Regional Revenue (FIXED)"


def test_a_translated_measure_is_unaffected():
    after, drops, _ = _run(_visual("Revenue"))
    assert drops == []
    assert after["visual"]["query"]["queryState"]["Y"]["projections"]


def test_losing_a_non_required_role_does_not_empty_the_visual():
    """Only REQUIRED roles force the empty -- otherwise this would be wildly over-broad.

    ``Tooltips`` is optional on a clusteredColumnChart, so losing it leaves a perfectly valid visual
    that must keep rendering.
    """
    vis = _visual("Revenue")
    vis["visual"]["query"]["queryState"]["Tooltips"] = {"projections": [
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}},
                               "Property": "Nope Not Here"}},
         "queryRef": "_Measures.Nope Not Here"}]}
    after, drops, _ = _run(vis)
    assert len(drops) == 1
    assert drops[0]["emptied"] is False, "an optional role going missing must not empty the visual"
    assert "query" in after["visual"]
    assert after["visual"]["query"]["queryState"]["Y"]["projections"]


def test_an_unknown_visual_type_is_never_emptied_on_this_account():
    """Fail-safe: 'we cannot judge' must not become 'delete it'."""
    after, drops, _ = _run(_visual("Definitely Not Emitted", visual_type="someFutureVisual"))
    assert len(drops) == 1
    assert drops[0]["emptied"] is False
    assert "query" in after["visual"]


@pytest.mark.parametrize("visual_type,role", [
    ("clusteredColumnChart", "Y"),
    ("lineChart", "Y"),
    ("pieChart", "Y"),
    ("scatterChart", "Y"),
    ("gauge", "Y"),
])
def test_required_roles_are_enforced_across_visual_types(visual_type, role):
    vis = _visual("Definitely Not Emitted", visual_type=visual_type)
    if visual_type in ("scatterChart", "gauge"):
        # these do not require Category; keep the fixture honest about what they need
        vis["visual"]["query"]["queryState"].pop("Category", None)
        if visual_type == "scatterChart":
            vis["visual"]["query"]["queryState"]["X"] = {"projections": [
                {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "FACT_ORDERS"}},
                                      "Property": "Amount"}},
                 "queryRef": "FACT_ORDERS.Amount"}]}
    after, drops, _ = _run(vis)
    assert drops and drops[0]["emptied"] is True, visual_type
    assert "query" not in after["visual"], visual_type


def test_a_visual_that_arrived_already_incomplete_is_left_alone():
    """Only a role THIS pass empties is ours to act on.

    A visual can arrive without a required role for reasons that have nothing to do with reference
    dropping -- a field-parameter axis expansion builds exactly that shape. Emptying those would be
    an unrelated behaviour change smuggled in under this fix, and it regressed
    ``test_field_parameter_on_a_chart_axis_is_expanded`` when the guard was written the broad way.
    """
    vis = _visual("Definitely Not Emitted")
    # arrives with NO Category at all -> already missing a required role before any drop, and keeps
    # an optional role so the drop cannot empty it via the pre-existing "lost every role" path.
    vis["visual"]["query"]["queryState"].pop("Category")
    vis["visual"]["query"]["queryState"]["Tooltips"] = {"projections": [
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}},
                               "Property": "Revenue"}},
         "queryRef": "_Measures.Revenue"}]}
    after, drops, _ = _run(vis)
    assert len(drops) == 1
    assert drops[0]["emptied"] is False, (
        "a visual that was already incomplete on arrival must not be emptied by this pass")
    assert "query" in after["visual"]


def test_the_required_role_table_matches_the_validator_that_enforces_it():
    """Spot-checks against `powerbi-report-author catalog describe` v0.1.4 output.

    Harvested, not authored. If the catalog ever disagrees, this table is the thing that is wrong.
    """
    import pbir_lint

    table = pbir_lint.REQUIRED_ROLES
    assert table["clusteredColumnChart"] == ("Category", "Y")
    assert table["scatterChart"] == ("X", "Y")
    assert table["kpi"] == ("Indicator",)
    assert table["pivotTable"] == ("Values",)
    assert table["cardVisual"] == ("Data",)
    assert table["decompositionTreeVisual"] == ("Analyze",)
    assert "someFutureVisual" not in table


def test_the_emitter_and_the_linter_read_the_same_table():
    """One table, two consumers -- the emitter that must not produce an invalid visual and the
    linter that must not let one through. Two copies would drift, and a gate drifting away from the
    emitter it guards is exactly what #137 was."""
    import pbir_lint

    assert M._required_roles_table() is pbir_lint.REQUIRED_ROLES


def test_a_field_parameter_binding_satisfies_a_required_role():
    """A role occupied only by a fieldParameters binding is still occupied.

    The rescue path rebinds a dangling measure to a field parameter; treating that role as empty
    would empty a visual the engine had just successfully repaired.
    """
    qs = {"Y": {"projections": [], "fieldParameters": [{"index": 0, "length": 1}]},
          "Category": {"projections": [{"queryRef": "x"}]}}
    assert M._missing_required_role({"visualType": "clusteredColumnChart"}, qs) is None
