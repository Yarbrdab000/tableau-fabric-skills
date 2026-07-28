"""Tableau zone PADDING fidelity -- the general class behind "the icons sit far below the logo".

Tableau draws a dashboard object inside the CONTENT BOX left after its zone's outer padding
(``<zone-style><format attr='margin'|'margin-bottom'|...>``, in real pixels). ``_parse_zone_padding``
has parsed that model for a long time but had **no callers** -- every zone's padding was discarded, so
an object whose author pushed it to one side of its zone was emitted at the raw zone rect and Power BI
(which has no zone concept -- a visual fills its rect and centres its image) drew it in the wrong
place. The ATTI icon strip is the visible instance: a 133px zone with ``margin=4`` +
``margin-bottom=85`` leaves a 44px content band at the TOP, but we emitted the full 133px and the icon
landed ~46px low.

These tests pin the general rule, not that one dashboard:

* only the ASYMMETRIC excess displaces content (a uniform inset does not move a centre, and the
  layout model already provides separation via its own gap -- subtracting it twice would shrink every
  visual on every dashboard for no fidelity gain);
* the inset applies under BOTH layout engines, because it is source fidelity, not layout repair;
* it only ever SHRINKS a rect, so it can never re-introduce an overlap the solver resolved;
* it never collapses a rect to nothing.
"""
import pytest

import twb_to_pbir
from twb_to_pbir import _apply_zone_padding, _zone_pad_inset, _scale_zone


def _pad(top, right, bottom, left, inner=0):
    return {"outer": {"top": top, "right": right, "bottom": bottom, "left": left},
            "inner": {"top": inner, "right": inner, "bottom": inner, "left": inner}}


# --------------------------------------------------------------------------- inset rule

def test_uniform_padding_does_not_displace_content():
    """Tableau's documented 4px default is uniform -- it shrinks evenly and moves no centre."""
    assert _zone_pad_inset({"pad": _pad(4, 4, 4, 4)}) is None


def test_absent_padding_is_a_no_op():
    assert _zone_pad_inset({"pad": None}) is None
    assert _zone_pad_inset({}) is None


def test_asymmetric_padding_returns_only_the_excess_over_the_minimum():
    """The ATTI icon zone: margin=4 everywhere, margin-bottom=85."""
    assert _zone_pad_inset({"pad": _pad(4, 4, 85, 4)}) == (0.0, 0.0, 81.0, 0.0)


def test_malformed_padding_is_ignored_rather_than_raising():
    assert _zone_pad_inset({"pad": {"outer": {"top": "x", "right": 4, "bottom": 4, "left": 4}}}) is None
    assert _zone_pad_inset({"pad": {"outer": {"top": 4}}}) is None


# --------------------------------------------------------------------------- rect application

def test_bottom_heavy_padding_top_aligns_content_in_its_zone():
    """The real defect: a tall zone whose author pinned the icon to the TOP band."""
    rect = _apply_zone_padding((100.0, 78.0, 32.0, 133.0), {"pad": _pad(4, 4, 85, 4)})
    assert rect == (100.0, 78.0, 32.0, 52.0)
    # the content stays anchored at the zone's top edge, directly under whatever is above it
    assert rect[1] == 78.0


def test_left_heavy_padding_insets_horizontally_without_moving_the_top():
    """The EBI logo: margin=10, margin-top/bottom=5, margin-left=13 -> excess (0, 5, 0, 8)."""
    x, y, w, h = _apply_zone_padding((1286.0, 8.0, 206.0, 61.0),
                                     {"pad": _pad(5, 10, 5, 13)})
    assert (x, y) == (1294.0, 8.0)
    assert (w, h) == (193.0, 61.0)


def test_padding_only_ever_shrinks_so_it_cannot_create_an_overlap():
    """Containment argument: the result is strictly inside the rect it replaces."""
    orig = (10.0, 20.0, 300.0, 200.0)
    x, y, w, h = _apply_zone_padding(orig, {"pad": _pad(4, 30, 60, 12)})
    assert x >= orig[0] and y >= orig[1]
    assert x + w <= orig[0] + orig[2] + 1e-9
    assert y + h <= orig[1] + orig[3] + 1e-9


def test_padding_never_collapses_a_rect_below_one_pixel():
    """A tiny zone with huge authored padding keeps its raw rect rather than vanishing."""
    orig = (0.0, 0.0, 20.0, 20.0)
    assert _apply_zone_padding(orig, {"pad": _pad(4, 4, 400, 4)}) == orig


def test_padding_scale_converts_authored_pixels_to_the_emitted_page(monkeypatch):
    """Margins are authored PIXELS; a rescaled page must rescale them too."""
    monkeypatch.setattr(twb_to_pbir, "_ZONE_PAD_SCALE", (1.0, 2.0))
    x, y, w, h = _apply_zone_padding((0.0, 0.0, 100.0, 300.0), {"pad": _pad(4, 4, 84, 4)})
    assert (y, h) == (0.0, 300.0 - 80.0 * 2.0)


# --------------------------------------------------------------------------- engine coverage

@pytest.mark.parametrize("solved", [None, (500.0, 600.0, 40.0, 133.0)])
def test_padding_applies_under_both_the_solver_and_the_legacy_scale(monkeypatch, solved):
    """Honouring the source is fidelity, not layout repair -- neither engine may skip it."""
    monkeypatch.setattr(twb_to_pbir, "_PAGE_W_OVERRIDE", 1000.0)
    monkeypatch.setattr(twb_to_pbir, "_PAGE_H_OVERRIDE", 1000.0)
    monkeypatch.setattr(twb_to_pbir, "_ZONE_PAD_SCALE", (1.0, 1.0))
    monkeypatch.setattr(twb_to_pbir, "_solved_rect", lambda z: solved)
    zone = {"zone_id": "65", "x": 50000, "y": 60000, "w": 4000, "h": 13300,
            "pad": _pad(4, 4, 85, 4)}
    _, _, _, h = _scale_zone(zone, 100000, 100000)
    unpadded = dict(zone, pad=None)
    _, _, _, h0 = _scale_zone(unpadded, 100000, 100000)
    assert h == pytest.approx(h0 - 81.0)
