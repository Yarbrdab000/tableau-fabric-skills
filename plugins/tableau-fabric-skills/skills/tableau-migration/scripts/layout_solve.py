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

# Ceiling on a FRAME's inverted-fraction height demand, as a multiple of the requested page height.
#
# This is a pathology guard, not a size preference. A frame positions each child at
# ``child_src / frame_src`` of its own box, so satisfying a child's minimum requires inverting that
# fraction -- and a near-zero fraction inverts to a near-infinite demand: one corpus dashboard has a
# sliver zone barely 0.8% of its frame's width, whose 120px slicer minimum alone would ask for a
# 164384px canvas. A sliver is decoration, not a reason to make the whole page unreadable, so the
# demand is capped and ``allocate``'s proportional squeeze absorbs the residue.
#
# It deliberately does NOT double as a growth budget. An earlier version of this constant was tuned
# by watching the sub-41px "floor" defect count fall as the cap rose -- but that count mechanically
# improves when the canvas is inflated (nothing can be under 41px once the page doubles), so the
# metric was rewarding the growth rather than measuring the layout. Page growth is now height-only
# and exact (see ``solve``); this value only bounds the pathological case.
FRAME_DEMAND_CAP = 2.0

# Backwards-compatible alias: this used to be the symmetric both-axes growth ceiling.
MAX_GROWTH = FRAME_DEMAND_CAP

# Default leaf minimum table -- (min_w, min_h) in page pixels, keyed by zone_tree leaf_kind.
# These are deliberately conservative floors; the emit path injects a richer resolver (that knows a
# worksheet's viz type and a matrix's row count) via the ``min_for_leaf`` callback in a later slice.
# A leaf minimum must be at least what the EMIT path will actually give that leaf: emit re-floors a
# slicer to its own dropdown minimum after placement, so a smaller reservation here does not shrink
# the emitted box, it just makes it overrun whatever the solver seated below it.
MIN_SLICER = (120.0, 76.0)       # dropdown: matches the emitter's own SLICER_DROPDOWN_MIN_H floor
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

# -- preferred sizes and growth weights (quality spec 5.1) -----------------------
# A minimum answers "how small may this get before it breaks"; it says nothing about how large a
# thing should be ALLOWED to become. With only a minimum, every child of a flow container is a
# grower: surplus main-axis space is handed out by fraction, so a one-line caption in a roomy row
# inflates until it fills its share. Measured on the user's real ATTI dashboard, that is exactly how
# the "Director" and "Manager" section labels -- one word each -- rendered as 155px purple slabs
# while the identical labels on a tighter row below rendered correctly at ~22px.
#
# ``grow = 0`` on slicers, parameter controls, captions and legends is the consequential entry: it
# is what stops a two-row slicer band from silently eating a third of the canvas. These controls are
# fixed-height chrome -- a taller dropdown is not a better dropdown -- so they are capped at their
# preferred size and the surplus goes to the visuals that actually benefit from it.
#
# Scoped to the MAIN AXIS ONLY. Every statement of the rule is about height ("do NOT stretch a
# slicer ROW to fill the page"), and capping a slicer's WIDTH would make the measured truncation
# defect worse -- the same ATTI page renders "Ontrac Ma...", "Techsuper...", "Customer ..." because
# its slicers are already too narrow for their labels. Cross-axis preferences stay unbounded.
PREF_CHART_H = 240.0             # a chart below this reads as a sparkline, not a chart
_NOGROW_KINDS = ("filter", "paramctrl", "text", "legend")

INF = float("inf")


def default_pref_for_leaf(node, min_wh):
    """``(pref_w, pref_h, grow)`` for a leaf, given its already-resolved ``(min_w, min_h)``.

    Pure and side-effect free. ``pref`` is a CAP for a non-grower and merely an ordering hint for a
    grower, so an unbounded (``inf``) preference means "no opinion, take the surplus".
    """
    lk = node.get("leaf_kind") or "worksheet"
    mw, mh = float(min_wh[0]), float(min_wh[1])
    if lk in _NOGROW_KINDS:
        # Chrome: its minimum IS its preference. Nothing is gained by making it taller.
        return (INF, mh, 0)
    if lk == "worksheet":
        return (INF, max(mh, PREF_CHART_H), 1)
    return (INF, INF, 1)          # bitmap / blank: no vertical opinion


_PREF_KEYS = ("pref_w", "pref_h")


# -- authored size: the ceiling on every generic minimum -------------------------
MIN_ABSOLUTE = 16.0      # below this an object cannot render at all, authored size notwithstanding


def _authored_px(node, scale):
    """The size the AUTHOR drew this node at, in page pixels -- ``(w, h)``, either possibly ``None``.

    ``scale`` is ``(page_w / root_src_w, page_h / root_src_h)``, so a zone occupying 8.8% of the
    dashboard's height on a 1000px page measures 88px here. ``None`` scale (root without a source
    extent) disables the clamp entirely.
    """
    if not scale:
        return None, None
    src = node.get("src") or {}
    w, h = src.get("w"), src.get("h")
    return ((float(w) * scale[0]) if w else None,
            (float(h) * scale[1]) if h else None)


def _clamp_to_authored(m, authored_px):
    """Bound a generic minimum by the authored size, never dropping below :data:`MIN_ABSOLUTE`.

    The leaf minimums in :data:`_LEAF_MIN` are READABILITY HEURISTICS -- "a chart under 160px is
    not a chart". The author's own layout is EVIDENCE, and where the two disagree the author wins:
    a worksheet drawn 88px tall is a caption strip, and a 160px "chart floor" applied to it is
    simply the wrong classification. Enforcing the floor anyway does not make that strip readable;
    it makes the strip demand more room than its zone has, and a frame satisfies that demand by
    scaling the ENTIRE canvas. On a real 1000x1000 dashboard a single 21px text line with a 32px
    floor -- occupying 2.1% of the page -- demanded 32 / 0.021 = 1506px of canvas, so eleven pixels
    of caption cost five hundred pixels of page and every object on it rendered 50% taller with the
    whitespace to match. A heuristic that inflates the whole page to satisfy itself is worth less
    than the author's own judgement about one object.

    Returns ``m`` unchanged when there is no authored size, and preserves an explicit zero so a
    genuinely collapsible spacer stays collapsible.
    """
    if authored_px is None or authored_px <= 0 or m <= 0:
        return m
    return min(float(m), max(float(authored_px), MIN_ABSOLUTE))


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
    """A child's contribution to its flow parent's MAIN-axis MIN -- always its own subtree min.

    A pinned child used to contribute ``fixed_px`` here, on the reasoning that it occupies exactly
    its pin regardless of its subtree. That conflates a REQUEST with a REQUIREMENT, and it is the
    same confusion ``allocate``'s fixed-share rule already had to undo: a pin is what the author
    asked for at the container's authored size, not a floor the layout must be enlarged to honour.
    Propagating it as a minimum makes an ancestor grow to fit the pins -- measured on a real
    dashboard whose table-band pins summed to 871px inside a zone the author drew 725px tall,
    demanding a 1506px canvas for a 1000x1000 dashboard whose every object fits.

    The pin is not discarded: it is the child's PREFERENCE (see :func:`_main_pref_contrib`), so a
    container with room still lays its children out at exactly the pinned sizes, and only a
    container genuinely short of room trades the pins down -- proportionally, and never below the
    subtree min this function reports.
    """
    return child["min_h"] if vertical else child["min_w"]


def _main_pref_contrib(child, vertical):
    """A child's contribution to its flow parent's MAIN-axis PREFERENCE.

    Mirrors :func:`_main_min_contrib`: a pinned child's preference is exactly its pin, since
    ``fixed_px`` already states the size the author asked for.
    """
    if child.get("fixed_px") is not None:
        return float(child["fixed_px"])
    return child.get("pref_h", INF) if vertical else child.get("pref_w", INF)


def compute_mins(node, min_for_leaf, cap=None, authored=None):
    """Populate ``min_w`` / ``min_h`` on every node, bottom-up. Mutates the tree in place.

    ``cap`` is an optional ``(max_min_w, max_min_h)`` ceiling applied to FRAME nodes only -- see
    :data:`FRAME_DEMAND_CAP`. Flow (vstack/hstack) mins are never capped: they are exact sums of
    real content requirements, and understating them is what lets a container overrun its own box.

    ``authored`` is an optional ``(page_w / root_src_w, page_h / root_src_h)`` scale; when given,
    every LEAF minimum is bounded by the size its author drew (see :func:`_clamp_to_authored`).
    """
    if node["kind"] == K_LEAF:
        mw, mh = min_for_leaf(node)
        aw, ah = _authored_px(node, authored)
        node["min_w"] = _clamp_to_authored(float(mw), aw)
        node["min_h"] = _clamp_to_authored(float(mh), ah)
        pw, ph, g = default_pref_for_leaf(node, (node["min_w"], node["min_h"]))
        # A preference below the minimum is incoherent -- the min always wins.
        node["pref_w"], node["pref_h"] = max(pw, node["min_w"]), max(ph, node["min_h"])
        node["grow"] = g
        return
    kids = node["children"]
    for c in kids:
        compute_mins(c, min_for_leaf, cap, authored)
    gaps = GAP * max(0, len(kids) - 1)
    # A container grows only if something inside it wants to. A vstack of nothing but captions and
    # slicers is itself fixed-height chrome -- this is what makes a whole header BAND stop absorbing
    # surplus, not just the individual captions in it.
    node["grow"] = 1 if any(c.get("grow", 1) for c in kids) else 0
    if node["kind"] == K_VSTACK:
        node["min_h"] = sum(_main_min_contrib(c, True) for c in kids) + gaps
        node["min_w"] = max((c["min_w"] for c in kids), default=0.0)
        node["pref_h"] = sum(_main_pref_contrib(c, True) for c in kids) + gaps
        node["pref_w"] = max((c.get("pref_w", INF) for c in kids), default=INF)
    elif node["kind"] == K_HSTACK:
        node["min_w"] = sum(_main_min_contrib(c, False) for c in kids) + gaps
        node["min_h"] = max((c["min_h"] for c in kids), default=0.0)
        node["pref_w"] = sum(_main_pref_contrib(c, False) for c in kids) + gaps
        node["pref_h"] = max((c.get("pref_h", INF) for c in kids), default=INF)
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


def _cross_from_src(child, node, origin, extent, axis):
    """The child's cross-axis ``(size, position)`` from its AUTHORED size, not a stretch-to-fill.

    A flexbox container stretches its children across the cross axis; Tableau does not. A Tableau
    zone declares its own size inside its parent, and the leftover is the card's padding -- so
    stretching a leaf to its container's cross extent inflates every object by exactly that padding.

    Measured on the Salesforce NPSP "Staff Capacity" dashboard (1366x768 fixed): a filter card
    authored ``h=7422`` inside a ``layout-flow`` authored ``h=12890`` was emitted **99px tall instead
    of 57px**, i.e. the container's height, leaving a dead band under every dropdown. The same
    stretch inflated the KPI row's worksheets from their authored 235px to the container's 257px.
    Across the page not one of 16 scored objects landed within 10px of its authored SIZE, and the
    median displacement of an object's centre was 44px.

    This is the flow-axis twin of what ``_scale_abs`` already does for a ``frame``: map the source
    fraction into the allocated box. The MAIN axis is untouched -- the flex algorithm above owns it,
    with its fixed/min/squeeze rules -- so this only ever changes the axis that was previously
    ignoring the source entirely.

    Fail-safe and containment-preserving:
      * a node or child with no usable ``src`` extent falls back to the old stretch, so any tree the
        parser could not measure behaves exactly as before;
      * the size is clamped UP to the child's own minimum (never past the container), keeping the
        min-respect invariant;
      * the offset is clamped so ``offset + size <= extent``, so cross-axis containment stays
        unconditional -- the same promise the module already makes on the main axis.
    """
    ns = node.get("src") or {}
    cs = child.get("src") or {}
    n_ext = ns.get(axis)
    c_ext = cs.get(axis)
    try:
        n_ext = float(n_ext)
        c_ext = float(c_ext)
    except (TypeError, ValueError):
        return extent, origin
    if n_ext <= 0 or c_ext <= 0:
        return extent, origin

    size = extent * min(1.0, c_ext / n_ext)
    minkey = "min_w" if axis == "w" else "min_h"
    try:
        size = max(size, min(float(child.get(minkey) or 0.0), extent))
    except (TypeError, ValueError):
        pass
    size = max(0.0, min(size, extent))

    okey = "x" if axis == "w" else "y"
    try:
        off = (float(cs.get(okey) or 0.0) - float(ns.get(okey) or 0.0)) / n_ext * extent
    except (TypeError, ValueError):
        off = 0.0
    off = max(0.0, min(off, max(0.0, extent - size)))
    return size, origin + off


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
    room = main_total - gaps

    alloc = {id(c): float(c["fixed_px"]) for c in fixed}
    # A fixed child's pixels are a REQUEST measured at the container's AUTHORED size -- not a first
    # claim on whatever box an ancestor actually hands it. Paying every fixed child in full out of a
    # SHRUNKEN box makes the flexible siblings absorb the entire shortfall: on a real dashboard a row
    # authored as four equal quarters (three fixed filter cards + one flexible logo) solved to
    # 177/205/179/26 px, because the three cards took 561 of the 587 px available and the logo -- the
    # only flexible child -- was left the 26 px remainder. It cleared its own 24 px bitmap minimum, so
    # no violator check fired; the aspect ratio was simply destroyed (204x59 authored -> 26x97).
    # Cap the fixed children's collective demand at the room NOT authored to flexible siblings and
    # scale them proportionally into it, never below their own minimum, so a squeeze is SHARED rather
    # than dumped on whichever sibling happens to be flexible. A no-op whenever the container has room
    # for everyone -- which is the common case, and why the default corpus result is unchanged.
    if fixed and flex and room > 0:
        tot_frac = sum(c.get("frac") or 0.0 for c in kids) or 1.0
        flex_share = sum(c.get("frac") or 0.0 for c in flex) / tot_frac
        # Two independent ceilings, and the tighter one binds.
        #
        # The first is the author's own split: a fixed child may not claim room the author gave to a
        # flexible sibling (the ft4f rule -- it is what stops three filter cards from taking 561 of
        # 587px and leaving a logo the 26px remainder).
        #
        # The second is min-respect, and it became load-bearing once ``_main_min_contrib`` stopped
        # reporting pins as minimums. The container is now sized to the sum of its subtree MINS,
        # while ``allocate`` still pays every pin in FULL -- so a pin larger than its subtree min
        # eats into exactly the pixels a flexible sibling needs to reach its own minimum, and the
        # final squeeze then drags every child below min together. Reserving the flexible minimums
        # up front makes the pin yield to them instead, which is the correct precedence: a minimum
        # is a requirement and a pin is a request.
        budget = min(room * max(0.0, 1.0 - flex_share),
                     room - sum(float(c[minkey]) for c in flex))
        budget = max(0.0, budget)
        fixed_total = sum(alloc[id(c)] for c in fixed)
        if fixed_total > budget + TOL and fixed_total > 0:
            scale = budget / fixed_total
            for c in fixed:
                request = float(c["fixed_px"])
                # Floor at the child's own minimum -- but never ABOVE its request, so a child the
                # author pinned smaller than its min keeps the pinned size it has always had. This
                # rule may only ever take pixels off a fixed child, never hand it more.
                alloc[id(c)] = max(min(float(c[minkey]), request), request * scale)

    avail = room - sum(alloc[id(c)] for c in fixed)
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
    # Quality spec 5.1 -- cap the non-growers, redistribute the surplus.
    # The freeze loop above hands out every available pixel by fraction, which is correct only if
    # every child benefits from more space. A slicer, parameter control, caption or legend does not:
    # past its preferred size the extra pixels become a blank slab. Take the surplus back and give it
    # to the children that do benefit, weighted by the fraction the author already assigned them.
    prefkey = "pref_h" if vertical else "pref_w"
    surplus, growers = 0.0, []
    for c in flex:
        pref = float(c.get(prefkey, INF))
        if not c.get("grow", 1):
            if alloc[id(c)] > pref + TOL:
                surplus += alloc[id(c)] - pref
                alloc[id(c)] = pref
        else:
            growers.append(c)
    if surplus > TOL and growers:
        tot = sum(c.get("frac") or 0.0 for c in growers)
        for c in growers:
            share = ((c.get("frac") or 0.0) / tot) if tot > 0 else (1.0 / len(growers))
            alloc[id(c)] += surplus * share
    # When NOTHING in the container grows -- a pure header band of captions and slicers -- the
    # reclaimed pixels are deliberately left unallocated and become trailing space rather than being
    # pushed back into the chrome they were just taken from. A row of controls that ends early and
    # leaves the canvas beneath it free is the intended outcome; that free space is what the visuals
    # on the rest of the page get to use once the page stops having to grow to fit inflated chrome.
    # Last-resort containment. Everything above is a REQUEST: fixed children take their pixels
    # outright and min-violators are clamped UP, so when a container is itself under-allocated (a
    # capped frame, or a frame whose child fraction could not be fully honoured) the requests can sum
    # past the box and the advancing cursor walks straight off it. Scaling every allocation by the
    # one factor that makes them fit keeps the children's relative proportions and makes flow
    # containment UNCONDITIONAL -- the invariant this module promises -- at the cost of letting a
    # child fall below its min, which is a legible "too small" (floor) signal rather than a silently
    # off-page visual. Load-bearing: without it the capped solver still leaves out-of-bounds rects.
    content = sum(alloc[id(c)] for c in kids)
    gap = GAP
    if room >= 0:
        if content > room + TOL and content > 0:
            squeeze = room / content
            for c in kids:
                alloc[id(c)] *= squeeze
    else:
        # Degenerate box: the inter-sibling GAPS ALONE exceed it, so ``room`` is negative and there
        # is no content budget to squeeze into. Squeezing only the children then leaves every gap at
        # its full 8px, and the advancing cursor walks straight off the parent -- a two-child hstack
        # in a 1px box seats its second child at x=8. The gaps have to shrink with everything else.
        # Height-only growth is what surfaced this: while the page grew on BOTH axes, a container was
        # never narrower than its own gaps, so the case was unreachable.
        total = content + gaps
        squeeze = (max(0.0, main_total) / total) if total > 0 else 0.0
        for c in kids:
            alloc[id(c)] *= squeeze
        gap = GAP * squeeze
    for c in kids:
        size = max(0.0, alloc[id(c)])
        if vertical:
            cw, cx = _cross_from_src(c, node, x, w, "w")
            allocate(c, (cx, cursor, cw, size))
        else:
            ch, cy = _cross_from_src(c, node, y, h, "h")
            allocate(c, (cursor, cy, size, ch))
        cursor += size + gap


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
        # Frame demand is capped before the bottom-up min pass, since inverting a near-zero source
        # fraction explodes (one corpus sliver zone barely 0.8% of its frame's width would demand a
        # 164384px canvas). This is a PATHOLOGY GUARD, not a size preference -- see FRAME_DEMAND_CAP.
        cap = (pw, ph * FRAME_DEMAND_CAP) if (grow and pw > 0 and ph > 0) else None
        # The authored scale maps a zone's source extent to page pixels, so a generic leaf floor can
        # never demand more room than the author gave that object -- the single largest source of
        # canvas inflation before this. See _clamp_to_authored.
        rs = root.get("src") or {}
        rsw, rsh = rs.get("w"), rs.get("h")
        authored = ((pw / float(rsw), ph / float(rsh))
                    if (rsw and rsh and float(rsw) > 0 and float(rsh) > 0) else None)
        compute_mins(root, resolver, cap, authored)

        if grow:
            # Growth is HEIGHT-ONLY, and exact.
            #
            # A taller page rescales to the viewport under FitToPage; it does not produce a
            # scrollbar, so height is the axis where growth is free. WIDTH is not: widening the
            # canvas makes every visual on it render proportionally smaller and every font
            # correspondingly less legible, buying nothing, because the content that needed room
            # needed it vertically. Growing both axes produced 2732px-wide pages on the user's real
            # workbooks -- 1.32x the authored canvas area -- for content that fit horizontally.
            #
            # The width cap above pins the frame demand at exactly the requested width, so a frame
            # that cannot honour a child's fraction degrades through allocate's proportional squeeze
            # rather than by inflating the canvas.
            ph = max(ph, float(math.ceil(root["min_h"])))
        if pw <= 0 or ph <= 0:
            return None

        allocate(root, (px, py, pw, ph))

        rects = {}
        _collect_rects(root, rects)
        return {"rects": rects, "page": (px, py, pw, ph)}
    except Exception:  # fail-closed: never raise into the caller
        return None
