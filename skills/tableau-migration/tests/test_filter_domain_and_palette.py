"""Filter cards must keep the author's domain and selection; named diverging palettes their order.

Three defects measured together on one customer highlight table, each an independent whole-class
bug in how an authored CONTROL or COLOUR is carried across.

1. **A date filter card rebound to the shared calendar.** ``_rebind_date_axis`` exists to route a
   date AXIS through the marked Date dimension so time intelligence works. Applied to a FILTER it
   is destructive: Tableau's card enumerates the distinct values present in the FACT column (a
   discrete ``FiscalMonth`` holds three or four month stamps), while the calendar is a generated
   contiguous range, so the rebuilt control listed EVERY day it spanned. Worse, the authored
   selection is a fact-column member, so the preselection gate could no longer prove it was in the
   bound domain, declined, and the slicer opened on "All" -- the page rendered unfiltered and every
   number was wrong behind a control that looked right. Date PARTS still rebind (a Year filter
   wants the calendar's integer ``Year`` column, whose members match the integer literals Tableau
   writes); parts NARROW the domain, the exact-date key column EXPLODES it.

2. **An enumerated keep-list read as "no selection".** Tableau writes a union keep-list two ways:
   ``op='manual'``, or its intent attribute ``ui-enumeration='inclusive'`` with
   ``ui-marker='enumerate'`` and no ``op``. Only the first was read. Across every real workbook
   available, ``ui-enumeration`` is perfectly consistent -- ``all`` on the non-narrowing
   ``level-members`` form, ``inclusive`` on enumerated keeps (including the single-``member``
   include shape already honoured), ``exclusive`` only ever with ``except`` -- so the second form
   is unambiguously a keep and was being dropped.

3. **A three-hue diverging palette rebuilt with the wrong ends and no midpoint.** Tableau names a
   diverging palette after its two ENDS then its MIDDLE (``red_blue_white_diverging`` = red ..
   white .. blue). Reading it as "first .. neutral .. last" put the wrong hue at the top end and
   replaced the author's midpoint with grey, so a rank column coloured green-through-gold-to-red
   came out red-through-grey-to-gold -- an INVERSION of the reader's good/bad intuition, which is
   worse than no colour at all.
"""
import xml.etree.ElementTree as ET

import twb_to_pbir as R

_DATE_BINDING = {
    "date_table": "Date",
    "key_column": "Date",
    "active_keys": ["FiscalMonth"],
    "grain_columns": {"Year": "Year", "Month": "Month"},
}


# -- 1. a filter card keeps the fact column ------------------------------------------------

def _field():
    return {"caption": "Fiscal Month", "property": "FiscalMonth", "entity": "CombinedData",
            "role": "dimension", "datatype": "date"}


def test_exact_date_axis_still_rebinds_to_the_calendar():
    """No regression: an AXIS pill keeps routing through the marked Date table."""
    r = R._rebind_date_axis(_field(), "None", _DATE_BINDING)
    assert r == {"entity": "Date", "property": "Date"}


def test_exact_date_filter_keeps_the_fact_column():
    assert R._rebind_date_axis(_field(), "None", _DATE_BINDING, for_filter=True) is None


def test_date_part_filter_still_rebinds():
    """Parts narrow the domain and match Tableau's integer literals -- they must keep rebinding."""
    assert R._rebind_date_axis(_field(), "Year", _DATE_BINDING, for_filter=True) == {
        "entity": "Date", "property": "Year"}


def test_exact_date_value_filter_keeps_the_fact_column():
    for deriv in R._DATE_EXACT_DERIVATIONS:
        assert R._rebind_date_axis(_field(), deriv, _DATE_BINDING, for_filter=True) is None


# -- 2. the enumerated keep-list ------------------------------------------------------------

_NS = "xmlns:user='http://www.tableausoftware.com/xml/user'"


def _filt(inner):
    return ET.fromstring("<filter class='categorical' %s>%s</filter>" % (_NS, inner))


_UI_ENUM = (
    "<groupfilter function='union' user:ui-domain='database' user:ui-enumeration='inclusive'"
    " user:ui-marker='enumerate'>"
    "<groupfilter function='member' level='[none:FiscalMonth:ok]' member='#2026-06-21#' />"
    "<groupfilter function='member' level='[none:FiscalMonth:ok]' member='#2026-07-21#' />"
    "</groupfilter>")

_MANUAL = (
    "<groupfilter function='union' op='manual'>"
    "<groupfilter function='member' level='[none:Region:nk]' member='&quot;West&quot;' />"
    "</groupfilter>")

_ALL = ("<groupfilter function='level-members' level='[none:Region:nk]'"
        " user:ui-enumeration='all' user:ui-marker='enumerate' />")

_EXCEPT = ("<groupfilter function='except' user:ui-enumeration='exclusive'"
           " user:ui-marker='enumerate'>"
           "<groupfilter function='member' level='[none:Region:nk]' member='&quot;East&quot;' />"
           "</groupfilter>")


def test_ui_enumeration_inclusive_union_is_a_keep_list():
    assert R._parse_filter_selection(_filt(_UI_ENUM)) == {
        "mode": "include", "values": ["#2026-06-21#", "#2026-07-21#"]}


def test_manual_union_keep_list_unchanged():
    assert R._parse_filter_selection(_filt(_MANUAL)) == {"mode": "include", "values": ["West"]}


def test_all_members_is_still_no_selection():
    assert R._parse_filter_selection(_filt(_ALL)) is None


def test_exclusive_except_is_still_an_exclude():
    assert R._parse_filter_selection(_filt(_EXCEPT)) == {"mode": "exclude", "values": ["East"]}


def test_union_with_no_intent_attribute_still_declines():
    """Warn-never-wrong: a union that states neither ``manual`` nor ``inclusive`` is not read."""
    bare = ("<groupfilter function='union'>"
            "<groupfilter function='member' level='[l]' member='&quot;X&quot;' />"
            "</groupfilter>")
    assert R._parse_filter_selection(_filt(bare)) is None


# -- 3. named diverging palette order -------------------------------------------------------

def test_three_hue_diverging_uses_ends_then_middle():
    """``red_green_gold`` -> red (low) .. gold (mid) .. green (high)."""
    assert R._named_family_stops("red_green_gold_diverging_10_0", True) == [
        R._NAMED_HUE_STOPS["red"], R._NAMED_HUE_STOPS["gold"], R._NAMED_HUE_STOPS["green"]]


def test_three_hue_diverging_with_white_midpoint():
    assert R._named_family_stops("red_blue_white_diverging_10_0", True) == [
        R._NAMED_HUE_STOPS["red"], "#ffffff", R._NAMED_HUE_STOPS["blue"]]


def test_two_hue_diverging_keeps_the_neutral_middle():
    assert R._named_family_stops("orange_blue_diverging_10_0", True) == [
        R._NAMED_HUE_STOPS["orange"], R._NAMED_NEUTRAL_MID, R._NAMED_HUE_STOPS["blue"]]


def test_sequential_named_palette_unchanged():
    assert R._named_family_stops("blue_10_0", False) == ["#f7f7f7", R._NAMED_HUE_STOPS["blue"]]


def test_unrecognised_name_still_declines():
    assert R._named_family_stops("some_unknown_ramp", True) is None


def test_reverse_flips_a_three_hue_diverging_ramp():
    """The customer encoding is ``reverse='true'`` over red_green_gold -> green .. gold .. red."""
    spec = R._default_continuous_gradient(
        {"palette": "red_green_gold_diverging_10_0", "reverse": "true", "field": "[ds].[f]"})
    assert spec["colors"] == [R._NAMED_HUE_STOPS["green"], R._NAMED_HUE_STOPS["gold"],
                              R._NAMED_HUE_STOPS["red"]]
