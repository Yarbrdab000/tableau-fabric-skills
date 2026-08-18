"""#144 -- the linter must catch structurally-invalid PBIR the engine can emit (R9).

#143 was one instance; this is the systemic gap that let it ship green. A run graded
``definition_of_done: warn`` / ``0 error`` / ``Viz=built`` over a report that
``powerbi-report-author validate`` refused with ``PBIR_ROLE_REQUIRED_MISSING`` and exit 1.

The reporter framed the boundary precisely, and R9 is the missing half of a pair:

* ``lint_visual_model_bindings`` covers *validate is blind and we can see* -- a binding to a model
  object that does not exist validates clean and renders EMPTY.
* **R9** covers the reverse, *validate can see and we were blind* -- a missing required role is
  structurally invalid, and the only tool that reported it was an opt-in npm pre-gate an ordinary
  run never reaches.

``REQUIRED_ROLES`` is harvested from ``powerbi-report-author catalog describe`` (v0.1.4), the same
tool that raises the diagnostic, and is the SINGLE table ``migrate_estate`` also reads when deciding
whether a projection drop left a visual invalid.
"""

import json

import pytest

import pbir_lint as P


def _visual(visual_type, roles):
    """A visual.json whose queryState declares exactly ``roles`` (each with one projection)."""
    qs = {}
    for r in roles:
        qs[r] = {"projections": [{"field": {"Column": {
            "Expression": {"SourceRef": {"Entity": "T"}}, "Property": "C"}},
            "queryRef": "T.C"}]}
    return json.dumps({"name": "v1",
                       "visual": {"visualType": visual_type,
                                  "query": {"queryState": qs}}})


def _lint(parts):
    return [p for p in P.lint_pbir_parts(parts) if "requires role" in p]


def test_a_missing_required_role_is_reported():
    """The exact shape from #143: a column chart with Category and no Y."""
    parts = {"definition/pages/p1/visuals/v1/visual.json":
             _visual("clusteredColumnChart", ["Category"])}
    problems = _lint(parts)
    assert len(problems) == 1
    assert "'Y'" in problems[0]
    assert "PBIR_ROLE_REQUIRED_MISSING" in problems[0]


def test_a_complete_visual_is_clean():
    parts = {"definition/pages/p1/visuals/v1/visual.json":
             _visual("clusteredColumnChart", ["Category", "Y"])}
    assert _lint(parts) == []


def test_an_emptied_placeholder_visual_is_not_flagged():
    """The VALID outcome this rule steers toward must not itself be the defect.

    ``migrate_estate`` empties a visual that lost a required role by dropping ``query`` wholesale.
    Flagging that would make the fix for #143 trip the gate for #144 -- the two would deadlock.
    """
    parts = {"definition/pages/p1/visuals/v1/visual.json":
             json.dumps({"name": "v1", "visual": {"visualType": "clusteredColumnChart"}})}
    assert _lint(parts) == []


def test_an_unknown_visual_type_is_not_judged():
    """'Cannot judge' must never become 'declare invalid' -- a new visual type is not a defect."""
    parts = {"definition/pages/p1/visuals/v1/visual.json":
             _visual("someFutureVisual", ["Whatever"])}
    assert _lint(parts) == []


@pytest.mark.parametrize("visual_type,present,missing", [
    ("lineChart", ["Category"], "Y"),
    ("pieChart", ["Y"], "Category"),
    ("scatterChart", ["X"], "Y"),
    ("kpi", [], "Indicator"),
    ("pivotTable", [], "Values"),
    ("cardVisual", [], "Data"),
])
def test_required_roles_are_enforced_across_types(visual_type, present, missing):
    parts = {"definition/pages/p1/visuals/v1/visual.json": _visual(visual_type, present or ["Zzz"])}
    problems = _lint(parts)
    assert problems, "%s missing %s should be reported" % (visual_type, missing)
    assert repr(missing) in problems[0]


def test_a_field_parameter_binding_occupies_the_role():
    """A role held only by a fieldParameters binding is occupied -- the rescue path builds that."""
    doc = {"name": "v1", "visual": {"visualType": "clusteredColumnChart", "query": {"queryState": {
        "Category": {"projections": [{"queryRef": "T.C"}]},
        "Y": {"projections": [], "fieldParameters": [{"index": 0, "length": 1}]},
    }}}}
    assert _lint({"definition/pages/p1/visuals/v1/visual.json": json.dumps(doc)}) == []


def test_malformed_input_never_raises():
    """The linter runs on every migration; it must degrade rather than break the build."""
    for junk in ("not json at all", "[]", "{}", json.dumps({"visual": "a string"}),
                 json.dumps({"visual": {"visualType": "clusteredColumnChart", "query": "nope"}})):
        P.lint_pbir_parts({"definition/pages/p1/visuals/v1/visual.json": junk})


def test_the_rule_participates_in_the_public_entry_point():
    """R9 must be wired into ``lint_pbir_parts``, not merely defined.

    A rule that exists but is never called is the same failure as #141 -- a check reporting nothing
    because it never ran.
    """
    parts = {"definition/pages/p1/visuals/v1/visual.json":
             _visual("clusteredColumnChart", ["Category"])}
    assert any("requires role" in p for p in P.lint_pbir_parts(parts))
