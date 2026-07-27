"""Reconstruct a Tableau dashboard's LAYOUT TREE from its ``<zones>`` element (offline, stdlib-only).

Tableau dashboards are a nested flow layout, not a bag of rectangles: ``type-v2='layout-flow'``
containers stack their children on one axis (``param='vert'`` / ``'horz'``), ``type-v2='layout-basic'``
containers position children absolutely, and leaf zones are the worksheets / text / filters / legends
the author placed. A tiled (non-floating) sibling set PARTITIONS its parent's main axis -- Tableau's
engine cannot emit overlapping tiled flow zones -- so preserving the tree preserves an overlap-freeness
guarantee that the engine's current flat ``_findall_local(db, "zone")`` walk (a full ``.iter()``
descendant flatten) throws away and then pays for downstream in three repair passes.

Coordinates in the source are absolute within a normalized 100000x100000 square (a child at x=5000
inside a parent at x=0 w=100000 is at absolute 5000, NOT parent-relative -- verified against real
workbooks: every child rect nests inside its parent's rect). We keep the absolute rect for float
classification (a later slice) and derive each flow child's FRACTION of its parent's main axis for
the solver.

**Fixed-size zones are special and were the hidden gotcha.** A flow child with ``is-fixed='true'`` +
``fixed-size='<px>'`` persists a *nominal* rect whose stored w/h does NOT reflect the resolved flow
layout, so two fixed-size (or fixed x flexible) siblings can legitimately OVERLAP in raw source
coordinates while the resolved layout is disjoint. That is exactly why the legacy ``_scale_zone``
path -- which scales the stored rect directly -- generates the ATTI header-band overlap it then
cannot repair. The premise check here therefore ignores any pair where either side is fixed-size or
floating; a raw-source overlap among two *flexible* flow siblings would be a genuine premise
violation and fails the tree closed.

Fail-closed: if a dashboard's zones do not form a recognizable, overlap-free flow tree,
``parse_zone_tree`` returns ``None`` and the caller keeps the legacy absolute-rect path. Never raises.

This module has NO emit dependency, NO PBIR knowledge, and does not import ``twb_to_pbir``; it is a
pure parser so the solver (a later slice) can consume it and the premise can be asserted in isolation.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

# -- node kinds ------------------------------------------------------------------
K_VSTACK = "vstack"   # type-v2='layout-flow' param='vert'
K_HSTACK = "hstack"   # type-v2='layout-flow' param='horz'
K_FRAME = "frame"     # type-v2='layout-basic' (root, or an absolute/float host)
K_LEAF = "leaf"       # worksheet / text / filter / paramctrl / legend / bitmap / blank

# leaf-kind taxonomy, mapped from a leaf zone's ``type-v2`` (falling back to ``type``)
_LEAF_KIND = {
    "text": "text",
    "filter": "filter",
    "paramctrl": "paramctrl",
    "color": "legend",
    "bitmap": "bitmap",
    "dashboard-object": "bitmap",   # export / filter-toggle / image host -> treated as a bitmap decoration
    "empty": "blank",
}

_MAX_DEPTH = 32          # malformed-input recursion guard
_TOL = 1.0               # linear-overlap tolerance in 100000-space units (touching != overlapping)


# -- XML helpers (namespace-agnostic; duplicated locally to keep this module import-free) --------
def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children_local(elem, name):
    return [c for c in list(elem) if _local(c.tag) == name]


def _first_local(elem, name):
    for c in elem.iter():
        if _local(c.tag) == name:
            return c
    return None


def _znum(zone, attr):
    try:
        return float(zone.get(attr))
    except (TypeError, ValueError):
        return None


def _rect(zone):
    return {"x": _znum(zone, "x"), "y": _znum(zone, "y"),
            "w": _znum(zone, "w"), "h": _znum(zone, "h")}


def _rect_ok(r):
    return None not in (r["x"], r["y"], r["w"], r["h"]) and r["w"] > 0 and r["h"] > 0


def _overlap_lin(a, b):
    """Linear overlap on each axis for two rect dicts; both > 0 means the rects intersect."""
    ox = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    oy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    return ox, oy


def _is_floating(zone):
    return zone.get("floating") in ("true", "1")


def _fixed_px(zone):
    """A flow child's fixed pixel size, or ``None``.  ``is-fixed='true'`` + ``fixed-size='<n>'``."""
    if zone.get("is-fixed") != "true":
        return None
    v = _znum(zone, "fixed-size")
    return v if v is not None and v > 0 else None


def _classify_leaf(zone):
    tv = zone.get("type-v2") or zone.get("type")
    if tv in _LEAF_KIND:
        return _LEAF_KIND[tv]
    # a childless zone with no recognized decoration type but a name is a worksheet placement
    if zone.get("name"):
        return "worksheet"
    return "blank"


def _axis_disjoint(rects, lo, sz):
    """True if every pair of rects is disjoint on the axis given by keys (lo, sz), e.g. ('x','w')."""
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            a, b = rects[i], rects[j]
            if min(a[lo] + a[sz], b[lo] + b[sz]) - max(a[lo], b[lo]) > _TOL:
                return False
    return True


def _infer_kind(child_rects):
    """Fallback container-axis inference for a zone with children but no recognized ``type-v2``."""
    usable = [r for r in child_rects if _rect_ok(r)]
    if len(usable) < 2:
        return K_FRAME
    xs_disjoint = _axis_disjoint(usable, "x", "w")
    ys_disjoint = _axis_disjoint(usable, "y", "h")
    if ys_disjoint and not xs_disjoint:
        return K_VSTACK
    if xs_disjoint and not ys_disjoint:
        return K_HSTACK
    return K_FRAME


def _container_kind(zone, child_rects):
    tv = zone.get("type-v2")
    param = zone.get("param")
    if tv == "layout-flow":
        if param == "vert":
            return K_VSTACK
        if param == "horz":
            return K_HSTACK
        return _infer_kind(child_rects)   # layout-flow with an odd param -> infer
    if tv == "layout-basic":
        return K_FRAME
    return _infer_kind(child_rects)       # unrecognized container -> infer from geometry


def _mk_node(zone, kind, leaf_kind=None):
    r = _rect(zone)
    return {
        "kind": kind,
        "zone_id": zone.get("id"),
        "leaf_kind": leaf_kind,
        "name": zone.get("name") if leaf_kind == "worksheet" else None,
        "src": r,
        "frac": 0.0,
        "fixed_px": _fixed_px(zone),
        "floating": _is_floating(zone),
        "hidden": zone.get("hidden-by-user") == "true",
        "children": [],
        "zone_el": zone,
    }


def _flow_overlaps(kind, kids):
    """Genuine premise-violating overlaps among a flow container's FLEXIBLE tiled children.

    Returns a list of ``(a_node, b_node, ox, oy)``.  A pair is skipped when either child is
    fixed-size or floating (a fixed-size zone's persisted rect is nominal, not the resolved layout,
    so it may legitimately overlap -- see the module docstring).  Only VSTACK/HSTACK are checked;
    a FRAME positions children absolutely and overlap there is allowed by design.
    """
    if kind not in (K_VSTACK, K_HSTACK):
        return []
    tiled = [c for c in kids
             if c["fixed_px"] is None and not c["floating"] and _rect_ok(c["src"])]
    out = []
    for i in range(len(tiled)):
        for j in range(i + 1, len(tiled)):
            ox, oy = _overlap_lin(tiled[i]["src"], tiled[j]["src"])
            if ox > _TOL and oy > _TOL:
                out.append((tiled[i], tiled[j], ox, oy))
    return out


def _compute_fracs(kind, node):
    """Each flow child's share of the parent's main axis; returns False if the axis is zero-sized."""
    if kind not in (K_VSTACK, K_HSTACK):
        return True
    pr = node["src"]
    main = pr["h"] if kind == K_VSTACK else pr["w"]
    if not main or main <= 0:
        return False
    key = "h" if kind == K_VSTACK else "w"
    for c in node["children"]:
        cv = c["src"].get(key)
        c["frac"] = max(0.0, min(1.0, (cv / main) if cv is not None else 0.0))
    return True


def _extent(roots, device_zones):
    ew = eh = 0.0
    seen = roots[0].iter() if len(roots) == 1 else _iter_many(roots)
    for z in seen:
        if _local(z.tag) != "zone" or z in device_zones:
            continue
        r = _rect(z)
        if _rect_ok(r):
            ew = max(ew, r["x"] + r["w"])
            eh = max(eh, r["y"] + r["h"])
    return {"w": ew, "h": eh}


def _iter_many(roots):
    for r in roots:
        for z in r.iter():
            yield z


def _build(zone, depth, device_zones, floats, diagnostics, state):
    if depth > _MAX_DEPTH:
        diagnostics.append("recursion depth > %d at zone id=%s" % (_MAX_DEPTH, zone.get("id")))
        state["ok"] = False
        return None
    child_els = [c for c in _children_local(zone, "zone") if c not in device_zones]
    if not child_els:
        return _mk_node(zone, K_LEAF, _classify_leaf(zone))

    child_rects = [_rect(c) for c in child_els]
    kind = _container_kind(zone, child_rects)
    node = _mk_node(zone, kind)

    for ce in child_els:
        cnode = _build(ce, depth + 1, device_zones, floats, diagnostics, state)
        if cnode is None:
            continue
        if cnode["floating"]:
            floats.append(cnode)          # hoist floats out of the flow entirely
        else:
            node["children"].append(cnode)

    if not _compute_fracs(kind, node):
        diagnostics.append("flow container id=%s has a zero-sized main axis" % zone.get("id"))
        state["ok"] = False

    for a, b, ox, oy in _flow_overlaps(kind, node["children"]):
        diagnostics.append(
            "tiled %s siblings overlap in source: id=%s x id=%s (%dx%d) -- premise violated"
            % (kind, a["zone_id"], b["zone_id"], int(ox), int(oy)))
        state["ok"] = False

    return node


def _roots_of(db, device_zones):
    # The primary layout is the dashboard's DIRECT-child <zones>; a <devicelayouts> block nests its
    # own <zones> deeper, so prefer the direct child and never mistake a phone/tablet layout for root.
    direct = _children_local(db, "zones")
    zones_el = direct[0] if direct else None
    if zones_el is None:
        for c in db.iter():
            if _local(c.tag) == "zones":
                zones_el = c
                break
    if zones_el is None:
        return None
    return [z for z in _children_local(zones_el, "zone") if z not in device_zones]


def parse_zone_tree(db, device_zones=None):
    """Parse a ``<dashboard>`` Element into an overlap-free layout tree, or ``None`` (fail-closed).

    ``device_zones`` is the set of ``<devicelayouts>`` zone Elements the caller already excludes
    (phone/tablet alternates); pass the same set ``_parse_dashboard`` builds so the primary layout
    is the only one parsed.

    Returns ``{"root": node, "floats": [node, ...], "extent": {"w":.., "h":..},
    "diagnostics": [...]}`` on success, or ``None`` when the tree is unusable (no ``<zones>``, an
    empty root, a zero-sized flow axis, a genuine flexible-sibling source overlap, or malformed
    depth).  Never raises.
    """
    device_zones = device_zones or set()
    diagnostics = []
    try:
        roots = _roots_of(db, device_zones)
        if roots is None:
            diagnostics.append("no <zones> element")
            return None
        if not roots:
            diagnostics.append("root <zones> has no zone children")
            return None

        floats = []
        state = {"ok": True}
        if len(roots) == 1:
            root = _build(roots[0], 0, device_zones, floats, diagnostics, state)
        else:
            # rare: multiple top-level zones -> wrap them in a synthetic absolute frame
            root = {
                "kind": K_FRAME, "zone_id": None, "leaf_kind": None, "name": None,
                "src": _extent(roots, device_zones) | {"x": 0.0, "y": 0.0},
                "frac": 0.0, "fixed_px": None, "floating": False, "hidden": False,
                "children": [], "zone_el": None,
            }
            for r in roots:
                cn = _build(r, 0, device_zones, floats, diagnostics, state)
                if cn is None:
                    continue
                (floats if cn["floating"] else root["children"]).append(cn)

        if root is None or not state["ok"]:
            return None
        return {"root": root, "floats": floats,
                "extent": _extent(roots, device_zones), "diagnostics": diagnostics}
    except Exception as exc:   # fail-closed: never raise into the caller
        diagnostics.append("unexpected error: %r" % (exc,))
        return None


def audit_source_overlaps(db, device_zones=None):
    """Pure diagnostic (does NOT fail-close): report tiled flow-sibling overlaps in SOURCE coords.

    This is the premise instrument (spec 6.2).  It walks the tree WITHOUT the fail-closed contract
    so a test / A-B harness can see the offending zones and distinguish the two cases:

    * ``overlaps`` -- genuine flexible-sibling overlaps (a premise violation; expected to be empty),
    * ``fixed_artifacts`` -- pairs that overlap only because a fixed-size zone persists a nominal
      rect (benign; the resolved layout is disjoint).

    Returns ``{"flow_containers", "checked_pairs", "overlaps": [...], "fixed_artifacts": [...]}``.
    Each entry is ``{"kind", "parent_id", "a", "b", "overlap": (ox, oy)}``.  Never raises.
    """
    device_zones = device_zones or set()
    res = {"flow_containers": 0, "checked_pairs": 0, "overlaps": [], "fixed_artifacts": []}
    try:
        roots = _roots_of(db, device_zones)
        if not roots:
            return res

        def walk(zone, depth):
            if depth > _MAX_DEPTH:
                return
            child_els = [c for c in _children_local(zone, "zone") if c not in device_zones]
            if child_els:
                child_rects = [_rect(c) for c in child_els]
                kind = _container_kind(zone, child_rects)
                if kind in (K_VSTACK, K_HSTACK):
                    res["flow_containers"] += 1
                    kids = [(c, _rect(c)) for c in child_els if not _is_floating(c)]
                    for i in range(len(kids)):
                        for j in range(i + 1, len(kids)):
                            (ci, ri), (cj, rj) = kids[i], kids[j]
                            if not (_rect_ok(ri) and _rect_ok(rj)):
                                continue
                            res["checked_pairs"] += 1
                            ox, oy = _overlap_lin(ri, rj)
                            if ox > _TOL and oy > _TOL:
                                entry = {"kind": kind, "parent_id": zone.get("id"),
                                         "a": ci.get("id"), "b": cj.get("id"),
                                         "overlap": (ox, oy)}
                                if _fixed_px(ci) is not None or _fixed_px(cj) is not None:
                                    res["fixed_artifacts"].append(entry)
                                else:
                                    res["overlaps"].append(entry)
                for ce in child_els:
                    walk(ce, depth + 1)

        for r in roots:
            walk(r, 0)
    except Exception:
        return res
    return res
