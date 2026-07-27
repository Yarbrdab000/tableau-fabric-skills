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


# ===============================================================================
# sub-region PANEL classifier (ft2): a static-decoration leaf that ENCLOSES content
# ===============================================================================
def test_is_decoration_leaf_kinds():
    assert ll.is_decoration_leaf(_leaf("text", (0, 0, 10, 10))) is True
    assert ll.is_decoration_leaf(_leaf("bitmap", (0, 0, 10, 10))) is True
    assert ll.is_decoration_leaf(_leaf("worksheet", (0, 0, 10, 10))) is False
    assert ll.is_decoration_leaf(_leaf("filter", (0, 0, 10, 10))) is False
    assert ll.is_decoration_leaf(_leaf("legend", (0, 0, 10, 10))) is False
    assert ll.is_decoration_leaf(_leaf("paramctrl", (0, 0, 10, 10))) is False


def test_is_decoration_leaf_robustness():
    assert ll.is_decoration_leaf(None) is False
    assert ll.is_decoration_leaf((0, 0, 10, 10)) is False
    assert ll.is_decoration_leaf({"rect": (0, 0, 10, 10)}) is False  # no leaf_kind


def test_text_panel_enclosing_worksheet_is_panel():
    # The SF-Admin case: a branded text/shape panel behind a chart cluster.
    panel = _leaf("text", (100, 100, 400, 300))
    chart = _leaf("worksheet", (120, 140, 300, 200))
    assert ll.panel_leaves([panel, chart]) == [panel]


def test_bitmap_cluster_enclosing_icons_is_panel():
    # The EBI case: a decoration image enclosing smaller images.
    outer = _leaf("bitmap", (0, 0, 200, 60))
    i1 = _leaf("bitmap", (5, 5, 40, 40))
    i2 = _leaf("bitmap", (60, 5, 40, 40))
    assert ll.panel_leaves([outer, i1, i2]) == [outer]


def test_full_bleed_worksheet_enclosing_cards_is_not_panel():
    # THE guardrail: a real full-bleed chart that contains cards is content, never a panel, so its
    # containment stays a real defect for the frame-child slice to resolve.
    chart = _leaf("worksheet", (0, 0, 1280, 720))
    c1 = _leaf("worksheet", (40, 40, 200, 120))
    c2 = _leaf("text", (300, 40, 150, 60))
    assert ll.panel_leaves([chart, c1, c2]) == []


def test_decoration_enclosing_nothing_is_not_panel():
    # A lone caption / KPI text tile that encloses no sibling is not a panel -- it is still audited.
    a = _leaf("text", (0, 0, 200, 40))
    b = _leaf("worksheet", (0, 100, 300, 200))  # beside/below, not inside a
    assert ll.panel_leaves([a, b]) == []


def test_panel_may_enclose_pure_decoration():
    # A thin divider/banner enclosing only a small text label still counts (decoration-over-
    # decoration is a z-order stack, not a data collision) -- matches the corpus ar~51 divider case.
    divider = _leaf("text", (0, 0, 600, 12))
    label = _leaf("text", (10, 2, 80, 8))
    assert ll.panel_leaves([divider, label]) == [divider]


def test_panel_leaves_preserves_order_and_filters():
    p1 = _leaf("text", (0, 0, 500, 400))       # panel (encloses w1)
    w1 = _leaf("worksheet", (20, 20, 200, 200))  # content inside p1
    p2 = _leaf("bitmap", (600, 0, 300, 300))   # panel (encloses w2)
    w2 = _leaf("worksheet", (620, 20, 100, 100))  # content inside p2
    lone = _leaf("text", (1000, 0, 50, 50))    # decoration enclosing nothing
    out = ll.panel_leaves([p1, w1, p2, w2, lone])
    assert out == [p1, p2]


def test_panel_enclosure_honors_tolerance():
    # Inner edge 1px outside the panel is still "contained" (tol=1.0), matching the auditor.
    panel = _leaf("text", (100, 100, 200, 200))
    inner = _leaf("worksheet", (99, 99, 202, 202))  # each edge 1px past -> within tol
    assert ll.panel_leaves([panel, inner]) == [panel]
    # 2px past on an edge -> not contained.
    outside = _leaf("worksheet", (97, 100, 205, 200))
    assert ll.panel_leaves([panel, outside]) == []


def test_page_background_also_appears_in_panels_and_is_subtractable():
    # A page-blanketing backdrop encloses everything, so it shows up in panel_leaves too; the
    # INCREMENTAL sub-region set is panel_leaves minus background_leaves.
    backdrop = _leaf("bitmap", (0, 0, 1280, 720))
    panel = _leaf("text", (100, 100, 400, 300))
    chart = _leaf("worksheet", (120, 140, 300, 200))
    leaves = [backdrop, panel, chart]
    panels = ll.panel_leaves(leaves)
    assert backdrop in panels and panel in panels
    bg = ll.background_leaves(leaves, PAGE)
    incremental = [p for p in panels if p not in bg]
    assert incremental == [panel]


def test_panel_leaves_empty_and_none_input():
    assert ll.panel_leaves([]) == []
    assert ll.panel_leaves(None) == []


def test_panel_leaves_robust_to_junk_rects():
    good = _leaf("text", (0, 0, 400, 300))
    inner = _leaf("worksheet", (10, 10, 100, 100))
    junk1 = _leaf("text", None)
    junk2 = _leaf("bitmap", ("x", "y", "w", "h"))
    junk3 = {"leaf_kind": "text"}  # no rect
    out = ll.panel_leaves([good, inner, junk1, junk2, junk3])
    assert out == [good]


def test_panel_documented_tunables():
    assert ll.PANEL_KINDS == ("text", "bitmap")
    assert "worksheet" not in ll.PANEL_KINDS
    assert ll._CONTAIN_TOL == 1.0
