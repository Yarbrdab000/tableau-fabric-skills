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

import difflib
import math
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

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


# --------------------------------------------------------------------------------------
# Precision helpers: fuzzy-name fallback + generic-column down-weighting
# --------------------------------------------------------------------------------------
# A fuzzy (character-level) name match only contributes when the two normalised names are this
# similar, and is capped just under 1.0 so it can never beat a true exact-name match. This rescues
# abbreviations / typos / spacing ("SalesOrders" vs "Sales Order") without rewarding random overlap.
FUZZY_NAME_FLOOR = 0.6
FUZZY_NAME_CAP = 0.9

# Ubiquitous column names carry little discriminating power: a shared ``id`` / ``date`` / ``region``
# says far less about "same dataset" than a shared ``net_bookings_usd``. They are **down-weighted**
# (not dropped) in the column-overlap signal so a coincidental overlap of generic columns cannot, on
# its own, manufacture a match -- the exact "generic column" false-positive the methodology warns of.
# Generic-column stoplist (above) always applies. The estate IDF penalty is only *informative* on a
# real estate -- on a handful of assets a shared column trivially looks "ubiquitous" (df ~ N) and
# would be unfairly demoted -- so it is gated behind a minimum asset count.
GENERIC_COLUMN_WEIGHT = 0.25
MIN_IDF_ASSETS = 8
_GENERIC_COLUMN_TOKENS = frozenset({
    "id", "key", "pk", "fk", "uuid", "guid", "rowid", "index", "seq",
    "name", "fullname", "firstname", "lastname", "title", "label", "description", "desc",
    "code", "type", "category", "status", "state", "flag", "active", "enabled",
    "date", "datetime", "timestamp", "createddate", "modifieddate", "updateddate",
    "createdat", "updatedat", "year", "month", "day", "week", "quarter", "hour",
    "value", "amount", "qty", "quantity", "count", "total", "sum", "price", "cost",
    "region", "country", "state", "city", "zip", "zipcode", "postalcode", "address",
    "currency", "comment", "comments", "notes", "source", "version", "number", "num",
})


def name_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Name signal: token-set Jaccard, an exact-normalised short-circuit, and a capped fuzzy tail.

    Returns 1.0 for an exact normalised-name match. Otherwise the token-set Jaccard, raised to a
    character-level ``difflib`` ratio (``x FUZZY_NAME_CAP``) only when that ratio clears
    ``FUZZY_NAME_FLOOR`` -- so near-miss spellings score, random pairs do not.
    """
    na, nb = normalize_token(a), normalize_token(b)
    if na and na == nb:
        return 1.0
    score = jaccard(tokenize_name(a), tokenize_name(b))
    if na and nb:
        ratio = difflib.SequenceMatcher(None, na, nb).ratio()
        if ratio >= FUZZY_NAME_FLOOR:
            score = max(score, ratio * FUZZY_NAME_CAP)
    return score


def _default_col_weight(token: str) -> float:
    """Per-column weight with no estate context: generic names are down-weighted, others full."""
    return GENERIC_COLUMN_WEIGHT if token in _GENERIC_COLUMN_TOKENS else 1.0


def column_weight_fn(
    doc_freq: Optional[Dict[str, int]] = None, n_assets: int = 0
) -> Callable[[str], float]:
    """Build a column-weight function: the generic-name penalty blended with an estate IDF penalty.

    ``doc_freq`` maps a normalised column name to how many assets (Tableau datasources + Fabric
    models) contain it; ``n_assets`` is the asset count. A column that appears in nearly every asset
    gets a small weight (low information); a distinctive column keeps weight ~1.0. When no estate
    context is supplied this degrades to the generic-stoplist penalty only.
    """
    denom = math.log(n_assets + 1) if n_assets and n_assets >= MIN_IDF_ASSETS else 0.0

    def weight(token: str) -> float:
        base = GENERIC_COLUMN_WEIGHT if token in _GENERIC_COLUMN_TOKENS else 1.0
        if doc_freq and denom:
            df = doc_freq.get(token, 0)
            if df > 0:
                idf = math.log((n_assets + 1) / (df + 0.5)) / denom
                idf = max(0.15, min(1.0, idf))
                base *= idf
        return base

    return weight


def _weighted_jaccard(a: Iterable, b: Iterable, weight: Callable[[str], float]) -> float:
    """Jaccard where each element contributes ``weight(element)`` instead of 1. Identical sets -> 1.0."""
    sa, sb = set(a), set(b)
    union = sa | sb
    if not union:
        return 0.0
    num = sum(weight(c) for c in (sa & sb))
    den = sum(weight(c) for c in union)
    return (num / den) if den else 0.0


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


# Source/lineage table names too generic to anchor a *containment* match on their own: custom-SQL
# aliases, spreadsheet defaults, scratch/staging tables. A coverage match resting solely on these
# gets no superset boost (it falls back to plain Jaccard), so a lone generic table shared with a
# large consolidated model can never, by itself, manufacture an "already exists" verdict.
_GENERIC_TABLE_TOKENS = frozenset(
    {
        "data", "table", "table1", "sheet", "sheet1", "export", "extract", "query",
        "customsql", "dataset", "output", "results", "result", "temp", "tmp",
        "staging", "stage", "raw", "import", "source", "main", "default",
    }
)


def table_coverage(tab_tables: set, fab_tables: set) -> Tuple[float, list, bool]:
    """Containment of a Tableau datasource's source tables within a Fabric model's tables.

    Returns ``(coverage, shared_tables, distinctive)`` where ``coverage = |tab ∩ fab| / |tab|`` --
    the fraction of the *datasource's* upstream tables present in the model. Unlike Jaccard this is
    **not** diluted when one consolidated Fabric model unions many sources (the dominant migration
    pattern): a datasource whose every upstream table lives in the model scores full coverage even
    though the model is a strict superset. ``distinctive`` is False when the shared tables are only
    generic names, so the caller can withhold the superset boost in that case.
    """
    if not tab_tables:
        return 0.0, [], False
    inter = tab_tables & fab_tables
    coverage = len(inter) / len(tab_tables)
    distinctive = any(t not in _GENERIC_TABLE_TOKENS for t in inter)
    return coverage, sorted(inter), distinctive


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
    col_weight: Optional[Callable[[str], float]] = None,
) -> Dict[str, Any]:
    """Score one Tableau datasource against one Fabric model. Returns signals + weighted score.

    ``col_weight`` is an optional per-column weight function (see :func:`column_weight_fn`) used to
    down-weight ubiquitous column names in the overlap signal; when omitted, generic names are still
    down-weighted via the stoplist so a coincidental generic overlap cannot manufacture a match.
    """
    weights = weights or DEFAULT_WEIGHTS
    col_weight = col_weight or _default_col_weight

    # -- name (token-set Jaccard + exact short-circuit + capped fuzzy tail) -------------
    name_score = name_similarity(tableau_ds.get("name"), fabric_model.get("name"))

    # -- column overlap (ubiquitous names down-weighted so they can't carry a match) ---
    tab_fields = _field_name_map(tableau_ds.get("fields", []))
    fab_columns = _field_name_map(fabric_model.get("columns", []))
    column_score = _weighted_jaccard(tab_fields.keys(), fab_columns.keys(), col_weight)

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
    coverage = 0.0
    shared_tables: List[str] = []
    if source_comparable:
        strict_score = jaccard(tab_strict, fab_strict)
        loose_score = jaccard(tab_loose, fab_loose)
        # Table-only overlap is the durable cross-platform signal; weight it just under a loose match.
        table_score = jaccard(tab_tables, fab_tables)
        # Containment: a model that *covers* all of the datasource's upstream tables is a real match
        # even when it is a strict superset (one consolidated model serving many datasources) -- plain
        # Jaccard would bury that as a partial. The superset boost applies only when a distinctive
        # (non-generic) table is shared; otherwise we fall back to Jaccard so a lone generic table
        # cannot carry it. ``cover_term >= table_score`` always, so existing scores never drop.
        coverage, shared_tables, distinctive = table_coverage(tab_tables, fab_tables)
        cover_term = coverage if distinctive else table_score
        source_score: Optional[float] = max(
            strict_score, 0.85 * loose_score, 0.7 * cover_term
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
        "source_coverage": round(coverage, 4) if source_comparable else None,
        "shared_tables": shared_tables,
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


def _pct(x: Optional[float]) -> str:
    return f"{int(round((x or 0.0) * 100))}%"


def reason_for(match: Dict[str, Any]) -> str:
    """A short, deterministic explanation of a match's verdict, built from its best candidate.

    Pure text from the already-computed signals -- no re-scoring. Surfaces the drivers (exact/fuzzy
    name, column overlap, shared vs obscured source) and flags a contested model, so the ranked
    report carries a human-readable *why* next to each tier.
    """
    best = match.get("best_match")
    if not best:
        return "No Fabric model overlaps on name, columns, or source -- rebuild."
    sig = best.get("signals", {}) or {}
    name, col, src = sig.get("name") or 0.0, sig.get("column") or 0.0, sig.get("source")
    shared_tbls = best.get("shared_tables") or []
    parts: List[str] = []
    if name >= 0.999:
        parts.append("exact name")
    elif name >= 0.5:
        parts.append(f"close name ({_pct(name)})")
    if col >= 0.5:
        parts.append(f"{_pct(col)} weighted column overlap")
    elif col > 0:
        parts.append(f"weak column overlap ({_pct(col)})")
    if src is None:
        parts.append("source obscured (name/columns only)")
    elif src >= 0.5:
        if shared_tbls:
            shown = ", ".join(shared_tbls[:3])
            extra = f" +{len(shared_tbls) - 3} more" if len(shared_tbls) > 3 else ""
            parts.append(f"shared source tables ({shown}{extra})")
        else:
            parts.append("shared physical source")
    elif src > 0:
        parts.append("partial source overlap")
    if not parts:
        parts.append("little measurable overlap")
    if match.get("contested"):
        n = len(match.get("contested_with") or []) + 1
        parts.append(f"shared with {n} datasources")
    return "; ".join(parts) + f" -- {match.get('tier')}."


def logic_parity(
    tableau_fields: Sequence[Dict[str, Any]], fabric_measures: Sequence[str]
) -> Dict[str, Any]:
    """Compare a datasource's *calculated fields* against a model's *measures*, by name.

    Structural matching (columns / types / source) says nothing about whether the datasource's
    business **logic** was re-expressed in the Fabric model. This is a deliberately conservative,
    name-level signal -- it never claims expression equivalence (proving a Tableau calc equals a DAX
    measure is the migration translator's job). It exists to flag the dangerous case where the
    columns line up yet the calculations almost certainly did not come across, so a structural
    "already exists" is never mistaken for "safe to retire".

    Status:
      * ``none``       -- the datasource has no calculated fields; nothing to verify.
      * ``likely``     -- every calc name has a same-named measure (logic probably carried over).
      * ``partial``    -- some calc names line up, some do not.
      * ``unverified`` -- calcs exist but the model exposes no measures, or none line up by name --
                          the calculations almost certainly still need to be rebuilt.
    """
    calc_names: List[str] = []
    for f in tableau_fields or []:
        if isinstance(f, dict) and f.get("is_calculated"):
            tok = normalize_token(f.get("name"))
            if tok:
                calc_names.append(tok)
    calc_names = list(dict.fromkeys(calc_names))  # de-dupe, keep order
    measure_tokens = {t for t in (normalize_token(m) for m in (fabric_measures or [])) if t}

    calc_count = len(calc_names)
    measure_count = len(measure_tokens)
    if calc_count == 0:
        return {"status": "none", "tableau_calc_count": 0,
                "fabric_measure_count": measure_count, "matched": 0, "unmatched": []}
    matched = [c for c in calc_names if c in measure_tokens]
    unmatched = [c for c in calc_names if c not in measure_tokens]
    if measure_count == 0 or not matched:
        status = "unverified"
    elif not unmatched:
        status = "likely"
    else:
        status = "partial"
    return {"status": status, "tableau_calc_count": calc_count,
            "fabric_measure_count": measure_count, "matched": len(matched),
            "unmatched": unmatched[:20]}


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
    review_band: float = 0.08,
) -> Dict[str, Any]:
    """Compare every Tableau datasource against every Fabric model.

    Returns ``{"summary": {...}, "matches": [...]}`` where ``matches`` is sorted most-comparable
    first. Each match carries the best Fabric candidate, its tier, the four signal scores, and up to
    ``top_n`` runner-up candidates so the caller can show alternatives.
    """
    weights = weights or DEFAULT_WEIGHTS
    bands = bands or DEFAULT_BANDS
    fabric = list(fabric or [])

    # Estate document-frequency over normalised column names (Tableau datasources + Fabric models),
    # so the column-overlap signal can down-weight names that appear almost everywhere (low signal).
    doc_freq: Dict[str, int] = {}
    n_assets = 0
    for asset in list(tableau or []) + fabric:
        cols = {
            normalize_token(f.get("name"))
            for f in (asset.get("fields") or asset.get("columns") or [])
        }
        cols.discard("")
        for c in cols:
            doc_freq[c] = doc_freq.get(c, 0) + 1
        n_assets += 1
    col_weight = column_weight_fn(doc_freq, n_assets)

    matches: List[Dict[str, Any]] = []
    for ds in (tableau or []):
        scored: List[Dict[str, Any]] = []
        for fm in fabric:
            result = score_pair(ds, fm, weights, col_weight=col_weight)
            scored.append({
                "fabric_name": fm.get("name"),
                "workspace": fm.get("workspace"),
                "workspace_id": fm.get("workspaceId"),
                "fabric_id": fm.get("id"),
                "score": result["score"],
                "signals": result["signals"],
                "source_compared": result["source_compared"],
                "source_coverage": result["source_coverage"],
                "shared_tables": result["shared_tables"],
                "shared_column_count": result["shared_column_count"],
            })
        scored.sort(key=lambda c: c["score"], reverse=True)

        best = scored[0] if scored else None
        best_score = best["score"] if best else 0.0
        tier = band_for(best_score, bands) if best else "None"
        # Logic-parity: only meaningful when a real candidate exists (a rebuild's calcs are moot --
        # the whole datasource is being recreated anyway). Re-find the winning model to read its
        # measures; structural matching above never carried them.
        parity = None
        if best and best_score > 0:
            best_fm = next(
                (fm for fm in fabric
                 if fm.get("id") == best.get("fabric_id") and fm.get("name") == best.get("fabric_name")),
                None,
            )
            parity = logic_parity(ds.get("fields") or [], (best_fm or {}).get("measures") or [])
        matches.append({
            "tableau_name": ds.get("name"),
            "project": ds.get("project"),
            "tableau_luid": ds.get("luid"),
            "tier": tier,
            "score": best_score,
            "bucket": rollup_bucket(tier),
            "source_compared": bool(best and best.get("source_compared")),
            "usage": ds.get("usage"),
            "best_match": best if (best and best_score > 0) else None,
            "candidates": scored[:top_n],
            "logic_parity": parity,
        })

    matches.sort(key=lambda m: m["score"], reverse=True)

    by_tier = {t: 0 for t in TIER_ORDER}
    buckets = {"already_exists": 0, "partial": 0, "rebuild": 0}
    for m in matches:
        by_tier[m["tier"]] = by_tier.get(m["tier"], 0) + 1
        buckets[m["bucket"]] += 1

    # Business-logic-parity rollup (additive). ``review_needed`` is the headline risk: matches that
    # look already-in-Fabric / partial but whose calculated fields are not confirmed as measures.
    logic = {"none": 0, "likely": 0, "partial": 0, "unverified": 0, "review_needed": 0}
    for m in matches:
        lp = m.get("logic_parity")
        if not lp:
            continue
        st = lp.get("status", "none")
        logic[st] = logic.get(st, 0) + 1
        if m.get("bucket") in ("already_exists", "partial") and st in ("unverified", "partial"):
            logic["review_needed"] += 1

    summary = {
        "tableau_total": len(matches),
        "fabric_total": len(fabric),
        "by_tier": by_tier,
        "already_exist": buckets["already_exists"],
        "partial": buckets["partial"],
        "rebuild": buckets["rebuild"],
        "logic_parity": logic,
        "weights": dict(weights),
        "bands": [list(b) for b in bands],
    }
    result = {"summary": summary, "matches": matches}

    # Counting-correctness signals: collision detection, a one-to-one assignment view, and reverse
    # (Fabric -> Tableau) coverage. Additive -- adds matches[].contested/.assigned_* and the
    # summary.{distinct_fabric_matched, contested_models, assignment, fabric_coverage} keys without
    # touching the greedy per-datasource verdict above.
    annotate_assignment(result, fabric, bands=bands)

    # Per-match human-readable rationale (after assignment so it can mention contested models).
    for m in matches:
        m["reason"] = reason_for(m)

    # Tier-1 handoff: classify the not-confidently-matched datasources into an additive
    # adjudication packet for the LLM-optional "second matcher" (resources/llm-adjudication.md).
    # Imported lazily so the deterministic core has no import-time dependency on the router.
    try:  # pragma: no cover - trivial wiring
        try:
            from . import adjudicate as _adjudicate
        except ImportError:
            import adjudicate as _adjudicate
        result["adjudication"] = _adjudicate.build_adjudication(matches, tableau or [], fabric)
    except Exception:  # never let the optional tier break the deterministic verdict
        pass

    # Migration-priority signal: fuse each match's downstream usage (attached workbooks/sheets/
    # dashboards, gathered by tableau_inventory) with its comparison verdict so the report can rank
    # *which* rebuilds matter. Additive (adds matches[].priority / .migration_priority and the
    # summary rollups); lazily imported and never allowed to break the deterministic verdict.
    try:  # pragma: no cover - trivial wiring
        try:
            from . import priority as _priority
        except ImportError:
            import priority as _priority
        _priority.annotate(result)
    except Exception:
        pass

    # Artifact importance: fuse the connected-assets + telemetry layer (reach, views, certification)
    # into a per-datasource importance level + rollup so the deliverable can spotlight the assets a
    # migration must not get wrong. Additive; never alters tier/score/bucket/priority.
    try:  # pragma: no cover - trivial wiring
        try:
            from . import importance as _importance
        except ImportError:
            import importance as _importance
        _importance.annotate(result)
    except Exception:
        pass

    # Confidence synthesis: fuse the independent corroborators (score level, runner-up margin,
    # signal agreement, reciprocity) into one explainable confidence per verdict + a rollup. Additive
    # and idempotent -- re-run after --verify so the data check folds in. Never alters tier/score.
    try:  # pragma: no cover - trivial wiring
        try:
            from . import confidence as _confidence
        except ImportError:
            import confidence as _confidence
        _confidence.annotate_confidence(result)
    except Exception:
        pass

    # Borderline decision review: for the on-the-fence verdicts (Partial tier, score within
    # ``review_band`` of a bucket boundary, low-confidence, or structurally-matched-but-logic-
    # unverified) attach an exact structural diff -- Tableau-only / Fabric-only columns, type
    # mismatches, the source-table gap -- so a migration lead can adjudicate reuse-vs-rebuild from
    # evidence rather than a single score. Runs last so it can read the confidence level; needs both
    # inventories for the column/table lists the matches don't retain. Additive; never alters
    # tier/score/bucket.
    try:  # pragma: no cover - trivial wiring
        try:
            from . import borderline as _borderline
        except ImportError:
            import borderline as _borderline
        _borderline.annotate(result, tableau or [], fabric, band=review_band)
    except Exception:
        pass

    return result


# --------------------------------------------------------------------------------------
# Counting-correctness: collisions, one-to-one assignment, reverse coverage
# --------------------------------------------------------------------------------------
def _model_identity(candidate: Optional[Dict[str, Any]]):
    """Stable identity for a Fabric model: its id when known, else (name, workspace)."""
    if not candidate:
        return None
    fid = candidate.get("fabric_id") or candidate.get("id")
    if fid:
        return ("id", fid)
    return ("nw", candidate.get("fabric_name") or candidate.get("name"),
            candidate.get("workspace"))


def annotate_assignment(
    result: Dict[str, Any],
    fabric: Sequence[Dict[str, Any]],
    *,
    bands: Optional[List[Tuple[str, float]]] = None,
) -> Dict[str, Any]:
    """Add collision / one-to-one-assignment / reverse-coverage signals. Additive; mutates result.

    The greedy verdict lets several Tableau datasources claim the **same** Fabric model, which can
    inflate the headline ``already_exist`` count. This annotates:

      * ``matches[].contested`` / ``contested_with`` -- this match's best Fabric model is also the
        best match of one or more other datasources.
      * ``matches[].assigned_match`` / ``assigned_tier`` -- a stable greedy **one-to-one** assignment
        (each Fabric model backs at most one datasource), so the estate can also be sized without
        double-counting a shared model.
      * ``summary.distinct_fabric_matched`` / ``contested_models`` / ``assignment`` /
        ``fabric_coverage`` (which Fabric models nothing in Tableau maps to -- net-new in Fabric).
    """
    bands = bands or DEFAULT_BANDS
    matches = result.get("matches", [])
    summary = result.setdefault("summary", {})

    # -- 1) collision detection over each datasource's best Fabric model --------------------
    claims: Dict[Any, List[Dict[str, Any]]] = {}
    label_for: Dict[Any, Dict[str, Any]] = {}
    for m in matches:
        m.setdefault("contested", False)
        m.setdefault("contested_with", [])
        best = m.get("best_match")
        if not best or m.get("bucket") == "rebuild":
            continue
        ident = _model_identity(best)
        claims.setdefault(ident, []).append(m)
        label_for[ident] = best
    for ident, claimants in claims.items():
        shared = len(claimants) > 1
        for m in claimants:
            m["contested"] = shared
            m["contested_with"] = [x["tableau_name"] for x in claimants if x is not m]

    distinct_already = {
        _model_identity(m["best_match"])
        for m in matches
        if m.get("bucket") == "already_exists" and m.get("best_match")
    }
    summary["distinct_fabric_matched"] = len(distinct_already)

    contested_models = [
        {
            "fabric_name": label_for[ident].get("fabric_name"),
            "workspace": label_for[ident].get("workspace"),
            "claimed_by": [x["tableau_name"] for x in claimants],
        }
        for ident, claimants in claims.items()
        if len(claimants) > 1
    ]
    contested_models.sort(key=lambda d: (-len(d["claimed_by"]), d["fabric_name"] or ""))
    summary["contested_models"] = contested_models

    # -- 2) greedy one-to-one assignment (each model claimed once, highest score wins) ------
    pairs: List[Tuple[float, int, Dict[str, Any]]] = []
    for mi, m in enumerate(matches):
        for cand in m.get("candidates", []) or []:
            sc = cand.get("score") or 0.0
            if sc > 0:
                pairs.append((sc, mi, cand))
    pairs.sort(key=lambda p: p[0], reverse=True)
    assigned: Dict[int, Dict[str, Any]] = {}
    used_models: set = set()
    for sc, mi, cand in pairs:
        if mi in assigned:
            continue
        ident = _model_identity(cand)
        if ident in used_models:
            continue
        assigned[mi] = cand
        used_models.add(ident)

    assign_by_tier = {t: 0 for t in TIER_ORDER}
    assign_buckets = {"already_exists": 0, "partial": 0, "rebuild": 0}
    for mi, m in enumerate(matches):
        cand = assigned.get(mi)
        if cand:
            tier = band_for(cand.get("score") or 0.0, bands)
            m["assigned_match"] = cand
            m["assigned_tier"] = tier
        else:
            m["assigned_match"] = None
            m["assigned_tier"] = "None"
        assign_by_tier[m["assigned_tier"]] = assign_by_tier.get(m["assigned_tier"], 0) + 1
        assign_buckets[rollup_bucket(m["assigned_tier"])] += 1
    summary["assignment"] = {
        "by_tier": assign_by_tier,
        "already_exist": assign_buckets["already_exists"],
        "partial": assign_buckets["partial"],
        "rebuild": assign_buckets["rebuild"],
    }

    # -- 3) reverse coverage: Fabric models with no Tableau counterpart ---------------------
    matched_models = {
        _model_identity(m["best_match"])
        for m in matches
        if m.get("bucket") in ("already_exists", "partial") and m.get("best_match")
    }
    seen: set = set()
    matched_count = 0
    unmatched: List[Dict[str, Any]] = []
    for fm in fabric or []:
        ident = _model_identity(
            {"fabric_id": fm.get("id"), "fabric_name": fm.get("name"), "workspace": fm.get("workspace")}
        )
        if ident in seen:
            continue
        seen.add(ident)
        if ident in matched_models:
            matched_count += 1
        else:
            unmatched.append({"fabric_name": fm.get("name"), "workspace": fm.get("workspace")})
    unmatched.sort(key=lambda d: ((d["workspace"] or ""), (d["fabric_name"] or "")))
    summary["fabric_coverage"] = {
        "fabric_total": len(seen),
        "matched_models": matched_count,
        "unmatched_models": len(unmatched),
        "unmatched_model_names": unmatched,
    }
    return result


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------
def _action_for(tier: str) -> str:
    return _RECOMMENDED_ACTION.get(tier, "")


def _cell(value: Any) -> str:
    """Render a value safe for a Markdown table cell.

    A datasource / model / workspace name is attacker-influenced free text that can contain a
    pipe or a newline; dropped verbatim into a ``| ... |`` row it would silently break the table
    (extra columns / a split row). Neutralise those so the report stays well-formed -- names
    without special characters are returned unchanged, so existing output is untouched.
    """
    s = "" if value is None else str(value)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _render_counting_rollup(result: Dict[str, Any], lines: List[str]) -> None:
    """One-line counting-correctness rollup: distinct matched models + the 1:1 assignment view."""
    s = result.get("summary", {})
    distinct = s.get("distinct_fabric_matched")
    assign = s.get("assignment") or {}
    cov = s.get("fabric_coverage") or {}
    bits: List[str] = []
    if distinct is not None:
        bits.append(
            f"**{distinct}** distinct Fabric model(s) back the {s.get('already_exist', 0)} "
            "already-in-Fabric datasource(s)"
        )
    if assign:
        bits.append(
            "under a 1:1 assignment: already-exist="
            f"{assign.get('already_exist', 0)}, partial={assign.get('partial', 0)}, "
            f"rebuild={assign.get('rebuild', 0)}"
        )
    if cov:
        bits.append(
            f"Fabric coverage: {cov.get('matched_models', 0)}/{cov.get('fabric_total', 0)} "
            f"model(s) matched, {cov.get('unmatched_models', 0)} with no Tableau counterpart"
        )
    if not bits:
        return
    lines.append("- " + "  \n- ".join(bits))
    lines.append("")


def _render_contested(result: Dict[str, Any], lines: List[str]) -> None:
    """List Fabric models claimed as the best match by more than one Tableau datasource."""
    contested = (result.get("summary", {}) or {}).get("contested_models") or []
    if not contested:
        return
    lines.append("## Contested matches (one model, several datasources)")
    lines.append("")
    lines.append(
        "These Fabric models are the **best match for more than one** Tableau datasource, so the "
        "headline already-in-Fabric count can over-count a single reused model. Confirm whether each "
        "really covers every datasource below, or only one (use the 1:1 assignment view as a cross-check)."
    )
    lines.append("")
    lines.append("| Fabric model | Workspace | Claimed by |")
    lines.append("|---|---|---|")
    for c in contested:
        claimed = ", ".join(c.get("claimed_by") or [])
        lines.append(f"| {c.get('fabric_name') or ''} | {c.get('workspace') or ''} | {claimed} |")
    lines.append("")


def _render_coverage(result: Dict[str, Any], lines: List[str]) -> None:
    """List Fabric models that nothing in Tableau maps to -- net-new content already in Fabric."""
    cov = (result.get("summary", {}) or {}).get("fabric_coverage") or {}
    unmatched = cov.get("unmatched_model_names") or []
    if not unmatched:
        return
    lines.append("## Fabric models with no Tableau counterpart")
    lines.append("")
    lines.append(
        f"{cov.get('unmatched_models', len(unmatched))} of {cov.get('fabric_total', 0)} Fabric "
        "semantic model(s) did not match any Tableau datasource -- they are net-new in Fabric (or "
        "built from sources Tableau does not publish). Nothing to migrate for these; listed for "
        "completeness."
    )
    lines.append("")
    lines.append("| Fabric model | Workspace |")
    lines.append("|---|---|")
    for u in unmatched:
        lines.append(f"| {u.get('fabric_name') or ''} | {u.get('workspace') or ''} |")
    lines.append("")


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
    _render_counting_rollup(result, lines)
    _render_confidence_rollup(result, lines)
    _render_borderline_rollup(result, lines)
    _render_priority_rollup(result, lines)
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
            f"| {_cell(m['tableau_name'])} | {_cell(m.get('project') or '')} | {_cell(fab)} | {_cell(ws)} | "
            f"{m['tier']} | {m['score']:.2f} | {sig_str} |"
        )
    lines.append("")
    lines.append(
        "_`src = n/a` means the underlying physical source was obscured on one side "
        "(composite/DirectQuery model, unresolved connector, or a referenced datasource); "
        "the match relies on name + columns + types instead._"
    )
    lines.append("")
    _render_contested(result, lines)
    _render_coverage(result, lines)
    _render_verification(result, lines)
    _render_logic_parity(result, lines)
    _render_confidence_detail(result, lines)
    _render_borderline_detail(result, lines)
    lines.append("## Recommended actions")
    lines.append("")
    reasons = {m["tableau_name"]: m.get("reason") for m in result["matches"]}
    for tier in TIER_ORDER:
        names = [m["tableau_name"] for m in result["matches"] if m["tier"] == tier]
        if not names:
            continue
        lines.append(f"### {tier} ({len(names)})")
        lines.append("")
        lines.append(_action_for(tier))
        lines.append("")
        for n in names:
            why = reasons.get(n)
            lines.append(f"- {n}" + (f" -- _{why}_" if why else ""))
        lines.append("")
    _render_priority_worklist(result, lines)
    _render_importance(result, lines)
    _render_adjudication(result, lines)
    return "\n".join(lines).rstrip() + "\n"


def _render_importance(result: Dict[str, Any], lines: List[str]) -> None:
    """Artifact importance + connected assets (additive). Spotlights the highest-value datasources --
    the ones a migration must not get wrong -- with their real usage and the assets that depend on
    them. Rendered only when telemetry produced at least one non-Unknown importance level."""
    summary = result.get("summary") or {}
    imp = summary.get("importance") or {}
    by_level = imp.get("by_level") or {}
    rated = sum(v for k, v in by_level.items() if k != "Unknown")
    if rated <= 0:
        return
    matches = result.get("matches") or []
    ranked = sorted(
        [m for m in matches if (m.get("importance") or {}).get("level") not in (None, "Unknown")],
        key=lambda m: (m.get("importance") or {}).get("score") or 0.0, reverse=True,
    )
    lines.append("## Artifact importance & connected assets")
    lines.append("")
    headline = (
        f"**{imp.get('critical', 0)} Critical** and **{imp.get('high', 0)} High** importance "
        f"datasource(s)."
    )
    extra = []
    if imp.get("total_views") is not None:
        extra.append(f"{imp['total_views']} total view(s) across the estate")
    if imp.get("certified_datasources"):
        extra.append(f"{imp['certified_datasources']} certified")
    if imp.get("datasources_with_quality_warning"):
        extra.append(f"{imp['datasources_with_quality_warning']} with an active data-quality warning")
    if extra:
        headline += " " + "; ".join(extra) + "."
    lines.append(headline)
    lines.append("")
    lines.append(
        "Importance fuses **reach** (dependent workbooks + dashboards), **consumption** (views) and "
        "**endorsement** (certified). It ranks which assets carry the most business value so the "
        "migration protects them first — independent of whether they already exist in Fabric."
    )
    lines.append("")
    lines.append("| Datasource | Verdict | Importance | Views | Workbooks | Dashboards | Last refreshed | Top connected assets |")
    lines.append("|---|---|---|---:|---:|---:|---|---|")
    for m in ranked[:15]:
        u = m.get("usage") or {}
        ci = m.get("importance") or {}
        ca = u.get("connected_assets") or {}
        names = [w.get("name") for w in (ca.get("workbooks") or []) if w.get("name")][:3]
        names += [d.get("name") for d in (ca.get("dashboards") or []) if d.get("name")][:2]
        assets = ", ".join(_cell(n) for n in names) if names else ""
        refreshed = str(u.get("extract_last_refresh") or "")[:10]
        lines.append(
            f"| {_cell(m['tableau_name'])} | {m['tier']} | {ci.get('level', '')} | "
            f"{_num_cell(u.get('view_count'))} | {_num_cell(u.get('workbook_count'))} | "
            f"{_num_cell(u.get('dashboard_count'))} | {refreshed} | {assets} |"
        )
    lines.append("")
    lines.append(
        "_Views / certification / refresh times are best-effort telemetry from the Tableau Metadata "
        "API + view statistics; blanks mean that signal was unavailable for the datasource._"
    )
    lines.append("")


def _num_cell(v: Any) -> str:
    """Render a numeric usage value for a table cell; blank when unknown."""
    if isinstance(v, bool) or v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(int(v))
    return ""


# Default cap on how many on-the-fence datasources get a full diff block in the Markdown report; the
# rest are summarised in the rollup, and the JSON / export carry the complete set. Overridable via the
# ``summary.borderline.render_limit`` key (the CLI ``--review-top-n`` flag sets it).
_BORDERLINE_DETAIL_CAP = 25

_HINT_LABEL = {
    "lean_reuse": "lean reuse",
    "lean_rebuild": "lean rebuild",
    "reuse_with_logic_review": "reuse, but review the calculations",
}
_REASON_LABEL = {
    "partial_tier": "partial tier",
    "near_reuse_boundary": "near the reuse boundary",
    "near_rebuild_boundary": "near the rebuild boundary",
    "low_confidence": "low confidence",
    "logic_unverified": "business logic unverified",
}


def _render_borderline_rollup(result: Dict[str, Any], lines: List[str]) -> None:
    """On-the-fence headline (additive). Names the count of borderline datasources near the top so a
    stakeholder sees -- without scrolling -- how many reuse-vs-rebuild calls still need a human. The
    per-datasource difference detail follows lower in the report. Rendered only when there is >=1."""
    summary = result.get("summary") or {}
    bl = summary.get("borderline") or {}
    count = bl.get("count", 0)
    if not count:
        return
    hints = bl.get("hints") or {}
    lines.append("## On-the-fence datasources")
    lines.append("")
    lines.append(
        f"**{count} datasource(s) are borderline** -- not clearly already in Fabric, and not clearly "
        "a rebuild. For these the reuse-vs-rebuild call is a judgment, so each is detailed below with "
        "exactly how its best Fabric candidate differs (missing columns, extra columns, type "
        "mismatches, source-table gap)."
    )
    if hints:
        leans = ", ".join(
            f"{_HINT_LABEL.get(h, h)} {n}" for h, n in sorted(hints.items())
        )
        lines.append("")
        lines.append(f"Advisory lean (never overrides the verdict): {leans}.")
    lines.append("")


def _render_borderline_detail(result: Dict[str, Any], lines: List[str]) -> None:
    """Per-datasource difference detail for the on-the-fence set (additive). For each borderline
    datasource: the column breakdown (shared / Tableau-only / Fabric-only / type mismatches), the
    source-table gap, the business-logic caveat, and the advisory reuse-vs-rebuild lean."""
    summary = result.get("summary") or {}
    bl = summary.get("borderline") or {}
    if not bl.get("count"):
        return
    items = [m for m in (result.get("matches") or []) if m.get("borderline")]
    if not items:
        return
    limit = bl.get("render_limit")
    cap = _BORDERLINE_DETAIL_CAP if not isinstance(limit, int) or limit <= 0 else limit

    lines.append("## Borderline decision detail")
    lines.append("")
    lines.append(
        "Each on-the-fence datasource and exactly how its best Fabric candidate differs, so you can "
        "decide reuse vs rebuild from evidence. The lean is advisory and never overrides the verdict."
    )
    lines.append("")
    for m in items[:cap]:
        b = m.get("borderline") or {}
        cols = b.get("columns") or {}
        src = b.get("source") or {}
        hint = _HINT_LABEL.get(b.get("recommendation_hint"), b.get("recommendation_hint") or "")
        lines.append(
            f"### {_cell(m.get('tableau_name'))}  (score {float(m.get('score') or 0.0):.2f}, "
            f"{m.get('tier')} -> {hint})"
        )
        lines.append("")
        why = ", ".join(_REASON_LABEL.get(r, r) for r in (b.get("reasons") or []))
        best = b.get("best_match")
        if best:
            ws = b.get("workspace") or ""
            where = f" in _{_cell(ws)}_" if ws else ""
            lines.append(f"Best Fabric candidate: **{_cell(best)}**{where}. Flagged: {why}.")
        else:
            lines.append(f"No Fabric candidate clears the bar. Flagged: {why}.")
        lines.append("")
        lines.append("| Columns | Count | Examples |")
        lines.append("|---|---:|---|")
        lines.append(
            f"| Shared | {cols.get('shared_count', 0)} | {_examples(cols.get('shared'))} |"
        )
        lines.append(
            f"| In Tableau only (missing from Fabric) | {cols.get('tableau_only_count', 0)} | "
            f"{_examples(cols.get('tableau_only'))} |"
        )
        lines.append(
            f"| In Fabric only (extra) | {cols.get('fabric_only_count', 0)} | "
            f"{_examples(cols.get('fabric_only'))} |"
        )
        tm = cols.get("type_mismatches") or []
        tm_examples = ", ".join(
            f"{_cell(r.get('column'))} (Tableau {_cell(r.get('tableau_type') or '?')} "
            f"vs Fabric {_cell(r.get('fabric_type') or '?')})"
            for r in tm[:3]
        )
        lines.append(
            f"| Type mismatches | {cols.get('type_mismatch_count', 0)} | {tm_examples} |"
        )
        lines.append("")
        if src.get("compared"):
            cov = src.get("coverage")
            cov_str = "n/a" if cov is None else f"{int(round(cov * 100))}%"
            parts = [f"coverage {cov_str}"]
            if src.get("shared_tables"):
                parts.append("shared: " + _examples(src.get("shared_tables")))
            if src.get("tableau_only_tables"):
                parts.append("in Tableau only: " + _examples(src.get("tableau_only_tables")))
            if src.get("fabric_only_tables"):
                parts.append("in Fabric only: " + _examples(src.get("fabric_only_tables")))
            lines.append("Source tables: " + "; ".join(parts) + ".")
            lines.append("")
        lp = b.get("logic_parity") or {}
        if lp.get("status") in ("unverified", "partial"):
            unmatched = lp.get("unmatched") or []
            ex = (" (" + _examples(unmatched) + ")") if unmatched else ""
            lines.append(
                f"Business logic: {lp.get('tableau_calc_count', 0)} calculated field(s), "
                f"{lp.get('matched', 0)} confirmed as Fabric measures{ex} -- 'already exists' is "
                "not 'safe to retire'."
            )
            lines.append("")
    remaining = len(items) - cap
    if remaining > 0:
        lines.append(
            f"_...and {remaining} more borderline datasource(s) -- see the JSON output or the "
            "Borderline export sheet for the full set._"
        )
        lines.append("")


def _examples(names: Optional[Sequence[Any]], limit: int = 6) -> str:
    """Render up to ``limit`` example names for a cell, comma-joined and table-safe."""
    if not names:
        return ""
    shown = [_cell(n) for n in list(names)[:limit]]
    suffix = ", ..." if len(names) > limit else ""
    return ", ".join(shown) + suffix



def _render_confidence_rollup(result: Dict[str, Any], lines: List[str]) -> None:
    """Confidence headline (additive). One-line read on how trustworthy the verdicts are, placed near
    the top so a stakeholder sees it without scrolling. Rendered only when confidence was synthesised."""
    summary = result.get("summary") or {}
    conf = summary.get("confidence") or {}
    if not conf:
        return
    high = conf.get("high", 0)
    medium = conf.get("medium", 0)
    low = conf.get("low", 0)
    total = high + medium + low
    if total <= 0:
        return
    review = conf.get("low_confidence_review", 0)
    lines.append("## Verdict confidence")
    lines.append("")
    lines.append(
        f"**{high} of {total} verdict(s) are high-confidence** "
        f"(medium {medium}, low {low}). High-confidence means independent signals -- name, columns, "
        "physical source, mutual-best agreement and (when run) the empirical data check -- corroborate "
        "the same conclusion."
    )
    if review > 0:
        lines.append("")
        lines.append(
            f"> **{review} verdict(s) are low-confidence and warrant a human look** before you act on "
            "them -- see _Lowest-confidence verdicts_ below."
        )
    lines.append("")


def _render_confidence_detail(result: Dict[str, Any], lines: List[str]) -> None:
    """Actionable low-confidence callout (additive). Lists the verdicts the customer should eyeball,
    with the drivers that support them and the cautions that weaken them. Rendered only when there is
    at least one low-confidence verdict."""
    matches = result.get("matches") or []
    flagged = [(m, m.get("confidence") or {}) for m in matches
               if (m.get("confidence") or {}).get("level") == "Low"]
    if not flagged:
        return
    # Weakest margin first -- the closest calls are the ones a reviewer should open first.
    flagged.sort(key=lambda mc: mc[1].get("margin") or 0.0)
    lines.append("## Lowest-confidence verdicts (review these first)")
    lines.append("")
    lines.append(
        "These verdicts rest on weaker or conflicting evidence -- a near-tie between Fabric models, a "
        "single supporting signal, a contested model, a borderline score, or an empirical mismatch. The "
        "deterministic verdict still stands; this is where a human reviewer adds the most value."
    )
    lines.append("")
    lines.append("| Tableau datasource | Verdict | Best Fabric match | Why it's uncertain |")
    lines.append("|---|---|---|---|")
    for m, conf in flagged:
        best = m.get("best_match")
        fab = best.get("fabric_name") if best else "_(none)_"
        cautions = conf.get("cautions") or []
        drivers = conf.get("drivers") or []
        why = "; ".join(cautions) if cautions else ("; ".join(drivers) if drivers else "limited evidence")
        lines.append(
            f"| {_cell(m['tableau_name'])} | {m['tier']} | {_cell(fab)} | {_cell(why)} |"
        )
    lines.append("")


def _render_logic_parity(result: Dict[str, Any], lines: List[str]) -> None:
    """Business-logic-parity section (additive). Rendered only when at least one matched datasource
    carries calculated fields; otherwise the report is unchanged."""
    summary = result.get("summary") or {}
    lp = summary.get("logic_parity") or {}
    with_calcs = lp.get("likely", 0) + lp.get("partial", 0) + lp.get("unverified", 0)
    if with_calcs <= 0:
        return
    lines.append("## Business-logic parity (calculated fields -> measures)")
    lines.append("")
    lines.append(
        "Structural matching compares columns, types and sources -- it does **not** prove the "
        "datasource's *calculated fields* were re-expressed as DAX measures. This name-level check "
        "flags where they likely were not, so a structural match is not mistaken for true "
        "equivalence. (It does not compare formulas -- that is the migration translator's job.)")
    lines.append("")
    review = lp.get("review_needed", 0)
    if review:
        lines.append(
            f"> **{review} match(es)** look already-in-Fabric or partial **but their business logic "
            f"is unverified** -- confirm the calculated fields exist as measures before retiring the "
            f"Tableau datasource.")
        lines.append("")
    lines.append("| Logic parity | Count | Meaning |")
    lines.append("|---|---:|---|")
    lines.append(
        f"| Likely carried over | {lp.get('likely', 0)} | every calc has a same-named measure |")
    lines.append(
        f"| Partial | {lp.get('partial', 0)} | some calcs line up, some do not |")
    lines.append(
        f"| Unverified | {lp.get('unverified', 0)} | calcs present but no matching measures -- likely rebuild |")
    lines.append("")
    rows = [
        (m, m.get("logic_parity") or {})
        for m in result.get("matches", [])
        if (m.get("logic_parity") or {}).get("status") in ("partial", "unverified")
    ]
    if rows:
        lines.append("| Tableau datasource | Calc fields | Matched as measures | Parity |")
        lines.append("|---|---:|---:|---|")
        for m, d in rows[:30]:
            n = d.get("tableau_calc_count", 0)
            lines.append(
                f"| {_cell(m['tableau_name'])} | {n} | {d.get('matched', 0)}/{n} | {d.get('status')} |")
        lines.append("")


# Empirical-verification rendering (Tier-2, additive). Present only when --verify ran; otherwise
# every line below is skipped and the deterministic report is byte-for-byte unchanged.
_VERIFICATION_VERDICT_LABEL = {
    "verified": "Verified",
    "compatible": "Compatible",
    "mismatch": "MISMATCH",
    "inconclusive": "Inconclusive",
}


def _render_verification(result: Dict[str, Any], lines: List[str]) -> None:
    v = (result.get("summary") or {}).get("verification")
    if not v:
        return
    lines.append("## Empirical verification")
    lines.append("")
    if not v.get("enabled"):
        reason = v.get("reason") or "not run"
        lines.append(f"_Requested but skipped: {_cell(str(reason))}._")
        lines.append("")
        return
    lines.append(
        "Read-only aggregate probes were run on **both** sides over each pair's shared columns and "
        "compared on their **overlapping data window**, so a Fabric superset (more history) still "
        "verifies rather than looking like a mismatch."
    )
    lines.append("")
    lines.append(
        f"Attempted {v.get('attempted', 0)} -- "
        f"verified={v.get('verified', 0)}, compatible={v.get('compatible', 0)}, "
        f"mismatch={v.get('mismatch', 0)}, inconclusive={v.get('inconclusive', 0)} "
        f"({v.get('probes_run', 0)} probe(s) run)."
    )
    lines.append("")
    no_data = v.get("fabric_no_data") or 0
    unreadable = v.get("fabric_unreadable") or 0
    if no_data or unreadable:
        total = no_data + unreadable
        parts = []
        if no_data:
            parts.append(f"{no_data} hold no rows (not yet refreshed)")
        if unreadable:
            parts.append(f"{unreadable} could not be queried (unrefreshed, capacity paused, or "
                         "source connection not configured)")
        lines.append(
            f"> **{total} model(s) could not be verified because Fabric returned no data** -- "
            + "; ".join(parts) + ". This is a data-state condition, **not a mismatch**: the "
            "schema/lineage match still stands. Resolve the model(s) in Fabric (refresh / repoint "
            "the source / resume the capacity), then re-run `--verify` to confirm the data agrees."
        )
        lines.append("")
    rows = [m for m in result.get("matches", []) if m.get("verification")]
    if rows:
        lines.append("| Tableau datasource | Fabric match | Verdict | Detail |")
        lines.append("|---|---|---|---|")
        for m in rows:
            mv = m.get("verification") or {}
            best = m.get("best_match") or {}
            verdict = _VERIFICATION_VERDICT_LABEL.get(mv.get("verdict"), mv.get("verdict") or "?")
            note = m.get("verification_note") or ""
            lines.append(
                f"| {_cell(m.get('tableau_name') or '')} | {_cell(best.get('fabric_name') or '')} | "
                f"{verdict} | {_cell(note)} |"
            )
        lines.append("")
    if v.get("mismatch"):
        lines.append(
            "> A **MISMATCH** means the data disagreed on the shared window (or the ranges were "
            "disjoint). It is advisory -- the deterministic tier is unchanged -- but it flags a "
            "pair a human should confirm before reusing the Fabric model."
        )
        lines.append("")


# Migration-priority rendering. Both helpers are guarded -- when usage was not gathered
# (``--usage off`` or a tenant with no Catalog) everything is Unknown/Unprioritized and the
# sections degrade quietly so the deterministic report is unchanged.
_MIGRATION_PRIORITY_ORDER = [
    "P1 - migrate first",
    "P2 - migrate",
    "P3 - deprioritize",
    "P4 - retire candidate",
    "Reuse (already in Fabric)",
    "Unprioritized",
]
_USAGE_ORDER = ["High", "Medium", "Low", "Unused", "Unknown"]


def _has_usage(result: Dict[str, Any]) -> bool:
    return any(
        (m.get("usage") or {}).get("workbook_count") is not None
        for m in result.get("matches", [])
    )


def _render_priority_rollup(result: Dict[str, Any], lines: List[str]) -> None:
    s = result.get("summary", {})
    by_mig = s.get("by_migration_priority")
    if not by_mig or not _has_usage(result):
        return
    shown = [(p, by_mig.get(p, 0)) for p in _MIGRATION_PRIORITY_ORDER if by_mig.get(p, 0)]
    if not shown:
        return
    lines.append("By migration priority: " + ", ".join(f"{p}={c}" for p, c in shown))
    lines.append("")


def _render_priority_worklist(result: Dict[str, Any], lines: List[str]) -> None:
    """Rank the rebuild/partial datasources by downstream impact -- what to migrate, and in what order."""
    if not _has_usage(result):
        return
    work = [
        m for m in result.get("matches", [])
        if m.get("bucket") in ("rebuild", "partial")
    ]
    if not work:
        return
    order = {p: i for i, p in enumerate(_MIGRATION_PRIORITY_ORDER)}
    work.sort(key=lambda m: (order.get(m.get("migration_priority"), 99), -(m.get("score") or 0.0)))
    th = result.get("summary", {}).get("usage_thresholds", {})
    lines.append("## Migration priority (what to rebuild first)")
    lines.append("")
    lines.append(
        "Ranks the datasources that need work by **downstream impact** -- how many workbooks "
        "(and the sheets / dashboards built on them) depend on each. Busy datasources rebuild "
        "first; a datasource with **0-1 attached workbook** is a deprioritize / retire candidate "
        "even if it needs a full rebuild."
    )
    if th:
        lines.append("")
        lines.append(
            f"_Usage bands: High >= {th.get('high')} workbooks, Medium >= {th.get('medium')}, "
            "Low = 1, Unused = 0, Unknown = not catalogued._"
        )
    lines.append("")
    lines.append("| Priority | Tableau datasource | Outcome | Workbooks | Usage | Score |")
    lines.append("|---|---|---|---:|---|---:|")
    bucket_label = {"rebuild": "Needs rebuild", "partial": "Partial overlap"}
    for m in work:
        usage = m.get("usage") or {}
        wc = usage.get("workbook_count")
        wc_s = "?" if wc is None else str(wc)
        lines.append(
            f"| {m.get('migration_priority', '')} | {m.get('tableau_name')} | "
            f"{bucket_label.get(m.get('bucket'), m.get('bucket'))} | {wc_s} | "
            f"{m.get('priority', '')} | {m.get('score', 0.0):.2f} |"
        )
    lines.append("")


# Friendly one-line labels for each uncertainty category in the adjudication queue.
_CATEGORY_LABEL = {
    "near_tie": "Near tie -- two close candidates",
    "renamed_columns_suspected": "Renamed columns / asset suspected",
    "obscured_source": "Obscured source -- confirm match",
    "borderline_band": "Borderline band -- likely under-scored",
    "likely_rebuild": "Likely rebuild -- final sanity check",
}


def _render_adjudication(result: Dict[str, Any], lines: List[str]) -> None:
    """Append the agent adjudication queue and (if applied) the post-review rollup. Additive."""
    adj = result.get("adjudication") or {}
    requests = adj.get("requests") or []
    if requests:
        lines.append("## Agent adjudication queue (LLM-optional review)")
        lines.append("")
        lines.append(
            f"The deterministic matcher is confident about "
            f"{adj.get('summary', {}).get('auto_confident', 0)} datasource(s). The "
            f"{len(requests)} below sit in a band where a **semantic** judgement can catch a match "
            "(or false match) that structure alone misses -- renamed columns, a renamed asset, a "
            "lakehouse mirror, or coincidental generic column names. Hand these to an agent per "
            "`resources/llm-adjudication.md`; the deterministic verdict stands until then."
        )
        lines.append("")
        lines.append("| Tableau datasource | Det. tier | Det. score | Why flagged | Top candidate |")
        lines.append("|---|---|---:|---|---|")
        for r in requests:
            det = r.get("deterministic", {})
            cands = r.get("candidates") or []
            top = cands[0]["fabric_name"] if cands else "_(none)_"
            why = _CATEGORY_LABEL.get(r.get("category"), r.get("category") or "")
            score = det.get("score")
            score_s = f"{score:.2f}" if isinstance(score, (int, float)) else ""
            lines.append(
                f"| {r.get('tableau_name')} | {det.get('tier')} | {score_s} | {why} | {top} |"
            )
        lines.append("")

    # Post-apply rollup: present only after apply_adjudication() folded agent verdicts in.
    adj_summary = result.get("adjudicated_summary")
    reviewed = [m for m in result.get("matches", []) if m.get("agent_review")]
    if adj_summary and reviewed:
        s = result["summary"]
        lines.append("## After semantic review (agent-adjudicated)")
        lines.append("")
        lines.append("| Outcome | Deterministic | After review | Delta |")
        lines.append("|---|---:|---:|---:|")
        for key, label in (
            ("already_exist", "Already in Fabric"),
            ("partial", "Partial overlap"),
            ("rebuild", "Needs rebuild"),
        ):
            d = adj_summary.get("delta", {}).get(key, 0)
            delta = f"+{d}" if d > 0 else str(d)
            lines.append(
                f"| {label} | {s.get(key, 0)} | {adj_summary.get(key, 0)} | {delta} |"
            )
        lines.append("")
        lines.append(
            "_Advisory only -- the deterministic tier/score above are unchanged; these are the "
            "agent's semantic verdicts._"
        )
        lines.append("")
        for m in reviewed:
            ar = m["agent_review"]
            conf = f" ({ar.get('confidence')})" if ar.get("confidence") else ""
            rationale = ar.get("rationale") or ""
            lines.append(
                f"- **{m.get('tableau_name')}** -> {ar.get('verdict')}{conf}"
                + (f" -- {rationale}" if rationale else "")
            )
        lines.append("")
