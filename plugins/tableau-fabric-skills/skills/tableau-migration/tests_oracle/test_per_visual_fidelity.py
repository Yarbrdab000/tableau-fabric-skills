"""Quarantined tests for the per-visual fidelity verdict layer (the migration loop's objective
function): the four-state REPRODUCED / PARTIAL / DEGRADED / MISSING classifier and its scoped
aggregator built on top of the structural ``score_report`` output.

These live in ``tests_oracle/`` and are NOT collected by ``pytest tests`` (the engine gate). They
cover the classifier ladder in isolation, the scoped aggregator (on-view scoping + MISSING
injection + tally/objective math + non-mutation), and one end-to-end pass through the real readers
(``score_report`` -> ``per_visual_fidelity``) for the benchmark cases (area->area = REPRODUCED,
area->line = PARTIAL, an on-view sheet absent from the rebuild = MISSING) plus the CLI/markdown wiring.
"""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fidelity_oracle as fo


# --------------------------------------------------------------------------- small fixture builders
def _col_field(entity, prop):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def _agg_field(entity, prop, func=0):
    return {"Aggregation": {"Expression": {"Column": {
        "Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}, "Function": func}}


def _projection(field, native=None):
    return {"field": field, "queryRef": "q", "nativeQueryRef": native or "n"}


def _visual_json(name, vtype, position, query_state):
    return {"name": name, "position": position,
            "visual": {"visualType": vtype, "query": {"queryState": query_state}}}


def _write_pbir(base, visuals, page_display="Dash", page_name="page1", width=1280, height=720):
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


TWB_XML = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <datasources>
    <datasource name='fed.0abc' caption='Sample'>
      <column name='[Sales]' caption='Sales' datatype='real' role='measure'/>
      <column name='[Profit]' caption='Profit' datatype='real' role='measure'/>
      <column name='[Category]' caption='Category' datatype='string' role='dimension'/>
      <column name='[Order Date]' caption='Order Date' datatype='date' role='dimension'/>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Bars'>
      <table>
        <view><datasources><datasource name='fed.0abc' caption='Sample'/></datasources></view>
        <panes><pane><mark class='Automatic'/>
          <encodings><color column='[fed.0abc].[sum:Profit:qk]'/></encodings>
        </pane></panes>
        <rows>[fed.0abc].[none:Category:nk]</rows>
        <cols>[fed.0abc].[sum:Sales:qk]</cols>
      </table>
    </worksheet>
    <worksheet name='Trend'>
      <table>
        <view><datasources><datasource name='fed.0abc' caption='Sample'/></datasources></view>
        <panes><pane><mark class='Area'/><encodings/></pane></panes>
        <rows>[fed.0abc].[sum:Sales:qk]</rows>
        <cols>[fed.0abc].[tmn:Order Date:qk]</cols>
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


def _bars_visual():
    return _visual_json(
        "v-bars", "clusteredBarChart",
        {"x": 0.0, "y": 0.0, "width": 640.0, "height": 720.0, "z": 0},
        {"Category": {"projections": [_projection(_col_field("fed.0abc", "Category"), "Category")]},
         "Y": {"projections": [_projection(_agg_field("fed.0abc", "Sales"), "Sum of Sales"),
                               _projection(_agg_field("fed.0abc", "Profit"), "Sum of Profit")]}})


def _trend_visual(vtype):
    return _visual_json(
        "v-trend", vtype,
        {"x": 640.0, "y": 0.0, "width": 640.0, "height": 720.0, "z": 0},
        {"Category": {"projections": [_projection(_col_field("fed.0abc", "Order_Date"), "Order_Date")]},
         "Y": {"projections": [_projection(_agg_field("fed.0abc", "Sales"), "Sum of Sales")]}})


def _result(worksheet, type_s, note, matched, missing, visual_type="someChart"):
    """A minimal structural per-visual record (the shape classify_visual_state reads)."""
    return {"worksheet": worksheet, "visual": "v-%s" % worksheet, "visual_type": visual_type,
            "components": {"type": type_s, "fields": 1.0, "roles": 1.0}, "type_note": note,
            "score": 0.9, "band": "strong", "diagnosis": None,
            "fields_matched": list(matched), "fields_missing": list(missing), "fields_extra": []}


# --------------------------------------------------------------------------- classifier ladder
def test_classify_exact_type_all_fields_is_reproduced():
    state, _ = fo.classify_visual_state(_result("S1", 1.0, "type-match", ["sales"], []))
    assert state == fo.STATE_REPRODUCED


def test_classify_exact_type_missing_field_is_partial():
    state, reason = fo.classify_visual_state(
        _result("S1", 1.0, "type-match", ["sales"], ["profit"]))
    assert state == fo.STATE_PARTIAL
    assert "missing" in reason


def test_classify_related_type_all_fields_is_partial():
    # Tableau AREA rebuilt as a Power BI LINE with the full field set: recognizable substitution.
    state, reason = fo.classify_visual_state(
        _result("S1", fo.TYPE_RELATED_CREDIT, "type-related (area~line)", ["sales", "orderdate"], []))
    assert state == fo.STATE_PARTIAL
    assert "related" in reason


def test_classify_related_type_missing_field_is_degraded():
    state, _ = fo.classify_visual_state(
        _result("S1", fo.TYPE_RELATED_CREDIT, "type-related (area~line)", ["sales"], ["orderdate"]))
    assert state == fo.STATE_DEGRADED


def test_classify_type_mismatch_is_degraded():
    # Tableau MAP rebuilt as a table: asserted wrong family -> a material fidelity loss.
    state, reason = fo.classify_visual_state(
        _result("Map", 0.0, "type-mismatch (map vs table)", ["state"], []))
    assert state == fo.STATE_DEGRADED
    assert "wrong chart type" in reason


def test_classify_no_matched_fields_is_degraded():
    state, _ = fo.classify_visual_state(_result("S1", 1.0, "type-match", [], ["sales"]))
    assert state == fo.STATE_DEGRADED


def test_classify_indeterminate_type_all_fields_is_partial():
    state, _ = fo.classify_visual_state(
        _result("S1", fo.TYPE_UNASSERTED_CREDIT, "type-indeterminate", ["sales"], []))
    assert state == fo.STATE_PARTIAL


# --------------------------------------------------------------------------- scoped aggregator
def _three_state_report():
    return {
        "visuals": [
            _result("Sheet 1", 1.0, "type-match", ["sales"], []),                 # REPRODUCED
            _result("Sheet 2", 1.0, "type-match", ["sales"], ["profit"]),         # PARTIAL
            _result("Sheet 3", 0.0, "type-mismatch (map vs table)", ["state"], []),  # DEGRADED
        ],
        "summary": {"unmatched_worksheets": ["Sheet 4"]},                         # MISSING
    }


def test_per_visual_tally_and_objective_unscoped():
    out = fo.per_visual_fidelity(_three_state_report())
    assert out["tier"] == "per-visual" and out["advisory"] is True
    assert out["tally"] == {fo.STATE_REPRODUCED: 1, fo.STATE_PARTIAL: 1,
                            fo.STATE_DEGRADED: 1, fo.STATE_MISSING: 1}
    assert out["scored_visuals"] == 4
    # (1.0 + 0.5 + 0.25 + 0.0) / 4
    assert out["objective_score"] == 0.4375
    # worst-first ordering: lowest-credit state leads.
    assert out["verdicts"][0]["state"] == fo.STATE_MISSING


def test_per_visual_on_view_scopes_and_ignores_off_view():
    out = fo.per_visual_fidelity(_three_state_report(), on_view=["Sheet 1", "Sheet 4"])
    states = {v["worksheet"]: v["state"] for v in out["verdicts"]}
    assert states == {"Sheet 1": fo.STATE_REPRODUCED, "Sheet 4": fo.STATE_MISSING}
    assert out["scored_visuals"] == 2
    assert "Sheet 2" not in states and "Sheet 3" not in states  # off-view sheets dropped
    assert out["scoped_to"] == ["Sheet 1", "Sheet 4"]


def test_per_visual_on_view_name_absent_from_rebuild_is_missing():
    out = fo.per_visual_fidelity(_three_state_report(), on_view=["Sheet 1", "Ghost"])
    ghost = [v for v in out["verdicts"] if v["worksheet"] == "Ghost"]
    assert ghost and ghost[0]["state"] == fo.STATE_MISSING
    assert "not found" in ghost[0]["reason"]


def test_per_visual_never_mutates_report():
    report = _three_state_report()
    before = copy.deepcopy(report)
    fo.per_visual_fidelity(report, on_view=["Sheet 1"])
    assert report == before
    assert "per_visual" not in report  # the aggregator returns; it does not attach


def test_per_visual_empty_report_is_safe():
    out = fo.per_visual_fidelity({"visuals": [], "summary": {}})
    assert out["scored_visuals"] == 0 and out["objective_score"] is None
    assert out["tally"] == {fo.STATE_REPRODUCED: 0, fo.STATE_PARTIAL: 0,
                            fo.STATE_DEGRADED: 0, fo.STATE_MISSING: 0}


# --------------------------------------------------------------------------- end-to-end (real readers)
def test_end_to_end_reproduced_and_missing(tmp_path):
    twb = fo.read_twb_views(TWB_XML)
    report_dir = _write_pbir(str(tmp_path), [_bars_visual(), _trend_visual("areaChart")])
    result = fo.score_report(twb, fo.read_pbir_report(report_dir))
    out = fo.per_visual_fidelity(result, on_view=["Trend", "Bars", "Ghost Sheet"])
    states = {v["worksheet"]: v["state"] for v in out["verdicts"]}
    assert states["Trend"] == fo.STATE_REPRODUCED      # Area -> areaChart, fields intact
    assert states["Bars"] == fo.STATE_REPRODUCED       # bar -> clusteredBarChart, fields intact
    assert states["Ghost Sheet"] == fo.STATE_MISSING   # on-view sheet absent from the rebuild
    assert out["objective_score"] == round((1.0 + 1.0 + 0.0) / 3, 4)


def test_end_to_end_area_to_line_is_partial(tmp_path):
    twb = fo.read_twb_views(TWB_XML)
    report_dir = _write_pbir(str(tmp_path), [_bars_visual(), _trend_visual("lineChart")])
    result = fo.score_report(twb, fo.read_pbir_report(report_dir))
    out = fo.per_visual_fidelity(result, on_view=["Trend"])
    trend = [v for v in out["verdicts"] if v["worksheet"] == "Trend"][0]
    assert trend["state"] == fo.STATE_PARTIAL          # Area ~ line related substitution


# --------------------------------------------------------------------------- run_oracle + markdown wiring
def test_run_oracle_attaches_per_visual_and_markdown(tmp_path):
    twb_path = os.path.join(str(tmp_path), "wb.twb")
    with open(twb_path, "w", encoding="utf-8") as fh:
        fh.write(TWB_XML)
    report_dir = _write_pbir(os.path.join(str(tmp_path), "out"),
                             [_bars_visual(), _trend_visual("areaChart")])
    report = fo.run_oracle(twb_path, report_dir,
                           per_visual_options={"on_view": ["Trend", "Bars"]})
    pv = report.get("per_visual")
    assert pv is not None and pv["tally"][fo.STATE_REPRODUCED] == 2
    md = fo.render_markdown(report)
    assert "Per-visual fidelity" in md and "REPRODUCED" in md


def test_run_oracle_without_per_visual_option_omits_block(tmp_path):
    twb_path = os.path.join(str(tmp_path), "wb.twb")
    with open(twb_path, "w", encoding="utf-8") as fh:
        fh.write(TWB_XML)
    report_dir = _write_pbir(os.path.join(str(tmp_path), "out"),
                             [_bars_visual(), _trend_visual("areaChart")])
    report = fo.run_oracle(twb_path, report_dir)
    assert "per_visual" not in report  # additive: off by default, no schema change
