"""Several date PARTS of one date on a shared axis collapse in Power BI and sum across the levels.

Tableau draws ONE MARK PER COMBINATION when two dimensions share an axis, so
``cols=([yr:Order Date:ok] / [mn:Order Date:ok])`` is a nested Year/Month axis with one bar per
month-in-year. Power BI renders a multi-field ``Category`` role as a DRILL HIERARCHY showing only
its top level, so every Year bar silently SUMS its twelve months.

Measured on ``0070_new_max``'s own source data (9,426 dated rows):

    collapsed Date[Year] only          4 marks   <- what shipped
    Tableau (Year x Month combos)     48 marks   <- the authored grain
    Date[Month Start]                 48 marks   <- what this binds
    leaf-only Date[Month]             12 marks   <- pools 4 years into each bar

and the worked rollup: the 2013 bar reads 2,852,360 where Tableau draws twelve bars
(215,229 / 149,129 / 171,791 / 143,739 / ...) summing to exactly that.

**Arithmetic, not geometry.** Nothing is mis-resolved -- both projections are valid, correctly
bound, validate clean and render a full plausible chart, which is why no static gate sees it. What
is wrong is how many marks Power BI CHOOSES TO DRAW.

The repair reuses the binding the continuous-truncation path already emits rather than inventing
one: a (Year, Month) pair and ``Date[Month Start]`` are in EXACT BIJECTION, so one scalar column
carries the identical grain in a single projection that has no hierarchy left to collapse -- and it
sorts, where ``Date[Month]`` is the text "Jan".

**The decline cases below are the substance of this file, not its edge cases.** The obvious fix --
bind the leaf only -- is right for a taxonomy with a unique leaf and CATASTROPHIC for date parts:
months would pool across years and render as a perfectly plausible SEASONAL chart, a worse wrong
number than the rollup it replaced, because a reader cannot see it. So every shape that cannot be
proved equivalent must be left exactly as it is, and each of those boundaries is asserted here.
"""
import twb_to_pbir as T


def _part(part, *, field_id="[Order Date]", ds="Sample", entity="Date", caption=None):
    """A date pill AS IT LOOKS AFTER ``_rebind_date_axis`` has redirected it to the calendar."""
    return {
        "caption": caption or f"{part} of Order Date",
        "field_id": field_id,
        "instance": f"[{part[:2].lower()}:Order Date:ok]",
        "datasource": ds,
        "role": "dimension",
        "datatype": "date",
        "is_calc": False,
        "derivation": part,
        "aggregation": None,
        "entity": entity,
        "property": part,
        "binding": "column",
        "kind": "category",
        "date_rebound": True,
        "date_part": part,
        "date_key_column": "Date",
    }


def _plain(name, entity="Orders$"):
    """An ordinary (non-date) category pill -- a taxonomy level."""
    return {
        "caption": name, "field_id": f"[{name}]", "instance": f"[none:{name}:nk]",
        "datasource": "Sample", "role": "dimension", "datatype": "string",
        "is_calc": False, "derivation": "None", "aggregation": None,
        "entity": entity, "property": name, "binding": "column", "kind": "category",
    }


# --------------------------------------------------------------------------- it fires

def test_year_and_month_fold_onto_the_month_start_grain_column():
    """The corpus case: 0070 / 0077 / 0134, six visuals across three workbooks."""
    out = T._collapse_date_part_axis([_part("Year"), _part("Month")])
    assert len(out) == 1, "a single projection is the whole point -- two collapse"
    assert out[0]["property"] == "Month Start"
    assert out[0]["entity"] == "Date"
    assert out[0]["date_part_axis_collapsed"] is True


def test_the_fold_is_order_insensitive():
    """0134 carries Month on ROWS and Year on COLS, so the merged list arrives Month-first."""
    out = T._collapse_date_part_axis([_part("Month"), _part("Year")])
    assert len(out) == 1 and out[0]["property"] == "Month Start"


def test_year_and_quarter_fold_onto_quarter_start():
    out = T._collapse_date_part_axis([_part("Year"), _part("Quarter")])
    assert len(out) == 1 and out[0]["property"] == "Quarter Start"


def test_year_quarter_and_month_fold_onto_the_finest_grain():
    """Quarter is redundant once Month is present; the finest part decides."""
    out = T._collapse_date_part_axis(
        [_part("Year"), _part("Quarter"), _part("Month")])
    assert len(out) == 1 and out[0]["property"] == "Month Start"


def test_day_grain_folds_onto_the_marked_key_column():
    """``_DATE_TRUNC_SCALAR_COLUMNS['Day']`` is None -- day grain IS the calendar key."""
    out = T._collapse_date_part_axis(
        [_part("Year"), _part("Month"), _part("Day")])
    assert len(out) == 1 and out[0]["property"] == "Date"


def test_the_collapsed_field_carries_a_truncation_derivation():
    """It must look like the truncation pill it is now equivalent to, not like a part."""
    out = T._collapse_date_part_axis([_part("Year"), _part("Month")])
    assert out[0]["derivation"] == "Month-Trunc"
    assert not out[0]["date_part"], "a folded field is no longer a part"


def test_the_fold_is_disclosed_to_the_reader():
    """Silent grain changes are how this class of defect survives; say it in the report."""
    warnings = []
    T._collapse_date_part_axis([_part("Year"), _part("Month")], warnings, "Challenge")
    assert len(warnings) == 1
    text = str(warnings[0])
    assert "Month Start" in text
    assert "Year, Month" in text


def test_the_disclosure_names_the_LABEL_change_too():
    """The numbers become right and the axis labels become DIFFERENT -- both are the reader's news.

    Tableau draws a two-tier discrete header (``2013`` over ``Jan``); a scalar date column renders
    one tier of formatted dates (``Jan 2013``). A reader comparing side by side sees that
    immediately, so it belongs on the surface they are standing at rather than only in a commit
    message they will never open.
    """
    warnings = []
    T._collapse_date_part_axis([_part("Year"), _part("Month")], warnings, "Challenge")
    text = str(warnings[0])
    assert "LABELS" in text
    assert "Jan 2013" in text


# ------------------------------------------------------------------- it declines (the substance)

def test_quarter_and_month_WITHOUT_a_year_declines():
    """The dangerous one. Every Q1/January would pool across all years and look legitimate."""
    fields = [_part("Quarter"), _part("Month")]
    assert T._collapse_date_part_axis(fields) is fields


def test_year_and_day_declines_because_it_names_no_period():
    """(year, day-of-month) is not a grain any calendar column carries."""
    fields = [_part("Year"), _part("Day")]
    assert T._collapse_date_part_axis(fields) is fields


def test_two_DIFFERENT_dates_decline():
    """``YEAR(Order Date)`` x ``MONTH(Ship Date)`` is a real cross-product; no bijection."""
    fields = [_part("Year"), _part("Month", field_id="[Ship Date]")]
    assert T._collapse_date_part_axis(fields) is fields


def test_the_same_column_name_in_two_ISLANDS_declines():
    """Per-island models give each datasource its own calendar; folding across them is wrong."""
    fields = [_part("Year"), _part("Month", ds="Intake", entity="Date (Intake)")]
    assert T._collapse_date_part_axis(fields) is fields


def test_a_nested_TAXONOMY_is_untouched():
    """Product_Category / Product_Sub-Category has no period grain to fold onto (#191, 9 visuals)."""
    fields = [_plain("Product_Category"), _plain("Product_Sub-Category")]
    assert T._collapse_date_part_axis(fields) is fields


def test_a_high_cardinality_leaf_is_untouched():
    """0065's Region / Order_ID -- a leaf-only rule here would emit 5,111 bars."""
    fields = [_plain("Region"), _plain("Order_ID")]
    assert T._collapse_date_part_axis(fields) is fields


def test_a_date_part_mixed_with_a_plain_dimension_declines():
    """Year x Region is a genuine two-dimension axis; folding only the date changes the grain."""
    fields = [_part("Year"), _part("Month"), _plain("Region")]
    assert T._collapse_date_part_axis(fields) is fields


def test_an_UNBOUND_date_part_declines():
    """The model-unbound first pass never rebinds, so there is no calendar to fold onto.

    This is why the six ``Year``/``Month`` cases in #191 appear in the ``pbip`` tree only: the
    first pass degrades both parts to the fact's own raw date column, where no grain column
    exists. ``date_part`` is stamped at the rebind site precisely so the two passes differ here.
    """
    a, b = _part("Year"), _part("Month")
    for f in (a, b):
        f.pop("date_part")
        f["date_rebound"] = False
    fields = [a, b]
    assert T._collapse_date_part_axis(fields) is fields


def test_a_single_part_is_untouched():
    """One field is not a hierarchy; there is nothing to collapse."""
    fields = [_part("Month")]
    assert T._collapse_date_part_axis(fields) is fields


def test_an_empty_axis_is_untouched():
    assert T._collapse_date_part_axis([]) == []


# ----------------------------------------------------------------- the fold is actually WIRED UP

def test_the_bar_and_line_branches_both_call_the_fold():
    """A gate that never executes passes every test it has, forever, silently.

    ``_collapse_date_part_axis`` is correct in isolation and worthless unless
    ``_build_query_state`` invokes it, so pin the call sites: the population splits across
    ``clusteredBarChart`` / ``clusteredColumnChart`` (0060, 0065, 0070, 0134) and ``lineChart``
    (0077), and a fold wired into only one of them would leave 0077 silently collapsed.

    Deliberately brittle -- it reads the source. If a refactor moves these calls, this test is
    meant to fail loudly rather than let the fold go quiet.
    """
    import inspect
    src = inspect.getsource(T._build_query_state)
    calls = [ln.strip() for ln in src.splitlines()
             if "_collapse_date_part_axis(" in ln]
    assert len(calls) == 2, f"expected a fold in both the bar and line branches, found {calls}"
    for ln in calls:
        # the ARGUMENT matters, not just the call: folding a pre-filtered list would run clean
        # and change nothing.
        assert ln.startswith("cat = _collapse_date_part_axis(cat"), ln
