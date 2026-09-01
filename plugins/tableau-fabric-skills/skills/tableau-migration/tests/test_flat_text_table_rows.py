"""A Tableau text table with a multi-level row shelf rebuilds FLAT, not as a collapsed matrix.

Tableau's text table gives every row-shelf field its own column and shows every row. A Power BI
matrix renders a multi-level row shelf as a STEPPED, COLLAPSED hierarchy, so a sheet whose rows are
Segment / Ship Mode / Order ID arrived as three expandable ``Segment`` rows instead of one row per
order -- the values were right and almost none of them were on screen.

``expansionStates`` does not fix that: it passes validation and is a no-op on initial render. A
``tableEx`` needs no expansion because it has no hierarchy to collapse, which is why the routing
changes rather than the formatting.

Boundaries under test, all of which must hold:
  * a genuine CROSS-TAB (real dimensions on BOTH axes) still needs the matrix;
  * a SINGLE row dimension stays a matrix -- one level has no hierarchy to collapse, so it already
    renders one row per member and switching it would change visuals that are correct today;
  * the no-dimension routes (measure table / BAN band) are untouched.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import twb_to_pbir as T  # noqa: E402


def _dim(name):
    return {"kind": "category", "name": name}


def _route(dims_rows, dims_cols, names_at="cols", values_at="text"):
    warnings = []
    return T._route_measure_values(
        "text", {"names": names_at, "values": values_at},
        ["Sales", "Profit"], 0, False, "keep",
        dims_rows, dims_cols, "Sheet 1", warnings)


def test_a_multi_level_row_shelf_rebuilds_as_a_flat_table():
    """The regression: rows Segment / Ship Mode / Order ID."""
    vt, _shelf, _note = _route([_dim("Segment"), _dim("Ship Mode"), _dim("Order ID")], [])
    assert vt == T.VT_TABLE


def test_two_row_dimensions_are_already_a_hierarchy():
    vt, _s, _n = _route([_dim("Segment"), _dim("Ship Mode")], [])
    assert vt == T.VT_TABLE


def test_a_single_row_dimension_stays_a_matrix():
    """One level has nothing to collapse, so it renders one row per member either way. Left alone
    deliberately: switching it would change three corpus visuals that are correct today."""
    vt, _s, _n = _route([_dim("Segment")], [])
    assert vt == T.VT_MATRIX


def test_a_genuine_cross_tab_still_needs_the_matrix():
    """Real dimensions on BOTH axes is what a matrix is for -- a flat table cannot express it."""
    vt, _s, _n = _route([_dim("Segment"), _dim("Ship Mode")], [_dim("Category")])
    assert vt == T.VT_MATRIX


def test_a_single_row_dimension_with_column_dimensions_stays_a_matrix():
    vt, _s, _n = _route([_dim("Segment")], [_dim("Category")])
    assert vt == T.VT_MATRIX


def test_columns_only_stays_a_matrix():
    vt, _s, _n = _route([], [_dim("Category")])
    assert vt == T.VT_MATRIX


def test_no_dimensions_with_measure_names_on_rows_is_still_a_measure_table():
    vt, _s, _n = _route([], [], names_at="rows")
    assert vt == T.VT_TABLE


def test_no_dimensions_with_measure_names_on_columns_is_still_a_ban_band():
    vt, _s, _n = _route([], [], names_at="cols", values_at="text")
    assert vt == T.VT_CARD


def test_the_member_measures_still_join_on_the_same_shelf():
    """Routing changed; the shelf the members inject on must not."""
    _vt, shelf, _n = _route([_dim("Segment"), _dim("Ship Mode")], [])
    assert shelf == "cols"


def test_the_fidelity_note_is_still_emitted():
    _vt, _s, note = _route([_dim("Segment"), _dim("Ship Mode")], [])
    assert note and "Measure Values" in note
