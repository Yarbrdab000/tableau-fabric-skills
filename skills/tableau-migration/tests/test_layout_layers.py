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


# ===============================================================================
# floating-OVERLAY classifier (ft3): a control/annotation leaf pinned ON content
# ===============================================================================
def test_is_overlay_leaf_kinds():
    assert ll.is_overlay_leaf(_leaf("text", (0, 0, 10, 10))) is True
    assert ll.is_overlay_leaf(_leaf("filter", (0, 0, 10, 10))) is True
    assert ll.is_overlay_leaf(_leaf("paramctrl", (0, 0, 10, 10))) is True
    # never a float: a worksheet (the guardrail), image decoration, legend, or empty spacer.
    assert ll.is_overlay_leaf(_leaf("worksheet", (0, 0, 10, 10))) is False
    assert ll.is_overlay_leaf(_leaf("bitmap", (0, 0, 10, 10))) is False
    assert ll.is_overlay_leaf(_leaf("legend", (0, 0, 10, 10))) is False
    assert ll.is_overlay_leaf(_leaf("blank", (0, 0, 10, 10))) is False


def test_is_overlay_leaf_robustness():
    assert ll.is_overlay_leaf(None) is False
    assert ll.is_overlay_leaf((0, 0, 10, 10)) is False
    assert ll.is_overlay_leaf({"rect": (0, 0, 10, 10)}) is False  # no leaf_kind


def test_text_label_inside_chart_is_float():
    # The annotation-inside-chart case (Clients / Hierarchy Trending): a caption CONTAINED BY the
    # chart. panel_leaves cannot catch this (the text does not enclose the chart) -- this tier does.
    chart = _leaf("worksheet", (0, 0, 600, 400))
    label = _leaf("text", (40, 20, 150, 30))
    assert ll.floating_overlay_leaves([chart, label]) == [label]
    # and it is NOT a panel (the inverse relation): the label encloses nothing.
    assert ll.panel_leaves([chart, label]) == []


def test_filter_and_paramctrl_over_chart_are_floats():
    # Tech Hierarchy: filters pinned on top of the chart; Staff Capacity: param controls over a chart.
    chart = _leaf("worksheet", (0, 0, 600, 400))
    filt = _leaf("filter", (450, 20, 120, 40))    # partly over the chart's top-right
    pc = _leaf("paramctrl", (20, 350, 200, 40))   # partly over the chart's bottom-left
    assert ll.floating_overlay_leaves([chart, filt, pc]) == [filt, pc]


def test_two_overlapping_worksheets_are_not_floats():
    # THE guardrail: "data hidden by data" is never exempted -- neither worksheet is a float, so the
    # pair stays a real, audited defect (this is the one thing the module must never hide).
    a = _leaf("worksheet", (0, 0, 400, 300))
    b = _leaf("worksheet", (100, 80, 400, 300))
    assert ll.floating_overlay_leaves([a, b]) == []


def test_control_or_label_colliding_with_nothing_is_not_float():
    # Relational: a cleanly tiled slicer / caption that overlays nothing is NOT flagged (so it stays
    # audited like any other tile) -- the exemption only ever fires on a leaf that actually overlays.
    filt = _leaf("filter", (0, 0, 200, 40))
    chart = _leaf("worksheet", (0, 100, 300, 200))   # below, no overlap
    label = _leaf("text", (400, 0, 120, 30))          # off to the side
    assert ll.floating_overlay_leaves([filt, chart, label]) == []


def test_float_stacked_control_and_label():
    # filter + text overlapping each other (a control with its caption) -- both are overlays, so both
    # are flagged; the pair is exempt from either side.
    filt = _leaf("filter", (0, 0, 200, 60))
    cap = _leaf("text", (10, 40, 150, 40))            # overlaps the filter's lower edge
    out = ll.floating_overlay_leaves([filt, cap])
    assert out == [filt, cap]


def test_bitmap_icon_over_label_exempt_via_label_side():
    # An icon (bitmap, NOT a float kind) overlapping a text label (EBI cluster): the bitmap is not
    # flagged, but the text IS, so the pair is still exempt from the auditor via the label member.
    icon = _leaf("bitmap", (0, 0, 40, 40))
    label = _leaf("text", (20, 10, 120, 24))
    assert ll.floating_overlay_leaves([icon, label]) == [label]


def test_float_partial_overlap_honors_auditor_threshold():
    # A control grazing a chart below the auditor's 2 %-of-smaller floor is NOT a float (it would not
    # be counted by the auditor either), so the classifier and the auditor agree exactly.
    chart = _leaf("worksheet", (0, 0, 100, 100))       # area 10000
    graze = _leaf("filter", (99.5, 0, 100, 100))       # intersection 0.5*100=50 -> 0.5 % < 2 %
    assert ll.floating_overlay_leaves([chart, graze]) == []
    real = _leaf("filter", (90, 0, 100, 100))          # intersection 10*100=1000 -> 10 % > 2 %
    assert ll.floating_overlay_leaves([chart, real]) == [real]


def test_rects_collide_matches_auditor_thresholds():
    # Direct unit of the shared collide test so it can never silently drift from the auditor.
    a = (0, 0, 100, 100)
    assert ll._rects_collide(a, (10, 10, 20, 20)) is True          # fully nested -> collide
    assert ll._rects_collide(a, (90, 0, 100, 100)) is True         # 10 % partial -> collide
    assert ll._rects_collide(a, (99.5, 0, 100, 100)) is False      # 0.5 % partial -> below floor
    assert ll._rects_collide((0, 0, 10, 10), (10, 0, 10, 10)) is False  # shared edge -> ia 0
    assert ll._rects_collide((0, 0, 10, 10), (8, 8, 10, 10)) is False   # ia == 4 -> not > 4
    assert ll._rects_collide(a, None) is False


def test_floating_overlay_leaves_preserves_order_and_filters():
    f1 = _leaf("filter", (450, 20, 120, 40))     # float (over chart)
    chart = _leaf("worksheet", (0, 0, 600, 400))  # content
    t1 = _leaf("text", (40, 20, 150, 30))         # float (inside chart)
    lone = _leaf("text", (1000, 0, 50, 30))       # caption overlaying nothing
    ws2 = _leaf("worksheet", (700, 0, 100, 100))  # a second chart, no overlap
    out = ll.floating_overlay_leaves([f1, chart, t1, lone, ws2])
    assert out == [f1, t1]


def test_floating_overlay_leaves_empty_and_none_input():
    assert ll.floating_overlay_leaves([]) == []
    assert ll.floating_overlay_leaves(None) == []


def test_floating_overlay_leaves_robust_to_junk_rects():
    chart = _leaf("worksheet", (0, 0, 600, 400))
    good = _leaf("text", (40, 20, 150, 30))
    junk1 = _leaf("filter", None)
    junk2 = _leaf("text", ("x", "y", "w", "h"))
    junk3 = {"leaf_kind": "paramctrl"}  # no rect
    out = ll.floating_overlay_leaves([chart, good, junk1, junk2, junk3])
    assert out == [good]


def test_float_documented_tunables():
    assert ll.FLOAT_KINDS == ("text", "filter", "paramctrl")
    assert "worksheet" not in ll.FLOAT_KINDS
    assert "bitmap" not in ll.FLOAT_KINDS
