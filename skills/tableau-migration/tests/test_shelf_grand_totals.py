"""Tableau's grand total is a declaration; Power BI's is a DEFAULT. Emitting nothing picks the default.

Tableau writes the grand total on the shelf element and never uses the word "grand"::

    <rows onTop='true' total='true'>[Superstore].[none:Sub-Category:nk]</rows>

``total`` turns the grand total on, ``onTop`` puts the row at the TOP instead of the bottom. Searching
a workbook for "grand" finds product names and nothing else, which is how both facts were previously
recorded as "not in the .twb" -- an inference from a keyword that Tableau does not use.

The reason this matters is asymmetric. **Power BI's table shows a Total row by default**, so writing
no toggle is not neutrality, it is a choice, and it is the wrong one for nearly every view. Measured
across the corpus of 34: **252 of 262 shelves declare no grand total**, while all **42** emitted grid
visuals set no toggle and therefore inherited the default. That is an extra row of plausible numbers
on tables whose source never showed one -- and an addition is harder to notice than an omission,
because it looks exactly like data.

The position half is a genuine platform limit rather than a gap in the rebuild. Looked up in the
visual-type schema rather than assumed: a flat ``tableEx`` exposes exactly one total control,
``total.totals`` (bool), with **no** position property; a matrix's ``rowSubtotalsPosition`` governs
per-group SUBTOTALS, not the grand total. So the row is rebuilt where Power BI will put it and the
move is disclosed -- silently relocating the one row a reader looks at first is the failure mode this
avoids.
"""
import json

import twb_to_pbir as R

from test_twb_to_pbir import (  # noqa: F401  -- shared inline-XML fixture builders
    _INST,
    _workbook,
    _worksheet,
    migrate_twb_to_pbir,
)

_ROWS = "[federated.abc].[none:Category:nk]"
_MEASURE = "[federated.abc].[sum:Sales:qk]"


def _table(rows_attrs=""):
    """A crosstab worksheet whose ``<rows>`` carries the given Tableau total attributes."""
    ws = _worksheet("Grid", "Text", rows=_ROWS, cols=_MEASURE, deps_extra=_INST)
    if rows_attrs:
        old, new = "<rows>", "<rows %s>" % rows_attrs
        assert ws.count(old) == 1, "fixture shape changed: expected exactly one <rows>"
        ws = ws.replace(old, new, 1)
        assert new in ws
    return _workbook(ws)


def _grid_visual(res):
    for part, body in res["parts"].items():
        if not part.endswith("visual.json"):
            continue
        v = json.loads(body) if isinstance(body, str) else body
        vt = (v.get("visual") or {}).get("visualType")
        if vt in ("tableEx", "pivotTable"):
            return v["visual"]
    raise AssertionError("no grid visual emitted")


def _totals_flag(res):
    objs = (_grid_visual(res).get("objects") or {}).get("total") or [{}]
    prop = (objs[0].get("properties") or {}).get("totals")
    return None if prop is None else prop["expr"]["Literal"]["Value"]


# =============================================================================
# The parse.
# =============================================================================
def test_shelf_totals_reads_total_and_on_top():
    import xml.etree.ElementTree as ET
    rows = ET.fromstring("<rows total='true' onTop='true'>x</rows>")
    cols = ET.fromstring("<cols total='true'>y</cols>")
    assert R._parse_shelf_totals(rows, cols) == {
        "rows": True, "rows_on_top": True, "cols": True, "cols_on_top": False}


def test_shelf_totals_absent_attributes_are_false():
    import xml.etree.ElementTree as ET
    rows = ET.fromstring("<rows>x</rows>")
    assert R._parse_shelf_totals(rows, None) == {
        "rows": False, "rows_on_top": False, "cols": False, "cols_on_top": False}


# =============================================================================
# The emitted artifact -- both directions, because the default is the hazard.
# =============================================================================
def test_a_table_whose_source_declares_no_total_suppresses_it():
    # THE defect: Power BI would otherwise add a Total row this workbook never showed. 252 of the
    # corpus's 262 shelves are this shape.
    res = migrate_twb_to_pbir(_table(), dataset_name="M", report_name="R")
    assert _totals_flag(res) == "false"


def test_a_table_whose_source_declares_a_total_keeps_it():
    res = migrate_twb_to_pbir(_table("total='true'"), dataset_name="M", report_name="R")
    assert _totals_flag(res) == "true"


def test_the_toggle_is_always_written_so_the_default_never_decides():
    # The point is explicitness in BOTH directions: a reader of the .pbip can see what was intended
    # rather than inherit whatever Power BI's default happens to be in a given release.
    for attrs in ("", "total='true'"):
        assert _totals_flag(migrate_twb_to_pbir(
            _table(attrs), dataset_name="M", report_name="R")) is not None


# =============================================================================
# The position, which Power BI cannot express.
# =============================================================================
def test_a_top_positioned_total_is_disclosed_not_silently_moved():
    res = migrate_twb_to_pbir(_table("total='true' onTop='true'"),
                              dataset_name="M", report_name="R")
    assert _totals_flag(res) == "true"
    assert any("shown at the TOP" in (w.get("reason") or "") for w in res["warnings"]), \
        "moving the grand total row must be disclosed"


def test_a_bottom_positioned_total_warns_about_nothing():
    # The common case must stay quiet, or the disclosure becomes noise and stops being read.
    res = migrate_twb_to_pbir(_table("total='true'"), dataset_name="M", report_name="R")
    assert not any("shown at the TOP" in (w.get("reason") or "") for w in res["warnings"])


def test_no_total_no_position_warning():
    res = migrate_twb_to_pbir(_table(), dataset_name="M", report_name="R")
    assert not any("shown at the TOP" in (w.get("reason") or "") for w in res["warnings"])
