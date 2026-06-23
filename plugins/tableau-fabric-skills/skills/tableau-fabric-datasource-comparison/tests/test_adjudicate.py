"""Unit tests for the LLM-optional adjudication tier in ``adjudicate.py``."""
import compare
import adjudicate


def _ds(name, fields, sources, luid=None, project=None):
    return {"name": name, "luid": luid, "project": project, "fields": fields, "sources": sources}


def _model(name, columns, sources, mid=None, tables=None, ws="WS"):
    return {
        "name": name, "id": mid, "workspace": ws, "workspaceId": "w-1",
        "columns": columns, "sources": sources, "tables": tables or [],
    }


# --------------------------------------------------------------------------------------
# Router classification
# --------------------------------------------------------------------------------------
def test_confident_exact_match_is_not_reviewed():
    cols = [{"name": "Sales", "dataType": "REAL"}, {"name": "Region", "dataType": "STRING"}]
    fcols = [{"name": "Sales", "dataType": "double"}, {"name": "Region", "dataType": "string"}]
    src = [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}]
    result = compare.compare_inventories(
        [_ds("Superstore", cols, src, luid="t1")],
        [_model("Superstore", fcols, src, mid="m1")],
    )
    adj = result["adjudication"]
    assert adj["summary"]["total_reviewed"] == 0
    assert adj["summary"]["auto_confident"] == 1
    assert adj["requests"] == []


def test_renamed_columns_suspected_when_name_matches_but_columns_diverge():
    # Same asset name, totally different column names -> renamed-columns category.
    tab = [_ds("Superstore", [{"name": "cust_id", "dataType": "INTEGER"},
                              {"name": "rev", "dataType": "REAL"}],
               [{"connectionType": "snowflake", "database": "D", "table": "ORDERS"}], luid="t1")]
    fab = [_model("Superstore", [{"name": "CustomerKey", "dataType": "int64"},
                                 {"name": "Sales Amount", "dataType": "double"}],
                  [{"connectionType": "databricks", "database": "lake", "table": "dim_customer"}],
                  mid="m1", tables=["dim_customer"])]
    result = compare.compare_inventories(tab, fab)
    cats = result["adjudication"]["summary"]["categories"]
    assert cats.get("renamed_columns_suspected") == 1
    req = result["adjudication"]["requests"][0]
    # The handoff carries typed Tableau columns and enriched Fabric candidates for semantic matching.
    assert {c["name"] for c in req["tableau_columns"]} == {"cust_id", "rev"}
    assert req["candidates"][0]["columns"]  # Fabric columns are attached
    assert "category_guidance" in req and req["category_guidance"]


def test_obscured_source_match_is_flagged_for_confirmation():
    # Fabric source obscured (no resolvable table, no tables list); names match and columns overlap
    # partially so the score lands in Strong (not Exact) -> obscured_source category.
    cols = [{"name": "A", "dataType": "STRING"}, {"name": "B", "dataType": "STRING"},
            {"name": "C", "dataType": "STRING"}]
    fcols = cols + [{"name": "D", "dataType": "STRING"}, {"name": "E", "dataType": "STRING"}]
    tab = [_ds("Widget Mart", cols,
               [{"connectionType": "sqlserver", "database": "D", "table": "Widgets"}], luid="t1")]
    fab = [_model("Widget Mart", fcols, [], mid="m1")]  # no sources, no tables -> obscured
    result = compare.compare_inventories(tab, fab)
    m = result["matches"][0]
    assert m["source_compared"] is False
    assert m["tier"] == "Strong"
    assert result["adjudication"]["summary"]["categories"].get("obscured_source") == 1


def test_near_tie_flagged_when_two_candidates_are_close():
    cols = [{"name": "X", "dataType": "STRING"}, {"name": "Y", "dataType": "STRING"}]
    src = [{"connectionType": "sqlserver", "database": "D", "table": "T"}]
    tab = [_ds("Sales", cols, src, luid="t1")]
    fab = [_model("Sales East", cols, src, mid="m1"), _model("Sales West", cols, src, mid="m2")]
    result = compare.compare_inventories(tab, fab)
    assert result["adjudication"]["summary"]["categories"].get("near_tie") == 1


# --------------------------------------------------------------------------------------
# Apply path -- advisory only
# --------------------------------------------------------------------------------------
def _partial_result():
    tab = [_ds("Superstore", [{"name": "cust_id", "dataType": "INTEGER"}],
               [{"connectionType": "snowflake", "database": "D", "table": "ORDERS"}], luid="t1")]
    fab = [_model("Superstore", [{"name": "CustomerKey", "dataType": "int64"}],
                  [{"connectionType": "databricks", "database": "lake", "table": "dim_customer"}],
                  mid="m1", tables=["dim_customer"])]
    return compare.compare_inventories(tab, fab)


def test_apply_adjudication_annotates_without_mutating_deterministic_verdict():
    result = _partial_result()
    det_tier = result["matches"][0]["tier"]
    det_score = result["matches"][0]["score"]
    det_bucket = result["matches"][0]["bucket"]

    out = adjudicate.apply_adjudication(result, {"reviews": [
        {"tableau_luid": "t1", "verdict": "match", "confidence": "high",
         "rationale": "cust_id == CustomerKey; same Snowflake orders mirrored in the lakehouse"},
    ]})
    m = out["matches"][0]
    # deterministic verdict preserved exactly
    assert m["tier"] == det_tier and m["score"] == det_score and m["bucket"] == det_bucket
    # advisory annotation added
    assert m["agent_review"]["verdict"] == "match"
    assert m["agent_review"]["adjudicated_bucket"] == "already_exists"
    # input result was not mutated (deep copy)
    assert "agent_review" not in result["matches"][0]


def test_apply_adjudication_rolls_up_and_reports_delta():
    result = _partial_result()
    # deterministic says this is NOT already-exists (renamed columns -> low score)
    assert result["summary"]["already_exist"] == 0
    out = adjudicate.apply_adjudication(
        result, [{"tableau_name": "Superstore", "verdict": "match"}])
    adj = out["adjudicated_summary"]
    assert adj["already_exist"] == 1
    assert adj["reviews_applied"] == 1
    assert adj["delta"]["already_exist"] == 1
    # the deterministic summary is untouched
    assert out["summary"]["already_exist"] == 0


# --------------------------------------------------------------------------------------
# Durability: hostile decision payloads and candidate-less matches degrade gracefully
# --------------------------------------------------------------------------------------
def test_apply_adjudication_ignores_unknown_ids_and_bad_types():
    src = [{"connectionType": "sqlserver", "database": "S", "table": "Orders"}]
    cols = [{"name": "Sales", "dataType": "REAL"}, {"name": "Region", "dataType": "STRING"}]
    fcols = [{"name": "Sales", "dataType": "double"}, {"name": "Region", "dataType": "string"}]
    result = compare.compare_inventories(
        [_ds("Superstore", cols, src, luid="t1")], [_model("Superstore", fcols, src, mid="m1")]
    )
    det_tiers = [m["tier"] for m in result["matches"]]
    # a list peppered with None / strings / ints / an unknown id alongside one valid review.
    out = adjudicate.apply_adjudication(result, [
        None, "garbage", 123, ["nested"],
        {"tableau_name": "DoesNotExist", "verdict": "match"},
        {"tableau_luid": "t1", "verdict": "no-match"},
    ])
    assert [m["tier"] for m in out["matches"]] == det_tiers  # deterministic verdict untouched
    assert out["adjudicated_summary"]["reviews_applied"] == 1  # only the valid, matching review applied


def test_apply_adjudication_with_entirely_garbage_decisions_is_passthrough():
    result = compare.compare_inventories(
        [_ds("X", [{"name": "a", "dataType": "REAL"}], [])], []
    )
    for junk in (None, "nope", 7, [None, "x", 3], {"reviews": [None, "x"]}):
        out = adjudicate.apply_adjudication(result, junk)
        assert out["adjudicated_summary"]["reviews_applied"] == 0


def test_build_adjudication_tolerates_matches_without_candidates():
    # A match with no candidates / no best_match (e.g. a hand-assembled or partial record).
    matches = [{
        "tableau_name": "Lonely", "tier": "Partial", "score": 0.5,
        "bucket": "partial", "best_match": None, "candidates": [],
    }]
    adj = adjudicate.build_adjudication(matches, [], [])
    assert set(adj) == {"summary", "needs_review", "requests"}
    assert adj["summary"]["total_reviewed"] == len(adj["requests"])
    assert adj["requests"][0]["candidates"] == []


def test_apply_adjudication_accepts_verdict_synonyms_and_keyed_dict():
    result = _partial_result()
    out = adjudicate.apply_adjudication(result, {"Superstore": {"verdict": "no-match"}})
    assert out["matches"][0]["agent_review"]["adjudicated_bucket"] == "rebuild"


def test_apply_adjudication_no_decisions_is_safe_passthrough():
    result = _partial_result()
    out = adjudicate.apply_adjudication(result, None)
    assert out["adjudicated_summary"]["reviews_applied"] == 0
    assert all("agent_review" not in m for m in out["matches"])


# --------------------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------------------
def test_markdown_renders_adjudication_queue_and_post_review_rollup():
    result = _partial_result()
    md = compare.render_markdown(result)
    assert "## Agent adjudication queue" in md

    out = adjudicate.apply_adjudication(
        result, [{"tableau_name": "Superstore", "verdict": "match", "confidence": "high"}])
    md2 = compare.render_markdown(out)
    assert "## After semantic review" in md2
    assert "Deterministic" in md2
