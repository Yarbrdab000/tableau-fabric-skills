"""Tests for ``_aggregate_predicate_flags``: the AGGREGATE parameter keep-filter recognizer.

The third sibling of the date-window and row-level-predicate flag pipelines, for the shape neither
can see -- a boolean whose predicate is an aggregate, e.g.

    ``IF SUM([Sales]) > [Parameters].[Sales Param] THEN TRUE ELSE FALSE END``

dropped on the filter shelf with ``member='true'``.

Ground truth: ``workbooks/0134_parameter_filters`` (corpus). Before this pipeline existed, that
workbook's ``sales Filter`` worksheet emitted a visual with NO ``filterConfig`` and no wrapper
measure -- an entirely unfiltered chart, warned but wrong.

Two properties matter more than the happy path and are pinned first:

* the binding carries **no** ``row_filter``, because the downstream row-predicate wrapper pass keys
  on exactly that. Wrapping an aggregate predicate into ``CALCULATE(<agg>, FILTER(t, <pred>))``
  evaluates it per ROW, which is a different question from the one Tableau asks;
* the pipeline is **disjoint** from the row-level one by construction, not by call ordering -- a
  calc the column translator can render as a boolean is refused here even when nothing skipped it.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from assemble_model import _aggregate_predicate_flags  # noqa: E402

# resolver: caption -> (table_display_name, clean_col, tmdl_type)
_FIELDS = {
    "Sales": ("Orders", "Sales", "decimal"),
    "Profit": ("Orders", "Profit", "decimal"),
    "Segment": ("Orders", "Segment", "string"),
    "Quantity": ("Orders", "Quantity", "int64"),
}
_KNOWN = {"Orders"}


def _resolve(caption):
    return _FIELDS.get(caption)


def _params(name):
    """param_resolver(name) -> the parameter's SELECTEDVALUE picker measure reference."""
    return {
        "Sales Param": "[Sales Param Value]",
        "Segment Param": "[Segment Param Value]",
        "Qty Param": "[Qty Param Value]",
    }.get(name)


def _calc(name, formula, internal=None):
    return {"name": name, "formula": formula, "internal_name": internal or "[%s]" % name}


AGG = _calc(
    "Sales Filter",
    "IF SUM([Sales]) > [Parameters].[Sales Param] THEN TRUE ELSE FALSE END",
    "[Calculation_agg01]",
)
ROW = _calc(
    "Segment Filter",
    "[Parameters].[Segment Param] = [Segment]",
    "[Calculation_row01]",
)


def _run(calcs, **kw):
    return _aggregate_predicate_flags(
        calcs, _resolve, _params, known_tables=_KNOWN, **kw)


def test_aggregate_parameter_boolean_becomes_a_keep_flag():
    flags, bindings = _run([AGG])
    assert len(flags) == 1, flags
    assert "Sales Filter" in bindings
    fm = flags[0]
    # 1 keep / BLANK drop -- the same contract the other two pipelines emit.
    assert fm["dax"].startswith("IF(")
    assert fm["dax"].endswith(", 1)")
    assert "SUM('Orders'[Sales])" in fm["dax"]
    assert "[Sales Param Value]" in fm["dax"]
    assert fm["source_calc_id"] == "Calculation_agg01"
    assert bindings["Sales Filter"]["predicate"] == {"op": "==", "value": 1}
    assert bindings["Sales Filter"]["model_table"] == "_Measures"


def test_binding_carries_no_row_filter_so_the_wrapper_pass_cannot_claim_it():
    """The load-bearing assertion.

    ``_apply_row_predicate_wrapped_measures`` rewrites a visual onto
    ``CALCULATE(<agg>, FILTER(<table>, <predicate>))`` for any binding that has a ``row_filter``.
    Doing that to an aggregate predicate silently changes the answer: "customers whose TOTAL sales
    clear the parameter" would become "rows whose OWN sales clear it". The absence of the key is what
    makes that impossible, so it is asserted directly rather than inferred from behaviour.
    """
    _flags, bindings = _run([AGG])
    spec = bindings["Sales Filter"]
    assert "row_filter" not in spec
    assert spec.get("aggregate") is True


def test_a_row_level_predicate_is_refused_even_when_nothing_skipped_it():
    """Disjointness by construction, not by ordering.

    The exemplar matters and was chosen by measurement, not by intuition. ``[Param] = [Segment]``
    looks like the obvious row-level case but proves NOTHING here: the measure translator refuses it
    outright (bare column, no aggregate), so the test passes with the guard deleted. The only input
    that can distinguish is one both translators type as ``bool`` -- a parameter-only comparison --
    and with the guard removed this pipeline does claim it. Verified by deleting the guard and
    watching this single assertion go red.
    """
    param_only = _calc("Qty Gate", "[Parameters].[Qty Param] > 5", "[Calculation_pg01]")
    flags, bindings = _run([param_only], skip_lower=set())
    assert flags == [], "a calc the COLUMN translator renders as bool belongs to the row-level pipeline"
    assert bindings == {}


def test_a_bare_column_predicate_is_refused_by_the_measure_gate():
    """The companion to the test above, kept separate BECAUSE it is the weaker one.

    ``[Param] = [Segment]`` is refused here, but by the ``dtype != "bool"`` measure gate rather than
    by the disjointness guard. Recording which gate does the work stops a future reader from citing
    this as evidence the guard is exercised -- it is not.
    """
    flags, bindings = _run([ROW], skip_lower=set())
    assert flags == []
    assert bindings == {}


def test_the_two_pipelines_partition_a_mixed_calc_set():
    flags, bindings = _run([ROW, AGG], skip_lower=set())
    assert [f["source_calc_name"] for f in flags] == ["Sales Filter"]
    assert set(bindings) == {"Sales Filter"}


def test_a_calc_without_a_parameter_is_ignored():
    plain = _calc("Big Sales", "IF SUM([Sales]) > 100 THEN TRUE ELSE FALSE END")
    assert _run([plain]) == ([], {})


def test_a_non_boolean_aggregate_calc_is_ignored():
    """Only a BOOLEAN qualifies -- a numeric aggregate over a parameter is an ordinary measure."""
    numeric = _calc("Scaled Sales", "SUM([Sales]) * [Parameters].[Qty Param]")
    assert _run([numeric]) == ([], {})


def test_an_already_consumed_calc_is_skipped_by_name_and_by_id():
    assert _run([AGG], skip_lower={"sales filter"}) == ([], {})
    assert _run([AGG], skip_lower={"calculation_agg01"}) == ([], {})


def test_the_measure_name_never_collides_with_the_translated_calc():
    """The calc itself is ALSO emitted as an ordinary measure, so the flag must take another name."""
    _flags, bindings = _run([AGG], reserved_names={"Sales Filter"})
    assert bindings["Sales Filter"]["measure_name"] == "Sales Filter Flag"


def test_without_a_param_resolver_the_pipeline_is_inert():
    """No parameters modelled -> nothing to read a slicer selection from, so emit nothing."""
    assert _aggregate_predicate_flags(
        [AGG], _resolve, None, known_tables=_KNOWN) == ([], {})


def test_an_untranslatable_calc_is_left_alone_rather_than_stubbed():
    weird = _calc("Odd", "IF SPATIAL_MAGIC([Sales]) > [Parameters].[Sales Param] THEN TRUE END")
    assert _run([weird]) == ([], {})
