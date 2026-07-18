"""Tests for the Tier-3 dashboard audit tier (``audit_tier``).

The audit tier folds the remediation worklist (coverage + priority) and the viz advisor (chart
alternatives) into ONE full-dashboard audit request, then lands an agent's proposals only through the
monotonic gate. These tests pin the contract: every visual is audited (not just flagged ones), priority
is the max item severity and orders the list, alternatives surface for advisable visuals and a reason
for the rest, dashboard-scope items are never dropped, the runbook prompt carries the rules + the
monotonic reminder, and landing keeps an uplift / reverts a regression while guaranteeing >= deterministic.
Synthetic (engine-independent) fixtures with an injected structural scorer keep the suite fast and offline.
"""
import copy

from audit_tier import (
    build_dashboard_audit,
    audit_prompt,
    land_dashboard_audit,
    AUDIT_VERSION,
    AUDIT_REQUEST_KIND,
    AUDIT_RESULT_KIND,
    AUDIT_RULES,
)


# -- synthetic worklist fixtures ----------------------------------------------
def _wl(visuals, items):
    return {"version": 1, "kind": "tableau-fabric-remediation-worklist",
            "summary": {}, "visuals": visuals, "items": items}


def _v(visual, worksheet, status, item_ids, vtype="clusteredBarChart", page="Dash", confidence=0.9):
    return {"visual": visual, "worksheet": worksheet, "page": page, "page_display": page,
            "visual_type": vtype, "confidence": confidence, "status": status, "item_ids": item_ids}


def _it(item_id, severity, reason="something to fix", category="other"):
    return {"id": item_id, "severity": severity, "category": category, "reason": reason,
            "remediation": "Fix it.", "source": "warning", "visual": None, "worksheet": None}


# -- producer: coverage -------------------------------------------------------
def test_audit_covers_every_visual_not_just_flagged():
    wl = _wl(
        visuals=[
            _v("v1", "S1", "needs_attention", ["i1"]),
            _v("v2", "S2", "ok", []),
            _v("v3", "S3", "ok", []),
        ],
        items=[_it("i1", "high")],
    )
    bundle = build_dashboard_audit(wl, candidate_records=[])
    assert bundle["version"] == AUDIT_VERSION
    assert bundle["kind"] == AUDIT_REQUEST_KIND
    assert bundle["summary"]["visuals"] == 3
    assert bundle["summary"]["needs_attention"] == 1
    audited = {(e["worksheet"], e["visual"]) for e in bundle["visuals"]}
    assert audited == {("S1", "v1"), ("S2", "v2"), ("S3", "v3")}


def test_priority_is_max_item_severity():
    wl = _wl(
        visuals=[_v("v1", "S1", "needs_attention", ["lo", "hi"])],
        items=[_it("lo", "low"), _it("hi", "blocking")],
    )
    bundle = build_dashboard_audit(wl, candidate_records=[])
    assert bundle["visuals"][0]["priority"] == "blocking"


def test_visuals_sorted_highest_priority_first():
    wl = _wl(
        visuals=[
            _v("clean", "S3", "ok", []),
            _v("med", "S2", "needs_attention", ["m"]),
            _v("block", "S1", "needs_attention", ["b"]),
        ],
        items=[_it("m", "medium"), _it("b", "blocking")],
    )
    bundle = build_dashboard_audit(wl, candidate_records=[])
    order = [e["visual"] for e in bundle["visuals"]]
    assert order == ["block", "med", "clean"]


def test_clean_visual_has_priority_none():
    wl = _wl(visuals=[_v("v1", "S1", "ok", [])], items=[])
    bundle = build_dashboard_audit(wl, candidate_records=[])
    assert bundle["visuals"][0]["priority"] == "none"
    assert bundle["visuals"][0]["items"] == []


# -- producer: advisor alternatives -------------------------------------------
def _geo_record(visual="v1", worksheet="S1"):
    # A geographic dimension + a measure -> the advisor is advisable and offers map alternatives.
    return {"visual": visual, "worksheet": worksheet, "page": "Dash",
            "visual_type": "clusteredBarChart", "confidence": 0.9,
            "fields": {"Category": ["[Region]"], "Y": ["[sum:Sales]"]}, "position": {}}


def test_advisable_visual_carries_alternatives():
    wl = _wl(visuals=[_v("v1", "S1", "needs_attention", ["i1"])], items=[_it("i1", "medium")])
    bundle = build_dashboard_audit(wl, candidate_records=[_geo_record()])
    entry = bundle["visuals"][0]
    assert entry["advisable"] is True
    assert entry["alternatives"]  # non-empty ranked suggestions
    assert all("visual_type" in s for s in entry["alternatives"])
    assert "fields" in entry


def test_non_advisable_visual_carries_reason_not_alternatives():
    # A detail table is not re-rankable -> advisable False with a reason, still audited.
    wl = _wl(visuals=[_v("t1", "S1", "ok", [], vtype="tableEx")], items=[])
    rec = {"visual": "t1", "worksheet": "S1", "page": "Dash", "visual_type": "tableEx",
           "confidence": 0.95, "fields": {}, "position": {}}
    bundle = build_dashboard_audit(wl, candidate_records=[rec])
    entry = bundle["visuals"][0]
    assert entry["advisable"] is False
    assert "alternatives" not in entry
    assert entry.get("advice_reason")


def test_build_without_records_still_audits_on_worklist_alone():
    wl = _wl(visuals=[_v("v1", "S1", "needs_attention", ["i1"])], items=[_it("i1", "high")])
    bundle = build_dashboard_audit(wl, candidate_records=None)
    entry = bundle["visuals"][0]
    assert entry["advisable"] is False
    assert entry["priority"] == "high"
    assert entry["items"][0]["reason"] == "something to fix"


# -- producer: unattached items + non-mutation --------------------------------
def test_unattached_items_surface_and_are_not_dropped():
    wl = _wl(
        visuals=[_v("v1", "S1", "needs_attention", ["i1"])],
        items=[_it("i1", "high"), _it("dash", "medium", reason="a dashboard-scope gap")],
    )
    bundle = build_dashboard_audit(wl, candidate_records=[])
    assert bundle["summary"]["unattached_items"] == 1
    assert bundle["unattached_items"][0]["reason"] == "a dashboard-scope gap"


def test_build_does_not_mutate_inputs():
    wl = _wl(visuals=[_v("v1", "S1", "needs_attention", ["i1"])], items=[_it("i1", "high")])
    recs = [_geo_record()]
    wl_before = copy.deepcopy(wl)
    recs_before = copy.deepcopy(recs)
    build_dashboard_audit(wl, candidate_records=recs)
    assert wl == wl_before
    assert recs == recs_before


# -- prompt -------------------------------------------------------------------
def test_prompt_contains_rules_and_monotonic_reminder():
    wl = _wl(visuals=[_v("v1", "S1", "needs_attention", ["i1"])], items=[_it("i1", "high")])
    text = audit_prompt(build_dashboard_audit(wl, candidate_records=[]))
    for rule in AUDIT_RULES:
        assert rule in text
    assert "monotonic-gated" in text
    assert "EVERY" in text or "whole dashboard" in text


def test_prompt_lists_every_visual():
    wl = _wl(
        visuals=[_v("v1", "S1", "needs_attention", ["i1"]), _v("v2", "S2", "ok", [])],
        items=[_it("i1", "high")],
    )
    text = audit_prompt(build_dashboard_audit(wl, candidate_records=[]))
    assert "'v1'" in text
    assert "'v2'" in text


# -- landing ------------------------------------------------------------------
def _stub_scorer(components):
    def _score(ws, visual, zone, relationships=None):
        return {"components": dict(components)}
    return _score


def _rich(name="v"):
    return {"name": name, "objects": {
        "dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": {"FillRule": {
            "Input": {}, "FillRule": {"linearGradient3": {}}}}}}}}}],
        "legend": [{"properties": {}}],
    }}


def _bare(name="v"):
    return {"name": name, "objects": {}}


def test_land_keeps_uplift_and_reverts_strip():
    stub = _stub_scorer({"type": 1.0, "fields": 1.0})
    pairs = [
        ({"name": "S1"}, _bare("v1"), _rich("v1")),           # uplift -> keep
        ({"name": "S2"}, _rich("v2"), _bare("v2")),           # strip  -> revert
    ]
    res = land_dashboard_audit(pairs, structural_scorer=stub)
    assert res["kind"] == AUDIT_RESULT_KIND
    assert res["summary"]["assisted_kept"] == 1
    assert res["summary"]["reverted"] == 1
    assert res["visuals"][0] is pairs[0][2]   # assisted rich kept
    assert res["visuals"][1] is pairs[1][1]   # deterministic rich kept


def test_land_guarantee_string_present():
    res = land_dashboard_audit([({"name": "S1"}, _bare("v1"), _rich("v1"))],
                               structural_scorer=_stub_scorer({"type": 1.0}))
    assert "deterministic baseline" in res["summary"]["guarantee"]


def test_land_annotates_priority_and_status_from_audit():
    wl = _wl(visuals=[_v("v1", "S1", "needs_attention", ["i1"])], items=[_it("i1", "high")])
    bundle = build_dashboard_audit(wl, candidate_records=[])
    pairs = [({"name": "S1"}, _bare("v1"), _rich("v1"))]
    res = land_dashboard_audit(pairs, audit=bundle, structural_scorer=_stub_scorer({"type": 1.0}))
    d = res["decisions"][0]
    assert d["priority"] == "high"
    assert d["status"] == "needs_attention"


def test_land_counts_flagged_improved():
    wl = _wl(
        visuals=[_v("v1", "S1", "needs_attention", ["i1"]), _v("v2", "S2", "needs_attention", ["i2"])],
        items=[_it("i1", "high"), _it("i2", "high")],
    )
    bundle = build_dashboard_audit(wl, candidate_records=[])
    stub = _stub_scorer({"type": 1.0})
    pairs = [
        ({"name": "S1"}, _bare("v1"), _rich("v1")),   # flagged, improved
        ({"name": "S2"}, _rich("v2"), _bare("v2")),   # flagged, reverted (no improvement)
    ]
    res = land_dashboard_audit(pairs, audit=bundle, structural_scorer=stub)
    assert res["summary"]["flagged_visuals"] == 2
    assert res["summary"]["flagged_improved"] == 1


def test_land_monotonic_property_holds_for_every_decision():
    import monotonic_gate as g
    stub = _stub_scorer({"type": 1.0, "fields": 1.0})
    pairs = [
        ({"name": "S1"}, _bare("v1"), _rich("v1")),
        ({"name": "S2"}, _rich("v2"), _bare("v2")),
        ({"name": "S3"}, _rich("v3"), _rich("v3")),
    ]
    res = land_dashboard_audit(pairs, structural_scorer=stub)
    for (ws, det, asst), d in zip(pairs, res["decisions"]):
        before = g.combined_score(ws, det, structural_scorer=stub)
        after = g.combined_score(ws, asst, structural_scorer=stub)
        assert g.verify_monotonic(before, after, d) is True


def test_land_does_not_mutate_inputs():
    det = _rich("v1")
    asst = _bare("v1")
    det_before = copy.deepcopy(det)
    asst_before = copy.deepcopy(asst)
    land_dashboard_audit([({"name": "S1"}, det, asst)], structural_scorer=_stub_scorer({"type": 1.0}))
    assert det == det_before
    assert asst == asst_before


def test_land_accepts_short_tuples():
    res = land_dashboard_audit([({"name": "S1"}, _bare("v1"), _rich("v1"))],
                               structural_scorer=_stub_scorer({"type": 1.0}))
    assert res["summary"]["visuals"] == 1


# -- integration through the real worklist builder ----------------------------
def test_end_to_end_from_build_worklist():
    from remediation_worklist import build_worklist
    warns = [{"scope": "worksheet", "name": "Sheet 1",
              "reason": "manual attention required: colour scale uses a default palette"}]
    recs = [_geo_record(visual="v1", worksheet="Sheet 1"),
            {"visual": "v2", "worksheet": "Sheet 2", "page": "Dash", "visual_type": "tableEx",
             "confidence": 0.95, "fields": {}, "position": {}}]
    wl = build_worklist(warns, recs)
    bundle = build_dashboard_audit(wl, candidate_records=recs)
    assert bundle["summary"]["visuals"] == 2
    # The flagged geographic visual is advisable and audited first.
    assert bundle["visuals"][0]["worksheet"] == "Sheet 1"
    assert bundle["visuals"][0]["advisable"] is True
