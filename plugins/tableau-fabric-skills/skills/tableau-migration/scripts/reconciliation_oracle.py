"""Local reconciliation oracle -- the *empirical* half of the second-compiler validation gate.

``check_candidate_dax`` (in :mod:`translation_router`) is the SYNTACTIC gate: it proves a candidate
is well-formed DAX that is not the inert stub. It cannot prove the candidate is *numerically
faithful* to the original Tableau calc. That empirical proof is what this module provides, offline,
against the data a local run already lands as CSVs (the extract / flat-file tables).

The strategy is deliberately conservative -- a false PASS (landing wrong DAX with confidence) is the
worst possible outcome, strictly worse than leaving a stub. So the oracle:

  * parses BOTH the original Tableau formula AND the candidate DAX into ONE shared canonical AST
    (two independent front-ends -> a wrong candidate diverges; a shared evaluator means edge-case
    arithmetic semantics never make two *equivalent* expressions disagree),
  * evaluates both over the real landed rows at the grand total (plus any caller-supplied grain),
  * returns ``pass`` ONLY when both sides parse inside a tight supported subset, evaluate without
    error, and agree over the data; a mismatch is ``fail``; anything else (out of subset, no data,
    unresolved reference, multi-table) is ``inconclusive`` -- which, under faithful-or-stub, keeps
    the stub.

Supported subset (v1): a measure that is an arithmetic combination (``+ - * /`` / ``DIVIDE``) of
single-column aggregations (``SUM/AVG/MIN/MAX/COUNT/COUNTD/MEDIAN`` and the ``*X`` row-iterator
forms) over ONE table, with ``ZN`` / ``IFNULL`` / ``COALESCE`` null handling and numeric literals.
The argmax/argmin-over-dimension idiom is handled separately by :func:`reconcile_argmax`. Everything
else -> ``inconclusive``.

Pure standard library (``csv`` + ``statistics``) so it runs everywhere and is fully unit-testable
offline; it never imports pandas/duckdb and never touches Tableau or Fabric.
"""
from __future__ import annotations

import csv
import math
import re
import statistics

PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"

# Aggregation function name (canonical) -> arity note. COUNTROWS is table-level (no column arg).
_AGG_FNS = {"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "MEDIAN", "COUNTROWS"}

# DAX function name -> canonical aggregation (single-column forms).
_DAX_AGG = {
    "SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX",
    "COUNT": "COUNT", "COUNTA": "COUNT", "DISTINCTCOUNT": "COUNTD", "MEDIAN": "MEDIAN",
}
# DAX row-iterator forms: FN(table, rowexpr) -> canonical aggregation over rowexpr.
_DAX_ITER = {"SUMX": "SUM", "AVERAGEX": "AVG", "MINX": "MIN", "MAXX": "MAX", "COUNTX": "COUNT"}

# Tableau aggregation spellings -> canonical.
_TABLEAU_AGG = {
    "SUM": "SUM", "AVG": "AVG", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX",
    "COUNT": "COUNT", "COUNTD": "COUNTD", "MEDIAN": "MEDIAN",
}


class _Unsupported(Exception):
    """Raised when a formula/candidate falls outside the oracle's supported subset."""


# --------------------------------------------------------------------------- canonical AST nodes
class Num:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = float(value)


class Col:
    """A row-level column reference ``(table, column)`` -- valid only inside an aggregation."""
    __slots__ = ("table", "column")

    def __init__(self, table, column):
        self.table = table
        self.column = column


class Agg:
    """A scalar aggregation over ``arg`` (a row-level expression); ``COUNTROWS`` has ``arg=None``."""
    __slots__ = ("fn", "arg", "table")

    def __init__(self, fn, arg, table=None):
        self.fn = fn
        self.arg = arg
        self.table = table


class Bin:
    __slots__ = ("op", "left", "right", "alt")

    def __init__(self, op, left, right, alt=None):
        self.op = op
        self.left = left
        self.right = right
        self.alt = alt  # DIVIDE's optional alternate result when the denominator is 0/blank


class Coalesce:
    """``ZN`` / ``IFNULL`` / ``COALESCE`` -- first non-null of its operands."""
    __slots__ = ("args",)

    def __init__(self, args):
        self.args = args


# --------------------------------------------------------------------------- tokenizer
_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<number>\d+\.\d+|\.\d+|\d+)
    | (?P<quoted>'(?:[^']|'')*')          # 'Table Name'  (DAX single-quoted table)
    | (?P<bracket>\[(?:[^\]]|\]\])*\])    # [Column]/[Field]/[Measure]
    | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)   # function name or bare table name
    | (?P<op>[()+\-*/,])
    """,
    re.VERBOSE,
)


def _tokenize(text):
    tokens = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise _Unsupported("unrecognized character %r" % text[pos])
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        val = m.group()
        if kind == "quoted":
            val = val[1:-1].replace("''", "'")
        elif kind == "bracket":
            val = val[1:-1].replace("]]", "]")
        tokens.append((kind, val))
    return tokens


class _Parser:
    """Shared recursive-descent core for the tiny arithmetic-of-aggregations grammar.

    Subclasses supply :meth:`_column` (how a column reference is spelled + resolved) and the set of
    recognized function names; everything else (precedence, aggregation shape, null-handling
    wrappers) is common so the Tableau and DAX front-ends produce byte-identical ASTs for equivalent
    expressions.
    """

    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def _next(self):
        tok = self._peek()
        self.i += 1
        return tok

    def _expect_op(self, op):
        kind, val = self._next()
        if kind != "op" or val != op:
            raise _Unsupported("expected %r" % op)

    def parse(self):
        node = self._expr()
        if self.i != len(self.toks):
            raise _Unsupported("trailing tokens")
        return node

    def _expr(self):
        node = self._term()
        while True:
            kind, val = self._peek()
            if kind == "op" and val in ("+", "-"):
                self._next()
                node = Bin(val, node, self._term())
            else:
                return node

    def _term(self):
        node = self._factor()
        while True:
            kind, val = self._peek()
            if kind == "op" and val in ("*", "/"):
                self._next()
                node = Bin(val, node, self._factor())
            else:
                return node

    def _factor(self):
        kind, val = self._peek()
        if kind == "op" and val == "-":
            self._next()
            return Bin("-", Num(0), self._factor())
        if kind == "op" and val == "+":
            self._next()
            return self._factor()
        if kind == "op" and val == "(":
            self._next()
            node = self._expr()
            self._expect_op(")")
            return node
        if kind == "number":
            self._next()
            return Num(val)
        if kind == "name":
            return self._name_lead()
        if kind in ("bracket", "quoted"):
            # A bare column at scalar position is not a measure -- reject (keeps top-level scalar).
            raise _Unsupported("bare column at scalar position")
        raise _Unsupported("unexpected token %r" % (val,))

    def _args(self):
        """Parse ``( a, b, ... )`` -> list of row/scalar expressions."""
        self._expect_op("(")
        args = []
        kind, val = self._peek()
        if kind == "op" and val == ")":
            self._next()
            return args
        args.append(self._expr())
        while True:
            kind, val = self._peek()
            if kind == "op" and val == ",":
                self._next()
                args.append(self._expr())
            elif kind == "op" and val == ")":
                self._next()
                return args
            else:
                raise _Unsupported("malformed argument list")

    # -- hooks overridden per front-end -----------------------------------------------------------
    def _name_lead(self):  # pragma: no cover - overridden
        raise _Unsupported("name")


def _row_expr_ok(node):
    """A validated row-level expression: Col/Num/Bin/Coalesce, NO aggregation inside."""
    if isinstance(node, Agg):
        raise _Unsupported("aggregation nested inside an aggregation")
    if isinstance(node, Bin):
        _row_expr_ok(node.left)
        _row_expr_ok(node.right)
    elif isinstance(node, Coalesce):
        for a in node.args:
            _row_expr_ok(a)
    return node


class _DaxParser(_Parser):
    def __init__(self, tokens, default_table=None):
        super().__init__(tokens)
        self.default_table = default_table

    def _column_from(self, table):
        kind, val = self._next()
        if kind != "bracket":
            raise _Unsupported("expected [column]")
        return Col(table, val)

    def _name_lead(self):
        kind, val = self._next()
        upper = val.upper()
        nxt_kind, nxt_val = self._peek()
        if nxt_kind == "op" and nxt_val == "(":
            return self._func(upper)
        if nxt_kind == "bracket":
            # Table[Column]
            return self._column_from(val)
        raise _Unsupported("bare name %r" % val)

    def _func(self, fn):
        if fn in _DAX_AGG:
            args = self._args()
            if len(args) != 1:
                raise _Unsupported("%s expects one column" % fn)
            return Agg(_DAX_AGG[fn], _row_expr_ok(args[0]))
        if fn in _DAX_ITER:
            # FN(table, rowexpr): the first arg is a bare table reference, not 'Table'[Column].
            self._expect_op("(")
            tkind, tval = self._next()
            if tkind not in ("quoted", "name"):
                raise _Unsupported("%s expects (table, expr)" % fn)
            self._expect_op(",")
            saved = self.default_table
            self.default_table = tval  # bare [Col] in the row expr resolves against this table
            try:
                expr = self._expr()
            finally:
                self.default_table = saved
            self._expect_op(")")
            return Agg(_DAX_ITER[fn], _row_expr_ok(expr))
        if fn == "COUNTROWS":
            args = self._args_tableref()
            return Agg("COUNTROWS", None, table=args)
        if fn == "DIVIDE":
            args = self._args()
            if len(args) not in (2, 3):
                raise _Unsupported("DIVIDE expects 2 or 3 args")
            return Bin("/", args[0], args[1], alt=(args[2] if len(args) == 3 else None))
        if fn == "COALESCE":
            args = self._args()
            if not args:
                raise _Unsupported("COALESCE expects args")
            return Coalesce(args)
        raise _Unsupported("unsupported DAX function %r" % fn)

    def _args_tableref(self):
        """COUNTROWS('Table') / COUNTROWS(Table) -> the referenced table name."""
        self._expect_op("(")
        kind, val = self._next()
        if kind not in ("quoted", "name"):
            raise _Unsupported("COUNTROWS expects a table")
        self._expect_op(")")
        return val

    def _factor(self):
        # Support 'Table'[Column] (quoted table lead) before the shared factor rules.
        kind, val = self._peek()
        if kind == "quoted":
            self._next()
            return self._column_from(val)
        if kind == "bracket":
            # bare [Column] inside a *X iterator -> resolve against the default table.
            if self.default_table is None:
                raise _Unsupported("bare [column] with no table context")
            _, col = self._next()
            return Col(self.default_table, col)
        return super()._factor()


class _TableauParser(_Parser):
    def __init__(self, tokens, resolver):
        super().__init__(tokens)
        self.resolver = resolver

    def _resolve(self, caption):
        if self.resolver is None:
            raise _Unsupported("no resolver for [%s]" % caption)
        hit = self.resolver(caption)
        if not hit or len(hit) < 2 or not hit[0] or not hit[1]:
            raise _Unsupported("unresolved field [%s]" % caption)
        return Col(hit[0], hit[1])

    def _name_lead(self):
        kind, val = self._next()
        upper = val.upper()
        nxt_kind, nxt_val = self._peek()
        if nxt_kind == "op" and nxt_val == "(":
            return self._func(upper)
        raise _Unsupported("bare name %r" % val)

    def _func(self, fn):
        if fn in _TABLEAU_AGG:
            args = self._args()
            if len(args) != 1:
                raise _Unsupported("%s expects one expression" % fn)
            return Agg(_TABLEAU_AGG[fn], _row_expr_ok(args[0]))
        if fn == "ZN":
            args = self._args()
            if len(args) != 1:
                raise _Unsupported("ZN expects one arg")
            return Coalesce([args[0], Num(0)])
        if fn == "IFNULL":
            args = self._args()
            if len(args) != 2:
                raise _Unsupported("IFNULL expects two args")
            return Coalesce(args)
        raise _Unsupported("unsupported Tableau function %r" % fn)

    def _factor(self):
        kind, val = self._peek()
        if kind == "bracket":
            self._next()
            return self._resolve(val)
        return super()._factor()


# --------------------------------------------------------------------------- evaluation
def _to_number(value):
    """Coerce a cell to float, or None when it is blank/non-numeric."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        f = float(value)
        return None if math.isnan(f) else f
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _eval_row(node, row):
    """Evaluate a row-level expression against a single row dict -> float or None."""
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Col):
        return _to_number(row.get(node.column))
    if isinstance(node, Coalesce):
        for a in node.args:
            v = _eval_row(a, row)
            if v is not None:
                return v
        return None
    if isinstance(node, Bin):
        return _combine(node, _eval_row(node.left, row), _eval_row(node.right, row))
    raise _Unsupported("non row-level node in row context")


def _raw_cell(node, row):
    """Raw (uncoerced) cell for COUNT/COUNTD, which count non-blank values incl. text."""
    if isinstance(node, Col):
        v = row.get(node.column)
        if v is None:
            return None
        s = v if not isinstance(v, str) else v.strip()
        return None if s == "" else s
    return _eval_row(node, row)


def _combine(node, a, b):
    op = node.op
    if op == "/":
        if b is None or b == 0:
            return node.alt.value if isinstance(node.alt, Num) else None
        if a is None:
            return None
        return a / b
    if a is None or b is None:
        return None
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    raise _Unsupported("op %r" % op)


def _aggregate(node, rows):
    fn = node.fn
    if fn == "COUNTROWS":
        return float(len(rows))
    if fn in ("COUNT", "COUNTD"):
        vals = [_raw_cell(node.arg, r) for r in rows]
        vals = [v for v in vals if v is not None]
        return float(len(set(vals))) if fn == "COUNTD" else float(len(vals))
    vals = [_eval_row(node.arg, r) for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    if fn == "SUM":
        return float(sum(vals))
    if fn == "AVG":
        return statistics.fmean(vals)
    if fn == "MIN":
        return float(min(vals))
    if fn == "MAX":
        return float(max(vals))
    if fn == "MEDIAN":
        return float(statistics.median(vals))
    raise _Unsupported("aggregation %r" % fn)


def _eval_scalar(node, rows):
    """Evaluate a scalar measure AST over a set of rows -> float or None."""
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Agg):
        return _aggregate(node, rows)
    if isinstance(node, Coalesce):
        for a in node.args:
            v = _eval_scalar(a, rows)
            if v is not None:
                return v
        return None
    if isinstance(node, Bin):
        return _combine(node, _eval_scalar(node.left, rows), _eval_scalar(node.right, rows))
    if isinstance(node, Col):
        raise _Unsupported("bare column is not a measure")
    raise _Unsupported("unsupported node")


# --------------------------------------------------------------------------- table / verdict utils
def _normalize_tables(tables):
    out = {}
    for name, tbl in (tables or {}).items():
        if isinstance(tbl, dict) and "rows" in tbl:
            rows = list(tbl["rows"])
            cols = list(tbl.get("columns") or (rows[0].keys() if rows else []))
        else:  # a bare list of row dicts
            rows = list(tbl)
            cols = list(rows[0].keys()) if rows else []
        out[name] = {"columns": cols, "rows": rows}
    return out


def _tables_of(node, acc):
    if isinstance(node, Col):
        if node.table:
            acc.add(node.table)
    elif isinstance(node, Agg):
        if node.table:
            acc.add(node.table)
        if node.arg is not None:
            _tables_of(node.arg, acc)
    elif isinstance(node, Bin):
        _tables_of(node.left, acc)
        _tables_of(node.right, acc)
    elif isinstance(node, Coalesce):
        for a in node.args:
            _tables_of(a, acc)
    return acc


def _columns_of(node, acc):
    """Column names referenced (as measure inputs) by an AST -- excluded from auto-grain dims."""
    if isinstance(node, Col):
        if node.column:
            acc.add(node.column)
    elif isinstance(node, Agg):
        if node.arg is not None:
            _columns_of(node.arg, acc)
    elif isinstance(node, Bin):
        _columns_of(node.left, acc)
        _columns_of(node.right, acc)
    elif isinstance(node, Coalesce):
        for a in node.args:
            _columns_of(a, acc)
    return acc


def _single_table_of(node):
    tables = _tables_of(node, set())
    if len(tables) == 1:
        return next(iter(tables))
    return None


def _auto_grain(tbl, used, *, max_cols=8, max_distinct=50):
    """Pick low-cardinality, non-measure columns to reconcile per group.

    Without this, a candidate that only *coincidentally* equals the Tableau formula at the grand
    total would PASS. Grouping by each dimension independently makes such a coincidence hold across
    many groups -- astronomically unlikely for two genuinely different functions over real data --
    so a wrong candidate is caught. A truly-equivalent candidate agrees at every grain, so PASS is
    never lost.
    """
    rows = tbl["rows"]
    limit = min(max_distinct, max(2, len(rows) // 2))
    out = []
    for c in tbl["columns"]:
        if c in used:
            continue
        seen = set()
        ok = True
        for r in rows:
            v = r.get(c)
            if v is None:
                continue
            s = v.strip() if isinstance(v, str) else v
            if s == "":
                continue
            seen.add(s)
            if len(seen) > limit:
                ok = False
                break
        if ok and 2 <= len(seen) <= limit:
            out.append(c)
        if len(out) >= max_cols:
            break
    return out


def _num_eq(a, b, tol):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    fa, fb = float(a), float(b)
    if math.isnan(fa) and math.isnan(fb):
        return True
    return abs(fa - fb) <= tol + tol * max(abs(fa), abs(fb))


def _verdict(status, reason, **extra):
    v = {"status": status, "reason": reason}
    v.update(extra)
    return v


def _resolve_dims(grain, resolver, table, tbl):
    """Map a grain spec (captions or (table,col)) to column names in ``table``."""
    dims = []
    for item in grain:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            t, c = item[0], item[1]
        elif resolver is not None:
            hit = resolver(item)
            if not hit or len(hit) < 2:
                raise _Unsupported("unresolved grain dimension %r" % (item,))
            t, c = hit[0], hit[1]
        else:
            t, c = table, item
        if t != table:
            raise _Unsupported("grain dimension %r not in table %r" % (c, table))
        if c not in tbl["columns"] and (not tbl["rows"] or c not in tbl["rows"][0]):
            raise _Unsupported("grain column %r missing from landed data" % c)
        dims.append(c)
    return dims


def _group_rows(rows, dims):
    groups = {}
    for r in rows:
        key = tuple(r.get(d) for d in dims)
        groups.setdefault(key, []).append(r)
    return groups


def reconcile(tableau_formula, candidate_dax, tables, *, resolver=None, grain=None,
              tolerance=1e-9, max_rows=500000):
    """Empirically reconcile ``candidate_dax`` against ``tableau_formula`` over landed ``tables``.

    ``tables`` maps a model table name to either ``{"columns": [...], "rows": [ {col: val} ]}`` or a
    bare list of row dicts. ``resolver(caption) -> (table, column, ...)`` maps a Tableau field
    caption to its model column (needed for the Tableau side). ``grain`` optionally lists dimensions
    (captions or ``(table, column)``) to also reconcile per group.

    Returns a verdict dict ``{"status": pass|fail|inconclusive, "reason": str, ...}``. ``pass`` means
    both sides parsed inside the supported subset and agreed over the real data at every evaluated
    grain; ``fail`` means they diverged; ``inconclusive`` means the oracle could not decide (out of
    subset, missing/empty data, unresolved or multi-table references) -- keep the stub.
    """
    tables = _normalize_tables(tables)

    try:
        t_ast = _TableauParser(_tokenize(tableau_formula or ""), resolver).parse()
    except _Unsupported as exc:
        return _verdict(INCONCLUSIVE, "tableau formula outside oracle subset: %s" % exc)
    t_tbl = _single_table_of(t_ast)
    try:
        d_ast = _DaxParser(_tokenize(candidate_dax or ""), default_table=t_tbl).parse()
    except _Unsupported as exc:
        return _verdict(INCONCLUSIVE, "candidate DAX outside oracle subset: %s" % exc)

    d_tbl = _single_table_of(d_ast)
    if t_tbl is None or d_tbl is None:
        return _verdict(INCONCLUSIVE, "measure references zero or multiple tables (v1 is single-table)")
    if t_tbl != d_tbl:
        return _verdict(
            INCONCLUSIVE,
            "candidate references table %r but the Tableau formula references %r" % (d_tbl, t_tbl))

    tbl = tables.get(t_tbl)
    if not tbl or not tbl["rows"]:
        return _verdict(INCONCLUSIVE, "no landed data for table %r" % t_tbl)
    if len(tbl["rows"]) > max_rows:
        return _verdict(INCONCLUSIVE, "table %r has more than max_rows=%d rows" % (t_tbl, max_rows))
    rows = tbl["rows"]

    try:
        dims = _resolve_dims(grain, resolver, t_tbl, tbl) if grain else []
    except _Unsupported as exc:
        return _verdict(INCONCLUSIVE, "grain outside oracle subset: %s" % exc)

    # Grand total is always compared; either the caller's grain (one combined grouping) or, when
    # none was given, each auto-picked dimension on its own strengthens it against a coincidental
    # total-only match.
    groupings = [[]]
    if dims:
        groupings.append(dims)
    else:
        used = _columns_of(t_ast, set()) | _columns_of(d_ast, set())
        groupings.extend([c] for c in _auto_grain(tbl, used))

    comparable = 0
    for gcols in groupings:
        grouped = _group_rows(rows, gcols) if gcols else {(): rows}
        for key, grp in sorted(grouped.items(), key=lambda kv: repr(kv[0])):
            tv = _safe_scalar(t_ast, grp)
            dv = _safe_scalar(d_ast, grp)
            if tv is _EVAL_ERROR or dv is _EVAL_ERROR:
                return _verdict(INCONCLUSIVE, "evaluation error over table %r" % t_tbl)
            if tv is None or dv is None:
                continue  # skip a grain that is null on either side (e.g. an empty group)
            comparable += 1
            if not _num_eq(tv, dv, tolerance):
                where = "grand total" if not gcols else "%s=%r" % (", ".join(gcols), key)
                return _verdict(
                    FAIL,
                    "values differ at %s: tableau=%r candidate=%r" % (where, tv, dv),
                    grain=where, tableau_value=tv, candidate_value=dv,
                    rows=len(rows), groups_compared=comparable)
    if comparable == 0:
        return _verdict(INCONCLUSIVE, "no comparable (non-null) grain to reconcile against")
    return _verdict(
        PASS,
        "candidate matches the Tableau formula over %d row(s) across %d grain(s)"
        % (len(rows), comparable),
        rows=len(rows), groups_compared=comparable)


# --------------------------------------------------------------------------- landed-CSV loader
def load_tables_from_csv(table_csv_paths, column_map=None, *, max_rows=None, encoding="utf-8-sig"):
    """Load landed CSV files into the ``tables`` shape :func:`reconcile` consumes.

    ``table_csv_paths`` maps a model table name to the path of the CSV a local run landed for it
    (``migrate_estate`` writes these under ``out/data/<ds>/``). Each CSV's header row supplies the
    column names; every value is kept as the raw string ``csv`` yields (the evaluator coerces to a
    number only where an aggregation needs one, so text dimensions survive for grouping).

    A landed CSV's headers are the extract's *physical* names, whereas the resolver and the candidate
    DAX speak the model's *sanitized* column names. ``column_map`` -- an optional
    ``{model_table: {csv_header: model_column}}`` -- renames headers to those model names so both
    sides line up; a header absent from the map (or a table with no map) is kept verbatim. Building
    that map from the datasource descriptor is the wiring layer's job, so this stays a pure,
    stdlib-only primitive.

    ``max_rows`` optionally caps rows read per table (a light guard for very large extracts). Returns
    ``{model_table: {"columns": [...], "rows": [ {col: val} ]}}`` ready to hand straight to
    :func:`reconcile`.
    """
    tables = {}
    for table, path in (table_csv_paths or {}).items():
        rename = (column_map or {}).get(table) or {}
        with open(path, "r", encoding=encoding, newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                tables[table] = {"columns": [], "rows": []}
                continue
            columns = [rename.get(h, h) for h in header]
            rows = []
            for raw in reader:
                if max_rows is not None and len(rows) >= max_rows:
                    break
                # tolerate ragged rows: pad short, ignore overflow cells
                row = {}
                for i, col in enumerate(columns):
                    row[col] = raw[i] if i < len(raw) else None
                rows.append(row)
        tables[table] = {"columns": columns, "rows": rows}
    return tables


_EVAL_ERROR = object()


def _safe_scalar(node, rows):
    try:
        return _eval_scalar(node, rows)
    except _Unsupported:
        return _EVAL_ERROR
