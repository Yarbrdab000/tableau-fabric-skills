#!/usr/bin/env python3
"""Artifact importance: how much does each datasource *matter* to the business?

Migration **priority** (``priority.py``) answers *"what do we rebuild, and in what order?"* from a
single signal -- attached workbook count. **Importance** answers a richer, deliverable-facing
question: *"if this datasource broke (or we retired it), how big is the blast radius, and how much is
it actually used?"* It fuses the connected-assets + telemetry layer that ``tableau_inventory.py``
attaches to each ``usage`` block:

  * **reach** -- how many workbooks and dashboards depend on it (the assets that break if it moves);
  * **consumption** -- the total **view count** across those workbooks (real, observed usage, not
    just existence);
  * **endorsement** -- whether the datasource is **certified** (officially blessed content);

and surfaces supporting context (an active data-quality warning, the last extract refresh) as
human-readable drivers. The result is a per-datasource ``importance`` block and an estate rollup the
report and the executive export use to spotlight the assets a migration team must not get wrong.

**Pure, offline, additive, read-only** -- it only reads the ``usage`` block and never changes a
``tier`` / ``score`` / ``bucket`` / ``priority``. Original work.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

IMPORTANCE_ORDER = ["Critical", "High", "Moderate", "Low", "Unknown"]

# Blend weights for the three independent value signals. Reach and consumption dominate (they are the
# real "does this matter" evidence); certification is a smaller, categorical endorsement boost.
W_REACH = 0.45
W_CONSUMPTION = 0.40
W_ENDORSEMENT = 0.15

# Saturating half-saturation constants: the value at which a signal contributes ~0.5. Deliberately
# modest so a handful of workbooks / a few hundred views already register as meaningful.
REACH_HALF = 8.0       # workbooks + 2*dashboards
VIEWS_HALF = 500.0     # total views across downstream workbooks

# Level thresholds on the 0..1 blended score.
_LEVEL_BANDS: List[Tuple[str, float]] = [
    ("Critical", 0.62),
    ("High", 0.38),
    ("Moderate", 0.17),
    ("Low", 0.0),
]


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):  # guard: bool is an int subclass
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _saturate(x: float, half: float) -> float:
    """``x / (x + half)`` -- a 0..1 saturating curve (0 at 0, 0.5 at ``half``, →1 for large x)."""
    if x <= 0:
        return 0.0
    return x / (x + half)


def _human_count(n: float) -> str:
    n = int(n)
    if n >= 1000:
        return f"{n/1000:.1f}k".replace(".0k", "k")
    return str(n)


def importance_for(usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return ``{level, score, drivers[]}`` for one datasource's ``usage`` block.

    ``level`` is ``Unknown`` only when there is **no** usage evidence at all (no counts, no views,
    no certification flag) -- it is never guessed. Otherwise a 0..1 ``score`` bands into
    ``Critical / High / Moderate / Low`` and ``drivers`` explains, in plain language, what makes it
    important.
    """
    if not usage:
        return {"level": "Unknown", "score": 0.0, "drivers": []}

    workbooks = _num(usage.get("workbook_count"))
    dashboards = _num(usage.get("dashboard_count"))
    views = _num(usage.get("view_count"))
    certified = usage.get("certified")

    have_reach = workbooks is not None or dashboards is not None
    have_views = views is not None
    have_endorsement = certified is not None
    if not (have_reach or have_views or have_endorsement):
        return {"level": "Unknown", "score": 0.0, "drivers": []}

    reach_raw = (workbooks or 0.0) + 2.0 * (dashboards or 0.0)
    reach = _saturate(reach_raw, REACH_HALF) if have_reach else None
    consumption = _saturate(views or 0.0, VIEWS_HALF) if have_views else None
    endorsement = (1.0 if certified else 0.0) if have_endorsement else None

    # Blend only the signals we actually have, renormalising the weights so a missing signal does not
    # silently drag the score toward zero.
    parts: List[Tuple[float, float]] = []
    if reach is not None:
        parts.append((W_REACH, reach))
    if consumption is not None:
        parts.append((W_CONSUMPTION, consumption))
    if endorsement is not None:
        parts.append((W_ENDORSEMENT, endorsement))
    wsum = sum(w for w, _ in parts) or 1.0
    score = round(sum(w * v for w, v in parts) / wsum, 4)

    drivers: List[str] = []
    if workbooks:
        drivers.append(f"{_human_count(workbooks)} connected workbook(s)")
    if dashboards:
        drivers.append(f"{_human_count(dashboards)} dashboard(s)")
    if views:
        drivers.append(f"{_human_count(views)} view(s)")
    if certified:
        drivers.append("certified")
    if usage.get("has_quality_warning"):
        drivers.append("active data-quality warning")
    if usage.get("extract_last_refresh"):
        drivers.append(f"last refreshed {str(usage.get('extract_last_refresh'))[:10]}")

    level = "Low"
    for name, lo in _LEVEL_BANDS:
        if score >= lo:
            level = name
            break
    return {"level": level, "score": score, "drivers": drivers}


def annotate(result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach ``importance`` to every match and a ``summary.importance`` rollup. Additive & idempotent.

    Reads each match's ``usage`` block. Never changes a tier / score / bucket / priority. Safe when
    usage is absent (everything becomes ``Unknown``).
    """
    by_level: Dict[str, int] = {k: 0 for k in IMPORTANCE_ORDER}
    total_views = 0
    have_any_views = False
    certified_count = 0
    warning_count = 0

    for m in result.get("matches", []) or []:
        usage = m.get("usage") or {}
        imp = importance_for(usage)
        m["importance"] = imp
        by_level[imp["level"]] = by_level.get(imp["level"], 0) + 1
        v = _num(usage.get("view_count"))
        if v is not None:
            total_views += int(v)
            have_any_views = True
        if usage.get("certified"):
            certified_count += 1
        if usage.get("has_quality_warning"):
            warning_count += 1

    result.setdefault("summary", {})["importance"] = {
        "by_level": by_level,
        "critical": by_level.get("Critical", 0),
        "high": by_level.get("High", 0),
        "total_views": total_views if have_any_views else None,
        "certified_datasources": certified_count,
        "datasources_with_quality_warning": warning_count,
    }
    return result


def importance_worklist(result: Dict[str, Any], *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Matches ordered most-important first (by importance score). Convenience for rendering."""
    rows = list(result.get("matches", []) or [])
    rows.sort(key=lambda m: (m.get("importance") or {}).get("score") or 0.0, reverse=True)
    return rows[:limit] if limit else rows
