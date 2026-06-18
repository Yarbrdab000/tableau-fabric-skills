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
