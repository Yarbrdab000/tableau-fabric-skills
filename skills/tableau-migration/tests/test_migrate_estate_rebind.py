"""Orchestrator/router tests for the opt-in ``rebind_plan=`` path of ``migrate_estate``.

Fully offline and self-contained: the ``.tds`` samples are authored inline (the repo deliberately
git-ignores Tableau artifacts), and the run is driven through ``InMemoryTableauSource``. These tests
assert the byte-identical no-op guarantee when no plan is given, the deterministic routing by
``binding_status``, model resolution + write-back, the shared ``used_folders`` accumulator, the
date-table echo, and graceful degradation (missing seam, schema mismatch, malformed entries, BOM).

The dashboard-migration stage owns the real per-report bind function; here it is injected as a fake
``rebind_bind_stage`` so the router is exercised without that seam being merged.
"""
import json
import os

import pytest

import migrate_estate as me
from migrate_estate import InMemoryTableauSource, migrate_estate


# -- authored samples (no third-party data) -----------------------------------
# Clean SQL Server datasource -> migrates to an Import/DirectQuery model.
ORDERS_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Orders DS' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.k'>
        <connection class='sqlserver' dbname='Shop' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.k' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Revenue</remote-name><local-name>[Revenue]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Revenue Sum' datatype='real' name='[c1]' role='measure'>
    <calculation class='tableau' formula='SUM([Revenue])' />
  </column>
</datasource>"""

# SAP HANA datasource with no resolvable columns -> storage-mode needs-storage-decision fallback.
FALLBACK_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Inventory Feed' version='18.1'>
  <connection class='saphana'>
    <named-connections>
      <named-connection caption='hana' name='saphana.x'>
        <connection class='saphana' server='hana.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='saphana.x' name='Stock' table='SCHEMA.STOCK' type='table' />
  </connection>
</datasource>"""


def _src():
    return InMemoryTableauSource(datasources={"Orders DS": ORDERS_TDS,
                                              "Inventory Feed": FALLBACK_TDS})


def _entry(source_id, label, status, action, *, model_id=None, target=None, workbook_luid=None):
    # schema_version "1.0": source_ref is the bare source_id STRING; label / workbook_luid /
    # model_id are top-level entry siblings (never folded into source_ref).
    entry = {
        "source_ref": source_id,
        "workbook_luid": workbook_luid if workbook_luid is not None else f"wb-{source_id}",
        "binding_status": status,
        "action": action,
    }
    if label is not None:
        entry["label"] = label
    if model_id is not None:
        entry["model_id"] = model_id
    if target is not None:
        entry["binding_target"] = target
    if status == "built_local":
        entry.setdefault("date_table", None)
    return entry


def _fake_bind(**kw):
    """Stand-in for the dashboard bind fn: mints a deduped report folder from the shared accumulator
    and returns it (plus a resolved date table) the way the real bind fn will."""
    seed = kw["entry"].get("label") or "Report"
    folder = me._safe_folder(seed, kw["used_folders"]) + ".Report"
    return {"resolved_report_folder": f"reports/{folder}", "date_table": "Calendar"}


def _plan(*entries):
    return {"schema_version": "1.0", "plan": list(entries)}


def _compile_report(out):
    return json.load(open(os.path.join(out, "compile-report.json"), encoding="utf-8"))


# -- byte-identical no-op guarantee -------------------------------------------
def test_rebind_plan_absent_writes_no_compile_report(tmp_path):
    out = str(tmp_path / "b")
    migrate_estate(_src(), out)
    assert not os.path.exists(os.path.join(out, "compile-report.json"))


def test_rebind_opt_in_does_not_change_canonical_report(tmp_path):
    # Providing a plan must not alter report.json (modulo the run timestamp) or the semantic_models
    # tree -- the rebind output lands only in the separate compile-report.json.
    plain = str(tmp_path / "plain")
    withp = str(tmp_path / "withp")
    migrate_estate(_src(), plain)
    migrate_estate(_src(), withp,
                   rebind_plan=_plan(_entry("s-1", "Orders DS", "built_local", "rebind_to_rebuilt",
                                            model_id="m-1", target={"model_path": None})))

    a = json.load(open(os.path.join(plain, "report.json"), encoding="utf-8"))
    b = json.load(open(os.path.join(withp, "report.json"), encoding="utf-8"))
    a.pop("generated_at"); b.pop("generated_at")
    assert a == b
    assert (tmp_path / "withp" / "semantic_models" / "Orders DS.SemanticModel").is_dir()


# -- deferral when the bind seam is unavailable -------------------------------
def test_rebind_no_bind_seam_defers_every_routed_entry(tmp_path):
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_plan=_plan(
        _entry("s-1", "Orders DS", "built_local", "rebind_to_rebuilt", model_id="m-1"),
        _entry("s-2", "Sales DB", "existing_fabric", "rebind_to_rebuilt", model_id="m-2",
               target={"kind": "byConnection", "workspace_id": "w", "semantic_model_id": "sm",
                       "dataset_name": "Sales DB"}),
    ))
    cr = _compile_report(out)
    reasons = [d["reason"] for d in cr["deferred"]]
    assert len(cr["deferred"]) == 2
    assert all("bind seam unavailable" in r for r in reasons)
    assert cr["workbooks"] == []
    assert cr["routing"]["by_binding_status"] == {"built_local": 1, "existing_fabric": 1}


# -- routing / partition by binding_status ------------------------------------
def test_rebind_partitions_by_binding_status_first(tmp_path):
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=_plan(
        _entry("s-1", "Orders DS", "built_local", "rebind_to_rebuilt", model_id="m-1",
               target={"model_path": None}),
        _entry("s-2", None, "existing_fabric", "rebind_to_rebuilt", model_id="m-2",
               target={"kind": "byConnection", "workspace_id": "w", "semantic_model_id": "sm",
                       "dataset_name": "Sales DB"}),
        _entry("s-3", "Unmapped", "needs_attention", "rebind_to_rebuilt"),
    ))
    cr = _compile_report(out)

    models = {m["model_id"]: m for m in cr["models"]}
    # built_local -> byPath: reuse the model the estate pass already wrote (real model_path)
    assert models["m-1"]["resolved_model_name"] == "Orders DS"
    assert models["m-1"]["model_path"] == "semantic_models/Orders DS.SemanticModel"
    # existing_fabric -> byConnection identity: no local model_path, dataset_name carried through
    assert models["m-2"]["model_path"] is None
    assert models["m-2"]["resolved_model_name"] == "Sales DB"
    # needs_attention -> deferred (the DEFER key, not an action)
    assert [d["source_id"] for d in cr["deferred"]] == ["s-3"]
    assert "needs_attention" in cr["deferred"][0]["reason"]
    bound = {w["source_id"] for w in cr["workbooks"]}
    assert bound == {"s-1", "s-2"}  # s-1 and s-2 bound, s-3 deferred


def test_rebind_built_local_storage_fallback_yields_null_model_path(tmp_path):
    # A built_local entry whose source storage-mode-falls-back resolves through migrate_datasource
    # and yields model_path=None (the contract's storage-fallback rule), still binding the report.
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=_plan(
        _entry("s-9", "Inventory Feed", "built_local", "rebind_to_rebuilt", model_id="m-9",
               target={"model_path": None}),
    ))
    cr = _compile_report(out)
    models = {m["model_id"]: m for m in cr["models"]}
    assert models["m-9"]["model_path"] is None
    assert any(w["source_id"] == "s-9" for w in cr["workbooks"])


# -- write-back: resolved_report_folder under BOTH keys + date echo -----------
def test_rebind_resolved_report_folder_indexed_by_both_keys(tmp_path):
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=_plan(
        _entry("s-1", "Orders DS", "built_local", "rebind_to_rebuilt", model_id="m-1",
               target={"model_path": None}),
    ))
    cr = _compile_report(out)
    wb = cr["workbooks"][0]
    folder = wb["resolved_report_folder"]
    assert folder.startswith("reports/") and folder.endswith(".Report")
    assert cr["resolved_report_folders"]["by_source_id"]["s-1"] == folder
    assert cr["resolved_report_folders"]["by_workbook_luid"]["wb-s-1"] == folder
    assert wb["bound_model_id"] == "m-1"
    assert wb["source_id"] == "s-1"          # join key echoed straight through


def test_rebind_echoes_date_table_only_for_rebuild_actions(tmp_path):
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=_plan(
        _entry("s-1", "Orders DS", "built_local", "rebind_to_rebuilt", model_id="m-1",
               target={"model_path": None}),
        _entry("s-2", None, "existing_fabric", "rebind_to_rebuilt", model_id="m-2",
               target={"kind": "byConnection", "workspace_id": "w", "semantic_model_id": "sm",
                       "dataset_name": "Sales DB"}),
    ))
    cr = _compile_report(out)
    by_src = {w["source_id"]: w for w in cr["workbooks"]}
    assert by_src["s-1"]["date_table"] == "Calendar"      # built_local byPath rebuild -> echoed
    assert "date_table" not in by_src["s-2"]              # existing_fabric byConnection -> not echoed


def test_rebind_consolidate_new_model_echoes_date_table(tmp_path):
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=_plan(
        _entry("s-1", "Orders DS", "built_local", "consolidate_new_model", model_id="m-1",
               target={"model_path": None}),
    ))
    cr = _compile_report(out)
    assert cr["workbooks"][0]["date_table"] == "Calendar"


# -- shared used_folders accumulator ------------------------------------------
def test_rebind_shares_used_folders_with_estate_pass(tmp_path):
    # The estate pass already minted "Orders DS" for the semantic model; the report folder the bind
    # fn mints from the SAME accumulator must dedup against it rather than collide.
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=_plan(
        _entry("s-1", "Orders DS", "built_local", "rebind_to_rebuilt", model_id="m-1",
               target={"model_path": None}),
    ))
    cr = _compile_report(out)
    assert cr["workbooks"][0]["resolved_report_folder"] == "reports/Orders DS_2.Report"


# -- validation / graceful degradation ----------------------------------------
def test_rebind_schema_version_mismatch_is_recorded_not_fatal(tmp_path):
    out = str(tmp_path / "b")
    plan = _plan(_entry("s-1", "Orders DS", "built_local", "rebind_to_rebuilt", model_id="m-1"))
    plan["schema_version"] = "2.0"
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=plan)
    cr = _compile_report(out)
    assert any("schema_version" in e for e in cr["errors"])
    # entries are self-describing, so the run still routes them rather than aborting
    assert cr["workbooks"] or cr["deferred"]


def test_rebind_unreadable_plan_path_is_recorded(tmp_path):
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_plan=str(tmp_path / "missing.json"))
    cr = _compile_report(out)
    assert any("unreadable" in e for e in cr["errors"])
    assert cr["workbooks"] == [] and cr["deferred"] == []


def test_rebind_entry_without_selector_is_deferred(tmp_path):
    out = str(tmp_path / "b")
    bad = {"source_ref": "s-x", "workbook_luid": "wb-x",
           "binding_status": "built_local", "action": "rebind_to_rebuilt", "model_id": "m-x"}
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=_plan(
        bad,
        _entry("s-1", "Orders DS", "built_local", "rebind_to_rebuilt", model_id="m-1",
               target={"model_path": None}),
    ))
    cr = _compile_report(out)
    deferred = {d["source_id"]: d for d in cr["deferred"]}
    assert "s-x" in deferred and "selector" in deferred["s-x"]["reason"]
    assert any(w["source_id"] == "s-1" for w in cr["workbooks"])  # the good entry still binds


def test_rebind_selector_miss_is_deferred(tmp_path):
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=_plan(
        _entry("s-7", "Nonexistent DS", "built_local", "rebind_to_rebuilt", model_id="m-7"),
    ))
    cr = _compile_report(out)
    assert any("Nonexistent DS" in d["reason"] for d in cr["deferred"])


def test_rebind_bind_failure_is_isolated(tmp_path):
    def boom(**kw):
        raise RuntimeError("bind exploded")
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_bind_stage=boom, rebind_plan=_plan(
        _entry("s-1", "Orders DS", "built_local", "rebind_to_rebuilt", model_id="m-1",
               target={"model_path": None}),
    ))
    cr = _compile_report(out)
    assert any("bind exploded" in e for e in cr["errors"])
    assert any(d["source_id"] == "s-1" for d in cr["deferred"])


# -- BOM ingest + BOM-free deterministic output -------------------------------
def test_rebind_plan_file_with_bom_is_ingested(tmp_path):
    plan_path = tmp_path / "rebind-plan.json"
    plan = _plan(_entry("s-1", "Orders DS", "built_local", "rebind_to_rebuilt", model_id="m-1",
                        target={"model_path": None}))
    plan_path.write_text(json.dumps(plan), encoding="utf-8-sig")  # write WITH a UTF-8 BOM
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=str(plan_path))
    cr = _compile_report(out)
    assert any(w["source_id"] == "s-1" for w in cr["workbooks"])


def test_rebind_compile_report_is_bom_free_and_deterministic(tmp_path):
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=_plan(
        _entry("s-1", "Orders DS", "built_local", "rebind_to_rebuilt", model_id="m-1",
               target={"model_path": None}),
    ))
    raw = open(os.path.join(out, "compile-report.json"), "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf")             # BOM-free
    text = raw.decode("utf-8")
    keys = list(json.loads(text).keys())
    assert keys == sorted(keys)                            # sort_keys -> deterministic diffs


def test_rebind_string_source_ref_and_models_registry(tmp_path):
    # The contract shape: source_ref is the bare source_id STRING, label/workbook_luid/model_id are
    # top-level siblings, and the plan's models{} registry seeds origin (echoed into compile-report).
    out = str(tmp_path / "b")
    plan = _plan(_entry("s-1", "Orders DS", "built_local", "rebind_to_published", model_id="m-1",
                        target={"kind": "byPath", "model_id": "m-1", "model_path": None}))
    plan["models"] = {"m-1": {"model_id": "m-1", "origin": "published",
                              "resolved_model_name": None, "model_path": None}}
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=plan)
    cr = _compile_report(out)
    m = {mm["model_id"]: mm for mm in cr["models"]}["m-1"]
    assert m["resolved_model_name"] == "Orders DS"
    assert m["model_path"] == "semantic_models/Orders DS.SemanticModel"
    assert m["origin"] == "published"                     # seeded by the registry, written back
    wb = cr["workbooks"][0]
    assert wb["source_id"] == "s-1" and wb["workbook_luid"] == "wb-s-1"


def test_rebind_landed_to_delta_is_deferred_unbound(tmp_path):
    # landed_to_delta is a write-back state (the calc-compiler's storage fell back): the consumer
    # keys off binding_status FIRST and leaves the report unbound rather than binding a null path.
    out = str(tmp_path / "b")
    migrate_estate(_src(), out, rebind_bind_stage=_fake_bind, rebind_plan=_plan(
        _entry("s-5", "Orders DS", "landed_to_delta", "convert_embedded", model_id="m-5",
               target={"kind": "byPath", "model_id": "m-5", "model_path": None}),
    ))
    cr = _compile_report(out)
    assert [d["source_id"] for d in cr["deferred"]] == ["s-5"]
    assert "landed_to_delta" in cr["deferred"][0]["reason"]
    assert cr["workbooks"] == []
    assert cr["routing"]["by_binding_status"] == {"landed_to_delta": 1}
