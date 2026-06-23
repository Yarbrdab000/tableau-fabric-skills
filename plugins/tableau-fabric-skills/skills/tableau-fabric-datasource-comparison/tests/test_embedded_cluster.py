"""Tests for ``embedded_cluster.py`` -- fingerprinting + clustering of embedded datasources."""
import embedded_cluster as ec


def _row(sid, ds, fields, tables, did=None):
    return {
        "workbook_luid": sid, "workbook_name": f"WB {sid}", "project": "P",
        "source_id": sid, "datasource_name": ds, "datasource_id": did or ds,
        "fields": [{"name": f, "dataType": "STRING", "role": "", "is_calculated": False}
                   for f in fields],
        "sources": [{"connectionType": "sqlserver", "database": "DB", "schema": "dbo", "table": t}
                    for t in tables],
        "objects": [], "has_extract": None, "source_path": "metadata",
    }


SUPERSTORE_FIELDS = ["OrderId", "NetSales", "GrossProfit", "ShipRegion", "ProductCategory"]
SUPERSTORE_TABLES = ["Orders"]


def test_fingerprint_is_order_and_caption_independent():
    a = _row("w1", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES)
    # Same structure: different caption, reversed field order, bracketed names.
    b = _row("w2", "Sales Copy",
             ["[Product Category]", "Ship Region", "Gross Profit", "Net Sales", "Order Id"],
             ["Orders"])
    assert ec.fingerprint(a) == ec.fingerprint(b)


def test_fingerprint_differs_on_structure():
    a = _row("w1", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES)
    b = _row("w2", "Superstore", SUPERSTORE_FIELDS + ["DiscountRate"], SUPERSTORE_TABLES)
    assert ec.fingerprint(a) != ec.fingerprint(b)


def test_exact_duplicates_collapse_into_one_cluster():
    rows = [
        _row("w1", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
        _row("w2", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
        _row("w3", "Superstore Copy", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
    ]
    out = ec.cluster_embedded(rows)
    assert len(out["clusters"]) == 1
    c = out["clusters"][0]
    assert c["cluster_id"] == "ec-001"
    assert c["size"] == 3
    assert c["is_duplicate_group"] is True
    assert len(c["members"]) == 3


def test_near_duplicate_merges_under_threshold():
    rows = [
        _row("w1", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
        _row("w2", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
        _row("w3", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
        _row("w4", "Superstore Plus", SUPERSTORE_FIELDS + ["DiscountRate"], SUPERSTORE_TABLES),
        _row("w5", "HR Headcount", ["EmployeeKeyId", "HireDate2", "DeptName2"], ["Employees"]),
    ]
    out = ec.cluster_embedded(rows, threshold=0.8)
    sizes = sorted(c["size"] for c in out["clusters"])
    assert sizes == [1, 4]                 # the near-dup joins the Superstore group; HR stands alone
    biggest = out["clusters"][0]
    assert biggest["size"] == 4
    # Representative is the most complete member (the +1-column copy).
    assert biggest["representative"]["field_count"] == 6
    assert out["summary"]["duplicate_group_count"] == 1
    assert out["summary"]["consolidatable_datasources"] == 4
    assert out["summary"]["largest_cluster_size"] == 4


def test_high_threshold_keeps_near_duplicate_separate():
    rows = [
        _row("w1", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
        _row("w2", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
        _row("w4", "Superstore Plus", SUPERSTORE_FIELDS + ["DiscountRate"], SUPERSTORE_TABLES),
    ]
    out = ec.cluster_embedded(rows, threshold=0.97)
    # The exact pair stays grouped; the +1-column copy is its own cluster.
    sizes = sorted(c["size"] for c in out["clusters"])
    assert sizes == [1, 2]


def test_index_maps_every_member():
    rows = [
        _row("w1", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
        _row("w2", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
        _row("w5", "HR Headcount", ["EmployeeKeyId", "HireDate2", "DeptName2"], ["Employees"]),
    ]
    out = ec.cluster_embedded(rows)
    assert set(out["index"].values()) == {c["cluster_id"] for c in out["clusters"]}
    assert len(out["index"]) == 3
    # Both Superstore copies share a cluster; HR is elsewhere.
    keys = {ec.member_key(r, i): r for i, r in enumerate(rows)}
    cl = {k: out["index"][k] for k in keys}
    sup = [out["index"][ec.member_key(rows[0], 0)], out["index"][ec.member_key(rows[1], 1)]]
    assert sup[0] == sup[1]
    assert out["index"][ec.member_key(rows[2], 2)] != sup[0]


def test_clusters_ordered_by_descending_size():
    rows = [
        _row("a1", "A", ["Alpha1", "Beta1", "Gamma1"], ["TableA"]),
        _row("a2", "A", ["Alpha1", "Beta1", "Gamma1"], ["TableA"]),
        _row("a3", "A", ["Alpha1", "Beta1", "Gamma1"], ["TableA"]),
        _row("b1", "B", ["Delta9", "Epsilon9"], ["TableB"]),
        _row("b2", "B", ["Delta9", "Epsilon9"], ["TableB"]),
        _row("c1", "C", ["Zeta7"], ["TableC"]),
    ]
    out = ec.cluster_embedded(rows)
    sizes = [c["size"] for c in out["clusters"]]
    assert sizes == [3, 2, 1]
    assert [c["cluster_id"] for c in out["clusters"]] == ["ec-001", "ec-002", "ec-003"]


def test_deterministic_repeated_runs():
    rows = [
        _row("w2", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
        _row("w1", "Superstore", SUPERSTORE_FIELDS, SUPERSTORE_TABLES),
        _row("w5", "HR", ["EmployeeKeyId", "HireDate2"], ["Employees"]),
    ]
    a = ec.cluster_embedded(rows)
    b = ec.cluster_embedded(rows)
    assert a == b


def test_empty_input():
    out = ec.cluster_embedded([])
    assert out["clusters"] == []
    assert out["index"] == {}
    assert out["summary"]["embedded_total"] == 0
