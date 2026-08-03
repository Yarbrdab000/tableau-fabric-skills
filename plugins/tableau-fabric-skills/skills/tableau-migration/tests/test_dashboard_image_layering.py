"""Guards for the dashboard image LAYER decision (backdrop plate vs decorative overlay).

Tableau paints dashboard zones in DOCUMENT ORDER: a zone written earlier sits beneath every zone
written after it. Designers exploit that with the SAME ``bitmap`` zone type in two opposite ways --
a full-canvas pre-rendered design plate written FIRST (header band, sidebar, card frames), and a
decorative overlay written LAST (corner logo, chevron icon, a toggled help panel).

Emitting every image on top -- the naive reading of "an image is decoration" -- paints the plate
over every chart and yields a dashboard that looks pixel-perfect and shows no data. That is the
worst possible failure mode: it passes a screenshot review and fails a data review.

The other half of the contract is the z SCHEME itself, which was settled by RENDERING against
Power BI Desktop 2.157 rather than by reading the PBIR schema:

  * the schema puts no minimum on ``z`` and Desktop honours a negative value for ORDERING -- but
    it does not PAINT the visual at all. A plate at ``z=-100`` stopped occluding the charts and
    simultaneously vanished, trading one fidelity defect for another;
  * so every layer must be >= 0, and worksheet content cannot sit on the natural floor of 0
    because the plate has to go underneath it.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from twb_to_pbir import (  # noqa: E402
    _Z_BACKDROP,
    _Z_BANNER,
    _Z_CAPTION,
    _Z_CONTENT,
    _Z_OVERLAY,
    _Z_SLICER,
    _deoverlap_captions,
    _image_z,
    _reflow_worksheets_below_slicers,
    migrate_twb_to_pbir,
)

from test_twb_to_pbir import (  # noqa: E402
    _INST,
    _text_object_container,
    _text_object_ws_zone,
    _visual_parts,
    _workbook,
    _worksheet,
)

_PNG_BG = b"\x89PNG\r\n\x1a\nPLATE"
_PNG_OVL = b"\x89PNG\r\n\x1a\nLOGO"


def _bitmap(param, zid, x=0, y=0, w=100000, h=100000):
    return ("<zone type-v2='bitmap' param='%s' id='%s' x='%d' y='%d' w='%d' h='%d' />"
            % (param, zid, x, y, w, h))


def _dash(name, *zones):
    return ("<dashboard name='%s'><size maxwidth='1400' maxheight='950' /><zones>" % name
            + _text_object_container(*zones)
            + "</zones></dashboard>")


def _sheet(name="Sheet1"):
    return _worksheet(name, "Bar", "[federated.abc].[sum:Sales:qk]",
                      "[federated.abc].[none:Category:nk]", deps_extra=_INST)


def _images(result):
    """Every emitted image visual, keyed by its ``ItemName`` -> z."""
    out = {}
    for v in _visual_parts(result["parts"]).values():
        if v["visual"]["visualType"] != "image":
            continue
        item = (v["visual"]["objects"]["general"][0]["properties"]
                ["imageUrl"]["expr"]["ResourcePackageItem"]["ItemName"])
        out[item] = v["position"]["z"]
    return out


def _content_zs(result):
    return sorted({v["position"]["z"] for v in _visual_parts(result["parts"]).values()
                   if v["visual"]["visualType"] not in ("image", "textbox")})


# -- the z scheme itself -------------------------------------------------------------------------


def test_z_scheme_is_strictly_ascending_and_never_negative():
    # Ordering: the backdrop is beneath content, which is beneath its own slicers, then captions,
    # the banner and finally overlay decoration. Every later pass depends on this being a total
    # order -- two layers sharing a value would make stacking fall back to array order.
    layers = [_Z_BACKDROP, _Z_CONTENT, _Z_SLICER, _Z_CAPTION, _Z_BANNER, _Z_OVERLAY]
    assert layers == sorted(layers)
    assert len(set(layers)) == len(layers)
    # Non-negative is a RENDER-derived constraint, not a schema one: Desktop accepts a negative z
    # and then declines to paint the visual, so a "background" at z<0 is an invisible background.
    assert all(z >= 0 for z in layers)
    # Content cannot sit on the natural floor -- that floor belongs to the backdrop.
    assert _Z_BACKDROP < _Z_CONTENT


# -- the layer decision --------------------------------------------------------------------------


def test_image_before_the_first_worksheet_is_a_backdrop():
    assert _image_z({"paint_ord": 2}, 7) == _Z_BACKDROP


def test_image_after_the_first_worksheet_is_an_overlay():
    assert _image_z({"paint_ord": 91}, 8) == _Z_OVERLAY


def test_image_at_the_same_ordinal_as_the_first_worksheet_is_an_overlay():
    # Strict "<" -- a tie cannot happen from a real document walk, but only a zone PROVEN to
    # precede the content may be demoted beneath it. Ambiguity keeps the historical layer.
    assert _image_z({"paint_ord": 5}, 5) == _Z_OVERLAY


def test_dashboard_with_no_worksheet_keeps_every_image_on_top():
    # Nothing can be occluded, and a lone image is decoration. Keeps image-only pages byte-stable.
    assert _image_z({"paint_ord": 0}, None) == _Z_OVERLAY


def test_missing_paint_ordinal_falls_back_to_the_overlay_layer():
    # Fail-safe: without provenance we cannot prove the author put it behind the sheets.
    assert _image_z({}, 7) == _Z_OVERLAY
    assert _image_z({"paint_ord": None}, 7) == _Z_OVERLAY


# -- end to end ----------------------------------------------------------------------------------


def test_backdrop_plate_is_emitted_beneath_the_worksheets_it_sits_behind():
    twb = _workbook(_sheet(), _dash(
        "Cover",
        _bitmap("Image/Plate.png", "2"),
        _text_object_ws_zone("Sheet1", zid="7"),
    ))
    res = migrate_twb_to_pbir(twb, resources={"Image/Plate.png": _PNG_BG})
    imgs = _images(res)
    assert len(imgs) == 1
    backdrop = list(imgs.values())[0]
    assert backdrop == _Z_BACKDROP
    content = _content_zs(res)
    assert content, "expected the worksheet to be rebuilt"
    assert all(backdrop < z for z in content)


def test_overlay_logo_after_the_worksheets_still_paints_on_top():
    twb = _workbook(_sheet(), _dash(
        "Cover",
        _text_object_ws_zone("Sheet1", zid="7"),
        _bitmap("Image/Logo.png", "40", x=85000, y=0, w=10000, h=7000),
    ))
    res = migrate_twb_to_pbir(twb, resources={"Image/Logo.png": _PNG_OVL})
    imgs = _images(res)
    assert len(imgs) == 1
    overlay = list(imgs.values())[0]
    assert overlay == _Z_OVERLAY
    assert all(overlay > z for z in _content_zs(res))


def test_backdrop_and_overlay_on_the_SAME_dashboard_are_separated_by_document_order():
    # The discriminating case, taken from a real Viz-of-the-Day workbook: one dashboard carrying a
    # full-canvas design plate written first AND a toggled help panel written last. Coverage does
    # not separate them (the plate is 100% of the canvas, the help panel 65%) -- and a help panel
    # can easily be the larger of the two -- so only the document position decides.
    twb = _workbook(_sheet(), _dash(
        "Alliance",
        _bitmap("Image/Plate.png", "3"),
        _text_object_ws_zone("Sheet1", zid="8"),
        _bitmap("Image/Guide.png", "91", x=10000, y=10000, w=65000, h=65000),
    ))
    res = migrate_twb_to_pbir(twb, resources={"Image/Plate.png": _PNG_BG,
                                              "Image/Guide.png": _PNG_OVL})
    by_z = sorted(_images(res).values())
    assert by_z == [_Z_BACKDROP, _Z_OVERLAY]
    content = _content_zs(res)
    assert content
    assert by_z[0] < min(content) and by_z[1] > max(content)


def test_two_backdrops_before_the_first_worksheet_both_go_underneath():
    # A page can stack several plates (one per state of a nav sidebar). Every zone preceding the
    # first worksheet is a backdrop -- the rule is not "the first image wins".
    twb = _workbook(_sheet(), _dash(
        "Airlines",
        _bitmap("Image/A.png", "2"),
        _bitmap("Image/B.png", "3"),
        _text_object_ws_zone("Sheet1", zid="7"),
    ))
    res = migrate_twb_to_pbir(twb, resources={"Image/A.png": _PNG_BG, "Image/B.png": _PNG_OVL})
    zs = list(_images(res).values())
    assert len(zs) == 2 and set(zs) == {_Z_BACKDROP}


def test_image_only_dashboard_is_unchanged_by_the_layer_rule():
    # No worksheet to order against -> historical behaviour, byte-stable.
    twb = _workbook(_sheet(), _dash("Splash", _bitmap("Image/Logo.png", "2")))
    res = migrate_twb_to_pbir(twb, resources={"Image/Logo.png": _PNG_OVL})
    assert list(_images(res).values()) == [_Z_OVERLAY]


def test_image_interleaved_BETWEEN_two_worksheets_stays_on_top():
    # The ordinal we compare against is the FIRST worksheet's, not the last, and this is the case
    # that proves it. Tableau paints such an image above sheet 1 and below sheet 2; PBIR carries
    # one z per visual, so that is inexpressible and we must pick a side. Demote only what is
    # proven to precede ALL content -- an interleaved image covers at least one sheet, so sinking
    # it would hide real data. Fail-safe beats symmetrical.
    twb = _workbook(
        _sheet("Sheet1") + _sheet("Sheet2"),
        _dash("Mixed",
              _text_object_ws_zone("Sheet1", zid="7", x=0, y=0, w=100000, h=40000),
              _bitmap("Image/Mid.png", "40", x=0, y=40000, w=100000, h=10000),
              _text_object_ws_zone("Sheet2", zid="9", x=0, y=50000, w=100000, h=50000)),
    )
    res = migrate_twb_to_pbir(twb, resources={"Image/Mid.png": _PNG_OVL})
    assert list(_images(res).values()) == [_Z_OVERLAY]


def test_first_worksheet_ordinal_is_the_first_not_the_last():
    # Same invariant read straight off the capture, so a regression is localised rather than only
    # showing up as a mis-layered image three passes downstream.
    twb = _workbook(
        _sheet("Sheet1") + _sheet("Sheet2"),
        _dash("Mixed",
              _text_object_ws_zone("Sheet1", zid="7", x=0, y=0, w=100000, h=40000),
              _bitmap("Image/Mid.png", "40", x=0, y=40000, w=100000, h=10000),
              _text_object_ws_zone("Sheet2", zid="9", x=0, y=50000, w=100000, h=50000)),
    )
    db = migrate_twb_to_pbir(twb, resources={"Image/Mid.png": _PNG_OVL})["ir"]["dashboards"][0]
    mid = next(z for z in db["image_zones"] if z.get("paint_ord") is not None)
    assert db["first_ws_ord"] < mid["paint_ord"], "the image must follow the FIRST worksheet"


# -- the two passes that SELECT by z -------------------------------------------------------------


def _v(z, x, y, w, h, vt="clusteredColumnChart"):
    return {"position": {"x": x, "y": y, "width": w, "height": h, "z": z},
            "visual": {"visualType": vt}}


def test_reflow_never_moves_the_backdrop_plate():
    # The slicer reflow compresses worksheet CONTENT below a surfaced filter band. A backdrop is
    # the page's design, pinned to the canvas -- shifting or squashing it would tear the artwork
    # away from the charts it frames.
    backdrop = _v(_Z_BACKDROP, 0, 0, 1400, 950, vt="image")
    slicer = _v(_Z_SLICER, 0, 200, 300, 100, vt="slicer")
    sheet = _v(_Z_CONTENT, 0, 240, 800, 400)
    before = dict(backdrop["position"])
    _reflow_worksheets_below_slicers([backdrop, slicer, sheet], 950)
    assert backdrop["position"] == before
    assert sheet["position"]["y"] > 240, "the worksheet should still have been reflowed"


def test_caption_may_be_relocated_onto_the_backdrop_plate():
    # A full-canvas plate must not COUNT as occupied space. Treating it as an obstacle leaves no
    # clear band anywhere on the page, silently switching the caption tidy off for exactly the
    # elaborately designed dashboards that need it. A caption is MEANT to sit on the plate.
    backdrop = _v(_Z_BACKDROP, 0, 0, 1000, 1000, vt="image")
    anchor = _v(_Z_CONTENT, 0, 500, 1000, 200)
    cap = _v(_Z_CAPTION, 0, 510, 1000, 30, vt="textbox")
    _deoverlap_captions([backdrop, anchor, cap], 1000, 1000)
    # Lifted into the clear strip directly above the chart it labels -- a strip that lies entirely
    # on the plate, and which the pass could not have used while the plate blocked it.
    assert cap["position"]["y"] == 462
    assert cap["position"]["y"] + cap["position"]["height"] <= anchor["position"]["y"]


def test_backdrop_plate_is_never_moved_or_resized_by_the_caption_tidy():
    # It is the page's artwork, pinned to the canvas. Shifting or squashing it would tear the
    # design away from the charts it frames.
    backdrop = _v(_Z_BACKDROP, 0, 0, 1000, 1000, vt="image")
    anchor = _v(_Z_CONTENT, 0, 500, 1000, 200)
    cap = _v(_Z_CAPTION, 0, 510, 1000, 30, vt="textbox")
    before = dict(backdrop["position"])
    _deoverlap_captions([backdrop, anchor, cap], 1000, 1000)
    assert backdrop["position"] == before


def test_backdrop_plate_never_grows_the_page():
    # A plate that scaled a few pixels proud of the authored canvas must not inflate the page:
    # growing to fit DECORATION pushes the real content off-screen, inverting the point of the
    # grow. Only real content can extend the canvas.
    tall_plate = _v(_Z_BACKDROP, 0, 0, 1000, 1400, vt="image")
    anchor = _v(_Z_CONTENT, 0, 500, 1000, 200)
    cap = _v(_Z_CAPTION, 0, 510, 1000, 30, vt="textbox")
    assert _deoverlap_captions([tall_plate, anchor, cap], 1000, 1000) == 1000
    # Control: the same overhang on a non-backdrop layer DOES grow the page.
    tall_ovl = _v(_Z_OVERLAY, 0, 0, 1000, 1400, vt="image")
    anchor2 = _v(_Z_CONTENT, 0, 500, 1000, 200)
    cap2 = _v(_Z_CAPTION, 0, 510, 1000, 30, vt="textbox")
    assert _deoverlap_captions([tall_ovl, anchor2, cap2], 1000, 1000) > 1000


def test_overlay_image_still_blocks_a_caption_relocation():
    # The backdrop exemption is specific to the backdrop layer. An overlay is real drawn content
    # in front of the page, so a caption must still be kept off it.
    overlay = _v(_Z_OVERLAY, 0, 0, 1000, 400, vt="image")
    anchor = _v(_Z_CONTENT, 0, 400, 1000, 200)
    cap = _v(_Z_CAPTION, 0, 410, 1000, 30, vt="textbox")
    _deoverlap_captions([overlay, anchor, cap], 1000, 1000)
    p = cap["position"]
    assert not (p["y"] < 400 and p["y"] + p["height"] > 0), "caption was parked under the overlay"
