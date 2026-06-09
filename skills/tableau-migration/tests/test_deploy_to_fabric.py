"""Offline tests for the self-contained Fabric deploy script (deploy_to_fabric.py).

These cover the PURE request-builders + folder I/O + LRO header parsing -- everything except the
thin ``_http`` network layer. No Fabric tenant or network is touched.
"""
import base64
import os

import pytest

import deploy_to_fabric as D


# -- read_model_folder ----------------------------------------------------------------------
def test_read_model_folder_roundtrips_parts(tmp_path):
    root = tmp_path / "Demo.SemanticModel"
    (root / "definition" / "tables").mkdir(parents=True)
    (root / ".platform").write_text("platform", encoding="utf-8")
    (root / "definition.pbism").write_text("{}", encoding="utf-8")
    (root / "definition" / "model.tmdl").write_text("model Demo", encoding="utf-8")
    (root / "definition" / "tables" / "Orders.tmdl").write_text("table Orders", encoding="utf-8")
    (root / "ignore.txt").write_text("nope", encoding="utf-8")  # non-model file is skipped

    parts = D.read_model_folder(str(root))

    assert set(parts) == {
        ".platform", "definition.pbism",
        "definition/model.tmdl", "definition/tables/Orders.tmdl",
    }
    # keys are POSIX-style relative paths regardless of OS separator
    assert all("\\" not in k for k in parts)
    assert parts["definition/tables/Orders.tmdl"] == "table Orders"


def test_read_model_folder_empty_raises(tmp_path):
    (tmp_path / "Empty.SemanticModel").mkdir()
    with pytest.raises(FileNotFoundError):
        D.read_model_folder(str(tmp_path / "Empty.SemanticModel"))


# -- build_create_payload / build_update_definition_payload ---------------------------------
def test_build_create_payload_has_displayname_and_base64_parts():
    parts = {"definition/model.tmdl": "model Demo"}
    body = D.build_create_payload("Demo", parts, description="hi")

    assert body["displayName"] == "Demo"
    assert body["description"] == "hi"
    one = body["definition"]["parts"][0]
    assert one["path"] == "definition/model.tmdl"
    assert one["payloadType"] == "InlineBase64"
    assert base64.b64decode(one["payload"]).decode("utf-8") == "model Demo"


def test_build_update_definition_payload_has_no_displayname():
    body = D.build_update_definition_payload({"definition/model.tmdl": "x"})
    assert "displayName" not in body
    assert body["definition"]["parts"][0]["path"] == "definition/model.tmdl"


# -- find_item_id ---------------------------------------------------------------------------
def test_find_item_id_case_insensitive_and_missing():
    items = [{"displayName": "Other", "id": "1"}, {"displayName": "My Model", "id": "abc"}]
    assert D.find_item_id(items, "my model") == "abc"
    assert D.find_item_id(items, "nope") is None
    assert D.find_item_id([], "x") is None


# -- parse_operation_headers ----------------------------------------------------------------
def test_parse_operation_headers_case_insensitive_with_retry():
    loc, retry = D.parse_operation_headers(
        {"Operation-Location": "https://op/123", "Retry-After": "7"})
    assert loc == "https://op/123" and retry == 7


def test_parse_operation_headers_falls_back_to_location_and_handles_bad_retry():
    loc, retry = D.parse_operation_headers({"location": "https://op/9", "retry-after": "soon"})
    assert loc == "https://op/9" and retry is None
    assert D.parse_operation_headers({}) == (None, None)


# -- _looks_like_guid -----------------------------------------------------------------------
def test_looks_like_guid():
    assert D._looks_like_guid("11111111-2222-3333-4444-555555555555")
    assert not D._looks_like_guid("My Workspace")
    assert not D._looks_like_guid("")


# -- acquire_token --------------------------------------------------------------------------
def test_acquire_token_prefers_explicit_then_env(monkeypatch):
    assert D.acquire_token("res", explicit="tok", env_var="X") == "tok"
    monkeypatch.setenv("MY_TOKEN", "from-env")
    assert D.acquire_token("res", explicit=None, env_var="MY_TOKEN") == "from-env"


def test_acquire_token_errors_without_source(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        D.acquire_token("res", explicit=None, env_var="MISSING_TOKEN", use_az=False)
