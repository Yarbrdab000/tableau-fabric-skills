"""Offline tests for the report-deploy path added to deploy_to_fabric.py.

Covers the PURE report builders + folder I/O + the byConnection rebind + PBIP discovery, and drives
``deploy_report`` / ``deploy_pbip`` through a fake ``_http`` so the create/update routing and the
model-then-report order are exercised WITHOUT touching a Fabric tenant or the network.
"""
import base64
import json

import pytest

import deploy_to_fabric as D

_WS_GUID = "11111111-2222-3333-4444-555555555555"
_MODEL_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

_PBIR_BYPATH = json.dumps({
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/"
               "definitionProperties/2.0.0/schema.json",
    "version": "4.0",
    "datasetReference": {"byPath": {"path": "../DemoModel.SemanticModel"}},
})


def _write_report_folder(root, *, pbir=_PBIR_BYPATH, display="DemoReport"):
    (root / "definition").mkdir(parents=True)
    (root / "definition.pbir").write_text(pbir, encoding="utf-8")
    (root / "definition" / "report.json").write_text("{}", encoding="utf-8")
    (root / "definition" / "version.json").write_text('{"version": "2.0.0"}', encoding="utf-8")
    (root / ".platform").write_text(
        json.dumps({"metadata": {"type": "Report", "displayName": display}}), encoding="utf-8")


def _write_model_folder(root):
    (root / "definition").mkdir(parents=True)
    (root / ".platform").write_text('{"metadata": {"type": "SemanticModel"}}', encoding="utf-8")
    (root / "definition.pbism").write_text('{"version": "4.0"}', encoding="utf-8")
    (root / "definition" / "model.tmdl").write_text("model DemoModel", encoding="utf-8")


# -- read_report_folder ---------------------------------------------------------------------
def test_read_report_folder_captures_pbir_and_posix_keys(tmp_path):
    root = tmp_path / "DemoReport.Report"
    _write_report_folder(root)
    (root / "notes.txt").write_text("skip me", encoding="utf-8")  # non-report file is skipped

    parts = D.read_report_folder(str(root))

    assert set(parts) == {
        "definition.pbir", "definition/report.json", "definition/version.json", ".platform",
    }
    assert "definition.pbir" in parts  # the part read_model_folder would miss
    assert all("\\" not in k for k in parts)


def test_read_report_folder_empty_raises(tmp_path):
    (tmp_path / "Empty.Report").mkdir()
    with pytest.raises(FileNotFoundError):
        D.read_report_folder(str(tmp_path / "Empty.Report"))


# -- build_report_create_payload / build_report_update_payload ------------------------------
def test_build_report_create_payload_has_displayname_and_base64_parts():
    parts = {"definition.pbir": _PBIR_BYPATH}
    body = D.build_report_create_payload("DemoReport", parts, description="hi")

    assert body["displayName"] == "DemoReport"
    assert body["description"] == "hi"
    one = body["definition"]["parts"][0]
    assert one["path"] == "definition.pbir"
    assert one["payloadType"] == "InlineBase64"
    assert base64.b64decode(one["payload"]).decode("utf-8") == _PBIR_BYPATH


def test_build_report_update_payload_has_no_displayname():
    body = D.build_report_update_payload({"definition.pbir": _PBIR_BYPATH})
    assert "displayName" not in body
    assert body["definition"]["parts"][0]["path"] == "definition.pbir"


# -- rebind_report_byConnection -------------------------------------------------------------
def test_rebind_byconnection_exact_shape_preserves_schema_and_version():
    parts = {"definition.pbir": _PBIR_BYPATH, "definition/report.json": "{}"}
    out = D.rebind_report_byConnection(parts, _MODEL_ID)

    doc = json.loads(out["definition.pbir"])
    assert doc["datasetReference"] == {
        "byConnection": {"connectionString": f"semanticmodelid={_MODEL_ID}"}}
    assert "byPath" not in json.dumps(doc["datasetReference"])
    # schema + version are carried through untouched (the byConnection example's exact shape)
    assert doc["$schema"].endswith("definitionProperties/2.0.0/schema.json")
    assert doc["version"] == "4.0"
    # other parts are untouched; the input dict is not mutated
    assert out["definition/report.json"] == "{}"
    assert "byPath" in parts["definition.pbir"]


def test_rebind_byconnection_fail_closed_cases():
    good = {"definition.pbir": _PBIR_BYPATH}
    assert D.rebind_report_byConnection(good, "") is None           # empty model id
    assert D.rebind_report_byConnection(good, None) is None         # no model id
    assert D.rebind_report_byConnection({"other": "x"}, _MODEL_ID) is None   # no definition.pbir
    assert D.rebind_report_byConnection({"definition.pbir": "{bad"}, _MODEL_ID) is None  # bad JSON
    assert D.rebind_report_byConnection("nope", _MODEL_ID) is None  # not a dict
    assert D.rebind_report_byConnection({"definition.pbir": "[1,2]"}, _MODEL_ID) is None  # not obj


# -- find_item_id idempotency on a reports-shaped list --------------------------------------
def test_find_item_id_matches_report_by_name():
    reports = [{"displayName": "Other", "id": "1"}, {"displayName": "Demo Report", "id": "rep-9"}]
    assert D.find_item_id(reports, "demo report") == "rep-9"
    assert D.find_item_id(reports, "missing") is None


# -- discover_pbip --------------------------------------------------------------------------
def test_discover_pbip_finds_one_model_and_report(tmp_path):
    bundle = tmp_path / "DemoBundle"
    _write_model_folder(bundle / "DemoModel.SemanticModel")
    _write_report_folder(bundle / "DemoReport.Report")
    (bundle / "DemoModel.pbip").write_text("{}", encoding="utf-8")

    model_dir, report_dir = D.discover_pbip(str(bundle))
    assert model_dir.endswith("DemoModel.SemanticModel")
    assert report_dir.endswith("DemoReport.Report")

    # a .pbip pointer file resolves to its parent bundle
    m2, r2 = D.discover_pbip(str(bundle / "DemoModel.pbip"))
    assert (m2, r2) == (model_dir, report_dir)


def test_discover_pbip_missing_and_ambiguous(tmp_path):
    empty = tmp_path / "Empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        D.discover_pbip(str(empty))

    _write_model_folder(empty / "A.SemanticModel")
    _write_report_folder(empty / "One.Report")
    _write_report_folder(empty / "Two.Report")
    with pytest.raises(RuntimeError):
        D.discover_pbip(str(empty))


def test_report_name_from_folder_prefers_platform_displayname(tmp_path):
    root = tmp_path / "folder-stem.Report"
    _write_report_folder(root, display="Pretty Name")
    assert D._report_name_from_folder(str(root)) == "Pretty Name"


# -- deploy_report routing (create vs update) via a fake _http ------------------------------
class _FakeHttp:
    """Records calls and answers list/create/update like the Fabric REST report endpoints."""

    def __init__(self, existing_reports=None, create_status=201):
        self.calls = []
        self.existing = existing_reports or []
        self.create_status = create_status

    def __call__(self, method, url, token, body=None, extra_headers=None, timeout=120):
        self.calls.append((method, url, body))
        if method == "GET" and url.endswith("/reports"):
            return 200, {}, {"value": self.existing}
        if method == "GET" and url.endswith("/semanticModels"):
            return 200, {}, {"value": []}
        if method == "POST" and url.endswith("/reports"):
            return self.create_status, {}, {"id": "report-new"}
        if method == "POST" and url.endswith("/updateDefinition"):
            return 200, {}, None
        raise AssertionError(f"unexpected {method} {url}")


def test_deploy_report_create(monkeypatch):
    fake = _FakeHttp(existing_reports=[])
    monkeypatch.setattr(D, "_http", fake)
    parts = {"definition.pbir": json.dumps({
        "$schema": "x", "version": "4.0",
        "datasetReference": {"byConnection": {"connectionString": f"semanticmodelid={_MODEL_ID}"}}})}

    summary = D.deploy_report(parts, report_name="DemoReport", workspace=_WS_GUID, token="tok")

    assert summary["operation"] == "created"
    assert summary["item_id"] == "report-new"
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert posts and posts[0][1].endswith("/reports")
    assert posts[0][2]["displayName"] == "DemoReport"


def test_deploy_report_update_existing(monkeypatch):
    fake = _FakeHttp(existing_reports=[{"displayName": "DemoReport", "id": "rep-existing"}])
    monkeypatch.setattr(D, "_http", fake)

    summary = D.deploy_report({"definition.pbir": "{}"}, report_name="DemoReport",
                              workspace=_WS_GUID, token="tok")

    assert summary["operation"] == "updated"
    assert summary["item_id"] == "rep-existing"
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert posts and posts[0][1].endswith("/reports/rep-existing/updateDefinition")
    assert "displayName" not in (posts[0][2] or {})


# -- deploy_pbip order + byConnection payload reaching the report POST ----------------------
def test_deploy_pbip_deploys_model_then_rebound_report(tmp_path, monkeypatch):
    bundle = tmp_path / "DemoBundle"
    _write_model_folder(bundle / "DemoModel.SemanticModel")
    _write_report_folder(bundle / "DemoReport.Report")

    calls = []

    def fake_http(method, url, token, body=None, extra_headers=None, timeout=120):
        calls.append((method, url, body))
        if method == "GET" and url.endswith("/semanticModels"):
            return 200, {}, {"value": []}
        if method == "GET" and url.endswith("/reports"):
            return 200, {}, {"value": []}
        if method == "POST" and url.endswith("/semanticModels"):
            return 201, {}, {"id": _MODEL_ID}
        if method == "POST" and url.endswith("/reports"):
            return 201, {}, {"id": "report-new"}
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(D, "_http", fake_http)
    model_dir, report_dir = D.discover_pbip(str(bundle))

    result = D.deploy_pbip(model_dir, report_dir, workspace=_WS_GUID, token="tok")

    assert result["model"]["item_id"] == _MODEL_ID
    assert result["report"]["item_id"] == "report-new"

    # model POST precedes report POST (deploy order)
    post_urls = [u for (m, u, _b) in calls if m == "POST"]
    assert post_urls[0].endswith("/semanticModels")
    assert post_urls[-1].endswith("/reports")

    # the report POST carried a byConnection definition.pbir wired to the deployed model id
    report_post = next(b for (m, u, b) in calls if m == "POST" and u.endswith("/reports"))
    pbir_part = next(p for p in report_post["definition"]["parts"]
                     if p["path"] == "definition.pbir")
    pbir = json.loads(base64.b64decode(pbir_part["payload"]).decode("utf-8"))
    assert pbir["datasetReference"] == {
        "byConnection": {"connectionString": f"semanticmodelid={_MODEL_ID}"}}


def test_deploy_pbip_skips_report_when_no_pbir(tmp_path, monkeypatch):
    bundle = tmp_path / "DemoBundle"
    _write_model_folder(bundle / "DemoModel.SemanticModel")
    # a report folder with NO definition.pbir -> rebind fails closed -> report skipped
    rep = bundle / "DemoReport.Report"
    rep.mkdir(parents=True)
    (rep / ".platform").write_text('{"metadata": {"type": "Report"}}', encoding="utf-8")

    def fake_http(method, url, token, body=None, extra_headers=None, timeout=120):
        if method == "GET" and url.endswith("/semanticModels"):
            return 200, {}, {"value": []}
        if method == "POST" and url.endswith("/semanticModels"):
            return 201, {}, {"id": _MODEL_ID}
        raise AssertionError(f"unexpected {method} {url}")  # no /reports call expected

    monkeypatch.setattr(D, "_http", fake_http)
    model_dir, report_dir = D.discover_pbip(str(bundle))

    result = D.deploy_pbip(model_dir, report_dir, workspace=_WS_GUID, token="tok")

    assert result["model"]["item_id"] == _MODEL_ID
    assert result["report"]["status"] == "skipped"
    assert "definition.pbir" in result["report"]["reason"]
