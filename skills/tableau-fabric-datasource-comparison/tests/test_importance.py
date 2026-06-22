"""Unit tests for the artifact-importance signal in ``importance.py`` (pure, offline, additive)."""
import copy

import importance


# --------------------------------------------------------------------------------------
# importance_for -- banding
# --------------------------------------------------------------------------------------
def test_no_usage_is_unknown():
    assert importance.importance_for(None)["level"] == "Unknown"
    assert importance.importance_for({})["level"] == "Unknown"


def test_no_signal_at_all_is_unknown():
    # counts present as keys but all null + no views/certification -> still no evidence
    u = {"workbook_count": None, "dashboard_count": None, "view_count": None,
         "certified": None, "source": "none"}
    assert importance.importance_for(u)["level"] == "Unknown"


def test_high_reach_high_views_certified_is_critical():
    u = {"workbook_count": 30, "dashboard_count": 12, "view_count": 5000, "certified": True}
    imp = importance.importance_for(u)
    assert imp["level"] == "Critical"
    assert imp["score"] >= 0.62
    assert "certified" in imp["drivers"]


def test_small_but_present_usage_is_low_not_unknown():
    u = {"workbook_count": 0, "dashboard_count": 0, "view_count": 1, "certified": False}
    imp = importance.importance_for(u)
    assert imp["level"] == "Low"
    assert imp["score"] > 0.0


def test_moderate_band():
    # a few workbooks + a few hundred views -> Moderate/High territory, never Unknown
    u = {"workbook_count": 3, "dashboard_count": 1, "view_count": 200, "certified": False}
    imp = importance.importance_for(u)
    assert imp["level"] in ("Moderate", "High")


def test_score_is_zero_to_one():
    u = {"workbook_count": 9999, "dashboard_count": 9999, "view_count": 9_000_000,
         "certified": True}
    imp = importance.importance_for(u)
    assert 0.0 <= imp["score"] <= 1.0


# --------------------------------------------------------------------------------------
# importance_for -- missing-signal renormalisation
# --------------------------------------------------------------------------------------
def test_missing_views_renormalises_not_drags_to_zero():
    # only reach known, and it is high -> score should reflect reach alone, not be halved
    only_reach = importance.importance_for({"workbook_count": 40, "dashboard_count": 20})
    both = importance.importance_for({"workbook_count": 40, "dashboard_count": 20,
                                      "view_count": 5000})
    # reach saturates near 1; missing views must not pull the reach-only score far below it
    assert only_reach["score"] >= 0.9
    assert abs(only_reach["score"] - both["score"]) < 0.15


def test_endorsement_only_blends_to_full_weight():
    imp = importance.importance_for({"certified": True})
    # certified is the only present signal -> renormalised endorsement = 1.0
    assert imp["score"] == 1.0
    assert imp["level"] == "Critical"


# --------------------------------------------------------------------------------------
# bool / type guards
# --------------------------------------------------------------------------------------
def test_bool_counts_do_not_masquerade_as_numbers():
    # a stray boolean must not be treated as a count of 1
    u = {"workbook_count": True, "dashboard_count": False, "view_count": None, "certified": None}
    imp = importance.importance_for(u)
    assert imp["level"] == "Unknown"  # no real numeric/categorical signal


def test_string_counts_are_ignored():
    u = {"workbook_count": "lots", "view_count": "many", "certified": None}
    assert importance.importance_for(u)["level"] == "Unknown"


# --------------------------------------------------------------------------------------
# annotate -- rollup, idempotency, invariants
# --------------------------------------------------------------------------------------
def _result():
    return {
        "summary": {"already_exist": 1},
        "matches": [
            {"tableau_name": "A", "tier": "Exact", "score": 0.9, "bucket": "already_exists",
             "usage": {"workbook_count": 20, "dashboard_count": 8, "view_count": 4000,
                       "certified": True, "has_quality_warning": True}},
            {"tableau_name": "B", "tier": "None", "score": 0.0, "bucket": "rebuild",
             "usage": {"workbook_count": 1, "dashboard_count": 0, "view_count": 5,
                       "certified": False}},
            {"tableau_name": "C", "tier": "Weak", "score": 0.2, "bucket": "rebuild",
             "usage": {"workbook_count": None, "dashboard_count": None, "source": "none"}},
        ],
    }


def test_annotate_attaches_importance_to_every_match():
    res = importance.annotate(_result())
    levels = [m["importance"]["level"] for m in res["matches"]]
    assert levels[0] == "Critical"
    assert levels[2] == "Unknown"
    for m in res["matches"]:
        assert set(m["importance"]) == {"level", "score", "drivers"}


def test_annotate_summary_rollup():
    res = importance.annotate(_result())
    roll = res["summary"]["importance"]
    assert roll["critical"] == 1
    assert roll["total_views"] == 4005  # 4000 + 5 (C has no view_count)
    assert roll["certified_datasources"] == 1
    assert roll["datasources_with_quality_warning"] == 1
    assert roll["by_level"]["Unknown"] == 1


def test_annotate_total_views_none_when_no_views_anywhere():
    res = importance.annotate({"summary": {}, "matches": [
        {"tableau_name": "X", "usage": {"workbook_count": 3}},
    ]})
    assert res["summary"]["importance"]["total_views"] is None


def test_annotate_is_idempotent():
    once = importance.annotate(_result())
    twice = importance.annotate(copy.deepcopy(once))
    assert [m["importance"] for m in once["matches"]] == \
           [m["importance"] for m in twice["matches"]]
    assert once["summary"]["importance"] == twice["summary"]["importance"]


def test_annotate_never_changes_tier_score_or_bucket():
    before = _result()
    snapshot = [(m["tier"], m["score"], m["bucket"]) for m in before["matches"]]
    after = importance.annotate(before)
    assert [(m["tier"], m["score"], m["bucket"]) for m in after["matches"]] == snapshot


def test_annotate_tolerates_missing_usage_and_empty():
    res = importance.annotate({"matches": [{"tableau_name": "X"}]})
    assert res["matches"][0]["importance"]["level"] == "Unknown"
    empty = importance.annotate({})
    assert empty["summary"]["importance"]["by_level"]["Unknown"] == 0


# --------------------------------------------------------------------------------------
# worklist ordering
# --------------------------------------------------------------------------------------
def test_importance_worklist_orders_most_important_first():
    res = importance.annotate(_result())
    ordered = importance.importance_worklist(res)
    scores = [(m.get("importance") or {}).get("score") or 0.0 for m in ordered]
    assert scores == sorted(scores, reverse=True)
    assert ordered[0]["tableau_name"] == "A"


def test_importance_worklist_respects_limit():
    res = importance.annotate(_result())
    assert len(importance.importance_worklist(res, limit=1)) == 1


# --------------------------------------------------------------------------------------
# report rendering (compare._render_importance)
# --------------------------------------------------------------------------------------
def _estate_with_usage():
    import compare
    tab = [
        {"name": "Sales Orders", "project": "Fin",
         "fields": [{"name": "order_id"}, {"name": "amount"}, {"name": "region"}],
         "sources": [{"connector": "snowflake", "database": "DB", "schema": "P", "table": "ORDERS"}],
         "usage": {"workbook_count": 20, "dashboard_count": 8, "view_count": 4000,
                   "certified": True, "has_quality_warning": False,
                   "extract_last_refresh": "2026-06-20T03:00:00Z",
                   "connected_assets": {"workbooks": [{"name": "Exec KPIs", "luid": "w1"}],
                                        "dashboards": [{"name": "Daily Sales"}]}}},
    ]
    fab = [
        {"name": "Sales Orders", "workspace": "WS", "id": "m1",
         "tables": [{"name": "ORDERS"}],
         "columns": [{"name": "order_id", "table": "ORDERS"}, {"name": "amount", "table": "ORDERS"},
                     {"name": "region", "table": "ORDERS"}],
         "sources": [{"connector": "snowflake", "database": "DB", "schema": "P", "table": "ORDERS"}]},
    ]
    return compare.compare_inventories(tab, fab)


def test_compare_inventories_attaches_importance():
    res = _estate_with_usage()
    assert "importance" in res["summary"]
    assert all("importance" in m for m in res["matches"])


def test_report_renders_importance_section_when_rated():
    import compare
    res = _estate_with_usage()
    md = compare.render_markdown(res)
    assert "## Artifact importance & connected assets" in md
    assert "Critical" in md
    assert "Exec KPIs" in md


def test_report_omits_importance_section_when_all_unknown():
    import compare
    tab = [{"name": "X", "project": "P", "fields": [{"name": "a"}], "sources": [],
            "usage": {"source": "none"}}]
    res = compare.compare_inventories(tab, [])
    md = compare.render_markdown(res)
    assert "## Artifact importance & connected assets" not in md


