"""Deterministic **remediation worklist** -- a structured, machine-readable, per-visual audit of every
fidelity gap the deterministic engine already knows about (offline, stdlib-only, additive).

The engine (``twb_to_pbir``) emits two by-products alongside the PBIR parts: a flat ``warnings`` list
(prose "manual attention required: ..." strings) and ``candidate_records`` (an additive per-visual
descriptor carrying the emitted/deferred *facts* -- colour scales, filters, data labels, ...). Those
are the ground truth of what the deterministic rebuild did and did NOT reproduce, but they are shaped
for humans and for the calc/viz seams, not for a downstream *fidelity* consumer.

``build_worklist`` folds both into ONE structured worklist that:

* **covers the ENTIRE dashboard** -- every rebuilt visual is enumerated (``status`` ``ok`` or
  ``needs_attention``), not just the ones that warned, so an assisted pass can audit all of it;
* is a **superset of the warnings** -- every warning becomes an item, PLUS every ``deferred`` fact
  that never surfaced as a prose warning, PLUS *advisory* items for things that emitted but are a
  disclosed approximation (a default palette, a low-confidence chart type, a fallback shape);
* attaches a deterministic **category + severity + remediation hint** to each item so a gate can
  rank them and an agent has a concrete task list.

This module is a PRODUCER only: it reads the engine's by-products and never touches the emitted PBIR,
so the deterministic default path stays byte-identical. It is the shared interface the monotonic
fidelity gate and the LLM-assisted audit tier both consume.

Contract (warn-never-wrong): an unrecognised warning or fact is NEVER dropped and NEVER silently
down-ranked into invisibility -- it lands as a ``category='other'``, ``severity='medium'`` item so it
still shows up as actionable. The classifier only ever *refines* the default, never hides work.

Provenance: original work. The category / severity taxonomy grounds only on this repo's own warning
phrasings and candidate-record fact shapes; no third-party migration tool was consulted.
"""
from __future__ import annotations

import json

WORKLIST_VERSION = 1
WORKLIST_KIND = "tableau-fabric-remediation-worklist"

# Severity ordering (highest first). ``blocking`` = the visual is unusable / not emitted; ``high`` =
# a data / binding gap that changes what is shown; ``medium`` = a visible fidelity approximation
# whose DATA is still correct; ``low`` = styling / cosmetic not carried, or an advisory refinement.
SEVERITY_RANK = {"blocking": 0, "high": 1, "medium": 2, "low": 3}
_DEFAULT_SEVERITY = "medium"
_DEFAULT_CATEGORY = "other"

# The additive fact keys on a visual candidate record that carry a ``{"status": emitted|deferred}``
# descriptor. A ``deferred`` fact is open remediation work even when no prose warning was emitted.
_FACT_KEYS = (
    "conditional_format", "visual_calc", "mark_colors", "chart_continuous_fill",
    "measure_colors", "data_labels", "legend",
)

# Ordered warning-reason classifier: the FIRST rule whose any-needle matches the (prefix-stripped)
# reason wins. Needles are stable, low-ambiguity fragments of the engine's own warning phrasings.
# Order matters -- more specific / higher-severity rules come first.
_WARNING_RULES = (
    (("no visual emitted", "zone left empty", "no visual was emitted"), "unsupported_visual", "blocking"),
    (("nothing to rebuild", "empty worksheet"), "empty_worksheet", "blocking"),
    (("no usable field bindings",), "no_field_bindings", "blocking"),
    (("bound by caption fallback",), "field_binding", "high"),
    (("has no model binding", "could not resolve field", "unsupported derivation"),
     "field_binding", "high"),
    (("not rebuilt as a slicer",), "parameter_control", "high"),
    (("not mapped to a slicer", "unsupported filter class"), "filter", "high"),
    (("keep-flag",), "filter", "high"),
    (("small multiples",), "small_multiples", "high"),
    (("measure names", "measure values"), "measure_shelf", "high"),
    (("aggregation",), "aggregation", "high"),
    (("default continuous palette",), "color_scale", "medium"),
    (("dynamic title",), "dynamic_title", "medium"),
    (("sheet swap", "field-parameter rebuild", "parameter-driven"), "parameter_swap", "medium"),
    (("left at default",), "filter", "medium"),
    (("grain not applied", "approximated as a plain date"), "date_grain", "medium"),
    (("target/trend", "tier-2 analytics", "target/goal"), "analytics_overlay", "medium"),
)

# Deferred-fact classifier: fact ``kind`` (or the record key) -> (category, severity).
_FACT_RULES = {
    "background_color_scale": ("color_scale", "medium"),
    "conditional_format": ("color_scale", "medium"),
    "chart_continuous_fill": ("color_scale", "medium"),
    "mark_colors": ("categorical_color", "low"),
    "measure_colors": ("measure_color", "low"),
    "measure_series": ("measure_color", "low"),
    "data_labels": ("data_labels", "low"),
    "legend": ("legend", "low"),
    "visual_calculation": ("visual_calc", "high"),
    "visual_calc": ("visual_calc", "high"),
}

# One imperative remediation hint per category (what a fixer / agent should DO).
_REMEDIATION = {
    "unsupported_visual": "Rebuild this visual by hand -- its mark class / geometry is outside the "
                          "deterministic emitter's supported set.",
    "empty_worksheet": "No source fields on any shelf; confirm the worksheet is intentionally empty.",
    "no_field_bindings": "Provide field bindings so the table can be rebuilt with real columns.",
    "field_binding": "Bind the field to the matching model table/column; verify the caption maps to "
                     "a real column name.",
    "parameter_control": "Rebuild the Tableau parameter as a single-select slicer once its target "
                         "column/measure is identified.",
    "filter": "Reproduce the Tableau filter as a visual/page filter or slicer selection.",
    "aggregation": "Recreate the aggregation as a model measure and bind it to the value slot.",
    "measure_shelf": "Reproduce the Measure Names/Values member set (or small multiples).",
    "small_multiples": "Rebuild as small multiples -- one pane per measure.",
    "color_scale": "Apply the source continuous colour scale (palette, range, centre, reverse) to "
                   "the mark fill.",
    "categorical_color": "Apply the per-member colour map to the visual's data points.",
    "measure_color": "Apply the per-measure series colours.",
    "data_labels": "Restore the source data-label visibility and format.",
    "legend": "Restore the source legend position and visibility.",
    "dynamic_title": "Reproduce the dynamic title with a measure-driven title expression.",
    "parameter_swap": "Reproduce the parameter-driven sheet/field swap with a bookmark or field "
                      "parameter.",
    "date_grain": "Bind the date part to the calendar column at the correct grain.",
    "analytics_overlay": "Add the target/goal/trend analytics overlay to the visual.",
    "visual_calc": "Land the visual calculation as a measure / visual calc and project it.",
    "chart_type": "Review the chart type -- a different visual may match the source better.",
    "other": "Review this item against the source and remediate.",
}

_WARN_PREFIX = "manual attention required:"


def _strip_prefix(reason):
    """Remove the standard ``manual attention required:`` prefix (case-insensitive) from a warning
    reason so the classifier keys on the substantive text."""
    s = (reason or "").strip()
    low = s.lower()
    if low.startswith(_WARN_PREFIX):
        return s[len(_WARN_PREFIX):].strip()
    return s


def _classify_warning(reason):
    """Map a (prefix-stripped) warning reason to ``(category, severity)`` via the ordered rule table;
    an unrecognised reason falls back to ``('other', 'medium')`` (never dropped)."""
    low = (reason or "").lower()
    for needles, category, severity in _WARNING_RULES:
        if any(n in low for n in needles):
            return category, severity
    return _DEFAULT_CATEGORY, _DEFAULT_SEVERITY


def _classify_fact(kind):
    """Map a deferred fact's ``kind`` (or record key) to ``(category, severity)``; unknown -> default."""
    return _FACT_RULES.get((kind or "").strip(), (_DEFAULT_CATEGORY, _DEFAULT_SEVERITY))


def _remediation(category):
    return _REMEDIATION.get(category, _REMEDIATION[_DEFAULT_CATEGORY])


def _is_visual_record(rec):
    """A visual candidate record carries a rebuilt-visual id; a parameter-control record does not
    (its open work is already surfaced by a dashboard-scope 'not rebuilt as a slicer' warning)."""
    return isinstance(rec, dict) and bool(rec.get("visual"))


def _advisories(rec):
    """Yield ``(category, severity, reason)`` for things the deterministic tier EMITTED but which are
    a disclosed approximation an assisted pass could still improve -- so the audit covers the whole
    dashboard, not just the deferrals. These never assert a defect; they flag a refinement."""
    out = []
    for key in ("conditional_format", "chart_continuous_fill"):
        fact = rec.get(key)
        if isinstance(fact, dict) and fact.get("status") == "emitted" and fact.get("default_palette"):
            out.append(("color_scale", "low",
                        "colour scale emitted with a disclosed DEFAULT palette (the source "
                        "serialised no explicit stops); verify/refine the colours against the source"))
            break
    conf = rec.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool) and conf < 0.5:
        out.append(("chart_type", "low",
                    "chart-type match confidence is low ({0:.2f}); a different visual may match the "
                    "source better".format(conf)))
    hack = rec.get("hack")
    if hack:
        detail = " ('{0}')".format(hack) if isinstance(hack, str) else ""
        out.append(("chart_type", "low",
                    "chart type used a fallback approximation{0}; review against the source".format(detail)))
    return out


def _item(seq, severity, category, source, scope, rec, reason, facts=None):
    """Build a worklist item bound to a specific visual candidate record."""
    item = {
        "id": "wl-{0:04d}".format(seq),
        "severity": severity,
        "category": category,
        "source": source,
        "scope": scope,
        "visual": rec.get("visual"),
        "worksheet": rec.get("worksheet"),
        "page": rec.get("page"),
        "page_display": rec.get("page_display") or rec.get("page"),
        "visual_type": rec.get("visual_type"),
        "reason": reason,
        "remediation": _remediation(category),
    }
    if facts is not None:
        item["facts"] = facts
    return item


def _item_unattached(seq, severity, category, source, scope, name, reason):
    """Build a worklist item that does not bind to a rebuilt visual (a dashboard-scope warning, or a
    worksheet that produced no visual at all). ``name`` is the warning's scope name."""
    return {
        "id": "wl-{0:04d}".format(seq),
        "severity": severity,
        "category": category,
        "source": source,
        "scope": scope,
        "visual": None,
        "worksheet": name if scope == "worksheet" else None,
        "page": name if scope == "dashboard" else None,
        "page_display": name if scope == "dashboard" else None,
        "visual_type": None,
        "reason": reason,
        "remediation": _remediation(category),
    }


def build_worklist(warnings, candidate_records):
    """Fold the engine's ``warnings`` + ``candidate_records`` into a structured, per-visual
    remediation worklist (see the module docstring). Returns a JSON-serialisable dict; never mutates
    its inputs and never raises on a partial / unexpected record shape (warn-never-wrong).
    """
    records = [r for r in (candidate_records or []) if isinstance(r, dict)]
    visual_records = [r for r in records if _is_visual_record(r)]
    warns = list(warnings or [])

    by_ws = {}
    for rec in visual_records:
        by_ws.setdefault(rec.get("worksheet"), []).append(rec)

    items = []
    # Track the categories already recorded PER visual so an advisory never double-counts a gap a
    # warning / deferred fact already raised for that same visual.
    visual_categories = {}
    seq = 0

    def _note(vid, category):
        if vid is not None:
            visual_categories.setdefault(vid, set()).add(category)

    # 1) Every warning becomes >=1 item (superset guarantee). Worksheet-scope warnings attach to each
    #    matching rebuilt visual; dashboard-scope or unmatched worksheet warnings stay unattached.
    for w in warns:
        reason = _strip_prefix(w.get("reason"))
        scope = (w.get("scope") or "").strip()
        name = w.get("name")
        category, severity = _classify_warning(reason)
        matched = by_ws.get(name) if scope == "worksheet" else None
        if matched:
            for rec in matched:
                items.append(_item(seq, severity, category, "warning", scope, rec, reason))
                _note(rec.get("visual"), category)
                seq += 1
        else:
            items.append(_item_unattached(seq, severity, category, "warning", scope, name, reason))
            seq += 1

    # 2) Every DEFERRED fact becomes an item (may not have surfaced as a prose warning).
    for rec in visual_records:
        for key in _FACT_KEYS:
            fact = rec.get(key)
            if isinstance(fact, dict) and fact.get("status") == "deferred":
                kind = fact.get("kind") or key
                category, severity = _classify_fact(kind)
                reason = fact.get("reason") or "'{0}' deferred by the deterministic tier".format(key)
                items.append(_item(seq, severity, category, "deferred_fact", "worksheet", rec,
                                   reason, facts=fact))
                _note(rec.get("visual"), category)
                seq += 1

    # 3) Advisory items for emitted-but-improvable visuals -- skip a category already raised for the
    #    same visual so the count stays honest.
    for rec in visual_records:
        vid = rec.get("visual")
        for category, severity, reason in _advisories(rec):
            if category in visual_categories.get(vid, ()):
                continue
            items.append(_item(seq, severity, category, "advisory", "worksheet", rec, reason))
            _note(vid, category)
            seq += 1

    items.sort(key=lambda it: (SEVERITY_RANK.get(it["severity"], 99),
                               it.get("page_display") or "", it.get("worksheet") or "",
                               it["id"]))

    # Full-dashboard coverage: one entry per rebuilt visual, flagged ok / needs_attention.
    ids_by_visual = {}
    for it in items:
        if it["visual"]:
            ids_by_visual.setdefault(it["visual"], []).append(it["id"])
    visuals = []
    for rec in visual_records:
        vid = rec.get("visual")
        ids = ids_by_visual.get(vid, [])
        visuals.append({
            "visual": vid,
            "worksheet": rec.get("worksheet"),
            "page": rec.get("page"),
            "page_display": rec.get("page_display") or rec.get("page"),
            "visual_type": rec.get("visual_type"),
            "confidence": rec.get("confidence"),
            "status": "needs_attention" if ids else "ok",
            "item_ids": ids,
        })

    by_severity = {}
    by_category = {}
    for it in items:
        by_severity[it["severity"]] = by_severity.get(it["severity"], 0) + 1
        by_category[it["category"]] = by_category.get(it["category"], 0) + 1
    flagged = sum(1 for v in visuals if v["status"] == "needs_attention")
    unattached = sum(1 for it in items if not it["visual"])

    return {
        "version": WORKLIST_VERSION,
        "kind": WORKLIST_KIND,
        "summary": {
            "visuals_total": len(visuals),
            "visuals_flagged": flagged,
            "visuals_clean": len(visuals) - flagged,
            "items_total": len(items),
            "unattached_items": unattached,
            "by_severity": by_severity,
            "by_category": by_category,
        },
        "visuals": visuals,
        "items": items,
    }


# -- command-line entry point --------------------------------------------------
# Offline: reads a .twb (running the deterministic engine to produce warnings + candidate_records),
# or an existing migration-result JSON ({"warnings": [...], "candidate_records": [...]}), and writes
# the worklist JSON. Never touches an emitted PBIR; purely a read-and-summarise pass.
def _load_from_twb(path):
    try:
        from twb_to_pbir import migrate_twb_to_pbir
    except ImportError as exc:  # pragma: no cover - requires scripts dir on path
        raise SystemExit("cannot import twb_to_pbir (add the scripts dir to sys.path): {0}".format(exc))
    with open(path, "r", encoding="utf-8-sig") as fh:
        xml_text = fh.read()
    res = migrate_twb_to_pbir(xml_text)
    return res.get("warnings", []), res.get("candidate_records", [])


def main(argv=None):
    """CLI: ``remediation_worklist <input.twb|result.json> [-o worklist.json]``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="remediation_worklist",
        description="Emit a deterministic per-visual remediation worklist for a Tableau->PBIR migration.")
    parser.add_argument("input", help="a .twb workbook, or a migration-result JSON "
                                       "({\"warnings\":[...],\"candidate_records\":[...]}).")
    parser.add_argument("-o", "--out", help="write the worklist JSON here; default prints to stdout.")
    args = parser.parse_args(argv)

    low = args.input.lower()
    if low.endswith(".json"):
        with open(args.input, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        warnings = data.get("warnings", [])
        records = data.get("candidate_records", [])
    else:
        warnings, records = _load_from_twb(args.input)

    worklist = build_worklist(warnings, records)
    text = json.dumps(worklist, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        s = worklist["summary"]
        print("wrote worklist: {0} item(s) across {1} visual(s) ({2} flagged) -> {3}".format(
            s["items_total"], s["visuals_total"], s["visuals_flagged"], args.out))
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
