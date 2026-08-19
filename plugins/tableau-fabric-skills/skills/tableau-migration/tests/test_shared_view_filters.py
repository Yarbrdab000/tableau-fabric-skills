"""A filter scoped to ALL WORKSHEETS USING THIS DATA SOURCE is not written on the worksheet.

Tableau serialises filter scope structurally, and the two shapes live in different places:

* *Apply to -> Only This Worksheet* writes ``<filter>`` inside the worksheet's own ``<view>``.
* *Apply to -> All Worksheets Using This Data Source* HOISTS the filter out to a workbook-level
  ``<shared-views><shared-view name='<datasource>'>`` block, and leaves each participating worksheet
  only a ``<slices><column>`` naming the sliced field.

The parser read worksheet-local ``<filter>`` elements only, so every filter authored in the second
(entirely ordinary) scope vanished -- and with it every dashboard filter CARD, because a card is
little more than a ``(datasource, field-instance)`` token that must resolve through a matching
worksheet filter.

Measured on two corpus workbooks that differ in this scope and almost nothing else:

* ``0132_container-formatting-hidden-headers`` keeps its three filters on worksheet ``Profit`` and
  rebuilt all three slicers.
* ``0133_container-formatting-variations`` hoists the byte-identical three into ``<shared-views>``
  and rebuilt NONE -- nine cards across three dashboards, each disclosed as "resolved to no model
  field" and otherwise invisible.

The dashboard zone tokens are IDENTICAL in both workbooks; only the resolver map differed (3 entries
against 0). That is why the failure presented as a filter-card problem and was really a parse
problem, and why the fix belongs at the parse seam rather than at the card.

What wrong looks like, so the choice is defensible later: the broken form does not render a broken
slicer, it renders NO slicer, on a page that otherwise looks complete -- the reader sees a dashboard
missing an interaction they never knew was authored.
"""
import xml.etree.ElementTree as ET

import twb_to_pbir as R

from test_twb_to_pbir import (  # noqa: F401  -- shared inline-XML fixture builders
    _INST,
    _workbook,
    _worksheet,
)

_DS = "federated.abc"
_CATEGORY = "[federated.abc].[none:Category:nk]"
_SEGMENT = "[federated.abc].[none:Segment:nk]"


def _member_filter(column, member):
    return ("<filter class='categorical' column='%s'><groupfilter function='member' "
            "level='%s' member='&quot;%s&quot;' /></filter>"
            % (column, column.split(".")[-1].strip("[]"), member))


def _slices(*columns):
    return "<slices>" + "".join("<column>%s</column>" % c for c in columns) + "</slices>"


def _shared_views(*filters):
    return ("<shared-views><shared-view name='Superstore'>"
            + "".join(filters) + "</shared-view></shared-views>")


def _wb(worksheets, shared=""):
    """``<shared-views>`` is a direct child of ``<workbook>``, as Tableau writes it."""
    xml = _workbook(worksheets)
    return xml.replace("<worksheets>", shared + "<worksheets>", 1) if shared else xml


def _sheet(name, view_extra):
    return _worksheet(name, "Bar",
                      rows="[federated.abc].[sum:Sales:qk]",
                      cols="[federated.abc].[none:Category:nk]",
                      deps_extra=_INST, filters=view_extra)


def _filters_of(ir, name):
    for ws in ir["worksheets"]:
        if ws["name"] == name:
            return ws.get("filters") or []
    raise AssertionError("worksheet %r not parsed" % name)


# =============================================================================
# The index and the per-sheet selection, in isolation.
# =============================================================================
def test_shared_view_filters_are_indexed_by_their_token():
    root = ET.fromstring(_wb(_sheet("S", ""), _shared_views(_member_filter(_CATEGORY, "Furniture"))))
    idx = R._shared_view_filter_index(root)
    assert list(idx) == [(_DS, "none:Category:nk")]


def test_a_workbook_with_no_shared_views_indexes_nothing():
    # The overwhelmingly common shape: no shared views, so the index is empty and every downstream
    # call is byte-for-byte what it was.
    root = ET.fromstring(_wb(_sheet("S", "")))
    assert R._shared_view_filter_index(root) == {}


def test_a_sheet_inherits_only_the_columns_its_own_slices_name():
    # ``<slices>`` is Tableau's per-sheet record of what it is sliced by, so it is the participation
    # signal. A sheet that names one of two shared filters inherits exactly that one -- guessing
    # "every sheet using this datasource" would hand it a filter it does not have.
    shared = _shared_views(_member_filter(_CATEGORY, "Furniture"),
                           _member_filter(_SEGMENT, "Consumer"))
    root = ET.fromstring(_wb(_sheet("S", _slices(_CATEGORY)), shared))
    idx = R._shared_view_filter_index(root)
    ws = root.find(".//worksheet")
    got = R._worksheet_shared_filters(ws, idx)
    assert [f.get("column") for f in got] == [_CATEGORY]


def test_a_sheet_with_no_slices_inherits_nothing():
    shared = _shared_views(_member_filter(_CATEGORY, "Furniture"))
    root = ET.fromstring(_wb(_sheet("S", ""), shared))
    ws = root.find(".//worksheet")
    assert R._worksheet_shared_filters(ws, R._shared_view_filter_index(root)) == []


# =============================================================================
# End to end through parse_twb -- the artifact the dashboard card resolves against.
# =============================================================================
def test_a_hoisted_filter_reaches_the_worksheet_that_slices_it():
    # The defect, at the seam where it was measured: no worksheet-local <filter> at all, and the
    # sheet must still come back carrying the filter and its raw token.
    shared = _shared_views(_member_filter(_CATEGORY, "Furniture"))
    ir = R.parse_twb(_wb(_sheet("Profit", _slices(_CATEGORY)), shared))
    fs = _filters_of(ir, "Profit")
    assert [f["filter_token"] for f in fs] == [(_DS, "none:Category:nk")]


def test_the_card_resolver_map_sees_a_hoisted_filter():
    # The consequence that mattered: the dashboard filter-card resolver is built from worksheet
    # filters, so an empty map is what dropped all nine cards on 0133.
    shared = _shared_views(_member_filter(_CATEGORY, "Furniture"))
    ir = R.parse_twb(_wb(_sheet("Profit", _slices(_CATEGORY)), shared))
    by_token = R._filter_fields_by_token(ir["worksheets"])
    assert (_DS, "none:Category:nk") in by_token


def test_a_worksheet_filter_wins_over_the_hoisted_one_on_the_same_column():
    # A sheet-level filter is more specific than the inherited one, and duplicating the column would
    # emit the same slicer twice. The local SELECTION is the one that must survive.
    shared = _shared_views(_member_filter(_CATEGORY, "Technology"))
    ws = _sheet("Profit", _member_filter(_CATEGORY, "Furniture") + _slices(_CATEGORY))
    fs = _filters_of(R.parse_twb(_wb(ws, shared)), "Profit")
    assert len(fs) == 1
    assert fs[0]["selection"]["values"] == ["Furniture"]


def test_sheets_sharing_one_hoisted_filter_each_receive_it():
    # The scope's whole point: one authored filter, every participating sheet. 0133 has seven such
    # sheets, which is why a single missed parse cost nine cards across three dashboards.
    shared = _shared_views(_member_filter(_CATEGORY, "Furniture"))
    sheets = _sheet("Profit", _slices(_CATEGORY)) + _sheet("Sales", _slices(_CATEGORY))
    ir = R.parse_twb(_wb(sheets, shared))
    assert [f["filter_token"] for f in _filters_of(ir, "Profit")] == [(_DS, "none:Category:nk")]
    assert [f["filter_token"] for f in _filters_of(ir, "Sales")] == [(_DS, "none:Category:nk")]


def test_a_workbook_without_shared_views_keeps_its_local_filter_only():
    # Back-compat pin: the ordinary worksheet-scoped shape is untouched by any of this.
    ws = _sheet("Profit", _member_filter(_CATEGORY, "Furniture"))
    fs = _filters_of(R.parse_twb(_wb(ws)), "Profit")
    assert [f["filter_token"] for f in fs] == [(_DS, "none:Category:nk")]
    assert fs[0]["selection"]["values"] == ["Furniture"]
