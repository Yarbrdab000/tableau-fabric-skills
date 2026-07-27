"""Solve a Tableau layout tree (from ``zone_tree.parse_zone_tree``) into absolute page-pixel rects.

A two-pass flexbox-style resolution over the nested vstack / hstack / frame / leaf tree:

  1. MIN-SIZE, bottom-up  -- every leaf declares the pixel width/height below which it is unusable
     (a slicer needs its label + control, a matrix needs a header + rows, a textbox needs its line
     box). A flow container's min is the SUM of its children's main-axis contributions plus gaps on
     the main axis, and the MAX of its children on the cross axis. A ``frame`` (absolute host) takes
     the max of its children on both axes.
  2. ALLOCATE, top-down   -- each flow container distributes its rect among its children by fraction,
     with fixed-size children taking their pixels first, then clamps any child that falls below its
     min UP to that min and charges the deficit to the remaining flexible siblings (the CSS flex
     freeze loop: an iterative round that terminates in <= n rounds). A ``frame`` positions its
     children absolutely by their stored source rect, scaled into the frame's allocated box.

Because the tiled children of a flow container receive DISJOINT intervals of the same axis (a
monotonically advancing cursor), flow overlap is unrepresentable by construction -- there is nothing
to detect and nothing to repair. A ``frame`` is the deliberate escape hatch for absolutely-positioned
children (Tableau ``layout-basic``), where overlap is allowed by design; frame descendants are
therefore exempt from the disjointness / min-respect invariants, exactly as the source-overlap
auditor already treats them.

**Page growth closes the overflow case.** The public ``solve`` enlarges the page to the root's min on
either axis before allocating, so a solvable tree always yields rects that are disjoint (flow),
contained, and min-respecting. This extends the layout-solver spec's height-only FitToPage growth to
both axes so containment is a clean, unconditional invariant rather than a height-only one; a wider or
taller page still rescales to the viewport (it does not produce a scrollbar). Growth is bounded by
``MAX_GROWTH``, and whatever the cap leaves unsatisfied is absorbed by ``allocate``'s last-resort
proportional down-scale, so on-page containment holds even when the page may not grow far enough.

**A frame's min accounts for its children's SOURCE FRACTIONS.** A frame hands each child
``child_src / frame_src`` of its own box, so taking the frame's min as ``max(child min)`` understates
it by exactly that fraction: the frame looks satisfied while every child is starved, and a starved
flow child then overruns its own box. Inverting the fraction is what makes the growth above sufficient
rather than merely plausible.

**Fixed-size children are accounted correctly.** ``is-fixed`` zones occupy exactly their ``fixed_px``
on the container's main axis regardless of their subtree min, so a fixed child contributes
``fixed_px`` (not its subtree min) to its parent's main-axis min. Omitting this is what would let a
grown page still overflow -- the same nominal-rect gotcha that makes the legacy scale-then-repair path
manufacture the ATTI header-band overlap.

Pure, deterministic, stdlib-only. Never raises: an unsolvable tree (or any internal error) returns
``None`` so the caller keeps the legacy absolute-rect path. NO emit dependency, NO PBIR knowledge,
does not import ``twb_to_pbir``; the solver can be consumed by the emit path (a later slice) and its
invariants asserted in isolation.
"""
from __future__ import annotations

import math

# zone-tree node kinds. Imported when available (same scripts/ dir, put on sys.path by conftest);
# duplicated as a fallback so this module is import-safe in isolation.
try:
    from zone_tree import K_FRAME, K_HSTACK, K_LEAF, K_VSTACK
except ImportError:  # pragma: no cover - defensive; conftest puts scripts/ on the path
    K_VSTACK, K_HSTACK, K_FRAME, K_LEAF = "vstack", "hstack", "frame", "leaf"

# -- tunables (one place so tests can assert against them) -----------------------
GAP = 8.0                # inter-sibling gap on a flow container's main axis, page pixels
TOL = 1.0                # linear tolerance: touching / sub-pixel float noise is not overlap

# Ceiling on how far ``solve`` may enlarge the requested page on either axis. Growth is how the
# solver guarantees containment (a page big enough for the content cannot overflow), but a page grown
# without bound is its own defect: every visual on it renders proportionally smaller under FitToPage,
# and one corpus sliver zone would otherwise demand a 164384px-wide canvas. Measured across the
# six-workbook corpus, the sub-41px "floor" count falls 52 -> 47 -> 38 -> 32 at caps of 1.25x / 1.5x /
# 2x and then PLATEAUS -- 2.5x and 3x also yield 32 -- so 2x buys the entire available benefit and
# growing further only shrinks the render. At this ceiling the solver matches the legacy path's
# floor count (32 vs 31) while eliminating its own containment (1) and out-of-bounds (7) defects.
MAX_GROWTH = 2.0

# Default leaf minimum table -- (min_w, min_h) in page pixels, keyed by zone_tree leaf_kind.
# These are deliberately conservative floors; the emit path injects a richer resolver (that knows a
# worksheet's viz type and a matrix's row count) via the ``min_for_leaf`` callback in a later slice.
MIN_SLICER = (120.0, 56.0)       # dropdown: label ~20 + control ~28 + padding
MIN_SLICER_LIST_H = 100.0        # list/checklist mode is taller
MIN_PARAMCTRL = (120.0, 56.0)
MIN_TEXT = (120.0, 32.0)
MIN_WORKSHEET = (160.0, 160.0)   # generic chart floor (replaces the legacy 40px floor)
MIN_LEGEND = (100.0, 80.0)
MIN_BITMAP = (24.0, 24.0)
MIN_BLANK = (0.0, 0.0)           # a genuinely collapsible spacer

_LEAF_MIN = {
    "filter": MIN_SLICER,
    "paramctrl": MIN_PARAMCTRL,
    "text": MIN_TEXT,
    "worksheet": MIN_WORKSHEET,
    "legend": MIN_LEGEND,
    "bitmap": MIN_BITMAP,
    "blank": MIN_BLANK,
}

# Tableau filter modes whose control renders as a multi-row list rather than a dropdown.
_LIST_FILTER_MODES = {"checklist", "typeinandcheckdropdown", "radiolist", "singlevaluelist",
                      "typeinlist", "list"}


def default_min_for_leaf(node):
    """A conservative ``(min_w, min_h)`` for a leaf node, from its ``leaf_kind``.

    Pure and side-effect free. A ``filter`` leaf in a list/checklist mode gets a taller minimum;
    the mode is read from the retained source Element when present. Unknown kinds fall back to the
    worksheet floor.
    """
    lk = node.get("leaf_kind") or "worksheet"
    mw, mh = _LEAF_MIN.get(lk, MIN_WORKSHEET)
    if lk == "filter":
        el = node.get("zone_el")
        mode = el.get("mode") if el is not None else None
        if mode in _LIST_FILTER_MODES:
            mh = MIN_SLICER_LIST_H
    return (float(mw), float(mh))


# -- pass 1: min sizes (bottom-up) ----------------------------------------------
def _main_min_contrib(child, vertical):
    """A child's contribution to its flow parent's MAIN-axis min.

    A fixed-size child occupies exactly ``fixed_px`` on the main axis regardless of its subtree min,
    so it contributes ``fixed_px``; a flexible child contributes its own main-axis min.
    """
    if child.get("fixed_px") is not None:
        return float(child["fixed_px"])
    return child["min_h"] if vertical else child["min_w"]


def compute_mins(node, min_for_leaf, cap=None):
    """Populate ``min_w`` / ``min_h`` on every node, bottom-up. Mutates the tree in place.

    ``cap`` is an optional ``(max_min_w, max_min_h)`` ceiling applied to FRAME nodes only -- see
    :data:`MAX_GROWTH`. Flow (vstack/hstack) mins are never capped: they are exact sums of real
    content requirements, and understating them is what lets a container overrun its own box.
    """
    if node["kind"] == K_LEAF:
        mw, mh = min_for_leaf(node)
        node["min_w"], node["min_h"] = float(mw), float(mh)
        return
    kids = node["children"]
    for c in kids:
        compute_mins(c, min_for_leaf, cap)
    gaps = GAP * max(0, len(kids) - 1)
    if node["kind"] == K_VSTACK:
        node["min_h"] = sum(_main_min_contrib(c, True) for c in kids) + gaps
        node["min_w"] = max((c["min_w"] for c in kids), default=0.0)
    elif node["kind"] == K_HSTACK:
        node["min_w"] = sum(_main_min_contrib(c, False) for c in kids) + gaps
        node["min_h"] = max((c["min_h"] for c in kids), default=0.0)
    else:  # K_FRAME -- children are absolute, placed by SOURCE FRACTION of the frame
        # A frame hands each child ``child_src / frame_src`` of its own box (see ``_scale_abs``), so
        # a child occupying 40% of the frame's height only receives 0.4 * frame_h. Taking the frame's
        # min as ``max(child min)`` therefore UNDERSTATES it by exactly that fraction: the frame looks
        # satisfied while every child is starved. A starved flow child then overruns its own box (its
        # fixed-size and min-clamped grandchildren sum past the box it was given), which is precisely
        # how a solved page produced out-of-bounds rects even when the solver reported no growth --
        # measured as 7 out-of-bounds leaves across 4 corpus dashboards. Invert the fraction so the
        # frame asks for the size at which each child's scaled box actually meets that child's min.
        fs = node.get("src") or {}
        fsw, fsh = fs.get("w"), fs.get("h")
        need_w, need_h = [], []
        for c in kids:
            cs = c.get("src") or {}
            frac_w = (cs.get("w") or 0.0) / fsw if fsw else 0.0
            frac_h = (cs.get("h") or 0.0) / fsh if fsh else 0.0
            # A degenerate fraction means ``_scale_abs`` will make the child fill the frame, so the
            # child's own min is the requirement -- no inversion.
            need_w.append(c["min_w"] / frac_w if frac_w > 0 else c["min_w"])
            need_h.append(c["min_h"] / frac_h if frac_h > 0 else c["min_h"])
        node["min_w"] = max(need_w, default=0.0)
        node["min_h"] = max(need_h, default=0.0)
        if cap:
            # Inverting a near-zero fraction explodes: one corpus dashboard has a sliver zone barely
            # 0.8% of the frame's width, whose 120px slicer minimum would demand a 164384px-wide page.
            # A sliver is decoration, not a reason to make the whole canvas unreadable, so the frame's
            # demand is capped and ``allocate``'s last-resort scale absorbs the residue.
            node["min_w"] = min(node["min_w"], float(cap[0]))
            node["min_h"] = min(node["min_h"], float(cap[1]))


# -- pass 2: allocate (top-down) ------------------------------------------------
def _scale_abs(src, frame):
    """Map an absolute source rect (100000-space) into the frame's allocated page rect.

    ``src`` is a zone_tree rect dict; ``frame`` is a node with ``rect`` (already allocated) and
    ``src``. A degenerate frame source box (zero/None extent) makes the child fill the frame.
    """
    fx, fy, fw, fh = frame["rect"]
    fs = frame["src"]
    fsx, fsy = fs.get("x") or 0.0, fs.get("y") or 0.0
    fsw, fsh = fs.get("w"), fs.get("h")
    if not fsw or not fsh or fsw <= 0 or fsh <= 0:
        return (fx, fy, fw, fh)
    sx, sy = (src.get("x") or 0.0), (src.get("y") or 0.0)
    sw, sh = (src.get("w") or 0.0), (src.get("h") or 0.0)
    return (fx + (sx - fsx) / fsw * fw,
            fy + (sy - fsy) / fsh * fh,
            sw / fsw * fw,
            sh / fsh * fh)


def allocate(node, rect):
    """Assign ``node['rect']`` and recurse, distributing a flow container's rect among its children.

    ``rect`` is ``(x, y, w, h)`` in page pixels. Flow children receive disjoint main-axis intervals
    (disjoint by construction); frame children keep their absolute geometry scaled into ``rect``.
    Mutates the tree in place. Assumes ``compute_mins`` has run.
    """
    node["rect"] = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    if node["kind"] == K_LEAF:
        return
    kids = node["children"]
    if node["kind"] == K_FRAME:
        for c in kids:
            allocate(c, _scale_abs(c["src"], node))
        return
    if not kids:
        return

    x, y, w, h = node["rect"]
    vertical = node["kind"] == K_VSTACK
    minkey = "min_h" if vertical else "min_w"
    main_total = h if vertical else w
    gaps = GAP * max(0, len(kids) - 1)

    fixed = [c for c in kids if c.get("fixed_px") is not None]
    flex = [c for c in kids if c.get("fixed_px") is None]
    avail = main_total - gaps - sum(float(c["fixed_px"]) for c in fixed)

    alloc = {id(c): float(c["fixed_px"]) for c in fixed}
    frozen = set()
    # CSS flex freeze loop: distribute `avail` by fraction, clamp violators UP to min and freeze,
    # repeat. Terminates in <= len(flex) + 1 rounds (each round freezes >= 1 child or breaks).
    for _ in range(len(flex) + 1):
        frozen_sum = sum(alloc[id(c)] for c in flex if id(c) in frozen)
        free = avail - frozen_sum
        rem_frac = sum(c["frac"] for c in flex if id(c) not in frozen) or 1.0
        for c in flex:
            if id(c) not in frozen:
                alloc[id(c)] = free * (c["frac"] / rem_frac)
        violators = [c for c in flex
                     if id(c) not in frozen and alloc[id(c)] < c[minkey] - TOL]
        if not violators:
            break
        for c in violators:
            alloc[id(c)] = float(c[minkey])
            frozen.add(id(c))
        if len(frozen) == len(flex):
            break

    cursor = y if vertical else x
    # Last-resort containment. Everything above is a REQUEST: fixed children take their pixels
    # outright and min-violators are clamped UP, so when a container is itself under-allocated (a
    # capped frame, or a frame whose child fraction could not be fully honoured) the requests can sum
    # past the box and the advancing cursor walks straight off it. Scaling every allocation by the
    # one factor that makes them fit keeps the children's relative proportions and makes flow
    # containment UNCONDITIONAL -- the invariant this module promises -- at the cost of letting a
    # child fall below its min, which is a legible "too small" (floor) signal rather than a silently
    # off-page visual. Load-bearing: without it the capped solver still leaves out-of-bounds rects.
    content = sum(alloc[id(c)] for c in kids)
    room = main_total - gaps
    if content > room + TOL and content > 0:
        squeeze = max(0.0, room) / content
        for c in kids:
            alloc[id(c)] *= squeeze
    for c in kids:
        size = max(0.0, alloc[id(c)])
        if vertical:
            allocate(c, (x, cursor, w, size))
        else:
            allocate(c, (cursor, y, size, h))
        cursor += size + GAP


# -- public entry point ---------------------------------------------------------
def _collect_rects(node, out):
    if node.get("zone_id") is not None and "rect" in node:
        out[node["zone_id"]] = node["rect"]
    for c in node.get("children", ()):
        _collect_rects(c, out)


def solve(tree, page_rect, min_for_leaf=None, grow=True):
    """Solve a parsed zone tree into page-pixel rectangles, or ``None`` (fail-closed).

    ``tree`` is the dict returned by ``zone_tree.parse_zone_tree`` (must contain ``root``), or its
    ``root`` node directly. ``page_rect`` is ``(x, y, w, h)`` in page pixels (typically
    ``(0, 0, page_w, page_h)``). ``min_for_leaf`` is an optional ``node -> (min_w, min_h)`` callback;
    the default table (``default_min_for_leaf``) is used when omitted. When ``grow`` is true (the
    default) the page is enlarged to the root's min on either axis, guaranteeing the result is
    disjoint (flow), contained, and min-respecting.

    Returns ``{"rects": {zone_id: (x, y, w, h)}, "page": (x, y, w, h)}`` on success (and mutates the
    tree in place with ``min_w`` / ``min_h`` / ``rect`` on every node), or ``None`` on any failure.
    Never raises. Deterministic: the same tree and page always produce identical rects.
    """
    try:
        root = tree["root"] if isinstance(tree, dict) and "root" in tree else tree
        if not isinstance(root, dict) or "kind" not in root:
            return None
        resolver = min_for_leaf or default_min_for_leaf

        px, py, pw, ph = (float(page_rect[0]), float(page_rect[1]),
                          float(page_rect[2]), float(page_rect[3]))
        # The growth ceiling is derived from the REQUESTED page, so it must be known before the
        # bottom-up min pass can cap a frame's inverted-fraction demand.
        cap = (pw * MAX_GROWTH, ph * MAX_GROWTH) if (grow and pw > 0 and ph > 0) else None
        compute_mins(root, resolver, cap)

        if grow:
            # Enlarge to the root's min on both axes so allocation cannot overflow the page; min
            # sizes are page-independent, so one allocation pass on the grown rect suffices (this is
            # the layout-solver spec's "grow then re-run once", collapsed to a single pass).
            pw = max(pw, float(math.ceil(root["min_w"])))
            ph = max(ph, float(math.ceil(root["min_h"])))
        if pw <= 0 or ph <= 0:
            return None

        allocate(root, (px, py, pw, ph))

        rects = {}
        _collect_rects(root, rects)
        return {"rects": rects, "page": (px, py, pw, ph)}
    except Exception:  # fail-closed: never raise into the caller
        return None
