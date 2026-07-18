"""Tier-3 **dashboard audit tier** -- the opt-in, whole-dashboard LLM-assisted migration pass that is
provably **>= the deterministic tier on every visual** (offline, stdlib-only, additive).

The deterministic engine rebuilds a report and, alongside it, emits two by-products this tier stands on:
the per-visual **remediation worklist** (:mod:`remediation_worklist` -- full-dashboard coverage + a
severity/priority + a remediation hint for every gap) and the read-only **candidate records** the
:mod:`viz_advisor` turns into per-visual ranked chart *alternatives*. This tier folds them into ONE
audit and closes the loop with the monotonic fidelity gate:

* **PRODUCER (audit the ENTIRE dashboard, not just the easy stuff)** -- :func:`build_dashboard_audit`
  enumerates *every* rebuilt visual (``needs_attention`` AND ``ok``), attaches its priority, its
  remediation items, and its chart alternatives, and orders them highest-priority first so the agent
  is told exactly what needs immediate attention while still being handed the whole surface. Nothing is
  dropped: a visual with no canned alternative (e.g. a detail table) is still listed with its items, and
  worklist items that belong to no single visual (dashboard-scope / parameter controls) ride along in
  ``unattached_items``. :func:`audit_prompt` renders the runbook the out-of-band agent/vision pass runs.

* **GATED LANDING (better no matter what)** -- :func:`land_dashboard_audit` runs every proposed visual
  replacement through :mod:`monotonic_gate`, so an assisted visual is kept ONLY when it regresses no
  scored component vs the deterministic rebuild; otherwise the deterministic visual stands. Because a
  revert returns the exact deterministic object, opting into this tier can only ever match or beat the
  deterministic report, per visual -- the user's hard requirement. The result also reports how much of
  the flagged work the assisted pass actually improved.

Same contract as the rest of the engine (**warn-never-wrong**, offline, deterministic tests inject the
agent's answer): this tier is a PRODUCER + SELECTOR only. It authors no visuals itself and never touches
the deterministic default path, which stays byte-identical whether or not an audit is produced.

Provenance: original work. It composes this repo's own worklist, advisor, and gate; no third-party
migration tool was consulted.
"""
from __future__ import annotations

import json

AUDIT_VERSION = 1
AUDIT_REQUEST_KIND = "tableau-fabric-dashboard-audit-request"
AUDIT_RESULT_KIND = "tableau-fabric-dashboard-audit-result"

# Priority ordering for the audit (highest attention first); mirrors the worklist severity ranks with
# an explicit ``none`` tail for clean visuals so every visual sorts deterministically.
_PRIORITY_RANK = {"blocking": 0, "high": 1, "medium": 2, "low": 3, "none": 4}

# The hard rules the out-of-band audit pass must follow. They encode the two guarantees: full-dashboard
# coverage, and that every proposal is monotonic-gated so it can only land as an improvement.
AUDIT_RULES = (
    "Audit EVERY visual listed, not only the flagged ones -- the whole dashboard is in scope.",
    "Address the highest-priority visuals first (blocking, then high, then medium, then low).",
    "Keep the source field truth: never drop, add, or re-role a field the deterministic rebuild bound.",
    "Choose a chart type only from a visual's listed 'alternatives', or keep its 'current_type'.",
    "Never remove a legend, colour scale, or data label the deterministic rebuild already produced.",
    "Every proposal is monotonic-gated: it lands ONLY if it regresses no measured axis versus the "
    "deterministic rebuild, so aim for genuine improvements -- a regressing change is reverted for you.",
    "For a visual with no listed alternatives (e.g. a detail table), still address its items with "
    "faithful formatting/colour/label refinements; never invent data.",
)

# -- optional peers (fail-open, consistent with the rest of the toolchain) -----------------------------
try:  # pragma: no cover - import shape depends on how the package is loaded
    from viz_advisor import build_report_advice as _build_report_advice
except Exception:  # pragma: no cover
    try:
        from .viz_advisor import build_report_advice as _build_report_advice  # type: ignore
    except Exception:
        _build_report_advice = None

try:  # pragma: no cover
    import monotonic_gate as _gate
except Exception:  # pragma: no cover
    try:
        from . import monotonic_gate as _gate  # type: ignore
    except Exception:
        _gate = None


def _visual_key(worksheet, visual):
    return (worksheet or "", visual or "")


def _priority_of(items):
    """Highest severity across a visual's worklist items -> a single priority label ('none' if clean)."""
    best = "none"
    best_rank = _PRIORITY_RANK["none"]
    for it in items:
        sev = it.get("severity", "medium")
        rank = _PRIORITY_RANK.get(sev, _PRIORITY_RANK["medium"])
        if rank < best_rank:
            best_rank = rank
            best = sev
    return best


def _compact_item(it):
    """A slim, agent-facing view of a worklist item (drops redundant location echoes)."""
    return {
        "id": it.get("id"),
        "severity": it.get("severity"),
        "category": it.get("category"),
        "reason": it.get("reason"),
        "remediation": it.get("remediation"),
        "source": it.get("source"),
    }


def build_dashboard_audit(worklist, candidate_records, intent=None, field_types=None):
    """Build the full-dashboard audit request from the worklist + candidate records (additive, offline).

    Enumerates every rebuilt visual with its ``status``/``priority``/``items`` (from ``worklist``) and
    its ranked chart ``alternatives`` (from :mod:`viz_advisor`), ordered highest-priority first. Items
    that belong to no single visual land in ``unattached_items`` so nothing is dropped. Returns a
    JSON-serialisable request; never mutates its inputs and never touches the emitted PBIR.
    """
    worklist = worklist or {}
    visuals_in = worklist.get("visuals") or []
    items_in = worklist.get("items") or []
    items_by_id = {it.get("id"): it for it in items_in if it.get("id")}

    # Advisor alternatives, indexed by (worksheet, visual). Fail-open: no advisor -> no alternatives,
    # every visual is still audited on its worklist items alone.
    advice_by_key = {}
    if _build_report_advice is not None and candidate_records:
        try:
            report_advice = _build_report_advice(candidate_records, intent=intent, field_types=field_types)
            for a in report_advice.get("advice", []):
                advice_by_key[_visual_key(a.get("worksheet"), a.get("visual"))] = a
        except Exception:  # pragma: no cover - advisory only
            advice_by_key = {}

    audit_visuals = []
    attached_ids = set()
    for v in visuals_in:
        item_ids = v.get("item_ids") or []
        items = [items_by_id[i] for i in item_ids if i in items_by_id]
        attached_ids.update(i for i in item_ids if i in items_by_id)
        adv = advice_by_key.get(_visual_key(v.get("worksheet"), v.get("visual"))) or {}
        entry = {
            "worksheet": v.get("worksheet"),
            "visual": v.get("visual"),
            "page": v.get("page"),
            "current_type": v.get("visual_type"),
            "confidence": v.get("confidence"),
            "status": v.get("status"),
            "priority": _priority_of(items),
            "items": [_compact_item(it) for it in items],
            "advisable": bool(adv.get("advisable")),
        }
        if adv.get("advisable"):
            entry["alternatives"] = adv.get("suggestions", [])
            entry["top_alternative"] = adv.get("top_alternative")
            entry["fields"] = adv.get("fields", [])
        elif adv:
            entry["advice_reason"] = adv.get("reason")
        audit_visuals.append(entry)

    # Highest-attention visuals first; needs_attention before ok at equal priority; stable by name.
    audit_visuals.sort(key=lambda e: (
        _PRIORITY_RANK.get(e["priority"], _PRIORITY_RANK["none"]),
        0 if e["status"] == "needs_attention" else 1,
        e.get("worksheet") or "",
        e.get("visual") or "",
    ))

    unattached_items = [_compact_item(it) for it in items_in if it.get("id") not in attached_ids]

    by_priority = {}
    for e in audit_visuals:
        by_priority[e["priority"]] = by_priority.get(e["priority"], 0) + 1

    bundle = {
        "version": AUDIT_VERSION,
        "kind": AUDIT_REQUEST_KIND,
        "intent": intent,
        "rules": list(AUDIT_RULES),
        "summary": {
            "visuals": len(audit_visuals),
            "needs_attention": sum(1 for e in audit_visuals if e["status"] == "needs_attention"),
            "advisable": sum(1 for e in audit_visuals if e["advisable"]),
            "by_priority": by_priority,
            "unattached_items": len(unattached_items),
        },
        "visuals": audit_visuals,
        "unattached_items": unattached_items,
    }
    return bundle


def audit_prompt(bundle):
    """Render the runbook prompt for the out-of-band, full-dashboard audit pass (no API key, no tool)."""
    lines = [
        "You are the Tableau -> Power BI dashboard auditor (Tier-3). Below is EVERY rebuilt visual of "
        "one dashboard, ordered by how much attention it needs. Audit the whole dashboard: for each "
        "visual, decide whether to keep the deterministic rebuild or propose a faithful improvement.",
        "",
        "Hard rules:",
    ]
    for i, rule in enumerate(bundle.get("rules", []), 1):
        lines.append("  {0}. {1}".format(i, rule))
    intent = bundle.get("intent")
    lines.append("")
    lines.append("Intent: {0!r}".format(intent) if intent else "Intent: (none given)")
    s = bundle.get("summary", {})
    lines.append("")
    lines.append("Dashboard: {0} visual(s), {1} need attention.".format(
        s.get("visuals", 0), s.get("needs_attention", 0)))
    lines.append("")
    for e in bundle.get("visuals", []):
        head = "- [{0}] {1!r} (worksheet {2!r}, type {3})".format(
            e.get("priority"), e.get("visual"), e.get("worksheet"), e.get("current_type"))
        lines.append(head)
        for it in e.get("items", []):
            lines.append("    * ({0}/{1}) {2}".format(
                it.get("severity"), it.get("category"), it.get("reason")))
            if it.get("remediation"):
                lines.append("      -> {0}".format(it["remediation"]))
        if e.get("advisable"):
            alts = [a.get("visual_type") for a in e.get("alternatives", [])]
            if alts:
                lines.append("    alternatives: {0}".format(", ".join(alts)))
        elif e.get("advice_reason"):
            lines.append("    (no chart-type alternatives: {0})".format(e["advice_reason"]))
    unattached = bundle.get("unattached_items", [])
    if unattached:
        lines.append("")
        lines.append("Dashboard-scope items (not tied to one visual):")
        for it in unattached:
            lines.append("  * ({0}/{1}) {2}".format(
                it.get("severity"), it.get("category"), it.get("reason")))
    lines.append("")
    lines.append("Remember: every proposal is monotonic-gated -- it lands only if it does not regress "
                 "the deterministic rebuild, so a keep is always safe and an improvement must be real.")
    return "\n".join(lines)


def land_dashboard_audit(pairs, audit=None, structural_scorer=None, epsilon=None):
    """Gate a batch of proposed per-visual replacements and report the audit outcome (offline, additive).

    ``pairs`` is an iterable of ``(twb_ws, deterministic_visual, assisted_visual, zone)`` tuples (``zone``
    optional). Each proposal is run through :mod:`monotonic_gate`, so the chosen visual is ``>=`` its
    deterministic baseline on every scored component -- opting into the audit can only match or beat the
    deterministic report per visual. When an ``audit`` request bundle is supplied, each decision is
    annotated with that visual's ``priority``/``status`` and the summary reports how many *flagged*
    visuals the assisted pass actually improved. Returns the audit RESULT; never mutates inputs.
    """
    if _gate is None:  # pragma: no cover - defensive
        raise RuntimeError("monotonic_gate is unavailable; cannot land an audit safely")

    kwargs = {}
    if structural_scorer is not None:
        kwargs["structural_scorer"] = structural_scorer
    if epsilon is not None:
        kwargs["epsilon"] = epsilon

    # Priority/status lookup from the audit bundle (if given) so outcomes tie back to the audit.
    ctx_by_key = {}
    if audit:
        for e in audit.get("visuals", []):
            ctx_by_key[_visual_key(e.get("worksheet"), e.get("visual"))] = e

    decisions = []
    chosen_visuals = []
    flagged_total = 0
    flagged_improved = 0
    for row in pairs:
        row = tuple(row)
        twb_ws = row[0] if len(row) > 0 else None
        det_v = row[1] if len(row) > 1 else None
        asst_v = row[2] if len(row) > 2 else None
        zone = row[3] if len(row) > 3 else None
        chosen, decision = _gate.gate_visual(twb_ws, det_v, asst_v, zone, **kwargs)
        ctx = ctx_by_key.get(_visual_key(decision.get("worksheet"), decision.get("visual")))
        if ctx:
            decision["priority"] = ctx.get("priority")
            decision["status"] = ctx.get("status")
            if ctx.get("status") == "needs_attention":
                flagged_total += 1
                if decision["kept_assisted"]:
                    flagged_improved += 1
        decisions.append(decision)
        chosen_visuals.append(chosen)

    kept = sum(1 for d in decisions if d["kept_assisted"])
    summary = {
        "visuals": len(decisions),
        "assisted_kept": kept,
        "reverted": len(decisions) - kept,
        "guarantee": "every chosen visual is >= its deterministic baseline on all scored components",
    }
    if audit:
        summary["flagged_visuals"] = flagged_total
        summary["flagged_improved"] = flagged_improved
    return {
        "version": AUDIT_VERSION,
        "kind": AUDIT_RESULT_KIND,
        "summary": summary,
        "decisions": decisions,
        "visuals": chosen_visuals,
    }


def _load_worklist_and_records(path):
    """Load (worklist, candidate_records) from a ``.twb`` (via the engine) or a migration-result JSON."""
    low = path.lower()
    if low.endswith(".json"):
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        worklist = data.get("worklist")
        records = data.get("candidate_records", [])
        if worklist is None:
            # Rebuild the worklist from warnings + records if only those were provided.
            try:
                from remediation_worklist import build_worklist
            except Exception:  # pragma: no cover
                from .remediation_worklist import build_worklist  # type: ignore
            worklist = build_worklist(data.get("warnings", []), records)
        return worklist, records
    try:
        from twb_to_pbir import migrate_twb_to_pbir
    except Exception:  # pragma: no cover
        from .twb_to_pbir import migrate_twb_to_pbir  # type: ignore
    with open(path, "r", encoding="utf-8-sig") as fh:
        xml_text = fh.read()
    result = migrate_twb_to_pbir(xml_text)
    return result.get("worklist"), result.get("candidate_records", [])


def main(argv=None):  # pragma: no cover - thin CLI shell
    """CLI: ``audit_tier <input.twb|result.json> [-o audit.json] [--intent TEXT] [--prompt]``.

    Builds the full-dashboard audit request. With ``--prompt`` it prints the runbook prompt instead of
    the JSON. (Landing an agent's answer is done in-process via :func:`land_dashboard_audit`.)
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="audit_tier",
        description="Build a full-dashboard, priority-ordered migration audit request for the assisted tier.")
    parser.add_argument("input", help="a .twb workbook, or a migration-result JSON "
                                       "({\"worklist\":{...},\"candidate_records\":[...]}).")
    parser.add_argument("-o", "--out", help="write the audit request JSON here; default prints to stdout.")
    parser.add_argument("--intent", help="optional free-text intent to bias chart-type advice.")
    parser.add_argument("--prompt", action="store_true", help="print the runbook prompt instead of JSON.")
    args = parser.parse_args(argv)

    worklist, records = _load_worklist_and_records(args.input)
    bundle = build_dashboard_audit(worklist, records, intent=args.intent)

    if args.prompt:
        print(audit_prompt(bundle))
        return 0

    text = json.dumps(bundle, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        s = bundle["summary"]
        print("wrote dashboard audit: {0} visual(s), {1} need attention -> {2}".format(
            s["visuals"], s["needs_attention"], args.out))
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
