"""Unit tests for the Catalog-independent ``.tds`` / ``.tdsx`` parser in ``tableau_inventory.py``.

These cover the fallback path used when Tableau Catalog has not indexed a datasource (common on
Tableau Cloud), where we download the descriptor and parse columns + relation tables directly.
"""
import io
import zipfile

import tableau_inventory as tab

# A trimmed but structurally faithful federated .tds: a non-federated child connection (Azure SQL),
# two relation tables, and a mix of column / non-column metadata-records.
SAMPLE_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='federated.abc' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='host.database.windows.net' name='azure_sqldb.1wrkf7x0'>
        <connection authentication='sqlserver' class='azure_sqldb' dbname='SalesDW'
                    server='host.database.windows.net' username='app_reader' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='azure_sqldb.1wrkf7x0' name='Orders' table='[dbo].[Orders]' type='table' />
      <relation connection='azure_sqldb.1wrkf7x0' name='Returns' table='[dbo].[Returns]' type='table' />
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Row_ID</remote-name>
        <local-name>[Row_ID]</local-name>
        <parent-name>[Orders]</parent-name>
        <local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Order_ID</remote-name>
        <local-name>[Order_ID]</local-name>
        <parent-name>[Orders]</parent-name>
        <local-type>string</local-type>
      </metadata-record>
      <metadata-record class='capability'>
        <remote-name>ignored</remote-name>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>
"""


def test_parse_tds_extracts_sources_with_connector_db_schema_table():
    sources = tab.parse_tds(SAMPLE_TDS)["sources"]
    assert {s["table"] for s in sources} == {"Orders", "Returns"}
    orders = [s for s in sources if s["table"] == "Orders"][0]
    assert orders["connectionType"] == "azure_sqldb"
    assert orders["database"] == "SalesDW"
    assert orders["schema"] == "dbo"


def test_parse_tds_extracts_columns_with_types_and_skips_noncolumn_records():
    fields = {f["name"]: f["dataType"] for f in tab.parse_tds(SAMPLE_TDS)["fields"]}
    # local-type is upper-cased to line up with the Metadata API; the capability record is skipped.
    assert fields == {"Row_ID": "INTEGER", "Order_ID": "STRING"}


def test_parse_tds_tolerates_empty_and_garbage():
    assert tab.parse_tds("") == {"fields": [], "sources": []}
    assert tab.parse_tds("<not-a-tds/>") == {"fields": [], "sources": []}


def test_parse_tds_flags_calculated_fields():
    tds = (
        "<datasource>"
        "<column caption='Profit Ratio' datatype='real' name='[Calculation_1]' role='measure'>"
        "<calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />"
        "</column>"
        "<column caption='Plain Bin' datatype='integer' name='[Bin]' role='dimension' />"
        "</datasource>"
    )
    fields = {f["name"]: f for f in tab.parse_tds(tds)["fields"]}
    assert "Profit Ratio" in fields and fields["Profit Ratio"]["is_calculated"] is True
    # a non-calculated <column> with no <calculation> child is not flagged / not captured here
    assert "Plain Bin" not in fields


def test_split_schema_table_handles_bracketed_and_bare():
    assert tab._split_schema_table("[dbo].[Orders]") == ("dbo", "Orders")
    assert tab._split_schema_table("[Orders]") == ("", "Orders")
    assert tab._split_schema_table("Orders") == ("", "Orders")


def test_extract_tds_text_from_tdsx_zip_ignores_extract():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Data/extract.hyper", b"binary-extract-bytes")
        zf.writestr("mydatasource.tds", SAMPLE_TDS)
    text = tab.extract_tds_text(buf.getvalue())
    assert text is not None and "<datasource" in text
    assert tab.parse_tds(text)["sources"]


def test_extract_tds_text_from_bare_tds_and_empty():
    assert "<datasource" in tab.extract_tds_text(SAMPLE_TDS.encode("utf-8"))
    assert tab.extract_tds_text(b"") is None


def test_shape_from_tds_matches_inventory_shape():
    row = tab.shape_from_tds("Azure SQL - Superstore", "Default", "luid-1", SAMPLE_TDS)
    assert row["name"] == "Azure SQL - Superstore"
    assert row["project"] == "Default"
    assert row["luid"] == "luid-1"
    assert row["fields"] and row["sources"]
    assert set(row["fields"][0]) >= {"name", "dataType", "role"}
    assert set(row["sources"][0]) >= {"connectionType", "database", "schema", "table"}


# --------------------------------------------------------------------------------------
# Custom SQL (`<relation type='text'>`): mine FROM/JOIN tables out of the embedded SQL
# --------------------------------------------------------------------------------------
CUSTOM_SQL_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='federated.xyz' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection name='nc1'>
        <connection class='sqlserver' server='srv' dbname='Sales' />
      </named-connection>
    </named-connections>
    <relation name='Custom SQL Query' type='text' connection='nc1'>
SELECT o.id, c.name
FROM dbo.Orders o
JOIN [dbo].[Customers] c ON c.id = o.cid
LEFT JOIN analytics.Regions r ON r.code = c.region
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>id</remote-name><local-type>integer</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>
"""


def test_custom_sql_extracts_from_and_join_tables():
    sources = tab.parse_tds(CUSTOM_SQL_TDS)["sources"]
    pairs = {(s["schema"], s["table"]) for s in sources}
    assert ("dbo", "Orders") in pairs
    assert ("dbo", "Customers") in pairs
    assert ("analytics", "Regions") in pairs
    assert all(s["connectionType"] == "sqlserver" and s["database"] == "Sales" for s in sources)


def test_tables_from_sql_handles_quoting_and_dedupes():
    sql = 'select * from "public"."orders" o join public.orders o2 on 1=1 cross join Customers'
    pairs = tab._tables_from_sql(sql)
    # bracket/quote stripped, case-insensitive dedupe of the repeated orders, bare table kept
    assert ("public", "orders") in pairs
    assert ("", "Customers") in pairs
    assert sum(1 for _, t in pairs if t.lower() == "orders") == 1


def test_custom_sql_text_relations_nested_in_a_join_are_not_dropped():
    # Tableau wraps joined custom SQL in a <relation type='join'> with several inner text relations.
    # A lazy outer match starting at the join tag would swallow the first inner text relation, so the
    # parser must require type='text' to match each inner relation independently.
    tds = """<datasource>
  <connection class='federated'>
    <named-connections>
      <named-connection name='c1'><connection class='sqlserver' server='s' dbname='DB' /></named-connection>
    </named-connections>
    <relation type='join'>
      <relation connection='c1' name='Q1' type='text'>SELECT * FROM dbo.Orders</relation>
      <relation connection='c1' name='Q2' type='text'>SELECT * FROM dbo.Customers</relation>
    </relation>
  </connection>
</datasource>"""
    pairs = {(s["schema"], s["table"]) for s in tab.parse_tds(tds)["sources"]}
    assert ("dbo", "Orders") in pairs
    assert ("dbo", "Customers") in pairs


# --------------------------------------------------------------------------------------
# fullName backfill: recover database/schema/table when the Metadata API populates only fullName
# --------------------------------------------------------------------------------------
def test_parse_full_name_bracketed_dotted_and_bare():
    assert tab._parse_full_name("[Sales].[dbo].[Orders]") == ("Sales", "dbo", "Orders")
    assert tab._parse_full_name("analytics.public.fact_sales") == ("analytics", "public", "fact_sales")
    assert tab._parse_full_name("[dbo].[Orders]") == ("", "dbo", "Orders")
    assert tab._parse_full_name("Orders") == ("", "", "Orders")
    assert tab._parse_full_name("") == ("", "", "")


def test_shape_sources_backfills_database_from_full_name():
    # Metadata API left database empty but populated fullName -- the strict source tier needs database.
    upstream = [{
        "name": "Orders",
        "schema": "",
        "fullName": "[ANALYTICS].[SALES].[Orders]",
        "connectionType": "snowflake",
        "database": {"name": "", "connectionType": "snowflake"},
    }]
    src = tab._shape_sources(upstream)[0]
    assert src["database"] == "ANALYTICS"
    assert src["schema"] == "SALES"
    assert src["table"] == "Orders"


def test_shape_sources_prefers_explicit_fields_over_full_name():
    # explicit database/schema/table win; fullName only backfills what is missing.
    upstream = [{
        "name": "Orders",
        "schema": "dbo",
        "fullName": "[WRONG].[bad].[Nope]",
        "connectionType": "sqlserver",
        "database": {"name": "RealDB"},
    }]
    src = tab._shape_sources(upstream)[0]
    assert src["database"] == "RealDB"
    assert src["schema"] == "dbo"
    assert src["table"] == "Orders"


# --------------------------------------------------------------------------------------
# Durability: corrupt archives, malformed XML, and pathological fullName degrade gracefully
# --------------------------------------------------------------------------------------
def _zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_tds_text_corrupt_zip_returns_none():
    # Looks like a ZIP (PK magic) but the central directory is junk -> None, not an exception.
    assert tab.extract_tds_text(b"PK\x03\x04" + b"\x00" * 48) is None


def test_extract_tds_text_zip_without_tds_returns_none():
    assert tab.extract_tds_text(_zip_bytes({"readme.txt": "no descriptor here"})) is None


def test_extract_tds_text_picks_tds_among_multiple_members():
    content = _zip_bytes({
        "Data/extract.hyper": b"\x00\x01binary-extract",
        "mydatasource.tds": SAMPLE_TDS,
        "notes.txt": "ignore me",
    })
    text = tab.extract_tds_text(content)
    assert text is not None and "<datasource" in text
    assert any(s["table"] == "Orders" for s in tab.parse_tds(text)["sources"])


def test_parse_tds_malformed_xml_is_graceful():
    out = tab.parse_tds("<datasource><<not-xml <relation table='[dbo].[X]' </datasource")
    assert set(out) == {"fields", "sources"}
    assert isinstance(out["fields"], list) and isinstance(out["sources"], list)


def test_parse_tds_none_and_empty_are_graceful():
    for bad in (None, "", "   ", "<datasource/>"):
        out = tab.parse_tds(bad)
        assert out == {"fields": [], "sources": []}


def test_parse_full_name_pathological_inputs_never_throw():
    assert tab._parse_full_name("...") == ("", "", "")
    assert tab._parse_full_name("[].[]") == ("", "", "")
    assert tab._parse_full_name("a..b") == ("", "a", "b")
    assert tab._parse_full_name("Orders.") == ("", "", "Orders")
    assert tab._parse_full_name("   ") == ("", "", "")


def test_shape_sources_skips_blank_table_and_dedupes():
    upstream = [
        {"name": "", "fullName": ""},  # no resolvable table -> dropped
        {"name": "Orders", "schema": "dbo", "database": {"name": "DB"}, "connectionType": "sqlserver"},
        {"name": "Orders", "schema": "dbo", "database": {"name": "DB"}, "connectionType": "sqlserver"},
    ]
    out = tab._shape_sources(upstream)
    assert len(out) == 1
    assert out[0]["table"] == "Orders"
