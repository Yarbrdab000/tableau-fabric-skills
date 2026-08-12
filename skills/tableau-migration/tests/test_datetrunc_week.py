"""``DATETRUNC('week', ...)`` is a date bin Tableau writes for you, so it cannot be left untranslated.

Tableau's date-bin builder emits exactly this shape when an author drags a date onto a shelf and
picks a *Week numbers* bin -- the column is stamped ``user:agg-type='Week-Trunc'`` and its formula is
``DATE(DATETRUNC('week', [SomeDate]))``. So this is not an exotic function a power user typed; it is
what the product generates from a two-click UI gesture.

Refusing it stubbed the whole column to ``BLANK()``, and the damage did not stop there. On an
ATTI/ATTR technician-hierarchy dashboard that column was one branch of a Daily / Weekly / Monthly
field parameter, and a branch pointing at a blank column is dropped -- so the reader's date selector
silently offered only Daily and Monthly. A refused calc is rarely contained to its own cell.

Tableau truncates to the START of the week and its default start day is SUNDAY (Analysis > Date
Properties > Week start day, unless the author changed it). DAX has no ``DATETRUNC``, so the offset
is subtracted directly: ``WEEKDAY(d, 1)`` numbers Sunday=1..Saturday=7, hence
``d - (WEEKDAY(d, 1) - 1)`` lands on that week's Sunday. The subtraction also drops any time
component, which is what ``DATETRUNC`` does.

``quarter`` is still refused -- it needs fiscal-year-aware arithmetic this does not attempt, and a
wrong quarter boundary is worse than an honest fallback.
"""
import pytest

from test_calc_to_dax import _col, _tx


def test_datetrunc_week_translates_to_the_sunday_offset():
    dax = _col("DATETRUNC('week', [Order Date])")
    assert "WEEKDAY('Orders'[Order_Date], 1)" in dax
    assert "- 1" in dax


def test_the_tableau_date_bin_shape_translates_end_to_end():
    """The exact formula Tableau's date-bin builder writes for a 'Week numbers' bin."""
    dax = _col("DATE(DATETRUNC('week', [Order Date]))")
    assert dax is not None
    assert "BLANK()" not in dax
    assert "WEEKDAY" in dax


def test_week_truncation_is_not_confused_with_month():
    """Month truncation keeps its own (unchanged) DATE(YEAR, MONTH, 1) form."""
    month = _col("DATETRUNC('month', [Order Date])")
    assert "WEEKDAY" not in month


@pytest.mark.parametrize("part", ["day", "month", "year"])
def test_the_other_supported_parts_are_untouched(part):
    assert _col("DATETRUNC('%s', [Order Date])" % part) is not None


def test_quarter_still_falls_back():
    """Deliberate: a quarter boundary is fiscal-calendar dependent, so guessing it is worse."""
    assert _tx("DATETRUNC('quarter', MIN([Order Date]))") is None
