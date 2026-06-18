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
