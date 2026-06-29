"""Tests for the table-calc consumer (``TableCalcUsage`` -> faithful DAX | Tier-1 handoff).

The consumer is deliberately conservative: an explicit Tableau ``Field`` scope ("Specific
Dimensions") whose addressing is *unambiguous*, and the ``Rows`` pane scope (whose across/down
direction is recovered from the worksheet shelves and verified against real Tableau output), take
the deterministic path; everything else -- the other scope-relative tokens (``Pane`` / ``Columns``
/ the compound ones), an order-sensitive calc addressed by more than one dimension, a sort by an
aggregate, a date-grain partition, a secondary (stacked) calculation, Rank, and relative-bound
moving windows -- hands off with its recovered addressing facts intact. These tests pin both halves
of that contract using synthetic :class:`TableCalcUsage` records (the consumer is duck-typed) and a
simple resolver.
"""
import pytest

from workbook_table_calcs import Pill, TableCalcUsage, extract_table_calc_usages
from table_calc_to_dax import (
    translate_table_calc_usage,
    translate_table_calc_usages,
    _intent_for,
    _rank_formula,
    _is_order_sensitive,
    _has_moving_bounds,
    _synthesize_formula,
    extract_percent_diff_base,
    inherited_addressing,
    translate_unplaced_percent_diff,
    _is_calc_token,
)


# -- a minimal resolver over a Superstore-shaped model -------------------------
_MEASURES = {"Sales", "Profit"}
_DATES = {"Order Date"}


def resolver(caption):
    """``caption -> (table, column, tmdl_type)`` for the synthetic 'Orders' table."""
    col = caption.replace(" ", "_")
    if caption in _MEASURES:
        return ("Orders", col, "double")
    if caption in _DATES:
        return ("Orders", col, "dateTime")
    return ("Orders", col, "string")


def _pill(column, derivation="None"):
    return Pill(instance=f"{derivation}:{column}", column=column, derivation=derivation)


def _usage(**kw):
    """Build a TableCalcUsage with sensible defaults for the Sheet-8 calibration layout."""
    defaults = dict(
        worksheet="WS", instance="i", column="Profit", caption="Profit", kind="quick",
        calc_type="CumTotal", aggregation="Sum", ordering_type="Field",
        rows=[_pill("Category"), _pill("Sub-Category"), _pill("Segment")],
        cols=[_pill("Profit", "Sum")],
    )
    defaults.update(kw)
    return TableCalcUsage(**defaults)


# -- the faithful path ---------------------------------------------------------
def test_field_single_dim_cumtotal_translates_as_running_total():
    u = _usage(ordering_fields=["Category"])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "translated"
    assert t.translated_by == "deterministic (workbook addressing)"
    # checked dim addresses; the two unchecked row dims partition.
    assert t.partition_by == ("Sub-Category", "Segment")
    assert t.order_by == (("Category", "ASC"),)
    # running total = WINDOW from partition start (1, ABS) to current row (0, REL).
    assert "WINDOW(1, ABS, 0, REL" in t.dax
    assert "ORDERBY('Orders'[Category], ASC)" in t.dax
    assert "PARTITIONBY('Orders'[Sub-Category], 'Orders'[Segment])" in t.dax
    assert "CALCULATE(SUM('Orders'[Profit]))" in t.dax


def test_field_order_insensitive_window_translates_with_multiple_dims():
    # WINDOW_SUM over the full partition is order-independent, so >1 addressing dim is fine.
    u = _usage(
        kind="field", calc_type=None, column="Calc1", caption="Window Sum", derivation="User",
        formula="WINDOW_SUM(SUM([Sales]))", aggregation=None,
        ordering_fields=["Category", "Sub-Category"],
        cols=[_pill("Calc1", "User")],
    )
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "translated"
    assert t.partition_by == ("Segment",)               # the one unchecked dim
    assert t.order_by == (("Category", "ASC"), ("Sub-Category", "ASC"))
    assert "WINDOW(1, ABS, -1, ABS" in t.dax            # whole partition
    assert "CALCULATE(SUM('Orders'[Sales]))" in t.dax


# -- the handoff contract ------------------------------------------------------
def test_field_multi_dim_order_sensitive_hands_off():
    u = _usage(ordering_fields=["Segment", "Category"])  # CumTotal is order-sensitive
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "multiple dimensions" in t.reason
    assert t.handoff["ordering_fields"] == ["Segment", "Category"]
    assert t.handoff["intent"] == "running total (cumulative)"


def test_field_sort_by_aggregate_hands_off():
    u = _usage(
        kind="field", calc_type=None, column="Calc2", caption="Index", derivation="User",
        formula="INDEX()", aggregation=None,
        ordering_fields=["Sub-Category"], sort_field="Sales", sort_direction="DESC",
        rows=[_pill("Sub-Category")], cols=[_pill("Sales", "Sum")],
    )
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "aggregate/derived field" in t.reason


def test_field_date_grain_partition_hands_off():
    # partition would include a Year-derived date pill -> needs date-table modeling.
    u = _usage(
        ordering_fields=["Category"],
        rows=[_pill("Category")],
        cols=[_pill("Profit", "Sum"), _pill("Order Date", "Year")],
    )
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "date-grain dimension" in t.reason


@pytest.mark.parametrize("token", ["Pane", "Columns", "ColumnInPane",
                                    "PaneCol", "CellInPane", "Cell", "Table"])
def test_scope_relative_tokens_hand_off(token):
    u = _usage(ordering_type=token, ordering_fields=[])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "scope-relative addressing" in t.reason
    assert t.handoff["ordering_type"] == token


# -- Rank (RANKX) -- synthesized from the QTC rank-options, translated by the window seam ------
def _rank_usage(**kw):
    """A Field-scope Rank QTC ranking sub-categories by Sum(Profit). A Rank QTC carries no
    ``aggregation`` attr -- the inner aggregate comes from the pill ``derivation``."""
    defaults = dict(
        worksheet="WS", instance="i", column="Profit", caption="Profit", kind="quick",
        calc_type="Rank", aggregation=None, derivation="Sum",
        rank_options="Competition,Descending", ordering_type="Field",
        ordering_fields=["Sub-Category"],
        rows=[_pill("Sub-Category")], cols=[_pill("Profit", "Sum")],
    )
    defaults.update(kw)
    return TableCalcUsage(**defaults)


class _RankOpts:
    """Duck-typed minimal usage for the pure ``_rank_formula`` synthesis unit."""
    def __init__(self, **kw):
        self.aggregation = None
        self.derivation = "Sum"
        self.rank_options = None
        self.__dict__.update(kw)


@pytest.mark.parametrize("opts, expected", [
    ("Competition,Descending", "RANK(SUM([Profit]), 'desc')"),
    ("Dense,Ascending",        "RANK_DENSE(SUM([Profit]), 'asc')"),
    ("ModifiedCompetition,Descending", "RANK_MODIFIED(SUM([Profit]), 'desc')"),
    ("",                       "RANK(SUM([Profit]), 'desc')"),   # Tableau default
    ("Descending",             "RANK(SUM([Profit]), 'desc')"),   # tie mode omitted -> competition
])
def test_rank_formula_maps_tie_modes_and_direction(opts, expected):
    formula, reason = _rank_formula(_RankOpts(rank_options=opts), "Profit")
    assert reason is None
    assert formula == expected


def test_rank_formula_unique_hands_off():
    # Tableau's 'Unique' ranking breaks ties by addressing order -> not faithful in DAX.
    formula, reason = _rank_formula(_RankOpts(rank_options="Unique,Descending"), "Profit")
    assert formula is None
    assert "Unique" in reason and "addressing order" in reason


def test_rank_formula_unknown_option_hands_off():
    formula, reason = _rank_formula(_RankOpts(rank_options="Percentile,Descending"), "Profit")
    assert formula is None
    assert "unsupported ranking option" in reason


def test_rank_formula_unsupported_aggregation_hands_off():
    # Count-distinct is not in the small faithful aggregate set; fail closed (no guessing).
    formula, reason = _rank_formula(_RankOpts(derivation="Cntd"), "Profit")
    assert formula is None
    assert "unsupported aggregation" in reason


def test_field_rank_competition_translates_as_rankx():
    t = translate_table_calc_usage(_rank_usage(), resolver)
    assert t.status == "translated"
    assert t.translated_by == "deterministic (workbook addressing)"
    assert t.intent == "rank within partition"
    assert t.partition_by == ()                      # ranks across all sub-categories
    assert t.order_by == (("Sub-Category", "ASC"),)
    # competition ranking (Skip ties), highest profit -> rank 1 (DESC), no partition FILTER.
    assert t.dax == ("RANKX(ALLSELECTED('Orders'[Sub-Category]), "
                     "CALCULATE(SUM('Orders'[Profit])), , DESC, Skip)")


def test_field_rank_dense_ascending_translates():
    t = translate_table_calc_usage(_rank_usage(rank_options="Dense,Ascending"), resolver)
    assert t.status == "translated"
    assert "Dense)" in t.dax                         # dense ranking
    assert ", ASC, " in t.dax                        # lowest value -> rank 1


def test_field_rank_partitioned_restricts_relation():
    # rank sub-categories by profit WITHIN each Region: Region is the unchecked partition dim.
    t = translate_table_calc_usage(
        _rank_usage(rows=[_pill("Region"), _pill("Sub-Category")]), resolver)
    assert t.status == "translated"
    assert t.partition_by == ("Region",)
    assert "FILTER(ALLSELECTED('Orders'[Region], 'Orders'[Sub-Category])" in t.dax
    assert "'Orders'[Region] = SELECTEDVALUE('Orders'[Region])" in t.dax


def test_field_rank_unique_hands_off_preserving_options():
    t = translate_table_calc_usage(_rank_usage(rank_options="Unique,Descending"), resolver)
    assert t.status == "handoff"
    assert "RANK_UNIQUE" in t.reason
    assert t.handoff["rank_options"] == "Unique,Descending"
    assert t.handoff["intent"] == "rank within partition"


# -- moving window (WindowTotal with relative from/to bounds) ------------------
def _moving_usage(**kw):
    """A WindowTotal moving-window QTC over Sum/Avg([Profit]) with integer relative bounds, addressed
    under the 'Rows' scope (partition = Rows dims, order across the Cols date axis)."""
    defaults = dict(
        worksheet="Moving", instance="i", column="Profit", caption="Profit", kind="quick",
        calc_type="WindowTotal", aggregation="Avg", window_from=-2, window_to=0,
        ordering_type="Rows", rows=[_pill("Segment")],
        cols=[_pill("Order Date", "Day-Trunc")],
    )
    defaults.update(kw)
    return TableCalcUsage(**defaults)


@pytest.mark.parametrize("formula, moving", [
    ("WINDOW_AVG(AVG([Sales]))", False),          # whole partition
    ("WINDOW_AVG(AVG([Sales]), -2, 0)", True),    # trailing-3 moving frame
    ("WINDOW_SUM(SUM([a]) + SUM([b]))", False),   # compound inner, no bounds
])
def test_has_moving_bounds_distinguishes_moving_from_whole_partition(formula, moving):
    assert _has_moving_bounds(formula) is moving


def test_is_order_sensitive_moving_window_is_order_sensitive():
    # a whole-partition window is order-independent; the SAME head with moving bounds is NOT.
    assert _is_order_sensitive("WINDOW_AVG(AVG([Sales]))") is False
    assert _is_order_sensitive("WINDOW_AVG(AVG([Sales]), -2, 0)") is True


def test_synthesize_moving_window_emits_bounds():
    formula, reason = _synthesize_formula(_moving_usage())
    assert reason is None
    assert formula == "WINDOW_AVG(AVG([Profit]), -2, 0)"


def test_rows_scope_moving_window_translates_with_relative_frame():
    t = translate_table_calc_usage(_moving_usage(), resolver)
    assert t.status == "translated"
    assert t.partition_by == ("Segment",)            # restarts at each Rows-shelf row
    assert t.order_by == (("Order Date", "ASC"),)    # runs across the Cols date axis
    assert "WINDOW(-2, REL, 0, REL" in t.dax         # relative moving frame
    assert "ORDERBY('Orders'[Order_Date], ASC)" in t.dax
    assert "PARTITIONBY('Orders'[Segment])" in t.dax
    assert "CALCULATE(AVERAGE('Orders'[Profit]))" in t.dax


def test_field_single_dim_moving_window_translates():
    u = _moving_usage(
        aggregation="Sum", window_from=-1, window_to=1, ordering_type="Field",
        ordering_fields=["Order Date"],
        rows=[_pill("Order Date")], cols=[_pill("Profit", "Sum")])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "translated"
    assert "SUMX(WINDOW(-1, REL, 1, REL" in t.dax
    assert "CALCULATE(SUM('Orders'[Profit]))" in t.dax


def test_field_multi_dim_moving_window_hands_off():
    # a moving window is order-SENSITIVE, so >1 addressing dim leaves the order ambiguous -> handoff
    # (contrast the whole-partition window, which stays translated with multiple dims).
    u = _moving_usage(
        ordering_type="Field", ordering_fields=["Category", "Sub-Category"],
        rows=[_pill("Category"), _pill("Sub-Category"), _pill("Segment")],
        cols=[_pill("Profit", "Sum")])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "multiple dimensions" in t.reason


def test_moving_window_unsupported_aggregation_hands_off():
    # the seam only certifies SUM/AVG/MIN/MAX moving frames; a moving COUNT/STDEV stays a handoff.
    formula, reason = _synthesize_formula(_moving_usage(aggregation="Count"))
    assert formula is None
    assert "unsupported aggregation" in reason


def test_moving_window_one_sided_bound_hands_off():
    # a complete relative frame needs BOTH integer bounds; a None bound is not a moving frame.
    formula, reason = _synthesize_formula(_moving_usage(window_from=-2, window_to=None))
    assert formula is None
    assert "both integer-literal bounds" in reason


# -- the 'Rows' pane scope (verified against real Tableau output) --------------
def _rows_scope_usage(**kw):
    """A ``Rows`` pane-scope usage shaped like the pilot: a calc pill restarting per Rows-shelf
    dimension and running across a (date) Cols axis. The verified addressing is partition = the
    Rows dims, order = across the Cols dims."""
    defaults = dict(
        worksheet="Segment % Dod", instance="usr:Calc:qk", column="Calc1",
        caption="Window Sum", kind="field", calc_type=None, derivation="User",
        formula="WINDOW_SUM(SUM([Sales]))", aggregation=None, ordering_type="Rows",
        ordering_fields=[],
        rows=[_pill("Segment")],
        cols=[_pill("Order Date", "Day-Trunc")],
    )
    defaults.update(kw)
    return TableCalcUsage(**defaults)


def test_rows_scope_partitions_by_rows_orders_across_cols():
    # VERIFIED: 'Rows' scope -> partition = Rows-shelf dims (Segment), order across Cols (Order Date,
    # a day-grain date axis -- allowed as the natural chronological order).
    t = translate_table_calc_usage(_rows_scope_usage(), resolver)
    assert t.status == "translated"
    assert t.translated_by == "deterministic (workbook addressing)"
    assert t.partition_by == ("Segment",)
    assert t.order_by == (("Order Date", "ASC"),)
    assert "PARTITIONBY('Orders'[Segment])" in t.dax
    assert "ORDERBY('Orders'[Order_Date], ASC)" in t.dax


def test_rows_scope_no_row_dim_is_unpartitioned_window():
    # The pilot's WINDOW_STDEV line chart: empty Rows shelf -> partition=[], order across Order Date.
    t = translate_table_calc_usage(
        _rows_scope_usage(rows=[], formula="WINDOW_STDEV(SUM([Sales]))"), resolver)
    assert t.status == "translated"
    assert t.partition_by == ()
    assert t.order_by == (("Order Date", "ASC"),)
    assert "STDEVX.S" in t.dax


def test_rows_scope_aggregate_on_order_axis_hands_off():
    # An aggregate measure on Cols is not a dimension to order across -> honest handoff.
    t = translate_table_calc_usage(
        _rows_scope_usage(cols=[_pill("Profit", "Sum")]), resolver)
    assert t.status == "handoff"
    assert "order (Cols) axis" in t.reason


def test_rows_scope_no_cols_axis_hands_off():
    # No Cols dimension -> the across direction is unrecoverable -> handoff.
    t = translate_table_calc_usage(_rows_scope_usage(cols=[]), resolver)
    assert t.status == "handoff"
    assert "no Cols dimension" in t.reason


def test_rows_scope_date_grain_partition_hands_off():
    # A date-grain dimension on the partition (Rows) needs date-table modeling -> handoff.
    t = translate_table_calc_usage(
        _rows_scope_usage(rows=[_pill("Order Date", "Year")]), resolver)
    assert t.status == "handoff"
    assert "date-grain dimension" in t.reason


def test_rows_scope_order_sensitive_multi_cols_hands_off():
    # An order-sensitive calc addressed across two Cols dims has an ambiguous order -> handoff.
    t = translate_table_calc_usage(
        _rows_scope_usage(formula="RUNNING_SUM(SUM([Sales]))",
                          cols=[_pill("Order Date", "Day-Trunc"), _pill("Region")]),
        resolver)
    assert t.status == "handoff"
    assert "multiple Cols" in t.reason


# -- percent-difference-from-prior quick table calc (composite; dedicated emitter) ---------------
_OID = "[__tableau_internal_object_id__].[Orders_ECFCA1FB690A41FE803BC071773BA862]"


def _pct_diff_usage(**kw):
    """A percent-difference quick table calc shaped like the pilot's heat-grid colour pill: a
    ``pcdf`` QTC over a base measure, restarting per Rows-shelf dim and running across a date Cols
    axis (partition=[Segment], order=[Order Date])."""
    defaults = dict(
        worksheet="Segment % Dod", instance="pcdf:usr:Calculation_0014172369735704:qk",
        column="Sales", caption="SUM(Sales)", kind="quick", calc_type="PctDiff",
        aggregation="Sum", ordering_type="Rows", ordering_fields=[],
        rows=[_pill("Segment")], cols=[_pill("Order Date", "Day-Trunc")],
    )
    defaults.update(kw)
    return TableCalcUsage(**defaults)


def test_pct_diff_over_aggregated_pill_translates():
    # pcdf over a directly-aggregated pill (SUM([Sales])): faithful DIVIDE over an OFFSET prior row,
    # addressed partition=[Segment] / order=[Order Date] from the Rows/Cols shelves.
    t = translate_table_calc_usage(_pct_diff_usage(), resolver)
    assert t.status == "translated"
    assert t.translated_by == "deterministic (workbook addressing)"
    assert t.partition_by == ("Segment",)
    assert t.order_by == (("Order Date", "ASC"),)
    assert t.dax.startswith("DIVIDE(")
    assert "OFFSET(-1, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Segment]))" in t.dax
    assert "ABS(CALCULATE(SUM('Orders'[Sales])" in t.dax


def test_pct_diff_inlines_named_calc_base():
    # The pilot's exact shape: the pcdf sits over the NAMED calc [count orders] + 100, whose formula
    # (and its nested [count orders] = ZN(COUNT(<object-id>))) is inlined to a self-contained
    # aggregate, then COUNT(<object-id>) -> COUNTROWS('Orders') because 'Orders' is a known table.
    lookup = {
        "calculation_0014172369248279": f"ZN(COUNT({_OID}))",
        "calculation_0014172369735704": "[Calculation_0014172369248279] + 100",
    }
    u = _pct_diff_usage(column="[Calculation_0014172369735704]", caption="[count orders] + 100",
                        aggregation=None)
    t = translate_table_calc_usage(u, resolver, known_tables={"Orders"},
                                   base_formula_lookup=lookup)
    assert t.status == "translated"
    assert t.partition_by == ("Segment",)
    assert t.order_by == (("Order Date", "ASC"),)
    assert "COUNTROWS('Orders')" in t.dax
    assert "+ 100" in t.dax
    assert "OFFSET(-1, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Segment]))" in t.dax


def test_pct_diff_unresolvable_base_hands_off():
    # No known calc base and no aggregation -> the base is not a single aggregate -> honest handoff.
    u = _pct_diff_usage(column="MysteryPill", caption="MysteryPill", aggregation=None)
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "neither a known calc nor a directly aggregated pill" in t.reason


def test_pct_diff_first_row_is_blank_via_divide_offset():
    # Faithfulness note pinned as a contract: the prior-row value is an OFFSET(-1, ...) that is BLANK
    # on the first row of each partition, and DIVIDE returns BLANK for a blank/zero denominator --
    # matching Tableau's null first row. The emitted shape encodes exactly that.
    t = translate_table_calc_usage(_pct_diff_usage(), resolver)
    assert "DIVIDE(" in t.dax and "OFFSET(-1" in t.dax
    assert t.dax.count("OFFSET(-1") == 2  # current - prior, and ABS(prior)


# -- difference-from-prior quick table calc (composite; the percent-diff's additive sibling) ---------
def _diff_usage(**kw):
    """A difference-from-prior quick table calc: the composite X - LOOKUP(X,-1), restarting per
    Rows-shelf dim and running across a date Cols axis (partition=[Segment], order=[Order Date])."""
    defaults = dict(
        worksheet="Sales Diff", instance="diff:usr:Calculation_0014172369735704:qk",
        column="Sales", caption="SUM(Sales)", kind="quick", calc_type="Difference",
        aggregation="Sum", ordering_type="Rows", ordering_fields=[],
        rows=[_pill("Segment")], cols=[_pill("Order Date", "Day-Trunc")],
    )
    defaults.update(kw)
    return TableCalcUsage(**defaults)


def test_difference_over_aggregated_pill_translates():
    # X - LOOKUP(X,-1) over a directly-aggregated pill: faithful (X) - prior over an OFFSET prior row,
    # addressed partition=[Segment] / order=[Order Date] from the Rows/Cols shelves.
    t = translate_table_calc_usage(_diff_usage(), resolver)
    assert t.status == "translated"
    assert t.translated_by == "deterministic (workbook addressing)"
    assert t.intent == "difference from a prior row"
    assert t.partition_by == ("Segment",)
    assert t.order_by == (("Order Date", "ASC"),)
    assert t.dax == (
        "VAR _prev = CALCULATE(SUM('Orders'[Sales]), "
        "OFFSET(-1, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Segment]))) "
        "RETURN IF(ISBLANK(_prev), BLANK(), (SUM('Orders'[Sales])) - _prev)")


def test_difference_inlines_named_calc_base():
    # The base is the NAMED calc [count orders] + 100, inlined to a self-contained aggregate, with the
    # nested [count orders] = ZN(COUNT(<object-id>)) -> COALESCE(COUNTROWS('Orders'),0) (known table).
    lookup = {
        "calculation_0014172369248279": f"ZN(COUNT({_OID}))",
        "calculation_0014172369735704": "[Calculation_0014172369248279] + 100",
    }
    u = _diff_usage(column="[Calculation_0014172369735704]", caption="[count orders] + 100",
                    aggregation=None)
    t = translate_table_calc_usage(u, resolver, known_tables={"Orders"}, base_formula_lookup=lookup)
    assert t.status == "translated"
    assert t.partition_by == ("Segment",)
    assert t.order_by == (("Order Date", "ASC"),)
    assert "COUNTROWS('Orders')" in t.dax
    assert "+ 100" in t.dax
    assert "OFFSET(-1, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Segment]))" in t.dax


def test_difference_multi_dim_hands_off():
    # Difference looks back one row, so it is order-SENSITIVE: two addressing dimensions leave the
    # slowest->fastest order unrecoverable from the workbook -> honest handoff (never a guessed order).
    u = _diff_usage(ordering_type="Field", ordering_fields=["Category", "Sub-Category"],
                    rows=[_pill("Category"), _pill("Sub-Category")], cols=[_pill("Sales", "Sum")])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "addressed by multiple dimensions" in t.reason


def test_difference_unresolvable_base_hands_off():
    # No known calc base and no aggregation -> the base is not a single aggregate -> honest handoff.
    u = _diff_usage(column="MysteryPill", caption="MysteryPill", aggregation=None)
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "neither a known calc nor a directly aggregated pill" in t.reason


def test_difference_first_row_is_blank_via_isblank_guard():
    # Contract: Tableau shows the first row of each partition as NULL (no prior to compare). The shape
    # guards that exactly -- OFFSET(-1,...) is blank on the first row and ISBLANK returns BLANK rather
    # than letting DAX coerce the missing prior into (X) - 0. A single prior-row lookup (unlike
    # percent-diff, which needs the prior twice: current - prior and ABS(prior)).
    t = translate_table_calc_usage(_diff_usage(), resolver)
    assert "RETURN IF(ISBLANK(_prev), BLANK(), " in t.dax
    assert t.dax.count("OFFSET(-1") == 1


# -- percent-of-total quick table calc (composite; order-INSENSITIVE whole-scope re-aggregation) ------
def _pct_total_usage(**kw):
    """A percent-of-total quick table calc: X / TOTAL(X) over an addressing scope. Order-INSENSITIVE
    (a whole-scope re-aggregation), so it addresses faithfully even across multiple dimensions."""
    defaults = dict(
        worksheet="Share of Total", instance="pctt:usr:Calculation_0014172369735704:qk",
        column="Sales", caption="SUM(Sales)", kind="quick", calc_type="PercentOfTotal",
        aggregation="Sum", ordering_type="Field", ordering_fields=["Sub-Category"],
        rows=[_pill("Sub-Category")], cols=[_pill("Sales", "Sum")],
    )
    defaults.update(kw)
    return TableCalcUsage(**defaults)


def test_pct_of_total_single_dim_translates():
    # One addressing dim, no remaining partition -> the scope total spans that whole column:
    # DIVIDE(X, CALCULATE(X, ALLSELECTED(<addr dim>))).
    t = translate_table_calc_usage(_pct_total_usage(), resolver)
    assert t.status == "translated"
    assert t.translated_by == "deterministic (workbook addressing)"
    assert t.intent == "percent-of-scope ratio"
    assert t.partition_by == ()
    assert t.dax == ("DIVIDE(SUM('Orders'[Sales]), "
                     "CALCULATE(SUM('Orders'[Sales]), ALLSELECTED('Orders'[Sub-Category])))")


def test_pct_of_total_multi_dim_translates():
    # The KEY differentiator from Difference: percent-of-total is order-INSENSITIVE, so two or more
    # addressing dimensions stay faithful (the order spec merely frames the scope). partition=[Segment]
    # (the dim not addressed); the scope total re-aggregates over Sub-Category x Order Date per Segment.
    u = _pct_total_usage(ordering_fields=["Sub-Category", "Order Date"],
                         rows=[_pill("Segment"), _pill("Sub-Category"), _pill("Order Date")],
                         cols=[_pill("Sales", "Sum")])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "translated"
    assert t.partition_by == ("Segment",)
    assert t.dax == (
        "DIVIDE(SUM('Orders'[Sales]), CALCULATE(SUM('Orders'[Sales]), "
        "FILTER(ALLSELECTED('Orders'[Segment], 'Orders'[Sub-Category], 'Orders'[Order_Date]), "
        "'Orders'[Segment] = SELECTEDVALUE('Orders'[Segment]))))")


def test_pct_of_total_unresolvable_base_hands_off():
    # No known calc base and no aggregation -> the base is not a single aggregate -> honest handoff.
    u = _pct_total_usage(column="MysteryPill", caption="MysteryPill", aggregation=None)
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "neither a known calc nor a directly aggregated pill" in t.reason


def test_pct_of_total_denominator_is_calculate_over_total_scope():
    # Structural contract: the share is DIVIDE(X, <total>), where the denominator is a CALCULATE that
    # re-aggregates the SAME base over the addressing scope (so a zero/blank scope total -> BLANK).
    t = translate_table_calc_usage(_pct_total_usage(), resolver)
    assert t.dax.startswith("DIVIDE(SUM('Orders'[Sales]), CALCULATE(SUM('Orders'[Sales]), ")
    assert t.dax.count("SUM('Orders'[Sales])") == 2  # numerator mark + denominator scope total


# -- force-translating an UNPLACED percent-difference measure (the pilot's `Percent Difference`) ----
_PCT_DIFF_FORMULA = (f"(ZN(COUNT({_OID})) - LOOKUP(ZN(COUNT({_OID})),-1)) "
                     f"/ ABS(LOOKUP(ZN(COUNT({_OID})),-1))")


def _consumer(rows, cols, formula='if [Calculation1] <= 0 then "Grey" else "Red" END'):
    """A PLACED consumer usage that references the unplaced calc (the Grey/Red colour rule on the
    pilot's 'Line chart (2)'), from which the unplaced calc inherits its window."""
    return TableCalcUsage(
        worksheet="Line chart (2)", instance="c", column="Calculation_0014172376637481",
        caption="Difference coloring", kind="field", calc_type=None, formula=formula,
        ordering_type="Rows", secondary=True, rows=rows, cols=cols)


def test_extract_percent_diff_base_matches_pilot_composite():
    assert extract_percent_diff_base(_PCT_DIFF_FORMULA) == f"ZN(COUNT({_OID}))"


@pytest.mark.parametrize("formula", [
    "",
    "ZN(COUNT([x]))",                            # not a composite at all
    "(A - LOOKUP(B,-1)) / ABS(LOOKUP(B,-1))",    # numerator/denominator bases differ
    "(A - LOOKUP(A,-1)) / LOOKUP(A,-1)",         # denominator is not ABS-wrapped
    "(A - LOOKUP(A,-2)) / ABS(LOOKUP(A,-2))",    # looks back more than one row
    "(A + LOOKUP(A,-1)) / ABS(LOOKUP(A,-1))",    # sum, not difference
])
def test_extract_percent_diff_base_rejects_non_composites(formula):
    assert extract_percent_diff_base(formula) is None


def test_is_calc_token_distinguishes_calcs_from_dimensions():
    assert _is_calc_token("Calculation_0014172376367143")  # auto-generated token
    assert _is_calc_token("Calculation1")                  # legacy short token
    assert _is_calc_token("West Sales (copy)_0001", calc_tokens={"West Sales (copy)_0001"})
    assert not _is_calc_token("Order Date")
    assert not _is_calc_token("")


def test_inherited_addressing_partitions_by_plain_rows_orders_across_cols():
    c = _consumer(rows=[_pill("Segment")], cols=[_pill("Order Date", "Day-Trunc")])
    order_by, partition_by, reason = inherited_addressing(c)
    assert reason is None
    assert order_by == (("Order Date", "ASC"),)
    assert partition_by == ("Segment",)


def test_inherited_addressing_excludes_calc_pill_from_partition():
    # The pilot's exact shape: the consumer's Rows pill is a calc token (a plotted measure on a line
    # chart), NOT a categorical dimension -> excluded, so the inherited window is UNPARTITIONED.
    c = _consumer(rows=[_pill("Calculation_0014172376367143")],
                  cols=[_pill("Order Date", "Day-Trunc")])
    order_by, partition_by, reason = inherited_addressing(c)
    assert reason is None
    assert order_by == (("Order Date", "ASC"),)
    assert partition_by == ()


def test_inherited_addressing_no_plain_cols_axis_fails_closed():
    c = _consumer(rows=[_pill("Segment")], cols=[])
    order_by, partition_by, reason = inherited_addressing(c)
    assert order_by is None and partition_by is None
    assert "no plain Cols dimension" in reason


def test_translate_unplaced_percent_diff_inherits_unpartitioned_window():
    # Force-translate the pilot's `Percent Difference`: base ZN(COUNT(<oid>)) -> COUNTROWS('Orders'),
    # window inherited from the line-chart consumer => order=[Order Date], UNPARTITIONED.
    c = _consumer(rows=[_pill("Calculation_0014172376367143")],
                  cols=[_pill("Order Date", "Day-Trunc")])
    dax, reason, order_by, partition_by = translate_unplaced_percent_diff(
        _PCT_DIFF_FORMULA, c, resolver, known_tables={"Orders"})
    assert reason is None
    assert dax.startswith("DIVIDE(")
    assert "COUNTROWS('Orders')" in dax
    assert "OFFSET(-1, ORDERBY('Orders'[Order_Date], ASC))" in dax
    assert "PARTITIONBY" not in dax            # the calc Rows pill was excluded -> unpartitioned
    assert order_by == (("Order Date", "ASC"),)
    assert partition_by == ()


def test_translate_unplaced_percent_diff_inlines_named_base():
    # When the base aggregate references a named calc, it is inlined to a self-contained aggregate.
    lookup = {"calculation_0014172369248279": f"ZN(COUNT({_OID}))"}
    formula = ("([Calculation_0014172369248279] - LOOKUP([Calculation_0014172369248279],-1)) "
               "/ ABS(LOOKUP([Calculation_0014172369248279],-1))")
    c = _consumer(rows=[_pill("Segment")], cols=[_pill("Order Date", "Day-Trunc")])
    dax, reason, _o, partition_by = translate_unplaced_percent_diff(
        formula, c, resolver, known_tables={"Orders"}, base_formula_lookup=lookup)
    assert reason is None
    assert "COUNTROWS('Orders')" in dax
    assert partition_by == ("Segment",)


def test_translate_unplaced_percent_diff_non_composite_fails_closed():
    c = _consumer(rows=[_pill("Segment")], cols=[_pill("Order Date", "Day-Trunc")])
    dax, reason, _o, _p = translate_unplaced_percent_diff("ZN(COUNT([x]))", c, resolver)
    assert dax is None
    assert "percent-difference composite" in reason


def test_translate_unplaced_percent_diff_no_order_axis_fails_closed():
    c = _consumer(rows=[_pill("Segment")], cols=[])
    dax, reason, _o, _p = translate_unplaced_percent_diff(
        _PCT_DIFF_FORMULA, c, resolver, known_tables={"Orders"})
    assert dax is None
    assert "no plain Cols dimension" in reason


def test_secondary_stacked_calc_hands_off():
    u = _usage(ordering_fields=["Category"], secondary=True)
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "secondary" in t.reason.lower()
    assert t.handoff["secondary"] is True


def test_rank_quick_calc_hands_off():
    u = _usage(calc_type="Rank", aggregation=None, rank_options="Unique,Descending",
               ordering_fields=["Sub-Category"])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "Rank" in t.reason


def test_moving_window_relative_bounds_single_dim_translates():
    # a single addressing dim leaves no order ambiguity, so a moving frame now translates faithfully
    # (this case formerly handed off when moving windows were unsupported in Tier 0).
    u = _usage(calc_type="WindowTotal", aggregation="Avg", window_from=-2, window_to=0,
               ordering_fields=["Category"])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "translated"
    assert t.intent == "moving window"
    assert t.order_by == (("Category", "ASC"),)
    assert "WINDOW(-2, REL, 0, REL" in t.dax
    assert "CALCULATE(AVERAGE('Orders'[Profit]))" in t.dax


# -- shape / batch / intent ----------------------------------------------------
def test_translation_to_dict_roundtrips():
    u = _usage(ordering_fields=["Category"])
    d = translate_table_calc_usage(u, resolver).to_dict()
    assert d["status"] == "translated"
    assert d["partition_by"] == ["Sub-Category", "Segment"]
    assert d["order_by"] == [["Category", "ASC"]]
    assert d["handoff"] is None


def test_handoff_to_dict_carries_facts():
    u = _usage(ordering_type="Pane", ordering_fields=[])
    d = translate_table_calc_usage(u, resolver).to_dict()
    assert d["status"] == "handoff"
    assert d["dax"] is None
    assert d["handoff"]["shelf_rows"] == [
        ["Category", "None"], ["Sub-Category", "None"], ["Segment", "None"]]


def test_batch_translate_mixes_outcomes():
    translated = _usage(ordering_fields=["Category"])
    handed_off = _usage(ordering_type="Pane", ordering_fields=[])
    out = translate_table_calc_usages([translated, handed_off], resolver)
    assert [t.status for t in out] == ["translated", "handoff"]


def test_intent_labels():
    assert _intent_for(_usage()) == "running total (cumulative)"
    assert _intent_for(_usage(calc_type="WindowTotal", window_from=-2, window_to=0)) == "moving window"
    win = _usage(kind="field", calc_type=None, formula="WINDOW_AVG(SUM([Sales]))")
    assert _intent_for(win) == "window aggregate (partition or moving)"


# -- end-to-end: raw .twb XML -> extractor -> consumer -> DAX -------------------
# These guard the extractor<->consumer *seam*: the consumer's other tests build TableCalcUsage
# by hand, so nothing else proves the shape the extractor actually emits (e.g. bracket-free
# field ids, the Field-scope <order> list) is the shape the consumer consumes.
E2E_RUNNING_TOTAL_TWB = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <worksheets>
    <worksheet name='Running Total'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column aggregation='Sum' datatype='real' name='[Profit]' role='measure' type='quantitative' caption='Profit' />
            <column datatype='string' name='[Category]' role='dimension' type='nominal' caption='Category' />
            <column datatype='string' name='[Sub-Category]' role='dimension' type='nominal' caption='Sub-Category' />
            <column datatype='string' name='[Segment]' role='dimension' type='nominal' caption='Segment' />
            <column-instance column='[Profit]' derivation='Sum' name='[cum:sum:Profit:qk]' pivot='key' type='quantitative'>
              <table-calc aggregation='Sum' level-break='[ds0].[Category]' ordering-type='Field' type='CumTotal'>
                <order field='[ds0].[none:Category:nk]' />
              </table-calc>
            </column-instance>
            <column-instance column='[Category]' derivation='None' name='[none:Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[Sub-Category]' derivation='None' name='[none:Sub-Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[Segment]' derivation='None' name='[none:Segment:nk]' pivot='key' type='nominal' />
          </datasource-dependencies>
        </view>
        <rows>([ds0].[none:Category:nk] / ([ds0].[none:Sub-Category:nk] / [ds0].[none:Segment:nk]))</rows>
        <cols>[ds0].[cum:sum:Profit:qk]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>
"""


def test_end_to_end_xml_to_running_total_dax():
    [u] = extract_table_calc_usages(E2E_RUNNING_TOTAL_TWB)
    # the extractor emits a bare (bracket-free) column -- the exact contract the consumer assumes.
    assert u.column == "Profit"
    assert u.ordering_type == "Field"
    assert u.ordering_fields == ["Category"]

    t = translate_table_calc_usage(u, resolver)
    assert t.status == "translated"
    assert t.partition_by == ("Sub-Category", "Segment")
    assert t.order_by == (("Category", "ASC"),)
    assert t.dax == (
        "SUMX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Category], ASC), "
        "PARTITIONBY('Orders'[Sub-Category], 'Orders'[Segment])), "
        "CALCULATE(SUM('Orders'[Profit])))"
    )


E2E_SECONDARY_TWB = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <worksheets>
    <worksheet name='Stacked'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column aggregation='Sum' datatype='real' name='[Profit]' role='measure' type='quantitative' caption='Profit' />
            <column datatype='string' name='[Sub-Category]' role='dimension' type='nominal' caption='Sub-Category' />
            <column-instance column='[Profit]' derivation='Sum' name='[pcto:cum:sum:Profit:qk]' pivot='key' type='quantitative'>
              <table-calc aggregation='Sum' level-break='[ds0].[Sub-Category]' ordering-type='Field' type='CumTotal'>
                <order field='[ds0].[none:Sub-Category:nk]' />
              </table-calc>
              <table-calc level-address='[ds0].[none:Sub-Category:nk]' ordering-type='Field' type='PctTotal'>
                <order field='[ds0].[none:Sub-Category:nk]' />
              </table-calc>
            </column-instance>
            <column-instance column='[Sub-Category]' derivation='None' name='[none:Sub-Category:nk]' pivot='key' type='nominal' />
          </datasource-dependencies>
        </view>
        <rows>[ds0].[none:Sub-Category:nk]</rows>
        <cols>[ds0].[pcto:cum:sum:Profit:qk]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>
"""


def test_end_to_end_xml_secondary_calc_hands_off():
    [u] = extract_table_calc_usages(E2E_SECONDARY_TWB)
    assert u.secondary is True
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert t.dax is None
    assert "secondary" in (t.reason or "")
    assert t.handoff["secondary"] is True


def test_synthesize_tolerates_bracketed_column():
    # a caller passing a *bracketed* field id must not double-wrap into "[[Profit]]" and degrade
    # to a misleading parser handoff -- it yields the same faithful DAX as the bare id.
    bare = translate_table_calc_usage(_usage(column="Profit", ordering_fields=["Category"]), resolver)
    bracketed = translate_table_calc_usage(_usage(column="[Profit]", ordering_fields=["Category"]), resolver)
    assert bracketed.status == "translated"
    assert bracketed.dax == bare.dax
