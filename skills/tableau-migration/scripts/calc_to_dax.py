"""Deterministic Tableau calculated-field -> DAX measure translator (no LLM).

Originated in the Tableau-Fabric-AI-Bridge project as an
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
    QUOTIENT(a, b), PI(), the trig family SIN/COS/TAN/ASIN/ACOS/ATAN/COT, and
    DEGREES/RADIANS (radian<->degree conversion)
  * comparison operators: = == <> != > >= < <=  (== -> = ; != -> <>). Booleans are equatable
    (= / <>) but not ordered (< > <= >=).
  * boolean literals: true / false -> TRUE() / FALSE()
  * set membership: x IN (a, b, ...) -> numeric/date x IN { a, b, ... }; text uses a
    case-sensitive EXACT chain (EXACT(x, a) || EXACT(x, b) ...); one consistent element type
  * boolean logic: AND -> && , OR -> || , NOT(x)
  * null handling: ZN(x) -> COALESCE(x, 0) ; IFNULL(a, b) -> COALESCE(a, b) ;
    ISNULL(x) -> ISBLANK(x)
  * string literals "..." / '...'
  * FIXED level-of-detail expressions wrapped in an outer aggregation:
    AGG({FIXED d1,d2,...: inner}) -> AGG_X(SUMMARIZE('T', 'T'[d1], ...), CALCULATE(inner))
    with SUM->SUMX, AVG->AVERAGEX, MIN->MINX, MAX->MAXX, MEDIAN->MEDIANX, COUNT->COUNTAX.
    Nested FIXED LODs translate only when each inner FIXED's dimension set is a SUPERSET of
    the enclosing FIXED's dimensions (otherwise the context-transition emit could silently
    compute the wrong number, so it falls back).
  * Table-scoped LODs with no dimensions -- {AGG(...)}, equivalently {FIXED : AGG(...)} -- which
    evaluate the inner aggregate across the ENTIRE source table (whatever the aggregate is: MAX,
    MIN, AVG, SUM, ...), ignoring filter/row context. Emitted as CALCULATE(inner, ALL('T')).
    INCLUDE/EXCLUDE LODs, COUNTD over an LOD, re-aggregating a table-scoped LOD, and a dimensioned
    bare LOD not wrapped in an outer aggregation all fall back.

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
A qualified bracket reference ``[A].[B]`` (Tableau parameter ``[Parameters].[X]``, a
datasource-qualified field, or a data-blend ``[federated.<hash>].[field]`` token) is tokenized
as a single reference and falls back with a SPECIFIC reason ("parameter reference ...
(unmodeled)" or "qualified reference ... (unmodeled)") rather than choking on the dot, so the
orchestrator can model parameters / cross-source fields later and revisit.
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
    "DEGREES", "RADIANS",
}
_MATH_1_SIG = {"CEILING": "1", "FLOOR": "1"}
_MATH_2 = {"POWER": "POWER", "DIV": "QUOTIENT", "MOD": "MOD"}
_SCALAR_MATH = _MATH_1 | set(_MATH_1_SIG) | set(_MATH_2) | {"ROUND", "LOG", "LOG2", "SQUARE", "PI", "ATAN2"}

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
    "SPACE", "PROPER", "ASCII", "CHAR",
}
_DATE_FNS = {
    "YEAR", "MONTH", "DAY", "TODAY", "NOW", "QUARTER",
    "DATEPART", "DATEADD", "DATEDIFF", "DATETRUNC", "DATE", "MAKEDATE",
    "ISOWEEK", "ISOWEEKDAY", "ISOYEAR", "DATENAME", "DATETIME",
}
_CAST_FNS = {"INT", "FLOAT"}
# Scalar ROW-LEVEL functions (string / date / numeric-cast). Available in BOTH measure and column
# mode: in a measure the argument must itself be measure-valid (an aggregate, an LOD result, a
# constant, or a parameter) -- a bare row-level [field] argument still raises via the row-field
# guard, so a genuine row-level use in a measure correctly falls back. This lets scalar-date/-string
# MEASURES translate, e.g. DATEADD('day', 7, MAX([Order Date])), YEAR(MAX([Order Date])), TODAY().
_ROW_LEVEL_FNS = _STRING_FNS | _DATE_FNS | _CAST_FNS
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
# DATENAME('part', d) -> the part rendered as TEXT via DAX FORMAT. Only parts whose *name* is
# independent of the workbook start-of-week are mapped: a month/weekday NAME (and the 4-digit year)
# is culture-dependent but NOT start-of-week-dependent, unlike the numeric DATEPART('week'/'weekday')
# that is deliberately excluded above. 'quarter'/'day'/time parts (ambiguous single-char FORMAT
# tokens) and an explicit start_of_week argument fall back to the Tier-1 handoff.
_DATENAME_FORMAT = {"year": "yyyy", "month": "mmmm", "weekday": "dddd"}

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
# COUNT iterates non-blank marks: RUNNING_COUNT over the partition-start->current frame,
# WINDOW_COUNT over the whole partition. COUNTX accepts any inner type (it counts rows).
_TABLECALC_COUNT_X = {
    "RUNNING_COUNT": "1, ABS, 0, REL",
    "WINDOW_COUNT": "1, ABS, -1, ABS",
}
# WINDOW_* statistical aggregates over the WHOLE partition. Each maps to the matching DAX
# row-iterator stat function; all require a numeric inner. STDEV/VAR are the SAMPLE estimators
# (Tableau's default, ddof=1 -> DAX *.S); the population forms map to *.P. Verified faithful to
# ~1e-15 against an independent pandas ground truth on the live engine (Phase 3 boundary map).
_TABLECALC_STAT_X = {
    "WINDOW_MEDIAN": "MEDIANX",
    "WINDOW_STDEV": "STDEVX.S", "WINDOW_STDEVP": "STDEVX.P",
    "WINDOW_VAR": "VARX.S", "WINDOW_VARP": "VARX.P",
}
# No-argument positional table calcs (value derived purely from the addressing): INDEX is the
# 1-based row position; SIZE the partition row count; FIRST/LAST the signed offset to the
# partition's first/last row (FIRST() == 0 on the first row, LAST() == 0 on the last).
_TABLECALC_POSITION = {"INDEX", "SIZE", "FIRST", "LAST"}
# RANK family: rank each mark's measure value WITHIN its partition. RANK is competition ranking
# (ties share a rank, the next rank skips: 1,2,2,4) -> RANKX(..., Skip); RANK_DENSE is dense
# ranking (no gap after ties: 1,2,2,3) -> RANKX(..., Dense). Default direction is DESC (highest
# value -> rank 1); an optional 'asc'/'desc' second argument overrides it. Unlike the WINDOW/
# RUNNING family the rank value is independent of the addressing SORT, so RANK consumes the raw
# partition/addressing COLUMNS (to enumerate marks + restrict to the current partition) rather
# than the ORDERBY/PARTITIONBY window spec. Both tie modes + directions were oracle-certified
# faithful (0 mismatches) against an independent pandas ranking on the live engine.
_TABLECALC_RANK = {"RANK", "RANK_DENSE"}
# RANK_MODIFIED / RANK_PERCENTILE: modified-competition ranking (ties share the HIGHEST ordinal --
# Tableau ranks the set (6,9,9,14) as (4,3,3,1) descending) and its percentile normalisation
# (rank - 1) / (N - 1). DAX RANKX has no modified mode, so these emit a count of the marks on the
# "better-or-equal" side of the current mark over the SAME oracle-certified relation RANK uses
# (faithful by construction from Tableau's documented definitions). RANK_UNIQUE is deliberately
# EXCLUDED: it breaks ties by Tableau's internal addressing/row order ((6,9,9,14) -> (4,2,3,1)),
# which has no faithful DAX equivalent, so it stays fail-closed.
_TABLECALC_MODRANK = {"RANK_MODIFIED", "RANK_PERCENTILE"}
# TOTAL re-aggregates the inner expression across the whole partition (Tableau's "compute total"):
# CALCULATE(<inner>, <partition relation>) -- the standard percent-of-total denominator pattern.
_TABLECALC_RANKLIKE = _TABLECALC_RANK | _TABLECALC_MODRANK | {"TOTAL"}
_TABLE_CALCS = (
    _TABLECALC_POSITION | _TABLECALC_RANKLIKE | {"LOOKUP", "WINDOW_PERCENTILE"}
    | set(_TABLECALC_X) | set(_TABLECALC_WINDOW_X)
    | set(_TABLECALC_COUNT_X) | set(_TABLECALC_STAT_X)
)


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
            parts = [s[i + 1:j]]
            i = j + 1
            # Qualified references: [A].[B] (and longer chains). Tableau uses this shape for
            # parameters ([Parameters].[X]), datasource-qualified fields, and blend fields
            # ([federated.<hash>].[field]). Fold the whole dotted chain into ONE token so the
            # '.' never trips the scanner; the parser decides how to handle it.
            while i + 1 < n and s[i] == "." and s[i + 1] == "[":
                k = s.find("]", i + 2)
                if k == -1:
                    raise _CalcError("unterminated field reference")
                parts.append(s[i + 2:k])
                i = k + 1
            if len(parts) == 1:
                toks.append(("field", parts[0]))
            else:
                toks.append(("qfield", parts))
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
#   agg    := AGGFUNC '(' ( '[' fieldref ']' | '{' FIXED-lod '}' | rowexpr ) ')'
# A bare [field] is legal only inside an aggregate -- either directly, or within an
# aggregate's row-level expression argument (parsed in column context and folded with the
# matching X-iterator, e.g. SUM(IF c THEN v END) -> SUMX('T', IF(c, v))). A row-level field
# at measure top level is therefore a parse error (-> fallback).
class _Parser:
    def __init__(self, toks, resolver, tables_used, mode="measure", param_resolver=None):
        self.toks = toks
        self.pos = 0
        self.resolver = resolver
        self.tables_used = tables_used
        self.mode = mode          # "measure" (default) or "column" (row-level)
        self.param_resolver = param_resolver
        self._lod_dim_stack = []

    def _peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def _next(self):
        t = self._peek()
        self.pos += 1
        return t

    def _peek_at(self, n):
        i = self.pos + n
        return self.toks[i] if i < len(self.toks) else (None, None)

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
        if self._is_kw("IN"):
            return self._in_list(left)
        k, v = self._peek()
        if k == "cmp":
            self._next()
            right = self._add()
            if left[1] != right[1]:
                raise _CalcError("incomparable types in comparison")
            # Booleans are equatable (= / <>) but not ordered (< > <= >=): `flag = true` is
            # meaningful, `flag < true` is not.
            if left[1] == "bool" and v not in ("=", "<>"):
                raise _CalcError("booleans support only = and <> comparison")
            if left[1] == "text":
                # Tableau text comparison is case-SENSITIVE; DAX '='/'<>' follow the model's
                # (usually case-INSENSITIVE) collation -- the same reason IN uses EXACT above.
                # EXACT is also null-safe: EXACT(BLANK(), "x") is FALSE, matching Tableau's
                # unmatched-null. Ordered text comparisons (< > <= >=) have no case-sensitive
                # DAX form, so fall back rather than emit a case-insensitive ordering.
                if v == "=":
                    return (f"EXACT({left[0]}, {right[0]})", "bool")
                if v == "<>":
                    return (f"NOT(EXACT({left[0]}, {right[0]}))", "bool")
                raise _CalcError("ordered text comparison is case-sensitive in Tableau; no faithful DAX form")
            return (f"{left[0]} {v} {right[0]}", "bool")
        return left

    def _in_list(self, left):
        # Tableau `x IN (a, b, ...)` set membership. Every list element must share the operand's
        # type; a boolean operand or a mixed-type list falls back. In a measure a bare row-level
        # operand already fails before reaching IN.
        #
        # Numeric/date operands -> DAX `x IN { a, b, ... }`. TEXT operands cannot use `IN { ... }`
        # because DAX set membership follows the model's (usually case-INSENSITIVE) collation,
        # whereas Tableau string comparison is case-SENSITIVE; we instead emit a parenthesised
        # `EXACT(x, a) || EXACT(x, b) || ...` chain (EXACT is the case-sensitive form, matching the
        # CONTAINS/STARTSWITH mappings). The wrapping parens are required because DAX `&&` binds
        # tighter than `||`, so an unparenthesised chain would mis-group inside a surrounding `&&`.
        self._next()  # IN
        self._expect_op("(")
        if left[1] == "bool":
            raise _CalcError("IN requires a non-boolean operand")
        items = [self._expr()]
        while self._peek() == ("op", ","):
            self._next()
            items.append(self._expr())
        self._expect_op(")")
        for it in items:
            if it[1] != left[1]:
                raise _CalcError("IN list element type does not match the operand")
        if left[1] == "text":
            chain = " || ".join(f"EXACT({left[0]}, {it[0]})" for it in items)
            return (f"({chain})", "bool")
        joined = ", ".join(it[0] for it in items)
        return (f"{left[0]} IN {{{joined}}}", "bool")

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
            if u == "TRUE" or u == "FALSE":
                # Tableau boolean literals -> DAX TRUE()/FALSE() (so `flag = true`, IIF/IF/CASE
                # branches, and AND/OR operands carrying a literal all translate).
                self._next()
                return (f"{u}()", "bool")
            if u in _AGG_MAP:
                if self.mode == "column":
                    raise _CalcError(f"aggregation {u} not valid in a row-level column calc")
                return self._agg()
            if u == "PERCENTILE":
                if self.mode == "column":
                    raise _CalcError("PERCENTILE not valid in a row-level column calc")
                return self._percentile()
            if u == "ATTR":
                if self.mode == "column":
                    raise _CalcError("ATTR not valid in a row-level column calc")
                return self._attr()
            if u == "GROUP_CONCAT":
                if self.mode == "column":
                    raise _CalcError("GROUP_CONCAT not valid in a row-level column calc")
                return self._group_concat()
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
            if u in _ROW_LEVEL_FNS:
                return self._row_fn(u)
            raise _CalcError(f"unsupported function {v}")
        if k == "field":
            if self.mode == "column":
                return self._row_field()
            raise _CalcError("bare row-level field [..] not valid in a measure")
        if k == "qfield":
            self._next()
            return self._qualified_ref(v, allow_param=True)
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
        if name == "LOG2":
            # Tableau LOG2(x) is the base-2 logarithm -> DAX LOG(x, 2).
            self._expect_op(")")
            return (f"LOG({x[0]}, 2)", "number")
        if name == "ATAN2":
            # Tableau ATAN2(y, x) (y is the FIRST argument) -> the quadrant-correct angle in
            # (-pi, pi]. DAX has ATAN but no ATAN2, so the quadrant is reconstructed explicitly.
            # SWITCH evaluates only the matched result expression, so ATAN(y / x) is never computed
            # in the x = 0 branches -- no division by zero. (x here is the already-parsed first
            # operand y; the second operand is x.)
            self._expect_op(",")
            second = self._expect_number(self._expr())
            self._expect_op(")")
            y, xx = x[0], second[0]
            atan = f"ATAN({y} / {xx})"
            return (
                "SWITCH(TRUE(), "
                f"{xx} > 0, {atan}, "
                f"AND({xx} < 0, {y} >= 0), {atan} + PI(), "
                f"AND({xx} < 0, {y} < 0), {atan} - PI(), "
                f"AND({xx} = 0, {y} > 0), PI() / 2, "
                f"AND({xx} = 0, {y} < 0), -PI() / 2, "
                "0)",
                "number",
            )
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
        # CASE e WHEN v1 THEN r1 ... [ELSE z] END
        #   numeric/date/bool comparand -> SWITCH(e, v1, r1, ..., z)
        #   text comparand            -> nested IF(EXACT(e, v), r, ...) chain
        # e and every v must be aggregations/literals of one consistent type (a bare row-level
        # comparand like CASE [Region] WHEN ... is a parse error in measure mode -> falls back).
        comparand = self._or()
        pairs = []
        while self._is_kw("WHEN"):
            self._next()
            value = self._or()
            if value[1] != comparand[1]:
                raise _CalcError("CASE WHEN value type does not match the CASE expression")
            self._expect_kw("THEN")
            pairs.append((value[0], self._expr()))
        # Tableau CASE string matching is case-SENSITIVE; DAX SWITCH compares its keys with '='
        # which is case-INSENSITIVE. For a text comparand emit a nested IF(EXACT(...)) chain (the
        # same form the IF/ELSEIF path uses) so matching stays case-sensitive. Numeric/date/bool
        # keys compare exactly, so SWITCH is faithful there.
        return self._switch_emit(comparand[0], pairs, text_comparand=comparand[1] == "text")

    def _switch_emit(self, head, pairs, text_comparand=False):
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
        if text_comparand:
            # Fold inside-out into nested IF(EXACT(head, key), result[, inner]). No ELSE -> the
            # innermost IF is 2-arg (BLANK when unmatched), matching Tableau's null for no match.
            inner = else_node[0] if else_node is not None else None
            for key, result in reversed(pairs):
                cond = f"EXACT({head}, {key})"
                inner = f"IF({cond}, {result[0]})" if inner is None else f"IF({cond}, {result[0]}, {inner})"
            return (inner, rtype)
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
        if k == "qfield":
            self._qualified_ref(v)  # specific "(unmodeled)" reason instead of the generic one
        # Fast path: AGG([field]) over a single bare field -> the scalar aggregate AGG('T'[Col]).
        if k == "field" and self._peek_at(1) == ("op", ")"):
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
        # Otherwise the argument is a conditional/arithmetic ROW-level expression
        # (e.g. SUM(IF c THEN v END), SUM([x] * [y])) -> fold with the X-iterator.
        return self._agg_iterator(name)

    def _in_row_context(self, parse_fn):
        # Run *parse_fn* in ROW (column) context, isolating the tables it references so the
        # caller can infer a single iteration table. The instance's table set is swapped for a
        # fresh one during the sub-parse, then merged back into the shared set. Returns
        # (node, tables_touched). Aggregations and LOD braces are illegal in column mode, so a
        # nested aggregate inside the expression argument falls back cleanly.
        saved_mode = self.mode
        saved_tables = self.tables_used
        inner_tables = set()
        self.mode = "column"
        self.tables_used = inner_tables
        try:
            node = parse_fn()
        finally:
            self.mode = saved_mode
            self.tables_used = saved_tables
        self.tables_used |= inner_tables
        return node, inner_tables

    def _agg_iterator(self, name):
        # AGG(<row expression>) -> AGGX('T', <expr>). The argument is parsed at row level so a
        # bare [field] resolves to 'T'[Col] and IF/arithmetic become a row-level expression; the
        # matching X-iterator re-aggregates it. A no-ELSE IF yields a 2-arg DAX IF (BLANK when
        # unmatched), which SUMX/AVERAGEX/MINX/MAXX/MEDIANX/COUNTAX all skip -- reproducing
        # Tableau's "SUM(IF c THEN v END)" = sum over the rows where c holds.
        if name == "COUNTD":
            return self._countd_if()  # no DISTINCTCOUNTX exists -> CALCULATE + FILTER form
        if name not in _AGG_X:
            raise _CalcError(f"{name} does not support an expression argument")
        inner, inner_tables = self._in_row_context(self._expr)
        self._expect_op(")")
        if len(inner_tables) != 1:
            raise _CalcError(f"{name}(expr) must reference exactly one table")
        table = next(iter(inner_tables))
        if name in ("SUM", "AVG", "MEDIAN") and inner[1] != "number":
            raise _CalcError(f"{name}(expr) requires a numeric expression")
        if name in ("MIN", "MAX") and inner[1] not in ("number", "date"):
            raise _CalcError(f"{name}(expr) requires a numeric/date expression")
        out_dtype = "date" if (name in ("MIN", "MAX") and inner[1] == "date") else "number"
        return (f"{_AGG_X[name]}({_dax_table(table)}, {inner[0]})", out_dtype)

    def _countd_if(self):
        # COUNTD(IF cond THEN [field] END) ->
        #   COALESCE(CALCULATE(DISTINCTCOUNTNOBLANK('T'[field]), FILTER('T', cond)), 0).
        # DAX has no DISTINCTCOUNTX, so a distinct count under a row-level condition is expressed as
        # a CALCULATE with a FILTER. When the FILTER matches no rows, DISTINCTCOUNTNOBLANK returns
        # BLANK, but Tableau COUNTD of an empty set is 0 (verified live), so COALESCE(..., 0) keeps
        # the count numeric. Only this exact shape (a bare-field value, single THEN, no ELSE) is
        # supported; anything else falls back.
        if not self._is_kw("IF"):
            raise _CalcError("COUNTD(...) supports only COUNTD(IF cond THEN [field] END)")
        self._next()  # IF
        cond, cond_tables = self._in_row_context(self._or)
        self._expect_bool(cond)
        self._expect_kw("THEN")
        k, v = self._peek()
        if k == "qfield":
            self._qualified_ref(v)  # specific "(unmodeled)" reason instead of the generic one
        if k != "field":
            raise _CalcError("COUNTD(IF ...) value must be a single bare [field]")
        self._next()
        if self._is_kw("ELSEIF") or self._is_kw("ELSE"):
            raise _CalcError("COUNTD(IF ...) supports only a single THEN with no ELSE")
        self._expect_kw("END")
        self._expect_op(")")
        resolved = self.resolver(v)
        if resolved is None:
            raise _CalcError(f"unresolved/ambiguous field [{v}]")
        table, col, _tmdl_type = resolved
        self.tables_used.add(table)
        if len(set(cond_tables) | {table}) != 1:
            raise _CalcError("COUNTD(IF ...) must reference exactly one table")
        return (
            f"COALESCE(CALCULATE(DISTINCTCOUNTNOBLANK({_dax_table(table)}{_dax_col(col)}), "
            f"FILTER({_dax_table(table)}, {cond[0]})), 0)",
            "number",
        )

    def _percentile(self):
        # PERCENTILE([field], n) -> PERCENTILE.INC('T'[field], n). Aggregation over a single
        # numeric field; n (the 0..1 fraction) must be numeric. A non-numeric field or a bare
        # row-level / aggregated first argument falls back.
        self._next()  # PERCENTILE
        self._expect_op("(")
        k, v = self._peek()
        if k == "qfield":
            self._qualified_ref(v)  # specific "(unmodeled)" reason instead of the generic one
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

    def _attr(self):
        # ATTR([field]) -> IF(HASONEVALUE('T'[col]), VALUES('T'[col]), "*"). Tableau ATTR returns
        # the field's value when it is unique within the partition, otherwise the literal "*"
        # sentinel -- exactly the HASONEVALUE/VALUES idiom. Only a single bare [field] is supported;
        # an expression or qualified/parameter reference falls back.
        self._next()  # ATTR
        self._expect_op("(")
        k, v = self._peek()
        if k == "qfield":
            self._qualified_ref(v)  # specific "(unmodeled)" reason instead of the generic one
        if k != "field":
            raise _CalcError("ATTR supports only a single bare [field]")
        self._next()
        self._expect_op(")")
        resolved = self.resolver(v)
        if resolved is None:
            raise _CalcError(f"unresolved/ambiguous field [{v}]")
        table, col, tmdl_type = resolved
        self.tables_used.add(table)
        ref = f"{_dax_table(table)}{_dax_col(col)}"
        dtype = _DTYPE_BY_TMDL.get(tmdl_type, "text")
        return (f'IF(HASONEVALUE({ref}), VALUES({ref}), "*")', dtype)

    def _group_concat(self):
        # GROUP_CONCAT([field][, sep]) -> CONCATENATEX('T', 'T'[col], sep). Tableau's GROUP_CONCAT
        # (an "Additional" pass-through to the source's GROUP_CONCAT/STRING_AGG) concatenates EVERY
        # value in the partition -- duplicates INCLUDED -- comma-joined by default, in an order that
        # is unspecified in BOTH engines. CONCATENATEX over the base table reproduces that exact
        # contract (dup-inclusive, order not guaranteed). Only a single bare [field] first argument
        # is supported; an optional second argument overrides the separator.
        self._next()  # GROUP_CONCAT
        self._expect_op("(")
        k, v = self._peek()
        if k == "qfield":
            self._qualified_ref(v)  # specific "(unmodeled)" reason instead of the generic one
        if k != "field":
            raise _CalcError("GROUP_CONCAT supports only a single bare [field]")
        self._next()
        sep = _dax_string(",")
        if self._peek() == ("op", ","):
            self._next()
            sep = self._expect_text(self._expr())[0]
        self._expect_op(")")
        resolved = self.resolver(v)
        if resolved is None:
            raise _CalcError(f"unresolved/ambiguous field [{v}]")
        table, col, _tmdl_type = resolved
        self.tables_used.add(table)
        ref = f"{_dax_table(table)}{_dax_col(col)}"
        return (f"CONCATENATEX({_dax_table(table)}, {ref}, {sep})", "text")

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

    def _qualified_ref(self, parts, *, allow_param=False):
        # Tableau qualified reference [A].[B] (parameter, datasource-qualified, or blend field).
        # A value/what-if PARAMETER resolves to its SELECTEDVALUE measure -- a scalar, model-global
        # ref deliberately NOT registered in tables_used so the host expression stays single-table.
        # Only the SCALAR position (``_primary``) passes ``allow_param``; the other call sites
        # invoke this purely for its specific "(unmodeled)" raise (a param can't be aggregated or
        # used row-level), so they must keep failing even when a param_resolver is present.
        # Everything else stays an explicit "(unmodeled)" fallback so the caller keeps the stub.
        pretty = ".".join(f"[{p}]" for p in parts)
        if parts and parts[0].strip().lower() == "parameters":
            if allow_param and self.param_resolver and len(parts) >= 2:
                ref = self.param_resolver(parts[1])
                if ref:
                    return ref, "number"
            raise _CalcError(f"parameter reference {pretty} (unmodeled)")
        raise _CalcError(f"qualified reference {pretty} (unmodeled)")

    def _row_fn(self, name):
        if name in _STRING_FNS:
            return self._string_fn(name)
        if name in _CAST_FNS:
            return self._cast_fn(name)
        return self._date_fn(name)

    def _string_fn(self, name):
        self._next()  # function name
        self._expect_op("(")
        if name == "SPACE":
            # Tableau SPACE(n) = n spaces -> DAX REPT(" ", n) (its operand is numeric, not text).
            n = self._expect_number(self._expr())
            self._expect_op(")")
            return ('REPT(" ", ' + n[0] + ")", "text")
        if name == "CHAR":
            # Tableau CHAR(n) returns the character for code point n -> DAX UNICHAR(n) (matches
            # over the ASCII range Tableau's CHAR covers; operand is numeric).
            n = self._expect_number(self._expr())
            self._expect_op(")")
            return (f"UNICHAR({n[0]})", "text")
        s = self._expect_text(self._expr())
        if name == "LEN":
            self._expect_op(")")
            return (f"LEN({s[0]})", "number")
        if name in ("UPPER", "LOWER"):
            self._expect_op(")")
            return (f"{name}({s[0]})", "text")
        if name == "PROPER":
            # Title-case each word; DAX PROPER matches Tableau PROPER exactly.
            self._expect_op(")")
            return (f"PROPER({s[0]})", "text")
        if name == "ASCII":
            # Tableau ASCII(s) = code of the first character -> DAX UNICODE(s) (identical over the
            # ASCII range; UNICODE returns the code point of the first char).
            self._expect_op(")")
            return (f"UNICODE({s[0]})", "number")
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
        if name in ("YEAR", "MONTH", "DAY", "QUARTER"):
            d = self._expect_date(self._expr())
            self._expect_op(")")
            return (f"{name}({d[0]})", "number")
        if name == "ISOWEEK":
            # ISO-8601 week number -> DAX WEEKNUM(d, 21) (return-type 21 = ISO, Monday-start).
            d = self._expect_date(self._expr())
            self._expect_op(")")
            return (f"WEEKNUM({d[0]}, 21)", "number")
        if name == "ISOWEEKDAY":
            # ISO weekday (Monday=1 .. Sunday=7) -> DAX WEEKDAY(d, 2).
            d = self._expect_date(self._expr())
            self._expect_op(")")
            return (f"WEEKDAY({d[0]}, 2)", "number")
        # MAKETIME / MAKEDATETIME deliberately fall back: DAX TIME uses a different epoch date than
        # Tableau's, and MAKEDATETIME's argument forms vary across Tableau versions -- neither is
        # provably faithful here, so they route to the Tier-1 handoff instead of emitting risky DAX.
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
        if name == "DATENAME":
            # DATENAME('part', d[, start_of_week]) -> FORMAT(d, <token>) for the name-valued parts.
            part = self._part_literal()
            self._expect_op(",")
            d = self._expect_date(self._expr())
            if self._peek() == ("op", ","):
                # An explicit start_of_week argument cannot be honored faithfully -> fall back.
                raise _CalcError("DATENAME with a start_of_week argument is not supported")
            self._expect_op(")")
            fmt = _DATENAME_FORMAT.get(part)
            if fmt is None:
                raise _CalcError(f"unsupported DATENAME part {part!r}")
            return (f'FORMAT({d[0]}, "{fmt}")', "text")
        if name == "ISOYEAR":
            # ISO-8601 week-numbering year: the calendar year of the Thursday of d's ISO week,
            # YEAR(d + 4 - ISOWEEKDAY(d)) with ISOWEEKDAY = WEEKDAY(d, 2) (Mon=1 .. Sun=7).
            d = self._expect_date(self._expr())
            self._expect_op(")")
            return (f"YEAR({d[0]} + 4 - WEEKDAY({d[0]}, 2))", "number")
        if name == "DATETIME":
            # Tableau DATETIME(expr) casts to datetime. A date/datetime argument is already a DAX
            # dateTime, so the cast is the identity; a string/number argument (a locale-dependent
            # parse) has no faithful form and falls back via _expect_date.
            d = self._expect_date(self._expr())
            self._expect_op(")")
            return (d[0], "date")
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
        # Parse a FIXED LOD body. Returns (table, [clean_cols], inner_node). Accepted shapes:
        #   {FIXED d1, d2, ... : inner}  -- dimensioned (>=1 [field] dimension)
        #   {FIXED : inner}              -- table-scoped (explicit empty dimension list)
        #   {inner}                      -- table-scoped shorthand: no keyword == "FIXED to nothing"
        # A table-scoped LOD (no dimensions) evaluates the inner aggregate across the ENTIRE
        # table -- whatever that aggregate is (MAX, MIN, AVG, SUM, ...), not necessarily a sum --
        # ignoring filter/row context, so it emits
        # CALCULATE(inner, ALL('T')) instead of ALLEXCEPT. Only FIXED is datasource-level and
        # deterministically translatable; INCLUDE/EXCLUDE depend on the view's dimensionality (a
        # worksheet artifact, not in the .tds) -> fall back. Enforces the nested-superset rule: a
        # nested FIXED must fix at least every dimension of the LOD enclosing it; otherwise the
        # emitted context transition could compute a value Tableau never would, so we fall back.
        self._expect_op("{")
        if self._is_kw("INCLUDE") or self._is_kw("EXCLUDE"):
            raise _CalcError("only FIXED LOD is translated (INCLUDE/EXCLUDE fall back)")
        cols = []
        table = None
        if self._is_kw("FIXED"):
            self._next()  # FIXED
            if self._peek() != ("op", ":"):  # {FIXED : inner} is the explicit table-scoped form
                while True:
                    k, v = self._peek()
                    if k == "qfield":
                        self._qualified_ref(v)  # specific "(unmodeled)" reason instead of the generic one
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
        # else: {inner} shorthand -- no FIXED/INCLUDE/EXCLUDE keyword == fixed to nothing (table-scoped)
        dim_set = frozenset(cols)
        if self._lod_dim_stack and not (dim_set >= self._lod_dim_stack[-1]):
            raise _CalcError("nested FIXED LOD does not fix a superset of the enclosing LOD")
        before = frozenset(self.tables_used)
        self._lod_dim_stack.append(dim_set)
        inner = self._expr()
        self._lod_dim_stack.pop()
        self._expect_op("}")
        if table is None:
            # table-scoped LOD (no dimensions): derive the single source table from the inner
            # aggregate's field references. A constant or cross-table inner has no single table
            # to scope ALL() over, so it falls back.
            new_tables = self.tables_used - before
            if len(new_tables) == 1:
                table = next(iter(new_tables))
            elif len(self.tables_used) == 1:
                table = next(iter(self.tables_used))
            else:
                raise _CalcError("table-scoped LOD must reference exactly one table")
        return table, cols, inner

    def _lod_cols_dax(self, table, cols):
        return ", ".join(_dax_table(table) + _dax_col(c) for c in cols)

    def _fixed_lod_bare(self):
        # {FIXED d : AGG(...)}        -> CALCULATE(AGG(...), ALLEXCEPT('T', 'T'[d], ...))
        # {AGG(...)} / {FIXED : AGG(...)} (table-scoped, no dims) -> CALCULATE(AGG(...), ALL('T'))
        table, cols, inner = self._lod_core()
        if not cols:
            return (f"CALCULATE({inner[0]}, ALL({_dax_table(table)}))", inner[1])
        cols_dax = self._lod_cols_dax(table, cols)
        return (f"CALCULATE({inner[0]}, ALLEXCEPT({_dax_table(table)}, {cols_dax}))", inner[1])

    def _fixed_lod_reagg(self, outer_agg):
        # AGG_outer({FIXED d : inner}) -> AGGX_outer(SUMMARIZE('T', 'T'[d], ...), CALCULATE(inner))
        if outer_agg not in _AGG_X:
            raise _CalcError(f"{outer_agg} cannot re-aggregate a FIXED LOD")
        table, cols, inner = self._lod_core()
        if not cols:
            # A table-scoped LOD is already a single value evaluated over the whole table;
            # re-aggregating it has no SUMMARIZE grain to iterate, so fall back rather than emit
            # a degenerate window.
            raise _CalcError("re-aggregating a table-scoped LOD is not supported")
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


def field_references(formula):
    """Distinct field references in a Tableau ``formula``, in first-appearance order.

    Each entry is ``{"caption", "qualified", "parts"}``: a bare ``[X]`` is an unqualified caption
    (``qualified=False``, ``parts=["X"]``); a dotted ``[A].[B]`` chain -- Tableau's shape for
    parameters (``[Parameters].[X]``), datasource-qualified fields, and blend fields -- keeps its
    ``parts`` (``qualified=True``) and a display ``caption`` like ``[A].[B]``. This is a read-only
    helper for the Tier-0 -> Tier-1 handoff (so a second compiler/oracle gets the resolved field
    list for a calc the deterministic translator could not faithfully render); it emits no DAX.
    Tolerant: a formula that cannot be tokenized yields ``[]``.
    """
    try:
        toks = _tokenize(formula or "")
    except _CalcError:
        return []
    seen, out = set(), []
    for kind, val in toks:
        if kind == "field":
            key = ("f", val)
            if key not in seen:
                seen.add(key)
                out.append({"caption": val, "qualified": False, "parts": [val]})
        elif kind == "qfield":
            key = ("q", tuple(val))
            if key not in seen:
                seen.add(key)
                out.append({"caption": ".".join(f"[{p}]" for p in val),
                            "qualified": True, "parts": list(val)})
    return out


# Tableau date-attribute functions whose value is a calendar attribute of a single date field,
# and the matching column on the engine's generated Date dimension (see
# ``tmdl_generate.generate_date_table_tmdl``). Tableau's numeric extractors return the NUMBER, so
# MONTH/QUARTER bind to the hidden numeric helper ([Month No]/[Quarter No]), never the display
# text column ([Month]="Jan" / [Quarter]="Q1"). ISOWEEK is the ISO week-of-year (WEEKNUM ...,21);
# ISOWEEKDAY is the ISO weekday Mon=1..Sun=7 (WEEKDAY ...,2 = [Weekday No]); ISOYEAR is the ISO
# week-numbering year ([ISO Year]).
_DATE_ATTR_COLUMN = {
    "YEAR": "Year",
    "QUARTER": "Quarter No",
    "MONTH": "Month No",
    "DAY": "Day",
    "ISOWEEK": "Week of Year",
    "ISOWEEKDAY": "Weekday No",
    "ISOYEAR": "ISO Year",
}
# DATEPART('<part>', d) numeric parts that map to the same Date-dimension columns. The
# start-of-week-dependent parts ('week'/'weekday') are deliberately excluded -- their value
# depends on a culture/first-day-of-week setting, so they are not a faithful 1:1 bind.
_DATEPART_ATTR_COLUMN = {
    "year": "Year",
    "quarter": "Quarter No",
    "month": "Month No",
    "day": "Day",
}


def date_attribute_binding(formula):
    """If ``formula`` is EXACTLY a calendar attribute of a single bare date field, return
    ``(field_caption, date_column)``; otherwise ``None``.

    Recognized shapes (and nothing more complex)::

        YEAR([f]) QUARTER([f]) MONTH([f]) DAY([f]) ISOWEEK([f]) ISOWEEKDAY([f]) ISOYEAR([f])
        DATEPART('year'|'quarter'|'month'|'day', [f])
        DATENAME('weekday', [f])

    ``date_column`` is the matching column on the generated Date dimension. This is a read-only
    recognizer the orchestrator uses to OPTIONALLY bind such a calc to the shared Date table via
    ``RELATED('Date'[<date_column>])`` -- but only when ``[f]`` is the ACTIVE date relationship
    (a role-playing date can't use RELATED safely). It is intentionally strict: a qualified /
    parameter field, any extra argument (e.g. a start-of-week argument), or anything beyond the
    bare single-field shapes returns ``None`` so only the unambiguous, culture-independent
    attributes ever bind. Emits no DAX; a formula that cannot be tokenized yields ``None``.
    """
    try:
        toks = _tokenize(formula or "")
    except _CalcError:
        return None
    # FN ( [field] )
    if (len(toks) == 4 and toks[0][0] == "id" and toks[1] == ("op", "(")
            and toks[2][0] == "field" and toks[3] == ("op", ")")):
        col = _DATE_ATTR_COLUMN.get(toks[0][1].upper())
        return (toks[2][1], col) if col else None
    # FN ( 'part' , [field] )
    if (len(toks) == 6 and toks[0][0] == "id" and toks[1] == ("op", "(")
            and toks[2][0] == "str" and toks[3] == ("op", ",")
            and toks[4][0] == "field" and toks[5] == ("op", ")")):
        fn, part = toks[0][1].upper(), toks[2][1].strip().lower()
        if fn == "DATEPART":
            col = _DATEPART_ATTR_COLUMN.get(part)
            return (toks[4][1], col) if col else None
        if fn == "DATENAME" and part == "weekday":
            return (toks[4][1], "Day Name")
    return None


def translate_tableau_calc_to_dax(formula, resolver, param_resolver=None):
    """Translate a SAFE-subset Tableau calc to DAX. Returns (dax|None, reason, tables_used).

    dax is None on any unsupported construct -> caller keeps the inert `= 0` stub.
    resolver(caption) -> (table_display_name, clean_col, tmdl_type) | None.
    param_resolver(name) -> "[Measure]" | None: turns a value/what-if ``[Parameters].[X]`` into
    its SELECTEDVALUE measure reference (measure-translation path only; omit it for calculated
    columns, where a slicer selection cannot be read and the calc should stub).
    """
    tables_used = set()
    f = (formula or "").strip()
    if not f:
        return None, "empty formula", tables_used
    try:
        toks = _tokenize(f)
        if not toks:
            return None, "empty formula", tables_used
        dax, _dtype = _Parser(toks, resolver, tables_used, param_resolver=param_resolver).parse()
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


def _order_captions(order_by):
    # The bare field captions from an order_by spec (each item is a caption or a
    # (caption, "ASC"|"DESC") pair); the sort DIRECTION is dropped -- callers that need the raw
    # addressing columns (RANK) don't depend on it.
    return [item[0] if isinstance(item, (tuple, list)) else item for item in order_by]


def _resolve_refs(captions, resolver, tables_used):
    # Resolve a list of field captions to raw ``'Table'[Column]`` DAX references (recording each
    # home table in ``tables_used``); raises on any unresolved/ambiguous caption.
    refs = []
    for cap in captions:
        resolved = resolver(cap)
        if resolved is None:
            raise _CalcError(f"unresolved/ambiguous field [{cap}]")
        table, col, _ty = resolved
        tables_used.add(table)
        refs.append(f"{_dax_table(table)}{_dax_col(col)}")
    return refs


def _emit_rank(name, p, mark_refs, part_refs):
    # name in _TABLECALC_RANKLIKE; p is a measure-context _Parser positioned just after the '('.
    # mark_refs enumerate the marks (partition + addressing columns); part_refs are the partition
    # subset. Computes the current mark's rank (or partition total) among all marks in its partition.
    inner = p._expr()  # measure-context inner; an aggregate (a bare row-level field falls back)
    is_total = name == "TOTAL"
    if not is_total and inner[1] != "number":
        # The RANK family ranks a numeric measure; TOTAL re-aggregates any supported inner
        # aggregate, including a date one (e.g. TOTAL(MIN([Order Date]))).
        raise _CalcError(f"{name} requires a numeric expression")
    # RANK/RANK_DENSE/RANK_MODIFIED default DESC (highest value -> rank 1); RANK_PERCENTILE defaults
    # ASC (lowest value -> 0.0). TOTAL takes no direction argument.
    direction = "ASC" if name == "RANK_PERCENTILE" else "DESC"
    if not is_total:
        k, v = p._peek()
        if k == "op" and v == ",":
            p._next()
            dk, dv = p._peek()
            if dk != "str" or str(dv).lower() not in ("asc", "desc"):
                raise _CalcError(f"{name} direction must be 'asc' or 'desc'")
            direction = str(dv).upper()
            p._next()
    p._expect_op(")")
    marks = "ALLSELECTED(" + ", ".join(mark_refs) + ")"
    if part_refs:
        # Restrict to the current partition: each partition column pinned to its mark value.
        pred = " && ".join(f"{c} = SELECTEDVALUE({c})" for c in part_refs)
        relation = f"FILTER({marks}, {pred})"
    else:
        relation = marks
    if name in _TABLECALC_RANK:
        ties = "Skip" if name == "RANK" else "Dense"  # competition vs dense ranking
        return f"RANKX({relation}, CALCULATE({inner[0]}), , {direction}, {ties})"
    if is_total:
        # Recompute the inner aggregate over every addressing mark in the current partition.
        return f"CALCULATE({inner[0]}, {relation})"
    # RANK_MODIFIED / RANK_PERCENTILE: modified-competition rank by counting marks on the
    # "better-or-equal" side of the current mark (DESC counts values >= the current value, ASC
    # counts values <=), so tied marks all take the HIGHEST ordinal. RANK_PERCENTILE normalises
    # that to (rank - 1) / (N - 1) -- 0.0 for the lowest mark, 1.0 for the highest, 0.0 if N == 1.
    op = ">=" if direction == "DESC" else "<="
    prefix = (
        f"VAR _rel = {relation} "
        f"VAR _cur = CALCULATE({inner[0]}) "
        f"VAR _rank = COUNTROWS(FILTER(_rel, CALCULATE({inner[0]}) {op} _cur)) "
    )
    if name == "RANK_MODIFIED":
        return prefix + "RETURN _rank"
    return prefix + "RETURN DIVIDE(_rank - 1, COUNTROWS(_rel) - 1, 0)"


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


def _parse_window_bound(p):
    """Parse a Tableau WINDOW_* relative bound: an (optionally signed) INTEGER literal offset.

    Only integer literals are supported. FIRST()/LAST()/expression bounds raise -> the caller
    falls back, keeping the faithful-or-stub contract (those forms are not yet oracle-certified).
    """
    sign = ""
    k, v = p._peek()
    if k == "op" and v in "+-":
        if v == "-":
            sign = "-"
        p._next()
        k, v = p._peek()
    if k != "num" or "." in v:
        raise _CalcError("WINDOW bound must be an integer literal")
    p._next()
    return int(sign + v)


def _window_frame(p, spec, default):
    """Optional Tableau moving-window bounds on a WINDOW_* call: ``WINDOW_*(expr, start, end)``.

    When a ``, start, end`` tail follows the inner expression, both must be integer literals and
    map directly to a relative frame ``WINDOW(start, REL, end, REL, spec)`` (oracle-certified
    faithful for SUM/AVG/MIN/MAX/COUNT to ~1e-15, edge-clamped exactly as Tableau clamps a moving
    window at the partition boundary). With no tail the frame is ``default`` (the whole partition).
    """
    k, v = p._peek()
    if not (k == "op" and v == ","):
        return default
    p._next()
    start = _parse_window_bound(p)
    p._expect_op(",")
    end = _parse_window_bound(p)
    return f"WINDOW({start}, REL, {end}, REL, {spec})"


def _emit_table_calc(name, p, spec):
    # p is a measure-context _Parser positioned just after the table-calc's '('. spec is the
    # "ORDERBY(...)[, PARTITIONBY(...)]" addressing tail shared by every window function.
    whole = f"WINDOW(1, ABS, -1, ABS, {spec})"  # first -> last row of the partition
    if name in _TABLECALC_POSITION:
        p._expect_op(")")
        if name == "INDEX":
            # Tableau INDEX() is the 1-based row position within the partition.
            return f"ROWNUMBER({spec})"
        if name == "SIZE":
            return f"COUNTROWS({whole})"
        if name == "FIRST":
            # offset to the first row: 0 on the first row, -1 on the second, ...
            return f"1 - ROWNUMBER({spec})"
        # LAST: offset to the last row: 0 on the last row, 1 on the previous, ...
        return f"COUNTROWS({whole}) - ROWNUMBER({spec})"
    inner = p._expr()  # measure-context inner (must be an aggregate, else it falls back)
    if name in _TABLECALC_X or name in _TABLECALC_WINDOW_X:
        aggx = _TABLECALC_X.get(name) or _TABLECALC_WINDOW_X[name]
        if aggx in ("SUMX", "AVERAGEX") and inner[1] != "number":
            raise _CalcError(f"{name} requires a numeric expression")
        if aggx in ("MINX", "MAXX") and inner[1] not in ("number", "date"):
            raise _CalcError(f"{name} requires a numeric/date expression")
        if name in _TABLECALC_X:
            # RUNNING_*: the partition's first row (1, ABS) to the current row (0, REL). No bounds.
            frame = f"WINDOW(1, ABS, 0, REL, {spec})"
        else:
            # WINDOW_*: the whole partition by default, or an explicit moving (start, end) frame.
            frame = _window_frame(p, spec, whole)
        p._expect_op(")")
        return f"{aggx}({frame}, CALCULATE({inner[0]}))"
    if name in _TABLECALC_COUNT_X:
        # COUNT counts non-blank marks; any inner type is valid (it counts, not sums). RUNNING_COUNT
        # frames partition-start -> current; WINDOW_COUNT defaults to the whole partition but, like
        # the other WINDOW_* aggregates, accepts an explicit moving (start, end) frame.
        if name == "RUNNING_COUNT":
            frame = f"WINDOW({_TABLECALC_COUNT_X[name]}, {spec})"
        else:
            frame = _window_frame(p, spec, whole)
        p._expect_op(")")
        return f"COUNTX({frame}, CALCULATE({inner[0]}))"
    if name in _TABLECALC_STAT_X:
        # Whole-partition statistical aggregates. Explicit moving bounds are intentionally NOT
        # enabled here (sample STDEV/VAR are undefined on a 1-row edge frame, so the moving form is
        # not oracle-certified); a trailing bounds argument trips the ')' below -> faithful fallback.
        if inner[1] != "number":
            raise _CalcError(f"{name} requires a numeric expression")
        p._expect_op(")")
        return f"{_TABLECALC_STAT_X[name]}({whole}, CALCULATE({inner[0]}))"
    if name == "WINDOW_PERCENTILE":
        # WINDOW_PERCENTILE(<agg>, k): the k-th percentile (k in 0..1) over the whole partition.
        # PERCENTILEX.INC uses linear interpolation, matching Tableau's WINDOW_PERCENTILE (verified
        # faithful against an independent pandas quantile on the live engine). Moving bounds are not
        # certified here -> a trailing bounds argument trips the ')' below and falls back.
        if inner[1] != "number":
            raise _CalcError("WINDOW_PERCENTILE requires a numeric expression")
        p._expect_op(",")
        k = p._expect_number(p._expr())
        p._expect_op(")")
        return f"PERCENTILEX.INC({whole}, CALCULATE({inner[0]}), {k[0]})"
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
      * ``SIZE()``  -> ``COUNTROWS(WINDOW(1, ABS, -1, ABS, <spec>))``
      * ``FIRST()`` -> ``1 - ROWNUMBER(<spec>)``    ``LAST()`` -> ``COUNTROWS(WINDOW(...)) - ROWNUMBER(<spec>)``
      * ``RUNNING_SUM/AVG/MIN/MAX/COUNT(<agg>)`` -> ``<X>(WINDOW(1, ABS, 0, REL, <spec>), CALCULATE(<agg>))``
      * ``WINDOW_SUM/AVG/MIN/MAX/COUNT(<agg>)``  -> ``<X>(WINDOW(1, ABS, -1, ABS, <spec>), CALCULATE(<agg>))``
      * ``WINDOW_SUM/AVG/MIN/MAX/COUNT(<agg>, start, end)`` (integer-literal moving bounds) ->
        ``<X>(WINDOW(start, REL, end, REL, <spec>), CALCULATE(<agg>))`` (e.g. a trailing-3 mean
        ``WINDOW_AVG(SUM([Sales]), -2, 0)``); FIRST()/LAST()/expression bounds fall back.
      * ``WINDOW_MEDIAN/STDEV/STDEVP/VAR/VARP(<agg>)`` -> ``<X>(WINDOW(1, ABS, -1, ABS, <spec>), CALCULATE(<agg>))``
      * ``WINDOW_PERCENTILE(<agg>, k)`` -> ``PERCENTILEX.INC(WINDOW(1, ABS, -1, ABS, <spec>), CALCULATE(<agg>), k)``
      * ``LOOKUP(<agg>, offset)`` -> ``CALCULATE(<agg>, OFFSET(offset, <spec>))``
      * ``RANK(<agg>[, 'asc'|'desc'])`` / ``RANK_DENSE(<agg>[, 'asc'|'desc'])`` ->
        ``RANKX(FILTER(ALLSELECTED(<mark cols>), <partition col> = SELECTEDVALUE(<partition col>)),
        CALCULATE(<agg>), , <DESC|ASC>, <Skip|Dense>)`` -- competition vs dense ranking of each
        mark's value within its partition (the FILTER is dropped when there is no partition).
      * ``RANK_MODIFIED/RANK_PERCENTILE(<agg>[, 'asc'|'desc'])`` -> a count of the marks on the
        better-or-equal side of the current mark over that same relation (modified-competition
        rank; percentile normalises it to ``(rank - 1) / (N - 1)``).
      * ``TOTAL(<agg>)`` -> ``CALCULATE(<agg>, <partition relation>)`` (re-aggregate over the partition).
    Each window function omits its <relation> argument; per the DAX spec that defaults to
    ``ALLSELECTED()`` of the ORDERBY/PARTITIONBY columns, so the result is correct when the
    measure is evaluated against the marks the addressing describes. Moving-window
    STDEV/VAR/MEDIAN/PERCENTILE and RANK_UNIQUE (addressing-order tiebreak) fall back for now.

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
        p = _Parser(toks, resolver, tables_used, mode="measure")
        p.pos = 2  # consume the table-calc name and '('
        if name in _TABLECALC_RANKLIKE:
            # The RANK family + TOTAL need the raw addressing/partition COLUMNS (to enumerate marks
            # + restrict to the current partition), not the ORDERBY/PARTITIONBY window spec -- their
            # value is independent of the addressing sort. order_by supplies the addressing dim(s).
            part_refs = _resolve_refs(partition_by, resolver, tables_used)
            addr_refs = _resolve_refs(_order_captions(order_by), resolver, tables_used)
            if not addr_refs:
                return None, "table calc requires an explicit order-by spec", tables_used
            dax = _emit_rank(name, p, part_refs + addr_refs, part_refs)
        else:
            order_clause = _orderby_clause(order_by, resolver, tables_used)
            if order_clause is None:
                return None, "table calc requires an explicit order-by spec", tables_used
            part_clause = _partitionby_clause(partition_by, resolver, tables_used)
            spec = order_clause if part_clause is None else f"{order_clause}, {part_clause}"
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


# ===========================================================================
# Assisted translation (opt-in, human-approved) -- a SEPARATE layer ABOVE the
# deterministic safe-subset translator.
#
# translate_tableau_calc_to_dax only emits DAX when the mapping is provably 1:1;
# everything else FALLS BACK to an inert `= 0` stub. That contract is unchanged.
# This layer runs ONLY on those fallbacks and recognizes a small registry of
# higher-level Tableau IDIOMS whose faithful DAX is a *semantic* rewrite (not a
# syntax swap) -- e.g. argmax-over-a-dimension ("the city with the most sales").
# Because the rewrite has real correctness forks (ties, filter context), the
# result is a clearly-labeled SUGGESTION a human approves, NEVER silently emitted
# as the live measure. The orchestrator records every suggestion in
# report["assisted_suggestions"] and emits it as a `TranslationSuggestion`
# annotation on the (still inert) measure; on bulk approval it flips into the real
# expression tagged `TranslatedBy = assisted translation (human-approved)`.
# ===========================================================================

# AGG token -> scalar DAX aggregation used inside the argmax detail table.
_ASSISTED_AGG_DAX = {
    "SUM": "SUM", "AVG": "AVERAGE", "MIN": "MIN", "MAX": "MAX",
    "COUNT": "COUNTA", "COUNTD": "DISTINCTCOUNTNOBLANK", "MEDIAN": "MEDIAN",
}


def _tok_is_kw(tok, kw):
    return tok[0] == "id" and tok[1].upper() == kw.upper()


def _split_top_level(toks, sep_kw):
    """Split ``toks`` at the FIRST top-level (paren/brace depth 0) keyword ``sep_kw``.
    Returns ``(before, after)`` or ``None`` when the keyword is absent at depth 0."""
    depth = 0
    for i, t in enumerate(toks):
        if t[0] == "op" and t[1] in "({":
            depth += 1
        elif t[0] == "op" and t[1] in ")}":
            depth -= 1
        elif depth == 0 and _tok_is_kw(t, sep_kw):
            return toks[:i], toks[i + 1:]
    return None


def _split_top_level_eq(toks):
    """Split on the SINGLE top-level ``=`` comparison. Returns ``(left, right)`` or
    ``None`` (no top-level ``=``, or more than one -> ambiguous)."""
    depth = 0
    found = None
    for i, t in enumerate(toks):
        if t[0] == "op" and t[1] in "({":
            depth += 1
        elif t[0] == "op" and t[1] in ")}":
            depth -= 1
        elif depth == 0 and t[0] == "cmp" and t[1] == "=":
            if found is not None:
                return None
            found = i
    return None if found is None else (toks[:found], toks[found + 1:])


def _strip_outer_parens(toks):
    """Remove a fully-wrapping outer ``( ... )`` pair (repeatedly), leaving inner unchanged."""
    while len(toks) >= 2 and toks[0] == ("op", "(") and toks[-1] == ("op", ")"):
        depth, ok = 0, True
        for i, t in enumerate(toks):
            if t == ("op", "("):
                depth += 1
            elif t == ("op", ")"):
                depth -= 1
                if depth == 0 and i != len(toks) - 1:
                    ok = False
                    break
        if not ok:
            break
        toks = toks[1:-1]
    return toks


def _parse_simple_field(toks):
    """``[Field]`` -> the caption string, else ``None``."""
    toks = _strip_outer_parens(toks)
    if len(toks) == 1 and toks[0][0] == "field":
        return toks[0][1]
    return None


def _parse_simple_agg(toks):
    """``AGG([Field])`` -> ``(AGG_upper, field_caption)`` for one bare field, else ``None``."""
    toks = _strip_outer_parens(toks)
    if (len(toks) == 4 and toks[0][0] == "id" and toks[1] == ("op", "(")
            and toks[2][0] == "field" and toks[3] == ("op", ")")):
        return toks[0][1].upper(), toks[2][1]
    return None


def _parse_fixed_lod(toks):
    """``{FIXED [d1], [d2], ... : <inner>}`` -> ``(dims, inner_toks)``; else ``None``.
    Only FIXED is recognized and every dimension must be a bare ``[field]`` reference."""
    toks = _strip_outer_parens(toks)
    if len(toks) < 2 or toks[0] != ("op", "{") or toks[-1] != ("op", "}"):
        return None
    body = toks[1:-1]
    if not body or not _tok_is_kw(body[0], "FIXED"):
        return None
    body = body[1:]
    depth, split = 0, None
    for i, t in enumerate(body):
        if t[0] == "op" and t[1] in "({":
            depth += 1
        elif t[0] == "op" and t[1] in ")}":
            depth -= 1
        elif depth == 0 and t == ("op", ":"):
            split = i
            break
    if split is None:
        return None
    dim_toks, inner = body[:split], body[split + 1:]
    dims, expect_field = [], True
    for t in dim_toks:
        if expect_field:
            if t[0] != "field":
                return None
            dims.append(t[1])
            expect_field = False
        elif t != ("op", ","):
            return None
        else:
            expect_field = True
    if not dims or expect_field:  # empty or trailing comma
        return None
    return dims, inner


def _parse_max_of_fixed(toks):
    """``{FIXED P : MAX({FIXED Q : AGG([f])})}`` -> ``(P, Q, AGG, f)``; else ``None``."""
    outer = _parse_fixed_lod(toks)
    if outer is None:
        return None
    p_dims, inner = outer
    inner = _strip_outer_parens(inner)
    if (len(inner) < 4 or inner[0][0] != "id" or inner[0][1].upper() != "MAX"
            or inner[1] != ("op", "(") or inner[-1] != ("op", ")")):
        return None
    fl = _parse_fixed_lod(inner[2:-1])
    if fl is None:
        return None
    q_dims, agg_inner = fl
    agg = _parse_simple_agg(agg_inner)
    if agg is None:
        return None
    return p_dims, q_dims, agg[0], agg[1]


def _resolve_detail_lod(toks, calc_lookup):
    """Return a detail FIXED LOD ``(dims, inner)`` with a SIMPLE-agg inner, accepting either an
    inline ``{FIXED dims : AGG([f])}`` or a bare reference to a calc that is one (the real
    "Highest Selling City By State Sales" shape names BOTH the per-partition max and the per-member
    detail as separate calcs). Narrowly gated: the reference must resolve, via ``calc_lookup``, to a
    FIXED LOD whose inner is a simple aggregation. Returns ``None`` otherwise."""
    fl = _parse_fixed_lod(toks)
    if fl is None:
        ref = _parse_simple_field(toks)
        if ref is None or not calc_lookup:
            return None
        ref_formula = calc_lookup.get(ref.lower())
        if not ref_formula:
            return None
        try:
            fl = _parse_fixed_lod(_tokenize(ref_formula))
        except _CalcError:
            return None
        if fl is None:
            return None
    if _parse_simple_agg(fl[1]) is None:
        return None
    return fl


def _detect_argmax_dimension(formula, resolver, calc_lookup):
    """Detect Tableau's argmax-over-a-dimension idiom and emit faithful, tie-aware DAX.

    Shape:  ``IF <A> = {FIXED P, C : AGG([f])} THEN [C] END``  where ``<A>`` -- inline or
    via another calc -- is ``{FIXED P : MAX({FIXED P, C : AGG([f])})}``. Reads as "the member
    of dimension C whose AGG([f]) equals the per-P maximum" (e.g. the city with the most
    sales in each state). Returns a suggestion dict or ``None``.
    """
    try:
        toks = _tokenize(formula)
    except _CalcError:
        return None
    if not toks or not _tok_is_kw(toks[0], "IF"):
        return None
    cond_then = _split_top_level(toks[1:], "THEN")
    if cond_then is None:
        return None
    cond, after_then = cond_then
    then_end = _split_top_level(after_then, "END")
    if then_end is None:
        return None
    result, tail = then_end
    if tail:  # nothing may follow END (a single IF/THEN/END only)
        return None
    # the THEN branch must be exactly one bare dimension C (this also rejects ELSEIF/ELSE,
    # which would leave extra tokens in `result`).
    c = _parse_simple_field(result)
    if c is None:
        return None
    eq = _split_top_level_eq(cond)
    if eq is None:
        return None
    left, right = eq
    # Identify which side is the detail FIXED LOD B = {FIXED dims : AGG([f])} (a SIMPLE-agg
    # inner) and which is A (the per-partition max). Try both orders -- the equality may be
    # written either way round -- and resolve a bare calc reference on the detail side too.
    b = a_side = None
    for cand_b, cand_a in ((left, right), (right, left)):
        fl = _resolve_detail_lod(cand_b, calc_lookup)
        if fl is not None:
            b, a_side = fl, cand_a
            break
    if b is None:
        return None
    b_dims, b_inner = b
    b_aggname, b_field = _parse_simple_agg(b_inner)
    # resolve A: an inline max-of-fixed, OR a reference to a calc that is one.
    a = _parse_max_of_fixed(a_side)
    if a is None:
        ref = _parse_simple_field(a_side)
        if ref is None or not calc_lookup:
            return None
        ref_formula = calc_lookup.get(ref.lower())
        if not ref_formula:
            return None
        try:
            a = _parse_max_of_fixed(_tokenize(ref_formula))
        except _CalcError:
            return None
        if a is None:
            return None
    p_dims, q_dims, a_aggname, a_field = a

    # ---- structural validation on RESOLVED (table, col) identities --------
    def rid(cap):
        r = resolver(cap)
        return None if r is None else (r[0], r[1])

    def dim_ids(dims):
        out = []
        for d in dims:
            r = rid(d)
            if r is None:
                return None
            out.append(r)
        return out

    b_field_id, a_field_id = rid(b_field), rid(a_field)
    if not b_field_id or not a_field_id or a_field_id != b_field_id:
        return None
    if a_aggname != b_aggname:
        return None
    b_ids, q_ids, p_ids, c_id = dim_ids(b_dims), dim_ids(q_dims), dim_ids(p_dims), rid(c)
    if b_ids is None or q_ids is None or p_ids is None or c_id is None:
        return None
    if set(q_ids) != set(b_ids):            # A's inner grain must equal B's grain
        return None
    if not (set(p_ids) < set(b_ids)):        # P must be a STRICT subset of the grain
        return None
    if set(b_ids) - set(p_ids) != {c_id}:    # exactly one dim argmax'd over, == the THEN dim
        return None
    if c_id in set(p_ids):
        return None
    tables = {t for (t, _c) in b_ids} | {b_field_id[0]}
    if len(tables) != 1:                      # single-table only
        return None
    table = next(iter(tables))
    agg_dax = _ASSISTED_AGG_DAX.get(b_aggname)
    if agg_dax is None:
        return None

    # ---- emit faithful, tie-aware DAX -------------------------------------
    p_cols = [col for (_t, col) in p_ids]
    c_col, field_col = c_id[1], b_field_id[1]
    tdax = _dax_table(table)
    summarize_cols = ", ".join(tdax + _dax_col(col) for col in p_cols + [c_col])
    allexcept_cols = ", ".join(tdax + _dax_col(col) for col in p_cols)
    detail_measure = f"{agg_dax}({tdax}{_dax_col(field_col)})"
    dax = (
        "VAR __detail =\n"
        "    CALCULATETABLE(\n"
        "        ADDCOLUMNS(\n"
        f"            SUMMARIZE({tdax}, {summarize_cols}),\n"
        f'            "@value", CALCULATE({detail_measure})\n'
        "        ),\n"
        f"        ALLEXCEPT({tdax}, {allexcept_cols})\n"
        "    )\n"
        "VAR __max = MAXX(__detail, [@value])\n"
        "RETURN\n"
        f'    CONCATENATEX(FILTER(__detail, [@value] = __max), {tdax}{_dax_col(c_col)}, ", ")'
    )
    caveats = [
        "Emitted as a MEASURE (text), not the row-level dimension Tableau modeled -- "
        "the faithful Power BI shape for an argmax.",
        f"Ties: every {c_col} sharing the maximum is returned, comma-joined. Confirm this "
        "matches your intended tie handling (a single-value form uses TOPN/SELECTEDVALUE).",
        f"FIXED semantics mapped via ALLEXCEPT({table}, {', '.join(p_cols)}): respects current "
        f"filter context on the partition but ignores filters on {c_col}. Matches Tableau FIXED "
        "at totals/context filters; can differ under a viz dimension filter.",
    ]
    return {
        "pattern": "argmax-dimension",
        "dax": dax,
        "confidence": "medium",
        "requires_approval": True,
        "caveats": caveats,
    }


# Idiom registry. Each detector takes (formula, resolver, calc_lookup) and returns a
# suggestion dict or None. First match wins. Add new idioms here.
_ASSISTED_DETECTORS = (_detect_argmax_dimension,)


def suggest_assisted_dax(formula, resolver, *, calc_lookup=None):
    """Second-opinion idiom matcher for a calc the deterministic translator FELL BACK on.

    Returns a SUGGESTION dict (``pattern``, ``dax``, ``confidence``, ``requires_approval``,
    ``caveats``) for a human to approve, or ``None`` when no idiom matches. This is a
    fallback-only helper -- never call it in place of ``translate_tableau_calc_to_dax``; its
    output requires explicit human sign-off before it becomes a live measure.

    ``calc_lookup`` maps a lowercased calc reference (caption AND the internal ``Calculation_*``
    name) to its Tableau formula, so an idiom that references a SEPARATE calc (argmax pointing at
    a standalone "max" calc) can resolve it. Optional; omit it for self-contained formulas.
    """
    f = (formula or "").strip()
    if not f:
        return None
    lookup = {(k or "").lower(): v for k, v in (calc_lookup or {}).items()}
    for detector in _ASSISTED_DETECTORS:
        try:
            sugg = detector(f, resolver, lookup)
        except Exception:
            sugg = None
        if sugg:
            return sugg
    return None


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
