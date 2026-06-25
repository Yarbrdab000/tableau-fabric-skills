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

* marks -> visual types: ``Bar`` -> clustered column/bar, ``Line`` -> line, ``Area`` -> area
  (``areaChart``), ``Text`` -> table (``tableEx``) or matrix (``pivotTable``). Anything else is
  ``unsupported``.
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
VT_AREA = "area"          # areaChart (native area chart; stacked-vs-overlap fill is a Tier-2 property)
VT_TABLE = "table"        # tableEx
VT_MATRIX = "matrix"      # pivotTable
VT_SCATTER = "scatter"    # scatterChart (X/Y measures disaggregated by a dimension)
VT_CARD = "card"          # card (1 measure) / multiRowCard (>=2 measures), no dimension
VT_PIE = "pie"            # pieChart (angle measure + legend dimension)
VT_FILLED_MAP = "filled_map"  # shapeMap (choropleth: geo Category + measure Value/color saturation)
VT_MAP = "map"            # map (symbol/bubble: geo Location + measure Size/Color)
VT_COMBO = "combo"        # lineClusteredColumnComboChart (column measure(s) on Y + line measure(s) on Y2)
VT_WATERFALL = "waterfall"  # waterfallChart (running-total Gantt hack: dimension Category + base measure Y)
VT_DONUT = "donut"          # donutChart (dual-axis pie/donut hack: legend Category + angle measure Y)
VT_RIBBON = "ribbon"        # ribbonChart (bump/rank hack: ordinal Category + legend Series + base measure Y)
VT_UNSUPPORTED = "unsupported"

_VT_TO_PBIR = {
    VT_COLUMN: "clusteredColumnChart",
    VT_BAR: "clusteredBarChart",
    VT_LINE: "lineChart",
    VT_AREA: "areaChart",
    VT_TABLE: "tableEx",
    VT_MATRIX: "pivotTable",
    VT_SCATTER: "scatterChart",
    VT_PIE: "pieChart",
    # Choropleths default to Power BI's Shape map (clean offline polygon fill + measure-driven
    # colour saturation), not the Bing-backed Filled map. NB: shapeMap is gated behind the
    # "Shape map visual" preview feature in Power BI Desktop.
    VT_FILLED_MAP: "shapeMap",
    VT_MAP: "map",
    # Dual-axis / combo: a column-family measure share an axis with a line-family measure. Power
    # BI's combo chart puts the column measure(s) on Y (primary axis) and the line measure(s) on
    # Y2 (secondary axis). Role keys (Category/Series/Y/Y2) verified against real Microsoft PBIR
    # visual.json files and the original ComboChart capabilities definition.
    VT_COMBO: "lineClusteredColumnComboChart",
    # Running-total Gantt waterfall hack -> native waterfallChart. Roles Category (required) +
    # Y (required) + optional Breakdown verified against a real Microsoft PBIR waterfall
    # visual.json (jaho5/pbip_reference) and the visualContainer 1.5.0 / semanticQuery schemas.
    VT_WATERFALL: "waterfallChart",
    # Dual-axis pie/donut hack -> native donutChart. Shares the pieChart capability family
    # (legend Category + value Y); same role keys as the verified pieChart emit.
    VT_DONUT: "donutChart",
    # Manual-rank bump hack -> native ribbonChart. Power BI recomputes the rank from the base
    # measure, so the INDEX()/RANK() table-calc rank axis is dropped; roles Category (ordinal
    # axis) + Series (legend) + Y (base measure) verified against real Microsoft PBIR ribbonChart
    # visual.json files (microsoft/fabric-toolbox) + the visualContainer 1.5.0 schema.
    VT_RIBBON: "ribbonChart",
}

# Mark classes that, when two measures on one shelf carry DIFFERENT mark families, signal a
# dual-axis combo: a bar/column-family measure overlaid with a line/area-family measure. (Area is
# treated as line-family, consistent with the area->line default elsewhere in this module.)
_COLUMN_FAMILY_MARKS = {"bar", "gantt"}
_LINE_FAMILY_MARKS = {"line", "area"}

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

# Tableau discrete date PART -> column name on the model's shared Date dimension. The datasource
# migration build (assemble_model._build_date_dimension + tmdl_generate.generate_date_table_tmdl)
# already emits a marked Date table carrying these exact columns, so a date pill on the active
# business date rebinds to that calendar -- routing time intelligence through it -- instead of
# degrading to the fact's raw date column. This consumer never recomputes those facts; the model
# owns them and passes them in via ``date_binding``. Sub-day parts (Hour/Minute/Second), composite
# parts (MonthYear/DayOfYear) and ISO-Quarter/ISO-Weekday have no dedicated calendar column and are
# deliberately omitted -- they stay on the source column + warn (warn-never-wrong).
_DEFAULT_DATE_GRAIN_COLUMNS = {
    "Year": "Year", "Quarter": "Quarter", "Month": "Month", "Day": "Day",
    "Week": "Week of Year", "Weekday": "Day Name",
    "ISO-Year": "ISO Year", "ISO-Week": "Week of Year",
}


def _norm_date_col(name):
    """Normalize a column name for active-date matching (case/space/underscore-insensitive)."""
    return re.sub(r"\s+", " ", (name or "").strip().lower().replace("_", " ").replace("-", " "))


def _rebind_date_axis(field, deriv, date_binding):
    """Redirect a date axis pill to the model's shared Date table, or ``None`` to leave it as-is.

    Fires ONLY for the single ACTIVE business date the model build selected, so a secondary or
    inactive date (e.g. Ship Date, or any date when the primary is ambiguous) is never bound to the
    calendar and therefore can't silently display the active date's values -- the exact "break a lot
    of stuff" risk. A discrete date PART rebinds to its calendar column (Year -> Date[Year]); a plain
    exact/continuous date OR a day-or-coarser continuous truncation (Day/Week/Month/Quarter/Year-Trunc,
    the green ``t*:`` pills) rebinds to the marked key column (Date[Date]) -- the day-grain Date table
    relates to the fact date and Power BI's continuous date axis carries the display grain (this is what
    a Desktop-authored rebuild does: its line-chart date axis is Date[Date]). A SUB-DAY truncation
    (Hour/Minute/Second-Trunc) can't be represented by a day-grain calendar, and any part with no
    calendar column, return ``None`` (deferred -- the caller keeps the source column + warns). Returns
    ``(entity, property)`` to rebind, else ``None``.
    """
    if not date_binding or field.get("role") == "measure":
        return None
    table = date_binding.get("date_table")
    if not table:
        return None
    active = {_norm_date_col(c) for c in (date_binding.get("active_keys") or ())}
    if _norm_date_col(field.get("property")) not in active:
        return None
    if deriv in _DATE_PARTS:
        grains = date_binding.get("grain_columns") or _DEFAULT_DATE_GRAIN_COLUMNS
        col = grains.get(deriv)
        return (table, col) if col else None
    if deriv in ("None", "", None):  # plain/continuous exact date -> the marked calendar key
        return (table, date_binding.get("key_column") or "Date")
    # A continuous DAY-or-coarser truncation (Day/Week/Month/Quarter/Year-Trunc, the green `t*:`
    # pills) on the active business date also binds to the marked calendar KEY column: the day-grain
    # Date table relates to the fact date and Power BI's continuous date axis carries the display
    # grain -- matching a Desktop-authored rebuild whose line-chart date axis is Date[Date]. A
    # SUB-DAY truncation (Hour/Minute/Second-Trunc) can't be represented by a day-grain calendar, so
    # it stays deferred (caller keeps the source column + warns; warn-never-wrong).
    if re.match(r"(?:Year|Quarter|Month|Week|Day)-Trunc$", str(deriv or "")):
        return (table, date_binding.get("key_column") or "Date")
    return None  # sub-day TRUNC / unmapped grain -> deferred (display-grain shape is a later pass)


# Tableau internal pseudo-fields that have no model binding. ``Number of Records`` is handled by
# the implicit row-count recognizer below (it maps to a COUNTROWS measure, not a silent drop), so
# it is deliberately NOT listed here.
_SPECIAL_FIELDS = {":Measure Names", "Measure Names", "Measure Values",
                   ":Measure Values", "Multiple Values"}

# -- Implicit row-count recognition --------------------------------------------
# Tableau expresses "count the rows of a table" two ways, neither of which names a real model
# column: (1) an aggregation over the object-model row identity ``__tableau_internal_object_id__``
# (a ``Count`` column-instance whose ``column`` ref encodes the table), and (2) the legacy
# auto-generated ``Number of Records`` field (the constant ``1`` summed). Both mean COUNTROWS of a
# table -- so the faithful Power BI target is a COUNTROWS measure, NOT a column projection. Left
# unrecognised, (1) is silently dropped (empty visual) and (2) emits a dangling ``SUM('T'[Number
# of Records])`` against a column the model never had. The model-side COUNTROWS measure is owned by
# the datasource-migration build; this layer RECOGNISES the implicit count, binds it when the caller
# supplies a ``row_count_binding`` target, and otherwise emits a precise warn-never-wrong warning
# (never a guessed or dangling ref). COUNT(*) == row count and the object-id ref encoding the table
# are unprotectable Tableau<->Power BI interoperability facts, verified directly against our own
# corpus XML; the recognizer/binder are authored here against our own IR.
_NUMBER_OF_RECORDS = "Number of Records"
_COUNT_DERIVS = {"Count", "CountD", "Cnt", "CntD"}
_OID_HASH_RE = re.compile(r"_[0-9A-Fa-f]{32}$")

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


def _attr_local(elem, name):
    """Read an attribute by local name, ignoring any XML namespace prefix.

    Tableau namespaces some group-filter attributes (e.g. ``user:op`` parses to
    ``{http://www.tableausoftware.com/xml/user}op``), so a plain ``elem.get("op")`` misses them.
    """
    v = elem.get(name)
    if v is not None:
        return v
    for k, val in elem.attrib.items():
        if _local(k) == name:
            return val
    return None


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

    Returns ``(index, ds_caption_by_name, internal_fields)`` where ``index[(ds_name, field_id)]``
    is ``{"entity": <relation name>, "property": clean_col(remote), "datatype": <bucket>}`` and
    ``internal_fields`` is a set of ``(ds_name, field_id)`` for Tableau auto-generated pseudo-fields.
    ``field_id`` is the field's internal id (the metadata ``local-name`` / column ``name``
    without brackets), so the binding survives a workbook-side rename of the caption.
    """
    index = {}
    ds_caption = {}
    internal = set()
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
        # Tableau auto-generates helper fields the user never created: dashboard filter/set
        # *action* groups (``user:auto-column='sheet_link'``), viz-in-tooltip and forecast
        # helpers. They carry no user model binding, so record their ids (authoritatively, via
        # the ``user:auto-column`` marker -- language independent) to drop them silently later.
        for el in ds.iter():
            if _attr_local(el, "auto-column"):
                nm = _strip_brackets((el.get("name") or "").strip())
                if nm:
                    internal.add((dsn, nm))
    return index, ds_caption, internal


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
            calc_el = _first(c, "calculation")
            base_cols[(dsn, cid)] = {
                "caption": c.get("caption") or cid,
                "role": (c.get("role") or "").lower(),
                "datatype": (c.get("datatype") or "").lower(),
                "is_calc": calc_el is not None,
                "formula": calc_el.get("formula") if calc_el is not None else None,
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


_INTERNAL_OBJECT_ID = "__tableau_internal_object_id__"


def _is_internal_field(ds, field_id, base_id, internal_fields):
    """True if a pill references a Tableau internal / auto-generated pseudo-field.

    These carry no user-facing model binding and must be dropped *silently* (never warned):
    warning on them is false noise, not a real coverage gap. Two authoritative signals:

    * ``__tableau_internal_object_id__`` -- Tableau's object-model row-count internal (a reserved
      double-underscore namespace, never a user field), matched anywhere in the id.
    * ``user:auto-column`` declarations -- dashboard filter/set *action* groups (``sheet_link``),
      viz-in-tooltip and forecast helpers. Their ids are collected from the datasource by
      :func:`_build_field_index` into ``internal_fields`` keyed by ``(ds, field_id)``.
    """
    if _INTERNAL_OBJECT_ID in (field_id or "") or _INTERNAL_OBJECT_ID in (base_id or ""):
        return True
    if internal_fields and (
            (ds, field_id) in internal_fields or (ds, base_id) in internal_fields):
        return True
    return False


def _oid_table(ds, inst_column, base_cols):
    """Resolve the table name a ``__tableau_internal_object_id__`` count refers to.

    The count instance's ``column`` ref encodes the table as ``...].[<relation>_<hex32>]``. Prefer
    the object-id column's ``caption`` (the user-facing table name, e.g. a Union's friendly name)
    when the worksheet's dependencies carry it; otherwise strip the trailing ``_<hex32>`` from the
    relation id. Returns the table name (or ``None``).
    """
    cap = (base_cols.get((ds, inst_column)) or {}).get("caption")
    if cap and _INTERNAL_OBJECT_ID not in cap:
        return cap
    tail = (inst_column or "").split("].[")[-1].rstrip("]")
    m = _OID_HASH_RE.search(tail)
    table = tail[:m.start()] if m else tail
    return table or None


def _row_count_tables(ds, instances, base_cols):
    """Distinct table names this worksheet implicitly counts via ``__tableau_internal_object_id__``.

    A genuine implicit COUNT pill leaves a ``Count`` column-instance on the object-id in the
    worksheet's dependencies. A bare ``[__tableau_internal_object_id__]`` filter/detail artifact
    (no count instance) yields an empty list, so it stays on the silent-drop path -- never warned.
    """
    out = []
    for (dsn, _iid), inst in (instances or {}).items():
        if dsn != ds:
            continue
        col = inst.get("column") or ""
        if _INTERNAL_OBJECT_ID in col and inst.get("derivation") in _COUNT_DERIVS:
            table = _oid_table(ds, col, base_cols)
            if table and table not in out:
                out.append(table)
    return out


def _classify_row_count(ds, field_id, base_id, deriv, base_cols, instances):
    """Classify a pill as an implicit row count, or ``None``.

    Returns ``{"kind": "object_id"|"numrec", "table": <name|None>, "candidates": [<name>...]}``.
    ``object_id`` is recognised only when the worksheet actually carries a count-of-object-id
    instance (so a bare object-id artifact is left to the silent-drop path). For ``object_id`` a
    single distinct table is named; multiple distinct tables are left ambiguous (``table=None``,
    ``candidates`` populated) so the binder never guesses which fact to count.
    """
    cap = (base_cols.get((ds, base_id)) or {}).get("caption") or ""
    if base_id == _NUMBER_OF_RECORDS or field_id == _NUMBER_OF_RECORDS or cap == _NUMBER_OF_RECORDS:
        return {"kind": "numrec", "table": None, "candidates": []}
    if _INTERNAL_OBJECT_ID in (base_id or "") or _INTERNAL_OBJECT_ID in (field_id or ""):
        tables = _row_count_tables(ds, instances, base_cols)
        if not tables:
            return None
        return {"kind": "object_id",
                "table": tables[0] if len(tables) == 1 else None,
                "candidates": tables}
    return None


def _row_count_measure_target(rc, row_count_binding):
    """Resolve the ``(entity, measure)`` to bind an implicit row count to, or ``None``.

    ``row_count_binding`` is this layer's own (consumer-owned) shape:
    ``{"measures": {<table name>: {"entity": ..., "measure": ...}}, "default": {"entity": ...,
    "measure": ...}}``. An ``object_id`` count binds only when its specific table has a measure
    (never via ``default`` -- it names a fact, so binding requires that fact's COUNTROWS measure); a
    ``numrec`` count (the legacy single-fact row count) binds via ``default``.
    """
    if not row_count_binding:
        return None
    measures = row_count_binding.get("measures") or {}
    if rc["kind"] == "object_id" and rc.get("table") in measures:
        m = measures[rc["table"]] or {}
        if m.get("entity") and m.get("measure"):
            return (m["entity"], m["measure"])
    if rc["kind"] == "numrec":
        d = row_count_binding.get("default") or {}
        if d.get("entity") and d.get("measure"):
            return (d["entity"], d["measure"])
    return None


def _bind_or_warn_row_count(rc, ds, worksheet, base_id, field_id, deriv,
                            warnings, warn_special, row_count_binding):
    """Bind an implicit row count to a COUNTROWS measure, or warn (warn-never-wrong).

    Returns a measure-bound IR field when ``row_count_binding`` supplies a faithful target,
    otherwise ``None`` -- emitting a precise warning (gated on ``warn_special`` so the Measure
    Values path stays silent). The warning always names the implicit row count and the COUNTROWS
    measure the model build needs to supply, so the gap is explicit and never a dangling/guessed
    binding.
    """
    target = _row_count_measure_target(rc, row_count_binding)
    if target is not None:
        entity, measure = target
        return {
            "caption": measure, "field_id": base_id, "instance": field_id,
            "role": "measure", "datatype": "integer", "is_calc": False,
            "derivation": deriv, "aggregation": None,
            "entity": entity, "property": measure,
            "binding": "measure", "kind": "value",
            "geo_area": None, "formula": None,
        }
    if warn_special:
        if rc["kind"] == "object_id" and rc.get("table"):
            reason = (f"implicit row count COUNT('{rc['table']}') has no model binding -- needs a "
                      f"row-count (COUNTROWS) measure on table '{rc['table']}' (left unbound)")
        elif rc["kind"] == "object_id":
            cands = ", ".join(rc.get("candidates") or []) or "unknown"
            reason = (f"implicit row count COUNT(*) is ambiguous across tables ({cands}) -- needs a "
                      f"row-count (COUNTROWS) measure (left unbound)")
        else:
            reason = ("implicit row count [Number of Records] has no model binding -- needs a "
                      "row-count (COUNTROWS) measure (left unbound)")
        warnings.append(_warn("worksheet", worksheet, reason))
    return None


# -- cross-layer measure binding (consumer of the model build's calc->measure manifest) --------
# The locked model<->viz contract: the datasource-migration (model) build translates each
# workbook calc / quick-table-calc into a named ``_Measures`` measure and hands back a token-keyed
# manifest; the dashboard (viz) build rebinds the matching pills to those real measures so a
# visual references the measure instead of a dangling caption/formula. Binding is DETERMINISTIC
# (token-keyed, never a fuzzy name match) and only for measures the model actually produced.
_MEASURE_BIND_OK = frozenset({"translated", "assisted-approved"})


def _measure_binding_entries(measure_binding):
    """Normalise the consumer-owned ``measure_binding`` into a flat ``{key: entry}`` map.

    Accepts a flat ``{key: entry}`` dict or a ``{"measures": {key: entry}}`` wrapper (mirroring
    ``row_count_binding``). Each entry carries ``entity``/``model_table`` + ``measure``/
    ``measure_name`` + an optional ``status``.
    """
    if not isinstance(measure_binding, dict) or not measure_binding:
        return {}
    inner = measure_binding.get("measures")
    return inner if isinstance(inner, dict) else measure_binding


def _measure_binding_candidate_keys(field_id, base_id, caption, worksheet):
    """Candidate lookup keys in deterministic join priority (token first, never fuzzy):
    pill instance token > bare calc id > ``worksheet|caption`` > caption. Mirrors the locked
    contract so a translated calc binds by its stable token even when captions collide."""
    keys = []
    for k in (field_id, base_id,
              (f"{worksheet}|{caption}" if worksheet and caption else None),
              caption):
        if k and k not in keys:
            keys.append(k)
    return keys


def _lookup_measure_binding(measure_binding, field_id, base_id, caption, worksheet):
    """Resolve a calc pill to its translated ``(entity, measure)`` model measure, or ``None``.

    Binds ONLY when a candidate key hits an entry whose ``status`` is bindable (translated /
    assisted-approved -- a missing status is treated as translated, since the model build only
    emits an entry for a measure it produced); any other status (assisted-suggested / stub /
    handoff) or a miss returns ``None`` so the caller degrades-and-warns. Default (no binding
    supplied) -> ``None`` -> byte-unchanged.
    """
    entries = _measure_binding_entries(measure_binding)
    if not entries:
        return None
    for key in _measure_binding_candidate_keys(field_id, base_id, caption, worksheet):
        entry = entries.get(key)
        if not isinstance(entry, dict):
            continue
        if (entry.get("status") or "translated") not in _MEASURE_BIND_OK:
            continue
        measure = entry.get("measure") or entry.get("measure_name")
        entity = entry.get("entity") or entry.get("model_table") or MEASURES_TABLE
        if measure:
            return (entity, measure)
    return None


def _resolve_field(ds, field_id, base_cols, instances, index, ds_caption,
                   worksheet, warnings, warn_special=True, internal_fields=None,
                   date_binding=None, row_count_binding=None, measure_binding=None):
    """Resolve one shelf/encoding pill into an IR field dict (or ``None`` if it must be dropped).

    Records a structured warning whenever a token cannot be bound to a model field, or is
    bound through a non-authoritative fallback, so the wireframe never claims a binding it
    cannot stand behind. ``warn_special`` is set ``False`` by the Measure Values/Names path,
    which handles the ``Multiple Values`` / ``:Measure Names`` pseudo-fields itself, so dropping
    them here must stay silent rather than emit a false "no model binding" warning.
    """
    if not field_id or field_id in _SPECIAL_FIELDS or field_id.startswith(":"):
        if warn_special:
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

    # Cross-layer measure binding (consumer of the model build's calc->measure manifest, the locked
    # model<->viz contract). A workbook-local calc or quick-table-calc pill that the model build
    # translated into a named ``_Measures`` measure is rebound here to that measure -- exact,
    # deterministic, token-keyed. Runs BEFORE the base-column resolve so a table-calc instance whose
    # base is not itself a model column (e.g. a ``pcdf`` percent-difference pill) still binds by its
    # token. Only a translated / assisted-approved entry binds (warn-never-wrong); a miss falls
    # through to the existing resolve/degrade path. Default (no binding supplied) -> byte-unchanged.
    if measure_binding:
        _mb_base = base_cols.get((ds, base_id)) or {}
        mb = _lookup_measure_binding(measure_binding, field_id, base_id,
                                     _mb_base.get("caption"), worksheet)
        if mb is not None:
            m_entity, m_measure = mb
            return {
                "caption": _mb_base.get("caption") or m_measure,
                "field_id": base_id, "instance": field_id,
                "role": "measure",
                "datatype": tableau_type_to_simple(_mb_base.get("datatype")) or "integer",
                "is_calc": True, "derivation": deriv, "aggregation": None,
                "entity": m_entity, "property": m_measure,
                "binding": "measure", "kind": "value",
                "geo_area": None, "formula": _mb_base.get("formula"),
                "measure_rebound": True,
            }

    # Implicit row count (object-id COUNT(*) / legacy [Number of Records]) -> a COUNTROWS measure.
    # Runs BEFORE the internal-field silent drop (object-id) and the base-column resolve (which
    # would otherwise emit a dangling SUM([Number of Records])), so an implicit count is either
    # faithfully bound or precisely warned -- never silently lost or mis-bound.
    rc = _classify_row_count(ds, field_id, base_id, deriv, base_cols, instances)
    if rc is not None:
        return _bind_or_warn_row_count(rc, ds, worksheet, base_id, field_id, deriv,
                                       warnings, warn_special, row_count_binding)

    if _is_internal_field(ds, field_id, base_id, internal_fields):
        return None

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
        "formula": base.get("formula"),
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

    # Date-table rebind (consumes the model build's date facts; never recomputes them). When the
    # pill is the active business date, redirect it to the shared marked Date dimension so time
    # intelligence runs through the calendar rather than the fact's raw date column. Secondary /
    # inactive dates, unmapped grains and continuous TRUNCs fall through to the degrade-and-warn
    # path below -- they are never silently rebound to the wrong date.
    rebind = _rebind_date_axis(field, deriv, date_binding)
    if rebind is not None:
        field["entity"], field["property"] = rebind
        field["binding"] = "column"
        field["kind"] = "category"
        field["date_rebound"] = True
        return field

    if deriv in _DATE_PARTS or deriv.startswith("Trunc") or deriv.endswith("-Trunc"):
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
                   worksheet, warnings, warn_special=True, internal_fields=None,
                   date_binding=None, row_count_binding=None, measure_binding=None):
    fields = []
    for tok in _TOKEN_RE.findall(text or ""):
        ds, fid = _split_token(tok)
        f = _resolve_field(ds or ds_default, fid, base_cols, instances, index,
                           ds_caption, worksheet, warnings, warn_special=warn_special,
                           internal_fields=internal_fields, date_binding=date_binding,
                           row_count_binding=row_count_binding, measure_binding=measure_binding)
        if f:
            fields.append(f)
    return fields


def _parse_encodings(pane, ds_default, base_cols, instances, index, ds_caption,
                     worksheet, warnings, warn_special=True, internal_fields=None,
                     date_binding=None, row_count_binding=None, measure_binding=None):
    enc = {"color": None, "size": None, "label": None, "detail": None, "angle": None}
    if pane is None:
        return enc
    holder = _first(pane, "encodings")
    if holder is None:
        return enc
    mapping = {"color": "color", "size": "size", "text": "label",
               "label": "label", "lod": "detail", "level-of-detail": "detail",
               "wedge-size": "angle"}
    for child in list(holder):
        role = mapping.get(_local(child.tag))
        if not role:
            continue
        ds, fid = _split_token_attr(child.get("column"))
        f = _resolve_field(ds or ds_default, fid, base_cols, instances, index,
                           ds_caption, worksheet, warnings, warn_special=warn_special,
                           internal_fields=internal_fields, date_binding=date_binding,
                           row_count_binding=row_count_binding, measure_binding=measure_binding)
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


_BRACKET_TOKEN_RE = re.compile(r"\[([^\]]+)\]")


def _pane_mark_map(table):
    """Index a worksheet's per-axis marks for dual-axis / combo detection.

    A dual-axis worksheet serialises one ``<pane>`` per measure axis. Each non-primary pane
    carries ``y-axis-name`` (the measure field ref, whose last bracketed token is the column
    instance, e.g. ``sum:Sales:qk``) and its own ``<mark class>``; a secondary axis additionally
    carries ``y-index`` >= 1. Returns ``(mark_by_instance, primary_mark, has_secondary_axis)``
    where ``mark_by_instance`` maps a measure instance token to that axis's mark class.
    """
    mark_by_instance = {}
    primary_mark = None
    has_secondary_axis = False
    panes_el = _first(table, "panes")
    if panes_el is None:
        return mark_by_instance, primary_mark, has_secondary_axis
    for pane in _children_local(panes_el, "pane"):
        mk_el = _first(pane, "mark")
        mk = mk_el.get("class") if mk_el is not None else None
        y_index = _attr_local(pane, "y-index")
        if y_index not in (None, "", "0"):
            has_secondary_axis = True
        y_axis = _attr_local(pane, "y-axis-name")
        if y_axis:
            toks = _BRACKET_TOKEN_RE.findall(y_axis)
            if toks:
                mark_by_instance[toks[-1]] = mk
        elif primary_mark is None and mk:
            primary_mark = mk
    return mark_by_instance, primary_mark, has_secondary_axis


def _mark_family(mark):
    m = (mark or "").strip().lower()
    if m in _COLUMN_FAMILY_MARKS:
        return "column"
    if m in _LINE_FAMILY_MARKS:
        return "line"
    return None


def _detect_combo(meas_rows, meas_cols, has_category, mark_by_instance, primary_mark):
    """Classify a dual-axis combo: measures on one shelf that split into a column-family group
    and a line-family group, against a shared category dimension.

    Returns ``(column_measures, line_measures)`` only when BOTH groups are non-empty (a genuine
    combo); otherwise ``(None, None)`` so the caller keeps the ordinary single-mark visual. This
    is deliberately conservative -- same-mark multi-measure shelves and unresolvable measures
    never trigger a combo (warn-never-wrong).
    """
    if not has_category:
        return None, None
    column_meas, line_meas = [], []
    for f in list(meas_rows) + list(meas_cols):
        fam = _mark_family(mark_by_instance.get(f.get("instance"), primary_mark))
        if fam == "column":
            column_meas.append(f)
        elif fam == "line":
            line_meas.append(f)
    if column_meas and line_meas:
        return column_meas, line_meas
    return None, None


_RUNNING_TOTAL_RE = re.compile(r"\.\[cum:")

# Manual-rank table-calc functions that signal a bump/rank chart: the rank/position is computed
# in the view (the INDEX/RANK family) and plotted on an axis. Power BI's ribbonChart recomputes
# the rank from the base measure, so these table-calc artifacts are dropped (like the waterfall's
# running total) and the base measure + legend + ordinal axis bind directly.
_RANK_TABLECALC_RE = re.compile(
    r"\b(INDEX|RANK|RANK_DENSE|RANK_MODIFIED|RANK_PERCENTILE|RANK_UNIQUE)\s*\(", re.I)


def _has_continuous_date(fields):
    """True when an axis carries a CONTINUOUS (green) Tableau date pill.

    A continuous date is a date *truncation* -- Tableau serialises it with a ``*-Trunc`` derivation
    (e.g. ``Day-Trunc`` / ``Month-Trunc``, pill prefixes ``tdy:`` / ``tmn:``). Truncation is a
    date-only operation, so the ``-Trunc`` suffix unambiguously marks a continuous date axis; a
    discrete date PART (Year / Month, derivation in ``_DATE_PARTS``) is NOT continuous. Under an
    Automatic mark Tableau renders a continuous date + a measure as a LINE (a discrete date -> bars).
    """
    return any(str(f.get("derivation") or "").endswith("-Trunc") for f in fields)


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

    # Location-only map: a geo-role dimension on Detail with NO measure anywhere and no axis
    # pills is Tableau's default rendering of that geography (auto-generated lat/lon, uniform
    # fill) -- there is no other faithful reading (no measure for a chart, and a geographic field
    # is a map, not a text list). The faithful rebuild is a shapeMap carrying just the Location
    # (Category); the colour-saturation Value is simply absent. Custom-geometry marks still defer.
    if geo_detail and not map_meas and not axis_dim and not axis_meas:
        if m not in _DEFER_MAP_MARKS:
            return VT_FILLED_MAP

    # measure(s) with no dimension anywhere -> a single-value card / multi-row card tile
    if has_meas and not has_dim:
        return VT_CARD

    if m == "line":
        return VT_LINE if has_meas else VT_UNSUPPORTED

    if m == "area":
        # Power BI has a native ``areaChart`` -- an area chart is its own chart type (a filled line),
        # not merely a styled line -- so an ``area`` mark binds to areaChart with the SAME axes and
        # encodings a line would use (Category/Y/Series/SmallMultiples), getting the chart TYPE right
        # (Tier-1). Stacked-vs-overlapping area is a fill property deferred to a later styling pass.
        # Without a measure on an axis (the value sits only on an encoding) the layout is ambiguous
        # and stays unsupported -> warn, rather than guess (warn-never-wrong).
        return VT_AREA if has_meas else VT_UNSUPPORTED

    if m == "pie":
        # an angle measure split by a legend dimension -> pie
        return VT_PIE if (has_meas and has_dim) else VT_UNSUPPORTED

    if m in ("circle", "square", "shape", "point"):
        # a measure on each axis, disaggregated by a dimension -> scatter
        if meas_rows and meas_cols and has_dim:
            return VT_SCATTER
        # Highlight table: a Square mark with dimensions on both axes (a coloured crosstab), the
        # measure carried on the colour/label encoding -> a matrix; the colour saturation itself
        # is Tier-2 styling. A single-axis highlight table degrades to a table. Square marks with
        # NO axis dimensions (treemap / packed-bubble / heatmap layouts) stay unsupported -> warn
        # rather than guess a visual we cannot place faithfully.
        if m == "square":
            if dims_rows and dims_cols:
                return VT_MATRIX
            if (dims_rows or dims_cols) and has_meas:
                return VT_TABLE
            return VT_UNSUPPORTED
        # Circle / Shape / Point dot (strip) plot: one category axis vs one measure axis carries
        # the SAME field binding as a column/bar -- the dot glyph itself is Tier-2 styling (cf.
        # area -> line). Restricted to exactly one axis dimension + one axis measure on opposite
        # axes so nothing on a second axis is silently dropped; packed-bubble / no-axis /
        # multi-axis circle layouts stay unsupported (ambiguous -> warn).
        if len(dims_rows) + len(dims_cols) == 1 and len(meas_rows) + len(meas_cols) == 1:
            if dims_cols and meas_rows:
                return VT_COLUMN
            if dims_rows and meas_cols:
                return VT_BAR
        return VT_UNSUPPORTED

    if m in ("bar", "automatic", ""):
        # An Automatic mark over a CONTINUOUS (green) date axis is Tableau's default LINE chart: a
        # continuous date + a measure renders as a line (a discrete date PART -> bars). An explicit
        # ``bar`` mark always stays bars. The field bindings are identical to a line over the same
        # shelves -- only the chart TYPE differs -- so this is squarely Tier-1 "right chart type".
        # Dual-axis / combo splitting still runs downstream on the VT_LINE result, so a
        # column+line combo over a date is unaffected.
        if m in ("automatic", "") and axis_meas and (
                _has_continuous_date(dims_cols) or _has_continuous_date(dims_rows)):
            return VT_LINE
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


def _strip_member_literal(raw):
    """Return a categorical filter member's inner value. Tableau serialises it as a quoted string
    literal (e.g. ``"South"``) or a bare token (``true`` / ``5``); strip the surrounding quotes."""
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def _filter_member_literals(group):
    """Collect the literal member values from a group's direct ``function='member'`` children."""
    out = []
    for gf in _children_local(group, "groupfilter"):
        if gf.get("function") == "member" and gf.get("member") is not None:
            out.append(_strip_member_literal(gf.get("member")))
    return out


def _parse_filter_selection(filt):
    """Extract a categorical filter's applied member selection.

    Returns ``{"mode": "include"|"exclude", "values": [str, ...]}`` for a cleanly enumerated
    selection, else ``None`` (an "all members" filter, or a structure we cannot read faithfully).
    Mirrors the three real Tableau serialisations: a single ``function='member'`` child, a
    ``function='union' op='manual'`` keep-list (include), or a ``function='except'`` wrapper
    (exclude). A non-narrowing or ambiguous filter returns ``None`` so the slicer stays at its
    faithful default (warn-never-wrong: never invent a selection that could hide real data wrong).
    """
    children = _children_local(filt, "groupfilter")
    if not children:
        return None
    members = []
    for child in children:
        fn = child.get("function")
        op = _attr_local(child, "op")
        if fn == "except":
            ex = _filter_member_literals(child)
            return {"mode": "exclude", "values": _dedupe_str(ex)} if ex else None
        if fn == "member" and child.get("member") is not None:
            members.append(_strip_member_literal(child.get("member")))
        elif fn == "union" and op == "manual":
            members.extend(_filter_member_literals(child))
    members = _dedupe_str([m for m in members if m != ""])
    return {"mode": "include", "values": members} if members else None


def _parse_filter_range(filt):
    """Extract a quantitative/date range filter's bounds: ``{"min": str|None, "max": str|None}``
    (or ``None`` when neither bound is present). Tableau wraps date literals in ``#...#``."""
    def _val(el):
        if el is None or el.text is None:
            return None
        t = el.text.strip()
        if len(t) >= 2 and t[0] == "#" and t[-1] == "#":
            t = t[1:-1]
        return t or None
    lo, hi = _val(_first(filt, "min")), _val(_first(filt, "max"))
    return {"min": lo, "max": hi} if (lo is not None or hi is not None) else None


def _dedupe_str(values):
    seen, out = set(), []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _parse_filters(ws, ds_default, base_cols, instances, index, ds_caption,
                   worksheet, warnings, warn_special=True, internal_fields=None):
    """Returns ``(filters, swap_controls)``. ``swap_controls`` carries any parameter-driven
    sheet-swap visibility controls detected on this worksheet (a categorical filter pinned to a
    pure parameter-passthrough calc). Recognising them structurally keeps them from being
    mis-warned as unmappable measure filters and lets :func:`parse_twb` group swap partners."""
    filters = []
    swap_controls = []
    for filt in _findall_local(ws, "filter"):
        cls = (filt.get("class") or "").lower()
        ds, fid = _split_token_attr(filt.get("column"))
        if fid is None:
            continue
        f = _resolve_field(ds or ds_default, fid, base_cols, instances, index,
                           ds_caption, worksheet, warnings, warn_special=warn_special,
                           internal_fields=internal_fields)
        if f is None:
            continue
        # Parameter-driven sheet swap: a categorical filter pinned to a pure passthrough control
        # calc ([Parameters].[id]) gates this whole worksheet's visibility -- it is not a data
        # filter, so record it as a swap control (parse_twb groups partners) and do NOT warn.
        if cls == "categorical":
            ctrl_formula = (base_cols.get((ds or ds_default, f["field_id"])) or {}).get("formula")
            pid = _param_control_ref(ctrl_formula)
            if pid:
                sel = _parse_filter_selection(filt)
                swap_controls.append({
                    "param_id": pid,
                    "calc_caption": f["caption"],
                    "members": list(sel["values"]) if sel and sel.get("mode") == "include" else [],
                })
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
        f["selection"] = _parse_filter_selection(filt) if cls == "categorical" else None
        f["range"] = _parse_filter_range(filt) if cls == "quantitative" else None
        filters.append(f)
    return filters, swap_controls


def _parse_sort(view, ds_default, base_cols, instances, index, ds_caption, worksheet, warnings,
                internal_fields=None):
    """Parse a worksheet ``<computed-sort>`` (sort a dimension by a measure) into an IR directive.

    Tableau serialises an axis sort as ``<computed-sort column='[dim]' direction='ASC|DESC'
    using='[measure]' />``. Returns ``{"field": <resolved sort-by measure>, "direction":
    "Ascending"|"Descending"}`` for the first computed-sort whose ``using`` measure resolves, else
    ``None``. ``<manual-sort>`` (an explicit, frozen member order) has no faithful Power BI sort
    expression, so it is deliberately ignored here (the default model order is used instead).
    """
    for cs in _findall_local(view, "computed-sort"):
        using = _attr_local(cs, "using")
        if not using:
            continue
        uds, ufid = _split_token_attr(using)
        if ufid is None:
            continue
        by = _resolve_field(uds or ds_default, ufid, base_cols, instances, index,
                            ds_caption, worksheet, warnings, warn_special=False,
                            internal_fields=internal_fields)
        if not by or by["kind"] != "value":
            continue
        direction = (_attr_local(cs, "direction") or "ASC").strip().upper()
        return {"field": by,
                "direction": "Descending" if direction == "DESC" else "Ascending"}
    return None


# -- Measure Values / Measure Names expansion (M1.0) ---------------------------
# Power BI has no "Measure Names" field: several measures dropped in one value well auto-produce
# the series / legend / column headers. So [Measure Values] expands to its ordered member
# measures (all exact-bound in the value well) and [Measure Names] is implicit -- never bound
# (binding it as a category/series would be a dangling reference). The authoritative member
# order is the worksheet's categorical filter on [:Measure Names] (its function="member" list,
# in document = shelf order, verified against real workbooks); the <manual-sort> dictionary is
# only a fallback because it retains stale, since-removed members. These are unprotectable
# Tableau<->Power BI behaviour facts, authored independently against our own IR + emitter.
_NUM_LITERAL_RE = re.compile(r"^[-+]?\d+(\.\d*)?$")
_PARAM_SWAP_RE = re.compile(r"(?is)\b(?:case|if)\b.*?\[Parameters\]\.")
_MV_VALUE_TOKENS = ("[Multiple Values]", ":Measure Values]")
# real chart marks for which Measure Names on an axis means small-multiples-by-measure (M1.2).
_MV_CHART_MARKS = {"bar", "line", "area", "circle", "square", "shape", "point", "pie", "gantt"}


def _is_dummy_constant(formula):
    """True when a calculated field is just a numeric literal (a path-hack spacer like ``0``)."""
    return bool(formula) and bool(_NUM_LITERAL_RE.match(formula.strip()))


def _is_param_swap(formula):
    """True for a parameter-driven CASE/IF swap calc (a field-parameter pattern: deferred to M1.3)."""
    return bool(formula) and bool(_PARAM_SWAP_RE.search(formula))


_PARAM_CONTROL_RE = re.compile(r"^\s*\[Parameters\]\.\[([^\]]+)\]\s*$")


def _param_control_ref(formula):
    """Return the parameter id for a *pure passthrough* control calc, else ``None``.

    A parameter-driven sheet swap is wired with a calc whose entire body is a single parameter
    reference (``[Parameters].[Parameter 001...]``). Because that calc is constant across every
    row (it equals the parameter's current value), a worksheet categorical filter pinned to one of
    its members shows the sheet wholesale at that parameter value and hides it otherwise -- i.e. it
    is a visibility control, not a data filter. Detection is deliberately narrow: only an exact
    passthrough qualifies, so a real comparison such as ``[Sales] > [p]`` keeps its ordinary
    (warned) filter handling. The id matches the bracket-stripped column ``name`` indexed by
    :func:`_parse_parameters`. Distinct from :func:`_is_param_swap` (a CASE/IF *field*-parameter).
    """
    if not formula:
        return None
    m = _PARAM_CONTROL_RE.match(formula)
    return m.group(1).strip() if m else None


def _uses_measure_values(rows_text, cols_text, pane):
    """True when the worksheet places the Measure Values shelf (the ``[Multiple Values]`` pill)."""
    blob = (rows_text or "") + " " + (cols_text or "")
    holder = _first(pane, "encodings") if pane is not None else None
    if holder is not None:
        blob += " " + " ".join((c.get("column") or "") for c in list(holder))
    return any(tok in blob for tok in _MV_VALUE_TOKENS)


def _mv_shelf_locations(rows_text, cols_text, pane):
    """Where the Measure Names pill and the Measure Values placeholder sit (shelf / encoding role)."""
    locs = {"names": None, "values": None}

    def mark(where, col):
        if not col:
            return
        if ":Measure Names]" in col and locs["names"] is None:
            locs["names"] = where
        if ("[Multiple Values]" in col or ":Measure Values]" in col) and locs["values"] is None:
            locs["values"] = where

    mark("rows", rows_text)
    mark("cols", cols_text)
    holder = _first(pane, "encodings") if pane is not None else None
    if holder is not None:
        for child in list(holder):
            mark(_local(child.tag), child.get("column"))
    return locs


def _measure_value_member_ids(view, ds_default):
    """Ordered ``(ds, instance_id)`` Measure Values members, plus an enumeration status.

    Returns ``(members, status)`` where ``status`` is one of:

    - ``"ok"``      -- an authoritative keep-list (a ``<groupfilter function="union" op="manual">``
      whose ``function="member"`` children are the *included* measures, in document = shelf
      order) or, when no such filter is present, the ``<manual-sort>`` dictionary fallback.
    - ``"exclude"`` -- the Measure Names filter is an Exclude / non-manual structure
      (``except`` / ``level-members``), where the listed members are the *excluded* set; the
      displayed set cannot be derived from the workbook alone, so the caller must warn + defer
      rather than show the wrong measures.
    - ``"none"``    -- no member source was found.

    The ``<manual-sort>`` dictionary is only a fallback because it keeps stale members that were
    since removed from the shelf.
    """
    def members_of(group):
        out = []
        for gf in _findall_local(group, "groupfilter"):
            if gf.get("function") == "member" and gf.get("member"):
                ds, fid = _split_token_attr(gf.get("member"))
                if fid:
                    out.append((ds or ds_default, fid))
        return out

    for filt in _findall_local(view, "filter"):
        col = filt.get("column") or ""
        if (filt.get("class") or "").lower() != "categorical" \
                or not col.endswith(":Measure Names]"):
            continue
        # the inclusion authority is a *direct* union+manual keep-list; any other top-level group
        # (except / level-members / non-manual union) is an Exclude action whose member list is
        # the removed set -- reading it as the keep-list would surface exactly the wrong measures.
        manual, nonmanual = None, False
        for child in _children_local(filt, "groupfilter"):
            fn = child.get("function")
            op = _attr_local(child, "op")
            if fn == "union" and op == "manual":
                manual = child
            elif fn in ("except", "level-members") or (fn == "union" and op != "manual"):
                nonmanual = True
        if manual is not None:
            mem = members_of(manual)
            if mem:
                return mem, "ok"
        if nonmanual:
            return [], "exclude"
    for ms in _findall_local(view, "manual-sort"):
        if (ms.get("column") or "").endswith(":Measure Names]"):
            members = []
            for b in _findall_local(ms, "bucket"):
                ds, fid = _split_token_attr(b.text or "")
                if fid:
                    members.append((ds or ds_default, fid))
            if members:
                return members, "ok"
    return [], "none"


def _resolve_measure_values(view, ds_default, base_cols, instances, index, ds_caption,
                            worksheet, warnings, internal_fields=None):
    """Resolve the ordered Measure Values members to value fields.

    Drops numeric-literal dummy spacers (the path-hack constant). Returns
    ``(members, dummy_count, has_param_swap, status)`` where ``status`` is the enumeration
    status from :func:`_measure_value_member_ids`.
    """
    member_ids, status = _measure_value_member_ids(view, ds_default)
    members, dummy_count, has_param_swap = [], 0, False
    for ds, fid in member_ids:
        inst = instances.get((ds, fid))
        base_id = inst["column"] if inst else fid
        formula = (base_cols.get((ds, base_id)) or {}).get("formula")
        if _is_dummy_constant(formula):
            dummy_count += 1
            continue
        if _is_param_swap(formula):
            has_param_swap = True
        f = _resolve_field(ds, fid, base_cols, instances, index, ds_caption,
                           worksheet, warnings, internal_fields=internal_fields)
        if f and f["kind"] == "value":
            members.append(f)
    return members, dummy_count, has_param_swap, status


def _route_measure_values(mark, locs, members, dummy_count, has_param_swap, status,
                          dims_rows, dims_cols, worksheet, warnings):
    """Route a Measure Values worksheet to a native visual.

    Returns ``(visual_type, inject_shelf, note)`` where ``inject_shelf`` is the IR shelf the
    member measures join as value fields. An unclassifiable or deliberately deferred case
    returns ``VT_UNSUPPORTED`` and appends one specific structured warning (so a handled case
    never carries a generic false "no model binding" warning).
    """
    m = (mark or "").strip().lower()
    names_at, values_at = locs["names"], locs["values"]
    values_on_text = values_at in ("text", "label")

    # An Exclude / non-manual Measure Names filter lists the REMOVED measures, so the displayed
    # set cannot be derived from the workbook alone -> warn + defer rather than show the wrong set.
    if status == "exclude":
        warnings.append(_warn(
            "worksheet", worksheet,
            "Measure Names uses an Exclude (non-manual) filter; the displayed measure set "
            "cannot be derived faithfully from the workbook (skipped)"))
        return VT_UNSUPPORTED, None, None

    if not members:
        warnings.append(_warn(
            "worksheet", worksheet,
            "Measure Values shelf could not be enumerated to member measures "
            "(no member list found; skipped)"))
        return VT_UNSUPPORTED, None, None

    if has_param_swap:
        warnings.append(_warn(
            "worksheet", worksheet,
            "Measure Values members are parameter-driven swap calculations; a faithful "
            "field-parameter rebuild is deferred (skipped)"))
        return VT_UNSUPPORTED, None, None

    # Path-mark "bar hack": a Line mark with Measure Names on Path (often padded by a dummy
    # constant member) fakes vertical bars. Tier-1 stays MARK-FAITHFUL -- drop the literal
    # spacer(s) and exact-bind the real measure(s) but KEEP the line mark. Re-reading the line as
    # a bar is chart-type adjudication (intent inference), which the two-tier split assigns to the
    # styling/Tier-2 pass, so the note surfaces it instead of silently changing the chart type.
    if m == "line" and names_at == "path":
        dummy_bit = (f"; dropped {dummy_count} dummy constant member"
                     + ("s" if dummy_count != 1 else "")) if dummy_count else ""
        if dims_rows or dims_cols:
            shelf = "cols" if dims_rows else "rows"
            note = (f"detected Tableau path-mark hack (Line mark + Measure Names on Path)"
                    f"{dummy_bit}; kept the line mark and bound {len(members)} real measure(s) "
                    f"(line->bar reinterpretation deferred to a styling pass)")
            return VT_LINE, shelf, note
        note = (f"detected Tableau path-mark hack (Line mark + Measure Names on Path){dummy_bit}; "
                f"no dimension to plot a line, bound {len(members)} measure(s) as a card")
        return VT_CARD, "cols", note

    # Measure Names on Rows/Columns against a real chart mark splits the chart into one pane per
    # measure (small multiples) -> deferred to the trellis pass rather than silently flattened.
    if names_at in ("rows", "cols") and m in _MV_CHART_MARKS and not values_on_text:
        warnings.append(_warn(
            "worksheet", worksheet,
            "Measure Names on rows/columns splits this chart into one pane per measure "
            "(small multiples); deferred (skipped)"))
        return VT_UNSUPPORTED, None, None

    # Measure Names on Color -> the member measures become the series/legend automatically.
    if names_at == "color" and not values_on_text:
        if m == "line":
            vt, shelf = VT_LINE, "rows"
        elif dims_cols and not dims_rows:
            vt, shelf = VT_COLUMN, "rows"
        elif dims_rows:
            vt, shelf = VT_BAR, "cols"
        else:
            vt, shelf = VT_CARD, "cols"
        note = (f"Measure Values -> {len(members)} measures as series; "
                "Measure Names legend is implicit")
        return vt, shelf, note

    # Default: a text table / crosstab (measures as columns) or, with no dimension, a bare
    # multi-measure card. Power BI renders measures-as-columns natively in a matrix.
    vt = VT_MATRIX if (dims_rows or dims_cols) else VT_CARD
    note = f"Measure Values -> {len(members)} measures; Measure Names implicit"
    return vt, "cols", note


# -- worksheet title (structural text only; per-run styling is Tier-2) ----------
_TITLE_DYNAMIC_RE = re.compile(r"<[^<>]+>")


def _parse_worksheet_title(ws):
    """Extract a worksheet's structural caption from ``<layout-options><title>``.

    Returns ``(text, is_dynamic)``. ``text`` is the concatenation of the title's ``<run>`` text
    -- the STRUCTURAL content only; per-run font / colour / size attributes are deliberately
    ignored (that is Tier-2 styling). ``is_dynamic`` is ``True`` when the title embeds a Tableau
    dynamic token (a field / parameter / sheet reference, authored as an escaped ``&lt;...&gt;``
    run that unescapes to ``<...>``), which cannot be reproduced as a static Power BI title --
    the caller defers it (warn) rather than emit a broken literal. ``(None, False)`` when there
    is no explicit, non-empty title.
    """
    layout = _first(ws, "layout-options")
    if layout is None:
        return None, False
    title = _first(layout, "title")
    if title is None:
        return None, False
    ft = _first(title, "formatted-text")
    runs = _findall_local(ft, "run") if ft is not None else []
    text = "".join((r.text or "") for r in runs).strip()
    if not text:
        return None, False
    return text, bool(_TITLE_DYNAMIC_RE.search(text))


# Cartesian visual types that carry an explicit category/value axis pair whose titles can be
# faithfully reproduced. Pie/scatter/matrix/etc. either lack a category-vs-value axis split or
# put measures on both axes, so an axis-title override there is deferred (warn-never-wrong).
_AXIS_TITLE_TYPES = (VT_COLUMN, VT_BAR, VT_LINE, VT_AREA)


def _parse_axis_titles(table, dims_rows, dims_cols, meas_rows, meas_cols):
    """Extract author-overridden axis-title captions from a worksheet's ``<style>`` axis rules.

    Tableau stores an axis-title override as
    ``table/style/style-rule[@element='axis']/format[@attr='title'][@scope]`` -- ``scope`` is
    ``rows`` or ``cols`` (which shelf's axis), and ``value`` is the title text, an EMPTY string
    meaning the author HID that axis title. Quick-filter caption rules live under
    ``style-rule[@element='quick-filter']`` and carry no ``scope``, so they are excluded here.

    The scope is mapped to a Power BI axis STRUCTURALLY by the role of the field(s) on that shelf:
    a shelf holding only the category dimension drives ``categoryAxis``; a shelf holding only the
    measure drives ``valueAxis``. This is orientation-independent -- it works whether the dimension
    sits on rows (a bar) or on cols (a column / line / area). A shelf with a mixed or empty role is
    skipped (never guess which axis a title belongs to).

    Returns a dict optionally containing ``categoryAxis`` / ``valueAxis`` keys, each
    ``{"text": <str|None>, "hide": <bool>}`` (``hide=True`` <=> the author blanked the title).
    """
    if table is None:
        return {}
    style = _first(table, "style")
    if style is None:
        return {}

    def _role(dims, meas):
        if dims and not meas:
            return "categoryAxis"
        if meas and not dims:
            return "valueAxis"
        return None

    scope_axis = {
        "cols": _role(dims_cols, meas_cols),
        "rows": _role(dims_rows, meas_rows),
    }
    out = {}
    for rule in _children_local(style, "style-rule"):
        if (rule.get("element") or "").lower() != "axis":
            continue
        for fmt in _children_local(rule, "format"):
            if (fmt.get("attr") or "") != "title":
                continue
            scope = fmt.get("scope")
            if scope not in ("rows", "cols"):
                continue
            axis = scope_axis.get(scope)
            if axis is None or axis in out:
                continue
            value = fmt.get("value")
            if value is None:
                continue
            text = value.strip()
            out[axis] = {"text": text or None, "hide": not text}
    return out


# A pill instance token can wrap the underlying field in a Tableau quick table calc -- e.g.
# "Percent Difference From" -> ``pcdf:``, running total -> ``cum:``, the window aggregates ->
# ``w*:``, INDEX/RANK -> ``index:`` / ``rank:``. Such a pill computes a DERIVED quantity that is
# NOT a plain model measure, so a background colour scale driven by one must DEFER (warn) until the
# model build lands an equivalent measure -- colouring by the mis-resolved BASE measure (the table
# calc's input, which is what ``_resolve_field`` recovers) would be confidently wrong. A plain
# aggregation or a clean calc measure carries no such leading code, so this gate stays off for the
# common heat-table case. The codes below are the unambiguous table-calc prefixes only; short
# words that could collide with a real field id (``size``/``first``/``last``/``total``) are left out.
_TABLE_CALC_CODES = frozenset({
    "cum", "rsum", "pcdf", "pdiff", "diff", "pcto", "rdiff",
    "wsum", "wavg", "wmin", "wmax", "wstdev", "wstdevp", "wvar", "wvarp",
    "wmedian", "wcount", "wcountd", "wcorr", "wcov",
    "movsum", "movavg", "movmin", "movmax", "movstdev", "movvar",
    "index", "rank", "rank_dense", "rank_modified", "rank_percentile", "rank_unique",
})


def _instance_is_table_calc(instance):
    """True when a pill instance token's leading code is a known quick table-calc op."""
    seg = (instance or "").split(":", 1)[0]
    return seg in _TABLE_CALC_CODES


# A continuous (heat) colour scale lives at
# ``worksheet/table/style/style-rule[@element='mark']/encoding[@attr='color']`` with an inner
# ``<color-palette>`` and either an interpolated encoding ``type`` (``custom-interpolated`` /
# ``interpolated``) or an ordered palette ``type`` (sequential / diverging). The ``center`` attr
# (when present) is the diverging mid-point; the ordered ``<color>`` children run min -> max in
# author order. A DISCRETE (categorical) colour legend is NOT a gradient -- that is a Tier-2 legend
# styling concern, not a cell heat scale -- and is ignored here.
_GRADIENT_PALETTE_TYPES = ("ordered-diverging", "ordered-sequential")


def _parse_color_gradient(table):
    """Extract a continuous background colour-scale spec from a worksheet's mark colour encoding.

    Returns ``{"field_token", "center", "palette_type", "colors", "interpolated",
    "is_table_calc"}`` when the colour encoding carries a continuous (interpolated / ordered)
    palette of at least two stops, else ``None``. ``colors`` preserves the Tableau author order
    (first -> min, last -> max); the direction is never guessed.
    """
    if table is None:
        return None
    style = _first(table, "style")
    if style is None:
        return None
    for rule in _children_local(style, "style-rule"):
        if (rule.get("element") or "").lower() != "mark":
            continue
        for enc in _children_local(rule, "encoding"):
            if (enc.get("attr") or "") != "color":
                continue
            palette = _first(enc, "color-palette")
            if palette is None:
                continue
            enc_type = (enc.get("type") or "").lower()
            pal_type = (palette.get("type") or "").lower()
            interpolated = "interpolated" in enc_type
            if not interpolated and pal_type not in _GRADIENT_PALETTE_TYPES:
                continue
            colors = [(c.text or "").strip()
                      for c in _children_local(palette, "color")
                      if (c.text or "").strip()]
            if len(colors) < 2:
                continue
            center = None
            raw_center = enc.get("center")
            if raw_center is not None:
                try:
                    center = float(raw_center)
                except (TypeError, ValueError):
                    center = None
            _, fid = _split_token_attr(enc.get("field"))
            return {
                "field_token": enc.get("field") or "",
                "center": center,
                "palette_type": (pal_type or ("ordered-diverging" if center is not None
                                              else "ordered-sequential")),
                "colors": colors,
                "interpolated": interpolated,
                "is_table_calc": _instance_is_table_calc(fid),
            }
    return None


# Tableau analytic-annotation elements live at ``table/panes/pane/<element>``: a reference /
# target / distribution line overlays a computed constant, average, percentile band, or an
# explicit goal on the mark, and a trend line overlays a fitted model. Power BI expresses these as
# visual-level analytics (or a richer KPI visual for a single-value target) -- a Tier-2 analytics /
# formatting concern Tier-1 cannot redraw faithfully. They are recorded (additive, for a later
# analytics pass) and surfaced as a warning; the underlying visual is unaffected. A reference line
# on a single-value card is exactly a KPI target/goal, so the warning calls that case out.
_REFERENCE_LINE_TAGS = ("reference-line", "reference-distribution", "reference-band")
_REF_INSTANCE_RE = re.compile(r"^[a-z]+:(.+):[a-z]{2}$")


def _annotation_label(el):
    """Human-readable name for a reference annotation: its custom label (auto ``<Value>`` tokens
    stripped), else ``<formula> of <target field>`` derived from the ``value-column`` instance."""
    label = (el.get("label") or "").strip()
    if label and (el.get("label-type") or "").lower() == "custom":
        cleaned = re.sub(r"\s*<[^>]*>", "", label).strip()
        if cleaned:
            return cleaned
    formula = (el.get("formula") or "").strip()
    target = _parse_item(el.get("value-column") or "") or ""
    m = _REF_INSTANCE_RE.match(target)
    if m:
        target = m.group(1)
    if formula and target:
        return "{0} of {1}".format(formula, target)
    return target or formula or "reference line"


def _parse_reference_lines(all_panes):
    """Collect reference / target / distribution and trend line annotations across a worksheet's
    panes into additive descriptor dicts ``{"kind", "label", "formula"}``."""
    refs = []
    for pn in all_panes:
        for tag in _REFERENCE_LINE_TAGS:
            for el in _children_local(pn, tag):
                refs.append({"kind": "reference_line",
                             "label": _annotation_label(el),
                             "formula": (el.get("formula") or "").strip() or None})
        for el in _findall_local(pn, "trend-line"):
            refs.append({"kind": "trend_line", "label": "trend line", "formula": None})
    return refs


def _parse_worksheet(ws, index, ds_caption, warnings, internal_fields=None, date_binding=None,
                     row_count_binding=None, measure_binding=None):
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
    all_panes = _findall_local(panes, "pane") if panes is not None else []
    pane = all_panes[0] if all_panes else None
    # Dual-axis pie/donut hack: the meaningful mark can live in a NON-primary pane (e.g. a Pie
    # pane hidden behind MIN(0) spacer axes that fake a donut ring). When a Pie pane is present,
    # drive the worksheet off it so its legend (colour) + angle (wedge-size) encodings are read
    # instead of the empty spacer pane. A genuine single-pane pie is unaffected (same pane).
    pie_pane = next(
        (p for p in all_panes
         if _first(p, "mark") is not None
         and (_first(p, "mark").get("class") or "").lower() == "pie"),
        None)
    donut_hack = pie_pane is not None and len(all_panes) > 1
    if pie_pane is not None:
        pane = pie_pane
    mark_el = _first(pane, "mark") if pane is not None else None
    mark = mark_el.get("class") if mark_el is not None else "Automatic"

    rows_el = _first(table, "rows")
    cols_el = _first(table, "cols")
    rows_text = (rows_el.text if rows_el is not None else "") or ""
    cols_text = (cols_el.text if cols_el is not None else "") or ""
    uses_mv = _uses_measure_values(rows_text, cols_text, pane)
    warn_special = not uses_mv
    rows = _resolve_shelf(rows_text, ds_default, base_cols, instances, index,
                          ds_caption, name, warnings, warn_special=warn_special,
                          internal_fields=internal_fields, date_binding=date_binding,
                          row_count_binding=row_count_binding, measure_binding=measure_binding)
    cols = _resolve_shelf(cols_text, ds_default, base_cols, instances, index,
                          ds_caption, name, warnings, warn_special=warn_special,
                          internal_fields=internal_fields, date_binding=date_binding,
                          row_count_binding=row_count_binding, measure_binding=measure_binding)
    encodings = _parse_encodings(pane, ds_default, base_cols, instances, index,
                                 ds_caption, name, warnings, warn_special=warn_special,
                                 internal_fields=internal_fields, date_binding=date_binding,
                                 row_count_binding=row_count_binding, measure_binding=measure_binding)
    filters, swap_controls = _parse_filters(view, ds_default, base_cols, instances, index,
                                            ds_caption, name, warnings, warn_special=warn_special,
                                            internal_fields=internal_fields)
    sort = _parse_sort(view, ds_default, base_cols, instances, index,
                       ds_caption, name, warnings, internal_fields=internal_fields)

    dims_rows = [f for f in rows if f["kind"] == "category"]
    dims_cols = [f for f in cols if f["kind"] == "category"]
    meas_rows = [f for f in rows if f["kind"] == "value"]
    meas_cols = [f for f in cols if f["kind"] == "value"]

    fidelity_note = None
    combo_split = None
    if uses_mv:
        # Measure Values/Names (M1.0): expand [Measure Values] to its ordered member measures in
        # the value well and route by mark + where the (implicit) Measure Names pill sits. The
        # member value fields join the IR shelves so the existing emitter binds them unchanged.
        locs = _mv_shelf_locations(rows_text, cols_text, pane)
        members, dummy_count, has_param_swap, mv_status = _resolve_measure_values(
            view, ds_default, base_cols, instances, index, ds_caption, name, warnings,
            internal_fields=internal_fields)
        visual_type, inject_shelf, fidelity_note = _route_measure_values(
            mark, locs, members, dummy_count, has_param_swap, mv_status,
            dims_rows, dims_cols, name, warnings)
        if visual_type != VT_UNSUPPORTED:
            if inject_shelf == "rows":
                rows = rows + members
            else:
                cols = cols + members
    else:
        # marks-card encodings also carry fields: color/detail can be the disaggregating
        # dimension (scatter) and label/size can be the measure of a bare card / KPI tile.
        enc_dims = [f for f in (encodings["color"], encodings["detail"])
                    if f and f["kind"] == "category"]
        enc_meas = [f for f in (encodings["size"], encodings["label"], encodings["angle"])
                    if f and f["kind"] == "value"]
        # geographic map signals: a geo-role dimension on Detail is the Location; a measure on
        # any shelf/encoding feeds Color/Size; generated lat/lon on the axes or a geometry
        # encoding is the extra spatial confirmation that separates a map from a normal chart.
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

        # Dual-axis combo: when a chart layout's measures split into a column-family group and a
        # line-family group (each measure's mark read from its own dual-axis pane), re-route to a
        # combo chart so the column measure(s) land on Y and the line measure(s) on Y2. Same-mark
        # multi-measure shelves keep their ordinary single-mark visual (no false combos).
        if visual_type in (VT_COLUMN, VT_BAR, VT_LINE, VT_AREA):
            mark_by_instance, primary_mark, _ = _pane_mark_map(table)
            column_meas, line_meas = _detect_combo(
                meas_rows, meas_cols, bool(dims_rows or dims_cols),
                mark_by_instance, primary_mark)
            if column_meas and line_meas:
                visual_type = VT_COMBO
                combo_split = {"Y": column_meas, "Y2": line_meas}
                fidelity_note = (
                    "dual-axis combo: column measure(s) on the primary axis + line measure(s) "
                    "on the secondary axis -> lineClusteredColumnComboChart")

        # Bump / rank chart hack: a manual rank built from an INDEX()/RANK() table calc plotted on
        # an axis (often a doubled dual-axis spacer), with the real ranked measure on a marks-card
        # encoding and a legend dimension colouring the ranked members. Power BI's native
        # ribbonChart recomputes the rank from the base measure, so the table-calc rank axis is
        # dropped (like the waterfall's running total) and Category (the ordinal/time axis) +
        # Series (the legend) + Y (the base measure) bind to real model fields. Gated on the rank
        # table-calc signal so ordinary column/bar/line charts never misfire.
        if visual_type in (VT_COLUMN, VT_BAR, VT_LINE, VT_AREA) and combo_split is None:
            axis_rank_calc = any(
                f["is_calc"] and _RANK_TABLECALC_RE.search(f.get("formula") or "")
                for f in (meas_rows + meas_cols))
            ribbon_meas = next(
                (f for f in (encodings["detail"], encodings["size"], encodings["label"])
                 if f and f["kind"] == "value" and not f["is_calc"]), None)
            ribbon_legend = bool(color and color["kind"] == "category"
                                 and not color["is_calc"])
            if (axis_rank_calc and ribbon_meas is not None and ribbon_legend
                    and (dims_rows or dims_cols)):
                visual_type = VT_RIBBON
                fidelity_note = (
                    "manual rank (INDEX/RANK table calc) bump chart -> native ribbonChart "
                    "(Power BI recomputes the rank from the base measure; the table-calc rank "
                    "axis dropped)")

        # Dual-axis pie/donut hack: a Pie mark stacked behind MIN(0) spacer axes (to fake a
        # donut ring with a hollow centre) routes to a native donutChart. The real slices are the
        # Pie pane's colour (legend -> Category) + wedge-size (angle -> Y); the spacer axes are
        # dropped by the dedicated donut emit. A plain single-pane pie stays a pieChart.
        if visual_type == VT_PIE and donut_hack:
            visual_type = VT_DONUT
            fidelity_note = (
                "dual-axis pie/donut hack -> native donutChart "
                "(legend + angle read from the Pie pane; MIN(0) spacer axes dropped)")

        # Running-total Gantt waterfall hack: a GanttBar mark whose value axis is a running-total
        # quick table calc (`cum:`) renders as a floating waterfall. Power BI's native
        # waterfallChart recomputes the running total, so Category = the dimension axis and
        # Y = the base measure (the running-total pill already resolves to its base aggregation);
        # the per-step gantt size delta + sentiment colour are dropped. Gated on the running-total
        # signal so ordinary Gantt timelines (project schedules) stay unsupported -> warn.
        if visual_type == VT_UNSUPPORTED and (mark or "").strip().lower() in ("ganttbar", "gantt"):
            running_total = bool(_RUNNING_TOTAL_RE.search(rows_text)
                                 or _RUNNING_TOTAL_RE.search(cols_text))
            if running_total and (dims_rows or dims_cols) and (meas_rows or meas_cols):
                visual_type = VT_WATERFALL
                fidelity_note = (
                    "running-total Gantt hack -> native waterfallChart "
                    "(Power BI recomputes the running total; per-step gantt size dropped)")

        # Single-dimension "text list" display: a lone categorical field carried only on the
        # marks card (label / colour / detail) with no measure anywhere and no axis pills is
        # Tableau's Automatic text rendering of that field -> a faithful one-column table that
        # lists its distinct values. Geographic dimensions are excluded (those are maps, deferred
        # to map routing) so a location field is never flattened into a plain list.
        if visual_type == VT_UNSUPPORTED and not (dims_rows or dims_cols) and not geo_detail:
            display_dims = [f for f in (encodings["label"], encodings["color"],
                                        encodings["detail"])
                            if f and f["kind"] == "category"]
            has_any_measure = bool(
                meas_rows or meas_cols or enc_meas
                or (color and color["kind"] == "value")
                or (detail and detail["kind"] == "value"))
            if display_dims and not has_any_measure:
                visual_type = VT_TABLE

        if visual_type == VT_UNSUPPORTED:
            raw_present = bool(_TOKEN_RE.search(rows_text or "")
                               or _TOKEN_RE.search(cols_text or ""))
            enc_holder = _first(pane, "encodings") if pane is not None else None
            enc_present = enc_holder is not None and len(list(enc_holder)) > 0
            is_empty = (not rows and not cols and not any(encodings.values())
                        and not raw_present and not enc_present)
            if is_empty:
                # A structurally bare worksheet (a blank/text/image placeholder a dashboard uses
                # for spacing or a title) is not an unsupported *visual* -- there is simply nothing
                # to rebuild. Classifying it precisely keeps it out of the "unsupported mark" count.
                warnings.append(_warn(
                    "worksheet", name,
                    "empty worksheet (no fields on any shelf or encoding) -> nothing to rebuild"))
            elif (mark or "").strip().lower() in _DEFER_MAP_MARKS or (geo_detail and map_meas):
                warnings.append(_warn(
                    "worksheet", name,
                    f"spatial/custom-geometry map (mark '{mark}') deferred "
                    f"(basics only: filled + symbol map) -> no visual emitted"))
            else:
                warnings.append(_warn(
                    "worksheet", name,
                    f"mark class '{mark}' / shelf layout not supported -> no visual emitted"))

    title_text, title_dynamic = _parse_worksheet_title(ws)
    if visual_type == VT_UNSUPPORTED:
        title_text = None
    elif title_dynamic:
        warnings.append(_warn(
            "worksheet", name,
            "dynamic title (embeds a field/parameter reference) not reproduced as static text; "
            "the rebuilt visual keeps its default title"))
        title_text = None

    axis_titles = {}
    if visual_type in _AXIS_TITLE_TYPES:
        axis_titles = _parse_axis_titles(table, dims_rows, dims_cols, meas_rows, meas_cols)

    # Continuous background colour scale (heat / gradient cells) on a table or matrix. Parsed here
    # (additive IR key) and turned into a PBIR backColor FillRule at emit time -- faithful-or-warn,
    # so a colour driver the model cannot yet bind (a quick table calc) defers rather than colours
    # by the wrong measure. Only the table/matrix family carries a cell heat scale.
    color_gradient = None
    if visual_type in (VT_MATRIX, VT_TABLE):
        color_gradient = _parse_color_gradient(table)

    # Reference / target / trend line annotations (KPI goals, average/percentile bands, trend
    # fits) are a Tier-2 analytics concern: record them (additive) and disclose them so the
    # rebuilt visual is never silently missing an author's target overlay. Gated on an emitted
    # visual -- an unsupported worksheet is already wholly deferred, so no extra warning is added.
    reference_lines = []
    if visual_type != VT_UNSUPPORTED:
        reference_lines = _parse_reference_lines(all_panes)
        if reference_lines:
            is_card = visual_type == VT_CARD
            labels = ", ".join(dict.fromkeys(r["label"] for r in reference_lines))
            warnings.append(_warn(
                "worksheet", name,
                "{0}(s) deferred (Tier-2 analytics): {1} -> the rebuilt {2} shows the value "
                "without the target/trend overlay".format(
                    "KPI target/goal" if is_card else "reference/target/trend line",
                    labels,
                    "card" if is_card else "visual")))

    return {
        "name": name,
        "datasource": primary_caption,
        "datasource_name": ds_default,
        "mark_class": mark,
        "visual_type": visual_type,
        "title": title_text,
        "axis_titles": axis_titles,
        "color_gradient": color_gradient,
        "reference_lines": reference_lines,
        "rows": rows,
        "cols": cols,
        "encodings": encodings,
        "filters": filters,
        "swap_controls": swap_controls,
        "fidelity_note": fidelity_note,
        "combo_split": combo_split,
        "sort": sort,
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

    # A dashboard's <devicelayouts> hold alternate (phone/tablet) arrangements of the SAME
    # worksheet zones. Their zones must be excluded or every worksheet is emitted twice and the
    # canvas extent is corrupted by phone-scale coordinates; only the primary layout is faithful.
    device_zones = set()
    for holder in _findall_local(db, "devicelayouts"):
        device_zones.update(_findall_local(holder, "zone"))

    zones = []
    param_controls = []
    seen_params = set()
    ext_w = ext_h = 0.0
    for zone in _findall_local(db, "zone"):
        if zone in device_zones:
            continue
        x, y = _zone_num(zone, "x"), _zone_num(zone, "y")
        w, h = _zone_num(zone, "w"), _zone_num(zone, "h")
        if None not in (x, y, w, h) and w > 0 and h > 0:
            # canvas extent spans every zone (incl. layout containers), in Tableau's
            # internal coordinate units -- the correct frame for scaling, NOT <size>
            # (which is pixels and a different unit system).
            ext_w = max(ext_w, x + w)
            ext_h = max(ext_h, y + h)
        ztype = zone.get("type-v2") or zone.get("type")
        # A parameter-control ("hamburger") zone hosts a Tableau parameter on the dashboard.
        # Capture it structurally so the fidelity report is honest about it: Tier-1 rebuilds it
        # as a slicer only once the model identifies the parameter's target column/measure, so
        # here we record the parameter id + faithful geometry and never silently drop it.
        if ztype == "paramctrl":
            pid = _param_control_ref(zone.get("param") or "")
            if pid and pid not in seen_params and None not in (x, y, w, h):
                seen_params.add(pid)
                param_controls.append({"param_id": pid, "x": x, "y": y, "w": w, "h": h})
            continue
        zname = zone.get("name")
        if not zname or zname not in worksheet_names:
            continue
        # worksheet zones carry no decoration type (legends/filters/titles do)
        if ztype:
            continue
        if None in (x, y, w, h) or w <= 0 or h <= 0:
            continue
        zones.append({"worksheet": zname, "x": x, "y": y, "w": w, "h": h})

    return {"name": name, "size": size,
            "extent": {"w": ext_w or None, "h": ext_h or None}, "zones": zones,
            "param_controls": param_controls}


def _warn(scope, name, reason):
    return {"scope": scope, "name": name,
            "reason": "manual attention required: " + reason}


def _resolve_parameter_controls(dashboards, params, warnings):
    """Resolve each dashboard's captured parameter-control zones to a fidelity record + warning.

    A dashboard parameter control (the "hamburger" on the canvas) hosts a Tableau parameter; Tier-1
    rebuilds it as a slicer only once the migrated model identifies the parameter's target column or
    measure (its kind + bound object). Until that binding is available this records the control
    additively (``ir["parameter_controls"]``) and emits one honest per-control warning so the report
    never silently loses it (warn-never-wrong). The parameter caption/datatype come from
    :func:`_parse_parameters`; the id is the bracket-stripped ``[Parameters].[<id>]`` reference.
    """
    records = []
    for db in dashboards:
        for pc in db.get("param_controls", []):
            pid = pc["param_id"]
            meta = params.get(pid) or {}
            caption = meta.get("caption") or pid
            records.append({
                "param_id": pid,
                "caption": caption,
                "datatype": meta.get("datatype") or None,
                "dashboard": db.get("name"),
                "position": {"x": pc.get("x"), "y": pc.get("y"),
                             "w": pc.get("w"), "h": pc.get("h")},
            })
            warnings.append(_warn(
                "dashboard", db.get("name"),
                f"parameter control '{caption}' not rebuilt as a slicer yet -> emit once the "
                f"migrated model identifies the parameter's target column/measure"))
    return records


def _parse_parameters(root):
    """Index workbook parameters: ``{param_id: {"caption", "datatype", "members":[{value, alias}]}}``.

    A Tableau parameter lives as a column in the reserved ``Parameters`` datasource; its id is the
    bracket-stripped column ``name`` (e.g. ``Parameter 0013965827592222``), which is exactly what a
    ``[Parameters].[<id>]`` reference resolves to. Member values serialise as quoted literals
    (``"1"``) with a display ``alias`` (``line``) -- carried inline on ``<member>`` and/or in an
    ``<aliases><alias key value>`` map -- so both forms are read and the literal stripped to match a
    filter's selected member.
    """
    params = {}
    datasources = []
    for h in _children_local(root, "datasources"):
        datasources.extend(_children_local(h, "datasource"))
    for ds in datasources:
        if (ds.get("name") or "") != "Parameters":
            continue
        for col in _findall_local(ds, "column"):
            pid = _strip_brackets((col.get("name") or "").strip())
            if not pid:
                continue
            alias_map = {}
            for al in _findall_local(col, "alias"):
                key = _strip_member_literal(al.get("key"))
                if key:
                    alias_map[key] = al.get("value")
            members, seen = [], set()
            for m in _findall_local(col, "member"):
                val = _strip_member_literal(m.get("value"))
                if val in seen:
                    continue
                seen.add(val)
                members.append({"value": val, "alias": m.get("alias") or alias_map.get(val)})
            for key, disp in alias_map.items():
                if key not in seen:
                    seen.add(key)
                    members.append({"value": key, "alias": disp})
            params[pid] = {
                "caption": col.get("caption") or pid,
                "datatype": (col.get("datatype") or "").lower(),
                "members": members,
            }
    return params


def _detect_sheet_swaps(worksheets, dashboards, params, warnings):
    """Group worksheets that toggle within one dashboard zone via a shared swap parameter.

    A *sheet swap* is the very common Tableau idiom where two (or more) worksheets are stacked in
    the same dashboard zone and a parameter chooses which one shows, each sheet carrying a
    visibility control filter (see :func:`_param_control_ref`) pinned to a distinct parameter
    member. Power BI has no native parameter-driven sheet swap, so every worksheet is still rebuilt
    as its own visual; this records the grouping (additive ``sheet_swaps`` IR) and emits ONE precise
    note per group so the swap can be reproduced with a bookmark / field parameter (a Tier-2
    interaction step). Sheet swaps show only one state in a single rendered frame, so they are
    recognised here, deterministically, rather than left to any image-based review.
    """
    by_param = {}
    for w in worksheets:
        for sc in (w.get("swap_controls") or []):
            by_param.setdefault(sc["param_id"], []).append((w["name"], sc))
    swaps = []
    for pid, entries in by_param.items():
        if len({n for n, _ in entries}) < 2:
            continue  # a lone gated sheet is a visibility toggle, not a swap pair
        pinfo = params.get(pid, {})
        caption = pinfo.get("caption", pid)
        alias_by_value = {m["value"]: m.get("alias") for m in pinfo.get("members", [])}
        assignments = []
        for wname, sc in entries:
            shown_for = [{"value": v, "alias": alias_by_value.get(v)}
                         for v in (sc.get("members") or [])]
            assignments.append({"worksheet": wname, "shown_for": shown_for})
        names = {n for n, _ in entries}
        host = None
        for db in dashboards:
            if len(names & {z["worksheet"] for z in db["zones"]}) >= 2:
                host = db["name"]
                break
        swaps.append({"param_id": pid, "param_caption": caption,
                      "dashboard": host, "assignments": assignments})
        labels = "; ".join(
            "'{0}' shown when '{1}' = {2}".format(
                a["worksheet"], caption,
                "/".join((s["alias"] or s["value"]) for s in a["shown_for"]) or "(a member)")
            for a in assignments)
        warnings.append(_warn(
            "dashboard" if host else "workbook", host or caption,
            "parameter-driven sheet swap on '{0}': {1}. Each worksheet is rebuilt as its own "
            "visual; reproduce the dynamic swap with a Power BI bookmark or a field parameter "
            "driving visual visibility (dynamic visibility is a Tier-2 interaction step).".format(
                caption, labels)))
    return swaps


def parse_twb(xml_text, *, date_binding=None, row_count_binding=None, measure_binding=None):
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

    index, ds_caption, internal_fields = _build_field_index(root)
    warnings = []

    ws_holder = _children_local(root, "worksheets")
    ws_elems = []
    for h in ws_holder:
        ws_elems.extend(_children_local(h, "worksheet"))
    worksheets = []
    for ws in ws_elems:
        parsed = _parse_worksheet(ws, index, ds_caption, warnings,
                                  internal_fields=internal_fields, date_binding=date_binding,
                                  row_count_binding=row_count_binding,
                                  measure_binding=measure_binding)
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

    params = _parse_parameters(root)
    parameter_controls = _resolve_parameter_controls(dashboards, params, warnings)
    sheet_swaps = _detect_sheet_swaps(worksheets, dashboards, params, warnings)

    return {"worksheets": worksheets, "dashboards": dashboards,
            "sheet_swaps": sheet_swaps, "parameter_controls": parameter_controls,
            "warnings": warnings}


# -- PBIR field expression emission --------------------------------------------
def _apply_override(field, model_table, field_map):
    """Return (entity, property, binding) after applying caller overrides.

    A field already rebound to the marked Date dimension by ``_rebind_date_axis`` is AUTHORITATIVE:
    neither ``field_map`` nor the ``model_table`` fallback may pull the active date axis back onto the
    fact's raw date column, so the model build's date facts win over the published-DS column rebind.
    """
    entity, prop, binding = field["entity"], field["property"], field["binding"]
    if field.get("date_rebound"):
        return entity, prop, binding
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
    angle = ws["encodings"].get("angle")

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
    if vt == VT_COMBO:
        # Dual-axis combo: the shared dimension(s) form the Category axis; the column-family
        # measures go to Y (primary axis) and the line-family measures to Y2 (secondary axis),
        # per the split classified at parse time. A colour dimension is the column Series/legend.
        split = ws.get("combo_split") or {}
        cat = drop_calc_axis(_dedupe(categories(rows) + categories(cols)))
        y_meas = _dedupe(split.get("Y", []))
        y2_meas = _dedupe(split.get("Y2", []))
        series = [color] if (color and color["kind"] == "category"
                             and not color["is_calc"]) else []
        cat = [f for f in cat if f not in series]
        if cat:
            state["Category"] = {"projections": _role_projections(
                cat, model_table, field_map, used_refs)}
        if y_meas:
            state["Y"] = {"projections": _role_projections(
                y_meas, model_table, field_map, used_refs)}
        if y2_meas:
            state["Y2"] = {"projections": _role_projections(
                y2_meas, model_table, field_map, used_refs)}
        if series:
            state["Series"] = {"projections": _role_projections(
                series, model_table, field_map, used_refs)}
    elif vt == VT_WATERFALL:
        # Running-total Gantt waterfall hack -> native waterfallChart. Category = the dimension
        # axis, Y = the base measure (Power BI recomputes the cumulative; the running-total pill
        # already resolved to its base aggregation). A colour DIMENSION maps to the waterfall's
        # Breakdown role (segments each bar); the per-step gantt size delta is dropped.
        cat = drop_calc_axis(_dedupe(categories(rows) + categories(cols)))
        val = _dedupe(values(rows) + values(cols))
        breakdown = [color] if (color and color["kind"] == "category"
                                and not color["is_calc"]) else []
        cat = [f for f in cat if f not in breakdown]
        if cat:
            state["Category"] = {"projections": _role_projections(
                cat, model_table, field_map, used_refs)}
        if val:
            state["Y"] = {"projections": _role_projections(
                val, model_table, field_map, used_refs)}
        if breakdown:
            state["Breakdown"] = {"projections": _role_projections(
                breakdown, model_table, field_map, used_refs)}
    elif vt == VT_DONUT:
        # Dual-axis pie/donut hack -> native donutChart. The real slices live on the Pie pane's
        # colour (legend -> Category) + wedge-size (angle -> Y); the MIN(0) spacer axes that fake
        # the donut ring are ignored. Same Category/Y role shape as pieChart.
        legend = drop_calc_axis(_dedupe(
            [color] if color and color["kind"] == "category" else []))
        vals = _dedupe(
            ([angle] if angle and angle["kind"] == "value" else [])
            + ([size] if size and size["kind"] == "value" else [])
            + ([label] if label and label["kind"] == "value" else []))
        if legend:
            state["Category"] = {"projections": _role_projections(
                legend, model_table, field_map, used_refs)}
        if vals:
            state["Y"] = {"projections": _role_projections(
                vals[:1], model_table, field_map, used_refs)}
    elif vt == VT_RIBBON:
        # Bump / rank hack -> native ribbonChart. Category = the ordinal/time axis dimension,
        # Series = the legend dimension (the ranked members), Y = the base measure (Power BI
        # recomputes the rank from it). The INDEX()/RANK() table-calc rank/spacer axis pills are
        # dropped (they are value-role calc artifacts, never categories, so they never reach a
        # role). Role keys Category/Series/Y verified against real Microsoft PBIR ribbonChart files.
        series = [color] if (color and color["kind"] == "category"
                             and not color["is_calc"]) else []
        cat = drop_calc_axis(_dedupe(categories(rows) + categories(cols)))
        cat = [f for f in cat if f not in series]
        ribbon_val = next((f for f in (detail, size, label)
                           if f and f["kind"] == "value" and not f["is_calc"]), None)
        if cat:
            state["Category"] = {"projections": _role_projections(
                cat, model_table, field_map, used_refs)}
        if ribbon_val is not None:
            state["Y"] = {"projections": _role_projections(
                [ribbon_val], model_table, field_map, used_refs)}
        if series:
            state["Series"] = {"projections": _role_projections(
                series, model_table, field_map, used_refs)}
    elif vt in (VT_COLUMN, VT_BAR):
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
    elif vt in (VT_LINE, VT_AREA):
        # A line/area chart's x-axis is the continuous shelf: Tableau puts the date/continuous
        # dimension on Columns. A discrete dimension on the OTHER shelf (Rows) panes the line
        # per member -- a small multiple (trellis). That maps to Power BI's native Small
        # multiples well (one pane per member), which is faithful to the Tableau layout; a
        # colour-encoding dimension is the legend/Series. Keeping the date on Category prevents
        # the discrete dimension from displacing the date off the x-axis.
        col_cats = drop_calc_axis(_dedupe(categories(cols)))
        row_cats = drop_calc_axis(_dedupe(categories(rows)))
        val = _dedupe(values(rows) + values(cols))
        color_series = [color] if (color and color["kind"] == "category"
                                   and not color["is_calc"]) else []
        if col_cats:
            cat = col_cats
            small = row_cats          # rows paning dimension -> small multiples (trellis)
            series = color_series     # colour legend -> series
        else:
            cat = row_cats
            small = []
            series = color_series
        small = [f for f in small if f not in cat]
        series = [f for f in series if f not in cat and f not in small]
        if cat:
            state["Category"] = {"projections": _role_projections(
                cat, model_table, field_map, used_refs)}
        if val:
            state["Y"] = {"projections": _role_projections(
                val, model_table, field_map, used_refs)}
        if series:
            state["Series"] = {"projections": _role_projections(
                series, model_table, field_map, used_refs)}
        if small:
            state["SmallMultiples"] = {"projections": _role_projections(
                small, model_table, field_map, used_refs)}
    elif vt == VT_MATRIX:
        row_dims = drop_calc_axis(_dedupe(categories(rows)))
        col_dims = drop_calc_axis(_dedupe(categories(cols)))
        # a highlight table carries its measure on the colour (saturation) encoding; in a Tier-1
        # matrix that measure is the displayed Values (the colour styling itself is deferred).
        vals = _dedupe(values(rows) + values(cols)
                       + ([color] if color and color["kind"] == "value" else [])
                       + ([label] if label and label["kind"] == "value" else []))
        # Heat-grid colour DRIVER -> tooltip, not a visible column. When a continuous colour scale
        # colours a DISTINCT displayed value (Tableau "colour by a different field"), the colour
        # measure is not shown as its own matrix column: it is surfaced on the TOOLTIP (faithful to
        # Tableau's default colour-card tooltip) and referenced by the background-gradient FillRule.
        # Only fires when there is another displayed value AND a gradient is present, so the classic
        # highlight table (colour == the shown measure) is unchanged.
        tooltip_meas = []
        if ws.get("color_gradient") and color and color["kind"] == "value":
            ck = (color["entity"], color["property"], color["binding"], color["aggregation"])
            others = [f for f in vals
                      if (f["entity"], f["property"], f["binding"], f["aggregation"]) != ck]
            if others:
                vals = others
                tooltip_meas = [color]
        if row_dims:
            state["Rows"] = {"projections": _role_projections(
                row_dims, model_table, field_map, used_refs)}
        if col_dims:
            state["Columns"] = {"projections": _role_projections(
                col_dims, model_table, field_map, used_refs)}
        if vals:
            state["Values"] = {"projections": _role_projections(
                vals, model_table, field_map, used_refs)}
        if tooltip_meas:
            state["Tooltips"] = {"projections": _role_projections(
                tooltip_meas, model_table, field_map, used_refs)}
    elif vt == VT_TABLE:
        ordered = drop_calc_axis(_dedupe(
            categories(rows) + categories(cols))) + _dedupe(
            values(rows) + values(cols)
            + ([label] if label and label["kind"] == "value" else []))
        if not ordered:
            # Encoding-only display (Automatic/text mark with the field(s) on label / colour /
            # detail and no axis pills): list whatever single dimension was placed on the marks
            # card as a one-column table. Calculated pills are dropped (no faithful model binding).
            ordered = _dedupe([f for f in (label, color, detail)
                               if f and f["kind"] == "category" and not f["is_calc"]])
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
                       + ([size] if size and size["kind"] == "value" else [])
                       + ([angle] if angle and angle["kind"] == "value" else []))
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
        # Shape map (choropleth): the geo-role dimension on Detail is the Category (location),
        # a single measure (prefer the colour saturation encoding, else any available) drives
        # the Value role (Power BI's shapeMap names its colour-saturation well "Value").
        loc = drop_calc_axis(_dedupe(
            [detail] if detail and detail["kind"] == "category" else []))
        meas = _dedupe(
            ([color] if color and color["kind"] == "value" else [])
            + values(rows) + values(cols)
            + ([size] if size and size["kind"] == "value" else [])
            + ([label] if label and label["kind"] == "value" else []))
        if loc:
            state["Category"] = {"projections": _role_projections(
                loc, model_table, field_map, used_refs)}
        if meas:
            state["Value"] = {"projections": _role_projections(
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
    if vt in (VT_COLUMN, VT_BAR, VT_LINE, VT_AREA, VT_PIE, VT_WATERFALL, VT_DONUT, VT_RIBBON):
        return "Category" in state and "Y" in state
    if vt == VT_COMBO:
        return "Category" in state and "Y" in state and "Y2" in state
    if vt == VT_SCATTER:
        return "X" in state and "Y" in state
    if vt == VT_CARD:
        return "Values" in state
    if vt == VT_FILLED_MAP:
        # A choropleth needs a Location (Category); the colour-saturation Value is optional --
        # a geo dimension on Detail with no measure is a valid location-only map (uniform fill).
        return "Category" in state
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
    # A colour DIMENSION on a bar/column mark stacks its segments within each bar by default in
    # Tableau ("Stack marks" is on by default). Power BI's clustered* charts render the same
    # legend side-by-side, so when a Series (legend) dimension is present the faithful default is
    # the stacked* variant -- preserving the Tableau layout rather than silently re-rendering a
    # stacked chart as grouped. (Default-stacking behaviour fact-checked against Tableau docs.)
    if vt in (VT_COLUMN, VT_BAR) and state.get("Series", {}).get("projections"):
        return "stackedColumnChart" if vt == VT_COLUMN else "stackedBarChart"
    return _VT_TO_PBIR[vt]


# -- Tier-2 image-oracle seam: per-visual candidate record -------------------------------------
# The deterministic Tier-1 engine commits to exactly ONE visual type per worksheet. For the later,
# agent-driven image-oracle pass, each emitted MAIN visual additionally records the small set of
# Tier-1 types the oracle is ALLOWED to switch to, a confidence in the deterministic pick, the
# read-only field truth (the oracle must NEVER rebind fields -- those are exact-bound to the model),
# the faithful position/z-order, and a hack flag for non-standard compositions. This is an ADDITIVE
# IR artifact (``ir["candidate_records"]``); it does not change the emitted PBIR parts at all.
def _orientation_flip(pbir_type):
    flips = {
        "clusteredColumnChart": "clusteredBarChart",
        "clusteredBarChart": "clusteredColumnChart",
        "stackedColumnChart": "stackedBarChart",
        "stackedBarChart": "stackedColumnChart",
    }
    return flips.get(pbir_type)


# vt -> (extra candidate PBIR types beyond chosen+orientation-flip, confidence, hack flag).
# "medium" marks a heuristic / hack reroute or a genuine visual look-alike an image can
# disambiguate; "high" marks a pick the shelf layout makes unambiguous. The applier may only ever
# switch a visual to a type that appears in its candidate list.
_CANDIDATE_ALTS = {
    VT_DONUT: (["pieChart"], "medium", "dual-axis pie/donut"),
    VT_PIE: (["donutChart"], "medium", None),
    VT_WATERFALL: (["clusteredColumnChart"], "medium", "running-total Gantt"),
    VT_RIBBON: (["clusteredColumnChart", "lineChart"], "medium", "bump/rank"),
    VT_COMBO: (["clusteredColumnChart", "lineChart"], "medium", "dual-axis combo"),
    VT_AREA: (["lineChart"], "medium", None),
    VT_LINE: (["areaChart"], "high", None),
    VT_FILLED_MAP: (["map"], "medium", None),
    VT_MAP: (["shapeMap"], "medium", None),
    VT_TABLE: (["pivotTable"], "medium", None),
    VT_MATRIX: (["tableEx"], "medium", None),
}


def _candidate_plan(vt, chosen_pbir):
    """(ranked candidate PBIR types [chosen first], confidence, hack flag) for a visual."""
    candidates = [chosen_pbir]
    flip = _orientation_flip(chosen_pbir)
    if flip:
        candidates.append(flip)
    extra, confidence, hack = _CANDIDATE_ALTS.get(vt, ([], "high", None))
    for c in extra:
        if c not in candidates:
            candidates.append(c)
    return candidates, confidence, hack


def _visual_field_summary(query_state):
    """``{role: [queryRef, ...]}`` of the EXACT-bound fields -- the oracle's read-only truth."""
    out = {}
    for role, role_obj in (query_state or {}).items():
        if isinstance(role_obj, dict):
            refs = [p.get("queryRef") for p in role_obj.get("projections", [])
                    if p.get("queryRef")]
            if refs:
                out[role] = refs
    return out


def _candidate_record(page_name, vname, ws, vtype, state, position, page_display=None):
    candidates, confidence, hack = _candidate_plan(ws["visual_type"], vtype)
    return {
        "page": page_name,
        "page_display": page_display or page_name,
        "visual": vname,
        "worksheet": ws["name"],
        "visual_type": vtype,
        "candidates": candidates,
        "confidence": confidence,
        "hack": hack,
        "fields": _visual_field_summary(state),
        "position": position,
    }


# -- PBIR JSON part assembly ---------------------------------------------------
def _sort_definition(ws, state, model_table, field_map):
    """Build a PBIR ``sortDefinition`` from a worksheet's ``<computed-sort>``.

    Power BI puts the sort on ``visual.query.sortDefinition`` (a sibling of ``queryState``) as an
    ordered ``sort`` array of ``{field, direction}`` (direction ``"Ascending"``/``"Descending"``),
    where ``field`` reuses the exact same expression shape as a projection. To stay
    warn-never-wrong we emit a sort ONLY when the sort-by field is already bound as a projection in
    this visual -- sorting by an unbound field would be a dangling reference. Returns ``None`` when
    there is no computed-sort or the sort-by field is not bound here.
    """
    sort = ws.get("sort")
    if not sort:
        return None
    expr, _, _ = _field_expression(sort["field"], model_table, field_map)
    bound = [p["field"]
             for role in state.values() if isinstance(role, dict)
             for p in role.get("projections", [])]
    if expr not in bound:
        return None
    return {"sort": [{"field": expr, "direction": sort["direction"]}],
            "isDefaultSort": False}


def _axis_objects(axis_titles):
    """Build the data-plane ``visual.objects`` categoryAxis/valueAxis entries for author-overridden
    axis titles. Each axis object is ``[{"properties": {...}}]`` (no ``selector`` needed for a
    global override). A blanked title (``hide``) emits ``showAxisTitle:false``; a custom caption
    emits ``titleText`` (single-quoted semantic-query literal) + ``showAxisTitle:true``. Shape
    verified against multiple real MS PBIR visual.json files + the PBIR enumerations reference.
    """
    objects = {}
    for axis in ("categoryAxis", "valueAxis"):
        spec = axis_titles.get(axis)
        if not spec:
            continue
        props = {}
        if spec.get("hide"):
            props["showAxisTitle"] = {"expr": {"Literal": {"Value": "false"}}}
        elif spec.get("text"):
            props["titleText"] = {
                "expr": {"Literal": {"Value": _semantic_string_literal(spec["text"])}}}
            props["showAxisTitle"] = {"expr": {"Literal": {"Value": "true"}}}
        if props:
            objects[axis] = [{"properties": props}]
    return objects


def _gradient_color_stops(cg):
    """Map a Tableau continuous palette to a PBIR ``linearGradient2`` / ``linearGradient3``.

    A diverging palette (a ``center`` value, >= 3 stops) becomes ``linearGradient3``: ``min`` =
    first colour, ``mid`` = the neutral middle colour pinned at the centre value, ``max`` = last
    colour. A sequential palette becomes ``linearGradient2`` (``min`` / ``max``). Tableau's author
    order (first -> min, last -> max) is preserved exactly. Colours are single-quoted semantic-query
    literals; the centre is a double literal. ``nullColoringStrategy`` defaults to ``asZero`` (the
    Power BI default), matching real formatted PBIR. Shape verified against a real MS-community
    ``tableEx`` gradient (min/mid/max with per-stop optional ``value``).
    """
    colors = cg["colors"]

    def _stop(hexv, value=None):
        stop = {"color": {"Literal": {"Value": _semantic_string_literal(hexv)}}}
        if value is not None:
            lit = _semantic_numeric_literal(str(value))
            if lit is not None:
                stop["value"] = {"Literal": {"Value": lit}}
        return stop

    nulls = {"strategy": {"Literal": {"Value": "'asZero'"}}}
    if cg.get("center") is not None and len(colors) >= 3:
        return {"linearGradient3": {
            "min": _stop(colors[0]),
            "mid": _stop(colors[len(colors) // 2], value=cg["center"]),
            "max": _stop(colors[-1]),
            "nullColoringStrategy": nulls}}
    return {"linearGradient2": {
        "min": _stop(colors[0]),
        "max": _stop(colors[-1]),
        "nullColoringStrategy": nulls}}


def _conditional_format(ws, state, model_table, field_map, warnings):
    """Table / matrix BACKGROUND colour scale (heat cells) -> (value_objects, fact).

    ``value_objects`` is the ``visual.objects.values`` entry list (a ``backColor`` FillRule
    gradient bound to the colour-driver measure) or ``None``; ``fact`` is an additive descriptor of
    the conditional format (``status`` ``emitted`` / ``deferred`` plus the raw palette) for the
    candidate record, or ``None`` when the worksheet has no continuous colour scale.

    WARN-NEVER-WRONG: the fill is emitted ONLY when the colour driver resolves to a clean model
    measure that is actually projected in THIS visual AND is not a quick table calc (whose derived
    quantity the model does not yet carry). Otherwise the visual emits with NO fill, a structured
    warning names the deferral, and the raw Tableau palette is preserved in ``fact`` so a later
    binding pass can light it up once the model build lands an equivalent measure. The FillRule's
    ``Input`` and the ``selector.metadata`` reuse the EXACT expression / queryRef already assigned
    to the visual's projections, so the fill never references something the query does not.
    """
    cg = ws.get("color_gradient")
    if not cg:
        return None, None
    color = ws["encodings"].get("color")
    fact = {
        "kind": "background_color_scale",
        "palette_type": cg["palette_type"],
        "center": cg["center"],
        "colors": cg["colors"],
    }

    values = (state.get("Values") or {}).get("projections", [])
    tooltips = (state.get("Tooltips") or {}).get("projections", [])

    def _match(field):
        if not field:
            return None
        expr, _, _ = _field_expression(field, model_table, field_map)
        # The colour driver may be surfaced on the matrix Tooltips (heat-grid "colour by a different
        # field") rather than as a visible Values column -- search both so the FillRule binds to the
        # exact projected queryRef wherever it lives.
        for p in values + tooltips:
            if p["field"] == expr:
                return p
        return None

    driver_proj = _match(color)
    # A quick table calc normally defers (the model carries no equivalent measure). But when the
    # colour pill was REBOUND to a real model measure via the model<->viz contract
    # (``measure_rebound``), it IS a bindable measure now -- so the table-calc gate is lifted and the
    # gradient lights up against the contracted measure.
    is_table_calc_defer = cg["is_table_calc"] and not (color or {}).get("measure_rebound")
    if (color is None or color["kind"] != "value"
            or color["binding"] not in ("aggregation", "measure")
            or is_table_calc_defer or driver_proj is None):
        reason = ("colour driver is a quick table calc -- no equivalent model measure yet"
                  if is_table_calc_defer
                  else "colour driver is not bound to a model measure in this visual")
        warnings.append(_warn(
            "worksheet", ws["name"],
            "background colour scale deferred ({0}); the visual is emitted without "
            "conditional formatting".format(reason)))
        fact["status"] = "deferred"
        fact["reason"] = reason
        return None, fact

    # Colour the displayed cell value: a distinct text/label measure when present (Tableau's "color
    # by a different field" pattern), else self-colour the driver measure itself.
    target_proj = _match(ws["encodings"].get("label")) or driver_proj
    value_objects = [{
        "properties": {
            "backColor": {"solid": {"color": {"expr": {"FillRule": {
                "Input": driver_proj["field"],
                "FillRule": _gradient_color_stops(cg)}}}}}},
        "selector": {
            "data": [{"dataViewWildcard": {"matchingOption": 1}}],
            "metadata": target_proj["queryRef"]},
    }]
    fact["status"] = "emitted"
    fact["bound_measure"] = driver_proj["queryRef"]
    fact["target"] = target_proj["queryRef"]
    return value_objects, fact


def _visual_json(name, vtype, position, query_state, sort_definition=None,
                 filter_config=None, title=None, axis_titles=None, value_objects=None):
    visual = {"visualType": vtype}
    if query_state:
        visual["query"] = {"queryState": query_state}
        if sort_definition:
            visual["query"]["sortDefinition"] = sort_definition
    visual["drillFilterOtherVisuals"] = True
    # Author-overridden axis-title captions (Tier-1 structural labels): the data-plane
    # ``visual.objects.categoryAxis`` / ``valueAxis`` entries. Shape verified against multiple real
    # MS PBIR visual.json files + the PBIR enumerations reference (``titleText`` = single-quoted
    # semantic-query literal; ``showAxisTitle`` = quoted boolean). Only the TITLE is touched -- the
    # whole-axis ``show`` toggle is deliberately left alone (a different property).
    if axis_titles:
        axis_objects = _axis_objects(axis_titles)
        if axis_objects:
            visual["objects"] = axis_objects
    # Background colour scale (Tier-2, lifted for tables/matrices): the data-plane
    # ``visual.objects.values`` entry carrying a ``backColor`` FillRule gradient. Shape verified
    # against a real MS-community formatted ``tableEx`` (``FillRule.Input`` measure +
    # ``linearGradient3`` min/mid/max; ``selector`` = dataViewWildcard + metadata queryRef).
    if value_objects:
        visual.setdefault("objects", {})["values"] = value_objects
    # Structural title text (Tier-1): the worksheet's authored caption -> the visual's container
    # title. Shape verified against the official PBIR visualContainer schema + real reports: a
    # single-quoted semantic-query string literal under visualContainerObjects.title; the
    # auto-generated field-name subtitle is suppressed so only the author's title shows. Font /
    # colour / size styling is deliberately omitted (Tier-2).
    if title:
        visual["visualContainerObjects"] = {
            "title": [{"properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "text": {"expr": {"Literal": {"Value": _semantic_string_literal(title)}}},
            }}],
            "subTitle": [{"properties": {
                "show": {"expr": {"Literal": {"Value": "false"}}},
            }}],
        }
    out = {
        "$schema": SCHEMA_VISUAL,
        "name": name,
        "position": position,
        "visual": visual,
    }
    # ``filterConfig`` is a TOP-LEVEL key on visual.json (sibling of ``visual``) -- verified
    # against real PBIR slicer files. On a slicer it carries the slicer's pre-selected members.
    if filter_config:
        out["filterConfig"] = filter_config
    return out


# -- applied filter selection -> slicer filterConfig ---------------------------
# When a Tableau worksheet filter narrows a field to specific members or a numeric range, carry
# that selection onto the rebuilt slicer so the report opens on the SAME filtered view. The PBIR
# JSON shapes below are verified against real Microsoft/community PBIR reports + the published
# semanticQuery schema (categorical ``In`` / ``Not`` ``In`` with ``isInvertedSelectionMode``;
# numeric ``Advanced`` ``Comparison``). Warn-never-wrong governs WHICH selections we emit (see
# ``_slicer_filter_config``): a wrong pre-filter would show wrong data, so anything we cannot bind
# faithfully (date-part members, the ``%null%`` sentinel, fixed date ranges) is left at "show all".
_FILTER_SOURCE_ALIAS = "f"


def _semantic_string_literal(value):
    """A Power BI semantic-query string literal: embedded single quotes, inner apostrophe doubled
    (``O'Brien`` -> ``'O''Brien'``)."""
    return "'" + str(value).replace("'", "''") + "'"


def _semantic_numeric_literal(value):
    """A semantic-query numeric literal (``24`` -> ``24L``, ``2.4`` -> ``2.4D``), or ``None`` when
    the token is not a clean number."""
    s = (value or "").strip()
    try:
        int(s)
        return s + "L"
    except (TypeError, ValueError):
        pass
    try:
        float(s)
        return s + "D"
    except (TypeError, ValueError):
        return None


def _filter_column_ref(entity, prop, *, source=None):
    src = {"Source": source} if source else {"Entity": entity}
    return {"Column": {"Expression": {"SourceRef": src}, "Property": prop}}


def _filter_container(entity, prop, condition, name, *, ftype, inverted=False):
    """One ``filterConfig.filters[]`` container (verified shape: ``name``/``field``/``type``/
    ``filter`` with ``Version:2``, a ``From[]`` source alias, and a single ``Where[].Condition``)."""
    container = {
        "name": name,
        "field": _filter_column_ref(entity, prop),
        "type": ftype,
        "filter": {
            "Version": 2,
            "From": [{"Name": _FILTER_SOURCE_ALIAS, "Entity": entity, "Type": 0}],
            "Where": [{"Condition": condition}],
        },
        "howCreated": "User",
    }
    if inverted:
        inverted_flag = {"expr": {"Literal": {"Value": "true"}}}
        container["objects"] = {
            "general": [{"properties": {"isInvertedSelectionMode": inverted_flag}}]}
    return container


def _categorical_condition(entity, prop, values, *, exclude):
    col = _filter_column_ref(entity, prop, source=_FILTER_SOURCE_ALIAS)
    in_expr = {"In": {
        "Expressions": [col],
        "Values": [[{"Literal": {"Value": _semantic_string_literal(v)}}] for v in values],
    }}
    return {"Not": {"Expression": in_expr}} if exclude else in_expr


def _range_condition(entity, prop, lo, hi):
    col = _filter_column_ref(entity, prop, source=_FILTER_SOURCE_ALIAS)

    def _cmp(kind, lit):
        # ComparisonKind 2 = GreaterThanOrEqual, 4 = LessThanOrEqual (inclusive bounds).
        return {"Comparison": {"ComparisonKind": kind, "Left": col,
                               "Right": {"Literal": {"Value": lit}}}}
    if lo is not None and hi is not None:
        return {"And": {"Left": _cmp(2, lo), "Right": _cmp(4, hi)}}
    return _cmp(2, lo) if lo is not None else _cmp(4, hi)


def _slicer_filter_config(field, model_table, field_map, name, warnings):
    """Build a slicer ``filterConfig`` from an applied Tableau filter selection/range, else ``None``.

    Warn-never-wrong: emit a pre-selection ONLY for shapes that bind faithfully AND whose PBIR JSON
    is verified against real reports -- a categorical include/exclude on a STRING dimension, or a
    numeric range. Date-part categoricals (e.g. month ``'4'`` / year ``'2026'``), the ``%null%``
    sentinel, and fixed date ranges fall through to the slicer's faithful "show all" default with a
    fidelity note (never a possibly-wrong pre-filter).
    """
    entity, prop, binding = _apply_override(field, model_table, field_map)
    if binding != "column":
        return None
    dt = (field.get("datatype") or "").lower()
    cap = field.get("caption") or prop
    sel, rng = field.get("selection"), field.get("range")
    if sel:
        if dt not in ("string", "boolean"):
            warnings.append(_warn(
                "filter", cap,
                "applied categorical selection left at default (date-part / numeric member "
                "values are not faithfully bindable to the raw column)"))
            return None
        values = [v for v in sel["values"] if v != "%null%"]
        if not values:
            warnings.append(_warn(
                "filter", cap,
                "applied selection reduced to null/sentinel members only; left at default"))
            return None
        cond = _categorical_condition(entity, prop, values,
                                      exclude=(sel["mode"] == "exclude"))
        return {"filters": [_filter_container(
            entity, prop, cond, name, ftype="Categorical",
            inverted=(sel["mode"] == "exclude"))]}
    if rng:
        if dt in _NUMERIC_TYPES:
            lo = (_semantic_numeric_literal(rng.get("min"))
                  if rng.get("min") is not None else None)
            hi = (_semantic_numeric_literal(rng.get("max"))
                  if rng.get("max") is not None else None)
            if lo is None and hi is None:
                return None
            cond = _range_condition(entity, prop, lo, hi)
            return {"filters": [_filter_container(
                entity, prop, cond, name, ftype="Advanced")]}
        warnings.append(_warn(
            "filter", cap,
            "applied date range left at default (date range filter shape deferred "
            "to a later pass)"))
        return None
    return None


def _slicer_json(name, field, position, model_table, field_map, *, warnings=None):
    expr, qref, nref = _field_expression(field, model_table, field_map)
    state = {"Values": {"projections": [
        {"field": expr, "queryRef": qref, "nativeQueryRef": nref}]}}
    fc = _slicer_filter_config(field, model_table, field_map, name + "-sel",
                               warnings if warnings is not None else [])
    return _visual_json(name, "slicer", position, state, filter_config=fc)


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
    records = []

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
            vtype = _pbir_vtype(ws["visual_type"], state)
            pos = _position(x, y, w, h, tab=i)
            value_objects, cf_fact = _conditional_format(
                ws, state, model_table, field_map, warnings)
            visuals.append(_visual_json(
                vname, vtype, pos, state,
                _sort_definition(ws, state, model_table, field_map),
                title=ws.get("title"), axis_titles=ws.get("axis_titles"),
                value_objects=value_objects))
            rec = _candidate_record(page_name, vname, ws, vtype, state, pos,
                                    page_display=db["name"] or page_name)
            if cf_fact:
                rec["conditional_format"] = cf_fact
            records.append(rec)
        visuals += _emit_slicers(page_ws, page_name, model_table, field_map, warnings)
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
        vname = _sanitize("v-" + ws["name"])
        vtype = _pbir_vtype(ws["visual_type"], state)
        pos = _position(40, 40, 880, 620)
        value_objects, cf_fact = _conditional_format(
            ws, state, model_table, field_map, warnings)
        main = _visual_json(
            vname, vtype, pos, state,
            _sort_definition(ws, state, model_table, field_map),
            title=ws.get("title"), axis_titles=ws.get("axis_titles"),
            value_objects=value_objects)
        rec = _candidate_record(page_name, vname, ws, vtype, state, pos,
                                page_display=ws["name"])
        if cf_fact:
            rec["conditional_format"] = cf_fact
        records.append(rec)
        visuals = [main] + _emit_slicers([ws], page_name, model_table, field_map, warnings)
        _emit_page(parts, page_name, ws["name"], visuals)
        page_order.append(page_name)

    parts["definition/pages/pages.json"] = _dumps({
        "$schema": SCHEMA_PAGES,
        "pageOrder": page_order,
        "activePageName": page_order[0] if page_order else "",
    })

    ir.setdefault("warnings", []).extend(warnings)
    ir["candidate_records"] = records
    return parts


def _emit_slicers(ws_list, page_name, model_table, field_map, warnings=None):
    visuals = []
    fields = _filter_slicer_fields(ws_list)
    for i, f in enumerate(fields):
        y = 40 + i * 120
        if y > PAGE_HEIGHT - 120:
            break
        vname = _sanitize(f"slicer-{page_name}-{i}-{f['property']}")
        visuals.append(_slicer_json(
            vname, f, _position(PAGE_WIDTH - 220, y, 200, 100, z=1, tab=100 + i),
            model_table, field_map, warnings=warnings))
    return visuals


def migrate_twb_to_pbir(xml_text, *, dataset_name="Model", report_name="Report",
                        model_table=None, field_map=None, date_binding=None,
                        row_count_binding=None, measure_binding=None):
    """One-call convenience: parse ``.twb`` text and emit the PBIR parts.

    Returns ``{"ir": ..., "parts": ..., "warnings": ...}``. ``parts`` is the
    ``{relative_path: text}`` PBIR definition; write it to a ``<report_name>.Report`` folder
    or base64-encode each part for the Fabric report *Update Definition* API.

    ``date_binding`` (optional) carries the model build's date facts -- ``date_table`` (the marked
    calendar table name), ``active_keys`` (the fact date column(s) the calendar relates to ACTIVELY,
    any spelling), ``grain_columns`` (Tableau date-part -> calendar column; defaults to the standard
    calendar columns) and ``key_column`` (the calendar key, default ``"Date"``). When given, a date
    axis pill on the active business date is rebound to the shared Date table so time intelligence
    runs through the calendar; without it the standalone path is unchanged.

    ``row_count_binding`` (optional) carries the model build's row-count (COUNTROWS) measures --
    ``{"measures": {<table name>: {"entity": ..., "measure": ...}}, "default": {"entity": ...,
    "measure": ...}}``. When given, an implicit row count (object-id ``COUNT(*)`` or legacy
    ``[Number of Records]``) binds to the matching COUNTROWS measure; without it the count is left
    unbound with a precise warning (warn-never-wrong), never a dangling/guessed binding.

    ``measure_binding`` (optional) carries the model build's calc->measure manifest (the locked
    model<->viz contract) -- a token-keyed ``{<calc token>: {"entity": "_Measures", "measure":
    <name>, "status": <translated|assisted-approved|...>}}`` map (a ``{"measures": {...}}`` wrapper
    is also accepted). When given, each workbook-local calc / quick-table-calc pill the model build
    translated is rebound to its named measure (deterministic, token-keyed; binds only for
    translated / assisted-approved measures) -- so a calc-driven value, a background colour-scale
    driver, etc. references the real measure. Without it, those pills degrade-and-warn unchanged.
    """
    ir = parse_twb(xml_text, date_binding=date_binding, row_count_binding=row_count_binding,
                   measure_binding=measure_binding)
    parts = emit_pbir(ir, dataset_name=dataset_name, report_name=report_name,
                      model_table=model_table, field_map=field_map)
    return {"ir": ir, "parts": parts, "warnings": ir["warnings"],
            "candidate_records": ir.get("candidate_records", [])}


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
