"""Tableau's Colour shelf paints THE MARK -- so a table's colour channel follows its MARK TYPE.

On a **text table** the mark IS the number, so a colour encoding is the FONT colour and the cell
background is left alone. On a **Square** highlight table the mark is a filled rectangle, so the
identical encoding is the CELL BACKGROUND. The two are not interchangeable, and getting it wrong
reproduces neither half: painting a text table's background fills every cell with a colour the
source never drew, while the numbers the author actually colour-coded stay black.

The driver here is a DISCRETE aggregate measure -- Tableau's ordinary "label these marks" calc,
``IF SUM([Profit]) < 0 THEN "negative" ELSE "positive" END``. Power BI has no native categorical
legend for a measure-driven colour (a legend needs a grouping COLUMN, which is row-level and would
change the aggregate grain and the row count), so the rebuild binds the model's hex-returning
colour twin through conditional formatting as the ``Field value`` format style.

The wildcard selector is load-bearing and invisible to every automated check: without it Power BI
still HONOURS the expression but evaluates it in ONE context and paints every cell the same colour,
with a clean validation pass. It is asserted directly for that reason.
"""
from twb_to_pbir import emit_pbir, parse_twb

from test_twb_to_pbir import _INST, _query_state, _visual_parts, _workbook, _worksheet

_MV_ALL_FILTER = ("<filter class='categorical' column='[federated.abc].[:Measure Names]'>"
                  "<groupfilter function='level-members' level='[:Measure Names]' />"
                  "</filter>")

# a STRING-valued measure calc -- the pill Tableau drops on Colour to label its marks
_LABEL_CALC = (
    "<column caption='Sign' datatype='string' name='[Calculation_sign]' role='measure' "
    "type='nominal'>"
    "<calculation class='tableau' formula='If SUM([Profit]) &lt; 0 then &quot;negative&quot; "
    "else &quot;positive&quot; END' /></column>"
    "<column-instance column='[Calculation_sign]' derivation='User' "
    "name='[usr:Calculation_sign:nk]' pivot='key' type='nominal' />")

_ENC = ("<encodings><color column='[federated.abc].[usr:Calculation_sign:nk]' />"
        "<text column='[federated.abc].[Multiple Values]' /></encodings>")


def _table_ir(mark):
    ws = _worksheet("Coloured Table", mark,
                    rows="[federated.abc].[none:Region:nk]",
                    cols="[federated.abc].[:Measure Names]",
                    deps_extra=_INST + _LABEL_CALC, encodings=_ENC, filters=_MV_ALL_FILTER)
    return parse_twb(_workbook(ws))


def _visual(ir):
    return list(_visual_parts(emit_pbir(ir)).values())[0]


class TestChannelFollowsTheMark:
    def test_a_text_mark_colours_the_text_and_leaves_the_background_alone(self):
        values = _visual(_table_ir("Text"))["visual"]["objects"]["values"]
        assert values
        for entry in values:
            assert "fontColor" in entry["properties"]
            assert "backColor" not in entry["properties"]

    def test_an_automatic_mark_draws_text_on_a_crosstab_so_it_colours_the_text(self):
        values = _visual(_table_ir("Automatic"))["visual"]["objects"]["values"]
        assert values
        assert all("fontColor" in e["properties"] for e in values)
        assert all("backColor" not in e["properties"] for e in values)

    def test_a_square_mark_is_a_filled_cell_so_it_colours_the_background(self):
        values = _visual(_table_ir("Square"))["visual"]["objects"]["values"]
        assert values
        for entry in values:
            assert "backColor" in entry["properties"]
            assert "fontColor" not in entry["properties"]

    def test_the_helper_fails_closed_toward_the_filled_cell(self):
        # An unrecognised mark keeps the long-standing background behaviour rather than silently
        # switching an existing highlight table to a font colour.
        import twb_to_pbir as R
        assert R._cell_colour_property({"mark_class": "Text"}) == "fontColor"
        assert R._cell_colour_property({"mark_class": None}) == "fontColor"
        assert R._cell_colour_property({"mark_class": "Square"}) == "backColor"
        assert R._cell_colour_property({"mark_class": "Circle"}) == "backColor"


class TestTheBinding:
    def test_a_string_member_calc_binds_a_native_RULES_conditional(self):
        # THE POINT OF THE COMPILER: the Tableau members collapse into Value literals, so the
        # comparison is against the REAL measure and nothing is added to the model. No twin.
        entry = _visual(_table_ir("Automatic"))["visual"]["objects"]["values"][0]
        expr = entry["properties"]["fontColor"]["solid"]["color"]["expr"]
        assert "Conditional" in expr, "a lowerable calc must not fall back to a colour twin"
        case = expr["Conditional"]["Cases"][0]
        cmp_ = case["Condition"]["Comparison"]
        assert cmp_["ComparisonKind"] == 3                     # LessThan
        assert cmp_["Left"]["Aggregation"]["Expression"]["Column"]["Property"] == "Profit"
        assert cmp_["Right"] == {"Literal": {"Value": "0D"}}
        assert case["Value"]["Literal"]["Value"].startswith("'#")

    def test_the_rule_references_no_model_measure_at_all(self):
        import json
        blob = json.dumps(_visual(_table_ir("Automatic"))["visual"]["objects"]["values"])
        assert "(colour)" not in blob, "the whole point is that no colour twin is needed"
        assert '"Measure"' not in blob

    def test_every_value_column_is_coloured_not_just_the_first(self):
        # Tableau colours every measure cell in the row from the one mark colour, so each projected
        # value needs its own scoped entry; a single unscoped one colours only the first column.
        visual = _visual(_table_ir("Automatic"))
        refs = [p["queryRef"] for p in _query_state(visual)["Values"]["projections"]]
        assert len(refs) > 1
        assert [v["selector"]["metadata"] for v in visual["visual"]["objects"]["values"]] == refs

    def test_the_dataviewwildcard_selector_is_present_on_every_entry(self):
        # Load-bearing and validation-invisible: without it every cell paints identically.
        for entry in _visual(_table_ir("Automatic"))["visual"]["objects"]["values"]:
            assert entry["selector"]["data"] == [{"dataViewWildcard": {"matchingOption": 1}}]

    def test_a_native_rebuild_is_not_warned_about(self):
        # A Rules conditional format is a FAITHFUL rebuild, not a degradation -- warning on it would
        # be false noise. The deferral paths still warn; those are covered in
        # test_view_scoped_colour_defer.py.
        ir = _table_ir("Automatic")
        emit_pbir(ir)
        assert not [w for w in ir["warnings"] if "discrete colour deferred" in w["reason"]]


class TestTheDriverIsNotAColumn:
    def test_the_string_colour_driver_is_not_projected_as_a_value(self):
        # It is a STRING measure: left projected it renders a literal "negative"/"positive" column
        # beside the numbers the author asked for. The FillRule / Field-value expression resolves
        # against the MODEL, so the driver never needs to be in the query.
        visual = _visual(_table_ir("Automatic"))
        natives = [p["nativeQueryRef"] for p in _query_state(visual)["Values"]["projections"]]
        assert "Sign" not in natives
        assert natives, "the real measures still project"

    def test_the_pill_is_classified_discrete(self):
        color = _table_ir("Automatic")["worksheets"][0]["encodings"]["color"]
        assert color["discrete_measure"] is True
        assert color["kind"] == "value" and color["binding"] == "measure"


class TestItIsDiscreteNeverAGradient:
    """The 2.127.0 trap, re-guarded for a STRING domain.

    A discrete pill on Colour was once rebuilt as a `linearGradient3`. Power BI evaluates MIN/MAX
    over the fill input to find the ramp's endpoints, cannot do that to a non-numeric driver, and
    the visual dies at query time with "Error fetching data for this visual" -- through a PBIR
    validation that reports zero errors. A two-member string domain has no ordering to interpolate,
    so a gradient over it is not merely wrong, it is fatal.
    """

    def test_a_matrix_carries_no_gradient_for_a_string_domain(self):
        import json
        visual = _visual(_table_ir("Automatic"))
        blob = json.dumps(visual)
        assert "linearGradient" not in blob
        assert "FillRule" not in blob, "a FillRule is the gradient shape; this must be Field value"

    def test_the_worksheet_never_synthesises_a_colour_gradient(self):
        assert _table_ir("Automatic")["worksheets"][0]["color_gradient"] is None

    def test_the_gradient_guard_refuses_a_string_driver_outright(self):
        import twb_to_pbir as R
        color = _table_ir("Automatic")["worksheets"][0]["encodings"]["color"]
        assert R._gradient_input_is_safe(color) is False


class TestTheSameMechanismServesMarksAndBars:
    """One compiler, every visual family -- "cells or bars or marks or whatever"."""

    def _chart_ir(self, mark, calc=None, token="usr:Calculation_sign:nk"):
        enc = "<encodings><color column='[federated.abc].[%s]' /></encodings>" % token
        ws = _worksheet("Bars", mark,
                        rows="[federated.abc].[sum:Profit:qk]",
                        cols="[federated.abc].[none:Category:nk]",
                        deps_extra=_INST + (calc or _LABEL_CALC), encodings=enc)
        return parse_twb(_workbook(ws))

    def test_a_bar_chart_fills_its_marks_from_the_same_rules_conditional(self):
        objs = _visual(self._chart_ir("Bar"))["visual"]["objects"]
        expr = objs["dataPoint"][0]["properties"]["fill"]["solid"]["color"]["expr"]
        assert "Conditional" in expr
        assert expr["Conditional"]["Cases"][0]["Condition"]["Comparison"]["ComparisonKind"] == 3

    def test_a_chart_mark_fill_carries_the_wildcard_selector(self):
        # matchingOption 0 = every data point. Without it every mark paints identically.
        objs = _visual(self._chart_ir("Bar"))["visual"]["objects"]
        assert objs["dataPoint"][0]["selector"]["data"] == [
            {"dataViewWildcard": {"matchingOption": 0}}]

    def test_cells_and_bars_agree_on_the_colours_they_paint(self):
        # the same calc, the same members, the same palette -- only the CHANNEL differs
        import json
        cell = _visual(_table_ir("Automatic"))["visual"]["objects"]["values"][0]
        bar = _visual(self._chart_ir("Bar"))["visual"]["objects"]["dataPoint"][0]
        cell_expr = cell["properties"]["fontColor"]["solid"]["color"]["expr"]
        bar_expr = bar["properties"]["fill"]["solid"]["color"]["expr"]
        assert json.dumps(cell_expr, sort_keys=True) == json.dumps(bar_expr, sort_keys=True)


# a BOOLEAN driver -- no string members, so the compiler declines and the colour TWIN still serves
_BOOL_CALC = ("<column caption='Profitable?' datatype='boolean' name='[Calculation_pf]' "
              "role='measure' type='nominal'><calculation class='tableau' "
              "formula='SUM([Profit]) &gt; 0' /></column>"
              "<column-instance column='[Calculation_pf]' derivation='User' "
              "name='[usr:Calculation_pf:nk]' pivot='key' type='nominal' />")


class TestTheColourTwinIsStillTheFallback:
    """The compiler handles calcs that OUTPUT STRING MEMBERS. A boolean driver has no members to
    collapse into a palette, so it keeps the hex-returning twin it has had since 2.127.0. The two
    mechanisms are complementary, and the split is by what the calc RETURNS."""

    def _bool_chart(self):
        enc = "<encodings><color column='[federated.abc].[usr:Calculation_pf:nk]' /></encodings>"
        ws = _worksheet("Bars", "Bar",
                        rows="[federated.abc].[sum:Profit:qk]",
                        cols="[federated.abc].[none:Category:nk]",
                        deps_extra=_INST + _BOOL_CALC, encodings=enc)
        return parse_twb(_workbook(ws))

    def test_a_boolean_driver_is_not_lowerable_to_rules(self):
        import colour_rules as CR
        assert CR.analyse_colour_calc("SUM([Profit]) > 0").supported is False

    def test_a_boolean_driver_still_paints_from_the_twin(self):
        objs = _visual(self._bool_chart())["visual"]["objects"]
        fill = objs["dataPoint"][0]["properties"]["fill"]["solid"]["color"]["expr"]
        assert fill["Measure"]["Property"] == "Profitable? (colour)"


def test_the_ir_exports_only_an_explicitly_authored_palette():
    # The report->model channel. A worksheet whose author never opened the colour editor exports
    # nothing, and the model then falls back to Tableau's own assignment.
    import twb_to_pbir as R
    assert R.discrete_colour_palettes(_table_ir("Automatic")) == {}
    assert R.discrete_colour_palettes({"worksheets": []}) == {}
    assert R.discrete_colour_palettes(None) == {}


def test_an_authored_palette_reaches_the_model_channel():
    import twb_to_pbir as R
    style = ("<style><style-rule element='mark'>"
             "<encoding attr='color' field='[federated.abc].[usr:Calculation_sign:nk]' "
             "type='palette'>"
             "<map to='#111111'><bucket>&quot;negative&quot;</bucket></map>"
             "<map to='#222222'><bucket>&quot;positive&quot;</bucket></map>"
             "</encoding></style-rule></style>")
    ws = _worksheet("Coloured Table", "Automatic",
                    rows="[federated.abc].[none:Region:nk]",
                    cols="[federated.abc].[:Measure Names]",
                    deps_extra=_INST + _LABEL_CALC, encodings=_ENC, filters=_MV_ALL_FILTER,
                    style=style)
    palettes = R.discrete_colour_palettes(parse_twb(_workbook(ws)))
    assert palettes == {"Sign": [("negative", "#111111"), ("positive", "#222222")]}