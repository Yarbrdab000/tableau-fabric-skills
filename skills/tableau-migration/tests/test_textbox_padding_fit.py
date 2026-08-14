"""A caption sized in Tableau does not fit in Power BI, because Power BI adds padding Tableau has not.

Power BI reserves 8px above AND below a textbox's text by default, so a box's usable height is
``height - 16``. Tableau reserves nothing. An author who drew a 24px caption strip drew it to fit
12pt text, and it does fit — in Tableau. Emitted verbatim, those same 24px leave 8px for a line that
needs 19, and the band renders **clipped**: descenders sheared off, a scrollbar stub where the text
should be.

Measured on a real network-operations dashboard (`Network Operational PowerBI Mock`):
`"Sort By = Network Score | Region = All | Fiscal Month ="` shipped at 24px with its descenders cut,
and a 16pt section header did the same at 31px. The build validated with **zero errors** and rendered
wrong — the giveaway being that our own gate already knew: `powerbi-report-author validate` warns
`PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR` with exactly the renderer's formula. We emitted geometry the gate
then told us was wrong, one step too late to matter.

**The fix is not to grow the box, and that distinction is the whole point.** Growing it is what the
layout solver deliberately refuses to do (`layout_solve._clamp_to_authored`), for a measured reason:
a readability floor propagated up a zone tree makes a frame scale the WHOLE canvas to satisfy it —
eleven pixels of caption once cost five hundred pixels of page, and every object on it rendered 50%
taller. `test_thin_caption_sizes_to_content_not_inflated_to_floor` guards that, and it caught the
first version of this fix, correctly.

The 16px is not the author's, it is OURS: a default we never asked for, on a box we emit. So the room
is taken from our own padding first, down to zero, and the authored geometry is never touched. A
textbox with room to spare emits no padding block at all and is byte-identical to before.
"""
import math

import twb_to_pbir as T


def _visual(height, font_pt=12.0, text="Sort By = Network Score"):
    return T._text_object_textbox_visual(
        "v-test", {"x": 0, "y": 0, "z": 0, "width": 400, "height": height, "tabOrder": 1},
        {"text": text, "font_size": font_pt})


def _padding(visual):
    """``(top, bottom)`` of the emitted padding block, or ``None`` when none was emitted."""
    blk = (visual["visual"].get("visualContainerObjects") or {}).get("padding")
    if not blk:
        return None
    props = blk[0]["properties"]
    return tuple(float(props[k]["expr"]["Literal"]["Value"].rstrip("D")) for k in ("top", "bottom"))


def test_the_reported_caption_now_has_room_for_its_text():
    """24px at 12pt: the exact box that rendered sheared."""
    v = _visual(24.0, 12.0)
    pad = _padding(v)
    assert pad is not None, "a box this tight must not keep the clipping 8px default"
    assert v["position"]["height"] == 24.0, "the author's geometry must not change"
    assert 24.0 - sum(pad) >= T._textbox_text_height(12.0)


def test_the_reported_section_header_too():
    """31px at 16pt: the second clipped box on the same page."""
    v = _visual(31.0, 16.0)
    assert 31.0 - sum(_padding(v)) >= T._textbox_text_height(16.0)


def test_a_box_with_room_to_spare_is_byte_identical_to_before():
    """Never-regress: the default already fits, so nothing is emitted."""
    for h, pt in ((70.0, 18.0), (55.0, 12.0), (40.0, 12.0), (35.0, 12.0)):
        assert _padding(_visual(h, pt)) is None, "%gpx/%gpt should not need a padding block" % (h, pt)


def test_padding_is_never_negative_on_a_hopeless_box():
    """A box too short even at zero padding clamps at 0 rather than emitting a negative."""
    v = _visual(6.0, 24.0)
    assert _padding(v) == (0.0, 0.0)


def test_the_authored_geometry_is_never_inflated():
    """The regression the first version of this fix caused, and the reason the padding route exists.

    Inflating a caption to a readability floor propagates up the solver's zone tree and scales the
    whole canvas -- the measured 1506px-for-a-1000px-dashboard failure. Padding costs nothing.
    """
    for h in (12.0, 24.0, 31.0, 100.0):
        assert _visual(h)["position"]["height"] == h


def test_the_floor_matches_the_renderers_own_numbers():
    """Not an estimate: these are the values `powerbi-report-author validate` reports.

    ``max(18, ceil(pt * 25/16)) + padTop + padBottom`` -- the messages on the reported workbook read
    "min 35px" for 12pt and "min 41px" for 16pt.
    """
    assert T._textbox_min_height(12.0) == 35.0
    assert T._textbox_min_height(16.0) == 41.0
    # and with padding we set ourselves, the floor tracks the box actually drawn
    assert T._textbox_min_height(12.0, 0, 0) == 20.0     # _TEXTBOX_MIN_H dominates
    assert T._textbox_min_height(16.0, 0, 0) == 25.0


def test_the_text_height_is_the_renderers_formula():
    for pt in (8, 10, 12, 14, 16, 18, 24):
        assert T._textbox_text_height(pt) == max(18.0, math.ceil(pt * 25.0 / 16.0))


def test_the_banner_is_fitted_too():
    """The title banner is a textbox on the same renderer rule; a short banner clips identically."""
    v = T._banner_textbox_visual(
        "v-banner", {"x": 0, "y": 0, "z": 0, "width": 800, "height": 30, "tabOrder": 1},
        {"text": "Network Operational Dashboard 2.1", "fill": "#5B2D90"})
    pad = _padding(v)
    assert pad is not None
    assert 30.0 - sum(pad) >= T._textbox_text_height(T._BANNER_FONT_PT) or pad == (0.0, 0.0)


def test_fitting_leaves_the_rest_of_the_visual_alone():
    """Padding is additive: background, title-off and the paragraphs are untouched."""
    v = _visual(24.0)
    vco = v["visual"]["visualContainerObjects"]
    assert "background" in vco and "title" in vco
    runs = v["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"]
    assert runs[0]["value"] == "Sort By = Network Score"
