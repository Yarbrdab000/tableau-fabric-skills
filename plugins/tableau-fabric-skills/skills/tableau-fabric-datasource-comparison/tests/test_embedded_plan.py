"""Tests for ``embedded_plan.py`` -- the rebind-plan emitter (schema_version "1.0")."""
import csv

import embedded_cluster as ec
import embedded_score as es
import embedded_plan as ep


SUPER_FIELDS = ["OrderId", "NetSales", "GrossProfit", "ShipRegion", "ProductCategory"]


def _embedded(sid, ds, fields, tables, objects=None, luid=None):
    return {
        "workbook_luid": sid if luid is None else luid, "workbook_name": f"WB {sid}", "project": "P",
        "source_id": sid, "datasource_name": ds, "datasource_id": ds,
        "fields": [{"name": f, "dataType": "STRING", "role": "", "is_calculated": False}
                   for f in fields],
        "sources": [{"connectionType": "sqlserver", "database": "DB", "schema": "dbo", "table": t}
                    for t in tables],
        "objects": objects or [], "has_extract": None, "source_path": "metadata",
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


def _plan(rows, fabric=None, published=None, **kw):
    return ep.generate_plan(rows, fabric=fabric, published=published, **kw)


def test_schema_version_and_required_keys():
    rows = [_embedded("w1", "Lonely", ["Unique1", "Unique2"], ["Tbl1"])]
    plan = _plan(rows)
    assert plan["schema_version"] == "1.0"
    assert plan["summary"]["schema_version"] == "1.0"
    e = plan["plan"][0]
    for k in ("workbook_luid", "source_ref", "action", "model_id",
              "binding_status", "binding_target", "evidence", "caveats"):
        assert k in e


def test_existing_fabric_reuse_binds_byconnection_and_excludes_rebuild():
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"])]
    fabric = [_fabric("Superstore", SUPER_FIELDS, ["Orders"], fid="ds-77", wsid="ws-9")]
    plan = _plan(rows, fabric=fabric)
    e = plan["plan"][0]
    assert e["binding_status"] == "existing_fabric"
    assert e["action"] == "rebind_to_rebuilt"
    bt = e["binding_target"]
    assert bt["kind"] == "byConnection"
    assert bt["workspace_id"] == "ws-9"
    assert bt["semantic_model_id"] == "ds-77"
    assert bt["dataset_name"] == "Superstore"
    # The model registry carries the existing-Fabric origin + connection identity (Gate 2).
    mid = e["model_id"]
    assert plan["models"][mid]["origin"] == "existing_fabric"
    assert plan["models"][mid]["connection"]["semantic_model_id"] == "ds-77"
    assert plan["summary"]["existing_fabric_reuse"] == 1
    assert any("Gate 2" in c for c in e["caveats"])


def test_rebind_to_published():
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"])]
    published = [_published("Superstore", SUPER_FIELDS, ["Orders"], luid="pub-5")]
    plan = _plan(rows, published=published)
    e = plan["plan"][0]
    assert e["action"] == "rebind_to_published"
    assert e["binding_status"] == "built_local"
    assert e["model_id"] == "mdl-published-pub-5"
    assert e["binding_target"]["kind"] == "byPath"
    assert e["binding_target"]["model_path"] is None


def test_consolidate_then_rebind_to_rebuilt():
    rows = [
        _embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"]),
        _embedded("w2", "Superstore", SUPER_FIELDS, ["Orders"]),
        _embedded("w3", "Superstore", SUPER_FIELDS, ["Orders"]),
    ]
    plan = _plan(rows)
    actions = [e["action"] for e in plan["plan"]]
    assert actions.count("consolidate_new_model") == 1
    assert actions.count("rebind_to_rebuilt") == 2
    model_ids = {e["model_id"] for e in plan["plan"]}
    assert model_ids == {"mdl-cluster-ec-001"}
    assert plan["summary"]["consolidated_model_total"] == 1
    assert plan["summary"]["consolidated_members"] == 3


def test_convert_embedded_singleton():
    rows = [_embedded("w1", "OneOff", ["Weird1", "Weird2", "Weird3"], ["WeirdTbl"])]
    plan = _plan(rows)
    e = plan["plan"][0]
    assert e["action"] == "convert_embedded"
    assert e["binding_status"] == "built_local"
    assert e["model_id"] == "mdl-embedded-ec-001"


def test_needs_attention_for_empty_datasource():
    rows = [_embedded("w1", "Empty", [], [])]
    plan = _plan(rows)
    e = plan["plan"][0]
    assert e["binding_status"] == "needs_attention"
    assert e["binding_target"]["kind"] == "unbound"


def test_bindings_reserve_optional_date_table_slot():
    # Contract 1.0 optional `date_table` (safe-default null) is reserved on every bound target:
    # byConnection (existing_fabric, enriched later from the Fabric inventory) and byPath
    # (rebuilt / consolidated, written back by the calc-compiler). The unbound target omits it.
    rows = [
        _embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"]),          # byConnection
        _embedded("w2", "OneOff", ["Weird1", "Weird2", "Weird3"], ["WeirdTbl"]),  # byPath
        _embedded("w3", "Empty", [], []),                                 # unbound
    ]
    fabric = [_fabric("Superstore", SUPER_FIELDS, ["Orders"], fid="ds-77", wsid="ws-9")]
    plan = _plan(rows, fabric=fabric)
    by_kind = {e["binding_target"]["kind"]: e["binding_target"] for e in plan["plan"]}
    assert by_kind["byConnection"]["date_table"] is None
    assert by_kind["byPath"]["date_table"] is None
    assert "date_table" not in by_kind["unbound"]


def test_gate1_downgrade_preserves_date_table_slot():
    objs = [{"name": "Profit Ratio", "kind": "calc"}]
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"], objects=objs)]
    published = [_published("Superstore", SUPER_FIELDS, ["Orders"], luid="pub-5")]
    plan = _plan(rows, published=published)
    report = {"w1": {"dropped": [{"name": "Profit Ratio"}]}}
    ep.apply_view_dependency_feedback(plan, report)
    e = plan["plan"][0]
    assert e["action"] == "convert_embedded"
    assert e["binding_target"]["kind"] == "byPath"
    assert e["binding_target"]["date_table"] is None


def test_source_map_carries_luid_and_source_id_distinctly():
    # Local-files style: source_id is the filename, workbook_luid is empty -> source_id != luid.
    rows = [_embedded("dash.twb", "DS", ["A1", "B1"], ["T1"], luid="")]
    plan = _plan(rows)
    sm = {m["source_id"]: m["workbook_luid"] for m in plan["source_map"]}
    assert sm == {"dash.twb": ""}
    assert plan["plan"][0]["source_ref"] == "dash.twb"
    assert plan["plan"][0]["workbook_luid"] == ""


def test_headline_and_summary_counts():
    rows = [
        _embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"]),     # -> existing_fabric
        _embedded("w2", "HR", ["EmpKeyId", "HireDt", "DeptNm"], ["Emp"]),  # -> convert
    ]
    fabric = [_fabric("Superstore", SUPER_FIELDS, ["Orders"])]
    plan = _plan(rows, fabric=fabric)
    s = plan["summary"]
    assert s["embedded_total"] == 2
    assert s["workbook_total"] == 2
    assert s["existing_fabric_reuse"] == 1
    assert s["convert_in_place"] == 1
    assert "embedded datasource" in s["headline"]


def test_generate_plan_matches_manual_chain():
    rows = [
        _embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"]),
        _embedded("w2", "Superstore", SUPER_FIELDS, ["Orders"]),
    ]
    fabric = [_fabric("Superstore", SUPER_FIELDS, ["Orders"])]
    clusters = ec.cluster_embedded(rows)
    scored = es.score_embedded(rows, fabric=fabric)
    manual = ep.build_rebind_plan(rows, clusters, scored)
    auto = ep.generate_plan(rows, fabric=fabric)
    assert auto["plan"] == manual["plan"]


# ----- Gate 1: view-dependency feedback ------------------------------------------------
def test_gate1_downgrades_when_dropped_object_present_in_embedded_source():
    objs = [{"name": "Profit Ratio", "kind": "calc"}]
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"], objects=objs)]
    published = [_published("Superstore", SUPER_FIELDS, ["Orders"], luid="pub-5")]
    plan = _plan(rows, published=published)
    assert plan["plan"][0]["action"] == "rebind_to_published"

    report = {"w1": {"refs_total": 10, "refs_dropped": 1,
                     "dropped": [{"name": "Profit Ratio"}], "visuals_emptied": 0}}
    ep.apply_view_dependency_feedback(plan, report)
    e = plan["plan"][0]
    assert e["action"] == "convert_embedded"
    assert e["binding_status"] == "built_local"
    assert e["model_id"] == "mdl-embedded-ec-001"
    assert plan["summary"]["gate1_downgrades"] == 1
    assert any("Gate 1" in c for c in e["caveats"])


def test_gate1_no_downgrade_when_drop_absent_from_embedded_source():
    objs = [{"name": "Profit Ratio", "kind": "calc"}]
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"], objects=objs)]
    published = [_published("Superstore", SUPER_FIELDS, ["Orders"], luid="pub-5")]
    plan = _plan(rows, published=published)
    # Dropped ref is NOT an object the embedded datasource contains -> convert reproduces same stub.
    report = {"w1": {"dropped": [{"name": "Some Published-Only Measure"}]}}
    ep.apply_view_dependency_feedback(plan, report)
    assert plan["plan"][0]["action"] == "rebind_to_published"
    assert "gate1_downgrades" not in plan["summary"]


def test_gate1_accepts_bindings_list_form():
    objs = [{"name": "Region Set", "kind": "set"}]
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"], objects=objs)]
    published = [_published("Superstore", SUPER_FIELDS, ["Orders"], luid="pub-5")]
    plan = _plan(rows, published=published)
    report = {"bindings": [{"workbook_luid": "w1", "dropped": ["Region Set"]}]}
    ep.apply_view_dependency_feedback(plan, report)
    assert plan["plan"][0]["action"] == "convert_embedded"


# ----- renderings ----------------------------------------------------------------------
def test_render_markdown_contains_headline_and_tables():
    rows = [
        _embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"]),
        _embedded("w2", "Superstore", SUPER_FIELDS, ["Orders"]),
    ]
    plan = _plan(rows)
    md = ep.render_markdown(plan)
    assert "# Embedded-datasource rebind plan" in md
    assert "schema_version 1.0" in md
    assert "## By action" in md
    assert "## Per-workbook plan" in md
    assert "Duplicate groups" in md          # the 2-member cluster surfaces


def test_write_export_csv(tmp_path):
    rows = [_embedded("w1", "Superstore", SUPER_FIELDS, ["Orders"])]
    fabric = [_fabric("Superstore", SUPER_FIELDS, ["Orders"], wsid="ws-3")]
    plan = _plan(rows, fabric=fabric)
    out = tmp_path / "rebind.csv"
    ep.write_export_csv(plan, str(out))
    with open(out, newline="", encoding="utf-8") as fh:
        data = list(csv.reader(fh))
    assert data[0][0] == "Workbook"
    assert "Fabric tier" in data[0]
    assert data[1][data[0].index("Action")] == "rebind_to_rebuilt"
    assert data[1][data[0].index("Fabric tier")] == "Exact"
