"""Compose the layout tree, the solver and the layer classifier into ONE per-dashboard PLAN.

``zone_tree`` parses a dashboard's ``<zones>`` into a layout tree, ``layout_solve`` resolves that
tree into page-pixel rectangles, and ``layout_layers`` classifies which of the resulting leaves are
z-order decoration rather than colliding tiles. Each is a separate pure module with its own tests;
this module is the single seam that runs all three and hands the emit path one lookup table:

    plan["rects"][zone_id] -> (x, y, w, h)      the page-pixel rectangle for that source zone
    plan["kinds"][zone_id] -> leaf_kind         worksheet / text / filter / paramctrl / legend / ...
    plan["background"]     -> frozenset(id)     full-canvas backdrops        (tier 1)
    plan["panel"]          -> frozenset(id)     sub-region decoration panels (tier 2)
    plan["overlay"]        -> frozenset(id)     author-intended floats       (tier 3)
    plan["page"]           -> (w, h)            possibly GROWN past the requested page
    plan["grew"]           -> bool              whether growth actually fired

The ``zone_id`` key is the seam ``_parse_dashboard`` records on every captured item, so an emit-side
lookup is exact -- rect-matching is not a substitute, because a single-child ``layout-flow`` wrapper
persists its child's rect exactly and so names two zones.

**Hoisted floats are placed here, not by the solver.** ``zone_tree`` lifts every ``floating='true'``
zone out of the flow into ``tree["floats"]`` (it is absolutely positioned by definition and takes
part in no flow allocation), and ``layout_solve.solve`` therefore never assigns it a rectangle. An
emit path consuming ``solve`` alone would silently lose those visuals. This module places each one
by scaling its absolute source rect into the solved page, applying the same leaf minimum, and
clamping it on-page -- so ``rects`` covers EVERY leaf the dashboard has.

**Page growth is reported, never silently applied.** ``solve`` enlarges the page to the root's
minimum so allocation cannot overflow; the caller decides whether to adopt the larger canvas (a
PBIR page rescales to the viewport under ``FitToPage``) or to keep its own. Growth is rare in
practice -- measured on the reference corpus, 11 of 13 dashboards fit their authored page exactly.

Pure, deterministic, stdlib-only. Fail-closed: an unparseable or unsolvable dashboard (or any
internal error) returns ``None`` so the caller keeps the legacy absolute-rect path. Never raises.
NO emit dependency, NO PBIR knowledge, does not import ``twb_to_pbir``.
"""
from __future__ import annotations

import layout_layers as _layers
from layout_solve import default_min_for_leaf, solve
from zone_tree import K_LEAF, parse_zone_tree

# Growth is reported when the solved page exceeds the requested one by more than this, in page
# pixels -- the same sub-pixel tolerance the solver and the auditor use.
TOL = 1.0

# Leaf kinds that never become a visual, so they take no part in classification: a ``blank`` zone is
# a genuinely empty spacer. A ``hidden`` (``hidden-by-user``) zone is deliberately NOT excluded --
# Tableau's show/hide toggle is not a delete, and the emit path surfaces such a zone anyway, so it
# occupies real page area and must be classified like anything else.
SKIP_KINDS = ("blank",)


def _leaf_nodes(node, out):
    if node.get("kind") == K_LEAF:
        out.append(node)
        return
    for c in node.get("children", ()) or ():
        _leaf_nodes(c, out)


def _leaves_of(node):
    """Every leaf at or under ``node`` (a hoisted float may itself be a floating layout box)."""
    out = []
    _leaf_nodes(node, out)
    return out


def _clamp_on_page(rect, pw, ph):
    """Keep an absolutely-placed rect inside the page without changing its size where possible."""
    x, y, w, h = rect
    w = max(0.0, min(w, pw))
    h = max(0.0, min(h, ph))
    x = max(0.0, min(x, pw - w))
    y = max(0.0, min(y, ph - h))
    return (x, y, w, h)


def _place_float(node, extent, pw, ph, resolver):
    """An absolutely-positioned float's page rect: scale its source box, floor it, clamp it on-page.

    There is no flow to solve for a float -- the author pinned it at an absolute position -- so the
    faithful placement is its source rect mapped into the page, exactly as the tree's extent maps
    every other coordinate. Returns ``None`` when the source rect or the extent is unusable.
    """
    src = node.get("src") or {}
    ew, eh = extent.get("w"), extent.get("h")
    if not ew or not eh or ew <= 0 or eh <= 0:
        return None
    try:
        sx, sy = float(src.get("x") or 0.0), float(src.get("y") or 0.0)
        sw, sh = float(src.get("w") or 0.0), float(src.get("h") or 0.0)
    except (TypeError, ValueError):
        return None
    if sw <= 0 or sh <= 0:
        return None
    kx, ky = pw / float(ew), ph / float(eh)
    mw, mh = resolver(node)
    return _clamp_on_page(
        (sx * kx, sy * ky, max(sw * kx, float(mw)), max(sh * ky, float(mh))), pw, ph)


def build_plan(db, device_zones=None, page_w=1280.0, page_h=720.0, min_for_leaf=None):
    """Build the solved layout plan for one ``<dashboard>`` Element, or ``None`` (fail-closed).

    ``db`` is the dashboard Element; ``device_zones`` is the ``<devicelayouts>`` zone set the caller
    already excludes (pass the same set ``_parse_dashboard`` builds). ``page_w`` / ``page_h`` are the
    page pixels the caller intends to emit at. ``min_for_leaf`` is an optional
    ``node -> (min_w, min_h)`` resolver forwarded to the solver.

    Returns the plan dict described in the module docstring, or ``None``. Never raises.
    """
    try:
        pw_req, ph_req = float(page_w), float(page_h)
        if pw_req <= 0 or ph_req <= 0:
            return None
        tree = parse_zone_tree(db, device_zones)
        if not tree:
            return None
        resolver = min_for_leaf or default_min_for_leaf
        res = solve(tree, (0.0, 0.0, pw_req, ph_req), min_for_leaf=resolver)
        if res is None:
            return None

        pw, ph = float(res["page"][2]), float(res["page"][3])
        rects = dict(res["rects"])

        leaves = []
        _leaf_nodes(tree["root"], leaves)

        # Hoisted floats never reach the solver, so place them here and add them to BOTH the rect
        # table and the classified leaf set -- an overlay pinned over a chart is exactly the case
        # tier 3 exists for, and omitting it would both lose the visual and skew the audit.
        for node in tree.get("floats") or ():
            for fl in _leaves_of(node):
                rect = _place_float(fl, tree.get("extent") or {}, pw, ph, resolver)
                if rect is None:
                    continue
                fl["rect"] = rect
                if fl.get("zone_id") is not None:
                    rects[fl["zone_id"]] = rect
                leaves.append(fl)

        page_rect = (0.0, 0.0, pw, ph)
        visible = [lf for lf in leaves
                   if lf.get("leaf_kind") not in SKIP_KINDS and lf.get("rect") is not None]

        def _ids(sub):
            return frozenset(lf["zone_id"] for lf in sub if lf.get("zone_id") is not None)

        return {
            "rects": rects,
            "kinds": {lf["zone_id"]: lf.get("leaf_kind") for lf in leaves
                      if lf.get("zone_id") is not None},
            "background": _ids(_layers.background_leaves(visible, page_rect)),
            "panel": _ids(_layers.panel_leaves(visible)),
            "overlay": _ids(_layers.floating_overlay_leaves(visible)),
            "page": (pw, ph),
            "grew": (pw > pw_req + TOL) or (ph > ph_req + TOL),
        }
    except Exception:   # fail-closed: never raise into the emit path
        return None


def is_decoration(plan, zone_id):
    """True when ``zone_id`` was classified into ANY of the three z-order decoration tiers.

    The single question the emit path asks per visual: should this be sent to back (and exempted
    from the geometry audit) rather than treated as a colliding tile? Returns False for an unknown
    id or a ``None`` plan, so a caller can ask unconditionally.
    """
    if not plan or zone_id is None:
        return False
    return (zone_id in plan["background"] or zone_id in plan["panel"]
            or zone_id in plan["overlay"])
