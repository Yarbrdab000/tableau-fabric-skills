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
