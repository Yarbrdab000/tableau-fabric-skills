"""Categorical mark-colour palettes stored at the DATASOURCE level must be found.

Tableau writes an explicit member->colour map ONCE on ``<datasource><style>`` whenever the
assignment is shared across worksheets -- the normal case for a consistently styled multi-sheet
dashboard -- and then omits it from each worksheet's own ``table/style``. The worksheet-only reader
therefore saw nothing and every such visual fell back to theme colours.

Matching is EXACT, never a guess: the worksheet writes its colour encoding in the qualified
``[datasource].[token]`` form and the palette is keyed by ``(datasource_name, token)``.
"""

import xml.etree.ElementTree as ET

from scripts.twb_to_pbir import (
    _datasource_mark_color_palettes,
    _pane_color_columns,
    _parse_mark_colors,
    _split_field_ref,
)

DS = "federated.abc"
TOKEN = "none:airline_name:nk"
QUALIFIED = "[%s].[%s]" % (DS, TOKEN)


def _palette_style(entries, element="mark"):
    """``<style>`` holding one mark colour encoding per ``(field, [(member, hex)])`` entry."""
    style = ET.Element("style")
    for field, members in entries:
        rule = ET.SubElement(style, "style-rule", {"element": element})
        enc = ET.SubElement(rule, "encoding", {"attr": "color", "field": field,
                                               "type": "palette"})
        for member, hexv in members:
            mp = ET.SubElement(enc, "map", {"to": hexv})
            ET.SubElement(mp, "bucket").text = '"%s"' % member
    return style


def _root(datasources):
    """A ``<workbook>`` whose ``<datasources>`` hold ``(name, style_entries)``."""
    root = ET.Element("workbook")
    holder = ET.SubElement(root, "datasources")
    for name, entries in datasources:
        ds = ET.SubElement(holder, "datasource", {"name": name})
        ds.append(_palette_style(entries))
    return root


def _table(entries=None):
    table = ET.Element("table")
    if entries is not None:
        table.append(_palette_style(entries))
    return table


def _pane(colors):
    pane = ET.Element("pane")
    encs = ET.SubElement(pane, "encodings")
    for col in colors:
        ET.SubElement(encs, "color", {"column": col})
    return pane


class TestSplitFieldRef:
    def test_qualified(self):
        assert _split_field_ref(QUALIFIED) == (DS, TOKEN)

    def test_unqualified_has_no_qualifier(self):
        assert _split_field_ref("[%s]" % TOKEN) == (None, TOKEN)

    def test_bare_text(self):
        assert _split_field_ref(TOKEN) == (None, TOKEN)

    def test_empty(self):
        assert _split_field_ref(None) == (None, "")


class TestDatasourcePalettes:
    def test_palette_is_keyed_by_datasource_and_token(self):
        root = _root([(DS, [("[%s]" % TOKEN, [("SkyConnect", "#26aba4")])])])
        assert _datasource_mark_color_palettes(root) == {
            (DS, TOKEN): [{"value": "SkyConnect", "color": "#26aba4"}]}

    def test_author_order_is_preserved(self):
        root = _root([(DS, [("[%s]" % TOKEN,
                             [("Up", "#49964f"), ("No", "#b3b7b8"), ("Down", "#e63946")])])])
        members = _datasource_mark_color_palettes(root)[(DS, TOKEN)]
        assert [m["value"] for m in members] == ["Up", "No", "Down"]

    def test_same_token_in_two_datasources_stays_distinct(self):
        """Two datasources may legitimately colour same-named fields differently."""
        root = _root([
            (DS, [("[%s]" % TOKEN, [("Up", "#49964f")])]),
            ("federated.xyz", [("[%s]" % TOKEN, [("Up", "#e63946")])]),
        ])
        pal = _datasource_mark_color_palettes(root)
        assert pal[(DS, TOKEN)][0]["color"] == "#49964f"
        assert pal[("federated.xyz", TOKEN)][0]["color"] == "#e63946"

    def test_conflicting_definitions_for_one_key_abstain(self):
        root = _root([(DS, [("[%s]" % TOKEN, [("Up", "#49964f")]),
                            ("[%s]" % TOKEN, [("Up", "#e63946")])])])
        assert _datasource_mark_color_palettes(root) == {}

    def test_identical_repeat_definitions_are_kept(self):
        root = _root([(DS, [("[%s]" % TOKEN, [("Up", "#49964f")]),
                            ("[%s]" % TOKEN, [("Up", "#49964f")])])])
        assert _datasource_mark_color_palettes(root)[(DS, TOKEN)][0]["color"] == "#49964f"

    def test_measure_names_palette_is_excluded(self):
        """Measure Names colours by measure identity and has its own reader + emit path."""
        root = _root([(DS, [("[:Measure Names]", [("[ds].[sum:Sales:qk]", "#2a52be")])])])
        assert _datasource_mark_color_palettes(root) == {}

    def test_continuous_gradient_is_excluded(self):
        root = ET.Element("workbook")
        holder = ET.SubElement(root, "datasources")
        ds = ET.SubElement(holder, "datasource", {"name": DS})
        style = ET.SubElement(ds, "style")
        rule = ET.SubElement(style, "style-rule", {"element": "mark"})
        enc = ET.SubElement(rule, "encoding", {"attr": "color", "field": "[%s]" % TOKEN})
        ET.SubElement(enc, "color-palette", {"name": "Blue"})
        mp = ET.SubElement(enc, "map", {"to": "#26aba4"})
        ET.SubElement(mp, "bucket").text = '"SkyConnect"'
        assert _datasource_mark_color_palettes(root) == {}

    def test_a_non_mark_style_rule_is_ignored(self):
        root = ET.Element("workbook")
        holder = ET.SubElement(root, "datasources")
        ds = ET.SubElement(holder, "datasource", {"name": DS})
        ds.append(_palette_style([("[%s]" % TOKEN, [("Up", "#49964f")])], element="axis"))
        assert _datasource_mark_color_palettes(root) == {}

    def test_a_non_color_encoding_is_ignored(self):
        root = ET.Element("workbook")
        holder = ET.SubElement(root, "datasources")
        ds = ET.SubElement(holder, "datasource", {"name": DS})
        style = ET.SubElement(ds, "style")
        rule = ET.SubElement(style, "style-rule", {"element": "mark"})
        enc = ET.SubElement(rule, "encoding", {"attr": "size", "field": "[%s]" % TOKEN})
        mp = ET.SubElement(enc, "map", {"to": "#26aba4"})
        ET.SubElement(mp, "bucket").text = '"SkyConnect"'
        assert _datasource_mark_color_palettes(root) == {}

    def test_an_empty_map_yields_no_palette(self):
        root = _root([(DS, [("[%s]" % TOKEN, [])])])
        assert _datasource_mark_color_palettes(root) == {}

    def test_no_datasources_yields_nothing(self):
        assert _datasource_mark_color_palettes(ET.Element("workbook")) == {}


class TestPaneColorColumns:
    def test_colors_are_collected_in_author_order(self):
        pane = _pane(["[a].[x]", "[a].[y]"])
        assert _pane_color_columns([pane]) == ["[a].[x]", "[a].[y]"]

    def test_duplicates_across_panes_are_deduped(self):
        assert _pane_color_columns([_pane(["[a].[x]"]), _pane(["[a].[x]"])]) == ["[a].[x]"]

    def test_no_panes_yields_nothing(self):
        assert _pane_color_columns(None) == []
        assert _pane_color_columns([]) == []

    def test_a_pane_with_no_color_yields_nothing(self):
        assert _pane_color_columns([ET.Element("pane")]) == []


class TestParseMarkColorsFallback:
    def test_datasource_palette_resolves_a_worksheet_with_no_local_style(self):
        pal = {(DS, TOKEN): [{"value": "SkyConnect", "color": "#26aba4"}]}
        got = _parse_mark_colors(_table(), pal, [QUALIFIED])
        assert got["members"] == [{"value": "SkyConnect", "color": "#26aba4"}]
        assert got["field_token"] == QUALIFIED

    def test_worksheet_local_palette_wins_over_the_datasource_one(self):
        """The worksheet is the more specific statement."""
        pal = {(DS, TOKEN): [{"value": "SkyConnect", "color": "#26aba4"}]}
        table = _table([("[%s]" % TOKEN, [("SkyConnect", "#ff0000")])])
        assert _parse_mark_colors(table, pal, [QUALIFIED])["members"][0]["color"] == "#ff0000"

    def test_colour_encodings_are_tried_in_author_order(self):
        pal = {(DS, "none:second:nk"): [{"value": "B", "color": "#222222"}],
               (DS, "none:first:nk"): [{"value": "A", "color": "#111111"}]}
        got = _parse_mark_colors(_table(), pal,
                                 ["[%s].[none:first:nk]" % DS, "[%s].[none:second:nk]" % DS])
        assert got["members"][0]["color"] == "#111111"

    def test_an_unassigned_first_encoding_falls_through_to_the_next(self):
        pal = {(DS, "none:second:nk"): [{"value": "B", "color": "#222222"}]}
        got = _parse_mark_colors(_table(), pal,
                                 ["[%s].[none:first:nk]" % DS, "[%s].[none:second:nk]" % DS])
        assert got["members"][0]["color"] == "#222222"

    def test_a_different_datasource_never_matches(self):
        pal = {("federated.other", TOKEN): [{"value": "SkyConnect", "color": "#26aba4"}]}
        assert _parse_mark_colors(_table(), pal, [QUALIFIED]) is None

    def test_no_palette_and_no_local_style_yields_nothing(self):
        assert _parse_mark_colors(_table(), {}, [QUALIFIED]) is None
        assert _parse_mark_colors(_table(), None, [QUALIFIED]) is None

    def test_no_colour_encoding_yields_nothing(self):
        pal = {(DS, TOKEN): [{"value": "SkyConnect", "color": "#26aba4"}]}
        assert _parse_mark_colors(_table(), pal, []) is None

    def test_returned_members_are_copies_not_the_shared_palette(self):
        """A caller mutating one worksheet's palette must not corrupt every other worksheet."""
        shared = [{"value": "SkyConnect", "color": "#26aba4"}]
        pal = {(DS, TOKEN): shared}
        got = _parse_mark_colors(_table(), pal, [QUALIFIED])
        got["members"][0]["color"] = "#000000"
        assert shared[0]["color"] == "#26aba4"

    def test_legacy_single_argument_call_still_works(self):
        table = _table([("[%s]" % TOKEN, [("SkyConnect", "#ff0000")])])
        assert _parse_mark_colors(table)["members"][0]["color"] == "#ff0000"

    def test_legacy_call_with_no_local_style_yields_nothing(self):
        assert _parse_mark_colors(_table()) is None
        assert _parse_mark_colors(None) is None
