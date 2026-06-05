"""Storage-mode policy tests (pure, descriptor-driven — no XML).

Locks the per-datasource decision tree: extract->Import, live relational->DirectQuery,
flat file->Import, and the fallback to land-to-Delta + DirectLake for shapes that can't be
rebuilt directly (join trees, multi-connection, unknown/partial connectors).
"""
from storage_mode import FALLBACK_LAND_TO_DELTA, select_storage_mode


def _desc(**kw):
    base = {
        "connection_class": "sqlserver",
        "server": "srv",
        "database": "db",
        "is_extract": False,
        "named_connection_count": 1,
        "relations": [{"kind": "table", "name": "Orders", "item": "Orders",
                       "columns": [{"model_name": "Sales", "tmdl_type": "double"}]}],
        "unsupported_reasons": [],
    }
    base.update(kw)
    return base


def test_live_sqlserver_is_directquery():
    d = select_storage_mode(_desc())
    assert d["mode"] == "DirectQuery"
    assert d["connector"] == "Sql.Database"
    assert d["fully_supported"] is True
    assert d["fallback"] is None
    # gateway + credentials always surfaced as manual steps.
    assert any("credentials" in f.lower() for f in d["manual_followups"])
    assert any("gateway" in f.lower() for f in d["manual_followups"])


def test_extract_is_import_with_live_alternative():
    d = select_storage_mode(_desc(is_extract=True))
    assert d["mode"] == "Import"
    assert d["direct_upstream_available"] is True   # sqlserver underneath -> live option exists
    assert "snapshot" in d["rationale"].lower()


def test_postgres_is_directquery_fully_supported():
    d = select_storage_mode(_desc(connection_class="postgres"))
    assert d["mode"] == "DirectQuery"
    assert d["connector"] == "PostgreSQL.Database"
    assert d["fully_supported"] is True


def test_snowflake_is_directquery_but_not_fully_supported():
    d = select_storage_mode(_desc(connection_class="snowflake"))
    assert d["mode"] == "DirectQuery"
    assert d["connector"] == "Snowflake.Databases"
    assert d["fully_supported"] is False
    assert any("snowflake.databases" in f.lower() for f in d["manual_followups"])


def test_flat_file_is_import_scaffold():
    d = select_storage_mode(_desc(connection_class="excel-direct", server=None, database=None))
    assert d["mode"] == "Import"
    assert d["connector"] == "Excel.Workbook"
    assert d["fully_supported"] is False   # needs a file path


def test_custom_sql_sets_native_query_flag():
    rel = {"kind": "custom_sql", "name": "Custom SQL Query", "sql": "SELECT 1",
           "columns": [{"model_name": "x", "tmdl_type": "int64"}]}
    d = select_storage_mode(_desc(relations=[rel]))
    assert d["uses_native_query"] is True
    assert any("native query" in f.lower() for f in d["manual_followups"])


def test_join_tree_falls_back():
    d = select_storage_mode(_desc(relations=[{"kind": "join", "name": "Orders+People"}]))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_LAND_TO_DELTA
    assert "join" in d["rationale"].lower()


def test_multiple_named_connections_fall_back():
    d = select_storage_mode(_desc(named_connection_count=2))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_LAND_TO_DELTA


def test_unknown_connector_falls_back():
    d = select_storage_mode(_desc(connection_class="teradata"))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_LAND_TO_DELTA


def test_no_columns_falls_back():
    d = select_storage_mode(_desc(relations=[{"kind": "table", "name": "Orders",
                                              "item": "Orders", "columns": []}]))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_LAND_TO_DELTA
    assert "column" in d["rationale"].lower()
