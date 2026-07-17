"""Monotonic **fidelity gate** -- the safety net that makes opting into the LLM-assisted tier
provably **>= the deterministic tier, per visual, no matter what** (offline, stdlib-only, additive).

The assisted tier proposes a *replacement* PBIR visual object for a given source worksheet (a richer
chart type, a tuned colour ramp, added data labels, ...). Left unchecked, an LLM edit could just as
easily make a visual WORSE than the deterministic rebuild. This gate removes that risk: it scores the
deterministic baseline and the assisted candidate on the SAME axes and keeps the assisted candidate
**only when it regresses nothing**; otherwise it reverts to the deterministic baseline. Because a
revert returns the exact deterministic object, the chosen visual is `>=` deterministic on every scored
component by construction -- a hard guarantee, not a heuristic.

Two scoring surfaces are combined so the guarantee covers BOTH what the deterministic engine gets right
and what the assisted tier is allowed to touch:

* **structural** (reused from :mod:`fidelity_oracle` ``_score_pair``): visual type, field set, field
  roles, and canvas position -- the load-bearing "is this the same chart of the same data" axes. These
  are read fail-open; if the oracle cannot be imported the gate still runs on the feature axes alone
  (and records that structural scoring was unavailable).
* **feature** (:func:`visual_feature_components`, computed here): the visible-fidelity aspects the
  assisted/worklist tier actually changes -- continuous colour fill richness, per-point colour, data
  labels, legend, title -- read straight off the emitted PBIR visual object. Scoring these is what lets
  the gate *credit* a genuine colour/label improvement (an improved component -> kept) while still
  *reverting* one that strips a colour ramp or a legend (a regressed component -> reverted). Without
  them a colour-only change would read as a structural tie and the guarantee would not extend to colour.

Design notes:

* The decision core (:func:`decide`) is a **pure function of two score dicts** -- no I/O, no oracle, no
  engine -- so the monotonic policy is trivially unit-testable and identical in every caller.
* The gate **never mutates** its inputs; it selects and returns one of the two candidate objects.
* An ``epsilon`` absorbs float jitter so a true tie is never mis-read as a regression.
* Conservative by charter: a *mixed* change (improves A, regresses B) is REVERTED, not landed -- the
  user's requirement is "better no matter what", so we never trade one axis's gain for another's loss.
  A net-positive change must be re-proposed as a pure (non-regressing) improvement to land.

This module is a SELECTOR only: it chooses between two already-built objects and emits a decision
record. It never authors visuals and never touches the deterministic default path, which stays
byte-identical. It is the gate the LLM-assisted audit tier lands its ``--approved-viz`` candidates
through.

Provenance: original work. The monotonic policy and the feature taxonomy ground only on this repo's
own emitted PBIR object shapes (``objects.dataPoint`` / ``objects.labels`` / ``objects.legend`` /
``visualContainerObjects.title``, nested ``FillRule`` gradients); no third-party migration tool was
consulted.
"""
from __future__ import annotations

import json

GATE_VERSION = 1
GATE_KIND = "tableau-fabric-monotonic-fidelity-gate"

# Float slack so a genuine tie is never read as a regression (or an improvement).
DEFAULT_EPSILON = 1e-4

# ---------------------------------------------------------------------------
# Structural scorer -- reuse the fidelity oracle's per-visual pair scorer fail-open. Importing the
# oracle is offline/stdlib-only; if it is unavailable for any reason the gate degrades to feature-only
# scoring rather than failing (and says so in each decision record via ``structural_scored``).
# ---------------------------------------------------------------------------
try:  # pragma: no cover - import shape depends on how the package is loaded
    from fidelity_oracle import _score_pair as _ORACLE_SCORE_PAIR
except Exception:  # pragma: no cover
    try:
        from .fidelity_oracle import _score_pair as _ORACLE_SCORE_PAIR  # type: ignore
    except Exception:
        _ORACLE_SCORE_PAIR = None


def _default_structural_scorer(twb_ws, pbir_visual, zone, relationships=None):
    """Adapter over :func:`fidelity_oracle._score_pair`; returns its result dict or ``None`` when the
    oracle is unavailable or the pair cannot be scored. Never raises."""
    if _ORACLE_SCORE_PAIR is None or twb_ws is None or pbir_visual is None:
        return None
    try:
        return _ORACLE_SCORE_PAIR(twb_ws, pbir_visual, zone, relationships=relationships)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Feature scoring -- deterministic 0..1 fidelity-feature scores read off the emitted PBIR visual.
# ---------------------------------------------------------------------------
# Continuous-colour richness grades by the richest gradient the visual carries anywhere (a diverging
# 3-stop ramp is a fuller reproduction than a 2-stop, which beats a flat solid, which beats none).
_FILL_GRADE = (
    ("linearGradient3", 1.0),
    ("linearGradient2", 0.7),
    ("linearGradient", 0.7),
)
_FILLRULE_PRESENT = 0.4  # a FillRule with no recognised gradient token (e.g. a solid conditional fill)


def _deep_keys(obj):
    """Yield every mapping key that appears anywhere in a nested dict/list structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _deep_keys(v)
    elif isinstance(obj, (list, tuple)):
        for it in obj:
            yield from _deep_keys(it)


def _fillrule_richness(visual):
    """0..1 richness of the richest colour FillRule anywhere in the visual (0.0 when none)."""
    keys = set(_deep_keys(visual or {}))
    for token, grade in _FILL_GRADE:
        if token in keys:
            return grade
    if "solid" in keys and "FillRule" in keys:
        return _FILLRULE_PRESENT
    if "FillRule" in keys:
        return _FILLRULE_PRESENT
    return 0.0


def _has_object(visual, name):
    """True when the visual carries a non-empty ``objects[name]`` or ``visualContainerObjects[name]``
    entry -- the two places PBIR hangs per-visual formatting objects."""
    if not isinstance(visual, dict):
        return False
    for bag_key in ("objects", "visualContainerObjects"):
        bag = visual.get(bag_key)
        if isinstance(bag, dict) and bag.get(name):
            return True
    return False


# The visible-fidelity features the assisted/worklist tier may change. Each maps to a 0..1 score so the
# combined overall stays a clean average; the per-component monotonic check is what enforces "no strip".
def visual_feature_components(visual):
    """Deterministic 0..1 fidelity-feature scores for a single emitted PBIR visual object.

    Covers exactly the aspects an assisted pass is allowed to touch, so the gate can both credit a real
    improvement and revert a regression on each: continuous ``color_fill`` richness, presence of a
    per-point ``data_point`` colour object, ``data_labels``, ``legend``, and a ``title``.
    """
    visual = visual if isinstance(visual, dict) else {}
    return {
        "color_fill": _fillrule_richness(visual),
        "data_point": 1.0 if _has_object(visual, "dataPoint") else 0.0,
        "data_labels": 1.0 if _has_object(visual, "labels") else 0.0,
        "legend": 1.0 if _has_object(visual, "legend") else 0.0,
        "title": 1.0 if _has_object(visual, "title") else 0.0,
    }


def combined_score(twb_ws, visual, zone=None, relationships=None, structural_scorer=_default_structural_scorer):
    """Score one (source worksheet, emitted visual) pair on both surfaces into a single component dict.

    Structural components (from the fidelity oracle) are namespaced ``struct_*``; feature components are
    namespaced ``feat_*`` so the two never collide. ``score`` is the unweighted mean of all present
    components -- a reporting/tiebreak number; the monotonic decision is driven per-component, so the
    overall's exact weighting is not load-bearing. ``structural_scored`` records whether the oracle
    contributed (False when it was unavailable, so callers can see the guarantee ran feature-only).
    """
    components = {}
    structural_scored = False
    if structural_scorer is not None and twb_ws is not None and visual is not None:
        sp = structural_scorer(twb_ws, visual, zone, relationships=relationships)
        if isinstance(sp, dict):
            for k, v in (sp.get("components") or {}).items():
                try:
                    components["struct_" + k] = float(v)
                    structural_scored = True
                except (TypeError, ValueError):
                    continue
    for k, v in visual_feature_components(visual).items():
        components["feat_" + k] = float(v)
    overall = sum(components.values()) / len(components) if components else 0.0
    return {"score": round(overall, 6), "components": components, "structural_scored": structural_scored}


# ---------------------------------------------------------------------------
# The monotonic decision -- a pure function of two score dicts.
# ---------------------------------------------------------------------------
def decide(before, after, epsilon=DEFAULT_EPSILON):
    """Decide whether to keep the assisted candidate (``after``) over the deterministic baseline
    (``before``). Both are ``{"score": float, "components": {name: float}}`` dicts.

    Keep the assisted candidate **only when it regresses nothing** and does not lower the overall:

    * no shared component drops by more than ``epsilon`` (``regressed_components`` empty), AND
    * no component the baseline scored is missing from the candidate (``dropped_components`` empty -- a
      dropped scored axis is treated as a regression), AND
    * the overall score does not fall by more than ``epsilon``.

    Otherwise revert to the deterministic baseline. Returns a decision record; never mutates inputs.
    """
    b_comp = (before or {}).get("components") or {}
    a_comp = (after or {}).get("components") or {}
    shared = set(b_comp) & set(a_comp)
    regressed = sorted(c for c in shared if a_comp[c] < b_comp[c] - epsilon)
    improved = sorted(c for c in shared if a_comp[c] > b_comp[c] + epsilon)
    dropped = sorted(set(b_comp) - set(a_comp))  # scored by baseline, absent from candidate
    added = sorted(set(a_comp) - set(b_comp))    # new axis the candidate introduced
    b_score = float((before or {}).get("score", 0.0))
    a_score = float((after or {}).get("score", 0.0))
    overall_ok = a_score >= b_score - epsilon
    kept = (not regressed) and (not dropped) and overall_ok

    if kept and improved:
        reason = "assisted improves {0} with no measured regression -> kept".format(", ".join(improved))
    elif kept:
        reason = ("assisted ties the deterministic baseline on every measured component -> kept "
                  "(permits an unmeasured gain without risking a measured loss)")
    elif regressed:
        reason = "assisted regresses {0} -> reverted to deterministic".format(", ".join(regressed))
    elif dropped:
        reason = "assisted drops scored component(s) {0} -> reverted to deterministic".format(", ".join(dropped))
    else:
        reason = "assisted lowers the overall score -> reverted to deterministic"

    return {
        "chosen": "assisted" if kept else "deterministic",
        "kept_assisted": kept,
        "score_before": round(b_score, 4),
        "score_after": round(a_score, 4),
        "score_delta": round(a_score - b_score, 4),
        "regressed_components": regressed,
        "dropped_components": dropped,
        "improved_components": improved,
        "added_components": added,
        "reason": reason,
    }


def gate_visual(twb_ws, deterministic_visual, assisted_visual, zone=None,
                relationships=None, structural_scorer=_default_structural_scorer,
                epsilon=DEFAULT_EPSILON):
    """Gate ONE assisted visual against its deterministic baseline for the same source worksheet.

    Returns ``(chosen_visual, decision)`` where ``chosen_visual`` is the assisted object when it
    regresses nothing, else the deterministic object (returned by identity -- never a copy, never
    mutated). ``decision`` carries the before/after component dicts and the monotonic verdict.
    """
    before = combined_score(twb_ws, deterministic_visual, zone, relationships, structural_scorer)
    after = combined_score(twb_ws, assisted_visual, zone, relationships, structural_scorer)
    decision = decide(before, after, epsilon=epsilon)
    decision["worksheet"] = (twb_ws or {}).get("name") if isinstance(twb_ws, dict) else None
    _named = assisted_visual if isinstance(assisted_visual, dict) else deterministic_visual
    decision["visual"] = _named.get("name") if isinstance(_named, dict) else None
    decision["structural_scored"] = bool(before.get("structural_scored") and after.get("structural_scored"))
    decision["components_before"] = before["components"]
    decision["components_after"] = after["components"]
    chosen = assisted_visual if decision["kept_assisted"] else deterministic_visual
    return chosen, decision


def gate_changes(pairs, structural_scorer=_default_structural_scorer, epsilon=DEFAULT_EPSILON):
    """Gate a batch of assisted proposals.

    ``pairs`` is an iterable of ``(twb_ws, deterministic_visual, assisted_visual, zone)`` tuples (``zone``
    optional -- a 2- or 3-tuple is accepted, missing entries default to ``None``). Returns a structured
    record: a ``summary`` count, the ordered ``decisions``, and the ``visuals`` actually chosen (the
    monotonic-safe set to land). By construction every chosen visual is ``>=`` its deterministic
    baseline on every scored component.
    """
    decisions = []
    chosen_visuals = []
    for row in pairs:
        row = tuple(row)
        twb_ws = row[0] if len(row) > 0 else None
        det_v = row[1] if len(row) > 1 else None
        asst_v = row[2] if len(row) > 2 else None
        zone = row[3] if len(row) > 3 else None
        chosen, decision = gate_visual(twb_ws, det_v, asst_v, zone,
                                       structural_scorer=structural_scorer, epsilon=epsilon)
        decisions.append(decision)
        chosen_visuals.append(chosen)
    kept = sum(1 for d in decisions if d["kept_assisted"])
    return {
        "version": GATE_VERSION,
        "kind": GATE_KIND,
        "summary": {
            "visuals": len(decisions),
            "assisted_kept": kept,
            "reverted": len(decisions) - kept,
        },
        "decisions": decisions,
        "visuals": chosen_visuals,
    }


def verify_monotonic(before, after, decision, epsilon=DEFAULT_EPSILON):
    """Assertion helper: confirm the chosen candidate is ``>=`` the deterministic baseline on every
    scored component (the gate's core guarantee). Returns True/False; used by tests and callers that
    want a runtime check. ``before``/``after`` are the two :func:`combined_score` dicts."""
    b_comp = (before or {}).get("components") or {}
    chosen_comp = (after if decision.get("kept_assisted") else before).get("components") or {}
    for c, bv in b_comp.items():
        if chosen_comp.get(c, bv) < bv - epsilon:
            return False
    return True


def main(argv=None):  # pragma: no cover - thin CLI shell
    """CLI: ``monotonic_gate <pairs.json> [-o decisions.json]``.

    ``pairs.json`` is ``{"pairs": [{"worksheet": {...}, "deterministic": {...}, "assisted": {...},
    "zone": {...}}, ...]}`` where each object is already-parsed (oracle worksheet record + two emitted
    PBIR visual dicts). Emits the :func:`gate_changes` record.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="monotonic_gate",
        description="Gate assisted PBIR visuals so each is >= its deterministic baseline, per visual.")
    parser.add_argument("input", help="a JSON file: {\"pairs\": [{worksheet, deterministic, assisted, zone}]}.")
    parser.add_argument("-o", "--out", help="write the decision record JSON here; default prints to stdout.")
    args = parser.parse_args(argv)

    with open(args.input, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    pairs = []
    for row in data.get("pairs", []):
        pairs.append((row.get("worksheet"), row.get("deterministic"), row.get("assisted"), row.get("zone")))

    record = gate_changes(pairs)
    text = json.dumps(record, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        s = record["summary"]
        print("wrote gate decisions: {0} visual(s), {1} assisted kept, {2} reverted -> {3}".format(
            s["visuals"], s["assisted_kept"], s["reverted"], args.out))
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
