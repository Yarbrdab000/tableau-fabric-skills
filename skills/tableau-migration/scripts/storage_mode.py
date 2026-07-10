"""Per-datasource storage-mode auto-selection (pure, XML-free).

Given a normalized Tableau connection *descriptor* (produced by ``connection_to_m.parse_tds``),
decide which Power BI storage mode rebuilds the datasource with the least manual remapping,
or route to an honest **needs-decision** state when a direct-to-upstream rebuild is unsafe.

This module is deliberately pure: it knows nothing about XML or TMDL syntax, only about the
descriptor shape, so the policy is trivially unit-testable. ``connection_to_m`` does the
parsing and M emission; it may *call* this to decide a mode, but never the reverse.

Decision policy (first match wins):

1. Structurally unsafe shape -> no direct mode; route to ``needs-storage-decision``. This is
   NOT triggered by multiple connections per se: a federated source whose tables each resolve to
   their own connection is rebuilt directly (multi-source model + model relationships). It is
   only a ``join``/``union`` relation tree (one logical table spans relations), a multi-connection
   table that can't be routed to a specific upstream, or no resolvable columns.
2. Unknown / unmapped connector class -> needs-storage-decision.
3. Flat file (Excel/CSV) -> Import.
4. Extract enabled -> Import (preserve Tableau snapshot semantics); if the underlying live
   connector is supported, also report ``direct_upstream_available`` so the caller can offer
   live DirectQuery as an explicit alternative.
5. Live relational -> DirectQuery (live-to-live), including a multi-connection federation, where
   each table binds to its OWN upstream and the joins become model relationships.

**DirectLake is OPT-IN ONLY and is never auto-selected or defaulted here.** An unsafe shape does
NOT silently route to land-to-Delta + DirectLake; it routes to ``needs-storage-decision`` -- an
honest flag whose ``recommended_mode`` defaults to Import (rebuild direct-to-source where a
connection can be supplied) and whose follow-ups point at the explicit land-to-Delta + DirectLake
OPT-IN. The land-to-Delta + DirectLake capability stays available, but only when a caller opts in
to it deliberately, never as the automatic fallback for an unresolved or multi-source datasource.

Credentials and on-prem gateway setup are ALWAYS left to the user (security boundary) and
surfaced as ``manual_followups``.
"""
from __future__ import annotations

# Connectors whose M we emit as deploy-ready, doc-verified partitions (never a guessed
# scaffold). Each entry is `(function, connect_style, nav_style)` -- the two style facts are
# what make the emission correct rather than guessed:
#
#   connect_style:
#     "server_database"  -> Fn(#"Server", #"Database")                  (SQL Server protocol family)
#     "server_only"      -> Fn(#"Server", [HierarchicalNavigation=false])  (Oracle: service/SID is
#                            in the server string; flat schema navigation, hierarchy off)
#     "server_warehouse" -> Fn(#"Server", #"Warehouse")                 (Snowflake)
#     "server_httppath"  -> Fn(#"Server", #"HttpPath")                  (Databricks SQL warehouse)
#   nav_style:
#     "schema_item"            -> Source{[Schema=.., Item=..]}[Data]     (flat ADO.NET navigation)
#     "database_schema_table"  -> 3 hops keyed by [Name=.., Kind=..]     (Snowflake + Databricks)
#
# The Microsoft SQL Server TDS-protocol family: every Tableau class here speaks the SQL Server
# wire protocol, so each binds through the SAME emitter -- Sql.Database(#"Server", #"Database")
# with flat [Schema, Item] navigation, DirectQuery-capable. Grouped explicitly so a new Microsoft
# TDS surface is a one-line addition. Verified Tableau connection-class strings:
#   'sqlserver'                     -> Microsoft SQL Server AND Azure SQL Managed Instance
#                                      (Tableau connects to Managed Instance with the SQL Server
#                                      connector, so MI arrives as 'sqlserver').
#   'azure_sqldb'                   -> Azure SQL Database.
#   'azure_sql_dw'                  -> Azure Synapse Analytics, BOTH dedicated and serverless SQL
#                                      pool (the Synapse connector emits one class for either pool).
#   'microsoft_fabric_sql_endpoint' -> Microsoft Fabric Warehouse / Lakehouse SQL analytics endpoint.
# ('sqlserver' / 'azure_sqldb' are confirmed by real .tds; 'azure_sql_dw' /
# 'microsoft_fabric_sql_endpoint' are web-verified -- a wrong class string only causes a safe
# fallback, never wrong M, since the TDS->Sql.Database mapping itself is the verified fact.)
SQL_SERVER_TDS_FAMILY = (
    "sqlserver",
    "azure_sqldb",
    "azure_sql_dw",
    "microsoft_fabric_sql_endpoint",
)

# Verified facts (Microsoft Power Query M / connector docs):
#  * Sql/PostgreSQL/MySQL/AmazonRedshift.Database take (server, database) + flat [Schema, Item].
#    The whole SQL_SERVER_TDS_FAMILY above binds through Sql.Database on this same shape.
#  * Oracle.Database(server, [options]) is server-only (the M function reference page confirms the
#    signature), and HierarchicalNavigation defaults false, so the flat [Schema, Item] navigation
#    (schema = owner) applies. We set HierarchicalNavigation=false explicitly so the flat selector
#    is correct rather than default-reliant.
#  * Snowflake connector: connection inputs are Server + Warehouse; navigation is
#    database -> schema -> table. (Snowflake.Databases has no M function reference page, so its
#    navigation selectors are doc-informed, but the emitted M has been reconciled against a live
#    Snowflake instance and resolves end-to-end.)
#  * Databricks.Catalogs(host, httpPath, [options]) (official MS doc): navigation is
#    catalog -> schema -> table, and the catalog hop is keyed Kind="Database" -- byte-identical
#    to Snowflake's [Name, Kind] navigation, so it reuses "database_schema_table". The HTTP path
#    is a connection parameter (#"HttpPath") that is not stored portably in the .tds (the user
#    supplies it at bind time); the emitted M has been reconciled against a live Databricks
#    instance and resolves end-to-end.
DIRECT_CONNECTORS = {
    # Microsoft SQL Server TDS family -> Sql.Database(server, database) + flat [Schema, Item].
    **{cls: ("Sql.Database", "server_database", "schema_item") for cls in SQL_SERVER_TDS_FAMILY},
    "postgres":     ("PostgreSQL.Database",     "server_database",  "schema_item"),
    "mysql":        ("MySQL.Database",          "server_database",  "schema_item"),
    "redshift":     ("AmazonRedshift.Database", "server_database",  "schema_item"),
    "oracle":       ("Oracle.Database",         "server_only",      "schema_item"),
    "snowflake":    ("Snowflake.Databases",     "server_warehouse", "database_schema_table"),
    "databricks":   ("Databricks.Catalogs",     "server_httppath",  "database_schema_table"),
}

# Connectors whose custom-SQL native query is auto-emitted via the catalog drill --
# Source{[Name=<catalog>, Kind="Database"]}[Data] -> Value.NativeQuery(<handle>, sql) -- because
# that exact shape has been reconciled against a LIVE instance. The drilled [Data] handle exposes
# the native-query capability; the connector's root collection (e.g. Databricks.Catalogs(...))
# does NOT, so a native query against the root is rejected ("Native queries aren't supported by
# this value"). Membership here therefore means "the DRILLED form is verified", never the root.
#
#   * databricks -- live-verified 2026-06 (Databricks.Catalogs -> Kind="Database" drill).
#
# Snowflake shares the database_schema_table nav shape but is deliberately NOT included: its
# drilled-handle native-query capability, mandatory compute warehouse, and uppercase identifier
# folding are unverified against live, so it stays scaffolded (same charter as PARTIAL_LIVE_CONNECTORS).
# Promotion is a one-line addition here once a connector's drilled native query is confirmed live.
NATIVE_QUERY_CATALOG_DRILL = {"databricks"}

# Recognized live connectors that are deliberately NOT auto-emitted yet: their navigation
# selector or required identifiers cannot be verified offline, so emitting a call body would be
# a guess. We pick a mode but mark it not fully supported and emit a clearly-flagged scaffold
# that names the intended connector. Promotion is gated on doc-verified correctness.
PARTIAL_LIVE_CONNECTORS = {
    # GoogleBigQuery.Database([BillingProject=..]) has no M function reference page (the connector
    # doc lists no function reference), so neither the project/dataset/table navigation selectors
    # nor the billing-project vs project mapping in the .tds can be verified from an official
    # source -- it stays a scaffold pending a primary-doc shape or a real BigQuery datasource.
    "bigquery": "GoogleBigQuery.Database",
    # Teradata.Database(server, [options]) has a documented server-only signature, BUT there is no
    # live Teradata navigator in the validation environment to confirm the emitted flat-navigation
    # body actually binds (schema = Teradata database). Rather than ship M that has never resolved
    # against a real instance, Teradata is held as a flagged scaffold (recognized + mode chosen)
    # until a real navigator confirms it -- consistent with "never ship unverified-against-live M".
    "teradata": "Teradata.Database",
}

# Microsoft Analysis Services (SSAS / MSOLAP). This is NOT a relational datasource we rebuild
# into an M partition: the source is ALREADY a tabular/multidimensional semantic model. It needs
# a separate model-migration path (e.g. XMLA / semantic-model import), so we recognize it, route
# it away from both the M emitters and the land-to-Delta pipeline, and flag it explicitly.
ANALYSIS_SERVICES_CLASSES = {"msolap", "sqlserver-analysis-services"}

FLAT_FILE_CLASSES = {
    "excel-direct": "Excel.Workbook",
    "excel": "Excel.Workbook",
    "textscan": "Csv.Document",
    "csv": "Csv.Document",
}

# Generic ODBC. Unlike the DIRECT_CONNECTORS, an ODBC source has no fixed connector function:
# the driver named in the .tds (or its DSN) is what binds, and a Custom SQL relation passes its
# query straight THROUGH the driver to whatever engine sits behind it -- so the SAME emitter is
# correct regardless of the upstream (e.g. a query engine such as Trino / Starburst / Dremio
# fronting object storage like MinIO). Power Query's generic ``Odbc.Query`` (custom SQL) /
# ``Odbc.DataSource`` (tables) are the faithful targets, engine-agnostic by construction. The
# JDBC sibling class ('genericjdbc') is deliberately NOT here: Power BI has no JDBC connector
# (it cannot load Java drivers), so a JDBC source stays on the land-to-Delta fallback.
ODBC_CLASSES = {"genericodbc"}

# Native Tableau query-engine classes (Spark / Presto / Trino / Starburst). Power BI has no
# first-party connector that transfers these cleanly without a live-verified scaffold, but each
# engine ships an ODBC driver -- so we promote them to first-class by binding over the SAME
# engine-agnostic ODBC emitter as ODBC_CLASSES (Odbc.Query for custom SQL / Odbc.DataSource for
# tables), Import by default, and NEVER landing in Delta. Unlike a 'genericodbc' .tds these carry
# no odbc-driver attribute (Tableau used its bundled native driver), but the inner <connection>
# still records server / port / catalog, so the connection IS reconstructable; the one missing
# value -- the ODBC driver NAME -- is supplied per-engine below and flagged confirm-required.
NATIVE_ODBC_ENGINES = {"spark", "presto", "trino", "starburst"}

# Per-engine default ODBC driver name. The exact registered name varies by installer version (hence
# the confirm-required follow-up). Trino and its enterprise distribution Starburst share the
# Starburst Trino driver; Spark and Presto bind through the Simba drivers Tableau bundles.
NATIVE_ODBC_DRIVER = {
    "spark": "Simba Spark ODBC Driver",
    "presto": "Simba Presto ODBC Driver",
    "trino": "Starburst ODBC Driver for Trino",
    "starburst": "Starburst ODBC Driver for Trino",
}

# Connector classes a hyper extract may sit over; used only to report whether a live
# alternative exists for an extracted datasource.
_LIVE_CLASSES = set(DIRECT_CONNECTORS) | set(PARTIAL_LIVE_CONNECTORS)


def connector_spec(cls):
    """Return the ``(function, connect_style, nav_style)`` spec for a fully-supported direct
    connector class, or ``None`` if the class is not auto-emitted (scaffold / flat / unknown)."""
    return DIRECT_CONNECTORS.get((cls or "").lower())


def connector_function(cls):
    """Return the Power Query M function for a connector class (fully-supported or recognized
    scaffold), or ``None`` if the class is unmapped."""
    cls = (cls or "").lower()
    spec = DIRECT_CONNECTORS.get(cls)
    return spec[0] if spec else PARTIAL_LIVE_CONNECTORS.get(cls)

FALLBACK_LAND_TO_DELTA = "land-to-delta-directlake"
# The honest default for an unresolved / undoable shape: no direct-to-source rebuild was safe, so a
# STORAGE DECISION is required. This is NOT DirectLake -- DirectLake (land-to-Delta) is opt-in only
# and is never auto-stamped here. ``recommended_mode`` defaults to Import (rebuild direct-to-source
# where a connection can be supplied); the follow-ups point at the explicit land-to-Delta OPT-IN.
FALLBACK_NEEDS_DECISION = "needs-storage-decision"
# Analysis Services is a finished semantic model, not a datasource to rebuild -- it gets its own
# routing label so callers don't mistake it for the relational needs-decision fallback.
FALLBACK_ANALYSIS_SERVICES = "analysis-services-model-migration"

# Confidence scores (0-100) for the scored recommendation: higher == less manual remapping.
# They rank feasibility, not data quality -- a fully-supported live connector needs the least
# hand-finishing, a flagged scaffold needs more, and a fallback needs the land-to-Delta path.
SCORE_DIRECTQUERY_FULL = 95   # live, fully-supported (server, database) connector
SCORE_IMPORT_FULL = 90        # extract over a fully-supported live source
SCORE_FLAT_FILE = 80          # Excel/CSV Import (still needs a file path)
SCORE_PARTIAL = 60            # recognized connector emitted as a flagged scaffold
SCORE_ODBC = 60              # generic-ODBC custom SQL emitted via Odbc.Query (needs driver + creds)
SCORE_FALLBACK = 30           # no direct rebuild; storage decision required (Import default / opt-in DirectLake)
NATIVE_QUERY_PENALTY = 10     # custom-SQL native query needs a folding review before refresh

_CREDENTIALS_FOLLOWUP = "Configure connection credentials in Fabric (bind links IDs only)."
_GATEWAY_FOLLOWUP = "If the source is on-premises, set up / select a data gateway for the connection."
_NATIVE_QUERY_FOLLOWUP = "Review the preserved custom SQL native query (folding / approval) before refresh."
# Pointer for a needs-decision shape: default to a direct-to-source Import rebuild where a connection
# can be supplied; DirectLake (land-to-Delta) is an explicit OPT-IN, never auto-selected here.
_NEEDS_DECISION_FOLLOWUP = (
    "No safe direct-to-source rebuild for this shape: choose a storage approach. Default -- rebuild "
    "directly as an Import model once a connection can be supplied. Opt-in -- land the source to Delta "
    "and rebuild as DirectLake (never auto-selected; must be chosen deliberately).")
# An extract-backed source on a connector with NO first-party Power BI rebuild (Salesforce and other
# SaaS): the .hyper extract IS a point-in-time snapshot of the data, so we land it as an offline
# Import over the materialized extract (one table per CSV), exactly like a flat-file Import -- rather
# than dying at the unknown-connector needs-decision branch. There is no live upstream to rebuild;
# refreshing means re-exporting the extract.
_EXTRACT_IMPORT_FOLLOWUP = (
    "Extract-backed source with no first-party Power BI connector: imported as an offline snapshot of "
    "the bundled extract (one table per CSV). To refresh, re-export the Tableau extract from the "
    "source, or opt in to land-to-Delta + DirectLake (never auto-selected).")
# Generic ODBC binds through a named driver (or DSN); that driver must be present wherever the
# model runs. It is the SAME driver Tableau already uses, so this is a known quantity, not a new
# dependency -- but Power BI Desktop (authoring) and the on-premises data gateway (refresh) each
# need it installed, so we always surface it.
_ODBC_DRIVER_FOLLOWUP = ("Install the matching ODBC driver where the model runs (Power BI Desktop to "
                         "author, the on-premises data gateway to refresh) -- it is the same driver "
                         "Tableau uses; then set the connection credentials in Fabric.")


def _native_engine_driver_followup(cls, driver):
    """Confirm-required follow-up for a native query engine bound over ODBC. The native .tds did
    not record an ODBC driver name (Tableau used its bundled driver), so we assume a per-engine
    default; the user must confirm it matches the driver actually installed."""
    return (f"{cls.capitalize()}: no first-party Power BI connector exists, so the model binds over "
            f"ODBC using the assumed driver '{driver}'. The .tds did not record an ODBC driver name "
            "(Tableau used its bundled native driver), so confirm this matches the ODBC driver "
            "installed where the model runs (Power BI Desktop to author, the on-premises data "
            "gateway to refresh); adjust the Driver=/Host= clauses to your driver's spelling if "
            "needed, then set the connection credentials in Fabric.")
# Databricks emits a doc-verified function shape, but two values can't be sourced portably from
# the .tds: the SQL-warehouse HTTP path and (depending on the workbook) the Unity Catalog name.
_DATABRICKS_FOLLOWUP = ('Databricks: set the SQL-warehouse HTTP Path parameter (#"HttpPath") and confirm '
                        "the catalog name (mapped from the Tableau database) matches your Unity Catalog catalog.")
# Snowflake stores the compute warehouse as a connection attribute that can be empty in the .tds;
# Snowflake.Databases needs a real warehouse to run queries, so flag it when it's missing.
_SNOWFLAKE_WAREHOUSE_FOLLOWUP = ('Snowflake: the .tds carried no compute warehouse; set the #"Warehouse" '
                                 "parameter to a valid warehouse before refresh.")


def _decision(mode, connector, **kw):
    # `recommended_mode` is the storage mode to default to if the model is rebuilt directly;
    # it equals `mode` when a direct rebuild is possible and falls back to "Import" when
    # `mode` is None (the `fallback` pipeline is otherwise the authoritative route).
    recommended_mode = kw.pop("recommended_mode", None) or mode or "Import"
    d = {
        "mode": mode,
        "connector": connector,
        "fully_supported": False,
        "uses_native_query": False,
        "direct_upstream_available": False,
        "fallback": None,
        "rationale": "",
        "manual_followups": [],
        "score": kw.pop("score", SCORE_FALLBACK),
        "recommended_mode": recommended_mode,
    }
    d.update(kw)
    return d


def _has_custom_sql(descriptor):
    return any(r.get("kind") == "custom_sql" for r in descriptor.get("relations", []))


def _odbc_reconstructable(descriptor):
    """True if a generic-ODBC descriptor carries enough to rebuild a connection string.

    The precondition mirrors ``connection_to_m._odbc_connection_string``: a DSN name (the DSN
    encapsulates driver + host) OR a driver name (a driver-based string is then built from
    server/port/database). With neither there is nothing to bind, so the caller fails closed to
    the land-to-Delta fallback. Reads only descriptor facts -- no XML, no secrets.
    """
    return bool((descriptor.get("odbc_dsn") or "").strip()
                or (descriptor.get("odbc_driver") or "").strip())


def _structurally_unsupported_reason(descriptor):
    """Return a reason string if the datasource shape can't be rebuilt directly, else None.

    DEFAULT IS DIRECT. A datasource with MULTIPLE named connections is still rebuilt directly --
    each table binds to its OWN upstream (a multi-source model) and Tableau's join keys are
    re-created as model *relationships*. Power BI relates such tables in the model layer, so a
    federation of independent tables needs no land-to-Delta step. Direct rebuild is unsafe only
    when:

    * a table can't be routed to a SPECIFIC connection (so we can't tell which upstream a table
      in a multi-connection source comes from), or
    * one logical table spans several relations as a ``join``/``union`` (a row-level join that no
      single direct query against one source can reproduce), or
    * the shape is unknown / has no typable columns.

    Only those fall back to land-to-Delta + DirectLake (offered as an explicit option, not the
    default). A single named connection is always fine.
    """
    reasons = list(descriptor.get("unsupported_reasons", []))
    relations = descriptor.get("relations", [])
    table_like = [r for r in relations if r.get("kind") in ("table", "custom_sql")]
    if descriptor.get("named_connection_count", 0) > 1:
        unrouted = [r for r in table_like if not r.get("connection")]
        if unrouted:
            names = ", ".join(repr(r.get("name")) for r in unrouted)
            reasons.append(
                f"multiple named connections but {len(unrouted)} table(s) don't resolve to a "
                f"specific connection ({names}); can't bind them to a single upstream")
    kinds = {r.get("kind") for r in relations}
    if kinds & {"join", "union", "unknown"}:
        reasons.append("join/union relation tree (one logical table spans multiple relations)")
    if not table_like:
        reasons.append("no table or custom-SQL relations found")
    elif all(not r.get("columns") for r in table_like):
        reasons.append("no resolvable column metadata (cannot type the model deterministically)")
    return "; ".join(dict.fromkeys(reasons)) or None


def select_storage_mode(descriptor):
    """Choose a storage mode for one Tableau datasource descriptor.

    Returns a decision dict: ``mode`` ('Import'|'DirectQuery'|None), ``connector``,
    ``fully_supported``, ``uses_native_query``, ``direct_upstream_available``,
    ``fallback`` (e.g. 'needs-storage-decision' when ``mode`` is None -- DirectLake is opt-in only
    and is never auto-stamped), ``rationale``, ``manual_followups`` (security-boundary steps that
    stay with the user), plus the scored recommendation: ``score`` (0-100 confidence; higher ==
    less manual remapping) and ``recommended_mode`` (the mode to default to -- equal to ``mode``
    for a direct rebuild, or 'Import' when ``mode`` is None, since unknown/unsupported shapes
    default to a direct-to-source Import rebuild rather than an automatic DirectLake landing).
    """
    cls = (descriptor.get("connection_class") or "").lower()
    uses_native = _has_custom_sql(descriptor)
    base_followups = [_CREDENTIALS_FOLLOWUP]
    if cls == "databricks":
        base_followups = base_followups + [_DATABRICKS_FOLLOWUP]
    if cls == "snowflake" and not (descriptor.get("warehouse") or "").strip():
        base_followups = base_followups + [_SNOWFLAKE_WAREHOUSE_FOLLOWUP]

    # 0. Analysis Services (SSAS / MSOLAP): the source is already a tabular/multidimensional
    #    semantic model. It is NOT a datasource->M rebuild and must NOT be routed to the
    #    relational land-to-Delta path -- migrate the model directly (XMLA / semantic model).
    if cls in ANALYSIS_SERVICES_CLASSES:
        return _decision(
            None, None,
            fallback=FALLBACK_ANALYSIS_SERVICES,
            score=SCORE_FALLBACK,
            rationale=(f"Microsoft Analysis Services ({cls}) is already a tabular/multidimensional "
                       "semantic model, not a datasource to rebuild; migrate the model directly "
                       "(XMLA endpoint / semantic-model import) rather than emitting an M partition."),
            manual_followups=base_followups + [
                "Migrate the SSAS/MSOLAP model via its XMLA endpoint or a semantic-model import; "
                "do not rebuild it from a datasource M query."],
        )

    # 1. structurally unsupported -> no safe direct rebuild; route to an honest needs-decision
    #    state (DirectLake is opt-in only and is never auto-stamped here).
    reason = _structurally_unsupported_reason(descriptor)
    if reason:
        return _decision(
            None, None,
            fallback=FALLBACK_NEEDS_DECISION,
            score=SCORE_FALLBACK,
            rationale=f"Direct-upstream rebuild not safe ({reason}); storage decision required -- "
                      f"default to a direct-to-source Import rebuild once a connection can be "
                      f"supplied, or opt in to land-to-Delta + DirectLake (never auto-selected).",
            manual_followups=base_followups + [_NEEDS_DECISION_FOLLOWUP],
        )

    # 1.5 generic ODBC -> Odbc.Query (custom SQL) / Odbc.DataSource (tables), Import by default.
    #     The query passes straight through the named driver to whatever engine sits behind it,
    #     so this is engine-agnostic (the upstream can be a query engine such as Trino / Starburst
    #     / Dremio over object storage). A Custom SQL snapshot mirrors how Tableau uses the source,
    #     so Import is the natural mode. Fails closed to a needs-decision state when the .tds carries
    #     neither a DSN nor a driver name (nothing to bind).
    if cls in ODBC_CLASSES:
        if not _odbc_reconstructable(descriptor):
            return _decision(
                None, None,
                fallback=FALLBACK_NEEDS_DECISION,
                score=SCORE_FALLBACK,
                rationale=(f"Generic ODBC source ({cls}) carried neither a DSN nor a driver name, "
                           "so no connection string can be reconstructed; storage decision required "
                           "-- default Import once the driver/DSN is supplied, or opt in to "
                           "land-to-Delta + DirectLake (never auto-selected)."),
                manual_followups=base_followups + [_NEEDS_DECISION_FOLLOWUP],
            )
        connector = "Odbc.Query" if uses_native else "Odbc.DataSource"
        followups = list(base_followups) + [_ODBC_DRIVER_FOLLOWUP, _GATEWAY_FOLLOWUP]
        if uses_native:
            followups.append(_NATIVE_QUERY_FOLLOWUP)
        return _decision(
            "Import", connector,
            fully_supported=False,
            uses_native_query=uses_native,
            score=SCORE_ODBC,
            rationale=(f"Generic ODBC source ({cls}) -> Import via "
                       + ("Odbc.Query (the custom SQL passes through the driver to the upstream "
                          "engine, preserving its dialect)." if uses_native
                          else "Odbc.DataSource.")
                       + " Engine-agnostic: the driver/DSN named in the .tds binds the connection."),
            manual_followups=followups,
        )

    # 1.6 native query engine (Spark / Presto / Trino / Starburst) -> route through the SAME
    #     engine-agnostic ODBC emitter as generic ODBC (Odbc.Query for custom SQL / Odbc.DataSource
    #     for a table), Import by default, NEVER landing in Delta. These classes have no clean
    #     first-party Power BI connector, but each ships an ODBC driver and the .tds carries the
    #     server/port/catalog, so the connection IS reconstructable -- the only value the native
    #     .tds does not record is the ODBC driver NAME, which we supply per-engine and flag
    #     confirm-required. Placed before branches 2-5 so BOTH extract and live native-engine
    #     sources take the ODBC path. Fails closed to a needs-decision state only when there is no server.
    if cls in NATIVE_ODBC_ENGINES:
        if not (descriptor.get("server") or "").strip():
            return _decision(
                None, None,
                fallback=FALLBACK_NEEDS_DECISION,
                score=SCORE_FALLBACK,
                rationale=(f"{cls.capitalize()} source carried no server/host, so no ODBC connection "
                           "string can be reconstructed; storage decision required -- default Import "
                           "once a server/driver is supplied, or opt in to land-to-Delta + DirectLake "
                           "(never auto-selected)."),
                manual_followups=base_followups + [_NEEDS_DECISION_FOLLOWUP],
            )
        driver = NATIVE_ODBC_DRIVER[cls]
        connector = "Odbc.Query" if uses_native else "Odbc.DataSource"
        followups = list(base_followups) + [_native_engine_driver_followup(cls, driver), _GATEWAY_FOLLOWUP]
        if uses_native:
            followups.append(_NATIVE_QUERY_FOLLOWUP)
        return _decision(
            "Import", connector,
            fully_supported=False,
            uses_native_query=uses_native,
            score=SCORE_ODBC,
            rationale=(f"{cls.capitalize()} query engine -> Import via "
                       + ("Odbc.Query (the custom SQL passes through the ODBC driver to the engine, "
                          "preserving its SQL dialect)." if uses_native else "Odbc.DataSource.")
                       + f" Power BI has no first-party {cls} connector that transfers cleanly without "
                         f"a live-verified scaffold, so the connection is rebuilt over ODBC using the "
                         f"'{driver}' driver (confirm/replace with the driver installed where the model "
                         "runs); never landed in Delta."),
            manual_followups=followups,
        )

    # 1.7 extract-backed source on an UNMAPPED connector (SaaS such as Salesforce) -> land the
    #     bundled extract as an offline Import (one CSV per table), the same faithful snapshot home a
    #     flat-file extract gets. Placed BEFORE branch 2 (unknown-connector) so a SaaS extract lands
    #     its data instead of dying at needs-decision; the ``import_from_extract`` marker tells the
    #     assembler to materialize the .hyper -> CSV. Mapped-live extracts (branch 4) and flat-file
    #     extracts (branch 3) are excluded here and route unchanged. A structurally-unsupported shape
    #     (join / union tree, multi-connection) was already caught above, so it still fails closed.
    if descriptor.get("is_extract") and cls not in _LIVE_CLASSES and cls not in FLAT_FILE_CLASSES:
        return _decision(
            "Import", None,
            fully_supported=False,
            uses_native_query=uses_native,
            direct_upstream_available=False,
            import_from_extract=True,
            score=SCORE_FLAT_FILE,
            rationale=(f"Extract-backed source ({cls or 'unknown connector'}) with no first-party "
                       "Power BI connector -> Import over the bundled extract snapshot (one table per "
                       "CSV). No live upstream rebuild is available; never landed in Delta."),
            manual_followups=base_followups + [_EXTRACT_IMPORT_FOLLOWUP],
        )

    # 2. unknown connector class -> no mapped direct rebuild; route to needs-decision.
    if cls not in _LIVE_CLASSES and cls not in FLAT_FILE_CLASSES:
        return _decision(
            None, None,
            fallback=FALLBACK_NEEDS_DECISION,
            score=SCORE_FALLBACK,
            rationale=f"Connector class '{cls or 'unknown'}' is not mapped for direct M; storage "
                      f"decision required -- default to a direct-to-source Import rebuild, or opt in "
                      f"to land-to-Delta + DirectLake (never auto-selected).",
            manual_followups=base_followups + [_NEEDS_DECISION_FOLLOWUP],
        )

    # 3. flat file -> Import (mode is correct; M is a path-based scaffold, not Sql.Database).
    if cls in FLAT_FILE_CLASSES:
        return _decision(
            "Import", FLAT_FILE_CLASSES[cls],
            fully_supported=False,
            score=SCORE_FLAT_FILE,
            rationale=f"Flat-file source ({cls}) -> Import.",
            manual_followups=base_followups + [
                f"Set the file path (and sheet/range) for the {FLAT_FILE_CLASSES[cls]} M partition."],
        )

    # 4. extract enabled -> Import snapshot; offer live alternative when the connector is live.
    if descriptor.get("is_extract"):
        connector = connector_function(cls)
        live_available = cls in _LIVE_CLASSES
        fully = cls in DIRECT_CONNECTORS
        followups = list(base_followups)
        if uses_native:
            followups.append(_NATIVE_QUERY_FOLLOWUP)
        score = (SCORE_IMPORT_FULL if fully else SCORE_PARTIAL) - (NATIVE_QUERY_PENALTY if uses_native else 0)
        return _decision(
            "Import", connector,
            fully_supported=fully,
            uses_native_query=uses_native,
            direct_upstream_available=live_available,
            score=score,
            rationale=("Tableau extract enabled -> Import (preserves snapshot semantics). "
                       + ("A live DirectQuery rebuild against the upstream source is also available."
                          if live_available else "")),
            manual_followups=followups,
        )

    # 5. live relational -> DirectQuery.
    connector = connector_function(cls)
    fully = cls in DIRECT_CONNECTORS
    followups = base_followups + [_GATEWAY_FOLLOWUP]
    if uses_native:
        followups.append(_NATIVE_QUERY_FOLLOWUP)
    if not fully:
        followups.append(f"Complete the M partition for {connector} (its signature/navigation differs "
                         f"from the Sql.Database family; emitted as a flagged scaffold).")
    score = (SCORE_DIRECTQUERY_FULL if fully else SCORE_PARTIAL) - (NATIVE_QUERY_PENALTY if uses_native else 0)
    return _decision(
        "DirectQuery", connector,
        fully_supported=fully,
        uses_native_query=uses_native,
        score=score,
        rationale=(f"Live {cls} connection -> DirectQuery (live-to-live)."
                   if fully else
                   f"Live {cls} connection -> DirectQuery, but {connector} M is not auto-emitted in v1."),
        manual_followups=followups,
    )
