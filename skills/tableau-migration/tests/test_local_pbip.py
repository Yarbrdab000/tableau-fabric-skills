"""Tests for the local .pbip writer (write_local_pbip / build_thin_report_parts).

These lock the exact layout + schemas a pilot agent previously had to improvise (and got the
.pbip $schema wrong on the first try): the project must open in Power BI Desktop, which means the
.pbip pointer schema, the report's byPath dataset link, and every JSON part must be valid.
"""
import json
import os
import sys

HERE = os.path.dirname(__file__)
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import assemble_model as A  # noqa: E402


def _model_parts():
    return {
        "definition/model.tmdl": "model Model\n",
        "definition/tables/Orders.tmdl": "table Orders\n\tcolumn Sales\n\t\tdataType: double\n",
    }


def test_thin_report_parts_bind_by_path_to_model():
    parts = A.build_thin_report_parts("Superstore")
    pbir = json.loads(parts["definition.pbir"])
    assert pbir["datasetReference"]["byPath"]["path"] == "../Superstore.SemanticModel"
    # every report part must be valid JSON
    for name, text in parts.items():
        json.loads(text)
    # an empty page exists so Desktop can open the report
    assert "definition/pages/page1/page.json" in parts
    pages = json.loads(parts["definition/pages/pages.json"])
    assert pages["activePageName"] == "page1"


def test_thin_report_json_has_base_theme():
    """Regression: current Power BI Desktop NREs in GetEnhancedReportDocument when report.json has
    no themeCollection.baseTheme, so the report (canvas/Visualizations pane) never loads even though
    the semantic model does. The thin shell must carry the same baseTheme as the full viz seam."""
    parts = A.build_thin_report_parts("Superstore")
    report = json.loads(parts["definition/report.json"])
    assert report["themeCollection"]["baseTheme"]["name"]


def test_write_local_pbip_layout_and_schema(tmp_path):
    dest = str(tmp_path / "out")
    pbip = A.write_local_pbip(_model_parts(), dest, model_name="Superstore")

    assert os.path.isfile(pbip)
    assert os.path.isdir(os.path.join(dest, "Superstore.SemanticModel"))
    assert os.path.isdir(os.path.join(dest, "Superstore.Report"))
    assert os.path.isfile(os.path.join(dest, "Superstore.SemanticModel", "definition", "model.tmdl"))

    proj = json.loads(open(pbip, encoding="utf-8").read())
    # the exact schema the pilot agent first got wrong
    assert proj["$schema"] == (
        "https://developer.microsoft.com/json-schemas/fabric/"
        "pbip/pbipProperties/1.0.0/schema.json"
    )
    assert proj["artifacts"] == [{"report": {"path": "Superstore.Report"}}]

    pbir = json.loads(
        open(os.path.join(dest, "Superstore.Report", "definition.pbir"), encoding="utf-8").read()
    )
    assert pbir["datasetReference"]["byPath"]["path"] == "../Superstore.SemanticModel"


def test_write_local_pbip_distinct_report_name(tmp_path):
    dest = str(tmp_path / "out")
    A.write_local_pbip(_model_parts(), dest, model_name="Superstore", report_name="Overview")
    assert os.path.isdir(os.path.join(dest, "Overview.Report"))
    proj = json.loads(open(os.path.join(dest, "Superstore.pbip"), encoding="utf-8").read())
    assert proj["artifacts"] == [{"report": {"path": "Overview.Report"}}]


def test_write_local_pbip_accepts_custom_report_parts(tmp_path):
    dest = str(tmp_path / "out")
    custom = {".platform": '{"x": 1}', "definition.pbir": '{"y": 2}'}
    A.write_local_pbip(_model_parts(), dest, model_name="M", report_parts=custom)
    assert os.path.isfile(os.path.join(dest, "M.Report", ".platform"))
    assert open(os.path.join(dest, "M.Report", ".platform"), encoding="utf-8").read() == '{"x": 1}'


# -- field-parameter (swap) self-service report -------------------------------------
def _swap_specs():
    """Specs as ``emit_field_parameters`` surfaces them: one dim + one measure swap."""
    return [
        {"calc_name": "Dim Swap", "table_name": "Dim Swap", "display_col": "Dim Swap",
         "role": "dimension", "entries": [
             {"label": "Region", "table": "Orders", "column": "Region",
              "is_measure": False, "order": 0},
             {"label": "Category", "table": "Orders", "column": "Category",
              "is_measure": False, "order": 1}]},
        {"calc_name": "Measure Swap", "table_name": "Measure Swap", "display_col": "Measure Swap",
         "role": "measure", "entries": [
             {"label": "sales", "table": "_Measures", "column": "Total Sales",
              "is_measure": True, "order": 0},
             {"label": "profit", "table": "_Measures", "column": "Total Profit",
              "is_measure": True, "order": 1}]},
    ]


def test_build_swap_report_parts_emits_self_service_page():
    parts = A.build_swap_report_parts("Superstore", _swap_specs())
    # every part is valid JSON, still bound by path to the model
    for text in parts.values():
        json.loads(text)
    pbir = json.loads(parts["definition.pbir"])
    assert pbir["datasetReference"]["byPath"]["path"] == "../Superstore.SemanticModel"
    # the active page is the self-service page (not the thin page1 shell)
    pages = json.loads(parts["definition/pages/pages.json"])
    page_name = pages["activePageName"]
    assert page_name != "page1"
    assert f"definition/pages/{page_name}/page.json" in parts
    # a table visual that consumes the field parameters + one slicer per spec
    visuals = [json.loads(v) for k, v in parts.items() if k.endswith("visual.json")]
    types = sorted(v["visual"]["visualType"] for v in visuals)
    assert types == ["listSlicer", "listSlicer", "tableEx"]
    table = next(v for v in visuals if v["visual"]["visualType"] == "tableEx")
    well = table["visual"]["query"]["queryState"]["Values"]
    assert len(well["fieldParameters"]) == 2


def test_build_swap_report_parts_falls_back_to_thin_without_usable_specs():
    # no specs, and specs with empty entries, both yield the openable thin one-page shell
    for specs in ([], [{"calc_name": "X", "table_name": "X", "display_col": "X", "entries": []}]):
        parts = A.build_swap_report_parts("Superstore", specs)
        pages = json.loads(parts["definition/pages/pages.json"])
        assert pages["activePageName"] == "page1"


def test_write_local_pbip_swap_specs_writes_self_service_report(tmp_path):
    dest = str(tmp_path / "out")
    A.write_local_pbip(_model_parts(), dest, model_name="Superstore", swap_specs=_swap_specs())
    report_dir = os.path.join(dest, "Superstore.Report")
    # the report folder carries a self-service page with a fieldParameters table visual
    visual_paths = []
    for root, _dirs, files in os.walk(report_dir):
        for fn in files:
            if fn == "visual.json":
                visual_paths.append(os.path.join(root, fn))
    visuals = [json.loads(open(p, encoding="utf-8").read()) for p in visual_paths]
    assert any("fieldParameters" in v["visual"]["query"]["queryState"].get("Values", {})
               for v in visuals)


def test_write_local_pbip_explicit_report_parts_beats_swap_specs(tmp_path):
    dest = str(tmp_path / "out")
    custom = {".platform": '{"x": 1}', "definition.pbir": '{"y": 2}'}
    A.write_local_pbip(_model_parts(), dest, model_name="M",
                       report_parts=custom, swap_specs=_swap_specs())
    # the explicit report wins; no self-service page parts were written
    assert open(os.path.join(dest, "M.Report", ".platform"), encoding="utf-8").read() == '{"x": 1}'
    assert not os.path.isdir(os.path.join(dest, "M.Report", "definition", "pages"))
