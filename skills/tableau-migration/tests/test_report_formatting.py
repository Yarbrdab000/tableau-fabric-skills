"""Unit tests for :mod:`report_formatting` -- the pure, inventory-grounded PBIR report-layer
formatting builders (reference lines, rule-based conditional fill, data bars, opacity/transparency).

Each assertion is pinned to a REAL ``.pbix`` serialization recorded in
``docs/research/powerbi-formatting-inventory.json`` (quoted inline next to the case), so the emit
shapes are validated against ground truth rather than the author's expectation.  No Tableau workbook
or ``.pbix`` is needed or committed -- these builders take PBIR-natural inputs and are independent of
detection (which is wired separately in ``twb_to_pbir``).
"""
import json

import pytest

import report_formatting as rf
from twb_to_pbir import _semantic_string_literal


# -- valueGrammar literal encodings -------------------------------------------

def test_vg_string_single_quotes_and_doubles_apostrophes():
    assert rf.vg_string("Top") == "'Top'"
    assert rf.vg_string("O'Brien") == "'O''Brien'"


def test_vg_string_matches_house_semantic_string_literal():
    # Single source of truth for string literals: must not drift from twb_to_pbir's encoder.
    for v in ["Top", "O'Brien", "#118DFF", "a'b'c", "plain"]:
        assert rf.vg_string(v) == _semantic_string_literal(v)


def test_vg_bool_is_quoted_boolean_word():
    assert rf.vg_bool(True) == "true"
    assert rf.vg_bool(False) == "false"


def test_vg_int_is_bare_and_int_l_carries_suffix():
    assert rf.vg_int(12) == "12"
    assert rf.vg_int_l(1) == "1L"          # columnFormatting.labelPrecision raw: "1L"
    assert rf.vg_int_l(110) == "110L"      # dataBars.dataBarWidthPercent raw: "110L"


def test_vg_double_trails_d_and_drops_whole_number_decimals():
    assert rf.vg_double(-0.5) == "-0.5D"   # y1AxisReferenceLine.value raw: "-0.5D"
    assert rf.vg_double(100) == "100D"     # transparency raw: "100D"
    assert rf.vg_double(1000.0) == "1000D"
    assert rf.vg_double(0) == "0D"


def test_numeric_encoders_reject_bool():
    with pytest.raises(TypeError):
        rf.vg_double(True)


def test_expr_literal_and_solid_color_literal_shapes():
    assert rf.expr_literal("true") == {"expr": {"Literal": {"Value": "true"}}}
    # valueGrammar.colorForms.literalHex
    assert rf.solid_color_literal("#118DFF") == {
        "solid": {"color": {"expr": {"Literal": {"Value": "'#118DFF'"}}}}}


# -- reference line -----------------------------------------------------------

def test_reference_line_object_matches_inventory_property_raws():
    obj = rf.reference_line_object(
        -0.5, display_name="Constant line 1", line_color_hex="#FF0000",
        style="dotted", position="back", show=True, line_id="Ref0")
    props = obj["properties"]
    # Each of these is a verbatim objectIndex raw for data::y1AxisReferenceLine.
    assert props["value"] == json.loads('{"expr": {"Literal": {"Value": "-0.5D"}}}')
    assert props["displayName"] == json.loads(
        '{"expr": {"Literal": {"Value": "\'Constant line 1\'"}}}')
    assert props["show"] == json.loads('{"expr": {"Literal": {"Value": "true"}}}')
    assert props["style"] == json.loads('{"expr": {"Literal": {"Value": "\'dotted\'"}}}')
    assert props["position"] == json.loads('{"expr": {"Literal": {"Value": "\'back\'"}}}')
    assert props["lineColor"] == {
        "solid": {"color": {"expr": {"Literal": {"Value": "'#FF0000'"}}}}}
    assert obj["selector"] == {"id": "Ref0"}


def test_reference_line_object_minimal_only_show_and_value():
    obj = rf.reference_line_object(1000)
    assert set(obj["properties"]) == {"show", "value"}
    assert obj["properties"]["value"]["expr"]["Literal"]["Value"] == "1000D"


def test_reference_line_objects_targets_correct_axis_and_unique_ids():
    y = rf.reference_line_objects([{"value": 10}, {"value": 20}], "value")
    assert list(y) == ["y1AxisReferenceLine"]
    assert [o["selector"]["id"] for o in y["y1AxisReferenceLine"]] == ["Ref0", "Ref1"]
    x = rf.reference_line_objects([{"value": 5}], "category")
    assert list(x) == ["xAxisReferenceLine"]


def test_reference_line_objects_rejects_unknown_axis():
    with pytest.raises(ValueError):
        rf.reference_line_objects([{"value": 1}], "diagonal")


# -- rule-based conditional fill ----------------------------------------------

def test_comparison_uses_documented_kind_ints():
    assert rf.COMPARISON_KIND == {"=": 0, ">": 1, ">=": 2, "<": 3, "<=": 4}
    left = rf.measure_ref("Orders", "Profit")
    cond = rf.comparison(left, ">=", rf.vg_double(1000))
    assert cond == {"Comparison": {
        "ComparisonKind": 2,
        "Left": {"Measure": {"Expression": {"SourceRef": {"Entity": "Orders"}},
                             "Property": "Profit"}},
        "Right": {"Literal": {"Value": "1000D"}}}}


def test_comparison_rejects_unknown_op():
    with pytest.raises(ValueError):
        rf.comparison(rf.measure_ref("t", "m"), "!=", "1D")


def test_rule_based_fill_is_conditional_cases_solid():
    left = rf.measure_ref("Orders", "Profit")
    fill = rf.rule_based_fill([
        {"condition": rf.comparison(left, "<", rf.vg_double(0)), "color": "#D64550"},
        {"condition": rf.comparison(left, ">=", rf.vg_double(0)), "color": "#3A9D23"},
    ])
    cases = fill["solid"]["color"]["expr"]["Conditional"]["Cases"]
    assert len(cases) == 2
    assert cases[0]["Value"] == {"Literal": {"Value": "'#D64550'"}}
    assert cases[0]["Condition"]["Comparison"]["ComparisonKind"] == 3
    assert cases[1]["Condition"]["Comparison"]["ComparisonKind"] == 2


def test_rule_based_fill_supports_and_or_trees():
    left = rf.measure_ref("Orders", "Sales")
    band = rf.and_(rf.comparison(left, ">=", rf.vg_double(100)),
                   rf.comparison(left, "<", rf.vg_double(200)))
    fill = rf.rule_based_fill([{"condition": band, "color": "#FFAA00"}])
    cond = fill["solid"]["color"]["expr"]["Conditional"]["Cases"][0]["Condition"]
    assert "And" in cond and "Left" in cond["And"] and "Right" in cond["And"]


def test_rule_based_fill_requires_a_case():
    with pytest.raises(ValueError):
        rf.rule_based_fill([])


# -- data bars ----------------------------------------------------------------

def test_data_bars_colours_are_literal_hex_solids():
    db = rf.data_bars("#FDD9AC", "#B30000", axis_color_hex="#000000", reverse_direction=False)
    # positiveColor raw: {"solid":{"color":{"expr":{"Literal":{"Value":"'#FDD9AC'"}}}}}
    assert db["positiveColor"] == {
        "solid": {"color": {"expr": {"Literal": {"Value": "'#FDD9AC'"}}}}}
    assert db["negativeColor"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#B30000'"
    assert db["axisColor"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#000000'"
    assert db["reverseDirection"] == {"expr": {"Literal": {"Value": "false"}}}


def test_data_bars_axis_color_optional():
    db = rf.data_bars("#111111", "#222222")
    assert "axisColor" not in db
    assert db["reverseDirection"]["expr"]["Literal"]["Value"] == "false"


def test_column_formatting_data_bars_scopes_to_one_column_by_metadata():
    obj = rf.column_formatting_data_bars("q_sales", "#FDD9AC", "#B30000")
    assert obj["selector"] == {"metadata": "q_sales"}
    assert "dataBars" in obj["properties"]
    assert obj["properties"]["dataBars"]["positiveColor"][
        "solid"]["color"]["expr"]["Literal"]["Value"] == "'#FDD9AC'"


# -- opacity / transparency (inverted) ----------------------------------------

def test_transparency_is_inverted_opacity_in_double_form():
    # opacity 100 (solid) -> transparency 0 ; opacity 0 (invisible) -> transparency 100.
    assert rf.transparency_from_opacity(100) == {"expr": {"Literal": {"Value": "0D"}}}
    assert rf.transparency_from_opacity(0) == {"expr": {"Literal": {"Value": "100D"}}}
    assert rf.transparency_from_opacity(40) == {"expr": {"Literal": {"Value": "60D"}}}


def test_transparency_rejects_out_of_range():
    with pytest.raises(ValueError):
        rf.transparency_from_opacity(150)
    with pytest.raises(ValueError):
        rf.transparency_from_opacity(-1)
