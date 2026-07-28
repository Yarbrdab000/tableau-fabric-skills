"""Layout-solver property tests -- Zone Geometry v3 slice 2.

``layout_solve.solve`` resolves a parsed zone tree (vstack / hstack / frame / leaf) into absolute
page-pixel rectangles via a two-pass flexbox solve. These tests turn the headline claim -- *tiled
flow siblings cannot overlap* -- from an observed property into a PROVEN one, by generating seeded
random trees (stdlib ``random``; no Hypothesis dependency) and asserting the invariants hold for
every one.

Scope of each invariant (deliberate, and documented so the contract is honest):

* **Disjointness, containment, min-respect, coverage** are FLOW-container theorems and are asserted
  on flow-only trees (vstack/hstack). A ``frame`` is Tableau's absolute-positioning escape hatch
  (``layout-basic``): its children keep their stored geometry (``scale_abs``), so overlap is allowed
  there BY DESIGN and a frame can legitimately place / squash a child outside the flow's guarantees.
  Frames are covered by their own small test set (overlap is representable; a within-bounds child is
  contained; a degenerate frame source makes a child fill the frame).
* The opt-in real-workbook golden asserts the slice gate -- the solver never raises on a real tree
  and produces **zero flow overlaps**. Containment is NOT asserted here, but it is no longer a
  conceded gap: slice 4c made a frame's min account for its children's source fractions and gave
  every flow container a last-resort down-scale, so on-page containment is now unconditional. That
  invariant is owned by ``test_layout_containment.py``; a frame squashing a child BELOW ITS MIN is
  still permitted (the author pinned that geometry) and surfaces as a legible "too small" signal.

Fixed-size (``is-fixed``) leaves are pinned to ``fixed_px`` on the container main axis regardless of
their min, so they are exempt from main-axis min-respect (their author pinned them); they still must
not break disjointness or coverage.
"""
import os
import random
import xml.etree.ElementTree as ET

import pytest

from zone_tree import K_FRAME, K_HSTACK, K_LEAF, K_VSTACK, parse_zone_tree
from layout_solve import (
    GAP,
    MIN_SLICER,
    MIN_SLICER_LIST_H,
    MIN_WORKSHEET,
    TOL,
    allocate,
    compute_mins,
    default_min_for_leaf,
    solve,
)

_SEEDS = list(range(1, 9))            # 8 seeds x 4 invariants = 32 generated-tree assertions


# -- geometry helpers -----------------------------------------------------------
def _overlap_area_pos(a, b):
    ox = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    oy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return ox > TOL and oy > TOL


def _contained(child, parent, tol=1.0):
    cx, cy, cw, ch = child
    px, py, pw, ph = parent
    return (cx >= px - tol and cy >= py - tol
            and cx + cw <= px + pw + tol and cy + ch <= py + ph + tol)


def _iter_flow(node):
    if node["kind"] in (K_VSTACK, K_HSTACK):
        yield node
    for c in node["children"]:
        yield from _iter_flow(c)


def _iter_leaves(node):
    if node["kind"] == K_LEAF:
        yield node
    for c in node["children"]:
        yield from _iter_leaves(c)


# -- random tree generators -----------------------------------------------------
_ids = {"n": 0}


def _nid():
    _ids["n"] += 1
    return "z%d" % _ids["n"]


def _leaf(mn):
    return {"kind": K_LEAF, "zone_id": _nid(), "leaf_kind": "worksheet", "name": None,
            "src": {"x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0}, "frac": 0.0,
            "fixed_px": None, "floating": False, "hidden": False, "children": [],
            "zone_el": None, "_min": (float(mn[0]), float(mn[1]))}


def _flow_tree(rng, depth=0, max_depth=4):
    """A flow-only tree (vstack/hstack/leaf) with positive fracs summing to 1 per container and, on
    some containers, one fixed-size LEAF child (leaving >= 1 flexible sibling)."""
    if depth >= max_depth or (depth > 0 and rng.random() < 0.4):
        mw = 0.0 if rng.random() < 0.1 else rng.uniform(20, 160)
        mh = 0.0 if rng.random() < 0.1 else rng.uniform(20, 160)
        return _leaf((mw, mh))
    kind = rng.choice([K_VSTACK, K_HSTACK])
    n = rng.randint(1, 4)
    kids = [_flow_tree(rng, depth + 1, max_depth) for _ in range(n)]
    weights = [rng.uniform(0.2, 1.0) for _ in range(n)]
    tot = sum(weights)
    for c, w in zip(kids, weights):
        c["frac"] = w / tot
    # optionally pin one leaf child to a fixed pixel size, always leaving a flexible sibling
    leaf_kids = [c for c in kids if c["kind"] == K_LEAF]
    if n >= 2 and len(leaf_kids) >= 1 and rng.random() < 0.35:
        fc = rng.choice(leaf_kids)
        if not all(c["fixed_px"] is not None for c in kids if c is not fc):
            fc["fixed_px"] = rng.uniform(16, 60)
    return {"kind": kind, "zone_id": _nid(), "leaf_kind": None, "name": None,
            "src": {"x": 0.0, "y": 0.0, "w": 1000.0, "h": 1000.0}, "frac": 0.0,
            "fixed_px": None, "floating": False, "hidden": False, "children": kids,
            "zone_el": None}


def _min_for(node):
    return node["_min"]


def _build(seed, max_depth=4):
    rng = random.Random(seed)
    return _flow_tree(rng, max_depth=max_depth)


# -- the four flow invariants (parametrized over seeds) -------------------------
@pytest.mark.parametrize("seed", _SEEDS)
def test_flow_siblings_are_disjoint(seed):
    root = _build(seed)
    assert solve(root, (0, 0, 1280, 720), min_for_leaf=_min_for) is not None
    for cont in _iter_flow(root):
        kids = cont["children"]
        for i in range(len(kids)):
            for j in range(i + 1, len(kids)):
                assert not _overlap_area_pos(kids[i]["rect"], kids[j]["rect"]), (
                    "seed %d: %s siblings overlap: %r / %r"
                    % (seed, cont["kind"], kids[i]["rect"], kids[j]["rect"]))


@pytest.mark.parametrize("seed", _SEEDS)
def test_children_contained_in_parent(seed):
    root = _build(seed)
    solve(root, (0, 0, 1280, 720), min_for_leaf=_min_for)
    for cont in _iter_flow(root):
        for c in cont["children"]:
            assert _contained(c["rect"], cont["rect"]), (
                "seed %d: child %r escapes %s parent %r"
                % (seed, c["rect"], cont["kind"], cont["rect"]))


@pytest.mark.parametrize("seed", _SEEDS)
def test_nonfixed_leaves_respect_min(seed):
    """Min-respect is unconditional on HEIGHT, and conditional on width.

    Since growth is height-only (a taller page rescales under FitToPage; a wider one just shrinks
    every visual on it), the solver can always make room vertically but never horizontally. A page
    narrower than the root's own ``min_w`` therefore has no honest option left: ``allocate``'s
    proportional squeeze takes every child below its minimum together, which renders as a legible
    "too small" signal rather than a visual silently pushed off the page. Width min-respect is
    asserted only when the page is actually wide enough to deliver it.

    The minimum asserted is the EFFECTIVE one the solver computed, not the raw resolver value: a
    generic floor is bounded by the size the author drew (``_clamp_to_authored``), and these
    synthetic leaves each declare a source box of 10% of the canvas, so their floors are legitimately
    clamped. Asserting the raw value would be asserting that the solver ignores the author.
    """
    root = _build(seed)
    solve(root, (0, 0, 1280, 720), min_for_leaf=_min_for)
    wide_enough = root["min_w"] <= 1280 + TOL
    for leaf in _iter_leaves(root):
        if leaf["fixed_px"] is not None:
            continue  # pinned by the author; exempt from main-axis min
        mw, mh = leaf["min_w"], leaf["min_h"]
        assert (mw, mh) <= leaf["_min"], "the clamp may only ever lower a minimum"
        rw, rh = leaf["rect"][2], leaf["rect"][3]
        assert rh >= mh - TOL, (
            "seed %d: leaf %r below min height %.1f" % (seed, leaf["rect"], mh))
        if wide_enough:
            assert rw >= mw - TOL, (
                "seed %d: leaf %r below min width %.1f" % (seed, leaf["rect"], mw))


@pytest.mark.parametrize("seed", _SEEDS)
def test_flow_coverage_sums_to_parent(seed):
    root = _build(seed)
    solve(root, (0, 0, 1280, 720), min_for_leaf=_min_for)
    for cont in _iter_flow(root):
        kids = cont["children"]
        vertical = cont["kind"] == K_VSTACK
        gaps = GAP * max(0, len(kids) - 1)
        used = sum((c["rect"][3] if vertical else c["rect"][2]) for c in kids) + gaps
        main = cont["rect"][3] if vertical else cont["rect"][2]
        assert abs(used - main) <= 1e-3, (
            "seed %d: %s children sum %.4f != parent main %.4f"
            % (seed, cont["kind"], used, main))


# -- determinism ----------------------------------------------------------------
def test_solve_is_deterministic_across_100_runs():
    root = _build(7)
    first = solve(root, (0, 0, 1280, 720), min_for_leaf=_min_for)["rects"]
    for _ in range(100):
        again = solve(root, (0, 0, 1280, 720), min_for_leaf=_min_for)["rects"]
        assert again == first


# -- growth ---------------------------------------------------------------------
@pytest.mark.parametrize("seed", [3, 5])
def test_page_grows_to_ceil_root_min(seed):
    """Growth is height-only and exact: width is left at the requested value.

    Widening the canvas buys nothing -- FitToPage rescales the page to the viewport, so a wider
    canvas renders every visual and every font proportionally smaller for content that needed room
    vertically. Growing both axes inflated the user's real workbooks to 2732px-wide pages at 1.32x
    the authored area, which is why the both-axes contract this test used to assert was wrong.
    """
    import math
    root = _build(seed)
    res = solve(root, (0, 0, 1, 1), min_for_leaf=_min_for)          # tiny page -> must grow
    assert res["page"] == (0.0, 0.0, 1.0, float(math.ceil(root["min_h"])))
    # and the grown solve is still disjoint + contained
    for cont in _iter_flow(root):
        kids = cont["children"]
        for c in kids:
            assert _contained(c["rect"], cont["rect"])
        for i in range(len(kids)):
            for j in range(i + 1, len(kids)):
                assert not _overlap_area_pos(kids[i]["rect"], kids[j]["rect"])


# -- termination on pathological inputs (grow=False so the freeze loop is stressed) ---
def test_terminates_when_every_child_below_min():
    kids = [_leaf((300, 300)) for _ in range(5)]
    for i, c in enumerate(kids):
        c["frac"] = 0.2
    root = {"kind": K_VSTACK, "zone_id": _nid(), "leaf_kind": None, "name": None,
            "src": {"x": 0, "y": 0, "w": 10, "h": 10}, "frac": 0.0, "fixed_px": None,
            "floating": False, "hidden": False, "children": kids, "zone_el": None}
    res = solve(root, (0, 0, 100, 100), min_for_leaf=_min_for, grow=False)
    assert res is not None
    for i in range(len(kids)):
        for j in range(i + 1, len(kids)):
            assert not _overlap_area_pos(kids[i]["rect"], kids[j]["rect"])


def test_terminates_with_one_grabby_fraction():
    kids = [_leaf((20, 20)) for _ in range(4)]
    kids[0]["frac"] = 1.0
    for c in kids[1:]:
        c["frac"] = 0.0
    root = {"kind": K_HSTACK, "zone_id": _nid(), "leaf_kind": None, "name": None,
            "src": {"x": 0, "y": 0, "w": 10, "h": 10}, "frac": 0.0, "fixed_px": None,
            "floating": False, "hidden": False, "children": kids, "zone_el": None}
    res = solve(root, (0, 0, 800, 200), min_for_leaf=_min_for)
    assert res is not None
    for i in range(len(kids)):
        for j in range(i + 1, len(kids)):
            assert not _overlap_area_pos(kids[i]["rect"], kids[j]["rect"])


def test_terminates_with_200_children():
    kids = [_leaf((10, 10)) for _ in range(200)]
    for c in kids:
        c["frac"] = 1.0 / 200
    root = {"kind": K_VSTACK, "zone_id": _nid(), "leaf_kind": None, "name": None,
            "src": {"x": 0, "y": 0, "w": 10, "h": 10}, "frac": 0.0, "fixed_px": None,
            "floating": False, "hidden": False, "children": kids, "zone_el": None}
    res = solve(root, (0, 0, 1280, 720), min_for_leaf=_min_for)
    assert res is not None and len(res["rects"]) >= 200   # 200 leaves + the root container
    for i in range(0, len(kids) - 1):        # adjacent pairs suffice for a stacked column
        assert not _overlap_area_pos(kids[i]["rect"], kids[i + 1]["rect"])


# -- frame behaviour (the absolute escape hatch) --------------------------------
def _node(kind, src, children=(), **kw):
    d = {"kind": kind, "zone_id": _nid(), "leaf_kind": None, "name": None,
         "src": {"x": src[0], "y": src[1], "w": src[2], "h": src[3]}, "frac": 0.0,
         "fixed_px": None, "floating": False, "hidden": False, "children": list(children),
         "zone_el": None}
    d.update(kw)
    return d


def test_frame_children_may_overlap_by_design():
    a = _node(K_LEAF, (0, 0, 60, 60), leaf_kind="worksheet")
    b = _node(K_LEAF, (40, 40, 60, 60), leaf_kind="worksheet")   # overlaps A in source
    frame = _node(K_FRAME, (0, 0, 100, 100), [a, b])
    res = solve(frame, (0, 0, 200, 200), min_for_leaf=lambda n: (0.0, 0.0), grow=False)
    assert res is not None
    # a frame REPRESENTS overlap (unlike a flow container): the two rects do overlap
    assert _overlap_area_pos(a["rect"], b["rect"])


def test_frame_child_within_source_is_contained():
    a = _node(K_LEAF, (10, 10, 50, 50), leaf_kind="worksheet")
    frame = _node(K_FRAME, (0, 0, 100, 100), [a])
    solve(frame, (0, 0, 200, 200), min_for_leaf=lambda n: (0.0, 0.0), grow=False)
    assert _contained(a["rect"], frame["rect"])
    assert a["rect"] == (20.0, 20.0, 100.0, 100.0)   # (10,10,50,50) scaled x2 into the frame


def test_degenerate_frame_source_fills_child():
    a = _node(K_LEAF, (0, 0, 0, 0), leaf_kind="worksheet")
    frame = _node(K_FRAME, (0, 0, 0, 0), [a])         # zero-extent source
    solve(frame, (0, 0, 300, 150), min_for_leaf=lambda n: (0.0, 0.0), grow=False)
    assert a["rect"] == frame["rect"] == (0.0, 0.0, 300.0, 150.0)


# -- never-raises + defaults ----------------------------------------------------
def test_solve_never_raises_on_garbage():
    assert solve(None, (0, 0, 100, 100)) is None
    assert solve("nope", (0, 0, 100, 100)) is None
    assert solve({"root": {"no_kind": 1}}, (0, 0, 100, 100)) is None
    assert solve(_leaf((10, 10)), (0, 0, 0, 0), grow=False) is None   # non-positive page, no grow room


def test_default_min_table():
    assert default_min_for_leaf({"leaf_kind": "worksheet", "zone_el": None}) == MIN_WORKSHEET
    assert default_min_for_leaf({"leaf_kind": "filter", "zone_el": None}) == MIN_SLICER
    listmode = ET.fromstring("<zone type-v2='filter' mode='checklist'/>")
    mw, mh = default_min_for_leaf({"leaf_kind": "filter", "zone_el": listmode})
    assert mh == MIN_SLICER_LIST_H and mw == MIN_SLICER[0]


# -- opt-in real-workbook gate --------------------------------------------------
@pytest.mark.skipif(not os.environ.get("TFMIG_ZONE_TREE_TWB"),
                    reason="opt-in: set TFMIG_ZONE_TREE_TWB to a real .twb to run the solver gate")
def test_real_workbook_solves_without_flow_overlap():
    # Slice-2 gate: the solver never raises on a real tree and produces ZERO flow overlaps on every
    # dashboard. (Containment is NOT asserted here: a real frame can squash a nested flow subtree
    # below its min -- an out-of-bounds concern owned by the quality track, never an overlap.)
    with open(os.environ["TFMIG_ZONE_TREE_TWB"], "r", encoding="utf-8-sig") as fh:
        root_el = ET.fromstring(fh.read())
    dashboards = [d for d in root_el.iter() if d.tag.rsplit("}", 1)[-1] == "dashboard"]
    assert dashboards, "workbook has no dashboards"
    for db in dashboards:
        dev = set()
        for holder in db.iter():
            if holder.tag.rsplit("}", 1)[-1] == "devicelayouts":
                for z in holder.iter():
                    if z.tag.rsplit("}", 1)[-1] == "zone":
                        dev.add(z)
        tree = parse_zone_tree(db, dev)
        if tree is None:
            continue
        res = solve(tree, (0, 0, 1280, 720))
        assert res is not None, "solver returned None on %r" % db.get("name")
        for cont in _iter_flow(tree["root"]):
            kids = cont["children"]
            for i in range(len(kids)):
                for j in range(i + 1, len(kids)):
                    assert not _overlap_area_pos(kids[i]["rect"], kids[j]["rect"]), (
                        "dashboard %r: solved flow overlap %r / %r"
                        % (db.get("name"), kids[i]["rect"], kids[j]["rect"]))
