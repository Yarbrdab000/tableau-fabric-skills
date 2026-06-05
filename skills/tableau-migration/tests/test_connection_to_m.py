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
        <connection class='snowflake' dbname='ANALYTICS' server='acct.snowflakecomputing.com' />
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


def test_emit_snowflake_is_scaffold_not_wrong_m():
    d = parse_tds(SNOWFLAKE)
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert "TODO" in body
    assert "Sql.Database" not in body   # must not emit the wrong connector


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
