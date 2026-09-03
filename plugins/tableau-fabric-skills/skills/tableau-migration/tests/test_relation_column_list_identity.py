"""Two relations joining the SAME physical table keep their OWN column lists.

Tableau joins a table twice by giving the second relation a distinct name -- ``Contact`` and
``Contact1``, both ``table='[Contact]'`` -- and ``<metadata-record><parent-name>`` groups each
relation's columns under that RELATION name. ``_relation_entry`` looked the columns up by the
PHYSICAL table first, so both relations received the FIRST one's list object.

That is not merely untidy: the hidden-column prune mutates these lists IN PLACE. A column hidden on
``Contact`` (``Name``) was therefore deleted out from under ``Contact1``, which does not hide it.
Downstream the pill had no model column to bind to, missed every ``field_map`` key, and fell back to
the fact table -- a chart captioned "By Service Provider" listed engagement records instead of
people, with ZERO overlap between the two columns' values, and rendered plausibly because the axis
truncates the labels.

The identity assertions below are deliberate: two lists that merely COMPARE equal today would still
alias tomorrow, and aliasing is the defect.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import xml.etree.ElementTree as ET  # noqa: E402

import connection_to_m as C  # noqa: E402


def _rel(name, table):
    x = ET.Element("relation")
    x.set("name", name)
    x.set("table", table)
    x.set("type", "table")
    return x


def _cols(*names):
    return [{"model_name": n, "remote_name": n} for n in names]


def test_two_relations_over_one_physical_table_get_their_own_lists():
    """The regression, at its smallest."""
    cbp = {"Contact": _cols("Id", "MailingState"),
           "Contact1": _cols("Id", "Name")}
    a = C._classify_relation(_rel("Contact", "[Contact]"), cbp, {})
    b = C._classify_relation(_rel("Contact1", "[Contact]"), cbp, {})
    assert [c["model_name"] for c in b["columns"]] == ["Id", "Name"]
    assert a["columns"] is not b["columns"], "the two relations still SHARE one list object"


def test_the_shared_list_is_what_lets_a_prune_delete_another_relation_s_column():
    """Why identity matters: the hidden-column prune mutates in place."""
    cbp = {"Contact": _cols("Id", "Name"), "Contact1": _cols("Id", "Name")}
    a = C._classify_relation(_rel("Contact", "[Contact]"), cbp, {})
    b = C._classify_relation(_rel("Contact1", "[Contact]"), cbp, {})
    # simulate the prune dropping a column hidden on Contact only
    a["columns"][:] = [c for c in a["columns"] if c["model_name"] != "Name"]
    assert [c["model_name"] for c in b["columns"]] == ["Id", "Name"], \
        "pruning Contact removed Name from Contact1 -- the lists are aliased"


def test_a_relation_whose_name_matches_its_table_is_unchanged():
    cbp = {"Orders": _cols("Sales", "Region")}
    e = C._classify_relation(_rel("Orders", "[Orders]"), cbp, {})
    assert e["columns"] is cbp["Orders"]


def test_a_relation_with_no_name_keyed_columns_falls_back_to_the_physical_table():
    """The ordinary Superstore shape: the parent key is the sheet name (``Orders$``) while the
    relation is named ``Orders``, or vice versa. Only ONE key exists, so nothing changes."""
    cbp = {"Orders$": _cols("Sales")}
    e = C._classify_relation(_rel("Orders", "[Orders$]"), cbp, {})
    assert [c["model_name"] for c in e["columns"]] == ["Sales"]


def test_an_unknown_relation_still_yields_an_empty_column_list():
    e = C._classify_relation(_rel("Ghost", "[Ghost]"), {"Orders": _cols("Sales")}, {})
    assert e["columns"] == []


def test_the_relation_name_is_preferred_even_when_its_list_is_empty():
    """An empty own-list is still the RIGHT list: falling back on emptiness would silently restore
    the aliasing for a relation whose columns were all pruned."""
    cbp = {"Contact": _cols("Id", "Name"), "Contact1": []}
    e = C._classify_relation(_rel("Contact1", "[Contact]"), cbp, {})
    assert e["columns"] == []
    assert e["columns"] is not cbp["Contact"]


def test_bracketed_relation_names_resolve():
    cbp = {"Contact1": _cols("Name")}
    e = C._classify_relation(_rel("[Contact1]", "[Contact]"), cbp, {})
    assert [c["model_name"] for c in e["columns"]] == ["Name"]


def test_an_extract_qualified_table_still_resolves_by_relation_name():
    """An extracted datasource names the table ``[Extract].[Contact]``; the relation name is still
    the discriminator."""
    cbp = {"Contact": _cols("Id"), "Contact1": _cols("Id", "Name")}
    e = C._classify_relation(_rel("Contact1", "[Extract].[Contact]"), cbp, {})
    assert [c["model_name"] for c in e["columns"]] == ["Id", "Name"]


def test_custom_sql_relations_are_untouched():
    cbp = {"Q": _cols("A")}
    x = ET.Element("relation")
    x.set("name", "Q")
    x.set("type", "text")
    x.text = "SELECT 1"
    e = C._classify_relation(x, cbp, {})
    assert e["kind"] == "custom_sql"
    assert [c["model_name"] for c in e["columns"]] == ["A"]
