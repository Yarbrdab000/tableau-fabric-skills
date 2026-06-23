"""Tests for ``embedded_score.py`` -- scoring embedded datasources via the reused engine."""
import embedded_score as es


SUPER_FIELDS = ["OrderId", "NetSales", "GrossProfit", "ShipRegion", "ProductCategory"]


def _embedded(sid, ds, fields, tables):
    return {
        "workbook_luid": sid, "workbook_name": f"WB {sid}", "project": "P",
        "source_id": sid, "datasource_name": ds, "datasource_id": ds,
        "fields": [{"name": f, "dataType": "STRING", "role": "", "is_calculated": False}
                   for f in fields],
        "sources": [{"connectionType": "sqlserver", "database": "DB", "schema": "dbo", "table": t}
                    for t in tables],
        "objects": [], "has_extract": None, "source_path": "metadata",
    }


def _fabric(name, cols, tables, fid="m1", ws="WS", wsid="ws-1"):
    return {
        "name": name, "id": fid, "workspace": ws, "workspaceId": wsid,
        "columns": [{"name": c, "dataType": "string"} for c in cols],
        "tables": tables,
        "sources": [{"connectionType": "sqlserver", "database": "DB", "schema": "dbo", "table": t}
                    for t in tables],
    }


def _published(name, fields, tables, luid="pub-1", project="Pub"):
    return {
        "name": name, "luid": luid, "project": project,
        "fields": [{"name": f, "dataType": "STRING"} for f in fields],
        "sources": [{"connectionType": "sqlserver", "database": "DB", "schema": "dbo", "table": t}
                    for t in tables],
    }


def test_embedded_matches_fabric_exact_with_connection_identity():
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"])]
    fabric = [_fabric("Superstore", SUPER_FIELDS, ["Orders"], fid="abc", ws="Sales", wsid="ws-42")]
    out = es.score_embedded(rows, fabric=fabric)
    s = out["scores"][0]
    assert s["fabric"]["tier"] == "Exact"
    bm = s["fabric"]["best_match"]
    # byConnection identity the dashboard skill binds to, supplied straight from the comparison.
    assert bm["fabric_name"] == "Superstore"
    assert bm["workspace_id"] == "ws-42"
    assert bm["fabric_id"] == "abc"
    assert out["summary"]["fabric_reuse_candidates"] == 1
    assert s["published"] is None


def test_embedded_matches_published_exact():
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"])]
    published = [_published("Superstore", SUPER_FIELDS, ["Orders"], luid="pub-9")]
    out = es.score_embedded(rows, published=published)
    s = out["scores"][0]
    assert s["fabric"] is None
    assert s["published"]["tier"] == "Exact"
    assert s["published"]["best_match"]["published_luid"] == "pub-9"
    assert out["summary"]["published_reuse_candidates"] == 1


def test_no_overlap_yields_no_best_match():
    rows = [_embedded("w1", "Lonely", ["Unique1", "Unique2"], ["WeirdTable"])]
    fabric = [_fabric("Totally Other", ["Alpha9", "Beta9"], ["OtherTable"])]
    out = es.score_embedded(rows, fabric=fabric)
    s = out["scores"][0]
    assert s["fabric"]["tier"] == "None"
    assert s["fabric"]["best_match"] is None       # zero score -> no candidate surfaced
    assert s["fabric"]["candidates"]               # candidates list still present
    assert out["summary"]["fabric_reuse_candidates"] == 0


def test_both_axes_scored_together():
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"])]
    fabric = [_fabric("Superstore", SUPER_FIELDS, ["Orders"])]
    published = [_published("Superstore Source", SUPER_FIELDS, ["Orders"])]
    out = es.score_embedded(rows, fabric=fabric, published=published)
    s = out["scores"][0]
    assert s["fabric"]["best_match"] is not None
    assert s["published"]["best_match"] is not None


def test_candidates_capped_at_top_n():
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"])]
    fabric = [_fabric(f"M{i}", SUPER_FIELDS, ["Orders"], fid=f"id{i}") for i in range(6)]
    out = es.score_embedded(rows, fabric=fabric, top_n=2)
    assert len(out["scores"][0]["fabric"]["candidates"]) == 2


def test_summary_totals():
    rows = [
        _embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"]),
        _embedded("w2", "HR", ["EmpKeyId", "HireDt"], ["Employees"]),
    ]
    fabric = [_fabric("Superstore", SUPER_FIELDS, ["Orders"])]
    published = [_published("HR Source", ["EmpKeyId", "HireDt"], ["Employees"])]
    out = es.score_embedded(rows, fabric=fabric, published=published)
    sm = out["summary"]
    assert sm["embedded_total"] == 2
    assert sm["fabric_total"] == 1
    assert sm["published_total"] == 1
    assert sm["fabric_reuse_candidates"] == 1       # only Superstore matches the model
    assert sm["published_reuse_candidates"] == 1     # only HR matches the published source


def test_attach_cluster_scores_to_representative():
    import embedded_cluster as ec
    rows = [
        _embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"]),
        _embedded("w2", "Superstore", SUPER_FIELDS, ["Orders"]),
    ]
    fabric = [_fabric("Superstore", SUPER_FIELDS, ["Orders"], wsid="ws-7")]
    clusters = ec.cluster_embedded(rows)
    scored = es.score_embedded(rows, fabric=fabric)
    rollup = es.attach_cluster_scores(clusters, scored)
    assert set(rollup) == {"ec-001"}
    entry = rollup["ec-001"]
    assert entry["size"] == 2
    assert entry["fabric"]["best_match"]["workspace_id"] == "ws-7"


def test_deterministic():
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"])]
    fabric = [_fabric("Superstore", SUPER_FIELDS, ["Orders"])]
    a = es.score_embedded(rows, fabric=fabric)
    b = es.score_embedded(rows, fabric=fabric)
    assert a == b


def test_no_targets_both_none():
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"])]
    out = es.score_embedded(rows)
    assert out["scores"][0]["fabric"] is None
    assert out["scores"][0]["published"] is None
