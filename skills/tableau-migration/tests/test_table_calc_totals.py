"""A table calc on a SUBTOTAL / GRAND TOTAL row must reproduce Tableau's single-mark window.

A Tableau table calculation is evaluated inside its own window. On a total row that window
collapses to a SINGLE mark, so every rank there is 1 and a percentile is 0.0 -- which is exactly
what the source renders.

Translated to DAX the addressing column is simply OUT OF SCOPE on that row, so ``CALCULATE(<inner>)``
yields the TOTAL's value and ``RANKX`` cheerfully ranks that total against the individual marks. The
result is a plausible-looking middle ordinal that corresponds to nothing at all. It is not confined
to the rank column either: a composite score built from several ranks inherits every one of those
wrong ordinals, so the error surfaces in a headline number far from the rank that caused it.

Measured against a customer render of a 13-region highlight table:

  * ``Weighted Rank`` totalled **9** where the source shows **1**; and
  * ``Weighted Rank Score`` totalled **96.30** where the source shows **100.5**.

The second is the decisive evidence. That score is ``101 - (0.15*RankGMC + 0.20*RankMH +
0.20*RankANS)``, and 100.5 is only reachable when EVERY constituent rank equals 1
(``101 - 0.55 = 100.45``). The identity holds algebraically, independently of any reading of the
screenshot, which is why the single-mark window is the right rule rather than a guess.

The guard is ``ISINSCOPE`` on the ADDRESSING columns only -- the partition columns still scope the
window as before. A calc with no addressing column keeps its previous shape byte-for-byte.
"""
import calc_to_dax as C
from calc_to_dax import translate_tableau_table_calc_to_dax

_TABLE = "Orders"
_COLS = {
    "Sales": ("Orders", "Sales", "decimal"),
    "Region": ("Orders", "Region", "string"),
    "Order Date": ("Orders", "Order_Date", "dateTime"),
}


def _resolve(cap):
    return _COLS.get(cap)


def _dax(formula, partition=(), order=("Order Date",)):
    return translate_tableau_table_calc_to_dax(formula, _resolve, partition, order)[0]


# -- the collapse itself --------------------------------------------------------------------

def test_rank_returns_one_off_the_addressing_scope():
    d = _dax("RANK(SUM([Sales]))")
    assert d.startswith("IF(ISINSCOPE('Orders'[Order_Date]), ")
    assert d.endswith(", 1)")


def test_rank_dense_collapses_to_one():
    assert _dax("RANK_DENSE(SUM([Sales]))").endswith(", 1)")


def test_rank_modified_collapses_to_one():
    assert _dax("RANK_MODIFIED(SUM([Sales]))").endswith(
        "RETURN IF(ISINSCOPE('Orders'[Order_Date]), _rank, 1)")


def test_rank_percentile_collapses_to_zero():
    """A one-mark window is the degenerate N == 1 case, which the percentile defines as 0.0."""
    assert _dax("RANK_PERCENTILE(SUM([Sales]))").endswith(
        "COUNTROWS(_rel) - 1, 0), 0)")


# -- the guard is on ADDRESSING, not partition ----------------------------------------------

def test_partition_column_is_not_part_of_the_guard():
    d = _dax("RANK(SUM([Sales]))", partition=("Region",))
    assert "ISINSCOPE('Orders'[Order_Date])" in d
    assert "ISINSCOPE('Orders'[Region])" not in d
    # the partition still scopes the window exactly as before
    assert "'Orders'[Region] = SELECTEDVALUE('Orders'[Region])" in d


def test_multiple_addressing_columns_all_have_to_be_in_scope():
    d = _dax("RANK(SUM([Sales]))", order=("Region", "Order Date"))
    assert "ISINSCOPE('Orders'[Region]) && ISINSCOPE('Orders'[Order_Date])" in d


def test_addressing_fully_covered_by_the_partition_keeps_the_unguarded_shape():
    """Nothing left to collapse along -- the previous emit is preserved byte-for-byte."""
    d = _dax("RANK(SUM([Sales]))", partition=("Region",), order=("Region",))
    assert "ISINSCOPE" not in d
    assert d.startswith("RANKX(")


def test_a_table_calc_with_no_order_by_still_declines():
    """Unchanged guard: without an explicit addressing spec there is no window to evaluate in."""
    dax, status, _ = translate_tableau_table_calc_to_dax(
        "RANK(SUM([Sales]))", _resolve, (), ())
    assert dax is None
    assert "order-by" in status


# -- neighbours that must NOT change ---------------------------------------------------------

def test_total_is_not_guarded():
    """TOTAL re-aggregates; on a total row that re-aggregation IS the single-mark window."""
    d = _dax("TOTAL(SUM([Sales]))")
    assert "ISINSCOPE" not in d
    assert d.startswith("CALCULATE(")


def test_running_sum_is_untouched():
    d = _dax("RUNNING_SUM(SUM([Sales]))")
    assert "ISINSCOPE" not in d


def test_window_sum_is_untouched():
    d = _dax("WINDOW_SUM(SUM([Sales]))")
    assert "ISINSCOPE" not in d


# -- the composite identity the customer render proved ---------------------------------------

def test_composite_of_three_ranks_collapses_to_the_source_total():
    """101 - (.15 + .20 + .20) * 1 = 100.45 -- reachable only when each rank collapses to 1."""
    d = _dax("101 - (RANK(SUM([Sales])) * 0.15 + RANK(SUM([Sales])) * 0.20 "
             "+ RANK(SUM([Sales])) * 0.20)")
    assert d.count("ISINSCOPE('Orders'[Order_Date])") == 3
    assert d.count(", 1)") == 3
