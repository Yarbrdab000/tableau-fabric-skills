#!/usr/bin/env python3
"""Emit the embedded-datasource ``rebind-plan.json`` (frozen cross-skill schema_version "1.0").

Phase 3d. This is where the embedded-datasource work becomes a *decision*: given the enumerated
embedded datasources (:mod:`embedded_inventory`), their duplicate clusters (:mod:`embedded_cluster`),
and their scores against the Fabric / published estates (:mod:`embedded_score`), assign every
embedded datasource a migration **action** and a **binding target**, and roll the whole estate up
into the headline a migration lead reads ("of N embedded datasources, M overlap a published
datasource -> rebind; K cluster into J new consolidated models; ...").

The output is consumed by two downstream skills, so the shape is a **frozen contract**
(``schema_version "1.0"`` -- see ``resources/rebind-plan-contract.md``):

  * the **calc-compiler / migration** skill builds the models and *writes back* (per ``model_id``)
    ``resolved_model_name`` + ``model_path``, and (per workbook) ``resolved_report_folder`` +
    ``bound_model_id`` -- this module seeds those slots but never computes them;
  * the **dashboard** skill binds each report, keying off ``binding_status`` FIRST
    (``built_local`` -> byPath, ``existing_fabric`` -> byConnection, ``landed_to_delta`` /
    ``needs_attention`` -> unbound).

Two gates from the contract are honoured here:

  * **Gate 1** (:func:`apply_view_dependency_feedback`): a dashboard ``view_dependency_report``
    downgrades a ``rebind_*`` entry to ``convert_embedded`` **only** when a dropped reference names
    an object the embedded ``<datasource>`` *actually contains* (a workbook-local calc / set / group
    / bin / LOD) -- presence-in-embedded-source, not drop volume.
  * **Gate 2**: an ``existing_fabric`` binding carries the live ``byConnection`` identity straight
    from the comparison and is excluded from the rebuild set.

Pure and offline; reuses the scoring already done upstream.
"""

from __future__ import annotations

import csv
import re
from typing import Any, Dict, List, Optional, Sequence

try:  # package or flat-script execution
    from . import embedded_cluster as cluster_mod
    from . import embedded_score as score_mod
except ImportError:  # pragma: no cover - exercised via flat script execution
    import embedded_cluster as cluster_mod
    import embedded_score as score_mod

SCHEMA_VERSION = "1.0"

# Band cut at/above which an overlap is treated as "an equivalent already exists" -> rebind/reuse.
# Matches the comparison engine's Strong band so the two skills agree on what "already exists" means.
DEFAULT_STRONG_CUT = 0.65

# The frozen action vocabulary and binding-status vocabulary (documented in the contract).
ACTIONS = ("convert_embedded", "rebind_to_published", "rebind_to_rebuilt", "consolidate_new_model")
BINDING_STATUSES = ("built_local", "existing_fabric", "landed_to_delta", "needs_attention")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: Optional[str]) -> str:
    return _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-") or "x"


def _fabric_model_id(best: Dict[str, Any]) -> str:
    return "mdl-fabric-" + _slug(best.get("fabric_id") or best.get("fabric_name"))


def _published_model_id(best: Dict[str, Any]) -> str:
    return "mdl-published-" + _slug(best.get("published_luid") or best.get("published_name"))


def _cluster_model_id(cluster_id: str, consolidate: bool) -> str:
    return ("mdl-cluster-" if consolidate else "mdl-embedded-") + cluster_id


def _reuse(block: Optional[Dict[str, Any]], strong_cut: float) -> bool:
    """True when a score block is a confident reuse candidate (best match clears the strong cut)."""
    return bool(block and block.get("best_match") and (block.get("score") or 0.0) >= strong_cut)


def _byconnection(best: Dict[str, Any]) -> Dict[str, Any]:
    """The ``existing_fabric`` binding target: the live identity the dashboard binds ``byConnection``."""
    return {
        "kind": "byConnection",
        "workspace_id": best.get("workspace_id"),
        "semantic_model_id": best.get("fabric_id"),
        "dataset_name": best.get("fabric_name"),
    }


def _evidence(score_block: Optional[Dict[str, Any]], cluster: Dict[str, Any]) -> Dict[str, Any]:
    fab = (score_block or {}).get("fabric") or None
    pub = (score_block or {}).get("published") or None

    def trim(b, ident_keys):
        if not b:
            return None
        bm = b.get("best_match") or {}
        out = {"tier": b.get("tier"), "score": b.get("score")}
        for k in ident_keys:
            if k in bm:
                out[k] = bm.get(k)
        if bm.get("shared_tables"):
            out["shared_tables"] = bm.get("shared_tables")
        if bm.get("shared_column_count") is not None:
            out["shared_column_count"] = bm.get("shared_column_count")
        return out

    return {
        "fabric": trim(fab, ("fabric_name", "workspace", "workspace_id", "fabric_id")),
        "published": trim(pub, ("published_name", "published_luid", "project")),
        "cluster": {
            "cluster_id": cluster.get("cluster_id"),
            "size": cluster.get("size"),
            "is_duplicate_group": cluster.get("is_duplicate_group"),
        },
    }


def _objects_brief(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The embedded datasource's workbook-local object list (Gate-1 presence test source)."""
    return [{"name": o.get("name"), "kind": o.get("kind")}
            for o in (row.get("objects") or []) if o.get("name")]


def _decide_cluster(rep_fab, rep_pub, cluster, strong_cut):
    """Decide the cluster-wide ``(action_kind, model_id, model_origin, binding_status, target_seed)``.

    ``action_kind`` is one of ``existing_fabric`` / ``published`` / ``consolidate`` / ``convert`` --
    a *cluster intent* the per-member assignment turns into the contract's four actions.
    """
    cid = cluster["cluster_id"]
    size = cluster.get("size", 1)
    if _reuse(rep_fab, strong_cut):
        best = rep_fab["best_match"]
        return ("existing_fabric", _fabric_model_id(best), "existing_fabric",
                "existing_fabric", _byconnection(best))
    if _reuse(rep_pub, strong_cut):
        best = rep_pub["best_match"]
        return ("published", _published_model_id(best), "published",
                "built_local", {"kind": "byPath", "model_path": None})
    if size > 1:
        return ("consolidate", _cluster_model_id(cid, True), "consolidated_new_model",
                "built_local", {"kind": "byPath", "model_path": None})
    return ("convert", _cluster_model_id(cid, False), "embedded_convert",
            "built_local", {"kind": "byPath", "model_path": None})


def build_rebind_plan(
    rows: Sequence[Dict[str, Any]],
    cluster_result: Dict[str, Any],
    score_result: Dict[str, Any],
    *,
    source_map: Optional[Dict[str, str]] = None,
    strong_cut: float = DEFAULT_STRONG_CUT,
) -> Dict[str, Any]:
    """Assemble the ``rebind-plan.json`` object (``schema_version "1.0"``).

    ``rows`` are the embedded-inventory rows; ``cluster_result`` / ``score_result`` are the outputs of
    :func:`embedded_cluster.cluster_embedded` / :func:`embedded_score.score_embedded`. ``source_map``
    is the ``{source_id: workbook_luid}`` linkage (NEVER assume ``source_id == workbook_luid``); it is
    derived from the rows when omitted.
    """
    rows = list(rows)
    by_key_row = {cluster_mod.member_key(r, i): r for i, r in enumerate(rows)}
    by_key_score = {s["member_key"]: s for s in score_result.get("scores", [])}
    rep_scores = score_mod.attach_cluster_scores(cluster_result, score_result)

    if source_map is None:
        source_map = {}
        for r in rows:
            sid = r.get("source_id")
            if sid:
                source_map.setdefault(sid, r.get("workbook_luid") or "")

    plan: List[Dict[str, Any]] = []
    models: Dict[str, Dict[str, Any]] = {}

    for cluster in cluster_result.get("clusters", []):
        rep = rep_scores.get(cluster["cluster_id"], {})
        kind, model_id, origin, binding_status, target_seed = _decide_cluster(
            rep.get("fabric"), rep.get("published"), cluster, strong_cut)

        # Register the model once (the calc-compiler writes resolved_model_name / model_path back).
        if model_id not in models:
            entry = {"model_id": model_id, "origin": origin,
                     "resolved_model_name": None, "model_path": None}
            if origin == "existing_fabric":
                entry["connection"] = target_seed   # the byConnection identity (Gate 2)
            models[model_id] = entry

        rep_key = rep.get("representative_member_key")
        members = cluster.get("members", [])
        for m in members:
            mk = m["member_key"]
            row = by_key_row.get(mk, {})
            score_block = by_key_score.get(mk)
            is_rep = (mk == rep_key) or (rep_key is None and m is members[0])

            action, binding, target, caveats = _assign_member(
                kind, model_id, binding_status, target_seed, is_rep, cluster, row)

            plan.append({
                "workbook_luid": row.get("workbook_luid", ""),
                "workbook_name": row.get("workbook_name", ""),
                "source_ref": row.get("source_id", ""),
                "datasource_id": row.get("datasource_id", ""),
                "datasource_name": row.get("datasource_name", ""),
                "cluster_id": cluster["cluster_id"],
                "action": action,
                "model_id": model_id,
                "binding_status": binding,
                "binding_target": target,
                "evidence": _evidence(score_block, cluster),
                "caveats": caveats,
                "objects": _objects_brief(row),
            })

    summary = _summarize(plan, cluster_result, models, strong_cut)
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": summary,
        "source_map": [{"source_id": sid, "workbook_luid": luid}
                       for sid, luid in source_map.items()],
        "clusters": cluster_result.get("clusters", []),
        "models": models,
        "plan": plan,
    }


def generate_plan(
    rows: Sequence[Dict[str, Any]],
    fabric: Optional[Sequence[Dict[str, Any]]] = None,
    published: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    source_map: Optional[Dict[str, str]] = None,
    threshold: float = cluster_mod.DEFAULT_CLUSTER_THRESHOLD,
    strong_cut: float = DEFAULT_STRONG_CUT,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """One-call orchestrator: cluster -> score -> build the rebind plan.

    A convenience wrapper for the CLI / callers that have the raw embedded rows plus the Fabric and
    published inventories. Each stage stays independently testable; this just chains them.
    """
    rows = list(rows)
    cluster_result = cluster_mod.cluster_embedded(rows, threshold=threshold)
    score_result = score_mod.score_embedded(
        rows, fabric=fabric, published=published, weights=weights)
    return build_rebind_plan(
        rows, cluster_result, score_result, source_map=source_map, strong_cut=strong_cut)


def _assign_member(kind, model_id, binding_status, target_seed, is_rep, cluster, row):
    """Turn a cluster intent + member role into the contract's ``(action, binding_status, target, caveats)``."""
    caveats: List[str] = []
    target = dict(target_seed)
    if target.get("kind") == "byPath":
        target["model_id"] = model_id

    # An empty datasource cannot be bound to anything -- flag for a human.
    if not (row.get("fields") or row.get("sources")):
        return ("convert_embedded", "needs_attention",
                {"kind": "unbound", "reason": "embedded datasource has no fields or sources"},
                ["thin embedded datasource -- no fields or sources to bind"])

    if kind == "existing_fabric":
        # Already in Fabric: rebind every copy to the live model; excluded from the rebuild set.
        caveats.append("existing_fabric reuse -- excluded from the rebuild set (Gate 2)")
        return ("rebind_to_rebuilt", "existing_fabric", target, caveats)

    if kind == "published":
        caveats.append("overlaps a published Tableau datasource -- rebind to its model")
        return ("rebind_to_published", binding_status, target, caveats)

    if kind == "consolidate":
        if is_rep:
            caveats.append(
                "representative of a %d-workbook duplicate group -- build one consolidated model"
                % cluster.get("size", 1))
            return ("consolidate_new_model", binding_status, target, caveats)
        caveats.append("duplicate of consolidated model %s -- rebind, do not rebuild" % model_id)
        return ("rebind_to_rebuilt", binding_status, target, caveats)

    # convert: a unique embedded datasource with no published / Fabric home.
    return ("convert_embedded", binding_status, target, caveats)


def _summarize(plan, cluster_result, models, strong_cut):
    by_action = {a: 0 for a in ACTIONS}
    by_binding = {b: 0 for b in BINDING_STATUSES}
    for e in plan:
        by_action[e["action"]] = by_action.get(e["action"], 0) + 1
        by_binding[e["binding_status"]] = by_binding.get(e["binding_status"], 0) + 1

    workbooks = {e["workbook_luid"] or e["source_ref"] for e in plan}
    consolidated_models = sorted(
        {e["model_id"] for e in plan if e["action"] == "consolidate_new_model"})
    rebind_published = by_action["rebind_to_published"]
    reuse_fabric = sum(1 for e in plan if e["binding_status"] == "existing_fabric")
    consolidate_members = sum(
        1 for e in plan if e["model_id"] in set(consolidated_models))
    convert = by_action["convert_embedded"]

    headline = (
        "Of %d embedded datasource(s) across %d workbook(s): %d overlap a published datasource "
        "(rebind), %d already exist in Fabric (reuse, excluded from rebuild), %d cluster into %d new "
        "consolidated model(s), %d convert in place."
        % (len(plan), len(workbooks), rebind_published, reuse_fabric, consolidate_members,
           len(consolidated_models), convert)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "embedded_total": len(plan),
        "workbook_total": len(workbooks),
        "cluster_total": cluster_result.get("summary", {}).get("cluster_count", 0),
        "duplicate_group_count": cluster_result.get("summary", {}).get("duplicate_group_count", 0),
        "model_total": len(models),
        "consolidated_model_total": len(consolidated_models),
        "by_action": by_action,
        "by_binding_status": by_binding,
        "rebind_to_published": rebind_published,
        "existing_fabric_reuse": reuse_fabric,
        "consolidated_members": consolidate_members,
        "convert_in_place": convert,
        "strong_cut": strong_cut,
        "headline": headline,
    }


# --------------------------------------------------------------------------------------
# Gate 1: view-dependency feedback (presence-in-embedded-source downgrade)
# --------------------------------------------------------------------------------------
def apply_view_dependency_feedback(plan: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    """Fold a dashboard ``view_dependency_report`` into the plan (Gate 1; mutates + returns ``plan``).

    ``report`` is ``{workbook_luid|source_ref: {refs_total, refs_dropped, dropped:[...], visuals_emptied}}``
    (or ``{"bindings": [ {workbook_luid|source_ref, dropped:[...]} ]}``). A ``rebind_*`` entry is
    downgraded to ``convert_embedded`` **only** when one of its dropped references names an object the
    embedded ``<datasource>`` actually contains -- a workbook-local calc / set / group / bin / LOD --
    because reproducing such an object requires converting the embedded source, not rebinding. A drop
    that is merely untranslatable in the *published* model (absent from the embedded source) yields the
    same stub under convert, so it is NOT a downgrade trigger.
    """
    feedback = _index_feedback(report)
    downgraded = 0
    for e in plan.get("plan", []):
        if not str(e.get("action", "")).startswith("rebind"):
            continue
        dropped = feedback.get(e.get("workbook_luid") or "") or feedback.get(e.get("source_ref") or "")
        if not dropped:
            continue
        present = {_norm_obj(o.get("name")) for o in (e.get("objects") or [])}
        hits = sorted({d for d in dropped if _norm_obj(d) in present})
        if not hits:
            continue
        # Downgrade: rebind would drop an object the embedded source actually carries.
        e["action"] = "convert_embedded"
        cid = e.get("cluster_id") or "x"
        e["model_id"] = _cluster_model_id(cid, False)
        e["binding_status"] = "built_local"
        e["binding_target"] = {"kind": "byPath", "model_id": e["model_id"], "model_path": None}
        e.setdefault("caveats", []).append(
            "Gate 1: downgraded to convert_embedded -- dropped object(s) present in the embedded "
            "source: %s" % ", ".join(hits))
        plan["models"].setdefault(
            e["model_id"], {"model_id": e["model_id"], "origin": "embedded_convert",
                            "resolved_model_name": None, "model_path": None})
        downgraded += 1
    if downgraded:
        plan["summary"] = _summarize(
            plan["plan"], {"summary": {
                "cluster_count": plan["summary"].get("cluster_total", 0),
                "duplicate_group_count": plan["summary"].get("duplicate_group_count", 0)}},
            plan["models"], plan["summary"].get("strong_cut", DEFAULT_STRONG_CUT))
        plan["summary"]["gate1_downgrades"] = downgraded
    return plan


def _norm_obj(name: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _index_feedback(report: Dict[str, Any]) -> Dict[str, List[str]]:
    """Normalise a view-dependency report into ``{key: [dropped ref names]}``."""
    out: Dict[str, List[str]] = {}

    def collect(key, payload):
        dropped = []
        for d in (payload.get("dropped") or []):
            dropped.append(d.get("name") if isinstance(d, dict) else d)
        out[str(key)] = [d for d in dropped if d]

    if isinstance(report.get("bindings"), list):
        for b in report["bindings"]:
            key = b.get("workbook_luid") or b.get("source_ref") or b.get("source_id")
            if key is not None:
                collect(key, b)
    else:
        for key, payload in report.items():
            if isinstance(payload, dict):
                collect(key, payload)
    return out


# --------------------------------------------------------------------------------------
# Renderings (Markdown rollup + executive CSV) -- additive, mirror the export style
# --------------------------------------------------------------------------------------
_ACTION_LABEL = {
    "convert_embedded": "Convert embedded (build new)",
    "rebind_to_published": "Rebind to published datasource",
    "rebind_to_rebuilt": "Rebind to resolved model",
    "consolidate_new_model": "Consolidate into one new model",
}


def render_markdown(plan: Dict[str, Any]) -> str:
    s = plan.get("summary", {}) or {}
    out: List[str] = []
    out.append("# Embedded-datasource rebind plan")
    out.append("")
    out.append("_schema_version %s_" % plan.get("schema_version", SCHEMA_VERSION))
    out.append("")
    out.append(s.get("headline", ""))
    out.append("")

    out.append("## By action")
    out.append("")
    out.append("| Action | Count |")
    out.append("|---|---:|")
    for a in ACTIONS:
        out.append("| %s | %d |" % (_ACTION_LABEL.get(a, a), (s.get("by_action") or {}).get(a, 0)))
    out.append("")

    out.append("## By binding status")
    out.append("")
    out.append("| Binding status | Count |")
    out.append("|---|---:|")
    for b in BINDING_STATUSES:
        out.append("| %s | %d |" % (b, (s.get("by_binding_status") or {}).get(b, 0)))
    out.append("")

    dup = [c for c in plan_clusters(plan) if c.get("size", 1) > 1]
    if dup:
        out.append("## Duplicate groups (consolidation candidates)")
        out.append("")
        out.append("| Cluster | Size | Representative |")
        out.append("|---|---:|---|")
        for c in dup:
            rep = (c.get("representative") or {}).get("datasource_name", "")
            out.append("| %s | %d | %s |" % (c.get("cluster_id"), c.get("size", 0), rep))
        out.append("")

    out.append("## Per-workbook plan")
    out.append("")
    out.append("| Workbook | Datasource | Cluster | Action | Binding | Model id |")
    out.append("|---|---|---|---|---|---|")
    for e in plan.get("plan", []):
        out.append("| %s | %s | %s | %s | %s | %s |" % (
            e.get("workbook_name") or e.get("workbook_luid") or e.get("source_ref"),
            e.get("datasource_name"), e.get("cluster_id"),
            _ACTION_LABEL.get(e.get("action"), e.get("action")),
            e.get("binding_status"), e.get("model_id")))
    out.append("")
    return "\n".join(out)


def plan_clusters(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The cluster summaries embedded for rendering (empty when the plan was built without them)."""
    return plan.get("clusters", []) or []


_CSV_COLUMNS = [
    ("Workbook", "workbook_name"),
    ("Workbook LUID", "workbook_luid"),
    ("Source ref", "source_ref"),
    ("Datasource", "datasource_name"),
    ("Cluster", "cluster_id"),
    ("Action", "action"),
    ("Model id", "model_id"),
    ("Binding status", "binding_status"),
    ("Fabric tier", "_fab_tier"),
    ("Fabric score", "_fab_score"),
    ("Published tier", "_pub_tier"),
    ("Published score", "_pub_score"),
    ("Caveats", "_caveats"),
]


def _csv_cell(entry: Dict[str, Any], key: str) -> Any:
    if not key.startswith("_"):
        return entry.get(key)
    ev = entry.get("evidence") or {}
    fab = ev.get("fabric") or {}
    pub = ev.get("published") or {}
    if key == "_fab_tier":
        return fab.get("tier") or ""
    if key == "_fab_score":
        return fab.get("score") if fab.get("score") is not None else ""
    if key == "_pub_tier":
        return pub.get("tier") or ""
    if key == "_pub_score":
        return pub.get("score") if pub.get("score") is not None else ""
    if key == "_caveats":
        return "; ".join(entry.get("caveats") or [])
    return ""


def build_export_rows(plan: Dict[str, Any]) -> List[List[Any]]:
    """``[header, *rows]`` -- one row per plan entry, the analyst pivot source."""
    rows: List[List[Any]] = [[h for h, _ in _CSV_COLUMNS]]
    for e in plan.get("plan", []):
        rows.append([_csv_cell(e, key) for _, key in _CSV_COLUMNS])
    return rows


def write_export_csv(plan: Dict[str, Any], path: str) -> None:
    rows = build_export_rows(plan)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        for r in rows:
            w.writerow(["" if c is None else c for c in r])
