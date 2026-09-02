"""Pure-Python PBIR report well-formedness linter -- a dependency-free validity guard.

Complements :mod:`tmdl_lint` (which guards the MODEL / ``.tmdl`` parts) by guarding the REPORT /
PBIR parts that :func:`twb_to_pbir.emit_pbir` produces. It re-checks -- with ZERO third-party
dependencies, so it runs inside the ordinary pytest gate as a fast, always-on regression guard --
the two static defects the Microsoft ``powerbi-report-author validate`` CLI flags on our output:

  1. VISUAL TYPE VALIDITY (R4) -- every ``visual.json`` ``visual.visualType`` must be a known
     built-in PBIR visual type. An unknown type renders in Power BI as a MISSING custom visual. The
     classic trap: Power BI spells a stacked column/bar as the UNQUALIFIED ``columnChart`` /
     ``barChart``; the look-alikes ``stackedColumnChart`` / ``stackedBarChart`` are NOT valid PBIR
     types and trip ``PBIR_VISUAL_TYPE_UNKNOWN``.
  2. THEME NAME CONSISTENCY (R3) -- when ``report.json`` registers a ``customTheme``, its
     ``customTheme.name`` must (a) end in ``.json``, (b) exactly equal the matching
     ``RegisteredResources`` item ``name`` AND ``path``, and (c) equal the bundled theme file's own
     internal ``name``. Any mismatch makes the theme fail to load, silently dropping the palette
     (``PBIR_THEME_FILE_NAME_MISMATCH`` / ``PBIR_THEME_NAME_MISSING_JSON_EXT``).

Plus two FIDELITY / VALIDITY guards on the emitter's own output:

  3. CARD DISPLAY UNITS (R5) -- a ``card`` must pin its value object ``labels.labelDisplayUnits``
     to None (``1D``). Power BI defaults it to Auto (0), which abbreviates the big number
     (2,747 -> "3K"); this guard catches a regression that leaves it Auto or unset, so a migrated KPI
     never silently abbreviates versus the Tableau text / BAN mark. (A ``multiRowCard``'s value object
     ``dataLabels`` has no display-units channel, so R5 is scoped to ``card`` only.)
  4. NATIVE QUERY REF UNIQUENESS (R6) -- every projection in ONE visual's ``queryState`` must carry a
     DISTINCT ``nativeQueryRef``. Two fields from different tables that share a column name (e.g.
     ``Program[Name]`` + ``Service[Name]``) otherwise both serialize ``'Name'`` and the visual query
     collides -> "Error fetching data" at render. The emitter uniquifies them; this guard catches a
     regression that lets a duplicate native name slip back into a single visual.

The valid-visual-type catalog below was ground-truthed against ``powerbi-report-author validate``
v0.1.4: every type the emitter can produce was confirmed KNOWN, and only genuinely invalid strings
trip ``PBIR_VISUAL_TYPE_UNKNOWN`` (distinct from role-binding diagnostics). It is deliberately
conservative -- a clean result means "free of these two known PBIR validity defects", not "provably
valid"; the authoritative external ``validate`` CLI remains the opt-in deeper check in
:mod:`fidelity_oracle`. Fail-safe throughout: a malformed or absent part is skipped, never raised on.
"""
from __future__ import annotations

import json

# The hand-curated part of the built-in ``visualType`` catalog: types that carry NO required data
# roles (text, shapes, buttons, images, AI visuals) and therefore never appear in the harvested
# role tables below. Every value here was ground-truthed KNOWN against the Microsoft
# ``powerbi-report-author validate`` CLI (v0.1.4). The invalid look-alikes the emitter must NEVER
# produce are deliberately ABSENT: "stackedColumnChart", "stackedBarChart" (Power BI spells those as
# the unqualified "columnChart" / "barChart").
#
# THIS IS NOT THE VALIDITY SET. ``VALID_VISUAL_TYPES`` is derived from this UNION the harvested
# tables -- see its definition below and #179 for why: a hand-maintained validity set drifted
# narrower than the catalog and reported five real Power BI visuals as "unknown visualType", among
# them ``matrix`` and ``table``.
_CURATED_VISUAL_TYPES = frozenset({
    # column / bar family (unqualified column/bar ARE the stacked variants)
    "columnChart", "barChart", "clusteredColumnChart", "clusteredBarChart",
    "hundredPercentStackedColumnChart", "hundredPercentStackedBarChart",
    # line / area
    "lineChart", "areaChart", "stackedAreaChart",
    # column+line combos
    "lineClusteredColumnComboChart", "lineStackedColumnComboChart",
    # point / part-to-whole / rank / flow
    "scatterChart", "pieChart", "donutChart", "treemap",
    "funnel", "gauge", "kpi", "ribbonChart", "waterfallChart",
    # tables
    "tableEx", "pivotTable",
    # maps
    "map", "filledMap", "shapeMap", "azureMap",
    # cards
    "card", "multiRowCard",
    # slicers
    "slicer", "listSlicer", "textSlicer", "advancedSlicerVisual",
    # text / shapes / buttons / images
    "textbox", "image", "actionButton", "basicShape",
    # analytics / AI
    "decompositionTreeVisual", "keyDriversVisual", "qnaVisual", "aiNarratives",
})

_THEME_DIR = "StaticResources/RegisteredResources/"


def _as_text(value):
    """Coerce a part value (``str`` or ``bytes``) to text for JSON parsing; ``None`` if undecodable."""
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8-sig")
        except Exception:
            return None
    return value


def _load_json(parts, key):
    raw = _as_text(parts.get(key))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _lint_visual_types(parts):
    problems = []
    for path in sorted(parts):
        if not path.endswith("visual.json"):
            continue
        doc = _load_json(parts, path)
        if not isinstance(doc, dict):
            continue
        visual = doc.get("visual")
        vt = visual.get("visualType") if isinstance(visual, dict) else None
        if vt and vt not in VALID_VISUAL_TYPES:
            problems.append(
                "%s: unknown visualType %r -- not a valid PBIR built-in visual type "
                "(Power BI renders it as a missing custom visual)" % (path, vt))
    return problems


# Card value display units (fidelity R5): a Power BI ``card`` defaults its big-number
# ``labelDisplayUnits`` to Auto (0), which ABBREVIATES (2,747 -> "3K"). Setting it to Auto (0) does
# NOT disable the abbreviation -- "None" is the enum value 1 (emitted as ``1D``). The emitter forces
# None on every rebuilt ``card`` via its value object ``labels`` (see
# ``twb_to_pbir._apply_card_display_units``); this guard catches a regression that drops it or leaves
# Auto, so a migrated KPI never silently abbreviates its value versus the Tableau text / BAN mark. A
# ``multiRowCard``'s value object is ``dataLabels``, which has NO ``labelDisplayUnits`` channel
# (verified against ``formatting describe-object multiRowCard dataLabels``), so display units cannot be
# pinned there and R5 is inapplicable to it -- the guard is scoped to ``card`` only.
_CARD_DISPLAY_UNITS_TYPE = "card"


def _card_value_units(visual):
    """The ``card`` value's ``labels.labelDisplayUnits`` literal string (e.g. ``'1D'``), or ``None``."""
    objs = visual.get("objects")
    if not isinstance(objs, dict):
        return None
    for entry in (objs.get("labels") or []):
        props = entry.get("properties") if isinstance(entry, dict) else None
        if not isinstance(props, dict):
            continue
        ldu = props.get("labelDisplayUnits")
        if isinstance(ldu, dict):
            val = ldu.get("expr", {}).get("Literal", {}).get("Value")
            if val is not None:
                return str(val)
    return None


def _units_is_auto(units):
    """True when a ``labelDisplayUnits`` literal resolves to Auto (0), which abbreviates big numbers."""
    core = units.strip().rstrip("DLdl")
    try:
        return float(core) == 0.0
    except ValueError:
        return False


def _lint_card_display_units(parts):
    problems = []
    for path in sorted(parts):
        if not path.endswith("visual.json"):
            continue
        doc = _load_json(parts, path)
        visual = doc.get("visual") if isinstance(doc, dict) else None
        if not isinstance(visual, dict) or visual.get("visualType") != _CARD_DISPLAY_UNITS_TYPE:
            continue
        units = _card_value_units(visual)
        if units is None or _units_is_auto(units):
            problems.append(
                "%s: %s visual must set labels.labelDisplayUnits to None ('1D'); Auto (0) "
                "silently abbreviates the big number (2,747 -> '3K'), breaking fidelity vs the "
                "Tableau text mark" % (path, visual.get("visualType")))
    return problems


# Native-query-ref uniqueness (validity R6): every projection in ONE visual's queryState must carry
# a DISTINCT ``nativeQueryRef``. Two fields from different tables that share a column name (e.g.
# ``Program[Name]`` + ``Service[Name]``) otherwise both serialize ``'Name'`` and the visual query
# collides -> "Error fetching data" at render. The emitter uniquifies them (see
# ``twb_to_pbir._dedupe_native_query_refs``); this guard catches a regression that lets a duplicate
# native name slip back into a single visual.
def _visual_native_refs(visual):
    """Ordered list of every projection ``nativeQueryRef`` across all queryState roles of a visual."""
    refs = []
    query = visual.get("query") if isinstance(visual, dict) else None
    state = query.get("queryState") if isinstance(query, dict) else None
    if not isinstance(state, dict):
        return refs
    for role in state.values():
        if not isinstance(role, dict):
            continue
        for proj in (role.get("projections") or []):
            nref = proj.get("nativeQueryRef") if isinstance(proj, dict) else None
            if nref:
                refs.append(nref)
    return refs


def _lint_native_query_refs(parts):
    problems = []
    for path in sorted(parts):
        if not path.endswith("visual.json"):
            continue
        doc = _load_json(parts, path)
        visual = doc.get("visual") if isinstance(doc, dict) else None
        if not isinstance(visual, dict):
            continue
        refs = _visual_native_refs(visual)
        seen, dupes = set(), []
        for nref in refs:
            if nref in seen and nref not in dupes:
                dupes.append(nref)
            seen.add(nref)
        for nref in dupes:
            problems.append(
                "%s: duplicate nativeQueryRef %r across the visual's projections -- two fields "
                "with the same native name collide in the visual query and render 'Error fetching "
                "data'; qualify one with its source entity" % (path, nref))
    return problems


def _lint_page_order(parts):
    """Validity R7: the report must declare at least one page.

    A PBIR whose ``pages.json`` ``pageOrder`` is empty does NOT open as an empty report -- Power BI
    Desktop throws ``TypeError: Cannot read properties of undefined (reading 'visualContainers')``
    and refuses the project outright, which also puts the semantic model built beside it out of
    reach. ``powerbi-report-author validate`` does catch this (``PBIR_PAGE_ORDER_EMPTY``), but that
    CLI is an opt-in npm pre-gate an ordinary run never reaches, so the hermetic linter -- which
    runs in the always-on pytest gate -- must catch it too.

    Only checked when a ``pages.json`` is present: a parts dict that carries no report at all (a
    model-only build) is not a page-less report.
    """
    problems = []
    for path in sorted(parts):
        if not path.endswith("pages/pages.json"):
            continue
        doc = _load_json(parts, path)
        if not isinstance(doc, dict):
            continue
        order = doc.get("pageOrder")
        if isinstance(order, list) and not order:
            problems.append(
                "%s: pageOrder is empty -- a PBIR with no pages CRASHES Power BI Desktop on open "
                "('Cannot read properties of undefined (reading 'visualContainers')'), taking the "
                "semantic model beside it out of reach; emit at least a placeholder page" % path)
    return problems


# The signature R8 stamps into its finding, and the roster of findings whose failure mode is
# VALIDATE-CLEAN / RENDER-WRONG: the report opens, `powerbi-report-author validate` reports zero
# errors, the definition-of-done reports success, and the visual is silently unpainted or empty.
#
# Exported so a consumer can treat those findings as FATAL without matching on prose. The
# definition-of-done needs to know WHICH rule fired, and grepping a message is a proxy for that --
# the same proxy-versus-artifact mistake `REQUIRED_ROLES` exists to avoid. This module owns the
# fact; `migrate_estate` reads it. A rule added here becomes fatal with no change on the other side.
_DANGLING_SELECT_REF = "SelectRef names"
SILENT_RENDER_FINDINGS = (_DANGLING_SELECT_REF,)


def _lint_dangling_select_refs(parts):
    """Validity R8: a ``SelectRef`` must name a projection the SAME visual declares.

    ``{"SelectRef": {"ExpressionName": "colourRule"}}`` is how a formatting property points at an
    in-visual expression (a Visual Calculation). If no projection carries that ``queryRef`` the
    property resolves to nothing: the visual renders with default colours, reports no error, and
    passes ``powerbi-report-author validate``.

    :func:`lint_visual_model_bindings` does not cover this -- it proves MODEL references
    (``Measure`` / ``Column``) against the emitted TMDL, and a Visual Calculation is not in the
    model at all. The gap was found the hard way: a first attempt at binding a view-scoped colour
    appended its projection to a query state that the emit path later rebuilt, so half the visuals
    of one workbook shipped a ``SelectRef`` naming a projection that did not exist, and every
    existing gate stayed silent.
    """
    problems = []
    for path in sorted(parts):
        if not path.endswith("visual.json"):
            continue
        doc = _load_json(parts, path)
        visual = doc.get("visual") if isinstance(doc, dict) else None
        if not isinstance(visual, dict):
            continue
        declared, refs = set(), set()
        # ``query`` may be any JSON value in a malformed part; guard the type rather than assuming
        # a dict, or the linter raises on the very input it exists to survive.
        _query = visual.get("query")
        state = (_query.get("queryState") if isinstance(_query, dict) else None) or {}
        if isinstance(state, dict):
            for role in state.values():
                for proj in ((role or {}).get("projections") or []):
                    if isinstance(proj, dict) and proj.get("queryRef"):
                        declared.add(proj["queryRef"])

        def _walk(node):
            if isinstance(node, dict):
                ref = node.get("SelectRef")
                if isinstance(ref, dict) and isinstance(ref.get("ExpressionName"), str):
                    refs.add(ref["ExpressionName"])
                for value in node.values():
                    _walk(value)
            elif isinstance(node, list):
                for value in node:
                    _walk(value)

        _walk(visual)
        for name in sorted(refs - declared):
            problems.append(
                "%s: %s %r, which this visual does not project -- the property "
                "resolves to nothing, the visual renders with its defaults and reports no error"
                % (path, _DANGLING_SELECT_REF, name))
    return problems


def _registered_items(report):
    """Yield each ``RegisteredResources`` item dict, tolerating both the flat
    ``{name,type,items}`` shape the emitter writes and the wrapped ``{resourcePackage:{...}}`` shape
    some hand-authored reports use."""
    for entry in (report.get("resourcePackages") or []):
        if not isinstance(entry, dict):
            continue
        pkg = entry.get("resourcePackage") if "resourcePackage" in entry else entry
        if not isinstance(pkg, dict):
            continue
        for item in (pkg.get("items") or []):
            if isinstance(item, dict):
                yield item


def _lint_theme(parts):
    problems = []
    report_key = next((k for k in sorted(parts) if k.endswith("definition/report.json")
                       or k == "report.json"), None)
    if report_key is None:
        return problems
    report = _load_json(parts, report_key)
    if not isinstance(report, dict):
        return problems
    theme_collection = report.get("themeCollection")
    custom = theme_collection.get("customTheme") if isinstance(theme_collection, dict) else None
    ct_name = custom.get("name") if isinstance(custom, dict) else None
    if not ct_name:
        return problems  # a baseTheme-only report registers no custom theme -> nothing to check

    if not ct_name.lower().endswith(".json"):
        problems.append(
            "report.json customTheme.name %r must include the '.json' extension and exactly match "
            "its RegisteredResources item name and path" % ct_name)

    theme_items = [it for it in _registered_items(report) if it.get("type") == "CustomTheme"]
    if not theme_items:
        problems.append(
            "report.json registers customTheme %r but no RegisteredResources CustomTheme item "
            "declares it (the theme file is never bundled)" % ct_name)
    else:
        matched = [it for it in theme_items
                   if it.get("name") == ct_name or it.get("path") == ct_name]
        target = matched[0] if matched else theme_items[0]
        if target.get("name") != ct_name or target.get("path") != ct_name:
            problems.append(
                "report.json customTheme.name %r must exactly match its RegisteredResources item "
                "name (%r) and path (%r)" % (ct_name, target.get("name"), target.get("path")))

    # locate + validate the bundled theme file's own internal ``name``
    theme_path = None
    for it in theme_items:
        theme_path = it.get("path") or it.get("name")
        if theme_path:
            break
    theme_path = theme_path or ct_name
    theme_key = next((k for k in sorted(parts) if k.endswith(_THEME_DIR + theme_path)), None)
    if theme_key is None:
        problems.append(
            "report.json references theme file %r but it is not bundled under %s"
            % (theme_path, _THEME_DIR))
    else:
        theme_doc = _load_json(parts, theme_key)
        if isinstance(theme_doc, dict):
            internal = theme_doc.get("name")
            if internal != ct_name:
                problems.append(
                    "theme file %r declares internal name %r but report.json references it as %r "
                    "-- the name mismatch stops the theme (and its palette) from loading"
                    % (theme_path, internal, ct_name))
    return problems



def _iter_model_refs(node):
    """Yield ``(kind, entity, property)`` for every model reference in a parsed visual.json.

    Walks the PARSED document rather than the raw text: a visual.json escapes non-ASCII as ``\\uXXXX``
    (a measure named ``... above Goal \u25b2`` is stored escaped), so a regex over the text compares an
    escape sequence against a decoded model name and reports a perfectly valid reference as dangling.
    Measured: 8 such false positives on one workbook before this was parsed properly.
    """
    if isinstance(node, dict):
        for kind in ("Column", "Measure"):
            ref = node.get(kind)
            if isinstance(ref, dict) and isinstance(ref.get("Property"), str):
                src = ((ref.get("Expression") or {}).get("SourceRef") or {})
                entity = src.get("Entity")
                if isinstance(entity, str):
                    yield kind, entity, ref["Property"]
        for value in node.values():
            for hit in _iter_model_refs(value):
                yield hit
    elif isinstance(node, list):
        for value in node:
            for hit in _iter_model_refs(value):
                yield hit


def lint_visual_model_bindings(parts, model_surface):
    """Every ``'Table'[Column]`` / ``[Measure]`` a VISUAL names must exist in the model.

    :mod:`reference_gate` proves this invariant for the DAX the second compiler writes. Nothing
    proved it for the PBIR side, where the same class of defect is *worse*: a visual bound to a
    column the model does not contain does not error and does not fail validation -- Power BI simply
    renders an EMPTY chart. ``powerbi-report-author validate`` returns 0 errors for it, because a
    reference to a missing column is structurally well-formed JSON; only opening the report, or
    running the query by hand, reveals it.

    ``model_surface`` is a :func:`reference_gate.build_model_surface` result (case-insensitive
    lookups over ``{table -> {"columns": {...}, "measures": {...}}}``). Passing ``None`` makes this a
    no-op, so callers without a model in scope are unaffected.

    Deliberately reports the ENTITY as missing only when the model has no such table at all: a
    reference into a table that exists but lacks the column is the more common and more diagnostic
    case, and naming the column is what points at the cause.
    """
    problems = []
    if not isinstance(model_surface, dict):
        return problems
    tables = model_surface.get("tables") or {}
    if not tables:
        return problems
    columns = model_surface.get("columns") or {}
    measure_by_table = model_surface.get("measure_by_table") or {}
    measures = model_surface.get("measures") or {}
    for path, content in sorted((parts or {}).items()):
        if not path.endswith("visual.json") or not isinstance(content, str):
            continue
        try:
            doc = json.loads(content)
        except (ValueError, TypeError):
            continue          # a malformed part is another check's problem, never this one's
        seen = set()
        for kind, entity, prop in _iter_model_refs(doc):
            key = (kind, entity, prop)
            if key in seen:
                continue
            seen.add(key)
            ent_l, prop_l = entity.lower(), prop.lower()
            if ent_l not in tables:
                problems.append(
                    "PBIR_VISUAL_REF_TABLE_MISSING: %s references table %r, which the model does "
                    "not contain (%s %r)" % (path, entity, kind.lower(), prop))
                continue
            if kind == "Measure":
                # A measure is model-global in DAX, so a qualified reference resolves if the model
                # holds that measure anywhere -- checking only the named table would flag a
                # correctly-working reference.
                ok = prop_l in measure_by_table.get(ent_l, {}) or prop_l in measures
            else:
                ok = prop_l in columns.get(ent_l, {})
            if not ok:
                problems.append(
                    "PBIR_VISUAL_REF_MISSING: %s binds %s %r on table %r, which the model does not "
                    "contain -- the visual renders EMPTY and validation reports no error"
                    % (path, kind.lower(), prop, entity))
    return problems


# Roles a visual type cannot render without (#143, #144). HARVESTED from
# ``powerbi-report-author catalog describe`` (v0.1.4) -- the same tool whose
# PBIR_ROLE_REQUIRED_MISSING diagnostic this prevents -- across all 38 visual types that declare
# required roles, so it states what the validator ENFORCES rather than what we believe it enforces.
# A visual type absent here is never judged: "cannot judge" must not become "declare invalid".
#
# SINGLE SOURCE OF TRUTH. ``migrate_estate`` reads this same table to decide whether a projection
# drop left a visual structurally invalid. Two copies would drift, and a gate drifting away from the
# emitter it guards is precisely the defect #137 was.
REQUIRED_ROLES = {
    "advancedSlicerVisual": ("Values",),
    "areaChart": ("Category", "Y"),
    "azureMap": ("Category",),
    "barChart": ("Category", "Y"),
    "card": ("Values",),
    "cardVisual": ("Data",),
    "clusteredBarChart": ("Category", "Y"),
    "clusteredColumnChart": ("Category", "Y"),
    "columnChart": ("Category", "Y"),
    "decompositionTreeVisual": ("Analyze",),
    "donutChart": ("Category", "Y"),
    "filledMap": ("Category",),
    "filterSlicer": ("Values",),
    "funnel": ("Category", "Y"),
    "gauge": ("Y",),
    "hundredPercentStackedAreaChart": ("Category", "Y"),
    "hundredPercentStackedBarChart": ("Category", "Y"),
    "hundredPercentStackedColumnChart": ("Category", "Y"),
    "kpi": ("Indicator",),
    "lineChart": ("Category", "Y"),
    "lineClusteredColumnComboChart": ("Category",),
    "lineStackedColumnComboChart": ("Category",),
    "listSlicer": ("Values",),
    "map": ("Category",),
    "matrix": ("Values",),
    "multiRowCard": ("Values",),
    "pieChart": ("Category", "Y"),
    "pivotTable": ("Values",),
    "ribbonChart": ("Category", "Y"),
    "scatterChart": ("X", "Y"),
    "shapeMap": ("Category",),
    "slicer": ("Values",),
    "stackedAreaChart": ("Category", "Y"),
    "table": ("Values",),
    "tableEx": ("Values",),
    "textSlicer": ("Values",),
    "treemap": ("Values",),
    "waterfallChart": ("Category", "Y"),
}

_REQUIRED_ROLES = REQUIRED_ROLES  # module-internal alias used by the rule below


# --- R10: invisible ink -------------------------------------------------------------------
# EVERY OTHER RULE IN THIS FILE ASKS "IS THIS WELL-FORMED". THIS ONE ASKS "WILL A HUMAN SEE IT",
# and the two are independent: a colour is schema-valid whatever its value, so no validator --
# ours, or Microsoft's ``powerbi-report-author validate`` -- can ever flag a mark painted the same
# colour as the page. There is no malformed artifact to find. The report is perfect and empty.
#
# THE DEFECT THAT MOTIVATED IT (fixed in 2.267.0, guarded here so it cannot return by another
# route). A Tableau donut is a pie with a WHITE CIRCLE punched through its middle; that white is a
# real mark colour in the source, so the palette harvester collected it and it landed at
# ``dataColors[0]`` -- the default series colour for the whole report. On 0090_small_multiples that
# erased FIVE bar charts, the donut's own fourth slice, and one of two series in all four
# time-series panels. ``validate`` clean, ``pbir_lint`` clean, definition-of-done PASS, throughout.
#
# Scope is deliberately narrow, because a false positive here blocks a legitimate build:
#   * only EXPLICIT colours the emitter wrote are judged -- never a Power BI default, which we
#     cannot see and must not guess at;
#   * the comparison is against the background that colour actually sits on, resolved
#     theme-background -> white (Power BI's own default canvas), not an assumed white;
#   * the threshold is "the same colour to the eye", far below any accessibility bar. Tableau's own
#     pale yellow #EDC948 sits at 1.61 on white and must pass -- this rule finds ink that is
#     literally invisible, not ink that is merely low-contrast.
_INK_INVISIBLE_CONTRAST = 1.2


def _hex6(value):
    """``value`` as a ``#rrggbb`` string, or ``None`` if it is not one.

    Power BI writes colours several ways (bare literal, ``solid.color`` expr, theme index). Only a
    literal hex can be judged here; a themed index is resolved by Power BI at render time and
    guessing at it would manufacture false positives.
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if len(v) == 7 and v[0] == "#":
        try:
            int(v[1:], 16)
        except ValueError:
            return None
        return v.lower()
    return None


def _relative_luminance(hex6):
    chans = (int(hex6[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    lin = [(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4) for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast_ratio(a, b):
    """WCAG 2.x contrast ratio between two ``#rrggbb`` colours: 1.0 (identical) .. 21.0."""
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _theme_background(parts):
    """The report theme's declared ``background``, or Power BI's own default canvas white.

    Falling back to white is not an assumption about the workbook -- it is what Power BI actually
    paints when a theme declares no background, which is the surface the ink competes with.
    """
    for path in sorted(parts):
        if not path.endswith(".json") or "RegisteredResources" not in path:
            continue
        doc = _load_json(parts, path)
        if isinstance(doc, dict) and "dataColors" in doc:
            return _hex6(doc.get("background")) or "#ffffff"
    return "#ffffff"


def _lint_invisible_ink(parts):
    """R10: an explicitly emitted colour that is indistinguishable from what it sits on.

    Judges two surfaces, both of which have burned us or could:

      * the report theme's ``dataColors`` -- position 0 is the default series colour for every
        single-series visual, so one invisible entry there is a whole-report outage;
      * a visual's explicit ``dataPoint.defaultColor`` / ``fill`` literal.

    Returns a problem per invisible colour. Never raises.
    """
    problems = []
    background = _theme_background(parts)

    for path in sorted(parts):
        if not path.endswith(".json") or "RegisteredResources" not in path:
            continue
        doc = _load_json(parts, path)
        if not isinstance(doc, dict) or "dataColors" not in doc:
            continue
        for idx, raw in enumerate(doc.get("dataColors") or []):
            hexed = _hex6(raw)
            if hexed and contrast_ratio(hexed, background) < _INK_INVISIBLE_CONTRAST:
                problems.append(
                    "%s: theme dataColors[%d] = %s is invisible on the %s page background "
                    "(contrast %.2f) -- Power BI paints series with it and the marks disappear; "
                    "at index 0 this silently blanks every single-series visual in the report"
                    % (path, idx, hexed, background, contrast_ratio(hexed, background)))

    for path in sorted(parts):
        if not path.endswith("visual.json"):
            continue
        doc = _load_json(parts, path)
        if not isinstance(doc, dict):
            continue
        visual = doc.get("visual")
        objs = visual.get("objects") if isinstance(visual, dict) else None
        if not isinstance(objs, dict):
            continue
        for obj_name in ("dataPoint",):
            for entry in (objs.get(obj_name) or []):
                props = entry.get("properties") if isinstance(entry, dict) else None
                if not isinstance(props, dict):
                    continue
                for prop_name in ("defaultColor", "fill"):
                    hexed = _hex6(_color_literal(props.get(prop_name)))
                    if hexed and contrast_ratio(hexed, background) < _INK_INVISIBLE_CONTRAST:
                        problems.append(
                            "%s: %s.%s = %s is invisible on the %s page background "
                            "(contrast %.2f) -- the visual renders empty"
                            % (path, obj_name, prop_name, hexed, background,
                               contrast_ratio(hexed, background)))
    return problems


def _color_literal(node):
    """Dig a ``#rrggbb`` out of PBIR's ``solid.color.expr.Literal.Value`` nesting, or ``None``."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return None
    for key in ("solid", "color", "expr", "Literal"):
        if key in node:
            inner = _color_literal(node.get(key))
            if inner:
                return inner
    value = node.get("Value")
    if isinstance(value, str):
        return value.strip().strip("'")
    return None


def _lint_required_roles(parts):
    """Validity R9: a visual must project every role its visual type requires (#144).

    A visual missing a required role is STRUCTURALLY INVALID -- not merely low-fidelity. Power BI
    Desktop renders it broken, and ``powerbi-report-author validate`` reports
    ``PBIR_ROLE_REQUIRED_MISSING`` and exits 1. That CLI is an opt-in npm pre-gate an ordinary run
    never reaches, so the hermetic linter has to catch it too -- which is the exact reasoning
    ``_lint_page_order`` (R7) already applies to ``PBIR_PAGE_ORDER_EMPTY``.

    This is the MIRROR of ``lint_visual_model_bindings``, and the pair is worth reading together:
    that rule covers the case where *validate is blind and we can see* (a binding to a model object
    that does not exist validates clean and renders empty); this one covers the reverse, where
    *validate can see and we were blind*. Reported in #144 as the systemic gap that let #143 ship
    green -- the run graded ``definition_of_done: warn`` / ``0 error`` / ``Viz=built`` over a report
    that could not pass structural validation.

    ``_REQUIRED_ROLES`` is HARVESTED from ``powerbi-report-author catalog describe`` (v0.1.4), the
    same tool that raises the diagnostic, so it states what the validator enforces rather than what
    we believe it enforces. A visual type absent from the table is not judged -- "cannot judge" must
    never become "declare invalid".

    A role counts as occupied by either a projection or a ``fieldParameters`` binding, because a
    field-parameter expansion legitimately seeds one and rejecting it would flag a repaired visual.
    """
    problems = []
    for path in sorted(parts):
        if not path.endswith("visual.json"):
            continue
        doc = _load_json(parts, path)
        if not isinstance(doc, dict):
            continue
        vis = doc.get("visual")
        if not isinstance(vis, dict):
            continue
        required = _REQUIRED_ROLES.get(vis.get("visualType"))
        if not required:
            continue
        query = vis.get("query")
        query_state = (query.get("queryState") if isinstance(query, dict) else None) or {}
        if not isinstance(query_state, dict) or not query_state:
            # A visual emitted with NO query at all is the deliberate placeholder an emptied visual
            # becomes (migrate_estate drops ``query`` wholesale). That is the VALID outcome this
            # rule exists to steer toward, so it must not be flagged as the defect.
            continue
        for role in required:
            spec = query_state.get(role) or {}
            if not (isinstance(spec, dict)
                    and (spec.get("projections") or spec.get("fieldParameters"))):
                problems.append(
                    "%s: visualType %r requires role %r but the visual does not project it -- this "
                    "is structurally invalid PBIR (powerbi-report-author validate reports "
                    "PBIR_ROLE_REQUIRED_MISSING and Power BI Desktop renders it broken); emit the "
                    "role or emit the visual as an empty placeholder"
                    % (path, vis.get("visualType"), role))
    return problems


MEASURE_ROLES = {
    "advancedSlicerVisual": ("Label", "Tooltips"),
    "areaChart": ("Tooltips", "Y", "Y2"),
    "azureMap": ("Size", "Tooltips"),
    "barChart": ("Tooltips", "Y"),
    "card": ("Values",),
    "cardVisual": ("Data", "Tooltips"),
    "clusteredBarChart": ("Tooltips", "Y"),
    "clusteredColumnChart": ("Tooltips", "Y"),
    "columnChart": ("Tooltips", "Y"),
    "decompositionTreeVisual": ("Analyze", "Tooltips"),
    "donutChart": ("Tooltips", "Y"),
    "filledMap": ("Tooltips", "X", "Y"),
    "funnel": ("Tooltips", "Y"),
    "gauge": ("MaxValue", "MinValue", "TargetValue", "Tooltips", "Y"),
    "hundredPercentStackedAreaChart": ("Tooltips", "Y"),
    "hundredPercentStackedBarChart": ("Tooltips", "Y"),
    "hundredPercentStackedColumnChart": ("Tooltips", "Y"),
    "kpi": ("Goal", "Indicator"),
    "lineChart": ("Tooltips", "Y", "Y2"),
    "lineClusteredColumnComboChart": ("Tooltips", "Y", "Y2"),
    "lineStackedColumnComboChart": ("Tooltips", "Y", "Y2"),
    "listSlicer": ("Tooltips",),
    "map": ("Size", "Tooltips"),
    "matrix": ("Values",),
    "pieChart": ("Tooltips", "Y"),
    "pivotTable": ("Values",),
    "ribbonChart": ("Tooltips", "Y"),
    "scatterChart": ("Size", "Tooltips"),
    "shapeMap": ("Tooltips", "Value"),
    "stackedAreaChart": ("Tooltips", "Y"),
    "treemap": ("Tooltips", "Values"),
    "waterfallChart": ("Tooltips", "Y"),
}


# The built-in PBIR ``visualType`` validity set, DERIVED so it can never again be narrower than the
# catalog the role tables were harvested from (#179).
#
# It was a hand-maintained literal, and it drifted: ``_REQUIRED_ROLES`` and ``MEASURE_ROLES`` are
# harvested from ``powerbi-report-author catalog describe``, and both knew FIVE types the literal
# did not -- ``cardVisual``, ``filterSlicer``, ``hundredPercentStackedAreaChart``, ``matrix`` and
# ``table``. Every one of them made R4 report a *false* "unknown visualType ... Power BI renders it
# as a missing custom visual", and two of them (``matrix``, ``table``) are core Power BI visuals.
# Our own emitter produced none of them, which is exactly why it went unnoticed here: the linter
# ships as a tool consumers run over THEIR reports, and the estate that reported this hand-authors
# 140 ``cardVisual`` visuals.
#
# The union is the point. A literal can drift; a union with the harvested tables cannot be narrower
# than them by construction, so adding a type to the catalog can never again make a valid report
# look invalid. ``_CURATED_VISUAL_TYPES`` still carries its own weight: role-LESS types (textbox,
# image, actionButton, basicShape, and the AI visuals) have no required roles and so appear in
# neither harvested table.
VALID_VISUAL_TYPES = frozenset(
    _CURATED_VISUAL_TYPES | set(_REQUIRED_ROLES) | set(MEASURE_ROLES))


def _lint_measure_role_kind(parts):
    """Validity R10: a MEASURE-kind role must not carry a bare ``Column`` (#142).

    ``powerbi-report-author validate`` reports ``PBIR_ROLE_KIND_MISMATCH`` and exits 1:
    *"Column expression in Measure-only role 'Values' (use Measure or Aggregation)"*. Reported on a
    ``pivotTable`` where **five of six** projections in the same role were correctly wrapped and one
    was not -- so the engine already knew the shape and missed a per-field branch, most likely an
    unaggregated/discrete pill whose Tableau aggregation is absent.

    IT REPRODUCES HERE. Swept at 2.333.0, our own corpus emits it too, on a different visual type:
    ``0135_aggregation_types`` -- a workbook literally about aggregation types -- has a
    ``clusteredBarChart`` whose ``Y`` carries ``Orders[Sales]`` bare while its sibling visual on the
    same page wraps the identical field in ``Aggregation``. Confirmed by running the real validator
    against the emitted report: 1 error, exit 1, same diagnostic code.

    ``MEASURE_ROLES`` is HARVESTED from ``powerbi-report-author catalog describe``, exactly as
    ``REQUIRED_ROLES`` (R9) is -- the tool that raises the diagnostic is the tool asked what the rule
    is. That mattered: the catalog distinguishes three kinds, ``Grouping`` / ``Measure`` /
    **``GroupingOrMeasure``**, and my hand-written first attempt guessed ``scatterChart``'s ``X``/``Y``
    and ``multiRowCard``'s ``Values`` were measure-only. They are not -- ``GroupingOrMeasure`` and
    absent respectively -- so a guessed table would have failed sound reports. Only ``Measure`` is
    enforced here; ``GroupingOrMeasure`` is deliberately untouched, since "cannot judge" must never
    become "declare invalid".

    An ``Aggregation``, a ``Measure``, a ``NativeVisualCalculation`` or a ``Hierarchy`` all satisfy
    the role; only a bare ``Column`` is a violation.
    """
    problems = []
    for path in sorted(parts):
        if not path.endswith("visual.json"):
            continue
        doc = _load_json(parts, path)
        if not isinstance(doc, dict):
            continue
        vis = doc.get("visual")
        if not isinstance(vis, dict):
            continue
        measure_roles = MEASURE_ROLES.get(vis.get("visualType"))
        if not measure_roles:
            continue
        query = vis.get("query")
        query_state = (query.get("queryState") if isinstance(query, dict) else None) or {}
        if not isinstance(query_state, dict):
            continue
        for role in measure_roles:
            spec = query_state.get(role)
            if not isinstance(spec, dict):
                continue
            for proj in spec.get("projections") or []:
                field = (proj or {}).get("field")
                if not isinstance(field, dict):
                    continue
                if "Column" in field and not ({"Aggregation", "Measure"} & set(field)):
                    problems.append(
                        "%s: visualType %r role %r is Measure-only but projection %r is a bare "
                        "Column expression -- powerbi-report-author validate reports "
                        "PBIR_ROLE_KIND_MISMATCH and exits 1; wrap it in an Aggregation or bind a "
                        "Measure"
                        % (path, vis.get("visualType"), role,
                           (field.get("Column") or {}).get("Property")))
    return problems


_SCATTER_GROUPING_ROLES = ("Category", "Series", "Play")


def _lint_scatter_grouping(parts):
    """Validity R11: a scatterChart with aggregated X/Y and no grouping role cannot map (#173).

    Power BI's scatter dataViewMapping needs a grouping field to produce more than one point. With
    ``X`` and ``Y`` both aggregated and no ``Category`` / ``Series`` / ``Play``, every row collapses
    to a single aggregate and Desktop raises ``DataViewMappingError_ScatterGroupingValues``.

    THIS IS THE 'VALIDATE IS BLIND AND WE CAN SEE' CASE, and deliberately so:
    ``powerbi-report-author catalog describe scatterChart`` lists ``requiredRoles: ["X", "Y"]`` with
    ``Category`` merely OPTIONAL, so R9 cannot fire and the external validator passes the report.
    The visual is structurally valid and does not render. That is precisely the gap #173 reports --
    a handover status of ``rebuilt`` over a visual with a live Desktop error -- and it is why this
    rule is keyed on the RENDER contract rather than on the catalog's role list.

    Conservative by construction, because "optional" means a legitimate ungrouped scatter exists:
    * a grouping projection on ANY of the three roles clears it;
    * X or Y carrying a bare ``Column`` clears it -- an unaggregated axis is itself the grain, which
      is the ordinary way to draw a per-row scatter;
    * a visual with no query at all is the deliberate emptied placeholder and is never flagged.

    Measured on the 34-workbook corpus at 2.334.0: 7 scatterCharts, **all 7 grouped**, so this fires
    on nothing we currently build -- it is a guard against a shape a customer hit, not a description
    of one we produce.
    """
    problems = []
    for path in sorted(parts):
        if not path.endswith("visual.json"):
            continue
        doc = _load_json(parts, path)
        if not isinstance(doc, dict):
            continue
        vis = doc.get("visual")
        if not isinstance(vis, dict) or vis.get("visualType") != "scatterChart":
            continue
        query = vis.get("query")
        query_state = (query.get("queryState") if isinstance(query, dict) else None) or {}
        if not isinstance(query_state, dict) or not query_state:
            continue
        if any((query_state.get(r) or {}).get("projections") for r in _SCATTER_GROUPING_ROLES):
            continue
        aggregated = []
        for axis in ("X", "Y"):
            projections = (query_state.get(axis) or {}).get("projections") or []
            if not projections:
                aggregated = []
                break
            for proj in projections:
                field = (proj or {}).get("field")
                if not isinstance(field, dict):
                    aggregated = []
                    break
                if not ({"Aggregation", "Measure"} & set(field)):
                    aggregated = []
                    break
                aggregated.append(axis)
            else:
                continue
            break
        if not aggregated:
            continue
        problems.append(
            "%s: scatterChart projects aggregated X and Y with no grouping field (Category, Series "
            "or Play) -- every row collapses to one point and Power BI Desktop raises "
            "DataViewMappingError_ScatterGroupingValues. powerbi-report-author validate PASSES this "
            "(the catalog marks Category optional), so the visual is structurally valid and does "
            "not render; add the grain dimension to Category or leave an axis unaggregated" % path)
    return problems


def lint_pbir_parts(parts, model_surface=None):
    """Return a list of PBIR validity violations for an emitted ``{path: content}`` parts dict.

    An empty list means the report is free of the known static PBIR validity defects (an unknown
    ``visualType`` and a ``customTheme`` name mismatch). Never raises; a malformed / absent part is
    silently skipped so the linter is safe to run on every migration.

    ``model_surface`` is OPTIONAL: supply a :func:`reference_gate.build_model_surface` result to also
    prove every visual's model references resolve. Omitting it leaves the result unchanged.
    """
    parts = parts or {}
    return (_lint_visual_types(parts) + _lint_theme(parts)
            + _lint_card_display_units(parts) + _lint_native_query_refs(parts)
            + _lint_page_order(parts) + _lint_required_roles(parts)
            + _lint_measure_role_kind(parts)
            + _lint_scatter_grouping(parts)
            + _lint_dangling_select_refs(parts)
            + _lint_invisible_ink(parts)
            + lint_visual_model_bindings(parts, model_surface))


def lint_pbir_report(report_dir, model_surface=None):
    """Lint an on-disk ``*.Report`` folder: read every file under it into a ``{relpath: text}`` parts
    dict (forward-slash paths, ``utf-8-sig``) and apply :func:`lint_pbir_parts`."""
    import os

    parts = {}
    for root, _dirs, files in os.walk(report_dir):
        for filename in files:
            full = os.path.join(root, filename)
            rel = os.path.relpath(full, report_dir).replace(os.sep, "/")
            try:
                with open(full, "r", encoding="utf-8-sig") as handle:
                    parts[rel] = handle.read()
            except (UnicodeDecodeError, OSError):
                try:
                    with open(full, "rb") as handle:
                        parts[rel] = handle.read()
                except OSError:
                    continue
    return lint_pbir_parts(parts, model_surface)


def _main(argv):
    import os

    if not argv:
        print("usage: pbir_lint.py <path-to-.Report-folder> [more ...]")
        return 2
    total = 0
    for target in argv:
        if not os.path.isdir(target):
            print("%s: not a directory (expected a *.Report folder) -- skipped" % target)
            continue
        problems = lint_pbir_report(target)
        if problems:
            total += len(problems)
            print(target)
            for problem in problems:
                print("  " + problem)
    if total:
        print("FAIL: %d PBIR validity violation(s)" % total)
        return 1
    print("OK: PBIR report(s) clean")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main(sys.argv[1:]))
