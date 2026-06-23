"""End-to-end (offline) test of the ``compare_estate.py`` orchestrator using cached JSON."""
import json

import compare_estate


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_estate_cli_cached_json_writes_json_report(tmp_path):
    tableau = [{
        "name": "Superstore", "project": "Samples", "luid": "t-1",
        "fields": [{"name": "Sales", "dataType": "REAL"}, {"name": "Region", "dataType": "STRING"}],
        "sources": [{"connectionType": "sqlserver", "database": "SalesDB", "table": "Orders"}],
    }]
    fabric = [{
        "name": "Superstore", "workspace": "WS", "workspaceId": "w-1", "id": "m-1",
        "tables": ["Orders"],
        "columns": [{"name": "Sales", "dataType": "double"}, {"name": "Region", "dataType": "string"}],
        "sources": [{"connectionType": "sqlserver", "database": "SalesDB", "table": "Orders"}],
    }]
    t_json = _write(tmp_path, "tableau.json", tableau)
    f_json = _write(tmp_path, "fabric.json", fabric)
    out = tmp_path / "result.json"

    rc = compare_estate.main([
        "--tableau-inventory-json", t_json,
        "--fabric-inventory-json", f_json,
        "--format", "json", "--out", str(out),
    ])
    assert rc == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["summary"]["already_exist"] == 1
    assert result["matches"][0]["tier"] == "Exact"


def test_estate_cli_writes_executive_csv_and_xlsx(tmp_path):
    import csv as _csv
    import io as _io
    import zipfile

    tableau = [{
        "name": "Superstore", "project": "Samples", "luid": "t-1",
        "fields": [{"name": "Sales", "dataType": "REAL"}, {"name": "Region", "dataType": "STRING"}],
        "sources": [{"connectionType": "sqlserver", "database": "SalesDB", "table": "Orders"}],
    }]
    fabric = [{
        "name": "Superstore", "workspace": "WS", "workspaceId": "w-1", "id": "m-1",
        "tables": ["Orders"],
        "columns": [{"name": "Sales", "dataType": "double"}, {"name": "Region", "dataType": "string"}],
        "sources": [{"connectionType": "sqlserver", "database": "SalesDB", "table": "Orders"}],
    }]
    t_json = _write(tmp_path, "tableau.json", tableau)
    f_json = _write(tmp_path, "fabric.json", fabric)
    out = tmp_path / "result.json"
    csv_path = tmp_path / "estate.csv"
    xlsx_path = tmp_path / "estate.xlsx"

    rc = compare_estate.main([
        "--tableau-inventory-json", t_json,
        "--fabric-inventory-json", f_json,
        "--format", "json", "--out", str(out),
        "--export-csv", str(csv_path),
        "--export-xlsx", str(xlsx_path),
    ])
    assert rc == 0

    rows = list(_csv.reader(_io.StringIO(csv_path.read_text(encoding="utf-8-sig"))))
    assert rows[0][0] == "Tableau datasource"
    assert any(r and r[0] == "Superstore" for r in rows[1:])

    raw = xlsx_path.read_bytes()
    assert raw[:2] == b"PK"
    zf = zipfile.ZipFile(_io.BytesIO(raw))
    assert zf.testzip() is None
    assert "xl/worksheets/sheet1.xml" in zf.namelist()


def test_parse_weights_merges_overrides():
    w = compare_estate._parse_weights("name=0.5,source=0.1")
    assert w["name"] == 0.5
    assert w["source"] == 0.1
    # untouched keys keep their defaults
    assert w["column"] == compare_estate.compare_mod.DEFAULT_WEIGHTS["column"]


def test_load_json_accepts_value_wrapper(tmp_path):
    p = tmp_path / "wrapped.json"
    p.write_text(json.dumps({"value": [{"name": "A"}]}), encoding="utf-8")
    assert compare_estate._load_json(str(p)) == [{"name": "A"}]


def test_estate_cli_emits_rebind_plan(tmp_path):
    import csv as _csv
    import io as _io

    tableau = [{
        "name": "Superstore", "project": "Samples", "luid": "t-1",
        "fields": [{"name": "OrderId", "dataType": "STRING"}, {"name": "NetSales", "dataType": "REAL"}],
        "sources": [{"connectionType": "sqlserver", "database": "SalesDB", "table": "Orders"}],
    }]
    fabric = [{
        "name": "HR Headcount", "workspace": "WS", "workspaceId": "w-1", "id": "m-1",
        "tables": ["Employees"],
        "columns": [{"name": "EmployeeKeyId", "dataType": "string"}],
        "sources": [{"connectionType": "sqlserver", "database": "HRDB", "table": "Employees"}],
    }]
    # Two workbooks embed a near-identical copy of the published Superstore -> rebind_to_published.
    embedded = [
        {"workbook_luid": "wb-1", "workbook_name": "Sales A", "project": "P",
         "source_id": "wb-1", "datasource_name": "Superstore (copy)", "datasource_id": "e-1",
         "fields": [{"name": "OrderId", "dataType": "STRING"}, {"name": "NetSales", "dataType": "REAL"}],
         "sources": [{"connectionType": "sqlserver", "database": "SalesDB", "schema": "dbo", "table": "Orders"}],
         "objects": [], "has_extract": None, "source_path": "metadata"},
        {"workbook_luid": "wb-2", "workbook_name": "Sales B", "project": "P",
         "source_id": "wb-2", "datasource_name": "Superstore (copy)", "datasource_id": "e-2",
         "fields": [{"name": "OrderId", "dataType": "STRING"}, {"name": "NetSales", "dataType": "REAL"}],
         "sources": [{"connectionType": "sqlserver", "database": "SalesDB", "schema": "dbo", "table": "Orders"}],
         "objects": [], "has_extract": None, "source_path": "metadata"},
    ]
    t_json = _write(tmp_path, "tableau.json", tableau)
    f_json = _write(tmp_path, "fabric.json", fabric)
    e_json = _write(tmp_path, "embedded.json", embedded)
    out = tmp_path / "result.json"
    plan_out = tmp_path / "rebind-plan.json"
    plan_md = tmp_path / "rebind-plan.md"
    plan_csv = tmp_path / "rebind-plan.csv"

    rc = compare_estate.main([
        "--tableau-inventory-json", t_json,
        "--fabric-inventory-json", f_json,
        "--embedded-inventory-json", e_json,
        "--rebind-plan-out", str(plan_out),
        "--rebind-plan-md", str(plan_md),
        "--rebind-plan-csv", str(plan_csv),
        "--format", "json", "--out", str(out),
    ])
    assert rc == 0
    plan = json.loads(plan_out.read_text(encoding="utf-8"))
    assert plan["schema_version"] == "1.0"
    assert plan["summary"]["embedded_total"] == 2
    actions = {e["action"] for e in plan["plan"]}
    assert actions == {"rebind_to_published"}
    # The luid <-> source_id linkage is carried explicitly.
    assert {m["source_id"] for m in plan["source_map"]} == {"wb-1", "wb-2"}

    assert "# Embedded-datasource rebind plan" in plan_md.read_text(encoding="utf-8")
    csv_rows = list(_csv.reader(_io.StringIO(plan_csv.read_text(encoding="utf-8"))))
    assert csv_rows[0][0] == "Workbook"
    assert len(csv_rows) == 3  # header + 2 entries



def test_verify_with_cached_tableau_degrades_to_skip(tmp_path):
    # --verify needs a live Tableau client (VDS); a cached inventory cannot be probed.
    tableau = [{"name": "Superstore", "project": "S", "luid": "t-1",
                "fields": [{"name": "Sales", "dataType": "REAL"}]}]
    fabric = [{"name": "Superstore", "workspace": "WS", "workspaceId": "w-1", "id": "m-1",
               "tables": ["Orders"], "columns": [{"name": "Sales", "dataType": "double", "table": "Orders"}]}]
    out = tmp_path / "r.json"
    rc = compare_estate.main([
        "--tableau-inventory-json", _write(tmp_path, "t.json", tableau),
        "--fabric-inventory-json", _write(tmp_path, "f.json", fabric),
        "--verify", "--format", "json", "--out", str(out),
    ])
    assert rc == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    v = result["summary"]["verification"]
    assert v["enabled"] is False
    assert "live Tableau" in v["reason"]


def test_run_verification_live_path_with_fakes(monkeypatch):
    # Exercise the CLI's probe closures end-to-end with a fake client + fake executeQueries.
    import fabric_inventory as fab

    class FakeClient:
        def vds_query(self, luid, query):
            func = query["fields"][0]["function"]
            windowed = "filters" in query
            table = {("MIN", False): [{"a0": "2021-01-01"}], ("MAX", False): [{"a0": "2026-12-31"}]}
            if (func, windowed) in table:
                return table[(func, windowed)]
            return [{"a0": 1000}]  # any windowed aggregate

    def fake_execute_dax(token, ws, ds, dax, *a, **k):
        if "MIN(" in dax:
            return 200, {"results": [{"tables": [{"rows": [{"[v]": "2019-01-01"}]}]}]}
        if "MAX(" in dax:
            return 200, {"results": [{"tables": [{"rows": [{"[v]": "2026-12-31"}]}]}]}
        return 200, {"results": [{"tables": [{"rows": [{"[v]": 1000}]}]}]}

    monkeypatch.setattr(fab, "acquire_powerbi_token", lambda explicit, use_az: "pbi-token")
    monkeypatch.setattr(fab, "execute_dax", fake_execute_dax)

    result = {
        "summary": {},
        "matches": [{"tableau_name": "DS1", "tableau_luid": "t-1", "bucket": "already_exists",
                     "tier": "Exact", "score": 0.9,
                     "best_match": {"fabric_name": "M1", "fabric_id": "m-1",
                                    "workspace": "WS", "workspace_id": "w-1"}}],
    }
    tableau = [{"name": "DS1", "luid": "t-1", "fields": [
        {"name": "Order Date", "dataType": "DATE"}, {"name": "Sales", "dataType": "REAL"}]}]
    fabric = [{"name": "M1", "id": "m-1", "workspace": "WS", "workspaceId": "w-1", "columns": [
        {"name": "Order Date", "dataType": "datetime", "table": "Orders"},
        {"name": "Sales", "dataType": "double", "table": "Orders"}]}]

    class Args:
        powerbi_token = None
        use_az = False
        verify_top_n = 10
        verify_max_cols = 4
        verify_rtol = 0.01

    compare_estate._run_verification(Args(), result, tableau, fabric, FakeClient(), lambda *_: None)
    assert result["summary"]["verification"]["enabled"] is True
    assert result["matches"][0]["verification"]["verdict"] == "verified"
