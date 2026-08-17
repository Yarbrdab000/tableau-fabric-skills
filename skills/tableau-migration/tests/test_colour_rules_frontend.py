"""The conditional-colour compiler front end: branches, member domain, and predicate scope.

These are the three questions that decide whether a Tableau colour calculation can be rebuilt as
Power BI's own conditional formatting, and by which mechanism. They are asked of the formula's
PROPERTIES, not matched against templates, so a calculation the engine has never seen still routes.

The corpus supplies the shapes asserted here: every "real workbook" formula below is copied verbatim
from a workbook in the 29-workbook regression corpus or from `Logic example 4`.
"""
import json

import pytest

import colour_rules as CR


def _spec(formula):
    return CR.analyse_colour_calc(formula)


# -- the scope lattice ------------------------------------------------------------------------
class TestScopeLattice:
    def test_the_lub_picks_the_highest_scope(self):
        assert CR.scope_max(CR.SCOPE_ROW, CR.SCOPE_VIEW) == CR.SCOPE_VIEW
        assert CR.scope_max(CR.SCOPE_CONSTANT, CR.SCOPE_AGGREGATE) == CR.SCOPE_AGGREGATE
        assert CR.scope_max() == CR.SCOPE_CONSTANT

    def test_an_unknown_scope_does_not_raise_or_win(self):
        assert CR.scope_max("nonsense", CR.SCOPE_ROW) == CR.SCOPE_ROW


# -- question 1: what are the branches? --------------------------------------------------------
class TestBranchFlattening:
    def test_the_shipped_two_member_case(self):
        # Logic example 4, verbatim
        s = _spec('If SUM([Profit]) < 0 then "negative" else "positive" END')
        assert s.supported
        assert [b.member for b in s.branches] == ["negative"]
        assert s.default == "positive"
        assert s.members == ["negative", "positive"]

    def test_an_elseif_chain_flattens_to_ordered_branches(self):
        s = _spec('IF SUM([Sales]) > 100 THEN "hi" ELSEIF SUM([Sales]) > 50 THEN "mid" '
                  'ELSE "lo" END')
        assert s.supported
        assert [b.member for b in s.branches] == ["hi", "mid"]
        assert s.default == "lo"
        assert s.members == ["hi", "mid", "lo"]

    def test_a_NESTED_if_in_the_else_arm_flattens_into_the_same_chain(self):
        # depth is free: Conditional.Cases is an ordered list, so nesting collapses onto it
        s = _spec('IF [a] > 1 THEN "A" ELSE IF [b] > 2 THEN "B" ELSE '
                  'IF [c] > 3 THEN "C" ELSE "D" END END END')
        assert s.supported
        assert [b.member for b in s.branches] == ["A", "B", "C"]
        assert s.default == "D"
        assert s.members == ["A", "B", "C", "D"]

    def test_deep_nesting_keeps_predicate_order(self):
        s = _spec('IF [a] > 1 THEN "A" ELSE IF [b] > 2 THEN "B" ELSE "C" END END')
        assert [CR._render(b.predicate) for b in s.branches] == ["[a] > 1", "[b] > 2"]

    def test_a_case_chain_normalises_to_comparisons(self):
        s = _spec('CASE [Region] WHEN "East" THEN "a" WHEN "West" THEN "b" ELSE "c" END')
        assert s.supported
        assert [b.member for b in s.branches] == ["a", "b"]
        assert s.default == "c"
        # every branch carries ONE predicate shape, whichever surface form it came from
        assert [CR._render(b.predicate) for b in s.branches] == [
            '[Region] = "East"', '[Region] = "West"']

    def test_a_missing_else_is_a_real_branchless_default(self):
        # Tableau yields NULL, which paints nothing -- distinct from "there is a default member"
        s = _spec('IF SUM([Profit]) < 0 THEN "negative" END')
        assert s.supported
        assert [b.member for b in s.branches] == ["negative"]
        assert s.default is None

    def test_a_nested_if_inside_a_THEN_arm_does_not_capture_the_outer_scan(self):
        # structural depth counts IF/CASE...END, not just parentheses
        s = _spec('IF [a] > 1 THEN IF [b] > 2 THEN "x" ELSE "y" END ELSE "z" END')
        assert s.supported
        assert s.default == "z"
        assert [CR._render(b.predicate) for b in s.branches] == ["[a] > 1"]


class TestItFailsClosed:
    @pytest.mark.parametrize("formula, fragment", [
        ("", "empty"),
        ("SUM([Sales])", "not a conditional"),
        ('IF SUM([Sales]) > 1 "a" ELSE "b" END', "without THEN"),
        ("IF THEN \"a\" ELSE \"b\" END", "empty condition"),
        ("CASE [Region] THEN \"a\" END", "without WHEN"),
    ])
    def test_an_unreadable_formula_is_refused_with_a_reason(self, formula, fragment):
        s = _spec(formula)
        assert not s.supported
        assert fragment in s.reason
        # a refused spec must never look like a usable one
        assert s.branches == [] and s.members == []


# -- question 2: is the member domain closed? --------------------------------------------------
class TestMemberDomain:
    def test_all_string_literals_is_a_closed_domain(self):
        assert _spec('IF [a] > 1 THEN "A" ELSE "B" END').closed_domain is True

    def test_a_data_valued_outcome_is_NOT_closed(self):
        # the one shape a static palette provably cannot serve: members are data, not literals
        s = _spec('IF [a] > 1 THEN [Category] ELSE "Other" END')
        assert s.supported, "it parses fine -- it just cannot be given a static palette"
        assert s.closed_domain is False
        assert s.members == ["Other"], "only the literal members are known statically"

    def test_a_missing_default_is_not_a_closed_domain(self):
        # no ELSE means an unpainted NULL member, so the palette is not fully determined
        assert _spec('IF [a] > 1 THEN "A" END').closed_domain is False

    def test_members_keep_first_appearance_order_and_dedupe(self):
        s = _spec('IF [a] > 1 THEN "hi" ELSEIF [b] > 2 THEN "lo" ELSEIF [c] > 3 THEN "hi" '
                  'ELSE "lo" END')
        assert s.members == ["hi", "lo"]


# -- question 3: what scope does each predicate need? ------------------------------------------
class TestPredicateScope:
    def test_a_literal_comparison_is_aggregate_scope(self):
        # SUM(...) < 0 -- expressible directly in the visual's own semantic query
        assert _spec('IF SUM([Profit]) < 0 THEN "n" ELSE "p" END').scope == CR.SCOPE_AGGREGATE

    def test_a_parameter_threshold_is_parameter_scope_or_higher(self):
        # corpus: IF [Calc] >= [Parameters].[Parameter 11] THEN ... ELSE ... END
        s = _spec('IF [Calc] >= [Parameters].[Parameter 11] THEN "Above" ELSE "Below" END')
        assert s.scope in (CR.SCOPE_PARAMETER, CR.SCOPE_ROW)

    def test_an_LOD_predicate_is_lod_scope(self):
        s = _spec('IF SUM([Sales]) > {FIXED [Region] : AVG([Sales])} THEN "a" ELSE "b" END')
        assert s.scope == CR.SCOPE_LOD

    @pytest.mark.parametrize("fn", ["WINDOW_MAX", "WINDOW_MEDIAN", "RUNNING_MAX", "TOTAL",
                                    "RANK_PERCENTILE", "INDEX", "FIRST", "LAST", "SIZE"])
    def test_every_view_scoped_function_lifts_the_predicate_to_view_scope(self, fn):
        s = _spec('IF SUM([Sales]) = %s(SUM([Sales])) THEN "max" ELSE "other" END' % fn)
        assert s.scope == CR.SCOPE_VIEW, fn

    def test_the_corpus_highlight_max_calc_is_view_scoped(self):
        # 0070_new_max, verbatim -- the calc that rendered every bar the same wrong colour
        s = _spec('SUM([Sales]) = WINDOW_MAX(SUM([Sales]), FIRST(),0)')
        assert not s.supported, "a bare comparison is not a branch chain"
        # ...but its scope is still readable, which is what the deferral gate needs
        assert CR.scope_of(CR._C._tokenize(
            "SUM([Sales]) = WINDOW_MAX(SUM([Sales]), FIRST(),0)")) == CR.SCOPE_VIEW

    def test_the_lub_is_taken_across_ALL_branches_not_just_the_first(self):
        # a cheap first predicate must not hide an expensive later one
        s = _spec('IF SUM([Sales]) > 1 THEN "a" '
                  'ELSEIF SUM([Sales]) = WINDOW_MAX(SUM([Sales])) THEN "b" ELSE "c" END')
        assert s.branches[0].scope == CR.SCOPE_AGGREGATE
        assert s.branches[1].scope == CR.SCOPE_VIEW
        assert s.scope == CR.SCOPE_VIEW

    def test_percentile_colouring_routes_to_view_scope(self):
        # "colour marks by what percentile they fall in" -- the user's own example
        s = _spec('IF SUM([Sales]) >= WINDOW_PERCENTILE(SUM([Sales]), 0.9) THEN "top" '
                  'ELSE "rest" END')
        assert s.supported and s.closed_domain
        assert s.scope == CR.SCOPE_VIEW


# =============================================================================================
# BACK END -- lowering to a PBIR `Conditional` (rung 1)
# =============================================================================================
def _resolve(toks):
    """A stand-in for the emitter's model binding: SUM([X]) -> an Aggregation over Orders[X]."""
    if len(toks) == 1 and toks[0][0] == "field":
        return {"Column": {"Expression": {"SourceRef": {"Entity": "Orders"}},
                           "Property": toks[0][1]}}
    if (len(toks) == 4 and toks[0][0] == "id" and toks[1] == ("op", "(")
            and toks[2][0] == "field" and toks[3] == ("op", ")")):
        return {"Aggregation": {"Expression": {"Column": {
            "Expression": {"SourceRef": {"Entity": "Orders"}},
            "Property": toks[2][1]}}, "Function": 0}}
    return None


_PALETTE2 = {"negative": "#D62728", "positive": "#2CA02C"}


class TestDNF:
    def test_a_predicate_without_or_is_one_disjunct(self):
        assert len(CR.to_dnf(CR._C._tokenize("SUM([Profit]) < 0"))) == 1

    def test_or_splits_into_disjuncts(self):
        assert len(CR.to_dnf(CR._C._tokenize("[a] > 1 OR [b] > 2 OR [c] > 3"))) == 3

    def test_and_is_left_intact(self):
        # AND is a real PBIR node; only OR has to be distributed
        assert len(CR.to_dnf(CR._C._tokenize("[a] > 1 AND [b] > 2"))) == 1


class TestLoweringToConditional:
    def test_the_shipped_case_lowers_with_no_model_objects(self):
        spec = _spec('IF SUM([Profit]) < 0 THEN "negative" ELSE "positive" END')
        expr = CR.lower_to_conditional(spec, _PALETTE2, _resolve)
        assert expr == {"Conditional": {
            "Cases": [{"Condition": {"Comparison": {
                "ComparisonKind": 3,
                "Left": {"Aggregation": {"Expression": {"Column": {
                    "Expression": {"SourceRef": {"Entity": "Orders"}},
                    "Property": "Profit"}}, "Function": 0}},
                "Right": {"Literal": {"Value": "0D"}}}},
                "Value": {"Literal": {"Value": "'#D62728'"}}}],
            "DefaultValue": {"Literal": {"Value": "'#2CA02C'"}}}}

    def test_an_n_way_chain_becomes_n_ordered_cases(self):
        spec = _spec('IF SUM([Sales]) > 100 THEN "a" ELSEIF SUM([Sales]) > 50 THEN "b" '
                     'ELSE "c" END')
        expr = CR.lower_to_conditional(
            spec, {"a": "#111111", "b": "#222222", "c": "#333333"}, _resolve)
        cases = expr["Conditional"]["Cases"]
        assert [c["Value"]["Literal"]["Value"] for c in cases] == ["'#111111'", "'#222222'"]
        assert expr["Conditional"]["DefaultValue"]["Literal"]["Value"] == "'#333333'"

    def test_OR_becomes_two_cases_sharing_one_colour(self):
        # PBIR silently ignores an {"Or": ...} node, so disjunction is spelled as repeated Cases
        spec = _spec('IF SUM([Profit]) < 0 OR SUM([Sales]) < 10 THEN "negative" '
                     'ELSE "positive" END')
        expr = CR.lower_to_conditional(spec, _PALETTE2, _resolve)
        cases = expr["Conditional"]["Cases"]
        assert len(cases) == 2
        assert {c["Value"]["Literal"]["Value"] for c in cases} == {"'#D62728'"}
        # test the JSON KEY, not the substring: a bare "Or" matches inside "Orders"
        assert '"Or":' not in json.dumps(expr), "an Or node is silently ignored by Power BI"

    def test_AND_is_emitted_as_a_node(self):
        spec = _spec('IF SUM([Profit]) < 0 AND SUM([Sales]) > 10 THEN "negative" '
                     'ELSE "positive" END')
        cond = CR.lower_to_conditional(spec, _PALETTE2, _resolve)["Conditional"]["Cases"][0]
        assert "And" in cond["Condition"]

    def test_NOT_is_emitted_as_a_node(self):
        spec = _spec('IF NOT SUM([Profit]) < 0 THEN "positive" ELSE "negative" END')
        cond = CR.lower_to_conditional(spec, _PALETTE2, _resolve)["Conditional"]["Cases"][0]
        assert "Not" in cond["Condition"]

    def test_arithmetic_inside_a_comparison(self):
        spec = _spec('IF SUM([Discount]) * 200 > SUM([Profit]) THEN "negative" '
                     'ELSE "positive" END')
        cond = CR.lower_to_conditional(spec, _PALETTE2, _resolve)["Conditional"]["Cases"][0]
        arith = cond["Condition"]["Comparison"]["Left"]["Arithmetic"]
        assert arith["Operator"] == 2                      # multiply
        assert arith["Right"] == {"Literal": {"Value": "200D"}}

    def test_measure_versus_measure(self):
        spec = _spec('IF SUM([Profit]) > SUM([Sales]) THEN "negative" ELSE "positive" END')
        cmp_ = CR.lower_to_conditional(spec, _PALETTE2, _resolve)["Conditional"]["Cases"][0]
        both = cmp_["Condition"]["Comparison"]
        assert "Aggregation" in both["Left"] and "Aggregation" in both["Right"]

    @pytest.mark.parametrize("op, kind", [("<", 3), ("<=", 4), (">", 1), (">=", 2), ("=", 0)])
    def test_every_comparison_kind_maps(self, op, kind):
        spec = _spec('IF SUM([Profit]) %s 0 THEN "negative" ELSE "positive" END' % op)
        cond = CR.lower_to_conditional(spec, _PALETTE2, _resolve)["Conditional"]["Cases"][0]
        assert cond["Condition"]["Comparison"]["ComparisonKind"] == kind

    def test_a_case_chain_lowers_through_the_same_path(self):
        spec = _spec('CASE [Region] WHEN "East" THEN "negative" ELSE "positive" END')
        cond = CR.lower_to_conditional(spec, _PALETTE2, _resolve)["Conditional"]["Cases"][0]
        assert cond["Condition"]["Comparison"]["ComparisonKind"] == 0
        assert cond["Condition"]["Comparison"]["Right"] == {"Literal": {"Value": "'East'"}}


class TestLoweringFailsClosed:
    def test_an_unsupported_spec_lowers_to_nothing(self):
        assert CR.lower_to_conditional(_spec("SUM([Sales])"), _PALETTE2, _resolve) is None

    def test_an_open_member_domain_lowers_to_nothing(self):
        # members are DATA -- no static palette exists, so this must fall to another rung
        spec = _spec('IF SUM([Profit]) < 0 THEN [Category] ELSE "positive" END')
        assert CR.lower_to_conditional(spec, _PALETTE2, _resolve) is None

    def test_a_missing_palette_entry_lowers_to_nothing(self):
        spec = _spec('IF SUM([Profit]) < 0 THEN "negative" ELSE "positive" END')
        assert CR.lower_to_conditional(spec, {"negative": "#D62728"}, _resolve) is None

    def test_an_unresolvable_operand_lowers_to_nothing(self):
        # the resolver stands in for the model binding; None from it must abort the WHOLE rule
        # rather than emit a rule with a hole in it
        spec = _spec('IF WINDOW_MAX(SUM([Profit])) < 0 THEN "negative" ELSE "positive" END')
        assert CR.lower_to_conditional(spec, _PALETTE2, _resolve) is None

    def test_a_partial_rule_is_never_returned(self):
        # first branch fine, second unresolvable -> nothing at all
        spec = _spec('IF SUM([Profit]) < 0 THEN "negative" '
                     'ELSEIF WINDOW_MAX(SUM([Sales])) > 1 THEN "negative" ELSE "positive" END')
        assert CR.lower_to_conditional(spec, _PALETTE2, _resolve) is None