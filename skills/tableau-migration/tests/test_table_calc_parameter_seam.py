"""Tests for the table-calc seam reading a Tableau PARAMETER.

Two defects, one feature. A table calc written over a parameter
(``WINDOW_MAX([Parameters].[Top N?])``, or over a calc that filters by a date parameter) never
translated, and the user-visible reason blamed the parameter for being "unmodeled" when it was
modelled a few files away in the same emitted model.

1. **The seam was never given the resolver.** ``parameters.py`` emits a value/what-if parameter as
   a calculated table plus a ``[<Param> Value]`` ``SELECTEDVALUE`` picker measure, and returns a
   ``param_resolver`` that every other translation path in ``assemble_model`` already receives.
   ``table_calc_to_dax`` imported neither, and its own comment said "the seam parses no
   ``[Parameters].`` form". Measured on the 34-workbook corpus this was **23 of 54** table-calc
   handoffs -- the single largest cause.

2. **The inliners MANGLED the reference.** Both ``_inline_scope_calcs`` and
   ``_inline_calc_formula`` substitute at bracket level (``re.sub(r"\\[([^\\[\\]]+)\\]", ...)``),
   which cannot tell the NAME half of a qualified ``[Parameters].[X]`` from an ordinary ``[calc]``
   reference. A Tableau parameter column's *formula* is its CURRENT VALUE, so the name half was
   replaced by that value and ``[Parameters].[Start Date]`` became ``[Parameters].(#2020-01-01#)``
   -- a reference naming no parameter, which could never resolve however good the resolver was.
   Corpus control: **34 occurrences of that malformed shape before, 0 after, and ZERO exist in any
   source workbook**, so the engine produced every one of them.

Both inliners needed the same guard. Fixing only the outer one moved the mangling one level down
and looked fixed -- the count did not move, which is what caught it.

``param_resolver=None`` stays the default everywhere, so every pre-existing caller is
byte-identical; the tests below pin that too, since a fail-open default would be a silent
behaviour change across the whole estate.
"""
import pytest

from workbook_table_calcs import TableCalcUsage
from table_calc_to_dax import (
    _inline_calc_formula,
    _inline_scope_calcs,
    _unresolved_params,
    translate_table_calc_usage,
    translate_table_calc_usages,
)


def _resolver(caption):
    col = caption.replace(" ", "_")
    return ("Orders", col, "number")


def _params(**answers):
    """A ``param_resolver`` answering only for the named parameters."""
    low = {k.strip().lower(): v for k, v in answers.items()}
    return lambda n: low.get((n or "").strip().lower())


# -- _unresolved_params: the gate the two guards consult ------------------------
def test_with_no_resolver_every_parameter_is_unresolved():
    """The historical default. Every caller that does not pass a resolver must behave exactly as
    before, so the fix cannot change output for anyone who has not opted in."""
    assert _unresolved_params("WINDOW_MAX([Parameters].[Top N?])", None) == ["Top N?"]
    assert _unresolved_params("[Parameters].[A] + [Parameters].[B]", None) == ["A", "B"]


def test_a_formula_with_no_parameter_is_never_blocked():
    assert _unresolved_params("RUNNING_SUM(SUM([Sales]))", None) == []
    assert _unresolved_params("RUNNING_SUM(SUM([Sales]))", _params()) == []


def test_a_resolved_parameter_clears_the_gate():
    r = _params(**{"Top N?": "[Top N? Value]"})
    assert _unresolved_params("WINDOW_MAX([Parameters].[Top N?])", r) == []


def test_a_partially_resolvable_formula_still_declines():
    """Substituting only some parameters would leave a bare ``[Parameters].`` for the lexer to trip
    on and blame a stray ``.`` -- the exact misdiagnosis the handoff text exists to avoid."""
    r = _params(A="[A Value]")
    assert _unresolved_params("[Parameters].[A] + [Parameters].[B]", r) == ["B"]


def test_a_parameter_reference_with_no_parsable_name_declines_even_with_a_resolver():
    """A ``[Parameters].`` that is not ``[Parameters].[Name]`` has nothing to look up. It must NOT
    read as "no parameters present, therefore fully resolved" -- that is the empty-set-looks-clean
    failure, and here it would hand malformed text to the seam."""
    assert _unresolved_params("MIN([Parameters].)", _params(A="[A Value]")) == ["<unnamed>"]


def test_the_resolver_raising_is_treated_as_unresolved_not_as_a_crash():
    def _boom(_n):
        raise RuntimeError("resolver blew up")
    assert _unresolved_params("WINDOW_MAX([Parameters].[X])", _boom) == ["X"]


# -- the mangling: a qualified reference must survive inlining ------------------
_PARAM_VALUE_LOOKUP = {
    # A Tableau parameter column's "formula" IS its current value. This lookup shape is what made
    # the mangling possible, so the fixture reproduces it exactly.
    "start date": "#2020-01-01#",
    "top n?": "10",
    "in range": "[Created Date] >= [Parameters].[Start Date]",
}


def test_inline_scope_calcs_leaves_a_qualified_parameter_reference_intact():
    out, reason = _inline_scope_calcs(
        "RUNNING_SUM(SUM([Sales])) > [Parameters].[Start Date]",
        _PARAM_VALUE_LOOKUP, param_resolver=_params(**{"Start Date": "[Start Date Value]"}))
    assert reason is None, reason
    assert "[Parameters].[Start Date]" in out, out
    assert "(#2020-01-01#)" not in out, "the parameter's VALUE was spliced over its name: %s" % out


def test_inline_calc_formula_leaves_a_qualified_parameter_reference_intact():
    """The RECURSIVE inliner needs the identical guard. Fixing only the outer one moved the
    mangling one level down: the malformed shape still appeared, via the referenced calc's body."""
    out = _inline_calc_formula("in range", _PARAM_VALUE_LOOKUP, set())
    assert out is not None
    assert "[Parameters].[Start Date]" in out, out
    assert "(#2020-01-01#)" not in out, out


def test_an_ordinary_calc_reference_is_still_inlined():
    """Positive control. The guard must be scoped to the parameter qualifier -- if it suppressed
    ordinary inlining, every one of these tests could pass while the feature was dead."""
    out = _inline_calc_formula("in range", {"in range": "SUM([Sales]) + [base]",
                                            "base": "SUM([Profit])"}, set())
    assert "(SUM([Profit]))" in out, out


def test_a_bare_bracket_named_like_a_parameter_is_still_inlined():
    """``[Start Date]`` NOT preceded by the ``[Parameters].`` qualifier is an ordinary field/calc
    reference and must keep inlining -- the guard keys on the qualifier, not on the name."""
    out = _inline_calc_formula("outer", {"outer": "SUM([Start Date])",
                                         "start date": "#2020-01-01#"}, set())
    assert "(#2020-01-01#)" in out, out


# -- end to end through the public entry point ---------------------------------
def _usage(formula, **kw):
    kw.setdefault("worksheet", "Sheet 1")
    kw.setdefault("instance", "usr:Calculation_1:qk")
    kw.setdefault("column", "Calculation_1")
    kw.setdefault("caption", "Over Param")
    kw.setdefault("kind", "field")
    kw.setdefault("ordering_type", "Table")
    return TableCalcUsage(formula=formula, **kw)


def test_without_a_resolver_a_parameter_table_calc_still_hands_off_with_the_truthful_reason():
    t = translate_table_calc_usage(
        _usage("RUNNING_SUM(SUM([Parameters].[Top N?]))"), _resolver)
    assert t.status != "translated"
    assert "unmodeled parameter reference" in (t.reason or "")


def test_the_batch_wrapper_threads_the_resolver():
    """A keyword accepted by the batch wrapper but dropped before the per-usage call would leave
    every caller silently on the old path."""
    calls = []

    def _spy(name):
        calls.append(name)
        return "[Top N? Value]"

    translate_table_calc_usages([_usage("RUNNING_SUM(SUM([Parameters].[Top N?]))")],
                                _resolver, param_resolver=_spy)
    assert calls, "param_resolver was never consulted through translate_table_calc_usages"


def test_the_resolver_is_consulted_for_the_name_the_measure_path_uses():
    """``calc_to_dax._Parser._resolve_param(parts[1])`` looks up the SECOND bracketed part, so the
    seam must ask for the same token or the two paths disagree about which parameter this is."""
    asked = []
    translate_table_calc_usage(
        _usage("RUNNING_SUM(SUM([Parameters].[Top N?]))"), _resolver,
        param_resolver=lambda n: asked.append(n) or "[Top N? Value]")
    assert asked == ["Top N?"], asked
