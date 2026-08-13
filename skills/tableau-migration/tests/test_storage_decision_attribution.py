"""A failure message must name the datasource that OWNS the failure.

Reported alongside #124. A workbook's embedded datasources are consolidated into ONE model, and the
message that reports a storage-decision fallback named the **ranked primary** datasource — which is
not necessarily the one whose relations failed:

    embedded datasource 'Big Data Source' needs a storage decision
      (Direct-upstream rebuild not safe (relation 'Orders.csv' has no resolvable columns;
       relation 'Orders_Archive.csv' has no resolvable columns))
      -- workbook .pbip skipped

Both column-less relations belonged to **`Small Data Source`**. `Big Data Source` was the one
datasource in that workbook with nothing wrong with it, so the only actionable fact in the sentence
pointed at the wrong place — a reader following it would open a healthy datasource, find three
cleanly-typed tables, and be no closer to the cause.

Two independent things were wrong, and both are fixed:

* the **subject** named one island for a model that spans several, so it now names the
  consolidation and lists every island;
* the **reasons** lost their island as they were merged, so each is now attributed to the datasource
  that raised it.

Attribution happens only on the consolidation path. ``combine_descriptors`` returns a lone
descriptor unchanged, so a single-datasource workbook's reasons are byte-identical to before.
"""
import connection_to_m as C
import migrate_estate as M


def _desc(name, reasons):
    return {"datasource_name": name, "class": "textscan", "relations": [], "relationships": [],
            "relationship_warnings": [], "logical_fields": [], "connections": {},
            "unsupported_reasons": list(reasons)}


def test_the_reported_message_no_longer_names_the_healthy_datasource():
    subject = M._storage_decision_subject(
        "Big Data Source",
        descriptor={"relations": []},
        combine_datasources=[{"caption": "Big Data Source"}, {"caption": "Small Data Source"}])
    assert "Small Data Source" in subject
    assert not subject.startswith("embedded datasource 'Big Data Source'")


def test_a_single_datasource_workbook_still_names_that_datasource():
    """The un-consolidated message is the one that was always correct; leave it alone."""
    assert (M._storage_decision_subject("Superstore", descriptor=None, combine_datasources=None)
            == "embedded datasource 'Superstore'")
    assert (M._storage_decision_subject("Superstore", descriptor={"relations": []},
                                        combine_datasources=[{"caption": "Superstore"}])
            == "embedded datasource 'Superstore'")


def test_each_reason_is_attributed_to_the_island_that_raised_it():
    combined = C.combine_descriptors(
        [_desc("big", []), _desc("small", ["relation 'Orders.csv' has no resolvable columns"])],
        captions=["Big Data Source", "Small Data Source"])
    assert combined["unsupported_reasons"] == [
        "Small Data Source: relation 'Orders.csv' has no resolvable columns"]


def test_reasons_from_several_islands_each_keep_their_own_owner():
    combined = C.combine_descriptors(
        [_desc("a", ["no resolvable column metadata"]), _desc("b", ["unknown connector"])],
        captions=["A", "B"])
    assert combined["unsupported_reasons"] == [
        "A: no resolvable column metadata", "B: unknown connector"]


def test_a_lone_descriptor_is_returned_untouched():
    """``combine_descriptors`` short-circuits on one input, so nothing is ever prefixed there."""
    only = _desc("solo", ["relation 'X' has no resolvable columns"])
    assert C.combine_descriptors([only], captions=["Solo"]) is only
    assert only["unsupported_reasons"] == ["relation 'X' has no resolvable columns"]


def test_attribution_is_not_applied_twice():
    """Guards a re-combine (or a caller that pre-attributes) from stuttering the caption."""
    combined = C.combine_descriptors(
        [_desc("a", []), _desc("b", ["B: already attributed"])], captions=["A", "B"])
    assert combined["unsupported_reasons"] == ["B: already attributed"]
