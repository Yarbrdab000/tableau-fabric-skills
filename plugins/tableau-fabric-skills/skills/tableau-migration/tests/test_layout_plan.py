"""Per-dashboard layout PLAN -- Zone Geometry v3 frame track slice 4b.

``layout_plan.build_plan`` is the single seam that runs the three pure layout modules together and
hands the emit path one lookup table: ``zone_tree`` (parse) -> ``layout_solve`` (resolve) ->
``layout_layers`` (classify), keyed by the ``zone_id`` slice 4a records on every captured item.
These tests lock:

  * LOOKUP  -- every leaf in the tree gets a page-pixel rect and a leaf kind, keyed by zone id,
               and the ``_parse_dashboard`` round-trip resolves (the emit-facing contract),
  * FLOATS  -- a hoisted ``floating='true'`` zone IS placed. ``layout_solve.solve`` alone never
               allocates one (``zone_tree`` lifts it out of the flow), so an emit path built on the
               solver alone would silently LOSE those visuals. Placement scales the absolute source
               rect into the page, applies the leaf minimum, and clamps on-page,
  * LAYERS  -- the three z-order tiers are exposed as id sets and collapsed by ``is_decoration``,
  * GROWTH  -- page growth is REPORTED (``grew``) rather than silently applied,
  * CLOSED  -- every failure path returns ``None`` and NEVER raises.
"""
import xml.etree.ElementTree as ET

import layout_plan as LP
import twb_to_pbir as R
from layout_plan import build_plan, is_decoration
from layout_solve import GAP, MIN_ABSOLUTE, MIN_TEXT, solve
from zone_tree import parse_zone_tree

_WS = {"WsA", "WsB"}


def _dash(zones_xml):
    return ET.fromstring("<dashboard name='D'><zones>%s</zones></dashboard>" % zones_xml)


# a layout-basic root over a vstack of [text banner, hstack(WsA, WsB)]
_FLOW = (
    "<zone id='r' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
    "<zone id='v' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
    "<zone id='t' type-v2='text' x='0' y='0' w='100000' h='10000'/>"
    "<zone id='h' type-v2='layout-flow' param='horz' x='0' y='10000' w='100000' h='90000'>"
    "<zone id='a' name='WsA' x='0' y='10000' w='50000' h='90000'/>"
    "<zone id='b' name='WsB' x='50000' y='10000' w='50000' h='90000'/>"
    "</zone></zone></zone>"
)


# -- lookup table --------------------------------------------------------------
def test_plan_gives_every_leaf_a_rect_and_a_kind():
    p = build_plan(_dash(_FLOW), page_w=1000.0, page_h=800.0)
    assert p is not None
    for zid in ("t", "a", "b"):
        x, y, w, h = p["rects"][zid]
        assert w > 0 and h > 0
    assert p["kinds"]["t"] == "text"
    assert p["kinds"]["a"] == "worksheet" and p["kinds"]["b"] == "worksheet"


def test_flow_siblings_stay_disjoint_in_the_plan():
    p = build_plan(_dash(_FLOW), page_w=1000.0, page_h=800.0)
    ax, ay, aw, ah = p["rects"]["a"]
    bx, by, bw, bh = p["rects"]["b"]
    assert ax + aw <= bx + 1.0            # hstack children partition the x axis


def test_plan_page_matches_the_request_when_nothing_needs_to_grow():
    p = build_plan(_dash(_FLOW), page_w=1000.0, page_h=800.0)
    assert p["page"] == (1000.0, 800.0) and p["grew"] is False


def test_parse_dashboard_zone_ids_all_resolve_in_the_plan():
    # the emit-facing contract: slice 4a's captured zone_id is a valid key into slice 4b's rects
    db = _dash(_FLOW)
    p = build_plan(db, page_w=1000.0, page_h=800.0)
    parsed = R._parse_dashboard(db, _WS, [])
    ids = [z["zone_id"] for z in parsed["zones"]] + [t["zone_id"] for t in parsed["text_objects"]]
    assert ids
    for zid in ids:
        assert zid in p["rects"], zid


# -- hoisted floats ------------------------------------------------------------
_FLOAT = (
    "<zone id='r' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
    "<zone id='a' name='WsA' x='0' y='0' w='100000' h='100000'/>"
    "<zone id='f' type-v2='text' floating='true' x='50000' y='50000' w='20000' h='10000'/>"
    "</zone>"
)


def test_solver_alone_loses_a_floating_zone():
    # the gap this module closes -- documents WHY float placement lives here
    tree = parse_zone_tree(_dash(_FLOAT))
    assert [n["zone_id"] for n in tree["floats"]] == ["f"]
    assert "f" not in solve(tree, (0.0, 0.0, 1000.0, 1000.0))["rects"]


def test_plan_places_a_floating_zone():
    p = build_plan(_dash(_FLOAT), page_w=1000.0, page_h=1000.0)
    assert p["rects"]["f"] == (500.0, 500.0, 200.0, 100.0)
    assert p["kinds"]["f"] == "text"


def test_floating_zone_is_clamped_onto_the_page():
    xml = _FLOAT.replace("x='50000' y='50000' w='20000' h='10000'",
                         "x='95000' y='95000' w='20000' h='10000'")
    x, y, w, h = build_plan(_dash(xml), page_w=1000.0, page_h=1000.0)["rects"]["f"]
    assert (x + w) <= 1000.0 + 1e-9 and (y + h) <= 1000.0 + 1e-9
    assert x >= 0.0 and y >= 0.0


def test_floating_zone_gets_the_leaf_minimum_applied():
    """A degenerate float is floored so it stays visible -- but only to MIN_ABSOLUTE, not to its
    kind minimum.

    The floor exists so a 1px zone does not emit as an invisible visual, and 16x16 satisfies that.
    Inflating it to the 120x32 text minimum would be growing an ABSOLUTELY positioned object by
    120x on one axis, and a float has no flow siblings to push -- it simply expands over whatever
    the author placed beneath it. That is not hypothetical: on a real dashboard a small floating
    table floored to its 160x94 kind minimum swallowed the slicer underneath it outright.
    """
    xml = _FLOAT.replace("x='50000' y='50000' w='20000' h='10000'",
                         "x='10000' y='10000' w='100' h='100'")
    x, y, w, h = build_plan(_dash(xml), page_w=1000.0, page_h=1000.0)["rects"]["f"]
    assert (w, h) == (MIN_ABSOLUTE, MIN_ABSOLUTE)
    assert (w, h) < MIN_TEXT, "a float is never inflated to its kind minimum"


def test_a_roomy_float_still_gets_its_full_kind_minimum():
    """The clamp is a ceiling, not a replacement: a float the author drew larger than its kind
    minimum keeps its authored size, and one drawn between MIN_ABSOLUTE and the kind minimum keeps
    exactly what the author drew."""
    xml = _FLOAT.replace("x='50000' y='50000' w='20000' h='10000'",
                         "x='10000' y='10000' w='5000' h='2000'")
    _x, _y, w, h = build_plan(_dash(xml), page_w=1000.0, page_h=1000.0)["rects"]["f"]
    assert (w, h) == (50.0, 20.0)


def test_leaves_inside_a_floating_container_are_placed_too():
    xml = ("<zone id='r' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
           "<zone id='a' name='WsA' x='0' y='0' w='100000' h='100000'/>"
           "<zone id='fc' type-v2='layout-flow' param='vert' floating='true'"
           " x='40000' y='40000' w='20000' h='20000'>"
           "<zone id='f1' type-v2='text' x='40000' y='40000' w='20000' h='10000'/>"
           "<zone id='f2' type-v2='text' x='40000' y='50000' w='20000' h='10000'/>"
           "</zone></zone>")
    p = build_plan(_dash(xml), page_w=1000.0, page_h=1000.0)
    assert "f1" in p["rects"] and "f2" in p["rects"]
    assert p["kinds"]["f1"] == "text" and p["kinds"]["f2"] == "text"


def test_a_dashboard_with_no_floats_is_unaffected():
    p = build_plan(_dash(_FLOW), page_w=1000.0, page_h=800.0)
    assert set(p["kinds"]) == {"t", "a", "b"}


# -- z-order tiers -------------------------------------------------------------
_LAYERED = (
    "<zone id='r' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
    "<zone id='bg' type-v2='bitmap' x='0' y='0' w='100000' h='100000'/>"       # backdrop
    "<zone id='pn' type-v2='text' x='0' y='0' w='60000' h='60000'/>"           # panel
    "<zone id='a' name='WsA' x='5000' y='5000' w='40000' h='40000'/>"          # content in panel
    "<zone id='ov' type-v2='filter' x='10000' y='10000' w='10000' h='6000'/>"  # overlay on content
    "</zone>"
)


def test_the_three_tiers_are_exposed_as_id_sets():
    p = build_plan(_dash(_LAYERED), page_w=1000.0, page_h=1000.0)
    assert "bg" in p["background"]
    assert "pn" in p["panel"]
    assert "ov" in p["overlay"]
    assert "a" not in p["background"] and "a" not in p["panel"] and "a" not in p["overlay"]


def test_is_decoration_collapses_the_tiers():
    p = build_plan(_dash(_LAYERED), page_w=1000.0, page_h=1000.0)
    assert all(is_decoration(p, z) for z in ("bg", "pn", "ov"))
    assert not is_decoration(p, "a")


def test_is_decoration_is_safe_on_unknown_and_missing_input():
    p = build_plan(_dash(_LAYERED), page_w=1000.0, page_h=1000.0)
    assert is_decoration(p, "nope") is False
    assert is_decoration(p, None) is False
    assert is_decoration(None, "bg") is False


def test_a_worksheet_is_never_classified_as_decoration():
    # the guardrail carried through from the classifier: a full-bleed WORKSHEET is content
    xml = ("<zone id='r' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
           "<zone id='a' name='WsA' x='0' y='0' w='100000' h='100000'/>"
           "<zone id='b' name='WsB' x='10000' y='10000' w='30000' h='30000'/></zone>")
    p = build_plan(_dash(xml), page_w=1000.0, page_h=1000.0)
    assert not is_decoration(p, "a") and not is_decoration(p, "b")


def test_blank_leaves_are_skipped_by_classification_but_keep_a_rect():
    xml = ("<zone id='r' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
           "<zone id='e' type-v2='empty' x='0' y='0' w='100000' h='100000'/>"
           "<zone id='a' name='WsA' x='0' y='0' w='50000' h='50000'/></zone>")
    p = build_plan(_dash(xml), page_w=1000.0, page_h=1000.0)
    assert p["kinds"]["e"] == "blank" and "e" in p["rects"]
    assert not is_decoration(p, "e")


# -- growth --------------------------------------------------------------------
def test_growth_is_reported_when_the_minimum_exceeds_the_page():
    """The plan reports growth -- and the growth is exactly the gaps the author left no room for.

    Five worksheets tile the canvas exactly, so the only thing that does not fit is the solver's own
    4 x 8px of inter-sibling gap. The page used to grow to 5 x MIN_WORKSHEET (800px on a 200px
    request) because each 40px worksheet demanded a 160px floor; a minimum may no longer exceed the
    size its author drew, so the demand is now the honest one.
    """
    stack = "".join("<zone id='w%d' name='WsA' x='0' y='%d' w='100000' h='20000'/>" % (i, i * 20000)
                    for i in range(5))
    xml = ("<zone id='v' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
           + stack + "</zone>")
    p = build_plan(_dash(xml), page_w=1000.0, page_h=200.0)
    assert p["grew"] is True
    assert abs(p["page"][1] - (200.0 + 4 * GAP)) <= 1.0


def test_growth_is_not_reported_on_a_roomy_page():
    assert build_plan(_dash(_FLOW), page_w=1600.0, page_h=1200.0)["grew"] is False


# -- fail-closed ---------------------------------------------------------------
def test_dashboard_without_zones_returns_none():
    assert build_plan(ET.fromstring("<dashboard name='D'/>")) is None


def test_source_overlap_premise_violation_returns_none():
    # two FLEXIBLE tiled siblings overlapping is a genuine premise violation -> tree fails closed
    xml = ("<zone id='v' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
           "<zone id='a' name='WsA' x='0' y='0' w='100000' h='60000'/>"
           "<zone id='b' name='WsB' x='0' y='30000' w='100000' h='70000'/></zone>")
    assert build_plan(_dash(xml)) is None


def test_non_positive_page_returns_none():
    assert build_plan(_dash(_FLOW), page_w=0.0, page_h=800.0) is None
    assert build_plan(_dash(_FLOW), page_w=1000.0, page_h=-5.0) is None


def test_garbage_input_returns_none_and_never_raises():
    for bad in (None, "not-an-element", 42, object()):
        assert build_plan(bad) is None
    assert build_plan(_dash(_FLOW), page_w="wide", page_h=800.0) is None


def test_plan_is_deterministic():
    a = build_plan(_dash(_LAYERED), page_w=1000.0, page_h=1000.0)
    b = build_plan(_dash(_LAYERED), page_w=1000.0, page_h=1000.0)
    assert a["rects"] == b["rects"] and a["kinds"] == b["kinds"]
    assert (a["background"], a["panel"], a["overlay"]) == (b["background"], b["panel"], b["overlay"])


def test_documented_tunables():
    assert LP.TOL == 1.0
    assert LP.SKIP_KINDS == ("blank",)
