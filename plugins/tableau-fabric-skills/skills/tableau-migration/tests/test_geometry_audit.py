"""Unit tests for ``scripts/geometry_audit.py`` -- the promoted v2.7.0 layout-defect auditor.

The auditor was moved out of ``tests/test_twb_to_pbir.py`` in Zone Geometry v3 slice 3 so the A/B
harness can score both engines from outside the suite. These tests pin the exact thresholds the
"pure move" must preserve (TOL=1.0, the > 2 % overlap floor, the 4 px raw-intersection gate, the
<= 41 px squash floor, full-nesting classified as containment), the composite-group exemption, and
the part-map / directory / CLI plumbing.
"""
import json
import subprocess
import sys
from pathlib import Path

import geometry_audit as ga

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _vis(x, y, w, h, group=None):
    d = {"position": {"x": x, "y": y, "width": w, "height": h, "z": 0}}
    if group is not None:
        d["parentGroupName"] = group
    return d


# --------------------------------------------------------------------------- primitives
def test_tol_is_one():
    assert ga.TOL == 1.0


def test_rect_extracts_and_defaults_missing_size():
    assert ga.rect(_vis(3, 4, 50, 60)) == (3, 4, 50, 60)
    # missing/None width or height read as 0
    assert ga.rect({"position": {"x": 1, "y": 2}}) == (1, 2, 0, 0)
    assert ga.rect({"position": {"x": 1, "y": 2, "width": None, "height": 5}}) == (1, 2, 0, 5)


def test_intersection_area_touch_is_zero_overlap_is_area():
    a = (0, 0, 100, 100)
    assert ga.intersection_area(a, (100, 0, 50, 50)) == 0.0     # shares only an edge
    assert ga.intersection_area(a, (90, 0, 100, 100)) == 10 * 100  # 10px-wide strip


def test_contains_respects_tolerance():
    outer = (0, 0, 200, 200)
    assert ga.contains(outer, (50, 50, 50, 50))
    assert ga.contains(outer, (-1.0, -1.0, 202.0, 202.0))   # exactly TOL past every edge
    assert not ga.contains(outer, (50, 50, 200, 50))        # spills past the right edge


def test_composite_group_of_none_when_absent_or_empty():
    assert ga.composite_group_of(_vis(0, 0, 1, 1)) is None
    assert ga.composite_group_of(_vis(0, 0, 1, 1, group="")) is None
    assert ga.composite_group_of(_vis(0, 0, 1, 1, group="donut_1")) == "donut_1"


# --------------------------------------------------------------------------- defect catalogue
def test_partial_overlap_counts_when_above_two_percent():
    a = _vis(0, 0, 100, 100)
    b = _vis(90, 0, 100, 100)     # 10 % of the smaller rect
    d = ga.geometry_defects([a, b], 1280, 720)
    assert d["overlaps"] == 1 and d["contain"] == 0


def test_two_percent_is_a_strict_floor():
    a = _vis(0, 0, 100, 100)
    # 2px strip = 2 % exactly -> NOT counted (strict >); 3px strip = 3 % -> counted.
    at_floor = _vis(98, 0, 100, 100)
    over_floor = _vis(97, 0, 100, 100)
    assert ga.geometry_defects([a, at_floor], 1280, 720)["overlaps"] == 0
    assert ga.geometry_defects([a, over_floor], 1280, 720)["overlaps"] == 1


def test_tiny_intersection_is_ignored():
    # a 2px x 1px sliver = 2px^2 <= 4px^2 raw-intersection gate -> skipped entirely
    a = _vis(0, 0, 100, 1)
    b = _vis(98, 0, 100, 1)
    assert ga.geometry_defects([a, b], 1280, 720)["overlaps"] == 0


def test_touching_corner_is_not_an_overlap():
    a = _vis(0, 0, 100, 100)
    corner = _vis(100, 100, 100, 100)
    assert ga.geometry_defects([a, corner], 1280, 720)["overlaps"] == 0


def test_full_nesting_is_containment_not_overlap():
    big = _vis(0, 0, 400, 400)
    inside = _vis(50, 50, 60, 60)
    d = ga.geometry_defects([big, inside], 1280, 720)
    assert d["contain"] == 1 and d["overlaps"] == 0


def test_out_of_bounds_on_each_edge():
    assert ga.geometry_defects([_vis(-5, 10, 50, 50)], 1280, 720)["oob"] == 1     # left
    assert ga.geometry_defects([_vis(10, -5, 50, 50)], 1280, 720)["oob"] == 1     # top
    assert ga.geometry_defects([_vis(1260, 10, 50, 50)], 1280, 720)["oob"] == 1   # right
    assert ga.geometry_defects([_vis(10, 700, 50, 50)], 1280, 720)["oob"] == 1    # bottom
    assert ga.geometry_defects([_vis(10, 10, 50, 50)], 1280, 720)["oob"] == 0     # inside


def test_zero_page_dims_disable_far_edge_oob():
    # page_w / page_h of 0 (unknown) must not manufacture a far-edge oob
    assert ga.geometry_defects([_vis(10, 10, 5000, 5000)], 0, 0)["oob"] == 0


def test_floor_squash_on_either_axis():
    assert ga.geometry_defects([_vis(10, 10, 41, 100)], 1280, 720)["floor"] == 1   # thin width
    assert ga.geometry_defects([_vis(10, 10, 100, 41)], 1280, 720)["floor"] == 1   # thin height
    assert ga.geometry_defects([_vis(10, 10, 42, 42)], 1280, 720)["floor"] == 0    # just above


def test_clean_page_has_no_defects():
    a = _vis(0, 0, 600, 700)
    b = _vis(620, 0, 600, 700)     # tidy gutter, both full-size, in bounds
    assert dict(ga.geometry_defects([a, b], 1280, 720)) == {}


# --------------------------------------------------------------------------- composite exemption
def test_same_group_overlap_is_exempt():
    ring = _vis(100, 100, 200, 200, group="donut_1")
    card = _vis(150, 150, 100, 100, group="donut_1")   # centre KPI, overlaps by design
    d = ga.geometry_defects([ring, card], 1280, 720)
    assert d["overlaps"] == 0 and d["contain"] == 0


def test_cross_group_and_ungrouped_still_count():
    ring = _vis(100, 100, 200, 200, group="donut_1")
    card = _vis(150, 150, 100, 100, group="donut_1")
    stray = _vis(120, 120, 90, 90)                      # ungrouped, overlaps the ring
    d = ga.geometry_defects([ring, card, stray], 1280, 720)
    assert d["overlaps"] + d["contain"] >= 1


# --------------------------------------------------------------------------- part-map plumbing
def _report_parts():
    return {
        "definition/pages/p1/page.json":
            json.dumps({"displayName": "Overview", "width": 1280, "height": 720}),
        "definition/pages/p1/visuals/v1/visual.json":
            json.dumps(_vis(0, 0, 100, 100)),
        "definition/pages/p1/visuals/v2/visual.json":
            json.dumps(_vis(90, 0, 100, 100)),   # overlaps v1
        "definition/pages/p2/page.json":
            json.dumps({"displayName": "Detail", "width": 1280, "height": 720}),
        "definition/pages/p2/visuals/v1/visual.json":
            json.dumps(_vis(0, 0, 600, 700)),
        "definition/pages/pages.json":
            json.dumps({"pageOrder": ["p1", "p2"]}),   # index -> must be skipped
    }


def test_pages_from_parts_groups_and_skips_index():
    pages = ga.pages_from_parts(_report_parts())
    assert set(pages) == {"p1", "p2"}
    assert pages["p1"]["display"] == "Overview"
    assert pages["p1"]["w"] == 1280 and pages["p1"]["h"] == 720
    assert len(pages["p1"]["visuals"]) == 2
    assert len(pages["p2"]["visuals"]) == 1


def test_score_report_keys_by_display_and_scores_each_page():
    scored = ga.score_report(_report_parts())
    assert set(scored) == {"Overview", "Detail"}
    assert scored["Overview"]["overlaps"] == 1
    assert dict(scored["Detail"]) == {}


def test_backslash_paths_are_normalized():
    parts = {
        "definition\\pages\\p1\\page.json":
            json.dumps({"displayName": "Win", "width": 800, "height": 600}),
        "definition\\pages\\p1\\visuals\\v1\\visual.json":
            json.dumps(_vis(0, 0, 100, 100)),
    }
    scored = ga.score_report(parts)
    assert set(scored) == {"Win"}


# --------------------------------------------------------------------------- directory + CLI
def _write_report(tmp_path):
    root = tmp_path / "My.Report"
    for rel, txt in _report_parts().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")
    return root


def test_parts_from_dir_roundtrips(tmp_path):
    root = _write_report(tmp_path)
    scored = ga.score_report(ga.parts_from_dir(str(root)))
    assert scored["Overview"]["overlaps"] == 1
    assert dict(scored["Detail"]) == {}


def test_cli_json_output(tmp_path):
    root = _write_report(tmp_path)
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "geometry_audit.py"), str(root), "--json"],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(out.stdout)
    assert parsed["Overview"]["overlaps"] == 1


def test_cli_text_output(tmp_path):
    root = _write_report(tmp_path)
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "geometry_audit.py"), str(root)],
        capture_output=True, text=True, check=True,
    )
    assert "Overview: overlaps=1" in out.stdout


def test_cli_usage_error_without_arg():
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "geometry_audit.py")],
        capture_output=True, text=True,
    )
    assert out.returncode == 2
    assert "usage" in out.stderr.lower()
