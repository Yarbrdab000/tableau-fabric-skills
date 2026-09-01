"""An unaggregated measure pill must never emit a bare ``Column`` into a Measure-only role (#142).

``powerbi-report-author validate`` reports ``PBIR_ROLE_KIND_MISMATCH`` and exits 1 on that shape, so
the report does not import. Ground truth is corpus workbook ``0135_aggregation_types`` -- a workbook
whose whole subject is aggregation types -- whose ``Bar chart Example`` shelf carries the SAME field
three times at three aggregations:

    sum:Sales:qk    -> Aggregation Sum       already correct
    attr:Sales:qk   -> Aggregation Min       correct since 2.229.0 (see test_attr_derivation)
    none:Sales:qk   -> bare Column           THE DEFECT

Measured before/after with the real Microsoft validator on the pristine ``reports/`` tree:

    baseline : 1 error(s)  PBIR_ROLE_KIND_MISMATCH   result=failed
    fixed    : 0 error(s)                            result=succeeded

WHY THE PILL ROLE CODE IS THE ONLY USABLE DISCRIMINATOR. Tableau writes ``derivation='None'`` on a
measure in two unrelated situations, and the base ``<column>`` declaration is byte-identical for
both -- ``role='measure' type='quantitative'``. Only the pill INSTANCE separates them, via the
trailing role code that Tableau stamps on it:

  * ``:qk`` continuous (green) -- Analysis -> Aggregate Measures OFF, i.e. a DISAGGREGATED measure,
    one mark per underlying row. Power BI has no disaggregated value role, so it is summed and
    warned.
  * ``:ok`` / ``:nk`` discrete (blue) -- a measure dragged onto a shelf AS A DIMENSION. It groups;
    no aggregate is correct, and wrapping it in one would invent an aggregation the author never
    asked for. This is the shape reported upstream (a ``pivotTable`` whose source held the pill on
    ``Rows`` and whose rebuild put it in ``Values``).

THE THIRD CASE IS THE ONE WITH TEETH: a pill with NO role code must keep its previous behaviour.
``_is_continuous_pill`` answers a boolean and reads any unknown pill as discrete -- correct for
choosing a colour ramp, and wrong here, because reusing it would silently reclassify every
caption-fallback measure in the estate as a grouping field. That is why ``_pill_role_code`` exists
separately and returns ``None`` rather than a default.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import twb_to_pbir as T  # noqa: E402


# --------------------------------------------------------------------------- the reader

def test_pill_role_code_reads_tableaus_three_codes():
    assert T._pill_role_code({"instance": "none:Sales:qk"}) == "qk"
    assert T._pill_role_code({"instance": "none:Pay Amount:ok"}) == "ok"
    assert T._pill_role_code({"instance": "none:Region:nk"}) == "nk"


def test_pill_role_code_returns_none_for_anything_unrecognised():
    """The load-bearing half. ``None`` must be distinguishable from "discrete".

    A synthesized caption-fallback instance carries no role code at all. If this returned a default
    -- or if the caller reused ``_is_continuous_pill``, which reads unknown as discrete -- every
    caption-fallback measure pill in the estate would be reclassified as a grouping field. That
    population is 29 visuals across 2 workbooks in the current corpus, none of which is a #142
    defect.
    """
    for unknown in ("Sales", "", None, "none:Sales", "none:Sales:zz", "[none:Sales:qk]"):
        assert T._pill_role_code({"instance": unknown}) is None, unknown
    assert T._pill_role_code({}) is None


def test_pill_role_code_agrees_with_is_continuous_pill_on_qk():
    """Pins the relationship between the two readers so a later edit cannot drift them apart."""
    for inst in ("none:Sales:qk", "sum:Sales:qk", "none:Pay Amount:ok", "Sales", ""):
        field = {"instance": inst}
        assert T._is_continuous_pill(field) == (T._pill_role_code(field) == "qk"), inst


# --------------------------------------------------------------------------- the branch

def _measure_branch():
    src = inspect.getsource(T)
    i = src.index('    if role == "measure" and not for_filter:\n'
                  '        # An UNAGGREGATED measure pill (#142).')
    return src[i:i + 4000]


def test_a_filter_pill_is_never_rewrapped():
    """A filter on an unaggregated measure filters ROWS, not an aggregate.

    ``Sales BETWEEN 10 AND 500`` selects rows whose Sales is in range; it does not compare a SUM to
    that range. ``for_filter`` marks a pill destined for a filter card, and the branch must decline
    it -- otherwise every quantitative filter on a raw measure in the estate changes meaning.
    Caught by the pre-existing ``test_numeric_range_selection_emits_advanced_comparison_filter``,
    which went from green to IndexError on the first version of this change.
    """
    src = inspect.getsource(T)
    assert '    if role == "measure" and not for_filter:' in src
    block = _measure_branch()
    assert "for_filter" in block
    assert "filters ROWS" in block


def test_continuous_disaggregated_measure_is_summed_and_warned():
    block = _measure_branch()
    assert 'if code == "qk":' in block
    assert 'field["aggregation"] = "Sum"' in block
    assert 'field["binding"] = "aggregation"' in block
    # Warned, never silent: the rebuild is NOT what Tableau draws, and a reader comparing the two
    # side by side has to be able to find out why the marks collapsed.
    assert "warnings.append" in block
    assert "disaggregated measure" in block


def test_discrete_measure_pill_groups_rather_than_being_wrapped():
    block = _measure_branch()
    assert 'if code in ("ok", "nk"):' in block
    i = block.index('if code in ("ok", "nk"):')
    tail = block[i:i + 700]
    assert 'field["kind"] = "category"' in tail
    # Wrapping it would invent an aggregate the author never asked for -- the upstream report's
    # whole point. Assert the discrete branch does NOT reach for an aggregation.
    assert 'field["aggregation"]' not in tail


def test_a_pill_with_no_role_code_keeps_the_previous_behaviour():
    """The fail-safe, and the reason this shipped narrow.

    Neither branch may fire on an unrecognised pill: it must fall through to the pre-existing
    ``kind = "value" if role == "measure"`` line. Asserted structurally because the alternative --
    ``else:`` reclassifying everything that is not ``qk`` -- is the tempting simplification and
    would change 29 corpus visuals that have no defect.
    """
    block = _measure_branch()
    assert "else:" not in block.split('if code in ("ok", "nk"):')[1][:400]
    src = inspect.getsource(T)
    src = inspect.getsource(T)
    i = src.index('    if role == "measure" and not for_filter:\n'
                  '        # An UNAGGREGATED measure pill (#142).')
    after = src[i:i + 4800]
    assert '# plain field: role decides axis vs value placement.' in after
    assert 'field["kind"] = "value" if role == "measure" else "category"' in after


def test_a_non_numeric_measure_is_not_summed():
    """``Sum`` on a non-numeric column is invalid DAX. The existing ``_AGG_FUNC`` branch guards this
    for declared aggregations; the new branch must not bypass that guard."""
    block = _measure_branch()
    i = block.index('if code == "qk":')
    qk = block[i:i + 1400]
    assert "datatype in _NUMERIC_TYPES" in qk
    assert "string" not in T._NUMERIC_TYPES


def test_the_measure_branch_runs_before_the_unsupported_catch_all():
    """Ordering guard, matching the ATTR one. The catch-all above returns ``None`` (drops the pill);
    if the ordering ever inverted, the new branch would be present and dead while the defect
    returned."""
    src = inspect.getsource(T)
    assert src.index("unsupported derivation") < src.index(
        '    if role == "measure" and not for_filter:\n'
        '        # An UNAGGREGATED measure pill (#142).')
    assert src.index(
        '    if role == "measure" and not for_filter:\n'
        '        # An UNAGGREGATED measure pill (#142).') < src.index(
        "# plain field: role decides axis vs value placement.")


def test_dimension_pills_are_untouched_by_this_change():
    """Blast-radius pin. The branch is gated on ``role == "measure"``; a dimension pill with
    ``derivation='None'`` is the ordinary case and by far the commonest pill in any workbook."""
    block = _measure_branch()
    assert block.startswith('    if role == "measure" and not for_filter:')
