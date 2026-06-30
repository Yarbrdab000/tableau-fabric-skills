"""Quarantined tests for the advisory structural fidelity oracle (``fidelity_oracle``).

Hermetic: every fixture is built inline (a tiny Tableau ``.twb`` XML string + an on-disk PBIR
report tree under ``tmp_path``) so the suite never depends on the migration scratch outputs. These
tests are deliberately NOT under ``tests/`` -- ``pytest tests`` (the engine's green gate) must not
collect them, and the optional value/image tiers must degrade gracefully so importing the module
never fails offline.
"""
import json
import os
import time

import pytest

import fidelity_oracle as fo


# --------------------------------------------------------------------------- fixtures / helpers
def _ds_pill(inner):
    return "[fed.0abc].[%s]" % inner


TWB_XML = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <datasources>
    <datasource name='fed.0abc' caption='Sample'>
      <column name='[Calculation_99]' caption='My Ratio' datatype='real' role='measure'/>
      <column name='[Sales]' caption='Sales' datatype='real' role='measure'/>
      <column name='[Profit]' caption='Profit' datatype='real' role='measure'/>
      <column name='[Discount]' caption='Discount' datatype='real' role='measure'/>
      <column name='[Category]' caption='Category' datatype='string' role='dimension'/>
      <column name='[Order Date]' caption='Order Date' datatype='date' role='dimension'/>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Bars'>
      <table>
        <view>
          <datasources><datasource name='fed.0abc' caption='Sample'/></datasources>
          <filter class='categorical' column='[fed.0abc].[none:Category:nk]'/>
        </view>
        <panes>
          <pane>
            <mark class='Automatic'/>
            <encodings>
              <color column='[fed.0abc].[sum:Profit:qk]'/>
            </encodings>
          </pane>
        </panes>
        <rows>[fed.0abc].[none:Category:nk]</rows>
        <cols>[fed.0abc].[sum:Sales:qk]</cols>
      </table>
    </worksheet>
    <worksheet name='Trend'>
      <table>
        <view>
          <datasources><datasource name='fed.0abc' caption='Sample'/></datasources>
        </view>
        <panes>
          <pane>
            <mark class='Area'/>
            <encodings/>
          </pane>
        </panes>
        <rows>[fed.0abc].[sum:Sales:qk]</rows>
        <cols>[fed.0abc].[tmn:Order Date:qk]</cols>
      </table>
    </worksheet>
    <worksheet name='Card'>
      <table>
        <view>
          <datasources><datasource name='fed.0abc' caption='Sample'/></datasources>
          <datasource-dependencies datasource='fed.0abc'>
            <column name='[Discount]' datatype='real' role='measure'/>
            <column name='[Calculation_99]' datatype='real' role='measure'/>
            <column-instance column='[Discount]' derivation='Avg' name='[avg:Discount:qk]'/>
            <column-instance column='[Calculation_99]' derivation='User' name='[usr:Calculation_99:qk]'/>
          </datasource-dependencies>
        </view>
        <panes>
          <pane>
            <mark class='Automatic'/>
            <encodings>
              <text column='[fed.0abc].[:Measure Names]'/>
            </encodings>
          </pane>
        </panes>
        <rows></rows>
        <cols>[fed.0abc].[:Measure Names]</cols>
      </table>
    </worksheet>
  </worksheets>
  <dashboards>
    <dashboard name='Dash'>
      <size maxwidth='1000' maxheight='800'/>
      <zones>
        <zone x='0' y='0' w='100000' h='100000' type-v2='layout-basic'>
          <zone name='Bars' x='0' y='0' w='50000' h='100000'/>
          <zone name='Trend' x='50000' y='0' w='50000' h='100000'/>
        </zone>
      </zones>
    </dashboard>
  </dashboards>
</workbook>
"""


def _col_field(entity, prop):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def _agg_field(entity, prop, func=0):
    return {"Aggregation": {"Expression": {"Column": {
        "Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}, "Function": func}}


def _measure_field(entity, prop):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def _projection(field, native=None):
    return {"field": field, "queryRef": "q", "nativeQueryRef": native or "n"}


def _visual_json(name, vtype, position, query_state, filter_config=None):
    blob = {
        "name": name,
        "position": position,
        "visual": {"visualType": vtype, "query": {"queryState": query_state}},
    }
    if filter_config is not None:
        blob["filterConfig"] = filter_config
    return blob


def _write_pbir(base, page_display, visuals, page_name="page1", width=1280, height=720):
    """Write a minimal *.Report tree under ``base`` and return the .Report dir path."""
    report = os.path.join(base, "Sample.Report")
    pages_dir = os.path.join(report, "definition", "pages")
    os.makedirs(pages_dir)
    with open(os.path.join(pages_dir, "pages.json"), "w", encoding="utf-8") as fh:
        json.dump({"pageOrder": [page_name], "activePageName": page_name}, fh)
    pdir = os.path.join(pages_dir, page_name)
    os.makedirs(pdir)
    with open(os.path.join(pdir, "page.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": page_name, "displayName": page_display,
                   "width": width, "height": height}, fh)
    for v in visuals:
        vdir = os.path.join(pdir, "visuals", v["name"])
        os.makedirs(vdir)
        with open(os.path.join(vdir, "visual.json"), "w", encoding="utf-8") as fh:
            json.dump(v, fh)
    return report


def _faithful_visuals():
    """PBIR visuals that faithfully rebuild the TWB_XML dashboard (Bars + Trend)."""
    bars = _visual_json(
        "v-bars", "clusteredBarChart",
        {"x": 0.0, "y": 0.0, "width": 640.0, "height": 720.0, "z": 0},
        {"Category": {"projections": [_projection(_col_field("fed.0abc", "Category"), "Category")]},
         "Y": {"projections": [_projection(_agg_field("fed.0abc", "Sales"), "Sum of Sales"),
                               _projection(_agg_field("fed.0abc", "Profit"), "Sum of Profit")]}})
    trend = _visual_json(
        "v-trend", "areaChart",
        {"x": 640.0, "y": 0.0, "width": 640.0, "height": 720.0, "z": 0},
        {"Category": {"projections": [_projection(_col_field("fed.0abc", "Order_Date"), "Order_Date")]},
         "Y": {"projections": [_projection(_agg_field("fed.0abc", "Sales"), "Sum of Sales")]}})
    return [bars, trend]


# --------------------------------------------------------------------------- normalization / fields
def test_norm_collapses_separators():
    assert fo._norm("Order Date") == fo._norm("Order_Date") == "orderdate"
    assert fo._norm("Country/Region") == "countryregion"
    assert fo._norm("Sub-Category") == "subcategory"


def test_pbir_extract_field_shapes():
    col = fo._pbir_extract_field(_col_field("E", "City"))
    assert col["kind"] == "column" and col["is_measure"] is False and col["norm"] == "city"
    agg = fo._pbir_extract_field(_agg_field("E", "Sales", 0))
    assert agg["kind"] == "aggregation" and agg["is_measure"] is True
    mea = fo._pbir_extract_field(_measure_field("M", "Ratio"))
    assert mea["kind"] == "measure" and mea["is_measure"] is True
    assert fo._pbir_extract_field({"junk": 1}) is None


# --------------------------------------------------------------------------- PBIR reader
def test_read_pbir_report_normalizes_positions(tmp_path):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    parsed = fo.read_pbir_report(report)
    assert len(parsed["pages"]) == 1
    page = parsed["pages"][0]
    assert page["display"] == "Dash"
    bars = next(v for v in page["visuals"] if v["name"] == "v-bars")
    assert bars["family"] == fo.FAM_BAR
    assert bars["nposition"]["w"] == pytest.approx(0.5)
    assert {f["norm"] for f in bars["fields"]} == {"category", "sales", "profit"}


def test_read_pbir_report_accepts_parent_dir(tmp_path):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    # passing the parent (which contains exactly one *.Report) resolves to the same report
    parsed = fo.read_pbir_report(str(tmp_path))
    assert parsed["report_name"] == os.path.basename(report)


def test_pbir_extract_field_tolerates_malformed_aggregation():
    # "Aggregation" present but NOT a dict (a malformed visual.json) must not raise; the field is
    # still recognized as a measure and its aggregation function is simply absent.
    fld = fo._pbir_extract_field({"Aggregation": "notadict", "Property": "Sales"})
    assert fld is not None and fld["is_measure"] is True and fld["norm"] == "sales"
    assert fld["agg"] is None


def test_pbir_read_visual_tolerates_wrong_types(tmp_path):
    # A structurally valid JSON whose position/queryState/filterConfig carry the WRONG types must
    # not raise -- the reader coerces each and returns a (possibly empty) record.
    blob = {
        "name": "v-bad",
        "position": [],                                              # not a dict
        "visual": {"visualType": "barChart", "query": {"queryState": [1, 2, 3]}},  # list, not dict
        "filterConfig": "nope",                                     # not a dict
    }
    vdir = tmp_path / "v-bad"
    vdir.mkdir()
    (vdir / "visual.json").write_text(json.dumps(blob), encoding="utf-8")
    rec = fo._pbir_read_visual(str(vdir / "visual.json"))
    assert rec is not None
    assert rec["visual_type"] == "barChart"
    assert rec["fields"] == [] and rec["filter_fields"] == []
    assert rec["position"]["x"] is None                            # malformed position coerced, no crash


def test_read_pbir_report_isolates_malformed_visual(tmp_path):
    # One visual whose projections are the wrong type must neither crash the run nor drop the good
    # visuals -- the advisory reader isolates the bad one and keeps scoring the rest.
    bad = {
        "name": "v-bad",
        "position": {"x": 0, "y": 0, "width": 10, "height": 10, "z": 0},
        "visual": {"visualType": "barChart",
                   "query": {"queryState": {"Y": {"projections": "notalist"}}}},
    }
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals() + [bad])
    parsed = fo.read_pbir_report(report)                            # must not raise
    names = {v["name"] for v in parsed["pages"][0]["visuals"]}
    assert "v-bars" in names and "v-trend" in names                # good visuals preserved
    bad_rec = next(v for v in parsed["pages"][0]["visuals"] if v["name"] == "v-bad")
    assert bad_rec["fields"] == []                                 # bad projections coerced to empty


# --------------------------------------------------------------------------- TWB reader
def test_read_twb_worksheets_and_families():
    twb = fo.read_twb_views(TWB_XML)
    ws = twb["worksheets"]
    assert set(ws) == {"Bars", "Trend", "Card"}
    assert ws["Bars"]["family"] == fo.FAM_BAR
    assert ws["Trend"]["family"] == fo.FAM_AREA and ws["Trend"]["family_asserted"] is True
    # The "Card" worksheet lays out Measure Names + 2 Measure Values (Discount + My Ratio): that is a
    # measures TABLE (faithful rebuild = a Power BI table/tableEx), not a single-value card.
    assert ws["Card"]["family"] == fo.FAM_TABLE
    assert {f["norm"] for f in ws["Bars"]["fields"]} == {"category", "sales", "profit"}
    assert {f["norm"] for f in ws["Bars"]["measures"]} == {"sales", "profit"}
    assert {f["norm"] for f in ws["Bars"]["dims"]} == {"category"}


def test_twb_caption_resolution_for_calc_member():
    twb = fo.read_twb_views(TWB_XML)
    card = twb["worksheets"]["Card"]
    # the Measure Values table resolves [Calculation_99] -> caption 'My Ratio', plus Discount
    norms = {f["norm"] for f in card["fields"]}
    assert "myratio" in norms and "discount" in norms


def test_infer_family_measure_table_vs_single_card():
    # Measure Names + multiple Measure Values is a measures TABLE (faithful rebuild: a Power BI
    # table/tableEx), so tableEx scores as a type-match rather than a card->table substitution.
    fam, asserted = fo._infer_twb_family(
        "Text", [], [{"norm": "sales"}, {"norm": "profit"}], False, True)
    assert fam == fo.FAM_TABLE and asserted is True
    # The same shape under an Automatic mark is also a measures table.
    fam_a, asserted_a = fo._infer_twb_family(
        "Automatic", [], [{"norm": "sales"}, {"norm": "profit"}], False, True)
    assert fam_a == fo.FAM_TABLE and asserted_a is True
    # A single dimensionless Measure Value is still a card, not a table.
    fam_c, asserted_c = fo._infer_twb_family("Text", [], [{"norm": "sales"}], False, True)
    assert fam_c == fo.FAM_CARD and asserted_c is True


def test_twb_dashboard_zones_normalized():
    twb = fo.read_twb_views(TWB_XML)
    dash = twb["dashboards"][0]
    assert dash["name"] == "Dash"
    zmap = {z["worksheet"]: z for z in dash["zones"]}
    assert set(zmap) == {"Bars", "Trend"}
    assert zmap["Bars"]["nposition"]["w"] == pytest.approx(0.5)
    assert zmap["Trend"]["nposition"]["x"] == pytest.approx(0.5)


def test_object_id_and_generated_fields_excluded():
    twb = fo.read_twb_views(TWB_XML.replace(
        "<rows>[fed.0abc].[none:Category:nk]</rows>",
        "<rows>([fed.0abc].[none:Category:nk] * [fed.0abc].[none:__tableau_internal_object_id__:nk])</rows>"
    ).replace(
        "<color column='[fed.0abc].[sum:Profit:qk]'/>",
        "<color column='[fed.0abc].[sum:Profit:qk]'/><lod column='[fed.0abc].[Latitude (generated)]'/>"
    ))
    norms = {f["norm"] for f in twb["worksheets"]["Bars"]["fields"]}
    assert "tableauinternalobjectid" not in norms
    assert not any("generated" in n for n in norms)


def test_lod_measure_encoding_excluded_dimension_kept():
    # A MEASURE on the <lod> channel backs a reference-line distribution band (e.g. a WINDOW_STDEV
    # decoration), not a visible mark encoding -- it must be excluded so a faithful rebuild is not
    # charged for omitting it. A genuine detail DIMENSION on <lod> is a real binding and is kept.
    xml = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<workbook><datasources>"
        "<datasource name='fed.0abc' caption='Sample'>"
        "<column name='[Sales]' caption='Sales' datatype='real' role='measure'/>"
        "<column name='[Std Dev]' caption='Std Dev' datatype='real' role='measure'/>"
        "<column name='[Region]' caption='Region' datatype='string' role='dimension'/>"
        "<column name='[Order Date]' caption='Order Date' datatype='date' role='dimension'/>"
        "</datasource></datasources><worksheets>"
        "<worksheet name='Line'><table>"
        "<view><datasources><datasource name='fed.0abc' caption='Sample'/></datasources></view>"
        "<panes><pane><mark class='Line'/><encodings>"
        "<lod column='[fed.0abc].[sum:Std Dev:qk]'/>"   # measure on lod -> excluded
        "<lod column='[fed.0abc].[none:Region:nk]'/>"   # dimension on lod -> kept
        "</encodings></pane></panes>"
        "<rows>[fed.0abc].[sum:Sales:qk]</rows>"
        "<cols>[fed.0abc].[tmn:Order Date:qk]</cols>"
        "</table></worksheet></worksheets></workbook>"
    )
    ws = fo.read_twb_views(xml)["worksheets"]["Line"]
    norms = {f["norm"] for f in ws["fields"]}
    assert "stddev" not in norms          # lod MEASURE excluded (reference-line decoration)
    assert "region" in norms              # lod DIMENSION kept (genuine detail)
    assert "sales" in norms and "orderdate" in norms
    assert "stddev" not in {f["norm"] for f in ws["measures"]}


def test_dual_axis_secondary_pane_encodings_collected():
    # A dual-axis worksheet emits one <pane> per axis, each with its own <encodings>. The parser
    # must read EVERY pane, not just the first, or the secondary axis's color/size/detail bindings
    # are silently dropped. The mark is taken from the first pane for family inference.
    xml = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<workbook><datasources>"
        "<datasource name='fed.0abc' caption='Sample'>"
        "<column name='[Sales]' caption='Sales' datatype='real' role='measure'/>"
        "<column name='[Profit]' caption='Profit' datatype='real' role='measure'/>"
        "<column name='[Region]' caption='Region' datatype='string' role='dimension'/>"
        "<column name='[Segment]' caption='Segment' datatype='string' role='dimension'/>"
        "</datasource></datasources><worksheets>"
        "<worksheet name='Dual'><table>"
        "<view><datasources><datasource name='fed.0abc' caption='Sample'/></datasources></view>"
        "<panes>"
        "<pane><mark class='Line'/><encodings>"
        "<color column='[fed.0abc].[none:Region:nk]'/></encodings></pane>"
        "<pane><mark class='Bar'/><encodings>"
        "<color column='[fed.0abc].[none:Segment:nk]'/></encodings></pane>"
        "</panes>"
        "<rows>([fed.0abc].[sum:Sales:qk] + [fed.0abc].[sum:Profit:qk])</rows>"
        "<cols>[fed.0abc].[none:Region:nk]</cols>"
        "</table></worksheet></worksheets></workbook>"
    )
    ws = fo.read_twb_views(xml)["worksheets"]["Dual"]
    norms = {f["norm"] for f in ws["fields"]}
    assert "segment" in norms              # secondary-pane color must NOT be dropped
    assert "region" in norms
    assert "sales" in norms and "profit" in norms
    assert ws["mark"] == "Line"            # mark taken from the FIRST pane


def test_infer_family_card_when_no_dims():
    fam, asserted = fo._infer_twb_family("Automatic", [], [{"norm": "sales"}], False, False)
    assert fam == fo.FAM_CARD and asserted is True
    fam2, asserted2 = fo._infer_twb_family("Automatic", [{"norm": "cat"}], [{"norm": "s"}], False, False)
    assert fam2 == fo.FAM_BAR and asserted2 is False  # plausible, not asserted


def test_infer_family_square_is_highlight_table_matrix():
    # A Tableau Square mark with axis dimensions is a highlight table -> Power BI matrix (the real
    # Comcast "Segment % Dod" case): it must NOT misinfer as an unasserted bar.
    fam, asserted = fo._infer_twb_family(
        "Square", [{"norm": "segment"}], [{"norm": "pct"}], False, False)
    assert fam == fo.FAM_MATRIX and asserted is True
    # Square without dimensions (treemap/density) stays unasserted rather than guessing a matrix.
    fam2, asserted2 = fo._infer_twb_family("Square", [], [{"norm": "pct"}], False, False)
    assert fam2 == fo.FAM_UNKNOWN and asserted2 is False


def test_type_score_square_highlight_table_matches_pivot_table():
    # The highlight-table worksheet (matrix) vs an emitted pivotTable (matrix) is a clean match,
    # not a misleading "bar?/matrix" unasserted partial.
    ht_ws = {"family": fo.FAM_MATRIX, "family_asserted": True}
    pivot_v = {"family": fo.FAM_MATRIX}
    score, note = fo._type_score(ht_ws, pivot_v)
    assert score == pytest.approx(1.0) and note == "type-match"


def test_parse_pill_captures_continuous_flag():
    # ``qk`` (quantitative) = continuous green pill; ``ok``/``nk`` = discrete blue pill. The same
    # date-truncation derivation (``tdy``) appears in both forms, so the typekey is what decides.
    assert fo._parse_pill("tdy:Order Date:qk", {})["continuous"] is True
    assert fo._parse_pill("tdy:Order Date:ok", {})["continuous"] is False
    assert fo._parse_pill("none:Segment:nk", {})["continuous"] is False


def test_infer_family_continuous_date_automatic_is_line():
    # Automatic mark over a continuous (green) date axis is Tableau's default line chart (the real
    # Comcast "Line chart" anchor: tdy:Order Date:qk). It must assert FAM_LINE, not an unasserted bar.
    date_dim = {"norm": "orderdate", "deriv": "tdy", "is_measure": False, "continuous": True}
    fam, asserted = fo._infer_twb_family(
        "Automatic", [date_dim], [{"norm": "stdev"}], False, False)
    assert fam == fo.FAM_LINE and asserted is True
    # A lone continuous-date dimension (implicit COUNT drawn as a line) still reads as a line.
    fam2, asserted2 = fo._infer_twb_family("Automatic", [date_dim], [], False, False)
    assert fam2 == fo.FAM_LINE and asserted2 is True


def test_infer_family_discrete_date_automatic_is_not_line():
    # A discrete (ordinal) date axis under an Automatic mark is NOT a line -- it falls back to the
    # conservative unasserted bar (the Comcast "Segment % Dod" date is tdy:...:ok = discrete).
    disc_date = {"norm": "orderdate", "deriv": "tdy", "is_measure": False, "continuous": False}
    fam, asserted = fo._infer_twb_family(
        "Automatic", [disc_date], [{"norm": "pct"}], False, False)
    assert fam == fo.FAM_BAR and asserted is False


def test_infer_family_text_mark_continuous_date_stays_table():
    # An explicit Text mark wins over the continuous date: Comcast "Line chart (2)/(3)" carry a
    # continuous date (tdy:...:qk) but a Text mark, so they are tables -- NOT lines.
    date_dim = {"norm": "orderdate", "deriv": "tdy", "is_measure": False, "continuous": True}
    fam, asserted = fo._infer_twb_family(
        "Text", [{"norm": "ent"}, date_dim], [{"norm": "cnt"}], False, False)
    assert fam == fo.FAM_TABLE and asserted is True


def test_type_score_line_anchor_matches_line_chart():
    # The genuine-line worksheet (asserted FAM_LINE) vs an emitted lineChart is a clean type-match,
    # lifting the faithful-end anchor sheet off the 0.85 unasserted credit.
    line_ws = {"family": fo.FAM_LINE, "family_asserted": True}
    line_v = {"family": fo.FAM_LINE}
    score, note = fo._type_score(line_ws, line_v)
    assert score == pytest.approx(1.0) and note == "type-match"


# ------------------------------------------------------ remodel/rename advisory diagnosis
def test_score_pair_flags_remodel_rename():
    # Strong type-match + low field-NAME overlap = the faithful star-schema remodel signature
    # (Tableau "Order Date"/implicit COUNT -> a "Date" dimension + a "count orders" measure).
    twb_ws = {
        "name": "Line chart", "family": fo.FAM_LINE, "family_asserted": True,
        "fields": [{"norm": "orderdate"}, {"norm": "countorders"}],
        "dims": [{"norm": "orderdate"}], "measures": [{"norm": "countorders"}],
    }
    pbir_visual = {
        "name": "v1", "visual_type": "lineChart", "family": fo.FAM_LINE,
        "fields": [{"norm": "date", "is_measure": False},
                   {"norm": "countordersmeasure", "is_measure": True}],
    }
    r = fo._score_pair(twb_ws, pbir_visual, None)
    assert r["components"]["type"] == pytest.approx(1.0)
    assert r["components"]["fields"] == pytest.approx(0.0)
    assert r["diagnosis"] == fo._REMODEL_DIAGNOSIS


def test_score_pair_no_remodel_flag_when_fields_match():
    # Faithful AND same field names -> nothing to diagnose; the flag stays off.
    twb_ws = {
        "name": "Bars", "family": fo.FAM_BAR, "family_asserted": True,
        "fields": [{"norm": "category"}, {"norm": "sales"}],
        "dims": [{"norm": "category"}], "measures": [{"norm": "sales"}],
    }
    pbir_visual = {
        "name": "v2", "visual_type": "barChart", "family": fo.FAM_BAR,
        "fields": [{"norm": "category", "is_measure": False},
                   {"norm": "sales", "is_measure": True}],
    }
    r = fo._score_pair(twb_ws, pbir_visual, None)
    assert r["diagnosis"] is None


def test_score_pair_no_remodel_flag_on_type_mismatch():
    # A genuine type divergence must NOT be excused as a rename, even with zero field overlap.
    twb_ws = {
        "name": "Bars", "family": fo.FAM_BAR, "family_asserted": True,
        "fields": [{"norm": "category"}], "dims": [{"norm": "category"}], "measures": [],
    }
    pbir_visual = {
        "name": "v3", "visual_type": "pieChart", "family": fo.FAM_PIE,
        "fields": [{"norm": "segment", "is_measure": False}],
    }
    r = fo._score_pair(twb_ws, pbir_visual, None)
    assert r["components"]["type"] == pytest.approx(0.0)
    assert r["diagnosis"] is None


# --------------------------------------- geographic map: finest bound geo level satisfies ancestors
def _geo_map_ws():
    # A filled-map worksheet: Country/Region + State/Province on Marks detail (lod), Profit on color.
    return {
        "name": "Sheet 3", "family": fo.FAM_MAP, "family_asserted": True, "has_geometry": True,
        "fields": [{"norm": "countryregion", "is_measure": False, "channel": "lod"},
                   {"norm": "stateprovince", "is_measure": False, "channel": "lod"},
                   {"norm": "profit", "is_measure": True, "channel": "color"}],
        "dims": [{"norm": "countryregion", "is_measure": False, "channel": "lod"},
                 {"norm": "stateprovince", "is_measure": False, "channel": "lod"}],
        "measures": [{"norm": "profit", "is_measure": True, "channel": "color"}],
    }


def test_geo_rank_known_levels_only():
    assert fo._geo_rank("countryregion") == 0 and fo._geo_rank("country") == 0
    assert fo._geo_rank("stateprovince") == 1 and fo._geo_rank("state") == 1
    assert fo._geo_rank("city") == 3
    # "region" alone is a categorical group (Central/East/...), not a geocoded level -> non-geo.
    assert fo._geo_rank("region") is None and fo._geo_rank("sales") is None


def test_geo_ancestor_satisfied_when_finer_level_bound():
    # A filledMap binds State/Province (finest) + Profit. Country/Region (only on detail) is implied
    # by geocoding the finer level -> not a missing field; the map scores a clean REPRODUCED.
    ws = _geo_map_ws()
    v = {"name": "v-map", "visual_type": "filledMap", "family": fo.FAM_MAP,
         "fields": [{"norm": "stateprovince", "is_measure": False},
                    {"norm": "profit", "is_measure": True}]}
    r = fo._score_pair(ws, v, None)
    assert r["fields_missing"] == [] and "countryregion" not in r["fields_missing"]
    assert r["geo_implied"] == ["countryregion"]
    assert r["components"]["type"] == pytest.approx(1.0)
    assert fo.classify_visual_state(r)[0] == fo.STATE_REPRODUCED


def test_geo_ancestor_not_suppressed_when_only_coarser_level_bound():
    # The honest negative: a Country-grain shapeMap (binds only Country/Region) must NOT have the
    # finer, genuinely-absent State/Province masked -> it stays missing (a real grain gap).
    ws = _geo_map_ws()
    v = {"name": "v-map", "visual_type": "shapeMap", "family": fo.FAM_MAP,
         "fields": [{"norm": "countryregion", "is_measure": False},
                    {"norm": "profit", "is_measure": True}]}
    r = fo._score_pair(ws, v, None)
    assert "stateprovince" in r["fields_missing"]
    assert "geo_implied" not in r


def test_geo_ancestor_only_on_geographic_map():
    # Map-gated: the same field shape on a non-map worksheet (no geometry) suppresses nothing.
    ws = dict(_geo_map_ws())
    ws["has_geometry"] = False
    ws["family"] = fo.FAM_TABLE
    v = {"name": "v-tbl", "visual_type": "tableEx", "family": fo.FAM_TABLE,
         "fields": [{"norm": "stateprovince", "is_measure": False},
                    {"norm": "profit", "is_measure": True}]}
    r = fo._score_pair(ws, v, None)
    assert "countryregion" in r["fields_missing"]
    assert "geo_implied" not in r


def test_geo_ancestor_on_strong_channel_not_suppressed():
    # Guard: a coarser geo level carried on an independent encoding (color, not detail) is a real
    # binding -- a choropleth-by-Country -- so it is never suppressed even under a finer bound level.
    ws = _geo_map_ws()
    ws["fields"][0]["channel"] = "color"
    ws["dims"][0]["channel"] = "color"
    v = {"name": "v-map", "visual_type": "filledMap", "family": fo.FAM_MAP,
         "fields": [{"norm": "stateprovince", "is_measure": False},
                    {"norm": "profit", "is_measure": True}]}
    r = fo._score_pair(ws, v, None)
    assert "countryregion" in r["fields_missing"]
    assert "geo_implied" not in r


# ---------------------------------- date axis rebound onto a related Date dimension via active rel
def _date_rel(from_table, from_col, to_table, to_col, active=True):
    """Build a normalized relationship record in the shape _parse_tmdl_relationships emits."""
    return {"active": active, "date_behavior": "datePartOnly",
            "from_table": from_table, "from_col": from_col, "from_norm": fo._norm(from_col),
            "to_table": to_table, "to_table_norm": fo._norm(to_table),
            "to_col": to_col, "to_norm": fo._norm(to_col)}


def _date_axis_ws():
    # An area worksheet: a continuous Order Date axis (date-truncation green pill) + Region small
    # multiple + Sales/Profit. The rebuild rebinds the continuous order-date axis onto a Date dim.
    return {
        "name": "Sheet 2", "family": fo.FAM_AREA, "family_asserted": True, "has_geometry": False,
        "fields": [{"norm": "orderdate", "is_measure": False, "deriv": "tmn", "continuous": True},
                   {"norm": "region", "is_measure": False, "deriv": "none"},
                   {"norm": "sales", "is_measure": True, "deriv": "sum"},
                   {"norm": "profit", "is_measure": True, "deriv": "sum"}],
        "dims": [{"norm": "orderdate", "is_measure": False, "deriv": "tmn", "continuous": True},
                 {"norm": "region", "is_measure": False, "deriv": "none"}],
        "measures": [{"norm": "sales", "is_measure": True, "deriv": "sum"},
                     {"norm": "profit", "is_measure": True, "deriv": "sum"}],
    }


def _date_axis_visual():
    # The rebuilt area: the date axis is bound to Date[Date] (a related dimension), the rest by name.
    return {"name": "v-area", "visual_type": "areaChart", "family": fo.FAM_AREA,
            "fields": [{"norm": "date", "is_measure": False, "entity": "Date"},
                       {"norm": "region", "is_measure": False, "entity": "Orders"},
                       {"norm": "sales", "is_measure": True, "entity": "Orders"},
                       {"norm": "profit", "is_measure": True, "entity": "Orders"}]}


def test_split_tmdl_colref_variants():
    assert fo._split_tmdl_colref("Orders.Order_Date") == ("Orders", "Order_Date")
    assert fo._split_tmdl_colref("'Date Dim'.Date") == ("Date Dim", "Date")
    assert fo._split_tmdl_colref("Orders.'Order Date'") == ("Orders", "Order Date")
    assert fo._split_tmdl_colref("") == (None, None)


def test_parse_tmdl_relationships_grammar(tmp_path):
    defn = tmp_path / "definition"
    defn.mkdir()
    (defn / "relationships.tmdl").write_text(
        "relationship aaa\n"
        "\tjoinOnDateBehavior: datePartOnly\n"
        "\tfromColumn: Orders.Order_Date\n"
        "\ttoColumn: Date.Date\n"
        "\n"
        "relationship bbb\n"
        "\tisActive: false\n"
        "\tfromColumn: Orders.Ship_Date\n"
        "\ttoColumn: 'Date'.Date\n",
        encoding="utf-8")
    rels = fo._parse_tmdl_relationships(str(defn))
    assert len(rels) == 2
    a, b = rels
    assert a["active"] is True and a["from_norm"] == "orderdate" and a["to_table_norm"] == "date"
    assert a["date_behavior"] == "datePartOnly"
    assert b["active"] is False and b["from_norm"] == "shipdate" and b["to_table_norm"] == "date"


def test_parse_tmdl_relationships_missing_file(tmp_path):
    # A missing/absent relationships.tmdl (or None) yields [] -> scoring stays relationship-blind.
    assert fo._parse_tmdl_relationships(str(tmp_path)) == []
    assert fo._parse_tmdl_relationships(None) == []


def test_date_axis_credited_via_active_relationship():
    # The faithful star pattern: the order-date axis is rebound onto Date[Date], and an ACTIVE
    # relationship runs Orders.Order_Date -> Date.Date -> the source date field is reproduced.
    ws, v = _date_axis_ws(), _date_axis_visual()
    rels = [_date_rel("Orders", "Order_Date", "Date", "Date", active=True)]
    r = fo._score_pair(ws, v, None, relationships=rels)
    assert r["date_implied"] == ["orderdate"]
    assert r["fields_missing"] == []
    assert r["components"]["type"] == pytest.approx(1.0)
    assert fo.classify_visual_state(r)[0] == fo.STATE_REPRODUCED


def test_date_axis_not_credited_when_only_inactive_relationship():
    # The honest negative (mirrors geo): an axis backed ONLY by an inactive (isActive:false) rel --
    # e.g. a secondary Ship Date role-playing relationship -- is NOT credited; orderdate stays
    # missing and the visual stays PARTIAL, so a real grain/field gap is never masked.
    ws, v = _date_axis_ws(), _date_axis_visual()
    rels = [_date_rel("Orders", "Order_Date", "Date", "Date", active=False),
            _date_rel("Orders", "Ship_Date", "Date", "Date", active=False)]
    r = fo._score_pair(ws, v, None, relationships=rels)
    assert "orderdate" in r["fields_missing"]
    assert "date_implied" not in r
    assert fo.classify_visual_state(r)[0] == fo.STATE_PARTIAL


def test_date_axis_not_credited_when_active_relationship_is_other_field():
    # Active rel runs from Ship_Date (not the Order Date actually on the axis): the order-date axis
    # has no active relationship of its own -> it correctly stays missing.
    ws, v = _date_axis_ws(), _date_axis_visual()
    rels = [_date_rel("Orders", "Ship_Date", "Date", "Date", active=True)]
    r = fo._score_pair(ws, v, None, relationships=rels)
    assert "orderdate" in r["fields_missing"]
    assert "date_implied" not in r


def test_date_axis_not_credited_when_visual_binds_no_date_table():
    # The active rel exists, but the rebuilt visual binds no Date-dimension entity at all -> there is
    # no related date table standing in for the dropped axis, so nothing is credited.
    ws = _date_axis_ws()
    v = {"name": "v-area", "visual_type": "areaChart", "family": fo.FAM_AREA,
         "fields": [{"norm": "region", "is_measure": False, "entity": "Orders"},
                    {"norm": "sales", "is_measure": True, "entity": "Orders"}]}
    rels = [_date_rel("Orders", "Order_Date", "Date", "Date", active=True)]
    r = fo._score_pair(ws, v, None, relationships=rels)
    assert "orderdate" in r["fields_missing"]
    assert "date_implied" not in r


def test_date_implied_only_credits_date_truncation_axes():
    # A non-date categorical dim the rebuild dropped is NOT laundered by a relationship: only a
    # date-truncation axis qualifies, so the implied credit cannot mask an ordinary field gap.
    ws = {
        "name": "Sheet X", "family": fo.FAM_BAR, "family_asserted": True, "has_geometry": False,
        "fields": [{"norm": "segment", "is_measure": False, "deriv": "none"},
                   {"norm": "sales", "is_measure": True, "deriv": "sum"}],
        "dims": [{"norm": "segment", "is_measure": False, "deriv": "none"}],
        "measures": [{"norm": "sales", "is_measure": True, "deriv": "sum"}],
    }
    v = {"name": "v", "visual_type": "barChart", "family": fo.FAM_BAR,
         "fields": [{"norm": "segmentkey", "is_measure": False, "entity": "SegmentDim"},
                    {"norm": "sales", "is_measure": True, "entity": "Orders"}]}
    rels = [_date_rel("Orders", "Segment", "SegmentDim", "SegmentKey", active=True)]
    r = fo._score_pair(ws, v, None, relationships=rels)
    assert "segment" in r["fields_missing"]
    assert "date_implied" not in r


def test_date_implied_noop_without_relationships():
    # Default (relationship-blind) scoring is unchanged: no relationships -> orderdate stays missing.
    ws, v = _date_axis_ws(), _date_axis_visual()
    r = fo._score_pair(ws, v, None)
    assert "orderdate" in r["fields_missing"]
    assert "date_implied" not in r


def test_assemble_report_counts_remodel_suspected():
    twb = {"worksheets": [{"name": "A"}]}
    vis = [{"worksheet": "A", "score": 0.45, "diagnosis": fo._REMODEL_DIAGNOSIS}]
    rep = fo._assemble_report(twb, {}, vis, [], [], [], None)
    assert rep["summary"]["remodel_rename_suspected"] == 1
    assert any("remodel" in n.lower() for n in rep["notes"])


def test_assemble_report_no_remodel_note_when_clean():
    twb = {"worksheets": [{"name": "A"}]}
    vis = [{"worksheet": "A", "score": 0.95, "diagnosis": None}]
    rep = fo._assemble_report(twb, {}, vis, [], [], [], None)
    assert rep["summary"]["remodel_rename_suspected"] == 0
    assert not any("remodel" in n.lower() for n in rep["notes"])


# ------------------------------------------ field-alias resolution (see through a faithful rename)
def test_aliases_from_candidate_records_merges_and_tolerates_missing():
    recs = [
        {"worksheet": "Line chart", "field_aliases": {"Date.Date": "Order Date"}},
        {"worksheet": "X", "field_aliases": {"_Measures.count orders": "Orders"}},
        {"worksheet": "Y"},        # a record predating the producer -> no field_aliases key
        "not-a-dict",
    ]
    merged = fo.aliases_from_candidate_records(recs)
    assert merged == {"Date.Date": "Order Date", "_Measures.count orders": "Orders"}
    assert fo.aliases_from_candidate_records([]) == {}
    assert fo.aliases_from_candidate_records(None) == {}


def test_aliased_norm_prefers_full_ref_not_bare_property():
    lookup = fo._alias_lookup({"Date.Date": "Order Date"})
    fld = {"entity": "Date", "property": "Date", "display": "Date", "query_ref": "Date.Date"}
    assert fo._aliased_norm(fld, lookup) == "orderdate"
    # A bare property that is not itself a full-ref alias key must NOT match.
    assert fo._aliased_norm({"property": "Date"}, lookup) is None


def test_apply_field_aliases_remaps_norm_preserves_emitted_and_counts():
    pbir = {"pages": [{"visuals": [{"fields": [
        {"entity": "Date", "property": "Date", "norm": "date"},
        {"entity": "Sales", "property": "Sales", "norm": "sales"},
    ]}]}]}
    n = fo._apply_field_aliases(pbir, {"Date.Date": "Order Date"})
    assert n == 1
    f0 = pbir["pages"][0]["visuals"][0]["fields"][0]
    assert f0["norm"] == "orderdate" and f0["norm_emitted"] == "date"
    # Untouched field keeps its norm; empty/None alias maps are a no-op.
    assert pbir["pages"][0]["visuals"][0]["fields"][1]["norm"] == "sales"
    assert fo._apply_field_aliases(pbir, {}) == 0
    assert fo._apply_field_aliases(pbir, None) == 0


def test_score_report_field_aliases_resolve_rename(tmp_path):
    # Emit the Trend date as a renamed star-schema rebind (Date.Date); without aliases it reads as a
    # field mismatch, with aliases it resolves back to the Tableau 'Order Date' and scores higher.
    trend = _visual_json(
        "v-trend", "areaChart",
        {"x": 640.0, "y": 0.0, "width": 640.0, "height": 720.0, "z": 0},
        {"Category": {"projections": [_projection(_col_field("Date", "Date"), "Date")]},
         "Y": {"projections": [_projection(_agg_field("fed.0abc", "Sales"), "Sum of Sales")]}})
    bars = _faithful_visuals()[0]
    report_dir = _write_pbir(str(tmp_path), "Dash", [bars, trend])
    twb = fo.read_twb_views(TWB_XML)

    base = fo.score_report(twb, fo.read_pbir_report(report_dir))
    aliased = fo.score_report(twb, fo.read_pbir_report(report_dir),
                              field_aliases={"Date.Date": "Order Date"})
    base_trend = next(r for r in base["visuals"] if r["worksheet"] == "Trend")
    al_trend = next(r for r in aliased["visuals"] if r["worksheet"] == "Trend")
    assert al_trend["components"]["fields"] > base_trend["components"]["fields"]
    assert al_trend["score"] >= base_trend["score"]
    assert aliased["summary"]["fields_alias_resolved"] >= 1
    assert base["summary"]["fields_alias_resolved"] == 0


def test_load_field_aliases_accepts_list_wrapped_flat_and_garbage(tmp_path):
    (tmp_path / "recs.json").write_text(
        json.dumps([{"field_aliases": {"Date.Date": "Order Date"}}]), encoding="utf-8")
    assert fo._load_field_aliases(str(tmp_path / "recs.json")) == {"Date.Date": "Order Date"}
    (tmp_path / "wrap.json").write_text(
        json.dumps({"candidate_records": [{"field_aliases": {"A.B": "Cap"}}]}), encoding="utf-8")
    assert fo._load_field_aliases(str(tmp_path / "wrap.json")) == {"A.B": "Cap"}
    (tmp_path / "flat.json").write_text(json.dumps({"X.Y": "Zee"}), encoding="utf-8")
    assert fo._load_field_aliases(str(tmp_path / "flat.json")) == {"X.Y": "Zee"}
    assert fo._load_field_aliases(str(tmp_path / "missing.json")) == {}


# --------------------------------------------------------------------------- scoring primitives
def test_jaccard_and_bands():
    assert fo._jaccard(set(), set()) == 1.0
    assert fo._jaccard({"a"}, set()) == 0.0
    assert fo._jaccard({"a", "b"}, {"a"}) == pytest.approx(0.5)
    assert fo._band(0.99) == "faithful"
    assert fo._band(0.9) == "strong"
    assert fo._band(0.7) == "review"
    assert fo._band(0.1) == "divergent"


def test_iou_identical_and_disjoint():
    a = {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}
    assert fo._iou(a, dict(a)) == pytest.approx(1.0)
    b = {"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5}
    assert fo._iou(a, b) == pytest.approx(0.0)


def test_placement_delta_reports_minute_pixel_drift():
    # The Tableau zone projected onto the canvas vs the emitted px, diffed edge-by-edge -- render free.
    zone = {"nposition": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0}}
    # Emitted 10 px right and 4 px narrower on a 1280x720 canvas.
    visual = {"position": {"x": 10.0, "y": 0.0, "w": 636.0, "h": 720.0},
              "nposition": {"x": 10 / 1280, "y": 0.0, "w": 636 / 1280, "h": 1.0}}
    pl = fo._placement_delta(zone, visual, 1280.0, 720.0)
    assert pl["tableau_zone_px"] == {"x": 0.0, "y": 0.0, "w": 640.0, "h": 720.0}
    assert pl["delta_px"]["left"] == pytest.approx(10.0)
    assert pl["delta_px"]["right"] == pytest.approx(6.0)     # (10+636)-(0+640)
    assert pl["delta_px"]["width"] == pytest.approx(-4.0)
    assert pl["max_edge_px"] == pytest.approx(10.0)
    assert pl["pixel_exact"] is False
    assert pl["within_tolerance"] is True                    # 10 <= 0.01*1280


def test_placement_delta_guards_missing_geometry():
    # No zone, no position, or no canvas -> graceful None (never raises).
    assert fo._placement_delta(None, {"position": {"x": 0, "y": 0, "w": 1, "h": 1}}, 1280, 720) is None
    assert fo._placement_delta({"nposition": {"x": 0, "y": 0, "w": 1, "h": 1}}, {}, 1280, 720) is None
    assert fo._placement_delta({"nposition": {"x": 0, "y": 0, "w": 1, "h": 1}},
                               {"position": {"x": 0, "y": 0, "w": 1, "h": 1}}, 0, 0) is None


def test_score_pair_attaches_pixel_exact_placement(tmp_path):
    # An engine that derives placement from the source zones lands pixel-exact -- no render needed.
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb = fo.read_twb_views(TWB_XML)
    pbir = fo.read_pbir_report(report)
    result = fo.score_report(twb, pbir)
    by_ws = {r["worksheet"]: r for r in result["visuals"]}
    for ws in ("Bars", "Trend"):
        pl = by_ws[ws]["placement"]
        assert pl["pixel_exact"] is True
        assert pl["max_edge_px"] == pytest.approx(0.0)
        assert pl["iou"] == pytest.approx(1.0)


def test_type_score_related_partial():
    area_ws = {"family": fo.FAM_AREA, "family_asserted": True}
    line_v = {"family": fo.FAM_LINE}
    score, note = fo._type_score(area_ws, line_v)
    assert score == fo.TYPE_RELATED_CREDIT and "related" in note


# --------------------------------------------------------------------------- end-to-end scoring
def test_score_report_faithful_rebuild(tmp_path):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb = fo.read_twb_views(TWB_XML)
    pbir = fo.read_pbir_report(report)
    result = fo.score_report(twb, pbir)
    # Bars + Trend are matched and score perfectly; Card has no peer visual on this page.
    by_ws = {r["worksheet"]: r for r in result["visuals"]}
    assert by_ws["Bars"]["score"] == pytest.approx(1.0)
    assert by_ws["Trend"]["score"] == pytest.approx(1.0)
    assert result["summary"]["mean_visual_score"] == pytest.approx(1.0)
    # Card worksheet is unmatched -> coverage drags the aggregate below the per-visual mean.
    assert "Card" in result["summary"]["unmatched_worksheets"]
    assert result["summary"]["aggregate_score"] < 1.0


def test_score_report_detects_dropped_field(tmp_path):
    visuals = _faithful_visuals()
    # Drop Profit from the bar's Y well -> a real binding gap.
    visuals[0]["visual"]["query"]["queryState"]["Y"]["projections"] = [
        _projection(_agg_field("fed.0abc", "Sales"), "Sum of Sales")]
    report = _write_pbir(str(tmp_path), "Dash", visuals)
    twb = fo.read_twb_views(TWB_XML)
    pbir = fo.read_pbir_report(report)
    result = fo.score_report(twb, pbir)
    bars = next(r for r in result["visuals"] if r["worksheet"] == "Bars")
    assert "profit" in bars["fields_missing"]
    assert bars["score"] < 1.0


def test_score_report_area_to_line_is_partial(tmp_path):
    visuals = _faithful_visuals()
    visuals[1]["visual"]["visualType"] = "lineChart"  # area -> line simplification
    report = _write_pbir(str(tmp_path), "Dash", visuals)
    twb = fo.read_twb_views(TWB_XML)
    pbir = fo.read_pbir_report(report)
    result = fo.score_report(twb, pbir)
    trend = next(r for r in result["visuals"] if r["worksheet"] == "Trend")
    assert trend["target_family"] == fo.FAM_LINE
    assert "related" in trend["type_note"]
    assert trend["components"]["type"] == pytest.approx(fo.TYPE_RELATED_CREDIT)


def test_slicer_matches_source_filter(tmp_path):
    visuals = _faithful_visuals()
    slicer = _visual_json(
        "v-slicer", "slicer",
        {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0, "z": 1},
        {"Values": {"projections": [_projection(_col_field("fed.0abc", "Category"), "Category")]}},
        filter_config={"filters": [{"field": _col_field("fed.0abc", "Category")}]})
    report = _write_pbir(str(tmp_path), "Dash", visuals + [slicer])
    twb = fo.read_twb_views(TWB_XML)
    pbir = fo.read_pbir_report(report)
    result = fo.score_report(twb, pbir)
    assert result["slicers"] and result["slicers"][0]["matches_source_filter"] is True


def test_worksheet_captures_non_categorical_filters():
    # Range (quantitative) and relative-date quick filters must also be captured for the slicer
    # cross-check -- each tagged with its Tableau filter class, not only categorical ones.
    xml = TWB_XML.replace(
        "<filter class='categorical' column='[fed.0abc].[none:Category:nk]'/>",
        "<filter class='categorical' column='[fed.0abc].[none:Category:nk]'/>"
        "<filter class='quantitative' column='[fed.0abc].[sum:Sales:qk]'/>"
        "<filter class='relative-date' column='[fed.0abc].[tmn:Order Date:qk]'/>",
    )
    ws = fo.read_twb_views(xml)["worksheets"]["Bars"]
    by_norm = {f["norm"]: f.get("filter_class") for f in ws["filters"]}
    assert by_norm.get("category") == "categorical"
    assert by_norm.get("sales") == "quantitative"
    assert by_norm.get("orderdate") == "relative-date"


def test_slicer_matches_non_categorical_filter(tmp_path):
    # A slicer emitted for a RANGE (quantitative) source filter must cross-check as a match and
    # report the matched filter class -- proving non-categorical filters are now verifiable.
    xml = TWB_XML.replace(
        "<filter class='categorical' column='[fed.0abc].[none:Category:nk]'/>",
        "<filter class='quantitative' column='[fed.0abc].[sum:Sales:qk]'/>",
    )
    slicer = _visual_json(
        "v-slicer", "slicer",
        {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0, "z": 1},
        {"Values": {"projections": [_projection(_agg_field("fed.0abc", "Sales"), "Sales")]}},
        filter_config={"filters": [{"field": _agg_field("fed.0abc", "Sales")}]})
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals() + [slicer])
    twb = fo.read_twb_views(xml)
    pbir = fo.read_pbir_report(report)
    result = fo.score_report(twb, pbir)
    s = result["slicers"][0]
    assert s["matches_source_filter"] is True
    assert s["matched_filter_classes"] == ["quantitative"]


# ------------------------------ placement rollup + non-worksheet dashboard object accounting (render-free)
def _twb_with_objects():
    """TWB_XML with non-worksheet object zones added to the dashboard (title/text/legend/param)."""
    return TWB_XML.replace(
        "<zone name='Trend' x='50000' y='0' w='50000' h='100000'/>",
        "<zone name='Trend' x='50000' y='0' w='50000' h='100000'/>"
        "<zone id='40' x='0' y='0' w='100000' h='6000' type-v2='title'/>"
        "<zone id='41' x='0' y='6000' w='30000' h='4000' type-v2='text'/>"
        "<zone id='42' name='Bars' x='80000' y='10000' w='20000' h='20000' type-v2='color'/>"
        "<zone id='43' x='90000' y='0' w='10000' h='6000' type-v2='paramctrl' "
        "param='[Parameters].[P0]'/>")


def test_dashboard_record_captures_non_worksheet_objects():
    db = fo.read_twb_views(_twb_with_objects())["dashboards"][0]
    # The worksheet zone list is unchanged; containers + objects never leak into it.
    assert sorted(z["worksheet"] for z in db["zones"]) == ["Bars", "Trend"]
    assert [o["kind"] for o in db["objects"]] == ["title", "text", "color", "paramctrl"]
    # A legend (color) carries its owning worksheet; a param control carries its binding.
    color = next(o for o in db["objects"] if o["kind"] == "color")
    assert color["worksheet"] == "Bars"
    pc = next(o for o in db["objects"] if o["kind"] == "paramctrl")
    assert pc["param"] == "[Parameters].[P0]"
    # Structural containers (layout-basic / layout-flow) are NOT objects.
    assert not any((o["kind"] or "").startswith("layout") for o in db["objects"])
    # Each object is normalized to the dashboard extent (title spans the full width).
    assert db["objects"][0]["nposition"]["w"] == pytest.approx(1.0)


def test_object_target_projects_zone_to_canvas_px():
    obj = {"kind": "title", "worksheet": None, "param": None,
           "nposition": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.05}}
    rec = fo._object_target(obj, "Dash", "Dash", 1280.0, 720.0)
    assert rec["target_px"] == {"x": 0.0, "y": 0.0, "w": 1280.0, "h": 36.0}
    # No canvas -> no target_px, but the record (kind/nposition) still returns.
    rec2 = fo._object_target(obj, "Dash", "Dash", None, None)
    assert "target_px" not in rec2 and rec2["kind"] == "title"


def test_score_report_surfaces_dashboard_objects(tmp_path):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    pbir = fo.read_pbir_report(report)
    result = fo.score_report(fo.read_twb_views(_twb_with_objects()), pbir)
    assert result["summary"]["dashboard_objects"] == 4
    detail = result["dashboard_objects_detail"]
    assert {o["kind"] for o in detail} == {"title", "text", "color", "paramctrl"}
    assert all("target_px" in o for o in detail)        # projected onto the 1280x720 page canvas
    # Objects are expected extras: they never move coverage or the aggregate.
    plain = fo.score_report(fo.read_twb_views(TWB_XML), fo.read_pbir_report(report))
    assert result["summary"]["coverage"] == plain["summary"]["coverage"]
    assert result["summary"]["aggregate_score"] == plain["summary"]["aggregate_score"]
    md = fo.render_markdown(result)
    assert "Non-worksheet dashboard objects" in md and "paramctrl" in md


def test_placement_rollup_pixel_exact_on_faithful(tmp_path):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    result = fo.score_report(fo.read_twb_views(TWB_XML), fo.read_pbir_report(report))
    pr = result["summary"]["placement"]
    assert pr["verdict"] == "pixel-exact"
    assert pr["evaluated"] == 2 and pr["pixel_exact"] == 2 and pr["drifted"] == 0
    assert pr["worst_max_edge_px"] == pytest.approx(0.0)
    assert "Layout (placement)" in fo.render_markdown(result)


def test_placement_rollup_verdicts_and_empty():
    # Synthetic per-visual placements exercise the acceptable + drifted verdicts and worst-zone pick.
    exact = {"worksheet": "A",
             "placement": {"pixel_exact": True, "within_tolerance": True, "max_edge_px": 1.0}}
    accept = {"worksheet": "B",
              "placement": {"pixel_exact": False, "within_tolerance": True, "max_edge_px": 9.0}}
    drift = {"worksheet": "C",
             "placement": {"pixel_exact": False, "within_tolerance": False, "max_edge_px": 40.0}}
    assert fo._placement_rollup([exact, accept])["verdict"] == "acceptable"
    roll = fo._placement_rollup([exact, accept, drift])
    assert roll["verdict"] == "drifted"
    assert roll["drifted"] == 1 and roll["within_tolerance"] == 2
    assert roll["worst_worksheet"] == "C" and roll["worst_max_edge_px"] == 40.0
    # No placements (e.g. a non-dashboard worksheet match) -> None.
    assert fo._placement_rollup([{"worksheet": "Z"}]) is None


def test_run_oracle_and_markdown(tmp_path):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb_path = tmp_path / "wb.twb"
    twb_path.write_text(TWB_XML, encoding="utf-8")
    result = fo.run_oracle(str(twb_path), report)
    assert result["advisory"] is True and result["kind"] == fo.ORACLE_KIND
    md = fo.render_markdown(result)
    assert "Fidelity Oracle" in md and "Aggregate" in md


# --------------------------------------------------------------------------- optional tiers / guards
def test_optional_tiers_degrade_gracefully(monkeypatch):
    # Hermetic: force the ADOMD-load guard so the value tier returns its unavailable record without
    # discovering or connecting to any instance. (With Power BI Desktop open, real discovery + a raw
    # conn.Open() would block indefinitely -- the bug this guard + _open_bounded fix.)
    def _raise():
        raise RuntimeError("ADOMD.NET client DLL not found")
    monkeypatch.setattr(fo, "_load_adomd", _raise)
    dax = fo.dax_value_tier()
    img = fo.image_tier()
    assert dax["available"] is False and "reason" in dax
    assert img["available"] is False and "reason" in img


def test_module_imports_without_optional_deps():
    # The structural tier must be import-clean offline; re-import is a cheap proof.
    import importlib
    importlib.reload(fo)
    assert hasattr(fo, "run_oracle")


# --------------------------------------------------------------------------- robustness / hardening
def test_read_twb_views_handles_malformed_xml():
    # The advisory oracle must never raise on a bad input -- it returns an empty parse + warning.
    res = fo.read_twb_views("<workbook><not-closed>")
    assert res["worksheets"] == {} and res["dashboards"] == []
    assert res["warnings"]


def test_read_pbir_report_handles_malformed_files(tmp_path):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    # Corrupt one visual.json and the page.json -- the reader must skip/recover, never raise.
    vjson = os.path.join(report, "definition", "pages", "page1", "visuals", "v-bars", "visual.json")
    with open(vjson, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    parsed = fo.read_pbir_report(report)
    page = parsed["pages"][0]
    # The good visual still parses; the corrupt one is dropped with a warning.
    names = {v["name"] for v in page["visuals"]}
    assert "v-trend" in names and "v-bars" not in names
    assert parsed["warnings"]


def test_pbir_extract_field_excludes_implicit():
    # Row-count / generated-geo columns on the PBIR side must not show up as fields.
    assert fo._pbir_extract_field(_col_field("E", "__tableau_internal_object_id__")) is None
    assert fo._pbir_extract_field(_agg_field("E", "Number of Records")) is None
    assert fo._pbir_extract_field(_col_field("E", "Latitude (generated)")) is None


def test_parse_pill_excludes_wrapped_generated_and_row_count():
    # Wrapped tokens pass the raw-token guard (they end in ``:qk``) but must drop on the resolved name.
    assert fo._parse_pill("none:Number of Records:qk", {}) is None
    assert fo._parse_pill("none:Latitude (generated):qk", {}) is None
    # A real field with the same shape still parses.
    assert fo._parse_pill("sum:Sales:qk", {})["norm"] == "sales"


def _write_multi_page_pbir(base, pages):
    """Write a *.Report with multiple pages. ``pages`` = [(display, page_name, [visual dicts])]."""
    report = os.path.join(base, "Sample.Report")
    pages_dir = os.path.join(report, "definition", "pages")
    os.makedirs(pages_dir)
    with open(os.path.join(pages_dir, "pages.json"), "w", encoding="utf-8") as fh:
        json.dump({"pageOrder": [pn for _d, pn, _v in pages]}, fh)
    for display, page_name, visuals in pages:
        pdir = os.path.join(pages_dir, page_name)
        os.makedirs(pdir)
        with open(os.path.join(pdir, "page.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": page_name, "displayName": display,
                       "width": 1280, "height": 720}, fh)
        for v in visuals:
            vdir = os.path.join(pdir, "visuals", v["name"])
            os.makedirs(vdir)
            with open(os.path.join(vdir, "visual.json"), "w", encoding="utf-8") as fh:
                json.dump(v, fh)
    return report


_TWB_TWO_DASH = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <datasources>
    <datasource name='fed.0abc' caption='Sample'>
      <column name='[Sales]' caption='Sales' datatype='real' role='measure'/>
      <column name='[Category]' caption='Category' datatype='string' role='dimension'/>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Bars'>
      <table>
        <view><datasources><datasource name='fed.0abc' caption='Sample'/></datasources></view>
        <panes><pane><mark class='Bar'/><encodings/></pane></panes>
        <rows>[fed.0abc].[none:Category:nk]</rows>
        <cols>[fed.0abc].[sum:Sales:qk]</cols>
      </table>
    </worksheet>
  </worksheets>
  <dashboards>
    <dashboard name='Dash1'>
      <size maxwidth='1000' maxheight='800'/>
      <zones><zone x='0' y='0' w='100000' h='100000' type-v2='layout-basic'>
        <zone name='Bars' x='0' y='0' w='100000' h='100000'/>
      </zone></zones>
    </dashboard>
    <dashboard name='Dash2'>
      <size maxwidth='1000' maxheight='800'/>
      <zones><zone x='0' y='0' w='100000' h='100000' type-v2='layout-basic'>
        <zone name='Bars' x='0' y='0' w='100000' h='100000'/>
      </zone></zones>
    </dashboard>
  </dashboards>
</workbook>
"""


def _bar_visual(name):
    return _visual_json(
        name, "clusteredBarChart",
        {"x": 0.0, "y": 0.0, "width": 1280.0, "height": 720.0, "z": 0},
        {"Category": {"projections": [_projection(_col_field("fed.0abc", "Category"), "Category")]},
         "Y": {"projections": [_projection(_agg_field("fed.0abc", "Sales"), "Sum of Sales")]}})


def test_coverage_clamped_when_worksheet_on_two_dashboards(tmp_path):
    # A worksheet placed on two dashboards yields two scored visuals, but coverage (over UNIQUE
    # source worksheets) must stay <= 1.0 and the aggregate must never exceed the per-visual mean.
    report = _write_multi_page_pbir(str(tmp_path), [
        ("Dash1", "p1", [_bar_visual("v1")]),
        ("Dash2", "p2", [_bar_visual("v2")]),
    ])
    twb = fo.read_twb_views(_TWB_TWO_DASH)
    pbir = fo.read_pbir_report(report)
    result = fo.score_report(twb, pbir)
    s = result["summary"]
    assert len(result["visuals"]) == 2          # Bars scored once per dashboard page
    assert s["coverage"] <= 1.0
    assert s["aggregate_score"] <= s["mean_visual_score"]


def test_non_dashboard_visual_not_double_matched(tmp_path):
    # Two field-identical worksheets, one lone leftover visual -> only one may claim it.
    twb_xml = TWB_XML.replace(
        "<worksheet name='Card'>",
        "<worksheet name='BarsTwin'>"
        "<table><view><datasources><datasource name='fed.0abc' caption='Sample'/></datasources></view>"
        "<panes><pane><mark class='Bar'/><encodings/></pane></panes>"
        "<rows>[fed.0abc].[none:Category:nk]</rows>"
        "<cols>[fed.0abc].[sum:Sales:qk]</cols></table></worksheet>"
        "<worksheet name='Card'>")
    # Remove dashboards so both Bars and BarsTwin go through the leftover (field-only) path.
    twb_xml = twb_xml.split("<dashboards>")[0] + "</workbook>\n"
    twb = fo.read_twb_views(twb_xml)
    # Single page with one bar visual.
    report = _write_multi_page_pbir(str(tmp_path), [("P", "p1", [_bar_visual("only")])])
    pbir = fo.read_pbir_report(report)
    result = fo.score_report(twb, pbir)
    matched = [r["worksheet"] for r in result["visuals"]]
    # Exactly one of the twins matched the lone visual; the other is reported unmatched.
    assert ("Bars" in matched) ^ ("BarsTwin" in matched)
    assert {"Bars", "BarsTwin"} & set(result["summary"]["unmatched_worksheets"])


# --------------------------------------------------------------------------- optional Tier-2 (DAX-value)
def test_discover_pbi_instances_reads_port_files(tmp_path):
    ws = tmp_path / "AnalysisServicesWorkspace_x" / "Data"
    ws.mkdir(parents=True)
    # Power BI writes the port file as UTF-16; a stray file must be ignored.
    (ws / "msmdsrv.port.txt").write_bytes("57777".encode("utf-16-le"))
    (ws / "other.txt").write_text("9999", encoding="utf-8")
    found = fo.discover_pbi_instances(workspace_roots=[str(tmp_path)])
    assert [i["port"] for i in found] == [57777]
    assert found[0]["host"] == "localhost"


def test_discover_pbi_instances_dedups_by_port(tmp_path):
    for sub in ("a", "b"):
        d = tmp_path / sub / "Data"
        d.mkdir(parents=True)
        (d / "msmdsrv.port.txt").write_bytes("60000".encode("utf-16-le"))
    found = fo.discover_pbi_instances(workspace_roots=[str(tmp_path)])
    assert [i["port"] for i in found] == [60000]


def test_discover_pbi_instances_missing_root_is_empty(tmp_path):
    assert fo.discover_pbi_instances(workspace_roots=[str(tmp_path / "nope")]) == []
    assert fo.discover_pbi_instances(workspace_roots=[]) == []


def test_compare_value_tolerance_bands():
    assert fo._compare_value("m", 100.0, 100.4, tolerance=0.01)["within_tolerance"] is True
    miss = fo._compare_value("m", 100.0, 105.0, tolerance=0.01)
    assert miss["within_tolerance"] is False and miss["rel_diff"] == pytest.approx(0.05)
    assert fo._compare_value("m", None, 5)["within_tolerance"] is False
    assert fo._compare_value("m", "Yes", "Yes")["within_tolerance"] is True
    assert fo._compare_value("m", "Yes", "No")["within_tolerance"] is False


def test_score_value_results():
    res = [{"ok": True}, {"ok": True}, {"ok": False}]
    assert fo._score_value_results(res, []) == pytest.approx(round(2 / 3, 4))
    comps = [{"within_tolerance": True}, {"within_tolerance": False}]
    assert fo._score_value_results(res, comps) == 0.5
    assert fo._score_value_results([], []) is None


def test_normalize_expected_flat_and_rich():
    flat = fo._normalize_expected({"Sales": 100.0})
    assert flat == [{"label": "Sales", "measure": "Sales", "expected": 100.0,
                     "filter": None, "query": None}]
    rich = fo._normalize_expected({
        "Sales (US map)": {"measure": "Sales", "expected": 2026.0,
                            "filter": "'Orders'[Country] = \"United States\""},
        "Sales (all)": {"value": 2326.0},  # measure defaults to label, 'value' aliases 'expected'
    })
    by_label = {c["label"]: c for c in rich}
    assert by_label["Sales (US map)"]["measure"] == "Sales"
    assert "United States" in by_label["Sales (US map)"]["filter"]
    assert by_label["Sales (all)"]["measure"] == "Sales (all)"
    assert by_label["Sales (all)"]["expected"] == 2326.0
    assert by_label["Sales (all)"]["filter"] is None


class _FakeReader:
    def __init__(self, rows):
        self._rows, self._i = rows, -1

    def Read(self):
        self._i += 1
        return self._i < len(self._rows)

    def GetValue(self, i):
        return self._rows[self._i][i]

    def Close(self):
        pass


class _FakeCmd:
    def __init__(self, sink):
        self._sink, self.CommandText = sink, None

    def ExecuteReader(self):
        self._sink["query"] = self.CommandText
        return _FakeReader([[123.0]])


class _FakeConn:
    def __init__(self):
        self.sink = {}

    def CreateCommand(self):
        return _FakeCmd(self.sink)

    def Close(self):
        pass


def test_evaluate_measure_wraps_filter_in_calculate():
    conn = _FakeConn()
    plain = fo._evaluate_measure(conn, "Sales")
    assert conn.sink["query"] == 'EVALUATE ROW("v", [Sales])'
    assert plain["ok"] is True and plain["value"] == 123.0
    filtered = fo._evaluate_measure(conn, "Sales", "'Orders'[Country] = \"United States\"")
    assert "CALCULATE([Sales]" in conn.sink["query"]
    assert "United States" in conn.sink["query"]
    assert filtered["ok"] is True and filtered["value"] == 123.0


def test_normalize_expected_query_form():
    checks = fo._normalize_expected({
        "winners": {"query": "EVALUATE ROW(\"n\", 59)", "expected": 59},
        "Sales": 100.0,  # flat entries still carry a (None) query key
    })
    by_label = {c["label"]: c for c in checks}
    assert by_label["winners"]["query"] == "EVALUATE ROW(\"n\", 59)"
    assert by_label["winners"]["expected"] == 59
    assert by_label["Sales"]["query"] is None


def test_evaluate_query_runs_scalar_and_captures_error():
    conn = _FakeConn()
    ok = fo._evaluate_query(conn, "EVALUATE ROW(\"v\", COUNTROWS('Orders'))")
    assert conn.sink["query"] == "EVALUATE ROW(\"v\", COUNTROWS('Orders'))"
    assert ok["ok"] is True and ok["value"] == 123.0 and ok["query"]

    class _Boom:
        def CreateCommand(self):
            raise RuntimeError("syntax error near FOO")

    bad = fo._evaluate_query(_Boom(), "EVALUATE FOO")
    assert bad["ok"] is False and bad["value"] is None
    assert "syntax error" in bad["error"]


def test_dax_value_tier_dispatches_query_vs_measure(monkeypatch):
    # A query-shaped expected entry must be evaluated by its own scalar DAX (calc-column path),
    # while a plain entry goes through the measure path -- both compared under the same tolerance.
    captured = []

    class _Conn:
        def Open(self):
            pass

        def Close(self):
            pass

    def _fake_rows(conn, query, columns):
        captured.append(query)
        if "DBSCHEMA_CATALOGS" in query:
            return [{"CATALOG_NAME": "Model"}]
        if "MDSCHEMA_MEASURES" in query:
            return [{"MEASUREGROUP_NAME": "Orders", "MEASURE_NAME": "C1",
                     "MEASURE_IS_VISIBLE": True}]
        if "COUNTROWS('winners')" in query:
            return [{"v": 59.0}]
        if "[C1]" in query:
            return [{"v": 1221139.3614}]
        return [{"v": 0.0}]

    # _load_adomd returns the connection *constructor*; the tier calls it as AdomdConnection(ds).
    monkeypatch.setattr(fo, "_load_adomd", lambda: (lambda ds: _Conn()))
    monkeypatch.setattr(fo, "_adomd_rows", _fake_rows)

    out = fo.dax_value_tier(
        port=12345,
        measures=["C1"],
        expected={
            "C1 grand total": {"measure": "C1", "expected": 1221139.3614},
            "winners distinct": {"query": "EVALUATE ROW(\"v\", COUNTROWS('winners'))",
                                 "expected": 59},
        },
    )
    assert out["available"] is True
    cmp_by = {c["measure"]: c for c in out["comparisons"]}
    assert cmp_by["C1 grand total"]["within_tolerance"] is True
    win = cmp_by["winners distinct"]
    assert win["within_tolerance"] is True and win.get("query")
    # the query-shaped check ran its own scalar DAX, not a measure evaluation
    assert any("COUNTROWS('winners')" in q for q in captured)


def test_image_tier_regions_breakdown(tmp_path):
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    # A tall image whose top half differs from the bottom between ref and candidate.
    top = np.tile(np.linspace(0, 255, 60).astype("uint8"), (60, 1))
    ref = np.vstack([top, top])
    cand = np.vstack([top, 255 - top])  # bottom half inverted in the candidate
    p1, p2 = tmp_path / "r.png", tmp_path / "c.png"
    Image.fromarray(ref).save(str(p1))
    Image.fromarray(cand).save(str(p2))
    regions = [
        {"name": "top", "ref": (0.0, 0.0, 1.0, 0.5)},
        {"name": "bottom", "ref": (0.0, 0.5, 1.0, 1.0)},
    ]
    out = fo.image_tier(str(p1), str(p2), regions=regions)
    assert out["available"] is True
    zones = {z["name"]: z for z in out["regions"]}
    assert zones["top"]["ssim"] > zones["bottom"]["ssim"]  # top matches, bottom diverges
    assert out["regions_mean_ssim"] == pytest.approx(
        round((zones["top"]["ssim"] + zones["bottom"]["ssim"]) / 2, 4))


def test_dax_value_tier_unavailable_degrades(tmp_path):
    # No workspace roots + no explicit port -> a structured unavailable record, never a raise
    # (ADOMD/pythonnet missing on CI, or no live instance on a host both land here).
    out = fo.dax_value_tier(port=None, workspace_roots=[str(tmp_path / "none")])
    assert out["tier"] == "dax-value" and out["available"] is False and "reason" in out


def test_dax_value_tier_degrades_when_no_live_instance(monkeypatch):
    # ADOMD importable but discovery finds nothing -> the "no live instance" branch, and crucially
    # _connect is never invoked (proven by a connection class that explodes if constructed).
    class _NoConnect:
        def __init__(self, *a, **k):
            raise AssertionError("must not attempt a connection when nothing is discovered")
    monkeypatch.setattr(fo, "_load_adomd", lambda: _NoConnect)
    monkeypatch.setattr(fo, "discover_pbi_instances", lambda *a, **k: [])
    out = fo.dax_value_tier(port=None)
    assert out["available"] is False
    assert "no live" in out["reason"].lower()
    assert out["discovered_ports"] == []


def test_dax_value_tier_discovery_budget_degrades(monkeypatch):
    # A host littered with stale Desktop port files must not grind for minutes: discovery stops once the
    # wall-clock budget is spent and returns a structured unavailable record naming the budget, instead of
    # probing every instance. Hermetic -- no real Analysis Services contact.
    monkeypatch.setattr(fo, "_DISCOVERY_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(fo, "discover_pbi_instances",
                        lambda *a, **k: [{"port": 50000 + i} for i in range(50)])

    class _StaleConn:
        def __init__(self, *a, **k):
            pass

        def Open(self):
            time.sleep(0.2)            # each probe is slow ...
            raise RuntimeError("stale")  # ... and never succeeds

        def Close(self):
            pass

    monkeypatch.setattr(fo, "_load_adomd", lambda: _StaleConn)
    t0 = time.monotonic()
    out = fo.dax_value_tier(port=None)
    elapsed = time.monotonic() - t0
    assert out["available"] is False
    assert "budget" in out["reason"]
    assert elapsed < 5  # bounded: did NOT probe all 50 stale instances at 0.2s each (=10s)


def test_open_bounded_returns_opened_conn_on_fast_open():
    class _Conn:
        def __init__(self):
            self.opened = False
        def Open(self):
            self.opened = True
    c = _Conn()
    assert fo._open_bounded(c, seconds=2) is c and c.opened is True


def test_open_bounded_times_out_instead_of_hanging():
    import time
    class _Conn:
        def Open(self):
            time.sleep(5)  # simulate a blocked native Open(); the bound must not wait this long
    with pytest.raises(TimeoutError):
        fo._open_bounded(_Conn(), seconds=0.2)


def test_open_bounded_propagates_connect_error():
    class _Conn:
        def Open(self):
            raise RuntimeError("connection refused")
    with pytest.raises(RuntimeError, match="connection refused"):
        fo._open_bounded(_Conn(), seconds=2)


def test_dax_value_tier_live_if_available():
    # Opt-in live test: it connects to a real local Power BI Desktop Analysis Services instance, so it is
    # skipped by default -- including in the self-update pytest gate -- to keep the suite hermetic and fast
    # (a host with many stale Desktop instances would otherwise make it slow). Set
    # TABLEAU_MIGRATION_LIVE_ORACLE=1 to exercise it against a running Desktop model.
    if not os.environ.get("TABLEAU_MIGRATION_LIVE_ORACLE"):
        pytest.skip("set TABLEAU_MIGRATION_LIVE_ORACLE=1 to run the live ADOMD value-tier test")
    try:
        AdomdConnection = fo._load_adomd()
    except Exception:  # noqa: BLE001
        pytest.skip("ADOMD.NET / pythonnet not available")
    live_port = None
    for inst in fo.discover_pbi_instances():
        try:
            c = AdomdConnection("Data Source=localhost:%d" % inst["port"])
            fo._open_bounded(c)
            c.Close()
            live_port = inst["port"]
            break
        except Exception:  # noqa: BLE001
            continue
    if live_port is None:
        pytest.skip("no live Power BI Desktop Analysis Services instance")
    res = fo.dax_value_tier(port=live_port)
    assert res["available"] is True
    assert res["instance"]["port"] == live_port
    assert res["value_score"] is None or 0.0 <= res["value_score"] <= 1.0
    # Every reported measure carries an ok flag and (on success) a value or (on failure) an error.
    for r in res["results"]:
        assert "ok" in r and ("value" in r or "error" in r)


# --------------------------------------------------------------------------- optional Tier-3 (image)
def test_image_band_thresholds():
    assert fo._image_band(0.99) == "near-identical"
    assert fo._image_band(0.9) == "strong"
    assert fo._image_band(0.7) == "moderate"
    assert fo._image_band(0.1) == "divergent"


def test_image_tier_requires_two_paths():
    out = fo.image_tier(None, None)
    assert out["available"] is False and "reason" in out


def test_ssim_identical_and_inverted():
    np = pytest.importorskip("numpy")
    a = np.tile(np.linspace(0, 255, 64), (64, 1))
    assert fo._ssim(np, a, a.copy()) == pytest.approx(1.0, abs=1e-6)
    assert fo._ssim(np, a, 255.0 - a) < 0.9


def test_image_tier_ssim_when_deps_present(tmp_path):
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    arr = np.tile(np.linspace(0, 255, 80).astype("uint8"), (80, 1))
    p1, p2 = tmp_path / "a.png", tmp_path / "b.png"
    Image.fromarray(arr).save(str(p1))
    Image.fromarray(arr).save(str(p2))
    out = fo.image_tier(str(p1), str(p2))
    assert out["available"] is True
    assert out["ssim"] == pytest.approx(1.0, abs=1e-6)
    assert out["band"] == "near-identical"
    # A very different candidate scores materially lower and is resized to the reference shape.
    p3 = tmp_path / "c.png"
    Image.fromarray((255 - arr)).resize((40, 120)).save(str(p3))
    out2 = fo.image_tier(str(p1), str(p3))
    assert out2["available"] is True and out2["ssim"] < out["ssim"]
    assert out2["reference_shape"] == [80, 80]


def test_image_tier_meets_target_threshold(tmp_path):
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    arr = np.tile(np.linspace(0, 255, 80).astype("uint8"), (80, 1))
    p1, p2 = tmp_path / "a.png", tmp_path / "b.png"
    Image.fromarray(arr).save(str(p1))
    Image.fromarray(arr).save(str(p2))
    # Identical images clear the default 0.80 acceptance floor.
    out = fo.image_tier(str(p1), str(p2))
    assert out["acceptance_threshold"] == pytest.approx(fo.DEFAULT_ACCEPTANCE_SSIM)
    assert out["meets_target"] is True
    # An impossibly high custom floor is reported as below target without erroring.
    strict = fo.image_tier(str(p1), str(p2), acceptance_threshold=1.01)
    assert strict["acceptance_threshold"] == pytest.approx(1.01)
    assert strict["meets_target"] is False


def test_run_oracle_attaches_optional_tiers(tmp_path):
    # run_oracle wires the optional tiers in without ever failing the structural run.
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb_path = tmp_path / "wb.twb"
    twb_path.write_text(TWB_XML, encoding="utf-8")
    result = fo.run_oracle(str(twb_path), report,
                           dax_options={"port": None, "workspace_roots": [str(tmp_path / "no")]},
                           image_options={"reference_png": None, "candidate_png": None})
    assert result["dax_value"]["available"] is False
    assert result["image"]["available"] is False
    # Structural tier still produced its summary regardless of the optional tiers.
    assert result["summary"]["aggregate_score"] is not None
    md = fo.render_markdown(result)
    assert "DAX-value tier" in md and "Image tier" in md


def test_nbox_converts_normalized_position():
    assert fo._nbox({"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}) == (0.5, 0.0, 1.0, 1.0)
    assert fo._nbox(None) is None
    assert fo._nbox({"x": 0.0, "y": 0.0, "w": 0.5}) is None  # missing 'h'


def test_regions_from_layout_pairs_zones(tmp_path):
    # The structural pairing drives the per-zone image crop boxes: each worksheet's Tableau zone
    # (ref) and its paired PBIR visual position (cand), with no hand-tuned fractions.
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb = fo.read_twb_views(TWB_XML)
    pbir = fo.read_pbir_report(report)
    regions = fo.regions_from_layout(twb, pbir)
    by_name = {r["name"]: r for r in regions}
    assert set(by_name) == {"Bars", "Trend"}
    # Bars occupies the left half on both sides; Trend the right half.
    assert by_name["Bars"]["ref"][0] == pytest.approx(0.0)
    assert by_name["Bars"]["ref"][2] == pytest.approx(0.5)
    assert by_name["Bars"]["cand"][2] == pytest.approx(0.5)
    assert by_name["Trend"]["ref"][0] == pytest.approx(0.5)
    assert by_name["Trend"]["cand"][0] == pytest.approx(0.5)


def test_run_oracle_auto_regions_injects_image_regions(tmp_path):
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb_path = tmp_path / "wb.twb"
    twb_path.write_text(TWB_XML, encoding="utf-8")
    # Left half (Bars zone) identical; right half (Trend zone) diverges between ref and candidate.
    half = np.tile(np.linspace(0, 255, 64).astype("uint8"), (64, 1))
    ref = np.hstack([half, half])
    cand = np.hstack([half, 255 - half])
    p1, p2 = tmp_path / "ref.png", tmp_path / "cand.png"
    Image.fromarray(ref).save(str(p1))
    Image.fromarray(cand).save(str(p2))
    result = fo.run_oracle(
        str(twb_path), report,
        image_options={"reference_png": str(p1), "candidate_png": str(p2),
                       "auto_regions": True})
    img = result["image"]
    assert img["available"] is True
    zones = {z["name"]: z for z in img["regions"]}
    assert set(zones) == {"Bars", "Trend"}
    # Auto-derived crops localize the divergence to the Trend (right-half) zone.
    assert zones["Bars"]["ssim"] > zones["Trend"]["ssim"]


def test_combined_fidelity_structural_only_low_confidence():
    report = {"summary": {"aggregate_score": 0.868}}
    cf = fo._combined_fidelity(report)
    assert cf["combined_score"] == pytest.approx(0.868)
    assert cf["confidence"] == "low"
    assert cf["contributing_tiers"] == ["structural"]


def test_combined_fidelity_fuses_all_three_tiers():
    report = {
        "summary": {"aggregate_score": 0.9},
        "dax_value": {"available": True, "value_score": 0.8},
        "image": {"available": True, "ssim": 0.6},
    }
    cf = fo._combined_fidelity(report)
    # 0.9*0.5 + 0.8*0.3 + 0.6*0.2 over a full weight sum of 1.0
    assert cf["combined_score"] == pytest.approx(0.81)
    assert cf["confidence"] == "high"
    assert cf["contributing_tiers"] == ["image", "structural", "value"]


def test_combined_fidelity_prefers_regions_mean_and_renormalizes():
    # Two tiers (structural + image) -> weights renormalized over 0.5 + 0.2; regions_mean wins.
    report = {
        "summary": {"aggregate_score": 0.9},
        "image": {"available": True, "ssim": 0.99, "regions_mean_ssim": 0.6},
    }
    cf = fo._combined_fidelity(report)
    assert cf["tier_scores"]["image"] == pytest.approx(0.6)  # regions_mean preferred over ssim
    assert cf["combined_score"] == pytest.approx(round((0.9 * 0.5 + 0.6 * 0.2) / 0.7, 4))
    assert cf["confidence"] == "medium"


def test_combined_fidelity_none_without_structural():
    assert fo._combined_fidelity({"summary": {"aggregate_score": None}}) is None
    assert fo._combined_fidelity({}) is None


def test_run_oracle_attaches_combined_fidelity(tmp_path):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb_path = tmp_path / "wb.twb"
    twb_path.write_text(TWB_XML, encoding="utf-8")
    result = fo.run_oracle(str(twb_path), report)
    cf = result["combined_fidelity"]
    # Structural-only run: combined == aggregate, confidence low, headline rendered in markdown.
    assert cf["combined_score"] == pytest.approx(result["summary"]["aggregate_score"])
    assert cf["confidence"] == "low"
    assert "Combined fidelity" in fo.render_markdown(result)


# ---------------------------------------------- host-bridge: CLI plumbing + PBIR validation + render
def test_locate_and_run_cli_guard_when_missing():
    # A bogus command resolves to nothing, and running a non-exe never raises.
    assert fo._locate_cli("totally_bogus_cli_xyz") is None
    res = fo._run_cli("totally_bogus_cli_xyz", ["status"], timeout=5)
    assert res["ran"] is False and "reason" in res


def test_parse_json_loose_tolerates_prefix_and_garbage():
    assert fo._parse_json_loose('log noise\n{"a": 1}') == {"a": 1}
    assert fo._parse_json_loose('[{"x": 2}]') == [{"x": 2}]
    assert fo._parse_json_loose("not json at all") is None
    assert fo._parse_json_loose("") is None


def test_collect_diagnostics_tolerant_shapes():
    # split errors/warnings
    d1 = fo._collect_diagnostics({"errors": [{"message": "bad", "file": "v.json", "jsonPath": "$.a"}],
                                  "warnings": ["heads up"]})
    assert [x["severity"] for x in d1] == ["error", "warning"]
    assert d1[0]["file"] == "v.json" and d1[0]["json_path"] == "$.a"
    # a flat diagnostics list with an explicit severity/level
    d2 = fo._collect_diagnostics({"diagnostics": [{"level": "warn", "text": "x"}]})
    assert d2[0]["severity"] == "warning"
    # nested results[].diagnostics
    d3 = fo._collect_diagnostics({"results": [{"diagnostics": [{"severity": "fatal", "message": "boom"}]}]})
    assert d3[0]["severity"] == "error"
    # a bare top-level list of strings
    assert fo._collect_diagnostics(["oops"])[0]["severity"] == "error"
    # an unknown shape never raises
    assert fo._collect_diagnostics(42) == []


def test_validate_pbir_unavailable_when_cli_missing(tmp_path):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    rec = fo.validate_pbir(report, cli="totally_bogus_cli_xyz")
    assert rec["available"] is False and "not found" in rec["reason"]


def test_validate_pbir_valid_via_monkeypatch(tmp_path, monkeypatch):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    monkeypatch.setattr(fo, "_locate_cli", lambda cmd, explicit=None: "fake-cli")
    monkeypatch.setattr(fo, "_run_cli",
                        lambda exe, args, **kw: {"ran": True, "rc": 0,
                                                 "stdout": json.dumps({"valid": True, "diagnostics": []})})
    rec = fo.validate_pbir(report)
    assert rec["available"] is True and rec["valid"] is True
    assert rec["error_count"] == 0 and rec["exit_code"] == 0


def test_validate_pbir_invalid_collects_errors(tmp_path, monkeypatch):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    monkeypatch.setattr(fo, "_locate_cli", lambda cmd, explicit=None: "fake-cli")
    payload = {"errors": [{"severity": "error", "message": "bad node",
                           "file": "visual.json", "jsonPath": "$.visual"}],
               "warnings": [{"message": "odd type"}]}
    monkeypatch.setattr(fo, "_run_cli",
                        lambda exe, args, **kw: {"ran": True, "rc": 1, "stdout": json.dumps(payload)})
    rec = fo.validate_pbir(report)
    assert rec["available"] is True and rec["valid"] is False
    assert rec["error_count"] == 1 and rec["warning_count"] == 1
    assert rec["diagnostics"][0]["file"] == "visual.json"


def test_shape_instances_and_select_pid():
    inst = fo._shape_instances({"instances": [
        {"pid": "11", "bridgeStatus": "connected", "currentFilePath": r"C:\a\Sales.pbip",
         "reportDir": r"C:\a\Sales.Report"}]})
    assert inst[0]["pid"] == 11 and inst[0]["bridge_status"] == "connected"
    # match by report dir
    pid, reason = fo._select_bridge_pid(inst, pbip_path=r"C:\a\Sales.pbip")
    assert pid == 11 and reason is None
    # sole instance wins even without a path match
    assert fo._select_bridge_pid(inst)[0] == 11
    # ambiguous -> no pick, with a reason
    two = [{"pid": 1}, {"pid": 2}]
    assert fo._select_bridge_pid(two) == (None, "multiple bridge instances; pass an explicit pid")
    # none running
    assert fo._select_bridge_pid([])[0] is None


def test_discover_and_render_unavailable_when_cli_missing(tmp_path):
    assert fo.discover_bridge_instances(cli="totally_bogus_cli_xyz")["available"] is False
    pbip = tmp_path / "r.pbip"
    pbip.write_text("x", encoding="utf-8")
    assert fo.render_pbi_report(str(pbip), cli="totally_bogus_cli_xyz")["available"] is False


def test_render_pbi_report_captures_pages(tmp_path, monkeypatch):
    pbip = tmp_path / "r.pbip"
    pbip.write_text("x", encoding="utf-8")
    out_dir = tmp_path / "shots"
    monkeypatch.setattr(fo, "_locate_cli", lambda cmd, explicit=None: "fake-bridge")

    def fake_run(exe, args, **kw):
        args = list(args)
        if args and args[0] == "status":
            return {"ran": True, "rc": 0, "stdout": json.dumps(
                {"instances": [{"pid": 7, "bridgeStatus": "connected",
                                "currentFilePath": str(pbip)}]})}
        if args and args[0] == "screenshot-all":
            d = args[args.index("--output-dir") + 1]
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "ReportSection1.png"), "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n")
            return {"ran": True, "rc": 0, "stdout": ""}
        return {"ran": True, "rc": 0, "stdout": ""}

    monkeypatch.setattr(fo, "_run_cli", fake_run)
    rec = fo.render_pbi_report(str(pbip), output_dir=str(out_dir))
    assert rec["available"] is True and rec["pid"] == 7
    assert rec["pages"][0]["page_id"] == "ReportSection1"
    assert os.path.isfile(rec["pages"][0]["png"])


def test_run_oracle_validate_is_additive(tmp_path, monkeypatch):
    report_dir = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb = tmp_path / "wb.twb"
    twb.write_text(TWB_XML, encoding="utf-8")
    base = fo.run_oracle(str(twb), report_dir)
    monkeypatch.setattr(fo, "validate_pbir",
                        lambda path, **kw: {"tier": "pbir_validation", "available": True,
                                            "valid": True, "error_count": 0, "warning_count": 1,
                                            "diagnostics": [], "advisory": True, "notes": []})
    withval = fo.run_oracle(str(twb), report_dir, validate=True)
    # The validation pre-gate is purely additive -- the structural aggregate is byte-for-byte equal.
    assert withval["summary"]["aggregate_score"] == base["summary"]["aggregate_score"]
    assert withval["pbir_validation"]["valid"] is True
    assert withval["summary"]["pbir_valid"] is True
    md = fo.render_markdown(withval)
    assert "PBIR validation" in md and "PBIR schema valid" in md


def test_run_oracle_render_wires_candidate(tmp_path, monkeypatch):
    report_dir = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb = tmp_path / "wb.twb"
    twb.write_text(TWB_XML, encoding="utf-8")
    png = tmp_path / "p.png"
    captured = {}
    monkeypatch.setattr(fo, "render_pbi_report",
                        lambda pbip, **kw: {"available": True, "pid": 9,
                                            "pages": [{"page_id": "P1", "png": str(png)}]})
    monkeypatch.setattr(fo, "image_tier",
                        lambda **opts: captured.update(opts) or {"tier": "image", "available": True,
                                                                 "ssim": 0.9})
    result = fo.run_oracle(str(twb), report_dir,
                           image_options={"reference_png": str(tmp_path / "ref.png"),
                                          "render_pbip": str(tmp_path / "x.pbip")})
    assert captured["candidate_png"] == str(png)
    assert result["pbi_render"]["available"] is True and result["image"]["available"] is True


def test_run_oracle_render_unavailable_marks_image(tmp_path, monkeypatch):
    report_dir = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb = tmp_path / "wb.twb"
    twb.write_text(TWB_XML, encoding="utf-8")
    monkeypatch.setattr(fo, "render_pbi_report",
                        lambda pbip, **kw: {"available": False, "pages": [], "reason": "no bridge"})
    result = fo.run_oracle(str(twb), report_dir,
                           image_options={"reference_png": str(tmp_path / "ref.png"),
                                          "render_pbip": str(tmp_path / "x.pbip")})
    assert result["image"]["available"] is False
    assert "render bridge unavailable" in result["image"]["reason"]
    assert "render unavailable" in fo.render_markdown(result)


# --- Tableau reference-PNG resolution from a local source (image-tier reference half) ------------
def test_resolve_reference_png_folder_and_name(tmp_path):
    (tmp_path / "Sheet 1.png").write_bytes(b"x")
    (tmp_path / "Other.png").write_bytes(b"x")
    p = fo._resolve_reference_png(str(tmp_path), "Sheet 1")
    assert p is not None and os.path.basename(p) == "Sheet 1.png"


def test_resolve_reference_png_single_png_no_name(tmp_path):
    only = tmp_path / "Dashboard 1.png"
    only.write_bytes(b"x")
    # A lone PNG resolves even without a name.
    assert fo._resolve_reference_png(str(only)) == os.path.abspath(str(only))


def test_resolve_reference_png_from_twbx(tmp_path):
    import zipfile
    twbx = tmp_path / "wb.twbx"
    with zipfile.ZipFile(twbx, "w") as zf:
        zf.writestr("Image/Region Map.png", b"\x89PNG")
    p = fo._resolve_reference_png(str(twbx), "Region Map")
    assert p is not None and os.path.basename(p) == "Region Map.png"
    assert open(p, "rb").read() == b"\x89PNG"


def test_resolve_reference_png_name_miss_returns_none(tmp_path):
    (tmp_path / "Sheet 1.png").write_bytes(b"x")
    assert fo._resolve_reference_png(str(tmp_path), "Nope") is None


def test_resolve_reference_png_empty_source_is_none():
    assert fo._resolve_reference_png(None) is None
    assert fo._resolve_reference_png("") is None
    # A non-str/path source must degrade to None, never raise (fuzz-discovered contract).
    assert fo._resolve_reference_png(123, "x") is None
    assert fo._resolve_reference_png(object()) is None


def test_run_oracle_image_reference_source_resolves(tmp_path, monkeypatch):
    report_dir = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb = tmp_path / "wb.twb"
    twb.write_text(TWB_XML, encoding="utf-8")
    refs = tmp_path / "refs"
    refs.mkdir()
    (refs / "Sheet 1.png").write_bytes(b"x")
    captured = {}

    def fake_image_tier(**kw):
        captured.update(kw)
        return {"tier": "image", "available": True, "ssim": 0.9}

    monkeypatch.setattr(fo, "image_tier", fake_image_tier)
    fo.run_oracle(str(twb), report_dir,
                  image_options={"reference_source": str(refs), "reference_name": "Sheet 1",
                                 "candidate_png": str(tmp_path / "cand.png")})
    # The Tableau reference was resolved from the local folder and passed through to the tier;
    # the bespoke reference_source/reference_name keys are consumed, not leaked into image_tier.
    assert os.path.basename(captured["reference_png"]) == "Sheet 1.png"
    assert "reference_source" not in captured and "reference_name" not in captured


# =================================================================================================
# Gate 0: openability (TOM TmdlSerializer pre-flight) -- all TOM access is monkeypatched, so the
# suite never needs pythonnet/.NET; these exercise the harness, parsing, and degrade-paths only.
# =================================================================================================
class _FakeCount:
    def __init__(self, n):
        self.Count = n


class _FakeTable:
    def __init__(self, measures):
        self.Measures = _FakeCount(measures)


class _FakeModel:
    def __init__(self, tables, rels):
        self.Tables = [_FakeTable(m) for m in tables]
        self.Relationships = _FakeCount(rels)


class _FakeDb:
    def __init__(self, tables=(2, 1), rels=1):
        self.Model = _FakeModel(list(tables), rels)


def _make_model_dir(base, name="Sample"):
    """Create a minimal ``<name>.SemanticModel/definition`` folder (a single model.tmdl)."""
    defn = os.path.join(base, "%s.SemanticModel" % name, "definition")
    os.makedirs(defn)
    with open(os.path.join(defn, "model.tmdl"), "w", encoding="utf-8") as fh:
        fh.write("model Model\n")
    return base


def test_resolve_model_definition_from_report_parent(tmp_path):
    base = _make_model_dir(str(tmp_path))
    report_dir = os.path.join(base, "Sample.Report")
    os.makedirs(report_dir)
    defn = fo._resolve_model_definition(report_dir, None)
    assert defn is not None and defn.lower().endswith("definition")
    # model_dir pointing straight at the .SemanticModel resolves too; absent -> None.
    assert fo._resolve_model_definition(None, os.path.join(base, "Sample.SemanticModel")) == defn
    # An isolated tree whose parent also holds no model resolves to None (the walk-up is one level).
    iso = tmp_path / "isolated"
    iso.mkdir()
    assert fo._resolve_model_definition(str(iso / "deep"), None) is None


def test_parse_tmdl_error_location():
    f, ln = fo._parse_tmdl_error_location("Error in '_Measures.tmdl' at line 5: bad token")
    assert f == "_Measures.tmdl" and ln == 5
    assert fo._parse_tmdl_error_location("no location here") == (None, None)
    assert fo._parse_tmdl_error_location(None) == (None, None)


def test_parse_tmdl_error_location_real_tom_shape():
    # The actual TOM TmdlSerializer message: a 'Document - ...' payload + 'Line Number - N', followed
    # by a dotted stack-trace type. The Document wins (not the stack-trace '...Tabular.Tmdl' type),
    # and 'Line Number - 107' parses where a bare 'line N' regex would miss it.
    msg = ("TMDL Format Error: Parsing error type - UnsupportedObjectType  Detailed error - "
           "Unsupported object type - VAR is not a supported property in the current context!  "
           "Document - './tables/_Measures'  Line Number - 107  Line - 'VAR d = SELECTEDVALUE("
           "'Date'[Date])'     at Microsoft.AnalysisServices.Tabular.Tmdl.TmdlParser.ObjectContext")
    f, ln = fo._parse_tmdl_error_location(msg)
    assert f == "tables/_Measures"   # from the Document payload, NOT the stack-trace type
    assert ln == 107


def test_tom_model_stats_counts():
    stats = fo._tom_model_stats(_FakeDb(tables=(2, 1, 0), rels=3))
    assert stats == {"tables": 3, "measures": 3, "relationships": 3}


def test_openability_tier_unavailable_no_model(tmp_path):
    rec = fo.openability_tier(report_dir=str(tmp_path))
    assert rec["available"] is False and "no *.SemanticModel" in rec["reason"]


def test_openability_tier_unavailable_no_tom(tmp_path, monkeypatch):
    base = _make_model_dir(str(tmp_path))
    monkeypatch.setattr(fo, "_load_tmdl_serializer",
                        lambda d=None: (_ for _ in ()).throw(RuntimeError("TOM assemblies not found")))
    rec = fo.openability_tier(report_dir=base)
    assert rec["available"] is False and "TOM/pythonnet not available" in rec["reason"]
    assert rec["definition"].lower().endswith("definition")


def test_openability_tier_opens(tmp_path, monkeypatch):
    base = _make_model_dir(str(tmp_path))

    class _Serializer:
        @staticmethod
        def DeserializeDatabaseFromFolder(path):
            assert os.path.isdir(path)
            return _FakeDb(tables=(2, 1), rels=1)

    monkeypatch.setattr(fo, "_load_tmdl_serializer", lambda d=None: _Serializer)
    rec = fo.openability_tier(report_dir=base)
    assert rec["available"] is True and rec["openable"] is True
    assert rec["verdict"] == "opens"
    assert rec["tables"] == 2 and rec["measures"] == 3 and rec["relationships"] == 1
    assert rec["advisory"] is True


def test_openability_tier_blocked_parses_location(tmp_path, monkeypatch):
    base = _make_model_dir(str(tmp_path))

    class _Serializer:
        @staticmethod
        def DeserializeDatabaseFromFolder(path):
            raise Exception("Failed in '_Measures.tmdl' at line 12: unexpected continuation")

    monkeypatch.setattr(fo, "_load_tmdl_serializer", lambda d=None: _Serializer)
    rec = fo.openability_tier(report_dir=base)
    assert rec["available"] is True and rec["openable"] is False
    assert rec["verdict"] == "blocked"
    assert rec["error_file"] == "_Measures.tmdl" and rec["error_line"] == 12
    assert "_Measures.tmdl" in rec["error"]


# ------------------------------------------------------- Gate 0 dominates the combined headline ----
def test_combined_fidelity_openability_blocks_and_keeps_raw():
    report = {"summary": {"aggregate_score": 0.9},
              "openability": {"available": True, "openable": False, "verdict": "blocked"}}
    cf = fo._combined_fidelity(report)
    assert cf["blocked"] is True
    assert cf["combined_score"] == fo._BLOCKED_COMBINED_SCORE == 0.0
    assert cf["raw_combined_score"] == pytest.approx(0.9)  # uncapped structural preserved
    assert cf["verdict"] == "blocked"
    assert cf["openable"] is False


def test_combined_fidelity_openable_true_reads_faithful():
    report = {"summary": {"aggregate_score": 0.9},
              "openability": {"available": True, "openable": True}}
    cf = fo._combined_fidelity(report)
    assert "blocked" not in cf
    assert cf["openable"] is True
    assert cf["verdict"] == "faithful-candidate"


def test_combined_fidelity_verdict_additive_without_openability():
    # No openability tier at all: verdict is still emitted, openable is None, nothing is blocked.
    high = fo._combined_fidelity({"summary": {"aggregate_score": 0.9}})
    assert high["openable"] is None and "blocked" not in high
    assert high["verdict"] == "faithful-candidate"
    assert high["combined_score"] == pytest.approx(0.9)
    mid = fo._combined_fidelity({"summary": {"aggregate_score": 0.7}})
    assert mid["verdict"] == "opens-needs-review"


def test_combined_fidelity_opens_but_broken_on_measure_error():
    report = {"summary": {"aggregate_score": 0.9},
              "dax_value": {"available": True, "value_score": 0.9, "measures_errored": 1},
              "openability": {"available": True, "openable": True}}
    cf = fo._combined_fidelity(report)
    assert cf["verdict"] == "opens-but-broken"


def test_combined_fidelity_openability_only_blocked():
    # Openability is the ONLY tier and it failed: still a blocked verdict, score forced to 0.0.
    cf = fo._combined_fidelity({"openability": {"available": True, "openable": False}})
    assert cf is not None
    assert cf["contributing_tiers"] == []
    assert cf["blocked"] is True and cf["combined_score"] == 0.0
    assert cf["verdict"] == "blocked"


# =================================================================================================
# LLM-assist adjudication harness (a deterministic producer; the agent IS the judgment model).
# =================================================================================================
def _blocked_report():
    report = {"summary": {"aggregate_score": 0.9, "unmatched_worksheets": []},
              "visuals": [{"worksheet": "Bars", "visual_type": "barChart", "score": 0.6,
                           "band": "review", "fields_missing": ["Profit"], "fields_extra": [],
                           "type_note": None, "diagnosis": None}],
              "openability": {"available": True, "openable": False, "verdict": "blocked",
                              "error": "Failed in '_Measures.tmdl' at line 12",
                              "error_file": "_Measures.tmdl", "error_line": 12}}
    report["combined_fidelity"] = fo._combined_fidelity(report)
    return report


def test_build_fidelity_bundle_shape():
    report = _blocked_report()
    bundle = fo.build_fidelity_bundle(report)
    assert bundle["version"] == fo.LLM_BUNDLE_VERSION
    assert bundle["kind"] == fo.LLM_BUNDLE_KIND
    assert bundle["rules"] == list(fo.FIDELITY_ORACLE_RULES)
    assert bundle["deterministic"]["verdict"] == "blocked"
    assert bundle["deterministic"]["gate_locked"] is True
    assert bundle["deterministic"]["openable"] is False
    ids = {it["id"] for it in bundle["items"]}
    assert "blocker:openability" in ids and "visual:Bars" in ids
    assert bundle["summary"]["blockers"] >= 1
    # serialisable + never mutates the report
    json.dumps(bundle)
    assert "llm_assist" not in report


def test_build_fidelity_bundle_vision_item(tmp_path):
    png = tmp_path / "render.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    report = {"summary": {"aggregate_score": 0.9}, "visuals": [],
              "image": {"available": True, "ssim": 0.7, "band": "advisory"},
              "pbi_render": {"available": True, "pages": [{"page_id": "P1", "png": str(png)}]}}
    report["combined_fidelity"] = fo._combined_fidelity(report)
    bundle = fo.build_fidelity_bundle(report)
    assert bundle["summary"]["image_refs"] == 1
    assert len(bundle["image_refs"]) == 1
    vis = [it for it in bundle["items"] if it["id"] == "visual-diff:render"]
    assert vis and vis[0]["kind"] == "vision"
    assert vis[0]["evidence"]["image_refs"][0]["role"] == "powerbi-render"


def test_fidelity_image_refs_dedups_and_skips_missing(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    report = {"image_inputs": {"reference_png": None, "candidate_png": str(png)},
              "pbi_render": {"available": True,
                             "pages": [{"page_id": "P1", "png": str(png)},          # dup of candidate
                                       {"page_id": "P2", "png": str(tmp_path / "missing.png")}]}}
    refs = fo._fidelity_image_refs(report)
    assert len(refs) == 1 and os.path.basename(refs[0]["path"]) == "a.png"


def test_fidelity_agent_prompt_lists_rules_items_images(tmp_path):
    png = tmp_path / "r.png"
    png.write_bytes(b"\x89PNG")
    report = {"summary": {"aggregate_score": 0.9}, "visuals": [],
              "pbi_render": {"available": True, "pages": [{"page_id": "P1", "png": str(png)}]}}
    report["combined_fidelity"] = fo._combined_fidelity(report)
    prompt = fo.fidelity_agent_prompt(fo.build_fidelity_bundle(report))
    assert "Hard rules:" in prompt
    assert "Deterministic verdict (authoritative" in prompt
    assert "Rendered images to VIEW" in prompt and str(png) in prompt
    assert "STRICT JSON" in prompt


def test_apply_fidelity_adjudication_cannot_unblock():
    report = _blocked_report()
    answers = {"verdict": "faithful-candidate", "confidence": 0.95, "summary": "looks ok to me",
               "per_item": [{"id": "blocker:openability", "judgment": "blocker"}]}
    rec = fo.apply_fidelity_adjudication(report, answers)
    assert rec["available"] is True
    assert rec["deterministic_verdict"] == "blocked"
    assert rec["effective_verdict"] == "blocked"          # advisory cannot upgrade past the block
    assert rec["gate_locked"] is True
    assert rec["llm_verdict"] == "faithful-candidate"
    assert any("gate_locked" in n for n in rec["notes"])


def test_apply_fidelity_adjudication_accepts_json_string():
    report = {"combined_fidelity": {"verdict": "faithful-candidate"}}
    rec = fo.apply_fidelity_adjudication(
        report, '{"verdict": "faithful-candidate", "confidence": 0.8, "per_item": []}')
    assert rec["available"] is True and rec["gate_locked"] is False
    assert rec["effective_verdict"] == "faithful-candidate"


def test_apply_fidelity_adjudication_garbage_unavailable():
    report = {"combined_fidelity": {"verdict": "opens-needs-review"}}
    rec = fo.apply_fidelity_adjudication(report, "not json at all")
    assert rec["available"] is False and "no parseable" in rec["reason"]
    assert rec["deterministic_verdict"] == "opens-needs-review"


def test_llm_assist_tier_without_answers_carries_bundle_and_prompt():
    report = _blocked_report()
    rec = fo.llm_assist_tier(report)
    assert rec["available"] is False
    assert "bundle" in rec and "prompt" in rec
    assert rec["bundle"]["kind"] == fo.LLM_BUNDLE_KIND


def test_llm_assist_tier_with_answers_folds_back():
    report = _blocked_report()
    rec = fo.llm_assist_tier(report, answers={"verdict": "blocked", "confidence": 0.9,
                                              "summary": "model does not parse", "per_item": []})
    assert rec["available"] is True
    assert rec["effective_verdict"] == "blocked"
    assert "bundle" in rec and "prompt" in rec


# ----------------------------------------------------------------- run_oracle wires the new tiers --
def test_run_oracle_openability_wired_blocks(tmp_path, monkeypatch):
    report_dir = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb = tmp_path / "wb.twb"
    twb.write_text(TWB_XML, encoding="utf-8")
    monkeypatch.setattr(
        fo, "openability_tier",
        lambda **kw: {"tier": "openability", "available": True, "openable": False,
                      "verdict": "blocked", "error": "boom", "error_file": "_Measures.tmdl",
                      "error_line": 12, "advisory": True})
    result = fo.run_oracle(str(twb), report_dir, openability_options={})
    assert result["openability"]["openable"] is False
    cf = result["combined_fidelity"]
    assert cf["blocked"] is True and cf["verdict"] == "blocked"
    assert cf["combined_score"] == 0.0 and "raw_combined_score" in cf
    assert "⛔" in fo.render_markdown(result)


def test_run_oracle_llm_wired_without_and_with_answers(tmp_path):
    report_dir = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb = tmp_path / "wb.twb"
    twb.write_text(TWB_XML, encoding="utf-8")
    no_answers = fo.run_oracle(str(twb), report_dir, llm_options={})
    assert no_answers["llm_assist"]["available"] is False
    assert "bundle" in no_answers["llm_assist"] and "prompt" in no_answers["llm_assist"]
    with_answers = fo.run_oracle(
        str(twb), report_dir,
        llm_options={"answers": {"verdict": "faithful-candidate", "confidence": 0.8,
                                 "summary": "clean", "per_item": []}})
    assert with_answers["llm_assist"]["available"] is True
    assert "LLM-assist" in fo.render_markdown(with_answers)


