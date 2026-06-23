#!/usr/bin/env python3
"""Confidence synthesis: how sure are we of each verdict?

The deterministic engine (``compare.py``) emits a ``tier`` / ``score`` / ``bucket`` per datasource.
That answers *"what is the best match?"* but not *"how much should the customer trust this line in
the migration plan?"* Two datasources can both land **Strong / already-in-Fabric** with very
different trustworthiness:

  * one wins its model by a mile, on matching name **and** columns **and** physical source, and the
    model picks it back (mutual best) -- nothing else comes close;
  * the other squeaks over the line on name alone, ties with a runner-up, and the model is also
    claimed by a second datasource.

This module fuses the independent evidence already computed elsewhere into a single, explainable
**confidence** per match -- and, crucially, does it for *both* sides of the decision: high confidence
means *"confidently reuse"* on an already-in-Fabric verdict and *"confidently rebuild"* on a
needs-rebuild verdict (nothing in Fabric comes close). It is **deterministic, additive, and
read-only over the report**: it never changes a tier / score / bucket. Original work.

Evidence fused (each an *independent* corroborator, so agreement compounds confidence):
  * **score level** -- how strong the absolute match is (the band the verdict sits in);
  * **margin** over the runner-up -- is the winner decisively ahead, or a coin-flip near-tie?
  * **signal corroboration** -- how many of name / column overlap / physical source *independently*
    support it (a blend that leans on one signal is weaker than three signals agreeing);
  * **reciprocity** -- is this a *mutual* best match (the model's strongest Tableau suitor is this
    very datasource)? Reciprocal top-1 pairs are a classic high-precision confirmation;
  * **empirical verification** -- if ``--verify`` ran, did the data agree on the overlap window?
    A ``verified`` lifts confidence; a ``mismatch`` caps it at Low regardless of structure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

# Tunables (deliberately conservative). A near-tie is a runner-up within this score of the winner;
# a rebuild verdict this close *below* the partial threshold is "borderline" (we might be wrongly
# rejecting a real partial match).
NEAR_TIE_DELTA = 0.10
BORDERLINE_DELTA = 0.07

# Signal thresholds for counting an *independent* corroborator. Column overlap runs naturally lower
# than name similarity (Fabric models add measures/among columns), so its bar is lower.
NAME_STRONG = 0.85
NAME_NEAR = 0.60
COLUMN_STRONG = 0.60
COLUMN_MODERATE = 0.35
SOURCE_STRONG = 0.50


def _band_min(bands: Optional[Sequence[Sequence[Any]]], tier: str, default: float) -> float:
    for row in bands or []:
        if row and row[0] == tier:
            try:
                return float(row[1])
            except (TypeError, ValueError):
                return default
    return default


def _runner_up_score(match: Dict[str, Any]) -> float:
    """Score of the second-best candidate (0 when there is no runner-up)."""
    cands = match.get("candidates") or []
    if len(cands) >= 2 and isinstance(cands[1], dict):
        try:
            return float(cands[1].get("score") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def compute_reciprocity(matches: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """Map ``fabric_id`` -> the ``tableau_name`` of its strongest suitor.

    A match is *reciprocal* (mutual best) when the datasource's best Fabric model picks that same
    datasource back as its highest-scoring claimant. We only retain each datasource's *best*
    candidate in the report, so reciprocity is computed over "datasources whose top-1 is this model"
    -- exactly the reciprocal-top-1 relation, and the same population the 1:1 assignment resolves.
    """
    best_suitor: Dict[str, Tuple[float, str]] = {}
    for m in matches:
        bm = m.get("best_match")
        if not bm:
            continue
        fid = bm.get("fabric_id") or bm.get("fabric_name")
        if fid is None:
            continue
        try:
            sc = float(m.get("score") or 0.0)
        except (TypeError, ValueError):
            sc = 0.0
        cur = best_suitor.get(fid)
        if cur is None or sc > cur[0]:
            best_suitor[fid] = (sc, m.get("tableau_name"))
    return {fid: name for fid, (_, name) in best_suitor.items()}


def _suitor_counts(matches: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Map ``fabric_id`` -> how many datasources picked it as their best match. A count >= 2 means the
    model was genuinely *contested*, which is what makes winning it (reciprocity) a real signal."""
    counts: Dict[str, int] = {}
    for m in matches:
        bm = m.get("best_match")
        if not bm:
            continue
        fid = bm.get("fabric_id") or bm.get("fabric_name")
        if fid is None:
            continue
        counts[fid] = counts.get(fid, 0) + 1
    return counts


def match_confidence(
    match: Dict[str, Any],
    *,
    reciprocal: bool,
    bands: Optional[Sequence[Sequence[Any]]] = None,
) -> Dict[str, Any]:
    """Return ``{level, drivers[], cautions[], margin, corroborating_signals, reciprocal_best}``.

    ``level`` is confidence in the **verdict** (``bucket``): for already-in-Fabric it measures how
    sure we are the match is real; for needs-rebuild it measures how sure we are nothing in Fabric
    fits. Pure -- depends only on the match dict (plus any ``verification`` already attached).
    """
    bucket = match.get("bucket")
    try:
        score = float(match.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    best = match.get("best_match") or {}
    signals = best.get("signals") or {}

    def _sig(key: str) -> float:
        try:
            v = signals.get(key)
            return float(v) if isinstance(v, (int, float)) else 0.0
        except (TypeError, ValueError):
            return 0.0

    name_s = _sig("name")
    col_s = _sig("column")
    src_s = _sig("source")
    source_compared = bool(match.get("source_compared"))
    contested = bool(match.get("contested"))
    verdict = (match.get("verification") or {}).get("verdict")

    runner = _runner_up_score(match)
    has_runner = len(match.get("candidates") or []) >= 2
    margin = round(max(0.0, score - runner), 3)
    # A near-tie only matters when we're *claiming a match* (already_exists / partial). For a rebuild
    # the relevant question is the absolute score, not the gap to an equally-poor runner-up.
    is_match_bucket = bucket in ("already_exists", "partial")
    near_tie = is_match_bucket and has_runner and (score - runner) < NEAR_TIE_DELTA

    drivers: List[str] = []
    cautions: List[str] = []
    strong = 0  # count of independent corroborators

    if name_s >= NAME_STRONG:
        strong += 1
        drivers.append("name matches exactly")
    elif name_s >= NAME_NEAR:
        drivers.append("names are similar")
    if col_s >= COLUMN_STRONG:
        strong += 1
        drivers.append("strong column overlap")
    elif col_s >= COLUMN_MODERATE:
        drivers.append("partial column overlap")
    if source_compared and src_s >= SOURCE_STRONG:
        strong += 1
        drivers.append("shared physical source")
    if reciprocal:
        strong += 1
        drivers.append("mutual best match")
    if verdict in ("verified", "compatible"):
        strong += 1
        drivers.append("data agrees on the overlap window")

    if near_tie:
        cautions.append("near tie with the runner-up (margin %.2f)" % margin)
    if contested and is_match_bucket:
        cautions.append("model also claimed by another datasource")
    if bucket in ("already_exists", "partial") and not source_compared:
        cautions.append("physical source was obscured")
    if verdict == "mismatch":
        cautions.append("data disagreed on the overlap window")
    if bucket in ("already_exists", "partial") and strong <= 1 and verdict not in ("verified", "compatible"):
        cautions.append("rests on a single signal")

    level = _decide_level(
        bucket=bucket, score=score, strong=strong, near_tie=near_tie,
        contested=contested, verdict=verdict, best=best, bands=bands,
        drivers=drivers, cautions=cautions,
    )

    return {
        "level": level,
        "drivers": drivers,
        "cautions": cautions,
        "margin": margin,
        "corroborating_signals": strong,
        "reciprocal_best": bool(reciprocal),
    }


def _decide_level(
    *, bucket, score, strong, near_tie, contested, verdict, best, bands, drivers, cautions
) -> str:
    """The deterministic level rule, factored out for clarity. Confidence in the *verdict*."""
    if bucket == "already_exists":
        if verdict == "mismatch":
            return "Low"
        if verdict in ("verified", "compatible"):
            return "High"  # empirical agreement trumps structural caution
        if strong >= 2 and not near_tie and not contested:
            return "High"
        if strong >= 1:
            return "Medium"
        return "Low"
    if bucket == "partial":
        # Partial is an inherently uncertain middle; only the data can lift it to High.
        if verdict == "mismatch":
            return "Low"
        if verdict in ("verified", "compatible"):
            return "High"
        if strong >= 1 and not near_tie:
            return "Medium"
        return "Low"
    # rebuild: confidence that *nothing in Fabric fits* -> we really do need to rebuild.
    partial_min = _band_min(bands, "Partial", 0.40)
    weak_min = _band_min(bands, "Weak", 0.15)
    if not best or score <= weak_min:
        drivers.append("no comparable Fabric model")
        return "High"
    if score >= partial_min - BORDERLINE_DELTA:
        cautions.append("borderline -- just below the partial-match threshold")
        return "Low"
    return "Medium"


def annotate_confidence(result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach ``confidence`` to every match and a ``summary.confidence`` rollup. Additive & idempotent.

    Safe to call twice: once inside ``compare_inventories`` (structural confidence) and again after
    ``--verify`` attaches verification verdicts (which then fold into the level). Never mutates a
    tier / score / bucket.
    """
    matches = result.get("matches", []) or []
    summary = result.get("summary", {}) or {}
    bands = summary.get("bands")

    reciprocity = compute_reciprocity(matches)
    suitor_counts = _suitor_counts(matches)
    levels = {"High": 0, "Medium": 0, "Low": 0}
    high_already = 0
    low_review = 0
    for m in matches:
        bm = m.get("best_match")
        reciprocal = False
        if bm:
            fid = bm.get("fabric_id") or bm.get("fabric_name")
            # Reciprocity is only a *corroborator* when the model had genuine competition (>= 2
            # suitors) and this datasource won it. A model with a single suitor is trivially
            # "mutual best" and tells us nothing -- counting it would inflate every clean 1:1 estate.
            reciprocal = (
                reciprocity.get(fid) == m.get("tableau_name")
                and suitor_counts.get(fid, 0) >= 2
            )
        conf = match_confidence(m, reciprocal=reciprocal, bands=bands)
        m["confidence"] = conf
        lvl = conf["level"]
        levels[lvl] = levels.get(lvl, 0) + 1
        if lvl == "High" and m.get("bucket") == "already_exists":
            high_already += 1
        if lvl == "Low" and m.get("bucket") in ("already_exists", "partial"):
            low_review += 1

    result.setdefault("summary", {})["confidence"] = {
        "high": levels["High"],
        "medium": levels["Medium"],
        "low": levels["Low"],
        "high_confidence_already_exists": high_already,
        "low_confidence_review": low_review,
    }
    return result
