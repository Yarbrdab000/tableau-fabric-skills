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

Each per-workbook plan entry carries a ``label`` -- a SEPARATE per-entry field (NOT folded into
``source_ref``, which stays the ``source_id`` string). ``label`` is the datasource's caption-preferred
display name (``caption`` | ``formatted-name`` | raw internal ``name``, mirroring the migration skill's
``_datasource_label``) -- the exact case-insensitive selector ``migrate_datasource(datasource=label)``
/ ``list_workbook_datasources`` accept to pick this embedded datasource out of its workbook. It is
unsafe to re-derive from ``source_ref`` (a workbook can hold several embedded datasources), so the
emitter surfaces it explicitly. In the no-caption case ``label`` is derived from the RAW
(un-debracketed) ``<datasource name=...>`` so it exactly matches the migration side's raw ``name``
compare. Entries also carry an optional ``drift`` fingerprint (``{table_count, column_count,
calc_count}``) the orchestrator re-checks at resolve time. Both additive to ``1.0``.
"""

from __future__ import annotations

import csv
import json
import re
from json import JSONDecodeError
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
MODEL_ORIGINS = ("existing_fabric", "published", "consolidated_new_model", "embedded_convert")

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_TOP_LEVEL_REQUIRED = ("schema_version", "summary", "source_map", "clusters", "models", "plan")
_PLAN_REQUIRED = (
    "workbook_luid", "source_ref", "action", "model_id", "label",
    "binding_status", "binding_target", "evidence", "caveats",
)


class RebindPlanSchemaError(ValueError):
    """Raised when a rebind-plan payload is malformed or contract-incompatible."""


def _fail(path: str, message: str) -> None:
    raise RebindPlanSchemaError(f"{path}: {message}")


def _expect(condition: bool, path: str, message: str) -> None:
    if not condition:
        _fail(path, message)


def _expect_dict(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "expected object")
    return value


def _expect_list(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        _fail(path, "expected array")
    return value


def _expect_str(value: Any, path: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        _fail(path, "expected string")
    if not allow_empty and not value.strip():
        _fail(path, "expected non-empty string")
    return value


def _expect_optional_str(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(path, "expected string or null")
    return value


def _expect_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "expected number")
    return float(value)


def _expect_nonneg_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "expected non-negative integer")
    if value < 0:
        _fail(path, "expected non-negative integer")
    return value


def _validate_date_table(value: Any, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        _fail(path, "expected object or null")


def _validate_binding_target(entry: Dict[str, Any], models: Dict[str, Any], path: str) -> None:
    status = _expect_str(entry.get("binding_status"), f"{path}.binding_status", allow_empty=False)
    _expect(status in BINDING_STATUSES, f"{path}.binding_status",
            "must be one of %s" % ", ".join(BINDING_STATUSES))

    target = _expect_dict(entry.get("binding_target"), f"{path}.binding_target")
    kind = _expect_str(target.get("kind"), f"{path}.binding_target.kind", allow_empty=False)

    if status == "existing_fabric":
        _expect(kind == "byConnection", f"{path}.binding_target.kind",
                "must be 'byConnection' when binding_status is existing_fabric")
        _expect_str(target.get("workspace_id"), f"{path}.binding_target.workspace_id", allow_empty=False)
        _expect_str(target.get("semantic_model_id"), f"{path}.binding_target.semantic_model_id", allow_empty=False)
        _expect_str(target.get("dataset_name"), f"{path}.binding_target.dataset_name", allow_empty=False)
        _validate_date_table(target.get("date_table"), f"{path}.binding_target.date_table")
        return

    if status in ("built_local", "landed_to_delta"):
        _expect(kind == "byPath", f"{path}.binding_target.kind",
                "must be 'byPath' when binding_status is %s" % status)
        tid = _expect_str(target.get("model_id"), f"{path}.binding_target.model_id", allow_empty=False)
        mid = _expect_str(entry.get("model_id"), f"{path}.model_id", allow_empty=False)
        _expect(tid == mid, f"{path}.binding_target.model_id", "must equal plan entry model_id")
        _expect_optional_str(target.get("model_path"), f"{path}.binding_target.model_path")
        _validate_date_table(target.get("date_table"), f"{path}.binding_target.date_table")
        _expect(mid in models, f"{path}.model_id", "must reference a key in models")
        return

    # needs_attention
    _expect(kind == "unbound", f"{path}.binding_target.kind",
            "must be 'unbound' when binding_status is needs_attention")
    _expect_str(target.get("reason"), f"{path}.binding_target.reason", allow_empty=False)
    _expect("date_table" not in target, f"{path}.binding_target.date_table",
            "must be absent for unbound targets")


def _validate_evidence(value: Any, path: str) -> None:
    ev = _expect_dict(value, path)
    cluster = _expect_dict(ev.get("cluster"), f"{path}.cluster")
    _expect_str(cluster.get("cluster_id"), f"{path}.cluster.cluster_id", allow_empty=False)
    _expect_nonneg_int(cluster.get("size"), f"{path}.cluster.size")
    _expect(isinstance(cluster.get("is_duplicate_group"), bool), f"{path}.cluster.is_duplicate_group",
            "expected boolean")
    for axis in ("fabric", "published"):
        block = ev.get(axis)
        if block is None:
            continue
        b = _expect_dict(block, f"{path}.{axis}")
        _expect_str(b.get("tier"), f"{path}.{axis}.tier", allow_empty=False)
        _expect_number(b.get("score"), f"{path}.{axis}.score")


def validate_rebind_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a rebind-plan payload against the frozen schema_version ``1.0`` contract.

    Raises :class:`RebindPlanSchemaError` on malformed/missing/wrong-type inputs.
    """
    root = _expect_dict(plan, "root")
    for k in _TOP_LEVEL_REQUIRED:
        _expect(k in root, f"root.{k}", "missing required field")

    schema_version = _expect_str(root.get("schema_version"), "schema_version", allow_empty=False)
    _expect(schema_version == SCHEMA_VERSION, "schema_version",
            "expected %r" % SCHEMA_VERSION)

    summary = _expect_dict(root.get("summary"), "summary")
    _expect_str(summary.get("schema_version"), "summary.schema_version", allow_empty=False)
    _expect(summary.get("schema_version") == SCHEMA_VERSION, "summary.schema_version",
            "expected %r" % SCHEMA_VERSION)
    _expect_nonneg_int(summary.get("embedded_total"), "summary.embedded_total")
    _expect_nonneg_int(summary.get("workbook_total"), "summary.workbook_total")
    _expect_nonneg_int(summary.get("cluster_total"), "summary.cluster_total")
    _expect_nonneg_int(summary.get("duplicate_group_count"), "summary.duplicate_group_count")
    _expect_nonneg_int(summary.get("model_total"), "summary.model_total")
    _expect_nonneg_int(summary.get("consolidated_model_total"), "summary.consolidated_model_total")
    _expect_nonneg_int(summary.get("rebind_to_published"), "summary.rebind_to_published")
    _expect_nonneg_int(summary.get("existing_fabric_reuse"), "summary.existing_fabric_reuse")
    _expect_nonneg_int(summary.get("consolidated_members"), "summary.consolidated_members")
    _expect_nonneg_int(summary.get("convert_in_place"), "summary.convert_in_place")
    _expect_number(summary.get("strong_cut"), "summary.strong_cut")
    _expect_str(summary.get("headline"), "summary.headline")

    by_action = _expect_dict(summary.get("by_action"), "summary.by_action")
    by_binding = _expect_dict(summary.get("by_binding_status"), "summary.by_binding_status")
    for action in ACTIONS:
        _expect_nonneg_int(by_action.get(action), f"summary.by_action.{action}")
    for status in BINDING_STATUSES:
        _expect_nonneg_int(by_binding.get(status), f"summary.by_binding_status.{status}")

    source_map = _expect_list(root.get("source_map"), "source_map")
    source_ids: set[str] = set()
    for i, item in enumerate(source_map):
        m = _expect_dict(item, f"source_map[{i}]")
        sid = _expect_str(m.get("source_id"), f"source_map[{i}].source_id", allow_empty=False)
        _expect_str(m.get("workbook_luid"), f"source_map[{i}].workbook_luid")
        source_ids.add(sid)

    _expect_list(root.get("clusters"), "clusters")

    models = _expect_dict(root.get("models"), "models")
    for model_id, payload in models.items():
        _expect_str(model_id, f"models[{model_id!r}].key", allow_empty=False)
        m = _expect_dict(payload, f"models[{model_id}]")
        mid = _expect_str(m.get("model_id"), f"models[{model_id}].model_id", allow_empty=False)
        _expect(mid == model_id, f"models[{model_id}].model_id", "must equal models key")
        origin = _expect_str(m.get("origin"), f"models[{model_id}].origin", allow_empty=False)
        _expect(origin in MODEL_ORIGINS, f"models[{model_id}].origin",
                "must be one of %s" % ", ".join(MODEL_ORIGINS))
        _expect_optional_str(m.get("resolved_model_name"), f"models[{model_id}].resolved_model_name")
        _expect_optional_str(m.get("model_path"), f"models[{model_id}].model_path")
        if origin == "existing_fabric":
            conn = _expect_dict(m.get("connection"), f"models[{model_id}].connection")
            _expect(conn.get("kind") == "byConnection", f"models[{model_id}].connection.kind",
                    "must be 'byConnection'")
            _expect_str(conn.get("workspace_id"), f"models[{model_id}].connection.workspace_id", allow_empty=False)
            _expect_str(conn.get("semantic_model_id"),
                        f"models[{model_id}].connection.semantic_model_id", allow_empty=False)
            _expect_str(conn.get("dataset_name"), f"models[{model_id}].connection.dataset_name", allow_empty=False)
            _validate_date_table(conn.get("date_table"), f"models[{model_id}].connection.date_table")

    entries = _expect_list(root.get("plan"), "plan")
    _expect(len(entries) == summary["embedded_total"], "summary.embedded_total",
            "must equal number of plan entries")
    _expect(sum(by_action[a] for a in ACTIONS) == len(entries), "summary.by_action",
            "counts must sum to number of plan entries")
    _expect(sum(by_binding[b] for b in BINDING_STATUSES) == len(entries), "summary.by_binding_status",
            "counts must sum to number of plan entries")

    for i, raw in enumerate(entries):
        path = f"plan[{i}]"
        entry = _expect_dict(raw, path)
        for k in _PLAN_REQUIRED:
            _expect(k in entry, f"{path}.{k}", "missing required field")
        _expect_str(entry.get("workbook_luid"), f"{path}.workbook_luid")
        source_ref = _expect_str(entry.get("source_ref"), f"{path}.source_ref")
        if source_ids:
            _expect(source_ref in source_ids, f"{path}.source_ref",
                    "must exist in source_map.source_id")
        action = _expect_str(entry.get("action"), f"{path}.action", allow_empty=False)
        _expect(action in ACTIONS, f"{path}.action",
                "must be one of %s" % ", ".join(ACTIONS))
        _expect_str(entry.get("model_id"), f"{path}.model_id", allow_empty=False)
        _expect_str(entry.get("label"), f"{path}.label")
        _validate_binding_target(entry, models, path)
        _validate_evidence(entry.get("evidence"), f"{path}.evidence")

        caveats = _expect_list(entry.get("caveats"), f"{path}.caveats")
        for j, caveat in enumerate(caveats):
            _expect_str(caveat, f"{path}.caveats[{j}]")

        if "drift" in entry:
            drift = _expect_dict(entry.get("drift"), f"{path}.drift")
            for key in ("table_count", "column_count", "calc_count"):
                _expect_nonneg_int(drift.get(key), f"{path}.drift.{key}")
        if "objects" in entry:
            objs = _expect_list(entry.get("objects"), f"{path}.objects")
            for j, obj in enumerate(objs):
                o = _expect_dict(obj, f"{path}.objects[{j}]")
                if "name" in o:
                    _expect_str(o.get("name"), f"{path}.objects[{j}].name")
                if "kind" in o:
                    _expect_str(o.get("kind"), f"{path}.objects[{j}].kind")
    return plan


def load_rebind_plan(path: str) -> Dict[str, Any]:
    """Load and validate a rebind-plan JSON file (fail-loud on malformed/invalid input)."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            payload = json.load(fh)
    except JSONDecodeError as exc:
        raise RebindPlanSchemaError(
            f"{path}: malformed JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})"
        ) from exc
    return validate_rebind_plan(payload)


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
        "date_table": None,   # optional contract slot; enriched later by the Fabric-inventory owner
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


def _row_label(row: Dict[str, Any]) -> str:
    """The migrate_datasource(datasource=label) selector: caption-preferred display name.

    Prefers the inventory-captured ``label`` (caption | formatted-name | raw internal name, mirroring
    the migration skill's ``_datasource_label``); falls back to the datasource display name / id so
    synthetic rows that only set ``datasource_name`` still resolve.
    """
    return (row.get("label") or row.get("datasource_name")
            or row.get("datasource_id") or "")


def _drift(row: Dict[str, Any]) -> Dict[str, int]:
    """The optional drift fingerprint: ``{table_count, column_count, calc_count}``.

    A cheap structural signature the orchestrator re-extracts at resolve time and WARNs on mismatch;
    consumers degrade gracefully when it is absent. ``calc_count`` counts the workbook-local objects
    (calcs / sets / groups / bins / LODs).
    """
    tables = {s.get("table") for s in (row.get("sources") or []) if s.get("table")}
    return {
        "table_count": len(tables),
        "column_count": len(row.get("fields") or []),
        "calc_count": len(row.get("objects") or []),
    }


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
                "label": _row_label(row),
                "drift": _drift(row),
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
    payload = {
        "schema_version": SCHEMA_VERSION,
        "summary": summary,
        "source_map": [{"source_id": sid, "workbook_luid": luid}
                       for sid, luid in source_map.items()],
        "clusters": cluster_result.get("clusters", []),
        "models": models,
        "plan": plan,
    }
    return validate_rebind_plan(payload)


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
    # Reserve the optional contract slot on every real (bound) target; absent == null. For
    # rebind_to_published / existing_fabric it is enriched later from the Fabric inventory, and for
    # rebuilt / consolidated models the calc-compiler writes it back -- the emitter only reserves it.
    if target.get("kind") in ("byPath", "byConnection"):
        target.setdefault("date_table", None)

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
    validate_rebind_plan(plan)
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
        e["binding_target"] = {"kind": "byPath", "model_id": e["model_id"],
                               "model_path": None, "date_table": None}
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
    return validate_rebind_plan(plan)


def _norm_obj(name: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _index_feedback(report: Dict[str, Any]) -> Dict[str, List[str]]:
    """Normalise a view-dependency report into ``{key: [dropped ref names]}``."""
    if not isinstance(report, dict):
        raise RebindPlanSchemaError("view_dependency_report: expected object")
    out: Dict[str, List[str]] = {}

    def collect(key, payload):
        if not isinstance(payload, dict):
            raise RebindPlanSchemaError("view_dependency_report[%r]: expected object" % key)
        dropped = []
        raw = payload.get("dropped") or []
        if not isinstance(raw, list):
            raise RebindPlanSchemaError("view_dependency_report[%r].dropped: expected array" % key)
        for d in raw:
            dropped.append(d.get("name") if isinstance(d, dict) else d)
        out[str(key)] = [d for d in dropped if d]

    if isinstance(report.get("bindings"), list):
        for b in report["bindings"]:
            if not isinstance(b, dict):
                raise RebindPlanSchemaError("view_dependency_report.bindings[]: expected object")
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
    validate_rebind_plan(plan)
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
    ("Label", "label"),
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
    validate_rebind_plan(plan)
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
