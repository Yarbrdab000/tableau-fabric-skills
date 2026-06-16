"""Tableau workbook ``.twb`` viz grammar -> Power BI **PBIR** wireframe (offline, stdlib-only).

This is the v2 *report* half of the migration skill (the v1 cores rebuild the semantic
model). It reads a Tableau workbook's viz grammar -- worksheets (marks, shelves, encodings,
filters) and dashboards (zones) -- into a normalized intermediate representation (IR), then
emits a minimal **PBIR** (Power BI Enhanced Report) definition whose visuals bind to the
SAME names the v1 model generator produces:

* a model **table** display name == the Tableau ``<relation name=...>`` (the visual's ``Entity``),
* a model **column** name == ``clean_col(<remote source name>)`` (the visual's ``Property``),
* a model **measure** name == the Tableau calculated-field caption, in the ``_Measures`` table.

The binding is resolved from the workbook's OWN embedded ``<datasources>`` (the ``.twb``
carries the full ``<relation>`` + ``<metadata-records>`` tree, exactly like a ``.tds``), so a
field's internal id ``[Sales]`` -> remote ``Sales`` -> ``clean_col`` -> model column is exact
even when the field was renamed in the workbook. When a workbook ships without that metadata,
binding falls back to the field caption and a structured ``warnings[]`` entry is recorded -- a
wrong/over-confident visual is never emitted silently.

Scope (small, correct slice; everything else -> ``warnings[]``):

* marks -> visual types: ``Bar`` -> clustered column/bar, ``Line`` -> line, ``Text`` ->
  table (``tableEx``) or matrix (``pivotTable``). Anything else is ``unsupported``.
* categorical / date filters -> a slicer visual (a wireframe placeholder; Tableau filter
  scope is not identical to a Power BI slicer -- see ``resources/viz-rebuild.md``).

Only the Microsoft PBIR JSON schemas (report definition format) and the public Tableau
workbook XML structure were used to build this; it is original, deterministic, and offline.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

try:  # package or scripts-on-path (mirrors the other cores)
    from .tmdl_generate import clean_col
except ImportError:
    from tmdl_generate import clean_col


# -- PBIR schema URLs ----------------------------------------------------------
_S = "https://developer.microsoft.com/json-schemas/fabric/item/report"
SCHEMA_DEFINITION_PROPERTIES = f"{_S}/definitionProperties/2.0.0/schema.json"
SCHEMA_VERSION = f"{_S}/definition/versionMetadata/1.0.0/schema.json"
SCHEMA_REPORT = f"{_S}/definition/report/1.0.0/schema.json"
SCHEMA_PAGES = f"{_S}/definition/pagesMetadata/1.0.0/schema.json"
SCHEMA_PAGE = f"{_S}/definition/page/1.0.0/schema.json"
SCHEMA_VISUAL = f"{_S}/definition/visualContainer/1.0.0/schema.json"
SCHEMA_PLATFORM = ("https://developer.microsoft.com/json-schemas/fabric/"
                   "gitIntegration/platformProperties/2.0.0/schema.json")

# Field-parameter (swap) report schema set. A visual that CONSUMES a field parameter must encode it
# as an *expansion* -- a seed projection per slot plus a sibling ``fieldParameters`` array binding
# each slot index to the parameter's display column. Omitting that block makes Power BI render the
# parameter option *labels* as static text instead of swapping the field. The expansion is only
# honored at the newer schema versions a current Power BI Desktop stamps for such a report (verified
# against a Desktop-authored oracle), so the self-service swap report pins them explicitly rather
# than reusing the thin-shell 1.0.0 set above.
SCHEMA_REPORT_FP = f"{_S}/definition/report/3.3.0/schema.json"
SCHEMA_PAGES_FP = f"{_S}/definition/pagesMetadata/1.1.0/schema.json"
SCHEMA_PAGE_FP = f"{_S}/definition/page/2.1.0/schema.json"
SCHEMA_VISUAL_FP = f"{_S}/definition/visualContainer/2.10.0/schema.json"

MEASURES_TABLE = "_Measures"
PAGE_WIDTH = 1280
PAGE_HEIGHT = 720

# -- Tableau mark class -> internal visual-type enum ---------------------------
# A small, deliberately conservative enum. The shelf layout decides bar vs column
# and table vs matrix; anything outside this set becomes ``unsupported``.
VT_COLUMN = "column"      # clusteredColumnChart (vertical bars: dim on x / cols)
VT_BAR = "bar"            # clusteredBarChart   (horizontal bars: dim on y / rows)
VT_LINE = "line"          # lineChart
VT_TABLE = "table"        # tableEx
VT_MATRIX = "matrix"      # pivotTable
VT_SCATTER = "scatter"    # scatterChart (X/Y measures disaggregated by a dimension)
VT_CARD = "card"          # card (1 measure) / multiRowCard (>=2 measures), no dimension
VT_PIE = "pie"            # pieChart (angle measure + legend dimension)
VT_FILLED_MAP = "filled_map"  # filledMap (choropleth: geo Location + measure Color)
VT_MAP = "map"            # map (symbol/bubble: geo Location + measure Size/Color)
VT_UNSUPPORTED = "unsupported"

_VT_TO_PBIR = {
    VT_COLUMN: "clusteredColumnChart",
    VT_BAR: "clusteredBarChart",
    VT_LINE: "lineChart",
    VT_TABLE: "tableEx",
    VT_MATRIX: "pivotTable",
    VT_SCATTER: "scatterChart",
    VT_PIE: "pieChart",
    VT_FILLED_MAP: "filledMap",
    VT_MAP: "map",
}

# Mark classes for geometry-backed / custom-spatial maps we deliberately defer (basics only:
# filled + symbol map). These degrade to a structured warning rather than a guessed visual.
_DEFER_MAP_MARKS = {"multipolygon", "polygon", "density", "heatmap"}

# Tableau derivation -> Power BI QueryAggregateFunction code.
_AGG_FUNC = {
    "Sum": 0, "Avg": 1, "Average": 1, "CntD": 2, "CountD": 2,
    "Min": 3, "Max": 4, "Count": 5, "Cnt": 5, "Median": 6,
}
# Aggregations restricted to numeric source columns (others -> warn + skip).
_NUMERIC_AGGS = {"Sum", "Avg", "Average", "Median"}
_NUMERIC_TYPES = {"integer", "real", "decimal", "double"}
_DATE_TYPES = {"date", "datetime"}
_DATE_PARTS = {
    "Year", "Quarter", "Month", "Week", "Weekday", "Day", "Hour", "Minute",
    "Second", "ISO-Year", "ISO-Quarter", "ISO-Week", "ISO-Weekday",
    "MonthYear", "DayOfYear",
}
# Tableau internal pseudo-fields that have no model binding.
_SPECIAL_FIELDS = {":Measure Names", "Measure Names", "Measure Values",
                   ":Measure Values", "Number of Records", "Multiple Values"}

_GEO_ROLE_RE = re.compile(r"\[([^\]]+)\]")


def _geo_area(semantic_role):
    """Map a Tableau ``semantic-role`` to its geographic area name, or ``None``.

    Tableau tags a geographic column with ``semantic-role='[State].[Name]'`` /
    ``[City].[Name]`` / ``[Country].[ISO3166_2]`` / ``[ZipCode].[Name]`` etc. The area name is
    the first bracketed token. The generated ``[Latitude]`` / ``[Longitude]`` point roles are
    deliberately excluded: a geographic *area* dimension (not lat/lon) is the map trigger.
    """
    if not semantic_role:
        return None
    m = _GEO_ROLE_RE.match(semantic_role.strip())
    if not m:
        return None
    area = m.group(1)
    if area.lower() in ("latitude", "longitude"):
        return None
    return area


def tableau_type_to_simple(local_type):
    """Map a Tableau ``<local-type>`` / column ``datatype`` to a coarse type bucket."""
    t = (local_type or "").lower().strip()
    return {
        "integer": "integer", "real": "real", "string": "string",
        "boolean": "boolean", "date": "date", "datetime": "datetime",
    }.get(t, t or None)


# -- XML helpers (namespace-agnostic; .twb is normally namespace-free) ----------
def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _findall_local(elem, name):
    return [c for c in elem.iter() if _local(c.tag) == name]


def _children_local(elem, name):
    return [c for c in list(elem) if _local(c.tag) == name]


def _first(elem, name):
    got = _children_local(elem, name)
    return got[0] if got else None


def _strip_brackets(name):
    if name and name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return name


_ITEM_PAIR = re.compile(r"^\[(?P<schema>[^\[\]]+)\]\.\[(?P<item>[^\[\]]+)\]$")
_ITEM_ONE = re.compile(r"^\[(?P<item>[^\[\]]+)\]$")
_TOKEN_RE = re.compile(r"\[[^\[\]]*\]\.\[[^\[\]]*\]")


def _parse_item(raw):
    """Extract the table item from a relation ``table`` attribute (``[schema].[item]``)."""
    if not raw:
        return None
    raw = raw.strip()
    m = _ITEM_PAIR.match(raw) or _ITEM_ONE.match(raw)
    return m.group("item") if m else None


def _split_token(token):
    """Split a shelf/encoding pill ``[datasource].[field]`` into (datasource, field)."""
    inner = token[1:-1]  # drop outer [ ]
    if "].[" not in inner:
        return None, None
    ds, field = inner.split("].[", 1)
    return ds, field


def _sanitize(text):
    """A deterministic PBIR object name: word chars / hyphen only, <= 50 chars."""
    base = re.sub(r"[^0-9A-Za-z_-]+", "", (text or "").replace(" ", ""))
    h = hashlib.md5((text or "").encode("utf-8")).hexdigest()[:8]
    name = (base[:32] + h) if base else ("v" + h)
    return name[:50]


# -- workbook datasource index (the binding contract) --------------------------
def _build_field_index(root):
    """Index the workbook's embedded datasources -> exact model binding per field.

    Returns ``(index, ds_caption_by_name)`` where ``index[(ds_name, field_id)]`` is
    ``{"entity": <relation name>, "property": clean_col(remote), "datatype": <bucket>}``.
    ``field_id`` is the field's internal id (the metadata ``local-name`` / column ``name``
    without brackets), so the binding survives a workbook-side rename of the caption.
    """
    index = {}
    ds_caption = {}
    holders = _children_local(root, "datasources")
    datasources = []
    for h in holders:
        datasources.extend(_children_local(h, "datasource"))
    if not datasources and _local(root.tag) == "datasource":
        datasources = [root]

    for ds in datasources:
        dsn = ds.get("name")
        ds_caption[dsn] = ds.get("caption") or dsn
        # relation item -> relation name (the model table display name)
        item_to_rel = {}
        for rel in _findall_local(ds, "relation"):
            rtype = (rel.get("type") or "").lower()
            if rtype in ("join", "union", "collection"):
                continue
            item = _parse_item(rel.get("table")) or _strip_brackets(rel.get("name") or "")
            if item:
                item_to_rel.setdefault(item, rel.get("name") or item)
        for rec in _findall_local(ds, "metadata-record"):
            if (rec.get("class") or "").lower() != "column":
                continue

            def _txt(tag):
                els = _children_local(rec, tag)
                return els[0].text if els and els[0].text is not None else None

            remote = (_txt("remote-name") or "").strip()
            local = _strip_brackets((_txt("local-name") or "").strip())
            parent = _strip_brackets((_txt("parent-name") or "").strip())
            if not remote or not local:
                continue
            entity = item_to_rel.get(parent, parent or ds_caption[dsn])
            index[(dsn, local)] = {
                "entity": entity,
                "property": clean_col(remote),
                "datatype": tableau_type_to_simple(_txt("local-type")),
            }
    return index, ds_caption


# -- worksheet parsing ---------------------------------------------------------
def _parse_dependencies(view):
    """Read ``<datasource-dependencies>`` -> (base_cols, instances) keyed by (ds, id)."""
    base_cols = {}
    instances = {}
    for dep in _findall_local(view, "datasource-dependencies"):
        dsn = dep.get("datasource")
        for c in _children_local(dep, "column"):
            cid = _strip_brackets(c.get("name") or "")
            if not cid:
                continue
            base_cols[(dsn, cid)] = {
                "caption": c.get("caption") or cid,
                "role": (c.get("role") or "").lower(),
                "datatype": (c.get("datatype") or "").lower(),
                "is_calc": bool(_children_local(c, "calculation")),
                "geo_role": c.get("semantic-role") or "",
            }
        for ci in _children_local(dep, "column-instance"):
            iid = _strip_brackets(ci.get("name") or "")
            if not iid:
                continue
            instances[(dsn, iid)] = {
                "column": _strip_brackets(ci.get("column") or ""),
                "derivation": ci.get("derivation") or "None",
            }
    return base_cols, instances


def _resolve_field(ds, field_id, base_cols, instances, index, ds_caption,
                   worksheet, warnings):
    """Resolve one shelf/encoding pill into an IR field dict (or ``None`` if it must be dropped).

    Records a structured warning whenever a token cannot be bound to a model field, or is
    bound through a non-authoritative fallback, so the wireframe never claims a binding it
    cannot stand behind.
    """
    if not field_id or field_id in _SPECIAL_FIELDS or field_id.startswith(":"):
        warnings.append(_warn("worksheet", worksheet,
                              f"field '{field_id}' has no model binding (skipped)"))
        return None

    # Tableau auto-generated helpers (Latitude/Longitude/Geometry "(generated)") carry no model
    # binding; drop them quietly. Their presence is read separately as a map signal.
    if field_id.endswith("(generated)"):
        return None

    inst = instances.get((ds, field_id))
    if inst:
        base_id, deriv = inst["column"], inst["derivation"]
    else:
        base_id, deriv = field_id, "None"

    base = base_cols.get((ds, base_id))
    if base is None:
        warnings.append(_warn("worksheet", worksheet,
                              f"could not resolve field '{base_id}' (skipped)"))
        return None

    caption = base["caption"]
    role = base["role"] or ("measure" if (deriv in _AGG_FUNC) else "dimension")
    datatype = (tableau_type_to_simple(base["datatype"])
                or (index.get((ds, base_id), {}).get("datatype")))
    is_calc = base["is_calc"]

    bound = index.get((ds, base_id))
    if bound:
        entity, prop = bound["entity"], bound["property"]
        if not datatype:
            datatype = bound["datatype"]
    elif is_calc:
        entity, prop = MEASURES_TABLE, caption
    else:
        entity, prop = ds_caption.get(ds, ds), clean_col(caption)
        warnings.append(_warn(
            "worksheet", worksheet,
            f"field '{caption}' bound by caption fallback (no datasource metadata); "
            f"verify it matches model table/column names"))

    field = {
        "caption": caption, "field_id": base_id, "instance": field_id,
        "role": role, "datatype": datatype, "is_calc": is_calc,
        "derivation": deriv, "aggregation": None,
        "entity": entity, "property": prop,
        "binding": None, "kind": None,
        "geo_area": _geo_area(base.get("geo_role", "")) if role != "measure" else None,
    }

    # measure calc: only valid in a value role; an axis role is flagged + dropped later.
    if is_calc and bound is None:
        field["binding"] = "measure"
        field["kind"] = "value"
        return field

    if deriv in _AGG_FUNC:
        if deriv in _NUMERIC_AGGS and datatype not in _NUMERIC_TYPES:
            warnings.append(_warn(
                "worksheet", worksheet,
                f"aggregation '{deriv}' on non-numeric field '{caption}' (skipped)"))
            return None
        if deriv in ("Min", "Max") and datatype not in (_NUMERIC_TYPES | _DATE_TYPES):
            warnings.append(_warn(
                "worksheet", worksheet,
                f"aggregation '{deriv}' on field '{caption}' of type "
                f"'{datatype}' (skipped)"))
            return None
        field["aggregation"] = deriv
        field["binding"] = "aggregation"
        field["kind"] = "value"
        return field

    if deriv in _DATE_PARTS or deriv.startswith("Trunc"):
        warnings.append(_warn(
            "worksheet", worksheet,
            f"date part '{deriv}' on '{caption}' approximated as a plain date column "
            f"(grain not applied)"))
        field["binding"] = "column"
        field["kind"] = "category"
        return field

    if deriv not in ("None", "", None):
        warnings.append(_warn(
            "worksheet", worksheet,
            f"unsupported derivation '{deriv}' on '{caption}' (skipped)"))
        return None

    # plain field: role decides axis vs value placement.
    field["binding"] = "column"
    field["kind"] = "value" if role == "measure" else "category"
    return field


def _resolve_shelf(text, ds_default, base_cols, instances, index, ds_caption,
                   worksheet, warnings):
    fields = []
    for tok in _TOKEN_RE.findall(text or ""):
        ds, fid = _split_token(tok)
        f = _resolve_field(ds or ds_default, fid, base_cols, instances, index,
                           ds_caption, worksheet, warnings)
        if f:
            fields.append(f)
    return fields


def _parse_encodings(pane, ds_default, base_cols, instances, index, ds_caption,
                     worksheet, warnings):
    enc = {"color": None, "size": None, "label": None, "detail": None}
    if pane is None:
        return enc
    holder = _first(pane, "encodings")
    if holder is None:
        return enc
    mapping = {"color": "color", "size": "size", "text": "label",
               "label": "label", "lod": "detail", "level-of-detail": "detail"}
    for child in list(holder):
        role = mapping.get(_local(child.tag))
        if not role:
            continue
        ds, fid = _split_token_attr(child.get("column"))
        f = _resolve_field(ds or ds_default, fid, base_cols, instances, index,
                           ds_caption, worksheet, warnings)
        if f and enc[role] is None:
            enc[role] = f
    return enc


def _has_geometry(pane):
    """True if the marks card carries a ``<geometry>`` encoding (custom spatial geometry).

    A geometry encoding (e.g. ``Geometry (generated)``) is a strong "this view is a map"
    signal, used to disambiguate an ambiguous mark from an ordinary chart.
    """
    if pane is None:
        return False
    holder = _first(pane, "encodings")
    if holder is None:
        return False
    return any(_local(c.tag) == "geometry" for c in list(holder))


def _split_token_attr(value):
    if not value:
        return None, None
    m = _TOKEN_RE.search(value)
    return _split_token(m.group(0)) if m else (None, None)


def _visual_type(mark, dims_rows, dims_cols, meas_rows, meas_cols,
                 enc_dims=(), enc_meas=(), geo_detail=False, map_meas=False,
                 map_signal=False):
    """Pick the internal visual-type enum from the mark class + shelf/encoding layout.

    Deliberately conservative: only proven layouts map to a chart; ambiguous or unrecognized
    layouts return ``unsupported`` so the caller warns instead of guessing. ``enc_dims`` /
    ``enc_meas`` are dimension / measure fields carried on the marks-card encodings (color,
    size, label, detail), which matter for card (a measure on the label with empty shelves)
    and scatter (a dimension on detail/color). ``geo_detail`` is True when a geographic-role
    dimension sits on the Detail encoding (the map Location); ``map_signal`` is an extra
    spatial confirmation (generated lat/lon on the axes or a geometry encoding) used to keep
    ambiguous marks from hijacking ordinary charts.
    """
    m = (mark or "").strip().lower()
    axis_dim = bool(dims_rows or dims_cols)
    axis_meas = bool(meas_rows or meas_cols)
    has_dim = axis_dim or bool(enc_dims)
    has_meas = axis_meas or bool(enc_meas)

    if not has_meas and not has_dim:
        return VT_UNSUPPORTED

    # Geographic maps (basics only): a geo-role dimension on Detail + a measure. The geo dim
    # being on Detail (not an axis) is what separates a map from an ordinary chart that merely
    # uses a geographic dimension on a shelf. Custom-geometry marks are deferred; ambiguous
    # marks additionally require a spatial signal (generated lat/lon or a geometry encoding).
    if geo_detail and map_meas:
        if m in _DEFER_MAP_MARKS:
            return VT_UNSUPPORTED
        if m in ("map", "filled", "filledmap"):
            return VT_FILLED_MAP
        if m in ("circle", "square", "shape", "point") and map_signal:
            return VT_MAP
        if m in ("automatic", "") and map_signal:
            return VT_FILLED_MAP
        # geo on Detail but no confirming spatial signal -> fall through to chart heuristics

    # measure(s) with no dimension anywhere -> a single-value card / multi-row card tile
    if has_meas and not has_dim:
        return VT_CARD

    if m == "line":
        return VT_LINE if has_meas else VT_UNSUPPORTED

    if m == "pie":
        # an angle measure split by a legend dimension -> pie
        return VT_PIE if (has_meas and has_dim) else VT_UNSUPPORTED

    if m in ("circle", "square", "shape", "point"):
        # a measure on each axis, disaggregated by a dimension -> scatter
        if meas_rows and meas_cols and has_dim:
            return VT_SCATTER
        return VT_UNSUPPORTED

    if m in ("bar", "automatic", ""):
        # vertical bars: category on cols (x), measure on rows (y)
        if dims_cols and meas_rows and not meas_cols:
            return VT_COLUMN
        # horizontal bars: category on rows (y), measure on cols (x)
        if dims_rows and meas_cols and not meas_rows:
            return VT_BAR
        if m in ("automatic", ""):
            # measures on both axes + a dimension -> scatter
            if meas_rows and meas_cols and has_dim:
                return VT_SCATTER
            if dims_rows and dims_cols and not axis_meas:
                return VT_MATRIX
            if axis_dim and not axis_meas:
                return VT_TABLE
            # Automatic with one dimension + one measure defaults to a column chart.
            if has_dim and axis_meas:
                return VT_COLUMN
        return VT_UNSUPPORTED

    if m == "text":
        if dims_rows and dims_cols:
            return VT_MATRIX
        if has_dim or has_meas:
            return VT_TABLE
        return VT_UNSUPPORTED

    return VT_UNSUPPORTED


def _parse_filters(ws, ds_default, base_cols, instances, index, ds_caption,
                   worksheet, warnings):
    filters = []
    for filt in _findall_local(ws, "filter"):
        cls = (filt.get("class") or "").lower()
        ds, fid = _split_token_attr(filt.get("column"))
        if fid is None:
            continue
        f = _resolve_field(ds or ds_default, fid, base_cols, instances, index,
                           ds_caption, worksheet, warnings)
        if f is None:
            continue
        # A slicer binds a raw column; an aggregate (SUM(Sales)) or calculated-measure
        # filter has no faithful slicer mapping -> warn instead of emitting a wrong slicer.
        if f["binding"] == "aggregation" or f["is_calc"]:
            warnings.append(_warn(
                "worksheet", worksheet,
                f"aggregate/measure filter on '{f['caption']}' is not mapped to a slicer "
                f"(filter scope requires manual attention)"))
            continue
        if cls == "categorical":
            kind = "categorical"
        elif cls in ("relative-date", "relative_date"):
            kind = "date_range"
        elif cls == "quantitative":
            kind = "date_range" if f["datatype"] in _DATE_TYPES else "quantitative"
        else:
            warnings.append(_warn("worksheet", worksheet,
                                  f"unsupported filter class '{cls}' (skipped)"))
            continue
        f = dict(f)
        f["filter_kind"] = kind
        f["binding"] = "column"
        f["aggregation"] = None
        filters.append(f)
    return filters


def _parse_worksheet(ws, index, ds_caption, warnings):
    name = ws.get("name")
    table = _first(ws, "table")
    if table is None:
        return None
    view = _first(table, "view")
    if view is None:
        view = table

    ds_refs = [d.get("name") for d in _findall_local(view, "datasource") if d.get("name")]
    ds_default = ds_refs[0] if ds_refs else None
    primary_caption = ds_caption.get(ds_default, ds_default)

    base_cols, instances = _parse_dependencies(view)

    panes = _first(table, "panes")
    pane = _first(panes, "pane") if panes is not None else None
    mark_el = _first(pane, "mark") if pane is not None else None
    mark = mark_el.get("class") if mark_el is not None else "Automatic"

    rows_el = _first(table, "rows")
    cols_el = _first(table, "cols")
    rows_text = (rows_el.text if rows_el is not None else "") or ""
    cols_text = (cols_el.text if cols_el is not None else "") or ""
    rows = _resolve_shelf(rows_text, ds_default,
                          base_cols, instances, index, ds_caption, name, warnings)
    cols = _resolve_shelf(cols_text, ds_default,
                          base_cols, instances, index, ds_caption, name, warnings)
    encodings = _parse_encodings(pane, ds_default, base_cols, instances, index,
                                 ds_caption, name, warnings)
    filters = _parse_filters(view, ds_default, base_cols, instances, index,
                             ds_caption, name, warnings)

    dims_rows = [f for f in rows if f["kind"] == "category"]
    dims_cols = [f for f in cols if f["kind"] == "category"]
    meas_rows = [f for f in rows if f["kind"] == "value"]
    meas_cols = [f for f in cols if f["kind"] == "value"]
    # marks-card encodings also carry fields: color/detail can be the disaggregating
    # dimension (scatter) and label/size can be the measure of a bare card / KPI tile.
    enc_dims = [f for f in (encodings["color"], encodings["detail"])
                if f and f["kind"] == "category"]
    enc_meas = [f for f in (encodings["size"], encodings["label"])
                if f and f["kind"] == "value"]
    # geographic map signals: a geo-role dimension on Detail is the Location; a measure on any
    # shelf/encoding feeds Color/Size; generated lat/lon on the axes or a geometry encoding is
    # the extra spatial confirmation that disambiguates an ambiguous mark from a normal chart.
    detail = encodings["detail"]
    color = encodings["color"]
    geo_detail = bool(detail and detail["kind"] == "category" and detail.get("geo_area"))
    map_meas = bool(meas_rows or meas_cols
                    or (color and color["kind"] == "value")
                    or (encodings["size"] and encodings["size"]["kind"] == "value")
                    or (encodings["label"] and encodings["label"]["kind"] == "value"))
    shelf_text = (rows_text + " " + cols_text).lower()
    has_latlon_axes = ("latitude (generated)" in shelf_text
                       and "longitude (generated)" in shelf_text)
    map_signal = has_latlon_axes or _has_geometry(pane)
    visual_type = _visual_type(mark, dims_rows, dims_cols, meas_rows, meas_cols,
                               enc_dims, enc_meas, geo_detail=geo_detail,
                               map_meas=map_meas, map_signal=map_signal)

    if visual_type == VT_UNSUPPORTED:
        if (mark or "").strip().lower() in _DEFER_MAP_MARKS or (geo_detail and map_meas):
            warnings.append(_warn(
                "worksheet", name,
                f"spatial/custom-geometry map (mark '{mark}') deferred "
                f"(basics only: filled + symbol map) -> no visual emitted"))
        else:
            warnings.append(_warn(
                "worksheet", name,
                f"mark class '{mark}' / shelf layout not supported -> no visual emitted"))

    return {
        "name": name,
        "datasource": primary_caption,
        "datasource_name": ds_default,
        "mark_class": mark,
        "visual_type": visual_type,
        "rows": rows,
        "cols": cols,
        "encodings": encodings,
        "filters": filters,
    }


# -- dashboard parsing ---------------------------------------------------------
def _zone_num(zone, attr):
    try:
        return float(zone.get(attr))
    except (TypeError, ValueError):
        return None


def _parse_dashboard(db, worksheet_names, warnings):
    name = db.get("name")
    size_el = _first(db, "size")
    size = {"w": None, "h": None}
    if size_el is not None:
        try:
            size["w"] = float(size_el.get("maxwidth")) if size_el.get("maxwidth") else None
            size["h"] = float(size_el.get("maxheight")) if size_el.get("maxheight") else None
        except ValueError:
            pass

    zones = []
    ext_w = ext_h = 0.0
    for zone in _findall_local(db, "zone"):
        x, y = _zone_num(zone, "x"), _zone_num(zone, "y")
        w, h = _zone_num(zone, "w"), _zone_num(zone, "h")
        if None not in (x, y, w, h) and w > 0 and h > 0:
            # canvas extent spans every zone (incl. layout containers), in Tableau's
            # internal coordinate units -- the correct frame for scaling, NOT <size>
            # (which is pixels and a different unit system).
            ext_w = max(ext_w, x + w)
            ext_h = max(ext_h, y + h)
        zname = zone.get("name")
        if not zname or zname not in worksheet_names:
            continue
        # worksheet zones carry no decoration type (legends/filters/titles do)
        if (zone.get("type-v2") or zone.get("type")):
            continue
        if None in (x, y, w, h) or w <= 0 or h <= 0:
            continue
        zones.append({"worksheet": zname, "x": x, "y": y, "w": w, "h": h})

    return {"name": name, "size": size,
            "extent": {"w": ext_w or None, "h": ext_h or None}, "zones": zones}


def _warn(scope, name, reason):
    return {"scope": scope, "name": name,
            "reason": "manual attention required: " + reason}


def parse_twb(xml_text):
    """Parse a Tableau ``.twb`` (workbook XML) into the normalized viz IR.

    Accepts ``str`` or ``bytes``; ``.twb`` files carry a UTF-8 BOM, so callers reading from
    disk should use ``encoding="utf-8-sig"``. Returns a JSON-serializable dict with
    ``worksheets``, ``dashboards``, and a structured ``warnings`` list. Never raises on
    unsupported viz grammar -- it degrades to warnings instead.
    """
    if isinstance(xml_text, bytes):
        xml_text = xml_text.decode("utf-8-sig")
    else:
        xml_text = xml_text.lstrip("\ufeff")
    root = ET.fromstring(xml_text)

    index, ds_caption = _build_field_index(root)
    warnings = []

    ws_holder = _children_local(root, "worksheets")
    ws_elems = []
    for h in ws_holder:
        ws_elems.extend(_children_local(h, "worksheet"))
    worksheets = []
    for ws in ws_elems:
        parsed = _parse_worksheet(ws, index, ds_caption, warnings)
        if parsed:
            worksheets.append(parsed)
    worksheet_names = {w["name"] for w in worksheets}
    ws_by_name = {w["name"]: w for w in worksheets}

    db_holder = _children_local(root, "dashboards")
    db_elems = []
    for h in db_holder:
        db_elems.extend(_children_local(h, "dashboard"))
    dashboards = []
    for db in db_elems:
        parsed = _parse_dashboard(db, worksheet_names, warnings)
        for z in parsed["zones"]:
            target = ws_by_name.get(z["worksheet"])
            if target and target["visual_type"] == VT_UNSUPPORTED:
                warnings.append(_warn(
                    "dashboard", parsed["name"],
                    f"worksheet '{z['worksheet']}' is unsupported -> zone left empty"))
        dashboards.append(parsed)

    return {"worksheets": worksheets, "dashboards": dashboards, "warnings": warnings}


# -- PBIR field expression emission --------------------------------------------
def _apply_override(field, model_table, field_map):
    """Return (entity, property, binding) after applying caller overrides."""
    entity, prop, binding = field["entity"], field["property"], field["binding"]
    if field_map and field["caption"] in field_map:
        ov = field_map[field["caption"]]
        entity = ov.get("entity", entity)
        prop = ov.get("property", prop)
        binding = ov.get("binding", binding)
    elif model_table and binding != "measure":
        entity = model_table
    return entity, prop, binding


def _field_expression(field, model_table, field_map):
    """Build the (expr, queryRef, nativeQueryRef) for one IR field."""
    entity, prop, binding = _apply_override(field, model_table, field_map)
    if binding == "measure":
        expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}},
                            "Property": prop}}
        return expr, f"{entity}.{prop}", prop
    column = {"Column": {"Expression": {"SourceRef": {"Entity": entity}},
                         "Property": prop}}
    if binding == "aggregation":
        func = _AGG_FUNC[field["aggregation"]]
        expr = {"Aggregation": {"Expression": column, "Function": func}}
        fname = field["aggregation"]
        return expr, f"{fname}({entity}.{prop})", f"{fname} of {prop}"
    return column, f"{entity}.{prop}", prop


def _projection(field, model_table, field_map, used_refs):
    expr, qref, nref = _field_expression(field, model_table, field_map)
    base_qref, i = qref, 1
    while qref in used_refs:
        i += 1
        qref = f"{base_qref} {i}"
    used_refs.add(qref)
    return {"field": expr, "queryRef": qref, "nativeQueryRef": nref}


def _role_projections(fields, model_table, field_map, used_refs):
    return [_projection(f, model_table, field_map, used_refs) for f in fields]


def _dedupe(fields):
    seen, out = set(), []
    for f in fields:
        key = (f["entity"], f["property"], f["binding"], f["aggregation"])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _build_query_state(ws, model_table, field_map, warnings):
    """Map a worksheet IR to a PBIR ``queryState`` (role -> projections)."""
    vt = ws["visual_type"]
    used_refs = set()

    rows, cols = ws["rows"], ws["cols"]
    color = ws["encodings"]["color"]
    label = ws["encodings"]["label"]
    size = ws["encodings"]["size"]
    detail = ws["encodings"]["detail"]

    def categories(fs):
        return [f for f in fs if f["kind"] == "category"]

    def values(fs):
        return [f for f in fs if f["kind"] == "value"]

    # calc fields can only live in a value role; flag any that landed on an axis.
    def drop_calc_axis(fs):
        kept = []
        for f in fs:
            if f["is_calc"] and f["binding"] == "measure":
                warnings.append(_warn(
                    "worksheet", ws["name"],
                    f"calculated field '{f['caption']}' used as a category/axis "
                    f"(skipped; measures cannot bind to an axis)"))
                continue
            kept.append(f)
        return kept

    state = {}
    if vt in (VT_COLUMN, VT_BAR, VT_LINE):
        cat = drop_calc_axis(_dedupe(categories(rows) + categories(cols)))
        val = _dedupe(values(rows) + values(cols))
        series = [color] if (color and color["kind"] == "category"
                             and not color["is_calc"]) else []
        cat = [f for f in cat if f not in series]
        if cat:
            state["Category"] = {"projections": _role_projections(
                cat, model_table, field_map, used_refs)}
        if val:
            state["Y"] = {"projections": _role_projections(
                val, model_table, field_map, used_refs)}
        if series:
            state["Series"] = {"projections": _role_projections(
                series, model_table, field_map, used_refs)}
    elif vt == VT_MATRIX:
        row_dims = drop_calc_axis(_dedupe(categories(rows)))
        col_dims = drop_calc_axis(_dedupe(categories(cols)))
        vals = _dedupe(values(rows) + values(cols)
                       + ([label] if label and label["kind"] == "value" else []))
        if row_dims:
            state["Rows"] = {"projections": _role_projections(
                row_dims, model_table, field_map, used_refs)}
        if col_dims:
            state["Columns"] = {"projections": _role_projections(
                col_dims, model_table, field_map, used_refs)}
        if vals:
            state["Values"] = {"projections": _role_projections(
                vals, model_table, field_map, used_refs)}
    elif vt == VT_TABLE:
        ordered = drop_calc_axis(_dedupe(
            categories(rows) + categories(cols))) + _dedupe(
            values(rows) + values(cols)
            + ([label] if label and label["kind"] == "value" else []))
        if ordered:
            state["Values"] = {"projections": _role_projections(
                ordered, model_table, field_map, used_refs)}
    elif vt == VT_SCATTER:
        x = _dedupe(values(cols))   # measure(s) on columns -> X axis
        y = _dedupe(values(rows))   # measure(s) on rows    -> Y axis
        cat = drop_calc_axis(_dedupe(
            categories(rows) + categories(cols)
            + ([detail] if detail and detail["kind"] == "category" else [])))
        series = [color] if (color and color["kind"] == "category"
                             and not color["is_calc"]) else []
        cat = [f for f in cat if f not in series]
        # only bind Size if that measure is not already an axis (avoid double-binding)
        axis_keys = {(f["entity"], f["property"], f["binding"], f["aggregation"])
                     for f in x + y}
        size_f = ([size] if (size and size["kind"] == "value"
                  and (size["entity"], size["property"], size["binding"],
                       size["aggregation"]) not in axis_keys) else [])
        if x:
            state["X"] = {"projections": _role_projections(
                x, model_table, field_map, used_refs)}
        if y:
            state["Y"] = {"projections": _role_projections(
                y, model_table, field_map, used_refs)}
        if cat:
            state["Category"] = {"projections": _role_projections(
                cat, model_table, field_map, used_refs)}
        if series:
            state["Series"] = {"projections": _role_projections(
                series, model_table, field_map, used_refs)}
        if size_f:
            state["Size"] = {"projections": _role_projections(
                size_f, model_table, field_map, used_refs)}
    elif vt == VT_PIE:
        legend = drop_calc_axis(_dedupe(
            categories(rows) + categories(cols)
            + ([color] if color and color["kind"] == "category" else [])))
        vals = _dedupe(values(rows) + values(cols)
                       + ([label] if label and label["kind"] == "value" else [])
                       + ([size] if size and size["kind"] == "value" else []))
        if legend:
            state["Category"] = {"projections": _role_projections(
                legend, model_table, field_map, used_refs)}
        if vals:
            state["Y"] = {"projections": _role_projections(
                vals, model_table, field_map, used_refs)}
    elif vt == VT_CARD:
        vals = _dedupe(values(rows) + values(cols)
                       + ([label] if label and label["kind"] == "value" else [])
                       + ([size] if size and size["kind"] == "value" else []))
        if vals:
            state["Values"] = {"projections": _role_projections(
                vals, model_table, field_map, used_refs)}
    elif vt == VT_FILLED_MAP:
        # choropleth: the geo-role dimension on Detail is the Location, a single measure
        # (prefer the color saturation encoding, else any available) drives Color.
        loc = drop_calc_axis(_dedupe(
            [detail] if detail and detail["kind"] == "category" else []))
        meas = _dedupe(
            ([color] if color and color["kind"] == "value" else [])
            + values(rows) + values(cols)
            + ([size] if size and size["kind"] == "value" else [])
            + ([label] if label and label["kind"] == "value" else []))
        if loc:
            state["Location"] = {"projections": _role_projections(
                loc, model_table, field_map, used_refs)}
        if meas:
            state["Color"] = {"projections": _role_projections(
                meas[:1], model_table, field_map, used_refs)}
    elif vt == VT_MAP:
        # symbol / bubble map: geo Location, a measure on Size (prefer the size encoding),
        # and a distinct color measure on Color when present.
        loc = drop_calc_axis(_dedupe(
            [detail] if detail and detail["kind"] == "category" else []))
        size_pref = _dedupe(
            ([size] if size and size["kind"] == "value" else [])
            + values(rows) + values(cols)
            + ([label] if label and label["kind"] == "value" else []))
        size_sel = size_pref[:1]
        color_meas = [color] if (color and color["kind"] == "value") else []
        color_sel = [f for f in color_meas if f not in size_sel][:1]
        if loc:
            state["Location"] = {"projections": _role_projections(
                loc, model_table, field_map, used_refs)}
        if size_sel:
            state["Size"] = {"projections": _role_projections(
                size_sel, model_table, field_map, used_refs)}
        if color_sel:
            state["Color"] = {"projections": _role_projections(
                color_sel, model_table, field_map, used_refs)}
    return state


def _query_state_complete(vt, state):
    """A supported visual must carry its essential roles; otherwise it is degenerate.

    Guards against a visual whose fields were all dropped by aggregation/type/calc guards
    (e.g. a line chart left with a measure but no category) being emitted as an empty shell.
    """
    if vt in (VT_COLUMN, VT_BAR, VT_LINE, VT_PIE):
        return "Category" in state and "Y" in state
    if vt == VT_SCATTER:
        return "X" in state and "Y" in state
    if vt == VT_CARD:
        return "Values" in state
    if vt == VT_FILLED_MAP:
        return "Location" in state and "Color" in state
    if vt == VT_MAP:
        return "Location" in state and ("Size" in state or "Color" in state)
    if vt == VT_MATRIX:
        return "Values" in state and ("Rows" in state or "Columns" in state)
    if vt == VT_TABLE:
        return "Values" in state
    return False


def _pbir_vtype(vt, state):
    """Resolve the PBIR ``visualType`` string; a card splits into card vs multiRowCard."""
    if vt == VT_CARD:
        n = len(state.get("Values", {}).get("projections", []))
        return "multiRowCard" if n > 1 else "card"
    return _VT_TO_PBIR[vt]


# -- PBIR JSON part assembly ---------------------------------------------------
def _visual_json(name, vtype, position, query_state):
    visual = {"visualType": vtype}
    if query_state:
        visual["query"] = {"queryState": query_state}
    visual["drillFilterOtherVisuals"] = True
    return {
        "$schema": SCHEMA_VISUAL,
        "name": name,
        "position": position,
        "visual": visual,
    }


def _slicer_json(name, field, position, model_table, field_map):
    expr, qref, nref = _field_expression(field, model_table, field_map)
    state = {"Values": {"projections": [
        {"field": expr, "queryRef": qref, "nativeQueryRef": nref}]}}
    return _visual_json(name, "slicer", position, state)


def _position(x, y, w, h, z=0, tab=0):
    return {"x": round(x, 2), "y": round(y, 2), "z": z,
            "width": round(w, 2), "height": round(h, 2), "tabOrder": tab}


def _scale_zone(zone, ref_w, ref_h):
    sx = PAGE_WIDTH / ref_w if ref_w else 1
    sy = PAGE_HEIGHT / ref_h if ref_h else 1
    x = max(0.0, min(zone["x"] * sx, PAGE_WIDTH - 1))
    y = max(0.0, min(zone["y"] * sy, PAGE_HEIGHT - 1))
    w = max(40.0, min(zone["w"] * sx, PAGE_WIDTH - x))
    h = max(40.0, min(zone["h"] * sy, PAGE_HEIGHT - y))
    return x, y, w, h


def _page_json(name, display_name):
    return {
        "$schema": SCHEMA_PAGE,
        "name": name,
        "displayName": display_name,
        "displayOption": "FitToPage",
        "height": PAGE_HEIGHT,
        "width": PAGE_WIDTH,
    }


def _emit_page(parts, page_name, display_name, visuals):
    """Write a page.json plus its visual.json parts; ``visuals`` is a list of dicts."""
    base = f"definition/pages/{page_name}"
    parts[f"{base}/page.json"] = _dumps(_page_json(page_name, display_name))
    for v in visuals:
        parts[f"{base}/visuals/{v['name']}/visual.json"] = _dumps(v)


def _dumps(obj):
    return json.dumps(obj, indent=2)


def report_json_part():
    """The ``definition/report.json`` content shared by the full viz seam (``emit_pbir``) and the
    thin ``.pbip`` shell (``assemble_model.build_thin_report_parts``).

    The ``themeCollection.baseTheme`` is **required**: current Power BI Desktop's enhanced-report
    loader dereferences the report theme inside ``GetEnhancedReportDocument``, so a ``report.json``
    with no ``baseTheme`` throws a ``NullReferenceException`` when the report opens (the semantic
    model still loads, but the authoring canvas/Visualizations pane never initializes). Keeping a
    single builder prevents the two emit paths from drifting on this again.
    """
    return {
        "$schema": SCHEMA_REPORT,
        "layoutOptimization": "None",
        "themeCollection": {"baseTheme": {
            "name": "CY24SU10",
            "reportVersionAtImport": "5.61",
            "type": "SharedResources"}},
    }


# -- Field-parameter (swap) self-service report --------------------------------
def report_json_part_fp():
    """``report.json`` for the field-parameter (swap) self-service report.

    Mirrors what a current Power BI Desktop stamps for a report whose visuals consume field
    parameters: the richer ``report/3.3.0`` theme block (``reportVersionAtImport`` is an object, and
    a ``SharedResources`` resource package + ``settings`` accompany it). The ``baseTheme`` is still
    REQUIRED -- a ``report.json`` without it throws ``NullReferenceException`` on open (see
    ``report_json_part``). ``CY24SU10`` is a built-in shared theme, so no local theme file is needed.
    """
    return {
        "$schema": SCHEMA_REPORT_FP,
        "themeCollection": {"baseTheme": {
            "name": "CY24SU10",
            "reportVersionAtImport": {"visual": "1.8.97", "report": "2.0.97", "page": "1.3.97"},
            "type": "SharedResources"}},
        "resourcePackages": [{
            "name": "SharedResources", "type": "SharedResources",
            "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json",
                       "type": "BaseTheme"}]}],
        "settings": {"useEnhancedTooltips": False},
    }


def _fp_seed_projection(entry):
    """One seed projection for a field-parameter slot -- the parameter's first candidate field.

    The field parameter overrides this at runtime per the slicer selection, so the seed only
    supplies a valid default; ``nativeQueryRef``/``displayName`` carry the parameter's option label
    (matching what Desktop writes), while ``queryRef`` points at the concrete seed field.
    """
    table, col, label = entry["table"], entry["column"], entry["label"]
    if entry.get("is_measure"):
        field = {"Measure": {"Expression": {"SourceRef": {"Entity": table}}, "Property": col}}
    else:
        field = {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": col}}
    return {"field": field, "queryRef": f"{table}.{col}",
            "nativeQueryRef": label, "displayName": label}


def field_parameter_table_visual(name, specs, position, *, visual_type=VT_TABLE):
    """A ``tableEx``/``pivotTable`` whose Values well EXPANDS a list of field parameters.

    ``specs`` is an ordered list of ``emit_field_parameters`` spec dicts
    (``{table_name, display_col, entries:[{label, table, column, is_measure, order}, ...]}``). Each
    spec contributes ONE seed projection (its first candidate) and ONE ``fieldParameters`` entry
    binding that slot's projection index to the parameter's display column (``length`` 1). Slot
    order follows ``specs`` order, so a 3-dim + 3-measure self-service table reproduces the customer
    layout 1:1. Specs with no resolved entries are skipped.
    """
    projections, field_params = [], []
    for spec in specs or []:
        entries = spec.get("entries") or []
        if not entries:
            continue
        idx = len(projections)
        projections.append(_fp_seed_projection(entries[0]))
        field_params.append({
            "parameterExpr": {"Column": {
                "Expression": {"SourceRef": {"Entity": spec["table_name"]}},
                "Property": spec["display_col"]}},
            "index": idx, "length": 1})
    state = {"Values": {"projections": projections, "fieldParameters": field_params}}
    return {
        "$schema": SCHEMA_VISUAL_FP,
        "name": name,
        "position": position,
        "visual": {"visualType": _VT_TO_PBIR[visual_type], "query": {"queryState": state}},
    }


def field_parameter_slicer(name, spec, position):
    """A ``listSlicer`` bound to one field parameter's display column (a slot's field picker)."""
    table, col = spec["table_name"], spec["display_col"]
    state = {"Values": {"projections": [{
        "field": {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": col}},
        "queryRef": f"{table}.{col}", "nativeQueryRef": col, "active": True}]}}
    return {
        "$schema": SCHEMA_VISUAL_FP,
        "name": name,
        "position": position,
        "visual": {"visualType": "listSlicer", "query": {"queryState": state}},
    }


def build_field_parameter_page(parts, specs, *, page_name="pageSelfService",
                               display_name="Self-Service Table", visual_type=VT_TABLE):
    """Write one self-service page into ``parts``: a field-parameter-driven table across the top and
    a row of field-picker slicers beneath (one ``listSlicer`` per parameter).

    ``specs`` are ``emit_field_parameters`` specs (dim + measure swaps, in slot order). Returns the
    ``page_name`` written, or ``None`` when there are no usable specs (caller falls back to the thin
    shell). Page/visual ``$schema`` values use the field-parameter set so the expansion renders.
    """
    usable = [s for s in (specs or []) if (s.get("entries") or [])]
    if not usable:
        return None
    base = f"definition/pages/{page_name}"
    parts[f"{base}/page.json"] = _dumps({
        "$schema": SCHEMA_PAGE_FP, "name": page_name, "displayName": display_name,
        "displayOption": "FitToPage", "height": PAGE_HEIGHT, "width": PAGE_WIDTH})

    visuals = []
    table_h = round(PAGE_HEIGHT * 0.55, 2)
    tname = _sanitize(f"fptable-{page_name}")
    visuals.append((tname, field_parameter_table_visual(
        tname, usable, _position(8, 12, PAGE_WIDTH - 16, table_h, tab=0),
        visual_type=visual_type)))

    n = len(usable)
    gap = 12
    slot_w = (PAGE_WIDTH - 16 - gap * (n - 1)) / n if n else 200.0
    slot_w = max(120.0, slot_w)
    sy = table_h + 28
    sh = max(80.0, PAGE_HEIGHT - sy - 12)
    for i, spec in enumerate(usable):
        sx = 8 + i * (slot_w + gap)
        sname = _sanitize(f"fpslicer-{page_name}-{i}-{spec['table_name']}")
        visuals.append((sname, field_parameter_slicer(
            sname, spec, _position(sx, sy, slot_w, sh, z=1, tab=i + 1))))

    for vname, vjson in visuals:
        parts[f"{base}/visuals/{vname}/visual.json"] = _dumps(vjson)
    return page_name


def _filter_slicer_fields(ws_list):
    """Collect distinct filtered fields across worksheets (one slicer each)."""
    seen, out = set(), []
    for ws in ws_list:
        for f in ws.get("filters", []):
            key = (f["entity"], f["property"])
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
    return out


def emit_pbir(ir, *, dataset_name="Model", report_name="Report",
              model_table=None, field_map=None):
    """Emit a PBIR report definition (a ``{relative_path: text}`` parts dict) from the IR.

    One page per dashboard (a visual per worksheet zone), plus one page per worksheet not
    placed on any dashboard. Visuals bind to the model names captured in the IR; pass
    ``model_table`` to force every column ``Entity`` to a single model table, or ``field_map``
    (``{caption: {"entity","property","binding"}}``) to remap individual fields. Worksheets
    whose ``visual_type`` is ``unsupported`` are skipped (already recorded in ``warnings``).
    """
    parts = {}
    ws_by_name = {w["name"]: w for w in ir["worksheets"]}
    warnings = []

    parts["definition.pbir"] = _dumps({
        "$schema": SCHEMA_DEFINITION_PROPERTIES,
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{dataset_name}.SemanticModel"}},
    })
    parts["definition/version.json"] = _dumps({
        "$schema": SCHEMA_VERSION, "version": "2.0.0"})
    parts["definition/report.json"] = _dumps(report_json_part())
    parts[".platform"] = _dumps({
        "$schema": SCHEMA_PLATFORM,
        "metadata": {"type": "Report", "displayName": report_name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    })

    page_order = []
    placed = set()

    for db in ir["dashboards"]:
        page_name = _sanitize("page-" + (db["name"] or "dashboard"))
        zones = db["zones"]
        ref_w = (db["extent"]["w"] or max((z["x"] + z["w"] for z in zones), default=0)
                 or db["size"]["w"])
        ref_h = (db["extent"]["h"] or max((z["y"] + z["h"] for z in zones), default=0)
                 or db["size"]["h"])
        visuals = []
        page_ws = []
        for i, zone in enumerate(zones):
            ws = ws_by_name.get(zone["worksheet"])
            if not ws or ws["visual_type"] == VT_UNSUPPORTED:
                continue
            placed.add(ws["name"])
            state = _build_query_state(ws, model_table, field_map, warnings)
            if not _query_state_complete(ws["visual_type"], state):
                warnings.append(_warn(
                    "worksheet", ws["name"],
                    f"{ws['visual_type']} visual has no usable field bindings (skipped)"))
                continue
            page_ws.append(ws)
            x, y, w, h = _scale_zone(zone, ref_w, ref_h)
            vname = _sanitize(f"v-{page_name}-{i}-{ws['name']}")
            visuals.append(_visual_json(
                vname, _pbir_vtype(ws["visual_type"], state),
                _position(x, y, w, h, tab=i), state))
        visuals += _emit_slicers(page_ws, page_name, model_table, field_map)
        if not visuals:
            warnings.append(_warn("dashboard", db["name"],
                                  "no supported visuals on this dashboard"))
            continue
        _emit_page(parts, page_name, db["name"] or page_name, visuals)
        page_order.append(page_name)

    for ws in ir["worksheets"]:
        if ws["name"] in placed or ws["visual_type"] == VT_UNSUPPORTED:
            continue
        page_name = _sanitize("page-ws-" + ws["name"])
        state = _build_query_state(ws, model_table, field_map, warnings)
        if not _query_state_complete(ws["visual_type"], state):
            warnings.append(_warn(
                "worksheet", ws["name"],
                f"{ws['visual_type']} visual has no usable field bindings (skipped)"))
            continue
        main = _visual_json(
            _sanitize("v-" + ws["name"]), _pbir_vtype(ws["visual_type"], state),
            _position(40, 40, 880, 620), state)
        visuals = [main] + _emit_slicers([ws], page_name, model_table, field_map)
        _emit_page(parts, page_name, ws["name"], visuals)
        page_order.append(page_name)

    parts["definition/pages/pages.json"] = _dumps({
        "$schema": SCHEMA_PAGES,
        "pageOrder": page_order,
        "activePageName": page_order[0] if page_order else "",
    })

    ir.setdefault("warnings", []).extend(warnings)
    return parts


def _emit_slicers(ws_list, page_name, model_table, field_map):
    visuals = []
    fields = _filter_slicer_fields(ws_list)
    for i, f in enumerate(fields):
        y = 40 + i * 120
        if y > PAGE_HEIGHT - 120:
            break
        vname = _sanitize(f"slicer-{page_name}-{i}-{f['property']}")
        visuals.append(_slicer_json(
            vname, f, _position(PAGE_WIDTH - 220, y, 200, 100, z=1, tab=100 + i),
            model_table, field_map))
    return visuals


def migrate_twb_to_pbir(xml_text, *, dataset_name="Model", report_name="Report",
                        model_table=None, field_map=None):
    """One-call convenience: parse ``.twb`` text and emit the PBIR parts.

    Returns ``{"ir": ..., "parts": ..., "warnings": ...}``. ``parts`` is the
    ``{relative_path: text}`` PBIR definition; write it to a ``<report_name>.Report`` folder
    or base64-encode each part for the Fabric report *Update Definition* API.
    """
    ir = parse_twb(xml_text)
    parts = emit_pbir(ir, dataset_name=dataset_name, report_name=report_name,
                      model_table=model_table, field_map=field_map)
    return {"ir": ir, "parts": parts, "warnings": ir["warnings"]}


# -- command-line entry point --------------------------------------------------
# Turns the library into a runnable tool so a real exported workbook can be converted
# and the resulting ``<report>.Report`` folder opened in Power BI Desktop or deployed to
# Fabric. It is purely local: it reads a ``.twb`` file (or stdin) and writes JSON files --
# no network, no credentials, no secrets. All target names come from args / env, never the
# code. (The committed pytest suite stays offline; live open/deploy is a separate manual pass.)
def _write_parts(out_dir, report_name, parts):
    """Write ``{relative_path: text}`` PBIR parts under ``<out_dir>/<report_name>.Report``."""
    root = os.path.join(out_dir, report_name + ".Report")
    written = []
    for rel, text in parts.items():
        dest = os.path.join(root, *rel.split("/"))
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)
        written.append(dest)
    return root, written


def main(argv=None):
    """CLI: ``twb_to_pbir <input.twb|-> [-o OUT] [--dataset N] [--report N]``.

    With ``-o/--out`` the PBIR parts are written to ``<OUT>/<report>.Report``; without it a
    JSON manifest (part paths + warnings) is printed to stdout for a no-write dry run.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="twb_to_pbir",
        description="Convert a Tableau .twb workbook into a PBIR report wireframe.")
    parser.add_argument(
        "input", help="path to a .twb workbook, or '-' to read workbook XML from stdin")
    parser.add_argument(
        "-o", "--out", default=os.environ.get("TWB_PBIR_OUT"),
        help="output directory; a <report>.Report folder is written inside it. "
             "If omitted, a JSON manifest is printed to stdout (dry run).")
    parser.add_argument(
        "--dataset", default=os.environ.get("TWB_PBIR_DATASET", "Model"),
        help="semantic model name the report binds to (datasetReference byPath).")
    parser.add_argument(
        "--report", default=os.environ.get("TWB_PBIR_REPORT", "Report"),
        help="report display name and .Report folder name.")
    parser.add_argument(
        "--model-table", default=os.environ.get("TWB_PBIR_MODEL_TABLE"),
        help="optional: pin every column binding to this single model table.")
    args = parser.parse_args(argv)

    if args.input == "-":
        xml_text = sys.stdin.read()
    else:
        with open(args.input, "r", encoding="utf-8-sig") as fh:
            xml_text = fh.read()

    result = migrate_twb_to_pbir(
        xml_text, dataset_name=args.dataset, report_name=args.report,
        model_table=args.model_table)
    parts, warnings = result["parts"], result["warnings"]

    if args.out:
        root, written = _write_parts(args.out, args.report, parts)
        print("wrote {0} PBIR part(s) to {1}".format(len(written), root), file=sys.stderr)
        if warnings:
            print("{0} warning(s) need manual attention:".format(len(warnings)),
                  file=sys.stderr)
            for w in warnings:
                print("  - [{0}:{1}] {2}".format(w["scope"], w["name"], w["reason"]),
                      file=sys.stderr)
    else:
        print(json.dumps({"parts": sorted(parts), "warnings": warnings},
                         indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
