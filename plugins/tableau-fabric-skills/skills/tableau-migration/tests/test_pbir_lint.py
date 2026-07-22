"""Unit + emitter-clean guard tests for :mod:`pbir_lint`.

Two layers:

  * UNIT -- hand-built ``{path: content}`` parts dicts exercise each of the two checks (visual-type
    validity R4, theme-name consistency R3) in isolation: a clean report yields ``[]`` and every
    distinct defect yields exactly one problem, with no false positives on a baseTheme-only report.
  * EMITTER-CLEAN GUARD -- the real :func:`twb_to_pbir.emit_pbir` output for a representative multi
    -visual workbook must lint clean, and the two regression locks (a colour-legend column emits the
    valid ``columnChart``, not ``stackedColumnChart``; every theme string matches and ends in
    ``.json``) hold. A final test corrupts an emitted visual type to prove the guard would CATCH a
    future regression rather than silently pass.
"""
import json

import pbir_lint
from pbir_lint import lint_pbir_parts, VALID_VISUAL_TYPES

import twb_to_pbir as R
from twb_to_pbir import emit_pbir, parse_twb

from test_twb_to_pbir import _workbook, _worksheet, _INST, _visual_parts


_THEME_DIR = "StaticResources/RegisteredResources/"


# -- unit fixtures -------------------------------------------------------------
def _report_json(ct_name="TableauPalette.json", item_name=None, item_path=None):
    """A report.json dict shaped exactly like ``report_json_part`` emits. ``ct_name=None`` -> a
    baseTheme-only report (no customTheme, no resource package)."""
    part = {
        "$schema": "https://.../report/1.0.0/schema.json",
        "layoutOptimization": "None",
        "themeCollection": {"baseTheme": {"name": "CY24SU10", "type": "SharedResources"}},
    }
    if ct_name is not None:
        part["themeCollection"]["customTheme"] = {"name": ct_name, "type": "RegisteredResources"}
        part["resourcePackages"] = [{
            "name": "RegisteredResources", "type": "RegisteredResources",
            "items": [{
                "name": ct_name if item_name is None else item_name,
                "path": ct_name if item_path is None else item_path,
                "type": "CustomTheme"}]}]
    return part


def _parts(ct_name="TableauPalette.json", item_name=None, item_path=None,
           theme_internal="__CT__", file_stored_as=None, visual_type="columnChart",
           include_theme_file=True):
    parts = {
        "definition/report.json": json.dumps(_report_json(ct_name, item_name, item_path)),
        "definition/pages/p/visuals/v/visual.json":
            json.dumps({"visual": {"visualType": visual_type}}),
    }
    if ct_name is not None and include_theme_file:
        internal = ct_name if theme_internal == "__CT__" else theme_internal
        stored = file_stored_as or (item_path if item_path is not None else ct_name)
        parts[_THEME_DIR + stored] = json.dumps({"name": internal, "dataColors": ["#4E79A7"]})
    return parts


# -- unit: clean ---------------------------------------------------------------
def test_clean_parts_lint_empty():
    assert lint_pbir_parts(_parts()) == []


def test_empty_parts_lint_empty():
    assert lint_pbir_parts({}) == []
    assert lint_pbir_parts(None) == []


def test_base_theme_only_no_false_positive():
    # no customTheme registered -> nothing to check, and a valid visual stays clean
    assert lint_pbir_parts(_parts(ct_name=None)) == []


# -- unit: visual-type validity (R4) -------------------------------------------
def test_invalid_visual_type_flagged():
    problems = lint_pbir_parts(_parts(visual_type="stackedColumnChart"))
    assert len(problems) == 1
    assert "stackedColumnChart" in problems[0]
    assert "visualType" in problems[0]


def test_invalid_bar_visual_type_flagged():
    problems = lint_pbir_parts(_parts(visual_type="stackedBarChart"))
    assert len(problems) == 1 and "stackedBarChart" in problems[0]


def test_valid_stacked_variants_are_clean():
    # the UNQUALIFIED column/bar ARE Power BI's stacked variants -> valid; so is stackedAreaChart
    for vt in ("columnChart", "barChart", "stackedAreaChart",
               "hundredPercentStackedColumnChart", "clusteredBarChart"):
        assert lint_pbir_parts(_parts(visual_type=vt)) == [], vt
        assert vt in VALID_VISUAL_TYPES


def test_visual_missing_type_is_ignored():
    parts = _parts()
    parts["definition/pages/p/visuals/v/visual.json"] = json.dumps({"visual": {}})
    assert lint_pbir_parts(parts) == []


# -- unit: theme-name consistency (R3) -----------------------------------------
def test_theme_name_missing_json_extension_flagged():
    problems = lint_pbir_parts(_parts(
        ct_name="Tableau", item_name="Tableau", item_path="Tableau",
        theme_internal="Tableau", file_stored_as="Tableau"))
    assert len(problems) == 1 and ".json" in problems[0]


def test_theme_resource_item_name_mismatch_flagged():
    problems = lint_pbir_parts(_parts(
        ct_name="TableauPalette.json", item_name="Other.json", item_path="Other.json"))
    assert len(problems) == 1
    assert "match its RegisteredResources item" in problems[0]


def test_theme_file_internal_name_mismatch_flagged():
    problems = lint_pbir_parts(_parts(theme_internal="Tableau"))
    assert len(problems) == 1
    assert "internal name" in problems[0]


def test_theme_file_not_bundled_flagged():
    problems = lint_pbir_parts(_parts(include_theme_file=False))
    assert len(problems) == 1
    assert "not bundled" in problems[0]


def test_no_custom_theme_item_flagged():
    # customTheme declared in themeCollection but no RegisteredResources CustomTheme item backs it
    report = _report_json()
    report.pop("resourcePackages", None)
    parts = {
        "definition/report.json": json.dumps(report),
        "definition/pages/p/visuals/v/visual.json":
            json.dumps({"visual": {"visualType": "columnChart"}}),
        _THEME_DIR + "TableauPalette.json":
            json.dumps({"name": "TableauPalette.json", "dataColors": ["#4E79A7"]}),
    }
    problems = lint_pbir_parts(parts)
    assert any("no RegisteredResources CustomTheme item" in p for p in problems)


# -- emitter-clean guard -------------------------------------------------------
def _representative_workbook():
    """A workbook whose emit exercises multiple visual types + the always-on custom theme:
    a colour-legend stacked column, a plain clustered bar, and a line chart."""
    stacked_col = _worksheet(
        "Stacked Cols", "Bar",
        rows="[federated.abc].[sum:Sales:qk]",
        cols="[federated.abc].[none:Category:nk]",
        deps_extra=_INST,
        encodings="<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>")
    plain_bar = _worksheet(
        "Plain Cols", "Bar",
        rows="[federated.abc].[sum:Sales:qk]",
        cols="[federated.abc].[none:Category:nk]",
        deps_extra=_INST)
    line = _worksheet(
        "Sales Trend", "Line",
        rows="[federated.abc].[sum:Sales:qk]",
        cols="[federated.abc].[mn:Order Date:ok]",
        deps_extra=_INST)
    return _workbook(stacked_col + plain_bar + line)


def test_emitted_pbir_lints_clean():
    parts = emit_pbir(parse_twb(_representative_workbook()))
    assert lint_pbir_parts(parts) == []


def test_emitted_stacked_column_uses_valid_columnchart_type():
    # R4 lock: a colour-legend column must emit the valid stacked spelling "columnChart"
    parts = emit_pbir(parse_twb(_representative_workbook()))
    vtypes = {v["visual"]["visualType"] for v in _visual_parts(parts).values()}
    assert "columnChart" in vtypes
    assert "stackedColumnChart" not in vtypes and "stackedBarChart" not in vtypes
    assert vtypes <= VALID_VISUAL_TYPES


def test_emitted_theme_strings_all_match_and_end_json():
    # R3 lock: customTheme.name == item name == item path == theme file internal name, all *.json
    parts = emit_pbir(parse_twb(_representative_workbook()))
    report = json.loads(parts["definition/report.json"])
    ct_name = report["themeCollection"]["customTheme"]["name"]
    assert ct_name == R._TABLEAU_THEME_FILE and ct_name.lower().endswith(".json")

    item = report["resourcePackages"][0]["items"][0]
    assert item["type"] == "CustomTheme"
    assert item["name"] == ct_name and item["path"] == ct_name

    theme_file = json.loads(parts[_THEME_DIR + ct_name])
    assert theme_file["name"] == ct_name


def test_lint_catches_injected_regression():
    # prove the guard would FAIL (not silently pass) if the emitter regressed to an invalid type
    parts = dict(emit_pbir(parse_twb(_representative_workbook())))
    vkey = next(k for k in parts if k.endswith("visual.json"))
    doc = json.loads(parts[vkey])
    doc["visual"]["visualType"] = "stackedColumnChart"
    parts[vkey] = json.dumps(doc)
    problems = lint_pbir_parts(parts)
    assert any("stackedColumnChart" in p for p in problems)
