#!/usr/bin/env python3
"""Empirical verification (Tier-2): probe both sides and check the *data* lines up.

The deterministic engine (``compare.py``) decides "same dataset?" from structure -- name, columns,
types, lineage. That is strong, but it can be fooled by a same-shape / different-data pair and it
never looks at the values. This module adds an opt-in, advisory layer that, for a bounded set of
confident/partial matches, runs a few read-only **aggregate probes** on both sides and checks the
answers line up -- *without* being fooled by volume differences.

Why naive equality is wrong
---------------------------
A Fabric model holding 2019-2026 and a Tableau datasource holding 2021-2026 are the **same** source
(Fabric is simply a superset), yet ``SUM(Sales)`` / ``COUNT`` / ``DISTINCTCOUNT`` would legitimately
differ. Treating that as a "mismatch" would tell a migration team to rebuild something they should
reuse -- the opposite of the skill's purpose. Unbounded totals are **not** invariant under
subset/superset, so they cannot be compared as equality.

What we do instead: overlap-window agreement
--------------------------------------------
1. **Establish each side's range** -- ``MIN`` / ``MAX`` on a shared date (preferred) or numeric key
   column. From those, compute the **common overlap window** and classify the relationship
   (*equal / subset / superset / partial / disjoint*).
2. **Compare equality probes only inside that overlap** -- windowed ``SUM`` / ``DISTINCTCOUNT`` on
   both sides. On the shared slice the same dataset agrees within tolerance regardless of how much
   extra history either side carries. ``MIN`` / ``MAX`` only *establish* the window; they are never
   pass/fail equality checks.
3. **Verdict semantics** -- "one side is a superset" is a **PASS** ("verified; Fabric is a superset
   -- agrees on the 2021-2026 overlap"), not a mismatch. Only a disagreement *within the overlap*
   (or a fully **disjoint** range) is a real ``mismatch``.
4. **No shared time/key column?** Fall back to a conservative *containment* read of the raw totals:
   consistent with one-side-superset -> ``compatible``; exactly equal -> ``verified``; otherwise
   ``inconclusive``. Containment mode never emits a hard ``mismatch`` from magnitude alone.

Design
------
* **Pure core, injected transport.** The verdict logic takes two *probe callables* (which accept an
  optional ``window``) so it is fully unit-testable offline; live runs pass closures that build the
  VDS / executeQueries requests via :func:`build_vds_query` / :func:`build_dax`.
* **Additive + advisory.** Only adds ``match["verification"]`` and ``summary["verification"]``;
  never changes the deterministic tier / score / bucket.
* **Read-only, aggregate-only.** Every probe is a single scalar aggregate. No row-level data leaves
  either platform.

Original work; the VDS / DAX / envelope shapes are independently re-implemented here so this skill
folder stays self-contained (no cross-skill imports).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:  # package or flat-script execution
    from . import compare as _compare
except ImportError:  # pragma: no cover - exercised via flat script execution
    import compare as _compare

normalize_token = _compare.normalize_token

# Canonical probe functions. Each maps to a Tableau VDS function token and a DAX builder.
FUNC_DISTINCT = "DISTINCTCOUNT"
FUNC_SUM = "SUM"
FUNC_MIN = "MIN"
FUNC_MAX = "MAX"

# Tableau VDS uses ``COUNTD`` for a distinct count; the rest share the token.
_VDS_FUNCTION = {FUNC_DISTINCT: "COUNTD", FUNC_SUM: "SUM", FUNC_MIN: "MIN", FUNC_MAX: "MAX"}

# Defaults (also surfaced as CLI flags by compare_estate.py).
DEFAULT_TOP_N = 10
DEFAULT_MAX_COLS = 4
DEFAULT_MAX_PROBES_PER_PAIR = 24
DEFAULT_RTOL = 0.01
DEFAULT_ATOL = 0.0


# ======================================================================================
# Column-kind classification
# ======================================================================================
_TAB_NUMERIC = {"INTEGER", "REAL", "FLOAT", "NUMBER", "DECIMAL", "DOUBLE", "BIGINT", "SMALLINT"}
_TAB_DATE = {"DATE", "DATETIME", "TIMESTAMP"}
_FAB_NUMERIC = {"int64", "double", "decimal"}
_FAB_DATE = {"datetime", "date"}


def _tableau_kind(data_type: Optional[str]) -> str:
    t = (data_type or "").strip().upper()
    if t in _TAB_NUMERIC:
        return "numeric"
    if t in _TAB_DATE:
        return "date"
    if not t or t in {"UNKNOWN", "TUPLE", "TABLE"}:
        return "unknown"
    return "other"


def _fabric_kind(data_type: Optional[str]) -> str:
    t = (data_type or "").strip().lower()
    if t in _FAB_NUMERIC:
        return "numeric"
    if t in _FAB_DATE:
        return "date"
    if not t:
        return "unknown"
    return "other"


def column_kind(tableau_type: Optional[str], fabric_type: Optional[str]) -> str:
    """Combined kind used for probe selection: ``date`` / ``numeric`` / ``other``.

    A column is only treated as ``date`` or ``numeric`` when the two sides agree (treating an
    unknown/blank type on one side as compatible). Anything else is ``other`` (distinct-count only).
    """
    tk, fk = _tableau_kind(tableau_type), _fabric_kind(fabric_type)
    pair = {tk, fk} - {"unknown"}
    if pair == {"date"}:
        return "date"
    if pair == {"numeric"}:
        return "numeric"
    if not pair:  # both unknown -> assume distinct-count-only
        return "other"
    return "other"


# ======================================================================================
# Probe planning
# ======================================================================================
def _index_fields(fields: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """``[{name,dataType[,table]}]`` -> ``{normalized: {name, dataType, table}}`` (first non-blank wins)."""
    out: Dict[str, Dict[str, str]] = {}
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        key = normalize_token(f.get("name"))
        if not key or key in out:
            continue
        out[key] = {
            "name": f.get("name") or "",
            "dataType": f.get("dataType") or f.get("type") or "",
            "table": f.get("table") or "",
            "role": str(f.get("role") or "").strip().lower(),
        }
    return out


def _is_generic(token: str) -> bool:
    return token in _compare._GENERIC_COLUMN_TOKENS


def plan_probes(
    tableau_ds: Dict[str, Any],
    fabric_model: Dict[str, Any],
    max_cols: int = DEFAULT_MAX_COLS,
) -> Dict[str, List[Dict[str, Any]]]:
    """Plan the probes for one matched pair. Pure + deterministic; no network.

    Returns ``{"window_candidates": [...], "equality_probes": [...]}``:

      * **window_candidates** -- shared date (preferred) then numeric columns we can ``MIN`` / ``MAX``
        to establish each side's range and the common overlap window.
      * **equality_probes** -- the values we compare *within* that window: ``SUM`` for numeric
        columns and ``DISTINCTCOUNT`` for every shared column. Distinctive (non-generic) columns are
        planned first so a small budget is spent where it discriminates most; capped at ``max_cols``.
    """
    tab = _index_fields((tableau_ds or {}).get("fields", []))
    fab = _index_fields((fabric_model or {}).get("columns", []))
    shared = sorted(set(tab) & set(fab), key=lambda k: (_is_generic(k), k))

    window_candidates: List[Dict[str, Any]] = []
    equality_probes: List[Dict[str, Any]] = []
    cols_used = 0
    for key in shared:
        if cols_used >= max(0, max_cols):
            break
        fcol, tcol = fab[key], tab[key]
        if not fcol.get("table") or not fcol.get("name"):
            continue  # cannot build a DAX reference without a table-qualified column
        kind = column_kind(tcol.get("dataType"), fcol.get("dataType"))
        ref = {
            "column": key,
            "tableau_field": tcol["name"],
            "fabric_table": fcol["table"],
            "fabric_column": fcol["name"],
            "kind": kind,
        }
        cols_used += 1
        # A window axis must be a stable *dimension*. Never range on an additive **measure**
        # (e.g. ``Sales``): filtering a measure by its own MIN/MAX overlap is self-referential and
        # would flag a pure Fabric superset (same data, just more rows) as a false ``mismatch``.
        # Dates and numeric dimensions (year / key / id) stay valid axes; when only measures are
        # shared we fall back to the conservative containment verdict instead of a bogus window.
        is_measure = tcol.get("role") == "measure"
        if kind == "date" or (kind == "numeric" and not is_measure):
            window_candidates.append(ref)
        # Equality signal: SUM only makes sense for a numeric measure; distinct count for anything.
        if kind == "numeric":
            equality_probes.append(dict(ref, function=FUNC_SUM))
        equality_probes.append(dict(ref, function=FUNC_DISTINCT))

    # Prefer a *date* window (history depth is the usual difference); fall back to numeric.
    window_candidates.sort(key=lambda r: 0 if r["kind"] == "date" else 1)
    return {"window_candidates": window_candidates, "equality_probes": equality_probes}


# ======================================================================================
# Value coercion + range arithmetic
# ======================================================================================
def _to_number(value: Any) -> Optional[float]:
    """Best-effort numeric coercion (ints, floats, numeric strings, thousands separators)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return f if f == f else None  # drop NaN
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")


def _iso_date(value: Any) -> Optional[str]:
    """Normalise a date/datetime scalar to ``YYYY-MM-DD`` (so a date and date-at-midnight compare)."""
    if value is None:
        return None
    s = str(value).strip().replace("T", " ")
    m = _ISO_DATE_RE.match(s)
    if not m:
        return None
    return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def date_parts(iso: str) -> Optional[Tuple[int, int, int]]:
    """``"2021-03-09"`` -> ``(2021, 3, 9)`` for a DAX ``DATE(y,m,d)`` literal."""
    m = _ISO_DATE_RE.match(str(iso or ""))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _range_relationship(
    tmin: Any, tmax: Any, fmin: Any, fmax: Any, *, kind: str, rtol: float
) -> Tuple[str, Optional[Any], Optional[Any]]:
    """Classify two ranges and return ``(relationship, overlap_lo, overlap_hi)``.

    ``relationship`` is one of ``equal`` / ``subset`` (Tableau within Fabric -> Fabric is a superset)
    / ``superset`` (Tableau contains Fabric) / ``partial`` / ``disjoint`` / ``unknown``. When the
    overlap is empty ``relationship`` is ``disjoint`` and the bounds are ``None``.
    """
    if kind == "date":
        a0, a1 = _iso_date(tmin), _iso_date(tmax)
        b0, b1 = _iso_date(fmin), _iso_date(fmax)
    else:
        a0, a1 = _to_number(tmin), _to_number(tmax)
        b0, b1 = _to_number(fmin), _to_number(fmax)
    if a0 is None or a1 is None or b0 is None or b1 is None:
        return ("unknown", None, None)
    if a1 < a0:
        a0, a1 = a1, a0
    if b1 < b0:
        b0, b1 = b1, b0

    lo = a0 if a0 > b0 else b0      # max of the two mins
    hi = a1 if a1 < b1 else b1      # min of the two maxes
    if lo > hi:
        return ("disjoint", None, None)

    def _eq(x: Any, y: Any) -> bool:
        if kind == "date":
            return x == y
        return abs((x or 0.0) - (y or 0.0)) <= (rtol * max(abs(x or 0.0), abs(y or 0.0)) + 1e-9)

    if _eq(a0, b0) and _eq(a1, b1):
        rel = "equal"
    elif a0 >= b0 and a1 <= b1:
        rel = "subset"      # Tableau is inside Fabric -> Fabric is the superset
    elif b0 >= a0 and b1 <= a1:
        rel = "superset"    # Tableau contains Fabric
    else:
        rel = "partial"
    return (rel, lo, hi)


# ======================================================================================
# Request builders (pure) + envelope parsers (pure)
# ======================================================================================
def build_vds_query(
    field_caption: str, vds_func: str, window: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Build a single-aggregate VizQL Data Service query, optionally range-filtered to a window."""
    query: Dict[str, Any] = {
        "fields": [{"fieldCaption": field_caption, "function": vds_func, "fieldAlias": "a0"}]
    }
    if window and window.get("min") is not None and window.get("max") is not None:
        wfield = window.get("tableau_field")
        if window.get("kind") == "date":
            query["filters"] = [{
                "field": {"fieldCaption": wfield},
                "filterType": "QUANTITATIVE_DATE",
                "quantitativeFilterType": "RANGE",
                "minDate": _iso_date(window["min"]),
                "maxDate": _iso_date(window["max"]),
            }]
        else:
            query["filters"] = [{
                "field": {"fieldCaption": wfield},
                "filterType": "QUANTITATIVE_NUMERICAL",
                "quantitativeFilterType": "RANGE",
                "min": _to_number(window["min"]),
                "max": _to_number(window["max"]),
            }]
    return query


def _dax_ref(table: str, column: str) -> str:
    return "'%s'[%s]" % (str(table).replace("'", "''"), str(column).replace("]", "]]"))


def _dax_window_filter(window: Dict[str, Any]) -> Optional[str]:
    ref = _dax_ref(window.get("fabric_table", ""), window.get("fabric_column", ""))
    if window.get("kind") == "date":
        lo, hi = date_parts(window.get("min")), date_parts(window.get("max"))
        if not lo or not hi:
            return None
        return "%s >= DATE(%d,%d,%d) && %s <= DATE(%d,%d,%d)" % (
            ref, lo[0], lo[1], lo[2], ref, hi[0], hi[1], hi[2])
    lo, hi = _to_number(window.get("min")), _to_number(window.get("max"))
    if lo is None or hi is None:
        return None
    return "%s >= %s && %s <= %s" % (ref, _dax_num(lo), ref, _dax_num(hi))


def _dax_num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else repr(float(x))


def build_dax(
    function: str, table: str, column: str, window: Optional[Dict[str, Any]] = None
) -> str:
    """Build a single-scalar ``EVALUATE ROW(...)`` DAX query, optionally filtered to a window."""
    ref = _dax_ref(table, column)
    fn = (function or "").strip().upper()
    inner = {
        FUNC_DISTINCT: "DISTINCTCOUNT(%s)" % ref,
        FUNC_SUM: "SUM(%s)" % ref,
        FUNC_MIN: "MIN(%s)" % ref,
        FUNC_MAX: "MAX(%s)" % ref,
    }.get(fn)
    if inner is None:
        raise ValueError("unsupported probe function: %r" % function)
    if window and window.get("min") is not None and window.get("max") is not None:
        filt = _dax_window_filter(window)
        if filt:
            inner = "CALCULATE(%s, %s)" % (inner, filt)
    return 'EVALUATE ROW("v", %s)' % inner


def vds_function(function: str) -> str:
    """Map a canonical probe function to its Tableau VDS token (``DISTINCTCOUNT`` -> ``COUNTD``)."""
    f = (function or "").strip().upper()
    return _VDS_FUNCTION.get(f, f)


def parse_vds_scalar(rows: Any, alias: str = "a0") -> Tuple[Any, Optional[str]]:
    """Pull a single aggregate value out of a VDS ``data`` array (one clean row expected)."""
    if rows is None:
        return (None, "VizQL Data Service unavailable")
    if not isinstance(rows, list):
        return (None, "unexpected VDS response shape")
    if len(rows) != 1:
        return (None, "not aggregatable (%d rows)" % len(rows))
    row = rows[0]
    if not isinstance(row, dict):
        return (None, "unexpected VDS row shape")
    if alias in row:
        return (row[alias], None)
    if len(row) == 1:
        return (next(iter(row.values())), None)
    return (None, "alias %r not in VDS row" % alias)


def parse_executequeries_scalar(payload: Any) -> Tuple[Any, Optional[str]]:
    """Pull a single scalar out of a Power BI ``executeQueries`` envelope."""
    if payload is None:
        return (None, "no executeQueries response")
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return (None, str(msg)[:200])
    try:
        rows = payload["results"][0]["tables"][0]["rows"]
    except (KeyError, IndexError, TypeError):
        return (None, "unrecognized executeQueries envelope")
    if not rows:
        return (None, "empty result set")
    row = rows[0]
    if not isinstance(row, dict) or not row:
        return (None, "empty executeQueries row")
    for key in ("[v]", "v"):
        if key in row:
            return (row[key], None)
    return (next(iter(row.values())), None)


# Phrases Analysis Services / Power BI emit when a model's tables hold no rows because the
# semantic model has never been processed/refreshed (a very common state for freshly-created
# Fabric mirror models). Detecting this lets us turn a vague "inconclusive" into an actionable
# "refresh the model, then re-verify" instead of looking like a failure or a false mismatch.
_NO_DATA_PHRASES = (
    "needs to be recalculated or refreshed",
    "does not hold any data",
    "has not been processed",
    "needs to be refreshed",
    "no data because",
    "column which does not hold any data",
)


def is_no_data_error(message: Any) -> bool:
    """True when an error string indicates a Fabric model holds no rows (not yet refreshed)."""
    if not message:
        return False
    low = str(message).lower()
    return any(phrase in low for phrase in _NO_DATA_PHRASES)


def extract_executequeries_error(payload: Any) -> Optional[str]:
    """Pull the most useful human-readable message out of an ``executeQueries`` error envelope.

    Power BI nests the actionable detail under ``error.pbi.error.details[].detail.value``; fall
    back to ``error.message`` then ``error.code``. Pure -- safe on any malformed shape.
    """
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if not isinstance(err, dict):
        return str(err)[:200] if err else None
    pbi = err.get("pbi.error") or err.get("pbiError")
    if isinstance(pbi, dict):
        details = pbi.get("details")
        if isinstance(details, list):
            for d in details:
                detail = d.get("detail") if isinstance(d, dict) else None
                if isinstance(detail, dict):
                    val = detail.get("value")
                    if val:
                        return str(val)[:240]
    if err.get("message"):
        return str(err["message"])[:240]
    if err.get("code"):
        return str(err["code"])[:240]
    return None


# ======================================================================================
# Equality comparison (within a window) + verdict
# ======================================================================================
def compare_values(
    function: str,
    tableau_value: Any,
    fabric_value: Any,
    *,
    rtol: float = DEFAULT_RTOL,
    atol: float = DEFAULT_ATOL,
) -> str:
    """Compare one (windowed) equality probe. Returns ``agree`` / ``disagree`` / ``inconclusive``.

    Numeric answers compare with a relative+absolute tolerance (extracts drift; floats are inexact).
    A one-sided ``None`` (a probe we could not read) is ``inconclusive``, never a mismatch.
    """
    if tableau_value is None or fabric_value is None:
        if tableau_value is None and fabric_value is None:
            return "agree"
        return "inconclusive"
    tn, fn = _to_number(tableau_value), _to_number(fabric_value)
    if tn is not None and fn is not None:
        return "agree" if abs(tn - fn) <= (atol + rtol * max(abs(tn), abs(fn))) else "disagree"
    if str(tableau_value).strip().lower() == str(fabric_value).strip().lower():
        return "agree"
    return "disagree"


ProbeFn = Callable[..., Tuple[Any, Optional[str]]]


def _establish_window(
    tableau_luid, workspace_id, dataset_id, candidate, tableau_probe, fabric_probe, rtol
) -> Tuple[str, Optional[Dict[str, Any]], Dict[str, Any]]:
    """Range a single window candidate on both sides -> (relationship, window_or_None, detail)."""
    kind = candidate["kind"]
    tmin, terr1 = tableau_probe(tableau_luid, candidate["tableau_field"], vds_function(FUNC_MIN), None)
    tmax, terr2 = tableau_probe(tableau_luid, candidate["tableau_field"], vds_function(FUNC_MAX), None)
    fmin, ferr1 = fabric_probe(
        workspace_id, dataset_id, candidate["fabric_table"], candidate["fabric_column"], FUNC_MIN, None)
    fmax, ferr2 = fabric_probe(
        workspace_id, dataset_id, candidate["fabric_table"], candidate["fabric_column"], FUNC_MAX, None)
    detail = {
        "column": candidate["column"], "kind": kind,
        "tableau_range": [tmin, tmax], "fabric_range": [fmin, fmax],
    }
    if any([terr1, terr2, ferr1, ferr2]):
        detail["error"] = "; ".join(p for p in (terr1, terr2, ferr1, ferr2) if p)[:160]
        return ("unknown", None, detail)
    rel, lo, hi = _range_relationship(tmin, tmax, fmin, fmax, kind=kind, rtol=rtol)
    detail["relationship"] = rel
    detail["overlap"] = [lo, hi]
    if rel in ("unknown", "disjoint"):
        return (rel, None, detail)
    window = {
        "kind": kind, "min": lo, "max": hi,
        "tableau_field": candidate["tableau_field"],
        "fabric_table": candidate["fabric_table"],
        "fabric_column": candidate["fabric_column"],
    }
    return (rel, window, detail)


def _containment_verdict(equality_results: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Conservative read of *unwindowed* equality probes when no window column was available.

    Returns ``(verdict, relationship)``. Never emits ``mismatch`` from magnitude alone -- without a
    shared time/key column we cannot tell "different data" from "more data".
    """
    comparable = [r for r in equality_results if r.get("outcome") in ("agree", "directional")]
    if not comparable:
        return ("inconclusive", "unknown")
    if all(r["outcome"] == "agree" for r in comparable):
        return ("verified", "equal")
    # Some differ: is every difference consistent with a single containment direction?
    directions = {r["direction"] for r in comparable if r.get("direction") in ("fabric_ge", "tableau_ge")}
    if directions and len(directions) == 1:
        rel = "subset" if directions == {"fabric_ge"} else "superset"
        return ("compatible", rel)
    return ("inconclusive", "partial")


def verify_match(
    tableau_luid: Optional[str],
    workspace_id: Optional[str],
    dataset_id: Optional[str],
    plan: Dict[str, List[Dict[str, Any]]],
    tableau_probe: ProbeFn,
    fabric_probe: ProbeFn,
    *,
    rtol: float = DEFAULT_RTOL,
    atol: float = DEFAULT_ATOL,
    max_probes: int = DEFAULT_MAX_PROBES_PER_PAIR,
) -> Dict[str, Any]:
    """Run windowed-overlap verification for one matched pair and reduce it to a verdict.

    ``tableau_probe(luid, field_caption, vds_function, window) -> (value, error)`` and
    ``fabric_probe(workspace_id, dataset_id, table, column, function, window) -> (value, error)``
    are injected, so this is pure logic over their results.

    Verdict: ``verified`` (overlap agrees -- the relationship may be equal/subset/superset),
    ``compatible`` (no window column; raw totals are consistent with one side being a superset),
    ``mismatch`` (overlap disagrees, or ranges are disjoint), ``inconclusive`` (nothing comparable).
    """
    window_candidates = list((plan or {}).get("window_candidates") or [])
    equality_probes = list((plan or {}).get("equality_probes") or [])
    budget = {"left": max(0, max_probes)}

    # 1) Establish a window from the best available candidate (date preferred).
    relationship = "unknown"
    window: Optional[Dict[str, Any]] = None
    range_detail: Optional[Dict[str, Any]] = None
    for cand in window_candidates:
        if budget["left"] < 4:
            break
        budget["left"] -= 4
        relationship, window, range_detail = _establish_window(
            tableau_luid, workspace_id, dataset_id, cand, tableau_probe, fabric_probe, rtol)
        if relationship == "disjoint":
            return {
                "verdict": "mismatch", "method": "windowed", "relationship": "disjoint",
                "reason_code": None,
                "window_column": cand["column"], "range": range_detail,
                "probes_run": 4, "probes_agreed": 0, "probes_disagreed": 0,
                "probes_inconclusive": 0, "agreement": None, "probes": [],
                "notes": ["no overlapping %s range (Tableau %s vs Fabric %s) -- different data" % (
                    cand["column"], range_detail.get("tableau_range"), range_detail.get("fabric_range"))],
            }
        if window is not None:
            break  # got a usable overlap

    # 2) Run equality probes -- windowed when we have an overlap, else raw (containment mode).
    results: List[Dict[str, Any]] = []
    notes: List[str] = []
    agreed = disagreed = inconclusive = 0
    # Track whether Fabric could return any data at all. When Tableau returns real values but
    # *every* Fabric probe is null/errored, the model can't be verified -- and we say why:
    #   fabric_no_data  -> 200+null or an explicit "needs refresh" error (an unrefreshed model);
    #   fabric_err      -> the model errored on every probe (paused capacity / DirectQuery source
    #                      not configured / not yet processed) -- surfaced as ``fabric_unreadable``.
    fabric_no_data = fabric_real = fabric_err = tableau_real = 0
    first_fabric_err: Optional[str] = None
    method = "windowed" if window is not None else "containment"

    for probe in equality_probes:
        if budget["left"] <= 0:
            break
        budget["left"] -= 1
        fn = probe["function"]
        tval, terr = tableau_probe(tableau_luid, probe["tableau_field"], vds_function(fn), window)
        fval, ferr = fabric_probe(
            workspace_id, dataset_id, probe["fabric_table"], probe["fabric_column"], fn, window)
        rec: Dict[str, Any] = {
            "column": probe["column"], "function": fn,
            "tableau": tval, "fabric": fval, "windowed": window is not None,
        }
        if fval is not None and not ferr:
            fabric_real += 1
        if tval is not None and not terr:
            tableau_real += 1
        if ferr:
            fabric_err += 1
            if first_fabric_err is None:
                first_fabric_err = str(ferr)
            if is_no_data_error(ferr):
                fabric_no_data += 1
        elif fval is None and tval is not None and not terr:
            fabric_no_data += 1
        if terr or ferr:
            rec["outcome"] = "inconclusive"
            inconclusive += 1
            why = "; ".join(p for p in (terr, ferr) if p)
            if why and len(notes) < 6:
                notes.append("%s(%s): %s" % (fn, probe["column"], why[:120]))
        elif window is not None:
            outcome = compare_values(fn, tval, fval, rtol=rtol, atol=atol)
            rec["outcome"] = outcome
            if outcome == "agree":
                agreed += 1
            elif outcome == "disagree":
                disagreed += 1
                if len(notes) < 6:
                    notes.append("%s(%s) on overlap: Tableau=%s vs Fabric=%s" % (
                        fn, probe["column"], tval, fval))
            else:
                inconclusive += 1
        else:
            # Containment mode: classify rather than pass/fail.
            outcome = compare_values(fn, tval, fval, rtol=rtol, atol=atol)
            if outcome == "agree":
                rec["outcome"] = "agree"
                agreed += 1
            elif outcome == "disagree":
                tn, fnum = _to_number(tval), _to_number(fval)
                if tn is not None and fnum is not None:
                    rec["outcome"] = "directional"
                    rec["direction"] = "fabric_ge" if fnum >= tn else "tableau_ge"
                else:
                    rec["outcome"] = "inconclusive"
                    inconclusive += 1
            else:
                rec["outcome"] = "inconclusive"
                inconclusive += 1
        results.append(rec)

    # 3) Reduce to a verdict.
    if window is not None:
        comparable = agreed + disagreed
        if disagreed:
            verdict = "mismatch"
        elif comparable:
            verdict = "verified"
        else:
            verdict = "inconclusive"
    else:
        verdict, relationship = _containment_verdict(results)

    # Fold in window-establishment evidence, then classify an inconclusive verdict that is purely
    # "Fabric returned nothing while Tableau returned data" into an actionable reason.
    if range_detail:
        rerr = range_detail.get("error")
        frange = range_detail.get("fabric_range") or []
        trange = range_detail.get("tableau_range") or []
        tableau_in_range = any(x is not None for x in trange)
        if rerr:
            fabric_err += 1
            if first_fabric_err is None:
                first_fabric_err = str(rerr)
            if is_no_data_error(rerr):
                fabric_no_data += 1
        elif frange and all(x is None for x in frange) and tableau_in_range:
            fabric_no_data += 1
        if tableau_in_range:
            tableau_real += 1
    reason_code = None
    if verdict == "inconclusive" and fabric_real == 0 and tableau_real >= 1:
        if fabric_no_data >= 1:
            reason_code = "fabric_no_data"
            notes = [
                "Fabric model returned no data -- the semantic model holds no rows (it has not been "
                "refreshed/processed). Refresh it in Fabric, then re-run --verify."
            ] + notes
        elif fabric_err >= 1:
            reason_code = "fabric_unreadable"
            hint = (" (e.g. %s)" % first_fabric_err[:90]) if first_fabric_err else ""
            notes = [
                "Fabric model could not be queried -- every probe failed%s while Tableau returned "
                "data. The model may be unrefreshed, its capacity paused, or its source connection "
                "not configured. Resolve it, then re-run --verify." % hint
            ] + notes

    return {
        "verdict": verdict,
        "method": method,
        "relationship": relationship,
        "reason_code": reason_code,
        "window_column": (window or {}).get("column") if window else (
            range_detail.get("column") if range_detail else None),
        "range": range_detail,
        "probes_run": len(results),
        "probes_agreed": agreed,
        "probes_disagreed": disagreed,
        "probes_inconclusive": inconclusive,
        "agreement": round(agreed / (agreed + disagreed), 4) if (agreed + disagreed) else None,
        "probes": results,
        "notes": notes,
    }


# ======================================================================================
# Estate-level orchestration (mutates result additively; never touches tier/score)
# ======================================================================================
def _index_by(entries: Sequence[Dict[str, Any]], *keys: str) -> Dict[Any, Dict[str, Any]]:
    out: Dict[Any, Dict[str, Any]] = {}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        for k in keys:
            v = e.get(k)
            if v and v not in out:
                out[v] = e
    return out


_REL_PHRASE = {
    "equal": "identical range",
    "subset": "Fabric is a superset (extra history)",
    "superset": "Tableau is a superset",
    "partial": "overlapping ranges",
    "equal_containment": "raw totals match",
}


def verification_note(v: Dict[str, Any]) -> str:
    """A short, human-readable summary of a verification verdict for the report."""
    verdict = v.get("verdict")
    rel = v.get("relationship")
    rel_txt = _REL_PHRASE.get(rel, "")
    if v.get("reason_code") == "fabric_no_data":
        return ("inconclusive -- Fabric model holds no data (not yet refreshed); refresh the "
                "semantic model in Fabric, then re-run --verify")
    if v.get("reason_code") == "fabric_unreadable":
        return ("inconclusive -- Fabric model could not be queried (unrefreshed, capacity paused, "
                "or source connection not configured); resolve it, then re-run --verify")
    if verdict == "verified":
        base = "empirically verified (%d/%d overlap probe[s] agree" % (
            v.get("probes_agreed", 0), v.get("probes_agreed", 0) + v.get("probes_disagreed", 0))
        return base + ("; %s)" % rel_txt if rel_txt else ")")
    if verdict == "compatible":
        return "compatible (raw totals consistent with %s; no shared time/key column to window on)" % (
            rel_txt or "same data, different volume")
    if verdict == "mismatch":
        lead = (v.get("notes") or ["data does not line up"])[0]
        return "VERIFY MISMATCH -- %s" % lead
    return "verification inconclusive (no comparable probe ran)"


def verify_estate(
    result: Dict[str, Any],
    tableau_inventory: Sequence[Dict[str, Any]],
    fabric_inventory: Sequence[Dict[str, Any]],
    tableau_probe: ProbeFn,
    fabric_probe: ProbeFn,
    *,
    top_n: int = DEFAULT_TOP_N,
    max_cols: int = DEFAULT_MAX_COLS,
    rtol: float = DEFAULT_RTOL,
    atol: float = DEFAULT_ATOL,
    max_probes_per_pair: int = DEFAULT_MAX_PROBES_PER_PAIR,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Empirically verify the top ``top_n`` confident/partial matches. Additive; mutates ``result``.

    Adds ``match["verification"]`` (+ a short ``match["verification_note"]``) and a
    ``summary["verification"]`` rollup. The deterministic tier / score / bucket are never changed --
    a ``mismatch`` is advisory, surfaced for a human to confirm.
    """
    matches = result.get("matches") or []
    tab_idx = _index_by(tableau_inventory, "luid", "name")
    fab_idx = _index_by(fabric_inventory, "id")
    fab_by_nw: Dict[Any, Dict[str, Any]] = {}
    for e in fabric_inventory or []:
        if isinstance(e, dict):
            fab_by_nw.setdefault((e.get("name"), e.get("workspace")), e)

    def _log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    eligible = [
        m for m in matches
        if m.get("best_match") and m.get("bucket") in ("already_exists", "partial")
    ]
    eligible.sort(key=lambda m: m.get("score") or 0.0, reverse=True)

    counts = {"verified": 0, "compatible": 0, "mismatch": 0, "inconclusive": 0}
    attempted = probes_run = fabric_no_data = fabric_unreadable = 0
    for m in eligible[: max(0, top_n)]:
        best = m.get("best_match") or {}
        ds = tab_idx.get(m.get("tableau_luid")) or tab_idx.get(m.get("tableau_name"))
        model = fab_idx.get(best.get("fabric_id")) or fab_by_nw.get(
            (best.get("fabric_name"), best.get("workspace")))
        attempted += 1
        if ds is None or model is None:
            v = {
                "verdict": "inconclusive", "method": "none", "relationship": "unknown",
                "probes_run": 0, "agreement": None, "probes": [],
                "notes": ["could not resolve both inventory entries"],
            }
            m["verification"] = v
            m["verification_note"] = verification_note(v)
            counts["inconclusive"] += 1
            continue

        plan = plan_probes(ds, model, max_cols=max_cols)
        _log("Verifying %s vs %s..." % (m.get("tableau_name"), best.get("fabric_name")))
        v = verify_match(
            m.get("tableau_luid"),
            best.get("workspace_id") or model.get("workspaceId"),
            best.get("fabric_id") or model.get("id"),
            plan, tableau_probe, fabric_probe,
            rtol=rtol, atol=atol, max_probes=max_probes_per_pair,
        )
        m["verification"] = v
        m["verification_note"] = verification_note(v)
        probes_run += v.get("probes_run", 0)
        if v.get("reason_code") == "fabric_no_data":
            fabric_no_data += 1
        elif v.get("reason_code") == "fabric_unreadable":
            fabric_unreadable += 1
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1

    result.setdefault("summary", {})["verification"] = {
        "enabled": True,
        "attempted": attempted,
        "verified": counts["verified"],
        "compatible": counts["compatible"],
        "mismatch": counts["mismatch"],
        "inconclusive": counts["inconclusive"],
        "fabric_no_data": fabric_no_data,
        "fabric_unreadable": fabric_unreadable,
        "probes_run": probes_run,
        "top_n": top_n,
        "max_cols": max_cols,
        "rtol": rtol,
    }
    return result
