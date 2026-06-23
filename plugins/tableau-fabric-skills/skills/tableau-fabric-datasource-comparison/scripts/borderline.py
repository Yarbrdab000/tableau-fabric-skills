#!/usr/bin/env python3
"""Borderline decision review: detail the *differences* behind the on-the-fence verdicts.

The deterministic engine (``compare.py``) gives every Tableau datasource a single ``tier`` /
``score`` / ``bucket`` -- *already in Fabric* / *partial* / *needs rebuild*. For the clear-cut
datasources that single line is enough. For the **on-the-fence** ones -- the ones sitting between
"reuse what's in Fabric" and "rebuild from scratch" -- a score alone is not actionable: a migration
lead needs to see *exactly* how the candidate Fabric model differs from the Tableau datasource before
they commit to reuse-vs-rebuild.

This module finds that on-the-fence set and, for each member, computes a precise **structural diff**
against its best Fabric candidate:

  * which columns are in the Tableau datasource but **missing** from the Fabric model;
  * which columns the Fabric model has that the datasource does **not**;
  * shared columns whose **data types** disagree;
  * the underlying **source-table** gap (tables on one side but not the other);
  * the existing **business-logic** caveat (calculated fields not confirmed as measures);

plus an honest, advisory ``recommendation_hint`` (lean-reuse / lean-rebuild / reuse-with-logic-
review) that **never** overrides the deterministic verdict.

"On the fence" is the union of four independent triggers (each recorded as a reason code), so the
default errs toward surfacing *more* for review -- consistent with the skill's asymmetric
conservatism (silently calling a real datasource "already exists" is the costliest error):

  * ``partial_tier``         -- the verdict landed in the Partial bucket (inherently a judgment call);
  * ``near_reuse_boundary``  -- the score sits within ``band`` of the already-in-Fabric (Strong) cutoff;
  * ``near_rebuild_boundary``-- the score sits within ``band`` of the rebuild (Partial) cutoff;
  * ``low_confidence``       -- the confidence layer rated a reuse/rebuild verdict low;
  * ``logic_unverified``     -- a structurally-matched datasource whose calcs are not confirmed as
                               measures (the dangerous "looks done, isn't" case).

**Pure, offline, additive, read-only over the report** -- it only reads the comparison ``result``
plus the two inventories (needed for the column/table lists the matches don't retain) and never
changes a ``tier`` / ``score`` / ``bucket`` / ``priority`` / ``confidence``. Original work.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# Default half-width of the review band around each bucket boundary. A match whose score sits within
# this of the already-in-Fabric (Strong) cutoff or the rebuild (Partial) cutoff is "on the fence".
# Deliberately modest so the borderline set stays the genuine tail, not half the estate.
DEFAULT_REVIEW_BAND = 0.08

# Enumerated names are capped per diff list (the *counts* are always exact); keeps the report / JSON
# bounded on very wide datasources without hiding the magnitude of the difference.
_NAME_CAP = 50
# Cap on the names listed in the summary rollup (matches are score-sorted, so this is the top set).
_SUMMARY_NAME_CAP = 100


def _compare():
    """Lazy handle to the sibling ``compare`` module (works as a package or via the flat test path).

    Imported lazily so there is no import-time cycle: ``compare`` only pulls this module in *after*
    it is fully defined, at which point ``import compare`` here resolves cleanly.
    """
    try:  # pragma: no cover - import shim
        from . import compare as _c
    except ImportError:  # pragma: no cover - import shim
        import compare as _c
    return _c


def _band_min(bands: Optional[Sequence[Sequence[Any]]], tier: str, default: float) -> float:
    """Read a tier's minimum score from the ``summary.bands`` table (falls back to ``default``)."""
    for row in bands or []:
        if row and row[0] == tier:
            try:
                return float(row[1])
            except (TypeError, ValueError):
                return default
    return default


def _norm_map(fields: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, Dict[str, str]]:
    """``[{name,dataType,...}]`` -> ``{normalized_name: {"name": original, "type": dtype}}``.

    Uses the same ``normalize_token`` the scorer uses so the diff lines up exactly with the column
    overlap that produced the score. First non-blank occurrence of a normalized name wins.
    """
    c = _compare()
    out: Dict[str, Dict[str, str]] = {}
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        orig = f.get("name")
        key = c.normalize_token(orig)
        if not key or key in out:
            continue
        out[key] = {"name": orig or "", "type": (f.get("dataType") or f.get("type") or "")}
    return out


def _find_tableau(tableau: Sequence[Dict[str, Any]], match: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Locate the Tableau datasource record backing a match (by luid, then by name)."""
    luid = match.get("tableau_luid")
    name = match.get("tableau_name")
    if luid:
        for ds in tableau or []:
            if ds.get("luid") == luid:
                return ds
    for ds in tableau or []:
        if ds.get("name") == name:
            return ds
    return None


def _find_fabric(fabric: Sequence[Dict[str, Any]], best: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Locate the winning Fabric model record (by id+name, then by name)."""
    if not best:
        return None
    fid = best.get("fabric_id")
    fname = best.get("fabric_name")
    if fid:
        for fm in fabric or []:
            if fm.get("id") == fid and fm.get("name") == fname:
                return fm
    for fm in fabric or []:
        if fm.get("name") == fname:
            return fm
    return None


def borderline_reasons(
    match: Dict[str, Any], *, strong_cut: float, partial_cut: float, band: float
) -> List[str]:
    """Return the reason codes that make a match 'on the fence' (empty list -> not borderline)."""
    reasons: List[str] = []
    score = float(match.get("score") or 0.0)
    bucket = match.get("bucket")
    best = match.get("best_match")

    # 1. A Partial verdict is, by definition, the reconcile-before-reuse judgment call.
    if bucket == "partial":
        reasons.append("partial_tier")

    # 2. Score sitting within the band of either decision boundary (needs a real candidate; an empty
    #    best_match is an unambiguous rebuild, never "on the fence").
    if best:
        if abs(score - strong_cut) <= band:
            reasons.append("near_reuse_boundary")
        if abs(score - partial_cut) <= band:
            reasons.append("near_rebuild_boundary")

    # 3. The confidence layer rated a reuse/rebuild verdict low (a coin-flip dressed as a decision).
    conf = match.get("confidence") or {}
    if str(conf.get("level")).lower() == "low" and bucket in ("already_exists", "rebuild"):
        reasons.append("low_confidence")

    # 4. Columns line up but the business logic did not provably carry over -- a structural
    #    "already exists" that may still need real rebuild work.
    lp = match.get("logic_parity") or {}
    if bucket in ("already_exists", "partial") and lp.get("status") in ("unverified", "partial"):
        reasons.append("logic_unverified")

    return list(dict.fromkeys(reasons))  # de-dupe, preserve order


def _recommendation_hint(
    score: float, strong_cut: float, partial_cut: float, logic_status: Optional[str]
) -> str:
    """An honest, advisory nudge for an on-the-fence match. Never overrides the deterministic verdict.

    Leans on which boundary the score is closer to; an unverified/partial logic parity tempers a
    reuse lean (the calculations may still need rebuilding even if the columns match).
    """
    mid = (strong_cut + partial_cut) / 2.0
    if score >= strong_cut:
        hint = "lean_reuse"
    elif score <= partial_cut:
        hint = "lean_rebuild"
    else:
        hint = "lean_reuse" if score >= mid else "lean_rebuild"
    if hint == "lean_reuse" and logic_status in ("unverified", "partial"):
        return "reuse_with_logic_review"
    return hint


def diff_for_match(
    match: Dict[str, Any],
    tableau: Sequence[Dict[str, Any]],
    fabric: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute the column + source-table difference detail for one match (pure)."""
    c = _compare()
    best = match.get("best_match") or {}
    ds = _find_tableau(tableau, match) or {}
    fm = _find_fabric(fabric, best) or {}

    tab = _norm_map(ds.get("fields"))
    fab = _norm_map(fm.get("columns"))
    shared_keys = set(tab) & set(fab)
    tableau_only = sorted(tab[k]["name"] for k in (set(tab) - set(fab)))
    fabric_only = sorted(fab[k]["name"] for k in (set(fab) - set(tab)))
    shared_names = sorted(tab[k]["name"] for k in shared_keys)
    type_mismatches = sorted(
        (
            {
                "column": tab[k]["name"],
                "tableau_type": tab[k]["type"],
                "fabric_type": fab[k]["type"],
            }
            for k in shared_keys
            if not c.type_compatible(tab[k]["type"], fab[k]["type"])
        ),
        key=lambda r: str(r["column"]),
    )

    # Connector-agnostic source-table diff (the same table-name view the scorer matches on, so a
    # lakehouse boundary doesn't manufacture a phantom gap).
    tab_tables = c._table_name_set(ds.get("sources"))
    fab_tables = c._table_name_set(fm.get("sources"), fm.get("tables"))
    shared_tables = sorted(tab_tables & fab_tables)
    tableau_only_tables = sorted(tab_tables - fab_tables)
    fabric_only_tables = sorted(fab_tables - tab_tables)

    return {
        "columns": {
            "shared_count": len(shared_keys),
            "tableau_total": len(tab),
            "fabric_total": len(fab),
            "tableau_only_count": len(tableau_only),
            "fabric_only_count": len(fabric_only),
            "type_mismatch_count": len(type_mismatches),
            "shared": shared_names[:_NAME_CAP],
            "tableau_only": tableau_only[:_NAME_CAP],
            "fabric_only": fabric_only[:_NAME_CAP],
            "type_mismatches": type_mismatches[:_NAME_CAP],
        },
        "source": {
            "compared": bool(best.get("source_compared")),
            "coverage": best.get("source_coverage"),
            "shared_tables": shared_tables[:_NAME_CAP],
            "tableau_only_tables": tableau_only_tables[:_NAME_CAP],
            "fabric_only_tables": fabric_only_tables[:_NAME_CAP],
        },
    }


def annotate(
    result: Dict[str, Any],
    tableau: Sequence[Dict[str, Any]],
    fabric: Sequence[Dict[str, Any]],
    *,
    band: float = DEFAULT_REVIEW_BAND,
) -> Dict[str, Any]:
    """Attach ``match["borderline"]`` to the on-the-fence matches + a ``summary["borderline"]`` rollup.

    Read-only over the deterministic verdict: it adds keys, never alters ``tier`` / ``score`` /
    ``bucket``. Idempotent (re-running recomputes the same annotation).
    """
    summary = result.get("summary") or {}
    bands = summary.get("bands")
    strong_cut = _band_min(bands, "Strong", 0.65)
    partial_cut = _band_min(bands, "Partial", 0.40)
    matches = result.get("matches") or []

    try:
        band = float(band)
    except (TypeError, ValueError):
        band = DEFAULT_REVIEW_BAND

    count = 0
    by_origin = {"already_exists": 0, "partial": 0, "rebuild": 0}
    reason_counts: Dict[str, int] = {}
    hint_counts: Dict[str, int] = {}
    names: List[str] = []

    for m in matches:
        # Recompute cleanly each run so the layer is idempotent under re-annotation.
        m.pop("borderline", None)
        reasons = borderline_reasons(
            m, strong_cut=strong_cut, partial_cut=partial_cut, band=band
        )
        if not reasons:
            continue
        diff = diff_for_match(m, tableau, fabric)
        lp = m.get("logic_parity") or {}
        hint = _recommendation_hint(
            float(m.get("score") or 0.0), strong_cut, partial_cut, lp.get("status")
        )
        best = m.get("best_match") or {}
        m["borderline"] = {
            "is_borderline": True,
            "band": band,
            "reasons": reasons,
            "score": m.get("score"),
            "tier": m.get("tier"),
            "bucket": m.get("bucket"),
            "best_match": best.get("fabric_name"),
            "workspace": best.get("workspace"),
            "recommendation_hint": hint,
            "columns": diff["columns"],
            "source": diff["source"],
            "logic_parity": m.get("logic_parity"),
        }

        count += 1
        bkt = m.get("bucket")
        if bkt in by_origin:
            by_origin[bkt] += 1
        for r in reasons:
            reason_counts[r] = reason_counts.get(r, 0) + 1
        hint_counts[hint] = hint_counts.get(hint, 0) + 1
        if len(names) < _SUMMARY_NAME_CAP:
            names.append(m.get("tableau_name"))

    summary["borderline"] = {
        "count": count,
        "band": band,
        "strong_cut": strong_cut,
        "partial_cut": partial_cut,
        "by_origin_bucket": by_origin,
        "reasons": reason_counts,
        "hints": hint_counts,
        "names": names,
    }
    result["summary"] = summary
    return result
