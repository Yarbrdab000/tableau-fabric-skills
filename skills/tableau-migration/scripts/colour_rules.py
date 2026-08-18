"""Conditional-colour compiler -- FRONT END.

Tableau's most common conditional-formatting idiom is a calculation that outputs a **string /
dimension value**, placed on the Colour shelf, with each distinct output painted its own colour::

    <arbitrarily complex numeric logic>  ->  STRING member  ->  colour per member
              the predicates                  the legend         the palette

The key structural fact -- and the reason this is worth compiling rather than pattern-matching -- is
that **the string output is an intermediate Power BI never needs**::

    IF p1 THEN "A" ELSEIF p2 THEN "B" ELSE "C" END
        ==>  Cases = [ p1 -> colour("A"), p2 -> colour("B") ],  Default = colour("C")

The members collapse into the palette. Nothing is added to the model: no string measure, no colour
twin. All the difficulty therefore lives in the PREDICATES, and depth costs nothing because
`Conditional.Cases` is an ordered list that a nested `IF`/`ELSEIF` chain flattens onto 1:1.

This module does the ANALYSIS half only. It answers three questions about a Tableau formula and
takes no view on how the answer is emitted:

1. **What are the branches?** An ordered list of ``(predicate, member)`` plus a default member,
   flattened out of arbitrarily nested ``IF`` / ``ELSEIF`` / ``CASE`` forms.
2. **Is the member domain CLOSED?** Every outcome a string literal (so a static palette can be
   built), or does an outcome return DATA -- ``IF x THEN [Category] ELSE "Other" END`` -- in which
   case no static colour map exists and a different mechanism is required.
3. **What SCOPE does each predicate need?** The lattice below. The maximum over all predicates is
   what selects the lowering target, and it is computed from the formula's own properties rather
   than matched against a template -- so a calculation this module has never seen still routes.

Scope lattice, lowest to highest::

    constant  < parameter < row < aggregate < lod < view

``view`` is the one that changes everything: ``WINDOW_*`` / ``RUNNING_*`` / ``RANK`` / ``INDEX`` /
``TOTAL`` / ``FIRST`` / ``LAST`` compare a mark against the OTHER MARKS IN THE VIEW. Measured on
``0070_new_max``, lowering such a predicate to a model measure produces a comparison that never
aligns with the visual's axis and is false on every mark -- so scope is not a nicety, it decides
whether the rebuild is correct at all.

Fail-closed throughout: anything this module cannot read returns ``supported=False`` with a reason,
never a half-parsed guess.
"""
from __future__ import annotations

import re

try:  # package-relative first, matching the rest of the scripts folder
    from . import calc_to_dax as _C
except ImportError:  # pragma: no cover - direct-script execution
    import calc_to_dax as _C


# -- scope lattice ---------------------------------------------------------------------------
SCOPE_CONSTANT = "constant"
SCOPE_PARAMETER = "parameter"
SCOPE_ROW = "row"
SCOPE_AGGREGATE = "aggregate"
SCOPE_LOD = "lod"
SCOPE_VIEW = "view"

SCOPE_ORDER = (SCOPE_CONSTANT, SCOPE_PARAMETER, SCOPE_ROW,
               SCOPE_AGGREGATE, SCOPE_LOD, SCOPE_VIEW)


def scope_max(*scopes):
    """The least upper bound of ``scopes`` in :data:`SCOPE_ORDER` (``constant`` for none)."""
    best = 0
    for s in scopes:
        if s in SCOPE_ORDER:
            best = max(best, SCOPE_ORDER.index(s))
    return SCOPE_ORDER[best]


# Functions that compute ACROSS THE MARKS OF THE VIEW. Deliberately a superset of the quick-calc
# codes: a hand-written formula reaches these by name, and it is the NAME that makes the predicate
# view-scoped regardless of how the pill was authored.
_VIEW_FNS = frozenset("""
    WINDOW_SUM WINDOW_AVG WINDOW_MIN WINDOW_MAX WINDOW_COUNT WINDOW_MEDIAN WINDOW_STDEV
    WINDOW_STDEVP WINDOW_VAR WINDOW_VARP WINDOW_PERCENTILE WINDOW_CORR WINDOW_COVAR
    RUNNING_SUM RUNNING_AVG RUNNING_MIN RUNNING_MAX RUNNING_COUNT
    TOTAL RANK RANK_DENSE RANK_MODIFIED RANK_PERCENTILE RANK_UNIQUE
    INDEX SIZE FIRST LAST LOOKUP PREVIOUS_VALUE
    SCRIPT_BOOL SCRIPT_INT SCRIPT_REAL SCRIPT_STR
""".split())

# Row-level aggregates. Their presence lifts a predicate to ``aggregate``: the comparison is between
# aggregated values per mark, which is expressible in the visual's own semantic query.
_AGG_FNS = frozenset("""
    SUM AVG MIN MAX COUNT COUNTD MEDIAN ATTR STDEV STDEVP VAR VARP PERCENTILE
""".split())

_LOD_RE = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.IGNORECASE)

_IF_OPENERS = frozenset({"IF", "CASE"})


def _kw(tok):
    """The upper-cased keyword an ``id`` token carries, else ``None``."""
    return tok[1].upper() if tok and tok[0] == "id" else None


def _split_structural(toks, words):
    """Index of the first token in ``words`` at structural depth 0, else ``-1``.

    Depth counts BOTH bracketing (``(`` / ``{``) and Tableau's block forms (``IF`` / ``CASE`` …
    ``END``), so a nested conditional in a THEN/ELSE arm never captures the outer scan.
    """
    depth = 0
    for i, t in enumerate(toks):
        if t[0] == "op" and t[1] in "({":
            depth += 1
        elif t[0] == "op" and t[1] in ")}":
            depth -= 1
        else:
            k = _kw(t)
            if k in _IF_OPENERS:
                depth += 1
            elif k == "END":
                depth -= 1
            elif depth == 0 and k in words:
                return i
    return -1


def scope_of(toks):
    """The scope a predicate/expression token list requires."""
    scope = SCOPE_CONSTANT
    if _LOD_RE.search(_render(toks)):
        scope = scope_max(scope, SCOPE_LOD)
    for t in toks:
        if t[0] == "qfield":
            head = (t[1][0] if t[1] else "").strip().lower()
            scope = scope_max(scope, SCOPE_PARAMETER if head in ("parameters", "parameter")
                              else SCOPE_ROW)
        elif t[0] == "field":
            scope = scope_max(scope, SCOPE_ROW)
        elif t[0] == "id":
            name = t[1].upper()
            if name in _VIEW_FNS:
                scope = scope_max(scope, SCOPE_VIEW)
            elif name in _AGG_FNS:
                scope = scope_max(scope, SCOPE_AGGREGATE)
    return scope


def _render(toks):
    """A flat, readable rendering of a token list -- for scope regexes and disclosure text."""
    out = []
    for t in toks:
        if t[0] == "field":
            out.append("[%s]" % t[1])
        elif t[0] == "qfield":
            out.append(".".join("[%s]" % p for p in t[1]))
        elif t[0] == "str":
            out.append('"%s"' % t[1])
        else:
            out.append(str(t[1]))
    return " ".join(out)


class ColourBranch(object):
    """One ``(predicate -> member)`` arm. ``predicate`` is ``None`` for the default arm."""

    __slots__ = ("predicate", "member", "member_tokens")

    def __init__(self, predicate, member, member_tokens):
        self.predicate = predicate
        self.member = member
        self.member_tokens = member_tokens

    @property
    def scope(self):
        return scope_of(self.predicate or [])

    def __repr__(self):  # pragma: no cover - debugging aid
        return "ColourBranch(%r -> %r)" % (
            _render(self.predicate) if self.predicate else "(default)", self.member)


class ColourRuleSpec(object):
    """The analysed form of a colour-driving calculation.

    ``supported`` is False with a ``reason`` whenever the formula could not be read as an ordered
    branch chain over a closed member domain -- the caller then keeps whatever fallback it has,
    rather than acting on a partial parse.
    """

    __slots__ = ("branches", "default", "supported", "reason", "formula")

    def __init__(self, branches=(), default=None, supported=False, reason=None, formula=""):
        self.branches = list(branches)
        self.default = default
        self.supported = supported
        self.reason = reason
        self.formula = formula

    @property
    def members(self):
        """Distinct outcome members in first-appearance order (the legend, ordered as authored)."""
        out, seen = [], set()
        for b in self.branches:
            if b.member is not None and b.member not in seen:
                seen.add(b.member)
                out.append(b.member)
        if self.default is not None and self.default not in seen:
            out.append(self.default)
        return out

    @property
    def closed_domain(self):
        """True when EVERY outcome is a string literal, so a static palette can be built.

        ``IF x THEN [Category] ELSE "Other" END`` is not closed: its members are data, discoverable
        only at query time, so no static member->colour map exists.
        """
        return all(b.member is not None for b in self.branches) and self.default is not None

    @property
    def scope(self):
        """The least upper bound over every predicate -- what selects the lowering target."""
        return scope_max(*(b.scope for b in self.branches)) if self.branches else SCOPE_CONSTANT

    def __repr__(self):  # pragma: no cover - debugging aid
        if not self.supported:
            return "ColourRuleSpec(unsupported: %s)" % self.reason
        return "ColourRuleSpec(%d branch(es), default=%r, scope=%s, closed=%s)" % (
            len(self.branches), self.default, self.scope, self.closed_domain)


def _member_of(toks):
    """The literal member an outcome arm yields, or ``None`` when the outcome is DATA."""
    return toks[0][1] if len(toks) == 1 and toks[0][0] == "str" else None


def _unsupported(reason, formula):
    return ColourRuleSpec(supported=False, reason=reason, formula=formula)


def analyse_colour_calc(formula):
    """Analyse a Tableau colour calculation into a :class:`ColourRuleSpec`.

    Reads ``IF``/``ELSEIF``/``ELSE`` chains and ``CASE``/``WHEN`` chains, at any nesting depth in
    the ELSE position, and normalises both to one ordered branch list. A ``CASE <subject> WHEN <v>``
    arm becomes the predicate ``<subject> = <v>`` so every branch carries a uniform comparison and
    downstream code never needs to know which surface form it came from.
    """
    formula = formula or ""
    if not formula.strip():
        return _unsupported("empty formula", formula)
    try:
        toks = _C._tokenize(formula)
    except Exception as exc:                                  # noqa: BLE001 - fail closed
        return _unsupported("could not tokenize (%s)" % type(exc).__name__, formula)
    branches, default, reason = _parse_chain(toks)
    if reason:
        return _unsupported(reason, formula)
    if not branches:
        return _unsupported("no conditional branches", formula)
    return ColourRuleSpec(branches=branches, default=default, supported=True, formula=formula)


def _strip_end(toks):
    """Drop one trailing ``END`` if present (Tableau allows it to be the last token)."""
    return toks[:-1] if toks and _kw(toks[-1]) == "END" else toks


def _parse_chain(toks):
    """``(branches, default_member, reason)`` for an IF/CASE chain; ``reason`` non-None on failure."""
    toks = _strip_end(list(toks))
    if not toks:
        return [], None, "empty expression"
    head = _kw(toks[0])
    if head == "IF":
        return _parse_if(toks[1:])
    if head == "CASE":
        return _parse_case(toks[1:])
    return [], None, "not a conditional expression (no IF/CASE)"


def _parse_if(toks):
    branches = []
    while True:
        i_then = _split_structural(toks, {"THEN"})
        if i_then < 0:
            return [], None, "IF without THEN"
        predicate = toks[:i_then]
        if not predicate:
            return [], None, "IF with an empty condition"
        rest = toks[i_then + 1:]
        i_next = _split_structural(rest, {"ELSEIF", "ELSE"})
        if i_next < 0:
            # no ELSE arm: Tableau yields NULL, which paints nothing -- a real, colourable member
            outcome = _strip_end(rest)
            branches.append(ColourBranch(predicate, _member_of(outcome), outcome))
            return branches, None, None
        outcome = rest[:i_next]
        branches.append(ColourBranch(predicate, _member_of(outcome), outcome))
        kw_next = _kw(rest[i_next])
        tail = rest[i_next + 1:]
        if kw_next == "ELSEIF":
            toks = tail
            continue
        # ELSE: either a nested conditional (flatten it into this same chain) or the default arm
        tail = _strip_end(tail)
        if tail and _kw(tail[0]) in _IF_OPENERS:
            nested, nested_default, reason = _parse_chain(tail)
            if reason:
                return [], None, reason
            branches.extend(nested)
            return branches, nested_default, None
        return branches, _member_of(tail), None


def _parse_case(toks):
    i_when = _split_structural(toks, {"WHEN"})
    if i_when < 0:
        return [], None, "CASE without WHEN"
    subject = toks[:i_when]
    if not subject:
        return [], None, "CASE with an empty subject"
    branches = []
    rest = toks[i_when + 1:]
    while True:
        i_then = _split_structural(rest, {"THEN"})
        if i_then < 0:
            return [], None, "CASE WHEN without THEN"
        value = rest[:i_then]
        # normalise to a uniform comparison so downstream code sees one predicate shape
        predicate = list(subject) + [("cmp", "=")] + list(value)
        rest = rest[i_then + 1:]
        i_next = _split_structural(rest, {"WHEN", "ELSE"})
        if i_next < 0:
            outcome = _strip_end(rest)
            branches.append(ColourBranch(predicate, _member_of(outcome), outcome))
            return branches, None, None
        outcome = rest[:i_next]
        branches.append(ColourBranch(predicate, _member_of(outcome), outcome))
        kw_next = _kw(rest[i_next])
        rest = rest[i_next + 1:]
        if kw_next == "WHEN":
            continue
        rest = _strip_end(rest)
        if rest and _kw(rest[0]) in _IF_OPENERS:
            nested, nested_default, reason = _parse_chain(rest)
            if reason:
                return [], None, reason
            branches.extend(nested)
            return branches, nested_default, None
        return branches, _member_of(rest), None


# =============================================================================================
# BACK END -- lowering a predicate to a PBIR ``Conditional`` (the "Rules" format style)
# =============================================================================================
# Render-verified against Power BI Desktop, because every shape here also passes
# `powerbi-report-author validate` when it is WRONG. What was proven to work:
#
#   Comparison x5 kinds | measure-vs-literal | measure-vs-measure | Arithmetic inside a comparison
#   And | Not | N ordered Cases + DefaultValue
#
# What was proven NOT to work, silently (the whole Conditional falls through to DefaultValue):
#
#   {"Or": {"Left": ..., "Right": ...}}   as an expression node
#
# So disjunction is never emitted as a node. Because each Case already maps ONE predicate to ONE
# colour, ``a OR b -> "X"`` is simply two Cases both yielding ``colour("X")`` -- which is why
# predicates are normalised to DNF here and one Case is emitted per disjunct. Disjunction is free
# in the target representation; it just cannot be spelled the obvious way.

# Tableau comparison -> PBIR ComparisonKind (schema: semanticQuery/1.4.0).
_COMPARISON_KIND = {"=": 0, "==": 0, ">": 1, ">=": 2, "<": 3, "<=": 4}
# Tableau arithmetic -> PBIR Arithmetic Operator.
_ARITH_OP = {"+": 0, "-": 1, "*": 2, "/": 3}


def _find_top(toks, kinds, values):
    """Index of the LAST top-level token whose ``(kind, value)`` matches -- right-associative split.

    Splitting on the last operator keeps left-to-right evaluation order when the expression is
    rebuilt as a nested binary tree.
    """
    depth, hit = 0, -1
    for i, t in enumerate(toks):
        if t[0] == "op" and t[1] in "({":
            depth += 1
        elif t[0] == "op" and t[1] in ")}":
            depth -= 1
        elif depth == 0 and t[0] in kinds and str(t[1]).upper() in values:
            hit = i
    return hit


def _unwrap(toks):
    """Strip one fully-enclosing pair of parentheses, repeatedly."""
    while (len(toks) >= 2 and toks[0] == ("op", "(") and toks[-1] == ("op", ")")):
        depth, encloses = 0, True
        for i, t in enumerate(toks):
            if t == ("op", "("):
                depth += 1
            elif t == ("op", ")"):
                depth -= 1
                if depth == 0 and i != len(toks) - 1:
                    encloses = False
                    break
        if not encloses:
            break
        toks = toks[1:-1]
    return toks


def to_dnf(toks):
    """Split a predicate into DISJUNCTS -- ``[tokens, ...]`` -- flattening top-level ``OR``.

    Only ``OR`` is distributed, because that is the only operator PBIR cannot express as a node.
    ``AND`` and ``NOT`` are left intact for the expression lowering, which handles both.
    """
    toks = _unwrap(list(toks))
    i = _find_top(toks, {"id"}, {"OR"})
    if i < 0:
        return [toks]
    return to_dnf(toks[:i]) + to_dnf(toks[i + 1:])


def lower_condition(toks, resolve):
    """A predicate token list -> a PBIR boolean condition, or ``None`` when not expressible.

    ``resolve(tokens)`` is supplied by the caller (the emitter knows the model binding) and returns
    a PBIR value expression for a non-boolean sub-expression -- e.g. ``SUM([Profit])`` ->
    ``{"Aggregation": {...}}``. Returning ``None`` from it makes the whole predicate unexpressible,
    which is the fail-closed path.
    """
    toks = _unwrap(list(toks))
    if not toks:
        return None
    i = _find_top(toks, {"id"}, {"AND"})
    if i > 0:
        left = lower_condition(toks[:i], resolve)
        right = lower_condition(toks[i + 1:], resolve)
        return {"And": {"Left": left, "Right": right}} if left and right else None
    if _kw(toks[0]) == "NOT":
        inner = lower_condition(toks[1:], resolve)
        return {"Not": {"Expression": inner}} if inner else None
    i = _find_top(toks, {"cmp"}, set(_COMPARISON_KIND))
    if i > 0:
        kind = _COMPARISON_KIND.get(str(toks[i][1]))
        if kind is None:
            return None
        left = lower_value(toks[:i], resolve)
        right = lower_value(toks[i + 1:], resolve)
        if left is None or right is None:
            return None
        return {"Comparison": {"ComparisonKind": kind, "Left": left, "Right": right}}
    return None


def lower_value(toks, resolve):
    """A value token list -> a PBIR value expression, or ``None``.

    Literals are emitted directly; arithmetic is rebuilt as nested ``Arithmetic`` nodes; anything
    else is handed to ``resolve`` (a field, an aggregate call, a measure reference -- all things
    only the emitter can bind).
    """
    toks = _unwrap(list(toks))
    if not toks:
        return None
    if len(toks) == 1:
        t = toks[0]
        if t[0] == "num":
            return {"Literal": {"Value": "%sD" % t[1]}}
        if t[0] == "str":
            return {"Literal": {"Value": "'%s'" % str(t[1]).replace("'", "''")}}
    for ops in (("+", "-"), ("*", "/")):          # lowest precedence first
        i = _find_top(toks, {"op"}, set(ops))
        if i > 0:
            left = lower_value(toks[:i], resolve)
            right = lower_value(toks[i + 1:], resolve)
            if left is None or right is None:
                return None
            return {"Arithmetic": {"Left": left, "Right": right,
                                   "Operator": _ARITH_OP[str(toks[i][1])]}}
    return resolve(toks)


def lower_to_conditional(spec, palette, resolve):
    """A :class:`ColourRuleSpec` + ``{member: hex}`` -> a PBIR ``Conditional`` expression.

    This is rung 1 of the lowering ladder and the reason the whole compiler is worth building: the
    Tableau string members never reach Power BI at all. They collapse into ``Value`` literals, so
    the rebuild adds NOTHING to the model -- no string measure, no colour twin -- and opens in
    Desktop's Conditional formatting dialog as editable rules.

    Returns ``None`` (never a partial rule) when the spec is unsupported, the member domain is not
    closed, a palette entry is missing, or any predicate cannot be expressed. A caller that gets
    ``None`` falls to the next rung.
    """
    if not spec.supported or not spec.closed_domain:
        return None
    cases = []
    for branch in spec.branches:
        colour = palette.get(branch.member)
        if not colour:
            return None
        for disjunct in to_dnf(branch.predicate):
            cond = lower_condition(disjunct, resolve)
            if cond is None:
                return None
            # one Case per disjunct, all yielding the same colour -- this IS the OR
            cases.append({"Condition": cond,
                          "Value": {"Literal": {"Value": "'%s'" % colour}}})
    default = palette.get(spec.default)
    if not cases or not default:
        return None
    return {"Conditional": {"Cases": cases,
                            "DefaultValue": {"Literal": {"Value": "'%s'" % default}}}}


# =============================================================================================
# BACK END -- lowering to a VISUAL CALCULATION (rung 4, the view-scoped rung)
# =============================================================================================
# When any predicate needs a value that does not exist until the visual is evaluated -- "the lowest
# of the displayed bars", "the 90th percentile of what is on screen" -- no model measure can serve
# it. Measured on 0070_new_max: the model-measure form of a WINDOW comparison orders by a row-level
# column while the visual's axis is a grouped one, so it is false on EVERY mark.
#
# The mechanism that does work is Microsoft's own documented one: a Visual Calculation returning a
# hex string, consumed as the `Field value` format style. Render-verified, including PERCENTILEX.INC,
# RANK and MINX over WINDOW(1, ABS, -1, ABS).
#
# TWO SHAPES WERE REFUTED BY RENDER, both passing `validate` with 0 errors:
#   * a NativeVisualCalculation placed INLINE in a formatting property -- silently ignored. It must
#     be a DECLARED, hidden projection referenced by {"SelectRef": {"ExpressionName": <queryRef>}}.
#   * an {"Or": ...} node (see rung 1) -- which is why DNF is applied here too.
#
# A Visual Calculation addresses the visual's own matrix, so its operands are the PROJECTED column
# names ([Sum of Profit]), not model measures. The caller supplies that naming through ``resolve``.

# Tableau view-scoped function -> (DAX aggregator, window spec). The window is the whole visual
# partition for WINDOW_*/TOTAL, and first-row-to-current for RUNNING_*.
_WHOLE = "WINDOW(1, ABS, -1, ABS)"
_RUNNING = "WINDOW(1, ABS, 0, REL)"
_VC_AGGREGATOR = {
    "WINDOW_SUM": ("SUMX", _WHOLE), "WINDOW_AVG": ("AVERAGEX", _WHOLE),
    "WINDOW_MIN": ("MINX", _WHOLE), "WINDOW_MAX": ("MAXX", _WHOLE),
    "WINDOW_MEDIAN": ("MEDIANX", _WHOLE), "WINDOW_COUNT": ("COUNTX", _WHOLE),
    "TOTAL": ("SUMX", _WHOLE),
    "RUNNING_SUM": ("SUMX", _RUNNING), "RUNNING_AVG": ("AVERAGEX", _RUNNING),
    "RUNNING_MIN": ("MINX", _RUNNING), "RUNNING_MAX": ("MAXX", _RUNNING),
    "RUNNING_COUNT": ("COUNTX", _RUNNING),
}

_DAX_COMPARISON = {"=": "=", "==": "=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}


def _call_args(toks):
    """``(FN, [arg_tokens, ...])`` when ``toks`` is exactly one function call, else ``None``."""
    toks = _unwrap(list(toks))
    if len(toks) < 3 or toks[0][0] != "id" or toks[1] != ("op", "("):
        return None
    if toks[-1] != ("op", ")"):
        return None
    depth, args, cur = 0, [], []
    for t in toks[1:]:
        if t == ("op", "("):
            depth += 1
            if depth == 1:
                continue
        elif t == ("op", ")"):
            depth -= 1
            if depth == 0:
                args.append(cur)
                break
        if depth == 1 and t == ("op", ","):
            args.append(cur)
            cur = []
            continue
        cur.append(t)
    # A zero-argument call (``INDEX()`` / ``SIZE()``) collects one EMPTY argument; normalise it away
    # so "no arguments" is spelled the same as an empty list rather than ``[[]]``, which is truthy.
    return (toks[0][1].upper(), [a for a in args if a])


def dax_value(toks, resolve):
    """A value token list -> a visual-calculation DAX fragment, or ``None``.

    ``resolve(tokens)`` names the visual's own projected column for a leaf the caller owns
    (``SUM([Profit])`` -> ``[Sum of Profit]``). Everything view-scoped is rewritten here.
    """
    toks = _unwrap(list(toks))
    if not toks:
        return None
    if len(toks) == 1:
        t = toks[0]
        if t[0] == "num":
            return str(t[1])
        if t[0] == "str":
            return '"%s"' % str(t[1]).replace('"', '""')
    for ops in (("+", "-"), ("*", "/")):
        i = _find_top(toks, {"op"}, set(ops))
        if i > 0:
            left = dax_value(toks[:i], resolve)
            right = dax_value(toks[i + 1:], resolve)
            if left is None or right is None:
                return None
            return "(%s %s %s)" % (left, toks[i][1], right)
    call = _call_args(toks)
    if call:
        fn, args = call
        if fn in _VC_AGGREGATOR and args:
            aggregator, window = _VC_AGGREGATOR[fn]
            inner = dax_value(args[0], resolve)
            return "%s(%s, %s)" % (aggregator, window, inner) if inner else None
        if fn == "WINDOW_PERCENTILE" and len(args) >= 2:
            inner = dax_value(args[0], resolve)
            pct = dax_value(args[1], resolve)
            return ("PERCENTILEX.INC(%s, %s, %s)" % (_WHOLE, inner, pct)
                    if inner and pct else None)
        if fn in ("RANK", "RANK_DENSE") and args:
            inner = dax_value(args[0], resolve)
            return "RANK(DENSE, ORDERBY(%s, DESC))" % inner if inner else None
        if fn == "INDEX" and not args:
            return "ROWNUMBER()"
        if fn == "SIZE" and not args:
            return "COUNTROWS(%s)" % _WHOLE
    return resolve(toks)


def dax_condition(toks, resolve):
    """A predicate token list -> a visual-calculation DAX boolean, or ``None``."""
    toks = _unwrap(list(toks))
    if not toks:
        return None
    i = _find_top(toks, {"id"}, {"AND"})
    if i > 0:
        left = dax_condition(toks[:i], resolve)
        right = dax_condition(toks[i + 1:], resolve)
        return "(%s && %s)" % (left, right) if left and right else None
    i = _find_top(toks, {"id"}, {"OR"})
    if i > 0:
        left = dax_condition(toks[:i], resolve)
        right = dax_condition(toks[i + 1:], resolve)
        return "(%s || %s)" % (left, right) if left and right else None
    if _kw(toks[0]) == "NOT":
        inner = dax_condition(toks[1:], resolve)
        return "NOT(%s)" % inner if inner else None
    i = _find_top(toks, {"cmp"}, set(_DAX_COMPARISON))
    if i > 0:
        op = _DAX_COMPARISON.get(str(toks[i][1]))
        left = dax_value(toks[:i], resolve)
        right = dax_value(toks[i + 1:], resolve)
        if op is None or left is None or right is None:
            return None
        return "%s %s %s" % (left, op, right)
    return None


def lower_to_visual_calc(spec, palette, resolve):
    """A :class:`ColourRuleSpec` -> the DAX of a hex-returning Visual Calculation, or ``None``.

    Emits one nested ``IF`` per branch, in authored order, ending at the default colour -- so the
    calculation reads alongside the Tableau formula it came from. Unlike rung 1 this keeps ``||``
    for disjunction (DAX has it; the PBIR expression tree does not), so no DNF expansion is needed.

    The result is destined for a DECLARED, HIDDEN projection
    (``{"field": {"NativeVisualCalculation": {...}}, "hidden": true}``) referenced from the colour
    property by ``SelectRef`` -- the inline form does not work. Returning the DAX only, rather than
    the projection, keeps this module free of PBIR assembly, which the emitter owns.

    Fail-closed: ``None`` on an unsupported spec, an open member domain, a missing palette entry, or
    any operand that cannot be expressed -- never a calculation with a hole in it.
    """
    if not spec.supported or not spec.closed_domain:
        return None
    default = palette.get(spec.default)
    if not default:
        return None
    parts = []
    for branch in spec.branches:
        colour = palette.get(branch.member)
        if not colour:
            return None
        cond = dax_condition(branch.predicate, resolve)
        if cond is None:
            return None
        parts.append((cond, colour))
    if not parts:
        return None
    dax = '"%s"' % default
    for cond, colour in reversed(parts):
        dax = 'IF(%s, "%s", %s)' % (cond, colour, dax)
    return dax
