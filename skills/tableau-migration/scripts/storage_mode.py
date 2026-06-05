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

# Connector classes whose M shape we can emit with confidence in v1. Membership is gated on
# one verified fact (from the Microsoft Power Query M docs): the connector takes the
# `<Connector>.Database(server, database)` shape and uses `Source{[Schema=..,Item=..]}[Data]`
# navigation, so the two-argument emission is correct rather than guessed.
SQL_DATABASE_FAMILY = {
    "sqlserver": "Sql.Database",
    "azure_sqldb": "Sql.Database",  # Azure SQL Database speaks the SQL Server protocol -> same connector
    "postgres": "PostgreSQL.Database",
    "mysql": "MySQL.Database",
    "redshift": "AmazonRedshift.Database",
}

# Live connectors that are recognized (verified Tableau class -> M function) but whose M
# signature or navigation differs from the `(server, database)` family, so emitting a
# two-argument call blind would be wrong. We pick a mode but mark it not fully supported and
# emit a clearly-flagged scaffold (or fall back) rather than guessing the call body.
PARTIAL_LIVE_CONNECTORS = {
    # Server-only signature: `<Connector>.Database(server, [options])` -- no (server, database)
    # form (database is reached by navigation), so the family's 2-arg emission does not apply.
    "oracle": "Oracle.Database",
    "teradata": "Teradata.Database",
    # Differently-shaped: Snowflake.Databases(server, [warehouse]) and
    # GoogleBigQuery.Database(location) use multi-level navigation, not Schema/Item.
    "snowflake": "Snowflake.Databases",
    "bigquery": "GoogleBigQuery.Database",
}

FLAT_FILE_CLASSES = {
    "excel-direct": "Excel.Workbook",
    "excel": "Excel.Workbook",
    "textscan": "Csv.Document",
    "csv": "Csv.Document",
}

# Connector classes a hyper extract may sit over; used only to report whether a live
# alternative exists for an extracted datasource.
_LIVE_CLASSES = set(SQL_DATABASE_FAMILY) | set(PARTIAL_LIVE_CONNECTORS)

FALLBACK_LAND_TO_DELTA = "land-to-delta-directlake"

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
        connector = SQL_DATABASE_FAMILY.get(cls) or PARTIAL_LIVE_CONNECTORS.get(cls)
        live_available = cls in _LIVE_CLASSES
        fully = cls in SQL_DATABASE_FAMILY
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
    connector = SQL_DATABASE_FAMILY.get(cls) or PARTIAL_LIVE_CONNECTORS.get(cls)
    fully = cls in SQL_DATABASE_FAMILY
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
