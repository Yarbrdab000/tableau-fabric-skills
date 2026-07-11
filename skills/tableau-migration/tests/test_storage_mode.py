"""Storage-mode policy tests (pure, descriptor-driven — no XML).

Locks the per-datasource decision tree: extract->Import, live relational->DirectQuery,
flat file->Import, and the honest needs-storage-decision routing for shapes that can't be
rebuilt directly (join trees, multi-connection, unknown/partial connectors). DirectLake
(land-to-Delta) is opt-in only and is never auto-stamped by the router.
"""
from storage_mode import (
    FALLBACK_ANALYSIS_SERVICES, FALLBACK_LAND_TO_DELTA, FALLBACK_NEEDS_DECISION,
    select_storage_mode)

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


def test_snowflake_is_directquery_fully_supported():
    # Snowflake.Databases(server, warehouse) + database/schema/table navigation is auto-emitted,
    # so Snowflake is a fully-supported DirectQuery rebuild (navigation doc-informed, and the
    # emitted M has been reconciled against a live Snowflake instance end-to-end).
    d = select_storage_mode(_desc(connection_class="snowflake"))
    assert d["mode"] == "DirectQuery"
    assert d["connector"] == "Snowflake.Databases"
    assert d["fully_supported"] is True
    assert d["fallback"] is None


def test_oracle_is_directquery_fully_supported():
    # Oracle.Database(server, [HierarchicalNavigation=false]) + flat schema/item navigation is
    # auto-emitted (server-only signature verified from the official M reference).
    d = select_storage_mode(_desc(connection_class="oracle"))
    assert d["mode"] == "DirectQuery"
    assert d["connector"] == "Oracle.Database"
    assert d["fully_supported"] is True
    assert d["fallback"] is None


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
    assert d["fallback"] == FALLBACK_NEEDS_DECISION
    assert "join" in d["rationale"].lower()


def test_multiple_named_connections_fall_back():
    d = select_storage_mode(_desc(named_connection_count=2))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_NEEDS_DECISION


def test_unknown_connector_falls_back():
    # SAP HANA is intentionally outside the verified v1 connector set -> needs a storage decision.
    d = select_storage_mode(_desc(connection_class="saphana"))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_NEEDS_DECISION


def test_no_columns_falls_back():
    d = select_storage_mode(_desc(relations=[{"kind": "table", "name": "Orders",
                                              "item": "Orders", "columns": []}]))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_NEEDS_DECISION
    assert "column" in d["rationale"].lower()


def test_needs_decision_label_is_distinct_from_land_to_delta_optin():
    # The de-default contract: an unresolved shape routes to the honest needs-storage-decision
    # label, which is a DIFFERENT value from the land-to-Delta + DirectLake opt-in. DirectLake is
    # never auto-stamped -- the opt-in label still exists (Wave-5 hook) but is never the auto route.
    assert FALLBACK_NEEDS_DECISION == "needs-storage-decision"
    assert FALLBACK_LAND_TO_DELTA == "land-to-delta-directlake"
    assert FALLBACK_NEEDS_DECISION != FALLBACK_LAND_TO_DELTA
    d = select_storage_mode(_desc(relations=[{"kind": "join", "name": "Orders+People"}]))
    assert d["fallback"] != FALLBACK_LAND_TO_DELTA


def test_needs_decision_defaults_to_import_and_points_at_optin():
    # A needs-decision shape must (a) default recommended_mode to a direct-to-source Import rebuild,
    # never an automatic DirectLake landing, and (b) surface a follow-up that names the land-to-Delta
    # + DirectLake path as an explicit OPT-IN the user chooses deliberately.
    d = select_storage_mode(_desc(connection_class="saphana"))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_NEEDS_DECISION
    assert d["recommended_mode"] == "Import"
    fu = " || ".join(d["manual_followups"]).lower()
    assert "opt-in" in fu or "opt in" in fu
    assert "directlake" in fu.replace(" ", "") or "land-to-delta" in fu
    # the rationale never claims DirectLake/land-to-Delta was auto-selected
    assert "auto-selected" not in d["rationale"].lower() or "never auto-selected" in d["rationale"].lower()


# -- extract-backed SaaS (unmapped connector) -> honest offline Import ---------
def test_extract_backed_saas_routes_to_import_over_extract():
    # A Salesforce (or any unmapped-connector) datasource shipped WITH an extract carries a full
    # .hyper snapshot of its data. Power BI has no live Salesforce DirectQuery rebuild we support,
    # but the snapshot IS the data -> land it as an offline Import (over the materialized extract),
    # NOT the honest-but-dead needs-storage-decision, and NEVER an automatic DirectLake landing.
    d = select_storage_mode(_desc(connection_class="salesforce", is_extract=True))
    assert d["mode"] == "Import"
    assert d["import_from_extract"] is True
    assert d["fallback"] is None
    assert d["recommended_mode"] == "Import"
    # no live upstream connector we can rebuild -> the Import over the snapshot is the only home.
    assert d["direct_upstream_available"] is False
    assert d["fully_supported"] is False
    assert "snapshot" in d["rationale"].lower() or "extract" in d["rationale"].lower()


def test_extract_backed_saas_not_stamped_directlake():
    # The de-default contract holds for the SaaS extract too: never land-to-Delta / DirectLake.
    d = select_storage_mode(_desc(connection_class="salesforce", is_extract=True))
    assert d["fallback"] != FALLBACK_LAND_TO_DELTA


def test_non_extract_unmapped_connector_still_needs_decision():
    # The new branch is gated on is_extract: a Salesforce datasource with NO extract (no bundled
    # snapshot) has no data to import offline -> it stays the honest needs-storage-decision.
    d = select_storage_mode(_desc(connection_class="salesforce", is_extract=False))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_NEEDS_DECISION
    assert not d.get("import_from_extract")


def test_mapped_extract_stays_live_connector_import_not_import_from_extract():
    # A mapped-live connector (sqlserver) shipped as an extract already builds an Import over its
    # live Sql.Database connector (branch 4) -- it must NOT be diverted to the extract-CSV path.
    d = select_storage_mode(_desc(connection_class="sqlserver", is_extract=True))
    assert d["mode"] == "Import"
    assert d["connector"] == "Sql.Database"
    assert d["direct_upstream_available"] is True
    assert not d.get("import_from_extract")


def test_flat_file_extract_stays_flatfile_not_import_from_extract():
    # A flat-file class (excel-direct) with an extract flag still routes through the flat-file
    # Import branch, not the extract-over-unmapped branch (which excludes flat-file classes).
    d = select_storage_mode(_desc(connection_class="excel-direct", is_extract=True))
    assert d["mode"] == "Import"
    assert not d.get("import_from_extract")


# -- expanded connector dispatch ----------------------------------------------
@pytest.mark.parametrize("cls,connector", [
    ("sqlserver", "Sql.Database"),
    ("azure_sqldb", "Sql.Database"),
    ("azure_sql_dw", "Sql.Database"),       # Azure Synapse Analytics (TDS protocol)
    ("microsoft_fabric_sql_endpoint", "Sql.Database"),  # Fabric Warehouse / Lakehouse SQL endpoint
    ("postgres", "PostgreSQL.Database"),
    ("mysql", "MySQL.Database"),
    ("redshift", "AmazonRedshift.Database"),
    ("oracle", "Oracle.Database"),
    ("snowflake", "Snowflake.Databases"),
    ("databricks", "Databricks.Catalogs"),
])
def test_fully_supported_family_is_directquery(cls, connector):
    d = select_storage_mode(_desc(connection_class=cls))
    assert d["mode"] == "DirectQuery"
    assert d["connector"] == connector
    assert d["fully_supported"] is True
    assert d["fallback"] is None


def test_databricks_directquery_flags_httppath_followup():
    # Databricks emits a doc-verified shape, but the SQL-warehouse HTTP path + catalog can't be
    # sourced portably from the .tds, so the decision surfaces a loud manual follow-up.
    d = select_storage_mode(_desc(connection_class="databricks"))
    assert d["mode"] == "DirectQuery"
    assert d["fully_supported"] is True
    assert any("httppath" in f.lower() for f in d["manual_followups"])


@pytest.mark.parametrize("cls", ["msolap", "sqlserver-analysis-services"])
def test_analysis_services_is_model_migration_not_relational_fallback(cls):
    # SSAS / MSOLAP is already a semantic model: no Import/DirectQuery rebuild, and NOT the
    # relational needs-decision path -- it gets its own model-migration routing + rationale.
    d = select_storage_mode(_desc(connection_class=cls))
    assert d["mode"] is None
    assert d["connector"] is None
    assert d["fallback"] == FALLBACK_ANALYSIS_SERVICES
    assert d["fallback"] != FALLBACK_NEEDS_DECISION
    assert d["fallback"] != FALLBACK_LAND_TO_DELTA
    assert "model" in d["rationale"].lower()
    assert any("xmla" in f.lower() or "semantic-model" in f.lower() for f in d["manual_followups"])


@pytest.mark.parametrize("cls,connector", [
    ("bigquery", "GoogleBigQuery.Database"),
    ("teradata", "Teradata.Database"),   # documented signature, but no live navigator -> scaffold
])
def test_partial_live_connector_is_directquery_scaffold(cls, connector):
    # Recognized connector, DirectQuery chosen, but M is a flagged scaffold (its navigation or
    # required identifiers aren't verified offline), so it is not fully supported.
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
        _desc(connection_class="bigquery"),                                  # live, partial scaffold
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
    partial = select_storage_mode(_desc(connection_class="bigquery"))
    fallback = select_storage_mode(_desc(connection_class="saphana"))
    assert full["score"] > partial["score"] > fallback["score"]


def test_recommended_mode_directquery_for_live_supported():
    assert select_storage_mode(_desc())["recommended_mode"] == "DirectQuery"


def test_recommended_mode_import_for_extract_and_flat_file():
    assert select_storage_mode(_desc(is_extract=True))["recommended_mode"] == "Import"
    flat = select_storage_mode(_desc(connection_class="excel-direct", server=None, database=None))
    assert flat["recommended_mode"] == "Import"


def test_recommended_mode_import_default_for_unknown_fallback():
    # mode is None (needs-storage-decision), but the scored recommendation defaults to Import.
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


# -- generic ODBC routing (engine-agnostic lift-and-shift) ---------------------
def _odbc_desc(**kw):
    base = {
        "connection_class": "genericodbc",
        "server": "trino.minio.local",
        "database": "lake",
        "port": "8080",
        "odbc_driver": "Simba Trino ODBC Driver",
        "odbc_dsn": "",
        "is_extract": False,
        "named_connection_count": 1,
        "relations": [{"kind": "custom_sql", "name": "Q", "sql": "SELECT 1",
                       "columns": [{"model_name": "x", "tmdl_type": "int64"}]}],
        "unsupported_reasons": [],
    }
    base.update(kw)
    return base


def test_generic_odbc_custom_sql_is_import_via_odbc_query():
    d = select_storage_mode(_odbc_desc())
    assert d["mode"] == "Import"
    assert d["connector"] == "Odbc.Query"
    assert d["uses_native_query"] is True
    assert d["fallback"] is None
    assert d["score"] == 60
    # the SAME driver Tableau uses must be present wherever the model runs (Desktop + gateway),
    # and the preserved custom SQL is surfaced for native-query review.
    assert any("odbc driver" in f.lower() for f in d["manual_followups"])
    assert any("gateway" in f.lower() for f in d["manual_followups"])
    assert any("native query" in f.lower() for f in d["manual_followups"])


def test_generic_odbc_table_relation_is_import_via_odbc_datasource():
    table_rel = {"kind": "table", "name": "orders", "item": "orders",
                 "columns": [{"model_name": "region", "tmdl_type": "string"}]}
    d = select_storage_mode(_odbc_desc(relations=[table_rel]))
    assert d["mode"] == "Import"
    assert d["connector"] == "Odbc.DataSource"
    assert d["uses_native_query"] is False
    # a plain table has no preserved native query, so no native-query review step.
    assert not any("native query" in f.lower() for f in d["manual_followups"])


def test_generic_odbc_dsn_only_is_reconstructable():
    # DSN form (no driver name) is enough to bind -> still Import.
    d = select_storage_mode(_odbc_desc(odbc_driver="", odbc_dsn="Bamboo DSN"))
    assert d["mode"] == "Import"
    assert d["connector"] == "Odbc.Query"
    assert d["score"] == 60


def test_generic_odbc_without_driver_or_dsn_falls_back():
    # neither a DSN nor a driver name -> nothing to bind -> fail closed to needs-storage-decision.
    d = select_storage_mode(_odbc_desc(odbc_driver="", odbc_dsn=""))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_NEEDS_DECISION
    assert d["score"] == 30
    assert d["recommended_mode"] == "Import"


def test_generic_jdbc_is_not_routed_as_odbc():
    # genericjdbc is deliberately EXCLUDED from the ODBC tier (Power BI has no JDBC connector and
    # cannot load Java drivers); it must route to needs-storage-decision, never Odbc.Query.
    table_rel = {"kind": "table", "name": "orders", "item": "orders",
                 "columns": [{"model_name": "region", "tmdl_type": "string"}]}
    d = select_storage_mode(_odbc_desc(connection_class="genericjdbc", relations=[table_rel]))
    assert d["mode"] is None
    assert d["connector"] is None
    assert d["fallback"] == FALLBACK_NEEDS_DECISION


# -- native query-engine routing over ODBC (Spark / Presto / Trino / Starburst) ------------------
# These classes have no clean first-party Power BI connector but ship an ODBC driver, so we promote
# them to first-class by binding over the engine-agnostic ODBC emitter -- Import, never Delta.
def _engine_desc(cls, **kw):
    # A native engine .tds records server/port/catalog but NO odbc-driver/odbc-dsn (Tableau used its
    # bundled native driver) -- the descriptor reflects that absence.
    base = {
        "connection_class": cls,
        "server": "engine.example.com",
        "database": "hive",
        "port": "8080",
        "odbc_driver": None,
        "odbc_dsn": None,
        "is_extract": False,
        "named_connection_count": 1,
        "relations": [{"kind": "custom_sql", "name": "Q", "sql": "SELECT 1",
                       "columns": [{"model_name": "x", "tmdl_type": "int64"}]}],
        "unsupported_reasons": [],
    }
    base.update(kw)
    return base


_EXPECTED_DRIVER = {
    "spark": "Simba Spark ODBC Driver",
    "presto": "Simba Presto ODBC Driver",
    "trino": "Starburst ODBC Driver for Trino",
    "starburst": "Starburst ODBC Driver for Trino",
}


@pytest.mark.parametrize("cls", ["spark", "presto", "trino", "starburst"])
def test_native_engine_custom_sql_is_import_via_odbc_query(cls):
    d = select_storage_mode(_engine_desc(cls))
    assert d["mode"] == "Import"
    assert d["connector"] == "Odbc.Query"
    assert d["uses_native_query"] is True
    assert d["fully_supported"] is False
    assert d["fallback"] is None          # never lands in Delta
    assert d["score"] == 60
    # the assumed per-engine ODBC driver is named and flagged confirm-required.
    fu = " || ".join(d["manual_followups"])
    assert _EXPECTED_DRIVER[cls] in fu
    assert "confirm" in fu.lower()
    assert any("native query" in f.lower() for f in d["manual_followups"])


@pytest.mark.parametrize("cls", ["spark", "presto", "trino", "starburst"])
def test_native_engine_table_relation_is_import_via_odbc_datasource(cls):
    table_rel = {"kind": "table", "name": "orders", "item": "orders",
                 "columns": [{"model_name": "region", "tmdl_type": "string"}]}
    d = select_storage_mode(_engine_desc(cls, relations=[table_rel]))
    assert d["mode"] == "Import"
    assert d["connector"] == "Odbc.DataSource"
    assert d["uses_native_query"] is False
    assert d["fallback"] is None          # a plain table still binds over ODBC, never Delta


def test_native_engine_without_server_needs_decision():
    # no server/host -> no ODBC connection string can be reconstructed -> fail closed.
    d = select_storage_mode(_engine_desc("presto", server=""))
    assert d["mode"] is None
    assert d["fallback"] == FALLBACK_NEEDS_DECISION
    assert d["score"] == 30
    assert d["recommended_mode"] == "Import"


def test_native_engine_extract_still_routes_over_odbc_not_plain_scaffold():
    # an extract-enabled native-engine source must take the ODBC path (branch 1.6 precedes the
    # extract branch), not the unknown-connector / plain-extract scaffold.
    d = select_storage_mode(_engine_desc("starburst", is_extract=True))
    assert d["mode"] == "Import"
    assert d["connector"] == "Odbc.Query"
    assert d["fallback"] is None


def test_native_engine_trino_and_starburst_share_driver_presto_differs():
    # Trino and its enterprise distribution Starburst share the Starburst Trino driver; Presto and
    # Spark use the Simba drivers -- lock the per-engine mapping so a rename is caught.
    def driver_in(cls):
        return " || ".join(select_storage_mode(_engine_desc(cls))["manual_followups"])
    assert "Starburst ODBC Driver for Trino" in driver_in("trino")
    assert "Starburst ODBC Driver for Trino" in driver_in("starburst")
    assert "Simba Presto ODBC Driver" in driver_in("presto")
    assert "Simba Spark ODBC Driver" in driver_in("spark")
