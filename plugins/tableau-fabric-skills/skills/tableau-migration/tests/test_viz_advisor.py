"""Tier-2 viz-advisor tests (offline; deterministic; no model / LLM call).

These pin the deterministic recommender (data semantics -> ranked candidate charts), the additive
out-of-band advice bundle / prompt, and the gated applier (a chart lands only when its type is in
the candidate set and every field binds to a role-compatible slot -- otherwise the deterministic top
pick stands). Mirrors the calc compiler's Tier-2 assisted loop + the image-oracle harness.
"""
import json

import pytest

from viz_advisor import (
    ADVICE_SIDECAR_KIND,
    ADVISOR_BUNDLE_KIND,
    ADVISOR_RULES,
    PBIR_VISUAL_TYPES,
    VizAdvisorError,
    advice_prompt,
    advise_for_candidate_record,
    apply_advice,
    build_advice_bundle,
    build_report_advice,
    classify_fields,
    fields_from_candidate_record,
    normalize_field,
    recommend_visuals,
    refine_with_feedback,
    validate_suggestion,
)


def _f(name, role, data_type=None, semantic_role=None, cardinality=None):
    return {"name": name, "role": role, "data_type": data_type,
            "semantic_role": semantic_role, "cardinality": cardinality}


def _top(fields, **kw):
    recs = recommend_visuals(fields, **kw)
    return recs[0]["visual_type"]


def _types(recs):
    return [r["visual_type"] for r in recs]


# -- normalize_field -----------------------------------------------------------

def test_normalize_field_derives_temporal_from_date_type():
    nf = normalize_field(_f("Order Date", "dimension", "date"))
    assert nf["semantic_role"] == "temporal"


def test_normalize_field_derives_geo_from_name():
    nf = normalize_field(_f("State/Province", "dimension", "string"))
    assert nf["semantic_role"] == "geo"


def test_normalize_field_explicit_semantic_role_wins():
    nf = normalize_field(_f("Segment", "dimension", "string", semantic_role="geo"))
    assert nf["semantic_role"] == "geo"


# Regression: geo detection matches whole word tokens, not raw substrings. A substring test wrongly
# flagged these common non-geographic dimensions as geographic ("city" in Ethnicity/Capacity, "lat"
# in Relationship/Inflation/Translation, "state" in Real Estate, "geo" in Geometry) and would route
# them to a confident, wrong map -- a warn-never-wrong violation.
@pytest.mark.parametrize("name", [
    "Ethnicity", "Capacity", "Relationship", "Inflation", "Translation",
    "Plate", "Real Estate", "Statement", "Geometry", "Allocation", "Velocity",
])
def test_geo_substring_in_nongeo_name_is_not_geographic(name):
    assert normalize_field(_f(name, "dimension", "string"))["semantic_role"] is None


@pytest.mark.parametrize("name", [
    "Country", "State/Province", "Postal Code", "City", "Region", "County",
    "Latitude", "Longitude", "Zip", "Geo", "Geography",
    "CustomerCity", "ship_city", "Cities", "Countries", "ZIPCode", "Region2024",
])
def test_real_geo_names_classify_as_geo(name):
    assert normalize_field(_f(name, "dimension", "string"))["semantic_role"] == "geo"


def test_nongeo_dimension_is_not_routed_to_a_map():
    # Ethnicity embeds "city" but is not geographic; the advisor must not propose a map for it.
    recs = recommend_visuals([_f("Ethnicity", "dimension", "string"),
                              _f("Population", "measure", "integer")])
    types = _types(recs)
    assert "shapeMap" not in types and "map" not in types
    assert recs[0]["visual_type"] == "clusteredColumnChart"


def test_normalize_field_missing_name_raises():
    with pytest.raises(VizAdvisorError):
        normalize_field({"role": "measure"})


def test_normalize_field_bad_role_raises():
    with pytest.raises(VizAdvisorError):
        normalize_field(_f("Sales", "metric"))


def test_classify_fields_counts_roles_and_semantics():
    c = classify_fields([
        _f("Sales", "measure", "double"),
        _f("Order Date", "dimension", "date"),
        _f("Country", "dimension", "string"),
        _f("Segment", "dimension", "string"),
    ])
    assert c["n_measures"] == 1
    assert c["n_dimensions"] == 3
    assert [f["name"] for f in c["temporal"]] == ["Order Date"]
    assert [f["name"] for f in c["geo"]] == ["Country"]
    assert [f["name"] for f in c["plain_dimensions"]] == ["Segment"]


# -- recommend_visuals (deterministic ranking) ---------------------------------

def test_single_measure_no_dimension_is_a_card():
    assert _top([_f("Sales", "measure", "double")]) == "card"


def test_multiple_measures_no_dimension_is_multi_row_card():
    recs = recommend_visuals([_f("Sales", "measure", "double"),
                              _f("Profit", "measure", "double"),
                              _f("Quantity", "measure", "integer")])
    assert recs[0]["visual_type"] == "multiRowCard"


def test_temporal_dimension_and_measure_is_a_line():
    recs = recommend_visuals([_f("Order Date", "dimension", "date"),
                              _f("Sales", "measure", "double")])
    assert recs[0]["visual_type"] == "lineChart"
    assert "areaChart" in _types(recs)


def test_geo_dimension_and_measure_is_a_shape_map():
    recs = recommend_visuals([_f("Country", "dimension", "string"),
                              _f("Sales", "measure", "double")])
    assert recs[0]["visual_type"] == "shapeMap"
    assert "map" in _types(recs)


def test_low_card_category_and_measure_offers_column_and_pie():
    recs = recommend_visuals([_f("Segment", "dimension", "string", cardinality="low"),
                              _f("Sales", "measure", "double")])
    assert recs[0]["visual_type"] == "clusteredColumnChart"
    assert "clusteredBarChart" in _types(recs)
    assert "pieChart" in _types(recs)


def test_high_card_category_and_measure_has_no_pie():
    recs = recommend_visuals([_f("Customer Name", "dimension", "string", cardinality="high"),
                              _f("Sales", "measure", "double")])
    assert recs[0]["visual_type"] == "clusteredColumnChart"
    assert "pieChart" not in _types(recs)


def test_two_dimensions_one_measure_is_a_matrix():
    recs = recommend_visuals([_f("Segment", "dimension", "string"),
                              _f("Ship Mode", "dimension", "string"),
                              _f("Sales", "measure", "double")])
    assert recs[0]["visual_type"] == "pivotTable"
    assert "stackedColumnChart" in _types(recs)


def test_two_measures_offer_scatter():
    recs = recommend_visuals([_f("Sales", "measure", "double"),
                              _f("Profit", "measure", "double"),
                              _f("Category", "dimension", "string")])
    assert "scatterChart" in _types(recs)


def test_table_is_always_the_fallback():
    recs = recommend_visuals([_f("Sales", "measure", "double")])
    assert "tableEx" in _types(recs)


def test_no_fields_yields_no_recommendations():
    assert recommend_visuals([]) == []


def test_recommendations_are_ranked_and_capped():
    recs = recommend_visuals([_f("Segment", "dimension", "string", cardinality="low"),
                              _f("Sales", "measure", "double")], max_suggestions=2)
    assert len(recs) == 2
    assert [r["rank"] for r in recs] == [1, 2]
    confs = [r["confidence"] for r in recs]
    assert confs == sorted(confs, reverse=True)


def test_every_recommended_type_is_a_known_pbir_type():
    recs = recommend_visuals([_f("Region", "dimension", "string"),
                              _f("Segment", "dimension", "string"),
                              _f("Sales", "measure", "double")])
    for r in recs:
        assert r["visual_type"] in PBIR_VISUAL_TYPES


def test_every_recommendation_carries_reasoning():
    recs = recommend_visuals([_f("Order Date", "dimension", "date"),
                              _f("Sales", "measure", "double")])
    assert all(r["reasoning"].strip() for r in recs)


# -- bundle + prompt -----------------------------------------------------------

def test_build_advice_bundle_shape():
    fields = [_f("Segment", "dimension", "string", cardinality="low"),
              _f("Sales", "measure", "double")]
    b = build_advice_bundle(fields, intent="compare segments")
    assert b["kind"] == ADVISOR_BUNDLE_KIND
    assert b["rules"] == list(ADVISOR_RULES)
    assert b["intent"] == "compare segments"
    assert b["summary"]["top_pick"] == "clusteredColumnChart"
    assert b["summary"]["candidates"] == len(b["candidates"])
    assert b["answer"] == {"chosen_index": None, "reason": ""}


def test_build_advice_bundle_does_not_mutate_fields():
    fields = [_f("Sales", "measure", "double")]
    before = json.dumps(fields, sort_keys=True)
    build_advice_bundle(fields)
    assert json.dumps(fields, sort_keys=True) == before


def test_advice_prompt_lists_rules_fields_and_candidates():
    b = build_advice_bundle([_f("Country", "dimension", "string"),
                             _f("Sales", "measure", "double")], intent="where are sales")
    prompt = advice_prompt(b)
    assert ADVISOR_RULES[0] in prompt
    assert "where are sales" in prompt
    assert "Country" in prompt and "Sales" in prompt
    assert "shapeMap" in prompt
    assert "[0]" in prompt


# -- validate_suggestion (the gate) --------------------------------------------

def _candidates():
    return recommend_visuals([_f("Segment", "dimension", "string", cardinality="low"),
                              _f("Sales", "measure", "double")])


def test_validate_accepts_a_real_candidate():
    fields = [_f("Segment", "dimension", "string", cardinality="low"),
              _f("Sales", "measure", "double")]
    cands = _candidates()
    ok, why = validate_suggestion(cands[0], fields, cands)
    assert ok, why


def test_validate_rejects_a_non_candidate_type():
    fields = [_f("Segment", "dimension", "string"), _f("Sales", "measure", "double")]
    cands = _candidates()
    bogus = {"visual_type": "waterfallChart", "encodings": {"Category": ["Segment"], "Y": ["Sales"]}}
    ok, why = validate_suggestion(bogus, fields, cands)
    assert not ok
    assert "candidate set" in why


def test_validate_rejects_unknown_field():
    fields = [_f("Segment", "dimension", "string"), _f("Sales", "measure", "double")]
    cands = _candidates()
    bad = {"visual_type": "clusteredColumnChart",
           "encodings": {"Category": ["Segment"], "Y": ["Nonexistent"]}}
    ok, why = validate_suggestion(bad, fields, cands)
    assert not ok
    assert "not in the provided field set" in why


def test_validate_rejects_measure_on_a_dimension_slot():
    fields = [_f("Segment", "dimension", "string"), _f("Sales", "measure", "double")]
    cands = _candidates()
    bad = {"visual_type": "clusteredColumnChart",
           "encodings": {"Category": ["Sales"], "Y": ["Sales"]}}
    ok, why = validate_suggestion(bad, fields, cands)
    assert not ok
    assert "not a dimension" in why


def test_validate_rejects_dimension_on_a_measure_slot():
    fields = [_f("Segment", "dimension", "string"), _f("Sales", "measure", "double")]
    cands = _candidates()
    bad = {"visual_type": "clusteredColumnChart",
           "encodings": {"Category": ["Segment"], "Y": ["Segment"]}}
    ok, why = validate_suggestion(bad, fields, cands)
    assert not ok
    assert "not a measure" in why


# -- apply_advice (gated landing) ----------------------------------------------

def test_apply_keeps_top_pick_when_chosen_index_null():
    fields = [_f("Segment", "dimension", "string", cardinality="low"),
              _f("Sales", "measure", "double")]
    cands = _candidates()
    chosen, report = apply_advice(cands, {"chosen_index": None, "reason": ""}, fields)
    assert chosen["visual_type"] == cands[0]["visual_type"]
    assert report["kept"] and not report["applied"]


def test_apply_lands_a_valid_non_top_candidate():
    fields = [_f("Segment", "dimension", "string", cardinality="low"),
              _f("Sales", "measure", "double")]
    cands = _candidates()
    bar_idx = _types(cands).index("clusteredBarChart")
    chosen, report = apply_advice(cands, {"chosen_index": bar_idx, "reason": "long labels"}, fields)
    assert chosen["visual_type"] == "clusteredBarChart"
    assert report["applied"][0]["to_type"] == "clusteredBarChart"


def test_apply_rejects_out_of_range_index_and_keeps_top():
    fields = [_f("Sales", "measure", "double")]
    cands = recommend_visuals(fields)
    chosen, report = apply_advice(cands, {"chosen_index": 99}, fields)
    assert chosen["visual_type"] == cands[0]["visual_type"]
    assert report["rejected"] and report["kept"]


def test_apply_rejects_an_invalid_candidate_spec_and_keeps_top():
    # A candidate whose binding is role-incompatible must NOT land even if selected by index.
    fields = [_f("Segment", "dimension", "string"), _f("Sales", "measure", "double")]
    good = {"visual_type": "clusteredColumnChart",
            "encodings": {"Category": ["Segment"], "Y": ["Sales"]}, "confidence": 0.8,
            "reasoning": "ok", "rank": 1}
    bad = {"visual_type": "clusteredBarChart",
           "encodings": {"Category": ["Sales"], "Y": ["Sales"]}, "confidence": 0.7,
           "reasoning": "bad", "rank": 2}
    chosen, report = apply_advice([good, bad], {"chosen_index": 1}, fields)
    assert chosen["visual_type"] == "clusteredColumnChart"
    assert report["rejected"][0]["rejected_because"]


def test_apply_with_no_candidates_returns_none():
    chosen, report = apply_advice([], {"chosen_index": None}, [])
    assert chosen is None
    assert report == {"applied": [], "kept": [], "rejected": []}


def test_apply_does_not_mutate_candidates():
    fields = [_f("Segment", "dimension", "string", cardinality="low"),
              _f("Sales", "measure", "double")]
    cands = _candidates()
    before = json.dumps(cands, sort_keys=True)
    apply_advice(cands, {"chosen_index": 1}, fields)
    assert json.dumps(cands, sort_keys=True) == before


# -- refine_with_feedback (multi-turn hook) ------------------------------------

def test_refine_promotes_an_up_voted_type():
    fields = [_f("Segment", "dimension", "string", cardinality="low"),
              _f("Sales", "measure", "double")]
    cands = _candidates()
    refined = refine_with_feedback(cands, {"pieChart": "up", "clusteredColumnChart": "down"})
    # pieChart (+0.15) overtakes the down-voted column (-0.25); ranks are recomputed.
    assert refined[0]["rank"] == 1
    pie = next(r for r in refined if r["visual_type"] == "pieChart")
    col = next(r for r in refined if r["visual_type"] == "clusteredColumnChart")
    assert pie["confidence"] > col["confidence"]


def test_refine_clamps_and_ignores_unknown_types():
    cands = [{"visual_type": "card", "encodings": {}, "confidence": 0.95, "reasoning": "", "rank": 1}]
    refined = refine_with_feedback(cands, {"card": "up", "nonexistentChart": "down"})
    assert refined[0]["confidence"] <= 1.0


def test_refine_does_not_mutate_input():
    cands = _candidates()
    before = json.dumps(cands, sort_keys=True)
    refine_with_feedback(cands, {"pieChart": "up"})
    assert json.dumps(cands, sort_keys=True) == before


# -- pipeline bridge: advice from candidate records ----------------------------

def _record(visual_type, fields, **kw):
    rec = {"page": "p1", "visual": "v1", "worksheet": "Sheet", "visual_type": visual_type,
           "fields": fields}
    rec.update(kw)
    return rec


def test_fields_from_record_infers_role_from_slot():
    fields, reason = fields_from_candidate_record(
        _record("clusteredColumnChart", {"Category": ["Orders.Segment"], "Y": ["Orders.Sales"]}))
    assert reason is None
    by = {f["name"]: f["role"] for f in fields}
    assert by == {"Segment": "dimension", "Sales": "measure"}


def test_fields_from_record_uses_field_types_for_data_type():
    fields, _ = fields_from_candidate_record(
        _record("clusteredColumnChart", {"Category": ["Orders.Order Date"], "Y": ["Orders.Sales"]}),
        field_types={"Orders.Order Date": "date"})
    dt = {f["name"]: f["data_type"] for f in fields}
    assert dt["Order Date"] == "date"


def test_fields_from_record_rejects_unknown_slot():
    fields, reason = fields_from_candidate_record(
        _record("clusteredColumnChart", {"Tooltips": ["Orders.Sales"]}))
    assert fields is None
    assert "no unambiguous field role" in reason


def test_fields_from_record_rejects_empty():
    fields, reason = fields_from_candidate_record(_record("card", {}))
    assert fields is None
    assert "no bound fields" in reason


def test_advise_for_record_offers_alternatives_and_drops_current():
    e = advise_for_candidate_record(
        _record("clusteredColumnChart", {"Category": ["Orders.Segment"], "Y": ["Orders.Sales"]}))
    assert e["advisable"]
    types = [s["visual_type"] for s in e["suggestions"]]
    assert "clusteredColumnChart" not in types       # the current type is never re-suggested
    assert e["top_alternative"] == types[0]


def test_advise_for_record_table_is_not_advisable():
    e = advise_for_candidate_record(
        _record("tableEx", {"Values": ["Orders.Sales", "Orders.Segment"]}))
    assert e["advisable"] is False
    assert "detail table" in e["reason"]
    assert "suggestions" not in e


def test_advise_for_record_temporal_prefers_line():
    e = advise_for_candidate_record(
        _record("clusteredColumnChart", {"Category": ["Orders.Order Date"], "Y": ["Orders.Sales"]}),
        field_types={"Orders.Order Date": "date"})
    assert e["top_alternative"] == "lineChart"


def test_build_report_advice_shape_and_counts():
    records = [
        _record("clusteredColumnChart", {"Category": ["Orders.Segment"], "Y": ["Orders.Sales"]}),
        _record("tableEx", {"Values": ["Orders.Sales"]}),
    ]
    body = build_report_advice(records)
    assert body["kind"] == ADVICE_SIDECAR_KIND
    assert body["rules"] == list(ADVISOR_RULES)
    assert body["summary"]["visuals"] == 2
    assert body["summary"]["advisable"] == 1
    assert body["summary"]["with_alternative"] == 1


def test_build_report_advice_empty_is_well_formed():
    body = build_report_advice([])
    assert body["advice"] == []
    assert body["summary"] == {"visuals": 0, "advisable": 0, "with_alternative": 0}
