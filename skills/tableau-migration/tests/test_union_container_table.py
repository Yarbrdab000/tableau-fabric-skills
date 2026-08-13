"""A UNION is one table; a JOIN is several. Surfacing union members as tables loses the columns.

Tableau writes the two containers with opposite metadata, and that difference is the whole bug
(issue #124):

    union   <relation type='union' name='Orders.csv+'>          <- ALL 11 columns filed under
                <relation type='table' name='Orders.csv'/>         [Orders.csv+]; the members get
                <relation type='table' name='Orders_Archive.csv'/> NO metadata parent at all

    join    <relation type='join'>                              <- each member keeps its OWN
                <relation type='table' name='Customers.csv'/>      [Customers.csv] parent and
                <relation type='table' name='Customers_Details.csv'/>  its own columns

That is not a quirk, it is the semantics: a union produces the SAME columns with MORE rows
(``Table.Combine``), so there is only ever one column list and it belongs to the container. A join
produces a WIDER row from real tables, which is why we deliberately surface join leaves individually
and rebuild the join keys as model relationships.

We surfaced BOTH kinds of leaves individually, so every union member came out column-less. That is
survivable when the whole datasource is column-less -- the extract collapse
(``_collapse_untyped_relations_to_extract``) or the multi-parent expansion
(``_expand_untyped_relations_to_extract_tables``) then re-types everything from the ``.hyper`` -- and
fatal the moment the datasource is PARTIALLY typed, because both of those rescues begin with
``any(r["columns"]) -> return None``. A union sitting beside a join is exactly that shape:

    Direct-upstream rebuild not safe (relation 'Orders.csv' has no resolvable columns;
    relation 'Orders_Archive.csv' has no resolvable columns)
      -- workbook .pbip skipped

Measured on the reported workbook (``Section 09 - Filtering Data.twbx``): 0/1 before, 1/1 after --
9 tables, 51 columns, 5/5 calcs, PBIR validating with zero errors. The two union workbooks that
already built are unchanged in substance (21 columns, byte-identical 2,316 KB / 2,425 KB of data)
and now name the table ``Union``, which is what Tableau's own data pane calls it, instead of leaking
the extract's internal ``Extract`` name or standing the datasource caption in for a table.
"""
import xml.etree.ElementTree as ET

import connection_to_m as C
import storage_mode as S


def _cols(parent, names):
    return "".join(
        "<metadata-record class='column'><remote-name>%s</remote-name>"
        "<local-name>[%s]</local-name><parent-name>[%s]</parent-name>"
        "<local-type>string</local-type></metadata-record>" % (n, n, parent)
        for n in names)


def _ds(relations_xml, records_xml):
    return ET.fromstring(
        "<datasource formatted-name='Small Data Source' inline='true' version='18.1'>"
        "<connection class='federated'><named-connections>"
        "<named-connection caption='files' name='textscan.0a1b'>"
        "<connection class='textscan' directory='C:/data' filename='Orders.csv'/>"
        "</named-connection></named-connections>"
        + relations_xml +
        "<metadata-records>" + records_xml + "</metadata-records>"
        "</connection></datasource>")


UNION_BESIDE_JOIN = _ds(
    "<relation type='union' name='Orders.csv+'>"
    "  <relation type='table' name='Orders.csv' table='[Orders.csv]'/>"
    "  <relation type='table' name='Orders_Archive.csv' table='[Orders_Archive.csv]'/>"
    "</relation>"
    "<relation type='join'>"
    "  <relation type='table' name='Customers.csv' table='[Customers.csv]'/>"
    "  <relation type='table' name='Customers_Details.csv' table='[Customers_Details.csv]'/>"
    "</relation>"
    "<relation type='table' name='Products.csv' table='[Products.csv]'/>",
    _cols("Orders.csv+", ["Order ID", "Sales", "Region"])
    + _cols("Customers.csv", ["Customer ID", "Name"])
    + _cols("Customers_Details.csv", ["Segment"])
    + _cols("Products.csv", ["Product ID"]))


def _parse(ds):
    rels = C._extract_relations(ds, C._columns_by_parent(ds), {})
    return {r.get("name"): r for r in rels}


def test_the_reported_bug_the_union_members_are_no_longer_column_less():
    """0/1 -> 1/1: the two column-less relations that skipped the workbook are gone."""
    by_name = _parse(UNION_BESIDE_JOIN)
    assert "Orders.csv" not in by_name
    assert "Orders_Archive.csv" not in by_name
    assert not [r for r in by_name.values() if not r.get("columns")]


def test_the_union_container_is_surfaced_as_the_one_table_it_is():
    by_name = _parse(UNION_BESIDE_JOIN)
    union = by_name["Orders.csv+"]
    assert union["kind"] == "table"
    assert [c["remote_name"] for c in union["columns"]] == ["Order ID", "Sales", "Region"]
    assert union["union_of"] == ["Orders.csv", "Orders_Archive.csv"]


def test_a_join_still_surfaces_its_members_individually():
    """The control the reporter identified: joins already worked and must keep working."""
    by_name = _parse(UNION_BESIDE_JOIN)
    assert len(by_name["Customers.csv"]["columns"]) == 2
    assert len(by_name["Customers_Details.csv"]["columns"]) == 1
    assert len(by_name["Products.csv"]["columns"]) == 1


def test_a_wildcard_union_has_no_members_and_is_still_the_table():
    """``batch-union`` resolves its members from a pattern at connect time, so there are none."""
    ds = _ds("<relation type='batch-union' name='Union' all='true' is-recursive='true' path=''/>",
             _cols("Union", ["Order ID", "Sales"]))
    by_name = _parse(ds)
    assert list(by_name) == ["Union"]
    assert len(by_name["Union"]["columns"]) == 2
    assert by_name["Union"]["union_of"] == []


def test_a_union_whose_members_type_on_their_own_is_left_alone():
    """Fail-safe: never discard a member that IS a real typed table."""
    ds = _ds(
        "<relation type='union' name='U'>"
        "  <relation type='table' name='A' table='[A]'/>"
        "  <relation type='table' name='B' table='[B]'/>"
        "</relation>",
        _cols("U", ["X"]) + _cols("A", ["X"]) + _cols("B", ["X"]))
    by_name = _parse(ds)
    assert sorted(by_name) == ["A", "B"]


def test_a_union_with_nothing_filed_under_it_is_left_alone():
    """The wholly-untyped datasource still belongs to the extract collapse/expansion, untouched."""
    ds = _ds(
        "<relation type='union' name='Union'>"
        "  <relation type='table' name='order - 2023.csv' table='[order - 2023.csv]'/>"
        "  <relation type='table' name='order - 2024.csv' table='[order - 2024.csv]'/>"
        "</relation>",
        _cols("Extract", ["Order ID", "Sales"]))
    by_name = _parse(ds)
    assert sorted(by_name) == ["order - 2023.csv", "order - 2024.csv"]


def test_the_same_union_written_twice_dedupes_to_one_table():
    """Tableau writes the physical and logical layers as two copies of the same tree.

    Confirmed in the reported workbook: ``<relation type='union' name='Orders.csv+'>`` appears twice,
    each with the same two members. Both copies must promote to the SAME one table, and every member
    of both must be dropped.
    """
    tree = ("<relation type='union' name='Orders.csv+'>"
            "  <relation type='table' name='Orders.csv' table='[Orders.csv]'/>"
            "  <relation type='table' name='Orders_Archive.csv' table='[Orders_Archive.csv]'/>"
            "</relation>")
    ds = _ds(tree + tree, _cols("Orders.csv+", ["Sales"]))
    by_name = _parse(ds)
    assert list(by_name) == ["Orders.csv+"]
    assert len(by_name["Orders.csv+"]["columns"]) == 1


def test_a_typed_relation_sharing_a_members_display_name_survives():
    """Members are dropped by element IDENTITY, never by name.

    A name-based skip would delete the independent table below, because it happens to display the
    same name as a union input while resolving its columns from a different physical table. That is
    a fail-closed property rather than a common shape, and it is exactly the kind of collateral a
    name match causes.
    """
    ds = _ds(
        "<relation type='union' name='Orders.csv+'>"
        "  <relation type='table' name='Orders.csv' table='[Orders.csv]'/>"
        "  <relation type='table' name='Orders_Archive.csv' table='[Orders_Archive.csv]'/>"
        "</relation>"
        "<relation type='table' name='Orders.csv' table='[Orders_Live]'/>",
        _cols("Orders.csv+", ["Sales"]) + _cols("Orders_Live", ["Sales", "Qty"]))
    by_name = _parse(ds)
    assert sorted(by_name) == ["Orders.csv", "Orders.csv+"]
    assert len(by_name["Orders.csv"]["columns"]) == 2


def test_a_union_is_a_container_but_a_join_is_not_a_union():
    """One list per concept, and the union list is a strict subset of the container list."""
    assert set(S.UNION_RELATION_TYPES) == {"union", "batch-union"}
    assert set(S.UNION_RELATION_TYPES) < set(S.CONTAINER_RELATION_TYPES)
    assert "join" in S.CONTAINER_RELATION_TYPES
    assert "join" not in S.UNION_RELATION_TYPES
    assert C.UNION_RELATION_TYPES is S.UNION_RELATION_TYPES


def test_both_import_branches_bind_the_shared_constants():
    """2.137.0 imported ``CONTAINER_RELATION_TYPES`` in the flat branch ONLY.

    Tests run with ``scripts/`` on ``sys.path``, so they always took that branch and never saw it;
    a package-style import (``from .storage_mode import ...``) succeeded and then raised
    ``NameError`` at the first container check. Both branches must bind the same names.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(C))
    branches = [n for n in ast.walk(tree)
                if isinstance(n, ast.Try) and any(
                    isinstance(h.type, ast.Name) and h.type.id == "ImportError"
                    for h in n.handlers)]
    assert branches, "the dual-mode import block is gone"
    def _names(body):
        return {a.name for n in ast.walk(ast.Module(body=body, type_ignores=[]))
                if isinstance(n, ast.ImportFrom) for a in n.names}
    for blk in branches:
        pkg = _names(blk.body)
        flat = _names([s for h in blk.handlers for s in h.body])
        assert pkg == flat, "package/flat import branches bind different names: %s" % (
            pkg ^ flat)
