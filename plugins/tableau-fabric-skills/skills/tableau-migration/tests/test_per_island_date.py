"""Separate Tableau datasources are ISLANDS, so they get separate calendars.

Tableau never lets one datasource's filters reach another's marks. A single shared calendar related
to facts in every island breaks that in the quietest possible way: a date slicer silently filters all
four dashboards' visuals at once, and nothing warns. Measured on a Salesforce case-management
workbook -- four datasources (Service Delivery, Intake, Client Enrollment and participation,
Assessments), ONE generated ``Date`` table wired to facts in all of them.

It is also redundant, which is what makes the fix safe rather than a trade-off. Tableau's own
mechanism for a filter that spans datasources is a PARAMETER -- parameters are global and belong to
no datasource -- and that already translates: a date-window parameter lands as a disconnected
what-if table whose ``[Start Date Value]`` / ``[End Date Value]`` measures each island's own
row-filter flag measure reads. Verified in the emitted model:

    measure 'Date FIlter Flag' =
      IF(COUNTROWS(FILTER('pmdm__ProgramEngagement__c',
         ('pmdm__ProgramEngagement__c'[pmdm__StartDate__c] >= [Start Date Value] && ... ))) > 0, 1)
    annotation TranslatedBy = deterministic (parameter-driven row filter)

So the cross-island date filter keeps working with no relationship at all -- which is exactly how the
source behaved. Splitting the calendar removes a filter path Tableau never had.

Islands are read from each relation's ``source_datasource`` tag. A descriptor with fewer than two
distinct tags -- every single-datasource workbook, and 25 of the 29 corpus fixtures -- takes the
original single-calendar path under the original ``"Date"`` name, so its output is byte-identical.
That boundary is asserted below, because it is what keeps this change from touching workbooks that
were already right.
"""
import assemble_model as M


def _rel(name, *date_cols, ds=None):
    r = {"name": name, "kind": "table",
         "columns": [{"model_name": c, "tmdl_type": "dateTime"} for c in date_cols]}
    if ds:
        r["source_datasource"] = ds
    return r


def test_an_untagged_descriptor_takes_the_original_single_calendar_path():
    """The byte-identical guarantee for every single-datasource workbook."""
    built, rels, report = M._build_date_dimensions(
        [_rel("Orders", "Order_Date")], ["Orders"], [])
    assert [n for n, _ in built] == ["Date"]
    assert {r["from_table"] for r in rels} == {"Orders"}
    assert report.get("per_island") is not True


def test_one_tagged_island_is_still_the_original_path():
    """A workbook with a single datasource must not acquire a suffixed calendar."""
    built, _rels, _report = M._build_date_dimensions(
        [_rel("Orders", "Order_Date", ds="Sales")], ["Orders"], [])
    assert [n for n, _ in built] == ["Date"]


def test_two_islands_get_one_calendar_each():
    """The per-island behaviour, which now SHIPS enabled.

    It was disabled for months because splitting the calendar cost three calculations on the workbook
    it was built for (119 -> 116 on Salesforce NPSP). The recorded suspicion was field-resolution
    tie-breaking; the measured cause was different -- see
    ``test_every_generated_calendar_is_a_conformed_hub``. The flag is still forced here so the test
    states its own precondition rather than inheriting it from module state.
    """
    saved = M.PER_ISLAND_DATE_ENABLED
    M.PER_ISLAND_DATE_ENABLED = True
    try:
        built, rels, report = M._build_date_dimensions(
            [_rel("Orders", "Order_Date", ds="Sales"),
             _rel("Tickets", "Opened_Date", ds="Support")],
            ["Orders", "Tickets"], [])
    finally:
        M.PER_ISLAND_DATE_ENABLED = saved
    names = [n for n, _ in built]
    assert len(names) == 2
    assert "Date (Sales)" in names and "Date (Support)" in names
    assert report["per_island"] is True


def test_each_calendar_relates_only_to_its_own_island():
    """The whole point of the feature: a Sales date slicer must not reach a Support visual."""
    saved = M.PER_ISLAND_DATE_ENABLED
    M.PER_ISLAND_DATE_ENABLED = True
    try:
        built, rels, _report = M._build_date_dimensions(
            [_rel("Orders", "Order_Date", ds="Sales"),
             _rel("Tickets", "Opened_Date", ds="Support")],
            ["Orders", "Tickets"], [])
    finally:
        M.PER_ISLAND_DATE_ENABLED = saved
    by_date = {}
    for r in rels:
        by_date.setdefault(r["to_table"], set()).add(r["from_table"])
    assert by_date["Date (Sales)"] == {"Orders"}
    assert by_date["Date (Support)"] == {"Tickets"}
    for target, facts in by_date.items():
        assert len(facts) == 1, "a calendar related to two islands' facts fuses them"


def test_per_island_ships_enabled_so_a_multi_datasource_workbook_gets_one_calendar_each():
    """The shipped behaviour, and the replacement for the guard that pinned the revert.

    That guard asserted the flag was ``False`` "because if it silently flipped back on, the three
    Salesforce calculations would go dead again". The cause of those three deaths is now fixed and
    measured (see ``test_every_generated_calendar_is_a_conformed_hub``), so the guard was asserting a
    workaround rather than an invariant. It is replaced rather than deleted: what it actually cared
    about -- that enabling this does not cost calculations -- is now covered by the hub test below,
    which pins the mechanism instead of the symptom.
    """
    assert M.PER_ISLAND_DATE_ENABLED is True
    built, _rels, report = M._build_date_dimensions(
        [_rel("Orders", "Order_Date", ds="Sales"),
         _rel("Tickets", "Opened_Date", ds="Support")],
        ["Orders", "Tickets"], [])
    assert sorted(n for n, _ in built) == ["Date (Sales)", "Date (Support)"]
    assert report["per_island"] is True


def test_every_generated_calendar_is_a_conformed_hub():
    """THE bug that kept per-island calendars switched off for months.

    A generated calendar is a DEGENERATE HUB: every fact joins its date columns into the shared
    calendar key, so any two facts look "connected" through same-calendar-date co-occurrence.
    ``_unique_countd_path`` therefore excludes calendars as TRANSIT nodes, or a cross-table
    ``COUNTD(IF ...)`` sees several spurious paths, calls false ambiguity, and stubs.

    The exclusion set was built from ONE name::

        conformed_hubs = {date_name} if date_name else None      # date_name = _date_built[0][0]

    With one calendar that is complete. With four it names the FIRST and leaves three live hubs, so
    the spurious paths come straight back. Measured on Salesforce NPSP: 119/158 translated with one
    calendar, **116/158** with four, and the three losses were
    ``Count of Waitlisted Engagements``, ``... in Date Range`` (both *"COUNTD(IF ...) must reference
    exactly one table"*) and ``Sort by Intake``. With every calendar named as a hub the count returns
    to **119/158** and the needs-review set is byte-identical to the single-calendar build -- the
    same three calcs, not merely the same total.

    The recorded suspicion had been field-resolution tie-breaking. It was wrong: the emitted table
    sets are identical apart from the calendars, and ``Record ID`` -- the field blamed for it --
    resolves to no table in EITHER build.

    Asserted against the real ``assemble_import_model`` call rather than a restatement of the
    derivation, because the defect was precisely that the production call passed a set that looked
    right.
    """
    seen = {}

    def _capture(*args, **kwargs):
        seen["hubs"] = kwargs.get("conformed_hubs")
        return "table _Measures\n", [], []

    descriptor = {
        "datasource_name": "Federated",
        "connection_class": "excel-direct",
        "flatfile_filename": "Data/Federated.xlsx",
        "relations": [
            {"kind": "table", "name": "Orders", "source_datasource": "Sales",
             "columns": [{"model_name": "Order_Date", "tmdl_type": "dateTime"},
                         {"model_name": "Order_ID", "tmdl_type": "int64"}]},
            {"kind": "table", "name": "Tickets", "source_datasource": "Support",
             "columns": [{"model_name": "Opened_Date", "tmdl_type": "dateTime"},
                         {"model_name": "Ticket_ID", "tmdl_type": "int64"}]},
        ],
        "relationships": [],
    }
    saved_flag, saved_part = M.PER_ISLAND_DATE_ENABLED, M._measures_part
    M.PER_ISLAND_DATE_ENABLED = True
    M._measures_part = _capture
    try:
        M.assemble_import_model(descriptor, model_name="M", calcs=[])
    finally:
        M.PER_ISLAND_DATE_ENABLED = saved_flag
        M._measures_part = saved_part

    hubs = seen.get("hubs") or set()
    assert hubs == {"Date (Sales)", "Date (Support)"}, (
        "every generated calendar must be a conformed hub; a calendar left out re-creates the "
        "spurious co-occurrence paths that stub cross-table COUNTD(IF ...). got: %r" % (hubs,))


def test_island_calendar_names_do_not_collide_with_emitted_tables():
    """Names are reserved cumulatively, so a second island cannot reuse the first's calendar name."""
    built, _rels, _report = M._build_date_dimensions(
        [_rel("Orders", "Order_Date", ds="Sales"),
         _rel("Tickets", "Opened_Date", ds="Support")],
        ["Orders", "Tickets"], [])
    names = [n for n, _ in built]
    assert len(names) == len(set(names))


# =============================================================================
# Binding a date pill to the RIGHT island's calendar.
# =============================================================================
import migrate_estate as E  # noqa: E402  -- kept beside the tests that use it
import twb_to_pbir as V  # noqa: E402


def _two_island_report():
    """The model build's date report for a two-island model, in its real shape."""
    return {"date_table": {
        "generated": True, "per_island": True,
        "tables": ["Date (Sales)", "Date (Support)"],
        "islands": [
            {"generated": True, "mark_as_date": True, "table": "Date (Sales)", "island": "Sales",
             "relationships": [{"table": "Orders", "column": "CreatedDate", "active": True}]},
            {"generated": True, "mark_as_date": True, "table": "Date (Support)", "island": "Support",
             "relationships": [{"table": "Tickets", "column": "CreatedDate", "active": True}]},
        ]}}


def test_a_per_island_model_still_produces_a_binding():
    """It used to produce NONE. ``_date_binding_from_model`` gated on ``table`` (singular), which a
    per-island report does not carry, so date binding switched off for the whole workbook -- measured
    at 0073 6 -> 0, 0088 5 -> 0, 0079 1 -> 0 calendar-bound refs. Axes still showed correct values on
    the fact's own column, but lost the calendar hierarchy entirely."""
    db = E._date_binding_from_model(_two_island_report())
    assert db is not None
    assert set(db["by_island"]) == {"sales", "support"}
    assert db["by_island"]["sales"]["date_table"] == "Date (Sales)"
    assert db["by_island"]["support"]["date_table"] == "Date (Support)"


def test_a_pill_binds_to_its_own_islands_calendar():
    db = E._date_binding_from_model(_two_island_report())
    sales = {"role": "dimension", "datasource": "Sales", "entity": "Orders",
             "property": "CreatedDate"}
    support = {"role": "dimension", "datasource": "Support", "entity": "Tickets",
               "property": "CreatedDate"}
    assert V._rebind_date_axis(sales, "Year", db)["entity"] == "Date (Sales)"
    assert V._rebind_date_axis(support, "Year", db)["entity"] == "Date (Support)"


def test_the_island_key_is_the_datasource_not_the_relation_name():
    """REGRESSION, and one I introduced then measured: keying on the field's ``entity`` looks right
    and is wrong, because the SAME relation name exists in several islands.

    ``pmdm__ProgramEngagement__c`` is a table in all four Salesforce datasources. With an entity key
    the map collides, and resolving the collision "first wins" bound an **Intake** pill to the
    **Service Delivery** calendar -- a calendar with no active join to that fact, so every bucket
    returns the grand total. The flat series the split exists to remove, reintroduced by the fix for
    it. Measured on 0088: 1 FLAT visual, and the corpus count read 6 (up from 5) so the regression
    presented as an IMPROVEMENT.

    The datasource caption is the one identifier both sides genuinely share -- the model tags each
    relation with ``source_datasource``, and a resolved field carries ``datasource``.
    """
    db = E._date_binding_from_model(_two_island_report())
    shared_entity = "pmdm__ProgramEngagement__c"
    a = {"role": "dimension", "datasource": "Sales", "entity": shared_entity,
         "property": "CreatedDate"}
    b = {"role": "dimension", "datasource": "Support", "entity": shared_entity,
         "property": "CreatedDate"}
    assert V._rebind_date_axis(a, "Year", db)["entity"] == "Date (Sales)"
    assert V._rebind_date_axis(b, "Year", db)["entity"] == "Date (Support)", (
        "an identical entity in two islands must still reach its OWN calendar")


def test_a_pill_whose_datasource_names_no_island_declines():
    """Fail-closed: binding an unattributable pill to an arbitrary island's calendar is exactly the
    cross-island rebind above."""
    db = E._date_binding_from_model(_two_island_report())
    orphan = {"role": "dimension", "datasource": "Unknown DS", "entity": "Orders",
              "property": "CreatedDate"}
    assert V._rebind_date_axis(orphan, "Year", db) is None


def test_a_single_calendar_model_is_untouched_by_the_island_path():
    """Every single-datasource workbook -- 28 of the corpus's 34 -- must be byte-for-byte unchanged."""
    db = E._date_binding_from_model({"date_table": {
        "generated": True, "mark_as_date": True, "table": "Date",
        "relationships": [{"table": "Orders", "column": "Order Date", "active": True}]}})
    assert "by_island" not in db
    field = {"role": "dimension", "datasource": "Anything", "entity": "Orders",
             "property": "Order Date"}
    assert V._rebind_date_axis(field, "Year", db)["entity"] == "Date"
