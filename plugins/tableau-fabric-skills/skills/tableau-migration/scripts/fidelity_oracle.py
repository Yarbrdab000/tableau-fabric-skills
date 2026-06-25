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
        # A Text mark with no dimensions is a card, not a table.
        if fam == FAM_TABLE and not dims and not uses_measure_values:
            return FAM_CARD, True
        if fam == FAM_TABLE and uses_measure_values and not dims:
            return FAM_CARD, True
        return fam, True
    # Automatic: infer from shelf shape (Tableau's own default heuristic, applied conservatively).
    # A continuous (green) date axis under an Automatic mark is Tableau's default line chart -- the
    # implicit measure (e.g. COUNT) or an explicit one is drawn as a line over the continuous date.
    if any(_is_continuous_date_dim(d) for d in dims) and (measures or len(dims) == 1):
        return FAM_LINE, True
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
    # Advisory diagnosis: strong type/position agreement with low field-name overlap is the
    # signature of a faithful field remodel/rename, not a divergent rebuild (see constants above).
    pos_val = parts.get("position")
    diagnosis = None
    if (type_s >= _REMODEL_TYPE_MIN and field_s < _REMODEL_FIELDS_MAX
            and (pos_val is None or pos_val >= _REMODEL_POSITION_MIN)):
        diagnosis = _REMODEL_DIAGNOSIS
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
        "diagnosis": diagnosis,
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

    remodel_suspected = [r for r in visual_results
                         if r.get("diagnosis") == _REMODEL_DIAGNOSIS]

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
        },
        "visuals": sorted(visual_results, key=lambda r: r["score"]),
        "slicers": slicer_results,
        "extra_visuals_detail": extra_visuals,
        "notes": notes,
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


def _normalize_expected(expected):
    """Normalize an ``expected`` map into a list of value checks.

    Supports two shapes (mixable in one map):

    * **flat** ``{measure_name: expected_value}`` -- a model-level check (no filter context).
    * **rich** ``{label: {"measure": name, "expected": value, "filter": dax}}`` -- a per-view check
      whose ``filter`` (caller-supplied DAX) reproduces that view's filter context. ``measure``
      defaults to ``label``; ``value`` is accepted as an alias for ``expected``.

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
            })
        else:
            checks.append({"label": key, "measure": key, "expected": val, "filter": None})
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
        conn = AdomdConnection("Data Source=%s:%d" % (host, p))
        conn.Open()
        return conn

    chosen = port
    if chosen is None:
        discovered = discover_pbi_instances(workspace_roots)
        live = []
        for inst in discovered:
            try:
                c = _connect(inst["port"]); c.Close(); live.append(inst)
            except Exception:  # noqa: BLE001 -- stale/closed instance
                continue
        if len(live) == 1:
            chosen = live[0]["port"]
        elif not live:
            return {"tier": "dax-value", "available": False,
                    "reason": "no live Power BI Desktop Analysis Services instance found",
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
                ev = _evaluate_measure(conn, chk["measure"], chk.get("filter"))
                actual = ev["value"] if ev["ok"] else None
                cmp = _compare_value(chk["label"], chk["expected"], actual, tolerance)
                if chk["measure"] != chk["label"]:
                    cmp["measure_name"] = chk["measure"]
                if chk.get("filter"):
                    cmp["filter"] = chk["filter"]
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
# Top-level convenience + CLI
# =====================================================================================
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
    if not tiers:
        return None
    wsum = sum(COMBINED_WEIGHTS[k] for k in tiers)
    combined = sum(COMBINED_WEIGHTS[k] * v for k, v in tiers.items()) / wsum if wsum else None
    confidence = {3: "high", 2: "medium", 1: "low"}.get(len(tiers), "low")
    return {
        "combined_score": round(combined, 4) if combined is not None else None,
        "band": _band(combined) if combined is not None else None,
        "confidence": confidence,
        "contributing_tiers": sorted(tiers),
        "tier_scores": {k: round(v, 4) for k, v in tiers.items()},
        "weights": {k: COMBINED_WEIGHTS[k] for k in tiers},
        "advisory": True,
        "note": ("Advisory headline fusing the tiers that ran (weights renormalized over those "
                 "present); confidence reflects how many tiers backed it, not their mutual "
                 "agreement -- a low image score pulling the headline down IS the signal."),
    }


def run_oracle(twb_path, report_dir, engine_report_path=None,
               dax_options=None, image_options=None):
    """Read both sides and score them. Returns the advisory report dict.

    The structural tier always runs. The optional value/image tiers run only when their options are
    supplied, and each attaches its own ``{available, ...}`` record without ever failing the run.
    """
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
    if dax_options is not None:
        report["dax_value"] = dax_value_tier(report_dir=report_dir, **dax_options)
    if image_options is not None:
        opts = dict(image_options)
        if opts.pop("auto_regions", False) and not opts.get("regions"):
            derived = regions_from_layout(twb, pbir)
            if derived:
                opts["regions"] = derived
        report["image"] = image_tier(**opts)
    combined = _combined_fidelity(report)
    if combined is not None:
        report["combined_fidelity"] = combined
    return report


def render_markdown(report):
    """Render the advisory report as a compact Markdown summary."""
    s = report["summary"]
    lines = ["# Fidelity Oracle (advisory, structural)", ""]
    cf = report.get("combined_fidelity")
    if cf is not None:
        lines.append("- **Combined fidelity:** %s (%s) — confidence %s [%s]" % (
            cf["combined_score"], cf["band"], cf["confidence"],
            ", ".join(cf["contributing_tiers"])))
    lines.append("- **Aggregate:** %s (%s)" % (
        s["aggregate_score"], s["aggregate_band"]))
    lines.append("- **Mean / worst visual:** %s / %s" % (
        s["mean_visual_score"], s["worst_visual_score"]))
    lines.append("- **Coverage:** %s (%d/%d worksheets matched)" % (
        s["coverage"], s["matched_visuals"], s["source_worksheets"]))
    if s["unmatched_worksheets"]:
        lines.append("- **Unmatched worksheets:** %s" % ", ".join(s["unmatched_worksheets"]))
    if s.get("remodel_rename_suspected"):
        lines.append(
            "- **Remodel/rename suspected:** %d visual(s) match on type+position but not field "
            "names — likely a faithful field remodel; confirm via the value/image tiers." %
            s["remodel_rename_suspected"])
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
    # Optional Tier-2 (DAX-value, needs local Power BI Desktop):
    ap.add_argument("--dax", action="store_true",
                    help="Run the optional DAX-value tier against a live Power BI Desktop instance.")
    ap.add_argument("--dax-port", type=int, default=None,
                    help="Explicit Analysis Services port (else auto-discovered when only one is live).")
    ap.add_argument("--expected", default=None,
                    help="Optional JSON file of {measure: expected_value} for value comparison.")
    # Optional Tier-3 (image, needs numpy + Pillow):
    ap.add_argument("--image-ref", default=None, help="Tableau reference PNG for the image tier.")
    ap.add_argument("--image-cand", default=None, help="Power BI render PNG for the image tier.")
    ap.add_argument("--image-threshold", type=float, default=DEFAULT_ACCEPTANCE_SSIM,
                    help="Advisory SSIM acceptance floor a faithful rebuild should clear "
                         "(default %(default)s).")
    ap.add_argument("--image-auto-regions", action="store_true",
                    help="Derive per-worksheet image crop regions from the dashboard layout "
                         "(crops each render by its own zone positions; no hand-tuned boxes).")
    args = ap.parse_args(argv)

    dax_options = None
    if args.dax or args.dax_port is not None:
        expected = None
        if args.expected and os.path.isfile(args.expected):
            expected = _read_json(args.expected)
        dax_options = {"port": args.dax_port, "expected": expected}
    image_options = None
    if args.image_ref or args.image_cand:
        image_options = {"reference_png": args.image_ref, "candidate_png": args.image_cand,
                         "acceptance_threshold": args.image_threshold,
                         "auto_regions": args.image_auto_regions}

    report = run_oracle(args.twb, args.report_dir, args.engine_report,
                        dax_options=dax_options, image_options=image_options)
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
