"""Fixed-child SHARE tests -- Zone Geometry v3 frame track slice 4f.

Slice 4d wired the solver into emit, and the first end-to-end build of a real customer workbook
found what six workbooks of synthetic A/B scoring had not: on Salesforce "Service Delivery" a logo
authored as one of four equal quarters was emitted **26px wide**, beside three filter cards at
177/205/179. Legacy drew the same logo at 204x59.

The cause was one line in :func:`layout_solve.allocate`::

    avail = main_total - gaps - sum(fixed_px for c in fixed)

A fixed child's pixels were treated as a first claim on whatever box the container actually got.
But ``fixed_px`` is a request measured at the container's AUTHORED size, and this row's container
had itself been allocated less (611px against ~819 authored). Paying every fixed child in full out
of the shrunken box made the single FLEXIBLE child absorb 100% of the shortfall: 561 of 587px went
to the three cards and the logo took the 26px remainder.

Nothing caught it. The row never overran, so slice 4c's last-resort squeeze had nothing to fix; the
leaf cleared ``MIN_BITMAP`` (24px), so the freeze loop saw no violator. It was *satisfied* and
wildly out of proportion -- which is why only a real workbook surfaced it.

The rule these tests pin: cap the fixed children's collective demand at the room NOT authored to
flexible siblings and scale them proportionally into it, never below their own minimum and **never
above their own request**. A squeeze becomes SHARED rather than dumped on whichever sibling happens
to be flexible. It is a no-op whenever the container has room for everyone, which is the common
case and why the corpus result is unchanged.

The same slice closes a reachability gap found alongside it: ``--layout`` existed only on
``twb_to_pbir``'s CLI, so the engine could not be selected from ``migrate_estate`` -- the one-button
entry point users actually run. The second half of this file pins that seam.
"""
import xml.etree.ElementTree as ET

import pytest

import migrate_estate as me
from layout_solve import GAP, MIN_SLICER, TOL, solve
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


def _row(*cells):
    """A frame holding one horizontal flow row, so the row is handed the full page width."""
    return ("<zone id='f' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
            "<zone id='h' type-v2='layout-flow' param='horz' x='0' y='0' w='100000' h='12500'>"
            + "".join(cells) + "</zone></zone>")


def _filter(zid, x, fixed):
    return ("<zone id='%s' type-v2='filter' is-fixed='true' fixed-size='%d' "
            "x='%d' y='0' w='25000' h='12500'/>" % (zid, fixed, x))


# The shipped shape of the bug: four zones authored as equal quarters, three of them pinned.
_QUARTERS = _row(
    _filter("a", 0, 177),
    _filter("b", 25000, 205),
    _filter("c", 50000, 179),
    "<zone id='d' type-v2='bitmap' x='75000' y='0' w='25000' h='12500'/>",
)

# 611px box, 3 gaps -> 587px of content room; the three pins want 561 of it.
_TIGHT = (0.0, 0.0, 611.0, 400.0)


# -- (1) the regression ---------------------------------------------------------
def test_a_flexible_sibling_keeps_roughly_its_authored_share():
    # Before the fix this leaf was 26px -- 4% of the row instead of the authored 25%.
    tree = parse_zone_tree(_dash(_QUARTERS))
    res = solve(tree, _TIGHT, grow=False)
    room = _TIGHT[2] - 3 * GAP
    assert res["rects"]["d"][2] > 0.75 * (room / 4.0)


def test_the_squeeze_is_shared_not_dumped_on_the_flexible_child():
    # Every pinned sibling gives up pixels too, rather than one child paying the whole shortfall.
    res = solve(parse_zone_tree(_dash(_QUARTERS)), _TIGHT, grow=False)
    for zid, request in (("a", 177.0), ("b", 205.0), ("c", 179.0)):
        assert res["rects"][zid][2] < request - TOL


def test_pinned_siblings_keep_their_relative_proportions():
    res = solve(parse_zone_tree(_dash(_QUARTERS)), _TIGHT, grow=False)
    wa, wb = res["rects"]["a"][2], res["rects"]["b"][2]
    assert wa / wb == pytest.approx(177.0 / 205.0, rel=1e-6)


# -- (2) the rule is a no-op when the box is roomy ------------------------------
def test_pinned_children_get_their_exact_pixels_when_there_is_room():
    # The common case must be untouched: this is why the corpus A/B did not move.
    res = solve(parse_zone_tree(_dash(_QUARTERS)), (0.0, 0.0, 1400.0, 400.0), grow=False)
    assert res["rects"]["a"][2] == pytest.approx(177.0, abs=TOL)
    assert res["rects"]["b"][2] == pytest.approx(205.0, abs=TOL)
    assert res["rects"]["c"][2] == pytest.approx(179.0, abs=TOL)


def test_an_all_fixed_row_is_never_rescaled_by_this_rule():
    # With no flexible sibling there is nobody to starve, so the rule must not fire at all.
    xml = _row(_filter("a", 0, 177), _filter("b", 25000, 205), _filter("c", 50000, 179))
    res = solve(parse_zone_tree(_dash(xml)), (0.0, 0.0, 1400.0, 400.0), grow=False)
    assert res["rects"]["a"][2] == pytest.approx(177.0, abs=TOL)
    assert res["rects"]["c"][2] == pytest.approx(179.0, abs=TOL)


# -- (3) bounds: only ever takes pixels off, never adds --------------------------
def test_a_pin_smaller_than_its_own_min_keeps_the_pinned_size():
    # Flooring at the child's min must not RAISE a child the author pinned below that min -- an
    # earlier cut of this fix did, inflating a 58px text zone to 120px and starving its sibling.
    small = int(MIN_SLICER[0]) - 80
    xml = _row(_filter("a", 0, small), _filter("b", 25000, 400), _filter("c", 50000, 400),
               "<zone id='d' type-v2='bitmap' x='75000' y='0' w='25000' h='12500'/>")
    res = solve(parse_zone_tree(_dash(xml)), _TIGHT, grow=False)
    assert res["rects"]["a"][2] <= small + TOL


def test_a_pinned_child_is_not_scaled_below_its_own_minimum():
    xml = _row(_filter("a", 0, 300), _filter("b", 25000, 300), _filter("c", 50000, 300),
               "<zone id='d' type-v2='bitmap' x='75000' y='0' w='25000' h='12500'/>")
    res = solve(parse_zone_tree(_dash(xml)), _TIGHT, grow=False)
    assert res["rects"]["a"][2] >= MIN_SLICER[0] - TOL


# -- (4) the slice 4c invariant still holds -------------------------------------
def test_flow_containment_still_unconditional():
    tree = parse_zone_tree(_dash(_QUARTERS))
    res = solve(tree, _TIGHT, grow=False)
    pw, ph = res["page"][2], res["page"][3]
    for leaf in _leaves(tree["root"]):
        x, y, w, h = leaf["rect"]
        assert x >= -TOL and y >= -TOL and x + w <= pw + TOL and y + h <= ph + TOL


def test_squeezed_children_remain_disjoint():
    res = solve(parse_zone_tree(_dash(_QUARTERS)), _TIGHT, grow=False)
    rs = sorted((res["rects"][z] for z in "abcd"), key=lambda r: r[0])
    for lo, hi in zip(rs, rs[1:]):
        assert lo[0] + lo[2] <= hi[0] + TOL


def test_the_rule_applies_on_the_vertical_axis_too():
    xml = ("<zone id='f' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
           "<zone id='v' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
           "<zone id='a' name='WsA' is-fixed='true' fixed-size='300' x='0' y='0' w='100000' h='50000'/>"
           "<zone id='d' type-v2='bitmap' x='0' y='50000' w='100000' h='50000'/>"
           "</zone></zone>")
    res = solve(parse_zone_tree(_dash(xml)), (0.0, 0.0, 800.0, 320.0), grow=False)
    assert res["rects"]["d"][3] > 0.5 * ((320.0 - GAP) / 2.0)


# -- (5) the estate seam --------------------------------------------------------
def test_viz_adapter_forwards_layout_only_when_supported():
    # Additive against older viz entry points: a stage with no `layout` param is called as before.
    seen = {}

    def viz_with(text, *, report_name, dataset_name, layout=None):
        seen["with"] = layout
        return {"parts": {}}

    def viz_without(text, *, report_name, dataset_name):
        seen["without"] = True
        return {"parts": {}}

    me._viz_adapter(viz_with, layout="solver")("<twb/>", "WB")
    me._viz_adapter(viz_without, layout="solver")("<twb/>", "WB")
    assert seen["with"] == "solver"
    assert seen["without"] is True  # called without raising despite no layout param


def test_viz_adapter_omits_layout_when_unset():
    # An unset engine must not be forwarded at all, so the viz stage keeps its own default.
    seen = {}

    def viz(text, *, report_name, dataset_name, layout="legacy"):
        seen["layout"] = layout
        return {"parts": {}}

    me._viz_adapter(viz)("<twb/>", "WB")
    assert seen["layout"] == "legacy"


def test_resolve_viz_stage_returns_an_injected_stage_untouched():
    # A caller supplying its own viz stage owns its own configuration.
    injected = lambda text, name: {"parts": {}}
    assert me._resolve_viz_stage(injected, layout="solver") is injected


def test_migrate_estate_and_migrate_workbook_accept_layout():
    import inspect
    assert "layout" in inspect.signature(me.migrate_estate).parameters
    assert "layout" in inspect.signature(me.migrate_workbook).parameters


def test_cli_exposes_layout_and_defaults_to_legacy(tmp_path, monkeypatch):
    # The flag has to reach the ONE-BUTTON entry point, and the default must stay legacy so an
    # existing run is byte-identical until a user opts in.
    src = tmp_path / "in"
    src.mkdir()
    (src / "d.twb").write_text(
        "<workbook><dashboards><dashboard name='D'><zones/></dashboard></dashboards></workbook>",
        encoding="utf-8")
    seen = {}
    real = me.migrate_estate

    def spy(source, output_dir, **kw):
        seen.update(kw)
        return real(source, output_dir, **kw)

    monkeypatch.setattr(me, "migrate_estate", spy)
    me.main(["-i", str(src), "-o", str(tmp_path / "a")])
    assert seen["layout"] == "legacy"

    seen.clear()
    me.main(["-i", str(src), "-o", str(tmp_path / "b"), "--layout", "solver"])
    assert seen["layout"] == "solver"
