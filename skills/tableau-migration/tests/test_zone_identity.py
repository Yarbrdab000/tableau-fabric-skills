"""Zone-identity seam (Zone Geometry v3 -- frame track slice 4a).

``_parse_dashboard`` flattens a dashboard's ``<zones>`` into per-kind lists of plain coordinate
dicts (``zones`` / ``filter_zones`` / ``param_controls`` / ``legend_zones`` / ``text_objects`` /
``image_zones`` / ``title_banner``). Those dicts carried geometry but no IDENTITY, so a captured
item could not be matched back to its node in the layout tree (``zone_tree``) or to its solved
rectangle (``layout_solve``) -- the lookup every solver-backed emit path needs.

Matching by rect is not a substitute: a single-child ``layout-flow`` wrapper persists its child's
rect EXACTLY, so a rect can name two different zones (observed on 12 of 13 corpus dashboards).
The fix is to record the zone's own ``id`` at walk time. These tests lock:

  * CAPTURE   -- every one of the seven captured kinds carries ``zone_id``,
  * ROUND-TRIP -- every captured ``zone_id`` resolves to a rect in ``layout_solve.solve(...)``
                 (the load-bearing invariant of the whole solver-emit track),
  * IDENTITY  -- captured ids are distinct, and a ``<devicelayouts>`` alternate never leaks one,
  * ADDITIVE  -- pre-existing keys are untouched (``image_zones`` keeps its original ``id``),
  * ROBUST    -- a zone with no ``id`` attribute captures ``zone_id=None`` and never raises.

Nothing reads ``zone_id`` yet; it is the seam a later slice turns into a solved-rect lookup.
"""
import xml.etree.ElementTree as ET

import twb_to_pbir as R
from layout_solve import solve
from zone_tree import parse_zone_tree

_WS = {"WsA"}


# -- fixture -------------------------------------------------------------------
def _dash(zones_xml, name="D"):
    return ET.fromstring("<dashboard name='%s'><zones>%s</zones></dashboard>" % (name, zones_xml))


def _text_zone(zid, x, y, w, h, text="Hello", fill=None):
    style = ("<zone-style><format attr='background-color' value='%s' /></zone-style>" % fill
             if fill else "")
    return ("<zone id='%s' type-v2='text' x='%d' y='%d' w='%d' h='%d'>"
            "<formatted-text><run fontcolor='#ffffff'>%s</run></formatted-text>%s</zone>"
            % (zid, x, y, w, h, text, style))


# one layout-basic root holding exactly one zone of each captured kind
_ALL_KINDS = (
    "<zone id='root' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
    + _text_zone("zb", 0, 0, 100000, 9000, "Title Band", fill="#ac145a")      # banner
    + _text_zone("zt", 0, 40000, 30000, 4000, "Caption")                      # text object
    + "<zone id='zw' name='WsA' x='0' y='10000' w='50000' h='30000' />"       # worksheet
    + "<zone id='zl' type-v2='color' name='WsA' x='50000' y='10000' w='20000' h='10000' />"
    + ("<zone id='zf' type-v2='filter' name='WsA' param='[federated.abc].[none:Region:nk]'"
       " x='70000' y='10000' w='20000' h='8000' />")
    + ("<zone id='zp' type-v2='paramctrl' param='[Parameters].[Parameter 1]'"
       " x='70000' y='20000' w='16000' h='9333' />")
    + "<zone id='zi' type-v2='bitmap' param='Image/logo.png' x='85000' y='0' w='10000' h='7000' />"
    + "</zone>"
)

_EXPECTED = {"banner": "zb", "text": "zt", "worksheet": "zw",
             "legend": "zl", "filter": "zf", "paramctrl": "zp", "image": "zi"}


def _parsed(xml=_ALL_KINDS, ws=_WS):
    return R._parse_dashboard(_dash(xml), ws, [])


def _captured(p):
    """Every captured item as ``(kind, dict)`` -- the full emit-facing surface."""
    out = [("worksheet", z) for z in p["zones"]]
    out += [("filter", z) for z in p["filter_zones"]]
    out += [("paramctrl", z) for z in p["param_controls"]]
    out += [("legend", z) for z in p["legend_zones"]]
    out += [("text", z) for z in p["text_objects"]]
    out += [("image", z) for z in p["image_zones"]]
    if p.get("title_banner"):
        out.append(("banner", p["title_banner"]))
    return out


# -- capture -------------------------------------------------------------------
def test_every_captured_kind_records_its_zone_id():
    p = _parsed()
    got = {kind: d.get("zone_id") for kind, d in _captured(p)}
    assert got == _EXPECTED


def test_all_seven_kinds_are_present_in_the_fixture():
    # guards the test itself: a capture predicate change that silently stops collecting a kind
    # would otherwise make the identity assertion vacuously pass for that kind.
    assert sorted(k for k, _ in _captured(_parsed())) == sorted(_EXPECTED)


def test_zone_id_is_never_missing_from_a_captured_item():
    for kind, d in _captured(_parsed()):
        assert "zone_id" in d, kind


# -- round-trip: the load-bearing invariant -------------------------------------
def test_every_captured_zone_id_resolves_to_a_solved_rect():
    db = _dash(_ALL_KINDS)
    tree = parse_zone_tree(db)
    assert tree is not None
    solved = solve(tree, (0.0, 0.0, 1280.0, 720.0))
    assert solved is not None
    rects = solved["rects"]
    for kind, d in _captured(R._parse_dashboard(db, _WS, [])):
        zid = d.get("zone_id")
        assert zid in rects, "%s (zone_id=%r) has no solved rect" % (kind, zid)
        x, y, w, h = rects[zid]
        assert w > 0 and h > 0


def test_solved_rect_lookup_is_by_id_not_by_rect():
    # A single-child layout-flow wrapper persists its child's rect EXACTLY, so the rect alone
    # names two zones -- the reason identity is carried as an id rather than reverse-mapped.
    xml = ("<zone id='outer' type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
           "<zone id='inner' name='WsA' x='0' y='0' w='100000' h='100000' /></zone>")
    db = _dash(xml)
    p = R._parse_dashboard(db, _WS, [])
    assert [z["zone_id"] for z in p["zones"]] == ["inner"]
    rects = solve(parse_zone_tree(db), (0.0, 0.0, 1280.0, 720.0))["rects"]
    assert rects["outer"] == rects["inner"]       # the ambiguity a rect key would hit
    assert p["zones"][0]["zone_id"] in rects      # the id disambiguates it


# -- identity ------------------------------------------------------------------
def test_captured_zone_ids_are_distinct():
    ids = [d["zone_id"] for _, d in _captured(_parsed())]
    assert len(ids) == len(set(ids))


def test_device_layout_zone_ids_are_never_captured():
    db = ET.fromstring(
        "<dashboard name='D'><zones>"
        "<zone id='zw' name='WsA' x='0' y='0' w='100000' h='100000' /></zones>"
        "<devicelayouts><devicelayout><zones>"
        "<zone id='phone' name='WsA' x='0' y='0' w='50000' h='50000' />"
        "</zones></devicelayout></devicelayouts></dashboard>")
    p = R._parse_dashboard(db, _WS, [])
    assert [z["zone_id"] for z in p["zones"]] == ["zw"]


# -- additive ------------------------------------------------------------------
def test_image_zone_keeps_its_original_id_key():
    img = _parsed()["image_zones"][0]
    assert img["id"] == "zi" and img["zone_id"] == "zi"


def test_pre_existing_keys_are_unchanged():
    p = _parsed()
    z = p["zones"][0]
    assert (z["worksheet"], z["x"], z["y"], z["w"], z["h"]) == ("WsA", 0.0, 10000.0, 50000.0, 30000.0)
    f = p["filter_zones"][0]
    assert f["token"] == ("federated.abc", "none:Region:nk") and f["hidden"] is False
    assert p["param_controls"][0]["param_id"] == "Parameter 1"
    assert p["legend_zones"][0]["worksheet"] == "WsA"
    assert p["text_objects"][0]["text"] == "Caption"
    assert p["title_banner"]["fill"] == "#ac145a"


def test_banner_dedupe_still_removes_the_header_from_text_objects():
    # the banner is captured as a text zone too; the de-dupe must still fire with zone_id present
    texts = [t["text"] for t in _parsed()["text_objects"]]
    assert texts == ["Caption"]


# -- robustness ----------------------------------------------------------------
def test_zone_without_an_id_attribute_captures_none_and_does_not_raise():
    p = R._parse_dashboard(
        _dash("<zone name='WsA' x='0' y='0' w='100000' h='100000' />"), _WS, [])
    assert p["zones"] and p["zones"][0]["zone_id"] is None


def test_empty_dashboard_still_parses():
    p = R._parse_dashboard(_dash(""), _WS, [])
    assert p["zones"] == [] and p["title_banner"] is None
