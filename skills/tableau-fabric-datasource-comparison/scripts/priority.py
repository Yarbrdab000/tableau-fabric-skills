#!/usr/bin/env python3
"""Migration-priority signal: rank *which* datasources to migrate first by downstream impact.

"Does it already exist in Fabric?" (``compare.py``) and "how much does it matter?" are different
questions. A datasource that powers 40 dashboards is a very different migration line item from one
with **zero or one** attached workbook -- the latter is a deprioritize / retire candidate even if it
needs a full rebuild. This module turns a datasource's downstream **usage** (attached workbooks, and
the sheets / dashboards built on it -- the "other assets") into a priority label, then fuses that
with the comparison verdict so the report can answer *"what do we rebuild, and in what order?"*.

Usage is gathered by ``tableau_inventory.py`` (Tableau **Metadata API** as the trusted primary
source -- in a real migration effort the assets that matter are catalogued -- with a thin REST
fallback for any datasource Catalog has not indexed yet) and rides along on each datasource as a
``usage`` block. This module is **pure and offline**: it only reads that block. Original work.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# Workbook-count thresholds for the usage label. The user's rule -- "0 or 1 attached workbook is
# deprioritized" -- is encoded by the Unused (0) and Low (1) labels both mapping to a deprioritize
# migration priority below. Overridable by the caller.
DEFAULT_USAGE_THRESHOLDS: Dict[str, int] = {"high": 5, "medium": 2}

USAGE_ORDER = ["High", "Medium", "Low", "Unused", "Unknown"]

# Combined migration priority -- the actionable ranking. Reuse never needs migration; otherwise the
# order is driven by downstream usage so the busy datasources rebuild first and the orphans last.
MIGRATION_PRIORITY_ORDER = [
    "P1 - migrate first",
    "P2 - migrate",
    "P3 - deprioritize",
    "P4 - retire candidate",
    "Reuse (already in Fabric)",
    "Unprioritized",
]

_USAGE_TO_MIGRATION = {
    "High": "P1 - migrate first",
    "Medium": "P2 - migrate",
    "Low": "P3 - deprioritize",
    "Unused": "P4 - retire candidate",
    "Unknown": "Unprioritized",
}


def usage_priority(
    usage: Optional[Dict[str, Any]], thresholds: Optional[Dict[str, int]] = None
) -> str:
    """Map a datasource's ``usage`` block to ``High / Medium / Low / Unused / Unknown``.

    ``Unknown`` when usage was not gathered (no count available) -- never guessed. ``Unused`` is 0
    attached workbooks and ``Low`` is exactly 1; both are "deprioritize" per the rule that a
    datasource with 0-1 workbooks is not worth migrating eagerly.
    """
    thresholds = thresholds or DEFAULT_USAGE_THRESHOLDS
    if not usage:
        return "Unknown"
    wc = usage.get("workbook_count")
    if wc is None:
        return "Unknown"
    try:
        wc = int(wc)
    except (TypeError, ValueError):
        return "Unknown"
    if wc >= thresholds.get("high", 5):
        return "High"
    if wc >= thresholds.get("medium", 2):
        return "Medium"
    if wc == 1:
        return "Low"
    return "Unused"


def migration_priority(bucket: Optional[str], usage_label: str) -> str:
    """Fuse the comparison bucket with the usage label into the actionable migration priority.

    A datasource that already exists in Fabric never needs migrating, whatever its usage. Everything
    that must be rebuilt or reconciled is ordered by downstream impact.
    """
    if bucket == "already_exists":
        return "Reuse (already in Fabric)"
    return _USAGE_TO_MIGRATION.get(usage_label, "Unprioritized")


def annotate(
    result: Dict[str, Any], *, thresholds: Optional[Dict[str, int]] = None
) -> Dict[str, Any]:
    """Add ``priority`` + ``migration_priority`` to every match and roll up the counts. Additive.

    Reads each match's ``usage`` block (placed there by ``compare_inventories`` from the Tableau
    inventory). Mutates ``result`` in place (and returns it). Safe when usage is absent -- everything
    becomes ``Unknown`` / ``Unprioritized`` and the deterministic verdict is untouched.
    """
    by_priority: Dict[str, int] = {k: 0 for k in USAGE_ORDER}
    by_migration: Dict[str, int] = {k: 0 for k in MIGRATION_PRIORITY_ORDER}

    for m in result.get("matches", []):
        label = usage_priority(m.get("usage"), thresholds)
        mp = migration_priority(m.get("bucket"), label)
        m["priority"] = label
        m["migration_priority"] = mp
        by_priority[label] = by_priority.get(label, 0) + 1
        by_migration[mp] = by_migration.get(mp, 0) + 1

    summary = result.setdefault("summary", {})
    summary["by_priority"] = by_priority
    summary["by_migration_priority"] = by_migration
    summary["usage_thresholds"] = dict(thresholds or DEFAULT_USAGE_THRESHOLDS)
    return result


def rebuild_worklist(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The rebuild/partial datasources, ordered by migration priority then score. Convenience."""
    out = [
        m for m in result.get("matches", [])
        if m.get("bucket") in ("rebuild", "partial")
    ]
    order = {p: i for i, p in enumerate(MIGRATION_PRIORITY_ORDER)}
    out.sort(key=lambda m: (order.get(m.get("migration_priority"), 99), -(m.get("score") or 0.0)))
    return out
