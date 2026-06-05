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

MEASURE-CONTEXT INVARIANT: output is a DAX *measure*, so every leaf operand must be an
aggregation or a literal. A bare row-level field (e.g. ``[Sales]`` outside an aggregation) is
invalid in a measure and deterministically FALLS BACK. The parser also tracks a static data
type per node (number / text / date / bool) and falls back on any type mismatch (e.g. an IF
whose branches return different types, an arithmetic op on a non-numeric term, or a comparison
between incomparable types) so it never emits DAX that would error or silently coerce.

Anything outside this subset (INCLUDE/EXCLUDE LODs, table calcs WINDOW_/RUNNING_/RANK/LOOKUP/
INDEX/TOTAL, scalar date/string/regex functions, row-level operands inside a scalar math
function or CASE, nested arithmetic inside an aggregation, 4-arg IIF, references to other
calcs, unresolved or ambiguous fields, cross-table terms) deterministically FALLS BACK by
returning ``None`` so the caller keeps an inert ``= 0`` stub.
The original Tableau formula is preserved as a ``TableauFormula`` annotation by the renderer
either way.

Table calculations are intentionally NOT translated here: their result depends on the
worksheet's Compute-Using / addressing / sort, which lives in the workbook (``.twb``), not the
datasource (``.tds``) this skill operates on. They become tractable once worksheets are parsed
(roadmap v2). FIXED LODs, by contrast, are datasource-level semantics and ARE translated.

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
    def __init__(self, toks, resolver, tables_used):
        self.toks = toks
        self.pos = 0
        self.resolver = resolver
        self.tables_used = tables_used
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
            return self._fixed_lod_bare()
        if k == "id":
            u = v.upper()
            if u in _AGG_MAP:
                return self._agg()
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
            if u == "PERCENTILE":
                return self._percentile()
            raise _CalcError(f"unsupported function {v}")
        if k == "field":
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
