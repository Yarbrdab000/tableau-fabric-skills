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

import copy
import decimal
import hashlib
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime

try:  # package or scripts-on-path (mirrors the other cores)
    from .tmdl_generate import clean_col, tableau_default_format_to_pbi
except ImportError:
    from tmdl_generate import clean_col, tableau_default_format_to_pbi

# View-only Quick Table Calc -> Power BI Visual Calculation (additive; report-layer counterpart to
# the model measure path). These three cooperate: ``extract_table_calc_usages`` recovers the quick
# pill's addressing facts, ``usage_to_visual_calc_spec`` normalizes them to a view-layer IR, and
# ``emit_visual_calc`` renders the Visual-Calculation DAX. The wiring below projects the base measure
# + the VC into the visual's ``queryState`` (see ``_apply_visual_calcs``). Import is optional-safe so
# a partial checkout still emits everything else.
try:
    from .workbook_table_calcs import extract_table_calc_usages
    from .visual_calc_spec import (usage_to_visual_calc_spec, resolve_addressing,
                                   FAMILY_PERCENT_OF_TOTAL)
    from .visual_calc_emitter import emit_visual_calc
    from .formula_table_calc_to_visual_calc import (compile_chain as compile_formula_chain,
                                                    formula_is_table_calc,
                                                    rename_calc_references)
except ImportError:  # pragma: no cover - flat scripts-on-path
    try:
        from workbook_table_calcs import extract_table_calc_usages
        from visual_calc_spec import (usage_to_visual_calc_spec, resolve_addressing,
                                       FAMILY_PERCENT_OF_TOTAL)
        from visual_calc_emitter import emit_visual_calc
        from formula_table_calc_to_visual_calc import (compile_chain as compile_formula_chain,
                                                       formula_is_table_calc,
                                                       rename_calc_references)
    except ImportError:
        extract_table_calc_usages = None
        usage_to_visual_calc_spec = None
        resolve_addressing = None
        FAMILY_PERCENT_OF_TOTAL = None
        emit_visual_calc = None
        compile_formula_chain = None
        rename_calc_references = None
        formula_is_table_calc = None

# Report-layer formatting emit builders (additive; PBIR analytics/format objects grounded on the
# Power BI formatting inventory). Optional-safe so a partial checkout still emits everything else.
try:
    from . import report_formatting
except ImportError:  # pragma: no cover - flat scripts-on-path
    try:
        import report_formatting
    except ImportError:
        report_formatting = None

# Deterministic remediation worklist (additive; folds warnings + candidate_records into a structured
# per-visual audit). Optional-safe so the engine still runs standalone if the module is absent.
try:
    from .remediation_worklist import build_worklist as _build_worklist
except ImportError:  # pragma: no cover - flat scripts-on-path
    try:
        from remediation_worklist import build_worklist as _build_worklist
    except ImportError:
        _build_worklist = None

# Opt-in Tier-3 dashboard audit request builder (additive; folds the worklist + viz advisor into a
# full-dashboard, priority-ordered audit for the assisted tier). Built only on demand from the CLI's
# ``--audit`` flag -- NOT on the default migrate path -- so a standalone run does no extra work.
try:
    from .audit_tier import build_dashboard_audit as _build_dashboard_audit
except ImportError:  # pragma: no cover - flat scripts-on-path
    try:
        from audit_tier import build_dashboard_audit as _build_dashboard_audit
    except ImportError:
        _build_dashboard_audit = None

# Zone Geometry v3 layout solver (opt-in ``--layout solver``). Optional so the emitter still imports
# and runs the legacy engine unchanged if the solver stack is absent.
try:
    from . import layout_plan as _layout_plan
except ImportError:  # pragma: no cover - flat scripts-on-path
    try:
        import layout_plan as _layout_plan
    except ImportError:
        _layout_plan = None


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
# -- layout engine (Zone Geometry v3, frame track slice 4d) ----------------------
# ``legacy`` scales each zone's normalized source rect straight into the page and repairs collisions
# afterwards; ``solver`` resolves the dashboard's zone TREE first (``layout_plan``), so tiled siblings
# receive disjoint intervals and overlap is unrepresentable rather than repaired. ``solver`` is the
# DEFAULT: it resolves every zone on the corpus (no zone falls through), roughly halves the residual
# collisions, and does not inflate the canvas -- most pages keep their exact legacy dimensions and the
# two that change get SHORTER, landing on their authored size. ``legacy`` is retained for two reasons,
# not as a rival engine: it is the per-zone FALLBACK inside ``_scale_zone`` for any zone the plan does
# not name (fail-closed, never half-solved), and it remains selectable as an escape hatch.
# ``_LAYOUT_PLAN`` is the plan for the dashboard currently being emitted -- set and reset around each
# page exactly like the page overrides above -- and is the ONE thing ``_scale_zone`` consults, which
# is why every emitted item records its ``zone_id`` (slice 4a).
LAYOUT_ENGINES = ("legacy", "solver")
LAYOUT_DEFAULT = "solver"
_LAYOUT_PLAN = None
# Authored-px -> emitted-px scale for zone PADDING. Tableau stores a zone's margins in real pixels
# while its rect is in normalized 0..100000 units, so the two need different scales; this is set per
# dashboard beside the page overrides and reset with them.
_ZONE_PAD_SCALE = (1.0, 1.0)
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
# absolute floor (SLICER_DROPDOWN_MIN_H) guarantees a degenerate tiny card still renders its control.
#
# The floor is 57px: the height Tableau itself authors for a filter card, and the height a Power BI
# dropdown demonstrably needs AT THE FACE THIS EMITTER STAMPS. Render-verified 2026-08-25 on the
# Salesforce NPSP "Staff Capacity" band -- every card showed its full label AND its selector, with no
# clipping and no dead space.
#
# It was 76.0, and that number is the arithmetic of Power BI's DEFAULT (~12pt) chrome: header 28 +
# selector 32 + padding 8/8. That was correct when it was measured (issue #100: 16 slicers between
# 45px and 64px, every one clipped) -- and it was measured BEFORE this emitter began stamping the
# source's point size, which is the other half of that same fix. A floor calibrated against the
# larger face outlived the reason for it, and then overrode the authored size on every dashboard:
# a 57px Tableau card was silently grown to 76px, i.e. a third taller than the author drew it.
#
# WHERE THE STAMP ACTUALLY LIVES, because this cost a wrong release (#180, reverted at 2.344.0).
# It is emitted as ``textSize`` on the slicer's ``header`` and ``items`` wells -- NOT ``fontSize``,
# and NOT via the ``SLICER_FONT_PT`` constant below, which is genuinely unreferenced. Measured on the
# 34-workbook corpus: **94 of 94 slicers carry a header AND items textSize**, and the value is the
# AUTHORED size rather than a fixed 9 (``9D`` x182, ``8D`` x4, ``11D`` x2). A probe searching for
# ``fontSize`` finds zero and reads as proof that nothing is stamped; that probe shipped a revert of
# this line, which regressed every dropdown card in the corpus by 19px.
#
# EXPECT ``powerbi-report-author validate`` TO FLAG THIS as PBIR_SLICER_HEIGHT_BELOW_FLOOR
# ("57px < 76px minimum (header 28 + selector 32 + padding 8/8)"). That rule computes the DEFAULT
# face's chrome and cannot see the stamped size, so on our output it is a known false positive. It is
# the one place we knowingly fail a first-party rule, and the reason is that the render is the
# authority over the rule: satisfying it costs a third of the authored card height on every
# dashboard. If that trade is ever revisited, re-render -- do not re-argue it from the validator.
#
# Keep a floor -- a degenerate tiny card still has to render its control -- but set it to the
# smallest height actually shown to work at the font we emit, not to the chrome of a font we do not.
SLICER_DROPDOWN_MIN_H = 57.0
SLICER_PAD_X = 7.0
SLICER_ROW_GUTTER = 8.0
# UNREFERENCED. The 9pt intent it records is really emitted as ``textSize`` at the slicer build
# sites, from the AUTHORED point size. Kept only because its name has twice been read as evidence
# about whether a font is stamped -- it is not evidence either way. See SLICER_DROPDOWN_MIN_H.
SLICER_FONT_PT = 9.0


def _whole_px(v):
    """Round a canvas dimension UP to a whole pixel.

    A Power BI page is measured in integral pixels: a fractional page height emitted verbatim
    ("height": 830.06) makes the Desktop layout parser reject the ENTIRE report -- "Input string
    '830.06' is not a valid integer" -- which also blocks render verification. Every producer of the
    emitted page (authored <size>, the solver plan, and caption band-insertion growth) runs through
    here. Rounds UP, never down, so a page can never end up fractionally SHORTER than the content
    that was placed on it.
    """
    return float(math.ceil(float(v)))


def _page_w():
    """Active page width: the per-dashboard real <size> width override, else the default."""
    return _PAGE_W_OVERRIDE or PAGE_WIDTH


def _page_h():
    """Active page height: the per-dashboard aspect-faithful override, else the default."""
    return _PAGE_H_OVERRIDE or PAGE_HEIGHT


def _dash_page_dims(size):
    """The emitted page ``(w, h)`` for a parsed dashboard ``size`` dict.

    The single definition of the dashboard canvas: a fixed ``<size maxwidth/maxheight>`` wins, else
    the automatic fit-to-window canvas derived from the declared minimum, else Tableau's own default.
    Extracted so the layout PLAN is solved against exactly the page the emit path will use -- solving
    against a different page silently invalidates every rect it produces.
    """
    auto_w, auto_h = _automatic_canvas_dims(size.get("min_w"), size.get("min_h"))
    return (_whole_px(size.get("w") or auto_w or DASH_DEFAULT_W),
            _whole_px(size.get("h") or auto_h or DASH_DEFAULT_H))


def _automatic_canvas_dims(min_w, min_h):
    """Canvas ``(w, h)`` for an "automatic"-sized Tableau dashboard -- one that declares only a
    MINIMUM (minwidth/minheight) and no fixed maxwidth/maxheight. Tableau renders such a dashboard
    fit-to-window, almost always LARGER than its authored minimum, so emitting the raw min would
    under-size the page (a 1000x620 design would look cramped). We keep the authored ASPECT exactly
    and scale it UP with a MAX-cover factor so the page covers at least the standard 1280x720 screen
    frame in BOTH axes (never scaled down): ``k = max(1.0, PAGE_WIDTH/min_w, PAGE_HEIGHT/min_h)``.
    Only the absolute size grows; the aspect ratio (hence every scaled zone's shape) is untouched.
    Returns ``(None, None)`` when there is no usable minimum on both axes."""
    if not min_w or not min_h or min_w <= 0 or min_h <= 0:
        return None, None
    k = max(1.0, PAGE_WIDTH / float(min_w), PAGE_HEIGHT / float(min_h))
    return round(min_w * k), round(min_h * k)


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
VT_DENSITY_MAP = "density_map"  # azureMap heatMapLayer (Tableau's Density/Heatmap mark over a geography)
VT_COMBO = "combo"        # lineClusteredColumnComboChart (column measure(s) on Y + line measure(s) on Y2)
VT_WATERFALL = "waterfall"  # waterfallChart (running-total Gantt hack: dimension Category + base measure Y)
VT_DONUT = "donut"          # donutChart (dual-axis pie/donut hack: legend Category + angle measure Y)
VT_RIBBON = "ribbon"        # ribbonChart (bump/rank hack: ordinal Category + legend Series + base measure Y)
VT_TREEMAP = "treemap"      # treemap (marks-card dimension tiled by a measure; no axis pills)
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
    # ALL THREE Tableau map shapes migrate to ``azureMap``. Microsoft deprecates the Bing-backed
    # ``map`` and ``filledMap`` (Desktop now shows a modal "Bing map visuals are going away"), and
    # ``shapeMap`` -- which the choropleth path used to take -- was measured rendering COMPLETELY
    # BLANK: a 4-visual control page proved azureMap draws bubbles and a data-bound referenceLayer
    # choropleth on the same machine and the same data where a byte-identical shapeMap drew nothing.
    # So this is not merely a deprecation swap; shapeMap was emitting an empty visual (issues #106,
    # #112). The three constants are kept distinct because they still select DIFFERENT azureMap
    # layers and query-state shapes (choropleth vs symbol vs categorical legend).
    VT_FILLED_MAP: "azureMap",
    VT_MAP: "azureMap",
    VT_SHAPE_MAP: "azureMap",
    # Tableau's Density / Heatmap mark over a geography. It used to defer to VT_UNSUPPORTED -- "no
    # offline home" -- and the whole worksheet produced NO PAGE AT ALL (issue #112). azureMap has a
    # native ``heatMapLayer`` that is exactly this mark, so the worksheet is now rebuilt instead of
    # dropped.
    VT_DENSITY_MAP: "azureMap",
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
    # Marks-card dimension tiled by a measure (Automatic/Square mark, no axis pills) -> native
    # treemap. Roles Group (category) + Values (size measure) + optional Details (extra categories);
    # a continuous colour measure shades the tiles via the chart continuous-fill path.
    VT_TREEMAP: "treemap",
}

# Mark classes that, when two measures on one shelf carry DIFFERENT mark families, signal a
# dual-axis combo: a bar/column-family measure overlaid with a line/area-family measure. (Area is
# treated as line-family, consistent with the area->line default elsewhere in this module.)
_COLUMN_FAMILY_MARKS = {"bar", "gantt"}
_LINE_FAMILY_MARKS = {"line", "area"}

# Mark classes for geometry-backed / custom-spatial maps we deliberately defer (basics only:
# filled + symbol map). These degrade to a structured warning rather than a guessed visual.
# Split (v2-6): density/heatmap have no faithful offline Power BI home in any case -> always defer.
# A Multipolygon/Polygon FILL, by contrast, is how Tableau renders a *standard geography* (a
# recognized geo-role dimension with generated lat/lon) -- an ordinary choropleth whose faithful
# home is a shapeMap, recovered in _visual_type() when a spatial signal confirms it. Only a polygon
# WITHOUT that confirmation is a truly-custom/arbitrary polygon (no built-in topology) that still
# defers. The union preserves the location-only + caller-warning semantics unchanged.
_DENSITY_MAP_MARKS = {"density", "heatmap"}
_POLYGON_MAP_MARKS = {"multipolygon", "polygon"}
_DEFER_MAP_MARKS = _DENSITY_MAP_MARKS | _POLYGON_MAP_MARKS

# Tableau derivation -> Power BI QueryAggregateFunction code.
_AGG_FUNC = {
    "Sum": 0, "Avg": 1, "Average": 1, "CntD": 2, "CountD": 2,
    "Min": 3, "Max": 4, "Count": 5, "Cnt": 5, "Median": 6,
}
# Aggregations restricted to numeric source columns (others -> warn + skip).
_NUMERIC_AGGS = {"Sum", "Avg", "Average", "Median"}
# sf-npo Lesson 8: aggregations that collapse a symbol map's bubble sizes toward uniformity. Sizing
# bubbles by an AVERAGE gives every location a near-identical radius (each place averages to a similar
# value), so the map reads as undifferentiated dots -- a count/sum measure is what makes it legible.
_AVERAGE_AGGS = {"Avg", "Average"}
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
# artifact.) Verified against the real Sample Hierarchy workbook, whose FiscalMonth filter card
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

# The subset of shared Date-dimension columns that are INTEGER-valued AND whose Tableau date-part
# member integer equals the DAX function output verbatim -- Year (YEAR), Quarter (QUARTER), Month
# (MONTH), Day (DAY). For these, an applied date-part filter selection (e.g. keep Month in {4}) can be
# re-emitted faithfully as an integer categorical filter on the calendar column once the field has
# been rebound to it. "Week of Year" (Tableau week numbering can diverge from DAX WEEKNUM), "Day Name"
# (a string column, not the weekday integer), and "ISO Year" are deliberately excluded -- their
# applied selections stay at the slicer's "show all" default with a fidelity note (warn-never-wrong).
_INTEGER_DATE_PART_COLUMNS = frozenset({"Year", "Quarter", "Month", "Day"})

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
# The scalar Date-table column that carries each CONTINUOUS truncation grain (see
# ``generate_date_table_tmdl``). Day-Trunc is the key column itself, so it maps to ``None`` and the
# caller substitutes the marked key column.
_DATE_TRUNC_SCALAR_COLUMNS = {
    "Year": "Year Start",
    "Quarter": "Quarter Start",
    "Month": "Month Start",
    "Week": "Week Start",
    "Day": None,
}


def _is_continuous_pill(field):
    """True when a Tableau shelf pill is CONTINUOUS (green), read from Tableau's own encoding.

    Tableau stamps continuity on the pill INSTANCE, as the trailing role code: ``:qk`` is
    quantitative/continuous, ``:ok`` (ordinal) and ``:nk`` (nominal) are discrete. The workbook that
    exposed this carries both spellings of the same truncated month --
    ``[tmn:Order Date:qk]`` and ``[tmn:Order Date:ok]`` -- so the derivation alone (``Month-Trunc``
    for both) cannot tell them apart; only this suffix can.

    Deliberately conservative: anything that is not explicitly ``qk`` (including a synthesized
    caption-fallback instance with no role code) reads as DISCRETE, which is Power BI's own default,
    so an unrecognised pill keeps the previous behaviour rather than being guessed onto a scalar
    axis it cannot support.
    """
    return str(field.get("instance") or "").rsplit(":", 1)[-1].strip().lower() == "qk"


def _norm_date_col(name):
    """Normalize a column name for active-date matching (case/space/underscore-insensitive)."""
    return re.sub(r"\s+", " ", (name or "").strip().lower().replace("_", " ").replace("-", " "))


def _rebind_date_axis(field, deriv, date_binding, for_filter=False):
    """Redirect a date axis pill to the model's shared Date table, or ``None`` to leave it as-is.

    ``for_filter`` marks a pill that becomes a FILTER CARD rather than an axis, and it declines the
    exact/plain-date rebind. A Tableau date filter card enumerates the DISTINCT VALUES PRESENT IN
    THE FACT COLUMN -- a discrete ``FiscalMonth`` dimension offers the three or four month stamps
    the data actually contains. The shared calendar is a generated contiguous range, so rebinding a
    filter to ``Date[Date]`` replaces that short authored domain with EVERY day it spans: the
    control lists 1/1/2026, 1/2/2026, ... instead of 4/21, 5/21, 6/21, and the authored selection
    (a fact-column member) is no longer provably inside the bound column's domain, so the
    preselection gate declines and the slicer opens on "All" -- leaving the page UNFILTERED and the
    numbers silently wrong. Measured on a customer workbook: a 3-member Fiscal Month card rebuilt as
    a 365-entry day picker with no selection.

    Date PARTS still rebind on a filter, deliberately and with evidence: a Year filter binds to the
    calendar's INTEGER ``Date[Year]`` column, whose domain is small and whose members match the
    integer-year literals Tableau writes (binding those to a datetime column matches nothing and
    silently empties the visual). Parts narrow the domain; the exact-date key column explodes it.

    Fires ONLY for the single ACTIVE business date the model build selected, so a secondary or
    inactive date (e.g. Ship Date, or any date when the primary is ambiguous) is never bound to the
    calendar and therefore can't silently display the active date's values -- the exact "break a lot
    of stuff" risk. A discrete date PART rebinds to its calendar column (Year -> Date[Year]); a plain
    exact/continuous date, OR a discrete exact-date VALUE (e.g. MDY -- the full date shown as "Month,
    Day, Year"), rebinds to the marked key column (Date[Date]); a day-or-coarser TRUNCATION
    (Day/Week/Month/Quarter/Year-Trunc) rebinds to the Date table's scalar GRAIN column
    (Month-Trunc -> Date[Month Start]) because DATETRUNC yields one date value per period, not a
    drill. A SUB-DAY truncation (Hour/Minute/Second-Trunc) can't be represented by a
    day-grain calendar, and any part with no calendar column, return ``None`` (deferred -- the caller
    keeps the source column + warns). Returns a rebind dict -- ``{"entity","property"}`` for a column
    or ``{"entity","hierarchy","levels"}`` for the drill hierarchy -- else ``None``.
    """
    if not date_binding or field.get("role") == "measure":
        return None
    # PER-ISLAND MODELS: several datasources -> one calendar each. A resolved field carries its own
    # ``datasource`` (the island's caption), which is the one identifier the model build and the
    # report share -- so a pill binds to ITS OWN island's calendar. Selecting the island first also
    # repairs the name-based gates below: ``active_keys`` / ``ambiguous_keys`` become that island's,
    # so a column name active in one island and not another stops contesting itself workbook-wide.
    #
    # Keyed on the datasource, NOT on the field's ``entity``: the same relation name
    # (``pmdm__ProgramEngagement__c``) exists in all four Salesforce islands, so an entity key
    # collides and picking one bound an Intake pill to the Service Delivery calendar -- a
    # cross-island rebind onto a calendar with no active join, which is the flat series this split
    # exists to remove.
    #
    # DECLINES when the pill's datasource names no island: binding it to an arbitrary calendar is
    # exactly that defect. Falls through untouched for single-calendar models, so a
    # single-datasource workbook is byte-for-byte unchanged.
    by_island = date_binding.get("by_island")
    if by_island:
        scoped = by_island.get((field.get("datasource") or "").strip().lower())
        if not scoped:
            return None
        date_binding = scoped
    table = date_binding.get("date_table")
    if not table:
        return None
    # A date column name is safe to rebind only when it is ACTIVE everywhere it appears. Matching on
    # the name alone is a silent correctness bug: a fact with NO active date relationship still
    # rebinds its axis to the calendar as soon as some OTHER table's active date shares the column
    # name (``CreatedDate`` is near-universal). The calendar then cannot filter that fact at all, so
    # every bucket returns the grand total and the time series renders as a FLAT line / solid block --
    # confidently wrong, with no warning.
    #
    # The gate is expressed on the COLUMN NAME rather than on ``(table, column)`` deliberately: here
    # the field's ``entity`` is still the WORKBOOK's relation name (a Tableau extract emits
    # ``Orders_ECFCA1FB690A41FE803BC071773BA862``), not the model's table display name, so a pair
    # comparison could never hold. ``ambiguous_keys`` names the date columns that are active on one
    # table and NOT active on another that also carries them -- precisely the case a name match
    # cannot disambiguate -- and those decline. Additive: a caller that supplies no
    # ``ambiguous_keys`` keeps the previous behaviour byte-for-byte.
    active = {_norm_date_col(c) for c in (date_binding.get("active_keys") or ())}
    prop = _norm_date_col(field.get("property"))
    if prop not in active:
        return None
    if prop in {_norm_date_col(c) for c in (date_binding.get("ambiguous_keys") or ())}:
        return None
    if deriv in _DATE_PARTS:
        grains = date_binding.get("grain_columns") or _DEFAULT_DATE_GRAIN_COLUMNS
        col = grains.get(deriv)
        return {"entity": table, "property": col} if col else None
    if deriv in ("None", "", None) or deriv in _DATE_EXACT_DERIVATIONS:
        if for_filter:
            # A filter card keeps the fact's own column so the control offers exactly the members
            # the data holds (and the authored selection stays inside the bound domain).
            return None
        # plain / continuous exact date, or a discrete exact-date VALUE (e.g. MDY = the full date
        # shown as "Month, Day, Year") -> the marked calendar key column. Both are the same
        # underlying date, so the exact-date-value display format binds exactly like a plain date.
        return {"entity": table, "property": date_binding.get("key_column") or "Date"}
    # A DAY-or-coarser date TRUNCATION (Day/Week/Month/Quarter/Year-Trunc) is DATETRUNC: ONE DATE
    # VALUE per period. That is a scalar date column in the model, so it binds to the Date table's
    # grain column (Month-Trunc -> ``Date[Month Start]``) whatever colour the pill is -- a truncation
    # is a single flat series of period stamps, NOT a drill. Binding it to the Calendar hierarchy
    # instead was measured to be wrong in three separate ways: it builds a Year x Month CROSS-PRODUCT
    # of category slots, so Power BI refuses a Scalar axis and PAGES the surplus behind a scrollbar
    # (45-month series -> 21 shown, 24 silently hidden); it renders nested Year/Month headers Tableau
    # never shows for a single truncation pill; and it leaves a running-total window ordering by a
    # column that is not on the axis. The Calendar hierarchy remains correct for date PARTS, which
    # are handled above -- those really are drill levels.
    #
    # The pill's CONTINUITY decides only how the axis is DRAWN, not what it binds to: a green ``:qk``
    # pill is a continuous number line (``axisType: Scalar``, applied downstream from
    # ``continuous_axis``), a blue ``:ok`` pill is the same members drawn as discrete headers.
    #
    # A SUB-DAY truncation (Hour/Minute/Second-Trunc) has no day-grain calendar column, so it stays
    # deferred (caller keeps the source column + warns; warn-never-wrong).
    m = re.match(r"(Year|Quarter|Month|Week|Day)-Trunc$", str(deriv or ""))
    if m:
        grain = m.group(1)
        if grain in _DATE_TRUNC_SCALAR_COLUMNS:
            col = _DATE_TRUNC_SCALAR_COLUMNS[grain]
            if col:
                return {"entity": table, "property": col}
            # Day-Trunc IS the key column.
            return {"entity": table, "property": date_binding.get("key_column") or "Date"}
        levels = _DATE_TRUNC_HIERARCHY_LEVELS.get(grain)
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
            if rtype in ("join", "union", "batch-union", "collection"):
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
                "number_format": c.get("default-format"),
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


def _row_count_column_target(rc, row_count_binding):
    """Resolve the ``(entity, column)`` constant COLUMN to bind an implicit row count to, or ``None``.

    The model may land Tableau's row-level constant (the stock ``Number of Records`` field, or any
    literal calc) as a calculated COLUMN of 1s with ``summarizeBy: sum`` rather than as a COUNTROWS
    measure -- which is the more faithful shape, because aggregating it on the shelf reproduces
    every Tableau aggregation (``SUM`` -> n*k, ``AVG`` -> k, ``CNT`` -> n) where a single COUNTROWS
    measure only reproduces the count. This is the column-side twin of
    :func:`_row_count_measure_target` and is consulted only AFTER it, so a model that supplies a
    real COUNTROWS measure binds exactly as before.

    ``object_id`` counts are deliberately excluded: they name a specific fact table and are answered
    by that fact's own COUNTROWS measure, never by a constant column that may live elsewhere.
    """
    if not row_count_binding or rc.get("kind") != "numrec":
        return None
    d = row_count_binding.get("default_column") or {}
    if d.get("entity") and d.get("column"):
        return (d["entity"], d["column"])
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
            "number_format": None,
        }
    col_target = _row_count_column_target(rc, row_count_binding)
    if col_target is not None:
        # The constant COLUMN carries the pill's OWN shelf aggregation (Tableau's implicit row-count
        # pill is ``SUM([Number of Records])``, but the same field is legitimately averaged or
        # counted), so the visual reproduces the source aggregation rather than being pinned to a
        # count. ``Sum`` is the default only because that is the aggregation Tableau applies when
        # the pill carries none.
        entity, column = col_target
        return {
            "caption": column, "field_id": base_id, "instance": field_id,
            "role": "measure", "datatype": "integer", "is_calc": False,
            "derivation": deriv, "aggregation": deriv if deriv in _AGG_FUNC else "Sum",
            "entity": entity, "property": column,
            "binding": "column", "kind": "value",
            "geo_area": None, "formula": None,
            "number_format": None,
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


def _island_binding_key(caption, island):
    """``"<caption> (<island>)"`` -- the key a per-island calc is emitted under, or ``None``.

    The model build (``migrate_estate._island_qualified_calc_name``) keeps a same-captioned calc
    from a SECOND datasource island under this name, so a pill can find its OWN island's version
    instead of silently binding to whichever island happened to be parsed first. Returns ``None``
    unless both halves are present, and the key simply misses on a model that emitted no
    island-qualified calc -- so single-island workbooks are byte-unchanged.
    """
    if not (caption and island):
        return None
    return f"{caption} ({island})"


def _measure_binding_candidate_keys(field_id, base_id, caption, worksheet, island=None):
    """Candidate lookup keys in deterministic join priority (token first, never fuzzy):
    island-qualified caption > pill instance token > bare calc id > ``worksheet|caption`` >
    caption. Mirrors the locked contract so a translated calc binds by its stable token even when
    captions collide.

    The island-qualified caption outranks the token because two islands' copies of one calc can
    share a Tableau internal name (a datasource duplicated inside the workbook), which makes the
    token AMBIGUOUS while the island is exact. It is emitted only when the model actually kept a
    per-island copy, so on every other workbook it misses and the historical order stands."""
    keys = []
    for k in (_island_binding_key(caption, island), field_id, base_id,
              (f"{worksheet}|{caption}" if worksheet and caption else None),
              caption):
        if k and k not in keys:
            keys.append(k)
    return keys


def _lookup_measure_binding(measure_binding, field_id, base_id, caption, worksheet, island=None):
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
    for key in _measure_binding_candidate_keys(field_id, base_id, caption, worksheet, island):
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


def _lookup_column_binding(column_binding, field_id, base_id, caption, worksheet, island=None):
    """Resolve a calc DIMENSION pill to its model ``(table, column)``, or ``None``.

    Tries candidate keys case-insensitively in a deterministic order (island-qualified caption,
    caption, trimmed caption, bare calc id, pill instance token) and returns the first
    model-confirmed hit; a miss (or no binding supplied) -> ``None`` so the caller degrades to the
    caption fallback + warns. A pure consumer of the model-built manifest -- never a fuzzy/guessed
    match.

    The island-qualified caption is tried FIRST so a pill from a second datasource island binds to
    its OWN version of a same-captioned calc (see ``_island_binding_key``). It misses on every
    model that kept only one copy, leaving the historical order byte-unchanged.
    """
    entries = _column_binding_entries(column_binding)
    if not entries:
        return None
    for key in (_island_binding_key(caption, island),
                caption, (caption or "").strip(), base_id, field_id):
        if not key:
            continue
        hit = entries.get(str(key).lower())
        if hit:
            return (hit["table"], hit["column"])
    return None


def dax_safe_measure_name(name):
    """The measure name the MODEL will emit for ``name`` -- kept identical to the model-side rule.

    The model strips DAX identifier brackets from a measure name, because Tableau names an unnamed
    calc after its own FORMULA and ``[``/``]`` delimit an identifier in DAX, so a measure called
    ``IF NOT ISNULL(SUM([Sales])) ...`` cannot be referenced by name at all. The REPORT must apply
    the same rule when it binds a calc by caption, or the two ends disagree and the reference names
    an object the model does not contain: measured after the model-side strip landed, a
    ``RUNNING_SUM`` measure reference was dropped by the report/model cross-check and its visual
    lost the Y measure entirely -- a silent fidelity loss, since a dropped projection neither errors
    nor fails validation.

    Deliberately a small duplicate of :func:`assemble_model.dax_safe_measure_name` rather than an
    import: the report layer does not depend on the model layer. ``test_report_and_model_agree_on_
    dax_safe_measure_names`` asserts the two implementations agree, so they cannot drift.
    """
    text = str(name or "")
    if "[" not in text and "]" not in text:
        return text
    cleaned = " ".join(text.replace("[", "").replace("]", "").split())
    return cleaned or text


def _lookup_scalar_row_aggregate(measure_binding, field_id, base_id, caption, worksheet,
                                 island, deriv):
    """The row-aggregated companion measure for this pill's own derivation, or ``None``.

    A Tableau calc built only from parameters and literals is row-level but constant across rows, so
    Tableau's ``SUM`` of it is ``n*k`` while the DAX measure returns ``k``. The model emits a
    ``SUMX``/``COUNTX`` companion for exactly the two derivations that differ; ``Avg``/``Min``/``Max``
    are the scalar itself and keep binding the base measure.

    This also un-collapses the shelf. Two pills over the same calc at different derivations used to
    resolve to one identical measure reference, so the second projection was de-duplicated away --
    measured on Superstore (issue #103), the ``OTE`` table lost Tableau's *Avg. OTE* column entirely
    and reported the remaining one at the wrong grain.
    """
    if not measure_binding or not deriv:
        return None
    entries = _measure_binding_entries(measure_binding)
    if not entries:
        return None
    for key in _measure_binding_candidate_keys(field_id, base_id, caption, worksheet, island):
        entry = entries.get(key)
        if not isinstance(entry, dict):
            continue
        alt = (entry.get("row_aggregates") or {}).get(deriv)
        if alt:
            return alt
    return None


def _resolve_field(ds, field_id, base_cols, instances, index, ds_caption,
                   worksheet, warnings, warn_special=True, internal_fields=None,
                   date_binding=None, row_count_binding=None, measure_binding=None,
                   column_binding=None, for_filter=False):
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
                                     _mb_base.get("caption"), worksheet,
                                     island=ds_caption.get(ds))
        if mb is not None:
            m_entity, m_measure = mb
            # A ROW-LEVEL SCALAR calc (parameter/literal arithmetic, no column reference) is constant
            # per row but still aggregated by Tableau on the shelf, so a Sum pill means n*k while the
            # measure returns k. Bind this pill's OWN derivation to the model's row-aggregated
            # companion when one exists; Avg/Min/Max are the scalar itself and keep the base measure.
            _alt = _lookup_scalar_row_aggregate(measure_binding, field_id, base_id,
                                                _mb_base.get("caption"), worksheet,
                                                ds_caption.get(ds), deriv)
            if _alt:
                m_measure = _alt
            # Did the model translate THIS PILL'S OWN table calc, or only the base field under it?
            # The lookup tries the pill instance token FIRST, then falls back to the bare calc id /
            # caption -- so re-running it with the instance token ALONE answers the question exactly.
            # A view-only quick table calc (running total, moving average, ...) is deliberately NOT
            # given a model measure: the model supplies only its base, and the transform is rebuilt in
            # the report layer as a Visual Calculation. Conflating the two made every such calc vanish
            # on the model-fact rebind pass (see ``_apply_visual_calcs``).
            rebound_to_instance = (
                field_id is not None
                and _lookup_measure_binding(measure_binding, field_id, None, None, None) is not None)
            return {
                "caption": _mb_base.get("caption") or m_measure,
                "field_id": base_id, "instance": field_id,
                "role": "measure",
                "datatype": tableau_type_to_simple(_mb_base.get("datatype")) or "integer",
                "is_calc": True, "derivation": deriv, "aggregation": None,
                "entity": m_entity, "property": m_measure,
                "binding": "measure", "kind": "value",
                "geo_area": None, "formula": _mb_base.get("formula"),
                "number_format": _tableau_number_format(_mb_base.get("number_format")),
                "measure_rebound": True,
                "rebound_to_instance": rebound_to_instance,
                # Carry the DISCRETENESS decision across the rebind. This branch builds a FRESH
                # field dict, so a flag stamped on the classifier's dict below would be lost -- and
                # it was: the first (viz-only) pass classified every boolean colour driver discrete,
                # then the second pass, once the model supplied ``measure_binding``, returned here
                # and silently dropped it, so only the calcs the model FAILED to translate kept the
                # discrete encoding. Same two signals as the classifier: a boolean datatype, or a
                # non-``:qk`` role code on the pill instance.
                "discrete_measure": (
                    (_mb_base.get("datatype") or "").strip().lower() == "boolean"
                    or not _is_continuous_pill({"instance": field_id})),
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
    calc_col = (_lookup_column_binding(column_binding, field_id, base_id, caption, worksheet,
                                       island=ds_caption.get(ds))
                if calc_is_axis else None)

    if bound:
        entity, prop = bound["entity"], bound["property"]
        if not datatype:
            datatype = bound["datatype"]
    elif calc_col is not None:
        entity, prop = calc_col
    elif is_calc and not calc_is_axis:
        entity, prop = MEASURES_TABLE, dax_safe_measure_name(caption)
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
        # The datasource (island) this pill came from. A caption is NOT a unique name when a
        # workbook consolidates several embedded datasources into one model, so ``_apply_override``
        # resolves "<datasource>||<caption>" before the bare caption.
        "datasource": ds_caption.get(ds, ds),
        "role": role, "datatype": datatype, "is_calc": is_calc,
        "derivation": deriv, "aggregation": None,
        "entity": entity, "property": prop,
        "binding": None, "kind": None,
        "geo_area": _geo_area(base.get("geo_role", "")) if role != "measure" else None,
        "formula": base.get("formula"),
        "number_format": _tableau_number_format(base.get("number_format")),
        # Did a MODEL get consulted for this pill at all? Set only when the model<->viz contract
        # supplied its calc->measure manifest, i.e. on the model-bound pass. Reaching here WITH the
        # manifest present means the model was asked about this calc and did not claim it (it
        # stubbed rather than translated). That is a different fact from "no model exists yet"
        # (the first, pre-rebind viz pass, and every direct ``parse_twb`` caller), where nothing can
        # be concluded -- so a consumer that must fail closed can tell the two apart instead of
        # deferring on both.
        "model_consulted": bool(measure_binding),
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
        # DISCRETENESS. Tableau states it twice on this pill and the engine used to consult neither,
        # unconditionally stamping every unbound calc measure CONTINUOUS. A discrete boolean measure
        # on Colour then flowed into the continuous-gradient path and Power BI evaluated MIN/MAX over
        # a BOOLEAN -- "The function 'Min' cannot be invoked with the specified arguments" -- so the
        # visual threw the moment the page rendered (measured on all 7 sheets of the reporting
        # workbook). The two signals:
        #   * the pill's own role code -- ``:qk`` is quantitative/continuous, ``:nk`` (nominal) and
        #     ``:ok`` (ordinal) are DISCRETE. ``_is_continuous_pill`` already reads exactly this.
        #   * a ``boolean`` datatype, which is a 2-value domain and cannot be a continuous ramp
        #     whatever the role code says.
        # Recorded as a flag rather than by flipping ``kind``: this IS still a measure (it must stay
        # in the value well and keep its per-mark aggregate grain -- that is the whole reason a
        # Legend column is the wrong answer), it simply drives a DISCRETE colour encoding.
        field["discrete_measure"] = (
            (datatype or "").strip().lower() == "boolean" or not _is_continuous_pill(field))
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
    rebind = _rebind_date_axis(field, deriv, date_binding, for_filter=for_filter)
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

    if deriv == "Attribute":
        # Tableau's ATTR([x]): the value when it is unique across the mark's rows, and the literal
        # ``*`` when it is not. Power BI has no such aggregate, and the previous behaviour was to
        # DROP the pill -- which is why 0135's ``ATTR`` sheet emitted a table with only its row
        # dimension and no value at all, and why its 3-pill trellis produced 2 charts instead of 3.
        #
        # ``Min`` is EXACT for the case the author is asking about. ATTR is written precisely when
        # the value is expected to be constant within the mark, and where it is, MIN(x) IS x. The two
        # differ only when the value is NOT unique -- Tableau prints ``*``, Power BI shows the
        # minimum -- so the degradation is confined to the case the source itself flags as ambiguous,
        # and it is warned rather than silent. Emitting the pill wrong-in-one-case beats dropping it
        # in every case: a missing value cannot be noticed by a reader, a minimum can.
        #
        # Not routed through the ``Min`` branch above: that one refuses non-numeric/non-date columns,
        # and ATTR is most often used on a STRING (``ATTR([Region])``), where a text Min is both
        # valid in PBIR and exactly the intended answer. Restricting it the same way would drop
        # precisely the commonest ATTR.
        warnings.append(_warn(
            "worksheet", worksheet,
            f"ATTR('{caption}') rebuilt as MIN -- identical wherever the value is unique within the "
            f"mark (which is what ATTR asserts); where it is not, Tableau shows '*' and this shows "
            f"the minimum"))
        field["aggregation"] = "Min"
        field["binding"] = "aggregation"
        field["kind"] = "value"
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
           "geo_levels": [], "detail_dims": [], "label_fields": []}
    if pane is None:
        return enc
    holder = _first(pane, "encodings")
    if holder is None:
        return enc
    mapping = {"color": "color", "size": "size", "text": "label",
               "label": "label", "lod": "detail", "level-of-detail": "detail",
               "wedge-size": "angle"}
    seen_detail_dims = set()
    seen_label_fields = set()
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
            # Tableau's Text/Label shelf can carry MANY pills that a rich mark-label template
            # arranges into one block -- a KPI "BAN" writes a static caption, a big number, and a
            # set of MUTUALLY EXCLUSIVE coloured delta measures (exactly one is non-blank in a
            # given month, its colour carrying the direction). ``enc["label"]`` keeps only the
            # FIRST, so such a card bound whichever pill Tableau happened to serialise first and
            # rendered "(Blank)" whenever that was not the live one. Retain EVERY label pill, in
            # template order, so the card can project them all and no slot is silently dropped.
            # Additive: ``enc["label"]`` is untouched, so every existing reader is unchanged.
            if role == "label":
                _lf_key = (f.get("caption"), f.get("field_id"), f.get("aggregation"))
                if _lf_key not in seen_label_fields:
                    seen_label_fields.add(_lf_key)
                    # Stamp the SOURCE TOKEN so the mark-label template (which addresses pills by
                    # that token, not by caption) can be matched back to this resolved field. The
                    # encodings shelf order and the template order are independent, and the two sets
                    # need not coincide, so positional pairing would be wrong.
                    f["label_token"] = (ds or ds_default, fid)
                    enc["label_fields"].append(f)
            # Retain ALL geo-role Detail pills (not just the first) so a multi-level map binds its
            # Location to the FINEST geography present, not whichever level Tableau serialised first.
            if role == "detail" and f.get("geo_area"):
                enc["geo_levels"].append(f)
            # Tableau's Detail shelf can hold MANY pills (tooltip measures + the real
            # disaggregating dimension(s)). enc["detail"] keeps only the FIRST, so a scatter whose
            # first Detail pill is a measure would otherwise lose its dimension and misclassify as a
            # card. Retain EVERY category Detail pill (deduped, in order) so scatter classification
            # and its Category/Details binding see the true granularity dimension(s).
            if role == "detail" and f.get("kind") == "category":
                key = (f.get("entity"), f.get("property"),
                       f.get("binding"), f.get("aggregation"))
                if key not in seen_detail_dims:
                    seen_detail_dims.add(key)
                    enc["detail_dims"].append(f)
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


def _folded_axis_instances(table, axis):
    """Measure instances whose axis is FOLDED onto another -- Tableau's spelling of a dual axis.

    Tableau writes no ``dual-axis`` attribute anywhere in a ``.twb`` (searched the whole corpus:
    the only literal "dual" is in product names). What it writes is a worksheet
    ``<style><style-rule element='axis'>`` whose ``<encoding attr='space'>`` for the SECONDARY
    measure carries ``fold='true'`` (usually alongside ``synchronized='true'``) -- literally "fold
    this axis into the other one's rectangle".

    That flag is the ONLY difference between a dual axis and a side-by-side measure trellis, which
    are otherwise serialised identically: a leading blank pane plus one ``*-axis-name`` pane per
    measure, no index on any of them. ``scope`` names the shelf (``rows`` for a y axis, ``cols``
    for an x one) and is treated as a filter only when present.

    The neighbouring ``synchronized='true'`` is deliberately NOT read. It looks like the signal for
    "both measures share one scale" and is not: swept across 35 corpus workbooks plus a dedicated
    dual-axis workbook it holds on 33 of 37 folded axes -- including the ``SUM``/``AVG`` sheet whose
    two scales are the reason the combo route exists -- so it restates ``fold`` rather than
    qualifying it. See :func:`_is_scale_pair` for the discriminator that does work.
    """
    folded = set()
    style = _first(table, "style")
    if style is None:
        return folded
    scope = "rows" if axis == "y" else "cols"
    for rule in _children_local(style, "style-rule"):
        if _attr_local(rule, "element") != "axis":
            continue
        for enc in _children_local(rule, "encoding"):
            if _attr_local(enc, "attr") != "space":
                continue
            if (_attr_local(enc, "fold") or "").strip().lower() != "true":
                continue
            enc_scope = _attr_local(enc, "scope")
            if enc_scope and enc_scope != scope:
                continue
            field = _attr_local(enc, "field") or ""
            toks = _BRACKET_TOKEN_RE.findall(field)
            if toks:
                folded.add(toks[-1])
    return folded


def _pane_mark_map(table, measure_axis=None):
    """Index a worksheet's per-axis marks for dual-axis / combo detection.

    A dual-axis worksheet serialises one ``<pane>`` per measure axis. Each non-primary pane
    carries the measure field ref of its axis (whose last bracketed token is the column instance,
    e.g. ``sum:Sales:qk``) and its own ``<mark class>``. Returns
    ``(mark_by_instance, primary_mark, has_secondary_axis)`` where ``mark_by_instance`` maps a
    measure instance token to that axis's mark class.

    TWO spellings mean "a second measure axis in the SAME pane rectangle", and reading only the
    first misclassified a whole family of sheets:

    * an axis INDEX >= 1 -- how Tableau distinguishes two axes over the SAME measure (a line + its
      area fill, a lollipop's stick + head): the name alone cannot tell them apart, so it numbers
      them;
    * TWO OR MORE DISTINCT axis NAMES **whose axes fold onto one rectangle** -- how it spells two
      axes over DIFFERENT measures (e.g. ``SUM(Sales)`` and ``AVG(Sales)``), where the names
      already disambiguate so no index is written. The fold is essential: distinct names on their
      own are ALSO how Tableau spells a side-by-side measure trellis, and the two are otherwise
      byte-identical. See :func:`_folded_axis_instances`.

    Only the first was detected, so a different-measure dual axis looked like an ordinary sheet and
    was rebuilt as a measure TRELLIS -- separate panes -- when the source draws both series overlaid
    in one plot area. Confirmed by pixel-measuring the Tableau render: both series span the full
    plot height from a shared baseline, so there is one pane, not two.

    ORIENTATION. Tableau names the measure axis after the shelf the measures sit on: ``y-axis-name``
    / ``y-index`` when they are on Rows (a vertical chart), ``x-axis-name`` / ``x-index`` when they
    are on Cols (a HORIZONTAL one). Reading only ``y`` meant a horizontal dual axis was invisible --
    a horizontal lollipop, whose stick and head both live on Cols, came back as an ordinary bar
    chart in the wrong colour. ``measure_axis`` is ``"x"`` or ``"y"``; ``None`` keeps the historical
    y-only read, which is also the right answer when measures sit on BOTH shelves (a scatter, where
    each axis is its own measure and neither is a second axis over the other).
    """
    mark_by_instance = {}
    primary_mark = None
    has_secondary_axis = False
    panes_el = _first(table, "panes")
    if panes_el is None:
        return mark_by_instance, primary_mark, has_secondary_axis
    axis = measure_axis if measure_axis in ("x", "y") else "y"
    name_attr = "{0}-axis-name".format(axis)
    index_attr = "{0}-index".format(axis)
    axis_panes = 0
    for pane in _children_local(panes_el, "pane"):
        mk_el = _first(pane, "mark")
        mk = mk_el.get("class") if mk_el is not None else None
        idx = _attr_local(pane, index_attr)
        is_indexed = idx not in (None, "", "0")
        axis_name = _attr_local(pane, name_attr)
        if axis_name or is_indexed:
            axis_panes += 1
        if axis_name:
            toks = _BRACKET_TOKEN_RE.findall(axis_name)
            if toks:
                mark_by_instance[toks[-1]] = mk
        elif primary_mark is None and mk:
            primary_mark = mk
    # ONE RULE FOR BOTH SPELLINGS: a dual axis is measure axes that share ONE RECTANGLE.
    #
    # Tableau writes "another axis in the same rectangle" two ways -- an INDEX >= 1 (two axes over the
    # SAME measure: a line + its area fill, a lollipop's stick + head) and a FOLD (two axes over
    # DIFFERENT measures, where the names already disambiguate). Neither spelling means "dual axis" on
    # its own, because both also occur INSIDE a side-by-side measure trellis, which is serialised
    # identically: a leading blank pane plus one named pane per measure.
    #
    # Gating only the fold left the index ungated, and a trellis whose FIRST column happens to be
    # internally dual still collapsed: measured on "Engagements by Dimension" (Staff Capacity), five
    # measures on Cols where the first is drawn on two axes -- one ``x-index='1'`` pane was enough to
    # rebuild the whole block as ONE combo chart spanning the dashboard.
    #
    # So count RECTANGLES: distinct axis NAMES minus the FOLDED ones. An index needs no subtraction --
    # it repeats a name already in the map, so the dedupe by instance has counted that rectangle once
    # already. Subtracting it too would erase a rectangle that genuinely exists, turning a two-column
    # trellis whose first column is dual back into a combo.
    #
    # Evidence for treating the two spellings differently rather than symmetrically: across every
    # workbook available, exactly ONE sheet pairs an index with 2+ distinct axis names -- and it is
    # the trellis above. Every real DIFFERENT-measure dual axis (SUM+AVG, pareto, control chart,
    # previous-vs-current-year) carries a fold and no index.
    #
    # ``axis_panes >= 2`` keeps an ordinary single-axis chart out of it.
    overlaid = _folded_axis_instances(table, axis)
    rectangles = len(set(mark_by_instance) - overlaid)
    if axis_panes >= 2 and rectangles <= 1:
        has_secondary_axis = True
    return mark_by_instance, primary_mark, has_secondary_axis


def _measure_shelf_axis(meas_rows, meas_cols):
    """Which pane axis carries this worksheet's measures: ``"y"`` (Rows), ``"x"`` (Cols), else None.

    Measures on BOTH shelves is a scatter -- each axis is its own measure, so neither is a "second
    axis" over the other -- and returns ``None`` so the caller keeps the conservative y-only read.
    """
    if meas_rows and not meas_cols:
        return "y"
    if meas_cols and not meas_rows:
        return "x"
    return None


def _mark_family(mark):
    m = (mark or "").strip().lower()
    if m in _COLUMN_FAMILY_MARKS:
        return "column"
    if m in _LINE_FAMILY_MARKS:
        return "line"
    return None


def _detect_combo(meas_rows, meas_cols, has_category, mark_by_instance, primary_mark,
                  dual_axis=False, overlay=False):
    """Classify a dual-axis combo: measures on one shelf that split into a column-family group
    and a line-family group, against a shared category dimension.

    Returns ``(column_measures, line_measures)`` only when BOTH groups are non-empty (a genuine
    combo); otherwise ``(None, None)`` so the caller keeps the ordinary single-mark visual. This
    is deliberately conservative -- same-mark multi-measure shelves and unresolvable measures
    never trigger a combo (warn-never-wrong).

    SAME-FAMILY DUAL AXIS STILL NEEDS TWO AXES. When the sheet is dual-axis over DIFFERENT measures
    but both panes draw the same mark family (e.g. Bar + Bar), the family split finds nothing -- yet
    the whole point of the source's second axis is its own SCALE. Collapsing both onto one Power BI
    axis is not a cosmetic loss: measured on a `SUM(Sales)` + `AVG(Sales)` sheet, the average is
    ~1/40th of the sum, so it rendered as an invisible sliver where Tableau draws it at a third of
    the plot height. An invisible series is the same failure as an error tile, so the secondary-axis
    measure goes to Y2 and keeps its scale. Power BI draws a Y2 series as a LINE, so this trades one
    mark type for both series actually being visible -- disclosed by the caller's fidelity note.

    ...UNLESS THE TWO MEASURES ARE AN OVERLAY rather than two scales -- see :func:`_is_scale_pair`,
    which is what ``overlay`` carries. Tableau's overlapping-bar ("lipstick") idiom is serialised
    IDENTICALLY to the sliver case above, so the two cannot be told apart by the dual-axis markers;
    routing every same-family dual axis to a combo turned all 16 sheets of a lipstick workbook into
    line-on-Y2 combos, 8 of them ALSO rotated (Power BI's only combo draws its columns vertically,
    so a measures-on-Cols sheet loses its orientation as well as its mark type).

    NOT gated on ``synchronized='true'``, which looks like the obvious discriminator and is not one:
    swept across the corpus it holds on 33 of 37 folded axes -- including the ``SUM``/``AVG`` sheet
    that motivates the paragraph above -- so it is a restatement of ``fold`` rather than a signal.

    Which measure is secondary comes from SHELF ORDER: Tableau writes the primary axis first.

    Gated on the PRIMARY axis already being a column family, because Power BI's combo always draws
    its Y well as COLUMNS. Promoting a line-primary dual axis would turn the main series into bars --
    measured as a regression on a three-line running-total sheet that came back as stacked columns.
    """
    if not has_category:
        return None, None
    measures = list(meas_rows) + list(meas_cols)
    column_meas, line_meas = [], []
    for f in measures:
        fam = _mark_family(mark_by_instance.get(f.get("instance"), primary_mark))
        if fam == "column":
            column_meas.append(f)
        elif fam == "line":
            line_meas.append(f)
    if column_meas and line_meas:
        return column_meas, line_meas
    if dual_axis and len(measures) > 1 and not line_meas and not overlay:
        distinct = {str(f.get("instance") or f.get("caption")) for f in measures}
        if len(distinct) > 1:
            return measures[:1], measures[1:]
    return None, None


def _is_scale_pair(measures):
    """True when a dual axis's measures are TWO SCALES of one field rather than an OVERLAY.

    Tableau serialises both idioms identically -- same shelf, same fold, same marks -- so this is
    the only structural signal that separates them, and it is read off the measure INSTANCE tokens
    (``sum:Sales:qk``, ``avg:Sales:qk``) rather than from any data:

    * ``SUM(Sales)`` + ``AVG(Sales)``  -- one field, two aggregations. A sum and an average of the
      same column differ by roughly the row count, so they are on different magnitude scales BY
      CONSTRUCTION. That is why the author reached for a second axis, and collapsing them onto one
      renders the smaller as an invisible sliver (measured: ~1/40th on 0085 ``Small Bar (2)``).
    * ``pcto:cum:cnt:Profit`` + ``cnt:Profit`` -- one field, a percent-of-total running total
      against its own count. The Pareto idiom; also two scales by construction.
    * ``SUM(Sales)`` + ``SUM(Profit)`` -- DIFFERENT fields, same aggregation. Nothing about the pair
      forces different magnitudes, and overlaying two comparable measures is precisely what the
      overlapping-bar idiom is for.

    Conservative in the direction that preserves today's behaviour: anything that cannot be parsed
    into ``agg:field:tag`` counts as a scale pair, so an unrecognised shape keeps the combo route it
    has today rather than being re-routed into an overlay on a guess.

    Corpus check (35 workbooks + the lipstick workbook, 37 folded axes): every same-field pair is a
    genuine two-scale idiom (``SUM``/``AVG``, Pareto) and every different-field pair is a comparison
    overlay (a lipstick, or Salesforce current-vs-previous-year bars).
    """
    fields, aggs = set(), set()
    for f in measures or ():
        inst = str(f.get("instance") or "")
        parts = inst.split(":")
        if len(parts) < 3:
            return True
        # ``pcto:cum:cnt:Profit:qk`` -- the FIELD is the second-to-last token; everything before it
        # is the aggregation chain.
        fields.add(parts[-2])
        aggs.add(":".join(parts[:-2]))
    if len(fields) != 1:
        return False
    return len(aggs) > 1


_LOLLIPOP_HEAD_MARKS = frozenset({"circle", "shape", "point"})


def _lipstick_measures(meas_rows, meas_cols, mark_by_instance, primary_mark):
    """The overlaid measures of a "lipstick" (overlapping-bar) sheet, in SHELF ORDER, else ``None``.

    Requires 2+ DISTINCT measures on one shelf, every one of them drawn by a COLUMN-family mark.
    A mixed-family shelf is a real combo and was already claimed by :func:`_detect_combo`; a
    single-measure shelf has nothing to overlap. Shelf order is preserved because it IS the
    z-order -- see :func:`_lipstick_overlap`.
    """
    measures = list(meas_rows) + list(meas_cols)
    if len(measures) < 2:
        return None
    distinct = {str(f.get("instance") or f.get("caption")) for f in measures}
    if len(distinct) < 2:
        return None
    for f in measures:
        if _mark_family(mark_by_instance.get(f.get("instance"), primary_mark)) != "column":
            return None
    return measures


# The clustered families whose layout card exposes the overlap controls. The unqualified
# ``columnChart`` / ``barChart`` are Power BI's STACKED variants (see _pbir_visual_type) -- their
# segments are stacked, not overlaid, so an overlap card there is meaningless at best.
_LIPSTICK_VTYPES = frozenset({"clusteredColumnChart", "clusteredBarChart"})


def _pane_mark_colors(table, axis):
    """Per-AXIS constant mark colours: ``{measure instance -> '#hex'}``.

    A dual-axis worksheet colours each overlaid series on its OWN pane
    (``<pane x-axis-name='...'><style><style-rule element='mark'><format attr='mark-color'>``).
    :func:`_constant_mark_color` deliberately collapses the worksheet to ONE colour, which is right
    for a single-series chart and wrong for an overlay: rendered, it painted both overlapping bars
    the same orange, so the only thing separating them was the transparency. Tableau draws them in
    two colours, and two same-coloured bars sharing one slot is precisely the readability failure
    the overlap rebuild exists to avoid.

    Only panes that name an axis are read, so the leading unnamed pane (which carries no series of
    its own) cannot supply a colour for everything.
    """
    colors = {}
    panes_el = _first(table, "panes")
    if panes_el is None:
        return colors
    name_attr = "{0}-axis-name".format(axis if axis in ("x", "y") else "y")
    for pane in _children_local(panes_el, "pane"):
        axis_name = _attr_local(pane, name_attr)
        if not axis_name:
            continue
        toks = _BRACKET_TOKEN_RE.findall(axis_name)
        if not toks:
            continue
        style = _first(pane, "style")
        if style is None:
            continue
        for rule in _children_local(style, "style-rule"):
            if _attr_local(rule, "element") != "mark":
                continue
            for fmt in _children_local(rule, "format"):
                if _attr_local(fmt, "attr") != "mark-color":
                    continue
                val = (_attr_local(fmt, "value") or "").strip().lower()
                if val.startswith("#"):
                    colors[toks[-1]] = val
    return colors


def _pane_mark_transparency(table, axis):
    """Per-AXIS mark transparency: ``{measure instance -> percent}``, opaque panes omitted.

    The sibling of :func:`_pane_mark_colors`, and the reason the overlapping-bar rebuild does not
    have to INVENT a transparency: Tableau's author already made this choice per overlaid series,
    and it is sitting in the pane as
    ``<format attr='mark-transparency' value='N'/>``.

    ``N`` is an ALPHA BYTE (0..255), not the 0..100 the Tableau UI shows -- see
    :func:`_worksheet_mark_transparency`, where that reading is verified against two renders. Power
    BI's ``fillTransparency`` is the complement as a percentage, so ``255`` -> 0 (omitted, opaque)
    and ``147`` -> 42.

    Measured on a 16-sheet dual-axis workbook: the author set this on ONE series of 8 sheets and
    left the other 8 fully opaque. An engine-chosen "always make the front series translucent" rule
    therefore contradicted the source on half the corpus -- and rendered as mud on the rest, because
    a translucent series over an opaque one BLENDS with it rather than revealing it.
    """
    out = {}
    panes_el = _first(table, "panes")
    if panes_el is None:
        return out
    name_attr = "{0}-axis-name".format(axis if axis in ("x", "y") else "y")
    for pane in _children_local(panes_el, "pane"):
        axis_name = _attr_local(pane, name_attr)
        if not axis_name:
            continue
        toks = _BRACKET_TOKEN_RE.findall(axis_name)
        if not toks:
            continue
        for el in pane.iter():
            if _local(el.tag) != "format" or (el.get("attr") or "") != "mark-transparency":
                continue
            try:
                n = float(el.get("value"))
            except (TypeError, ValueError):
                continue
            if not (0 <= n <= 255):
                continue
            pct = int(round((1.0 - n / 255.0) * 100))
            if pct > 0:
                out[toks[-1]] = pct
    return out


def _pane_mark_sizes(table, axis):
    """Per-AXIS mark size: ``{measure instance -> float}``, panes declaring none omitted.

    The third sibling of :func:`_pane_mark_colors` / :func:`_pane_mark_transparency`, and the one
    that reads a property Power BI cannot express AT ALL. Tableau's overlapping-bar idiom separates
    its two series three ways -- length, WIDTH, and transparency -- and a clustered Power BI chart
    has no per-series bar width, so an author who reached only for width has their entire separation
    mechanism dropped in translation.

    An UNDECLARED size is Tableau's default and says nothing about the author's intent, so it is
    omitted rather than defaulted to 1.0 -- the same rule :func:`_has_oversized_second_pane` already
    applies to the lollipop's head-vs-stick signal.
    """
    out = {}
    panes_el = _first(table, "panes")
    if panes_el is None:
        return out
    name_attr = "{0}-axis-name".format(axis if axis in ("x", "y") else "y")
    for pane in _children_local(panes_el, "pane"):
        axis_name = _attr_local(pane, name_attr)
        if not axis_name:
            continue
        toks = _BRACKET_TOKEN_RE.findall(axis_name)
        if not toks:
            continue
        size = _pane_mark_size(pane)
        if size is not None and size > 0:
            out[toks[-1]] = size
    return out


# Transparency substituted for a WIDTH difference Power BI cannot draw, as a percentage.
#
# Not invented: the authors of the reference workbook chose 36%, 42% and 48% by hand on the sheets
# where they used transparency, and 42 is one of those values -- taken from the sheet that is the
# closest analogue (same variant family, same series wider). A flat value rather than one scaled to
# the width ratio, because the ratio is a Tableau mark-size number with no defined mapping onto a
# Power BI transparency and any curve over it would be invented precision.
_LIPSTICK_WIDTH_SUBSTITUTE_TRANSPARENCY = 42


def _lipstick_series_transparency(measures, pane_transp, pane_sizes):
    """Per-series transparency for an overlapping-bar rebuild, in SHELF ORDER.

    Two sources, in strict precedence:

    1. **The author's own** ``mark-transparency`` on that series' pane. Always wins -- Tableau has
       this setting and the author already made the choice, so there is nothing to decide.
    2. **A substitute for a WIDTH difference**, only for a series the author left opaque. When both
       overlaid series declare a mark SIZE and one is strictly wider, that width gap is the author's
       entire separation mechanism and Power BI's clustered chart cannot draw it -- equal-width bars
       in one slot means the front one simply covers the back one. Lightening the WIDER series is
       the closest available stand-in for "a broad pale bar with a narrow solid one inside it",
       which is the look the width difference produces in the source.

    Returns ``[]`` when nothing applies, so an untouched sheet emits no ``dataPoint`` at all.

    Deliberately does NOT substitute when only one side declares a size: an undeclared size is
    Tableau's default and says nothing about intent, so comparing it against a declared one would
    manufacture a width gap the author never expressed.
    """
    out = []
    sizes = [pane_sizes.get(f.get("instance")) for f in measures]
    widest = None
    if len(sizes) >= 2 and all(s is not None for s in sizes):
        top = max(sizes)
        # A tie is not a width difference, and neither is more than one series sharing the maximum.
        if sizes.count(top) == 1:
            widest = sizes.index(top)
    for i, f in enumerate(measures):
        authored = pane_transp.get(f.get("instance"))
        if authored:
            out.append(authored)
        elif i == widest:
            out.append(_LIPSTICK_WIDTH_SUBSTITUTE_TRANSPARENCY)
        else:
            out.append(None)
    return out if any(out) else []


def _folded_measure_groups(measures, folded):
    """Group shelf measures into RECTANGLES: ``[[0], [1, 2], [3]]`` as index lists.

    Tableau folds a secondary axis onto the one BEFORE it, so a pill whose instance is in
    ``folded`` joins the group its predecessor opened. Everything else opens a new group.

    This is the shape :func:`_pane_mark_map` reduces to a COUNT. That count is the right question
    for "is this whole sheet one overlaid pane?" and the wrong one for a sheet that is N overlaid
    panes side by side -- measured on ``0088 Service Provider Details``, six pills with three folds:
    ``rectangles = 3``, so the ``<= 1`` gate said "not a dual axis" and the measure trellis fanned
    all SIX measures into six single-measure charts where Tableau draws three overlaid pairs.
    """
    groups = []
    for i, f in enumerate(measures or ()):
        if groups and f.get("instance") in folded:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def _lipstick_overlap_objects(query_state, vtype, series_colors=None, series_transparency=None):
    """PBIR format objects that rebuild Tableau's overlapping-bar idiom, or ``None``.

    Returns ``{"layout": [...], "dataPoint": [...]}`` -- the ``layout`` card turning Overlap on with
    100% series spacing (so the two series share one slot instead of sitting side by side), plus
    per-series ``dataPoint`` fills where the source coloured its panes.

    Z-ORDER IS PROJECTION ORDER, and it needs no translation: Tableau draws the FIRST measure on a
    folded axis behind the second, and so does Power BI, so preserving shelf order preserves which
    series is in front. Confirmed against the ground-truth `.pbip`, whose two pages differ ONLY in
    ``Y`` projection order and whose own page titles name the resulting front/back split.

    NO TRANSPARENCY IS INVENTED, and the reason is worth keeping because the argument for inventing
    one is sound and still lost at the render. Overlap makes total occlusion possible: with both bars
    in one slot the back one is hidden wherever the front is longer. Tableau escapes that three ways
    -- length, per-series WIDTH, and transparency -- and Power BI's clustered chart has no per-series
    width at all, so a translucent FRONT series looked like the only escape decidable at migration
    time (nothing is ever drawn on top of the front series, so it leaves the back visible for ANY
    data). Shipped, rendered, rejected: a translucent series over an opaque one does not REVEAL the
    one underneath, it BLENDS with it, producing a third colour that reads as a series which does not
    exist. Measured on a 16-sheet workbook whose author set one series to Tableau's orange while the
    other took the palette's blue -- near-complementary hues, so every overlap region came out brown.
    The ground-truth `.pbip` avoids this only incidentally: it emits no explicit fills at all, so both
    its series take the default theme's light-blue/dark-blue pair and a blend of them is still a blue.

    What ships instead is the AUTHOR'S OWN transparency, read per pane by
    :func:`_pane_mark_transparency`. Tableau has this setting and the author already used it, so
    there is nothing to choose: 8 of those 16 sheets carry it on one series and 8 are fully opaque,
    which means any engine-chosen rule contradicted the source on half of them. Where the source set
    none, the overlap ships opaque -- and the occlusion that leaves is disclosed in the worksheet's
    fidelity note rather than hidden.
    """
    if vtype not in _LIPSTICK_VTYPES:
        return None
    projs = [p for p in ((query_state or {}).get("Y") or {}).get("projections", [])
             if not p.get("hidden")]
    if len(projs) < 2:
        return None
    objects = {"layout": [{"properties": {
        "clusteredGapOverlaps": {"expr": {"Literal": {"Value": "true"}}},
        "clusteredGapSize": {"expr": {"Literal": {"Value": "100D"}}},
    }}]}
    fills = []
    colors = list(series_colors or ())
    transp = list(series_transparency or ())
    # Positional pairing, refused unless the lengths agree. The projections carry a queryRef but not
    # the measure instance the colour was read against, so a mismatched zip would paint the wrong
    # series -- and a wrong colour on an overlay is indistinguishable from a correct one at a glance.
    if len(colors) != len(projs):
        colors = [None] * len(projs)
    if len(transp) != len(projs):
        transp = [None] * len(projs)
    if any(colors):
        # EVERY series gets an explicit colour once ANY of them does, because Power BI colours an
        # unset series by its POSITION in the theme palette -- and the Tableau palette this engine
        # emits is the same list the author picked their mark colour from. Rendered: Tableau set
        # Sales to #F28E2B, which is the theme's SECOND entry, so the second series defaulted to
        # that very colour and both overlapping bars came out orange, distinguished only by the
        # transparency. Fill the gaps from the palette skipping colours already taken, which also
        # matches Tableau: a pane with no explicit mark colour draws in the palette's first colour.
        taken = {c.lower() for c in colors if c}
        spare = [c for c in _TABLEAU_10 if c.lower() not in taken]
        for i, c in enumerate(colors):
            if c:
                continue
            colors[i] = spare.pop(0) if spare else None
    for i, p in enumerate(projs):
        qref = p.get("queryRef")
        if not qref:
            continue
        props = {}
        hexv = colors[i]
        if hexv:
            props["fill"] = {"solid": {"color": {"expr": {"Literal": {
                "Value": _semantic_string_literal(hexv)}}}}}
        tpct = transp[i]
        if tpct:
            props["fillTransparency"] = {"expr": {"Literal": {"Value": "%dD" % tpct}}}
        if not props:
            continue
        # ``selector.metadata`` must name a queryRef this visual actually PROJECTS, which is why it
        # is read back off the emitted query state rather than rebuilt from the parsed shelf.
        fills.append({"properties": props, "selector": {"metadata": qref}})
    if fills:
        objects["dataPoint"] = fills
    return objects


def _all_pane_marks(table):
    """Every mark class present across ALL of a worksheet's panes (lower-cased).

    Unlike :func:`_pane_mark_map` (which indexes only the ``y-axis-name`` panes used for combo
    splitting and records just the FIRST primary pane), this surfaces EVERY pane's mark -- needed for
    the lollipop, whose Circle/Shape head can sit on a primary pane that ``_pane_mark_map`` drops.
    """
    marks = set()
    panes_el = _first(table, "panes")
    if panes_el is None:
        return marks
    for pane in _children_local(panes_el, "pane"):
        mk_el = _first(pane, "mark")
        cls = mk_el.get("class") if mk_el is not None else None
        if cls:
            marks.add(cls.strip().lower())
    return marks


def _constant_mark_color(table):
    """A worksheet's single constant mark colour -- the ``<format attr='mark-color' value='#hex'/>``
    on a ``mark`` style-rule -- lower-cased, or ``None``.

    This is the flat per-mark default that :func:`_parse_mark_colors` deliberately skips (that reader
    only takes an explicit per-member palette). The lollipop stick/dot colour is sourced from here,
    falling back to the theme when the worksheet set no constant colour.

    AN AXIS PANE OUTRANKS PANE 0. A dual-axis worksheet writes a leading pane that carries no axis
    of its own -- Tableau's all-panes default, which draws nothing once per-axis panes exist -- and
    it can hold a STALE colour from before the second axis was added. Taking the first hex in
    document order therefore painted a lollipop in the cyan its pane 0 still remembered while both
    drawing panes said green. Any colour declared on a pane that owns an axis wins.
    """
    if table is None:
        return None
    axis_color = None
    panes_el = _first(table, "panes")
    for pane in (_children_local(panes_el, "pane") if panes_el is not None else []):
        owns_axis = any(_attr_local(pane, a) not in (None, "")
                        for a in ("x-axis-name", "y-axis-name", "x-index", "y-index"))
        if not owns_axis:
            continue
        for el in pane.iter():
            if _local(el.tag) != "format" or (el.get("attr") or "") != "mark-color":
                continue
            v = (el.get("value") or "").strip()
            if re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
                axis_color = axis_color or v.lower()
    if axis_color:
        return axis_color
    for el in table.iter():
        if _local(el.tag) != "format":
            continue
        if (el.get("attr") or "") != "mark-color":
            continue
        v = (el.get("value") or "").strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            return v.lower()
    return None


def _lollipop_measure_key(field):
    """Stable identity for the lollipop same-measure test: the pill instance token when present
    (e.g. ``sum:Sales:qk``), else the caption + aggregation + calc flag."""
    inst = field.get("instance")
    if inst:
        return ("inst", inst)
    return ("cap", field.get("caption"), field.get("derivation") or field.get("agg"),
            bool(field.get("is_calc")))


def _detect_lollipop(table, meas_rows, meas_cols, has_category):
    """Detect a dual-axis lollipop: a Bar (stick) pane AND a Circle/Shape/Point (head) pane plotting
    the SAME measure against a shared category.

    Power BI has no native lollipop; the faithful build is a ``lineClusteredColumnComboChart`` with
    the one measure on BOTH wells -- thin columns (the sticks) on Y and a marker-only hidden line
    (the dots) on Y2. Returns the shared measure as a one-element list (to bind to both wells) or
    ``None``. Deliberately conservative -- requires a head mark AND a Bar mark AND a single measure
    identity across >=2 axes, so ordinary bar/line charts, area overlays (line+area, no bar), and
    different-measure dual-scale combos never misfire (warn-never-wrong).

    AN ``Automatic`` HEAD IS STILL A HEAD. Tableau writes ``class="Automatic"`` on a pane whose mark
    the author never picked by hand, so a real lollipop can reach us as Bar + Automatic and the
    head-mark test found nothing -- the sheet fell through to a plain bar chart. What identifies the
    second pane as the head, without having to resolve what Automatic means, is its SIZE: it is
    markedly FATTER than the stick (measured 1.86 against 0.81 on one workbook, 1.86 against 0.39 on
    another). Two bars over the same measure where the one drawn second is the WIDER and fully
    opaque is not a chart anyone builds -- it would hide the first completely -- so a same-measure
    second axis that is much wider than its Bar partner is a point mark, whatever the file calls it.
    """
    if not has_category:
        return None
    measures = list(meas_rows) + list(meas_cols)
    if len(measures) < 2:
        return None
    if len({_lollipop_measure_key(f) for f in measures}) != 1:
        return None
    marks = _all_pane_marks(table)
    if (marks & _LOLLIPOP_HEAD_MARKS) and "bar" in marks:
        return measures[:1]
    if "bar" in marks and "automatic" in marks and _has_oversized_second_pane(table):
        return measures[:1]
    return None


# How much fatter than its Bar partner a same-measure second pane must be before it is read as a
# point mark rather than a second bar. Well below the smallest measured ratio (1.86 / 0.81 = 2.3).
_LOLLIPOP_HEAD_SIZE_RATIO = 1.6


def _pane_mark_size(pane):
    """A pane's ``<format attr='size'>`` mark size as a float, or ``None`` when it declares none."""
    for rule in _findall_local(pane, "style-rule"):
        if (rule.get("element") or "").lower() != "mark":
            continue
        for fmt in _children_local(rule, "format"):
            if (fmt.get("attr") or "") == "size":
                try:
                    return float(fmt.get("value"))
                except (TypeError, ValueError):
                    return None
    return None


def _has_oversized_second_pane(table):
    """True when some pane's mark size is >= :data:`_LOLLIPOP_HEAD_SIZE_RATIO` x a Bar pane's size.

    The lollipop's head-vs-stick signal that survives an ``Automatic`` mark class (see
    :func:`_detect_lollipop`). Both sizes must be declared -- an undeclared size is Tableau's
    default and says nothing about the author's intent.
    """
    panes_el = _first(table, "panes")
    if panes_el is None:
        return False
    bar_sizes, other_sizes = [], []
    for pane in _children_local(panes_el, "pane"):
        mk_el = _first(pane, "mark")
        cls = (mk_el.get("class") if mk_el is not None else "") or ""
        size = _pane_mark_size(pane)
        if size is None or size <= 0:
            continue
        (bar_sizes if cls.strip().lower() == "bar" else other_sizes).append(size)
    if not bar_sizes or not other_sizes:
        return False
    return max(other_sizes) >= min(bar_sizes) * _LOLLIPOP_HEAD_SIZE_RATIO


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
    # uses a geographic dimension on a shelf. Truly-custom-geometry marks (density/heatmap, or a
    # polygon with no spatial signal) are deferred; ambiguous marks additionally require a spatial
    # signal (generated lat/lon or a geometry encoding).
    if geo_detail and map_meas:
        # v2-6: a Multipolygon/Polygon FILL over a recognized geography (we are already inside
        # geo_detail -- a geo-role dimension on Detail -- with a measure) plus a spatial signal
        # (generated lat/lon on the axes or a <geometry> encoding) is Tableau's rendering of a
        # standard-geography choropleth. Its faithful Power BI home is a shapeMap, not a custom
        # polygon with no built-in topology, so recover it here BEFORE the blanket defer that
        # previously short-circuited every polygon mark. A polygon with NO spatial signal stays a
        # truly-custom polygon and still defers; density/heatmap have no offline home, always defer.
        if m in _POLYGON_MAP_MARKS and map_signal:
            return VT_SHAPE_MAP
        # Tableau's DENSITY / HEATMAP mark over a geography has a native azureMap home -- the
        # ``heatMapLayer`` -- so it is rebuilt rather than deferred. Deferring it produced no page at
        # all for the worksheet (issue #112), which is a worse answer than a faithful heat layer.
        if m in _DENSITY_MAP_MARKS:
            return VT_DENSITY_MAP
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
        # A PIE mark over a geography is Tableau's pie-on-a-map. Falling through to the chart
        # heuristics turned it into a plain ``pieChart`` -- the geography SILENTLY dropped, and the
        # output looks finished, which is worse than a degraded map (issue #112). azureMap has no
        # per-point pie, so the faithful degrade is a bubble layer with the pie's own colour
        # dimension as the Series legend; the caller warns that the per-slice split is lost.
        if m == "pie" and map_signal:
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
        if m in _DENSITY_MAP_MARKS:
            return VT_DENSITY_MAP
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
    Mirrors the real Tableau serialisations: a single ``function='member'`` child, a
    ``function='union'`` keep-list, or a ``function='except'`` wrapper (exclude). A non-narrowing or
    ambiguous filter returns ``None`` so the slicer stays at its faithful default (warn-never-wrong:
    never invent a selection that could hide real data wrong).

    A union keep-list is written TWO ways and both mean the same thing. The long-recognised form
    carries ``op='manual'``; the other carries Tableau's own intent attribute
    ``ui-enumeration='inclusive'`` with ``ui-marker='enumerate'`` and no ``op`` at all. Surveying
    every categorical filter across the corpus and the customer workbooks, ``ui-enumeration`` takes
    exactly three values and is perfectly consistent: ``all`` on the non-narrowing ``level-members``
    form (134), ``inclusive`` on enumerated keep-lists -- including the single-``member`` include
    shape already treated as a keep (106) -- and ``exclusive`` only ever paired with ``except`` (6).
    So ``inclusive`` is Tableau stating "the enumerated members are the KEPT set", and reading only
    ``op='manual'`` silently discarded the other 6. The cost was not cosmetic: an authored Fiscal
    Month card enumerating two months parsed as "no selection", so the rebuilt slicer opened on All
    and the page rendered UNFILTERED -- right-looking control, wrong numbers, and no warning."""
    children = _children_local(filt, "groupfilter")
    if not children:
        return None
    members = []
    for child in children:
        fn = child.get("function")
        op = _attr_local(child, "op")
        enum = _attr_local(child, "ui-enumeration")
        if fn == "except":
            ex = _filter_member_literals(child)
            return {"mode": "exclude", "values": _dedupe_str(ex)} if ex else None
        if fn == "member" and child.get("member") is not None:
            members.append(_strip_member_literal(child.get("member")))
        elif fn == "union" and (op == "manual" or enum == "inclusive"):
            members.extend(_filter_member_literals(child))
    members = _dedupe_str([m for m in members if m != ""])
    return {"mode": "include", "values": members} if members else None


# A Tableau table calculation placed on the FILTERS shelf is a migration class of its own. In
# Tableau's order of operations it runs LAST -- after aggregation, after the viz LOD is materialised
# -- so it HIDES marks rather than removing data, and every OTHER table calc in the view is
# unaffected by it. Power BI has no equivalent: a visual calculation cannot filter at all, and a
# visual-level filter genuinely REMOVES rows, which would silently re-scope the neighbouring table
# calcs (a running sum restarts, a percent-of-total's denominator shrinks, a moving average loses its
# lead-in). That failure renders cleanly, keeps the right ROW COUNT, and is wrong only in the values
# -- the worst shape in the migration surface, and the reason a row-count check can never verify this
# class.
#
# Detection is mechanical and needs no judgement: resolve the filter's field to its formula and test
# for a table-calc head. These are the heads the translator itself recognises (``_TABLECALC_ALL`` in
# ``calc_to_dax``), kept as a literal pattern here so the report layer stays import-free.
_TABLE_CALC_HEAD_RE = re.compile(
    r"\b(INDEX|SIZE|FIRST|LAST|LOOKUP|TOTAL|RANK|RANK_DENSE|RANK_MODIFIED|RANK_PERCENTILE"
    r"|RUNNING_[A-Z_]+|WINDOW_[A-Z_]+|SCRIPT_[A-Z_]+)\s*\(", re.IGNORECASE)


def _table_calc_filter_idioms(formula):
    """The table-calc heads a filter formula uses, or ``()`` when it is an ordinary filter.

    Matches ``NAME(`` only, so a field REFERENCE that merely contains the word (``[Last Year
    Sales]``, ``[Total Cost]``) never trips it -- Tableau writes those inside brackets."""
    if not formula:
        return ()
    return tuple(sorted({m.group(1).upper() for m in _TABLE_CALC_HEAD_RE.finditer(formula)}))


def _worksheet_table_calc_count(ws):
    """How many ``<table-calc>`` elements the worksheet carries -- the cascade signal.

    A table-calc filter alongside other table calcs is the branch where translating the filter as an
    ordinary data filter would corrupt its neighbours' values, so the count is reported to the reader
    rather than left for them to discover."""
    if ws is None:
        return 0
    return len(_findall_local(ws, "table-calc"))


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


def _shared_view_filter_index(root):
    """Index the filters Tableau hoists OUT of worksheets into a workbook-level ``<shared-views>``.

    ``Apply to -> All Worksheets Using This Data Source`` does **not** write a ``<filter>`` on each
    participating worksheet. It writes ONE filter under
    ``<shared-views><shared-view name='<datasource>'>`` and leaves every participating worksheet only
    a ``<slices><column>`` naming the sliced field. Reading worksheet-local ``<filter>`` elements
    alone therefore loses every filter authored in that scope -- and with them every dashboard filter
    CARD, because a card resolves to its model column through the matching worksheet filter's token.

    Measured on corpus `0133` against its sibling `0132`, which differ in this scope and almost
    nothing else: `0132` keeps three filters on worksheet ``Profit`` and rebuilds all three slicers;
    `0133` hoists the byte-identical three into ``<shared-views>`` and rebuilt NONE, on all three of
    its dashboards -- nine cards, disclosed as "resolved to no model field" and otherwise silent.
    The dashboard zone tokens are the SAME in both workbooks; only the resolver map differed
    (3 entries vs 0), which is why the failure looked like a card problem and was a parse problem.

    Returns ``{(datasource, field_instance): <filter element>}``. Built once per workbook rather than
    per worksheet: the lookup walks the whole tree, and a 49-worksheet workbook would otherwise walk
    it 49 times.
    """
    out = {}
    for holder in _children_local(root, "shared-views"):
        for sv in _children_local(holder, "shared-view"):
            for filt in _findall_local(sv, "filter"):
                tok = _split_token_attr(filt.get("column"))
                if tok[1] is not None:
                    out.setdefault(tok, filt)
    return out


def _worksheet_shared_filters(ws, shared_index):
    """The shared-view filters that apply to THIS worksheet, via its own ``<slices>`` record.

    ``<slices><column>`` is Tableau's per-sheet statement of which fields the sheet is sliced by, so
    it is the exact participation signal: a sheet that opted out of a data-source-scoped filter does
    not list it, and therefore is not handed a filter it does not have. Keying on the sheet's own
    record rather than on "every sheet using this datasource" keeps the inference evidence-based.
    """
    if not shared_index:
        return []
    sliced = []
    for sl in _findall_local(ws, "slices"):
        for c in _children_local(sl, "column"):
            tok = _split_token_attr((c.text or "").strip())
            if tok[1] is not None and tok not in sliced:
                sliced.append(tok)
    return [shared_index[t] for t in sliced if t in shared_index]


def _parse_filters(ws, ds_default, base_cols, instances, index, ds_caption,
                   worksheet, warnings, warn_special=True, internal_fields=None,
                   date_binding=None, table_calc_filters=None, table_calc_peers=0,
                   extra_filters=()):
    """Returns ``(filters, swap_controls)``. ``swap_controls`` carries any parameter-driven
    sheet-swap visibility controls detected on this worksheet (a categorical filter pinned to a
    pure parameter-passthrough calc). Recognising them structurally keeps them from being
    mis-warned as unmappable measure filters and lets :func:`parse_twb` group swap partners.

    ``extra_filters`` are filter elements that live OUTSIDE the worksheet but apply to it -- today,
    the workbook-level ``<shared-views>`` filters written by *Apply to -> All Worksheets Using This
    Data Source* (see :func:`_shared_view_filter_index`). They are appended AFTER the local ones and
    skipped when the worksheet already carries a filter on the same token, so a sheet-level filter
    always wins over the inherited one and a workbook with no shared views is byte-identical.
    """
    filters = []
    swap_controls = []
    local = _findall_local(ws, "filter")
    seen_tokens = {_split_token_attr(x.get("column")) for x in local}
    inherited = [x for x in extra_filters
                 if _split_token_attr(x.get("column")) not in seen_tokens]
    for filt in local + inherited:
        cls = (filt.get("class") or "").lower()
        ds, fid = _split_token_attr(filt.get("column"))
        if fid is None:
            continue
        # Thread ``date_binding`` so a discrete date PART filter on the active business date (e.g. a
        # "keep Year in {2021, 2022}" card) rebinds to the marked Date table's calendar column
        # (Date[Year]) exactly like the same pill on an axis -- instead of staying on the fact's raw
        # datetime column, where the integer-year members match no datetime value and silently empty
        # the visual. Same tight gate as the axis path (active date + mapped part only); a secondary/
        # inactive date, or any workbook with no date_binding, is byte-identical to before.
        f = _resolve_field(ds or ds_default, fid, base_cols, instances, index,
                           ds_caption, worksheet, warnings, warn_special=warn_special,
                           internal_fields=internal_fields, date_binding=date_binding,
                           for_filter=True)
        if f is None:
            continue
        # Parameter-driven sheet swap: a filter pinned to a pure passthrough control calc
        # ([Parameters].[id]) gates this whole worksheet's VISIBILITY -- it is not a data filter, so
        # record it as a swap control (parse_twb groups partners) and do NOT warn.
        #
        # Tableau writes the pin two ways and both mean the same thing. A discrete control writes a
        # CATEGORICAL member list; a NUMERIC one writes a QUANTITATIVE range degenerated to a single
        # value (``<min>2</min><max>2</max>``, "show this sheet when the parameter equals 2"). Only
        # the categorical form was recognised, so every numeric swap fell through to the
        # aggregate/measure-filter warning and its worksheet lost the control -- measured at 8
        # identical warnings on one real workbook for a single pair of PY-vs-Goal KPI sheets. A range
        # is a swap pin ONLY when it is closed and degenerate (min == max); a genuine open or spanning
        # range on a passthrough calc is a real data filter and still falls through.
        ctrl_formula = (base_cols.get((ds or ds_default, f["field_id"])) or {}).get("formula")
        pid = _param_control_ref(ctrl_formula)
        if pid and cls == "categorical":
            sel = _parse_filter_selection(filt)
            swap_controls.append({
                "param_id": pid,
                "calc_caption": f["caption"],
                "members": list(sel["values"]) if sel and sel.get("mode") == "include" else [],
            })
            continue
        if pid and cls == "quantitative":
            rng = _parse_filter_range(filt) or {}
            lo, hi = rng.get("min"), rng.get("max")
            if lo is not None and hi is not None and str(lo).strip() == str(hi).strip():
                swap_controls.append({
                    "param_id": pid,
                    "calc_caption": f["caption"],
                    "members": [str(lo).strip()],
                })
                continue
        # A slicer binds a raw column. An aggregate (SUM(Sales)) or a measure-role /
        # parameter-comparing calc has no faithful slicer mapping -> warn instead of
        # emitting a wrong slicer. A row-level DIMENSION calc (an IF/CASE bucket like
        # "Job Type ") lands as a real sliceable model column, so it IS kept as a slicer;
        # only calcs that (a) roll up to a measure or (b) compare against a parameter
        # (whose value isn't a column the slicer can bind) stay warned-and-dropped.
        _calc_formula = (base_cols.get((ds or ds_default, f["field_id"])) or {}).get("formula") or ""
        # TABLE-CALC FILTER -- its own class (see `_table_calc_filter_idioms`). Classified BEFORE the
        # generic aggregate/measure branch below, because that message mis-frames the failure: it
        # reads as a missing CONTROL ("not mapped to a slicer") when the actual consequence is that
        # the rebuilt visual shows EVERY mark instead of the author's slice. Naming the idiom, the
        # hide-vs-exclude semantics and the cascade turns a misleading note into something a reader
        # can act on -- and steers them off the "obvious" fix (re-adding it as a visual-level
        # filter), which is the dangerous one.
        _tc_idioms = _table_calc_filter_idioms(_calc_formula)
        if _tc_idioms:
            _peers = table_calc_peers
            if table_calc_filters is not None:
                table_calc_filters.append({
                    "caption": f["caption"], "idioms": list(_tc_idioms), "peers": _peers})
            _cascade = (
                f"; {_peers} other table calc(s) share this view and would be silently re-scoped if "
                f"it were re-added as an ordinary filter (their values change, the row count does not)"
                if _peers else
                "; no other table calc shares this view, so a visual-level filter over an equivalent "
                "model measure is a safe manual rebuild")
            warnings.append(_warn(
                "worksheet", worksheet,
                f"table-calc filter on '{f['caption']}' ({', '.join(_tc_idioms)}) is not reproduced: "
                f"it runs after aggregation and HIDES marks, which Power BI cannot express as a "
                f"filter, so the visual shows all marks{_cascade}"))
            continue
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
        # A title/text zone can embed this field as a live token (``<[ds].[field]>``), which Tableau
        # renders as the field's value IN THIS VIEW. That is knowable exactly when the sheet pins it:
        # one selected member renders as that member, and an unrestricted filter renders "All".
        # Both are confirmed against source renders of two independent customer workbooks (Region
        # filtered to one member showed "Big South"; the same field unrestricted showed "All").
        # A selection of SEVERAL specific members is deliberately left unresolved -- the observed
        # evidence is ambiguous there, and a wrong literal in a header is worse than a blank one.
        f["title_display"] = None
        if cls == "categorical":
            sel = f["selection"]
            if _filter_is_unrestricted(filt):
                f["title_display"] = "All"
            elif sel and sel.get("mode") == "include" and len(sel.get("values") or ()) == 1:
                f["title_display"] = _filter_member_display(sel["values"][0])
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

    A RECENT TABLEAU WRITES THE SAME SORT DIFFERENTLY. Newer builds serialise it as
    ``<shelf-sorts><shelf-sort-v2 dimension-to-sort='[dim]' measure-to-sort-by='[measure]'
    direction='ASC|DESC' shelf='rows|cols' /></shelf-sorts>``, and reading only ``<computed-sort>``
    meant those sheets shipped in the model's own order: a ranked bar chart whose whole point is
    "biggest first" came back scrambled, with no warning, because nothing was missing -- only
    unread. Both spellings are the same directive and are read the same way.
    """
    for cs in _findall_local(view, "computed-sort"):
        using = _attr_local(cs, "using")
        if not using:
            continue
        parsed = _sort_from_measure_token(
            using, _attr_local(cs, "direction"), ds_default, base_cols, instances, index,
            ds_caption, worksheet, warnings, internal_fields)
        if parsed:
            return parsed
    for ss in _findall_local(view, "shelf-sort-v2"):
        parsed = _sort_from_measure_token(
            _attr_local(ss, "measure-to-sort-by"), _attr_local(ss, "direction"),
            ds_default, base_cols, instances, index, ds_caption, worksheet, warnings,
            internal_fields)
        if parsed:
            return parsed
    return None


def _sort_from_measure_token(token, direction, ds_default, base_cols, instances, index,
                             ds_caption, worksheet, warnings, internal_fields):
    """One sort directive's measure token + direction -> the IR sort dict, or ``None``.

    Shared by both spellings of a Tableau axis sort (``<computed-sort using=…>`` and
    ``<shelf-sort-v2 measure-to-sort-by=…>``) so neither can drift from the other.
    """
    if not token:
        return None
    uds, ufid = _split_token_attr(token)
    if ufid is None:
        return None
    by = _resolve_field(uds or ds_default, ufid, base_cols, instances, index,
                        ds_caption, worksheet, warnings, warn_special=False,
                        internal_fields=internal_fields)
    if not by or by["kind"] != "value":
        return None
    direction = (direction or "ASC").strip().upper()
    return {"field": by,
            "direction": "Descending" if direction == "DESC" else "Ascending"}


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
    """Where the Measure Names pill and the Measure Values placeholder sit (shelf / encoding role).

    ``values`` reports the location that decides the SHAPE of the rebuild, which is not simply the
    first one encountered. Tableau's HIGHLIGHT TABLE places Measure Values on BOTH ``text`` (the
    number shown in each cell) and ``color`` (the cell's background), and serialises ``<color>``
    before ``<text>``. Taking whichever came first therefore reported ``color``, the shape decision
    was made as though the numbers were never displayed, and a plain text table with conditional
    formatting was misread as a chart -- then deferred as small multiples, dropping the entire
    worksheet. Ranking the roles instead makes the answer independent of serialisation order: a
    SHELF (rows/cols) outranks ``text``/``label``, which outranks a pure visual encoding like
    ``color`` or ``size``.

    ``values_roles`` additionally reports EVERY role Measure Values occupies, so a caller can see
    that a table is also colour-encoded (and rebuild that as conditional formatting) rather than
    having to choose between the two facts.
    """
    locs = {"names": None, "values": None, "values_roles": set()}
    rank = {"rows": 0, "cols": 0, "text": 1, "label": 1}

    def mark(where, col):
        if not col:
            return
        if ":Measure Names]" in col and locs["names"] is None:
            locs["names"] = where
        if "[Multiple Values]" in col or ":Measure Values]" in col:
            locs["values_roles"].add(where)
            current = locs["values"]
            if current is None or rank.get(where, 2) < rank.get(current, 2):
                locs["values"] = where

    mark("rows", rows_text)
    mark("cols", cols_text)
    holder = _first(pane, "encodings") if pane is not None else None
    if holder is not None:
        for child in list(holder):
            mark(_local(child.tag), child.get("column"))
    return locs


def _placed_instance_ids(rows_text, cols_text, panes):
    """Instance ids this view spends on a NAMED shelf (Rows/Columns) or a pane encoding.

    Encoding children are read generically -- ``color``/``text``/``tooltip``/``size``/``lod``/
    ``shape``/... all carry the pill on a ``column`` attribute -- so a new encoding kind needs no
    change here. Ids are returned in the SAME stripped form the instance table is keyed by
    (``sum:Sales:qk``), because ``<column-instance name>`` keeps its brackets while a shelf token
    does not: comparing the two raw forms misses by exactly one character on every pill.
    """
    out = set()
    for tok in _TOKEN_RE.findall((rows_text or "") + " " + (cols_text or "")):
        _ds, fid = _split_token(tok)
        if fid:
            out.add(_strip_brackets(fid))
    for pane in (panes or ()):
        holder = _first(pane, "encodings")
        if holder is None:
            continue
        for child in list(holder):
            _ds, fid = _split_token_attr(child.get("column"))
            if fid:
                out.add(_strip_brackets(fid))
    return out


def _view_measure_instances(view, ds_default, used_instances=None):
    """Every measure pill the view declares that is NOT placed on a named shelf/encoding.

    This is the member set of an UNFILTERED Measure Values shelf. Tableau records no explicit
    member list for that case -- the shelf carries the pseudo-field ``[Multiple Values]`` and the
    real measures exist only as the view's own ``<column-instance>`` declarations -- so the
    displayed set is exactly "every measure instance this view depends on, minus the ones it
    spends somewhere else".

    ``type="quantitative"`` is the measure test rather than the base column's ``role``: Measure
    Values is a continuous-only container, so a ``role="measure"`` STRING calc (e.g. a
    ``"positive"``/``"negative"`` label driving Colour, serialised ``type="nominal"``) is a
    measure that is *not* a Measure Values member. ``used_instances`` carries the instance ids
    already spent on Rows/Columns/Colour/Size/Text/Tooltip/... so a measure on Tooltip is not
    mistaken for a displayed column.
    """
    used = used_instances or frozenset()
    out, seen = [], set()
    for dep in _findall_local(view, "datasource-dependencies"):
        dsn = dep.get("datasource") or ds_default
        for ci in _children_local(dep, "column-instance"):
            if (ci.get("type") or "").strip().lower() != "quantitative":
                continue
            iid = _strip_brackets(ci.get("name") or "")
            if not iid or iid in used or (dsn, iid) in seen:
                continue
            seen.add((dsn, iid))
            out.append((dsn, iid))
    return out


def _measure_value_member_ids(view, ds_default, used_instances=None):
    """Ordered ``(ds, instance_id)`` Measure Values members, plus an enumeration status.

    Returns ``(members, status)`` where ``status`` is one of:

    - ``"ok"``      -- an authoritative keep-list (a ``<groupfilter function="union" op="manual">``
      whose ``function="member"`` children are the *included* measures, in document = shelf
      order) or, when no such filter is present, the ``<manual-sort>`` dictionary fallback.
    - ``"all"``     -- the Measure Names filter is a bare ``<groupfilter function="level-members">``
      with no member children: Tableau's "every member of the level", which is what it writes the
      moment Measure Names lands on a shelf with nothing filtered out. Members are enumerated from
      the view's own measure declarations and carry no authored order, so the caller sorts them.
    - ``"exclude"`` -- the Measure Names filter is an Exclude / non-manual structure (``except``,
      or a ``level-members`` that itself carries member children), where the listed members are
      the *excluded* set; the displayed set cannot be derived from the workbook alone, so the
      caller must warn + defer rather than show the wrong measures.
    - ``"none"``    -- no member source was found.

    ``except`` and a bare ``level-members`` are OPPOSITES and were previously conflated: ``except``
    lists the REMOVED members, ``level-members`` means every member. Refusing the latter refused
    the most ordinary Measure Names worksheet there is, and -- when it was the only sheet -- left
    the report with zero pages.

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
        # (except / narrowed level-members / non-manual union) is an Exclude action whose member
        # list is the removed set -- reading it as the keep-list would surface the wrong measures.
        # A CHILDLESS level-members narrows nothing at all and is handled separately.
        manual, nonmanual, all_members = None, False, False
        for child in _children_local(filt, "groupfilter"):
            fn = child.get("function")
            op = _attr_local(child, "op")
            if fn == "union" and op == "manual":
                manual = child
            elif fn == "level-members" and not _children_local(child, "groupfilter"):
                all_members = True
            elif fn in ("except", "level-members") or (fn == "union" and op != "manual"):
                nonmanual = True
        if manual is not None:
            mem = members_of(manual)
            if mem:
                return mem, "ok"
        if nonmanual:
            return [], "exclude"
        if all_members:
            mem = _view_measure_instances(view, ds_default, used_instances)
            if mem:
                return mem, "all"
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
                            worksheet, warnings, internal_fields=None,
                            measure_binding=None, row_count_binding=None, column_binding=None,
                            date_binding=None, used_instances=None):
    """Resolve the ordered Measure Values members to value fields.

    Drops numeric-literal dummy spacers (the path-hack constant). Returns
    ``(members, dummy_count, has_param_swap, status)`` where ``status`` is the enumeration
    status from :func:`_measure_value_member_ids`.
    """
    member_ids, status = _measure_value_member_ids(view, ds_default, used_instances)
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
                           worksheet, warnings, internal_fields=internal_fields,
                           measure_binding=measure_binding,
                           row_count_binding=row_count_binding,
                           column_binding=column_binding, date_binding=date_binding)
        if f and f["kind"] == "value":
            members.append(f)
    if status == "all":
        # An unfiltered Measure Names level carries NO authored order -- the member set was
        # recovered from the view's declarations, whose serialised order is Tableau's internal
        # id sort (``cnt:`` < ``none:`` < ``sum:`` < ``usr:``), not the display order. Tableau
        # renders an unsorted Measure Names header alphabetically by caption, so sort on that:
        # it reproduces the source column order instead of scattering the calcs to the end.
        members.sort(key=lambda f: ((f.get("caption") or "").strip().casefold(),
                                    (f.get("caption") or "").strip()))
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


def _parse_row_labels_hidden(table, dims_rows):
    """Did the author hide the ROW LABELS of this sheet? ``True`` / ``False``.

    Deliberately separate from :func:`_parse_hidden_axes` rather than folded into it. That function
    maps a hide onto a Power BI AXIS, which needs the shelf's role, and it resolves cleanly for a
    cartesian chart but yields nothing for a crosstab -- a text table has no category axis to hide,
    so the fact was computed and then dropped. This asks the narrower question the container-stitch
    detector actually needs: is there a ``style-rule[element='label']`` carrying
    ``format[@attr='display'][@value='false']`` that names a field on the ROWS shelf?

    Kept additive and side-effect free so the chart path is untouched: nothing reads this except the
    stitched-table detector, and a sheet with no such rule answers ``False`` exactly as before.
    """
    if table is None or not dims_rows:
        return False
    style = _first(table, "style")
    if style is None:
        return False
    row_keys = set()
    for f in dims_rows:
        row_keys.update(_field_ref_keys(f))
    if not row_keys:
        return False
    for rule in _children_local(style, "style-rule"):
        if (rule.get("element") or "").strip().lower() != "label":
            continue
        for fmt in _children_local(rule, "format"):
            if (fmt.get("attr") or "") != "display":
                continue
            if (fmt.get("value") or "").strip().lower() != "false":
                continue
            for key in _field_ref_keys_from_text(fmt.get("field")):
                if key in row_keys:
                    return True
    return False


def _parse_hidden_axes(table, dims_rows, dims_cols, meas_rows, meas_cols):
    """Which Power BI axes the Tableau author HID (``Show Header`` off).

    Tableau serialises a hidden header/axis as ``format[@attr='display'][@value='false']`` under
    ``table/style/style-rule``. Two spellings occur and BOTH mean "this shelf's axis is hidden":

    * ``@scope='rows'|'cols'`` -- the shelf named directly (typically under ``style-rule[@element
      ='axis']``, i.e. the continuous measure axis).
    * ``@field='<field-id>'`` -- the shelf identified by the field it carries (typically the
      discrete header, serialised under ``style-rule[@element='label']``).

    Either way the scope is mapped to a Power BI axis STRUCTURALLY by the role of the field(s) on
    that shelf, reusing :func:`_parse_axis_titles`' rule: a shelf holding only dimensions drives
    ``categoryAxis``; a shelf holding only measures drives ``valueAxis``. A mixed/empty shelf, an
    unknown field, or any ``value`` other than ``false`` is skipped -- we never guess which axis a
    hide belongs to, and never invent a hide the author did not write.

    This matters well beyond cosmetics: dashboards built as tiled composites (a KPI card whose
    bar strip, dot row and month labels are SEPARATE worksheets stacked in one panel) hide every
    axis so the pieces align. Rendering those axes both adds furniture Tableau never showed and
    steals the plot area, which is what forces a scrollbar into a small tile.

    Returns a set of PBIR axis names to hide (subset of ``{"categoryAxis", "valueAxis"}``).
    """
    if table is None:
        return set()
    style = _first(table, "style")
    if style is None:
        return set()

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
    # field-reference -> shelf, so a hide written against a field resolves to the same role
    # mapping. A key that would map to BOTH shelves is poisoned rather than resolved first-wins:
    # picking one would be a guess, and this pass never guesses which axis a hide belongs to.
    field_scope = {}
    for scope, fields in (("cols", list(dims_cols or []) + list(meas_cols or [])),
                          ("rows", list(dims_rows or []) + list(meas_rows or []))):
        for f in fields:
            for key in _field_ref_keys(f):
                if field_scope.get(key, scope) != scope:
                    field_scope[key] = None
                else:
                    field_scope[key] = scope

    hidden = set()
    for rule in _children_local(style, "style-rule"):
        for fmt in _children_local(rule, "format"):
            if (fmt.get("attr") or "") != "display":
                continue
            if (fmt.get("value") or "").strip().lower() != "false":
                continue
            scope = fmt.get("scope")
            if scope not in ("rows", "cols"):
                scope = None
                for key in _field_ref_keys_from_text(fmt.get("field")):
                    if key in field_scope:
                        scope = field_scope[key]
                        break
            if scope is None:
                continue
            axis = scope_axis.get(scope)
            if axis is not None:
                hidden.add(axis)
    # A CATEGORY HEADER HIDDEN ONLY BECAUSE ITS MEMBERS ARE DRAWN INSIDE THE MARKS MUST STAY.
    # Tableau's horizontal-lollipop idiom turns the row header off and writes each member's NAME
    # into the bar as a mark label (``<customized-label>`` referencing that same dimension). Power
    # BI has no "category name inside the bar" label -- its data labels show the MEASURE -- so
    # honouring the hide deleted the only copy of the names: four unlabelled green bars where the
    # source reads "Sadie Pawthorne / Chuck Magee / ...". Keeping the axis moves the names beside
    # the bars instead of inside them, which loses placement but not information.
    if "categoryAxis" in hidden and _members_drawn_as_labels(table, dims_rows, dims_cols):
        hidden.discard("categoryAxis")
    return hidden


def _members_drawn_as_labels(table, dims_rows, dims_cols):
    """True when a pane's ``<customized-label>`` prints one of the axis DIMENSIONS as a mark label.

    That is Tableau saying "the member names are inside the marks", which is why the author could
    turn the header off. Only an AXIS dimension counts -- a label naming some other field is
    ordinary annotation and says nothing about the category header.
    """
    keys = set()
    for f in list(dims_rows or []) + list(dims_cols or []):
        keys.update(_field_ref_keys(f))
    if not keys:
        return False
    for label in _findall_local(table, "customized-label"):
        for run in _findall_local(label, "run"):
            for key in _field_ref_keys_from_text(run.text or ""):
                if key in keys:
                    return True
    return False


# A Tableau field reference is written as a bracketed path, e.g. ``[federated.abc].[mn:date:ok]``.
_FIELD_SEG_RE = re.compile(r"\[([^\[\]]+)\]")


def _field_ref_keys(field):
    """Match keys for a parsed shelf entry (tolerant of the str / dict shapes in use).

    A parsed shelf entry is a dict carrying BOTH an ``instance`` (the shelf-level spelling, e.g.
    ``mn:date:ok`` -- what a style rule names) and a ``field_id`` (the underlying column, e.g.
    ``date``). Both are offered as keys so a rule written against either spelling resolves.
    """
    if isinstance(field, str):
        return _field_ref_keys_from_text(field)
    keys = set()
    if isinstance(field, dict):
        for key in ("instance", "field_id", "id", "column", "field", "name"):
            v = field.get(key)
            if isinstance(v, str) and v.strip():
                keys.update(_field_ref_keys_from_text(v))
    return keys


def _field_ref_keys_from_text(text):
    """Match keys for a field reference as written in the workbook XML.

    Yields the reference verbatim AND its final bracketed segment, so the qualified form the XML
    uses (``[federated.abc].[mn:date:ok]``) matches the unqualified ``instance`` a parsed shelf
    entry carries. Returns an empty set for a missing / blank reference.
    """
    if not isinstance(text, str):
        return set()
    text = text.strip()
    if not text:
        return set()
    keys = {text}
    segs = _FIELD_SEG_RE.findall(text)
    if segs:
        keys.add(segs[-1].strip())
    return keys


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
    # Tableau encodes a title's LAYOUT MARKERS with the sentinel ``\u00c6`` (Æ) -- the same idiom
    # ``_zone_text`` already scrubs for dashboard text zones, but it was never scrubbed here, so a
    # customer status band rebuilt as ``Region =Æ Big South``.
    #
    # ``Æ`` is ALSO a real letter, so the scrub has to be narrow or it mutilates legitimate
    # Danish/Norwegian text (``Ærø Sales``). Two shapes are markers and nothing else is touched:
    #   * ``Æ`` immediately before a hard newline -- the long-known line-break sentinel; and
    #   * a run whose ENTIRE content is ``Æ`` plus whitespace -- a separator/spacer run, which no
    #     real word can be (a word carries its other letters in the same run).
    # Surrounding whitespace is preserved either way, so the spacing the author saw is kept.
    parts = []
    for r in runs:
        rt = r.text or ""
        if rt.strip() == "\u00c6":
            rt = rt.replace("\u00c6", "")
        parts.append(rt)
    text = "".join(parts).replace("\u00c6\n", "\n").strip()
    if not text:
        return None, False
    return text, bool(_TITLE_DYNAMIC_RE.search(text))


# A Tableau KPI ("BAN" / big-number) worksheet embeds its headline number IN the title: a static
# caption run plus a LARGE dynamic field-ref run (the live measure), drawn above a small sparkline
# mark. Power BI cannot embed a live measure in a container title, so the number is rebuilt as a
# real ``card`` bound to that measure (see ``_emit_kpi_title_card``) while the worksheet's own visual
# keeps the sparkline. A whole ``<[ds].[field]>`` run at >= this point size is the number; a small
# inline ``<[Parameters]...>`` token (a parameter woven into a caption) is NOT -- it stays deferred.
_KPI_TITLE_MIN_SIZE = 18.0
_TITLE_FULL_REF_RE = re.compile(r"^<(\[[^<>\[\]]*\]\.\[[^<>\[\]]*\])>$")
# Tableau's documented worksheet-title point size, used when the workbook is silent at every
# cascade layer. Kept as a named fallback so the resolver below never invents a number.
_WORKSHEET_TITLE_DEFAULT_SIZE = 15.0


def _points(value):
    """A point size from either a raw Tableau ``fontsize`` ('12') or a PBIR literal ('12D') -> float.

    ``None`` for anything non-numeric or non-positive, so callers can distinguish "declared" from
    "silent" -- which is the whole basis of :func:`_title_run_size`.
    """
    s = str(value or "").strip()
    if s[-1:] in ("D", "d"):
        s = s[:-1]
    try:
        n = float(s)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _title_default_size(ws):
    """The point size a title ``<run>`` renders at when it declares no ``fontsize`` of its own.

    Tableau writes ``fontsize`` on a run ONLY when it differs from the title's resolved default. So
    the run carrying a KPI's HEADLINE NUMBER -- the one the author leaves alone while SHRINKING the
    caption around it -- carries no ``fontsize`` attribute at all, and any rule that reads the raw
    attribute sees ``0``. Resolving the default through the same font cascade every other element
    uses (workbook -> worksheet -> ``worksheet-title`` rule, over Tableau's documented 15pt) is what
    makes a run's RENDERED size knowable, and therefore comparable.
    """
    table = _first(ws, "table") if ws is not None else None
    style = _first(table, "style") if table is not None else None
    resolved = _resolve_element_font(style, "worksheet-title") or {}
    return _points(resolved.get("font_size")) or _WORKSHEET_TITLE_DEFAULT_SIZE


def _title_run_size(run, default_size):
    """One title run's RENDERED point size: its own ``fontsize``, else the title default."""
    return _points(run.get("fontsize")) or default_size


def _run_visible_text(run):
    """A title run's visible text, with Tableau's layout sentinels removed.

    ``U+00C6`` (soft-return / spacer marker) and ``U+FFFD`` carry no glyph of their own, so a run
    made only of them is empty for the purpose of "is anything else on this line".
    """
    return (run.text or "").replace("\u00c6", "").replace("\ufffd", "").strip()


def _detect_kpi_title_cards(ws):
    """Every KPI headline number a worksheet's title carries -> a list of card specs (may be empty).

    One entry PER TITLE LINE, in document order, each
    ``{"caption", "ref", "value_color", "value_size", "caption_style"}``. Tableau writes a
    multi-metric KPI as several lines in ONE title ("Current: <this year>" / "vs Last Year: <delta>"),
    which no single Power BI card can hold; taking only the first reference silently dropped every
    metric after it. Splitting on the line breaks the author already wrote keeps each number with the
    label it belongs to.

    The signature per line: a whole-run dynamic ``<[ds].[Calculation...]>`` reference -- NOT a
    ``[Parameters]`` reference (a parameter woven into a caption is a different, deferred case) --
    that reads as a HEADLINE NUMBER rather than as a token inside a sentence, plus the static caption
    runs on that same line (cleaned of the ``U+FFFD`` replacement char and the hidden white ``_``
    spacer run). ``value_color`` / ``value_size`` carry the reference run's authored colour and its
    RENDERED point size; ``caption_style`` the same for its label.

    WHAT MAKES A REFERENCE A HEADLINE. Gating on an explicit ``fontsize >= 18`` missed the entire
    family, because Tableau omits ``fontsize`` on a run that uses the title's own default: three KPI
    cards in one workbook came out titled with the SHEET NAME ("Bar Chart", "Sheet 6") and no number
    anywhere. Sizes are therefore resolved to what each run RENDERS at, and either of two
    file-grounded signals qualifies a reference:

      * it renders LARGER than every static caption run -- the author shrank the label to make the
        number the headline ("Days Left In Sales Year" at 12pt over a 15pt number); or
      * it stands ALONE on its own line -- Tableau's caption-over-number layout, which is a headline
        even when both lines share one size ("Total Sales" / "2,326,534", both 15pt).

    An inline, no-larger reference ("Sales for <Region>") is a caption with a live token, not a KPI,
    and is left to the dynamic-title path.
    """
    layout = _first(ws, "layout-options")
    title = _first(layout, "title") if layout is not None else None
    ft = _first(title, "formatted-text") if title is not None else None
    if ft is None:
        return []
    runs = _findall_local(ft, "run")
    default_size = _title_default_size(ws)

    def _is_ref(run):
        return bool(_TITLE_FULL_REF_RE.match((run.text or "").strip()))

    # A run whose text carries a hard newline is Tableau's line break between title lines. Split the
    # run list on them so each line's label and number stay together.
    lines = []
    current = []
    for r in runs:
        current.append(r)
        if "\n" in (r.text or ""):
            lines.append(current)
            current = []
    if current:
        lines.append(current)

    caption_size = max(
        [_title_run_size(r, default_size) for r in runs
         if _run_visible_text(r) and not _is_ref(r)],
        default=0.0)

    def _size_literal(pts):
        return "{0}D".format(int(pts) if pts == int(pts) else pts)

    out = []
    for line in lines:
        number = None
        number_idx = None
        for idx, r in enumerate(line):
            m = _TITLE_FULL_REF_RE.match((r.text or "").strip())
            if not m or "[Parameters]" in m.group(1):
                continue
            size = _title_run_size(r, default_size)
            alone = not any(_run_visible_text(line[j])
                            for j in range(len(line)) if j != idx)
            if size >= _KPI_TITLE_MIN_SIZE or size > caption_size or alone:
                number, number_idx = r, idx
                break
        if number is None:
            continue
        ref = _TITLE_FULL_REF_RE.match((number.text or "").strip()).group(1)

        # The label is the static text BEFORE the number on this line ("Current: "), falling back to
        # the preceding line when this line is the number alone ("Total Sales" / "2,326,534") -- but
        # only when that line contributed no card of its own, so a two-metric title never reuses one
        # label twice.
        cap_runs = [r for r in line[:number_idx]
                    if _run_visible_text(r) and (r.get("fontcolor") or "").lower() != "#ffffff"]
        if not cap_runs and not out:
            prev = lines[lines.index(line) - 1] if lines.index(line) > 0 else []
            cap_runs = [r for r in prev
                        if _run_visible_text(r) and not _is_ref(r)
                        and (r.get("fontcolor") or "").lower() != "#ffffff"]
        caption = re.sub(
            r"\s+", " ",
            "".join(r.text or "" for r in cap_runs).replace("\ufffd", " ").replace("\u00c6", " ")
        ).strip()
        if not caption:
            continue

        color = number.get("fontcolor")
        color = color if (color and _HEX6_RE.match(color)) else None
        # Sizes are the RENDERED ones, not the declared ones. A ``None`` here is not "keep it small"
        # -- it is "let Power BI choose", and Power BI chooses a ~45pt card callout and its own
        # container title size, which is how a faithful 300x300 Tableau KPI tile came out as one
        # oversized number with the caption crowded off the plate. Emitting the size the source
        # actually renders at is what keeps the rebuilt tile in proportion.
        cap_size = min([_title_run_size(r, default_size) for r in cap_runs], default=None)
        cap_colors = {(r.get("fontcolor") or "").lower() for r in cap_runs if r.get("fontcolor")}
        caption_style = {}
        if cap_size:
            caption_style["font_size"] = _size_literal(cap_size)
        if len(cap_colors) == 1 and _HEX6_RE.match(next(iter(cap_colors))):
            caption_style["font_color"] = next(iter(cap_colors))
        if cap_runs and all(r.get("bold") == "true" for r in cap_runs):
            caption_style["bold"] = True
        # THE TREND ARROW IS PART OF THE LINE. Tableau writes a delta KPI as
        # "vs Last Year: <number> <Arrow Up><Arrow down>", where the two arrow calcs return a glyph
        # or "" so exactly one shows. They are measures, but STRING ones -- a card bound to one
        # would print a lone arrow as if it were the metric -- so they are carried here as trailing
        # GLYPH references, rebuilt beside the number rather than in place of it. Dropping them (the
        # previous behaviour) silently removed the up/down indicator the KPI exists to show.
        glyphs = []
        for r in line[number_idx + 1:]:
            m = _TITLE_FULL_REF_RE.match((r.text or "").strip())
            if not m or "[Parameters]" in m.group(1):
                continue
            gcolor = r.get("fontcolor")
            glyphs.append({"ref": m.group(1),
                           "color": gcolor if (gcolor and _HEX6_RE.match(gcolor)) else None,
                           "size": _size_literal(_title_run_size(r, default_size))})
        out.append({"caption": caption, "ref": ref, "value_color": color,
                    "value_size": _size_literal(_title_run_size(number, default_size)),
                    "caption_style": caption_style or None,
                    "glyphs": glyphs})
    return out


def _detect_kpi_title_card(ws):
    """The FIRST KPI headline number in a worksheet's title, or ``None``.

    Thin wrapper over :func:`_detect_kpi_title_cards` for callers that only ever handled one.
    """
    cards = _detect_kpi_title_cards(ws)
    return cards[0] if cards else None


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


def _parse_title_style(ws, static_only=False):
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

    ``static_only`` drops the whole-run field references from the sample. Once a KPI's headline
    number has been lifted out into its own card, those runs are no longer part of the caption the
    container title shows -- and leaving them in is what made the surviving caption defer its size:
    the number run declares no ``fontsize`` (it uses the title default) while the caption declares
    one, so ``_uniform`` saw a partial declaration, emitted nothing, and Power BI applied its own
    much larger default to a 300x300 tile.
    """
    layout = _first(ws, "layout-options")
    title = _first(layout, "title") if layout is not None else None
    ft = _first(title, "formatted-text") if title is not None else None
    if ft is None:
        return None
    runs = _findall_local(ft, "run")
    # Tableau's layout sentinels (``Æ`` before a soft return, ``U+FFFD``) carry no glyph, so a run
    # made only of them is not TEXT -- but it declares no font either, and counting it as a text run
    # made ``_uniform`` see a partial declaration and defer every property the real runs agreed on.
    text_runs = [r for r in runs if _run_visible_text(r)]
    if static_only:
        text_runs = [r for r in text_runs
                     if not _TITLE_FULL_REF_RE.match((r.text or "").strip())]
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


def _is_view_level_calc(field):
    """True when a resolved field is a VIEW-level table calc rather than a model measure.

    Two independent spellings: a quick-table-calc pill (the instance token's leading code), and a
    hand-written formula whose head is a table-calc function (``TOTAL`` / ``RUNNING_*`` /
    ``WINDOW_*`` / ``LOOKUP`` / ``FIRST`` / ``LAST`` / ``INDEX`` / ``RANK*`` / ``SCRIPT_*``). Both
    compute over the VIEW's own partition, so neither survives as a standalone model measure -- they
    translate to inert stubs. An ordinary aggregate or LOD (``{SUM([Sales])}``,
    ``DATEDIFF('day', TODAY(), {MAX([Order Date])})``) is NOT one of these and binds directly.
    """
    return bool(_instance_is_table_calc(field.get("instance"))
                or _table_calc_filter_idioms(field.get("formula")))


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
# exact ramp cannot be recovered from the workbook XML -- it is a CURATED CONSTANT, disclosed via
# ``default_palette`` (warn-never-wrong), not a parse.
#
# The stops below are MEASURED from Tableau's own rendered output, replacing an earlier pair of
# generic ColorBrewer stand-ins ("Blues" / "RdBu") that had the right DIRECTION but the wrong HUE.
# Two corpus workbooks serialise an ``interpolated`` colour encoding with no palette AND ship a
# reference render of what Tableau actually drew; pixel-sampling both (skipping neutral chrome) gives
# one coherent GREEN family, not blue:
#
#   * positive-only measure  (0063, SUM(Sales) on a filled map): every mark is GREEN --
#     palest ``#dde4bc`` through mid ``#95cb7d`` to dark green. NO red appears at all.
#   * signed measure         (0064, SUM(Profit) on a bar chart): dark green ``#076229`` at the
#     maximum (+38.4k), fading through near-white around zero, to red ``#cc1617`` at the minimum
#     (-25.1k). Mid greens ``#257f36`` / ``#73a86b`` and mid reds ``#dd4738`` / ``#f57d69`` sit
#     between, so it is a continuous ramp, not a two-tone split.
#
# ONE palette explains both: red at the negative extreme, near-white at zero, green at the positive
# extreme -- an all-positive measure simply never reaches the red arm, which is exactly the
# single-hue green 0063 shows. The sequential pair is therefore that palette's white->green arm, so
# the two constants stay in the same family instead of disagreeing.
#
# CONFIDENCE: two workbooks, two reference renders, pixel-sampled -- a curated constant, and the
# corpus's own rule table classifies "Tableau's built-in default palette endpoints" as exactly that
# (not derivable from the XML; needs a curated table plus a confidence flag). The DIRECTION
# (low -> light, high -> dark) is unchanged and was never in doubt.
#
# Provenance: original work -- measured from reference renders in our own corpus. The reference tool
# cyphou/Tableau-To-PowerBI keys all colour handling on an explicit ``<color-palette>`` and has no
# default-ramp handling, so this default-synthesis path is entirely ours.
_DEFAULT_SEQUENTIAL_COLORS = ("#dde4bc", "#076229")
_DEFAULT_DIVERGING_COLORS = ("#cc1617", "#f7f7f7", "#076229")

# Tableau also ships BUILT-IN NAMED continuous palettes (e.g. ``orange_blue_diverging_10_0``) that it
# serialises by NAME + ``min`` / ``max`` / ``reverse`` only -- NO explicit ``<color-palette>`` stops --
# so the exact ramp is unrecoverable from the workbook XML just like the unnamed automatic default.
# The palette NAME still carries recoverable author intent: its hue tokens (orange <-> blue, red <->
# green, ...) name the two ends, and a ``diverging`` name (or a domain straddling zero) is a diverging
# scale. These single-hue anchors are published Tableau-10 palette facts, sourced independently, and
# reconstruct a DISCLOSED stand-in that matches the author's chosen ends and direction (warn-never-
# wrong). The neutral middle is Tableau-10 grey; both were render-verified against the source
# dashboard. The reference tool cyphou/Tableau-To-PowerBI drops named continuous palettes entirely,
# so this named-palette reconstruction is entirely ours.
_NAMED_HUE_STOPS = {
    "orange": "#f28e2b", "blue": "#4e79a7", "red": "#d62728", "green": "#59a14f",
    "purple": "#b07aa1", "brown": "#9c755f", "teal": "#4e9caf", "gold": "#edc948",
    # Midpoint hues. Tableau names a diverging palette after its two ENDS and then its MIDDLE
    # (``red_blue_white_diverging`` = red .. white .. blue), so the third token is frequently a
    # neutral that never appears as an endpoint. Without these the token is unrecognised and the
    # whole name silently degrades to the generic default ramp.
    "white": "#ffffff", "grey": "#bab0ac", "gray": "#bab0ac",
}
_NAMED_NEUTRAL_MID = "#bab0ac"


def _parse_gradient_center(enc):
    """The numeric ``center`` attribute of a colour encoding, or ``None`` when absent/unparseable."""
    raw = enc.get("center")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_gradient_bounds(enc):
    """The numeric ``(min, max)`` domain of a colour encoding; each is ``None`` when absent or
    unparseable. A domain that straddles zero (``min < 0 < max``) marks a diverging scale centred
    at zero even when Tableau serialised no explicit ``center``."""
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return _f(enc.get("min")), _f(enc.get("max"))


def _named_family_stops(name, diverging):
    """Reconstruct a DISCLOSED stand-in ramp from the hue tokens of a Tableau built-in continuous
    palette NAME (``orange_blue_diverging`` -> orange .. grey .. blue). Author token order is
    preserved (first -> min, last -> max). Returns ``None`` when no hue is recognised, so the caller
    falls back to the generic ColorBrewer default; never guesses beyond the named hues.

    A THREE-hue diverging name is read as ``<low>_<high>_<middle>``: Tableau names such a palette
    after its two ENDS and then its MIDDLE, so ``red_blue_white_diverging`` is red .. white .. blue
    and ``red_green_gold_diverging`` is red .. gold .. green. Reading it as "first .. neutral ..
    last" instead put the wrong hue at the top end and discarded the author's midpoint for grey --
    a rank column the author coloured green-through-gold-to-red rebuilt as red-through-grey-to-gold,
    which inverts the reader's good/bad intuition. Verified two ways on a customer highlight table:
    the encoding carries ``reverse='true'`` over ``red_green_gold_diverging_10_0``, and the source
    render's cell backgrounds run green (#b0ceba) -> gold (#fbefc8) -> red (#eebcbc) across the
    ranks -- i.e. the un-reversed palette is red .. gold .. green, exactly this reading.

    TWO-hue diverging names keep the neutral middle (``orange_blue_diverging`` -> orange .. grey ..
    blue), which is unchanged.
    """
    tokens = []
    for tok in re.split(r"[^a-z]+", (name or "").lower()):
        if tok in _NAMED_HUE_STOPS and tok not in tokens:
            tokens.append(tok)
    if diverging:
        if len(tokens) >= 3:
            return [_NAMED_HUE_STOPS[tokens[0]], _NAMED_HUE_STOPS[tokens[2]],
                    _NAMED_HUE_STOPS[tokens[1]]]
        if len(tokens) == 2:
            return [_NAMED_HUE_STOPS[tokens[0]], _NAMED_NEUTRAL_MID, _NAMED_HUE_STOPS[tokens[1]]]
        return None
    if tokens:
        return ["#f7f7f7", _NAMED_HUE_STOPS[tokens[-1]]]
    return None


def _default_continuous_gradient(enc):
    """Synthesise a continuous-gradient spec for a colour encoding that is continuous
    (``interpolated``) but carries NO explicit ``<color-palette>`` -- either the author kept
    Tableau's unnamed automatic ramp, or chose a built-in NAMED palette
    (e.g. ``orange_blue_diverging_10_0``) serialised by name only. A ``center``, a name containing
    ``diverging``, or a domain straddling zero all mark a DIVERGING scale centred at the domain
    midpoint (zero when it straddles zero); otherwise the ramp is sequential. The hue family is read
    from the palette name so the stand-in matches the author's ends, and ``reverse='true'`` flips
    min <-> max. Flags ``default_palette`` so the emitter discloses the approximation
    (warn-never-wrong).
    """
    name = enc.get("palette") or ""
    center = _parse_gradient_center(enc)
    lo, hi = _parse_gradient_bounds(enc)
    spans_zero = lo is not None and hi is not None and lo < 0 < hi
    diverging = center is not None or "diverging" in name.lower() or spans_zero
    if center is None and diverging:
        # NEVER invent a centre out of thin air. A fabricated centre is not a cosmetic default: the
        # emitter pins the mid stop at that literal value, so if it falls OUTSIDE the data range the
        # whole dataset lands on one side of it and only HALF the ramp is ever used. Pinning an
        # unknown centre at 0 did exactly that to every all-positive measure -- a rank column over
        # 1..13 rendered gold-through-red with the green end unreachable, because 0 sits below the
        # minimum. Measured on a customer highlight table, and confirmed by the fix a reader reached
        # for by hand in Desktop: tick "Add a middle color" and set Center to the middle of the range
        # (6.5 for 1..13) and the authored green -> gold -> red returns.
        #
        # So a centre is emitted only when it is KNOWN: the author pinned one (handled above), or the
        # declared domain straddles zero (a genuinely signed measure diverges about 0), or the domain
        # is declared and its midpoint is therefore computable. With none of those, leave it None --
        # `_gradient_color_stops` then omits the value and Power BI centres the mid stop on the data
        # midpoint, which is precisely what Tableau does for a diverging palette whose centre the
        # author never set.
        if spans_zero:
            center = 0.0
        elif lo is not None and hi is not None:
            center = (lo + hi) / 2.0
    colors = _named_family_stops(name, diverging)
    if colors is None:
        colors = list(_DEFAULT_DIVERGING_COLORS if diverging else _DEFAULT_SEQUENTIAL_COLORS)
    if (enc.get("reverse") or "").strip().lower() == "true":
        colors = list(reversed(colors))
    _, fid = _split_token_attr(enc.get("field"))
    return {
        "field_token": enc.get("field") or "",
        "center": center,
        "palette_type": "ordered-diverging" if diverging else "ordered-sequential",
        "colors": colors,
        "interpolated": True,
        "is_table_calc": _instance_is_table_calc(fid),
        "default_palette": True,
    }


# Tableau's AUTOMATIC continuous colour ramp: when a MEASURE is dropped on the Color shelf and the
# author leaves the palette on "Automatic", the worksheet carries the colour ENCODING
# (``<encodings><color column=.../>``) but the FORMAT ``<style>`` block holds NO ``<color-palette>``
# and NO min/max/center -- an empty or format-only style. ``_parse_color_gradient`` (which reads only
# the style cascade) therefore returns ``None`` and the colour channel is dropped silently even though
# a continuous measure is unambiguously colour-encoded. Tableau renders that automatic default as an
# ORANGE-BLUE DIVERGING ramp centred at zero (orange for negatives, blue for positives) that collapses
# to a single hue when the data does not cross zero -- documented Tableau behaviour, render-verified.
# Pinning the synthesised centre at literal 0 lets Power BI's own domain computation reproduce that
# adaptive behaviour without the workbook carrying a data range: signed data renders diverging about 0,
# one-signed data renders the single relevant half (neutral -> blue, or orange -> neutral). The hues
# are the Tableau-10 orange / blue anchors already used for named-palette reconstruction; the middle is
# Tableau-10 grey. DISCLOSED via ``default_palette`` (warn-never-wrong). Provenance: original work --
# the reference tool cyphou/Tableau-To-PowerBI keys all colour handling on an explicit ``<color-palette>``
# and has no automatic-default synthesis, so this path is entirely ours.
_TABLEAU_AUTO_NEG = _NAMED_HUE_STOPS["orange"]
_TABLEAU_AUTO_POS = _NAMED_HUE_STOPS["blue"]


def _automatic_color_gradient(color_enc):
    """Synthesise Tableau's automatic default continuous ramp for a MEASURE on the Color shelf that
    carries no explicit ``<style>`` palette.

    ``color_enc`` is the resolved colour ENCODING (from ``_parse_encodings``); the caller only invokes
    this for a continuous measure (``kind == "value"`` with an ``aggregation`` / ``measure`` binding).
    Returns a gradient spec shaped exactly like ``_parse_color_gradient`` -- a DIVERGING orange-blue
    ramp centred at zero, flagged ``default_palette`` (and ``automatic_default``) for disclosure. The
    ``is_table_calc`` flag is read from the colour pill's instance token so a quick-table-calc colour
    driver (e.g. a ``pcdf`` percent-difference heat scale) is still recognised downstream and lit up
    through the Visual-Calculation path rather than mis-bound to a base measure."""
    inst = color_enc.get("instance") or ""
    return {
        "field_token": inst,
        "center": 0.0,
        "palette_type": "ordered-diverging",
        "colors": [_TABLEAU_AUTO_NEG, _NAMED_NEUTRAL_MID, _TABLEAU_AUTO_POS],
        "interpolated": True,
        "is_table_calc": _instance_is_table_calc(inst),
        "default_palette": True,
        "automatic_default": True,
    }


def _color_card_instances(root, worksheet_name):
    """Instance tokens of every pill on this worksheet's COLOUR shelf.

    Tableau records the Colour shelf as ``<card type='color'>`` entries under
    ``workbook/windows/window[@name=<worksheet>]/cards/...`` -- NOT in the worksheet element, and not
    alongside the ``<style>`` palettes. That distinction matters: the cards are the authoritative
    record of WHICH measures are colour-encoded, while a ``<style-rule>`` ``<encoding attr='color'>``
    only supplies a palette for those the author explicitly styled. A measure with a card but no
    encoding is still coloured -- Tableau just paints it with the automatic default ramp.

    Reading only the encodings therefore UNDER-counts a highlight table. Measured on a real
    workbook: 8 measures sat on the Colour shelf but only 5 carried explicit palettes, so three
    columns that are visibly shaded in Tableau rebuilt with no fill at all.

    Returns a set of raw instance tokens (e.g. ``usr:Calculation_123:qk``); empty when the workbook
    records no cards for this worksheet, which keeps every previously-handled sheet byte-identical.
    """
    out = set()
    if root is None or not worksheet_name:
        return out
    for window in _findall_local(root, "window"):
        if (window.get("name") or "") != worksheet_name:
            continue
        for card in window.iter():
            if _local(card.tag) != "card" or (card.get("type") or "") != "color":
                continue
            _, fid = _split_token_attr(card.get("param"))
            if fid:
                out.add(fid)
    return out


def _parse_color_gradients_by_field(table):
    """Every per-field continuous colour scale on a worksheet: ``{field_token: gradient_spec}``.

    ``_parse_color_gradient`` answers "what is THE colour scale for this chart" and returns the
    first one, which is right for a chart whose marks share one legend. A Tableau HIGHLIGHT TABLE
    with ``separate-domains='true'`` is the opposite case: Measure Values sits on Colour and each
    member measure carries its OWN palette and its OWN domain, so the worksheet holds one colour
    encoding PER measure. Collapsing those to a single scale would paint every column from one
    palette -- visibly wrong wherever the author chose different hues per metric, which is the whole
    point of the idiom.

    Keyed by the encoding's raw ``field`` token so a caller can match each scale to the projection
    built from the same pill. Only encodings that yield a real gradient are included, so a miss is
    simply an absent key.
    """
    out = {}
    if table is None:
        return out
    style = _first(table, "style")
    if style is None:
        return out
    for rule in _children_local(style, "style-rule"):
        if (rule.get("element") or "").lower() != "mark":
            continue
        for enc in _children_local(rule, "encoding"):
            if (enc.get("attr") or "") != "color":
                continue
            token = enc.get("field") or ""
            if not token or token in out:
                continue
            spec = _gradient_from_encoding(enc)
            if spec is not None:
                out[token] = spec
    return out


def _gradient_from_encoding(enc):
    """One colour encoding -> a gradient spec, or ``None`` when it carries no continuous scale.

    Extracted from ``_parse_color_gradient`` so the single-scale and per-measure readers share one
    definition of what a gradient IS and can never diverge on palette handling.
    """
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
    if interpolated:
        return _default_continuous_gradient(enc)
    return None


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


def _split_field_ref(text):
    """Split a Tableau field reference into ``(qualifier, inner_token)``.

    ``[federated.abc].[none:airline_name:nk]`` -> ``("federated.abc", "none:airline_name:nk")``.
    An unqualified reference (``[none:airline_name:nk]``, the spelling used INSIDE a datasource,
    which is already scoped) returns ``(None, "none:airline_name:nk")``.
    """
    segs = _FIELD_SEG_RE.findall(text or "")
    if not segs:
        return (None, (text or "").strip())
    return ((segs[0].strip() if len(segs) >= 2 else None), segs[-1].strip())


def _datasource_mark_color_palettes(root):
    """Workbook-wide categorical colour palettes stored on the DATASOURCE, keyed by
    ``(datasource_name, colour_field_token)``.

    Tableau writes an explicit member->colour map ONCE on ``<datasource><style>`` whenever the
    assignment is shared across worksheets -- which is the normal case for a consistently styled
    dashboard -- and then omits it from each worksheet. :func:`_parse_mark_colors` reads only the
    worksheet-local copy, so on such a workbook every palette went unseen and every visual fell
    back to theme colours. This reader supplies the shared definition; the worksheet-local copy
    still WINS where both exist, being the more specific statement.

    The key carries the datasource name because a workbook with several datasources can legitimately
    define different palettes for same-named fields; the worksheet's colour encoding is written in
    the qualified ``[datasource].[token]`` form, so the match is EXACT and never a guess. A
    ``[Measure Names]`` palette is deliberately excluded -- it colours by measure identity, not by a
    dimension member, and has its own reader (:func:`_parse_measure_color_palette`) and its own
    metadata-selector emit path.

    Returns ``{(ds_name, token): [{"value", "color"}]}`` with Tableau author order preserved.
    """
    palettes = {}
    datasources = []
    for holder in _children_local(root, "datasources"):
        datasources.extend(_children_local(holder, "datasource"))
    if not datasources and _local(root.tag) == "datasource":
        datasources = [root]
    for ds in datasources:
        ds_name = ds.get("name")
        style = _first(ds, "style")
        if style is None:
            continue
        for rule in _children_local(style, "style-rule"):
            if (rule.get("element") or "").lower() != "mark":
                continue
            for enc in _children_local(rule, "encoding"):
                if (enc.get("attr") or "") != "color":
                    continue
                if _first(enc, "color-palette") is not None:
                    continue  # continuous gradient -> _parse_color_gradient
                _, token = _split_field_ref(enc.get("field"))
                if not token or "Measure Names" in token:
                    continue
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
                if not members:
                    continue
                key = (ds_name, token)
                prior = palettes.get(key)
                if prior is not None and prior != members:
                    palettes[key] = None  # conflicting definitions -> abstain, never guess
                elif prior is None and key in palettes:
                    continue
                else:
                    palettes[key] = members
    return {k: v for k, v in palettes.items() if v}


def _pane_color_columns(panes):
    """Every ``<color column=...>`` reference on the given panes, in author order (deduped)."""
    out = []
    for pane in panes or ():
        for encs in _children_local(pane, "encodings"):
            for enc in _children_local(encs, "color"):
                col = (enc.get("column") or "").strip()
                if col and col not in out:
                    out.append(col)
    return out


def _parse_mark_colors(table, ds_palettes=None, color_columns=()):
    """Extract an explicit categorical colour palette (member -> hex) for a worksheet.

    Returns ``{"field_token", "members": [{"value", "color"}]}`` when an explicit discrete
    ``<map to='#hex'><bucket>...</bucket></map>`` palette of at least one member is found, else
    ``None``. A continuous ``<color-palette>`` gradient (handled by ``_parse_color_gradient``) and a
    bare single ``mark-color`` default are both ignored -- only an explicit per-member map is an
    unambiguous author colour assignment. Tableau author order is preserved.

    The worksheet's own ``table/style`` is consulted FIRST (the most specific statement). Failing
    that, the worksheet's mark colour encodings are resolved against the workbook's shared
    datasource-level palettes (see :func:`_datasource_mark_color_palettes`), which is where Tableau
    actually stores the assignment on a consistently styled multi-sheet dashboard. Colour encodings
    are tried in author order and the first that resolves wins; nothing is inferred for a colour
    field the author never assigned.
    """
    if table is not None:
        style = _first(table, "style")
        if style is not None:
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
    if not ds_palettes:
        return None
    for column in color_columns or ():
        qualifier, token = _split_field_ref(column)
        members = ds_palettes.get((qualifier, token))
        if members:
            return {"field_token": column, "members": [dict(m) for m in members]}
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


def _parse_label_slots(all_panes):
    """Tableau's mark-label TEMPLATE -> the ordered list of DISPLAY SLOTS it actually draws.

    A KPI "BAN" is one mark whose ``<customized-label><formatted-text>`` arranges many pills into a
    laid-out block. The template is authoritative and structural -- it is the only place that says
    which pills share a slot::

        "All Passengers"        fontcolor=#666666                  static caption
        "\\n"
        <CM Count On-Time...>   bold fontsize=14 #333333           the BIG NUMBER
        "\\n\\n"
        <Pos MoM Load Factor>   #49964f  green   |  ADJACENT field runs with
        <Pos MoM Passenger>     #e63946  red     |  NO separator text between them
        <Neut MoM Passenger>    #b4b4b4  grey    |  => ONE display slot
        <Neg MoM Passenger>     #e63946  red     |
        " "
        <Calculation_2009...>   #898989 fontsize=8                 a footnote

    THE RULE: runs are split into groups by any run carrying visible text; a maximal run of
    CONSECUTIVE field runs is ONE display slot. Tableau writes mutually exclusive alternatives
    (exactly one non-blank per period, its colour carrying the direction) adjacently precisely
    because they occupy the same position on the card. Binding them as sibling values instead
    renders the dead alternatives as ``(Blank)`` rows.

    This is deliberately STRUCTURAL: no name matching. A ``Pos``/``Neg`` prefix convention is one
    author's habit -- and matching on it here would have silently missed this workbook's third
    member, ``Neut``. Adjacency is what Tableau actually serialises.

    Returns a list of slot dicts ``{"tokens", "colors", "bold", "size", "text"}`` -- ``tokens``
    empty for a static-text slot -- or ``None`` when the pane carries no mark-label template.
    """
    for p in all_panes or []:
        cl = _first(p, "customized-label")
        ft = _first(cl, "formatted-text") if cl is not None else None
        if ft is None:
            continue
        slots, group = [], None
        for run in _findall_local(ft, "run"):
            text = run.text or ""
            tokens = _TOKEN_RE.findall(text)
            if tokens:
                color = (run.get("fontcolor") or "").strip()
                if group is None:
                    group = {"tokens": [], "colors": [], "bold": False, "size": None, "text": ""}
                    slots.append(group)
                group["tokens"].extend(tokens)
                group["colors"].extend([color if _HEX6_RE.match(color) else None] * len(tokens))
                if (run.get("bold") or "").strip().lower() == "true":
                    group["bold"] = True
                size = _font_size_points(run.get("fontsize"))
                if size and group["size"] is None:
                    group["size"] = size
                continue
            # A run with no field token ENDS the current group. Whitespace-only runs (the
            # "\n" / "\n\n" spacers) still separate slots -- that is exactly how Tableau lays the
            # block out -- but only a run with visible text is recorded as a caption.
            group = None
            caption = _strip_label_control(text)
            if caption:
                slots.append({"tokens": [], "colors": [], "bold": False,
                              "size": _font_size_points(run.get("fontsize")), "text": caption})
        if slots:
            return slots
    return None


# Tableau writes its mark-label LINE BREAK as the literal character ``\u00c6`` immediately followed
# by a newline (``<run fontalignment='0'>&#xC6;&#10;</run>``, confirmed in the raw workbook bytes).
# It is layout, not content, so it must not become a caption -- but ``\u00c6`` is also a real letter,
# so only the marker SEQUENCE is removed, never a bare character inside authored text.
_LABEL_BREAK_RE = re.compile("\u00c6(?=\r?\n)")


def _strip_label_control(text):
    """Visible caption text of a mark-label run (drops Tableau's layout-only runs).

    A run that is NOTHING but markers and whitespace is layout, so it yields no caption. A run that
    carries real text keeps it verbatim apart from the break sequence, so an authored caption that
    genuinely contains ``\u00c6`` is never mutilated.
    """
    t = text or ""
    if not t.replace("\u00c6", "").strip():
        return ""
    return _LABEL_BREAK_RE.sub("", t).strip()


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


def _parse_shelf_totals(rows_el, cols_el):
    """Whether Tableau shows a GRAND TOTAL on this view, from the shelf's own attributes.

    Tableau writes it on the shelf element and never uses the word "grand":
    ``<rows onTop='true' total='true'>``. ``total`` turns the grand total on; ``onTop`` puts the
    total row at the TOP instead of the bottom.

    This matters because **Power BI's table shows a Total row by DEFAULT**, so emitting nothing is
    not neutral -- it is a decision, and it is the wrong one for almost every view. Measured across
    the corpus of 34: **252 of 262 shelves declare no total at all**, while every one of the 42
    emitted grid visuals set no toggle and therefore inherited Power BI's default. That is an extra
    row of numbers on tables whose source never showed one -- a confidently-wrong addition rather
    than a missing feature, which is the harder kind to notice because it looks like data.

    Returns ``{"rows": bool, "rows_on_top": bool, "cols": bool, "cols_on_top": bool}``.
    """
    def _flag(el, attr):
        return bool(el is not None and (el.get(attr) or "").strip().lower() == "true")

    return {"rows": _flag(rows_el, "total"), "rows_on_top": _flag(rows_el, "onTop"),
            "cols": _flag(cols_el, "total"), "cols_on_top": _flag(cols_el, "onTop")}


def _pane_has_ring_encodings(pane):
    """Does this pane carry the encodings that DEFINE a pie/donut ring?

    A Tableau donut is several stacked Pie panes: one draws the ring, the others draw the number in
    the hole (and any spacers). Only the ring pane binds a ``color`` (the slice dimension) or a
    ``wedge-size`` / ``angle`` (the slice measure) -- the rest carry ``text`` alone. Those encodings,
    not pane order, are what identify the ring.

    Conservative: a pane with no ``<encodings>`` returns False and the caller falls back to the first
    Pie pane, which is precisely the previous behaviour, so a workbook without such a pane is
    unaffected.
    """
    enc = _first(pane, "encodings") if pane is not None else None
    if enc is None:
        return False
    for child in list(enc):
        tag = str(child.tag).rsplit("}", 1)[-1].lower()
        if tag in ("color", "wedge-size", "angle"):
            return True
    return False


def _parse_worksheet(ws, index, ds_caption, warnings, internal_fields=None, date_binding=None,
                     row_count_binding=None, measure_binding=None, column_binding=None,
                     measure_palette=None, ds_color_palettes=None, workbook_root=None,
                     shared_filters=None):
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
    #
    # PICK THE PANE THAT CARRIES THE RING, NOT MERELY THE FIRST PIE PANE. When every pane is a Pie
    # -- which is how Tableau writes a donut whose hole holds a total -- "first with a Pie mark" is
    # a coin toss, and it lost. Measured on the Salesforce NPSP "Engagements by Stage Total": three
    # Pie panes, whose <encodings> are text / (color=[Stage] + wedge-size) / text. The old selector
    # took pane 1, so the ring's colour dimension never reached ``encodings``; ``latent_color``
    # stayed False, the pie/donut card-collapse router could not fire, and the donut shipped as a
    # ``card`` reading "(Blank) 320". The defining encodings ARE the discriminator the comment above
    # already names, so select on them and fall back to the first Pie pane when none stands out.
    pie_panes = [p for p in all_panes
                 if _first(p, "mark") is not None
                 and (_first(p, "mark").get("class") or "").lower() == "pie"]
    pie_pane = next((p for p in pie_panes if _pane_has_ring_encodings(p)),
                    pie_panes[0] if pie_panes else None)
    donut_hack = pie_pane is not None and len(all_panes) > 1
    if pie_pane is not None:
        pane = pie_pane
    mark_el = _first(pane, "mark") if pane is not None else None
    mark = mark_el.get("class") if mark_el is not None else "Automatic"

    rows_el = _first(table, "rows")
    cols_el = _first(table, "cols")
    rows_text = (rows_el.text if rows_el is not None else "") or ""
    cols_text = (cols_el.text if cols_el is not None else "") or ""
    shelf_totals = _parse_shelf_totals(rows_el, cols_el)
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
    _tc_filters = []
    filters, swap_controls = _parse_filters(view, ds_default, base_cols, instances, index,
                                            ds_caption, name, warnings, warn_special=warn_special,
                                            internal_fields=internal_fields, date_binding=date_binding,
                                            table_calc_filters=_tc_filters,
                                            table_calc_peers=_worksheet_table_calc_count(ws),
                                            extra_filters=_worksheet_shared_filters(ws, shared_filters))
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
    label_slots = _parse_label_slots(all_panes)

    dims_rows = [f for f in rows if f["kind"] == "category"]
    dims_cols = [f for f in cols if f["kind"] == "category"]
    meas_rows = [f for f in rows if f["kind"] == "value"]
    meas_cols = [f for f in cols if f["kind"] == "value"]

    # Does the DIMENSION axis carry a continuous (green) Tableau pill? A continuous axis is a
    # number line, not a row of category slots, so the rebuilt visual must ask Power BI for a Scalar
    # category axis. Read from both shelves because the dimension axis is ``cols`` on a vertical
    # chart and ``rows`` on a horizontal bar; a chart normally carries its dimension on exactly one,
    # and a second DISCRETE pill (a trellis dimension) reads False and cannot flip the answer.
    continuous_axis = any(_is_continuous_pill(f) for f in (dims_rows + dims_cols))

    fidelity_note = None
    combo_split = None
    lipstick_overlap = False
    lipstick_series_colors = {}
    lipstick_series_transparency = []
    fold_groups = []
    pane_style_by_index = []
    lollipop = False
    lollipop_color = None
    mv_color_scales = []
    if uses_mv:
        # Measure Values/Names (M1.0): expand [Measure Values] to its ordered member measures in
        # the value well and route by mark + where the (implicit) Measure Names pill sits. The
        # member value fields join the IR shelves so the existing emitter binds them unchanged.
        locs = _mv_shelf_locations(rows_text, cols_text, pane)
        members, dummy_count, has_param_swap, mv_status = _resolve_measure_values(
            view, ds_default, base_cols, instances, index, ds_caption, name, warnings,
            internal_fields=internal_fields,
            # The Measure Values path used to resolve its members WITHOUT the model's binding
            # channels, so every member fell back to the standing caption resolution instead of the
            # authoritative model measure -- which is how two pills over one calc at different
            # derivations collapsed into a single identical reference (issue #103: Tableau's
            # "Avg. OTE" column vanished and the survivor reported the wrong grain).
            measure_binding=measure_binding, row_count_binding=row_count_binding,
            column_binding=column_binding, date_binding=date_binding,
            # Every pill this view spends on a NAMED shelf or encoding. Only consulted when the
            # Measure Names level is unfiltered and the member set has to be recovered from the
            # view's own declarations -- it is what keeps a measure parked on Tooltip/Colour from
            # being mistaken for a displayed Measure Values column.
            used_instances=_placed_instance_ids(rows_text, cols_text, all_panes))
        visual_type, inject_shelf, fidelity_note = _route_measure_values(
            mark, locs, members, dummy_count, has_param_swap, mv_status,
            dims_rows, dims_cols, name, warnings)
        if visual_type != VT_UNSUPPORTED:
            if inject_shelf == "rows":
                rows = rows + members
            else:
                cols = cols + members
            # HIGHLIGHT TABLE: Measure Values is on Colour as well as Text, so each member measure
            # carries its OWN palette (``separate-domains``). Pair each member with the colour
            # encoding built from the SAME pill -- matching on the instance token, which is what
            # both the shelf pill and the colour encoding name -- so the emitter can lay one
            # independent conditional-fill scale per column. Members with no colour encoding simply
            # get no entry, so a partially coloured table colours exactly the columns the author did.
            if "color" in (locs.get("values_roles") or ()):
                by_field = _parse_color_gradients_by_field(table)
                by_instance = {}
                for token, spec in by_field.items():
                    _, fid = _split_token_attr(token)
                    if fid:
                        by_instance[fid] = spec
                # Every pill on the Colour shelf is coloured, whether or not the author styled it.
                # One with a card but no explicit palette gets Tableau's automatic default ramp --
                # the same synthesis the single-scale path already performs -- so a highlight table
                # rebuilds with a fill on every column Tableau shades, not just the styled ones.
                carded = _color_card_instances(workbook_root, name)
                mv_color_scales = []
                for member in members:
                    inst = member.get("instance")
                    spec = by_instance.get(inst)
                    if spec is None and inst in carded and member.get("kind") == "value":
                        spec = _automatic_color_gradient(member)
                    if spec is not None:
                        mv_color_scales.append({"caption": member.get("caption"),
                                                "instance": inst,
                                                "gradient": spec})
    else:
        # marks-card encodings also carry fields: color/detail can be the disaggregating
        # dimension (scatter) and label/size can be the measure of a bare card / KPI tile.
        enc_dims = [f for f in (encodings["color"], encodings["detail"])
                    if f and f["kind"] == "category"]
        # A scatter's disaggregating dimension can be ANY Detail pill, not just the first one
        # enc["detail"] captured (Tableau serialises tooltip measures on Detail ahead of it). Fold
        # in every category Detail pill so has_dim reflects the real granularity dimension(s).
        enc_dims += [f for f in encodings.get("detail_dims", [])
                     if f and f["kind"] == "category" and f not in enc_dims]
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
            _measure_axis = _measure_shelf_axis(meas_rows, meas_cols)
            mark_by_instance, primary_mark, dual_axis = _pane_mark_map(table, _measure_axis)
            # RECTANGLES, not a count. `_pane_mark_map` reduces the fold structure to "is the whole
            # sheet ONE overlaid pane", which is binary and cannot express N overlaid panes side by
            # side -- a trellis whose every column is itself a dual axis. Captured here for both
            # readers: the single-rectangle case below, and the measure trellis, which otherwise
            # fans one chart per MEASURE where the source draws one per PAIR.
            _shelf_meas = list(meas_rows) + list(meas_cols)
            _folded = _folded_axis_instances(
                table, _measure_axis if _measure_axis in ("x", "y") else "y")
            fold_groups = _folded_measure_groups(_shelf_meas, _folded)
            if not any(len(g) > 1 for g in fold_groups):
                fold_groups = []
            _pane_cols_all = _pane_mark_colors(table, _measure_axis)
            _pane_transp_all = _pane_mark_transparency(table, _measure_axis)
            _pane_sizes_all = _pane_mark_sizes(table, _measure_axis)
            if fold_groups:
                # Per-shelf-position style, so a trellis band can colour its own overlaid pair
                # without re-deriving the pane reads. Aligned to the shelf, and the emitter
                # refuses to use it unless the lengths agree.
                pane_style_by_index = [
                    {"color": _pane_cols_all.get(f.get("instance")),
                     "transparency": _pane_transp_all.get(f.get("instance")),
                     "size": _pane_sizes_all.get(f.get("instance"))}
                    for f in _shelf_meas]
            # OVERLAY vs TWO SCALES. Tableau serialises the overlapping-bar ("lipstick") idiom and
            # a genuine two-scale dual axis identically, so this is the discriminator -- see
            # _is_scale_pair. Deliberately NOT ``synchronized='true'``, which holds on 33 of the
            # corpus's 37 folded axes and so separates nothing.
            _overlay = not _is_scale_pair(list(meas_rows) + list(meas_cols))
            column_meas, line_meas = _detect_combo(
                meas_rows, meas_cols, bool(dims_rows or dims_cols),
                mark_by_instance, primary_mark, dual_axis=dual_axis,
                overlay=_overlay)
            if column_meas and line_meas:
                visual_type = VT_COMBO
                combo_split = {"Y": column_meas, "Y2": line_meas}
                fidelity_note = (
                    "dual-axis combo: column measure(s) on the primary axis + line measure(s) "
                    "on the secondary axis -> lineClusteredColumnComboChart")
            elif dual_axis and visual_type == VT_LINE and "area" in _all_pane_marks(table):
                # A LINE axis overlaid with an AREA axis over the same measure is Tableau's
                # "line with a filled area" idiom -- the second pane exists only to draw the fill
                # under the first. Power BI's areaChart IS a line with the region below filled, so
                # the whole two-pane construct collapses to one areaChart. Reading only the primary
                # pane's mark left the fill off entirely: a bare line where the source is a filled
                # mountain.
                visual_type = VT_AREA
                fidelity_note = (
                    "dual-axis line + area over the same measure (Tableau's filled-line idiom) -> "
                    "areaChart, whose fill is the second pane")
            elif (_overlay and dual_axis and visual_type in (VT_COLUMN, VT_BAR)
                    and _lipstick_measures(meas_rows, meas_cols, mark_by_instance, primary_mark)):
                # Tableau's overlapping-bar / "lipstick" idiom: two same-family measures folded onto
                # one axis and drawn overlaid in a single plot rectangle. Kept as a clustered
                # bar/column with both measures on Y -- the combo route above would cost the second
                # measure's mark type and, on a Cols sheet, the orientation.
                lipstick_overlap = True
                # Per-series colours in SHELF ORDER (hex or None per measure). Tableau colours each
                # overlaid series on its own pane; rendered, collapsing them to one worksheet-wide
                # colour painted both bars the same orange and left transparency as the only
                # separator. A list rather than a dict because the emitted projections carry a
                # queryRef but not the measure instance, so the pairing is positional -- and the
                # emitter refuses to apply it unless the lengths agree.
                _pane_cols = _pane_mark_colors(table, _measure_axis)
                _pane_transp = _pane_mark_transparency(table, _measure_axis)
                _pane_sizes = _pane_mark_sizes(table, _measure_axis)
                _lip_meas = _lipstick_measures(
                    meas_rows, meas_cols, mark_by_instance, primary_mark) or []
                lipstick_series_colors = [_pane_cols.get(f.get("instance")) for f in _lip_meas]
                if not any(lipstick_series_colors):
                    lipstick_series_colors = []
                # The AUTHOR'S OWN per-series transparency, with a substitute where they separated
                # the series by mark WIDTH instead -- a property Power BI cannot draw at all.
                _authored_transp = [_pane_transp.get(f.get("instance")) for f in _lip_meas]
                lipstick_series_transparency = _lipstick_series_transparency(
                    _lip_meas, _pane_transp, _pane_sizes)
                # Whether any value in there is SUBSTITUTED rather than authored. Worth stating
                # because the two are indistinguishable in the emitted artifact -- the substitute is
                # deliberately drawn from the range the author used elsewhere, so on this very
                # workbook a derived 42 sits beside an authored 42 (alpha 147) on a sibling sheet.
                _lipstick_width_substituted = bool(
                    lipstick_series_transparency
                    and lipstick_series_transparency != [
                        t or None for t in _authored_transp])
                fidelity_note = (
                    "dual-axis overlapping bars (two different measures folded onto one axis) -> "
                    "clustered {0} with Overlap on and series spacing 100%; the first measure is "
                    "drawn behind the second, so where the front series is longer it covers the "
                    "back one -- as Tableau renders the same construct at equal mark widths, and "
                    "Power BI has no per-series bar width to narrow the front one with".format(
                        "bars" if visual_type == VT_BAR else "columns"))
                if _lipstick_width_substituted:
                    fidelity_note += (
                        ". The source separated these series by mark WIDTH, which Power BI cannot "
                        "draw, so the WIDER one is made {0}% transparent as the closest available "
                        "stand-in -- this is the engine's substitution, not a transparency the "
                        "author set".format(_LIPSTICK_WIDTH_SUBSTITUTE_TRANSPARENCY))

        # Dual-axis lollipop: a Bar (stick) pane + a Circle/Shape/Point (head) pane plotting the SAME
        # measure against a shared category. Power BI has no native lollipop, so re-route to a combo --
        # thin columns (the sticks) on Y + a marker-only hidden line (the dots) on Y2, BOTH wells bound
        # to the one shared measure. Checked AFTER _detect_combo (which needs DIFFERENT measures split
        # across mark families, so it never fires on a same-measure lollipop) and gated on the head +
        # Bar marks + a single measure identity, so ordinary charts and dual-scale combos never
        # misfire. The stick/dot colour is the worksheet's own constant mark colour (theme fallback).
        if visual_type in (VT_COLUMN, VT_BAR, VT_LINE, VT_AREA) and combo_split is None:
            lolli_meas = _detect_lollipop(
                table, meas_rows, meas_cols, bool(dims_rows or dims_cols))
            if lolli_meas and _measure_shelf_axis(meas_rows, meas_cols) == "y":
                visual_type = VT_COMBO
                combo_split = {"Y": lolli_meas, "Y2": list(lolli_meas)}
                lollipop = True
                lollipop_color = _constant_mark_color(table)
                fidelity_note = (
                    "dual-axis lollipop (Bar stick + Circle/Shape/Point head, same measure) -> "
                    "lineClusteredColumnComboChart (thin columns = sticks; marker-only line = heads)")
            elif lolli_meas:
                # A HORIZONTAL lollipop has no combo to go to. Power BI's only combo
                # (``lineClusteredColumnComboChart``) draws its columns VERTICALLY, so rerouting a
                # sheet whose measures sit on Cols would rotate the whole chart -- trading a missing
                # dot layer for a wrong orientation, which is the worse loss. Keep the horizontal
                # bars (the sticks, which ARE the source's own Bar pane), take the stick colour from
                # the drawing panes, and say plainly that the head layer has no horizontal home.
                lollipop_color = _constant_mark_color(table)
                fidelity_note = (
                    "dual-axis horizontal lollipop (Bar stick + point head over the same measure): "
                    "the sticks are rebuilt as a clusteredBarChart in the source's own mark colour. "
                    "Power BI has no horizontal combo chart, so the head layer is not drawn")
                warnings.append(_warn(
                    "worksheet", name,
                    "horizontal lollipop: sticks rebuilt faithfully (orientation, colour, labels); "
                    "the round heads have no native horizontal marker layer in Power BI and are "
                    "left off rather than rotating the chart to fit one"))

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

        # Treemap: a categorical TILING dimension on the Text (label) shelf -- the defining treemap
        # signal (the labelled tiles) -- sized by a measure on Size (or Colour when there is no Size),
        # with NO axis pills and an Automatic/Square mark, is Tableau's treemap. Power BI has a native
        # treemap -> Group = the Text dimension (plus any extra category Detail/Colour dims as
        # Details), Values = the size measure; a continuous colour measure shades the tiles via
        # _chart_continuous_fill. Requiring the category on TEXT is what keeps this OFF a
        # packed-bubble / heat layout (dimension on Detail + measure on Colour, which stays
        # unsupported) and OFF a bare KPI card (a measure on Label with no category) -- so it can
        # safely rescue BOTH a card- and an unsupported-classified worksheet (a Text dimension + a
        # Size measure with no axis is a card by the measure-no-axis-dim rule, since label dims are
        # not counted in enc_dims). Geographic dimensions defer to map routing (not geo_detail).
        if (visual_type in (VT_CARD, VT_UNSUPPORTED) and not (dims_rows or dims_cols)
                and not geo_detail
                and (mark or "").strip().lower() in ("automatic", "square", "")
                and encodings["label"] and encodings["label"]["kind"] == "category"):
            tm_group = [f for f in (encodings["label"], encodings["detail"], encodings["color"])
                        if f and f["kind"] == "category"]
            tm_value = next((f for f in (encodings["size"], encodings["color"])
                             if f and f["kind"] == "value"), None)
            if tm_group and tm_value is not None:
                visual_type = VT_TREEMAP
                fidelity_note = (
                    "categorical dimension on Text + a measure on Size/Colour (no axis pills) "
                    "-> native treemap (Group + Values; continuous colour shades the tiles)")

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

    # A shapeMap draws NOTHING until Desktop's preview feature is switched on. Measured: a US-state
    # choropleth emitted as a schema-valid ``shapeMap`` (correct topology, correct gradient, correct
    # bindings) rendered as an empty rectangle on a default Desktop install, while the same query on
    # a ``filledMap`` drew a real map -- "Shape map visual" lives under Options -> Preview features
    # and ships OFF. Nothing in the file is wrong and nothing the emitter can write turns it on, so
    # the honest thing is to say so rather than let a reader conclude the migration lost the map.
    if visual_type == VT_SHAPE_MAP:
        warnings.append(_warn(
            "worksheet", name,
            "rebuilt as a Power BI shapeMap (built-in topology choropleth). Power BI Desktop ships "
            "the shape map visual OFF: enable Options -> Preview features -> 'Shape map visual' and "
            "reopen, or the visual renders as an empty rectangle"))

    title_text, title_dynamic = _parse_worksheet_title(ws)
    # v2-3 (caption-only worksheet -> textbox): a thin status / refresh / filter-breadcrumb bar that
    # Tableau builds as a worksheet whose ONLY content is its (often dynamic) title -- no rows, no
    # cols, no plottable mark channel -- classifies as VT_UNSUPPORTED and would be DROPPED, leaving a
    # blank band on the dashboard (the user's "a thin view doesn't generate at all" defect). Capture
    # its RAW title HERE (before the nulling just below) so the page assembler can rebuild it as a
    # textbox at its authored zone, honouring the completeness invariant (never leave a labeled band
    # empty). A Detail-only encoding is the title tokens' data source, not a plottable mark, so it
    # still counts as caption-only; any rows/cols/colour/size/label/angle channel means a real -- if
    # unsupported -- visual that must NOT be flattened to its title. Dynamic tokens are resolved later
    # in ``parse_twb`` (once the parameters are known); this only records the raw text.
    caption_only_raw = None
    if visual_type == VT_UNSUPPORTED and title_text and title_text.strip():
        _plottable = bool(rows or cols or encodings.get("color") or encodings.get("size")
                          or encodings.get("label") or encodings.get("angle"))
        if not _plottable:
            caption_only_raw = title_text
    kpi_title_card = None
    kpi_title_cards = []
    dynamic_title_raw = None
    # A TITLE-ONLY KPI worksheet (no rows, no cols -- its entire content is the number(s) in its
    # title) classifies VT_UNSUPPORTED, and the branch below used to skip it outright: the caption
    # path then dropped it too, because the text still held a field ref. The zone came out EMPTY -- a
    # whole "Current: 745,568 / vs Last Year: 131,634" tile missing from the dashboard. A title whose
    # references resolve to real measures is exactly what the card path is for, whether or not the
    # sheet also draws a mark.
    #
    # The gate is EMPTY SHELVES, not ``caption_only_raw``: Tableau blanks such a sheet by dropping an
    # empty-string calc onto Text, and that Text encoding alone was enough to mark it "plottable" and
    # withhold the caption. An unsupported sheet WITH shelves is a genuinely deferred visual and must
    # not be quietly replaced by cards.
    if title_dynamic and (visual_type != VT_UNSUPPORTED or not (rows or cols)):
        specs = _detect_kpi_title_cards(ws)
        for spec in specs:
            fields = _resolve_shelf(
                spec["ref"], ds_default, base_cols, instances, index, ds_caption, name, warnings,
                warn_special=False, internal_fields=internal_fields, date_binding=date_binding,
                row_count_binding=row_count_binding, measure_binding=measure_binding,
                column_binding=column_binding)
            # A STRING measure is a conditional glyph, not a headline number -- Tableau's paired
            # "Arrow Up" / "Arrow down" calcs return an arrow character or "" so exactly one shows.
            # A card bound to one would render a lone arrow (or a blank) as if it were the metric.
            measures = [f for f in fields
                        if (f.get("binding") == "measure" or f.get("role") == "measure")
                        and (f.get("datatype") or "") != "string"]
            if not measures:
                continue
            # WHICH measure the card shows is the title's OWN reference -- that is literally what the
            # title says. The one exception is a VIEW-LEVEL table calc (a quick-table-calc pill, or a
            # formula built on ``TOTAL`` / ``RUNNING_*`` / ``WINDOW_*`` / ``LAST``): those have no
            # model measure to bind (they translate to deterministic ``= 0`` stubs that would render a
            # blank ``0``), and in a card -- which has no axis for a window to run along -- the base
            # metric's grand total equals the wrapper's final value, so the worksheet's own measure is
            # the faithful stand-in. That exception is why this branch existed.
            #
            # Preferring the worksheet's measure UNCONDITIONALLY was wrong the moment a title named a
            # DIFFERENT metric: "Days Left In Sales Year", whose number is
            # ``DATEDIFF('day', TODAY(), {MAX([Order Date])})`` = 145, rebuilt as the sheet's
            # SUM(Sales) = 2,326,534 -- a real number, in the right place, measuring the wrong thing.
            ws_measures = meas_rows + meas_cols
            if _is_view_level_calc(measures[0]):
                card_measures = (ws_measures or measures)[:1]
            else:
                card_measures = measures[:1]
            # The trailing GLYPH references on this line (Tableau's paired up/down arrow calcs) are
            # resolved to their own measures and carried alongside, to be drawn beside the number.
            glyph_specs = []
            for g in spec.get("glyphs") or []:
                gfields = _resolve_shelf(
                    g["ref"], ds_default, base_cols, instances, index, ds_caption, name, warnings,
                    warn_special=False, internal_fields=internal_fields, date_binding=date_binding,
                    row_count_binding=row_count_binding, measure_binding=measure_binding,
                    column_binding=column_binding)
                gmeas = [f for f in gfields
                         if f.get("binding") == "measure" or f.get("role") == "measure"]
                if gmeas:
                    glyph_specs.append({"measure_fields": gmeas[:1],
                                        "color": g.get("color"), "size": g.get("size")})
            kpi_title_cards.append({
                "caption": spec["caption"], "measure_fields": card_measures,
                "value_color": spec["value_color"], "value_size": spec["value_size"],
                "caption_style": spec.get("caption_style"),
                "glyphs": glyph_specs})
        if kpi_title_cards:
            # KPI title-card: keep the static caption as the title and rebuild the headline number
            # (the big dynamic measure run) as a companion card above the sparkline at emit time.
            title_text = kpi_title_cards[0]["caption"]
            kpi_title_card = kpi_title_cards[0]
            caption_only_raw = None
            warnings.append(_warn(
                "worksheet", name,
                "KPI title number(s) (dynamic measures embedded in the title) rebuilt as companion "
                "card(s) above the visual; the static captions are kept as their titles"))
    if visual_type == VT_UNSUPPORTED and not kpi_title_cards:
        title_text = None
    elif title_dynamic and not kpi_title_cards:
        # v2-4 (resolve dynamic captions on a SUPPORTED visual): a dynamic non-KPI title weaves a
        # live Tableau token -- a parameter ref (``<[Parameters].[id]>``) and/or a federated
        # field-ref / runtime special. It was historically dropped outright, so a parameter-driven
        # title like "Sales by <Show by Dimension>" lost its meaning (or, on a text zone, leaked the
        # raw token). Capture the RAW title and defer resolution to ``parse_twb`` (once parameters
        # are known): a title that becomes FULLY static after parameter substitution is kept; one
        # that still carries a field-ref / runtime special is dropped there (stripping it would leave
        # a dangling label). ``title_text`` stays ``None`` for now so nothing pre-resolution emits.
        dynamic_title_raw = title_text
        title_text = None

    title_style = (_parse_title_style(ws, static_only=bool(kpi_title_card))
                   if (title_text or dynamic_title_raw) else None)

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
    # The worksheet's OWN canvas colour (``style-rule[@element='table']``) -- the surface behind the
    # whole chart, not one of its parts. Same Tableau spelling as the dashboard canvas; the container
    # it hangs from is what differs (see _container_background_fill). Measured across the corpus: a
    # dark workbook sets this on every worksheet, and without it each chart rebuilds as a white tile
    # sitting on the (now correct) dark page -- a worse-looking result than getting neither right.
    canvas_fill = _container_background_fill(table)

    axis_titles = {}
    axis_hidden = set()
    # Computed UNCONDITIONALLY, unlike the axis parsing below. That block is gated on
    # ``_AXIS_TITLE_TYPES``, which a text table is not a member of -- a crosstab has no axes -- so
    # the author's row-label hide was parsed for charts and silently dropped for exactly the visual
    # type the container-stitch idiom uses.
    row_labels_hidden = _parse_row_labels_hidden(table, dims_rows)
    if visual_type in _AXIS_TITLE_TYPES:
        axis_titles = _parse_axis_titles(table, dims_rows, dims_cols, meas_rows, meas_cols)
        axis_hidden = _parse_hidden_axes(table, dims_rows, dims_cols, meas_rows, meas_cols)
        # A category axis RESCUED from a hide (because the member names existed only as mark
        # labels) is shown for its members, not for a caption -- the author displayed no header at
        # all, so Power BI's auto field-name title ("Regional_Man...") is furniture the source never
        # had, and it steals a slice of the plot on a small tile.
        if _members_drawn_as_labels(table, dims_rows, dims_cols):
            axis_titles = dict(axis_titles or {})
            axis_titles.setdefault("categoryAxis", {"hide": True})

    # Continuous colour scale on a worksheet's mark colour encoding. On a table / matrix it becomes a
    # cell heat scale (a PBIR ``backColor`` FillRule via ``_conditional_format``); on a cartesian
    # chart the SAME continuous encoding colours the marks (a ``dataPoint.fill`` FillRule via
    # ``_chart_continuous_fill``). Parsed here for both families (additive IR key); each emit path is
    # gated by visual type and defers faithfully when the colour driver cannot bind to a model
    # measure. Other visual types (cards / pies / maps) carry their colour elsewhere -- not parsed here.
    color_gradient = None
    if visual_type in (VT_MATRIX, VT_TABLE, VT_COLUMN, VT_BAR, VT_LINE,
                       VT_AREA, VT_SCATTER, VT_COMBO, VT_TREEMAP):
        color_gradient = _parse_color_gradient(table)
        if color_gradient is None:
            # No explicit ``<color-palette>`` in the worksheet FORMAT style, but a continuous MEASURE
            # sits on the Color shelf (an "Automatic" palette). Synthesise Tableau's automatic default
            # continuous ramp so the colour channel is reconstructed and DISCLOSED rather than silently
            # dropped -- feeding the identical emit paths (chart ``dataPoint.fill`` / matrix
            # ``backColor``, incl. the Visual-Calculation-driven heat scale) as an explicit palette.
            _color_enc_g = encodings.get("color")
            if (_color_enc_g and _color_enc_g.get("kind") == "value"
                    and _color_enc_g.get("binding") in ("aggregation", "measure")
                    and not _color_enc_g.get("discrete_measure")):
                color_gradient = _automatic_color_gradient(_color_enc_g)

    # Per-measure colour scales. A highlight table puts Measure Values on Colour with
    # ``separate-domains``, so each member measure owns its own palette and domain; the single
    # ``color_gradient`` above cannot represent that. Parsed for every table/matrix (additive IR key)
    # and consumed only when the worksheet actually colours by Measure Values, so a chart or a
    # single-scale table is unaffected.
    color_gradients = {}
    if visual_type in (VT_MATRIX, VT_TABLE):
        color_gradients = _parse_color_gradients_by_field(table)

    # Explicit categorical mark-colour palette (author member -> hex). Parsed here (additive IR key)
    # and turned into PBIR dataPoint per-member fills at emit time -- faithful-or-warn, so a palette
    # on a visual type that cannot carry a per-member fill, or whose coloured dimension is not bound,
    # defers rather than colouring the wrong mark.
    mark_colors = _parse_mark_colors(
        table, ds_color_palettes, _pane_color_columns(all_panes))

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
    # Tableau can place the grand total at the TOP of the view (``<rows onTop='true' total='true'>``).
    # Power BI cannot: the schema for a flat table (``tableEx``) exposes exactly one total control,
    # ``total.totals`` (bool), with NO position property, and a matrix's ``rowSubtotalsPosition``
    # governs per-group SUBTOTALS rather than the grand total. So the row is rebuilt in the only place
    # Power BI will put it and the move is DISCLOSED -- the alternative is a silent relocation of the
    # one row a reader is most likely to look at first.
    if visual_type in (VT_TABLE, VT_MATRIX) and shelf_totals.get("rows") and \
            shelf_totals.get("rows_on_top"):
        warnings.append(_warn(
            "worksheet", name,
            "grand total is shown at the TOP in Tableau; Power BI's table/matrix has no total-position "
            "control, so the total row is rebuilt at the BOTTOM (the values are unchanged)"))
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
        # Every mark class present across the worksheet's panes. A Tableau DUAL-AXIS map stacks
        # several mark layers in one worksheet (a Multipolygon choropleth plus Pie marks at a finer
        # LOD), and one Power BI map has one Location well and one Legend well, so the extra layers
        # are dropped -- which has to be SAID (issue #111). Recorded here so the emitter can name the
        # real loss instead of only the palette detail that follows from it.
        "pane_marks": _all_pane_marks(table),
        "visual_type": visual_type,
        "title": title_text,
        "caption_only_raw": caption_only_raw,
        "dynamic_title_raw": dynamic_title_raw,
        "title_style": title_style,
        "filter_hdr_style": filter_hdr_style,
        "filter_itm_style": filter_itm_style,
        "filter_plate_fill": filter_plate_fill,
        "grid_styles": grid_styles,
        "canvas_fill": canvas_fill,
        # The worksheet's own basemap token (#128). Captured HERE, at parse time, because the
        # emitter only ever sees this dict -- and it is per-worksheet on purpose: one workbook can
        # draw satellite on one sheet and a dark basemap on the next, so no module-level constant
        # can serve both. Raw token; `_tableau_map_style` maps it to a Power BI style.
        "map_style_raw": _worksheet_map_style_raw(ws),
        "axis_titles": axis_titles,
        "continuous_axis": continuous_axis,
        # Read on the shelf the measures ACTUALLY sit on. ``_pane_mark_map`` defaults to the y axis,
        # which is right for a vertical chart and blind on a HORIZONTAL one -- a measures-on-Cols
        # dual axis writes ``x-axis-name`` panes, so the y-only read finds no axis panes and reports
        # False. Measured on the 16-sheet dual-axis corpus: all 8 horizontal sheets reported False
        # where the axis-aware read (the one ``_detect_combo`` already uses at the visual-type
        # decision) says True. The disagreement was latent only because those sheets were promoted
        # to a combo before the single consumer of this field could act on it; the moment a
        # same-family horizontal dual axis stays a bar chart, the sole consumer
        # (``_measure_trellis_measures``, whose guard means "a dual axis is ONE overlaid pane, not a
        # trellis") reads the wrong answer and fans it into N side-by-side charts.
        "dual_axis": _pane_mark_map(table, _measure_shelf_axis(meas_rows, meas_cols))[2],
        "axis_hidden": sorted(axis_hidden),
        "row_labels_hidden": row_labels_hidden,
        # Grand-total visibility, from the shelf's own ``total``/``onTop`` attributes. Power BI's
        # table shows a Total row by DEFAULT, so this must be emitted explicitly in BOTH directions
        # -- see _parse_shelf_totals.
        "shelf_totals": shelf_totals,
        "color_gradient": color_gradient,
        "color_gradients": color_gradients,
        "mv_color_scales": mv_color_scales,
        "mark_colors": mark_colors,
        "measure_colors": measure_colors,
        "card_label_colors": card_label_colors,
        "label_slots": label_slots,
        "data_labels": data_labels,
        "reference_lines": reference_lines,
        "reference_line_constants": reference_line_constants,
        "rows": rows,
        "cols": cols,
        "uses_measure_values": uses_mv,
        "encodings": encodings,
        "filters": filters,
        "table_calc_filters": _tc_filters,
        "swap_controls": swap_controls,
        "fidelity_note": fidelity_note,
        "combo_split": combo_split,
        # Tableau's overlapping-bar ("lipstick") idiom: two same-family measures on ONE synchronized
        # axis. Recorded here because the emitter only ever sees this dict.
        "lipstick_overlap": lipstick_overlap,
        "lollipop": lollipop,
        "lollipop_color": lollipop_color,
        # The worksheet's single constant mark colour, for EVERY visual type -- the flat
        # ``<format attr='mark-color'>`` a Tableau author sets when they colour the marks without
        # binding a field to Colour. It lives on a PANE-level ``style-rule[element='mark']``, not on
        # the worksheet's own ``table/style``, which is why the table-style readers never saw it.
        #
        # Without this a workbook whose author picked a deliberate palette rebuilds in Power BI's
        # default blue for every chart -- measured on the corpus's time-series workbook, where three
        # orange (#f28e2b), three green (#59a14f) and three cyan (#00bceb) charts ALL came out blue.
        # It is the single most visible whole-dashboard defect after the page background.
        # A lipstick's series are coloured PER PANE, so the worksheet-wide flat colour is suppressed
        # for it -- see _pane_mark_colors. Left in place for every other shape, which is where the
        # flat read is correct.
        "mark_color": (None if (lipstick_overlap and lipstick_series_colors)
                       else _constant_mark_color(table)),
        "lipstick_series_colors": lipstick_series_colors,
        "lipstick_series_transparency": lipstick_series_transparency,
        # RECTANGLE grouping of the measure shelf (index lists) plus the per-position pane
        # style. Empty unless some rectangle holds more than one measure, so a sheet with no
        # folded axis carries nothing new.
        "fold_groups": fold_groups,
        "pane_style_by_index": pane_style_by_index,        "mark_transparency": _mark_transparency_pct(table),
        "sort": sort,
        "kpi_title_card": kpi_title_card,
        "kpi_title_cards": kpi_title_cards,
    }


# -- dashboard parsing ---------------------------------------------------------
def _zone_num(zone, attr):
    try:
        return float(zone.get(attr))
    except (TypeError, ValueError):
        return None


def _zone_shows_title(zone):
    """Does this dashboard zone display its worksheet's title? ``True`` unless explicitly disabled.

    Tableau's dashboard zones show the sheet title by default and serialise ``show-title='false'``
    ONLY when the author unticks "Show Title", so the attribute is a disable flag rather than a
    state field -- absent means shown. Any other/garbled value is read as shown, matching Tableau's
    own default rather than guessing.

    This is load-bearing for fidelity because Power BI's default is NOT "no title": a visual that
    emits no title object gets an auto-generated field-name caption ("Sum of Quantity by Name"),
    which is neither the author's title nor the blank they asked for. Both branches therefore have
    to be stated explicitly downstream -- see ``_visual_json``'s ``show_title``.
    """
    v = zone.get("show-title")
    if v is None:
        return True
    return str(v).strip().lower() not in ("false", "0", "no")


def _title_display(ws, show_title=True):
    """A worksheet visual's container title as ``(text, show)``.

    Tableau's *implicit* title is the WORKSHEET NAME; ``<layout-options><title>`` only overrides it.
    ``_worksheet_title`` therefore yields ``None`` for the very common "author kept the default"
    case, and forwarding that as "no title" is wrong in both directions:

    * shown zones lose the author's caption and get Power BI's auto-generated field-name one
      instead (e.g. ``pmdm__UnitOfMeasurement__c``), which the reader has no reason to distrust;
    * hidden zones get that same invented caption rather than the blank that was asked for.

    So resolve the default here and let the caller pass BOTH halves to ``_visual_json``.
    """
    if not show_title:
        return None, False
    return (ws.get("title") or ws.get("name") or None), True


def _container_background_fill(container):
    """The background a Tableau ``<style>`` block paints on its OWN container -> ``#rrggbb`` or None.

    Tableau spells "the background of this whole thing" as a ``style-rule`` whose ``element`` is
    ``table``, and the container it hangs from decides what "this whole thing" means:

    * under ``<dashboard>``          -> the dashboard canvas (the page background)
    * under ``<worksheet><table>``   -> that one chart's own canvas

    So ONE reader serves both, which is why this takes the container rather than a dashboard. Measured
    across the 29-workbook corpus: a dark dashboard declares ``#1b1b1b`` at dashboard scope and
    ``#333333`` on each of its nine worksheets, and neither was read -- the rebuild came out white on
    both layers, which is the single most visible whole-page fidelity defect available.

    Distinct from :func:`_zone_background_fill`, which reads a ``<zone-style>`` on ONE tile. A zone
    fill paints a tile; this paints the surface behind every tile.

    Only a well-formed opaque ``#rrggbb`` is returned. Alpha is deliberately NOT blended here: a
    partial-alpha canvas has no faithful single-hex form, and inventing one would silently change
    every colour composited over it.
    """
    if container is None:
        return None
    for style in _children_local(container, "style"):
        for rule in _children_local(style, "style-rule"):
            if (rule.get("element") or "").lower() != "table":
                continue
            for fmt in _children_local(rule, "format"):
                if fmt.get("attr") != "background-color":
                    continue
                val = (fmt.get("value") or "").strip()
                if _HEX6_RE.match(val):
                    return val.lower()
                # 8-digit #rrggbbaa: honour it only when fully opaque, and treat a fully
                # transparent canvas as "no authored background" rather than as black.
                if _HEX8_RE.match(val) and val[7:9].lower() == "ff":
                    return val[:7].lower()
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
    separately (see ``_zone_run_color``). Returns ``""`` when the zone carries no formatted text.

    Tableau encodes a HARD line break inside a text box as the sentinel ``\\u00c6`` (Æ) immediately
    followed by a real newline in its own ``<run>`` (e.g. a two-line column header "New Inbound" /
    "Referrals"). We drop the Æ sentinel and keep the newline so the break survives without leaking a
    literal "Æ" onto the page."""
    ft = _first(zone, "formatted-text")
    if ft is None:
        return ""
    joined = "".join((r.text or "") for r in _findall_local(ft, "run"))
    return joined.replace("\u00c6\n", "\n").replace("\u00c6", "").strip()


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


def _parse_dashboard(db, worksheet_names, warnings, layout=LAYOUT_DEFAULT):
    name = db.get("name")
    size_el = _first(db, "size")
    size = {"w": None, "h": None, "min_w": None, "min_h": None, "sizing_mode": None}
    if size_el is not None:
        size["sizing_mode"] = size_el.get("sizing-mode")
        try:
            size["w"] = float(size_el.get("maxwidth")) if size_el.get("maxwidth") else None
            size["h"] = float(size_el.get("maxheight")) if size_el.get("maxheight") else None
        except ValueError:
            pass
        # An "automatic" dashboard (sizing-mode='automatic', or any <size> that declares only
        # minwidth/minheight with no fixed max) has no fixed pixel canvas -- Tableau grows it to the
        # window but the author DESIGNED against the minimum, so minwidth/minheight is the only signal
        # of the intended aspect ratio. Capture it so emit can adopt that aspect instead of squashing
        # every automatic dashboard into the flat 1000x800 fallback (which de-normalizes the square
        # 100000x100000 zone frame to the WRONG shape, vertically stretching a landscape layout).
        try:
            size["min_w"] = float(size_el.get("minwidth")) if size_el.get("minwidth") else None
            size["min_h"] = float(size_el.get("minheight")) if size_el.get("minheight") else None
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
    # Tableau paints dashboard zones in DOCUMENT ORDER: a zone written earlier is drawn first and so
    # sits BENEATH everything written after it. That single fact separates the two opposite roles a
    # dashboard image plays -- a full-canvas background PLATE (written before the worksheets, so the
    # sheets draw on top of it) versus a decorative OVERLAY such as a corner logo or a toggled help
    # panel (written after them, so it covers them). Both are the same ``bitmap`` zone type and are
    # otherwise indistinguishable, so the walk records each image's paint ordinal here and the
    # emitter compares it against the first worksheet's. See ``_image_z``.
    zone_ord = 0
    first_ws_ord = None
    ext_w = ext_h = 0.0
    # AUTHOR-HIDDEN ZONES. ``hidden-by-user='true'`` is Tableau's record that the author collapsed a
    # dashboard object behind a show/hide toggle -- Tableau renders NOTHING for it on open. Emitting
    # it anyway does not merely add furniture, it OCCLUDES: a toggled help panel is written after the
    # worksheets (see ``_image_z``) and therefore covers the entire page, and a hidden per-airline
    # background plate stacks on top of the one that should show. Hidden content is therefore skipped.
    #
    # ``filter`` zones are deliberately EXEMPT and keep their long-standing behaviour (surfaced at
    # their authored position, flagged ``hidden``): a collapsed filter BAND is a control the reader
    # can still use and it does not paint over anything, and Power BI has no Tier-1 collapse
    # equivalent. The distinction is occluding CONTENT (skip) versus a usable CONTROL (keep).
    #
    # Hiding is inherited: ``_findall_local`` is a flat walk, so a hidden layout container's children
    # are visited independently and must be pruned with it -- otherwise a container toggled off as a
    # unit leaks its contents onto the canvas.
    hidden_zones = set()
    hidden_skipped = []
    for _hz in _findall_local(db, "zone"):
        if (_hz.get("hidden-by-user") or "").strip().lower() == "true":
            for _desc in _hz.iter("zone"):
                hidden_zones.add(id(_desc))
    # Every captured item below additively records ``zone_id`` -- the source zone's ``id`` attribute,
    # the same key ``zone_tree`` stores on each node and ``layout_solve`` keys its solved rects by.
    # It is the IDENTITY SEAM the layout-solver emit path needs: without it a captured dict is just a
    # bag of coordinates and cannot be matched back to its node in the layout tree (matching by rect
    # is ambiguous -- a single-child ``layout-flow`` wrapper shares its child's rect exactly, which
    # occurs on 12 of the 13 corpus dashboards). Captured at walk time, so it is exact. Verified
    # across the corpus: ``id`` is present on every zone (454/454) and unique within a dashboard.
    # Nothing reads it yet; it is recorded here so the solver wiring is a lookup, not a re-parse.
    for zone in _findall_local(db, "zone"):
        if zone in device_zones:
            continue
        zone_ord += 1
        x, y = _zone_num(zone, "x"), _zone_num(zone, "y")
        w, h = _zone_num(zone, "w"), _zone_num(zone, "h")
        if None not in (x, y, w, h) and w > 0 and h > 0:
            # canvas extent spans every zone (incl. layout containers), in Tableau's
            # internal coordinate units -- the correct frame for scaling, NOT <size>
            # (which is pixels and a different unit system).
            ext_w = max(ext_w, x + w)
            ext_h = max(ext_h, y + h)
        ztype = zone.get("type-v2") or zone.get("type")
        # Skip author-hidden CONTENT (see ``hidden_zones`` above). Filter cards AND parameter
        # controls are exempt and fall through to their own branches, which record ``hidden`` for
        # diagnostics and still surface the control. Counted so the fidelity report states what was
        # withheld rather than silently dropping it.
        #
        # ``paramctrl`` belongs in this exemption for exactly the reason ``filter`` does, and leaving
        # it out silently deleted the single most important control on a dashboard. Measured on an
        # ATTI/ATTR technician-hierarchy workbook whose ``Date Selection`` parameter (Monthly /
        # Weekly / Daily) is what drives the matrix column grain: the zone carries
        # ``hidden-by-user='true'`` because it lives in a collapsible band, so it was skipped as
        # occluding content 74 lines before reaching the ``paramctrl`` branch below -- the branch
        # whose own comment promises it is "never silently dropped". The reader lost the control and
        # the matrix fell back to raw daily dates instead of the authored monthly buckets.
        #
        # A parameter control cannot occlude: like a filter card it is a small, usable CONTROL, which
        # is the distinction this skip is drawing (occluding CONTENT -> skip, usable CONTROL -> keep).
        if ztype not in ("filter", "paramctrl") and id(zone) in hidden_zones:
            hidden_skipped.append({
                "zone_id": zone.get("id"),
                "type": ztype or "worksheet",
                "ref": zone.get("name") or zone.get("param") or "",
            })
            continue
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
                    "zone_id": zone.get("id"),
                    "pad": _parse_zone_padding(_first(zone, "zone-style")),
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
                    "zone_id": zone.get("id"),
                    "pad": _parse_zone_padding(_first(zone, "zone-style")),
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
                        "zone_id": zone.get("id"),
                        # The worksheet this card BELONGS to. Tableau's ``quick-filter-title`` /
                        # ``quick-filter`` style rules live on a worksheet, and a dashboard filter
                        # card takes its face from its OWNING sheet -- not from whichever sheet
                        # happens to filter on the same field first. Without this the slicer style
                        # was resolved by field token alone and landed on an arbitrary sheet.
                        "owner": zone.get("name"),
                        "pad": _parse_zone_padding(_first(zone, "zone-style")),
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
                param_controls.append({"param_id": pid, "x": x, "y": y, "w": w, "h": h,
                                       "zone_id": zone.get("id"),
                                       "pad": _parse_zone_padding(_first(zone, "zone-style")),
                                       "mode": zone.get("mode")})
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
                                "zone_id": zone.get("id"),
                                "pad": _parse_zone_padding(_first(zone, "zone-style")),
                                "kind": "image" if ztype == "bitmap" else "button",
                                "image": ref, "x": x, "y": y, "w": w, "h": h,
                                "url": zone.get("url"),
                                "paint_ord": zone_ord,
                            })
                    continue
        zname = zone.get("name")
        if not zname or zname not in worksheet_names:
            continue
        # A colour-legend decoration zone (``type='color'``) names the worksheet whose colour Series
        # it legends; capture its geometry so the report can faithfully reproduce legend show/position
        # (a present zone = the legend is shown at that side; an absent one = the author hid it).
        if ztype == "color" and None not in (x, y, w, h) and w > 0 and h > 0:
            legend_zones.append({"worksheet": zname, "zone_id": zone.get("id"),
                                 "x": x, "y": y, "w": w, "h": h})
            continue
        # worksheet zones carry no decoration type (legends/filters/titles do)
        if ztype:
            continue
        if None in (x, y, w, h) or w <= 0 or h <= 0:
            continue
        if first_ws_ord is None:
            first_ws_ord = zone_ord
        zones.append({"worksheet": zname, "zone_id": zone.get("id"),
                      # Tableau writes ``show-title`` ONLY when the author turns the title OFF, so an
                      # absent attribute means shown. Captured per ZONE, not per worksheet: the same
                      # sheet can be titled on one dashboard and untitled on another.
                      "show_title": _zone_shows_title(zone),
                      "x": x, "y": y, "w": w, "h": h})

    title_banner = _select_title_banner(banner_candidates, ext_w, ext_h)
    if title_banner:
        # The header band is emitted from ``title_banner`` (its own crimson-fill textbox); drop the
        # matching zone from the general text-object list so it is never drawn a second time.
        text_objects = [t for t in text_objects
                        if not (t["text"] == title_banner["text"]
                                and t["x"] == title_banner["x"]
                                and t["y"] == title_banner["y"])]
    if hidden_skipped:
        warnings.append(_warn(
            "dashboard", name,
            "%d author-hidden zone(s) not rebuilt (Tableau 'hidden-by-user' show/hide toggle -- "
            "Tableau renders nothing for them on open): %s" % (
                len(hidden_skipped),
                ", ".join("%s[%s]" % (z["type"], z["ref"] or z["zone_id"])
                          for z in hidden_skipped[:8]))))
    return {"name": name, "size": size,
            # The dashboard's own canvas colour (``style-rule[@element='table']``), or None when the
            # author left it default. Read here because this is the only place the source
            # <dashboard> element is in scope.
            "canvas_fill": _container_background_fill(db),
            "extent": {"w": ext_w or None, "h": ext_h or None}, "zones": zones,
            "param_controls": param_controls, "legend_zones": legend_zones,
            "filter_field_tokens": sorted(filter_field_tokens),
            "filter_zones": filter_zones,
            "text_objects": text_objects,
            "image_zones": image_zones,
            # Paint ordinal of the first worksheet zone, or None when the dashboard has no worksheet
            # at all. Every image whose ``paint_ord`` is below this was written BENEATH the sheets.
            "first_ws_ord": first_ws_ord,
            "title_banner": title_banner,
            # Author-hidden zones withheld from the rebuild, for the fidelity report.
            "hidden_zones_skipped": hidden_skipped,
            # The solved layout for this dashboard, or None under the legacy engine / on any solve
            # failure. Built HERE because this is the only place the source <dashboard> element is in
            # scope; the emit path consumes it as a lookup rather than re-parsing the XML.
            "layout_plan": _build_layout_plan(db, size, layout, device_zones)}


def _build_layout_plan(db, size, layout, device_zones):
    """Solve this dashboard's zone tree into a plan, or ``None`` (legacy engine / fail-closed).

    Solved against ``_dash_page_dims(size)`` -- the exact page the emit path derives -- because a plan
    solved against any other page produces rects that are out of bounds on the page actually emitted.
    ``device_zones`` is the caller's already-computed phone/tablet exclusion set, reused rather than
    recomputed so the tree sees exactly the zones the emit walk did.
    """
    if layout != "solver" or _layout_plan is None:
        return None
    try:
        page_w, page_h = _dash_page_dims(size)
        return _layout_plan.build_plan(db, device_zones=device_zones,
                                       page_w=page_w, page_h=page_h)
    except Exception:  # pragma: no cover - defensive; the solver is never allowed to break emit
        return None


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
                # Carry the zone's IDENTITY, not just its geometry. ``_scale_zone`` looks a solved
                # rect up by ``zone_id``, so dropping the id here made every parameter-control
                # slicer INVISIBLE to the layout solver: it alone kept the naive scale-and-clamp
                # position while its neighbours were re-solved onto a grown page, which is exactly
                # how a slicer ends up sitting on top of the table beside it. The id is already
                # captured above; it just never reached the emitter.
                # (``pad`` is deliberately NOT carried here: padding is applied on both engines, so
                # adding it would change legacy output too. Tracked separately.)
                "position": {"x": pc.get("x"), "y": pc.get("y"),
                             "w": pc.get("w"), "h": pc.get("h"),
                             "zone_id": pc.get("zone_id")},
                "mode": pc.get("mode"),
                # Tableau's CURRENT parameter value travels with the control so the emitter can
                # open the rebuilt slicer on it. Captured here (where ``_parse_parameters`` output
                # is in scope) rather than re-parsed downstream.
                "param_meta": {
                    "current_value": meta.get("current_value"),
                    "current_display": meta.get("current_display"),
                    "members": list(meta.get("members") or []),
                },
            }
            bound = slicers.get(_norm_param_key(pid))
            if bound:
                rec["resolved"] = {
                    "table": bound["table"], "column": bound["column"],
                    "single_select": bool(bound.get("single_select", True)),
                    "caption": bound.get("caption") or caption,
                }
                if isinstance(bound.get("select"), dict):
                    rec["resolved"]["select"] = dict(bound["select"])
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
            # CASCADE GUARD. A keep-flag whose source calc is a TABLE CALC (e.g. `IF LAST()<=15`)
            # is a Tableau *table-calc filter*: it runs after aggregation and HIDES marks, leaving
            # every other table calc in the view untouched. The visual-level filter we build here
            # genuinely REMOVES rows, so on a sheet that also carries table calcs it silently
            # re-scopes them -- a running sum restarts, a percent-of-total's denominator shrinks.
            # That output renders cleanly and keeps the right ROW COUNT; only the values are wrong,
            # which is why a row-count check can never catch it.
            #
            # So the filter is applied only on the branch where it is SAFE -- no other table calc in
            # the view, where hide and exclude are indistinguishable. Where they differ, we decline
            # and say why: an honest gap beats a confidently wrong number.
            _tcf = next((t for t in (ws_by_name[ws_name].get("table_calc_filters") or ())
                         if t.get("caption") == token), None)
            if _tcf and _tcf.get("peers"):
                warnings.append(_warn(
                    "worksheet", ws_name,
                    f"table-calc filter '{token}' ({', '.join(_tcf.get('idioms') or ())}) left "
                    f"UNAPPLIED on this worksheet: it hides marks after aggregation, but a "
                    f"visual-level filter removes rows, which would silently re-scope the "
                    f"{_tcf['peers']} table calc(s) in this view (their values change, the row "
                    f"count does not)"))
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
                        and (f"aggregate/measure filter on '{token}'" in reason
                             or f"table-calc filter on '{token}'" in reason)):
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
            # The parameter's CURRENT value is the column's own ``value`` attribute; its display is
            # the column ``alias`` (Tableau writes the resolved alias inline) or the matching member
            # alias, else the raw literal. Tableau renders a ``<[Parameters].[<id>]>`` token woven
            # into a caption/text zone AS this current display value (e.g. "Program Name"), so we
            # capture it here to resolve those tokens at emit instead of leaking raw markup.
            cur_val = _strip_member_literal(col.get("value")) if col.get("value") is not None else None
            cur_display = col.get("alias")
            if not cur_display and cur_val is not None:
                cur_display = next((m.get("alias") for m in members
                                    if m["value"] == cur_val and m.get("alias")), None)
            # No alias -> honour the author's NUMBER FORMAT before falling back to the raw literal.
            # A range parameter has no members to alias, so its format code is the only statement of
            # how the value is meant to READ: `$500K` is the authored display of 500000, and leaking
            # the raw integer into a title changes what the number appears to say.
            if not cur_display and cur_val is not None and col.get("default-format"):
                cur_display = _format_number_literal(cur_val, col.get("default-format"))
            if not cur_display and cur_val is not None:
                cur_display = cur_val
            params[pid] = {
                "caption": col.get("caption") or pid,
                "datatype": (col.get("datatype") or "").lower(),
                "members": members,
                "current_value": cur_val,
                "current_display": cur_display,
                "default_format": col.get("default-format"),
            }
    return params


_PARAM_TOKEN_RE = re.compile(r"<\[Parameters\]\.\[(?P<pid>[^\]]+)\]>")
_FIELD_TOKEN_RE = re.compile(r"<\[[^<>]+\]\.\[[^<>]+\]>")

# A quoted literal inside a Tableau/Excel number-format code, and the numeric placeholder run.
_FMT_QUOTED_RE = re.compile(r'"([^"]*)"')
_FMT_PATTERN_RE = re.compile(r"[#0][#0,.]*")


def _format_number_literal(value, code):
    """Render ``value`` through a Tableau ``default-format`` code, or ``None`` if it cannot.

    Tableau persists an author's parameter/field number format as an Excel-style code
    (``c"$"#,##0,K;("$"#,##0,K)``). A MEASURE can carry that straight through as a Power BI
    ``formatString`` (see ``tableau_default_format_to_pbi``), but a title or text box is STATIC
    text in Power BI -- there is no format to apply at render time, so the literal has to be
    computed here or the reader sees the raw number. Measured: an authored ``$500K`` rebuilt as
    ``500000``, which is not just uglier, it is a different claim about the value's scale.

    Implements the subset that actually appears in the wild, and declines anything else rather than
    approximate it: section split on ``;`` (positive/negative), quoted literals, thousands grouping,
    a fixed number of decimals, TRAILING commas as 1000x scaling (``#,##0,K`` -> 500000 renders
    ``500K``), and ``%`` as a 100x scale. Returns ``None`` when the code has no numeric placeholder
    or the value is not numeric, so the caller keeps the raw value instead of inventing a format.
    """
    if value is None or code is None:
        return None
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    body = tableau_default_format_to_pbi(str(code))
    if not body:
        return None
    # Negative values use the second section when the author supplied one.
    sections = body.split(";")
    section = sections[0]
    if len(sections) > 1 and num < 0:
        section = sections[1]
        num = abs(num)
    # Tableau also writes a PRECISION code rather than a placeholder pattern: ``p1%`` is "percent to
    # one decimal". Only the percent form is unambiguous -- a bare ``n2`` does not say whether the
    # author wanted thousands grouping, so it declines to the raw value rather than guess.
    pct_precision = re.match(r"^(\d+)%$", section.strip())
    if pct_precision:
        return ("{:.%df}%%" % int(pct_precision.group(1))).format(num * 100.0)
    literals = []

    def _stash(mo):
        literals.append(mo.group(1))
        return "\x00"           # digit-free sentinel: a digit here would be read as the pattern

    section = _FMT_QUOTED_RE.sub(_stash, section)
    m = _FMT_PATTERN_RE.search(section)
    if not m:
        return None
    pattern = m.group(0)
    prefix, suffix = section[:m.start()], section[m.end():]

    scale = len(pattern) - len(pattern.rstrip(","))
    pattern = pattern.rstrip(",")
    if "%" in prefix or "%" in suffix:
        num *= 100.0
    num /= (1000.0 ** scale)

    if "." in pattern:
        decimals = len(pattern.split(".", 1)[1].replace(",", ""))
    else:
        decimals = 0
    grouped = "," in pattern.split(".", 1)[0]
    # Excel/Tableau round HALF AWAY FROM ZERO; Python's format() rounds half to EVEN, so a value
    # landing exactly on .5 renders one lower (2.5 -> "2" instead of "3"). Quantize explicitly so a
    # scaled figure like $2.5M does not silently disagree with the source render.
    try:
        q = decimal.Decimal(repr(num)).quantize(
            decimal.Decimal(1).scaleb(-decimals), rounding=decimal.ROUND_HALF_UP)
    except (decimal.InvalidOperation, ValueError):
        q = num
    text = ("{:,.%df}" % decimals).format(q) if grouped else ("{:.%df}" % decimals).format(q)

    lits = iter(literals)

    def _restore(s):
        return "".join(next(lits, "") if ch == "\x00" else ch for ch in s)

    return (_restore(prefix) + text + _restore(suffix)).strip()



def _resolve_dynamic_text_tokens(text, params):
    """Resolve/strip Tableau dynamic ``<...>`` markup in a dashboard text zone -> literal string.

    Tableau text zones can weave a live token into their caption -- most commonly
    ``<[Parameters].[<id>]>``, which renders as the parameter's CURRENT display value (e.g. the
    "Show by Dimension" table-column header that reads "Program Name"). The deterministic emit path
    has no live parameter, so a raw token would surface as literal ``<[Parameters].[...]>`` markup on
    the page. We resolve every parameter token to its current display value (via ``params``) and
    strip any remaining unresolvable field-ref token (``<[ds].[field]>``) rather than leak markup.
    Returns the cleaned text (whitespace-collapsed); an all-token zone collapses to ``""`` so the
    caller can drop the now-empty text object. Byte-identical for text carrying no dynamic token."""
    if not text or "<" not in text:
        return text

    def _sub_param(mo):
        pid = mo.group("pid")
        info = params.get(pid) if params else None
        disp = info.get("current_display") if info else None
        return disp if disp else ""

    out = _PARAM_TOKEN_RE.sub(_sub_param, text)
    out = _FIELD_TOKEN_RE.sub("", out)
    # Collapse only horizontal whitespace so a hard line break (Æ-sentinel newline) is preserved.
    out = re.sub(r"[ \t]+", " ", out)
    return "\n".join(ln.strip() for ln in out.split("\n")).strip()


def _filter_is_unrestricted(filt):
    """True when a categorical filter narrows NOTHING -- Tableau's "(All)" state.

    Serialised as a lone ``<groupfilter function='level-members' ui-enumeration='all'>`` with no
    member children. Distinguished from a filter we simply could not READ (which also yields no
    selection) so a title token can render ``All`` on the former and stay blank on the latter."""
    children = _children_local(filt, "groupfilter")
    if not children:
        return False
    for ch in children:
        if ch.get("function") != "level-members":
            return False
        if _attr_local(ch, "ui-enumeration") != "all":
            return False
        if _children_local(ch, "groupfilter"):
            return False
    return True


def _filter_member_display(raw):
    """Render one Tableau filter member literal as Tableau displays it in a title.

    ``"Big South"`` -> ``Big South`` (quotes are serialisation, not content) and ``#2026-06-21#`` ->
    ``6/21/2026`` (Tableau's default short-date display, no leading zeros). Anything else passes
    through stripped. Returns ``""`` for an empty member."""
    s = _strip_member_literal(raw)
    if not s:
        return ""
    m = re.match(r"^#(\d{4})-(\d{2})-(\d{2})(?:\s.*)?#$", s.strip())
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return "%d/%d/%s" % (mo, d, y)
    return s


# Tableau runtime specials that a title/text zone can embed. Only the ones whose value is knowable
# from the workbook itself are resolved; the rest stay unresolved so the caller can decline rather
# than invent a value (``<User Name>``/``<Server Name>`` depend on who is viewing, not on the file).
_RUNTIME_SPECIAL_RE = re.compile(
    r"<(Workbook Name|Sheet Name|Page Name|Data Update Time)>")


def _substitute_dynamic_tokens(text, params, field_values=None, specials=None):
    """Substitute every resolvable dynamic token; return ``(text, unresolved_tokens)``.

    Tableau weaves four kinds of live token into a title or text zone, and all four are resolvable
    from the workbook alone when the view pins them:

      * ``<[Parameters].[id]>`` -- the parameter's CURRENT display value (its alias when it has one,
        else its authored number format, else the raw literal);
      * ``<[ds].[field]>`` -- the field's value in this view's context, which is knowable exactly
        when the worksheet FILTERS that field to a single member (``Big South``) or leaves it
        unrestricted (``All``);
      * ``<Workbook Name>`` / ``<Sheet Name>`` -- names we already hold;
      * ``<Data Update Time>`` -- the extract's recorded refresh stamp.

    Anything still unresolved is returned to the caller rather than guessed at, so the two call
    sites can apply their own policy (a status band blanks them; a chart title declines entirely).
    """
    unresolved = []

    def _sub_param(mo):
        info = params.get(mo.group("pid")) if params else None
        disp = info.get("current_display") if info else None
        if disp:
            return str(disp)
        unresolved.append(mo.group(0))
        return ""

    out = _PARAM_TOKEN_RE.sub(_sub_param, text)

    def _sub_special(mo):
        val = (specials or {}).get(mo.group(1))
        if val:
            return str(val)
        # A special the workbook cannot answer (no Pages shelf -> <Page Name> is genuinely empty in
        # Tableau too) resolves to nothing WITHOUT being called unresolved, so it does not veto an
        # otherwise-static title.
        if mo.group(1) in (specials or {}):
            return ""
        unresolved.append(mo.group(0))
        return ""

    out = _RUNTIME_SPECIAL_RE.sub(_sub_special, out)

    def _sub_field(mo):
        key = mo.group(0)[1:-1].strip()
        val = (field_values or {}).get(key)
        if val is not None:
            return str(val)
        unresolved.append(mo.group(0))
        return ""

    out = _FIELD_TOKEN_RE.sub(_sub_field, out)
    # FAIL CLOSED. Anything still wrapped in <...> is a token shape none of the resolvers above
    # claimed -- a bare field name (``<Region>``), a runtime special we deliberately do not answer
    # (``<User Name>``), or something Tableau adds later. Report it as unresolved so a caller that
    # must not dangle still declines. Without this the residue would survive as RAW MARKUP in the
    # rendered text, which is strictly worse than dropping the title.
    unresolved.extend(_TITLE_DYNAMIC_RE.findall(out))
    return out, unresolved


def _worksheet_field_token_values(ws):
    """``{"[ds].[field]": display}`` for every field this worksheet's filters PIN to a value.

    Keyed on the exact raw token a title embeds, which is the same ``[datasource].[field-instance]``
    string the filter's ``column`` attribute carries -- so the lookup is a direct match, never a
    name heuristic."""
    out = {}
    for f in (ws.get("filters") or ()):
        tok = f.get("filter_token")
        disp = f.get("title_display")
        if tok and disp is not None:
            out["[%s].[%s]" % (tok[0], tok[1])] = disp
    return out


def _extract_update_time(root):
    """The extract's recorded refresh stamp (``<connection @update-time>``), or ``None``.

    Tableau writes this on the ``.hyper`` connection and renders it for ``<Data Update Time>``,
    normalising it to its short display style (``07/24/2026 11:33:40 AM`` -> ``7/24/2026 11:33:40
    AM``), which is what this returns.

    The CLOCK TIME is emitted exactly as recorded. Tableau renders this stamp in the VIEWER's time
    zone, which a static Power BI textbox cannot track -- converting here would swap a deterministic
    value for one that changes with whatever machine ran the build, so a corpus diff would flip
    between runs. The caller discloses the caveat instead."""
    for conn in root.iter():
        if conn.tag.split("}")[-1] != "connection":
            continue
        ut = (conn.get("update-time") or "").strip()
        if not ut:
            continue
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})\b(.*)$", ut)
        if m:
            return "%d/%d/%s%s" % (int(m.group(1)), int(m.group(2)), m.group(3), m.group(4))
        return ut
    return None


def _runtime_specials(sheet_name, workbook_name, update_time):
    """The runtime-special values this workbook can answer.

    A key present with an empty value resolves to empty text WITHOUT vetoing an otherwise-static
    title -- which is what Tableau itself does for ``<Page Name>`` on a view with no Pages shelf
    (confirmed against a source render: the token contributed nothing while the other five in the
    same title all rendered). A key that is ABSENT stays unresolved, so a title that depends on it
    declines rather than silently loses meaning."""
    return {
        "Sheet Name": sheet_name or "",
        "Workbook Name": workbook_name or "",
        "Data Update Time": update_time or "",
        "Page Name": "",
    }


def _resolve_caption_text(text, params, field_values=None, specials=None):
    """Resolve a caption-only worksheet's raw title to static display text (v2-3).

    A caption-only worksheet (see ``caption_only_raw`` in :func:`_parse_worksheet`) is a thin
    status / refresh / filter-breadcrumb bar whose title is usually *dynamic* -- woven from
    ``<[Parameters].[id]>`` tokens, federated field-refs (``<[ds].[field]>``), and Tableau runtime
    specials (``<Data Update Time>``, ``<Data Connection Name>``, ...). This renders it to a plain
    literal for the rebuilt textbox: every token this view PINS is substituted with its live display
    value (see :func:`_substitute_dynamic_tokens` -- parameters by alias/format, filtered fields by
    their selected member or ``All``, workbook/sheet names, the extract's refresh stamp), and any
    token that remains genuinely unknowable is stripped rather than leaked as raw markup. Collapses
    runs of horizontal whitespace (a hard newline is preserved); returns ``""`` for an all-token
    caption so the caller drops the now-empty band."""
    if not text:
        return ""
    out, _unresolved = _substitute_dynamic_tokens(text, params, field_values, specials)
    out = _TITLE_DYNAMIC_RE.sub("", out)   # strip anything still unresolved
    out = re.sub(r"[ \t]+", " ", out)
    return "\n".join(ln.strip() for ln in out.split("\n")).strip()


def _resolve_dynamic_title(text, params, field_values=None, specials=None):
    """Resolve a SUPPORTED visual's raw dynamic title to a static Power BI title, or ``None`` (v2-4).

    A non-KPI worksheet title that embeds a live Tableau token cannot be reproduced verbatim by the
    deterministic emit path (there is no live parameter / field value at build time). Unlike a
    caption-only status bar (:func:`_resolve_caption_text`, which blanks every unresolved token because
    a thin band of label scaffolding still reads sensibly), a CHART title must never degrade to a
    dangling label -- "Days to Ship for <Category>" stripped to "Days to Ship for" reads as broken. So
    the rule is deliberately conservative and targets the case the view PINS: substitute every token
    whose value this worksheet fixes (see :func:`_substitute_dynamic_tokens` -- a parameter's current
    display, a field the sheet filters to one member or leaves unrestricted, the workbook/sheet name,
    the extract's refresh stamp), then KEEP the result ONLY when the title is now FULLY static
    (e.g. "Sales by <Show by Dimension>" -> "Sales by Program Name"). If any token remains
    unresolvable -- a per-row field whose value varies down the view -- return ``None`` so the caller
    drops the title (the rebuilt visual keeps its default) rather than emit a partial one. Returns
    the resolved static title, or ``None`` to drop (also ``None`` for empty input or a title that
    collapses to whitespace)."""
    if not text:
        return None
    out, unresolved = _substitute_dynamic_tokens(text, params, field_values, specials)
    if unresolved:
        return None  # something is still per-row unknowable -> drop, never dangle
    out = re.sub(r"[ \t]+", " ", out)
    out = "\n".join(ln.strip() for ln in out.split("\n")).strip()
    return out or None


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
              column_binding=None, param_binding=None, layout=LAYOUT_DEFAULT,
              workbook_name=None):
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
    ds_color_palettes = _datasource_mark_color_palettes(root)

    ws_holder = _children_local(root, "worksheets")
    ws_elems = []
    for h in ws_holder:
        ws_elems.extend(_children_local(h, "worksheet"))
    # Built once for the workbook -- see _shared_view_filter_index: the lookup walks the tree, and a
    # 49-worksheet workbook would otherwise walk it 49 times.
    shared_filters = _shared_view_filter_index(root)
    worksheets = []
    for ws in ws_elems:
        parsed = _parse_worksheet(ws, index, ds_caption, warnings,
                                  internal_fields=internal_fields, date_binding=date_binding,
                                  row_count_binding=row_count_binding,
                                  measure_binding=measure_binding,
                                  column_binding=column_binding,
                                  measure_palette=measure_palette,
                                 ds_color_palettes=ds_color_palettes,
                                 workbook_root=root,
                                 shared_filters=shared_filters)
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
        parsed = _parse_dashboard(db, worksheet_names, warnings, layout=layout)
        for z in parsed["zones"]:
            target = ws_by_name.get(z["worksheet"])
            if (target and target["visual_type"] == VT_UNSUPPORTED
                    and not target.get("caption_only_raw")):
                warnings.append(_warn(
                    "dashboard", parsed["name"],
                    f"worksheet '{z['worksheet']}' is unsupported -> zone left empty"))
        dashboards.append(parsed)

    params = _parse_parameters(root)
    update_time = _extract_update_time(root)
    # Resolve any live ``<[Parameters].[<id>]>`` / field-ref markup woven into a dashboard text zone
    # to the parameter's current display value (Tableau renders these as literal text -- e.g. the
    # "Program Name" column header driven by the "Show by Dimension" parameter). A text object that
    # was ONLY a dynamic token collapses to empty and is dropped, so no raw ``<[Parameters]...>``
    # markup ever reaches the page. Byte-identical for text objects that carry no dynamic token.
    for db in dashboards:
        resolved_tobs = []
        for tob in db.get("text_objects") or []:
            new_text = _resolve_dynamic_text_tokens(tob.get("text", ""), params)
            if not new_text:
                continue
            if new_text != tob.get("text"):
                tob = dict(tob, text=new_text)
            resolved_tobs.append(tob)
        db["text_objects"] = resolved_tobs
    # v2-3: with parameters now known, resolve each caption-only worksheet's raw title to static
    # display text so the page assembler can rebuild the thin status / refresh / filter-breadcrumb
    # bar as a textbox (instead of dropping it to a blank band -- the "a thin view doesn't generate
    # at all" defect). A caption that resolves to non-empty text is recorded on ``caption_only_text``;
    # one that collapses to empty (an all-token caption with no static value) is left unset, so that
    # rare band is still dropped. Additive: a worksheet with no ``caption_only_raw`` is untouched.
    for w in worksheets:
        raw = w.get("caption_only_raw")
        if raw:
            w["caption_only_text"] = _resolve_caption_text(
                raw, params, _worksheet_field_token_values(w),
                _runtime_specials(w["name"], workbook_name, update_time))
    # v2-4: resolve each SUPPORTED visual's DEFERRED dynamic title now that parameters are known. A
    # title that becomes fully static after parameter substitution is kept (with its parsed style); one
    # that still carries an unresolvable field-ref / runtime special is dropped (title + style cleared)
    # and warned exactly as before -- so a parameter-driven title ("Sales by <Show by Dimension>" ->
    # "Sales by Program Name") is recovered while a per-row dynamic title never degrades to a dangling
    # label. Additive: a worksheet with no ``dynamic_title_raw`` is untouched.
    for w in worksheets:
        raw = w.get("dynamic_title_raw")
        if not raw:
            continue
        resolved = _resolve_dynamic_title(
            raw, params, _worksheet_field_token_values(w),
            _runtime_specials(w["name"], workbook_name, update_time))
        if resolved:
            w["title"] = resolved
            warnings.append(_warn(
                "worksheet", w["name"],
                "dynamic title resolved to its current parameter display value "
                f"({resolved!r}); a live re-selection at view time is not reproduced"))
        else:
            w["title"] = None
            w["title_style"] = None
            warnings.append(_warn(
                "worksheet", w["name"],
                "dynamic title (embeds a field/parameter reference) not reproduced as static text; "
                "the rebuilt visual keeps its default title"))
    parameter_controls = _resolve_parameter_controls(dashboards, params, warnings, param_binding)
    sheet_swaps = _detect_sheet_swaps(worksheets, dashboards, params, warnings)
    visual_flags = _resolve_visual_flags(param_binding, ws_by_name, warnings)
    # Scalar readers for what-if parameters, so a conditional-colour rule can COMPARE against a
    # parameter. Resolved here (the only scope holding ``param_binding``) and carried on the IR
    # rather than re-derived per visual, which is also how ``parameter_controls`` travels.
    parameter_values = _colour_param_values(param_binding)

    return {"worksheets": worksheets, "dashboards": dashboards,
            "sheet_swaps": sheet_swaps, "parameter_controls": parameter_controls,
            "visual_flags": visual_flags, "parameter_values": parameter_values,
            "warnings": warnings}


# -- PBIR field expression emission --------------------------------------------
def _model_bound_category(field, field_map=None):
    """Whether a categorical colour pill can safely become a Series/Legend projection.

    Tableau's Colour shelf splits marks by the coloured dimension, which is exactly Power BI's
    Series/Legend well -- so a colour dimension SHOULD project. A raw column always can. A CALC
    dimension can only be projected when the model build actually materialised it as a column; a
    calc that never became one (e.g. a row-level calc reaching a Tableau parameter, which cannot
    be a calculated column because it would freeze at refresh time) would bind to a dangling
    reference, so it abstains and the caller defers with its existing warning.

    ``column_rebound`` / ``date_rebound`` are the model build's own authoritative statements that
    the field WAS materialised, and ``field_map`` is that build's manifest of real model columns
    (see :func:`_apply_override`); a calc present in either is safe to project. A blanket refusal
    of every calc -- the previous rule, applied at all 8 chart-type sites -- silently dropped the
    legend on the many real dashboards whose colour rule is a calc.
    """
    if not field or field.get("kind") != "category":
        return False
    if not field.get("is_calc"):
        return True
    if field.get("column_rebound") or field.get("date_rebound"):
        return True
    return bool(field_map) and field.get("caption") in field_map


def _apply_override(field, model_table, field_map):
    """Return (entity, property, binding) after applying caller overrides.

    A field already rebound to the marked Date dimension by ``_rebind_date_axis`` is AUTHORITATIVE:
    neither ``field_map`` nor the ``model_table`` fallback may pull the active date axis back onto the
    fact's raw date column, so the model build's date facts win over the published-DS column rebind.

    A calc DIMENSION resolved by the model build's ``column_binding`` manifest (``column_rebound``) is
    AUTHORITATIVE for the same reason: the model materialised it into a specific table (a field-parameter
    axis lands in its OWN ``calculated`` table, e.g. ``'Choose Date'[Choose Date]``), so the
    ``model_table`` fallback must not re-pin it onto the fact and produce a dangling ``Sheet1[<calc>]``.

    A calc MEASURE resolved by ``measure_binding`` (``measure_rebound``) is AUTHORITATIVE on the same
    grounds, and its absence here was a silent data defect rather than a naming one. ``field_map`` is
    keyed by CAPTION and its targets are always model COLUMNS (see below), so a calc whose caption
    matches a physical column -- which is the normal case, since a quick table calc over ``[Sales]``
    is captioned ``Sales`` -- was retargeted from its own measure onto that raw column AND flipped
    ``measure`` -> ``column``, after which the value-pill aggregation recovery re-emitted it as a plain
    ``Sum(Orders.Sales)``.

    Measured 2026-08-07: the model translated ``cum:sum:Sales:qk`` to a real
    ``Sales (running total (cumulative))`` measure and ``win:sum:Sales:qk`` to ``Sales (moving
    window)``; both sat in the emitted ``_Measures.tmdl`` and NO visual referenced either. The chart
    showed the raw un-accumulated measure. Nothing warned, because every layer believed it had
    succeeded -- the model emitted its measure, the Visual-Calculation path correctly yielded to it
    ("the model owns this transform"), and only this override quietly undid the binding in between.
    """
    entity, prop, binding = field["entity"], field["property"], field["binding"]
    # A model object is named from its Tableau caption STRIPPED, so a caption carrying stray
    # leading/trailing whitespace -- which authors leave constantly, and which Tableau renders no
    # differently -- would emit a reference naming an object that does not exist. Measured on a real
    # workbook: a caption ``'Weighted Rank Score '`` produced a report reference the model-vs-report
    # crosscheck could not match, so the whole column was dropped from the table while its correctly
    # translated measure sat unused in the model. Normalised HERE, at the single choke point every
    # reference shape derives ``prop`` from, so ``Property``, ``queryRef`` and ``nativeQueryRef``
    # cannot disagree AT THIS POINT.
    #
    # SCOPE, because the previous wording overstated and mis-ranked a real investigation twice.
    # It read "...can never disagree about the name -- a mismatch between them renders an error
    # tile", and BOTH halves need qualifying:
    #
    #   * "never" is true of this function, not of the artifact. A LATER stage rewrites
    #     ``field.Measure.Property`` without re-deriving the labels -- see
    #     ``migrate_estate._apply_row_predicate_wrapped_measures``, which points a projection at a
    #     ``CALCULATE(<base>, FILTER(...))`` wrapper while deliberately leaving ``nativeQueryRef``
    #     as the user-facing Tableau name. That is intended output, not a bypass: 55 projections
    #     across 39 visuals in one corpus workbook. Rebinding them "back" to the base would drop
    #     the filter and silently change every one of those numbers.
    #   * "renders an error tile" is the consequence of the condition MEASURED ABOVE -- a reference
    #     naming an object the model does NOT declare. It does not transfer to a name mismatch whose
    #     ``Property`` resolves: 59 of 59 divergent projections across the 34-workbook corpus resolve
    #     to a declared object, with a green suite and no error tiles.
    #
    # Stated because the fused form was unfalsifiable in place -- checking the consequence required a
    # population the mechanism clause never described -- and because a comment that makes the next
    # reader either panic or dismiss it fails in both directions at once.
    if isinstance(prop, str):
        prop = prop.strip() or prop
    if field.get("date_rebound") or field.get("column_rebound") or field.get("measure_rebound"):
        return entity, prop, binding
    # A caption is only a unique name in a SINGLE-datasource workbook -- and not even then across
    # tables. Tableau duplicates its datasource per dashboard, so a consolidated model holds one copy
    # of the same physical table per datasource (``pmdm__Program__c`` + ``pmdm__Program__c (Intake)``),
    # and the bare-caption map is first-writer-wins: every worksheet outside the first datasource
    # bound to a table with NO relationship to its own facts. Its slicers then filtered nothing and
    # grouping by it returned the grand total on every row -- silently, because the reference itself
    # resolves. Resolve on the pill's own (datasource, RELATION, caption) -- ``field["entity"]`` is
    # still the Tableau relation here, before any override -- and fall back to the bare caption so
    # single-datasource output is unchanged.
    ov = None
    if field_map:
        _ds = field.get("datasource")
        if _ds and entity:
            ov = field_map.get("%s||%s||%s" % (_ds, entity, field["caption"]))
        if ov is None and _ds:
            # The relation name did not match -- an EXTRACTED datasource carries two relations for
            # the same logical table (the live one and ``Extract``), and a worksheet bound to the
            # extract names the one the model did not key. Fall back to the pill's own DATASOURCE,
            # which is still far narrower than the bare caption: this key exists only where the
            # caption is unambiguous within that datasource (issue #103).
            ov = field_map.get("%s||%s" % (_ds, field["caption"]))
        if ov is None:
            ov = field_map.get(field["caption"])
    if ov is not None:
        entity = ov.get("entity", entity)
        prop = ov.get("property", prop)
        if isinstance(prop, str):
            prop = prop.strip() or prop
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
    # A VALUE pill (kind="value") that carries a Tableau aggregation in its ``derivation`` but has been
    # redirected to a model COLUMN (e.g. a calc materialised as a column, so ``_apply_override`` flipped
    # ``measure`` -> ``column``) must still aggregate. A bare column in a value well cannot scalarise --
    # a Power BI card/KPI over an un-aggregated column with ``summarizeBy: none`` renders an error visual
    # ("See details"). Recover the source aggregation from ``derivation`` (dropped by the measure->column
    # flip, which never carried it onto ``aggregation``) so e.g. ``AVG([Days to Close])`` stays an average.
    if binding == "column" and field.get("kind") == "value":
        deriv = field.get("derivation")
        dt = field.get("datatype")
        if deriv in _AGG_FUNC and not (
                (deriv in _NUMERIC_AGGS and dt not in _NUMERIC_TYPES)
                or (deriv in ("Min", "Max") and dt not in (_NUMERIC_TYPES | _DATE_TYPES))):
            func = _AGG_FUNC[deriv]
            expr = {"Aggregation": {"Expression": column, "Function": func}}
            return expr, f"{deriv}({entity}.{prop})", f"{deriv} of {prop}"
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


# Tableau records each field's authored number format on its ``<column>`` as ``default-format``, a
# TYPE-PREFIXED pattern: one leading marker character selects the family and the remainder is an
# Excel-style pattern (``positive;negative`` sections, ``,`` scaling, quoted literals) -- which is
# the same dialect Power BI's format strings use. Observed across the corpus:
#
#     n#,##0;-#,##0                        number
#     c"$"#,##0,,.00M;-"$"#,##0,,.00M      currency, millions-scaled
#     p0.0%                                percent
#     *<up>0.0%;<down>0.0%                 custom -- the arrow glyphs the author drew into the format
#
# The emitter previously read this attribute NOWHERE, so every measure rendered raw: ``339851``
# instead of ``339,851``, and a month-over-month delta as ``0.0070`` instead of its authored arrow.
# Only the numeric families are mapped. Tableau's DATE patterns use its own token vocabulary rather
# than the .NET/Excel one, so ``d``-prefixed formats are deliberately left alone rather than passed
# through and silently mis-rendered.
_TABLEAU_FORMAT_MARKERS = "ncp*"


def _tableau_number_format(default_format):
    """Tableau ``default-format`` -> a Power BI format string (or ``None`` to leave it default).

    Fail-closed: anything without a recognised numeric marker, or with an empty pattern after it,
    yields ``None`` so the value keeps Power BI's default rendering instead of being handed a
    pattern that might not mean the same thing.
    """
    fmt = (default_format or "").strip()
    if len(fmt) < 2 or fmt[0] not in _TABLEAU_FORMAT_MARKERS:
        return None
    pattern = fmt[1:].strip()
    return pattern or None


def _role_projections(fields, model_table, field_map, used_refs, pairs_out=None):
    """Fields -> PBIR projections.

    ``pairs_out``, when a list is supplied, collects ``(field, projection)`` for every field that
    produced exactly one projection. Purely an OUT parameter -- it lets a caller address the
    projection a given field became without re-deriving this function's skip rules. Hierarchy
    expansions produce several projections for one field and are deliberately not paired.
    """
    out = []
    for f in fields:
        if f.get("hierarchy"):
            out.extend(_hierarchy_level_projections(f, used_refs))
            continue
        # A field that resolves to an EMPTY (or quotes-only) property -- e.g. a Tableau degenerate
        # spacer calc named "" whose formula is the empty-string literal "" -- can only form a
        # dangling ``Entity[""]`` reference, which errors the whole visual at render. Skip it: it
        # never named a real column.
        _e, _p, _b = _apply_override(f, model_table, field_map)
        if not (_p or "").strip().strip("\"'").strip():
            continue
        proj = _projection(f, model_table, field_map, used_refs)
        # The authored format rides on the projection (PBIR ``RoleProjection.format``, the same key
        # the Visual Calculation path already uses). Applied to VALUE-kind fields only: a measure's
        # format is unambiguous, whereas a date/category axis format is Tableau's own token dialect.
        _fmt = f.get("number_format")
        if _fmt and f.get("kind") == "value" and "format" not in proj:
            proj["format"] = _fmt
        out.append(proj)
        if pairs_out is not None:
            pairs_out.append((f, proj))
    return out


def _slot_group_name(captions, taken):
    """A display name for a collapsed slot, derived from what its members SHARE.

    Naming it after one member would claim a direction the card may not be showing ("Pos MoM
    Revenue" on a month that fell), so the name is built from the words common to EVERY member, in
    the first member's order. When the members share no word at all it falls back to the first
    caption -- honest, since there is nothing else to say. Uniquified against ``taken`` so the DAX
    reference cannot be ambiguous.
    """
    words = [c.split() for c in captions if c]
    if not words:
        return None
    common = set(words[0])
    for w in words[1:]:
        common &= set(w)
    name = " ".join([w for w in words[0] if w in common]).strip() or captions[0]
    base, i = name, 1
    while name in taken:
        i += 1
        name = "{0} {1}".format(base, i)
    return name


def _collapse_label_slot_projections(pairs, projections, ws):
    """Collapse each MUTUALLY EXCLUSIVE mark-label slot into one COALESCE Visual Calculation.

    Tableau lets one mark label stack several measures in a single display position: exactly one is
    non-blank in any given period and its COLOUR carries the meaning (green up / red down / grey
    flat). Projected as siblings they become one real row plus N-1 rows reading ``(Blank)``. The
    template says which pills share a position -- see ``_parse_label_slots`` -- so each such group
    is hidden behind a single ``COALESCE`` Visual Calculation, the shape the base measures already
    support and which is render-verified on a ``multiRowCard``.

    TWO INDEPENDENT GUARDS, both required, both structural (no name matching), and both measured
    against a real workbook rather than assumed:

    * **every member is value-kind.** A dimension slot stacks genuinely DIFFERENT attributes -- an
      aircraft tile pairs a label calc with ``aircraft_type``, and a summary tile stacks Distance,
      Hub City and Hub Country. Those must all keep rendering.
    * **the members carry >= 2 distinct font colours.** Direction colouring IS the idiom; when the
      author did not colour-code we decline rather than guess, because a wrong collapse LOSES data
      whereas a declined one merely leaves today's behaviour in place.

    On the reference workbook these two guards agree exactly: all 14 delta groups are 3-colour
    measure groups and all 7 rejected groups are single-colour DIMENSION groups.

    Records a ``fidelity_note`` (not a warning) -- the collapse is a faithful automatic reshaping,
    not something needing manual attention -- so the decision stays auditable in the report.
    """
    label_slots = ws.get("label_slots")
    if not label_slots or not projections:
        return projections
    by_token = {}
    for f, proj in pairs:
        tok = f.get("label_token")
        if tok and tok not in by_token:
            by_token[tok] = (f, proj)

    out = list(projections)
    taken = {p.get("nativeQueryRef") for p in out if p.get("nativeQueryRef")}
    collapsed = []
    for slot in label_slots:
        tokens = slot.get("tokens") or []
        if len(tokens) < 2:
            continue
        keys = [_split_token(t) for t in tokens]
        members = [by_token[k] for k in keys if k in by_token]
        if len(members) < 2:
            continue
        if any(f.get("kind") != "value" for f, _ in members):
            continue
        if len({c for c in (slot.get("colors") or []) if c}) < 2:
            continue
        refs = [p.get("nativeQueryRef") for _, p in members]
        if not all(refs) or len(set(refs)) != len(refs):
            continue
        name = _slot_group_name([f.get("caption") or "" for f, _ in members], taken)
        if not name:
            continue
        taken.add(name)
        for _, p in members:
            p["hidden"] = True
        vc = {"field": {"NativeVisualCalculation": {
                  "Language": "dax",
                  "Expression": "COALESCE({0})".format(
                      ", ".join("[{0}]".format(r) for r in refs)),
                  "Name": name}},
              "queryRef": "select_vc{0}".format(len(collapsed)),
              "nativeQueryRef": name}
        # The members' authored format (the arrow patterns) belongs to the value that is SHOWN, so
        # it moves onto the calculation; the hidden bases display nothing.
        fmt = next((p.get("format") for _, p in members if p.get("format")), None)
        if fmt:
            vc["format"] = fmt
        # Insert where the group began, so the card keeps the author's slot order.
        out.insert(min(out.index(p) for _, p in members), vc)
        collapsed.append("{0} <- {1}".format(name, " | ".join(refs)))
    if collapsed:
        note = ("mark-label slots stacking mutually exclusive measures in one display position "
                "collapsed to COALESCE visual calculations ({0}), so the card shows the live "
                "value instead of (Blank) rows".format("; ".join(collapsed)))
        ws["fidelity_note"] = (ws["fidelity_note"] + "; " + note
                               if ws.get("fidelity_note") else note)
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


def _expr_entity(field_expr):
    """The source Entity (model-table) name carried by a projection's ``field`` expression, or
    ``None`` when it cannot be read (a literal / unrecognised shape). Handles the Column,
    Aggregation, Measure and HierarchyLevel expression forms the emitter produces."""
    if not isinstance(field_expr, dict):
        return None
    for key in ("Column", "Measure"):
        node = field_expr.get(key)
        if isinstance(node, dict):
            ref = ((node.get("Expression") or {}).get("SourceRef")) or {}
            return ref.get("Entity")
    agg = field_expr.get("Aggregation")
    if isinstance(agg, dict):
        col = (agg.get("Expression") or {}).get("Column") or {}
        ref = ((col.get("Expression") or {}).get("SourceRef")) or {}
        return ref.get("Entity")
    hier = field_expr.get("HierarchyLevel")
    if isinstance(hier, dict):
        h = (hier.get("Expression") or {}).get("Hierarchy") or {}
        ref = ((h.get("Expression") or {}).get("SourceRef")) or {}
        return ref.get("Entity")
    return None


def _uniquify_native_ref(base, seen):
    """Return ``base`` if unused, else ``base 2`` / ``base 3`` ... -- the first free variant."""
    if base not in seen:
        return base
    n = 2
    while f"{base} {n}" in seen:
        n += 1
    return f"{base} {n}"


def _dedupe_native_query_refs(state):
    """sf-npo Lesson 2: two projections in ONE visual that serialize the SAME ``nativeQueryRef``
    (e.g. ``Program[Name]`` + ``Service[Name]`` both -> ``'Name'``) collide in the visual query and
    render 'Error fetching data'. ``queryRef`` (the DAX SELECT alias) is already uniquified per
    visual; this guarantees ``nativeQueryRef`` is too. The FIRST occurrence keeps its clean native
    name; each later collision is qualified with its source entity (``'Name (Service)'``), then a
    numeric counter if that still clashes. Pure serialization guard -- no LLM judgment, no effect on
    ``queryRef``-keyed bindings (sort / FillRule / backColor), which are untouched. A no-op for the
    common case where every native name is already distinct."""
    seen = set()
    for role in state.values():
        if not isinstance(role, dict):
            continue
        for proj in role.get("projections", []):
            nref = proj.get("nativeQueryRef")
            if not nref:
                continue
            if nref not in seen:
                seen.add(nref)
                continue
            entity = _expr_entity(proj.get("field"))
            base = f"{nref} ({entity})" if entity else nref
            new_ref = _uniquify_native_ref(base, seen)
            proj["nativeQueryRef"] = new_ref
            seen.add(new_ref)
    return state


def _build_query_state(ws, model_table, field_map, warnings):
    """Map a worksheet IR to a PBIR ``queryState`` (role -> projections)."""
    vt = ws["visual_type"]
    used_refs = set()

    rows, cols = ws["rows"], ws["cols"]
    color = ws["encodings"]["color"]
    label = ws["encodings"]["label"]
    # EVERY Text/Label pill, in template order (see ``_parse_encodings``). ``label`` remains the
    # first one so all other roles are unchanged; only the card path widens to the full set.
    label_fields = ws["encodings"].get("label_fields") or ([label] if label else [])
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
        series = [color] if (_model_bound_category(color, field_map)) else []
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
        breakdown = [color] if (_model_bound_category(color, field_map)) else []
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
        series = [color] if (_model_bound_category(color, field_map)) else []
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
    elif vt == VT_TREEMAP:
        # Group = the tiling dimension(s): Text/Detail, or a categorical Colour. Values = the SIZE
        # measure (tile area); when there is no Size, fall back to a value Colour/Label so a single-
        # measure treemap still sizes its tiles. A *continuous* Colour measure is NOT a second Values
        # -- it drives the tile fill via _chart_continuous_fill. Extra category dimensions beyond the
        # first -> Details. Role keys Group / Details / Values match a real Desktop treemap visual.json.
        #
        # CALC-DIMENSION GOTCHA: do NOT drop_calc_axis() the Group. drop_calc_axis only drops calc
        # *measures* (is_calc and binding=="measure"); a calc *dimension* (a field-parameter "swap"
        # field like `Dim Swap calc 2`, is_calc=True, binding=="column") is a VALID treemap Group and
        # must be kept -- dropping it would leave the treemap with no Group and silently vanish the
        # visual (and, if it is the page's only visual, the whole page).
        group = _dedupe([f for f in (label, detail, color)
                         if f and f["kind"] == "category"])
        val = _dedupe(
            ([size] if size and size["kind"] == "value" else [])
            or ([color] if color and color["kind"] == "value" else [])
            or ([label] if label and label["kind"] == "value" else []))
        primary, details = group[:1], group[1:]
        if primary:
            state["Group"] = {"projections": _role_projections(
                primary, model_table, field_map, used_refs)}
        if details:
            state["Details"] = {"projections": _role_projections(
                details, model_table, field_map, used_refs)}
        if val:
            state["Values"] = {"projections": _role_projections(
                val, model_table, field_map, used_refs)}
    elif vt in (VT_COLUMN, VT_BAR):
        cat = drop_calc_axis(_dedupe(categories(rows) + categories(cols)))
        val = _dedupe(values(rows) + values(cols))
        series = [color] if (_model_bound_category(color, field_map)) else []
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
        color_series = [color] if (_model_bound_category(color, field_map)) else []
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
            # The small-multiples role is "Rows" (displayName "Small multiples"), NOT
            # "SmallMultiple". Confirmed on the installed capabilities for lineChart,
            # stackedAreaChart and the clustered/stacked column+bar family; the invented name is
            # rejected as PBIR_ROLE_UNKNOWN and the paning dimension is simply lost (issue #100).
            state["Rows"] = {"projections": _role_projections(
                small, model_table, field_map, used_refs)}
    elif vt == VT_MATRIX:
        row_dims = drop_calc_axis(_dedupe(categories(rows)))
        col_dims = drop_calc_axis(_dedupe(categories(cols)))
        # a highlight table carries its measure on the colour (saturation) OR size encoding; in a
        # Tier-1 matrix that measure is the displayed Values (the colour/size styling itself is
        # deferred). Promoting the size measure keeps a heat-grid sized by a measure (rows x cols,
        # measure on Size, e.g. a "days to ship" grid) from having NO Values and being dropped as an
        # empty matrix -- its whole dashboard page would then silently vanish.
        vals = _dedupe(values(rows) + values(cols)
                       + ([color] if color and color["kind"] == "value" else [])
                       + ([label] if label and label["kind"] == "value" else [])
                       + ([size] if size and size["kind"] == "value" else []))
        # Heat-grid colour DRIVER -> not a visible column. When a colour encoding colours a
        # DISTINCT displayed value (Tableau "colour by a different field"), the colour measure must
        # not appear as its own matrix column -- it only drives the cell colour, which the
        # `FillRule` / `Field value` expression references by MODEL name and therefore does not need
        # projected into the query. Fires for a continuous gradient AND for a DISCRETE colour
        # measure: the latter is a STRING measure, so leaving it projected renders a literal
        # ``negative``/``positive`` text column next to the numbers the author actually asked for.
        # Only fires when there is another displayed value, so the classic highlight table
        # (colour == the shown measure) is unchanged.
        #
        # It used to ride along in a ``Tooltips`` role. A pivotTable has NO Tooltips well, so that
        # role made the visual invalid and it failed to render -- adjudicated on a ground-truth run
        # as a deterministic-rule defect ("remove invalid Tooltips role from pivotTable", 2 pages).
        # Dropping the projection entirely is both the fix and the faithful shape.
        if (ws.get("color_gradient") or (color or {}).get("discrete_measure")) \
                and color and color["kind"] == "value":
            ck = (color["entity"], color["property"], color["binding"], color["aggregation"])
            others = [f for f in vals
                      if (f["entity"], f["property"], f["binding"], f["aggregation"]) != ck]
            if others:
                vals = others
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
        if not ordered:
            # Encoding-only display (Automatic/text mark with the field(s) on label / colour /
            # detail and no axis pills): list whatever single dimension was placed on the marks
            # card as a one-column table. A calculated DIMENSION binds as a real model column
            # (binding="column"), so keep it -- only a calc MEASURE (binding="measure") has no
            # faithful category binding and is dropped, matching drop_calc_axis on the axis path.
            # (Dropping the calc dimension here would leave the table with no Values and silently
            # remove the whole worksheet -- and, if it is the last one, its dashboard page.)
            ordered = _dedupe([f for f in (label, color, detail)
                               if f and f["kind"] == "category"
                               and not (f["is_calc"] and f["binding"] == "measure")])
        if ordered:
            state["Values"] = {"projections": _role_projections(
                ordered, model_table, field_map, used_refs)}
    elif vt == VT_SCATTER:
        x = _dedupe(values(cols))   # measure(s) on columns -> X axis
        y = _dedupe(values(rows))   # measure(s) on rows    -> Y axis
        # Every category Detail pill is a disaggregating dimension (Tableau allows many); bind them
        # all to Category/Details so the scatter plots one mark per granularity combination, not
        # just the first Detail pill enc["detail"] happened to capture.
        detail_dims = [f for f in ws["encodings"].get("detail_dims", [])
                       if f and f["kind"] == "category"]
        cat = drop_calc_axis(_dedupe(
            categories(rows) + categories(cols)
            + ([detail] if detail and detail["kind"] == "category" else [])
            + detail_dims))
        series = [color] if (_model_bound_category(color, field_map)) else []
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
            # ONE field only. Power BI's scatter Values/Details well is ``maxPerRole = 1``; several
            # projections is a hard PBIR_ROLE_MAX_EXCEEDED, and dropping the extras silently changes
            # the mark grain (measured: keeping the wrong pill collapsed ~5,000 marks into 3, and it
            # rendered perfectly). Microsoft's documented answer is to concatenate the dimensions
            # into one field that "must be unique for each point you want to plot" -- the model emits
            # exactly that key (see ``scatter_composite_keys``), so bind it here.
            if len(cat) > 1:
                _tables = {f.get("entity") for f in cat if f.get("entity")}
                _key_name = scatter_composite_key_name(cat)
                if len(_tables) == 1:
                    _dim_names = ", ".join(
                        repr(f.get("caption") or f.get("property")) for f in cat)
                    warnings.append(_warn(
                        "worksheet", ws["name"],
                        "scatter grain: Tableau plots one mark per distinct combination of %d "
                        "dimensions (%s), but a Power BI scatter takes ONE field in Values. The "
                        "dimensions are folded into a composite key column %r so the mark count is "
                        "preserved exactly. The individual dimensions cannot be shown on the native "
                        "scatter tooltip (it accepts measures only) -- add a report-page tooltip if "
                        "they need to be readable on hover"
                        % (len(cat), _dim_names, _key_name)))
                    cat = [{**cat[0], "property": _key_name, "caption": _key_name,
                            "entity": next(iter(_tables)), "binding": "column",
                            "kind": "category", "aggregation": None,
                            "is_calc": False, "derivation": "None",
                            "composite_scatter_key": True}]
                else:
                    # A key spanning two tables is not a column. Keep the finest single pill and say
                    # so, rather than emitting a visual the validator rejects.
                    warnings.append(_warn(
                        "worksheet", ws["name"],
                        "scatter grain: %d dimensions span more than one table, so no single "
                        "composite key column can be built; only %r is bound and the mark grain is "
                        "COARSER than Tableau's"
                        % (len(cat), cat[0].get("caption") or cat[0].get("property"))))
                    cat = cat[:1]
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
        # A Tableau KPI "BAN" arranges MANY Text pills into one rich mark label: a static caption,
        # a big number, and a set of MUTUALLY EXCLUSIVE coloured delta measures (exactly one is
        # non-blank in a given month, its colour carrying the direction). Binding only the first
        # pill rendered "(Blank)" on every card whose live value sat in a later slot. Project every
        # value-kind label pill so the live one is always present; ``_pbir_vtype`` then resolves
        # >=2 values to a native multiRowCard, Power BI's row of labelled big numbers.
        vals = _dedupe(values(rows) + values(cols)
                       + values(label_fields)
                       + ([size] if size and size["kind"] == "value" else []))
        if vals:
            _pairs = []
            _projs = _role_projections(vals, model_table, field_map, used_refs,
                                       pairs_out=_pairs)
            state["Values"] = {"projections": _collapse_label_slot_projections(
                _pairs, _projs, ws)}
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
            # azureMap has NO ``Value`` and no ``Gradient`` role (catalog describe azureMap ->
            # Category/Y/X/Series/Size/Tooltips/PathID/PointOrder). ``Tooltips`` IS a real measure
            # role, so the shading measure is genuinely in the dataview and the referenceLayer's
            # FillRule ``Input`` resolves against it.
            state["Tooltips"] = {"projections": _role_projections(
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
            # azureMap has no ``Gradient`` role; the shading measure rides ``Tooltips`` (a real
            # measure role) so the referenceLayer FillRule can resolve it. See VT_SHAPE_MAP above.
            state["Tooltips"] = {"projections": _role_projections(
                meas[:1], model_table, field_map, used_refs)}
        # a categorical (dimension) colour on the Color shelf is the map LEGEND -> the "Series"
        # role (a valid filledMap role on a real visual.json); each area is shaded by its legend
        # member. Mutually exclusive with Gradient by construction: Tableau's single Color shelf
        # holds either a measure (Gradient saturation) or a dimension (Series legend), never both.
        color_series = ([color] if (_model_bound_category(color, field_map)) else [])
        color_series = [f for f in color_series if f not in loc]
        if color_series:
            state["Series"] = {"projections": _role_projections(
                color_series, model_table, field_map, used_refs)}
    elif vt == VT_DENSITY_MAP:
        # Density / heat map: the geo dimension is the Category (Location) and the weighting measure
        # rides ``Size`` -- azureMap's heatMapLayer uses Size as its intensity field, exactly as
        # Tableau's Density mark weights by the measure on Size/Colour.
        loc = drop_calc_axis(_dedupe(finest_geo(detail)))
        meas = _dedupe(
            ([size] if size and size["kind"] == "value" else [])
            + ([color] if color and color["kind"] == "value" else [])
            + values(rows) + values(cols))
        if loc:
            state["Category"] = {"projections": _role_projections(
                loc, model_table, field_map, used_refs)}
        if meas:
            state["Size"] = {"projections": _role_projections(
                meas[:1], model_table, field_map, used_refs)}
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
            # sf-npo Lesson 8: keep the author's Size measure (faithful) but flag when an AVERAGE
            # drives bubble size -- it renders near-uniform radii, so the symbol map reads as
            # undifferentiated dots. The caveat nudges toward a count/sum measure on Size.
            _size_meas = size_sel[0]
            if _size_meas.get("aggregation") in _AVERAGE_AGGS:
                _size_label = (_size_meas.get("caption")
                               or _size_meas.get("property") or "the Size measure")
                warnings.append(_warn(
                    "worksheet", ws["name"],
                    f"symbol map sizes its bubbles by an average "
                    f"('{_size_meas['aggregation']}' of '{_size_label}'); an average produces "
                    f"near-uniform bubble radii -- prefer a count or sum measure on Size so the "
                    f"bubbles are differentiable"))
        if color_sel:
            # azureMap has no ``Gradient`` role; a symbol map's colour measure rides ``Tooltips``.
            state["Tooltips"] = {"projections": _role_projections(
                color_sel, model_table, field_map, used_refs)}
        # a categorical (dimension) colour binds the map LEGEND -> the "Series" role (verified on a
        # real classic "map" visual.json, e.g. Series=Continent); bubbles are coloured by legend
        # member. Disjoint from Gradient (above): Gradient takes colour only when it is a measure.
        color_series = ([color] if (_model_bound_category(color, field_map)) else [])
        color_series = [f for f in color_series if f not in loc]
        if color_series:
            state["Series"] = {"projections": _role_projections(
                color_series, model_table, field_map, used_refs)}
    return _dedupe_native_query_refs(state)


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
        # ``Gradient`` no longer exists on the emitted visual (azureMap has no such role -- the
        # colour measure moved to ``Tooltips``), so a symbol map whose only extra encoding was a
        # colour measure would have been judged degenerate and DROPPED.
        return "Category" in state and (
            "Size" in state or "Tooltips" in state or "Series" in state)
    if vt == VT_DENSITY_MAP:
        # A heat layer needs somewhere to draw: the Location is essential, the weighting measure is
        # optional (an unweighted density map is a valid point-density surface).
        return "Category" in state
    if vt == VT_MATRIX:
        return "Values" in state and ("Rows" in state or "Columns" in state)
    if vt == VT_TABLE:
        return "Values" in state
    if vt == VT_TREEMAP:
        # A faithful treemap needs at least a Group (the tiling) and Values (the sizing measure).
        return "Group" in state and "Values" in state
    return False


def _pbir_vtype(vt, state):
    """Resolve the PBIR ``visualType`` string; a card splits into card vs multiRowCard."""
    if vt == VT_CARD:
        n = len(state.get("Values", {}).get("projections", []))
        return "multiRowCard" if n > 1 else "card"
    # A colour DIMENSION on a bar/column/area mark stacks its segments within each bar/band by
    # default in Tableau ("Stack marks" is on by default for bar AND area marks). Power BI's
    # clustered* charts render the legend side-by-side and its plain areaChart OVERLAPS the bands,
    # so when a Series (legend) dimension is present the faithful default is the STACKED variant --
    # preserving the Tableau layout rather than silently re-rendering a stacked chart as grouped /
    # overlapping. (Default-stacking behaviour fact-checked against Tableau docs.) NOTE: Power BI
    # spells a stacked column/bar as the UNQUALIFIED "columnChart" / "barChart" -- the look-alikes
    # "stackedColumnChart" / "stackedBarChart" are NOT valid PBIR types (they render as a missing
    # custom visual; caught by pbir_lint / powerbi-report-author validate). The clustered variants
    # are "clusteredColumnChart" / "clusteredBarChart".
    if state.get("Series", {}).get("projections"):
        if vt == VT_COLUMN:
            return "columnChart"
        if vt == VT_BAR:
            return "barChart"
        if vt == VT_AREA:
            # Same "Stack marks" default as bars: a colour-dimension area chart stacks its bands.
            return "stackedAreaChart"
        if vt == VT_COMBO:
            # A dual-axis combo whose COLUMN family carries a colour-dimension legend stacks those
            # columns by default in Tableau; the faithful Power BI target is the stacked-column
            # combo. Same role wells as the clustered combo (Category / Y / Y2 / Series) -- only
            # the column-stacking differs, so the binding path is unchanged.
            return "lineStackedColumnComboChart"
    return _VT_TO_PBIR[vt]


def _bool_literal(value):
    """A PBIR semantic-query boolean literal (``{"expr": {"Literal": {"Value": "true"|"false"}}}``)."""
    return {"expr": {"Literal": {"Value": "true" if value else "false"}}}


def _detect_measure_trellis(ws, state):
    """Return the ordered list of Y measure projections when a bar/column worksheet is a Tableau
    *measure-trellis* (2+ DISTINCT measure pills ``+``-concatenated on one shelf, drawing one
    adjacent pane per measure), else ``None`` (fail-closed -> the existing single clustered chart,
    byte-identical).

    Tableau lays several measures side by side by concatenating separate pills with ``+`` on the
    Columns shelf (horizontal bars) or Rows shelf (vertical bars); each measure gets its OWN axis /
    pane. Power BI has no native multi-measure trellis -- a clustered bar/column with 2+ Y measures
    renders them as GROUPED bars sharing ONE axis, which reads as a single clustered block rather
    than the separate per-measure panels Tableau draws. The faithful rebuild is N side-by-side
    single-measure charts aligned on a shared category axis (see ``_emit_measure_trellis``).

    Guards (any miss -> ``None``):
      * the mark is a bar or column;
      * NO Series (a colour-dimension legend is a genuine grouped/stacked chart, not a trellis) and
        NO SmallMultiple role (already a native trellis);
      * 2+ Y measure projections and 1+ Category dimension;
      * the worksheet is not DUAL-AXIS. A trellis and a dual axis both put 2+ measure pills on one
        shelf, but they draw opposite things: a trellis gives each measure its OWN pane, a dual axis
        overlays them in ONE pane. Rebuilding a dual axis as a trellis splits a single plot area into
        separate charts -- measured on a ``SUM(Sales) + AVG(Sales)`` sheet whose Tableau render has
        both series spanning the full plot height from a shared baseline (so: one pane), which came
        out as two. A dual axis keeps the ordinary single clustered chart, where both measures share
        the one plot area;
      * the worksheet does NOT use the ``[Measure Values]`` shelf -- that pseudo-field is the
        clustered/series idiom (its member measures share ONE axis, routed elsewhere); only distinct
        ``+``-concatenated measure pills form the separate-pane trellis this rebuilds. (In Tableau
        the ONLY way to get grouped bars on one axis is Measure Values/Names, so distinct concatenated
        measures are always separate panes -- making this signature exact.)

    Only VISIBLE projections count. The signature this detects is a property of the SOURCE SHELF --
    two or more measure pills concatenated on it -- so a projection the visual computes but does not
    show can never be one of them. Measured on ``0060_adjustable_fixed_axis``, whose ``Challenge``
    worksheet carries a single ``pcto:sum:Sales:qk`` pill on ``<cols>`` (one percent-of-total quick
    table calc, no second measure): counting the hidden base measure that the quick-calc keeps only
    so its Visual Calculation can reference it inferred a trellis the shelf does not describe, and
    emitted TWO side-by-side charts where Tableau draws one pane -- the second of them drawing a
    projection explicitly marked ``hidden``. ``0088`` had the same shape. Corpus-wide this invented
    three bands.

    The colour Visual Calculation now adds a hidden projection too, which is what makes this
    exclusion load-bearing rather than merely tidy: without it, every view-scoped colour would
    turn its chart into a spurious two-band trellis.
    """
    if ws["visual_type"] not in (VT_BAR, VT_COLUMN):
        return None
    if ws.get("uses_measure_values"):
        return None
    if ws.get("dual_axis"):
        return None
    if state.get("Series", {}).get("projections"):
        return None
    if "Rows" in state:
        return None
    y_projs = [p for p in state.get("Y", {}).get("projections", []) if not p.get("hidden")]
    cat_projs = state.get("Category", {}).get("projections", [])
    if len(y_projs) < 2 or len(cat_projs) < 1:
        return None
    return list(y_projs)


def _emit_measure_trellis(ws, state, measures, x, y, w, h, tab,
                          page_name, page_display, model_table, field_map, vname_base,
                          sort_definition, label_objects, data_point_objects, warnings,
                          title=None, show_title=True):
    """Fan a measure-trellis into N single-measure ``clustered{Bar,Column}Chart`` visuals aligned on a
    shared category axis. Returns ``(visuals, records)``.

    ORIENTATION FOLLOWS THE SHELF THE MEASURES SIT ON, because that is what Tableau splits:

    * measures on COLUMNS (horizontal bars, category on Rows) -> panes SIDE BY SIDE, category labels
      down the left, so the FIRST band carries the label gutter and shows the category axis;
    * measures on ROWS (vertical bars, category on Columns) -> panes STACKED, category labels along
      the bottom, so the LAST band carries the gutter and shows the category axis.

    Emitting the horizontal fan for both was measured wrong on a two-measure column sheet: Tableau
    drew one pane above the other sharing the month axis, the rebuild drew them left and right, and
    the resulting pair read as two unrelated charts.

    Geometry: the label gutter is asymmetric, because the two axes need different room. CATEGORY
    LABELS DOWN THE LEFT (the fanned case) carry member names and need real WIDTH, so the band that
    shows them is given a double slot -- the source band is split into N+1 columns and the first
    spans two. CATEGORY LABELS ALONG THE BOTTOM (the stacked case) are a single row of text that
    Power BI draws inside the visual's own rectangle, so the bands are simply equal, matching
    Tableau's equal panes; giving the last band a double slot there would hand a third of the chart
    to a strip of month names. The numeric value axis is hidden on all (the data labels carry the
    numbers, matching the compact Tableau strip); every chart shares the SAME category binding +
    sort, so the bands stay aligned.

    Titles: the strip is ONE Tableau zone, so it carries at most one caption. When the zone shows its
    title, only the FIRST chart carries it and the rest are explicitly captionless; when the zone
    hides it, all of them are. "Explicitly" matters -- a Power BI visual that emits no title object
    does not render untitled, it renders an auto-generated field-name caption, which would put a
    different invented heading over every band.
    """
    visuals, records = [], []
    # BANDS ARE RECTANGLES, NOT MEASURES. A folded (dual) axis inside the strip means two measures
    # share ONE pane, so fanning per measure draws twice as many charts as the source and loses the
    # overlay in every one of them -- measured on ``0088 Service Provider Details``: six pills, three
    # folds, six single-measure charts where Tableau draws three overlaid pairs.
    #
    # ``fold_groups`` is aligned to the measure SHELF, so it is refused unless it accounts for
    # exactly the measures being fanned; a mismatch falls back to one band per measure, which is the
    # behaviour every non-folded sheet already had.
    groups = [list(g) for g in (ws.get("fold_groups") or [])]
    if sum(len(g) for g in groups) != len(measures) or not groups:
        groups = [[i] for i in range(len(measures))]
    styles = list(ws.get("pane_style_by_index") or ())
    if len(styles) != len(measures):
        styles = [{} for _ in measures]
    n = len(groups)
    # A column chart's measures came off the ROWS shelf, so its panes stack; a bar chart's came off
    # COLUMNS, so its panes sit side by side.
    stacked = ws["visual_type"] == VT_COLUMN
    # The band that carries the category labels is the one nearest them: bottom when stacked, left
    # when fanned. Only the fanned case reserves an extra slot for them (see above).
    gutter_k = n - 1 if stacked else 0
    u = (h / n if n else h) if stacked else (w / (n + 1) if n else w)
    for k, group in enumerate(groups):
        proj = measures[group[0]]
        cat_shown = (k == gutter_k)
        if stacked:
            xk, yk, wk, hk = x, y + k * u, w, u
        else:
            # Slots consumed before this band: the gutter band is two columns wide.
            xk, yk, wk, hk = x + (k if k == 0 else k + 1) * u, y, (2 * u if cat_shown else u), h
        sub_state = dict(state)
        # Each band keeps its OWN single measure -- plus any HIDDEN projection the parent declared,
        # because a formatting property may already reference one by ``SelectRef``. Dropping those
        # here is what made the first rung-4 attempt ship a dangling reference on exactly the
        # trellis bands (the property travelled in ``data_point_objects``, the projection did not).
        # A hidden projection is never a visible band measure, so carrying it changes nothing else.
        hidden = [p for p in (state.get("Y") or {}).get("projections", []) if p.get("hidden")]
        band_projs = [measures[i] for i in group]
        sub_state["Y"] = {"projections": band_projs + hidden}
        # Each band replaces Y with its OWN single measure, so a sort-by measure that was bound in
        # the parent's multi-measure Y is unbound in every other band -- and an unhonoured sort
        # still suppresses Power BI's default one, which would break exactly the row alignment this
        # strip depends on. Re-bind per band (copy-on-write, so bands cannot leak into each other),
        # and drop the sort for any band where it cannot be bound.
        sub_sort = sort_definition
        if sub_sort and ws.get("sort"):
            _e, _q, _n = _field_expression(ws["sort"]["field"], model_table, field_map)
            if not _bind_sort_field(sub_state, _e, _q, _n, ws["visual_type"]):
                sub_sort = None
        vtype = _pbir_vtype(ws["visual_type"], sub_state)
        vname_k = _sanitize(f"{vname_base}-mt{k}")
        pos = _position(xk, yk, wk, hk, tab=tab)
        extra = {
            "valueAxis": [{"properties": {"show": _bool_literal(False)}}],
            "categoryAxis": [{"properties": {"show": _bool_literal(cat_shown)}}],
        }
        # A band holding a folded PAIR is an overlay in its own right, so it carries the same
        # Overlap card and per-series style the single-rectangle route emits.
        band_overlay = len(group) > 1
        band_colors = [styles[i].get("color") for i in group] if band_overlay else None
        band_transp = (_lipstick_series_transparency(
            [{"instance": i} for i in group],
            {i: styles[i].get("transparency") for i in group},
            {i: styles[i].get("size") for i in group}) if band_overlay else None)
        k_title = title if (k == 0 and show_title) else None
        visuals.append(_visual_json(
            vname_k, vtype, pos, sub_state, sub_sort,
            title=k_title, show_title=bool(k_title),
            label_objects=label_objects, data_point_objects=data_point_objects,
            extra_objects=extra, container_fill=ws.get("canvas_fill"),
            lipstick_overlap=band_overlay,
            lipstick_series_colors=band_colors,
            lipstick_series_transparency=band_transp,
            continuous_axis=ws.get("continuous_axis")))
        rec = _candidate_record(page_name, vname_k, ws, vtype, sub_state, pos,
                                page_display=page_display,
                                model_table=model_table, field_map=field_map)
        rec["measure_trellis"] = {"index": k, "of": n,
                                  "measures_in_band": len(group),
                                  "overlaid_pair": band_overlay,
                                  "category_axis_shown": cat_shown,
                                  "orientation": "stacked" if stacked else "side-by-side"}
        records.append(rec)
    _paired = sum(1 for g in groups if len(g) > 1)
    _what = "measures" if not _paired else "panes (%d of them a folded dual-axis pair)" % _paired
    warnings.append(_warn(
        "worksheet", ws["name"],
        f"measure-trellis ({len(measures)} measures on one axis, {n} {_what}) rebuilt as {n} "
        f"{'stacked' if stacked else 'side-by-side'} charts aligned on a shared category axis "
        f"(Power BI has no native multi-measure trellis; the source draws one pane per rectangle)"))
    return visuals, records


def _emit_kpi_title_card(ws, kpi, x, y, w, h, tab, page_name, page_display,
                         model_table, field_map, vname_base):
    """Rebuild a KPI worksheet's title-embedded headline number as a companion Power BI ``card``.

    Returns ``(visual, record)`` for the card, or ``None`` when the embedded measure could not be
    resolved to a projection (the caller then leaves the sparkline captioned as-is). The card is
    bound to the title measure, styled in the source's authored number colour / size, captioned with
    the static title text, and placed in the TOP band of the zone; the caller shrinks the worksheet's
    own sparkline into the band below it. The auto category label (the measure name) is hidden -- the
    caption title already names the KPI, matching Tableau's caption-over-number layout.
    """
    projections = _role_projections(kpi["measure_fields"], model_table, field_map, set())
    if not projections:
        return None
    # TABLEAU'S "AUTOMATIC" NUMBER FORMAT IS NOT POWER BI'S. A Tableau measure that declares no
    # number format renders under Automatic, which suppresses the decimals on an aggregated value --
    # 2,326,534.35 is shown as "2,326,534", 745,567.53 as "745,568" (rounded, not truncated). Power
    # BI, given no format, prints the raw double, so a headline KPI came out as "2,326,534.35" where
    # An AUTHORED format always wins (``_role_projections`` has already put it on the projection);
    # this only fills the silence -- and only for a NUMBER. A trend-arrow tile is bound to a STRING
    # measure, where a numeric format string has nothing to format.
    _numeric = all((f.get("datatype") or "") != "string" for f in kpi["measure_fields"])
    if _numeric:
        for proj in projections:
            proj.setdefault("format", _TABLEAU_AUTOMATIC_NUMBER_FORMAT)
    state = {"Values": {"projections": projections}}
    pos = _position(x, y, w, h, tab=tab)
    value_props = {}
    if kpi.get("value_color"):
        value_props["color"] = {"solid": {"color": {"expr": {"Literal": {
            "Value": _semantic_string_literal(kpi["value_color"])}}}}}
    if kpi.get("value_size"):
        value_props["fontSize"] = {"expr": {"Literal": {"Value": kpi["value_size"]}}}
    card_label_objects = {"categoryLabels": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "false"}}}}}]}
    if value_props:
        # A ``card``'s big-number value object is ``labels`` (NOT ``dataLabels`` -- that is
        # ``multiRowCard``'s; a ``dataLabels`` on a ``card`` is rejected FORMATTING_OBJECT_UNKNOWN and
        # the colour/size are silently dropped at render). This KPI-title tile is always a ``card``.
        card_label_objects["labels"] = [{"properties": value_props}]
    vname = _sanitize(f"{vname_base}-kpi{kpi.get('ordinal') or ''}")
    visual = _visual_json(vname, "card", pos, state, None,
                          title=kpi["caption"], title_style=kpi.get("caption_style"),
                          show_title=bool(kpi["caption"]),
                          card_label_objects=card_label_objects)
    rec = _candidate_record(page_name, vname, ws, "card", state, pos,
                            page_display=page_display,
                            model_table=model_table, field_map=field_map)
    rec["kpi_title_card"] = {"caption": kpi["caption"]}
    return visual, rec


# Vertical breathing room a Power BI ``card`` adds around its caption + callout, in device pixels.
# Measured off a rendered 300x300 KPI tile.
_KPI_CARD_PADDING_PX = 12.0
# Tableau's "Automatic" number format for an aggregated measure: thousands separators, no decimals.
# Confirmed against four ground-truth KPI numbers in one workbook (2,326,534.35 -> "2,326,534";
# 745,567.53 -> "745,568"; 131,633.95 -> "131,634"; 145 -> "145").
_TABLEAU_AUTOMATIC_NUMBER_FORMAT = "#,0"
# No KPI text is shrunk below this; past it the number stops being a headline.
_KPI_MIN_FONT_PT = 7.0
# Width of a trend-arrow tile beside a KPI number, in device pixels. One glyph, so it only needs to
# clear the character plus the card's own padding.
_KPI_GLYPH_WIDTH_PX = 26


def _scaled_font_literal(size_literal, factor, fallback):
    """A ``'15D'`` font literal scaled by ``factor`` (floored), or ``None`` when there is nothing."""
    pts = _points(size_literal) or fallback
    if not pts:
        return None
    pts = max(_KPI_MIN_FONT_PT, pts * factor)
    pts = round(pts, 1)
    return "{0}D".format(int(pts) if pts == int(pts) else pts)


def _emit_kpi_title_cards(ws, x, y, w, h, tab, page_name, page_display,
                          model_table, field_map, vname_base, flag_fc=None):
    """Every KPI card a worksheet's title carries, stacked top-down -> ``(visuals, records, used_h)``.

    A Tableau KPI title can name SEVERAL metrics on its own lines ("Current: <this year>" /
    "vs Last Year: <delta>"); each becomes its own card, sharing the band equally, in title order.
    ``used_h`` is how much of the zone the cards consumed, so the caller can shrink the worksheet's
    own sparkline into whatever is left. ``(.., .., 0)`` when nothing resolved -- the caller then
    leaves the visual captioned as it was.
    """
    cards = ws.get("kpi_title_cards") or (
        [ws["kpi_title_card"]] if ws.get("kpi_title_card") else [])
    if not cards:
        return [], [], 0
    # HOW TALL THE BAND IS, from the text it holds. A fixed 58% of the zone put a 15pt number in the
    # middle of a half-empty plate and squashed the sparkline into the remainder -- the source draws
    # the caption and number as two ordinary lines at the TOP and gives everything else to the mark.
    # So each card is one caption line plus one value line at their AUTHORED point sizes: points ->
    # pixels is 96/72, and a rendered line box is about 1.25x its point size, hence 1.67 px per
    # point, plus the card's own padding.
    def _line_px(size_literal, fallback):
        return (_points(size_literal) or fallback) * 96.0 / 72.0 * 1.25

    natural = 0
    for card in cards:
        cap = (card.get("caption_style") or {}).get("font_size")
        natural += (_line_px(cap, _WORKSHEET_TITLE_DEFAULT_SIZE)
                    + _line_px(card.get("value_size"), _WORKSHEET_TITLE_DEFAULT_SIZE)
                    + _KPI_CARD_PADDING_PX)
    # A title-only worksheet has no mark to leave room for, so its cards take the whole zone; one
    # that also draws a mark keeps the band to what the text needs, capped so a tall caption can
    # never starve the chart.
    if ws.get("visual_type") == VT_UNSUPPORTED:
        band = h
    else:
        band = int(min(round(natural), round(h * 0.58)))
    band = max(1, band)
    each = max(1, band // len(cards))
    # FIT THE TEXT TO THE BAND THE AUTHOR DREW. Tableau renders "Current: 745,568" as ONE line -- the
    # label and the number side by side -- so two metrics fit a 67px zone comfortably. A Power BI card
    # STACKS its container title above its callout, needing roughly half as much again; at the same
    # 67px the 9pt caption and 15pt number overlapped and the number was clipped. Scaling both by the
    # same factor keeps the authored 9:15 contrast while fitting the zone, which is the closest a
    # stacked control gets to the source's inline line.
    fit = min(1.0, (each * len(cards)) / natural) if natural else 1.0
    visuals, records, used = [], [], 0
    for i, card in enumerate(cards):
        spec = dict(card)
        spec["ordinal"] = i or ""
        if fit < 1.0:
            spec["value_size"] = _scaled_font_literal(
                card.get("value_size"), fit, _WORKSHEET_TITLE_DEFAULT_SIZE)
            cap_style = dict(card.get("caption_style") or {})
            scaled_cap = _scaled_font_literal(
                cap_style.get("font_size"), fit, _WORKSHEET_TITLE_DEFAULT_SIZE)
            if scaled_cap:
                cap_style["font_size"] = scaled_cap
            spec["caption_style"] = cap_style or None
        # The trend arrow sits to the RIGHT of the number on the same line, as Tableau draws it.
        # Each glyph measure gets its own narrow, title-less card; exactly one is ever non-empty
        # (the paired calcs return a glyph or ""), so the reader sees a single arrow.
        glyphs = [g for g in (card.get("glyphs") or []) if g.get("measure_fields")]
        num_w = w - len(glyphs) * _KPI_GLYPH_WIDTH_PX if glyphs else w
        if num_w < _KPI_GLYPH_WIDTH_PX:
            glyphs, num_w = [], w
        out = _emit_kpi_title_card(
            ws, spec, x, y + i * each, num_w, each, tab, page_name, page_display,
            model_table, field_map, vname_base)
        if out is None:
            continue
        vis, rec = out
        visuals.append(_inherit_flag_filters([vis], flag_fc)[0] if flag_fc else vis)
        records.append(rec)
        for gi, g in enumerate(glyphs):
            gspec = {"caption": None, "measure_fields": g["measure_fields"],
                     "value_color": g.get("color"),
                     "value_size": (_scaled_font_literal(g.get("size"), fit,
                                                        _WORKSHEET_TITLE_DEFAULT_SIZE)
                                    if fit < 1.0 else g.get("size")),
                     "ordinal": "%s-g%d" % (i or "", gi)}
            gout = _emit_kpi_title_card(
                ws, gspec, x + num_w + gi * _KPI_GLYPH_WIDTH_PX, y + i * each,
                _KPI_GLYPH_WIDTH_PX, each, tab, page_name, page_display,
                model_table, field_map, vname_base)
            if gout is None:
                continue
            gvis, grec = gout
            visuals.append(_inherit_flag_filters([gvis], flag_fc)[0] if flag_fc else gvis)
            records.append(grec)
        used = (i + 1) * each
    return visuals, records, used


def _detect_native_pct_stacked(ws, state, vc_index):
    """Return the native 100%-stacked ``visualType`` when this worksheet is a color-legend, within-bar
    percent-of-total bar/column -- else ``None`` (fail-closed).

    A Tableau bar/column with a dimension on Colour and a Percent-of-Total quick calc on its measure
    normalizes each bar's coloured segments to sum to 100%. Power BI's native
    ``hundredPercentStacked{Bar,Column}Chart`` does exactly that same per-bar normalization on the RAW
    measure. So the faithful emit is: keep the field wells we already build (Category axis / Series
    legend / Y measure), pick the native 100%-stacked type, and bind the RAW measure -- letting Power
    BI normalize -- instead of a Visual Calculation whose ``COLLAPSE`` runs along the category axis
    (which would divide by each colour's cross-bar total, not each bar's cross-colour total).

    All of the following must hold (any other shape -> ``None`` -> existing behaviour is unchanged):
      * the mark is a bar or column;
      * exactly one Series (legend) dimension, one Category-axis dimension, and one Y measure;
      * exactly one view-only quick calc for the worksheet, a percent-of-total;
      * its spec is a within-partition percent (``collapse_all`` False -- each bar sums to 100%, not a
        grand-total percent); and
      * its addressing partitions by EXACTLY the Category-axis dimension while the Series/colour
        dimension is the normalized (addressed) one -- i.e. the percent runs across the stack within
        each bar. This is the deterministic guard that the native per-bar normalization is faithful.
    """
    if (usage_to_visual_calc_spec is None or resolve_addressing is None
            or FAMILY_PERCENT_OF_TOTAL is None):
        return None
    vt = ws.get("visual_type")
    if vt not in (VT_BAR, VT_COLUMN):
        return None
    series = (state.get("Series") or {}).get("projections") or []
    category = (state.get("Category") or {}).get("projections") or []
    yvals = (state.get("Y") or {}).get("projections") or []
    if len(series) != 1 or len(category) != 1 or len(yvals) != 1:
        return None
    usages = (vc_index or {}).get(ws["name"]) or []
    if len(usages) != 1:
        return None
    usage = usages[0]
    if getattr(usage, "calc_type", None) != "PctTotal":
        return None
    spec, _ = usage_to_visual_calc_spec(usage, role="value", visual_axis="ROWS")
    if spec is None or spec.family != FAMILY_PERCENT_OF_TOTAL or spec.collapse_all:
        return None
    _addressed, partition, _ordering = resolve_addressing(usage)
    if not partition:
        return None
    cat_ref = category[0].get("nativeQueryRef")
    series_ref = series[0].get("nativeQueryRef")
    if list(partition) != [cat_ref] or series_ref in partition:
        return None
    return ("hundredPercentStackedBarChart" if vt == VT_BAR
            else "hundredPercentStackedColumnChart")


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
        "columnChart": "barChart",
        "barChart": "columnChart",
        "hundredPercentStackedColumnChart": "hundredPercentStackedBarChart",
        "hundredPercentStackedBarChart": "hundredPercentStackedColumnChart",
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
    VT_TREEMAP: (["clusteredBarChart", "clusteredColumnChart"], "medium", None),
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
# Visual types that pair a Tooltips well with a category order a sort can act on, so a sort-by
# measure the chart does not otherwise show can ride along in the query. A tableEx / card / map /
# scatter has no such pairing (no Tooltips well, or no category order), so the rescue declines
# there and the sort stays unemitted rather than being emitted and silently ignored.
_SORT_TOOLTIP_VTYPES = frozenset({
    VT_COLUMN, VT_BAR, VT_LINE, VT_AREA, VT_COMBO,
    VT_RIBBON, VT_WATERFALL, VT_PIE, VT_DONUT, VT_TREEMAP,
})


def _state_projections(state):
    """Every projection dict currently bound in a visual's ``queryState``."""
    return [p
            for role in state.values() if isinstance(role, dict)
            for p in role.get("projections", [])]


def _bind_sort_field(state, expr, qref, nref, visual_type):
    """Make ``expr`` part of the visual's query so Power BI will honour a sort on it.

    Power BI only sorts a visual by a field the visual actually QUERIES. A ``sortDefinition``
    naming an unprojected field is accepted with no error or warning -- and then ignored, while
    still SUPPRESSING Power BI's own default sort, so the category axis silently falls back to
    natural/alphabetical order. That is strictly worse than emitting nothing, which is why the
    original guard declined outright. Both halves of this were render-verified against Power BI
    Desktop (an unprojected sort changed the order but not to the sort measure; the same sort with
    the measure added to Tooltips ordered exactly by it).

    Tableau's ordinary idiom -- "sort this dimension by a measure that is not on the shelf" -- has
    no faithful Power BI shape unless that measure joins the query, so route it to the ``Tooltips``
    well: it participates in the query and the tooltip (which is also what Tableau's own sort-by
    measure surfaces) without changing the chart's visual encoding. This is exactly the field set
    Power BI's own "Sort axis" menu offers.

    Mutates ``state`` copy-on-write (never in place), so a shallow ``dict(state)`` -- as the
    measure-trellis fan-out takes -- cannot leak a projection back into its parent or siblings.
    Returns True when ``expr`` is bound afterwards (already was, or was just added).
    """
    projections = _state_projections(state)
    if any(p.get("field") == expr for p in projections):
        return True
    if visual_type not in _SORT_TOOLTIP_VTYPES:
        return False
    used = {p.get("queryRef") for p in projections}
    unique, i = qref, 1
    while unique in used:
        i += 1
        unique = f"{qref} {i}"
    role = state.get("Tooltips") if isinstance(state.get("Tooltips"), dict) else {}
    state["Tooltips"] = dict(role, projections=list(role.get("projections") or []) + [
        {"field": expr, "queryRef": unique, "nativeQueryRef": nref}])
    return True


def _row_header_sort(ws):
    """A leading DISCRETE measure on the Rows shelf, as an implicit ascending row sort.

    Tableau renders a discrete (blue) measure pill on the Rows shelf as a row HEADER column, and a
    text table's rows are ordered by their headers left to right -- so a numeric pill placed before
    the dimension is how authors pin a custom row order without a ``<computed-sort>``. The workbook
    records no sort element for this at all; the order is a consequence of the shelf layout.

    The rebuild routes that pill into the matrix's Values well (it is a measure), which drops its
    ordering role, and the rows then fall back to alphabetical. Measured against a real dashboard:
    the whole table came out A-Z instead of ranked, which reads as the migration having lost the
    ranking even though every value was correct.

    Restricted to a TABLE / MATRIX. The reading depends on the pill being rendered as a row header,
    which only happens in a text table -- a cartesian chart puts the same pill on an axis, where it
    carries no ordering role. The corpus caught this: without the gate a scatter chart picked up a
    spurious sort on its measure.

    Fail-closed otherwise: only a measure pill that PRECEDES every dimension on the shelf qualifies,
    since that is what makes it the leading header. Ascending, matching Tableau's header order.
    """
    if ws.get("visual_type") not in (VT_MATRIX, VT_TABLE):
        return None
    rows = ws.get("rows") or []
    if len(rows) < 2 or (rows[0] or {}).get("kind") != "value":
        return None
    if not any((p or {}).get("kind") == "category" for p in rows[1:]):
        return None
    return {"field": rows[0], "direction": "Ascending"}


def _sort_definition(ws, state, model_table, field_map):
    """Build a PBIR ``sortDefinition`` from a worksheet's ``<computed-sort>``.

    Power BI puts the sort on ``visual.query.sortDefinition`` (a sibling of ``queryState``) as an
    ordered ``sort`` array of ``{field, direction}`` (direction ``"Ascending"``/``"Descending"``),
    where ``field`` reuses the exact same expression shape as a projection. The sort-by measure
    must be bound in this visual for Power BI to honour it, so when it is not already projected
    :func:`_bind_sort_field` adds it to the Tooltips well. Returns ``None`` when there is no
    computed-sort, or when the sort-by field can neither be projected nor tooltip-bound here --
    emitting an unhonourable sort is worse than emitting none.

    Falls back to :func:`_row_header_sort` -- a leading discrete measure on the Rows shelf, which
    orders a Tableau text table without any sort element being written.

    NOTE: this may MUTATE ``state`` (adding the Tooltips projection), so call it before the
    ``queryState`` is serialised, and after any helper whose behaviour depends on the role set.
    """
    sort = ws.get("sort") or _row_header_sort(ws)
    if not sort:
        return None
    expr, qref, nref = _field_expression(sort["field"], model_table, field_map)
    if not _bind_sort_field(state, expr, qref, nref, ws.get("visual_type")):
        return None
    return {"sort": [{"field": expr, "direction": sort["direction"]}],
            "isDefaultSort": False}


def _axis_objects(axis_titles, axis_hidden=None):
    """Build the data-plane ``visual.objects`` categoryAxis/valueAxis entries for author-overridden
    axis titles AND author-hidden axes. Each axis object is ``[{"properties": {...}}]`` (no
    ``selector`` needed for a global override). A blanked title (``hide``) emits
    ``showAxisTitle:false``; a custom caption emits ``titleText`` (single-quoted semantic-query
    literal) + ``showAxisTitle:true``. An axis the author HID (``Show Header`` off, see
    :func:`_parse_hidden_axes`) emits ``show:false``, which suppresses the whole axis -- labels,
    ticks and title -- and returns the plot area to the marks. ``show`` and the title properties
    are independent, so an axis can be hidden with or without a title override and vice versa.
    Shape verified against multiple real MS PBIR visual.json files + the PBIR enumerations
    reference.
    """
    objects = {}
    hidden = set(axis_hidden or ())
    for axis in ("categoryAxis", "valueAxis"):
        spec = (axis_titles or {}).get(axis)
        props = {}
        if axis in hidden:
            props["show"] = {"expr": {"Literal": {"Value": "false"}}}
            # ``show:false`` does NOT take the title with it. Measured on a 300x300 KPI tile whose
            # source hides every axis: the plot lost its ticks and labels as asked, and Power BI went
            # on drawing a rotated "Sales" caption down the left edge that the source does not have,
            # eating a fifth of the plot width. An axis the author hid has no title either.
            props["showAxisTitle"] = {"expr": {"Literal": {"Value": "false"}}}
        if spec:
            if spec.get("hide"):
                props["showAxisTitle"] = {"expr": {"Literal": {"Value": "false"}}}
            elif spec.get("text"):
                props["titleText"] = {
                    "expr": {"Literal": {"Value": _semantic_string_literal(spec["text"])}}}
                # The author's caption is preserved either way, but a HIDDEN axis shows none of it.
                props["showAxisTitle"] = {"expr": {"Literal": {
                    "Value": "false" if axis in hidden else "true"}}}
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
    # A palette is diverging when the author pinned a CENTER, or when Tableau typed the palette
    # itself ``ordered-diverging``. The second case matters on its own: a highlight table's
    # per-measure palettes are typed diverging but carry no center, and reducing those to a
    # two-stop ramp interpolates end-to-end THROUGH the middle (orange straight to blue, muddying
    # every mid-range cell) instead of passing through the neutral the author chose. The centre
    # VALUE is only emitted when it is known -- with none, Power BI centres the mid stop on the data
    # midpoint, which is exactly what Tableau does for an uncentred diverging ramp -- so the hue
    # path is preserved without inventing a breakpoint.
    diverging = (cg.get("center") is not None
                 or (cg.get("palette_type") or "") == "ordered-diverging")
    if diverging and len(colors) >= 3:
        return {"linearGradient3": {
            "min": _stop(colors[0]),
            "mid": _stop(colors[len(colors) // 2], value=cg.get("center")),
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


# Tableau's Colour shelf paints THE MARK, so which channel a table/matrix cell colour lands on is
# decided by the MARK TYPE -- and the two are not interchangeable:
#
#   * ``Text`` (and ``Automatic``, which draws text on a crosstab) -- the mark IS the number, so
#     Colour is the FONT colour. The cell keeps its normal background. This is the ordinary
#     "text table with conditionally coloured numbers".
#   * ``Square`` -- the mark is a filled rectangle behind the label, so Colour is the CELL
#     BACKGROUND. This is the classic highlight table.
#
# Painting a text table's background reproduces neither: it fills every cell with a colour the
# source never drew, and the numbers -- the thing the author actually colour-coded -- stay black.
_TEXT_MARK_CLASSES = frozenset({"", "automatic", "text", "label"})


def _cell_colour_property(ws):
    """``"fontColor"`` or ``"backColor"`` -- the PBIR cell channel this worksheet's mark colours.

    Fail-closed toward the filled cell: only a mark that is explicitly text-drawing colours the
    font, so an unrecognised mark keeps the long-standing background behaviour.
    """
    return ("fontColor" if (ws.get("mark_class") or "").strip().lower() in _TEXT_MARK_CLASSES
            else "backColor")


def _matrix_discrete_measure_colour(ws, state, model_table, field_map, warnings,
                                    param_values=None):
    """A DISCRETE aggregate measure on Colour over a TABLE / MATRIX -> ``(value_objects, fact)``.

    The table-shaped sibling of :func:`_chart_discrete_measure_fill`, and the answer to the most
    ordinary Tableau text table there is: rows of dimensions, a band of measures, and every number
    coloured by a calc that returns a LABEL -- ``IF SUM([Profit]) < 0 THEN "negative" ELSE
    "positive" END``. Power BI cannot drive a categorical legend from a measure (a legend needs a
    grouping column, which is row-level and would change the aggregate grain and the row count), so
    this binds the model's hex-returning colour twin through conditional formatting as the
    ``Field value`` format style -- editable in Desktop's ``fx`` dialog rather than unreachable JSON.

    Emitted per VALUE COLUMN. Tableau colours every measure cell in the row from the one mark
    colour, so each projected value gets its own ``selector.metadata`` entry naming the column it
    paints; a single unscoped entry colours only the first column.

    The channel is chosen by MARK TYPE -- font for a text table, background for a Square highlight
    table (see :data:`_TEXT_MARK_CLASSES`) -- because Tableau's Colour shelf paints the mark, and on
    a text table the mark is the number itself.
    """
    color = ws["encodings"].get("color")
    if not color or not color.get("discrete_measure"):
        return None, None
    if ws.get("visual_type") not in (VT_MATRIX, VT_TABLE):
        return None, None
    if color.get("kind") != "value" or color.get("binding") not in ("aggregation", "measure"):
        return None, None
    if _is_view_level_calc(color):
        # RUNG 4: a view-scoped driver ("colour the lowest cell") has no rung-1 form, because the
        # comparison is against the OTHER marks in the view. It survives only as a Visual
        # Calculation, declared as a hidden projection and referenced by ``SelectRef`` -- the
        # inline form was refuted by render (validates clean, paints nothing).
        values = (state.get("Values") or {}).get("projections", [])
        dax = _discrete_colour_visual_calc(
            ws, color, values, model_table, field_map, param_values) if values else None
        qref = _declare_colour_projection(state, dax)
        if qref:
            prop = _cell_colour_property(ws)
            return ([{"properties": {prop: {"solid": {"color": {
                          "expr": {"SelectRef": {"ExpressionName": qref}}}}}},
                      "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                                   "metadata": p["queryRef"]}} for p in values],
                    {"kind": "cell_discrete_measure_colour", "channel": prop,
                     "mark": ws.get("mark_class"), "style": "visual_calculation",
                     "source_measure": color.get("caption"),
                     "query_ref": qref, "status": "emitted"})
        return None, _discrete_view_scoped_defer(
            ws, color, "cell_discrete_measure_colour", warnings)
    values = (state.get("Values") or {}).get("projections", [])
    if not values:
        return None, None
    prop = _cell_colour_property(ws)
    # RUNG 1 first: a native Rules conditional format, which adds nothing to the model at all.
    rule = _discrete_colour_rule(ws, color, model_table, field_map, param_values)
    if rule is not None:
        return ([{"properties": {prop: {"solid": {"color": {"expr": rule}}}},
                  "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                               "metadata": p["queryRef"]}} for p in values],
                {"kind": "cell_discrete_measure_colour", "channel": prop,
                 "mark": ws.get("mark_class"), "style": "rules",
                 "source_measure": color.get("caption"),
                 "cases": len(rule["Conditional"]["Cases"]),
                 "targets": [p["queryRef"] for p in values], "status": "emitted"})
    if _discrete_colour_twin_unavailable(color):
        return None, _discrete_unbound_defer(ws, color, "cell_discrete_measure_colour", warnings)
    if not values:
        return None, None
    measure_name = _discrete_colour_measure_name(color)
    prop = _cell_colour_property(ws)
    expr = {"solid": {"color": {"expr": {
        "Measure": {"Expression": {"SourceRef": {"Entity": MEASURES_TABLE}},
                    "Property": measure_name}}}}}
    value_objects = [{
        "properties": {prop: expr},
        # matchingOption 1 + metadata = "this column, every data point in it". Without the
        # wildcard Power BI evaluates the expression in ONE context and paints every cell the same
        # colour -- with a clean validation pass (see _chart_discrete_measure_fill).
        "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                     "metadata": p["queryRef"]},
    } for p in values]
    fact = {"kind": "cell_discrete_measure_colour",
            "channel": prop,
            "mark": ws.get("mark_class"),
            "colour_measure": measure_name,
            "source_measure": color.get("caption"),
            "targets": [p["queryRef"] for p in values],
            "status": "emitted"}
    warnings.append(_warn(
        "worksheet", ws["name"],
        "discrete colour: Tableau colours these cells by a DISCRETE aggregate measure (%r). Power "
        "BI has no native categorical legend for a measure-driven colour -- a legend needs a "
        "grouping COLUMN, which is row-level and would change the aggregate grain -- so this is "
        "rebuilt as conditional formatting on %s (the %s mark colours the %s) driven by a colour "
        "measure %r (Format style 'Field value', editable in Desktop's fx dialog), applied to all "
        "%d value column(s). There is no colour legend"
        % (color.get("caption"), prop, ws.get("mark_class") or "Automatic",
           "text" if prop == "fontColor" else "cell background", measure_name, len(values))))
    return value_objects, fact


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
    scales = ws.get("mv_color_scales") or []
    if not cg and not scales:
        return None, None
    if ws["visual_type"] not in (VT_MATRIX, VT_TABLE):
        # A cartesian chart's continuous mark colour is owned by ``_chart_continuous_fill``
        # (a ``dataPoint.fill`` FillRule), not a table/matrix cell ``backColor``. Now that the
        # gradient is also parsed for charts, skip silently here rather than feign a
        # conditional-format deferral for a visual that never carries a cell fill.
        return None, None

    # HIGHLIGHT TABLE: one INDEPENDENT conditional-fill scale per measure column. Tableau builds
    # this by putting Measure Values on Colour with ``separate-domains``, giving each member measure
    # its own palette and its own domain -- so the faithful rebuild is N scoped ``values[]`` entries,
    # each ``selector.metadata``-bound to the one column it colours, NOT a single scale stretched
    # across the table. The whole worksheet was previously dropped for this shape, taking the
    # conditional formatting with it; both are the same defect.
    if scales:
        values = (state.get("Values") or {}).get("projections", [])
        # Match on the STRIPPED name: the projection's ``nativeQueryRef`` is normalised to the
        # model's object name, while a Tableau member caption keeps whatever whitespace the author
        # left on it, so a raw-caption lookup silently misses and the column loses its fill.
        by_ref = {str(p.get("nativeQueryRef") or "").strip(): p for p in values}
        value_objects, bound, missed = [], [], []
        for entry in scales:
            proj = by_ref.get(str(entry.get("caption") or "").strip())
            if proj is None:
                missed.append(entry.get("caption"))
                continue
            value_objects.append({
                "properties": {
                    "backColor": {"solid": {"color": {"expr": {"FillRule": {
                        "Input": _fill_rule_input(proj),
                        "FillRule": _gradient_color_stops(entry["gradient"])}}}}}},
                "selector": {
                    "data": [{"dataViewWildcard": {"matchingOption": 1}}],
                    "metadata": proj["queryRef"]},
            })
            bound.append(proj["queryRef"])
        fact = {
            "kind": "background_color_scale_per_measure",
            "status": "emitted" if value_objects else "deferred",
            "scales": len(scales),
            "bound_measures": bound,
        }
        if missed:
            # Never silent: a member whose column is not projected (e.g. its calc did not translate)
            # loses its fill, and the reader is told which one.
            fact["unbound"] = sorted(missed)
            warnings.append(_warn(
                "worksheet", ws["name"],
                "conditional fill not applied to %d measure column(s) that are not projected in "
                "this visual (%s)" % (len(missed), ", ".join(sorted(m or "?" for m in missed)))))
        return (value_objects or None), fact

    color = ws["encodings"].get("color")
    fact = {
        "kind": "background_color_scale",
        "palette_type": cg["palette_type"],
        "center": cg["center"],
        "colors": cg["colors"],
    }

    values = (state.get("Values") or {}).get("projections", [])

    def _match(field):
        if not field:
            return None
        expr, _, _ = _field_expression(field, model_table, field_map)
        for p in values:
            if p["field"] == expr:
                return p
        return None

    driver_proj = _match(color)
    # COLOUR BY A DIFFERENT FIELD. The driver measure is not a visible column here, and a matrix has
    # no Tooltips well to park it in -- emitting one made the visual invalid and it did not render
    # (adjudicated on a ground-truth run as a det-rule defect). Power BI does not require the driver
    # to be projected at all: the FillRule's ``Input`` is resolved against the MODEL, and only the
    # ``selector.metadata`` (which column receives the fill) must name a projected queryRef.
    # Confirmed on the adjudicated ground-truth `.pbip` for this very workbook: its matrix carries
    # roles ['Values'] only, projects Category/Profit/Sales, and its fills reference ``Total Profit``
    # / ``Total Sales`` -- measures that appear in NO projection.
    driver_expr = None
    driver_ref = None
    if driver_proj is not None:
        driver_expr = driver_proj["field"]
        driver_ref = driver_proj.get("queryRef")
    elif color and color.get("kind") == "value" and values:
        driver_expr, driver_ref, _nr = _field_expression(color, model_table, field_map)
    # A quick table calc normally defers (the model carries no equivalent measure). But when the
    # colour pill was REBOUND to a real model measure via the model<->viz contract
    # (``measure_rebound``), it IS a bindable measure now -- so the table-calc gate is lifted and the
    # gradient lights up against the contracted measure.
    is_table_calc_defer = cg["is_table_calc"] and not (color or {}).get("measure_rebound")
    if (color is None or color["kind"] != "value"
            or color["binding"] not in ("aggregation", "measure")
            or is_table_calc_defer or driver_expr is None):
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
    target_proj = _match(ws["encodings"].get("label")) or driver_proj or (values[0] if values else None)
    if target_proj is None:
        reason = "no projected column to receive the fill"
        warnings.append(_warn(
            "worksheet", ws["name"],
            "background colour scale deferred ({0}); the visual is emitted without "
            "conditional formatting".format(reason)))
        fact["status"] = "deferred"
        fact["reason"] = reason
        return None, fact
    value_objects = [{
        "properties": {
            "backColor": {"solid": {"color": {"expr": {"FillRule": {
                "Input": driver_expr,
                "FillRule": _gradient_color_stops(cg)}}}}}},
        "selector": {
            "data": [{"dataViewWildcard": {"matchingOption": 1}}],
            "metadata": target_proj["queryRef"]},
    }]
    fact["status"] = "emitted"
    fact["bound_measure"] = driver_ref or "(model measure)"
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


def _axis_orders_by(state, base_field, model_table, field_map):
    """Is a model-side table-calc measure still trustworthy on THIS visual's axis?

    A model-side measure hard-codes its ordering at BUILD time (``ORDERBY('Orders'[Order_Date])``).
    That reproduces Tableau only while the drawn axis still contains that column. The report may
    rebind a date axis to the model's Date hierarchy, and then the window orders by a column that is
    not on the axis and NOTHING accumulates -- the chart shows raw values under a name that promises
    a running total.

    The discriminator is the base pill's own instance token. A VIEW-ONLY quick table calc (``cum:``
    running total, ``win:`` moving window, ``pcto:`` percent of total, ``rsum:``/``movavg:``) is a
    property of the sheet, not of the data, so a fixed-order model measure cannot express it once the
    axis is rebound. Anything else keeps the existing precedence.

    Conservative by construction: returns True (keep the model measure) unless the pill positively
    declares one of those view-only tokens, so no visual that works today changes.
    """
    instance = str(base_field.get("instance") or "").strip().lower()
    if not instance:
        return True
    token = instance.split(":", 1)[0]
    return token not in _VIEW_ONLY_QUICK_TOKENS


# Tableau's view-only quick-table-calc instance prefixes: the transform is a property of the VIEW, so
# it must be rebuilt against the visual's own axis rather than frozen into a model measure.
_VIEW_ONLY_QUICK_TOKENS = frozenset({
    "cum", "rsum", "movavg", "win", "pcto", "pcdf", "pdiff", "diff", "rdiff", "pcrk",
})


def _untransformed_base(ws, base_field):
    """The base pill with its model-measure rebind stripped, so a Visual Calculation runs over RAW
    values instead of an already-transformed measure.

    Returned as a COPY -- the worksheet IR is shared with other emit paths, and mutating it here
    would silently change what they see. ``None`` when the pill carries no untransformed identity to
    fall back to, which keeps the caller on the safe (yield-to-the-model) path.
    """
    caption = base_field.get("caption")
    if not caption:
        return None
    out = dict(base_field)
    out.pop("measure_rebound", None)
    out.pop("rebound_to_instance", None)
    # The pill's own pre-rebind identity: its Tableau caption resolved against the fact table, with
    # its original aggregation restored. This is exactly what the FIRST (unbound) viz pass produces,
    # which is the pass whose Visual Calculations were verified to render correctly.
    out["property"] = caption
    out["binding"] = "aggregation" if out.get("aggregation") else "column"
    return out


def _reclaim_transform(ws, base_field, values, model_table, field_map):
    """Re-point the base projection at the RAW measure so the report can own the transform.

    Used only when a model-side table-calc measure cannot survive this visual's axis (see
    :func:`_axis_orders_by`). The projection currently names the model's pre-transformed measure
    (``_Measures.Sales (running total (cumulative))``); a Visual Calculation appended on top of that
    would accumulate an already-accumulated series. Swapping it back to ``Sum(Orders[Sales])`` -- the
    exact shape the FIRST, unbound viz pass emits, and the pass whose Visual Calculations were
    verified to render correctly -- makes the calc run over raw values.

    Mutates the projection dict IN PLACE (it is the one already sitting in ``state``) and returns the
    untransformed base field, or ``None`` if anything cannot be resolved -- in which case the caller
    keeps the safe yield-to-the-model behaviour rather than shipping a dangling reference. Every step
    is verified before the swap: a half-applied rewrite is what blanks a visual.
    """
    raw = _untransformed_base(ws, base_field)
    if raw is None:
        return None
    _, rebound_qref, _ = _field_expression(base_field, model_table, field_map)
    proj = next((p for p in values if p.get("queryRef") == rebound_qref), None)
    if proj is None:
        return None
    expr, qref, nref = _field_expression(raw, model_table, field_map)
    if not expr or not qref:
        return None
    proj["field"], proj["queryRef"] = expr, qref
    if nref:
        proj["nativeQueryRef"] = nref
    else:
        proj.pop("nativeQueryRef", None)
    return raw


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

    Precedence: the model-level table-calc measure path is the first-class owner of a **value**-role
    calc. If the base value pill was rebound to a real model measure (``measure_rebound``), a
    ``role == "value"`` calc yields so the two paths never double-emit the same shown transform. A
    ``role == "color"`` calc does **not** yield when a SEPARATE base value pill (label / text) drives the
    shown number and the colour is a distinct quick-calc DRIVER (e.g. a ``pcdf`` percent-difference over
    that base): it survives the rebind and keeps colouring the cells from the (hidden) calc rather than
    the raw rebound measure. It yields only when the colour pill IS the base (no label/text value), where
    the rebound measure may already embody the transform and re-applying it would double-count.

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

    # A TABLE CALC BELONGS TO ITS OWN PILL. ``usages`` is every table-calc instance the worksheet
    # DECLARES, which is not the same as every one it PLOTS -- Tableau keeps a pill parked on Detail
    # (an ``<lod>`` encoding) in the very same dependency list as the pills on Rows and Cols. Taking
    # ``usages[0]`` unconditionally let a parked calc hijack the axis: a sparkline whose Rows shelf
    # holds the RAW ``sum:Sales`` (and whose ``cum:sum:Sales`` sits on Detail, drawing nothing) was
    # rebuilt with ``RUNNINGSUM`` over its Y measure and rendered as a smooth cumulative ramp where
    # the source draws a jagged monthly series -- the wrong shape AND the wrong numbers.
    #
    # A quick table calc DOES carry its token onto the pill it transforms: "Running total with
    # stacked bar chart" plots ``[cum:sum:Sales:qk]``, "running total with end point dot" plots
    # ``([cum:sum:Sales:qk] + ...)``, "Bar with Moving Average" plots ``[win:sum:Sales:qk]``, and
    # ``_resolve_field`` keeps that token as the field's ``instance``.
    #
    # So the calc that transforms the shown value is the one whose instance IS the shown pill. When
    # none matches, a base pill that is itself a table calc keeps the first usage (the instances can
    # legitimately differ across encodings); a base pill that is a PLAIN measure has no calc of its
    # own and is left exactly as the author plotted it. A base pill that records NO instance says
    # nothing either way, so it keeps the long-standing behaviour rather than lose a real calc.
    _base_inst = (base_field or {}).get("instance") if isinstance(base_field, dict) else None
    usage = next((u for u in usages if getattr(u, "instance", None) == _base_inst), None)
    if usage is None:
        if _base_inst and not _instance_is_table_calc(_base_inst):
            return None, None
        usage = usages[0]

    # Yield to the model measure path when the base pill was rebound to a real model measure (precedence).
    if not base_field or base_field.get("kind") != "value":
        return None, None
    # A VALUE-role calc yields: the rebound model measure IS the shown transform and re-applying the
    # quick calc would double-transform. A COLOUR-role calc is a distinct colour DRIVER (a view-only
    # quick table calc such as a ``pcdf`` percent-difference) that runs over a SEPARATE base value pill
    # (label / text), so it must survive the rebind and keep colouring the cells -- yielding here dropped
    # the Visual Calculation in the final rebind pass, leaving the heat scale bound to the RAW rebound
    # measure instead of the percent-difference Tableau colours by. It yields ONLY when the colour pill
    # IS the base (no separate label/text value): then the rebound measure may already embody the
    # transform, so re-applying the quick calc would double it -- defer to the model-measure fill.
    #
    # ``measure_rebound`` alone does NOT mean the model owns the transform, and treating it that way
    # silently deleted EVERY view-only quick table calc from the shipped report: the estate runs the viz
    # stage twice (once bare to build the model, once rebound to it) and only the second pass ships, so a
    # calc emitted in pass 1 vanished in pass 2. ``rebound_to_instance`` is the exact discriminator --
    # true only when the model translated THIS PILL'S OWN table-calc instance into a measure (transform
    # embodied -> yielding is right), false when it merely translated the base field the calc runs over
    # (the running total exists nowhere but here). Absent -> treated as instance-bound, so a field dict
    # that predates the flag keeps the old, never-double-transform behaviour.
    #
    # THE AXIS EXCEPTION. A model measure embodies the transform only while the visual's axis still
    # contains the column its window orders by. When the report rebinds the date axis to the model's
    # Date hierarchy -- ``Date[Calendar Year]`` + ``Date[Calendar Month]`` in place of the fact's own
    # ``Order_Date`` -- an ``ORDERBY('Orders'[Order_Date])`` window no longer follows what is drawn, so
    # the chart renders with NO accumulation at all. Measured 2026-08-07: a running total and a moving
    # average both rendered flat/jagged for exactly this reason, while the Visual Calculation (which
    # follows the visual's own axis by construction) rendered the correct cumulative curve.
    #
    # So the model measure only wins when its ordering survives on this axis. When it does not, the
    # report layer takes the transform back -- and the base projection is re-pointed at the
    # UNTRANSFORMED base measure first, so the calc runs over raw values instead of double-applying
    # over an already-accumulated one.
    if base_field.get("measure_rebound") and base_field.get("rebound_to_instance", True):
        _color_is_base = base_field is ws["encodings"].get("color")
        if role == "value" or _color_is_base:
            if _axis_orders_by(state, base_field, model_table, field_map):
                return None, None
            # The model measure's frozen ORDERBY does not survive this axis. Take the transform back
            # into the report, re-pointing the base at the RAW measure first so the Visual Calculation
            # runs over unaccumulated values instead of double-applying.
            base_field = _reclaim_transform(ws, base_field, values, model_table, field_map)
            if base_field is None:
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


# ``[<field id>]`` bracket token (no nested brackets). Used to spot a nested calc chain: a formula
# whose bracketed reference is ANOTHER in-scope calc field's id.
_CALC_REF_TOKEN = re.compile(r"\[([^\[\]]+)\]")
# Tableau formula aggregate keyword -> the field ``aggregation`` name the model resolver uses.
_FORMULA_AGG_TOKEN = {
    "SUM": "Sum", "AVG": "Avg", "AVERAGE": "Avg", "MIN": "Min", "MAX": "Max",
    "COUNT": "Count", "COUNTD": "CntD", "MEDIAN": "Median",
}


def _view_only_field_chain_index(table_calc_usages):
    """Group formula-authored table-calc usages by worksheet -- a ``kind == "field"`` calc whose
    formula calls a Tableau table-calculation function.

    Two shapes qualify and both take the Visual-Calculation path:

      * a **nested** chain -- the calc references ANOTHER in-scope calc field (``RANK([composit])``
        over ``composit = RUNNING_SUM(...)``), which rebuilds as nested Visual Calculations;
      * a **single-level** formula table calc (``WINDOW_AVG(SUM([Sales]))``), which rebuilds as one.

    Single-level usages were previously excluded and left to the model-measure path, where they
    emitted an inert ``BLANK()`` stub -- structurally valid, and the chart rendered EMPTY. They are
    admitted here because a Visual Calculation runs over the visual's result matrix in DISPLAY
    order, so the axis reproduces that worksheet's addressing structurally and stays correct when
    the user re-sorts.

    This is NOT because the addressing is unknowable. Measured corpus-wide, ``ordering_type`` is
    populated on 46/46 formula usages and the rows/cols shelf layout on 46/46;
    ``translation_router`` scopes ``missing_addressing_intent`` to what the bare ``.tds`` cannot
    carry, and the Tier-1 guidance prescribes recovering it from the ``.twb`` and emitting windowed
    MODEL DAX. That route is the complement of this one and is required wherever the calc is not a
    shown projection -- a reference-line bound, or a model measure that references the calc (see
    :func:`formula_table_calc_to_visual_calc.compile_chain`).

    Admission here is only a candidacy test: the compiler still fails closed on anything outside its
    faithful subset, and the emitter still declines unless the calc is the visual's shown value.

    Returns ``{worksheet_name: [usage, ...]}`` -- an empty dict for ``None`` / nothing, so every
    existing caller stays byte-identical.
    """
    index = {}
    for usage in (table_calc_usages or []):
        if getattr(usage, "kind", None) != "field":
            continue
        formula = getattr(usage, "formula", None)
        scope = getattr(usage, "scope_formulas", None) or {}
        col = getattr(usage, "column", None)
        if not formula or not scope:
            continue
        refs = set(_CALC_REF_TOKEN.findall(formula))
        nested = any(rid in scope and rid != col for rid in refs)
        if nested or (formula_is_table_calc is not None and formula_is_table_calc(formula)):
            index.setdefault(getattr(usage, "worksheet", None), []).append(usage)
    index.pop(None, None)
    return index


def _resolved_value_fields(ws):
    """``{name_lower: field}`` for every already-resolved **aggregation** value pill on the visual
    (rows / cols / encodings), keyed by both caption and property. The nested-chain compiler resolves
    a formula's ``SUM([col])`` base against these -- reusing a field the visual already resolves keeps
    a synthesised base measure non-dangling."""
    out = {}

    def _add(f):
        if isinstance(f, dict) and f.get("kind") == "value" and f.get("binding") == "aggregation":
            for key in (f.get("caption"), f.get("property")):
                if key:
                    out.setdefault(str(key).strip().lower(), f)

    for f in list(ws.get("rows") or []) + list(ws.get("cols") or []):
        _add(f)
    for v in (ws.get("encodings") or {}).values():
        if isinstance(v, list):
            for f in v:
                _add(f)
        else:
            _add(v)
    return out


def _axis_value_field_names(ws):
    """``{name_lower}`` for the aggregation value pills carried on the ROWS/COLS shelves.

    These are the visual's PLOTTED measures. A base measure the chain reuses is hidden only when it
    is *not* one of them: an encoding-only pill (detail / tooltip) exists purely to feed the calc, so
    hiding it is right, whereas a plotted measure is a displayed series in its own right -- a control
    chart's ``SUM([Sales])`` line under its Upper/Lower bands. Hiding that would trade two blank
    bands for a missing line, which is the same "structurally valid, semantically absent" failure
    this path exists to remove.
    """
    out = set()
    for f in list(ws.get("rows") or []) + list(ws.get("cols") or []):
        if isinstance(f, dict) and f.get("kind") == "value" and f.get("binding") == "aggregation":
            for key in (f.get("caption"), f.get("property")):
                if key:
                    out.add(str(key).strip().lower())
    return out


def _apply_formula_table_calc_chain(ws, state, chain_index, model_table, field_map, warnings,
                                    param_values=None):
    """Rebuild a **nested** formula table-calc chain (a calc field that references another calc field)
    as nested Power BI Visual Calculations. Returns ``(handled, vc_fact_or_None)``.

    Additive + fail-closed. Fires ONLY for a worksheet in ``chain_index``. Every base aggregate and
    reference must resolve from THIS visual's already-resolved worksheet fields; a blend / secondary
    source, a non-calc bare reference, or an out-of-subset function (``WINDOW_*``, LOD, ...) routes the
    visual to review and returns ``(False, fact)`` so the base visual is emitted unchanged. On success
    it removes the displayed calc's plain measure projection and lands the hidden base measures + the
    hidden inner Visual Calculation(s) + the shown outer Visual Calculation, returning ``(True, fact)``.
    """
    if not chain_index or compile_formula_chain is None or rename_calc_references is None:
        return False, None
    usages = chain_index.get(ws["name"])
    if not usages:
        return False, None

    is_chart = ws.get("visual_type") in _VC_CHART_TYPES
    value_key = "Y" if is_chart else "Values"
    values = (state.get(value_key) or {}).get("projections", [])
    if not values:
        return False, None

    # A worksheet can display SEVERAL formula table calcs at once -- a control chart shows an Upper
    # AND a Lower band -- so every admitted usage is attempted, not just the first. Each attempt is
    # independent and fail-closed: one that leaves the faithful subset routes to review and leaves
    # the others (and the base visual) untouched.
    emitted_facts, review_fact = [], None
    for usage in usages:
        handled, fact = _apply_one_formula_table_calc(
            ws, state, usage, value_key, model_table, field_map, warnings, param_values)
        if handled:
            emitted_facts.append(fact)
        elif fact is not None and review_fact is None:
            review_fact = fact
    if not emitted_facts:
        return False, review_fact
    head = dict(emitted_facts[0])
    if len(emitted_facts) > 1:
        # Additive disclosure: every entry this visual rebuilt, and the union of their calcs.
        head["entries"] = [f["entry"] for f in emitted_facts]
        head["base_measures"] = sorted({m for f in emitted_facts for m in f["base_measures"]})
        head["visual_calcs"] = [vc for f in emitted_facts for vc in f["visual_calcs"]]
    return True, head


def _apply_one_formula_table_calc(ws, state, usage, value_key, model_table, field_map, warnings,
                                  param_values=None):
    """Rebuild ONE formula table-calc usage on ``ws`` as Visual Calculation(s).

    Returns ``(handled, fact)`` exactly as :func:`_apply_formula_table_calc_chain` does for a single
    usage; the caller folds several of these together.
    """
    values = (state.get(value_key) or {}).get("projections", [])
    if not values:
        return False, None

    entry_caption = usage.caption
    id2cap = dict(usage.scope_captions or {})
    calc_formulas, summaries = {}, {}
    for cid, f in (usage.scope_formulas or {}).items():
        cap = id2cap.get(cid, cid)
        calc_formulas[cap] = rename_calc_references(f, id2cap)
        summaries[cap] = f
    if entry_caption not in calc_formulas:
        # The displayed calc's own formula must be in scope for the chain to have an entry point.
        return False, None

    resolved_values = _resolved_value_fields(ws)
    axis_value_names = _axis_value_field_names(ws)
    used_refs = {p.get("queryRef") for p in values if p.get("queryRef")}
    base_projected = {}   # nativeQueryRef -> hidden projection to append
    base_nrefs = set()    # every base measure the chain resolved (reused or synthesized)
    # The displayed calc's own plain-measure projection (the Visual Calculation replaces it). Looked
    # up BEFORE the compile so a dependency can never bind to it as if it were a base measure.
    entry_proj = next((p for p in values if p.get("nativeQueryRef") == entry_caption), None)
    # Measures this visual ALREADY projects, keyed by their native reference. A non-table-calc
    # dependency binds here instead of recursing as a nested Visual Calculation.
    projected_by_nref = {}
    for p in values:
        nref = p.get("nativeQueryRef")
        if nref and p is not entry_proj:
            projected_by_nref.setdefault(nref, p)

    def resolve_measure(name, ds):
        """Bind a dependency to a model measure this visual can reference.

        Two shapes resolve:

        * an UNQUALIFIED non-table-calc calc field -> the measure this visual already projects.
          Visibility is left ALONE: unlike ``resolve_aggregate`` (which synthesises hidden base
          measures purely to feed the calc), such a projection is usually the charted value itself
          -- a reference band's ``WINDOW_MAX([Count of Engagements]) * 1.2`` must not hide
          ``Count of Engagements``.
        * ``[Parameters].[X]`` -> the what-if parameter's ``SELECTEDVALUE`` measure, added to the
          visual as a HIDDEN projection. A Visual Calculation reads the visual's own result matrix,
          so the scalar has to BE on the visual; binding to the measure (not the picker column)
          keeps the parameter live, exactly as the colour-rule resolver does. A parameter the model
          never turned into a what-if table resolves to nothing and the chain fails closed.

        Any other qualified reference (a blend / secondary source) fails closed.
        """
        if ds:
            if _norm_param_key(ds) != "parameters":
                return None
            hit = (param_values or {}).get(_norm_param_key(name))
            if not hit:
                return None
            nref = hit["measure"]
            if nref not in projected_by_nref and nref not in base_projected:
                qref = "{0}.{1}".format(hit["table"], hit["measure"])
                while qref in used_refs:
                    qref += "_p"
                used_refs.add(qref)
                base_projected[nref] = {
                    "field": {"Measure": {
                        "Expression": {"SourceRef": {"Entity": hit["table"]}},
                        "Property": hit["measure"]}},
                    "queryRef": qref, "nativeQueryRef": nref, "hidden": True}
            base_nrefs.add(nref)
            return nref
        proj = projected_by_nref.get((name or "").strip())
        if proj is None:
            return None
        nref = proj.get("nativeQueryRef")
        base_nrefs.add(nref)
        return nref

    def resolve_aggregate(agg, fieldname, ds):
        if ds:                                   # blend / secondary source -> Feature B, fail closed
            return None
        tok = _FORMULA_AGG_TOKEN.get((agg or "").upper())
        if not tok:
            return None
        src = resolved_values.get((fieldname or "").strip().lower())
        if not src:
            return None
        field = dict(src)
        field["aggregation"] = tok              # same resolved column, requested aggregation
        _, _qref, nref = _field_expression(field, model_table, field_map)
        existing = next((p for p in values if p.get("nativeQueryRef") == nref), None)
        if existing is not None:
            if (fieldname or "").strip().lower() not in axis_value_names:
                existing["hidden"] = True
        elif nref not in base_projected:
            proj = _projection(field, model_table, field_map, used_refs)
            proj["hidden"] = True
            base_projected[nref] = proj
        base_nrefs.add(nref)
        return nref

    def _review(reason):
        warnings.append(_warn(
            "worksheet", ws["name"],
            "nested formula table calc routed to review ({0}); the visual is emitted with the "
            "base value only".format(reason)))
        return False, {"kind": "visual_calculation", "worksheet": ws["name"], "role": "value",
                       "status": "review", "reason": reason, "family": "FORMULA_TABLE_CALC",
                       "entry": entry_caption}

    defs, reason = compile_formula_chain(
        entry_caption, calc_formulas, axis="ROWS",
        resolve_aggregate=resolve_aggregate, resolve_measure=resolve_measure, summaries=summaries)
    if not defs:
        return _review(reason or "chain did not compile")

    # The displayed calc must already be projected as a plain measure (the VC replaces it). If it is
    # not the shown value pill, fail closed rather than ADD a stray column.
    base_proj = entry_proj
    if base_proj is None:
        return _review("displayed calc is not the shown value")

    # SPLICE THE VC IN AT THE PILL'S OWN POSITION -- a matrix renders Values in projection order, so
    # appending rebuilt calcs at the end silently re-columns the table. Measured on the ``Network
    # Operational`` workbook, whose author interleaves each rank with the measure it ranks:
    #
    #   authored / previous build   Weighted Rank | Weighted Rank Score | Rank % ANS FTR | ANS FTR | ...
    #   appended                    Weighted Rank Score | ANS FTR | % GMC | % MH | <4 ranks at the end>
    #
    # Four pills moved to the far right and every rank lost its adjacency to the value it explains.
    # Nothing flags it: the projections are all present and correct, the visual validates, and only
    # the reading order is destroyed. The base measure is removed from its slot and the calc(s) that
    # replace it take that slot, so a per-usage rebuild is position-preserving by construction.
    base_idx = next((i for i, p in enumerate(values) if p is base_proj), len(values))
    head = [p for p in values[:base_idx] if p is not base_proj]
    tail = [p for p in values[base_idx + 1:] if p is not base_proj]

    # Operand measures the visual does not already project are genuine ADDITIONS with no authored
    # slot of their own. They are projected BEFORE the calc(s) that reference them -- a Visual
    # Calculation reads its operands from the visual's own matrix, so an operand must already be in
    # scope -- and they carry ``hidden``, so their position never disturbs the rendered column order.
    extras = []
    for nref, proj in base_projected.items():
        if not any(p.get("nativeQueryRef") == nref for p in head + tail):
            extras.append(proj)
    new_values = head + extras + tail

    vc_projections = []
    # queryRefs must be unique ACROSS every usage this visual rebuilds, so allocation skips any ref
    # already taken by a surviving projection (a second usage would otherwise reuse "select").
    taken = {p.get("queryRef") for p in new_values if p.get("queryRef")}
    pool = [q for q in _VC_QUERY_REFS if q not in taken]
    for i, vc in enumerate(defs):
        qref = pool[i] if i < len(pool) else "select{0}".format(len(taken) + i)
        while qref in taken:
            qref = qref + "x"
        taken.add(qref)
        proj = {"field": {"NativeVisualCalculation": {
                    "Language": "dax", "Expression": vc.expression, "Name": vc.name}},
                "queryRef": qref, "nativeQueryRef": vc.name}
        if vc.hidden:
            proj["hidden"] = True
        vc_projections.append((proj, vc))
    state[value_key]["projections"] = (
        head + extras + [p for p, _ in vc_projections] + tail)

    vc_fact = {
        "kind": "visual_calculation",
        "worksheet": ws["name"],
        "role": "value",
        "status": "emitted",
        "family": "FORMULA_TABLE_CALC",
        "axis": "ROWS",
        "entry": entry_caption,
        "base_measures": sorted(base_nrefs),
        "visual_calcs": [
            {"name": vc.name, "expression": vc.expression, "hidden": vc.hidden,
             "is_inner": vc.is_inner, "queryRef": p["queryRef"]}
            for p, vc in vc_projections],
    }
    return True, vc_fact


# A per-member dataPoint fill (a ``scopeId`` data selector) carries a colour dimension's own member
# colours. It applies to LINE and AREA as well as the discrete categorical charts: the earlier
# concern that an explicit dataPoint override "can drop the line" is disproven by the adjudicated
# rebuild of this corpus, whose orange line and cyan area BOTH carry a dataPoint fill -- the line
# additionally needs ``lineStyles.strokeColor``, which is what was actually missing. Excluding them
# meant a three-series green line/area rebuilt in a single flat colour (or Power BI's default blue),
# which is the most visible per-visual colour defect there is. Tables / matrices are still excluded:
# they carry the backColor heat scale instead.
_DATAPOINT_COLOR_TYPES = (VT_COLUMN, VT_BAR, VT_PIE, VT_DONUT, VT_LINE, VT_AREA, VT_COMBO)


def _mark_transparency_pct(table):
    """A worksheet's mark opacity as a Power BI ``transparency`` percent (0 = opaque), or ``None``.

    ``<format attr='mark-transparency' value='N'/>`` is an ALPHA BYTE (0..255), not the 0..100
    percentage Tableau's UI shows -- confirmed against two renders: stems written ``70`` draw as a
    very pale green (70/255 = 27% opacity) and an area fill written ``91`` draws as pale pink
    (36%), while ``255`` is fully opaque. Reading it as a percentage would have made a 27%-opacity
    mark 70% opaque; ignoring it entirely (what happened) painted every translucent Tableau mark at
    full strength, which is why pale fills came back as flat blocks of colour.

    An axis pane outranks the leading all-panes pane for the same reason the colour does (see
    :func:`_constant_mark_color`): once per-axis panes exist, they are what draws. A fully opaque
    mark returns ``None`` so nothing is emitted.
    """
    if table is None:
        return None
    def _values(scope):
        out = []
        for el in scope.iter():
            if _local(el.tag) != "format" or (el.get("attr") or "") != "mark-transparency":
                continue
            try:
                n = float(el.get("value"))
            except (TypeError, ValueError):
                continue
            if 0 <= n <= 255:
                out.append(n)
        return out

    panes_el = _first(table, "panes")
    axis_vals = []
    for pane in (_children_local(panes_el, "pane") if panes_el is not None else []):
        if any(_attr_local(pane, a) not in (None, "")
               for a in ("x-axis-name", "y-axis-name", "x-index", "y-index")):
            axis_vals.extend(_values(pane))
    vals = axis_vals or _values(table)
    if not vals:
        return None
    alpha = max(vals)
    pct = int(round((1.0 - alpha / 255.0) * 100))
    return pct if pct > 0 else None


_DATAPOINT_TRANSPARENCY_PROP = {
    # Power BI does NOT agree with itself on what "transparency" is called, and using the wrong name
    # is a HARD validation error (``PBIR_FORMATTING_PROP_UNKNOWN``) that fails the entire report
    # rather than losing one property. Taken verbatim from the visual catalog
    # (``powerbi-report-author formatting describe-object <type> dataPoint``), not inferred:
    # a bar/column/pie fill uses ``fillTransparency``; an area/line surface uses ``transparency``.
    "clusteredColumnChart": "fillTransparency",
    "clusteredBarChart": "fillTransparency",
    "barChart": "fillTransparency",
    "columnChart": "fillTransparency",
    "hundredPercentStackedBarChart": "fillTransparency",
    "hundredPercentStackedColumnChart": "fillTransparency",
    "pieChart": "fillTransparency",
    "donutChart": "fillTransparency",
    "ribbonChart": "fillTransparency",
    "areaChart": "transparency",
    "stackedAreaChart": "transparency",
    "lineChart": "transparency",
    "lineClusteredColumnComboChart": "transparency",
    "lineStackedColumnComboChart": "transparency",
    # scatterChart / funnel expose no dataPoint transparency at all -> omitted deliberately.
}

# Visual types whose ``dataPoint`` object has no ``defaultColor`` property. Emitting one is a hard
# validation error, so a flat mark colour is dropped for these rather than failing the report.
_NO_DATAPOINT_DEFAULT_COLOR = frozenset({"treemap", "waterfallChart", "azureMap"})


def _constant_mark_color_objects(ws, pbir_vtype=None):
    """The worksheet's flat mark colour as PBIR format objects, keyed by object name, or ``None``.

    The LAST colour source consulted, so it never displaces a real encoding: an explicit per-member
    map, a continuous scale and a Measure-Names series all win. It answers only "the author chose one
    colour for every mark on this sheet" -- which otherwise falls through to Power BI's default blue.

    The CHANNEL depends on the visual, and getting this wrong is the reason line/area previously
    deferred entirely. A column/bar takes ``dataPoint.defaultColor``. A line/area needs
    ``lineStyles.strokeColor`` as well, because ``dataPoint`` alone governs the marker/fill and can
    leave the stroke on the theme colour -- which on a line chart is the whole visual. Shape verified
    against the adjudicated rebuild of this corpus workbook, whose orange line carries BOTH
    ``dataPoint.defaultColor`` and ``lineStyles.strokeColor`` at ``strokeWidth`` ``2D``, and whose
    cyan area carries both plus ``areaShow``.
    """
    color = ws.get("mark_color")
    if not color:
        return None
    lit = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{color}'"}}}}}
    if pbir_vtype == "azureMap":
        # An azureMap has NO ``dataPoint`` object at all -- its marks are drawn by layers, so the
        # cartesian channel is not merely ineffective here, it is a HARD validation error
        # (``PBIR_FORMATTING_PROP_UNKNOWN`` on both ``dataPoint.defaultColor`` and
        # ``dataPoint.transparency``), which fails the whole report rather than just losing a colour.
        # The bubble layer owns the fill, so the flat mark colour rides ``bubbleLayer.fillColor``.
        # Transparency is deliberately dropped rather than guessed onto the layer: the map's own
        # default opacity is a defensible rendering, an invented property name is not.
        return {"bubbleLayer": [{"properties": {"fillColor": lit}}]}
    if pbir_vtype in _NO_DATAPOINT_DEFAULT_COLOR:
        # No dataPoint.defaultColor on this visual -- emitting one is a hard validation error, and
        # losing a flat colour is strictly better than losing the report.
        return None
    props = {"defaultColor": lit}
    tpct = ws.get("mark_transparency")
    if tpct:
        # Named per visual type; omitted entirely where the visual has no such property.
        tprop = _DATAPOINT_TRANSPARENCY_PROP.get(pbir_vtype)
        if tprop:
            props[tprop] = {"expr": {"Literal": {"Value": "%dD" % tpct}}}
    objs = {"dataPoint": [{"properties": props}]}
    if pbir_vtype in _STROKE_COLOR_VTYPES:
        objs["lineStyles"] = [{"properties": {
            "strokeColor": lit,
            "strokeWidth": {"expr": {"Literal": {"Value": "2D"}}},
        }}]
    return objs


# Visuals whose mark colour rides the STROKE as well as the data point. A line's colour IS its
# stroke; an area draws a stroked boundary over its fill. Both are emitted, matching the adjudicated
# rebuild -- setting only ``dataPoint`` leaves the line itself on the theme colour.
#
# The COMBO types are deliberately absent even though they draw a line: checked against the installed
# capabilities, ``lineStyles`` on ``lineClusteredColumnComboChart`` / ``lineStackedColumnComboChart``
# installs NO ``strokeColor`` (it is a real property on lineChart / areaChart / stackedAreaChart, so
# the original finding holds for those). Emitting it on a combo is rejected as
# PBIR_FORMATTING_PROP_UNKNOWN and the whole ``lineStyles`` card is discarded -- taking the
# ``strokeWidth`` with it. A combo's line colour comes from its ``dataPoint`` entry instead (issue
# #100).
_STROKE_COLOR_VTYPES = ("lineChart", "areaChart", "stackedAreaChart")


def _with_constant_mark_color(ws, pbir_vtype, data_point_objects, extra_objects):
    """Fold the worksheet's flat mark colour into ``(data_point_objects, extra_objects)``.

    A worksheet is emitted by TWO paths -- one per dashboard zone, one per standalone worksheet page
    -- and this step existed on the dashboard path only. The consequence was silent and total: a
    workbook of LOOSE worksheets (or any sheet not placed on a dashboard) rebuilt every chart in
    Power BI's default blue, discarding the author's chosen mark colour, while the same sheet on a
    dashboard came out correct. Measured on a nine-sheet workbook: two orange charts and an orange
    scatter all rendered blue.

    Shared here so the two paths cannot drift again -- the fix is the single call site, not two
    copies of the same four lines.

    DECLINES WHEN THE SHEET COLOURS BY AN ENCODING. Tableau writes a ``mark-color`` rule on every
    worksheet, including the inert default it emits when the author chose nothing. Once a field sits
    on the Color shelf the marks are coloured PER MEMBER and that flat value is dead metadata --
    applying it would repaint a whole segmented chart in one colour (measured: Tableau's default
    ``#b4b4b4`` flattening a Region-coloured bar chart to grey). ``mark_colors`` alone is not a
    sufficient guard because it holds only an EXPLICIT member palette; a Color encoding with no
    authored palette leaves it empty while still owning the colour.

    Remaining precedence is preserved: any richer colour source (an explicit per-member palette, a
    continuous scale, a Measure-Names series) already populated ``data_point_objects`` and wins, and
    a lollipop owns its own colour objects. The stroke half rides ``extra_objects`` (merged last by
    ``_visual_json``) with ``setdefault``, so a mark rule that already set ``lineStyles`` keeps it.
    """
    if data_point_objects or ws.get("lollipop"):
        return data_point_objects, extra_objects
    if (ws.get("encodings") or {}).get("color"):
        return data_point_objects, extra_objects
    objs = _constant_mark_color_objects(ws, pbir_vtype)
    if not objs:
        return data_point_objects, extra_objects
    if objs.get("lineStyles"):
        extra_objects = dict(extra_objects or {})
        extra_objects.setdefault("lineStyles", objs["lineStyles"])
    return objs.get("dataPoint"), extra_objects


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


# Continuous colour scale on a CARTESIAN chart's marks: a measure on Tableau's Color shelf with an
# interpolated ramp (e.g. a diverging blue -> grey -> orange scale on Sum(Sentiment Score)). Unlike a
# highlight table / matrix -- whose gradient rides ``values.backColor`` bound to a projected queryRef
# (``_conditional_format``) -- a chart carries its colour measure on neither the Category nor the
# value axis, so there is no projection to bind against. The faithful PBIR home is a ``dataPoint``
# ``fill`` FillRule whose ``Input`` is built DIRECTLY from the colour field's aggregation expression
# and whose selector is a data-plane wildcard (``matchingOption`` 0, no ``metadata``) -- the shape
# Power BI Desktop honours for a per-mark continuous fill. Verified against a Desktop render of a
# scatter + bar coloured by a continuous measure. Discrete colour (a dimension member palette or a
# Measure-Names series) is handled by ``_data_point_colors`` / ``_measure_series_colors`` instead.
_CHART_CONTINUOUS_FILL_TYPES = (VT_COLUMN, VT_BAR, VT_LINE, VT_AREA, VT_SCATTER, VT_COMBO, VT_TREEMAP)


_DISCRETE_COLOUR_TRUE = "#4E79A7"     # Tableau's default blue  -> the TRUE / pass member
_DISCRETE_COLOUR_FALSE = "#F28E2B"    # Tableau's default orange -> the FALSE / fail member
_COLOUR_MEASURE_SUFFIX = " (colour)"
# Power BI's gradient establishes its domain with MIN/MAX over the fill input. Those are invalid on a
# non-numeric measure, and the visual throws ``rsDataShapeQueryTranslationError`` -- "The function
# 'Min' cannot be invoked with the specified arguments" -- the moment the page renders.
_GRADIENT_SAFE_DATATYPES = frozenset({"integer", "real", "number", "float", "decimal", "date",
                                      "datetime", "date_time"})


def _gradient_input_is_safe(color):
    """True when the colour driver can support a continuous gradient's MIN/MAX domain.

    Fail-closed: an unknown/absent datatype reads as SAFE so an existing numeric heat map is
    byte-unchanged, but an explicitly non-numeric one (above all ``boolean``) is refused.
    """
    if not color:
        return False
    if color.get("discrete_measure"):
        return False
    dt = (color.get("datatype") or "").strip().lower()
    return not dt or dt in _GRADIENT_SAFE_DATATYPES


def _discrete_colour_measure_name(color):
    """The name of the colour-twin measure that drives a discrete mark colour.

    Prefers the BOUND model measure name for a rebound pill: the model emits the twin from its own
    measure name, so keying on the Tableau caption would miss whenever the model renamed the measure
    (a DAX-unsafe caption, a de-duplicated collision).
    """
    color = color or {}
    base = (color.get("property") if color.get("measure_rebound") else None) \
        or color.get("caption") or color.get("property") or "colour"
    return dax_safe_measure_name(str(base).strip() + _COLOUR_MEASURE_SUFFIX)


SCATTER_KEY_PREFIX = "_ScatterKey"


def scatter_composite_key_name(dims):
    """Deterministic name for the composite grain key of a multi-dimension scatter.

    Both layers derive the name from the same ordered dimension list, so the model and the report
    agree without a lookup table travelling between them.
    """
    parts = [str((d.get("property") or d.get("caption") or "")).strip() for d in dims]
    return SCATTER_KEY_PREFIX + " (" + " + ".join(p for p in parts if p) + ")"


def date_field_usage(ir):
    """``{date column name (lowered): times the workbook puts it on a shelf}``.

    The model build has to pick ONE date column per fact as the calendar's ACTIVE relationship --
    Power BI allows a single active path between two tables. ``_select_primary_date`` used to choose
    by NAME CONVENTION (literally ``Date``, or an ``order``/``created`` date) and refuse when no
    convention matched, which is right in spirit and blind on real schemas: Salesforce writes
    ``pmdm__StartDate__c``, ``pmdm__EndDate__c``, ``SystemModstamp``,
    ``caseman__AssessmentCompletedDate__c``. Nothing matched, so every one of those facts got NO
    active date and lost its calendar hierarchy.

    The workbook already answers the question the naming convention was guessing at: the author put
    the business date on a SHELF. Measured on Salesforce NPSP, over the 10 facts the calendar
    relates: 5 already had an active date, and shelf evidence resolves the other **5** -- each with
    exactly ONE of its date columns used anywhere (``pmdm__StartDate__c`` x2 while
    ``pmdm__EndDate__c`` is used nowhere) -- leaving **0** unresolved.

    Counted across rows / cols / filters of every worksheet, keyed by COLUMN NAME because that is
    what the model build has when it ranks a fact's own date columns (the field's ``entity`` is still
    the workbook's relation name, not the model's table display name). Ranking is only ever used
    WITHIN one table's own columns, so a shared name cannot promote a column its table does not have.
    Empty -> the model build keeps the pure naming-convention behaviour, byte-for-byte.
    """
    out = {}
    for ws in (ir or {}).get("worksheets") or []:
        for shelf in ("rows", "cols", "filters"):
            for f in (ws.get(shelf) or []):
                if not isinstance(f, dict):
                    continue
                if (f.get("datatype") or "").lower() not in _DATE_TYPES:
                    continue
                key = (f.get("property") or "").strip().lower()
                if key:
                    out[key] = out.get(key, 0) + 1
    return out


def date_field_usage_by_island(ir):
    """``{datasource caption (lowered): {date column (lowered): shelf count}}``.

    The island-scoped sibling of :func:`date_field_usage`, and it exists because that map's own
    contract forbids the use this enables: *"ranking is only ever used WITHIN one table's own
    columns, so a shared name cannot promote a column its table does not have"*. Ordering CANDIDATE
    TABLES against each other is a cross-table comparison, and a workbook-wide count cannot make it.

    Measured on Salesforce NPSP, which keeps four datasource islands: ``pmdm__DeliveryDate__c`` is
    charted 5 times and ``caseman__AssessmentCompletedDate__c`` twice, but every one of those 5 is on
    a SERVICE DELIVERY sheet. Ranked workbook-wide, Delivery Date therefore won the active calendar
    edge on the ASSESSMENTS island too -- where it is charted zero times -- and knocked that island's
    real business date off the calendar, degrading two Assessments charts from
    ``Date (Assessments)[Month Start]`` to a raw fact column.

    A field's ``datasource`` is the island caption, which is the identifier the model build and the
    report already share (see ``_build_date_dimensions``), so scoping is exact rather than inferred.
    """
    out = {}
    for ws in (ir or {}).get("worksheets") or []:
        for shelf in ("rows", "cols", "filters"):
            for f in (ws.get(shelf) or []):
                if not isinstance(f, dict):
                    continue
                if (f.get("datatype") or "").lower() not in _DATE_TYPES:
                    continue
                key = (f.get("property") or "").strip().lower()
                island = (f.get("datasource") or "").strip().lower()
                if key and island:
                    out.setdefault(island, {})[key] = out.setdefault(island, {}).get(key, 0) + 1
    return out


def discrete_colour_palettes(ir):
    """``{measure caption: [(member, "#rrggbb"), ...]}`` -- AUTHORED colours for each discrete
    colour measure this report paints with.

    The model owns the hex-returning colour twin (it is a DAX measure), but only the REPORT layer
    can see which colours the author assigned: Tableau stores them per worksheet as
    ``<style-rule element='mark'><encoding attr='color'><map to='#hex'><bucket>"member"</bucket>``.
    So the first viz pass exports them here and the model build consumes them, the same
    report-informs-model channel the scatter composite grain key already uses.

    Only an EXPLICIT per-member assignment is exported. A worksheet whose author never opened the
    colour editor yields nothing, and the model then falls back to Tableau's own default categorical
    assignment -- which is what such a worksheet actually renders, so the fallback is a faithful
    reproduction rather than a guess. Returns ``{}`` for a report that paints nothing discretely.
    """
    out = {}
    for ws in (ir or {}).get("worksheets") or []:
        color = (ws.get("encodings") or {}).get("color") or {}
        if not color.get("discrete_measure") or color.get("kind") != "value":
            continue
        caption = (color.get("caption") or "").strip()
        palette = ws.get("mark_colors") or {}
        members = [(m.get("value"), m.get("color"))
                   for m in (palette.get("members") or [])
                   if m.get("value") and m.get("color")]
        if caption and members and caption not in out:
            out[caption] = members
    return out


def scatter_composite_keys(ir):
    """``[{table, name, columns}]`` -- the composite grain keys this report's scatters need.

    Power BI's scatter allows exactly ONE field in its Values/Details well (PBIR role ``Category``,
    ``maxPerRole = 1``), while Tableau's Detail shelf takes many and grains the marks by the distinct
    COMBINATION. Emitting several projections is a hard ``PBIR_ROLE_MAX_EXCEEDED`` error, and simply
    dropping the extras silently changes the grain -- measured, keeping the wrong pill collapsed
    ~5,000 marks into 3, which renders perfectly.

    Microsoft documents the fix: *"If your data doesn't include a specific row number or ID, you can
    create a field to concatenate your x and y values together. The field must be unique for each
    point you want to plot."* So the dimensions are folded into one key column, which by construction
    has exactly Tableau's distinct-tuple count.

    Returns ``[]`` for a report with no multi-dimension scatter, so nothing is emitted unless needed.
    """
    out, seen = [], set()
    for ws in (ir or {}).get("worksheets") or []:
        if ws.get("visual_type") != VT_SCATTER:
            continue
        dims = _scatter_category_dims(ws)
        if len(dims) < 2:
            continue
        tables = {d.get("entity") for d in dims if d.get("entity")}
        if len(tables) != 1:
            continue        # a key spanning two tables is not a column -- fail closed
        name = scatter_composite_key_name(dims)
        key = (next(iter(tables)), name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"table": key[0], "name": name,
                    "columns": [d.get("property") or d.get("caption") for d in dims]})
    return out


def _scatter_category_dims(ws):
    """The ordered category dimensions a scatter worksheet grains its marks by."""
    enc = ws.get("encodings") or {}
    detail = enc.get("detail")
    detail_dims = [f for f in (enc.get("detail_dims") or []) if f and f.get("kind") == "category"]
    rows, cols = ws.get("rows") or [], ws.get("cols") or []
    cats = [f for f in (list(rows) + list(cols)) if f and f.get("kind") == "category"]
    cats += ([detail] if detail and detail.get("kind") == "category" else []) + detail_dims
    out, seen = [], set()
    for f in cats:
        k = (f.get("entity"), f.get("property"))
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def _discrete_colour_twin_unavailable(color):
    """True when the model provably did NOT produce a colour twin for this driver.

    The twin is emitted only for a base measure the model actually TRANSLATED. A calc the
    translator could not express lands as an inert ``= BLANK()`` stub and gets no twin -- but the
    report layer, which derives the twin's NAME from the caption, will happily reference one that
    does not exist. Measured across the corpus: 4 such dangling references, and Power BI resolves
    none of them (the visual simply keeps its default colours while the run reports success).

    The signal is the same one the continuous path already trusts: ``measure_rebound``, stamped by
    the model<->viz contract when the calc was rebound to a REAL translated measure
    (``_MEASURE_BIND_OK`` = translated / assisted-approved). A calc pill that reaches emit WITH the
    model's manifest in hand (``model_consulted``) but WITHOUT a rebind was offered to the model and
    declined by it, so its twin cannot exist.

    ``model_consulted`` is what keeps this from over-firing. Without it the gate cannot tell "the
    model said no" from "there is no model yet" -- and the pre-rebind viz pass, plus every direct
    ``parse_twb`` caller, has no model by construction. Gating on the rebind alone therefore
    deferred every calc-driven colour in the single-pass path. A NON-calc driver (a plain aggregated
    column) has no twin to miss and is never gated here.
    """
    return (bool(color.get("is_calc")) and bool(color.get("model_consulted"))
            and not color.get("measure_rebound"))


def _discrete_unbound_defer(ws, color, kind, warnings):
    """Defer a DISCRETE colour whose twin the model did not produce, and name the reason."""
    warnings.append(_warn(
        "worksheet", ws["name"],
        "discrete colour deferred: the colour driver %r was not translated into a model measure, "
        "so the colour measure %r it would be painted from does not exist. Emitting the reference "
        "anyway produces a visual that binds a missing object -- it renders with default colours "
        "and reports no error. The visual is emitted unpainted; translate the source calc to "
        "restore the encoding"
        % (color.get("caption"), _discrete_colour_measure_name(color))))
    return {"kind": kind,
            "colour_measure": _discrete_colour_measure_name(color),
            "source_measure": color.get("caption"),
            "status": "deferred",
            "reason": "colour driver has no translated model measure, so no colour twin exists"}


# -- conditional-colour compiler: report-side wiring -------------------------------------------
# ``colour_rules`` is the pure analyser/lowerer -- it knows Tableau formulas and PBIR shapes, and
# nothing about this workbook's model. These are the two things only the emitter can supply: which
# model object a Tableau leaf binds to, and which colour each string member is painted.
try:
    from . import colour_rules as _CR
except ImportError:  # pragma: no cover - direct-script execution
    try:
        import colour_rules as _CR
    except ImportError:  # pragma: no cover - the compiler is optional
        _CR = None


def _colour_leaf_fields(ws):
    """``{caption(lower): field}`` for every field this worksheet already resolved.

    Reusing the worksheet's OWN resolved fields keeps a lowered predicate bound to the same model
    objects the rest of the visual uses, rather than re-deriving a binding from the caption and
    risking a second, subtly different answer for the same column.
    """
    out = {}
    for f in (list(ws.get("rows") or []) + list(ws.get("cols") or [])
              + [v for v in (ws.get("encodings") or {}).values() if isinstance(v, dict)]):
        cap = str((f or {}).get("caption") or "").strip().lower()
        if cap and cap not in out:
            out[cap] = f
    return out


def _colour_param_values(param_binding):
    """``{normalised param key: {table, measure}}`` for every what-if parameter with a scalar reader.

    Keyed by BOTH the parameter's internal name and its caption, because a Tableau formula may spell
    ``[Parameters].[X]`` either way, and normalised through :func:`_norm_param_key` so bracket and
    case differences at the model<->viz seam cannot cause a near-miss. Derived ONCE at IR-build time
    (where ``param_binding`` is in scope) and carried on the IR, the same way parameter controls are.
    """
    out = {}
    for pid, v in ((param_binding or {}).get("values") or {}).items():
        if not isinstance(v, dict) or not v.get("table") or not v.get("measure"):
            continue
        rec = {"table": v["table"], "measure": v["measure"]}
        for key in (pid, v.get("caption")):
            k = _norm_param_key(key)
            if k:
                out.setdefault(k, rec)
    return out


def _colour_rule_resolver(ws, model_table, field_map, param_values=None):
    """Build ``resolve(tokens) -> PBIR expression`` for the rung-1 lowering.

    Recognises the three leaf shapes a colour predicate is built from -- ``AGG([Field])``, a bare
    ``[Field]``, and a what-if parameter ``[Parameters].[X]`` -- and routes the first two through
    :func:`_field_expression`, so a rule compares against an expression produced by the SAME code
    path that projects the visual's own columns. Anything else returns ``None``, which aborts the
    whole rule (fail-closed).

    A parameter operand binds to the model's ``SELECTEDVALUE`` measure rather than to the picker
    column: the picker is a table of CANDIDATE rows, so referencing it in a comparison would compare
    against the whole domain instead of the current selection. A parameter the model did not turn
    into a what-if table resolves to nothing and the rule declines, which is correct -- there is no
    object in the model holding that value.
    """
    known = _colour_leaf_fields(ws)
    params = dict(param_values or {})

    def _synth(caption, aggregation):
        base = known.get(str(caption).strip().lower())
        field = dict(base) if base else {
            "caption": caption, "entity": None, "property": caption,
            "datatype": None, "is_calc": False, "derivation": None}
        if aggregation:
            field["binding"] = "aggregation"
            field["aggregation"] = aggregation
            field["kind"] = "value"
        else:
            field.setdefault("binding", "column")
            field.setdefault("kind", "category")
        return field

    def _param(parts):
        if len(parts) < 2 or _norm_param_key(parts[0]) != "parameters":
            return None
        hit = params.get(_norm_param_key(parts[-1]))
        if not hit:
            return None
        return {"Measure": {"Expression": {"SourceRef": {"Entity": hit["table"]}},
                            "Property": hit["measure"]}}

    def resolve(toks):
        toks = list(toks)
        if len(toks) == 1 and toks[0][0] == "field":
            expr, _q, _n = _field_expression(_synth(toks[0][1], None), model_table, field_map)
            return expr
        if len(toks) == 1 and toks[0][0] == "qfield":
            return _param(list(toks[0][1] or []))
        if (len(toks) == 4 and toks[0][0] == "id" and toks[1] == ("op", "(")
                and toks[2][0] == "field" and toks[3] == ("op", ")")):
            agg = str(toks[0][1]).capitalize()
            if agg not in _AGG_FUNC:
                return None
            expr, _q, _n = _field_expression(_synth(toks[2][1], agg), model_table, field_map)
            return expr
        return None

    return resolve


# A backslash escape inside a serialised mark-colour member value (Tableau writes ``"Top 25\%"``).
_MARK_COLOUR_ESCAPE_RE = re.compile(r"\\(.)")


def _colour_member_palette(ws, members):
    """``{member: hex}`` for the string members of a colour calculation.

    The author's own assignment wins (``mark_colors`` -- the worksheet's ``<map to='#hex'>``
    palette); otherwise Tableau's default categorical ramp in SORTED member order, which is how
    Tableau assigns it, so an unauthored domain reproduces the source's own hues. Same precedence
    the model colour twin uses, so a rule and a twin can never paint one workbook two ways.
    """
    authored = {}
    for m in ((ws.get("mark_colors") or {}).get("members") or []):
        if m.get("value") and m.get("color"):
            raw = str(m["value"]).strip()
            # Index BOTH the raw spelling and the unescaped one. Tableau backslash-escapes some
            # characters inside a serialised colour-map member -- ``"Top 25\%"`` for a member the
            # calc itself writes as ``"Top 25%"`` -- so a literal comparison silently misses and the
            # member falls through to the default ramp. Measured on a dynamic-quartile workbook:
            # ``middle`` matched while ``Top 25%`` and ``Bottom 25%`` did not, so the author's own
            # green/red became two arbitrary palette hues, with no warning anywhere. Both forms are
            # indexed rather than only the unescaped one, so a member whose real name contains a
            # backslash keeps matching exactly as before.
            for key in {raw, _MARK_COLOUR_ESCAPE_RE.sub(r"\1", raw)}:
                authored.setdefault(key.casefold(), m["color"])
    out = {}
    for i, member in enumerate(sorted(members, key=lambda s: (s.casefold(), s))):
        out[member] = authored.get(member.strip().casefold()) or _TABLEAU_10[i % len(_TABLEAU_10)]
    return out


def _discrete_colour_rule(ws, color, model_table, field_map, param_values=None):
    """Compile the colour driver's calc into a PBIR ``Conditional`` (rung 1), or ``None``.

    Rung 1 is preferred over every other mechanism because it adds NOTHING to the model: the Tableau
    string members collapse into ``Value`` literals, so there is no synthetic string measure and no
    colour twin, and the result opens in Desktop's Conditional formatting dialog as editable rules.
    Only reached for predicates the visual's own semantic query can evaluate -- a view-scoped one
    (``WINDOW_*`` / ``RANK`` / percentile) has no rung-1 form and falls through to the deferral.
    """
    if _CR is None or not color:
        return None
    spec = _CR.analyse_colour_calc(color.get("formula"))
    if not spec.supported or not spec.closed_domain or spec.scope == _CR.SCOPE_VIEW:
        return None
    return _CR.lower_to_conditional(
        spec, _colour_member_palette(ws, spec.members),
        _colour_rule_resolver(ws, model_table, field_map, param_values))


# The hidden Visual Calculation that carries a view-scoped colour, and its queryRef. Named rather
# than numbered so it is recognisable in the field list; the queryRef is distinct from the
# ``select*`` names the quick-table-calc path already claims on the same visual.
_COLOUR_VC_NAME = "Colour rule"
_COLOUR_VC_QUERY_REF = "colourRule"


# The measure-bearing query roles, in the order a colour Visual Calculation should claim one. A
# Visual Calculation is a calculation over the visual's MEASURES, so it must be declared in a
# measure role -- ``Values`` on a matrix/table, ``Y`` (or ``Y2``/``X``) on a chart. The dimension
# roles (``Category``/``Rows``/``Columns``/``Series``) are deliberately absent: declaring it there
# validates clean and is semantically wrong.
#
# THIS LIST IS THE FIX FOR A MEASURED DEFECT. The first attempt at rung 4 appended to a single
# hard-coded role, so on every visual that did not have that role the append silently no-opped
# while the formatting property still emitted a ``SelectRef`` naming it -- a dangling reference on
# roughly half of ``0070_new_max``'s visuals, invisible to validation. Hence
# :func:`_declare_colour_projection` returns ``None`` rather than guessing, and every caller must
# treat ``None`` as "defer", never as "emit anyway".
_COLOUR_VC_ROLES = ("Values", "Y", "Y2", "X")


def _declare_colour_projection(state, dax):
    """Declare the hidden colour Visual Calculation on ``state``; return its queryRef or ``None``.

    ``state`` is the very object the emit site passes to :func:`_visual_json`, so appending here IS
    the binding -- measured end to end: the projection appears in the emitted ``visual.json`` on
    both the pre-rebind and the final tree.

    Returns ``None`` when the visual carries no measure role to host the calculation, which is the
    only honest answer: there is nothing to attach it to, so a ``SelectRef`` naming it would dangle.
    """
    if not isinstance(state, dict) or not dax:
        return None
    role = next((state[r] for r in _COLOUR_VC_ROLES
                 if isinstance(state.get(r), dict)), None)
    if role is None:
        return None
    # Idempotent: an identical calculation already declared on this visual is REUSED rather than
    # declared again. The colour emitters can be reached more than once for one visual, and a
    # second copy is not a correctness bug (both references resolve) but it computes the same
    # window twice and shows a duplicate entry in Desktop's field list.
    for existing in role.get("projections", []):
        nvc = (existing.get("field") or {}).get("NativeVisualCalculation") or {}
        if nvc.get("Expression") == dax and existing.get("queryRef"):
            return existing["queryRef"]
    qref = _colour_vc_query_ref(state)
    role.setdefault("projections", []).append({
        "field": {"NativeVisualCalculation": {
            "Language": "dax", "Expression": dax, "Name": _COLOUR_VC_NAME}},
        "queryRef": qref,
        "nativeQueryRef": qref,
        "hidden": True,
    })
    return qref


def _colour_vc_query_ref(state):
    """A queryRef for the colour Visual Calculation that no existing projection already uses."""
    used = {p.get("queryRef") for role in (state or {}).values()
            for p in (role or {}).get("projections", [])}
    qref, i = _COLOUR_VC_QUERY_REF, 1
    while qref in used:
        i += 1
        qref = "%s%d" % (_COLOUR_VC_QUERY_REF, i)
    return qref


def _colour_projection_dax_resolver(ws, projections, model_table, field_map, param_values=None):
    """Build ``resolve(tokens) -> "[Projected Column]"`` for the rung-4 lowering.

    A Visual Calculation addresses the visual's OWN matrix, so its operands are the projected column
    names, not model objects. Each Tableau leaf is therefore lowered to a PBIR expression exactly as
    rung 1 would, then matched against the projections this visual already carries -- so an operand
    is only accepted when the visual really shows it. An operand the visual does not project cannot
    be referenced from a Visual Calculation at all, and returning ``None`` for it aborts the rule,
    which is the honest outcome rather than a calculation over a column that is not there.
    """
    inner = _colour_rule_resolver(ws, model_table, field_map, param_values)
    by_expr = {}
    for p in projections or []:
        native = str(p.get("nativeQueryRef") or "").strip()
        if native:
            by_expr.setdefault(_dumps(p.get("field")), native)

    def resolve(toks):
        expr = inner(toks)
        if expr is None:
            return None
        native = by_expr.get(_dumps(expr))
        return "[%s]" % native if native else None

    return resolve


def _discrete_colour_visual_calc(ws, color, projections, model_table, field_map,
                                 param_values=None):
    """Compile a VIEW-SCOPED colour driver into Visual-Calculation DAX, or ``None``.

    Rung 4: the only mechanism that can express "compare this mark to the other marks in the view".
    Returns the DAX only; the caller declares it as a hidden projection and binds it by ``SelectRef``
    (the inline form was refuted by render -- it validates clean and paints nothing).
    """
    if _CR is None or not color:
        return None
    spec = _CR.analyse_colour_calc(color.get("formula"), datatype=color.get("datatype"))
    if not spec.supported or not spec.closed_domain:
        return None
    return _CR.lower_to_visual_calc(
        spec, _colour_member_palette(ws, spec.members),
        _colour_projection_dax_resolver(ws, projections, model_table, field_map, param_values))


def _discrete_view_scoped_defer(ws, color, kind, warnings):
    """Defer a DISCRETE colour whose driver is a VIEW-level table calc, and say exactly why.

    A ``WINDOW_*`` / ``RUNNING_*`` / ``RANK`` / ``INDEX`` calc computes across the marks of the
    VIEW. Translated into a standalone model measure it keeps neither the visual's axis nor its
    partition, so the comparison it encodes is evaluated against the wrong rows. Measured on
    ``0070_new_max`` (``SUM([Sales]) = WINDOW_MAX(SUM([Sales]), FIRST(), 0)``, four monotonically
    rising years where every bar sets a new maximum): the emitted measure returned **False on every
    bar**, so every bar took the "no" colour. The visual rendered, validated clean, and was wrong.

    The continuous paths have always refused a table-calc driver for exactly this reason
    (``is_table_calc_defer``); the discrete path did not, which is the asymmetry this closes. Until
    the driver can be lowered to a Power BI **Visual Calculation** -- which does evaluate against
    the visual's own matrix -- painting no colour is the honest outcome: the source's colour
    encoding is disclosed and unpainted rather than reproduced backwards.
    """
    warnings.append(_warn(
        "worksheet", ws["name"],
        "discrete colour deferred: the colour driver %r is a VIEW-level table calc (%s). Such a "
        "calc compares each mark against the OTHER MARKS IN THE VIEW, which a standalone model "
        "measure cannot reproduce -- it would evaluate against the wrong rows and paint a "
        "confident but incorrect colour (measured: every mark took the same wrong swatch). The "
        "visual is emitted with its default colours; the encoding needs a Visual Calculation to "
        "rebuild faithfully"
        % (color.get("caption"), " ".join(str(color.get("formula") or "").split())[:120])))
    return {"kind": kind,
            "colour_measure": _discrete_colour_measure_name(color),
            "source_measure": color.get("caption"),
            "status": "deferred",
            "reason": "colour driver is a view-level table calc (needs a Visual Calculation)"}


def _chart_discrete_measure_fill(ws, state, vtype, model_table, field_map, warnings,
                                 param_values=None):
    """A DISCRETE aggregate measure on Tableau's Colour shelf -> ``(dataPoint objects, fact)``.

    Tableau paints marks by CATEGORY when a discrete field sits on Colour. When that field is an
    *aggregate* measure (``IF SUM([Profit]) > 0 THEN TRUE ELSE FALSE END``) Power BI cannot express
    it as a native categorical legend: a legend needs a grouping COLUMN, a column is row-level, and
    a row-level split changes the mark grain (one bar becomes two stacked segments) -- a well-known
    Power BI pitfall, and a silent corruption of the numbers.

    So this emits what an experienced Power BI developer writes for exactly this problem: a **colour
    measure** returning a hex string, bound through conditional formatting. Microsoft's own guidance
    calls this out -- *"You can create a DAX measure that returns color values based on your business
    logic"* -- and it is the `Field value` format style, so it round-trips **editably** in Desktop's
    `fx` dialog rather than being unreachable JSON. It also keeps the per-mark AGGREGATE grain, which
    is the whole point.

    THE SELECTOR IS LOAD-BEARING. Without ``dataViewWildcard`` Power BI still honours the expression
    but evaluates it in ONE context and paints every mark the same colour -- with a clean validation
    pass. Rendered proof: with the selector a Sub-Category bar chart split Bookcases/Supplies/Tables
    from the profitable members, and a scatter split thousands of points on the zero line; without
    it, every mark was identical. This is a validation-invisible failure, hence the explicit note.
    """
    color = ws["encodings"].get("color")
    if not color or not color.get("discrete_measure"):
        return None, None
    if vtype not in _CHART_CONTINUOUS_FILL_TYPES:
        return None, None
    if color.get("kind") != "value" or color.get("binding") not in ("aggregation", "measure"):
        return None, None
    if _is_view_level_calc(color):
        # RUNG 4: the only mechanism that can express "compare this mark to the OTHER marks in the
        # view". Bound as a hidden Visual Calculation projection + ``SelectRef``; the inline form
        # was refuted by render (validates clean, paints nothing).
        #
        # The first attempt at this shipped a dangling ``SelectRef`` on roughly half of
        # ``0070_new_max``'s visuals. The cause was NOT that the mutation is discarded -- measured
        # since: a projection appended to ``state`` here reaches the emitted ``visual.json`` on
        # both the pre-rebind and the final tree. It was that the append targeted a role the visual
        # did not have (a chart has ``Y``, not ``Values``), so it silently no-opped while the
        # property still emitted. :func:`_declare_colour_projection` picks a measure role that
        # actually exists and returns ``None`` when there is none, and ``None`` means DEFER.
        measures = next((state[r] for r in _COLOUR_VC_ROLES
                         if isinstance(state.get(r), dict)), None)
        projections = (measures or {}).get("projections", [])
        dax = _discrete_colour_visual_calc(
            ws, color, projections, model_table, field_map, param_values) if projections else None
        qref = _declare_colour_projection(state, dax)
        if qref:
            return ([{"properties": {"fill": {"solid": {"color": {
                          "expr": {"SelectRef": {"ExpressionName": qref}}}}}},
                      "selector": {"data": [{"dataViewWildcard": {"matchingOption": 0}}]}}],
                    {"kind": "chart_discrete_measure_fill", "style": "visual_calculation",
                     "source_measure": color.get("caption"),
                     "query_ref": qref, "status": "emitted"})
        return None, _discrete_view_scoped_defer(ws, color, "chart_discrete_measure_fill", warnings)
    # RUNG 1 first: a native Rules conditional format on the mark fill -- no model objects.
    rule = _discrete_colour_rule(ws, color, model_table, field_map, param_values)
    if rule is not None:
        return ([{"properties": {"fill": {"solid": {"color": {"expr": rule}}}},
                  "selector": {"data": [{"dataViewWildcard": {"matchingOption": 0}}]}}],
                {"kind": "chart_discrete_measure_fill", "style": "rules",
                 "source_measure": color.get("caption"),
                 "cases": len(rule["Conditional"]["Cases"]), "status": "emitted"})
    if _discrete_colour_twin_unavailable(color):
        return None, _discrete_unbound_defer(ws, color, "chart_discrete_measure_fill", warnings)
    measure_name = _discrete_colour_measure_name(color)
    fact = {"kind": "chart_discrete_measure_fill",
            "colour_measure": measure_name,
            "source_measure": color.get("caption"),
            "colors": [_DISCRETE_COLOUR_TRUE, _DISCRETE_COLOUR_FALSE],
            "status": "emitted"}
    objects = [{
        "properties": {"fill": {"solid": {"color": {"expr": {
            "Measure": {"Expression": {"SourceRef": {"Entity": MEASURES_TABLE}},
                        "Property": measure_name}}}}}},
        # REQUIRED -- see the docstring. matchingOption 0 = every data point.
        "selector": {"data": [{"dataViewWildcard": {"matchingOption": 0}}]},
    }]
    warnings.append(_warn(
        "worksheet", ws["name"],
        "discrete colour: Tableau colours these marks by a DISCRETE aggregate measure (%r). Power BI "
        "has no native categorical legend for a measure-driven colour -- a legend needs a grouping "
        "COLUMN, which is row-level and would change the mark grain -- so this is rebuilt as "
        "conditional formatting driven by a colour measure %r (Format style 'Field value', editable "
        "in Desktop's fx dialog). The marks keep their aggregate grain; there is no colour legend"
        % (color.get("caption"), measure_name)))
    return objects, fact


def _chart_continuous_fill(ws, state, vtype, model_table, field_map, warnings):
    """Continuous colour measure on a chart's marks -> (data_point_objects, fact).

    ``data_point_objects`` is the ``visual.objects.dataPoint`` entry list (a single ``fill`` FillRule
    gradient driven by the colour measure's aggregation) or ``None``; ``fact`` is an additive
    descriptor of the fill (``status`` ``emitted`` / ``deferred`` plus the raw palette) for the
    candidate record, or ``None`` when the worksheet has no continuous colour scale OR is not a chart
    that carries a per-mark fill (a table / matrix / map colours elsewhere -- silent skip, no fact).

    WARN-NEVER-WRONG: the fill is emitted ONLY for the cartesian chart types AND when the colour
    driver resolves to a clean model measure (an ``aggregation`` / ``measure`` binding) that is not a
    quick table calc without a rebound model measure. Otherwise the visual emits with theme colours,
    a structured warning names the deferral, and the raw Tableau palette is preserved in ``fact``.
    The FillRule ``Input`` reuses the EXACT aggregation expression the colour field would project, so
    the fill never references something the model does not carry.
    """
    cg = ws.get("color_gradient")
    if not cg:
        return None, None
    if vtype not in _CHART_CONTINUOUS_FILL_TYPES:
        # Tables / matrices / maps carry their continuous colour on backColor / the Value / Gradient
        # role -- not a per-mark chart fill. Silently skip (no fact, no warning) rather than feign a
        # deferral (the highlight-table path in ``_conditional_format`` owns those).
        return None, None
    color = ws["encodings"].get("color")
    fact = {
        "kind": "chart_continuous_fill",
        "palette_type": cg["palette_type"],
        "center": cg["center"],
        "colors": cg["colors"],
    }
    # A quick table calc normally defers (the model carries no equivalent measure) unless the colour
    # pill was rebound to a real model measure via the model<->viz contract (``measure_rebound``).
    is_table_calc_defer = cg["is_table_calc"] and not (color or {}).get("measure_rebound")
    if (color is None or color["kind"] != "value"
            or color["binding"] not in ("aggregation", "measure")
            or is_table_calc_defer):
        reason = ("colour driver is a quick table calc -- no equivalent model measure yet"
                  if is_table_calc_defer
                  else "colour driver is not a continuous model measure in this visual")
        warnings.append(_warn(
            "worksheet", ws["name"],
            "continuous mark colours deferred ({0}); the visual is emitted with theme "
            "colours".format(reason)))
        fact["status"] = "deferred"
        fact["reason"] = reason
        return None, fact

    input_expr, bound_ref, _ = _field_expression(color, model_table, field_map)
    # CRASH GUARD, unconditional. A gradient establishes its domain with MIN/MAX over this input, and
    # neither is valid on a non-numeric measure: Power BI throws rsDataShapeQueryTranslationError
    # ("The function 'Min' cannot be invoked with the specified arguments") the moment the page
    # renders, and NO structural gate sees it -- the TMDL deserialises, the M is valid, the PBIR
    # validates clean. The discrete path above should already have claimed such a driver; this is the
    # belt-and-braces that makes "never emit a visual that errors on open" an invariant rather than a
    # claim, however the colour scale arrived.
    if not _gradient_input_is_safe(color):
        reason = ("colour driver %r is not numeric (%s), so a continuous gradient's MIN/MAX domain "
                  "cannot be computed over it" % (color.get("caption"),
                                                  color.get("datatype") or "unknown type"))
        warnings.append(_warn(
            "worksheet", ws["name"],
            "continuous mark colours deferred (%s); the visual is emitted with theme colours rather "
            "than a gradient that would fail at render" % reason))
        fact["status"] = "deferred"
        fact["reason"] = reason
        return None, fact
    data_point_objects = [{
        "properties": {"fill": {"solid": {"color": {"expr": {
            "FillRule": {
                "Input": input_expr,
                "FillRule": _gradient_color_stops(cg)}}}}}},
        "selector": {"data": [{"dataViewWildcard": {"matchingOption": 0}}]},
    }]
    fact["status"] = "emitted"
    fact["bound_measure"] = bound_ref
    if cg.get("default_palette"):
        fact["default_palette"] = True
        _disclose_default_palette(ws, cg, warnings)
    return data_point_objects, fact


# KPI / card label colours: a recoloured Tableau card writes the category-label colour and the
# value (big-number) colour / size on its customized-label runs. The faithful PBIR home is the card
# formatting objects ``categoryLabels`` (the label) and ``dataLabels`` (the value) -- each a
# ``color`` property (and an optional value ``fontSize``), verified against the Power BI card /
# multiRowCard formatting reference. Bold is deliberately NOT emitted (the card label weight
# property is unconfirmed -> warn-never-wrong).
_CARD_LABEL_COLOR_TYPES = ("card", "multiRowCard")


def _card_value_object_name(vtype):
    """The card-family big-number VALUE formatting object name for the emitted ``vtype``.

    A ``card`` exposes its value callout as ``labels``; a ``multiRowCard`` exposes it as ``dataLabels``
    (verified against ``formatting list-objects card`` / ``multiRowCard`` -- a ``dataLabels`` on a
    ``card`` OR a ``labels`` on a ``multiRowCard`` is rejected ``PBIR_FORMATTING_OBJECT_UNKNOWN`` and
    the value colour / size / display-units are silently dropped at render).
    """
    return "labels" if vtype == "card" else "dataLabels"


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
        out[_card_value_object_name(vtype)] = [{"properties": value_props}]
    return out or None


# Card value display units (fidelity): a Power BI ``card`` defaults its big-number ``labelDisplayUnits``
# to Auto (0), which ABBREVIATES (2,747 -> "3K") and breaks fidelity vs the Tableau text / BAN mark,
# which shows the value in full. Setting it to Auto (0) does NOT disable the abbreviation -- "None" is
# the display-units enum value 1, emitted as the Double literal ``1D``. We force None on every rebuilt
# ``card`` so the big number reads in full. The property lives on the ``card`` value object ``labels``
# (merge-safe via ``setdefault`` so an author-recoloured value keeps its properties and only gains the
# display units). A ``multiRowCard``'s value object is ``dataLabels``, which has NO ``labelDisplayUnits``
# channel (verified against ``formatting describe-object multiRowCard dataLabels``) -> display units
# cannot be pinned there, so this is a no-op for it (and every non-card visual).
def _apply_card_display_units(visual, vtype):
    """Force ``labels.labelDisplayUnits = None`` (``1D``) on a rebuilt ``card`` so it reads in full.

    A no-op for ``multiRowCard`` (its ``dataLabels`` object has no display-units property) and every
    non-card visual. Never clobbers an existing ``labels`` value object (author colour / font size);
    it only adds the display-units property when absent.
    """
    if vtype != "card":
        return
    objs = visual.setdefault("objects", {})
    labels = objs.setdefault("labels", [{"properties": {}}])
    if not labels:
        labels.append({"properties": {}})
    props = labels[0].setdefault("properties", {})
    props.setdefault("labelDisplayUnits", {"expr": {"Literal": {"Value": "1D"}}})


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


_AZURE_MAP_US_STATES_GEOJSON = (
    "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json")
# Closest render-verified match to Tableau's own map chrome: white background, soft grey borders, no
# controls. Four variants were compared in Desktop; `blank` lost the state borders (white on white),
# `grayscale_light` drew a grey basemap with Canada/Mexico, and plain `blank` with the default stroke
# drew a loud blue outline. `blank_accessible` + #D9D9D9 was the closest to the Tableau reference.
_AZURE_MAP_DEFAULT_STYLE = "blank_accessible"
_AZURE_MAP_POLYGON_STROKE = "#D9D9D9"

# Tableau's per-worksheet basemap -> the Power BI `azureMap` `mapControls.defaultStyle` that draws
# the same thing. Reported in #128: the constant above was applied to EVERY emitted map regardless of
# what the source draws, so a satellite or dark basemap rebuilt as marks floating on white. One
# workbook can contain satellite, dark and light basemaps at once, so no single constant can be
# right -- the style has to come from the worksheet.
#
# Keys are the values Tableau writes to `<style-rule element='map'><format attr='map-style'>`,
# harvested from real workbooks rather than guessed (counts across the corpora on this machine:
# `light` x20, `tableau-light-gray` x7, `satellite` x1, two `mapbox://` customs). `tableau-z-black`
# is included on the reporter's thumbnail evidence for a dark basemap; it is the one entry here not
# observed locally, and is safe because an unrecognised key falls through to the default rather than
# guessing.
#
# Values are checked against the live enum, not assumed --
# `powerbi-report-author formatting describe-object azureMap mapControls` reports exactly:
#   road, satellite, satellite_road_labels, grayscale_dark, night, grayscale_light,
#   road_shaded_relief, blank, blank_accessible, high_contrast_dark, high_contrast_light
_TABLEAU_MAP_STYLE_TO_AZURE = {
    "satellite": "satellite",
    "tableau-satellite": "satellite",
    "light": "grayscale_light",
    "tableau-light": "grayscale_light",
    "tableau-light-gray": "grayscale_light",
    "gray": "grayscale_light",
    "tableau-gray": "grayscale_light",
    "dark": "grayscale_dark",
    "tableau-dark": "grayscale_dark",
    "tableau-z-black": "night",
    "black": "night",
    "normal": "road",
    "tableau-normal": "road",
    "streets": "road",
    "tableau-streets": "road",
    "outdoors": "road_shaded_relief",
}


def _worksheet_map_style_raw(ws_el):
    """The raw ``map-style`` token a worksheet ELEMENT declares, or ``None``.

    Read once at parse time (the emitter only ever sees the parsed dict) and stored as
    ``map_style_raw``. ``<style-rule element='map'><format attr='map-style' value='...'>``.
    """
    for rule in _findall_local(ws_el, "style-rule"):
        if (rule.get("element") or "").lower() != "map":
            continue
        for fmt in _children_local(rule, "format"):
            if (fmt.get("attr") or "") == "map-style":
                return (fmt.get("value") or "").strip() or None
    return None


def _tableau_map_style(ws):
    """The worksheet's own basemap as a Power BI ``defaultStyle``, or ``None`` to keep the default.

    Reads the parsed ``map_style_raw`` -- a PER-WORKSHEET signal, which is the point: a single
    workbook may draw satellite on one sheet and a dark basemap on the next, so a module-level
    constant cannot be correct for both (#128).

    Returns ``None``, meaning "leave :data:`_AZURE_MAP_DEFAULT_STYLE` alone", in three cases, and
    each is deliberate:

    * **the worksheet declares no ``map-style``.** Absence is not evidence of a blank basemap -- it
      means the author never moved off Tableau's default. Changing that case would alter every map
      this engine has ever emitted, and the current constant is the one value that WAS compared
      against a Tableau reference in Desktop (see above). Left for a render-verified change of its
      own rather than folded in here on inference.
    * **a custom Mapbox style** (``mapbox://styles/<user>/<id>``). The basemap is an arbitrary
      third-party design that no stock Azure style reproduces; picking a near-miss would silently
      misrepresent it. The caller warns instead.
    * **an unrecognised token.** Fail-closed: a Tableau version that spells a style differently keeps
      today's behaviour rather than being mapped by guesswork.
    """
    raw = (ws or {}).get("map_style_raw")
    if not raw:
        return None
    return _TABLEAU_MAP_STYLE_TO_AZURE.get(str(raw).strip().lower())


def _tableau_map_style_raw(ws):
    """The raw ``map-style`` token from the PARSED worksheet, or ``None`` -- for honest warnings."""
    return (ws or {}).get("map_style_raw")


def _azure_map_objects(ws, visual_type, shading_field=None):
    """The ``objects`` block for an ``azureMap``, or ``None``.

    Render-proven, not inferred. A 4-visual control page in Power BI Desktop established:

    * ``azureMap`` with a Category renders basemap + bubbles;
    * ``azureMap`` with a data-bound ``referenceLayer`` renders a real choropleth;
    * a byte-identical ``shapeMap`` -- what this engine used to emit -- rendered **completely
      blank**, on the same machine, same data, with the shared ``usa.states.topo`` resource.

    Two pieces are non-obvious and were each proved by rendering:

    * ``bubbleLayer.show = false`` is REQUIRED on a choropleth. Without it Azure Maps draws a bubble
      on every state centroid ON TOP of the shaded polygons.
    * ``referenceLayer`` is a TWO-entry array: entry ``[0]`` (no selector) carries the layer itself,
      and entry ``[1]`` (a ``dataViewWildcard`` selector) carries the data-bound ``polygonFillColor``.
      One merged entry does not shade.

    The choropleth depends on a PUBLIC GeoJSON URL, so an offline or locked-down tenant must
    re-point ``referenceLayerUrl`` -- the caller warns about exactly that.
    """
    controls = {
        "showStylePicker": {"expr": {"Literal": {"Value": "false"}}},
        "showNavigationControls": {"expr": {"Literal": {"Value": "false"}}},
        "showSelectionControl": {"expr": {"Literal": {"Value": "false"}}},
        "defaultStyle": {"expr": {"Literal": {
            "Value": _semantic_string_literal(
                _tableau_map_style(ws) or _AZURE_MAP_DEFAULT_STYLE)}}},
    }
    objects = {"mapControls": [{"properties": dict(controls)}]}

    if visual_type == VT_DENSITY_MAP:
        # Tableau's Density mark IS a heat layer, and azureMap has one natively. The bubble layer is
        # switched off so the points do not double-draw over the heat surface.
        objects["heatMapLayer"] = [{"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}}}}]
        objects["bubbleLayer"] = [{"properties": {
            "show": {"expr": {"Literal": {"Value": "false"}}}}}]
        return objects

    if visual_type == VT_MAP or shading_field is None:
        # A symbol/bubble map keeps the bubble layer and gets NO reference layer: its geography is
        # the point itself, not an area, so a polygon overlay would be an invention.
        return objects

    if _azure_map_state_grain(ws) is None:
        # Only a US-state grain has a verified boundary file whose feature ``name`` matches Tableau's
        # state captions. A coarser or non-US geography gets the basemap + bubbles rather than a
        # polygon layer keyed on names we have not proven line up (fail-closed).
        return objects

    objects["mapControls"][0]["properties"]["autoZoomIncludesReferenceLayer"] = {
        "expr": {"Literal": {"Value": "true"}}}
    objects["bubbleLayer"] = [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}]
    objects["referenceLayer"] = [
        {"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "datasourceType": {"expr": {"Literal": {"Value": "'url'"}}},
            "referenceLayerUrl": {"expr": {"Literal": {
                "Value": _semantic_string_literal(_AZURE_MAP_US_STATES_GEOJSON)}}},
            "unmappedObjectVisibility": {"expr": {"Literal": {"Value": "false"}}},
            "polygonStrokeColor": {"solid": {"color": {"expr": {"Literal": {
                "Value": _semantic_string_literal(_AZURE_MAP_POLYGON_STROKE)}}}}},
            "polygonStrokeWidth": {"expr": {"Literal": {"Value": "1D"}}},
        }},
        {"properties": {
            "polygonFillColor": {"solid": {"color": {"expr": {"FillRule": {
                "Input": shading_field,
                "FillRule": _azure_map_fill_rule(),
            }}}}},
        },
         "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}},
    ]
    return objects


def _azure_map_state_grain(ws):
    """The state-grain geo field for an azureMap choropleth, or ``None``.

    Same granularity test the shapeMap path used: the reference boundary file IS US states, and its
    feature ``name`` property is the full state name, which is what Tableau's state captions carry.
    """
    geo_levels = [g for g in (ws["encodings"].get("geo_levels") or [])
                  if g.get("kind") == "category"]
    finest = (max(geo_levels, key=lambda g: _geo_rank(g.get("geo_area")))
              if geo_levels else ws["encodings"].get("detail"))
    area = finest.get("geo_area") if finest else None
    return finest if _geo_rank(area) == _GEO_GRANULARITY["state"] else None


def _azure_map_fill_rule():
    """The diverging ``linearGradient3`` an azureMap choropleth shades with.

    Same palette and the same value-anchored ``mid`` the shapeMap path used -- orange (loss) ->
    white (break-even, pinned to 0) -> blue (high) -- because Power BI otherwise auto-centres on the
    DATA midpoint and paints break-even states orange on a mostly-positive measure. A fillRule stop
    is an UNWRAPPED literal; the ``{"expr": ...}`` wrapper makes the stop fail to load and the
    choropleth reverts to a flat fill.
    """
    return {
        "linearGradient3": {
            "min": {"color": {"Literal": {
                "Value": _semantic_string_literal(_SHAPE_MAP_DIVERGING_MIN)}}},
            "mid": {"color": {"Literal": {
                "Value": _semantic_string_literal(_SHAPE_MAP_DIVERGING_MID)}},
                "value": {"Literal": {"Value": _SHAPE_MAP_DIVERGING_CENTRE}}},
            "max": {"color": {"Literal": {
                "Value": _semantic_string_literal(_SHAPE_MAP_DIVERGING_MAX)}}},
            "nullColoringStrategy": {"strategy": {"Literal": {"Value": "'asZero'"}}},
        },
    }


def _merge_extra_objects(*blocks):
    """Merge several ``extra_objects`` dicts, later keys winning. ``None`` when all are empty."""
    out = {}
    for block in blocks:
        if block:
            out.update(block)
    return out or None


def _azure_map_objects_for(ws, state, warnings):
    """``extra_objects`` for a map worksheet, or ``None`` for anything that is not a map.

    Pulls the shading measure's field expression straight out of the emitted ``Tooltips`` projection
    -- that is the SAME expression the referenceLayer's ``FillRule.Input`` must name, so reading it
    back from the query state guarantees the two agree rather than rebuilding it and hoping.
    """
    vt = ws.get("visual_type")
    if vt not in (VT_SHAPE_MAP, VT_FILLED_MAP, VT_MAP, VT_DENSITY_MAP):
        return None
    # ISSUE #111: a Tableau DUAL-AXIS map stacks several mark layers -- e.g. a Multipolygon state
    # choropleth coloured by SUM(Profit) PLUS Pie marks at City LOD sized by SUM(Sales) -- and a
    # single azureMap has ONE Location well and ONE Legend well, so it structurally cannot host two
    # LODs with two colour encodings. The layers beyond the first are therefore DROPPED, and the only
    # thing said about it used to be a note that "categorical mark colours are deferred" -- true, but
    # a palette detail that reads like a nit, so a reader would never guess two of three layers were
    # gone. Named first and explicitly, because it is not repairable downstream.
    _marks = [m for m in (ws.get("pane_marks") or []) if m]
    if len(dict.fromkeys(_marks)) > 1:
        warnings.insert(0, _warn(
            "worksheet", ws["name"],
            "dual-axis map: %d mark layer(s) (%s) were flattened into ONE visual and layers 2..%d "
            "are DROPPED -- a single Power BI map has one Location well and one Legend well, so it "
            "cannot host several LODs with several colour encodings. The faithful rebuild is one "
            "visual PER layer; this is a structural loss, not a styling one"
            % (len(_marks), ", ".join(dict.fromkeys(_marks)), len(_marks))))
    if vt == VT_MAP and (ws.get("mark_class") or "").strip().lower() == "pie":
        warnings.append(_warn(
            "worksheet", ws["name"],
            "pie-on-a-map rebuilt as a BUBBLE map: Power BI has no per-point pie marker, so the "
            "geography and the sizing measure are kept and the per-slice split is lost. Previously "
            "this became a plain pieChart with the geography dropped entirely"))
    shading = None
    for proj in ((state.get("Tooltips") or {}).get("projections") or []):
        if proj.get("field"):
            shading = proj["field"]
            break
    objects = _azure_map_objects(ws, vt, shading)
    # A custom Mapbox basemap is an arbitrary third-party design (roads, palette, labels all
    # author-chosen); no stock Azure style reproduces it, and picking a near-miss would silently
    # misrepresent the source. Say so rather than pretend (#128).
    _raw_style = _tableau_map_style_raw(ws)
    if _raw_style and _raw_style.lower().startswith("mapbox://"):
        warnings.append(_warn(
            "worksheet", ws["name"],
            f"map uses a CUSTOM Mapbox basemap ({_raw_style}) that no stock Power BI map style "
            f"reproduces; the rebuilt map keeps the default basemap -- re-point it at your own "
            f"tile source, or pick the closest built-in style, before trusting its appearance"))
    elif _raw_style and _tableau_map_style(ws) is None:
        warnings.append(_warn(
            "worksheet", ws["name"],
            f"map declares an unrecognised Tableau basemap ({_raw_style}); the rebuilt map keeps "
            f"the default basemap rather than guessing a near-match"))
    if objects and objects.get("referenceLayer"):
        warnings.append(_warn(
            "worksheet", ws["name"],
            "map choropleth shades a PUBLIC GeoJSON boundary file "
            f"({_AZURE_MAP_US_STATES_GEOJSON}) -- an offline or locked-down tenant must re-point "
            "the visual's referenceLayerUrl at its own hosted copy"))
    return objects


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
    Structure verified against a real Desktop-authored ``filledMap``/shapeMap ``visual.json`` whose
    ``linearGradient3`` stops carry both a ``color`` and a value-anchor. A fillRule colour/value/
    strategy stop is an UNWRAPPED literal (``{"Literal": {"Value": ...}}``) -- NOT the ``{"expr":
    {"Literal": ...}}`` wrapper a plain formatting property uses: the ``expr`` wrapper makes the stop
    fail to load (``PBIR_FILLRULE_STOP_DOUBLE_WRAP``) and the choropleth reverts to a flat fill. This
    matches the chart/tableEx gradient builder :func:`_gradient_color_stops` exactly; only
    ``showAllDataPoints`` (an ordinary boolean property, not a fillRule stop) keeps the ``expr`` wrapper.
    """
    return [{
        "properties": {
            "fillRule": {
                "linearGradient3": {
                    "min": {"color": {"Literal": {
                        "Value": _semantic_string_literal(_SHAPE_MAP_DIVERGING_MIN)}}},
                    "mid": {
                        "color": {"Literal": {
                            "Value": _semantic_string_literal(_SHAPE_MAP_DIVERGING_MID)}},
                        "value": {"Literal": {
                            "Value": _SHAPE_MAP_DIVERGING_CENTRE}},
                    },
                    "max": {"color": {"Literal": {
                        "Value": _semantic_string_literal(_SHAPE_MAP_DIVERGING_MAX)}}},
                    "nullColoringStrategy": {
                        "strategy": {"Literal": {"Value": "'asZero'"}}},
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


def _lollipop_objects(ws):
    """Faithful lollipop overlay objects for a dual-axis stick+dot worksheet re-routed to a combo.

    Renders a ``lineClusteredColumnComboChart`` as a lollipop: thin columns (the sticks) via a wide
    ``categoryAxis.innerPadding``; a hidden line with markers on (the dots) via ``lineStyles``; both
    wells sharing ONE scale (``valueAxis`` sharedAxis on, secondary hidden); the legend off. The
    stick/dot colour is the worksheet's own constant mark colour when present (``lollipop_color``,
    read at parse time), else omitted so the theme's first data colour drives it. Property SHAPES are
    the standard PBIR single-quoted semantic-query literals used throughout ``_visual_json`` (each
    object name -> ``[{"properties": {...}}]``).
    """
    def lit(v):
        return {"expr": {"Literal": {"Value": v}}}
    objs = {
        "categoryAxis": [{"properties": {"innerPadding": lit("60L")}}],
        "valueAxis": [{"properties": {
            "secShow": lit("false"), "sharedAxis": lit("true")}}],
        "lineStyles": [{"properties": {
            "strokeShow": lit("false"),
            "showMarker": lit("true"),
            "markerShape": lit("'circle'"),
            "markerSize": lit("6D")}}],
        "legend": [{"properties": {"show": lit("false")}}],
    }
    color = ws.get("lollipop_color")
    if color:
        fill = {"solid": {"color": {"expr": {"Literal": {
            "Value": _semantic_string_literal(color)}}}}}
        objs["dataPoint"] = [{"properties": {"defaultColor": fill}}]
        objs["lineStyles"][0]["properties"]["markerColor"] = fill
    return objs


def _enforce_legend_measure_limit(query_state, vtype, warnings=None, worksheet=None):
    """A cartesian chart may carry a Legend OR several measures -- never both.

    Power BI hard-refuses the combination: the visual renders as a full-tile error, *"There's too many
    columns in the Legend bucket"*, showing NOTHING. It is not a formatting nicety -- it is the
    difference between a chart and a grey box, and it validates clean, so only a render catches it.

    Tableau has no such rule: a sheet can put two measures on Rows and a dimension on Colour at once.
    When it does, the faithful rebuild is the one the source actually LOOKS like -- a series per
    colour member -- so the legend is kept and the extra measures are dropped with a warning. Keeping
    the measures instead would drop the colour split, which is the more visible half, and keeping both
    renders nothing at all.

    A VISUAL CALCULATION IS NOT ONE OF THOSE MEASURES. The rule is about how many measure COLUMNS the
    legend has to cross-join; a Visual Calculation is an expression evaluated INSIDE the visual over a
    projection that is already there, so it adds no column to cross-join. Counting it as one was
    silently fatal: a running total over a colour-split chart projects its base measure HIDDEN and the
    calc VISIBLE, and dropping ``projections[1:]`` deleted exactly the visible half -- leaving a legend
    and a single hidden measure, i.e. an empty chart. Measured on two colour-split running-total sheets
    that both rendered blank while the same calc on a legend-free sheet rendered correctly.

    Mutates ``query_state`` in place and returns the dropped projection names (``[]`` = untouched, so
    every visual without this collision is byte-identical).
    """
    if not query_state or vtype not in _LEGEND_MEASURE_LIMIT_VTYPES:
        return []
    series = (query_state.get("Series") or {}).get("projections") or []
    y_state = query_state.get("Y") or {}
    y_projections = y_state.get("projections") or []
    if not series:
        return []
    calcs = [p for p in y_projections if _is_visual_calculation(p)]
    measures = [p for p in y_projections if not _is_visual_calculation(p)]
    if len(measures) < 2:
        return []
    dropped = [p.get("nativeQueryRef") or p.get("queryRef") or "?" for p in measures[1:]]
    # Keep source order (base measure, then anything computed over it).
    keep = measures[:1] + calcs
    y_state["projections"] = [p for p in y_projections if p in keep]
    if warnings is not None:
        warnings.append(_warn(
            "worksheet", worksheet or "?",
            f"{vtype} carried {len(measures)} measures AND a colour legend -- Power BI renders "
            f"that combination as an error tile, so the legend was kept (one series per colour "
            f"member, matching the source) and {len(dropped)} measure(s) dropped: "
            f"{', '.join(str(d) for d in dropped)}"))
    return dropped


def _fill_rule_input(projection):
    """The ``FillRule.Input`` expression that READS a projection's value.

    A ``FillRule`` needs a REFERENCE to the projected column, not a redeclaration of it. For a model
    measure the projection's own ``field`` already *is* a reference (``Measure`` + ``SourceRef``), so
    it passes through unchanged. For a **Visual Calculation** it is not: ``field`` carries the whole
    ``NativeVisualCalculation`` definition (``Language``/``Expression``/``Name``), and inlining that
    as ``Input`` re-declares the calc inside the colour expression instead of pointing at the column
    the visual already projects. Power BI then applies no fill at all -- the rule is structurally
    valid, ``selector.metadata`` resolves to a real ``queryRef``, PBIR validates clean, and the
    column simply renders with no colour.

    Measured on the ``Network Operational`` corpus workbook: four RANK pills moved from the model
    -measure route to the Visual-Calculation route, and every one of them lost its gradient --
    including the two ``red_green_gold`` diverging scales, which are the most visually load-bearing
    thing on the page. Same palettes, same stops, same selector shape; only ``Input`` differed:

        renders     "Input": {"Measure": {"Expression": {"SourceRef": ...}, "Property": "Rank GMC"}}
        no colour   "Input": {"NativeVisualCalculation": {"Expression": "RANK(SKIP, ORDERBY(...))"}}

    ``SelectRef``/``ExpressionName`` is the reference form for an in-visual expression, and it is
    what this file already emits for the chained table-calc path -- the design note above
    ``_VC_QUERY_REFS`` states the contract outright ("a FillRule ``Input`` references the outer
    calc's queryRef"). This is the one site that was still inlining the field instead.

    Conditional formatting driven BY a Visual Calculation is a first-class Power BI capability, not
    a limitation to route around: it is one of the main reasons to reach for one. So the fix binds
    the reference correctly rather than declining the Visual-Calculation route when a colour rule is
    present.
    """
    if _is_visual_calculation(projection):
        qref = (projection or {}).get("queryRef")
        if qref:
            return {"SelectRef": {"ExpressionName": qref}}
    return (projection or {}).get("field")


def _is_visual_calculation(projection):
    """True when a projection is an in-visual expression rather than a model measure column."""
    field = (projection or {}).get("field") or {}
    return "NativeVisualCalculation" in field or "VisualCalculation" in field


# Cartesian visuals that pair a Legend well with a measure well, where Power BI enforces the
# one-measure-with-a-legend rule. A matrix/table has no Legend well and is unaffected.
_LEGEND_MEASURE_LIMIT_VTYPES = (
    "lineChart", "areaChart", "stackedAreaChart", "columnChart", "clusteredColumnChart",
    "stackedColumnChart", "barChart", "clusteredBarChart", "stackedBarChart",
    "hundredPercentStackedColumnChart", "hundredPercentStackedBarChart",
)


def _suppress_zoom_sliders(visual, vtype, continuous_axis=False):
    """Turn OFF the scrollbar Power BI puts under and beside a busy cartesian chart.

    A Tableau worksheet draws EVERY mark in its pane: the axis compresses until they all fit, and
    there is no scrollbar because there is nothing to scroll to. Power BI instead reserves a minimum
    width per category and, when they no longer fit, PAGES the axis behind a grey scrollbar -- so a
    faithful rebuild of a dense time series grows one on every chart AND SHOWS ONLY PART OF THE DATA.
    Measured on a 45-month series in a 3x3 dashboard: 21 months rendered, 24 silently hidden, on
    every one of the nine tiles. Nothing in the source asks for this, so nothing in the source can
    turn it off -- it is a Power BI default the emitter has to actively defeat.

    THREE mechanisms produce it, and turning off fewer than all three leaves it on screen. This was
    established by render, one property at a time:

    * ``categoryAxis.axisType = 'Scalar'`` -- THE decisive one, and only legal when the category
      field is a scalar date/number. A scalar axis is a number line: it has no category slots, so
      there is nothing to page and the scrollbar cannot exist. Gated on ``continuous_axis`` (the
      Tableau pill's own ``:qk`` continuity) because a genuinely DISCRETE axis must stay categorical.
    * ``categoryAxis.preferredCategoryWidth = 1D`` -- for the categorical case that remains: the
      default reserves per-category width, so N categories claim N * default pixels and page the
      moment that exceeds the plot. ``1D`` lets a category compress to one pixel, which is Tableau's
      "fit the pane" behaviour.
    * ``zoom`` + ``general.responsive`` -- the zoom slider control itself, and responsive layout,
      which reflows a dense chart BY paging its axis.

    LITERAL TYPES DIFFER and a wrong one validates clean while silently no-opping: the flags are
    UNQUOTED ``false``, the width is the typed double ``1D`` (not ``1``, not ``'1'``), and the axis
    type is a QUOTED string ``'Scalar'``.

    ``setdefault`` throughout, so a mark rule that deliberately set one of these earlier keeps it.
    """
    if vtype not in _ZOOM_SLIDER_VTYPES:
        return
    off = {"expr": {"Literal": {"Value": "false"}}}
    objs = visual.setdefault("objects", {})
    objs.setdefault("zoom", [{"properties": {}}])
    objs["zoom"][0]["properties"].update({
        "show": off, "showOnCategoryAxis": off, "showOnValueAxis": off,
    })
    objs.setdefault("general", [{"properties": {}}])
    objs["general"][0]["properties"].setdefault("responsive", off)
    objs.setdefault("categoryAxis", [{"properties": {}}])
    ca = objs["categoryAxis"][0]["properties"]
    ca.setdefault("preferredCategoryWidth", {"expr": {"Literal": {"Value": "1D"}}})
    if continuous_axis:
        ca.setdefault("axisType", {"expr": {"Literal": {"Value": "'Scalar'"}}})


# Cartesian visuals that carry Power BI's zoom-slider control.
_ZOOM_SLIDER_VTYPES = (
    "lineChart", "areaChart", "stackedAreaChart", "columnChart", "clusteredColumnChart",
    "stackedColumnChart", "barChart", "clusteredBarChart", "stackedBarChart",
    "hundredPercentStackedColumnChart", "hundredPercentStackedBarChart",
    "lineClusteredColumnComboChart", "lineStackedColumnComboChart", "scatterChart",
)


def _visual_json(name, vtype, position, query_state, sort_definition=None,
                 filter_config=None, title=None, title_style=None, axis_titles=None,
                 axis_hidden=None,
                 value_objects=None,
                 data_point_objects=None, label_objects=None, legend_objects=None,
                 shape_objects=None, card_label_objects=None, analytics_objects=None,
                 slicer_mode=None, font_objects=None, extra_objects=None,
                 show_title=None, container_fill=None, continuous_axis=False,
                 lipstick_overlap=False, lipstick_series_colors=None,
                 lipstick_series_transparency=None):
    # Power BI refuses a Legend alongside several measures and renders an error tile instead of a
    # chart. Enforced HERE so every emit path is covered by construction rather than by each
    # caller remembering.
    _enforce_legend_measure_limit(query_state, vtype)
    visual = {"visualType": vtype}
    if query_state:
        visual["query"] = {"queryState": query_state}
        if sort_definition:
            visual["query"]["sortDefinition"] = sort_definition
    visual["drillFilterOtherVisuals"] = True
    # Author-overridden axis-title captions AND author-hidden axes (Tier-1 structural): the
    # data-plane ``visual.objects.categoryAxis`` / ``valueAxis`` entries. Shape verified against
    # multiple real MS PBIR visual.json files + the PBIR enumerations reference (``titleText`` =
    # single-quoted semantic-query literal; ``showAxisTitle`` / ``show`` = quoted boolean). An axis
    # is hidden ONLY where the Tableau author turned its header off (see _parse_hidden_axes).
    if axis_titles or axis_hidden:
        axis_objects = _axis_objects(axis_titles, axis_hidden)
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
    if data_point_objects and vtype not in _NO_DATA_POINT_TYPES:
        visual.setdefault("objects", {})["dataPoint"] = data_point_objects
    # Tableau's overlapping-bar ("lipstick") idiom -> the ``layout`` overlap card + a front-series
    # ``dataPoint.fillTransparency``. Applied AFTER the dataPoint assignment above and APPENDING to
    # it, because that line assigns rather than merges: emitting the transparency first would have
    # it silently overwritten by any mark/measure palette, and the loss would be invisible (the
    # visual still validates and still renders -- just with the back series hidden, which is the
    # exact defect this rebuild exists to prevent).
    if lipstick_overlap:
        _lip = _lipstick_overlap_objects(query_state, vtype, lipstick_series_colors,
                                        lipstick_series_transparency)
        if _lip:
            _objs = visual.setdefault("objects", {})
            _objs["layout"] = _lip["layout"]
            # ``dataPoint`` is absent when the source set neither a per-pane colour nor a
            # per-pane transparency -- the overlap card alone is the whole rebuild then.
            if _lip.get("dataPoint") and vtype not in _NO_DATA_POINT_TYPES:
                _objs.setdefault("dataPoint", []).extend(_lip["dataPoint"])
    # Data labels (Tableau "Show Mark Labels"): the data-plane ``visual.objects.labels`` ``show``
    # toggle, applied uniformly (no selector). Per the Power BI formatting reference, ``labels`` is a
    # visual-wide object; only show/hide is set here (label detail styling is Tier-2).
    #
    # ``labels`` is NOT universal, so the object name is chosen by what the target visual actually
    # installs. Surveyed the installed capabilities of every type we emit: all the cartesian /
    # pie / treemap / funnel / combo families expose ``labels``; ``scatterChart`` is the lone chart
    # that does NOT -- its point labels are ``categoryLabels`` -- and ``pivotTable`` / ``tableEx``
    # expose neither (a grid has no mark labels at all). Emitting ``labels`` on a scatter is
    # rejected as PBIR_FORMATTING_OBJECT_UNKNOWN and the toggle is silently lost (issue #100).
    if label_objects:
        _labels_key = _DATA_LABEL_OBJECT.get(vtype, "labels")
        if _labels_key:
            visual.setdefault("objects", {})[_labels_key] = label_objects
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
    # Card value display units (fidelity): force the big-number display units to None so a card /
    # multiRowCard shows the value in full (2,747) instead of Power BI's abbreviating Auto ("3K").
    # A no-op for every non-card visual (see ``_apply_card_display_units``).
    _apply_card_display_units(visual, vtype)
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
    # The "Rows" role is OVERLOADED: on a chart it is the small-multiples paning dimension, but on a
    # pivotTable/matrix it is the ROW HEADERS -- and a matrix installs no smallMultiplesLayout object
    # at all. Keying the layout card on the role alone therefore leaked it onto every matrix
    # (PBIR_FORMATTING_OBJECT_UNKNOWN). Gate on the visual TYPE actually installing the object.
    if (query_state and "Rows" in query_state
            and vtype in _SMALL_MULTIPLES_TYPES):
        visual.setdefault("objects", {})["smallMultiplesLayout"] = [{
            "properties": {
                "layoutType": {"expr": {"Literal": {"Value": "'auto'"}}},
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
    if show_title is False:
        # The author unticked "Show Title" on this zone. Emitting no title object would NOT honour
        # that: Power BI's own default is a shown, auto-generated field-name caption ("Sum of
        # Quantity by Name"), so silence here renders a title Tableau does not show. Say hidden
        # explicitly. Suppressing the title also hands its band back to the plot area, which is why
        # honouring the flag improves the zone's usable geometry rather than only its chrome.
        visual["visualContainerObjects"] = {
            "title": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
            "subTitle": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
        }
    elif title:
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
    else:
        # Neither an explicit hide NOR a resolvable caption. Leaving the container silent here is
        # the one outcome that is never faithful: Power BI's absent-value default is a SHOWN,
        # auto-generated field-name caption ("Sum of Quantity by Name"), so silence renders a title
        # the source never authored -- and, when a caption textbox was already emitted for the zone,
        # renders it twice. There is no title text to state, so state the only other thing that is
        # true: no title. This makes the container's title state total -- every visual this function
        # builds now says show=true|false, never nothing -- so a future path that forgets to pass
        # ``show_title`` degrades to "no title" instead of silently inheriting Power BI's caption.
        visual["visualContainerObjects"] = {
            "title": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
            "subTitle": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
        }
    # The worksheet's own canvas colour -> this container's background. Merged with ``setdefault``
    # AFTER the title block above, which assigns ``visualContainerObjects`` wholesale, so it composes
    # instead of being clobbered by whichever title branch ran.
    #
    # Two shape details, both of which fail SILENTLY (the file validates and the colour just does not
    # appear), verified against the adjudicated rebuild of the corpus's own dark workbook:
    #   * this is a visualCONTAINERObject, not a data-plane ``visual.objects`` entry -- the corpus
    #     records a whole run lost to putting a container property under ``objects``;
    #   * it needs an explicit ``show: true`` (UNQUOTED bool). A colour with no ``show`` is not shown.
    if container_fill:
        visual.setdefault("visualContainerObjects", {})["background"] = [{"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "color": {"solid": {"color": {"expr": {"Literal": {
                "Value": f"'{container_fill}'"}}}}},
            "transparency": {"expr": {"Literal": {"Value": "0D"}}},
        }}]
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
            _target = visual["objects"][_fk][0]["properties"]
            # The cascade is a DEFAULT: it must not overwrite a property an earlier pass already
            # set on this entry. Entry 0 may be a conditional-format rule bound to one column, and a
            # blind ``update`` replaces that rule's own ``fontColor`` with the flat cascade colour --
            # silently, and only for whichever rule happens to sort first. Measured on a merged
            # container band: the leading column's quartile colouring was replaced by plain black
            # while the other two columns painted correctly, which reads as "one column didn't work"
            # rather than as a formatting collision. Keys the entry does NOT set are still applied,
            # so a gradient entry keeps taking the cascade's font face exactly as before.
            for _pk, _pv in _fv[0]["properties"].items():
                _target.setdefault(_pk, _pv)
    # Faithful-mark overlay objects (Tier-1): a ready ``{object_name: [{"properties": {...}}]}`` block
    # a specific mark rule pre-built (e.g. the lollipop's categoryAxis/valueAxis/lineStyles/dataPoint/
    # legend). Merged LAST so it composes with anything an earlier pass added: when the object already
    # exists (e.g. an axis-title categoryAxis) its ``properties`` are updated in place rather than
    # clobbered; otherwise the whole entry is set. ``None`` everywhere except the rules that opt in, so
    # every other visual stays byte-identical.
    if extra_objects:
        for _xk, _xv in extra_objects.items():
            existing = visual.setdefault("objects", {}).get(_xk)
            if (isinstance(existing, list) and existing
                    and isinstance(existing[0], dict) and "properties" in existing[0]
                    and isinstance(_xv, list) and _xv
                    and isinstance(_xv[0], dict) and "properties" in _xv[0]):
                existing[0]["properties"].update(_xv[0]["properties"])
            else:
                visual["objects"][_xk] = _xv
    # Power BI shows a zoom slider (a scrollbar) by default on a busy cartesian axis; Tableau has
    # no such control, so a faithful rebuild turns it off. Applied LAST so it composes with every
    # objects block assembled above.
    _suppress_zoom_sliders(visual, vtype, continuous_axis=continuous_axis)
    # Small-multiples visuals need a newer schema (see SCHEMA_VISUAL_SM): Desktop drops a
    # SmallMultiple role on the legacy 1.0.0 stamp. The bump is gated to exactly those visuals so the
    # verified non-trellis gates keep their proven 1.0.0 stamp.
    schema = SCHEMA_VISUAL
    if query_state and "Rows" in query_state and vtype in _SMALL_MULTIPLES_TYPES:
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


# Zone Geometry v2 slice 5 -- emit-boundary sentinel / mojibake scrub. Tableau reuses the Latin
# letter 'Æ' (U+00C6) as a soft/hard line-break sentinel inside formatted-text runs, and can leave a
# U+FFFD replacement char where a source byte failed to decode. Several parse helpers strip these at
# their own site, but the resolver paths (dynamic caption / title, which preserve a real newline)
# carry a bare sentinel through -- so a two-line caption emitted "New Inbound<Æ>" / "Referrals". This
# is the FINAL net at the emit boundary: every string bound for a Literal (via
# ``_semantic_string_literal``) and every textbox run value passes through it. Crucially 'Æ' is ALSO a
# legitimate letter (Danish/Norwegian "Ærø"), so it is scrubbed ONLY where it marks a break -- right
# before a newline, or at the very end of the text -- never mid-word. U+FFFD is never legitimate and
# is always dropped.
_SENTINEL_BREAK_RE = re.compile(r"\u00c6(?=[\r\n])|\u00c6\Z")
_REPLACEMENT_CHAR = "\ufffd"


def _scrub_sentinels(text):
    """Drop Tableau's line-break sentinel (``Æ``) where it marks a break and any ``U+FFFD`` mojibake
    from a string bound for the report. Non-string or already-clean input is returned unchanged via a
    fast no-op path, so this is safe to call on every emitted value (colours, hex, geo blobs included).
    """
    if not isinstance(text, str) or ("\u00c6" not in text and _REPLACEMENT_CHAR not in text):
        return text
    return _SENTINEL_BREAK_RE.sub("", text).replace(_REPLACEMENT_CHAR, "")


def _semantic_string_literal(value):
    """A Power BI semantic-query string literal: embedded single quotes, inner apostrophe doubled
    (``O'Brien`` -> ``'O''Brien'``). Any Tableau line-break sentinel / mojibake is scrubbed first
    (see :func:`_scrub_sentinels`) so no stray ``Æ`` / ``U+FFFD`` ever reaches an emitted Literal."""
    return "'" + _scrub_sentinels(str(value)).replace("'", "''") + "'"


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
    """Build the matrix/table per-channel format objects from the worksheet's resolved grid styles,
    each carrying resolved font props and/or a backColor plate. Only the grid family; None for
    anything else so other visuals stay unchanged.

    TYPE-AWARE object names (a flat table and a matrix expose DIFFERENT formatting objects):
    a ``pivotTable`` (matrix) has ``rowHeaders`` + per-group ``subTotals`` (both valid there), while a
    flat ``tableEx`` has NEITHER -- it exposes only ``columnHeaders`` / ``values`` / ``total``.
    Emitting ``rowHeaders`` / ``subTotals`` on a ``tableEx`` trips ``PBIR_FORMATTING_OBJECT_UNKNOWN``
    and Power BI silently drops the styling, so on a table the total-row style maps to the ``total``
    object (same font/fill props) and no ``rowHeaders`` object is emitted."""
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
    _put("values", gs.get("body"), gs.get("body_fill"))
    if ws.get("visual_type") == VT_MATRIX:
        _put("rowHeaders", gs.get("header"), gs.get("header_fill"))
        _put("subTotals", gs.get("total"), gs.get("subtotal_fill"))
    else:
        _put("total", gs.get("total"), gs.get("subtotal_fill"))
        # Grand-total VISIBILITY, emitted in both directions because Power BI's table defaults it ON.
        # Leaving it unset is a decision, not a neutral act: measured across the corpus of 34, 252 of
        # 262 Tableau shelves declare NO grand total, yet all 42 emitted grid visuals inherited the
        # default and showed one. An extra row of plausible numbers is harder to notice than a
        # missing feature, which is what makes it worth being explicit about.
        #
        # ``tableEx`` exposes exactly one control here -- ``total.totals`` (bool) -- confirmed
        # against the visual-type schema rather than assumed. It has NO position property, so
        # Tableau's ``onTop`` cannot be honoured on a table; the caller discloses that instead of
        # silently placing the row at the bottom and calling it done.
        st = ws.get("shelf_totals") or {}
        fo.setdefault("total", [{"properties": {}}])
        fo["total"][0]["properties"]["totals"] = _bool_literal(bool(st.get("rows")))
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


def _semantic_datetime_literal(value):
    """A semantic-query datetime literal from a Tableau ``#...#`` date/datetime literal, else ``None``.

    PBIR slicer preselection proved valid only when expressed as a semantic-query literal
    (``datetime'2020-01-01T00:00:00'``), not as DAX ``DATE(...)``. The workbook already stores a
    Tableau date parameter's current value in this exact ``#YYYY-MM-DD#`` / ``#YYYY-MM-DD HH:MM:SS#``
    form, so parse only those documented shapes and fail closed on anything else.
    """
    s = (value or "").strip()
    m = re.match(
        r"^#(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2}):(\d{1,2}))?#$",
        s)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4) or 0)
    mm = int(m.group(5) or 0)
    ss = int(m.group(6) or 0)
    try:
        dt = datetime(year, month, day, hh, mm, ss)
    except ValueError:
        return None
    return "datetime'%s'" % dt.strftime("%Y-%m-%dT%H:%M:%S")


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


def _semantic_boolean_literal(value):
    """A PBIR semantic-query BOOLEAN literal (``true``/``false``), or ``None`` when not boolean-ish.

    Unquoted and lower-case: a boolean column compared against the STRING ``'true'`` matches no row,
    which is the silent-wrong-data shape rather than an error. Tableau writes an applied boolean
    filter's members as ``true``/``false`` text, so the conversion is here rather than at the caller.
    """
    s = str(value).strip().lower()
    if s in ("true", "1", "yes"):
        return "true"
    if s in ("false", "0", "no"):
        return "false"
    return None


def _categorical_condition(entity, prop, values, *, exclude, numeric=False, datey=False,
                           boolean=False):
    # ``numeric=True`` emits integer literals (``4L``) instead of quoted strings -- used when an
    # applied date-part selection (month ``4``, year ``2021``) has been rebound onto an INTEGER
    # calendar column (Date[Month]/[Year]/...), where a string literal would match no row.
    # ``datey=True`` emits semantic-query ``datetime'...'`` literals for an applied selection on a
    # real DATE column, where a quoted string likewise matches no row.
    # ``boolean=True`` emits bare ``true``/``false`` for a boolean column -- same reasoning: a
    # boolean compared against the string ``'true'`` matches nothing and reports no error.
    if datey:
        lit = _semantic_datetime_literal
    elif numeric:
        lit = _semantic_numeric_literal
    elif boolean:
        lit = _semantic_boolean_literal
    else:
        lit = _semantic_string_literal
    col = _filter_column_ref(entity, prop, source=_FILTER_SOURCE_ALIAS)
    in_expr = {"In": {
        "Expressions": [col],
        "Values": [[{"Literal": {"Value": lit(v)}}] for v in values],
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


def _inherit_flag_filters(visuals, flag_fc):
    """Stamp a worksheet's keep-flag ``filterConfig`` onto every visual DERIVED from that worksheet.

    One Tableau worksheet often rebuilds as SEVERAL Power BI visuals -- a KPI headline card above its
    sparkline, or a measure-trellis fanned into one chart per measure. Those derived pieces answer the
    same question as the worksheet they came from, so they must inherit its filters: Tableau applies a
    worksheet filter to the whole worksheet, not to one mark layer of it. Leaving a piece unfiltered
    silently widens it to the entire table, which is why an unfiltered KPI card read 1354 against the
    source's date-ranged 820 -- a WRONG NUMBER, shown confidently, with no warning anywhere.

    Applied to the split emitters' output rather than inside each one, so a future split path inherits
    correctly by construction instead of having to remember. Never overwrites a ``filterConfig`` a
    visual already carries (a piece with its own narrower scope keeps it), and is a no-op when the
    worksheet has no flags -- byte-for-byte the prior output in that case.
    """
    if not flag_fc:
        return visuals
    for v in visuals or ():
        if isinstance(v, dict) and not v.get("filterConfig"):
            fc = copy.deepcopy(flag_fc)
            # A filter's ``name`` must be unique REPORT-WIDE, not merely within its visual. Stamping
            # the same worksheet filterConfig onto several derived visuals copies the name with it,
            # which trips PBIR_FILTER_NAME_DUPLICATE_GLOBAL -- and that is emitted as a WARNING, so a
            # gate that reads ``errorCount`` alone sails past it (flagged as encoding detail #1 in
            # #130, and confirmed live on 0088_salesforce_nonprofit_case_mgmt).
            #
            # Suffixing with the visual's own name keeps it deterministic and readable, and the
            # visual name is already unique per report by construction.
            vname = v.get("name")
            if vname:
                for f in (fc.get("filters") or ()):
                    if isinstance(f, dict) and f.get("name"):
                        f["name"] = _sanitize("%s-%s" % (f["name"], vname))
            v["filterConfig"] = fc
    return visuals


def _surfaced_filter_keys(ws_list, db):
    """``(entity, property)`` of every filter the dashboard exposes as an interactive card.

    A filter the author SURFACED is a control the reader can change, so its restriction belongs on
    the slicer (as an open-on selection) and must NOT also be baked into the visuals -- baking it
    would pin the numbers while the slicer appeared to move. A filter the author did NOT surface is
    a fixed property of the worksheet and has to be applied to the visual instead. Matching on the
    resolved model column rather than the raw token mirrors the de-duplication
    :func:`_emit_dashboard_slicers` already does, so a field carded once but filtered under several
    per-sheet tokens still counts as surfaced.
    """
    by_token = _filter_fields_by_token(ws_list)
    keys = set()
    for tok in (db.get("filter_field_tokens") or ()):
        f = by_token.get(tuple(tok))
        if f and f.get("entity") and f.get("property"):
            keys.add((f["entity"], f["property"]))
    return keys


def _applied_filter_config_for(ws, surfaced_keys, model_table, field_map, warnings):
    """A worksheet's UNSURFACED applied filters as a visual-level ``filterConfig``, else ``None``.

    Tableau applies a worksheet filter to that worksheet whether or not a quick-filter card is
    shown for it. Only the shown ones were ever rebuilt (as slicers), so an applied restriction with
    no card -- ``Status = Closed``, ``Record Type = <id>``, ``Stage = Waitlisted`` -- was dropped
    entirely and the visual silently widened to the whole table. That is a WRONG NUMBER shown
    confidently, the same failure class as the unfiltered KPI card that read 1354 against a
    date-ranged 820, and it is why several rebuilt sheets disagreed with the source workbook.

    On a CHART, ``filterConfig`` is the real data filter (unlike on a slicer, where it only limits
    the offered member list) -- so this is the correct slot, and it composes with the keep-flag
    containers through the same ``filterConfig.filters[]`` array.

    Binding is delegated to :func:`_slicer_filter_config`, so exactly the shapes already proven
    bindable are applied here and everything else keeps its existing fidelity warning. Filters the
    dashboard surfaces are skipped: those are the reader's to change.
    """
    containers, seen = [], set()
    for i, f in enumerate(ws.get("filters") or []):
        if not (f.get("selection") or f.get("range")):
            continue
        entity, prop, binding = _apply_override(f, model_table, field_map)
        if binding != "column" or (entity, prop) in surfaced_keys:
            continue
        if (entity, prop) in seen:
            continue
        if not _applied_selection_is_bindable(f):
            warnings.append(_warn(
                "filter", f.get("caption") or prop,
                "applied worksheet filter not carried onto the visual (a %s selection has no "
                "faithful literal against the rebuilt column)" % (f.get("datatype") or "typed")))
            continue
        fc = _slicer_filter_config(
            f, model_table, field_map,
            _sanitize(f"wsfilter-{ws.get('name')}-{i}-{prop}"), warnings)
        if fc:
            seen.add((entity, prop))
            containers.extend(fc["filters"])
    return {"filters": containers} if containers else None


def _applied_selection_is_bindable(f):
    """Whether an applied selection has a literal that is type-safe against the rebuilt column.

    On a SLICER a mistyped ``filterConfig`` merely mis-limits the offered member list. On a CHART
    the same container is the real data filter, and Power BI rejects the whole visual with
    "Something's wrong with one or more filters" -- an error tile where there used to be a number.
    So this path has to be stricter than the slicer one, and it fails CLOSED: an uncarried filter
    leaves the previous (already-shipped) behaviour, while a mistyped one destroys the visual.

    Tableau serialises a boolean member as the STRING ``"true"``/``"false"``, but the rebuilt
    column is whatever the translated calculation produced -- ``int64`` for both boolean LOD flags
    in the corpus, one of which is a ``BLANK()`` stub no literal can ever match. There is no value
    we could emit that is right by construction, and guessing ``1``/``TRUE`` is exactly the kind of
    unverifiable assumption that renders as an error tile. Ranges are left to
    :func:`_slicer_filter_config`, which already types them itself.
    """
    sel = f.get("selection")
    if not sel:
        return True
    if bool(f.get("date_rebound")) and (f.get("property") in _INTEGER_DATE_PART_COLUMNS):
        return True
    return (f.get("datatype") or "").lower() == "string"


def _merge_filter_configs(*configs):
    """Combine several ``{"filters": [...]}`` into one, dropping ``None`` and duplicate names."""
    out, seen = [], set()
    for cfg in configs:
        for cont in ((cfg or {}).get("filters") or ()):
            nm = cont.get("name")
            if nm in seen:
                continue
            seen.add(nm)
            out.append(cont)
    return {"filters": out} if out else None


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
        # A date-part selection (month "4", year "2021") rebound onto an INTEGER calendar column
        # (Date[Month]/[Year]/[Quarter]/[Day]) binds faithfully as an integer categorical filter --
        # the member value equals the DAX part function output verbatim -- so the report opens on the
        # SAME filtered years/months instead of dropping the selection to "show all". Tightly gated:
        # the field must have been rebound by the date machinery, land on one of the four exact
        # integer part columns, and carry only clean-integer members. Everything else (string parts
        # like Day Name, Week numbering, non-integer members) falls through to the existing paths.
        rebound_int_values = [v for v in sel["values"] if v != "%null%"]
        if (field.get("date_rebound") and prop in _INTEGER_DATE_PART_COLUMNS
                and rebound_int_values
                and all(_semantic_numeric_literal(v) is not None
                        and _semantic_numeric_literal(v).endswith("L")
                        for v in rebound_int_values)):
            cond = _categorical_condition(entity, prop, rebound_int_values,
                                          exclude=(sel["mode"] == "exclude"), numeric=True)
            return {"filters": [_filter_container(
                entity, prop, cond, name, ftype="Categorical",
                inverted=(sel["mode"] == "exclude"))]}
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


# -- Tableau PARAMETER-control show mode -> Power BI slicer mode ---------------
# A dashboard parameter control is authored with its own ``mode``, a DIFFERENT vocabulary from a
# filter card: ``compact`` is the collapsed DROPDOWN (Tableau's default single-select parameter
# presentation) and ``typein`` / ``typeinlist`` are type-in pickers (still a single-value
# dropdown-family control), whereas ``radio`` is an in-place radio LIST and ``slider`` is a value
# slider (no Power BI slicer equivalent -> the in-place List reads closest). Map dropdown-family
# modes to ``'Dropdown'`` and radio/slider to ``'Basic'``, defaulting to ``'Dropdown'`` -- ``compact``
# is the overwhelmingly common parameter-control style AND matches Tableau's default, so a control
# whose mode we could not read still rebuilds as the compact dropdown a top control band needs
# (rather than Power BI's default vertical List, which reads as a stack of buttons). The Power BI
# mode names are unprotectable PBIR-schema interop facts.
_LIST_PARAM_MODES = frozenset({"radio", "radiolist", "slider", "checklist"})


def _tableau_param_control_mode_to_pbi(mode):
    m = (mode or "").strip().lower()
    if m in _LIST_PARAM_MODES:
        return "Basic"
    return "Dropdown"


def _slicer_font_props(style):
    """Like :func:`_font_style_props` but for a slicer ``header`` / ``items`` object, whose *size*
    channel is ``textSize`` -- NOT the ``fontSize`` every title/axis/grid channel uses (verified
    against ``formatting describe-object slicer header``/``items`` -> the only size key is
    ``textSize``; a ``fontSize`` there is rejected as ``PBIR_FORMATTING_PROP_UNKNOWN`` and the header
    caption clips). Every other key (``fontColor``/``bold``/``fontFamily``) is identical, so we reuse
    the shared builder and rename the one divergent key rather than duplicating the block.
    """
    props = _font_style_props(style)
    if "fontSize" in props:
        props["textSize"] = props.pop("fontSize")
    return props


def _apply_slicer_format(visual, hdr_style=None, itm_style=None, plate_fill=None, header_text=None):
    """Stamp the resolved Tableau quick-filter style onto an already-built slicer visual.

    ``hdr_style`` / ``itm_style`` are resolved font dicts (family/size/weight/color) for the slicer
    header (the filter caption) and the item list; each maps to a PBIR ``objects.header`` /
    ``objects.items`` font block via :func:`_font_style_props`. ``plate_fill`` is the resolved slicer
    background fill -> a ``visualContainerObjects.background`` via :func:`_container_background_props`.
    All three are applied post-build (a slicer NEVER routes its format through ``_visual_json``'s
    ``font_objects`` channel-merge) with ``setdefault(...).update(...)`` so an existing header/items
    object or plate is composed with, not clobbered. A falsy arg is skipped -> that face keeps its
    Power BI default.

    ``header_text`` (optional) overrides the slicer header's *Title text* with the Tableau field
    CAPTION (the filter card's authored title). Without it Power BI shows the bound model column's
    raw name (``pmdm__ProgramIssueArea__c`` / ``Name``) instead of the faithful ``Program Issue
    Area`` / ``Program Name`` the source dashboard displays. The header ``text`` property is the
    slicer ``header`` object's authoritative title-text channel (``formatting describe-object slicer
    header`` -> ``text``: "Title text"); emitted as the same single-quoted semantic string literal
    every other text property uses, alongside ``show:true`` so the retitled header is guaranteed
    visible.
    """
    if header_text:
        hdr = visual.setdefault("objects", {}).setdefault("header", [{"properties": {}}])[0]["properties"]
        hdr["show"] = {"expr": {"Literal": {"Value": "true"}}}
        hdr["text"] = {"expr": {"Literal": {"Value": _semantic_string_literal(str(header_text))}}}
    if hdr_style:
        visual.setdefault("objects", {}).setdefault(
            "header", [{"properties": {}}])[0]["properties"].update(_slicer_font_props(hdr_style))
    if itm_style:
        visual.setdefault("objects", {}).setdefault(
            "items", [{"properties": {}}])[0]["properties"].update(_slicer_font_props(itm_style))
    cont = _container_background_props(plate_fill) if plate_fill else None
    if cont:
        visual.setdefault("visualContainerObjects", {}).setdefault(
            "background", [{"properties": {}}])[0]["properties"].update(cont)


def _slicer_preselection_object(field, model_table, field_map):
    """The slicer's OPEN-ON selection (``objects.general[].properties.filter``), else ``None``.

    PBIR keeps two things apart that are easy to conflate, and conflating them is a silent
    CORRECTNESS bug rather than a cosmetic one:

      * ``filterConfig.filters[]`` constrains the data feeding the slicer -- which members it
        OFFERS, and nothing else;
      * ``objects.general[].properties.filter`` (note the doubly-nested ``filter.filter``) is what
        the slicer opens SELECTED on, and it is the ONLY one of the two that propagates to the
        rest of the page.

    An applied Tableau filter selection expressed only as the first left the slicer reading "All"
    AND every other visual unfiltered, so the report showed numbers over rows the source workbook
    had excluded. Rendering an isolated slicer with the selection and NO ``filterConfig`` produces
    exactly Tableau's quick-filter shape -- the full member list offered, the authored members
    checked, and the page genuinely filtered -- which is why :func:`_slicer_json` drops
    ``filterConfig`` whenever this returns a selection.

    Two ways in, both fail-closed:

      * ``preselect_override`` -- select through a DIFFERENT column than the projected one. A field
        parameter is projected on its visible display column but is only selectable through its
        hidden group-by column (rendering the same slicer four ways -- display / group-by /
        composite / order -- left group-by as the only variant that opened on the authored value).
      * an applied ``selection``, gated exactly like :func:`_slicer_filter_config` so the two never
        disagree about what is faithfully bindable.

    EXCLUDE mode declines. On screen a ``Not(In(...))`` selection is indistinguishable from no
    selection at all, so "it rendered" could not tell a working exclusion from a silently dropped
    one; every real exclude found in the corpus is either ``%null%``-only (already reduced to
    nothing here) or a compound/derived set this layer does not bind. Declining keeps today's
    ``filterConfig`` behaviour, which is strictly better than an unverifiable guess.
    """
    field = field or {}
    explicit = field.get("preselect_object")
    if isinstance(explicit, dict):
        return explicit
    override = field.get("preselect_override")
    sel = field.get("selection")
    if not override and not sel:
        return None
    entity, prop, binding = _apply_override(field, model_table, field_map)
    if binding != "column":
        return None
    if override:
        col = override.get("property")
        values = [v for v in (override.get("values") or []) if v not in (None, "")]
        if not col or not values:
            return None
        cond = _categorical_condition(entity, col, values, exclude=False)
    else:
        if sel.get("mode") == "exclude":
            return None
        dt = (field.get("datatype") or "").lower()
        numeric = bool(field.get("date_rebound") and prop in _INTEGER_DATE_PART_COLUMNS)
        # A DATE column carrying explicit date members is bindable too. Tableau writes an applied
        # date filter as ``#YYYY-MM-DD#`` literals, and PBIR accepts those as semantic-query
        # ``datetime'...'`` literals -- the exact form the scalar date PARAMETER pickers already
        # open on. Without this a date-scoped worksheet rebuilt with its slicer reading "All" and
        # NOTHING filtered, so every number on the page was computed over the full history the
        # source workbook had excluded: measured on a real workbook as a whole ranked table drifting
        # off its oracle because one month filter never applied. Fail-closed as ever -- every member
        # must parse as a date literal, or the selection declines rather than binding a partial set.
        datey = (not numeric and dt in ("date", "datetime")
                 and all(_semantic_datetime_literal(v) is not None
                         for v in sel.get("values") or [] if v != "%null%"))
        # A BOOLEAN column is bindable too, and must be, because it is the residual case that still
        # fell through to ``filterConfig`` after 2.51.0 moved every other selection to the open-on
        # object. Measured on the corpus at 2.148.0: of 52 slicers carrying a default, 50 used the
        # open-on object and the only 2 that did not were boolean DAX calculated columns
        # (``'X'[a] = 'X'[b]``), whose selection therefore pre-selected NOTHING -- the exact "slicer
        # reads All, page unfiltered" symptom reported in #130, surviving in one narrow shape.
        #
        # The literal must be bare ``true``/``false``: those same two slicers were emitting the
        # STRING ``'true'`` against a boolean column, which matches no row and reports no error.
        booly = (not numeric and not datey and dt in ("boolean", "bool")
                 and all(_semantic_boolean_literal(v) is not None
                         for v in sel.get("values") or [] if v != "%null%"))
        if not numeric and not datey and not booly and dt != "string":
            return None
        values = [v for v in sel["values"] if v != "%null%"]
        if not values:
            return None
        if numeric and not all(
                _semantic_numeric_literal(v) is not None
                and _semantic_numeric_literal(v).endswith("L") for v in values):
            return None
        cond = _categorical_condition(entity, prop, values, exclude=False,
                                      numeric=numeric, datey=datey, boolean=booly)
    return {"properties": {"filter": {"filter": {
        "Version": 2,
        "From": [{"Name": _FILTER_SOURCE_ALIAS, "Entity": entity, "Type": 0}],
        "Where": [{"Condition": cond}],
    }}}}


def _slicer_json(name, field, position, model_table, field_map, *, mode=None, warnings=None):
    # THE floor, applied here so it cannot be missed by a caller. A Power BI DROPDOWN slicer's own
    # chrome costs 76px -- header 28 + selector 32 + padding 8/8 -- and below that the header or the
    # selector is CLIPPED and the control is unusable. It is a validation-invisible rendering bug in
    # the classic sense: the JSON is well-formed, the model is fine, the report opens, and the
    # slicers are simply broken.
    #
    # Enforced at the single point every slicer is built rather than at each layout site, because
    # that is exactly the mistake this fix already made once: the filter-card path had a floor and
    # the PARAMETER-CONTROL path did not, so raising the shared constant fixed the filter slicers
    # and left nine parameter slicers between 44px and 75px still clipped (issue #100). A caller
    # that grows a slicer beyond the floor keeps its own height untouched.
    if position and str(mode or "").lower() == "dropdown":
        try:
            if float(position.get("height") or 0) < SLICER_DROPDOWN_MIN_H:
                position = dict(position)
                position["height"] = SLICER_DROPDOWN_MIN_H
        except (TypeError, ValueError):
            pass
    expr, qref, nref = _field_expression(field, model_table, field_map)
    state = {"Values": {"projections": [
        {"field": expr, "queryRef": qref, "nativeQueryRef": nref}]}}
    # Compute the open-on selection FIRST: it decides whether a ``filterConfig`` is wanted at all.
    # The two are alternatives, never partners. ``filterConfig`` restricts which members the slicer
    # OFFERS without filtering the page, so pairing it with a selection would hand the reader a
    # one-item list they cannot change -- less faithful than Tableau, which shows every member with
    # the authored ones checked. When the selection declines (exclude mode, date ranges, unbindable
    # datatypes) ``filterConfig`` still runs, so those paths -- and their warnings -- are unchanged.
    # A parameter control is a single-value PICKER and never takes a ``filterConfig`` either.
    pre = _slicer_preselection_object(field, model_table, field_map)
    fc = None if (pre or (field or {}).get("preselect_only")) else _slicer_filter_config(
        field, model_table, field_map, name + "-sel",
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
        plate_fill=(field.get("_slicer_plate") if isinstance(field, dict) else None),
        header_text=(field.get("caption") if isinstance(field, dict) else None))
    # Stamp the open-on selection AFTER formatting so neither clobbers the other's ``objects``.
    if pre:
        out["visual"].setdefault("objects", {}).setdefault("general", []).append(pre)
    return out


# ---------------------------------------------------------------------------------------------
# PBIR stacking scheme. Power BI paints overlapping visuals in ascending ``z``. Two constraints
# were established by RENDERING (Desktop 2.157), not by reading the schema:
#
#   * the schema puts no minimum on ``z`` and Desktop honours a negative value for ORDERING -- but
#     it does not PAINT the visual at all. A background plate at ``z=-100`` correctly stopped
#     occluding the charts and simultaneously became invisible, losing the whole authored design.
#     Every layer therefore has to be >= 0;
#   * a Tableau background plate has to sit strictly BELOW worksheet content, so content cannot
#     stay on the natural floor of 0. The whole stack is lifted to leave room underneath it.
#
# The layers are named (rather than spelled as literals at each call site) because two later passes
# SELECT by ``z`` -- the slicer reflow picks out content, the caption tidy picks out captions -- and
# an unnamed renumber silently changes which visuals those passes act on.
_Z_BACKDROP = 0     # background image plate: painted before the first worksheet zone
_Z_CONTENT = 100    # worksheets, tables, cards, tiled text objects
_Z_SLICER = 101     # surfaced parameter / filter controls
_Z_CAPTION = 900    # floating caption textbox
_Z_BANNER = 1000    # dashboard title banner
_Z_OVERLAY = 1100   # decorative overlay image: logo, icon, toggled help panel


def _position(x, y, w, h, z=_Z_CONTENT, tab=0):
    return {"x": round(x, 2), "y": round(y, 2), "z": z,
            "width": round(w, 2), "height": round(h, 2), "tabOrder": tab}


def _image_z(image_zone, first_ws_ord):
    """Which layer a dashboard image belongs on, from Tableau's own paint order.

    Tableau draws dashboard zones in DOCUMENT ORDER, so a zone written earlier sits beneath every
    zone written after it. Authors exploit this in two opposite ways with the SAME ``bitmap`` zone
    type: a full-canvas background PLATE (the pre-rendered card frames / header band / sidebar an
    author designs in Figma and drops behind the sheets) is written FIRST, while a decorative
    OVERLAY -- a corner logo, an icon, a toggled help panel -- is written LAST so it covers content.

    Nothing about the zone itself distinguishes them: both are ``bitmap``, neither is marked
    floating, and size does not decide it either (a help panel can be large, a background can be
    inset). Assuming "image == decoration on top", as a naive rebuild does, silently paints the
    background plate over every chart on the page and produces a dashboard that looks perfect and
    shows no data. Position in the document is the author's actual declaration of intent, so it is
    what we read.

    Falls back to the overlay layer when the dashboard has no worksheet to order against (nothing
    can be occluded, and a lone image is decoration), which keeps every image-only page byte-stable.
    """
    if first_ws_ord is None:
        return _Z_OVERLAY
    ordinal = image_zone.get("paint_ord")
    if ordinal is None:
        return _Z_OVERLAY
    return _Z_BACKDROP if ordinal < first_ws_ord else _Z_OVERLAY


def _zone_pad_inset(zone):
    """The part of a zone's OUTER padding that actually DISPLACES its content, or ``None``.

    Tableau stores a dashboard object's outer padding as ``<zone-style><format attr='margin'>``
    (all sides) plus optional per-side ``margin-top`` / ``-right`` / ``-bottom`` / ``-left``
    overrides, in real pixels. The object is drawn in the CONTENT BOX left after that padding --
    which is why a 133px-tall icon zone carrying ``margin=4`` + ``margin-bottom=85`` renders its
    icon in a 44px band at the TOP of the zone, not floating in the middle of it. Power BI has no
    zone concept: a visual fills its whole rectangle and centres its image, so emitting the raw
    zone rect drops the icon ~46px below where Tableau draws it.

    Only the ASYMMETRIC excess is returned. A uniform inset (including Tableau's documented 4px
    default) shrinks an object evenly and does not move its centre, and the layout model already
    accounts for that separation via its own inter-sibling gap; subtracting it again here would
    shrink every visual on every dashboard for no fidelity gain. Non-uniform padding is different
    in kind -- it is the author positioning content WITHIN its zone, and it is the only part we are
    currently discarding. Returns ``(top, right, bottom, left)`` px, or ``None`` when the padding
    is uniform/absent.
    """
    pad = zone.get("pad") if isinstance(zone, dict) else None
    if not pad:
        return None
    outer = pad.get("outer") or {}
    try:
        vals = [float(outer[s]) for s in _SIDES]
    except (KeyError, TypeError, ValueError):
        return None
    if max(vals) - min(vals) < 1.0:
        return None
    base = min(vals)
    return tuple(v - base for v in vals)


def _apply_zone_padding(rect, zone):
    """Inset ``rect`` by the zone's displacing outer padding. Only ever SHRINKS the rect.

    Because the result is strictly contained in the rect it replaces, this can never introduce an
    overlap the layout engine had already resolved -- the same containment argument that lets the
    slicer inset run after the solve.
    """
    ins = _zone_pad_inset(zone)
    if ins is None:
        return rect
    sx, sy = _ZONE_PAD_SCALE
    top, right, bottom, left = ins
    x, y, w, h = rect
    nx, ny = x + left * sx, y + top * sy
    nw, nh = w - (left + right) * sx, h - (top + bottom) * sy
    if nw < 1.0 or nh < 1.0:
        return rect
    return nx, ny, nw, nh


def _scale_zone(zone, ref_w, ref_h, min_w=40.0, min_h=40.0):
    # THE layout choke point. Under the solver engine a resolved rect for this zone (looked up by the
    # ``zone_id`` slice 4a records at capture time) replaces the naive scale-and-clamp below: the
    # solver already resolved the whole dashboard as a tree, so its rect is disjoint from its tiled
    # siblings and on-page by construction. Applying the min floors here too would re-inflate a box
    # the solver deliberately sized, re-introducing the very overlap the tree solve removed, so a
    # solved rect is taken verbatim. Any zone the plan does not know (or any dashboard whose plan
    # failed to build) falls through to the legacy path unchanged -- fail-closed, never half-solved.
    solved = _solved_rect(zone)
    if solved is not None:
        return _apply_zone_padding(solved, zone)
    pw = _page_w()
    ph = _page_h()
    sx = pw / ref_w if ref_w else 1
    sy = ph / ref_h if ref_h else 1
    x = max(0.0, min(zone["x"] * sx, pw - 1))
    y = max(0.0, min(zone["y"] * sy, ph - 1))
    w = max(min_w, min(zone["w"] * sx, pw - x))
    h = max(min_h, min(zone["h"] * sy, ph - y))
    return _apply_zone_padding((x, y, w, h), zone)


def _solved_rect(zone):
    """The active layout plan's rect for ``zone``, or ``None`` to use the legacy scale."""
    plan = _LAYOUT_PLAN
    if not plan:
        return None
    zid = zone.get("zone_id") if isinstance(zone, dict) else None
    if zid is None:
        return None
    return plan["rects"].get(zid)


# Which formatting object carries a visual's MARK LABELS. ``labels`` for nearly everything, but not
# universally -- surveyed against the installed visual capabilities (``catalog describe <type>``):
#   * ``scatterChart``  -> ``categoryLabels`` (it installs no ``labels`` object at all)
#   * ``pivotTable`` / ``tableEx`` -> NEITHER; a grid has no mark labels, so the toggle is dropped
# Anything absent from this map keeps ``labels``, which every other emitted type does install.
# Visual types that install NO ``dataPoint`` formatting object, so a mark-colour block aimed at one
# is rejected as PBIR_FORMATTING_OBJECT_UNKNOWN and silently discarded. Checked against the installed
# capabilities of every type this emitter produces. The card family colours through
# ``dataLabels``/``categoryLabels`` (already handled by ``_card_label_objects``), a grid has no marks
# to colour, a waterfall colours through its own ``sentimentColors``, and a slicer through ``items``.
_NO_DATA_POINT_TYPES = frozenset({
    "card", "multiRowCard", "pivotTable", "tableEx", "waterfallChart", "slicer",
})


# Visual types whose "Rows" role means SMALL MULTIPLES (and which therefore install the
# ``smallMultiplesLayout`` card). A pivotTable/matrix also has a "Rows" role -- its ROW HEADERS --
# and installs no such object, so the role name alone cannot decide this. Every entry was CHECKED
# against the installed capabilities (``catalog describe <type>``) for BOTH a ``Rows`` role and a
# ``smallMultiplesLayout`` object; ``scatterChart`` and ``waterfallChart`` were in the first draft
# of this list and have NEITHER, so they are deliberately absent.
_SMALL_MULTIPLES_TYPES = frozenset({
    "lineChart", "areaChart", "stackedAreaChart", "columnChart", "barChart",
    "clusteredColumnChart", "clusteredBarChart",
    "hundredPercentStackedColumnChart", "hundredPercentStackedBarChart",
    "lineClusteredColumnComboChart", "lineStackedColumnComboChart",
    "ribbonChart",
})


_DATA_LABEL_OBJECT = {
    "scatterChart": "categoryLabels",
    "pivotTable": None,
    "tableEx": None,
}


def _solid_fill_object(hex_color):
    """One PBIR fill entry: an opaque solid colour.

    Shape verified against 59 real adjudicated ``page.json`` files. Two details are load-bearing and
    both fail SILENTLY when wrong -- the file validates either way and the colour simply does not
    appear:

    * the hex literal is QUOTED (``'#1b1b1b'``), unlike a numeric literal;
    * ``transparency`` is the UNQUOTED typed double ``0D``. Power BI's page background is
      transparent by default, so a colour emitted without it can render as nothing at all.
    """
    return {"properties": {
        "color": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{hex_color}'"}}}}},
        "transparency": {"expr": {"Literal": {"Value": "0D"}}},
    }}


def _page_json(name, display_name, canvas_fill=None):
    page = {
        "$schema": SCHEMA_PAGE,
        "name": name,
        "displayName": display_name,
        "displayOption": "FitToPage",
        "height": _page_h(),
        "width": _page_w(),
    }
    if canvas_fill:
        # BOTH surfaces, in the same colour, because Tableau has only one. ``background`` is the
        # canvas itself; ``outspace`` is the margin Power BI shows around it whenever the viewport
        # aspect differs from the page. Painting only the canvas leaves a dark dashboard sitting in a
        # bright white surround -- which is what the reader actually sees, and reads as broken.
        # Matches the adjudicated rebuild of the corpus's own dark dashboard.
        page["objects"] = {
            "background": [_solid_fill_object(canvas_fill)],
            "outspace": [_solid_fill_object(canvas_fill)],
        }
    return page


# The display name of the placeholder page a page-less report ships so Desktop can open it. Named
# so a reader who opens the project knows immediately that nothing was rebuilt, rather than
# wondering which page is missing.
_EMPTY_REPORT_PAGE_NAME = "No visuals rebuilt"


def _emit_page(parts, page_name, display_name, visuals, canvas_fill=None):
    """Write a page.json plus its visual.json parts; ``visuals`` is a list of dicts."""
    base = f"definition/pages/{page_name}"
    parts[f"{base}/page.json"] = _dumps(_page_json(page_name, display_name, canvas_fill))
    for v in visuals:
        parts[f"{base}/visuals/{v['name']}/visual.json"] = _dumps(v)


def _dumps(obj):
    return json.dumps(obj, indent=2)


# -- dashboard title banner (header band) -------------------------------------
# The banner font size is fixed rather than lifted from the source run: a scaled header band is a
# few dozen px tall, so a single "reasonably large" bold size reads as a header at any page scale
# (the source point size, tuned to Tableau's own banner geometry, does not transfer 1:1).
_BANNER_FONT_SIZE = "18pt"
_BANNER_FONT_PT = 18.0


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
    run = {"value": _scrub_sentinels(banner["text"]),
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
    _fit_textbox_padding(visual, position, _BANNER_FONT_PT)
    return {"$schema": SCHEMA_VISUAL, "name": name, "position": position, "visual": visual}


_TEXT_OBJECT_FONT_SIZE = "12pt"

# --- Zone Geometry v2 (readability-first layout) -------------------------------------------------
# USER DIRECTIVE (2026-07-24): faithful *placement* is NOT a hard goal. The non-negotiables are
# completeness (every element present), correct numbers, and faithful graphs; ARRANGEMENT is flexible.
# Start from Tableau's layout as a scaffold (keep grouping + reading order), then optimise for
# readability / tidiness. Pixel-perfect reproduction of a floating canvas is explicitly a non-goal --
# and is the very source of the inherited-overlap defects (we faithfully copy Tableau's own
# overlapping coordinates).
#
# Slice 1 -- content-aware min-size for caption/text zones. Tableau authors thin caption bands
# (section headers, instruction lines) at their natural text height (~24-34px). The generic 40px zone
# floor (``_scale_zone``) *inflated* those bands into unreadable blocks and worsened the overlap they
# already had with the content beneath. Text zones instead floor to a single line of their OWN font
# (never inflating a caption already tall enough, never over-expanding a multi-line one) and keep their
# authored width. Charts / tables / images / banner keep the 40px floor unchanged.
_TEXTBOX_MIN_H = 20.0   # px: one line of ~12pt body text (never clips a single line)
_TEXTBOX_MIN_W = 8.0    # px: degenerate-width guard only; never inflate a caption's authored width
_PT_TO_PX = 96.0 / 72.0

# Power BI reserves 8px of padding above AND below a textbox's text by default, so a box's USABLE
# height is ``height - 16``. Tableau reserves nothing: an author who drew a 24px caption strip drew
# it to fit 12pt text, and it does -- in Tableau. Emitted verbatim, those 24px leave 8 for a line
# that needs 19, and the band renders CLIPPED with a scrollbar stub.
#
# The geometry is NOT the thing to change (see ``_fit_textbox_padding``); the padding is. These
# constants are the RENDERER's own, not an estimate: ``max(18, ceil(pt * 25/16)) + padTop + padBottom``
# reproduces `powerbi-report-author validate`'s PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR numbers exactly
# (12pt -> 35, 16pt -> 41).
_TEXTBOX_DEFAULT_PAD = 8.0   # px, per side -- Power BI's default textbox padding


def _textbox_text_height(font_pt=None):
    """Px the RENDERER gives the text itself, before padding: ``max(18, ceil(pt * 25 / 16))``."""
    return max(18.0, math.ceil(float(font_pt or 12.0) * 25.0 / 16.0))


def _textbox_min_height(font_pt=None, pad_top=None, pad_bottom=None):
    """The smallest textbox height that renders ``font_pt`` text without clipping it.

    Mirrors the renderer's rule that `powerbi-report-author validate` enforces as
    ``PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR``. Padding defaults to Power BI's 8px per side; pass the
    authored padding to get the floor for a box whose padding we set ourselves.

    Deliberately NOT used as a ``_scale_zone`` floor. Inflating a caption to reach it is the
    regression ``test_thin_caption_sizes_to_content_not_inflated_to_floor`` guards, and the reason
    is measured: a readability floor propagated up a zone tree makes a frame scale the WHOLE canvas
    to satisfy it (:func:`layout_solve._clamp_to_authored` -- eleven pixels of caption once cost five
    hundred pixels of page). Sizing is settled by padding instead.
    """
    pad = (_TEXTBOX_DEFAULT_PAD if pad_top is None else float(pad_top)) + \
          (_TEXTBOX_DEFAULT_PAD if pad_bottom is None else float(pad_bottom))
    return max(_TEXTBOX_MIN_H, _textbox_text_height(font_pt) + pad)


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
    # A hard line break (Tableau's Æ-sentinel newline, e.g. a two-line column header) becomes its own
    # paragraph so the break renders in Power BI; single-line text stays one paragraph (unchanged). The
    # sentinel itself is scrubbed at the break so no literal "Æ" ends a line (v2-5).
    lines = _scrub_sentinels(tob["text"]).split("\n")
    paragraphs = [{"textRuns": [{"value": ln, "textStyle": style}],
                   "horizontalTextAlignment": "left"} for ln in lines]
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
            "general": [{"properties": {"paragraphs": paragraphs}}]
        },
        "visualContainerObjects": {
            "background": [background],
            "title": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
        },
        "drillFilterOtherVisuals": True,
    }
    _fit_textbox_padding(visual, position, size)
    return {"$schema": SCHEMA_VISUAL, "name": name, "position": position, "visual": visual}


def _fit_textbox_padding(visual, position, font_pt=None):
    """Shrink OUR OWN padding so the author's caption height still renders its text.

    Power BI reserves 8px above and below a textbox's text by default, so a box's USABLE height is
    ``height - 16``. Tableau has no such reserve: an author who drew a 24px caption strip drew it to
    fit 12pt text, and it does fit -- in Tableau. Emitted verbatim into Power BI, those same 24px
    become 8 of text in a 12pt line's worth of space, and the band renders CLIPPED with a scrollbar
    stub. Measured on a real network-operations dashboard: the caption
    "Sort By = Network Score | Region = All | Fiscal Month =" sheared its descenders at 24px, and a
    16pt section header did the same at 31px.

    The fix is NOT to grow the box. Growing it is what the layout solver deliberately refuses to do
    (:func:`layout_solve._clamp_to_authored`) and for good reason -- a readability floor propagated up
    a zone tree makes a frame scale the WHOLE canvas to satisfy it, and eleven pixels of caption once
    cost five hundred pixels of page. The author's geometry is evidence and it wins.

    But the 16px is not the author's, it is OURS: a default we never asked for on a box we emit. So
    give the text the room by spending our own padding first, down to zero, and only then leave the
    box as authored. Explicit padding is emitted ONLY when the default would clip -- a textbox with
    room to spare is byte-identical to before.

    Mirrors the renderer's own rule, which ``powerbi-report-author validate`` enforces as
    ``PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR``: text needs ``max(18, ceil(pt * 25 / 16))`` px, plus padding.
    """
    try:
        height = float((position or {}).get("height") or 0.0)
    except (TypeError, ValueError):
        return
    if height <= 0:
        return
    need = _textbox_text_height(font_pt)
    spare = height - need
    if spare >= 2 * _TEXTBOX_DEFAULT_PAD:
        return                      # the default already fits -- emit nothing (never-regress)
    pad = max(0.0, math.floor(spare / 2.0))
    visual.setdefault("visualContainerObjects", {})["padding"] = [{"properties": {
        "top": {"expr": {"Literal": {"Value": "%dD" % int(pad)}}},
        "bottom": {"expr": {"Literal": {"Value": "%dD" % int(pad)}}},
    }}]


def _caption_only_textbox_visual(ws, zone, ref_w, ref_h, name, tab=0):
    """Rebuild a caption-only worksheet (v2-3) as a textbox at its authored dashboard zone.

    A worksheet whose only content is its title -- a thin status / refresh / filter-breadcrumb bar
    (no rows, no cols, no plottable mark channel; see ``caption_only_raw`` in
    :func:`_parse_worksheet`) -- classifies as ``VT_UNSUPPORTED`` and would be dropped, leaving a
    blank band on the dashboard (the "a thin view doesn't generate at all" defect). Emit its resolved
    caption (``caption_only_text``) as a plain textbox scaled to the zone with the caption-content
    floor (``_TEXTBOX_MIN_W/H`` -- never the 40px chart floor, so a thin bar is not inflated), at the
    normal tiled z-order (an anchor, NOT a floating caption, so the v2-2 de-overlap pass
    leaves it in its own reserved band). Styling is intentionally minimal (default body font, no
    fill) -- completeness first; honouring the bar's authored fill/font is a later refinement.
    Returns ``None`` when there is no caption text to show (an all-token caption that resolved empty),
    so the caller falls back to the existing deferred-visual handling."""
    text = ws.get("caption_only_text")
    if not text:
        return None
    x, y, w, h = _scale_zone(zone, ref_w, ref_h, min_w=_TEXTBOX_MIN_W, min_h=_TEXTBOX_MIN_H)
    return _text_object_textbox_visual(name, _position(x, y, w, h, tab=tab), {"text": text})


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
        # A Tableau dashboard image object carries no caption, and Power BI's absent-value default
        # for a container title is a SHOWN auto-generated one -- on an image that renders as a stray
        # caption bar above the artwork, shrinking it. This constructor bypasses ``_visual_json``, so
        # it states the same "no title" that every other visual now states explicitly.
        "visualContainerObjects": {
            "title": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
            "subTitle": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
        },
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


def _harvest_workbook_palette(ir):
    """Every mark colour the workbook actually uses, in first-seen order -> ``['#rrggbb', ...]``.

    Leads the report theme's ``dataColors`` so a MULTI-SERIES visual -- which takes its series
    colours from the theme in order, and which no per-visual override can address without inventing
    a member map -- rebuilds in the source's own colours instead of Power BI's blue-first default.

    This is the lever the theme builder reserved ``extra_palette`` for. Measured on the corpus's
    time-series workbook: three green series (``#59a14f`` and its lighter/darker siblings) and a
    three-shade stacked area all rebuilt blue, because a single ``dataPoint.defaultColor`` cannot
    express a per-series palette and the theme led with Tableau 10's blue.

    Order matters and is deliberately DOCUMENT order, not sorted: Power BI assigns theme colours to
    series positionally, so the sequence the author's sheets use is the sequence that reproduces
    them. Deduplicated case-insensitively; a workbook that sets no mark colour returns ``[]`` and the
    theme is byte-identical to before.
    """
    out, seen = [], set()

    def _add(value):
        if (isinstance(value, str) and _HEX6_RE.match(value)
                and value.lower() not in seen):
            seen.add(value.lower())
            out.append(value.lower())

    for ws in (ir.get("worksheets") or []):
        # Per-member palette FIRST: a multi-series visual is exactly the case a single
        # ``defaultColor`` cannot express, so its colours must lead the theme's sequence.
        members = (ws.get("mark_colors") or {}).get("members")
        if isinstance(members, dict):
            for member_color in members.values():
                _add(member_color)
        elif isinstance(members, (list, tuple)):
            for entry in members:
                _add(entry.get("color") if isinstance(entry, dict) else entry)
        _add(ws.get("mark_color"))
        _add(ws.get("lollipop_color"))
    return out


def _theme_canvas(ir):
    """``(background, foreground)`` for the report theme from the dashboards' own canvas, or ``None``.

    A dark workbook needs the THEME to know it is dark, not just the page object: the theme supplies
    the default text/label/axis colour every visual inherits. Emit a dark page without it and the
    axis labels, legends and data labels stay near-black on near-black -- technically present,
    invisible in practice.

    Taken from the first dashboard that declares a canvas fill (they are near-always uniform in one
    workbook), and the foreground is chosen for CONTRAST against it by relative luminance rather than
    assumed white, so a light workbook is not given white-on-white. ``None`` when no dashboard
    declares a background, leaving the theme byte-identical.
    """
    for db in (ir.get("dashboards") or []):
        fill = db.get("canvas_fill")
        if fill and _HEX6_RE.match(fill):
            r, g, b = (int(fill[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
            # Rec. 601 luma -- enough to separate "dark canvas" from "light canvas".
            luma = 0.299 * r + 0.587 * g + 0.114 * b
            return fill, ("#ffffff" if luma < 0.5 else "#252423")
    return None


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


def _contrast_ratio(a, b):
    """WCAG 2.x contrast ratio between two ``#rrggbb`` colours, 1.0 (identical) .. 21.0.

    Used only to decide whether a colour is INDISTINGUISHABLE from the page, so the absolute scale
    matters less than the bottom of it. WCAG's own minimum for non-text contrast is 3.0; the
    threshold here is far below that deliberately -- the goal is to drop colours that cannot be seen
    at all, not to enforce accessible contrast on the author's palette.
    """
    def _lum(hex6):
        h = hex6.lstrip("#")
        chans = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        lin = [(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4) for c in chans]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# Below this ratio two colours are the same colour to the eye. 1.0 is exact equality; #ffffff on
# #f5f5f5 is 1.09. Set just above that so an "invisible" mark is caught whether the canvas is pure
# white or the near-white a Tableau dashboard usually paints, while anything a viewer could actually
# distinguish is left alone.
_INVISIBLE_CONTRAST = 1.2


def _visible_against(hex_color, background):
    """Would a mark painted ``hex_color`` be visible on ``background``?

    A Tableau workbook legitimately contains mark colours that exist in order to be INVISIBLE: the
    classic donut is a pie with a white circle punched through its middle, and spacer/halo marks are
    painted in the canvas colour on purpose. Those are overlay geometry, not series colours -- but
    ``_harvest_workbook_palette`` sees them as ordinary mark colours and, because harvested colours
    LEAD ``dataColors``, one of them can land at position 0 and become the default series colour for
    the whole report.

    Measured 2026-08-24 on ``0090_small_multiples``, whose donut hack paints ``#ffffff``: that white
    reached ``dataColors[0]`` and silently erased FIVE bar charts entirely, the donut's own fourth
    slice, and one of the two series in all four time-series panels -- while the report validated
    clean and every gate passed. Recovered by setting that one entry to a visible colour and looking.
    """
    return _contrast_ratio(hex_color, background) >= _INVISIBLE_CONTRAST


def tableau_theme_dict(brand=None, extra_palette=None, canvas=None):
    """The custom-theme JSON: a minimal, always-valid Power BI theme (``name`` + ``dataColors``).

    ``dataColors`` is Tableau's categorical palette (Tableau 10, then the distinct Tableau 20
    extras) so automatically coloured marks rebuild in the source's colour language. Deliberately
    minimal -- no ``background``/``foreground`` overrides -- so it recolours marks only and never
    fights the base theme on text/canvas.

    The theme's ``name`` is the bundled FILE name (``_TABLEAU_THEME_FILE``), NOT a display label:
    Power BI's PBIR schema requires the theme file's internal ``name`` to exactly equal the
    ``customTheme.name`` and the ``RegisteredResources`` item name/path that ``report_json_part``
    registers (all four must match and end in ``.json``). A bare display name like "Tableau" trips
    ``PBIR_THEME_FILE_NAME_MISMATCH`` in ``powerbi-report-author validate`` and the theme -- with its
    whole palette -- silently fails to load. (Guarded by ``pbir_lint``.)

    ``brand`` (a workbook-derived ``#rrggbb``, see ``_derive_brand_color``) leads ``dataColors`` when
    given, so a single-series / auto-coloured chart rebuilds in the workbook's brand colour instead of
    Power BI's blue-first default, while the full Tableau 10/20 sequence still trails as the fallback
    for multi-category charts (the brand is de-duplicated out of that tail, case-insensitively, so it
    never appears twice). ``extra_palette`` (the workbook's own harvested mark colours) is inserted
    after the brand, ahead of the Tableau tail, likewise de-duplicated. With no ``brand`` and no
    ``extra_palette`` the return is byte-identical to the prior default apart from the corrected
    ``name`` (the never-regress contract for ``dataColors``).

    LEAD COLOURS ARE FILTERED FOR VISIBILITY, the Tableau tail is not. A harvested colour that is
    indistinguishable from the page background is dropped (see ``_visible_against``), because such a
    mark is overlay geometry in the source -- a donut's punched hole, a spacer -- and promoting it to
    a series colour paints real data in the canvas colour. The curated Tableau tail is left exactly
    as-is: it is a known-visible palette and filtering it would put the never-regress contract at the
    mercy of whatever canvas a workbook happens to declare.
    """
    base = list(_TABLEAU_10 + _TABLEAU_EXTRA)
    # Power BI's own default report canvas is white, so that is the background a mark competes with
    # when the workbook declares none.
    background = (canvas[0] if canvas and canvas[0] and _HEX6_RE.match(canvas[0]) else "#ffffff")
    lead, dropped = [], []
    for hex_color in ([brand] if brand else []) + list(extra_palette or []):
        if not (hex_color and _HEX6_RE.match(hex_color)):
            continue
        if _visible_against(hex_color, background):
            lead.append(hex_color)
        else:
            dropped.append(hex_color)
    ordered, seen = [], set()
    for hex_color in (lead + base if lead else base):
        low = hex_color.lower()
        if low not in seen:
            seen.add(low)
            ordered.append(hex_color)
    theme = {"name": _TABLEAU_THEME_FILE, "dataColors": ordered}
    # ``canvas`` = the dashboards' own (background, foreground). The theme has to carry it, not
    # just the page object: every visual inherits its default label/axis/legend colour from the
    # theme, so a dark canvas without it renders near-black text on a near-black page -- present
    # in the file, invisible on screen. Matches the adjudicated rebuild, which pairs
    # ``background: #1b1b1b`` with ``foreground: #ffffff``.
    if canvas:
        background_fill, foreground = canvas
        theme["background"] = background_fill
        theme["foreground"] = foreground
        if ordered:
            theme["tableAccent"] = ordered[0]
    return theme
    for hex_color in (lead + base if lead else base):
        low = hex_color.lower()
        if low not in seen:
            seen.add(low)
            ordered.append(hex_color)
    theme = {"name": _TABLEAU_THEME_FILE, "dataColors": ordered}
    # ``canvas`` = the dashboards' own (background, foreground). The theme has to carry it, not
    # just the page object: every visual inherits its default label/axis/legend colour from the
    # theme, so a dark canvas without it renders near-black text on a near-black page -- present
    # in the file, invisible on screen. Matches the adjudicated rebuild, which pairs
    # ``background: #1b1b1b`` with ``foreground: #ffffff``.
    if canvas:
        background, foreground = canvas
        theme["background"] = background
        theme["foreground"] = foreground
        if ordered:
            theme["tableAccent"] = ordered[0]
    return theme


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


def _fp_default_entry(spec):
    """The candidate a field-parameter slot should SEED with -- the controlling Tableau parameter's
    default selection (``spec["default_index"]``), falling back to the first candidate.

    An unselected Power BI field parameter shows its seed, so seeding on branch 0 rather than the
    Tableau default silently opens every such visual on the wrong field. Bounds-checked, so a spec
    without a ``default_index`` (or with a stale one) behaves exactly as before.
    """
    entries = (spec or {}).get("entries") or []
    if not entries:
        return None
    idx = (spec or {}).get("default_index")
    return entries[idx] if isinstance(idx, int) and 0 <= idx < len(entries) else entries[0]


def field_parameter_table_visual(name, specs, position, *, visual_type=VT_TABLE):
    """A ``tableEx``/``pivotTable`` whose Values well EXPANDS a list of field parameters.

    ``specs`` is an ordered list of ``emit_field_parameters`` spec dicts
    (``{table_name, display_col, default_index, entries:[{label, table, column, is_measure, order}, ...]}``).
    Each spec contributes ONE seed projection (its Tableau-default candidate) and ONE
    ``fieldParameters`` entry binding that slot's projection index to the parameter's display column
    (``length`` 1). Slot order follows ``specs`` order, so a 3-dim + 3-measure self-service table
    reproduces the customer layout 1:1. Specs with no resolved entries are skipped.
    """
    projections, field_params = [], []
    for spec in specs or []:
        seed = _fp_default_entry(spec)
        if seed is None:
            continue
        idx = len(projections)
        projections.append(_fp_seed_projection(seed))
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
            sname, spec, _position(sx, sy, slot_w, sh, z=_Z_SLICER, tab=i + 1))))

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


# -- container-stitched pseudo-table -----------------------------------------------------------
# Tableau cannot put several independently table-calculated measures in one view, so authors fake a
# single table: N worksheets laid in a contiguous horizontal container, each contributing one
# measure, with the row labels suppressed on every sheet but the leading one. The dashboard then
# READS as one table. Power BI does this natively in one matrix, so the faithful rebuild is a MERGE.
#
# The signature is exact in the source and needs no heuristic:
#   * the zones form a contiguous horizontal band -- same y, same h, and each zone's x continues
#     where the previous one ended;
#   * every member worksheet groups by the SAME row dimension;
#   * the TRAILING members hide their row labels (Tableau's ``style-rule[element='label']`` ->
#     ``display=false``, which the parser already resolves to a hidden ``categoryAxis``), while the
#     leader keeps its labels. That asymmetry is what distinguishes a stitched pseudo-table from
#     three unrelated tables that merely sit side by side, and it is the gate that keeps this from
#     merging things the author meant to keep separate.
_BAND_EDGE_TOLERANCE = 2          # Tableau's 100000-unit grid rounds; adjacent edges can differ by 1.


def _band_row_signature(ws):
    """The row dimension a table groups by, as a comparable key -- or ``None`` if it has none."""
    rows = [f for f in (ws.get("rows") or []) if isinstance(f, dict)]
    if len(rows) != 1:
        return None
    f = rows[0]
    cap = str(f.get("caption") or f.get("property") or "").strip().casefold()
    return cap or None


def _band_hides_row_labels(ws):
    """Did the author suppress this sheet's row labels? (``style-rule element='label'`` display=false)

    The parser resolves that Tableau spelling to a hidden ``categoryAxis`` and records it on the
    worksheet as ``axis_hidden`` (see :func:`_parse_hidden_axes`), so this reads the fact rather
    than re-deriving it from the source.
    """
    return "categoryAxis" in set(ws.get("axis_hidden") or ()) or bool(ws.get("row_labels_hidden"))


def detect_stitched_table_band(db, ws_by_name):
    """Zones that together fake ONE table -> ``[{leader, members, zones}]`` (possibly empty).

    Pure and side-effect free so it can be tested without emitting anything. Returns at most one
    band per contiguous run; a dashboard may contain several.
    """
    tabular = []
    for z in (db or {}).get("zones") or []:
        ws = (ws_by_name or {}).get(z.get("worksheet"))
        if not isinstance(ws, dict):
            continue
        if ws.get("visual_type") not in (VT_TABLE, VT_MATRIX):
            continue
        if _band_row_signature(ws) is None:
            continue
        tabular.append((z, ws))
    if len(tabular) < 2:
        return []

    bands, run = [], []

    def _flush():
        if len(run) >= 2:
            leader = run[0]
            # The leader must SHOW its labels and every follower must HIDE them. Anything else is
            # not a stitched table -- it is several tables that happen to be adjacent.
            if (not _band_hides_row_labels(leader[1])
                    and all(_band_hides_row_labels(w) for _z, w in run[1:])):
                bands.append({
                    "leader": leader[1]["name"],
                    "members": [w["name"] for _z, w in run],
                    "zones": [z for z, _w in run],
                })
        del run[:]

    for z, ws in sorted(tabular, key=lambda t: (t[0].get("y") or 0, t[0].get("x") or 0)):
        if run:
            pz, pw = run[-1]
            same_row = _band_row_signature(pw) == _band_row_signature(ws)
            aligned = (abs((pz.get("y") or 0) - (z.get("y") or 0)) <= _BAND_EDGE_TOLERANCE
                       and abs((pz.get("h") or 0) - (z.get("h") or 0)) <= _BAND_EDGE_TOLERANCE)
            adjacent = abs(((pz.get("x") or 0) + (pz.get("w") or 0)) - (z.get("x") or 0)) \
                <= _BAND_EDGE_TOLERANCE
            if not (same_row and aligned and adjacent):
                _flush()
        run.append((z, ws))
    _flush()
    return bands


def merge_stitched_band_state(state, leader_ws, follower_ws, model_table, field_map, warnings):
    """Fold the followers' MEASURE columns into the leader's query state -> ``[(ws, state)]``.

    The members all group by the same row dimension and each contributes one measure, so the merged
    visual is the leader's state plus every follower's measure projections. The shared dimension is
    contributed once, by the leader; a follower's copy is dropped -- that repeated column is exactly
    what the author hid and what the un-merged rebuild puts back.

    Returns EVERY member paired with the state its own conditional format must be computed against,
    the leader included. The leader's is a pre-merge snapshot, because
    :func:`_matrix_discrete_measure_colour` paints every value projection it is handed: run after the
    merge it would apply the leader's quartile buckets to the other members' columns too
    (render-confirmed).
    """
    values = (state.get("Values") or {}).get("projections")
    if values is None:
        return []
    leader_own = {"Values": {"projections": list(values)}}
    seen = {p.get("queryRef") for p in values}
    shared = {_dumps(p.get("field")) for p in values if "Column" in (p.get("field") or {})}
    member_states = [(leader_ws, leader_own)]
    for fw in follower_ws:
        fstate = _build_query_state(fw, model_table, field_map, warnings)
        if not _query_state_complete(fw.get("visual_type"), fstate):
            continue
        member_states.append((fw, fstate))
        for p in ((fstate.get("Values") or {}).get("projections") or []):
            if _dumps(p.get("field")) in shared:
                continue                      # the row dimension the leader already contributes
            if p.get("queryRef") in seen:
                continue
            seen.add(p.get("queryRef"))
            values.append(p)
    return member_states


def port_band_member_projections(state, member_state, objects):
    """Move a member's hidden calculation onto the merged state -> that member's objects, rebound.

    Two things have to happen for a member's conditional format to survive the merge.

    SCOPE. A member's colour emitter paints every projection in the state it was given -- its
    measure, the shared row dimension, and its own hidden calculation. In a merged visual only the
    measure is that member's to paint; leaving the rest in would let the last member processed
    colour the row-label column.

    PORTING, WITH A RENAME. The calculation the member declared lives on the member's state, which
    the merged visual never serialises, so it must be copied across or the ``SelectRef`` dangles.
    The refs collide by construction: every member state starts empty, so
    :func:`_colour_vc_query_ref` hands each the same first free name. Ported naively the last one
    wins and every column paints from whichever DAX arrived first -- resolving cleanly, reporting
    nothing. A colliding ref whose EXPRESSION differs is renamed and the member's own ``SelectRef``s
    are rewritten to match.
    """
    values = (state.get("Values") or {}).get("projections")
    if values is None:
        return objects
    own = {p.get("queryRef") for p in ((member_state.get("Values") or {}).get("projections") or [])
           if not p.get("hidden") and "Column" not in (p.get("field") or {})}
    objects = [o for o in (objects or [])
               if ((o.get("selector") or {}).get("metadata") in own)]
    if not objects:
        return objects
    declared = {p.get("queryRef"): _dumps(p.get("field")) for p in values}
    rename = {}
    for p in ((member_state.get("Values") or {}).get("projections") or []):
        if not p.get("hidden"):
            continue
        ref, body = p.get("queryRef"), _dumps(p.get("field"))
        if declared.get(ref) == body:
            continue                                  # identical calculation already present
        if ref in declared:
            fresh, n = ref, 1
            while fresh in declared:
                n += 1
                fresh = "%s%d" % (ref, n)
            rename[ref] = fresh
            p = dict(p, queryRef=fresh, nativeQueryRef=fresh)
            ref = fresh
        declared[ref] = body
        values.append(p)
    if not rename:
        return objects

    def _rebind(node):
        if isinstance(node, dict):
            sel = node.get("SelectRef")
            if isinstance(sel, dict) and sel.get("ExpressionName") in rename:
                sel["ExpressionName"] = rename[sel["ExpressionName"]]
            for v in node.values():
                _rebind(v)
        elif isinstance(node, list):
            for v in node:
                _rebind(v)

    objects = copy.deepcopy(objects)
    _rebind(objects)
    return objects


def _stitched_band_zone(zones):
    """One zone spanning the whole band -- the leader's origin, the band's full width."""
    xs = [z.get("x") or 0 for z in zones]
    rights = [(z.get("x") or 0) + (z.get("w") or 0) for z in zones]
    lead = min(zones, key=lambda z: z.get("x") or 0)
    merged = dict(lead)
    merged["x"] = min(xs)
    merged["w"] = max(rights) - min(xs)
    return merged


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
    chain_index = _view_only_field_chain_index(table_calc_usages)
    # What-if parameter scalars a colour rule may compare against. Read off the IR (built by
    # :func:`build_ir`); ``{}`` for any IR built before this key existed, which simply means a
    # parameter operand does not resolve and the rule declines -- the pre-existing behaviour.
    _param_values = ir.get("parameter_values") or {}

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
        tableau_theme_dict(brand=brand_color,
                           extra_palette=_harvest_workbook_palette(ir),
                           canvas=_theme_canvas(ir)))
    parts[".platform"] = _dumps({
        "$schema": SCHEMA_PLATFORM,
        "metadata": {"type": "Report", "displayName": report_name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    })

    page_order = []
    placed = set()

    global _PAGE_H_OVERRIDE, _PAGE_W_OVERRIDE, _LAYOUT_PLAN, _ZONE_PAD_SCALE
    for db in ir["dashboards"]:
        page_name = _sanitize("page-" + (db["name"] or "dashboard"))
        # A worksheet whose ONLY appearance on this dashboard is an author-hidden zone is still
        # PLACED -- the author put it on the dashboard and then collapsed it. Seeding ``placed``
        # keeps it from falling through to the standalone-worksheet pass below, which would give a
        # deliberately hidden sheet (e.g. a help/guidelines panel) its own visible report page --
        # the opposite of the author's intent.
        for _hz in db.get("hidden_zones_skipped") or []:
            if _hz.get("ref"):
                placed.add(_hz["ref"])
        zones = db["zones"]
        ref_w = (db["extent"]["w"] or max((z["x"] + z["w"] for z in zones), default=0)
                 or db["size"]["w"])
        ref_h = (db["extent"]["h"] or max((z["y"] + z["h"] for z in zones), default=0)
                 or db["size"]["h"])
        # Emit the page at the dashboard's OWN fixed pixel canvas (<size maxwidth/maxheight>), so a
        # 1400x1000 Tableau dashboard becomes a 1400x1000 page -- exact number-for-number match, aspect
        # preserved. Tableau normalizes the zone coords to a square 100000x100000 (see _scale_zone),
        # so the real aspect is recoverable ONLY from <size>; scaling the normalized rect into the real
        # page (independent sx/sy) de-normalizes it back to faithful pixels. When the dashboard has no
        # fixed max size (sizing-mode='automatic' -- only minwidth/minheight), it renders fit-to-window
        # (usually LARGER than the min), so we keep its authored ASPECT but scale it UP to cover the
        # 1280x720 screen frame (_automatic_canvas_dims) rather than squashing it into the near-square
        # 1000x800 default. Final fallback (no usable <size> at all): Tableau's own 1000x800 default.
        _PAGE_W_OVERRIDE, _PAGE_H_OVERRIDE = _dash_page_dims(db["size"])
        _authored_px_w, _authored_px_h = _PAGE_W_OVERRIDE, _PAGE_H_OVERRIDE
        # Solver engine: activate this dashboard's plan and ADOPT the page it resolved. Growth is not
        # cosmetic -- the solver enlarges the page (bounded by MAX_GROWTH) precisely so the content
        # fits, so its rects are valid ONLY on that page. Keeping the authored page while using solved
        # rects puts them out of bounds; measured on the corpus that is 100 out-of-bounds visuals
        # versus 0 when the grown page is adopted. A grown page still rescales to the viewport under
        # FitToPage, so adopting it costs render scale, never content.
        _LAYOUT_PLAN = db.get("layout_plan")
        if _LAYOUT_PLAN:
            _PAGE_W_OVERRIDE = _whole_px(_LAYOUT_PLAN["page"][0])
            _PAGE_H_OVERRIDE = _whole_px(_LAYOUT_PLAN["page"][1])
        # Zone padding is authored in real pixels against the AUTHORED canvas, so it scales by the
        # authored->emitted page ratio (not by the 0..100000 zone scale). Identity in the normal
        # case where the emitted page is the authored size.
        _ZONE_PAD_SCALE = ((_page_w() / _authored_px_w) if _authored_px_w else 1.0,
                           (_page_h() / _authored_px_h) if _authored_px_h else 1.0)
        visuals = []
        page_ws = []
        # A dashboard filter CARD carries only a raw field token; its slicer field is resolved from
        # the filter shelves of the worksheets on the page. Resolving against ``page_ws`` alone loses
        # every card whose owning sheet produced no visual -- and a worksheet that exists PURELY to
        # host filter cards (empty shelves, a name like "filters <dashboard>") is a standard Tableau
        # idiom, so exactly the sheets that are all-filter were the ones contributing no filters.
        # Index every worksheet the dashboard references instead, so a card is dropped only when its
        # token genuinely resolves to nothing.
        # A container-stitched pseudo-table is DISCLOSED rather than silently rebuilt as N separate
        # tables. Tableau cannot put several independently table-calculated measures in one view, so
        # the author lays N sheets in a contiguous band and hides the row labels on all but the
        # first, making the dashboard read as ONE table. Power BI does that natively in a single
        # matrix -- but the rebuild currently emits one table per sheet, which repeats the row-label
        # column N times and breaks the illusion the author built.
        #
        # Detection is exact (see :func:`detect_stitched_table_band`) and is verified to fire on the
        # stitched case while declining BOTH near-misses in the same workbook: a single-sheet measure
        # trellis, and a bar-mark band whose category axes the engine already suppresses correctly.
        # Until the merge lands, naming it is strictly better than a silently wrong layout -- the
        # difference is visible on the page and invisible to every validator.
        for _band in detect_stitched_table_band(db, ws_by_name):
            warnings.append(_warn(
                "dashboard", db["name"],
                "container-stitched table: %d worksheets (%s) sit in one contiguous band sharing a "
                "row dimension, with the row labels hidden on every sheet but %r -- in Tableau that "
                "reads as ONE table. They are rebuilt as %d separate tables, so the row-label column "
                "repeats %d times. Power BI expresses this natively as a single matrix with %d value "
                "columns; merge them there to restore the source's appearance"
                % (len(_band["members"]), ", ".join(repr(m) for m in _band["members"]),
                   _band["leader"], len(_band["members"]), len(_band["members"]),
                   len(_band["members"]))))
        # A container-stitched pseudo-table is MERGED into one visual rather than rebuilt as N
        # separate tables. Tableau cannot put several independently table-calculated measures in one
        # view, so the author lays N sheets in a contiguous band and hides the row labels on all but
        # the first, making the dashboard read as ONE table. Power BI does that natively, so the
        # faithful rebuild is a merge: the leader's zone widens to the whole band, the followers'
        # measures join its Values, and each member's conditional formatting comes with it, bound to
        # its own column.
        #
        # Detection is exact (see :func:`detect_stitched_table_band`) and declines BOTH near-misses
        # in the same source workbook: a single-sheet measure trellis, and a bar-mark band whose
        # category axes the engine already suppresses correctly.
        _band_leader, _band_follow, _band_zone = {}, {}, {}
        for _band in detect_stitched_table_band(db, ws_by_name):
            _lead = _band["leader"]
            _band_leader[_lead] = [ws_by_name[m] for m in _band["members"][1:] if m in ws_by_name]
            _band_zone[_lead] = _stitched_band_zone(_band["zones"])
            for _m in _band["members"][1:]:
                _band_follow[_m] = _lead
            warnings.append(_warn(
                "dashboard", db["name"],
                "container-stitched table: %d worksheets (%s) sat in one contiguous band sharing a "
                "row dimension with the row labels hidden on every sheet but %r -- in Tableau that "
                "reads as ONE table. Rebuilt as a SINGLE visual with %d value columns, Power BI's "
                "native form, so the row-label column appears once as the author intended rather "
                "than %d times. Each column keeps its own conditional formatting"
                % (len(_band["members"]), ", ".join(repr(m) for m in _band["members"]),
                   _lead, len(_band["members"]), len(_band["members"]))))
        card_ws = []
        for i, zone in enumerate(zones):
            ws = ws_by_name.get(zone["worksheet"])
            if not ws:
                continue
            card_ws.append(ws)
            if ws["visual_type"] == VT_UNSUPPORTED:
                # A TITLE-ONLY KPI worksheet -- no rows, no cols, its entire content is the live
                # number(s) in its title -- has no mark to draw, but it is not a caption either. The
                # caption path dropped it outright (the text still holds a field ref), so a whole
                # "Current: 745,568 / vs Last Year: 131,634" tile came out EMPTY. Emit its cards at
                # the authored zone; only fall through to the static caption when nothing resolved.
                if ws.get("kpi_title_cards"):
                    x, y, w, h = _scale_zone(zone, ref_w, ref_h)
                    kvis, krecs, _kh = _emit_kpi_title_cards(
                        ws, x, y, w, h, i, page_name, db["name"] or page_name,
                        model_table, field_map, _sanitize(f"v-{page_name}-{i}-{ws['name']}"))
                    if kvis:
                        visuals.extend(kvis)
                        records.extend(krecs)
                        placed.add(ws["name"])
                        continue
                # v2-3: a caption-only worksheet (a thin status / refresh / filter-breadcrumb bar
                # whose only content is its -- often dynamic -- title, no plottable mark) is rebuilt
                # as a textbox at its authored zone instead of vanishing, so the dashboard band is
                # never left empty (completeness). Every other unsupported worksheet is a genuinely
                # deferred visual and still falls through to the existing "zone left empty" note.
                cap = _caption_only_textbox_visual(
                    ws, zone, ref_w, ref_h,
                    _sanitize(f"v-{page_name}-cap-{i}-{ws['name']}"), tab=i)
                if cap is not None:
                    visuals.append(cap)
                    placed.add(ws["name"])
                    # Honest disclosure. The dynamic tokens this view PINS are now resolved to their
                    # live display values (parameters by alias/format, filtered fields by their
                    # selected member or "All", workbook/sheet names, the extract refresh stamp), so
                    # the band reads as the author wrote it. What a STATIC textbox still cannot do is
                    # re-resolve on a slicer change, so say that rather than claim the values are
                    # missing -- and call out the refresh stamp's time zone, which Tableau renders in
                    # the VIEWER's zone while this carries the value exactly as recorded.
                    _raw = ws.get("caption_only_raw") or ""
                    _had_dynamic = "<" in _raw
                    _tz = ("; <Data Update Time> is the extract's recorded stamp -- Tableau renders "
                           "it in the viewer's time zone" if "<Data Update Time>" in _raw else "")
                    _reason = ("caption-only worksheet rebuilt as a textbox with its dynamic tokens "
                               "resolved to current values; the text is STATIC, so it does not "
                               "re-resolve when a reader changes a slicer" + _tz
                               if _had_dynamic else
                               "caption-only worksheet rebuilt as a textbox (static caption)")
                    warnings.append(_warn("worksheet", ws["name"], _reason))
                continue
            placed.add(ws["name"])
            # A band FOLLOWER contributes its measure to the leader's merged visual and emits no
            # visual of its own. Marked placed above, so it does not fall through to the
            # standalone-worksheet pass and reappear as its own page.
            if ws["name"] in _band_follow:
                continue
            _band_followers = _band_leader.get(ws["name"]) or []
            if _band_followers:
                zone = _band_zone.get(ws["name"], zone)
            state = _build_query_state(ws, model_table, field_map, warnings)
            if not _query_state_complete(ws["visual_type"], state):
                warnings.append(_warn(
                    "worksheet", ws["name"],
                    f"{ws['visual_type']} visual has no usable field bindings (skipped)"))
                continue
            _band_states = merge_stitched_band_state(
                state, ws, _band_followers, model_table, field_map, warnings) \
                if _band_followers else []
            page_ws.append(ws)
            x, y, w, h = _scale_zone(zone, ref_w, ref_h)
            vname = _sanitize(f"v-{page_name}-{i}-{ws['name']}")
            native_pct = _detect_native_pct_stacked(ws, state, vc_index)
            fchain_handled, fchain_fact = _apply_formula_table_calc_chain(
                ws, state, chain_index, model_table, field_map, warnings, _param_values)
            if fchain_handled:
                # A nested formula table-calc chain rebuilt as nested Visual Calculations owns the
                # value shelf; the quick-calc / native-percent paths do not also run for this visual.
                vtype = _pbir_vtype(ws["visual_type"], state)
                vc_value_objects, vc_fact = None, fchain_fact
            elif native_pct:
                # A colour-legend within-bar percent-of-total emits as a native 100%-stacked chart:
                # Power BI normalizes each bar's series to 100% off the RAW measure, so the
                # percent-of-total Visual Calculation is intentionally skipped (raw Sum stays on Y).
                vtype = native_pct
                vc_value_objects, vc_fact = None, None
            else:
                vtype = _pbir_vtype(ws["visual_type"], state)
                vc_value_objects, vc_fact = _apply_visual_calcs(
                    ws, state, vc_index, model_table, field_map, warnings)
            if vc_fact is None and fchain_fact is not None:
                vc_fact = fchain_fact   # preserve a nested-chain route-to-review disclosure
            pos = _position(x, y, w, h, tab=i)
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
                if value_objects is None:
                    # No CONTINUOUS scale. A DISCRETE colour measure paints the same cells through
                    # the model's hex-returning colour twin instead -- font or background per the
                    # Tableau mark. Only consulted when the gradient path emitted nothing, so a
                    # heat table is byte-unchanged.
                    if not _band_states:
                        _dc_objects, _dc_fact = _matrix_discrete_measure_colour(
                            ws, state, model_table, field_map, warnings, _param_values)
                        if _dc_objects:
                            value_objects, cf_fact = _dc_objects, _dc_fact
                # Each band member owns ONE column and ONE rule. Computed against that member's own
                # state so its selector names only its own measure, then the calculation it declared
                # is ported onto the merged state so the SelectRef resolves.
                for _mw, _mstate in _band_states:
                    _mo, _mf = _matrix_discrete_measure_colour(
                        _mw, _mstate, model_table, field_map, warnings, _param_values)
                    if _mo:
                        _mo = port_band_member_projections(state, _mstate, _mo)
                        value_objects = list(value_objects or []) + list(_mo)
                        cf_fact = cf_fact or _mf
            data_point_objects, mc_fact = _data_point_colors(
                ws, state, ws["visual_type"], model_table, field_map, warnings)
            cont_objects, cont_fact = _chart_discrete_measure_fill(
                ws, state, ws["visual_type"], model_table, field_map, warnings, _param_values)
            if cont_objects is None:
                cont_objects, cont_fact = _chart_continuous_fill(
                    ws, state, ws["visual_type"], model_table, field_map, warnings)
            if cont_objects:
                # a continuous colour scale owns the mark fill; the Measure-Names series is N/A
                ms_objects, ms_fact = None, None
                if not data_point_objects:
                    data_point_objects = cont_objects
            else:
                ms_objects, ms_fact = _measure_series_colors(
                    ws, state, ws["visual_type"], warnings)
                if ms_objects and not data_point_objects:
                    data_point_objects = ms_objects
            # LAST resort: the worksheet's flat constant mark colour. Every richer source above
            # wins; this only replaces Power BI's default blue with the colour the author chose.
            card_label_objects = _card_label_objects(ws, vtype)
            label_objects, dl_fact = _data_labels(ws, ws["visual_type"], warnings)
            legend_objects, lg_fact = _legend_objects(
                ws, zone, db.get("legend_zones"), ws["visual_type"])
            flag_fc = _merge_filter_configs(
                _flag_filter_config_for(ir, ws["name"]),
                _applied_filter_config_for(
                    ws, _surfaced_filter_keys(ir.get("worksheets") or [], db),
                    model_table, field_map, warnings))
            shape_objects = None
            # Every map is an azureMap now (shapeMap rendered blank; Bing map/filledMap are
            # deprecated and pop a modal in Desktop). Its whole appearance -- basemap style, the
            # data-bound referenceLayer choropleth, and the bubbleLayer suppression that stops a
            # bubble being drawn on every centroid -- rides ``extra_objects``.
            azure_objects = _azure_map_objects_for(ws, state, warnings)
            analytics_objects = _reference_line_analytics_objects(ws)
            lollipop_objects = _lollipop_objects(ws) if ws.get("lollipop") else None
            data_point_objects, lollipop_objects = _with_constant_mark_color(
                ws, vtype, data_point_objects, lollipop_objects)
            trellis_measures = _detect_measure_trellis(ws, state)
            if trellis_measures:
                # Measure-trellis: fan the N '+'-concatenated measures into N side-by-side bar
                # charts aligned on a shared category axis (no native PBI multi-measure trellis).
                _tt, _ts = _title_display(ws, zone.get("show_title", True))
                # _sort_definition may add a Tooltips projection to `state`; bind it explicitly
                # here rather than relying on argument-evaluation order.
                sort_def = _sort_definition(ws, state, model_table, field_map)
                tvis, trecs = _emit_measure_trellis(
                    ws, state, trellis_measures, x, y, w, h, i,
                    page_name, db["name"] or page_name, model_table, field_map, vname,
                    sort_def,
                    label_objects, data_point_objects, warnings,
                    title=_tt, show_title=_ts)
                visuals += _inherit_flag_filters(tvis, flag_fc)
                records += trecs
                continue
            spark_title, show_title = _title_display(ws, zone.get("show_title", True))
            kpi_card = ws.get("kpi_title_card")
            if kpi_card:
                # KPI title-card: rebuild the title-embedded headline number as a companion card in
                # the top band of the zone and shrink the sparkline into the band below it. The card
                # carries the caption, so the sparkline drops its (duplicate) title.
                card_vis, card_recs, card_h = _emit_kpi_title_cards(
                    ws, x, y, w, h, i, page_name, db["name"] or page_name,
                    model_table, field_map, vname, flag_fc=flag_fc)
                if card_vis:
                    visuals.extend(card_vis)
                    records.extend(card_recs)
                    pos = _position(x, y + card_h, w, max(1, h - card_h), tab=i)
                    # Drop the sparkline's caption AND say so explicitly: leaving the title object
                    # off would let Power BI auto-generate a field-name one under the card.
                    spark_title, show_title = None, False
            sort_def = _sort_definition(ws, state, model_table, field_map)
            visuals.append(_visual_json(
                vname, vtype, pos, state,
                sort_def,
                filter_config=flag_fc,
                title=spark_title, title_style=ws.get("title_style"),
                show_title=show_title,
                axis_titles=ws.get("axis_titles"),
                continuous_axis=ws.get("continuous_axis"),
                axis_hidden=ws.get("axis_hidden"),
                value_objects=value_objects, data_point_objects=data_point_objects,
                label_objects=label_objects, legend_objects=legend_objects,
                shape_objects=shape_objects, card_label_objects=card_label_objects,
                analytics_objects=analytics_objects,
                font_objects=_grid_font_objects(ws),
                extra_objects=_merge_extra_objects(lollipop_objects, azure_objects),
                lipstick_overlap=ws.get("lipstick_overlap"),
                lipstick_series_colors=ws.get("lipstick_series_colors"),
                lipstick_series_transparency=ws.get("lipstick_series_transparency"),
                container_fill=ws.get("canvas_fill")))
            rec = _candidate_record(page_name, vname, ws, vtype, state, pos,
                                    page_display=db["name"] or page_name,
                                    model_table=model_table, field_map=field_map)
            if cf_fact:
                rec["conditional_format"] = cf_fact
            if vc_fact:
                rec["visual_calc"] = vc_fact
            if mc_fact:
                rec["mark_colors"] = mc_fact
            if cont_fact:
                rec["chart_continuous_fill"] = cont_fact
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
                # ``flag_fc`` now carries two kinds of container: measure keep-flags and applied
                # worksheet-filter columns. Report each under its own key so neither is mistaken
                # for the other, and so a column container cannot crash the measure read.
                rec_flags = [c["field"]["Measure"]["Property"]
                             for c in flag_fc["filters"] if "Measure" in (c.get("field") or {})]
                rec_cols = [c["field"]["Column"]["Property"]
                            for c in flag_fc["filters"] if "Column" in (c.get("field") or {})]
                if rec_flags:
                    rec["flag_filters"] = rec_flags
                if rec_cols:
                    rec["applied_filters"] = rec_cols
            records.append(rec)
        visuals += _emit_slicers(
            page_ws, page_name, model_table, field_map, warnings,
            shown_tokens={tuple(t) for t in (db.get("filter_field_tokens") or ())},
            filter_zones=db.get("filter_zones") or [], ref_w=ref_w, ref_h=ref_h,
            filter_ws=card_ws)
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
                _position(bx, by, bw, bh, z=_Z_BANNER, tab=0),
                banner))
        # General text objects: every OTHER captured dashboard text zone (section-header caption bars
        # + fill-less instruction/metric lines) rebuilds as its own textbox at its authored position.
        # The caption layer sits below the banner and overlay images but above worksheet content, so a
        # caption bar layered over a matrix stays on top. The title banner is already de-duped out of
        # this list upstream, so it is never drawn twice. Empty list -> nothing added (never-regress).
        for j, tob in enumerate(db.get("text_objects") or []):
            # Content-aware floor: size to one line of the zone's own font rather than the blanket 40px
            # zone floor, so a thin caption band renders its text without being inflated into an
            # overlap-inducing block (Zone Geometry v2 slice 1). ``max()`` with the authored height in
            # ``_scale_zone`` means a taller / multi-line caption is preserved unchanged -- never-regress
            # and never over-expanded (a wider tidy pass owns holistic spacing).
            _tmin_h = max(_TEXTBOX_MIN_H, (tob.get("font_size") or 12.0) * _PT_TO_PX * 1.35)
            tx, ty, tw, th = _scale_zone(tob, ref_w, ref_h, min_w=_TEXTBOX_MIN_W, min_h=_tmin_h)
            visuals.append(_text_object_textbox_visual(
                _sanitize("v-%s-text-%d" % (page_name, j)),
                _position(tx, ty, tw, th, z=_Z_CAPTION, tab=len(visuals) + 1),
                tob))
        # Shown-state reflow: if the slicers we surfaced (a hidden/collapsed Tableau filter band) now
        # collide with a worksheet zone authored at its hidden-state position, push the sheets below the
        # band and compress to fit -- exactly what Tableau does on "Show Filters". No-op when nothing
        # overlaps (never-regress).
        _reflow_worksheets_below_slicers(visuals, _page_h())
        # Dashboard image / button objects: place each packaged PNG (logo, export/filter/info icon) as
        # a positioned image visual at its own zone geometry. Added AFTER the reflow so a top-corner
        # image is never shoved by the slicer-band compaction (it is decoration, not worksheet
        # content). ``_image_z`` routes each one to the backdrop or the overlay layer from Tableau's
        # own paint order; an overlay sits above the title banner so a logo overlapping the band
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
                _position(ix, iy, iw, ih, z=_image_z(iz, db.get("first_ws_ord")),
                          tab=len(visuals) + 1),
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
        # Fail-closed layering backstop: demote any image that would fully cover a rebuilt visual
        # beneath the content. ``_image_z`` reads Tableau's document paint order, which is correct
        # on every workbook measured, but it is a heuristic whose fallbacks favour the overlay
        # layer -- and getting this wrong renders the whole page BLANK while validating clean.
        # Geometry decides it independently: an image that would erase content cannot be
        # decoration. Runs before the caption tidy so a demoted plate stops acting as an anchor.
        _demote_occluding_overlays(visuals, warnings, db["name"])
        # Tidy pass (v2-2 / v2-8): relocate any floating caption textbox that Tableau floated
        # on top of the content it labels into a clear band (prefer directly above the anchor); on a
        # packed page with no clear strip, OPEN a band by pushing content down (v2-8) and grow the
        # page to keep it in-bounds. Gated to a no-op on a page with no caption<->anchor overlap, so
        # cleanly tiled dashboards are byte-identical. Runs last, after the images are placed, so
        # every anchor is final. The page is FitToPage, so a taller canvas just rescales to the viewport.
        _grown_h = _deoverlap_captions(visuals, _page_w(), _page_h())
        if _grown_h and _grown_h > _page_h():
            _PAGE_H_OVERRIDE = _whole_px(_grown_h)
        _emit_page(parts, page_name, db["name"] or page_name, visuals,
                   canvas_fill=db.get("canvas_fill"))
        page_order.append(page_name)

    _PAGE_W_OVERRIDE = None
    _PAGE_H_OVERRIDE = None
    _LAYOUT_PLAN = None
    _ZONE_PAD_SCALE = (1.0, 1.0)
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
        native_pct = _detect_native_pct_stacked(ws, state, vc_index)
        fchain_handled, fchain_fact = _apply_formula_table_calc_chain(
            ws, state, chain_index, model_table, field_map, warnings, _param_values)
        if fchain_handled:
            # A nested formula table-calc chain rebuilt as nested Visual Calculations owns the
            # value shelf; the quick-calc / native-percent paths do not also run for this visual.
            vtype = _pbir_vtype(ws["visual_type"], state)
            vc_value_objects, vc_fact = None, fchain_fact
        elif native_pct:
            # A colour-legend within-bar percent-of-total emits as a native 100%-stacked chart:
            # Power BI normalizes each bar's series to 100% off the RAW measure, so the
            # percent-of-total Visual Calculation is intentionally skipped (raw Sum stays on Y).
            vtype = native_pct
            vc_value_objects, vc_fact = None, None
        else:
            vtype = _pbir_vtype(ws["visual_type"], state)
            vc_value_objects, vc_fact = _apply_visual_calcs(
                ws, state, vc_index, model_table, field_map, warnings)
        if vc_fact is None and fchain_fact is not None:
            vc_fact = fchain_fact   # preserve a nested-chain route-to-review disclosure
        pos = _position(40, 40, 880, 620)
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
            if value_objects is None:
                # See the dashboard path: a DISCRETE colour measure paints these cells through the
                # model's colour twin when there is no continuous scale to own them.
                _dc_objects, _dc_fact = _matrix_discrete_measure_colour(
                    ws, state, model_table, field_map, warnings, _param_values)
                if _dc_objects:
                    value_objects, cf_fact = _dc_objects, _dc_fact
        data_point_objects, mc_fact = _data_point_colors(
            ws, state, ws["visual_type"], model_table, field_map, warnings)
        cont_objects, cont_fact = _chart_discrete_measure_fill(
            ws, state, ws["visual_type"], model_table, field_map, warnings, _param_values)
        if cont_objects is None:
            cont_objects, cont_fact = _chart_continuous_fill(
                ws, state, ws["visual_type"], model_table, field_map, warnings)
        if cont_objects:
            # a continuous colour scale owns the mark fill; the Measure-Names series is N/A
            ms_objects, ms_fact = None, None
            if not data_point_objects:
                data_point_objects = cont_objects
        else:
            ms_objects, ms_fact = _measure_series_colors(
                ws, state, ws["visual_type"], warnings)
            if ms_objects and not data_point_objects:
                data_point_objects = ms_objects
        card_label_objects = _card_label_objects(ws, vtype)
        label_objects, dl_fact = _data_labels(ws, ws["visual_type"], warnings)
        flag_fc = _flag_filter_config_for(ir, ws["name"])
        shape_objects = None
        azure_objects = _azure_map_objects_for(ws, state, warnings)
        analytics_objects = _reference_line_analytics_objects(ws)
        lollipop_objects = _lollipop_objects(ws) if ws.get("lollipop") else None
        data_point_objects, lollipop_objects = _with_constant_mark_color(
            ws, vtype, data_point_objects, lollipop_objects)
        trellis_measures = _detect_measure_trellis(ws, state)
        if trellis_measures:
            # Measure-trellis on a standalone worksheet page: fan into N side-by-side bar charts
            # across the page band (no dashboard header row here, so each carries no title either --
            # the fan-out stays faithful to the one-pane-per-measure source layout).
            px, py, pw, ph = 40, 40, 880, 620
            sort_def = _sort_definition(ws, state, model_table, field_map)
            tvis, trecs = _emit_measure_trellis(
                ws, state, trellis_measures, px, py, pw, ph, 0,
                page_name, ws["name"], model_table, field_map, vname,
                sort_def,
                label_objects, data_point_objects, warnings)
            records += trecs
            visuals = (_inherit_flag_filters(tvis, flag_fc)
                       + _emit_slicers([ws], page_name, model_table, field_map, warnings))
            _emit_page(parts, page_name, ws["name"], visuals)
            page_order.append(page_name)
            continue
        spark_title, show_title = _title_display(ws)
        kpi_card = ws.get("kpi_title_card")
        if kpi_card:
            # KPI title-card on a standalone page: card (top band) + sparkline (below).
            card_vis, card_recs, card_h = _emit_kpi_title_cards(
                ws, 40, 40, 880, 620, 0, page_name, ws["name"],
                model_table, field_map, vname, flag_fc=flag_fc)
            if card_vis:
                visuals.extend(card_vis)
                records.extend(card_recs)
                pos = _position(40, 40 + card_h, 880, max(1, 620 - card_h))
                spark_title, show_title = None, False
        sort_def = _sort_definition(ws, state, model_table, field_map)
        main = _visual_json(
            vname, vtype, pos, state,
            sort_def,
            filter_config=flag_fc,
            title=spark_title, title_style=ws.get("title_style"),
            show_title=show_title,
            axis_titles=ws.get("axis_titles"),
            continuous_axis=ws.get("continuous_axis"),
            axis_hidden=ws.get("axis_hidden"),
            value_objects=value_objects, data_point_objects=data_point_objects,
            label_objects=label_objects, shape_objects=shape_objects,
            card_label_objects=card_label_objects,
            analytics_objects=analytics_objects,
            font_objects=_grid_font_objects(ws),
                extra_objects=_merge_extra_objects(lollipop_objects, azure_objects),
            lipstick_overlap=ws.get("lipstick_overlap"),
            lipstick_series_colors=ws.get("lipstick_series_colors"),
            lipstick_series_transparency=ws.get("lipstick_series_transparency"),
            container_fill=ws.get("canvas_fill"))
        rec = _candidate_record(page_name, vname, ws, vtype, state, pos,
                                page_display=ws["name"],
                                model_table=model_table, field_map=field_map)
        if cf_fact:
            rec["conditional_format"] = cf_fact
        if vc_fact:
            rec["visual_calc"] = vc_fact
        if mc_fact:
            rec["mark_colors"] = mc_fact
        if cont_fact:
            rec["chart_continuous_fill"] = cont_fact
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

    # ZERO-PAGE CRASH GUARD. A PBIR whose ``pageOrder`` is empty does not open EMPTY -- Power BI
    # Desktop throws ``TypeError: Cannot read properties of undefined (reading 'visualContainers')``
    # and the project fails to open at all, which takes the correctly built semantic model beside it
    # out of reach too. So a run where every worksheet deferred still ships ONE page.
    #
    # Deliberately a page with NO visuals: the emit gate's contract -- "an unsupported mark / a
    # chart missing a required role emits no visual" -- is exactly right and stays intact. This adds
    # the container Desktop requires, never a visual the gate refused.
    if not page_order:
        _placeholder = _sanitize("page-empty")
        _emit_page(parts, _placeholder, _EMPTY_REPORT_PAGE_NAME, [])
        page_order.append(_placeholder)
        warnings.append(_warn(
            "workbook", ir.get("name") or "workbook",
            "no worksheet could be rebuilt, so the report carries a single empty placeholder page: "
            "a PBIR with no pages CRASHES Power BI Desktop on open (it does not open empty), which "
            "would also put the semantic model built beside it out of reach"))

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
    overlap. Rows are clustered by top-y (``tol`` px).

    Under the solver engine the vertical half of that is skipped: the solve already reserved each
    filter leaf's minimum (``layout_solve.MIN_SLICER``, kept equal to ``SLICER_DROPDOWN_MIN_H``) and
    seated the rest of the dashboard around the result, so re-flooring the height here would grow a
    band the solver deliberately sized and overrun whatever it placed below -- the same reason
    ``_scale_zone`` takes a solved rect verbatim. The purely horizontal inset still applies: it only
    ever shrinks a card, so it cannot introduce a collision.
    """
    if not entries:
        return
    solved = _LAYOUT_PLAN is not None
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
        if solved:
            box = zone_h
        elif any(e.get("mode") == "Dropdown" for e in row):
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
    # Tableau's ``quick-filter-title`` / ``quick-filter`` style rules live on a WORKSHEET, and a
    # dashboard filter card wears the face of the sheet it BELONGS to -- which the zone names. The
    # token map cannot know that: one field is filtered on many sheets, so keying style by token
    # alone lands on whichever sheet parsed first. Measured on an ATTI/ATTR dashboard: the cards
    # belong to ``Trend ATTI`` (Segoe UI / bold / ``#5a23b9`` / 9pt, grey ``#f5f5f5`` plate) but
    # resolved through ``tech filters``, whose only rule is ``font-size 6`` -- so 55 of 57 captions
    # rebuilt as unreadable 6pt grey with no plate.
    ws_style = {}
    for _ws in ws_list:
        _nm = _ws.get("name")
        if _nm:
            ws_style[_nm] = (_ws.get("filter_hdr_style"), _ws.get("filter_itm_style"),
                             _ws.get("filter_plate_fill"))
    seen = set()
    entries = []
    for i, fz in enumerate(filter_zones):
        f = by_token.get(tuple(fz.get("token") or ()))
        if f is None:
            # Never drop a control in silence: an authored filter card that resolves to no field is
            # a missing interaction on the rebuilt dashboard, and the reader must be told which one.
            if warnings is not None:
                _tok = ".".join(str(p) for p in (fz.get("token") or ())) or "(no token)"
                warnings.append(_warn(
                    "dashboard", page_name,
                    f"filter card {_tok} resolved to no model field (slicer not rebuilt)"))
            continue
        key = (f["entity"], f["property"])
        if key in seen:
            continue
        seen.add(key)
        _own = ws_style.get(fz.get("owner"))
        if _own and any(_own):
            # Copy so two cards on different sheets can wear different faces for the same field.
            _hdr, _itm, _plate = _own
            f = dict(f)
            if _hdr:
                f["_slicer_hdr"] = _hdr
            if _itm:
                f["_slicer_itm"] = _itm
            if _plate:
                f["_slicer_plate"] = _plate
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
            vname, e["f"], _position(e["x"], e["y"], e["w"], e["h"], z=_Z_SLICER, tab=100 + e["i"]),
            model_table, field_map, mode=e["mode"], warnings=warnings))
    return visuals


def _emit_slicers(ws_list, page_name, model_table, field_map, warnings=None, shown_tokens=None,
                  filter_zones=None, ref_w=None, ref_h=None, filter_ws=None):
    """Emit the page's filter slicers.

    On a dashboard page ``filter_zones`` carries the parsed filter *cards* (geometry + Tableau
    ``mode`` + ``hidden`` flag); each is placed faithfully at its own scaled zone with the right
    dropdown/List mode and no page-height cap (see :func:`_emit_dashboard_slicers`). The standalone
    worksheet-page surface has no dashboard card geometry, so ``filter_zones`` is ``None``/empty
    there and the original synthetic right-rail stack is kept byte-for-byte (``shown_tokens`` gate
    unchanged).

    ``filter_ws`` is the worksheet list used to RESOLVE card tokens; it is broader than ``ws_list``
    (which is the rendered set) because a filter-host worksheet contributes cards without
    contributing a visual. It falls back to ``ws_list`` when not supplied."""
    if filter_zones:
        return _emit_dashboard_slicers(
            filter_ws if filter_ws is not None else ws_list,
            page_name, model_table, field_map, filter_zones, ref_w, ref_h, warnings)
    visuals = []
    fields = _filter_slicer_fields(ws_list, shown_tokens)
    for i, f in enumerate(fields):
        y = 40 + i * 120
        if y > PAGE_HEIGHT - 120:
            break
        vname = _sanitize(f"slicer-{page_name}-{i}-{f['property']}")
        visuals.append(_slicer_json(
            vname, f, _position(PAGE_WIDTH - 220, y, 200, 100, z=_Z_SLICER, tab=100 + i),
            model_table, field_map, warnings=warnings))
    return visuals


def _param_control_selection(pc):
    """Tableau's CURRENT parameter value -> a single-member slicer pre-selection, else ``None``.

    A Tableau parameter control always opens on the value the author saved; a Power BI slicer with no
    selection opens on "All". That difference is not cosmetic. The model rebuilds a list parameter as
    a picker table keyed on the human-facing label, and downstream DAX reads it with
    ``SELECTEDVALUE(<picker>, <authored default>)`` -- so an unselected slicer shows "All" while the
    numbers silently come from the default, and the face of the report disagrees with its own data.
    For a FIELD parameter it is worse than cosmetic: an empty selection leaves EVERY field active at
    once, so the visual breaks down by a different dimension than the source workbook.

    Warn-never-wrong gate. A pre-selection is emitted only when the literal is provably inside the
    bound column's domain:

      * the parameter exposes aliased members (a genuine list parameter -- the only shape the model
        rebuilds as a label-keyed picker), and
      * its current display is one of those aliases, and
      * that display differs from the raw stored value, which is precisely what makes the alias the
        human-facing text the picker column holds (``2`` -> ``"Program Name"``).

    A plain string-list parameter whose stored value already IS the picker label (no alias) is also
    safe: the current value itself must appear in the member list. Date/numeric scalars still decline
    here; they use the dedicated exact-literal path in :func:`_param_control_preselection_object`.
    """
    meta = (pc or {}).get("param_meta") or {}
    disp, val = meta.get("current_display"), meta.get("current_value")
    members = {m.get("value") for m in (meta.get("members") or []) if m.get("value") is not None}
    aliases = {m.get("alias") for m in (meta.get("members") or []) if m.get("alias")}
    if disp and disp != val and disp in aliases:
        return {"mode": "include", "values": [disp]}
    if val in members:
        return {"mode": "include", "values": [val]}
    return None


def _comparison_preselection_object(entity, prop, literal):
    """One exact-value slicer preselection object (``column == literal``)."""
    return {"properties": {"filter": {"filter": {
        "Version": 2,
        "From": [{"Name": _FILTER_SOURCE_ALIAS, "Entity": entity, "Type": 0}],
        "Where": [{"Condition": {"Comparison": {
            "ComparisonKind": 0,
            "Left": _filter_column_ref(entity, prop, source=_FILTER_SOURCE_ALIAS),
            "Right": {"Literal": {"Value": literal}},
        }}}],
    }}}}


def _param_control_preselection_object(pc, res):
    """A model-resolved scalar parameter control's exact open-on selection, else ``None``.

    Global rule, not caption-based: when a dashboard parameter control is bound to a real picker
    column and the workbook proves its current saved value, open the rebuilt slicer on that value.
    The proof comes from the parameter's datatype + saved literal + resolved picker column, so the
    same logic covers every workbook that uses the same parameter class.

    Field parameters and aliased string list parameters already flow through the categorical
    selection path above; this helper is for the scalar cases that were previously left at ``All``:
    date/datetime pickers and numeric value pickers.
    """
    meta = (pc or {}).get("param_meta") or {}
    entity, prop = (res or {}).get("table"), (res or {}).get("column")
    if not entity or not prop:
        return None
    dtype = ((pc or {}).get("datatype") or "").lower()
    cur = meta.get("current_value")
    if dtype in ("date", "datetime"):
        lit = _semantic_datetime_literal(cur)
        return _comparison_preselection_object(entity, prop, lit) if lit else None
    if dtype in ("integer", "real"):
        lit = _semantic_numeric_literal(cur)
        return _comparison_preselection_object(entity, prop, lit) if lit else None
    return None


def _field_param_selection(res):
    """A field-parameter control's open-on selection, expressed on its GROUP-BY column.

    The model hands the report a ``select`` ``{column, value}`` for a field-parameter picker: the
    hidden ``<table> Fields`` column plus the ``NAMEOF`` argument text of the branch Tableau opens
    on. Both come from the entry that wrote the partition row, so the literal is inside the built
    column's domain by construction -- the property that makes a pre-selection safe to emit.

    Why not the visible display column: rendering one slicer four ways on a single page (display
    column / group-by column / composite of both / order column) left the group-by variant as the
    only one that opened on the authored value; the other three all read "All".
    """
    sel = (res or {}).get("select")
    if not isinstance(sel, dict):
        return None
    col, val = sel.get("column"), sel.get("value")
    if not col or val in (None, ""):
        return None
    return {"property": col, "values": [val]}


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
        # Open the slicer on the parameter value the author saved. Two disjoint routes:
        # a FIELD parameter is selected through its group-by column (``preselect_override``), while
        # a VALUE parameter is selected on the picker column it already projects, but only when that
        # literal provably exists there (see :func:`_param_control_selection`). ``datatype`` is
        # declared only alongside a value selection so the verified string-categorical path is the
        # one that runs; a decline on both routes stays byte-identical to before.
        override = _field_param_selection(res)
        sel = None if override else _param_control_selection(pc)
        pre_obj = None if (override or sel) else _param_control_preselection_object(pc, res)
        field = {"entity": res["table"], "property": res["column"], "binding": "column",
                 "caption": res.get("caption") or res["column"], "aggregation": None,
                 "selection": sel, "range": None,
                 "preselect_override": override,
                 "preselect_object": pre_obj,
                 "datatype": "string" if sel else None,
                 "preselect_only": True}
        vname = _sanitize(f"paramslicer-{page_name}-{i}-{res['column']}")
        # A Tableau parameter control is a single-value picker; its ``mode`` (``compact`` -> dropdown)
        # decides the slicer face. Without an explicit mode Power BI renders its default vertical
        # List (a stack of buttons), which does not read as the dropdown the source control is --
        # so map the captured mode (defaulting to the compact Dropdown Tableau itself defaults to).
        slicer_mode = _tableau_param_control_mode_to_pbi(pc.get("mode"))
        visuals.append(_slicer_json(
            vname, field, _position(x, y, w, h, z=_Z_SLICER, tab=200 + i),
            None, None, mode=slicer_mode, warnings=warnings))
    return visuals


# A slicer band must OVERLAP a sheet by at least this fraction of the band's own height before the
# shown-state reflow treats it as a collision. Below it the overlap is a graze -- and on a real
# dashboard a graze is usually OUR OWN doing, since the emit step floors a slicer's height and can
# push a band past a sheet the solver placed clear of it. Measured endpoints: the case the reflow
# exists for overlaps 72% of the band; the false positive that motivated this, 7%.
_REFLOW_MIN_OVERLAP = 0.25


def _reflow_worksheets_below_slicers(visuals, page_h, *, gap=8.0, tol=1.0):
    """Reproduce Tableau's SHOWN-state reflow when surfaced slicers collide with worksheet content.

    On a dashboard whose filter band is ``hidden-by-user`` (collapsed behind the funnel icon), Tableau
    reflows the sheets UP to fill the freed space, so the authored zone coords put the sheets where the
    filters would be. We choose to SHOW those filters as slicers (Power BI has no collapse toggle), which
    reintroduces the band -- so a sheet authored at the hidden-state position now overlaps the slicers
    (the Sample card at y=241 under a filter band at y~211-320). This mirrors what Tableau itself does the
    moment you click "Show Filters": the sheet stack is pushed BELOW the band and compressed to fit the
    remaining canvas (Sample -> y~351, h~285). We reflow the worksheet-content visuals into
    ``[band_bottom+gap, page_h]`` proportionally, keeping their relative layout.

    Guard: only fires when a worksheet visual actually intersects the slicer band ON BOTH AXES -- a
    dashboard whose slicers sit in their own clear band (no overlap) is untouched (never-regress),
    and so is a sheet that merely sits at the band's HEIGHT in a side column the band never reaches.
    Slicers, the banner and the backdrop plate are never moved; only worksheet content is reflowed.

    Banding: a dashboard can surface TWO separate slicer strips -- a top filter row and a lower
    ``Selections``/parameter-control row -- with authored content (e.g. a KPI-card band) SANDWICHED
    between them. Collapsing every slicer into one ``min(top)..max(bottom)`` band makes that band span
    the gap and swallow the sandwiched content, shoving it far below and scrambling the layout. Instead
    the slicers are grouped into CONTIGUOUS vertical bands (a new band starts once the next slicer's top
    is more than one slicer-height below the running band bottom), and we reflow only against the
    topmost band that content actually collides with. A dashboard with a single slicer strip is
    unaffected (one band == the old behaviour)."""
    slicers = [v for v in visuals if (v.get("position") or {}).get("z") == _Z_SLICER]
    content = [v for v in visuals if (v.get("position") or {}).get("z") == _Z_CONTENT]
    if not slicers or not content:
        return
    slicers.sort(key=lambda v: v["position"]["y"])
    cluster_gap = max(v["position"]["height"] for v in slicers)
    bands = []
    for v in slicers:
        top = v["position"]["y"]
        bot = top + v["position"]["height"]
        if bands and top <= bands[-1][1] + cluster_gap:
            bands[-1][1] = max(bands[-1][1], bot)
            bands[-1][2] = min(bands[-1][2], v["position"]["x"])
            bands[-1][3] = max(bands[-1][3], v["position"]["x"] + v["position"]["width"])
        else:
            bands.append([top, bot, v["position"]["x"],
                          v["position"]["x"] + v["position"]["width"]])
    band = None
    for band_top, band_bottom, band_left, band_right in bands:
        band_h = max(1.0, band_bottom - band_top)
        # A collision needs BOTH axes, and it needs to be MATERIAL.
        #
        # Testing only Y asks "is this sheet at the band's height", which is true of every sheet in
        # a side column the band never reaches -- and one such sheet then reflows the WHOLE page.
        # Measured on Salesforce NPSP: a full-height left column at x=0..439 spanning y=72..768
        # shares ZERO horizontal space with a filter band at x=505..962, and triggered a reflow that
        # compressed every other visual to 87.5% and pushed it down 88px, moving the KPI band from
        # its solved y=163..273 down to 239..336 where it overran a parameter row it had not
        # touched. The reflow's own arithmetic predicted 11 of that page's 12 emitted rects to
        # within 1.5px, so it accounted for the entire vertical error.
        #
        # Materiality matters for a second, subtler reason: the emit step FLOORS a slicer to a
        # minimum height (45 -> 57 px on that dashboard), so a band can grow past a sheet that the
        # SOLVER placed clear of it. On the same page the parameter band then claimed a 4.0 px
        # overlap -- 2.9% of the sheet, and 0.0 px against the band's own solved extent -- and
        # compressed the page to 61%. That is the reflow reacting to damage the emit step did one
        # step earlier, not to anything the author wrote.
        #
        # The threshold is not tuned to that instance: the case this pass EXISTS for overlaps 79 px
        # of a 109 px band (72%), and the false positive 4 px of a 57 px band (7%). Any value in a
        # very wide interval separates them; a quarter of the band's height is a round one.
        #
        # Both guards can only ever remove FALSE positives -- a surfaced slicer genuinely drawn on
        # top of a sheet overlaps on both axes and by much more than a graze.
        if any(v["position"]["x"] < band_right - tol
               and v["position"]["x"] + v["position"]["width"] > band_left + tol
               and (min(v["position"]["y"] + v["position"]["height"], band_bottom)
                    - max(v["position"]["y"], band_top)) > _REFLOW_MIN_OVERLAP * band_h
               for v in content):
            band = (band_top, band_bottom)
            break
    if band is None:
        return
    band_top, band_bottom = band
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


def _demote_occluding_overlays(visuals, warnings=None, dashboard=None):
    """Fail-closed backstop: an image that would BLANK the page cannot be an overlay.

    ``_image_z`` decides an image's layer from Tableau's document paint order, which is the
    author's real declaration of intent and is right on every workbook measured. But it is a
    HEURISTIC over a signal that can be absent or misleading, and its two fallbacks both return
    the overlay layer -- so it fails toward the single most destructive outcome this rebuild has:
    a full-canvas background plate painted on top of every chart, which renders the page
    completely blank while every visual underneath is correctly built, bound and populated. That
    failure is invisible to schema validation (a fully-occluded page validates with zero errors)
    and it masquerades as a data-binding bug, so it costs far more to diagnose than to prevent.

    Geometry settles it independently of document order: if an emitted image would cover a data
    visual whole, it CANNOT be decoration, because the source dashboard would have been just as
    blank in Tableau and no author ships that. Such an image is therefore the page's backdrop and
    is demoted beneath the content. The test is containment, not size, which is what keeps the
    legitimate case intact -- a corner logo or a 17x18 icon cannot contain a chart, so overlays
    stay on top; only a plate big enough to erase content is moved.

    Ordering is a no-op by construction: the demoted plate lands on the same layer a correctly
    ordered backdrop would already occupy, so this only ever converts a blank page into the page
    the author designed, never rearranges a page that was already right.
    """
    imgs = [v for v in visuals
            if (v.get("position") or {}).get("z", 0) > _Z_CONTENT
            and ((v.get("visual") or {}).get("visualType") == "image")]
    content = [v for v in visuals
               if (v.get("position") or {}).get("z") in (_Z_CONTENT, _Z_SLICER)]
    if not imgs or not content:
        return []

    def _rect(v):
        p = v["position"]
        return (p["x"], p["y"], p["x"] + p["width"], p["y"] + p["height"])

    def _contains(outer, inner, tol=1.0):
        return (outer[0] <= inner[0] + tol and outer[1] <= inner[1] + tol
                and outer[2] >= inner[2] - tol and outer[3] >= inner[3] - tol)

    demoted = []
    for img in imgs:
        r = _rect(img)
        hidden = [c for c in content if _contains(r, _rect(c))]
        if not hidden:
            continue
        img["position"]["z"] = _Z_BACKDROP
        demoted.append({"visual": img.get("name"), "hidden": len(hidden)})
        if warnings is not None:
            warnings.append(_warn(
                "dashboard", dashboard,
                "background image re-layered beneath %d visual(s) it would have hidden "
                "(Tableau paint order implied an overlay)" % len(hidden)))
    return demoted


def _deoverlap_captions(visuals, page_w, page_h, *, gap=8.0, tol=1.0, min_frac=0.05):
    """Lift a floating caption textbox off any content it overlaps into the nearest clear band.

    Tableau habitually FLOATS a section-header / panel-label / instruction text zone directly on
    top of (or fully inside) the worksheet, table or slicer row it labels. The deterministic rebuild
    scales those zones faithfully (``z==900``), so it inherits every source overlap -- empirically
    the dominant geometry defect on real dashboards (100% of measured caption overlaps are a caption
    textbox sitting on an anchor: chart column-headers pinned inside their bars at 98-100%, or a wide
    section header stretched behind a row of slicers at ~41-47%). Faithful pixel placement is NOT a
    hard invariant here (only completeness, correct numbers and faithful graphs are) -- so we
    relocate the CAPTION, never the content, into a readable strip: preferring the gap directly ABOVE
    the anchor it labels (a clean title row), then directly below, then the closest clear horizontal
    band. Only ``x``/``y`` change; the caption is never dropped, resized or restyled (v2-1 already
    sized it to its own text), so completeness is preserved.

    Never-regress gate: overlaps are computed FIRST and the pass returns immediately when no caption
    sits on an anchor, so an already-tidy page (e.g. a cleanly tiled dashboard) is byte-identical.
    A caption is moved only to a position proven clear of every anchor and every other caption, and
    is otherwise left exactly where it was -- the page can only ever get tidier, never worse. Anchors
    (worksheets, slicers, the banner and overlay images) are never moved.

    The backdrop plate is deliberately NOT an anchor. It is the author's page design, drawn beneath
    everything and routinely covering the whole canvas -- treating it as content to avoid would leave
    no clear band anywhere on the page and silently switch this pass off for exactly the elaborately
    designed dashboards that need it most. A caption is MEANT to sit on the plate."""
    caps = [v for v in visuals if (v.get("position") or {}).get("z") == _Z_CAPTION]
    anchors = [v for v in visuals
               if (v.get("position") or {}).get("z") not in (_Z_CAPTION, _Z_BACKDROP)]
    if not caps or not anchors:
        return page_h

    def _r(v):
        p = v["position"]
        return (p["x"], p["y"], p["width"], p["height"])

    def _inter(a, b):
        ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
        return ix * iy

    def _hits(caprect):
        floor = max(tol, min_frac * (caprect[2] * caprect[3]))
        return [(a, _inter(caprect, _r(a))) for a in anchors
                if _inter(caprect, _r(a)) > floor]

    # Gate: do nothing unless at least one caption actually sits on an anchor.
    if not any(_hits(_r(c)) for c in caps):
        return page_h

    caps.sort(key=lambda v: (v["position"]["y"], v["position"]["x"]))

    def _clear(rect, self_v):
        if rect[0] < -tol or rect[1] < -tol:
            return False
        if rect[0] + rect[2] > page_w + tol or rect[1] + rect[3] > page_h + tol:
            return False
        for a in anchors:
            if _inter(rect, _r(a)) > tol:
                return False
        for other in caps:
            if other is self_v:
                continue
            if _inter(rect, _r(other)) > tol:
                return False
        return True

    for cap in caps:
        cr = _r(cap)
        hits = _hits(cr)
        if not hits:
            continue
        target = max(hits, key=lambda t: t[1])[0]
        tr = _r(target)
        x, w, h = cr[0], cr[2], cr[3]
        # Candidate y positions (caption keeps its width/height + x, staying aligned with its
        # content), in preference order: directly above the labelled anchor, directly below it,
        # then the clear strip nearest the caption's original y.
        candidates = []
        candidates.append(max(0.0, tr[1] - gap - h))
        yb = tr[1] + tr[3] + gap
        candidates.append(min(yb, page_h - h))
        strips = [0.0] + [_r(a)[1] + _r(a)[3] + gap for a in anchors]
        strips = [s for s in strips if -tol <= s <= page_h - h + tol]
        strips.sort(key=lambda s: abs(s - cr[1]))
        candidates.extend(strips)
        for y in candidates:
            if _clear((x, y, w, h), cap):
                cap["position"]["x"] = round(x, 2)
                cap["position"]["y"] = round(y, 2)
                break

    # v2-8 band-insertion fallback. On a PACKED page every horizontal strip is occupied, so the
    # relocation loop above finds NO clear candidate and leaves the caption stuck on top of the
    # content it labels -- the real-workbook overlap defect. Rather than inherit that overlap, OPEN a
    # band for the caption: shift the content block it heads (and everything beneath) straight DOWN by
    # the caption's height + gap, then seat the caption in the cleared strip directly above that
    # block. Only ``y`` changes and only ever downward; nothing is resized or dropped, so completeness
    # and every side-by-side (horizontal) arrangement are preserved. Side-by-side captions that head
    # the SAME band (e.g. the left/right headers of a two-up crosstab) share ONE inserted band instead
    # of stacking. The page grows to keep the pushed content in-bounds -- the page is emitted
    # ``FitToPage``, so a taller canvas simply rescales to the viewport (no scrollbar). Deterministic:
    # stuck captions are processed top-to-bottom against LIVE positions so successive insertions
    # compose; a caption whose opened band would clip a non-shifting item above it is left untouched
    # (never made worse), and the loop is hard-bounded so it always terminates.
    def _line_for(cap):
        cr = _r(cap)
        hits = _hits(cr)
        if not hits:
            return None
        tops = [_r(a)[1] for a, _ in hits]
        below = [t for t in tops if t >= cr[1] - tol]
        return round(min(below) if below else min(tops), 2)

    skip = set()
    _iter = 0
    while _iter < len(caps) * 2 + 4:
        _iter += 1
        stuck = [c for c in caps if id(c) not in skip and _line_for(c) is not None]
        if not stuck:
            break
        stuck.sort(key=lambda v: (v["position"]["y"], v["position"]["x"]))
        line = _line_for(stuck[0])
        # Group every stuck caption that heads this same band and does not horizontally collide with an
        # already-grouped member, so a row of side-by-side headers opens a single shared band.
        group, spans = [], []
        for c in stuck:
            if _line_for(c) != line:
                continue
            cx0 = c["position"]["x"]
            cx1 = cx0 + c["position"]["width"]
            if any(not (cx1 <= sx0 + tol or cx0 >= sx1 - tol) for sx0, sx1 in spans):
                continue
            group.append(c)
            spans.append((cx0, cx1))
        gx0 = min(s[0] for s in spans)
        gx1 = max(s[1] for s in spans)
        delta = round(max(c["position"]["height"] for c in group) + gap, 2)
        # Guard: never open a band that would clip a non-shifting item straddling the insertion line.
        clip = any(
            (not any(v is g for g in group))
            and v["position"]["y"] < line - tol
            and v["position"]["y"] + v["position"]["height"] > line + tol
            and not (v["position"]["x"] + v["position"]["width"] <= gx0 + tol
                     or v["position"]["x"] >= gx1 - tol)
            for v in visuals)
        if clip:
            skip.update(id(c) for c in group)
            continue
        for v in visuals:
            if any(v is g for g in group):
                continue
            if v["position"]["y"] >= line - tol:
                v["position"]["y"] = round(v["position"]["y"] + delta, 2)
        for c in group:
            c["position"]["y"] = round(line, 2)

    # The backdrop plate is excluded from the measurement for the same reason it is excluded from
    # the anchor set: it is the page's canvas artwork, not content. Letting it drive the height
    # makes a plate that scaled a few pixels proud of the authored canvas silently inflate the
    # page, which pushes the real content off-screen -- the opposite of what growing is for.
    content_bottom = max((v["position"]["y"] + v["position"]["height"] for v in visuals
                          if (v.get("position") or {}).get("z") != _Z_BACKDROP),
                         default=page_h)
    return round(max(page_h, content_bottom + gap), 2)


def migrate_twb_to_pbir(xml_text, *, dataset_name="Model", report_name="Report",
                        model_table=None, field_map=None, date_binding=None,
                        row_count_binding=None, measure_binding=None, column_binding=None,
                        param_binding=None, resources=None, layout=LAYOUT_DEFAULT):
    """One-call convenience: parse ``.twb`` text and emit the PBIR parts.

    Returns ``{"ir": ..., "parts": ..., "warnings": ..., "candidate_records": ...,
    "worklist": ...}``. ``parts`` is the ``{relative_path: text}`` PBIR definition; write it to a
    ``<report_name>.Report`` folder or base64-encode each part for the Fabric report *Update
    Definition* API. ``worklist`` (additive, present when ``remediation_worklist`` is importable) is
    the deterministic per-visual remediation audit -- a structured, full-dashboard superset of
    ``warnings`` -- and never affects the emitted PBIR.

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
                   param_binding=param_binding, layout=layout, workbook_name=report_name)
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
    result = {"ir": ir, "parts": parts, "warnings": ir["warnings"],
              "candidate_records": ir.get("candidate_records", [])}
    # Additive per-visual remediation worklist (folds warnings + candidate_records into a structured,
    # full-dashboard audit). Fail-open: never blocks the report emission, never changes the PBIR.
    if _build_worklist is not None:
        try:
            result["worklist"] = _build_worklist(result["warnings"], result["candidate_records"])
        except Exception:  # pragma: no cover - defensive; worklist is advisory
            pass
    return result


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
    parser.add_argument(
        "--worklist", default=os.environ.get("TWB_PBIR_WORKLIST"), metavar="PATH",
        help="optional: also write the deterministic per-visual remediation worklist JSON here "
             "(a structured, full-dashboard superset of the warnings). Never changes the PBIR.")
    parser.add_argument(
        "--audit", default=os.environ.get("TWB_PBIR_AUDIT"), metavar="PATH",
        help="optional: also write the opt-in Tier-3 dashboard AUDIT request JSON here (folds the "
             "worklist + chart-type advice into a full-dashboard, priority-ordered audit for the "
             "assisted tier). Built on demand only; never changes the PBIR.")
    parser.add_argument(
        "--layout", choices=LAYOUT_ENGINES,
        default=os.environ.get("TWB_PBIR_LAYOUT", LAYOUT_DEFAULT),
        help="dashboard layout engine. 'legacy' (default) scales each zone's absolute Tableau rect "
             "independently and repairs collisions afterwards. 'solver' resolves the dashboard's "
             "zone TREE (flow containers + frames) so flow overlap is structurally impossible, and "
             "adopts the page size that solve resolved. Opt-in; 'legacy' output is unchanged.")
    args = parser.parse_args(argv)

    if args.input == "-":
        xml_text = sys.stdin.read()
    else:
        with open(args.input, "r", encoding="utf-8-sig") as fh:
            xml_text = fh.read()

    result = migrate_twb_to_pbir(
        xml_text, dataset_name=args.dataset, report_name=args.report,
        model_table=args.model_table, layout=args.layout)
    parts, warnings = result["parts"], result["warnings"]

    if args.worklist and result.get("worklist") is not None:
        wl_parent = os.path.dirname(args.worklist)
        if wl_parent:
            os.makedirs(wl_parent, exist_ok=True)
        with open(args.worklist, "w", encoding="utf-8") as fh:
            json.dump(result["worklist"], fh, indent=2, ensure_ascii=False)
        s = result["worklist"]["summary"]
        print("wrote remediation worklist: {0} item(s) across {1} visual(s) ({2} flagged) to {3}"
              .format(s["items_total"], s["visuals_total"], s["visuals_flagged"], args.worklist),
              file=sys.stderr)

    if args.audit and _build_dashboard_audit is not None and result.get("worklist") is not None:
        try:
            audit = _build_dashboard_audit(result["worklist"], result.get("candidate_records", []))
        except Exception:  # pragma: no cover - advisory; audit never blocks the emit
            audit = None
        if audit is not None:
            au_parent = os.path.dirname(args.audit)
            if au_parent:
                os.makedirs(au_parent, exist_ok=True)
            with open(args.audit, "w", encoding="utf-8") as fh:
                json.dump(audit, fh, indent=2, ensure_ascii=False)
            a_s = audit["summary"]
            print("wrote dashboard audit: {0} visual(s), {1} need attention to {2}"
                  .format(a_s["visuals"], a_s["needs_attention"], args.audit), file=sys.stderr)

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
