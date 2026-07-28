"""Classify a dashboard's BACKGROUND / DECORATION layers (offline, stdlib-only).

A Tableau dashboard routinely places a single full-canvas image behind everything -- a branded
backdrop, a watermark, a section-panel graphic -- as the FIRST (bottom) zone. Faithfully rebuilt as
a positioned Power BI image visual it covers the whole page, so a naive geometry audit reports it as
"containing" every chart on the page (in the reference corpus TWO such backdrops account for ~49 of
the 93 containments the solver projection would otherwise score). That containment is not a defect:
the backdrop is a z-order layer, sent to back, and the content sits on top of it BY DESIGN -- exactly
the intent the donut composite-group exemption already encodes for a different stacked case.

This module is the shared, kind-aware test for "is this leaf a background layer?" so both the
geometry auditor (to exempt a backdrop from the colliding-tile scan) and, later, the emit path (to
send it to back / route it to the page background) decide it from ONE implementation.

**Kind-aware on purpose.** Coverage alone is NOT sufficient and would be actively wrong: a real
worksheet can legitimately be full-bleed (a map, a single big chart with KPI cards floated on top --
"Engagements by Dimension" in the corpus covers 100 % of its page and contains seven cards), and
dropping THAT from the collision scan would hide a genuine layout problem. So only a DECORATION leaf
whose ``leaf_kind`` is an image (``bitmap`` -- the kind ``zone_tree`` assigns both a straight
``type-v2='bitmap'`` and an image-hosting ``dashboard-object``) can ever be a background. A
worksheet, text, filter, legend or param control never qualifies here, whatever its size.

**Geometry test.** A background must blanket the page, not merely be wide: it must cover
``BG_COVER`` (85 %) of the page AREA *and* span ``BG_SPAN`` (85 %) of BOTH the page width and height.
Requiring both-axis span is what distinguishes a backdrop from a full-width header BAND (which fails
the height span) or a tall side rail (which fails the width span) -- those are decorations handled by
other passes, not backdrops.

Pure, deterministic, stdlib-only. NO emit dependency, NO auditor import, NO ``zone_tree`` import: it
reads only ``rect`` + ``leaf_kind`` off plain node dicts, so it can be unit-tested in isolation and
imported by either side without a cycle. Never raises: a malformed node is simply "not a background".

**Three tiers of the same z-order idea.** A z-order layer comes in three flavours and all are exempt
from the colliding-tile scan for the same reason -- something the content is deliberately stacked WITH
inside a frame, not a tiling accident:

* PAGE background -- a full-canvas image behind everything (:func:`is_background_leaf` /
  :func:`background_leaves`), the branded backdrop above.
* SUB-REGION panel -- a static-decoration leaf (a colored section panel or a title / divider strip)
  that does not blanket the whole page but ENCLOSES the content of one region
  (:func:`is_decoration_leaf` / :func:`panel_leaves`). A Tableau author routinely drops a filled
  text/shape object behind a KPI+chart cluster and floats the worksheets on top within the same
  frame; faithfully projected, that panel encloses the cluster, which a naive scan miscounts as
  containment (in the reference corpus the branded SF-Admin panels account for ~23 such false
  containments). Sent to back it is a background for its region, not a colliding tile.
* FLOATING overlay -- an interactive control or annotation (a ``filter`` / ``paramctrl`` slicer or a
  ``text`` label) the author FLOATS on top of a chart within a frame (:func:`is_overlay_leaf` /
  :func:`floating_overlay_leaves`). A Tableau ``layout-basic`` frame permits overlap by design, so a
  filter pinned to a chart corner or a caption dropped inside a plot is intentional geometry the
  solver faithfully reproduces -- not a defect. This is the inverse relation to a panel: the overlay
  is the SMALL thing on top (often CONTAINED BY the chart, which the panel test -- decoration that
  contains -- never catches), so it is detected by "does this control/label collide with anything?"
  In the reference corpus these floating controls/labels account for the entire residual after the
  first two tiers (Tech Hierarchy's filter pile over its chart, section labels inside plots, stacked
  control+caption clusters) -- 42 of 43 remaining collisions.

Every tier is **kind-gated**, and that gate is the load-bearing guardrail. For panels only
``text`` / ``bitmap`` static decoration qualifies; for floating overlays only ``text`` / ``filter`` /
``paramctrl`` control-or-annotation kinds qualify. A ``worksheet`` is NEVER exempt by any tier, so two
overlapping worksheets ("data hidden by data") remain a real, audited defect -- the one thing this
whole module must never hide. The panel and floating tiers are RELATIONAL (they depend on what else is
on the page), so :func:`panel_leaves` / :func:`floating_overlay_leaves` take the sibling set, while the
per-leaf :func:`is_decoration_leaf` / :func:`is_overlay_leaf` only report kind.
"""
from __future__ import annotations

# -- tunables (one place so tests can assert against them) -----------------------
# Fraction of the page AREA a backdrop must cover.
BG_COVER = 0.85
# Fraction of the page WIDTH and (separately) HEIGHT a backdrop must span. Both-axis gate is what
# separates a true full-canvas backdrop from a full-width header band or a full-height side rail.
BG_SPAN = 0.85

# ``zone_tree`` leaf kinds that can be a background layer. Only image decoration qualifies -- a
# worksheet/text/filter/legend/paramctrl is content, never a backdrop, regardless of its size.
BG_KINDS = ("bitmap",)

# Static, non-data leaf kinds that can serve as a SUB-REGION background PANEL when they enclose
# other content: a filled section panel or a title/divider strip. Text is included here (a page
# background is image-only, but a sub-region panel is very often a filled TEXT/shape object), so
# PANEL_KINDS is deliberately broader than BG_KINDS. A worksheet/filter/legend/paramctrl is data or
# interactive content and never a panel, whatever it encloses.
PANEL_KINDS = ("text", "bitmap")

# Control/annotation leaf kinds that can serve as a FLOATING OVERLAY when the author pins them on top
# of a chart inside a frame: an interactive slicer (``filter`` / ``paramctrl``) or a ``text`` label /
# caption. A Tableau ``layout-basic`` frame permits overlap by design, so such an overlay collides
# with the content it floats on BY INTENT, not by accident. Deliberately EXCLUDES ``worksheet`` (two
# overlapping charts is a real defect this module must never hide) and ``bitmap`` (image decoration is
# already covered by the background/panel tiers; a small icon that overlaps a label is exempted via
# the label side of the pair, so it needs no separate kind here). ``legend`` / ``blank`` are excluded
# too -- neither appears as a residual float in the corpus and both would only broaden the exemption.
FLOAT_KINDS = ("text", "filter", "paramctrl")

# Edge tolerance for the enclosure test, matching the geometry auditor's ``contains`` (TOL = 1.0) so
# a panel this module exempts is exactly one the auditor would otherwise have scored as containment.
_CONTAIN_TOL = 1.0


def _rect_of(leaf):
    """The ``(x, y, w, h)`` rect of a leaf node, or ``None`` if it has no resolved rect.

    Accepts either a solver-resolved node (``leaf['rect']`` is an ``(x, y, w, h)`` tuple) or a
    plain ``(x, y, w, h)`` passed directly. Returns ``None`` for anything unusable.
    """
    r = leaf.get("rect") if isinstance(leaf, dict) else leaf
    if r is None:
        return None
    try:
        x, y, w, h = float(r[0]), float(r[1]), float(r[2]), float(r[3])
    except (TypeError, ValueError, IndexError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (x, y, w, h)


def rect_blankets_page(rect, page_rect, cover=BG_COVER, span=BG_SPAN):
    """True when ``rect`` covers ``cover`` of the page area AND spans ``span`` of BOTH axes.

    Pure geometry -- no leaf-kind opinion. ``page_rect`` is ``(x, y, w, h)``; a degenerate page
    (zero/negative extent) is never blanketed. Used by :func:`is_background_leaf` after the kind
    gate, and exposed so a caller can reuse the same both-axis blanket test elsewhere.
    """
    r = _rect_of(rect)
    if r is None or page_rect is None:
        return False
    try:
        pw, ph = float(page_rect[2]), float(page_rect[3])
    except (TypeError, ValueError, IndexError):
        return False
    if pw <= 0 or ph <= 0:
        return False
    _x, _y, w, h = r
    if (w / pw) < span or (h / ph) < span:
        return False
    return (w * h) / (pw * ph) >= cover


def is_background_leaf(leaf, page_rect, cover=BG_COVER, span=BG_SPAN):
    """True when ``leaf`` is a background layer: a DECORATION image that blankets the page.

    ``leaf`` is a node dict carrying ``leaf_kind`` and a resolved ``rect`` (or ``rect`` may be
    ``(x, y, w, h)`` on the node). The kind gate (:data:`BG_KINDS`) is applied first, so a
    full-bleed worksheet or a page-spanning text band is never a background here. Never raises.
    """
    if not isinstance(leaf, dict):
        return False
    if leaf.get("leaf_kind") not in BG_KINDS:
        return False
    return rect_blankets_page(leaf, page_rect, cover=cover, span=span)


def background_leaves(leaves, page_rect, cover=BG_COVER, span=BG_SPAN):
    """The sublist of ``leaves`` that are background layers, preserving order.

    ``leaves`` is any iterable of leaf node dicts. Deterministic and side-effect free -- it does not
    mutate the nodes. Returns ``[]`` for empty / all-content input.
    """
    return [lf for lf in (leaves or []) if is_background_leaf(lf, page_rect, cover=cover, span=span)]


def _rect_contains(outer, inner, tol=_CONTAIN_TOL):
    """True when ``inner`` ``(x, y, w, h)`` is fully inside ``outer`` within ``tol`` on every edge.

    Deliberately identical to the geometry auditor's ``contains`` so the set of panels this module
    exempts is exactly the set of containments the auditor would otherwise count. Both args must be
    real ``(x, y, w, h)`` rects (as returned by :func:`_rect_of`); ``None`` is never contained.
    """
    if outer is None or inner is None:
        return False
    ax, ay, aw, ah = outer
    bx, by, bw, bh = inner
    return (bx >= ax - tol and by >= ay - tol and
            bx + bw <= ax + aw + tol and by + bh <= ay + ah + tol)


def is_decoration_leaf(leaf):
    """True when ``leaf`` is a STATIC, non-data decoration object (text or image).

    These are the only kinds that may act as a sub-region background panel. A worksheet, filter,
    legend or param control is content and never a panel, whatever its geometry. This is the
    per-leaf KIND test only -- whether such a leaf is *actually* a panel additionally requires it to
    enclose something, which is what :func:`panel_leaves` decides. Never raises.
    """
    return isinstance(leaf, dict) and leaf.get("leaf_kind") in PANEL_KINDS


def panel_leaves(leaves, tol=_CONTAIN_TOL):
    """Decoration leaves that ENCLOSE >=1 OTHER leaf -- sub-region background panels.

    The sub-region analogue of :func:`background_leaves`: a branded section panel or a title/divider
    strip authored behind its content encloses the worksheets/cards floated on top of it, which a
    naive collision scan miscounts as containment. Returns the decoration leaves that should be sent
    to back / exempted.

    KIND-GATED via :func:`is_decoration_leaf`: only text/image leaves qualify, so a full-bleed
    WORKSHEET that encloses cards is NEVER a panel and its containment stays a real, audited defect.
    A page-blanketing background image also encloses everything and so appears here too -- harmless,
    since both tiers are exempted the same way (a caller that wants only the INCREMENTAL sub-region
    set can subtract :func:`background_leaves`). Relational (needs the sibling set), order-preserving,
    does not mutate the nodes, and never raises.
    """
    items = list(leaves or [])
    rects = [_rect_of(lf) for lf in items]
    out = []
    for i, li in enumerate(items):
        if rects[i] is None or not is_decoration_leaf(li):
            continue
        for j in range(len(items)):
            if i == j or rects[j] is None:
                continue
            if _rect_contains(rects[i], rects[j], tol=tol):
                out.append(li)
                break
    return out


def _rects_collide(a, b, tol=_CONTAIN_TOL):
    """True when rects ``a`` / ``b`` collide by the geometry auditor's OWN pairwise test.

    Replicates ``geometry_audit.geometry_defects`` exactly so the set of overlays this module exempts
    is precisely the set of collisions the auditor would otherwise have scored: the raw intersection
    must exceed 4 px^2 (a shared edge/corner is not a collision), and then the pair counts if one rect
    is fully nested in the other (within ``tol``) OR the intersection exceeds 2 % of the smaller rect.
    Both args are ``(x, y, w, h)`` rects (as from :func:`_rect_of`); ``None`` never collides.
    """
    if a is None or b is None:
        return False
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    ia = ix * iy
    if ia <= 4.0:
        return False
    if _rect_contains(a, b, tol=tol) or _rect_contains(b, a, tol=tol):
        return True
    small = min(a[2] * a[3], b[2] * b[3]) or 1
    return (ia / small) > 0.02


def is_overlay_leaf(leaf):
    """True when ``leaf`` is a control/annotation kind that can be a floating overlay.

    These are the only kinds that may be exempted as an author-intended float: an interactive slicer
    (``filter`` / ``paramctrl``) or a ``text`` label/caption. A worksheet, bitmap, legend or blank is
    never an overlay here (see :data:`FLOAT_KINDS`). This is the per-leaf KIND test only -- whether
    such a leaf is *actually* floating additionally requires it to collide with a sibling, which is
    what :func:`floating_overlay_leaves` decides. Never raises.
    """
    return isinstance(leaf, dict) and leaf.get("leaf_kind") in FLOAT_KINDS


def floating_overlay_leaves(leaves, tol=_CONTAIN_TOL):
    """Control/annotation leaves that COLLIDE with >=1 other leaf -- author-intended floats.

    The third z-order tier: a Tableau ``layout-basic`` frame lets the author pin a filter or a caption
    on top of a chart, and the solver faithfully reproduces that overlap. Such a control/label is not
    a colliding tile but an intentional overlay, so it is exempted from the overlap/containment scan
    exactly as a backdrop (:func:`background_leaves`) or a sub-region panel (:func:`panel_leaves`) is.

    KIND-GATED via :func:`is_overlay_leaf` so a ``worksheet`` is NEVER an overlay: a pair of
    overlapping worksheets ("data hidden by data") has neither member flagged here and therefore
    stays a real, audited defect -- the guardrail this module exists to protect. RELATIONAL: a
    control/label that collides with nothing (a cleanly tiled slicer or caption) is NOT flagged, so
    the exemption only ever fires on a leaf that actually overlays content. Collision is measured with
    :func:`_rects_collide` (the auditor's own test), so every overlay exempted here is exactly a
    collision the auditor would otherwise have counted. Order-preserving, does not mutate the nodes,
    never raises.
    """
    items = list(leaves or [])
    rects = [_rect_of(lf) for lf in items]
    out = []
    for i, li in enumerate(items):
        if rects[i] is None or not is_overlay_leaf(li):
            continue
        for j in range(len(items)):
            if i == j or rects[j] is None:
                continue
            if _rects_collide(rects[i], rects[j], tol=tol):
                out.append(li)
                break
    return out
