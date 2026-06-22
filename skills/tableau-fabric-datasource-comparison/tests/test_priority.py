"""Unit tests for the migration-priority signal (``priority.py``) and its wiring into ``compare``."""
import compare
import priority


def _ds(name, fields, sources, luid=None, usage=None):
    return {"name": name, "luid": luid, "project": None,
            "fields": fields, "sources": sources, "usage": usage}


def _model(name, columns, sources, mid=None, tables=None, ws="WS"):
    return {"name": name, "id": mid, "workspace": ws, "workspaceId": "w-1",
            "columns": columns, "sources": sources, "tables": tables or []}


def _usage(workbooks, sheets=None, dashboards=None, source="metadata"):
    return {"workbook_count": workbooks, "sheet_count": sheets,
            "dashboard_count": dashboards, "source": source}


# --------------------------------------------------------------------------------------
# usage_priority -- threshold banding (incl. the 0/1 deprioritize rule)
# --------------------------------------------------------------------------------------
def test_usage_priority_high_at_threshold():
    assert priority.usage_priority(_usage(5)) == "High"
    assert priority.usage_priority(_usage(40)) == "High"


def test_usage_priority_medium_band():
    assert priority.usage_priority(_usage(2)) == "Medium"
    assert priority.usage_priority(_usage(4)) == "Medium"


def test_usage_priority_one_workbook_is_low_deprioritized():
    assert priority.usage_priority(_usage(1)) == "Low"


def test_usage_priority_zero_workbooks_is_unused_deprioritized():
    assert priority.usage_priority(_usage(0)) == "Unused"


def test_usage_priority_unknown_when_not_gathered():
    assert priority.usage_priority(None) == "Unknown"
    assert priority.usage_priority({}) == "Unknown"
    assert priority.usage_priority(_usage(None, source="none")) == "Unknown"


def test_usage_priority_unknown_on_garbage_count():
    assert priority.usage_priority({"workbook_count": "lots"}) == "Unknown"


def test_usage_priority_custom_thresholds():
    th = {"high": 10, "medium": 3}
    assert priority.usage_priority(_usage(9), th) == "Medium"
    assert priority.usage_priority(_usage(10), th) == "High"
    assert priority.usage_priority(_usage(1), th) == "Low"


def test_usage_priority_two_below_medium_is_unused():
    # With default thresholds medium=2, so 2 is Medium; with medium=3, 2 is neither High/Medium/1 -> Unused
    assert priority.usage_priority(_usage(2), {"high": 10, "medium": 3}) == "Unused"


# --------------------------------------------------------------------------------------
# migration_priority -- fusion of bucket + usage label
# --------------------------------------------------------------------------------------
def test_migration_priority_reuse_when_already_exists_regardless_of_usage():
    assert priority.migration_priority("already_exists", "High") == "Reuse (already in Fabric)"
    assert priority.migration_priority("already_exists", "Unused") == "Reuse (already in Fabric)"


def test_migration_priority_orders_rebuilds_by_usage():
    assert priority.migration_priority("rebuild", "High") == "P1 - migrate first"
    assert priority.migration_priority("rebuild", "Medium") == "P2 - migrate"
    assert priority.migration_priority("partial", "Low") == "P3 - deprioritize"
    assert priority.migration_priority("rebuild", "Unused") == "P4 - retire candidate"
    assert priority.migration_priority("rebuild", "Unknown") == "Unprioritized"


# --------------------------------------------------------------------------------------
# annotate -- per-match labels + summary rollups
# --------------------------------------------------------------------------------------
def test_annotate_adds_labels_and_rollups():
    result = {
        "summary": {},
        "matches": [
            {"tableau_name": "A", "bucket": "already_exists", "usage": _usage(12)},
            {"tableau_name": "B", "bucket": "rebuild", "usage": _usage(0)},
            {"tableau_name": "C", "bucket": "rebuild", "usage": _usage(1)},
            {"tableau_name": "D", "bucket": "rebuild", "usage": None},
        ],
    }
    priority.annotate(result)
    labels = {m["tableau_name"]: (m["priority"], m["migration_priority"]) for m in result["matches"]}
    assert labels["A"] == ("High", "Reuse (already in Fabric)")
    assert labels["B"] == ("Unused", "P4 - retire candidate")
    assert labels["C"] == ("Low", "P3 - deprioritize")
    assert labels["D"] == ("Unknown", "Unprioritized")
    bp = result["summary"]["by_priority"]
    assert bp["High"] == 1 and bp["Unused"] == 1 and bp["Low"] == 1 and bp["Unknown"] == 1
    bm = result["summary"]["by_migration_priority"]
    assert bm["Reuse (already in Fabric)"] == 1
    assert bm["P4 - retire candidate"] == 1
    assert bm["P3 - deprioritize"] == 1
    assert bm["Unprioritized"] == 1
    assert result["summary"]["usage_thresholds"] == priority.DEFAULT_USAGE_THRESHOLDS


def test_annotate_is_safe_without_usage():
    result = {"summary": {}, "matches": [{"tableau_name": "A", "bucket": "rebuild"}]}
    priority.annotate(result)
    assert result["matches"][0]["priority"] == "Unknown"
    assert result["matches"][0]["migration_priority"] == "Unprioritized"


def test_rebuild_worklist_sorted_by_priority_then_score():
    result = {
        "summary": {},
        "matches": [
            {"tableau_name": "low1", "bucket": "rebuild", "score": 0.3, "usage": _usage(1)},
            {"tableau_name": "busy", "bucket": "rebuild", "score": 0.1, "usage": _usage(20)},
            {"tableau_name": "reuse", "bucket": "already_exists", "score": 0.9, "usage": _usage(9)},
            {"tableau_name": "mid", "bucket": "partial", "score": 0.5, "usage": _usage(3)},
        ],
    }
    priority.annotate(result)
    work = priority.rebuild_worklist(result)
    names = [m["tableau_name"] for m in work]
    # already_exists is excluded; order is P1(busy) -> P2(mid) -> P3(low1)
    assert names == ["busy", "mid", "low1"]


# --------------------------------------------------------------------------------------
# compare_inventories carries usage through to priority annotations
# --------------------------------------------------------------------------------------
def test_compare_inventories_attaches_usage_and_priority():
    src = [{"connectionType": "snowflake", "database": "DW", "schema": "dbo", "table": "sales"}]
    tab = [
        _ds("Sales", [{"name": "id", "dataType": "INTEGER"}, {"name": "amt", "dataType": "REAL"}],
            src, luid="t1", usage=_usage(12, 40, 8)),
        _ds("Orphan", [{"name": "x", "dataType": "INTEGER"}],
            [{"connectionType": "postgres", "database": "P", "table": "orphan"}],
            luid="t2", usage=_usage(0, 0, 0)),
    ]
    fab = [_model("Sales", [{"name": "id", "dataType": "int64"}, {"name": "amt", "dataType": "double"}],
                  src, mid="m1")]
    result = compare.compare_inventories(tab, fab)
    by_name = {m["tableau_name"]: m for m in result["matches"]}
    assert by_name["Sales"]["usage"]["workbook_count"] == 12
    assert by_name["Sales"]["migration_priority"] == "Reuse (already in Fabric)"
    assert by_name["Orphan"]["priority"] == "Unused"
    assert by_name["Orphan"]["migration_priority"] == "P4 - retire candidate"
    assert "by_migration_priority" in result["summary"]


def test_render_markdown_shows_priority_section_when_usage_present():
    src = [{"connectionType": "postgres", "database": "P", "table": "orphan"}]
    tab = [_ds("Orphan", [{"name": "x", "dataType": "INTEGER"}], src, luid="t1", usage=_usage(0))]
    fab = [_model("Totally Different", [{"name": "zzz", "dataType": "int64"}],
                  [{"connectionType": "snowflake", "database": "Q", "table": "other"}], mid="m1")]
    md = compare.render_markdown(compare.compare_inventories(tab, fab))
    assert "## Migration priority (what to rebuild first)" in md
    assert "By migration priority:" in md
    assert "P4 - retire candidate" in md


def test_render_markdown_hides_priority_section_without_usage():
    src = [{"connectionType": "postgres", "database": "P", "table": "orphan"}]
    tab = [_ds("Orphan", [{"name": "x", "dataType": "INTEGER"}], src, luid="t1", usage=None)]
    fab = [_model("Totally Different", [{"name": "zzz", "dataType": "int64"}],
                  [{"connectionType": "snowflake", "database": "Q", "table": "other"}], mid="m1")]
    md = compare.render_markdown(compare.compare_inventories(tab, fab))
    assert "## Migration priority (what to rebuild first)" not in md
    assert "By migration priority:" not in md


# --------------------------------------------------------------------------------------
# Durability: hostile / out-of-range usage counts and missing priority fields
# --------------------------------------------------------------------------------------
def test_usage_priority_negative_count_is_safe():
    # A negative count is nonsensical but must not throw or land outside the defined labels.
    assert priority.usage_priority(_usage(-5)) == "Unused"


def test_usage_priority_float_and_huge_counts():
    assert priority.usage_priority(_usage(3.0)) == "Medium"   # int(3.0) -> 3
    assert priority.usage_priority(_usage(10_000_000)) == "High"
    assert priority.usage_priority(_usage("7")) == "High"      # numeric string coerces


def test_rebuild_worklist_tolerates_missing_priority_fields():
    # Matches that never went through annotate() lack migration_priority/score -- sort must not raise.
    result = {"matches": [
        {"tableau_name": "A", "bucket": "rebuild"},
        {"tableau_name": "B", "bucket": "partial", "score": 0.5,
         "migration_priority": "P1 - migrate first"},
        {"tableau_name": "C", "bucket": "already_exists"},
    ]}
    work = priority.rebuild_worklist(result)
    assert [m["tableau_name"] for m in work] == ["B", "A"]  # already_exists excluded; B ranks first


# --------------------------------------------------------------------------------------
# Metadata downstream-usage parsing (the trusted primary source)
# --------------------------------------------------------------------------------------
def test_downstream_usage_metadata_parses_counts(monkeypatch):
    import tableau_inventory as ti

    pages = [
        {"publishedDatasourcesConnection": {
            "nodes": [
                {"luid": "a", "downstreamWorkbooks": [{"luid": "w1"}, {"luid": "w2"}],
                 "downstreamSheets": [{"id": "s1"}], "downstreamDashboards": []},
                {"luid": "b", "downstreamWorkbooks": [], "downstreamSheets": [],
                 "downstreamDashboards": [{"id": "d1"}]},
            ],
            "pageInfo": {"hasNextPage": True, "endCursor": "C1"}}},
        {"publishedDatasourcesConnection": {
            "nodes": [
                {"luid": "c", "downstreamWorkbooks": [{"luid": "w3"}],
                 "downstreamSheets": [{"id": "s2"}, {"id": "s3"}], "downstreamDashboards": [{"id": "d2"}]},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}},
    ]
    calls = {"n": 0}

    def fake_metadata_query(self, query, variables):
        page = pages[calls["n"]]
        calls["n"] += 1
        return page

    client = ti.TableauClient.__new__(ti.TableauClient)
    monkeypatch.setattr(ti.TableauClient, "metadata_query", fake_metadata_query)
    out = client.downstream_usage_metadata(page_size=2)
    assert calls["n"] == 2  # paged through both pages
    assert out["a"] == {"workbook_count": 2, "sheet_count": 1, "dashboard_count": 0, "source": "metadata"}
    assert out["b"]["dashboard_count"] == 1
    assert out["c"] == {"workbook_count": 1, "sheet_count": 2, "dashboard_count": 1, "source": "metadata"}
