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
addressing **direction** (Tableau's "across" vs "down"), and the scope-relative ``ordering-type``
tokens (``Table`` / ``Pane`` / ``Cell`` / ``Rows`` / ``Columns`` and the compound
``ColumnInPane`` / ``PaneCol`` / ``CellInPane``) do **not** encode that direction in a way this
code can pin from the workbook alone. Emitting DAX for those would be a guess masquerading as a
translation -- exactly what the faithful-or-stub contract forbids. So only the **explicit
``Field`` scope** (Tableau "Specific Dimensions"), where the addressing dimensions and sort are
stated outright, takes the deterministic path here; the scope-relative majority is handed off with
its facts intact. Two further always-handoff cases: a **secondary (stacked) calculation** (only the
primary pass is synthesized in Tier 0) and an **order-sensitive** Field calc addressed by **more
than one dimension** (the slowest->fastest order among them is not recoverable from the workbook).

Stdlib-only, offline, deterministic. The ``usage`` argument is duck-typed (any object with the
:class:`TableCalcUsage` attributes), so this module does not hard-depend on the extractor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from calc_to_dax import translate_tableau_table_calc_to_dax


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

# Leading table-calc functions whose value is INDEPENDENT of the addressing order: a window
# aggregate over the entire partition (no relative bounds). For these the order spec only frames
# the partition, so any order yields the same result and multiple addressing dims stay faithful.
# Everything else (RUNNING_* / INDEX / RANK / LOOKUP / FIRST / LAST / PREVIOUS_VALUE) is
# order-SENSITIVE: its value changes with the slowest->fastest order among addressing dims.
_ORDER_INSENSITIVE_HEADS = ("WINDOW_SUM", "WINDOW_AVG", "WINDOW_MIN", "WINDOW_MAX")

# Pill derivations that mean "an aggregated measure", not a partition dimension.
_AGG_DERIVATIONS = {
    "Sum", "Avg", "Min", "Max", "Count", "Cntd", "Median", "Attr",
    "Stdev", "StdevP", "Var", "VarP", "Measure",
}


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


def _is_order_sensitive(formula: str) -> bool:
    """True unless the formula is a full-partition window aggregate (order-independent value)."""
    head = (formula or "").lstrip().upper()
    return not any(head.startswith(h) for h in _ORDER_INSENSITIVE_HEADS)


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
        if usage.window_from is not None or usage.window_to is not None:
            return None, "moving window (relative from/to bounds) not yet supported in Tier 0"
        if agg not in _WINDOW_FN:
            return None, f"WindowTotal with unsupported aggregation {agg!r}"
        return f"{_WINDOW_FN[agg]}({_AGG_FN[agg]}([{col}]))", None
    if ct == "Rank":
        return None, "Rank (RANKX with rank-options) not yet supported in Tier 0"
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


# -- public API ----------------------------------------------------------------
def translate_table_calc_usage(usage, resolver) -> TableCalcTranslation:
    """Map one :class:`TableCalcUsage` to faithful DAX or a structured Tier-1 handoff.

    ``resolver(caption) -> (table, column, type) | None`` is the same field resolver the rest of
    the translator uses.
    """
    intent = _intent_for(usage)

    # 0) a stacked secondary calculation adds a second addressing pass Tier 0 does not model.
    if getattr(usage, "secondary", False):
        return _handoff(
            usage, intent,
            "secondary (stacked) table calculation: only the primary pass is synthesized in "
            "Tier 0, so the second addressing pass would be silently dropped")

    # 1) the table-calc formula (synthesized for a QTC; given for a user calc field).
    if usage.kind == "quick":
        formula, reason = _synthesize_formula(usage)
        if formula is None:
            return _handoff(usage, intent, reason)
    else:
        formula = (usage.formula or "").strip()
        if not formula:
            return _handoff(usage, intent, "user calc field carries no formula")

    # 2) addressing -- only the explicit Field scope is deterministically recoverable here.
    if usage.ordering_type != "Field":
        return _handoff(
            usage, intent,
            f"scope-relative addressing {usage.ordering_type!r}: the across/down direction that "
            "fixes the partition is not recoverable from the workbook encoding")
    order_by, partition_by, reason = _field_scope_addressing(
        usage, _is_order_sensitive(formula))
    if reason is not None:
        return _handoff(usage, intent, reason)

    # 3) hand the synthesized formula + explicit addressing to the trusted window seam.
    dax, seam_reason, _tables = translate_tableau_table_calc_to_dax(
        formula, resolver, partition_by=partition_by, order_by=order_by)
    if dax is None:
        return _handoff(usage, intent, f"window seam fallback: {seam_reason}")
    return TableCalcTranslation(
        worksheet=usage.worksheet, field=usage.caption, intent=intent,
        status="translated", dax=dax, partition_by=partition_by, order_by=order_by,
        translated_by="deterministic (workbook addressing)")


def translate_table_calc_usages(usages, resolver) -> List[TableCalcTranslation]:
    """Batch :func:`translate_table_calc_usage` over an iterable of usages."""
    return [translate_table_calc_usage(u, resolver) for u in usages]
