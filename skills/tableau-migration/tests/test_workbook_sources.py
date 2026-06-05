"""Workbook-embedded datasource rebuild tests (offline, stdlib-only, deterministic).

Fixtures are built as real Tableau-shaped ``.twb`` / ``.twbx`` documents (with the UTF-8 BOM
Tableau writes) in a temp dir, so the path-loading, BOM-stripping, zip-reading, and packaged
CSV header auto-detection paths are all exercised without any network or credentials.
"""
import json
import os
import re
import zipfile

import pytest

from workbook_sources import (
    enumerate_workbook_datasources,
    rebuild_workbook_models,
    build_flatfile_model,
    build_cross_db_model,
    _is_physical_join,
    _manifest,
    main,
)
from connection_to_m import parse_tds
import xml.etree.ElementTree as ET


# -- fixtures (structurally faithful, trimmed) --------------------------------
EXCEL_DS = """
    <datasource name='excel.1' caption='Sample - Superstore'>
      <connection class='excel-direct' filename='Sample - Superstore.xlsx' directory='C:\\\\Data'>
        <relation type='collection'>
          <relation connection='excel-direct.0' name='Orders' table='[Orders$]' type='table'/>
          <relation connection='excel-direct.0' name='People' table='[People$]' type='table'/>
        </relation>
        <metadata-records>
          <metadata-record class='column'><remote-name>Row ID</remote-name>
            <local-name>[Row ID]</local-name><parent-name>[Orders$]</parent-name><local-type>integer</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Sales</remote-name>
            <local-name>[Sales]</local-name><parent-name>[Orders$]</parent-name><local-type>real</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Order Date</remote-name>
            <local-name>[Order Date]</local-name><parent-name>[Orders$]</parent-name><local-type>date</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Person</remote-name>
            <local-name>[Person]</local-name><parent-name>[People$]</parent-name><local-type>string</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""

CSV_DS = """
    <datasource name='csv.1' caption='Sales Commission'>
      <connection class='textscan' filename='Sales Commission.csv' directory='C:\\\\Data'
                  separator=',' charset='utf-8'>
        <relation name='Sales Commission' table='[Sales Commission#csv]' type='table'/>
        <metadata-records>
          <metadata-record class='column'><remote-name>Rep</remote-name>
            <local-name>[Rep]</local-name><parent-name>[Sales Commission#csv]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Commission Rate</remote-name>
            <local-name>[Commission Rate]</local-name><parent-name>[Sales Commission#csv]</parent-name><local-type>real</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""

SQLPROXY_DS = """
    <datasource name='federated.pub' caption='Corp Sales (Published)'>
      <connection class='sqlproxy' server='tableau.example.com' dbname='analytics-site'
                  server-ds-friendly-name='Corp Sales' port='443' channel='https'/>
    </datasource>"""

SQLSERVER_DS = """
    <datasource name='federated.sql' caption='Live Orders'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='srv' name='sqlserver.0'>
            <connection class='sqlserver' dbname='Sales' server='srv.example.com' username='svc'/>
          </named-connection>
        </named-connections>
        <relation connection='sqlserver.0' name='Orders' table='[dbo].[Orders]' type='table'/>
        <metadata-records>
          <metadata-record class='column'><remote-name>Order ID</remote-name>
            <local-name>[Order ID]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Quantity</remote-name>
            <local-name>[Quantity]</local-name><parent-name>[Orders]</parent-name><local-type>integer</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""

PARAMETERS_DS = """
    <datasource name='Parameters' hasconnection='false' caption='Parameters'>
      <column caption='Top N' name='[Parameters].[Param 1]' datatype='integer' role='measure'/>
    </datasource>"""

# A published .tds the resolution seam hands back (a normal standalone SQL Server datasource).
RESOLVED_TDS = """<?xml version='1.0' encoding='utf-8'?>
<datasource formatted-name='Corp Sales' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.r'>
        <connection class='sqlserver' dbname='Corp' server='corp.database.windows.net'/>
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.r' name='Sales' table='[dbo].[Sales]' type='table'/>
    <metadata-records>
      <metadata-record class='column'><remote-name>Amount</remote-name>
        <local-name>[Amount]</local-name><parent-name>[Sales]</parent-name><local-type>real</local-type></metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# A CROSS-DATABASE JOIN: one federated datasource whose <relation type='join'> tree spans TWO
# <named-connection>s (Snowflake JOIN a local CSV), each leaf relation carrying a different
# connection= id. This cannot be one Import partition -> must fall back, not be rebuilt wrong.
CROSS_DB_DS = """
    <datasource name='federated.xdb' caption='Blended Sales'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='snow' name='snowflake.0'>
            <connection class='snowflake' server='acct.snowflakecomputing.com' dbname='SALES' warehouse='WH'/>
          </named-connection>
          <named-connection caption='local' name='textscan.0'>
            <connection class='textscan' filename='targets.csv' directory='C:\\\\Data'/>
          </named-connection>
        </named-connections>
        <relation join='inner' type='join'>
          <clause type='join'>
            <expression op='='>
              <expression op='[snowflake.0].[ORDERS].[REGION]'/>
              <expression op='[textscan.0].[targets#csv].[Region]'/>
            </expression>
          </clause>
          <relation connection='snowflake.0' name='ORDERS' table='[SALES].[ORDERS]' type='table'/>
          <relation connection='textscan.0' name='targets' table='[targets#csv]' type='table'/>
        </relation>
        <metadata-records>
          <metadata-record class='column'><remote-name>REGION</remote-name>
            <local-name>[REGION]</local-name><parent-name>[ORDERS]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Target</remote-name>
            <local-name>[Target]</local-name><parent-name>[targets#csv]</parent-name><local-type>real</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""


def _workbook(*datasources):
    inner = "".join(datasources)
    return (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<workbook version='18.1'>\n"
        f"  <datasources>{inner}\n  </datasources>\n"
        "</workbook>\n"
    )


def _write_bom(path, text):
    """Write ``text`` with the UTF-8 BOM Tableau emits (forces the utf-8-sig read path)."""
    with open(path, "wb") as fh:
        fh.write(b"\xef\xbb\xbf" + text.encode("utf-8"))


# -- enumeration ---------------------------------------------------------------
def test_enumeration_skips_parameters_and_classifies():
    wb = _workbook(PARAMETERS_DS, EXCEL_DS, CSV_DS, SQLPROXY_DS, SQLSERVER_DS)
    entries = enumerate_workbook_datasources(wb)

    captions = {e["caption"] for e in entries}
    assert "Parameters" not in captions          # pseudo-datasource skipped
    assert len(entries) == 4

    by_caption = {e["caption"]: e for e in entries}
    assert by_caption["Sample - Superstore"]["classification"] == "flat_file"
    assert by_caption["Sample - Superstore"]["file_type"] == "Excel"
    assert by_caption["Sales Commission"]["classification"] == "flat_file"
    assert by_caption["Sales Commission"]["file_type"] == "CSV"
    assert by_caption["Corp Sales (Published)"]["classification"] == "published_reference"
    assert by_caption["Live Orders"]["classification"] == "relational"


def test_enumeration_captures_typed_columns_from_metadata():
    entries = enumerate_workbook_datasources(_workbook(EXCEL_DS))
    rels = {r["name"]: r for r in entries[0]["relations"]}
    # multi-sheet Excel collection -> independent tables, each typed from metadata
    assert set(rels) == {"Orders", "People"}
    orders_types = {c["model_name"]: c["tmdl_type"] for c in rels["Orders"]["columns"]}
    assert orders_types["Row_ID"] == "int64"
    assert orders_types["Sales"] == "double"
    assert orders_types["Order_Date"] == "dateTime"


def test_enumeration_accepts_file_path_with_bom(tmp_path):
    twb = tmp_path / "wb.twb"
    _write_bom(str(twb), _workbook(EXCEL_DS, CSV_DS))
    entries = enumerate_workbook_datasources(str(twb))
    assert {e["caption"] for e in entries} == {"Sample - Superstore", "Sales Commission"}


# -- flat-file Excel -----------------------------------------------------------
def test_excel_flatfile_m_shape_and_filepath_parameter():
    res = rebuild_workbook_models(_workbook(EXCEL_DS))
    model = res["models"]["Sample - Superstore"]
    assert model["status"] == "migrated_with_followups"

    orders = model["parts"]["definition/tables/Orders.tmdl"]
    assert 'Excel.Workbook(File.Contents(#"FilePath"), null, true)' in orders
    assert 'Source{[Item="Orders", Kind="Sheet"]}[Data]' in orders     # $ stripped, sheet nav
    assert "Table.PromoteHeaders(Navigation, [PromoteAllScalars=true])" in orders
    # raw header renamed to the clean model column name the report binds to
    assert '{"Row ID", "Row_ID"}' in orders
    # typed from metadata, not deferred to Power BI inference
    assert "dataType: int64" in orders
    assert '{"Row_ID", Int64.Type}' in orders

    exprs = model["parts"]["definition/expressions.tmdl"]
    assert "expression FilePath =" in exprs
    assert "IsParameterQuery=true" in exprs
    assert "Sample - Superstore.xlsx" in exprs   # default = original directory/filename

    model_tmdl = model["parts"]["definition/model.tmdl"]
    assert "ref table Orders" in model_tmdl
    assert "ref table People" in model_tmdl
    assert "ref table _Measures" in model_tmdl
    assert 'PBI_QueryOrder = ["FilePath"]' in model_tmdl


def test_excel_flatfile_has_repoint_followup():
    res = rebuild_workbook_models(_workbook(EXCEL_DS))
    msgs = [f["message"] for f in res["followups"] if f["datasource"] == "Sample - Superstore"]
    assert any("Repoint the FilePath parameter" in m for m in msgs)


# -- flat-file CSV -------------------------------------------------------------
def test_csv_flatfile_m_shape():
    res = rebuild_workbook_models(_workbook(CSV_DS))
    tmdl = res["models"]["Sales Commission"]["parts"]["definition/tables/Sales Commission.tmdl"]
    assert 'Csv.Document(File.Contents(#"FilePath")' in tmdl
    assert 'Delimiter=","' in tmdl
    assert "Columns=2" in tmdl
    assert "Encoding=65001" in tmdl           # utf-8 -> code page
    assert "Table.PromoteHeaders(Source, [PromoteAllScalars=true])" in tmdl
    assert '{"Commission Rate", "Commission_Rate"}' in tmdl
    assert '{"Commission_Rate", type number}' in tmdl


# -- published reference + resolution seam ------------------------------------
def test_sqlproxy_marked_published_unresolved_without_resolver():
    res = rebuild_workbook_models(_workbook(SQLPROXY_DS))
    model = res["models"]["Corp Sales (Published)"]
    assert model["status"] == "published_unresolved"
    assert model["parts"] == {}
    assert model["report"]["referenced_name"] == "Corp Sales"
    assert any("Corp Sales" in f["message"] for f in res["followups"])


def test_sqlproxy_resolution_seam_runs_the_spine():
    seen = {}

    def resolver(name):
        seen["name"] = name
        return RESOLVED_TDS

    res = rebuild_workbook_models(_workbook(SQLPROXY_DS), resolve_published=resolver)
    model = res["models"]["Corp Sales (Published)"]
    assert seen["name"] == "Corp Sales"                 # seam called with the referenced name
    assert model["status"] in ("migrated", "migrated_with_followups")
    sales = model["parts"]["definition/tables/Sales.tmdl"]
    assert 'Sql.Database(#"Server", #"Database")' in sales
    assert model["report"]["resolved_published"] == "Corp Sales"


# -- relational embedded datasource -> spine ----------------------------------
def test_relational_federated_rebuilds_via_spine():
    res = rebuild_workbook_models(_workbook(SQLSERVER_DS))
    model = res["models"]["Live Orders"]
    assert model["status"] == "migrated"               # sqlserver is fully supported
    orders = model["parts"]["definition/tables/Orders.tmdl"]
    assert 'Sql.Database(#"Server", #"Database")' in orders
    assert "mode: directQuery" in orders


# -- per-source error isolation -----------------------------------------------
def test_one_failing_source_does_not_abort_the_rest():
    def exploding_resolver(name):
        raise RuntimeError("tableau server unreachable")

    res = rebuild_workbook_models(
        _workbook(SQLPROXY_DS, EXCEL_DS, SQLSERVER_DS),
        resolve_published=exploding_resolver)

    assert res["models"]["Corp Sales (Published)"]["status"] == "error"
    assert "tableau server unreachable" in res["models"]["Corp Sales (Published)"]["error"]
    # the other two datasources still migrate cleanly
    assert res["models"]["Sample - Superstore"]["status"] == "migrated_with_followups"
    assert res["models"]["Live Orders"]["status"] == "migrated"


# -- structural / honesty fallbacks -------------------------------------------
def test_spatial_source_falls_back_not_guessed():
    spatial = """
    <datasource name='spatial.1' caption='Territories'>
      <connection class='ogrdirect' filename='zones.shp'>
        <relation name='zones' table='[zones]' type='table'/>
      </connection>
    </datasource>"""
    res = rebuild_workbook_models(_workbook(spatial))
    model = res["models"]["Territories"]
    assert model["status"] == "fallback"
    assert model["parts"] == {}


def test_flatfile_clean_name_collision_falls_back():
    # 'Order ID' and 'Order_ID' both clean to 'Order_ID' -> ambiguous, refuse to guess.
    collide = """
    <datasource name='excel.c' caption='Collide'>
      <connection class='excel-direct' filename='c.xlsx'>
        <relation name='T' table='[T$]' type='table'/>
        <metadata-records>
          <metadata-record class='column'><remote-name>Order ID</remote-name>
            <local-name>[Order ID]</local-name><parent-name>[T$]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Order_ID</remote-name>
            <local-name>[Order_ID]</local-name><parent-name>[T$]</parent-name><local-type>string</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""
    res = rebuild_workbook_models(_workbook(collide))
    assert res["models"]["Collide"]["status"] == "fallback"


# -- cross-database join detection (multi-source join rebuild deferred) -------
def test_cross_db_join_detected_in_enumeration():
    entry = enumerate_workbook_datasources(_workbook(CROSS_DB_DS))[0]
    assert entry["cross_db_join"] is True
    # the SET of distinct named-connections the relation tree spans
    assert set(entry["named_connections"]) == {"snowflake.0", "textscan.0"}
    assert entry["classification"] == "cross_db_join"


def test_cross_db_join_falls_back_with_reason_not_one_partition():
    res = rebuild_workbook_models(_workbook(CROSS_DB_DS))
    model = res["models"]["Blended Sales"]
    assert model["status"] == "fallback"
    assert model["parts"] == {}                      # never emitted as a single Import partition
    assert model["report"]["reason"] == "cross_db_join"
    assert set(model["report"]["connection_classes"]) == {"snowflake", "textscan"}

    msgs = [f["message"] for f in res["followups"] if f["datasource"] == "Blended Sales"]
    assert any("Cross-database join" in m for m in msgs)
    assert any(("lakehouse" in m or "composite model" in m) for m in msgs)


def test_single_connection_relational_not_flagged_cross_db():
    # a normal single-connection federated source must NOT be misread as a cross-database join
    entry = enumerate_workbook_datasources(_workbook(SQLSERVER_DS))[0]
    assert entry["cross_db_join"] is False
    assert entry["named_connections"] == ["sqlserver.0"]
    assert entry["classification"] == "relational"


def test_cross_db_join_isolated_from_healthy_sources():
    # a cross-database join falls back but never blocks the other datasources in the estate
    res = rebuild_workbook_models(_workbook(CROSS_DB_DS, EXCEL_DS, SQLSERVER_DS))
    assert res["models"]["Blended Sales"]["status"] == "fallback"
    assert res["models"]["Sample - Superstore"]["status"] == "migrated_with_followups"
    assert res["models"]["Live Orders"]["status"] == "migrated"


# -- cross-database join LOGICAL rebuild (per-source land + model relationships) ----
# A SINGLE federated datasource whose <relation type='collection'> groups THREE INDEPENDENT
# tables, each from a DIFFERENT cloud connection (Azure SQL / Snowflake / Databricks). There is
# NO physical <clause> join -- the join keys live in a top-level <relationships> block. This is
# the modern logical/noli model: each side is landed via its own per-connector M and the keys
# become model relationships (NOT a deferred fallback). Placeholder hosts only -- no real
# credentials. Snowflake warehouse is empty (-> P4 prompt, must not fail). The two join keys
# exercise a cross-DB case mismatch (Order_ID vs ORDER_ID) and a disambiguated/renamed key
# (Region vs the local name "Region (people)").
XDB_LOGICAL_DS = """
    <datasource caption='Orders+ (Multiple Connections)' inline='true' name='federated.multi'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='HOST_A' name='azure_sqldb.A'>
            <connection class='azure_sqldb' authentication='sqlserver' dbname='DB_A'
                        server='host-a.placeholder.net' username='USER_A'/>
          </named-connection>
          <named-connection caption='HOST_B' name='snowflake.B'>
            <connection class='snowflake' authentication='Username Password' dbname='DB_B'
                        schema='PUBLIC' server='host-b.placeholder.net' username='USER_B' warehouse=''/>
          </named-connection>
          <named-connection caption='HOST_C' name='databricks.C'>
            <connection class='databricks' authentication='oauth' dbname='CATALOG_C' schema='default'
                        server='host-c.placeholder.net' http-path='/sql/1.0/warehouses/WID'/>
          </named-connection>
        </named-connections>
        <relation type='collection'>
          <relation connection='azure_sqldb.A' name='Orders'  table='[dbo].[Orders]' type='table'/>
          <relation connection='snowflake.B'   name='RETURNS' table='[PUBLIC].[RETURNS]' type='table'/>
          <relation connection='databricks.C'  name='people'  table='[default].[people]' type='table'/>
        </relation>
        <metadata-records>
          <metadata-record class='column'><remote-name>Order_ID</remote-name>
            <local-name>[Order_ID]</local-name><parent-name>[Orders]</parent-name><local-type>integer</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Region</remote-name>
            <local-name>[Region]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>ORDER_ID</remote-name>
            <local-name>[ORDER_ID]</local-name><parent-name>[RETURNS]</parent-name><local-type>integer</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Reason</remote-name>
            <local-name>[Reason]</local-name><parent-name>[RETURNS]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Region</remote-name>
            <local-name>[Region (people)]</local-name><parent-name>[people]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Manager</remote-name>
            <local-name>[Manager]</local-name><parent-name>[people]</parent-name><local-type>string</local-type></metadata-record>
        </metadata-records>
      </connection>
      <relationships>
        <relationship><expression op='='>
          <expression op='[Orders].[Order_ID]'/><expression op='[RETURNS].[ORDER_ID]'/></expression></relationship>
        <relationship><expression op='='>
          <expression op='[Orders].[Region]'/><expression op='[people].[Region (people)]'/></expression></relationship>
      </relationships>
    </datasource>"""


def test_cross_db_logical_detected_in_enumeration():
    entry = enumerate_workbook_datasources(_workbook(XDB_LOGICAL_DS))[0]
    assert entry["cross_db_join"] is True
    assert entry["classification"] == "cross_db_join"
    assert set(entry["named_connections"]) == {"azure_sqldb.A", "snowflake.B", "databricks.C"}


def test_physical_vs_logical_split():
    # the modern logical (collection) shape is rebuildable; a physical <clause> join is not
    assert _is_physical_join(ET.fromstring(CROSS_DB_DS)) is True
    assert _is_physical_join(ET.fromstring(XDB_LOGICAL_DS)) is False


def test_cross_db_logical_rebuilds_each_side_with_its_own_connector():
    res = rebuild_workbook_models(_workbook(XDB_LOGICAL_DS))
    model = res["models"]["Orders+ (Multiple Connections)"]
    assert model["status"] == "migrated_with_followups"
    assert model["report"]["kind"] == "cross_db_join_logical"

    # each side lands through its OWN per-connector M, with per-side renamed connection params
    orders = model["parts"]["definition/tables/Orders.tmdl"]
    assert 'Sql.Database(#"Server_Orders", #"Database_Orders")' in orders
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in orders
    assert "mode: directQuery" in orders

    returns = model["parts"]["definition/tables/RETURNS.tmdl"]
    assert 'Snowflake.Databases(#"Server_RETURNS", #"Warehouse_RETURNS")' in returns
    assert '[Name="DB_B", Kind="Database"]' in returns
    assert '[Name="RETURNS", Kind="Table"]' in returns

    people = model["parts"]["definition/tables/people.tmdl"]
    assert 'Databricks.Catalogs(#"Server_people", #"HttpPath_people")' in people
    assert '[Name="CATALOG_C", Kind="Database"]' in people

    # NO single federated cross-database query: three independent partitions, no shared param tokens
    exprs = model["parts"]["definition/expressions.tmdl"]
    for name in ("Server_Orders", "Database_Orders", "Server_RETURNS", "Warehouse_RETURNS",
                 "Server_people", "HttpPath_people"):
        assert f"expression {name} =" in exprs
    # the un-suffixed (colliding) tokens never survive into the combined model
    assert '#"Server"' not in orders + returns + people
    assert '#"Database"' not in orders + returns + people

    model_tmdl = model["parts"]["definition/model.tmdl"]
    for t in ("Orders", "RETURNS", "people", "_Measures"):
        assert f"ref table {t}" in model_tmdl


def test_cross_db_logical_emits_relationships_tolerating_case_and_rename():
    res = rebuild_workbook_models(_workbook(XDB_LOGICAL_DS))
    model = res["models"]["Orders+ (Multiple Connections)"]
    rels = model["parts"]["definition/relationships.tmdl"]
    # case-mismatched key across DBs resolves by relation+field (Order_ID -> ORDER_ID)
    assert "fromColumn: Orders.Order_ID" in rels
    assert "toColumn: RETURNS.ORDER_ID" in rels
    # disambiguated/renamed key: the local name "Region (people)" binds to the model column "Region"
    assert "fromColumn: Orders.Region" in rels
    assert "toColumn: people.Region" in rels

    keys = model["report"]["join_keys"]
    assert {"from_table": "Orders", "from_col": "Order_ID",
            "to_table": "RETURNS", "to_col": "ORDER_ID"} in keys
    assert model["report"]["unresolved_join_keys"] == []


def test_cross_db_logical_records_each_side_in_report():
    res = rebuild_workbook_models(_workbook(XDB_LOGICAL_DS))
    report = res["models"]["Orders+ (Multiple Connections)"]["report"]
    by_class = {c["connection_class"]: c for c in report["connections"]}
    assert by_class["azure_sqldb"]["table"] == "Orders"
    assert by_class["azure_sqldb"]["database"] == "DB_A"
    assert by_class["snowflake"]["schema"] == "PUBLIC"
    assert by_class["snowflake"]["server"] == "host-b.placeholder.net"
    assert by_class["databricks"]["database"] == "CATALOG_C"
    assert all(c["mode"] == "DirectQuery" for c in report["connections"])


def test_cross_db_logical_empty_snowflake_warehouse_prompts_not_fails():
    res = rebuild_workbook_models(_workbook(XDB_LOGICAL_DS))
    model = res["models"]["Orders+ (Multiple Connections)"]
    # the empty warehouse must NOT abort the rebuild
    assert model["status"] == "migrated_with_followups"
    assert 'expression Warehouse_RETURNS = ""' in model["parts"]["definition/expressions.tmdl"]
    msgs = [f["message"] for f in res["followups"]
            if f["datasource"] == "Orders+ (Multiple Connections)"]
    assert any("Snowflake warehouse" in m and "Warehouse_RETURNS" in m for m in msgs)
    assert any("cardinality" in m for m in msgs)


def test_cross_db_logical_unresolvable_key_reported_not_emitted_wrong():
    # a relationship operand that names no real field stays unresolved (reported), and the rest
    # of the model is still built honestly.
    variant = XDB_LOGICAL_DS.replace(
        "<expression op='[Orders].[Region]'/><expression op='[people].[Region (people)]'/>",
        "<expression op='[Orders].[Nonexistent]'/><expression op='[people].[Region (people)]'/>")
    res = rebuild_workbook_models(_workbook(variant))
    model = res["models"]["Orders+ (Multiple Connections)"]
    assert model["status"] == "migrated_with_followups"
    rels = model["parts"]["definition/relationships.tmdl"]
    assert "Orders.Order_ID" in rels                  # the good key still emits
    assert "people.Region" not in rels                # the unresolvable one does not
    assert len(model["report"]["unresolved_join_keys"]) == 1
    msgs = [f["message"] for f in res["followups"]]
    assert any("Could not bind join key" in m for m in msgs)


def test_cross_db_logical_isolated_from_healthy_sources():
    res = rebuild_workbook_models(_workbook(XDB_LOGICAL_DS, EXCEL_DS, SQLSERVER_DS))
    assert res["models"]["Orders+ (Multiple Connections)"]["status"] == "migrated_with_followups"
    assert res["models"]["Sample - Superstore"]["status"] == "migrated_with_followups"
    assert res["models"]["Live Orders"]["status"] == "migrated"


def test_cross_db_logical_direct_builder_returns_parts():
    # the public builder seam the orchestrator calls returns a complete model definition
    out = build_cross_db_model(XDB_LOGICAL_DS, model_name="Orders+ (Multiple Connections)")
    assert out["status"] == "migrated_with_followups"
    assert set(out["report"]["tables"]) == {"Orders", "RETURNS", "people"}
    assert "definition/model.tmdl" in out["parts"]
    assert "definition/relationships.tmdl" in out["parts"]


def test_cross_db_logical_report_is_storage_neutral_and_flags_limited_relationships():
    # The realization is storage-mode-agnostic: the report describes the composite from the actual
    # per-side modes the shared chooser picked (no hardwired single mode, no forced Delta). Three
    # live DirectQuery sources mean cross-source relationships become LIMITED (weak); that must be
    # surfaced honestly with the two strong-relationship alternatives, not silently chosen.
    out = build_cross_db_model(XDB_LOGICAL_DS, model_name="Orders+ (Multiple Connections)")
    report = out["report"]
    assert report["storage_mode"] == "Composite (DirectQuery)"   # derived from per-side modes
    assert report["relationship_fidelity"] == "limited"
    assert report["limited_relationships"]                       # the cross-source keys are listed
    msgs = [f for f in out["followups"]]
    fidelity = [m for m in msgs if "LIMITED" in m]
    assert fidelity, "composite-model fidelity follow-up missing"
    note = fidelity[0]
    assert "Import" in note and "DirectLake" in note            # both strong-relationship options offered
    # Delta is offered only as one alternative -- it is never the default realization.
    assert "definition/relationships.tmdl" in out["parts"]


# A minimal TWO-connection logical datasource (two Azure SQL hosts -> still cross-DB by connection
# span) used to exercise the harder edge cases: identifier-unsafe table names, casefold-only column
# collisions, duplicate emitted display names, and compound/unsupported relationship predicates.
def _logical_two(rel_a, cols_a, rel_b, cols_b, relationships):
    def _records(parent, cols):
        return "".join(
            f"<metadata-record class='column'><remote-name>{r}</remote-name>"
            f"<local-name>[{l}]</local-name><parent-name>[{parent}]</parent-name>"
            f"<local-type>{t}</local-type></metadata-record>"
            for r, l, t in cols)
    return f"""
    <datasource caption='Edge' name='federated.edge'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='A' name='azure_sqldb.A'>
            <connection class='azure_sqldb' dbname='DB_A' server='a.placeholder.net'/>
          </named-connection>
          <named-connection caption='B' name='azure_sqldb.B'>
            <connection class='azure_sqldb' dbname='DB_B' server='b.placeholder.net'/>
          </named-connection>
        </named-connections>
        <relation type='collection'>
          <relation connection='azure_sqldb.A' name="{rel_a}" table='[dbo].[{rel_a}]' type='table'/>
          <relation connection='azure_sqldb.B' name="{rel_b}" table='[dbo].[{rel_b}]' type='table'/>
        </relation>
        <metadata-records>
          {_records(rel_a, cols_a)}
          {_records(rel_b, cols_b)}
        </metadata-records>
      </connection>
      <relationships>{relationships}</relationships>
    </datasource>"""


def test_cross_db_logical_param_suffix_is_identifier_safe():
    # a relation name with apostrophes/dots/brackets must not leak into the (unquoted) expression
    # name or the PBI_QueryOrder annotation, or the TMDL would be invalid. (The messy text lives in
    # the relation NAME, which drives the param suffix; the table item stays clean.)
    ds = """
    <datasource caption='Edge' name='federated.edge'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='A' name='azure_sqldb.A'>
            <connection class='azure_sqldb' dbname='DB_A' server='a.placeholder.net'/>
          </named-connection>
          <named-connection caption='B' name='azure_sqldb.B'>
            <connection class='azure_sqldb' dbname='DB_B' server='b.placeholder.net'/>
          </named-connection>
        </named-connections>
        <relation type='collection'>
          <relation connection='azure_sqldb.A' name="O'Brien.Sales[2024]" table='[dbo].[Sales]' type='table'/>
          <relation connection='azure_sqldb.B' name='People' table='[dbo].[People]' type='table'/>
        </relation>
        <metadata-records>
          <metadata-record class='column'><remote-name>K</remote-name>
            <local-name>[K]</local-name><parent-name>[Sales]</parent-name><local-type>integer</local-type></metadata-record>
          <metadata-record class='column'><remote-name>K</remote-name>
            <local-name>[K]</local-name><parent-name>[People]</parent-name><local-type>integer</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""
    model = rebuild_workbook_models(_workbook(ds))["models"]["Edge"]
    assert model["status"] == "migrated_with_followups"
    exprs = model["parts"]["definition/expressions.tmdl"]
    names = re.findall(r"(?m)^expression (\S+) =", exprs)
    assert names                                   # params were emitted
    for n in names:
        assert re.fullmatch(r"[A-Za-z0-9_]+", n), f"unsafe expression name: {n}"
    # the messy original text never reaches the query-order annotation
    order_line = [ln for ln in model["parts"]["definition/model.tmdl"].splitlines()
                  if "PBI_QueryOrder" in ln][0]
    assert "O'Brien" not in order_line and "[2024]" not in order_line


def test_cross_db_logical_casefold_column_collision_is_ambiguous_not_wrong():
    # 'Region' and 'REGION' are DISTINCT model columns but collide under casefold; a key on that
    # name must be left unresolved rather than silently bound to the first one.
    rels = ("<relationship><expression op='='>"
            "<expression op='[A].[Region]'/><expression op='[B].[Key]'/></expression></relationship>")
    ds = _logical_two(
        "A", [("Region", "Region", "string"), ("REGION", "REGION", "string")],
        "B", [("Key", "Key", "integer")],
        rels)
    model = rebuild_workbook_models(_workbook(ds))["models"]["Edge"]
    assert model["status"] == "migrated_with_followups"
    assert "definition/relationships.tmdl" not in model["parts"]   # nothing bound
    assert len(model["report"]["unresolved_join_keys"]) == 1


def test_cross_db_logical_duplicate_display_names_do_not_overwrite():
    # two leaves that share a name from different connections must each get a distinct table part
    # and a distinct ref, never silently clobbering one another.
    ds = _logical_two(
        "Shared", [("K", "K", "integer")],
        "Shared", [("K", "K", "integer")],
        "")
    model = rebuild_workbook_models(_workbook(ds))["models"]["Edge"]
    assert model["status"] == "migrated_with_followups"
    table_parts = [p for p in model["parts"] if p.startswith("definition/tables/")
                   and not p.endswith("_Measures.tmdl")]
    assert len(table_parts) == 2                                   # both landed, no overwrite
    assert len(set(model["report"]["tables"])) == 2               # distinct emitted names


def test_cross_db_logical_compound_relationship_is_surfaced_not_dropped():
    # a compound (AND of equalities) predicate is not a simple key we can map -> it must surface as
    # an unresolved follow-up rather than vanish silently.
    rels = ("<relationship><expression op='AND'>"
            "<expression op='='><expression op='[A].[K]'/><expression op='[B].[K]'/></expression>"
            "<expression op='='><expression op='[A].[J]'/><expression op='[B].[J]'/></expression>"
            "</expression></relationship>")
    ds = _logical_two(
        "A", [("K", "K", "integer"), ("J", "J", "integer")],
        "B", [("K", "K", "integer"), ("J", "J", "integer")],
        rels)
    model = rebuild_workbook_models(_workbook(ds))["models"]["Edge"]
    assert model["status"] == "migrated_with_followups"
    assert "definition/relationships.tmdl" not in model["parts"]
    assert len(model["report"]["unresolved_join_keys"]) == 1
    assert any("Could not bind join key" in f["message"] for f in
               rebuild_workbook_models(_workbook(ds))["followups"])


# -- packaged .twbx: zip read + CSV delimiter auto-detection -------------------
def _make_twbx(path, workbook_xml, data_files):
    """Write a real .twbx zip: inner .twb (BOM) + packaged data files under Data/."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("workbook.twb", b"\xef\xbb\xbf" + workbook_xml.encode("utf-8"))
        for name, content in data_files.items():
            zf.writestr(name, content)


def test_twbx_reads_inner_workbook_and_autodetects_csv_delimiter(tmp_path):
    # textscan with NO separator attribute -> delimiter must be sniffed from the packaged header.
    csv_ds = """
    <datasource name='csv.semi' caption='Regional'>
      <connection class='textscan' filename='regional.csv' directory='Data' charset='utf-8'>
        <relation name='Regional' table='[regional#csv]' type='table'/>
        <metadata-records>
          <metadata-record class='column'><remote-name>Region</remote-name>
            <local-name>[Region]</local-name><parent-name>[regional#csv]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Total</remote-name>
            <local-name>[Total]</local-name><parent-name>[regional#csv]</parent-name><local-type>real</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""
    twbx = tmp_path / "estate.twbx"
    _make_twbx(str(twbx), _workbook(csv_ds),
               {"Data/regional.csv": b"Region;Total;Share\nWest;100;0.5\n",
                "Data/ignored.xlsx": b"PK\x03\x04 not a real xlsx, only here to exist"})

    entries = enumerate_workbook_datasources(str(twbx))
    assert entries[0]["caption"] == "Regional"
    assert entries[0]["package_path"] == str(twbx)

    res = rebuild_workbook_models(str(twbx))
    tmdl = res["models"]["Regional"]["parts"]["definition/tables/Regional.tmdl"]
    assert 'Delimiter=";"' in tmdl          # auto-detected from the packaged header line
    assert "Columns=3" in tmdl              # header had 3 fields


# -- CLI manifest is summary-only (no GUIDs / secrets) ------------------------
def test_manifest_has_no_guids_or_raw_parts():
    res = rebuild_workbook_models(_workbook(EXCEL_DS, SQLSERVER_DS))
    manifest = _manifest(res)
    blob = json.dumps(manifest)
    assert "lineageTag" not in blob          # no TMDL part text / GUIDs leak into the manifest
    assert "Sql.Database" not in blob
    assert manifest["model_count"] == 2
    statuses = {m["folder"]: m["status"] for m in manifest["models"]}
    assert statuses["Sample - Superstore.SemanticModel"] == "migrated_with_followups"


def test_cli_writes_folders(tmp_path, capsys):
    twb = tmp_path / "wb.twb"
    _write_bom(str(twb), _workbook(EXCEL_DS, SQLSERVER_DS))
    out = tmp_path / "bundle"
    rc = main([str(twb), "-o", str(out)])
    assert rc == 0

    # one <caption>.SemanticModel folder per migrated datasource, with the core parts
    assert (out / "Sample - Superstore.SemanticModel" / "definition" / "model.tmdl").exists()
    assert (out / "Live Orders.SemanticModel" / "definition" / "tables" / "Orders.tmdl").exists()

    printed = json.loads(capsys.readouterr().out)
    assert printed["model_count"] == 2


# -- folder de-duplication for repeated captions ------------------------------
def test_duplicate_captions_get_distinct_folders():
    res = rebuild_workbook_models(_workbook(SQLSERVER_DS, SQLSERVER_DS.replace("federated.sql", "federated.sql2")))
    folders = [rec["folder"] for rec in res["models"].values()]
    assert len(folders) == len(set(folders))   # no collision / silent overwrite
