"""A DISAGGREGATED measure pill must stay a bare column in a visual that lists every row (#142 follow-up).

2.348.0 fixed #142 by rebuilding a ``none:Sales:qk`` pill -- Tableau's *Analysis -> Aggregate
Measures OFF*, one mark per underlying row -- as ``Sum``, because a CHART's value role cannot hold a
bare column (``powerbi-report-author validate`` reports ``PBIR_ROLE_KIND_MISMATCH`` and exits 1).
That is correct for a chart and WRONG for a table, and 2.348.0 applied it to every visual type:

    workbook   0135_aggregation_types    worksheet "Disaggregated sales"    tableEx
    Tableau    draws every underlying row (~10k)
    2.348.0    Values: Aggregation "Sum of Sales"   -- one summed row per Sub-Category

The CHANGELOG rationale for 2.348.0 -- *"Power BI has no disaggregated value role"* -- is true of a
chart and false of a table. ``tableEx`` has no entry in the harvested ``MEASURE_ROLES`` at all, so a
bare column in its ``Values`` is valid AND is the faithful rebuild.

Reported by a parallel session from an isolated A/B against a REBUILT baseline, not by any gate
here: the model was valid, the report opened, and the visual rendered a full plausible table of
wrong numbers. Nothing static could see it -- the same shape as every defect in this area.

TWO FIXES THAT WERE CORRECT CODE AND DID NOTHING, both pinned below, because each ran on every
build and changed no artifact while reporting no failure:

  1. ``_measure_only_roles`` was called with the INTERNAL enum (``"table"``, ``"bar"``) against a
     catalog keyed by PBIR names (``tableEx``, ``clusteredBarChart``). Every lookup missed, so every
     visual type looked table-like, and the revert fired on a ``clusteredBarChart`` -- putting a
     bare ``Column`` back into role ``Y`` and REINTRODUCING #142 while fixing its follow-up.
     ``test_unknown_visual_type_is_left_alone`` is the control: the distinction between ``None``
     (unknown -- do not touch) and ``()`` (known, no measure-only role -- revert) is what makes this
     fail CLOSED, and returning ``()`` for both is exactly the bug.

  2. The revert then walked ``ws["rows"]`` and ``ws["cols"]``. A Tableau text table puts its measure
     on the **Text/Label** marks card, so the pill was never in either bucket.
     ``test_reverts_a_pill_on_the_text_shelf`` is that case; it fails against a rows/cols-only walk.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import twb_to_pbir as T  # noqa: E402


def _ws(visual_type, *, shelf="label", synthesized=True, authored=False):
    """A minimal worksheet IR carrying one measure pill on the requested shelf."""
    pill = {
        "caption": "Sales",
        "kind": "value",
        "binding": "aggregation",
        "aggregation": "Sum",
    }
    if synthesized:
        pill["synthesized_agg"] = True
    if authored:
        pill.pop("synthesized_agg", None)
    ws = {
        "visual_type": visual_type,
        "rows": [],
        "cols": [],
        "encodings": {"color": None, "label": None, "size": None, "detail": None},
    }
    if shelf in ("rows", "cols"):
        ws[shelf] = [pill]
    else:
        ws["encodings"][shelf] = pill
    return ws, pill


# --------------------------------------------------------- the catalog lookup, and its translation

def test_measure_only_roles_translates_the_internal_enum_to_the_pbir_name():
    """The catalog is keyed by PBIR names; the IR carries the internal enum.

    Skipping this translation is defect (1) in the module docstring: every lookup missed, so every
    type read as "no measure-only role" and the revert fired on charts.
    """
    assert T._measure_only_roles(T.VT_TABLE) == ()
    assert T._measure_only_roles(T.VT_MATRIX) == ("Values",)
    assert "Y" in T._measure_only_roles(T.VT_BAR)
    assert "Y" in T._measure_only_roles(T.VT_COLUMN)


def test_measure_only_roles_distinguishes_unknown_from_no_measure_role():
    """``None`` (unknown) and ``()`` (known, no measure-only role) drive OPPOSITE behaviour.

    Collapsing them to one falsy value is what made the first fix revert charts.
    """
    assert T._measure_only_roles("no-such-visual-type") is None
    assert T._measure_only_roles(T.VT_TABLE) == ()
    assert T._measure_only_roles("no-such-visual-type") != T._measure_only_roles(T.VT_TABLE)


def test_measure_only_roles_agrees_with_the_harvested_catalog():
    """Pins the SOURCE of the table, not a second copy of it (2.352.0's rule)."""
    from pbir_lint import MEASURE_ROLES

    for vt, pbir in T._VT_TO_PBIR.items():
        expected = tuple(MEASURE_ROLES.get(pbir) or ())
        assert T._measure_only_roles(vt) == expected, vt


def test_the_enums_absent_from_the_pbir_map_are_exactly_the_two_that_need_no_translation():
    """Some internal enum values ARE already PBIR names, so they are absent from ``_VT_TO_PBIR``.

    That absence makes ``_measure_only_roles`` return ``None`` -- "unknown, do not touch" -- for a
    type that is in fact perfectly well known. Here that lands on the right answer for both, but by
    the fail-CLOSED default rather than by consulting the catalog, so it is pinned rather than left
    to luck:

        card         MEASURE_ROLES -> ('Values',)   measure-only, so NOT reverting is correct
        unsupported  in neither catalog             nothing to reason about, so leave it alone

    If a THIRD unmapped enum ever appears and its type has no measure-only role, it would silently
    fail to revert -- the same never-emitted-object blindness this whole area keeps producing. This
    test fails the day that set changes, which is the only cheap way to notice.
    """
    unmapped = sorted(v for n, v in vars(T).items()
                      if n.startswith("VT_") and isinstance(v, str) and v not in T._VT_TO_PBIR)
    assert unmapped == ["card", "unsupported"], (
        "the set of internal enums absent from _VT_TO_PBIR changed to %r; a new entry that has no "
        "measure-only role would silently never be reverted" % (unmapped,))


def test_a_card_never_has_its_aggregation_reverted():
    """``card``'s ``Values`` IS measure-only, so a bare column there would be the #142 defect.

    Asserted directly on behaviour rather than inferred from the map, because the map is exactly
    what does not contain it.
    """
    ws, pill = _ws(T.VT_CARD)
    T._unsynthesize_table_aggregations(ws)
    assert pill["binding"] == "aggregation"
    assert pill["aggregation"] == "Sum"


# --------------------------------------------------------------------------- the revert itself

def test_reverts_a_synthesised_sum_on_a_table():
    ws, pill = _ws(T.VT_TABLE)
    T._unsynthesize_table_aggregations(ws)
    assert pill["binding"] == "column"
    assert pill["aggregation"] is None
    assert "synthesized_agg" not in pill


def test_reverts_a_pill_on_the_text_shelf():
    """Defect (2): a Tableau text table carries its measure on Text/Label, not Rows or Columns.

    This is the actual 0135 shape. A rows/cols-only walk leaves it untouched and reports nothing.
    """
    ws, pill = _ws(T.VT_TABLE, shelf="label")
    assert not ws["rows"] and not ws["cols"], "fixture must exercise the encodings path"
    T._unsynthesize_table_aggregations(ws)
    assert pill["binding"] == "column"


def test_reverts_a_pill_on_rows_too():
    ws, pill = _ws(T.VT_TABLE, shelf="rows")
    T._unsynthesize_table_aggregations(ws)
    assert pill["binding"] == "column"


# ------------------------------------------------------------------- and the things it must NOT do

def test_a_matrix_keeps_the_synthesised_sum():
    """``pivotTable``'s ``Values`` IS measure-only -- #142's sibling case."""
    ws, pill = _ws(T.VT_MATRIX)
    T._unsynthesize_table_aggregations(ws)
    assert pill["binding"] == "aggregation"
    assert pill["aggregation"] == "Sum"


def test_a_bar_chart_keeps_the_synthesised_sum():
    """#142 itself. This is the assertion the first, broken fix failed."""
    ws, pill = _ws(T.VT_BAR)
    T._unsynthesize_table_aggregations(ws)
    assert pill["binding"] == "aggregation"
    assert pill["aggregation"] == "Sum"


def test_unknown_visual_type_is_left_alone():
    """Fail CLOSED. An unmapped type must never be treated as table-like."""
    ws, pill = _ws("no-such-visual-type")
    T._unsynthesize_table_aggregations(ws)
    assert pill["binding"] == "aggregation"


def test_an_authored_aggregation_is_never_touched():
    """Only an aggregation WE synthesised may be reverted.

    Without the ``synthesized_agg`` mark this would strip a real Tableau ``SUM([Sales])`` off every
    text table in the estate -- a worse defect than the one being fixed, in the other direction.
    """
    ws, pill = _ws(T.VT_TABLE, synthesized=False, authored=True)
    T._unsynthesize_table_aggregations(ws)
    assert pill["binding"] == "aggregation"
    assert pill["aggregation"] == "Sum"


# ------------------------------------------------------- that it is CALLED, and with the right mark

def test_the_qk_branch_marks_the_aggregation_as_ours():
    """The revert is unreachable unless the synthesis site sets the mark.

    A text pin: brittle by design. If this fails because the branch moved, re-point it -- do not
    delete it, because without the mark every test above passes while nothing is ever reverted.
    """
    src = inspect.getsource(T)
    assert 'field["synthesized_agg"] = True' in src, (
        "the qk synthesis site no longer marks the aggregation as ours; "
        "_unsynthesize_table_aggregations can never fire")


def test_build_query_state_calls_the_revert():
    """That the gate is CALLED, not merely that it works.

    Pins the ARGUMENT as well as the call: passing anything other than the worksheet being built
    would run clean and revert nothing.
    """
    src = inspect.getsource(T._build_query_state)
    assert "_unsynthesize_table_aggregations(ws)" in src, (
        "_build_query_state no longer invokes the revert on its own worksheet")
