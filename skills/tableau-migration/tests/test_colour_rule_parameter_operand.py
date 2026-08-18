"""A conditional-colour rule can compare against a Tableau PARAMETER.

"Colour it red when it is above [Threshold]" is the canonical parameter-driven colour in Tableau,
and until now the rule declined on it: the resolver knew ``AGG([Field])`` and a bare ``[Field]``,
and a ``[Parameters].[X]`` operand returned ``None``, which aborts the whole rule (fail-closed).

The binding that makes it work is specific and is the thing these tests pin: a what-if parameter is
a disconnected table of CANDIDATE rows plus a ``SELECTEDVALUE`` measure that reads the current
selection. Only the measure is a scalar, so only the measure can stand in a comparison -- binding
the picker column instead would compare against the whole domain and is the plausible wrong answer.
"""

import json

import pytest

import assemble_model as A
import twb_to_pbir as T


PARAM_CALC = ("IF SUM([Sales]) > [Parameters].[Clients Goal] THEN 'over' ELSE 'under' END")


def _ws(formula=PARAM_CALC, vtype=None):
    return {
        "name": "Sheet 1",
        "visual_type": vtype or T.VT_MATRIX,
        "rows": [], "cols": [],
        "encodings": {"color": {"caption": "Flag", "kind": "value",
                                "binding": "aggregation", "discrete_measure": True,
                                "formula": formula}},
        "mark_colors": {},
    }


def _values(**over):
    rec = {"table": "Clients Goal", "measure": "Clients Goal Value", "caption": "Clients Goal"}
    rec.update(over)
    return T._colour_param_values({"values": {"[Parameter 1]": rec}})


# -- the model manifest has to carry the scalar in the first place ------------------------------

def test_a_value_parameter_publishes_its_selectedvalue_measure_not_only_its_picker():
    """``_classify_parameters`` exposes ``value`` = the scalar reader, alongside ``picker``.

    The picker is what a SLICER binds to; the measure is what a COMPARISON binds to. Both are
    needed, and conflating them is the defect this key exists to prevent.
    """
    vp = {"consumed_params": [{"caption": "Clients Goal", "internal_name": "[Parameter 1]",
                               "table": "Clients Goal", "measure": "Clients Goal Value",
                               "picker_column": "Clients Goal"}]}
    recs = A._classify_parameters(
        [{"caption": "Clients Goal", "internal_name": "[Parameter 1]"}], {}, vp)

    assert len(recs) == 1
    rec = recs[0]
    assert rec["kind"] == "value"
    assert rec["picker"] == {"table": "Clients Goal", "column": "Clients Goal"}
    assert rec["value"] == {"table": "Clients Goal", "measure": "Clients Goal Value"}


def test_a_parameter_the_model_did_not_consume_publishes_no_scalar():
    """A plain filter parameter has no what-if table, so there is no measure to name."""
    recs = A._classify_parameters(
        [{"caption": "Region Picker", "internal_name": "[Parameter 9]"}], {}, {})

    assert recs[0]["kind"] == "filter"
    assert "value" not in recs[0]


def test_a_consumed_parameter_without_a_measure_publishes_no_scalar():
    """Fail-closed: no measure name recorded -> no ``value`` key, rather than a half-built one."""
    vp = {"consumed_params": [{"caption": "Clients Goal", "internal_name": "[Parameter 1]",
                               "table": "Clients Goal", "picker_column": "Clients Goal"}]}
    recs = A._classify_parameters(
        [{"caption": "Clients Goal", "internal_name": "[Parameter 1]"}], {}, vp)

    assert recs[0]["kind"] == "value"
    assert "value" not in recs[0]


# -- the seam: keys must join whichever way the formula spells the parameter --------------------

def test_the_scalar_is_keyed_by_both_internal_name_and_caption():
    """A Tableau formula may write either spelling, so both must resolve to the same record."""
    values = _values()

    assert set(values) == {"parameter 1", "clients goal"}
    assert values["parameter 1"] == values["clients goal"]


def test_bracket_and_case_differences_at_the_seam_do_not_cause_a_near_miss():
    """The model keys with brackets; a formula token arrives without them."""
    values = T._colour_param_values(
        {"values": {"[Parameter 1]": {"table": "T", "measure": "M", "caption": "  CLIENTS Goal "}}})

    assert values["clients goal"] == {"table": "T", "measure": "M"}
    assert values["parameter 1"] == {"table": "T", "measure": "M"}


@pytest.mark.parametrize("bad", [
    {"table": "T"},                      # no measure
    {"measure": "M"},                    # no table
    {"table": "", "measure": "M"},       # empty table
    "not-a-dict",
])
def test_a_half_built_value_record_is_ignored(bad):
    """Fail-closed at the seam too -- a partial record must not produce a partial reference."""
    assert T._colour_param_values({"values": {"[P]": bad}}) == {}


# -- the lowering ------------------------------------------------------------------------------

def _right_operand(rule):
    return rule["Conditional"]["Cases"][0]["Condition"]["Comparison"]["Right"]


def test_a_parameter_operand_lowers_to_the_selectedvalue_measure():
    rule = T._discrete_colour_rule(_ws(), _ws()["encodings"]["color"], "Orders", {}, _values())

    assert _right_operand(rule) == {
        "Measure": {"Expression": {"SourceRef": {"Entity": "Clients Goal"}},
                    "Property": "Clients Goal Value"}}


def test_the_parameter_operand_is_a_measure_not_the_picker_column():
    """The plausible wrong answer, pinned.

    Binding the picker COLUMN validates clean and renders: it just compares each mark against the
    whole candidate domain instead of against the selection, which is silently wrong output.
    """
    rule = T._discrete_colour_rule(_ws(), _ws()["encodings"]["color"], "Orders", {}, _values())
    right = _right_operand(rule)

    assert "Measure" in right
    assert "Column" not in right
    assert "Clients Goal Value" in json.dumps(right)


def test_the_rest_of_the_rule_is_unchanged_by_the_parameter_operand():
    """Only the operand is new -- the comparison, the cases and the palette are as before."""
    rule = T._discrete_colour_rule(_ws(), _ws()["encodings"]["color"], "Orders", {}, _values())
    cond = rule["Conditional"]

    assert cond["Cases"][0]["Condition"]["Comparison"]["ComparisonKind"] == 1     # >
    assert cond["Cases"][0]["Condition"]["Comparison"]["Left"]["Aggregation"]["Function"] == 0
    assert len(cond["Cases"]) == 1
    assert cond["DefaultValue"]["Literal"]["Value"].startswith("'#")


def test_an_unmodeled_parameter_declines_the_whole_rule():
    """No object in the model holds that value, so there is nothing honest to emit."""
    assert T._discrete_colour_rule(_ws(), _ws()["encodings"]["color"], "Orders", {}, {}) is None


def test_a_parameter_operand_declines_when_only_a_DIFFERENT_parameter_is_bound():
    other = T._colour_param_values(
        {"values": {"[Parameter 7]": {"table": "T", "measure": "M", "caption": "Something Else"}}})

    assert T._discrete_colour_rule(_ws(), _ws()["encodings"]["color"], "Orders", {}, other) is None


def test_a_qualified_reference_that_is_not_a_parameter_declines():
    """``[Datasource].[Field]`` is a different shape; binding it as a parameter would be a guess."""
    ws = _ws("IF SUM([Sales]) > [Orders].[Target] THEN 'over' ELSE 'under' END")

    assert T._discrete_colour_rule(ws, ws["encodings"]["color"], "Orders", {}, _values()) is None


def test_the_parameter_operand_works_on_the_chart_path_too():
    """Same resolver, both channels -- a bar chart's fill must bind identically to a matrix cell."""
    ws = _ws(vtype=T.VT_BAR)
    state = {"Values": {"projections": [{"queryRef": "q"}]}}
    objs, fact = T._chart_discrete_measure_fill(
        ws, state, T.VT_BAR, "Orders", {}, [], _values())

    assert fact and fact["style"] == "rules"
    expr = objs[0]["properties"]["fill"]["solid"]["color"]["expr"]
    assert _right_operand(expr)["Measure"]["Property"] == "Clients Goal Value"


def test_arithmetic_against_a_parameter_lowers(monkeypatch):
    """A threshold is often scaled -- ``> [Goal] * 1.1`` -- so the operand must nest in Arithmetic."""
    ws = _ws("IF SUM([Sales]) > [Parameters].[Clients Goal] * 1.1 THEN 'over' ELSE 'under' END")
    rule = T._discrete_colour_rule(ws, ws["encodings"]["color"], "Orders", {}, _values())

    arith = _right_operand(rule)["Arithmetic"]
    assert arith["Operator"] == 2                                          # *
    assert arith["Left"]["Measure"]["Property"] == "Clients Goal Value"
    assert arith["Right"]["Literal"]["Value"] == "1.1D"


# -- the IR seam -------------------------------------------------------------------------------

def test_emit_pbir_tolerates_an_ir_built_before_parameter_values_existed():
    """``ir["parameter_values"]`` is additive; its absence must degrade to "no parameter binds"."""
    resolver = T._colour_rule_resolver(_ws(), "Orders", {}, ({} or {}).get("parameter_values"))

    assert resolver([("qfield", ["Parameters", "Clients Goal"])]) is None
