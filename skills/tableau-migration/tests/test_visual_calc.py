"""Tests for the view-only quick-table-calc -> Power BI Visual-Calculation path (additive).

This exercises the *logic* of the new path end to end, not a lookup of the 21 corpus examples:

* :mod:`visual_calc_spec` -- normalize a recovered ``TableCalcUsage`` into a ``VisualCalcSpec``
  (family + axis + reset + offset + scope + chain), including the axis-from-the-view rule that is
  the whole point ("computed Down" flips the axis) and the fail-closed review reasons.
* :mod:`visual_calc_emitter` -- render a spec into exact Visual-Calculation DAX, with the
  false-friend arity rules (``PREVIOUS(x, axis)`` vs ``PREVIOUS(x, k, axis)``; ``ROWNUMBER`` for a
  running position) and the role-driven visibility + two-pass chain.
* the wiring seam in :mod:`twb_to_pbir` (``_view_only_quick_index`` / ``_apply_visual_calcs``) that
  projects the calc into ``visual.json``.
* the additive report rollup in :mod:`migrate_estate` (``_visual_calc_rollup``).
* a raw ``.twb`` XML -> extractor -> spec -> emitter chain, proving the facts flow through parsing.

All fixtures are hand-authored synthetic Tableau XML / dataclass instances -- no raw workbook,
extract, or ``.pbix`` is committed (secret discipline).
"""
import pytest

import json

from workbook_table_calcs import Pill, TableCalcUsage, extract_table_calc_usages
import visual_calc_spec as vcs
from visual_calc_spec import usage_to_visual_calc_spec
from visual_calc_emitter import emit_visual_calc
from twb_to_pbir import _apply_visual_calcs, _view_only_quick_index
from twb_to_pbir import emit_pbir, parse_twb
from migrate_estate import _visual_calc_rollup, _color_scale_rollup, _measure_filter_rollup


# -- fact factories ------------------------------------------------------------
def _pill(column, derivation="None"):
    return Pill(instance=f"{derivation.lower()}:{column}:x", column=column, derivation=derivation)


# The canonical corpus layout: a non-temporal dimension down the ROWS shelf, a date grain across
# the COLUMNS shelf. The ordered families therefore compute across -> axis COLUMNS.
_SEGMENT = _pill("Segment", "None")
_YEAR = _pill("Order Date", "Year")
_QUARTER = _pill("Order Date", "Quarter")


def _usage(**kw):
    base = dict(
        worksheet="WS", instance="cum:cnt:Order ID:qk", column="Order ID",
        caption="Order ID", kind="quick", calc_type="CumTotal",
        rows=[_SEGMENT], cols=[_YEAR], ordering_type="Rows",
    )
    base.update(kw)
    return TableCalcUsage(**base)


def _spec(**kw):
    spec, reason = usage_to_visual_calc_spec(_usage(**kw))
    assert reason is None, reason
    assert spec is not None
    return spec


# -- 1. spec normalizer: one family per row (derived, not looked up) -----------
def test_running_total_no_reset():
    s = _spec(calc_type="CumTotal", level_break=None)
    assert s.family == vcs.FAMILY_RUNNING_TOTAL
    assert s.axis == "COLUMNS"
    assert s.reset is None


def test_ytd_has_highestparent_reset():
    s = _spec(calc_type="CumTotal", level_break="[ds].[yr:Order Date:ok]")
    assert s.family == vcs.FAMILY_YTD
    assert s.reset == "HIGHESTPARENT"


def test_moving_average_window_size_from_bounds():
    s = _spec(calc_type="WindowTotal", aggregation="Avg", window_from=-2, window_to=0)
    assert s.family == vcs.FAMILY_MOVING_AVERAGE
    assert s.window_size == 3           # -2..0 inclusive is a 3-period window
    assert s.window_agg == "Avg"


def test_percentile_partitions_by_outermost_temporal_on_axis():
    s = _spec(calc_type="PctRank", ordering_type="Pane")
    assert s.family == vcs.FAMILY_PERCENTILE
    assert s.partition_pill == "Order Date"
    assert s.partition_grain == "Year"


def test_rank_table_scope_maps_to_family_rank_with_no_partition():
    # A Table-scoped rank (the common CustomerRank shape) ranks the WHOLE visual -> no PARTITIONBY.
    s = _spec(calc_type="Rank")
    assert s.family == vcs.FAMILY_RANK
    assert s.rank_ties == "SKIP"            # Tableau "Competition" default -> DAX SKIP
    assert s.rank_direction == "DESC"       # largest value = rank 1
    assert s.partition_pill is None


def test_rank_options_dense_ascending_parsed():
    s = _spec(calc_type="Rank", rank_options="Dense,Ascending")
    assert s.rank_ties == "DENSE"
    assert s.rank_direction == "ASC"


def test_rank_pane_scope_partitions_by_outer_temporal_grain():
    s = _spec(calc_type="Rank", ordering_type="Pane")
    assert s.family == vcs.FAMILY_RANK
    assert s.partition_pill == "Order Date"
    assert s.partition_grain == "Year"


def test_rank_modified_tie_mode_routes_to_review():
    # Tableau "Modified" (1,3,3,4) has no faithful native RANK equivalent -> review, never a wrong rule.
    spec, reason = usage_to_visual_calc_spec(_usage(calc_type="Rank", rank_options="Modified,Descending"))
    assert spec is None
    assert "Modified" in reason


def test_rank_unique_tie_mode_routes_to_review():
    spec, reason = usage_to_visual_calc_spec(_usage(calc_type="Rank", rank_options="Unique,Descending"))
    assert spec is None
    assert "Unique" in reason


def test_rank_pane_without_temporal_grain_is_reviewed():
    spec, reason = usage_to_visual_calc_spec(
        _usage(calc_type="Rank", ordering_type="Pane", rows=[], cols=[_pill("Region")]))
    assert spec is None
    assert "pane partition" in reason


def test_compound_growth_from_compounded_diff():
    s = _spec(calc_type="PctDiff", diff_options="Relative,Compounded")
    assert s.family == vcs.FAMILY_COMPOUND_GROWTH


def test_percent_difference_leaf_offset_one():
    s = _spec(calc_type="PctDiff", diff_options="Relative")
    assert s.family == vcs.FAMILY_PERCENT_DIFFERENCE
    assert s.offset_k == 1


def test_percent_of_total_collapse_scope_both_shelves():
    s = _spec(calc_type="PctTotal", ordering_type="Table")
    assert s.family == vcs.FAMILY_PERCENT_OF_TOTAL
    assert s.collapse_scope == "ROWS COLUMNS"


def test_year_over_year_calendar_ratio_year_over_quarter_is_four():
    s = _spec(calc_type="PctDiff", diff_options="Relative",
              level_address="[ds].[yr:Order Date:ok]", cols=[_QUARTER])
    assert s.family == vcs.FAMILY_YEAR_OVER_YEAR
    assert s.offset_k == 4              # one year = 4 quarters back on a quarter leaf


def test_difference_leaf_offset_one():
    s = _spec(calc_type="Difference")
    assert s.family == vcs.FAMILY_DIFFERENCE
    assert s.offset_k == 1


def test_ytd_growth_is_a_chain_with_inner_ytd():
    s = _spec(calc_type="CumTotal", secondary=True,
              secondary_pass={"calc_type": "PctDiff",
                              "level_address": "[ds].[yr:Order Date:ok]"},
              cols=[_QUARTER])
    assert s.family == vcs.FAMILY_YTD_GROWTH
    assert s.offset_k == 4
    assert s.chain_inner is not None
    assert s.chain_inner.family == vcs.FAMILY_YTD
    assert s.chain_inner.reset == "HIGHESTPARENT"


# -- 2. axis-from-the-view logic (NOT the raw ordering token) ------------------
def test_axis_rows_token_computes_across_columns():
    assert vcs._derive_axis(_usage(ordering_type="Rows"), vcs._is_temporal_pill)[0] == "COLUMNS"


def test_axis_columns_token_computes_down_rows():
    # The corpus' "computed Down" twin: token is Columns yet it must run vertically -> axis ROWS.
    assert vcs._derive_axis(_usage(ordering_type="Columns"), vcs._is_temporal_pill)[0] == "ROWS"


def test_axis_follows_explicit_ordering_field_on_columns():
    u = _usage(ordering_type="Field", ordering_fields=["Order Date"])
    assert vcs._derive_axis(u, vcs._is_temporal_pill)[0] == "COLUMNS"


def test_axis_follows_unique_temporal_shelf_when_on_rows():
    u = _usage(ordering_type="Table", rows=[_YEAR], cols=[_SEGMENT])
    assert vcs._derive_axis(u, vcs._is_temporal_pill)[0] == "ROWS"


def test_axis_unrecoverable_when_both_shelves_temporal_and_no_ordering_field():
    u = _usage(ordering_type="Table", rows=[_pill("Ship Date", "Year")], cols=[_YEAR])
    axis, reason = vcs._derive_axis(u, vcs._is_temporal_pill)
    assert axis is None
    assert "not recoverable" in reason


# -- 3. emitter: exact Visual-Calculation DAX (tell-tale strings) --------------
def _emit(spec, **kw):
    defs, reason = emit_visual_calc(spec, base_measure="Count Orders", **kw)
    assert reason is None, reason
    assert defs
    return defs


def test_emit_running_total_dax():
    (d,) = _emit(_spec(calc_type="CumTotal", level_break=None))
    assert d.expression == "RUNNINGSUM([Count Orders], COLUMNS)"
    assert d.hidden is False            # value role -> shown


def test_emit_ytd_dax_carries_reset():
    (d,) = _emit(_spec(calc_type="CumTotal", level_break="[ds].[yr:Order Date:ok]"))
    assert d.expression == "RUNNINGSUM([Count Orders], COLUMNS, HIGHESTPARENT)"


def test_emit_moving_average_dax():
    (d,) = _emit(_spec(calc_type="WindowTotal", aggregation="Avg",
                       window_from=-2, window_to=0))
    assert d.expression == "MOVINGAVERAGE([Count Orders], 3, TRUE, COLUMNS)"


def test_emit_percentile_dax_uses_rank_pair_and_partition():
    (d,) = _emit(_spec(calc_type="PctRank", ordering_type="Pane"),
                 partition_column="Calendar Year")
    assert "PARTITIONBY([Calendar Year])" in d.expression
    assert "RANK(DENSE, ORDERBY([Count Orders], ASC)" in d.expression
    assert "DIVIDE(RankAsc - 1, N - 1)" in d.expression


def test_emit_rank_table_scope_has_no_partition():
    (d,) = _emit(_spec(calc_type="Rank"))
    assert d.expression == "RANK(SKIP, ORDERBY([Count Orders], DESC))"
    assert d.hidden is False            # value role -> shown
    assert d.number_format is None      # rank is an integer, not a percent family


def test_emit_rank_dense_ascending():
    (d,) = _emit(_spec(calc_type="Rank", rank_options="Dense,Ascending"))
    assert d.expression == "RANK(DENSE, ORDERBY([Count Orders], ASC))"


def test_emit_rank_pane_scope_partitions_by_resolved_column():
    (d,) = _emit(_spec(calc_type="Rank", ordering_type="Pane"),
                 partition_column="Calendar Year")
    assert d.expression == "RANK(SKIP, ORDERBY([Count Orders], DESC), PARTITIONBY([Calendar Year]))"


def test_emit_compound_growth_dax_uses_first_and_rownumber():
    (d,) = _emit(_spec(calc_type="PctDiff", diff_options="Relative,Compounded"))
    assert "FIRST([Count Orders], COLUMNS)" in d.expression
    assert "ROWNUMBER(COLUMNS) - 1" in d.expression
    assert "POWER(DIVIDE([Count Orders], FirstVal), Exponent) - 1" in d.expression


def test_emit_percent_difference_dax():
    (d,) = _emit(_spec(calc_type="PctDiff", diff_options="Relative"))
    assert d.expression == ("DIVIDE([Count Orders] - PREVIOUS([Count Orders], COLUMNS), "
                            "PREVIOUS([Count Orders], COLUMNS))")


def test_emit_percent_of_total_dax_uses_collapseall():
    (d,) = _emit(_spec(calc_type="PctTotal", ordering_type="Table"))
    assert d.expression == "DIVIDE([Count Orders], COLLAPSEALL([Count Orders], ROWS COLUMNS))"


def test_emit_year_over_year_dax_offset_four():
    (d,) = _emit(_spec(calc_type="PctDiff", diff_options="Relative",
                       level_address="[ds].[yr:Order Date:ok]", cols=[_QUARTER]))
    assert d.expression == ("VAR PriorYear = PREVIOUS([Count Orders], 4, COLUMNS)\n"
                            "RETURN DIVIDE([Count Orders] - PriorYear, ABS(PriorYear))")


def test_emit_difference_dax():
    (d,) = _emit(_spec(calc_type="Difference"))
    assert d.expression == "[Count Orders] - PREVIOUS([Count Orders], COLUMNS)"


def test_emit_ytd_growth_chain_inner_then_outer():
    inner, outer = _emit(_spec(
        calc_type="CumTotal", secondary=True,
        secondary_pass={"calc_type": "PctDiff", "level_address": "[ds].[yr:Order Date:ok]"},
        cols=[_QUARTER]))
    # inner YTD is always hidden and referenced by name from the outer growth calc.
    assert inner.name == "YTD"
    assert inner.hidden is True
    assert inner.is_inner is True
    assert inner.expression == "RUNNINGSUM([Count Orders], COLUMNS, HIGHESTPARENT)"
    assert outer.name == "YTD Growth"
    assert outer.expression == ("VAR PriorYear = PREVIOUS([YTD], 4, COLUMNS)\n"
                                "RETURN DIVIDE([YTD] - PriorYear, ABS(PriorYear))")


# -- 4. false-friend + role rules ---------------------------------------------
def test_previous_arity_single_step_omits_the_offset():
    # PREVIOUS(x, axis) for one step -- the axis must never be mistaken for a numeric offset.
    (d,) = _emit(_spec(calc_type="Difference"))
    assert "PREVIOUS([Count Orders], COLUMNS)" in d.expression
    assert "PREVIOUS([Count Orders], 1," not in d.expression


def test_previous_arity_multi_step_includes_the_offset():
    (d,) = _emit(_spec(calc_type="PctDiff", diff_options="Relative",
                       level_address="[ds].[yr:Order Date:ok]", cols=[_QUARTER]))
    assert "PREVIOUS([Count Orders], 4, COLUMNS)" in d.expression


def test_color_role_hides_the_calc():
    spec, _ = usage_to_visual_calc_spec(_usage(calc_type="CumTotal", level_break=None),
                                        role="color")
    defs, _ = emit_visual_calc(spec, base_measure="Count Orders")
    d = defs[0]
    assert d.role == "color"
    assert d.hidden is True             # conditionally-formatted table: calc drives fill, is hidden


def test_value_role_shows_the_calc():
    spec, _ = usage_to_visual_calc_spec(_usage(calc_type="CumTotal", level_break=None),
                                        role="value")
    defs, _ = emit_visual_calc(spec, base_measure="Count Orders")
    assert defs[0].hidden is False


def test_base_measure_name_flows_into_the_dax():
    defs, _ = emit_visual_calc(_spec(calc_type="CumTotal", level_break=None),
                               base_measure="Count Rows")
    assert defs[0].expression == "RUNNINGSUM([Count Rows], COLUMNS)"


# -- 5. off-substrate refusals route to review (never a guess) ----------------
def test_model_field_calc_is_not_the_visual_calc_path():
    spec, reason = usage_to_visual_calc_spec(_usage(kind="field", calc_type=None))
    assert spec is None
    assert "not a quick table calc" in reason


def test_windowed_non_average_is_refused():
    spec, reason = usage_to_visual_calc_spec(_usage(calc_type="WindowTotal", aggregation="Sum"))
    assert spec is None
    assert "moving average" in reason


def test_unmapped_calc_type_is_refused():
    spec, reason = usage_to_visual_calc_spec(_usage(calc_type="Frobnicate"))
    assert spec is None
    assert "no view-layer visual-calculation mapping" in reason


def test_unrecognized_secondary_chain_is_refused():
    spec, reason = usage_to_visual_calc_spec(_usage(
        calc_type="CumTotal", secondary=True,
        secondary_pass={"calc_type": "Rank"}))
    assert spec is None
    assert "secondary" in reason


def test_non_constant_calendar_ratio_is_refused():
    # A year-grain address over a week leaf is not a fixed number of periods -> review, not a wrong k.
    spec, reason = usage_to_visual_calc_spec(_usage(
        calc_type="PctDiff", diff_options="Relative",
        level_address="[ds].[yr:Order Date:ok]", cols=[_pill("Order Date", "Week")]))
    assert spec is None
    assert "calendar ratio" in reason or "calendar grains" in reason


def test_percentile_without_partition_column_is_refused_by_emitter():
    defs, reason = emit_visual_calc(_spec(calc_type="PctRank", ordering_type="Pane"),
                                    base_measure="Count Orders", partition_column=None)
    assert defs is None
    assert "partition column" in reason


def test_rank_pane_without_partition_column_is_refused_by_emitter():
    defs, reason = emit_visual_calc(_spec(calc_type="Rank", ordering_type="Pane"),
                                    base_measure="Count Orders", partition_column=None)
    assert defs is None
    assert "partition column" in reason


def test_percent_of_total_falls_back_to_row_axis_without_compute_using():
    # No explicit "compute using" (a Rows/Columns token) + no residual shelf -> Tableau's Table (Down)
    # default: collapse the row axis to the grand total, rather than route the whole visual to review.
    s = _spec(calc_type="PctTotal", ordering_type="Rows", rows=[], cols=[])
    assert s.family == vcs.FAMILY_PERCENT_OF_TOTAL
    assert s.collapse_scope == "ROWS"


def test_percent_of_total_with_unmapped_explicit_compute_using_stays_review():
    # An explicit "compute using" field that is not on the visual's shelves is genuinely unpinnable ->
    # keep routing to review (faithful-or-stub); never guess a direction for explicit addressing.
    spec, reason = usage_to_visual_calc_spec(_usage(
        calc_type="PctTotal", ordering_type="Field", ordering_fields=["Ship Mode"],
        rows=[_SEGMENT], cols=[_YEAR]))
    assert spec is None
    assert "collapse over" in reason


# -- 6. provenance is preserved (mirrors TableauFormula / TranslatedBy) --------
def test_spec_preserves_tableau_provenance():
    s = _spec(calc_type="CumTotal", level_break="[ds].[yr:Order Date:ok]")
    assert s.tableau_calc_type == "CumTotal"
    assert s.source_worksheet == "WS"
    assert "Quick Table Calc CumTotal" in s.tableau_summary
    assert "restart=level-break" in s.tableau_summary


# -- 7. raw .twb XML -> extractor -> spec -> emitter (the whole chain) ---------
_TWB = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <worksheets>
    <worksheet name='Running Total'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column aggregation='Count' datatype='integer' name='[Order ID]' role='measure' type='quantitative' caption='Order ID' />
            <column datatype='date' name='[Order Date]' role='dimension' type='ordinal' />
            <column datatype='string' name='[Segment]' role='dimension' type='nominal' />
            <column-instance column='[Order ID]' derivation='Count' name='[cum:cnt:Order ID:qk]' pivot='key' type='quantitative'>
              <table-calc ordering-type='Rows' type='CumTotal' />
            </column-instance>
            <column-instance column='[Order Date]' derivation='Year' name='[yr:Order Date:ok]' pivot='key' type='ordinal' />
            <column-instance column='[Segment]' derivation='None' name='[none:Segment:nk]' pivot='key' type='nominal' />
          </datasource-dependencies>
        </view>
        <rows>[ds0].[none:Segment:nk]</rows>
        <cols>[ds0].[yr:Order Date:ok]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>"""


def test_extractor_to_emitter_chain_running_total():
    usages = [u for u in extract_table_calc_usages(_TWB) if u.kind == "quick"]
    assert len(usages) == 1
    usage = usages[0]
    assert usage.calc_type == "CumTotal"
    assert usage.worksheet == "Running Total"
    spec, reason = usage_to_visual_calc_spec(usage, role="value")
    assert reason is None
    assert spec.family == vcs.FAMILY_RUNNING_TOTAL
    assert spec.axis == "COLUMNS"       # Segment on rows, Year on cols -> across -> COLUMNS
    defs, reason = emit_visual_calc(spec, base_measure="Count Orders")
    assert reason is None
    assert defs[0].expression == "RUNNINGSUM([Count Orders], COLUMNS)"


# -- 8. wiring seam in twb_to_pbir --------------------------------------------
def test_view_only_quick_index_groups_quick_only_by_worksheet():
    quick_a = _usage(worksheet="A", kind="quick")
    quick_b = _usage(worksheet="B", kind="quick")
    field = _usage(worksheet="A", kind="field", calc_type=None)
    index = _view_only_quick_index([quick_a, field, quick_b])
    assert set(index) == {"A", "B"}          # field usage is not a visual-calc candidate
    assert index["A"] == [quick_a]
    assert index["B"] == [quick_b]


def test_view_only_quick_index_empty_when_none():
    assert _view_only_quick_index(None) == {}


def _base_measure_field():
    return {"entity": "_Measures", "property": "Count Orders", "binding": "measure",
            "aggregation": None, "caption": "Count Orders", "kind": "value"}


def _matrix_state():
    base_proj = {"field": {"Measure": {"Property": "Count Orders"}},
                 "queryRef": "_Measures.Count Orders", "nativeQueryRef": "Count Orders"}
    state = {"Values": {"projections": [base_proj]},
             "Columns": {"projections": [{"nativeQueryRef": "Calendar Year"}]},
             "Rows": {"projections": [{"nativeQueryRef": "Segment"}]}}
    return state, base_proj


def test_apply_visual_calcs_value_role_projects_and_hides_base():
    base = _base_measure_field()
    ws = {"name": "WS", "encodings": {"color": None, "label": base, "text": None}}
    state, base_proj = _matrix_state()
    warnings = []
    value_objects, fact = _apply_visual_calcs(
        ws, state, {"WS": [_usage(calc_type="CumTotal", level_break=None)]},
        None, None, warnings)

    assert value_objects is None                 # value role, no colour scale -> no backColor
    assert fact["status"] == "emitted"
    assert fact["family"] == vcs.FAMILY_RUNNING_TOTAL
    assert fact["axis"] == "COLUMNS"
    projections = state["Values"]["projections"]
    assert len(projections) == 2                 # base + the appended Visual Calculation
    vc = projections[1]["field"]["NativeVisualCalculation"]
    assert vc["Expression"] == "RUNNINGSUM([Count Orders], COLUMNS)"
    assert vc["Name"] == "Running Total"
    assert projections[1]["queryRef"] == "select"
    assert base_proj["hidden"] is True           # plain table hides the base; the calc is shown
    assert "hidden" not in projections[1]         # the value calc itself is visible


def test_apply_visual_calcs_color_role_keeps_base_and_drives_backcolor():
    base = _base_measure_field()
    ws = {"name": "WS", "encodings": {"color": base, "label": None, "text": None},
          "color_gradient": {"colors": ["#FFFFFF", "#FF0000"]}}
    state, base_proj = _matrix_state()
    warnings = []
    value_objects, fact = _apply_visual_calcs(
        ws, state, {"WS": [_usage(calc_type="CumTotal", level_break=None)]},
        None, None, warnings)

    assert fact["status"] == "emitted"
    assert fact["role"] == "color"
    assert "hidden" not in base_proj             # conditionally-formatted table keeps the base shown
    assert state["Values"]["projections"][1]["hidden"] is True   # the calc is hidden, drives fill
    fill_input = (value_objects[0]["properties"]["backColor"]["solid"]["color"]["expr"]
                  ["FillRule"]["Input"]["SelectRef"]["ExpressionName"])
    assert fill_input == "select"                # coloured by the (hidden) calc's queryRef
    assert fact["backColor"]["driver"] == "select"
    assert fact["backColor"]["target"] == "_Measures.Count Orders"


def test_apply_visual_calcs_value_role_with_gradient_drives_backcolor():
    # A value-role table that also carries a continuous colour scale tints its shown calc column: the
    # base stays hidden and the (visible) calc drives the backColor FillRule -- mirroring the oracle,
    # which carries the identical backColor block for the value role (only visibility differs).
    base = _base_measure_field()
    ws = {"name": "WS", "encodings": {"color": None, "label": base, "text": None},
          "color_gradient": {"colors": ["#FFFFFF", "#FF7F00"]}}
    state, base_proj = _matrix_state()
    warnings = []
    value_objects, fact = _apply_visual_calcs(
        ws, state, {"WS": [_usage(calc_type="CumTotal", level_break=None)]},
        None, None, warnings)

    assert fact["status"] == "emitted"
    assert fact["role"] == "value"
    assert base_proj["hidden"] is True            # value role hides the base ...
    assert "hidden" not in state["Values"]["projections"][1]   # ... and shows the calc
    fr = value_objects[0]["properties"]["backColor"]["solid"]["color"]["expr"]["FillRule"]
    assert fr["Input"]["SelectRef"]["ExpressionName"] == "select"   # driven by the shown calc
    # value role paints the VISIBLE calc column (not the hidden base) so the heat scale renders
    assert value_objects[0]["selector"]["metadata"] == "select"
    assert fact["backColor"] == {"driver": "select", "target": "select", "emitted": True}


def test_apply_visual_calcs_review_leaves_base_only_visual_untouched():
    base = _base_measure_field()
    ws = {"name": "WS", "encodings": {"color": None, "label": base, "text": None}}
    state, _ = _matrix_state()
    warnings = []
    value_objects, fact = _apply_visual_calcs(
        ws, state, {"WS": [_usage(calc_type="Frobnicate")]},   # unmapped -> review
        None, None, warnings)

    assert value_objects is None
    assert fact["status"] == "review"
    assert "reason" in fact
    assert len(state["Values"]["projections"]) == 1             # base untouched, nothing appended
    assert warnings                                             # a route-to-review warning was raised


def test_apply_visual_calcs_yields_to_model_measure_path():
    base = _base_measure_field()
    base["measure_rebound"] = True                              # the model path already bound it
    ws = {"name": "WS", "encodings": {"color": None, "label": base, "text": None}}
    state, _ = _matrix_state()
    value_objects, fact = _apply_visual_calcs(
        ws, state, {"WS": [_usage(calc_type="CumTotal", level_break=None)]},
        None, None, [])

    assert (value_objects, fact) == (None, None)                # precedence: never double-emit
    assert len(state["Values"]["projections"]) == 1


def test_apply_visual_calcs_noop_when_no_quick_calc_for_worksheet():
    base = _base_measure_field()
    ws = {"name": "WS", "encodings": {"color": None, "label": base, "text": None}}
    state, _ = _matrix_state()
    assert _apply_visual_calcs(ws, state, {}, None, None, []) == (None, None)


# -- 9. additive report rollup in migrate_estate ------------------------------
def _record(**vc):
    return {"visual_calc": vc}


def test_visual_calc_rollup_counts_by_status_role_family_and_chain():
    result = {"candidate_records": [
        _record(status="emitted", role="value", family="RUNNING_TOTAL", axis="COLUMNS",
                worksheet="A", visual_calcs=[{"is_inner": False}]),
        _record(status="emitted", role="color", family="RUNNING_TOTAL", axis="COLUMNS",
                worksheet="B", visual_calcs=[{"is_inner": False}]),
        _record(status="emitted", role="value", family="YTD_GROWTH", axis="COLUMNS",
                worksheet="C", visual_calcs=[{"is_inner": True}, {"is_inner": False}]),
        _record(status="review", role="value", family=None, reason="axis unrecoverable",
                worksheet="D"),
        {"no_visual_calc_here": True},         # non-VC records are ignored
    ]}
    roll = _visual_calc_rollup(result)
    assert roll["emitted_total"] == 3
    assert roll["review_total"] == 1
    assert roll["by_role"] == {"value": 2, "color": 1}
    assert roll["chained"] == 1                 # only the YTD_GROWTH record has an inner calc
    assert roll["families"] == {"RUNNING_TOTAL": 2, "YTD_GROWTH": 1}
    assert len(roll["worksheets"]) == 4         # all VC facts, emitted + review


def test_visual_calc_rollup_none_when_no_facts():
    assert _visual_calc_rollup({"candidate_records": [{"other": 1}]}) is None
    assert _visual_calc_rollup({}) is None


# -- default-palette disclosure rollup -----------------------------------------
def test_color_scale_rollup_lists_default_palette_worksheets():
    # A conditional-format fill and a visual-calculation fill each rode Tableau's default palette
    # (default_palette=True); an explicit-palette fill and a non-dict record do NOT contribute.
    result = {"candidate_records": [
        {"worksheet": "ProductView",
         "conditional_format": {"kind": "background_color_scale", "default_palette": True}},
        {"worksheet": "HeatVC",
         "visual_calc": {"kind": "visual_calculation", "default_palette": True}},
        {"worksheet": "ExplicitPalette",
         "conditional_format": {"kind": "background_color_scale"}},   # no default_palette -> skipped
        {"no_facts_here": True},                                       # ignored
        "not-a-dict",                                                  # ignored
    ]}
    roll = _color_scale_rollup(result)
    assert roll is not None
    assert roll["count"] == 2
    assert roll["worksheets"] == ["ProductView", "HeatVC"]
    assert "default continuous palette" in roll["note"]
    assert "verify the colours against the source" in roll["note"]


def test_color_scale_rollup_dedupes_worksheet_with_both_facts():
    # A single worksheet that carries BOTH a cf and a vc default-palette fact is listed once.
    result = {"candidate_records": [
        {"worksheet": "ProductView",
         "conditional_format": {"default_palette": True},
         "visual_calc": {"default_palette": True}},
    ]}
    roll = _color_scale_rollup(result)
    assert roll["count"] == 1
    assert roll["worksheets"] == ["ProductView"]


def test_color_scale_rollup_none_when_no_default_palette():
    # Facts present but none flagged default_palette -> None (report byte-identical).
    assert _color_scale_rollup({"candidate_records": [
        {"worksheet": "A", "conditional_format": {"kind": "background_color_scale"}},
        {"worksheet": "B", "visual_calc": {"kind": "visual_calculation"}},
    ]}) is None
    assert _color_scale_rollup({"candidate_records": [{"other": 1}]}) is None
    assert _color_scale_rollup({}) is None
    assert _color_scale_rollup(None) is None


def test_measure_filter_rollup_lists_dropped_aggregate_filters():
    # Two worksheets each dropped an aggregate/measure filter to review; a non-matching warning and a
    # non-dict entry do NOT contribute. Mirrors twb_to_pbir._parse_filters' exact reason string.
    result = {"warnings": [
        {"scope": "worksheet", "name": "Sales by Region",
         "reason": "aggregate/measure filter on 'SUM(Sales)' is not mapped to a slicer "
                   "(filter scope requires manual attention)"},
        {"scope": "worksheet", "name": "Profit Detail",
         "reason": "aggregate/measure filter on 'Profit Ratio' is not mapped to a slicer "
                   "(filter scope requires manual attention)"},
        {"scope": "worksheet", "name": "Sales by Region",
         "reason": "Day-Trunc grain not applied"},   # unrelated -> ignored
        "not-a-dict",                                 # ignored
    ]}
    roll = _measure_filter_rollup(result)
    assert roll is not None
    assert roll["count"] == 2
    assert [w["worksheet"] for w in roll["worksheets"]] == ["Sales by Region", "Profit Detail"]
    assert "SUM(Sales)" in roll["worksheets"][0]["reason"]
    assert "re-apply it as a visual-level filter" in roll["note"]


def test_measure_filter_rollup_dedupes_identical_warning():
    # The same (worksheet, reason) emitted twice is listed once.
    reason = ("aggregate/measure filter on 'SUM(Sales)' is not mapped to a slicer "
              "(filter scope requires manual attention)")
    result = {"warnings": [
        {"scope": "worksheet", "name": "Sheet 1", "reason": reason},
        {"scope": "worksheet", "name": "Sheet 1", "reason": reason},
    ]}
    roll = _measure_filter_rollup(result)
    assert roll["count"] == 1
    assert roll["worksheets"] == [{"worksheet": "Sheet 1", "reason": reason}]


def test_measure_filter_rollup_none_when_no_dropped_filters():
    # No matching warning -> None (report byte-identical); resolved flag filters never reach here
    # because twb_to_pbir._drop_resolved_flag_warnings removes them before the result is returned.
    assert _measure_filter_rollup({"warnings": [
        {"scope": "worksheet", "name": "A", "reason": "Day-Trunc grain not applied"},
    ]}) is None
    assert _measure_filter_rollup({"warnings": []}) is None
    assert _measure_filter_rollup({}) is None
    assert _measure_filter_rollup(None) is None


# -- 10. full parse_twb -> emit_pbir integration (VC lands in visual.json) -----
_MATRIX_DS = """
  <datasources>
    <datasource caption='S' inline='true' name='federated.abc' version='18.1'>
      <connection class='federated'>
        <relation name='Orders' table='[dbo].[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'><remote-name>Segment</remote-name><local-name>[Segment]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Order Date</remote-name><local-name>[Order Date]</local-name><parent-name>[Orders]</parent-name><local-type>datetime</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Sales</remote-name><local-name>[Sales]</local-name><parent-name>[Orders]</parent-name><local-type>real</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>
  </datasources>"""

_MATRIX_DEPS = """
            <column caption='Segment' datatype='string' name='[Segment]' role='dimension' type='nominal' />
            <column caption='Order Date' datatype='datetime' name='[Order Date]' role='dimension' type='ordinal' />
            <column caption='Sales' datatype='real' name='[Sales]' role='measure' type='quantitative' />
            <column-instance column='[Segment]' derivation='None' name='[none:Segment:nk]' pivot='key' type='nominal' />
            <column-instance column='[Order Date]' derivation='Year' name='[yr:Order Date:ok]' pivot='key' type='ordinal' />
            <column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' pivot='key' type='quantitative' />"""


def _matrix_workbook(ws_name, encodings):
    ws = f"""
    <worksheet name='{ws_name}'>
      <table>
        <view>
          <datasources><datasource caption='S' name='federated.abc' /></datasources>
          <datasource-dependencies datasource='federated.abc'>{_MATRIX_DEPS}</datasource-dependencies>
        </view>
        <panes><pane><mark class='Text' />{encodings}</pane></panes>
        <rows>[federated.abc].[none:Segment:nk]</rows>
        <cols>[federated.abc].[yr:Order Date:ok]</cols>
      </table>
    </worksheet>"""
    return ("<?xml version='1.0' encoding='utf-8' ?>\n<workbook>" + _MATRIX_DS
            + "<worksheets>" + ws + "</worksheets></workbook>")


def _values_projections(parts):
    for k, v in parts.items():
        if k.endswith("visual.json"):
            qs = json.loads(v)["visual"]["query"]["queryState"]
            return qs.get("Values", {}).get("projections", [])
    return []


def test_emit_pbir_projects_visual_calculation_for_a_quick_calc_worksheet():
    # The quick-calc token does not survive onto the resolved value pill; the wiring correlates the
    # recovered usage to the worksheet by NAME. So a plain aggregate base + a quick usage for the same
    # worksheet is exactly the real pipeline's shape.
    wb = _matrix_workbook("Running Total",
                          "<encodings><text column='[federated.abc].[sum:Sales:qk]' /></encodings>")
    ir = parse_twb(wb)
    assert ir["worksheets"][0]["visual_type"] == "matrix"
    usage = _usage(worksheet="Running Total", calc_type="CumTotal", level_break=None,
                   rows=[_pill("Segment", "None")], cols=[_pill("Order Date", "Year")])

    parts = emit_pbir(ir, table_calc_usages=[usage])
    projections = _values_projections(parts)
    assert len(projections) == 2

    base, vc = projections[0], projections[1]
    assert base["hidden"] is True                              # plain table hides the base value
    native = vc["field"]["NativeVisualCalculation"]
    assert native["Name"] == "Running Total"
    assert native["Expression"] == "RUNNINGSUM([Sum of Sales], COLUMNS)"
    assert native["Language"] == "dax"
    assert "hidden" not in vc                                  # the value calc is shown


def test_emit_pbir_without_usages_is_byte_identical_no_visual_calc():
    # The additive path must be a no-op for every existing caller (which passes no usages).
    wb = _matrix_workbook("Plain",
                          "<encodings><text column='[federated.abc].[sum:Sales:qk]' /></encodings>")
    ir = parse_twb(wb)
    projections = _values_projections(emit_pbir(ir))
    assert len(projections) == 1
    assert "NativeVisualCalculation" not in projections[0]["field"]
    assert "hidden" not in projections[0]


# -- 11. cartesian chart path (base on the Y axis, axis ROWS, category re-nest) -
# A chart carries its measure on Y (not the matrix Values shelf) and its dimensions on a single
# Category axis, so a Visual Calculation runs along ROWS (chart geometry) and is appended to Y. These
# mirror the hand-built oracle for the two live-feedback charts (a line moving average, a bar percent
# of total) -- derived from the Tableau facts, not looked up.
def _chart_base_field():
    return {"entity": "Orders", "property": "Sales", "binding": "aggregation",
            "aggregation": "Sum", "caption": "Sales", "kind": "value", "is_calc": False}


def _cat_field(name):
    return {"entity": "Orders", "property": name, "binding": "column", "aggregation": None,
            "caption": name, "kind": "category", "is_calc": False}


def _chart_base_proj():
    # The in-visual aggregation projection the Visual Calculation references by its nativeQueryRef;
    # queryRef matches _field_expression's "Sum(Orders.Sales)" so the wiring finds the base in Y.
    return {"field": {"Aggregation": {"Expression": {"Column": {}}, "Function": 0}},
            "queryRef": "Sum(Orders.Sales)", "nativeQueryRef": "Sum of Sales"}


def test_apply_visual_calcs_line_moving_average_appends_to_y_along_rows():
    base_proj = _chart_base_proj()
    ws = {"name": "Line", "visual_type": "line",
          "rows": [], "cols": [_cat_field("Month"), _chart_base_field()],
          "encodings": {"color": None, "label": None, "text": None}}
    state = {"Category": {"projections": [{"nativeQueryRef": "Month"}]},
             "Y": {"projections": [base_proj]}}
    warnings = []
    value_objects, fact = _apply_visual_calcs(
        ws, state,
        {"Line": [_usage(worksheet="Line", calc_type="WindowTotal", aggregation="Avg",
                         window_from=-2, window_to=0, ordering_type="Rows",
                         rows=[], cols=[_pill("Order Date", "Month")])]},
        None, None, warnings)

    assert value_objects is None                 # a cartesian chart carries no backColor cell fill
    assert fact["status"] == "emitted"
    assert fact["family"] == vcs.FAMILY_MOVING_AVERAGE
    assert fact["axis"] == "ROWS"                # chart geometry -> ROWS regardless of the token
    projections = state["Y"]["projections"]
    assert len(projections) == 2                 # base + the appended Visual Calculation
    assert base_proj["hidden"] is True           # the base measure is hidden ...
    vc = projections[1]
    assert "hidden" not in vc                     # ... and the moving average is the shown value
    native = vc["field"]["NativeVisualCalculation"]
    assert native["Expression"] == "MOVINGAVERAGE([Sum of Sales], 3, TRUE, ROWS)"
    assert native["Name"] == "Moving Average"


def test_apply_visual_calcs_bar_percent_of_total_collapses_and_reorders_category():
    base_proj = _chart_base_proj()
    region, segment = {"nativeQueryRef": "Region"}, {"nativeQueryRef": "Segment"}
    ws = {"name": "Bar", "visual_type": "bar",
          "rows": [_cat_field("Region")], "cols": [_cat_field("Segment"), _chart_base_field()],
          "encodings": {"color": None, "label": None, "text": None}}
    # _build_query_state orders Category rows-then-cols: [Region, Segment].
    state = {"Category": {"projections": [region, segment]},
             "Y": {"projections": [base_proj]}}
    warnings = []
    value_objects, fact = _apply_visual_calcs(
        ws, state,
        {"Bar": [_usage(worksheet="Bar", calc_type="PctTotal", derivation="Sum",
                        ordering_type="Columns",
                        rows=[_pill("Region", "None")], cols=[_pill("Segment", "None")])]},
        None, None, warnings)

    assert value_objects is None
    assert fact["status"] == "emitted"
    assert fact["family"] == vcs.FAMILY_PERCENT_OF_TOTAL
    y = state["Y"]["projections"]
    assert len(y) == 2
    assert base_proj["hidden"] is True
    assert (y[1]["field"]["NativeVisualCalculation"]["Expression"]
            == "DIVIDE([Sum of Sales], COLLAPSE([Sum of Sales], ROWS))")
    # COLLAPSE removes the innermost category level, so the addressed dim (Region) is re-nested inner
    # and the partition dim (Segment) outer: the category order becomes [Segment, Region].
    assert [p["nativeQueryRef"] for p in state["Category"]["projections"]] == ["Segment", "Region"]


def test_apply_visual_calcs_chart_yields_to_model_measure_path():
    base = _chart_base_field()
    base["measure_rebound"] = True                # the model path already bound this pill
    ws = {"name": "Line", "visual_type": "line", "rows": [], "cols": [base],
          "encodings": {"color": None, "label": None, "text": None}}
    state = {"Category": {"projections": [{"nativeQueryRef": "Month"}]},
             "Y": {"projections": [_chart_base_proj()]}}
    out = _apply_visual_calcs(
        ws, state, {"Line": [_usage(worksheet="Line", calc_type="CumTotal")]}, None, None, [])
    assert out == (None, None)                     # precedence: never double-emit
    assert len(state["Y"]["projections"]) == 1


# -- 12. the shared addressing decomposition + the AGG_DERIVATIONS guard -------
def test_resolve_addressing_truth_table():
    seg, reg = _pill("Segment"), _pill("Region")
    yr = _pill("Order Date", "Year")
    # Rows -> addressed on Cols, partition on Rows.
    assert vcs.resolve_addressing(_usage(rows=[seg], cols=[yr], ordering_type="Rows")) \
        == (["Order Date"], ["Segment"], None)
    # Columns -> addressed on Rows, partition on Cols (the token the measure path hands off).
    assert vcs.resolve_addressing(_usage(rows=[reg], cols=[seg], ordering_type="Columns")) \
        == (["Region"], ["Segment"], None)
    # Table -> everything addressed, no partition (grand total -> COLLAPSEALL).
    assert vcs.resolve_addressing(_usage(rows=[seg], cols=[yr], ordering_type="Table")) \
        == (["Segment", "Order Date"], [], None)
    # Field -> addressed = the ordering fields, partition = the rest.
    addr, part, reason = vcs.resolve_addressing(
        _usage(rows=[seg], cols=[yr], ordering_type="Field", ordering_fields=["Order Date"]))
    assert addr == ["Order Date"] and part == ["Segment"] and reason is None
    # Pane -> not decomposable from the shelves alone -> fail closed to a review reason.
    a, p, r = vcs.resolve_addressing(_usage(rows=[seg], cols=[yr], ordering_type="Pane"))
    assert a is None and p is None and "does not decompose" in r


def test_agg_derivations_is_one_shared_set_across_paths():
    import table_calc_to_dax as tcd
    from workbook_table_calcs import AGG_DERIVATIONS
    assert tcd._AGG_DERIVATIONS is AGG_DERIVATIONS      # the measure path reuses the one shared set
    assert _pill("Segment", "None").is_dimension is True
    assert _pill("Sales", "Sum").is_dimension is False  # an aggregated measure is not a dimension
    assert _pill("Customer", "User").is_dimension is False  # a user LOD reference is excluded too


# -- 13. per-projection number format (percent families) -----------------------
# A Tableau quick table calc that yields a RATIO (percent of total / difference, YoY, YTD growth,
# compound growth, percentile) is shown by Tableau as a percentage, so the VISIBLE Visual Calculation
# carries a percent display format on its projection -- the PBIR ``RoleProjection.format`` seam
# ("format string scoped to the visual", schema-verified). The absolute families keep the default/
# base format, and a hidden colour-driver stays unformatted (matching the hand-built oracle, whose
# hidden percent calc carries no format).
def test_emit_percent_family_carries_percent_number_format():
    (d,) = _emit(_spec(calc_type="PctTotal"))
    assert d.family == vcs.FAMILY_PERCENT_OF_TOTAL
    assert d.number_format == "0.00%"


def test_emit_absolute_family_has_no_number_format():
    (running,) = _emit(_spec(calc_type="CumTotal"))
    assert running.family == vcs.FAMILY_RUNNING_TOTAL
    assert running.number_format is None
    (movavg,) = _emit(_spec(calc_type="WindowTotal", aggregation="Avg",
                            window_from=-2, window_to=0))
    assert movavg.family == vcs.FAMILY_MOVING_AVERAGE
    assert movavg.number_format is None


def test_emit_ytd_growth_chain_formats_only_the_visible_outer():
    inner, outer = _emit(_spec(
        calc_type="CumTotal", secondary=True,
        secondary_pass={"calc_type": "PctDiff", "level_address": "[ds].[yr:Order Date:ok]"},
        level_break="[ds].[yr:Order Date:ok]"))
    assert inner.family == vcs.FAMILY_YTD
    assert inner.number_format is None                 # the inner YTD is absolute (and hidden)
    assert outer.family == vcs.FAMILY_YTD_GROWTH
    assert outer.number_format == "0.00%"              # only the visible growth ratio is a percent


def test_apply_visual_calcs_value_role_percent_projection_carries_format():
    base = _base_measure_field()
    ws = {"name": "WS", "encodings": {"color": None, "label": base, "text": None}}
    state, base_proj = _matrix_state()
    value_objects, fact = _apply_visual_calcs(
        ws, state, {"WS": [_usage(calc_type="PctTotal", level_break=None)]}, None, None, [])

    assert fact["family"] == vcs.FAMILY_PERCENT_OF_TOTAL
    vc_proj = state["Values"]["projections"][1]
    assert "hidden" not in vc_proj                     # value role: the percent is the shown value ...
    assert vc_proj["format"] == "0.00%"                # ... so it carries a percent display format
    assert fact["visual_calcs"][-1]["format"] == "0.00%"   # additive report enrichment


def test_apply_visual_calcs_color_role_percent_calc_stays_unformatted():
    base = _base_measure_field()
    ws = {"name": "WS", "encodings": {"color": base, "label": None, "text": None},
          "color_gradient": {"colors": ["#FFFFFF", "#FF0000"]}}
    state, base_proj = _matrix_state()
    value_objects, fact = _apply_visual_calcs(
        ws, state, {"WS": [_usage(calc_type="PctTotal", level_break=None)]}, None, None, [])

    assert fact["role"] == "color"
    vc_proj = state["Values"]["projections"][1]
    assert vc_proj["hidden"] is True                   # colour-driver: hidden, shows nothing ...
    assert "format" not in vc_proj                     # ... so no display format (matches the oracle)


