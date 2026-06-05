"""Deterministic Tableau calculated-field -> DAX measure translator (no LLM).

Originated from the Tableau-Fabric-AI-Bridge Play 4 notebook (cell 3b) as an
aggregation+arithmetic-only safe subset, then extended in-place into a typed
recursive-descent parser that also covers conditional and null-handling logic.

Translates a SAFE subset of Tableau calculated fields into working DAX measures:
  * aggregations over a single bare field: SUM, AVG, MIN, MAX, COUNT, COUNTD, MEDIAN,
    STDEV/STDEVP (-> STDEV.S/STDEV.P), VAR/VARP (-> VAR.S/VAR.P), PERCENTILE([f], n)
    (-> PERCENTILE.INC)
  * arithmetic between those terms / numeric literals: + - * /, parentheses, unary minus
  * conditional logic: IF/THEN/ELSEIF/ELSE/END and IIF(cond, a, b)
  * CASE/WHEN -> SWITCH: searched form CASE WHEN c THEN r ... [ELSE z] END ->
    SWITCH(TRUE(), c, r, ..., z) and simple form CASE e WHEN v THEN r ... [ELSE z] END ->
    SWITCH(e, v, r, ..., z); measure-context-safe only (the comparand, values, and a single
    consistent result type must be aggregations or literals)
  * scalar math over NUMERIC (aggregated) operands: ABS, ROUND (1-arg -> ROUND(x, 0)),
    CEILING(x) -> CEILING(x, 1), FLOOR(x) -> FLOOR(x, 1), POWER, SQRT, SQUARE(x) ->
    POWER(x, 2), SIGN, EXP, LOG (base-10, or 2-arg LOG(x, base)), LN, DIV(a, b) ->
    QUOTIENT(a, b), PI(), and the trig family SIN/COS/TAN/ASIN/ACOS/ATAN/COT
  * comparison operators: = == <> != > >= < <=  (== -> = ; != -> <>)
  * boolean logic: AND -> && , OR -> || , NOT(x)
  * null handling: ZN(x) -> COALESCE(x, 0) ; IFNULL(a, b) -> COALESCE(a, b) ;
    ISNULL(x) -> ISBLANK(x)
  * string literals "..." / '...'
  * FIXED level-of-detail expressions wrapped in an outer aggregation:
    AGG({FIXED d1,d2,...: inner}) -> AGG_X(SUMMARIZE('T', 'T'[d1], ...), CALCULATE(inner))
    with SUM->SUMX, AVG->AVERAGEX, MIN->MINX, MAX->MAXX, MEDIAN->MEDIANX, COUNT->COUNTAX.
    Nested FIXED LODs translate only when each inner FIXED's dimension set is a SUPERSET of
    the enclosing FIXED's dimensions (otherwise the context-transition emit could silently
    compute the wrong number, so it falls back). INCLUDE/EXCLUDE, zero-dimension (grand-total)
    LODs, COUNTD over an LOD, and a bare LOD not wrapped in an outer aggregation all fall back.

MEASURE-CONTEXT INVARIANT: the default entry point (translate_tableau_calc_to_dax) emits a
DAX *measure*, so every leaf operand must be an aggregation or a literal. A bare row-level field
(e.g. ``[Sales]`` outside an aggregation) is invalid in a measure and deterministically FALLS
BACK. The parser also tracks a static data type per node (number / text / date / bool) and falls
back on any type mismatch (e.g. an IF whose branches return different types, an arithmetic op on
a non-numeric term, or a comparison between incomparable types) so it never emits DAX that would
error or silently coerce.

ROW-LEVEL (CALCULATED-COLUMN) COMPANION: translate_tableau_calc_to_column_dax shares the same
public shape but parses in row context (mode="column"): a bare ``[field]`` resolves to
``'Table'[Col]`` and the row-level string / date / numeric-cast functions become available
(LEN/LEFT/RIGHT/MID/UPPER/LOWER/REPLACE/CONTAINS/STARTSWITH/ENDSWITH/FIND; YEAR/MONTH/DAY/TODAY/
NOW/DATEPART/DATEADD/DATEDIFF/DATETRUNC/DATE/MAKEDATE; INT/FLOAT; string ``+`` -> null-preserving
concatenation). Aggregations, PERCENTILE, and LODs are invalid there and fall back. Mappings whose
DAX equivalent is NOT faithful are deliberately left to fall back: TRIM/LTRIM/RTRIM (DAX TRIM also
collapses internal whitespace), SPLIT (no general DAX form), STR and DATE(text) (culture-sensitive
formatting/parsing), and the start-of-week-dependent DATEPART('week'/'weekday')/DATEDIFF('week').

Anything outside this subset (INCLUDE/EXCLUDE LODs, table calcs WINDOW_/RUNNING_/RANK/LOOKUP/
INDEX/TOTAL, scalar date/string/regex functions, row-level operands inside a scalar math
function or CASE, nested arithmetic inside an aggregation, 4-arg IIF, references to other
calcs, unresolved or ambiguous fields, cross-table terms) deterministically FALLS BACK by
returning ``None`` so the caller keeps an inert ``= 0`` stub.
The original Tableau formula is preserved as a ``TableauFormula`` annotation by the renderer
either way.

Table calculations are translated by a SEPARATE seam, translate_tableau_table_calc_to_dax, because
their result depends on the worksheet's Compute-Using / addressing / sort, which lives in the
workbook (``.twb``), not the datasource (``.tds``) this module parses. That entry point therefore
takes the partition/order spec explicitly and emits the modern-DAX window-function pattern
(INDEX -> ROWNUMBER; RUNNING_*/WINDOW_* -> WINDOW; LOOKUP -> OFFSET); the orchestrator/viz layer
supplies the real addressing once worksheets are parsed. FIXED LODs, by contrast, are
datasource-level semantics and are translated inline by the measure path above.

Known semantic notes:
  * Emitted comparison/arithmetic operators follow DAX's BLANK coercion (an empty aggregation
    behaves as 0/"" in an operator), which differs from Tableau's three-valued NULL logic in the
    edge case of a fully-empty aggregation.
  * A FIXED LOD's SUMMARIZE/CALCULATE form respects ALL current Power BI filter context, whereas
    Tableau FIXED ignores view dimension filters (it respects only context filters). The two
    agree at a measure total and under context filters, but can diverge under a viz dimension
    filter. This matches the universal Tableau->DAX FIXED mapping.
These translated measures are flagged (TranslatedBy) and are exactly what the live
value-reconciliation step verifies.

Prior art: the breadth of Tableau->DAX construct mappings was informed by surveying the
MIT-licensed ``cyphou/Tableau-To-PowerBI`` project. No third-party code is vendored here; only
the (non-copyrightable) language-to-language equivalences were used. This module is an
independent recursive-descent implementation. See THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import re

_AGG_MAP = {
    "SUM": "SUM", "AVG": "AVERAGE", "MIN": "MIN", "MAX": "MAX",
    "MEDIAN": "MEDIAN", "COUNT": "COUNTA", "COUNTD": "DISTINCTCOUNTNOBLANK",
    "STDEV": "STDEV.S", "STDEVP": "STDEV.P", "VAR": "VAR.S", "VARP": "VAR.P",
}
# COUNT  -> COUNTA               (Tableau COUNT = non-null of ANY type; DAX COUNT errors on text)
# COUNTD -> DISTINCTCOUNTNOBLANK (plain DISTINCTCOUNT counts BLANK -> off-by-one vs Tableau)
# STDEV/VAR  -> STDEV.S/VAR.S    (Tableau STDEV/VAR are the SAMPLE statistics)
# STDEVP/VARP-> STDEV.P/VAR.P    (the POPULATION statistics)

# Aggregations that require a NUMERIC column (emit DAX that errors on text/date otherwise).
_NUMERIC_ONLY_AGGS = {"SUM", "AVG", "MEDIAN", "STDEV", "STDEVP", "VAR", "VARP"}

# Outer aggregation -> DAX iterator used to RE-AGGREGATE a FIXED LOD over its own grain:
# SUMMARIZE materializes the LOD grain, CALCULATE re-enters row context for the inner measure.
# COUNT -> COUNTAX (counts non-blank scalars of any type, parity with Tableau COUNT). COUNTD is
# intentionally absent so a distinct re-aggregation of an LOD falls back rather than mis-emit.
_AGG_X = {
    "SUM": "SUMX", "AVG": "AVERAGEX", "MIN": "MINX", "MAX": "MAXX",
    "MEDIAN": "MEDIANX", "COUNT": "COUNTAX",
}
_NUMERIC_TYPES = {"int64", "double", "decimal"}

# Scalar math functions that wrap a NUMERIC (aggregated) operand and stay valid in a measure
# (they compose with the existing arithmetic). Operand(s) must be numeric or the whole calc
# falls back. Most Tableau math names map identically to DAX, so we re-emit the (uppercased)
# name; the handful that don't are listed explicitly below.
#   _MATH_1     : single numeric operand -> FN(x). Includes the trig family; LN is natural log.
#   _MATH_1_SIG : single numeric operand -> FN(x, <significance>). Tableau CEILING/FLOOR take
#                 one argument (round to the nearest integer); DAX requires a significance step.
#   _MATH_2     : two numeric operands -> DAXNAME(a, b). Tableau DIV (integer division) maps to
#                 DAX QUOTIENT; POWER and MOD are identical.
# Functions with their own arity/shape are handled directly in _scalar_fn: ROUND (1-or-2 arg),
# LOG (1-arg base-10 or 2-arg LOG(x, base)), SQUARE(x) -> POWER(x, 2), and PI() (nullary).
_MATH_1 = {
    "ABS", "SQRT", "SIGN", "EXP", "LN",
    "SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN", "COT",
}
_MATH_1_SIG = {"CEILING": "1", "FLOOR": "1"}
_MATH_2 = {"POWER": "POWER", "DIV": "QUOTIENT", "MOD": "MOD"}
_SCALAR_MATH = _MATH_1 | set(_MATH_1_SIG) | set(_MATH_2) | {"ROUND", "LOG", "SQUARE", "PI"}

# ---------------------------------------------------------------------------
# Row-level (calculated-COLUMN) context. The functions below are NOT valid in a
# measure: they operate on a bare row-level field, so they are reachable only via
# translate_tableau_calc_to_column_dax (mode="column"), where a [field] token
# resolves to 'Table'[Col] instead of falling back. Mappings are built from the
# Tableau function reference and the DAX function reference; anything whose DAX
# equivalent is not faithful (collapses internal spaces, is culture-sensitive, or
# depends on a workbook start-of-week setting) is deliberately left to fall back.
# ---------------------------------------------------------------------------
# Map a TMDL/storage data type to this parser's static dtype.
_DTYPE_BY_TMDL = {
    "string": "text",
    "int64": "number", "double": "number", "decimal": "number",
    "dateTime": "date", "date": "date",
    "boolean": "bool",
}
_STRING_FNS = {
    "LEN", "UPPER", "LOWER", "LEFT", "RIGHT", "MID",
    "REPLACE", "CONTAINS", "STARTSWITH", "ENDSWITH", "FIND",
}
_DATE_FNS = {
    "YEAR", "MONTH", "DAY", "TODAY", "NOW",
    "DATEPART", "DATEADD", "DATEDIFF", "DATETRUNC", "DATE", "MAKEDATE",
}
_CAST_FNS = {"INT", "FLOAT"}
_COLUMN_ONLY_FNS = _STRING_FNS | _DATE_FNS | _CAST_FNS
# DATEPART(part, d) -> scalar DAX extractor. 'week'/'weekday' omitted on purpose:
# their result depends on the workbook's start-of-week, so they fall back.
_DATEPART_FN = {
    "year": "YEAR", "month": "MONTH", "day": "DAY",
    "hour": "HOUR", "minute": "MINUTE", "second": "SECOND", "quarter": "QUARTER",
}
# DATEDIFF('part', d1, d2) -> DAX DATEDIFF(d1, d2, UNIT). 'week' omitted (start-of-week).
_DATEDIFF_UNITS = {
    "day": "DAY", "month": "MONTH", "year": "YEAR", "quarter": "QUARTER",
    "hour": "HOUR", "minute": "MINUTE", "second": "SECOND",
}

# ---------------------------------------------------------------------------
# Table calculations (translate_tableau_table_calc_to_dax). These depend on the
# worksheet's addressing (Compute-Using partition + sort), which lives in the .twb,
# NOT the .tds. So this is a SEAM: the caller passes the partition/order spec
# explicitly and we emit the modern-DAX window-function pattern. Each window/offset
# function omits its <relation> argument, which per the DAX spec defaults to
# ALLSELECTED() of the ORDERBY()/PARTITIONBY() columns -- the standard measure form.
# A RUNNING_/WINDOW_ aggregate is re-evaluated per addressed row via CALCULATE (context
# transition) and folded with the matching iterator, mirroring the FIXED-LOD pattern.
# ---------------------------------------------------------------------------
_TABLECALC_X = {            # RUNNING_*: partition start -> current row
    "RUNNING_SUM": "SUMX", "RUNNING_AVG": "AVERAGEX",
    "RUNNING_MIN": "MINX", "RUNNING_MAX": "MAXX",
}
_TABLECALC_WINDOW_X = {     # WINDOW_*: entire partition (first -> last row)
    "WINDOW_SUM": "SUMX", "WINDOW_AVG": "AVERAGEX",
    "WINDOW_MIN": "MINX", "WINDOW_MAX": "MAXX",
}
_TABLE_CALCS = {"INDEX", "LOOKUP"} | set(_TABLECALC_X) | set(_TABLECALC_WINDOW_X)


class _CalcError(Exception):
    """Raised on any construct outside the supported subset -> caller falls back."""


def _dax_table(name):
    # DAX table reference: single-quoted, embedded single quotes doubled.
    return "'" + name.replace("'", "''") + "'"


def _dax_col(name):
    # DAX column reference: [bracketed], embedded ] doubled.
    return "[" + name.replace("]", "]]") + "]"


def _norm_number(tok):
    # .5 -> 0.5 ; 1. -> 1.0 (DAX dislikes a bare leading/trailing dot)
    if tok.startswith("."):
        tok = "0" + tok
    if tok.endswith("."):
        tok = tok + "0"
    return tok


_NUM_RE = re.compile(r"\d+\.?\d*|\.\d+")
_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Comparison operators, longest first so '<=' wins over '<'. '==' and '!=' are
# normalized to their DAX spellings ('=' and '<>').
_CMP_2 = {"<=": "<=", ">=": ">=", "<>": "<>", "==": "=", "!=": "<>"}
_CMP_1 = {"<": "<", ">": ">", "=": "="}


def _dax_string(value):
    # DAX string literal: double-quoted, embedded double quotes doubled.
    return '"' + value.replace('"', '""') + '"'


def _tokenize(formula):
    s = formula or ""
    i, n = 0, len(s)
    toks = []
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "[":
            j = s.find("]", i + 1)
            if j == -1:
                raise _CalcError("unterminated field reference")
            toks.append(("field", s[i + 1:j]))
            i = j + 1
            continue
        if c == '"' or c == "'":
            j = s.find(c, i + 1)
            if j == -1:
                raise _CalcError("unterminated string literal")
            inner = s[i + 1:j]
            if "\\" in inner:
                # Backslash escapes are ambiguous to map safely -> fall back.
                raise _CalcError("string literal with escape not supported")
            toks.append(("str", inner))
            i = j + 1
            continue
        two = s[i:i + 2]
        if two in _CMP_2:
            toks.append(("cmp", _CMP_2[two]))
            i += 2
            continue
        if c in _CMP_1:
            toks.append(("cmp", _CMP_1[c]))
            i += 1
            continue
        if c in "+-*/(),{}:":
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
        raise _CalcError(f"unsupported character {c!r}")
    return toks


# Recursive-descent parser. Each production returns a (text, dtype) node where dtype
# is one of: "number", "text", "date", "bool". Precedence (low -> high):
#   expr   := if | or
#   if     := IF or THEN expr (ELSEIF or THEN expr)* [ELSE expr] END
#   or     := and (OR and)*            ; and := not (AND not)*
#   not    := NOT not | cmp
#   cmp    := add (CMP add)?           ; add := mul (('+'|'-') mul)*
#   mul    := unary (('*'|'/') unary)* ; unary := '-' unary | primary
#   primary:= agg | number | string | IIF(...) | ZN(...) | IFNULL(...) | ISNULL(...) | '(' expr ')'
#   agg    := AGGFUNC '(' '[' fieldref ']' ')'
# The measure-context invariant is enforced structurally: a bare [field] only ever
# appears inside agg, so a row-level field reference is a parse error (-> fallback).
class _Parser:
    def __init__(self, toks, resolver, tables_used, mode="measure"):
        self.toks = toks
        self.pos = 0
        self.resolver = resolver
        self.tables_used = tables_used
        self.mode = mode          # "measure" (default) or "column" (row-level)
        self._lod_dim_stack = []

    def _peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def _next(self):
        t = self._peek()
        self.pos += 1
        return t

    def _expect_op(self, ch):
        k, v = self._peek()
        if k != "op" or v != ch:
            raise _CalcError(f"expected {ch!r}")
        self.pos += 1

    def _is_kw(self, kw):
        k, v = self._peek()
        return k == "id" and v.upper() == kw

    def _expect_kw(self, kw):
        if not self._is_kw(kw):
            raise _CalcError(f"expected {kw}")
        self._next()

    @staticmethod
    def _expect_bool(node):
        if node[1] != "bool":
            raise _CalcError("expected a boolean expression")
        return node

    @staticmethod
    def _expect_number(node):
        if node[1] != "number":
            raise _CalcError("expected a numeric expression")
        return node

    @staticmethod
    def _expect_text(node):
        if node[1] != "text":
            raise _CalcError("expected a text expression")
        return node

    @staticmethod
    def _expect_date(node):
        if node[1] != "date":
            raise _CalcError("expected a date expression")
        return node

    def parse(self):
        node = self._expr()
        if self.pos != len(self.toks):
            raise _CalcError("unexpected trailing tokens")
        return node

    def _expr(self):
        if self._is_kw("IF"):
            return self._if()
        if self._is_kw("CASE"):
            return self._case()
        return self._or()

    def _if(self):
        self._next()  # IF
        branches = []
        cond = self._expect_bool(self._or())
        self._expect_kw("THEN")
        branches.append((cond, self._expr()))
        while self._is_kw("ELSEIF"):
            self._next()
            c = self._expect_bool(self._or())
            self._expect_kw("THEN")
            branches.append((c, self._expr()))
        else_node = None
        if self._is_kw("ELSE"):
            self._next()
            else_node = self._expr()
        self._expect_kw("END")
        # All THEN/ELSE branches must return the same data type (DAX requires a single
        # return type; mixed number/text/bool would error or silently coerce).
        dtype = branches[0][1][1]
        for _, then in branches:
            if then[1] != dtype:
                raise _CalcError("IF branches return inconsistent types")
        if else_node is not None and else_node[1] != dtype:
            raise _CalcError("IF/ELSE branches return inconsistent types")
        # Fold ELSEIF chain into nested DAX IF, inside-out. No ELSE -> 2-arg IF (BLANK
        # when unmatched), matching Tableau's null result for an unmatched IF.
        inner = else_node
        for cond, then in reversed(branches):
            text = f"IF({cond[0]}, {then[0]})" if inner is None else f"IF({cond[0]}, {then[0]}, {inner[0]})"
            inner = (text, dtype)
        return inner

    def _or(self):
        left = self._and()
        while self._is_kw("OR"):
            self._next()
            right = self._and()
            self._expect_bool(left)
            self._expect_bool(right)
            left = (f"{left[0]} || {right[0]}", "bool")
        return left

    def _and(self):
        left = self._not()
        while self._is_kw("AND"):
            self._next()
            right = self._not()
            self._expect_bool(left)
            self._expect_bool(right)
            left = (f"{left[0]} && {right[0]}", "bool")
        return left

    def _not(self):
        if self._is_kw("NOT"):
            self._next()
            operand = self._expect_bool(self._not())
            return (f"NOT({operand[0]})", "bool")
        return self._cmp()

    def _cmp(self):
        left = self._add()
        k, v = self._peek()
        if k == "cmp":
            self._next()
            right = self._add()
            # Only compare like, ordered/equatable types; never two booleans.
            if left[1] != right[1] or left[1] == "bool":
                raise _CalcError("incomparable types in comparison")
            return (f"{left[0]} {v} {right[0]}", "bool")
        return left

    def _add(self):
        left = self._mul()
        while self._peek() == ("op", "+") or self._peek() == ("op", "-"):
            op = self._next()[1]
            right = self._mul()
            if op == "+" and self.mode == "column" and left[1] == "text" and right[1] == "text":
                # Tableau '+' concatenates strings and PROPAGATES null; DAX '&' coerces a
                # BLANK operand to "", so wrap to keep Tableau's null-propagating semantics.
                left = (
                    f"IF(ISBLANK({left[0]}) || ISBLANK({right[0]}), BLANK(), {left[0]} & {right[0]})",
                    "text",
                )
                continue
            self._expect_number(left)
            self._expect_number(right)
            left = (f"{left[0]} {op} {right[0]}", "number")
        return left

    def _mul(self):
        left = self._unary()
        while self._peek() == ("op", "*") or self._peek() == ("op", "/"):
            op = self._next()[1]
            right = self._unary()
            self._expect_number(left)
            self._expect_number(right)
            if op == "/":
                left = (f"DIVIDE({left[0]}, {right[0]})", "number")
            else:
                left = (f"{left[0]} * {right[0]}", "number")
        return left

    def _unary(self):
        if self._peek() == ("op", "-"):
            self._next()
            operand = self._expect_number(self._unary())
            return (f"-({operand[0]})", "number")  # parenthesize so '--' never forms a DAX comment
        return self._primary()

    def _primary(self):
        k, v = self._peek()
        if k == "num":
            self._next()
            return (_norm_number(v), "number")
        if k == "str":
            self._next()
            return (_dax_string(v), "text")
        if k == "op" and v == "(":
            self._next()
            inner = self._expr()
            self._expect_op(")")
            return (f"({inner[0]})", inner[1])
        if k == "op" and v == "{":
            if self.mode == "column":
                raise _CalcError("LOD expression not valid in a row-level column calc")
            return self._fixed_lod_bare()
        if k == "id":
            u = v.upper()
            if u in _AGG_MAP:
                if self.mode == "column":
                    raise _CalcError(f"aggregation {u} not valid in a row-level column calc")
                return self._agg()
            if u == "PERCENTILE":
                if self.mode == "column":
                    raise _CalcError("PERCENTILE not valid in a row-level column calc")
                return self._percentile()
            if u == "IIF":
                return self._iif()
            if u == "ZN":
                return self._zn()
            if u == "IFNULL":
                return self._ifnull()
            if u == "ISNULL":
                return self._isnull()
            if u in _SCALAR_MATH:
                return self._scalar_fn(u)
            if self.mode == "column" and u in _COLUMN_ONLY_FNS:
                return self._row_fn(u)
            raise _CalcError(f"unsupported function {v}")
        if k == "field":
            if self.mode == "column":
                return self._row_field()
            raise _CalcError("bare row-level field [..] not valid in a measure")
        raise _CalcError("expected a value")

    def _iif(self):
        self._next()  # IIF
        self._expect_op("(")
        cond = self._expect_bool(self._expr())
        self._expect_op(",")
        a = self._expr()
        self._expect_op(",")
        b = self._expr()
        if self._peek() == ("op", ","):
            raise _CalcError("4-arg IIF (unknown branch) not supported")
        self._expect_op(")")
        if a[1] != b[1]:
            raise _CalcError("IIF branches return inconsistent types")
        return (f"IF({cond[0]}, {a[0]}, {b[0]})", a[1])

    def _zn(self):
        self._next()  # ZN
        self._expect_op("(")
        x = self._expect_number(self._expr())
        self._expect_op(")")
        return (f"COALESCE({x[0]}, 0)", "number")

    def _ifnull(self):
        self._next()  # IFNULL
        self._expect_op("(")
        a = self._expr()
        self._expect_op(",")
        b = self._expr()
        self._expect_op(")")
        if a[1] != b[1]:
            raise _CalcError("IFNULL arguments return inconsistent types")
        return (f"COALESCE({a[0]}, {b[0]})", a[1])

    def _isnull(self):
        self._next()  # ISNULL
        self._expect_op("(")
        x = self._expr()
        self._expect_op(")")
        return (f"ISBLANK({x[0]})", "bool")

    def _scalar_fn(self, name):
        # Scalar math over a NUMERIC (aggregated) operand. Each operand is parsed as a full
        # expression but must be numeric: a bare row-level [field] (parse error in a measure),
        # a text/date operand, or wrong arity all raise -> the whole calc falls back.
        self._next()  # function name
        self._expect_op("(")
        if name == "PI":
            # Nullary numeric constant; PI() composes with aggregates (e.g. SUM([x]) * PI()).
            self._expect_op(")")
            return ("PI()", "number")
        x = self._expect_number(self._expr())
        if name in _MATH_1:
            self._expect_op(")")
            return (f"{name}({x[0]})", "number")
        if name in _MATH_1_SIG:
            # DAX CEILING/FLOOR need a significance; Tableau's 1-arg form rounds to the integer.
            self._expect_op(")")
            return (f"{name}({x[0]}, {_MATH_1_SIG[name]})", "number")
        if name == "SQUARE":
            # DAX has no SQUARE; x squared is POWER(x, 2).
            self._expect_op(")")
            return (f"POWER({x[0]}, 2)", "number")
        if name == "ROUND":
            # Tableau ROUND(x) -> DAX ROUND(x, 0); ROUND(x, n) passes the digit count through.
            if self._peek() == ("op", ","):
                self._next()
                digits = self._expect_number(self._expr())
                self._expect_op(")")
                return (f"ROUND({x[0]}, {digits[0]})", "number")
            self._expect_op(")")
            return (f"ROUND({x[0]}, 0)", "number")
        if name == "LOG":
            # Tableau LOG(x) is base 10 (so is DAX LOG(x)); LOG(x, base) passes the base through.
            if self._peek() == ("op", ","):
                self._next()
                base = self._expect_number(self._expr())
                self._expect_op(")")
                return (f"LOG({x[0]}, {base[0]})", "number")
            self._expect_op(")")
            return (f"LOG({x[0]})", "number")
        # Two-operand numeric functions: POWER(x, n) and DIV(a, b) -> QUOTIENT(a, b).
        self._expect_op(",")
        second = self._expect_number(self._expr())
        self._expect_op(")")
        return (f"{_MATH_2[name]}({x[0]}, {second[0]})", "number")

    def _case(self):
        # CASE/WHEN -> DAX SWITCH. Parsed at expression-statement level (like IF) so the END
        # self-terminates the construct and it never composes into arithmetic (which would
        # otherwise expose DAX's BLANK coercion on an unmatched no-ELSE CASE).
        self._next()  # CASE
        if self._is_kw("WHEN"):
            return self._case_searched()
        return self._case_simple()

    def _case_searched(self):
        # CASE WHEN c1 THEN r1 ... [ELSE z] END  ->  SWITCH(TRUE(), c1, r1, ..., z)
        pairs = []
        while self._is_kw("WHEN"):
            self._next()
            cond = self._expect_bool(self._or())
            self._expect_kw("THEN")
            pairs.append((cond[0], self._expr()))
        return self._switch_emit("TRUE()", pairs)

    def _case_simple(self):
        # CASE e WHEN v1 THEN r1 ... [ELSE z] END  ->  SWITCH(e, v1, r1, ..., z)
        # e and every v must be aggregations/literals of one consistent type (a bare row-level
        # comparand like CASE [Region] WHEN ... is a parse error -> falls back).
        comparand = self._or()
        pairs = []
        while self._is_kw("WHEN"):
            self._next()
            value = self._or()
            if value[1] != comparand[1]:
                raise _CalcError("CASE WHEN value type does not match the CASE expression")
            self._expect_kw("THEN")
            pairs.append((value[0], self._expr()))
        return self._switch_emit(comparand[0], pairs)

    def _switch_emit(self, head, pairs):
        # Shared tail for both CASE forms: require >=1 WHEN, then a single consistent return type
        # across every THEN branch and the optional ELSE (DAX SWITCH needs one return type; mixed
        # number/text/etc. would error or silently coerce, so fall back instead).
        if not pairs:
            raise _CalcError("CASE requires at least one WHEN")
        else_node = None
        if self._is_kw("ELSE"):
            self._next()
            else_node = self._expr()
        self._expect_kw("END")
        rtype = pairs[0][1][1]
        for _, result in pairs:
            if result[1] != rtype:
                raise _CalcError("CASE results return inconsistent types")
        if else_node is not None and else_node[1] != rtype:
            raise _CalcError("CASE/ELSE results return inconsistent types")
        args = [head]
        for key, result in pairs:
            args.append(key)
            args.append(result[0])
        if else_node is not None:
            args.append(else_node[0])
        return (f"SWITCH({', '.join(args)})", rtype)

    def _agg(self):
        name = self._next()[1].upper()
        if name not in _AGG_MAP:
            raise _CalcError(f"unsupported function {name}")
        self._expect_op("(")
        if self._peek() == ("op", "{"):
            node = self._fixed_lod_reagg(name)
            self._expect_op(")")
            return node
        k, v = self._peek()
        if k != "field":
            raise _CalcError(f"{name} argument must be a single bare [field]")
        self._next()
        self._expect_op(")")
        resolved = self.resolver(v)
        if resolved is None:
            raise _CalcError(f"unresolved/ambiguous field [{v}]")
        table, col, tmdl_type = resolved
        # Reject aggregates invalid for the column's data type (would emit DAX that errors).
        if name in _NUMERIC_ONLY_AGGS and tmdl_type not in _NUMERIC_TYPES:
            raise _CalcError(f"{name} requires a numeric field, got {tmdl_type} for [{v}]")
        if name in ("MIN", "MAX") and tmdl_type not in (_NUMERIC_TYPES | {"dateTime"}):
            raise _CalcError(f"{name} requires a numeric/date field, got {tmdl_type} for [{v}]")
        self.tables_used.add(table)
        if name in ("MIN", "MAX") and tmdl_type == "dateTime":
            dtype = "date"
        else:
            dtype = "number"  # SUM/AVG/MEDIAN/COUNT/COUNTD and numeric MIN/MAX
        return (f"{_AGG_MAP[name]}({_dax_table(table)}{_dax_col(col)})", dtype)

    def _percentile(self):
        # PERCENTILE([field], n) -> PERCENTILE.INC('T'[field], n). Aggregation over a single
        # numeric field; n (the 0..1 fraction) must be numeric. A non-numeric field or a bare
        # row-level / aggregated first argument falls back.
        self._next()  # PERCENTILE
        self._expect_op("(")
        k, v = self._peek()
        if k != "field":
            raise _CalcError("PERCENTILE first argument must be a single bare [field]")
        self._next()
        self._expect_op(",")
        n = self._expect_number(self._expr())
        self._expect_op(")")
        resolved = self.resolver(v)
        if resolved is None:
            raise _CalcError(f"unresolved/ambiguous field [{v}]")
        table, col, tmdl_type = resolved
        if tmdl_type not in _NUMERIC_TYPES:
            raise _CalcError(f"PERCENTILE requires a numeric field, got {tmdl_type} for [{v}]")
        self.tables_used.add(table)
        return (f"PERCENTILE.INC({_dax_table(table)}{_dax_col(col)}, {n[0]})", "number")

    # ----- Row-level (calculated-column) constructs; reachable only in mode="column" -----

    def _row_field(self):
        # A bare [field] in column context resolves to 'Table'[Col] (in measure context this
        # token raises -> fallback). The single table is tracked so the caller can bind the
        # calculated column to it; a row-level calc spanning >1 table falls back upstream.
        _, cap = self._next()
        resolved = self.resolver(cap)
        if resolved is None:
            raise _CalcError(f"unresolved/ambiguous field [{cap}]")
        table, col, tmdl_type = resolved
        dtype = _DTYPE_BY_TMDL.get(tmdl_type)
        if dtype is None:
            raise _CalcError(f"unsupported field type {tmdl_type} for [{cap}]")
        self.tables_used.add(table)
        return (f"{_dax_table(table)}{_dax_col(col)}", dtype)

    def _row_fn(self, name):
        if name in _STRING_FNS:
            return self._string_fn(name)
        if name in _CAST_FNS:
            return self._cast_fn(name)
        return self._date_fn(name)

    def _string_fn(self, name):
        self._next()  # function name
        self._expect_op("(")
        s = self._expect_text(self._expr())
        if name == "LEN":
            self._expect_op(")")
            return (f"LEN({s[0]})", "number")
        if name in ("UPPER", "LOWER"):
            self._expect_op(")")
            return (f"{name}({s[0]})", "text")
        if name in ("LEFT", "RIGHT"):
            self._expect_op(",")
            n = self._expect_number(self._expr())
            self._expect_op(")")
            return (f"{name}({s[0]}, {n[0]})", "text")
        if name == "MID":
            self._expect_op(",")
            start = self._expect_number(self._expr())
            if self._peek() == ("op", ","):
                self._next()
                length = self._expect_number(self._expr())
                self._expect_op(")")
                return (f"MID({s[0]}, {start[0]}, {length[0]})", "text")
            self._expect_op(")")
            # Tableau 2-arg MID runs to the end of the string; DAX MID needs a length.
            return (f"MID({s[0]}, {start[0]}, LEN({s[0]}))", "text")
        if name == "REPLACE":
            self._expect_op(",")
            old = self._expect_text(self._expr())
            self._expect_op(",")
            new = self._expect_text(self._expr())
            self._expect_op(")")
            return (f"SUBSTITUTE({s[0]}, {old[0]}, {new[0]})", "text")
        if name == "CONTAINS":
            self._expect_op(",")
            sub = self._expect_text(self._expr())
            self._expect_op(")")
            # CONTAINSSTRINGEXACT is the case-SENSITIVE form (Tableau CONTAINS is case-sensitive;
            # plain CONTAINSSTRING is case-insensitive and would change results).
            return (f"CONTAINSSTRINGEXACT({s[0]}, {sub[0]})", "bool")
        if name in ("STARTSWITH", "ENDSWITH"):
            self._expect_op(",")
            sub = self._expect_text(self._expr())
            self._expect_op(")")
            side = "LEFT" if name == "STARTSWITH" else "RIGHT"
            # EXACT keeps the prefix/suffix test case-sensitive, matching Tableau.
            return (f"EXACT({side}({s[0]}, LEN({sub[0]})), {sub[0]})", "bool")
        if name == "FIND":
            self._expect_op(",")
            sub = self._expect_text(self._expr())
            start = ("1", "number")
            if self._peek() == ("op", ","):
                self._next()
                start = self._expect_number(self._expr())
            self._expect_op(")")
            # DAX FIND(find, within, start, NotFound) is case-sensitive and returns 0 when the
            # substring is absent -- matching Tableau FIND's case-sensitivity and 0 sentinel.
            return (f"FIND({sub[0]}, {s[0]}, {start[0]}, 0)", "number")
        raise _CalcError(f"unsupported string function {name}")

    def _cast_fn(self, name):
        self._next()  # INT / FLOAT
        self._expect_op("(")
        x = self._expect_number(self._expr())
        self._expect_op(")")
        if name == "INT":
            # Tableau INT truncates toward zero; DAX INT() floors toward -inf, so TRUNC is the
            # faithful mapping (they differ for negative values).
            return (f"TRUNC({x[0]})", "number")
        return (f"CONVERT({x[0]}, DOUBLE)", "number")  # FLOAT

    def _part_literal(self):
        k, v = self._peek()
        if k != "str":
            raise _CalcError("date part must be a string literal")
        self._next()
        return v.lower()

    def _date_fn(self, name):
        self._next()  # function name
        self._expect_op("(")
        if name in ("TODAY", "NOW"):
            self._expect_op(")")
            return (f"{name}()", "date")
        if name in ("YEAR", "MONTH", "DAY"):
            d = self._expect_date(self._expr())
            self._expect_op(")")
            return (f"{name}({d[0]})", "number")
        if name == "DATE":
            # Tableau DATE(x) casts to a date and strips any time-of-day component.
            x = self._expect_date(self._expr())
            self._expect_op(")")
            return (f"DATE(YEAR({x[0]}), MONTH({x[0]}), DAY({x[0]}))", "date")
        if name == "MAKEDATE":
            # Tableau MAKEDATE(year, month, day) -> DAX DATE(year, month, day): an exact,
            # culture-independent mapping (all three operands must be numeric).
            y = self._expect_number(self._expr())
            self._expect_op(",")
            m = self._expect_number(self._expr())
            self._expect_op(",")
            d = self._expect_number(self._expr())
            self._expect_op(")")
            return (f"DATE({y[0]}, {m[0]}, {d[0]})", "date")
        if name == "DATEPART":
            part = self._part_literal()
            self._expect_op(",")
            d = self._expect_date(self._expr())
            self._expect_op(")")
            fn = _DATEPART_FN.get(part)
            if fn is None:
                raise _CalcError(f"unsupported DATEPART part {part!r}")
            return (f"{fn}({d[0]})", "number")
        if name == "DATEADD":
            part = self._part_literal()
            self._expect_op(",")
            n = self._expect_number(self._expr())
            self._expect_op(",")
            d = self._expect_date(self._expr())
            self._expect_op(")")
            return (self._dateadd_emit(part, n[0], d[0]), "date")
        if name == "DATEDIFF":
            part = self._part_literal()
            self._expect_op(",")
            d1 = self._expect_date(self._expr())
            self._expect_op(",")
            d2 = self._expect_date(self._expr())
            self._expect_op(")")
            unit = _DATEDIFF_UNITS.get(part)
            if unit is None:
                raise _CalcError(f"unsupported DATEDIFF part {part!r}")
            # Tableau DATEDIFF('part', start, end) -> DAX DATEDIFF(start, end, UNIT) (args reorder).
            return (f"DATEDIFF({d1[0]}, {d2[0]}, {unit})", "number")
        if name == "DATETRUNC":
            part = self._part_literal()
            self._expect_op(",")
            d = self._expect_date(self._expr())
            self._expect_op(")")
            return (self._datetrunc_emit(part, d[0]), "date")
        raise _CalcError(f"unsupported date function {name}")

    @staticmethod
    def _dateadd_emit(part, n, d):
        # DAX has no scalar DATEADD (the DATEADD function is time-intelligence over a column),
        # so add an interval directly. EDATE handles calendar months; MOD(d, 1) restores the
        # time-of-day that EDATE drops, so a dateTime keeps its time. Result is parenthesized so
        # it composes safely inside a larger expression.
        if part == "day":
            expr = f"{d} + ({n})"
        elif part == "week":
            expr = f"{d} + ({n}) * 7"
        elif part == "hour":
            expr = f"{d} + ({n}) / 24"
        elif part == "minute":
            expr = f"{d} + ({n}) / 1440"
        elif part == "second":
            expr = f"{d} + ({n}) / 86400"
        elif part == "month":
            expr = f"EDATE({d}, {n}) + MOD({d}, 1)"
        elif part == "quarter":
            expr = f"EDATE({d}, ({n}) * 3) + MOD({d}, 1)"
        elif part == "year":
            expr = f"EDATE({d}, ({n}) * 12) + MOD({d}, 1)"
        else:
            raise _CalcError(f"unsupported DATEADD part {part!r}")
        return f"({expr})"

    @staticmethod
    def _datetrunc_emit(part, d):
        # No scalar DATETRUNC in DAX; rebuild the date at the start of the period.
        if part == "day":
            return f"DATE(YEAR({d}), MONTH({d}), DAY({d}))"
        if part == "month":
            return f"DATE(YEAR({d}), MONTH({d}), 1)"
        if part == "year":
            return f"DATE(YEAR({d}), 1, 1)"
        # 'quarter'/'week' need extra arithmetic / a start-of-week setting -> fall back.
        raise _CalcError(f"unsupported DATETRUNC part {part!r}")

    def _lod_core(self):
        # Parse a {FIXED d1, d2, ... : inner} body. Returns (table, [clean_cols], inner_node).
        # Only FIXED is datasource-level and deterministically translatable. INCLUDE/EXCLUDE
        # depend on the view's dimensionality (a worksheet artifact, not in the .tds) -> fall back.
        # Enforces the nested-superset rule: a nested FIXED must fix at least every dimension of
        # the LOD enclosing it; otherwise the emitted context transition could compute a value
        # Tableau never would, so we fall back instead of emitting a confidently-wrong measure.
        self._expect_op("{")
        if not self._is_kw("FIXED"):
            raise _CalcError("only FIXED LOD is translated (INCLUDE/EXCLUDE fall back)")
        self._next()  # FIXED
        cols = []
        table = None
        while True:
            k, v = self._peek()
            if k != "field":
                raise _CalcError("FIXED LOD requires at least one [dimension]")
            self._next()
            resolved = self.resolver(v)
            if resolved is None:
                raise _CalcError(f"unresolved/ambiguous LOD dimension [{v}]")
            t, c, _ty = resolved
            if table is None:
                table = t
            elif t != table:
                raise _CalcError("cross-table FIXED LOD dimensions not supported")
            self.tables_used.add(t)
            cols.append(c)
            if self._peek() == ("op", ","):
                self._next()
                continue
            break
        self._expect_op(":")
        dim_set = frozenset(cols)
        if self._lod_dim_stack and not (dim_set >= self._lod_dim_stack[-1]):
            raise _CalcError("nested FIXED LOD does not fix a superset of the enclosing LOD")
        self._lod_dim_stack.append(dim_set)
        inner = self._expr()
        self._lod_dim_stack.pop()
        self._expect_op("}")
        return table, cols, inner

    def _lod_cols_dax(self, table, cols):
        return ", ".join(_dax_table(table) + _dax_col(c) for c in cols)

    def _fixed_lod_bare(self):
        # {FIXED d : AGG(...)}  ->  CALCULATE(AGG(...), ALLEXCEPT('T', 'T'[d], ...))
        table, cols, inner = self._lod_core()
        cols_dax = self._lod_cols_dax(table, cols)
        return (f"CALCULATE({inner[0]}, ALLEXCEPT({_dax_table(table)}, {cols_dax}))", inner[1])

    def _fixed_lod_reagg(self, outer_agg):
        # AGG_outer({FIXED d : inner}) -> AGGX_outer(SUMMARIZE('T', 'T'[d], ...), CALCULATE(inner))
        if outer_agg not in _AGG_X:
            raise _CalcError(f"{outer_agg} cannot re-aggregate a FIXED LOD")
        table, cols, inner = self._lod_core()
        if outer_agg in ("SUM", "AVG", "MEDIAN") and inner[1] != "number":
            raise _CalcError(f"{outer_agg} over an LOD requires a numeric inner expression")
        if outer_agg in ("MIN", "MAX") and inner[1] not in ("number", "date"):
            raise _CalcError(f"{outer_agg} over an LOD requires a numeric/date inner expression")
        cols_dax = self._lod_cols_dax(table, cols)
        out_dtype = "date" if (outer_agg in ("MIN", "MAX") and inner[1] == "date") else "number"
        return (
            f"{_AGG_X[outer_agg]}(SUMMARIZE({_dax_table(table)}, {cols_dax}), CALCULATE({inner[0]}))",
            out_dtype,
        )


def validate_dax(text):
    """Lightweight guardrail on emitted DAX. Returns an error string, or "" if clean.

    Not a full DAX parser -- a defense-in-depth check that the emit is structurally
    sound (balanced parentheses and string quotes) before it ships. The
    recursive-descent emitter already guarantees this; the check backstops future
    edits. It deliberately does NOT scan for keyword "leakage" because legitimate
    column names / string literals (e.g. a column named [END]) would false-positive.
    """
    depth = 0
    in_str = False
    for ch in text:
        if ch == '"':
            in_str = not in_str
        elif not in_str and ch == "(":
            depth += 1
        elif not in_str and ch == ")":
            depth -= 1
            if depth < 0:
                return "unbalanced parentheses"
    if depth != 0:
        return "unbalanced parentheses"
    if in_str:
        return "unbalanced string quotes"
    return ""


def translate_tableau_calc_to_dax(formula, resolver):
    """Translate a SAFE-subset Tableau calc to DAX. Returns (dax|None, reason, tables_used).

    dax is None on any unsupported construct -> caller keeps the inert `= 0` stub.
    resolver(caption) -> (table_display_name, clean_col, tmdl_type) | None.
    """
    tables_used = set()
    f = (formula or "").strip()
    if not f:
        return None, "empty formula", tables_used
    try:
        toks = _tokenize(f)
        if not toks:
            return None, "empty formula", tables_used
        dax, _dtype = _Parser(toks, resolver, tables_used).parse()
        # Single-table only: terms spanning >1 table fall back (a relationship path
        # does not guarantee the DAX filter context reproduces Tableau's result).
        if len(tables_used) > 1:
            return None, "cross-table terms (fields span multiple tables)", tables_used
        leak = validate_dax(dax)
        if leak:
            return None, f"emit guardrail: {leak}", tables_used
        return dax, "ok", tables_used
    except _CalcError as e:
        return None, str(e), tables_used


def translate_tableau_calc_to_column_dax(formula, resolver):
    """Translate a ROW-LEVEL Tableau calc to a DAX *calculated-column* expression.

    Companion to translate_tableau_calc_to_dax with the SAME public shape --
    (dax|None, reason, tables_used) -- but it parses in row (calculated-column) context:
      * a bare ``[field]`` resolves to ``'Table'[Col]`` (in a measure this falls back), and
      * the row-level string / date / numeric-cast functions become available
        (LEN/LEFT/RIGHT/MID/UPPER/LOWER/REPLACE/CONTAINS/STARTSWITH/ENDSWITH/FIND;
        YEAR/MONTH/DAY/TODAY/NOW/DATEPART/DATEADD/DATEDIFF/DATETRUNC/DATE/MAKEDATE; INT/FLOAT),
        plus string ``+`` -> null-preserving concatenation.
    Aggregations, PERCENTILE, and LOD expressions are NOT valid in a row-level column and
    fall back here (use the measure entry point for those).

    Caller binding contract (the orchestrator/renderer owns the actual binding): when
    ``tables_used`` is a single ``{T}``, the emitted expression must be materialized as a
    calculated column on table ``T``. Empty ``tables_used`` -> no field references, bindable
    anywhere. More than one table -> falls back here (a row-level column cannot span tables).
    """
    tables_used = set()
    f = (formula or "").strip()
    if not f:
        return None, "empty formula", tables_used
    try:
        toks = _tokenize(f)
        if not toks:
            return None, "empty formula", tables_used
        dax, _dtype = _Parser(toks, resolver, tables_used, mode="column").parse()
        if len(tables_used) > 1:
            return None, "cross-table terms (fields span multiple tables)", tables_used
        leak = validate_dax(dax)
        if leak:
            return None, f"emit guardrail: {leak}", tables_used
        return dax, "ok", tables_used
    except _CalcError as e:
        return None, str(e), tables_used


def _orderby_clause(order_by, resolver, tables_used):
    # order_by items are a caption or a (caption, "ASC"|"DESC") pair. An explicit order is
    # REQUIRED for every table calc (the window functions omit <relation>, so DAX requires an
    # ORDERBY). Returns None when no order is supplied -> the caller falls back.
    parts = []
    for item in order_by:
        if isinstance(item, (tuple, list)):
            cap = item[0]
            direction = str(item[1]).upper() if len(item) > 1 and item[1] else "ASC"
        else:
            cap, direction = item, "ASC"
        if direction not in ("ASC", "DESC"):
            raise _CalcError(f"invalid sort direction {direction!r}")
        resolved = resolver(cap)
        if resolved is None:
            raise _CalcError(f"unresolved/ambiguous order-by field [{cap}]")
        table, col, _ty = resolved
        tables_used.add(table)
        parts.append(f"{_dax_table(table)}{_dax_col(col)}, {direction}")
    if not parts:
        return None
    return "ORDERBY(" + ", ".join(parts) + ")"


def _partitionby_clause(partition_by, resolver, tables_used):
    cols = []
    for cap in partition_by:
        resolved = resolver(cap)
        if resolved is None:
            raise _CalcError(f"unresolved/ambiguous partition field [{cap}]")
        table, col, _ty = resolved
        tables_used.add(table)
        cols.append(f"{_dax_table(table)}{_dax_col(col)}")
    if not cols:
        return None
    return "PARTITIONBY(" + ", ".join(cols) + ")"


def _emit_table_calc(name, p, spec):
    # p is a measure-context _Parser positioned just after the table-calc's '('. spec is the
    # "ORDERBY(...)[, PARTITIONBY(...)]" addressing tail shared by every window function.
    if name == "INDEX":
        p._expect_op(")")
        # Tableau INDEX() is the 1-based row position within the partition.
        return f"ROWNUMBER({spec})"
    inner = p._expr()  # measure-context inner (must be an aggregate, else it falls back)
    if name in _TABLECALC_X or name in _TABLECALC_WINDOW_X:
        aggx = _TABLECALC_X.get(name) or _TABLECALC_WINDOW_X[name]
        if aggx in ("SUMX", "AVERAGEX") and inner[1] != "number":
            raise _CalcError(f"{name} requires a numeric expression")
        if aggx in ("MINX", "MAXX") and inner[1] not in ("number", "date"):
            raise _CalcError(f"{name} requires a numeric/date expression")
        p._expect_op(")")
        # RUNNING_*: from the partition's first row (1, ABS) to the current row (0, REL).
        # WINDOW_*:  the whole partition, first row (1, ABS) to last row (-1, ABS).
        bounds = "1, ABS, 0, REL" if name in _TABLECALC_X else "1, ABS, -1, ABS"
        return f"{aggx}(WINDOW({bounds}, {spec}), CALCULATE({inner[0]}))"
    if name == "LOOKUP":
        p._expect_op(",")
        offset = p._expect_number(p._expr())
        p._expect_op(")")
        # Tableau LOOKUP(expr, offset): value of expr at a row offset (signed) from the current
        # row along the addressing -> OFFSET picks that row, CALCULATE re-evaluates expr there.
        return f"CALCULATE({inner[0]}, OFFSET({offset[0]}, {spec}))"
    raise _CalcError(f"unsupported table calculation {name}")


def translate_tableau_table_calc_to_dax(formula, resolver, partition_by=(), order_by=()):
    """Translate a Tableau TABLE CALCULATION to a modern-DAX window-function measure.

    Same (dax|None, reason, tables_used) shape as the other entry points, plus the explicit
    addressing a table calc needs (and which the .tds does not carry): ``partition_by`` is an
    iterable of field captions (Tableau's Compute-Using partition) and ``order_by`` is an
    iterable of captions or ``(caption, "ASC"|"DESC")`` pairs (the addressing sort). An order
    spec is REQUIRED; without one the calc falls back.

    Supported (the inner expression is translated in measure context, so it must be an
    aggregation):
      * ``INDEX()`` -> ``ROWNUMBER(ORDERBY(...)[, PARTITIONBY(...)])``
      * ``RUNNING_SUM/AVG/MIN/MAX(<agg>)`` -> ``<X>(WINDOW(1, ABS, 0, REL, <spec>), CALCULATE(<agg>))``
      * ``WINDOW_SUM/AVG/MIN/MAX(<agg>)``  -> ``<X>(WINDOW(1, ABS, -1, ABS, <spec>), CALCULATE(<agg>))``
      * ``LOOKUP(<agg>, offset)`` -> ``CALCULATE(<agg>, OFFSET(offset, <spec>))``
    Each window function omits its <relation> argument; per the DAX spec that defaults to
    ``ALLSELECTED()`` of the ORDERBY/PARTITIONBY columns, so the result is correct when the
    measure is evaluated against the marks the addressing describes. RANK/FIRST/LAST and other
    forms fall back for now.

    This is the DAX-pattern side of the seam; the orchestrator/viz layer supplies the real
    addressing once worksheets are parsed. Cross-table terms (inner + addressing spanning more
    than one table) fall back, consistent with the measure path.
    """
    tables_used = set()
    f = (formula or "").strip()
    if not f:
        return None, "empty formula", tables_used
    try:
        toks = _tokenize(f)
        if len(toks) < 3 or toks[0][0] != "id" or toks[1] != ("op", "("):
            return None, "not a table calculation", tables_used
        name = toks[0][1].upper()
        if name not in _TABLE_CALCS:
            return None, f"unsupported table calculation {toks[0][1]}", tables_used
        order_clause = _orderby_clause(order_by, resolver, tables_used)
        if order_clause is None:
            return None, "table calc requires an explicit order-by spec", tables_used
        part_clause = _partitionby_clause(partition_by, resolver, tables_used)
        spec = order_clause if part_clause is None else f"{order_clause}, {part_clause}"
        p = _Parser(toks, resolver, tables_used, mode="measure")
        p.pos = 2  # consume the table-calc name and '('
        dax = _emit_table_calc(name, p, spec)
        if p.pos != len(toks):
            raise _CalcError("unexpected trailing tokens after table calculation")
        if len(tables_used) > 1:
            return None, "cross-table terms (fields span multiple tables)", tables_used
        leak = validate_dax(dax)
        if leak:
            return None, f"emit guardrail: {leak}", tables_used
        return dax, "ok", tables_used
    except _CalcError as e:
        return None, str(e), tables_used


if __name__ == "__main__":
    _demo = {
        "Profit": ("Orders", "Profit", "decimal"),
        "Sales": ("Orders", "Sales", "decimal"),
        "Order Date": ("Orders", "Order_Date", "dateTime"),
        "State": ("Orders", "State", "string"),
        "City": ("Orders", "City", "string"),
    }
    _r = lambda cap: _demo.get(cap)
    for _f in (
        "SUM([Profit])/SUM([Sales])",
        "IF SUM([Sales]) > 0 THEN SUM([Profit]) ELSE 0 END",
        "ZN(SUM([Sales]))",
        "IIF(SUM([Sales]) >= 100, SUM([Profit]), 0)",
        "{FIXED [State] : SUM([Sales])}",
        "AVG({FIXED [State] : MAX({FIXED [State], [City] : SUM([Sales])})})",
    ):
        print(_f, "->", translate_tableau_calc_to_dax(_f, _r))
