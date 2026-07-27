"""Zone-tree parser tests (offline, inline XML fixtures) -- Zone Geometry v3 slice 1.

``zone_tree.parse_zone_tree`` reconstructs a Tableau dashboard's nested flow layout from its
``<zones>`` element into a tree of vstack / hstack / frame / leaf nodes, or fails CLOSED to ``None``
when the zones are not a recognizable overlap-free flow tree. These tests lock:

* tree shape + kind classification + main-axis fraction on synthetic nested flow XML,
* leaf-kind taxonomy (worksheet / text / filter / paramctrl / legend / bitmap / blank),
* float hoisting and the ``<devicelayouts>`` exclusion,
* the **fixed-size-aware source-overlap premise** -- a raw-source overlap involving an
  ``is-fixed`` zone is a benign persistence artifact (tree still parses), while two *flexible*
  tiled siblings overlapping is a genuine premise violation (tree fails closed),
* every fail-closed trigger returns ``None`` and NEVER raises.

An opt-in golden (``TFMIG_ZONE_TREE_TWB``) asserts the premise on a real workbook without committing
any ``.twb`` -- the standing repro is ATTI_ATTR Hierarchy, whose 3 dashboards each parse to an
overlap-free tree (its only source overlaps are fixed-size artifacts).
"""
import os
import xml.etree.ElementTree as ET

import pytest

from zone_tree import (
    K_FRAME,
    K_HSTACK,
    K_LEAF,
    K_VSTACK,
    audit_source_overlaps,
    parse_zone_tree,
)


def _dash(inner_zones, extra="", size=""):
    return ET.fromstring(
        "<dashboard name='D'>%s<zones>%s</zones>%s</dashboard>" % (size, inner_zones, extra))


def _device_set(db):
    """Replicate _parse_dashboard's devicelayouts exclusion set (all zones under <devicelayouts>)."""
    dev = set()
    for holder in db.iter():
        if holder.tag.rsplit("}", 1)[-1] == "devicelayouts":
            for z in holder.iter():
                if z.tag.rsplit("}", 1)[-1] == "zone":
                    dev.add(z)
    return dev


# a layout-basic root wrapping a vstack of [text, hstack(2 worksheets), legend]
_NESTED = """
<zone id='1' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>
  <zone id='2' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>
    <zone id='3' type-v2='text' x='0' y='0' w='100000' h='10000'/>
    <zone id='4' type-v2='layout-flow' param='horz' x='0' y='10000' w='100000' h='80000'>
      <zone id='5' name='Sheet A' x='0' y='10000' w='50000' h='80000'/>
      <zone id='6' name='Sheet B' x='50000' y='10000' w='50000' h='80000'/>
    </zone>
    <zone id='7' type-v2='color' x='0' y='90000' w='100000' h='10000'/>
  </zone>
</zone>
"""


def test_nested_flow_tree_shape_and_kinds():
    t = parse_zone_tree(_dash(_NESTED))
    assert t is not None
    root = t["root"]
    assert root["kind"] == K_FRAME and root["zone_id"] == "1"
    vs = root["children"][0]
    assert vs["kind"] == K_VSTACK and len(vs["children"]) == 3
    k = [(c["zone_id"], c["kind"], c["leaf_kind"]) for c in vs["children"]]
    assert k == [("3", K_LEAF, "text"), ("4", K_HSTACK, None), ("7", K_LEAF, "legend")]
    assert t["extent"] == {"w": 100000.0, "h": 100000.0}
    assert t["floats"] == [] and t["diagnostics"] == []


def test_flow_children_fractions():
    t = parse_zone_tree(_dash(_NESTED))
    vs = t["root"]["children"][0]
    fr = [round(c["frac"], 3) for c in vs["children"]]
    assert fr == [0.1, 0.8, 0.1]          # 10000/100000, 80000/100000, 10000/100000
    assert abs(sum(fr) - 1.0) < 1e-9
    hs = vs["children"][1]
    assert [round(c["frac"], 3) for c in hs["children"]] == [0.5, 0.5]   # 50000/100000 each


def test_leaf_node_carries_worksheet_name_and_absolute_src_and_element():
    t = parse_zone_tree(_dash(_NESTED))
    hs = t["root"]["children"][0]["children"][1]
    a = hs["children"][0]
    assert a["leaf_kind"] == "worksheet" and a["name"] == "Sheet A"
    assert a["src"] == {"x": 0.0, "y": 10000.0, "w": 50000.0, "h": 80000.0}  # absolute, preserved
    assert a["zone_el"].get("id") == "5"                                     # real Element retained


def test_leaf_kind_taxonomy():
    inner = (
        "<zone id='1' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
        "<zone id='t' type-v2='text' x='0' y='0' w='100000' h='12500'/>"
        "<zone id='f' type-v2='filter' x='0' y='12500' w='100000' h='12500'/>"
        "<zone id='p' type-v2='paramctrl' x='0' y='25000' w='100000' h='12500'/>"
        "<zone id='c' type-v2='color' x='0' y='37500' w='100000' h='12500'/>"
        "<zone id='b' type-v2='bitmap' x='0' y='50000' w='100000' h='12500'/>"
        "<zone id='o' type-v2='dashboard-object' x='0' y='62500' w='100000' h='12500'/>"
        "<zone id='e' type-v2='empty' x='0' y='75000' w='100000' h='12500'/>"
        "<zone id='w' name='WS' x='0' y='87500' w='100000' h='12500'/>"
        "</zone>")
    t = parse_zone_tree(_dash(inner))
    got = {c["zone_id"]: c["leaf_kind"] for c in t["root"]["children"]}
    assert got == {"t": "text", "f": "filter", "p": "paramctrl", "c": "legend",
                   "b": "bitmap", "o": "bitmap", "e": "blank", "w": "worksheet"}


def test_floating_zone_is_hoisted_out_of_flow():
    inner = (
        "<zone id='1' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
        "<zone id='2' name='Tiled' x='0' y='0' w='100000' h='90000'/>"
        "<zone id='9' name='Float' floating='true' x='70000' y='5000' w='20000' h='20000'/>"
        "</zone>")
    t = parse_zone_tree(_dash(inner))
    assert t is not None
    kids = t["root"]["children"]
    assert [c["zone_id"] for c in kids] == ["2"]        # the float is NOT a flow child
    assert len(t["floats"]) == 1 and t["floats"][0]["zone_id"] == "9"
    assert t["floats"][0]["floating"] is True


def test_devicelayouts_zones_excluded():
    # primary layout = 1 worksheet; a phone <devicelayouts> re-lists the same zone id under its own
    # <zones>. The direct-child <zones> is the root and the device set keeps the phone copy out.
    inner = "<zone id='1' name='Primary WS' x='0' y='0' w='100000' h='100000'/>"
    phone = ("<devicelayouts><devicelayout name='Phone'><zones>"
             "<zone id='ph' name='Phone WS' x='0' y='0' w='50000' h='50000'/>"
             "</zones></devicelayout></devicelayouts>")
    db = _dash(inner, extra=phone)
    t = parse_zone_tree(db, _device_set(db))
    assert t is not None
    names = []

    def walk(n):
        names.append(n.get("name"))
        for c in n["children"]:
            walk(c)
    walk(t["root"])
    assert "Primary WS" in names and "Phone WS" not in names


def test_fixed_size_source_overlap_is_benign_not_a_premise_violation():
    # A fixed-size band (is-fixed) persists a NOMINAL rect (stored h spans its neighbours) -- the
    # ATTI z84 shape. The resolved layout is disjoint, so the tree MUST still parse and the audit
    # must bucket the raw overlap as a fixed artifact, never a genuine overlap.
    inner = (
        "<zone id='1' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
        "<zone id='84' is-fixed='true' fixed-size='110' name='Band' x='0' y='0' w='100000' h='30000'/>"
        "<zone id='255' name='Body' x='0' y='11000' w='100000' h='44500'/>"
        "<zone id='22' name='Foot' x='0' y='55500' w='100000' h='44500'/>"
        "</zone>")
    db = _dash(inner)
    t = parse_zone_tree(db)
    assert t is not None, "a fixed-size persistence overlap must NOT fail the tree closed"
    band = t["root"]["children"][0]
    assert band["fixed_px"] == 110.0
    aud = audit_source_overlaps(db)
    assert aud["overlaps"] == []                    # no GENUINE overlap
    assert len(aud["fixed_artifacts"]) >= 1         # the fixed band vs its neighbours


def test_genuine_flexible_sibling_overlap_fails_closed():
    # Two FLEXIBLE tiled siblings whose source rects overlap = the premise is violated -> None.
    inner = (
        "<zone id='1' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
        "<zone id='2' name='A' x='0' y='0' w='100000' h='60000'/>"
        "<zone id='3' name='B' x='0' y='40000' w='100000' h='60000'/>"   # overlaps A by 20000
        "</zone>")
    db = _dash(inner)
    assert parse_zone_tree(db) is None
    aud = audit_source_overlaps(db)
    assert len(aud["overlaps"]) == 1 and aud["fixed_artifacts"] == []
    assert {aud["overlaps"][0]["a"], aud["overlaps"][0]["b"]} == {"2", "3"}


def test_frame_children_may_overlap_without_failing():
    # A layout-basic FRAME positions children absolutely; intentional overlap there is allowed.
    inner = (
        "<zone id='1' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
        "<zone id='2' name='A' x='0' y='0' w='60000' h='60000'/>"
        "<zone id='3' name='B' x='40000' y='40000' w='60000' h='60000'/>"
        "</zone>")
    t = parse_zone_tree(_dash(inner))
    assert t is not None and t["root"]["kind"] == K_FRAME


def test_no_zones_returns_none():
    assert parse_zone_tree(ET.fromstring("<dashboard name='D'/>")) is None


def test_empty_zones_returns_none():
    assert parse_zone_tree(_dash("")) is None


def test_zero_sized_flow_axis_returns_none():
    inner = (
        "<zone id='1' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='0'>"
        "<zone id='2' name='A' x='0' y='0' w='100000' h='0'/>"
        "</zone>")
    assert parse_zone_tree(_dash(inner)) is None


def test_deep_recursion_returns_none_without_raising():
    depth = 40
    inner = ""
    for i in range(depth):
        inner += ("<zone id='z%d' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>" % i)
    inner += "<zone id='leaf' name='WS' x='0' y='0' w='100000' h='100000'/>"
    inner += "</zone>" * depth
    assert parse_zone_tree(_dash(inner)) is None


def test_never_raises_on_garbage_input():
    # A non-dashboard element, and a dashboard whose zones carry non-numeric coords: both -> None.
    assert parse_zone_tree(ET.fromstring("<not-a-dashboard/>")) is None
    weird = ("<zone id='1' type-v2='layout-flow' param='vert' x='oops' y='?' w='n/a' h='-'>"
             "<zone id='2' name='A'/></zone>")
    # must not raise; a flow parent with an unreadable main axis fails closed
    assert parse_zone_tree(_dash(weird)) is None


def test_audit_counts_are_reported():
    aud = audit_source_overlaps(_dash(_NESTED))
    assert aud["flow_containers"] >= 2          # the vstack + the inner hstack
    assert aud["checked_pairs"] >= 1
    assert aud["overlaps"] == [] and aud["fixed_artifacts"] == []


@pytest.mark.skipif(not os.environ.get("TFMIG_ZONE_TREE_TWB"),
                    reason="opt-in: set TFMIG_ZONE_TREE_TWB to a real .twb to run the zone-tree premise golden")
def test_real_workbook_zone_trees_are_overlap_free():
    # Every dashboard in a real workbook must parse to an overlap-free flow tree; its only source
    # overlaps (if any) must be fixed-size persistence artifacts, never genuine flexible overlaps.
    with open(os.environ["TFMIG_ZONE_TREE_TWB"], "r", encoding="utf-8-sig") as fh:
        root = ET.fromstring(fh.read())
    dashboards = [d for d in root.iter() if d.tag.rsplit("}", 1)[-1] == "dashboard"]
    assert dashboards, "workbook has no dashboards"
    for db in dashboards:
        dev = _device_set(db)
        aud = audit_source_overlaps(db, dev)
        assert aud["overlaps"] == [], (
            "dashboard %r has genuine flexible-sibling source overlaps: %r"
            % (db.get("name"), aud["overlaps"]))
        assert parse_zone_tree(db, dev) is not None, (
            "dashboard %r failed to parse to a usable tree" % db.get("name"))
