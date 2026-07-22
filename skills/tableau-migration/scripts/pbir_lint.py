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

The valid-visual-type catalog below was ground-truthed against ``powerbi-report-author validate``
v0.1.4: every type the emitter can produce was confirmed KNOWN, and only genuinely invalid strings
trip ``PBIR_VISUAL_TYPE_UNKNOWN`` (distinct from role-binding diagnostics). It is deliberately
conservative -- a clean result means "free of these two known PBIR validity defects", not "provably
valid"; the authoritative external ``validate`` CLI remains the opt-in deeper check in
:mod:`fidelity_oracle`. Fail-safe throughout: a malformed or absent part is skipped, never raised on.
"""
from __future__ import annotations

import json

# The closed built-in PBIR ``visualType`` catalog. Every value here was ground-truthed KNOWN against
# the Microsoft ``powerbi-report-author validate`` CLI (v0.1.4); this is a strict SUPERSET of what
# ``twb_to_pbir`` emits, so a valid built-in never trips the linter. The invalid look-alikes the
# emitter must NEVER produce are deliberately ABSENT: "stackedColumnChart", "stackedBarChart" (Power
# BI spells those as the unqualified "columnChart" / "barChart"). Keep this in sync with the emitter's
# vocabulary; the emitter-clean pytest guard enforces that they never diverge.
VALID_VISUAL_TYPES = frozenset({
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


def lint_pbir_parts(parts):
    """Return a list of PBIR validity violations for an emitted ``{path: content}`` parts dict.

    An empty list means the report is free of the two known static PBIR validity defects (an unknown
    ``visualType`` and a ``customTheme`` name mismatch). Never raises; a malformed / absent part is
    silently skipped so the linter is safe to run on every migration.
    """
    parts = parts or {}
    return _lint_visual_types(parts) + _lint_theme(parts)


def lint_pbir_report(report_dir):
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
    return lint_pbir_parts(parts)


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
