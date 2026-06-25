"""QUARANTINED tests for the optional reference-image acquisition module (``fidelity_reference``).

Offline-safe: every network path is exercised by monkeypatching the reused ``fetch_tds`` HTTP
helpers, so no real Tableau server, PAT, or image is ever touched. Lives in ``tests_oracle/`` so the
engine's ``pytest tests`` gate never collects it.
"""
import os

import pytest

import fidelity_reference as fr
import fetch_tds as tds


# ---- local-exclusive path (no network) -----------------------------------------------------------
def test_safe_filename_normalizes():
    assert fr.safe_filename("Sales by Sub-Category!") == "sales_by_sub_category"
    assert fr.safe_filename("  Sheet 1  ") == "sheet_1"
    assert fr.safe_filename("***") == "sheet"  # degenerate input still yields a usable base


def test_reference_image_path_is_png_under_dir(tmp_path):
    p = fr.reference_image_path(str(tmp_path), "Region Map")
    assert p == os.path.join(os.path.abspath(str(tmp_path)), "region_map.png")


def test_resolve_local_references_found_and_missing(tmp_path):
    (tmp_path / "sheet_1.png").write_bytes(b"x")
    out = fr.resolve_local_references(["Sheet 1", "Sheet 2"], str(tmp_path))
    assert "Sheet 1" in out["found"]
    assert out["missing"] == ["Sheet 2"]
    # The instruction names the exact file the user must drop.
    assert "sheet_2.png" in out["instructions"]
    assert "Sheet 2" in out["instructions"]


def test_build_acquisition_plan_all_present(tmp_path):
    (tmp_path / "sheet_1.png").write_bytes(b"x")
    plan = fr.build_acquisition_plan(["Sheet 1"], str(tmp_path))
    assert plan["missing"] == []
    assert "All 1 reference image(s) present" in plan["instructions"]


# ---- URL construction --------------------------------------------------------------------------
def test_views_url_scoped_and_unscoped():
    site, wb = "SITE", "WB1"
    assert fr.views_url("srv", site, "3.24").endswith("/sites/SITE/views?pageSize=1000")
    assert fr.views_url("srv", site, "3.24", workbook_id=wb).endswith("/sites/SITE/workbooks/WB1/views")


def test_view_image_url_includes_resolution():
    url = fr.view_image_url("srv", "SITE", "V9", "3.24", resolution="high")
    assert url.endswith("/sites/SITE/views/V9/image?resolution=high")


# ---- live path (monkeypatched fetch_tds) -------------------------------------------------------
def test_list_views_parses(monkeypatch):
    payload = {"views": {"view": [
        {"id": "1", "name": "Sheet 1", "contentUrl": "wb/sheets/Sheet1"},
        {"id": "2", "name": "Sheet 2"},
        {"bogus": "no id -> dropped"},
    ]}}
    monkeypatch.setattr(tds, "_http_json", lambda *a, **k: payload)
    views = fr.list_views("srv", "SITE", "tok")
    assert [v["id"] for v in views] == ["1", "2"]
    assert views[0]["name"] == "Sheet 1"


def test_fetch_view_image_sends_png_accept_and_returns_bytes(monkeypatch):
    captured = {}

    def fake_http(method, url, headers=None, body=None, timeout=120):
        captured["url"] = url
        captured["headers"] = headers
        return 200, {}, b"\x89PNG-bytes"

    monkeypatch.setattr(tds, "_http", fake_http)
    out = fr.fetch_view_image("srv", "SITE", "tok", "V1", "3.24", resolution="high")
    assert out == b"\x89PNG-bytes"
    assert "/views/V1/image?resolution=high" in captured["url"]
    # Tableau Online 406s on a bare ``Accept: image/png`` (verified live); must advertise fallback.
    assert captured["headers"]["Accept"] == "image/png, */*"
    assert "*/*" in captured["headers"]["Accept"]
    assert captured["headers"]["X-Tableau-Auth"] == "tok"


def test_fetch_view_image_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(tds, "_http", lambda *a, **k: (403, {}, b"denied"))
    with pytest.raises(RuntimeError):
        fr.fetch_view_image("srv", "SITE", "tok", "V1")


def test_match_views_case_insensitive():
    views = [{"id": "1", "name": "Sheet 1"}, {"id": "2", "name": "Region MAP"}]
    matched = fr.match_views(views, ["sheet 1", "Region Map", "Missing"])
    assert matched["sheet 1"]["id"] == "1"
    assert matched["Region Map"]["id"] == "2"
    assert matched["Missing"] is None


def test_acquire_reference_images_writes_and_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(tds, "sign_in", lambda *a, **k: ("tok", "SITE"))
    monkeypatch.setattr(tds, "sign_out", lambda *a, **k: None)
    monkeypatch.setattr(tds, "_http_json", lambda *a, **k: {
        "views": {"view": [{"id": "10", "name": "Sheet 1"}]}})
    monkeypatch.setattr(tds, "_http", lambda *a, **k: (200, {}, b"PNGDATA"))

    manifest = fr.acquire_reference_images(
        "srv", "", str(tmp_path), worksheet_names=["Sheet 1", "Sheet 2"],
        pat_name="n", pat_secret="s")
    assert manifest["available"] is True
    assert manifest["saved"] == ["Sheet 1"]
    assert manifest["not_found"] == ["Sheet 2"]
    saved_path = fr.reference_image_path(str(tmp_path), "Sheet 1")
    assert os.path.isfile(saved_path)
    assert open(saved_path, "rb").read() == b"PNGDATA"


def test_acquire_reference_images_unavailable_without_tds(monkeypatch, tmp_path):
    monkeypatch.setattr(fr, "_tds", None)
    out = fr.acquire_reference_images("srv", "", str(tmp_path))
    assert out["available"] is False and "reason" in out


def test_cli_list_does_not_require_out(monkeypatch, capsys):
    # --list enumerates views and exits; it must NOT demand --out (a real usability fix found live).
    monkeypatch.setattr(tds, "sign_in", lambda *a, **k: ("tok", "SITE"))
    monkeypatch.setattr(tds, "sign_out", lambda *a, **k: None)
    monkeypatch.setattr(fr, "list_views",
                        lambda *a, **k: [{"id": "V1", "name": "Sheet 1", "contentUrl": "c"}])
    rc = fr.main(["--list", "--server", "srv", "--site", "S", "--pat-name", "N"])
    assert rc == 0
    assert "V1\tSheet 1" in capsys.readouterr().out


def test_cli_acquisition_requires_out(monkeypatch):
    # The acquisition path (no --list/--check-local) still needs --out to know where to write.
    monkeypatch.setattr(tds, "sign_in", lambda *a, **k: ("tok", "SITE"))
    with pytest.raises(SystemExit):
        fr.main(["--server", "srv", "--site", "S", "--pat-name", "N"])
