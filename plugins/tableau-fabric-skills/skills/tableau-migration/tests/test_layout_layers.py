"""Unit tests for scripts/layout_layers.py -- the kind-aware background-layer classifier.

Pins the two things the corpus finding proved must hold: a full-canvas DECORATION image is a
background layer (so the auditor can exempt it from the colliding-tile scan), and a full-bleed
WORKSHEET is NOT (dropping a real chart from the scan would hide a genuine defect). Also pins the
both-axis blanket geometry, the inclusive area threshold, robustness on junk input, and the tunables.
"""
import layout_layers as ll

PAGE = (0, 0, 1280, 720)


def _leaf(kind, rect):
    return {"leaf_kind": kind, "rect": rect}


# -- the two load-bearing cases -------------------------------------------------
def test_full_canvas_bitmap_is_background():
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 1280, 720)), PAGE) is True


def test_full_canvas_worksheet_is_not_background():
    # The corpus guardrail: "Engagements by Dimension" covers 100% of its page but is a real chart.
    assert ll.is_background_leaf(_leaf("worksheet", (0, 0, 1280, 720)), PAGE) is False


def test_full_canvas_text_is_not_background():
    assert ll.is_background_leaf(_leaf("text", (0, 0, 1280, 720)), PAGE) is False


def test_full_canvas_legend_and_filter_are_not_background():
    assert ll.is_background_leaf(_leaf("legend", (0, 0, 1280, 720)), PAGE) is False
    assert ll.is_background_leaf(_leaf("filter", (0, 0, 1280, 720)), PAGE) is False


# -- geometry --------------------------------------------------------------------
def test_full_bleed_bitmap_is_background():
    # A backdrop that bleeds past the page on both axes still blankets it.
    assert ll.is_background_leaf(_leaf("bitmap", (-20, -20, 1360, 800)), PAGE) is True


def test_slightly_inset_bitmap_is_background():
    assert ll.is_background_leaf(_leaf("bitmap", (20, 20, 1240, 680)), PAGE) is True


def test_partial_bitmap_is_not_background():
    # ~41% of the page (a section-panel graphic), not a full backdrop.
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 1280, 295)), PAGE) is False


def test_wide_short_banner_bitmap_is_not_background():
    # Full page WIDTH but only 11% height -> a header band, fails the height span (and area).
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 1280, 80)), PAGE) is False


def test_tall_narrow_rail_bitmap_is_not_background():
    # Full page HEIGHT but ~9% width -> a side rail, fails the width span.
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 120, 720)), PAGE) is False


def test_eighty_percent_each_axis_is_not_background():
    # 80% x 80% = 64% area, both spans below 0.85 -> an inset chart, not a backdrop.
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 1024, 576)), PAGE) is False


def test_area_threshold_is_inclusive():
    # On a 1000x1000 page, 850x1000 = 0.85 area with both spans >= 0.85 -> background (inclusive).
    page = (0, 0, 1000, 1000)
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 850, 1000)), page) is True
    # 850x999 keeps both spans >= 0.85 but drops area to 0.849 -> the area gate rejects it.
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 850, 999)), page) is False


def test_span_gate_rejects_bleeding_sliver():
    # A tall thin sliver whose bleed makes AREA >= 0.85 but width span only 0.2: the both-axis span
    # gate (not the area gate) is what rejects it -> proves the span gate is not redundant.
    page = (0, 0, 1000, 1000)
    r = ll.rect_blankets_page((0, 0, 200, 6000), page)
    assert r is False
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 200, 6000)), page) is False


# -- robustness ------------------------------------------------------------------
def test_missing_rect_is_not_background():
    assert ll.is_background_leaf({"leaf_kind": "bitmap"}, PAGE) is False


def test_zero_dim_rect_is_not_background():
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 0, 720)), PAGE) is False
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 1280, 0)), PAGE) is False


def test_malformed_rect_never_raises():
    assert ll.is_background_leaf(_leaf("bitmap", ("x", "y", "w", "h")), PAGE) is False
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0)), PAGE) is False
    assert ll.is_background_leaf(_leaf("bitmap", None), PAGE) is False


def test_non_dict_leaf_is_not_background():
    assert ll.is_background_leaf((0, 0, 1280, 720), PAGE) is False
    assert ll.is_background_leaf(None, PAGE) is False


def test_degenerate_page_is_never_background():
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 1280, 720)), (0, 0, 0, 0)) is False
    assert ll.is_background_leaf(_leaf("bitmap", (0, 0, 1280, 720)), None) is False


# -- collection API --------------------------------------------------------------
def test_background_leaves_filters_and_preserves_order():
    a = _leaf("bitmap", (0, 0, 1280, 720))    # background
    b = _leaf("worksheet", (0, 0, 1280, 720))  # full-bleed chart, not background
    c = _leaf("bitmap", (0, 0, 200, 120))     # small logo, not background
    d = _leaf("bitmap", (-5, -5, 1290, 730))  # second backdrop (bleed)
    out = ll.background_leaves([a, b, c, d], PAGE)
    assert out == [a, d]


def test_background_leaves_empty_and_none_input():
    assert ll.background_leaves([], PAGE) == []
    assert ll.background_leaves(None, PAGE) == []


def test_custom_thresholds_are_honored():
    # Relaxing the thresholds lets a half-page bitmap count (a caller may tune for a different use).
    half = _leaf("bitmap", (0, 0, 1280, 400))  # ~56% area, spans 1.0 x 0.56
    assert ll.is_background_leaf(half, PAGE) is False
    assert ll.is_background_leaf(half, PAGE, cover=0.5, span=0.5) is True


# -- pure-geometry helper is kind-agnostic --------------------------------------
def test_rect_blankets_page_is_kind_agnostic():
    # rect_blankets_page knows nothing about leaf kind: a worksheet rect DOES blanket the page...
    assert ll.rect_blankets_page((0, 0, 1280, 720), PAGE) is True
    # ...but is_background_leaf still rejects it because of the kind gate.
    assert ll.is_background_leaf(_leaf("worksheet", (0, 0, 1280, 720)), PAGE) is False


# -- tunables --------------------------------------------------------------------
def test_documented_tunable_values():
    assert ll.BG_COVER == 0.85
    assert ll.BG_SPAN == 0.85
    assert ll.BG_KINDS == ("bitmap",)
    assert "worksheet" not in ll.BG_KINDS
    assert "text" not in ll.BG_KINDS
