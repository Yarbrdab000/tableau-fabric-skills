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

A SECOND, complementary audit answers the question the defect catalogue cannot -- "did each object
land where TABLEAU put it". ``authored_leaves`` / ``displacement_defects`` / ``displacement_summary``
match authored leaf zones to emitted visuals and rank them by displacement, so a whole CLASS of
infidelity (a discarded padding model, a mis-scaled axis, an unplaced float) shows up as a rising
median for one kind of zone instead of as one visibly wrong dashboard.

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


# ---------------------------------------------------------------------------------------------
# SOURCE-vs-OUTPUT FIDELITY
#
# ``geometry_defects`` above asks "is the emitted page internally well-formed" -- nothing overlaps,
# nothing is squashed, nothing spills. A page can pass all of that and still be WRONG, because it
# says nothing about whether an object landed where TABLEAU put it. The check below closes that gap:
# it matches each authored leaf zone to its emitted visual and measures the displacement, which is
# what surfaces a whole CLASS of infidelity (a dropped padding model, a mis-scaled axis, an ignored
# float) rather than one bad dashboard.
#
# Two exclusions keep the signal honest, both learned by running it across a real estate:
#   * DEGENERATE zones (authored width or height ~0) are persistence slivers, not objects -- they
#     have no meaningful centre, so a "displacement" for them is noise that dominates the ranking.
#   * CONTAINER zones (``empty`` / ``layout-flow`` / ``layout-basic``) are layout scaffolding that
#     Power BI has no equivalent for. They are deliberately NOT emitted, so matching them to the
#     nearest visual compares an object against something it was never meant to be.
# ---------------------------------------------------------------------------------------------

#: Authored zone kinds that are layout scaffolding rather than a drawn object.
CONTAINER_ZONE_KINDS = frozenset({"empty", "layout-flow", "layout-basic", "layout-flow-v2"})

#: An authored zone thinner than this (in 0..100000 source units) is a persistence sliver.
#: A zone smaller than this in either dimension has no meaningful centre to compare. Judged in
#: EMITTED PAGE PIXELS (the space displacement is measured in), not Tableau source units.
DEGENERATE_ZONE_PX = 2.0

#: Source-unit fallback for callers with no page dimensions (0..100000 space).
DEGENERATE_ZONE_UNITS = 2.0

_ZONE_SIDES = ("top", "right", "bottom", "left")

#: Tableau's documented default outer padding, in authored pixels.
_DEFAULT_OUTER_PAD = 4.0


def _zone_pad_excess(zone_el):
    """The ASYMMETRIC part of a zone's outer padding, in authored px, or ``None``.

    Tableau draws an object in the content box left after ``<zone-style><format attr='margin'>``
    (plus per-side ``margin-top`` / ``-right`` / ``-bottom`` / ``-left`` overrides). Only the excess
    over the smallest side actually MOVES the content -- a uniform inset shrinks evenly and leaves
    the centre where it was. Mirrors the emitter's rule so the audit and the emit agree on what
    "where Tableau drew it" means; if they disagreed, this net would penalise a correct page.
    """
    def _local(tag):
        return tag.rsplit("}", 1)[-1]

    style = next((c for c in zone_el if _local(c.tag) == "zone-style"), None)
    if style is None:
        return None
    sides = dict.fromkeys(_ZONE_SIDES, _DEFAULT_OUTER_PAD)
    saw = False
    for fmt in style:
        if _local(fmt.tag) != "format":
            continue
        attr = (fmt.get("attr") or "").strip().lower()
        if not attr.startswith("margin"):
            continue
        try:
            val = float(fmt.get("value"))
        except (TypeError, ValueError):
            continue
        if attr == "margin":
            sides = dict.fromkeys(_ZONE_SIDES, val)
            saw = True
        elif attr[7:] in _ZONE_SIDES:
            sides[attr[7:]] = val
            saw = True
    if not saw:
        return None
    vals = [sides[s] for s in _ZONE_SIDES]
    if max(vals) - min(vals) < 1.0:
        return None
    base = min(vals)
    return tuple(v - base for v in vals)


def authored_leaves(dashboard_el):
    """Leaf zones of a Tableau ``<dashboard>`` element, in page FRACTIONS (0..1).

    Only the dashboard's default ``<zones>`` subtree is read. ``<devicelayouts>`` re-states the same
    zone ids with phone/tablet geometry; folding those in silently compares a desktop visual against
    a phone rect. Each leaf is ``{id, kind, name, fx, fy, fw, fh, pad}``, where ``pad`` is the
    asymmetric outer-padding excess in authored pixels (or ``None``) -- the difference between the
    zone rect and the CONTENT BOX Tableau actually draws in. A zone with child zones is a container
    and is skipped -- only what actually draws is returned.
    """
    def _local(tag):
        return tag.rsplit("}", 1)[-1]

    zones_el = next((c for c in dashboard_el if _local(c.tag) == "zones"), None)
    if zones_el is None:
        return []
    seen, leaves = set(), []
    for z in zones_el.iter():
        if _local(z.tag) != "zone":
            continue
        zid = z.get("id")
        if zid is None or zid in seen:
            continue
        if any(_local(c.tag) == "zone" for c in z):
            continue
        try:
            x, y, w, h = (float(z.get(k)) for k in ("x", "y", "w", "h"))
        except (TypeError, ValueError):
            continue
        seen.add(zid)
        leaves.append({
            "id": zid,
            "kind": (z.get("type-v2") or z.get("type") or "worksheet"),
            "name": z.get("name") or z.get("param") or "",
            "fx": x / 100000.0, "fy": y / 100000.0,
            "fw": w / 100000.0, "fh": h / 100000.0,
            "pad": _zone_pad_excess(z),
            "_units": (w, h),
        })
    return leaves


def content_box(leaf, page_w, page_h):
    """The ``(x, y, w, h)`` page-pixel box Tableau DRAWS the leaf in -- its rect minus asymmetric padding.

    This, not the raw zone rect, is the fidelity target: a 133px icon zone carrying
    ``margin-bottom=85`` draws its icon in a 44px band at the TOP. Measuring against the raw rect
    would score a correctly-placed icon as displaced and a wrongly-placed one as perfect.
    """
    x, y = leaf["fx"] * page_w, leaf["fy"] * page_h
    w, h = leaf["fw"] * page_w, leaf["fh"] * page_h
    pad = leaf.get("pad")
    if not pad:
        return x, y, w, h
    top, right, bottom, left = pad
    nw, nh = w - (left + right), h - (top + bottom)
    if nw < 1.0 or nh < 1.0:
        return x, y, w, h
    return x + left, y + top, nw, nh


def auditable_leaves(leaves, page_w=None, page_h=None):
    """Drop the leaves whose displacement is meaningless (containers + degenerate slivers).

    Degeneracy is judged in EMITTED PAGE PIXELS when ``page_w``/``page_h`` are supplied, because
    that is the space the displacement is measured in: a zone one pixel wide has no meaningful
    centre to compare, however many source units that is. Judging it in Tableau's 0..100000 source
    space instead lets a 1px sliver through (one page pixel is ~73 source units on a 1366px page),
    and such a sliver then matches an arbitrary neighbour and reports a huge fake displacement.
    The source-unit fallback is kept for callers that have no page dimensions to hand.
    """
    out = []
    for z in leaves:
        if z.get("kind") in CONTAINER_ZONE_KINDS:
            continue
        if page_w and page_h:
            if z["fw"] * page_w < DEGENERATE_ZONE_PX or z["fh"] * page_h < DEGENERATE_ZONE_PX:
                continue
        else:
            uw, uh = z.get("_units") or (z["fw"] * 100000.0, z["fh"] * 100000.0)
            if uw < DEGENERATE_ZONE_UNITS or uh < DEGENERATE_ZONE_UNITS:
                continue
        out.append(z)
    return out


def _pair_cost(a, b):
    """Match cost between an authored content box and an emitted rect: centre distance + size gap.

    Size is part of the cost, not just position: a full-width 1346x40 banner and a 273x245 chart are
    not the same object even if their centres happen to sit close together.
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (abs(bx + bw / 2.0 - (ax + aw / 2.0)) + abs(by + bh / 2.0 - (ay + ah / 2.0))
            + abs(bw - aw) + abs(bh - ah))


def displacement_defects(leaves, visuals, page_w, page_h, tolerance=24.0):
    """How far each authored object moved between Tableau and the emitted page.

    ``leaves`` come from :func:`authored_leaves` (page fractions), ``visuals`` are emitted visual
    JSON dicts. Each auditable leaf is matched to one emitted visual and the Manhattan distance
    between the two centres is its displacement -- measured against the leaf's CONTENT BOX
    (:func:`content_box`), which is where Tableau actually draws, not the raw zone rect. Returns a
    list of records ranked worst-first, unmatched leaves ahead of merely displaced ones (an object
    nothing was emitted for is the worse defect); ``displacement > tolerance`` and every unmatched
    leaf are flagged ``defect: True``.

    Matching is EXCLUSIVE and OVERLAP-GATED, which is what makes the numbers trustworthy on a
    dashboard nobody has looked at:

    * A pair is only allowed when the emitted rect actually INTERSECTS the authored content box. An
      emitted visual that does not touch the footprint it was authored in is not that object, so a
      leaf with no intersecting candidate is reported ``matched: False`` rather than being assigned
      the nearest stranger and charged a fabricated displacement. That also turns a genuinely
      DROPPED visual -- an authored object nothing was emitted for -- into its own honest signal
      instead of hiding it inside the displacement median.
    * Every emitted visual is consumed at most once, assigned greedily over the globally sorted
      pair costs, so two authored objects can no longer both claim the same tile.

    It remains a RANKING tool: a rising median for one kind of zone names the class that regressed.
    """
    if page_w <= 0 or page_h <= 0:
        return []
    boxes = [rect(v) for v in visuals]
    audit = auditable_leaves(leaves, page_w, page_h)
    if not boxes or not audit:
        return []
    authored = [content_box(z, page_w, page_h) for z in audit]

    pairs = []
    for li, a in enumerate(authored):
        for vi, b in enumerate(boxes):
            if intersection_area(a, b) <= 0:
                continue
            pairs.append((_pair_cost(a, b), li, vi))
    pairs.sort()
    leaf_of, used = {}, set()
    for _, li, vi in pairs:
        if li in leaf_of or vi in used:
            continue
        leaf_of[li] = vi
        used.add(vi)

    out = []
    for li, z in enumerate(audit):
        ax, ay, aw, ah = authored[li]
        acx, acy = ax + aw / 2.0, ay + ah / 2.0
        vi = leaf_of.get(li)
        if vi is None:
            out.append({
                "id": z["id"], "kind": z["kind"], "name": z["name"],
                "authored": (ax, ay, aw, ah), "emitted": None, "matched": False,
                "dx": None, "dy": None, "displacement": None, "defect": True,
            })
            continue
        bx, by, bw, bh = boxes[vi]
        dx = (bx + bw / 2.0) - acx
        dy = (by + bh / 2.0) - acy
        out.append({
            "id": z["id"], "kind": z["kind"], "name": z["name"],
            "authored": (ax, ay, aw, ah), "emitted": (bx, by, bw, bh), "matched": True,
            "dx": dx, "dy": dy, "displacement": abs(dx) + abs(dy),
            "defect": (abs(dx) + abs(dy)) > tolerance,
        })
    out.sort(key=lambda r: (r["matched"], -(r["displacement"] or 0.0)))
    return out


def displacement_summary(records):
    """Per-zone-kind ``{n, median, p90, max, defects, unmatched}`` roll-up.

    ``unmatched`` counts authored objects with no intersecting emitted visual. They are excluded
    from the distance statistics -- there is no honest distance to report for an object that was
    never emitted -- but they still count as defects, so a dropped visual can never improve a score.
    """
    by_kind = collections.defaultdict(list)
    unmatched = collections.Counter()
    kinds = []
    for r in records:
        if r["kind"] not in by_kind and r["kind"] not in kinds:
            kinds.append(r["kind"])
        if r.get("matched", True):
            by_kind[r["kind"]].append(r["displacement"])
        else:
            unmatched[r["kind"]] += 1
    summary = {}
    for kind in kinds:
        s = sorted(by_kind.get(kind) or [])
        summary[kind] = {
            "n": len(s) + unmatched[kind],
            "median": s[len(s) // 2] if s else None,
            "p90": s[min(int(len(s) * 0.9), len(s) - 1)] if s else None,
            "max": s[-1] if s else None,
            "defects": sum(1 for r in records if r["kind"] == kind and r["defect"]),
            "unmatched": unmatched[kind],
        }
    return summary


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
