"""Ambiguous relationship paths: the defect class that stops a .pbip OPENING.

Power BI does not render this badly -- it refuses the whole project:

    There's a problem with the definition content in your Power BI Project.
    There are ambiguous paths between 'pmdm__ServiceDelivery__c' and 'Date (Service Delivery)':
    'pmdm__ServiceDelivery__c'->'Date (Service Delivery)' and
    'pmdm__ServiceDelivery__c'->'pmdm__ProgramEngagement__c'->'Date (Service Delivery)'

The model beside that report is unreachable too, so this ranks with invalid TMDL, not with fidelity.

It arrived as a REGRESSION from the per-datasource calendar work: giving every fact its own active
date edge is safe until two facts are joined to EACH OTHER as well as to the same calendar, at which
point a diamond appears. Neither edge is wrong on its own -- the defect exists only in the RELATION
between them, which is exactly the shape a per-object validator cannot see. Measured: the pre-change
engine emitted 28 active / 11 inactive relationships and 0 ambiguous pairs; the post-change engine
emitted 33 active / 6 inactive and 4 ambiguous pairs.

Two layers are tested here, and they are deliberately independent: the EMITTER refuses to create the
ambiguity, and the GATE refuses to ship it however it arose.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import assemble_model as A  # noqa: E402
import openability_gate as G  # noqa: E402


# --- the shape, as TMDL --------------------------------------------------------------------

DIAMOND = """
relationship a
\tfromColumn: pmdm__ServiceDelivery__c.pmdm__DeliveryDate__c
\ttoColumn: 'Date (Service Delivery)'.Date

relationship b
\tfromColumn: pmdm__ProgramEngagement__c.pmdm__StartDate__c
\ttoColumn: 'Date (Service Delivery)'.Date

relationship c
\tfromColumn: pmdm__ServiceDelivery__c.pmdm__ProgramEngagement__c
\ttoColumn: pmdm__ProgramEngagement__c.Id
"""

CLEAN_STAR = """
relationship a
\tfromColumn: Sales.OrderDate
\ttoColumn: 'Date'.Date

relationship b
\tfromColumn: Returns.ReturnDate
\ttoColumn: 'Date'.Date
"""


def test_the_reported_diamond_is_detected():
    pairs = G._ambiguous_relationship_pairs(DIAMOND)
    assert pairs, "the exact shape Desktop refused must be detected"
    targets = {(a, b) for a, b, _ in pairs}
    assert ("Date (Service Delivery)", "pmdm__ServiceDelivery__c") in targets


def test_a_plain_star_with_two_facts_on_one_calendar_is_NOT_ambiguous():
    """The most important negative. Two facts hanging off one shared date table is the single most
    common legal model there is; a check that flags it would fail every healthy build.

    This is what makes the direction handling load-bearing rather than a detail -- modelled
    undirected, this fixture is 'ambiguous' and the gate is worthless.
    """
    assert G._ambiguous_relationship_pairs(CLEAN_STAR) == []


def test_an_inactive_second_edge_removes_the_ambiguity():
    fixed = DIAMOND.replace("relationship b\n\tfromColumn",
                            "relationship b\n\tisActive: false\n\tfromColumn")
    assert G._ambiguous_relationship_pairs(fixed) == []


def test_quoted_table_names_are_parsed_whole():
    """'Date (Service Delivery)' contains spaces and parentheses. Splitting on the first '.' yields
    a table named "'Date (Service" -- and the check then compares tables that do not exist and
    reports nothing, which is the silent-pass failure mode."""
    table, col = G._split_qualified("'Date (Service Delivery)'.Date")
    assert (table, col) == ("Date (Service Delivery)", "Date")


def test_bidirectional_relationship_is_traversable_both_ways():
    both = CLEAN_STAR + """
relationship c
\tfromColumn: Returns.OrderId
\ttoColumn: Sales.Id
\tcrossFilteringBehavior: bothDirections
"""
    assert G._ambiguous_relationship_pairs(both), "a bidirectional edge re-opens the second path"


# --- the gate ------------------------------------------------------------------------------


def test_openability_gate_fails_a_model_that_will_not_open():
    verdict = G.check_model_openability({"definition/relationships.tmdl": DIAMOND})
    assert verdict["checks"]["unambiguous_relationship_paths"] is False
    assert verdict["ok"] is False
    detail = " ".join(i["detail"] for i in verdict["issues"]
                      if i["check"] == "unambiguous_relationship_paths")
    assert "refuses to OPEN" in detail


def test_openability_gate_passes_a_clean_star():
    verdict = G.check_model_openability({"definition/relationships.tmdl": CLEAN_STAR})
    assert verdict["checks"]["unambiguous_relationship_paths"] is True


def test_openability_gate_is_silent_when_there_are_no_relationships():
    """A model with no relationships part must not be judged -- 'cannot evaluate' is not 'invalid'."""
    verdict = G.check_model_openability({})
    assert verdict["checks"]["unambiguous_relationship_paths"] is True


# --- the emitter ---------------------------------------------------------------------------


def _rels(*triples):
    return [{"from_table": f, "to_table": t, "is_active": True} for f, t in triples]


def test_emitter_refuses_the_second_active_edge_into_one_calendar():
    """Both facts want their own date. Only one may have it, or the project will not open."""
    candidates = [("pmdm__ProgramEngagement__c", ["pmdm__StartDate__c"], "pmdm__StartDate__c"),
                  ("pmdm__ServiceDelivery__c", ["pmdm__DeliveryDate__c"], "pmdm__DeliveryDate__c")]
    rels = _rels(("pmdm__ServiceDelivery__c", "pmdm__ProgramEngagement__c"))
    activated = A._activate_without_ambiguity(candidates, rels, "Date (Service Delivery)")
    assert len(activated) == 1, "exactly one of a joined fact pair may hold the active date edge"
    assert "pmdm__ProgramEngagement__c" in activated  # upstream fact wins; deterministic by name


def test_emitter_activates_both_when_the_facts_are_unrelated():
    """The negative that keeps the fix from degrading into 'one active date edge per calendar'."""
    candidates = [("Sales", ["OrderDate"], "OrderDate"), ("Returns", ["ReturnDate"], "ReturnDate")]
    activated = A._activate_without_ambiguity(candidates, _rels(), "Date")
    assert activated == {"Sales", "Returns"}


def test_emitter_ignores_an_inactive_join_when_deciding():
    """An INACTIVE fact-to-fact join carries no filter, so it creates no second path and must not
    cost a fact its own date relationship."""
    candidates = [("Sales", ["OrderDate"], "OrderDate"), ("Returns", ["ReturnDate"], "ReturnDate")]
    rels = [{"from_table": "Returns", "to_table": "Sales", "is_active": False}]
    activated = A._activate_without_ambiguity(candidates, rels, "Date")
    assert activated == {"Sales", "Returns"}


def test_emitter_skips_a_table_with_no_primary_date():
    candidates = [("Sales", ["A", "B"], None)]
    assert A._activate_without_ambiguity(candidates, _rels(), "Date") == set()


def test_emitted_model_for_the_diamond_has_no_ambiguity_left():
    """End to end through the two layers: what the emitter produces must satisfy the gate."""
    candidates = [("PE", ["StartDate"], "StartDate"), ("SD", ["DeliveryDate"], "DeliveryDate")]
    activated = A._activate_without_ambiguity(candidates, _rels(("SD", "PE")), "Date")
    lines = ["relationship j\n\tfromColumn: SD.PE_Id\n\ttoColumn: PE.Id"]
    for disp, _cols, primary in candidates:
        flag = "" if disp in activated else "\n\tisActive: false"
        lines.append("relationship %s%s\n\tfromColumn: %s.%s\n\ttoColumn: 'Date'.Date"
                     % (disp, flag, disp, primary))
    assert G._ambiguous_relationship_pairs("\n\n".join(lines)) == []
