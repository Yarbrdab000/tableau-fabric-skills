"""A scatterChart with aggregated X/Y and no grouping cannot map (#173).

THE REPORT. A customer dashboard shipped a visual whose handover status was ``rebuilt`` -- success --
while Power BI Desktop showed a live ``DataViewMappingError_ScatterGroupingValues``. Their words:
*"a visual marked clean that's actually broken."* The per-visual status is what every downstream
consumer trusts, so a false green there silently defeats any gate keyed on it.

THE MECHANISM. Power BI's scatter dataViewMapping needs a grouping field to produce more than one
point. With ``X`` and ``Y`` both aggregated and no ``Category`` / ``Series`` / ``Play``, every row
collapses to a single aggregate and the mapping fails.

WHY NO EXISTING GATE COULD SEE IT. ``powerbi-report-author catalog describe scatterChart`` reports
``requiredRoles: ["X", "Y"]`` with ``Category`` merely **optional**. So R9 (required roles) cannot
fire, and the external validator PASSES the report: the visual is structurally valid and does not
render. This is the mirror of R10 -- there *validate could see and we were blind*; here *validate is
blind and we can see*.

IT DOES NOT REPRODUCE IN OUR CORPUS: 7 scatterCharts at 2.334.0, all 7 grouped. This is a guard
against a shape a customer hit, not a description of one we produce -- which is also why it is
conservative. "Optional" means a legitimate ungrouped scatter exists, so an unaggregated axis (a
bare Column, which is itself the grain) clears the rule.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from pbir_lint import _lint_scatter_grouping, lint_pbir_parts  # noqa: E402

_COL = {"Column": {"Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Sales"}}
_AGG = {"Aggregation": {"Expression": _COL["Column"], "Function": 0}}
_DIM = {"Column": {"Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Customer"}}


def _scatter(state, vtype="scatterChart"):
    return {"definition/pages/p/visuals/v/visual.json": json.dumps(
        {"visual": {"visualType": vtype, "query": {"queryState": state}}})}


def _proj(field):
    return {"projections": [{"field": field}]}


def test_the_reported_shape_is_flagged():
    out = _lint_scatter_grouping(_scatter({"X": _proj(_AGG), "Y": _proj(_AGG)}))
    assert len(out) == 1, out
    assert "DataViewMappingError_ScatterGroupingValues" in out[0]
    # It must say that validate PASSES this, or a reader assumes the external gate covers it.
    assert "validate PASSES" in out[0]


def test_any_grouping_role_clears_it():
    for role in ("Category", "Series", "Play"):
        state = {"X": _proj(_AGG), "Y": _proj(_AGG), role: _proj(_DIM)}
        assert _lint_scatter_grouping(_scatter(state)) == [], role


def test_an_unaggregated_axis_clears_it():
    """A bare Column axis IS the grain -- the ordinary way to draw a per-row scatter. Flagging it
    would reject the exact shape the rule tells people to use as a remedy."""
    assert _lint_scatter_grouping(_scatter({"X": _proj(_COL), "Y": _proj(_AGG)})) == []
    assert _lint_scatter_grouping(_scatter({"X": _proj(_AGG), "Y": _proj(_COL)})) == []
    assert _lint_scatter_grouping(_scatter({"X": _proj(_COL), "Y": _proj(_COL)})) == []


def test_a_missing_axis_is_R9s_business_not_ours():
    """A scatter with no Y is missing a REQUIRED role -- one defect reported twice teaches a reader
    that a hit might be either."""
    assert _lint_scatter_grouping(_scatter({"X": _proj(_AGG)})) == []
    assert _lint_scatter_grouping(_scatter({"Y": _proj(_AGG)})) == []


def test_other_visual_types_are_untouched():
    assert _lint_scatter_grouping(_scatter({"X": _proj(_AGG), "Y": _proj(_AGG)},
                                           vtype="clusteredBarChart")) == []


def test_an_emptied_placeholder_is_not_flagged():
    p = {"definition/pages/p/visuals/v/visual.json":
         json.dumps({"visual": {"visualType": "scatterChart"}})}
    assert _lint_scatter_grouping(p) == []
    assert _lint_scatter_grouping(_scatter({})) == []


def test_the_rule_is_wired_into_the_aggregate_linter():
    """A rule nobody calls is a rule that does not exist."""
    problems = lint_pbir_parts(_scatter({"X": _proj(_AGG), "Y": _proj(_AGG)}))
    assert any("DataViewMappingError_ScatterGroupingValues" in p for p in problems)


def test_malformed_parts_are_survived():
    assert _lint_scatter_grouping({"definition/x/visual.json": "{not json"}) == []
    assert _lint_scatter_grouping({}) == []
