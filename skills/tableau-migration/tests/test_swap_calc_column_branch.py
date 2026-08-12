"""A field-swap branch may point at a CALCULATED column, not just a physical one.

Power BI field parameters are built from a Tableau swap calc -- ``IF [Param] = 'Daily' THEN [a]
ELSEIF [Param] = 'Weekly' THEN [b] ELSEIF [Param] = 'Monthly' THEN [c] END`` -- by resolving each
branch's field to its landed model home so a ``NAMEOF`` target can be emitted. A branch that does
not resolve is dropped fail-closed, which is the right instinct: a ``NAMEOF`` pointing at nothing
breaks the model.

But the swap is assembled BEFORE the calcs are translated -- the build has to reserve every name up
front so emitted objects cannot collide -- so at that moment the field resolver knows only the
PHYSICAL columns. Any branch naming a calculated column therefore resolved to nothing and was
dropped, and because dropping is silent the selector simply came out shorter than the author wrote.

Measured on an ATTI/ATTR technician-hierarchy dashboard whose ``Choose Date`` swap offers
Daily / Weekly / Monthly:

    ("'Daily'",   NAMEOF('Sheet1'[completedatedt]), 0),
    ("'Monthly'", NAMEOF('Sheet1'[FiscalMonth]),    1)

``completedatedt`` and ``FiscalMonth`` are physical and survived; ``Complete Date (Week numbers)``
is a calculated column (Tableau's week date-bin, ``DATE(DATETRUNC('week', [completedatedt]))``) and
vanished, so the reader could pick Daily or Monthly but never Weekly.

The fix does not reorder the build -- that ordering exists for a reason. A calc column's NAME and
HOME TABLE are both knowable up front even though its DAX is not yet translated, and a ``NAMEOF``
target needs nothing more, so the planned homes are handed to the locator as a fallback consulted
only when the physical resolver comes back empty. A calc whose own field references span several
tables (or none) is omitted rather than guessed: a ``NAMEOF`` aimed at the wrong table is worse than
a dropped branch.
"""
import parameters as P


def _resolve_physical(name):
    """Stands in for the real M resolver: physical columns only, exactly as at swap-build time."""
    phys = {"completedatedt": ("Sheet1", "completedatedt", "dateTime"),
            "fiscalmonth": ("Sheet1", "FiscalMonth", "dateTime")}
    return phys.get((name or "").strip().lower())


def test_a_physical_branch_still_resolves():
    loc = P.field_locator_from_resolver(_resolve_physical)
    assert loc("completedatedt") == ("Sheet1", "completedatedt", False)


def test_a_calculated_column_branch_resolves_to_its_home_table():
    loc = P.field_locator_from_resolver(
        _resolve_physical,
        calc_column_homes={"Complete Date (Week numbers)": "Sheet1"})
    assert loc("Complete Date (Week numbers)") == (
        "Sheet1", "Complete Date (Week numbers)", False)


def test_the_calc_fallback_never_shadows_a_physical_column():
    """The physical resolver wins; the calc map is consulted only when it comes back empty."""
    loc = P.field_locator_from_resolver(
        _resolve_physical, calc_column_homes={"completedatedt": "WrongTable"})
    assert loc("completedatedt") == ("Sheet1", "completedatedt", False)


def test_a_measure_still_wins_over_both():
    loc = P.field_locator_from_resolver(
        _resolve_physical, measure_names=["Sales"],
        calc_column_homes={"Sales": "Sheet1"})
    assert loc("Sales") == (None, "Sales", True)


def test_an_unknown_field_still_fails_closed():
    loc = P.field_locator_from_resolver(_resolve_physical, calc_column_homes={"X": "Sheet1"})
    assert loc("nothing at all") is None


def test_matching_is_case_insensitive_like_the_measure_path():
    loc = P.field_locator_from_resolver(
        _resolve_physical, calc_column_homes={"Complete Date (Week numbers)": "Sheet1"})
    assert loc("complete date (WEEK NUMBERS)") == (
        "Sheet1", "Complete Date (Week numbers)", False)


def test_no_calc_homes_is_byte_identical_to_before():
    """Every workbook without a calc-column swap branch must behave exactly as it always did."""
    a = P.field_locator_from_resolver(_resolve_physical, measure_names=["Sales"])
    b = P.field_locator_from_resolver(_resolve_physical, measure_names=["Sales"],
                                      calc_column_homes={})
    for probe in ("completedatedt", "FiscalMonth", "Sales", "missing"):
        assert a(probe) == b(probe)
