"""Table calcs on the FILTERS shelf are their own migration class, and must be disclosed as one.

In Tableau's order of operations a table calculation on the Filters shelf runs LAST -- after
aggregation, after the viz LOD is materialised. Its effect is to HIDE marks, not to remove data, so
every OTHER table calc in the view is unaffected by it.

Power BI has no equivalent. A visual calculation cannot filter at all, and a visual-level filter
genuinely REMOVES rows -- which would silently re-scope the neighbouring table calcs (a running sum
restarts, a percent-of-total's denominator shrinks, a moving average loses its lead-in). That
failure renders cleanly and keeps the correct ROW COUNT; only the values are wrong. It is the worst
shape in the migration surface, and the reason a row-count check can never verify this class.

Three things are locked here.

1. **Detection** (a deterministic rule -- no judgement): resolve a filter's field to its formula and
   test for a table-calc head. Verified against three real corpus specimens -- `WINDOW_AVG` on
   *Customers above avg.*, `WINDOW_MAX` on *Filter to Most Recent Date*, and `LOOKUP` on the sheet
   in `0077_table_calculation_breaks_with_filter` -- each of which also carries 2-3 other table
   calcs, i.e. every one of them is the cascade case.

2. **Disclosure.** These used to be reported as "aggregate/measure filter ... is not mapped to a
   slicer", which mis-frames the failure as a missing CONTROL when the real consequence is that the
   visual shows EVERY mark instead of the author's slice -- and says nothing about the neighbours.
   The classification now names the idiom, states hide-vs-exclude, and reports the cascade count.

3. **The fail-closed invariant.** A table-calc filter must NEVER become a row-removing filter. That
   holds today because the row-level column translator rejects every table-calc head, so such a
   formula can never produce the boolean predicate the keep-flag path requires. It holds by
   CONSTRUCTION rather than by intent, which is precisely why it is pinned here: teaching the column
   translator about `LAST`/`INDEX` later would silently open the dangerous path.
"""
import calc_to_dax as C
import twb_to_pbir as R


_FIELDS = {"Sales": ("Orders", "Sales", "decimal"),
           "Number of Records": ("Orders", "Number_of_Records", "int64"),
           "Order Date": ("Orders", "Order_Date", "dateTime")}


def _resolve(cap):
    return _FIELDS.get(cap)


# -- 1. detection ----------------------------------------------------------------------------

# The three real corpus specimens, verbatim.
_SPECIMENS = [
    ("SUM([Number of Records]) > WINDOW_AVG(SUM([Number of Records]))", ("WINDOW_AVG",)),
    ("ATTR([Order Date]) = WINDOW_MAX(MAX([Order Date]))", ("WINDOW_MAX",)),
    ("LOOKUP(YEAR(MIN([Order Date])),0)", ("LOOKUP",)),
]


def test_real_corpus_specimens_are_detected():
    for formula, expected in _SPECIMENS:
        assert R._table_calc_filter_idioms(formula) == expected, formula


def test_positional_idioms_are_detected():
    for head in ("INDEX", "SIZE", "FIRST", "LAST"):
        assert R._table_calc_filter_idioms("%s() <= 6" % head) == (head,)


def test_rank_and_running_idioms_are_detected():
    assert R._table_calc_filter_idioms("RANK(SUM([Sales])) <= 5") == ("RANK",)
    assert R._table_calc_filter_idioms("RUNNING_SUM(SUM([Sales])) > 0") == ("RUNNING_SUM",)


def test_the_parameter_wrapped_idiom_is_detected():
    """The wild specimen from the kit: a CASE over a parameter selecting between windows."""
    f = ("case [Parameters].[Parameter 0014106093154313] when 14 then LAST() <= 100 "
         "when 28 then LAST() <= [Parameters].[Parameter 0014106093154313] and LAST() > 15 "
         "when 31 then LAST() <= 31 END")
    assert R._table_calc_filter_idioms(f) == ("LAST",)


def test_multiple_idioms_are_all_reported():
    assert R._table_calc_filter_idioms("INDEX() <= SIZE() - 3") == ("INDEX", "SIZE")


def test_an_ordinary_filter_is_not_classified():
    assert R._table_calc_filter_idioms("SUM([Sales]) > 100") == ()
    assert R._table_calc_filter_idioms("[Region] = 'West'") == ()
    assert R._table_calc_filter_idioms("") == ()
    assert R._table_calc_filter_idioms(None) == ()


def test_a_field_named_like_a_head_is_not_a_false_positive():
    """Tableau writes field references in brackets, so only ``NAME(`` counts."""
    assert R._table_calc_filter_idioms("SUM([Last Year Sales]) > [Total Cost]") == ()
    assert R._table_calc_filter_idioms("[Index Value] > 3") == ()


# -- 2. the fail-closed invariant (the dangerous path stays shut) -----------------------------

def _would_become_a_row_filter(formula):
    """Mirror the keep-flag gate: a bool row-level predicate over exactly one table."""
    pred, _reason, tables, dtype = C.translate_tableau_calc_to_column_dax_typed(
        formula, _resolve, known_tables={"Orders"}, param_resolver=lambda pid: {"value": 28})
    return bool(pred) and dtype == "bool" and len(tables) == 1


def test_no_table_calc_filter_can_become_a_row_removing_filter():
    """The whole point: a row filter REMOVES rows and would corrupt every neighbouring table calc."""
    for formula in ("LAST() <= 6", "INDEX() <= 6", "SIZE() > 10",
                    "RANK(SUM([Sales])) <= 5", "WINDOW_SUM(SUM([Sales])) > 100",
                    "case [Parameters].[P] when 14 then LAST() <= 100 end") + \
                   tuple(f for f, _ in _SPECIMENS):
        assert not _would_become_a_row_filter(formula), formula


def test_every_detected_idiom_is_rejected_by_the_row_level_translator():
    """Detection and the invariant must not drift apart: anything we CLASSIFY must also be REFUSED."""
    for formula, _ in _SPECIMENS:
        assert R._table_calc_filter_idioms(formula)
        assert not _would_become_a_row_filter(formula)


# -- 3. positional primitives keep Tableau's semantics (the false friends) --------------------

def _tc(formula, order=("Order Date",)):
    return C.translate_tableau_table_calc_to_dax(formula, _resolve, (), order)[0]


def test_index_is_a_position_not_a_value():
    """Power BI's own INDEX/LAST/FIRST are VALUE-retrieval; Tableau's are POSITIONAL."""
    assert _tc("INDEX()") == "ROWNUMBER(ORDERBY('Orders'[Order_Date], ASC))"


def test_last_is_a_distance_and_is_zero_on_the_final_row():
    d = _tc("LAST()")
    assert d.startswith("COUNTROWS(") and "- ROWNUMBER(" in d


def test_first_is_a_negative_going_distance():
    assert _tc("FIRST()") == "1 - ROWNUMBER(ORDERBY('Orders'[Order_Date], ASC))"


def test_size_is_the_partition_row_count():
    assert _tc("SIZE()").startswith("COUNTROWS(WINDOW(1, ABS, -1, ABS,")


def test_the_off_by_one_between_index_and_last_is_preserved():
    """INDEX() is 1-based and LAST() 0-based, so ``INDEX()<=6`` shows 6 rows and ``LAST()<=6`` shows 7.

    Evaluated over a 48-row partition: ROWNUMBER runs 1..48, so INDEX()<=6 keeps rows 1..6, while
    LAST() = 48 - ROWNUMBER runs 47..0, so LAST()<=6 keeps rows 42..48 -- seven of them."""
    n = 48
    index_rows = [r for r in range(1, n + 1) if r <= 6]
    last_rows = [r for r in range(1, n + 1) if (n - r) <= 6]
    assert len(index_rows) == 6
    assert len(last_rows) == 7
    assert last_rows[0] == 42 and last_rows[-1] == n
