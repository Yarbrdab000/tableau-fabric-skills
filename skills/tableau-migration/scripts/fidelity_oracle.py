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
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
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

# Minute placement check: the Tableau dashboard zone, projected onto the PBI canvas, is compared
# edge-by-edge against the emitted visual's own canvas px. A worst-edge offset at/under this many
# target-canvas pixels is treated as a pixel-exact placement. This is a DIAGNOSTIC (it surfaces the
# exact per-zone drift the IoU band rounds away); it does not change the position score, so it never
# moves calibration. Layout fidelity is proven from geometry alone -- never from a PBI render.
PLACEMENT_EXACT_PX = 2.0
# A softer, human-calibrated bar: a hand-built faithful copy places zones by eye to within roughly a
# percent of the canvas and reads as "decent" zoning, so a worst-edge offset within this fraction of
# the canvas is reported as an acceptable placement (an engine deriving placement from the source
# zones should sit comfortably under it).
PLACEMENT_ACCEPTABLE_FRAC = 0.01

# A Tableau dashboard zone tree nests its worksheet/object zones inside structural CONTAINERS
# (basic/flow layouts). Those containers are scaffolding, not placed objects -- they hold a region
# but draw nothing -- so they are excluded from non-worksheet object accounting. Everything else
# carrying a ``type-v2`` (title, text, image, legend/color/size/shape, paramctrl, filter,
# dashboard-object, ...) is a real placed object the rebuild must also position.
_CONTAINER_ZONE_TYPES = frozenset({"layout-basic", "layout-flow", "layout"})

# Advisory band thresholds on the aggregate 0..1 score. Named bands, never pass/fail.
BANDS = (
    (0.95, "faithful"),       # indistinguishable within cross-engine noise
    (0.85, "strong"),         # minor, explainable divergence
    (0.60, "review"),         # advisory: a human should eyeball it
    (0.0, "divergent"),       # materially different -- likely a real rebuild gap
)

# Combined cross-tier fidelity: an advisory headline that fuses the tiers that actually ran.
# Structural leads (least engine-coupled, always available); value and image add the two things
# structural is blind to (computed numbers; mark-type/layout). Weights are renormalized over only
# the tiers present, so the headline is comparable whether one tier ran or all three -- while a
# separate ``confidence`` flag records how much evidence backs it.
COMBINED_WEIGHTS = {"structural": 0.5, "value": 0.3, "image": 0.2}

# A visual whose chart TYPE (and position, when placed) agree strongly while its field-NAME overlap
# is low is the signature of a faithful rebuild that REMODELED/renamed fields -- e.g. promoting a
# Tableau column to a star-schema dimension (``Order Date`` -> a ``Date`` table) or naming an
# implicit aggregate (``COUNT(Orders)`` -> a ``count orders`` measure). That is good Power BI
# modeling, not an infidelity, but it craters the name-based field/role components. We flag it
# advisorily so a low structural score is not misread as a divergent rebuild -- the DAX-value and
# image tiers (which compare numbers/pixels, immune to renaming) are the authority in that case.
_REMODEL_TYPE_MIN = 0.95
_REMODEL_POSITION_MIN = 0.85
_REMODEL_FIELDS_MAX = 0.50
_REMODEL_DIAGNOSIS = "remodel-rename-suspected"


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

# Tableau date-truncation derivation tokens (TRUNC to a unit). On an axis these render as a
# CONTINUOUS (green) date; paired with the quantitative typekey (``:qk``) and an Automatic mark
# they are Tableau's canonical line-chart trigger. The SAME tokens with an ordinal typekey
# (``tdy:Order Date:ok``) are a discrete date instead -- e.g. a highlight-table axis -- so the
# typekey, not the derivation alone, decides continuity.
_DATE_TRUNC_DERIVS = frozenset({
    "tyr", "tqr", "tmn", "twk", "tdy", "thr", "tmi", "tse",
})

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
def _as_dict(value):
    """Coerce to a dict for safe ``.get``/``.items`` access; a non-dict (list/str/None from a
    malformed visual.json) becomes ``{}`` so the advisory reader never raises on bad input."""
    return value if isinstance(value, dict) else {}


def _as_list(value):
    """Coerce to a list for safe iteration; a non-list becomes ``[]`` (see ``_as_dict``)."""
    return value if isinstance(value, list) else []


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
        agg = _as_dict(node.get("Aggregation")).get("Function")

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
    visual = _as_dict(data.get("visual"))
    vtype = visual.get("visualType")
    pos = _as_dict(data.get("position"))
    position = {
        "x": _f(pos.get("x")), "y": _f(pos.get("y")),
        "w": _f(pos.get("width")), "h": _f(pos.get("height")),
        "z": _f(pos.get("z")),
    }

    roles = {}
    fields = []
    qstate = _as_dict(_as_dict(visual.get("query")).get("queryState"))
    for role_key, role_block in qstate.items():
        projections = _as_list(_as_dict(role_block).get("projections"))
        bucket = []
        for proj in projections:
            if not isinstance(proj, dict):
                continue
            fld = _pbir_extract_field(proj.get("field"))
            if fld is None:
                continue
            fld = dict(fld, role=role_key,
                       display=proj.get("nativeQueryRef") or proj.get("queryRef"),
                       query_ref=proj.get("queryRef"))
            bucket.append(fld)
            fields.append(fld)
        if bucket:
            roles[role_key] = bucket

    # Slicer selection fields come from a sibling filterConfig, not the query projections.
    filt_fields = []
    for filt in _as_list(_as_dict(data.get("filterConfig")).get("filters")):
        if not isinstance(filt, dict):
            continue
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
                        except (ValueError, OSError, AttributeError, TypeError, KeyError, IndexError) as exc:
                            # Advisory tool: one malformed visual.json must never crash the whole
                            # run -- isolate it to a warning and keep scoring the rest.
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
    typekey = None
    # ``deriv:Name:typekey`` -- split on the FIRST and LAST colon (names can contain neither here,
    # but guard by taking head/tail around the middle).
    parts = raw.split(":")
    if len(parts) >= 3:
        deriv = parts[0].strip().lower()
        name = ":".join(parts[1:-1]).strip()
        typekey = parts[-1].strip().lower()
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
        # ``qk`` = quantitative/continuous (green pill); ``ok``/``nk`` = ordinal/nominal (discrete).
        "continuous": typekey == "qk",
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


def _is_continuous_date_dim(field):
    """True when a pill is a continuous (green) date axis: a date-truncation derivation rendered as
    quantitative (``tdy:Order Date:qk``). Under an Automatic mark Tableau draws such an axis as a
    line; a discrete date part (``...:ok``/``:nk``) or any non-date field is not one."""
    if not isinstance(field, dict) or field.get("is_measure"):
        return False
    return bool(field.get("continuous")) and (field.get("deriv") or "") in _DATE_TRUNC_DERIVS


def _distinct_measure_count(measures):
    """Count distinct measures on a worksheet by normalized name (a Measure Names/Values layout
    lists each measure once); used to tell a single-value card from a multi-measure table."""
    return len({(m.get("norm") or "") for m in (measures or []) if isinstance(m, dict)})


def _infer_twb_family(mark, dims, measures, has_geometry, uses_measure_values):
    """Second-opinion chart-family classifier for a Tableau worksheet.

    Returns ``(family, asserted)``. ``asserted`` is False when the mark is ``Automatic`` and the
    shelf shape does not strongly imply a family -- the scorer then declines to punish a plausible
    rebuild rather than guessing aggressively.
    """
    mlow = (mark or "").lower()
    if has_geometry:
        return FAM_MAP, True
    # A Square mark with axis dimensions is a highlight table -> Power BI matrix (the Comcast
    # "Segment % Dod" case); a Square mark without dimensions is a treemap/density we don't assert.
    if mlow == "square":
        return (FAM_MATRIX, True) if dims else (FAM_UNKNOWN, False)
    if mlow in _MARK_FAMILY and mlow != "automatic":
        fam = _MARK_FAMILY[mlow]
        # A dimensionless Text mark is a card -- UNLESS it lays out Measure Names + multiple Measure
        # Values, which is a measures TABLE (its faithful rebuild is a Power BI table/tableEx, not a
        # multiRowCard); a single dimensionless value stays a card.
        if fam == FAM_TABLE and not dims:
            if uses_measure_values and _distinct_measure_count(measures) >= 2:
                return FAM_TABLE, True
            return FAM_CARD, True
        return fam, True
    # Automatic: infer from shelf shape (Tableau's own default heuristic, applied conservatively).
    # A continuous (green) date axis under an Automatic mark is Tableau's default line chart -- the
    # implicit measure (e.g. COUNT) or an explicit one is drawn as a line over the continuous date.
    if any(_is_continuous_date_dim(d) for d in dims) and (measures or len(dims) == 1):
        return FAM_LINE, True
    if uses_measure_values and not dims:
        # Measure Names + multiple Measure Values is a measures TABLE; a single value is a card.
        if _distinct_measure_count(measures) >= 2:
            return FAM_TABLE, True
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
        # Iterate EVERY pane, not just the first: a dual-axis worksheet emits one <pane> per axis,
        # each with its own <encodings>, so a first-pane-only read silently drops the secondary
        # axis's color/size/detail bindings. (This matches _has_measure_values_encoding, which
        # already scans all panes.) The mark class is taken from the first pane as the family-
        # inference representative; encodings repeated across panes dedup downstream.
        for pane_index, pane in enumerate(_children(panes, "pane")):
            mark_el = _first_child(pane, "mark")
            if pane_index == 0 and mark_el is not None and mark_el.get("class"):
                mark = mark_el.get("class")
            enc = _first_child(pane, "encodings")
            if enc is None:
                continue
            for e in enc:
                if _local(e.tag) == "geometry":
                    has_geometry = True
                    continue
                col = e.get("column")
                channel = _local(e.tag)
                for m in _PILL_RE.finditer(col or ""):
                    fld = _parse_pill(m.group("inner"), caption_index)
                    if fld is None:
                        continue
                    # A MEASURE on the LOD/detail channel backs a reference-line distribution
                    # band (e.g. a WINDOW_STDEV computation), not a visible mark encoding the
                    # rebuild must reproduce -- exclude it so a faithful rebuild is not charged
                    # for omitting decoration. Genuine detail DIMENSIONS on <lod> are kept.
                    if channel == "lod" and fld.get("is_measure"):
                        continue
                    encoding_fields.append(dict(fld, channel=channel))

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

    # Worksheet filters feed the advisory slicer cross-check only (never the per-visual score, so
    # this cannot move calibration). Capture EVERY filter with a resolvable field -- categorical
    # (discrete), quantitative (numeric/date RANGE), and relative-date quick filters alike --
    # tagging each with its Tableau filter class, so an emitted slicer can be cross-checked against
    # a range/relative-date source filter, not only a categorical one.
    filters = []
    for fl in _iter_local(view, "filter"):
        fclass = (fl.get("class") or "").lower() or None
        col = fl.get("column") or ""
        m = _PILL_RE.search(col)
        if not m:
            continue
        fld = _parse_pill(m.group("inner"), caption_index)
        if fld is not None:
            filters.append(dict(fld, filter_class=fclass))

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
    objects = []
    seen_object_ids = set()
    ext_w = ext_h = 0.0
    for zone in _iter_local(db, "zone"):
        if zone in device_zones:
            continue
        x, y = _zone_f(zone, "x"), _zone_f(zone, "y")
        w, h = _zone_f(zone, "w"), _zone_f(zone, "h")
        valid_rect = None not in (x, y, w, h) and w > 0 and h > 0
        if valid_rect:
            ext_w = max(ext_w, x + w)
            ext_h = max(ext_h, y + h)
        zname = zone.get("name")
        ztype = (zone.get("type-v2") or zone.get("type") or "").lower()
        # A worksheet zone is a named, *untyped* zone whose name resolves to a real worksheet.
        if zname and zname in worksheet_names and not ztype:
            if valid_rect:
                zones.append({"worksheet": zname, "x": x, "y": y, "w": w, "h": h})
            continue
        # A non-worksheet object zone is any typed, NON-container zone (title, text, image,
        # legend/color, paramctrl, filter, ...). These are real placed objects -- captured as
        # additive "expected extras" so the reviewer sees the full layout and the engine has a
        # placement target for them. They never feed the score and never affect coverage.
        if ztype and ztype not in _CONTAINER_ZONE_TYPES and valid_rect:
            oid = zone.get("id")
            if oid is not None and oid in seen_object_ids:
                continue
            if oid is not None:
                seen_object_ids.add(oid)
            objects.append({"kind": ztype, "id": oid, "worksheet": zname or None,
                            "param": zone.get("param"), "x": x, "y": y, "w": w, "h": h})

    for z in zones:
        if ext_w and ext_h:
            z["nposition"] = {"x": z["x"] / ext_w, "y": z["y"] / ext_h,
                              "w": z["w"] / ext_w, "h": z["h"] / ext_h}
        else:
            z["nposition"] = None
    for o in objects:
        if ext_w and ext_h:
            o["nposition"] = {"x": o["x"] / ext_w, "y": o["y"] / ext_h,
                              "w": o["w"] / ext_w, "h": o["h"] / ext_h}
        else:
            o["nposition"] = None
    return {"name": name, "extent": {"w": ext_w or None, "h": ext_h or None},
            "zones": zones, "objects": objects}


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


# Standard geographic drill hierarchy (coarse -> fine), by normalized field name. A filled/choropleth
# map geocodes its finest level *within* its ancestors, and Tableau auto-adds the whole hierarchy to
# the Marks "Detail" (lod) card -- so a coarser geo ancestor sitting only on detail is implied by the
# geocoding of the finer level, not an independent visual encoding the rebuild must reproduce. This is
# the general geographic-role principle; "region" alone is excluded because Tableau's Superstore
# "Region" (Central/East/West/South) is a categorical group, not a geocoded map level.
_GEO_LEVEL_RANK = {
    "country": 0, "countryregion": 0, "nation": 0,
    "state": 1, "stateprovince": 1, "province": 1,
    "county": 2,
    "city": 3,
    "postalcode": 4, "zipcode": 4, "zippostalcode": 4,
}

# Marks channels that merely carry a geocoding-implied geo ancestor (vs an independent encoding such
# as color/size/label/path or a rows/cols axis, which would make a coarser level a real binding).
_GEO_IMPLIED_CHANNELS = frozenset({"lod", "detail"})


def _geo_rank(norm):
    """Return a field's geographic-drill rank (smaller = coarser), or None if it is not a geo level."""
    return _GEO_LEVEL_RANK.get(norm)


def _suppressed_geo_ancestors(twb_ws, pbir_visual):
    """Source geo dims that a faithful map rebuild does NOT need to project independently.

    For a geographic map worksheet, if the rebuilt visual binds a source geo level (e.g.
    State/Province), any *coarser* same-hierarchy ancestor (e.g. Country/Region) that the Tableau
    sheet carries only on the Marks detail/lod card is satisfied by geocoding the finer level -- so
    it must not be charged as a "missing" field. Map-gated (``has_geometry``), detail-only, and tied
    to a finest level that is itself a bound source geo dim, so it cannot mask a genuine grain
    mismatch or touch any non-map visual.
    """
    if not twb_ws.get("has_geometry"):
        return set()
    tgt = _field_norms(pbir_visual.get("fields") or [])
    # Finest source geo level that the rebuild actually bound (rank; larger = finer).
    bound_src_ranks = [r for f in twb_ws.get("dims") or []
                       for r in (_geo_rank(f.get("norm")),)
                       if r is not None and f.get("norm") in tgt]
    if not bound_src_ranks:
        return set()
    finest_bound = max(bound_src_ranks)
    suppressed = set()
    for f in twb_ws.get("dims") or []:
        norm = f.get("norm")
        rank = _geo_rank(norm)
        if rank is None or rank >= finest_bound or norm in tgt:
            continue
        if (f.get("channel") or "").lower() in _GEO_IMPLIED_CHANNELS:
            suppressed.add(norm)
    return suppressed


def _date_implied_sources(twb_ws, pbir_visual, relationships):
    """Source date axes a faithful rebuild satisfies via an ACTIVE model date relationship.

    When a Tableau worksheet carries a continuous date-truncation axis (e.g. ``Order Date``) that the
    rebuilt visual does NOT bind by name, but the visual instead binds a related ``Date`` dimension
    table, credit the source date field as reproduced IFF an **ACTIVE** relationship runs from that
    exact source date column to the Date table the visual binds. This is the documented Power BI star
    pattern -- the engine rebinds the continuous order-date axis onto a proper ``Date`` dimension
    whose ACTIVE relationship (``Orders.Order_Date -> Date.Date``) makes it display identical values
    to Tableau's "Order Date" axis.

    Honesty gate (mirrors the geo-implied principle, but on live directional relationship evidence):
    an axis backed only by an **INACTIVE** relationship (e.g. a secondary ``Ship_Date`` role-playing
    rel ``isActive: false``) or by an UNRELATED date table is NOT credited -- the source date field
    correctly stays a missing binding, so a real grain/field gap is never masked. Returns ``set()``
    whenever no relationships are supplied, so the default (relationship-blind) scoring is unchanged.
    """
    if not relationships:
        return set()
    fields = pbir_visual.get("fields") or []
    tgt = _field_norms(fields)
    # Tables the rebuilt visual binds as a (non-measure) dimension axis, by normalized entity name.
    bound_dim_entities = {
        _norm(f.get("entity")) for f in fields
        if not f.get("is_measure") and f.get("entity")
    }
    if not bound_dim_entities:
        return set()
    active_rels = [r for r in relationships
                   if r.get("active") and r.get("from_norm") and r.get("to_table_norm")]
    if not active_rels:
        return set()
    credited = set()
    for f in twb_ws.get("dims") or []:
        norm = f.get("norm")
        # Only a source DATE-truncation axis the rebuild did not bind by name is a candidate.
        if not norm or norm in tgt or (f.get("deriv") or "") not in _DATE_TRUNC_DERIVS:
            continue
        for r in active_rels:
            if r["from_norm"] == norm and r["to_table_norm"] in bound_dim_entities:
                credited.add(norm)
                break
    return credited


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


def _placement_delta(zone, pbir_visual, canvas_w, canvas_h):
    """Minute, render-free placement check.

    Project the Tableau dashboard zone (normalized to the dashboard extent) onto the PBI canvas in
    pixels, then diff it edge-by-edge against the emitted visual's own canvas px. The result is how
    far -- to the pixel -- the engine placed each visual from the zone it must occupy. This proves
    layout fidelity from geometry alone: the PBI side is the spec, never a render. It is purely
    additive diagnostics and does not feed the position score (so it cannot move calibration).
    """
    z = zone.get("nposition") if zone else None
    p = pbir_visual.get("position")
    if not z or not p:
        return None
    if None in (p.get("x"), p.get("y"), p.get("w"), p.get("h")) or not canvas_w or not canvas_h:
        return None
    # The Tableau zone projected onto the PBI canvas (px) is the authoritative placement target.
    tz = {"x": z["x"] * canvas_w, "y": z["y"] * canvas_h,
          "w": z["w"] * canvas_w, "h": z["h"] * canvas_h}
    d_left = p["x"] - tz["x"]
    d_top = p["y"] - tz["y"]
    d_right = (p["x"] + p["w"]) - (tz["x"] + tz["w"])
    d_bottom = (p["y"] + p["h"]) - (tz["y"] + tz["h"])
    d_w = p["w"] - tz["w"]
    d_h = p["h"] - tz["h"]
    d_center = math.hypot((p["x"] + p["w"] / 2.0) - (tz["x"] + tz["w"] / 2.0),
                          (p["y"] + p["h"] / 2.0) - (tz["y"] + tz["h"] / 2.0))
    max_edge = max(abs(d_left), abs(d_top), abs(d_right), abs(d_bottom))
    accept_px = PLACEMENT_ACCEPTABLE_FRAC * max(canvas_w, canvas_h)
    iou = _iou(z, pbir_visual.get("nposition")) if pbir_visual.get("nposition") else None
    r2 = lambda v: round(v, 2)
    return {
        "canvas": {"w": canvas_w, "h": canvas_h},
        "tableau_zone_px": {k: r2(tz[k]) for k in ("x", "y", "w", "h")},
        "pbir_px": {"x": r2(p["x"]), "y": r2(p["y"]), "w": r2(p["w"]), "h": r2(p["h"])},
        "delta_px": {"left": r2(d_left), "top": r2(d_top), "right": r2(d_right),
                     "bottom": r2(d_bottom), "width": r2(d_w), "height": r2(d_h),
                     "center": r2(d_center)},
        "max_edge_px": r2(max_edge),
        "iou": round(iou, 4) if iou is not None else None,
        "pixel_exact": max_edge <= PLACEMENT_EXACT_PX,
        "within_tolerance": max_edge <= accept_px,
    }


def _score_pair(twb_ws, pbir_visual, zone, canvas_w=None, canvas_h=None, relationships=None):
    type_s, type_note = _type_score(twb_ws, pbir_visual)
    # On a geographic map, a coarser geo ancestor carried only on Marks detail is implied by
    # geocoding the finer bound level (see _suppressed_geo_ancestors) -- drop it from the scored
    # source set so a faithful map is not charged for "omitting" Country/Region above State/Province.
    geo_implied = _suppressed_geo_ancestors(twb_ws, pbir_visual)
    # A continuous date axis the rebuild rebinds onto a related Date dimension is reproduced when an
    # ACTIVE model relationship runs from the source date column to that Date table (see
    # _date_implied_sources) -- drop it from the scored source set so a faithful star-schema date
    # rebind is not charged as a "missing" Order Date. Inactive/unrelated rels are NOT credited.
    date_implied = _date_implied_sources(twb_ws, pbir_visual, relationships)
    suppressed = geo_implied | date_implied
    if suppressed:
        scored_ws = dict(twb_ws)
        scored_ws["fields"] = [f for f in twb_ws["fields"] if f.get("norm") not in suppressed]
        scored_ws["dims"] = [f for f in twb_ws["dims"] if f.get("norm") not in suppressed]
    else:
        scored_ws = twb_ws
    src_fields = _field_norms(scored_ws["fields"])
    tgt_fields = _field_norms(pbir_visual["fields"])
    field_s = _jaccard(src_fields, tgt_fields)
    roles_s = _roles_score(scored_ws, pbir_visual)
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
    # Advisory diagnosis: strong type/position agreement with low field-name overlap is the
    # signature of a faithful field remodel/rename, not a divergent rebuild (see constants above).
    pos_val = parts.get("position")
    diagnosis = None
    if (type_s >= _REMODEL_TYPE_MIN and field_s < _REMODEL_FIELDS_MAX
            and (pos_val is None or pos_val >= _REMODEL_POSITION_MIN)):
        diagnosis = _REMODEL_DIAGNOSIS
    result = {
        "worksheet": twb_ws["name"],
        "visual": pbir_visual["name"],
        "visual_type": pbir_visual["visual_type"],
        "source_family": twb_ws["family"],
        "target_family": pbir_visual["family"],
        "components": {k: round(parts[k], 4) for k in parts},
        "score": round(overall, 4),
        "band": _band(overall),
        "type_note": type_note,
        "diagnosis": diagnosis,
        "fields_matched": matched,
        "fields_missing": missing,   # in Tableau source, absent from rebuilt visual
        "fields_extra": extra,       # in rebuilt visual, absent from Tableau source
    }
    if geo_implied:
        # Coarser geo ancestors satisfied by geocoding the finer bound level (not counted missing).
        result["geo_implied"] = sorted(geo_implied)
    if date_implied:
        # Source date axes reproduced via an ACTIVE model relationship onto a related Date dimension.
        result["date_implied"] = sorted(date_implied)
    placement = _placement_delta(zone, pbir_visual, canvas_w, canvas_h)
    if placement is not None:
        result["placement"] = placement
    return result


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


def aliases_from_candidate_records(records):
    """Merge each candidate record's ``field_aliases`` ({emitted queryRef -> Tableau caption})
    into one ``{ref: caption}`` map.

    ``field_aliases`` is the engine's additive bridge across a faithful star-schema rename -- the
    emitted Power BI ref (e.g. ``Date.Date``, ``_Measures.count orders``) back to the Tableau source
    caption it was rebound from (``Order Date``). Tolerates records (or whole builds) that predate
    the producer and so carry no ``field_aliases`` key -- then the merged map is empty and aliasing
    is a no-op, preserving the un-aliased score exactly.
    """
    merged = {}
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        fa = rec.get("field_aliases")
        if isinstance(fa, dict):
            for ref, caption in fa.items():
                if ref and caption:
                    merged.setdefault(str(ref), str(caption))
    return merged


def _alias_lookup(field_aliases):
    """Index an alias map by normalized emitted ref -> caption (so ``Date.Date`` matches a
    reconstructed ``entity.property`` regardless of dotting/spacing/case)."""
    out = {}
    for ref, caption in (field_aliases or {}).items():
        key = _norm(ref)
        if key and caption:
            out.setdefault(key, caption)
    return out


def _aliased_norm(field, lookup):
    """Resolve a PBIR field's emitted ref back to its Tableau source-caption norm, or ``None``.

    Tries the strongest keys first (the full ``queryRef`` / ``entity.property``) before the bare
    property, so a short native ref never spuriously matches a full-ref alias key.
    """
    candidates = (
        field.get("query_ref"),
        "%s.%s" % (field.get("entity") or "", field.get("property") or ""),
        field.get("display"),
        field.get("property"),
    )
    for key in candidates:
        if key:
            caption = lookup.get(_norm(key))
            if caption:
                return _norm(caption)
    return None


def _apply_field_aliases(pbir, field_aliases):
    """Rewrite each PBIR field's match-norm to its Tableau source caption when an alias resolves.

    Lets the structural tier see THROUGH a faithful star-schema rename (emitted ``Date.Date`` ->
    source ``Order Date``) so a renamed-but-faithful binding is not scored as a field mismatch. The
    original emitted norm is preserved as ``norm_emitted``. Returns the count of fields remapped;
    a no-op (returns 0) when no usable alias map is supplied, so the un-aliased score is unchanged.
    """
    lookup = _alias_lookup(field_aliases)
    if not lookup:
        return 0
    remapped = 0
    for page in pbir.get("pages", []):
        for v in page.get("visuals", []):
            for f in v.get("fields", []):
                resolved = _aliased_norm(f, lookup)
                if resolved and resolved != f.get("norm"):
                    f.setdefault("norm_emitted", f.get("norm"))
                    f["norm"] = resolved
                    remapped += 1
    return remapped


def score_report(twb, pbir, engine_report=None, field_aliases=None, relationships=None):
    """Score a parsed Tableau workbook against a parsed PBIR report. Both come from the readers above.

    Pairs each Tableau dashboard to the PBIR page sharing its display name, greedily matches that
    dashboard's worksheets to the page's non-slicer visuals by content + position, and grades each
    pair. Worksheets not placed on any dashboard fall back to a best-effort field-only match against
    any remaining visual. Returns an advisory report dict (never a pass/fail).

    ``field_aliases`` (optional ``{emitted queryRef -> Tableau caption}``, e.g. from
    ``aliases_from_candidate_records``) lets the field/role components see through a faithful
    star-schema rename before name overlap is computed. Omitting it leaves scoring exactly as-is.

    ``relationships`` (optional, from ``_parse_tmdl_relationships``) supplies the emitted model's
    ACTIVE/inactive relationship edges so a continuous date axis rebound onto a related ``Date``
    dimension can be credited as reproducing its source date field (see ``_date_implied_sources``).
    Omitting it (the default) leaves scoring relationship-blind and therefore exactly as-is.
    """
    alias_resolved = _apply_field_aliases(pbir, field_aliases) if field_aliases else 0
    ws_by_name = twb["worksheets"]
    visual_pages = {p["display"]: p for p in pbir["pages"]}
    visual_pages_by_name = {p["name"]: p for p in pbir["pages"]}

    engine_intent = _engine_intent_index(engine_report)


    visual_results = []
    slicer_results = []
    dashboard_objects = []
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
            res = _score_pair(ws, v, zone, page.get("width"), page.get("height"),
                              relationships=relationships)
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
        # Non-worksheet dashboard objects (title/text/legend/paramctrl/filter/...) -> advisory
        # placement targets, projected onto this page's canvas px. Expected extras: never scored,
        # never counted against coverage.
        for obj in dash.get("objects", []):
            dashboard_objects.append(
                _object_target(obj, dash["name"], page["display"],
                               page.get("width"), page.get("height")))

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
            res = _score_pair(ws, v, None, relationships=relationships)
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
                            unmatched_worksheets, extra_visuals, engine_report,
                            alias_resolved=alias_resolved,
                            dashboard_objects=dashboard_objects)


def _page_for_dashboard(dash, pbir):
    """Fallback page match when display names don't line up: a Tableau dashboard maps to the lone
    multi-visual PBIR page, if exactly one such page exists.
    """
    multi = [p for p in pbir["pages"]
             if len([v for v in p["visuals"] if not v["is_slicer"]]) > 1]
    return multi[0] if len(multi) == 1 else None


def _nbox(npos):
    """Convert a normalized ``{x, y, w, h}`` position into a fractional ``(x0, y0, x1, y1)`` crop box."""
    if not npos:
        return None
    x, y, w, h = npos.get("x"), npos.get("y"), npos.get("w"), npos.get("h")
    if None in (x, y, w, h):
        return None
    return (float(x), float(y), float(x) + float(w), float(y) + float(h))


def regions_from_layout(twb, pbir):
    """Derive per-worksheet image crop regions from the structural dashboard pairing.

    For each Tableau dashboard worksheet that pairs to a PBIR visual, emit a region whose ``ref``
    box is the worksheet's normalized dashboard-zone rect and whose ``cand`` box is the paired
    visual's normalized PBIR position. The image tier then crops *each engine's* render by its OWN
    layout and SSIM-compares the same logical zone -- no hand-estimated crop fractions. This is the
    structural tier feeding the image tier: it localizes which worksheet (mark-type/sort/layout)
    diverges, exactly where a single whole-dashboard SSIM number is blind.
    """
    regions = []
    visual_pages = {p["display"]: p for p in pbir["pages"]}
    ws_by_name = twb["worksheets"]
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
            zone = zone_by_ws.get(wsn)
            rbox = _nbox(zone.get("nposition") if zone else None)
            if rbox is None:
                continue
            region = {"name": wsn, "ref": rbox}
            cbox = _nbox(vidx[vn].get("nposition"))
            if cbox is not None:
                region["cand"] = cbox
            regions.append(region)
    return regions


def _score_slicer(visual, dash_ws, page_display):
    fields = visual["filter_fields"] or visual["fields"]
    field_norms = {f["norm"] for f in fields if f.get("norm")}
    # Map each source-filter field to its Tableau filter class so a matched slicer can report
    # whether it cross-checks a categorical, quantitative (range), or relative-date filter.
    source_filter_classes = {}
    for ws in dash_ws:
        for f in ws["filters"]:
            source_filter_classes.setdefault(f["norm"], f.get("filter_class"))
    matched = sorted(field_norms & set(source_filter_classes))
    matched_filter_classes = sorted({source_filter_classes[n] for n in matched
                                     if source_filter_classes.get(n)})
    if matched:
        cls_phrase = "/".join(matched_filter_classes) if matched_filter_classes else "categorical"
        note = "slicer field corresponds to a Tableau %s filter" % cls_phrase
    else:
        note = "slicer has no matching Tableau filter on this dashboard"
    return {
        "page": page_display,
        "visual": visual["name"],
        "fields": sorted(field_norms),
        "matches_source_filter": bool(matched),
        "matched": matched,
        "matched_filter_classes": matched_filter_classes,
        "note": note,
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


def _object_target(obj, dashboard, page_display, canvas_w, canvas_h):
    """Project one non-worksheet dashboard object's zone onto the PBI page canvas (px).

    Returns the object's kind, any worksheet association (a legend belongs to a worksheet) and
    parameter binding, plus its ``target_px`` -- where, to the pixel, the rebuild should place it.
    This is the render-free placement target for titles/legends/filter cards/param controls, the
    same zone->px projection the worksheet placement check uses. Purely advisory.
    """
    npos = obj.get("nposition")
    rec = {
        "kind": obj.get("kind"),
        "worksheet": obj.get("worksheet"),
        "param": obj.get("param"),
        "dashboard": dashboard,
        "page": page_display,
        "nposition": npos,
    }
    if npos and canvas_w and canvas_h:
        rec["target_px"] = {"x": round(npos["x"] * canvas_w, 2),
                            "y": round(npos["y"] * canvas_h, 2),
                            "w": round(npos["w"] * canvas_w, 2),
                            "h": round(npos["h"] * canvas_h, 2)}
    return rec


def _placement_rollup(visual_results):
    """Aggregate the per-visual minute placement deltas into one dashboard-level layout verdict.

    Counts how many paired visuals the engine placed pixel-exact vs merely within the acceptable
    band vs drifted, and reports the worst/mean worst-edge offset and which worksheet drifted most.
    Render-free and purely additive -- it summarizes the existing ``placement`` diagnostics and does
    not touch the score. ``None`` when no visual carried a placement delta (no canvas/zone).
    """
    placements = [(r["worksheet"], r["placement"]) for r in visual_results if r.get("placement")]
    if not placements:
        return None
    n = len(placements)
    exact = sum(1 for _w, p in placements if p.get("pixel_exact"))
    within = sum(1 for _w, p in placements if p.get("within_tolerance"))
    edges = [(w, p.get("max_edge_px")) for w, p in placements if p.get("max_edge_px") is not None]
    worst_edge = max((e for _w, e in edges), default=None)
    mean_edge = round(sum(e for _w, e in edges) / len(edges), 2) if edges else None
    worst_ws = max(edges, key=lambda we: we[1])[0] if edges else None
    if exact == n:
        verdict = "pixel-exact"
    elif within == n:
        verdict = "acceptable"
    else:
        verdict = "drifted"
    return {
        "evaluated": n,
        "pixel_exact": exact,
        "within_tolerance": within,
        "drifted": n - within,
        "worst_max_edge_px": worst_edge,
        "mean_max_edge_px": mean_edge,
        "worst_worksheet": worst_ws,
        "verdict": verdict,
    }


def _assemble_report(twb, pbir, visual_results, slicer_results,
                     unmatched_worksheets, extra_visuals, engine_report,
                     alias_resolved=0, dashboard_objects=None):
    dashboard_objects = dashboard_objects or []
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

    remodel_suspected = [r for r in visual_results
                         if r.get("diagnosis") == _REMODEL_DIAGNOSIS]
    placement = _placement_rollup(visual_results)

    notes = [
        "ADVISORY structural fidelity only -- not a pass/fail and not a pixel comparison.",
        "Scores are tolerance-banded agreement, not exactness; review bands below 'faithful'.",
        "'fields_missing' = on the Tableau source but absent from the rebuilt visual; "
        "'fields_extra' = on the rebuilt visual but not on the source.",
    ]
    if remodel_suspected:
        notes.append(
            "{} visual(s) show strong chart-type/position agreement but low field-NAME overlap -- "
            "the signature of a faithful rebuild that remodeled/renamed fields (e.g. a star-schema "
            "Date dimension or a renamed measure). A low structural score there reflects naming, "
            "not infidelity; corroborate with the DAX-value and image tiers, which compare "
            "numbers/pixels and are immune to renaming.".format(len(remodel_suspected)))
    if alias_resolved:
        notes.append(
            "{} emitted field ref(s) were resolved back to their Tableau source caption via the "
            "engine field-alias map before name overlap, so a faithful star-schema rename "
            "(e.g. Date.Date -> Order Date) scores as a match rather than a mismatch.".format(
                alias_resolved))
    if dashboard_objects:
        notes.append(
            "{} non-worksheet dashboard object(s) (title/text/legend/param control/filter) were "
            "captured as advisory placement targets (their 'target_px' on the page canvas). They are "
            "expected extras: never scored and never counted against coverage.".format(
                len(dashboard_objects)))

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
            "remodel_rename_suspected": len(remodel_suspected),
            "fields_alias_resolved": alias_resolved,
            "placement": placement,
            "dashboard_objects": len(dashboard_objects),
        },
        "visuals": sorted(visual_results, key=lambda r: r["score"]),
        "slicers": slicer_results,
        "extra_visuals_detail": extra_visuals,
        "dashboard_objects_detail": dashboard_objects,
        "notes": notes,
    }


# =====================================================================================
# Per-visual fidelity verdict: the migration loop's objective function
# =====================================================================================
# The structural tier above scores each paired visual on a 0..1 continuum. The fidelity *loop*
# wants a coarse, honest, per-visual VERDICT it can read at a glance and optimize toward. This
# layer reduces a structural per-visual record to one of four states:
#   REPRODUCED -- exact chart family AND every source field present (a faithful rebuild).
#   PARTIAL    -- recognizable but not faithful: exact family with some source fields missing, OR a
#                 related/soft-typed substitution (e.g. an area sheet rebuilt as a line) with the
#                 full source field set intact.
#   DEGRADED   -- a material visual-fidelity loss: wrong chart family (e.g. a map rebuilt as a
#                 table), none of the source fields reproduced, or a soft-typed pairing that also
#                 dropped source fields.
#   MISSING    -- the Tableau worksheet has no paired Power BI visual at all.
# It is DETERMINISTIC and derived ENTIRELY from the structural per-visual record (no new parsing)
# and never feeds the structural aggregate, the combined headline, or any calibration -- it is a
# separate, additive, advisory read layered on top of score_report's output.
STATE_REPRODUCED = "REPRODUCED"
STATE_PARTIAL = "PARTIAL"
STATE_DEGRADED = "DEGRADED"
STATE_MISSING = "MISSING"
# Advisory loop-credit per state: lets the loop reduce the four-state verdicts to one objective
# number to optimize. Faithful=1.0, recognizable=0.5, lossy=0.25, absent=0.0.
_STATE_CREDIT = {STATE_REPRODUCED: 1.0, STATE_PARTIAL: 0.5, STATE_DEGRADED: 0.25, STATE_MISSING: 0.0}


def classify_visual_state(visual_result):
    """Map one structural per-visual record (a :func:`_score_pair` dict) to a coarse fidelity state.

    Reads only keys the structural tier already emits: ``components.type`` (1.0 exact-family,
    ``0 < c < 1`` related/soft, 0.0 asserted-mismatch), ``type_note`` (to tell a related
    substitution apart from a type-indeterminate pairing) and ``fields_matched`` / ``fields_missing``.
    Deterministic; never raises. Returns ``(state, reason)``.
    """
    comp = visual_result.get("components") or {}
    type_s = comp.get("type")
    note = visual_result.get("type_note") or ""
    matched = visual_result.get("fields_matched") or []
    missing = visual_result.get("fields_missing") or []
    type_exact = type_s is not None and type_s >= 1.0
    type_related = note.startswith("type-related")
    # Asserted wrong family (both sides typed, families differ) is a clear visual-fidelity loss.
    if type_s == 0.0:
        return STATE_DEGRADED, "wrong chart type: %s" % (note or "family mismatch")
    # A paired visual that carries NONE of the source fields is degraded regardless of type.
    if not matched:
        return STATE_DEGRADED, "no source field reproduced on the rebuilt visual"
    if type_exact:
        if not missing:
            return STATE_REPRODUCED, "exact chart type; all %d source field(s) present" % len(matched)
        return STATE_PARTIAL, "exact chart type; %d source field(s) missing" % len(missing)
    # Soft type (related family, indeterminate, or unasserted) with matched fields.
    if not missing:
        if type_related:
            return STATE_PARTIAL, "related chart-type substitution (%s); all source fields present" % note
        return STATE_PARTIAL, "%s; all source fields present" % (note or "soft type match")
    return STATE_DEGRADED, "%s with %d source field(s) missing" % (note or "soft type", len(missing))


def _per_visual_record(v):
    """Compact per-visual verdict record built from a structural :func:`_score_pair` result."""
    state, reason = classify_visual_state(v)
    return {
        "worksheet": v.get("worksheet"),
        "visual": v.get("visual"),
        "visual_type": v.get("visual_type"),
        "state": state,
        "reason": reason,
        "score": v.get("score"),
        "band": v.get("band"),
        "type_note": v.get("type_note"),
        "components": v.get("components"),
        "fields_missing": v.get("fields_missing"),
        "fields_extra": v.get("fields_extra"),
        "diagnosis": v.get("diagnosis"),
    }


def _missing_record(name, reason):
    return {"worksheet": name, "visual": None, "visual_type": None, "state": STATE_MISSING,
            "reason": reason, "score": None, "band": None, "type_note": None, "components": None,
            "fields_missing": None, "fields_extra": None, "diagnosis": None}


def per_visual_fidelity(report, on_view=None):
    """Reduce a structural :func:`score_report` result to a per-visual
    REPRODUCED / PARTIAL / DEGRADED / MISSING verdict list -- the migration loop's objective function.

    ``on_view`` (optional) restricts scoring to a set/list of Tableau worksheet names (the
    dashboard's on-view sheets); an on-view worksheet that has no paired visual -- whether it landed
    in ``summary.unmatched_worksheets`` or was never parsed at all -- is still reported MISSING.
    When ``on_view`` is ``None`` every matched visual and every unmatched worksheet is scored.
    Off-view worksheets (and off-view calcs by construction) are ignored.

    Additive + advisory: never mutates ``report`` and never raises. Returns a dict with the per-visual
    ``verdicts``, a four-state ``tally``, and an advisory ``objective_score`` (the credit-weighted mean).
    """
    want = None if on_view is None else {_norm(n): n for n in on_view if n}
    verdicts = []
    seen = set()
    for v in report.get("visuals", []) or []:
        ws = v.get("worksheet")
        if want is not None and _norm(ws) not in want:
            continue
        seen.add(_norm(ws))
        verdicts.append(_per_visual_record(v))
    summary = report.get("summary") or {}
    for ws in summary.get("unmatched_worksheets", []) or []:
        if want is not None and _norm(ws) not in want:
            continue
        seen.add(_norm(ws))
        verdicts.append(_missing_record(ws, "no paired Power BI visual"))
    if want is not None:
        for nk, name in want.items():
            if nk not in seen:
                verdicts.append(_missing_record(name, "worksheet not found in the rebuild"))
    tally = {STATE_REPRODUCED: 0, STATE_PARTIAL: 0, STATE_DEGRADED: 0, STATE_MISSING: 0}
    for r in verdicts:
        tally[r["state"]] = tally.get(r["state"], 0) + 1
    n = len(verdicts)
    objective = round(sum(_STATE_CREDIT[r["state"]] for r in verdicts) / n, 4) if n else None
    verdicts.sort(key=lambda r: (_STATE_CREDIT[r["state"]], r["worksheet"] or ""))
    return {
        "tier": "per-visual",
        "advisory": True,
        "scoped_to": (list(want.values()) if want is not None else "all-matched+unmatched"),
        "scored_visuals": n,
        "tally": tally,
        "objective_score": objective,
        "state_credit": dict(_STATE_CREDIT),
        "verdicts": verdicts,
    }


# =====================================================================================
# Optional Tier-2: DAX-value oracle (live model measure values via local Analysis Services)
# =====================================================================================
# Cross-engine value agreement is tolerance-banded: Tableau and Power BI can round or aggregate
# slightly differently, so a small relative difference is not a defect. A measure that *errors*,
# however, is a concrete fidelity defect the structural tier cannot see -- so evaluability itself
# is a first-class signal here.
DEFAULT_VALUE_TOLERANCE = 0.005  # 0.5% relative tolerance for "values agree"

# Where Power BI Desktop drops its local Analysis Services workspace port files. The Store build
# uses the profile path; the classic installer uses LOCALAPPDATA. Each running model writes a
# ``msmdsrv.port.txt`` (UTF-16) under ``<workspace>\Data``; closed instances leave stale files, so
# discovery is verified by actually connecting.
def _pbi_workspace_roots():
    roots = []
    home = os.path.expanduser("~")
    if home:
        roots.append(os.path.join(home, "Microsoft", "Power BI Desktop Store App",
                                  "AnalysisServicesWorkspaces"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(os.path.join(local, "Microsoft", "Power BI Desktop",
                                  "AnalysisServicesWorkspaces"))
    return roots


def discover_pbi_instances(workspace_roots=None):
    """Find local Power BI Desktop Analysis Services ports from on-disk workspace port files.

    Pure file I/O: returns ``[{host, port, workspace}]`` for every ``msmdsrv.port.txt`` found
    (de-duplicated by port). Stale entries from closed instances may be present; callers verify
    liveness by connecting. Never raises -- an unreadable/odd file is skipped.
    """
    roots = workspace_roots if workspace_roots is not None else _pbi_workspace_roots()
    found, seen = [], set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.lower() != "msmdsrv.port.txt":
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                digits = re.sub(rb"[^0-9]", b"", raw).decode("ascii", "ignore")
                if not digits:
                    continue
                port = int(digits)
                if port in seen:
                    continue
                seen.add(port)
                found.append({"host": "localhost", "port": port, "workspace": dirpath})
    return found


def _adomd_dll_path():
    """Locate the highest-versioned ADOMD.NET client DLL, or ``None`` if it is not installed."""
    candidates = []
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if not base:
            continue
        root = os.path.join(base, "Microsoft.NET", "ADOMD.NET")
        if not os.path.isdir(root):
            continue
        for ver in sorted(os.listdir(root), reverse=True):
            dll = os.path.join(root, ver, "Microsoft.AnalysisServices.AdomdClient.dll")
            if os.path.isfile(dll):
                candidates.append(dll)
    return candidates[0] if candidates else None


def _load_adomd():
    """Lazily load the ADOMD.NET client via pythonnet. Returns the ``AdomdConnection`` type.

    Raises on any missing piece (pythonnet absent, DLL not installed); the caller turns that into
    a structured ``unavailable`` record so importing this module never requires the optional stack.
    """
    import clr  # pythonnet -- optional, host-only
    dll = _adomd_dll_path()
    if dll is None:
        raise RuntimeError("ADOMD.NET client DLL not found")
    import sys as _sys
    dll_dir = os.path.dirname(dll)
    if dll_dir not in _sys.path:
        _sys.path.append(dll_dir)
    try:
        clr.AddReference("Microsoft.AnalysisServices.AdomdClient")
    except Exception:  # noqa: BLE001 -- fall back to an explicit file load
        import System
        System.Reflection.Assembly.LoadFile(dll)
    from Microsoft.AnalysisServices.AdomdClient import AdomdConnection
    return AdomdConnection


def _net_to_py(val):
    """Coerce an ADOMD .NET scalar into a plain Python value (``DBNull`` -> ``None``)."""
    if val is None or type(val).__name__ == "DBNull":
        return None
    if isinstance(val, bool):
        return val
    try:
        return float(val)
    except (TypeError, ValueError):
        return str(val)


def _adomd_rows(conn, query, columns):
    cmd = conn.CreateCommand()
    cmd.CommandText = query
    reader = cmd.ExecuteReader()
    rows = []
    try:
        while reader.Read():
            rows.append({col: _net_to_py(reader.GetValue(i)) for i, col in enumerate(columns)})
    finally:
        reader.Close()
    return rows


def _evaluate_measure(conn, measure_name, filter_expr=None):
    """Evaluate one model measure to a scalar via ``EVALUATE ROW``; capture errors, never raise.

    ``filter_expr`` (optional, caller-supplied DAX) wraps the measure in ``CALCULATE`` so the value
    is evaluated under a specific *view* filter context -- e.g. ``'Orders'[Country] = "United
    States"`` to reproduce a worksheet that is US-filtered while others are not. This is what lets
    the tier catch a per-view filter-scope mismatch that a model-level total would hide.
    """
    safe = str(measure_name).replace("]", "]]")
    if filter_expr:
        dax = 'EVALUATE ROW("v", CALCULATE([%s], %s))' % (safe, filter_expr)
    else:
        dax = 'EVALUATE ROW("v", [%s])' % safe
    try:
        rows = _adomd_rows(conn, dax, ["v"])
        return {"measure": measure_name, "ok": True,
                "value": rows[0]["v"] if rows else None, "error": None}
    except Exception as exc:  # noqa: BLE001 -- a failed evaluation is itself a fidelity signal
        return {"measure": measure_name, "ok": False, "value": None,
                "error": str(exc).strip()[:200]}


def _evaluate_query(conn, dax):
    """Evaluate a caller-supplied scalar DAX query and return its single value; capture errors.

    This is the calc-*column* (or arbitrary-expression) sibling of :func:`_evaluate_measure`. A
    Tableau calculated *column* is not a model measure, so ``EVALUATE ROW("v", [m])`` can't reach
    it; its fidelity is checked by its own scalar DAX instead -- e.g. a distinct *non-blank* count
    of the column's values across the table. The query must return a single row whose first column
    is the scalar of interest (its name is irrelevant -- the reader maps column 0). A failed
    evaluation is itself a fidelity signal, so errors are captured rather than raised.
    """
    try:
        rows = _adomd_rows(conn, dax, ["v"])
        return {"query": dax, "ok": True,
                "value": rows[0]["v"] if rows else None, "error": None}
    except Exception as exc:  # noqa: BLE001 -- a failed evaluation is itself a fidelity signal
        return {"query": dax, "ok": False, "value": None, "error": str(exc).strip()[:200]}


def _normalize_expected(expected):
    """Normalize an ``expected`` map into a list of value checks.

    Supports two shapes (mixable in one map):

    * **flat** ``{measure_name: expected_value}`` -- a model-level check (no filter context).
    * **rich** ``{label: {"measure": name, "expected": value, "filter": dax}}`` -- a per-view check
      whose ``filter`` (caller-supplied DAX) reproduces that view's filter context. ``measure``
      defaults to ``label``; ``value`` is accepted as an alias for ``expected``. A rich entry may
      instead carry ``"query": <scalar DAX>`` to check a calc *column* (or any non-measure scalar)
      by its own ``EVALUATE``; when ``query`` is present it takes precedence over ``measure``/
      ``filter``.

    The rich form is what models "Sales on the US-only map vs Sales on the Canada-inclusive KPIs":
    the same measure, two checks, two filter contexts, two expected values.
    """
    checks = []
    for key, val in (expected or {}).items():
        if isinstance(val, dict):
            checks.append({
                "label": val.get("label") or key,
                "measure": val.get("measure") or key,
                "expected": val.get("expected", val.get("value")),
                "filter": val.get("filter"),
                "query": val.get("query"),
            })
        else:
            checks.append({"label": key, "measure": key, "expected": val,
                           "filter": None, "query": None})
    return checks


def _compare_value(name, expected, actual, tolerance=DEFAULT_VALUE_TOLERANCE):
    """Tolerance-banded comparison of an expected vs live measure value (advisory, never exact)."""
    rec = {"measure": name, "expected": expected, "actual": actual,
           "abs_diff": None, "rel_diff": None, "within_tolerance": False, "note": ""}
    if actual is None or expected is None:
        rec["note"] = "missing value"
        return rec
    try:
        e, a = float(expected), float(actual)
    except (TypeError, ValueError):
        rec["within_tolerance"] = str(expected) == str(actual)
        rec["note"] = "string comparison"
        return rec
    abs_diff = abs(a - e)
    rel = abs_diff / max(abs(e), 1e-12)
    rec["abs_diff"] = abs_diff
    rec["rel_diff"] = rel
    rec["within_tolerance"] = rel <= tolerance or abs_diff <= 1e-9
    rec["note"] = "within tolerance" if rec["within_tolerance"] else "exceeds tolerance"
    return rec


def _score_value_results(results, comparisons):
    """Advisory value score: when expected values are supplied, the fraction that agree within
    tolerance; otherwise the fraction of measures that simply evaluate without error."""
    if comparisons:
        n = len(comparisons)
        return round(sum(1 for c in comparisons if c["within_tolerance"]) / n, 4) if n else None
    n = len(results)
    return round(sum(1 for r in results if r["ok"]) / n, 4) if n else None


_CONNECT_TIMEOUT_SECONDS = 5  # daemon-thread join bound on conn.Open() -- the load-bearing backstop
_NATIVE_CONNECT_TIMEOUT_SECONDS = 2  # connection-string "Connect Timeout=" hint; ADOMD's Open() empirically
# self-terminates at roughly twice this, so keeping it below the join bound above lets the daemon thread
# complete on its own (a clean error) instead of being abandoned mid-native-call -- abandoning many such
# threads can crash pythonnet, so this pairing matters when several stale instances are probed.
_DISCOVERY_BUDGET_SECONDS = 15  # total wall-clock cap on auto-select probing so a host littered with stale
# Power BI Desktop port files (dozens of dead instances) degrades gracefully instead of grinding for minutes


def _open_bounded(conn, seconds=_CONNECT_TIMEOUT_SECONDS):
    """Call ``conn.Open()`` but stop *waiting* after ``seconds`` instead of blocking indefinitely.

    A blocked native ADOMD ``Open()`` cannot be interrupted from Python, so the call runs on a
    daemon thread and we stop waiting (raise ``TimeoutError``) if it has not returned -- the lingering
    daemon dies with the process. This is the load-bearing guard: it lets the value tier degrade
    instead of hanging when Power BI Desktop has a stale/unreachable Analysis Services instance open.
    Any error ``Open()`` raises is re-raised here; on success the (opened) ``conn`` is returned.
    """
    import threading
    box = {}

    def _run():
        try:
            conn.Open()
            box["ok"] = True
        except BaseException as exc:  # noqa: BLE001 -- surfaced to the caller below
            box["err"] = exc

    t = threading.Thread(target=_run, name="adomd-open", daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError("ADOMD Open() exceeded %ss (stale/unreachable instance)" % seconds)
    if "err" in box:
        raise box["err"]
    return conn


def dax_value_tier(report_dir=None, host="localhost", port=None, expected=None,
                   measures=None, tolerance=DEFAULT_VALUE_TOLERANCE, workspace_roots=None):
    """Optional Tier-2: evaluate a live Power BI model's measures and (optionally) compare them to
    expected Tableau values, via a local Analysis Services instance.

    Lazy + guarded: if pythonnet/ADOMD or a live Desktop instance is absent, returns a structured
    ``{available: False, reason}`` record rather than raising. With ``port`` omitted it auto-selects
    when exactly one live instance is found, else reports the candidates. Every measure is evaluated
    (an error is a concrete fidelity defect); ``expected`` adds tolerance-banded value comparison.
    ``report_dir`` is accepted for symmetry/future model matching and is not required.
    """
    try:
        AdomdConnection = _load_adomd()
    except Exception as exc:  # noqa: BLE001
        return {"tier": "dax-value", "available": False,
                "reason": "ADOMD.NET/pythonnet not available: %s" % str(exc).strip()[:160]}

    def _connect(p):
        # Native Connect Timeout hint (kept below the daemon-thread join bound so Open() self-terminates
        # before the bound expires -> the thread completes cleanly instead of being abandoned) plus the
        # load-bearing _open_bounded backstop, so neither discovery nor an explicit-port connect can hang
        # on a stale/unreachable instance.
        conn = AdomdConnection(
            "Data Source=%s:%d;Connect Timeout=%d" % (host, p, _NATIVE_CONNECT_TIMEOUT_SECONDS))
        return _open_bounded(conn)

    chosen = port
    if chosen is None:
        discovered = discover_pbi_instances(workspace_roots)
        live = []
        probed = 0
        budget_exhausted = False
        _start = time.monotonic()
        for inst in discovered:
            if time.monotonic() - _start > _DISCOVERY_BUDGET_SECONDS:
                budget_exhausted = True  # too many stale instances -> stop probing, degrade gracefully
                break
            probed += 1
            try:
                c = _connect(inst["port"]); c.Close(); live.append(inst)
            except Exception:  # noqa: BLE001 -- stale/closed instance
                continue
        if len(live) == 1:
            chosen = live[0]["port"]
        elif not live:
            reason = "no live Power BI Desktop Analysis Services instance found"
            if budget_exhausted:
                reason = ("discovery time budget (%ss) exceeded after probing %d of %d instances; "
                          "pass an explicit port"
                          % (_DISCOVERY_BUDGET_SECONDS, probed, len(discovered)))
            return {"tier": "dax-value", "available": False, "reason": reason,
                    "discovered_ports": [i["port"] for i in discovered]}
        else:
            return {"tier": "dax-value", "available": False,
                    "reason": "multiple live instances found; pass an explicit port",
                    "live_ports": [i["port"] for i in live]}

    try:
        conn = _connect(chosen)
    except Exception as exc:  # noqa: BLE001
        return {"tier": "dax-value", "available": False,
                "reason": "connect failed on port %s: %s" % (chosen, str(exc).strip()[:160])}
    try:
        cats = _adomd_rows(conn, "SELECT [CATALOG_NAME] FROM $SYSTEM.DBSCHEMA_CATALOGS",
                           ["CATALOG_NAME"])
        catalog = cats[0]["CATALOG_NAME"] if cats else None
        model_measures, seen = [], set()
        for r in _adomd_rows(
                conn,
                "SELECT [MEASUREGROUP_NAME], [MEASURE_NAME], [MEASURE_IS_VISIBLE] "
                "FROM $SYSTEM.MDSCHEMA_MEASURES",
                ["MEASUREGROUP_NAME", "MEASURE_NAME", "MEASURE_IS_VISIBLE"]):
            m = r["MEASURE_NAME"]
            # Skip Analysis Services internal/system measures (e.g. ``__Default measure``) and any
            # explicitly hidden measure -- neither is an author-facing fidelity signal.
            if not m or str(m).startswith("__") or r["MEASURE_IS_VISIBLE"] in (False, 0, 0.0):
                continue
            if m not in seen:
                seen.add(m)
                model_measures.append(m)
        target = list(measures) if measures else model_measures
        results = [_evaluate_measure(conn, m) for m in target]
        comparisons = []
        if expected:
            for chk in _normalize_expected(expected):
                if chk.get("query"):
                    ev = _evaluate_query(conn, chk["query"])
                else:
                    ev = _evaluate_measure(conn, chk["measure"], chk.get("filter"))
                actual = ev["value"] if ev["ok"] else None
                cmp = _compare_value(chk["label"], chk["expected"], actual, tolerance)
                if not chk.get("query") and chk["measure"] != chk["label"]:
                    cmp["measure_name"] = chk["measure"]
                if chk.get("filter"):
                    cmp["filter"] = chk["filter"]
                if chk.get("query"):
                    cmp["query"] = chk["query"]
                if not ev["ok"]:
                    cmp["note"] = "evaluation error: %s" % ((ev["error"] or "")[:160])
                comparisons.append(cmp)
    finally:
        conn.Close()

    value_score = _score_value_results(results, comparisons)
    n = len(results)
    n_ok = sum(1 for r in results if r["ok"])
    return {
        "tier": "dax-value",
        "available": True,
        "instance": {"host": host, "port": chosen, "catalog": catalog},
        "measures_total": n,
        "measures_evaluated": n_ok,
        "measures_errored": n - n_ok,
        "results": results,
        "comparisons": comparisons,
        "value_score": value_score,
        "band": _band(value_score) if value_score is not None else None,
        "tolerance": tolerance,
        "report_dir": os.path.abspath(report_dir) if report_dir else None,
        "notes": [
            "ADVISORY: a measure that errors is a concrete fidelity defect; value comparisons use "
            "a relative-tolerance band, not equality.",
            "value_score = fraction of expected values that agree within tolerance, or (without "
            "expected values) the fraction of measures that evaluate without error.",
            "expected values may carry a per-view 'filter' (DAX) so a measure is checked under that "
            "view's filter context -- e.g. a US-only map vs Canada-inclusive KPIs on the same model.",
            "an expected entry may instead carry 'query' (scalar DAX) to check a calc COLUMN or any "
            "non-measure scalar by its own EVALUATE -- e.g. a distinct non-blank count of a column.",
        ],
    }


# =====================================================================================
# Optional Tier-3: image oracle (tolerance-banded perceptual similarity of two PNGs)
# =====================================================================================
# Bands for cross-engine perceptual similarity. Literal pixel-equality across two rendering engines
# is impossible, so this is explicitly a BAND, never pass/fail.
IMAGE_BANDS = ((0.95, "near-identical"), (0.85, "strong"), (0.65, "moderate"), (0.0, "divergent"))

# Advisory acceptance floor for a faithful cross-engine rebuild. Calibrated against a real
# Tableau-vs-Power-BI pair: a hand-built rebuild that diverged on mark type (area->line), sort,
# basemap, and a dropped filter scored ~0.64-0.65, so a genuinely faithful rebuild should clear
# this. Configurable per run; still advisory (a target, not a hard pass/fail gate).
DEFAULT_ACCEPTANCE_SSIM = 0.80


def _image_band(score):
    for threshold, label in IMAGE_BANDS:
        if score >= threshold:
            return label
    return IMAGE_BANDS[-1][1]


def _box_mean(np, img, k):
    """Mean over each ``k x k`` window via an integral image (numpy-only, no scipy)."""
    ii = np.cumsum(np.cumsum(img, axis=0), axis=1)
    ii = np.pad(ii, ((1, 0), (1, 0)), mode="constant")
    total = ii[k:, k:] - ii[:-k, k:] - ii[k:, :-k] + ii[:-k, :-k]
    return total / float(k * k)


def _ssim(np, a, b, k=7):
    """Windowed structural similarity (SSIM) mean over ``k x k`` windows. Inputs are 2-D grayscale
    arrays of identical shape; returns a scalar in roughly ``[-1, 1]`` (1.0 == identical)."""
    a = a.astype("float64")
    b = b.astype("float64")
    k = min(k, a.shape[0], a.shape[1])
    if k < 1:
        return 0.0
    mu_a = _box_mean(np, a, k)
    mu_b = _box_mean(np, b, k)
    va = _box_mean(np, a * a, k) - mu_a ** 2
    vb = _box_mean(np, b * b, k) - mu_b ** 2
    cov = _box_mean(np, a * b, k) - mu_a * mu_b
    L = 255.0
    c1 = (0.01 * L) ** 2
    c2 = (0.03 * L) ** 2
    smap = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / \
           ((mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2))
    return float(smap.mean())


def _load_gray(np, Image, path, shape=None):
    im = Image.open(path).convert("L")
    if shape is not None:
        im = im.resize((shape[1], shape[0]))  # PIL size is (width, height)
    return np.asarray(im)


def _crop_fractional(pil_im, box):
    """Crop a PIL image by a fractional ``(x0, y0, x1, y1)`` box (each in ``[0, 1]``)."""
    w, h = pil_im.size
    x0, y0, x1, y1 = box
    x0 = min(max(x0, 0.0), 1.0)
    y0 = min(max(y0, 0.0), 1.0)
    x1 = min(max(x1, 0.0), 1.0)
    y1 = min(max(y1, 0.0), 1.0)
    px0, py0 = int(x0 * w), int(y0 * h)
    px1, py1 = max(px0 + 1, int(x1 * w)), max(py0 + 1, int(y1 * h))
    return pil_im.crop((px0, py0, px1, py1))


def _score_regions(np, Image, reference_png, candidate_png, regions, threshold):
    """Per-zone SSIM for a list of fractional crop regions.

    Each region is ``{"name", "ref": (x0,y0,x1,y1)[, "cand": (x0,y0,x1,y1)]}`` with fractional
    boxes; ``cand`` defaults to ``ref``. This localizes *where* two composite renders (e.g. a
    multi-worksheet dashboard) agree or diverge, rather than collapsing everything into one number.
    """
    ref_pil = Image.open(reference_png).convert("L")
    cand_pil = Image.open(candidate_png).convert("L")
    out = []
    for reg in regions or []:
        rbox = reg.get("ref") or reg.get("box")
        if not rbox:
            continue
        cbox = reg.get("cand") or rbox
        rc = _crop_fractional(ref_pil, rbox)
        cc = _crop_fractional(cand_pil, cbox).resize(rc.size)
        a = np.asarray(rc, dtype="float64")
        b = np.asarray(cc, dtype="float64")
        s = _ssim(np, a, b)
        out.append({"name": reg.get("name") or "region", "ssim": round(s, 4),
                    "band": _image_band(s), "meets_target": bool(s >= threshold)})
    return out


def image_tier(reference_png=None, candidate_png=None, acceptance_threshold=None, regions=None):
    """Optional Tier-3: tolerance-banded perceptual (SSIM) similarity of a Tableau reference PNG and
    a Power BI render PNG.

    Lazy + guarded (numpy + Pillow): returns ``{available: False, reason}`` when the deps or the
    files are missing. The candidate is resized to the reference's shape before comparison. The
    result is a similarity *band*, framed explicitly as advisory -- never a pixel-equality pass/fail.

    ``acceptance_threshold`` is the advisory SSIM floor a faithful rebuild is expected to clear
    (default :data:`DEFAULT_ACCEPTANCE_SSIM`); the result reports ``meets_target`` against it.

    ``regions`` (optional) is a list of fractional crop boxes (see :func:`_score_regions`); when
    given, a per-zone SSIM breakdown + ``regions_mean_ssim`` is attached, which localizes divergence
    in a composite render far better than a single whole-image number.
    """
    threshold = DEFAULT_ACCEPTANCE_SSIM if acceptance_threshold is None else float(acceptance_threshold)
    if not reference_png or not candidate_png:
        return {"tier": "image", "available": False,
                "reason": "two PNG paths required (reference_png, candidate_png)"}
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        return {"tier": "image", "available": False,
                "reason": "numpy/Pillow not available: %s" % str(exc).strip()[:160]}
    for p in (reference_png, candidate_png):
        if not os.path.isfile(p):
            return {"tier": "image", "available": False, "reason": "file not found: %s" % p}
    ref = _load_gray(np, Image, reference_png)
    cand = _load_gray(np, Image, candidate_png, shape=ref.shape)
    score = _ssim(np, ref, cand)
    result = {
        "tier": "image",
        "available": True,
        "ssim": round(score, 4),
        "band": _image_band(score),
        "acceptance_threshold": round(threshold, 4),
        "meets_target": bool(score >= threshold),
        "reference_shape": [int(ref.shape[0]), int(ref.shape[1])],
        "notes": [
            "ADVISORY: cross-engine pixel-equality is impossible; this is a tolerance BAND of "
            "perceptual (SSIM) similarity, not pass/fail.",
            "The candidate render is resized to the reference's pixel shape before comparison.",
            "meets_target compares SSIM against the advisory acceptance floor (%.2f); a faithful "
            "rebuild is expected to clear it." % threshold,
        ],
    }
    if regions:
        zone_scores = _score_regions(np, Image, reference_png, candidate_png, regions, threshold)
        if zone_scores:
            result["regions"] = zone_scores
            result["regions_mean_ssim"] = round(
                sum(z["ssim"] for z in zone_scores) / len(zone_scores), 4)
    return result


# =====================================================================================
# Optional Tier-3 host bridge: local Power BI Desktop render + first-party PBIR validation
# =====================================================================================
# Two deterministic, first-party Microsoft CLIs un-park the parts of the oracle that need a real
# Power BI *render* (or an authoritative schema check) rather than just the emitted JSON -- and they
# run locally/offline, so the oracle stays a standalone advisory tool:
#
#   * ``@microsoft/powerbi-desktop-bridge-cli`` (``powerbi-desktop``) drives a running Power BI
#     Desktop over its secure local bridge: ``open`` a .pbip, read ``status`` (the running instances
#     + their PIDs/report dirs), and capture a per-PAGE ``screenshot``/``screenshot-all``. That page
#     PNG is the Power BI half the image tier was missing; cropping it by the per-visual PBIR px this
#     module already computes (see :func:`regions_from_layout`) yields a per-zone SSIM with no
#     hand-tuned crop boxes.
#   * ``@microsoft/powerbi-report-authoring-cli`` (``powerbi-report-author``) ``validate``s a
#     .pbip/.Report directory against the first-party PBIR schema -- an independent structural
#     pre-gate that catches schema drift a hand-rolled parser cannot self-detect.
#
# Both are wrapped exactly like the DAX/image tiers: located on PATH, run in a child process with a
# timeout, parsed defensively (CLI JSON shapes are tolerated, not assumed), and degraded to
# ``{available: False, reason}`` when absent or erroring. They are ADVISORY add-ons -- never invoked
# at import, never able to raise, and never fed into the structural aggregate -- so they cannot move
# calibration or break the engine gate.
DESKTOP_BRIDGE_CMD = "powerbi-desktop"
DESKTOP_BRIDGE_PKG = "@microsoft/powerbi-desktop-bridge-cli"
REPORT_AUTHOR_CMD = "powerbi-report-author"
REPORT_AUTHOR_PKG = "@microsoft/powerbi-report-authoring-cli"
DEFAULT_CLI_TIMEOUT = 180        # seconds; a reload/render on a large model can be slow
DEFAULT_SCREENSHOT_SCALE = 2     # the bridge's own default: readable without huge PNGs
_MAX_DIAGNOSTICS = 50            # cap stored validation diagnostics so a report stays compact


def _locate_cli(cmd, explicit=None):
    """Resolve a Node CLI executable: an explicit path/name, else ``cmd`` on PATH, else its Windows
    shim (``npm`` global bins are ``.cmd`` on Windows). Returns the resolved path or None -- never
    raises, so a missing CLI is just an ``unavailable`` tier."""
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        return shutil.which(explicit) or None
    found = shutil.which(cmd)
    if found:
        return found
    for ext in (".cmd", ".exe", ".bat"):
        found = shutil.which(cmd + ext)
        if found:
            return found
    return None


def _run_cli(exe, args, timeout=DEFAULT_CLI_TIMEOUT, cwd=None):
    """Run ``exe`` with ``args`` in a child process, capturing output. Never raises: returns
    ``{ran, rc, stdout, stderr}`` on completion, or ``{ran: False, reason}`` when the process could
    not start or timed out. This is the single choke point the host-bridge tiers go through, so a
    missing/slow CLI degrades gracefully instead of crashing the advisory run."""
    try:
        proc = subprocess.run([exe] + [str(a) for a in args], capture_output=True,
                              text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return {"ran": False, "reason": "timed out after %ss" % timeout}
    except (OSError, ValueError) as exc:  # noqa: BLE001
        return {"ran": False, "reason": str(exc).strip()[:160]}
    return {"ran": True, "rc": proc.returncode,
            "stdout": proc.stdout or "", "stderr": proc.stderr or ""}


def _parse_json_loose(text):
    """Best-effort JSON parse of CLI output: try the whole string, then the widest ``{...}`` or
    ``[...]`` slice (CLIs sometimes prefix a log line before the JSON payload). Returns the parsed
    object or None -- strict parsing alone is too brittle for an advisory wrapper."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    for op, cl in (("{", "}"), ("[", "]")):
        i, j = text.find(op), text.rfind(cl)
        if 0 <= i < j:
            try:
                return json.loads(text[i:j + 1])
            except (ValueError, TypeError):
                continue
    return None


def _safe_unlink(path):
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _safe_name(text):
    """Filesystem-safe slug for a page id used as a screenshot filename."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)) or "page"


# -- first-party PBIR validation (powerbi-report-author validate) --------------------------------
def _collect_diagnostics(payload):
    """Pull a flat, normalized diagnostics list out of whatever shape the validator emits.

    Tolerant by design: accepts a top-level list, a ``{diagnostics|issues|...: [...]}`` dict, nested
    ``results[].diagnostics``, or split ``{errors: [...], warnings: [...]}`` -- normalizing each
    entry to ``{severity, message, file, json_path}``. Unknown shapes yield ``[]`` rather than
    raising. (Field names per the CLI's documented diagnostics: severity + file path + JSON path.)
    """
    out = []

    def _norm_one(item, default_sev=None):
        if isinstance(item, str):
            return {"severity": default_sev or "error", "message": item[:300],
                    "file": None, "json_path": None}
        if not isinstance(item, dict):
            return None
        raw_sev = (item.get("severity") or item.get("level") or item.get("type")
                   or default_sev or "error")
        sev = str(raw_sev).strip().lower()
        if sev in ("err", "fatal", "critical"):
            sev = "error"
        elif sev == "warn":
            sev = "warning"
        elif sev not in ("error", "warning", "info"):
            sev = default_sev or "error"
        msg = (item.get("message") or item.get("text") or item.get("description")
               or item.get("rule") or item.get("code") or "")
        f = item.get("file") or item.get("filePath") or item.get("fileName")
        jp = (item.get("jsonPath") or item.get("json_path") or item.get("path")
              or item.get("pointer"))
        return {"severity": sev, "message": str(msg)[:300],
                "file": str(f) if f else None, "json_path": str(jp) if jp else None}

    if isinstance(payload, list):
        for it in payload:
            n = _norm_one(it)
            if n:
                out.append(n)
        return out
    if isinstance(payload, dict):
        for key in ("diagnostics", "issues", "problems", "messages", "results"):
            seq = payload.get(key)
            if not isinstance(seq, list):
                continue
            for it in seq:
                if isinstance(it, dict) and isinstance(it.get("diagnostics"), list):
                    for d in it["diagnostics"]:
                        n = _norm_one(d)
                        if n:
                            out.append(n)
                else:
                    n = _norm_one(it)
                    if n:
                        out.append(n)
        for key, sev in (("errors", "error"), ("warnings", "warning")):
            seq = payload.get(key)
            if isinstance(seq, list):
                for it in seq:
                    n = _norm_one(it, default_sev=sev)
                    if n:
                        out.append(n)
    return out


def _shape_validation(payload, res):
    """Normalize the parsed validator output (+ raw run result) into the advisory record."""
    rc = res.get("rc")
    diags = _collect_diagnostics(payload)
    errors = [d for d in diags if d["severity"] == "error"]
    warnings = [d for d in diags if d["severity"] == "warning"]
    valid = None
    if isinstance(payload, dict):
        for key in ("valid", "isValid", "success", "passed", "ok"):
            if isinstance(payload.get(key), bool):
                valid = payload[key]
                break
    if valid is None:
        if errors:
            valid = False
        elif rc is not None:
            valid = (rc == 0)
    record = {
        "tier": "pbir_validation",
        "available": True,
        "valid": valid,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "diagnostics": diags[:_MAX_DIAGNOSTICS],
        "diagnostics_truncated": len(diags) > _MAX_DIAGNOSTICS,
        "exit_code": rc,
        "advisory": True,
        "notes": [
            "ADDITIVE first-party PBIR schema validation (powerbi-report-author): an independent "
            "structural pre-gate, never folded into the fidelity aggregate and never a verdict on "
            "the rebuild's faithfulness.",
            "A validation ERROR is a concrete, fix-before-ship defect (malformed PBIR); warnings "
            "(e.g. an unknown visual type) usually mean a typo unless a custom visual is intended.",
        ],
    }
    if not diags and payload is None:
        record["notes"].append(
            "CLI produced no parseable diagnostics; validity was inferred from the exit code.")
    return record


def validate_pbir(report_path, cli=None, timeout=DEFAULT_CLI_TIMEOUT):
    """Optional first-party PBIR validation pre-gate via ``powerbi-report-author validate``.

    Runs Microsoft's own report-authoring CLI over a ``.pbip`` or ``.Report`` directory and returns
    ``{available, valid, error_count, warning_count, diagnostics, ...}``. ADDITIVE: it confirms the
    emitted PBIR is well-formed against the authoritative schema and surfaces drift this module's
    hand-rolled reader cannot self-detect. It is NOT blended into the structural aggregate -- a
    separate boolean/diagnostics signal beside type/position/fields -- so it never moves calibration.
    Lazy + guarded: ``{available: False, reason}`` when the CLI is absent or the run fails, exactly
    like the DAX/image tiers."""
    exe = _locate_cli(REPORT_AUTHOR_CMD, explicit=cli)
    if not exe:
        return {"tier": "pbir_validation", "available": False,
                "reason": "%s CLI not found on PATH (npm i -g %s)" % (
                    REPORT_AUTHOR_CMD, REPORT_AUTHOR_PKG)}
    if not report_path or not os.path.exists(report_path):
        return {"tier": "pbir_validation", "available": False,
                "reason": "path not found: %s" % report_path}
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="pbir_validate_")
        os.close(fd)
    except OSError:
        tmp = None
    args = ["validate", report_path]
    if tmp:
        args += ["--out", tmp]
    res = _run_cli(exe, args, timeout=timeout)
    if not res.get("ran"):
        _safe_unlink(tmp)
        return {"tier": "pbir_validation", "available": False,
                "reason": "validate did not run: %s" % res.get("reason")}
    payload = None
    if tmp and os.path.isfile(tmp):
        try:
            with open(tmp, "r", encoding="utf-8-sig") as fh:
                payload = _parse_json_loose(fh.read())
        except OSError:
            payload = None
    _safe_unlink(tmp)
    if payload is None:
        payload = _parse_json_loose(res.get("stdout"))
    return _shape_validation(payload, res)


# -- local Power BI Desktop render bridge (powerbi-desktop) ---------------------------------------
def _shape_instances(payload):
    """Normalize ``powerbi-desktop status`` output to ``[{pid, bridge_status, current_file,
    report_dir}]``. Tolerates a top-level ``instances`` list or a bare list; skips odd entries."""
    raw = []
    if isinstance(payload, dict):
        for key in ("instances", "desktops", "processes"):
            if isinstance(payload.get(key), list):
                raw = payload[key]
                break
    elif isinstance(payload, list):
        raw = payload
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        pid = it.get("pid") or it.get("processId") or it.get("PID")
        try:
            pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid = None
        out.append({
            "pid": pid,
            "bridge_status": it.get("bridgeStatus") or it.get("status"),
            "current_file": (it.get("currentFilePath") or it.get("currentFile")
                             or it.get("filePath")),
            "report_dir": it.get("reportDir") or it.get("reportDirectory"),
        })
    return out


def discover_bridge_instances(cli=None, timeout=DEFAULT_CLI_TIMEOUT):
    """Read the running Power BI Desktop bridge instances via ``powerbi-desktop status``.

    Returns ``{available, status, instances, reason}`` -- each instance carries ``pid``,
    ``bridge_status``, ``current_file`` and ``report_dir`` when the CLI exposes them. Pure discovery
    (safe to run concurrently); guarded so a missing CLI or a stopped Desktop degrades to
    ``available: False`` rather than raising."""
    exe = _locate_cli(DESKTOP_BRIDGE_CMD, explicit=cli)
    if not exe:
        return {"available": False, "instances": [],
                "reason": "%s CLI not found on PATH (npm i -g %s)" % (
                    DESKTOP_BRIDGE_CMD, DESKTOP_BRIDGE_PKG)}
    res = _run_cli(exe, ["status"], timeout=timeout)
    if not res.get("ran"):
        return {"available": False, "instances": [],
                "reason": "status did not run: %s" % res.get("reason")}
    payload = _parse_json_loose(res.get("stdout"))
    instances = _shape_instances(payload)
    status = payload.get("status") if isinstance(payload, dict) else None
    return {"available": True, "status": status, "instances": instances,
            "reason": (None if instances
                       else "no bridge instances reported (Desktop not connected?)")}


def _select_bridge_pid(instances, pbip_path=None, pid=None):
    """Choose the bridge PID to drive. An explicit ``pid`` wins; else match an instance whose
    ``report_dir``/``current_file`` resolves to ``pbip_path``; else the sole instance. Returns
    ``(pid, reason)`` -- reason is set only when no unambiguous choice exists."""
    live = [i for i in instances if i.get("pid")]
    if pid is not None:
        return pid, None  # trust an explicit caller even if status did not enumerate it
    if pbip_path:
        targets = set()
        for cand in (pbip_path, _resolve_report_dir(pbip_path), os.path.dirname(pbip_path)):
            if cand:
                targets.add(os.path.normcase(os.path.abspath(cand)))
        for i in live:
            for cand in (i.get("current_file"), i.get("report_dir")):
                if cand and os.path.normcase(os.path.abspath(cand)) in targets:
                    return i["pid"], None
    if len(live) == 1:
        return live[0]["pid"], None
    if not live:
        return None, "no running bridge instance"
    return None, "multiple bridge instances; pass an explicit pid"


def _collect_pngs(output_dir):
    """List PNGs in a screenshot output dir as ``[{page_id, png}]`` (page id = filename stem)."""
    out = []
    try:
        names = sorted(os.listdir(output_dir))
    except OSError:
        return out
    for fn in names:
        if fn.lower().endswith(".png"):
            out.append({"page_id": os.path.splitext(fn)[0],
                        "png": os.path.join(output_dir, fn)})
    return out


def _pick_render_page(pages, page_id=None):
    """Pick the rendered page PNG to use as the image-tier candidate (a named id, else the first)."""
    if not pages:
        return None
    if page_id:
        want = _safe_name(page_id)
        for p in pages:
            if p.get("page_id") in (page_id, want):
                return p
    return pages[0]


def render_pbi_report(pbip_path, output_dir=None, scale=DEFAULT_SCREENSHOT_SCALE,
                      page_ids=None, pid=None, cli=None, timeout=DEFAULT_CLI_TIMEOUT,
                      open_first=True):
    """Render a .pbip's pages to PNGs via the Power BI Desktop bridge -- the Power BI half of the
    image tier, captured locally and deterministically.

    Drives ``powerbi-desktop``: optionally ``open`` the .pbip, read ``status`` to choose the PID
    (explicit ``pid`` > a report-dir match > the sole instance), then ``screenshot-all`` (or one
    ``screenshot`` per id in ``page_ids``) into ``output_dir``. Captures run SERIALLY per PID (the
    bridge cancels parallel reload/screenshot). Returns ``{available, pid, output_dir, pages:
    [{page_id, png}], scale, reason}``; guarded end-to-end so an absent CLI, an unenabled bridge, or
    a stopped Desktop degrades to ``available: False``.

    NOTE: screenshots are per PAGE -- the bridge supplies no per-visual capture, and none is needed:
    a per-zone breakdown comes from cropping each page PNG by the per-visual PBIR px this module
    already derives (:func:`regions_from_layout`)."""
    exe = _locate_cli(DESKTOP_BRIDGE_CMD, explicit=cli)
    if not exe:
        return {"available": False, "pages": [],
                "reason": "%s CLI not found on PATH (npm i -g %s)" % (
                    DESKTOP_BRIDGE_CMD, DESKTOP_BRIDGE_PKG)}
    if not pbip_path or not os.path.exists(pbip_path):
        return {"available": False, "pages": [], "reason": "pbip path not found: %s" % pbip_path}
    if open_first:
        _run_cli(exe, ["open", pbip_path], timeout=timeout)  # best effort; status is authoritative
    disc = discover_bridge_instances(cli=exe, timeout=timeout)
    if not disc.get("available") or not disc.get("instances"):
        return {"available": False, "pages": [],
                "reason": disc.get("reason") or "no bridge instances"}
    sel, reason = _select_bridge_pid(disc["instances"], pbip_path=pbip_path, pid=pid)
    if sel is None:
        return {"available": False, "pages": [], "reason": reason,
                "instances": disc["instances"]}
    if output_dir:
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as exc:
            return {"available": False, "pages": [],
                    "reason": "cannot create output dir: %s" % exc}
    else:
        try:
            output_dir = tempfile.mkdtemp(prefix="pbi_render_")
        except OSError as exc:
            return {"available": False, "pages": [], "reason": "no output dir: %s" % exc}
    scale_args = ["--scale", str(int(scale))] if scale else []
    captured_reason = None
    if page_ids:
        for pidx in page_ids:
            out_png = os.path.join(output_dir, "%s.png" % _safe_name(pidx))
            r = _run_cli(exe, ["screenshot", str(pidx), "--pid", str(sel),
                               "--output", out_png] + scale_args, timeout=timeout)
            if not r.get("ran") and captured_reason is None:
                captured_reason = r.get("reason")
    else:
        r = _run_cli(exe, ["screenshot-all", "--pid", str(sel),
                           "--output-dir", output_dir] + scale_args, timeout=timeout)
        if not r.get("ran"):
            captured_reason = r.get("reason")
    pages = _collect_pngs(output_dir)
    return {
        "available": bool(pages),
        "pid": sel,
        "output_dir": output_dir,
        "pages": pages,
        "scale": int(scale) if scale else None,
        "advisory": True,
        "reason": None if pages else (captured_reason or "no PNGs were produced"),
    }


# =====================================================================================
# Gate 0: openability (does the emitted semantic model actually deserialize?)
# =====================================================================================
# The structural tier grades whether the rebuild bound the right fields onto the right chart types --
# but a report can score well and still be DEAD ON ARRIVAL if its semantic model does not parse. This
# gate runs the authoritative TMDL deserializer (the Tabular Object Model's ``TmdlSerializer``) over
# the emitted ``*.SemanticModel/definition`` folder: the SAME grammar Power BI Desktop loads on open
# and the SAME grammar Microsoft Fabric ingests when a PBIP is published (Git integration / a
# deployment pipeline). A structural/syntactic defect that fails this offline parse fails BOTH targets
# identically, so this is a cheap pre-flight that predicts a clean open/deploy with no Desktop, no
# Fabric capacity, no credentials, and no round-trip.
#
# Lazy + guarded exactly like the DAX tier: pythonnet + the TOM assemblies are imported only when the
# gate runs, and any absence degrades to ``{available: False, reason}`` -- importing this module never
# needs .NET. What it validates is the model DEFINITION (syntax/structure: the class of defect that
# bites today -- e.g. a multi-line measure body emitted at column 0, or an empty-valued annotation);
# data binding / refresh / credential validity is target-specific and belongs to the value tier.
TABULAR_EDITOR_DLLS = (
    "Microsoft.AnalysisServices.Core.dll",
    "Microsoft.AnalysisServices.dll",
    "Microsoft.AnalysisServices.Tabular.Json.dll",
    "Microsoft.AnalysisServices.Tabular.dll",
)


def _tabular_editor_dirs():
    """Candidate folders that ship the TOM (AMO/Tabular) assemblies, in search order.

    A ``TABULAR_EDITOR_DIR`` env override wins; then the common Tabular Editor 2/3 install paths.
    Tabular Editor is the most reliable local carrier of a loadable, file-based
    ``Microsoft.AnalysisServices.Tabular.dll`` (the GAC copy is awkward to bind via pythonnet)."""
    dirs = []
    env = os.environ.get("TABULAR_EDITOR_DIR")
    if env:
        dirs.append(env)
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    dirs.append(os.path.join(pf86, "Tabular Editor"))
    dirs.append(os.path.join(pf, "Tabular Editor 3"))
    dirs.append(os.path.join(pf86, "Tabular Editor 3"))
    return [d for d in dirs if d]


def _load_tmdl_serializer(tabular_editor_dir=None):
    """Lazy-load the TOM ``TmdlSerializer`` type via pythonnet + the Tabular Editor assemblies.

    Raises (never returns ``None``) when pythonnet or the assemblies are absent, so the caller can
    map any failure to ``{available: False, reason}`` -- mirrors :func:`_load_adomd`."""
    from pythonnet import load as _pythonnet_load
    _pythonnet_load("netfx")
    import clr  # noqa: F401  -- registers the .NET runtime hook for the Assembly import below
    from System.Reflection import Assembly

    search = [tabular_editor_dir] if tabular_editor_dir else _tabular_editor_dirs()
    chosen = None
    for d in search:
        if d and os.path.isfile(os.path.join(d, TABULAR_EDITOR_DLLS[-1])):
            chosen = d
            break
    if chosen is None:
        raise RuntimeError(
            "TOM assemblies not found (looked in: %s); set TABULAR_EDITOR_DIR or install Tabular "
            "Editor" % ", ".join(d for d in search if d))
    for dll in TABULAR_EDITOR_DLLS:
        path = os.path.join(chosen, dll)
        if os.path.isfile(path):
            Assembly.LoadFrom(path)
    from Microsoft.AnalysisServices.Tabular import TmdlSerializer
    return TmdlSerializer


def _resolve_model_definition(report_dir=None, model_dir=None):
    """Locate the ``*.SemanticModel/definition`` folder TMDL is deserialized from.

    ``model_dir`` may be the ``definition`` folder itself, the ``.SemanticModel`` folder, or a parent
    holding one; ``report_dir`` is the ``.Report`` folder (or any parent of the project) whose sibling
    ``*.SemanticModel`` is then found. Returns the absolute ``definition`` path, or ``None``."""
    def _defn_under(root):
        if not root or not os.path.isdir(root):
            return None
        base = os.path.basename(os.path.normpath(root))
        if base.lower() == "definition":
            return os.path.abspath(root)
        if root.lower().endswith(".semanticmodel"):
            direct = os.path.join(root, "definition")
            if os.path.isdir(direct):
                return os.path.abspath(direct)
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            return None
        for name in entries:
            if name.lower().endswith(".semanticmodel"):
                cand = os.path.join(root, name, "definition")
                if os.path.isdir(cand):
                    return os.path.abspath(cand)
        return None

    for start in (model_dir, report_dir):
        if not start:
            continue
        hit = _defn_under(start)
        if hit:
            return hit
        hit = _defn_under(os.path.dirname(os.path.normpath(start)))
        if hit:
            return hit
    return None


def _tom_model_stats(db):
    """Table / measure / relationship counts from a deserialized TOM database (best-effort)."""
    model = getattr(db, "Model", None)
    tables = list(getattr(model, "Tables", []) or [])
    measures = 0
    for t in tables:
        try:
            measures += int(t.Measures.Count)
        except Exception:  # noqa: BLE001 -- tolerate a fake/odd shape
            try:
                measures += len(list(t.Measures))
            except Exception:  # noqa: BLE001
                pass
    rels = None
    try:
        rels = int(model.Relationships.Count)
    except Exception:  # noqa: BLE001
        try:
            rels = len(list(model.Relationships))
        except Exception:  # noqa: BLE001
            rels = None
    return {"tables": len(tables), "measures": measures, "relationships": rels}


_TMDL_ERR_LINE_RE = re.compile(r"line(?:\s+number)?\s*[-:]?\s*(\d+)", re.IGNORECASE)
_TMDL_ERR_DOC_RE = re.compile(r"Document\s*[-:]?\s*'([^']+)'", re.IGNORECASE)
_TMDL_ERR_FILE_RE = re.compile(r"([^\s'\"]+\.tmdl)(?![.\w])", re.IGNORECASE)


def _parse_tmdl_error_location(message):
    """Best-effort ``(file, line)`` from a TOM deserialize exception string (either may be ``None``).

    Handles the real TOM ``TmdlSerializer`` shape -- ``Document - './tables/_Measures'`` +
    ``Line Number - 107`` -- as well as a terser ``'_Measures.tmdl' at line 5``. The ``Document``
    capture is preferred so the file is taken from the error payload, never from a dotted stack-trace
    type (e.g. ``...Tabular.Tmdl.TmdlParser``), and the ``.tmdl`` fallback is anchored so it cannot
    latch onto such a type."""
    if not message:
        return None, None
    dm = _TMDL_ERR_DOC_RE.search(message)
    if dm:
        file_ = dm.group(1).strip()
        if file_.startswith("./"):
            file_ = file_[2:]
    else:
        fm = _TMDL_ERR_FILE_RE.search(message)
        file_ = fm.group(1) if fm else None
    lm = _TMDL_ERR_LINE_RE.search(message)
    return file_, (int(lm.group(1)) if lm else None)


def openability_tier(report_dir=None, model_dir=None, tabular_editor_dir=None):
    """Gate 0: does the emitted semantic model deserialize under the authoritative TMDL parser?

    Runs the TOM ``TmdlSerializer.DeserializeDatabaseFromFolder`` over the emitted
    ``*.SemanticModel/definition`` -- the same grammar Power BI Desktop loads and Microsoft Fabric
    ingests on publish, so a structural defect that fails here fails BOTH. Returns one of:

    * ``{available: True, openable: True, tables, measures, relationships}`` -- OPENS;
    * ``{available: True, openable: False, error, error_file, error_line}`` -- BLOCKED (a concrete,
      fix-before-ship defect, not a fidelity judgment);
    * ``{available: False, reason}`` -- the check could not run (no model folder, or pythonnet/TOM
      assemblies absent). Lazy + guarded: importing this module never needs .NET."""
    defn = _resolve_model_definition(report_dir, model_dir)
    if not defn:
        return {"tier": "openability", "available": False,
                "reason": "no *.SemanticModel/definition folder found near %s"
                          % (model_dir or report_dir)}
    try:
        serializer = _load_tmdl_serializer(tabular_editor_dir)
    except Exception as exc:  # noqa: BLE001 -- absent deps must degrade, never raise
        return {"tier": "openability", "available": False, "definition": defn,
                "reason": "TOM/pythonnet not available: %s" % str(exc).strip()[:200]}
    try:
        db = serializer.DeserializeDatabaseFromFolder(defn)
    except Exception as exc:  # noqa: BLE001 -- a parse failure IS the BLOCKED signal, not an error
        msg = str(exc).strip().replace(chr(10), " ").replace(chr(13), " ")
        file_, line_ = _parse_tmdl_error_location(msg)
        return {
            "tier": "openability", "available": True, "openable": False,
            "definition": defn, "verdict": "blocked",
            "error": msg[:400], "error_file": file_, "error_line": line_, "advisory": True,
            "notes": [
                "BLOCKED: the semantic model does not deserialize under the authoritative TMDL "
                "parser -- it will NOT open in Power BI Desktop and will NOT deploy to Fabric. This "
                "is a concrete, fix-before-ship defect, not a fidelity judgment.",
                "Same TMDL grammar both targets consume, so this offline parse predicts both a "
                "Desktop open and a Fabric publish without Desktop, capacity, or credentials.",
            ],
        }
    stats = _tom_model_stats(db)
    return {
        "tier": "openability", "available": True, "openable": True,
        "definition": defn, "verdict": "opens",
        "tables": stats["tables"], "measures": stats["measures"],
        "relationships": stats["relationships"], "advisory": True,
        "notes": [
            "OPENS: the model deserializes under the authoritative TMDL parser -- a faithful "
            "pre-flight for both a Desktop open and a Fabric publish (same grammar).",
            "This gate validates the model DEFINITION (syntax/structure) only; data binding / "
            "refresh / credential validity is target-specific and belongs to the value tier.",
        ],
    }


# =====================================================================================
# Deterministic advisory tier: metadata / binding fidelity (emitted TMDL vs Tableau source)
# =====================================================================================
# Stdlib-only and offline -- no Power BI Desktop, no Analysis Services, no credentials. It re-reads
# the emitted ``*.SemanticModel`` TMDL with its OWN line reader (never the engine's emitter) and the
# Tableau ``.twb``/``.tds`` column schema with ElementTree, then cross-references three things:
#   * DATATYPE DRIFT  -- an emitted physical column's ``dataType`` vs the mapped Tableau source type.
#   * BINDING RESOLUTION -- every ``'Table'[Column]`` / ``[Column]`` reference inside a measure body,
#     a calculated-column body, or a calculated-table partition ``source`` resolves to a real model
#     column or measure. This is the static catch for the whole UNRESOLVABLE-REF defect class: a
#     sanitized-name mismatch (``[State/Province]`` vs ``[State_Province]``), or a window function
#     (OFFSET / WINDOW / INDEX) whose ORDERBY/relation points at a column that is not in the model
#     ("cross-table" defect) -- both surface here as an unresolved binding BEFORE any live fire.
#   * COVERAGE -- Tableau source columns absent from the model (dropped), and physical model columns
#     with no Tableau source (added).
# Advisory and tolerance-banded like every other tier; it does NOT feed the combined headline, so it
# can never move calibration -- it is a separate, fail-loud pre-fire fidelity gate.

# A Tableau scalar datatype maps to the TMDL ``dataType`` the engine should land. The COMPATIBLE sets
# keep faithful cross-engine widenings (a Tableau ``date`` landing as ``dateTime``; an ``integer``
# measure summarized as ``double``) from reading as drift -- only a genuine family change is flagged.
_TABLEAU_TO_TMDL_DTYPE = {
    "integer": "int64",
    "real": "double",
    "string": "string",
    "boolean": "boolean",
    "date": "dateTime",
    "datetime": "dateTime",
}
_DTYPE_COMPATIBLE = {
    "int64": {"int64", "double", "decimal"},
    "double": {"double", "decimal", "int64"},
    "decimal": {"decimal", "double", "int64"},
    "dateTime": {"dateTime", "date"},
    "date": {"date", "dateTime"},
    "string": {"string"},
    "boolean": {"boolean"},
}
# A column reference: ``'Quoted Table'[Col]``, ``Table[Col]``, or a bare ``[Col]`` (measure / context
# column). DAX string literals use double quotes, so a bracket pair is always an identifier ref.
_DAX_REF_RE = re.compile(r"(?:'(?P<tq>[^']+)'|(?P<tb>[A-Za-z_]\w*))?\s*\[(?P<col>[^\]]+)\]")
# Window/navigation functions whose arguments routinely reach ACROSS tables -- an unresolved ref here
# is exactly the recent cross-table defect class, so it is tagged for fail-loud emphasis.
_DAX_WINDOW_FUNCS = ("OFFSET", "WINDOW", "INDEX", "RANK", "ROWNUMBER", "MOVINGAVERAGE", "RUNNINGSUM")


def _tmdl_unquote(token):
    """Strip TMDL single-quote object quoting (``'Dim Swap Calc 1'`` -> ``Dim Swap Calc 1``)."""
    token = (token or "").strip()
    if len(token) >= 2 and token.startswith("'") and token.endswith("'"):
        return token[1:-1].replace("''", "'")
    return token


def _extract_dax_refs(expression):
    """Yield ``(table_or_None, column)`` for each column/measure reference in a DAX string."""
    for m in _DAX_REF_RE.finditer(expression or ""):
        table = m.group("tq") or m.group("tb")
        yield (table, m.group("col"))


# The navigation clauses inside a window function whose column argument fixes the SORT and the GROUP
# the window walks over. The semantic rule (matched to the engine's own positional-measure fix): a
# window's ORDERBY column must live in the SAME table the window partitions / aggregates -- a
# resolvable-but-cross-table ORDERBY (e.g. ``ORDERBY('Date'[Date])`` over an ``'Orders'`` aggregate)
# is invalid at query time even though every column reference exists, so pure binding resolution
# cannot see it. This is the deterministic, offline catch for that class.
_ORDERBY_CLAUSE_RE = re.compile(r"ORDERBY\s*\(", re.IGNORECASE)
_PARTITIONBY_CLAUSE_RE = re.compile(r"PARTITIONBY\s*\(", re.IGNORECASE)
# A single-quoted token in DAX is ALWAYS a table identifier (string literals use double quotes), so
# this captures table-only references like ``COUNTROWS('Orders')`` / ``ALLEXCEPT('Orders', ...)`` that
# carry no bracketed column and are therefore invisible to ``_extract_dax_refs``.
_DAX_TABLE_TOKEN_RE = re.compile(r"'([^']+)'")


def _balanced_paren_arg(expression, open_idx):
    """Return the substring inside the parentheses whose ``(`` is at ``open_idx`` (balanced)."""
    depth = 0
    for i in range(open_idx, len(expression)):
        ch = expression[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return expression[open_idx + 1:i]
    return expression[open_idx + 1:]


def _clause_tables(expression, clause_re):
    """Set of explicitly-qualified table names referenced inside every ``clause_re`` invocation."""
    tables = set()
    for m in clause_re.finditer(expression or ""):
        arg = _balanced_paren_arg(expression, m.end() - 1)
        for tbl, _col in _extract_dax_refs(arg):
            if tbl:
                tables.add(tbl.strip())
    return tables


def _strip_clause(expression, clause_re):
    """Remove every balanced ``clause_re(...)`` invocation, leaving the surrounding body intact."""
    out = expression or ""
    while True:
        m = clause_re.search(out)
        if not m:
            return out
        open_idx = m.end() - 1
        depth = 0
        close = None
        for i in range(open_idx, len(out)):
            if out[i] == "(":
                depth += 1
            elif out[i] == ")":
                depth -= 1
                if depth == 0:
                    close = i
                    break
        if close is None:
            return out[:m.start()]
        out = out[:m.start()] + out[close + 1:]


def _all_table_tokens(text):
    """Every table identifier in ``text``: quoted ``'Table'`` tokens plus the table of any
    ``Table[Col]`` / ``'Table'[Col]`` bracketed reference. Catches table-only refs (``COUNTROWS(
    'Orders')``) that carry no column."""
    tables = {t.strip() for t in _DAX_TABLE_TOKEN_RE.findall(text or "")}
    for tbl, _col in _extract_dax_refs(text or ""):
        if tbl:
            tables.add(tbl.strip())
    return tables


def _mask_dax_string_literals(expression):
    """Blank out DAX string literals (double-quoted; ``""`` is an escaped inner quote) with
    same-length spaces so any ``ORDERBY(`` / ``PARTITIONBY(`` / bracket / single-quote that appears
    INSIDE a string can never be mis-parsed as a real navigation clause or table/column reference.
    Length is preserved exactly so the balanced-paren index math downstream stays valid. Returns the
    input unchanged when it carries no string literal."""
    if not expression or '"' not in expression:
        return expression or ""
    out = []
    in_str = False
    i = 0
    n = len(expression)
    while i < n:
        ch = expression[i]
        if in_str:
            if ch == '"':
                if i + 1 < n and expression[i + 1] == '"':  # "" -> escaped quote, stay in string
                    out.append("  ")
                    i += 2
                    continue
                out.append(" ")
                in_str = False
            else:
                out.append(" ")
        elif ch == '"':
            out.append(" ")
            in_str = True
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _window_cross_table_findings(home_table, kind, obj_name, expression):
    """Flag a window-function ORDERBY whose table is not the table the window groups/aggregates.

    Conservative + deterministic: explicit-table refs only (a bare ``[Col]`` ORDERBY never flags),
    and a finding is raised only when there is a contradicting anchor table (a PARTITIONBY column or
    a non-ORDERBY body reference on a DIFFERENT table). A same-table ORDERBY -- the corrected form --
    never flags. String literals are masked first so a clause/ref quoted inside a DAX string cannot
    raise a false alarm. Returns a possibly-empty list; never raises."""
    if not expression:
        return []
    scan = _mask_dax_string_literals(expression)
    if not any(fn in scan.upper() for fn in _DAX_WINDOW_FUNCS):
        return []
    order_tables = _clause_tables(scan, _ORDERBY_CLAUSE_RE)
    if not order_tables:
        return []
    part_tables = _clause_tables(scan, _PARTITIONBY_CLAUSE_RE)
    body = _strip_clause(scan, _ORDERBY_CLAUSE_RE)
    body_tables = _all_table_tokens(body)
    anchor = part_tables | body_tables
    if not anchor:
        return []
    findings = []
    for ot in sorted(order_tables):
        if ot not in anchor:
            findings.append({
                "table": home_table, "object": obj_name, "kind": kind,
                "orderby_table": ot, "anchor_tables": sorted(anchor),
                "partition_tables": sorted(part_tables),
                "reason": ("window-function ORDERBY references table '%s' but the window "
                           "partitions/aggregates over %s -- a cross-table ORDERBY is invalid at "
                           "query/refresh time even though every column resolves"
                           % (ot, sorted(anchor))),
            })
    return findings


def _parse_tmdl_model(definition_dir):
    """Parse ``definition/tables/*.tmdl`` into a light model with its own stdlib line reader.

    Returns ``{"definition", "tables": {name: {...}}}`` where each table carries ``physical_columns``
    (``{name, data_type, source_column}``), ``calc_columns`` / ``measures`` (``{name, expression}``),
    and ``partition_sources`` (calculated-partition ``source`` bodies). The expression-bearing
    objects feed binding resolution; the physical columns feed datatype/coverage. Never raises on a
    malformed file -- a file that cannot be read is skipped."""
    tables_dir = os.path.join(definition_dir, "tables")
    if not os.path.isdir(tables_dir):
        tables_dir = definition_dir
    model = {"definition": os.path.abspath(definition_dir), "tables": {}}
    try:
        names = sorted(os.listdir(tables_dir))
    except OSError:
        return model
    for fname in names:
        if not fname.lower().endswith(".tmdl"):
            continue
        try:
            with open(os.path.join(tables_dir, fname), "r", encoding="utf-8-sig") as fh:
                lines = fh.read().replace("\r\n", "\n").replace("\r", "\n").split("\n")
        except OSError:
            continue
        _parse_tmdl_table_lines(lines, model)
    return model


def _split_tmdl_colref(ref):
    """Split a TMDL column reference into ``(table, column)``.

    Handles bare ``Orders.Order_Date`` as well as single-quoted object names on either side
    (``'Date Dim'.Date`` / ``Orders.'Order Date'``), including a dotted name inside the quotes.
    Returns ``(None, None)`` for an empty/garbage reference.
    """
    ref = (ref or "").strip()
    if not ref:
        return None, None
    if ref.startswith("'"):
        end = ref.find("'", 1)
        if end == -1:
            return _tmdl_unquote(ref), None
        table = ref[1:end].replace("''", "'")
        rest = ref[end + 1:].lstrip()
        if rest.startswith("."):
            rest = rest[1:].strip()
        return table, (_tmdl_unquote(rest) if rest else None)
    if "." in ref:
        table, col = ref.split(".", 1)
        return _tmdl_unquote(table), _tmdl_unquote(col)
    return _tmdl_unquote(ref), None


def _parse_tmdl_relationships(definition_dir):
    """Parse ``definition/relationships.tmdl`` into a list of normalized relationship records.

    Each record is ``{from_table, from_col, from_norm, to_table, to_table_norm, to_col, to_norm,
    active, date_behavior}``. ``active`` defaults to ``True`` -- a TMDL relationship is active unless
    it declares ``isActive: false`` -- which is exactly the directional/active evidence the date-axis
    credit (see ``_date_implied_sources``) gates on. Stdlib-only, own line reader; never raises -- a
    missing or unreadable file yields ``[]`` so scoring degrades to relationship-blind.
    """
    if not definition_dir:
        return []
    path = os.path.join(definition_dir, "relationships.tmdl")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            lines = fh.read().replace("\r\n", "\n").replace("\r", "\n").split("\n")
    except OSError:
        return []
    rels = []
    cur = None

    def _flush(rec):
        if rec and rec.get("from_col") and rec.get("to_col"):
            rels.append(rec)

    for raw in lines:
        content = raw.strip()
        if not content:
            continue
        if content.startswith("relationship "):
            _flush(cur)
            cur = {"active": True, "date_behavior": None,
                   "from_table": None, "from_col": None, "from_norm": None,
                   "to_table": None, "to_table_norm": None, "to_col": None, "to_norm": None}
            continue
        if cur is None or ":" not in content:
            continue
        key, val = content.split(":", 1)
        key = key.strip().lower()
        val = val.strip()
        if key == "isactive":
            cur["active"] = val.lower() not in ("false", "0", "no")
        elif key == "joinondatebehavior":
            cur["date_behavior"] = val or None
        elif key == "fromcolumn":
            t, c = _split_tmdl_colref(val)
            cur["from_table"], cur["from_col"], cur["from_norm"] = t, c, (_norm(c) if c else None)
        elif key == "tocolumn":
            t, c = _split_tmdl_colref(val)
            cur["to_table"], cur["to_col"] = t, c
            cur["to_table_norm"] = _norm(t) if t else None
            cur["to_norm"] = _norm(c) if c else None
    _flush(cur)
    return rels


def _parse_tmdl_table_lines(lines, model):
    """Stateful single pass over one TMDL file's lines, folding tables into ``model``."""
    cur = None            # current table record
    obj = None            # ("physcol", dict) | ("partition", dict) | None -- the open child
    capturing = False     # inside a calculated-partition ``source`` block
    for raw in lines:
        indent = len(raw) - len(raw.lstrip("\t"))
        content = raw.strip()
        if not content:
            continue
        if indent == 0 and content.startswith("table "):
            name = _tmdl_unquote(content[len("table "):])
            cur = {"name": name, "physical_columns": [], "calc_columns": [],
                   "measures": [], "partition_sources": []}
            model["tables"][name] = cur
            obj, capturing = None, False
            continue
        if cur is None:
            continue
        if indent == 1:
            obj, capturing = None, False
            if content.startswith("column "):
                rest = content[len("column "):]
                if _looks_like_assignment(rest):
                    name, expr = rest.split("=", 1)
                    cur["calc_columns"].append(
                        {"name": _tmdl_unquote(name), "expression": expr.strip()})
                else:
                    col = {"name": _tmdl_unquote(rest), "data_type": None, "source_column": None}
                    cur["physical_columns"].append(col)
                    obj = ("physcol", col)
            elif content.startswith("measure "):
                rest = content[len("measure "):]
                if _looks_like_assignment(rest):
                    name, expr = rest.split("=", 1)
                    cur["measures"].append(
                        {"name": _tmdl_unquote(name), "expression": expr.strip()})
            elif content.startswith("partition ") and "calculated" in content:
                part = {"name": content, "expression": ""}
                cur["partition_sources"].append(part)
                obj = ("partition", part)
        elif indent >= 2 and obj is not None:
            kind, rec = obj
            if kind == "physcol":
                if content.startswith("dataType:"):
                    rec["data_type"] = content.split(":", 1)[1].strip()
                elif content.startswith("sourceColumn:"):
                    rec["source_column"] = content.split(":", 1)[1].strip()
            elif kind == "partition":
                if content.startswith("source"):
                    capturing = True
                    after = content[len("source"):].lstrip()
                    if after.startswith("="):
                        after = after[1:].strip()
                    if after:
                        rec["expression"] += after + " "
                elif capturing:
                    rec["expression"] += content + " "


def _looks_like_assignment(rest):
    """True when a ``column``/``measure`` header line carries an ``= <expr>`` body (not just a name
    that happens to contain ``=`` inside quotes)."""
    head = rest.split("=", 1)[0]
    if "=" not in rest:
        return False
    # A quoted name with no closing quote before ``=`` means the ``=`` is inside the name.
    return head.count("'") % 2 == 0


def _map_tableau_dtype(dt):
    return _TABLEAU_TO_TMDL_DTYPE.get((dt or "").strip().lower())


_DERIVED_SRC_RE = re.compile(r"\(copy\)|\(group\)|\(bin\)|^Calculation_\d", re.IGNORECASE)
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _is_derived_source_name(name):
    """True for a Tableau-DERIVED source field name (a group, a copy, a bin, or an internal
    ``Calculation_<hash>``) -- these are not dropped physical columns, so they are not coverage gaps."""
    return bool(_DERIVED_SRC_RE.search(name or ""))


def _strip_trailing_paren(name):
    """Drop a single trailing ``(Qualifier)`` from a field name (``Region (People)`` -> ``Region``),
    so a blend/secondary-source reference matches its base physical column."""
    return _TRAILING_PAREN_RE.sub("", name or "").strip()


def _parse_tableau_schema(twb_path):
    """Source-column schema from the Tableau ``.twb`` (and any sibling ``.tds``).

    Returns ``{norm_name: {"name", "datatype", "role", "calculated"}}`` keyed by the normalized
    field name, unioning ``<column>`` elements (caption/datatype/role, a ``<calculation>`` child marks
    a calc) with ``<metadata-record class='column'>`` physical records (``remote-name``/``local-type``)
    so a column the workbook only references physically is still seen. Best-effort: a parse failure
    returns ``{}`` so the tier degrades to "no source schema" rather than raising."""
    paths = []
    if twb_path and os.path.isfile(twb_path):
        paths.append(twb_path)
        sib_dir = os.path.dirname(os.path.abspath(twb_path))
        try:
            for n in sorted(os.listdir(sib_dir)):
                if n.lower().endswith(".tds") and os.path.join(sib_dir, n) not in paths:
                    paths.append(os.path.join(sib_dir, n))
        except OSError:
            pass
    schema = {}
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        for col in _iter_local(root, "column"):
            name = _strip_brackets(col.get("caption") or col.get("name") or "")
            if not name:
                continue
            dt = (col.get("datatype") or "").strip().lower()
            if dt in ("", "table"):
                continue
            calc = _first_child(col, "calculation") is not None
            key = _norm(name)
            if key and (key not in schema or not schema[key].get("datatype")):
                schema[key] = {"name": name, "datatype": dt,
                               "role": (col.get("role") or "").strip().lower(), "calculated": calc}
        for rec in _iter_local(root, "metadata-record"):
            if (rec.get("class") or "") != "column":
                continue
            rn = _first_child(rec, "remote-name")
            lt = _first_child(rec, "local-type")
            if rn is None or not (rn.text or "").strip():
                continue
            name = _strip_brackets((rn.text or "").strip())
            key = _norm(name)
            dt = ((lt.text or "").strip().lower() if lt is not None else "")
            if key and key not in schema:
                schema[key] = {"name": name, "datatype": dt, "role": "", "calculated": False}
    return schema


def metadata_tier(report_dir=None, model_dir=None, twb_path=None):
    """Deterministic metadata / binding fidelity of an emitted semantic model vs its Tableau source.

    Walks every emitted table -- physical columns (datatype + coverage vs the Tableau schema) and
    every expression-bearing object (measures, calculated columns, calculated-partition sources) --
    resolving each column/measure reference against the parsed model. Returns an advisory record:

    * ``tables`` -- per table: physical/expression counts, datatype drift, extra (unmatched) physical
      columns, and a 0..1 ``confidence`` (clean physical matches / physical columns).
    * ``datatype_drift`` / ``missing_source_columns`` / ``extra_model_columns`` -- flat roll-ups.
    * ``unresolved_bindings`` -- every reference that does NOT resolve (the fail-loud catch), each
      tagged with its owner object, the offending ref, a reason, and a ``window_function`` flag.
    * ``scores`` -- ``metadata`` (mean table confidence), ``binding`` (resolved objects / total),
      and an ``overall`` mean, each with a band.

    Offline + stdlib only. Never raises: a missing model/twb degrades to ``{available: False}``."""
    defn = _resolve_model_definition(report_dir, model_dir)
    if not defn:
        return {"tier": "metadata", "available": False,
                "reason": "no *.SemanticModel/definition folder found near %s"
                          % (model_dir or report_dir)}
    try:
        model = _parse_tmdl_model(defn)
    except Exception as exc:  # noqa: BLE001 -- a parse fault degrades, never raises
        return {"tier": "metadata", "available": False, "definition": defn,
                "reason": "TMDL parse failed: %s" % str(exc).strip()[:200]}
    schema = _parse_tableau_schema(twb_path) if twb_path else {}

    # Two resolution namespaces, deliberately different:
    #   * NORMALIZED (``_norm``) -- cross-engine COVERAGE/datatype matching, where ``Order Date`` and
    #     ``Order_Date`` are the same source column (cosmetic rename is faithful).
    #   * EXACT (case-insensitive literal) -- BINDING resolution, where DAX is literal: ``[State/
    #     Province]`` and ``[State_Province]`` are DIFFERENT identifiers. Using ``_norm`` here would
    #     hide the sanitized-name defect (the engine lands DAX verbatim; AS resolves it literally).
    col_norms_by_table = {}
    exact_cols_by_table = {}
    exact_table_lookup = {}
    exact_measures = set()
    for tname, t in model["tables"].items():
        exact_table_lookup[tname.lower()] = tname
        norms = set()
        exact = set()
        for c in t["physical_columns"]:
            norms.add(_norm(c["name"]))
            exact.add(c["name"].lower())
        for c in t["calc_columns"]:
            norms.add(_norm(c["name"]))
            exact.add(c["name"].lower())
        col_norms_by_table[tname] = norms
        exact_cols_by_table[tname] = exact
        for mrec in t["measures"]:
            exact_measures.add(mrec["name"].lower())

    def _resolve(home_table, ref_table, ref_col):
        ecol = (ref_col or "").strip().lower()
        if ref_table:
            real = exact_table_lookup.get(ref_table.strip().lower())
            if real is None:
                return (False, "unknown table '%s'" % ref_table)
            if ecol in exact_cols_by_table.get(real, set()) or ecol in exact_measures:
                return (True, None)
            return (False, "column '%s' not found in table '%s'" % (ref_col, ref_table))
        # bare [Col]: a measure, or a column reachable in the home-table row context
        if ecol in exact_measures or ecol in exact_cols_by_table.get(home_table, set()):
            return (True, None)
        for exact in exact_cols_by_table.values():
            if ecol in exact:
                return (True, None)
        return (False, "unresolved bare reference '[%s]'" % ref_col)

    unresolved = []
    cross_table_windows = []
    window_objects = 0
    total_objs = 0
    resolved_objs = 0

    def _scan(home_table, kind, obj_name, expression):
        nonlocal total_objs, resolved_objs, window_objects
        total_objs += 1
        # Mask DAX string literals before any raw-DAX scan so a bracket / single-quote / window-name
        # quoted INSIDE a string can never be mis-read as a real reference (a false unresolved
        # binding) or a phantom window object. Outside string literals the text is byte-identical, so
        # resolution of every genuine reference -- and thus the established calibration -- is unchanged.
        scan = _mask_dax_string_literals(expression)
        up = scan.upper()
        is_window = any(fn in up for fn in _DAX_WINDOW_FUNCS)
        if is_window:
            window_objects += 1
            cross_table_windows.extend(
                _window_cross_table_findings(home_table, kind, obj_name, expression))
        obj_ok = True
        for ref_table, ref_col in _extract_dax_refs(scan):
            ok, reason = _resolve(home_table, ref_table, ref_col)
            if not ok:
                obj_ok = False
                ref_txt = ("'%s'[%s]" % (ref_table, ref_col)) if ref_table else "[%s]" % ref_col
                unresolved.append({
                    "table": home_table, "object": obj_name, "kind": kind,
                    "ref": ref_txt, "reason": reason, "window_function": is_window})
        if obj_ok:
            resolved_objs += 1

    tables_out = []
    datatype_drift = []
    extra_model_columns = []
    model_col_norms = set()
    for tname, t in model["tables"].items():
        for c in t["physical_columns"]:
            src = c.get("source_column") or ""
            if src and not src.startswith("["):
                model_col_norms.add(_norm(c["name"]))
                model_col_norms.add(_norm(_strip_brackets(src)))
    for tname, t in model["tables"].items():
        drift_here = []
        extra_here = []
        clean = 0
        physical = t["physical_columns"]
        for c in physical:
            # calc-table tuple columns bind to a synthetic [ValueN]; only real source columns
            # (an unbracketed sourceColumn) are checked against the Tableau schema.
            src = c.get("source_column") or ""
            is_real_source = bool(src) and not src.startswith("[")
            if not is_real_source:
                continue
            match = schema.get(_norm(c["name"])) or schema.get(_norm(_strip_brackets(src)))
            if match is None:
                extra_here.append(c["name"])
                continue
            expected = _map_tableau_dtype(match["datatype"])
            emitted = c.get("data_type")
            if expected and emitted and emitted not in _DTYPE_COMPATIBLE.get(expected, {expected}):
                drift_here.append({"table": tname, "column": c["name"],
                                   "emitted_type": emitted, "tableau_type": match["datatype"],
                                   "expected_type": expected})
            else:
                clean += 1
        for mrec in t["measures"]:
            _scan(tname, "measure", mrec["name"], mrec["expression"])
        for crec in t["calc_columns"]:
            _scan(tname, "calc_column", crec["name"], crec["expression"])
        for prec in t["partition_sources"]:
            _scan(tname, "partition_source", tname, prec["expression"])
        real_physical = [c for c in physical
                         if (c.get("source_column") or "") and not (c["source_column"]).startswith("[")]
        n_real = len(real_physical)
        confidence = round(clean / n_real, 4) if n_real else None
        datatype_drift.extend(drift_here)
        extra_model_columns.extend({"table": tname, "column": x} for x in extra_here)
        tables_out.append({
            "table": tname,
            "physical_columns": len(physical),
            "source_backed_columns": n_real,
            "calc_columns": len(t["calc_columns"]),
            "measures": len(t["measures"]),
            "datatype_drift": drift_here,
            "extra_columns": extra_here,
            "confidence": confidence,
        })

    # A Tableau source field is "missing" only when it is a genuine, non-derived physical field with
    # no matching model column. Tableau-derived fields (groups, copies, internal ``Calculation_*``)
    # and blend references (a trailing ``(Other Source)`` suffix) are not dropped physical columns --
    # match them by their base name so they do not read as false gaps.
    missing = []
    for k, v in sorted(schema.items()):
        if v.get("calculated"):
            continue
        name = v["name"]
        if _is_derived_source_name(name):
            continue
        base = _strip_trailing_paren(name)
        if k in model_col_norms or _norm(base) in model_col_norms:
            continue
        missing.append({"name": name, "datatype": v["datatype"]})

    confidences = [t["confidence"] for t in tables_out if t["confidence"] is not None]
    meta_score = round(sum(confidences) / len(confidences), 4) if confidences else None
    binding_score = round(resolved_objs / total_objs, 4) if total_objs else None
    parts = [s for s in (meta_score, binding_score) if s is not None]
    overall = round(sum(parts) / len(parts), 4) if parts else None
    # window_consistency is a SEPARATE, additive signal -- it never feeds ``overall`` (so the
    # established calibration is untouched) and it never re-counts a cross-table window as an
    # "unresolved binding" (those refs DO resolve). It is the deterministic catch for the
    # resolvable-but-cross-table ORDERBY class that pure binding resolution is blind to.
    flagged_window_objs = len({(f["table"], f["object"]) for f in cross_table_windows})
    window_consistency = (round(1 - flagged_window_objs / window_objects, 4)
                          if window_objects else None)

    notes = [
        "ADVISORY metadata/binding tier: deterministic, offline, stdlib-only -- it re-reads the "
        "emitted TMDL and the Tableau schema with independent readers and does NOT feed the combined "
        "headline (so it can never move calibration).",
        "BINDING is the fail-loud catch: an unresolved ref is a query/refresh-time break the "
        "openability gate cannot see (a model can deserialize yet reference a column that is not in "
        "it) -- e.g. a sanitized-name mismatch or a window-function ORDERBY pointing across tables.",
        "WINDOW CONSISTENCY is a distinct fail-loud catch: a window function (OFFSET/WINDOW/INDEX/"
        "ROWNUMBER/...) whose ORDERBY column lives in a DIFFERENT table than the partition/aggregate "
        "is invalid at query time even though every column resolves -- pure binding resolution is "
        "blind to it, so it is reported separately and never moves the binding score.",
    ]
    if not schema:
        notes.append("No Tableau source schema parsed (no twb_path or unreadable): datatype/coverage "
                     "checks are skipped; binding resolution still ran against the model alone.")

    return {
        "tier": "metadata", "available": True, "advisory": True,
        "definition": defn,
        "source_schema_columns": len(schema),
        "tables": tables_out,
        "datatype_drift": datatype_drift,
        "missing_source_columns": missing,
        "extra_model_columns": extra_model_columns,
        "unresolved_bindings": unresolved,
        "cross_table_windows": cross_table_windows,
        "window_objects_total": window_objects,
        "objects_total": total_objs,
        "objects_resolved": resolved_objs,
        "scores": {
            "metadata": meta_score, "metadata_band": _band(meta_score) if meta_score is not None else None,
            "binding": binding_score, "binding_band": _band(binding_score) if binding_score is not None else None,
            "overall": overall, "overall_band": _band(overall) if overall is not None else None,
            "window_consistency": window_consistency,
            "window_consistency_band": _band(window_consistency) if window_consistency is not None else None,
        },
        "notes": notes,
    }


# =====================================================================================
# Optional advisory tier: LLM-assist adjudication (a deterministic harness; the agent IS the model)
# =====================================================================================
# This mirrors the skill's two established agent-assisted tiers -- the second compiler
# (``second-compiler.md`` / ``translation_router`` / ``translation_handoff_artifact``) and the image
# oracle (``image_oracle.build_oracle_bundle`` / ``agent_prompt`` / ``apply_adjudications``). Like
# both, it is a DETERMINISTIC PRODUCER, not an API client: it packages the oracle's own evidence into
# an additive *adjudication-request bundle* + a driving-agent prompt, the agent running the skill
# answers in strict JSON, and a constrained applier folds that answer back. There is NO network, key,
# or model call here -- the agent is the judgment model. The hard invariant (confirmed against the
# exemplars): the advisory answer can never flip a deterministic verdict or move a score -- a BLOCKED
# openability gate stays BLOCKED (recorded as ``gate_locked``), and nothing is renamed or rebound.
LLM_BUNDLE_VERSION = 1
LLM_BUNDLE_KIND = "tableau-fabric-fidelity-adjudication-request"

# The hard invariants the adjudication pass must honour, surfaced verbatim in the bundle + prompt so
# the constraints travel WITH the request (the same discipline image_oracle.ORACLE_RULES uses).
FIDELITY_ORACLE_RULES = (
    "You are ADVISORY. You may add color, adjudicate a borderline divergence as faithful-or-not, "
    "diagnose a blocker, and suggest a concrete fix -- but you can NEVER flip a deterministic verdict "
    "or move a score. A BLOCKED openability gate stays BLOCKED regardless of your opinion: the model "
    "literally does not parse.",
    "Judge FAITHFULNESS, not preference. A divergence is faithful when it preserves the same data, "
    "encoding, and answer (e.g. area->line, a star-schema field rename, an implicit-aggregate measure "
    "name). Flag only divergences that change what the user SEES or the NUMBERS.",
    "Ground every judgment in the evidence provided (types, fields, values, positions, errors). Do "
    "not invent fields, values, or visuals that are not in the bundle.",
    "Never propose RENAMING a model field/measure or rebinding anything as a 'fix' -- naming and "
    "provenance are deterministic concerns. Target fixes at the actual artifact (a measure body, an "
    "annotation, a visual ref).",
    "No tool call, no API, no network: you (the agent running this skill) ARE the judgment model. "
    "Read the evidence and answer in STRICT JSON only.",
)


def _fidelity_image_refs(report):
    """Collect on-disk PNG paths for a VISUAL adjudication: the Power BI first-pass render(s) (from
    the ``powerbi-desktop`` render bridge) plus any Tableau reference image the image tier compared
    against. The agent running this skill is vision-capable (same premise as ``image_oracle``), so
    handing it the actual render lets it say concretely 'this part is off' -- grounded, not guessed.
    Returns ``[{role, path, page_id?}]``; empty when nothing was rendered/resolved."""
    refs = []
    seen = set()

    def _add(role, path, page_id=None):
        if not path or not isinstance(path, str):
            return
        ap = os.path.abspath(path)
        key = os.path.normcase(ap)
        if key in seen or not os.path.isfile(ap):
            return
        seen.add(key)
        rec = {"role": role, "path": ap}
        if page_id:
            rec["page_id"] = page_id
        refs.append(rec)

    inputs = report.get("image_inputs") or {}
    _add("tableau-reference", inputs.get("reference_png"))
    _add("powerbi-render", inputs.get("candidate_png"))
    ren = report.get("pbi_render") or {}
    if ren.get("available"):
        for p in ren.get("pages", []) or []:
            _add("powerbi-render", p.get("png"), page_id=p.get("page_id"))
    return refs


def _fidelity_items(report):
    """The per-item adjudication evidence: blockers (openability, measure-eval errors), per-visual
    diffs, and unmatched worksheets -- each with a pre-filled ``answer`` template the agent edits.
    When a Power BI render exists, a VISION item is prepended so the agent looks at the actual
    first-pass image and adjudicates what is visually off."""
    items = []
    image_refs = _fidelity_image_refs(report)
    if image_refs:
        items.append({
            "id": "visual-diff:render", "kind": "vision",
            "subject": "Power BI first-pass render vs Tableau",
            "evidence": {"image_refs": image_refs,
                         "image_ssim": (report.get("image") or {}).get("ssim"),
                         "image_band": (report.get("image") or {}).get("band")},
            "priority": "high",
            "question": "VIEW the referenced PNG(s) -- the Power BI first-pass render (and the "
                        "Tableau reference if present). List the concrete visual divergences you can "
                        "SEE (wrong chart type, missing/extra mark, mis-sorted or mis-colored series, "
                        "a dropped filter, mis-placed legend/title/zone), each tied to where on the "
                        "page it is. Judge whether each is a faithful difference or a real defect.",
            "answer": {"id": "visual-diff:render", "judgment": "",
                       "divergences": [{"where": "", "what": "", "faithful": True}]},
        })
    openrec = report.get("openability")
    if openrec and openrec.get("available") and openrec.get("openable") is False:
        items.append({
            "id": "blocker:openability", "kind": "blocker",
            "subject": "semantic model does not deserialize",
            "evidence": {"error": openrec.get("error"), "error_file": openrec.get("error_file"),
                         "error_line": openrec.get("error_line")},
            "priority": "high",
            "question": "Diagnose why the model fails to deserialize and give the minimal fix. This "
                        "is a deterministic BLOCK -- your answer is advisory triage; it does not "
                        "unblock anything.",
            "answer": {"id": "blocker:openability", "judgment": "blocker", "diagnosis": "",
                       "suggested_fix": ""},
        })
    dax = report.get("dax_value")
    if dax and dax.get("available"):
        for r in dax.get("results", []) or []:
            if not r.get("ok"):
                mid = "blocker:measure:%s" % r.get("measure")
                items.append({
                    "id": mid, "kind": "blocker",
                    "subject": "measure %r fails to evaluate" % r.get("measure"),
                    "evidence": {"measure": r.get("measure"), "error": r.get("error")},
                    "priority": "high",
                    "question": "Diagnose the evaluation error and give the minimal fix.",
                    "answer": {"id": mid, "judgment": "blocker", "diagnosis": "", "suggested_fix": ""},
                })
    for v in report.get("visuals", []) or []:
        diag = v.get("diagnosis")
        needs = (v.get("band") in ("review", "divergent")) or bool(v.get("fields_missing")) \
            or bool(v.get("fields_extra")) or diag == _REMODEL_DIAGNOSIS
        vid = "visual:%s" % v.get("worksheet")
        items.append({
            "id": vid, "kind": "visual", "subject": v.get("worksheet"),
            "evidence": {"visual_type": v.get("visual_type"), "type_note": v.get("type_note"),
                         "score": v.get("score"), "band": v.get("band"),
                         "fields_missing": v.get("fields_missing"),
                         "fields_extra": v.get("fields_extra"), "diagnosis": diag},
            "priority": "high" if needs else "low",
            "question": "Is this rebuilt visual a FAITHFUL match of the Tableau worksheet, or a real "
                        "divergence? Judge faithful / divergent / unsure with a short rationale.",
            "answer": {"id": vid, "judgment": "", "rationale": ""},
        })
    for ws in (report.get("summary") or {}).get("unmatched_worksheets", []) or []:
        uid = "unmatched:%s" % ws
        items.append({
            "id": uid, "kind": "unmatched", "subject": ws,
            "evidence": {"worksheet": ws}, "priority": "high",
            "question": "This Tableau worksheet has no paired Power BI visual. Is it genuinely "
                        "missing from the rebuild, or paired under a different name/page?",
            "answer": {"id": uid, "judgment": "", "rationale": ""},
        })
    return items


def build_fidelity_bundle(report, *, max_items=200):
    """Deterministic adjudication-request bundle from the oracle report (mirror of
    :func:`image_oracle.build_oracle_bundle`). Additive, JSON-serialisable, and it NEVER mutates the
    report -- it packages the deterministic evidence (blockers, per-visual diffs, unmatched
    worksheets) + the hard rules + a per-item answer template for the agent-as-judgment pass."""
    items = _fidelity_items(report)[:max_items]
    cf = report.get("combined_fidelity") or {}
    summary = report.get("summary") or {}
    openrec = report.get("openability") or {}
    image_refs = _fidelity_image_refs(report)
    high = sum(1 for it in items if it.get("priority") == "high")
    return {
        "version": LLM_BUNDLE_VERSION,
        "kind": LLM_BUNDLE_KIND,
        "rules": list(FIDELITY_ORACLE_RULES),
        "deterministic": {
            "verdict": cf.get("verdict"),
            "combined_score": cf.get("combined_score"),
            "openable": openrec.get("openable") if openrec.get("available") else None,
            "aggregate_score": summary.get("aggregate_score"),
            "gate_locked": cf.get("verdict") == "blocked",
        },
        "summary": {
            "items": len(items),
            "to_review": high,
            "blockers": sum(1 for it in items if it.get("kind") == "blocker"),
            "image_refs": len(image_refs),
        },
        "image_refs": image_refs,
        "items": items,
        "answer_schema": {
            "verdict": "faithful-candidate | opens-but-broken | blocked | needs-human",
            "confidence": "0.0-1.0",
            "per_item": "array of the per-item answer objects above (key back by id)",
            "summary": "one-paragraph plain-language adjudication",
        },
    }


def fidelity_agent_prompt(bundle):
    """Render the driving-agent instruction for a fidelity bundle (no API key, no tool call).

    States the hard invariants, echoes the authoritative deterministic verdict (which the agent may
    NOT change), lists each item to adjudicate with its evidence, and asks for strict JSON answers."""
    lines = [
        "You are the Tableau -> Power BI FIDELITY oracle's advisory adjudicator. The deterministic "
        "gates have already scored this rebuild; your job is the judgment the math cannot do: decide "
        "whether borderline divergences are FAITHFUL or real, diagnose any blockers, and suggest "
        "concrete fixes. You are advisory -- you cannot change a verdict or a score.",
        "",
        "Hard rules:",
    ]
    for i, rule in enumerate(bundle.get("rules", []), 1):
        lines.append("  %d. %s" % (i, rule))
    det = bundle.get("deterministic", {})
    lines.append("")
    lines.append("Deterministic verdict (authoritative, do NOT change): %s  "
                 "(combined_score=%s, openable=%s, gate_locked=%s)" % (
                     det.get("verdict"), det.get("combined_score"),
                     det.get("openable"), det.get("gate_locked")))
    image_refs = bundle.get("image_refs") or []
    if image_refs:
        lines.append("")
        lines.append("Rendered images to VIEW (you are vision-capable -- open each path and LOOK; "
                     "the Power BI render is the first-pass rebuild, the Tableau reference is ground "
                     "truth where present):")
        for ref in image_refs:
            tag = ref.get("page_id")
            lines.append("  - [%s] %s%s" % (
                ref.get("role"), ref.get("path"), (" (page %s)" % tag) if tag else ""))
    items = bundle.get("items", [])
    review = [it for it in items if it.get("priority") == "high"] or items
    if not review:
        lines.append("")
        lines.append("No items to adjudicate -- the rebuild is clean on the deterministic evidence.")
        return "\n".join(lines)
    lines.append("")
    lines.append("Items to adjudicate (%d):" % len(review))
    for it in review:
        lines.append("")
        lines.append("- id=%r kind=%s subject=%r priority=%s" % (
            it.get("id"), it.get("kind"), it.get("subject"), it.get("priority")))
        lines.append("    evidence: %s" % json.dumps(it.get("evidence"), ensure_ascii=False))
        lines.append("    question: %s" % it.get("question"))
    schema = bundle.get("answer_schema", {})
    lines.append("")
    lines.append(
        "Respond with STRICT JSON only: {\"verdict\": <one of: %s>, \"confidence\": <0..1>, "
        "\"summary\": <one paragraph>, \"per_item\": [ <one answer object per item, keyed by its "
        "id, using each item's 'answer' template shape> ]}. Send nothing anywhere -- you ARE the "
        "judgment model." % schema.get("verdict"))
    return "\n".join(lines)


def apply_fidelity_adjudication(report, answers):
    """Fold the agent's JSON adjudication into an ADVISORY ``llm-assist`` record (mirror of
    :func:`image_oracle.apply_adjudications`).

    It never flips a deterministic verdict, never moves a score, and never renames/rebinds anything:
    a BLOCKED gate stays BLOCKED (recorded as ``gate_locked``; the advisory ``effective_verdict`` can
    never upgrade past it). The input ``report`` is not mutated; ``answers`` may be a dict or a JSON
    string. Returns ``{available: False, reason}`` when the answer is not a parseable object."""
    parsed = _parse_json_loose(answers) if isinstance(answers, str) else answers
    cf = report.get("combined_fidelity") or {}
    det_verdict = cf.get("verdict")
    gate_locked = det_verdict == "blocked"
    if not isinstance(parsed, dict):
        return {"tier": "llm-assist", "available": False,
                "reason": "no parseable agent adjudication (expected a JSON object)",
                "deterministic_verdict": det_verdict, "gate_locked": gate_locked}
    per_item = parsed.get("per_item")
    if not isinstance(per_item, list):
        per_item = []
    llm_verdict = parsed.get("verdict")
    # The advisory verdict can never UPGRADE past a deterministic block (warn-never-wrong analog).
    effective = det_verdict if gate_locked else (det_verdict or llm_verdict)
    notes = ["ADVISORY: agent adjudication adds judgment / diagnosis / fix suggestions; it never "
             "changes a deterministic verdict or score."]
    if gate_locked and llm_verdict and llm_verdict != "blocked":
        notes.append(
            "gate_locked: the deterministic openability gate is BLOCKED, so the model does not open "
            "regardless of the agent's %r opinion -- the verdict stays blocked." % llm_verdict)
    return {
        "tier": "llm-assist", "available": True, "advisory": True,
        "deterministic_verdict": det_verdict,
        "effective_verdict": effective,
        "llm_verdict": llm_verdict,
        "gate_locked": gate_locked,
        "confidence": parsed.get("confidence"),
        "summary": parsed.get("summary"),
        "adjudications": per_item,
        "notes": notes,
    }


def llm_assist_tier(report, *, answers=None, max_items=200):
    """Optional advisory LLM-assist tier: a deterministic harness, NOT an API client.

    Builds the adjudication-request bundle + the driving-agent prompt from the oracle report. With
    the agent's ``answers`` (a JSON string/dict) it folds them into an advisory record via
    :func:`apply_fidelity_adjudication`; without them it returns ``{available: False, ...}`` carrying
    the ``bundle`` + ``prompt`` so the agent running the skill can read, judge, and re-invoke. No
    network, no key -- the agent IS the judgment model (mirror of the image oracle)."""
    bundle = build_fidelity_bundle(report, max_items=max_items)
    prompt = fidelity_agent_prompt(bundle)
    if answers is None:
        return {"tier": "llm-assist", "available": False,
                "reason": "no agent adjudication provided; read 'prompt', judge the items, then "
                          "re-invoke with answers (the agent is the judgment model -- nothing is "
                          "sent anywhere)",
                "bundle": bundle, "prompt": prompt}
    record = apply_fidelity_adjudication(report, answers)
    record["bundle"] = bundle
    record["prompt"] = prompt
    return record


# =====================================================================================
# Top-level convenience + CLI
# =====================================================================================
# Gate 0 (openability) dominates the combined headline: a model that does not deserialize cannot be
# faithful no matter how the structural/value/image tiers score. The uncapped number is preserved as
# ``raw_combined_score`` for diagnostics.
_BLOCKED_COMBINED_SCORE = 0.0
_FAITHFUL_CANDIDATE_MIN = 0.85


def _fidelity_verdict(report, combined_score, openable):
    """Three-band advisory headline: ``blocked`` -> ``opens-but-broken`` -> ``opens-needs-review``
    -> ``faithful-candidate``. Openability dominates; a value-tier eval error means it opens but is
    broken; otherwise a strong combined score reads as a faithful candidate."""
    if openable is False:
        return "blocked"
    dax = report.get("dax_value")
    if dax and dax.get("available") and dax.get("measures_errored"):
        return "opens-but-broken"
    if combined_score is not None and combined_score >= _FAITHFUL_CANDIDATE_MIN:
        return "faithful-candidate"
    if openable is True or combined_score is not None:
        return "opens-needs-review"
    return None


def _combined_fidelity(report):
    """Fuse the tiers that actually ran into one advisory headline + a confidence flag.

    Pulls the structural aggregate, the DAX-value ``value_score``, and the image SSIM (preferring
    the per-zone ``regions_mean_ssim`` when present) -- using only those that are available -- then
    blends them with :data:`COMBINED_WEIGHTS` renormalized over the contributing tiers. ``confidence``
    is ``high``/``medium``/``low`` for 3/2/1 tiers. Advisory only: this is a headline for triage, not
    a gate, and it does not assume the tiers agree (that divergence is itself the useful signal).
    Returns ``None`` when not even the structural score is available.
    """
    tiers = {}
    struct = (report.get("summary") or {}).get("aggregate_score")
    if struct is not None:
        tiers["structural"] = float(struct)
    dax = report.get("dax_value")
    if dax and dax.get("available") and dax.get("value_score") is not None:
        tiers["value"] = float(dax["value_score"])
    img = report.get("image")
    if img and img.get("available"):
        iscore = img.get("regions_mean_ssim")
        if iscore is None:
            iscore = img.get("ssim")
        if iscore is not None:
            tiers["image"] = float(iscore)
    openrec = report.get("openability")
    openable = openrec.get("openable") if (openrec and openrec.get("available")) else None
    if not tiers and openable is None:
        return None
    wsum = sum(COMBINED_WEIGHTS[k] for k in tiers)
    combined = sum(COMBINED_WEIGHTS[k] * v for k, v in tiers.items()) / wsum if wsum else None
    confidence = {3: "high", 2: "medium", 1: "low"}.get(len(tiers), "low")
    result = {
        "combined_score": round(combined, 4) if combined is not None else None,
        "band": _band(combined) if combined is not None else None,
        "confidence": confidence,
        "contributing_tiers": sorted(tiers),
        "tier_scores": {k: round(v, 4) for k, v in tiers.items()},
        "weights": {k: COMBINED_WEIGHTS[k] for k in tiers},
        "openable": openable,
        "advisory": True,
        "note": ("Advisory headline fusing the tiers that ran (weights renormalized over those "
                 "present); confidence reflects how many tiers backed it, not their mutual "
                 "agreement -- a low image score pulling the headline down IS the signal."),
    }
    # Gate 0 dominates: a model that does not deserialize cannot be faithful at any structural score.
    if openable is False:
        result["raw_combined_score"] = result["combined_score"]
        result["raw_band"] = result["band"]
        result["combined_score"] = _BLOCKED_COMBINED_SCORE
        result["band"] = _band(_BLOCKED_COMBINED_SCORE)
        result["blocked"] = True
    result["verdict"] = _fidelity_verdict(report, result["combined_score"], openable)
    return result


def _load_field_aliases(path):
    """Load an alias source JSON into a ``{ref: caption}`` map. Accepts a candidate_records list, a
    dict wrapping ``candidate_records`` (e.g. a ``migrate_twb_to_pbir`` result), or an already-flat
    ``{ref: caption}`` map. Returns ``{}`` on any problem -- the advisory tier never raises."""
    data = _read_json(path)
    if isinstance(data, list):
        return aliases_from_candidate_records(data)
    if isinstance(data, dict):
        if isinstance(data.get("candidate_records"), list):
            return aliases_from_candidate_records(data["candidate_records"])
        return {str(k): str(v) for k, v in data.items()
                if k and isinstance(v, str) and v}
    return {}


def _resolve_reference_png(source, name=None):
    """Resolve a single Tableau *reference* PNG from a local source, for the image tier.

    ``source`` may be a folder of exported PNGs, a single ``.png``, or a packaged ``.twbx`` (whose
    embedded ``Image/`` objects are extracted to a temp dir first). When ``name`` is given the PNG
    whose (tolerant) stem matches it is returned; with no name a lone PNG is used. Lazy-imports
    :mod:`fidelity_reference` and is fully guarded -- any problem yields ``None`` (the optional image
    tier then simply reports ``available: False``), so wiring it in never breaks an offline run.
    """
    if not source:
        return None
    try:
        import fidelity_reference as fref
    except Exception:  # noqa: BLE001 - reference module/CWD issue must not break the oracle
        return None
    try:
        src = str(source)
        search = src
        if src.lower().endswith(".twbx"):
            rec = fref.extract_twbx_images(src, tempfile.mkdtemp(prefix="fo_twbx_img_"))
            if not rec.get("available") or not rec.get("extracted"):
                return None
            search = os.path.dirname(next(iter(rec["extracted"].values())))
        loaded = fref.load_exported_references(search, [name] if name else None)
        if name:
            return loaded.get("found", {}).get(name)
        by_stem = loaded.get("by_stem") or {}
        return next(iter(by_stem.values())) if len(by_stem) == 1 else None
    except Exception:  # noqa: BLE001 - advisory resolver never raises
        return None


def run_oracle(twb_path, report_dir, engine_report_path=None,
               dax_options=None, image_options=None, candidate_records_path=None,
               field_aliases=None, validate=False, openability_options=None,
               llm_options=None, metadata_options=None, per_visual_options=None):
    """Read both sides and score them. Returns the advisory report dict.

    The structural tier always runs. The optional value/image tiers run only when their options are
    supplied, and each attaches its own ``{available, ...}`` record without ever failing the run.

    ``field_aliases`` (or a ``candidate_records_path`` JSON to derive it from) lets the field/role
    components see through a faithful star-schema rename; both are optional and default to off.

    ``validate`` (optional, default off) runs the first-party ``powerbi-report-author validate``
    pre-gate over ``report_dir`` and attaches an ADDITIVE ``pbir_validation`` record + a compact
    ``summary.pbir_valid`` flag -- it never feeds the structural aggregate, so calibration is
    unchanged whether or not it runs.

    ``openability_options`` (optional; pass ``{}`` to enable with defaults) runs Gate 0 -- the TOM
    ``TmdlSerializer`` openability pre-flight over the emitted ``*.SemanticModel/definition`` -- and
    attaches it as ``openability``. It runs BEFORE the combined headline so a non-deserializing model
    dominates the verdict (``blocked``). Lazy + guarded: absent pythonnet/TOM degrades to
    ``{available: False, reason}``.

    ``llm_options`` (optional; pass ``{}`` to enable) builds the advisory LLM-assist adjudication
    bundle + agent prompt AFTER the combined headline is attached; pass ``{"answers": <json/dict>}``
    to fold the agent's adjudication back. No network/key -- the agent running this skill is the
    judgment model.

    ``per_visual_options`` (optional; pass ``{}`` to enable, or ``{"on_view": [names]}`` to scope to
    the dashboard's on-view sheets) attaches the per-visual REPRODUCED/PARTIAL/DEGRADED/MISSING
    verdict layer as ``per_visual`` -- the migration loop's objective function. Additive: it reads the
    structural per-visual records only and never feeds the structural aggregate or calibration.

    ``image_options`` may carry ``render_pbip`` (a .pbip path): when set and no ``candidate_png`` is
    given, the Power BI candidate render is captured locally via the ``powerbi-desktop`` bridge and
    used as the image-tier candidate; the raw render record is attached as ``pbi_render``.
    """
    twb = read_twb_views(twb_path)
    pbir = read_pbir_report(report_dir)
    engine_report = None
    if engine_report_path and os.path.isfile(engine_report_path):
        engine_report = _read_json(engine_report_path)
    if field_aliases is None and candidate_records_path and os.path.isfile(candidate_records_path):
        field_aliases = _load_field_aliases(candidate_records_path)
    # Resolve the emitted model's relationships (ACTIVE/inactive edges) so a faithful star-schema
    # date rebind onto a related Date dimension can be credited (see _date_implied_sources). Absent a
    # *.SemanticModel near report_dir this is [], leaving scoring relationship-blind (unchanged).
    _defn_dir = _resolve_model_definition(report_dir=report_dir)
    relationships = _parse_tmdl_relationships(_defn_dir) if _defn_dir else []
    report = score_report(twb, pbir, engine_report=engine_report, field_aliases=field_aliases,
                          relationships=relationships)
    report["inputs"] = {
        "twb": os.path.abspath(twb_path),
        "report_dir": os.path.abspath(report_dir),
        "engine_report": os.path.abspath(engine_report_path) if engine_report_path else None,
        "candidate_records": (os.path.abspath(candidate_records_path)
                              if candidate_records_path else None),
    }
    if dax_options is not None:
        report["dax_value"] = dax_value_tier(report_dir=report_dir, **dax_options)
    if image_options is not None:
        opts = dict(image_options)
        render_pbip = opts.pop("render_pbip", None)
        render_page_id = opts.pop("render_page_id", None)
        render_opts = dict(opts.pop("render_options", None) or {})
        ref_source = opts.pop("reference_source", None)
        ref_name = opts.pop("reference_name", None)
        if ref_source and not opts.get("reference_png"):
            ref_png = _resolve_reference_png(ref_source, ref_name)
            if ref_png:
                opts["reference_png"] = ref_png
        render_record = None
        if render_pbip and not opts.get("candidate_png"):
            render_record = render_pbi_report(
                render_pbip, page_ids=[render_page_id] if render_page_id else None, **render_opts)
            if render_record.get("available"):
                chosen = _pick_render_page(render_record.get("pages"), render_page_id)
                if chosen:
                    opts["candidate_png"] = chosen["png"]
        if opts.pop("auto_regions", False) and not opts.get("regions"):
            derived = regions_from_layout(twb, pbir)
            if derived:
                opts["regions"] = derived
        if render_record is not None and not render_record.get("available") \
                and not opts.get("candidate_png"):
            report["image"] = {"tier": "image", "available": False,
                               "reason": "render bridge unavailable: %s" % render_record.get("reason")}
        else:
            report["image"] = image_tier(**opts)
        if render_record is not None:
            report["pbi_render"] = render_record
        # Stash the resolved PNG paths so the LLM-assist tier can hand the actual render to the
        # vision-capable agent (the image tier echoes only scores, not the paths it compared).
        report["image_inputs"] = {"reference_png": opts.get("reference_png"),
                                  "candidate_png": opts.get("candidate_png")}
    if validate:
        vrec = validate_pbir(report_dir)
        report["pbir_validation"] = vrec
        report["summary"]["pbir_valid"] = vrec.get("valid") if vrec.get("available") else None
    if openability_options is not None:
        report["openability"] = openability_tier(report_dir=report_dir, **openability_options)
    if metadata_options is not None:
        opts = dict(metadata_options)
        opts.setdefault("twb_path", twb_path)
        report["metadata"] = metadata_tier(report_dir=report_dir, **opts)
    if per_visual_options is not None:
        report["per_visual"] = per_visual_fidelity(report, **per_visual_options)
    combined = _combined_fidelity(report)
    if combined is not None:
        report["combined_fidelity"] = combined
    if llm_options is not None:
        report["llm_assist"] = llm_assist_tier(report, **llm_options)
    return report


def render_markdown(report):
    """Render the advisory report as a compact Markdown summary."""
    s = report["summary"]
    lines = ["# Fidelity Oracle (advisory, structural)", ""]
    cf = report.get("combined_fidelity")
    if cf is not None:
        headline = "- **Combined fidelity:** %s (%s) — confidence %s [%s]" % (
            cf["combined_score"], cf["band"], cf["confidence"],
            ", ".join(cf["contributing_tiers"]))
        if cf.get("verdict"):
            headline += " — verdict **%s**" % cf["verdict"]
        lines.append(headline)
        if cf.get("blocked"):
            lines.append("- ⛔ **BLOCKED (Gate 0):** the semantic model does not deserialize; "
                         "combined score forced to 0.0 (raw was %s). Fix before any fidelity claim." %
                         cf.get("raw_combined_score"))
    lines.append("- **Aggregate:** %s (%s)" % (
        s["aggregate_score"], s["aggregate_band"]))
    lines.append("- **Mean / worst visual:** %s / %s" % (
        s["mean_visual_score"], s["worst_visual_score"]))
    lines.append("- **Coverage:** %s (%d/%d worksheets matched)" % (
        s["coverage"], s["matched_visuals"], s["source_worksheets"]))
    if s.get("pbir_valid") is not None:
        lines.append("- **PBIR schema valid:** %s (first-party validator)" % s["pbir_valid"])
    pr = s.get("placement")
    if pr:
        lines.append(
            "- **Layout (placement):** %s — %d/%d visuals pixel-exact, worst edge %s px%s" % (
                pr["verdict"], pr["pixel_exact"], pr["evaluated"],
                pr["worst_max_edge_px"],
                "" if pr["verdict"] == "pixel-exact" or not pr.get("worst_worksheet")
                else " (`%s`)" % pr["worst_worksheet"]))
    if s.get("dashboard_objects"):
        lines.append(
            "- **Dashboard objects:** %d non-worksheet object(s) captured as placement targets "
            "(expected extras; not scored)." % s["dashboard_objects"])
    if s["unmatched_worksheets"]:
        lines.append("- **Unmatched worksheets:** %s" % ", ".join(s["unmatched_worksheets"]))
    if s.get("remodel_rename_suspected"):
        lines.append(
            "- **Remodel/rename suspected:** %d visual(s) match on type+position but not field "
            "names — likely a faithful field remodel; confirm via the value/image tiers." %
            s["remodel_rename_suspected"])
    if s.get("fields_alias_resolved"):
        lines.append(
            "- **Aliases resolved:** %d emitted field ref(s) mapped back to their Tableau caption "
            "(star-schema rename seen through)." % s["fields_alias_resolved"])
    lines.append("")
    lines.append("| Worksheet | Visual type | Score | Band | Type | Missing | Extra |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in report["visuals"]:
        type_cell = r["type_note"]
        if r.get("diagnosis") == _REMODEL_DIAGNOSIS:
            type_cell += " · remodel/rename?"
        lines.append("| %s | %s | %.3f | %s | %s | %s | %s |" % (
            r["worksheet"], r["visual_type"], r["score"], r["band"],
            type_cell,
            ", ".join(r["fields_missing"]) or "-",
            ", ".join(r["fields_extra"]) or "-"))
    pv = report.get("per_visual")
    if pv is not None:
        lines.append("")
        lines.append("## Per-visual fidelity (advisory loop objective)")
        t = pv.get("tally") or {}
        scoped = pv.get("scoped_to")
        scope_txt = (", ".join(scoped) if isinstance(scoped, list) else str(scoped))
        lines.append("- **Objective score:** %s — %d visual(s) scored (scope: %s)" % (
            pv.get("objective_score"), pv.get("scored_visuals", 0), scope_txt))
        lines.append("- **Tally:** REPRODUCED %d · PARTIAL %d · DEGRADED %d · MISSING %d" % (
            t.get(STATE_REPRODUCED, 0), t.get(STATE_PARTIAL, 0),
            t.get(STATE_DEGRADED, 0), t.get(STATE_MISSING, 0)))
        lines.append("")
        lines.append("| Worksheet | State | Visual type | Reason |")
        lines.append("|---|---|---|---|")
        for r in pv.get("verdicts", []):
            lines.append("| %s | %s | %s | %s |" % (
                r["worksheet"], r["state"], r.get("visual_type") or "-", r.get("reason") or "-"))
    placed = [r for r in report["visuals"] if r.get("placement")]
    if placed:
        lines.append("")
        lines.append("## Placement (zone fidelity, target-canvas px)")
        lines.append("_Render-free: the Tableau dashboard zone is projected onto the PBI canvas and "
                     "diffed edge-by-edge against the emitted visual's px — no Power BI render._")
        lines.append("")
        lines.append("| Worksheet | Worst edge px | Center px | IoU | Placement |")
        lines.append("|---|---|---|---|---|")
        for r in placed:
            pl = r["placement"]
            if pl.get("pixel_exact"):
                verdict = "pixel-exact"
            elif pl.get("within_tolerance"):
                verdict = "acceptable"
            else:
                verdict = "drifted"
            lines.append("| %s | %s | %s | %s | %s |" % (
                r["worksheet"], pl["max_edge_px"], pl["delta_px"]["center"],
                "-" if pl["iou"] is None else ("%.3f" % pl["iou"]), verdict))
    objs = report.get("dashboard_objects_detail") or []
    if objs:
        lines.append("")
        lines.append("## Non-worksheet dashboard objects (expected extras)")
        lines.append("_Advisory placement targets only — titles, text, legends, parameter controls "
                     "and filter cards. Never scored and never counted against coverage; their "
                     "`target_px` is where the rebuild should place them on the page canvas._")
        lines.append("")
        lines.append("| Kind | Worksheet | Target px (x, y, w, h) | Param |")
        lines.append("|---|---|---|---|")
        for o in objs:
            tp = o.get("target_px")
            box = ("%s, %s, %s, %s" % (tp["x"], tp["y"], tp["w"], tp["h"])) if tp else "-"
            param = o.get("param")
            param_cell = ("`%s`" % param) if param else "-"
            lines.append("| %s | %s | %s | %s |" % (
                o.get("kind") or "?", o.get("worksheet") or "-", box, param_cell))
    if report["slicers"]:
        lines.append("")
        lines.append("## Slicers / filters")
        for sl in report["slicers"]:
            lines.append("- `%s`: %s" % (", ".join(sl["fields"]) or "?", sl["note"]))
    dax = report.get("dax_value")
    if dax is not None:
        lines.append("")
        lines.append("## DAX-value tier (advisory)")
        if not dax.get("available"):
            lines.append("- _unavailable_: %s" % dax.get("reason"))
        else:
            lines.append("- **Value score:** %s (%s) on port %s" % (
                dax.get("value_score"), dax.get("band"), dax["instance"]["port"]))
            lines.append("- **Measures:** %d evaluated, %d errored (of %d)" % (
                dax.get("measures_evaluated", 0), dax.get("measures_errored", 0),
                dax.get("measures_total", 0)))
            for r in dax.get("results", []):
                if not r["ok"]:
                    lines.append("  - ERROR `%s`: %s" % (r["measure"], r["error"]))
    img = report.get("image")
    if img is not None:
        lines.append("")
        lines.append("## Image tier (advisory)")
        if not img.get("available"):
            lines.append("- _unavailable_: %s" % img.get("reason"))
        else:
            lines.append("- **SSIM:** %s (%s)" % (img.get("ssim"), img.get("band")))
            if img.get("acceptance_threshold") is not None:
                verdict = "MEETS target" if img.get("meets_target") else "BELOW target"
                lines.append("- **Acceptance floor:** %s -> %s" %
                             (img.get("acceptance_threshold"), verdict))
            if img.get("regions"):
                lines.append("- **Per-zone SSIM:**")
                for z in img["regions"]:
                    flag = "meets" if z.get("meets_target") else "below"
                    lines.append("  - %s: %s (%s, %s target)" %
                                 (z.get("name"), z.get("ssim"), z.get("band"), flag))
                if img.get("regions_mean_ssim") is not None:
                    lines.append("  - _zone mean:_ %s" % img.get("regions_mean_ssim"))
    ren = report.get("pbi_render")
    if ren is not None and not ren.get("available"):
        lines.append("")
        lines.append("## Power BI render (bridge)")
        lines.append("- _render unavailable_: %s" % ren.get("reason"))
    val = report.get("pbir_validation")
    if val is not None:
        lines.append("")
        lines.append("## PBIR validation (first-party, advisory pre-gate)")
        if not val.get("available"):
            lines.append("- _unavailable_: %s" % val.get("reason"))
        else:
            verdict = {True: "valid", False: "INVALID", None: "unknown"}.get(val.get("valid"))
            lines.append("- **Schema:** %s — %d error(s), %d warning(s)" % (
                verdict, val.get("error_count", 0), val.get("warning_count", 0)))
            for d in val.get("diagnostics", []):
                if d.get("severity") != "error":
                    continue
                loc = d.get("file") or ""
                if d.get("json_path"):
                    loc = ("%s %s" % (loc, d["json_path"])).strip()
                lines.append("  - ERROR %s%s" % (d.get("message", ""),
                                                 (" (%s)" % loc) if loc else ""))
    openrec = report.get("openability")
    if openrec is not None:
        lines.append("")
        lines.append("## Openability gate (Gate 0, TOM TmdlSerializer)")
        if not openrec.get("available"):
            lines.append("- _unavailable_: %s" % openrec.get("reason"))
        elif openrec.get("openable"):
            lines.append("- ✅ **OPENS** — %s table(s), %s measure(s), %s relationship(s) "
                         "deserialize cleanly." % (
                             openrec.get("tables"), openrec.get("measures"),
                             openrec.get("relationships")))
            lines.append("- _Same TMDL grammar Power BI Desktop loads and Microsoft Fabric ingests; "
                         "a faithful pre-flight for both, offline._")
        else:
            loc = openrec.get("error_file") or "?"
            if openrec.get("error_line") is not None:
                loc = "%s line %s" % (loc, openrec.get("error_line"))
            lines.append("- ⛔ **BLOCKED** — model does not deserialize (%s)" % loc)
            lines.append("  - %s" % openrec.get("error"))
    meta = report.get("metadata")
    if meta is not None:
        lines.append("")
        lines.append("## Metadata / binding fidelity (advisory, deterministic)")
        if not meta.get("available"):
            lines.append("- _unavailable_: %s" % meta.get("reason"))
        else:
            sc = meta.get("scores") or {}
            lines.append("- **Scores:** metadata %s (%s) · binding %s (%s) · overall %s (%s)" % (
                sc.get("metadata"), sc.get("metadata_band"), sc.get("binding"),
                sc.get("binding_band"), sc.get("overall"), sc.get("overall_band")))
            lines.append("- **Bindings:** %d/%d expression objects fully resolve; %d unresolved "
                         "reference(s)." % (
                             meta.get("objects_resolved", 0), meta.get("objects_total", 0),
                             len(meta.get("unresolved_bindings", []))))
            lines.append("- **Coverage:** %d datatype drift · %d missing source column(s) · %d extra "
                         "model column(s)." % (
                             len(meta.get("datatype_drift", [])),
                             len(meta.get("missing_source_columns", [])),
                             len(meta.get("extra_model_columns", []))))
            for u in meta.get("unresolved_bindings", []):
                wf = " [window-fn]" if u.get("window_function") else ""
                lines.append("  - ⛔ UNRESOLVED `%s` in %s `%s` (%s) — %s%s" % (
                    u.get("ref"), u.get("table"), u.get("object"), u.get("kind"),
                    u.get("reason"), wf))
            for d in meta.get("datatype_drift", []):
                lines.append("  - ⚠ DRIFT %s[%s]: emitted `%s` vs Tableau `%s` (expected `%s`)" % (
                    d.get("table"), d.get("column"), d.get("emitted_type"),
                    d.get("tableau_type"), d.get("expected_type")))
            ctw = meta.get("cross_table_windows", [])
            if sc.get("window_consistency") is not None:
                lines.append("- **Window consistency:** %s (%s) — %d window object(s), %d "
                             "cross-table ORDERBY finding(s)." % (
                                 sc.get("window_consistency"), sc.get("window_consistency_band"),
                                 meta.get("window_objects_total", 0), len(ctw)))
            for f in ctw:
                lines.append("  - ⛔ CROSS-TABLE WINDOW in %s `%s` (%s): ORDERBY uses `%s` but the "
                             "window aggregates/partitions over %s — %s" % (
                                 f.get("table"), f.get("object"), f.get("kind"),
                                 f.get("orderby_table"), f.get("anchor_tables"), f.get("reason")))
    llm = report.get("llm_assist")
    if llm is not None:
        lines.append("")
        lines.append("## LLM-assist (advisory adjudication — the agent IS the judgment model)")
        if not llm.get("available"):
            lines.append("- _awaiting agent_: %s" % llm.get("reason"))
            b = llm.get("bundle") or {}
            bs = b.get("summary") or {}
            lines.append("- Bundle ready: %s item(s), %s to review, %s blocker(s), %s image(s) to "
                         "view. Read `prompt`, judge, re-invoke with answers." % (
                             bs.get("items"), bs.get("to_review"), bs.get("blockers"),
                             bs.get("image_refs")))
        else:
            lines.append("- **Effective verdict:** %s (deterministic: %s; agent: %s; gate_locked: "
                         "%s)" % (llm.get("effective_verdict"), llm.get("deterministic_verdict"),
                                  llm.get("llm_verdict"), llm.get("gate_locked")))
            if llm.get("summary"):
                lines.append("- %s" % llm.get("summary"))
            for note in llm.get("notes", []):
                lines.append("  - _%s_" % note)
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
    ap.add_argument("--candidate-records", default=None,
                    help="Optional JSON (a migrate_twb_to_pbir candidate_records list/result, or a "
                         "flat {emitted ref: Tableau caption} map) used to resolve faithful "
                         "star-schema field renames before name overlap.")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    ap.add_argument("--out", default=None, help="Write output here instead of stdout.")
    # Optional Tier-2 (DAX-value, needs local Power BI Desktop):
    ap.add_argument("--dax", action="store_true",
                    help="Run the optional DAX-value tier against a live Power BI Desktop instance.")
    ap.add_argument("--dax-port", type=int, default=None,
                    help="Explicit Analysis Services port (else auto-discovered when only one is live).")
    ap.add_argument("--expected", default=None,
                    help="Optional JSON file of {measure: expected_value} for value comparison.")
    # Optional Tier-3 (image, needs numpy + Pillow):
    ap.add_argument("--image-ref", default=None, help="Tableau reference PNG for the image tier.")
    ap.add_argument("--image-ref-source", default=None,
                    help="Resolve the Tableau reference PNG from a local source instead of "
                         "--image-ref: a folder of exported PNGs, a single .png, or a .twbx whose "
                         "embedded Image/ objects are extracted. Pair with --image-ref-name.")
    ap.add_argument("--image-ref-name", default=None,
                    help="Worksheet/view name to pick from --image-ref-source (tolerant filename "
                         "match); omit when the source holds a single PNG.")
    ap.add_argument("--image-cand", default=None, help="Power BI render PNG for the image tier.")
    ap.add_argument("--image-threshold", type=float, default=DEFAULT_ACCEPTANCE_SSIM,
                    help="Advisory SSIM acceptance floor a faithful rebuild should clear "
                         "(default %(default)s).")
    ap.add_argument("--image-auto-regions", action="store_true",
                    help="Derive per-worksheet image crop regions from the dashboard layout "
                         "(crops each render by its own zone positions; no hand-tuned boxes).")
    ap.add_argument("--image-render", default=None,
                    help="Capture the Power BI candidate PNG locally from this .pbip via the "
                         "powerbi-desktop bridge instead of passing --image-cand.")
    ap.add_argument("--image-render-page", default=None,
                    help="PBIR page id to render (e.g. ReportSection...); default renders all and "
                         "uses the first.")
    ap.add_argument("--image-render-scale", type=int, default=DEFAULT_SCREENSHOT_SCALE,
                    help="Screenshot scale for the bridge render (default %(default)s).")
    # Optional first-party PBIR validation pre-gate (needs powerbi-report-author):
    ap.add_argument("--validate", action="store_true",
                    help="Run the first-party PBIR validation pre-gate (powerbi-report-author "
                         "validate); ADDITIVE -- it never changes the structural aggregate.")
    ap.add_argument("--openability", action="store_true",
                    help="Run Gate 0: the TOM TmdlSerializer openability pre-flight over the emitted "
                         "*.SemanticModel/definition. A non-deserializing model dominates the verdict "
                         "(blocked). Lazy + guarded: degrades to {available: False} without TOM.")
    ap.add_argument("--tabular-editor-dir", default=None,
                    help="Directory holding the TOM assemblies (e.g. a Tabular Editor install) for "
                         "Gate 0. Implies --openability. Auto-discovered when omitted.")
    ap.add_argument("--model-dir", default=None,
                    help="Override the semantic-model folder Gate 0 inspects (defaults to the "
                         "*.SemanticModel/definition resolved from report_dir).")
    ap.add_argument("--metadata", action="store_true",
                    help="Run the deterministic metadata/binding fidelity tier: emitted TMDL columns/"
                         "measures vs the Tableau source schema (datatype drift, coverage) plus static "
                         "binding resolution of every DAX column/measure reference. ADDITIVE -- it "
                         "never feeds the combined headline. Offline, stdlib-only.")
    ap.add_argument("--llm", action="store_true",
                    help="Build the advisory LLM-assist adjudication bundle + agent prompt (no "
                         "network/key -- the agent running this skill is the judgment model).")
    ap.add_argument("--llm-answers", default=None,
                    help="Path to a JSON file of the agent's adjudication answers to fold back into "
                         "the LLM-assist record. Implies --llm.")
    ap.add_argument("--per-visual", action="store_true",
                    help="Attach the per-visual REPRODUCED/PARTIAL/DEGRADED/MISSING verdict layer "
                         "(the migration loop's objective function). ADDITIVE -- it never feeds the "
                         "structural aggregate or the combined headline.")
    ap.add_argument("--on-view", default=None,
                    help="Comma-separated Tableau worksheet names to scope the per-visual verdicts to "
                         "(the dashboard's on-view sheets); off-view sheets/calcs are ignored. "
                         "Implies --per-visual.")
    args = ap.parse_args(argv)

    dax_options = None
    if args.dax or args.dax_port is not None:
        expected = None
        if args.expected and os.path.isfile(args.expected):
            expected = _read_json(args.expected)
        dax_options = {"port": args.dax_port, "expected": expected}
    image_options = None
    if args.image_ref or args.image_ref_source or args.image_cand or args.image_render:
        image_options = {"reference_png": args.image_ref, "candidate_png": args.image_cand,
                         "acceptance_threshold": args.image_threshold,
                         "auto_regions": args.image_auto_regions}
        if args.image_ref_source:
            image_options["reference_source"] = args.image_ref_source
            image_options["reference_name"] = args.image_ref_name
        if args.image_render:
            image_options["render_pbip"] = args.image_render
            image_options["render_page_id"] = args.image_render_page
            image_options["render_options"] = {"scale": args.image_render_scale}

    openability_options = None
    if args.openability or args.tabular_editor_dir or args.model_dir:
        openability_options = {}
        if args.tabular_editor_dir:
            openability_options["tabular_editor_dir"] = args.tabular_editor_dir
        if args.model_dir:
            openability_options["model_dir"] = args.model_dir
    llm_options = None
    if args.llm or args.llm_answers:
        llm_options = {}
        if args.llm_answers and os.path.isfile(args.llm_answers):
            llm_options["answers"] = _read_json(args.llm_answers)

    metadata_options = None
    if args.metadata:
        metadata_options = {}
        if args.model_dir:
            metadata_options["model_dir"] = args.model_dir

    per_visual_options = None
    if args.per_visual or args.on_view:
        per_visual_options = {}
        if args.on_view:
            per_visual_options["on_view"] = [s.strip() for s in args.on_view.split(",") if s.strip()]

    report = run_oracle(args.twb, args.report_dir, args.engine_report,
                        dax_options=dax_options, image_options=image_options,
                        candidate_records_path=args.candidate_records,
                        validate=args.validate,
                        openability_options=openability_options,
                        llm_options=llm_options,
                        metadata_options=metadata_options,
                        per_visual_options=per_visual_options)
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
