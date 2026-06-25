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


# ---- local-exclusive path #3: consume already-exported PNGs (no Tableau on this box) ------------
import zipfile  # noqa: E402  (stdlib; used only by the .twbx tests below)


def test_norm_match_key_collapses_spacing_and_punctuation():
    assert fr.norm_match_key("Sheet 1") == "sheet1"
    assert fr.norm_match_key("sheet_1") == "sheet1"
    assert fr.norm_match_key("Sheet1.png") == "sheet1png"  # caller strips ext before normalizing
    assert fr.norm_match_key("Region / Map!") == "regionmap"


def test_list_local_pngs_recursive_and_guarded(tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "b.png").write_bytes(b"x")
    (tmp_path / "c.txt").write_bytes(b"x")  # non-png ignored
    pngs = fr.list_local_pngs(str(tmp_path))
    assert [os.path.basename(p) for p in pngs] == ["a.png", "b.png"]
    # Missing folder degrades to [] (never raises).
    assert fr.list_local_pngs(str(tmp_path / "does-not-exist")) == []


def test_load_exported_references_folder_match(tmp_path):
    # Tableau Desktop's Worksheet>Export>Image defaults the file name to the view name.
    (tmp_path / "Sheet 1.png").write_bytes(b"x")
    (tmp_path / "region_map.png").write_bytes(b"x")
    (tmp_path / "Leftover.png").write_bytes(b"x")
    out = fr.load_exported_references(str(tmp_path), ["Sheet 1", "Region Map", "Missing"])
    assert out["available"] is True
    assert os.path.basename(out["found"]["Sheet 1"]) == "Sheet 1.png"
    assert os.path.basename(out["found"]["Region Map"]) == "region_map.png"
    assert out["missing"] == ["Missing"]
    assert [os.path.basename(p) for p in out["unmatched"]] == ["Leftover.png"]


def test_load_exported_references_single_png(tmp_path):
    p = tmp_path / "Dashboard 1.png"
    p.write_bytes(b"x")
    out = fr.load_exported_references(str(p), ["Dashboard 1"])
    assert out["found"]["Dashboard 1"] == str(p.resolve()) or out["found"]["Dashboard 1"] == os.path.abspath(str(p))


def test_load_exported_references_no_names_offers_by_stem(tmp_path):
    (tmp_path / "Sheet 1.png").write_bytes(b"x")
    out = fr.load_exported_references(str(tmp_path))
    assert out["available"] is True
    assert "sheet1" in out["by_stem"]
    assert out["found"] == {} and out["missing"] == []


def test_load_exported_references_empty_is_unavailable(tmp_path):
    out = fr.load_exported_references(str(tmp_path), ["Sheet 1"])
    assert out["available"] is False
    assert out["missing"] == ["Sheet 1"]
    assert "no PNG names matched" in out["reason"]


def _make_twbx(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_extract_twbx_images_pulls_image_objects(tmp_path):
    twbx = tmp_path / "wb.twbx"
    _make_twbx(twbx, {
        "wb.twb": b"<workbook/>",
        "Image/logo.png": b"\x89PNG-logo",
        "Image/banner.jpg": b"JFIF-banner",
        "Data/extract.hyper": b"not-an-image",
    })
    out = tmp_path / "extracted"
    rec = fr.extract_twbx_images(str(twbx), str(out))
    assert rec["available"] is True
    assert rec["images"] == ["Image/banner.jpg", "Image/logo.png"]
    assert os.path.isfile(os.path.join(str(out), "logo.png"))
    assert open(os.path.join(str(out), "logo.png"), "rb").read() == b"\x89PNG-logo"


def test_extract_twbx_images_list_only_without_out(tmp_path):
    twbx = tmp_path / "wb.twbx"
    _make_twbx(twbx, {"Image/logo.png": b"x"})
    rec = fr.extract_twbx_images(str(twbx))  # no output_dir -> report members, extract nothing
    assert rec["available"] is True
    assert rec["images"] == ["Image/logo.png"]
    assert rec["extracted"] == {}


def test_extract_twbx_images_decollides_duplicate_basenames(tmp_path):
    twbx = tmp_path / "wb.twbx"
    _make_twbx(twbx, {"Image/logo.png": b"a", "Images/logo.png": b"b"})
    out = tmp_path / "x"
    rec = fr.extract_twbx_images(str(twbx), str(out))
    names = sorted(os.path.basename(p) for p in rec["extracted"].values())
    assert names == ["logo.png", "logo_1.png"]  # second collision is renamed, not overwritten


def test_extract_twbx_images_plain_twb_is_unavailable(tmp_path):
    twb = tmp_path / "wb.twb"
    twb.write_bytes(b"<workbook/>")  # not a zip
    rec = fr.extract_twbx_images(str(twb))
    assert rec["available"] is False
    assert "not a packaged workbook" in rec["reason"]


def test_extract_twbx_images_missing_file_is_unavailable(tmp_path):
    rec = fr.extract_twbx_images(str(tmp_path / "nope.twbx"))
    assert rec["available"] is False and "file not found" in rec["reason"]


def test_extract_twbx_images_no_image_folder(tmp_path):
    twbx = tmp_path / "wb.twbx"
    _make_twbx(twbx, {"wb.twb": b"<workbook/>", "Data/x.hyper": b"y"})
    rec = fr.extract_twbx_images(str(twbx), str(tmp_path / "o"))
    assert rec["available"] is False
    assert "no Image/ assets" in rec["reason"]


def test_cli_from_twbx_extracts(tmp_path, capsys):
    twbx = tmp_path / "wb.twbx"
    _make_twbx(twbx, {"Image/logo.png": b"x"})
    rc = fr.main(["--from-twbx", str(twbx), "--out", str(tmp_path / "o")])
    assert rc == 0
    assert "extracted 1 image object(s)" in capsys.readouterr().out


def test_cli_from_export_maps_to_worksheets(tmp_path, capsys):
    (tmp_path / "Sheet 1.png").write_bytes(b"x")
    rc = fr.main(["--from-export", str(tmp_path), "--worksheets", "Sheet 1,Sheet 2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "matched 1, missing 1" in out
    assert "no PNG for worksheet: Sheet 2" in out


# ---- adversarial hardening: the advisory loaders must NEVER raise (fuzz-discovered) ------------
@pytest.mark.parametrize("bad", [123, 4.5, None, "", "   ", b"", object()])
def test_load_exported_references_bad_source_degrades(bad):
    out = fr.load_exported_references(bad, ["Sheet 1"])
    assert out["available"] is False
    assert out["missing"] == ["Sheet 1"]
    assert set(out) >= {"available", "found", "missing", "unmatched", "by_stem", "reason"}


def test_load_exported_references_non_iterable_names_is_safe(tmp_path):
    (tmp_path / "Sheet 1.png").write_bytes(b"x")
    # A non-iterable worksheet_names (an int) must not raise; it degrades to "no names".
    out = fr.load_exported_references(str(tmp_path), 123)
    assert "sheet1" in out["by_stem"]


def test_load_exported_references_blank_source_does_not_scan_cwd(tmp_path, monkeypatch):
    # Regression: abspath("") used to resolve to CWD and silently scan it. A blank source must
    # yield an empty result instead of leaking whatever PNGs happen to be in the working dir.
    (tmp_path / "stray.png").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    out = fr.load_exported_references("", None)
    assert out["available"] is False and out["by_stem"] == {}


@pytest.mark.parametrize("bad", [123, None, "", object()])
def test_list_local_pngs_bad_input_is_empty(bad):
    assert fr.list_local_pngs(bad) == []


@pytest.mark.parametrize("bad", [123, 4.5, None, "", object()])
def test_extract_twbx_images_bad_source_degrades(bad):
    rec = fr.extract_twbx_images(bad)
    assert rec["available"] is False
    assert set(rec) >= {"available", "images", "extracted", "reason"}


def test_extract_twbx_images_bad_output_dir_degrades(tmp_path):
    twbx = tmp_path / "wb.twbx"
    _make_twbx(twbx, {"Image/logo.png": b"x"})
    # An int output_dir is unusable -> coerced to None -> graceful list-only (no crash, no write).
    rec = fr.extract_twbx_images(str(twbx), 123)
    assert rec["available"] is True
    assert rec["extracted"] == {}
    assert rec["images"] == ["Image/logo.png"]


def test_extract_twbx_images_output_dir_is_a_file(tmp_path):
    twbx = tmp_path / "wb.twbx"
    _make_twbx(twbx, {"Image/logo.png": b"x"})
    clash = tmp_path / "out_is_file"
    clash.write_bytes(b"x")  # output_dir path already exists as a FILE
    rec = fr.extract_twbx_images(str(twbx), str(clash))
    assert rec["available"] is False  # makedirs over a file -> guarded, not a crash


def test_extract_twbx_images_zip_slip_is_contained(tmp_path):
    # Malicious traversal member names must extract by BASENAME only, never escaping --out.
    twbx = tmp_path / "evil.twbx"
    with zipfile.ZipFile(twbx, "w") as zf:
        zf.writestr("Image/../../../evil.png", b"pwn")
        zf.writestr("Image/..\\..\\evil2.png", b"pwn2")
        zf.writestr("Image/ok.png", b"ok")
    out = tmp_path / "safe_out"
    rec = fr.extract_twbx_images(str(twbx), str(out))
    assert rec["available"] is True
    for p in rec["extracted"].values():
        assert os.path.realpath(p).startswith(os.path.realpath(str(out)) + os.sep)
    # Nothing was written above the out dir.
    assert not (tmp_path / "evil.png").exists()
    assert not (tmp_path.parent / "evil.png").exists()


def test_extract_twbx_images_corrupt_zip_degrades(tmp_path):
    good = tmp_path / "g.twbx"
    _make_twbx(good, {"Image/a.png": b"a"})
    raw = good.read_bytes()
    corrupt = tmp_path / "c.twbx"
    corrupt.write_bytes(raw[: len(raw) // 2])  # truncated central directory
    rec = fr.extract_twbx_images(str(corrupt))
    assert rec["available"] is False
