#!/usr/bin/env python3
"""Deep comparison engine: match Tableau datasources to Fabric semantic models.

Pure and offline -- **no network**. Consumes two inventories (the JSON shapes produced by
``tableau_inventory.py`` and ``fabric_inventory.py``) and, for every Tableau datasource, scores it
against every Fabric semantic model on a weighted blend of four independent signals:

    name    -- token-set similarity of the asset names
    column  -- name overlap of fields/columns (Jaccard)
    type    -- data-type compatibility across the overlapping columns
    source  -- overlap of the underlying physical sources (connector + database + table)

Each datasource is assigned its best-matching model and a tier band from
``Exact -> Strong -> Partial -> Weak -> None`` ("most comparable -> no comparison"). The estate
rollup counts how many datasources already exist in Fabric vs. need a rebuild.

This module is deliberately self-contained (no imports from the other skills) so the skill folder is
independently movable. Original work; see resources/comparison-methodology.md.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------------------
# Defaults (all overridable by the caller)
# --------------------------------------------------------------------------------------
DEFAULT_WEIGHTS: Dict[str, float] = {
    "name": 0.20,
    "column": 0.35,
    "type": 0.15,
    "source": 0.30,
}

# Score >= threshold  ->  tier.  Checked high-to-low.
DEFAULT_BANDS: List[Tuple[str, float]] = [
    ("Exact", 0.85),
    ("Strong", 0.65),
    ("Partial", 0.40),
    ("Weak", 0.15),
    ("None", 0.0),
]

TIER_ORDER = ["Exact", "Strong", "Partial", "Weak", "None"]

# How the rollup buckets each tier.
_ALREADY_EXIST = {"Exact", "Strong"}
_PARTIAL = {"Partial"}
_REBUILD = {"Weak", "None"}

_RECOMMENDED_ACTION = {
    "Exact": "Already in Fabric -- reuse the existing semantic model; do not rebuild.",
    "Strong": "Very likely already in Fabric -- verify the candidate, then reuse instead of rebuilding.",
    "Partial": "Partial overlap -- reconcile differences (added/renamed columns, source drift) before reusing.",
    "Weak": "No real equivalent -- rebuild via the tableau-migration skill.",
    "None": "No equivalent in Fabric -- rebuild via the tableau-migration skill.",
}


# --------------------------------------------------------------------------------------
# Tableau dataType -> compatible Fabric/TMDL column dataTypes
# --------------------------------------------------------------------------------------
# Tableau Metadata API field dataTypes are upper-case (INTEGER, REAL, STRING, ...); TMDL column
# dataTypes are camelCase (int64, double, string, dateTime, ...). A Tableau type is "compatible"
# with a Fabric type if the Fabric type appears in the mapped set. Unknown/ambiguous Tableau types
# map to None, which is treated as "compatible with anything" so we never penalise on missing info.
_TYPE_MAP: Dict[str, Optional[set]] = {
    "INTEGER": {"int64", "decimal", "double"},
    "REAL": {"double", "decimal", "int64"},
    "FLOAT": {"double", "decimal", "int64"},
    "NUMBER": {"double", "decimal", "int64"},
    "STRING": {"string"},
    "BOOLEAN": {"boolean"},
    "BOOL": {"boolean"},
    "DATE": {"datetime", "date"},
    "DATETIME": {"datetime", "date"},
    "SPATIAL": {"string", "binary"},
    "UNKNOWN": None,
    "TUPLE": None,
    "TABLE": None,
}

# Tableau connector class names -> canonical connector token. Fabric M function names map to the
# same tokens in fabric_inventory.py, so source keys line up across the two clouds.
_CONNECTOR_CANON = {
    "sqlserver": "sqlserver",
    "mssql": "sqlserver",
    "azure-sql": "sqlserver",
    "azuresqldb": "sqlserver",
    "azure_sqldb": "sqlserver",
    "snowflake": "snowflake",
    "postgres": "postgres",
    "postgresql": "postgres",
    "redshift": "redshift",
    "bigquery": "bigquery",
    "google-big-query": "bigquery",
    "oracle": "oracle",
    "mysql": "mysql",
    "databricks": "databricks",
    "spark": "databricks",
    "excel-direct": "excel",
    "textscan": "file",
    "hyper": "extract",
}


def canonical_connector(value: Optional[str]) -> str:
    """Fold a raw connector/connection-type string to a stable token (``other`` if unrecognised)."""
    if not value:
        return "other"
    key = re.sub(r"[^a-z0-9]+", "", str(value).lower())
    for raw, canon in _CONNECTOR_CANON.items():
        if re.sub(r"[^a-z0-9]+", "", raw) == key:
            return canon
    # Substring fallback for verbose connection-type names (e.g. "microsoft sql server").
    low = str(value).lower()
    if "snowflake" in low:
        return "snowflake"
    if "postgres" in low:
        return "postgres"
    if "sql server" in low or "sqlserver" in low or "mssql" in low or "sqldb" in low:
        return "sqlserver"
    if "redshift" in low:
        return "redshift"
    if "bigquery" in low:
        return "bigquery"
    if "oracle" in low:
        return "oracle"
    if "mysql" in low:
        return "mysql"
    if "databricks" in low:
        return "databricks"
    return key or "other"


# --------------------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------------------
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
# Noise tokens that shouldn't drive a name match on their own.
_NAME_STOPWORDS = {
    "the", "a", "an", "of", "and",
    "datasource", "data", "source", "ds",
    "model", "semantic", "dataset",
    "extract", "live", "copy", "final", "v1", "v2", "prod", "dev", "test",
}


def normalize_token(value: Optional[str]) -> str:
    """Lower-case and strip every non-alphanumeric character (``[Sales Amount]`` -> ``salesamount``)."""
    if value is None:
        return ""
    return _TOKEN_SPLIT.sub("", str(value).lower())


def tokenize_name(value: Optional[str]) -> set:
    """Split a display name into a set of meaningful lower-case tokens (stopwords removed)."""
    if not value:
        return set()
    toks = {t for t in _TOKEN_SPLIT.split(str(value).lower()) if t}
    meaningful = {t for t in toks if t not in _NAME_STOPWORDS}
    return meaningful or toks  # never return empty if the name was all-stopwords


def jaccard(a: Iterable, b: Iterable) -> float:
    """Jaccard similarity of two iterables as sets. Two empty sets -> 0.0."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def type_compatible(tableau_type: Optional[str], fabric_type: Optional[str]) -> bool:
    """True if a Tableau dataType is compatible with a Fabric/TMDL column dataType."""
    if not tableau_type or not fabric_type:
        return True
    allowed = _TYPE_MAP.get(str(tableau_type).strip().upper(), None)
    if allowed is None:  # unknown Tableau type -> don't penalise
        return True
    return str(fabric_type).strip().lower() in allowed


# --------------------------------------------------------------------------------------
# Field / column / source extraction (tolerant of partial inventories)
# --------------------------------------------------------------------------------------
def _field_name_map(fields: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """``[{name,dataType}]`` -> ``{normalized_name: dataType}`` (last write wins; blanks dropped)."""
    out: Dict[str, str] = {}
    for f in fields or []:
        key = normalize_token(f.get("name"))
        if not key:
            continue
        out[key] = f.get("dataType") or f.get("type") or ""
    return out


def _source_keys(sources: Sequence[Dict[str, Any]]) -> Tuple[set, set]:
    """Return (strict, loose) source key sets.

    strict = ``(connector, database, table)`` -- both sides agree on the catalog *and* the table.
    loose  = ``(connector, table)``           -- same table, possibly a different database name
                                                  (dev vs prod, renamed catalog).
    """
    strict, loose = set(), set()
    for s in sources or []:
        conn = canonical_connector(s.get("connectionType") or s.get("connector"))
        db = normalize_token(s.get("database"))
        tbl = normalize_token(s.get("table"))
        if not tbl:
            continue
        strict.add((conn, db, tbl))
        loose.add((conn, tbl))
    return strict, loose


# Model objects a Fabric semantic model commonly adds that are NOT physical source tables: a date
# dimension, a measures-holder table, parameter tables, and field-parameter "swap" tables. Excluding
# these keeps the table-name signal precise so a real source-table overlap is not diluted.
_HELPER_TABLE_TOKENS = frozenset(
    {"date", "dates", "calendar", "measures", "measure", "parameters", "parameter", "keymeasures"}
)


def _is_helper_table(token: str) -> bool:
    """True for normalized table names that look like model scaffolding rather than source tables."""
    if not token:
        return True
    if token in _HELPER_TABLE_TOKENS:
        return True
    # Field-parameter tables (e.g. "Measure Swap 1", "Dim Swap") and any explicitly-private table.
    return "swap" in token


def _table_name_set(
    sources: Sequence[Dict[str, Any]], tables: Optional[Sequence[Any]] = None
) -> set:
    """Durable, connector-agnostic table-name set used to match across a lakehouse boundary.

    In practice a Fabric model often sits on a Lakehouse/Warehouse that *mirrors* the primary source
    while the Tableau datasource connects to that source directly, so the connector and database never
    line up -- only the **table names** survive the move. We therefore collect bare table names from
    both the parsed physical ``sources`` and (for the Fabric side) the model's own ``tables`` list,
    dropping obvious helper tables.
    """
    out: set = set()
    for s in sources or []:
        tok = normalize_token(s.get("table"))
        if tok and not _is_helper_table(tok):
            out.add(tok)
    for t in tables or []:
        tok = normalize_token(t)
        if tok and not _is_helper_table(tok):
            out.add(tok)
    return out


# --------------------------------------------------------------------------------------
# Pairwise scoring
# --------------------------------------------------------------------------------------
def score_pair(
    tableau_ds: Dict[str, Any],
    fabric_model: Dict[str, Any],
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Score one Tableau datasource against one Fabric model. Returns signals + weighted score."""
    weights = weights or DEFAULT_WEIGHTS

    # -- name --------------------------------------------------------------------------
    tab_tokens = tokenize_name(tableau_ds.get("name"))
    fab_tokens = tokenize_name(fabric_model.get("name"))
    name_score = jaccard(tab_tokens, fab_tokens)
    if normalize_token(tableau_ds.get("name")) and (
        normalize_token(tableau_ds.get("name")) == normalize_token(fabric_model.get("name"))
    ):
        name_score = 1.0

    # -- column overlap ----------------------------------------------------------------
    tab_fields = _field_name_map(tableau_ds.get("fields", []))
    fab_columns = _field_name_map(fabric_model.get("columns", []))
    column_score = jaccard(tab_fields.keys(), fab_columns.keys())

    # -- type compatibility over the overlapping columns -------------------------------
    shared = set(tab_fields) & set(fab_columns)
    if shared:
        compatible = sum(
            1 for c in shared if type_compatible(tab_fields[c], fab_columns[c])
        )
        type_score = compatible / len(shared)
    else:
        type_score = 0.0

    # -- physical source overlap -------------------------------------------------------
    # FALLBACK FOR OBSCURED UPSTREAM SOURCES: composite/DirectQuery PBI models
    # (AnalysisServices, Power BI dataset, dataflow), Databricks/Snowflake expressions we can't
    # resolve to a table, Tableau datasources that reference another published datasource, and
    # extracts can all hide the ultimate physical table. When *either* side has no usable source,
    # we must NOT score source as 0 (that would bury a real schema-level overlap) -- instead we drop
    # the source signal and redistribute its weight across name/column/type.
    #
    # LAKEHOUSE-INTERMEDIARY CASE: a Fabric model frequently reads from a Lakehouse/Warehouse that
    # mirrors the primary source, while the Tableau datasource connects to that source directly. The
    # connector + database therefore never match -- only the table names do -- so we add a
    # connector-agnostic table-name tier (also drawing on the model's own ``tables`` list) and take
    # the best of the strict, loose, and table-only signals.
    tab_strict, tab_loose = _source_keys(tableau_ds.get("sources", []))
    fab_strict, fab_loose = _source_keys(fabric_model.get("sources", []))
    tab_tables = _table_name_set(tableau_ds.get("sources", []))
    fab_tables = _table_name_set(fabric_model.get("sources", []), fabric_model.get("tables", []))
    source_comparable = bool(tab_tables) and bool(fab_tables)
    if source_comparable:
        strict_score = jaccard(tab_strict, fab_strict)
        loose_score = jaccard(tab_loose, fab_loose)
        # Table-only overlap is the durable cross-platform signal; weight it just under a loose match.
        table_score = jaccard(tab_tables, fab_tables)
        source_score: Optional[float] = max(
            strict_score, 0.85 * loose_score, 0.7 * table_score
        )
    else:
        source_score = None

    signals: Dict[str, Optional[float]] = {
        "name": round(name_score, 4),
        "column": round(column_score, 4),
        "type": round(type_score, 4),
        "source": round(source_score, 4) if source_score is not None else None,
    }
    # Weight only the signals we could actually measure; this redistributes the source weight to the
    # remaining signals when the upstream source is obscured on either side.
    active = [k for k in ("name", "column", "type", "source") if signals[k] is not None]
    total_w = sum(weights.get(k, 0.0) for k in active) or 1.0
    score = sum(weights.get(k, 0.0) * signals[k] for k in active) / total_w

    return {
        "signals": signals,
        "score": round(score, 4),
        "source_compared": source_comparable,
        "shared_columns": sorted(shared),
        "shared_column_count": len(shared),
    }


def band_for(score: float, bands: Optional[List[Tuple[str, float]]] = None) -> str:
    """Map a 0..1 score to its tier label using the (label, min_score) table, high-to-low."""
    for label, threshold in (bands or DEFAULT_BANDS):
        if score >= threshold:
            return label
    return "None"


def rollup_bucket(tier: str) -> str:
    """Bucket a tier into one of ``already_exists`` / ``partial`` / ``rebuild``."""
    if tier in _ALREADY_EXIST:
        return "already_exists"
    if tier in _PARTIAL:
        return "partial"
    return "rebuild"


# --------------------------------------------------------------------------------------
# Estate-level comparison
# --------------------------------------------------------------------------------------
def compare_inventories(
    tableau: Sequence[Dict[str, Any]],
    fabric: Sequence[Dict[str, Any]],
    *,
    weights: Optional[Dict[str, float]] = None,
    bands: Optional[List[Tuple[str, float]]] = None,
    top_n: int = 3,
) -> Dict[str, Any]:
    """Compare every Tableau datasource against every Fabric model.

    Returns ``{"summary": {...}, "matches": [...]}`` where ``matches`` is sorted most-comparable
    first. Each match carries the best Fabric candidate, its tier, the four signal scores, and up to
    ``top_n`` runner-up candidates so the caller can show alternatives.
    """
    weights = weights or DEFAULT_WEIGHTS
    bands = bands or DEFAULT_BANDS
    fabric = list(fabric or [])

    matches: List[Dict[str, Any]] = []
    for ds in (tableau or []):
        scored: List[Dict[str, Any]] = []
        for fm in fabric:
            result = score_pair(ds, fm, weights)
            scored.append({
                "fabric_name": fm.get("name"),
                "workspace": fm.get("workspace"),
                "workspace_id": fm.get("workspaceId"),
                "fabric_id": fm.get("id"),
                "score": result["score"],
                "signals": result["signals"],
                "source_compared": result["source_compared"],
                "shared_column_count": result["shared_column_count"],
            })
        scored.sort(key=lambda c: c["score"], reverse=True)

        best = scored[0] if scored else None
        best_score = best["score"] if best else 0.0
        tier = band_for(best_score, bands) if best else "None"
        matches.append({
            "tableau_name": ds.get("name"),
            "project": ds.get("project"),
            "tableau_luid": ds.get("luid"),
            "tier": tier,
            "score": best_score,
            "bucket": rollup_bucket(tier),
            "source_compared": bool(best and best.get("source_compared")),
            "best_match": best if (best and best_score > 0) else None,
            "candidates": scored[:top_n],
        })

    matches.sort(key=lambda m: m["score"], reverse=True)

    by_tier = {t: 0 for t in TIER_ORDER}
    buckets = {"already_exists": 0, "partial": 0, "rebuild": 0}
    for m in matches:
        by_tier[m["tier"]] = by_tier.get(m["tier"], 0) + 1
        buckets[m["bucket"]] += 1

    summary = {
        "tableau_total": len(matches),
        "fabric_total": len(fabric),
        "by_tier": by_tier,
        "already_exist": buckets["already_exists"],
        "partial": buckets["partial"],
        "rebuild": buckets["rebuild"],
        "weights": dict(weights),
        "bands": [list(b) for b in bands],
    }
    return {"summary": summary, "matches": matches}


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------
def _action_for(tier: str) -> str:
    return _RECOMMENDED_ACTION.get(tier, "")


def render_markdown(result: Dict[str, Any]) -> str:
    """Render a comparison result as a human-readable Markdown report."""
    s = result["summary"]
    lines: List[str] = []
    lines.append("# Tableau -> Fabric datasource comparison")
    lines.append("")
    lines.append(
        f"Compared **{s['tableau_total']} Tableau datasource(s)** against "
        f"**{s['fabric_total']} Fabric semantic model(s)**."
    )
    lines.append("")
    lines.append("## Estate rollup")
    lines.append("")
    lines.append("| Outcome | Count | Meaning |")
    lines.append("|---|---:|---|")
    lines.append(
        f"| Already in Fabric | {s['already_exist']} | Exact/Strong match exists -- reuse, don't rebuild |"
    )
    lines.append(
        f"| Partial overlap | {s['partial']} | A related model exists -- reconcile before reuse |"
    )
    lines.append(
        f"| Needs rebuild | {s['rebuild']} | No real equivalent -- migrate via tableau-migration |"
    )
    lines.append("")
    lines.append("By tier: " + ", ".join(f"{t}={s['by_tier'].get(t, 0)}" for t in TIER_ORDER))
    lines.append("")
    lines.append("## Ranked matches (most comparable first)")
    lines.append("")
    lines.append("| Tableau datasource | Project | Best Fabric match | Workspace | Tier | Score | name/col/type/src |")
    lines.append("|---|---|---|---|---|---:|---|")
    for m in result["matches"]:
        best = m.get("best_match")
        fab = best["fabric_name"] if best else "_(none)_"
        ws = (best.get("workspace") if best else "") or ""
        sig = best["signals"] if best else {"name": 0, "column": 0, "type": 0, "source": None}
        src = "n/a" if sig.get("source") is None else f"{sig['source']:.2f}"
        sig_str = f"{sig['name']:.2f}/{sig['column']:.2f}/{sig['type']:.2f}/{src}"
        lines.append(
            f"| {m['tableau_name']} | {m.get('project') or ''} | {fab} | {ws} | "
            f"{m['tier']} | {m['score']:.2f} | {sig_str} |"
        )
    lines.append("")
    lines.append(
        "_`src = n/a` means the underlying physical source was obscured on one side "
        "(composite/DirectQuery model, unresolved connector, or a referenced datasource); "
        "the match relies on name + columns + types instead._"
    )
    lines.append("")
    lines.append("## Recommended actions")
    lines.append("")
    for tier in TIER_ORDER:
        names = [m["tableau_name"] for m in result["matches"] if m["tier"] == tier]
        if not names:
            continue
        lines.append(f"### {tier} ({len(names)})")
        lines.append("")
        lines.append(_action_for(tier))
        lines.append("")
        for n in names:
            lines.append(f"- {n}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
