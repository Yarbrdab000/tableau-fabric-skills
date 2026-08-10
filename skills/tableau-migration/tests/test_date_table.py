"""Date-dimension generation + auto-wiring.

Three layers are exercised here, matching how the feature is built:

* the TMDL generator (``tmdl_generate.generate_date_table_tmdl``) renders a marked CALENDARAUTO
  calendar with the Year->Quarter->Month->Week->Day hierarchy;
* the assembler helpers (``_select_primary_date`` / ``_build_date_dimension``) pick the active
  (business) date and only relate fact-like tables;
* the convenience entry point (``migrate_tds_to_semantic_model``) wires the calendar into the
  emitted bundle by default, with ``date_table=False`` as a clean opt-out.
"""
import pytest

import tmdl_generate as T
from assemble_model import (
    _build_date_dimension,
    _select_primary_date,
    migrate_tds_to_semantic_model,
)
from test_connection_to_m import LIVE_SQLSERVER


# A single-DB (DirectQuery) datasource whose Orders table carries TWO date columns, so the
# active/inactive (role-playing) selection is exercised end-to-end.
DATES_SQLSERVER = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Sales' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='myserver' name='sqlserver.0a1b2c'>
        <connection authentication='sqlserver' class='sqlserver' dbname='Sales'
                    server='myserver.database.windows.net' username='svc' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order Date</remote-name><local-name>[Order Date]</local-name>
        <parent-name>[Orders]</parent-name><local-type>datetime</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Ship Date</remote-name><local-name>[Ship Date]</local-name>
        <parent-name>[Orders]</parent-name><local-type>datetime</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


# =============================================================================
# Generator: the rendered TMDL of a marked CALENDARAUTO calendar.
# =============================================================================
def test_date_table_tmdl_structure():
    tmdl = T.generate_date_table_tmdl("Date")

    # marked as a date table: table-level dataCategory + a single dateTime key column
    assert "table Date" in tmdl
    assert "dataCategory: Time" in tmdl
    assert "column Date" in tmdl
    assert "dataType: dateTime" in tmdl
    assert "isKey" in tmdl
    assert "isNameInferred" in tmdl
    assert "sourceColumn: [Date]" in tmdl

    # derived calendar parts all present and DAX references the (single-quoted) table name
    assert "column Year = YEAR('Date'[Date])" in tmdl
    assert "column Quarter = \"Q\" & QUARTER('Date'[Date])" in tmdl
    assert "column Month = FORMAT('Date'[Date], \"MMM\")" in tmdl
    assert "column 'Week of Month'" in tmdl
    assert "column 'Week of Year' = WEEKNUM('Date'[Date], 21)" in tmdl
    assert "column Day = DAY('Date'[Date])" in tmdl

    # ISO attributes that back ISOWEEKDAY / DATENAME('weekday') / ISOYEAR date-attribute calcs.
    assert "column 'Weekday No' = WEEKDAY('Date'[Date], 2)" in tmdl
    assert "column 'Day Name' = FORMAT('Date'[Date], \"dddd\")" in tmdl
    assert "column 'ISO Year' = YEAR('Date'[Date] + 4 - WEEKDAY('Date'[Date], 2))" in tmdl
    # Day Name orders by the (hidden) ISO weekday number, not alphabetically.
    assert "sortByColumn: 'Weekday No'" in tmdl

    # Month/Quarter sort by their numeric helper so they don't order alphabetically
    assert "sortByColumn: 'Quarter No'" in tmdl
    assert "sortByColumn: 'Month No'" in tmdl

    # the drill hierarchy + the calculated CALENDARAUTO partition
    assert "hierarchy Calendar" in tmdl
    for level in ("level Year", "level Quarter", "level Month", "level Week", "level Day"):
        assert level in tmdl
    assert "partition Date = calculated" in tmdl
    assert "mode: import" in tmdl
    assert "source = CALENDARAUTO()" in tmdl


def test_date_table_carries_a_scalar_column_for_every_continuous_truncation_grain():
    # Tableau's green `t*:` pill is DATETRUNC: one DATE VALUE per period, drawn on a CONTINUOUS axis.
    # Power BI can only express that as a scalar date column -- a Calendar drill level is categorical
    # by construction, so an axis bound to the hierarchy gets cross-product category slots, refuses a
    # Scalar axis, and pages the surplus behind a scrollbar (measured: half a 45-month series hidden).
    # These columns are the truncation's honest landing spot, one per grain the binder supports.
    tmdl = T.generate_date_table_tmdl("Date")
    assert "column 'Year Start' = DATE(YEAR('Date'[Date]), 1, 1)" in tmdl
    assert ("column 'Quarter Start' = "
            "DATE(YEAR('Date'[Date]), (QUARTER('Date'[Date]) - 1) * 3 + 1, 1)") in tmdl
    assert "column 'Month Start' = DATE(YEAR('Date'[Date]), MONTH('Date'[Date]), 1)" in tmdl
    assert "column 'Week Start' = 'Date'[Date] - WEEKDAY('Date'[Date], 2) + 1" in tmdl
    # Each is an explicitly typed, date-formatted column: an INFERRED type would let Desktop treat it
    # as text, and the Scalar axis that depends on it would silently fall back to categorical.
    for grain in ("Year Start", "Quarter Start", "Month Start", "Week Start"):
        body = tmdl.split(f"column '{grain}' =", 1)[1].split("column ", 1)[0]
        assert "dataType: dateTime" in body
        assert "formatString: Short Date" in body
    # Day-Trunc needs no column of its own -- it IS the key column.
    assert "column 'Day Start'" not in tmdl


def test_date_table_tmdl_unmarked_drops_time_category_and_key():
    tmdl = T.generate_date_table_tmdl("Date", mark_as_date=False)
    assert "dataCategory: Time" not in tmdl
    assert "isKey" not in tmdl
    # it is still a real calendar with the calculated partition
    assert "source = CALENDARAUTO()" in tmdl


def test_date_table_tmdl_quotes_names_with_spaces():
    tmdl = T.generate_date_table_tmdl("Date Dimension")
    assert "table 'Date Dimension'" in tmdl
    assert "YEAR('Date Dimension'[Date])" in tmdl


# =============================================================================
# Primary-date selection (which date column gets the ACTIVE relationship).
# =============================================================================
def test_select_primary_date_single_is_always_primary():
    assert _select_primary_date(["Ship_Date"]) == "Ship_Date"


def test_select_primary_date_prefers_order_date():
    assert _select_primary_date(["Order_Date", "Ship_Date"]) == "Order_Date"


def test_select_primary_date_prefers_bare_date():
    assert _select_primary_date(["Date", "Created"]) == "Date"


def test_select_primary_date_ambiguous_returns_none():
    # two date columns, neither an unambiguous "order/date" primary -> caller emits all inactive
    assert _select_primary_date(["Ship_Date", "Delivery_Date"]) is None


# =============================================================================
# Detection + relationship building (fact-vs-dimension, naming).
# =============================================================================
def _rel(name, *date_cols, extra=()):
    cols = [{"model_name": c, "tmdl_type": "dateTime"} for c in date_cols]
    cols += [{"model_name": c, "tmdl_type": "string"} for c in extra]
    return {"name": name, "kind": "table", "columns": cols}


def test_build_date_dimension_relates_only_fact_tables():
    # People is purely the 'one' side of an existing join -> a dimension -> its date is ignored;
    # the calendar relates the fact (Orders) only.
    tables = [_rel("Orders", "Order_Date"), _rel("People", "Hire_Date")]
    rels = [{"from_table": "Orders", "from_col": "Region",
             "to_table": "People", "to_col": "Region"}]
    name, part, date_rels, report = _build_date_dimension(
        tables, ["Orders", "People"], rels)

    assert name == "Date" and part is not None
    assert {r["from_table"] for r in date_rels} == {"Orders"}
    # Plain exact dateTime join -- no joinOnDateBehavior. The generated Date table is a CALCULATED
    # CALENDARAUTO table, and Power BI Desktop drops a datePartOnly relationship that involves a
    # calculated table on .pbip open (the relationship vanishes and the time series flattens).
    assert all("join_on_date_behavior" not in r for r in date_rels)
    assert report["generated"] is True


def test_build_date_dimension_active_and_inactive():
    tables = [_rel("Orders", "Order_Date", "Ship_Date")]
    name, part, date_rels, report = _build_date_dimension(tables, ["Orders"], [])
    by_col = {r["from_col"]: r for r in date_rels}
    assert by_col["Order_Date"]["is_active"] is True
    assert by_col["Ship_Date"]["is_active"] is False
    assert not report["warnings"]


def test_build_date_dimension_directquery_omits_datepartonly():
    # On a DirectQuery model a datePartOnly (datetime-to-date) join is illegal -- Power BI refuses
    # to open the model. The calendar relationships must still be emitted (active + inactive
    # role-playing) but as plain dateTime joins, with no joinOnDateBehavior, plus a report warning.
    tables = [_rel("Orders", "Order_Date", "Ship_Date")]
    name, part, date_rels, report = _build_date_dimension(
        tables, ["Orders"], [], mode="DirectQuery")
    assert name == "Date" and part is not None
    assert {r["from_table"] for r in date_rels} == {"Orders"}
    assert all("join_on_date_behavior" not in r for r in date_rels)
    by_col = {r["from_col"]: r for r in date_rels}
    assert by_col["Order_Date"]["is_active"] is True
    assert by_col["Ship_Date"]["is_active"] is False
    assert any("DirectQuery" in w for w in report["warnings"])


def test_build_date_dimension_import_spans_only_related_fact_dates():
    # An Import model must NOT use CALENDARAUTO(): it scans EVERY dateTime column in the model, so a
    # single unrelated column (a contact birthdate) drags the calendar back decades and buries the
    # business range under tens of thousands of empty rows -- every date axis then renders its real
    # data as an unreadable sliver. The calendar spans only the fact date columns it actually relates
    # to, rounded OUT to whole years so Mark-as-Date's contiguous full-year requirement still holds.
    tables = [_rel("Orders", "Order_Date")]
    _name, part, _rels, _report = _build_date_dimension(tables, ["Orders"], [], mode="import")
    assert ("source = CALENDAR(DATE(YEAR(MIN('Orders'[Order_Date])), 1, 1), "
            "DATE(YEAR(MAX('Orders'[Order_Date])), 12, 31))") in part
    assert "CALENDARAUTO" not in part


def test_build_date_dimension_import_span_folds_every_related_date_column():
    # Several related fact dates (including a secondary/inactive one) all contribute a bound, so the
    # calendar covers every date the model can actually put on an axis -- and nothing else.
    tables = [_rel("Orders", "Order_Date"), _rel("Orders", "Ship_Date"),
              _rel("Returns", "Return_Date")]
    _name, part, rels, _report = _build_date_dimension(
        tables, ["Orders", "Returns"], [], mode="import")
    for t, c in (("Orders", "Order_Date"), ("Orders", "Ship_Date"), ("Returns", "Return_Date")):
        assert f"MIN('{t}'[{c}])" in part and f"MAX('{t}'[{c}])" in part
    # every emitted relationship's column is represented in the span (no date axis lands off-calendar)
    assert {(r["from_table"], r["from_col"]) for r in rels} == {
        ("Orders", "Order_Date"), ("Orders", "Ship_Date"), ("Returns", "Return_Date")}


def test_a_single_distinct_date_column_does_not_bound_the_calendar():
    # Issue #102 part 2. A small lookup whose date column is a PLACEHOLDER -- every row the same
    # 1/1/02 or 1900-01-01, a very common Excel/CSV idiom -- is still a related fact date column, so
    # it set the calendar's lower bound. Measured on a Superstore rebuild: a 41-row commission
    # lookup with ONE distinct date stretched Date to 2002-01-01..2027-12-31 (~9,496 rows) for data
    # spanning 2021-2024, putting ~21 empty years in every Year/Quarter slicer.
    #
    # The test is structural rather than a guess about which dates "look like" sentinels: a column
    # whose MIN equals its MAX has one distinct value and cannot bound a range. Verified in live DAX
    # against a real model -- unguarded MIN over a sentinel returned 2002-01-01, the guarded form
    # returned 2017-01-07 (the real data's start), and an all-degenerate span fell back rather than
    # producing an empty calendar.
    tables = [_rel("Orders", "Order_Date"), _rel("Commission", "Order_Date")]
    _name, part, _rels, _report = _build_date_dimension(
        tables, ["Orders", "Commission"], [], mode="import")
    # each contributing column is guarded by its own MIN<>MAX test ...
    for t in ("Orders", "Commission"):
        assert (f"IF(MIN('{t}'[Order_Date]) <> MAX('{t}'[Order_Date]), "
                f"MIN('{t}'[Order_Date]))") in part
        assert (f"IF(MIN('{t}'[Order_Date]) <> MAX('{t}'[Order_Date]), "
                f"MAX('{t}'[Order_Date]))") in part
    # ... and the guarded bound falls back to the plain fold, so the calendar is never empty
    assert "COALESCE(MINX({" in part and "COALESCE(MAXX({" in part
    assert "}, [Value])" in part
    assert "CALENDARAUTO" not in part


def test_a_lone_date_column_is_not_guarded_because_it_is_the_only_bound():
    # A single contributing column cannot be excluded -- it is the only bound there is -- so the
    # guard is skipped entirely. This also keeps every single-fact model byte-for-byte unchanged,
    # which is what confines the blast radius of the fix above to models that actually have a
    # second date column to fall back on.
    tables = [_rel("Orders", "Order_Date")]
    _name, part, _rels, _report = _build_date_dimension(tables, ["Orders"], [], mode="import")
    assert ("source = CALENDAR(DATE(YEAR(MIN('Orders'[Order_Date])), 1, 1), "
            "DATE(YEAR(MAX('Orders'[Order_Date])), 12, 31))") in part
    assert "COALESCE" not in part
    assert "MINX(" not in part


def test_build_date_dimension_directquery_uses_fixed_range_calendar():
    # CALENDARAUTO() on a DirectQuery model has to query the source to find its span and fails to
    # process without it (the user's "date table isn't working"). A self-contained fixed-range
    # CALENDAR(...) is emitted instead, with a warning explaining how to fit it to the data.
    tables = [_rel("Orders", "Order_Date")]
    _name, part, _rels, report = _build_date_dimension(
        tables, ["Orders"], [], mode="DirectQuery")
    assert "source = CALENDAR(DATE(2015, 1, 1), DATE(2035, 12, 31))" in part
    assert "CALENDARAUTO" not in part
    assert any("fixed-range" in w and "CALENDARAUTO" in w for w in report["warnings"])


def test_build_date_dimension_directquery_honors_custom_range():
    tables = [_rel("Orders", "Order_Date")]
    _name, part, _rels, _report = _build_date_dimension(
        tables, ["Orders"], [], mode="DirectQuery", date_range=(2020, 2024))
    assert "source = CALENDAR(DATE(2020, 1, 1), DATE(2024, 12, 31))" in part


def test_build_date_dimension_dedupes_table_name():
    # a (degenerate) source table literally named 'Date' forces the calendar to a free name
    tables = [_rel("Date", "Event_Date")]
    name, part, date_rels, report = _build_date_dimension(tables, ["Date"], [])
    assert name == "Date Dimension"
    assert all(r["to_table"] == "Date Dimension" for r in date_rels)


def test_build_date_dimension_no_date_columns():
    tables = [_rel("Orders", extra=["Region", "Category"])]
    name, part, date_rels, report = _build_date_dimension(tables, ["Orders"], [])
    assert name is None and part is None and date_rels == []
    assert report["generated"] is False


# =============================================================================
# End-to-end wiring through the convenience entry point.
# =============================================================================
def test_migrate_generates_date_table_by_default():
    out = migrate_tds_to_semantic_model(DATES_SQLSERVER, model_name="Sales")
    parts = out["parts"]

    assert "definition/tables/Date.tmdl" in parts
    # DATES_SQLSERVER resolves to DirectQuery: a CALENDARAUTO() calculated table would have to query
    # the source to discover its span (and fails to process without it), so a self-contained
    # fixed-range CALENDAR(...) is emitted instead. (Import models keep CALENDARAUTO -- see below.)
    assert "source = CALENDAR(DATE(2015, 1, 1), DATE(2035, 12, 31))" in parts["definition/tables/Date.tmdl"]
    assert "CALENDARAUTO" not in parts["definition/tables/Date.tmdl"]
    # the calendar is referenced by the model like any other table
    assert "ref table Date" in parts["definition/model.tmdl"]

    rels = parts["definition/relationships.tmdl"]
    assert "fromColumn: Orders.Order_Date" in rels
    assert "toColumn: Date.Date" in rels
    assert "fromColumn: Orders.Ship_Date" in rels
    # DATES_SQLSERVER resolves to DirectQuery, where a datePartOnly (datetime-to-date) join is
    # illegal -- Power BI rejects such a model. The calendar relationships must be plain dateTime
    # joins instead, so no joinOnDateBehavior is emitted.
    assert "joinOnDateBehavior" not in rels
    # The generated Date joins are many-to-one (Date[Date] is unique by construction) -- they must
    # NOT be emitted many-to-many; only authored Tableau object-graph relationships are (cause #1).
    assert "toCardinality: many" not in rels
    assert "isActive: false" in rels   # exactly the secondary (Ship Date) role

    dt = out["report"]["date_table"]
    assert dt["generated"] is True and dt["table"] == "Date"
    active = [r for r in dt["relationships"] if r["active"]]
    assert [r["column"] for r in active] == ["Order_Date"]


def test_migrate_date_table_opt_out():
    out = migrate_tds_to_semantic_model(DATES_SQLSERVER, model_name="Sales", date_table=False)
    parts = out["parts"]
    assert "definition/tables/Date.tmdl" not in parts
    assert "ref table Date" not in parts["definition/model.tmdl"]
    # with no other relationships in this datasource, none are emitted at all
    assert "definition/relationships.tmdl" not in parts
    assert out["report"]["date_table"]["generated"] is False


def test_migrate_no_date_table_when_no_date_columns():
    # LIVE_SQLSERVER's Orders has no date column -> no calendar injected
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore")
    assert "definition/tables/Date.tmdl" not in out["parts"]
    assert out["report"]["date_table"]["generated"] is False


# =============================================================================
# Date-attribute calc routing: bind to the auto-generated Date dimension only
# when the calc's date field carries the ACTIVE relationship (Option 1).
# =============================================================================
def _calc_row(report, column):
    rows = [r for r in report["calc_columns"] if r["column"] == column]
    assert len(rows) == 1, f"expected exactly one calc-column row for {column!r}"
    return rows[0]


def test_date_attribute_calc_over_active_date_binds_to_dimension():
    # YEAR([Order Date]) -- Order Date is the ACTIVE relationship -> RELATED follows it correctly,
    # so the calc binds to the Date dimension's [Year] attribute instead of an inline YEAR(...).
    dim_calcs = [{"name": "Order Year", "formula": "YEAR([Order Date])"}]
    out = migrate_tds_to_semantic_model(DATES_SQLSERVER, model_name="Sales", dim_calcs=dim_calcs)

    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "column 'Order Year' = RELATED('Date'[Year])" in orders
    # the original formula is preserved and provenance marks the date-dimension route
    assert "annotation TableauFormula = YEAR([Order Date])" in orders
    assert "annotation TranslatedBy = deterministic (date dimension)" in orders

    row = _calc_row(out["report"], "Order Year")
    assert row["status"] == "translated"
    assert row["table"] == "Orders"
    assert row["date_bound"] is True
    assert row["date_table"] == "Date"
    assert row["date_attribute"] == "Year"
    assert row["dax"] == "RELATED('Date'[Year])"


def test_date_attribute_calc_over_role_playing_date_stays_inline():
    # MONTH([Ship Date]) -- Ship Date is the INACTIVE (role-playing) relationship. RELATED would
    # silently follow the ACTIVE (Order Date) relationship and return the wrong month, so the calc
    # must keep a faithful inline MONTH(...) over the physical column instead of binding.
    dim_calcs = [{"name": "Ship Month", "formula": "MONTH([Ship Date])"}]
    out = migrate_tds_to_semantic_model(DATES_SQLSERVER, model_name="Sales", dim_calcs=dim_calcs)

    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "column 'Ship Month' = MONTH('Orders'[Ship_Date])" in orders
    assert "RELATED('Date'" not in orders.split("column 'Ship Month'")[1].split("column ")[0]
    assert "annotation TranslatedBy = deterministic" in orders

    row = _calc_row(out["report"], "Ship Month")
    assert row["status"] == "translated"
    assert row["date_bound"] is False
    assert row["date_attribute"] is None
    assert row["dax"] == "MONTH('Orders'[Ship_Date])"


def test_iso_date_attribute_calc_binds_and_adds_coverage():
    # ISOYEAR / DATENAME('weekday') have no faithful inline DAX (they honest-stub on their own), but
    # over the ACTIVE date they bind faithfully to the Date dimension's ISO attributes -- the date
    # routing strictly ADDS coverage here.
    dim_calcs = [
        {"name": "Order ISO Year", "formula": "ISOYEAR([Order Date])"},
        {"name": "Order Weekday", "formula": "DATENAME('weekday', [Order Date])"},
        {"name": "Order Quarter", "formula": "QUARTER([Order Date])"},
    ]
    out = migrate_tds_to_semantic_model(DATES_SQLSERVER, model_name="Sales", dim_calcs=dim_calcs)

    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "column 'Order ISO Year' = RELATED('Date'[ISO Year])" in orders
    assert "column 'Order Weekday' = RELATED('Date'[Day Name])" in orders
    # MONTH/QUARTER return a NUMBER in Tableau -> bind the numeric helper, never the display text.
    assert "column 'Order Quarter' = RELATED('Date'[Quarter No])" in orders

    assert _calc_row(out["report"], "Order ISO Year")["date_attribute"] == "ISO Year"
    assert _calc_row(out["report"], "Order Weekday")["date_attribute"] == "Day Name"
    assert _calc_row(out["report"], "Order Quarter")["date_attribute"] == "Quarter No"
    assert all(_calc_row(out["report"], c)["date_bound"] is True
               for c in ("Order ISO Year", "Order Weekday", "Order Quarter"))
