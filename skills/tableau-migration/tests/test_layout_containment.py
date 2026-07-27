"""Solver CONTAINMENT tests -- Zone Geometry v3 frame track slice 4c.

Slice 2 shipped the flow solver with an honest hole: a ``frame`` (Tableau ``layout-basic``) hands
each child ``child_src / frame_src`` of its own box, but ``compute_mins`` took a frame's min as
``max(child min)``. That UNDERSTATES the frame's requirement by exactly that fraction -- a child
occupying 40% of the frame's height needs the frame to be ``child_min / 0.4`` tall, not
``child_min`` -- so the frame looked satisfied while its children were starved. A starved flow child
then overran its own box, because its fixed-size and min-clamped grandchildren sum past whatever it
was given and the advancing cursor simply walked off the end. Measured on the six-workbook corpus:
**7 out-of-bounds leaves across 4 dashboards, on pages the solver reported as not needing to grow.**

Three coupled changes close it, and these tests pin all three plus the invariant they buy:

  1. a frame's min INVERTS each child's source fraction,
  2. that demand is CAPPED at :data:`MAX_GROWTH` x the requested page, because inverting a near-zero
     fraction explodes (one corpus sliver would demand a 164384px canvas),
  3. a flow container SCALES its allocations down when they still exceed its box -- the last-resort
     step that makes flow containment unconditional rather than conditional on the cap being generous.

Corpus result: overlaps 0, containment 0, out-of-bounds 0, floor 32 (from 0 / 1 / 7 / 52), which is
parity with the legacy path's 0 / 0 / 0 / 31.
"""
import xml.etree.ElementTree as ET

import pytest

from layout_solve import (
    GAP,
    MAX_GROWTH,
    MIN_WORKSHEET,
    TOL,
    compute_mins,
    default_min_for_leaf,
    solve,
)
from zone_tree import K_LEAF, parse_zone_tree


def _dash(zones_xml):
    return ET.fromstring("<dashboard name='D'><zones>%s</zones></dashboard>" % zones_xml)


def _leaves(node, out=None):
    out = [] if out is None else out
    if node["kind"] == K_LEAF:
        out.append(node)
    for c in node.get("children", ()):
        _leaves(c, out)
    return out


def _on_page(rect, pw, ph, tol=TOL):
    x, y, w, h = rect
    return x >= -tol and y >= -tol and x + w <= pw + tol and y + h <= ph + tol


# -- the tunable ----------------------------------------------------------------
def test_max_growth_is_the_measured_knee():
    # 1.25x/1.5x/2x drove the corpus floor count 47 -> 38 -> 32 and 2.5x/3x also gave 32, so 2x buys
    # the whole benefit; growing further only shrinks every visual under FitToPage.
    assert MAX_GROWTH == 2.0


# -- (1) a frame's min inverts the child's source fraction ----------------------
def test_frame_min_inverts_the_child_source_fraction():
    # child occupies the lower 40% of the frame -> the frame must be 1/0.4 = 2.5x the child's min
    xml = ("<zone id='f' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
           "<zone id='v' type-v2='layout-flow' param='vert' x='0' y='60000' w='100000' h='40000'>"
           "<zone id='a' name='WsA' x='0' y='60000' w='100000' h='40000'/>"
           "</zone></zone>")
    tree = parse_zone_tree(_dash(xml))
    compute_mins(tree["root"], default_min_for_leaf)
    assert tree["root"]["min_h"] == pytest.approx(MIN_WORKSHEET[1] / 0.4, rel=1e-6)


def test_a_full_bleed_frame_child_does_not_inflate_the_frame_min():
    xml = ("<zone id='f' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
           "<zone id='a' name='WsA' x='0' y='0' w='100000' h='100000'/></zone>")
    tree = parse_zone_tree(_dash(xml))
    compute_mins(tree["root"], default_min_for_leaf)
    assert tree["root"]["min_h"] == pytest.approx(MIN_WORKSHEET[1])
    assert tree["root"]["min_w"] == pytest.approx(MIN_WORKSHEET[0])


def test_degenerate_frame_source_falls_back_to_the_child_min():
    # a zero-extent frame source makes _scale_abs fill the frame, so there is no fraction to invert
    xml = ("<zone id='f' type-v2='layout-basic' x='0' y='0' w='0' h='0'>"
           "<zone id='a' name='WsA' x='0' y='0' w='100000' h='100000'/></zone>")
    tree = parse_zone_tree(_dash(xml))
    if tree is None:
        pytest.skip("degenerate frame source rejected at parse time")
    compute_mins(tree["root"], default_min_for_leaf)
    assert tree["root"]["min_h"] == pytest.approx(MIN_WORKSHEET[1])


def test_compute_mins_cap_is_optional_and_backward_compatible():
    tree = parse_zone_tree(_dash(
        "<zone id='v' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
        "<zone id='a' name='WsA' x='0' y='0' w='100000' h='100000'/></zone>"))
    compute_mins(tree["root"], default_min_for_leaf)          # no cap arg
    assert tree["root"]["min_h"] > 0


def test_flow_mins_are_never_capped():
    # only FRAME demand is speculative; a flow min is an exact sum of real content requirements
    xml = ("<zone id='v' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
           + "".join("<zone id='w%d' name='WsA' x='0' y='%d' w='100000' h='10000'/>" % (i, i * 10000)
                     for i in range(10)) + "</zone>")
    tree = parse_zone_tree(_dash(xml))
    compute_mins(tree["root"], default_min_for_leaf, (100.0, 100.0))
    assert tree["root"]["min_h"] >= 10 * MIN_WORKSHEET[1]


# -- (2) the growth cap ---------------------------------------------------------
_SLIVER = ("<zone id='f' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
           "<zone id='a' name='WsA' x='0' y='0' w='100000' h='100000'/>"
           "<zone id='s' type-v2='filter' x='0' y='0' w='300' h='100000'/></zone>")


def test_a_sliver_child_cannot_explode_the_page():
    # 0.3% of the width; uncapped, a 120px slicer minimum would demand a ~40000px canvas
    res = solve(parse_zone_tree(_dash(_SLIVER)), (0.0, 0.0, 1000.0, 800.0))
    assert res is not None
    assert res["page"][2] <= 1000.0 * MAX_GROWTH + 1.0


def test_growth_never_exceeds_the_ceiling_on_either_axis():
    for xml in (_SLIVER,
                "<zone id='f' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
                "<zone id='t' type-v2='text' x='0' y='0' w='100000' h='200'/></zone>"):
        res = solve(parse_zone_tree(_dash(xml)), (0.0, 0.0, 900.0, 700.0))
        assert res is not None
        assert res["page"][2] <= 900.0 * MAX_GROWTH + 1.0
        assert res["page"][3] <= 700.0 * MAX_GROWTH + 1.0


def test_the_page_still_grows_when_growth_is_modest():
    xml = ("<zone id='f' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
           "<zone id='a' name='WsA' x='0' y='50000' w='100000' h='50000'/></zone>")
    res = solve(parse_zone_tree(_dash(xml)), (0.0, 0.0, 1000.0, 200.0))
    assert res["page"][3] == pytest.approx(2.0 * MIN_WORKSHEET[1], abs=1.0)


# -- (3) unconditional flow containment -----------------------------------------
_REGRESSION = (
    # the shipped shape of the bug: a frame gives a vstack 40% of the page, and the vstack holds a
    # fixed-size child far taller than that share -- the cursor used to walk straight off the page
    "<zone id='f' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
    "<zone id='v' type-v2='layout-flow' param='vert' x='0' y='60000' w='100000' h='40000'>"
    "<zone id='a' name='WsA' is-fixed='true' fixed-size='1077' x='0' y='60000' w='100000' h='40000'/>"
    "</zone></zone>")


def test_regression_an_oversized_fixed_child_stays_on_the_page():
    res = solve(parse_zone_tree(_dash(_REGRESSION)), (0.0, 0.0, 1366.0, 768.0))
    assert res is not None
    pw, ph = res["page"][2], res["page"][3]
    for zid, r in res["rects"].items():
        assert _on_page(r, pw, ph), (zid, r, pw, ph)


def test_every_leaf_is_on_page_for_a_starved_frame_child():
    xml = ("<zone id='f' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
           "<zone id='v' type-v2='layout-flow' param='vert' x='0' y='80000' w='100000' h='20000'>"
           + "".join("<zone id='w%d' name='WsA' x='0' y='%d' w='100000' h='4000'/>"
                     % (i, 80000 + i * 4000) for i in range(5)) + "</zone></zone>")
    tree = parse_zone_tree(_dash(xml))
    res = solve(tree, (0.0, 0.0, 1000.0, 600.0))
    assert res is not None
    pw, ph = res["page"][2], res["page"][3]
    for leaf in _leaves(tree["root"]):
        assert _on_page(leaf["rect"], pw, ph), (leaf["zone_id"], leaf["rect"])


def test_containment_holds_even_with_growth_disabled():
    tree = parse_zone_tree(_dash(_REGRESSION))
    res = solve(tree, (0.0, 0.0, 1366.0, 768.0), grow=False)
    assert res is not None
    assert res["page"][2:] == (1366.0, 768.0)          # grow=False really does not grow
    for leaf in _leaves(tree["root"]):
        assert _on_page(leaf["rect"], 1366.0, 768.0), (leaf["zone_id"], leaf["rect"])


def test_the_squeeze_preserves_relative_proportions():
    # two fixed children at 3:1 whose sum far exceeds the box keep that ratio after scaling down
    xml = ("<zone id='f' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
           "<zone id='v' type-v2='layout-flow' param='vert' x='0' y='90000' w='100000' h='10000'>"
           "<zone id='a' name='WsA' is-fixed='true' fixed-size='900' x='0' y='90000' w='100000' h='7500'/>"
           "<zone id='b' name='WsB' is-fixed='true' fixed-size='300' x='0' y='97500' w='100000' h='2500'/>"
           "</zone></zone>")
    tree = parse_zone_tree(_dash(xml))
    res = solve(tree, (0.0, 0.0, 800.0, 400.0))
    assert res is not None
    ha, hb = res["rects"]["a"][3], res["rects"]["b"][3]
    assert ha / hb == pytest.approx(3.0, rel=1e-6)


def test_squeezed_children_remain_disjoint():
    xml = ("<zone id='f' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
           "<zone id='h' type-v2='layout-flow' param='horz' x='0' y='0' w='10000' h='100000'>"
           + "".join("<zone id='c%d' type-v2='filter' x='%d' y='0' w='2000' h='100000'/>"
                     % (i, i * 2000) for i in range(5)) + "</zone></zone>")
    tree = parse_zone_tree(_dash(xml))
    res = solve(tree, (0.0, 0.0, 600.0, 400.0))
    assert res is not None
    rs = sorted((res["rects"]["c%d" % i] for i in range(5)), key=lambda r: r[0])
    for a, b in zip(rs, rs[1:]):
        assert a[0] + a[2] <= b[0] + TOL


def test_no_negative_sizes_when_the_box_is_tiny():
    tree = parse_zone_tree(_dash(_REGRESSION))
    res = solve(tree, (0.0, 0.0, 60.0, 40.0), grow=False)
    assert res is not None
    for leaf in _leaves(tree["root"]):
        assert leaf["rect"][2] >= 0.0 and leaf["rect"][3] >= 0.0


def test_solve_still_fails_closed_and_never_raises():
    assert solve(None, (0.0, 0.0, 100.0, 100.0)) is None
    # a non-positive page is only fatal when growth cannot rescue it
    assert solve({"root": {"kind": "leaf"}}, (0.0, 0.0, 0.0, 0.0), grow=False) is None
    assert solve(parse_zone_tree(_dash(_REGRESSION)), "nope") is None
