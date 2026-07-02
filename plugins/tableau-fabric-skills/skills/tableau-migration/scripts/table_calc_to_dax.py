"""Map a recovered :class:`TableCalcUsage` to faithful DAX, or to a structured Tier-1 handoff.

Translate the **intent, not the function**. A Tableau table calculation is an
addressing/partitioning expression over the viz rows; its faithfulness hinges entirely on
its *Compute Using* (partition + order), which :mod:`workbook_table_calcs` recovers from the
worksheet. This module is the consumer of that record. It emits DAX **only** when the
addressing is deterministically and unambiguously recoverable *and* the existing window seam
(:func:`calc_to_dax.translate_tableau_table_calc_to_dax`) can honor it faithfully. Everything
else becomes a **structured handoff** carrying every recovered fact plus an inferred-intent
label, for the agent-as-second-compiler (Tier 1) to resolve against real usage.

Why so conservative? A running total / moving window / rank's *value* depends on the
addressing **direction** (Tableau's "across" vs "down"), and most scope-relative ``ordering-type``
tokens (``Table`` / ``Pane`` / ``Cell`` / ``Columns`` and the compound ``ColumnInPane`` /
``PaneCol`` / ``CellInPane``) do **not** encode that direction in a way this code can pin from the
workbook alone. Emitting DAX for those would be a guess masquerading as a translation -- exactly
what the faithful-or-stub contract forbids. So the deterministic path is taken only for addressing
this code can pin: the **explicit ``Field`` scope** (Tableau "Specific Dimensions"), where the
dimensions and sort are stated outright, and the **``Rows`` pane scope**, whose across/down
direction *is* recoverable from the shelves (VERIFIED against real Tableau output: the calc restarts
at each Rows-shelf row -- partition = the Rows dims -- and runs across the Cols-shelf dims -- order =
the Cols dims). The remaining scope-relative tokens are handed off with their facts intact. Two
further always-handoff cases:  a **secondary (stacked) calculation** (only the
primary pass is synthesized in Tier 0) and an **order-sensitive** Field calc addressed by **more
than one dimension** (the slowest->fastest order among them is not recoverable from the workbook).

Stdlib-only, offline, deterministic. The ``usage`` argument is duck-typed (any object with the
:class:`TableCalcUsage` attributes), so this module does not hard-depend on the extractor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from calc_to_dax import (
    translate_tableau_table_calc_to_dax,
    translate_percent_diff_to_dax,
    translate_difference_to_dax,
    translate_percent_of_total_to_dax,
)
from workbook_table_calcs import AGG_DERIVATIONS


# -- intent classification -----------------------------------------------------
# QTC type / leading table-calc function -> the business INTENT it encodes. The intent is what
# a Power BI modeler reasons about; it is carried on every result (translated or handoff) so the
# second compiler can pick the leanest faithful idiom.
_QTC_INTENT = {
    "CumTotal": "running total (cumulative)",
    "WindowTotal": "window aggregate (partition or moving)",
    "Rank": "rank within partition",
    "RunningTotal": "running total (cumulative)",
    "Difference": "difference from a prior row",
    "PercentDifference": "percent difference from a prior row",
    "PctDiff": "percent difference from a prior row",
    "PercentDifferenceFrom": "percent difference from a prior row",
    "PercentOfTotal": "percent-of-scope ratio",
    "Movingcalculation": "moving window",
}
_FORMULA_INTENT = [
    ("RUNNING_", "running total (cumulative)"),
    ("WINDOW_", "window aggregate (partition or moving)"),
    ("INDEX", "row number within partition"),
    ("SIZE", "partition size"),
    ("RANK", "rank within partition"),
    ("FIRST", "offset to first row"),
    ("LAST", "offset to last row"),
    ("LOOKUP", "offset / value at another row"),
    ("PREVIOUS_VALUE", "prior-row value"),
    ("TOTAL", "scope total"),
]

# Tableau aggregation (as it appears in the QTC ``aggregation`` attr / pill derivation) ->
# the RUNNING_* / WINDOW_* function and the inner Tableau aggregate function name.
_RUNNING_FN = {"Sum": "RUNNING_SUM", "Avg": "RUNNING_AVG",
               "Min": "RUNNING_MIN", "Max": "RUNNING_MAX"}
_WINDOW_FN = {"Sum": "WINDOW_SUM", "Avg": "WINDOW_AVG",
              "Min": "WINDOW_MIN", "Max": "WINDOW_MAX"}
_AGG_FN = {"Sum": "SUM", "Avg": "AVG", "Min": "MIN", "Max": "MAX"}

# Rank quick-table-calc ``rank-options`` tie-mode token -> the Tableau RANK* function the window
# seam translates to RANKX. Unlike CumTotal/WindowTotal a Rank QTC carries NO ``aggregation`` attr
# (it lives on the pill), so the inner aggregate is taken from the pill's own ``derivation``.
#   Competition          -> RANK          (1, 2, 2, 4 -- standard competition ranking; Tableau default)
#   Dense                -> RANK_DENSE    (1, 2, 2, 3)
#   ModifiedCompetition  -> RANK_MODIFIED (1, 3, 3, 4 -- ties take the HIGHEST ordinal)
# "Unique" is intentionally ABSENT: Tableau's unique ranking breaks ties by ADDRESSING ORDER, which
# the window seam cannot reproduce faithfully (RANK_UNIQUE falls back), so it stays a handoff.
_RANK_TIE_FN = {
    "competition": "RANK",
    "dense": "RANK_DENSE",
    "modifiedcompetition": "RANK_MODIFIED",
}

# QTC ``calc_type`` values that encode "percent difference from the previous row" -- a COMPOSITE
# ``(X - LOOKUP(X,-1)) / ABS(LOOKUP(X,-1))`` the single-head window seam cannot parse, so it routes
# to the dedicated :func:`calc_to_dax.translate_percent_diff_to_dax` emitter. Tableau writes the
# type as ``PctDiff`` in the .twb; the older/internal spelling ``PercentDifference`` is accepted too.
_PCT_DIFF_TYPES = {"PctDiff", "PercentDifference", "PercentDifferenceFrom"}
# QTC ``calc_type`` for "difference from the previous row" -- the ADDITIVE composite
# ``X - LOOKUP(X,-1)`` (the percent-difference's un-normalised sibling), routed to the dedicated
# :func:`calc_to_dax.translate_difference_to_dax` emitter (single-head seam cannot parse a composite).
_DIFF_TYPES = {"Difference", "DifferenceFrom"}
# QTC ``calc_type`` for "percent of total" -- the composite ``X / TOTAL(X)`` (the current mark's
# share of its addressing-scope total), routed to :func:`calc_to_dax.translate_percent_of_total_to_dax`.
_PCT_TOTAL_TYPES = {"PercentOfTotal", "PctOfTotal"}
# A short human label for the derived measure name so the percent-difference measure reads distinctly
# from the untransformed base measure it is computed over (the two are bound by DIFFERENT tokens).
_PCT_DIFF_LABEL = "% Difference"

# Leading table-calc functions whose value is INDEPENDENT of the addressing order: a window
# aggregate over the entire partition (no relative bounds). For these the order spec only frames
# the partition, so any order yields the same result and multiple addressing dims stay faithful.
# Everything else (RUNNING_* / INDEX / RANK / LOOKUP / FIRST / LAST / PREVIOUS_VALUE) is
# order-SENSITIVE: its value changes with the slowest->fastest order among addressing dims.
_ORDER_INSENSITIVE_HEADS = ("WINDOW_SUM", "WINDOW_AVG", "WINDOW_MIN", "WINDOW_MAX")

# Pill derivations that mean "an aggregated measure", not a partition dimension. Shared with the
# view-layer path: the canonical set now lives in :mod:`workbook_table_calcs` (next to the ``Pill``
# both paths import); this alias keeps the local references below behaviour-identical. An equality
# test guards the two from drifting apart.
_AGG_DERIVATIONS = AGG_DERIVATIONS

# Tableau writes a calculated field's shelf reference as its INTERNAL token -- the auto-generated
# ``Calculation_<digits>`` / legacy ``Calculation<n>`` form -- whereas a physical field appears by its
# caption. A calc pill can carry derivation ``None`` (a calc is not an aggregation), so derivation
# alone does NOT distinguish it from a plain dimension; this pattern (plus a known-calc set) does.
_CALC_TOKEN_RE = re.compile(r"^Calculation_?\d+$")


def _is_calc_token(column, calc_tokens=()):
    """True iff ``column`` names a calculated field rather than a physical dimension -- it is in the
    known-calc set ``calc_tokens`` OR matches Tableau's internal ``Calculation_<digits>`` token. Used
    to keep a calc pill out of an INHERITED partition/order (a calc is not a faithful physical axis)."""
    if not column:
        return False
    if column in calc_tokens:
        return True
    return bool(_CALC_TOKEN_RE.match(column))


def _intent_for(usage) -> str:
    if usage.kind == "quick" and usage.calc_type:
        if usage.calc_type == "WindowTotal" and (
                usage.window_from is not None or usage.window_to is not None):
            return "moving window"
        return _QTC_INTENT.get(usage.calc_type, f"table calc ({usage.calc_type})")
    head = (usage.formula or "").lstrip().upper()
    for prefix, intent in _FORMULA_INTENT:
        if head.startswith(prefix):
            return intent
    return "table calculation"


def _has_moving_bounds(formula: str) -> bool:
    """True iff a WINDOW_* call carries explicit ``, start, end`` moving bounds (>1 top-level arg).

    The whole-partition form ``WINDOW_AVG(AVG([x]))`` has no top-level comma inside its outermost
    parens; the moving form ``WINDOW_AVG(AVG([x]), -2, 0)`` has two. Commas nested inside the inner
    aggregate's own parens sit at a deeper paren depth and are not counted.
    """
    f = (formula or "").strip()
    i = f.find("(")
    if i < 0:
        return False
    depth = 0
    for ch in f[i:]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        elif ch == "," and depth == 1:
            return True
    return False


def _is_order_sensitive(formula: str) -> bool:
    """True unless the formula is a FULL-PARTITION window aggregate (order-independent value).

    The four WINDOW_SUM/AVG/MIN/MAX heads are order-independent ONLY in their bare whole-partition
    form; the SAME head with explicit moving (start, end) bounds is a sliding frame whose value
    depends on the addressing order, so it is order-sensitive like every other table calc.
    """
    head = (formula or "").lstrip().upper()
    if not any(head.startswith(h) for h in _ORDER_INSENSITIVE_HEADS):
        return True
    return _has_moving_bounds(formula)


# -- result --------------------------------------------------------------------
@dataclass
class TableCalcTranslation:
    """The outcome of mapping one :class:`TableCalcUsage` to DAX (or to a handoff)."""
    worksheet: str
    field: str                              # the field caption
    intent: str
    status: str                             # "translated" | "handoff"
    dax: Optional[str] = None
    partition_by: Tuple[str, ...] = ()
    order_by: Tuple = ()                    # captions or (caption, "ASC"|"DESC") pairs
    translated_by: Optional[str] = None     # provenance stamp when translated
    reason: Optional[str] = None            # why it was handed off
    handoff: Optional[dict] = None          # structured Tier-1 request (when status="handoff")

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["partition_by"] = list(self.partition_by)
        d["order_by"] = [list(o) if isinstance(o, (tuple, list)) else o
                         for o in self.order_by]
        return d


# -- helpers -------------------------------------------------------------------
def _dim_pills(usage):
    """Shelf pills that are real partition dimensions (not aggregated measures / calc instances)."""
    out = []
    for pill in list(usage.rows) + list(usage.cols):
        if pill.derivation in _AGG_DERIVATIONS or pill.derivation == "User":
            continue
        out.append(pill)
    return out


def _is_plain_dim_column(usage, column: str) -> bool:
    """True iff ``column`` appears on a shelf as a plain (non-derived) dimension pill."""
    for pill in list(usage.rows) + list(usage.cols):
        if pill.column == column:
            return pill.derivation == "None"
    return False


def _handoff(usage, intent, reason) -> TableCalcTranslation:
    base = usage.formula if usage.kind == "field" else None
    request = {
        "worksheet": usage.worksheet,
        "field": usage.caption,
        "kind": usage.kind,
        "calc_type": usage.calc_type,
        "formula": base,
        "base_column": usage.column,
        "intent": intent,
        "aggregation": usage.aggregation,
        "window_from": usage.window_from,
        "window_to": usage.window_to,
        "window_options": usage.window_options,
        "rank_options": usage.rank_options,
        "ordering_type": usage.ordering_type,
        "ordering_fields": list(usage.ordering_fields),
        "sort_field": usage.sort_field,
        "sort_direction": usage.sort_direction,
        "secondary": bool(getattr(usage, "secondary", False)),
        "shelf_rows": [[p.column, p.derivation] for p in usage.rows],
        "shelf_cols": [[p.column, p.derivation] for p in usage.cols],
        "reason": reason,
    }
    return TableCalcTranslation(
        worksheet=usage.worksheet, field=usage.caption, intent=intent,
        status="handoff", reason=reason, handoff=request)


def _rank_formula(usage, col) -> Tuple[Optional[str], Optional[str]]:
    """Synthesize the Tableau ``RANK*`` formula for a Rank quick table calc from its ``rank-options``.

    A Rank QTC has no ``aggregation`` attr (it sits on the pill), so the inner aggregate is the
    pill's ``derivation`` (e.g. ``Sum`` -> ``SUM([col])``). ``rank-options`` is a comma-joined
    ``"<tie-mode>,<direction>"`` (e.g. ``"Competition,Descending"``); a missing token defaults to
    Tableau's own default (competition ranking, descending). Fail-closed: the 'Unique' tie mode (its
    addressing-order tiebreak is not faithfully expressible in DAX) and any unrecognized ranking
    token hand off rather than emit an unfaithful rank.
    """
    agg = usage.aggregation or getattr(usage, "derivation", None)
    if agg not in _AGG_FN:
        return None, f"Rank over unsupported aggregation {agg!r}"
    tokens = [t.strip().lower() for t in (usage.rank_options or "").split(",") if t.strip()]
    direction = "asc" if any(t.startswith("asc") for t in tokens) else "desc"
    tie_tokens = [t for t in tokens if not (t.startswith("asc") or t.startswith("desc"))]
    if "unique" in tie_tokens:
        return None, ("Rank with 'Unique' ranking breaks ties by addressing order, which is not "
                      "faithfully expressible in DAX (RANK_UNIQUE falls back)")
    unknown = [t for t in tie_tokens if t not in _RANK_TIE_FN]
    if unknown:
        return None, f"Rank with unsupported ranking option {unknown[0]!r}"
    tie = tie_tokens[0] if tie_tokens else "competition"
    return f"{_RANK_TIE_FN[tie]}({_AGG_FN[agg]}([{col}]), '{direction}')", None


def _synthesize_formula(usage) -> Tuple[Optional[str], Optional[str]]:
    """For a Quick Table Calc, build the equivalent Tableau table-calc formula.

    Returns ``(formula, None)`` or ``(None, reason)``. User-defined calc fields already carry a
    formula and skip this path.
    """
    ct = usage.calc_type
    agg = usage.aggregation
    col = usage.column or ""
    # The workbook extractor emits bare field ids, but this is a public entry point that
    # orchestrators / agents may also call directly -- tolerate a bracketed id rather than
    # double-wrapping it into "[[col]]" and degrading to a misleading parser handoff.
    if col.startswith("[") and col.endswith("]"):
        col = col[1:-1]
    if ct == "CumTotal":
        if agg not in _RUNNING_FN:
            return None, f"CumTotal with unsupported aggregation {agg!r}"
        return f"{_RUNNING_FN[agg]}({_AGG_FN[agg]}([{col}]))", None
    if ct == "WindowTotal":
        if agg not in _WINDOW_FN:
            return None, f"WindowTotal with unsupported aggregation {agg!r}"
        if usage.window_from is not None or usage.window_to is not None:
            # A moving window with relative bounds: faithful only when BOTH bounds are integer
            # literals (the seam's WINDOW(start, REL, end, REL) form, oracle-certified for
            # SUM/AVG/MIN/MAX). A one-sided / non-integer bound is not a complete moving frame, so
            # fall back rather than guess.
            if not (isinstance(usage.window_from, int) and isinstance(usage.window_to, int)):
                return None, ("moving window needs both integer-literal bounds (got "
                              f"from={usage.window_from!r}, to={usage.window_to!r})")
            return (f"{_WINDOW_FN[agg]}({_AGG_FN[agg]}([{col}]), "
                    f"{usage.window_from}, {usage.window_to})", None)
        return f"{_WINDOW_FN[agg]}({_AGG_FN[agg]}([{col}]))", None
    if ct == "Rank":
        return _rank_formula(usage, col)
    return None, f"Quick Table Calc type {ct!r} not yet supported in Tier 0"


def _field_scope_addressing(usage, order_sensitive: bool):
    """Derive ``(order_by, partition_by, None)`` for an explicit ``Field`` scope, else a reason.

    Tableau "Specific Dimensions": the checked dimensions (``ordering_fields``) are the
    **addressing** direction; the remaining viz dimensions are the **partition**; an explicit
    ``<sort>`` (or the addressed dimension's natural order) defines the order within. We only take
    this path when every dimension involved is a *plain* dimension -- a sort by an aggregate
    measure, or a partition at a date grain (Year/Month/...), is not faithfully expressible
    through the window seam and is handed off instead.

    For an **order-sensitive** calc (running / index / rank / lookup) the order must be
    unambiguous: a single addressing dimension, or an explicit sort by one plain dimension.
    Two or more addressing dimensions leave the slowest->fastest order unrecoverable from the
    workbook, so we hand off rather than guess. For an **order-insensitive** full-partition
    window aggregate the value does not depend on order, so any number of addressing dimensions
    is faithful (the order spec merely frames the partition).
    """
    if not usage.ordering_fields:
        return None, None, "Field scope without an explicit ordering field"

    # partition = shelf dimensions not in the addressing set; require all to be plain dims.
    addressing = set(usage.ordering_fields)
    partition = []
    for pill in _dim_pills(usage):
        if pill.column in addressing:
            continue
        if pill.derivation != "None":
            return None, None, (f"partition includes a date-grain dimension "
                                f"[{pill.column}]/{pill.derivation} (needs date-table modeling)")
        if pill.column not in partition:
            partition.append(pill.column)

    if not order_sensitive:
        # full-partition window aggregate: value is order-independent, so address by the checked
        # dims in any order (the seam still needs a non-empty order spec to frame the window).
        order_by = tuple((f, "ASC") for f in usage.ordering_fields)
        return order_by, tuple(partition), None

    # order-sensitive: the order must be unambiguous.
    if len(usage.ordering_fields) > 1:
        return None, None, (
            "order-sensitive table calc addressed by multiple dimensions "
            f"{list(usage.ordering_fields)}: the slowest->fastest order among them is not "
            "recoverable from the workbook encoding")
    if usage.sort_field:
        if not _is_plain_dim_column(usage, usage.sort_field):
            return None, None, ("orders by an aggregate/derived field "
                                f"[{usage.sort_field}] (window seam orders by base columns only)")
        direction = (usage.sort_direction or "ASC").upper()
        order_by = ((usage.sort_field, direction),)
    else:
        order_by = tuple((f, "ASC") for f in usage.ordering_fields)
    return order_by, tuple(partition), None


def _scope_relative_rows_addressing(usage, order_sensitive):
    """Recover ``(order_by, partition_by, None)`` for the scope-relative ``Rows`` pane scope from
    the worksheet shelves, or ``(None, None, reason)``.

    VERIFIED against real Tableau output (a day-grain DoD heat grid and an unpartitioned line
    chart): under the ``Rows`` scope the calc **restarts at each Rows-shelf row** (so the Rows-shelf
    dimensions are the **partition**) and **runs across the Cols-shelf dimensions** (so the Cols
    dimensions are the **order**, the "across" axis). This is the one scope-relative token whose
    across/down direction is pinned -- every other token (``Pane`` / ``Columns`` / ``Cell`` /
    ``Table`` / the compound ones) still hands off, because its direction is not recoverable from
    the workbook alone.

    Gates (fail-closed): a partition (Rows) pill must be a **plain** dimension -- an aggregate or a
    date-grain partition is not faithfully expressible through the window seam. The order (Cols)
    axis must carry at least one dimension and no aggregate; a **date-grain** order pill IS allowed
    (it is the natural chronological order). An order-sensitive calc addressed across more than one
    Cols dimension hands off (their slowest->fastest order is ambiguous), mirroring the Field-scope
    rule.
    """
    partition = []
    for pill in usage.rows:
        if pill.derivation in _AGG_DERIVATIONS or pill.derivation == "User":
            return None, None, ("Rows-shelf partition carries an aggregate/calc pill "
                                f"[{pill.column}]/{pill.derivation} (not a plain dimension)")
        if pill.derivation != "None":
            return None, None, (f"partition includes a date-grain dimension "
                                f"[{pill.column}]/{pill.derivation} (needs date-table modeling)")
        if pill.column not in partition:
            partition.append(pill.column)

    order_dims = []
    for pill in usage.cols:
        if pill.derivation in _AGG_DERIVATIONS or pill.derivation == "User":
            return None, None, ("order (Cols) axis carries an aggregate/calc pill "
                                f"[{pill.column}]/{pill.derivation}, not a dimension to order across")
        if pill.column not in order_dims:
            order_dims.append(pill.column)
    if not order_dims:
        return None, None, ("scope-relative 'Rows' addressing with no Cols dimension to order "
                            "across: the across direction is not recoverable")
    if order_sensitive and len(order_dims) > 1:
        return None, None, ("order-sensitive 'Rows' table calc addressed across multiple Cols "
                            f"dimensions {order_dims}: their slowest->fastest order is ambiguous")
    order_by = tuple((c, "ASC") for c in order_dims)
    return order_by, tuple(partition), None


# -- public API ----------------------------------------------------------------
def _addressing_for(usage, order_sensitive):
    """Recover ``(order_by, partition_by, reason)`` for a usage from its addressing scope.

    The explicit ``Field`` scope ("Specific Dimensions") is recovered from the stated ordering
    fields; the ``Rows`` pane scope is recovered from the worksheet shelves (VERIFIED: partition =
    the Rows-shelf dims, order = across the Cols-shelf dims). Every other scope-relative token stays
    a handoff -- its across/down direction is not recoverable from the workbook encoding here.
    """
    if usage.ordering_type == "Field":
        return _field_scope_addressing(usage, order_sensitive)
    if usage.ordering_type == "Rows":
        return _scope_relative_rows_addressing(usage, order_sensitive)
    return (None, None,
            f"scope-relative addressing {usage.ordering_type!r}: the across/down direction that "
            "fixes the partition is not recoverable from the workbook encoding")


def _inline_calc_formula(key, base_formula_lookup, seen):
    """Resolve a calc id/caption to its formula, recursively inlining nested calc references so the
    result is a self-contained aggregate expression. Returns ``None`` when ``key`` is not a known
    calc. Cycle-guarded (a reference chain that loops fails closed -> ``None``); a bracketed
    reference that is NOT itself a known calc (a raw field, or the ``__tableau_internal_object_id__``
    token) is left verbatim so the downstream measure parser resolves it normally.
    """
    k = (key or "").strip().lower()
    formula = base_formula_lookup.get(k)
    if formula is None:
        return None
    if k in seen:
        return None
    seen = seen | {k}

    def _sub(m):
        inner = _inline_calc_formula(m.group(1), base_formula_lookup, seen)
        return f"({inner})" if inner is not None else m.group(0)

    return re.sub(r"\[([^\[\]]+)\]", _sub, formula)


def _pct_diff_base_formula(usage, base_formula_lookup):
    """Return ``(base_formula, None)`` -- the pure Tableau aggregate a percent-difference QTC is
    computed over -- or ``(None, reason)`` when the base cannot be recovered as a single aggregate.

    Two shapes are faithful: (a) the QTC sits over a NAMED calc (the pilot's ``[count orders] + 100``)
    whose formula is inlined (recursively) to a self-contained aggregate; (b) the QTC sits directly
    over an aggregated pill (``pcdf`` of ``SUM([Sales])``), rebuilt as ``{AGG}([{col}])``. Anything
    else (an unknown base, or a non-aggregated pill) hands off honestly.
    """
    col = (usage.column or "").strip()
    if col.startswith("[") and col.endswith("]"):
        col = col[1:-1]
    if base_formula_lookup:
        inlined = _inline_calc_formula(col, base_formula_lookup, set())
        if inlined is not None:
            return inlined, None
    agg = getattr(usage, "aggregation", None)
    if agg in _AGG_FN:
        return f"{_AGG_FN[agg]}([{col}])", None
    return None, (f"table-calc base [{col}] is neither a known calc nor a directly "
                  f"aggregated pill (aggregation={agg!r})")


_WS_RE = re.compile(r"\s+")


def extract_percent_diff_base(formula):
    """If ``formula`` is EXACTLY a percent-difference-from-the-previous-row composite
    ``(X - LOOKUP(X, -1)) / ABS(LOOKUP(X, -1))``, return the inner aggregate ``X`` (whitespace
    collapsed); otherwise return ``None``.

    This recognizes the hand-written form a user authors as a NAMED calc field (the pilot's
    ``Percent Difference``) -- distinct from the quick-table-calc ``PctDiff`` pill, which carries a
    ``calc_type`` and is handled by :func:`_pct_diff_base_formula`. The three ``X`` occurrences must
    be byte-identical after whitespace collapse, so a structurally different composite (a percent
    difference over a DIFFERENT base in the numerator vs denominator, say) never matches -- the
    detector is fail-closed by construction. ``X`` itself is validated as a numeric aggregate later,
    by :func:`calc_to_dax.translate_percent_diff_to_dax`.
    """
    if not formula:
        return None
    s = _WS_RE.sub("", formula)
    if not (s.startswith("(") and s.endswith(")")):
        return None
    # Find the top-level ``-LOOKUP(`` (at paren depth 1) that separates X from the first LOOKUP.
    depth = 0
    cut = -1
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "-" and depth == 1 and s[i + 1:i + 8] == "LOOKUP(":
            cut = i
            break
    if cut < 1:
        return None
    x = s[1:cut]
    if not x:
        return None
    expected = "({x}-LOOKUP({x},-1))/ABS(LOOKUP({x},-1))".format(x=x)
    return x if s == expected else None


def inherited_addressing(usage, calc_tokens=()):
    """Recover ``(order_by, partition_by, reason)`` to LEND to an UNPLACED calc from a PLACED
    consumer ``usage`` whose formula references it.

    An unplaced table calc (one that is never dropped on a shelf -- e.g. a percent-difference measure
    used only inside a Grey/Red colour rule and a tooltip) has no addressing of its own. When a
    PLACED consumer references it, the consumer's worksheet supplies a faithful default window: the
    order runs across the consumer's plain/date **Cols** axis (the natural "across" direction) and the
    partition is the consumer's plain **Rows** dimensions only. Unlike the strict scope-relative
    recovery, a non-plain Rows pill is **excluded** from the inherited partition rather than rejected
    -- the unplaced calc inherits only the clean categorical context, never a window the seam cannot
    express. A CALC pill (``calc_tokens`` / a ``Calculation_*`` token) is excluded from BOTH axes: a
    calculated field is not a faithful physical partition or order column (on a line chart its Rows
    pill is the plotted measure, not a categorical partition -> the percent difference is unpartitioned).
    Fail-closed (``reason``) when, after exclusions, the consumer carries no plain/date Cols axis to
    order across.
    """
    order_dims = []
    for pill in usage.cols:
        if pill.derivation in _AGG_DERIVATIONS or pill.derivation == "User":
            continue
        if _is_calc_token(pill.column, calc_tokens):
            continue
        if pill.column not in order_dims:
            order_dims.append(pill.column)
    if not order_dims:
        return None, None, ("consumer worksheet carries no plain Cols dimension to order across: "
                            "the prior-row direction is not recoverable")
    partition = []
    for pill in usage.rows:
        if pill.derivation != "None":
            continue
        if _is_calc_token(pill.column, calc_tokens):
            continue
        if pill.column not in partition:
            partition.append(pill.column)
    order_by = tuple((c, "ASC") for c in order_dims)
    return order_by, tuple(partition), None


def _inline_refs_in_expr(expr, base_formula_lookup):
    """Inline every ``[calc]`` reference inside an arbitrary expression to a self-contained aggregate.
    A bracketed token that is NOT a known calc (a raw field, or the ``__tableau_internal_object_id__``
    token) is left verbatim so the downstream measure parser resolves it normally.
    """
    if not base_formula_lookup:
        return expr

    def _sub(m):
        inner = _inline_calc_formula(m.group(1), base_formula_lookup, set())
        return f"({inner})" if inner is not None else m.group(0)

    return re.sub(r"\[([^\[\]]+)\]", _sub, expr)


def translate_unplaced_percent_diff(calc_formula, consumer_usage, resolver,
                                    known_tables=None, base_formula_lookup=None, calc_tokens=(),
                                    order_resolver=None):
    """Force-translate an UNPLACED percent-difference measure calc by inheriting addressing from a
    PLACED consumer usage that references it.

    A percent-difference measure authored as a named calc but never dropped on a shelf (the pilot's
    ``Percent Difference``, used only inside a Grey/Red colour rule and a tooltip) has no addressing
    of its own, so the plain measure path can only stub it (``LOOKUP`` needs a window). When a placed
    consumer references it, that consumer's worksheet lends a faithful window via
    :func:`inherited_addressing` (``calc_tokens`` keeps the consumer's calc pills out of that window).
    Returns ``(dax, reason, order_by, partition_by)``: ``dax`` is ``None`` (with ``reason``) --
    fail-closed -- when the formula is not an exact percent-difference composite, the consumer carries
    no orderable axis, or the inlined base aggregate does not translate; the recovered
    ``order_by``/``partition_by`` are returned for provenance either way.
    """
    base = extract_percent_diff_base(calc_formula)
    if base is None:
        return (None,
                "not a (X - LOOKUP(X,-1)) / ABS(LOOKUP(X,-1)) percent-difference composite",
                (), ())
    base = _inline_refs_in_expr(base, base_formula_lookup or {})
    order_by, partition_by, reason = inherited_addressing(consumer_usage, calc_tokens=calc_tokens)
    if reason is not None:
        return None, reason, (), ()
    dax, seam_reason, _tables = translate_percent_diff_to_dax(
        base, resolver, partition_by=partition_by, order_by=order_by, known_tables=known_tables,
        order_resolver=order_resolver)
    if dax is None:
        return None, f"percent-difference seam fallback: {seam_reason}", order_by, partition_by
    return dax, None, order_by, partition_by


def _translate_composite_over_base(usage, intent, resolver, *, base_formula_lookup, known_tables,
                                   order_resolver, order_sensitive, emitter, fallback_label):
    """Shared driver for the composite over-a-base QTCs (percent-difference / difference /
    percent-of-total): recover the base aggregate, recover the worksheet addressing, then hand both
    to the dedicated faithful ``emitter``. Each is a scalar composite the single-head window seam
    cannot parse, so each gets its own emitter; the base recovery + addressing recovery + result
    shaping are identical, and fail-closed at every step (an unrecoverable base, an un-pinnable
    addressing, or an emitter fallback all hand off with the recovered facts intact)."""
    base, base_reason = _pct_diff_base_formula(usage, base_formula_lookup or {})
    if base is None:
        return _handoff(usage, intent, base_reason)
    order_by, partition_by, reason = _addressing_for(usage, order_sensitive=order_sensitive)
    if reason is not None:
        return _handoff(usage, intent, reason)
    dax, seam_reason, _tables = emitter(
        base, resolver, partition_by=partition_by, order_by=order_by,
        known_tables=known_tables, order_resolver=order_resolver)
    if dax is None:
        return _handoff(usage, intent, f"{fallback_label} seam fallback: {seam_reason}")
    return TableCalcTranslation(
        worksheet=usage.worksheet, field=usage.caption, intent=intent,
        status="translated", dax=dax, partition_by=partition_by, order_by=order_by,
        translated_by="deterministic (workbook addressing)")


def translate_table_calc_usage(usage, resolver, known_tables=None,
                               base_formula_lookup=None, order_resolver=None) -> TableCalcTranslation:
    """Map one :class:`TableCalcUsage` to faithful DAX or a structured Tier-1 handoff.

    ``resolver(caption) -> (table, column, type) | None`` is the same field resolver the rest of
    the translator uses. ``known_tables`` (an optional set of model table names) is threaded to the
    window seam so an object-id aggregate inside the formula (e.g. ``COUNT([__tableau_internal…]))``)
    resolves to ``COUNTROWS('<Table>')`` only when ``<Table>`` is a real model table.
    ``base_formula_lookup`` (``{calc-id/caption(lower) -> formula}``) lets a percent-difference quick
    table calc inline the formula of the NAMED calc it is computed over (e.g. ``[count orders]+100``).
    ``order_resolver`` (optional) is a TRUSTED ORDERBY-only addressing redirect (e.g. a continuous
    date axis -> the marked-calendar key ``Date[Date]`` on a related Date dimension); it only
    affects the addressing sort, never the inner aggregate or partition, and defaults to None for
    byte-identical output.
    """
    intent = _intent_for(usage)

    # 0) a stacked secondary calculation adds a second addressing pass Tier 0 does not model.
    if getattr(usage, "secondary", False):
        return _handoff(
            usage, intent,
            "secondary (stacked) table calculation: only the primary pass is synthesized in "
            "Tier 0, so the second addressing pass would be silently dropped")

    # 1) composite over-a-base QTCs the single-head window seam cannot parse, each routed to its own
    #    faithful emitter. Their base may be a named calc (inlined) or an aggregated pill.
    ct = (getattr(usage, "calc_type", "") or "")
    if usage.kind == "quick" and ct in _PCT_DIFF_TYPES:
        # (X - LOOKUP(X,-1)) / ABS(LOOKUP(X,-1)) -- order-sensitive (looks back one row).
        return _translate_composite_over_base(
            usage, intent, resolver, base_formula_lookup=base_formula_lookup,
            known_tables=known_tables, order_resolver=order_resolver, order_sensitive=True,
            emitter=translate_percent_diff_to_dax, fallback_label="percent-difference")
    if usage.kind == "quick" and ct in _DIFF_TYPES:
        # X - LOOKUP(X,-1) -- order-sensitive (looks back one row); null on the first row.
        return _translate_composite_over_base(
            usage, intent, resolver, base_formula_lookup=base_formula_lookup,
            known_tables=known_tables, order_resolver=order_resolver, order_sensitive=True,
            emitter=translate_difference_to_dax, fallback_label="difference")
    if usage.kind == "quick" and ct in _PCT_TOTAL_TYPES:
        # X / TOTAL(X) -- order-INSENSITIVE (a whole-scope re-aggregation), so multi-dim is faithful.
        return _translate_composite_over_base(
            usage, intent, resolver, base_formula_lookup=base_formula_lookup,
            known_tables=known_tables, order_resolver=order_resolver, order_sensitive=False,
            emitter=translate_percent_of_total_to_dax, fallback_label="percent-of-total")

    # 2) the table-calc formula (synthesized for a QTC; given for a user calc field).
    if usage.kind == "quick":
        formula, reason = _synthesize_formula(usage)
        if formula is None:
            return _handoff(usage, intent, reason)
    else:
        formula = (usage.formula or "").strip()
        if not formula:
            return _handoff(usage, intent, "user calc field carries no formula")

    # 3) addressing -- recovered from the explicit Field scope or the Rows-shelf layout (never
    #    hard-coded); every other scope-relative token hands off.
    order_sensitive = _is_order_sensitive(formula)
    order_by, partition_by, reason = _addressing_for(usage, order_sensitive)
    if reason is not None:
        return _handoff(usage, intent, reason)

    # 4) hand the synthesized formula + explicit addressing to the trusted window seam.
    dax, seam_reason, _tables = translate_tableau_table_calc_to_dax(
        formula, resolver, partition_by=partition_by, order_by=order_by,
        known_tables=known_tables, order_resolver=order_resolver)
    if dax is None:
        return _handoff(usage, intent, f"window seam fallback: {seam_reason}")
    return TableCalcTranslation(
        worksheet=usage.worksheet, field=usage.caption, intent=intent,
        status="translated", dax=dax, partition_by=partition_by, order_by=order_by,
        translated_by="deterministic (workbook addressing)")


def translate_table_calc_usages(usages, resolver, known_tables=None,
                                base_formula_lookup=None, order_resolver=None) -> List[TableCalcTranslation]:
    """Batch :func:`translate_table_calc_usage` over an iterable of usages."""
    return [translate_table_calc_usage(u, resolver, known_tables=known_tables,
                                       base_formula_lookup=base_formula_lookup,
                                       order_resolver=order_resolver) for u in usages]
