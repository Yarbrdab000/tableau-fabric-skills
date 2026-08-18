"""Tests for ``_visuals_projecting_stub_measures`` -- the "structurally valid, semantically absent" gate.

When a calc cannot be translated the model emits ``measure 'X' = BLANK()`` so the reference still
resolves. It resolves PERFECTLY: the visual binds, `pbir_lint` is clean,
`lint_visual_model_bindings` is clean (the measure genuinely exists),
`powerbi-report-author validate` returns 0 errors -- and the chart renders EMPTY.

Measured on corpus workbook ``0136_custom_sql_prefix_and_params`` before 2.225.0: Sheet 3 projected
``complex nested``, which was a stub, while ``viz_fidelity`` recorded
``{"status": "rebuilt", "reason": null}``. The MODEL layer knew (the translation handoff listed the
calc as needs-review); the VISUAL layer never repeated it.

Every other gate in this module asks *is this well-formed*. This one asks *does it SAY anything*, and
that is why it has to read the measure's EXPRESSION rather than its existence.

The narrowness is the point: a measure that returns blank CONDITIONALLY -- ``IF(<cond>, 1)``, the
shape every keep-flag uses -- is doing its job, and flagging it would fire on correct output
constantly.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from migrate_estate import _visuals_projecting_stub_measures  # noqa: E402


def _model(*measures):
    body = "table _Measures\n"
    for name, expr in measures:
        body += "\n\tmeasure '%s' = %s\n\t\tlineageTag: x\n" % (name, expr)
    return {"definition/tables/_Measures.tmdl": body}


def _visual(name, *measure_names, page="page-ws-Sheet1"):
    projections = [{"field": {"Measure": {
        "Expression": {"SourceRef": {"Entity": "_Measures"}}, "Property": m}}}
        for m in measure_names]
    doc = {"name": name, "visual": {"visualType": "clusteredBarChart", "query": {
        "queryState": {"Y": {"projections": projections}}}}}
    return {"definition/pages/%s/visuals/%s/visual.json" % (page, name): json.dumps(doc)}


def test_a_visual_projecting_a_stub_is_reported_with_page_and_measure():
    out = _visuals_projecting_stub_measures(
        _model(("complex nested", "BLANK()")), _visual("v-Sheet3", "complex nested"))
    assert out == [{"visual": "v-Sheet3", "page": "page-ws-Sheet1", "measure": "complex nested"}]


def test_a_real_measure_is_not_reported():
    out = _visuals_projecting_stub_measures(
        _model(("Sales", "SUM('Orders'[Sales])")), _visual("v1", "Sales"))
    assert out == []


def test_a_conditionally_blank_measure_is_NOT_a_stub():
    """The load-bearing refusal.

    ``IF(<cond>, 1)`` is the shape every keep-flag in this project emits -- it returns BLANK for the
    rows it drops, which is its whole purpose. Matching "contains BLANK" or "can return blank" would
    fire on correct output constantly and the finding would be worthless within one release.
    """
    for expr in ("IF(SUM('Orders'[Sales]) > 100, 1)",
                 "IF(ISBLANK([X]), BLANK(), [X])",
                 "COALESCE([X], BLANK())"):
        out = _visuals_projecting_stub_measures(_model(("M", expr)), _visual("v1", "M"))
        assert out == [], expr


def test_stub_recognition_tolerates_parentheses_and_whitespace():
    for expr in ("BLANK()", "  BLANK()  ", "(BLANK())", "BLANK ( )"):
        out = _visuals_projecting_stub_measures(_model(("M", expr)), _visual("v1", "M"))
        assert out and out[0]["measure"] == "M", expr


def test_a_stub_NO_visual_projects_is_not_reported():
    """Silence about an unused stub is correct: nothing renders empty, so there is nothing to warn
    about. 0136 carries two such calcs after 2.225.0 and the report is clean."""
    out = _visuals_projecting_stub_measures(
        _model(("unused stub", "BLANK()")), _visual("v1", "Sales"))
    assert out == []


def test_several_visuals_and_several_stubs_are_all_named():
    model = _model(("a", "BLANK()"), ("b", "BLANK()"), ("real", "SUM('T'[X])"))
    parts = {}
    parts.update(_visual("v1", "a", "real", page="page-ws-P1"))
    parts.update(_visual("v2", "b", page="page-ws-P2"))
    out = _visuals_projecting_stub_measures(model, parts)
    assert [(o["visual"], o["measure"]) for o in out] == [("v1", "a"), ("v2", "b")]


def test_malformed_or_missing_input_does_not_raise():
    assert _visuals_projecting_stub_measures(None, None) == []
    assert _visuals_projecting_stub_measures({}, {}) == []
    assert _visuals_projecting_stub_measures(
        _model(("a", "BLANK()")),
        {"definition/pages/p/visuals/v/visual.json": "{not json"}) == []
