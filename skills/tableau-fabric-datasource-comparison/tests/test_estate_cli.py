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
