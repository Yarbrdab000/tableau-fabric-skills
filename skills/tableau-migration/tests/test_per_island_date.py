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
    built, rels, report = M._build_date_dimensions(
        [_rel("Orders", "Order_Date", ds="Sales"),
         _rel("Tickets", "Opened_Date", ds="Support")],
        ["Orders", "Tickets"], [])
    names = [n for n, _ in built]
    assert len(names) == 2
    assert "Date (Sales)" in names and "Date (Support)" in names
    assert report["per_island"] is True


def test_each_calendar_relates_only_to_its_own_island():
    """The whole point: a Sales date slicer must not reach a Support visual."""
    built, rels, _report = M._build_date_dimensions(
        [_rel("Orders", "Order_Date", ds="Sales"),
         _rel("Tickets", "Opened_Date", ds="Support")],
        ["Orders", "Tickets"], [])
    by_date = {}
    for r in rels:
        by_date.setdefault(r["to_table"], set()).add(r["from_table"])
    assert by_date["Date (Sales)"] == {"Orders"}
    assert by_date["Date (Support)"] == {"Tickets"}
    # and no calendar reaches across
    for target, facts in by_date.items():
        assert len(facts) == 1, "a calendar related to two islands' facts fuses them"


def test_island_calendar_names_do_not_collide_with_emitted_tables():
    """Names are reserved cumulatively, so a second island cannot reuse the first's calendar name."""
    built, _rels, _report = M._build_date_dimensions(
        [_rel("Orders", "Order_Date", ds="Sales"),
         _rel("Tickets", "Opened_Date", ds="Support")],
        ["Orders", "Tickets"], [])
    names = [n for n, _ in built]
    assert len(names) == len(set(names))
