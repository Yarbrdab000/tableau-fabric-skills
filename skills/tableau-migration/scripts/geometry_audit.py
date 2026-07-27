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


def geometry_defects(visuals, page_w, page_h):
    """The per-page defect catalogue, composite-group-aware.

    Returns a ``collections.Counter`` with keys ``overlaps`` / ``contain`` / ``oob`` / ``floor``
    (missing keys read as 0)."""
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
