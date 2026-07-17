"""Tableau ``.twb`` viz-grammar -> PBIR wireframe tests (offline, inline XML fixtures).

Every fixture is a structurally faithful (but trimmed) Tableau workbook string; no files are
touched and no network is used. The asserts validate (a) the normalized IR a worksheet/
dashboard parses into, (b) the emitted PBIR JSON structure + field bindings per supported
visual, and (c) that unsupported marks/derivations/filters degrade to ``warnings`` instead of
producing a wrong visual.
"""
import json
import xml.etree.ElementTree as ET

import pytest

from twb_to_pbir import (
    MEASURES_TABLE,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    SCHEMA_VISUAL,
    SCHEMA_VISUAL_FP,
    SCHEMA_VISUAL_SM,
    _DATE_EXACT_DERIVATIONS,
    _apply_grow_to_fit,
    _apply_override,
    _candidate_plan,
    _card_latent_candidates,
    _drop_resolved_flag_warnings,
    _field_expression,
    _flag_filter_container,
    _image_item_name,
    _image_visual,
    _norm_param_key,
    _position,
    _reconcile_caption_fallback,
    _resolve_parameter_controls,
    _resolve_resource_bytes,
    _resolve_visual_flags,
    _resource_basename,
    _tableau_filter_mode_to_pbi,
    _text_object_textbox_visual,
    _visual_json,
    _zone_background_fill2,
    _zone_run_font,
    build_field_parameter_page,
    emit_pbir,
    field_parameter_slicer,
    field_parameter_table_visual,
    migrate_twb_to_pbir,
    parse_twb,
    report_json_part,
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
    assert state["SmallMultiple"]["projections"][0]["field"]["Column"]["Property"] == "Region"
    assert "Series" not in state


def test_small_multiples_visual_uses_newer_schema_others_stay_1_0_0():
    # A visual that binds a SmallMultiple (trellis) role must be stamped at the newer
    # visualContainer schema -- Power BI Desktop silently DROPS the small-multiples role on the
    # legacy 1.0.0 stamp (the chart collapses to a single aggregated panel). The bump is gated to
    # ONLY trellis visuals so the verified non-trellis gates keep their proven 1.0.0 stamp.
    tmonth = ("<column-instance column='[Order Date]' derivation='Month-Trunc' "
              "name='[tmn:Order Date:qk]' pivot='key' type='quantitative' />")
    trellis = _worksheet("Trend by Region", "Line",
                         rows="([federated.abc].[none:Region:nk] * "
                              "[federated.abc].[sum:Sales:qk])",
                         cols="[federated.abc].[tmn:Order Date:qk]",
                         deps_extra=_INST + tmonth)
    vis = list(_visual_parts(emit_pbir(parse_twb(_workbook(trellis)))).values())[0]
    assert "SmallMultiple" in vis["visual"]["query"]["queryState"]
    assert vis["$schema"] == SCHEMA_VISUAL_SM
    # the data-plane formatting card that actually lays the panes out (without it the role binds
    # but no trellis renders)
    sm = vis["visual"]["objects"]["smallMultiple"][0]["properties"]
    assert sm["layoutMode"]["expr"]["Literal"]["Value"] == "'flow'"
    assert sm["maxItemsPerRow"]["expr"]["Literal"]["Value"] == "3L"

    # a plain bar (no paning dimension -> no SmallMultiple role) keeps the proven 1.0.0 stamp
    plain = _worksheet("Sales by Region", "Bar",
                       rows="[federated.abc].[sum:Sales:qk]",
                       cols="[federated.abc].[none:Region:nk]",
                       deps_extra=_INST)
    bar = list(_visual_parts(emit_pbir(parse_twb(_workbook(plain)))).values())[0]
    assert "SmallMultiple" not in bar["visual"]["query"]["queryState"]
    assert bar["$schema"] == SCHEMA_VISUAL
    assert "smallMultiple" not in bar["visual"].get("objects", {})


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


def test_geo_dimension_on_detail_only_is_location_only_filled_map():
    # A geographic dimension alone on Detail with no measure is Tableau's default map of that
    # geography -> a faithful location-only filledMap (Category = the location, no saturation
    # measure); it must NOT be flattened into a one-column text list.
    enc = "<encodings><lod column='[federated.abc].[none:State:nk]' /></encodings>"
    ws = _worksheet("State Map", "Automatic", rows="", cols="",
                    deps_extra=_INST, encodings=enc)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "filled_map"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "filledMap"
    state = _query_state(vis)
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "State"
    assert "Tooltips" not in state


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


# -- implicit row count across a Tableau join-order prefix (AAR #1 Issue E) -----
# When a physical table is added to a join Tableau stamps an order prefix on its name ("1. Login-
# History"); the migrated model declares the clean table ("LoginHistory") and keys its COUNTROWS
# measure clean. The implicit object-id COUNT (from the workbook) still carries the prefixed name,
# so an exact-only match blanked the KPI card. The binding now normalises the prefix on either side.
_HEX3 = "A1B2C3D4E5F607182930415263748596"
_COL_OID_LOGIN = (f"<column caption='1. LoginHistory' datatype='integer' "
                  f"name='[{_OID}].[LoginHistory_{_HEX3}]' role='measure' type='quantitative' />")
_CI_CNT_LOGIN = (f"<column-instance column='[{_OID}].[LoginHistory_{_HEX3}]' derivation='Count' "
                 f"name='[cnt:LoginHistory_{_HEX3}:qk]' pivot='key' type='quantitative' />")
_OID_COUNT_PILL_LOGIN = f"[federated.abc].[{_OID}].[cnt:LoginHistory_{_HEX3}:qk]"


def test_object_id_row_count_binds_across_tableau_order_prefix():
    # workbook count names the prefixed physical table "1. LoginHistory"; the model measure is keyed
    # by the clean table "LoginHistory" -> the KPI card binds (no silent blank, no warning).
    ws = _worksheet("KPI", "Bar",
                    rows=_OID_COUNT_PILL_LOGIN,
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _COL_OID_LOGIN + _CI_CNT_LOGIN)
    rcb = {"measures": {"LoginHistory": {"entity": "_Measures", "measure": "count logins"}}}
    ir = parse_twb(_workbook(ws), row_count_binding=rcb)
    rows = ir["worksheets"][0]["rows"]
    assert len(rows) == 1
    f = rows[0]
    assert f["binding"] == "measure" and f["entity"] == "_Measures" and f["property"] == "count logins"
    assert _count_warns(ir) == []


def test_strip_table_order_prefix_only_strips_the_tableau_shape():
    from twb_to_pbir import _strip_table_order_prefix
    assert _strip_table_order_prefix("1. LoginHistory") == "LoginHistory"
    assert _strip_table_order_prefix("12.  Orders") == "Orders"      # multi-digit + extra space
    assert _strip_table_order_prefix("LoginHistory") == "LoginHistory"
    assert _strip_table_order_prefix("2024.Q1") == "2024.Q1"          # dot, no space -> untouched
    assert _strip_table_order_prefix("3D Models") == "3D Models"      # no dot -> untouched
    assert _strip_table_order_prefix("") == ""


def test_row_count_measure_target_prefix_tolerant_both_directions():
    from twb_to_pbir import _row_count_measure_target
    # prefix on the workbook side, clean model key
    rc_pref = {"kind": "object_id", "table": "1. LoginHistory", "candidates": ["1. LoginHistory"]}
    clean = {"measures": {"LoginHistory": {"entity": "_Measures", "measure": "count logins"}}}
    assert _row_count_measure_target(rc_pref, clean) == ("_Measures", "count logins")
    # clean workbook side, prefixed model key (symmetric)
    rc_clean = {"kind": "object_id", "table": "LoginHistory", "candidates": ["LoginHistory"]}
    pref = {"measures": {"1. LoginHistory": {"entity": "_Measures", "measure": "count logins"}}}
    assert _row_count_measure_target(rc_clean, pref) == ("_Measures", "count logins")


def test_row_count_measure_target_prefix_match_must_be_unambiguous():
    from twb_to_pbir import _row_count_measure_target
    rc = {"kind": "object_id", "table": "LoginHistory", "candidates": ["LoginHistory"]}
    # two model measures normalise to the same physical table -> ambiguous -> no bind (warn later)
    ambiguous = {"measures": {"1. LoginHistory": {"entity": "_Measures", "measure": "A"},
                              "2. LoginHistory": {"entity": "_Measures", "measure": "B"}}}
    assert _row_count_measure_target(rc, ambiguous) is None
    # an exact key still wins even when a normalised collision also exists
    exact_wins = {"measures": {"LoginHistory": {"entity": "_Measures", "measure": "X"},
                               "1. LoginHistory": {"entity": "_Measures", "measure": "Y"}}}
    assert _row_count_measure_target(rc, exact_wins) == ("_Measures", "X")


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


def test_reconcile_caption_fallback_drops_model_confirmed_keeps_unverified():
    # the model build's field_map confirms 'Sales' but not 'Widget'
    covered = {"scope": "worksheet", "name": "S1",
               "reason": "manual attention required: field 'Sales' bound by caption fallback "
                         "(no datasource metadata); verify it matches model table/column names",
               "caption_fallback": "Sales"}
    uncovered = {"scope": "worksheet", "name": "S2",
                 "reason": "manual attention required: field 'Widget' bound by caption fallback "
                           "(no datasource metadata); verify it matches model table/column names",
                 "caption_fallback": "Widget"}
    other = {"scope": "worksheet", "name": "S3",
             "reason": "manual attention required: something unrelated"}
    out = _reconcile_caption_fallback([covered, uncovered, other],
                                      {"Sales": {"entity": "Orders", "property": "Sales"}})
    reasons = [w["reason"] for w in out]
    # the model-confirmed caption's advisory is dropped (it is no longer true)
    assert not any("'Sales' bound by caption fallback" in r for r in reasons)
    # the unverified caption's advisory is kept (genuinely unconfirmed)
    assert any("'Widget' bound by caption fallback" in r for r in reasons)
    # an unrelated warning is untouched
    assert any("something unrelated" in r for r in reasons)
    # the internal marker never leaks into the surfaced warnings
    assert all("caption_fallback" not in w for w in out)


# -- _apply_override: rebind a mis-roled ref to its real column (never a dangling measure) ------
def _ir_field(caption, binding, *, entity="sqlproxy", prop=None, aggregation=None):
    """Minimal IR field dict for the ``_apply_override`` / ``_field_expression`` seam."""
    return {"caption": caption, "entity": entity, "property": prop or caption,
            "binding": binding, "aggregation": aggregation}


def test_apply_override_rebinds_measure_kind_ref_to_the_resolved_column():
    # a ``measure``-kind pill whose caption the (columns-only) field_map resolves to a real model
    # column must be rebound TO that column -- a ``{"Measure"}`` expr pointing at a column is invalid
    fld = _ir_field("Region", "measure")
    fm = {"Region": {"entity": "Orders", "property": "Region"}}  # real producer: no "binding" key
    entity, prop, binding = _apply_override(fld, "Orders", fm)
    assert (entity, prop, binding) == ("Orders", "Region", "column")
    expr, qref, _ = _field_expression(fld, "Orders", fm)
    assert "Column" in expr and "Measure" not in expr
    assert qref == "Orders.Region"


def test_apply_override_keeps_aggregation_pill_aggregation():
    # a SUM([Sales]) pill in the field_map keeps its aggregation (entity corrected only) -- the
    # documented invariant the change must not disturb
    fld = _ir_field("Sales", "aggregation", aggregation="Sum")
    fm = {"Sales": {"entity": "Orders", "property": "Sales"}}
    entity, prop, binding = _apply_override(fld, "Orders", fm)
    assert (entity, prop, binding) == ("Orders", "Sales", "aggregation")
    expr, _, _ = _field_expression(fld, "Orders", fm)
    assert "Aggregation" in expr


def test_apply_override_keeps_plain_column_pill_column():
    fld = _ir_field("Category", "column")
    fm = {"Category": {"entity": "Orders", "property": "Category"}}
    _, _, binding = _apply_override(fld, "Orders", fm)
    assert binding == "column"


def test_apply_override_measure_ref_not_in_field_map_stays_measure():
    # the zero-regression guard: a genuine _Measures ref (row-count / measure_binding) whose caption
    # is NOT in the columns-only field_map is untouched -- it must still emit a {"Measure"} expr
    fld = _ir_field("count orders", "measure", entity="_Measures")
    fm = {"Sales": {"entity": "Orders", "property": "Sales"}}
    entity, prop, binding = _apply_override(fld, "Orders", fm)
    assert (entity, prop, binding) == ("_Measures", "count orders", "measure")
    expr, _, _ = _field_expression(fld, "Orders", fm)
    assert "Measure" in expr and "Column" not in expr


def test_apply_override_explicit_field_map_binding_wins():
    # a producer that DOES stamp an explicit binding is honoured verbatim (guards the ``or`` path)
    fld = _ir_field("Region", "measure")
    fm = {"Region": {"entity": "Orders", "property": "Region", "binding": "column"}}
    _, _, binding = _apply_override(fld, "Orders", fm)
    assert binding == "column"


def test_apply_override_column_rebound_survives_model_table_clobber():
    # THE Choose-Date regression: a calc DIMENSION the model materialised into its OWN table (a
    # field-parameter axis lands in 'Choose Date'[Choose Date]) is stamped ``column_rebound`` by
    # ``_resolve_field``. The ``model_table`` fallback must NOT re-pin it onto the sheet's fact and
    # dangle Sheet1[Choose Date] -- the stamp makes the manifest binding authoritative.
    fld = _ir_field("Choose Date", "column", entity="Choose Date")
    fld["column_rebound"] = True
    entity, prop, binding = _apply_override(fld, "Sheet1", {})
    assert (entity, prop, binding) == ("Choose Date", "Choose Date", "column")
    # even a field_map entry for the caption cannot pull it back (guard returns before field_map)
    fm = {"Choose Date": {"entity": "Sheet1", "property": "Choose Date"}}
    assert _apply_override(fld, "Sheet1", fm) == ("Choose Date", "Choose Date", "column")


def test_apply_override_unstamped_column_still_takes_model_table_fallback():
    # zero-regression companion: an ordinary column pill NOT stamped ``column_rebound`` still takes
    # the ``model_table`` fallback (entity corrected to the sheet's model table) exactly as before,
    # so the fail-closed stamp changes nothing for a field with no manifest hit.
    fld = _ir_field("Widget", "column", entity="sqlproxy")
    assert _apply_override(fld, "Sheet1", {}) == ("Sheet1", "Widget", "column")


def test_caption_fallback_warning_cleared_when_field_map_confirms_binding():
    # workbook WITHOUT a <datasources> metadata tree -> Sales + Category fall back to caption
    wb = ("<?xml version='1.0' encoding='utf-8' ?>\n<workbook><worksheets>"
          + _worksheet("Bare", "Bar",
                       rows="[federated.abc].[sum:Sales:qk]",
                       cols="[federated.abc].[none:Category:nk]",
                       deps_extra=_INST)
          + "</worksheets></workbook>")
    # the model build's metadata-confirmed naming (field_map) covers Sales but not Category
    fm = {"Sales": {"entity": "Orders", "property": "Sales", "binding": "aggregation"}}
    res = migrate_twb_to_pbir(wb, dataset_name="Superstore", field_map=fm)
    reasons = [w["reason"] for w in res["warnings"]]
    # Sales is model-confirmed -> its stale caption-fallback advisory is gone
    assert not any("'Sales' bound by caption fallback" in r for r in reasons)
    # Category is NOT in field_map -> its caption-fallback advisory persists (warn-never-wrong)
    assert any("'Category' bound by caption fallback" in r for r in reasons)
    # the emitted Sales projection is bound to the model table the field_map named
    blob = "\n".join(res["parts"].values())
    assert '"Entity": "Orders"' in blob and '"Property": "Sales"' in blob


# -- calc-dimension crosstab: stays a matrix, binds to the model column (Fix 1 + column_binding) --
# Two calculated fields used as discrete DIMENSIONS on both axes of a Text crosstab. Before the fix
# every calc pill was forced into the measure well, so ``_visual_type`` saw zero dimensions and
# collapsed the crosstab into a single ``card`` (axes dropped). Now a calc dimension binds to its
# real model column and lands in the category well, so the crosstab rebuilds as a matrix.
_CALC_DIM_COHORT = (
    "<column caption='Cohort' datatype='string' name='[Calculation_CO]' role='dimension' type='nominal'>"
    "<calculation class='tableau' formula='IF [Sales] &gt; 100 THEN &quot;Hi&quot; ELSE &quot;Lo&quot; END' />"
    "</column>"
    "<column-instance column='[Calculation_CO]' derivation='None' name='[none:Calculation_CO:nk]' pivot='key' type='nominal' />"
)
_CALC_DIM_SEGMENT = (
    "<column caption='Segment' datatype='string' name='[Calculation_SG]' role='dimension' type='nominal'>"
    "<calculation class='tableau' formula='IF [Profit] &gt; 0 THEN &quot;Win&quot; ELSE &quot;Loss&quot; END' />"
    "</column>"
    "<column-instance column='[Calculation_SG]' derivation='None' name='[none:Calculation_SG:nk]' pivot='key' type='nominal' />"
)


def _calc_dim_crosstab_workbook():
    enc = "<encodings><text column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    ws = _worksheet("Cross", "Text",
                    rows="[federated.abc].[none:Calculation_CO:nk]",
                    cols="[federated.abc].[none:Calculation_SG:nk]",
                    deps_extra=_INST + _CALC_DIM_COHORT + _CALC_DIM_SEGMENT, encodings=enc)
    return _workbook(ws)


def _matrix_state(res):
    vparts = {k: json.loads(v) for k, v in res["parts"].items() if k.endswith("visual.json")}
    return list(vparts.values())[0]["visual"]["query"]["queryState"]


def test_calc_dimension_crosstab_binds_to_model_columns_and_survives_model_table():
    # WITH the model-confirmed manifest each calc dim binds to its OWN model table+column; passing a
    # distinct fact ``model_table`` proves the ``column_rebound`` stamp protects the binding from the
    # clobber end-to-end (Cohort stays DimCohort[Cohort], never Sheet1[Cohort]).
    wb = _calc_dim_crosstab_workbook()
    cb = {"columns": {
        "Cohort": {"table": "DimCohort", "column": "Cohort"},
        "Segment": {"table": "DimSegment", "column": "Segment"},
    }}
    res = migrate_twb_to_pbir(wb, dataset_name="Superstore", model_table="Sheet1", column_binding=cb)
    assert res["ir"]["worksheets"][0]["visual_type"] == "matrix"
    state = _matrix_state(res)
    assert set(state) == {"Rows", "Columns", "Values"}
    rows_field = state["Rows"]["projections"][0]["field"]["Column"]
    cols_field = state["Columns"]["projections"][0]["field"]["Column"]
    assert rows_field["Property"] == "Cohort"
    assert rows_field["Expression"]["SourceRef"]["Entity"] == "DimCohort"
    assert cols_field["Property"] == "Segment"
    assert cols_field["Expression"]["SourceRef"]["Entity"] == "DimSegment"
    # the SUM(Sales) pill is the matrix value (aggregation over the fact) -- the calc dims are the
    # axes, so nothing collapsed into the value well and neither dim was clobbered onto the fact
    val_field = state["Values"]["projections"][0]["field"]
    assert val_field["Aggregation"]["Expression"]["Column"]["Property"] == "Sales_Amount"


def test_calc_dimension_crosstab_stays_matrix_without_column_binding():
    # Fix 1 is robust WITHOUT the manifest: a calc dimension still resolves as a category (caption
    # fallback + warning), so the crosstab is a matrix -- never a card -- even when no column_binding
    # is supplied. Binding to the real column is the manifest's job; keeping the axes is the fix's.
    wb = _calc_dim_crosstab_workbook()
    res = migrate_twb_to_pbir(wb, dataset_name="Superstore")
    assert res["ir"]["worksheets"][0]["visual_type"] == "matrix"
    state = _matrix_state(res)
    assert set(state) == {"Rows", "Columns", "Values"}
    # the axes carry the calc dimensions (as categories), not a single collapsed value well
    assert state["Rows"]["projections"][0]["field"]["Column"]["Property"] == "Cohort"
    assert state["Columns"]["projections"][0]["field"]["Column"]["Property"] == "Segment"


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
    # §13 geometry: the page adopts the dashboard's real <size> (1200x800), not the fixed
    # 1280x720 default; the placed zone scales within those real page bounds.
    page = json.loads(parts[page_jsons[0]])
    assert (page["width"], page["height"]) == (1200, 800)
    pos = list(_visual_parts(parts).values())[0]["position"]
    assert 0 <= pos["x"] and pos["x"] + pos["width"] <= page["width"]
    assert 0 <= pos["y"] and pos["y"] + pos["height"] <= page["height"]


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
    # a dashboard page carries a slicer for each filter the author SHOWED as a dashboard filter card
    # (a <zone type-v2='filter'>), deduped across worksheets: Region + State are both shown on the
    # dashboard (two sheets reference Region, one also State) -> exactly two distinct slicers (Region
    # once, State once), alongside the two chart visuals.
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
             "<zone h='45000' w='90000' x='5000' y='55000' name='ProfitWs' id='3' />"
             "<zone name='SalesWs' param='[federated.abc].[none:Region:nk]' type-v2='filter' "
             "h='6000' w='20000' x='5000' y='92000' id='20' />"
             "<zone name='SalesWs' param='[federated.abc].[none:State:nk]' type-v2='filter' "
             "h='6000' w='20000' x='30000' y='92000' id='21' /></zone>")
    dash = ("<dashboard name='D'><size maxheight='800' maxwidth='1200' />"
            "<zones>" + inner + "</zones></dashboard>")
    parts = emit_pbir(parse_twb(_workbook(ws1 + ws2, dash)))
    # all visuals land on the single dashboard page
    assert len([k for k in parts if k.endswith("page.json")]) == 1
    slicer_props = sorted(
        v["visual"]["query"]["queryState"]["Values"]["projections"][0]["field"]["Column"]["Property"]
        for v in _visual_parts(parts).values() if v["visual"]["visualType"] == "slicer")
    assert slicer_props == ["Region", "State"]  # deduped: Region once despite two sheets


def test_unshown_dashboard_filter_does_not_fabricate_a_slicer():
    # Faithful contract (warn-never-wrong): a worksheet filter the author did NOT expose as a
    # dashboard filter card -- e.g. a single-member scope include that merely narrows one sheet to a
    # region -- must NOT fabricate a page slicer the dashboard never had. With no <zone
    # type-v2='filter'> on the dashboard, the page carries zero slicers (the chart visuals remain).
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='member' member='&quot;West&quot;' /></filter>")
    ws1 = _worksheet("MapWs", "Bar", "[federated.abc].[sum:Sales:qk]",
                     "[federated.abc].[none:Category:nk]", deps_extra=_INST, filters=filt)
    ws2 = _worksheet("TrendWs", "Bar", "[federated.abc].[sum:Profit:qk]",
                     "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    inner = ("<zone h='100000' w='100000' x='0' y='0'>"
             "<zone h='45000' w='90000' x='5000' y='5000' name='MapWs' id='2' />"
             "<zone h='45000' w='90000' x='5000' y='55000' name='TrendWs' id='3' /></zone>")
    dash = ("<dashboard name='D'><size maxheight='800' maxwidth='1200' />"
            "<zones>" + inner + "</zones></dashboard>")
    parts = emit_pbir(parse_twb(_workbook(ws1 + ws2, dash)))
    slicers = [v for v in _visual_parts(parts).values()
               if v["visual"]["visualType"] == "slicer"]
    assert slicers == []
    mains = [v for v in _visual_parts(parts).values()
             if v["visual"]["visualType"] != "slicer"]
    assert len(mains) == 2  # the charts are still emitted -- only the fabricated slicer is gone


def test_dashboard_filter_card_in_nested_container_is_still_surfaced_as_slicer():
    # A shown filter card can live INSIDE a (collapsible) layout container rather than at the top
    # level; the zone walk recurses, so a nested <zone type-v2='filter'> is still recognised and the
    # field becomes a slicer.
    f_region = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
                "<groupfilter function='member' level='[none:Region:nk]' /></filter>")
    ws = _worksheet("Ws", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST, filters=f_region)
    inner = ("<zone h='100000' w='100000' x='0' y='0' type-v2='layout-basic'>"
             "<zone h='98000' w='98000' x='1000' y='1000' type-v2='layout-flow' param='vert'>"
             "<zone h='80000' w='90000' x='5000' y='5000' name='Ws' id='2' />"
             "<zone name='Ws' param='[federated.abc].[none:Region:nk]' type-v2='filter' "
             "h='6000' w='20000' x='5000' y='90000' id='20' /></zone></zone>")
    dash = ("<dashboard name='D'><size maxheight='800' maxwidth='1200' />"
            "<zones>" + inner + "</zones></dashboard>")
    parts = emit_pbir(parse_twb(_workbook(ws, dash)))
    slicer_props = sorted(
        v["visual"]["query"]["queryState"]["Values"]["projections"][0]["field"]["Column"]["Property"]
        for v in _visual_parts(parts).values() if v["visual"]["visualType"] == "slicer")
    assert slicer_props == ["Region"]



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
    # Region is shown as a dashboard filter card -> it surfaces as a slicer that cross-filters both charts.
    inner = ("<zone h='100000' w='100000' x='0' y='0'>"
             "<zone h='45000' w='90000' x='5000' y='5000' name='SalesWs' id='2' />"
             "<zone h='45000' w='90000' x='5000' y='55000' name='ProfitWs' id='3' />"
             "<zone name='SalesWs' param='[federated.abc].[none:Region:nk]' type-v2='filter' "
             "h='6000' w='20000' x='5000' y='92000' id='20' /></zone>")
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


def test_candidate_record_field_aliases_map_emitted_ref_to_source_tableau_caption():
    # star-schema-remodel rename guard: per emitted ref the oracle reads in ``fields``, carry the
    # SOURCE Tableau caption so a NAME-based structural compare can see through the remodel
    # (Tableau ``Sales`` lands as the emitted ``Sum(Orders.Sales_Amount)``; ``Category`` as the
    # table-qualified ``Orders.Category``).
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]", deps_extra=_INST)
    res = migrate_twb_to_pbir(_workbook(ws))
    r = res["candidate_records"][0]
    aliases = r["field_aliases"]
    assert aliases["Sum(Orders.Sales_Amount)"] == "Sales"
    assert aliases["Orders.Category"] == "Category"
    # keyed EXACTLY by the refs the oracle reads in ``fields`` (1:1 alignment, no stray keys)
    refs = {ref for role in r["fields"].values() for ref in role}
    assert set(aliases) <= refs


def test_field_aliases_never_written_into_emitted_pbir_parts():
    # the alias sidecar lives only on the candidate record, never in the emitted PBIR definition
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    res = migrate_twb_to_pbir(_workbook(ws))
    assert res["candidate_records"][0].get("field_aliases")  # present on the record
    blob = "\n".join(res["parts"].values())
    assert "field_aliases" not in blob


def test_field_alias_map_unit_maps_ref_to_caption_and_tolerates_no_model_table():
    # the helper maps every bound field's emitted ref -> its Tableau caption; with no model_table /
    # field_map it falls back to the field's own entity but still records the caption alias.
    from twb_to_pbir import _field_alias_map
    ws = {"name": "W",
          "rows": [{"caption": "Sales", "entity": "Q", "property": "Sales_Amount",
                    "binding": "aggregation", "aggregation": "Sum"}],
          "cols": [{"caption": "Order Date", "entity": "Q", "property": "Order_Date",
                    "binding": "column"}],
          "encodings": {"color": {"caption": "Segment", "entity": "Q", "property": "Segment",
                                  "binding": "column"}}}
    amap = _field_alias_map(ws, "Orders", None)
    assert amap["Sum(Orders.Sales_Amount)"] == "Sales"
    assert amap["Orders.Order_Date"] == "Order Date"
    assert amap["Orders.Segment"] == "Segment"


def test_field_alias_map_maps_star_schema_date_rebind_back_to_source_caption():
    # the headline COMCAST remodel: a continuous-date axis rebound to the marked Date dimension
    # emits ``Date.Date`` but the Tableau source is ``Order Date`` -- the alias resolves that exact
    # field-NAME divergence so a name-based structural compare reads the visual as faithful.
    from twb_to_pbir import _field_alias_map
    ws = {"name": "Line chart", "rows": [],
          "cols": [{"caption": "Order Date", "entity": "Date", "property": "Date",
                    "binding": "column", "date_rebound": True}],
          "encodings": {}}
    assert _field_alias_map(ws, "Orders", None) == {"Date.Date": "Order Date"}


# -- Spec 9a: card-collapse latent-dimension candidate rescue ------------------
def _mv_card(name, members, encodings_body=""):
    """A Measure-Values card (measure names on cols, Text marks) -- the exact path the six real
    ``multiRowCard`` collapses take. ``encodings_body`` adds the latent marks-card pills that survive
    the MV route (a <color> legend dim, a <lod> detail dim)."""
    text = "<text column='[federated.abc].[Multiple Values]' />"
    return _worksheet(name, "Text", rows="", cols="[federated.abc].[:Measure Names]",
                      deps_extra=_INST,
                      encodings=f"<encodings>{text}{encodings_body}</encodings>",
                      filters=_mv_filter(members))


def test_card_collapse_with_latent_legend_offers_pie_at_medium_confidence():
    # A pie whose slice category was demoted to Colour goes through the Measure-Values path and
    # card-collapses to a single big number; the latent legend dimension survives on the colour
    # encoding, so Spec 9a widens the candidate list to pie/donut at MEDIUM confidence -- the image
    # oracle can then rescue it. The DETERMINISTIC emit is unchanged (still a card).
    ws = _mv_card("Engagements by Stage", ["sum:Sales:qk"],
                  "<color column='[federated.abc].[none:Region:nk]' />")
    res = migrate_twb_to_pbir(_workbook(ws))
    rec = [r for r in res["candidate_records"] if r["worksheet"] == "Engagements by Stage"][0]
    assert rec["candidates"] == ["card", "pieChart", "donutChart"]
    assert rec["confidence"] == "medium"
    assert rec["hack"] == "latent-legend pie card-collapse"
    # deterministic emit untouched: the visual still renders as the collapsed card
    vt = list(_visual_parts(emit_pbir(parse_twb(_workbook(ws)))).values())[0]["visual"]["visualType"]
    assert vt in ("card", "multiRowCard")


def test_card_collapse_with_latent_detail_and_two_measures_offers_scatter():
    # A scatter (two measures against each other, granularity dim on Detail) card-collapses; the
    # latent detail dimension + two real measures make it recoverable as a scatterChart at medium.
    ws = _mv_card("Score Distribution", ["sum:Sales:qk", "sum:Profit:qk"],
                  "<lod column='[federated.abc].[none:State:nk]' />")
    res = migrate_twb_to_pbir(_workbook(ws))
    rec = [r for r in res["candidate_records"] if r["worksheet"] == "Score Distribution"][0]
    assert rec["candidates"] == ["multiRowCard", "scatterChart"]
    assert rec["confidence"] == "medium"
    assert rec["hack"] == "latent-detail scatter card-collapse"


def test_genuine_kpi_card_keeps_single_high_confidence_candidate():
    # A real KPI tile row (measures only, NO latent dimension) must NOT be offered an alternate -- it
    # stays a single high-confidence multiRowCard so the oracle never second-guesses a true card.
    ws = _mv_card("KPIs", ["sum:Sales:qk", "sum:Profit:qk"])
    res = migrate_twb_to_pbir(_workbook(ws))
    rec = [r for r in res["candidate_records"] if r["worksheet"] == "KPIs"][0]
    assert rec["candidates"] == ["multiRowCard"]
    assert rec["confidence"] == "high"
    assert rec["hack"] is None


def test_card_latent_candidates_binned_calc_offers_histogram():
    # a continuous binned calc demoted into the value well (caption keeps the Tableau spelling
    # "Age Bins Label"; the emitted ref is underscore-sanitised) -> a histogram column/bar.
    ws = {"rows": [], "cols": [{"caption": "Age Bins Label", "kind": "value"}],
          "encodings": {}, "swap_controls": []}
    state = {"Values": {"projections": [{"queryRef": "Sum(Orders.Age_Bins_Label)"}]}}
    cands, hack = _card_latent_candidates(ws, state)
    assert cands == ["clusteredColumnChart", "clusteredBarChart"]
    assert hack == "binned-calc card-collapse"


def test_card_latent_candidates_field_param_dimension_swap_offers_bar():
    # the field-parameter dimension swap ("... by Dimension"): detected either from swap_controls or
    # the bound field caption -> a swapped-category column/bar.
    state = {"Values": {"projections": [{"queryRef": "Sum(Orders.Sales_Amount)"}]}}
    by_swap = {"rows": [], "cols": [], "encodings": {}, "swap_controls": [{"param_id": "p"}]}
    c1, h1 = _card_latent_candidates(by_swap, state)
    assert c1 == ["clusteredColumnChart", "clusteredBarChart"]
    assert h1 == "field-param dimension-swap card-collapse"
    by_caption = {"rows": [], "cols": [{"caption": "Show by Dimension", "kind": "value"}],
                  "encodings": {}, "swap_controls": []}
    c2, _ = _card_latent_candidates(by_caption, state)
    assert c2 == ["clusteredColumnChart", "clusteredBarChart"]


def test_card_latent_candidates_ignores_constant_spacer_measure():
    # a Tableau donut-ring "1" is a spacer constant, not a real measure -- excluding it leaves ONE
    # real measure beside a latent detail category, so the shape is a pie, NOT a two-measure scatter.
    ws = {"rows": [], "cols": [],
          "encodings": {"detail": {"caption": "Stage", "kind": "category"}}, "swap_controls": []}
    state = {"Values": {"projections": [{"queryRef": "Count(Orders.Engagements)"},
                                        {"queryRef": "_Measures.1"}]}}
    cands, hack = _card_latent_candidates(ws, state)
    assert cands == ["pieChart", "donutChart"]
    assert hack == "latent-legend pie card-collapse"


def test_card_latent_candidates_no_signal_returns_empty():
    ws = {"rows": [], "cols": [], "encodings": {}, "swap_controls": []}
    state = {"Values": {"projections": [{"queryRef": "Sum(Orders.Sales_Amount)"}]}}
    assert _card_latent_candidates(ws, state) == ([], None)
    assert _card_latent_candidates(None, state) == ([], None)


def test_candidate_plan_card_medium_only_with_latent_signal_and_non_card_unchanged():
    from twb_to_pbir import VT_CARD
    with_latent = {"rows": [], "cols": [],
                   "encodings": {"color": {"caption": "R", "kind": "category"}}, "swap_controls": []}
    one_meas = {"Values": {"projections": [{"queryRef": "Sum(Orders.X)"}]}}
    cands, conf, hack = _candidate_plan(VT_CARD, "card", ws=with_latent, state=one_meas)
    assert cands == ["card", "pieChart", "donutChart"] and conf == "medium"
    assert hack == "latent-legend pie card-collapse"
    no_latent = {"rows": [], "cols": [], "encodings": {}, "swap_controls": []}
    cands2, conf2, hack2 = _candidate_plan(VT_CARD, "card", ws=no_latent, state=one_meas)
    assert cands2 == ["card"] and conf2 == "high" and hack2 is None
    # a non-card visual is unaffected by ws/state (existing orientation-alt behaviour preserved)
    cands3, conf3, _ = _candidate_plan("bar", "clusteredColumnChart", ws=None, state=None)
    assert cands3 == ["clusteredColumnChart", "clusteredBarChart"] and conf3 == "high"


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


def test_continuous_trunc_on_active_date_rebinds_to_calendar_hierarchy():
    # A continuous month truncation (green `tmn:` pill) on the ACTIVE business date is a display-grain
    # axis -> the marked Date table's Calendar drill hierarchy, drilled to the truncation grain
    # (Year + Month). This matches a Desktop-authored rebuild whose area/line date axis carries the
    # Calendar Year + Month levels (never the flat fact date column, never an undrillable key column).
    tmonth = ("<column-instance column='[Order Date]' derivation='Month-Trunc' "
              "name='[tmn:Order Date:qk]' pivot='key' type='quantitative' />")
    ws = _worksheet("Monthly Trend", "Line",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[tmn:Order Date:qk]",
                    deps_extra=_INST + tmonth)
    ir = parse_twb(_workbook(ws), date_binding=_DATE_BINDING)
    col = ir["worksheets"][0]["cols"][0]
    assert col["entity"] == "Date"
    assert col["hierarchy"] == {"name": "Calendar", "levels": ["Year", "Month"]}
    # rebound to the calendar -> the "grain not applied" degrade warning is gone
    assert not any("grain not applied" in x["reason"].lower() for x in ir["warnings"])
    # emits one HierarchyLevel projection per level (Year + Month), each active, against Date.Calendar
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    cats = _query_state(vis)["Category"]["projections"]
    assert [(p["field"]["HierarchyLevel"]["Level"], p["queryRef"], p.get("active")) for p in cats] == [
        ("Year", "Date.Calendar.Year", True), ("Month", "Date.Calendar.Month", True)]
    hexpr = cats[0]["field"]["HierarchyLevel"]["Expression"]["Hierarchy"]
    assert hexpr["Hierarchy"] == "Calendar"
    assert hexpr["Expression"]["SourceRef"]["Entity"] == "Date"
    assert cats[0]["nativeQueryRef"] == "Calendar Year"


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


def test_shape_map_from_geo_detail_and_color_measure():
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Sales:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    ir = parse_twb(_workbook(_geo_ws("Sales by State", "Automatic", enc)))
    assert ir["worksheets"][0]["visual_type"] == "shape_map"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "shapeMap"
    state = _query_state(vis)
    # Category = the geographic dimension; the colour-saturation measure binds the "Value" role
    # (the PBIR well behind shapeMap "Color saturation"), so the choropleth shades by the measure.
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "State"
    assert state["Value"]["projections"][0]["field"]["Aggregation"]["Function"] == 0
    assert "Tooltips" not in state  # the measure is the colour driver, not a redundant tooltip copy
    # generated lat/lon are dropped quietly, not bound as fields
    assert "no model binding" not in json.dumps(ir["warnings"])


def test_shape_map_measure_binds_value_color_saturation_well():
    # A Tableau measure on the Color shelf of a filled map is the choropleth's saturation driver.
    # The faithful Power BI home is the shapeMap "Value" role (its "Color saturation" well), NOT
    # Tooltips/Gradient -- so Power BI actually shades each state by the measure with its default ramp.
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Profit:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    ir = parse_twb(_workbook(_geo_ws("Profit by State", "Automatic", enc)))
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "shapeMap"
    state = _query_state(vis)
    # the measure lands on Value (colour saturation), and ONLY there -- no redundant Tooltips copy
    assert "Value" in state
    assert "Tooltips" not in state
    assert "Gradient" not in state
    assert state["Value"]["projections"][0]["field"]["Aggregation"]["Function"] == 0  # Sum(Profit)
    # the geo dimension is still the Location/Category at the finest level
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "State"


def test_shape_map_objects_pin_usa_states_topo_built_in_map():
    # A state-grain choropleth emits the objects.shape block that pins Power BI's built-in
    # "usa.states.topo" SHARED map (PackageType 2) + the albersUsa projection, so the shapeMap
    # renders OFFLINE with no bundled TopoJSON. Shape verified against real Desktop shapeMap JSON.
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Profit:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    ir = parse_twb(_workbook(_geo_ws("Profit by State", "Automatic", enc)))
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "shapeMap"
    shape = vis["visual"]["objects"]["shape"][0]["properties"]
    geo = shape["map"]["geoJson"]
    assert geo["type"]["expr"]["Literal"]["Value"] == "'shared'"
    assert geo["name"]["expr"]["Literal"]["Value"] == "'usa.states.topo'"
    rpi = geo["content"]["expr"]["ResourcePackageItem"]
    assert rpi == {"PackageName": "SharedResources", "PackageType": 2,
                   "ItemName": "usa.states.topo"}
    assert shape["projectionEnum"]["expr"]["Literal"]["Value"] == "'albersUsa'"


def test_shape_map_objects_emit_diverging_saturation_gradient_centred_at_zero():
    # A measure shapeMap must carry an explicit objects.dataPoint colour-saturation gradient or
    # Desktop renders a FLAT fill until the Value field is nudged off-and-on. We emit a DIVERGING
    # linearGradient3 -- orange (loss) -> white (0) -> blue (high profit) -- matching Tableau's
    # Orange-Blue map palette. Power BI does NOT default the centre to 0 (it auto-centres on the
    # data midpoint), so we PIN the mid stop's value to 0; white then lands on break-even with the
    # min/max stops left to auto-scale. Verified against a real Desktop filledMap/shapeMap visual.json.
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Profit:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    ir = parse_twb(_workbook(_geo_ws("Profit by State", "Automatic", enc)))
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "shapeMap"
    dp = vis["visual"]["objects"]["dataPoint"][0]["properties"]
    grad = dp["fillRule"]["linearGradient3"]
    # orange -> white -> blue, white PINNED at the 0 centre (mid carries a value anchor; min/max
    # stay value-less so they auto-scale to the data low/high)
    assert grad["min"]["color"]["expr"]["Literal"]["Value"] == "'#FEA043'"
    assert grad["mid"]["color"]["expr"]["Literal"]["Value"] == "'#FFFFFF'"
    assert grad["max"]["color"]["expr"]["Literal"]["Value"] == "'#4A88C2'"
    assert grad["mid"]["value"]["expr"]["Literal"]["Value"] == "0D"   # centre pinned at break-even
    assert "value" not in grad["min"]                                 # endpoints auto-scale...
    assert "value" not in grad["max"]                                 # ...to the data range
    assert grad["nullColoringStrategy"]["strategy"]["expr"]["Literal"]["Value"] == "'asZero'"
    assert dp["showAllDataPoints"]["expr"]["Literal"]["Value"] == "true"
    assert "linearGradient2" not in dp["fillRule"]  # not the old sequential 2-colour ramp


# Country/Region declared with its own geo semantic-role, so both it AND State carry a geo_area and
# both land on the Detail (lod) shelf -- the exact Superstore Sheet-3 shape (a Country -> State drill).
_CI_COUNTRY_DECL = ("<column caption='Country/Region' datatype='string' name='[Country/Region]' "
                    "role='dimension' semantic-role='[Country].[ISO3166_2]' type='nominal' />")
_CI_COUNTRY = ("<column-instance column='[Country/Region]' derivation='None' "
               "name='[none:Country/Region:nk]' pivot='key' type='nominal' />")


def test_shape_map_binds_finest_geo_level_not_coarsest():
    # Tableau serialises a geo drill hierarchy coarse->fine: Country/Region BEFORE State/Province.
    # The map renders at the FINEST level (each state is its own fill), so the faithful Power BI
    # Location is State, not the first-serialised Country. Without finest-geo selection the old
    # first-wins logic would (wrongly) bind Country here -- this locks the fix.
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Profit:qk]' />"
           "<lod column='[federated.abc].[none:Country/Region:nk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    ws = _worksheet("Profit by State", "Automatic",
                    rows="[federated.abc].[Latitude (generated)]",
                    cols="[federated.abc].[Longitude (generated)]",
                    deps_extra=_INST + _CI_COUNTRY_DECL + _CI_COUNTRY, encodings=enc)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "shape_map"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "shapeMap"
    state = _query_state(vis)
    cat = [p["field"]["Column"]["Property"] for p in state["Category"]["projections"]]
    assert cat == ["State"]   # finest level only, NOT the coarser Country/Region
    assert state["Value"]["projections"][0]["field"]["Aggregation"]["Function"] == 0


def test_shape_map_explicit_map_mark_needs_no_latlon_signal():
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Profit:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    # explicit Map mark is self-signaling: no generated lat/lon on the (empty) axes
    ir = parse_twb(_workbook(_geo_ws("Profit Map", "Map", enc, rows="", cols="")))
    assert ir["worksheets"][0]["visual_type"] == "shape_map"
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
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "State"
    assert state["Size"]["projections"][0]["field"]["Aggregation"]["Function"] == 0


def test_symbol_map_color_measure_binds_gradient_not_color_well():
    # A distinct colour MEASURE on a symbol/bubble map binds the PBIR "Gradient" role -- the Bing
    # map "Color saturation" well (a measure), the SAME role the filled map uses. The classic map
    # visual has NO "Color" role, so a colour measure must land on Gradient to shade the bubbles.
    enc = ("<encodings>"
           "<size column='[federated.abc].[sum:Sales:qk]' />"
           "<color column='[federated.abc].[sum:Profit:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    ir = parse_twb(_workbook(_geo_ws("Bubble Map", "Circle", enc)))
    assert ir["worksheets"][0]["visual_type"] == "map"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "map"
    state = _query_state(vis)
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "State"
    # size keeps its own measure; the distinct colour measure shades via Gradient, never "Color"
    assert state["Size"]["projections"][0]["queryRef"] == "Sum(Orders.Sales_Amount)"
    assert state["Gradient"]["projections"][0]["queryRef"] == "Sum(Orders.Profit)"
    assert "Color" not in state
    enc = ("<encodings>"
           "<color column='[federated.abc].[sum:Sales:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "<geometry column='[federated.abc].[Geometry (generated)]' />"
           "</encodings>")
    ir = parse_twb(_workbook(_geo_ws("Spatial", "Multipolygon", enc)))
    assert ir["worksheets"][0]["visual_type"] == "unsupported"
    assert _visual_parts(emit_pbir(ir)) == {}
    assert any("deferred" in w["reason"] and "Spatial" == w["name"] for w in ir["warnings"])


def test_filled_map_categorical_color_binds_series_legend():
    # A categorical (dimension) colour on a filled map is the map LEGEND -> the "Series" role
    # (each area shaded by its legend member), NOT the Gradient saturation well (that is only for
    # a colour MEASURE). Geo stays on Category at the finest level; no measure -> no Gradient.
    enc = ("<encodings>"
           "<color column='[federated.abc].[none:Region:nk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    ir = parse_twb(_workbook(_geo_ws("Region Map", "Automatic", enc, rows="", cols="")))
    assert ir["worksheets"][0]["visual_type"] == "filled_map"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "filledMap"
    state = _query_state(vis)
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "State"
    assert state["Series"]["projections"][0]["field"]["Column"]["Property"] == "Region"
    assert "Gradient" not in state   # a dimension colour is a legend, not a saturation measure


def test_symbol_map_categorical_color_binds_series_legend():
    # A categorical (dimension) colour on a symbol/bubble map binds the "Series" legend role
    # (bubbles coloured by member), distinct from a colour MEASURE (which would bind Gradient).
    enc = ("<encodings>"
           "<size column='[federated.abc].[sum:Sales:qk]' />"
           "<color column='[federated.abc].[none:Region:nk]' />"
           "<lod column='[federated.abc].[none:State:nk]' />"
           "</encodings>")
    ir = parse_twb(_workbook(_geo_ws("Bubble Legend Map", "Circle", enc)))
    assert ir["worksheets"][0]["visual_type"] == "map"
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "map"
    state = _query_state(vis)
    assert state["Category"]["projections"][0]["field"]["Column"]["Property"] == "State"
    assert state["Size"]["projections"][0]["field"]["Aggregation"]["Function"] == 0
    assert state["Series"]["projections"][0]["field"]["Column"]["Property"] == "Region"
    assert "Gradient" not in state


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


def test_norm_param_key_strips_brackets_space_and_casefolds():
    # The model build keys param_binding slicers WITH brackets ([Parameter 001...]); a dashboard
    # paramctrl zone yields the bracket-stripped id. Normalization must bridge the two forms.
    assert _norm_param_key("[Parameter 0014172372426784]") == "parameter 0014172372426784"
    assert _norm_param_key("Parameter 0014172372426784") == "parameter 0014172372426784"
    assert _norm_param_key("  [ParaM 1] ") == "param 1"
    assert _norm_param_key(None) == ""


def test_parameter_control_resolved_by_param_binding_emits_slicer_and_clears_warning():
    # When the model build identifies the parameter's target column (param_binding["slicers"]), the
    # dashboard parameter control is rebuilt as a single-select slicer at its OWN zone and its
    # "not rebuilt as a slicer yet" warning is cleared -- even though the model keys the slicer WITH
    # brackets ([Parameter 1]) while the control id is bracket-stripped (Parameter 1).
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]", deps_extra=_INST)
    dash = ("<dashboard name='Dash'><zones>"
            "<zone h='100000' w='100000' x='0' y='0'>"
            "<zone h='90000' w='60000' x='0' y='0' name='Sales by Category' id='2' />"
            + _paramctrl_zone("Parameter 1") +
            "</zone></zones></dashboard>")
    pb = {"slicers": {"[Parameter 1]": {"table": "Orders", "column": "Segment",
                                        "single_select": True, "caption": "view swap"}},
          "flags": {}}
    ir = parse_twb(_workbook_with_params(ws, dash), param_binding=pb)

    rec = ir["parameter_controls"][0]
    assert rec["resolved"] == {"table": "Orders", "column": "Segment",
                               "single_select": True, "caption": "view swap"}
    # the standing per-control warning is cleared for a resolved control (no longer "not rebuilt")
    assert not [w for w in ir["warnings"] if "parameter control 'view swap'" in w["reason"]]

    parts = _visual_parts(emit_pbir(ir))
    slicers = [v for v in parts.values() if v["visual"]["visualType"] == "slicer"]
    assert len(slicers) == 1
    proj = slicers[0]["visual"]["query"]["queryState"]["Values"]["projections"][0]
    assert proj["queryRef"] == "Orders.Segment"


def test_parameter_control_not_in_binding_still_warns_and_emits_no_slicer():
    # A control the model did NOT resolve keeps its honest warning and is not rebuilt (warn-never-wrong):
    # an unrelated slicer binding must never bind a different control.
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]", deps_extra=_INST)
    dash = ("<dashboard name='Dash'><zones>"
            "<zone h='100000' w='100000' x='0' y='0'>"
            "<zone h='90000' w='60000' x='0' y='0' name='Sales by Category' id='2' />"
            + _paramctrl_zone("Parameter 1") +
            "</zone></zones></dashboard>")
    pb = {"slicers": {"[Parameter 9999]": {"table": "Orders", "column": "Region",
                                           "single_select": True}}, "flags": {}}
    ir = parse_twb(_workbook_with_params(ws, dash), param_binding=pb)

    assert "resolved" not in ir["parameter_controls"][0]
    assert [w for w in ir["warnings"] if "parameter control 'view swap'" in w["reason"]]
    parts = _visual_parts(emit_pbir(ir))
    assert [v for v in parts.values() if v["visual"]["visualType"] == "slicer"] == []


def test_migrate_twb_to_pbir_accepts_param_binding_and_places_slicer_in_dashboard_zone():
    # End-to-end through the one-call entry: a resolved control's slicer lands on the dashboard page
    # scaled into the page frame (so it sits where the Tableau control was, not off-canvas).
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]", deps_extra=_INST)
    dash = ("<dashboard name='Dash'><zones>"
            "<zone h='100000' w='100000' x='0' y='0'>"
            "<zone h='90000' w='60000' x='0' y='0' name='Sales by Category' id='2' />"
            + _paramctrl_zone("Parameter 1", x=80000, y=10000) +
            "</zone></zones></dashboard>")
    pb = {"slicers": {"[Parameter 1]": {"table": "Orders", "column": "Segment",
                                        "single_select": True, "caption": "view swap"}}}
    res = migrate_twb_to_pbir(_workbook_with_params(ws, dash), param_binding=pb)
    slicer_parts = [k for k in res["parts"] if "/paramslicer-" in k]
    assert len(slicer_parts) == 1
    v = json.loads(res["parts"][slicer_parts[0]])
    assert v["visual"]["visualType"] == "slicer"
    # placed within the page frame (x scaled from the ~0.8 fractional zone position)
    pos = v["position"]
    assert 0 <= pos["x"] <= PAGE_WIDTH and 0 <= pos["y"] <= PAGE_HEIGHT
    assert pos["x"] > PAGE_WIDTH / 2  # the control was on the right (x=80000 of 100000)


# -- model keep-flag -> visual-level measure filter (param_binding["flags"]) ---
# A translated parameter-driven keep calc (e.g. a relative-date window selector) comes back from the
# model build as a measure in ``param_binding["flags"][<token>] = {entity, measure, value, visuals}``;
# each scoped worksheet's rebuilt visual then carries a visual-level ``[measure] == value`` measure
# filter so it opens on the SAME windowed rows, and the now-obsolete parse-time "aggregate/measure
# filter on '<token>'" warning is dropped for that worksheet. Warn-never-wrong governs the edges: a
# non-numeric value, an empty/absent scope, or a worksheet the workbook lacks leaves the filter
# UNAPPLIED with a warning -- never applied to a guessed set of visuals.
_DATE_FILTER_CALC = (
    "<column caption='Date Filter' datatype='boolean' name='[DateFilterCalc]' "
    "role='measure' type='quantitative'>"
    "<calculation class='tableau' formula='IF LAST()&lt;=15 THEN 1 END' />"
    "</column>"
    "<column-instance column='[DateFilterCalc]' derivation='None' "
    "name='[none:DateFilterCalc:qk]' pivot='key' type='quantitative' />")
_DATE_FILTER_FILT = ("<filter class='quantitative' "
                     "column='[federated.abc].[none:DateFilterCalc:qk]' />")


def _flag_pb(visuals, *, measure="Date Filter", entity="_Measures", value=1, token="Date Filter"):
    return {"flags": {token: {"entity": entity, "measure": measure,
                              "status": "translated", "value": value, "visuals": visuals}}}


def _flagged_line_worksheet(name="Line chart"):
    # a faithful line chart that ALSO carries an aggregate/measure filter on a "Date Filter" calc --
    # so the bare parse warns "aggregate/measure filter on 'Date Filter'" and a flag can supersede it.
    return _worksheet(name, "Line",
                      rows="[federated.abc].[sum:Sales:qk]",
                      cols="[federated.abc].[mn:Order Date:ok]",
                      deps_extra=_INST + _DATE_FILTER_CALC, filters=_DATE_FILTER_FILT)


def _measure_filter_containers(parts):
    out = []
    for v in _visual_parts(parts).values():
        for cont in (v.get("filterConfig") or {}).get("filters", []):
            if "Measure" in cont.get("field", {}):
                out.append(cont)
    return out


def test_flag_filter_container_is_advanced_measure_equals_filter():
    cont = _flag_filter_container("_Measures", "Date Filter", "1L", "flag-x")
    # the top-level field references the measure by its home Entity ...
    assert cont["field"]["Measure"]["Property"] == "Date Filter"
    assert cont["field"]["Measure"]["Expression"]["SourceRef"] == {"Entity": "_Measures"}
    assert cont["type"] == "Advanced"
    assert cont["howCreated"] == "User"
    assert cont["filter"]["From"][0] == {"Name": "f", "Entity": "_Measures", "Type": 0}
    cmp_ = cont["filter"]["Where"][0]["Condition"]["Comparison"]
    assert cmp_["ComparisonKind"] == 0  # Equal
    # ... but the Where comparison reaches it through the From SOURCE alias, never the Entity (an
    # Entity inside Where is a silent filter failure)
    assert cmp_["Left"]["Measure"]["Expression"]["SourceRef"] == {"Source": "f"}
    assert cmp_["Left"]["Measure"]["Property"] == "Date Filter"
    assert cmp_["Right"]["Literal"]["Value"] == "1L"


def test_resolve_visual_flags_maps_each_scoped_worksheet():
    warnings = []
    by_ws = _resolve_visual_flags(_flag_pb(["A", "B"]), {"A": {}, "B": {}}, warnings)
    assert set(by_ws) == {"A", "B"}
    assert len(by_ws["A"]) == 1 and len(by_ws["B"]) == 1
    assert by_ws["A"][0]["field"]["Measure"]["Property"] == "Date Filter"
    lit = by_ws["A"][0]["filter"]["Where"][0]["Condition"]["Comparison"]["Right"]["Literal"]["Value"]
    assert lit == "1L"
    assert warnings == []


def test_resolve_visual_flags_decimal_value_emits_double_literal():
    by_ws = _resolve_visual_flags(_flag_pb(["A"], value=1.5), {"A": {}}, [])
    lit = by_ws["A"][0]["filter"]["Where"][0]["Condition"]["Comparison"]["Right"]["Literal"]["Value"]
    assert lit == "1.5D"


def test_resolve_visual_flags_non_numeric_value_warns_and_skips():
    warnings = []
    by_ws = _resolve_visual_flags(_flag_pb(["A"], value="abc"), {"A": {}}, warnings)
    assert by_ws == {}
    assert any("non-numeric keep-value" in w["reason"] for w in warnings)


def test_resolve_visual_flags_empty_visuals_warns_and_skips():
    warnings = []
    by_ws = _resolve_visual_flags(_flag_pb([]), {"A": {}}, warnings)
    assert by_ws == {}
    assert any("no worksheet scope" in w["reason"] for w in warnings)


def test_resolve_visual_flags_unknown_worksheet_warns_and_skips():
    warnings = []
    by_ws = _resolve_visual_flags(_flag_pb(["Ghost"]), {"A": {}}, warnings)
    assert by_ws == {}
    assert any("not in the workbook" in w["reason"] for w in warnings)


def test_drop_resolved_flag_warnings_prunes_only_matching_worksheet_token():
    warnings = [
        {"scope": "worksheet", "name": "Line chart",
         "reason": "manual attention required: aggregate/measure filter on 'Date Filter' (...)"},
        {"scope": "worksheet", "name": "Other",
         "reason": "manual attention required: aggregate/measure filter on 'Date Filter' (...)"},
        {"scope": "worksheet", "name": "Line chart",
         "reason": "manual attention required: date part 'Month' on 'Order Date' (...)"},
    ]
    _drop_resolved_flag_warnings(warnings, [("Line chart", "Date Filter")])
    assert len(warnings) == 2  # only the (Line chart, Date Filter) aggregate warning is removed
    tags = {(w["name"], "aggregate" in w["reason"], "date part" in w["reason"]) for w in warnings}
    assert ("Other", True, False) in tags          # a different worksheet is untouched
    assert ("Line chart", False, True) in tags      # an unrelated warning on the same ws survives


def test_param_binding_flag_applies_measure_filter_to_scoped_visual():
    ws = _flagged_line_worksheet("Line chart")
    ir = parse_twb(_workbook(ws), param_binding=_flag_pb(["Line chart"]))
    assert len(ir["visual_flags"]["Line chart"]) == 1

    conts = _measure_filter_containers(emit_pbir(ir))
    assert len(conts) == 1
    cont = conts[0]
    assert cont["type"] == "Advanced"
    assert cont["field"]["Measure"]["Property"] == "Date Filter"
    cmp_ = cont["filter"]["Where"][0]["Condition"]["Comparison"]
    assert cmp_["ComparisonKind"] == 0
    assert cmp_["Left"]["Measure"]["Expression"]["SourceRef"] == {"Source": "f"}
    assert cmp_["Right"]["Literal"]["Value"] == "1L"


def test_param_binding_flag_clears_aggregate_measure_filter_warning():
    ws = _flagged_line_worksheet("Line chart")
    # without the flag the worksheet keeps its honest "not mapped to a slicer" warning ...
    bare = parse_twb(_workbook(ws))
    assert any("aggregate/measure filter on 'Date Filter'" in w["reason"] for w in bare["warnings"])
    # ... and the model keep-flag supersedes it (the visual filters on the measure instead)
    ir = parse_twb(_workbook(ws), param_binding=_flag_pb(["Line chart"]))
    assert not any("aggregate/measure filter on 'Date Filter'" in w["reason"]
                   for w in ir["warnings"])


def test_param_binding_flag_scoped_to_absent_worksheet_applies_nothing():
    ws = _flagged_line_worksheet("Line chart")
    ir = parse_twb(_workbook(ws), param_binding=_flag_pb(["Ghost sheet"]))
    assert ir["visual_flags"] == {}
    assert any("not in the workbook" in w["reason"] for w in ir["warnings"])
    # warn-never-wrong: the real visual gets NO measure filter (the scope is never guessed) ...
    assert _measure_filter_containers(emit_pbir(ir)) == []
    # ... and the worksheet keeps its own honest aggregate-filter warning
    assert any("aggregate/measure filter on 'Date Filter'" in w["reason"] for w in ir["warnings"])


def test_no_flags_leaves_visuals_and_records_untouched():
    ws = _worksheet("Plain", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    res = migrate_twb_to_pbir(_workbook(ws))
    assert res["ir"]["visual_flags"] == {}
    assert _measure_filter_containers(res["parts"]) == []
    assert all("flag_filters" not in r for r in res["candidate_records"])


def test_migrate_twb_to_pbir_flag_filter_lands_on_part_and_candidate_record():
    ws = _flagged_line_worksheet("Line chart")
    res = migrate_twb_to_pbir(_workbook(ws), param_binding=_flag_pb(["Line chart"]))
    # the emitted visual part carries the measure filter ...
    conts = _measure_filter_containers(res["parts"])
    assert len(conts) == 1
    assert conts[0]["field"]["Measure"]["Property"] == "Date Filter"
    # ... and the candidate record names the applied keep-flag measure (additive fact)
    rec = [r for r in res["candidate_records"] if r["worksheet"] == "Line chart"][0]
    assert rec["flag_filters"] == ["Date Filter"]


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


# -- "Grow to fit" column auto-size: the table/matrix column-width DEFAULT --------------------------
def _auto_size_value(vis):
    """Pull columnHeaders[0].autoSizeColumnWidth's literal ('true'/None) from a built visual."""
    ch = (vis["visual"].get("objects") or {}).get("columnHeaders")
    if not ch:
        return None
    return (ch[0].get("properties", {}).get("autoSizeColumnWidth", {})
            .get("expr", {}).get("Literal", {}).get("Value"))


def _col_adjustment_value(vis):
    """Pull columnHeaders[0].columnAdjustment's literal ("'growToFit'"/None) from a built visual."""
    ch = (vis["visual"].get("objects") or {}).get("columnHeaders")
    if not ch:
        return None
    return (ch[0].get("properties", {}).get("columnAdjustment", {})
            .get("expr", {}).get("Literal", {}).get("Value"))


def test_grid_visuals_default_to_grow_to_fit():
    # Every rebuilt table (tableEx) and matrix (pivotTable) opens "Grow to fit": columnAdjustment is
    # the enum the modern "Auto-size behavior" dropdown reads (autoSizeColumnWidth alone resolves to
    # "Fit to content"); the boolean rides along so Power BI never falls back to fixed "Custom" widths.
    for vtype in ("tableEx", "pivotTable"):
        vis = _visual_json("g", vtype, {"x": 0, "y": 0}, {"Values": {"projections": []}})
        assert _col_adjustment_value(vis) == "'growToFit'"
        assert _auto_size_value(vis) == "true"


def test_grid_visuals_emit_no_custom_width_selectors():
    # The per-column columnWidth[] "Custom widths" scaffolding is deliberately NOT emitted -- adding
    # it (even empty) is what flips a grid toward fixed widths.
    for vtype in ("tableEx", "pivotTable"):
        vis = _visual_json("g", vtype, {"x": 0, "y": 0}, {"Values": {"projections": []}})
        assert "columnWidth" not in (vis["visual"].get("objects") or {})


def test_non_grid_visuals_have_no_column_headers_object():
    # Grow-to-fit is a table/matrix-only control; a bar/line/pie/card/slicer stays byte-unchanged
    # (no columnHeaders object at all) -- neither the columnAdjustment enum nor the boolean.
    for vtype in ("clusteredColumnChart", "lineChart", "pieChart", "card", "slicer"):
        vis = _visual_json("n", vtype, {"x": 0, "y": 0}, {"Category": {}})
        assert "columnHeaders" not in (vis["visual"].get("objects") or {})
        assert _col_adjustment_value(vis) is None
        assert _auto_size_value(vis) is None


def test_grow_to_fit_is_merge_safe_with_a_background_gradient():
    # A table that also carries a values background gradient keeps it AND gains grow-to-fit.
    vis = _visual_json("g", "tableEx", {"x": 0, "y": 0}, {"Values": {"projections": []}},
                       value_objects=[{"properties": {"__grad": 1}}])
    assert "values" in vis["visual"]["objects"]
    assert _auto_size_value(vis) == "true"


def test_field_parameter_table_defaults_to_grow_to_fit():
    # The self-service field-parameter table is a real grid the user sees -- same default, no
    # custom-width selectors.
    vis = field_parameter_table_visual(
        "fp", _fp_specs(), {"x": 0, "y": 0, "width": 100, "height": 100})
    assert _auto_size_value(vis) == "true"
    assert "columnWidth" not in (vis["visual"].get("objects") or {})


def test_grow_to_fit_preserves_existing_column_headers_props():
    # setdefault twice: a columnHeaders object a later formatting pass added (header font/colour)
    # keeps its props and merely GAINS autoSizeColumnWidth.
    vis = {"visualType": "tableEx",
           "objects": {"columnHeaders": [{"properties": {"fontColor": "kept"}}]}}
    _apply_grow_to_fit(vis, "tableEx")
    props = vis["objects"]["columnHeaders"][0]["properties"]
    assert props["fontColor"] == "kept"
    assert props["autoSizeColumnWidth"]["expr"]["Literal"]["Value"] == "true"


def test_grow_to_fit_does_not_overwrite_an_explicit_auto_size():
    # A future Tier-2 pass pinning fixed widths (autoSizeColumnWidth=false) is respected, not clobbered.
    vis = {"visualType": "pivotTable",
           "objects": {"columnHeaders": [{"properties": {
               "autoSizeColumnWidth": {"expr": {"Literal": {"Value": "false"}}}}}]}}
    _apply_grow_to_fit(vis, "pivotTable")
    assert (vis["objects"]["columnHeaders"][0]["properties"]
            ["autoSizeColumnWidth"]["expr"]["Literal"]["Value"] == "false")


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
    # A matrix now always carries a grow-to-fit columnHeaders object; it must still emit NO
    # axis-title objects (categoryAxis / valueAxis) -- the axis style-rule is not reproduced on a grid.
    objects = _only_visual(res)["visual"].get("objects", {})
    assert "categoryAxis" not in objects and "valueAxis" not in objects


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


# -- constant reference lines -> Power BI analytics y1AxisReferenceLine (additive, Tier-2 lift) ----
def _const_ref_line(value, label=None, label_type="none", extra_attrs="", child=""):
    """A Tableau ``formula='constant'`` reference line at a fixed numeric ``value`` (the shape a user
    gets by dropping a constant line on a value axis)."""
    lbl = f"label='{label}' " if label else ""
    head = (f"<reference-line {lbl}label-type='{label_type}' formula='constant' "
            f"value='{value}' scope='per-pane' {extra_attrs}")
    return f"{head}>{child}</reference-line>" if child else f"{head}/>"


def _visual_objects_of(res):
    return _only_visual(res)["visual"].get("objects", {})


def test_constant_reference_line_emits_y1_axis_object():
    # a constant target on a value-axis column chart is faithfully rebuilt as a Power BI analytics
    # reference line (y1AxisReferenceLine) carrying the value + custom caption; no Tier-2 defer.
    ref = _const_ref_line(50000, label="Target", label_type="custom")
    ws = _worksheet("Sales vs Target", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, pane_extra=ref)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["visual_type"] == "column"
    assert w["reference_line_constants"] == [{"value": 50000.0, "display_name": "Target"}]
    # the emittable constant does NOT trigger the deferral warning
    assert not any(x["name"] == "Sales vs Target" and "deferred (Tier-2 analytics)" in x["reason"]
                   for x in parse_twb(_workbook(ws))["warnings"])
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    rl = _visual_objects_of(res)["y1AxisReferenceLine"]
    assert len(rl) == 1
    props = rl[0]["properties"]
    assert props["value"]["expr"]["Literal"]["Value"] == "50000D"
    assert props["displayName"]["expr"]["Literal"]["Value"] == "'Target'"
    assert props["show"]["expr"]["Literal"]["Value"] == "true"
    assert rl[0]["selector"]["id"] == "Ref0"


def test_constant_reference_line_without_custom_label_omits_display_name():
    # value-only constant (automatic/none label) -> line with a value but no displayName override.
    ref = _const_ref_line(0, label_type="none")
    ws = _worksheet("Break-even", "Line",
                    rows="[federated.abc].[sum:Profit:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, pane_extra=ref)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["reference_line_constants"] == [{"value": 0.0, "display_name": None}]
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    rl = _visual_objects_of(res)["y1AxisReferenceLine"]
    assert len(rl) == 1
    assert rl[0]["properties"]["value"]["expr"]["Literal"]["Value"] == "0D"
    assert "displayName" not in rl[0]["properties"]


def test_computed_reference_line_defers_and_emits_no_axis_object():
    # a computed (average) line has no fixed value to place -> stays a Tier-2 defer, and NO
    # approximate analytics object is emitted (warn-never-wrong).
    ref = _ref_line("[federated.abc].[sum:Profit:qk]", formula="average")
    ws = _worksheet("Sales by Category", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, pane_extra=ref)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["reference_line_constants"] == []
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    assert "y1AxisReferenceLine" not in _visual_objects_of(res)
    assert any(x["name"] == "Sales by Category" and "deferred (Tier-2 analytics)" in x["reason"]
               and "average of Profit" in x["reason"]
               for x in res["ir"]["warnings"])


def test_percentage_band_reference_line_defers():
    # a percentage-band distribution is not a single constant line -> defer, no analytics object.
    ref = _const_ref_line(25, extra_attrs="percentage-bands='true'",
                          child="<reference-line-value percentage='60' />")
    ws = _worksheet("Banded", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, pane_extra=ref)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["reference_line_constants"] == []
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    assert "y1AxisReferenceLine" not in _visual_objects_of(res)
    assert any(x["name"] == "Banded" and "deferred (Tier-2 analytics)" in x["reason"]
               for x in res["ir"]["warnings"])


def test_constant_reference_line_on_horizontal_bar_defers():
    # on a horizontal bar the measure axis is X (ambiguous for a y1 line) -> defer rather than risk
    # drawing the line on the wrong axis.
    ref = _const_ref_line(50000, label="Target", label_type="custom")
    ws = _worksheet("Sales by Cat H", "Bar",
                    rows="[federated.abc].[none:Category:nk]",
                    cols="[federated.abc].[sum:Sales:qk]",
                    deps_extra=_INST, pane_extra=ref)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["visual_type"] == "bar"
    assert w["reference_line_constants"] == []
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    assert "y1AxisReferenceLine" not in _visual_objects_of(res)
    assert any(x["name"] == "Sales by Cat H" and "deferred (Tier-2 analytics)" in x["reason"]
               for x in res["ir"]["warnings"])


def test_mixed_constant_and_computed_reference_lines():
    # a column chart with BOTH a constant target and an average line: the constant is rebuilt, the
    # average is deferred, and the warning names only the dropped (computed) line.
    ref = (_const_ref_line(50000, label="Target", label_type="custom")
           + _ref_line("[federated.abc].[sum:Profit:qk]", formula="average"))
    ws = _worksheet("Sales mixed", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, pane_extra=ref)
    w = parse_twb(_workbook(ws))["worksheets"][0]
    assert w["reference_line_constants"] == [{"value": 50000.0, "display_name": "Target"}]
    res = migrate_twb_to_pbir(_workbook(ws), dataset_name="M", report_name="R")
    rl = _visual_objects_of(res)["y1AxisReferenceLine"]
    assert len(rl) == 1
    assert rl[0]["properties"]["value"]["expr"]["Literal"]["Value"] == "50000D"
    warn = [x for x in res["ir"]["warnings"]
            if x["name"] == "Sales mixed" and "deferred (Tier-2 analytics)" in x["reason"]]
    assert len(warn) == 1
    assert "average of Profit" in warn[0]["reason"]
    assert "Target" not in warn[0]["reason"]


def test_measure_values_no_dimension_is_table():
    # A Measure Values text table with NO real dimension is Tableau's "measure table" (the measure
    # names listed down the side with their values) -> a faithful tableEx of the member measures,
    # NOT a multiRowCard of standalone value tiles. The implicit Measure Names pill is never bound
    # (Power BI's column headers are the measure names); the member measures fill the Values well.
    ws = _worksheet("KPIs", "Text",
                    rows="[federated.abc].[Multiple Values]",
                    cols="",
                    deps_extra=_INST,
                    filters=_mv_filter(["sum:Sales:qk", "sum:Profit:qk"]))
    ir = parse_twb(_workbook(ws))
    w = ir["worksheets"][0]
    assert w["visual_type"] == "table"
    assert w["fidelity_note"] and "implicit" in w["fidelity_note"].lower()
    vis = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert vis["visual"]["visualType"] == "tableEx"
    vrefs = [p["queryRef"] for p in _query_state(vis)["Values"]["projections"]]
    assert vrefs == ["Sum(Orders.Sales_Amount)", "Sum(Orders.Profit)"]


def test_measure_values_no_dimension_names_on_cols_is_card():
    # A Measure Values "BAN strip": Measure Names on COLUMNS with the values shown as Text marks
    # (each measure its own labelled big number across a horizontal band) is Tableau's KPI tile row
    # -> a multiRowCard (Power BI's native row of labelled big numbers), NOT a single-column tableEx.
    # The implicit Measure Names pill stays unbound; the member measures fill the Values well in order.
    ws = _worksheet("KPIs", "Text",
                    rows="",
                    cols="[federated.abc].[:Measure Names]",
                    deps_extra=_INST,
                    encodings="<encodings><text column='[federated.abc].[Multiple Values]' /></encodings>",
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
        "Golden ShapeMap": ("Automatic", geo, geo2, fmap_enc, ""),
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
        "Golden ShapeMap": ("shapeMap",
                            {"Category": ["Orders.State"],
                             "Value": ["Sum(Orders.Sales_Amount)"]}),
        "Golden SymbolMap": ("map",
                             {"Category": ["Orders.State"], "Size": ["Sum(Orders.Sales_Amount)"]}),
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


def _values_backcolor(visual_json):
    # The conditional-format FILL only: a matrix/table now always carries an additive compact-grid
    # font on ``values`` (documented 9pt), so presence of a ``values`` object no longer implies a
    # fill. Assert on the ``backColor`` heat rule specifically.
    vo = _values_objects(visual_json)
    if not vo:
        return None
    return vo[0].get("properties", {}).get("backColor")


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
    # no conditional-format fill emitted on the visual (the additive compact-grid font may ride
    # ``values`` but no backColor heat rule)
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert _values_backcolor(vj) is None
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
    # Additivity: a plain highlight-table matrix (no <style> colour scale) carries no backColor
    # conditional-format fill (only the additive compact-grid font) and no conditional_format fact.
    enc = "<encodings><color column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    res = migrate_twb_to_pbir(_workbook(
        _heat_ws("Heat", color_field="sum:Sales:qk", encodings=enc, style="")))
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert _values_backcolor(vj) is None
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


# -- default (automatic) continuous colour ramp -- Tableau serialises NO <color-palette> -----------
# When the author keeps Tableau's default automatic colour ramp on a continuous measure, Tableau
# writes the colour encoding (``type='interpolated'``) but NO ``<color-palette>``. The exact colours
# are unrecoverable, so a standard ColorBrewer stand-in is SYNTHESISED (faithful direction) and
# DISCLOSED via a warning, rather than silently dropping the heat scale (the prior behaviour).
def _mark_color_style_no_palette(field_token, enc_type="interpolated", center=None):
    center_attr = f" center='{center}'" if center is not None else ""
    return (f"<style><style-rule element='mark'>"
            f"<encoding attr='color'{center_attr} type='{enc_type}' field='{field_token}'>"
            f"</encoding></style-rule></style>")


def test_default_continuous_palette_synthesised_when_no_explicit_stops():
    # A continuous (interpolated) colour encoding with NO <color-palette> -> a synthesised sequential
    # default gradient flagged ``default_palette`` (was ``None`` = a silent drop before the fix).
    style = _mark_color_style_no_palette("[federated.abc].[sum:Sales:qk]")
    enc = "<encodings><color column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    ir = parse_twb(_workbook(_heat_ws("Heat", color_field="sum:Sales:qk",
                                      encodings=enc, style=style)))
    cg = ir["worksheets"][0]["color_gradient"]
    assert cg is not None
    assert cg["default_palette"] is True
    assert cg["palette_type"] == "ordered-sequential"
    assert cg["center"] is None
    assert len(cg["colors"]) >= 2          # a usable min -> max ramp
    assert cg["interpolated"] is True
    assert cg["is_table_calc"] is False


def test_default_continuous_palette_emits_backcolor_and_discloses():
    # The synthesised default ramp emits a real backColor FillRule (linearGradient2) AND a disclosure
    # warning naming the default palette -- warn-never-wrong (emit the scale, flag the approximation).
    style = _mark_color_style_no_palette("[federated.abc].[sum:Sales:qk]")
    enc = "<encodings><color column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    res = migrate_twb_to_pbir(_workbook(
        _heat_ws("Heat", color_field="sum:Sales:qk", encodings=enc, style=style)))
    vj = list(_visual_parts(res["parts"]).values())[0]
    vo = _values_objects(vj)
    assert vo, "expected a default-palette conditional-format fill"
    assert "linearGradient2" in _fill_rule(vo)["FillRule"]
    fact = _cf_fact(res["candidate_records"], "Heat")
    assert fact["status"] == "emitted"
    assert fact["default_palette"] is True
    disclosures = [w for w in res["warnings"] if "default continuous palette" in w["reason"]]
    assert len(disclosures) == 1           # exactly one warning, no double-warn
    assert "sequential" in disclosures[0]["reason"]


def test_default_diverging_palette_when_center_present():
    # A continuous encoding with a ``center`` but no palette -> a synthesised DIVERGING default
    # (linearGradient3, centre pinned) with a matching disclosure.
    style = _mark_color_style_no_palette("[federated.abc].[sum:Profit:qk]", center="0.0")
    enc = "<encodings><color column='[federated.abc].[sum:Profit:qk]' /></encodings>"
    res = migrate_twb_to_pbir(_workbook(
        _heat_ws("Heat", color_field="sum:Profit:qk", encodings=enc, style=style)))
    vj = list(_visual_parts(res["parts"]).values())[0]
    vo = _values_objects(vj)
    assert vo, "expected a default diverging fill"
    grad = _fill_rule(vo)["FillRule"]["linearGradient3"]
    assert grad["mid"]["value"]["Literal"]["Value"] == "0.0D"   # centre pinned
    assert any("diverging" in w["reason"] and "default continuous palette" in w["reason"]
               for w in res["warnings"])


def test_explicit_palette_not_flagged_default_and_no_disclosure():
    # Guard: an EXPLICIT palette is byte-unchanged by the default-synthesis path -- no
    # ``default_palette`` flag on the IR or the fact, and no default-palette disclosure warning.
    style = _mark_color_style("[federated.abc].[sum:Sales:qk]", "ordered-sequential",
                              ["#f7fbff", "#08306b"])
    enc = "<encodings><color column='[federated.abc].[sum:Sales:qk]' /></encodings>"
    wb = _workbook(_heat_ws("Heat", color_field="sum:Sales:qk", encodings=enc, style=style))
    cg = parse_twb(wb)["worksheets"][0]["color_gradient"]
    assert "default_palette" not in cg
    res = migrate_twb_to_pbir(wb)
    fact = _cf_fact(res["candidate_records"], "Heat")
    assert fact["status"] == "emitted"
    assert "default_palette" not in fact
    assert not any("default continuous palette" in w["reason"] for w in res["warnings"])


# -- categorical mark colours (explicit author member -> hex palette) ----------
# An explicit per-member colour map (``<map to='#hex'><bucket>"Member"</bucket></map>``) is
# unambiguous author intent. On the discrete categorical charts (column / bar / pie / donut) it
# becomes a PBIR ``visual.objects.dataPoint`` per-member ``fill`` targeted by a ``scopeId`` data
# selector. WARN-NEVER-WRONG: a palette on an unsupported visual type, or whose coloured dimension
# is not projected, defers (no dataPoint, a warning, the raw palette kept on the candidate record).
def _palette_style(field_token, members):
    # members: list of (member_value, hex); a string bucket is wrapped in literal double quotes.
    maps = "".join(
        "<map to='{0}'><bucket>&quot;{1}&quot;</bucket></map>".format(hexv, val)
        for val, hexv in members)
    return ("<style><style-rule element='mark'>"
            "<encoding attr='color' type='palette' field='{0}'>{1}</encoding>"
            "</style-rule></style>".format(field_token, maps))


def _data_point_objects(visual_json):
    return visual_json["visual"].get("objects", {}).get("dataPoint")


def _mc_fact(records, worksheet):
    rec = next(r for r in records if r["worksheet"] == worksheet)
    return rec.get("mark_colors")


_REGION_PALETTE = [("Central", "#4e79a7"), ("West", "#76b7b2"), ("South", "#e15759")]


def _stacked_palette_ws(name="Stacked"):
    enc = "<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>"
    return _worksheet(name, "Bar",
                      rows="[federated.abc].[sum:Sales:qk]",
                      cols="[federated.abc].[none:Category:nk]",
                      deps_extra=_INST, encodings=enc,
                      style=_palette_style("[federated.abc].[none:Region:nk]", _REGION_PALETTE))


def test_categorical_palette_parsed_into_ir():
    ir = parse_twb(_workbook(_stacked_palette_ws()))
    mc = ir["worksheets"][0]["mark_colors"]
    assert mc is not None
    # author order is preserved; each member carries its explicit hex
    assert [(m["value"], m["color"]) for m in mc["members"]] == _REGION_PALETTE
    assert "Region" in mc["field_token"]


def test_categorical_palette_emits_datapoint_fills_with_scope_selectors():
    parts = emit_pbir(parse_twb(_workbook(_stacked_palette_ws())))
    vj = list(_visual_parts(parts).values())[0]
    assert vj["visual"]["visualType"] == "stackedColumnChart"
    dp = _data_point_objects(vj)
    assert dp and len(dp) == len(_REGION_PALETTE)
    series_field = _query_state(vj)["Series"]["projections"][0]["field"]
    for entry, (member, hexv) in zip(dp, _REGION_PALETTE):
        fill = entry["properties"]["fill"]["solid"]["color"]["expr"]["Literal"]["Value"]
        assert fill == "'{0}'".format(hexv)
        comp = entry["selector"]["data"][0]["scopeId"]["Comparison"]
        assert comp["ComparisonKind"] == 0                 # Equal
        assert comp["Left"] == series_field                # reuse the projected column expr
        assert comp["Right"]["Literal"]["Value"] == "'{0}'".format(member)


def test_categorical_palette_fact_recorded_emitted():
    res = migrate_twb_to_pbir(_workbook(_stacked_palette_ws()))
    fact = _mc_fact(res["candidate_records"], "Stacked")
    assert fact["status"] == "emitted"
    assert fact["kind"] == "categorical_palette"
    assert [(m["value"], m["color"]) for m in fact["members"]] == _REGION_PALETTE


def test_categorical_palette_on_line_defers_with_palette_preserved():
    # Line / area charts colour a continuous series; an explicit dataPoint override can drop the
    # line, so the palette defers (theme colours kept) with the raw palette preserved on the record.
    enc = "<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>"
    ws = _worksheet("Trend", "Line",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[mn:Order Date:ok]",
                    deps_extra=_INST, encodings=enc,
                    style=_palette_style("[federated.abc].[none:Region:nk]", _REGION_PALETTE))
    res = migrate_twb_to_pbir(_workbook(ws))
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert vj["visual"]["visualType"] == "lineChart"
    assert _data_point_objects(vj) is None
    fact = _mc_fact(res["candidate_records"], "Trend")
    assert fact["status"] == "deferred"
    assert [(m["value"], m["color"]) for m in fact["members"]] == _REGION_PALETTE
    assert any("categorical mark colours deferred" in w["reason"] for w in res["warnings"])


def test_categorical_palette_unprojected_dimension_defers():
    # The style palette names Region, but no colour pill is on the marks card, so Region is not a
    # projection -> the per-member selector could not resolve -> defer (no dataPoint).
    ws = _worksheet("Plain Cols", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST,
                    style=_palette_style("[federated.abc].[none:Region:nk]", _REGION_PALETTE))
    res = migrate_twb_to_pbir(_workbook(ws))
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert _data_point_objects(vj) is None
    fact = _mc_fact(res["candidate_records"], "Plain Cols")
    assert fact["status"] == "deferred"
    assert "not bound" in fact["reason"]


def test_chart_without_palette_emits_no_datapoint():
    # Additivity: a stacked column with a colour legend but NO explicit palette carries neither a
    # dataPoint object nor a mark_colors fact -- the report is byte-unchanged from before.
    enc = "<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>"
    ws = _worksheet("Stacked", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, encodings=enc)
    res = migrate_twb_to_pbir(_workbook(ws))
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert _data_point_objects(vj) is None
    assert _mc_fact(res["candidate_records"], "Stacked") is None


def test_single_default_mark_color_is_not_emitted():
    # A bare single ``mark-color`` is Tableau's default fill (written even when the author chose
    # nothing); it is deliberately NOT turned into a defaultColor -- only an explicit member map is.
    style = ("<style><style-rule element='mark'><format attr='mark-color' value='#b4b4b4' />"
             "</style-rule></style>")
    enc = "<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>"
    ws = _worksheet("Stacked", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, encodings=enc, style=style)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["mark_colors"] is None
    vj = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert _data_point_objects(vj) is None


# -- measure-series colours (datasource "Measure Names" palette) ---------------
# When a chart colours its marks by MEASURE IDENTITY -- either a [:Measure Names] colour pill or a
# measure VALUE on Color -- each member measure renders in its own colour drawn from the workbook's
# datasource-level "Measure Names" palette (the author's Sales-orange / Profit-blue convention). The
# faithful PBIR home is a per-measure ``dataPoint`` fill targeted by a ``metadata`` selector (the
# measure's queryRef). A chart coloured by a DIMENSION keeps its own categorical palette instead.
_PROFIT_HEX = "#4e79a7"   # blue
_SALES_HEX = "#f28e2b"    # orange


def _measure_palette_ds():
    # the shared datasource with Sales un-renamed (remote-name == logical name, as in the real
    # target workbook, so BOTH measure series resolve) + the global Measure-Names colour palette
    # (stored once on ``<datasource><style>``, shared by every Measure-Names-coloured sheet).
    ds = _DATASOURCE.replace("<remote-name>Sales Amount</remote-name>",
                             "<remote-name>Sales</remote-name>")
    style = (
        "<style><style-rule element='mark'>"
        "<encoding attr='color' field='[:Measure Names]' type='palette'>"
        f"<map to='{_PROFIT_HEX}'><bucket>&quot;[federated.abc].[sum:Profit:qk]&quot;</bucket></map>"
        f"<map to='{_SALES_HEX}'><bucket>&quot;[federated.abc].[sum:Sales:qk]&quot;</bucket></map>"
        "</encoding></style-rule></style>")
    return ds.replace("</datasource>", style + "</datasource>", 1)


def _measure_palette_workbook(worksheets):
    return ("<?xml version='1.0' encoding='utf-8' ?>\n<workbook>"
            + _measure_palette_ds()
            + "<worksheets>" + worksheets + "</worksheets></workbook>")


def _metadata_fills(visual_json):
    # {queryRef -> literal hex value} for every dataPoint fill targeted by a metadata (measure)
    # selector (the per-measure series fills); ignores categorical scopeId / gradient fills.
    dp = visual_json["visual"].get("objects", {}).get("dataPoint") or []
    return {e["selector"]["metadata"]:
            e["properties"]["fill"]["solid"]["color"]["expr"]["Literal"]["Value"]
            for e in dp if "metadata" in e.get("selector", {})}


def _ms_line_ws(name="MN Line"):
    # two measures on Rows + a date on Cols, coloured by Measure Names -> a line measure series.
    enc = "<encodings><color column='[federated.abc].[:Measure Names]' /></encodings>"
    return _worksheet(name, "Line",
                      rows="[federated.abc].[Multiple Values]",
                      cols="[federated.abc].[mn:Order Date:ok]",
                      deps_extra=_INST, encodings=enc,
                      filters=_mv_filter(["sum:Sales:qk", "sum:Profit:qk"]))


def _ms_bar_ws(name="MV Bar"):
    # two measures on Rows + a dimension on Cols, coloured by a measure VALUE (sum:Profit) -- the
    # broadened "colour by measure identity" path (the Sheet-1 case), not Measure Names.
    enc = "<encodings><color column='[federated.abc].[sum:Profit:qk]' /></encodings>"
    return _worksheet(name, "Bar",
                      rows="[federated.abc].[Multiple Values]",
                      cols="[federated.abc].[none:Category:nk]",
                      deps_extra=_INST, encodings=enc,
                      filters=_mv_filter(["sum:Sales:qk", "sum:Profit:qk"]))


def test_measure_names_palette_parsed_into_ir():
    ir = parse_twb(_measure_palette_workbook(_ms_line_ws()))
    assert ir["worksheets"][0]["measure_colors"] == {"profit": _PROFIT_HEX, "sales": _SALES_HEX}


def test_measure_series_line_emits_per_measure_metadata_fills():
    parts = emit_pbir(parse_twb(_measure_palette_workbook(_ms_line_ws())))
    vj = list(_visual_parts(parts).values())[0]
    assert vj["visual"]["visualType"] == "lineChart"
    assert _metadata_fills(vj) == {
        "Sum(Orders.Sales)": "'{0}'".format(_SALES_HEX),
        "Sum(Orders.Profit)": "'{0}'".format(_PROFIT_HEX)}


def test_measure_value_colour_attaches_palette_detection():
    # The broadened detection branch: colouring by a measure VALUE (kind == "value"), not only by
    # Measure Names, still attaches the datasource measure palette to the worksheet (the Sheet-1
    # case). Emit of the per-measure fills is exercised by the Measure-Names line test above -- the
    # two paths share ``_measure_series_colors``, which only depends on ``measure_colors`` being set.
    ir = parse_twb(_measure_palette_workbook(_ms_bar_ws()))
    assert ir["worksheets"][0]["measure_colors"] == {"profit": _PROFIT_HEX, "sales": _SALES_HEX}


def test_measure_series_fact_recorded_emitted():
    res = migrate_twb_to_pbir(_measure_palette_workbook(_ms_line_ws()))
    rec = next(r for r in res["candidate_records"] if r["worksheet"] == "MN Line")
    fact = rec["measure_colors"]
    assert fact["status"] == "emitted"
    assert fact["kind"] == "measure_series_palette"
    assert fact["count"] == 2


def test_dimension_coloured_chart_skips_measure_series_palette():
    # A chart coloured by a DIMENSION (Region) must NOT pick up the measure palette even though the
    # datasource declares one -- measure_colors stays None and no metadata fill is emitted.
    enc = "<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>"
    ws = _worksheet("Dim Bar", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, encodings=enc)
    ir = parse_twb(_measure_palette_workbook(ws))
    assert ir["worksheets"][0]["measure_colors"] is None
    vj = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert _metadata_fills(vj) == {}


def test_measure_palette_does_not_leak_into_map():
    # A measure-coloured choropleth carries measure_colors (its colour pill IS a measure value) but
    # is NOT a per-measure series -> no metadata fill and no deferral warning (silent skip).
    enc = ("<encodings><color column='[federated.abc].[sum:Profit:qk]' />"
           "<lod column='[federated.abc].[none:State:nk]' /></encodings>")
    res = migrate_twb_to_pbir(_measure_palette_workbook(
        _geo_ws("Profit by State", "Automatic", enc)))
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert vj["visual"]["visualType"] == "shapeMap"
    assert _metadata_fills(vj) == {}
    assert not any("measure series colours deferred" in w["reason"] for w in res["warnings"])


def test_no_datasource_palette_emits_no_measure_series():
    # Additivity: the same Measure-Names line WITHOUT a datasource palette emits no metadata fill
    # and carries no measure_colors fact (the report is byte-unchanged from before).
    ir = parse_twb(_workbook(_ms_line_ws()))
    assert ir["worksheets"][0]["measure_colors"] is None
    vj = list(_visual_parts(emit_pbir(ir)).values())[0]
    assert _metadata_fills(vj) == {}


# -- KPI / card label colours --------------------------------------------------
# A recoloured Tableau KPI card writes the category-label colour on its Measure-Names run and the
# value (big-number) colour + size on its value run, inside a ``customized-label``. These map to the
# card formatting objects ``categoryLabels`` (label) and ``dataLabels`` (value); bold is not emitted.
def _card_label_xml():
    return ("<customized-label><formatted-text>"
            f"<run fontcolor='{_SALES_HEX}'><![CDATA[<[federated.abc].[:Measure Names]>]]></run>"
            f"<run bold='true' fontcolor='{_PROFIT_HEX}' fontsize='14'>"
            "<![CDATA[<[federated.abc].[Multiple Values]>]]></run>"
            "</formatted-text></customized-label>")


def _kpi_card_ws(name="KPIs"):
    # two measures, no dimension -> a multiRowCard, with a recoloured customized-label.
    return _worksheet(name, "Bar",
                      rows="[federated.abc].[sum:Sales:qk]",
                      cols="[federated.abc].[sum:Profit:qk]",
                      deps_extra=_INST, pane_extra=_card_label_xml())


def test_card_label_colours_parsed_into_ir():
    ir = parse_twb(_workbook(_kpi_card_ws()))
    cc = ir["worksheets"][0]["card_label_colors"]
    assert cc["category_color"] == _SALES_HEX
    assert cc["value_color"] == _PROFIT_HEX
    assert cc["value_size"] == "14D"


def test_card_label_colours_emit_category_and_data_label_objects():
    vj = list(_visual_parts(emit_pbir(parse_twb(_workbook(_kpi_card_ws())))).values())[0]
    assert vj["visual"]["visualType"] == "multiRowCard"
    objs = vj["visual"]["objects"]
    cat = objs["categoryLabels"][0]["properties"]["color"]["solid"]["color"]["expr"]["Literal"]["Value"]
    val = objs["dataLabels"][0]["properties"]["color"]["solid"]["color"]["expr"]["Literal"]["Value"]
    size = objs["dataLabels"][0]["properties"]["fontSize"]["expr"]["Literal"]["Value"]
    assert cat == "'{0}'".format(_SALES_HEX)
    assert val == "'{0}'".format(_PROFIT_HEX)
    assert size == "14D"


def test_card_without_recoloured_label_emits_no_label_objects():
    # Additivity: a plain KPI card (no customized-label) carries neither card_label_colors nor the
    # categoryLabels / dataLabels objects.
    ws = _worksheet("Plain KPIs", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[sum:Profit:qk]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["card_label_colors"] is None
    vj = list(_visual_parts(emit_pbir(ir)).values())[0]
    objs = vj["visual"].get("objects", {})
    assert "categoryLabels" not in objs and "dataLabels" not in objs


# -- data labels (Tableau "Show Mark Labels" toggle) ---------------------------
# Tableau writes the mark-label show/hide as ``<format attr='mark-labels-show' value='..'/>`` in a
# ``<style-rule element='mark'>`` (at the worksheet ``table/style`` and/or per ``pane``). It maps to
# the PBIR data-plane ``visual.objects.labels`` ``show`` toggle, applied uniformly (no selector).
# WARN-NEVER-WRONG: show=true is emitted whenever the toggle is unambiguously ON; show=false is
# emitted ONLY for pie/donut (whose Power BI default is ON); other types default OFF so an OFF toggle
# is a no-op; a dual-axis worksheet whose panes disagree defers (keeps the default) with a warning;
# a table/matrix/card already displays its values so no label object is produced.
def _label_style(show):
    return ("<style><style-rule element='mark'>"
            "<format attr='mark-labels-show' value='{0}' />"
            "</style-rule></style>".format("true" if show else "false"))


def _labels_objects(visual_json):
    return visual_json["visual"].get("objects", {}).get("labels")


def _dl_fact(records, worksheet):
    rec = next(r for r in records if r["worksheet"] == worksheet)
    return rec.get("data_labels")


def test_data_labels_pane_toggle_parsed_into_ir():
    ws = _worksheet("Cols", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, pane_extra=_label_style(True))
    dl = parse_twb(_workbook(ws))["worksheets"][0]["data_labels"]
    assert dl == {"show": True, "uniform": True, "raw_values": [True]}


def test_data_labels_table_style_toggle_parsed_into_ir():
    # the worksheet-level table/style toggle is read too (not only the per-pane one)
    ws = _worksheet("Cols", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, style=_label_style(True))
    dl = parse_twb(_workbook(ws))["worksheets"][0]["data_labels"]
    assert dl["show"] is True and dl["uniform"] is True


def test_data_labels_show_emits_labels_object_and_fact():
    ws = _worksheet("Cols", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, pane_extra=_label_style(True))
    res = migrate_twb_to_pbir(_workbook(ws))
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert _labels_objects(vj) == [
        {"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}}}]
    fact = _dl_fact(res["candidate_records"], "Cols")
    assert fact["kind"] == "data_labels"
    assert fact["status"] == "emitted" and fact["show"] is True


def test_data_labels_off_on_cartesian_is_noop():
    # Power BI defaults data labels OFF on column/bar/line/area, so an OFF Tableau toggle needs no
    # object -- the record still discloses the (default_off) fact, additively.
    ws = _worksheet("Cols", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, pane_extra=_label_style(False))
    res = migrate_twb_to_pbir(_workbook(ws))
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert _labels_objects(vj) is None
    fact = _dl_fact(res["candidate_records"], "Cols")
    assert fact["status"] == "default_off" and fact["show"] is False


def test_data_labels_off_on_pie_emits_show_false():
    # Pie/donut default labels ON in Power BI, so an OFF Tableau toggle must be emitted as show=false
    # to faithfully hide them.
    ws = _worksheet("Share", "Pie",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, pane_extra=_label_style(False))
    res = migrate_twb_to_pbir(_workbook(ws))
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert vj["visual"]["visualType"] == "pieChart"
    assert _labels_objects(vj) == [
        {"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}]
    assert _dl_fact(res["candidate_records"], "Share")["status"] == "emitted"


def test_data_labels_disagreeing_panes_defer_with_warning():
    # table/style says ON, the pane says OFF -> the toggle is ambiguous (a dual-axis per-series
    # difference) -> no global toggle is guessed; the visual keeps its default + a warning discloses.
    ws = _worksheet("Cols", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST, style=_label_style(True),
                    pane_extra=_label_style(False))
    res = migrate_twb_to_pbir(_workbook(ws))
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert _labels_objects(vj) is None
    fact = _dl_fact(res["candidate_records"], "Cols")
    assert fact["status"] == "deferred"
    assert any("data labels deferred" in w["reason"] for w in res["warnings"])


def test_data_labels_absent_emits_nothing():
    # Additivity: a chart with no mark-labels-show toggle carries neither a labels object nor a fact.
    ws = _worksheet("Cols", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    res = migrate_twb_to_pbir(_workbook(ws))
    assert parse_twb(_workbook(ws))["worksheets"][0]["data_labels"] is None
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert _labels_objects(vj) is None
    assert _dl_fact(res["candidate_records"], "Cols") is None


def test_data_labels_on_card_not_applicable():
    # A card already displays its value, so a label toggle on a card emits no labels object and no
    # fact (the label types exclude card / table / matrix / map).
    ws = _worksheet("KPI", "Text",
                    rows="[federated.abc].[sum:Sales:qk]", cols="",
                    deps_extra=_INST, pane_extra=_label_style(True))
    res = migrate_twb_to_pbir(_workbook(ws))
    vj = list(_visual_parts(res["parts"]).values())[0]
    assert _labels_objects(vj) is None
    assert _dl_fact(res["candidate_records"], "KPI") is None


# -- legend (Tableau dashboard colour-legend zone) -----------------------------
# Tableau writes a SHOWN colour-Series legend as a dashboard ``<zone type='color' name='<ws>'>``;
# its geometry vs the worksheet's own zone reproduces the legend side, and a worksheet that carries a
# categorical colour Series but has NO colour zone means the author hid the legend. Maps to the PBIR
# data-plane ``visual.objects.legend`` ``show`` / ``position`` (applied uniformly, no selector).
# WARN-NEVER-WRONG: a ``position`` is emitted only when the zone sits clearly on one side; an
# overlapping zone keeps Power BI's default position; ``show:false`` only when a real colour Series
# has no zone; the standalone (non-dashboard) emit path is untouched (PBI's default legend matches
# Tableau's default).
def _legend_series_ws(name="Sales by Region"):
    enc = "<encodings><color column='[federated.abc].[none:Region:nk]' /></encodings>"
    return _worksheet(name, "Bar",
                      rows="[federated.abc].[sum:Sales:qk]",
                      cols="[federated.abc].[none:Category:nk]",
                      deps_extra=_INST, encodings=enc)


def _color_zone(ws_name, x, y, w, h):
    return ("<zone type='color' name='{0}' param='[federated.abc].[none:Region:nk]' "
            "x='{1}' y='{2}' w='{3}' h='{4}' id='9' />".format(ws_name, x, y, w, h))


def _legend_dash(ws_name, color_zone="", ws_geom=(0, 0, 80000, 100000)):
    wx, wy, ww, wh = ws_geom
    inner = ("<zone h='100000' w='100000' x='0' y='0'>"
             "<zone h='{0}' w='{1}' x='{2}' y='{3}' name='{4}' id='2' />".format(wh, ww, wx, wy, ws_name)
             + color_zone + "</zone>")
    return "<dashboard name='D'><zones>" + inner + "</zones></dashboard>"


def _legend_objects_of(visual_json):
    return visual_json["visual"].get("objects", {}).get("legend")


def _dash_main_visual(parts):
    mains = [v for v in _visual_parts(parts).values()
             if v["visual"]["visualType"] != "slicer"]
    assert len(mains) == 1
    return mains[0]


def _lg_fact(records, worksheet):
    rec = next(r for r in records if r["worksheet"] == worksheet)
    return rec.get("legend")


def test_legend_color_zone_parsed_into_dashboard_ir():
    cz = _color_zone("Sales by Region", 85000, 0, 15000, 100000)
    ir = parse_twb(_workbook(_legend_series_ws(), _legend_dash("Sales by Region", cz)))
    lz = ir["dashboards"][0]["legend_zones"]
    assert len(lz) == 1
    assert lz[0]["worksheet"] == "Sales by Region"
    assert (lz[0]["x"], lz[0]["w"]) == (85000.0, 15000.0)


def test_legend_right_zone_emits_position_right():
    cz = _color_zone("Sales by Region", 85000, 0, 15000, 100000)
    res = migrate_twb_to_pbir(_workbook(_legend_series_ws(), _legend_dash("Sales by Region", cz)))
    vj = _dash_main_visual(res["parts"])
    assert _legend_objects_of(vj) == [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "position": {"expr": {"Literal": {"Value": "'Right'"}}}}}]
    fact = _lg_fact(res["candidate_records"], "Sales by Region")
    assert fact == {"kind": "legend", "status": "emitted", "position": "Right"}


def test_legend_bottom_zone_emits_position_bottom():
    cz = _color_zone("Sales by Region", 0, 85000, 100000, 15000)
    res = migrate_twb_to_pbir(_workbook(
        _legend_series_ws(), _legend_dash("Sales by Region", cz, ws_geom=(0, 0, 100000, 80000))))
    vj = _dash_main_visual(res["parts"])
    pos = _legend_objects_of(vj)[0]["properties"]["position"]["expr"]["Literal"]["Value"]
    assert pos == "'Bottom'"
    assert _lg_fact(res["candidate_records"], "Sales by Region")["position"] == "Bottom"


def test_legend_overlapping_zone_defers_position():
    # A colour zone that overlaps the chart (no clear side) keeps Power BI's default legend position:
    # no position is guessed, but the (position_deferred) fact still discloses the legend.
    cz = _color_zone("Sales by Region", 20000, 40000, 20000, 15000)
    res = migrate_twb_to_pbir(_workbook(
        _legend_series_ws(), _legend_dash("Sales by Region", cz, ws_geom=(0, 0, 100000, 100000))))
    vj = _dash_main_visual(res["parts"])
    assert _legend_objects_of(vj) is None
    assert _lg_fact(res["candidate_records"], "Sales by Region")["status"] == "position_deferred"


def test_legend_absent_zone_hides_legend():
    # A worksheet with a categorical colour Series but NO colour zone on the dashboard = the author
    # hid the legend -> emit show=false to faithfully suppress Power BI's default legend.
    res = migrate_twb_to_pbir(_workbook(_legend_series_ws(), _legend_dash("Sales by Region")))
    vj = _dash_main_visual(res["parts"])
    assert _legend_objects_of(vj) == [
        {"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}]
    assert _lg_fact(res["candidate_records"], "Sales by Region")["status"] == "hidden"


def test_legend_no_color_series_emits_nothing():
    # Additivity: a chart with no colour Series has no legend in either tool -> no legend object and
    # no fact, even on a dashboard.
    ws = _worksheet("Plain", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]", deps_extra=_INST)
    res = migrate_twb_to_pbir(_workbook(ws, _legend_dash("Plain")))
    vj = _dash_main_visual(res["parts"])
    assert _legend_objects_of(vj) is None
    assert _lg_fact(res["candidate_records"], "Plain") is None


def test_legend_matrix_type_ignored():
    # A highlight-table matrix has no cartesian legend object; even with a categorical colour Series
    # and a colour zone, no legend object/fact is produced (the legend types exclude matrix/table).
    enc = ("<encodings><color column='[federated.abc].[none:Region:nk]' />"
           "<text column='[federated.abc].[sum:Sales:qk]' /></encodings>")
    ws = _heat_ws("Grid", color_field="none:Region:nk", encodings=enc, style="")
    cz = _color_zone("Grid", 85000, 0, 15000, 100000)
    res = migrate_twb_to_pbir(_workbook(ws, _legend_dash("Grid", cz)))
    vj = _dash_main_visual(res["parts"])
    assert vj["visual"]["visualType"] == "pivotTable"
    assert _legend_objects_of(vj) is None
    assert _lg_fact(res["candidate_records"], "Grid") is None


def test_legend_standalone_worksheet_unaffected():
    # The legend signal is dashboard-scoped; a standalone worksheet page (no dashboard) is byte-
    # unchanged -- Power BI's default legend already matches Tableau's default for a colour Series.
    res = migrate_twb_to_pbir(_workbook(_legend_series_ws()))
    vj = _dash_main_visual(res["parts"])
    assert _legend_objects_of(vj) is None
    assert _lg_fact(res["candidate_records"], "Sales by Region") is None


# -- title font styling (Tier-2) ----------------------------------------------
# A worksheet title's per-run font attributes -> the visual container title's font. Only the two
# unambiguous, schema-grounded properties are emitted (fontSize as a "Nd" literal, fontColor as a
# solid single-quoted hex literal), and ONLY when every text-bearing run agrees (Power BI applies
# one font to the whole title). Disagreeing / partial runs, plus bold / family / alignment, are
# deferred (recorded on the additive ``title_style`` fact, never emitted) -- warn-never-wrong.
def _titled_ws(title):
    return _workbook(_worksheet("Sales by Category", "Bar",
                                rows="[federated.abc].[sum:Sales:qk]",
                                cols="[federated.abc].[none:Category:nk]",
                                deps_extra=_INST, title=title))


def _title_props(res):
    return _only_visual(res)["visual"]["visualContainerObjects"]["title"][0]["properties"]


def _title_fact(res, ws_name):
    for rec in res["candidate_records"]:
        if rec["worksheet"] == ws_name:
            return rec.get("title_style")
    return None


def test_title_style_uniform_fontsize_emits_literal():
    res = migrate_twb_to_pbir(_titled_ws("<run fontsize='15'>Quarterly Sales</run>"))
    assert res["ir"]["worksheets"][0]["title_style"] == {"font_size": "15D"}
    props = _title_props(res)
    assert props["fontSize"]["expr"]["Literal"]["Value"] == "15D"
    assert "fontColor" not in props
    assert _title_fact(res, "Sales by Category") == {"font_size": "15D"}
    assert res["warnings"] == []


def test_title_style_uniform_fontcolor_emits_solid_literal():
    res = migrate_twb_to_pbir(_titled_ws("<run fontcolor='#d3872a'>Quarterly Sales</run>"))
    assert res["ir"]["worksheets"][0]["title_style"] == {"font_color": "#d3872a"}
    props = _title_props(res)
    color = props["fontColor"]["solid"]["color"]["expr"]["Literal"]["Value"]
    assert color == "'#d3872a'"
    assert "fontSize" not in props


def test_title_style_size_and_color_both_emit():
    res = migrate_twb_to_pbir(
        _titled_ws("<run fontcolor='#1f77b4' fontsize='18'>Quarterly Sales</run>"))
    props = _title_props(res)
    assert props["fontSize"]["expr"]["Literal"]["Value"] == "18D"
    assert props["fontColor"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#1f77b4'"


def test_title_style_multi_run_agree_emits_once():
    res = migrate_twb_to_pbir(
        _titled_ws("<run fontsize='13'>Sales </run><run fontsize='13'>by Region</run>"))
    assert _title_props(res)["fontSize"]["expr"]["Literal"]["Value"] == "13D"


def test_title_style_disagreeing_runs_defer_property():
    # runs disagree on size -> defer fontSize (no font emitted), keep the structural title text.
    res = migrate_twb_to_pbir(
        _titled_ws("<run fontsize='15'>Big</run><run fontsize='9'>small</run>"))
    props = _title_props(res)
    assert "fontSize" not in props
    assert props["text"]["expr"]["Literal"]["Value"] == "'Bigsmall'"
    assert res["ir"]["worksheets"][0]["title_style"] == {"deferred": ["fontsize"]}
    assert _title_fact(res, "Sales by Category") == {"deferred": ["fontsize"]}


def test_title_style_uniform_bold_emits_weight():
    # every text run is bold -> the container title carries bold=true.
    res = migrate_twb_to_pbir(_titled_ws("<run bold='true' fontsize='14'>Sales</run>"))
    props = _title_props(res)
    assert props["bold"]["expr"]["Literal"]["Value"] == "true"
    assert props["fontSize"]["expr"]["Literal"]["Value"] == "14D"
    assert res["ir"]["worksheets"][0]["title_style"]["bold"] is True


def test_title_style_mixed_bold_is_deferred():
    # one run bold, one not -> mixed weight defers (Power BI applies one font to the whole title).
    res = migrate_twb_to_pbir(_titled_ws("<run bold='true'>Big</run><run>plain</run>"))
    assert "bold" not in _title_props(res)
    assert res["ir"]["worksheets"][0]["title_style"] == {"deferred": ["bold"]}


def test_title_style_real_font_family_emits():
    # a real (non-Tableau-internal) uniform font family -> fontFamily literal.
    res = migrate_twb_to_pbir(_titled_ws("<run fontname='Georgia' fontsize='12'>Sales</run>"))
    props = _title_props(res)
    assert props["fontFamily"]["expr"]["Literal"]["Value"] == "'Georgia'"
    assert res["ir"]["worksheets"][0]["title_style"]["font_family"] == "Georgia"


def test_title_style_internal_font_and_alignment_deferred_bold_emitted():
    # Tableau-internal family ('Tableau Bold') + alignment defer; an explicit uniform bold emits.
    res = migrate_twb_to_pbir(_titled_ws(
        "<run bold='true' fontname='Tableau Bold' fontalignment='1' fontsize='14'>Sales</run>"))
    props = _title_props(res)
    assert props["fontSize"]["expr"]["Literal"]["Value"] == "14D"
    assert props["bold"]["expr"]["Literal"]["Value"] == "true"
    assert "fontFamily" not in props  # Tableau-internal font has no Power BI equivalent
    assert set(res["ir"]["worksheets"][0]["title_style"]["deferred"]) == {"fontname", "fontalignment"}


def test_title_style_non_hex_color_is_deferred():
    res = migrate_twb_to_pbir(_titled_ws("<run fontcolor='#11223344'>Sales</run>"))
    assert "fontColor" not in _title_props(res)
    assert res["ir"]["worksheets"][0]["title_style"] == {"deferred": ["fontcolor"]}


def test_title_without_styling_emits_no_font_and_no_fact():
    res = migrate_twb_to_pbir(_titled_ws("<run>Plain Title</run>"))
    props = _title_props(res)
    assert set(props) == {"show", "text"}
    assert res["ir"]["worksheets"][0]["title_style"] is None
    assert _title_fact(res, "Sales by Category") is None


def test_title_style_dynamic_title_has_no_style():
    # a dynamic title is deferred entirely (no static text) -> no title_style computed.
    ws = _worksheet("Dyn", "Bar",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST,
                    title="<run fontsize='15'>Sales for </run><run>&lt;Region&gt;</run>")
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["title"] is None
    assert ir["worksheets"][0]["title_style"] is None


def test_title_style_standalone_path_emits_font():
    # the standalone (non-dashboard) worksheet page carries the same title font styling.
    res = migrate_twb_to_pbir(_titled_ws("<run fontsize='20'>Big Title</run>"))
    vj = _only_visual(res)
    props = vj["visual"]["visualContainerObjects"]["title"][0]["properties"]
    assert props["fontSize"]["expr"]["Literal"]["Value"] == "20D"


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
        assert _values_backcolor(vj) is None, f"{status} should not emit a fill"
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


# =============================================================================
# Regression: dashboard filter-card -> Power BI slicer fidelity.
#   (1) a full top filter BAND rebuilds one slicer per card at its authored grid
#       position + show mode, with no five-deep synthetic-stack cap;
#   (2) a row-level DIMENSION calc ("Job Type") is kept as a sliceable column,
#       while measure-role / parameter-comparing calcs stay warned-and-dropped;
#   (3) a discrete exact-date VALUE derivation ("Fiscal Month" shown MDY) binds
#       as an ordinary date column instead of being dropped as unsupported.
# Locks the filter/slicer fidelity fixes; the report schema stays additive.
# =============================================================================

# -- a wide datasource with >= 6 filterable dimensions. The shared _DATASOURCE
#    has only three plain dimensions -- too few to prove a filter band of more
#    than five cards is NOT truncated to five by the old synthetic-stack guard.
_WIDE_DIMS = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]


def _wide_datasource():
    recs = "".join(
        f"<metadata-record class='column'><remote-name>{d}</remote-name>"
        f"<local-name>[{d}]</local-name><parent-name>[T]</parent-name>"
        f"<local-type>string</local-type></metadata-record>" for d in _WIDE_DIMS)
    recs += ("<metadata-record class='column'><remote-name>Amount</remote-name>"
             "<local-name>[Amount]</local-name><parent-name>[T]</parent-name>"
             "<local-type>real</local-type></metadata-record>")
    return (
        "<datasources><datasource caption='Wide' inline='true' "
        "name='federated.wide' version='18.1'><connection class='federated'>"
        "<relation name='T' table='[dbo].[T]' type='table' />"
        "<metadata-records>" + recs + "</metadata-records>"
        "</connection></datasource></datasources>")


def _wide_deps():
    cols = "".join(
        f"<column caption='{d}' datatype='string' name='[{d}]' role='dimension' "
        "type='nominal' />" for d in _WIDE_DIMS)
    cols += ("<column caption='Amount' datatype='real' name='[Amount]' role='measure' "
             "type='quantitative' />")
    insts = "".join(
        f"<column-instance column='[{d}]' derivation='None' name='[none:{d}:nk]' "
        "pivot='key' type='nominal' />" for d in _WIDE_DIMS)
    insts += ("<column-instance column='[Amount]' derivation='Sum' name='[sum:Amount:qk]' "
              "pivot='key' type='quantitative' />")
    return cols + insts


def _wide_worksheet(name, dims):
    filters = "".join(
        f"<filter class='categorical' column='[federated.wide].[none:{d}:nk]'>"
        f"<groupfilter function='member' level='[none:{d}:nk]' /></filter>" for d in dims)
    return (
        f"<worksheet name='{name}'><table><view>"
        "<datasources><datasource caption='Wide' name='federated.wide' /></datasources>"
        f"<datasource-dependencies datasource='federated.wide'>{_wide_deps()}"
        "</datasource-dependencies>"
        f"{filters}</view>"
        "<panes><pane><mark class='Bar' /></pane></panes>"
        "<rows>[federated.wide].[sum:Amount:qk]</rows>"
        "<cols>[federated.wide].[none:Alpha:nk]</cols>"
        "</table></worksheet>")


def _wide_workbook(worksheets, dashboards=""):
    return (
        "<?xml version='1.0' encoding='utf-8' ?>\n<workbook>"
        + _wide_datasource()
        + "<worksheets>" + worksheets + "</worksheets>"
        + ("<dashboards>" + dashboards + "</dashboards>" if dashboards else "")
        + "</workbook>")


def _wide_filter_cards(ws_name, dims, mode=None, hidden=False):
    attrs = (f" mode='{mode}'" if mode else "") + (" hidden-by-user='true'" if hidden else "")
    # each card at its own authored x (5000, 20000, ...) -> distinct scaled positions, never the
    # single x == PAGE_WIDTH-220 the synthetic right-rail stack uses.
    return "".join(
        f"<zone name='{ws_name}' param='[federated.wide].[none:{d}:nk]' type-v2='filter' "
        f"h='6000' w='15000' x='{5000 + i * 15000}' y='2000' id='{30 + i}'{attrs} />"
        for i, d in enumerate(dims))


def _wide_dashboard(ws_name, dims, mode=None, hidden=False):
    inner = (
        "<zone h='100000' w='100000' x='0' y='0'>"
        f"<zone h='80000' w='90000' x='5000' y='15000' name='{ws_name}' id='2' />"
        + _wide_filter_cards(ws_name, dims, mode=mode, hidden=hidden)
        + "</zone>")
    return (
        "<dashboard name='Dash'><size maxheight='800' maxwidth='1200' />"
        f"<zones>{inner}</zones></dashboard>")


def _page_slicers(parts):
    return [v for v in _visual_parts(parts).values()
            if v["visual"]["visualType"] == "slicer"]


def _slicer_prop(v):
    return (v["visual"]["query"]["queryState"]["Values"]["projections"][0]
            ["field"]["Column"]["Property"])


def _slicer_show_mode(v):
    return v["visual"]["objects"]["data"][0]["properties"]["mode"]["expr"]["Literal"]["Value"]


def test_dashboard_filter_band_emits_a_slicer_per_card_not_capped_at_five():
    dims = _WIDE_DIMS  # six distinct filter cards
    ws = _wide_worksheet("W", dims)
    parts = emit_pbir(parse_twb(_wide_workbook(ws, _wide_dashboard("W", dims))))
    slicers = _page_slicers(parts)
    assert len(slicers) == len(dims)  # all six -- NOT truncated to five by a page-height guard
    assert sorted(_slicer_prop(s) for s in slicers) == sorted(dims)
    xs = [s["position"]["x"] for s in slicers]
    assert all(x != PAGE_WIDTH - 220 for x in xs)      # not the synthetic right-rail stack (x==1060)
    assert len({round(x, 3) for x in xs}) == len(slicers)  # each card at its own authored position


def test_tableau_filter_show_mode_maps_to_pbi_slicer_mode():
    # dropdown-family -> compact 'Dropdown'; list/radio -> 'Basic' (List); unknown/absent defaults to
    # 'Dropdown' (the overwhelmingly common quick-filter style a top filter band uses).
    assert _tableau_filter_mode_to_pbi("checkdropdown") == "Dropdown"
    assert _tableau_filter_mode_to_pbi("typeindropdown") == "Dropdown"
    assert _tableau_filter_mode_to_pbi("checklist") == "Basic"
    assert _tableau_filter_mode_to_pbi("radiolist") == "Basic"
    assert _tableau_filter_mode_to_pbi(None) == "Dropdown"


def test_dashboard_filter_card_show_mode_lands_on_the_slicer():
    dims = _WIDE_DIMS[:2]
    ws = _wide_worksheet("W", dims)
    dd = emit_pbir(parse_twb(_wide_workbook(ws, _wide_dashboard("W", dims, mode="checkdropdown"))))
    assert _page_slicers(dd) and all(_slicer_show_mode(s) == "'Dropdown'" for s in _page_slicers(dd))
    lst = emit_pbir(parse_twb(_wide_workbook(ws, _wide_dashboard("W", dims, mode="checklist"))))
    assert _page_slicers(lst) and all(_slicer_show_mode(s) == "'Basic'" for s in _page_slicers(lst))


def test_hidden_by_user_filter_band_still_rebuilds_its_slicers():
    # ``hidden-by-user`` is a Tableau show/hide TOGGLE on a collapsible filter container, not a
    # delete; Power BI has no Tier-1 collapse equivalent, so the faithful rebuild surfaces every
    # filter (usable) regardless -- a fully-toggled-hidden band is never silently dropped.
    dims = _WIDE_DIMS
    ws = _wide_worksheet("W", dims)
    parts = emit_pbir(parse_twb(_wide_workbook(ws, _wide_dashboard("W", dims, hidden=True))))
    assert len(_page_slicers(parts)) == len(dims)


def test_non_slicer_visual_carries_no_slicer_mode_object():
    # the slicer show-mode block lives ONLY under a slicer's ``objects.data``; every other visual
    # stays byte-identical (no fabricated mode card), so the fix never regresses a chart.
    ws = _worksheet("Bars", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    mains = [v for v in _visual_parts(emit_pbir(parse_twb(_workbook(ws)))).values()
             if v["visual"]["visualType"] != "slicer"]
    assert mains and all("data" not in v["visual"].get("objects", {}) for v in mains)


def test_worksheet_page_filter_slicer_keeps_the_synthetic_right_rail_stack():
    # a STANDALONE worksheet page has no dashboard card geometry, so the original synthetic
    # right-rail slicer stack (x == PAGE_WIDTH-220, no show-mode object) is kept byte-for-byte --
    # the new dashboard-band path only engages when filter cards are present.
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='member' level='[none:Region:nk]' /></filter>")
    ws = _worksheet("Solo", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST, filters=filt)
    slicers = _page_slicers(emit_pbir(parse_twb(_workbook(ws))))
    assert len(slicers) == 1
    assert slicers[0]["position"]["x"] == PAGE_WIDTH - 220
    assert "data" not in slicers[0]["visual"].get("objects", {})


# -- row-level dimension calc kept as a slicer ("Job Type") --------------------
_JOBTYPE_CALC = (
    "<column caption='Job Type' datatype='string' name='[JobType]' role='dimension' "
    "type='nominal'><calculation class='tableau' "
    "formula='IF [Sales] &gt; 100 THEN &quot;High&quot; ELSE &quot;Low&quot; END' /></column>"
    "<column-instance column='[JobType]' derivation='None' name='[none:JobType:nk]' "
    "pivot='key' type='nominal' />")

_MEASURE_CALC = (
    "<column caption='Big Sales' datatype='real' name='[BigSales]' role='measure' "
    "type='quantitative'><calculation class='tableau' formula='SUM([Sales]) * 2' /></column>"
    "<column-instance column='[BigSales]' derivation='None' name='[none:BigSales:qk]' "
    "pivot='key' type='quantitative' />")


def _calc_filter(inst):
    return (f"<filter class='categorical' column='[federated.abc].[{inst}]'>"
            f"<groupfilter function='member' level='[{inst}]' /></filter>")


def test_row_level_dimension_calc_filter_is_kept_as_a_slicer():
    # "Job Type" is a row-level IF/CASE dimension bucket -> a real sliceable model column, so a
    # dashboard filter card on it must surface as a slicer, not be dropped as an unmappable calc.
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _JOBTYPE_CALC, filters=_calc_filter("none:JobType:nk"))
    card = ("<zone name='W' param='[federated.abc].[none:JobType:nk]' type-v2='filter' "
            "h='6000' w='20000' x='5000' y='2000' id='30' />")
    inner = ("<zone h='100000' w='100000' x='0' y='0'>"
             "<zone h='80000' w='90000' x='5000' y='15000' name='W' id='2' />" + card + "</zone>")
    dash = ("<dashboard name='D'><size maxheight='800' maxwidth='1200' />"
            "<zones>" + inner + "</zones></dashboard>")
    ir = parse_twb(_workbook(ws, dash))
    kept = [f for f in ir["worksheets"][0]["filters"] if f["caption"] == "Job Type"]
    assert len(kept) == 1 and kept[0]["binding"] == "column"
    assert not any("aggregate/measure filter on 'Job Type'" in w["reason"] for w in ir["warnings"])
    assert "Job Type" in [_slicer_prop(s) for s in _page_slicers(emit_pbir(ir))]


def test_measure_role_calc_filter_is_still_warned_and_dropped():
    # the guard is narrow: a calc that ROLLS UP to a measure has no faithful slicer mapping and
    # stays warned-and-dropped -- only row-level dimension calcs are kept.
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _MEASURE_CALC, filters=_calc_filter("none:BigSales:qk"))
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["filters"] == []
    assert any("aggregate/measure filter on 'Big Sales'" in w["reason"] for w in ir["warnings"])


def test_parameter_comparing_calc_filter_is_still_warned_and_dropped():
    # a DIMENSION calc that COMPARES against a parameter ([Parameters] in its formula) exposes no
    # column a slicer can bind, so it stays warned-and-dropped despite its dimension role.
    calc = ("<column caption='Over Target' datatype='boolean' name='[OverTgt]' role='dimension' "
            "type='nominal'><calculation class='tableau' "
            "formula='[Sales] &gt; [Parameters].[Parameter 1]' /></column>"
            "<column-instance column='[OverTgt]' derivation='None' name='[none:OverTgt:nk]' "
            "pivot='key' type='nominal' />")
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + calc, filters=_calc_filter("none:OverTgt:nk"))
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["filters"] == []
    assert any("aggregate/measure filter on 'Over Target'" in w["reason"] for w in ir["warnings"])


# -- discrete exact-date VALUE derivation binds as a plain date ("Fiscal Month") --
_MDY_INST = ("<column-instance column='[Order Date]' derivation='MDY' "
             "name='[md:Order Date:ok]' pivot='key' type='ordinal' />")


def test_mdy_exact_date_derivation_filter_is_kept_as_a_date_slicer():
    # "Fiscal Month" is an ordinary date column merely SHOWN in Month/Day/Year (MDY) discrete
    # format -- the same underlying date as a plain pill -- so a filter card on it is a faithful
    # date slicer; a display-format choice must never drop the field.
    filt = ("<filter class='categorical' column='[federated.abc].[md:Order Date:ok]'>"
            "<groupfilter function='member' level='[md:Order Date:ok]' /></filter>")
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]",
                    deps_extra=_INST + _MDY_INST, filters=filt)
    card = ("<zone name='W' param='[federated.abc].[md:Order Date:ok]' type-v2='filter' "
            "h='6000' w='20000' x='5000' y='2000' id='30' />")
    inner = ("<zone h='100000' w='100000' x='0' y='0'>"
             "<zone h='80000' w='90000' x='5000' y='15000' name='W' id='2' />" + card + "</zone>")
    dash = ("<dashboard name='D'><size maxheight='800' maxwidth='1200' />"
            "<zones>" + inner + "</zones></dashboard>")
    ir = parse_twb(_workbook(ws, dash))
    kept = ir["worksheets"][0]["filters"]
    assert len(kept) == 1 and kept[0]["caption"] == "Order Date" and kept[0]["binding"] == "column"
    assert not any("unsupported derivation" in w["reason"] for w in ir["warnings"])
    assert len(_page_slicers(emit_pbir(ir))) == 1


def test_mdy_exact_date_derivation_on_axis_binds_as_a_plain_date_column():
    # MDY on a shelf is the exact date VALUE at day grain -> a plain date category axis, bound (not
    # dropped): the visual keeps both the measure and the date pill.
    ws = _worksheet("Trend", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[md:Order Date:ok]", deps_extra=_INST + _MDY_INST)
    ir = parse_twb(_workbook(ws))
    assert not any("unsupported derivation" in w["reason"] for w in ir["warnings"])
    main = [v for v in _visual_parts(emit_pbir(ir)).values()
            if v["visual"]["visualType"] != "slicer"][0]
    refs = [p["queryRef"] for st in _query_state(main).values() for p in st["projections"]]
    assert len(refs) == 2  # the Sales measure AND the MDY date axis are both bound (date not dropped)


def test_exact_date_derivation_set_is_scoped_and_unknown_derivations_still_warn():
    # the exact-date allowance is scoped to the MDY value family; a coarser / unknown derivation is
    # NOT silently accepted -- it stays fail-closed (warn+skip) until verified against a real artifact.
    assert "MDY" in _DATE_EXACT_DERIVATIONS and "MDYHMS" in _DATE_EXACT_DERIVATIONS
    assert "MY" not in _DATE_EXACT_DERIVATIONS and "Xyz" not in _DATE_EXACT_DERIVATIONS
    inst = ("<column-instance column='[Order Date]' derivation='Xyz' "
            "name='[xyz:Order Date:ok]' pivot='key' type='ordinal' />")
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[xyz:Order Date:ok]", deps_extra=_INST + inst)
    ir = parse_twb(_workbook(ws))
    assert any("unsupported derivation" in w["reason"] for w in ir["warnings"])


# == §13 geometry fidelity acceptance ==========================================
# Per-dashboard page size (from <size maxwidth/maxheight>), dropdown-slicer height
# floor, and the shown-state worksheet reflow. All fixtures are the same inline-XML
# helpers the rest of the suite uses; assertions are hand-derived from _scale_zone
# (sx=page_w/ref_w, sy=page_h/ref_h; the outer 100000x100000 zone is the ref frame).

def _page_dims(parts):
    """displayName -> (width, height) for every emitted page.json."""
    out = {}
    for k, v in parts.items():
        if k.endswith("page.json"):
            pj = json.loads(v)
            out[pj["displayName"]] = (pj["width"], pj["height"])
    return out


def _content_visual(parts):
    return [v for v in _visual_parts(parts).values()
            if v["visual"]["visualType"] != "slicer"][0]


def _one_card_dashboard(ws_name, token, card_h, card_y, mode=None):
    """A single-worksheet, single-filter-card dashboard on a 1200x800 canvas.

    The outer 100000x100000 zone is the scaling frame (sx=0.012, sy=0.008), matching
    the MDY single-card idiom already used above. ``card_h``/``card_y`` are Tableau's
    normalized zone units; ``mode`` (absent -> Dropdown) drives the slicer show mode."""
    attr = f" mode='{mode}'" if mode else ""
    card = (f"<zone name='{ws_name}' param='[federated.abc].[{token}]' type-v2='filter' "
            f"h='{card_h}' w='20000' x='5000' y='{card_y}' id='30'{attr} />")
    inner = ("<zone h='100000' w='100000' x='0' y='0'>"
             f"<zone h='80000' w='90000' x='5000' y='15000' name='{ws_name}' id='2' />"
             + card + "</zone>")
    return ("<dashboard name='D'><size maxheight='800' maxwidth='1200' />"
            "<zones>" + inner + "</zones></dashboard>")


def test_dashboard_page_adopts_declared_size_1400x1000():
    # §13.2: the PBIR page is emitted at the dashboard's OWN fixed pixel canvas -- a
    # 1400x1000 Tableau <size> becomes a 1400x1000 page (not the 1280x720 default).
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    dash = ("<dashboard name='Big'><size maxheight='1000' maxwidth='1400' />"
            "<zones><zone h='100000' w='100000' x='0' y='0'>"
            "<zone h='90000' w='90000' x='5000' y='5000' name='W' id='2' /></zone>"
            "</zones></dashboard>")
    parts = emit_pbir(parse_twb(_workbook(ws, dash)))
    assert _page_dims(parts)["Big"] == (1400, 1000)


def test_sizeless_dashboard_page_falls_back_to_1000x800_default():
    # §13.2: a dashboard that declares no <size> (automatic/range canvas) falls back to
    # Tableau's own 1000x800 default (DASH_DEFAULT_W/H), NOT the 1280x720 module default.
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    dash = ("<dashboard name='Auto'><zones>"
            "<zone h='100000' w='100000' x='0' y='0'>"
            "<zone h='90000' w='90000' x='5000' y='5000' name='W' id='2' /></zone>"
            "</zones></dashboard>")
    parts = emit_pbir(parse_twb(_workbook(ws, dash)))
    assert _page_dims(parts)["Auto"] == (1000, 800)


def test_orphan_page_stays_1280x720_after_a_sized_dashboard():
    # §13.2 reset: the per-dashboard override is cleared after the dashboard loop, so a
    # standalone (orphan) worksheet page keeps the 1280x720 default even when a sized
    # dashboard (1400x1000) was emitted just before it -- the override never leaks.
    ws1 = _worksheet("Placed", "Bar", "[federated.abc].[sum:Sales:qk]",
                     "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    ws2 = _worksheet("Orphan", "Bar", "[federated.abc].[sum:Profit:qk]",
                     "[federated.abc].[none:Region:nk]", deps_extra=_INST)
    dash = ("<dashboard name='D'><size maxheight='1000' maxwidth='1400' />"
            "<zones><zone h='100000' w='100000' x='0' y='0'>"
            "<zone h='90000' w='90000' x='5000' y='5000' name='Placed' id='2' /></zone>"
            "</zones></dashboard>")
    dims = _page_dims(emit_pbir(parse_twb(_workbook(ws1 + ws2, dash))))
    assert dims["D"] == (1400, 1000)       # dashboard page adopts <size>
    assert dims["Orphan"] == (1280, 720)   # orphan page keeps the default (no leak)


def test_dropdown_filter_card_height_is_floored_at_64():
    # §13.3: a scaled filter card (h=6000 -> 6000*0.008 = 48px) in Dropdown mode is
    # floored at SLICER_DROPDOWN_MIN_H (64) so Power BI never clips the control.
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='member' level='[none:Region:nk]' /></filter>")
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST, filters=filt)
    dash = _one_card_dashboard("W", "none:Region:nk", card_h=6000, card_y=90000)
    slicers = _page_slicers(emit_pbir(parse_twb(_workbook(ws, dash))))
    assert len(slicers) == 1
    assert slicers[0]["position"]["height"] == 64.0


def test_checklist_filter_card_height_tracks_the_scaled_zone():
    # §13.3: a NON-dropdown (checklist -> Basic/List) card takes its own scaled height
    # (48px), floored only at the smaller SLICER_CTRL_H (40) -- so it stays 48, NOT 64.
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='member' level='[none:Region:nk]' /></filter>")
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST, filters=filt)
    dash = _one_card_dashboard("W", "none:Region:nk", card_h=6000, card_y=90000,
                               mode="checklist")
    slicers = _page_slicers(emit_pbir(parse_twb(_workbook(ws, dash))))
    assert len(slicers) == 1
    assert slicers[0]["position"]["height"] == 48.0


def test_reflow_is_a_noop_when_the_slicer_band_clears_content():
    # §13.4: a worksheet authored well ABOVE the slicer band (no overlap) is untouched
    # -- content stays at its scaled top and never runs into the slicer band below it.
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='member' level='[none:Region:nk]' /></filter>")
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST, filters=filt)
    # worksheet zone h=60000 y=2000 -> [16, 496]; filter card y=90000 -> band ~[720, 784]
    card = ("<zone name='W' param='[federated.abc].[none:Region:nk]' type-v2='filter' "
            "h='6000' w='20000' x='5000' y='90000' id='30' />")
    inner = ("<zone h='100000' w='100000' x='0' y='0'>"
             "<zone h='60000' w='90000' x='5000' y='2000' name='W' id='2' />" + card + "</zone>")
    dash = ("<dashboard name='D'><size maxheight='800' maxwidth='1200' />"
            "<zones>" + inner + "</zones></dashboard>")
    parts = emit_pbir(parse_twb(_workbook(ws, dash)))
    content = _content_visual(parts)
    slicer = _page_slicers(parts)[0]
    assert content["position"]["y"] < 100  # stayed at its authored top (not pushed down)
    assert (content["position"]["y"] + content["position"]["height"]
            <= slicer["position"]["y"])    # clears the band entirely


def test_reflow_pushes_content_below_an_overlapping_slicer_band():
    # §13.4: a worksheet authored at its hidden-state position that now OVERLAPS the
    # shown slicer band is pushed below the band bottom (Tableau's "Show Filters" reflow).
    filt = ("<filter class='categorical' column='[federated.abc].[none:Region:nk]'>"
            "<groupfilter function='member' level='[none:Region:nk]' /></filter>")
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST, filters=filt)
    # worksheet zone h=90000 y=10000 -> [80, 800]; filter card y=20000 -> band ~[160, 224]
    card = ("<zone name='W' param='[federated.abc].[none:Region:nk]' type-v2='filter' "
            "h='6000' w='20000' x='5000' y='20000' id='30' />")
    inner = ("<zone h='100000' w='100000' x='0' y='0'>"
             "<zone h='90000' w='90000' x='5000' y='10000' name='W' id='2' />" + card + "</zone>")
    dash = ("<dashboard name='D'><size maxheight='800' maxwidth='1200' />"
            "<zones>" + inner + "</zones></dashboard>")
    parts = emit_pbir(parse_twb(_workbook(ws, dash)))
    content = _content_visual(parts)
    slicer = _page_slicers(parts)[0]
    assert (content["position"]["y"]
            >= slicer["position"]["y"] + slicer["position"]["height"])  # below the band


# -- dashboard text objects (§12) ----------------------------------------------
# The §12 tests pass a bare ``<zone …>…</zone>`` XML string straight to the parse-side readers, so a
# tiny helper turns that string into the ElementTree node those readers expect. The three fixtures are
# structurally faithful (but trimmed) Tableau dashboards: a wide+top filled banner (the header the
# selector picks), narrower/lower section-header caption bars with 8-digit ``#rrggbbaa`` fills, and a
# fill-less instruction line -- every text zone the rebuild must capture as its own textbox.
def _zone_from_xml(xml):
    return ET.fromstring(xml)


def _text_object_zone(text, fill=None, color="#ffffff", bold=True, size=12,
                      x=0, y=0, w=100000, h=6000, zid="9"):
    """A dashboard ``type='text'`` zone: an author-titled caption/banner/instruction box.

    ``fill`` optional -- a section-header bar carries an ``#rrggbb`` / 8-digit ``#rrggbbaa`` fill; a
    fill-less instruction line omits the ``<zone-style>`` entirely (transparent)."""
    run = (f"<run bold='{'true' if bold else 'false'}' fontcolor='{color}' "
           f"fontsize='{size}'>{text}</run>")
    style = (f"<zone-style><format attr='background-color' value='{fill}' /></zone-style>"
             if fill else "")
    return (f"<zone type-v2='text' h='{h}' w='{w}' x='{x}' y='{y}' id='{zid}'>"
            f"<formatted-text>{run}</formatted-text>{style}</zone>")


def _text_object_ws_zone(name, x=0, y=40000, w=100000, h=50000, zid="2"):
    return f"<zone h='{h}' w='{w}' x='{x}' y='{y}' name='{name}' id='{zid}' />"


def _text_object_container(*inner):
    return "<zone h='100000' w='100000' x='0' y='0'>" + "".join(inner) + "</zone>"


# Tech Hierarchy -- a top banner (wide+top, picked as the header) plus four role caption bars that are
# narrower AND lower, so the selector never mistakes them for the header. Director/Supervisor carry the
# two 8-digit fills (#5a23b9c1 -> transparency 24; #5a23b981 -> 49); the <size> fixes the pixel canvas.
TECH_HIERARCHY_TWB = _workbook(
    _worksheet("Placeholder", "Bar", "[federated.abc].[sum:Sales:qk]",
               "[federated.abc].[none:Category:nk]", deps_extra=_INST),
    "<dashboard name='Tech Hierarchy'><size maxwidth='1400' maxheight='1000' /><zones>"
    + _text_object_container(
        _text_object_ws_zone("Placeholder"),
        _text_object_zone("ATTI/ATTR Tech Hierarchy", fill="#5a23b9", color="#ffffff",
                          x=0, y=0, w=100000, h=9245, zid="99"),
        _text_object_zone("Director", fill="#5a23b9c1", color="#ffffff",
                          x=0, y=25000, w=25000, h=5000, zid="10"),
        _text_object_zone("Manager", fill="#5a23b9c1", color="#ffffff",
                          x=25000, y=25000, w=25000, h=5000, zid="11"),
        _text_object_zone("Supervisor", fill="#5a23b981", color="#ffffff",
                          x=50000, y=25000, w=25000, h=5000, zid="12"),
        _text_object_zone("Technician", fill="#5a23b9c1", color="#ffffff",
                          x=75000, y=25000, w=25000, h=5000, zid="13"),
    )
    + "</zones></dashboard>",
)


# Hierarchy Trending -- a fill-less instruction line (no <zone-style>, so fill is None) placed low over
# the trend worksheet; there is no filled top band, so this dashboard has no banner.
HIERARCHY_TRENDING_TWB = _workbook(
    _worksheet("Trend", "Line", "[federated.abc].[sum:Sales:qk]",
               "[federated.abc].[mn:Order Date:ok]", deps_extra=_INST),
    "<dashboard name='Hierarchy Trending'><size maxwidth='1400' maxheight='1000' /><zones>"
    + _text_object_container(
        _text_object_ws_zone("Trend"),
        _text_object_zone("Click on Director Name to Expand", fill=None, color="#000000",
                          bold=False, size=10, x=0, y=30000, w=40000, h=4000, zid="20"),
    )
    + "</zones></dashboard>",
)


# Banner Only -- the single text zone is the wide+top banner, so after de-dupe text_objects is empty.
BANNER_ONLY_TWB = _workbook(
    _worksheet("Solo", "Bar", "[federated.abc].[sum:Sales:qk]",
               "[federated.abc].[none:Category:nk]", deps_extra=_INST),
    "<dashboard name='Banner Only'><zones>"
    + _text_object_container(
        _text_object_ws_zone("Solo"),
        _text_object_zone("ATTI/ATTR Tech Hierarchy", fill="#5a23b9", color="#ffffff",
                          x=0, y=0, w=100000, h=9245, zid="99"),
    )
    + "</zones></dashboard>",
)


def test_hex8_fill_reader_splits_alpha():
    # #5a23b9c1 (alpha c1) -> ("#5a23b9", 24); #5a23b981 -> ("#5a23b9", 49);
    # 6-digit -> (hex, None); name/rgba -> (None, None)
    z_c1 = _zone_from_xml("<zone type-v2='text'><zone-style>"
                          "<format attr='background-color' value='#5a23b9c1'/></zone-style></zone>")
    assert _zone_background_fill2(z_c1) == ("#5a23b9", 24)
    z_81 = _zone_from_xml("<zone type-v2='text'><zone-style>"
                          "<format attr='background-color' value='#5a23b981'/></zone-style></zone>")
    assert _zone_background_fill2(z_81) == ("#5a23b9", 49)
    z6 = _zone_from_xml("<zone type-v2='text'><zone-style>"
                        "<format attr='background-color' value='#5a23b9'/></zone-style></zone>")
    assert _zone_background_fill2(z6) == ("#5a23b9", None)
    z_named = _zone_from_xml("<zone type-v2='text'><zone-style>"
                             "<format attr='background-color' value='red'/></zone-style></zone>")
    assert _zone_background_fill2(z_named) == (None, None)


def test_run_font_reads_weight_and_size():
    z = _zone_from_xml("<zone type-v2='text'><formatted-text>"
                       "<run bold='true' fontcolor='#ffffff' fontsize='12'>Director</run>"
                       "</formatted-text></zone>")
    assert _zone_run_font(z) == ("#ffffff", True, 12.0)


def test_all_text_objects_captured_tech_hierarchy():
    ir = parse_twb(TECH_HIERARCHY_TWB)
    db = next(d for d in ir["dashboards"] if d["name"] == "Tech Hierarchy")
    texts = {t["text"] for t in db["text_objects"]}
    assert {"Director", "Manager", "Supervisor", "Technician"} <= texts
    # banner is kept as title_banner and NOT duplicated in text_objects
    assert db["title_banner"]["text"] == "ATTI/ATTR Tech Hierarchy"
    assert not any(t["text"] == "ATTI/ATTR Tech Hierarchy" for t in db["text_objects"])
    # 8-digit fills split into rgb + transparency
    director = next(t for t in db["text_objects"] if t["text"] == "Director")
    assert director["fill"] == "#5a23b9" and director["transparency"] == 24
    supervisor = next(t for t in db["text_objects"] if t["text"] == "Supervisor")
    assert supervisor["transparency"] == 49


def test_fill_less_text_captured():
    ir = parse_twb(HIERARCHY_TRENDING_TWB)
    db = next(d for d in ir["dashboards"] if d["name"] == "Hierarchy Trending")
    instr = [t for t in db["text_objects"] if t["text"].startswith("Click on Director Name")]
    assert instr and any(t["fill"] is None or t["fill"] == "#5a23b9" for t in instr)


def test_text_object_emits_faithful_textbox():
    tob = {"text": "Director", "fill": "#5a23b9", "transparency": 24,
           "text_color": "#ffffff", "bold": True, "font_size": 12.0}
    vis = _text_object_textbox_visual("v-x-text-0", _position(0, 0, 100, 20, z=900, tab=1), tob)
    v = vis["visual"]
    assert v["visualType"] == "textbox"
    run = v["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"][0]
    assert run["value"] == "Director"
    assert run["textStyle"] == {"fontSize": "12pt", "color": "#ffffff", "fontWeight": "bold"}
    bg = v["visualContainerObjects"]["background"][0]["properties"]
    assert bg["show"]["expr"]["Literal"]["Value"] == "true"
    assert bg["color"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#5a23b9'"
    assert bg["transparency"]["expr"]["Literal"]["Value"] == "24D"


def test_fill_less_textbox_is_transparent():
    tob = {"text": "Click on Director Name to Expand", "fill": None, "transparency": None,
           "text_color": "#000000", "bold": False, "font_size": 10.0}
    vis = _text_object_textbox_visual("v-x-text-1", _position(0, 0, 100, 20, z=900, tab=1), tob)
    bg = vis["visual"]["visualContainerObjects"]["background"][0]["properties"]
    assert bg["show"]["expr"]["Literal"]["Value"] == "false"
    run = vis["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"][0]
    assert "fontWeight" not in run["textStyle"]   # not bold
    assert run["textStyle"]["fontSize"] == "10pt"


def test_banner_only_dashboard_never_regresses():
    # a dashboard whose only text zone is the top banner keeps text_objects empty after de-dupe,
    # so emit_pbir adds exactly one banner textbox and nothing else.
    ir = parse_twb(BANNER_ONLY_TWB)
    db = ir["dashboards"][0]
    assert db["text_objects"] == []


def test_dashboard_size_dict_survives_capture():
    # regression guard for the variable-name trap: capturing text objects must NOT clobber db["size"].
    ir = parse_twb(TECH_HIERARCHY_TWB)
    db = next(d for d in ir["dashboards"] if d["name"] == "Tech Hierarchy")
    assert isinstance(db["size"], dict) and "w" in db["size"] and "h" in db["size"]


# -- §13 dashboard image & button objects --------------------------------------------------------
_IMAGE_ZONE = ("<zone type-v2='bitmap' param='Image/Logo.png' h='9245' w='20000' "
               "x='0' y='0' id='40' />")
_BUTTON_ZONE = ("<zone type-v2='dashboard-object' id='41' x='30000' y='0' w='20000' h='9245'>"
                "<image-path>Image/Icon.png</image-path></zone>")

IMAGE_BUTTON_TWB = _workbook(
    _worksheet("Sheet1", "Bar", "[federated.abc].[sum:Sales:qk]",
               "[federated.abc].[none:Category:nk]", deps_extra=_INST),
    "<dashboard name='Cover'><size maxwidth='1200' maxheight='800' /><zones>"
    + _text_object_container(_text_object_ws_zone("Sheet1"), _IMAGE_ZONE, _BUTTON_ZONE, _IMAGE_ZONE)
    + "</zones></dashboard>",
)

_PNG = b"\x89PNG\r\n\x1a\nFAKE"
_PNG2 = b"\x89PNG\r\n\x1a\nICON"


def test_resource_basename_handles_slashes_and_none():
    assert _resource_basename("Image/Logo.png") == "Logo.png"
    assert _resource_basename("Image\\Sub\\Logo.png") == "Logo.png"
    assert _resource_basename("Logo.png") == "Logo.png"
    assert _resource_basename(None) == ""


def test_image_item_name_is_deterministic_collision_safe_and_fs_safe():
    n1 = _image_item_name("Image/EBI Logo Black.png", set())
    assert n1 == _image_item_name("Image/EBI Logo Black.png", set())
    assert " " not in n1 and n1.endswith(".png")
    assert n1 != _image_item_name("Image/EBI Logo Black.png", {n1})
    assert _image_item_name("Image/logo", set()).endswith(".png")


def test_resolve_resource_bytes_exact_ci_and_misses():
    res = {"Image/Logo.png": _PNG}
    assert _resolve_resource_bytes(res, "Image/Logo.png") == _PNG
    assert _resolve_resource_bytes(res, "other/logo.PNG") == _PNG
    assert _resolve_resource_bytes(res, None) is None
    assert _resolve_resource_bytes(None, "Image/Logo.png") is None
    assert _resolve_resource_bytes(res, "Image/Missing.png") is None


def test_image_visual_shape_has_resource_package_item_and_no_data_binding():
    vis = _image_visual("v-cover-img-40", _position(0, 0, 200, 100), "Logo0123.png")
    assert vis["$schema"] == SCHEMA_VISUAL
    v = vis["visual"]
    assert v["visualType"] == "image"
    assert v["drillFilterOtherVisuals"] is True
    rp = v["objects"]["general"][0]["properties"]["imageUrl"]["expr"]["ResourcePackageItem"]
    assert rp["PackageName"] == "RegisteredResources"
    assert rp["PackageType"] == 1
    assert rp["ItemName"] == "Logo0123.png"
    assert "query" not in v


def test_report_json_part_registers_image_items_else_none():
    part = report_json_part(image_items=[{"name": "x.png", "path": "x.png", "type": "Image"}])
    pkg = part["resourcePackages"][0]
    assert pkg["name"] == "RegisteredResources"
    assert "x.png" in [it["name"] for it in pkg["items"]]
    assert part["themeCollection"]["baseTheme"]["name"] == "CY24SU10"
    plain = report_json_part()
    assert "resourcePackages" not in plain
    assert plain["themeCollection"]["baseTheme"]["name"] == "CY24SU10"


def test_parse_captures_image_and_button_zones_deduped():
    ir = parse_twb(IMAGE_BUTTON_TWB)
    db = next(d for d in ir["dashboards"] if d["name"] == "Cover")
    zones = db["image_zones"]
    assert sorted(z["kind"] for z in zones) == ["button", "image"]
    img = next(z for z in zones if z["kind"] == "image")
    assert img["image"] == "Image/Logo.png"
    assert img["w"] > 0 and img["h"] > 0
    assert all(k in img for k in ("x", "y", "w", "h"))
    assert next(z for z in zones if z["kind"] == "button")["image"] == "Image/Icon.png"


def test_migrate_emits_image_visual_and_packages_bytes():
    result = migrate_twb_to_pbir(
        IMAGE_BUTTON_TWB, resources={"Image/Logo.png": _PNG, "Image/Icon.png": _PNG2})
    parts = result["parts"]
    image_vis = [v for v in _visual_parts(parts).values()
                 if v["visual"]["visualType"] == "image"]
    assert len(image_vis) == 2
    png_parts = {k: v for k, v in parts.items()
                 if k.startswith("StaticResources/RegisteredResources/")
                 and isinstance(v, (bytes, bytearray))}
    assert set(png_parts.values()) == {_PNG, _PNG2}
    report = json.loads(parts["definition/report.json"])
    image_items = [it for it in report["resourcePackages"][0]["items"] if it["type"] == "Image"]
    assert len(image_items) == 2


def test_image_object_fail_closed_when_bytes_missing():
    result = migrate_twb_to_pbir(IMAGE_BUTTON_TWB, resources={"Image/Nope.png": _PNG})
    assert not any(v["visual"]["visualType"] == "image"
                   for v in _visual_parts(result["parts"]).values())
    assert any("image object" in str(w)
               and "not rebuilt (image bytes not packaged with the workbook)" in str(w)
               for w in result["warnings"])


def test_image_objects_never_regress_when_resources_none():
    result = migrate_twb_to_pbir(IMAGE_BUTTON_TWB)
    assert not any(v["visual"]["visualType"] == "image"
                   for v in _visual_parts(result["parts"]).values())
    assert not any("image object" in str(w) for w in result["warnings"])
    report = json.loads(result["parts"]["definition/report.json"])
    pkgs = report.get("resourcePackages", [])
    image_items = [it for pkg in pkgs for it in pkg.get("items", []) if it.get("type") == "Image"]
    assert image_items == []
