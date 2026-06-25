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
    assert flat == [{"label": "Sales", "measure": "Sales", "expected": 100.0, "filter": None}]
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


def test_dax_value_tier_live_if_available():
    # Offline-safe: skips unless a real Power BI Desktop model is reachable on this host.
    try:
        AdomdConnection = fo._load_adomd()
    except Exception:  # noqa: BLE001
        pytest.skip("ADOMD.NET / pythonnet not available")
    live_port = None
    for inst in fo.discover_pbi_instances():
        try:
            c = AdomdConnection("Data Source=localhost:%d" % inst["port"])
            c.Open()
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


