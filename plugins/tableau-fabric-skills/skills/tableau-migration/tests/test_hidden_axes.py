"""Author-hidden axes (Tableau ``Show Header`` off) must reach the PBIR ``show: false`` toggle.

Tableau serialises a hidden header/axis explicitly under ``worksheet/table/style/style-rule`` as
``format[@attr='display'][@value='false']``, in two spellings -- one naming the shelf (``@scope``),
one naming the field the shelf carries (``@field``). Both are honoured; the shelf is mapped to a
Power BI axis STRUCTURALLY by the role of the fields on it, never guessed.

This is load-bearing rather than cosmetic: high-design dashboards are tiled composites (a KPI
card's bar strip, dot row and month labels are SEPARATE worksheets stacked in one panel) that hide
every axis so the pieces align. Rendering those axes adds furniture Tableau never showed AND
steals plot area, which forces a scrollbar into a small tile.
"""

import xml.etree.ElementTree as ET

import pytest

from scripts.twb_to_pbir import (
    _axis_objects,
    _field_ref_keys,
    _field_ref_keys_from_text,
    _parse_hidden_axes,
)

DS = "federated.0i4yxiu1mu8scy15u7s891iiv9m6"


def _dim(instance="mn:date:ok", field_id="date", caption="Date"):
    return {"caption": caption, "field_id": field_id, "instance": instance,
            "role": "dimension", "datatype": "date", "binding": "column", "kind": "category"}


def _meas(instance="usr:Load:qk", field_id="Load", caption="Load Factor"):
    return {"caption": caption, "field_id": field_id, "instance": instance,
            "role": "measure", "datatype": "real", "binding": "measure", "kind": "value"}


def _table(rules):
    """A ``<table>`` carrying ``<style>`` with the given ``(element, attrs)`` format rules."""
    table = ET.Element("table")
    style = ET.SubElement(table, "style")
    for element, attrs in rules:
        rule = ET.SubElement(style, "style-rule", {"element": element})
        ET.SubElement(rule, "format", attrs)
    return table


def _display_off(scope=None, field=None):
    attrs = {"attr": "display", "value": "false"}
    if scope is not None:
        attrs["scope"] = scope
    if field is not None:
        attrs["field"] = field
    return attrs


class TestScopeSpelling:
    """``@scope='rows'|'cols'`` names the shelf directly."""

    def test_measure_on_rows_hides_the_value_axis(self):
        table = _table([("axis", _display_off(scope="rows"))])
        assert _parse_hidden_axes(table, [], [_dim()], [_meas()], []) == {"valueAxis"}

    def test_dimension_on_cols_hides_the_category_axis(self):
        table = _table([("axis", _display_off(scope="cols"))])
        assert _parse_hidden_axes(table, [], [_dim()], [_meas()], []) == {"categoryAxis"}

    def test_both_shelves_hidden_hides_both_axes(self):
        table = _table([("axis", _display_off(scope="rows")),
                        ("axis", _display_off(scope="cols"))])
        assert _parse_hidden_axes(table, [], [_dim()], [_meas()], []) == {
            "categoryAxis", "valueAxis"}

    def test_swapped_shelves_map_by_role_not_by_position(self):
        """The dimension on ROWS must still drive categoryAxis -- role decides, not the shelf."""
        table = _table([("axis", _display_off(scope="rows"))])
        assert _parse_hidden_axes(table, [_dim()], [], [], [_meas()]) == {"categoryAxis"}


class TestFieldSpelling:
    """``@field='[ds].[instance]'`` names the shelf by the field it carries."""

    def test_qualified_field_reference_matches_the_shelf_instance(self):
        """The XML writes ``[ds].[instance]``; the parsed entry carries the bare ``instance``."""
        table = _table([("label", _display_off(field="[%s].[mn:date:ok]" % DS))])
        assert _parse_hidden_axes(table, [], [_dim()], [_meas()], []) == {"categoryAxis"}

    def test_field_reference_may_also_name_the_underlying_column(self):
        table = _table([("label", _display_off(field="[%s].[date]" % DS))])
        assert _parse_hidden_axes(table, [], [_dim()], [_meas()], []) == {"categoryAxis"}

    def test_unqualified_field_reference_matches(self):
        table = _table([("label", _display_off(field="mn:date:ok"))])
        assert _parse_hidden_axes(table, [], [_dim()], [_meas()], []) == {"categoryAxis"}

    def test_field_reference_to_a_measure_hides_the_value_axis(self):
        table = _table([("label", _display_off(field="[%s].[usr:Load:qk]" % DS))])
        assert _parse_hidden_axes(table, [], [_dim()], [_meas()], []) == {"valueAxis"}

    def test_the_real_workbook_shape_hides_both(self):
        """Verbatim from ``Pass Count Bars``: an axis rule by scope + a label rule by field."""
        meas = _meas(instance="usr:CY Load Factor (copy)_2945072722349813854:qk",
                     field_id="CY Load Factor (copy)_2945072722349813854")
        table = _table([
            ("axis", _display_off(
                scope="rows",
                field="[%s].[usr:CY Load Factor (copy)_2945072722349813854:qk]" % DS)),
            ("label", _display_off(field="[%s].[mn:date:ok]" % DS)),
        ])
        assert _parse_hidden_axes(table, [], [_dim()], [meas], []) == {
            "categoryAxis", "valueAxis"}


class TestNeverGuesses:
    """Every ambiguous or unstated case must yield NO hide."""

    def test_unknown_field_reference_is_ignored(self):
        table = _table([("label", _display_off(field="[%s].[not:on:any:shelf]" % DS))])
        assert _parse_hidden_axes(table, [], [_dim()], [_meas()], []) == set()

    def test_display_true_is_not_a_hide(self):
        table = ET.Element("table")
        style = ET.SubElement(table, "style")
        rule = ET.SubElement(style, "style-rule", {"element": "axis"})
        ET.SubElement(rule, "format", {"attr": "display", "scope": "rows", "value": "true"})
        assert _parse_hidden_axes(table, [], [_dim()], [_meas()], []) == set()

    def test_a_non_display_attribute_is_not_a_hide(self):
        table = ET.Element("table")
        style = ET.SubElement(table, "style")
        rule = ET.SubElement(style, "style-rule", {"element": "axis"})
        ET.SubElement(rule, "format", {"attr": "line-visibility", "scope": "rows",
                                       "value": "false"})
        assert _parse_hidden_axes(table, [], [_dim()], [_meas()], []) == set()

    def test_a_mixed_shelf_is_skipped(self):
        """Dimensions AND measures on one shelf -- which axis is genuinely ambiguous."""
        table = _table([("axis", _display_off(scope="cols"))])
        assert _parse_hidden_axes(table, [_dim()], [_dim("mn:x:ok", "x")], [_meas()],
                                  [_meas("usr:y:qk", "y")]) == set()

    def test_an_empty_shelf_is_skipped(self):
        table = _table([("axis", _display_off(scope="cols"))])
        assert _parse_hidden_axes(table, [_dim()], [], [], []) == set()

    def test_a_field_key_on_both_shelves_is_poisoned_not_resolved_first_wins(self):
        """The same field id on rows AND cols cannot say which axis the author hid."""
        table = _table([("label", _display_off(field="[%s].[mn:date:ok]" % DS))])
        assert _parse_hidden_axes(table, [_dim()], [_dim()], [], []) == set()

    def test_no_table_yields_nothing(self):
        assert _parse_hidden_axes(None, [], [_dim()], [_meas()], []) == set()

    def test_no_style_element_yields_nothing(self):
        assert _parse_hidden_axes(ET.Element("table"), [], [_dim()], [_meas()], []) == set()

    def test_no_rules_yields_nothing(self):
        assert _parse_hidden_axes(_table([]), [], [_dim()], [_meas()], []) == set()


class TestFieldRefKeys:
    def test_qualified_text_yields_verbatim_and_last_segment(self):
        assert _field_ref_keys_from_text("[%s].[mn:date:ok]" % DS) == {
            "[%s].[mn:date:ok]" % DS, "mn:date:ok"}

    def test_bare_text_yields_itself(self):
        assert _field_ref_keys_from_text("mn:date:ok") == {"mn:date:ok"}

    @pytest.mark.parametrize("bad", [None, "", "   ", 7, []])
    def test_missing_or_non_string_yields_nothing(self, bad):
        assert _field_ref_keys_from_text(bad) == set()

    def test_dict_entry_offers_instance_and_field_id(self):
        keys = _field_ref_keys(_dim())
        assert "mn:date:ok" in keys
        assert "date" in keys

    def test_string_entry_is_tolerated(self):
        assert "mn:date:ok" in _field_ref_keys("[%s].[mn:date:ok]" % DS)

    def test_unknown_shape_yields_nothing(self):
        assert _field_ref_keys(object()) == set()


class TestAxisObjects:
    """``_axis_objects`` must emit ``show: false`` and keep working with titles alone."""

    def _show_values(self, objs, axis):
        return [(e.get("properties") or {}).get("show", {}).get("expr", {})
                .get("Literal", {}).get("Value")
                for e in objs.get(axis, [])
                if "show" in (e.get("properties") or {})]

    def test_hidden_axis_emits_show_false(self):
        objs = _axis_objects(None, {"valueAxis"})
        assert self._show_values(objs, "valueAxis") == ["false"]

    def test_both_axes_hidden(self):
        objs = _axis_objects(None, {"categoryAxis", "valueAxis"})
        assert self._show_values(objs, "categoryAxis") == ["false"]
        assert self._show_values(objs, "valueAxis") == ["false"]

    def test_no_hides_and_no_titles_emits_nothing(self):
        assert not _axis_objects(None, set())
        assert not _axis_objects(None, None)

    def test_a_visible_axis_never_gets_a_show_toggle(self):
        objs = _axis_objects(None, {"valueAxis"})
        assert not self._show_values(objs, "categoryAxis")

    def test_titles_still_emit_without_any_hide(self):
        objs = _axis_objects({"categoryAxis": {"text": "Month"}}, None)
        assert objs.get("categoryAxis")
        assert not self._show_values(objs, "categoryAxis")

    def test_a_title_and_a_hide_on_the_same_axis_are_merged(self):
        objs = _axis_objects({"valueAxis": {"text": "Load"}}, {"valueAxis"})
        props = {}
        for entry in objs["valueAxis"]:
            props.update(entry.get("properties") or {})
        assert props.get("show", {}).get("expr", {}).get("Literal", {}).get("Value") == "false"
        assert "titleText" in props

    def test_a_blanked_title_and_a_hide_are_merged(self):
        objs = _axis_objects({"valueAxis": {"hide": True}}, {"valueAxis"})
        props = {}
        for entry in objs["valueAxis"]:
            props.update(entry.get("properties") or {})
        assert props.get("show", {}).get("expr", {}).get("Literal", {}).get("Value") == "false"
        assert props.get("showAxisTitle", {}).get("expr", {}).get(
            "Literal", {}).get("Value") == "false"
