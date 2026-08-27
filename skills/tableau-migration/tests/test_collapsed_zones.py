"""A zone the AUTHOR collapsed to zero takes NO share of its flow container.

Tableau hides a zone by collapsing it to ``w=0`` (or ``h=0``). That is the ordinary mechanism behind
a parameter-driven show/hide pair -- two sheets share one slot and a parameter decides which is
drawn, which is how every "vs Goal / vs PY" toggle is built.

The layout solver had no guard for it, and neither of the two things it already reasons about
covers the case: ``SKIP_KINDS`` skips a ``blank`` spacer, and its comment weighs ``hidden-by-user``
and deliberately KEEPS it (a show/hide toggle is not a delete, and the emit path surfaces such a
zone anyway, so it occupies real page area). A collapsed zone is a THIRD mechanism and walked past
both -- reaching ``default_min_for_leaf``, being floored to a real box by its ``leaf_kind``, and
taking a full share of a container that has none to give.

Measured on a Salesforce NPSP dashboard with four such pairs: each hidden twin claimed ~160px of an
``hstack`` sized for one sheet, displacing its VISIBLE twin by up to 179px and halving its width
(277 -> 160). ``_place_float`` already declines exactly this condition for an absolutely-positioned
zone (``if sw <= 0 or sh <= 0: return None``) and says why -- inflating something with no flow
siblings simply grows it over its neighbour. This is that rule for the flow path.
"""
import xml.etree.ElementTree as ET

from layout_plan import build_plan
from zone_tree import parse_zone_tree

PAGE = (1000.0, 800.0)


def _dash(zones_xml):
    return ET.fromstring("<dashboard name='D'><zones>%s</zones></dashboard>" % zones_xml)


def _root(inner):
    return ("<zone id='r' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
            + inner + "</zone>")


# One hstack, two worksheets sharing a slot, the FIRST collapsed to zero -- the show/hide pair.
_HIDDEN_FIRST = _dash(_root(
    "<zone id='h' type-v2='layout-flow' param='horz' x='0' y='0' w='100000' h='100000'>"
    "<zone id='hidden' name='vsGoal' x='0' y='0' w='0' h='100000'/>"
    "<zone id='shown' name='vsPY' x='0' y='0' w='100000' h='100000'/>"
    "</zone>"))

# The same pair with the collapsed twin SECOND, because "first child" and "zero width" are
# different properties and a guard keyed on position rather than size would pass the test above.
_HIDDEN_SECOND = _dash(_root(
    "<zone id='h' type-v2='layout-flow' param='horz' x='0' y='0' w='100000' h='100000'>"
    "<zone id='shown' name='vsPY' x='0' y='0' w='100000' h='100000'/>"
    "<zone id='hidden' name='vsGoal' x='100000' y='0' w='0' h='100000'/>"
    "</zone>"))

# Both twins visible: the control that shows the guard is keyed on SIZE, not on sharing a slot.
_BOTH_VISIBLE = _dash(_root(
    "<zone id='h' type-v2='layout-flow' param='horz' x='0' y='0' w='100000' h='100000'>"
    "<zone id='left' name='vsPY' x='0' y='0' w='50000' h='100000'/>"
    "<zone id='right' name='vsGoal' x='50000' y='0' w='50000' h='100000'/>"
    "</zone>"))


def _rects(dash):
    plan = build_plan(dash, device_zones=set(), page_w=PAGE[0], page_h=PAGE[1])
    assert plan is not None, "solver declined the fixture -- the test would prove nothing"
    return plan


def test_the_fixture_really_puts_two_zones_in_one_slot():
    """Anti-vacuity. If the tree did not actually contain both children, every assertion below
    would pass for the wrong reason -- there would be nothing to apportion."""
    tree = parse_zone_tree(_HIDDEN_FIRST, set())
    kids = tree["root"]["children"][0].get("children") or []
    assert len(kids) == 2, "fixture has %d children in the hstack, expected 2" % len(kids)
    widths = sorted(float((k.get("src") or {}).get("w") or 0) for k in kids)
    assert widths[0] == 0.0 and widths[1] > 0.0, "fixture is not a hidden/shown pair: %s" % widths


def test_a_collapsed_twin_takes_no_width_from_the_visible_one():
    plan = _rects(_HIDDEN_FIRST)
    x, _y, w, _h = plan["rects"]["shown"]
    assert abs(x) < 1.0, "visible twin pushed to x=%.1f by a zone the author hid" % x
    assert w > PAGE[0] * 0.95, "visible twin squeezed to %.0f of %.0f px" % (w, PAGE[0])


def test_the_guard_is_keyed_on_SIZE_not_on_child_position():
    """Same pair, collapsed twin second. A guard that special-cased 'the first child' would pass
    the test above and fail here."""
    plan = _rects(_HIDDEN_SECOND)
    x, _y, w, _h = plan["rects"]["shown"]
    assert abs(x) < 1.0 and w > PAGE[0] * 0.95, "got x=%.1f w=%.1f" % (x, w)


def test_two_visible_siblings_still_split_the_container():
    """The negative that keeps the guard from degrading into 'one child per container'."""
    plan = _rects(_BOTH_VISIBLE)
    lw = plan["rects"]["left"][2]
    rw = plan["rects"]["right"][2]
    assert lw < PAGE[0] * 0.75 and rw < PAGE[0] * 0.75, \
        "a visible sibling was dropped: left=%.0f right=%.0f" % (lw, rw)
    assert abs(lw - rw) < PAGE[0] * 0.1, "even split expected, got %.0f / %.0f" % (lw, rw)


def test_a_collapsed_zone_gets_no_rect_and_is_reported():
    """It must not simply vanish: an absent rect and a deliberate exclusion look identical to a
    reader, so the plan names what it removed."""
    plan = _rects(_HIDDEN_FIRST)
    assert "hidden" not in plan["rects"], "a collapsed zone was allocated a rectangle"
    assert "hidden" in plan["collapsed"], "the collapsed zone was dropped without being reported"
    assert "shown" not in plan["collapsed"]


def test_a_zero_HEIGHT_zone_is_collapsed_too():
    """Tableau collapses on either axis; a vertical show/hide pair is the same idiom rotated."""
    dash = _dash(_root(
        "<zone id='v' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
        "<zone id='hidden' name='vsGoal' x='0' y='0' w='100000' h='0'/>"
        "<zone id='shown' name='vsPY' x='0' y='0' w='100000' h='100000'/>"
        "</zone>"))
    plan = _rects(dash)
    _x, y, _w, h = plan["rects"]["shown"]
    assert abs(y) < 1.0, "visible twin pushed to y=%.1f" % y
    assert h > PAGE[1] * 0.95, "visible twin squeezed to %.0f of %.0f px" % (h, PAGE[1])
    assert "hidden" in plan["collapsed"]


def test_collapsing_a_CONTAINER_removes_its_whole_subtree():
    """Tableau collapses the group, not each member. Every leaf under it must be reported, or a
    reader sees some of a hidden band accounted for and some of it simply missing."""
    dash = _dash(_root(
        "<zone id='h' type-v2='layout-flow' param='horz' x='0' y='0' w='100000' h='100000'>"
        "<zone id='hgroup' type-v2='layout-flow' param='vert' x='0' y='0' w='0' h='100000'>"
        "<zone id='ha' name='A' x='0' y='0' w='0' h='50000'/>"
        "<zone id='hb' name='B' x='0' y='50000' w='0' h='50000'/>"
        "</zone>"
        "<zone id='shown' name='vsPY' x='0' y='0' w='100000' h='100000'/>"
        "</zone>"))
    plan = _rects(dash)
    assert plan["rects"]["shown"][2] > PAGE[0] * 0.95
    for zid in ("hgroup", "ha", "hb"):
        assert zid in plan["collapsed"], "%s not reported as collapsed" % zid
        assert zid not in plan["rects"], "%s was allocated a rectangle" % zid


def test_a_dashboard_of_only_collapsed_zones_still_fails_closed():
    """Pruning must never leave the solver an empty tree it cannot resolve -- and if it does, the
    plan is None and the emit path keeps its legacy scale, which is the fail-closed contract."""
    dash = _dash(_root(
        "<zone id='h' type-v2='layout-flow' param='horz' x='0' y='0' w='100000' h='100000'>"
        "<zone id='x1' name='A' x='0' y='0' w='0' h='100000'/>"
        "<zone id='x2' name='B' x='0' y='0' w='0' h='100000'/>"
        "</zone>"))
    plan = build_plan(dash, device_zones=set(), page_w=PAGE[0], page_h=PAGE[1])
    if plan is not None:
        assert "x1" in plan["collapsed"] and "x2" in plan["collapsed"]
        assert "x1" not in plan["rects"] and "x2" not in plan["rects"]


def test_an_ordinary_dashboard_is_untouched():
    """The byte-identical guarantee for every dashboard with no collapsed zone: same rects as
    before, and an empty ``collapsed`` set."""
    plan = _rects(_BOTH_VISIBLE)
    assert plan["collapsed"] == frozenset()
    assert set(plan["rects"]) >= {"left", "right"}
