"""Aggregating a table-scoped LOD is Tableau syntax, not Tableau arithmetic — so DAX drops it.

The reporter's workbook highlights its best sub-category with

    IF SUM([Profit]) = SUM({ MAX({ FIXED [Sub-Category] : SUM([Profit]) }) }) THEN TRUE ELSE FALSE END

and the engine stubbed it on ``re-aggregating a table-scoped LOD is not supported``, leaving the bar
chart a single flat colour. The instinct is to read that outer ``SUM`` as arithmetic and refuse to
guess at it. It is not arithmetic. Tableau documents both halves of this:

* it is *there* only because **"Level of detail expressions are always automatically wrapped in an
  aggregate when they are added to a shelf in the view"** -- Tableau's aggregate/non-aggregate mixing
  rule needs a wrapper, and Tableau types one in for you; and
* it *does nothing* because **"When no aggregation is needed (because the expression's level of
  detail is coarser than the view's), the aggregation you specified is still shown when the
  expression is on a shelf, but it is ignored."**
  (https://help.tableau.com/current/pro/desktop/en-us/calculations_calculatedfields_lod_overview.htm)

A *table-scoped* LOD is the coarsest value that exists -- one number for the whole table, identical
on every row -- so it is coarser than every possible view grain and that "ignored" clause always
holds. Which outer aggregate was written is therefore irrelevant, and this is the one LOD shape
where the collapse is unconditionally safe rather than a judgement call.

That distinction is what keeps the notorious "SUM of a FIXED LOD multiplies by the row count" gotcha
out of scope here. That gotcha needs a grain BETWEEN row level and the view -- a partly-replicated
value a real SUM then double-counts. A table-scoped LOD has no such middle grain, so there is
nothing to double-count. A DIMENSIONED LOD still goes down the SUMMARIZE re-aggregation path
untouched, and the tests below pin that boundary.

ALL, NOT ALLSELECTED, and the workbook proves why the difference is real rather than pedantic. It
carries the same "highlight the max" intent written a second way, as a table calculation
(``WINDOW_MAX(SUM([Profit]))``), and the two are NOT interchangeable: FIXED **"ignores all the
filters in the view other than context filters, data source filters, and extract filters"** because
Tableau evaluates it before dimension filters, so it maps to ALL; WINDOW_MAX runs over the marks
actually in the partition, so it maps to ALLSELECTED. Same picture unfiltered, different pictures the
moment a reader touches a slicer. Rendered side by side in Power BI Desktop against a refreshed
model, both routes independently highlighted Copiers -- which is the cross-check that the collapsed
LOD really does compute what the table calc computes.
"""
import pytest  # noqa: F401  (kept for parametrized additions)

from test_calc_to_dax import _tx


def test_the_reporter_s_max_highlight_translates():
    """The whole point: this stubbed, so the bar chart shipped one flat colour."""
    assert _tx("SUM({MAX({fixed [Region] : SUM([Sales])})})") == (
        "CALCULATE(MAXX(SUMMARIZE('Orders', 'Orders'[Region]), "
        "CALCULATE(SUM('Orders'[Sales]))), ALL('Orders'))")


def test_the_outer_aggregate_is_discarded_whichever_one_was_written():
    """Tableau ignores it, so every wrapper must collapse to the SAME DAX -- including the bare LOD.

    If any of these disagreed, the emitted DAX would be encoding a wrapper Tableau never evaluated.
    """
    bare = _tx("{MAX({fixed [Region] : SUM([Sales])})}")
    for agg in ("SUM", "MIN", "MAX", "AVG"):
        assert _tx("%s({MAX({fixed [Region] : SUM([Sales])})})" % agg) == bare


def test_a_simple_table_scoped_lod_re_aggregates_to_itself():
    """The degenerate case: SUM({SUM(x)}) is SUM(x) over the whole table, not a double sum."""
    assert _tx("SUM({SUM([Sales])})") == _tx("{SUM([Sales])}") == \
        "CALCULATE(SUM('Orders'[Sales]), ALL('Orders'))"


def test_the_collapse_uses_all_not_allselected():
    """FIXED is evaluated BEFORE dimension filters, so it ignores them -- that is ALL."""
    dax = _tx("SUM({MAX({fixed [Region] : SUM([Sales])})})")
    assert "ALL('Orders')" in dax
    assert "ALLSELECTED" not in dax


def test_a_dimensioned_lod_still_takes_the_re_aggregation_path():
    """The boundary. Only the table-scoped shape collapses; a real grain is still iterated.

    This is the case the "SUM replicates per row" gotcha is actually about, so it must NOT be
    swept into the collapse.
    """
    assert _tx("MAX({fixed [Region] : SUM([Sales])})") == (
        "MAXX(SUMMARIZE('Orders', 'Orders'[Region]), CALCULATE(SUM('Orders'[Sales])))")


def test_re_aggregating_a_table_scoped_include_or_exclude_still_falls_back():
    """The collapse is justified by FIXED's semantics; it is not extended to the view-relative kinds."""
    for expr in ("SUM({EXCLUDE [Region] : SUM([Sales])})",
                 "AVG({FIXED [Region] : {INCLUDE [Order Date] : SUM([Sales])}})"):
        assert _tx(expr) is None, expr


def test_an_aggregate_that_cannot_re_aggregate_an_lod_is_still_refused():
    """COUNTD over an LOD is rejected before the collapse, so the guard order is preserved."""
    assert _tx("COUNTD({FIXED [Region] : SUM([Sales])})") is None
