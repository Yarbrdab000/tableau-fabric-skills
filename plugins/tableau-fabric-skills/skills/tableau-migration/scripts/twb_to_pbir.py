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

# View-only Quick Table Calc -> Power BI Visual Calculation (additive; report-layer counterpart to
# the model measure path). These three cooperate: ``extract_table_calc_usages`` recovers the quick
# pill's addressing facts, ``usage_to_visual_calc_spec`` normalizes them to a view-layer IR, and
# ``emit_visual_calc`` renders the Visual-Calculation DAX. The wiring below projects the base measure
# + the VC into the visual's ``queryState`` (see ``_apply_visual_calcs``). Import is optional-safe so
# a partial checkout still emits everything else.
try:
    from .workbook_table_calcs import extract_table_calc_usages
    from .visual_calc_spec import usage_to_visual_calc_spec
    from .visual_calc_emitter import emit_visual_calc
except ImportError:  # pragma: no cover - flat scripts-on-path
    try:
        from workbook_table_calcs import extract_table_calc_usages
        from visual_calc_spec import usage_to_visual_calc_spec
        from visual_calc_emitter import emit_visual_calc
    except ImportError:
        extract_table_calc_usages = None
        usage_to_visual_calc_spec = None
        emit_visual_calc = None

# Report-layer formatting emit builders (additive; PBIR analytics/format objects grounded on the
# Power BI formatting inventory). Optional-safe so a partial checkout still emits everything else.
try:
    from . import report_formatting
except ImportError:  # pragma: no cover - flat scripts-on-path
    try:
        import report_formatting
    except ImportError:
        report_formatting = None


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

# Small-multiples (trellis) visual schema + formatting. The original 1.0.0 visualContainer schema
# predates the small-multiples feature, so Power BI Desktop silently DROPS a small-multiples query
# role on a 1.0.0 visual (the chart renders as a single aggregated panel). A cartesian visual that
# panes by a dimension binds the single-name ``SmallMultiple`` query role AND carries a
# ``smallMultiple`` formatting card (layoutMode / maxItemsPerRow / showEmptyItems) so Desktop lays
# the panes out -- without that card the role binds but no trellis renders. Such a visual is stamped
# at the newer trellis-capable schema; the bump is gated to ONLY the visuals that emit a
# SmallMultiple role, so the already-verified non-trellis gates (KPI / bar / map) keep their proven
# 1.0.0 stamp.
SCHEMA_VISUAL_SM = f"{_S}/definition/visualContainer/2.7.0/schema.json"

MEASURES_TABLE = "_Measures"
PAGE_WIDTH = 1280
PAGE_HEIGHT = 720
# Tableau's own default dashboard canvas (Desktop "Fixed size" default) -- used only as the fallback
# when a dashboard declares no fixed <size> (e.g. an automatic/range-sized dashboard).
DASH_DEFAULT_W = 1000
DASH_DEFAULT_H = 800

# Per-dashboard page dimensions. A Tableau dashboard declares its own fixed pixel canvas via
# <size maxwidth= maxheight=> (e.g. 1400x1000). We emit the PBIR page at those exact pixel dimensions
# and scale the zone coordinates straight into it -- so a 1400x1000 dashboard becomes a 1400x1000 page,
# a 1000x1000 one becomes 1000x1000, etc. (NOT a uniform 1280 width). Tableau normalizes every
# dashboard's zone coordinates to 100000x100000 PER AXIS regardless of the real pixel aspect, so the
# real aspect lives ONLY in <size>; mapping the normalized zone rect into the real <size> page (with
# independent sx = page_w/extent_w, sy = page_h/extent_h) de-normalizes it back to faithful pixels.
# Both overrides are set per dashboard and reset to None afterwards so standalone worksheet pages stay
# the default 1280x720 (never-regress).
_PAGE_W_OVERRIDE = None
_PAGE_H_OVERRIDE = None
# A Power BI slicer fills its whole rectangle, unlike a Tableau filter *card*, which renders its
# control inset inside the zone with padding. Tableau packs filter zones edge-to-edge (tangent in
# BOTH axes) and relies on that per-card padding for the visible gaps, so emitting a slicer at the
# raw scaled zone makes neighbours collide. We therefore lay each slicer out as a fixed-height
# control inset in its zone: a uniform SLICER_CTRL_H tall, horizontally padded by SLICER_PAD_X, and
# vertically centered inside its (taller) zone -- which reproduces Tableau's inter-card gaps. A zone
# shorter than the control grows to it and pushes the rows below down (plus SLICER_ROW_GUTTER) so a
# cramped band never overlaps. The control height is sized for the SLICER_FONT_PT text below: a 9pt
# header + dropdown fits ~40px, versus the ~52px the oversized Power BI default (~12pt) forced. The
# font itself is the other half of the fix -- a Power BI slicer defaults to a larger face than
# Tableau's compact ~9pt filter card, which inflates every card's minimum footprint; stamping the
# source point size (here 9pt) both matches Tableau and lets the box shrink.
SLICER_CTRL_H = 40.0
# A DROPDOWN-mode slicer's height is the real scaled Tableau card height, translated DIRECTLY (no
# chrome pad added) -- the emitted box tracks the SOURCE card number-for-number, per the user. A small
# absolute floor (SLICER_DROPDOWN_MIN_H) guarantees a degenerate tiny card still renders its control:
# Power BI clips a dropdown below ~40px (only the field name shows), so the floor keeps it usable
# without inflating a card that is already tall enough.
SLICER_DROPDOWN_MIN_H = 64.0
SLICER_PAD_X = 7.0
SLICER_ROW_GUTTER = 8.0
SLICER_FONT_PT = 9.0


def _page_w():
    """Active page width: the per-dashboard real <size> width override, else the default."""
    return _PAGE_W_OVERRIDE or PAGE_WIDTH


def _page_h():
    """Active page height: the per-dashboard aspect-faithful override, else the default."""
    return _PAGE_H_OVERRIDE or PAGE_HEIGHT

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
VT_FILLED_MAP = "filled_map"  # filledMap (Bing choropleth: geo Category + saturation measure on the Gradient/Color-saturation well)
VT_MAP = "map"            # map (symbol/bubble: geo Location + measure Size/Color)
VT_SHAPE_MAP = "shape_map"  # shapeMap (built-in-topology choropleth: geo Category + measure on the "Value" well)
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
    # A Tableau filled map shaded by a MEASURE migrates to shapeMap -- a built-in-topology choropleth
    # that geocodes the Location dimension and shades each area by the measure on the "Value" well.
    # The "shared" usa.states.topo map (PackageType 2) is a Power-BI-provided resource, so a US-state
    # choropleth renders OFFLINE with no bundled TopoJSON (the "Value" role name + the shape object
    # are verified against a real Desktop-authored shapeMap visual.json). Microsoft deprecates the
    # legacy Bing filledMap; it is retained only for location-only / categorical-legend maps (a
    # measure-less geo Detail) -- shapes shapeMap cannot express -- and stays an image-oracle
    # candidate the assisted tier may restore.
    VT_FILLED_MAP: "filledMap",
    VT_MAP: "map",
    VT_SHAPE_MAP: "shapeMap",
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

# Tableau DISCRETE "exact date" derivations: a date pill shown as the literal DATE VALUE at a grain
# (NOT a numeric part like Year/Month). The code is the retained-field prefix of the canonical
# Year->Month->Day->Hour->Minute->Second sequence, so "MDY" = Month/Day/Year = the exact date at DAY
# grain (Tableau's "Month, Day, Year" display) and the longer codes just add a finer time grain. This
# is an ORDINARY date column -- the same underlying date as a continuous exact-date pill, only
# rendered as discrete members -- so a filter / axis on it is faithfully a normal date slicer / axis,
# and a display-format choice like MDY must never drop the field. (Day-grain-or-finer loses no grain;
# a coarser year/month exact date stays fail-closed -> honest warn+skip until verified against a real
# artifact.) Verified against the real ATTI/ATTR Hierarchy workbook, whose FiscalMonth filter card
# carries derivation "MDY" and is otherwise an ordinary date column.
_DATE_EXACT_DERIVATIONS = frozenset({"MDY", "MDYH", "MDYHM", "MDYHMS"})

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

# The model build's marked Date table also carries a single drill hierarchy named "Calendar"
# (Year -> Quarter -> Month -> Week -> Day) -- see tmdl_generate.generate_date_table_tmdl. A
# CONTINUOUS Tableau date truncation (a green ``t*:`` pill, e.g. DATETRUNC('month')) is a
# display-grain axis, so the faithful Power BI placement is that calendar hierarchy drilled to the
# truncation grain -- NOT the flat day-grain key column (which renders an undrillable continuous
# axis the user must then rewire by hand). The level path is Year-rooted; the Month case is verified
# against a Desktop-authored areaChart whose date axis is exactly Year + Month (Quarter is omitted).
# This layer only references the hierarchy the model already owns; it never builds it.
_DEFAULT_DATE_HIERARCHY = "Calendar"
_DATE_TRUNC_HIERARCHY_LEVELS = {
    "Year": ("Year",),
    "Quarter": ("Year", "Quarter"),
    "Month": ("Year", "Month"),
    "Week": ("Year", "Month", "Week"),
    "Day": ("Year", "Month", "Day"),
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
    exact/continuous date, OR a discrete exact-date VALUE (e.g. MDY -- the full date shown as "Month,
    Day, Year"), rebinds to the marked key column (Date[Date]); a day-or-coarser CONTINUOUS
    truncation (Day/Week/Month/Quarter/Year-Trunc, the green ``t*:`` pills) rebinds to the marked Date
    table's Calendar drill hierarchy, drilled to the truncation grain (Month-Trunc -> Year + Month) --
    this is what a Desktop-authored rebuild does (its area/line date axis carries the Calendar levels,
    not a flat date column). A SUB-DAY truncation (Hour/Minute/Second-Trunc) can't be represented by a
    day-grain calendar, and any part with no calendar column, return ``None`` (deferred -- the caller
    keeps the source column + warns). Returns a rebind dict -- ``{"entity","property"}`` for a column
    or ``{"entity","hierarchy","levels"}`` for the drill hierarchy -- else ``None``.
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
        return {"entity": table, "property": col} if col else None
    if deriv in ("None", "", None) or deriv in _DATE_EXACT_DERIVATIONS:
        # plain / continuous exact date, or a discrete exact-date VALUE (e.g. MDY = the full date
        # shown as "Month, Day, Year") -> the marked calendar key column. Both are the same
        # underlying date, so the exact-date-value display format binds exactly like a plain date.
        return {"entity": table, "property": date_binding.get("key_column") or "Date"}
    # A continuous DAY-or-coarser truncation (Day/Week/Month/Quarter/Year-Trunc, the green `t*:`
    # pills) on the active business date is a display-grain axis -> the marked Date table's Calendar
    # drill hierarchy, drilled to the truncation grain (Month-Trunc -> Year + Month). This is what a
    # Desktop-authored rebuild does: its area/line date axis carries the Calendar levels, not a flat
    # date column. A SUB-DAY truncation (Hour/Minute/Second-Trunc) has no day-grain calendar level,
    # so it stays deferred (caller keeps the source column + warns; warn-never-wrong).
    m = re.match(r"(Year|Quarter|Month|Week|Day)-Trunc$", str(deriv or ""))
    if m:
        levels = _DATE_TRUNC_HIERARCHY_LEVELS.get(m.group(1))
        if levels:
            return {"entity": table,
                    "hierarchy": date_binding.get("date_hierarchy") or _DEFAULT_DATE_HIERARCHY,
                    "levels": list(levels)}
        return {"entity": table, "property": date_binding.get("key_column") or "Date"}
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


# Coarse -> fine geographic granularity. When several geo levels sit on Detail (e.g. Country AND
# State, as Tableau serialises a drill hierarchy), the map is rendered at the FINEST level present:
# each state is its own filled mark and the coarser level is only its drill-up parent. The faithful
# Power BI Location is therefore the finest geo dimension, not the first/coarsest one. Keys are the
# area token _geo_area() yields from the Tableau semantic-role (e.g. "[State].[Name]" -> "State"),
# lower-cased; higher rank = finer.
_GEO_GRANULARITY = {
    "country": 1, "country/region": 1, "region": 1,
    "area code": 2,
    "state": 3, "state/province": 3, "province": 3,
    "county": 4, "cbsa": 4, "msa": 4, "congressional district": 4,
    "city": 5,
    "zip code": 6, "zipcode": 6, "postal code": 6, "postcode": 6,
}


def _geo_rank(area):
    """Granularity rank for a geographic area name (higher = finer); 0 if unknown."""
    return _GEO_GRANULARITY.get((area or "").strip().lower(), 0)


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
    """A deterministic, COMPACT PBIR object name: word chars / hyphen only.

    Uniqueness is carried entirely by the 8-char md5 of the FULL input text, so the
    human-readable prefix is deliberately short (<= 16 chars). This keeps the nested
    ``.Report/definition/pages/<page>/visuals/<visual>/visual.json`` paths well under the
    Windows MAX_PATH (260) limit -- two names that share a 16-char prefix (e.g. a visual
    name that redundantly embeds its already-hashed page slug) still differ by hash, so the
    shorter prefix costs no uniqueness. Max length is 16 + 8 = 24 chars.
    """
    base = re.sub(r"[^0-9A-Za-z_-]+", "", (text or "").replace(" ", ""))
    h = hashlib.md5((text or "").encode("utf-8")).hexdigest()[:8]
    name = (base[:16] + h) if base else ("v" + h)
    return name[:24]


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


# Tableau stamps a join/relationship *order* prefix onto a physical table name -- the second table
# added to a join surfaces as ``1. LoginHistory`` -- while the migrated model declares the clean
# table (``LoginHistory``). ``<digits>. `` + whitespace is that order prefix (a real table named in
# the exact ``<digits>. <name>`` shape does not occur in practice); stripping it lets an implicit
# object-id COUNT bind to its COUNTROWS measure across the rename. The trailing ``\s+`` is required
# so a name like ``2024.Q1`` (dot, no space) is left untouched.
_TABLE_ORDER_PREFIX_RE = re.compile(r"^\d+\.\s+")


def _strip_table_order_prefix(name):
    """Drop a leading Tableau join-order prefix (``"1. LoginHistory"`` -> ``"LoginHistory"``)."""
    return _TABLE_ORDER_PREFIX_RE.sub("", name or "").strip()


def _match_row_count_measure(table, measures):
    """Find ``table``'s row-count measure in ``measures``, tolerating a Tableau join-order prefix on
    either side (``"1. LoginHistory"`` vs ``"LoginHistory"``). Exact match wins; an
    order-prefix-normalised match binds ONLY when it is unambiguous (exactly one candidate), so two
    prefixed instances of the same physical table stay unbound and are warned -- warn-never-wrong.
    """
    if not table or not measures:
        return None
    if table in measures:
        return measures[table] or None
    norm = _strip_table_order_prefix(table)
    if not norm:
        return None
    cands = [v for k, v in measures.items() if _strip_table_order_prefix(k) == norm]
    return cands[0] if len(cands) == 1 else None


def _row_count_measure_target(rc, row_count_binding):
    """Resolve the ``(entity, measure)`` to bind an implicit row count to, or ``None``.

    ``row_count_binding`` is this layer's own (consumer-owned) shape:
    ``{"measures": {<table name>: {"entity": ..., "measure": ...}}, "default": {"entity": ...,
    "measure": ...}}``. An ``object_id`` count binds only when its specific table has a measure
    (never via ``default`` -- it names a fact, so binding requires that fact's COUNTROWS measure); a
    ``numrec`` count (the legacy single-fact row count) binds via ``default``.

    The ``object_id`` table match tolerates a Tableau join-order prefix (``"1. LoginHistory"``) on
    either side via :func:`_match_row_count_measure`, so a KPI card counting a prefixed physical
    table still binds to its clean COUNTROWS measure instead of silently blanking.
    """
    if not row_count_binding:
        return None
    measures = row_count_binding.get("measures") or {}
    if rc["kind"] == "object_id":
        m = _match_row_count_measure(rc.get("table"), measures) or {}
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


def _column_binding_entries(column_binding):
    """Normalise the consumer-owned ``column_binding`` into a flat ``{name_lower: entry}`` map.

    Accepts a flat ``{key: entry}`` dict or a ``{"columns": {key: entry}}`` wrapper (mirroring
    ``measure_binding``). Each entry names the REAL model ``table`` + ``column`` a Tableau calc
    *dimension* was materialised into -- read back from the built model's TMDL by the estate
    orchestrator (Fix 2). Pure consumer: this layer never invents a binding, it only echoes a
    model-confirmed one (an entry missing either half is dropped).
    """
    if not isinstance(column_binding, dict) or not column_binding:
        return {}
    inner = column_binding.get("columns")
    src = inner if isinstance(inner, dict) else column_binding
    out = {}
    for k, entry in src.items():
        if not isinstance(k, str) or not isinstance(entry, dict):
            continue
        table = entry.get("table") or entry.get("entity") or entry.get("model_table")
        column = entry.get("column") or entry.get("property")
        if table and column:
            out[k.lower()] = {"table": table, "column": column}
    return out


def _lookup_column_binding(column_binding, field_id, base_id, caption, worksheet):
    """Resolve a calc DIMENSION pill to its model ``(table, column)``, or ``None``.

    Tries candidate keys case-insensitively in a deterministic order (caption, trimmed caption,
    bare calc id, pill instance token) and returns the first model-confirmed hit; a miss (or no
    binding supplied) -> ``None`` so the caller degrades to the caption fallback + warns. A pure
    consumer of the model-built manifest -- never a fuzzy/guessed match.
    """
    entries = _column_binding_entries(column_binding)
    if not entries:
        return None
    for key in (caption, (caption or "").strip(), base_id, field_id):
        if not key:
            continue
        hit = entries.get(str(key).lower())
        if hit:
            return (hit["table"], hit["column"])
    return None


def _resolve_field(ds, field_id, base_cols, instances, index, ds_caption,
                   worksheet, warnings, warn_special=True, internal_fields=None,
                   date_binding=None, row_count_binding=None, measure_binding=None,
                   column_binding=None):
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

    # A calculated field used as a DIMENSION on an axis (a discrete pill, not an aggregation) is a
    # category column in the rebuilt model -- NOT a _Measures value. Detect that case so it binds to
    # the real model column and lands in the CATEGORY well, instead of being forced into the measure
    # well where _visual_type sees zero dimensions and collapses a crosstab of calc dimensions into a
    # single card. ``column_binding`` (Fix 2, model-confirmed) supplies the exact (table, column) the
    # calc was materialised into; without it the pill degrades to the caption fallback below (still a
    # category), never a measure.
    calc_is_axis = (is_calc and bound is None and role != "measure"
                    and deriv not in _AGG_FUNC)
    calc_col = (_lookup_column_binding(column_binding, field_id, base_id, caption, worksheet)
                if calc_is_axis else None)

    if bound:
        entity, prop = bound["entity"], bound["property"]
        if not datatype:
            datatype = bound["datatype"]
    elif calc_col is not None:
        entity, prop = calc_col
    elif is_calc and not calc_is_axis:
        entity, prop = MEASURES_TABLE, caption
    else:
        # A plain field with no datasource metadata, OR a calc dimension with no model-confirmed
        # column: bind by caption fallback and warn. A calc's model column name is the trimmed
        # caption (the model build trims it); a raw field uses clean_col.
        entity = ds_caption.get(ds, ds)
        prop = (caption or "").strip() if calc_is_axis else clean_col(caption)
        _wcf = _warn(
            "worksheet", worksheet,
            f"field '{caption}' bound by caption fallback (no datasource metadata); "
            f"verify it matches model table/column names")
        _wcf["caption_fallback"] = caption
        warnings.append(_wcf)

    field = {
        "caption": caption, "field_id": base_id, "instance": field_id,
        "role": role, "datatype": datatype, "is_calc": is_calc,
        "derivation": deriv, "aggregation": None,
        "entity": entity, "property": prop,
        "binding": None, "kind": None,
        "geo_area": _geo_area(base.get("geo_role", "")) if role != "measure" else None,
        "formula": base.get("formula"),
    }

    # A model-confirmed calc-DIMENSION binding (from the ``column_binding`` manifest) is AUTHORITATIVE
    # -- exactly like a date rebind, neither ``field_map`` nor the ``model_table`` fallback in
    # ``_apply_override`` may pull it back onto the fact table. Without this stamp a field-parameter axis
    # (materialised into its OWN ``calculated`` table, e.g. ``'Choose Date'[Choose Date]``) or any calc
    # dimension living outside the fact would be re-pinned to ``model_table`` and dangle as
    # ``Sheet1[<calc>]``. Fail-closed: a field with no manifest hit (``calc_col is None``) is never
    # stamped, so ``_apply_override`` behaves byte-for-byte as before.
    if calc_col is not None:
        field["column_rebound"] = True

    # A measure calc with no model binding lands in the value well; a calc DIMENSION (calc_is_axis)
    # is NOT stamped here -- it falls through to the plain-field path below so it binds as a category
    # column (binding="column", kind="category"), which is what lets a calc-dimension crosstab keep
    # its axes and rebuild as a matrix.
    if is_calc and bound is None and not calc_is_axis:
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
        field["entity"] = rebind["entity"]
        if "hierarchy" in rebind:
            field["hierarchy"] = {"name": rebind["hierarchy"], "levels": rebind["levels"]}
        else:
            field["property"] = rebind["property"]
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

    if deriv in _DATE_EXACT_DERIVATIONS:
        # Discrete exact-date VALUE (e.g. MDY = the full date shown as "Month, Day, Year"). This is
        # just a display format on an ordinary date column -- the same underlying date as a plain
        # date pill -- so bind it as a normal date column (a date slicer/axis), never drop it.
        field["binding"] = "column"
        field["kind"] = "value" if role == "measure" else "category"
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
                   date_binding=None, row_count_binding=None, measure_binding=None,
                   column_binding=None):
    fields = []
    for tok in _TOKEN_RE.findall(text or ""):
        ds, fid = _split_token(tok)
        f = _resolve_field(ds or ds_default, fid, base_cols, instances, index,
                           ds_caption, worksheet, warnings, warn_special=warn_special,
                           internal_fields=internal_fields, date_binding=date_binding,
                           row_count_binding=row_count_binding, measure_binding=measure_binding,
                           column_binding=column_binding)
        if f:
            fields.append(f)
    return fields


def _parse_encodings(pane, ds_default, base_cols, instances, index, ds_caption,
                     worksheet, warnings, warn_special=True, internal_fields=None,
                     date_binding=None, row_count_binding=None, measure_binding=None,
                     column_binding=None):
    enc = {"color": None, "size": None, "label": None, "detail": None, "angle": None,
           "geo_levels": []}
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
                           row_count_binding=row_count_binding, measure_binding=measure_binding,
                           column_binding=column_binding)
        if f:
            if enc[role] is None:
                enc[role] = f
            # Retain ALL geo-role Detail pills (not just the first) so a multi-level map binds its
            # Location to the FINEST geography present, not whichever level Tableau serialised first.
            if role == "detail" and f.get("geo_area"):
                enc["geo_levels"].append(f)
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
        # A geo Location + a measure is a choropleth shaded by that measure -> shapeMap (the faithful
        # successor to a Tableau filled map; Microsoft deprecates the legacy Bing filledMap). An
        # explicit filled/map mark is self-signaling; an automatic mark needs a spatial signal
        # (generated lat/lon) so an ordinary chart with a geo dimension is not hijacked into a map.
        if m in ("map", "filled", "filledmap"):
            return VT_SHAPE_MAP
        if m in ("circle", "square", "shape", "point") and map_signal:
            return VT_MAP
        if m in ("automatic", "") and map_signal:
            return VT_SHAPE_MAP
        # geo on Detail but no confirming spatial signal -> fall through to chart heuristics

    # Location-only map: a geo-role dimension on Detail with NO measure anywhere and no axis
    # pills is Tableau's default rendering of that geography (auto-generated lat/lon, uniform
    # fill) -- there is no other faithful reading (no measure for a chart, and a geographic field
    # is a map, not a text list). The faithful rebuild is a filledMap carrying just the Location
    # (Category); the colour-saturation measure is simply absent. Custom-geometry marks still defer.
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
        # A slicer binds a raw column. An aggregate (SUM(Sales)) or a measure-role /
        # parameter-comparing calc has no faithful slicer mapping -> warn instead of
        # emitting a wrong slicer. A row-level DIMENSION calc (an IF/CASE bucket like
        # "Job Type ") lands as a real sliceable model column, so it IS kept as a slicer;
        # only calcs that (a) roll up to a measure or (b) compare against a parameter
        # (whose value isn't a column the slicer can bind) stay warned-and-dropped.
        _calc_formula = (base_cols.get((ds or ds_default, f["field_id"])) or {}).get("formula") or ""
        _calc_unsliceable = f["is_calc"] and (
            f["role"] == "measure" or "[Parameters]" in _calc_formula)
        if f["binding"] == "aggregation" or _calc_unsliceable:
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
        # The raw ``[datasource].[field-instance]`` token (pre-resolution) lets the slicer gate match
        # this filter against the dashboard filter cards the author actually exposed -- the same token
        # a dashboard ``<zone type-v2='filter' param='...'>`` carries -- so an applied-but-unshown
        # filter never fabricates a control.
        f["filter_token"] = (ds, fid)
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

    # Default: the member measures as a table / matrix / card band. WITH a real dimension Power BI
    # renders measures-as-columns natively in a matrix (pivotTable). With NO dimension the faithful
    # rebuild splits on the shelf ORIENTATION of the Measure Names / Measure Values placeholders:
    #   * VERTICAL -- Measure Values (or Measure Names) on ROWS is a Tableau "measure table": the
    #     measure names listed down the side with their values beside them -> a faithful tableEx text
    #     table (one measure per row).
    #   * HORIZONTAL -- Measure Names on Columns with the values shown as Text marks (a measure-names
    #     BAN band: each measure is its own labelled big number across a strip) -> a multiRowCard
    #     (VT_CARD), Power BI's native row of labelled big numbers, NOT a single-column text table.
    # Either way the implicit Measure Names pill stays unbound -- Power BI's labels ARE the measure
    # names; the member measures fill the value well.
    if dims_rows or dims_cols:
        vt = VT_MATRIX
    elif names_at == "rows" or values_at == "rows":
        vt = VT_TABLE
    else:
        vt = VT_CARD
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


# Per-run font attributes on a title's ``<run>`` that Tier-2 title styling reproduces only when it
# can do so faithfully. ``bold`` and ``fontname`` (font family) are emitted when uniform (family
# only for a REAL font -- Tableau's internal 'Tableau Bold' / 'Tableau Semibold' etc. have no Power
# BI equivalent, so they defer); ``italic`` / ``underline`` (unconfirmed container-title props) and
# ``fontalignment`` (unconfirmed alignment enum -> a wrong guess would mis-align the title) are
# ALWAYS deferred. Deferred attributes are recorded for a future pass, never emitted.
_TITLE_ALWAYS_DEFER_ATTRS = ("italic", "underline", "fontalignment")
_TITLE_INTERNAL_FONT_RE = re.compile(r"^Tableau\b", re.IGNORECASE)
_HEX6_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _font_size_points(value):
    """A Tableau ``fontsize`` (points) -> a Power BI font-size literal (``'15'`` -> ``'15D'``).

    Power BI font sizes are doubles in points -- the same unit Tableau uses -- so the value passes
    through unchanged with a ``D`` suffix. Returns ``None`` for a non-positive / non-numeric size.
    """
    s = (value or "").strip()
    try:
        n = float(s)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return "{0}D".format(int(n) if n == int(n) else n)


def _parse_title_style(ws):
    """Uniform font styling for a worksheet's static title -> a Tier-2 title-style dict.

    Reads the per-run font attributes on the title's ``<run>`` elements (the styling that
    ``_parse_worksheet_title`` discards) and keeps the schema-grounded container-title font
    properties that can be reproduced faithfully: ``font_size`` (points), ``font_color``
    (``#rrggbb``), ``bold`` (weight), and ``font_family`` (a real, non-Tableau-internal font).
    Power BI applies ONE font to the whole title, so a property is emitted only when EVERY
    text-bearing run agrees; a title whose runs disagree -- or only partially declare a property --
    cannot be reproduced faithfully, so that property is deferred (warn-never-wrong). Italic /
    underline / alignment and Tableau-internal font families are always deferred. Returns the style
    dict (with an additive ``deferred`` list of property names seen but not emitted), or ``None``
    when the title carries no font styling at all.
    """
    layout = _first(ws, "layout-options")
    title = _first(layout, "title") if layout is not None else None
    ft = _first(title, "formatted-text") if title is not None else None
    if ft is None:
        return None
    runs = _findall_local(ft, "run")
    text_runs = [r for r in runs if (r.text or "").strip()]
    if not text_runs:
        return None

    def _uniform(attr):
        vals = [r.get(attr) for r in text_runs]
        if all(v is not None for v in vals) and len(set(vals)) == 1:
            return vals[0]
        return None

    style = {}
    deferred = []

    size_lit = _font_size_points(_uniform("fontsize"))
    if size_lit is not None:
        style["font_size"] = size_lit
    elif any(r.get("fontsize") for r in text_runs):
        deferred.append("fontsize")

    color = _uniform("fontcolor")
    if color is not None and _HEX6_RE.match(color):
        style["font_color"] = color
    elif any(r.get("fontcolor") for r in text_runs):
        deferred.append("fontcolor")

    # Bold weight: emit only when EVERY text-bearing run is bold; a title with mixed weight cannot
    # be reproduced by Power BI's single-font title, so defer.
    bold_runs = [r for r in text_runs if r.get("bold") == "true"]
    if bold_runs:
        if len(bold_runs) == len(text_runs):
            style["bold"] = True
        else:
            deferred.append("bold")

    # Font family: emit only a uniform, real font; Tableau's internal font families ('Tableau Bold'
    # etc.) have no Power BI equivalent, so defer them rather than emit an unresolvable face.
    family = _uniform("fontname")
    if family is not None and not _TITLE_INTERNAL_FONT_RE.match(family.strip()):
        style["font_family"] = family.strip()
    elif any(r.get("fontname") for r in text_runs):
        deferred.append("fontname")

    for attr in _TITLE_ALWAYS_DEFER_ATTRS:
        if any(r.get(attr) for r in text_runs):
            deferred.append(attr)

    if not style and not deferred:
        return None
    if deferred:
        style["deferred"] = deferred
    return style


# --- Font/formatting-fidelity cascade (Tier-2) -------------------------------
# Resolve Tableau's per-object <style-rule element='X'> font/shading cascade and stamp the resolved
# formatting onto the rebuilt PBIR visual. Mirrors _parse_title_style's warn-never-wrong contract:
# a property is emitted only when every in-scope value agrees; anything ambiguous defers (never a
# guess). See files handoff spec (Font & Formatting Fidelity full build).

# Tableau's documented per-element DEFAULT font size, transcribed from Tableau Desktop's Format
# dialogs (build 2026.2) and confirmed by the workbook owner. A Pure-Defaults workbook writes NO
# font at all, so these app defaults are not recoverable from the file; they are used ONLY as the
# cascade base layer when the workbook is silent at every level. Any authored <style-rule> overrides
# them (pure extraction wins). SIZE ONLY: the default family is always a Tableau-internal face
# (Book/Medium/Light) with no Power BI equivalent, so family is never defaulted; default weight
# ("Tableau Medium" = semibold) has no clean PBI toggle, so bold is emitted only when authored.
_TABLEAU_FONT_DEFAULTS = {
    "quick-filter-title":   {"font_size": "9"},   # filter/set/param title
    "quick-filter":         {"font_size": "9"},   # filter/set/param body
    "parameter-ctrl-title": {"font_size": "9"},
    "parameter-ctrl":       {"font_size": "9"},
    "worksheet":            {"font_size": "9"},   # worksheet base (cascades to pane/header/cell)
    "pane":                 {"font_size": "9"},   # matrix/table body
    "header":               {"font_size": "9"},   # matrix/table headers (+ totals)
    "cell":                 {"font_size": "9"},
    "label":                {"font_size": "9"},   # axis / data labels
    "tooltip":              {"font_size": "10"},
    "worksheet-title":      {"font_size": "15"},  # sheet title when shown but silent
    "dashboard-title":      {"font_size": "18"},  # banner
    "dashboard-text":       {"font_size": "9"},   # dashboard text object
}
# Font <format attr=...> names Tableau uses; 'color' and 'font-color' are aliases.
_FONT_ATTRS = ("font-size", "font-family", "font-weight", "color", "font-color")


def _parse_style_font(style, element, *, field=None, data_class=None):
    """Resolve one rendered element's font from a <style> block's <style-rule element='X'> rules.

    Mirrors _parse_title_style's contract: returns a style dict with any confidently-resolvable
    {font_size (a "Nd" literal), font_color (#rrggbb), bold (True), font_family (real face)} plus an
    additive 'deferred' list of property names seen but not uniformly resolvable; None when the
    element has no font rule at all. A <format> that carries a field= / data-class= applies only to
    the matching scope (a None scope on the format = applies to all). Warn-never-wrong: a property is
    emitted only when every in-scope value agrees; conflicting values defer (never guess).
    """
    if style is None:
        return None
    picks = {}
    for rule in _children_local(style, "style-rule"):
        if (rule.get("element") or "").lower() != element.lower():
            continue
        for fmt in _children_local(rule, "format"):
            attr = fmt.get("attr")
            if attr not in _FONT_ATTRS:
                continue
            if fmt.get("field") not in (None, field):
                continue
            if fmt.get("data-class") not in (None, data_class):
                continue
            picks.setdefault(attr, []).append(fmt.get("value"))
    if not picks:
        return None

    def _uniform(attr):
        vals = [v for v in picks.get(attr, []) if v is not None]
        return vals[0] if vals and len(set(vals)) == 1 else None

    out, deferred = {}, []
    size = _font_size_points(_uniform("font-size"))
    if size is not None:
        out["font_size"] = size
    elif picks.get("font-size"):
        deferred.append("font-size")

    color = _uniform("color") or _uniform("font-color")
    if color is not None and _HEX6_RE.match(color):
        out["font_color"] = color
    elif picks.get("color") or picks.get("font-color"):
        deferred.append("color")

    if _uniform("font-weight") == "bold":
        out["bold"] = True
    elif "font-weight" in picks and _uniform("font-weight") is None:
        deferred.append("font-weight")

    family = _uniform("font-family")
    if family is not None and not _TITLE_INTERNAL_FONT_RE.match(family.strip()):
        out["font_family"] = family.strip()
    elif picks.get("font-family"):
        deferred.append("font-family")

    if deferred:
        out["deferred"] = deferred
    return out or None


def _resolve_element_font(ws_table_style, element, *, field=None, data_class=None,
                          zone_style=None, wb_style=None):
    """Compose the effective font for one element across the FULL Tableau cascade, low -> high:
       (1) Tableau app default (documented, SIZE only)      _TABLEAU_FONT_DEFAULTS[element]
       (2) workbook <style> default font                    _parse_style_font(wb_style, ...)
       (3) worksheet sheet-wide default                     _parse_style_font(ws_style, 'worksheet')
       (4) worksheet element-specific rule                  _parse_style_font(ws_style, element, ...)
       (5) dashboard <zone-style> override                  _parse_style_font(zone_style, element, ...)
    Higher layer wins per-property. No invented values beyond the documented (1); layers (2)-(5) are
    pure extraction from the workbook. Returns {font_size?, font_color?, bold?, font_family?} of only
    the properties that resolve, or None.

    NOTE layer (3): Tableau writes a sheet-wide default as <style-rule element='worksheet'> (and
    sometimes 'table'); it cascades to every rendered element on that sheet. This is the layer that
    carries e.g. this workbook's authored 'Segoe UI' family, so it MUST be composed beneath the
    element-specific rule.
    """
    eff = {}
    base = _TABLEAU_FONT_DEFAULTS.get(element)
    if base:
        sz = _font_size_points(base.get("font_size"))
        if sz:
            eff["font_size"] = sz
    layers = [
        _parse_style_font(wb_style, element, field=field, data_class=data_class)
        if wb_style is not None else None,
        _parse_style_font(ws_table_style, "worksheet"),
        _parse_style_font(ws_table_style, "table"),
        _parse_style_font(ws_table_style, element, field=field, data_class=data_class),
        _parse_style_font(zone_style, element, field=field, data_class=data_class)
        if zone_style is not None else None,
    ]
    for layer in layers:
        if not layer:
            continue
        for k in ("font_size", "font_color", "bold", "font_family"):
            if k in layer:
                eff[k] = layer[k]
    return eff or None


# --- Shading / fill (companion pass: background-color -> PBIR fill) -----------
# Shading resolves through the SAME cascade as fonts but is a separate property. No documented app
# default is seeded -- Tableau's default sheet/filter background is *no shading*, so fills are pure
# extraction only (a silent element gets no fill).
_SHADE_ATTRS = ("background-color", "band-color", "shading")
# 8-digit #rrggbbAA (Tableau writes alpha); 6-digit handled by the existing _HEX6_RE.
_HEX8_RE = re.compile(r"^#[0-9a-fA-F]{8}$")
# Sentinel: element explicitly resolved to a fully-transparent fill => emit NO fill (never a box).
_FILL_NONE = object()


def _normalize_fill_hex(value):
    """Warn-never-wrong hex normaliser for a fill value:
         '#rrggbb'                -> '#rrggbb'      (opaque, emit)
         '#rrggbbff'              -> '#rrggbb'      (opaque alpha, strip -> emit)
         '#rrggbb00' / any AA==00 -> _FILL_NONE     (fully transparent -> emit no fill)
         '#rrggbbAA' (other alpha) / malformed -> None (defer; never guess a blend)
    """
    if not value:
        return None
    v = value.strip()
    if _HEX6_RE.match(v):
        return v.lower()
    if _HEX8_RE.match(v):
        rgb, aa = v[:7].lower(), v[7:9].lower()
        if aa == "ff":
            return rgb
        if aa == "00":
            return _FILL_NONE
        return None          # partial alpha -> defer (no faithful single-hex blend)
    return None


def _parse_style_fill(style, element, *, field=None, data_class=None):
    """Companion to _parse_style_font: resolve one element's SHADING from a <style> block.
    Returns {'fill': '#rrggbb'} (opaque), {'fill': _FILL_NONE} (explicitly transparent -> no fill),
    {'deferred': ['background-color']} (partial-alpha/conflict), or None (no fill rule).
    Same scope rules as _parse_style_font (field / data-class; None scope applies to all).
    """
    if style is None:
        return None
    picks = []
    for rule in _children_local(style, "style-rule"):
        if (rule.get("element") or "").lower() != element.lower():
            continue
        for fmt in _children_local(rule, "format"):
            if fmt.get("attr") not in _SHADE_ATTRS:
                continue
            if fmt.get("field") not in (None, field):
                continue
            if fmt.get("data-class") not in (None, data_class):
                continue
            picks.append(fmt.get("value"))
    if not picks:
        return None
    vals = [v for v in picks if v is not None]
    if not vals or len(set(vals)) != 1:          # conflicting deltas -> defer
        return {"deferred": ["background-color"]}
    norm = _normalize_fill_hex(vals[0])
    if norm is None:                              # partial alpha / malformed -> defer
        return {"deferred": ["background-color"]}
    return {"fill": norm}                         # opaque hex or _FILL_NONE


def _resolve_element_fill(ws_table_style, element, *, field=None, data_class=None,
                          zone_style=None, wb_style=None):
    """Compose the effective FILL across the cascade (low -> high), pure extraction (no base default).
    A higher opaque layer wins; a higher _FILL_NONE layer explicitly clears a lower fill (transparent
    override is a real authored decision). Returns {'fill': '#rrggbb'} to emit, or None to emit
    nothing (either no rule anywhere, or the winning layer is transparent)."""
    eff = None
    layers = [
        _parse_style_fill(wb_style, element, field=field, data_class=data_class)
        if wb_style is not None else None,
        _parse_style_fill(ws_table_style, "worksheet"),
        _parse_style_fill(ws_table_style, "table"),
        _parse_style_fill(ws_table_style, element, field=field, data_class=data_class),
        _parse_style_fill(zone_style, element, field=field, data_class=data_class)
        if zone_style is not None else None,
    ]
    for layer in layers:
        if layer and "fill" in layer:
            eff = layer["fill"]                   # opaque hex OR _FILL_NONE (transparent wins if higher)
    if not eff or eff is _FILL_NONE:
        return None                               # nothing to paint
    return {"fill": eff}


def _fill_style_props(fill):
    """A resolved fill dict -> the PBIR data-plane fill property 'backColor' (matrix/table channels:
    values / columnHeaders / rowHeaders / subTotals). Single-quoted hex literal, same shape as
    fontColor. Merge these into the SAME per-channel 'properties' dict as _font_style_props so a
    channel can carry both a face and a plate."""
    props = {}
    if fill and fill.get("fill"):
        props["backColor"] = {"solid": {"color": {"expr": {"Literal": {
            "Value": _semantic_string_literal(fill["fill"])}}}}}
    return props


def _container_background_props(fill):
    """A resolved fill -> the visual-CONTAINER background 'properties' (color + show), the shape the
    banner/textbox already emits. None -> caller passes container_objects=None (no plate)."""
    if not fill or not fill.get("fill"):
        return None
    return {
        "color": {"solid": {"color": {"expr": {"Literal": {
            "Value": _semantic_string_literal(fill["fill"])}}}}},
        "show": {"expr": {"Literal": {"Value": "true"}}},
    }


# --- Object padding (margin = outer, padding = inner; defaults 4 / 0) ---------
# Tableau Layout-panel defaults (px): Outer Padding = 4 all sides (stored <format attr='margin'>),
# Inner Padding = 0 all sides (stored <format attr='padding'>). NOTE the naming flip: UI "Outer" ->
# XML 'margin'; UI "Inner" -> XML 'padding'.
_TABLEAU_PADDING_DEFAULTS = {"outer": 4, "inner": 0}
_SIDES = ("top", "right", "bottom", "left")


def _parse_zone_padding(zone_style):
    """Resolve a dashboard object's outer (margin) + inner (padding) box from its <zone-style>.
    Returns {'outer': {top,right,bottom,left}, 'inner': {top,right,bottom,left}} in px. An all-sides
    <format attr='margin'|'padding' value='N'> seeds all four; a per-side 'margin-top' etc. overrides
    that side. When a family is entirely silent, the documented default (4 outer / 0 inner) fills it.
    A non-numeric value is ignored (keeps the default)."""
    def _num(v):
        try:
            return max(0, int(round(float(v))))
        except (TypeError, ValueError):
            return None
    fmts = {}
    for fmt in _children_local(zone_style, "format") if zone_style is not None else []:
        fmts[fmt.get("attr")] = fmt.get("value")
    box = {}
    for ui, xml_attr in (("outer", "margin"), ("inner", "padding")):
        base = _num(fmts.get(xml_attr))
        if base is None:
            base = _TABLEAU_PADDING_DEFAULTS[ui]      # documented fallback only when silent
        sides = {}
        for s in _SIDES:
            per = _num(fmts.get("{0}-{1}".format(xml_attr, s)))
            sides[s] = per if per is not None else base
        box[ui] = sides
    return box


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

# Tableau hard-codes its "automatic" continuous colour ramp: when the author keeps the default, it
# serialises the colour encoding (``type='interpolated'``) but NO ``<color-palette>`` element, so the
# exact ramp cannot be recovered from the workbook XML. These standard ColorBrewer ramps -- "Blues"
# (sequential) and "RdBu" (diverging), the published colour-science families Tableau's own defaults
# derive from -- stand in for that default with faithful DIRECTION (low -> light, high -> dark) and
# are DISCLOSED at emit time, so a default heat scale is reconstructed rather than silently dropped.
# Provenance: original work. The stop values are an unprotectable published colour-science fact
# (ColorBrewer, Cynthia Brewer / Penn State), sourced independently -- not copied from any migration
# tool. The reference tool cyphou/Tableau-To-PowerBI keys all colour handling on an explicit
# ``<color-palette>`` and has no default-ramp handling, so this default-synthesis path is entirely ours.
_DEFAULT_SEQUENTIAL_COLORS = ("#eff3ff", "#08519c")
_DEFAULT_DIVERGING_COLORS = ("#ca0020", "#f7f7f7", "#0571b0")


def _parse_gradient_center(enc):
    """The numeric ``center`` attribute of a colour encoding, or ``None`` when absent/unparseable."""
    raw = enc.get("center")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _default_continuous_gradient(enc):
    """Synthesise a continuous-gradient spec for a colour encoding that is continuous
    (``interpolated``) but carries NO explicit ``<color-palette>`` -- the author kept Tableau's
    default automatic ramp, which Tableau does not serialise. Uses a standard ColorBrewer stand-in
    and flags ``default_palette`` so the emitter discloses the approximation (warn-never-wrong). A
    ``center`` on the encoding implies a diverging default; otherwise the ramp is sequential.
    """
    center = _parse_gradient_center(enc)
    diverging = center is not None
    _, fid = _split_token_attr(enc.get("field"))
    return {
        "field_token": enc.get("field") or "",
        "center": center,
        "palette_type": "ordered-diverging" if diverging else "ordered-sequential",
        "colors": list(_DEFAULT_DIVERGING_COLORS if diverging else _DEFAULT_SEQUENTIAL_COLORS),
        "interpolated": True,
        "is_table_calc": _instance_is_table_calc(fid),
        "default_palette": True,
    }


def _parse_color_gradient(table):
    """Extract a continuous background colour-scale spec from a worksheet's mark colour encoding.

    Returns ``{"field_token", "center", "palette_type", "colors", "interpolated",
    "is_table_calc"}`` when the colour encoding carries a continuous (interpolated / ordered)
    palette of at least two stops, else ``None``. ``colors`` preserves the Tableau author order
    (first -> min, last -> max); the direction is never guessed.

    When the colour encoding is continuous (``interpolated``) but Tableau serialised NO explicit
    ``<color-palette>`` (the author kept the default automatic ramp), a default gradient is
    synthesised (with an additive ``default_palette: True`` flag) so the heat scale is reconstructed
    and disclosed rather than silently dropped. An EXPLICIT palette on any colour encoding always
    wins over the default; only when no encoding yields an explicit gradient is the default used.
    """
    if table is None:
        return None
    style = _first(table, "style")
    if style is None:
        return None
    default_enc = None
    for rule in _children_local(style, "style-rule"):
        if (rule.get("element") or "").lower() != "mark":
            continue
        for enc in _children_local(rule, "encoding"):
            if (enc.get("attr") or "") != "color":
                continue
            enc_type = (enc.get("type") or "").lower()
            interpolated = "interpolated" in enc_type
            palette = _first(enc, "color-palette")
            if palette is not None:
                pal_type = (palette.get("type") or "").lower()
                if interpolated or pal_type in _GRADIENT_PALETTE_TYPES:
                    colors = [(c.text or "").strip()
                              for c in _children_local(palette, "color")
                              if (c.text or "").strip()]
                    if len(colors) >= 2:
                        center = _parse_gradient_center(enc)
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
            # A continuous colour encoding with no usable explicit palette -> Tableau's default
            # automatic ramp. Remembered (not returned) so an explicit palette on a later encoding
            # still wins; synthesised below only if no explicit gradient is found.
            if interpolated and default_enc is None:
                default_enc = enc
    if default_enc is not None:
        return _default_continuous_gradient(default_enc)
    return None


# A DISCRETE (categorical) colour legend assigns an explicit hex per dimension MEMBER at the same
# ``worksheet/table/style/style-rule[@element='mark']/encoding[@attr='color']`` location as the
# continuous heat scale, but with ``<map to='#hex'><bucket>"Member"</bucket></map>`` children
# instead of a ``<color-palette>``. An explicit member->colour map is UNAMBIGUOUS author intent --
# unlike a bare single ``mark-color`` default, which Tableau also writes when the author chose
# nothing -- so it is the high-confidence categorical-palette signal we carry to Power BI.
def _bucket_member(text):
    """The member value carried by a ``<bucket>`` element: a string member is wrapped in literal
    double quotes (``"Central"``) which are stripped; anything else is returned trimmed."""
    s = (text or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def _parse_mark_colors(table):
    """Extract an explicit categorical colour palette (member -> hex) from a worksheet's mark
    colour encoding.

    Returns ``{"field_token", "members": [{"value", "color"}]}`` when the colour encoding carries a
    discrete ``<map to='#hex'><bucket>...</bucket></map>`` palette of at least one member, else
    ``None``. A continuous ``<color-palette>`` gradient (handled by ``_parse_color_gradient``) and a
    bare single ``mark-color`` default are both ignored here -- only an explicit per-member map is an
    unambiguous author colour assignment. Tableau author order is preserved.
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
            if _first(enc, "color-palette") is not None:
                continue  # continuous gradient -> _parse_color_gradient
            members = []
            for mp in _children_local(enc, "map"):
                hexv = (mp.get("to") or "").strip()
                bucket = _first(mp, "bucket")
                if not hexv or bucket is None:
                    continue
                value = _bucket_member(bucket.text)
                if value == "":
                    continue
                members.append({"value": value, "color": hexv})
            if members:
                return {"field_token": enc.get("field") or "", "members": members}
    return None


def _measure_name_from_member(member):
    """The measure name carried by a Measure-Names palette ``<bucket>`` member token.

    A member is a (quote-stripped) field instance token like ``[ds].[sum:Profit:qk]`` (an aggregated
    measure) or ``[ds].[Calc_1:qk]``; the inner ``[...]`` segment is ``agg:Name:type`` (3 parts) or
    ``Name:type`` (2 parts). Returns the bare measure name (``Profit``) or ``None``.
    """
    groups = re.findall(r"\[([^\]]+)\]", member or "")
    if not groups:
        return None
    parts = groups[-1].split(":")
    if len(parts) >= 3:
        return parts[1] or None
    if len(parts) == 2:
        return parts[0] or None
    return groups[-1] or None


def _parse_measure_color_palette(root):
    """Datasource-level "Measure Names" colour palette (measure name -> hex) for the whole workbook.

    Tableau stores the colour a user assigns to each measure of a [Measure Names] colour encoding
    ONCE, on the ``<datasource><style>`` (not per worksheet), so every sheet that colours by Measure
    Names shares it. Returns ``{measure_name_lower: "#rrggbb"}`` (author order collapsed to a map),
    or ``{}`` when no datasource declares such a palette. Only an explicit per-member ``<map>`` is
    read -- a continuous gradient (``<color-palette>``) is ignored. Tableau author order is preserved
    via ``setdefault`` so the first declared colour for a measure wins.
    """
    holders = _children_local(root, "datasources")
    datasources = []
    for h in holders:
        datasources.extend(_children_local(h, "datasource"))
    if not datasources and _local(root.tag) == "datasource":
        datasources = [root]
    palette = {}
    for ds in datasources:
        style = _first(ds, "style")
        if style is None:
            continue
        for rule in _children_local(style, "style-rule"):
            if (rule.get("element") or "").lower() != "mark":
                continue
            for enc in _children_local(rule, "encoding"):
                if (enc.get("attr") or "") != "color":
                    continue
                if "Measure Names" not in (enc.get("field") or ""):
                    continue
                if _first(enc, "color-palette") is not None:
                    continue
                for mp in _children_local(enc, "map"):
                    hexv = (mp.get("to") or "").strip()
                    bucket = _first(mp, "bucket")
                    if not hexv or bucket is None:
                        continue
                    name = _measure_name_from_member(_bucket_member(bucket.text))
                    if name:
                        palette.setdefault(name.lower(), hexv)
    return palette


def _pane_colors_by_measure_names(all_panes):
    """True when any pane carries a ``<color column='...:Measure Names]'/>`` encoding -- i.e. the
    worksheet colours its marks by measure identity (the member measures become the colour series)."""
    for p in all_panes or []:
        encs = _first(p, "encodings")
        if encs is None:
            continue
        for c in _children_local(encs, "color"):
            if (c.get("column") or "").endswith(":Measure Names]"):
                return True
    return False


def _parse_card_label_colors(all_panes):
    """Tableau card ``customized-label`` run colours -> ``{category_color, value_color, value_size}``.

    A KPI / card worksheet whose author recoloured the label text writes a ``<customized-label>``
    ``<formatted-text>`` whose ``<run>`` for the ``[:Measure Names]`` token carries the CATEGORY
    label colour and whose ``<run>`` for the value token carries the VALUE (data label) colour /
    size. Returns the colour dict (only the keys actually present), or ``None`` when no card label is
    recoloured. ``#rrggbb`` only (other colour notations are ignored); the value size passes through
    ``_font_size_points``.
    """
    for p in all_panes or []:
        cl = _first(p, "customized-label")
        ft = _first(cl, "formatted-text") if cl is not None else None
        if ft is None:
            continue
        out = {}
        for run in _findall_local(ft, "run"):
            color = (run.get("fontcolor") or "").strip()
            if not _HEX6_RE.match(color):
                continue
            text = run.text or ""
            if ":Measure Names" in text:
                out.setdefault("category_color", color)
            elif "<" in text and ">" in text:  # a bound value-field run (the big number)
                out.setdefault("value_color", color)
                size = _font_size_points(run.get("fontsize"))
                if size and "value_size" not in out:
                    out["value_size"] = size
        if out:
            return out
    return None


# Tableau's "Show Mark Labels" toggle is written as ``<format attr='mark-labels-show' value='..'/>``
# inside a ``<style-rule element='mark'>`` -- at the worksheet ``table/style`` level and/or each
# ``table/panes/pane/style`` (a dual-axis worksheet carries one per pane, which can disagree). It is
# the data-label show/hide signal Power BI expresses as ``visual.objects.labels`` ``show``.
def _data_label_show_values(style):
    """Boolean values of every ``mark-labels-show`` format under a ``<style>`` (mark style-rules)."""
    out = []
    if style is None:
        return out
    for rule in _children_local(style, "style-rule"):
        if (rule.get("element") or "").lower() != "mark":
            continue
        for fmt in _children_local(rule, "format"):
            if (fmt.get("attr") or "") == "mark-labels-show":
                v = (fmt.get("value") or "").strip().lower()
                if v in ("true", "false"):
                    out.append(v == "true")
    return out


def _parse_data_labels(table, all_panes):
    """Extract the worksheet's data-label (Show Mark Labels) toggle.

    Returns ``{"show": bool|None, "uniform": bool, "raw_values": [bool, ...]}`` when at least one
    ``mark-labels-show`` toggle is present (worksheet-level and/or per-pane), else ``None``.
    ``uniform`` is True when every captured pane agrees; a dual-axis worksheet whose panes disagree
    yields ``uniform=False`` / ``show=None`` so the emitter defers rather than guessing one global
    toggle. Tableau author order is preserved in ``raw_values``.
    """
    if table is None:
        return None
    values = list(_data_label_show_values(_first(table, "style")))
    for pane in all_panes or []:
        values.extend(_data_label_show_values(_first(pane, "style")))
    if not values:
        return None
    uniform = len(set(values)) == 1
    return {"show": values[0] if uniform else None,
            "uniform": uniform,
            "raw_values": values}


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


# A CONSTANT reference line (Tableau ``formula='constant'`` with a fixed numeric ``value=``) on a
# value-axis cartesian chart (column/line/area -- the measure is on the Y axis) is faithfully rebuilt
# as a Power BI analytics reference line (``y1AxisReferenceLine``). Every other annotation -- a
# computed line (average/median/min/max/total), a parameter-driven line, a percentage-band
# distribution, a trend fit, or any non-value-axis chart -- has no constant to place and stays a
# Tier-2 defer. (Discriminator + XML shape grounded on real workbooks: a constant line carries
# ``formula='constant' value='100.0'`` and no ``percentage-bands``/``<reference-line-value>`` band.)
_REFLINE_VALUE_AXIS_VTYPES = (VT_COLUMN, VT_LINE, VT_AREA)


def _constant_reference_value(el):
    """The fixed numeric value of a Tableau ``formula='constant'`` reference line, or ``None`` when
    the line is computed / parameter-driven / a percentage-band distribution (nothing to emit)."""
    if (el.get("formula") or "").strip().lower() != "constant":
        return None
    if (el.get("percentage-bands") or "").strip().lower() == "true":
        return None
    if _children_local(el, "reference-line-value"):
        return None
    raw = el.get("value")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _custom_reference_label(el):
    """A reference line's author-typed custom label (``<Value>`` tokens stripped), or ``None`` when
    the label is automatic/none -- so an emitted line only carries a genuine caption."""
    if (el.get("label-type") or "").strip().lower() != "custom":
        return None
    cleaned = re.sub(r"\s*<[^>]*>", "", (el.get("label") or "")).strip()
    return cleaned or None


def _classify_reference_lines(all_panes, visual_type):
    """Split a worksheet's reference/trend annotations into ``(constants, deferred_labels)``.

    ``constants`` is a list of ``{"value": float, "display_name": str|None}`` for the faithfully
    rebuildable constant lines (only on a value-axis cartesian chart); ``deferred_labels`` lists the
    human-readable names of every annotation that must stay a Tier-2 defer.
    """
    value_axis = visual_type in _REFLINE_VALUE_AXIS_VTYPES
    constants, deferred = [], []
    for pn in all_panes:
        for tag in _REFERENCE_LINE_TAGS:
            for el in _children_local(pn, tag):
                const = _constant_reference_value(el)
                if value_axis and const is not None:
                    constants.append({"value": const,
                                      "display_name": _custom_reference_label(el)})
                else:
                    deferred.append(_annotation_label(el))
        for el in _findall_local(pn, "trend-line"):
            deferred.append("trend line")
    return constants, deferred


def _parse_worksheet(ws, index, ds_caption, warnings, internal_fields=None, date_binding=None,
                     row_count_binding=None, measure_binding=None, column_binding=None,
                     measure_palette=None):
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
                          row_count_binding=row_count_binding, measure_binding=measure_binding,
                          column_binding=column_binding)
    cols = _resolve_shelf(cols_text, ds_default, base_cols, instances, index,
                          ds_caption, name, warnings, warn_special=warn_special,
                          internal_fields=internal_fields, date_binding=date_binding,
                          row_count_binding=row_count_binding, measure_binding=measure_binding,
                          column_binding=column_binding)
    encodings = _parse_encodings(pane, ds_default, base_cols, instances, index,
                                 ds_caption, name, warnings, warn_special=warn_special,
                                 internal_fields=internal_fields, date_binding=date_binding,
                                 row_count_binding=row_count_binding, measure_binding=measure_binding,
                                 column_binding=column_binding)
    filters, swap_controls = _parse_filters(view, ds_default, base_cols, instances, index,
                                            ds_caption, name, warnings, warn_special=warn_special,
                                            internal_fields=internal_fields)
    sort = _parse_sort(view, ds_default, base_cols, instances, index,
                       ds_caption, name, warnings, internal_fields=internal_fields)

    # Series colours: when a worksheet colours its marks by measure identity -- either by Measure
    # Names (the member measures become the colour series) or directly by a measure value -- the
    # rebuilt cartesian visual's per-measure series follow the workbook's datasource-level
    # Measure-Names palette (the author's declared Sales/Profit colour convention). A worksheet
    # coloured by a DIMENSION keeps its own categorical palette (handled by ``_data_point_colors``),
    # so it is excluded here. The KPI / card label colours come from the worksheet's customized-label
    # runs.
    _color_enc = encodings.get("color")
    _colors_by_measure = (_pane_colors_by_measure_names(all_panes)
                          or (_color_enc is not None and _color_enc.get("kind") == "value"))
    measure_colors = (dict(measure_palette)
                      if (measure_palette and _colors_by_measure) else None)
    card_label_colors = _parse_card_label_colors(all_panes)

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

    title_style = _parse_title_style(ws) if title_text else None

    # Font/shading fidelity for any filter cards this worksheet owns: resolve the slicer header
    # (quick-filter-title), items (quick-filter) faces + the card plate from the worksheet's
    # <table><style> cascade, so a dashboard slicer reproduces the authored face + grey plate
    # rather than Power BI's oversized default. Resolves to the documented 9pt size when silent.
    _tbl_style = _first(table, "style") if table is not None else None
    filter_hdr_style = _resolve_element_font(_tbl_style, "quick-filter-title")
    filter_itm_style = _resolve_element_font(_tbl_style, "quick-filter")
    filter_plate_fill = _resolve_element_fill(_tbl_style, "quick-filter")
    # Grid (matrix/table) header / body / total faces + plates from the same cascade -- resolves to
    # the documented 9pt when silent, matching Tableau's compact grid instead of Power BI's larger
    # default. Only consumed for the matrix/table family at emit time (_grid_font_objects).
    grid_styles = {
        "header": _resolve_element_font(_tbl_style, "header"),
        "body": (_resolve_element_font(_tbl_style, "pane")
                 or _resolve_element_font(_tbl_style, "cell")),
        "header_fill": _resolve_element_fill(_tbl_style, "header"),
        "body_fill": (_resolve_element_fill(_tbl_style, "pane")
                      or _resolve_element_fill(_tbl_style, "cell")),
        "total": _resolve_element_font(_tbl_style, "header", data_class="total"),
        "subtotal_fill": (_resolve_element_fill(_tbl_style, "header", data_class="subtotal")
                          or _resolve_element_fill(_tbl_style, "pane", data_class="subtotal")),
    }

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

    # Explicit categorical mark-colour palette (author member -> hex). Parsed here (additive IR key)
    # and turned into PBIR dataPoint per-member fills at emit time -- faithful-or-warn, so a palette
    # on a visual type that cannot carry a per-member fill, or whose coloured dimension is not bound,
    # defers rather than colouring the wrong mark.
    mark_colors = _parse_mark_colors(table)

    # Data labels (Tableau "Show Mark Labels"): the worksheet's mark-labels-show toggle. Parsed here
    # (additive IR key) and turned into a PBIR ``visual.objects.labels`` show/hide at emit time --
    # faithful-or-warn, so a dual-axis worksheet whose panes disagree defers rather than guessing.
    data_labels = _parse_data_labels(table, all_panes)

    # Reference / target / trend line annotations (KPI goals, average/percentile bands, trend
    # fits) are a Tier-2 analytics concern: record them (additive) and disclose them so the
    # rebuilt visual is never silently missing an author's target overlay. Gated on an emitted
    # visual -- an unsupported worksheet is already wholly deferred, so no extra warning is added.
    reference_lines = []
    reference_line_constants = []
    if visual_type != VT_UNSUPPORTED:
        reference_lines = _parse_reference_lines(all_panes)
        if reference_lines:
            # A constant line on a value-axis chart is now REBUILT as a Power BI analytics reference
            # line; only the annotations we cannot faithfully place (computed / parameter / band /
            # non-value-axis) are deferred and disclosed, so the warning names just the drops.
            reference_line_constants, deferred_labels = _classify_reference_lines(
                all_panes, visual_type)
            if deferred_labels:
                is_card = visual_type == VT_CARD
                labels = ", ".join(dict.fromkeys(deferred_labels))
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
        "title_style": title_style,
        "filter_hdr_style": filter_hdr_style,
        "filter_itm_style": filter_itm_style,
        "filter_plate_fill": filter_plate_fill,
        "grid_styles": grid_styles,
        "axis_titles": axis_titles,
        "color_gradient": color_gradient,
        "mark_colors": mark_colors,
        "measure_colors": measure_colors,
        "card_label_colors": card_label_colors,
        "data_labels": data_labels,
        "reference_lines": reference_lines,
        "reference_line_constants": reference_line_constants,
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


def _zone_background_fill(zone):
    """A dashboard zone's authored background fill -> a ``#rrggbb`` (lower-cased) or ``None``.

    Reads the ``<zone-style>`` ``<format attr='background-color' value='#..'/>`` a Tableau author
    sets on a decoration zone. On a full-width top text zone this fill is the workbook's most
    deliberate brand signal (the crimson header band), so it seeds both the header banner and the
    brand-first report theme. Only a well-formed ``#rrggbb`` is returned (never a name / rgba)."""
    style = _first(zone, "zone-style")
    if style is None:
        return None
    for fmt in _children_local(style, "format"):
        if fmt.get("attr") == "background-color":
            val = (fmt.get("value") or "").strip()
            if _HEX6_RE.match(val):
                return val.lower()
    return None


def _zone_formatted_text(zone):
    """Flatten a dashboard zone's ``<formatted-text>`` ``<run>`` descendants to plain text.

    STRUCTURAL content only (the concatenated run text, stripped); per-run font attributes are read
    separately (see ``_zone_run_color``). Returns ``""`` when the zone carries no formatted text."""
    ft = _first(zone, "formatted-text")
    if ft is None:
        return ""
    return "".join((r.text or "") for r in _findall_local(ft, "run")).strip()


def _zone_run_color(zone):
    """The first text-bearing ``<run>``'s ``fontcolor`` on a zone -> a ``#rrggbb`` or ``None``.

    The banner title's font colour (white over the crimson fill). Returns ``None`` when the first
    text run declares no colour, or declares one that is not a plain ``#rrggbb``."""
    ft = _first(zone, "formatted-text")
    if ft is None:
        return None
    for r in _findall_local(ft, "run"):
        if (r.text or "").strip():
            c = (r.get("fontcolor") or "").strip()
            return c.lower() if _HEX6_RE.match(c) else None
    return None


def _zone_background_fill2(zone):
    """A dashboard text zone's background fill -> ``(#rrggbb|None, transparency_pct|None)``.

    Unlike ``_zone_background_fill`` (strict 6-digit, used for the banner/brand signal), this also
    accepts Tableau's 8-digit ``#rrggbbaa`` -- the form written for ANY non-100%-opaque fill (a
    section-header caption bar is typically ``#5a23b9c1`` ~76% opaque, or ``#5a23b981`` ~50%). It
    returns the 6-digit RGB plus the transparency percent (0 = opaque .. 100 = clear) so a rebuilt
    textbox reproduces the authored see-through look. ``(None, None)`` for a colour name / ``rgba()``
    / malformed value -- never a guessed blend."""
    style = _first(zone, "zone-style")
    if style is None:
        return None, None
    for fmt in _children_local(style, "format"):
        if fmt.get("attr") == "background-color":
            val = (fmt.get("value") or "").strip()
            if _HEX6_RE.match(val):
                return val.lower(), None
            if _HEX8_RE.match(val):
                aa = int(val[7:9], 16)
                return val[:7].lower(), round((255 - aa) / 255 * 100)
            return None, None
    return None, None


def _zone_run_font(zone):
    """The first text-bearing ``<run>``'s ``(colour, bold, size_pt)`` on a zone.

    Colour is a ``#rrggbb`` or ``None``; ``bold`` is ``True`` only when the run declares
    ``bold='true'``; ``size_pt`` is the numeric ``fontsize`` (points) or ``None``. Used to rebuild a
    general text object's caption faithfully (weight / size / colour from the author's own run).
    Mirrors ``_zone_run_color`` for colour, which the banner path still uses on its own."""
    ft = _first(zone, "formatted-text")
    if ft is None:
        return None, False, None
    for r in _findall_local(ft, "run"):
        if (r.text or "").strip():
            c = (r.get("fontcolor") or "").strip()
            color = c.lower() if _HEX6_RE.match(c) else None
            bold = r.get("bold") == "true"
            try:
                size = float(r.get("fontsize")) if r.get("fontsize") else None
            except (TypeError, ValueError):
                size = None
            return color, bold, size
    return None, False, None


def _select_title_banner(candidates, ext_w, ext_h):
    """Choose the dashboard's title banner from its filled top text-zone candidates.

    A title banner is the author's header band: a ``type='text'`` zone carrying a background fill
    AND a non-empty title, spanning most of the dashboard width and sitting at/near the top. The
    two gates (wide + top) exclude the other filled text zones a dashboard may hold -- narrow tinted
    separators / callouts (small ``w``) and lower annotation boxes (large ``y``) -- so only the real
    header is picked, and a dashboard with none returns ``None`` (never-regress). Ties break on the
    topmost, then widest, then leftmost candidate, so the pick is fully deterministic."""
    picks = [c for c in candidates
             if ext_w and c["w"] >= 0.5 * ext_w
             and ((not ext_h) or c["y"] <= 0.2 * ext_h)]
    if not picks:
        return None
    picks.sort(key=lambda c: (round(c["y"], 3), -round(c["w"], 3), round(c["x"], 3)))
    return picks[0]


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
    legend_zones = []
    filter_field_tokens = set()
    filter_zones = []
    seen_params = set()
    banner_candidates = []
    text_objects = []
    image_zones = []
    seen_images = set()
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
        # A title/header zone is a decoration ``type='text'`` zone the author filled and titled
        # (e.g. the full-width crimson band at the very top). It is NOT a worksheet, so it must not
        # enter ``zones`` (existing behaviour below still skips it on the name check); we only
        # additively CAPTURE it here as a banner candidate. The final header is chosen after the
        # loop, once the canvas extent is known (a text zone can appear anywhere in document order).
        if ztype == "text" and None not in (x, y, w, h) and w > 0 and h > 0:
            fill = _zone_background_fill(zone)
            text = _zone_formatted_text(zone)
            if fill and text:
                banner_candidates.append({
                    "text": text, "fill": fill,
                    "text_color": _zone_run_color(zone) or "#ffffff",
                    "x": x, "y": y, "w": w, "h": h})
            # Additively capture EVERY text zone that carries content (fill OPTIONAL) as a general
            # text object -- the section-header caption bars (Director / Manager / Supervisor /
            # Technician over each matrix) and the fill-less instruction / metric-label lines a
            # dashboard places over its worksheets. Each rebuilds as its own textbox (emit loop
            # below), independent of the single wide+top title banner chosen from
            # ``banner_candidates``. Uses the rgba-aware reader so an 8-digit ``#rrggbbaa`` caption
            # keeps its authored transparency instead of collapsing to no-fill, and reads the run's
            # own colour / weight / size for a faithful caption. The chosen banner is de-duped out of
            # this list after the loop so the header is never drawn twice.
            if text:
                fill2, tpct = _zone_background_fill2(zone)
                run_color, run_bold, run_size = _zone_run_font(zone)
                text_objects.append({
                    "text": text, "fill": fill2, "transparency": tpct,
                    "text_color": run_color or "#000000", "bold": run_bold, "font_size": run_size,
                    "x": x, "y": y, "w": w, "h": h})
        # A dashboard FILTER card -- the filter the author actually exposed on the dashboard surface
        # (possibly nested inside a collapsible layout container; the zone walk recurses) -- is what
        # faithfully becomes a page slicer. Capture its field token so slicer emit only surfaces a
        # control the dashboard really had, never an applied-but-unshown scope filter (e.g. a
        # single-member include used only to narrow one sheet). ``param`` carries the same
        # ``[datasource].[field-instance]`` token the worksheet ``<filter column>`` does, so the two
        # match on the raw split; an unrecognised param shape simply captures nothing (fail-closed,
        # miss-over-wrong).
        if ztype == "filter":
            ftok = _split_token_attr(zone.get("param"))
            if ftok[1] is not None:
                filter_field_tokens.add(ftok)
                # Keep the card's real geometry + Tableau show ``mode`` (the sibling ``paramctrl`` /
                # ``color`` branches already retain theirs). Without this, slicer emit has to
                # fabricate a right-rail stack that a page-height guard truncates to five, dropping
                # most cards. ``hidden-by-user`` is a Tableau dashboard SHOW/HIDE TOGGLE on a
                # collapsible filter container -- not a delete -- so it is recorded for diagnostics
                # but is NOT used to drop the slicer downstream: Power BI has no Tier-1 collapse
                # equivalent, so the faithful rebuild surfaces the filter (usable) at its authored
                # position regardless (a dashboard whose whole band is toggled-hidden still rebuilds
                # its filters).
                if None not in (x, y, w, h) and w > 0 and h > 0:
                    filter_zones.append({
                        "token": ftok, "x": x, "y": y, "w": w, "h": h,
                        "mode": zone.get("mode"),
                        "hidden": zone.get("hidden-by-user") == "true",
                    })
            continue
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
        # A dashboard IMAGE object: either a straight bitmap (``type-v2='bitmap'`` with
        # ``param='Image/..png'`` -- e.g. the corner logo) or an image BUTTON
        # (``type-v2='dashboard-object'`` hosting an ``<image-path>`` -- an export / filter-toggle /
        # info icon). Tableau packages the PNG inside the ``.twbx`` (the ``Image/`` archive folder);
        # the faithful Tier-1 rebuild lays each out as a positioned Power BI image visual at the same
        # zone geometry. A button's INTERACTIVITY is not recreated (structure, not behaviour) -- the
        # icon is placed as-is. Captured with its raw image ref + geometry; the emitter resolves the
        # bytes from the packaged resources and skips any image whose bytes are not supplied
        # (fail-closed -- never a broken resource reference). A 2-state toggle button lists
        # ``[outline, filled]``; the shown/active state is the last, matching the always-visible
        # slicer rebuild.
        if ztype in ("bitmap", "dashboard-object") and None not in (x, y, w, h) and w > 0 and h > 0:
                    if ztype == "bitmap":
                        refs = [zone.get("param")] if zone.get("param") else []
                    else:
                        refs = [ip.text for ip in zone.findall(".//image-path") if ip.text]
                    if refs:
                        ref = refs[-1]
                        key = (zone.get("id"), ref, round(x), round(y))
                        if key not in seen_images:
                            seen_images.add(key)
                            image_zones.append({
                                "id": zone.get("id"),
                                "kind": "image" if ztype == "bitmap" else "button",
                                "image": ref, "x": x, "y": y, "w": w, "h": h,
                                "url": zone.get("url"),
                            })
                    continue
        zname = zone.get("name")
        if not zname or zname not in worksheet_names:
            continue
        # A colour-legend decoration zone (``type='color'``) names the worksheet whose colour Series
        # it legends; capture its geometry so the report can faithfully reproduce legend show/position
        # (a present zone = the legend is shown at that side; an absent one = the author hid it).
        if ztype == "color" and None not in (x, y, w, h) and w > 0 and h > 0:
            legend_zones.append({"worksheet": zname, "x": x, "y": y, "w": w, "h": h})
            continue
        # worksheet zones carry no decoration type (legends/filters/titles do)
        if ztype:
            continue
        if None in (x, y, w, h) or w <= 0 or h <= 0:
            continue
        zones.append({"worksheet": zname, "x": x, "y": y, "w": w, "h": h})

    title_banner = _select_title_banner(banner_candidates, ext_w, ext_h)
    if title_banner:
        # The header band is emitted from ``title_banner`` (its own crimson-fill textbox); drop the
        # matching zone from the general text-object list so it is never drawn a second time.
        text_objects = [t for t in text_objects
                        if not (t["text"] == title_banner["text"]
                                and t["x"] == title_banner["x"]
                                and t["y"] == title_banner["y"])]
    return {"name": name, "size": size,
            "extent": {"w": ext_w or None, "h": ext_h or None}, "zones": zones,
            "param_controls": param_controls, "legend_zones": legend_zones,
            "filter_field_tokens": sorted(filter_field_tokens),
            "filter_zones": filter_zones,
            "text_objects": text_objects,
            "image_zones": image_zones,
            "title_banner": title_banner}


def _warn(scope, name, reason):
    return {"scope": scope, "name": name,
            "reason": "manual attention required: " + reason}


def _norm_param_key(key):
    """Normalize a parameter id so the model<->viz seam joins regardless of bracket spelling.

    The model build keys ``param_binding["slicers"]`` by a parameter's internal name *with* brackets
    (``[Parameter 0014172372426784]``); a dashboard parameter-control zone yields the bracket-stripped
    id (``Parameter 0014172372426784``). Strip brackets + surrounding space and casefold so the two
    forms match.
    """
    return (key or "").strip().strip("[]").strip().lower()


def _resolve_parameter_controls(dashboards, params, warnings, param_binding=None):
    """Resolve each dashboard's captured parameter-control zones to a fidelity record (+ slicer/warn).

    A dashboard parameter control (the "hamburger" on the canvas) hosts a Tableau parameter; Tier-1
    rebuilds it as a slicer once the migrated model identifies the parameter's target column (passed
    in ``param_binding["slicers"]``, keyed by parameter id, bracket-insensitive). When the model
    resolved the target, the control's record carries a ``resolved`` ``{table, column, single_select,
    caption}`` binding and :func:`emit_pbir` emits a real single-select slicer at the control's
    dashboard zone -- no warning. Until that binding is available the control is still recorded
    additively (``ir["parameter_controls"]``) with one honest per-control warning so the report never
    silently loses it (warn-never-wrong). The parameter caption/datatype come from
    :func:`_parse_parameters`; the id is the bracket-stripped ``[Parameters].[<id>]`` reference.
    """
    slicers = {}
    for k, v in ((param_binding or {}).get("slicers") or {}).items():
        if isinstance(v, dict) and v.get("table") and v.get("column"):
            slicers[_norm_param_key(k)] = v
    records = []
    for db in dashboards:
        for pc in db.get("param_controls", []):
            pid = pc["param_id"]
            meta = params.get(pid) or {}
            caption = meta.get("caption") or pid
            rec = {
                "param_id": pid,
                "caption": caption,
                "datatype": meta.get("datatype") or None,
                "dashboard": db.get("name"),
                "position": {"x": pc.get("x"), "y": pc.get("y"),
                             "w": pc.get("w"), "h": pc.get("h")},
            }
            bound = slicers.get(_norm_param_key(pid))
            if bound:
                rec["resolved"] = {
                    "table": bound["table"], "column": bound["column"],
                    "single_select": bool(bound.get("single_select", True)),
                    "caption": bound.get("caption") or caption,
                }
                records.append(rec)
                continue
            records.append(rec)
            warnings.append(_warn(
                "dashboard", db.get("name"),
                f"parameter control '{caption}' not rebuilt as a slicer yet -> emit once the "
                f"migrated model identifies the parameter's target column/measure"))
    return records


def _resolve_visual_flags(param_binding, ws_by_name, warnings):
    """Resolve ``param_binding["flags"]`` into per-worksheet visual-level keep-filters.

    The model build translates a Tableau keep-flag calc (a CASE/IF over a parameter that returns a
    keep-value to KEEP a mark and is BLANK otherwise -- e.g. a relative-date window selector) into a
    model measure and hands it back as ``flags[<token>] = {"entity", "measure", "value", "visuals"}``:
    ``token`` is the calc caption, ``measure`` the emitted model measure, ``entity`` its home table
    (default ``_Measures``), ``value`` the keep-value, and ``visuals`` the Tableau worksheet names the
    calc filters (sourced from the workbook's calc usage). Each named worksheet's rebuilt visual then
    carries a visual-level measure filter ``[measure] == value`` (built by :func:`_flag_filter_container`)
    so it opens on the SAME windowed rows, and the now-obsolete parse-time "aggregate/measure filter on
    '<token>'" warning is dropped for that worksheet. Presence in ``flags`` means the model approved the
    translation -- an advisory ``status``/``entity`` stamp is not gated on (``entity`` is still read as
    the measure's home table).

    Warn-never-wrong governs the edges: a flag with a non-numeric keep-value, an empty/absent
    ``visuals`` scope, or a scope naming a worksheet the workbook lacks is left UNAPPLIED with an
    honest warning -- a visual filter is never applied to a guessed set of visuals. Returns
    ``{worksheet_name: [filter_container, ...]}`` (empty when there are no resolvable flags).
    """
    by_ws = {}
    resolved = []
    for token, spec in ((param_binding or {}).get("flags") or {}).items():
        if not isinstance(spec, dict):
            continue
        measure = spec.get("measure")
        if not measure:
            continue
        entity = spec.get("entity") or "_Measures"
        literal = _semantic_numeric_literal(str(spec.get("value", 1)))
        visuals = spec.get("visuals") or []
        if literal is None:
            warnings.append(_warn(
                "filter", measure,
                f"model keep-flag '{measure}' has a non-numeric keep-value -> left unapplied"))
            continue
        if not visuals:
            warnings.append(_warn(
                "filter", measure,
                f"model keep-flag '{measure}' carries no worksheet scope -> left unapplied "
                f"(a visual filter is not emitted rather than guess the scope)"))
            continue
        for ws_name in visuals:
            if ws_name not in ws_by_name:
                warnings.append(_warn(
                    "filter", measure,
                    f"model keep-flag '{measure}' scoped to worksheet '{ws_name}', which is not in "
                    f"the workbook -> skipped for that worksheet"))
                continue
            name = _sanitize(f"flag-{ws_name}-{token}")
            by_ws.setdefault(ws_name, []).append(
                _flag_filter_container(entity, measure, literal, name))
            resolved.append((ws_name, token))
    if resolved:
        _drop_resolved_flag_warnings(warnings, resolved)
    return by_ws


def _drop_resolved_flag_warnings(warnings, resolved):
    """Drop the now-obsolete parse-time "aggregate/measure filter on '<token>'" warnings for the
    ``(worksheet, token)`` pairs a model keep-flag rebuilt. Mutates ``warnings`` in place; every other
    warning is preserved (this only ever REMOVES an advisory the model superseded)."""
    obsolete = set(resolved)
    kept = []
    for w in warnings:
        drop = False
        if isinstance(w, dict) and w.get("scope") == "worksheet":
            reason = w.get("reason") or ""
            for ws_name, token in obsolete:
                if (w.get("name") == ws_name
                        and f"aggregate/measure filter on '{token}'" in reason):
                    drop = True
                    break
        if not drop:
            kept.append(w)
    warnings[:] = kept


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


def parse_twb(xml_text, *, date_binding=None, row_count_binding=None, measure_binding=None,
              column_binding=None, param_binding=None):
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
    measure_palette = _parse_measure_color_palette(root)

    ws_holder = _children_local(root, "worksheets")
    ws_elems = []
    for h in ws_holder:
        ws_elems.extend(_children_local(h, "worksheet"))
    worksheets = []
    for ws in ws_elems:
        parsed = _parse_worksheet(ws, index, ds_caption, warnings,
                                  internal_fields=internal_fields, date_binding=date_binding,
                                  row_count_binding=row_count_binding,
                                  measure_binding=measure_binding,
                                  column_binding=column_binding,
                                  measure_palette=measure_palette)
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
    parameter_controls = _resolve_parameter_controls(dashboards, params, warnings, param_binding)
    sheet_swaps = _detect_sheet_swaps(worksheets, dashboards, params, warnings)
    visual_flags = _resolve_visual_flags(param_binding, ws_by_name, warnings)

    return {"worksheets": worksheets, "dashboards": dashboards,
            "sheet_swaps": sheet_swaps, "parameter_controls": parameter_controls,
            "visual_flags": visual_flags, "warnings": warnings}


# -- PBIR field expression emission --------------------------------------------
def _apply_override(field, model_table, field_map):
    """Return (entity, property, binding) after applying caller overrides.

    A field already rebound to the marked Date dimension by ``_rebind_date_axis`` is AUTHORITATIVE:
    neither ``field_map`` nor the ``model_table`` fallback may pull the active date axis back onto the
    fact's raw date column, so the model build's date facts win over the published-DS column rebind.

    A calc DIMENSION resolved by the model build's ``column_binding`` manifest (``column_rebound``) is
    AUTHORITATIVE for the same reason: the model materialised it into a specific table (a field-parameter
    axis lands in its OWN ``calculated`` table, e.g. ``'Choose Date'[Choose Date]``), so the
    ``model_table`` fallback must not re-pin it onto the fact and produce a dangling ``Sheet1[<calc>]``.
    """
    entity, prop, binding = field["entity"], field["property"], field["binding"]
    if field.get("date_rebound") or field.get("column_rebound"):
        return entity, prop, binding
    if field_map and field["caption"] in field_map:
        ov = field_map[field["caption"]]
        entity = ov.get("entity", entity)
        prop = ov.get("property", prop)
        # ``field_map`` targets are always model COLUMNS (measure calcs are rebound via
        # ``measure_binding``, never here). An explicit override ``binding`` still wins; otherwise a
        # raw ``measure``-kind ref whose caption resolves to a column is rebound TO that column --
        # a ``{"Measure"}`` expression pointing at a column is invalid PBIR -- while an
        # ``aggregation`` pill keeps its aggregation (``SUM`` stays) and a ``column`` stays a column.
        # So a mis-roled ref lands as its real column instead of a dangling measure reference.
        binding = ov.get("binding") or ("column" if binding == "measure" else binding)
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


def _hierarchy_level_projections(field, used_refs):
    """Expand a date field rebound to the model's drill hierarchy into one PBIR HierarchyLevel
    projection per level (Year + Month for a Month truncation). Mirrors a Desktop-authored date
    axis, which carries each level as an active HierarchyLevel field rather than a single flat date
    column. The hierarchy is owned by the model build; this only references it."""
    entity = field["entity"]
    hname = field["hierarchy"]["name"]
    out = []
    for level in field["hierarchy"]["levels"]:
        expr = {"HierarchyLevel": {"Expression": {"Hierarchy": {
            "Expression": {"SourceRef": {"Entity": entity}},
            "Hierarchy": hname}}, "Level": level}}
        qref = base_qref = f"{entity}.{hname}.{level}"
        i = 1
        while qref in used_refs:
            i += 1
            qref = f"{base_qref} {i}"
        used_refs.add(qref)
        out.append({"field": expr, "queryRef": qref,
                    "nativeQueryRef": f"{hname} {level}", "active": True})
    return out


def _role_projections(fields, model_table, field_map, used_refs):
    out = []
    for f in fields:
        if f.get("hierarchy"):
            out.extend(_hierarchy_level_projections(f, used_refs))
        else:
            out.append(_projection(f, model_table, field_map, used_refs))
    return out


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
    geo_levels = [g for g in (ws["encodings"].get("geo_levels") or [])
                  if g.get("kind") == "category"]

    def finest_geo(fallback):
        """The faithful map Location is the finest geo level present (e.g. State over Country)."""
        if geo_levels:
            return [max(geo_levels, key=lambda g: _geo_rank(g.get("geo_area")))]
        return [fallback] if fallback and fallback["kind"] == "category" else []

    def categories(fs):
        return [f for f in fs if f["kind"] == "category"]

    def values(fs):
        return [f for f in fs if f["kind"] == "value"]

    # A calc DIMENSION now binds as a category column (binding="column"); only a genuine measure
    # calc (binding="measure") is invalid on an axis, so flag/drop just those if one lands here.
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
            state["SmallMultiple"] = {"projections": _role_projections(
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
    elif vt == VT_SHAPE_MAP:
        # Shape map (built-in-topology choropleth): the geo-role dimension on Detail is the Category
        # (Location), bound at the FINEST geo level present (State over its parent Country). A single
        # measure (prefer the colour-saturation encoding, else any available) binds the "Value" role
        # -- the shapeMap "Color saturation" well -- so each region shades by the measure with Power
        # BI's default ramp. The role name "Value" and the Category+Value shape are verified against a
        # real Desktop-authored shapeMap visual.json (a US-state choropleth shaded by Sum(Profit)); it
        # is NOT "Gradient"/"Color" (those are filledMap/Bing-map wells). A categorical colour cannot
        # drive a shapeMap legend, so such measure-less maps stay on filledMap (see _route_visual).
        loc = drop_calc_axis(_dedupe(finest_geo(detail)))
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
    elif vt == VT_FILLED_MAP:
        # Filled map (Bing choropleth): the geo-role dimension on Detail is the Category (Location),
        # bound at the FINEST geo level present (State over its parent Country). A single measure
        # (prefer the colour-saturation encoding, else any available) binds the "Gradient" role --
        # the PBIR role behind the filledMap "Color saturation" well -- so the choropleth actually
        # shades by the measure with Power BI's default saturation ramp, mirroring Tableau dropping a
        # measure on the Color shelf. (Matching Tableau's exact palette/stops is a Tier-2 styling
        # pass; the structural well binding is faithful on its own.)
        loc = drop_calc_axis(_dedupe(finest_geo(detail)))
        meas = _dedupe(
            ([color] if color and color["kind"] == "value" else [])
            + values(rows) + values(cols)
            + ([size] if size and size["kind"] == "value" else [])
            + ([label] if label and label["kind"] == "value" else []))
        if loc:
            state["Category"] = {"projections": _role_projections(
                loc, model_table, field_map, used_refs)}
        if meas:
            state["Gradient"] = {"projections": _role_projections(
                meas[:1], model_table, field_map, used_refs)}
        # a categorical (dimension) colour on the Color shelf is the map LEGEND -> the "Series"
        # role (a valid filledMap role on a real visual.json); each area is shaded by its legend
        # member. Mutually exclusive with Gradient by construction: Tableau's single Color shelf
        # holds either a measure (Gradient saturation) or a dimension (Series legend), never both.
        color_series = ([color] if (color and color["kind"] == "category"
                                    and not color["is_calc"]) else [])
        color_series = [f for f in color_series if f not in loc]
        if color_series:
            state["Series"] = {"projections": _role_projections(
                color_series, model_table, field_map, used_refs)}
    elif vt == VT_MAP:
        # symbol / bubble map: the geo dimension binds the Category role (the map's "Location" well
        # -- role NAME is "Category", displayName "Location"; there is NO role literally named
        # "Location", verified against a real classic "map" visual.json). A measure goes on Size
        # (prefer the size encoding); a distinct colour measure binds the "Gradient" well -- the
        # PBIR role behind the Bing map "Color saturation", the SAME role the filled map uses; the
        # classic map has no "Color" role, so geo->Category and colour->Gradient bind correctly.
        loc = drop_calc_axis(_dedupe(finest_geo(detail)))
        size_pref = _dedupe(
            ([size] if size and size["kind"] == "value" else [])
            + values(rows) + values(cols)
            + ([label] if label and label["kind"] == "value" else []))
        size_sel = size_pref[:1]
        color_meas = [color] if (color and color["kind"] == "value") else []
        color_sel = [f for f in color_meas if f not in size_sel][:1]
        if loc:
            state["Category"] = {"projections": _role_projections(
                loc, model_table, field_map, used_refs)}
        if size_sel:
            state["Size"] = {"projections": _role_projections(
                size_sel, model_table, field_map, used_refs)}
        if color_sel:
            state["Gradient"] = {"projections": _role_projections(
                color_sel, model_table, field_map, used_refs)}
        # a categorical (dimension) colour binds the map LEGEND -> the "Series" role (verified on a
        # real classic "map" visual.json, e.g. Series=Continent); bubbles are coloured by legend
        # member. Disjoint from Gradient (above): Gradient takes colour only when it is a measure.
        color_series = ([color] if (color and color["kind"] == "category"
                                    and not color["is_calc"]) else [])
        color_series = [f for f in color_series if f not in loc]
        if color_series:
            state["Series"] = {"projections": _role_projections(
                color_series, model_table, field_map, used_refs)}
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
    if vt == VT_SHAPE_MAP:
        # Same as the filled map: a Location (Category) is essential; the "Value" colour-saturation
        # measure is optional (a geo Detail whose measure was dropped is still a location-only map).
        return "Category" in state
    if vt == VT_MAP:
        return "Category" in state and (
            "Size" in state or "Gradient" in state or "Series" in state)
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
    VT_FILLED_MAP: (["map", "shapeMap"], "medium", None),
    VT_MAP: (["filledMap", "shapeMap"], "medium", None),
    VT_SHAPE_MAP: (["filledMap", "map"], "medium", None),
    VT_TABLE: (["pivotTable"], "medium", None),
    VT_MATRIX: (["tableEx"], "medium", None),
}


def _candidate_plan(vt, chosen_pbir, ws=None, state=None):
    """(ranked candidate PBIR types [chosen first], confidence, hack flag) for a visual."""
    candidates = [chosen_pbir]
    flip = _orientation_flip(chosen_pbir)
    if flip:
        candidates.append(flip)
    extra, confidence, hack = _CANDIDATE_ALTS.get(vt, ([], "high", None))
    if vt == VT_CARD:
        # Spec 9a: a worksheet that card-collapsed but carries a LATENT dimension (a pie's slice
        # category demoted to colour, a scatter's granularity dim on detail, a histogram bin calc,
        # or a field-parameter dimension swap) is really a pie / scatter / bar. We do NOT change the
        # deterministic emit (the safest step) -- we only widen the candidate list the image oracle
        # may switch WITHIN and drop confidence to "medium", so the six real card-collapses become
        # oracle-rescuable. A genuine KPI card has no latent signal -> keeps its single high candidate.
        latent, latent_hack = _card_latent_candidates(ws, state)
        if latent:
            extra, confidence, hack = latent, "medium", latent_hack
    for c in extra:
        if c not in candidates:
            candidates.append(c)
    return candidates, confidence, hack


_BIN_TOKEN_RE = re.compile(r"\bbin(s|ned|ning)?\b", re.I)
_DIM_SWAP_RE = re.compile(r"by dimension|show by", re.I)


def _card_is_constant_measure(ref):
    """A bare-number 'measure' (e.g. ``_Measures.1``) is a Tableau dummy/spacer constant used to fake
    a ring or pad a layout -- it is not a real datum, so it does not count toward the measure tally
    that distinguishes a 1-measure pie from a 2-measure scatter."""
    tail = str(ref or "").rsplit(".", 1)[-1].strip().strip('"')
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", tail))


def _card_latent_candidates(ws, state):
    """Spec 9a latent-dimension detector for a card-collapsed worksheet.

    Returns ``([alternate PBIR chart types], hack_label)`` when a latent dimension is present, else
    ``([], None)``. Conservative + additive: it only reads the already-parsed worksheet IR (encodings
    survive the Measure-Values path) and the emitted value well; it never changes a deterministic emit.
    Signals, in priority order (each maps to the faithful shape the fidelity-oracle confirmed):
      * a **binned calc** demoted into the value well  -> histogram ``clusteredColumn/BarChart``
      * a **field-parameter dimension swap** ("… by Dimension") -> swapped-category column/bar
      * **>=2 measures + a latent detail dimension**  -> ``scatterChart``
      * **<=1 measure + a latent legend/detail category** -> ``pieChart``/``donutChart``
    Bin/swap are detected on the bound fields' CAPTIONS (which keep the Tableau spelling, e.g.
    "Age Bins Label") -- the emitted queryRefs are underscore-sanitised, so a caption is the reliable
    signal. The measure tally comes from the emitted Values well, ignoring bare-number spacer
    constants (a Tableau donut-ring "1" is not a real measure)."""
    if not isinstance(ws, dict):
        return [], None
    enc = ws.get("encodings") or {}

    def _cat(role):
        f = enc.get(role)
        return isinstance(f, dict) and f.get("kind") == "category"

    latent_color = _cat("color")
    latent_detail = _cat("detail")
    # captions of every field this worksheet binds (shelves + marks-card encodings)
    bound = list(ws.get("rows") or []) + list(ws.get("cols") or [])
    for role in ("color", "detail", "size", "label", "angle", "text"):
        f = enc.get(role)
        if isinstance(f, dict):
            bound.append(f)
    captions = [str(f.get("caption") or "") for f in bound if isinstance(f, dict)]
    vals = (state.get("Values") or {}).get("projections", []) if isinstance(state, dict) else []
    refs = [p.get("queryRef") or p.get("field") for p in vals]
    n_real = sum(1 for r in refs if not _card_is_constant_measure(r))
    if any(_BIN_TOKEN_RE.search(c) for c in captions):
        return ["clusteredColumnChart", "clusteredBarChart"], "binned-calc card-collapse"
    if ws.get("swap_controls") or any(_DIM_SWAP_RE.search(c) for c in captions):
        return ["clusteredColumnChart", "clusteredBarChart"], "field-param dimension-swap card-collapse"
    if latent_detail and n_real >= 2:
        return ["scatterChart"], "latent-detail scatter card-collapse"
    if (latent_color or latent_detail) and n_real <= 1:
        return ["pieChart", "donutChart"], "latent-legend pie card-collapse"
    return [], None


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


def _field_alias_map(ws, model_table, field_map):
    """``{emitted_queryRef: source_tableau_caption}`` for every field the worksheet binds.

    A star-schema remodel RENAMES the source as it lands (``Order Date`` -> ``Date.Date``, an
    implicit ``COUNT(Orders)`` -> ``_Measures.count orders``), so a NAME-based structural compare
    UNDER-reports a pixel-faithful visual -- the visual is right, only the field labels differ. This
    additive map (carried on the candidate record, never written into the emitted PBIR) lets a
    rename-aware verifier align its Tableau-side field names to our emitted refs. Built with the SAME
    ``_field_expression`` the projections use, so the refs match what ``_visual_field_summary``
    reports; purely read-only (never mutates ``ws`` or the query state)."""
    out = {}
    fields = list(ws.get("rows") or []) + list(ws.get("cols") or [])
    enc = ws.get("encodings") or {}
    for key in ("color", "size", "label", "detail", "angle"):
        f = enc.get(key)
        if isinstance(f, dict):
            fields.append(f)
    for f in fields:
        if not isinstance(f, dict) or not f.get("caption"):
            continue
        try:
            _, qref, _ = _field_expression(f, model_table, field_map)
        except Exception:
            continue
        if qref and qref not in out:
            out[qref] = f["caption"]
    return out


def _candidate_record(page_name, vname, ws, vtype, state, position, page_display=None,
                      model_table=None, field_map=None):
    candidates, confidence, hack = _candidate_plan(ws["visual_type"], vtype, ws=ws, state=state)
    fields = _visual_field_summary(state)
    rec = {
        "page": page_name,
        "page_display": page_display or page_name,
        "visual": vname,
        "worksheet": ws["name"],
        "visual_type": vtype,
        "candidates": candidates,
        "confidence": confidence,
        "hack": hack,
        "fields": fields,
        "position": position,
    }
    # Rename-alias sidecar: map each emitted ref the oracle reads in ``fields`` back to its source
    # Tableau caption, so a name-based compare can see through a star-schema remodel. Keyed by the
    # EXACT ref (dedup suffix " 2" tolerated on lookup). Additive; only present when it carries info.
    aliases = _field_alias_map(ws, model_table, field_map)
    if aliases:
        aligned = {}
        for refs in fields.values():
            for ref in refs:
                cap = aliases.get(ref) or aliases.get(re.sub(r" \d+$", "", ref))
                if cap:
                    aligned[ref] = cap
        if aligned:
            rec["field_aliases"] = aligned
    return rec


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


def _disclose_default_palette(ws, cg, warnings):
    """Append a warn-never-wrong disclosure that a SYNTHESISED default continuous palette was used
    because Tableau serialised no explicit colours for the author's default automatic ramp. The
    disclosed direction (sequential / diverging) mirrors ``_gradient_color_stops`` exactly."""
    diverging = cg.get("center") is not None and len(cg.get("colors") or []) >= 3
    warnings.append(_warn(
        "worksheet", ws["name"],
        "background colour scale used Tableau's default continuous palette (the source serialised "
        "no explicit colours); applied a default {0} gradient -- verify the colours against the "
        "source".format("diverging" if diverging else "sequential")))


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
    if cg.get("default_palette"):
        fact["default_palette"] = True
        _disclose_default_palette(ws, cg, warnings)
    return value_objects, fact


# -- View-only Quick Table Calc -> Power BI Visual Calculation --------------------------------------
# The report-layer counterpart to the model measure path. A Tableau *quick* table calc (applied on a
# pill via the pill menu -- ``cum:`` / ``movavg:`` / ``pcto:`` ...) has no model equivalent: it is a
# view-layer transform over the worksheet's own result matrix. Its Power BI twin is a **Visual
# Calculation** stored in this visual's ``queryState`` and evaluated along the matrix axis. The quick
# token is stripped off the resolved value pill at the viz layer, so these are correlated back to the
# worksheet by NAME through ``extract_table_calc_usages`` (which recovers each quick pill's addressing
# facts), normalized by ``usage_to_visual_calc_spec``, and rendered by ``emit_visual_calc``.

# Deterministic queryRefs for the projected Visual Calculation(s). Inner-before-outer; the value is
# self-consistent within the visual (a FillRule ``Input`` references the outer calc's queryRef).
_VC_QUERY_REFS = ("select", "select1", "select2", "select3", "select4")

# Cartesian charts carry their measure on the Y axis (not the matrix Values shelf) and their
# dimensions on a single Category axis -- so a Visual Calculation runs along ROWS regardless of the
# Tableau ordering token (chart geometry). The reorder set is the subset whose Category is built
# ``categories(rows) + categories(cols)`` (so a projection-count split can re-nest it); a line/area
# splits its shelves into Category vs SmallMultiple instead, which already carries the partition.
_VC_CHART_TYPES = frozenset({VT_COLUMN, VT_BAR, VT_LINE, VT_AREA})
_VC_REORDER_TYPES = frozenset({VT_COLUMN, VT_BAR})


def _reorder_chart_category(ws, state, usage, model_table, field_map):
    """Re-nest a chart's Category so a COLLAPSE percent-of-total lands on the addressed dimension.

    ``COLLAPSE(m, ROWS)`` removes the **innermost** category level, so the addressed dimension (the one
    the percent runs over) must be innermost and the partition dimension outermost. For a Tableau
    ``ordering-type='Columns'`` the addressed dims are the Rows-shelf dims and the partition dims are
    the Cols-shelf dims, i.e. the reverse of the default ``categories(rows) + categories(cols)`` order,
    so the two shelf groups are swapped. The groups are found by a **projection count** -- how many
    Category projections came from the Rows shelf (a side-effect-free ``_role_projections`` over a
    throwaway ``used_refs``; the count is dedup- and hierarchy-consistent) -- never by fragile
    pill<->projection name matching. Fails closed (leaves the order unchanged) if the split does not
    reconcile. ``ordering-type='Rows'`` already yields partition-outer/addressed-inner, so it is a
    no-op here.
    """
    ot = getattr(usage, "ordering_type", None) or "Table"
    if ot != "Columns":
        return
    cat_state = state.get("Category") or {}
    projections = cat_state.get("projections") or []
    if len(projections) < 2:
        return

    def _categories(fs):
        return [f for f in fs if isinstance(f, dict) and f.get("kind") == "category"]

    def _drop_calc_axis(fs):
        return [f for f in fs
                if not (f.get("is_calc") and f.get("binding") == "measure")]

    n_row = len(_role_projections(
        _drop_calc_axis(_dedupe(_categories(list(ws.get("rows") or [])))),
        model_table, field_map, set()))
    if not 0 < n_row < len(projections):
        return
    row_group = projections[:n_row]
    col_group = projections[n_row:]
    cat_state["projections"] = col_group + row_group   # partition (Cols) outer, addressed (Rows) inner


def _view_only_quick_index(table_calc_usages):
    """Group view-only **quick** table-calc usages by worksheet name.

    Only ``kind == "quick"`` usages are candidates for the Visual-Calculation path; a model-level
    calc-field table calc is the measure path's job. Returns ``{worksheet_name: [usage, ...]}`` -- an
    empty dict when nothing (or ``None``) is passed, so every existing caller (which passes none)
    keeps byte-identical output.
    """
    index = {}
    for usage in (table_calc_usages or []):
        if getattr(usage, "kind", None) != "quick":
            continue
        index.setdefault(getattr(usage, "worksheet", None), []).append(usage)
    index.pop(None, None)
    return index


def _apply_visual_calcs(ws, state, vc_index, model_table, field_map, warnings):
    """Project a view-only quick table calc into this visual as a Power BI Visual Calculation.

    Returns ``(value_objects_or_None, vc_fact_or_None)``:

    * On success it mutates ``state`` in place -- setting the base measure's visibility per role and
      appending the Visual Calculation projection(s) after it -- and, for a conditionally-formatted
      table (``role == "color"``), returns the ``backColor`` FillRule ``value_objects`` that drive the
      cell colour from the (hidden) outer calc. ``vc_fact`` is an additive candidate-record descriptor.
    * When the quick calc cannot be pinned from the workbook facts (axis, calendar offset, or chain
      shape unresolved), it degrades-and-warns (route-to-review): the base-only visual is left
      untouched and ``vc_fact["status"] == "review"`` carries the reason -- never a guessed calc.
    * It is a no-op (``(None, None)``) when there is no quick calc for this worksheet, the emitter
      modules are unavailable, or the visual carries no base value projection to run the calc over.

    Precedence: the model-level table-calc measure path is the first-class owner. If the base value
    pill was rebound to a real model measure (``measure_rebound``), this yields so the two paths never
    double-emit the same transform.

    Cartesian charts (bar / column / line / area) are supported alongside tables/matrices: a chart
    carries its base measure on the ``Y`` role (not the matrix ``Values`` shelf) and its dimensions on
    a single Category axis, so the Visual Calculation runs along ``ROWS`` (chart geometry) and is
    appended to ``Y``. A chart has no colour-role conditional-format concept, so its calc is always the
    shown value (role ``"value"``) and it carries no ``backColor`` FillRule. When the worksheet is not
    a cartesian chart (no / other ``visual_type``) the matrix path runs exactly as before.
    """
    if not vc_index or usage_to_visual_calc_spec is None or emit_visual_calc is None:
        return None, None
    usages = vc_index.get(ws["name"])
    if not usages:
        return None, None

    # Chart vs matrix decides which role the base + Visual Calculation live on, the axis, and how the
    # base pill is found. An absent / non-cartesian ``visual_type`` takes the matrix path unchanged.
    is_chart = ws.get("visual_type") in _VC_CHART_TYPES
    value_key = "Y" if is_chart else "Values"
    values = (state.get(value_key) or {}).get("projections", [])
    if not values:
        return None, None

    usage = usages[0]

    if is_chart:
        # A chart's category axis is the "rows" of its result matrix, so any chart Visual Calculation
        # runs along ROWS regardless of the Tableau ordering token; the calc is always the shown value.
        role = "value"
        visual_axis = "ROWS"
        base_field = next(
            (f for f in (list(ws.get("rows") or []) + list(ws.get("cols") or []))
             if isinstance(f, dict) and f.get("kind") == "value"), None)
    else:
        # A conditionally-formatted table carries a colour encoding pill (the calc drives the fill); a
        # plain table does not (the calc is the shown value). This split decides base/calc visibility.
        role = "color" if ws["encodings"].get("color") else "value"
        visual_axis = None
        # The base measure the calc runs over is the displayed value pill (label / text / colour).
        base_field = (ws["encodings"].get("label") or ws["encodings"].get("text")
                      or ws["encodings"].get("color"))

    # Yield to the model measure path when the base pill was rebound to a real model measure (precedence).
    if not base_field or base_field.get("kind") != "value":
        return None, None
    if base_field.get("measure_rebound"):
        return None, None
    _, base_qref, base_nref = _field_expression(base_field, model_table, field_map)
    base_proj = next((p for p in values if p.get("queryRef") == base_qref), None)
    if base_proj is None:
        return None, None

    # A rank / percentile partitions by the outermost level on its axis (the pane boundary). Resolve
    # it from THIS visual's own outer axis projection so the partition is matrix-true: a chart's single
    # Category axis, else the matrix's outer Columns (then Rows).
    if is_chart:
        part_src = (state.get("Category") or {}).get("projections", [])
    else:
        part_src = ((state.get("Columns") or state.get("Rows") or {}).get("projections", []))
    partition_column = part_src[0].get("nativeQueryRef") if part_src else None

    def _review(reason, family=None):
        warnings.append(_warn(
            "worksheet", ws["name"],
            "view-only quick table calc routed to review ({0}); the visual is emitted "
            "with the base measure only".format(reason)))
        fact = {"kind": "visual_calculation", "worksheet": ws["name"], "role": role,
                "status": "review", "reason": reason,
                "tableau_calc_type": getattr(usage, "calc_type", None),
                "tableau_instance": getattr(usage, "instance", None)}
        if family:
            fact["family"] = family
        return None, fact

    spec, reason = usage_to_visual_calc_spec(usage, role=role, visual_axis=visual_axis)
    if spec is None:
        return _review(reason)

    defs, reason = emit_visual_calc(
        spec, base_measure=base_nref, partition_column=partition_column)
    if not defs:
        return _review(reason, family=spec.family)

    # Project the Visual Calculation(s) after the base measure (inner -> outer), each carrying its
    # native-DAX expression. ``_visual_json`` writes ``queryState`` verbatim, so the custom
    # ``NativeVisualCalculation`` field + ``hidden`` flag pass straight through.
    vc_projections = []
    for i, vc in enumerate(defs):
        qref = _VC_QUERY_REFS[i] if i < len(_VC_QUERY_REFS) else "select{0}".format(i)
        proj = {"field": {"NativeVisualCalculation": {
                    "Language": "dax", "Expression": vc.expression, "Name": vc.name}},
                "queryRef": qref, "nativeQueryRef": vc.name}
        if vc.hidden:
            proj["hidden"] = True
        elif vc.number_format:
            # A visible percent-family calc carries its display format on the projection itself
            # (PBIR ``RoleProjection.format`` -- "format string scoped to the visual"), so the shown
            # ratio renders as a percentage. A hidden colour-driver shows nothing, so it stays
            # unformatted (matching the hand-built oracle, whose hidden calc carries no format).
            proj["format"] = vc.number_format
        vc_projections.append((proj, vc))

    # Plain table / chart: hide the base measure (the calc is the shown value). Conditionally-formatted
    # table: keep the base measure visible (it is the shown number; the hidden calc only drives colour).
    if role == "value":
        base_proj["hidden"] = True
    else:
        base_proj.pop("hidden", None)
    state[value_key]["projections"] = values + [p for p, _ in vc_projections]

    # A chart percent-of-total that collapses to a partition subtotal (COLLAPSE, not COLLAPSEALL)
    # needs the addressed dimension innermost on the Category axis; re-nest it from the same resolver.
    if is_chart and not spec.collapse_all:
        _reorder_chart_category(ws, state, usage, model_table, field_map)

    outer_proj, _ = vc_projections[-1]
    # Background conditional formatting (a heat scale) is faithful to BOTH roles; only WHICH cell it
    # tints differs, because ``selector.metadata`` binds the fill to the measure column that is
    # actually shown (a fill anchored to a hidden column paints nothing):
    #   * colour role -- the shown base cell is tinted (metadata = base), driven by the hidden calc;
    #   * value role  -- the shown calc cell is tinted (metadata = the visible calc), driven by that
    #     same calc's magnitude.
    # Either way the FillRule ``Input`` is the outer Visual Calculation's queryRef and the gradient is
    # the Tableau palette (mirrors ``_conditional_format`` but drives off the calc, not a model
    # measure). Emitted only when the worksheet actually carries a continuous colour gradient; without
    # one ``value_objects`` stays ``None`` and the plain / base-only visual is unchanged. (The oracle
    # anchors even its value-role fills to the base -- a Desktop duplicate-and-flip artifact that
    # leaves them inert; anchoring to the visible calc is the faithful, actually-rendering choice.)
    # A backColor cell fill is a table/matrix concept; a cartesian chart never carries one.
    value_objects = None
    cg = ws.get("color_gradient")
    if cg and not is_chart:
        fill_target = base_qref if role == "color" else outer_proj["queryRef"]
        value_objects = [{
            "properties": {"backColor": {"solid": {"color": {"expr": {"FillRule": {
                "Input": {"SelectRef": {"ExpressionName": outer_proj["queryRef"]}},
                "FillRule": _gradient_color_stops(cg)}}}}}},
            "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                         "metadata": fill_target}}]
    if value_objects is not None and cg.get("default_palette"):
        # The heat scale rode Tableau's default automatic ramp (no serialised colours) -> disclose the
        # synthesised default gradient. Mutually exclusive with the ``_conditional_format`` disclosure:
        # the caller uses whichever path emitted the fill, never both, so a worksheet warns at most once.
        _disclose_default_palette(ws, cg, warnings)

    vc_fact = {
        "kind": "visual_calculation",
        "worksheet": ws["name"],
        "role": role,
        "status": "emitted",
        "family": spec.family,
        "axis": spec.axis,
        "base_measure": base_nref,
        "tableau_calc_type": getattr(usage, "calc_type", None),
        "tableau_instance": getattr(usage, "instance", None),
        "tableau_summary": spec.tableau_summary,
        "visual_calcs": [
            {"name": vc.name, "expression": vc.expression, "hidden": vc.hidden,
             "is_inner": vc.is_inner, "queryRef": p["queryRef"], "format": p.get("format")}
            for p, vc in vc_projections],
    }
    if role == "color":
        vc_fact["backColor"] = {"driver": outer_proj["queryRef"], "target": base_qref,
                                "emitted": value_objects is not None}
    elif value_objects is not None:
        # Value role only records the fact when it actually tinted the shown calc (gradient present);
        # a plain value table without a colour scale keeps no backColor fact and stays unchanged. The
        # fill both drives off and paints the visible calc, so driver == target here.
        vc_fact["backColor"] = {"driver": outer_proj["queryRef"], "target": outer_proj["queryRef"],
                                "emitted": True}
    if value_objects is not None and cg and cg.get("default_palette"):
        # Mirror the general path's fact flag so the estate's colour-scale rollup surfaces this
        # synthesised default gradient regardless of which fill path emitted it.
        vc_fact["default_palette"] = True
    return value_objects, vc_fact


# A per-member dataPoint fill (a ``scopeId`` data selector) is safe on the discrete categorical
# charts where a colour dimension drives separate bars / slices. Line / area charts colour a
# continuous series and an explicit dataPoint override there can drop the line (per the Power BI
# formatting reference), so they defer; tables / matrices carry the backColor heat scale instead.
_DATAPOINT_COLOR_TYPES = (VT_COLUMN, VT_BAR, VT_PIE, VT_DONUT)


def _data_point_colors(ws, state, vtype, model_table, field_map, warnings):
    """Explicit categorical mark-colour palette (member -> hex) -> (data_point_objects, fact).

    ``data_point_objects`` is the ``visual.objects.dataPoint`` entry list (one ``fill`` per author-
    coloured dimension member, each targeted by a ``scopeId`` data selector) or ``None``; ``fact`` is
    an additive descriptor (``status`` ``emitted`` / ``deferred`` plus the raw palette) for the
    candidate record, or ``None`` when the worksheet carries no explicit categorical palette.

    WARN-NEVER-WRONG: colours are emitted ONLY when (a) the visual is one of the discrete
    categorical chart types where a per-member fill is safe and (b) the coloured dimension is
    actually projected in THIS visual (so the selector's column resolves). Otherwise the visual
    emits with theme colours, a structured warning names the deferral, and the raw palette is
    preserved in ``fact``. The selector's ``Left`` reuses the EXACT column expression already
    assigned to the visual's projection, so a colour never references a field the query omits. Shape
    verified against the Power BI formatting reference (per-category scope-identity selector:
    ComparisonKind 0 Equal, Left = the coloured column, Right = the member literal).
    """
    mc = ws.get("mark_colors")
    if not mc:
        return None, None
    fact = {"kind": "categorical_palette",
            "field_token": mc["field_token"],
            "members": mc["members"]}

    color = ws["encodings"].get("color")
    left = None
    if color is not None and color["kind"] == "category":
        expr, _, _ = _field_expression(color, model_table, field_map)
        projected = any(
            p["field"] == expr
            for role in state.values()
            for p in role.get("projections", []))
        if projected and "Column" in expr:
            left = expr

    if vtype not in _DATAPOINT_COLOR_TYPES or left is None:
        reason = ("the {0} visual type does not carry a per-member mark colour".format(vtype)
                  if vtype not in _DATAPOINT_COLOR_TYPES
                  else "the coloured dimension is not bound in this visual")
        warnings.append(_warn(
            "worksheet", ws["name"],
            "categorical mark colours deferred ({0}); the visual is emitted with theme "
            "colours".format(reason)))
        fact["status"] = "deferred"
        fact["reason"] = reason
        return None, fact

    data_point_objects = []
    for m in mc["members"]:
        data_point_objects.append({
            "properties": {"fill": {"solid": {"color": {"expr": {
                "Literal": {"Value": _semantic_string_literal(m["color"])}}}}}},
            "selector": {"data": [{"scopeId": {"Comparison": {
                "ComparisonKind": 0,
                "Left": left,
                "Right": {"Literal": {"Value": _semantic_string_literal(m["value"])}}}}}]},
        })
    fact["status"] = "emitted"
    return data_point_objects, fact


# Series colours by Measure Names: when a chart colours its marks by measure identity, EACH member
# measure renders in its own colour (Sales orange / Profit blue), shared from the workbook's
# datasource-level palette. The faithful PBIR home is a per-measure ``dataPoint`` fill targeted by a
# ``metadata`` selector (the measure's queryRef) -- the same shape Power BI authors for a measure
# series (verified against the area/line measure-series fills in the Desktop-authored oracle), NOT
# the categorical ``scopeId`` data selector (which targets a dimension member, not a measure).
_MEASURE_SERIES_COLOR_TYPES = (VT_COLUMN, VT_BAR, VT_LINE, VT_AREA, VT_COMBO)


def _measure_name_from_queryref(queryref):
    """The bare measure / column name carried by an emitted projection ``queryRef``.

    ``Sum(Orders.Profit)`` -> ``Profit``; a non-aggregated ``Orders.Region`` -> ``Region``; a
    hierarchy level (``Date.Calendar.Year``) -> ``Calendar.Year`` (never a palette measure, so it
    simply will not match). Returns ``None`` when nothing resolves.
    """
    m = re.match(r"^[A-Za-z0-9_]+\(([^.()]+)\.(.+)\)$", queryref or "")
    if m:
        return m.group(2)
    m = re.match(r"^([^.()]+)\.(.+)$", queryref or "")
    if m:
        return m.group(2)
    return None


def _measure_series_colors(ws, state, vtype, warnings):
    """Measure-Names series palette -> (data_point_objects, fact).

    When the worksheet colours its marks by Measure Names, each member measure projected in THIS
    visual gets a ``dataPoint`` fill targeted by a ``metadata`` selector (its queryRef). Returns the
    object list (or ``None``) plus an additive candidate-record ``fact``.

    WARN-NEVER-WRONG: emitted only for the cartesian chart types that carry a per-measure series
    fill, and only for measures whose name matches the palette (case-insensitive); anything else
    defers to theme colours with a structured warning and the raw palette preserved in ``fact``.
    """
    palette = ws.get("measure_colors")
    if not palette:
        return None, None
    if vtype not in _MEASURE_SERIES_COLOR_TYPES:
        # Maps / cards / tables carry their measure colour elsewhere (or not at all) -- not a
        # per-measure series fill. Silently skip (no fact, no warning) rather than feign a deferral.
        return None, None
    fact = {"kind": "measure_series_palette", "palette": dict(palette)}

    objects = []
    seen = set()
    for role in state.values():
        for p in role.get("projections", []):
            qref = p.get("queryRef") or ""
            if qref in seen:
                continue
            name = _measure_name_from_queryref(qref)
            hexv = palette.get(name.lower()) if name else None
            if not hexv:
                continue
            seen.add(qref)
            objects.append({
                "properties": {"fill": {"solid": {"color": {"expr": {
                    "Literal": {"Value": _semantic_string_literal(hexv)}}}}}},
                "selector": {"metadata": qref},
            })
    if not objects:
        reason = "no coloured measure is bound in this visual"
        warnings.append(_warn(
            "worksheet", ws["name"],
            "measure series colours deferred ({0}); the visual is emitted with theme "
            "colours".format(reason)))
        fact["status"] = "deferred"
        fact["reason"] = reason
        return None, fact
    fact["status"] = "emitted"
    fact["count"] = len(objects)
    return objects, fact


# KPI / card label colours: a recoloured Tableau card writes the category-label colour and the
# value (big-number) colour / size on its customized-label runs. The faithful PBIR home is the card
# formatting objects ``categoryLabels`` (the label) and ``dataLabels`` (the value) -- each a
# ``color`` property (and an optional value ``fontSize``), verified against the Power BI card /
# multiRowCard formatting reference. Bold is deliberately NOT emitted (the card label weight
# property is unconfirmed -> warn-never-wrong).
_CARD_LABEL_COLOR_TYPES = ("card", "multiRowCard")


def _card_label_objects(ws, vtype):
    """Card label colours -> ``{categoryLabels, dataLabels}`` objects entry, or ``None``.

    ``vtype`` is the EMITTED Power BI visual type; colours are applied only to the card family.
    """
    if vtype not in _CARD_LABEL_COLOR_TYPES:
        return None
    cc = ws.get("card_label_colors")
    if not cc:
        return None
    out = {}
    if cc.get("category_color"):
        out["categoryLabels"] = [{"properties": {"color": {"solid": {"color": {"expr": {
            "Literal": {"Value": _semantic_string_literal(cc["category_color"])}}}}}}}]
    value_props = {}
    if cc.get("value_color"):
        value_props["color"] = {"solid": {"color": {"expr": {
            "Literal": {"Value": _semantic_string_literal(cc["value_color"])}}}}}
    if cc.get("value_size"):
        value_props["fontSize"] = {"expr": {"Literal": {"Value": cc["value_size"]}}}
    if value_props:
        out["dataLabels"] = [{"properties": value_props}]
    return out or None


# Data labels (Tableau "Show Mark Labels") -> the PBIR data-plane ``visual.objects.labels`` ``show``
# property, applied uniformly (the Power BI formatting reference lists ``labels`` as a visual-wide
# object). The high-value, always-faithful case is turning labels ON to match a Tableau view that
# displayed its numbers; OFF is emitted only for the pie/donut family, whose Power BI default is ON
# (so hiding them matches Tableau). Every other supported chart type defaults OFF in Power BI, so an
# OFF Tableau toggle is a no-op. Label DETAIL (culling / which value / placement) is a deeper Tier-2
# concern -- recorded on the candidate-record fact but not acted on here.
_DATA_LABEL_TYPES = (VT_COLUMN, VT_BAR, VT_LINE, VT_AREA, VT_PIE, VT_DONUT, VT_SCATTER,
                     VT_COMBO, VT_WATERFALL, VT_RIBBON)
_LABELS_DEFAULT_ON_TYPES = (VT_PIE, VT_DONUT)


def _data_labels(ws, vtype, warnings):
    """Tableau "Show Mark Labels" toggle -> (label_objects, fact).

    ``label_objects`` is the ``visual.objects.labels`` entry list (a single ``show`` property,
    applied uniformly -- no selector) or ``None``; ``fact`` is an additive candidate-record
    descriptor, or ``None`` when the worksheet carries no mark-label toggle or the visual type has no
    data-label concept (a table / matrix / card / map already displays its values).

    WARN-NEVER-WRONG: a global ``show`` is emitted only when the toggle is unambiguous (every
    captured pane agrees). When a dual-axis worksheet's panes disagree (per-series label
    visibility), no global toggle is guessed -- the visual keeps its default label visibility and a
    structured warning discloses the deferral. Labels-OFF is emitted only for the pie/donut family
    (Power BI default ON); every other supported type already defaults OFF, so an OFF toggle is a
    no-op (``status`` ``default_off``).
    """
    dl = ws.get("data_labels")
    if not dl or vtype not in _DATA_LABEL_TYPES:
        return None, None
    fact = {"kind": "data_labels", "raw_values": dl.get("raw_values")}
    if not dl.get("uniform"):
        warnings.append(_warn(
            "worksheet", ws["name"],
            "data labels deferred (mark-label visibility differs across the dual-axis panes); "
            "the visual keeps its default label visibility"))
        fact["status"] = "deferred"
        return None, fact
    show = bool(dl.get("show"))
    fact["show"] = show
    if show:
        fact["status"] = "emitted"
        return [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}}}], fact
    if vtype in _LABELS_DEFAULT_ON_TYPES:
        fact["status"] = "emitted"
        return [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}], fact
    fact["status"] = "default_off"
    return None, fact


# Legend (Tableau dashboard colour-legend zone) -> the PBIR data-plane ``visual.objects.legend``
# ``show`` / ``position`` properties, applied uniformly (the Power BI formatting reference lists
# ``legend`` as a visual-wide object with no selector). The signal is dashboard-scoped: Tableau
# writes a ``<zone type='color' name='<worksheet>'>`` for each SHOWN colour-Series legend, so a
# present zone reproduces the legend's side (Right/Left/Top/Bottom) and an absent one (for a
# worksheet that DOES carry a categorical colour Series) means the author hid the legend. Legend
# styling (font / title text / marker rendering) is a deeper Tier-2 concern.
_LEGEND_TYPES = (VT_COLUMN, VT_BAR, VT_LINE, VT_AREA, VT_PIE, VT_DONUT, VT_SCATTER,
                 VT_COMBO, VT_RIBBON)


def _has_color_series(ws):
    """A worksheet carries a categorical colour Series (the thing a legend legends) when its colour
    encoding is a dimension (``kind == "category"``)."""
    color = ws["encodings"].get("color")
    return color is not None and color.get("kind") == "category"


def _legend_side(ws_zone, lz):
    """Return ``'Right'``/``'Left'``/``'Bottom'``/``'Top'`` when the legend zone ``lz`` sits clearly
    on exactly ONE side of its worksheet's zone ``ws_zone`` (same Tableau coordinate space), else
    ``None`` (the legend overlaps the chart or straddles a corner -- too ambiguous to map to a single
    Power BI position enum, so the position is deferred to Power BI's default)."""
    wx, wy, ww, wh = ws_zone["x"], ws_zone["y"], ws_zone["w"], ws_zone["h"]
    lx, ly, lw, lh = lz["x"], lz["y"], lz["w"], lz["h"]
    htol, vtol = ww * 0.05, wh * 0.05
    sides = []
    if lx >= wx + ww - htol:
        sides.append("Right")
    if lx + lw <= wx + htol:
        sides.append("Left")
    if ly >= wy + wh - vtol:
        sides.append("Bottom")
    if ly + lh <= wy + vtol:
        sides.append("Top")
    return sides[0] if len(sides) == 1 else None


def _legend_objects(ws, ws_zone, legend_zones, vtype):
    """Tableau dashboard colour legend -> (legend_objects, fact).

    ``legend_objects`` is the ``visual.objects.legend`` entry list (``show`` + optional ``position``,
    applied uniformly -- no selector) or ``None``; ``fact`` is an additive candidate-record
    descriptor, or ``None`` when the worksheet has no categorical colour Series or the visual type has
    no legend concept (table / matrix / card / map).

    WARN-NEVER-WRONG: a ``position`` is emitted ONLY when a present colour zone sits unambiguously on
    one side of the chart (:func:`_legend_side`); an overlapping/corner zone keeps Power BI's default
    legend position (``status`` ``position_deferred``). ``show:false`` is emitted only when a
    worksheet that genuinely carries a categorical colour Series has NO colour zone on this dashboard
    -- i.e. the author hid the legend; a worksheet with no colour Series produces no legend in either
    tool and is left alone.
    """
    if vtype not in _LEGEND_TYPES or not _has_color_series(ws):
        return None, None
    lz = next((z for z in (legend_zones or []) if z["worksheet"] == ws["name"]), None)
    fact = {"kind": "legend"}
    if lz is None:
        fact["status"] = "hidden"
        return [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}], fact
    side = _legend_side(ws_zone, lz)
    if side is None:
        fact["status"] = "position_deferred"
        return None, fact
    fact["status"] = "emitted"
    fact["position"] = side
    return [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "position": {"expr": {"Literal": {"Value": _semantic_string_literal(side)}}},
    }}], fact


_SHAPE_MAP_USA_STATES = "usa.states.topo"

# Tableau "Orange-Blue Diverging" choropleth palette (the Superstore Profit-by-state map): the most
# negative values are orange, the most positive blue, with white at the break-even centre. Power BI
# does NOT default a diverging gradient's centre to 0 -- left unpinned it auto-centres on the DATA
# midpoint, so a mostly-positive measure paints break-even states orange (verified in Desktop). We
# therefore PIN the centre stop's value to 0 (``_SHAPE_MAP_DIVERGING_CENTRE``) so white lands exactly
# on break-even the way Tableau renders it, while the min/max stops stay value-less = auto data
# low/high. Endpoints approximate Tableau's documented Orange-Blue Diverging ramp.
_SHAPE_MAP_DIVERGING_MIN = "#FEA043"    # orange -> most-negative (loss); value-less = auto data low
_SHAPE_MAP_DIVERGING_MID = "#FFFFFF"    # white  -> pinned at 0 / break-even
_SHAPE_MAP_DIVERGING_MAX = "#4A88C2"    # blue   -> most-positive (high profit); value-less = auto data high
_SHAPE_MAP_DIVERGING_CENTRE = "0D"      # PBIR double-literal 0 -> the pinned centre value (break-even)


def _shape_map_objects(ws):
    """The ``objects.shape`` built-in-map block for a state-grain shapeMap, else ``None``.

    A US-state choropleth needs no bundled TopoJSON: Power BI ships ``usa.states.topo`` as a SHARED
    resource (PackageType 2), so a shapeMap bound to a state Category renders OFFLINE. This block
    pins that built-in map + the albersUsa projection. The exact nesting (``map.geoJson``
    type/name/content + sibling ``projectionEnum``) is verified byte-for-byte against real
    Desktop-authored shapeMap ``visual.json`` files (US-state choropleths shaded by a measure).
    Emitted only when the finest geo level present is state-grain (the built-in map IS US states);
    coarser/finer or non-US geographies return ``None`` (shapeMap then defaults, which the assisted
    intent tier may refine for non-US data).
    """
    geo_levels = [g for g in (ws["encodings"].get("geo_levels") or [])
                  if g.get("kind") == "category"]
    if geo_levels:
        finest = max(geo_levels, key=lambda g: _geo_rank(g.get("geo_area")))
    else:
        finest = ws["encodings"].get("detail")
    area = finest.get("geo_area") if finest else None
    if _geo_rank(area) != _GEO_GRANULARITY["state"]:
        return None
    return [{
        "properties": {
            "map": {
                "geoJson": {
                    "type": {"expr": {"Literal": {"Value": "'shared'"}}},
                    "name": {"expr": {"Literal": {
                        "Value": _semantic_string_literal(_SHAPE_MAP_USA_STATES)}}},
                    "content": {"expr": {"ResourcePackageItem": {
                        "PackageName": "SharedResources",
                        "PackageType": 2,
                        "ItemName": _SHAPE_MAP_USA_STATES,
                    }}},
                },
            },
            "projectionEnum": {"expr": {"Literal": {"Value": "'albersUsa'"}}},
        },
    }]


def _shape_map_datapoint_objects():
    """The default ``objects.dataPoint`` colour-saturation gradient for a measure shapeMap.

    A shapeMap with a measure on the Value well does NOT auto-render its gradient on first open:
    Power BI Desktop shows a flat default fill until the field is nudged off-and-on, which forces
    it to write this block. Emitting it up front makes the choropleth shade immediately, with no
    manual nudge. We emit a DIVERGING ``linearGradient3`` -- orange (most-negative / loss) -> white
    (0, break-even) -> blue (most-positive / high profit) -- matching Tableau's "Orange-Blue
    Diverging" map palette. Power BI does NOT default the centre to 0: left unpinned the gradient
    auto-centres on the DATA midpoint, painting break-even states orange on a mostly-positive
    measure. We therefore pin the ``mid`` stop's ``value`` to 0 (``_SHAPE_MAP_DIVERGING_CENTRE``) so
    white lands on break-even; ``min``/``max`` stay value-less = auto data low/high.
    ``nullColoringStrategy`` ``'asZero'`` and ``showAllDataPoints`` are Desktop's own defaults.
    Structure verified byte-for-byte against a real Desktop-authored ``filledMap``/shapeMap
    ``visual.json`` whose ``linearGradient3`` stops carry both a ``color`` and a value-anchor
    (``value.expr.Literal.Value`` = e.g. ``"0D"``); explicit hex colour literals are valid here.
    """
    return [{
        "properties": {
            "fillRule": {
                "linearGradient3": {
                    "min": {"color": {"expr": {"Literal": {
                        "Value": _semantic_string_literal(_SHAPE_MAP_DIVERGING_MIN)}}}},
                    "mid": {
                        "color": {"expr": {"Literal": {
                            "Value": _semantic_string_literal(_SHAPE_MAP_DIVERGING_MID)}}},
                        "value": {"expr": {"Literal": {
                            "Value": _SHAPE_MAP_DIVERGING_CENTRE}}},
                    },
                    "max": {"color": {"expr": {"Literal": {
                        "Value": _semantic_string_literal(_SHAPE_MAP_DIVERGING_MAX)}}}},
                    "nullColoringStrategy": {
                        "strategy": {"expr": {"Literal": {"Value": "'asZero'"}}}},
                },
            },
            "showAllDataPoints": {"expr": {"Literal": {"Value": "true"}}},
        },
    }]


def _reference_line_analytics_objects(ws):
    """Constant reference lines (``ws['reference_line_constants']``) -> a Power BI
    ``y1AxisReferenceLine`` analytics object dict, ready to merge into ``visual.objects``.

    Only value-axis constants ever reach here (the parse gate populates the field solely for
    column/line/area charts); a computed / parameter / band / non-value-axis line was already
    deferred. Returns ``None`` when there is nothing to draw -- byte-identical output for every
    existing visual.
    """
    if report_formatting is None:
        return None
    consts = ws.get("reference_line_constants") or []
    if not consts:
        return None
    lines = [{"value": c["value"], "display_name": c.get("display_name")} for c in consts]
    return report_formatting.reference_line_objects(lines, "value")


def _apply_grow_to_fit(visual, pbir_vtype):
    """Pin "Grow to fit" column auto-size on a table/matrix's ``columnHeaders`` object.

    "Grow to fit" is the modern "Auto-size behavior" dropdown = the ``columnAdjustment`` ENUM
    ('growToFit'). The legacy ``autoSizeColumnWidth`` boolean ALONE only governs "Custom widths" and,
    with ``columnAdjustment`` absent, resolves to "Fit to content" -- the wonky non-grow default a
    user reported on every rebuilt matrix. So emit BOTH, exactly as a Desktop-saved grid writes them
    (shape verified against real PBIR visual.json + Microsoft's base theme). ``columnAdjustment``'s
    value is a single-quoted semantic-query string literal, matching every other enum literal here
    (e.g. slicer ``mode`` ``'Dropdown'``). Grid-only: column width is a ``tableEx``/``pivotTable``
    concept, so ``pbir_vtype`` gates every other visual out (a safe no-op for cartesian charts, cards,
    slicers, textboxes). The per-column ``columnWidth[]`` "Custom widths" selectors are deliberately
    NOT emitted -- adding them (even empty) is what flips the toggle toward fixed widths. ``setdefault``
    twice so a ``columnHeaders`` object a later formatting pass adds (header font/colour) is never
    clobbered, and a future Tier-2 fixed-width override (``autoSizeColumnWidth=false`` +
    ``columnAdjustment='fixed'``) is respected -- co-existing with any ``values`` background gradient a
    table already carries.
    """
    if pbir_vtype not in ("tableEx", "pivotTable"):
        return
    props = (visual.setdefault("objects", {})
             .setdefault("columnHeaders", [{"properties": {}}])[0]
             .setdefault("properties", {}))
    props.setdefault("columnAdjustment", {"expr": {"Literal": {"Value": "'growToFit'"}}})
    props.setdefault("autoSizeColumnWidth", {"expr": {"Literal": {"Value": "true"}}})


def _visual_json(name, vtype, position, query_state, sort_definition=None,
                 filter_config=None, title=None, title_style=None, axis_titles=None,
                 value_objects=None,
                 data_point_objects=None, label_objects=None, legend_objects=None,
                 shape_objects=None, card_label_objects=None, analytics_objects=None,
                 slicer_mode=None, font_objects=None):
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
    # Explicit categorical mark colours (author member -> hex palette): the data-plane
    # ``visual.objects.dataPoint`` entries, each a ``fill`` targeted by a ``scopeId`` data selector
    # (ComparisonKind 0 Equal, Left = the coloured column, Right = the member literal). Shape
    # verified against the Power BI formatting reference's per-category scope-identity selector.
    # A measure shapeMap reuses this same channel to carry its default saturation gradient (the
    # diverging ``linearGradient3`` block from ``_shape_map_datapoint_objects``) so it renders on open.
    if data_point_objects:
        visual.setdefault("objects", {})["dataPoint"] = data_point_objects
    # Data labels (Tableau "Show Mark Labels"): the data-plane ``visual.objects.labels`` ``show``
    # toggle, applied uniformly (no selector). Per the Power BI formatting reference, ``labels`` is a
    # visual-wide object; only show/hide is set here (label detail styling is Tier-2).
    if label_objects:
        visual.setdefault("objects", {})["labels"] = label_objects
    # Legend (Tableau dashboard colour-legend zone): the data-plane ``visual.objects.legend``
    # ``show`` / ``position`` toggle, applied uniformly (no selector). Per the Power BI formatting
    # reference, ``legend`` is a visual-wide object; only show/position are set here (legend title /
    # font / marker styling is Tier-2).
    if legend_objects:
        visual.setdefault("objects", {})["legend"] = legend_objects
    # Shape map built-in topology (Tier-1 structural): the data-plane ``visual.objects.shape`` entry
    # pinning the Power-BI-provided ``usa.states.topo`` shared map + albersUsa projection, so a
    # state-grain choropleth renders offline. Shape verified against real Desktop-authored shapeMap
    # visual.json files (see ``_shape_map_objects``).
    if shape_objects:
        visual.setdefault("objects", {})["shape"] = shape_objects
    # KPI / card label colours (Tier-2): the data-plane ``visual.objects.categoryLabels`` (label) and
    # ``dataLabels`` (value) entries, each a ``color`` (and optional value ``fontSize``). Shape
    # verified against the Power BI card / multiRowCard formatting reference; emitted only for the
    # card family (see ``_card_label_objects``).
    if card_label_objects:
        for _ck, _cv in card_label_objects.items():
            visual.setdefault("objects", {})[_ck] = _cv
    # Analytics overlays (Tier-2, lifted for value-axis charts): the data-plane
    # ``visual.objects.y1AxisReferenceLine`` list -- one ``{properties, selector:{id}}`` element per
    # faithfully-rebuilt CONSTANT reference line. Object name, ``selectors:{id}`` envelope, and the
    # double ``value`` / string ``displayName`` / solid ``lineColor`` encodings are verified against
    # the Power BI formatting inventory's real reference-line raws. Computed / parameter / band lines
    # never reach here (they were deferred at parse time), so an approximate overlay is never drawn.
    if analytics_objects:
        for _ak, _av in analytics_objects.items():
            visual.setdefault("objects", {})[_ak] = _av
    # Categorical slicer show mode (Tableau dashboard filter card ``checkdropdown`` -> Power BI
    # ``'Dropdown'``; ``checklist`` / ``radiolist`` -> ``'Basic'`` List): the data-plane
    # ``visual.objects.data`` ``mode`` property. Without it Power BI renders its default vertical
    # List, which does not read as the compact dropdown a top filter band uses. Shape (a single-
    # quoted semantic-query string literal under ``data[0].properties.mode``) verified against real
    # PBIR slicer visual.json. Only set for slicers (``slicer_mode`` is None everywhere else), so
    # every non-slicer visual stays byte-identical.
    if slicer_mode:
        visual.setdefault("objects", {})["data"] = [{"properties": {"mode":
            {"expr": {"Literal": {"Value": _semantic_string_literal(slicer_mode)}}}}}]
    # Small multiples (trellis): the data-plane ``visual.objects.smallMultiple`` formatting card.
    # A ``SmallMultiple`` query role (a Rows paning dimension -> one pane per member) BINDS the
    # field, but Desktop needs this card to actually lay the panes out -- without it the role is
    # present yet no trellis renders. ``layoutMode`` 'flow' auto-wraps panes; ``maxItemsPerRow``
    # caps the grid width; ``showEmptyItems`` hides empty panes. The single-name role and this card
    # key are unprotectable PBIR-schema interop facts (authored here against our own IR).
    if query_state and "SmallMultiple" in query_state:
        visual.setdefault("objects", {})["smallMultiple"] = [{
            "properties": {
                "layoutMode": {"expr": {"Literal": {"Value": "'flow'"}}},
                "maxItemsPerRow": {"expr": {"Literal": {"Value": "3L"}}},
                "showEmptyItems": {"expr": {"Literal": {"Value": "false"}}},
            }
        }]
    # Column auto-size ("Grow to fit") -- the table/matrix column-width DEFAULT. Emitted for every
    # rebuilt grid so it opens grow-to-fit instead of Power BI's absent-value "Custom" (fixed) default;
    # a no-op for every non-grid visual. Placed after all data-plane ``objects`` are assembled so it
    # merges (via ``setdefault``) with any ``values`` gradient rather than being clobbered.
    _apply_grow_to_fit(visual, vtype)
    # Structural title text (Tier-1): the worksheet's authored caption -> the visual's container
    # title. Shape verified against the official PBIR visualContainer schema + real reports: a
    # single-quoted semantic-query string literal under visualContainerObjects.title; the
    # auto-generated field-name subtitle is suppressed so only the author's title shows. Tier-2
    # title font styling (uniform font size / colour across the title's runs) is merged in when
    # present; all other run styling is deferred (see ``_parse_title_style`` / ``_title_style_props``).
    if title:
        title_props = {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "text": {"expr": {"Literal": {"Value": _semantic_string_literal(title)}}},
        }
        title_props.update(_title_style_props(title_style))
        visual["visualContainerObjects"] = {
            "title": [{"properties": title_props}],
            "subTitle": [{"properties": {
                "show": {"expr": {"Literal": {"Value": "false"}}},
            }}],
        }
    # Font/formatting fidelity (Tier-2, resolved from the Tableau <style> cascade): per-channel
    # format objects (columnHeaders/values/... for grids; categoryAxis/valueAxis for axes). Each
    # channel's "properties" dict may carry BOTH font props (_font_style_props) AND a fill (backColor,
    # _fill_style_props) -- they share one dict, so one merge loop handles both.
    # ``setdefault(...).update(...)`` composes with any channel object an earlier pass added (e.g. the
    # ``values`` gradient, ``columnHeaders`` grow-to-fit) rather than clobbering it. Emitted only when
    # the cascade resolved a face. (Slicer header/items/plate are applied separately, post-build, by
    # _apply_slicer_format -- a slicer never routes its format through font_objects.)
    if font_objects:
        for _fk, _fv in font_objects.items():
            visual.setdefault("objects", {}).setdefault(_fk, [{"properties": {}}])
            visual["objects"][_fk][0]["properties"].update(_fv[0]["properties"])
    # Small-multiples visuals need a newer schema (see SCHEMA_VISUAL_SM): Desktop drops a
    # SmallMultiple role on the legacy 1.0.0 stamp. The bump is gated to exactly those visuals so the
    # verified non-trellis gates keep their proven 1.0.0 stamp.
    schema = SCHEMA_VISUAL
    if query_state and "SmallMultiple" in query_state:
        schema = SCHEMA_VISUAL_SM
    out = {
        "$schema": schema,
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


def _font_style_props(style):
    """A resolved font dict -> PBIR object 'properties' entries: fontSize (Nd literal),
    fontColor (solid single-quoted hex), bold (quoted boolean), fontFamily (single-quoted face).
    Shared by title / slicer / grid / axis channels -- the property NAMES are identical across them.
    Any 'deferred' styling recorded on the style dict is intentionally NOT emitted (warn-never-wrong).
    """
    props = {}
    if not style:
        return props
    size = style.get("font_size")
    if size:
        props["fontSize"] = {"expr": {"Literal": {"Value": size}}}
    color = style.get("font_color")
    if color:
        props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {
            "Value": _semantic_string_literal(color)}}}}}
    if style.get("bold"):
        props["bold"] = {"expr": {"Literal": {"Value": "true"}}}
    family = style.get("font_family")
    if family:
        props["fontFamily"] = {"expr": {"Literal": {"Value": _semantic_string_literal(family)}}}
    return props


def _title_style_props(title_style):
    """Uniform title font styling -> ``visualContainerObjects.title`` property entries. Delegates to
    the shared :func:`_font_style_props` (the property names are identical); kept as a named wrapper
    so the existing title callers are unchanged."""
    return _font_style_props(title_style)


def _grid_font_objects(ws):
    """Build the matrix/table per-channel format objects (columnHeaders / rowHeaders / values /
    subTotals) from the worksheet's resolved grid styles, each carrying resolved font props and/or a
    backColor plate. Only the grid family; None for anything else so other visuals stay unchanged."""
    if not ws or ws.get("visual_type") not in (VT_MATRIX, VT_TABLE):
        return None
    gs = ws.get("grid_styles") or {}
    fo = {}

    def _put(channel, font, fill):
        props = {}
        if font:
            props.update(_font_style_props(font))
        if fill:
            props.update(_fill_style_props(fill))
        if props:
            fo[channel] = [{"properties": props}]

    _put("columnHeaders", gs.get("header"), gs.get("header_fill"))
    _put("rowHeaders", gs.get("header"), gs.get("header_fill"))
    _put("values", gs.get("body"), gs.get("body_fill"))
    _put("subTotals", gs.get("total"), gs.get("subtotal_fill"))
    return fo or None


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


# -- measure keep-flag -> visual-level filter ----------------------------------
# A Tableau keep-flag calc (a CASE/IF over a parameter that returns a keep-value to KEEP a mark and
# is BLANK otherwise) is translated by the model build into a measure and handed back via
# ``param_binding["flags"]``. Each scoped worksheet's rebuilt visual then carries a visual-level
# measure filter ``[measure] == <keep-value>`` so it opens on the SAME windowed rows. A measure
# filter is always an ``Advanced`` filter; its top-level ``field`` is a Measure ref bound by
# ``Entity`` (the measure's home table), while the inner ``Where`` comparison references the measure
# through the ``From`` source alias (``Source``) -- using ``Entity`` inside ``Where`` is a silent
# filter failure. Shape verified against the published semantic-query schema + real PBIR reports
# (``ComparisonKind`` 0 = Equal). Mirrors ``_filter_container`` but with a ``Measure`` (not
# ``Column``) reference.
def _filter_measure_ref(entity, prop, *, source=None):
    src = {"Source": source} if source else {"Entity": entity}
    return {"Measure": {"Expression": {"SourceRef": src}, "Property": prop}}


def _flag_filter_container(entity, measure, literal, name):
    """One visual-level measure keep-filter container (``[measure] == literal``, Equal)."""
    condition = {"Comparison": {
        "ComparisonKind": 0,  # Equal
        "Left": _filter_measure_ref(entity, measure, source=_FILTER_SOURCE_ALIAS),
        "Right": {"Literal": {"Value": literal}}}}
    return {
        "name": name,
        "field": _filter_measure_ref(entity, measure),
        "type": "Advanced",
        "filter": {
            "Version": 2,
            "From": [{"Name": _FILTER_SOURCE_ALIAS, "Entity": entity, "Type": 0}],
            "Where": [{"Condition": condition}],
        },
        "howCreated": "User",
    }


def _flag_filter_config_for(ir, ws_name):
    """The visual-level ``filterConfig`` for a worksheet's resolved model keep-flags, else ``None``.

    Reads the additive ``ir["visual_flags"]`` map (built at parse time by ``_resolve_visual_flags``).
    Returns ``{"filters": [container, ...]}`` so the rebuilt visual opens windowed, or ``None`` when
    no flag is scoped to this worksheet (the visual then carries no ``filterConfig`` -- byte-for-byte
    the prior behaviour)."""
    containers = (ir.get("visual_flags") or {}).get(ws_name)
    return {"filters": list(containers)} if containers else None


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


# -- Tableau filter-card show mode -> Power BI slicer mode ---------------------
# A Tableau dashboard filter card is authored with a show ``mode``: ``checkdropdown`` /
# ``typeindropdown`` render as a DROPDOWN, while ``checklist`` / ``radiolist`` render as an in-place
# LIST. Power BI's categorical slicer carries the same choice as its ``mode`` formatting property --
# ``'Dropdown'`` or ``'Basic'`` (the List rendering). Map dropdown-family modes to ``'Dropdown'`` and
# list/radio modes to ``'Basic'``, defaulting to ``'Dropdown'`` (the overwhelmingly common Tableau
# quick-filter style and the compact form a top filter band needs). The Power BI mode names and this
# categorical mapping are unprotectable PBIR-schema interop facts.
_LIST_FILTER_MODES = frozenset({"checklist", "radiolist", "radio", "single", "multiple"})


def _tableau_filter_mode_to_pbi(mode):
    m = (mode or "").strip().lower()
    if "dropdown" in m:
        return "Dropdown"
    if m in _LIST_FILTER_MODES:
        return "Basic"
    return "Dropdown"


def _apply_slicer_format(visual, hdr_style=None, itm_style=None, plate_fill=None):
    """Stamp the resolved Tableau quick-filter style onto an already-built slicer visual.

    ``hdr_style`` / ``itm_style`` are resolved font dicts (family/size/weight/color) for the slicer
    header (the filter caption) and the item list; each maps to a PBIR ``objects.header`` /
    ``objects.items`` font block via :func:`_font_style_props`. ``plate_fill`` is the resolved slicer
    background fill -> a ``visualContainerObjects.background`` via :func:`_container_background_props`.
    All three are applied post-build (a slicer NEVER routes its format through ``_visual_json``'s
    ``font_objects`` channel-merge) with ``setdefault(...).update(...)`` so an existing header/items
    object or plate is composed with, not clobbered. A falsy arg is skipped -> that face keeps its
    Power BI default.
    """
    if hdr_style:
        visual.setdefault("objects", {}).setdefault(
            "header", [{"properties": {}}])[0]["properties"].update(_font_style_props(hdr_style))
    if itm_style:
        visual.setdefault("objects", {}).setdefault(
            "items", [{"properties": {}}])[0]["properties"].update(_font_style_props(itm_style))
    cont = _container_background_props(plate_fill) if plate_fill else None
    if cont:
        visual.setdefault("visualContainerObjects", {}).setdefault(
            "background", [{"properties": {}}])[0]["properties"].update(cont)


def _slicer_json(name, field, position, model_table, field_map, *, mode=None, warnings=None):
    expr, qref, nref = _field_expression(field, model_table, field_map)
    state = {"Values": {"projections": [
        {"field": expr, "queryRef": qref, "nativeQueryRef": nref}]}}
    fc = _slicer_filter_config(field, model_table, field_map, name + "-sel",
                               warnings if warnings is not None else [])
    out = _visual_json(name, "slicer", position, state, filter_config=fc, slicer_mode=mode)
    # Slicer face font defaults to Tableau's 9pt quick-filter text; the plate has no default (absent
    # -> Power BI's own slicer background). The ws->field style stash (``_slicer_hdr``/``_slicer_itm``/
    # ``_slicer_plate``) is set in _filter_fields_by_token, where the owning worksheet is in scope; a
    # freshly-built field dict (e.g. a parameter-control slicer) carries none -> the 9pt fallback.
    _default_pt = {"font_size": _font_size_points("9")}
    _apply_slicer_format(
        out["visual"],
        hdr_style=(field.get("_slicer_hdr") if isinstance(field, dict) else None) or _default_pt,
        itm_style=(field.get("_slicer_itm") if isinstance(field, dict) else None) or _default_pt,
        plate_fill=(field.get("_slicer_plate") if isinstance(field, dict) else None))
    return out


def _position(x, y, w, h, z=0, tab=0):
    return {"x": round(x, 2), "y": round(y, 2), "z": z,
            "width": round(w, 2), "height": round(h, 2), "tabOrder": tab}


def _scale_zone(zone, ref_w, ref_h):
    pw = _page_w()
    ph = _page_h()
    sx = pw / ref_w if ref_w else 1
    sy = ph / ref_h if ref_h else 1
    x = max(0.0, min(zone["x"] * sx, pw - 1))
    y = max(0.0, min(zone["y"] * sy, ph - 1))
    w = max(40.0, min(zone["w"] * sx, pw - x))
    h = max(40.0, min(zone["h"] * sy, ph - y))
    return x, y, w, h


def _page_json(name, display_name):
    return {
        "$schema": SCHEMA_PAGE,
        "name": name,
        "displayName": display_name,
        "displayOption": "FitToPage",
        "height": _page_h(),
        "width": _page_w(),
    }


def _emit_page(parts, page_name, display_name, visuals):
    """Write a page.json plus its visual.json parts; ``visuals`` is a list of dicts."""
    base = f"definition/pages/{page_name}"
    parts[f"{base}/page.json"] = _dumps(_page_json(page_name, display_name))
    for v in visuals:
        parts[f"{base}/visuals/{v['name']}/visual.json"] = _dumps(v)


def _dumps(obj):
    return json.dumps(obj, indent=2)


# -- dashboard title banner (header band) -------------------------------------
# The banner font size is fixed rather than lifted from the source run: a scaled header band is a
# few dozen px tall, so a single "reasonably large" bold size reads as a header at any page scale
# (the source point size, tuned to Tableau's own banner geometry, does not transfer 1:1).
_BANNER_FONT_SIZE = "18pt"


def _banner_textbox_visual(name, position, banner):
    """A dashboard title banner -> a schema-valid PBIR ``textbox`` ``visual.json`` dict.

    Rebuilds the author's header band: a full-width rectangle filled with the banner colour, showing
    the dashboard title in the banner's text colour (white over the crimson fill), bold and header-
    sized. The text lives in the classic ``objects.general.paragraphs[].textRuns`` channel; the fill
    is the container ``visualContainerObjects.background`` colour (a single-quoted hex literal). The
    visual carries no data binding, so it never dangles against the model. Shape + this exact nesting
    verified against Microsoft's PBIR ``textbox`` examples and validated against the
    ``visualContainer/1.0.0`` schema this engine stamps for every visual (``SCHEMA_VISUAL``)."""
    fill = banner["fill"]
    color = banner.get("text_color") or "#ffffff"
    run = {"value": banner["text"],
           "textStyle": {"fontWeight": "bold", "fontSize": _BANNER_FONT_SIZE, "color": color}}
    visual = {
        "visualType": "textbox",
        "objects": {
            "general": [{"properties": {"paragraphs": [
                {"textRuns": [run], "horizontalTextAlignment": "left"}]}}]
        },
        "visualContainerObjects": {
            "background": [{"properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "color": {"solid": {"color": {"expr": {"Literal": {
                    "Value": _semantic_string_literal(fill)}}}}},
                "transparency": {"expr": {"Literal": {"Value": "0D"}}},
            }}],
            "title": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
        },
        "drillFilterOtherVisuals": True,
    }
    return {"$schema": SCHEMA_VISUAL, "name": name, "position": position, "visual": visual}


_TEXT_OBJECT_FONT_SIZE = "12pt"


def _text_object_textbox_visual(name, position, tob):
    """A general dashboard text object -> a schema-valid PBIR ``textbox`` ``visual.json`` dict.

    Rebuilds any captured dashboard text zone (a section-header caption bar, or a fill-less
    instruction / metric line) as its own textbox: the author's text in its run colour, weight, and
    size, over the zone's authored fill (with transparency preserved when the source was an 8-digit
    ``#rrggbbaa``) or transparent when the zone had no fill. Same ``objects.general.paragraphs`` /
    ``visualContainerObjects.background`` nesting as the title banner, and carries no data binding so
    it never dangles against the model. Distinct from ``_banner_textbox_visual`` only in defaulting to
    a smaller body font and honouring the optional fill / transparency / weight the zone declared."""
    color = tob.get("text_color") or "#000000"
    size = tob.get("font_size")
    font_size = ("%gpt" % size) if size else _TEXT_OBJECT_FONT_SIZE
    style = {"fontSize": font_size, "color": color}
    if tob.get("bold"):
        style["fontWeight"] = "bold"
    run = {"value": tob["text"], "textStyle": style}
    fill = tob.get("fill")
    if fill:
        background = {"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "color": {"solid": {"color": {"expr": {"Literal": {
                "Value": _semantic_string_literal(fill)}}}}},
            "transparency": {"expr": {"Literal": {
                "Value": "%dD" % round(tob.get("transparency") or 0)}}},
        }}
    else:
        background = {"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}
    visual = {
        "visualType": "textbox",
        "objects": {
            "general": [{"properties": {"paragraphs": [
                {"textRuns": [run], "horizontalTextAlignment": "left"}]}}]
        },
        "visualContainerObjects": {
            "background": [background],
            "title": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
        },
        "drillFilterOtherVisuals": True,
    }
    return {"$schema": SCHEMA_VISUAL, "name": name, "position": position, "visual": visual}


def _resource_basename(ref):
    """The bare file name of a Tableau image ref (``Image/EBI Logo Black.png`` -> ``EBI Logo
    Black.png``). Tolerates either slash and a bare name."""
    return (ref or "").replace("\\", "/").rsplit("/", 1)[-1]


def _image_item_name(ref, taken):
    """A deterministic, filesystem-safe RegisteredResources item name for a packaged image.

    Mirrors Power BI Desktop's convention (descriptive stem + a unique suffix + extension) so two
    images with the same base name never collide: ``EBI Logo Black.png`` -> ``EBILogoBlack<hash>.png``.
    The hash is derived from the FULL original ref, so the mapping is stable across runs and unique
    per source image. ``taken`` is the set of already-issued names (defensive against a hash clash)."""
    base = _resource_basename(ref)
    stem, dot, ext = base.rpartition(".")
    stem = stem or base
    ext = ("." + ext) if dot else ".png"
    safe = _sanitize(stem) or "image"
    suffix = hashlib.md5((ref or "").encode("utf-8")).hexdigest()[:12]
    item = f"{safe}{suffix}{ext}"
    while item in taken:
        suffix = hashlib.md5((suffix + ref).encode("utf-8")).hexdigest()[:12]
        item = f"{safe}{suffix}{ext}"
    return item


def _resolve_resource_bytes(resources, ref):
    """Look up an image ref in the packaged ``{archive_path: bytes}`` map.

    Matches the exact archive path first (``Image/EBI Logo Black.png``), then falls back to a
    case-insensitive base-name match so a ref and its archive entry that differ only in folder
    casing still resolve. Returns ``bytes`` or ``None`` (never raises)."""
    if not resources or not ref:
        return None
    if ref in resources:
        return resources[ref]
    want = _resource_basename(ref).lower()
    for k, v in resources.items():
        if _resource_basename(k).lower() == want:
            return v
    return None


def _image_visual(name, position, item_name):
    """A Tableau dashboard image/button object -> a schema-valid PBIR ``image`` ``visual.json`` dict.

    The visual references a PNG bundled in the report's ``RegisteredResources`` package via a
    ``ResourcePackageItem`` expression (``PackageType`` 1). Shape verified against a Power BI Desktop
    image-visual export (``objects.general[].properties.imageUrl`` -> ``ResourcePackageItem``) and the
    ``visualContainer`` schema this engine stamps for every visual (``SCHEMA_VISUAL``). Carries no
    data binding, so it never dangles against the model."""
    visual = {
        "visualType": "image",
        "objects": {"general": [{"properties": {"imageUrl": {"expr": {"ResourcePackageItem": {
            "PackageName": "RegisteredResources",
            "PackageType": 1,
            "ItemName": item_name,
        }}}}}]},
        "drillFilterOtherVisuals": True,
    }
    return {"$schema": SCHEMA_VISUAL, "name": name, "position": position, "visual": visual}


# -- Tableau palette custom theme ---------------------------------------------
# Power BI applies the report theme's ``dataColors`` to every AUTOMATICALLY coloured categorical
# mark (the bulk of a workbook's charts). A migrated report with no custom theme falls back to
# Power BI's default palette, so a Tableau view that read blue/orange/red rebuilds in Fabric's teal
# default -- the single biggest at-a-glance colour mismatch. A custom theme whose ``dataColors`` are
# Tableau's canonical categorical palette recolours every chart at once to the source's colour
# language WITHOUT touching data (a theme is purely cosmetic, and an explicit per-visual ``dataPoint``
# fill still overrides it, so author-assigned member colours keep winning). Positions 1-10 are
# Tableau 10 in EXACT order -- Tableau's default automatic assignment for <=10 categories -- so a
# two-series chart rebuilds blue+orange like Tableau, never blue+light-blue. Positions 11-20 extend
# with distinct darker Tableau 20 hues for the rare >10-category chart; they never perturb the first
# ten. Hex verified against Tableau's published "Tableau 10"/"Tableau 20" palettes.
_TABLEAU_10 = [
    "#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
    "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#BAB0AC"]
_TABLEAU_EXTRA = [
    "#499894", "#D37295", "#B6992D", "#86BCB6", "#79706E",
    "#8CD17D", "#D7B5A6", "#FABFD2", "#A0CBE8", "#FFBE7D"]
_TABLEAU_THEME_FILE = "TableauPalette.json"
_TABLEAU_THEME_DISPLAY = "Tableau"


def _derive_brand_color(ir):
    """The workbook's brand colour -> a ``#rrggbb``, or ``None`` when the workbook carries no signal.

    Derived purely from the parsed dashboards: the brand is the dashboards' title-banner fill (the
    author's deliberate header colour). When several dashboards carry banners of different fills the
    most frequent wins (ties break on the lexically smallest hex, so the pick is deterministic).
    Returns ``None`` when no dashboard has a title banner -- the never-regress guard, so a workbook
    with no header band leaves the report theme byte-identical to the default Tableau palette. No hex
    is hardcoded here: the value is whatever the workbook painted its header band."""
    counts = {}
    for db in ir.get("dashboards", []):
        banner = db.get("title_banner")
        fill = banner.get("fill") if banner else None
        if fill and _HEX6_RE.match(fill):
            key = fill.lower()
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    top = max(counts.values())
    return sorted(h for h, c in counts.items() if c == top)[0]


def tableau_theme_dict(brand=None, extra_palette=None):
    """The custom-theme JSON: a minimal, always-valid Power BI theme (``name`` + ``dataColors``).

    ``dataColors`` is Tableau's categorical palette (Tableau 10, then the distinct Tableau 20
    extras) so automatically coloured marks rebuild in the source's colour language. Deliberately
    minimal -- no ``background``/``foreground`` overrides -- so it recolours marks only and never
    fights the base theme on text/canvas.

    ``brand`` (a workbook-derived ``#rrggbb``, see ``_derive_brand_color``) leads ``dataColors`` when
    given, so a single-series / auto-coloured chart rebuilds in the workbook's brand colour instead of
    Power BI's blue-first default, while the full Tableau 10/20 sequence still trails as the fallback
    for multi-category charts (the brand is de-duplicated out of that tail, case-insensitively, so it
    never appears twice). ``extra_palette`` (an optional ordered list of ``#rrggbb`` -- reserved for a
    later per-member-palette lever) is inserted after the brand, ahead of the Tableau tail, likewise
    de-duplicated. With no ``brand`` and no ``extra_palette`` the return is byte-identical to the prior
    default (the never-regress contract)."""
    base = list(_TABLEAU_10 + _TABLEAU_EXTRA)
    lead = []
    if brand and _HEX6_RE.match(brand):
        lead.append(brand)
    for hex_color in (extra_palette or []):
        if hex_color and _HEX6_RE.match(hex_color):
            lead.append(hex_color)
    if not lead:
        return {"name": _TABLEAU_THEME_DISPLAY, "dataColors": base}
    ordered, seen = [], set()
    for hex_color in lead + base:
        low = hex_color.lower()
        if low not in seen:
            seen.add(low)
            ordered.append(hex_color)
    return {"name": _TABLEAU_THEME_DISPLAY, "dataColors": ordered}


def report_json_part(custom_theme_name=None, image_items=None):
    """The ``definition/report.json`` content shared by the full viz seam (``emit_pbir``) and the
    thin ``.pbip`` shell (``assemble_model.build_thin_report_parts``).

    The ``themeCollection.baseTheme`` is **required**: current Power BI Desktop's enhanced-report
    loader dereferences the report theme inside ``GetEnhancedReportDocument``, so a ``report.json``
    with no ``baseTheme`` throws a ``NullReferenceException`` when the report opens (the semantic
    model still loads, but the authoring canvas/Visualizations pane never initializes). Keeping a
    single builder prevents the two emit paths from drifting on this again.

    When ``custom_theme_name`` is given (the full rebuilt-report path), a ``customTheme`` layered on
    the base theme plus its ``RegisteredResources`` package are added so the report loads a bundled
    theme file at ``StaticResources/RegisteredResources/<custom_theme_name>``. Shape verified against
    the ``report/1.0.0`` schema (``ThemeMetadata`` + ``ResourcePackage``/``ResourcePackageItem``) and
    a real Microsoft enhanced-format report.

    ``image_items`` (an optional list of ``{"name","path","type":"Image"}`` records, one per packaged
    dashboard image) registers those PNGs in the SAME ``RegisteredResources`` package alongside the
    theme item, so ``image`` visuals resolve their ``ResourcePackageItem`` bytes. Default ``None`` (and
    no ``custom_theme_name``) is byte-for-byte the prior output, so the thin ``.pbip`` shell is
    unchanged.
    """
    part = {
        "$schema": SCHEMA_REPORT,
        "layoutOptimization": "None",
        "themeCollection": {"baseTheme": {
            "name": "CY24SU10",
            "reportVersionAtImport": "5.61",
            "type": "SharedResources"}},
    }
    items = []
    if custom_theme_name:
        part["themeCollection"]["customTheme"] = {
            "name": custom_theme_name,
            "reportVersionAtImport": "5.61",
            "type": "RegisteredResources"}
        items.append({"name": custom_theme_name,
                      "path": custom_theme_name,
                      "type": "CustomTheme"})
    for it in (image_items or []):
        items.append(it)
    if items:
        part["resourcePackages"] = [{
            "name": "RegisteredResources",
            "type": "RegisteredResources",
            "items": items}]
    return part


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
    fp_vtype = _VT_TO_PBIR[visual_type]
    visual = {"visualType": fp_vtype, "query": {"queryState": state}}
    # A self-service field-parameter table is a real grid the user sees -- give it the same
    # "Grow to fit" column default as every other rebuilt table/matrix (see ``_apply_grow_to_fit``).
    _apply_grow_to_fit(visual, fp_vtype)
    return {
        "$schema": SCHEMA_VISUAL_FP,
        "name": name,
        "position": position,
        "visual": visual,
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


def _filter_slicer_fields(ws_list, shown_tokens=None):
    """Collect distinct filtered fields across worksheets (one slicer each).

    ``shown_tokens`` is the set of ``(datasource, field-instance)`` tokens the author exposed as
    filter cards on the dashboard surface (from :func:`_parse_dashboard`'s ``filter_field_tokens``).
    When provided, ONLY those filters become slicers -- an applied-but-unshown filter (e.g. a
    single-member scope include that merely narrows one sheet's data) no longer fabricates a slicer
    the dashboard never had. ``None`` keeps every filtered field, used for the standalone
    worksheet-page surface (the worksheet itself is the shown surface there)."""
    seen, out = set(), []
    for ws in ws_list:
        for f in ws.get("filters", []):
            if shown_tokens is not None:
                ft = f.get("filter_token")
                # ``filter_token`` is a (ds, field) tuple in memory but becomes a [ds, field] list
                # across a JSON round-trip of the IR; normalize both sides to a tuple to match.
                if ft is None or tuple(ft) not in shown_tokens:
                    continue
            key = (f["entity"], f["property"])
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
    return out


def emit_pbir(ir, *, dataset_name="Model", report_name="Report",
              model_table=None, field_map=None, table_calc_usages=None, resources=None):
    """Emit a PBIR report definition (a ``{relative_path: text}`` parts dict) from the IR.

    One page per dashboard (a visual per worksheet zone), plus one page per worksheet not
    placed on any dashboard. Visuals bind to the model names captured in the IR; pass
    ``model_table`` to force every column ``Entity`` to a single model table, or ``field_map``
    (``{caption: {"entity","property","binding"}}``) to remap individual fields. Worksheets
    whose ``visual_type`` is ``unsupported`` are skipped (already recorded in ``warnings``).

    ``table_calc_usages`` (optional) carries the workbook's extracted table-calc usages (from
    ``extract_table_calc_usages``). When given, a worksheet whose displayed value is a **view-only
    quick table calc** with no bound model measure gets a Power BI **Visual Calculation** projected
    into its visual (the report-layer twin of the model measure path); without it, that transform
    degrades-and-warns unchanged. Defaults to ``None`` so every existing caller stays byte-identical.
    """
    parts = {}
    ws_by_name = {w["name"]: w for w in ir["worksheets"]}
    warnings = []
    records = []
    vc_index = _view_only_quick_index(table_calc_usages)

    # Pre-pass: register every referenced-and-packaged dashboard image once, so report.json can list
    # it and each page's image visual can reference it by a stable RegisteredResources item name.
    image_resources = {}   # raw Tableau ref -> registered RegisteredResources item name
    image_items = []       # report.json RegisteredResources items ({"name","path","type":"Image"})
    if resources:
        seen_refs = []
        for db in ir.get("dashboards", []):
            for iz in (db.get("image_zones") or []):
                if iz.get("image"):
                    seen_refs.append(iz["image"])
        for ref in dict.fromkeys(seen_refs):   # stable de-dup, one resource per distinct image
            data = _resolve_resource_bytes(resources, ref)
            if data is None:
                continue
            item = _image_item_name(ref, set(image_resources.values()))
            image_resources[ref] = item
            parts["StaticResources/RegisteredResources/" + item] = data   # raw PNG bytes
            image_items.append({"name": item, "path": item, "type": "Image"})

    parts["definition.pbir"] = _dumps({
        "$schema": SCHEMA_DEFINITION_PROPERTIES,
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{dataset_name}.SemanticModel"}},
    })
    parts["definition/version.json"] = _dumps({
        "$schema": SCHEMA_VERSION, "version": "2.0.0"})
    parts["definition/report.json"] = _dumps(
        report_json_part(custom_theme_name=_TABLEAU_THEME_FILE, image_items=image_items or None))
    # Brand-first theme: lead ``dataColors`` with the workbook's derived brand colour (the dashboards'
    # title-banner fill) so auto-coloured single-series charts rebuild in the brand instead of Power
    # BI's blue-first default. ``None`` (no banner/brand) keeps the theme byte-identical (never-regress).
    brand_color = _derive_brand_color(ir)
    parts["StaticResources/RegisteredResources/" + _TABLEAU_THEME_FILE] = _dumps(
        tableau_theme_dict(brand=brand_color))
    parts[".platform"] = _dumps({
        "$schema": SCHEMA_PLATFORM,
        "metadata": {"type": "Report", "displayName": report_name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    })

    page_order = []
    placed = set()

    global _PAGE_H_OVERRIDE, _PAGE_W_OVERRIDE
    for db in ir["dashboards"]:
        page_name = _sanitize("page-" + (db["name"] or "dashboard"))
        zones = db["zones"]
        ref_w = (db["extent"]["w"] or max((z["x"] + z["w"] for z in zones), default=0)
                 or db["size"]["w"])
        ref_h = (db["extent"]["h"] or max((z["y"] + z["h"] for z in zones), default=0)
                 or db["size"]["h"])
        # Emit the page at the dashboard's OWN fixed pixel canvas (<size maxwidth/maxheight>), so a
        # 1400x1000 Tableau dashboard becomes a 1400x1000 page -- exact number-for-number match, aspect
        # preserved. Tableau normalizes the zone coords to a square 100000x100000 (see _scale_zone),
        # so the real aspect is recoverable ONLY from <size>; scaling the normalized rect into the real
        # page (independent sx/sy) de-normalizes it back to faithful pixels. Fallback (no fixed size):
        # Tableau's own 1000x800 default canvas.
        _PAGE_W_OVERRIDE = db["size"]["w"] or DASH_DEFAULT_W
        _PAGE_H_OVERRIDE = db["size"]["h"] or DASH_DEFAULT_H
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
            vc_value_objects, vc_fact = _apply_visual_calcs(
                ws, state, vc_index, model_table, field_map, warnings)
            # The Visual Calculation owns the cell colour whenever it emitted a backColor FillRule --
            # a colour-role table always (the hidden calc drives the fill), and a value-role table when
            # the worksheet carries a colour gradient (the shown calc tints its own column). Skip the
            # measure-driven conditional format then, so the fill is not double-emitted and the stale
            # "colour driver is a quick table calc" defer warning is not raised.
            vc_owns_fill = (vc_fact is not None and vc_fact.get("status") == "emitted"
                            and (vc_fact.get("role") == "color" or vc_value_objects is not None))
            if vc_owns_fill:
                value_objects, cf_fact = vc_value_objects, None
            else:
                value_objects, cf_fact = _conditional_format(
                    ws, state, model_table, field_map, warnings)
            data_point_objects, mc_fact = _data_point_colors(
                ws, state, ws["visual_type"], model_table, field_map, warnings)
            ms_objects, ms_fact = _measure_series_colors(
                ws, state, ws["visual_type"], warnings)
            if ms_objects and not data_point_objects:
                data_point_objects = ms_objects
            card_label_objects = _card_label_objects(ws, vtype)
            label_objects, dl_fact = _data_labels(ws, ws["visual_type"], warnings)
            legend_objects, lg_fact = _legend_objects(
                ws, zone, db.get("legend_zones"), ws["visual_type"])
            flag_fc = _flag_filter_config_for(ir, ws["name"])
            shape_objects = (_shape_map_objects(ws)
                             if ws["visual_type"] == VT_SHAPE_MAP else None)
            # A measure shapeMap needs its colour-saturation gradient written explicitly or
            # Desktop renders a flat fill until the Value field is nudged off-and-on. Route it
            # through the ``dataPoint`` channel (a measure choropleth has no categorical colours).
            if ws["visual_type"] == VT_SHAPE_MAP and not data_point_objects:
                data_point_objects = _shape_map_datapoint_objects()
            analytics_objects = _reference_line_analytics_objects(ws)
            visuals.append(_visual_json(
                vname, vtype, pos, state,
                _sort_definition(ws, state, model_table, field_map),
                filter_config=flag_fc,
                title=ws.get("title"), title_style=ws.get("title_style"),
                axis_titles=ws.get("axis_titles"),
                value_objects=value_objects, data_point_objects=data_point_objects,
                label_objects=label_objects, legend_objects=legend_objects,
                shape_objects=shape_objects, card_label_objects=card_label_objects,
                analytics_objects=analytics_objects,
                font_objects=_grid_font_objects(ws)))
            rec = _candidate_record(page_name, vname, ws, vtype, state, pos,
                                    page_display=db["name"] or page_name,
                                    model_table=model_table, field_map=field_map)
            if cf_fact:
                rec["conditional_format"] = cf_fact
            if vc_fact:
                rec["visual_calc"] = vc_fact
            if mc_fact:
                rec["mark_colors"] = mc_fact
            if ms_fact:
                rec["measure_colors"] = ms_fact
            if card_label_objects:
                rec["card_label_colors"] = ws.get("card_label_colors")
            if dl_fact:
                rec["data_labels"] = dl_fact
            if lg_fact:
                rec["legend"] = lg_fact
            if ws.get("title_style"):
                rec["title_style"] = ws["title_style"]
            if flag_fc:
                rec["flag_filters"] = [c["field"]["Measure"]["Property"]
                                       for c in flag_fc["filters"]]
            records.append(rec)
        visuals += _emit_slicers(
            page_ws, page_name, model_table, field_map, warnings,
            shown_tokens={tuple(t) for t in (db.get("filter_field_tokens") or ())},
            filter_zones=db.get("filter_zones") or [], ref_w=ref_w, ref_h=ref_h)
        visuals += _emit_param_control_slicers(
            ir.get("parameter_controls", []), db["name"], page_name, ref_w, ref_h, warnings)
        # Header band: rebuild the author's full-width title banner (crimson fill + white title) as a
        # textbox pinned to the top strip. High ``z`` keeps the header above any content it abuts;
        # ``tabOrder`` 0 makes it first in reading order. Emitted before the empty-page guard so a
        # dashboard that is only a banner still yields a page. Absent a banner nothing is added, so a
        # bannerless dashboard's output is byte-identical to before (never-regress).
        banner = db.get("title_banner")
        if banner and banner.get("fill"):
            bx, by, bw, bh = _scale_zone(banner, ref_w, ref_h)
            visuals.append(_banner_textbox_visual(
                _sanitize(f"v-{page_name}-banner"),
                _position(bx, by, bw, bh, z=1000, tab=0),
                banner))
        # General text objects: every OTHER captured dashboard text zone (section-header caption bars
        # + fill-less instruction/metric lines) rebuilds as its own textbox at its authored position.
        # ``z=900`` sits below the banner (1000) and images (1100) but above worksheet content, so a
        # caption bar layered over a matrix stays on top. The title banner is already de-duped out of
        # this list upstream, so it is never drawn twice. Empty list -> nothing added (never-regress).
        for j, tob in enumerate(db.get("text_objects") or []):
            tx, ty, tw, th = _scale_zone(tob, ref_w, ref_h)
            visuals.append(_text_object_textbox_visual(
                _sanitize("v-%s-text-%d" % (page_name, j)),
                _position(tx, ty, tw, th, z=900, tab=len(visuals) + 1),
                tob))
        # Shown-state reflow: if the slicers we surfaced (a hidden/collapsed Tableau filter band) now
        # collide with a worksheet zone authored at its hidden-state position, push the sheets below the
        # band and compress to fit -- exactly what Tableau does on "Show Filters". No-op when nothing
        # overlaps (never-regress).
        _reflow_worksheets_below_slicers(visuals, _page_h())
        # Dashboard image / button objects: place each packaged PNG (logo, export/filter/info icon) as
        # a positioned image visual at its own zone geometry. Added AFTER the reflow so a top-corner
        # image is never shoved by the slicer-band compaction (it is decoration, not worksheet
        # content). ``z=1100`` keeps it above the title banner (z=1000) so a logo overlapping the band
        # renders on top. An image whose bytes were not packaged is skipped with an honest warning; a
        # click-through URL (a linked logo/help icon) is noted -- the Tier-1 rebuild places the image
        # faithfully but does not recreate the hyperlink action.
        for iz in (db.get("image_zones") or []):
            item = image_resources.get(iz.get("image"))
            if not item:
                if resources:
                    warnings.append(_warn(
                        "dashboard", db["name"],
                        "image object '%s' not rebuilt (image bytes not packaged with the workbook)"
                        % _resource_basename(iz.get("image"))))
                continue
            ix, iy, iw, ih = _scale_zone(iz, ref_w, ref_h)
            visuals.append(_image_visual(
                _sanitize("v-%s-img-%s" % (page_name, iz.get("id") or item)),
                _position(ix, iy, iw, ih, z=1100, tab=len(visuals) + 1),
                item))
            if iz.get("url"):
                warnings.append(_warn(
                    "dashboard", db["name"],
                    "image object '%s' has a click-through URL that is not rebuilt as a link action "
                    "(image placed faithfully; interactivity deferred)"
                    % _resource_basename(iz.get("image"))))
        if not visuals:
            warnings.append(_warn("dashboard", db["name"],
                                  "no supported visuals on this dashboard"))
            continue
        _emit_page(parts, page_name, db["name"] or page_name, visuals)
        page_order.append(page_name)

    _PAGE_W_OVERRIDE = None
    _PAGE_H_OVERRIDE = None
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
        vc_value_objects, vc_fact = _apply_visual_calcs(
            ws, state, vc_index, model_table, field_map, warnings)
        # The Visual Calculation owns the cell colour whenever it emitted a backColor FillRule -- a
        # colour-role table always (the hidden calc drives the fill), and a value-role table when the
        # worksheet carries a colour gradient (the shown calc tints its own column). Skip the measure-
        # driven conditional format then, so the fill is not double-emitted and the stale "colour
        # driver is a quick table calc" defer warning is not raised.
        vc_owns_fill = (vc_fact is not None and vc_fact.get("status") == "emitted"
                        and (vc_fact.get("role") == "color" or vc_value_objects is not None))
        if vc_owns_fill:
            value_objects, cf_fact = vc_value_objects, None
        else:
            value_objects, cf_fact = _conditional_format(
                ws, state, model_table, field_map, warnings)
        data_point_objects, mc_fact = _data_point_colors(
            ws, state, ws["visual_type"], model_table, field_map, warnings)
        ms_objects, ms_fact = _measure_series_colors(
            ws, state, ws["visual_type"], warnings)
        if ms_objects and not data_point_objects:
            data_point_objects = ms_objects
        card_label_objects = _card_label_objects(ws, vtype)
        label_objects, dl_fact = _data_labels(ws, ws["visual_type"], warnings)
        flag_fc = _flag_filter_config_for(ir, ws["name"])
        shape_objects = (_shape_map_objects(ws)
                         if ws["visual_type"] == VT_SHAPE_MAP else None)
        if ws["visual_type"] == VT_SHAPE_MAP and not data_point_objects:
            data_point_objects = _shape_map_datapoint_objects()
        analytics_objects = _reference_line_analytics_objects(ws)
        main = _visual_json(
            vname, vtype, pos, state,
            _sort_definition(ws, state, model_table, field_map),
            filter_config=flag_fc,
            title=ws.get("title"), title_style=ws.get("title_style"),
            axis_titles=ws.get("axis_titles"),
            value_objects=value_objects, data_point_objects=data_point_objects,
            label_objects=label_objects, shape_objects=shape_objects,
            card_label_objects=card_label_objects,
            analytics_objects=analytics_objects,
            font_objects=_grid_font_objects(ws))
        rec = _candidate_record(page_name, vname, ws, vtype, state, pos,
                                page_display=ws["name"],
                                model_table=model_table, field_map=field_map)
        if cf_fact:
            rec["conditional_format"] = cf_fact
        if vc_fact:
            rec["visual_calc"] = vc_fact
        if mc_fact:
            rec["mark_colors"] = mc_fact
        if ms_fact:
            rec["measure_colors"] = ms_fact
        if card_label_objects:
            rec["card_label_colors"] = ws.get("card_label_colors")
        if dl_fact:
            rec["data_labels"] = dl_fact
        if ws.get("title_style"):
            rec["title_style"] = ws["title_style"]
        if flag_fc:
            rec["flag_filters"] = [c["field"]["Measure"]["Property"]
                                   for c in flag_fc["filters"]]
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
    ir["warnings"] = _reconcile_caption_fallback(ir["warnings"], field_map)
    ir["candidate_records"] = records
    return parts


def _reconcile_caption_fallback(warnings, field_map):
    """Drop caption-fallback warnings the model build's ``field_map`` actually rebinds.

    A parse-time caption-fallback warning (the workbook's embedded datasource carried no
    ``<metadata-record class='column'>`` for the field, so ``_resolve_field`` fell back to the
    datasource caption + ``clean_col(caption)``) is OBSOLETE once ``field_map`` -- the model
    build's metadata-confirmed naming -- contains that caption: ``_apply_override`` then binds the
    projection to the real model table/column, so the "verify it matches model table/column names"
    advisory no longer applies (the model already confirmed it). Captions NOT in ``field_map`` keep
    their warning (genuinely unverified). The internal ``caption_fallback`` marker is always
    stripped so it never surfaces in the report. Warn-never-wrong: this only ever REMOVES a
    now-false advisory the model superseded, never masks a real one.
    """
    confirmed = set(field_map or ())
    kept = []
    for w in warnings:
        cap = w.pop("caption_fallback", None) if isinstance(w, dict) else None
        if cap is not None and cap in confirmed:
            continue
        kept.append(w)
    return kept


def _filter_fields_by_token(ws_list):
    """Map each worksheet filter's raw ``(datasource, field-instance)`` token to its resolved slicer
    field descriptor, so a dashboard filter *card* (which carries only that token + its own geometry)
    can be placed as a slicer bound to the real model column. First occurrence wins on a repeated
    token (the descriptor is identical either way). Uses the same token shape a dashboard
    ``<zone type-v2='filter' param=...>`` carries -- both go through :func:`_split_token_attr`."""
    out = {}
    for ws in ws_list:
        for f in ws.get("filters", []):
            ft = f.get("filter_token")
            if ft is None:
                continue
            # Carry the owning worksheet's resolved slicer formatting onto the field, so the
            # dashboard card rebuilds with the authored face + plate (all filters on a sheet share it).
            f.setdefault("_slicer_hdr", ws.get("filter_hdr_style"))
            f.setdefault("_slicer_itm", ws.get("filter_itm_style"))
            f.setdefault("_slicer_plate", ws.get("filter_plate_fill"))
            out.setdefault(tuple(ft), f)
    return out


def _layout_slicers(entries, *, ctrl_h=SLICER_CTRL_H, pad_x=SLICER_PAD_X,
                    gutter=SLICER_ROW_GUTTER, tol=8.0):
    """In place: turn raw tangent slicer zones into an evenly-gapped grid.

    Tableau packs filter zones edge-to-edge and relies on each card's internal padding for the
    visible gaps; a Power BI slicer instead fills its whole rectangle, so the raw zones collide.
    Each slicer is inset horizontally by ``pad_x`` (reproducing Tableau's inter-card gaps) and its
    height is taken from the REAL scaled card: a DROPDOWN card's height is translated DIRECTLY from the
    Tableau card (floored at ``SLICER_DROPDOWN_MIN_H`` so a tiny card still renders its control), and a
    List/other card keeps its own height (floored at ``ctrl_h``). Nothing is a hardcoded fixed
    size -- the emitted height tracks the source card. When a row's control ends up taller than its
    source zone, the rows below shift down by the growth plus ``gutter`` so tangent bands never
    overlap. Rows are clustered by top-y (``tol`` px)."""
    if not entries:
        return
    rows = []
    for e in sorted(entries, key=lambda z: z["y"]):
        if rows and abs(e["y"] - rows[-1][0]["y"]) <= tol:
            rows[-1].append(e)
        else:
            rows.append([e])
    shift = 0.0
    for row in rows:
        top = min(e["y"] for e in row) + shift
        zone_h = max(e["h"] for e in row)
        if any(e.get("mode") == "Dropdown" for e in row):
            box = max(zone_h, SLICER_DROPDOWN_MIN_H)
        else:
            box = max(zone_h, ctrl_h)
        for e in row:
            e["y"] = round(top, 2)
            e["h"] = round(box, 2)
            e["x"] = round(e["x"] + pad_x, 2)
            e["w"] = round(max(40.0, e["w"] - 2.0 * pad_x), 2)
        grew = box - zone_h
        shift += grew + (gutter if grew > 0 else 0.0)


def _emit_dashboard_slicers(ws_list, page_name, model_table, field_map, filter_zones,
                            ref_w, ref_h, warnings=None):
    """Emit one slicer per dashboard filter *card*, at its own scaled position + show mode.

    Each ``filter_zones`` entry is a parsed ``<zone type-v2='filter'>`` (raw token + geometry +
    Tableau ``mode`` + ``hidden`` flag, from :func:`_parse_dashboard`). A card resolves to its slicer
    field via the same raw token the matching worksheet filter carries, so the slicer binds the real
    model column and lands at the card's authored grid position with the faithful dropdown/List mode.
    There is NO page-height cap, so a full top filter band is rebuilt instead of a five-deep
    right-rail stack silently truncated by a page guard.

    ``hidden-by-user`` is a Tableau SHOW/HIDE TOGGLE on a collapsible filter container, not a delete;
    Power BI has no Tier-1 collapse equivalent, so a toggled-hidden card is still surfaced (usable),
    never dropped -- a dashboard whose whole band is hidden still rebuilds its filters. Cards whose
    token resolves to no raw column (a calc/date control) are skipped (miss-over-wrong); binding
    those to their model objects is a separate parity step, not a fabricated raw-column slicer.
    Distinct model columns are de-duplicated so a field carded twice (e.g. one card per sheet in the
    band) yields a single slicer."""
    visuals = []
    by_token = _filter_fields_by_token(ws_list)
    seen = set()
    entries = []
    for i, fz in enumerate(filter_zones):
        f = by_token.get(tuple(fz.get("token") or ()))
        if f is None:
            continue
        key = (f["entity"], f["property"])
        if key in seen:
            continue
        seen.add(key)
        x, y, w, h = _scale_zone(fz, ref_w, ref_h)
        entries.append({"x": x, "y": y, "w": w, "h": h,
                        "mode": _tableau_filter_mode_to_pbi(fz.get("mode")),
                        "f": f, "i": i})
    # Reproduce Tableau's inter-card gaps: inset each slicer inside its (tangent) zone as a uniform
    # centered control so neighbouring rows/columns no longer collide (see _layout_slicers).
    _layout_slicers(entries)
    for e in entries:
        vname = _sanitize(f"slicer-{page_name}-{e['i']}-{e['f']['property']}")
        visuals.append(_slicer_json(
            vname, e["f"], _position(e["x"], e["y"], e["w"], e["h"], z=1, tab=100 + e["i"]),
            model_table, field_map, mode=e["mode"], warnings=warnings))
    return visuals


def _emit_slicers(ws_list, page_name, model_table, field_map, warnings=None, shown_tokens=None,
                  filter_zones=None, ref_w=None, ref_h=None):
    """Emit the page's filter slicers.

    On a dashboard page ``filter_zones`` carries the parsed filter *cards* (geometry + Tableau
    ``mode`` + ``hidden`` flag); each is placed faithfully at its own scaled zone with the right
    dropdown/List mode and no page-height cap (see :func:`_emit_dashboard_slicers`). The standalone
    worksheet-page surface has no dashboard card geometry, so ``filter_zones`` is ``None``/empty
    there and the original synthetic right-rail stack is kept byte-for-byte (``shown_tokens`` gate
    unchanged)."""
    if filter_zones:
        return _emit_dashboard_slicers(
            ws_list, page_name, model_table, field_map, filter_zones, ref_w, ref_h, warnings)
    visuals = []
    fields = _filter_slicer_fields(ws_list, shown_tokens)
    for i, f in enumerate(fields):
        y = 40 + i * 120
        if y > PAGE_HEIGHT - 120:
            break
        vname = _sanitize(f"slicer-{page_name}-{i}-{f['property']}")
        visuals.append(_slicer_json(
            vname, f, _position(PAGE_WIDTH - 220, y, 200, 100, z=1, tab=100 + i),
            model_table, field_map, warnings=warnings))
    return visuals


def _emit_param_control_slicers(controls, db_name, page_name, ref_w, ref_h, warnings):
    """Emit a single-select slicer for each model-resolved dashboard parameter control.

    A parameter control whose target column the model build resolved (``rec["resolved"]`` attached by
    :func:`_resolve_parameter_controls`) is rebuilt as an ordinary single-select slicer placed at the
    control's own dashboard zone (scaled with the same frame as the worksheet zones). The binding is
    already the authoritative model ``table[column]`` -- emitted directly (``model_table`` / ``field_map``
    are not re-applied) so it never double-resolves. Unresolved controls keep their warning (emitted in
    :func:`_resolve_parameter_controls`) and are skipped here, so this only ever ADDS a faithful slicer.
    """
    visuals = []
    for i, pc in enumerate(controls):
        if pc.get("dashboard") != db_name:
            continue
        res = pc.get("resolved")
        if not res:
            continue
        pos = pc.get("position") or {}
        if None in (pos.get("x"), pos.get("y"), pos.get("w"), pos.get("h")):
            continue
        x, y, w, h = _scale_zone(pos, ref_w, ref_h)
        field = {"entity": res["table"], "property": res["column"], "binding": "column",
                 "caption": res.get("caption") or res["column"], "aggregation": None,
                 "selection": None, "range": None, "datatype": None}
        vname = _sanitize(f"paramslicer-{page_name}-{i}-{res['column']}")
        visuals.append(_slicer_json(
            vname, field, _position(x, y, w, h, z=1, tab=200 + i),
            None, None, warnings=warnings))
    return visuals


def _reflow_worksheets_below_slicers(visuals, page_h, *, gap=8.0, tol=1.0):
    """Reproduce Tableau's SHOWN-state reflow when surfaced slicers collide with worksheet content.

    On a dashboard whose filter band is ``hidden-by-user`` (collapsed behind the funnel icon), Tableau
    reflows the sheets UP to fill the freed space, so the authored zone coords put the sheets where the
    filters would be. We choose to SHOW those filters as slicers (Power BI has no collapse toggle), which
    reintroduces the band -- so a sheet authored at the hidden-state position now overlaps the slicers
    (the ATTI card at y=241 under a filter band at y~211-320). This mirrors what Tableau itself does the
    moment you click "Show Filters": the sheet stack is pushed BELOW the band and compressed to fit the
    remaining canvas (ATTI -> y~351, h~285). We reflow the ``z==0`` worksheet visuals into
    ``[band_bottom+gap, page_h]`` proportionally, keeping their relative layout.

    Guard: only fires when a worksheet visual actually intersects the slicer band -- a dashboard whose
    slicers sit in their own clear band (no overlap) is untouched (never-regress). Slicers (``z==1``) and
    the banner (``z==1000``) are never moved; only worksheet content is reflowed."""
    slicers = [v for v in visuals if (v.get("position") or {}).get("z") == 1]
    content = [v for v in visuals if (v.get("position") or {}).get("z") == 0]
    if not slicers or not content:
        return
    band_top = min(v["position"]["y"] for v in slicers)
    band_bottom = max(v["position"]["y"] + v["position"]["height"] for v in slicers)
    intersect = [v for v in content
                 if v["position"]["y"] < band_bottom - tol
                 and v["position"]["y"] + v["position"]["height"] > band_top + tol]
    if not intersect:
        return
    # Move every sheet at or below the band start (content strictly ABOVE the band -- e.g. a header
    # sheet -- stays put). Compress the [orig_top, page_h] span into [new_top, page_h].
    movable = [v for v in content
               if v["position"]["y"] + v["position"]["height"] > band_top + tol]
    orig_top = min(v["position"]["y"] for v in movable)
    new_top = band_bottom + gap
    avail = page_h - new_top
    span = page_h - orig_top
    if avail <= 0 or span <= 0:
        return
    scale = avail / span
    for v in movable:
        p = v["position"]
        p["y"] = round(new_top + (p["y"] - orig_top) * scale, 2)
        p["height"] = round(p["height"] * scale, 2)


def migrate_twb_to_pbir(xml_text, *, dataset_name="Model", report_name="Report",
                        model_table=None, field_map=None, date_binding=None,
                        row_count_binding=None, measure_binding=None, column_binding=None,
                        param_binding=None, resources=None):
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

    ``column_binding`` (optional) carries the model build's calc-DIMENSION manifest -- a
    ``{"columns": {<calc name>: {"table", "column"}}}`` map (a flat ``{name: entry}`` is also
    accepted) naming the REAL model table + column each Tableau calc *dimension* was materialised
    into (read back from the built model TMDL by the estate orchestrator). When given, a calc
    dimension on an axis binds to that model column and lands in the category well; without it the
    calc dimension still resolves as a category (via a caption fallback + warning), never a measure
    -- so a crosstab whose Rows/Columns are calc dimensions rebuilds as a matrix, not a card.

    ``param_binding`` (optional) carries the model build's resolved parameter targets --
    ``{"slicers": {<param id>: {"table", "column", "single_select", "caption"}},
    "flags": {<token>: {"entity", "measure", "value", "visuals"}}}``.
    When given, a dashboard parameter control whose target column the model identified is rebuilt as a
    single-select slicer at the control's own dashboard zone (the standing "not rebuilt as a slicer
    yet" warning is then cleared for that control). Controls the model did not resolve keep their
    warning; without the binding every control degrades-and-warns unchanged (warn-never-wrong).
    ``flags`` carries model keep-flag measures (a translated parameter-driven keep calc): each named
    worksheet's rebuilt visual gets a visual-level ``[measure] == value`` filter so it opens windowed,
    and the obsolete "aggregate/measure filter on '<token>'" warning is cleared for it; a flag with no
    worksheet scope, an unknown worksheet, or a non-numeric value is left unapplied and warned.
    """
    ir = parse_twb(xml_text, date_binding=date_binding, row_count_binding=row_count_binding,
                   measure_binding=measure_binding, column_binding=column_binding,
                   param_binding=param_binding)
    # Recover the workbook's view-only quick table calcs (the quick token is stripped off the
    # resolved value pill, so the addressing facts live only here) and hand them to the emitter, which
    # projects each as a Power BI Visual Calculation. Fail-open: a parse hiccup never blocks the rest
    # of the report emission.
    table_calc_usages = None
    if extract_table_calc_usages is not None:
        try:
            # Normalize exactly as ``parse_twb`` does (``.twb`` files carry a UTF-8 BOM) so the
            # usage extraction never trips on a byte string or a leading BOM.
            norm = (xml_text.decode("utf-8-sig") if isinstance(xml_text, bytes)
                    else xml_text.lstrip("\ufeff"))
            table_calc_usages = extract_table_calc_usages(norm)
        except Exception:
            table_calc_usages = None
    parts = emit_pbir(ir, dataset_name=dataset_name, report_name=report_name,
                      model_table=model_table, field_map=field_map,
                      table_calc_usages=table_calc_usages, resources=resources)
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
