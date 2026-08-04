"""The Tableau mark-label TEMPLATE resolves which pills share one display slot.

A KPI "BAN" is ONE mark whose ``<customized-label><formatted-text>`` lays many pills out as a
block. That template is the only authoritative statement of layout, and it encodes slot membership
STRUCTURALLY: a maximal run of CONSECUTIVE field runs is one display slot, because Tableau writes
mutually exclusive alternatives (exactly one non-blank per period, its colour carrying the
direction) adjacently precisely so they occupy the same position.

Deliberately NO name matching. A ``Pos``/``Neg`` prefix is one author's habit, and matching on it
missed this workbook's third member ``Neut`` -- a defect the structural rule does not have.

``_parse_label_slots`` is a pure reader: nothing consumes it yet, so it cannot change any emitted
report. It is the foundation for collapsing a multi-measure slot into one value.
"""

import xml.etree.ElementTree as ET

import pytest

from scripts.twb_to_pbir import _parse_label_slots, _strip_label_control

DS = "federated.1abc"
BREAK = "\u00c6\n"   # Tableau's mark-label line break, verified in the raw workbook bytes


def _tok(name):
    return "<[%s].[usr:%s:qk]>" % (DS, name)


def _pane(runs):
    """A ``<pane>`` carrying ``customized-label/formatted-text`` with ``(text, attrs)`` runs."""
    pane = ET.Element("pane")
    ft = ET.SubElement(ET.SubElement(pane, "customized-label"), "formatted-text")
    for text, attrs in runs:
        ET.SubElement(ft, "run", attrs).text = text
    return pane


def _names(slot):
    return [t.split(":")[-2] for t in slot["tokens"]]


# The real ``Passenger Count BAN`` template, transcribed from the workbook.
BAN = [("All Passengers", {"fontcolor": "#666666"}),
       (BREAK, {}),
       (_tok("CM Count"), {"bold": "true", "fontcolor": "#333333", "fontsize": "14"}),
       (BREAK, {}),
       (_tok("Pos MoM Load"), {"fontcolor": "#49964f"}),
       (_tok("Pos MoM Pass"), {"fontcolor": "#e63946"}),
       (_tok("Neut MoM Pass"), {"fontcolor": "#b4b4b4"}),
       ("\u00c6", {}),
       (_tok("Footnote"), {"fontcolor": "#898989", "fontsize": "8"})]


class TestSlotGrouping:

    def test_the_real_ban_template_resolves_to_four_slots(self):
        slots = _parse_label_slots([_pane(BAN)])
        assert len(slots) == 4

    def test_adjacent_field_runs_form_one_slot(self):
        slots = _parse_label_slots([_pane(BAN)])
        assert _names(slots[2]) == ["Pos MoM Load", "Pos MoM Pass", "Neut MoM Pass"]

    def test_a_text_run_between_fields_splits_the_slot(self):
        """Adjacency is the whole rule -- a separator run means two positions, not one."""
        slots = _parse_label_slots([_pane([
            (_tok("A"), {}), ("of", {}), (_tok("B"), {})])])
        assert [_names(s) for s in slots if s["tokens"]] == [["A"], ["B"]]

    def test_a_whitespace_only_run_also_splits(self):
        """Tableau's line break is layout, and layout is exactly what separates slots."""
        slots = _parse_label_slots([_pane([
            (_tok("A"), {}), (BREAK, {}), (_tok("B"), {})])])
        assert [_names(s) for s in slots if s["tokens"]] == [["A"], ["B"]]

    def test_static_caption_is_its_own_slot_with_no_tokens(self):
        slots = _parse_label_slots([_pane(BAN)])
        assert slots[0]["text"] == "All Passengers" and slots[0]["tokens"] == []

    def test_layout_runs_never_become_captions(self):
        slots = _parse_label_slots([_pane(BAN)])
        assert all(s["text"] != "\u00c6" for s in slots)

    def test_slot_order_is_template_order(self):
        slots = _parse_label_slots([_pane(BAN)])
        assert [bool(s["tokens"]) for s in slots] == [False, True, True, True]

    def test_two_tokens_inside_ONE_run_are_one_slot(self):
        """Tableau can put both pills in a single run; that is the same adjacency."""
        slots = _parse_label_slots([_pane([(_tok("A") + _tok("B"), {})])])
        assert _names(slots[0]) == ["A", "B"]


class TestRunFormatting:

    def test_bold_and_size_mark_the_primary_slot(self):
        slots = _parse_label_slots([_pane(BAN)])
        assert slots[1]["bold"] is True and slots[1]["size"] is not None

    def test_the_delta_slot_is_not_bold(self):
        slots = _parse_label_slots([_pane(BAN)])
        assert slots[2]["bold"] is False

    def test_each_member_keeps_its_own_colour(self):
        """Colour carries direction, so it is per-member, not per-slot."""
        slots = _parse_label_slots([_pane(BAN)])
        assert slots[2]["colors"] == ["#49964f", "#e63946", "#b4b4b4"]

    def test_colours_align_one_to_one_with_tokens(self):
        slots = _parse_label_slots([_pane(BAN)])
        assert all(len(s["colors"]) == len(s["tokens"]) for s in slots)

    def test_a_non_hex_colour_is_recorded_as_none(self):
        slots = _parse_label_slots([_pane([(_tok("A"), {"fontcolor": "red"})])])
        assert slots[0]["colors"] == [None]

    def test_bold_on_any_member_marks_the_slot(self):
        slots = _parse_label_slots([_pane([
            (_tok("A"), {}), (_tok("B"), {"bold": "true"})])])
        assert slots[0]["bold"] is True


class TestAbsentTemplate:

    def test_no_customized_label_returns_none(self):
        assert _parse_label_slots([ET.Element("pane")]) is None

    def test_no_panes_returns_none(self):
        assert _parse_label_slots([]) is None
        assert _parse_label_slots(None) is None

    def test_an_empty_template_returns_none(self):
        assert _parse_label_slots([_pane([])]) is None

    def test_a_layout_only_template_returns_none(self):
        """A template with nothing but spacers describes no slot at all."""
        assert _parse_label_slots([_pane([(BREAK, {}), ("\u00c6", {})])]) is None

    def test_the_first_pane_carrying_a_template_wins(self):
        slots = _parse_label_slots([ET.Element("pane"), _pane([(_tok("A"), {})])])
        assert _names(slots[0]) == ["A"]


class TestLayoutMarkerStripping:
    """``\u00c6`` is Tableau's break marker AND a real letter; only layout runs are dropped."""

    @pytest.mark.parametrize("text", ["\u00c6\n", "\u00c6\r\n", "\u00c6", "  \u00c6  ", "\n", ""])
    def test_layout_only_runs_yield_no_caption(self, text):
        assert _strip_label_control(text) == ""

    def test_authored_text_is_preserved(self):
        assert _strip_label_control("All Passengers") == "All Passengers"

    def test_a_caption_containing_the_marker_is_not_mutilated(self):
        """The marker is only meaningful as layout; inside real text it is just a letter."""
        assert _strip_label_control("\u00c6ON Corp") == "\u00c6ON Corp"

    def test_a_trailing_break_is_removed_from_real_text(self):
        assert _strip_label_control("Revenue\u00c6\n") == "Revenue"

    def test_none_is_safe(self):
        assert _strip_label_control(None) == ""


class TestMutuallyExclusiveGroupIsDetectable:
    """What the collapse will key on -- recorded here so the contract is pinned before wiring."""

    def test_the_delta_slot_has_more_than_one_member(self):
        slots = _parse_label_slots([_pane(BAN)])
        assert len([s for s in slots if len(s["tokens"]) > 1]) == 1

    def test_the_delta_slot_members_have_distinct_colours(self):
        """Distinct colours corroborate alternation; identical colours mean concatenation."""
        slots = _parse_label_slots([_pane(BAN)])
        multi = [s for s in slots if len(s["tokens"]) > 1][0]
        assert len(set(multi["colors"])) == len(multi["colors"])

    def test_same_coloured_adjacent_fields_are_not_an_alternation(self):
        """The A320 tile concatenates two same-coloured pills; collapsing it would lose data."""
        slots = _parse_label_slots([_pane([
            (_tok("Calc"), {"fontcolor": "#333333", "bold": "true"}),
            (_tok("Type"), {"fontcolor": "#333333", "bold": "true"})])])
        assert len(set(slots[0]["colors"])) == 1
