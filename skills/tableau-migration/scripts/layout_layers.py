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

**Two tiers of the same z-order idea.** A background comes in two sizes and both are exempt from the
colliding-tile scan for the same reason -- decoration the content sits ON TOP OF by design:

* PAGE background -- a full-canvas image behind everything (:func:`is_background_leaf` /
  :func:`background_leaves`), the branded backdrop above.
* SUB-REGION panel -- a static-decoration leaf (a colored section panel or a title / divider strip)
  that does not blanket the whole page but ENCLOSES the content of one region
  (:func:`is_decoration_leaf` / :func:`panel_leaves`). A Tableau author routinely drops a filled
  text/shape object behind a KPI+chart cluster and floats the worksheets on top within the same
  frame; faithfully projected, that panel encloses the cluster, which a naive scan miscounts as
  containment (in the reference corpus the branded SF-Admin panels account for ~23 such false
  containments). Sent to back it is a background for its region, not a colliding tile.

The sub-region tier is **kind-gated exactly like the page tier**: only ``text`` / ``bitmap`` static
decoration can be a panel, so a full-bleed WORKSHEET that encloses cards ("Engagements by Dimension")
is never a panel and its containment stays a real, audited defect. Unlike the page tier, "is a panel"
is RELATIONAL -- it depends on what else is on the page -- so :func:`panel_leaves` takes the sibling
set, while the per-leaf :func:`is_decoration_leaf` only reports kind.
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
