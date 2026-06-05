"""Deterministic Tableau calculated-field -> DAX measure translator (no LLM).

Ported verbatim from the Tableau-Fabric-AI-Bridge Play 4 notebook (cell 3b). The
logic is unchanged; only the module-level ``import re`` and the ``__main__`` guard
were added so it runs as a standalone, offline-testable module.

Translates a SAFE subset of Tableau calculated fields into working DAX measures:
  * aggregations over a single bare field: SUM, AVG, MIN, MAX, COUNT, COUNTD, MEDIAN
  * arithmetic between those terms / numeric literals: + - * /, parentheses, unary minus

Anything outside this subset (IF/CASE, LOD {FIXED/...}, string/date/window funcs,
nested arithmetic inside an aggregation, references to other calcs, unresolved or
ambiguous fields, cross-table arithmetic) deterministically FALLS BACK by returning
``None`` so the caller can keep an inert ``= 0`` stub. The original Tableau formula is
preserved as an annotation by the renderer either way.
"""
from __future__ import annotations

import re

_AGG_MAP = {
    "SUM": "SUM", "AVG": "AVERAGE", "MIN": "MIN", "MAX": "MAX",
    "MEDIAN": "MEDIAN", "COUNT": "COUNTA", "COUNTD": "DISTINCTCOUNTNOBLANK",
}
# COUNT  -> COUNTA               (Tableau COUNT = non-null of ANY type; DAX COUNT errors on text)
# COUNTD -> DISTINCTCOUNTNOBLANK (plain DISTINCTCOUNT counts BLANK -> off-by-one vs Tableau)
_NUMERIC_TYPES = {"int64", "double", "decimal"}


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
        if c in "+-*/()":
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


# Recursive-descent parser with correct DAX/arithmetic precedence:
#   expr := add ; add := mul (('+'|'-') mul)* ; mul := unary (('*'|'/') unary)*
#   unary := '-' unary | primary ; primary := agg | number | '(' expr ')'
#   agg := AGGFUNC '(' '[' fieldref ']' ')'
class _Parser:
    def __init__(self, toks, resolver, tables_used):
        self.toks = toks
        self.pos = 0
        self.resolver = resolver
        self.tables_used = tables_used

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

    def parse(self):
        node = self._add()
        if self.pos != len(self.toks):
            raise _CalcError("unexpected trailing tokens")
        return node

    def _add(self):
        left = self._mul()
        while self._peek() == ("op", "+") or self._peek() == ("op", "-"):
            op = self._next()[1]
            right = self._mul()
            left = f"{left} {op} {right}"
        return left

    def _mul(self):
        left = self._unary()
        while self._peek() == ("op", "*") or self._peek() == ("op", "/"):
            op = self._next()[1]
            right = self._unary()
            left = f"DIVIDE({left}, {right})" if op == "/" else f"{left} * {right}"
        return left

    def _unary(self):
        if self._peek() == ("op", "-"):
            self._next()
            operand = self._unary()
            return f"-({operand})"  # parenthesize so '--' never forms a DAX comment
        return self._primary()

    def _primary(self):
        k, v = self._peek()
        if k == "id":
            return self._agg()
        if k == "num":
            self._next()
            return _norm_number(v)
        if k == "op" and v == "(":
            self._next()
            inner = self._add()
            self._expect_op(")")
            return f"({inner})"
        raise _CalcError("expected aggregation, number, or '('")

    def _agg(self):
        name = self._next()[1].upper()
        if name not in _AGG_MAP:
            raise _CalcError(f"unsupported function {name}")
        self._expect_op("(")
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
        if name in ("SUM", "AVG", "MEDIAN") and tmdl_type not in _NUMERIC_TYPES:
            raise _CalcError(f"{name} requires a numeric field, got {tmdl_type} for [{v}]")
        if name in ("MIN", "MAX") and tmdl_type not in (_NUMERIC_TYPES | {"dateTime"}):
            raise _CalcError(f"{name} requires a numeric/date field, got {tmdl_type} for [{v}]")
        self.tables_used.add(table)
        return f"{_AGG_MAP[name]}({_dax_table(table)}{_dax_col(col)})"


def translate_tableau_calc_to_dax(formula, resolver):
    """Translate a SIMPLE Tableau calc to DAX. Returns (dax|None, reason, tables_used).

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
        dax = _Parser(toks, resolver, tables_used).parse()
        # Single-table only: terms spanning >1 table fall back (a relationship path
        # does not guarantee the DAX filter context reproduces Tableau's result).
        if len(tables_used) > 1:
            return None, "cross-table arithmetic (terms span multiple tables)", tables_used
        return dax, "ok", tables_used
    except _CalcError as e:
        return None, str(e), tables_used


if __name__ == "__main__":
    _demo = {
        "Profit": ("Orders", "Profit", "decimal"),
        "Sales": ("Orders", "Sales", "decimal"),
    }
    _r = lambda cap: _demo.get(cap)
    print(translate_tableau_calc_to_dax("SUM([Profit])/SUM([Sales])", _r))
