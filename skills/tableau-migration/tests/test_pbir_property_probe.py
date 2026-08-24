"""The PBIR property probe: ask Power BI Desktop what JSON a feature writes.

The problem it solves is a TIMING one, not a knowledge one. Every published description of PBIR
formatting properties lags Desktop by at least a month, so on the day a feature ships there is no
authoritative source for the JSON you must emit. Measured 2026-08-24, all on one machine:

    npm @microsoft/powerbi-core-visual-schema  0.1.1  (latest published, no release tag)
    PBIR visualContainer json-schema           2.9.0  (May 2026)
    Report Theme JSON Schema                   2.156  (July 2026)
    Power BI Desktop installed                 2.157  (August 2026)

And the PBIR schema cannot close the gap even in principle: a visual's ``objects`` member resolves to
``DataViewObjectDefinitions``, which permits arbitrary object names and arbitrary properties. So
``validate`` returns 0 errors for a property that does not exist -- verified on a hand-written slicer
carrying ``data.relativeRange``, which validated exactly as clean as an invented name would.

Desktop knows, because it wrote the release. Snapshot -> human sets the feature in the format pane and
saves -> diff names the property.
"""
import io
import json
import os

import pbir_property_probe as PP


def _visual(objects=None, name="v1"):
    v = {"$schema": "x", "name": name,
         "position": {"x": 0, "y": 0, "z": 1, "width": 100, "height": 100, "tabOrder": 1},
         "visual": {"visualType": "donutChart", "query": {}}}
    if objects is not None:
        v["visual"]["objects"] = objects
    return v


def _report(tmp_path, visuals):
    """A minimal ``.Report`` tree: definition/pages/<page>/visuals/<v>/visual.json."""
    rep = tmp_path / "M.Report"
    for i, vis in enumerate(visuals):
        d = rep / "definition" / "pages" / "p1" / "visuals" / ("v%d" % i)
        d.mkdir(parents=True, exist_ok=True)
        io.open(str(d / "visual.json"), "w", encoding="utf-8", newline="").write(
            json.dumps(vis, indent=2))
    return str(rep)


def _lit(value):
    return {"expr": {"Literal": {"Value": value}}}


# =============================================================================
# Finding the report, and snapshotting it.
# =============================================================================
def test_find_report_dir_accepts_the_report_folder_or_anything_above_it(tmp_path):
    rep = _report(tmp_path, [_visual()])
    assert PP.find_report_dir(rep) == os.path.abspath(rep)
    assert PP.find_report_dir(str(tmp_path)) == os.path.abspath(rep)


def test_snapshot_records_every_leaf_value(tmp_path):
    rep = _report(tmp_path, [_visual({"centerValue": [{"properties": {"show": _lit("true")}}]})])
    snap = PP.snapshot(rep)
    flat = list(snap["files"].values())[0]
    assert "/visual/objects/centerValue[0]/properties/show/expr/Literal/Value" in flat
    assert flat["/visual/visualType"] == "donutChart"


# =============================================================================
# The discovery itself.
# =============================================================================
def test_a_property_set_in_desktop_is_named_by_the_diff(tmp_path):
    """THE point of the tool: the property Desktop wrote is reported by name and by JSON path."""
    rep = _report(tmp_path, [_visual()])
    before = PP.snapshot(rep)

    # stand-in for a human setting the feature in the format pane and saving
    p = os.path.join(rep, "definition", "pages", "p1", "visuals", "v0", "visual.json")
    doc = json.loads(io.open(p, encoding="utf-8-sig").read())
    doc["visual"]["objects"] = {"centerValue": [{"properties": {"show": _lit("true")}}]}
    io.open(p, "w", encoding="utf-8", newline="").write(json.dumps(doc, indent=2))

    added, changed, removed = PP.diff(before, PP.snapshot(rep))
    paths = [PP._object_path(p_) for _f, p_, _b, _a in added]
    assert "centerValue.show" in paths
    assert not changed and not removed


def test_a_changed_value_reports_both_sides(tmp_path):
    # Discovering the ENUM a feature uses needs the old value too -- "Dropdown -> Relative" is the
    # answer, not "Relative".
    rep = _report(tmp_path, [_visual({"data": [{"properties": {"mode": _lit("'Dropdown'")}}]})])
    before = PP.snapshot(rep)
    p = os.path.join(rep, "definition", "pages", "p1", "visuals", "v0", "visual.json")
    doc = json.loads(io.open(p, encoding="utf-8-sig").read())
    doc["visual"]["objects"]["data"][0]["properties"]["mode"] = _lit("'Relative'")
    io.open(p, "w", encoding="utf-8", newline="").write(json.dumps(doc, indent=2))

    _added, changed, _removed = PP.diff(before, PP.snapshot(rep))
    assert len(changed) == 1
    _f, path, was, now = changed[0]
    assert PP._object_path(path) == "data.mode"
    assert (was, now) == ("'Dropdown'", "'Relative'")


def test_save_noise_is_excluded_so_a_real_discovery_is_not_buried(tmp_path):
    """Desktop rewrites ids/ordering on every save. Left in, one property change arrives with dozens
    of irrelevant lines and the answer is lost -- which defeats the tool."""
    rep = _report(tmp_path, [_visual()])
    before = PP.snapshot(rep)
    p = os.path.join(rep, "definition", "pages", "p1", "visuals", "v0", "visual.json")
    doc = json.loads(io.open(p, encoding="utf-8-sig").read())
    doc["position"]["tabOrder"] = 999
    doc["name"] = "renamed-by-desktop"
    io.open(p, "w", encoding="utf-8", newline="").write(json.dumps(doc, indent=2))

    added, changed, removed = PP.diff(before, PP.snapshot(rep))
    assert (added, changed, removed) == ([], [], [])
    # ...but --all keeps them, because sometimes the id IS the thing being investigated.
    _a, changed_all, _r = PP.diff(before, PP.snapshot(rep), include_noise=True)
    assert {PP._object_path(x[1]) for x in changed_all} == {None}
    assert len(changed_all) == 2


def test_an_unreadable_file_is_recorded_not_raised(tmp_path):
    # Desktop writes files progressively; a snapshot taken mid-save must not crash the probe.
    rep = _report(tmp_path, [_visual()])
    p = os.path.join(rep, "definition", "pages", "p1", "visuals", "v0", "visual.json")
    io.open(p, "w", encoding="utf-8", newline="").write("{ not json")
    snap = PP.snapshot(rep)
    assert "<unreadable>" in list(snap["files"].values())[0]


# =============================================================================
# The offline schema half.
# =============================================================================
def _theme_schema(tmp_path):
    """Shaped like the real Report Theme JSON Schema: visual-<type> under nested allOf/properties."""
    doc = {"properties": {"visualStyles": {"properties": {
        "visual-donutChart": {"allOf": [
            {"x": 1},
            {"properties": {
                "centerValue": {"title": "Center value",
                                "properties": {"show": {"title": "Show", "type": "bool"}}},
                "centerBackground": {"title": "Background",
                                     "properties": {"fillColor": {"title": "Fill", "type": "fill"}}},
            }},
        ]},
        "visual-pivotTable": {"allOf": [{}, {"properties": {
            "columnHeaders": {"title": "Column headers", "properties": {
                "showExpandCollapseButtons": {"title": "+/- icons", "type": "bool"}}}}}]},
    }}}}
    p = tmp_path / "theme.json"
    io.open(str(p), "w", encoding="utf-8", newline="").write(json.dumps(doc))
    return str(p)


def test_schema_lookup_finds_a_visual_types_properties(tmp_path):
    hits = dict(PP.schema_lookup(_theme_schema(tmp_path), "donutChart"))
    assert "centerValue" in hits and hits["centerValue"] == "Center value"
    assert "centerValue.show" in hits


def test_schema_lookup_strips_the_allOf_plumbing_from_the_path(tmp_path):
    # ``allOf[1]`` is JSON-Schema composition, not part of the property path a caller must emit.
    for dotted, _title in PP.schema_lookup(_theme_schema(tmp_path), "donutChart"):
        assert "allOf" not in dotted


def test_schema_lookup_filters_by_pattern(tmp_path):
    hits = [d for d, _t in PP.schema_lookup(_theme_schema(tmp_path), "donutChart", "background")]
    assert hits and all("centerBackground" in h for h in hits)


def test_schema_lookup_scopes_to_the_requested_visual_type(tmp_path):
    hits = [d for d, _t in PP.schema_lookup(_theme_schema(tmp_path), "pivotTable")]
    assert any("showExpandCollapseButtons" in h for h in hits)
    assert not any("centerValue" in h for h in hits), "donutChart properties leaked into pivotTable"
