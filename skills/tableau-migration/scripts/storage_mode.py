"""Per-datasource storage-mode auto-selection (pure, XML-free).

Given a normalized Tableau connection *descriptor* (produced by ``connection_to_m.parse_tds``),
decide which Power BI storage mode rebuilds the datasource with the least manual remapping,
or fall back to the land-to-Delta + DirectLake path when direct-to-upstream is unsafe.

This module is deliberately pure: it knows nothing about XML or TMDL syntax, only about the
descriptor shape, so the policy is trivially unit-testable. ``connection_to_m`` does the
parsing and M emission; it may *call* this to decide a mode, but never the reverse.

Decision policy (first match wins):

1. Structurally unsupported shape (join/union relation tree, >1 named connection, no
   resolvable columns) -> no direct mode; fall back to land-to-Delta + DirectLake.
2. Unknown / unmapped connector class -> fall back.
3. Flat file (Excel/CSV) -> Import.
4. Extract enabled -> Import (preserve Tableau snapshot semantics); if the underlying live
   connector is supported, also report ``direct_upstream_available`` so the caller can offer
   live DirectQuery as an explicit alternative.
5. Live relational -> DirectQuery (live-to-live).

DirectLake is never auto-selected here; it is only reached via the explicit fallback path
(the existing Play 3/4 land-to-Delta pipeline), per the friction-minimizing design.

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
# Verified facts (Microsoft Power Query M / connector docs):
#  * Sql/PostgreSQL/MySQL/AmazonRedshift.Database take (server, database) + flat [Schema, Item].
#    Azure SQL Database / Azure Synapse Analytics (dedicated + serverless SQL pool) / Azure SQL
#    Managed Instance / Microsoft Fabric SQL endpoints all speak the SQL Server TDS protocol, so
#    they bind through Sql.Database too. Managed Instance + serverless Synapse connect through the
#    SQL Server / Azure SQL connector ('sqlserver' / 'azure_sqldb'); dedicated Synapse uses
#    'azure_sql_dw'; the dedicated Fabric SQL endpoint uses 'microsoft_fabric_sql_endpoint'
#    (class strings web-verified -- a wrong class only causes a safe fallback, never wrong M; the
#    TDS->Sql.Database mapping is the verified fact).
#  * Oracle.Database(server, [options]) and Teradata.Database(server, [options]) are both
#    server-only (the M function reference pages confirm both signatures), and HierarchicalNavigation
#    defaults false on each, so the flat [Schema, Item] navigation (schema = owner / Teradata
#    database) applies. We set HierarchicalNavigation=false explicitly so the flat selector is
#    correct rather than default-reliant. Teradata reuses Oracle's verified server-only path; it has
#    no live instance here, so live reconciliation is pending.
#  * Snowflake connector: connection inputs are Server + Warehouse; navigation is
#    database -> schema -> table. (Snowflake.Databases has no M function reference page, so its
#    navigation selectors are doc-informed; live reconciliation is pending -- see docs.)
#  * Databricks.Catalogs(host, httpPath, [options]) (official MS doc): navigation is
#    catalog -> schema -> table, and the catalog hop is keyed Kind="Database" -- byte-identical
#    to Snowflake's [Name, Kind] navigation, so it reuses "database_schema_table". The HTTP path
#    is a connection parameter (#"HttpPath") that is not stored portably in the .tds; live
#    reconciliation is pending (no live Databricks instance).
DIRECT_CONNECTORS = {
    "sqlserver":    ("Sql.Database",            "server_database",  "schema_item"),
    "azure_sqldb":  ("Sql.Database",            "server_database",  "schema_item"),  # Azure SQL Database / Managed Instance (SQL Server protocol)
    "azure_sql_dw": ("Sql.Database",            "server_database",  "schema_item"),  # Azure Synapse Analytics (dedicated SQL pool)
    "microsoft_fabric_sql_endpoint": ("Sql.Database", "server_database", "schema_item"),  # Fabric Warehouse / Lakehouse SQL endpoint (best-effort class; fails safe to fallback)
    "postgres":     ("PostgreSQL.Database",     "server_database",  "schema_item"),
    "mysql":        ("MySQL.Database",          "server_database",  "schema_item"),
    "redshift":     ("AmazonRedshift.Database", "server_database",  "schema_item"),
    "oracle":       ("Oracle.Database",         "server_only",      "schema_item"),
    "teradata":     ("Teradata.Database",       "server_only",      "schema_item"),
    "snowflake":    ("Snowflake.Databases",     "server_warehouse", "database_schema_table"),
    "databricks":   ("Databricks.Catalogs",     "server_httppath",  "database_schema_table"),
}

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
# Analysis Services is a finished semantic model, not a datasource to rebuild -- it gets its own
# routing label so callers don't mistake it for the relational land-to-Delta fallback.
FALLBACK_ANALYSIS_SERVICES = "analysis-services-model-migration"

# Confidence scores (0-100) for the scored recommendation: higher == less manual remapping.
# They rank feasibility, not data quality -- a fully-supported live connector needs the least
# hand-finishing, a flagged scaffold needs more, and a fallback needs the land-to-Delta path.
SCORE_DIRECTQUERY_FULL = 95   # live, fully-supported (server, database) connector
SCORE_IMPORT_FULL = 90        # extract over a fully-supported live source
SCORE_FLAT_FILE = 80          # Excel/CSV Import (still needs a file path)
SCORE_PARTIAL = 60            # recognized connector emitted as a flagged scaffold
SCORE_FALLBACK = 30           # no direct rebuild; route to land-to-Delta + DirectLake
NATIVE_QUERY_PENALTY = 10     # custom-SQL native query needs a folding review before refresh

_CREDENTIALS_FOLLOWUP = "Configure connection credentials in Fabric (bind links IDs only)."
_GATEWAY_FOLLOWUP = "If the source is on-premises, set up / select a data gateway for the connection."
_NATIVE_QUERY_FOLLOWUP = "Review the preserved custom SQL native query (folding / approval) before refresh."
# Databricks emits a doc-verified function shape, but two values can't be sourced portably from
# the .tds: the SQL-warehouse HTTP path and (depending on the workbook) the Unity Catalog name.
_DATABRICKS_FOLLOWUP = ('Databricks: set the SQL-warehouse HTTP Path parameter (#"HttpPath") and confirm '
                        "the catalog name (mapped from the Tableau database) matches your Unity Catalog catalog.")


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


def _structurally_unsupported_reason(descriptor):
    """Return a reason string if the datasource shape can't be rebuilt directly, else None."""
    reasons = list(descriptor.get("unsupported_reasons", []))
    if descriptor.get("named_connection_count", 0) > 1:
        reasons.append("multiple named connections in one datasource")
    kinds = {r.get("kind") for r in descriptor.get("relations", [])}
    if kinds & {"join", "union", "unknown"}:
        reasons.append("join/union relation tree (one logical table spans multiple relations)")
    table_like = [r for r in descriptor.get("relations", []) if r.get("kind") in ("table", "custom_sql")]
    if not table_like:
        reasons.append("no table or custom-SQL relations found")
    elif all(not r.get("columns") for r in table_like):
        reasons.append("no resolvable column metadata (cannot type the model deterministically)")
    return "; ".join(dict.fromkeys(reasons)) or None


def select_storage_mode(descriptor):
    """Choose a storage mode for one Tableau datasource descriptor.

    Returns a decision dict: ``mode`` ('Import'|'DirectQuery'|None), ``connector``,
    ``fully_supported``, ``uses_native_query``, ``direct_upstream_available``,
    ``fallback`` (e.g. 'land-to-delta-directlake' when ``mode`` is None), ``rationale``,
    ``manual_followups`` (security-boundary steps that stay with the user), plus the scored
    recommendation: ``score`` (0-100 confidence; higher == less manual remapping) and
    ``recommended_mode`` (the mode to default to -- equal to ``mode`` for a direct rebuild, or
    'Import' when ``mode`` is None, since unknown/unsupported shapes default to an Import model).
    """
    cls = (descriptor.get("connection_class") or "").lower()
    uses_native = _has_custom_sql(descriptor)
    base_followups = [_CREDENTIALS_FOLLOWUP]
    if cls == "databricks":
        base_followups = base_followups + [_DATABRICKS_FOLLOWUP]

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

    # 1. structurally unsupported -> fall back to the proven land-to-Delta path.
    reason = _structurally_unsupported_reason(descriptor)
    if reason:
        return _decision(
            None, None,
            fallback=FALLBACK_LAND_TO_DELTA,
            score=SCORE_FALLBACK,
            rationale=f"Direct-upstream rebuild not safe ({reason}); use land-to-Delta + DirectLake "
                      f"(default storage mode if rebuilt directly: Import).",
        )

    # 2. unknown connector class -> fall back.
    if cls not in _LIVE_CLASSES and cls not in FLAT_FILE_CLASSES:
        return _decision(
            None, None,
            fallback=FALLBACK_LAND_TO_DELTA,
            score=SCORE_FALLBACK,
            rationale=f"Connector class '{cls or 'unknown'}' is not mapped for direct M; "
                      f"use land-to-Delta + DirectLake (default storage mode if rebuilt directly: Import).",
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
