"""Compile a Tableau **formula-authored table calculation** into Power BI Visual Calculations.

Tableau lets a table calc be written two ways: as a *Quick Table Calc* dropped on a pill (the
``△`` chip -- handled by :mod:`workbook_table_calcs` + :mod:`visual_calc_spec` +
:mod:`visual_calc_emitter`), or as a **calculated field whose formula calls a table-calc
function** in the calc editor (``RUNNING_SUM(SUM([Sales]))``, ``RANK([Composite])`` ...). This
module is the faithful counterpart for the *formula* case, and -- crucially -- it composes: a
calc field may reference **another** calc field, so a two-level Tableau chain
(``Rank = RANK([composit])`` over ``composit = RUNNING_SUM(...)*a + RUNNING_SUM(...)*b``) rebuilds
as **nested Visual Calculations** -- the inner calc emitted hidden, the outer referencing it by
name -- exactly how Power BI expresses the same intent, and exactly how the user models it.

Why Visual Calculations rather than the model-measure path (:mod:`calc_to_dax` /
:mod:`table_calc_to_dax`): a Tableau table calc computes *along the visual's own layout order*
("Compute Using / Table (across|down)"). A Visual Calculation runs over the visual's result
matrix in that same display order, so it stays faithful when the user re-sorts; a model measure
must bake a fixed ``ORDERBY`` that can drift from the shown order. This module renders the
report-layer dialect (``RUNNINGSUM`` / ``RANK`` / ``ORDERBY`` over ``ROWS`` / ``COLUMNS``), reusing
the exact DAX conventions of :mod:`visual_calc_emitter`.

Closed, **fail-closed** subset (v1 -- the surface proven faithful cell-for-cell):

  * table-calc functions ``RUNNING_SUM`` -> ``RUNNINGSUM`` and ``RANK`` / ``RANK_DENSE`` ->
    ``RANK(SKIP|DENSE, ORDERBY(x, DESC|ASC))`` (Tableau defaults: competition ties, descending);
  * the aggregates ``SUM`` / ``AVG`` / ``MIN`` / ``MAX`` / ``COUNT`` / ``COUNTD`` of a *single*
    column, each resolved by the caller to a base measure present in the visual;
  * ``+ - * /``, unary minus, numeric constants, and parentheses;
  * references to *other* calc fields (-> a nested Visual Calculation) and to plain base measures.

The argument of ``RUNNING_SUM`` / ``RANK`` must be a *single* aggregate or field reference (Power BI
``RUNNINGSUM`` / ``ORDERBY`` take a column, not an inline expression) -- anything else returns
``(None, reason)``. Every token or construct outside the subset likewise returns ``(None, reason)``
so the caller keeps its current behaviour; this module never guesses a translation.

Grounded and validated against the paired Power BI replica; original work (CLEANROOM).
Stdlib-only, deterministic, side-effect free.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

try:
    from .visual_calc_emitter import VisualCalcDef
except ImportError:  # pragma: no cover - flat scripts-on-path
    from visual_calc_emitter import VisualCalcDef

# -- the closed function vocabulary -------------------------------------------
# Running family with a faithful single-argument Visual-Calculation form. Only RUNNING_SUM has a
# native running function (RUNNINGSUM); RUNNING_AVG/MIN/MAX/COUNT have no faithful view-layer
# counterpart, so they are deliberately absent and fail closed (matching the QTC emitter, whose
# running family is RUNNINGSUM-only).
_RUNNING = {"RUNNING_SUM": "RUNNINGSUM"}
# RANK family -> (ties). Tableau Competition ranking (1,2,2,4) is PBI SKIP; Dense (1,2,2,3) is
# DENSE. RANK_MODIFIED / RANK_UNIQUE / RANK_PERCENTILE have no faithful native tie rule and fail
# closed (same line the QTC path draws).
_RANK = {"RANK": "SKIP", "RANK_DENSE": "DENSE"}
# Aggregations the caller can resolve to a base measure. The set the deterministic measure engine
# already treats as faithful column aggregates.
_AGG = {"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "MEDIAN", "STDEV", "STDEVP", "VAR", "VARP"}
_ATTR = "ATTR"   # ATTR(x) is Tableau's "assume one value"; transparent for a single-column ref.

_RANK_DIR = {"asc": "ASC", "desc": "DESC"}


class _CompileError(Exception):
    """Raised internally when the formula leaves the faithful subset; surfaced as a review reason."""


# -- tokenizer (closed subset) -------------------------------------------------
_NUM_RE = re.compile(r"\d*\.?\d+")
_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _tokenize(s: str) -> List[Tuple[str, object]]:
    """Lex the closed subset. Bracketed names (possibly dotted, e.g. ``[ds].[Field]``) fold into a
    single field token; anything unrecognised raises so the whole formula fails closed."""
    toks: List[Tuple[str, object]] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "[":
            parts: List[str] = []
            while i < n and s[i] == "[":
                k = s.find("]", i + 1)
                if k == -1:
                    raise _CompileError("unterminated field reference")
                parts.append(s[i + 1:k])
                i = k + 1
                if i < n and s[i] == ".":     # dotted continuation: [a].[b]
                    i += 1
                    if i >= n or s[i] != "[":
                        raise _CompileError("malformed qualified field reference")
            toks.append(("field", parts))
            continue
        if c in "'\"":
            j = s.find(c, i + 1)
            if j == -1:
                raise _CompileError("unterminated string literal")
            toks.append(("str", s[i + 1:j]))
            i = j + 1
            continue
        if c in "+-*/(),":
            toks.append(("op", c))
            i += 1
            continue
        m = _NUM_RE.match(s, i)
        if m and (c.isdigit() or c == "."):
            toks.append(("num", m.group(0)))
            i = m.end()
            continue
        m = _ID_RE.match(s, i)
        if m:
            toks.append(("id", m.group(0)))
            i = m.end()
            continue
        raise _CompileError(f"unsupported character {c!r}")
    return toks


# -- recursive-descent compiler ------------------------------------------------
class _Compiler:
    """Compile one formula's token stream into a Visual-Calculation DAX expression.

    ``resolve_aggregate(agg, field, datasource) -> base_ref | None`` maps an aggregate leaf to the
    DAX name of a base measure present in the visual (``datasource`` is ``None`` for an unqualified
    field, else the secondary/blend source caption -- Feature B territory, left to the caller).
    ``resolve_reference(name, datasource) -> ("calc"|"measure", ref) | None`` classifies a bare
    field reference: another table-calc field (a nested Visual Calculation, referenced by name and
    recorded as a dependency) or a plain base measure. ``axis`` is the Visual-Calculation axis the
    running functions accumulate along (``ROWS`` for a table with row dimensions).
    """

    def __init__(self, toks, axis, resolve_aggregate, resolve_reference):
        self.toks = toks
        self.pos = 0
        self.axis = axis
        self.resolve_aggregate = resolve_aggregate
        self.resolve_reference = resolve_reference
        self.deps: List[str] = []      # names of referenced calc fields, in encounter order

    # -- token cursor --
    def _peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def _next(self):
        t = self._peek()
        self.pos += 1
        return t

    def _expect_op(self, ch):
        t = self._next()
        if t != ("op", ch):
            raise _CompileError(f"expected {ch!r}")

    # -- grammar: expr := term (('+'|'-') term)* ; term := factor (('*'|'/') factor)* --
    def parse(self) -> str:
        expr = self._expr()
        if self.pos != len(self.toks):
            raise _CompileError("trailing tokens after expression")
        return expr

    def _expr(self) -> str:
        out = self._term()
        while self._peek() in (("op", "+"), ("op", "-")):
            op = self._next()[1]
            out = f"{out} {op} {self._term()}"
        return out

    def _term(self) -> str:
        out = self._factor()
        while self._peek() in (("op", "*"), ("op", "/")):
            op = self._next()[1]
            out = f"{out} {op} {self._factor()}"
        return out

    def _factor(self) -> str:
        tok = self._peek()
        if tok == ("op", "-"):
            self._next()
            return f"- {self._factor()}"
        if tok == ("op", "("):
            self._next()
            inner = self._expr()
            self._expect_op(")")
            return f"({inner})"
        kind, val = tok
        if kind == "num":
            self._next()
            return f"0{val}" if val.startswith(".") else val
        if kind == "field":
            self._next()
            return self._reference(val)
        if kind == "id":
            self._next()
            return self._call(val)
        raise _CompileError("expected a value")

    # -- a bare [field] / [ds].[field] reference: another calc (nested VC) or a base measure --
    def _reference(self, parts: List[str]) -> str:
        datasource, name = (None, parts[0]) if len(parts) == 1 else (parts[0], parts[-1])
        res = self.resolve_reference(name, datasource)
        if not res:
            raise _CompileError(f"unresolved field reference [{name}]")
        kind, ref = res
        if kind == "calc":
            if ref not in self.deps:
                self.deps.append(ref)
        elif kind != "measure":
            raise _CompileError(f"reference [{name}] resolved to unknown kind {kind!r}")
        return f"[{ref}]"

    # -- a function call: table calc, aggregate, or ATTR passthrough --
    def _call(self, fn_raw: str) -> str:
        fn = fn_raw.upper()
        self._expect_op("(")
        if fn in _AGG:
            leaf = self._aggregate_leaf(fn)
            self._expect_op(")")
            return leaf
        if fn == _ATTR:
            inner = self._single_reference_arg()
            self._expect_op(")")
            return inner
        if fn in _RUNNING:
            arg = self._single_column_arg(fn)
            if self._peek() != ("op", ")"):
                raise _CompileError(f"{fn} argument must be a single aggregate or field reference")
            self._next()
            return f"{_RUNNING[fn]}({arg}, {self.axis})"
        if fn in _RANK:
            arg = self._single_column_arg(fn)
            direction = "DESC"
            if self._peek() not in (("op", ","), ("op", ")")):
                raise _CompileError(f"{fn} argument must be a single aggregate or field reference")
            if self._peek() == ("op", ","):
                self._next()
                dtok = self._next()
                if dtok[0] != "str" or dtok[1].lower() not in _RANK_DIR:
                    raise _CompileError("RANK direction must be 'asc' or 'desc'")
                direction = _RANK_DIR[dtok[1].lower()]
            self._expect_op(")")
            return f"RANK({_RANK[fn]}, ORDERBY({arg}, {direction}))"
        raise _CompileError(f"unsupported function {fn_raw}")

    # -- SUM([field]) etc: the aggregate wraps exactly one column, resolved to a base measure --
    def _aggregate_leaf(self, agg: str) -> str:
        tok = self._next()
        if tok[0] != "field":
            raise _CompileError(f"{agg} expects a single column argument")
        parts = tok[1]
        datasource, field = (None, parts[0]) if len(parts) == 1 else (parts[0], parts[-1])
        base = self.resolve_aggregate(agg, field, datasource)
        if not base:
            raise _CompileError(f"unresolved aggregate {agg}([{field}])")
        return f"[{base}]"

    # -- the argument of RUNNING_SUM / RANK: exactly one aggregate leaf or one field reference --
    def _single_column_arg(self, fn: str) -> str:
        kind, val = self._peek()
        if kind == "id" and val.upper() in _AGG:
            self._next()
            self._expect_op("(")
            leaf = self._aggregate_leaf(val.upper())
            self._expect_op(")")
            return leaf
        if kind == "id" and val.upper() == _ATTR:
            self._next()
            self._expect_op("(")
            inner = self._single_reference_arg()
            self._expect_op(")")
            return inner
        if kind == "field":
            self._next()
            return self._reference(val)
        raise _CompileError(f"{fn} argument must be a single aggregate or field reference")

    def _single_reference_arg(self) -> str:
        tok = self._next()
        if tok[0] != "field":
            raise _CompileError("expected a single field reference")
        return self._reference(tok[1])


def compile_expression(
    formula: str,
    *,
    axis: str,
    resolve_aggregate: Callable[[str, str, Optional[str]], Optional[str]],
    resolve_reference: Callable[[str, Optional[str]], Optional[Tuple[str, str]]],
) -> Tuple[Optional[str], List[str], Optional[str]]:
    """Compile one formula into ``(dax_expression, dependency_calc_names, None)`` or
    ``(None, [], reason)`` when it leaves the faithful subset."""
    try:
        toks = _tokenize(formula)
        if not toks:
            return None, [], "empty formula"
        c = _Compiler(toks, axis, resolve_aggregate, resolve_reference)
        expr = c.parse()
        return expr, c.deps, None
    except _CompileError as e:
        return None, [], str(e)


# -- chain assembler -----------------------------------------------------------
def rename_calc_references(formula: str, id_to_name: Dict[str, str]) -> str:
    """Rewrite ``[<calc_field_id>]`` tokens in a Tableau formula to ``[<display name>]``.

    Tableau formulas reference a calc field by its opaque ``Calculation_<n>`` id; a faithful Visual
    Calculation references its siblings by their **display name**. Only bracket tokens whose inner
    text is exactly a known id are rewritten -- base-column refs (``[Sales]``) and datasource-qualified
    refs (``[ds].[Sales]``) are left untouched -- so the compiler can key the chain by human names.
    """
    out = formula
    for cid, name in id_to_name.items():
        if cid and name and cid != name:
            out = re.sub(r"\[" + re.escape(cid) + r"\]", "[" + name + "]", out)
    return out


def compile_chain(
    entry: str,
    calc_formulas: Dict[str, str],
    *,
    axis: str = "ROWS",
    resolve_aggregate: Callable[[str, str, Optional[str]], Optional[str]],
    resolve_measure: Optional[Callable[[str, Optional[str]], Optional[str]]] = None,
    summaries: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[List[VisualCalcDef]], Optional[str]]:
    """Compile a displayed table-calc field and every calc field it (transitively) references into
    an ordered list of :class:`~visual_calc_emitter.VisualCalcDef` -- **inner calcs first, hidden;
    the displayed ``entry`` last, shown** -- or ``(None, reason)`` if any part leaves the subset.

    ``calc_formulas`` maps calc-field name -> Tableau formula for *all* table-calc fields in scope;
    a reference to a key here is a nested Visual Calculation, a reference ``resolve_measure`` can map
    is a base measure, anything else fails closed. ``summaries`` optionally carries each calc's
    original formula text for the provenance annotation. Cycles fail closed.
    """
    summaries = summaries or {}
    resolve_measure = resolve_measure or (lambda name, ds: None)

    def _resolve_reference(name: str, ds: Optional[str]):
        if ds is None and name in calc_formulas:
            return ("calc", name)
        base = resolve_measure(name, ds)
        return ("measure", base) if base else None

    order: List[str] = []          # post-order: dependencies before dependents
    compiled: Dict[str, str] = {}
    visiting: set = set()

    def _visit(name: str) -> Optional[str]:
        if name in compiled:
            return None
        if name in visiting:
            return f"calc reference cycle through [{name}]"
        if name not in calc_formulas:
            return f"referenced calc [{name}] has no formula in scope"
        visiting.add(name)
        expr, deps, reason = compile_expression(
            calc_formulas[name], axis=axis,
            resolve_aggregate=resolve_aggregate, resolve_reference=_resolve_reference)
        if expr is None:
            return f"[{name}]: {reason}"
        for dep in deps:
            r = _visit(dep)
            if r:
                return r
        visiting.discard(name)
        compiled[name] = expr
        order.append(name)
        return None

    reason = _visit(entry)
    if reason:
        return None, reason

    defs: List[VisualCalcDef] = []
    for name in order:
        is_entry = name == entry
        defs.append(VisualCalcDef(
            name=name,
            expression=compiled[name],
            hidden=not is_entry,
            is_inner=not is_entry,
            role="value",
            family="FORMULA_TABLE_CALC",
            tableau_summary=summaries.get(name, ""),
        ))
    return defs, None
