"""Tier-2 image-oracle **harness** tests (offline; synthetic candidate records + tiny PNGs).

These pin the deterministic producer that turns Tier-1 candidate records + a Tableau-side image into
the adjudication bundle the (separate, agent-driven) vision pass consumes. No model/LLM is called and
no PBIR part is touched here -- only input assembly + the offline-first image source resolution.
"""
import base64
import os

from image_oracle import (
    BUNDLE_KIND,
    ORACLE_RULES,
    agent_prompt,
    build_oracle_bundle,
    extract_thumbnails,
    read_workbook_xml,
)

# A real, minimal 1x1 PNG (valid magic + IHDR/IDAT/IEND) -- enough to exercise decode + magic check.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_BYTES = base64.b64decode(_PNG_B64)


def _thumb_xml(*names, payload=_PNG_B64):
    thumbs = "".join(f"<thumbnail name='{n}' height='384' width='384'>{payload}</thumbnail>"
                     for n in names)
    return f"<workbook><thumbnails>{thumbs}</thumbnails></workbook>"


def _record(page="Dash", visual="v1", worksheet="Sheet 1", vtype="donutChart",
            candidates=None, confidence="medium", hack="dual-axis pie/donut",
            fields=None, position=None, page_display=None):
    return {
        "page": page,
        "page_display": page_display if page_display is not None else page,
        "visual": visual,
        "worksheet": worksheet,
        "visual_type": vtype,
        "candidates": candidates if candidates is not None else [vtype, "pieChart"],
        "confidence": confidence,
        "hack": hack,
        "fields": fields if fields is not None else {"Category": ["Orders.Region"],
                                                     "Y": ["_Measures.Sum of Sales"]},
        "position": position if position is not None else {"x": 0, "y": 0, "z": 1, "width": 400,
                                                           "height": 300, "tabOrder": 1},
    }


# -- thumbnail extraction ------------------------------------------------------

def test_extract_thumbnails_decodes_named_png():
    thumbs = extract_thumbnails(_thumb_xml("Overview", "Performance"))
    assert set(thumbs) == {"Overview", "Performance"}
    assert thumbs["Overview"][:8] == b"\x89PNG\r\n\x1a\n"
    assert thumbs["Performance"] == _PNG_BYTES


def test_extract_thumbnails_skips_invalid_and_non_png():
    # not base64, and base64 that is not a PNG -- both must be skipped, never handed downstream.
    xml = ("<workbook><thumbnails>"
           "<thumbnail name='Bad'>not base64!!!</thumbnail>"
           "<thumbnail name='NotPng'>" + base64.b64encode(b"hello world").decode() + "</thumbnail>"
           "<thumbnail name='Good'>" + _PNG_B64 + "</thumbnail>"
           "</thumbnails></workbook>")
    thumbs = extract_thumbnails(xml)
    assert set(thumbs) == {"Good"}


def test_extract_thumbnails_tolerates_line_wrapped_base64():
    # real workbooks wrap the base64 payload across lines; whitespace must be stripped, not rejected.
    wrapped = "\n".join(_PNG_B64[i:i + 24] for i in range(0, len(_PNG_B64), 24))
    xml = f"<workbook><thumbnails><thumbnail name='D'>\n{wrapped}\n</thumbnail></thumbnails></workbook>"
    thumbs = extract_thumbnails(xml)
    assert set(thumbs) == {"D"}
    assert thumbs["D"] == _PNG_BYTES


def test_extract_thumbnails_empty_when_absent_or_unparseable():
    assert extract_thumbnails("<workbook></workbook>") == {}
    assert extract_thumbnails("") == {}
    assert extract_thumbnails("<not valid xml") == {}


# -- image source resolution (offline-first) -----------------------------------

def test_bundle_matches_dashboard_thumbnail_by_page_display_not_sanitized_id(tmp_path):
    # dashboard thumbnails are keyed by the DISPLAY name; the record's sanitized `page` id
    # (hash-suffixed) must not be used for matching -- `page_display` is.
    rec = _record(page="page-Performance3690e6d7", page_display="Performance", worksheet="Sheet 1")
    bundle = build_oracle_bundle([rec], twb_xml=_thumb_xml("Performance"), images_out=str(tmp_path))
    v = bundle["visuals"][0]
    assert v["page_display"] == "Performance"
    assert v["image"]["present"] is True and v["image"]["matched_on"] == "page"


def test_bundle_resolves_thumbnail_by_page_then_worksheet_and_writes_png(tmp_path):
    out = tmp_path / "images"
    bundle = build_oracle_bundle(
        [_record(page="Performance", worksheet="Sheet 1")],
        twb_xml=_thumb_xml("Performance"),
        images_out=str(out),
    )
    img = bundle["visuals"][0]["image"]
    assert img["present"] is True
    assert img["source"] == "thumbnail"
    assert img["matched_on"] == "page"
    assert os.path.isfile(img["path"])
    with open(img["path"], "rb") as fh:
        assert fh.read(8) == b"\x89PNG\r\n\x1a\n"


def test_bundle_matches_worksheet_thumbnail_when_page_absent(tmp_path):
    bundle = build_oracle_bundle(
        [_record(page="DashNoThumb", worksheet="Sheet 1")],
        twb_xml=_thumb_xml("Sheet 1"),
        images_out=str(tmp_path),
    )
    img = bundle["visuals"][0]["image"]
    assert img["present"] is True and img["matched_on"] == "worksheet"


def test_bundle_provided_image_wins_over_thumbnail(tmp_path):
    image_dir = tmp_path / "provided"
    image_dir.mkdir()
    (image_dir / "Performance.png").write_bytes(_PNG_BYTES)
    bundle = build_oracle_bundle(
        [_record(page="Performance")],
        twb_xml=_thumb_xml("Performance"),  # a thumbnail also exists
        image_dir=str(image_dir),
        images_out=str(tmp_path / "out"),
    )
    img = bundle["visuals"][0]["image"]
    assert img["source"] == "provided"
    assert img["path"].endswith("Performance.png")


def test_bundle_marks_no_image_when_unmatched():
    bundle = build_oracle_bundle(
        [_record(page="Nowhere", worksheet="AlsoNowhere")],
        twb_xml=_thumb_xml("SomeOtherDash"),
    )
    img = bundle["visuals"][0]["image"]
    assert img == {"source": "none", "matched_on": None, "present": False, "path": None}
    assert bundle["visuals"][0]["reviewable"] is False


# -- bundle shape / additivity / priority --------------------------------------

def test_bundle_carries_readonly_fields_candidates_and_answer_template():
    rec = _record(fields={"Category": ["Orders.Region"]}, candidates=["donutChart", "pieChart"])
    bundle = build_oracle_bundle([rec])
    v = bundle["visuals"][0]
    assert bundle["kind"] == BUNDLE_KIND
    assert v["fields"] == {"Category": ["Orders.Region"]}
    assert v["candidates"] == ["donutChart", "pieChart"]
    assert v["deterministic_type"] == "donutChart"
    # the agent's answer starts as keep-the-pick (chosen_type null), keyed back to the visual.
    assert v["answer"] == {"page": "Dash", "visual": "v1", "chosen_type": None, "reason": ""}
    # rules travel WITH the request so they cannot drift from the applier's enforcement.
    assert v["candidates"][0] == v["deterministic_type"]
    assert bundle["rules"] == list(ORACLE_RULES)


def test_bundle_summary_counts_images_and_flags(tmp_path):
    recs = [
        _record(page="Performance", confidence="medium", hack=None),          # flagged + image
        _record(page="Overview", confidence="high", hack=None,
                candidates=["lineChart", "areaChart"], vtype="lineChart"),     # not flagged + image
        _record(page="Nowhere", worksheet="Nope", confidence="high", hack=None,
                candidates=["clusteredColumnChart"], vtype="clusteredColumnChart"),  # no image
    ]
    bundle = build_oracle_bundle(recs, twb_xml=_thumb_xml("Performance", "Overview"),
                                 images_out=str(tmp_path))
    s = bundle["summary"]
    assert s["visuals"] == 3
    assert s["with_image"] == 2
    assert s["without_image"] == 1
    assert s["flagged"] == 1
    assert s["thumbnails_available"] == 2


def test_priority_high_for_hack_or_medium_confidence():
    high_conf = _record(confidence="high", hack=None, vtype="lineChart",
                        candidates=["lineChart", "areaChart"])
    assert build_oracle_bundle([high_conf])["visuals"][0]["priority"] == "normal"
    assert build_oracle_bundle([_record(confidence="medium", hack=None)])["visuals"][0]["priority"] == "high"
    assert build_oracle_bundle([_record(confidence="high", hack="x")])["visuals"][0]["priority"] == "high"


def test_bundle_does_not_emit_pbir_parts():
    # the harness must never produce report parts -- it is input-assembly only.
    bundle = build_oracle_bundle([_record()])
    assert "parts" not in bundle
    assert set(bundle) == {"version", "kind", "rules", "summary", "visuals"}


# -- agent prompt --------------------------------------------------------------

def test_agent_prompt_lists_reviewable_only_with_rules_and_candidates(tmp_path):
    recs = [
        _record(page="Performance", visual="donut", worksheet="Sheet 1"),
        _record(page="Nowhere", visual="hidden", worksheet="Gone"),  # no image -> excluded
    ]
    bundle = build_oracle_bundle(recs, twb_xml=_thumb_xml("Performance"), images_out=str(tmp_path))
    prompt = agent_prompt(bundle)
    assert "Hard rules:" in prompt
    assert "candidates" in prompt
    assert "READ-ONLY" in prompt
    assert "donut" in prompt           # the reviewable visual is listed
    assert "hidden" not in prompt      # the no-image visual is not in the review list
    assert "JSON array" in prompt


def test_agent_prompt_when_nothing_reviewable():
    bundle = build_oracle_bundle([_record(page="Nowhere", worksheet="Gone")])
    prompt = agent_prompt(bundle)
    assert "No visual has an available image" in prompt


# -- workbook reader (.twb / .twbx) --------------------------------------------

def test_read_workbook_xml_twb(tmp_path):
    p = tmp_path / "wb.twb"
    p.write_text(_thumb_xml("D"), encoding="utf-8")
    assert "thumbnail" in (read_workbook_xml(str(p)) or "")


def test_read_workbook_xml_twbx(tmp_path):
    import zipfile

    p = tmp_path / "wb.twbx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("wb.twb", _thumb_xml("D"))
        zf.writestr("Data/extract.hyper", b"binarydata")
    xml = read_workbook_xml(str(p))
    assert xml is not None and "thumbnail" in xml


def test_read_workbook_xml_missing_returns_none(tmp_path):
    assert read_workbook_xml(str(tmp_path / "nope.twb")) is None
    assert read_workbook_xml(None) is None


# -- applier: deterministic re-bind --------------------------------------------

import json as _json

from image_oracle import apply_adjudications


def _visual_part(name="v1", vtype="donutChart"):
    """A minimal but faithful visual.json (type + a real query/queryState the applier must not touch)."""
    return {
        "$schema": "https://example/visual/1.0.0/schema.json",
        "name": name,
        "position": {"x": 10, "y": 20, "z": 1, "width": 400, "height": 300, "tabOrder": 0},
        "visual": {
            "visualType": vtype,
            "query": {"queryState": {
                "Category": {"projections": [{"queryRef": "Orders.Region", "field": {"x": 1}}]},
                "Y": {"projections": [{"queryRef": "_Measures.Sum of Sales", "field": {"y": 2}}]},
            }},
            "drillFilterOtherVisuals": True,
        },
    }


def _parts_and_records(page="page-Dash", visual="v1", vtype="donutChart",
                       candidates=("donutChart", "pieChart")):
    path = f"definition/pages/{page}/visuals/{visual}/visual.json"
    parts = {
        "definition.pbir": "{}",
        path: _json.dumps(_visual_part(visual, vtype), indent=2),
    }
    records = [{
        "page": page, "visual": visual, "worksheet": "Sheet 1", "visual_type": vtype,
        "candidates": list(candidates), "confidence": "medium", "hack": "dual-axis pie/donut",
        "fields": {"Category": ["Orders.Region"], "Y": ["_Measures.Sum of Sales"]},
        "position": {"x": 10, "y": 20, "z": 1, "width": 400, "height": 300, "tabOrder": 0},
    }]
    return parts, records, path


def test_applier_switches_type_to_listed_candidate():
    parts, records, path = _parts_and_records()
    new_parts, report = apply_adjudications(
        parts, records,
        [{"page": "page-Dash", "visual": "v1", "chosen_type": "pieChart", "reason": "renders as pie"}],
    )
    vj = _json.loads(new_parts[path])
    assert vj["visual"]["visualType"] == "pieChart"
    assert len(report["applied"]) == 1
    assert report["applied"][0]["from_type"] == "donutChart"
    assert report["applied"][0]["to_type"] == "pieChart"
    assert report["rejected"] == [] and report["kept"] == []


def test_applier_rejects_non_candidate_type_and_leaves_part_unchanged():
    parts, records, path = _parts_and_records()
    original = parts[path]
    new_parts, report = apply_adjudications(
        parts, records,
        [{"page": "page-Dash", "visual": "v1", "chosen_type": "treemap"}],
    )
    assert new_parts[path] == original  # untouched
    assert len(report["rejected"]) == 1
    assert "not in candidates" in report["rejected"][0]["rejected_because"]


def test_applier_keeps_when_chosen_null_or_same_type():
    parts, records, path = _parts_and_records()
    original = parts[path]
    _, report = apply_adjudications(
        parts, records,
        [
            {"page": "page-Dash", "visual": "v1", "chosen_type": None},
            {"page": "page-Dash", "visual": "v1", "chosen_type": "donutChart"},
        ],
    )
    assert len(report["kept"]) == 2
    assert report["applied"] == [] and report["rejected"] == []


def test_applier_never_mutates_fields_or_query():
    parts, records, path = _parts_and_records()
    before = _json.loads(parts[path])["visual"]["query"]
    new_parts, _ = apply_adjudications(
        parts, records, [{"page": "page-Dash", "visual": "v1", "chosen_type": "pieChart"}],
    )
    after = _json.loads(new_parts[path])["visual"]["query"]
    assert after == before  # the query/queryState/fields are byte-identical after a type switch


def test_applier_adopts_valid_position_override_only():
    parts, records, path = _parts_and_records()
    new_parts, report = apply_adjudications(
        parts, records,
        [{"page": "page-Dash", "visual": "v1", "chosen_type": "pieChart",
          "position": {"x": 99, "y": 88, "bogus": "ignore"}}],
    )
    pos = _json.loads(new_parts[path])["position"]
    assert pos["x"] == 99 and pos["y"] == 88
    assert pos["width"] == 400 and pos["height"] == 300  # unchanged keys preserved
    assert "bogus" not in pos
    assert report["applied"][0]["position_changed"] is True


def test_applier_ignores_position_when_disallowed():
    parts, records, path = _parts_and_records()
    new_parts, report = apply_adjudications(
        parts, records,
        [{"page": "page-Dash", "visual": "v1", "chosen_type": "pieChart",
          "position": {"x": 99}}],
        allow_position=False,
    )
    pos = _json.loads(new_parts[path])["position"]
    assert pos["x"] == 10  # original
    assert report["applied"][0]["position_changed"] is False


def test_applier_rejects_unknown_visual():
    parts, records, _ = _parts_and_records()
    _, report = apply_adjudications(
        parts, records,
        [{"page": "page-Dash", "visual": "ghost", "chosen_type": "pieChart"}],
    )
    assert len(report["rejected"]) == 1
    assert "no candidate record" in report["rejected"][0]["rejected_because"]


def test_applier_rejects_when_part_missing_but_record_present():
    parts, records, path = _parts_and_records()
    del parts[path]  # record says this visual exists, but the emitted part is gone
    _, report = apply_adjudications(
        parts, records,
        [{"page": "page-Dash", "visual": "v1", "chosen_type": "pieChart"}],
    )
    assert "visual part not found" in report["rejected"][0]["rejected_because"]


def test_applier_does_not_mutate_input_parts():
    parts, records, path = _parts_and_records()
    snapshot = dict(parts)
    snapshot_text = parts[path]
    apply_adjudications(parts, records,
                        [{"page": "page-Dash", "visual": "v1", "chosen_type": "pieChart"}])
    assert parts[path] == snapshot_text  # input untouched
    assert parts == snapshot


# == additional coverage =======================================================
# Edge cases for the harness (thumbnail extraction, image resolution, bundle shape, prompt) and the
# applier, plus an env-gated walk of a real corpus that runs the full chain over every workbook.

import json
import os

import pytest

from image_oracle import _safe_filename, build_oracle_bundle, write_bundle


# -- extract_thumbnails edge cases ---------------------------------------------

def test_extract_thumbnails_handles_namespaced_xml():
    # a real workbook root often carries an xmlns; the local-name strip must still find <thumbnail>.
    xml = (f"<workbook xmlns='http://www.tableausoftware.com/xml/user'>"
           f"<thumbnails><thumbnail name='D'>{_PNG_B64}</thumbnail></thumbnails></workbook>")
    assert set(extract_thumbnails(xml)) == {"D"}


def test_extract_thumbnails_skips_unnamed_thumbnail():
    xml = (f"<workbook><thumbnails>"
           f"<thumbnail>{_PNG_B64}</thumbnail>"            # no name -> skipped
           f"<thumbnail name='Keep'>{_PNG_B64}</thumbnail>"
           f"</thumbnails></workbook>")
    assert set(extract_thumbnails(xml)) == {"Keep"}


def test_extract_thumbnails_skips_empty_text():
    xml = ("<workbook><thumbnails>"
           "<thumbnail name='Blank'>   </thumbnail>"
           f"<thumbnail name='Real'>{_PNG_B64}</thumbnail>"
           "</thumbnails></workbook>")
    assert set(extract_thumbnails(xml)) == {"Real"}


def test_extract_thumbnails_duplicate_name_keeps_one_valid_png():
    xml = (f"<workbook><thumbnails>"
           f"<thumbnail name='D'>{_PNG_B64}</thumbnail>"
           f"<thumbnail name='D'>{_PNG_B64}</thumbnail>"
           f"</thumbnails></workbook>")
    thumbs = extract_thumbnails(xml)
    assert list(thumbs) == ["D"]
    assert thumbs["D"][:8] == b"\x89PNG\r\n\x1a\n"


def test_safe_filename_neutralizes_path_separators_and_colons():
    assert _safe_filename("Sales: by City") == "Sales_by_City"
    assert _safe_filename("a/b\\c") == "a_b_c"
    assert _safe_filename("   ") == "image"        # empty -> stable default
    assert "/" not in _safe_filename("x/y") and "\\" not in _safe_filename("x\\y")


# -- build_oracle_bundle edge cases --------------------------------------------

def test_bundle_empty_records_is_empty_bundle():
    bundle = build_oracle_bundle([])
    assert bundle["visuals"] == []
    assert bundle["summary"] == {"visuals": 0, "with_image": 0, "without_image": 0,
                                 "flagged": 0, "thumbnails_available": 0}


def test_bundle_no_image_source_marks_all_absent():
    bundle = build_oracle_bundle([_record(), _record(visual="v2")])
    assert all(v["reviewable"] is False for v in bundle["visuals"])
    assert bundle["summary"]["with_image"] == 0
    assert bundle["summary"]["without_image"] == 2


def test_bundle_is_json_serialisable_roundtrip(tmp_path):
    bundle = build_oracle_bundle([_record()], twb_xml=_thumb_xml("Dash"), images_out=str(tmp_path))
    assert json.loads(json.dumps(bundle)) == bundle


def test_bundle_preserves_candidate_order_chosen_first():
    rec = _record(vtype="ribbonChart",
                  candidates=["ribbonChart", "clusteredColumnChart", "lineChart"])
    v = build_oracle_bundle([rec])["visuals"][0]
    assert v["candidates"] == ["ribbonChart", "clusteredColumnChart", "lineChart"]
    assert v["deterministic_type"] == v["candidates"][0]


def test_bundle_thumbnail_without_images_out_is_in_memory():
    # a matched thumbnail with no output dir is still 'present' but has no on-disk path.
    bundle = build_oracle_bundle([_record(page="Dash", page_display="Dash")],
                                 twb_xml=_thumb_xml("Dash"))
    img = bundle["visuals"][0]["image"]
    assert img["present"] is True and img["source"] == "thumbnail" and img["path"] is None


def test_bundle_nonexistent_image_dir_falls_through_to_thumbnail(tmp_path):
    bundle = build_oracle_bundle(
        [_record(page="Dash", page_display="Dash")],
        twb_xml=_thumb_xml("Dash"),
        image_dir=str(tmp_path / "does-not-exist"),
        images_out=str(tmp_path / "out"),
    )
    assert bundle["visuals"][0]["image"]["source"] == "thumbnail"


def test_write_bundle_is_bom_free_and_roundtrips(tmp_path):
    bundle = build_oracle_bundle([_record()])
    out = tmp_path / "nested" / "bundle.json"
    write_bundle(bundle, str(out))
    raw = out.read_bytes()
    assert raw[:3] != b"\xef\xbb\xbf"          # no UTF-8 BOM
    assert json.loads(raw.decode("utf-8")) == bundle


# -- provided-image resolution -------------------------------------------------

def test_provided_image_extension_priority_png_before_jpg(tmp_path):
    d = tmp_path / "imgs"
    d.mkdir()
    (d / "Dash.jpg").write_bytes(_PNG_BYTES)
    (d / "Dash.png").write_bytes(_PNG_BYTES)
    bundle = build_oracle_bundle([_record(page="Dash", page_display="Dash")], image_dir=str(d))
    assert bundle["visuals"][0]["image"]["path"].endswith("Dash.png")


def test_provided_image_matched_by_safe_stem(tmp_path):
    d = tmp_path / "imgs"
    d.mkdir()
    # display name has a colon; the file on disk uses the filesystem-safe stem.
    (d / (_safe_filename("Sales: by City") + ".png")).write_bytes(_PNG_BYTES)
    rec = _record(page="page-x", page_display="Sales: by City", worksheet="Sales: by City")
    bundle = build_oracle_bundle([rec], image_dir=str(d))
    assert bundle["visuals"][0]["image"]["present"] is True
    assert bundle["visuals"][0]["image"]["source"] == "provided"


# -- read_workbook_xml ---------------------------------------------------------

def test_read_workbook_xml_twbx_with_two_twb_picks_one(tmp_path):
    import zipfile

    p = tmp_path / "wb.twbx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("a.twb", _thumb_xml("A"))
        zf.writestr("b.twb", _thumb_xml("B"))
    xml = read_workbook_xml(str(p))
    assert xml is not None and "thumbnail" in xml


def test_read_workbook_xml_corrupt_twbx_returns_none(tmp_path):
    p = tmp_path / "bad.twbx"
    p.write_bytes(b"this is not a zip file")
    assert read_workbook_xml(str(p)) is None


# -- agent_prompt --------------------------------------------------------------

def test_agent_prompt_includes_hack_label_and_field_truth(tmp_path):
    rec = _record(page="Perf", page_display="Perf", visual="donut", worksheet="Sheet 1",
                  hack="dual-axis pie/donut",
                  fields={"Legend": ["Orders.Region"], "Values": ["_Measures.Sum of Sales"]})
    bundle = build_oracle_bundle([rec], twb_xml=_thumb_xml("Perf"), images_out=str(tmp_path))
    prompt = agent_prompt(bundle)
    assert "dual-axis pie/donut" in prompt          # the hack is surfaced for the reviewer
    assert "Orders.Region" in prompt                # the read-only field truth is shown
    assert "READ-ONLY" in prompt


def test_agent_prompt_counts_reviewable_and_excludes_others(tmp_path):
    recs = [
        _record(page="Seen", page_display="Seen", visual="shown", worksheet="W1"),
        _record(page="Unseen", page_display="Unseen", visual="hiddenviz", worksheet="W2"),
    ]
    bundle = build_oracle_bundle(recs, twb_xml=_thumb_xml("Seen"), images_out=str(tmp_path))
    prompt = agent_prompt(bundle)
    assert "Visuals to review (1)" in prompt
    assert "shown" in prompt and "hiddenviz" not in prompt


# -- priority surfaced in the bundle -------------------------------------------

def test_high_confidence_clean_visual_is_normal_priority_and_not_flagged(tmp_path):
    rec = _record(page="Dash", page_display="Dash", vtype="lineChart",
                  candidates=["lineChart", "areaChart"], confidence="high", hack=None)
    bundle = build_oracle_bundle([rec], twb_xml=_thumb_xml("Dash"), images_out=str(tmp_path))
    v = bundle["visuals"][0]
    assert v["priority"] == "normal"
    assert bundle["summary"]["flagged"] == 0
    assert v["reviewable"] is True       # reviewable regardless of priority (always-on review)


def test_thumbnail_written_to_contained_file_for_unsafe_display_name(tmp_path):
    # a dashboard caption with path characters must not escape images_out when materialized.
    rec = _record(page="p", page_display="Q1/Q2: Sales", worksheet="Q1/Q2: Sales")
    xml = _thumb_xml("Q1/Q2: Sales")
    bundle = build_oracle_bundle([rec], twb_xml=xml, images_out=str(tmp_path))
    path = bundle["visuals"][0]["image"]["path"]
    assert os.path.isfile(path)
    assert os.path.dirname(os.path.abspath(path)) == os.path.abspath(str(tmp_path))
    assert ":" not in os.path.basename(path)


# -- applier: deeper behavior --------------------------------------------------

def _multi_parts_and_records():
    """Three visuals on one page: a donut, a table, and a high-confidence line."""
    page = "page-Dash"
    recs, parts = [], {"definition.pbir": "{}"}
    specs = [
        ("v1", "donutChart", ["donutChart", "pieChart"]),
        ("v2", "tableEx", ["tableEx", "pivotTable"]),
        ("v3", "lineChart", ["lineChart", "areaChart"]),
    ]
    for name, vtype, cands in specs:
        path = f"definition/pages/{page}/visuals/{name}/visual.json"
        parts[path] = _json.dumps(_visual_part(name, vtype), indent=2)
        recs.append({"page": page, "visual": name, "worksheet": name, "visual_type": vtype,
                     "candidates": cands, "confidence": "medium", "hack": None,
                     "fields": {"X": ["t.c"]},
                     "position": {"x": 0, "y": 0, "z": 1, "width": 10, "height": 10, "tabOrder": 0}})
    return parts, recs, page


def test_applier_partitions_mixed_batch():
    parts, recs, page = _multi_parts_and_records()
    adj = [
        {"page": page, "visual": "v1", "chosen_type": "pieChart"},      # applied
        {"page": page, "visual": "v2", "chosen_type": None},           # kept
        {"page": page, "visual": "v3", "chosen_type": "treemap"},      # rejected (not candidate)
        {"page": page, "visual": "ghost", "chosen_type": "pieChart"},  # rejected (no record)
    ]
    new_parts, report = apply_adjudications(parts, recs, adj)
    assert {a["visual"] for a in report["applied"]} == {"v1"}
    assert {k["visual"] for k in report["kept"]} == {"v2"}
    assert {r["visual"] for r in report["rejected"]} == {"v3", "ghost"}
    assert _json.loads(new_parts[f"definition/pages/{page}/visuals/v1/visual.json"])["visual"]["visualType"] == "pieChart"


def test_applier_switches_on_orphan_worksheet_page_path_shape():
    # an orphan worksheet emits page 'page-ws-<ws>' / visual 'v-<ws>' -- the applier must locate it.
    page, visual = "page-ws-Sheet1", "v-Sheet1"
    path = f"definition/pages/{page}/visuals/{visual}/visual.json"
    parts = {path: _json.dumps(_visual_part(visual, "clusteredBarChart"), indent=2)}
    recs = [{"page": page, "visual": visual, "worksheet": "Sheet 1",
             "visual_type": "clusteredBarChart",
             "candidates": ["clusteredBarChart", "clusteredColumnChart"],
             "confidence": "high", "hack": None, "fields": {}, "position": {}}]
    new_parts, report = apply_adjudications(
        parts, recs, [{"page": page, "visual": visual, "chosen_type": "clusteredColumnChart"}])
    assert len(report["applied"]) == 1
    assert _json.loads(new_parts[path])["visual"]["visualType"] == "clusteredColumnChart"


def test_applier_adopts_full_position_override():
    parts, records, path = _parts_and_records()
    new_parts, report = apply_adjudications(
        parts, records,
        [{"page": "page-Dash", "visual": "v1", "chosen_type": "pieChart",
          "position": {"x": 1, "y": 2, "z": 3, "width": 4, "height": 5, "tabOrder": 6}}],
    )
    pos = _json.loads(new_parts[path])["position"]
    assert pos == {"x": 1, "y": 2, "z": 3, "width": 4, "height": 5, "tabOrder": 6}
    assert report["applied"][0]["position_changed"] is True


def test_applier_keep_is_relative_to_record_type_not_current_part():
    # 'keep' is judged against the RECORD's deterministic type, so re-asserting it after a switch
    # does not silently revert an already-applied change -- it is reported as kept, part unchanged.
    parts, records, path = _parts_and_records()
    switched, _ = apply_adjudications(
        parts, records, [{"page": "page-Dash", "visual": "v1", "chosen_type": "pieChart"}])
    assert _json.loads(switched[path])["visual"]["visualType"] == "pieChart"
    again, report = apply_adjudications(
        switched, records, [{"page": "page-Dash", "visual": "v1", "chosen_type": "donutChart"}])
    assert len(report["kept"]) == 1 and not report["applied"]
    assert again[path] == switched[path]   # the prior pieChart switch is left intact


def test_applier_empty_or_none_adjudications_is_noop():
    parts, records, _ = _parts_and_records()
    for adj in ([], None):
        new_parts, report = apply_adjudications(parts, records, adj)
        assert new_parts == parts
        assert report == {"applied": [], "kept": [], "rejected": []}


def test_applier_does_not_mutate_candidate_records_input():
    parts, records, _ = _parts_and_records()
    before = _json.loads(_json.dumps(records))
    apply_adjudications(parts, records,
                        [{"page": "page-Dash", "visual": "v1", "chosen_type": "pieChart"}])
    assert records == before


def test_applier_position_changed_false_without_position():
    parts, records, _ = _parts_and_records()
    _, report = apply_adjudications(
        parts, records, [{"page": "page-Dash", "visual": "v1", "chosen_type": "pieChart"}])
    assert report["applied"][0]["position_changed"] is False


def test_applier_locates_part_by_name_fallback_when_page_segment_differs():
    # the part lives under a different page segment than the record's page id; the name-match
    # fallback in _locate_visual_part must still find it by the visual's parsed name.
    visual = "v1"
    odd_path = f"definition/pages/SOME-OTHER-SEGMENT/visuals/{visual}/visual.json"
    parts = {odd_path: _json.dumps(_visual_part(visual, "donutChart"), indent=2)}
    records = [{"page": "page-Dash", "visual": visual, "worksheet": "S", "visual_type": "donutChart",
                "candidates": ["donutChart", "pieChart"], "confidence": "medium", "hack": None,
                "fields": {}, "position": {}}]
    new_parts, report = apply_adjudications(
        parts, records, [{"page": "page-Dash", "visual": visual, "chosen_type": "pieChart"}])
    assert len(report["applied"]) == 1
    assert _json.loads(new_parts[odd_path])["visual"]["visualType"] == "pieChart"


# -- full chain over a real corpus (opt-in: set IMAGE_ORACLE_CORPUS=<dir>) ------
# Offline but disk-backed, so it is gated behind an env var and skipped by default. Point the var at
# a folder of real .twb/.twbx workbooks to validate the entire migrate -> bundle -> apply chain and
# every oracle invariant across the whole estate (the CI suite stays hermetic).

def _corpus_workbooks():
    root = os.environ.get("IMAGE_ORACLE_CORPUS")
    if not root or not os.path.isdir(root):
        return []
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith((".twb", ".twbx")):
                out.append(os.path.join(dirpath, f))
    return out


@pytest.mark.skipif(not _corpus_workbooks(),
                    reason="set IMAGE_ORACLE_CORPUS to a dir of .twb/.twbx to run the swath")
def test_oracle_chain_holds_over_every_corpus_workbook():
    import hashlib

    from twb_to_pbir import migrate_twb_to_pbir

    seen, validated, total_records, total_switches = set(), 0, 0, 0
    for path in _corpus_workbooks():
        xml = read_workbook_xml(path)
        if not xml:
            continue
        h = hashlib.md5(xml.encode("utf-8", "ignore")).hexdigest()
        if h in seen:
            continue                      # dedup identical copies
        seen.add(h)
        res = migrate_twb_to_pbir(xml, dataset_name="M", report_name="R")
        parts, records = res["parts"], res["candidate_records"]
        validated += 1
        total_records += len(records)

        # additivity: the records never leak into the emitted PBIR
        assert "candidate_records" not in "\n".join(parts.values())

        bundle = build_oracle_bundle(records, twb_xml=xml)
        assert bundle["summary"]["visuals"] == len(records)
        for thumb in extract_thumbnails(xml).values():
            assert thumb[:8] == b"\x89PNG\r\n\x1a\n"

        for r in records:
            assert r["candidates"] and r["visual_type"] == r["candidates"][0]
            assert {"x", "y", "z", "width", "height", "tabOrder"} <= set(r["position"])
            # every emitted visual is locatable by the applier
            from image_oracle import _locate_visual_part
            assert _locate_visual_part(parts, r["page"], r["visual"]) is not None

        # all-keep is a byte-for-byte no-op
        keep = [{"page": r["page"], "visual": r["visual"], "chosen_type": None} for r in records]
        np_keep, rep_keep = apply_adjudications(parts, records, keep)
        assert np_keep == parts and len(rep_keep["kept"]) == len(records)
        assert not rep_keep["applied"] and not rep_keep["rejected"]

        # every multi-candidate visual can switch to candidates[1] with the query preserved
        multi = [r for r in records if len(r["candidates"]) >= 2]
        switch = [{"page": r["page"], "visual": r["visual"], "chosen_type": r["candidates"][1]}
                  for r in multi]
        np_sw, rep_sw = apply_adjudications(parts, records, switch)
        assert len(rep_sw["applied"]) == len(multi) and not rep_sw["rejected"]
        total_switches += len(multi)
        for r in multi:
            from image_oracle import _locate_visual_part
            p = _locate_visual_part(parts, r["page"], r["visual"])
            assert json.loads(np_sw[p])["visual"]["query"] == json.loads(parts[p])["visual"]["query"]

        # a non-candidate type is always rejected and leaves the parts untouched
        bogus = [{"page": r["page"], "visual": r["visual"], "chosen_type": "definitelyNotAType"}
                 for r in records]
        np_bo, rep_bo = apply_adjudications(parts, records, bogus)
        assert len(rep_bo["rejected"]) == len(records) and np_bo == parts

    assert validated >= 1


