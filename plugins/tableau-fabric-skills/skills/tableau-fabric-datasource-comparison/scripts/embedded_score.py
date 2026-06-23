#!/usr/bin/env python3
"""Score embedded datasources against Fabric models, published Tableau datasources, and each other.

Phase 3c of the comparison skill. Once :mod:`embedded_inventory` has enumerated the embedded
datasources and :mod:`embedded_cluster` has grouped the near-duplicates, the rebind engine needs to
know, for each embedded datasource, **what it most resembles**:

  * a **Fabric semantic model** -- it may already exist in the target estate (``rebind`` / leave it
    alone), and the winning model's connection identity (``workspace_id`` / ``fabric_id`` /
    ``fabric_name``) is exactly what the dashboard skill binds to ``byConnection``;
  * a **published Tableau datasource** -- it may be a private copy of something already published
    (``rebind_to_published``), so we should not rebuild a separate model;
  * **each other** -- handled by :mod:`embedded_cluster`; this module adds a per-cluster rollup that
    attaches the best Fabric / published candidate to the cluster *representative*.

It is **pure and offline** and reuses the existing engine wholesale -- :func:`compare.score_pair`
for the four-signal blend and :func:`compare.band_for` for the tier band. Nothing about scoring,
tiers, weights, or buckets is reinvented here; embedded rows are merely *adapted* to the shapes
``score_pair`` already consumes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # package or flat-script execution
    from . import compare as compare_mod
    from . import embedded_cluster as cluster_mod
except ImportError:  # pragma: no cover - exercised via flat script execution
    import compare as compare_mod
    import embedded_cluster as cluster_mod

member_key = cluster_mod.member_key

# Tiers that mean "an equivalent already exists" -- a Strong+/Exact match is treated as reuse.
_REUSE_TIERS = {"Exact", "Strong"}


def _embedded_as_left(row: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt an embedded-datasource row to the ``tableau_ds`` (left) shape ``score_pair`` reads."""
    return {
        "name": row.get("datasource_name") or row.get("workbook_name") or "",
        "fields": row.get("fields", []) or [],
        "sources": row.get("sources", []) or [],
    }


def _published_as_right(ds: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt a published Tableau datasource to the ``fabric_model`` (right) shape ``score_pair`` reads.

    A published datasource carries ``fields``/``sources`` like the embedded one; the right side of
    ``score_pair`` reads ``columns``/``tables``/``sources``, so we remap without inventing data.
    """
    return {
        "name": ds.get("name") or "",
        "columns": ds.get("fields", []) or [],
        "tables": [s.get("table") for s in (ds.get("sources", []) or []) if s.get("table")],
        "sources": ds.get("sources", []) or [],
    }


def _doc_freq(assets: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, int], int]:
    """Estate document-frequency over normalised column names (mirrors ``compare_inventories``)."""
    doc_freq: Dict[str, int] = {}
    n = 0
    for asset in assets:
        cols = {
            compare_mod.normalize_token(f.get("name"))
            for f in (asset.get("fields") or asset.get("columns") or [])
        }
        cols.discard("")
        for c in cols:
            doc_freq[c] = doc_freq.get(c, 0) + 1
        n += 1
    return doc_freq, n


def _fabric_candidate(fm: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """A scored Fabric candidate, carrying the ``byConnection`` identity the dashboard skill binds."""
    return {
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
    }


def _published_candidate(ds: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """A scored published-datasource candidate (identity = name + luid + project)."""
    return {
        "published_name": ds.get("name"),
        "published_luid": ds.get("luid"),
        "project": ds.get("project"),
        "score": result["score"],
        "signals": result["signals"],
        "source_compared": result["source_compared"],
        "source_coverage": result["source_coverage"],
        "shared_tables": result["shared_tables"],
        "shared_column_count": result["shared_column_count"],
    }


def _rank(
    left: Dict[str, Any],
    targets: Sequence[Dict[str, Any]],
    kind: str,
    weights: Optional[Dict[str, float]],
    bands,
    col_weight,
    top_n: int,
) -> Optional[Dict[str, Any]]:
    """Score one embedded datasource against every target; return best + tier + runners-up.

    ``kind`` is ``"fabric"`` (targets already in model shape) or ``"published"`` (targets are
    Tableau datasources, adapted to model shape here). Returns ``None`` when there are no targets.
    """
    if not targets:
        return None
    scored: List[Dict[str, Any]] = []
    for t in targets:
        if kind == "fabric":
            result = compare_mod.score_pair(left, t, weights, col_weight=col_weight)
            scored.append(_fabric_candidate(t, result))
        else:
            result = compare_mod.score_pair(left, _published_as_right(t), weights, col_weight=col_weight)
            scored.append(_published_candidate(t, result))
    scored.sort(key=lambda c: c["score"], reverse=True)
    best = scored[0]
    best_score = best["score"]
    tier = compare_mod.band_for(best_score, bands)
    return {
        "tier": tier,
        "score": best_score,
        "bucket": compare_mod.rollup_bucket(tier),
        "best_match": best if best_score > 0 else None,
        "candidates": scored[:top_n],
    }


def score_embedded(
    rows: Sequence[Dict[str, Any]],
    fabric: Optional[Sequence[Dict[str, Any]]] = None,
    published: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    weights: Optional[Dict[str, float]] = None,
    bands=None,
    top_n: int = 3,
) -> Dict[str, Any]:
    """Score every embedded datasource against Fabric models and published Tableau datasources.

    Returns ``{"scores": [...], "summary": {...}}``. Each ``scores`` entry is keyed by ``member_key``
    and carries a ``fabric`` and a ``published`` block (each ``None`` when that side was not supplied
    or produced no positive candidate). Reuses ``compare.score_pair`` / ``compare.band_for``; the
    column down-weighting matches ``compare_inventories`` by sharing one estate-wide document
    frequency across the embedded rows and both target lists.
    """
    rows = list(rows or [])
    fabric = list(fabric or [])
    published = list(published or [])
    bands = bands or compare_mod.DEFAULT_BANDS

    # One estate-wide column IDF over everything we are about to compare, so a column that appears
    # everywhere is down-weighted identically on both the Fabric and the published axes.
    doc_freq, n_assets = _doc_freq(list(rows) + list(fabric) + list(published))
    col_weight = compare_mod.column_weight_fn(doc_freq, n_assets)

    scores: List[Dict[str, Any]] = []
    fabric_reuse = published_reuse = 0
    for i, row in enumerate(rows):
        left = _embedded_as_left(row)
        fab = _rank(left, fabric, "fabric", weights, bands, col_weight, top_n)
        pub = _rank(left, published, "published", weights, bands, col_weight, top_n)
        if fab and fab["tier"] in _REUSE_TIERS:
            fabric_reuse += 1
        if pub and pub["tier"] in _REUSE_TIERS:
            published_reuse += 1
        scores.append({
            "member_key": member_key(row, i),
            "workbook_luid": row.get("workbook_luid", ""),
            "source_id": row.get("source_id", ""),
            "datasource_id": row.get("datasource_id", ""),
            "datasource_name": row.get("datasource_name", ""),
            "fabric": fab,
            "published": pub,
        })

    summary = {
        "embedded_total": len(rows),
        "fabric_total": len(fabric),
        "published_total": len(published),
        "fabric_reuse_candidates": fabric_reuse,
        "published_reuse_candidates": published_reuse,
        "weights": dict(weights or compare_mod.DEFAULT_WEIGHTS),
        "bands": [list(b) for b in bands],
    }
    return {"scores": scores, "summary": summary}


def attach_cluster_scores(
    cluster_result: Dict[str, Any],
    score_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Roll the per-member Fabric / published scores up to the cluster representative.

    Returns a ``{cluster_id: {...}}`` map: for each cluster, the representative member's best Fabric
    and published candidate, so the rebind plan can decide a *single* action for the whole duplicate
    group (consolidate to one model, or rebind every copy at the shared best match).
    """
    by_key = {s["member_key"]: s for s in score_result.get("scores", [])}
    out: Dict[str, Any] = {}
    for cluster in cluster_result.get("clusters", []):
        # Representative member = the cluster's most complete copy (most fields), matched by name.
        rep_name = (cluster.get("representative") or {}).get("datasource_name")
        members = cluster.get("members", [])
        rep_member = next((m for m in members if m.get("datasource_name") == rep_name), None)
        if rep_member is None and members:
            rep_member = members[0]
        rep_score = by_key.get(rep_member["member_key"]) if rep_member else None
        out[cluster["cluster_id"]] = {
            "cluster_id": cluster["cluster_id"],
            "size": cluster.get("size"),
            "representative_member_key": rep_member["member_key"] if rep_member else None,
            "fabric": (rep_score or {}).get("fabric"),
            "published": (rep_score or {}).get("published"),
        }
    return out
