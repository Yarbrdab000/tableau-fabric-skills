#!/usr/bin/env python3
"""Tier-1 adjudication: the LLM-optional "second matcher" for the comparison engine.

``compare.py`` is the deterministic **Tier 0** matcher: it scores name / column / type / source
overlap and emits the authoritative verdict. Like every structural matcher it is *blind to
semantic equivalence* -- two assets can be the same dataset with **renamed columns** (a Lakehouse
that snake-cases or re-friendlies the source), a **renamed asset**, or a coincidental overlap of
**generic column names** (``Date`` / ``Region`` / ``Sales``) that look identical but describe
different data. Those are exactly the cases a regex never resolves and a human/LLM resolves easily.

This module is the **router + apply path** for an agent-as-second-matcher, modelled on the
``tableau-migration`` skill's *second compiler*:

  * :func:`build_adjudication` -- deterministic. Walks the Tier-0 result, classifies every
    datasource that is *not* confidently matched into one uncertainty **category**, and emits a
    structured, additive ``report["adjudication"]`` handoff: the Tableau side's typed columns and
    physical sources plus the top-K Fabric candidates (each enriched with its own columns / tables /
    sources) and a per-category ``category_guidance`` string. Everything the agent needs to make a
    semantic judgement *without* re-deriving anything.
  * :func:`apply_adjudication` -- folds the agent's verdicts back in as **advisory annotations**.
    It attaches an ``agent_review`` to each reviewed match and produces an ``adjudicated_summary``
    rollup, but it **never** mutates the deterministic ``tier`` / ``score`` / ``bucket``. A default
    run adds zero agent verdicts; nothing is reclassified silently.

Pure and offline -- **no network**. Standard library only. Original work; see
``resources/llm-adjudication.md`` for the agent's operating contract.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Sequence

try:  # package or flat-script execution
    from . import compare as _compare
except ImportError:  # pragma: no cover - exercised via flat script execution
    import compare as _compare


# --------------------------------------------------------------------------------------
# Routing thresholds (all overridable by the caller)
# --------------------------------------------------------------------------------------
# Two candidates are a "near tie" when their scores sit within this gap -- the deterministic
# matcher cannot confidently pick a winner, so a human/LLM should disambiguate.
NEAR_TIE_EPSILON = 0.07
# A candidate must score at least this high to be a genuine contender for the near-tie test.
NEAR_TIE_FLOOR = 0.40
# Signal thresholds for the "renamed" detector: when name and column overlap disagree strongly,
# the asset was probably renamed (asset name moved) or its columns were (lakehouse rebrand).
NAME_HIGH = 0.50
NAME_LOW = 0.20
COLUMN_HIGH = 0.50
COLUMN_LOW = 0.20

# Tiers in the "danger zone" where a real semantic match is most likely to be under-scored.
_BORDERLINE_TIERS = {"Partial", "Weak"}
_OBSCURED_TIERS = {"Strong", "Partial", "Weak"}
_REBUILD_TIERS = {"Weak", "None"}

# Per-category playbook the agent reads (mirrors the second-compiler taxonomy idea, for *matching*).
CATEGORY_GUIDANCE: Dict[str, str] = {
    "near_tie": (
        "Two or more Fabric models scored within a hair of each other, so the structural matcher "
        "cannot confidently pick the counterpart. Compare the candidates' columns and business "
        "meaning and choose the true match (or declare there is none)."
    ),
    "renamed_columns_suspected": (
        "The asset names and the column sets disagree: either the names line up but the columns do "
        "not (columns were renamed -- a Lakehouse that snake-cases or re-friendlies the source), or "
        "the columns line up but the names do not (the asset was renamed). Decide whether the "
        "candidate is the same dataset under different labels by matching columns semantically "
        "(e.g. cust_id == CustomerKey, rev == Sales Amount)."
    ),
    "obscured_source": (
        "The physical source was obscured on one side (a composite / DirectQuery model, a Lakehouse "
        "mirror, or a referenced datasource), so the verdict rests only on name + column + type. "
        "Confirm the candidate really is the same data -- and watch for a coincidental overlap of "
        "generic column names (Date / Region / Sales) that look identical but describe different "
        "data."
    ),
    "borderline_band": (
        "The best score lands in the Partial/Weak band -- the zone where a genuine match is most "
        "often under-scored because Fabric remodelled the data (added measures, split a star schema, "
        "renamed fields). Judge whether this is really a partial overlap or actually a strong match "
        "the structure missed."
    ),
    "likely_rebuild": (
        "Nothing in Fabric overlaps structurally, so Tier 0 calls this a rebuild. Sanity-check the "
        "top candidates one last time for a semantic match the column/name comparison could not see "
        "before sending the datasource to the tableau-migration skill."
    ),
}

# Map an agent verdict (and common synonyms) to a rollup bucket.
_VERDICT_BUCKET = {
    "match": "already_exists",
    "already-exists": "already_exists",
    "already_exists": "already_exists",
    "reuse": "already_exists",
    "partial": "partial",
    "reconcile": "partial",
    "no-match": "rebuild",
    "no_match": "rebuild",
    "none": "rebuild",
    "rebuild": "rebuild",
}


# --------------------------------------------------------------------------------------
# Detail extraction (tolerant of partial inventories)
# --------------------------------------------------------------------------------------
def _typed_columns(obj: Dict[str, Any], key: str) -> List[Dict[str, str]]:
    """``obj[key] = [{name, dataType|type}]`` -> ``[{name, type}]`` (blanks dropped)."""
    out: List[Dict[str, str]] = []
    for f in obj.get(key, []) or []:
        name = f.get("name")
        if not name:
            continue
        out.append({"name": name, "type": (f.get("dataType") or f.get("type") or "")})
    return out


def _sources(obj: Dict[str, Any]) -> List[Dict[str, str]]:
    """Normalise a side's physical sources to ``[{connector, database, table}]``."""
    out: List[Dict[str, str]] = []
    for s in obj.get("sources", []) or []:
        conn = _compare.canonical_connector(s.get("connectionType") or s.get("connector"))
        out.append({
            "connector": conn,
            "database": s.get("database") or "",
            "table": s.get("table") or "",
        })
    return out


def _index_fabric(fabric: Sequence[Dict[str, Any]]):
    by_id: Dict[str, Dict[str, Any]] = {}
    by_name: Dict[str, Dict[str, Any]] = {}
    for m in fabric or []:
        if m.get("id"):
            by_id[str(m["id"])] = m
        if m.get("name"):
            by_name.setdefault(str(m["name"]), m)
    return by_id, by_name


# --------------------------------------------------------------------------------------
# The deterministic router
# --------------------------------------------------------------------------------------
def _classify(match: Dict[str, Any]) -> Optional[str]:
    """Return the single highest-priority uncertainty category for a match, or None if confident."""
    best = match.get("best_match")
    candidates = match.get("candidates") or []
    tier = match.get("tier")

    # 1) near tie -- two real contenders too close to separate structurally
    if len(candidates) >= 2:
        c0, c1 = candidates[0].get("score") or 0.0, candidates[1].get("score") or 0.0
        if c0 >= NEAR_TIE_FLOOR and (c0 - c1) < NEAR_TIE_EPSILON:
            return "near_tie"

    if best:
        sig = best.get("signals") or {}
        name = sig.get("name") or 0.0
        column = sig.get("column") or 0.0
        # 2) renamed columns / renamed asset -- name and column overlap disagree strongly
        if tier != "None":
            if (name >= NAME_HIGH and column <= COLUMN_LOW) or (
                column >= COLUMN_HIGH and name <= NAME_LOW
            ):
                return "renamed_columns_suspected"
        # 3) obscured source -- verdict rests on name+column only; confirm it is not a false match
        if tier in _OBSCURED_TIERS and best.get("source_compared") is False:
            return "obscured_source"

    # 4) general under-match danger zone
    if tier in _BORDERLINE_TIERS and (match.get("score") or 0.0) > 0:
        return "borderline_band"

    # 5) low-priority final sanity check on rebuilds that still had *some* candidate
    if tier in _REBUILD_TIERS and best:
        return "likely_rebuild"

    return None


def _enriched_candidates(
    candidates: Sequence[Dict[str, Any]], by_id, by_name, top_k: int
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in (candidates or [])[:top_k]:
        model = by_id.get(str(c.get("fabric_id"))) or by_name.get(str(c.get("fabric_name"))) or {}
        out.append({
            "fabric_id": c.get("fabric_id"),
            "fabric_name": c.get("fabric_name"),
            "workspace": c.get("workspace"),
            "score": c.get("score"),
            "signals": c.get("signals"),
            "source_compared": c.get("source_compared"),
            "shared_column_count": c.get("shared_column_count"),
            "columns": _typed_columns(model, "columns"),
            "tables": list(model.get("tables") or []),
            "sources": _sources(model),
        })
    return out


def build_adjudication(
    matches: Sequence[Dict[str, Any]],
    tableau: Sequence[Dict[str, Any]],
    fabric: Sequence[Dict[str, Any]],
    *,
    top_k: int = 3,
) -> Dict[str, Any]:
    """Build the additive Tier-1 handoff packet for an agent (the "second matcher").

    Returns ``{"summary": {...}, "needs_review": [...], "requests": [...]}``. ``requests`` carries
    one structured record per datasource that warrants semantic review -- the Tableau columns /
    sources, the deterministic verdict, and the top-K enriched Fabric candidates -- so the agent can
    adjudicate without re-parsing anything. Always present; empty lists when everything is confident.
    """
    by_id, by_name = _index_fabric(fabric)
    tab_by_luid: Dict[str, Dict[str, Any]] = {}
    tab_by_name: Dict[str, Dict[str, Any]] = {}
    for ds in tableau or []:
        if ds.get("luid"):
            tab_by_luid[str(ds["luid"])] = ds
        if ds.get("name"):
            tab_by_name.setdefault(str(ds["name"]), ds)

    needs_review: List[Dict[str, Any]] = []
    requests: List[Dict[str, Any]] = []
    categories: Dict[str, int] = {}
    auto_confident = 0

    for m in matches or []:
        category = _classify(m)
        if category is None:
            auto_confident += 1
            continue
        categories[category] = categories.get(category, 0) + 1

        ds = (
            tab_by_luid.get(str(m.get("tableau_luid")))
            or tab_by_name.get(str(m.get("tableau_name")))
            or {}
        )
        best = m.get("best_match")
        deterministic = {
            "tier": m.get("tier"),
            "score": m.get("score"),
            "bucket": m.get("bucket"),
            "source_compared": bool(m.get("source_compared")),
            "best_fabric_id": best.get("fabric_id") if best else None,
            "best_fabric_name": best.get("fabric_name") if best else None,
        }

        needs_review.append({
            "tableau_name": m.get("tableau_name"),
            "tableau_luid": m.get("tableau_luid"),
            "tier": m.get("tier"),
            "score": m.get("score"),
            "category": category,
            "deterministic_bucket": m.get("bucket"),
        })
        requests.append({
            "tableau_name": m.get("tableau_name"),
            "project": m.get("project"),
            "tableau_luid": m.get("tableau_luid"),
            "category": category,
            "category_guidance": CATEGORY_GUIDANCE.get(category, ""),
            "deterministic": deterministic,
            "tableau_columns": _typed_columns(ds, "fields"),
            "tableau_sources": _sources(ds),
            "candidates": _enriched_candidates(m.get("candidates") or [], by_id, by_name, top_k),
        })

    return {
        "summary": {
            "total_reviewed": len(requests),
            "auto_confident": auto_confident,
            "categories": categories,
        },
        "needs_review": needs_review,
        "requests": requests,
    }


# --------------------------------------------------------------------------------------
# The advisory apply path
# --------------------------------------------------------------------------------------
def _normalize_decisions(decisions: Any) -> List[Dict[str, Any]]:
    """Accept several shapes and return a flat list of review records.

    Tolerant of hostile input: non-dict entries (``None``, a bare string, a stray list) are dropped
    rather than carried through, so the apply path below can safely ``.get`` every record.
    """
    if decisions is None:
        return []
    if isinstance(decisions, dict):
        if "reviews" in decisions and isinstance(decisions["reviews"], list):
            return [r for r in decisions["reviews"] if isinstance(r, dict)]
        # dict keyed by tableau_name / luid -> review
        out = []
        for key, val in decisions.items():
            if isinstance(val, dict):
                rec = dict(val)
                rec.setdefault("tableau_name", key)
                out.append(rec)
        return out
    if isinstance(decisions, list):
        return [r for r in decisions if isinstance(r, dict)]
    return []


def _verdict_bucket(verdict: Optional[str]) -> Optional[str]:
    if not verdict:
        return None
    return _VERDICT_BUCKET.get(str(verdict).strip().lower().replace(" ", "-"))


def apply_adjudication(result: Dict[str, Any], decisions: Any) -> Dict[str, Any]:
    """Fold agent verdicts back in as **advisory** annotations (deterministic verdict untouched).

    ``decisions`` may be a list of review records, a ``{"reviews": [...]}`` wrapper, or a dict keyed
    by ``tableau_name`` / ``tableau_luid``. Each review record is
    ``{tableau_luid?|tableau_name?, verdict, fabric_id?, confidence?, rationale?}`` where ``verdict``
    is ``match`` / ``partial`` / ``no-match`` (synonyms accepted).

    Returns a deep copy of ``result`` with, per reviewed match, an additive ``agent_review`` block
    and an ``adjudicated_bucket``; plus a top-level ``adjudicated_summary`` rollup. The deterministic
    ``tier`` / ``score`` / ``bucket`` and the ``summary`` are never modified.
    """
    out = copy.deepcopy(result)
    reviews = _normalize_decisions(decisions)

    by_luid: Dict[str, Dict[str, Any]] = {}
    by_name: Dict[str, Dict[str, Any]] = {}
    for r in reviews:
        if r.get("tableau_luid"):
            by_luid[str(r["tableau_luid"])] = r
        if r.get("tableau_name"):
            by_name.setdefault(str(r["tableau_name"]), r)

    buckets = {"already_exists": 0, "partial": 0, "rebuild": 0}
    applied = 0
    for m in out.get("matches", []):
        review = by_luid.get(str(m.get("tableau_luid"))) or by_name.get(str(m.get("tableau_name")))
        det_bucket = m.get("bucket", "rebuild")
        if review is None:
            buckets[det_bucket] = buckets.get(det_bucket, 0) + 1
            continue

        adj_bucket = _verdict_bucket(review.get("verdict")) or det_bucket
        best = m.get("best_match") or {}
        fabric_id = review.get("fabric_id")
        if fabric_id is None and adj_bucket != "rebuild":
            fabric_id = best.get("fabric_id")
        m["agent_review"] = {
            "verdict": review.get("verdict"),
            "fabric_id": fabric_id,
            "confidence": review.get("confidence"),
            "rationale": review.get("rationale"),
            "adjudicated_bucket": adj_bucket,
        }
        buckets[adj_bucket] = buckets.get(adj_bucket, 0) + 1
        applied += 1

    det = out.get("summary", {})
    out["adjudicated_summary"] = {
        "reviews_applied": applied,
        "already_exist": buckets["already_exists"],
        "partial": buckets["partial"],
        "rebuild": buckets["rebuild"],
        "delta": {
            "already_exist": buckets["already_exists"] - det.get("already_exist", 0),
            "partial": buckets["partial"] - det.get("partial", 0),
            "rebuild": buckets["rebuild"] - det.get("rebuild", 0),
        },
    }
    adj = out.setdefault("adjudication", {})
    if isinstance(adj, dict):
        adj["applied"] = True
        adj["decisions_count"] = len(reviews)
    return out
