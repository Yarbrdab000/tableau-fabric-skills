"""Tests for reference-image acquisition (the Tableau half of the visual check).

Two properties carry the design and most of these tests exist to pin them:

1. **The join key is the exact Tableau dashboard name, never a filename.** Three different sanitizers
   are in play across this repo and the vendored extractor, and none can be derived from another, so
   any consumer that re-derives a filename will silently mismatch.
2. **This NEVER blocks a migration.** Every failure mode -- missing source, unreadable container,
   absent dependency, unknown mode -- must still exit 0 and still write a manifest, so "no images" is
   an explicit queryable state rather than a stalled run.
"""
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, HERE)

import reference_images as R  # noqa: E402


# --- fixtures -------------------------------------------------------------------------------------
def _twb(dashboards=("Dashboard 1",)):
    blocks = "".join(
        "<dashboard name='%s'><zones><zone name='z'/></zones></dashboard>" % d for d in dashboards)
    return "<?xml version='1.0'?><workbook><dashboards>%s</dashboards></workbook>" % blocks


def _twbx(tmp_path, dashboards=("Dashboard 1",), name="Book.twbx"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Book.twb", _twb(dashboards))
    return str(p)


_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
        + (1500).to_bytes(4, "big") + (800).to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00" + b"\x00" * 40)


def _pptx(tmp_path, mapping, quote="'", name="export.pptx"):
    """A minimal Tableau-shaped PowerPoint: slide i names its sheet via <p:cNvPr descr=...>."""
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        for i, (sheet, img) in enumerate(mapping.items(), start=2):
            z.writestr("ppt/slides/slide%d.xml" % i,
                       "<p:sld><p:pic><p:nvPicPr>"
                       "<p:cNvPr descr={q}{s}{q} id='2' name='slide{i}'/>"
                       "</p:nvPicPr></p:pic></p:sld>".format(q=quote, s=sheet, i=i))
            z.writestr("ppt/slides/_rels/slide%d.xml.rels" % i,
                       "<Relationships><Relationship Id='rId7' "
                       "Target={q}../media/{img}{q} Type='.../image'/></Relationships>".format(
                           q=quote, img=img))
            z.writestr("ppt/media/%s" % img, _PNG)
        # a title slide: no picture, no name -- must be ignored, not mis-mapped
        z.writestr("ppt/slides/slide1.xml", "<p:sld><p:sp><a:t>Title</a:t></p:sp></p:sld>")
        z.writestr("ppt/slides/_rels/slide1.xml.rels", "<Relationships/>")
    return str(p)


# --- (a) reading the workbook ---------------------------------------------------------------------
def test_declared_dashboards_from_twb_and_twbx(tmp_path):
    assert R.declared_dashboards(_twb(("Intake", "Clients"))) == ["Intake", "Clients"]
    assert R.declared_dashboards(R.read_workbook_xml(_twbx(tmp_path, ("A", "B")))) == ["A", "B"]


def test_declared_dashboards_dedupes_and_preserves_order():
    assert R.declared_dashboards(_twb(("B", "A", "B"))) == ["B", "A"]


def test_reading_a_missing_or_broken_workbook_never_raises(tmp_path):
    assert R.read_workbook_xml(str(tmp_path / "nope.twbx")) == ""
    assert R.declared_dashboards("") == []
    assert R.declared_dashboards(None) == []


# --- (b) naming: the stem is a convenience, the NAME is the key ------------------------------------
def test_safe_stem_matches_the_vendored_extractor_convention():
    assert R.safe_stem("Service Delivery") == "Service_Delivery"
    assert R.safe_stem("Sales / Ops") == "Sales_Ops"
    assert R.safe_stem("Q1 (2026)") == "Q1_2026"


def test_safe_stem_is_unicode_aware_so_non_ascii_names_survive():
    assert R.safe_stem("売上ダッシュボード") == "売上ダッシュボード"
    assert R.safe_stem("Café Report") == "Café_Report"


def test_safe_stem_never_returns_empty():
    assert R.safe_stem("///") == "dashboard"
    assert R.safe_stem("") == "dashboard"


# --- (c) PowerPoint: the descr -> media mapping ----------------------------------------------------
def test_pptx_maps_each_slide_to_its_tableau_sheet_name(tmp_path):
    p = _pptx(tmp_path, {"Dashboard 1": "image1.png", "Service Delivery": "image2.png"})
    assert R.pptx_sheet_images(p) == {"Dashboard 1": "ppt/media/image1.png",
                                      "Service Delivery": "ppt/media/image2.png"}


def test_pptx_relationship_double_quotes_are_also_matched(tmp_path):
    """Real Tableau exports use SINGLE quotes; assuming double yields a silently EMPTY mapping."""
    single = _pptx(tmp_path, {"D": "image1.png"}, quote="'", name="s.pptx")
    double = _pptx(tmp_path, {"D": "image1.png"}, quote='"', name="d.pptx")
    assert R.pptx_sheet_images(single) == R.pptx_sheet_images(double) != {}


def test_pptx_title_slide_without_a_picture_is_ignored(tmp_path):
    p = _pptx(tmp_path, {"Dashboard 1": "image1.png"})
    assert list(R.pptx_sheet_images(p)) == ["Dashboard 1"]


def test_pptx_route_writes_pngs_keyed_by_the_exact_dashboard_name(tmp_path):
    wb = _twbx(tmp_path, ("Dashboard 1",))
    p = _pptx(tmp_path, {"Dashboard 1": "image1.png", "Some Worksheet": "image2.png"})
    out = tmp_path / "ref"
    m = R.acquire("export", workbook=wb, outdir=str(out), source=p)
    assert m["summary"] == {"declared": 1, "acquired": 1, "missing": 0}
    rec = m["images"][0]
    assert rec["dashboard"] == "Dashboard 1"          # EXACT name, the join key
    assert rec["confidence"] == R.CONF_NAMED          # the container named it: no inference
    assert (out / rec["png"]).is_file()
    assert (rec["width"], rec["height"]) == (1500, 800)


def test_pptx_worksheets_are_not_acquired_only_declared_dashboards(tmp_path):
    wb = _twbx(tmp_path, ("Dashboard 1",))
    p = _pptx(tmp_path, {"Dashboard 1": "image1.png", "Some Worksheet": "image2.png"})
    m = R.acquire("export", workbook=wb, outdir=str(tmp_path / "o"), source=p)
    assert [r["dashboard"] for r in m["images"]] == ["Dashboard 1"]


def test_an_unreadable_pptx_is_a_warning_not_an_exception(tmp_path):
    bad = tmp_path / "bad.pptx"
    bad.write_bytes(b"not a zip")
    assert R.pptx_sheet_images(str(bad)) == {}
    m = R.acquire("export", workbook=_twbx(tmp_path), outdir=str(tmp_path / "o"), source=str(bad))
    assert m["images"] == [] and m["warnings"]


# --- (d) the manifest contract --------------------------------------------------------------------
def test_missing_is_derived_so_it_can_never_disagree_with_images():
    m = R.build_manifest("export", "wb.twbx", ["A", "B", "C"],
                         [{"dashboard": "B", "png": "B.png"}])
    assert m["missing"] == ["A", "C"]
    assert m["summary"] == {"declared": 3, "acquired": 1, "missing": 2}


def test_manifest_carries_the_schema_and_mode():
    m = R.build_manifest("skip", None, [], [])
    assert m["schema"] == R.SCHEMA_VERSION and m["mode"] == "skip"


def test_manifest_is_written_and_reloadable(tmp_path):
    m = R.build_manifest("export", "wb", ["A"], [])
    p = R.write_manifest(str(tmp_path), m)
    assert p and os.path.isfile(p)
    assert json.load(open(p, encoding="utf-8"))["declared_dashboards"] == ["A"]


def test_non_ascii_dashboard_names_round_trip_through_the_manifest(tmp_path):
    m = R.build_manifest("export", "wb", ["売上"], [{"dashboard": "売上", "png": "売上.png"}])
    p = R.write_manifest(str(tmp_path), m)
    assert json.load(open(p, encoding="utf-8"))["images"][0]["dashboard"] == "売上"


# --- (e) THE INVARIANT: never blocks --------------------------------------------------------------
def test_skip_still_writes_a_manifest_so_no_images_is_explicit(tmp_path):
    out = tmp_path / "ref"
    m = R.acquire("skip", workbook=_twbx(tmp_path), outdir=str(out))
    assert m["mode"] == "skip" and m["images"] == [] and m["reason"]
    assert R.write_manifest(str(out), m)
    assert (out / "manifest.json").is_file()


def test_every_failure_mode_still_returns_a_manifest_and_never_raises(tmp_path):
    wb = _twbx(tmp_path)
    cases = [
        dict(mode="export", source=str(tmp_path / "nope.pdf")),      # missing source
        dict(mode="export", source=str(tmp_path)),                   # empty folder
        dict(mode="export", source=str(tmp_path / "x.bin")),         # unsupported type
        dict(mode="drop", source=str(tmp_path / "absent")),          # nothing dropped
        dict(mode="capture"),                                        # route not installed yet
        dict(mode="nonsense"),                                       # unknown mode
    ]
    for kw in cases:
        m = R.acquire(workbook=wb, outdir=str(tmp_path / "o"), **kw)
        assert m["schema"] == R.SCHEMA_VERSION, kw
        assert m["images"] == [], kw
        assert m["missing"] == ["Dashboard 1"], kw
        assert m["warnings"] or m.get("reason"), kw


def test_cli_always_exits_zero_even_when_nothing_was_acquired(tmp_path):
    wb = _twbx(tmp_path)
    for argv in (["--mode", "skip", "--workbook", wb, "--out", str(tmp_path / "a")],
                 ["--mode", "export", "--workbook", wb, "--source", str(tmp_path / "nope.pdf"),
                  "--out", str(tmp_path / "b")],
                 ["--mode", "capture", "--workbook", wb, "--out", str(tmp_path / "c")]):
        assert R.main(argv) == 0, argv


def test_a_workbook_with_no_dashboards_is_not_an_error(tmp_path):
    wb = _twbx(tmp_path, ())
    m = R.acquire("export", workbook=wb, outdir=str(tmp_path / "o"), source=str(tmp_path))
    assert m["summary"]["declared"] == 0
    assert m["warnings"]


def test_a_pdf_route_with_a_broken_extractor_degrades_to_a_warning(tmp_path):
    def boom(_cmd, _timeout):
        raise OSError("python is on fire")

    imgs, warns, _ = R._extract_from_pdf(str(tmp_path / "x.pdf"), str(tmp_path / "wb.twbx"),
                                         str(tmp_path), runner=boom)
    assert imgs == [] and warns and "fire" in warns[0]


# --- (f) partial acquisition is a valid result ----------------------------------------------------
def test_a_partial_capture_is_recorded_not_failed(tmp_path):
    wb = _twbx(tmp_path, ("Dashboard 1", "Dashboard 2", "Dashboard 3"))
    p = _pptx(tmp_path, {"Dashboard 1": "image1.png", "Dashboard 3": "image2.png"})
    m = R.acquire("export", workbook=wb, outdir=str(tmp_path / "o"), source=p)
    assert m["summary"] == {"declared": 3, "acquired": 2, "missing": 1}
    assert m["missing"] == ["Dashboard 2"]
    assert "MISSING" in R.format_manifest(m)


# --- (g) PDF confidence labels are carried through, not invented ----------------------------------
def _fake_pdf_run(outdir, basis, score=0.79, runner=0.25):
    def run(_cmd, _timeout):
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "Dashboard_1.png"), "wb") as fh:
            fh.write(_PNG)
        with open(os.path.join(outdir, "dashboards.json"), "w", encoding="utf-8") as fh:
            json.dump({"dashboards": [{"dashboard": "Dashboard 1", "png": "Dashboard_1.png",
                                       "page": 1, "match_basis": basis,
                                       "content_score": score, "runner_up_score": runner}],
                       "warnings": []}, fh)
        return 0, "", ""
    return run


def test_pdf_match_basis_maps_to_confidence(tmp_path):
    for basis, expected in (("content", R.CONF_CONTENT),
                            ("content (WEAK margin - verify)", R.CONF_CONTENT_WEAK),
                            ("tab-order (no text signal)", R.CONF_TAB_ORDER)):
        out = tmp_path / re.sub(r"\W+", "_", basis)
        imgs, _w, _d = R._extract_from_pdf("x.pdf", "wb.twbx", str(out),
                                           runner=_fake_pdf_run(str(out), basis))
        assert imgs and imgs[0]["confidence"] == expected, basis
        assert imgs[0]["dashboard"] == "Dashboard 1"


def test_weak_matches_are_surfaced_for_human_verification(tmp_path):
    out = tmp_path / "w"
    imgs, _w, _d = R._extract_from_pdf(
        "x.pdf", "wb.twbx", str(out),
        runner=_fake_pdf_run(str(out), "content (WEAK margin - verify)"))
    m = R.build_manifest("export", "wb", ["Dashboard 1"], imgs)
    assert "VERIFY" in R.format_manifest(m)
