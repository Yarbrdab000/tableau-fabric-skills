"""Advisory **fidelity oracle** -- score an emitted Power BI **PBIR** report against its Tableau
``.twb`` source to help *prove* a faithful (toward pixel-perfect) rebuild.

This is **verification infrastructure, not the migration engine**. The deterministic engine
(``twb_to_pbir`` and friends) stands alone and owns correctness; this module is a *second,
independent opinion* that re-reads BOTH sides from disk and grades their agreement. It never
imports the engine's parse path and never round-trips the engine against itself -- it parses the
Tableau workbook XML and the emitted PBIR JSON with its OWN readers, then pairs and scores. That
independence is the whole point: a bug shared by the engine and a round-trip check would hide in
both, but it cannot hide from a separately authored reader.

Everything here is **advisory and tolerance-banded**. Cross-engine equality is not a binary; the
report hands back a 0..1 agreement score, a per-visual diff (match / mismatch / missing / extra),
and a named tolerance *band* -- never a hard pass/fail. The structural tier below is deterministic
and stdlib-only, so it runs offline with no Power BI Desktop. The optional value tier (live model
measure values via a local Analysis Services instance) and image tier (perceptual similarity) are
separate, lazily-imported add-ons that degrade gracefully to ``unavailable`` when their hosts or
optional packages are absent -- importing this module never fails offline.

Scoring model (structural tier), per paired visual, each component in ``[0, 1]``:

* **type** -- chart-type *family* agreement (exact / related / mismatch). The Tableau side is a
  second-opinion classifier from the mark class + shelf shape, deliberately conservative; an
  ``Automatic`` mark that the source does not strongly assert is given benefit of the doubt rather
  than punished.
* **fields** -- Jaccard overlap of the normalized *source field* sets (binding fidelity: did the
  rebuilt visual bind the same underlying columns/measures?). This is the strongest, most engine-
  independent signal.
* **roles** -- agreement of the dimension-set and measure-set split (did a field silently flip
  between an axis/group role and an aggregated value role?).
* **position** -- normalized-rectangle overlap for dashboard-placed visuals (Tableau zones are
  normalized by the dashboard extent, PBIR visuals by the page size), inside a tolerance band.
  Self-service / non-dashboard pages drop this component and the weights renormalize.

The Tableau workbook XML grammar and the Microsoft PBIR report-definition JSON shapes are public
interoperability facts; the readers, the pairing, and the scoring here are original work authored
against our own corpus and the calibration outputs, kept quarantined from the engine's test gate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import xml.etree.ElementTree as ET

ORACLE_VERSION = 2
ORACLE_KIND = "tableau-fabric-structural-fidelity"

# -- scoring weights (structural tier) -----------------------------------------
# Field-binding agreement dominates: it is the least engine-coupled and the most consequential
# ("are the same underlying columns on the visual?"). Type and role split are secondary checks;
# position is a light tie-breaking / layout-faithfulness signal that only applies on a dashboard.
W_TYPE = 0.30
W_FIELDS = 0.40
W_ROLES = 0.20
W_POSITION = 0.10

# Partial credit for a *related* (faithful-ish but not identical) chart family -- e.g. an area
# chart rebuilt as a line, or a bar promoted to a combo. These are common, defensible cross-engine
# choices, so they score as a strong-but-flagged partial rather than a hard miss.
TYPE_RELATED_CREDIT = 0.60
# When the Tableau mark is ``Automatic`` and the shelf shape does not strongly assert one family,
# we do not punish a plausible rebuild -- but we do not award a full match either.
TYPE_UNASSERTED_CREDIT = 0.85

# Position tolerance: a normalized-rectangle IoU at/above this counts as a full positional match;
# below it the credit tapers linearly to zero. Cross-engine layout rounding lives well inside this.
POSITION_FULL_IOU = 0.80
POSITION_ZERO_IOU = 0.20

# Advisory band thresholds on the aggregate 0..1 score. Named bands, never pass/fail.
BANDS = (
    (0.95, "faithful"),       # indistinguishable within cross-engine noise
    (0.85, "strong"),         # minor, explainable divergence
    (0.60, "review"),         # advisory: a human should eyeball it
    (0.0, "divergent"),       # materially different -- likely a real rebuild gap
)


# -- chart-type families -------------------------------------------------------
# A small, coarse family enum. Bar and column collapse into one family (orientation is a sub-detail
# the oracle reports but does not penalize); table vs matrix and pie vs donut are kept distinct but
# treated as "related".
FAM_BAR = "bar"
FAM_LINE = "line"
FAM_AREA = "area"
FAM_PIE = "pie"
FAM_DONUT = "donut"
FAM_SCATTER = "scatter"
FAM_MAP = "map"
FAM_TABLE = "table"
FAM_MATRIX = "matrix"
FAM_CARD = "card"
FAM_COMBO = "combo"
FAM_WATERFALL = "waterfall"
FAM_RIBBON = "ribbon"
FAM_SLICER = "slicer"
FAM_UNKNOWN = "unknown"

# Emitted PBIR ``visualType`` -> oracle family. Authored from the Microsoft report-definition
# visual catalog (public schema names), independent of the engine's own emit table.
_PBIR_FAMILY = {
    "clusteredColumnChart": FAM_BAR, "columnChart": FAM_BAR, "stackedColumnChart": FAM_BAR,
    "barChart": FAM_BAR, "clusteredBarChart": FAM_BAR, "stackedBarChart": FAM_BAR,
    "hundredPercentStackedColumnChart": FAM_BAR, "hundredPercentStackedBarChart": FAM_BAR,
    "lineChart": FAM_LINE, "lineStackedColumnComboChart": FAM_COMBO,
    "lineClusteredColumnComboChart": FAM_COMBO,
    "areaChart": FAM_AREA, "stackedAreaChart": FAM_AREA,
    "pieChart": FAM_PIE, "donutChart": FAM_DONUT,
    "scatterChart": FAM_SCATTER,
    "map": FAM_MAP, "filledMap": FAM_MAP, "shapeMap": FAM_MAP, "azureMap": FAM_MAP,
    "tableEx": FAM_TABLE, "table": FAM_TABLE, "pivotTable": FAM_MATRIX, "matrix": FAM_MATRIX,
    "card": FAM_CARD, "multiRowCard": FAM_CARD, "cardVisual": FAM_CARD, "kpi": FAM_CARD,
    "waterfallChart": FAM_WATERFALL, "ribbonChart": FAM_RIBBON,
    "slicer": FAM_SLICER, "advancedSlicerVisual": FAM_SLICER,
}

# Families that count as "related" (partial credit) rather than a clean mismatch. Symmetric.
_RELATED_FAMILIES = (
    frozenset({FAM_AREA, FAM_LINE}),
    frozenset({FAM_BAR, FAM_COMBO}),
    frozenset({FAM_LINE, FAM_COMBO}),
    frozenset({FAM_PIE, FAM_DONUT}),
    frozenset({FAM_TABLE, FAM_MATRIX}),
    frozenset({FAM_CARD, FAM_TABLE}),
    frozenset({FAM_BAR, FAM_RIBBON}),
    frozenset({FAM_BAR, FAM_WATERFALL}),
)

# Tableau mark class -> family, for the marks that assert a family on their own. ``Automatic`` and
# ``Square`` are resolved by shelf shape in ``_infer_twb_family`` instead.
_MARK_FAMILY = {
    "bar": FAM_BAR, "gantt": FAM_BAR, "line": FAM_LINE, "area": FAM_AREA, "pie": FAM_PIE,
    "circle": FAM_SCATTER, "shape": FAM_SCATTER, "text": FAM_TABLE, "polygon": FAM_MAP,
    "multipolygon": FAM_MAP, "map": FAM_MAP,
}

# Tableau aggregation derivation tokens (shelf/column-instance prefixes) that mark a pill as an
# aggregated *measure* rather than a dimension. Date-truncation derivations (``tmn``, ``tdy``,
# ``tyr`` ...) are intentionally absent -- a truncated date is still an axis dimension.
# Both spellings appear: shelf-pill tokens use the short prefix (``usr``), while a
# ``<column-instance derivation=...>`` attribute uses the long word (``User``, ``Average``).
_AGG_DERIVATIONS = {
    "sum", "avg", "average", "min", "max", "median", "count", "cnt", "cntd", "countd",
    "stdev", "stdevp", "var", "varp", "attr", "usr", "user",
}

# Tableau pseudo-fields with no underlying model column. They are placeholders for the
# Measure Values / Measure Names mechanism; the real members come from the worksheet's
# ``<datasource-dependencies>`` aggregated column-instances.
_SPECIAL_PILLS = {
    "measure names", "measure values", "multiple values", ":measure names", ":measure values",
}

# Generated geo/auto fields Tableau synthesizes (Latitude/Longitude/Geometry/Number of Records).
# They are encodings, not source columns, so they are excluded from the field-binding set.
_GENERATED_RE = re.compile(r"\((generated|copy)\)\s*$", re.IGNORECASE)
_NUMBER_OF_RECORDS = "number of records"
# Tableau's row-identity pseudo-column (``__tableau_internal_object_id__``) backs an implicit
# COUNT(*); it is not an author-facing field, so it never belongs in the field-binding set.
_OBJECT_ID_NORM = "tableauinternalobjectid"


def _norm(name):
    """Normalize a field/display name for cross-engine matching.

    Tableau and Power BI spell the same source column differently (``Order Date`` vs
    ``Order_Date``, ``Country/Region`` vs ``Country_Region``). Collapsing to lowercase alphanumerics
    makes the binding comparison robust to those cosmetic differences without being so loose that
    distinct fields collide.
    """
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _band(score):
    for threshold, label in BANDS:
        if score >= threshold:
            return label
    return BANDS[-1][1]


def _local(tag):
    return tag.split("}")[-1] if isinstance(tag, str) else tag


def _iter_local(elem, name):
    for child in elem.iter():
        if _local(child.tag) == name:
            yield child


def _children(elem, name):
    return [c for c in elem if _local(c.tag) == name]


def _first_child(elem, name):
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


# =====================================================================================
# PBIR reader -- emitted Power BI report on disk
# =====================================================================================
def _pbir_extract_field(node):
    """Pull a normalized field descriptor out of a PBIR projection ``field`` expression.

    Handles the three expression shapes a report projection uses -- a raw ``Column`` (dimension),
    an ``Aggregation`` wrapping a column (an aggregated measure), and a model ``Measure`` -- plus
    any nested variant, by walking to the innermost ``Property`` and its nearest ``Entity``.
    Returns ``{entity, property, is_measure, agg, kind, norm}`` or ``None``.
    """
    if not isinstance(node, dict):
        return None
    is_measure = False
    agg = None
    kind = "column"
    if "Measure" in node:
        kind = "measure"
        is_measure = True
    elif "Aggregation" in node:
        kind = "aggregation"
        is_measure = True
        agg = node["Aggregation"].get("Function")

    prop = _find_key(node, "Property")
    entity = _find_key(node, "Entity")
    if prop is None:
        return None
    norm = _norm(prop)
    # Exclude implicit/non-author fields symmetrically with the Tableau side, so an emitted
    # row-count or generated-geo column never shows up as a spurious ``fields_extra``.
    if (_OBJECT_ID_NORM in norm or norm == _norm(_NUMBER_OF_RECORDS)
            or _GENERATED_RE.search(prop or "")):
        return None
    return {
        "entity": entity,
        "property": prop,
        "is_measure": is_measure,
        "agg": agg,
        "kind": kind,
        "norm": norm,
    }


def _find_key(node, key):
    """Depth-first search for the first value of ``key`` anywhere inside a nested dict/list."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur and not isinstance(cur[key], (dict, list)):
                return cur[key]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _read_json(path):
    """Read a JSON file defensively. Returns the parsed value, or ``None`` when the file is
    missing or malformed -- the advisory oracle must never raise on real-world inputs."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _pbir_read_visual(path):
    """Read one PBIR ``visual.json`` into a normalized visual record, or ``None`` if unreadable."""
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    visual = data.get("visual", {}) or {}
    vtype = visual.get("visualType")
    pos = data.get("position", {}) or {}
    position = {
        "x": _f(pos.get("x")), "y": _f(pos.get("y")),
        "w": _f(pos.get("width")), "h": _f(pos.get("height")),
        "z": _f(pos.get("z")),
    }

    roles = {}
    fields = []
    qstate = (((visual.get("query") or {}).get("queryState")) or {})
    for role_key, role_block in qstate.items():
        projections = (role_block or {}).get("projections", []) or []
        bucket = []
        for proj in projections:
            fld = _pbir_extract_field(proj.get("field"))
            if fld is None:
                continue
            fld = dict(fld, role=role_key,
                       display=proj.get("nativeQueryRef") or proj.get("queryRef"))
            bucket.append(fld)
            fields.append(fld)
        if bucket:
            roles[role_key] = bucket

    # Slicer selection fields come from a sibling filterConfig, not the query projections.
    filt_fields = []
    for filt in ((data.get("filterConfig") or {}).get("filters") or []):
        fld = _pbir_extract_field(filt.get("field"))
        if fld is not None:
            filt_fields.append(fld)

    family = _PBIR_FAMILY.get(vtype, FAM_UNKNOWN)
    return {
        "name": data.get("name") or os.path.basename(os.path.dirname(path)),
        "visual_type": vtype,
        "family": family,
        "is_slicer": family == FAM_SLICER,
        "position": position,
        "roles": roles,
        "fields": fields,
        "filter_fields": filt_fields,
    }


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_pbir_report(report_dir):
    """Read an emitted ``*.Report`` PBIR folder into ``{report_name, pages: [...]}``.

    Accepts either the ``*.Report`` directory itself or a parent containing exactly one. Each page
    carries its declared pixel size (defaulting to 1280x720) so visual positions can be normalized.
    """
    report_dir = _resolve_report_dir(report_dir)
    if report_dir is None:
        return {"report_name": None, "pages": [], "warnings": ["no .Report folder found"]}

    pages_dir = os.path.join(report_dir, "definition", "pages")
    pages = []
    warnings = []
    if not os.path.isdir(pages_dir):
        warnings.append("no definition/pages directory")
    else:
        order = _page_order(pages_dir)
        page_names = order or sorted(
            d for d in os.listdir(pages_dir)
            if os.path.isdir(os.path.join(pages_dir, d)))
        for pname in page_names:
            pdir = os.path.join(pages_dir, pname)
            page_json = os.path.join(pdir, "page.json")
            display = pname
            width, height = 1280.0, 720.0
            if os.path.isfile(page_json):
                pj = _read_json(page_json)
                if isinstance(pj, dict):
                    display = pj.get("displayName") or pname
                    width = _f(pj.get("width")) or width
                    height = _f(pj.get("height")) or height
            visuals = []
            vis_dir = os.path.join(pdir, "visuals")
            if os.path.isdir(vis_dir):
                for vname in sorted(os.listdir(vis_dir)):
                    vjson = os.path.join(vis_dir, vname, "visual.json")
                    if os.path.isfile(vjson):
                        try:
                            rec = _pbir_read_visual(vjson)
                        except (ValueError, OSError) as exc:
                            warnings.append("unreadable visual %s: %s" % (vname, exc))
                            continue
                        if rec is None:
                            warnings.append("unreadable visual %s: malformed JSON" % vname)
                        else:
                            visuals.append(rec)
            for v in visuals:
                _attach_normalized_position(v, width, height)
            pages.append({
                "name": pname, "display": display,
                "width": width, "height": height, "visuals": visuals,
            })
    return {"report_name": os.path.basename(report_dir), "pages": pages, "warnings": warnings}


def _attach_normalized_position(visual, width, height):
    p = visual["position"]
    if None in (p["x"], p["y"], p["w"], p["h"]) or not width or not height:
        visual["nposition"] = None
        return
    visual["nposition"] = {
        "x": p["x"] / width, "y": p["y"] / height,
        "w": p["w"] / width, "h": p["h"] / height,
    }


def _page_order(pages_dir):
    pj = os.path.join(pages_dir, "pages.json")
    if os.path.isfile(pj):
        data = _read_json(pj)
        order = data.get("pageOrder") if isinstance(data, dict) else None
        if isinstance(order, list):
            return [p for p in order if os.path.isdir(os.path.join(pages_dir, p))]
    return None


def _resolve_report_dir(path):
    if path and os.path.isdir(path):
        if os.path.isdir(os.path.join(path, "definition", "pages")):
            return path
        candidates = [d for d in os.listdir(path) if d.endswith(".Report")
                      and os.path.isdir(os.path.join(path, d))]
        if len(candidates) == 1:
            return os.path.join(path, candidates[0])
        # nested reports/ folder (estate layout)
        reports = os.path.join(path, "reports")
        if os.path.isdir(reports):
            inner = [d for d in os.listdir(reports) if d.endswith(".Report")]
            if len(inner) == 1:
                return os.path.join(reports, inner[0])
    return None


# =====================================================================================
# Tableau .twb reader -- independent viz-grammar parse
# =====================================================================================
def _strip_brackets(token):
    token = (token or "").strip()
    if token.startswith("[") and token.endswith("]"):
        return token[1:-1]
    return token


def _build_caption_index(root):
    """Map a datasource column's internal name -> its author-facing caption.

    Lets a calc pill referenced as ``[Calculation_1368...]`` resolve to its display caption
    (``Profit Ratio``) so it matches the PBIR measure name. Plain columns are their own caption.
    """
    index = {}
    for col in _iter_local(root, "column"):
        name = _strip_brackets(col.get("name"))
        caption = col.get("caption")
        if name and caption:
            index.setdefault(name, caption)
    return index


# A shelf pill token: ``[datasource].[derivation:RemoteName:typekey]`` or ``[ds].[Generated Field]``.
_PILL_RE = re.compile(r"\[(?P<ds>[^\]]+)\]\.\[(?P<inner>[^\]]+)\]")


def _parse_pill(inner, caption_index):
    """Parse a pill's inner token into a field descriptor, or ``None`` for a special/generated pill.

    ``inner`` is the part inside the second bracket pair: ``sum:Sales:qk``, ``none:Sub-Category:nk``,
    ``tmn:Order Date:qk``, ``:Measure Names``, ``Latitude (generated)``, ``usr:Calculation_x:qk``.
    """
    raw = inner.strip()
    low = raw.lower()
    if low in _SPECIAL_PILLS or low.lstrip(":") in _SPECIAL_PILLS:
        return None
    if _GENERATED_RE.search(raw) or low == _NUMBER_OF_RECORDS:
        return None

    deriv = None
    name = raw
    # ``deriv:Name:typekey`` -- split on the FIRST and LAST colon (names can contain neither here,
    # but guard by taking head/tail around the middle).
    parts = raw.split(":")
    if len(parts) >= 3:
        deriv = parts[0].strip().lower()
        name = ":".join(parts[1:-1]).strip()
    elif len(parts) == 2 and parts[0] == "":
        # leading-colon special already handled above; any other ``:X`` -> treat X as name
        name = parts[1].strip()

    name = _strip_brackets(name)
    caption = caption_index.get(name, name)
    norm = _norm(caption)
    # Re-apply the implicit/generated exclusions on the *resolved* name: a wrapped pill such as
    # ``none:Number of Records:qk`` or ``none:Latitude (generated):qk`` passes the raw-token guard
    # above (it ends in ``:qk``) but must still be dropped from the field-binding set.
    if _OBJECT_ID_NORM in norm or norm == _norm(_NUMBER_OF_RECORDS):
        return None
    if _GENERATED_RE.search(name) or _GENERATED_RE.search(caption or ""):
        return None
    is_measure = bool(deriv) and deriv in _AGG_DERIVATIONS
    return {
        "property": caption,
        "deriv": deriv,
        "is_measure": is_measure,
        "norm": norm,
    }


def _pills_from_text(text, caption_index):
    """Extract every field pill from a shelf string (rows/cols), skipping specials/generated."""
    out = []
    for m in _PILL_RE.finditer(text or ""):
        fld = _parse_pill(m.group("inner"), caption_index)
        if fld is not None:
            out.append(fld)
    return out


def _measure_value_members(view, caption_index):
    """Resolve the Measure Values member set from a worksheet's aggregated column-instances.

    A text/card worksheet driven by Measure Values names no fields on its shelves -- the members
    live in ``<datasource-dependencies>`` as aggregated ``<column-instance>`` rows. This recovers
    them (resolving calc captions) so the field-binding comparison sees the real member fields.
    """
    members = []
    seen = set()
    for ci in _iter_local(view, "column-instance"):
        deriv = (ci.get("derivation") or "").lower()
        if deriv not in _AGG_DERIVATIONS:
            continue
        col = _strip_brackets(ci.get("column"))
        caption = caption_index.get(col, col)
        n = _norm(caption)
        if n and n not in seen and _OBJECT_ID_NORM not in n:
            seen.add(n)
            members.append({"property": caption, "deriv": deriv,
                            "is_measure": True, "norm": n})
    return members


def _infer_twb_family(mark, dims, measures, has_geometry, uses_measure_values):
    """Second-opinion chart-family classifier for a Tableau worksheet.

    Returns ``(family, asserted)``. ``asserted`` is False when the mark is ``Automatic`` and the
    shelf shape does not strongly imply a family -- the scorer then declines to punish a plausible
    rebuild rather than guessing aggressively.
    """
    mlow = (mark or "").lower()
    if has_geometry:
        return FAM_MAP, True
    if mlow in _MARK_FAMILY and mlow != "automatic":
        fam = _MARK_FAMILY[mlow]
        # A Text mark with no dimensions is a card, not a table.
        if fam == FAM_TABLE and not dims and not uses_measure_values:
            return FAM_CARD, True
        if fam == FAM_TABLE and uses_measure_values and not dims:
            return FAM_CARD, True
        return fam, True
    # Automatic: infer from shelf shape (Tableau's own default heuristic, applied conservatively).
    if uses_measure_values and not dims:
        return FAM_CARD, True
    if not dims and measures:
        return FAM_CARD, True
    if dims and measures:
        return FAM_BAR, False   # plausible but not asserted by the source
    if dims and not measures:
        return FAM_TABLE, False
    return FAM_UNKNOWN, False


def _has_measure_values_encoding(panes):
    """True when any pane encoding (e.g. a text mark) references the Measure Values placeholder."""
    if panes is None:
        return False
    for pane in _children(panes, "pane"):
        enc = _first_child(pane, "encodings")
        if enc is None:
            continue
        for e in enc:
            col = (e.get("column") or "").lower()
            if (":measure names" in col or "measure values" in col
                    or "multiple values" in col):
                return True
    return False


def _worksheet_record(ws, caption_index):
    name = ws.get("name")
    table = _first_child(ws, "table")
    if table is None:
        return None
    view = _first_child(table, "view") or table

    rows_el = _first_child(table, "rows")
    cols_el = _first_child(table, "cols")
    rows_text = (rows_el.text if rows_el is not None else "") or ""
    cols_text = (cols_el.text if cols_el is not None else "") or ""

    panes = _first_child(table, "panes")
    mark = "Automatic"
    encoding_fields = []
    has_geometry = False
    if panes is not None:
        pane = _first_child(panes, "pane")
        if pane is not None:
            mark_el = _first_child(pane, "mark")
            if mark_el is not None and mark_el.get("class"):
                mark = mark_el.get("class")
            enc = _first_child(pane, "encodings")
            if enc is not None:
                for e in enc:
                    if _local(e.tag) == "geometry":
                        has_geometry = True
                        continue
                    col = e.get("column")
                    for m in _PILL_RE.finditer(col or ""):
                        fld = _parse_pill(m.group("inner"), caption_index)
                        if fld is not None:
                            encoding_fields.append(dict(fld, channel=_local(e.tag)))

    shelf_text = (rows_text + " " + cols_text).lower()
    uses_measure_values = (":measure names" in shelf_text
                           or "measure values" in shelf_text
                           or "multiple values" in shelf_text)

    row_pills = _pills_from_text(rows_text, caption_index)
    col_pills = _pills_from_text(cols_text, caption_index)

    # A text/card worksheet may name its measures only via Measure Values (a placeholder pill in a
    # text encoding), with the real members living in <datasource-dependencies>. Detect that and
    # recover the members so the field-binding set is complete.
    placeholder_text_encoding = uses_measure_values or _has_measure_values_encoding(panes)
    members = []
    if placeholder_text_encoding:
        members = _measure_value_members(view, caption_index)
        uses_measure_values = uses_measure_values or bool(members)

    # Assemble the field set: shelf pills + encoding pills + measure-values members.
    all_fields = []
    seen = set()
    for fld in row_pills + col_pills + encoding_fields + members:
        key = (fld["norm"], fld.get("is_measure"))
        if fld["norm"] and key not in seen:
            seen.add(key)
            all_fields.append(fld)

    dims = [f for f in all_fields if not f["is_measure"]]
    measures = [f for f in all_fields if f["is_measure"]]

    family, asserted = _infer_twb_family(
        mark, dims, measures, has_geometry, uses_measure_values or bool(members))

    filters = []
    for fl in _iter_local(view, "filter"):
        if (fl.get("class") or "").lower() != "categorical":
            continue
        col = fl.get("column") or ""
        m = _PILL_RE.search(col)
        if m:
            fld = _parse_pill(m.group("inner"), caption_index)
            if fld is not None:
                filters.append(fld)

    return {
        "name": name,
        "mark": mark,
        "family": family,
        "family_asserted": asserted,
        "has_geometry": has_geometry,
        "uses_measure_values": uses_measure_values or bool(members),
        "fields": all_fields,
        "dims": dims,
        "measures": measures,
        "filters": filters,
    }


def _zone_f(zone, attr):
    try:
        return float(zone.get(attr))
    except (TypeError, ValueError):
        return None


def _dashboard_record(db, worksheet_names):
    name = db.get("name")
    # Device (phone/tablet) layouts duplicate the same zones; exclude them.
    device_zones = set()
    for holder in _iter_local(db, "devicelayouts"):
        for z in _iter_local(holder, "zone"):
            device_zones.add(z)

    zones = []
    ext_w = ext_h = 0.0
    for zone in _iter_local(db, "zone"):
        if zone in device_zones:
            continue
        x, y = _zone_f(zone, "x"), _zone_f(zone, "y")
        w, h = _zone_f(zone, "w"), _zone_f(zone, "h")
        if None not in (x, y, w, h) and w > 0 and h > 0:
            ext_w = max(ext_w, x + w)
            ext_h = max(ext_h, y + h)
        zname = zone.get("name")
        if not zname or zname not in worksheet_names:
            continue
        if zone.get("type-v2") or zone.get("type"):
            continue
        if None in (x, y, w, h) or w <= 0 or h <= 0:
            continue
        zones.append({"worksheet": zname, "x": x, "y": y, "w": w, "h": h})

    for z in zones:
        if ext_w and ext_h:
            z["nposition"] = {"x": z["x"] / ext_w, "y": z["y"] / ext_h,
                              "w": z["w"] / ext_w, "h": z["h"] / ext_h}
        else:
            z["nposition"] = None
    return {"name": name, "extent": {"w": ext_w or None, "h": ext_h or None}, "zones": zones}


def read_twb_views(twb_path_or_text):
    """Parse a Tableau ``.twb`` (path or raw XML) into independent worksheet + dashboard records.

    Defensive by contract: a missing file or malformed XML yields an empty parse with a warning
    rather than raising, so the advisory oracle never crashes on a bad input.
    """
    warnings = []
    try:
        is_path = os.path.exists(twb_path_or_text)
    except (TypeError, ValueError):
        is_path = False
    if is_path:
        try:
            with open(twb_path_or_text, "r", encoding="utf-8-sig") as fh:
                xml_text = fh.read()
        except OSError as exc:
            return {"worksheets": {}, "dashboards": [], "caption_index": {},
                    "warnings": ["unreadable .twb: %s" % exc]}
    else:
        xml_text = twb_path_or_text or ""

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {"worksheets": {}, "dashboards": [], "caption_index": {},
                "warnings": ["malformed .twb XML: %s" % exc]}
    caption_index = _build_caption_index(root)

    worksheets = {}
    for ws in _iter_local(root, "worksheet"):
        rec = _worksheet_record(ws, caption_index)
        if rec is not None:
            worksheets[rec["name"]] = rec

    dashboards = []
    for db in _iter_local(root, "dashboard"):
        dashboards.append(_dashboard_record(db, set(worksheets.keys())))

    return {"worksheets": worksheets, "dashboards": dashboards,
            "caption_index": caption_index, "warnings": warnings}


# =====================================================================================
# Scoring
# =====================================================================================
def _jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _field_norms(fields):
    return {f["norm"] for f in fields if f.get("norm")}


def _type_score(twb_ws, pbir_visual):
    src = twb_ws["family"]
    tgt = pbir_visual["family"]
    if src == FAM_UNKNOWN or tgt == FAM_UNKNOWN:
        return TYPE_UNASSERTED_CREDIT, "type-indeterminate"
    if src == tgt:
        return 1.0, "type-match"
    if frozenset({src, tgt}) in _RELATED_FAMILIES:
        return TYPE_RELATED_CREDIT, "type-related (%s~%s)" % (src, tgt)
    if not twb_ws.get("family_asserted", True):
        return TYPE_UNASSERTED_CREDIT, "type-unasserted (%s?/%s)" % (src, tgt)
    return 0.0, "type-mismatch (%s vs %s)" % (src, tgt)


def _roles_score(twb_ws, pbir_visual):
    src_dims = _field_norms(twb_ws["dims"])
    src_meas = _field_norms(twb_ws["measures"])
    tgt_dims = _field_norms([f for f in pbir_visual["fields"] if not f["is_measure"]])
    tgt_meas = _field_norms([f for f in pbir_visual["fields"] if f["is_measure"]])
    return (_jaccard(src_dims, tgt_dims) + _jaccard(src_meas, tgt_meas)) / 2.0


def _iou(a, b):
    if not a or not b:
        return None
    ax2, ay2 = a["x"] + a["w"], a["y"] + a["h"]
    bx2, by2 = b["x"] + b["w"], b["y"] + b["h"]
    ix = max(0.0, min(ax2, bx2) - max(a["x"], b["x"]))
    iy = max(0.0, min(ay2, by2) - max(a["y"], b["y"]))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else None


def _position_score(zone, pbir_visual):
    if zone is None or zone.get("nposition") is None or pbir_visual.get("nposition") is None:
        return None
    iou = _iou(zone["nposition"], pbir_visual["nposition"])
    if iou is None:
        return None
    if iou >= POSITION_FULL_IOU:
        return 1.0
    if iou <= POSITION_ZERO_IOU:
        return 0.0
    return (iou - POSITION_ZERO_IOU) / (POSITION_FULL_IOU - POSITION_ZERO_IOU)


def _score_pair(twb_ws, pbir_visual, zone):
    type_s, type_note = _type_score(twb_ws, pbir_visual)
    src_fields = _field_norms(twb_ws["fields"])
    tgt_fields = _field_norms(pbir_visual["fields"])
    field_s = _jaccard(src_fields, tgt_fields)
    roles_s = _roles_score(twb_ws, pbir_visual)
    pos_s = _position_score(zone, pbir_visual)

    weights = {"type": W_TYPE, "fields": W_FIELDS, "roles": W_ROLES}
    parts = {"type": type_s, "fields": field_s, "roles": roles_s}
    if pos_s is not None:
        weights["position"] = W_POSITION
        parts["position"] = pos_s
    total_w = sum(weights.values())
    overall = sum(parts[k] * weights[k] for k in parts) / total_w if total_w else 0.0

    missing = sorted(src_fields - tgt_fields)
    extra = sorted(tgt_fields - src_fields)
    matched = sorted(src_fields & tgt_fields)
    return {
        "worksheet": twb_ws["name"],
        "visual": pbir_visual["name"],
        "visual_type": pbir_visual["visual_type"],
        "source_family": twb_ws["family"],
        "target_family": pbir_visual["family"],
        "components": {k: round(parts[k], 4) for k in parts},
        "score": round(overall, 4),
        "band": _band(overall),
        "type_note": type_note,
        "fields_matched": matched,
        "fields_missing": missing,   # in Tableau source, absent from rebuilt visual
        "fields_extra": extra,       # in rebuilt visual, absent from Tableau source
    }


def _pair_score(twb_ws, pbir_visual, zone):
    """Cheap similarity used only to choose the best Tableau<->PBIR pairing (not the final score)."""
    field_s = _jaccard(_field_norms(twb_ws["fields"]), _field_norms(pbir_visual["fields"]))
    pos_s = _position_score(zone, pbir_visual)
    if pos_s is None:
        return field_s
    return 0.7 * field_s + 0.3 * pos_s


def _greedy_pair(worksheets, visuals, zone_by_ws):
    """Greedily pair each Tableau worksheet to its best unused PBIR visual by content+position."""
    candidates = []
    for ws in worksheets:
        zone = zone_by_ws.get(ws["name"])
        for v in visuals:
            candidates.append((_pair_score(ws, v, zone), ws["name"], v["name"]))
    candidates.sort(key=lambda t: t[0], reverse=True)
    used_ws, used_v, pairs = set(), set(), []
    for sim, wsn, vn in candidates:
        if wsn in used_ws or vn in used_v:
            continue
        used_ws.add(wsn)
        used_v.add(vn)
        pairs.append((wsn, vn, sim))
    return pairs


def score_report(twb, pbir, engine_report=None):
    """Score a parsed Tableau workbook against a parsed PBIR report. Both come from the readers above.

    Pairs each Tableau dashboard to the PBIR page sharing its display name, greedily matches that
    dashboard's worksheets to the page's non-slicer visuals by content + position, and grades each
    pair. Worksheets not placed on any dashboard fall back to a best-effort field-only match against
    any remaining visual. Returns an advisory report dict (never a pass/fail).
    """
    ws_by_name = twb["worksheets"]
    visual_pages = {p["display"]: p for p in pbir["pages"]}
    visual_pages_by_name = {p["name"]: p for p in pbir["pages"]}

    engine_intent = _engine_intent_index(engine_report)

    visual_results = []
    slicer_results = []
    matched_visuals = set()
    placed_worksheets = set()

    for dash in twb["dashboards"]:
        page = visual_pages.get(dash["name"]) or _page_for_dashboard(dash, pbir)
        if page is None:
            continue
        zone_by_ws = {z["worksheet"]: z for z in dash["zones"]}
        dash_ws = [ws_by_name[z["worksheet"]] for z in dash["zones"]
                   if z["worksheet"] in ws_by_name]
        non_slicers = [v for v in page["visuals"] if not v["is_slicer"]]
        pairs = _greedy_pair(dash_ws, non_slicers, zone_by_ws)
        vidx = {v["name"]: v for v in page["visuals"]}
        for wsn, vn, _sim in pairs:
            ws = ws_by_name[wsn]
            v = vidx[vn]
            zone = zone_by_ws.get(wsn)
            res = _score_pair(ws, v, zone)
            res["page"] = page["display"]
            res["dashboard"] = dash["name"]
            res["engine_intent"] = engine_intent.get(wsn)
            visual_results.append(res)
            matched_visuals.add((page["name"], vn))
            placed_worksheets.add(wsn)
        # Slicers on this page -> advisory filter-fidelity records.
        for v in page["visuals"]:
            if not v["is_slicer"]:
                continue
            slicer_results.append(_score_slicer(v, dash_ws, page["display"]))
            matched_visuals.add((page["name"], v["name"]))

    # Worksheets not on any dashboard: best-effort field-only match against leftover visuals.
    leftover_visuals = [
        (p, v) for p in pbir["pages"] for v in p["visuals"]
        if (p["name"], v["name"]) not in matched_visuals and not v["is_slicer"]
    ]
    for wsn, ws in ws_by_name.items():
        if wsn in placed_worksheets:
            continue
        best = None
        for p, v in leftover_visuals:
            if (p["name"], v["name"]) in matched_visuals:
                continue
            sim = _jaccard(_field_norms(ws["fields"]), _field_norms(v["fields"]))
            if best is None or sim > best[0]:
                best = (sim, p, v)
        if best and best[0] > 0.0:
            _sim, p, v = best
            res = _score_pair(ws, v, None)
            res["page"] = p["display"]
            res["dashboard"] = None
            res["engine_intent"] = engine_intent.get(wsn)
            res["note"] = "non-dashboard worksheet matched by fields only"
            visual_results.append(res)
            matched_visuals.add((p["name"], v["name"]))
            placed_worksheets.add(wsn)

    # Unmatched on either side -> advisory missing/extra.
    unmatched_worksheets = [w for w in ws_by_name if w not in placed_worksheets]
    extra_visuals = []
    for p in pbir["pages"]:
        for v in p["visuals"]:
            if (p["name"], v["name"]) in matched_visuals:
                continue
            if v["is_slicer"]:
                continue
            # Engine-generated self-service / field-parameter pages have no Tableau worksheet peer.
            extra_visuals.append({"page": p["display"], "visual": v["name"],
                                  "visual_type": v["visual_type"]})

    return _assemble_report(twb, pbir, visual_results, slicer_results,
                            unmatched_worksheets, extra_visuals, engine_report)


def _page_for_dashboard(dash, pbir):
    """Fallback page match when display names don't line up: a Tableau dashboard maps to the lone
    multi-visual PBIR page, if exactly one such page exists.
    """
    multi = [p for p in pbir["pages"]
             if len([v for v in p["visuals"] if not v["is_slicer"]]) > 1]
    return multi[0] if len(multi) == 1 else None


def _score_slicer(visual, dash_ws, page_display):
    fields = visual["filter_fields"] or visual["fields"]
    field_norms = {f["norm"] for f in fields if f.get("norm")}
    source_filter_norms = set()
    for ws in dash_ws:
        for f in ws["filters"]:
            source_filter_norms.add(f["norm"])
    matched = sorted(field_norms & source_filter_norms)
    return {
        "page": page_display,
        "visual": visual["name"],
        "fields": sorted(field_norms),
        "matches_source_filter": bool(matched),
        "matched": matched,
        "note": ("slicer field corresponds to a Tableau categorical filter"
                 if matched else
                 "slicer has no matching Tableau categorical filter on this dashboard"),
    }


def _engine_intent_index(engine_report):
    """From the engine's report.json, index each worksheet's declared visual_type + status."""
    index = {}
    if not engine_report:
        return index
    for wb in engine_report.get("workbooks", []) or []:
        for vf in wb.get("viz_fidelity", []) or []:
            ws = vf.get("worksheet")
            if ws:
                index[ws] = {"visual_type": vf.get("visual_type"), "status": vf.get("status")}
    return index


def _assemble_report(twb, pbir, visual_results, slicer_results,
                     unmatched_worksheets, extra_visuals, engine_report):
    scores = [r["score"] for r in visual_results]
    mean = sum(scores) / len(scores) if scores else None
    worst = min(scores) if scores else None

    # Penalize structural coverage gaps: a faithful rebuild leaves no source worksheet unmatched.
    # Coverage is over *unique* source worksheets and clamped to 1.0 -- a worksheet placed on more
    # than one dashboard yields multiple scored visuals but must not push coverage above 1.0.
    n_source = len(twb["worksheets"])
    n_matched = len(visual_results)
    matched_worksheets = {r["worksheet"] for r in visual_results}
    coverage = min(1.0, len(matched_worksheets) / n_source) if n_source else 1.0
    # Aggregate fidelity blends per-visual mean with coverage (unmatched worksheets drag it down).
    aggregate = None
    if mean is not None:
        aggregate = round(mean * coverage, 4)

    return {
        "kind": ORACLE_KIND,
        "version": ORACLE_VERSION,
        "advisory": True,
        "summary": {
            "aggregate_score": aggregate,
            "aggregate_band": _band(aggregate) if aggregate is not None else None,
            "mean_visual_score": round(mean, 4) if mean is not None else None,
            "worst_visual_score": round(worst, 4) if worst is not None else None,
            "coverage": round(coverage, 4),
            "source_worksheets": n_source,
            "matched_visuals": n_matched,
            "unmatched_worksheets": unmatched_worksheets,
            "extra_visuals": len(extra_visuals),
            "slicers": len(slicer_results),
        },
        "visuals": sorted(visual_results, key=lambda r: r["score"]),
        "slicers": slicer_results,
        "extra_visuals_detail": extra_visuals,
        "notes": [
            "ADVISORY structural fidelity only -- not a pass/fail and not a pixel comparison.",
            "Scores are tolerance-banded agreement, not exactness; review bands below 'faithful'.",
            "'fields_missing' = on the Tableau source but absent from the rebuilt visual; "
            "'fields_extra' = on the rebuilt visual but not on the source.",
        ],
    }


# =====================================================================================
# Optional tiers (lazy, guarded) -- never required for the structural tier above
# =====================================================================================
def dax_value_tier(*_args, **_kwargs):
    """Optional Tier-2 stub: compare live model measure values via a local Analysis Services host.

    Requires a running Power BI Desktop (an ``msmdsrv`` instance) and the ADOMD client, both of
    which are absent offline. The dependency is imported lazily so this module always imports; when
    unavailable the tier returns a structured ``unavailable`` record instead of raising. The full
    discovery/query implementation lands behind this guard.
    """
    try:  # pragma: no cover - optional, host-dependent
        from pyadomd import Pyadomd  # noqa: F401  (lazy: optional ADOMD client)
    except Exception as exc:  # noqa: BLE001
        return {"tier": "dax-value", "available": False,
                "reason": "ADOMD/pyadomd not available: %s" % exc}
    return {"tier": "dax-value", "available": False,
            "reason": "local Analysis Services discovery not yet implemented"}


def image_tier(*_args, **_kwargs):
    """Optional Tier-3 stub: tolerance-banded perceptual similarity of two rendered PNGs.

    Cross-engine literal pixel-equality is impossible, so this tier reports a similarity *band*,
    never pass/fail. numpy/Pillow are imported lazily; absent them the tier reports ``unavailable``.
    """
    try:  # pragma: no cover - optional dependency
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return {"tier": "image", "available": False,
                "reason": "numpy/Pillow not available: %s" % exc}
    return {"tier": "image", "available": False,
            "reason": "render capture not yet implemented"}


# =====================================================================================
# Top-level convenience + CLI
# =====================================================================================
def run_oracle(twb_path, report_dir, engine_report_path=None):
    """Read both sides and score them. Returns the advisory report dict."""
    twb = read_twb_views(twb_path)
    pbir = read_pbir_report(report_dir)
    engine_report = None
    if engine_report_path and os.path.isfile(engine_report_path):
        engine_report = _read_json(engine_report_path)
    report = score_report(twb, pbir, engine_report=engine_report)
    report["inputs"] = {
        "twb": os.path.abspath(twb_path),
        "report_dir": os.path.abspath(report_dir),
        "engine_report": os.path.abspath(engine_report_path) if engine_report_path else None,
    }
    return report


def render_markdown(report):
    """Render the advisory report as a compact Markdown summary."""
    s = report["summary"]
    lines = ["# Fidelity Oracle (advisory, structural)", ""]
    lines.append("- **Aggregate:** %s (%s)" % (
        s["aggregate_score"], s["aggregate_band"]))
    lines.append("- **Mean / worst visual:** %s / %s" % (
        s["mean_visual_score"], s["worst_visual_score"]))
    lines.append("- **Coverage:** %s (%d/%d worksheets matched)" % (
        s["coverage"], s["matched_visuals"], s["source_worksheets"]))
    if s["unmatched_worksheets"]:
        lines.append("- **Unmatched worksheets:** %s" % ", ".join(s["unmatched_worksheets"]))
    lines.append("")
    lines.append("| Worksheet | Visual type | Score | Band | Type | Missing | Extra |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in report["visuals"]:
        lines.append("| %s | %s | %.3f | %s | %s | %s | %s |" % (
            r["worksheet"], r["visual_type"], r["score"], r["band"],
            r["type_note"],
            ", ".join(r["fields_missing"]) or "-",
            ", ".join(r["fields_extra"]) or "-"))
    if report["slicers"]:
        lines.append("")
        lines.append("## Slicers / filters")
        for sl in report["slicers"]:
            lines.append("- `%s`: %s" % (", ".join(sl["fields"]) or "?", sl["note"]))
    lines.append("")
    for note in report["notes"]:
        lines.append("> %s" % note)
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Advisory structural fidelity oracle: Tableau .twb vs emitted PBIR report.")
    ap.add_argument("twb", help="Path to the Tableau .twb workbook (the source of truth).")
    ap.add_argument("report_dir",
                    help="Path to the emitted *.Report folder (or a parent containing one).")
    ap.add_argument("--engine-report", default=None,
                    help="Optional path to the engine's report.json for intent enrichment.")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    ap.add_argument("--out", default=None, help="Write output here instead of stdout.")
    args = ap.parse_args(argv)

    report = run_oracle(args.twb, args.report_dir, args.engine_report)
    text = (render_markdown(report) if args.format == "md"
            else json.dumps(report, indent=2, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
