"""The Visual-Calculation axis is read from the visual's own grouping roles.

A Visual Calculation walks the visual's result matrix along an axis, so the axis has to be one the
visual actually groups on. The formula table-calc path hardcoded ``axis="ROWS"`` at its single call
site, which is right for a cartesian chart and silently degenerate for a matrix grouped only on
COLUMNS: the ROWS axis then holds exactly ONE row, so a whole-partition window spans a single value.

That is the validate-clean/render-wrong class:

* ``AVERAGEX`` over one row returns that row's value -- wrong, and plausible enough that no static
  check can see it;
* ``STDEVX.S`` over one row divides by ``n - 1 = 0`` and renders the literal text ``NaN``.

Measured end to end on ``0074_control_chart``, whose matrix carries ``Columns=[Month Start]`` and no
``Rows``. OCR of the real Power BI Desktop render, before and after, with the pre-fix image as the
positive control that the reader is looking at the right region:

    PRE-FIX    Sales $139,730   Lower NaN          Upper NaN
    POST-FIX   Sales $139,730   Lower 94,351.10    Upper 278,646.04

and those two numbers are exactly what the engine computes independently over the 48-month
partition (mean 186,498.57, sample sd 92,147.47, k = 1). The engine also reproduces the defect
directly: ``STDEVX.S`` over a single row returns ``nan``.

The fix is deliberately narrow -- only the provably-degenerate case moves -- so the corpus is
byte-identical apart from that one visual.json.
"""
import pytest

from twb_to_pbir import _formula_vc_axis


def _state(rows=0, cols=0):
    """A visual state with ``rows``/``cols`` grouping projections."""
    out = {}
    if rows:
        out["Rows"] = {"projections": [{"n": i} for i in range(rows)]}
    if cols:
        out["Columns"] = {"projections": [{"n": i} for i in range(cols)]}
    return out


def test_a_matrix_grouped_only_on_columns_walks_the_columns_axis():
    """The defect case. ROWS here spans one row, so STDEVX.S renders NaN."""
    assert _formula_vc_axis(_state(rows=0, cols=1), is_chart=False) == "COLUMNS"
    assert _formula_vc_axis(_state(rows=0, cols=3), is_chart=False) == "COLUMNS"


def test_a_matrix_grouped_on_rows_keeps_the_rows_axis():
    assert _formula_vc_axis(_state(rows=1, cols=0), is_chart=False) == "ROWS"
    assert _formula_vc_axis(_state(rows=2, cols=0), is_chart=False) == "ROWS"


def test_a_matrix_grouped_on_BOTH_axes_keeps_todays_answer():
    """Picking a direction here needs the Tableau ordering token, which this path does not carry.
    Guessing would trade a known shape for an unevidenced one, so it stays ROWS."""
    assert _formula_vc_axis(_state(rows=1, cols=1), is_chart=False) == "ROWS"
    assert _formula_vc_axis(_state(rows=2, cols=3), is_chart=False) == "ROWS"


def test_a_cartesian_chart_is_always_ROWS_even_with_no_row_grouping():
    """A chart's category axis IS the rows of its result matrix -- a structural fact of charts, and
    the same reason ``visual_calc_spec``'s ``visual_axis`` override exists for the QTC path. A chart
    state carries Category/Y, never Rows, so without this guard every chart would flip to COLUMNS
    and every already-correct chart visual calc would break."""
    assert _formula_vc_axis(_state(rows=0, cols=0), is_chart=True) == "ROWS"
    assert _formula_vc_axis(_state(rows=0, cols=2), is_chart=True) == "ROWS"
    assert _formula_vc_axis({"Category": {"projections": [{}]}, "Y": {"projections": [{}, {}]}},
                            is_chart=True) == "ROWS"


def test_a_visual_with_no_grouping_at_all_keeps_todays_answer():
    """A value-only visual (a card, a single-cell table) has no axis to walk either way; it must not
    change behaviour just because the Rows role happens to be absent."""
    assert _formula_vc_axis({}, is_chart=False) == "ROWS"
    assert _formula_vc_axis({"Values": {"projections": [{}, {}]}}, is_chart=False) == "ROWS"


def test_an_empty_projection_list_counts_as_no_grouping():
    """A role present but EMPTY is not a grouping. Reading the key's presence rather than its length
    would call this a Rows grouping and leave the defect in place."""
    assert _formula_vc_axis({"Rows": {"projections": []},
                             "Columns": {"projections": [{}]}}, is_chart=False) == "COLUMNS"


def test_a_malformed_role_entry_does_not_raise():
    """The emitter must never crash a whole migration over a shape it did not expect."""
    assert _formula_vc_axis({"Rows": None, "Columns": None}, is_chart=False) == "ROWS"
    assert _formula_vc_axis({"Columns": {}}, is_chart=False) == "ROWS"


def test_the_call_site_passes_a_DERIVED_axis_not_a_literal():
    """Pin the call site, not just the helper.

    The helper can be perfect and unreached: the defect was a hardcoded ``axis="ROWS"`` argument,
    and a test of the helper alone would have passed against the broken engine. Brittle on purpose
    -- if this fails because the code moved, re-point it; do not delete it.
    """
    import inspect
    import twb_to_pbir

    src = inspect.getsource(twb_to_pbir._apply_formula_table_calc_chain)
    assert "_formula_vc_axis(" in src, "the chain entry point no longer derives an axis"
    assert 'axis="ROWS"' not in src, "a hardcoded ROWS axis came back at the chain entry point"

    one = inspect.getsource(twb_to_pbir._apply_one_formula_table_calc)
    assert "axis=axis" in one, "the derived axis is no longer forwarded to the compiler"
    assert 'axis="ROWS"' not in one.split("def ", 1)[-1].split("\n", 1)[-1] or \
        'compile_formula_chain(' in one, "call site shape changed -- re-read this pin"
