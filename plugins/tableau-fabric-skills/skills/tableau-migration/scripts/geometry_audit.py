"""Score a migrated Power BI report page for LAYOUT DEFECTS (offline, stdlib-only).

This is the exact per-page defect catalogue the v2.7.0 geometry goldens use, promoted out of
``tests/test_twb_to_pbir.py`` so both the test suite and an out-of-band A/B harness can score a
report from a single implementation. It is a **pure move** -- the thresholds are unchanged:

  * ``TOL = 1.0`` px slack on every edge comparison.
  * A pair of visuals counts as an **overlap** only when the intersection covers **> 2 %** of the
    smaller rect (and the raw intersection is more than 4 px^2 -- a shared edge or corner is not an
    overlap).
  * Full nesting (one rect inside the other within ``TOL``) is classified as **containment**, a
    distinct, usually-worse defect than a partial overlap.
  * A visual whose width OR height is ``<= 41`` px is a **floor** (squash) defect.
  * A rect whose edge falls outside the page (``< -TOL`` on the near edges, past ``page_w`` /
    ``page_h`` on the far edges) is **oob**.

These are METRIC checks, not pixel snapshots: they lock the quality invariants (0 overlaps /
contain / oob on a clean page) while deliberately never freezing x/y coordinates, so a later tidy
pass can improve an arrangement without tripping a test.

COMPOSITE-GROUP CONTRACT (donut safety): a faithful donut becomes two stacked Power BI visuals --
a ``donutChart`` ring plus a ``card`` in the hole -- whose rects overlap BY DESIGN. When the engine
emits such a composite it stamps both pieces with a shared PBIR ``parentGroupName``; an overlap
BETWEEN two same-group members is intentional and never counted, while every cross-group or
ungrouped collision is still a defect.

The module also groups an emitted ``{path: text}`` PBIR part map (or a ``.Report`` directory on
disk) into pages and scores each one, and exposes a small CLI::

    python scripts/geometry_audit.py <report.Report dir> [--json]

Pure, deterministic, stdlib-only. NO emit dependency, NO ``twb_to_pbir`` import: the auditor scores
already-emitted geometry, so it can be re-imported by the test suite and driven by the A/B harness
in isolation.
"""
from __future__ import annotations

import collections
import itertools
import json
import os
import sys

# One pixel of slack on every edge comparison -- absorbs float round-trips through the emit layer.
TOL = 1.0


def rect(vj):
    """The ``(x, y, w, h)`` rectangle of an emitted visual JSON dict."""
    p = vj["position"]
    return (p["x"], p["y"], p.get("width", 0) or 0, p.get("height", 0) or 0)


def intersection_area(a, b):
    """Area of the overlap between two ``(x, y, w, h)`` rects (0 when they only touch)."""
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy


def contains(a, b, tol=TOL):
    """True when rect ``b`` is fully inside rect ``a`` (within ``tol`` on every edge)."""
    return (b[0] >= a[0] - tol and b[1] >= a[1] - tol and
            b[0] + b[2] <= a[0] + a[2] + tol and b[1] + b[3] <= a[1] + a[3] + tol)


def composite_group_of(vj):
    """The composite-group id a visual declares, or ``None``.

    The engine stamps every member of a deliberately-stacked composite (e.g. a donut ring + its
    centre KPI card) with a shared PBIR ``parentGroupName``. An overlap between two members of the
    SAME group is intentional and exempt; everything else is a real collision."""
    g = vj.get("parentGroupName")
    return g if g else None


def geometry_defects(visuals, page_w, page_h, is_background=None):
    """The per-page defect catalogue, composite-group-aware.

    Returns a ``collections.Counter`` with keys ``overlaps`` / ``contain`` / ``oob`` / ``floor``
    (missing keys read as 0).

    ``is_background`` is an optional ``visual -> bool`` predicate. When supplied, a pair is skipped
    in the overlap/containment scan if EITHER member is a background layer -- a full-canvas backdrop
    (e.g. a branded image sent to back) is a z-order layer the content sits on top of BY DESIGN, not
    a colliding tile, exactly as a same-group composite pair is exempt. Default ``None`` preserves
    the v2.7.0 behaviour byte-for-byte (the shipped goldens call this with no predicate); backgrounds
    still contribute to ``oob`` / ``floor`` as before. See ``scripts/layout_layers.py`` for the
    kind-aware classifier that produces such a predicate."""
    d = collections.Counter()
    for v in visuals:
        r = rect(v)
        if (r[0] < -TOL or r[1] < -TOL
                or (page_w and r[0] + r[2] > page_w + TOL)
                or (page_h and r[1] + r[3] > page_h + TOL)):
            d["oob"] += 1
        if r[2] <= 41.0 or r[3] <= 41.0:
            d["floor"] += 1
    for a, b in itertools.combinations(visuals, 2):
        ga, gb = composite_group_of(a), composite_group_of(b)
        if ga is not None and ga == gb:
            continue  # intentional composite (donut ring + centre card): exempt by design
        if is_background is not None and (is_background(a) or is_background(b)):
            continue  # a backdrop layer is z-order background, not a colliding tile
        ra, rb = rect(a), rect(b)
        ia = intersection_area(ra, rb)
        if ia <= 4.0:
            continue
        small = min(ra[2] * ra[3], rb[2] * rb[3]) or 1
        frac = ia / small
        if contains(ra, rb) or contains(rb, ra):
            d["contain"] += 1
        elif frac > 0.02:
            d["overlaps"] += 1
    return d


def pages_from_parts(parts):
    """Group emitted PBIR parts into ``{page_name: {display, w, h, visuals[]}}``.

    ``parts`` is a ``{path: json_text}`` map exactly as ``twb_to_pbir`` emits it (or as the CLI
    reads a ``.Report`` directory). The ``pages.json`` index is skipped."""
    pages = {}
    for path, txt in parts.items():
        norm = path.replace("\\", "/")
        if norm.endswith("/page.json"):
            name = norm.split("/pages/")[1].split("/")[0]
            doc = json.loads(txt)
            pg = pages.setdefault(name, {"display": name, "w": None, "h": None, "visuals": []})
            pg["w"], pg["h"] = doc.get("width"), doc.get("height")
            pg["display"] = doc.get("displayName") or name
        elif norm.endswith("/visual.json"):
            name = norm.split("/pages/")[1].split("/")[0]
            pg = pages.setdefault(name, {"display": name, "w": None, "h": None, "visuals": []})
            pg["visuals"].append(json.loads(txt))
    return pages


def score_report(parts):
    """Score every page of an emitted PBIR part map -> ``{display_name: Counter}``.

    This is the entry point the A/B harness uses to compare two engines from outside the test
    suite. Pages keep their emitted order."""
    return {pg["display"]: geometry_defects(pg["visuals"], pg["w"], pg["h"])
            for pg in pages_from_parts(parts).values()}


def quality_report(parts):
    """Score every page's QUALITY block -> ``{display_name: dict}``.

    Deliberately a SEPARATE entry point rather than extra keys on :func:`score_report`, whose return
    is a ``Counter`` of hard-gated integers that callers sum and compare. Mixing continuous,
    report-only measures into that same mapping would let a float land in a gate that was written to
    read counts."""
    return {pg["display"]: geometry_quality(pg["visuals"], pg["w"], pg["h"])
            for pg in pages_from_parts(parts).values()}


def _visual_kind(vj):
    """Coarse role of an emitted visual: ``chrome`` (slicer / caption / legend / image) or ``content``.

    ``chrome_ratio`` asks how much of a page is spent on things that support the data rather than
    showing it. A page with 16 slicers, 4 caption textboxes and 2 legends has 22 objects competing
    for space with 4 charts, and arranging 22 objects well is strictly harder than needing 8."""
    vt = (((vj.get("visual") or {}).get("visualType")) or "").lower()
    if vt in ("slicer", "advancedslicervisual"):
        return "slicer"
    if vt in ("textbox", "actionbutton", "shape"):
        return "caption"
    if vt == "image":
        return "image"
    return "content"


CHROME_KINDS = ("slicer", "caption", "image", "legend")


def _edges(rects):
    """Every x-edge and y-edge in the page, as two lists."""
    xs, ys = [], []
    for x, y, w, h in rects:
        xs.extend((x, x + w))
        ys.extend((y, y + h))
    return xs, ys


def _misalign(rects, near_lo=2.0, near_hi=12.0):
    """Edge pairs that are CLOSE but not equal -- the signature of a machine-made layout.

    Two panels whose left edges land at 412.67 and 412.71 read as aligned; 412 and 419 read as a
    mistake. Anything under ``near_lo`` is already effectively aligned and anything over ``near_hi``
    is a deliberate offset, so only the band between them is a defect."""
    n = 0
    for axis in _edges(rects):
        vals = sorted(axis)
        for i, a in enumerate(vals):
            for b in vals[i + 1:]:
                d = b - a
                if d > near_hi:
                    break
                if near_lo <= d <= near_hi:
                    n += 1
    return n


def _gutter_cv(rects, band=24.0):
    """Coefficient of variation of the gaps between horizontally adjacent visuals in each row.

    Rhythm is about CONSISTENCY, not size: gaps of 8/8/8 read as designed, 6/17/9 reads as
    accidental, and both have the same mean. Rows are recovered by clustering on y within ``band``
    because the auditor sees a flat rect list, not the tree."""
    rows = {}
    for x, y, w, h in rects:
        rows.setdefault(round(y / band), []).append((x, w))
    gaps = []
    for items in rows.values():
        items.sort()
        for (x0, w0), (x1, _w1) in zip(items, items[1:]):
            g = x1 - (x0 + w0)
            if -1.0 <= g <= 200.0:
                gaps.append(max(0.0, g))
    if len(gaps) < 2:
        return 0.0
    mean = sum(gaps) / len(gaps)
    if mean <= 0.0:
        return 0.0
    var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
    return (var ** 0.5) / mean


def geometry_quality(visuals, page_w, page_h):
    """The per-page QUALITY block -- the second question, reported beside :func:`geometry_defects`.

    The defect counters answer *is this layout broken?* A page can score zero on all four and still
    look like a machine made it: dead bands, edges that are close but not equal, a caption stretched
    to fill the row it happens to sit in. Worse, ``floor`` (``w <= 41 or h <= 41``) mechanically
    IMPROVES when the canvas is inflated -- nothing can be under 41px once the page doubles -- so it
    must never be read without ``area`` beside it, or growing the page looks like progress.

    Returns a plain dict. **Report, do not assert**: these are continuous and comparative, so a
    threshold set before the corpus distribution is known produces flaky goldens that teach everyone
    to ignore them. The four defect counts stay hard-gated at zero; nothing here gates anything."""
    rects = [rect(v) for v in visuals]
    area = float(page_w or 0) * float(page_h or 0)
    ink = sum(w * h for _x, _y, w, h in rects)
    kinds = [_visual_kind(v) for v in visuals]
    chrome = sum(1 for k in kinds if k in CHROME_KINDS)
    # A chart squeezed to 12:1 is unreadable in either direction; captions and slicers are SUPPOSED
    # to be wide and short, so the ratio is only meaningful for content visuals.
    aspect = 0
    for (x, y, w, h), k in zip(rects, kinds):
        if k != "content" or w <= 0 or h <= 0:
            continue
        if max(w / h, h / w) > 8.0:
            aspect += 1
    return {
        "deadspace": round(max(0.0, 1.0 - (ink / area)), 4) if area else 0.0,
        "misalign": _misalign(rects),
        "gutter_cv": round(_gutter_cv(rects), 4),
        "aspect": aspect,
        "object_count": len(visuals),
        "chrome_ratio": round(chrome / len(visuals), 4) if visuals else 0.0,
        "area": round(area, 1),
    }


def parts_from_dir(report_dir):
    """Read a ``.Report`` directory on disk into the ``{relpath: text}`` map ``score_report`` wants."""
    parts = {}
    for root, _dirs, files in os.walk(report_dir):
        for fn in files:
            if fn in ("page.json", "visual.json"):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, report_dir).replace("\\", "/")
                with open(full, encoding="utf-8") as fh:
                    parts[rel] = fh.read()
    return parts


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    if not argv:
        print("usage: python geometry_audit.py <report.Report dir> [--json]", file=sys.stderr)
        return 2
    scored = score_report(parts_from_dir(argv[0]))
    if as_json:
        print(json.dumps({k: dict(v) for k, v in scored.items()}, indent=2, sort_keys=True))
    else:
        for page, d in scored.items():
            print("{}: overlaps={} contain={} oob={} floor={}".format(
                page, d["overlaps"], d["contain"], d["oob"], d["floor"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
