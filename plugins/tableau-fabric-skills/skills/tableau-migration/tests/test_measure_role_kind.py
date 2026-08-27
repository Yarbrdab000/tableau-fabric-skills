"""A Measure-only role must not carry a bare Column (#142).

THE REPORT. ``powerbi-report-author validate`` fails the engine's own untouched output with
``PBIR_ROLE_KIND_MISMATCH`` -- *"Column expression in Measure-only role 'Values' (use Measure or
Aggregation)"* -- on a ``pivotTable`` where **five of six** projections in the same role were
correctly wrapped and one was not. Same class as #108: pristine output that cannot clear the gate
our own agents are told to trust either forces a hand-fix every run or trains them to ignore it.

IT REPRODUCES HERE, and the root cause is one of the three the reporter guessed. Swept at 2.333.0,
``0135_aggregation_types`` -- a workbook explicitly about aggregation types -- emits a
``clusteredBarChart`` whose ``Y`` carries ``Orders[Sales]`` bare. Its source shelf carries three
Sales pills at once::

    [sum:Sales:qk]     derivation='Sum'         -> Aggregation   (correct)
    [attr:Sales:qk]    derivation='Attribute'
    [none:Sales:qk]    derivation='None'        -> bare Column   (the defect)

So it is the **disaggregated** pill. Confirmed by running the real validator against the emitted
report: 1 error, exit 1, same diagnostic code.

THE TABLE IS HARVESTED, NOT GUESSED, and that mattered. ``powerbi-report-author catalog describe``
distinguishes THREE role kinds -- ``Grouping``, ``Measure`` and **``GroupingOrMeasure``**. A
hand-written first attempt of mine assumed ``scatterChart``'s ``X``/``Y`` and ``multiRowCard``'s
``Values`` were measure-only; they are ``GroupingOrMeasure`` and absent respectively, so that table
would have rejected sound reports. Only ``Measure`` is enforced -- "cannot judge" must never become
"declare invalid".

AGREEMENT WITH THE AUTHORITY. Cross-checked against ``powerbi-report-author validate`` over all 34
corpus reports: 1 hit each, agreeing on **34 of 34**. Zero false positives, zero false negatives.
That is a stronger claim than any unit test here can make, because the validator is the authority
this rule exists to mirror.

WHAT IS NOT FIXED: the emitter still produces the bare Column. This rule makes it visible at build
time instead of only under an opt-in npm CLI. Deliberately not repaired in the same change -- the
faithful translation of a disaggregated pill is a rendering decision (Power BI cannot plot one mark
per underlying row in a clustered bar without the row grain), and that needs render verification
rather than a guess.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from pbir_lint import (  # noqa: E402
    MEASURE_ROLES, REQUIRED_ROLES, _lint_measure_role_kind, lint_pbir_parts,
)

_COL = {"Column": {"Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Sales"}}
_AGG = {"Aggregation": {"Expression": _COL["Column"], "Function": 0}}
_MEA = {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}}, "Property": "Total"}}


def _parts(vtype, role, field, path="definition/pages/p/visuals/v/visual.json"):
    return {path: json.dumps({"visual": {"visualType": vtype, "query": {"queryState": {
        role: {"projections": [{"field": field, "queryRef": "Orders.Sales"}]}}}}})}


def test_the_reproduced_defect_is_flagged():
    out = _lint_measure_role_kind(_parts("clusteredBarChart", "Y", _COL))
    assert len(out) == 1, out
    assert "PBIR_ROLE_KIND_MISMATCH" in out[0]
    assert "'Sales'" in out[0] or "Sales" in out[0]


def test_the_reported_pivottable_shape_is_flagged():
    out = _lint_measure_role_kind(_parts("pivotTable", "Values", _COL))
    assert len(out) == 1, out


def test_an_aggregation_or_a_measure_satisfies_the_role():
    assert _lint_measure_role_kind(_parts("clusteredBarChart", "Y", _AGG)) == []
    assert _lint_measure_role_kind(_parts("pivotTable", "Values", _MEA)) == []


def test_a_grouping_role_may_carry_a_column():
    """Rows/Category are grouping roles -- a bare Column is CORRECT there. Flagging them would
    reject every faithful chart in the corpus."""
    assert _lint_measure_role_kind(_parts("clusteredBarChart", "Category", _COL)) == []
    assert _lint_measure_role_kind(_parts("pivotTable", "Rows", _COL)) == []


def test_GroupingOrMeasure_roles_are_deliberately_not_judged():
    """The distinction a guessed table gets wrong. ``tableEx`` Values and ``scatterChart`` X/Y are
    GroupingOrMeasure in the catalog -- a bare Column there is 'Don't summarize', not a defect."""
    assert "tableEx" not in MEASURE_ROLES
    assert "Y" not in MEASURE_ROLES.get("scatterChart", ())
    assert "X" not in MEASURE_ROLES.get("scatterChart", ())
    assert _lint_measure_role_kind(_parts("tableEx", "Values", _COL)) == []
    assert _lint_measure_role_kind(_parts("scatterChart", "Y", _COL)) == []


def test_an_unknown_visual_type_is_not_judged():
    assert _lint_measure_role_kind(_parts("someFutureVisual", "Y", _COL)) == []


def test_the_table_agrees_with_the_required_roles_table_where_they_overlap():
    """Both are harvested from the same catalog, so a type present in one and contradicting the
    other means one of them was hand-edited."""
    for vtype, required in REQUIRED_ROLES.items():
        for role in required:
            if role in MEASURE_ROLES.get(vtype, ()):
                # a required MEASURE role: emitting it bare is exactly the defect
                assert _lint_measure_role_kind(_parts(vtype, role, _COL)), (vtype, role)


def test_a_visual_with_no_query_is_not_flagged():
    """An emptied visual is the deliberate placeholder a dropped-reference visual becomes."""
    p = {"definition/pages/p/visuals/v/visual.json":
         json.dumps({"visual": {"visualType": "clusteredBarChart"}})}
    assert _lint_measure_role_kind(p) == []


def test_the_rule_is_wired_into_the_aggregate_linter():
    """A rule nobody calls is a rule that does not exist. Pins reachability, not just behaviour."""
    assert lint_pbir_parts(_parts("clusteredBarChart", "Y", _COL))
    assert any("PBIR_ROLE_KIND_MISMATCH" in m
               for m in lint_pbir_parts(_parts("clusteredBarChart", "Y", _COL)))


def test_malformed_parts_are_survived():
    assert _lint_measure_role_kind({"definition/x/visual.json": "{not json"}) == []
    assert _lint_measure_role_kind({}) == []
