"""Consolidating PLAIN (non-federated) datasources must not look unroutable.

Tableau writes a ``<named-connections>`` block only for a FEDERATED datasource. A plain
single-upstream datasource stores its connection as the scalar attributes of its one
``<connection>`` element, so its relations carry NO ``connection`` attribute -- correctly, because
inside that datasource there is nothing to disambiguate.

That becomes a false negative the moment two such datasources are consolidated into one model: the
combined descriptor is multi-connection by construction, every relation looks unroutable to
``storage_mode._structurally_unsupported_reason``, and the ENTIRE workbook rebuild is declined --
no model, no report, nothing. Measured on the deterministic corpus: 5 of 29 workbooks, each a
Challenge/Solution or "(copy)" pair of plain Excel/Access datasources.

These tests pin the reconstruction (``_self_connection_facts``) end to end, plus the quoted-sheet
navigation defect it made reachable.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from connection_to_m import (  # noqa: E402
    _excel_sheet_name,
    _self_connection_facts,
    combine_descriptors,
    emit_flatfile_source,
    parse_tds,
)
from storage_mode import _structurally_unsupported_reason, select_storage_mode  # noqa: E402


def _plain_excel_tds(ds_name, filename, sheet="Orders$"):
    """A plain (NON-federated) single-connection Excel datasource: no <named-connections>, and a
    <relation> with no ``connection`` attribute -- exactly what Tableau writes."""
    return f"""
    <datasource name='{ds_name}'>
      <connection class='excel-direct' filename='{filename}'>
        <relation name='{sheet}' table='[{sheet}]' type='table' />
      </connection>
      <metadata-records>
        <metadata-record class='column'>
          <remote-name>Row ID</remote-name><local-name>[Row ID]</local-name>
          <parent-name>[{sheet}]</parent-name><local-type>integer</local-type>
        </metadata-record>
        <metadata-record class='column'>
          <remote-name>Region</remote-name><local-name>[Region]</local-name>
          <parent-name>[{sheet}]</parent-name><local-type>string</local-type>
        </metadata-record>
        <metadata-record class='column'>
          <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
          <parent-name>[{sheet}]</parent-name><local-type>real</local-type>
        </metadata-record>
      </metadata-records>
    </datasource>
    """


def _plain_sql_tds(ds_name, server, dbname, table="Orders"):
    return f"""
    <datasource name='{ds_name}'>
      <connection class='sqlserver' server='{server}' dbname='{dbname}'>
        <relation name='{table}' table='[dbo].[{table}]' type='table' />
      </connection>
      <metadata-records>
        <metadata-record class='column'>
          <remote-name>Id</remote-name><local-name>[Id]</local-name>
          <parent-name>[{table}]</parent-name><local-type>integer</local-type>
        </metadata-record>
        <metadata-record class='column'>
          <remote-name>Name</remote-name><local-name>[Name]</local-name>
          <parent-name>[{table}]</parent-name><local-type>string</local-type>
        </metadata-record>
      </metadata-records>
    </datasource>
    """


# -- the regression itself ------------------------------------------------------------------


def test_plain_single_connection_datasource_parses_with_an_empty_connection_map():
    """The precondition the bug rested on: a plain datasource has NO named-connection map."""
    desc = parse_tds(_plain_excel_tds("DS A", "Data/Book.xlsx"))
    assert desc["connections"] == {}
    assert desc["named_connection_count"] == 1
    assert [r.get("connection") for r in desc["relations"]] == [None]


def test_two_plain_datasources_consolidate_into_a_routable_descriptor():
    """THE REGRESSION: before the fix this combination reported every table as unroutable, and
    ``select_storage_mode`` declined the whole rebuild."""
    a = parse_tds(_plain_excel_tds("DS A", "Data/Book.xlsx"))
    b = parse_tds(_plain_excel_tds("DS B", "Data/Book.xlsx"))
    combined = combine_descriptors([a, b], captions=["DS A", "DS B"])

    assert combined["named_connection_count"] == 2, "consolidation is multi-connection by construction"
    assert _structurally_unsupported_reason(combined) is None
    decision = select_storage_mode(combined)
    assert decision["mode"] == "Import"
    assert decision["fallback"] is None


def test_every_consolidated_relation_carries_its_own_upstream_facts():
    a = parse_tds(_plain_excel_tds("DS A", "Data/A.xlsx"))
    b = parse_tds(_plain_excel_tds("DS B", "Data/B.xlsx"))
    combined = combine_descriptors([a, b], captions=["DS A", "DS B"])

    rels = [r for r in combined["relations"] if r.get("kind") == "table"]
    assert len(rels) == 2
    files = sorted((r["connection"] or {}).get("filename") for r in rels)
    assert files == ["Data/A.xlsx", "Data/B.xlsx"], (
        "each island must route to ITS OWN file, not the primary's")
    for r in rels:
        assert r["connection"]["connection_class"] == "excel-direct"


def test_mixed_connector_islands_each_keep_their_own_class():
    """A workbook mixing a flat file and a live database must not smear one class over both."""
    a = parse_tds(_plain_excel_tds("Excel DS", "Data/Book.xlsx"))
    b = parse_tds(_plain_sql_tds("SQL DS", "sql.example.com", "Sales"))
    combined = combine_descriptors([a, b], captions=["Excel DS", "SQL DS"])

    by_name = {r["name"]: r["connection"] for r in combined["relations"] if r.get("kind") == "table"}
    assert by_name["Orders$"]["connection_class"] == "excel-direct"
    assert by_name["Orders$"]["filename"] == "Data/Book.xlsx"
    assert by_name["Orders"]["connection_class"] == "sqlserver"
    assert by_name["Orders"]["server"] == "sql.example.com"
    assert by_name["Orders"]["database"] == "Sales"
    assert _structurally_unsupported_reason(combined) is None


def test_reconstructed_facts_match_the_federated_named_connection_key_shape():
    """``relation["connection"]`` is consumed as a ``_connection_facts`` dict by
    ``_effective_connection`` / ``_flatfile_path_for`` / ``_land_combined_flatfiles``. A shape drift
    would dangle silently, so pin the contract keys."""
    desc = parse_tds(_plain_excel_tds("DS A", "Data/Book.xlsx"))
    facts = _self_connection_facts(desc)
    for key in ("connection_class", "server", "database", "warehouse", "http_path",
                "schema", "auth_method", "filename", "directory"):
        assert key in facts, key
    assert "flatfile_path" not in facts, (
        "the driver pins the ABSOLUTE path per-relation; a relative one here would win over it")


# -- fail-closed ----------------------------------------------------------------------------


def test_a_datasource_with_no_connector_class_is_never_attributed():
    assert _self_connection_facts({}) is None
    assert _self_connection_facts({"connection_class": "   "}) is None


def test_a_federated_multi_connection_island_is_left_to_its_own_map():
    """Reconstruction must fire ONLY when there is no named-connection map to trust."""
    federated = """
    <datasource name='Fed'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='c1'><connection class='sqlserver' server='s1' dbname='d1' /></named-connection>
          <named-connection name='c2'><connection class='sqlserver' server='s2' dbname='d2' /></named-connection>
        </named-connections>
        <relation connection='c1' name='T1' table='[dbo].[T1]' type='table' />
        <relation connection='c2' name='T2' table='[dbo].[T2]' type='table' />
      </connection>
      <metadata-records>
        <metadata-record class='column'>
          <remote-name>Id</remote-name><local-name>[Id]</local-name>
          <parent-name>[T1]</parent-name><local-type>integer</local-type>
        </metadata-record>
        <metadata-record class='column'>
          <remote-name>Id</remote-name><local-name>[Id]</local-name>
          <parent-name>[T2]</parent-name><local-type>integer</local-type>
        </metadata-record>
      </metadata-records>
    </datasource>
    """
    plain = parse_tds(_plain_excel_tds("Plain", "Data/Book.xlsx"))
    fed = parse_tds(federated)
    assert len(fed["connections"]) == 2

    combined = combine_descriptors([fed, plain], captions=["Fed", "Plain"])
    by_name = {r["name"]: r["connection"] for r in combined["relations"] if r.get("kind") == "table"}
    assert by_name["T1"]["server"] == "s1"
    assert by_name["T2"]["server"] == "s2", "a federated island keeps its OWN per-relation routing"
    assert by_name["Orders$"]["filename"] == "Data/Book.xlsx"


def test_single_descriptor_is_returned_untouched():
    """One datasource is not a consolidation -- output must stay byte-identical."""
    desc = parse_tds(_plain_excel_tds("DS A", "Data/Book.xlsx"))
    assert combine_descriptors([desc]) is desc
    assert [r.get("connection") for r in desc["relations"]] == [None]


def test_consolidation_never_papers_over_a_genuinely_ambiguous_island():
    """The dangerous mutation: reconstructing an upstream for an island that HAS several named
    connections. There the missing ``connection`` attribute is a REAL ambiguity -- we cannot know
    which of the two upstreams the table came from -- so the relation must stay unrouted and the
    combined descriptor must still decline, even though a sibling plain island reconstructs fine."""
    ambiguous = """
    <datasource name='Fed'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='c1'><connection class='sqlserver' server='s1' dbname='d1' /></named-connection>
          <named-connection name='c2'><connection class='sqlserver' server='s2' dbname='d2' /></named-connection>
        </named-connections>
        <relation connection='c1' name='T1' table='[dbo].[T1]' type='table' />
        <relation name='Mystery' table='[dbo].[Mystery]' type='table' />
      </connection>
      <metadata-records>
        <metadata-record class='column'>
          <remote-name>Id</remote-name><local-name>[Id]</local-name>
          <parent-name>[T1]</parent-name><local-type>integer</local-type>
        </metadata-record>
        <metadata-record class='column'>
          <remote-name>Id</remote-name><local-name>[Id]</local-name>
          <parent-name>[Mystery]</parent-name><local-type>integer</local-type>
        </metadata-record>
      </metadata-records>
    </datasource>
    """
    combined = combine_descriptors(
        [parse_tds(ambiguous), parse_tds(_plain_excel_tds("Plain", "Data/Book.xlsx"))],
        captions=["Fed", "Plain"])

    by_name = {r["name"]: r.get("connection")
               for r in combined["relations"] if r.get("kind") == "table"}
    assert by_name["Mystery"] is None, (
        "a table inside a MULTI-connection island has a real ambiguity -- never invent an upstream")
    assert by_name["T1"]["server"] == "s1"
    assert by_name["Orders$"]["filename"] == "Data/Book.xlsx"

    reason = _structurally_unsupported_reason(combined)
    assert reason and "Mystery" in reason, "the genuine ambiguity must still decline the rebuild"


def test_a_genuinely_unroutable_table_still_declines():
    """The guard must keep catching a real ambiguity: a federated island whose relation names a
    connection id that resolves to nothing has no upstream, and must NOT be papered over."""
    federated_unrouted = """
    <datasource name='Fed'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='c1'><connection class='sqlserver' server='s1' dbname='d1' /></named-connection>
          <named-connection name='c2'><connection class='sqlserver' server='s2' dbname='d2' /></named-connection>
        </named-connections>
        <relation name='Mystery' table='[dbo].[Mystery]' type='table' />
      </connection>
      <metadata-records>
        <metadata-record class='column'>
          <remote-name>Id</remote-name><local-name>[Id]</local-name>
          <parent-name>[Mystery]</parent-name><local-type>integer</local-type>
        </metadata-record>
      </metadata-records>
    </datasource>
    """
    desc = parse_tds(federated_unrouted)
    reason = _structurally_unsupported_reason(desc)
    assert reason and "don't resolve to a specific connection" in reason


# -- the defect the fix made reachable ------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("[Orders$]", "Orders"),
    ("Orders$", "Orders"),
    ("['Master Date List$']", "Master Date List"),
    ('["Master Date List$"]', "Master Date List"),
    ("['Sheet1']", "Sheet1"),
    # an apostrophe INSIDE an unquoted name must survive untouched
    ("[John's Data$]", "John's Data"),
    # Tableau doubles an inner quote when it quotes the identifier
    ("['John''s Data$']", "John's Data"),
])
def test_excel_sheet_name_unquotes_tableau_identifiers(raw, expected):
    assert _excel_sheet_name({"raw_table": raw}) == expected


@pytest.mark.parametrize("raw", [
    "['Master Date List$]",     # opening quote, no closing quote
    "[Master Date List'$]",     # closing quote only
    '["Master Date List$]',
])
def test_an_unmatched_quote_is_never_stripped(raw):
    """The unquote requires a MATCHED pair. An unpaired quote is indistinguishable from a quote that
    is genuinely part of the sheet name, so it is left alone -- stripping it would silently navigate
    to a sheet that does not exist (the same class of failure this fix exists to remove)."""
    assert "'" in _excel_sheet_name({"raw_table": raw}) or '"' in _excel_sheet_name({"raw_table": raw})


def test_quoted_sheet_navigates_the_real_worksheet():
    """A quoted sheet name reached Power Query as ``Item="'Master Date List$'"`` -- matching no
    sheet, so the partition opened and loaded ZERO rows without erroring."""
    relation = {
        "kind": "table",
        "name": "'Master Date List$'",
        "raw_table": "['Master Date List$']",
        "columns": [{"model_name": "Date", "remote_name": "Date", "tmdl_type": "dateTime"}],
    }
    conn = {"connection_class": "excel-direct", "flatfile_path": "C:/data/book.xlsx"}
    m = emit_flatfile_source(relation, conn, "excel-direct")
    assert m is not None
    assert 'Item="Master Date List", Kind="Sheet"' in m
    assert "'Master Date List" not in m
