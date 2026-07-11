"""Emit Power BI Visual-Calculation DAX from a :class:`VisualCalcSpec`.

The view-layer counterpart to :mod:`calc_to_dax`: where that module compiles a Tableau formula into
a **model measure** with explicit ``WINDOW`` / ``OFFSET`` / ``RANKX`` addressing, this one renders a
normalized :class:`~visual_calc_spec.VisualCalcSpec` into a **Visual Calculation** -- report-layer
DAX that runs over the visual's own result matrix along an axis (``ROWS`` / ``COLUMNS``) with a
partition/reset scope. The functions used (``RUNNINGSUM`` / ``MOVINGAVERAGE`` / ``PREVIOUS`` /
``FIRST`` / ``ROWNUMBER`` / ``RANK`` / ``COLLAPSEALL``) exist only in the visual-calculation dialect
and take the axis by name; they are *not* the model-measure window functions.

Faithfulness rules enforced here (grounding doc section 4, the false-friend catalog):
  * a Tableau ``LOOKUP(x, -1)`` one step back becomes ``PREVIOUS(x, axis)`` -- and the arity rule
    ``PREVIOUS(x, axis)`` for one step vs ``PREVIOUS(x, k, axis)`` for k steps is applied exactly so
    the axis is never mistaken for the offset;
  * a running position uses ``ROWNUMBER`` (Tableau ``INDEX``), never PBI ``INDEX``;
  * a two-pass Tableau calc (e.g. YTD then its year-over-year growth) becomes a **chain**: the inner
    calc is emitted first as a hidden Visual Calculation, and the outer references it *by name*, so
    the numbers compose exactly as Tableau evaluates them pass-over-pass.

The base measure name and (for a rank partition) the resolved partition column are passed in already
resolved to their Power BI names -- this module stays pure string assembly so it is fully unit
testable and independent of the model build. Output is a list of :class:`VisualCalcDef` (one per
Visual Calculation, inner-before-outer) plus a review reason when a required input is missing.

Grounded and validated cell-for-cell against the paired Power BI replica; original work (CLEANROOM).
Stdlib-only, deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from visual_calc_spec import (
    VisualCalcSpec,
    FAMILY_RUNNING_TOTAL,
    FAMILY_YTD,
    FAMILY_YTD_GROWTH,
    FAMILY_MOVING_AVERAGE,
    FAMILY_PERCENTILE,
    FAMILY_COMPOUND_GROWTH,
    FAMILY_PERCENT_DIFFERENCE,
    FAMILY_PERCENT_OF_TOTAL,
    FAMILY_YEAR_OVER_YEAR,
    FAMILY_DIFFERENCE,
    FAMILY_RANK,
)

# Deterministic display / DAX-reference name per family. The name is also the ``nativeQueryRef`` a
# chain's outer calc uses to reference its inner calc, so it must be stable and self-consistent.
_FAMILY_NAME = {
    FAMILY_RUNNING_TOTAL: "Running Total",
    FAMILY_YTD: "YTD",
    FAMILY_YTD_GROWTH: "YTD Growth",
    FAMILY_MOVING_AVERAGE: "Moving Average",
    FAMILY_PERCENTILE: "Percentile",
    FAMILY_COMPOUND_GROWTH: "Compound Growth",
    FAMILY_PERCENT_DIFFERENCE: "Percent Difference",
    FAMILY_PERCENT_OF_TOTAL: "Percent of Total",
    FAMILY_YEAR_OVER_YEAR: "Year over Year",
    FAMILY_DIFFERENCE: "Difference",
    FAMILY_RANK: "Rank",
}

# Families whose DAX yields a RATIO (0..1-scaled), which Tableau's quick table calc shows as a
# percentage. The visible projection therefore carries a percent display format ``0.00%`` -- the
# faithful counterpart of Tableau's automatic percent display for these calcs. The absolute families
# (running total, YTD, moving average, plain difference) keep the default/base format -- no override,
# which also matches the hand-built oracle (it leaves those unformatted). The number format is a
# per-projection ``format`` string, the PBIR ``RoleProjection.format`` seam ("format string scoped to
# the visual"); it is emitted only for the VISIBLE calc, never a hidden colour-driver.
_PERCENT_FAMILIES = frozenset({
    FAMILY_PERCENT_OF_TOTAL,
    FAMILY_PERCENT_DIFFERENCE,
    FAMILY_YEAR_OVER_YEAR,
    FAMILY_YTD_GROWTH,
    FAMILY_COMPOUND_GROWTH,
    FAMILY_PERCENTILE,
})
_PERCENT_FORMAT = "0.00%"


def _family_number_format(family: str) -> Optional[str]:
    """The visual-scoped display format for a family, or ``None`` to inherit the default/base format."""
    return _PERCENT_FORMAT if family in _PERCENT_FAMILIES else None


@dataclass
class VisualCalcDef:
    """One emitted Visual Calculation: a name + DAX + its visibility, ready for the visual.json wiring."""
    name: str
    expression: str
    hidden: bool
    is_inner: bool = False
    role: str = "value"
    family: str = ""
    tableau_summary: str = ""
    number_format: Optional[str] = None   # per-projection ``format`` string (percent families) or None


def _ref(name: str) -> str:
    return f"[{name}]"


def _previous(ref: str, k: int, axis: str) -> str:
    """``PREVIOUS(ref, axis)`` for a single step; ``PREVIOUS(ref, k, axis)`` for k steps."""
    return f"PREVIOUS({ref}, {axis})" if k == 1 else f"PREVIOUS({ref}, {k}, {axis})"


def _body(spec: VisualCalcSpec, base_ref: str, partition_column: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """DAX body for a single (non-chain) family, or ``(None, reason)`` when an input is missing."""
    m = base_ref
    axis = spec.axis

    if spec.family in (FAMILY_RUNNING_TOTAL, FAMILY_YTD):
        if spec.reset:
            return f"RUNNINGSUM({m}, {axis}, {spec.reset})", None
        return f"RUNNINGSUM({m}, {axis})", None

    if spec.family == FAMILY_MOVING_AVERAGE:
        if not spec.window_size:
            return None, "moving average has no window size"
        if spec.reset:
            # The two empty positional args reach the reset parameter (includePartials, axis, .., reset).
            return f"MOVINGAVERAGE({m}, {spec.window_size}, TRUE, {axis}, , , {spec.reset})", None
        return f"MOVINGAVERAGE({m}, {spec.window_size}, TRUE, {axis})", None

    if spec.family == FAMILY_PERCENTILE:
        if not partition_column:
            return None, "percentile needs a resolved partition column but none was provided"
        p = _ref(partition_column)
        return (
            f"VAR RankAsc = RANK(DENSE, ORDERBY({m}, ASC), PARTITIONBY({p}))\n"
            f"VAR RankDesc = RANK(DENSE, ORDERBY({m}, DESC), PARTITIONBY({p}))\n"
            f"VAR N = RankAsc + RankDesc - 1\n"
            f"RETURN DIVIDE(RankAsc - 1, N - 1)"
        ), None

    if spec.family == FAMILY_RANK:
        # Faithful RANK: the tie rule + direction come from the parsed rank-options (default SKIP =
        # Tableau Competition, DESC = Tableau descending). A pane-scoped rank restarts per partition
        # column; a whole-table rank (the common CustomerRank case) has NO partition -- partitioning
        # by the sole category would make every partition size 1 and every rank 1. So PARTITIONBY is
        # emitted only when the spec pinned a pane partition AND its column resolved (else fail closed).
        ties = spec.rank_ties or "SKIP"
        direction = spec.rank_direction or "DESC"
        order = f"ORDERBY({m}, {direction})"
        if spec.partition_pill:
            if not partition_column:
                return None, "rank pane partition was detected but no resolved partition column was provided"
            return f"RANK({ties}, {order}, PARTITIONBY({_ref(partition_column)}))", None
        return f"RANK({ties}, {order})", None

    if spec.family == FAMILY_COMPOUND_GROWTH:
        return (
            f"VAR FirstVal = FIRST({m}, {axis})\n"
            f"VAR Periods = ROWNUMBER({axis}) - 1\n"
            f"VAR Exponent = DIVIDE(1, Periods, 0)\n"
            f"RETURN POWER(DIVIDE({m}, FirstVal), Exponent) - 1"
        ), None

    if spec.family == FAMILY_PERCENT_DIFFERENCE:
        prev = _previous(m, spec.offset_k, axis)
        return f"DIVIDE({m} - {prev}, {prev})", None

    if spec.family == FAMILY_PERCENT_OF_TOTAL:
        if not spec.collapse_scope:
            return None, "percent of total has no collapse scope"
        # COLLAPSEALL flattens to the grand total (all addressed); COLLAPSE flattens one level to a
        # partition subtotal (a partition dimension is retained). The spec's ``collapse_all`` carries
        # which, derived from the addressed/partition split -- never guessed here.
        fn = "COLLAPSEALL" if spec.collapse_all else "COLLAPSE"
        return f"DIVIDE({m}, {fn}({m}, {spec.collapse_scope}))", None

    if spec.family == FAMILY_YEAR_OVER_YEAR:
        prev = _previous(m, spec.offset_k, axis)
        return (
            f"VAR PriorYear = {prev}\n"
            f"RETURN DIVIDE({m} - PriorYear, ABS(PriorYear))"
        ), None

    if spec.family == FAMILY_DIFFERENCE:
        prev = _previous(m, spec.offset_k, axis)
        return f"{m} - {prev}", None

    return None, f"no visual-calculation template for family {spec.family!r}"


def emit_visual_calc(
    spec: VisualCalcSpec,
    *,
    base_measure: str = "Count Orders",
    partition_column: Optional[str] = None,
) -> Tuple[Optional[List[VisualCalcDef]], Optional[str]]:
    """Render ``spec`` into one or more :class:`VisualCalcDef` (inner-before-outer), or ``(None, reason)``.

    ``base_measure`` is the resolved Power BI measure the calc runs over (default the implicit-count
    ``Count Orders``); ``partition_column`` is the resolved column for a rank partition. Visibility
    follows the worksheet role: for a plain table (``role="value"``) the calc is shown; for a
    conditionally-formatted table (``role="color"``) the calc is hidden and drives the fill. A
    chain's inner calc is always hidden.
    """
    base_ref = _ref(base_measure)
    role = spec.role or "value"
    main_hidden = (role == "color")

    if spec.family == FAMILY_YTD_GROWTH:
        inner_spec = spec.chain_inner
        if inner_spec is None:
            return None, "YTD growth chain is missing its inner YTD calc"
        inner_body, reason = _body(inner_spec, base_ref, partition_column)
        if inner_body is None:
            return None, reason
        inner_name = _FAMILY_NAME.get(inner_spec.family, inner_spec.family)
        inner = VisualCalcDef(
            name=inner_name, expression=inner_body, hidden=True, is_inner=True,
            role=role, family=inner_spec.family, tableau_summary=spec.tableau_summary,
            number_format=_family_number_format(inner_spec.family),
        )
        inner_ref = _ref(inner_name)
        prev = _previous(inner_ref, spec.offset_k, spec.axis)
        outer_body = (
            f"VAR PriorYear = {prev}\n"
            f"RETURN DIVIDE({inner_ref} - PriorYear, ABS(PriorYear))"
        )
        outer = VisualCalcDef(
            name=_FAMILY_NAME[FAMILY_YTD_GROWTH], expression=outer_body, hidden=main_hidden,
            role=role, family=FAMILY_YTD_GROWTH, tableau_summary=spec.tableau_summary,
            number_format=_family_number_format(FAMILY_YTD_GROWTH),
        )
        return [inner, outer], None

    body, reason = _body(spec, base_ref, partition_column)
    if body is None:
        return None, reason
    name = _FAMILY_NAME.get(spec.family, spec.family)
    return [VisualCalcDef(
        name=name, expression=body, hidden=main_hidden, role=role,
        family=spec.family, tableau_summary=spec.tableau_summary,
        number_format=_family_number_format(spec.family),
    )], None
