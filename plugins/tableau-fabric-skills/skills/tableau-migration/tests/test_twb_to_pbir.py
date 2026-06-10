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
    emit_pbir,
    migrate_twb_to_pbir,
    parse_twb,
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


def _worksheet(name, mark, rows, cols, deps_extra="", encodings="", filters=""):
    return f"""
    <worksheet name='{name}'>
      <table>
        <view>
          <datasources>
            <datasource caption='Superstore' name='federated.abc' />
          </datasources>
          <datasource-dependencies datasource='federated.abc'>{_DEPS_COLUMNS}{deps_extra}
          </datasource-dependencies>
          {filters}
        </view>
        <panes><pane><mark class='{mark}' />{encodings}</pane></panes>
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
    ws = _worksheet("Area Chart", "Area",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    ir = parse_twb(_workbook(ws))
    assert ir["worksheets"][0]["visual_type"] == "unsupported"
    assert any(x["scope"] == "worksheet" and "Area" in x["reason"] for x in ir["warnings"])
    parts = emit_pbir(ir)
    assert _visual_parts(parts) == {}  # no visual emitted for the unsupported mark


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
    assert vis["visual"]["visualType"] == "filledMap"
    state = _query_state(vis)
    # Location = the geographic dimension; Color = the saturation measure
    assert state["Location"]["projections"][0]["field"]["Column"]["Property"] == "State"
    assert state["Color"]["projections"][0]["field"]["Aggregation"]["Function"] == 0
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
    assert state["Location"]["projections"][0]["field"]["Column"]["Property"] == "State"


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
