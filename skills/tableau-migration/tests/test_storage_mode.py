"""Storage-mode policy tests (pure, descriptor-driven — no XML).

Locks the per-datasource decision tree: extract->Import, live relational->DirectQuery,
flat file->Import, and the fallback to land-to-Delta + DirectLake for shapes that can't be
rebuilt directly (join trees, multi-connection, unknown/partial connectors).
"""
from storage_mode import FALLBACK_LAND_TO_DELTA, select_storage_mode

import pytest


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


def test_azure_sqldb_is_directquery_fully_supported():
    # Azure SQL Database (Tableau class 'azure_sqldb') speaks the SQL Server protocol, so it
    # rebuilds as a fully-supported Sql.Database DirectQuery model (verified on a live datasource).
    d = select_storage_mode(_desc(connection_class="azure_sqldb"))
    assert d["mode"] == "DirectQuery"
    assert d["connector"] == "Sql.Database"
    assert d["fully_supported"] is True
    assert d["fallback"] is None


def test_azure_sqldb_extract_is_import_with_live_directquery_alternative():
    # If the Azure SQL Superstore datasource ships as a .hyper extract, Import preserves the
    # snapshot while still advertising the live Sql.Database DirectQuery rebuild as an option.
    d = select_storage_mode(_desc(connection_class="azure_sqldb", is_extract=True))
    assert d["mode"] == "Import"
    assert d["connector"] == "Sql.Database"
    assert d["fully_supported"] is True
    assert d["direct_upstream_available"] is True
    assert d["recommended_mode"] == "Import"


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
    # SAP HANA is intentionally outside the verified v1 connector set -> fall back.
    d = select_storage_mode(_desc(connection_class="saphana"))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_LAND_TO_DELTA


def test_no_columns_falls_back():
    d = select_storage_mode(_desc(relations=[{"kind": "table", "name": "Orders",
                                              "item": "Orders", "columns": []}]))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_LAND_TO_DELTA
    assert "column" in d["rationale"].lower()


# -- expanded connector dispatch ----------------------------------------------
@pytest.mark.parametrize("cls,connector", [
    ("sqlserver", "Sql.Database"),
    ("azure_sqldb", "Sql.Database"),
    ("postgres", "PostgreSQL.Database"),
    ("mysql", "MySQL.Database"),
    ("redshift", "AmazonRedshift.Database"),
])
def test_fully_supported_family_is_directquery(cls, connector):
    d = select_storage_mode(_desc(connection_class=cls))
    assert d["mode"] == "DirectQuery"
    assert d["connector"] == connector
    assert d["fully_supported"] is True
    assert d["fallback"] is None


@pytest.mark.parametrize("cls,connector", [
    ("oracle", "Oracle.Database"),
    ("teradata", "Teradata.Database"),
    ("snowflake", "Snowflake.Databases"),
    ("bigquery", "GoogleBigQuery.Database"),
])
def test_partial_live_connector_is_directquery_scaffold(cls, connector):
    # Recognized connector, DirectQuery chosen, but M is a flagged scaffold (signature/navigation
    # differs from the (server, database) family), so it is not fully supported.
    d = select_storage_mode(_desc(connection_class=cls))
    assert d["mode"] == "DirectQuery"
    assert d["connector"] == connector
    assert d["fully_supported"] is False
    assert d["fallback"] is None
    assert any(connector.lower() in f.lower() for f in d["manual_followups"])


# -- scored recommendation ----------------------------------------------------
def test_decision_always_carries_score_and_recommended_mode():
    paths = [
        _desc(),                                                              # live, fully supported
        _desc(connection_class="snowflake"),                                 # live, partial scaffold
        _desc(is_extract=True),                                              # extract
        _desc(connection_class="excel-direct", server=None, database=None),  # flat file
        _desc(connection_class="saphana"),                                   # unknown -> fallback
        _desc(relations=[{"kind": "join", "name": "J"}]),                    # structural -> fallback
    ]
    for desc in paths:
        d = select_storage_mode(desc)
        assert isinstance(d["score"], int) and 0 <= d["score"] <= 100
        assert d["recommended_mode"] in ("Import", "DirectQuery")


def test_score_ranks_full_above_partial_above_fallback():
    full = select_storage_mode(_desc())
    partial = select_storage_mode(_desc(connection_class="snowflake"))
    fallback = select_storage_mode(_desc(connection_class="saphana"))
    assert full["score"] > partial["score"] > fallback["score"]


def test_recommended_mode_directquery_for_live_supported():
    assert select_storage_mode(_desc())["recommended_mode"] == "DirectQuery"


def test_recommended_mode_import_for_extract_and_flat_file():
    assert select_storage_mode(_desc(is_extract=True))["recommended_mode"] == "Import"
    flat = select_storage_mode(_desc(connection_class="excel-direct", server=None, database=None))
    assert flat["recommended_mode"] == "Import"


def test_recommended_mode_import_default_for_unknown_fallback():
    # mode is None (route to land-to-Delta), but the scored recommendation defaults to Import.
    d = select_storage_mode(_desc(connection_class="saphana"))
    assert d["mode"] is None
    assert d["recommended_mode"] == "Import"


def test_native_query_lowers_score():
    plain = select_storage_mode(_desc())
    native_rel = {"kind": "custom_sql", "name": "Q", "sql": "SELECT 1",
                  "columns": [{"model_name": "x", "tmdl_type": "int64"}]}
    native = select_storage_mode(_desc(relations=[native_rel]))
    assert native["uses_native_query"] is True
    assert native["score"] < plain["score"]
