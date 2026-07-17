"""Tests for the monotonic fidelity gate (``monotonic_gate``).

The gate makes opting into the assisted tier provably ``>=`` the deterministic tier per visual. These
tests pin the contract: the pure :func:`decide` core keeps an improvement, reverts a regression, keeps
a tie (so an unmeasured gain can land), and reverts a mixed/overall-lowering change; the feature scorer
reads the emitted PBIR object shapes; :func:`gate_visual` returns the deterministic object by identity
on a revert and never mutates inputs; and the batch monotonic property holds by construction. Synthetic
(engine-independent) fixtures with an injected structural scorer keep the suite fast and offline.
"""
import copy

from monotonic_gate import (
    decide,
    gate_visual,
    gate_changes,
    combined_score,
    visual_feature_components,
    verify_monotonic,
    GATE_VERSION,
    GATE_KIND,
    DEFAULT_EPSILON,
)


def _cs(comp, score=None):
    """A combined-score dict like :func:`combined_score` returns."""
    return {"score": score if score is not None else (sum(comp.values()) / len(comp) if comp else 0.0),
            "components": dict(comp)}


# A deterministic stub structural scorer so gate_visual tests never need the real oracle.
def _stub_scorer(components):
    def _score(ws, visual, zone, relationships=None):
        return {"components": dict(components)}
    return _score


# -- feature scoring ----------------------------------------------------------
def _rich_visual():
    return {
        "name": "v-rich",
        "objects": {
            "dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": {"FillRule": {
                "Input": {}, "FillRule": {"linearGradient3": {}}}}}}}}}],
            "labels": [{"properties": {}}],
            "legend": [{"properties": {}}],
        },
        "visualContainerObjects": {"title": [{"properties": {}}]},
    }


def test_feature_components_all_present():
    feats = visual_feature_components(_rich_visual())
    assert feats == {
        "color_fill": 1.0, "data_point": 1.0, "data_labels": 1.0, "legend": 1.0, "title": 1.0,
    }


def test_feature_components_none_present():
    feats = visual_feature_components({"name": "bare", "objects": {}})
    assert feats == {
        "color_fill": 0.0, "data_point": 0.0, "data_labels": 0.0, "legend": 0.0, "title": 0.0,
    }


def test_feature_color_fill_grades_by_gradient_richness():
    def vis(token):
        return {"objects": {"dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": {
            "FillRule": {"Input": {}, "FillRule": {token: {}}}}}}}}}]}}
    assert visual_feature_components(vis("linearGradient3"))["color_fill"] == 1.0
    assert visual_feature_components(vis("linearGradient2"))["color_fill"] == 0.7
    # A FillRule with no recognised gradient token still registers as present (a solid conditional fill).
    solid = {"objects": {"values": [{"properties": {"backColor": {"solid": {"color": {"expr": {
        "FillRule": {"Input": {}, "FillRule": {"solid": {}}}}}}}}}]}}
    assert visual_feature_components(solid)["color_fill"] == 0.4


def test_feature_components_tolerate_non_dict():
    assert visual_feature_components(None)["color_fill"] == 0.0
    assert visual_feature_components([])["legend"] == 0.0


# -- pure decide core ---------------------------------------------------------
def test_decide_keeps_pure_improvement():
    before = _cs({"feat_color_fill": 0.0, "feat_legend": 1.0})
    after = _cs({"feat_color_fill": 1.0, "feat_legend": 1.0})
    d = decide(before, after)
    assert d["chosen"] == "assisted"
    assert d["kept_assisted"] is True
    assert d["improved_components"] == ["feat_color_fill"]
    assert d["regressed_components"] == []


def test_decide_reverts_regression():
    before = _cs({"feat_color_fill": 1.0, "feat_legend": 1.0})
    after = _cs({"feat_color_fill": 1.0, "feat_legend": 0.0})
    d = decide(before, after)
    assert d["chosen"] == "deterministic"
    assert d["kept_assisted"] is False
    assert d["regressed_components"] == ["feat_legend"]


def test_decide_reverts_mixed_change():
    # improves color_fill but regresses legend -> conservative revert (no trading axes).
    before = _cs({"feat_color_fill": 0.0, "feat_legend": 1.0})
    after = _cs({"feat_color_fill": 1.0, "feat_legend": 0.0})
    d = decide(before, after)
    assert d["chosen"] == "deterministic"
    assert d["regressed_components"] == ["feat_legend"]
    assert d["improved_components"] == ["feat_color_fill"]


def test_decide_keeps_tie_to_allow_unmeasured_gain():
    before = _cs({"struct_type": 1.0, "struct_fields": 1.0})
    after = _cs({"struct_type": 1.0, "struct_fields": 1.0})
    d = decide(before, after)
    assert d["chosen"] == "assisted"
    assert d["improved_components"] == []
    assert "unmeasured" in d["reason"]


def test_decide_reverts_dropped_component():
    # The candidate stops scoring an axis the baseline scored -> treated as a regression.
    before = _cs({"struct_type": 1.0, "struct_position": 0.9})
    after = _cs({"struct_type": 1.0})
    d = decide(before, after)
    assert d["chosen"] == "deterministic"
    assert d["dropped_components"] == ["struct_position"]


def test_decide_reports_added_component_but_still_keeps():
    before = _cs({"feat_color_fill": 0.0})
    after = _cs({"feat_color_fill": 0.0, "feat_legend": 1.0})
    d = decide(before, after)
    assert d["chosen"] == "assisted"
    assert d["added_components"] == ["feat_legend"]


def test_decide_epsilon_absorbs_float_jitter():
    before = _cs({"feat_color_fill": 0.5000000})
    after = _cs({"feat_color_fill": 0.5000000 - (DEFAULT_EPSILON / 2)})
    d = decide(before, after)
    # within epsilon -> not a regression -> kept.
    assert d["chosen"] == "assisted"
    assert d["regressed_components"] == []


def test_decide_reverts_when_overall_drops_even_without_named_regression():
    # No shared component regresses, but the candidate's overall score is lower -> revert.
    before = {"score": 0.9, "components": {"feat_color_fill": 1.0}}
    after = {"score": 0.5, "components": {"feat_color_fill": 1.0}}
    d = decide(before, after)
    assert d["chosen"] == "deterministic"


# -- combined_score -----------------------------------------------------------
def test_combined_score_namespaces_and_flags_structural():
    ws = {"name": "Sheet 1"}
    visual = _rich_visual()
    cs = combined_score(ws, visual, structural_scorer=_stub_scorer({"type": 1.0, "fields": 0.8}))
    assert cs["structural_scored"] is True
    assert cs["components"]["struct_type"] == 1.0
    assert cs["components"]["struct_fields"] == 0.8
    assert cs["components"]["feat_color_fill"] == 1.0


def test_combined_score_feature_only_when_no_structural():
    cs = combined_score(None, _rich_visual(), structural_scorer=None)
    assert cs["structural_scored"] is False
    assert all(k.startswith("feat_") for k in cs["components"])


# -- gate_visual --------------------------------------------------------------
def test_gate_visual_keeps_assisted_on_color_uplift():
    ws = {"name": "Sheet 1"}
    det = {"name": "v", "objects": {}}
    asst = _rich_visual()
    stub = _stub_scorer({"type": 1.0, "fields": 1.0})
    chosen, dec = gate_visual(ws, det, asst, structural_scorer=stub)
    assert chosen is asst
    assert dec["kept_assisted"] is True
    assert dec["worksheet"] == "Sheet 1"
    assert "feat_color_fill" in dec["improved_components"]


def test_gate_visual_reverts_returns_deterministic_by_identity():
    ws = {"name": "Sheet 1"}
    det = _rich_visual()
    asst = {"name": "stripped", "objects": {}}  # strips every fidelity feature
    stub = _stub_scorer({"type": 1.0, "fields": 1.0})
    chosen, dec = gate_visual(ws, det, asst, structural_scorer=stub)
    assert chosen is det
    assert dec["kept_assisted"] is False
    assert dec["regressed_components"]  # non-empty


def test_gate_visual_does_not_mutate_inputs():
    ws = {"name": "Sheet 1"}
    det = _rich_visual()
    asst = {"name": "stripped", "objects": {}}
    det_before = copy.deepcopy(det)
    asst_before = copy.deepcopy(asst)
    gate_visual(ws, det, asst, structural_scorer=_stub_scorer({"type": 1.0}))
    assert det == det_before
    assert asst == asst_before


def test_gate_visual_reports_structural_availability():
    ws = {"name": "Sheet 1"}
    chosen, dec = gate_visual(ws, {"objects": {}}, _rich_visual(), structural_scorer=None)
    assert dec["structural_scored"] is False


# -- batch gate_changes + monotonic property ----------------------------------
def test_gate_changes_summary_and_chosen_set():
    ws = {"name": "S"}
    det_bad = {"objects": {}}
    asst_good = _rich_visual()
    det_good = _rich_visual()
    asst_bad = {"objects": {}}
    stub = _stub_scorer({"type": 1.0})
    pairs = [
        (ws, det_bad, asst_good),   # uplift -> keep assisted
        (ws, det_good, asst_bad),   # strip  -> revert
    ]
    rec = gate_changes(pairs, structural_scorer=stub)
    assert rec["version"] == GATE_VERSION
    assert rec["kind"] == GATE_KIND
    assert rec["summary"] == {"visuals": 2, "assisted_kept": 1, "reverted": 1}
    assert rec["visuals"][0] is asst_good
    assert rec["visuals"][1] is det_good


def test_monotonic_property_holds_across_random_pairs():
    # For every decision, the chosen candidate must be >= the deterministic baseline on every
    # scored component. This is the gate's whole reason to exist.
    stub = _stub_scorer({"type": 1.0, "fields": 1.0})
    cases = [
        ({"objects": {}}, _rich_visual()),
        (_rich_visual(), {"objects": {}}),
        (_rich_visual(), _rich_visual()),
        ({"objects": {"legend": [{"properties": {}}]}}, {"objects": {"labels": [{"properties": {}}]}}),
    ]
    ws = {"name": "S"}
    for det, asst in cases:
        before = combined_score(ws, det, structural_scorer=stub)
        after = combined_score(ws, asst, structural_scorer=stub)
        d = decide(before, after)
        assert verify_monotonic(before, after, d) is True


def test_gate_changes_accepts_short_tuples():
    # (ws, det, asst) with no zone should work.
    ws = {"name": "S"}
    rec = gate_changes([(ws, {"objects": {}}, _rich_visual())], structural_scorer=_stub_scorer({"type": 1.0}))
    assert rec["summary"]["visuals"] == 1
