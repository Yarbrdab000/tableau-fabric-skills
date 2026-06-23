#!/usr/bin/env python3
"""Fingerprint and cluster embedded datasources so near-duplicates collapse into one group.

A single Tableau estate routinely embeds the *same* datasource into dozens of workbooks (14
dashboards each carrying their own near-identical copy of "Superstore"). For a migration plan those
copies are one asset, not fourteen -- the rebind engine wants to build (or reuse) **one** model and
point every duplicate at it. This module groups structurally-equivalent embedded datasources.

It is **deterministic and offline**:

  * :func:`fingerprint` reduces an embedded datasource to a stable content hash of its normalised
    field-name set + table-name set (the same token normalisation the comparison engine uses), so
    exact structural duplicates collapse instantly regardless of workbook, caption, or ordering.
  * :func:`cluster_embedded` seeds clusters from identical fingerprints, then merges *near*-
    duplicates whose structural similarity clears a threshold. Similarity **reuses
    ``compare.score_pair``** (column + type + source signals; the asset name is weighted out so a
    re-titled copy still groups) -- the engine is not reinvented here.

The output is additive metadata: a list of clusters (size, members, a representative shape) and a
``{member_key: cluster_id}`` index the downstream rebind plan keys consolidation decisions off.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

try:  # package or flat-script execution
    from . import compare as compare_mod
except ImportError:  # pragma: no cover - exercised via flat script execution
    import compare as compare_mod

# Structural-similarity weights for clustering: the asset *name* is deliberately weighted out (two
# workbooks can embed the same datasource under different captions), so grouping rests on columns,
# types, and physical source. ``score_pair`` redistributes any signal it cannot measure.
CLUSTER_WEIGHTS: Dict[str, float] = {"name": 0.0, "column": 0.5, "type": 0.2, "source": 0.3}

# Default merge threshold: only *near-identical* embedded datasources collapse together. Tunable.
DEFAULT_CLUSTER_THRESHOLD = 0.80

# Cap the field / table token lists echoed in each cluster's representative shape.
_REP_TOKEN_CAP = 50


def _field_tokens(row: Dict[str, Any]) -> List[str]:
    toks = {compare_mod.normalize_token(f.get("name")) for f in row.get("fields", []) or []}
    toks.discard("")
    return sorted(toks)


def _table_tokens(row: Dict[str, Any]) -> List[str]:
    return sorted(compare_mod._table_name_set(row.get("sources", []) or []))


def fingerprint(row: Dict[str, Any]) -> str:
    """Stable structural fingerprint of an embedded datasource (normalised fields + tables).

    Two embedded datasources that share the same normalised field-name set and table-name set hash
    identically no matter the host workbook, the caption, or the element order -- so exact structural
    duplicates collapse with zero scoring.
    """
    blob = "F:" + ",".join(_field_tokens(row)) + "|T:" + ",".join(_table_tokens(row))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _as_model(row: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt an embedded-datasource row to the ``fabric_model`` shape ``score_pair`` consumes."""
    return {
        "name": row.get("datasource_name") or row.get("workbook_name") or "",
        "columns": row.get("fields", []) or [],
        "tables": [s.get("table") for s in row.get("sources", []) or [] if s.get("table")],
        "sources": row.get("sources", []) or [],
    }


def _as_tableau(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": row.get("datasource_name") or row.get("workbook_name") or "",
        "fields": row.get("fields", []) or [],
        "sources": row.get("sources", []) or [],
    }


def structural_similarity(a: Dict[str, Any], b: Dict[str, Any],
                          weights: Optional[Dict[str, float]] = None) -> float:
    """Structural similarity (0..1) between two embedded datasources, via ``compare.score_pair``."""
    return compare_mod.score_pair(_as_tableau(a), _as_model(b),
                                  weights=weights or CLUSTER_WEIGHTS)["score"]


def member_key(row: Dict[str, Any], index: int) -> str:
    """A stable per-embedded-datasource key (``source_id::datasource_id``; index breaks ties)."""
    sid = row.get("source_id") or row.get("workbook_luid") or f"row{index}"
    did = row.get("datasource_id") or row.get("datasource_name") or f"ds{index}"
    return f"{sid}::{did}"


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        root = i
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[i] != root:
            self.parent[i], i = root, self.parent[i]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Keep the smaller index as the root for determinism.
        if ra < rb:
            self.parent[rb] = ra
        else:
            self.parent[ra] = rb


def cluster_embedded(
    rows: List[Dict[str, Any]],
    *,
    threshold: float = DEFAULT_CLUSTER_THRESHOLD,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Group structurally-equivalent embedded datasources.

    Returns ``{"clusters": [...], "index": {member_key: cluster_id}, "summary": {...}}``. Clusters
    are ordered by descending size then by their lowest member key, and assigned stable ids
    (``ec-001`` ...). Each cluster carries its members, a representative shape (the most complete
    member's field / table tokens), the seeding fingerprint(s), and whether it is a duplicate group
    (``size > 1``).
    """
    weights = weights or CLUSTER_WEIGHTS
    n = len(rows)
    uf = _UnionFind(n)

    # 1. Seed: identical fingerprints are exact structural duplicates -- union with no scoring.
    fps = [fingerprint(r) for r in rows]
    by_fp: Dict[str, List[int]] = {}
    for i, fp in enumerate(fps):
        by_fp.setdefault(fp, []).append(i)
    for idxs in by_fp.values():
        for j in idxs[1:]:
            uf.union(idxs[0], j)

    # 2. Merge near-duplicates: compare one representative per distinct fingerprint (sorted for
    #    determinism); union when structural similarity clears the threshold.
    rep_by_fp: Dict[str, int] = {fp: idxs[0] for fp, idxs in by_fp.items()}
    rep_indices = sorted(rep_by_fp.values())
    for a in range(len(rep_indices)):
        ia = rep_indices[a]
        for b in range(a + 1, len(rep_indices)):
            ib = rep_indices[b]
            if uf.find(ia) == uf.find(ib):
                continue
            if structural_similarity(rows[ia], rows[ib], weights) >= threshold:
                uf.union(ia, ib)

    # 3. Collect members per root.
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    raw_clusters: List[Tuple[int, List[int]]] = list(groups.items())
    # Order: larger clusters first, then by lowest member key for a stable tie-break.
    raw_clusters.sort(key=lambda kv: (-len(kv[1]), min(member_key(rows[i], i) for i in kv[1])))

    clusters: List[Dict[str, Any]] = []
    index: Dict[str, str] = {}
    for ci, (_, idxs) in enumerate(raw_clusters, start=1):
        cluster_id = f"ec-{ci:03d}"
        # Representative = the member with the most fields (the most complete copy).
        rep_i = max(idxs, key=lambda i: (len(rows[i].get("fields", []) or []), -i))
        rep = rows[rep_i]
        members = []
        for i in sorted(idxs, key=lambda i: member_key(rows[i], i)):
            r = rows[i]
            key = member_key(r, i)
            index[key] = cluster_id
            members.append({
                "member_key": key,
                "workbook_luid": r.get("workbook_luid", ""),
                "source_id": r.get("source_id", ""),
                "datasource_id": r.get("datasource_id", ""),
                "datasource_name": r.get("datasource_name", ""),
                "workbook_name": r.get("workbook_name", ""),
                "source_path": r.get("source_path", ""),
            })
        fps_in = sorted({fps[i] for i in idxs})
        clusters.append({
            "cluster_id": cluster_id,
            "size": len(idxs),
            "is_duplicate_group": len(idxs) > 1,
            "fingerprints": fps_in,
            "representative": {
                "datasource_name": rep.get("datasource_name", ""),
                "field_count": len(rep.get("fields", []) or []),
                "table_count": len(rep.get("sources", []) or []),
                "fields": _field_tokens(rep)[:_REP_TOKEN_CAP],
                "tables": _table_tokens(rep)[:_REP_TOKEN_CAP],
            },
            "members": members,
        })

    duplicate_groups = [c for c in clusters if c["is_duplicate_group"]]
    summary = {
        "embedded_total": n,
        "cluster_count": len(clusters),
        "duplicate_group_count": len(duplicate_groups),
        "consolidatable_datasources": sum(c["size"] for c in duplicate_groups),
        "largest_cluster_size": clusters[0]["size"] if clusters else 0,
        "threshold": threshold,
    }
    return {"clusters": clusters, "index": index, "summary": summary}
