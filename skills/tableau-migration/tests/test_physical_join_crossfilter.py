"""A Tableau PHYSICAL join is one flat rowset, so its Power BI relationship must filter both ways.

Tableau's data model has two layers and they map onto Power BI exactly, but only one of them was
being honoured:

* a **physical join** (``<relation type='join'>``) pre-joins rows into ONE denormalized rowset, so a
  filter on any column restricts every column;
* a **logical relationship** (the 2020.2+ "noodle") keeps tables apart and joins per query, which is
  precisely what a Power BI model relationship already does.

Both were emitted one-directional. A Power BI relationship propagates filters from the ``to`` side
(lookup) to the ``from`` side (fact) and never back, so any measure aggregating a column on the
lookup table, broken down by a column on the fact table, silently returns the GRAND TOTAL on every
mark. It validates clean, it renders, every number is wrong.

Measured on a Salesforce case-management workbook whose "Clients by Engagement Stage" bar chart read
2,638 on all seven bars -- the unfiltered client count -- instead of 708/85/30/25/24/20/17. The
measure was ``DISTINCTCOUNTNOBLANK(Contact[Id])``; the axis was ``ProgramEngagement[Stage]``; the
relationship between them was many-to-one, one-directional. Emitting ``bothDirections`` fixed every
number with no change to the measure at all.

THE GUARD IS THE HARD PART, and it is why this file exists. Power BI REFUSES to open a project whose
relationships give a filter two routes between the same pair of tables. The first attempt marked 23
joins bidirectional and Desktop rejected the whole file:

    There are ambiguous paths between 'Contact' and 'Date':
    'Contact'->'pmdm__ProgramEngagement__c'->'pmdm__ServiceDelivery__c'->'Date' and
    'Contact'->'caseman__CasePlan__c'->'caseman__Goal__c'->'Date'

Direction is what makes that possible, and it is why the one-directional model loads with the very
same tables: several facts hanging off one Date hub is unambiguous because nothing travels back UP
into Date. Turn a fact-to-fact join bidirectional and a filter can leave Date, cross into a fact and
walk on -- and two such walks reaching one table is a refusal. So the guard runs over the FULL
relationship set, after the generated calendar exists, and keeps bidirectional edges only while they
form a forest. A demoted relationship still filters lookup->fact exactly as before, and is reported
so the fallback (``CALCULATE(..., CROSSFILTER(..., BOTH))``) is a visible choice.
"""
import assemble_model as M
import tmdl_generate as T


def _rel(frm, to, *, both=False, active=True):
    r = {"from_table": frm, "from_col": "FK", "to_table": to, "to_col": "Id",
         "cardinality": "many_to_many"}
    if both:
        r["cross_filter"] = "both"
    if not active:
        r["is_active"] = False
    return r


def test_a_physical_join_emits_both_directions():
    tmdl = T.generate_relationships_tmdl([_rel("Fact", "Dim", both=True)])
    assert "crossFilteringBehavior: bothDirections" in tmdl


def test_a_logical_relationship_stays_one_directional():
    """The noodle is already a per-query semantic join; making it bidirectional would be an invention."""
    tmdl = T.generate_relationships_tmdl([_rel("Fact", "Dim")])
    assert "crossFilteringBehavior: oneDirection" in tmdl
    assert "bothDirections" not in tmdl


def test_a_star_keeps_every_bidirectional_edge():
    """No cycle, no ambiguity -- the guard must not demote a model Power BI would have accepted."""
    rels = [_rel("Fact", "DimA", both=True), _rel("Fact", "DimB", both=True),
            _rel("Fact", "DimC", both=True)]
    out, warnings = M._guard_bidirectional_ambiguity(rels)
    assert all(r.get("cross_filter") == "both" for r in out)
    assert not warnings


def test_a_closed_loop_is_demoted_rather_than_shipped_unopenable():
    """Three tables in a triangle give two routes between every pair -- Power BI would refuse."""
    rels = [_rel("A", "B", both=True), _rel("B", "C", both=True), _rel("A", "C", both=True)]
    out, warnings = M._guard_bidirectional_ambiguity(rels)
    assert sum(1 for r in out if r.get("cross_filter") == "both") == 2
    assert len(out) == 3, "a demoted relationship is never dropped, only made one-directional"
    assert warnings


def test_a_single_direction_edge_closing_a_loop_also_demotes():
    """The real failure: the Date hub is one-directional, and it is what closed the second path.

    A -> B and A -> C are bidirectional; the one-directional D -> B / D -> C pair then joins B and C
    through D. Nothing can be done to the D edges (they are the only join there is), so the
    bidirectional ones give way.
    """
    rels = [_rel("B", "A", both=True), _rel("C", "A", both=True),
            _rel("B", "D"), _rel("C", "D")]
    out, _w = M._guard_bidirectional_ambiguity(rels)
    assert len(out) == 4
    assert sum(1 for r in out if r.get("cross_filter") == "both") < 2


def test_an_inactive_relationship_cannot_create_a_path():
    """It carries no filter until USERELATIONSHIP activates it, so it must not force a demotion."""
    rels = [_rel("A", "B", both=True), _rel("B", "C", both=True),
            _rel("A", "C", active=False)]
    out, _w = M._guard_bidirectional_ambiguity(rels)
    assert sum(1 for r in out if r.get("cross_filter") == "both") == 2


def test_a_model_with_no_physical_joins_is_untouched():
    """Byte-identical for every workbook that never used a physical join."""
    rels = [_rel("Fact", "DimA"), _rel("Fact", "DimB")]
    out, warnings = M._guard_bidirectional_ambiguity(rels)
    assert out == rels and not warnings
