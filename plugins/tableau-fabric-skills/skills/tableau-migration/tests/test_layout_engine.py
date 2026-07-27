"""Zone Geometry v3 -- the ``--layout`` engine seam (frame track slice 4d).

The solver stack (``zone_tree`` -> ``layout_solve`` -> ``layout_layers`` -> ``layout_plan``) is wired
into the emit path behind an opt-in ``layout="solver"`` switch. These tests pin the two halves of
that contract:

* **Never-regress.** ``legacy`` is the default and its output is unchanged -- the plan is not even
  built, no rect is substituted, and every post-placement repair pass behaves exactly as before.
* **Solver correctness.** When a plan exists, emit takes its rects VERBATIM (re-applying the min
  floors would re-inflate boxes the solver deliberately sized, re-introducing the overlap the tree
  solve removed), adopts the page the solve resolved (a solved rect is valid only on that page), and
  falls back to the legacy scale zone-by-zone for anything the plan does not name -- never half-solved.
"""
import json

import pytest

import layout_solve

import twb_to_pbir
from twb_to_pbir import (
    LAYOUT_DEFAULT,
    LAYOUT_ENGINES,
    SLICER_DROPDOWN_MIN_H,
    _dash_page_dims,
    _layout_slicers,
    _scale_zone,
    _solved_rect,
    emit_pbir,
    main,
    migrate_twb_to_pbir,
    parse_twb,
)

from test_twb_to_pbir import _INST, _workbook, _worksheet


def _tiled_dashboard(name="D", w=1200, h=800):
    """Two tiled worksheets stacked in a flow container -- a tree the solver can resolve."""
    return (f"<dashboard name='{name}'><size maxheight='{h}' maxwidth='{w}' />"
            "<zones><zone h='100000' w='100000' x='0' y='0' id='1'>"
            "<zone h='50000' w='100000' x='0' y='0' name='W' id='2' />"
            "<zone h='50000' w='100000' x='0' y='50000' name='W2' id='3' />"
            "</zone></zones></dashboard>")


def _two_sheet_workbook(dash=None):
    ws = _worksheet("W", "Bar", "[federated.abc].[sum:Sales:qk]",
                    "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    ws2 = _worksheet("W2", "Bar", "[federated.abc].[sum:Sales:qk]",
                     "[federated.abc].[none:Category:nk]", deps_extra=_INST)
    return _workbook(ws + ws2, dash if dash is not None else _tiled_dashboard())


def _positions(parts):
    """{visual name: (x, y, w, h)} across every emitted page."""
    out = {}
    for path, txt in parts.items():
        if not path.replace("\\", "/").endswith("/visual.json"):
            continue
        doc = json.loads(txt)
        p = doc["position"]
        out[doc["name"]] = (p["x"], p["y"], p["width"], p["height"])
    return out


def _pages(parts):
    """{page name: (width, height)}."""
    out = {}
    for path, txt in parts.items():
        norm = path.replace("\\", "/")
        if norm.endswith("/page.json"):
            doc = json.loads(txt)
            out[doc.get("displayName") or doc["name"]] = (doc["width"], doc["height"])
    return out


# -- the never-regress half -----------------------------------------------------------------


def test_layout_default_is_legacy():
    assert LAYOUT_DEFAULT == "legacy"
    assert LAYOUT_ENGINES == ("legacy", "solver")


def test_default_run_is_byte_identical_to_explicit_legacy():
    # The seam must be inert unless asked for: omitting ``layout`` and passing ``legacy`` produce
    # the exact same bytes, so an existing caller cannot drift onto the solver by accident.
    xml = _two_sheet_workbook()
    assert migrate_twb_to_pbir(xml)["parts"] == migrate_twb_to_pbir(xml, layout="legacy")["parts"]


def test_legacy_parse_attaches_no_plan():
    ir = parse_twb(_two_sheet_workbook())
    assert ir["dashboards"][0]["layout_plan"] is None


def test_legacy_emit_never_consults_a_plan(monkeypatch):
    # Belt-and-braces: under the default engine the plan builder is not reached at all, so a
    # solver-stack regression can never leak into the default path.
    calls = []

    class _Boom:
        @staticmethod
        def build_plan(*a, **k):
            calls.append(a)
            raise AssertionError("legacy must not build a layout plan")

    monkeypatch.setattr(twb_to_pbir, "_layout_plan", _Boom)
    migrate_twb_to_pbir(_two_sheet_workbook())
    assert calls == []


def test_scale_zone_without_a_plan_applies_the_legacy_floors():
    twb_to_pbir._LAYOUT_PLAN = None
    zone = {"zone_id": "2", "x": 0, "y": 0, "w": 100, "h": 100}
    x, y, w, h = _scale_zone(zone, 100000, 100000, min_w=40.0, min_h=40.0)
    assert (w, h) == (40.0, 40.0)   # tiny zone inflated to the floor, as legacy always did


# -- the solver half ------------------------------------------------------------------------


def test_solver_parse_attaches_a_plan_keyed_by_zone_id():
    ir = parse_twb(_two_sheet_workbook(), layout="solver")
    plan = ir["dashboards"][0]["layout_plan"]
    assert plan is not None
    assert set(plan["rects"]) >= {"2", "3"}
    assert plan["page"] == (1200.0, 800.0)


def test_solver_changes_geometry_relative_to_legacy():
    xml = _two_sheet_workbook()
    legacy = _positions(migrate_twb_to_pbir(xml, layout="legacy")["parts"])
    solver = _positions(migrate_twb_to_pbir(xml, layout="solver")["parts"])
    assert set(legacy) == set(solver)          # same visuals -- nothing dropped or added
    assert legacy != solver                    # ...but resolved by a different engine


def test_solved_rect_is_taken_verbatim_and_ignores_the_min_floors():
    # THE core contract. A solver rect is already disjoint from its siblings; re-applying the min
    # floors would grow it back over them, re-creating the very overlap the tree solve removed.
    twb_to_pbir._LAYOUT_PLAN = {"rects": {"7": (11.0, 22.0, 3.0, 4.0)}}
    try:
        zone = {"zone_id": "7", "x": 0, "y": 0, "w": 90000, "h": 90000}
        assert _scale_zone(zone, 100000, 100000, min_w=500.0, min_h=500.0) == (11.0, 22.0, 3.0, 4.0)
    finally:
        twb_to_pbir._LAYOUT_PLAN = None


def test_zone_without_a_zone_id_falls_back_to_the_legacy_scale():
    twb_to_pbir._LAYOUT_PLAN = {"rects": {"7": (11.0, 22.0, 3.0, 4.0)}}
    try:
        assert _solved_rect({"x": 0, "y": 0, "w": 1, "h": 1}) is None
    finally:
        twb_to_pbir._LAYOUT_PLAN = None


def test_zone_id_absent_from_the_plan_falls_back_to_the_legacy_scale():
    twb_to_pbir._LAYOUT_PLAN = {"rects": {"7": (11.0, 22.0, 3.0, 4.0)}}
    try:
        assert _solved_rect({"zone_id": "999"}) is None
    finally:
        twb_to_pbir._LAYOUT_PLAN = None


def test_a_failed_plan_degrades_to_legacy_wholesale(monkeypatch):
    # ``build_plan`` fails closed to ``None`` on an unsolvable dashboard; emit must then produce
    # exactly the legacy output rather than a half-solved page.
    class _NoPlan:
        @staticmethod
        def build_plan(*a, **k):
            return None

    xml = _two_sheet_workbook()
    baseline = migrate_twb_to_pbir(xml, layout="legacy")["parts"]
    monkeypatch.setattr(twb_to_pbir, "_layout_plan", _NoPlan)
    assert migrate_twb_to_pbir(xml, layout="solver")["parts"] == baseline


def test_a_raising_plan_builder_degrades_to_legacy_wholesale(monkeypatch):
    class _Raiser:
        @staticmethod
        def build_plan(*a, **k):
            raise RuntimeError("solver exploded")

    xml = _two_sheet_workbook()
    baseline = migrate_twb_to_pbir(xml, layout="legacy")["parts"]
    monkeypatch.setattr(twb_to_pbir, "_layout_plan", _Raiser)
    assert migrate_twb_to_pbir(xml, layout="solver")["parts"] == baseline


def test_absent_solver_stack_still_runs_the_legacy_engine(monkeypatch):
    xml = _two_sheet_workbook()
    baseline = migrate_twb_to_pbir(xml, layout="legacy")["parts"]
    monkeypatch.setattr(twb_to_pbir, "_layout_plan", None)
    assert migrate_twb_to_pbir(xml, layout="solver")["parts"] == baseline


def test_emitted_page_adopts_the_page_the_solver_resolved(monkeypatch):
    # A solved rect is valid ONLY on the page the solve resolved. Growth is not cosmetic -- keeping
    # the authored page while using grown-page rects pushes visuals out of bounds.
    real = twb_to_pbir._layout_plan

    class _Grown:
        @staticmethod
        def build_plan(db, **k):
            plan = real.build_plan(db, **k)
            if plan is None:
                return None
            plan = dict(plan)
            plan["page"] = (1600.0, 1300.0)
            plan["grew"] = True
            return plan

    monkeypatch.setattr(twb_to_pbir, "_layout_plan", _Grown)
    parts = migrate_twb_to_pbir(_two_sheet_workbook(), layout="solver")["parts"]
    assert _pages(parts)["D"] == (1600, 1300)


def test_page_override_is_reset_after_the_dashboard_loop():
    # The orphan worksheet pages that follow the dashboards must not inherit a solved canvas.
    parts = migrate_twb_to_pbir(_two_sheet_workbook(), layout="solver")["parts"]
    assert _pages(parts)["D"] == (1200, 800)
    assert twb_to_pbir._LAYOUT_PLAN is None


def test_dash_page_dims_is_the_single_definition_of_the_canvas():
    # The plan is solved against exactly the page emit derives; if these two ever disagree every
    # rect the solver produces is silently invalid.
    assert _dash_page_dims({"w": 1400.0, "h": 1000.0, "min_w": None, "min_h": None}) == (1400.0, 1000.0)
    assert _dash_page_dims({"w": None, "h": None, "min_w": None, "min_h": None}) == (
        float(twb_to_pbir.DASH_DEFAULT_W), float(twb_to_pbir.DASH_DEFAULT_H))
    auto = twb_to_pbir._automatic_canvas_dims(800.0, 600.0)
    assert _dash_page_dims({"w": None, "h": None, "min_w": 800.0, "min_h": 600.0}) == (
        float(auto[0]), float(auto[1]))


def test_solver_page_matches_dash_page_dims_when_nothing_grows():
    ir = parse_twb(_two_sheet_workbook(), layout="solver")
    db = ir["dashboards"][0]
    assert db["layout_plan"]["page"] == _dash_page_dims(db["size"])


# -- the slicer floor coupling ---------------------------------------------------------------


def test_solver_slicer_minimum_equals_the_emitters_own_floor():
    # The solve must reserve what emit will actually use. Reserving less does not shrink the
    # emitted slicer -- emit re-floors it -- it just makes it overrun whatever was seated below.
    assert layout_solve.MIN_SLICER[1] == SLICER_DROPDOWN_MIN_H


def test_layout_slicers_floors_the_height_under_legacy():
    twb_to_pbir._LAYOUT_PLAN = None
    entries = [{"x": 0.0, "y": 100.0, "w": 200.0, "h": 20.0, "mode": "Dropdown"}]
    _layout_slicers(entries)
    assert entries[0]["h"] == SLICER_DROPDOWN_MIN_H


def test_layout_slicers_keeps_the_solved_height_under_the_solver():
    twb_to_pbir._LAYOUT_PLAN = {"rects": {}}
    try:
        entries = [{"x": 0.0, "y": 100.0, "w": 200.0, "h": 20.0, "mode": "Dropdown"},
                   {"x": 0.0, "y": 300.0, "w": 200.0, "h": 30.0, "mode": "Dropdown"}]
        _layout_slicers(entries)
        assert entries[0]["h"] == 20.0
        assert entries[1]["h"] == 30.0
        assert entries[1]["y"] == 300.0       # no growth => no downward shift of the rows below
    finally:
        twb_to_pbir._LAYOUT_PLAN = None


def test_layout_slicers_still_insets_horizontally_under_the_solver():
    # The x inset only ever SHRINKS a card, so it cannot introduce a collision and is kept.
    twb_to_pbir._LAYOUT_PLAN = {"rects": {}}
    try:
        entries = [{"x": 10.0, "y": 0.0, "w": 200.0, "h": 64.0, "mode": "Dropdown"}]
        _layout_slicers(entries)
        assert entries[0]["x"] > 10.0
        assert entries[0]["w"] < 200.0
    finally:
        twb_to_pbir._LAYOUT_PLAN = None


# -- CLI ------------------------------------------------------------------------------------


def test_cli_rejects_an_unknown_layout_engine(tmp_path):
    src = tmp_path / "wb.twb"
    src.write_text(_two_sheet_workbook(), encoding="utf-8")
    with pytest.raises(SystemExit):
        main([str(src), "--layout", "magic"])


def test_cli_layout_solver_writes_a_report(tmp_path, capsys):
    src = tmp_path / "wb.twb"
    src.write_text(_two_sheet_workbook(), encoding="utf-8")
    out = tmp_path / "out"
    main([str(src), "-o", str(out), "--layout", "solver"])
    capsys.readouterr()
    assert (out / "Report.Report" / "definition" / "report.json").exists()


def test_cli_defaults_to_legacy_when_the_flag_is_absent(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TWB_PBIR_LAYOUT", raising=False)
    src = tmp_path / "wb.twb"
    src.write_text(_two_sheet_workbook(), encoding="utf-8")
    seen = {}
    real = twb_to_pbir.migrate_twb_to_pbir

    def _spy(xml_text, **kw):
        seen["layout"] = kw.get("layout")
        return real(xml_text, **kw)

    monkeypatch.setattr(twb_to_pbir, "migrate_twb_to_pbir", _spy)
    main([str(src)])
    capsys.readouterr()
    assert seen["layout"] == "legacy"


def test_emit_pbir_ir_round_trip_under_the_solver():
    # ``emit_pbir`` reads the plan off the parsed IR, so a caller that parses and emits in two
    # steps (the estate builder's shape) gets the same result as the one-call helper.
    xml = _two_sheet_workbook()
    one_call = migrate_twb_to_pbir(xml, layout="solver")["parts"]
    two_step = emit_pbir(parse_twb(xml, layout="solver"))
    assert _positions(two_step) == _positions(one_call)


# -- the v2.7.0 geometry goldens, now held under BOTH engines --------------------------------
# v2.7.0 locked "a clean page stays clean" against the legacy engine. Those goldens are left
# byte-for-byte alone; these run the SAME workbooks and the SAME auditor under each engine in turn,
# so the solver path carries the identical regression net rather than an unguarded parallel one. If
# a future solver change starts manufacturing overlap on a page legacy keeps clean, this fails.

_GOLDEN_WORKBOOKS = (
    ("clean_tiled", "_V27_CLEAN_TILED"),
    ("deoverlapped", "_V2_DEOVERLAP_TWB"),
    ("already_clear", "_V2_CLEAR_CAPTION_TWB"),
    ("caption_only", "_V2_CAPTION_ONLY_STATIC"),
)


def _golden_twb(attr):
    import test_twb_to_pbir

    return getattr(test_twb_to_pbir, attr)


def _worst_page_defects(parts):
    """The summed geometry defects across every emitted page."""
    import geometry_audit

    total = {"overlaps": 0, "contain": 0, "oob": 0}
    for pg in geometry_audit.pages_from_parts(parts).values():
        if not pg["w"] or not pg["h"]:
            continue
        d = geometry_audit.geometry_defects(pg["visuals"], pg["w"], pg["h"])
        for k in total:
            total[k] += d[k]
    return total


@pytest.mark.parametrize("engine", LAYOUT_ENGINES)
@pytest.mark.parametrize("label,attr", _GOLDEN_WORKBOOKS)
def test_v2_7_goldens_stay_clean_under_both_engines(engine, label, attr):
    res = migrate_twb_to_pbir(_golden_twb(attr), layout=engine)
    d = _worst_page_defects(res["parts"])
    assert d == {"overlaps": 0, "contain": 0, "oob": 0}, f"{label}/{engine}: {d}"


@pytest.mark.parametrize("engine", LAYOUT_ENGINES)
def test_thin_worksheet_still_emits_under_both_engines(engine):
    # A short band must still GENERATE its visual under either engine -- the solver resolves
    # geometry, it must never drop a zone.
    res = migrate_twb_to_pbir(_golden_twb("_V27_THIN_WS"), layout=engine)
    import geometry_audit

    kinds = sorted(v["visual"]["visualType"]
                   for pg in geometry_audit.pages_from_parts(res["parts"]).values()
                   for v in pg["visuals"])
    assert kinds == ["clusteredColumnChart", "lineChart"]


@pytest.mark.parametrize("engine", LAYOUT_ENGINES)
def test_no_visual_is_lost_between_the_engines(engine):
    # The strongest completeness guard: whatever the layout engine, the SAME set of visuals is
    # emitted. A solver that silently failed to place a zone would show up here, not as geometry.
    xml = _two_sheet_workbook()
    baseline = set(_positions(migrate_twb_to_pbir(xml, layout="legacy")["parts"]))
    assert set(_positions(migrate_twb_to_pbir(xml, layout=engine)["parts"])) == baseline

