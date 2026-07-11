"""View-layer normalizer: a recovered Quick Table Calc usage -> a :class:`VisualCalcSpec`.

A Tableau *quick table calc* (applied on a pill via the pill menu -- ``cum:`` / ``pcdf:`` /
``pcto:`` / ``win:`` / ``pcrk:`` ...) is a **report/view-layer** transform: it re-shapes the
worksheet's own result matrix along an axis, with a restart (reset) scope. It has **no model
equivalent** -- forcing it into an explicit-addressing model measure is exactly what the measure
path (:mod:`table_calc_to_dax`) deliberately *hands off* for the scope-relative tokens (``Table`` /
``Pane`` / ``Cell`` / ``Columns`` and the compound ones), because a model measure must pin
PARTITIONBY + ORDERBY absolutely and those tokens don't fix the across/down direction from the
workbook alone.

A Power BI **Visual Calculation** is the structurally identical home: it, too, is stored in the
visual and evaluated over the visual's own matrix along an axis (``ROWS`` / ``COLUMNS``) with a
partition/reset scope. Because a Visual Calculation is *matrix-relative* -- it reads its axis from
the visual's own shelves rather than needing an absolute partition -- this normalizer can faithfully
resolve the very tokens the measure path must refuse. That is the "better addressing logic" of the
view-layer target: same information, a target that can use it.

This module turns the recovered facts (:class:`workbook_table_calcs.TableCalcUsage`) into a small,
explicit :class:`VisualCalcSpec` intermediate representation (the grounding doc's IR, section 2):
family + axis + reset + grain/offset + scope + role, plus the stacked chain for a two-pass calc. It
**never guesses**: an axis, calendar ratio, or partition it cannot pin from the workbook facts
becomes a structured *review* reason (returned instead of a spec), mirroring the faithful-or-stub
discipline of the measure path. The :mod:`visual_calc_emitter` consumes the spec to produce DAX.

Grounded in the paired ground-truth corpus (Tableau quick calcs <-> a hand-built Power BI
Visual-Calculation replica); stdlib-only, offline, deterministic. Original work -- see CLEANROOM.md.
The ``usage`` argument is duck-typed (any object with the :class:`TableCalcUsage` attributes).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple, Callable

from workbook_table_calcs import AGG_DERIVATIONS

# -- families (the view-layer intent an emitter knows how to render) -----------
FAMILY_RUNNING_TOTAL = "RUNNING_TOTAL"
FAMILY_YTD = "YTD"
FAMILY_YTD_GROWTH = "YTD_GROWTH"          # chain: YTD (inner) then a year-over-year growth
FAMILY_MOVING_AVERAGE = "MOVING_AVERAGE"
FAMILY_PERCENTILE = "PERCENTILE"
FAMILY_COMPOUND_GROWTH = "COMPOUND_GROWTH"
FAMILY_PERCENT_DIFFERENCE = "PERCENT_DIFFERENCE"
FAMILY_PERCENT_OF_TOTAL = "PERCENT_OF_TOTAL"
FAMILY_YEAR_OVER_YEAR = "YEAR_OVER_YEAR"
FAMILY_DIFFERENCE = "DIFFERENCE"
FAMILY_RANK = "RANK"

# -- rank tie-mode + direction vocabulary --------------------------------------
# Tableau's ``rank-options`` is "<TieMode>,<Direction>" (e.g. "Competition,Descending"). Map the tie
# mode onto the Power BI visual-calculation ``RANK`` ties argument -- but ONLY the two that have a
# faithful native equivalent: Tableau Competition (1,2,2,4) == PBI SKIP; Tableau Dense (1,2,2,3) ==
# PBI DENSE. Tableau Modified (1,3,3,4) and Unique (1,2,3,4) have no faithful native RANK tie rule,
# so they fail closed to review (never a wrong ranking) -- the same posture the measure path takes
# for RANK_MODIFIED / RANK_UNIQUE.
_RANK_TIES = {"Competition": "SKIP", "Dense": "DENSE"}
_RANK_DIRECTION = {"Descending": "DESC", "Ascending": "ASC"}
_RANK_TIES_REVIEW = frozenset({"Modified", "Unique"})

# -- calendar grain vocabulary -------------------------------------------------
# Tableau encodes a date grain both as an instance-token prefix (inside a level-break /
# level-address reference, e.g. "[...].[qr:Order Date:ok]") and as a pill ``derivation`` on the
# shelf ("Year" / "Quarter" / "Month-Trunc"). Map both to a common unit so an above-leaf offset
# (Year-over-Year over a Quarter leaf = 4 periods back) is a deterministic calendar ratio, never a
# guess. Only *clean integer* ratios are honored; anything else fails closed to review.
_GRAIN_TOKEN_UNIT = {
    "yr": "year", "tyr": "year",
    "qr": "quarter", "tqr": "quarter",
    "mn": "month", "tmn": "month", "mo": "month",
    "wk": "week", "twk": "week",
    "dy": "day", "tdy": "day",
}
_DERIVATION_UNIT = {
    "Year": "year",
    "Quarter": "quarter",
    "Month": "month", "Month-Trunc": "month",
    "Week": "week", "Week-Trunc": "week",
    "Day": "day", "Day-Trunc": "day",
}
_DATE_DERIVATIONS = frozenset(_DERIVATION_UNIT)
# (parent unit, leaf unit) -> whole leaf periods in one parent. Deliberately conservative: week/day
# ratios to a month/year are non-constant, so they are absent -> review rather than a wrong offset.
_CALENDAR_RATIO = {
    ("year", "year"): 1, ("quarter", "quarter"): 1, ("month", "month"): 1,
    ("week", "week"): 1, ("day", "day"): 1,
    ("year", "quarter"): 4,
    ("year", "month"): 12,
    ("quarter", "month"): 3,
}

# Ordering-type tokens that carry a *pane* (per-parent restart) rather than the whole table.
_PANE_TOKENS = frozenset({"Pane", "RowInPane", "ColumnInPane", "PaneCol", "CellInPane"})


@dataclass
class VisualCalcSpec:
    """The view-layer intent of one quick table calc, ready for :mod:`visual_calc_emitter`.

    Abstract on purpose: it names *what* the calc does over the matrix (family + axis + reset +
    grain + scope), not the concrete Power BI column names. The wiring layer resolves the base
    measure and any partition pill to their model names before emission, so this stays workbook-only
    and unit-testable.
    """
    family: str
    axis: str                                  # "COLUMNS" | "ROWS"
    role: str = "value"                        # "value" (the calc is shown) | "color" (drives fill)
    reset: Optional[str] = None                # "HIGHESTPARENT" for a pane/level-break restart
    offset_k: int = 1                          # periods back for a PREVIOUS()-style offset
    collapse_scope: Optional[str] = None       # e.g. "ROWS COLUMNS" for percent-of-total
    collapse_all: bool = True                  # COLLAPSEALL (grand total) vs COLLAPSE (partition subtotal)
    window_size: Optional[int] = None          # moving-window width
    window_agg: Optional[str] = None           # window aggregation ("Avg")
    partition_pill: Optional[str] = None       # base field of a rank partition (e.g. "Order Date")
    partition_grain: Optional[str] = None      # its grain derivation (e.g. "Year")
    rank_ties: Optional[str] = None            # RANK ties arg: "SKIP" (competition) | "DENSE"
    rank_direction: Optional[str] = None       # ORDERBY direction: "DESC" (Tableau default) | "ASC"
    chain_inner: Optional["VisualCalcSpec"] = None   # first-computed calc a chain references
    # -- provenance (mirrors the TableauFormula / TranslatedBy annotation discipline) --
    tableau_calc_type: str = ""
    tableau_instance: str = ""
    source_worksheet: str = ""
    tableau_summary: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["chain_inner"] = self.chain_inner.to_dict() if self.chain_inner else None
        return d


# -- small workbook-fact helpers ----------------------------------------------
def _is_temporal_pill(pill) -> bool:
    return getattr(pill, "derivation", None) in _DATE_DERIVATIONS


def _grain_token(level_ref: Optional[str]) -> Optional[str]:
    """Grain prefix of a level reference, e.g. ``[...].[qr:Order Date:ok]`` -> ``"qr"``."""
    if not level_ref:
        return None
    segments = re.findall(r"\[([^\[\]]+)\]", level_ref)
    if not segments:
        return None
    seg = segments[-1]
    if ":" in seg:
        return seg.split(":", 1)[0].strip().lower()
    return None


def _shelf_for(usage, axis):
    return list(usage.cols) if axis == "COLUMNS" else list(usage.rows)


def _is_dim(pill) -> bool:
    """True iff a shelf pill is a real partition dimension (not an aggregated measure / user LOD).

    Prefers the pill's own :attr:`Pill.is_dimension` (the shared classifier that lives next to the
    ``Pill``), falling back to the shared :data:`AGG_DERIVATIONS` set for a duck-typed pill so this
    module stays honest even when handed a lightweight stub."""
    flag = getattr(pill, "is_dimension", None)
    if flag is not None:
        return bool(flag)
    deriv = getattr(pill, "derivation", None)
    return deriv not in AGG_DERIVATIONS and deriv != "User"


def _dim_names(pills) -> List[str]:
    """Tableau column names of the real dimension pills on a shelf, order preserved."""
    return [getattr(p, "column", None) for p in pills if _is_dim(p)]


def resolve_addressing(usage) -> Tuple[Optional[List[str]], Optional[List[str]], Optional[str]]:
    """The ONE view-layer shelf decomposition every Visual-Calculation consumer reads from.

    Splits the visual's dimension pills into **ADDRESSED** (the calc runs / sums / offsets ALONG
    these) and **PARTITION** (it restarts / subtotals WITHIN these), from the Tableau ordering token
    plus the shelf layout. This is the same decomposition the measure path's ``_addressing_for``
    performs -- it agrees on the shared tokens (``Rows`` -> addressed on Cols; ``Field`` -> the
    ordering fields) -- and additionally resolves ``Columns`` / ``Table`` (which a *model measure*
    must hand off, because it cannot pin their across/down direction, but a *matrix-relative* Visual
    Calculation can). Returns ``(addressed_columns, partition_columns, reason)``; a compound / pane
    token whose direction is not fixed by the shelves alone fails closed to ``(None, None, reason)``
    rather than guess a denominator.
    """
    rows = _dim_names(usage.rows)
    cols = _dim_names(usage.cols)
    ot = getattr(usage, "ordering_type", None) or "Table"
    if ot == "Rows":
        return cols, rows, None                    # across the Cols shelf, restart each Rows row
    if ot == "Columns":
        return rows, cols, None                    # down the Rows shelf, restart each Cols column
    if ot == "Table":
        return rows + cols, [], None               # the whole table, no restart
    if ot == "Field":
        of = list(getattr(usage, "ordering_fields", None) or [])
        addressed = [c for c in (rows + cols) if c in of]
        partition = [c for c in (rows + cols) if c not in of]
        if not addressed:
            return None, None, "field-scope ordering names no dimension present on the shelves"
        return addressed, partition, None
    return None, None, (f"ordering scope {ot!r} does not decompose into addressed/partition "
                        "dimensions from the shelf layout alone")


def _derive_axis(usage, is_temporal) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the Visual-Calculation axis (``COLUMNS`` / ``ROWS``) from the ordering token + shelves.

    The token is *not* the axis. ``Rows`` scope runs **across** the Cols shelf (restarting each
    Rows-shelf row) -> axis ``COLUMNS``; ``Columns`` scope runs **down** the Rows shelf -> axis
    ``ROWS`` (verified against the corpus' "computed Down" twin, whose token is ``Columns`` yet
    computes vertically). For the ordered scopes (``Field`` / ``Pane`` / ...), the axis is the shelf
    that carries the ordering dimension -- taken from the explicit ordering field when present, else
    from the shelf that uniquely carries a temporal (date-grain) pill (the natural ordering axis).
    """
    ot = usage.ordering_type or "Table"
    if ot == "Rows":
        return "COLUMNS", None
    if ot == "Columns":
        return "ROWS", None
    for od in (usage.ordering_fields or []):
        if any(getattr(p, "column", None) == od for p in usage.cols):
            return "COLUMNS", None
        if any(getattr(p, "column", None) == od for p in usage.rows):
            return "ROWS", None
    cols_temporal = any(is_temporal(p) for p in usage.cols)
    rows_temporal = any(is_temporal(p) for p in usage.rows)
    if cols_temporal and not rows_temporal:
        return "COLUMNS", None
    if rows_temporal and not cols_temporal:
        return "ROWS", None
    if usage.cols and not usage.rows:
        return "COLUMNS", None
    if usage.rows and not usage.cols:
        return "ROWS", None
    return None, ("axis is not recoverable: no explicit ordering field and no unique temporal "
                  "shelf to fix the across/down direction")


def _has_reset(usage) -> bool:
    """A pane token or a level-break both mean 'restart at each highest parent'."""
    return bool(usage.level_break) or (usage.ordering_type in _PANE_TOKENS)


def _offset_from_level_address(level_address, leaf_shelf) -> Tuple[Optional[int], Optional[str]]:
    """Periods back for an above-leaf addressing grain (Year-over-Year over a Quarter leaf = 4)."""
    if not level_address:
        return 1, None
    addr_unit = _GRAIN_TOKEN_UNIT.get(_grain_token(level_address))
    leaf_unit = None
    for p in leaf_shelf:                       # innermost (last) temporal pill wins
        u = _DERIVATION_UNIT.get(getattr(p, "derivation", None))
        if u:
            leaf_unit = u
    if not addr_unit or not leaf_unit:
        return None, (f"cannot resolve calendar grains for an above-leaf offset "
                      f"(address grain={_grain_token(level_address)!r}, leaf shelf unresolved)")
    if addr_unit == leaf_unit:
        return 1, None
    ratio = _CALENDAR_RATIO.get((addr_unit, leaf_unit))
    if not ratio:
        return None, (f"no constant calendar ratio between address grain {addr_unit!r} and leaf "
                      f"grain {leaf_unit!r}; the offset is not a fixed number of periods")
    return ratio, None


def _derive_collapse_scope(usage) -> Optional[str]:
    """Shelves a percent-of-total collapses over -> a single space-joined token (``ROWS COLUMNS``)."""
    of = set(usage.ordering_fields or [])
    cols_addr = any(getattr(p, "column", None) in of for p in usage.cols) if of else bool(usage.cols)
    rows_addr = any(getattr(p, "column", None) in of for p in usage.rows) if of else bool(usage.rows)
    scopes = []
    if rows_addr:
        scopes.append("ROWS")
    if cols_addr:
        scopes.append("COLUMNS")
    return " ".join(scopes) if scopes else None


def _derive_rank_partition(usage, axis) -> Tuple[Optional[str], Optional[str]]:
    """Pane boundary of a rank = the OUTERMOST temporal grain on the axis shelf (Year over Quarter)."""
    for p in _shelf_for(usage, axis):
        if _is_temporal_pill(p):
            return getattr(p, "column", None), getattr(p, "derivation", None)
    return None, None


def _parse_rank_options(rank_options) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """``(ties, direction, reason)`` from a Tableau ``rank-options`` string like ``"Unique,Descending"``.

    Defaults are Tableau's own quick-table-calc default: Competition ties (-> SKIP) and Descending
    direction (largest value = rank 1 -> ORDERBY DESC). A tie mode with no faithful native
    visual-calculation equivalent (Modified / Unique) returns a review reason instead of a wrong
    ranking rule. Unknown tokens are ignored (defaults kept), never guessed into a rule.
    """
    ties = "SKIP"
    direction = "DESC"
    for tok in (rank_options or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok in _RANK_DIRECTION:
            direction = _RANK_DIRECTION[tok]
        elif tok in _RANK_TIES:
            ties = _RANK_TIES[tok]
        elif tok in _RANK_TIES_REVIEW:
            return None, None, (f"rank tie mode {tok!r} has no faithful Power BI visual-calculation "
                                "RANK equivalent")
    return ties, direction, None


def _summary(usage) -> str:
    bits = [f"Quick Table Calc {usage.calc_type}"]
    if usage.aggregation:
        bits.append(f"agg={usage.aggregation}")
    if usage.window_from is not None or usage.window_to is not None:
        bits.append(f"window[{usage.window_from}..{usage.window_to}]")
    if usage.level_break:
        bits.append("restart=level-break")
    if usage.level_address:
        bits.append(f"level-address={_grain_token(usage.level_address)}")
    if usage.diff_options:
        bits.append(f"diff={usage.diff_options}")
    if usage.rank_options:
        bits.append(f"rank={usage.rank_options}")
    bits.append(f"ordering={usage.ordering_type}")
    if usage.secondary_pass:
        bits.append(f"+secondary({usage.secondary_pass.get('calc_type')})")
    return "; ".join(bits)


# -- classification ------------------------------------------------------------
def _classify(usage) -> Tuple[Optional[str], Optional[str]]:
    """Map the recovered facts to a view-layer family, or a review reason for an off-substrate calc."""
    ct = usage.calc_type
    diff = usage.diff_options or ""
    if usage.secondary or usage.secondary_pass:
        sp = usage.secondary_pass or {}
        if ct == "CumTotal" and sp.get("calc_type") == "PctDiff" and sp.get("level_address"):
            return FAMILY_YTD_GROWTH, None
        return None, ("stacked secondary calculation is not a recognized view-layer chain "
                      f"(primary={ct!r}, secondary={sp.get('calc_type')!r})")
    if ct == "CumTotal":
        return (FAMILY_YTD if usage.level_break else FAMILY_RUNNING_TOTAL), None
    if ct == "WindowTotal":
        if (usage.aggregation or "").lower() == "avg":
            return FAMILY_MOVING_AVERAGE, None
        return None, (f"windowed {usage.aggregation!r} aggregate is not yet a view-layer family "
                      "(only a moving average is)")
    if ct == "PctRank":
        return FAMILY_PERCENTILE, None
    if ct == "Rank":
        return FAMILY_RANK, None
    if ct == "PctTotal":
        return FAMILY_PERCENT_OF_TOTAL, None
    if ct == "Difference":
        return FAMILY_DIFFERENCE, None
    if ct == "PctDiff":
        if "Compounded" in diff:
            return FAMILY_COMPOUND_GROWTH, None
        if usage.level_address:
            return FAMILY_YEAR_OVER_YEAR, None
        return FAMILY_PERCENT_DIFFERENCE, None
    return None, f"quick table calc type {ct!r} has no view-layer visual-calculation mapping"


# -- public API ----------------------------------------------------------------
def usage_to_visual_calc_spec(
    usage,
    *,
    role: str = "value",
    is_temporal: Optional[Callable] = None,
    visual_axis: Optional[str] = None,
) -> Tuple[Optional[VisualCalcSpec], Optional[str]]:
    """Normalize one quick-table-calc usage into a :class:`VisualCalcSpec`, or ``(None, reason)``.

    ``role`` is ``"value"`` when the calc is the displayed number and ``"color"`` when it drives a
    conditional-format fill (the wiring layer decides which from the worksheet's marks encoding).
    ``visual_axis`` overrides the derived matrix axis for a **cartesian chart**: a chart has a single
    category axis (the "rows" of its result matrix), so any chart Visual Calculation runs along that
    axis (``"ROWS"``) regardless of the Tableau ordering token -- a structural fact of chart geometry,
    not a per-example override. When omitted (the matrix path) the axis is derived from the shelves as
    before, so matrix output is byte-for-byte unchanged. Returns a review reason (never a guess)
    whenever the axis, a calendar offset, or a chain shape cannot be pinned from the workbook facts.
    """
    if getattr(usage, "kind", None) != "quick":
        return None, "not a quick table calc (model-level calc fields are the measure path's job)"
    is_temporal = is_temporal or _is_temporal_pill

    family, reason = _classify(usage)
    if family is None:
        return None, reason

    if visual_axis is not None:
        axis = visual_axis
    else:
        axis, ax_reason = _derive_axis(usage, is_temporal)
        if axis is None:
            return None, ax_reason

    common = dict(
        role=role,
        tableau_calc_type=usage.calc_type or "",
        tableau_instance=getattr(usage, "instance", "") or "",
        source_worksheet=getattr(usage, "worksheet", "") or "",
        tableau_summary=_summary(usage),
    )

    if family in (FAMILY_RUNNING_TOTAL, FAMILY_YTD):
        reset = "HIGHESTPARENT" if _has_reset(usage) else None
        return VisualCalcSpec(family=family, axis=axis, reset=reset, **common), None

    if family == FAMILY_MOVING_AVERAGE:
        if usage.window_from is None or usage.window_to is None:
            return None, "moving average is missing its relative window bounds (from/to)"
        size = usage.window_to - usage.window_from + 1
        if size <= 0:
            return None, f"moving average window is non-positive (from={usage.window_from}, to={usage.window_to})"
        reset = "HIGHESTPARENT" if _has_reset(usage) else None
        return VisualCalcSpec(family=family, axis=axis, window_size=size,
                              window_agg="Avg", reset=reset, **common), None

    if family == FAMILY_PERCENTILE:
        pill, grain = _derive_rank_partition(usage, axis)
        if not pill and usage.ordering_type in _PANE_TOKENS:
            return None, "percentile pane partition is not recoverable (no temporal grain on the axis shelf)"
        return VisualCalcSpec(family=family, axis=axis,
                              partition_pill=pill, partition_grain=grain, **common), None

    if family == FAMILY_RANK:
        ties, direction, rank_reason = _parse_rank_options(getattr(usage, "rank_options", None))
        if rank_reason:
            return None, rank_reason
        # A pane-scoped rank restarts per outer temporal grain; a Table/other-scoped rank ranks the
        # whole visual with no partition. Recover the pane column only when the token is a pane token
        # (else no partition, which is the faithful whole-table rank -- the common CustomerRank case).
        pill, grain = _derive_rank_partition(usage, axis)
        if not pill and usage.ordering_type in _PANE_TOKENS:
            return None, "rank pane partition is not recoverable (no temporal grain on the axis shelf)"
        if usage.ordering_type not in _PANE_TOKENS:
            pill, grain = None, None
        return VisualCalcSpec(family=family, axis=axis, partition_pill=pill, partition_grain=grain,
                              rank_ties=ties, rank_direction=direction, **common), None

    if family == FAMILY_COMPOUND_GROWTH:
        return VisualCalcSpec(family=family, axis=axis, **common), None

    if family == FAMILY_PERCENT_OF_TOTAL:
        if visual_axis is not None:
            # A cartesian chart collapses over its single (visual) axis: COLLAPSE (a partition
            # subtotal) when the ordering leaves a partition dimension in place, else COLLAPSEALL
            # (the grand total). The addressed/partition split comes from the shared resolver.
            addressed, partition, addr_reason = resolve_addressing(usage)
            if addressed is None:
                return None, addr_reason
            return VisualCalcSpec(family=family, axis=axis, collapse_scope=visual_axis,
                                  collapse_all=not partition, **common), None
        scope = _derive_collapse_scope(usage)
        if not scope:
            if usage.ordering_fields:
                # An explicit "compute using" names a field that is not on the visual's shelves -> the
                # collapse target is genuinely unrecoverable. Keep routing to review (faithful-or-stub);
                # never guess a direction for an explicit addressing we cannot pin.
                return None, "percent of total has no populated shelf to collapse over"
            # No explicit "compute using" at all -> Tableau's default is Table (Down): collapse the row
            # axis to the grand total (COLLAPSEALL, the dataclass default). A documented default, not a
            # guess -- the axis is already pinned above from the shelves.
            scope = "ROWS"
        return VisualCalcSpec(family=family, axis=axis, collapse_scope=scope, **common), None

    if family == FAMILY_PERCENT_DIFFERENCE:
        return VisualCalcSpec(family=family, axis=axis, offset_k=1, **common), None

    if family == FAMILY_DIFFERENCE:
        return VisualCalcSpec(family=family, axis=axis, offset_k=1, **common), None

    if family == FAMILY_YEAR_OVER_YEAR:
        k, k_reason = _offset_from_level_address(usage.level_address, _shelf_for(usage, axis))
        if k is None:
            return None, k_reason
        return VisualCalcSpec(family=family, axis=axis, offset_k=k, **common), None

    if family == FAMILY_YTD_GROWTH:
        sp = usage.secondary_pass or {}
        k, k_reason = _offset_from_level_address(sp.get("level_address"), _shelf_for(usage, axis))
        if k is None:
            return None, k_reason
        inner = VisualCalcSpec(family=FAMILY_YTD, axis=axis, reset="HIGHESTPARENT", **common)
        return VisualCalcSpec(family=family, axis=axis, offset_k=k, chain_inner=inner, **common), None

    return None, f"internal: unhandled family {family!r}"
