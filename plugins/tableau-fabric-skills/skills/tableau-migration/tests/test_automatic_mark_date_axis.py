"""An Automatic mark over a DISCRETE date is a LINE, not a stacked column (#184).

Tableau's Automatic mark picks Line on DATE-NESS, not on continuity. From *Change the Type of Marks
in the View* (help.tableau.com/current/pro/desktop/en-us/viewparts_marks_marktypes.htm), stated
twice and qualified on neither discrete nor continuous:

    "The Line mark type is selected when there is A DATE FIELD and a measure as the inner fields on
     the Rows and Columns shelves."

    "[Bar] ... IF THE DIMENSION IS A DATE DIMENSION, THE LINE MARK IS USED INSTEAD."

The old predicate asked ``_has_continuous_date`` (a ``*-Trunc`` derivation), so a discrete date PART
fell through to bars. WHY THAT IS WORSE THAN A WRONG GLYPH: when the worksheet also carries a colour
dimension the emitted ``columnChart`` is Power BI's STACKED column, so the rebuild SUMS the series.
Reported upstream as five airlines at 95/92/88/97/90 % availability stacked into one ~462 % bar.

And it was SILENT: a stacked column is structurally valid PBIR, so nothing downstream could catch
it -- ``tier='rebuilt'``, ``reason=''``, ``remediation_worklist.items: 0``.

Corpus population, measured across 34 workbooks: 6 worksheets carry Automatic + a discrete date.
THREE are charts and flip to lineChart; three (`0073`) route to table/matrix/unsupported before this
branch and are correctly unaffected. One of the three -- `0066_bump_chart` -- carries a colour
dimension, i.e. it is the SUMMING case, and a bump chart is drawn with lines in Tableau anyway.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import twb_to_pbir as T  # noqa: E402


def _d(deriv=None, datatype=None):
    return {"derivation": deriv, "datatype": datatype}


MEAS = [{"caption": "Availability"}]


# ----------------------------------------------------------------- the reader

def test_a_discrete_date_part_is_a_date_dimension():
    for part in ("Year", "Quarter", "Month", "Weekday", "MonthYear", "ISO-Week"):
        assert T._has_date_dimension([_d(part)]), part
        # ... and is NOT continuous. Both readers must keep their own meaning.
        assert not T._has_continuous_date([_d(part)]), part


def test_an_exact_date_value_is_a_date_dimension():
    """`MDY` is the clearest case: not a numeric part at all, but the date rendered as discrete
    members. The engine's own comment calls it "an ORDINARY date column -- the same underlying date
    as a continuous exact-date pill, only rendered as discrete members"."""
    for deriv in sorted(T._DATE_EXACT_DERIVATIONS):
        assert T._has_date_dimension([_d(deriv)]), deriv


def test_a_continuous_trunc_date_still_counts():
    assert T._has_date_dimension([_d("Month-Trunc")])
    assert T._has_continuous_date([_d("Month-Trunc")])


def test_a_raw_date_column_with_no_derivation_counts():
    """Measured: 14 date-typed dimension pills in the corpus carry ``derivation=None``. A
    derivation-only predicate would miss the plainest date dimension there is."""
    assert T._has_date_dimension([_d(None, "date")])
    assert T._has_date_dimension([_d("", "datetime")])


def test_a_non_date_pill_is_not_a_date_dimension():
    """Fail-closed. Widening this predicate wrongly would turn ordinary bar charts into lines."""
    assert not T._has_date_dimension([_d(None, "string")])
    assert not T._has_date_dimension([_d("Sum", "integer")])
    assert not T._has_date_dimension([_d("Attribute", "string")])
    assert not T._has_date_dimension([])
    # a DERIVED pill on a date column is an aggregate, not a date axis
    assert not T._has_date_dimension([_d("Min", "date")])
    assert not T._has_date_dimension([_d("Max", "datetime")])


# ----------------------------------------------------------------- the routing

def test_automatic_over_a_discrete_date_is_a_LINE():
    vt = T._visual_type("automatic", dims_rows=[], dims_cols=[_d("Year")],
                        meas_rows=MEAS, meas_cols=[])
    assert vt == T.VT_LINE


def test_automatic_over_a_continuous_date_is_still_a_LINE():
    """The pre-existing behaviour, pinned so the widening cannot regress it."""
    vt = T._visual_type("automatic", dims_rows=[], dims_cols=[_d("Month-Trunc")],
                        meas_rows=MEAS, meas_cols=[])
    assert vt == T.VT_LINE


def test_an_EXPLICIT_bar_mark_over_a_discrete_date_stays_a_COLUMN():
    """The load-bearing carve-out. Tableau stacks bars by default, so an explicit ``bar`` mark over
    a date IS faithfully a column chart -- only the AUTOMATIC case was ever wrong. Widening this to
    every bar mark would break correct rebuilds."""
    vt = T._visual_type("bar", dims_rows=[], dims_cols=[_d("Year")],
                        meas_rows=MEAS, meas_cols=[])
    assert vt == T.VT_COLUMN


def test_automatic_over_a_NON_date_dimension_is_still_a_COLUMN():
    vt = T._visual_type("automatic", dims_rows=[], dims_cols=[_d(None, "string")],
                        meas_rows=MEAS, meas_cols=[])
    assert vt == T.VT_COLUMN


def test_a_date_with_no_measure_does_not_become_a_line():
    """Tableau's rule needs BOTH a date field AND a measure as the inner fields. ``axis_meas`` is
    the gate; without it a date-only shelf must not be routed to a line."""
    vt = T._visual_type("automatic", dims_rows=[], dims_cols=[_d("Year")],
                        meas_rows=[], meas_cols=[])
    assert vt != T.VT_LINE
