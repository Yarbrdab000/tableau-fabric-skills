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
