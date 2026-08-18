"""A container-stitched pseudo-table is detected from the source, and disclosed.

Tableau cannot put several independently table-calculated measures in one view, so authors fake a
single table: N worksheets in a contiguous horizontal container, each contributing one measure, with
the row labels suppressed on every sheet but the leading one. The dashboard then READS as one table.
Power BI expresses that natively in a single matrix.

THE SIGNATURE IS EXACT IN THE SOURCE and needs no image, heuristic or model call:

  * the zones form a contiguous horizontal band -- same y, same h, each x continuing where the
    previous zone ended;
  * every member groups by the SAME row dimension;
  * the TRAILING members hide their row labels and the LEADER does not. That asymmetry is the whole
    gate: it separates a stitched pseudo-table from tables that merely sit side by side.

The negatives matter as much as the positive, and the source workbook supplies both of them:
a single-sheet MEASURE TRELLIS renders an almost identical picture from completely different source,
and a BAR-mark band is already rebuilt correctly because the engine suppresses its category axes.
Neither must be merged. That is also the concrete argument for classifying from XML rather than from
a rendered image -- the image cannot separate cases that look the same.
"""

import twb_to_pbir as T


def _ws(name, *, hides_labels=False, row="Swap Calc", vtype=None):
    return {
        "name": name,
        "visual_type": vtype or T.VT_TABLE,
        "rows": [{"caption": row, "role": "dimension"}],
        "cols": [],
        "encodings": {},
        "row_labels_hidden": hides_labels,
        "axis_hidden": [],
    }


def _zone(name, x, w=32800, y=18750, h=80250):
    return {"worksheet": name, "zone_id": name, "x": x, "y": y, "w": w, "h": h}


def _band(*specs):
    """specs: (name, x, hides_labels) -> (dashboard, ws_by_name)."""
    ws_by = {n: _ws(n, hides_labels=h) for n, _x, h in specs}
    db = {"name": "Dashboard 1", "zones": [_zone(n, x) for n, x, _h in specs]}
    return db, ws_by


THREE_STITCHED = (("Profit", 800, False), ("Sales", 33600, True), ("Quantity", 66400, True))


class TestTheStitchedBandIsDetected:
    def test_the_canonical_three_sheet_band(self):
        db, ws_by = _band(*THREE_STITCHED)
        bands = T.detect_stitched_table_band(db, ws_by)

        assert len(bands) == 1
        assert bands[0]["leader"] == "Profit"
        assert bands[0]["members"] == ["Profit", "Sales", "Quantity"]

    def test_two_sheets_are_enough(self):
        db, ws_by = _band(("Profit", 800, False), ("Sales", 33600, True))

        assert len(T.detect_stitched_table_band(db, ws_by)) == 1

    def test_a_one_unit_gap_from_grid_rounding_is_tolerated(self):
        db, ws_by = _band(("Profit", 800, False), ("Sales", 33601, True))

        assert len(T.detect_stitched_table_band(db, ws_by)) == 1


class TestTheNearMissesAreDeclined:
    def test_a_single_sheet_is_not_a_band(self):
        db, ws_by = _band(("Profit", 800, False))

        assert T.detect_stitched_table_band(db, ws_by) == []

    def test_tables_that_all_show_their_labels_are_left_alone(self):
        """Three ordinary tables side by side are three tables, not a stitched one."""
        db, ws_by = _band(("A", 800, False), ("B", 33600, False), ("C", 66400, False))

        assert T.detect_stitched_table_band(db, ws_by) == []

    def test_a_leader_that_also_hides_its_labels_is_not_a_stitch(self):
        """With no visible labels anywhere there is no single table being faked."""
        db, ws_by = _band(("A", 800, True), ("B", 33600, True))

        assert T.detect_stitched_table_band(db, ws_by) == []

    def test_a_gap_between_zones_breaks_the_band(self):
        db, ws_by = _band(("Profit", 800, False), ("Sales", 40000, True))

        assert T.detect_stitched_table_band(db, ws_by) == []

    def test_different_rows_break_the_band(self):
        db = {"name": "d", "zones": [_zone("A", 800), _zone("B", 33600)]}
        ws_by = {"A": _ws("A", row="Region"), "B": _ws("B", row="Segment", hides_labels=True)}

        assert T.detect_stitched_table_band(db, ws_by) == []

    def test_a_different_row_height_breaks_the_band(self):
        db = {"name": "d", "zones": [_zone("A", 800), _zone("B", 33600, h=40000)]}
        ws_by = {"A": _ws("A"), "B": _ws("B", hides_labels=True)}

        assert T.detect_stitched_table_band(db, ws_by) == []

    def test_bar_marks_are_never_merged(self):
        """The bar variant already rebuilds correctly -- merging it would be a regression."""
        db = {"name": "d", "zones": [_zone("A", 800), _zone("B", 33600)]}
        ws_by = {"A": _ws("A", vtype=T.VT_BAR),
                 "B": _ws("B", vtype=T.VT_BAR, hides_labels=True)}

        assert T.detect_stitched_table_band(db, ws_by) == []

    def test_a_sheet_with_no_row_dimension_is_ignored(self):
        db = {"name": "d", "zones": [_zone("A", 800), _zone("B", 33600)]}
        ws_by = {"A": _ws("A"), "B": dict(_ws("B", hides_labels=True), rows=[])}

        assert T.detect_stitched_table_band(db, ws_by) == []

    def test_an_empty_dashboard_is_safe(self):
        assert T.detect_stitched_table_band({"name": "d", "zones": []}, {}) == []
        assert T.detect_stitched_table_band({}, {}) == []
        assert T.detect_stitched_table_band(None, None) == []


class TestTwoBandsOnOneDashboard:
    def test_two_separate_bands_are_both_found(self):
        ws_by = {n: _ws(n, hides_labels=h) for n, h in
                 (("A", False), ("B", True), ("C", False), ("D", True))}
        db = {"name": "d", "zones": [
            _zone("A", 800), _zone("B", 33600),
            _zone("C", 800, y=60000), _zone("D", 33600, y=60000)]}
        bands = T.detect_stitched_table_band(db, ws_by)

        assert [b["members"] for b in bands] == [["A", "B"], ["C", "D"]]


class TestTheRowLabelHideIsParsedForTables:
    def test_the_signal_is_read_from_the_worksheet_record(self):
        """`row_labels_hidden` exists BECAUSE the axis path drops it for a crosstab.

        `_parse_hidden_axes` maps a hide onto a Power BI axis, which needs the shelf's role; it
        resolves for a cartesian chart and yields nothing for a table, because a table has no
        category axis. The fact was parsed and then discarded for exactly the visual type this
        idiom uses.
        """
        assert T._band_hides_row_labels(_ws("x", hides_labels=True)) is True
        assert T._band_hides_row_labels(_ws("x", hides_labels=False)) is False

    def test_a_hidden_category_axis_also_counts(self):
        """The chart spelling still works, so a bar band is recognised by the same predicate."""
        ws = dict(_ws("x"), axis_hidden=["categoryAxis"])

        assert T._band_hides_row_labels(ws) is True
