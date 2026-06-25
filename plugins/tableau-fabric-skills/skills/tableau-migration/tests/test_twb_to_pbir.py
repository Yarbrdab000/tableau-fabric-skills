"""Tableau ``.twb`` viz-grammar -> PBIR wireframe tests (offline, inline XML fixtures).

Every fixture is a structurally faithful (but trimmed) Tableau workbook string; no files are
touched and no network is used. The asserts validate (a) the normalized IR a worksheet/
dashboard parses into, (b) the emitted PBIR JSON structure + field bindings per supported
visual, and (c) that unsupported marks/derivations/filters degrade to ``warnings`` instead of
producing a wrong visual.
"""
import json

import pytest

from twb_to_pbir import (
    MEASURES_TABLE,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    SCHEMA_VISUAL_FP,
    build_field_parameter_page,
    emit_pbir,
    field_parameter_slicer,
    field_parameter_table_visual,
    migrate_twb_to_pbir,
    parse_twb,
    report_json_part_fp,
)

# -- shared datasource (the workbook embeds the full relation + metadata tree) --
_DATASOURCE = """
  <datasources>
    <datasource caption='Superstore' inline='true' name='federated.abc' version='18.1'>
      <connection class='federated'>
        <relation name='Orders' table='[dbo].[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Category</remote-name><local-name>[Category]</local-name>
            <parent-name>[Orders]</parent-name><local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Region</remote-name><local-name>[Region]</local-name>
            <parent-name>[Orders]</parent-name><local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Sales Amount</remote-name><local-name>[Sales]</local-name>
            <parent-name>[Orders]</parent-name><local-type>real</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Profit</remote-name><local-name>[Profit]</local-name>
            <parent-name>[Orders]</parent-name><local-type>real</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Order Date</remote-name><local-name>[Order Date]</local-name>
            <parent-name>[Orders]</parent-name><local-type>datetime</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>State</remote-name><local-name>[State]</local-name>
            <parent-name>[Orders]</parent-name><local-type>string</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
    </datasource>
  </datasources>"""

# column declarations reused inside worksheet datasource-dependencies
_DEPS_COLUMNS = """
            <column caption='Category' datatype='string' name='[Category]' role='dimension' type='nominal' />
            <column caption='Region' datatype='string' name='[Region]' role='dimension' type='nominal' />
            <column caption='Sales' datatype='real' name='[Sales]' role='measure' type='quantitative' />
            <column caption='Profit' datatype='real' name='[Profit]' role='measure' type='quantitative' />
            <column caption='Order Date' datatype='datetime' name='[Order Date]' role='dimension' type='ordinal' />
            <column caption='State' datatype='string' name='[State]' role='dimension' semantic-role='[State].[Name]' type='nominal' />"""


def _workbook(worksheets, dashboards=""):
    return (
        "<?xml version='1.0' encoding='utf-8' ?>\n<workbook>"
        + _DATASOURCE
        + "<worksheets>" + worksheets + "</worksheets>"
        + ("<dashboards>" + dashboards + "</dashboards>" if dashboards else "")
        + "</workbook>"
    )


def _worksheet(name, mark, rows, cols, deps_extra="", encodings="", filters="", title="", style="", pane_extra=""):
    title_xml = (f"<layout-options><title><formatted-text>{title}</formatted-text>"
                 f"</title></layout-options>") if title else ""
    return f"""
    <worksheet name='{name}'>
      {title_xml}<table>
        <view>
          <datasources>
            <datasource caption='Superstore' name='federated.abc' />
          </datasources>
          <datasource-dependencies datasource='federated.abc'>{_DEPS_COLUMNS}{deps_extra}
          </datasource-dependencies>
          {filters}
        </view>
        {style}<panes><pane><mark class='{mark}' />{encodings}{pane_extra}</pane></panes>
        <rows>{rows}</rows>
        <cols>{cols}</cols>
      </table>
    </worksheet>"""


# common column-instances
_CI_CAT = "<column-instance column='[Category]' derivation='None' name='[none:Category:nk]' pivot='key' type='nominal' />"
_CI_REGION = "<column-instance column='[Region]' derivation='None' name='[none:Region:nk]' pivot='key' type='nominal' />"
_CI_SUM_SALES = "<column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' pivot='key' type='quantitative' />"
_CI_SUM_PROFIT = "<column-instance column='[Profit]' derivation='Sum' name='[sum:Profit:qk]' pivot='key' type='quantitative' />"
_CI_MONTH_DATE = "<column-instance column='[Order Date]' derivation='Month' name='[mn:Order Date:ok]' pivot='key' type='ordinal' />"
_CI_STATE = "<column-instance column='[State]' derivation='None' name='[none:State:nk]' pivot='key' type='nominal' />"
_INST = _CI_CAT + _CI_REGION + _CI_SUM_SALES + _CI_SUM_PROFIT + _CI_MONTH_DATE + _CI_STATE


def _visual_parts(parts):
    return {k: json.loads(v) for k, v in parts.items() if k.endswith("visual.json")}


def _query_state(visual_json):
    return visual_json["visual"]["query"]["queryState"]


# -- IR: clustered column ------------------------------------------------------
def test_bar_mark_dim_on_cols_is_column_chart_with_exact_bindings():
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "column"
    cat = w["cols"][0]
    val = w["rows"][0]
    # entity == relation name, property == clean_col(remote source name)
    assert (cat["entity"], cat["property"], cat["binding"]) == ("Orders", "Category", "column")
    assert (val["entity"], val["property"], val["binding"]) == ("Orders", "Sales_Amount", "aggregation")
    assert val["aggregation"] == "Sum"
    assert ir["warnings"] == []


def test_renamed_caption_still_binds_to_remote_source_column():
    # caption "Sales" but the remote source column is "Sales Amount" -> clean_col -> Sales_Amount
    ws = _worksheet("S", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["rows"][0]["property"] == "Sales_Amount"


# -- IR: horizontal bar --------------------------------------------------------
def test_bar_mark_dim_on_rows_is_bar_chart():
    ws = _worksheet("Profit by Region", "Bar",
                    rows="[federated.abc].[none:Region:nk]",
                    cols="[federated.abc].[sum:Profit:qk]",
                    deps_extra=_INST)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["visual_type"] == "bar"


# -- IR + emit: line -----------------------------------------------------------
def test_line_chart_date_part_is_category_with_grain_warning():
    ws = _worksheet("Sales Trend", "Line",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[mn:Order Date:ok]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "line"
    assert w["cols"][0]["kind"] == "category"
    assert w["cols"][0]["property"] == "Order_Date"
    assert any("date part" in x["reason"].lower() for x in ir["warnings"])

    parts = emit_pbir(ir)
    vis = list(_visual_parts(parts).values())
    state = _query_state(vis[0])
    assert vis[0]["visual"]["visualType"] == "lineChart"
    assert "Category" in state and "Y" in state


def test_line_chart_truncated_date_stays_on_x_axis_region_to_series():
    # Sheet-2 shape: a continuous truncated date on Columns (the x-axis) and a discrete
    # dimension paning the lines on Rows alongside the measures. The date must stay on the
    # x-axis; the paning dimension becomes the legend/Series -- it must never replace the date
    # on the category axis. Tableau serialises the month truncation as 'Month-Trunc'.
    tmonth = ("<column-instance column='[Order Date]' derivation='Month-Trunc' "
              "name='[tmn:Order Date:qk]' pivot='key' type='quantitative' />")
    ws = _worksheet("Trend by Region", "Line",
                    rows="([federated.abc].[none:Region:nk] * "
                         "([federated.abc].[sum:Sales:qk] + [federated.abc].[sum:Profit:qk]))",
                    cols="[federated.abc].[tmn:Order Date:qk]",
                    deps_extra=_INST + tmonth)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "line"
    # the truncated date resolved (was previously dropped as an "unsupported derivation")
    assert any("grain not applied" in x["reason"].lower() for x in ir["warnings"])
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "Order_Date"
    assert {p["field"]["Aggregation"]["Expression"]["Column"]["Property"]
            for p in state["Y"]["projections"]} == {"Sales_Amount", "Profit"}
    # the Rows paning dimension lands on Small multiples (Tableau trellis), not the x-axis
    assert state["SmallMultiples"]["projections"][0]["field"]["Column"]["Property"] == "Region"
    assert "Series" not in state


# -- IR: Automatic mark + continuous date -> line (Tableau's default chart type) ----
# An Automatic mark over a CONTINUOUS (green) date axis is Tableau's default LINE chart; a discrete
# date PART (blue) stays bars, and an explicit bar mark always stays bars. Only the chart TYPE
# changes -- the field bindings are identical to a line over the same shelves.
def test_automatic_mark_with_continuous_date_axis_is_a_line_not_column():
    tmonth = ("<column-instance column='[Order Date]' derivation='Month-Trunc' "
              "name='[tmn:Order Date:qk]' pivot='key' type='quantitative' />")
    ws = _worksheet("Sales Trend", "Automatic",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[tmn:Order Date:qk]",
                    deps_extra=_INST + tmonth)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "line"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "lineChart"
    state = _query_state(vis)
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "Order_Date"
    assert (state["Y"]["projections"][0]["field"]["Aggregation"]["Expression"]
            ["Column"]["Property"]) == "Sales_Amount"


def test_automatic_mark_with_discrete_date_part_stays_column():
    # a DISCRETE date PART (derivation 'Month', the `mn:` pill in _INST) is Tableau's default BARS,
    # not a line -- only a continuous (-Trunc) date routes to a line.
    ws = _worksheet("Sales by Month", "Automatic",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[mn:Order Date:ok]",
                    deps_extra=_INST)
    assert parse_twb(_workbook(ws))["worksheets"][0]["visual_type"] == "column"


def test_explicit_bar_mark_with_continuous_date_stays_column():
    # the continuous-date -> line default applies ONLY to the Automatic mark; an explicit Bar mark
    # means the author chose bars, so it stays a column chart even over a continuous date.
    tmonth = ("<column-instance column='[Order Date]' derivation='Month-Trunc' "
              "name='[tmn:Order Date:qk]' pivot='key' type='quantitative' />")
    ws = _worksheet("Bars over time", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[tmn:Order Date:qk]",
                    deps_extra=_INST + tmonth)
    assert parse_twb(_workbook(ws))["worksheets"][0]["visual_type"] == "column"


# -- IR + emit: dual-axis combo ------------------------------------------------
def _combo_worksheet(name, rows, cols, panes, deps_extra=""):
    # like _worksheet but with an explicit multi-pane <panes> block (dual axis)
    return f"""
    <worksheet name='{name}'>
      <table>
        <view>
          <datasources>
            <datasource caption='Superstore' name='federated.abc' />
          </datasources>
          <datasource-dependencies datasource='federated.abc'>{_DEPS_COLUMNS}{deps_extra}
          </datasource-dependencies>
        </view>
        {panes}
        <rows>{rows}</rows>
        <cols>{cols}</cols>
      </table>
    </worksheet>"""


def test_dual_axis_bar_plus_line_is_combo_chart_y_and_y2():
    # Tableau dual axis: two measures on Rows -- one drawn as Bar (primary), the other as Line
    # (secondary, y-index=1). Each axis pane names its measure via y-axis-name. Faithful target is
    # a combo: the bar measure on Y, the line measure on Y2, the date on the shared Category axis.
    panes = (
        "<panes>"
        "<pane><mark class='Bar' /></pane>"
        "<pane id='1' y-axis-name='[federated.abc].[sum:Sales:qk]'>"
        "<mark class='Bar' /></pane>"
        "<pane id='2' y-index='1' y-axis-name='[federated.abc].[sum:Profit:qk]'>"
        "<mark class='Line' /></pane>"
        "</panes>")
    ws = _combo_worksheet(
        "Sales and Profit Trend",
        rows="([federated.abc].[sum:Sales:qk] + [federated.abc].[sum:Profit:qk])",
        cols="[federated.abc].[mn:Order Date:ok]",
        panes=panes, deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "combo"

    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "lineClusteredColumnComboChart"
    state = _query_state(vis)
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "Order_Date"
    assert {p["field"]["Aggregation"]["Expression"]["Column"]["Property"]
            for p in state["Y"]["projections"]} == {"Sales_Amount"}
    assert {p["field"]["Aggregation"]["Expression"]["Column"]["Property"]
            for p in state["Y2"]["projections"]} == {"Profit"}


def test_two_measures_same_mark_stay_clustered_not_combo():
    # Two measures both drawn as Bar (no line family) is NOT a combo -- it stays an ordinary
    # multi-measure clustered column chart (both measures in Y). Guards against false combos.
    panes = (
        "<panes>"
        "<pane><mark class='Bar' /></pane>"
        "<pane id='1' y-axis-name='[federated.abc].[sum:Sales:qk]'>"
        "<mark class='Bar' /></pane>"
        "<pane id='2' y-index='1' y-axis-name='[federated.abc].[sum:Profit:qk]'>"
        "<mark class='Bar' /></pane>"
        "</panes>")
    ws = _combo_worksheet(
        "Both Bars",
        rows="([federated.abc].[sum:Sales:qk] + [federated.abc].[sum:Profit:qk])",
        cols="[federated.abc].[mn:Order Date:ok]",
        panes=panes, deps_extra=_INST)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["visual_type"] == "column"
    assert w["combo_split"] is None


# -- IR: text table & matrix ---------------------------------------------------
def test_text_mark_one_axis_is_table():
    ws = _worksheet("Detail", "Text",
                    rows="[federated.abc].[none:Category:nk]",
                    cols="[federated.abc].[sum:Sales:qk]",
                    deps_extra=_INST)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["visual_type"] == "table"


def test_text_mark_both_axes_is_matrix_with_rows_columns_values():
    enc = "<encodings><text column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    ws = _worksheet("Cross", "Text",
                    rows="[federated.abc].[none:Category:nk]",
                    cols="[federated.abc].[none:Region:nk]",
                    deps_extra=_INST, encodings=enc)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "matrix"

    parts = emit_pbir(ir)
    state = _query_state(list(_visual_parts(parts).values())[0])
    assert set(state) == {"Rows", "Columns", "Values"}
    assert state["Rows"]["projections"][0]["field"]["Column"]["Property"] == "Category"
    assert state["Columns"]["projections"][0]["field"]["Column"]["Property"] == "Region"
    assert state["Values"]["projections"][0]["field"]["Aggregation"]["Function"] == 0


# -- IR + emit: highlight table (Square mark) ----------------------------------
def test_square_mark_both_axes_with_colour_measure_is_highlight_table_matrix():
    # A Tableau highlight table uses the Square mark with dimensions on both axes and the measure
    # on the colour (saturation) encoding. Faithful Tier-1 target is a matrix -- the measure on
    # colour becomes the displayed Values; the colour styling itself is a later (Tier-2) pass.
    enc = "<encodings><color column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    ws = _worksheet("Heat", "Square",
                    rows="[federated.abc].[none:Category:nk]",
                    cols="[federated.abc].[none:Region:nk]",
                    deps_extra=_INST, encodings=enc)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "matrix"
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    assert set(state) == {"Rows", "Columns", "Values"}
    assert state["Rows"]["projections"][0]["field"]["Column"]["Property"] == "Category"
    assert state["Columns"]["projections"][0]["field"]["Column"]["Property"] == "Region"
    assert (state["Values"]["projections"][0]["field"]["Aggregation"]
            ["Expression"]["Column"]["Property"]) == "Sales_Amount"


def test_square_mark_without_axis_dims_stays_unsupported():
    # A Square mark with NO axis dimensions (treemap / packed-bubble / heatmap layout: the
    # dimension is on detail, the measure on colour) is deferred -> warn, not guessed as a chart.
    enc = ("<encodings><color column='[federated.abc].[sum:Sales:qk]' />"
           "<lod column='[federated.abc].[none:Category:nk]' /></encodings>")
    ws = _worksheet("Packed", "Square", rows="", cols="",
                    deps_extra=_INST, encodings=enc)
    assert parse_twb(_workbook(ws))["worksheets"][0]["visual_type"] == "unsupported"


# -- IR + emit: computed-sort (sort a dimension by a measure) ------------------
def _sort_definition(visual_json):
    return visual_json["visual"]["query"].get("sortDefinition")


def test_computed_sort_on_bound_measure_emits_sort_definition():
    # Tableau sorts a dimension by a measure via <computed-sort>. When that measure is bound in the
    # visual (here SUM(Sales) on Y), the faithful Power BI equivalent is a visual.query.sortDefinition
    # referencing the same field expression with direction Descending.
    sort = ("<computed-sort column='[federated.abc].[none:Category:nk]' direction='DESC' "
            "using='[federated.abc].[sum:Sales:qk]' />")
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=sort)
    ir = parse_twb(_workbook(ws))
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    sd = _sort_definition(vis)
    assert sd is not None
    assert sd["isDefaultSort"] is False
    assert len(sd["sort"]) == 1
    assert sd["sort"][0]["direction"] == "Descending"
    # the sort field is the very same expression bound in the Y role (no dangling reference)
    sort_field = sd["sort"][0]["field"]
    assert sort_field["Aggregation"]["Expression"]["Column"]["Property"] == "Sales_Amount"
    assert sort_field == _query_state(vis)["Y"]["projections"][0]["field"]


def test_computed_sort_on_unbound_measure_emits_no_sort_definition():
    # The dimension is sorted by SUM(Profit), but Profit is not shown anywhere in the visual.
    # Sorting by an unbound field would be a dangling reference, so warn-never-wrong drops the sort
    # entirely (the visual still renders in faithful default order).
    sort = ("<computed-sort column='[federated.abc].[none:Category:nk]' direction='DESC' "
            "using='[federated.abc].[sum:Profit:qk]' />")
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=sort)
    ir = parse_twb(_workbook(ws))
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert _sort_definition(vis) is None


# -- IR: calculated field -> measure ------------------------------------------
def test_calculated_field_binds_to_measures_table_by_caption():
    calc_col = ("<column caption='Profit Ratio' datatype='real' name='[Calculation_1]' "
                "role='measure' type='quantitative'>"
                "<calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />"
                "</column>")
    calc_inst = ("<column-instance column='[Calculation_1]' derivation='None' "
                 "name='[none:Calculation_1:qk]' pivot='key' type='quantitative' />")
    ws = _worksheet("Ratio by Cat", "Bar",
                    rows="[federated.abc].[none:Calculation_1:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + calc_col + calc_inst)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    measure = w["rows"][0]
    assert measure["is_calc"] is True
    assert measure["binding"] == "measure"
    assert (measure["entity"], measure["property"]) == (MEASURES_TABLE, "Profit Ratio")

    parts = emit_pbir(parse_twb(_workbook(ws)))
    state = _query_state(list(_visual_parts(parts).values())[0])
    yexpr = state["Y"]["projections"][0]["field"]
    assert yexpr["Measure"]["Expression"]["SourceRef"]["Entity"] == MEASURES_TABLE
    assert yexpr["Measure"]["Property"] == "Profit Ratio"


# -- unsupported handling ------------------------------------------------------
def test_unsupported_mark_produces_warning_and_no_visual():
    ws = _worksheet("Gantt Chart", "Gantt",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "unsupported"
    assert any(x["scope"] == "worksheet" and "Gantt" in x["reason"] for x in ir["warnings"])
    parts = emit_pbir(ir)
    assert _visual_parts(parts) == {}  # no visual emitted for the unsupported mark


def test_empty_worksheet_is_classified_as_empty_not_unsupported_mark():
    # A structurally bare sheet (no fields on any shelf or encoding) is a blank/text placeholder,
    # not an unsupported visual -> a precise "empty worksheet" note, not a "mark not supported".
    ws = _worksheet("Spacer", "Automatic", rows="", cols="", deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "unsupported"
    reasons = [x["reason"] for x in ir["warnings"] if x["scope"] == "worksheet"]
    assert any("empty worksheet" in r for r in reasons)
    assert not any("not supported" in r for r in reasons)


def test_unresolved_pills_are_not_misclassified_as_empty():
    # A sheet whose pills exist but fail to resolve is a real binding gap, NOT an empty sheet:
    # it must keep the generic "not supported" warning (plus its resolve warning), never "empty".
    ws = _worksheet("Broken", "Bar",
                    rows="[federated.abc].[none:Nonexistent:nk]",
                    cols="", deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    reasons = [x["reason"] for x in ir["warnings"] if x["scope"] == "worksheet"]
    assert not any("empty worksheet" in r for r in reasons)
    assert any("could not resolve" in r for r in reasons)


def test_single_dimension_on_label_is_one_column_table():
    # An "Automatic" sheet with a lone categorical field on the Label encoding (no axis pills,
    # no measure) is Tableau's text-list display of that field -> a faithful one-column table.
    enc = "<encodings><label column='[federated.abc].[none:Category:nk]' /></encodings>"
    ws = _worksheet("Genre", "Automatic", rows="", cols="", deps_extra=_INST, encodings=enc)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "table"
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    assert set(state) == {"Values"}
    projs = state["Values"]["projections"]
    assert len(projs) == 1
    assert projs[0]["field"]["Column"]["Property"] == "Category"


def test_single_dimension_color_and_label_same_field_is_one_column():
    # Tableau routinely drops the same field on both Colour and Label; the one-column table must
    # list it exactly once (deduped by model binding), never twice.
    enc = ("<encodings><color column='[federated.abc].[none:Category:nk]' />"
           "<label column='[federated.abc].[none:Category:nk]' /></encodings>")
    ws = _worksheet("Job Class", "Automatic", rows="", cols="",
                    deps_extra=_INST, encodings=enc)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "table"
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    assert len(state["Values"]["projections"]) == 1


def test_geo_dimension_on_detail_only_is_location_only_shapemap():
    # A geographic dimension alone on Detail with no measure is Tableau's default map of that
    # geography -> a faithful location-only shapeMap (Category = the location, no colour Value);
    # it must NOT be flattened into a one-column text list.
    enc = "<encodings><lod column='[federated.abc].[none:State:nk]' /></encodings>"
    ws = _worksheet("State Map", "Automatic", rows="", cols="",
                    deps_extra=_INST, encodings=enc)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "filled_map"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "shapeMap"
    state = _query_state(vis)
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "State"
    assert "Value" not in state


def test_area_mark_maps_to_area_chart():
    # Power BI has a native areaChart, so an ``area`` mark binds to areaChart (its own chart type)
    # with the same axes/encodings a line would use -- getting the chart TYPE right (Tier-1). The
    # stacked-vs-overlapping fill is a deferred Tier-2 property.
    ws = _worksheet("Sales Trend", "Area",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "area"
    parts = emit_pbir(ir)
    vis = list(_visual_parts(parts).values())[0]
    assert vis["visual"]["visualType"] == "areaChart"


def test_unsupported_derivation_is_skipped_with_warning():
    bad_inst = ("<column-instance column='[Sales]' derivation='WindowSum' "
                "name='[tablecalc:Sales:qk]' pivot='key' type='quantitative' />")
    ws = _worksheet("Tablecalc", "Bar",
                    rows="[federated.abc].[tablecalc:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + bad_inst)
    ir = parse_twb(_workbook(ws))
    assert any("WindowSum" in x["reason"] for x in ir["warnings"])
    # the bad pill is dropped from the rows shelf
    assert all(f["aggregation"] != "WindowSum" for f in ir["worksheets"][0]["rows"])


def test_sum_on_string_column_is_skipped_with_warning():
    bad_inst = ("<column-instance column='[Category]' derivation='Sum' "
                "name='[sum:Category:qk]' pivot='key' type='quantitative' />")
    ws = _worksheet("BadAgg", "Bar",
                    rows="[federated.abc].[sum:Category:qk]",
                    cols="[federated.abc].[none:Region:nk]",
                    deps_extra=_INST + bad_inst)
    ir = parse_twb(_workbook(ws))
    assert any("non-numeric" in x["reason"] for x in ir["warnings"])
    assert ir["worksheets"][0]["rows"] == []


# -- filters -> slicers --------------------------------------------------------
def test_categorical_filter_becomes_slicer_visual():
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='member' level='[none:Region:nk]' /></filter>")
    ws = _worksheet("Filtered", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=filt)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert len(w["filters"]) == 1
    assert w["filters"][0]["filter_kind"] == "categorical"

    parts = emit_pbir(ir)
    slicers = [v for v in _visual_parts(parts).values()
               if v["visual"]["visualType"] == "slicer"]
    assert len(slicers) == 1
    prop = (slicers[0]["visual"]["query"]["queryState"]["Values"]["projections"][0]
            ["field"]["Column"]["Property"])
    assert prop == "Region"


def test_quantitative_filter_on_date_is_date_range():
    filt = "<filter class='quantitative' column='[federated.abc].[none:Order Date:ok]' />"
    inst = "<column-instance column='[Order Date]' derivation='None' name='[none:Order Date:ok]' pivot='key' type='ordinal' />"
    ws = _worksheet("DateFilter", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + inst, filters=filt)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["filters"][0]["filter_kind"] == "date_range"


# -- applied filter selection -> slicer filterConfig ---------------------------
# A worksheet filter that narrows a field to specific members (or a numeric range) carries that
# selection onto the rebuilt slicer's ``filterConfig`` so the report opens on the SAME filtered
# view. Only faithfully bindable, JSON-verified shapes are emitted (categorical include/exclude on
# a STRING dimension; numeric range); date-part members, the %null% sentinel, and fixed date ranges
# stay at the slicer's "show all" default with a fidelity note (warn-never-wrong).
_CI_SALES_RAW = ("<column-instance column='[Sales]' derivation='None' "
                 "name='[none:Sales:qk]' pivot='key' type='quantitative' />")


def _slicer_filter_configs(parts):
    return [v["filterConfig"] for v in _visual_parts(parts).values()
            if v["visual"]["visualType"] == "slicer" and v.get("filterConfig")]


def _filter_scope_warnings(ir):
    return [w["reason"] for w in ir["warnings"] if w["scope"] == "filter"]


def test_categorical_include_selection_emits_in_filter_on_slicer():
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='union' op='manual'>"
            "<groupfilter function='member' member='&quot;South&quot;' />"
            "<groupfilter function='member' member='&quot;East&quot;' />"
            "</groupfilter></filter>")
    ws = _worksheet("Inc", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=filt)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["filters"][0]["selection"] == {
        "mode": "include", "values": ["South", "East"]}

    configs = _slicer_filter_configs(emit_pbir(ir))
    assert len(configs) == 1
    cont = configs[0]["filters"][0]
    assert cont["type"] == "Categorical"
    assert cont["field"]["Column"]["Property"] == "Region"
    in_expr = cont["filter"]["Where"][0]["Condition"]["In"]
    vals = [row[0]["Literal"]["Value"] for row in in_expr["Values"]]
    assert vals == ["'South'", "'East'"]
    assert "objects" not in cont  # an include is not an inverted selection


def test_categorical_exclude_selection_emits_inverted_not_in_filter():
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='except'>"
            "<groupfilter function='level-members' level='[none:Region:nk]' />"
            "<groupfilter function='member' member='&quot;West&quot;' />"
            "</groupfilter></filter>")
    ws = _worksheet("Exc", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=filt)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["filters"][0]["selection"] == {
        "mode": "exclude", "values": ["West"]}

    cont = _slicer_filter_configs(emit_pbir(ir))[0]["filters"][0]
    assert cont["type"] == "Categorical"
    not_in = cont["filter"]["Where"][0]["Condition"]["Not"]["Expression"]["In"]
    assert [row[0]["Literal"]["Value"] for row in not_in["Values"]] == ["'West'"]
    inverted = cont["objects"]["general"][0]["properties"]["isInvertedSelectionMode"]
    assert inverted["expr"]["Literal"]["Value"] == "true"


def test_apostrophe_member_is_sql_escaped_in_literal():
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='member' member='&quot;O&apos;Brien&quot;' /></filter>")
    ws = _worksheet("Apos", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=filt)
    cont = _slicer_filter_configs(emit_pbir(parse_twb(_workbook(ws))))[0]["filters"][0]
    val = cont["filter"]["Where"][0]["Condition"]["In"]["Values"][0][0]["Literal"]["Value"]
    assert val == "'O''Brien'"


def test_numeric_range_selection_emits_advanced_comparison_filter():
    filt = ("<filter class='quantitative' column='[federated.abc].[none:Sales:qk]'>"
            "<min>10</min><max>500</max></filter>")
    ws = _worksheet("Rng", "Bar",
                    rows="[federated.abc].[none:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _CI_SALES_RAW, filters=filt)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["filters"][0]["range"] == {"min": "10", "max": "500"}

    cont = _slicer_filter_configs(emit_pbir(ir))[0]["filters"][0]
    assert cont["type"] == "Advanced"
    both = cont["filter"]["Where"][0]["Condition"]["And"]
    assert both["Left"]["Comparison"]["ComparisonKind"] == 2
    assert both["Left"]["Comparison"]["Right"]["Literal"]["Value"] == "10L"
    assert both["Right"]["Comparison"]["ComparisonKind"] == 4
    assert both["Right"]["Comparison"]["Right"]["Literal"]["Value"] == "500L"


def test_date_part_categorical_selection_defers_to_default_with_warning():
    # A categorical filter on a DATE field is a date-part filter (month '4'); binding the part
    # value to the raw date column would be wrong -> no filterConfig, fidelity note instead.
    filt = ("<filter class='categorical' column='[federated.abc].[mn:Order Date:ok]'>"
            "<groupfilter function='member' member='&quot;4&quot;' /></filter>")
    ws = _worksheet("DPart", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=filt)
    ir = parse_twb(_workbook(ws))
    assert _slicer_filter_configs(emit_pbir(ir)) == []
    assert any("date-part" in r for r in _filter_scope_warnings(ir))


def test_null_only_selection_defers_to_default_with_warning():
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='member' member='&quot;%null%&quot;' /></filter>")
    ws = _worksheet("NullF", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=filt)
    ir = parse_twb(_workbook(ws))
    assert _slicer_filter_configs(emit_pbir(ir)) == []
    assert any("sentinel" in r for r in _filter_scope_warnings(ir))


def test_fixed_date_range_selection_defers_to_default_with_warning():
    filt = ("<filter class='quantitative' column='[federated.abc].[none:Order Date:ok]'>"
            "<min>#2020-01-01#</min><max>#2020-12-31#</max></filter>")
    inst = ("<column-instance column='[Order Date]' derivation='None' "
            "name='[none:Order Date:ok]' pivot='key' type='ordinal' />")
    ws = _worksheet("DRange", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + inst, filters=filt)
    ir = parse_twb(_workbook(ws))
    assert _slicer_filter_configs(emit_pbir(ir)) == []
    assert any("date range" in r for r in _filter_scope_warnings(ir))


def test_unselected_filter_emits_slicer_without_filter_config():
    # A filter that does not narrow to specific members (just exposes the field) -> a plain slicer
    # with no pre-selection (shows all), exactly as before applied-selection support.
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='member' level='[none:Region:nk]' /></filter>")
    ws = _worksheet("Plain", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=filt)
    parts = emit_pbir(parse_twb(_workbook(ws)))
    slicers = [v for v in _visual_parts(parts).values()
               if v["visual"]["visualType"] == "slicer"]
    assert len(slicers) == 1
    assert "filterConfig" not in slicers[0]


# -- Tableau internal / auto-generated pseudo-fields are silenced --------------
# Tableau auto-adds helper fields the user never created: dashboard filter/set *action* groups
# (``user:auto-column='sheet_link'``) and the ``__tableau_internal_object_id__`` row-count
# internal. They surface as worksheet filter/shelf refs but have no user model binding, so they
# are dropped SILENTLY (never a false "could not resolve" warning), not routed to a slicer.
_USER_NS = "xmlns:user='http://www.tableausoftware.com/xml/user'"
_ACTION_DATASOURCE = """
  <datasources>
    <datasource caption='Superstore' inline='true' name='federated.abc' version='18.1'>
      <connection class='federated'>
        <relation name='Orders' table='[dbo].[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Region</remote-name><local-name>[Region]</local-name>
            <parent-name>[Orders]</parent-name><local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Category</remote-name><local-name>[Category]</local-name>
            <parent-name>[Orders]</parent-name><local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Sales Amount</remote-name><local-name>[Sales]</local-name>
            <parent-name>[Orders]</parent-name><local-type>real</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
      <group caption='Action (Region)' hidden='true' name='[Action (Region)]' name-style='unqualified' user:auto-column='sheet_link'>
        <groupfilter function='crossjoin'>
          <groupfilter function='level-members' level='[Region]' />
        </groupfilter>
      </group>
    </datasource>
  </datasources>"""


def _ns_workbook(datasource, worksheets):
    return (
        "<?xml version='1.0' encoding='utf-8' ?>\n"
        f"<workbook {_USER_NS}>" + datasource
        + "<worksheets>" + worksheets + "</worksheets></workbook>"
    )


def test_action_auto_column_filter_is_dropped_silently():
    filt = ("<filter class='categorical' column='[federated.abc].[Action (Region)]'>"
            "<groupfilter function='member' member='&quot;East&quot;' /></filter>")
    ws = _worksheet("Act", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=filt)
    ir = parse_twb(_ns_workbook(_ACTION_DATASOURCE, ws))
    w = ir["worksheets"][0]
    blob = json.dumps(ir["warnings"])
    # the action pseudo-field never becomes a filter and never raises a false warning ...
    assert w["filters"] == []
    assert "Action (Region)" not in blob
    assert "could not resolve" not in blob
    # ... while the genuine fields still build the real visual.
    assert w["visual_type"] == "column"


def test_internal_object_id_filter_is_dropped_silently():
    filt = ("<filter class='categorical' "
            "column='[federated.abc].[__tableau_internal_object_id__]'>"
            "<groupfilter function='member' member='&quot;1&quot;' /></filter>")
    ws = _worksheet("Obj", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=filt)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    blob = json.dumps(ir["warnings"])
    assert w["filters"] == []
    assert "__tableau_internal_object_id__" not in blob
    assert "could not resolve" not in blob
    assert w["visual_type"] == "column"


def test_unknown_field_still_warns_after_internal_silencing():
    # The silencing is TARGETED: a real (non-internal) field that cannot be resolved must still
    # warn, so the noise fix never masks a genuine missing binding.
    filt = ("<filter class='categorical' column='[federated.abc].[Mystery]'>"
            "<groupfilter function='member' member='&quot;X&quot;' /></filter>")
    ws = _worksheet("Unk", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=filt)
    ir = parse_twb(_workbook(ws))
    blob = json.dumps(ir["warnings"])
    assert "could not resolve" in blob and "Mystery" in blob


# -- implicit row count (object-id COUNT(*) / legacy [Number of Records]) ------
# Tableau computes "count the rows of a table" two ways, neither naming a real model column: an
# aggregation over __tableau_internal_object_id__ (a Count column-instance encoding the table) and
# the legacy auto-generated [Number of Records] (the constant 1 summed). Both mean COUNTROWS of a
# table -> the faithful Power BI target is a COUNTROWS measure. Unrecognised, the first is silently
# dropped (empty visual) and the second emits a dangling SUM([Number of Records]). The recognizer
# binds when a row_count_binding target is supplied and otherwise warns precisely (never dangling).
_OID = "__tableau_internal_object_id__"
_HEX = "ECFCA1FB690A41FE803BC071773BA862"
_HEX2 = "D73023733B004CC1B3CB1ACF62F4A965"
_COL_OID_ORDERS = (f"<column caption='Orders' datatype='integer' "
                   f"name='[{_OID}].[Orders_{_HEX}]' role='measure' type='quantitative' />")
_CI_CNT_ORDERS = (f"<column-instance column='[{_OID}].[Orders_{_HEX}]' derivation='Count' "
                  f"name='[cnt:Orders_{_HEX}:qk]' pivot='key' type='quantitative' />")
_CI_CNT_PEOPLE = (f"<column-instance column='[{_OID}].[People_{_HEX2}]' derivation='Count' "
                  f"name='[cnt:People_{_HEX2}:qk]' pivot='key' type='quantitative' />")
_OID_COUNT_PILL = f"[federated.abc].[{_OID}].[cnt:Orders_{_HEX}:qk]"

_COL_NUMREC = ("<column caption='Number of Records' datatype='integer' "
               "name='[Number of Records]' role='measure' type='quantitative' />")
_CI_SUM_NUMREC = ("<column-instance column='[Number of Records]' derivation='Sum' "
                  "name='[sum:Number of Records:qk]' pivot='key' type='quantitative' />")
_NUMREC_PILL = "[federated.abc].[sum:Number of Records:qk]"


def _count_warns(ir):
    return [w["reason"] for w in ir["warnings"] if "implicit row count" in w["reason"]]


def test_object_id_row_count_warns_when_unbound_and_never_dangles():
    # object-id COUNT(*) with no binding target -> precise warning naming the table (resolved from
    # the object-id column caption), the count dropped, and NO object-id ref leaking into the report.
    ws = _worksheet("Cnt", "Bar",
                    rows=_OID_COUNT_PILL,
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _COL_OID_ORDERS + _CI_CNT_ORDERS)
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    warns = _count_warns(res["ir"])
    assert any("COUNT('Orders')" in r and "COUNTROWS" in r for r in warns)
    assert _OID not in json.dumps(res["parts"])
    # the count pill is dropped, never bound to a fabricated column.
    assert res["ir"]["worksheets"][0]["rows"] == []


def test_object_id_row_count_binds_when_target_supplied():
    ws = _worksheet("Cnt", "Bar",
                    rows=_OID_COUNT_PILL,
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _COL_OID_ORDERS + _CI_CNT_ORDERS)
    rcb = {"measures": {"Orders": {"entity": "Orders", "measure": "Rows"}}}
    ir = parse_twb(_workbook(ws), row_count_binding=rcb)
    rows = ir["worksheets"][0]["rows"]
    assert len(rows) == 1
    f = rows[0]
    assert f["binding"] == "measure" and f["entity"] == "Orders" and f["property"] == "Rows"
    assert _count_warns(ir) == []


def test_pilot_line_chart_object_id_count_over_continuous_date_is_a_line():
    # The Comcast pilot "Line chart" shape: an Automatic mark plotting the implicit object-id COUNT
    # (rows) over a CONTINUOUS truncated date (cols). With a row_count_binding the COUNT binds to a
    # model measure AND the continuous date makes it a LINE (not a column) -- the chart type Tableau
    # actually renders. Ties the row-count binding and the continuous-date routing together.
    tday = ("<column-instance column='[Order Date]' derivation='Day-Trunc' "
            "name='[tdy:Order Date:qk]' pivot='key' type='quantitative' />")
    ws = _worksheet("Line chart", "Automatic",
                    rows=_OID_COUNT_PILL,
                    cols="[federated.abc].[tdy:Order Date:qk]",
                    deps_extra=_INST + _COL_OID_ORDERS + _CI_CNT_ORDERS + tday)
    rcb = {"measures": {"Orders": {"entity": "_Measures", "measure": "count orders"}}}
    ir = parse_twb(_workbook(ws), row_count_binding=rcb)
    assert ir["worksheets"][0]["visual_type"] == "line"
    assert _count_warns(ir) == []
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "lineChart"
    state = _query_state(vis)
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "Order_Date"
    ymeas = state["Y"]["projections"][0]["field"]
    assert ymeas["Measure"]["Property"] == "count orders"
    assert ymeas["Measure"]["Expression"]["SourceRef"]["Entity"] == "_Measures"


def test_object_id_row_count_ambiguous_multi_table_warns_generic():
    # two distinct count instances in the worksheet's dependencies -> the binder cannot know which
    # fact to count, so it defers with a generic warning listing the candidates (never guesses).
    ws = _worksheet("Cnt", "Bar",
                    rows=_OID_COUNT_PILL,
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _CI_CNT_ORDERS + _CI_CNT_PEOPLE)
    ir = parse_twb(_workbook(ws))
    warns = _count_warns(ir)
    assert any("ambiguous across tables" in r and "Orders" in r and "People" in r for r in warns)


def test_numrec_row_count_warns_not_dangling():
    # legacy [Number of Records] summed -> recognised as a row count and warned, NOT emitted as a
    # dangling SUM('Orders'[Number of Records]) against a column the model never had.
    ws = _worksheet("Recs", "Bar",
                    rows=_NUMREC_PILL,
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _COL_NUMREC + _CI_SUM_NUMREC)
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    warns = _count_warns(res["ir"])
    assert any("[Number of Records]" in r and "COUNTROWS" in r for r in warns)
    assert "Number of Records" not in json.dumps(res["parts"])
    assert res["ir"]["worksheets"][0]["rows"] == []


def test_numrec_row_count_binds_via_default():
    ws = _worksheet("Recs", "Bar",
                    rows=_NUMREC_PILL,
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _COL_NUMREC + _CI_SUM_NUMREC)
    rcb = {"measures": {}, "default": {"entity": "Orders", "measure": "Rows"}}
    ir = parse_twb(_workbook(ws), row_count_binding=rcb)
    rows = ir["worksheets"][0]["rows"]
    assert len(rows) == 1 and rows[0]["binding"] == "measure"
    assert rows[0]["entity"] == "Orders" and rows[0]["property"] == "Rows"
    assert _count_warns(ir) == []


def test_real_countd_on_column_is_not_a_row_count():
    # A genuine COUNT/COUNTD on a real column (here CountD of Category) is distinct values, NOT a
    # table row count -> it must keep its ordinary aggregation binding and never be swept up.
    cntd = ("<column-instance column='[Category]' derivation='CountD' "
            "name='[ctd:Category:nk]' pivot='key' type='quantitative' />")
    ws = _worksheet("Distinct", "Bar",
                    rows="[federated.abc].[ctd:Category:nk]",
                    cols="[federated.abc].[none:Region:nk]",
                    deps_extra=_INST + cntd)
    ir = parse_twb(_workbook(ws))
    assert _count_warns(ir) == []
    rows = ir["worksheets"][0]["rows"]
    assert len(rows) == 1
    assert rows[0]["binding"] == "aggregation" and rows[0]["aggregation"] == "CountD"
    assert rows[0]["property"] == "Category"


# -- caption fallback (no embedded metadata) -----------------------------------
def test_caption_fallback_when_no_datasource_metadata_warns():
    # workbook WITHOUT a <datasources> metadata tree -> binding falls back to caption
    wb = ("<?xml version='1.0' encoding='utf-8' ?>\n<workbook><worksheets>"
          + _worksheet("Bare", "Bar",
                       rows="[federated.abc].[sum:Sales:qk]",
                       cols="[federated.abc].[none:Category:nk]",
                       deps_extra=_INST)
          + "</worksheets></workbook>")
    ir = parse_twb(wb)
    w = ir["worksheets"][0]
    # with no embedded metadata, binding falls back to the datasource id + clean_col(caption)
    assert w["cols"][0]["entity"] == "federated.abc"
    assert w["cols"][0]["property"] == "Category"
    assert any("caption fallback" in x["reason"] for x in ir["warnings"])


# -- PBIR report structure -----------------------------------------------------
def test_emitted_pbir_has_required_report_scaffold():
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    parts = migrate_twb_to_pbir(_workbook(ws), dataset_name="Superstore",
                                report_name="Superstore Report")["parts"]
    assert "definition.pbir" in parts
    pbir = json.loads(parts["definition.pbir"])
    assert pbir["datasetReference"]["byPath"]["path"] == "../Superstore.SemanticModel"
    for required in ("definition/version.json", "definition/report.json",
                     "definition/pages/pages.json", ".platform"):
        assert required in parts
    report = json.loads(parts["definition/report.json"])
    assert {"layoutOptimization", "themeCollection"} <= set(report)
    pages = json.loads(parts["definition/pages/pages.json"])
    assert len(pages["pageOrder"]) == 1
    assert pages["activePageName"] == pages["pageOrder"][0]


def test_dashboard_zone_scales_within_page_bounds_and_one_page_per_dashboard():
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    dash = """
    <dashboard name='Overview'>
      <size maxheight='800' maxwidth='1200' />
      <zones>
        <zone h='100000' w='100000' x='0' y='0'>
          <zone h='90000' w='90000' x='5000' y='5000' name='Sales by Category' id='4' />
        </zone>
      </zones>
    </dashboard>"""
    parts = emit_pbir(parse_twb(_workbook(ws, dash)))
    page_jsons = [k for k in parts if k.endswith("page.json")]
    assert len(page_jsons) == 1
    pos = list(_visual_parts(parts).values())[0]["position"]
    assert 0 <= pos["x"] and pos["x"] + pos["width"] <= PAGE_WIDTH
    assert 0 <= pos["y"] and pos["y"] + pos["height"] <= PAGE_HEIGHT


def test_orphan_worksheet_gets_its_own_page():
    # two worksheets, a dashboard that places only one of them
    ws1 = _worksheet("Placed", "Bar", "[federated.abc].[sum:Sales:qk]",
                     "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    ws2 = _worksheet("Orphan", "Bar", "[federated.abc].[sum:Profit:qk]",
                     "[federated.abc].[none:Region:nk]", deps_extra=_INST)
    dash = ("<dashboard name='D'><zones>"
            "<zone h='1000' w='1000' x='0' y='0'>"
            "<zone h='900' w='900' x='50' y='50' name='Placed' id='2' /></zone>"
            "</zones></dashboard>")
    parts = emit_pbir(parse_twb(_workbook(ws1 + ws2, dash)))
    display_names = {json.loads(v)["displayName"]
                     for k, v in parts.items() if k.endswith("page.json")}
    assert "D" in display_names         # dashboard page
    assert "Orphan" in display_names    # orphan worksheet page
    assert "Placed" not in display_names  # placed worksheet is NOT given its own page


def test_dashboard_device_layouts_do_not_duplicate_worksheet_visuals():
    # A <devicelayouts> section holds phone/tablet re-arrangements of the SAME worksheet zones.
    # Walking every <zone> would emit each worksheet twice (overlapping); only the primary layout
    # is faithful, so device-layout zones must be ignored.
    ws1 = _worksheet("WsA", "Bar", "[federated.abc].[sum:Sales:qk]",
                     "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    ws2 = _worksheet("WsB", "Bar", "[federated.abc].[sum:Profit:qk]",
                     "[federated.abc].[none:Region:nk]", deps_extra=_INST)
    inner = ("<zone h='100000' w='100000' x='0' y='0'>"
             "<zone h='45000' w='90000' x='5000' y='5000' name='WsA' id='2' />"
             "<zone h='45000' w='90000' x='5000' y='55000' name='WsB' id='3' /></zone>")
    dash = ("<dashboard name='D'>"
            "<size maxheight='800' maxwidth='1200' />"
            "<zones>" + inner + "</zones>"
            "<devicelayouts><devicelayout name='Phone'>"
            "<size sizing-mode='vscroll' maxheight='700' maxwidth='350' />"
            "<zones>" + inner + "</zones>"
            "</devicelayout></devicelayouts>"
            "</dashboard>")
    parts = emit_pbir(parse_twb(_workbook(ws1 + ws2, dash)))
    mains = [v for v in _visual_parts(parts).values()
             if v["visual"]["visualType"] != "slicer"]
    assert len(mains) == 2  # one per worksheet, NOT four (no device-layout duplicates)
    refs = sorted(p["queryRef"]
                  for v in mains
                  for st in _query_state(v).values()
                  for p in st["projections"])
    assert refs == ["Orders.Category", "Orders.Region",
                    "Sum(Orders.Profit)", "Sum(Orders.Sales_Amount)"]


def test_dashboard_page_surfaces_worksheet_filter_slicers_deduped():
    # a dashboard page carries the filter slicers of the worksheets it places, deduped across
    # worksheets: two sheets each filtered on Region (+ one also on State) -> the dashboard page
    # gets exactly two distinct slicers (Region once, State once), alongside the two chart visuals.
    f_region = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
                "<groupfilter function='member' level='[none:Region:nk]' /></filter>")
    f_state = ("<filter class='categorical' column='[federated.abc].[none:State:nk]'>"
               "<groupfilter function='member' level='[none:State:nk]' /></filter>")
    ws1 = _worksheet("SalesWs", "Bar", "[federated.abc].[sum:Sales:qk]",
                     "[federated.abc].[none:Category:nk]",
                     deps_extra=_INST, filters=f_region + f_state)
    ws2 = _worksheet("ProfitWs", "Bar", "[federated.abc].[sum:Profit:qk]",
                     "[federated.abc].[none:Region:nk]",
                     deps_extra=_INST, filters=f_region)
    inner = ("<zone h='100000' w='100000' x='0' y='0'>"
             "<zone h='45000' w='90000' x='5000' y='5000' name='SalesWs' id='2' />"
             "<zone h='45000' w='90000' x='5000' y='55000' name='ProfitWs' id='3' /></zone>")
    dash = ("<dashboard name='D'><size maxheight='800' maxwidth='1200' />"
            "<zones>" + inner + "</zones></dashboard>")
    parts = emit_pbir(parse_twb(_workbook(ws1 + ws2, dash)))
    # all visuals land on the single dashboard page
    assert len([k for k in parts if k.endswith("page.json")]) == 1
    slicer_props = sorted(
        v["visual"]["query"]["queryState"]["Values"]["projections"][0]["field"]["Column"]["Property"]
        for v in _visual_parts(parts).values() if v["visual"]["visualType"] == "slicer")
    assert slicer_props == ["Region", "State"]  # deduped: Region once despite two sheets


def test_duplicate_field_queryrefs_are_unique_per_visual():
    # same measure used as a value AND on color encoding
    enc = "<encodings><color column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    ws = _worksheet("Dup", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, encodings=enc)
    parts = emit_pbir(parse_twb(_workbook(ws)))
    state = _query_state(list(_visual_parts(parts).values())[0])
    refs = [p["queryRef"] for role in state.values() for p in role["projections"]]
    assert len(refs) == len(set(refs))  # all queryRefs unique within the visual


def test_dashboard_page_relies_on_default_cross_filter_no_interaction_overrides():
    # Tier-1 default cross-filter: Power BI cross-highlights/cross-filters every visual on a page
    # out of the box. The default is IMPLICIT in PBIR -- a page.json with no `visualInteractions`
    # override (and a report.json with no `defaultFilterActionIsDataFilter` flag) leaves every
    # source->target pair at its default, so the two charts + slicer interact automatically. This
    # locks that we never emit an interaction-disabling config that would silently break it.
    f_region = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
                "<groupfilter function='member' level='[none:Region:nk]' /></filter>")
    ws1 = _worksheet("SalesWs", "Bar", "[federated.abc].[sum:Sales:qk]",
                     "[federated.abc].[none:Category:nk]", deps_extra=_INST, filters=f_region)
    ws2 = _worksheet("ProfitWs", "Bar", "[federated.abc].[sum:Profit:qk]",
                     "[federated.abc].[none:Region:nk]", deps_extra=_INST)
    inner = ("<zone h='100000' w='100000' x='0' y='0'>"
             "<zone h='45000' w='90000' x='5000' y='5000' name='SalesWs' id='2' />"
             "<zone h='45000' w='90000' x='5000' y='55000' name='ProfitWs' id='3' /></zone>")
    dash = ("<dashboard name='D'><size maxheight='800' maxwidth='1200' />"
            "<zones>" + inner + "</zones></dashboard>")
    parts = emit_pbir(parse_twb(_workbook(ws1 + ws2, dash)))
    page_keys = [k for k in parts if k.endswith("page.json")]
    assert len(page_keys) == 1
    page = json.loads(parts[page_keys[0]])
    # no interaction override on the page -> default cross-highlight/cross-filter stays ON
    assert "visualInteractions" not in page
    # at least two main visuals (+ a slicer) coexist, so cross-filtering is meaningful
    vts = [v["visual"]["visualType"] for v in _visual_parts(parts).values()]
    assert len([t for t in vts if t != "slicer"]) >= 2
    assert "slicer" in vts
    # report-wide default is untouched (no forced data-filter flag)
    report = json.loads(parts["definition/report.json"])
    assert "defaultFilterActionIsDataFilter" not in report.get("settings", {})


def test_candidate_records_emitted_per_main_visual_with_fields_position_and_orientation_alt():
    # the image-oracle seam: every main visual gets an additive decision record carrying the
    # ranked Tier-1 candidate types (chosen first), a confidence, the read-only bound-field truth,
    # and the faithful position (incl. z / tabOrder for overlap / z-order analysis).
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]", deps_extra=_INST)
    res = migrate_twb_to_pbir(_workbook(ws))
    recs = res["candidate_records"]
    assert len(recs) == 1
    r = recs[0]
    assert r["worksheet"] == "Sales by Category"
    assert r["visual_type"] == "clusteredColumnChart"
    assert r["candidates"] == ["clusteredColumnChart", "clusteredBarChart"]  # orientation alt
    assert r["confidence"] == "high" and r["hack"] is None
    assert r["fields"]["Category"] == ["Orders.Category"]
    assert r["fields"]["Y"] == ["Sum(Orders.Sales_Amount)"]
    assert {"x", "y", "z", "width", "height", "tabOrder"} <= set(r["position"])


def test_candidate_record_carries_hack_flag_and_alternatives_on_donut():
    # a non-standard composition (dual-axis pie/donut hack) is flagged + offered an alternative
    # type the oracle may switch to, at medium confidence -- the field truth is still read-only.
    res = migrate_twb_to_pbir(_workbook(_donut_worksheet()))
    rec = [r for r in res["candidate_records"] if r["visual_type"] == "donutChart"][0]
    assert rec["candidates"] == ["donutChart", "pieChart"]
    assert rec["confidence"] == "medium"
    assert rec["hack"] == "dual-axis pie/donut"
    assert "Category" in rec["fields"] and "Y" in rec["fields"]


def test_candidate_records_are_additive_and_do_not_alter_pbir_parts():
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    res = migrate_twb_to_pbir(_workbook(ws))
    assert res["candidate_records"]  # present on the return / IR
    # ... but nothing about the record is written into the PBIR definition itself
    blob = "\n".join(res["parts"].values())
    assert "candidate_records" not in blob
    assert not any("candidate" in path.lower() for path in res["parts"])
    assert res["ir"]["candidate_records"] == res["candidate_records"]


def test_parse_accepts_utf8_bom_bytes():
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    raw = ("\ufeff" + _workbook(ws)).encode("utf-8-sig")
    ir = parse_twb(raw)
    assert ir["worksheets"][0]["visual_type"] == "column"


def test_visual_containers_have_required_pbir_fields():
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    parts = emit_pbir(parse_twb(_workbook(ws)))
    for vj in _visual_parts(parts).values():
        assert {"$schema", "name", "position"} <= set(vj)
        assert {"x", "y", "width", "height"} <= set(vj["position"])
        assert "visualType" in vj["visual"]


# -- conservative heuristic: ambiguous / non-bar marks -> unsupported ----------
def test_gantt_mark_is_unsupported():
    ws = _worksheet("Timeline", "Gantt",
                    rows="[federated.abc].[none:Category:nk]",
                    cols="[federated.abc].[sum:Sales:qk]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "unsupported"
    assert any("Gantt" in x["reason"] for x in ir["warnings"])
    assert _visual_parts(emit_pbir(ir)) == {}


def test_bar_with_measures_on_both_axes_and_no_dimension_is_card():
    # measure on rows AND cols, no dimension -> a multi-row card (two big numbers), not a chart
    ws = _worksheet("KPIs", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[sum:Profit:qk]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "card"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "multiRowCard"
    assert len(_query_state(vis)["Values"]["projections"]) == 2


def test_color_dimension_encoding_populates_series_role():
    enc = "<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>"
    ws = _worksheet("Stacked", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, encodings=enc)
    state = _query_state(list(_visual_parts(emit_pbir(parse_twb(_workbook(ws)))).values())[0])
    assert "Series" in state
    assert (state["Series"]["projections"][0]["field"]["Column"]["Property"]) == "Region"
    assert (state["Category"]["projections"][0]["field"]["Column"]["Property"]) == "Category"


def test_color_dimension_on_bar_emits_stacked_not_clustered():
    # Tableau stacks a colour-legend bar/column by default ("Stack marks" on); the rebuild must
    # emit the stacked* variant, not Power BI's side-by-side clustered* chart.
    enc = "<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>"
    # dimension on COLUMNS, measure on rows -> vertical column chart, stacked by the colour legend
    col_ws = _worksheet("Stacked Cols", "Bar",
                        rows="[federated.abc].[sum:Sales:qk]",
                        cols="[federated.abc].[none:Category:nk]",
                        deps_extra=_INST, encodings=enc)
    cv = list(_visual_parts(emit_pbir(parse_twb(_workbook(col_ws)))).values())[0]
    assert cv["visual"]["visualType"] == "stackedColumnChart"
    assert "Series" in _query_state(cv)
    # dimension on ROWS, measure on cols -> horizontal bar chart, stacked
    bar_ws = _worksheet("Stacked Bars", "Bar",
                        rows="[federated.abc].[none:Category:nk]",
                        cols="[federated.abc].[sum:Sales:qk]",
                        deps_extra=_INST, encodings=enc)
    bv = list(_visual_parts(emit_pbir(parse_twb(_workbook(bar_ws)))).values())[0]
    assert bv["visual"]["visualType"] == "stackedBarChart"


def test_bar_without_series_stays_clustered():
    # no colour-legend dimension -> nothing to stack -> the default clustered* chart is kept
    ws = _worksheet("Plain Cols", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    vis = list(_visual_parts(emit_pbir(parse_twb(_workbook(ws)))).values())[0]
    assert vis["visual"]["visualType"] == "clusteredColumnChart"
    assert "Series" not in _query_state(vis)


# -- degenerate visuals are skipped (not emitted as empty shells) --------------
def test_chart_missing_required_role_is_skipped_by_emit_gate():
    # a column visual whose shelves resolved to nothing must not emit an empty shell
    ir = {
        "worksheets": [{
            "name": "Empty", "visual_type": "column", "rows": [], "cols": [],
            "encodings": {"color": None, "size": None, "label": None, "detail": None},
            "filters": [],
        }],
        "dashboards": [], "warnings": [],
    }
    parts = emit_pbir(ir)
    assert _visual_parts(parts) == {}
    assert any("no usable field bindings" in w["reason"] for w in ir["warnings"])


# -- card / KPI (single measure, no dimension) ---------------------------------
def test_single_measure_no_dimension_is_card():
    ws = _worksheet("Total Sales", "Text",
                    rows="[federated.abc].[sum:Sales:qk]", cols="",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "card"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "card"
    proj = _query_state(vis)["Values"]["projections"][0]
    assert proj["field"]["Aggregation"]["Function"] == 0  # Sum
    assert proj["field"]["Aggregation"]["Expression"]["Column"]["Property"] == "Sales_Amount"


def test_measure_on_label_encoding_with_empty_shelves_is_card():
    enc = "<encodings><text column='[federated.abc].[sum:Profit:qk]' /></encodings>"
    ws = _worksheet("Profit KPI", "Text", rows="", cols="",
                    deps_extra=_INST, encodings=enc)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "card"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "card"
    assert _query_state(vis)["Values"]["projections"][0]["field"]["Aggregation"]["Function"] == 0


# -- scatter (two axis measures + a disaggregating dimension) -------------------
def test_circle_mark_two_measures_with_detail_dimension_is_scatter():
    enc = "<encodings><lod column='[federated.abc].[none:Category:nk]' /></encodings>"
    ws = _worksheet("Sales vs Profit", "Circle",
                    rows="[federated.abc].[sum:Profit:qk]",
                    cols="[federated.abc].[sum:Sales:qk]",
                    deps_extra=_INST, encodings=enc)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "scatter"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "scatterChart"
    state = _query_state(vis)
    assert set(state) >= {"X", "Y", "Category"}
    # X = measure on columns (Sales), Y = measure on rows (Profit), Category = detail dim
    assert state["X"]["projections"][0]["field"]["Aggregation"]["Expression"]["Column"]["Property"] == "Sales_Amount"
    assert state["Y"]["projections"][0]["field"]["Aggregation"]["Expression"]["Column"]["Property"] == "Profit"
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "Category"


def test_automatic_mark_two_measures_with_dimension_is_scatter():
    enc = "<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>"
    ws = _worksheet("Auto Scatter", "Automatic",
                    rows="[federated.abc].[sum:Profit:qk]",
                    cols="[federated.abc].[sum:Sales:qk]",
                    deps_extra=_INST, encodings=enc)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "scatter"
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    # the color dimension lands on Series, not Category
    assert state["Series"]["projections"][0]["field"]["Column"]["Property"] == "Region"


def test_scatter_layout_without_dimension_falls_back_to_card():
    # two measures, no disaggregating dimension -> a multi-row card, not a scatter
    ws = _worksheet("No Detail", "Circle",
                    rows="[federated.abc].[sum:Profit:qk]",
                    cols="[federated.abc].[sum:Sales:qk]",
                    deps_extra=_INST)
    assert parse_twb(_workbook(ws))["worksheets"][0]["visual_type"] == "card"


def test_scatter_size_measure_already_on_axis_is_not_double_bound():
    enc = ("<encodings>"
           "<lod column='[federated.abc].[none:Category:nk]' />"
           "<size column='[federated.abc].[sum:Sales:qk]' />"
           "</encodings>")
    ws = _worksheet("Sized Scatter", "Circle",
                    rows="[federated.abc].[sum:Profit:qk]",
                    cols="[federated.abc].[sum:Sales:qk]",
                    deps_extra=_INST, encodings=enc)
    ir = parse_twb(_workbook(ws))
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    assert "Size" not in state  # Sales is already on X, not re-bound to Size
    assert state["X"]["projections"][0]["field"]["Aggregation"]["Expression"]["Column"]["Property"] == "Sales_Amount"


def test_circle_dot_plot_one_dim_one_measure_is_column():
    # A Circle dot/strip plot with one category axis + one measure axis carries the SAME binding
    # as a column chart; only the dot glyph differs (Tier-2 styling, cf. an area mark -> areaChart).
    ws = _worksheet("Dot", "Circle",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "column"
    assert ir["warnings"] == []
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "Category"
    assert (state["Y"]["projections"][0]["field"]["Aggregation"]["Expression"]["Column"]["Property"]
            == "Sales_Amount")


def test_shape_dot_plot_with_colour_is_column_with_series():
    enc = "<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>"
    ws = _worksheet("ShapeDot", "Shape",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, encodings=enc)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "column"
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    # the colour dimension lands on Series; nothing is dropped.
    assert state["Series"]["projections"][0]["field"]["Column"]["Property"] == "Region"


def test_circle_multi_axis_layout_stays_unsupported():
    # Two axis dimensions + a measure (a complex circle crosstab): routing it to a column/bar
    # would silently drop the second axis dimension -> stays unsupported (ambiguous, warn).
    ws = _worksheet("MultiAxis", "Circle",
                    rows="[federated.abc].[none:Category:nk][federated.abc].[none:Region:nk]",
                    cols="[federated.abc].[sum:Sales:qk]",
                    deps_extra=_INST)
    assert parse_twb(_workbook(ws))["worksheets"][0]["visual_type"] == "unsupported"


def test_circle_packed_bubble_without_axes_stays_unsupported():
    # No axis fields (size = measure, colour = dimension): a packed-bubble layout with no faithful
    # Power BI native -> stays unsupported rather than guessing a column.
    enc = ("<encodings>"
           "<size column='[federated.abc].[sum:Sales:qk]' />"
           "<color column='[federated.abc].[none:Region:nk]' />"
           "</encodings>")
    ws = _worksheet("Bubble", "Circle", rows="", cols="",
                    deps_extra=_INST, encodings=enc)
    assert parse_twb(_workbook(ws))["worksheets"][0]["visual_type"] == "unsupported"


# -- pie -----------------------------------------------------------------------
def test_pie_mark_is_pie_chart_with_category_and_value():
    ws = _worksheet("Sales Share", "Pie",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "pie"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "pieChart"
    state = _query_state(vis)
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "Category"
    assert state["Y"]["projections"][0]["field"]["Aggregation"]["Function"] == 0


# -- waterfall (running-total Gantt hack) --------------------------------------
# A running-total quick table calc (token prefix ``cum:``) on a GanttBar value axis renders as a
# floating waterfall. The column-instance carries derivation='Sum' (the engine reads the base
# aggregation); the ``cum:`` running total lives only in the instance NAME -> the gate signal.
_CI_CUM_PROFIT = ("<column-instance column='[Profit]' derivation='Sum' "
                  "name='[cum:sum:Profit:qk]' pivot='key' type='quantitative' />")


def test_running_total_gantt_is_waterfall_chart():
    ws = _worksheet("Cumulative Profit", "GanttBar",
                    rows="[federated.abc].[cum:sum:Profit:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _CI_CUM_PROFIT)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "waterfall"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "waterfallChart"
    state = _query_state(vis)
    # Category = the dimension axis; Y = the BASE measure (Power BI recomputes the running total)
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "Category"
    assert state["Y"]["projections"][0]["field"]["Aggregation"]["Function"] == 0  # Sum(Profit)
    assert "Breakdown" not in state


def test_plain_gantt_without_running_total_is_not_a_waterfall():
    # an ordinary Gantt timeline (no running-total signal) must NOT be reinterpreted as a
    # waterfall -- it stays unsupported (warned), never a wrong visual.
    ws = _worksheet("Timeline", "GanttBar",
                    rows="[federated.abc].[sum:Profit:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["visual_type"] == "unsupported"


# -- donut (dual-axis pie/donut space hack) ------------------------------------
# Faking a donut with a Pie mark stacked behind MIN(0) spacer axes: the real slices live on a
# NON-primary Pie pane's colour (legend) + wedge-size (angle) encodings, which the engine must
# read off that pane rather than the empty spacer pane.
def _donut_worksheet(name="Donut", extra_pane=True):
    enc = ("<encodings>"
           "<color column='[federated.abc].[none:Region:nk]' />"
           "<wedge-size column='[federated.abc].[sum:Sales:qk]' />"
           "</encodings>")
    spacer = "<pane><mark class='Circle' /></pane>" if extra_pane else ""
    pie = f"<pane id='1'><mark class='Pie' />{enc}</pane>"
    return f"""
    <worksheet name='{name}'>
      <table>
        <view>
          <datasources><datasource caption='Superstore' name='federated.abc' /></datasources>
          <datasource-dependencies datasource='federated.abc'>{_DEPS_COLUMNS}{_INST}
          </datasource-dependencies>
        </view>
        <panes>{spacer}{pie}</panes>
        <rows></rows>
        <cols></cols>
      </table>
    </worksheet>"""


def test_dual_axis_pie_donut_hack_is_donut_chart():
    ir = parse_twb(_workbook(_donut_worksheet()))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "donut"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "donutChart"
    state = _query_state(vis)
    # legend (colour) -> Category; angle (wedge-size) -> Y; the spacer axes are dropped
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "Region"
    assert state["Y"]["projections"][0]["field"]["Aggregation"]["Function"] == 0  # Sum(Sales)


def test_single_pane_pie_with_wedge_size_stays_pie_chart():
    # a genuine single-pane Pie (no spacer) is NOT a donut hack -> pieChart; the wedge-size
    # angle measure is still bound to Y.
    ir = parse_twb(_workbook(_donut_worksheet(extra_pane=False)))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "pie"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "pieChart"
    state = _query_state(vis)
    assert state["Y"]["projections"][0]["field"]["Aggregation"]["Function"] == 0


# -- ribbon (bump / manual-rank table-calc hack) -------------------------------
# A bump chart manually ranks members with an INDEX()/RANK() table calc plotted on an axis (here a
# doubled dual-axis spacer), the real ranked measure on a marks-card encoding, and a legend
# dimension. Power BI's native ribbonChart recomputes the rank from the base measure, so the
# table-calc artifact is dropped and Category/Series/Y bind to real model fields.
_RANK_CALC = ("<column caption='index' datatype='integer' name='[Calculation_idx]' "
              "role='measure' type='quantitative'>"
              "<calculation class='tableau' formula='INDEX()' /></column>"
              "<column-instance column='[Calculation_idx]' derivation='None' "
              "name='[usr:Calculation_idx:qk]' pivot='key' type='quantitative' />")

_RIBBON_ENC = ("<encodings>"
               "<color column='[federated.abc].[none:Region:nk]' />"
               "<lod column='[federated.abc].[sum:Sales:qk]' />"
               "</encodings>")


def test_bump_rank_index_hack_is_ribbon_chart():
    ws = _worksheet("Bump Chart", "Automatic",
                    rows="[federated.abc].[usr:Calculation_idx:qk]",
                    cols="[federated.abc].[mn:Order Date:ok]",
                    deps_extra=_INST + _RANK_CALC, encodings=_RIBBON_ENC)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "ribbon"
    parts = emit_pbir(ir)
    vis = list(_visual_parts(parts).values())[0]
    assert vis["visual"]["visualType"] == "ribbonChart"
    state = _query_state(vis)
    # Category = the ordinal axis dim; Series = the legend dim; Y = the BASE measure (rank dropped)
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "Order_Date"
    assert state["Series"]["projections"][0]["field"]["Column"]["Property"] == "Region"
    assert state["Y"]["projections"][0]["field"]["Aggregation"]["Function"] == 0  # Sum(Sales)
    # the INDEX() rank table calc must NOT leak as a binding anywhere in the report
    blob = json.dumps(parts)
    assert "_Measures.index" not in blob
    assert '"index"' not in blob


def test_chart_without_rank_calc_is_not_a_ribbon():
    # the same layout but with a REAL measure (not an INDEX/RANK table calc) on the axis must
    # stay an ordinary chart -- the ribbon gate fires only on the rank-table-calc signal.
    ws = _worksheet("Sales by Year", "Automatic",
                    rows="[federated.abc].[sum:Profit:qk]",
                    cols="[federated.abc].[mn:Order Date:ok]",
                    deps_extra=_INST, encodings=_RIBBON_ENC)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["visual_type"] == "column"


# -- date-table rebinding (consume the model build's date facts) ---------------
# When the datasource-migration build emits a shared marked Date table, a date axis pill on the
# ACTIVE business date rebinds to that calendar (Month -> Date[Month]) so time intelligence runs
# through it instead of degrading to the fact's raw date column. The grain_columns map defaults to
# the standard calendar columns, so the binding need only name the table + the active date. Active
# keys match case/space/underscore-insensitively. Secondary/inactive dates, continuous TRUNCs and
# parts with no calendar column are NEVER silently rebound (warn-never-wrong).
_DATE_BINDING = {"date_table": "Date", "active_keys": ["Order Date"], "key_column": "Date"}


def test_date_part_on_active_date_rebinds_to_date_table():
    ws = _worksheet("Sales Trend", "Line",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[mn:Order Date:ok]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws), date_binding=_DATE_BINDING)
    col = ir["worksheets"][0]["cols"][0]
    assert (col["entity"], col["property"], col["binding"], col["kind"]) == \
        ("Date", "Month", "column", "category")
    # the grain is now applied (rebound to the calendar) -> the "date part approximated" warning is
    # gone, which is the fidelity win
    assert not any("date part" in x["reason"].lower() for x in ir["warnings"])
    # emits a clean Column projection against the Date table
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    cat = _query_state(vis)["Category"]["projections"][0]["field"]["Column"]
    assert cat["Expression"]["SourceRef"]["Entity"] == "Date"
    assert cat["Property"] == "Month"


def test_plain_active_date_rebinds_to_calendar_key():
    inst = ("<column-instance column='[Order Date]' derivation='None' "
            "name='[none:Order Date:ok]' pivot='key' type='ordinal' />")
    ws = _worksheet("Daily Sales", "Line",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Order Date:ok]",
                    deps_extra=_INST + inst)
    col = parse_twb(_workbook(ws), date_binding=_DATE_BINDING)["worksheets"][0]["cols"][0]
    # a plain/continuous exact date rebinds to the marked calendar key column Date[Date]
    assert (col["entity"], col["property"]) == ("Date", "Date")


def test_secondary_date_is_never_rebound():
    # the active business date is Ship Date; an Order Date pill must NOT be bound to the calendar
    # (it would silently show Ship Date's values) -- it stays on the fact column + warns.
    binding = dict(_DATE_BINDING, active_keys=["Ship Date"])
    ws = _worksheet("Sales Trend", "Line",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[mn:Order Date:ok]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws), date_binding=binding)
    col = ir["worksheets"][0]["cols"][0]
    assert (col["entity"], col["property"]) == ("Orders", "Order_Date")
    assert any("date part" in x["reason"].lower() for x in ir["warnings"])


def test_continuous_day_or_coarser_trunc_on_active_date_rebinds_to_calendar_key():
    # A continuous month truncation (green `tmn:` pill) on the ACTIVE business date binds to the
    # marked calendar KEY column Date[Date]: the day-grain Date table relates to the fact date and
    # Power BI's continuous date axis carries the monthly display grain. This matches a Desktop-
    # authored rebuild whose line-chart date axis is Date[Date] (never the fact's raw date column).
    tmonth = ("<column-instance column='[Order Date]' derivation='Month-Trunc' "
              "name='[tmn:Order Date:qk]' pivot='key' type='quantitative' />")
    ws = _worksheet("Monthly Trend", "Line",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[tmn:Order Date:qk]",
                    deps_extra=_INST + tmonth)
    ir = parse_twb(_workbook(ws), date_binding=_DATE_BINDING)
    col = ir["worksheets"][0]["cols"][0]
    assert (col["entity"], col["property"]) == ("Date", "Date")
    # rebound to the calendar -> the "grain not applied" degrade warning is gone
    assert not any("grain not applied" in x["reason"].lower() for x in ir["warnings"])


def test_subday_trunc_on_active_date_is_deferred():
    # An HOUR truncation can't be represented by the day-grain calendar, so it stays on the fact
    # column + warns (warn-never-wrong) -- never silently rebound to a day-grain key that would
    # drop the time component.
    thour = ("<column-instance column='[Order Date]' derivation='Hour-Trunc' "
             "name='[thr:Order Date:qk]' pivot='key' type='quantitative' />")
    ws = _worksheet("Hourly Trend", "Line",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[thr:Order Date:qk]",
                    deps_extra=_INST + thour)
    col = parse_twb(_workbook(ws), date_binding=_DATE_BINDING)["worksheets"][0]["cols"][0]
    assert (col["entity"], col["property"]) == ("Orders", "Order_Date")


def test_no_date_binding_leaves_date_on_fact_column():
    ws = _worksheet("Sales Trend", "Line",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[mn:Order Date:ok]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))  # no date_binding -> the standalone path is unchanged
    col = ir["worksheets"][0]["cols"][0]
    assert (col["entity"], col["property"]) == ("Orders", "Order_Date")
    assert any("date part" in x["reason"].lower() for x in ir["warnings"])


# -- geographic maps (filled + symbol; basics only) ----------------------------
# Latitude/Longitude (generated) on the axes is the realistic spatial signal; the geo-role
# dimension (State, semantic-role='[State].[Name]') sits on the Detail (lod) encoding.
_LATLON = ("rows=\"[federated.abc].[Latitude (generated)]\" "
           "cols=\"[federated.abc].[Longitude (generated)]\"")


def _geo_ws(name, mark, encodings, rows="[federated.abc].[Latitude (generated)]",
            cols="[federated.abc].[Longitude (generated)]"):
    return _worksheet(name, mark, rows=rows, cols=cols,
                      deps_extra=_INST, encodings=encodings)


def test_filled_map_from_geo_detail_and_color_measure():
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Sales:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    ir = parse_twb(_workbook(_geo_ws("Sales by State", "Automatic", enc)))
    assert ir["worksheets"][0]["visual_type"] == "filled_map"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "shapeMap"
    state = _query_state(vis)
    # Category = the geographic dimension; Value = the colour saturation measure
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "State"
    assert state["Value"]["projections"][0]["field"]["Aggregation"]["Function"] == 0
    # generated lat/lon are dropped quietly, not bound as fields
    assert "no model binding" not in json.dumps(ir["warnings"])


def test_filled_map_explicit_map_mark_needs_no_latlon_signal():
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Profit:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    # explicit Map mark is self-signaling: no generated lat/lon on the (empty) axes
    ir = parse_twb(_workbook(_geo_ws("Profit Map", "Map", enc, rows="", cols="")))
    assert ir["worksheets"][0]["visual_type"] == "filled_map"
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "State"


def test_symbol_map_circle_mark_with_size_measure():
    enc = ("<encodings>"
           "<size column='[federated.abc].[sum:Sales:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    ir = parse_twb(_workbook(_geo_ws("Bubble Map", "Circle", enc)))
    assert ir["worksheets"][0]["visual_type"] == "map"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "map"
    state = _query_state(vis)
    assert state["Location"]["projections"][0]["field"]["Column"]["Property"] == "State"
    assert state["Size"]["projections"][0]["field"]["Aggregation"]["Function"] == 0


def test_multipolygon_custom_geometry_is_deferred_with_warning():
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Sales:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "<geometry column='[federated.abc].[Geometry (generated)]' />"
           "</encodings>")
    ir = parse_twb(_workbook(_geo_ws("Spatial", "Multipolygon", enc)))
    assert ir["worksheets"][0]["visual_type"] == "unsupported"
    assert _visual_parts(emit_pbir(ir)) == {}
    assert any("deferred" in w["reason"] and "Spatial" == w["name"] for w in ir["warnings"])


def test_geo_dimension_on_axis_is_not_a_map():
    # State on a column AXIS (not Detail) with a measure -> an ordinary bar/column chart, not
    # a map. This is the anti-hijack guard: a geographic dimension alone must not force a map.
    ws = _worksheet("Sales by State Bars", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:State:nk]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "column"
    assert _visual_parts(emit_pbir(ir))  # a real chart is emitted


def test_geo_detail_without_spatial_signal_does_not_force_map():
    # geo dim on Detail but mark is automatic and there is NO generated lat/lon and no
    # geometry -> not enough signal to call it a map; it must not emit a filledMap.
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Sales:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    ws = _worksheet("Ambiguous Geo", "Automatic", rows="", cols="", deps_extra=_INST,
                    encodings=enc)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] != "filled_map"



def test_aggregate_filter_is_not_emitted_as_a_slicer():
    filt = "<filter class='quantitative' column='[federated.abc].[sum:Sales:qk]' />"
    ws = _worksheet("AggFilter", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, filters=filt)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["filters"] == []
    assert any("aggregate/measure filter" in x["reason"] for x in ir["warnings"])
    slicers = [v for v in _visual_parts(emit_pbir(ir)).values()
               if v["visual"]["visualType"] == "slicer"]
    assert slicers == []


# -- parameter-driven sheet swap (deterministic recognition) -------------------
_PARAMS_DS = """
    <datasource caption='Parameters' name='Parameters'>
      <column caption='view swap' datatype='string' name='[Parameter 1]' role='measure' type='nominal' value='&quot;1&quot;'>
        <members>
          <member value='&quot;1&quot;' alias='line' />
          <member value='&quot;2&quot;' alias='waterfall' />
        </members>
      </column>
    </datasource>"""

# a pure passthrough control calc ([Parameters].[id]) + its column-instance, added to a worksheet's
# datasource-dependencies; a categorical filter pinned to one of its members gates the whole sheet.
_SWAP_CTRL_CALC = """
            <column caption='Ctrl' datatype='string' name='[CalcCtrl]' role='dimension' type='nominal'>
              <calculation class='tableau' formula='[Parameters].[Parameter 1]' />
            </column>
            <column-instance column='[CalcCtrl]' derivation='None' name='[none:CalcCtrl:nk]' pivot='key' type='nominal' />"""


def _workbook_with_params(worksheets, dashboards=""):
    datasources = _DATASOURCE.replace("</datasources>", _PARAMS_DS + "\n  </datasources>")
    return (
        "<?xml version='1.0' encoding='utf-8' ?>\n<workbook>"
        + datasources
        + "<worksheets>" + worksheets + "</worksheets>"
        + ("<dashboards>" + dashboards + "</dashboards>" if dashboards else "")
        + "</workbook>"
    )


def _swap_filter(member):
    return ("<filter class='categorical' column='[federated.abc].[none:CalcCtrl:nk]'>"
            "<groupfilter function='member' member='&quot;" + member + "&quot;' "
            "level='[none:CalcCtrl:nk]' /></filter>")


def test_parameter_sheet_swap_is_grouped_and_not_warned_as_measure_filter():
    ws1 = _worksheet("LineSheet", "Bar",
                     rows="[federated.abc].[sum:Sales:qk]",
                     cols="[federated.abc].[none:Category:nk]",
                     deps_extra=_INST + _SWAP_CTRL_CALC, filters=_swap_filter("1"))
    ws2 = _worksheet("WaterfallSheet", "Bar",
                     rows="[federated.abc].[sum:Profit:qk]",
                     cols="[federated.abc].[none:Category:nk]",
                     deps_extra=_INST + _SWAP_CTRL_CALC, filters=_swap_filter("2"))
    dash = ("<dashboard name='Dash'><zones>"
            "<zone h='100000' w='100000' x='0' y='0'>"
            "<zone h='90000' w='90000' x='5000' y='5000' name='LineSheet' id='2' />"
            "<zone h='90000' w='90000' x='5000' y='5000' name='WaterfallSheet' id='3' />"
            "</zone></zones></dashboard>")
    ir = parse_twb(_workbook_with_params(ws1 + ws2, dash))

    swaps = ir["sheet_swaps"]
    assert len(swaps) == 1
    g = swaps[0]
    assert g["param_caption"] == "view swap"
    assert g["dashboard"] == "Dash"
    by_ws = {a["worksheet"]: a["shown_for"] for a in g["assignments"]}
    assert set(by_ws) == {"LineSheet", "WaterfallSheet"}
    assert by_ws["LineSheet"][0]["value"] == "1" and by_ws["LineSheet"][0]["alias"] == "line"
    assert by_ws["WaterfallSheet"][0]["alias"] == "waterfall"

    # the passthrough control is NOT mis-warned as an unmappable measure filter, and is not
    # emitted as a real data filter / slicer ...
    assert not any("aggregate/measure filter" in x["reason"] for x in ir["warnings"])
    for w in ir["worksheets"]:
        assert w["filters"] == []
    # ... it surfaces ONE precise swap note instead ...
    assert sum("parameter-driven sheet swap" in x["reason"] for x in ir["warnings"]) == 1
    # ... and both underlying worksheets still rebuild as their own (non-slicer) visuals.
    parts = _visual_parts(emit_pbir(ir))
    assert len([v for v in parts.values() if v["visual"]["visualType"] != "slicer"]) >= 2
    assert [v for v in parts.values() if v["visual"]["visualType"] == "slicer"] == []


def test_real_param_comparison_filter_still_warns_and_is_not_a_swap():
    # a calc that genuinely COMPARES against a parameter is not a passthrough control -> it keeps
    # its ordinary (warned) measure-filter handling; the narrow guard must not swallow it.
    calc = ("<column caption='Cmp' datatype='boolean' name='[Cmp]' role='dimension' type='nominal'>"
            "<calculation class='tableau' formula='[Sales] &gt; [Parameters].[Parameter 1]' />"
            "</column>"
            "<column-instance column='[Cmp]' derivation='None' name='[none:Cmp:nk]' "
            "pivot='key' type='nominal' />")
    filt = ("<filter class='categorical' column='[federated.abc].[none:Cmp:nk]'>"
            "<groupfilter function='member' member='true' level='[none:Cmp:nk]' /></filter>")
    ws = _worksheet("Cmp", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + calc, filters=filt)
    ir = parse_twb(_workbook_with_params(ws))
    assert ir["sheet_swaps"] == []
    assert any("aggregate/measure filter" in x["reason"] for x in ir["warnings"])


def test_lone_param_gated_sheet_is_not_a_swap_group():
    ws = _worksheet("Solo", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _SWAP_CTRL_CALC, filters=_swap_filter("1"))
    ir = parse_twb(_workbook_with_params(ws))
    # one gated sheet alone is a visibility toggle, not a swap pair -> no group, no swap note ...
    assert ir["sheet_swaps"] == []
    assert not any("parameter-driven sheet swap" in x["reason"] for x in ir["warnings"])
    # ... but the control is still recognised (not mis-warned) and recorded for a later rebuild.
    assert not any("aggregate/measure filter" in x["reason"] for x in ir["warnings"])
    assert ir["worksheets"][0]["swap_controls"][0]["param_id"] == "Parameter 1"


# -- dashboard parameter controls (hamburger filters): structural capture + honest warning ----
def _paramctrl_zone(pid, x=78833, y=9500):
    return (f"<zone h='9333' w='16000' x='{x}' y='{y}' type-v2='paramctrl' "
            f"param='[Parameters].[{pid}]' id='9' />")


def test_parameter_control_zone_captured_with_caption_and_warned():
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]", deps_extra=_INST)
    dash = ("<dashboard name='Dash'><zones>"
            "<zone h='100000' w='100000' x='0' y='0'>"
            "<zone h='90000' w='60000' x='0' y='0' name='Sales by Category' id='2' />"
            + _paramctrl_zone("Parameter 1") +
            "</zone></zones></dashboard>")
    ir = parse_twb(_workbook_with_params(ws, dash))

    pcs = ir["parameter_controls"]
    assert len(pcs) == 1
    rec = pcs[0]
    assert rec["caption"] == "view swap"          # resolved from the Parameters datasource
    assert rec["param_id"] == "Parameter 1"
    assert rec["datatype"] == "string"
    assert rec["dashboard"] == "Dash"
    assert rec["position"]["x"] == 78833 and rec["position"]["w"] == 16000
    # one honest per-control warning, warn-never-wrong (never silently dropped) ...
    pc_warns = [w for w in ir["warnings"] if "parameter control 'view swap'" in w["reason"]]
    assert len(pc_warns) == 1
    assert pc_warns[0]["scope"] == "dashboard"
    # ... and the control is NOT rebuilt as a slicer yet (no target column identified) while the
    # real worksheet visual still emits, and the paramctrl zone is not mistaken for a worksheet zone.
    parts = _visual_parts(emit_pbir(ir))
    assert [v for v in parts.values() if v["visual"]["visualType"] == "slicer"] == []
    mains = [v for v in parts.values() if v["visual"]["visualType"] != "slicer"]
    assert len(mains) == 1


def test_parameter_control_in_device_layout_not_double_counted():
    # The pilot's paramctrl zones appear once in the primary layout AND again in the phone
    # devicelayout; the control must be captured + warned exactly once (no phone-scale duplicate).
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]", deps_extra=_INST)
    primary = ("<zone h='100000' w='100000' x='0' y='0'>"
               "<zone h='90000' w='60000' x='0' y='0' name='Sales by Category' id='2' />"
               + _paramctrl_zone("Parameter 1") + "</zone>")
    dash = ("<dashboard name='Dash'>"
            "<zones>" + primary + "</zones>"
            "<devicelayouts><devicelayout name='Phone'>"
            "<zones>" + primary + "</zones>"
            "</devicelayout></devicelayouts></dashboard>")
    ir = parse_twb(_workbook_with_params(ws, dash))
    assert len(ir["parameter_controls"]) == 1
    assert sum("parameter control" in w["reason"] for w in ir["warnings"]) == 1


def test_parameter_control_unknown_param_falls_back_to_id():
    # An unresolved parameter id is never dropped: caption falls back to the id, datatype is None.
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]", deps_extra=_INST)
    dash = ("<dashboard name='Dash'><zones>"
            "<zone h='100000' w='100000' x='0' y='0'>"
            "<zone h='90000' w='60000' x='0' y='0' name='Sales by Category' id='2' />"
            + _paramctrl_zone("Parameter 9999 Missing") +
            "</zone></zones></dashboard>")
    ir = parse_twb(_workbook_with_params(ws, dash))
    pcs = ir["parameter_controls"]
    assert len(pcs) == 1
    assert pcs[0]["caption"] == "Parameter 9999 Missing"
    assert pcs[0]["datatype"] is None
    assert any("parameter control 'Parameter 9999 Missing'" in w["reason"]
               for w in ir["warnings"])


# -- multi-datasource: each field binds to its own relation --------------------
def test_multiple_datasources_bind_to_their_own_entities():
    wb = """<?xml version='1.0' encoding='utf-8' ?>
<workbook>
  <datasources>
    <datasource caption='Orders DS' name='ds.orders'>
      <connection class='federated'>
        <relation name='Orders' table='[dbo].[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Category</remote-name><local-name>[Category]</local-name>
            <parent-name>[Orders]</parent-name><local-type>string</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
    </datasource>
    <datasource caption='Returns DS' name='ds.returns'>
      <connection class='federated'>
        <relation name='Returns' table='[dbo].[Returns]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Return Reason</remote-name><local-name>[Category]</local-name>
            <parent-name>[Returns]</parent-name><local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Qty</remote-name><local-name>[Qty]</local-name>
            <parent-name>[Returns]</parent-name><local-type>integer</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Returns View'>
      <table>
        <view>
          <datasources><datasource caption='Returns DS' name='ds.returns' /></datasources>
          <datasource-dependencies datasource='ds.returns'>
            <column caption='Category' datatype='string' name='[Category]' role='dimension' type='nominal' />
            <column caption='Qty' datatype='integer' name='[Qty]' role='measure' type='quantitative' />
            <column-instance column='[Category]' derivation='None' name='[none:Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[Qty]' derivation='Sum' name='[sum:Qty:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
        </view>
        <panes><pane><mark class='Bar' /></pane></panes>
        <rows>[ds.returns].[sum:Qty:qk]</rows>
        <cols>[ds.returns].[none:Category:nk]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>"""
    w = parse_twb(wb)["worksheets"][0]
    cat = w["cols"][0]
    # the SAME local id [Category] resolves to the Returns relation + its remote source column
    assert (cat["entity"], cat["property"]) == ("Returns", "Return_Reason")
    assert (w["rows"][0]["entity"], w["rows"][0]["property"]) == ("Returns", "Qty")


# -- CLI (live-validatable, but tested offline via stdin/stdout, no disk) -------
def test_cli_dry_run_prints_manifest_to_stdout(monkeypatch, capsys):
    import io
    import sys as _sys

    from twb_to_pbir import main

    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    monkeypatch.setattr(_sys, "stdin", io.StringIO(_workbook(ws)))
    rc = main(["-", "--dataset", "Superstore", "--report", "Superstore Report"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert "definition.pbir" in out["parts"]
    assert any(p.endswith("visual.json") for p in out["parts"])
    # dataset name flows through to the dataset reference part
    pbir = json.loads(emit_pbir(parse_twb(_workbook(ws)), dataset_name="Superstore")
                      ["definition.pbir"])
    assert pbir["datasetReference"]["byPath"]["path"] == "../Superstore.SemanticModel"


# -- field-parameter (swap) self-service report --------------------------------
def _fp_specs():
    """Two swap specs (one dimension, one measure) -> a 2-slot self-service table."""
    return [
        {"table_name": "Dim Swap Calc", "display_col": "Dim Swap Calc", "role": "dimension",
         "entries": [
             {"label": "Region", "table": "Orders.csv", "column": "Region",
              "is_measure": False, "order": 0},
             {"label": "Category", "table": "Orders.csv", "column": "Category",
              "is_measure": False, "order": 1}]},
        {"table_name": "Measure Swap", "display_col": "Measure Swap", "role": "measure",
         "entries": [
             {"label": "sales", "table": MEASURES_TABLE, "column": "Total Sales",
              "is_measure": True, "order": 0},
             {"label": "profit", "table": MEASURES_TABLE, "column": "Total Profit",
              "is_measure": True, "order": 1}]},
    ]


def test_field_parameter_table_visual_expands_each_slot():
    specs = _fp_specs()
    vis = field_parameter_table_visual("t", specs, {"x": 0, "y": 0, "width": 100, "height": 100})
    # the swap visual pins the field-parameter schema (the expansion only renders there)
    assert vis["$schema"] == SCHEMA_VISUAL_FP
    well = vis["visual"]["query"]["queryState"]["Values"]
    # one seed projection + one fieldParameters entry per slot, indices sequential, length 1
    assert len(well["projections"]) == len(specs)
    assert [fp["index"] for fp in well["fieldParameters"]] == [0, 1]
    assert all(fp["length"] == 1 for fp in well["fieldParameters"])
    # each fieldParameters entry binds its slot to the parameter's display column
    binds = [(fp["parameterExpr"]["Column"]["Expression"]["SourceRef"]["Entity"],
              fp["parameterExpr"]["Column"]["Property"]) for fp in well["fieldParameters"]]
    assert binds == [("Dim Swap Calc", "Dim Swap Calc"), ("Measure Swap", "Measure Swap")]
    # the dimension seed is a Column ref; the measure seed is a Measure ref
    assert well["projections"][0]["field"]["Column"]["Expression"]["SourceRef"]["Entity"] == "Orders.csv"
    assert well["projections"][1]["field"]["Measure"]["Expression"]["SourceRef"]["Entity"] == MEASURES_TABLE
    # the seed carries the option label (what Desktop writes), queryRef the concrete field
    assert well["projections"][0]["nativeQueryRef"] == "Region"
    assert well["projections"][1]["queryRef"] == f"{MEASURES_TABLE}.Total Sales"


def test_field_parameter_table_visual_skips_specs_with_no_entries():
    specs = _fp_specs() + [{"table_name": "Empty", "display_col": "Empty", "entries": []}]
    well = field_parameter_table_visual("t", specs, {})["visual"]["query"]["queryState"]["Values"]
    assert len(well["projections"]) == 2  # the entry-less spec contributes no slot


def test_field_parameter_slicer_binds_display_column():
    sl = field_parameter_slicer("s", _fp_specs()[0], {"x": 0, "y": 0})
    assert sl["$schema"] == SCHEMA_VISUAL_FP
    assert sl["visual"]["visualType"] == "listSlicer"
    proj = sl["visual"]["query"]["queryState"]["Values"]["projections"][0]
    assert proj["queryRef"] == "Dim Swap Calc.Dim Swap Calc"
    assert proj["nativeQueryRef"] == "Dim Swap Calc"
    assert proj["active"] is True
    assert proj["field"]["Column"]["Expression"]["SourceRef"]["Entity"] == "Dim Swap Calc"


def test_build_field_parameter_page_writes_table_and_one_slicer_per_spec():
    parts = {}
    page = build_field_parameter_page(parts, _fp_specs(), page_name="pageSS",
                                      display_name="Self-Service Table")
    assert page == "pageSS"
    assert "definition/pages/pageSS/page.json" in parts
    visuals = [json.loads(v) for k, v in parts.items() if k.endswith("visual.json")]
    types = sorted(v["visual"]["visualType"] for v in visuals)
    # one tableEx + one listSlicer per spec (2)
    assert types == ["listSlicer", "listSlicer", "tableEx"]
    table = next(v for v in visuals if v["visual"]["visualType"] == "tableEx")
    assert "fieldParameters" in table["visual"]["query"]["queryState"]["Values"]
    # every emitted part is valid JSON and uses the field-parameter page/visual schemas
    page_json = json.loads(parts["definition/pages/pageSS/page.json"])
    assert page_json["$schema"].endswith("page/2.1.0/schema.json")
    assert all(v["$schema"] == SCHEMA_VISUAL_FP for v in visuals)


def test_build_field_parameter_page_no_specs_returns_none():
    parts = {}
    assert build_field_parameter_page(parts, []) is None
    assert build_field_parameter_page(parts, [{"table_name": "X", "display_col": "X",
                                               "entries": []}]) is None
    assert parts == {}


def test_report_json_part_fp_has_base_theme_and_newer_schema():
    rep = report_json_part_fp()
    # baseTheme is still required (NRE-on-open regression), schema is the swap-report version
    assert rep["themeCollection"]["baseTheme"]["name"]
    assert rep["$schema"].endswith("report/3.3.0/schema.json")


# -- Measure Values / Measure Names expansion (M1.0) --------------------------
# Power BI has no "Measure Names" field: dropping N measures in one value well auto-produces
# the series/column headers, and the implicit "Measure Names" pill must never be bound (a bound
# [Measure Names] would be a dangling ref). The ordered member list comes from the worksheet's
# categorical filter on [:Measure Names] (document = shelf order), with the <manual-sort>
# dictionary as a fallback. These fixtures exercise each routed branch + the deferred ones.

def _mv_filter(members):
    """A categorical [:Measure Names] keep-list: a union+manual group whose member entries fix
    the value-well order (matches real .twb structure; ``user:op`` is plain ``op`` here because
    the test root declares no ``user`` namespace -- the namespaced path is covered by the live
    corpus validation)."""
    gfs = "".join(
        f"<groupfilter function='member' level='[:Measure Names]' "
        f"member='[federated.abc].[{m}]' />" for m in members)
    return ("<filter class='categorical' column='[federated.abc].[:Measure Names]'>"
            "<groupfilter function='union' op='manual'>" + gfs + "</groupfilter>"
            "</filter>")


def _mv_manual_sort(members):
    """The fallback ordering source: a <manual-sort> dictionary of quoted member tokens."""
    bks = "".join(f"<bucket>&quot;[federated.abc].[{m}]&quot;</bucket>" for m in members)
    return ("<manual-sort column='[federated.abc].[:Measure Names]'>"
            "<dictionary>" + bks + "</dictionary></manual-sort>")


def _mv_exclude_filter(excluded):
    """An Exclude action on Measure Names: the listed members are the REMOVED set, not the keep
    list (wrapped in except > level-members + a manual union of the excluded measures)."""
    gfs = "".join(
        f"<groupfilter function='member' member='[federated.abc].[{m}]' />" for m in excluded)
    return ("<filter class='categorical' column='[federated.abc].[:Measure Names]'>"
            "<groupfilter function='except'>"
            "<groupfilter function='level-members' level='[:Measure Names]' />"
            "<groupfilter function='union' op='manual'>" + gfs + "</groupfilter>"
            "</groupfilter></filter>")


# a path-hack spacer: a calculated field whose formula is the constant 0
_DUMMY_CALC = ("<column caption='Path' datatype='integer' name='[Calculation_d]' "
               "role='measure' type='quantitative'>"
               "<calculation class='tableau' formula='0' /></column>"
               "<column-instance column='[Calculation_d]' derivation='None' "
               "name='[none:Calculation_d:qk]' pivot='key' type='quantitative' />")

# a parameter-driven swap calc (a field-parameter pattern, deferred to M1.3)
_SWAP_CALC = ("<column caption='Metric Swap' datatype='real' name='[Calculation_s]' "
              "role='measure' type='quantitative'>"
              "<calculation class='tableau' "
              "formula='CASE [Parameters].[Metric] WHEN 1 THEN SUM([Sales]) END' />"
              "</column>"
              "<column-instance column='[Calculation_s]' derivation='None' "
              "name='[none:Calculation_s:qk]' pivot='key' type='quantitative' />")


def test_measure_values_with_names_on_color_binds_all_measures_no_dangling_ref():
    # [Measure Values]={SUM(Sales),SUM(Profit)} + [Measure Names] on Color -> clustered column
    # with both measures in the value well; the implicit Measure Names pill is never bound.
    enc = "<encodings><color column='[federated.abc].[:Measure Names]' /></encodings>"
    ws = _worksheet("MV Series", "Bar",
                    rows="[federated.abc].[Multiple Values]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, encodings=enc,
                    filters=_mv_filter(["sum:Sales:qk", "sum:Profit:qk"]))
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "column"
    assert w["fidelity_note"] and "implicit" in w["fidelity_note"].lower()
    # a faithful rebuild raises no "no model binding" / caption-fallback noise
    assert ir["warnings"] == []

    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    yrefs = [p["queryRef"] for p in state["Y"]["projections"]]
    assert yrefs == ["Sum(Orders.Sales_Amount)", "Sum(Orders.Profit)"]
    assert state["Category"]["projections"][0]["queryRef"] == "Orders.Category"
    blob = json.dumps(state)
    assert "Measure Names" not in blob and "Multiple Values" not in blob


def test_measure_values_path_hack_keeps_line_drops_dummy_defers_bar_reinterpretation():
    # Line mark + Measure Names on Path + a dummy 0 constant member: Tier-1 stays mark-faithful
    # -> drop the constant spacer, bind the one real measure, KEEP the line; line->bar
    # reinterpretation is surfaced in the note and deferred to a styling pass (not silently done).
    enc = "<encodings><path column='[federated.abc].[:Measure Names]' /></encodings>"
    ws = _worksheet("Path Hack", "Line",
                    rows="[federated.abc].[none:Category:nk]",
                    cols="[federated.abc].[Multiple Values]",
                    deps_extra=_INST + _DUMMY_CALC, encodings=enc,
                    filters=_mv_filter(["none:Calculation_d:qk", "sum:Sales:qk"]))
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "line"
    note = w["fidelity_note"].lower()
    assert "path-mark hack" in note and "dummy" in note and "line->bar" in note
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    yrefs = [p["queryRef"] for p in state["Y"]["projections"]]
    assert yrefs == ["Sum(Orders.Sales_Amount)"]
    assert state["Category"]["projections"][0]["queryRef"] == "Orders.Category"
    assert "Calculation_d" not in json.dumps(state)


def test_measure_values_multi_measure_text_table_is_matrix_with_value_columns():
    # measures-as-columns in a crosstab is native in Power BI: a matrix with N value columns.
    enc = "<encodings><text column='[federated.abc].[Multiple Values]' /></encodings>"
    ws = _worksheet("Crosstab", "Text",
                    rows="[federated.abc].[none:Region:nk]",
                    cols="[federated.abc].[:Measure Names]",
                    deps_extra=_INST, encodings=enc,
                    filters=_mv_filter(["sum:Sales:qk", "sum:Profit:qk"]))
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "matrix"
    assert "implicit" in w["fidelity_note"].lower()
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    assert state["Rows"]["projections"][0]["queryRef"] == "Orders.Region"
    vrefs = [p["queryRef"] for p in state["Values"]["projections"]]
    assert vrefs == ["Sum(Orders.Sales_Amount)", "Sum(Orders.Profit)"]
    assert "Measure Names" not in json.dumps(state)


# -- worksheet structural titles (Tier-1: text only, no styling) ---------------
def _only_visual(res):
    vis = list(_visual_parts(res["parts"]).values())
    assert len(vis) == 1
    return vis[0]


def test_static_worksheet_title_emitted_on_visual_container():
    # an authored static caption -> the visual's visualContainerObjects.title.text (single-quoted
    # semantic-query literal), show=true, and the auto field-name subtitle suppressed.
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST,
                    title="<run fontsize='14'>Quarterly Sales</run>")
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    assert res["ir"]["worksheets"][0]["title"] == "Quarterly Sales"
    vco = _only_visual(res)["visual"]["visualContainerObjects"]
    assert vco["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"] == "'Quarterly Sales'"
    assert vco["title"][0]["properties"]["show"]["expr"]["Literal"]["Value"] == "true"
    assert vco["subTitle"][0]["properties"]["show"]["expr"]["Literal"]["Value"] == "false"
    assert res["warnings"] == []


def test_dynamic_worksheet_title_deferred_and_warned():
    # a templated title (an escaped <[field]> token) cannot be a static Power BI title -> defer +
    # warn, never emit the broken literal; no token leaks into the report.
    runs = ("<run>Days to Ship for </run><run>&lt;</run>"
            "<run>[federated.abc].[none:Category:nk]</run><run>&gt;</run>")
    ws = _worksheet("DaystoShip", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, title=runs)
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    assert res["ir"]["worksheets"][0]["title"] is None
    assert "visualContainerObjects" not in _only_visual(res)["visual"]
    assert any("dynamic title" in (w.get("reason") or "") for w in res["warnings"])
    blob = json.dumps(res["parts"])
    assert "Days to Ship for <" not in blob and "&lt;" not in blob


def test_no_title_means_no_visual_container_objects():
    # the common case (no authored title) leaves the visual untitled -> no container objects added.
    ws = _worksheet("Plain", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    res = migrate_twb_to_pbir(_workbook(ws))
    assert res["ir"]["worksheets"][0]["title"] is None
    assert "visualContainerObjects" not in _only_visual(res)["visual"]


def test_multi_run_static_title_is_joined():
    # a title split across styled runs joins to the structural text; per-run styling is dropped.
    ws = _worksheet("Split", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST,
                    title="<run fontsize='15'>Sales </run><run fontsize='9'>by Region</run>")
    res = migrate_twb_to_pbir(_workbook(ws))
    assert res["ir"]["worksheets"][0]["title"] == "Sales by Region"
    val = _only_visual(res)["visual"]["visualContainerObjects"]["title"][0]
    assert val["properties"]["text"]["expr"]["Literal"]["Value"] == "'Sales by Region'"


def test_title_apostrophe_is_doubled_in_literal():
    # semantic-query string literal escaping: an apostrophe doubles so the title text stays valid.
    ws = _worksheet("Quoted", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST,
                    title="<run>O'Brien's Q1</run>")
    res = migrate_twb_to_pbir(_workbook(ws))
    val = _only_visual(res)["visual"]["visualContainerObjects"]["title"][0]
    assert val["properties"]["text"]["expr"]["Literal"]["Value"] == "'O''Brien''s Q1'"


def test_title_dropped_for_unsupported_worksheet():
    # an unsupported layout emits no visual, so its authored title is dropped (nothing to title).
    ws = _worksheet("MultiAxis", "Circle",
                    rows="[federated.abc].[none:Category:nk][federated.abc].[none:Region:nk]",
                    cols="[federated.abc].[sum:Sales:qk]",
                    deps_extra=_INST, title="<run>My Title</run>")
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "unsupported"
    assert w["title"] is None


# -- axis-title captions (structural) ------------------------------------------
def _axis_style(*rules):
    # one <style-rule element='axis'> wrapping the given <format attr='title' .../> elements
    return "<style><style-rule element='axis'>" + "".join(rules) + "</style-rule></style>"


def _axis_objects_of(res):
    return _only_visual(res)["visual"].get("objects") or {}


def test_custom_category_axis_title_emitted_on_objects():
    # a column chart (dim on cols) with an author-set cols-axis title -> visual.objects.categoryAxis
    # titleText (single-quoted literal) + showAxisTitle:true.
    style = _axis_style("<format attr='title' scope='cols' "
                        "field='[federated.abc].[none:Category:nk]' value='Product Category' />")
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, style=style)
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    assert res["ir"]["worksheets"][0]["axis_titles"] == {
        "categoryAxis": {"text": "Product Category", "hide": False}}
    cat = _axis_objects_of(res)["categoryAxis"][0]["properties"]
    assert cat["titleText"]["expr"]["Literal"]["Value"] == "'Product Category'"
    assert cat["showAxisTitle"]["expr"]["Literal"]["Value"] == "true"
    assert "valueAxis" not in _axis_objects_of(res)


def test_custom_value_axis_title_emitted_on_objects():
    # the measure shelf (rows on a column chart) drives the valueAxis title.
    style = _axis_style("<format attr='title' scope='rows' "
                        "field='[federated.abc].[sum:Sales:qk]' value='Total Sales ($)' />")
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, style=style)
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    val = _axis_objects_of(res)["valueAxis"][0]["properties"]
    assert val["titleText"]["expr"]["Literal"]["Value"] == "'Total Sales ($)'"
    assert val["showAxisTitle"]["expr"]["Literal"]["Value"] == "true"


def test_blanked_axis_title_hides_only_the_title():
    # value='' means the author hid the axis title -> showAxisTitle:false, and NO titleText and NO
    # whole-axis show toggle (hiding the title must not hide the whole axis).
    style = _axis_style("<format attr='title' scope='cols' "
                        "field='[federated.abc].[none:Category:nk]' value='' />")
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, style=style)
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    assert res["ir"]["worksheets"][0]["axis_titles"] == {
        "categoryAxis": {"text": None, "hide": True}}
    cat = _axis_objects_of(res)["categoryAxis"][0]["properties"]
    assert cat["showAxisTitle"]["expr"]["Literal"]["Value"] == "false"
    assert "titleText" not in cat
    assert "show" not in cat


def test_bar_orientation_maps_dimension_shelf_to_category_axis():
    # a bar chart puts the dimension on ROWS; a rows-axis title must still resolve to categoryAxis
    # (the mapping is by shelf ROLE, not a fixed rows/cols->axis rule).
    style = _axis_style("<format attr='title' scope='rows' "
                        "field='[federated.abc].[none:Category:nk]' value='Category' />")
    ws = _worksheet("Bars", "Bar",
                    rows="[federated.abc].[none:Category:nk]",
                    cols="[federated.abc].[sum:Sales:qk]",
                    deps_extra=_INST, style=style)
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    assert res["ir"]["worksheets"][0]["visual_type"] == "bar"
    cat = _axis_objects_of(res)["categoryAxis"][0]["properties"]
    assert cat["titleText"]["expr"]["Literal"]["Value"] == "'Category'"


def test_axis_title_apostrophe_is_doubled_in_literal():
    style = _axis_style("<format attr='title' scope='cols' "
                        "field='[federated.abc].[none:Category:nk]' value=\"Q1 '24\" />")
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, style=style)
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    cat = _axis_objects_of(res)["categoryAxis"][0]["properties"]
    assert cat["titleText"]["expr"]["Literal"]["Value"] == "'Q1 ''24'"


def test_no_axis_style_means_no_axis_objects():
    ws = _worksheet("Plain", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    res = migrate_twb_to_pbir(_workbook(ws))
    assert res["ir"]["worksheets"][0]["axis_titles"] == {}
    assert "objects" not in _only_visual(res)["visual"]


def test_quick_filter_title_rule_is_not_an_axis_title():
    # a quick-filter caption rule (element='quick-filter', no scope) must NOT leak into axis objects.
    style = ("<style><style-rule element='quick-filter'>"
             "<format attr='title' field='[federated.abc].[none:Category:nk]' value='Pick one' />"
             "</style-rule></style>")
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, style=style)
    res = migrate_twb_to_pbir(_workbook(ws))
    assert res["ir"]["worksheets"][0]["axis_titles"] == {}
    assert "objects" not in _only_visual(res)["visual"]


def test_non_cartesian_visual_ignores_axis_titles():
    # a matrix has no category-vs-value axis pair, so an axis style-rule is not reproduced.
    enc = "<encodings><text column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    style = _axis_style("<format attr='title' scope='cols' "
                        "field='[federated.abc].[none:Region:nk]' value='Region' />")
    ws = _worksheet("Cross", "Text",
                    rows="[federated.abc].[none:Category:nk]",
                    cols="[federated.abc].[none:Region:nk]",
                    deps_extra=_INST, encodings=enc, style=style)
    res = migrate_twb_to_pbir(_workbook(ws))
    assert res["ir"]["worksheets"][0]["visual_type"] == "matrix"
    assert res["ir"]["worksheets"][0]["axis_titles"] == {}
    assert "objects" not in _only_visual(res)["visual"]


def _ref_line(value_column, formula="average", label="", label_type="none"):
    lbl = f"label='{label}' " if label else ""
    return (f"<reference-line {lbl}label-type='{label_type}' formula='{formula}' "
            f"value-column='{value_column}' scope='per-cell' />")


def test_reference_line_on_card_warns_kpi_target():
    # a single-value card carrying a reference line is a KPI goal/target; Power BI's plain card
    # cannot draw the target, so we keep the faithful value and disclose the deferred overlay.
    ref = _ref_line("[federated.abc].[sum:Profit:qk]", formula="max",
                    label="Goal &lt;Value&gt;", label_type="custom")
    ws = _worksheet("Profit vs Goal", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]", cols="",
                    deps_extra=_INST, pane_extra=ref)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "card"
    assert w["reference_lines"] == [{"kind": "reference_line", "label": "Goal", "formula": "max"}]
    kpi = [x for x in ir["warnings"]
           if x["name"] == "Profit vs Goal" and "KPI target/goal" in x["reason"]]
    assert len(kpi) == 1 and "Goal" in kpi[0]["reason"]
    # the card itself still emits faithfully
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "card"


def test_reference_line_on_chart_warns_generic_not_kpi():
    ref = _ref_line("[federated.abc].[sum:Profit:qk]", formula="average")
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, pane_extra=ref)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "column"
    assert w["reference_lines"] == [
        {"kind": "reference_line", "label": "average of Profit", "formula": "average"}]
    rl = [x for x in ir["warnings"] if x["name"] == "Sales by Category"
          and "reference/target/trend line" in x["reason"]]
    assert len(rl) == 1 and "average of Profit" in rl[0]["reason"]
    assert "KPI target/goal" not in rl[0]["reason"]


def test_reference_line_on_unsupported_worksheet_is_not_warned():
    # an unsupported worksheet is already wholly deferred; its reference line adds no extra noise.
    enc = ("<encodings><color column='[federated.abc].[sum:Sales:qk]' />"
           "<lod column='[federated.abc].[none:Category:nk]' /></encodings>")
    ref = _ref_line("[federated.abc].[sum:Sales:qk]")
    ws = _worksheet("Weird", "Square", rows="", cols="",
                    deps_extra=_INST, encodings=enc, pane_extra=ref)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "unsupported"
    assert w["reference_lines"] == []
    assert not any("deferred (Tier-2 analytics)" in x["reason"] for x in ir["warnings"])


def test_trend_line_is_deferred_with_warning():
    trend = "<trend-line model-type='linear' />"
    ws = _worksheet("Trend", "Line",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[mn:Order Date:ok]",
                    deps_extra=_INST, pane_extra=trend)
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert {"kind": "trend_line", "label": "trend line", "formula": None} in w["reference_lines"]
    assert any("trend line" in x["reason"] and "deferred (Tier-2 analytics)" in x["reason"]
               for x in ir["warnings"] if x["name"] == "Trend")


def test_no_reference_line_means_empty_list_and_no_warning():
    ws = _worksheet("Plain", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["reference_lines"] == []
    assert not any("deferred (Tier-2 analytics)" in x["reason"] for x in ir["warnings"])


def test_measure_values_no_dimension_is_multi_row_card():
    ws = _worksheet("KPIs", "Text",
                    rows="[federated.abc].[Multiple Values]",
                    cols="",
                    deps_extra=_INST,
                    filters=_mv_filter(["sum:Sales:qk", "sum:Profit:qk"]))
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "card"
    assert w["fidelity_note"] and "implicit" in w["fidelity_note"].lower()
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "multiRowCard"
    vrefs = [p["queryRef"] for p in _query_state(vis)["Values"]["projections"]]
    assert vrefs == ["Sum(Orders.Sales_Amount)", "Sum(Orders.Profit)"]


def test_measure_values_parameter_swap_members_are_deferred_with_warning():
    ws = _worksheet("Param Swap", "Bar",
                    rows="[federated.abc].[Multiple Values]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _SWAP_CALC,
                    filters=_mv_filter(["none:Calculation_s:qk"]))
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "unsupported"
    assert w["fidelity_note"] is None
    assert any(x["scope"] == "worksheet" and "parameter-driven" in x["reason"]
               for x in ir["warnings"])
    assert _visual_parts(emit_pbir(ir)) == {}


def test_measure_values_names_on_rows_with_chart_mark_defers_to_small_multiples():
    # Measure Names on rows against a real chart mark = one pane per measure (trellis) = M1.2.
    ws = _worksheet("Trellis", "Bar",
                    rows="[federated.abc].[:Measure Names]",
                    cols="[federated.abc].[Multiple Values]",
                    deps_extra=_INST,
                    filters=_mv_filter(["sum:Sales:qk", "sum:Profit:qk"]))
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "unsupported"
    assert w["fidelity_note"] is None
    assert any("small multiples" in x["reason"] for x in ir["warnings"])


def test_measure_values_member_order_follows_filter_document_order():
    # the filter lists Profit before Sales -> the value well must honour that order
    enc = "<encodings><text column='[federated.abc].[Multiple Values]' /></encodings>"
    ws = _worksheet("Ordered", "Text",
                    rows="[federated.abc].[none:Region:nk]",
                    cols="[federated.abc].[:Measure Names]",
                    deps_extra=_INST, encodings=enc,
                    filters=_mv_filter(["sum:Profit:qk", "sum:Sales:qk"]))
    ir = parse_twb(_workbook(ws))
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    vrefs = [p["queryRef"] for p in state["Values"]["projections"]]
    assert vrefs == ["Sum(Orders.Profit)", "Sum(Orders.Sales_Amount)"]


def test_measure_values_falls_back_to_manual_sort_when_no_filter():
    enc = "<encodings><text column='[federated.abc].[Multiple Values]' /></encodings>"
    ws = _worksheet("Fallback", "Text",
                    rows="[federated.abc].[none:Region:nk]",
                    cols="[federated.abc].[:Measure Names]",
                    deps_extra=_INST, encodings=enc,
                    filters=_mv_manual_sort(["sum:Profit:qk", "sum:Sales:qk"]))
    ir = parse_twb(_workbook(ws))
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    vrefs = [p["queryRef"] for p in state["Values"]["projections"]]
    assert vrefs == ["Sum(Orders.Profit)", "Sum(Orders.Sales_Amount)"]


def test_measure_values_exclude_filter_defers_instead_of_showing_wrong_measures():
    # an Exclude filter lists the REMOVED measure; reading it as a keep-list would bind exactly
    # the wrong set, so the worksheet must warn + defer rather than guess the displayed measures.
    enc = "<encodings><text column='[federated.abc].[Multiple Values]' /></encodings>"
    ws = _worksheet("Excluded", "Text",
                    rows="[federated.abc].[none:Region:nk]",
                    cols="[federated.abc].[:Measure Names]",
                    deps_extra=_INST, encodings=enc,
                    filters=_mv_exclude_filter(["sum:Profit:qk"]))
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "unsupported"
    assert w["fidelity_note"] is None
    assert any(x["scope"] == "worksheet" and "exclude" in x["reason"].lower()
               for x in ir["warnings"])
    assert _visual_parts(emit_pbir(ir)) == {}


# -- M1.1 golden regression: lock the full (visualType, role -> queryRefs) contract -----------
def _main_visuals_by_worksheet(parts):
    """Map each orphan worksheet's display name to its single non-slicer ('main') visual."""
    display_by_folder = {}
    for path, raw in parts.items():
        if path.endswith("page.json"):
            display_by_folder[path.split("/")[-2]] = json.loads(raw)["displayName"]
    out = {}
    for path, raw in parts.items():
        if not path.endswith("visual.json"):
            continue
        vj = json.loads(raw)
        if vj["visual"]["visualType"] == "slicer":
            continue
        out[display_by_folder[path.split("/")[-4]]] = vj
    return out


def test_golden_visual_types_lock_full_bindings():
    """Golden regression: one workbook, one worksheet per supported Tier-1 visual type (plus the
    Measure Values expansion), emitted end-to-end. Locks the (PBIR ``visualType``, role -> exact
    model ``queryRef``) contract so any drift in ``_resolve_shelf`` / ``_resolve_field`` / routing /
    the emitter rebaselines visibly. A Measure Values case is included so the M1.0 expansion is part
    of the locked baseline (every member exact-bound; the implicit Measure Names pill never bound).
    """
    geo, geo2 = "[federated.abc].[Latitude (generated)]", "[federated.abc].[Longitude (generated)]"
    text_sales = "<encodings><text column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    mv_text = "<encodings><text column='[federated.abc].[Multiple Values]' /></encodings>"
    fmap_enc = ("<encodings><color column='[federated.abc].[sum:Sales:qk]' />"
                "<lod column='[federated.abc].[none:State:nk]' /></encodings>")
    smap_enc = ("<encodings><size column='[federated.abc].[sum:Sales:qk]' />"
                "<lod column='[federated.abc].[none:State:nk]' /></encodings>")
    scatter_enc = "<encodings><lod column='[federated.abc].[none:Category:nk]' /></encodings>"

    # name -> (mark, rows, cols, encodings, filters)
    specs = {
        "Golden Column": ("Bar", "[federated.abc].[sum:Sales:qk]",
                          "[federated.abc].[none:Category:nk]", "", ""),
        "Golden Bar": ("Bar", "[federated.abc].[none:Region:nk]",
                       "[federated.abc].[sum:Profit:qk]", "", ""),
        "Golden Line": ("Line", "[federated.abc].[sum:Sales:qk]",
                        "[federated.abc].[mn:Order Date:ok]", "", ""),
        "Golden Area": ("Area", "[federated.abc].[sum:Sales:qk]",
                        "[federated.abc].[mn:Order Date:ok]", "", ""),
        "Golden Table": ("Text", "[federated.abc].[none:Category:nk]",
                         "[federated.abc].[sum:Sales:qk]", "", ""),
        "Golden Matrix": ("Text", "[federated.abc].[none:Category:nk]",
                          "[federated.abc].[none:Region:nk]", text_sales, ""),
        "Golden Scatter": ("Circle", "[federated.abc].[sum:Profit:qk]",
                           "[federated.abc].[sum:Sales:qk]", scatter_enc, ""),
        "Golden Pie": ("Pie", "[federated.abc].[sum:Sales:qk]",
                       "[federated.abc].[none:Category:nk]", "", ""),
        "Golden Card": ("Text", "[federated.abc].[sum:Sales:qk]", "", "", ""),
        "Golden MultiCard": ("Bar", "[federated.abc].[sum:Sales:qk]",
                             "[federated.abc].[sum:Profit:qk]", "", ""),
        "Golden FilledMap": ("Automatic", geo, geo2, fmap_enc, ""),
        "Golden SymbolMap": ("Circle", geo, geo2, smap_enc, ""),
        "Golden MeasureValues": ("Text", "[federated.abc].[none:Region:nk]",
                                 "[federated.abc].[:Measure Names]", mv_text,
                                 _mv_filter(["sum:Sales:qk", "sum:Profit:qk"])),
    }
    expect = {
        "Golden Column": ("clusteredColumnChart",
                          {"Category": ["Orders.Category"], "Y": ["Sum(Orders.Sales_Amount)"]}),
        "Golden Bar": ("clusteredBarChart",
                       {"Category": ["Orders.Region"], "Y": ["Sum(Orders.Profit)"]}),
        "Golden Line": ("lineChart",
                        {"Category": ["Orders.Order_Date"], "Y": ["Sum(Orders.Sales_Amount)"]}),
        "Golden Area": ("areaChart",
                        {"Category": ["Orders.Order_Date"], "Y": ["Sum(Orders.Sales_Amount)"]}),
        "Golden Table": ("tableEx",
                         {"Values": ["Orders.Category", "Sum(Orders.Sales_Amount)"]}),
        "Golden Matrix": ("pivotTable",
                          {"Rows": ["Orders.Category"], "Columns": ["Orders.Region"],
                           "Values": ["Sum(Orders.Sales_Amount)"]}),
        "Golden Scatter": ("scatterChart",
                           {"X": ["Sum(Orders.Sales_Amount)"], "Y": ["Sum(Orders.Profit)"],
                            "Category": ["Orders.Category"]}),
        "Golden Pie": ("pieChart",
                       {"Category": ["Orders.Category"], "Y": ["Sum(Orders.Sales_Amount)"]}),
        "Golden Card": ("card", {"Values": ["Sum(Orders.Sales_Amount)"]}),
        "Golden MultiCard": ("multiRowCard",
                             {"Values": ["Sum(Orders.Sales_Amount)", "Sum(Orders.Profit)"]}),
        "Golden FilledMap": ("shapeMap",
                             {"Category": ["Orders.State"], "Value": ["Sum(Orders.Sales_Amount)"]}),
        "Golden SymbolMap": ("map",
                             {"Location": ["Orders.State"], "Size": ["Sum(Orders.Sales_Amount)"]}),
        "Golden MeasureValues": ("pivotTable",
                                 {"Rows": ["Orders.Region"],
                                  "Values": ["Sum(Orders.Sales_Amount)", "Sum(Orders.Profit)"]}),
    }

    ws_xml = "".join(
        _worksheet(name, mark, rows, cols, deps_extra=_INST, encodings=enc, filters=filt)
        for name, (mark, rows, cols, enc, filt) in specs.items())
    result = migrate_twb_to_pbir(_workbook(ws_xml), dataset_name="Superstore")
    visuals = _main_visuals_by_worksheet(result["parts"])

    assert set(visuals) == set(expect)  # every type emitted; none dropped, none duplicated
    for name, (vtype, roles) in expect.items():
        vj = visuals[name]
        assert vj["visual"]["visualType"] == vtype, name
        state = _query_state(vj)
        assert set(state) == set(roles), (name, sorted(state), sorted(roles))
        for role, refs in roles.items():
            got = [p["queryRef"] for p in state[role]["projections"]]
            assert got == refs, (name, role, got)
    # the implicit Measure Names pseudo-field must never appear anywhere in the emitted report
    assert "Measure Names" not in json.dumps(result["parts"])


# -- Property invariants: structural robustness over a wide synthetic sweep ----
# The committable analogue of an equivalence/regression harness: emit a broad matrix of worksheet
# shapes (every supported chart type plus deliberately degenerate/unsupported ones) and assert the
# engine's standing guarantees hold for EVERY one -- never crash, never silently drop a worksheet
# (routed-or-warned), never emit a dangling field/sort reference, never leak a Measure Names/Values
# pseudo-field, and always produce well-formed semantic-query field expressions. This locks the
# warn-never-wrong contract structurally, so a future routing/emitter change cannot regress it
# unnoticed (rather than checking one shape at a time).
_PSEUDO_TOKENS = ("[Measure Names]", "[Measure Values]", "Multiple Values",
                  ":Measure Names", "Measure Names", "Measure Values")


def _field_entity_property(field):
    """Return (Entity, Property) for any semantic-query field expression, else (None, None)."""
    if "Column" in field:
        c = field["Column"]
        return c["Expression"]["SourceRef"]["Entity"], c["Property"]
    if "Measure" in field:
        mm = field["Measure"]
        return mm["Expression"]["SourceRef"]["Entity"], mm["Property"]
    if "Aggregation" in field:
        col = field["Aggregation"]["Expression"]["Column"]
        return col["Expression"]["SourceRef"]["Entity"], col["Property"]
    return None, None


def test_property_invariants_hold_across_a_wide_worksheet_sweep():
    geo, geo2 = "[federated.abc].[Latitude (generated)]", "[federated.abc].[Longitude (generated)]"
    text_sales = "<encodings><text column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    color_sales = "<encodings><color column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    color_region = "<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>"
    lod_cat = "<encodings><lod column='[federated.abc].[none:Category:nk]' /></encodings>"
    fmap = ("<encodings><color column='[federated.abc].[sum:Sales:qk]' />"
            "<lod column='[federated.abc].[none:State:nk]' /></encodings>")
    smap = ("<encodings><size column='[federated.abc].[sum:Sales:qk]' />"
            "<lod column='[federated.abc].[none:State:nk]' /></encodings>")
    packed = ("<encodings><color column='[federated.abc].[sum:Sales:qk]' />"
              "<lod column='[federated.abc].[none:Category:nk]' /></encodings>")
    mv_text = "<encodings><text column='[federated.abc].[Multiple Values]' /></encodings>"
    sortd = ("<computed-sort column='[federated.abc].[none:Category:nk]' direction='DESC' "
             "using='[federated.abc].[sum:Sales:qk]' />")
    s, c, r = "[federated.abc].[sum:Sales:qk]", "[federated.abc].[none:Category:nk]", \
        "[federated.abc].[none:Region:nk]"
    p, d = "[federated.abc].[sum:Profit:qk]", "[federated.abc].[mn:Order Date:ok]"

    # name -> (mark, rows, cols, encodings, filters): a deliberately broad/degenerate mix
    specs = {
        "P Column": ("Bar", s, c, "", ""),
        "P Column Sorted": ("Bar", s, c, "", sortd),
        "P Column Stacked": ("Bar", s, c, color_region, ""),
        "P Bar": ("Bar", r, p, "", ""),
        "P Line": ("Line", s, d, "", ""),
        "P Area": ("Area", s, d, "", ""),
        "P Table": ("Text", c, s, "", ""),
        "P Matrix": ("Text", c, r, text_sales, ""),
        "P Highlight": ("Square", c, r, color_sales, ""),
        "P Packed Unsupported": ("Square", "", "", packed, ""),
        "P Pie": ("Pie", s, c, "", ""),
        "P Scatter": ("Circle", p, s, lod_cat, ""),
        "P Card": ("Text", s, "", "", ""),
        "P MultiCard": ("Bar", s, p, "", ""),
        "P FilledMap": ("Automatic", geo, geo2, fmap, ""),
        "P SymbolMap": ("Circle", geo, geo2, smap, ""),
        "P Gantt Unsupported": ("Gantt", s, c, "", ""),
        "P Empty Unsupported": ("Bar", "", "", "", ""),
        "P MeasureValues": ("Text", r, "[federated.abc].[:Measure Names]", mv_text,
                            _mv_filter(["sum:Sales:qk", "sum:Profit:qk"])),
    }
    ws_xml = "".join(
        _worksheet(name, mark, rows, cols, deps_extra=_INST, encodings=enc, filters=filt)
        for name, (mark, rows, cols, enc, filt) in specs.items())

    ir = parse_twb(_workbook(ws_xml))
    parts = emit_pbir(ir)  # invariant: never raises across the whole sweep
    warned = {w["name"] for w in ir.get("warnings", [])}
    main = _main_visuals_by_worksheet(parts)

    # (1) routed-or-warned: no worksheet is ever silently dropped
    for name in specs:
        assert name in main or name in warned, f"silently dropped: {name}"

    # (2) no Measure Names / Measure Values pseudo-field literal survives into the emitted report
    blob = json.dumps(parts)
    for tok in _PSEUDO_TOKENS:
        assert tok not in blob, f"pseudo-field leaked: {tok}"

    # (3) per emitted visual: well-formed field expressions, unique queryRefs, and a sort (if any)
    #     that references only a field already bound in the same visual (no dangling sort)
    for name, vj in main.items():
        query = vj["visual"].get("query")
        if not query:
            continue
        state = query["queryState"]
        refs = []
        for role, payload in state.items():
            for proj in payload.get("projections", []):
                entity, prop = _field_entity_property(proj["field"])
                assert entity and prop, f"malformed field in {name}/{role}"
                if "Aggregation" in proj["field"]:
                    assert isinstance(proj["field"]["Aggregation"]["Function"], int)
                refs.append(proj["queryRef"])
        assert len(refs) == len(set(refs)), f"duplicate queryRef in {name}"
        sd = query.get("sortDefinition")
        if sd:
            bound = [proj["field"] for payload in state.values()
                     for proj in payload.get("projections", [])]
            for entry in sd["sort"]:
                assert entry["field"] in bound, f"dangling sort in {name}"
                assert entry["direction"] in ("Ascending", "Descending")


# -- table / matrix background colour scale (conditional formatting) -----------
# A continuous colour scale on a highlight table / matrix becomes a PBIR ``visual.objects.values``
# ``backColor`` FillRule gradient. WARN-NEVER-WRONG: the fill emits only when the colour driver is
# a clean model measure projected in the visual and NOT a quick table calc; otherwise the visual
# emits with no fill, a warning, and the raw palette preserved on the candidate record.
def _mark_color_style(field_token, palette_type, colors, center=None, enc_type="interpolated"):
    center_attr = f" center='{center}'" if center is not None else ""
    color_xml = "".join(f"<color>{c}</color>" for c in colors)
    return (f"<style><style-rule element='mark'>"
            f"<encoding attr='color'{center_attr} type='{enc_type}' field='{field_token}'>"
            f"<color-palette type='{palette_type}'>{color_xml}</color-palette>"
            f"</encoding></style-rule></style>")


def _heat_ws(name, *, color_field, encodings, style, deps_extra=_INST):
    # Square mark + dims on both axes -> a highlight-table matrix; the colour scale rides the
    # worksheet <style>. ``encodings`` carries the marks-card colour/text pills.
    return _worksheet(name, "Square",
                      rows="[federated.abc].[none:Category:nk]",
                      cols="[federated.abc].[none:Region:nk]",
                      deps_extra=deps_extra, encodings=encodings, style=style)


def _values_objects(visual_json):
    return visual_json["visual"].get("objects", {}).get("values")


def _fill_rule(values_objects):
    return (values_objects[0]["properties"]["backColor"]["solid"]["color"]
            ["expr"]["FillRule"])


def _cf_fact(records, worksheet):
    rec = next(r for r in records if r["worksheet"] == worksheet)
    return rec.get("conditional_format")


def test_color_gradient_palette_parsed_into_ir():
    # The mark colour encoding's interpolated palette is parsed (additive ``color_gradient`` IR
    # key) preserving the centre, the author colour order, and the table-calc flag.
    style = _mark_color_style("[federated.abc].[sum:Sales:qk]", "ordered-diverging",
                              ["#f28e2b", "#d9d9d9", "#e6e6e6"], center="0.0",
                              enc_type="custom-interpolated")
    enc = "<encodings><color column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    ir = parse_twb(_workbook(_heat_ws("Heat", color_field="sum:Sales:qk",
                                      encodings=enc, style=style)))
    cg = ir["worksheets"][0]["color_gradient"]
    assert cg is not None
    assert cg["palette_type"] == "ordered-diverging"
    assert cg["center"] == 0.0
    assert cg["colors"] == ["#f28e2b", "#d9d9d9", "#e6e6e6"]   # first -> min, last -> max
    assert cg["is_table_calc"] is False


def test_highlight_table_sequential_scale_emits_backcolor_lineargradient2():
    style = _mark_color_style("[federated.abc].[sum:Sales:qk]", "ordered-sequential",
                              ["#f7fbff", "#08306b"])
    enc = "<encodings><color column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    parts = emit_pbir(parse_twb(_workbook(
        _heat_ws("Heat", color_field="sum:Sales:qk", encodings=enc, style=style))))
    vj = list(_visual_parts(parts).values())[0]
    vo = _values_objects(vj)
    assert vo, "expected a conditional-format values object"
    fr = _fill_rule(vo)
    # Input mirrors the colour-driver projection (SUM of Sales); gradient is a 2-stop linear scale
    assert fr["Input"]["Aggregation"]["Expression"]["Column"]["Property"] == "Sales_Amount"
    grad = fr["FillRule"]["linearGradient2"]
    assert grad["min"]["color"]["Literal"]["Value"] == "'#f7fbff'"
    assert grad["max"]["color"]["Literal"]["Value"] == "'#08306b'"
    assert grad["nullColoringStrategy"]["strategy"]["Literal"]["Value"] == "'asZero'"
    # the selector targets a real Values projection by queryRef (self-colour)
    qs = _query_state(vj)
    metadata = vo[0]["selector"]["metadata"]
    assert metadata in {p["queryRef"] for p in qs["Values"]["projections"]}
    assert vo[0]["selector"]["data"][0]["dataViewWildcard"]["matchingOption"] == 1


def test_diverging_scale_with_center_emits_lineargradient3_mid_pinned():
    style = _mark_color_style("[federated.abc].[sum:Profit:qk]", "ordered-diverging",
                              ["#f28e2b", "#d9d9d9", "#e6e6e6"], center="0.0",
                              enc_type="custom-interpolated")
    enc = "<encodings><color column='[federated.abc].[sum:Profit:qk]' /></encodings>"
    parts = emit_pbir(parse_twb(_workbook(
        _heat_ws("Heat", color_field="sum:Profit:qk", encodings=enc, style=style))))
    fr = _fill_rule(_values_objects(list(_visual_parts(parts).values())[0]))
    grad = fr["FillRule"]["linearGradient3"]
    assert grad["min"]["color"]["Literal"]["Value"] == "'#f28e2b'"
    assert grad["mid"]["color"]["Literal"]["Value"] == "'#d9d9d9'"
    assert grad["mid"]["value"]["Literal"]["Value"] == "0.0D"    # centre pinned as a double literal
    assert grad["max"]["color"]["Literal"]["Value"] == "'#e6e6e6'"
    assert "value" not in grad["min"] and "value" not in grad["max"]  # auto min/max


def test_color_by_different_measure_targets_displayed_value():
    # Tableau "colour by a different field": text shows SUM(Sales), colour driven by SUM(Profit).
    # The FillRule Input is Profit; the selector targets the displayed Sales column. The colour
    # driver is surfaced on the matrix TOOLTIPS (faithful to Tableau's colour-card tooltip), not as
    # a visible Values column -- so Sales is the only displayed value and Profit rides the tooltip.
    style = _mark_color_style("[federated.abc].[sum:Profit:qk]", "ordered-sequential",
                              ["#ffffff", "#1f77b4"])
    enc = ("<encodings><color column='[federated.abc].[sum:Profit:qk]' />"
           "<text column='[federated.abc].[sum:Sales:qk]' /></encodings>")
    parts = emit_pbir(parse_twb(_workbook(
        _heat_ws("Heat", color_field="sum:Profit:qk", encodings=enc, style=style))))
    vj = list(_visual_parts(parts).values())[0]
    vo = _values_objects(vj)
    fr = _fill_rule(vo)
    assert fr["Input"]["Aggregation"]["Expression"]["Column"]["Property"] == "Profit"
    assert vo[0]["selector"]["metadata"] == "Sum(Orders.Sales_Amount)"
    qs = _query_state(vj)
    val_refs = {p["queryRef"] for p in qs["Values"]["projections"]}
    tip_refs = {p["queryRef"] for p in qs["Tooltips"]["projections"]}
    assert val_refs == {"Sum(Orders.Sales_Amount)"}       # only the displayed value is a column
    assert tip_refs == {"Sum(Orders.Profit)"}             # the colour driver rides the tooltip


def test_table_calc_colour_driver_defers_with_palette_preserved():
    # A quick-table-calc colour driver (e.g. "Percent Difference From" -> pcdf:) has no equivalent
    # model measure yet, so colouring by the mis-resolved base would be wrong: defer + warn + keep
    # the raw palette on the candidate record (no fill emitted).
    calc_col = ("<column caption='DoD %' datatype='real' name='[Calculation_1]' role='measure' "
                "type='quantitative'><calculation class='tableau' formula='[Sales]' /></column>")
    calc_inst = ("<column-instance column='[Calculation_1]' derivation='User' "
                 "name='[pcdf:Calculation_1:qk]' pivot='key' type='quantitative' />")
    style = _mark_color_style("[federated.abc].[pcdf:Calculation_1:qk]", "ordered-diverging",
                              ["#f28e2b", "#d9d9d9", "#e6e6e6"], center="0.0",
                              enc_type="custom-interpolated")
    enc = ("<encodings><color column='[federated.abc].[pcdf:Calculation_1:qk]' />"
           "<text column='[federated.abc].[sum:Sales:qk]' /></encodings>")
    res = migrate_twb_to_pbir(_workbook(
        _heat_ws("Heat", color_field="pcdf:Calculation_1:qk", encodings=enc, style=style,
                 deps_extra=_INST + calc_col + calc_inst)))
    # no fill emitted on the visual
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert _values_objects(vj) is None
    # candidate record keeps the palette + a deferred status
    fact = _cf_fact(res["candidate_records"], "Heat")
    assert fact["status"] == "deferred"
    assert "quick table calc" in fact["reason"]
    assert fact["colors"] == ["#f28e2b", "#d9d9d9", "#e6e6e6"]
    assert fact["center"] == 0.0
    assert any("background colour scale deferred" in w["reason"] for w in res["warnings"])


def test_emitted_conditional_format_fact_recorded_on_candidate_record():
    style = _mark_color_style("[federated.abc].[sum:Sales:qk]", "ordered-sequential",
                              ["#f7fbff", "#08306b"])
    enc = "<encodings><color column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    res = migrate_twb_to_pbir(_workbook(
        _heat_ws("Heat", color_field="sum:Sales:qk", encodings=enc, style=style)))
    fact = _cf_fact(res["candidate_records"], "Heat")
    assert fact["status"] == "emitted"
    assert fact["bound_measure"] == "Sum(Orders.Sales_Amount)"
    assert fact["target"] == "Sum(Orders.Sales_Amount)"


def test_matrix_without_colour_gradient_emits_no_conditional_format():
    # Additivity: a plain highlight-table matrix (no <style> colour scale) carries neither a
    # values object nor a conditional_format fact -- the report is byte-unchanged from before.
    enc = "<encodings><color column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    res = migrate_twb_to_pbir(_workbook(
        _heat_ws("Heat", color_field="sum:Sales:qk", encodings=enc, style="")))
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert _values_objects(vj) is None
    assert _cf_fact(res["candidate_records"], "Heat") is None


def test_categorical_colour_legend_is_not_a_gradient():
    # A discrete (categorical) colour legend is Tier-2 legend styling, not a cell heat scale:
    # no color_gradient is parsed and no fill is emitted.
    style = ("<style><style-rule element='mark'><encoding attr='color' type='palette' "
             "field='[federated.abc].[none:Region:nk]'>"
             "<color-palette type='regular'><color>#111111</color><color>#222222</color>"
             "</color-palette></encoding></style-rule></style>")
    enc = ("<encodings><color column='[federated.abc].[none:Region:nk]' />"
           "<text column='[federated.abc].[sum:Sales:qk]' /></encodings>")
    ir = parse_twb(_workbook(_heat_ws("Heat", color_field="none:Region:nk",
                                      encodings=enc, style=style)))
    assert ir["worksheets"][0]["color_gradient"] is None


# -- cross-layer measure binding (model<->viz contract consumer) ---------------
# The datasource-migration (model) build hands back a token-keyed calc->measure manifest; the
# dashboard (viz) build rebinds the matching workbook-local / quick-table-calc pills to those real
# ``_Measures`` measures. Binding is DETERMINISTIC (token-keyed) and only for translated /
# assisted-approved measures (warn-never-wrong). Default (no binding) -> byte-unchanged.
def _pcdf_heat_workbook():
    # The Comcast pilot heat grid: a percent-difference quick-table-calc (``pcdf:``) drives the cell
    # colour; the displayed value is SUM(Sales). Without a measure binding this DEFERS (no model
    # measure for the table calc); with one it lights up.
    calc_col = ("<column caption='Percent Difference' datatype='real' name='[Calculation_1]' "
                "role='measure' type='quantitative'>"
                "<calculation class='tableau' formula='[Sales]' /></column>")
    calc_inst = ("<column-instance column='[Calculation_1]' derivation='User' "
                 "name='[pcdf:Calculation_1:qk]' pivot='key' type='quantitative' />")
    style = _mark_color_style("[federated.abc].[pcdf:Calculation_1:qk]", "ordered-diverging",
                              ["#f28e2b", "#d9d9d9", "#e6e6e6"], center="0.0",
                              enc_type="custom-interpolated")
    enc = ("<encodings><color column='[federated.abc].[pcdf:Calculation_1:qk]' />"
           "<text column='[federated.abc].[sum:Sales:qk]' /></encodings>")
    return _workbook(_heat_ws("Heat", color_field="pcdf:Calculation_1:qk", encodings=enc,
                              style=style, deps_extra=_INST + calc_col + calc_inst))


def test_measure_binding_lights_up_heat_grid_via_pcdf_instance_token():
    # The model build translated the pcdf table calc into a named _Measures measure and reports it
    # under the pill INSTANCE token. The colour driver now binds: Sales is the only displayed value,
    # the Percent Difference measure rides the Tooltips, and the backColor FillRule references it.
    mb = {"pcdf:Calculation_1:qk": {"entity": "_Measures",
                                    "measure": "Percent Difference", "status": "translated"}}
    res = migrate_twb_to_pbir(_pcdf_heat_workbook(), measure_binding=mb)
    vj = list(_visual_parts(res["parts"]).values())[0]
    qs = _query_state(vj)
    val_refs = {p["queryRef"] for p in qs["Values"]["projections"]}
    tip_refs = {p["queryRef"] for p in qs["Tooltips"]["projections"]}
    assert val_refs == {"Sum(Orders.Sales_Amount)"}            # displayed value only
    assert tip_refs == {"_Measures.Percent Difference"}        # colour driver on the tooltip
    # the conditional-format fill lights up against the contracted measure
    fr = _fill_rule(_values_objects(vj))
    assert fr["Input"]["Measure"]["Property"] == "Percent Difference"
    fact = _cf_fact(res["candidate_records"], "Heat")
    assert fact["status"] == "emitted"
    assert fact["bound_measure"] == "_Measures.Percent Difference"
    assert fact["target"] == "Sum(Orders.Sales_Amount)"
    # no dangling Calculation_1 / pcdf reference leaks anywhere in the report
    blob = "".join(res["parts"].values())
    assert "Calculation_1" not in blob and "pcdf:" not in blob


def test_measure_binding_keyed_by_bare_calc_id_and_wrapper_form():
    # Join priority allows the bare Calculation_* id (not just the instance token); the wrapper
    # ``{"measures": {...}}`` shape is accepted too (mirrors row_count_binding).
    mb = {"measures": {"Calculation_1": {"model_table": "_Measures",
                                         "measure_name": "Percent Difference",
                                         "status": "assisted-approved"}}}
    res = migrate_twb_to_pbir(_pcdf_heat_workbook(), measure_binding=mb)
    vj = list(_visual_parts(res["parts"]).values())[0]
    tip_refs = {p["queryRef"] for p in _query_state(vj)["Tooltips"]["projections"]}
    assert tip_refs == {"_Measures.Percent Difference"}
    assert _cf_fact(res["candidate_records"], "Heat")["status"] == "emitted"


def test_measure_binding_non_bindable_status_still_defers():
    # A measure the model only SUGGESTED (or stubbed / handed off) is NOT bound -- warn-never-wrong.
    for status in ("assisted-suggested", "stub", "handoff"):
        mb = {"pcdf:Calculation_1:qk": {"entity": "_Measures",
                                        "measure": "Percent Difference", "status": status}}
        res = migrate_twb_to_pbir(_pcdf_heat_workbook(), measure_binding=mb)
        vj = list(_visual_parts(res["parts"]).values())[0]
        assert _values_objects(vj) is None, f"{status} should not emit a fill"
        fact = _cf_fact(res["candidate_records"], "Heat")
        assert fact["status"] == "deferred"
        assert "quick table calc" in fact["reason"]


def test_measure_binding_default_none_is_byte_unchanged():
    # Additivity: omitting the binding == passing None == passing an empty map -> the prior deferred
    # output, byte-for-byte.
    wb = _pcdf_heat_workbook()
    base = migrate_twb_to_pbir(wb)["parts"]
    assert migrate_twb_to_pbir(wb, measure_binding=None)["parts"] == base
    assert migrate_twb_to_pbir(wb, measure_binding={})["parts"] == base
    assert migrate_twb_to_pbir(wb, measure_binding={"measures": {}})["parts"] == base
    # and a binding for an UNRELATED token leaves this workbook untouched
    other = {"some:Other:qk": {"entity": "_Measures", "measure": "Nope", "status": "translated"}}
    assert migrate_twb_to_pbir(wb, measure_binding=other)["parts"] == base


def _pcdf_pilot_heat_workbook():
    # The Comcast pilot's heat-grid colour pill carries the FULL extractor instance token -- INCLUDING
    # the ``usr:`` addressing segment AND the ``:qk`` suffix (pcdf:usr:Calculation_*:qk). The model
    # build stamps ``calc_instance_token`` = the extractor's ``TableCalcUsage.instance`` VERBATIM, so
    # the join must be byte-identical on that token; the bare calc id alone resolves to the BASE value
    # ([count orders]+100), a DIFFERENT measure, so it must NOT be what lights the colour.
    cid = "Calculation_0014172369735704"
    tok = "pcdf:usr:Calculation_0014172369735704:qk"
    calc_col = (f"<column caption='[count orders] + 100' datatype='integer' name='[{cid}]' "
                "role='measure' type='quantitative'>"
                "<calculation class='tableau' formula='[Calculation_0014172369248279] + 100' />"
                "</column>")
    calc_inst = (f"<column-instance column='[{cid}]' derivation='User' "
                 f"name='[{tok}]' pivot='key' type='quantitative' />")
    style = _mark_color_style(f"[federated.abc].[{tok}]", "ordered-diverging",
                              ["#f28e2b", "#d9d9d9", "#e6e6e6"], center="0.0",
                              enc_type="custom-interpolated")
    enc = (f"<encodings><color column='[federated.abc].[{tok}]' />"
           "<text column='[federated.abc].[sum:Sales:qk]' /></encodings>")
    return _workbook(_heat_ws("Heat", color_field=tok, encodings=enc,
                              style=style, deps_extra=_INST + calc_col + calc_inst))


def test_measure_binding_binds_pilot_pcdf_usr_instance_token_verbatim():
    # THE PILOT LINCHPIN regression guard: bind on the extractor's verbatim instance token (with the
    # ``usr:`` segment). The heat grid lights against the contracted measure and the token never leaks.
    tok = "pcdf:usr:Calculation_0014172369735704:qk"
    mb = {tok: {"entity": "_Measures", "measure": "Percent Difference (DoD)", "status": "translated"}}
    res = migrate_twb_to_pbir(_pcdf_pilot_heat_workbook(), measure_binding=mb)
    vj = list(_visual_parts(res["parts"]).values())[0]
    fr = _fill_rule(_values_objects(vj))
    assert fr["Input"]["Measure"]["Property"] == "Percent Difference (DoD)"
    fact = _cf_fact(res["candidate_records"], "Heat")
    assert fact["status"] == "emitted"
    assert fact["bound_measure"] == "_Measures.Percent Difference (DoD)"
    blob = "".join(res["parts"].values())
    assert tok not in blob and "Calculation_0014172369735704" not in blob


def test_measure_binding_same_base_pcdf_and_plain_pills_disambiguate():
    # Pilot integration lock (verified live against the real .twb): on the heat grid two pills share
    # the SAME base calc (Calculation_0014172369735704). The COLOUR pill is the pcdf quick-table-calc
    # instance and the LABEL pill is the plain pill. The token-first join must resolve them to
    # DIFFERENT measures -- the pcdf instance -> the %-difference measure, the plain pill (no pcdf
    # entry) falling through to the bare calc id -> the untransformed base -- so the grid is coloured
    # by the %-diff and the label shows the base, never mis-coloured by the base value.
    cid = "Calculation_0014172369735704"
    pcdf = "pcdf:usr:Calculation_0014172369735704:qk"
    plain = "usr:Calculation_0014172369735704:qk"
    calc_col = (f"<column caption='[count orders] + 100' datatype='integer' name='[{cid}]' "
                "role='measure' type='quantitative'>"
                "<calculation class='tableau' formula='[Calculation_0014172369248279] + 100' />"
                "</column>")
    insts = (f"<column-instance column='[{cid}]' derivation='User' name='[{pcdf}]' "
             "pivot='key' type='quantitative' />"
             f"<column-instance column='[{cid}]' derivation='User' name='[{plain}]' "
             "pivot='key' type='quantitative' />")
    enc = (f"<encodings><color column='[federated.abc].[{pcdf}]' />"
           f"<text column='[federated.abc].[{plain}]' /></encodings>")
    ws = _worksheet("Seg", "Square",
                    rows="[federated.abc].[none:Segment:nk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + calc_col + insts, encodings=enc)
    # the model build's calc_bindings handback: the pcdf instance + the bare base id (NOT the plain
    # instance token), exactly as the model stamps them.
    mb = {"measures": {
        pcdf: {"model_table": "_Measures", "status": "translated",
               "measure_name": "[count orders] + 100 (percent difference from a prior row)"},
        cid: {"model_table": "_Measures", "status": "translated",
              "measure_name": "[count orders] + 100"},
    }}
    enc_ir = parse_twb(_workbook(ws), measure_binding=mb)["worksheets"][0]["encodings"]
    assert enc_ir["color"]["measure_rebound"] is True
    assert enc_ir["color"]["entity"] == "_Measures"
    assert enc_ir["color"]["property"] == "[count orders] + 100 (percent difference from a prior row)"
    # plain pill: its own instance token is absent from the binding, so it resolves on the bare calc
    # id to the BASE measure -- a different measure than the colour, no mis-colour.
    assert enc_ir["label"]["measure_rebound"] is True
    assert enc_ir["label"]["property"] == "[count orders] + 100"
