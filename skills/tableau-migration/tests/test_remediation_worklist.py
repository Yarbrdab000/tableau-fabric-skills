"""Tests for the deterministic remediation worklist (``remediation_worklist.build_worklist``).

The worklist folds the engine's ``warnings`` + ``candidate_records`` into a structured, per-visual
audit. These tests pin the contract: every warning becomes an item (superset), deferred facts and
emitted-but-approximate visuals also surface, every rebuilt visual is enumerated for full coverage,
categories/severities classify deterministically, unknown inputs never drop, and the inputs are never
mutated. Synthetic (engine-independent) fixtures keep the suite fast and offline.
"""
import copy

from remediation_worklist import (
    build_worklist,
    WORKLIST_VERSION,
    WORKLIST_KIND,
    SEVERITY_RANK,
)


def _warn(scope, name, reason):
    # Mirror twb_to_pbir._warn's shape (the standard prefix is stripped by the classifier).
    return {"scope": scope, "name": name, "reason": "manual attention required: " + reason}


def _visual_rec(visual, worksheet, vtype="clusteredBarChart", page="Dash", confidence=0.9, **extra):
    rec = {
        "visual": visual, "worksheet": worksheet, "page": page, "page_display": page,
        "visual_type": vtype, "confidence": confidence, "hack": None,
        "fields": {}, "position": {},
    }
    rec.update(extra)
    return rec


# -- basic shape ---------------------------------------------------------------
def test_empty_inputs_produce_empty_worklist():
    wl = build_worklist([], [])
    assert wl["version"] == WORKLIST_VERSION
    assert wl["kind"] == WORKLIST_KIND
    assert wl["summary"]["items_total"] == 0
    assert wl["summary"]["visuals_total"] == 0
    assert wl["items"] == []
    assert wl["visuals"] == []


def test_none_inputs_are_tolerated():
    wl = build_worklist(None, None)
    assert wl["summary"]["items_total"] == 0
    assert wl["visuals"] == []


# -- superset guarantee: every warning becomes an item -------------------------
def test_every_warning_becomes_an_item():
    recs = [_visual_rec("v1", "Sheet A")]
    warns = [
        _warn("worksheet", "Sheet A", "field 'X' bound by caption fallback (no datasource metadata)"),
        _warn("dashboard", "Dash", "parameter control 'P' not rebuilt as a slicer yet"),
        _warn("worksheet", "Gone", "mark class 'Shape' / shelf layout not supported -> no visual emitted"),
    ]
    wl = build_worklist(warns, recs)
    # 3 warnings -> at least 3 items (the caption one attaches to v1; the others are unattached).
    assert wl["summary"]["items_total"] >= 3
    reasons = [it["reason"] for it in wl["items"]]
    assert any("caption fallback" in r for r in reasons)
    assert any("not rebuilt as a slicer" in r for r in reasons)
    assert any("no visual emitted" in r for r in reasons)


def test_worksheet_warning_attaches_to_matching_visual():
    recs = [_visual_rec("v1", "Sheet A")]
    warns = [_warn("worksheet", "Sheet A", "field 'X' bound by caption fallback")]
    wl = build_worklist(warns, recs)
    item = next(it for it in wl["items"] if "caption fallback" in it["reason"])
    assert item["visual"] == "v1"
    assert item["worksheet"] == "Sheet A"
    assert item["category"] == "field_binding"
    assert item["severity"] == "high"


def test_dashboard_warning_is_unattached_and_page_scoped():
    recs = [_visual_rec("v1", "Sheet A")]
    warns = [_warn("dashboard", "MyDash", "parameter control 'P' not rebuilt as a slicer yet")]
    wl = build_worklist(warns, recs)
    item = next(it for it in wl["items"] if "not rebuilt" in it["reason"])
    assert item["visual"] is None
    assert item["page"] == "MyDash"
    assert item["scope"] == "dashboard"
    assert wl["summary"]["unattached_items"] >= 1


def test_unmatched_worksheet_warning_stays_unattached_but_named():
    recs = [_visual_rec("v1", "Sheet A")]
    warns = [_warn("worksheet", "Ghost", "empty worksheet (no fields) -> nothing to rebuild")]
    wl = build_worklist(warns, recs)
    item = next(it for it in wl["items"] if "nothing to rebuild" in it["reason"])
    assert item["visual"] is None
    assert item["worksheet"] == "Ghost"
    assert item["severity"] == "blocking"


# -- deferred facts ------------------------------------------------------------
def test_deferred_fact_becomes_item_with_facts_attached():
    fact = {"kind": "background_color_scale", "status": "deferred", "reason": "colour driver unresolved"}
    recs = [_visual_rec("v1", "Sheet A", conditional_format=fact)]
    wl = build_worklist([], recs)
    item = next(it for it in wl["items"] if it["source"] == "deferred_fact")
    assert item["category"] == "color_scale"
    assert item["visual"] == "v1"
    assert item["facts"] is fact or item["facts"] == fact
    assert item["reason"] == "colour driver unresolved"


def test_emitted_fact_is_not_a_deferred_item():
    fact = {"kind": "background_color_scale", "status": "emitted"}
    recs = [_visual_rec("v1", "Sheet A", conditional_format=fact)]
    wl = build_worklist([], recs)
    assert not any(it["source"] == "deferred_fact" for it in wl["items"])


def test_deferred_visual_calc_is_high_severity():
    fact = {"kind": "visual_calculation", "status": "deferred", "reason": "table calc unresolved"}
    recs = [_visual_rec("v1", "Sheet A", visual_calc=fact)]
    wl = build_worklist([], recs)
    item = next(it for it in wl["items"] if it["source"] == "deferred_fact")
    assert item["category"] == "visual_calc"
    assert item["severity"] == "high"


# -- advisory (emitted but improvable) -----------------------------------------
def test_default_palette_emits_advisory_when_no_warning():
    fact = {"kind": "chart_continuous_fill", "status": "emitted", "default_palette": True}
    recs = [_visual_rec("v1", "Sheet A", chart_continuous_fill=fact)]
    wl = build_worklist([], recs)
    adv = [it for it in wl["items"] if it["source"] == "advisory"]
    assert len(adv) == 1
    assert adv[0]["category"] == "color_scale"
    assert adv[0]["severity"] == "low"


def test_default_palette_advisory_suppressed_when_color_warning_present():
    # A default-palette WARNING already raised color_scale for the visual -> no duplicate advisory.
    fact = {"kind": "chart_continuous_fill", "status": "emitted", "default_palette": True}
    recs = [_visual_rec("v1", "Sheet A", chart_continuous_fill=fact)]
    warns = [_warn("worksheet", "Sheet A",
                   "background colour scale used Tableau's default continuous palette; applied a "
                   "default diverging gradient")]
    wl = build_worklist(warns, recs)
    color_items = [it for it in wl["items"] if it["category"] == "color_scale"]
    assert len(color_items) == 1
    assert color_items[0]["source"] == "warning"


def test_low_confidence_emits_chart_type_advisory():
    recs = [_visual_rec("v1", "Sheet A", confidence=0.2)]
    wl = build_worklist([], recs)
    adv = [it for it in wl["items"] if it["source"] == "advisory" and it["category"] == "chart_type"]
    assert len(adv) == 1
    assert adv[0]["severity"] == "low"


def test_high_confidence_no_advisory():
    recs = [_visual_rec("v1", "Sheet A", confidence=0.95)]
    wl = build_worklist([], recs)
    assert not any(it["source"] == "advisory" for it in wl["items"])


# -- full-dashboard coverage ---------------------------------------------------
def test_every_visual_is_enumerated_ok_or_flagged():
    recs = [
        _visual_rec("v1", "Sheet A"),          # will be flagged
        _visual_rec("v2", "Sheet B"),          # clean
    ]
    warns = [_warn("worksheet", "Sheet A", "field 'X' bound by caption fallback")]
    wl = build_worklist(warns, recs)
    by_visual = {v["visual"]: v for v in wl["visuals"]}
    assert set(by_visual) == {"v1", "v2"}
    assert by_visual["v1"]["status"] == "needs_attention"
    assert by_visual["v1"]["item_ids"]
    assert by_visual["v2"]["status"] == "ok"
    assert by_visual["v2"]["item_ids"] == []
    assert wl["summary"]["visuals_flagged"] == 1
    assert wl["summary"]["visuals_clean"] == 1


def test_parameter_control_records_are_not_counted_as_visuals():
    recs = [
        _visual_rec("v1", "Sheet A"),
        {"param_id": "[Parameter 1]", "caption": "Metric", "dashboard": "Dash", "position": {}},
    ]
    wl = build_worklist([], recs)
    assert wl["summary"]["visuals_total"] == 1
    assert all(v["visual"] == "v1" for v in wl["visuals"])


# -- classification + robustness -----------------------------------------------
def test_unknown_warning_falls_back_to_other_medium():
    recs = [_visual_rec("v1", "Sheet A")]
    warns = [_warn("worksheet", "Sheet A", "some brand new situation nobody classified yet")]
    wl = build_worklist(warns, recs)
    item = next(it for it in wl["items"] if "brand new" in it["reason"])
    assert item["category"] == "other"
    assert item["severity"] == "medium"
    assert item["remediation"]  # always carries an imperative hint


def test_items_sorted_blocking_first():
    recs = [_visual_rec("v1", "Sheet A")]
    warns = [
        _warn("worksheet", "Sheet A", "field 'X' bound by caption fallback"),           # high
        _warn("worksheet", "Sheet A", "dynamic title (embeds a field reference) not reproduced"),  # medium
        _warn("worksheet", "Sheet A", "mark class 'Shape' not supported -> no visual emitted"),     # blocking
    ]
    wl = build_worklist(warns, recs)
    sevs = [it["severity"] for it in wl["items"]]
    ranks = [SEVERITY_RANK[s] for s in sevs]
    assert ranks == sorted(ranks)
    assert sevs[0] == "blocking"


def test_summary_counts_match_items():
    recs = [_visual_rec("v1", "Sheet A"), _visual_rec("v2", "Sheet B")]
    warns = [
        _warn("worksheet", "Sheet A", "field 'X' bound by caption fallback"),
        _warn("worksheet", "Sheet B", "background colour scale used Tableau's default continuous palette"),
    ]
    wl = build_worklist(warns, recs)
    s = wl["summary"]
    assert s["items_total"] == len(wl["items"])
    assert sum(s["by_severity"].values()) == len(wl["items"])
    assert sum(s["by_category"].values()) == len(wl["items"])


def test_inputs_are_not_mutated():
    fact = {"kind": "legend", "status": "deferred"}
    recs = [_visual_rec("v1", "Sheet A", legend=fact)]
    warns = [_warn("worksheet", "Sheet A", "field 'X' bound by caption fallback")]
    recs_before = copy.deepcopy(recs)
    warns_before = copy.deepcopy(warns)
    build_worklist(warns, recs)
    assert recs == recs_before
    assert warns == warns_before


def test_item_ids_are_unique_and_stable():
    recs = [_visual_rec("v1", "Sheet A")]
    warns = [_warn("worksheet", "Sheet A", "field 'X' bound by caption fallback")]
    wl = build_worklist(warns, recs)
    ids = [it["id"] for it in wl["items"]]
    assert len(ids) == len(set(ids))
    # coverage item_ids reference real items
    all_ids = set(ids)
    for v in wl["visuals"]:
        assert set(v["item_ids"]) <= all_ids
