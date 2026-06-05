"""Tableau ``.tds`` parsing + M-emission tests (realistic XML fixtures)."""
import pytest

from connection_to_m import (
    build_m_field_resolver,
    connection_details_for_bind,
    emit_connection_parameters,
    emit_m_partition_source,
    emit_table_tmdl_m,
    parse_tds,
    tableau_type_to_tmdl,
)

# -- fixtures (trimmed but structurally faithful .tds documents) ---------------
LIVE_SQLSERVER = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='myserver' name='sqlserver.0a1b2c'>
        <connection authentication='sqlserver' class='sqlserver' dbname='Superstore'
                    server='myserver.database.windows.net' username='svc' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order ID</remote-name>
        <local-name>[Order ID]</local-name>
        <parent-name>[Orders]</parent-name>
        <local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name>
        <local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name>
        <local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Quantity</remote-name>
        <local-name>[Quantity]</local-name>
        <parent-name>[Orders]</parent-name>
        <local-type>integer</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

EXTRACT_OVER_SQLSERVER = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='SuperstoreExtract' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.x'>
        <connection class='sqlserver' dbname='Superstore' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.x' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <extract enabled='true'>
    <connection class='hyper' dbname='Data/Datasources/Superstore.hyper' />
  </extract>
</datasource>"""

CUSTOM_SQL = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='CustomSQL' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.y'>
        <connection class='sqlserver' dbname='Sales' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.y' name='Custom SQL Query' type='text'>SELECT "Region", SUM(Sales) AS Sales FROM Orders GROUP BY "Region"</relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

JOIN_TREE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Joined' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.z'>
        <connection class='sqlserver' dbname='Sales' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation join='inner' type='join'>
      <relation name='Orders' table='[dbo].[Orders]' type='table' />
      <relation name='People' table='[dbo].[People]' type='table' />
      <clause type='join'><expression op='='></expression></clause>
    </relation>
  </connection>
</datasource>"""

SNOWFLAKE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Snow' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='acct' name='snowflake.a'>
        <connection class='snowflake' dbname='ANALYTICS' server='acct.snowflakecomputing.com'
                    warehouse='COMPUTE_WH' />
      </named-connection>
    </named-connections>
    <relation name='ORDERS' table='[PUBLIC].[ORDERS]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>SALES</remote-name><local-name>[SALES]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

ORACLE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Ora' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='ora' name='oracle.a'>
        <connection class='oracle' server='oradb.example.com:1521/ORCL' username='app' />
      </named-connection>
    </named-connections>
    <relation name='ORDERS' table='[SALES].[ORDERS]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>ORDER_ID</remote-name><local-name>[ORDER_ID]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>SALES</remote-name><local-name>[SALES]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

TERADATA = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='TD' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='td' name='teradata.a'>
        <connection class='teradata' dbname='ANALYTICS' server='td.example.com' />
      </named-connection>
    </named-connections>
    <relation name='ORDERS' table='[ANALYTICS].[ORDERS]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>SALES</remote-name><local-name>[SALES]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Azure Synapse Analytics (Tableau class 'azure_sql_dw') speaks the SQL Server TDS protocol, so
# it binds through Sql.Database exactly like sqlserver / azure_sqldb.
SYNAPSE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Syn' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='syn' name='azure_sql_dw.a'>
        <connection class='azure_sql_dw' dbname='WideWorld' server='syn.sql.azuresynapse.net' />
      </named-connection>
    </named-connections>
    <relation name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Databricks: host + SQL-warehouse HTTP path, Unity Catalog catalog in dbname, [schema].[table].
DATABRICKS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Dbx' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='dbx' name='databricks.a'>
        <connection class='databricks' dbname='main' server='adb-123.azuredatabricks.net'
                    http-path='/sql/1.0/warehouses/abc123' />
      </named-connection>
    </named-connections>
    <relation name='ORDERS' table='[sales].[orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>amount</remote-name><local-name>[amount]</local-name>
        <parent-name>[orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Microsoft Fabric Warehouse / Lakehouse SQL endpoint (Tableau class
# 'microsoft_fabric_sql_endpoint'): a SQL Server TDS endpoint -> Sql.Database, like sqlserver.
FABRIC_SQL = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Fab' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='fab' name='fabric.a'>
        <connection class='microsoft_fabric_sql_endpoint' dbname='SalesWH'
                    server='abc.datawarehouse.fabric.microsoft.com' />
      </named-connection>
    </named-connections>
    <relation name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Azure SQL Managed Instance: Tableau reaches a Managed Instance through the ordinary SQL Server
# connector, so it arrives as connection class 'sqlserver' (the MI host just carries the
# instance-specific endpoint, often with a port). It must still bind through Sql.Database.
MANAGED_INSTANCE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Mi' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='mi' name='sqlserver.a'>
        <connection class='sqlserver' dbname='Sales'
                    server='myinst.public.0a1b2c3d4e5f.database.windows.net,3342' />
      </named-connection>
    </named-connections>
    <relation name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Azure Synapse Analytics serverless SQL pool: the Synapse connector emits the SAME class
# ('azure_sql_dw') for the serverless (on-demand) endpoint as for the dedicated pool, so a
# serverless workspace endpoint must bind through Sql.Database identically.
SYNAPSE_SERVERLESS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='SynS' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='syns' name='azure_sql_dw.b'>
        <connection class='azure_sql_dw' dbname='Lake'
                    server='myws-ondemand.sql.azuresynapse.net' />
      </named-connection>
    </named-connections>
    <relation name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Faithful reproduction of a modern multi-sheet Excel ``.tds`` (the published Superstore
# sample): a <relation type='collection'> container wrapping the physical sheet tables, the
# SAME tables duplicated under the logical <properties> layer, and columns in <metadata-records>.
EXCEL_COLLECTION = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Sample - Superstore' version='18.1'>
  <connection class='excel-direct' filename='Sample - Superstore.xlsx'>
    <relation type='collection'>
      <relation connection='excel-direct.0' name='Orders' table='[Orders$]' type='table' />
      <relation connection='excel-direct.0' name='People' table='[People$]' type='table' />
      <relation connection='excel-direct.0' name='Returns' table='[Returns$]' type='table' />
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Row ID</remote-name><local-name>[Row ID]</local-name>
        <parent-name>[Orders$]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders$]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Person</remote-name><local-name>[Person]</local-name>
        <parent-name>[People$]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[People$]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Returned</remote-name><local-name>[Returned]</local-name>
        <parent-name>[Returns$]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
    <objects>
      <object caption='Orders'><properties>
        <relation connection='excel-direct.0' name='Orders' table='[Orders$]' type='table' />
      </properties></object>
      <object caption='People'><properties>
        <relation connection='excel-direct.0' name='People' table='[People$]' type='table' />
      </properties></object>
      <object caption='Returns'><properties>
        <relation connection='excel-direct.0' name='Returns' table='[Returns$]' type='table' />
      </properties></object>
    </objects>
  </_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
</datasource>"""


# Faithful modern Azure SQL (`azure_sqldb`) Superstore .tds: a federated named-connection of
# class 'azure_sqldb', three independent physical tables wrapped in a <relation type='collection'>
# and duplicated under the object-model layer, with typed columns in <metadata-records>. Mirrors
# the live validation datasource (Orders / People / Returns on Azure SQL) so the exact deploy-ready
# M is pinned offline. Server/credentials here are placeholders -- never real values.
AZURE_SQL_SUPERSTORE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore (Azure SQL)' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='azuresql' name='azure_sqldb.0a1b2c'>
        <connection authentication='sqlserver' class='azure_sqldb' dbname='Superstore'
                    server='example.database.windows.net' username='svc' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='azure_sqldb.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
      <relation connection='azure_sqldb.0a1b2c' name='People' table='[dbo].[People]' type='table' />
      <relation connection='azure_sqldb.0a1b2c' name='Returns' table='[dbo].[Returns]' type='table' />
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order ID</remote-name><local-name>[Order ID]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Person</remote-name><local-name>[Person]</local-name>
        <parent-name>[People]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[People]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Returned</remote-name><local-name>[Returned]</local-name>
        <parent-name>[Returns]</parent-name><local-type>boolean</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
    <objects>
      <object caption='Orders'><properties>
        <relation connection='azure_sqldb.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
      </properties></object>
      <object caption='People'><properties>
        <relation connection='azure_sqldb.0a1b2c' name='People' table='[dbo].[People]' type='table' />
      </properties></object>
      <object caption='Returns'><properties>
        <relation connection='azure_sqldb.0a1b2c' name='Returns' table='[dbo].[Returns]' type='table' />
      </properties></object>
    </objects>
  </_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
</datasource>"""


# -- type mapping --------------------------------------------------------------
@pytest.mark.parametrize("local,expected", [
    ("integer", "int64"), ("real", "double"), ("string", "string"),
    ("boolean", "boolean"), ("date", "dateTime"), ("datetime", "dateTime"),
    ("table", None), ("spatial", None), ("", None),
])
def test_tableau_type_mapping(local, expected):
    assert tableau_type_to_tmdl(local) == expected


# -- parsing -------------------------------------------------------------------
def test_parse_live_sqlserver():
    d = parse_tds(LIVE_SQLSERVER)
    assert d["connection_class"] == "sqlserver"
    assert d["server"] == "myserver.database.windows.net"
    assert d["database"] == "Superstore"
    assert d["is_extract"] is False
    assert d["named_connection_count"] == 1
    assert len(d["relations"]) == 1
    rel = d["relations"][0]
    assert rel["kind"] == "table"
    assert rel["schema"] == "dbo"
    assert rel["item"] == "Orders"
    assert {c["remote_name"] for c in rel["columns"]} == {"Order ID", "Sales", "Quantity"}
    assert {c["tmdl_type"] for c in rel["columns"]} == {"string", "double", "int64"}


def test_parse_never_carries_credentials():
    d = parse_tds(LIVE_SQLSERVER)
    blob = repr(d)
    assert "username" not in blob and "svc" not in blob


def test_parse_extract_flag_and_does_not_inflate_connection_count():
    d = parse_tds(EXTRACT_OVER_SQLSERVER)
    assert d["is_extract"] is True
    # the hyper connection inside <extract> must NOT be counted as a second named connection.
    assert d["named_connection_count"] == 1
    assert d["connection_class"] == "sqlserver"


def test_parse_custom_sql_relation():
    d = parse_tds(CUSTOM_SQL)
    rel = d["relations"][0]
    assert rel["kind"] == "custom_sql"
    assert "GROUP BY" in rel["sql"]
    assert len(rel["columns"]) == 2


def test_parse_join_tree_is_flagged_not_expanded():
    d = parse_tds(JOIN_TREE)
    # the two leaf tables must NOT leak out as independent relations.
    kinds = [r["kind"] for r in d["relations"]]
    assert kinds == ["join"]


def test_parse_excel_collection_yields_independent_deduped_tables():
    d = parse_tds(EXCEL_COLLECTION)
    assert d["connection_class"] == "excel-direct"
    assert d["is_extract"] is False
    # collection container is dropped; the 3 sheets become independent tables (no duplicates
    # from the <properties> object-model layer), and none are mis-flagged as a join/union.
    assert [r["kind"] for r in d["relations"]] == ["table", "table", "table"]
    names = {r["name"] for r in d["relations"]}
    assert names == {"Orders", "People", "Returns"}
    by_name = {r["name"]: r for r in d["relations"]}
    assert {c["remote_name"] for c in by_name["Orders"]["columns"]} == {"Row ID", "Sales"}
    assert {c["remote_name"] for c in by_name["People"]["columns"]} == {"Person", "Region"}
    assert d["unsupported_reasons"] == []


def test_excel_collection_selects_import_not_fallback():
    from storage_mode import select_storage_mode
    decision = select_storage_mode(parse_tds(EXCEL_COLLECTION))
    # a container of independent sheets is a clean multi-table Import, never a join fallback.
    assert decision["mode"] == "Import"
    assert decision["connector"] == "Excel.Workbook"
    assert decision["fallback"] is None


# -- M emission ----------------------------------------------------------------
def test_emit_connection_parameters():
    d = parse_tds(LIVE_SQLSERVER)
    params = emit_connection_parameters(d)
    assert 'expression Server = "myserver.database.windows.net"' in params
    assert 'expression Database = "Superstore"' in params
    assert "IsParameterQuery=true" in params


def test_emit_directquery_table_partition():
    d = parse_tds(LIVE_SQLSERVER)
    tmdl = emit_table_tmdl_m(d["relations"][0], d, "DirectQuery")
    assert "partition Orders = m" in tmdl
    assert "mode: directQuery" in tmdl
    assert 'Source = Sql.Database(#"Server", #"Database")' in tmdl
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in tmdl
    # columns are typed from Tableau metadata, not deferred to PBI inference.
    assert "dataType: int64" in tmdl     # Quantity
    assert "dataType: double" in tmdl    # Sales
    assert "sourceColumn: Sales" in tmdl


def test_emit_import_mode_keyword():
    d = parse_tds(LIVE_SQLSERVER)
    tmdl = emit_table_tmdl_m(d["relations"][0], d, "Import")
    assert "mode: import" in tmdl


def test_emit_custom_sql_uses_native_query_with_folding():
    d = parse_tds(CUSTOM_SQL)
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert "Value.NativeQuery(Source" in body
    assert "[EnableFolding=true]" in body
    # embedded double quotes in the SQL are escaped for the M string literal.
    assert '""Region""' in body


def test_emit_oracle_table_is_deploy_ready_server_only_m():
    # Oracle.Database is server-only (service/SID embedded in the server); flat schema/item
    # navigation with hierarchy off. No unused #"Database" parameter is carried.
    d = parse_tds(ORACLE)
    assert d["connection_class"] == "oracle"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Oracle.Database(#"Server", [HierarchicalNavigation=false])' in body
    assert 'Source{[Schema="SALES", Item="ORDERS"]}[Data]' in body
    assert "TODO" not in body
    assert '#"Database"' not in body            # Oracle's database is in the server string
    params = emit_connection_parameters(d)
    assert 'expression Server = "oradb.example.com:1521/ORCL"' in params
    assert "Database" not in params             # no unused database parameter


def test_emit_snowflake_table_is_deploy_ready_three_level_navigation():
    # Snowflake.Databases(server, warehouse) then database -> schema -> table, keyed by [Name, Kind].
    d = parse_tds(SNOWFLAKE)
    assert d["connection_class"] == "snowflake"
    assert d["warehouse"] == "COMPUTE_WH"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Snowflake.Databases(#"Server", #"Warehouse")' in body
    assert 'Source{[Name="ANALYTICS", Kind="Database"]}[Data]' in body
    assert 'Db{[Name="PUBLIC", Kind="Schema"]}[Data]' in body
    assert 'Schema{[Name="ORDERS", Kind="Table"]}[Data]' in body
    assert "TODO" not in body
    assert "Sql.Database" not in body
    # the warehouse is parameterized (declared from the .tds), not hardcoded into the call.
    params = emit_connection_parameters(d)
    assert 'expression Warehouse = "COMPUTE_WH"' in params
    assert 'expression Server = "acct.snowflakecomputing.com"' in params
    assert "Database" not in params             # Snowflake reaches the database by navigation


def test_emit_snowflake_scaffolds_when_database_missing():
    # Without a resolvable database the first navigation hop can't be built -> scaffold, not a guess.
    d = parse_tds(SNOWFLAKE)
    d["database"] = None
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert "TODO" in body
    assert "Snowflake.Databases" in body
    assert "[Name=" not in body


# Each fully-supported connector takes the verified `(server, database)` signature, so the
# two-argument call + Schema/Item navigation is emitted as deploy-ready M.
@pytest.mark.parametrize("cls,connector", [
    ("sqlserver", "Sql.Database"),
    ("azure_sqldb", "Sql.Database"),
    ("postgres", "PostgreSQL.Database"),
    ("mysql", "MySQL.Database"),
    ("redshift", "AmazonRedshift.Database"),
])
def test_emit_fully_supported_connector_dispatch(cls, connector):
    rel = {"kind": "table", "name": "Orders", "item": "Orders", "schema": "dbo", "columns": []}
    body = emit_m_partition_source(rel, {"connection_class": cls}, "DirectQuery")
    assert f'Source = {connector}(#"Server", #"Database")' in body
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in body


# Recognized connectors we deliberately do NOT auto-emit yet: the body must be a named scaffold
# that hints the intended connector, never a guessed call (BigQuery has no M function reference
# page, so its navigation selectors / project identifiers aren't verifiable offline).
@pytest.mark.parametrize("cls,connector", [
    ("bigquery", "GoogleBigQuery.Database"),
])
def test_emit_partial_connector_is_named_scaffold_not_guessed_m(cls, connector):
    rel = {"kind": "table", "name": "T", "item": "T", "schema": "s", "columns": []}
    body = emit_m_partition_source(rel, {"connection_class": cls}, "DirectQuery")
    assert "TODO" in body
    assert connector in body                     # names the intended connector as a hint
    assert '(#"Server", #"Database")' not in body  # but never a guessed 2-arg upstream call
    assert "Sql.Database" not in body


def test_emit_unsupported_class_falls_back_to_scaffold():
    # A connector class outside the verified set is emitted as a bare scaffold, never wrong M.
    rel = {"kind": "table", "name": "T", "item": "T", "schema": "s", "columns": []}
    body = emit_m_partition_source(rel, {"connection_class": "saphana"}, "Import")
    assert "TODO" in body
    assert "'saphana'" in body
    assert '(#"Server", #"Database")' not in body


def test_emit_teradata_parsed_is_deploy_ready_server_only():
    # Teradata.Database(server, [options]) is server-only (doc-verified signature) with
    # HierarchicalNavigation=false, so it reuses Oracle's verified flat [Schema, Item] path:
    # no #"Database" parameter, and the Teradata database is the navigation Schema.
    d = parse_tds(TERADATA)
    assert d["connection_class"] == "teradata"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Teradata.Database(#"Server", [HierarchicalNavigation=false])' in body
    assert 'Source{[Schema="ANALYTICS", Item="ORDERS"]}[Data]' in body
    assert "TODO" not in body
    assert "Sql.Database" not in body
    assert '(#"Server", #"Database")' not in body   # server-only: no Database argument
    params = emit_connection_parameters(d)
    assert 'expression Server = "td.example.com"' in params
    assert "Database" not in params                 # reached through the server string, like Oracle


def test_emit_fabric_sql_endpoint_is_deploy_ready_sql_database():
    # Microsoft Fabric Warehouse / Lakehouse SQL endpoint speaks the SQL Server TDS protocol ->
    # Sql.Database(server, database), identical to the sqlserver / azure_sqldb path.
    d = parse_tds(FABRIC_SQL)
    assert d["connection_class"] == "microsoft_fabric_sql_endpoint"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Sql.Database(#"Server", #"Database")' in body
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in body
    assert "TODO" not in body


def test_emit_synapse_is_deploy_ready_sql_database():
    # Azure Synapse Analytics speaks the SQL Server TDS protocol -> Sql.Database, byte-identical
    # to the sqlserver / azure_sqldb path.
    d = parse_tds(SYNAPSE)
    assert d["connection_class"] == "azure_sql_dw"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Sql.Database(#"Server", #"Database")' in body
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in body
    assert "TODO" not in body
    params = emit_connection_parameters(d)
    assert 'expression Server = "syn.sql.azuresynapse.net"' in params
    assert 'expression Database = "WideWorld"' in params


def test_emit_managed_instance_is_deploy_ready_sql_database():
    # Azure SQL Managed Instance reaches Tableau through the SQL Server connector (class
    # 'sqlserver'), so it must bind through Sql.Database like any other SQL Server endpoint -- the
    # instance-specific host (here carrying a port) round-trips verbatim into the #"Server" param.
    from storage_mode import select_storage_mode
    d = parse_tds(MANAGED_INSTANCE)
    assert d["connection_class"] == "sqlserver"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Sql.Database(#"Server", #"Database")' in body
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in body
    assert "TODO" not in body
    params = emit_connection_parameters(d)
    assert 'expression Server = "myinst.public.0a1b2c3d4e5f.database.windows.net,3342"' in params
    assert 'expression Database = "Sales"' in params
    decision = select_storage_mode(d)
    assert decision["mode"] == "DirectQuery"
    assert decision["connector"] == "Sql.Database"
    assert decision["fully_supported"] is True
    assert decision["recommended_mode"] == "DirectQuery"
    assert decision["fallback"] is None


def test_emit_synapse_serverless_is_deploy_ready_sql_database():
    # The Synapse connector emits the same class ('azure_sql_dw') for the serverless (on-demand)
    # endpoint as for a dedicated pool, so a serverless workspace endpoint binds through
    # Sql.Database identically -- no separate class, no scaffold.
    from storage_mode import select_storage_mode
    d = parse_tds(SYNAPSE_SERVERLESS)
    assert d["connection_class"] == "azure_sql_dw"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Sql.Database(#"Server", #"Database")' in body
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in body
    assert "TODO" not in body
    params = emit_connection_parameters(d)
    assert 'expression Server = "myws-ondemand.sql.azuresynapse.net"' in params
    assert 'expression Database = "Lake"' in params
    decision = select_storage_mode(d)
    assert decision["mode"] == "DirectQuery"
    assert decision["connector"] == "Sql.Database"
    assert decision["fully_supported"] is True
    assert decision["recommended_mode"] == "DirectQuery"
    assert decision["fallback"] is None


def test_emit_databricks_table_is_deploy_ready_catalogs_navigation():
    # Databricks.Catalogs(host, httpPath) then catalog -> schema -> table, keyed [Name, Kind]
    # (catalog level is Kind="Database"). Server + HttpPath are parameterized; no Database param.
    d = parse_tds(DATABRICKS)
    assert d["connection_class"] == "databricks"
    assert d["http_path"] == "/sql/1.0/warehouses/abc123"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Databricks.Catalogs(#"Server", #"HttpPath")' in body
    assert 'Source{[Name="main", Kind="Database"]}[Data]' in body
    assert 'Db{[Name="sales", Kind="Schema"]}[Data]' in body
    assert 'Schema{[Name="orders", Kind="Table"]}[Data]' in body
    assert "TODO" not in body
    assert "Sql.Database" not in body
    params = emit_connection_parameters(d)
    assert 'expression Server = "adb-123.azuredatabricks.net"' in params
    assert 'expression HttpPath = "/sql/1.0/warehouses/abc123"' in params
    assert "Database" not in params            # the catalog is reached by navigation


def test_emit_databricks_scaffolds_when_catalog_missing():
    # Without a resolvable catalog (the first navigation hop) we scaffold rather than guess.
    d = parse_tds(DATABRICKS)
    d["database"] = None
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert "TODO" in body
    assert "Databricks.Catalogs" in body
    assert "[Name=" not in body


def test_emit_databricks_custom_sql_is_scaffold():
    # Native SQL folding for Databricks isn't auto-emitted (only the (server, database) family is),
    # so a custom-SQL relation is a named scaffold, never a guessed Value.NativeQuery.
    rel = {"kind": "custom_sql", "name": "Q", "item": "Q", "sql": "SELECT 1", "columns": []}
    body = emit_m_partition_source(rel, {"connection_class": "databricks"}, "DirectQuery")
    assert "TODO" in body
    assert "Databricks.Catalogs" in body
    assert "Value.NativeQuery" not in body


# Analysis Services (SSAS / MSOLAP) is already a tabular/multidimensional model -- never a naive
# M partition. It is flagged for the separate model-migration path, not emitted as upstream M.
@pytest.mark.parametrize("cls", ["msolap", "sqlserver-analysis-services"])
def test_emit_analysis_services_is_flagged_scaffold_not_m(cls):
    rel = {"kind": "table", "name": "Sales", "item": "Sales", "schema": "", "columns": []}
    body = emit_m_partition_source(rel, {"connection_class": cls}, "DirectQuery")
    assert "TODO" in body
    assert "Analysis Services" in body
    assert "model" in body.lower()
    assert "Sql.Database" not in body
    assert '(#"Server", #"Database")' not in body


def test_emit_table_none_when_no_columns():
    rel = {"kind": "table", "name": "Empty", "item": "Empty", "columns": []}
    assert emit_table_tmdl_m(rel, {"connection_class": "sqlserver"}, "Import") is None


# -- field resolver ------------------------------------------------------------
def test_m_field_resolver_resolves_caption():
    d = parse_tds(LIVE_SQLSERVER)
    resolve = build_m_field_resolver(d)
    assert resolve("Sales") == ("Orders", "Sales", "double")
    assert resolve("Quantity") == ("Orders", "Quantity", "int64")
    assert resolve("Nonexistent") is None


def test_m_field_resolver_feeds_calc_to_dax():
    from calc_to_dax import translate_tableau_calc_to_dax
    d = parse_tds(LIVE_SQLSERVER)
    resolve = build_m_field_resolver(d)
    dax, reason, _ = translate_tableau_calc_to_dax("SUM([Sales])/SUM([Quantity])", resolve)
    assert dax == "DIVIDE(SUM('Orders'[Sales]), SUM('Orders'[Quantity]))"


# -- bind details --------------------------------------------------------------
def test_connection_details_for_bind():
    d = parse_tds(LIVE_SQLSERVER)
    details = connection_details_for_bind(d)
    assert details["bind_type"] == "SQL"
    assert details["server"] == "myserver.database.windows.net"
    assert details["database"] == "Superstore"
    assert details["path"] == "myserver.database.windows.net;Superstore"


def test_connection_details_bind_type_for_teradata():
    details = connection_details_for_bind(
        {"connection_class": "teradata", "server": "td.example.com", "database": "ANALYTICS"})
    assert details["bind_type"] == "Teradata"
    assert details["path"] == "td.example.com;ANALYTICS"


def test_connection_details_bind_type_for_synapse():
    details = connection_details_for_bind(
        {"connection_class": "azure_sql_dw", "server": "syn.sql.azuresynapse.net", "database": "Pool"})
    assert details["bind_type"] == "SQL"
    assert details["path"] == "syn.sql.azuresynapse.net;Pool"


def test_connection_details_bind_type_for_databricks():
    details = connection_details_for_bind(
        {"connection_class": "databricks", "server": "adb.example.azuredatabricks.net", "database": "main"})
    assert details["bind_type"] == "Databricks"
    assert details["path"] == "adb.example.azuredatabricks.net;main"


def test_connection_details_bind_type_for_fabric_sql_endpoint():
    details = connection_details_for_bind(
        {"connection_class": "microsoft_fabric_sql_endpoint",
         "server": "abc.datawarehouse.fabric.microsoft.com", "database": "SalesWH"})
    assert details["bind_type"] == "SQL"
    assert details["path"] == "abc.datawarehouse.fabric.microsoft.com;SalesWH"


# -- azure_sqldb first-class path (live-validation target, pinned offline) ------
def test_parse_azure_sql_superstore_first_class_path():
    d = parse_tds(AZURE_SQL_SUPERSTORE)
    assert d["connection_class"] == "azure_sqldb"
    assert d["database"] == "Superstore"
    assert d["is_extract"] is False
    assert d["named_connection_count"] == 1
    # collection container dropped + object-model duplicates deduped -> 3 independent tables.
    assert [r["kind"] for r in d["relations"]] == ["table", "table", "table"]
    assert {r["name"] for r in d["relations"]} == {"Orders", "People", "Returns"}
    assert d["unsupported_reasons"] == []
    # credentials are never carried into the descriptor.
    blob = repr(d)
    assert "username" not in blob and "svc" not in blob


def test_azure_sqldb_full_pipeline_emits_deploy_ready_sql_database_m():
    from storage_mode import select_storage_mode
    d = parse_tds(AZURE_SQL_SUPERSTORE)

    decision = select_storage_mode(d)
    assert decision["mode"] == "DirectQuery"
    assert decision["connector"] == "Sql.Database"     # azure_sqldb speaks the SQL Server protocol
    assert decision["fully_supported"] is True
    assert decision["recommended_mode"] == "DirectQuery"
    assert decision["fallback"] is None

    by_name = {r["name"]: r for r in d["relations"]}
    orders = emit_table_tmdl_m(by_name["Orders"], d, decision["mode"])
    assert "partition Orders = m" in orders
    assert "mode: directQuery" in orders
    assert 'Source = Sql.Database(#"Server", #"Database")' in orders
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in orders
    assert "dataType: double" in orders   # Sales typed from Tableau metadata, not PBI inference

    # every table is deploy-ready M (no scaffold), with its own schema/item navigation.
    for name in ("Orders", "People", "Returns"):
        tmdl = emit_table_tmdl_m(by_name[name], d, decision["mode"])
        assert f'Source{{[Schema="dbo", Item="{name}"]}}[Data]' in tmdl
        assert "TODO" not in tmdl

    params = emit_connection_parameters(d)
    assert 'expression Server = "example.database.windows.net"' in params
    assert 'expression Database = "Superstore"' in params

    bind = connection_details_for_bind(d)
    assert bind["bind_type"] == "SQL"
    assert bind["path"] == "example.database.windows.net;Superstore"
