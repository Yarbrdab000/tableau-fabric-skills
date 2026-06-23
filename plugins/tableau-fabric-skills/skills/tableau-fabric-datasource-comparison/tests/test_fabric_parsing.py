"""Unit tests for the offline TMDL / M parsing in ``fabric_inventory.py``."""
import base64
import json

import fabric_inventory as fab


# --------------------------------------------------------------------------------------
# TMDL table/column parsing
# --------------------------------------------------------------------------------------
SQLSERVER_TMDL = """\
table Orders
\tcolumn Sales
\t\tdataType: double
\t\tsummarizeBy: sum

\tcolumn 'Region Name'
\t\tdataType: string

\tpartition Orders = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t\tSource = Sql.Database("myserver.database.windows.net", "SalesDB"),
\t\t\t\t\tdbo_Orders = Source{[Schema="dbo",Item="Orders"]}[Data]
\t\t\t\tin
\t\t\t\t\tdbo_Orders
"""


def test_parse_tmdl_tables_columns_and_types():
    tables = fab.parse_tmdl_tables(SQLSERVER_TMDL)
    assert len(tables) == 1
    t = tables[0]
    assert t["name"] == "Orders"
    cols = {c["name"]: c["dataType"] for c in t["columns"]}
    assert cols == {"Sales": "double", "Region Name": "string"}


def test_parse_tmdl_extracts_sqlserver_source():
    tables = fab.parse_tmdl_tables(SQLSERVER_TMDL)
    srcs = tables[0]["sources"]
    assert len(srcs) == 1
    s = srcs[0]
    assert s["connectionType"] == "sqlserver"
    assert s["server"] == "myserver.database.windows.net"
    assert s["database"] == "SalesDB"
    assert s["schema"] == "dbo"
    assert s["table"] == "Orders"


def test_unquote_tmdl_name_variants():
    assert fab._unquote_tmdl_name("Orders") == "Orders"
    assert fab._unquote_tmdl_name("'Region Name'") == "Region Name"
    assert fab._unquote_tmdl_name("Orders = m") == "Orders"
    assert fab._unquote_tmdl_name("'Sales Orders' = m") == "Sales Orders"


def test_measures_do_not_capture_datatype_as_column():
    tmdl = """\
table Metrics
\tcolumn Amount
\t\tdataType: double

\tmeasure 'Total Sales' = SUM(Metrics[Amount])
\t\tformatString: 0.00
"""
    tables = fab.parse_tmdl_tables(tmdl)
    cols = [c["name"] for c in tables[0]["columns"]]
    assert cols == ["Amount"]  # the measure is not a physical column


def test_parse_tmdl_captures_measure_names():
    tmdl = """\
table Metrics
\tcolumn Amount
\t\tdataType: double

\tmeasure 'Total Sales' = SUM(Metrics[Amount])
\t\tformatString: 0.00
\tmeasure Margin = DIVIDE([Profit], [Sales])
"""
    tables = fab.parse_tmdl_tables(tmdl)
    assert tables[0]["measures"] == ["Total Sales", "Margin"]


def test_model_inventory_rolls_up_measures_across_tables():
    a = "table Orders\n\tcolumn Sales\n\t\tdataType: double\n\tmeasure 'Profit Ratio' = DIVIDE([P],[S])\n"
    b = "table Returns\n\tcolumn Qty\n\t\tdataType: int64\n\tmeasure 'Return Rate' = [R]/[O]\n"
    inv = fab.model_inventory_from_parts({"a.tmdl": a, "b.tmdl": b})
    assert inv["measures"] == ["Profit Ratio", "Return Rate"]


# --------------------------------------------------------------------------------------
# M source mining across connectors
# --------------------------------------------------------------------------------------
def test_parse_m_sources_postgres_schema_item():
    m = 'let Source = PostgreSQL.Database("pg.host", "shop"), ' \
        'public_orders = Source{[Schema="public",Item="orders"]}[Data] in public_orders'
    srcs = fab.parse_m_sources(m)
    assert srcs == [{
        "connectionType": "postgres", "server": "pg.host",
        "database": "shop", "schema": "public", "table": "orders",
    }]


def test_parse_m_sources_snowflake_name_nav():
    m = 'let Source = Snowflake.Databases("acct.snowflakecomputing.com","WH"), ' \
        'db = Source{[Name="ANALYTICS"]}[Data], ' \
        'sch = db{[Name="PUBLIC"]}[Data], ' \
        'tbl = sch{[Name="ORDERS"]}[Data] in tbl'
    srcs = fab.parse_m_sources(m)
    assert len(srcs) == 1
    s = srcs[0]
    assert s["connectionType"] == "snowflake"
    assert s["database"] == "ANALYTICS"
    assert s["schema"] == "PUBLIC"
    assert s["table"] == "ORDERS"


def test_parse_m_sources_unknown_shape_is_graceful():
    assert fab.parse_m_sources("") == []
    # a connector with no resolvable table still reports connector/server/db
    m = 'let Source = Sql.Database("srv","db") in Source'
    srcs = fab.parse_m_sources(m)
    assert srcs == [{
        "connectionType": "sqlserver", "server": "srv",
        "database": "db", "schema": "", "table": "",
    }]


# --------------------------------------------------------------------------------------
# M source mining: Fabric-native + file connectors (Lakehouse / Warehouse / Dataflow / Excel / native)
# --------------------------------------------------------------------------------------
def test_parse_m_sources_lakehouse_id_nav():
    m = ('let Source = Lakehouse.Contents(null), '
         'ws = Source{[workspaceId="w1"]}[Data], '
         'lh = ws{[lakehouseId="l1"]}[Data], '
         't = lh{[Id="Orders", ItemKind="Table"]}[Data] in t')
    srcs = fab.parse_m_sources(m)
    assert srcs == [{
        "connectionType": "lakehouse", "server": "",
        "database": "", "schema": "", "table": "Orders",
    }]


def test_parse_m_sources_warehouse_schema_item():
    m = 'let S = Fabric.Warehouse(null){[Id="dw1"]}[Data]{[Schema="dbo",Item="Customers"]}[Data] in S'
    srcs = fab.parse_m_sources(m)
    assert srcs == [{
        "connectionType": "warehouse", "server": "",
        "database": "", "schema": "dbo", "table": "Customers",
    }]


def test_parse_m_sources_dataflow_entity_nav():
    m = ('let S = PowerPlatform.Dataflows(null){[workspaceId="w"]}[Data]'
         '{[dataflowId="d"]}[Data]{[entity="SalesFact"]}[Data] in S')
    srcs = fab.parse_m_sources(m)
    assert srcs == [{
        "connectionType": "dataflow", "server": "",
        "database": "", "schema": "", "table": "SalesFact",
    }]


def test_parse_m_sources_excel_item_nav():
    m = 'let S = Excel.Workbook(File.Contents("C:\\book.xlsx"), true){[Item="Sheet1",Kind="Sheet"]}[Data] in S'
    srcs = fab.parse_m_sources(m)
    assert srcs[0]["connectionType"] == "excel"
    assert srcs[0]["table"] == "Sheet1"


def test_parse_m_sources_native_query_from_join_keeps_schema():
    m = ('let S = Value.NativeQuery(Sql.Database("srv","db"), '
         '"select * from dbo.FactSales f join dim.Customer c on c.id=f.cid") in S')
    srcs = fab.parse_m_sources(m)
    pairs = {(s["schema"], s["table"]) for s in srcs}
    assert ("dbo", "FactSales") in pairs
    assert ("dim", "Customer") in pairs
    assert all(s["connectionType"] == "sqlserver" for s in srcs)


def test_parse_m_sources_csv_is_graceful():
    m = 'let S = Csv.Document(File.Contents("C:\\data.csv"),[Delimiter=","]) in S'
    srcs = fab.parse_m_sources(m)
    assert srcs and srcs[0]["connectionType"] == "csv"


# --------------------------------------------------------------------------------------
# Aggregation + payload decode
# --------------------------------------------------------------------------------------
def test_model_inventory_from_parts_aggregates_tables():
    parts = {
        "definition/tables/Orders.tmdl": SQLSERVER_TMDL,
        "definition/tables/Customers.tmdl": "table Customers\n\tcolumn Id\n\t\tdataType: int64\n",
    }
    inv = fab.model_inventory_from_parts(parts)
    assert set(inv["tables"]) == {"Orders", "Customers"}
    names = {c["name"] for c in inv["columns"]}
    assert {"Sales", "Region Name", "Id"} <= names
    assert any(s["table"] == "Orders" for s in inv["sources"])


def test_decode_definition_parts_base64():
    body = {"definition": {"parts": [
        {"path": "definition/tables/Orders.tmdl",
         "payload": base64.b64encode(SQLSERVER_TMDL.encode("utf-8")).decode("ascii"),
         "payloadType": "InlineBase64"},
        {"path": "definition/model.bim", "payload": "ignored", "payloadType": "InlineBase64"},
    ]}}
    parts = fab.decode_definition_parts(body)
    assert list(parts) == ["definition/tables/Orders.tmdl"]  # non-TMDL part dropped
    assert "table Orders" in parts["definition/tables/Orders.tmdl"]


# --------------------------------------------------------------------------------------
# Durability: malformed / hostile TMDL, M, and payloads degrade gracefully
# --------------------------------------------------------------------------------------
def test_parse_tmdl_tolerates_crlf_tabs_and_blank_lines():
    crlf = (
        "table Orders\r\n"
        "\r\n"
        "\tcolumn Sales\r\n"
        "\t\tdataType: double\r\n"
        "\r\n"
        "\tcolumn 'Region Name'\r\n"
        "\t\tdataType: string\r\n"
    )
    tables = fab.parse_tmdl_tables(crlf)
    assert [t["name"] for t in tables] == ["Orders"]
    cols = {c["name"]: c["dataType"] for c in tables[0]["columns"]}
    assert cols == {"Sales": "double", "Region Name": "string"}


def test_parse_tmdl_empty_and_whitespace_only_is_graceful():
    assert fab.parse_tmdl_tables("") == []
    assert fab.parse_tmdl_tables("   \n\t\n   \r\n") == []


def test_parse_tmdl_table_without_columns_or_source():
    tables = fab.parse_tmdl_tables("table LonelyTable\n")
    assert tables == [{"name": "LonelyTable", "columns": [], "measures": [], "sources": []}]


def test_parse_m_sources_truncated_expression_is_graceful():
    # An M expression cut off mid-call must not raise; we still recover the connector.
    srcs = fab.parse_m_sources('let S = Sql.Database("srv",')
    assert isinstance(srcs, list) and srcs
    assert srcs[0]["connectionType"] == "sqlserver"
    assert srcs[0]["table"] == ""  # no table resolvable from the truncated text


def test_parse_m_sources_empty_and_none_are_graceful():
    assert fab.parse_m_sources("") == []
    assert fab.parse_m_sources(None) == []


def test_parse_m_sources_long_noisy_input_still_finds_the_source():
    noise = "// " + ("x" * 50000) + "\n"
    m = noise + 'let S = Sql.Database("srv","db"){[Schema="dbo",Item="Orders"]}[Data] in S'
    srcs = fab.parse_m_sources(m)
    assert {(s["schema"], s["table"]) for s in srcs} == {("dbo", "Orders")}


def test_decode_definition_parts_skips_bad_payload_and_non_tmdl():
    good = base64.b64encode(b"table T\n\tcolumn C\n\t\tdataType: int64\n").decode("ascii")
    body = {"definition": {"parts": [
        {"path": "definition/tables/Good.tmdl", "payload": good, "payloadType": "InlineBase64"},
        # valid base64 but not valid UTF-8 -> decode raises -> part is skipped, not fatal.
        {"path": "definition/tables/Bad.tmdl", "payload": "//4=", "payloadType": "InlineBase64"},
        {"path": "definition/model.bim", "payload": "whatever", "payloadType": "InlineBase64"},
    ]}}
    parts = fab.decode_definition_parts(body)
    assert list(parts) == ["definition/tables/Good.tmdl"]


def test_decode_definition_parts_handles_missing_definition_key():
    assert fab.decode_definition_parts({}) == {}
    assert fab.decode_definition_parts({"definition": None}) == {}
    assert fab.decode_definition_parts(None) == {}
