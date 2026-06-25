"""Quarantined tests for the advisory structural fidelity oracle (``fidelity_oracle``).

Hermetic: every fixture is built inline (a tiny Tableau ``.twb`` XML string + an on-disk PBIR
report tree under ``tmp_path``) so the suite never depends on the migration scratch outputs. These
tests are deliberately NOT under ``tests/`` -- ``pytest tests`` (the engine's green gate) must not
collect them, and the optional value/image tiers must degrade gracefully so importing the module
never fails offline.
"""
import json
import os

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


# --------------------------------------------------------------------------- TWB reader
def test_read_twb_worksheets_and_families():
    twb = fo.read_twb_views(TWB_XML)
    ws = twb["worksheets"]
    assert set(ws) == {"Bars", "Trend", "Card"}
    assert ws["Bars"]["family"] == fo.FAM_BAR
    assert ws["Trend"]["family"] == fo.FAM_AREA and ws["Trend"]["family_asserted"] is True
    assert ws["Card"]["family"] == fo.FAM_CARD
    assert {f["norm"] for f in ws["Bars"]["fields"]} == {"category", "sales", "profit"}
    assert {f["norm"] for f in ws["Bars"]["measures"]} == {"sales", "profit"}
    assert {f["norm"] for f in ws["Bars"]["dims"]} == {"category"}


def test_twb_caption_resolution_for_calc_member():
    twb = fo.read_twb_views(TWB_XML)
    card = twb["worksheets"]["Card"]
    # the Measure Values card resolves [Calculation_99] -> caption 'My Ratio', plus Discount
    norms = {f["norm"] for f in card["fields"]}
    assert "myratio" in norms and "discount" in norms


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


def test_infer_family_card_when_no_dims():
    fam, asserted = fo._infer_twb_family("Automatic", [], [{"norm": "sales"}], False, False)
    assert fam == fo.FAM_CARD and asserted is True
    fam2, asserted2 = fo._infer_twb_family("Automatic", [{"norm": "cat"}], [{"norm": "s"}], False, False)
    assert fam2 == fo.FAM_BAR and asserted2 is False  # plausible, not asserted


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


def test_run_oracle_and_markdown(tmp_path):
    report = _write_pbir(str(tmp_path), "Dash", _faithful_visuals())
    twb_path = tmp_path / "wb.twb"
    twb_path.write_text(TWB_XML, encoding="utf-8")
    result = fo.run_oracle(str(twb_path), report)
    assert result["advisory"] is True and result["kind"] == fo.ORACLE_KIND
    md = fo.render_markdown(result)
    assert "Fidelity Oracle" in md and "Aggregate" in md


# --------------------------------------------------------------------------- optional tiers / guards
def test_optional_tiers_degrade_gracefully():
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

