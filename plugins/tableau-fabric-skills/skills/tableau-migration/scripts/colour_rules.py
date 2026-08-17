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
