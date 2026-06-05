"""Estate-orchestrator tests: enumerate assets -> bundle of semantic models + migration report.

Fully offline and self-contained. The ``.tds`` / ``.twb`` samples are authored inline (the repo
deliberately git-ignores Tableau artifacts as sensitive), and the file-backed adapter tests
materialize them into a temp folder *with a UTF-8 BOM* so the real ``LocalFilesSource`` + utf-8-sig
read path is exercised without committing any artifact files. The orchestrator is driven through
both real adapters and an injected viz stage, asserting on the emitted folder structure, the
machine-readable ``report.json``, fallback handling, the viz seam, and the no-credentials guarantee.
"""
import json
import os

import pytest

import migrate_estate as me
from migrate_estate import (
    InMemoryTableauSource,
    LiveTableauSource,
    LocalFilesSource,
    extract_calculations,
    migrate_estate,
)


# -- authored sample documents (no third-party data) --------------------------
# A live SQL Server datasource with one table and a mix of calculated fields:
# two translatable measures, one table-calc that stubs, one dimension calc and one
# bin that are skipped (and reported).
WIDGET_SALES_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Widget Sales' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='warehouse' name='sqlserver.aa11'>
        <connection authentication='sqlserver' class='sqlserver' dbname='WidgetDW'
                    server='widgetdw.database.windows.net' username='svc_widget' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.aa11' name='Sales' table='[dbo].[Sales]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Amount</remote-name><local-name>[Amount]</local-name>
        <parent-name>[Sales]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Units</remote-name><local-name>[Units]</local-name>
        <parent-name>[Sales]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Category</remote-name><local-name>[Category]</local-name>
        <parent-name>[Sales]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Total Amount' datatype='real' name='[Calculation_001]' role='measure'>
    <calculation class='tableau' formula='SUM([Amount])' />
  </column>
  <column caption='Avg Price' datatype='real' name='[Calculation_002]' role='measure'>
    <calculation class='tableau' formula='SUM([Amount])/SUM([Units])' />
  </column>
  <column caption='Running Amount' datatype='real' name='[Calculation_003]' role='measure'>
    <calculation class='tableau' formula='RUNNING_SUM(SUM([Amount]))' />
  </column>
  <column caption='Category Label' datatype='string' name='[Calculation_004]' role='dimension'>
    <calculation class='tableau' formula='[Category] + &quot; (cat)&quot;' />
  </column>
  <column caption='Amount Bin' datatype='integer' name='[Calculation_005]' role='dimension'>
    <calculation class='categorical-bin' />
  </column>
</datasource>"""

# An unmapped connector class -> land-to-Delta + DirectLake fallback.
INVENTORY_FEED_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Inventory Feed' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='hanaprod' name='saphana.bb22'>
        <connection class='saphana' dbname='INVENTORY' server='hana.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='saphana.bb22' name='Stock' table='[INV].[Stock]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>SKU</remote-name><local-name>[SKU]</local-name>
        <parent-name>[Stock]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>OnHand</remote-name><local-name>[OnHand]</local-name>
        <parent-name>[Stock]</parent-name><local-type>integer</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

WIDGET_DASHBOARD_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2023.1' version='18.1'>
  <worksheets>
    <worksheet name='Sales by Category'><table /></worksheet>
  </worksheets>
  <dashboards>
    <dashboard name='Overview'><zones><zone name='Sales by Category' /></zones></dashboard>
  </dashboards>
</workbook>"""

# small in-memory-only samples
LIVE_TDS = """<?xml version='1.0' encoding='utf-8' ?>
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
      <metadata-record class='column'>
        <remote-name>Qty</remote-name><local-name>[Qty]</local-name>
        <parent-name>[Orders]</parent-name><local-type>integer</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Revenue Sum' datatype='real' name='[c1]' role='measure'>
    <calculation class='tableau' formula='SUM([Revenue])' />
  </column>
</datasource>"""

UNKNOWN_CONNECTOR_TDS = INVENTORY_FEED_TDS
MALFORMED_TDS = "<datasource><connection class='federated'>  <oops "

EXCEL_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Sheet DS' version='18.1'>
  <connection class='excel-direct' filename='Book.xlsx'>
    <relation name='Data' table='[Data$]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Amount</remote-name><local-name>[Amount]</local-name>
        <parent-name>[Data$]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Two relations whose display names collide case-insensitively (Sales vs sales) -> would
# overwrite the same TMDL part on Windows; must be refused, not silently 'migrated'.
CASE_COLLISION_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Collide DS' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='s' name='sqlserver.c'>
        <connection class='sqlserver' dbname='DB' server='s.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.c' name='Sales' table='[dbo].[Sales]' type='table' />
    <relation connection='sqlserver.c' name='sales' table='[dbo].[SalesLower]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>A</remote-name><local-name>[A]</local-name>
        <parent-name>[Sales]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>B</remote-name><local-name>[B]</local-name>
        <parent-name>[SalesLower]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# A relation whose display name carries a path separator -> path-unsafe TMDL part.
UNSAFE_NAME_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Unsafe DS' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='s' name='sqlserver.u'>
        <connection class='sqlserver' dbname='DB' server='s.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.u' name='Mix/Up' table='[dbo].[Mix]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>A</remote-name><local-name>[A]</local-name>
        <parent-name>[Mix]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


# -- file-backed fixtures (materialized with a BOM, never committed) ----------
@pytest.fixture(scope="module")
def fixtures_dir(tmp_path_factory):
    """Write the authored samples to a temp folder *with a UTF-8 BOM* (like a real Tableau
    export) and return the path. Exercises LocalFilesSource + the utf-8-sig read path without
    committing any .tds/.twb artifact (the repo git-ignores them as sensitive)."""
    root = tmp_path_factory.mktemp("estate_fixtures")
    files = {
        "widget_sales.tds": WIDGET_SALES_TDS,
        "inventory_feed.tds": INVENTORY_FEED_TDS,
        "widget_dashboard.twb": WIDGET_DASHBOARD_TWB,
    }
    for name, text in files.items():
        with open(os.path.join(root, name), "w", encoding="utf-8-sig") as fh:
            fh.write(text)
    return str(root)


# -- calculated-field extraction ----------------------------------------------
def test_extract_calculations_keeps_measures_and_reports_skips():
    calcs, skipped = extract_calculations(WIDGET_SALES_TDS)

    names = [c["name"] for c in calcs]
    assert names == ["Total Amount", "Avg Price", "Running Amount"]
    assert {c["name"]: c["formula"] for c in calcs}["Total Amount"] == "SUM([Amount])"

    skipped_reasons = {s["name"]: s["reason"] for s in skipped}
    assert "role=dimension" in skipped_reasons["Category Label"]
    assert "Amount Bin" in skipped_reasons  # categorical-bin / no formula


def test_extract_calculations_dedupes_and_tolerates_bom_and_garbage():
    dup = ("\ufeff<datasource>"
           "<column caption='M' role='measure'><calculation formula='SUM([X])'/></column>"
           "<column caption='M' role='measure'><calculation formula='SUM([Y])'/></column>"
           "</datasource>")
    calcs, skipped = extract_calculations(dup)
    assert [c["name"] for c in calcs] == ["M"]
    assert any(s["reason"] == "duplicate calculated-field name" for s in skipped)
    # malformed XML never raises -> empty result
    assert extract_calculations("<broken") == ([], [])


# -- LocalFilesSource ----------------------------------------------------------
def test_local_files_source_enumeration_and_naming(fixtures_dir):
    src = LocalFilesSource(fixtures_dir)
    ds = src.list_datasources()
    wb = src.list_workbooks()

    assert [src.asset_name(p) for p in ds] == ["inventory_feed", "widget_sales"]
    assert [src.asset_name(p) for p in wb] == ["widget_dashboard"]
    # reads through the BOM transparently (utf-8-sig)
    assert src.read_datasource(ds[-1]).startswith("<?xml")
    assert src.describe() == {"kind": "LocalFilesSource", "root": fixtures_dir}


# -- full estate run over file-backed fixtures --------------------------------
def test_migrate_estate_local_full(fixtures_dir, tmp_path):
    out = str(tmp_path / "bundle")
    report = migrate_estate(LocalFilesSource(fixtures_dir), out)
    s = report["summary"]

    # counts: one migrated SQL Server DS, one SAP HANA (saphana) fallback, one built workbook
    assert s["datasources_total"] == 2
    assert s["datasources_migrated"] == 1
    assert s["datasources_fallback"] == 1
    assert s["datasources_error"] == 0
    assert s["tables_translated"] == 1
    assert s["columns_translated"] == 3
    assert s["measures_total"] == 3
    assert s["measures_translated"] == 2   # Total Amount, Avg Price
    assert s["measures_stubbed"] == 1      # Running Amount (table calc)
    assert s["storage_modes"] == {"Import": 0, "DirectQuery": 1, "fallback": 1}
    assert s["connectors_seen"] == ["saphana", "sqlserver"]
    assert s["workbooks_total"] == 1
    assert s["workbooks_viz_built"] == 1
    assert s["workbooks_viz_warned"] == 0
    assert s["viz_stage_available"] is True

    # emitted Fabric semantic-model folder layout
    sm = tmp_path / "bundle" / "semantic_models" / "widget_sales.SemanticModel"
    assert (sm / ".platform").is_file()
    assert (sm / "definition.pbism").is_file()
    assert (sm / "definition" / "model.tmdl").is_file()
    assert (sm / "definition" / "tables" / "Sales.tmdl").is_file()
    assert (sm / "definition" / "tables" / "_Measures.tmdl").is_file()

    # the workbook viz stage (Stream B) rebuilt the dashboard into a PBIR report folder
    rep = tmp_path / "bundle" / "reports" / "widget_dashboard.Report"
    assert (rep / "definition.pbir").is_file()

    # report.json + summary.md written to disk and machine-readable
    on_disk = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
    assert on_disk["summary"] == s
    summary_md = (tmp_path / "bundle" / "summary.md").read_text(encoding="utf-8")
    assert "Estate Migration Report" in summary_md
    assert "widget_sales" in summary_md


def test_migrate_estate_records_fallback_with_reason(fixtures_dir, tmp_path):
    report = migrate_estate(LocalFilesSource(fixtures_dir), str(tmp_path / "b"))
    assert len(report["fallbacks"]) == 1
    fb = report["fallbacks"][0]
    assert fb["datasource"] == "inventory_feed"
    assert fb["fallback_path"] == "land-to-delta-directlake"
    assert "saphana" in fb["reason"]

    detail = next(d for d in report["datasources"] if d["name"] == "inventory_feed")
    assert detail["status"] == "fallback"
    assert detail["storage_mode"] is None


def test_migrated_detail_carries_skipped_calcs_and_measures(fixtures_dir, tmp_path):
    report = migrate_estate(LocalFilesSource(fixtures_dir), str(tmp_path / "b"))
    detail = next(d for d in report["datasources"] if d["name"] == "widget_sales")

    assert detail["status"] == "migrated"
    assert detail["storage_mode"] == "DirectQuery"
    assert detail["table_count"] == 1
    assert detail["column_count"] == 3
    skipped = {s["name"] for s in detail["skipped_calcs"]}
    assert {"Category Label", "Amount Bin"} <= skipped
    by_status = {m["measure"]: m["status"] for m in detail["measures"]}
    assert by_status["Total Amount"] == "translated"
    assert by_status["Running Amount"] == "stub"


# -- in-memory fake (the offline double for a live source) --------------------
def test_in_memory_source_drives_orchestrator(tmp_path):
    src = InMemoryTableauSource(
        datasources={"Orders DS": LIVE_TDS, "Legacy DS": UNKNOWN_CONNECTOR_TDS},
        workbooks={},
    )
    report = migrate_estate(src, str(tmp_path / "b"))
    s = report["summary"]
    assert s["datasources_migrated"] == 1
    assert s["datasources_fallback"] == 1
    assert report["source"] == {"kind": "InMemoryTableauSource"}
    assert (tmp_path / "b" / "semantic_models" / "Orders DS.SemanticModel" /
            "definition" / "tables" / "Orders.tmdl").is_file()


def test_malformed_asset_is_isolated_as_error(tmp_path):
    src = InMemoryTableauSource(
        datasources={"Good DS": LIVE_TDS, "Bad DS": MALFORMED_TDS},
    )
    report = migrate_estate(src, str(tmp_path / "b"))
    s = report["summary"]
    assert s["datasources_error"] == 1
    assert s["datasources_migrated"] == 1  # one bad file does not abort the estate
    bad = next(d for d in report["datasources"] if d["name"] == "Bad DS")
    assert bad["status"] == "error"
    assert "error" in bad


def test_empty_source_writes_zeroed_report(tmp_path):
    out = str(tmp_path / "b")
    report = migrate_estate(InMemoryTableauSource(), out)
    s = report["summary"]
    assert s["datasources_total"] == 0
    assert s["workbooks_total"] == 0
    assert s["connectors_seen"] == []
    assert report["fallbacks"] == []
    assert os.path.isfile(os.path.join(out, "report.json"))
    assert os.path.isfile(os.path.join(out, "summary.md"))


def test_flat_file_import_is_partial_migration(tmp_path):
    src = InMemoryTableauSource(datasources={"Sheet DS": EXCEL_TDS})
    report = migrate_estate(src, str(tmp_path / "b"))
    s = report["summary"]
    assert s["datasources_migrated"] == 1
    assert s["datasources_partial"] == 1
    assert s["storage_modes"]["Import"] == 1
    detail = report["datasources"][0]
    assert detail["status"] == "migrated_with_followups"
    assert detail["storage_mode"] == "Import"
    assert detail["fully_supported"] is False
    assert detail["manual_followups"]  # flat-file path / sheet must be set manually


def test_assemble_layer_value_error_is_fallback(tmp_path, monkeypatch):
    # A non-fallback storage decision, but the assembler itself signals a fallback
    # (e.g. "no table produced columns") -> must be classified fallback, not error.
    def boom(descriptor, **kwargs):
        raise ValueError("no table produced columns; fall back to land-to-Delta + DirectLake.")

    monkeypatch.setattr(me, "assemble_import_model", boom)
    src = InMemoryTableauSource(datasources={"Orders DS": LIVE_TDS})
    report = migrate_estate(src, str(tmp_path / "b"))
    assert report["summary"]["datasources_fallback"] == 1
    assert report["summary"]["datasources_error"] == 0
    detail = report["datasources"][0]
    assert detail["status"] == "fallback"
    assert "no table produced columns" in detail["reason"]
    assert report["fallbacks"][0]["fallback_path"] == "land-to-delta-directlake"


def test_case_insensitive_table_name_collision_is_error(tmp_path):
    src = InMemoryTableauSource(datasources={"Collide DS": CASE_COLLISION_TDS})
    report = migrate_estate(src, str(tmp_path / "b"))
    assert report["summary"]["datasources_error"] == 1
    assert report["summary"]["datasources_migrated"] == 0
    detail = report["datasources"][0]
    assert detail["status"] == "error"
    assert "duplicate table display names" in detail["error"]


def test_path_unsafe_table_name_is_error(tmp_path):
    src = InMemoryTableauSource(datasources={"Unsafe DS": UNSAFE_NAME_TDS})
    report = migrate_estate(src, str(tmp_path / "b"))
    detail = report["datasources"][0]
    assert detail["status"] == "error"
    assert "path-unsafe table display names" in detail["error"]
    assert not (tmp_path / "b" / "semantic_models").exists()


# -- viz stage (optional, pluggable) ------------------------------------------
def test_viz_stage_absent_warns(tmp_path, monkeypatch):
    # Stream B's twb_to_pbir now ships in this repo, so explicitly force the "viz stage
    # unavailable" path (no module + none injected) to prove the orchestrator still degrades
    # gracefully into a warning rather than failing.
    monkeypatch.setattr(me, "_resolve_viz_stage", lambda injected: injected)
    src = InMemoryTableauSource(workbooks={"Dash": "<workbook/>"})
    report = migrate_estate(src, str(tmp_path / "b"))  # none injected -> viz None
    wb = report["workbooks"][0]
    assert wb["viz_status"] == "warned"
    assert "not available" in wb["note"]
    assert report["summary"]["viz_stage_available"] is False


def test_viz_stage_injected_builds_and_writes_parts(tmp_path):
    captured = {}

    def fake_viz(text, name):
        captured["called"] = (text, name)
        return {"parts": {"definition/report.json": "{}"}, "note": "rebuilt 1 sheet"}

    src = InMemoryTableauSource(workbooks={"Dash": "<workbook>x</workbook>"})
    report = migrate_estate(src, str(tmp_path / "b"), viz_stage=fake_viz)
    s = report["summary"]

    assert s["workbooks_viz_built"] == 1
    assert s["viz_stage_available"] is True
    assert captured["called"] == ("<workbook>x</workbook>", "Dash")
    wb = report["workbooks"][0]
    assert wb["viz_status"] == "built"
    assert wb["output_folder"] == "reports/Dash.Report"
    assert (tmp_path / "b" / "reports" / "Dash.Report" / "definition" / "report.json").is_file()


def test_viz_stage_without_parts_builds_no_folder(tmp_path):
    src = InMemoryTableauSource(workbooks={"Dash": "<workbook/>"})
    report = migrate_estate(src, str(tmp_path / "b"), viz_stage=lambda t, n: {"note": "noted"})
    wb = report["workbooks"][0]
    assert wb["viz_status"] == "built"
    assert wb["output_folder"] is None
    assert not (tmp_path / "b" / "reports").exists()


def test_viz_stage_failure_isolated_as_error(tmp_path):
    def boom(text, name):
        raise RuntimeError("viz exploded")

    src = InMemoryTableauSource(workbooks={"Dash": "<workbook/>"})
    report = migrate_estate(src, str(tmp_path / "b"), viz_stage=boom)
    wb = report["workbooks"][0]
    assert wb["viz_status"] == "error"
    assert "viz exploded" in wb["note"]


# -- LiveTableauSource seam ---------------------------------------------------
_LIVE_ENV_VARS = (
    "TABLEAU_SERVER_URL", "TABLEAU_SITE", "TABLEAU_MIGRATION_KEYVAULT",
    "TABLEAU_MIGRATION_PAT_SECRET", "TABLEAU_MIGRATION_PAT_NAME",
    "FABRIC_WORKSPACE", "TABLEAU_DATASOURCE_NAMES", "TABLEAU_WORKBOOK_NAMES",
)


@pytest.fixture
def clean_live_env(monkeypatch):
    """Clear every LiveTableauSource env var so config tests don't pick up the real shell."""
    for key in _LIVE_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_live_source_is_a_seam_with_no_network(clean_live_env):
    live = LiveTableauSource(
        server_url="https://tableau.example.com", site="finance",
        key_vault_name="vault-x", pat_secret_name="pat-secret", pat_name="migrator",
        datasource_names=["Superstore"], workbook_names=["Sales Dashboard"],
        fabric_workspace="workspace-x",
    )
    # constructing it performs no I/O; config is retained
    assert live.server_url == "https://tableau.example.com"
    assert live.site == "finance"
    assert live.key_vault_name == "vault-x"
    assert live.pat_secret_name == "pat-secret"
    assert live.fabric_workspace == "workspace-x"
    assert live.datasource_names == ["Superstore"]
    assert live.workbook_names == ["Sales Dashboard"]
    # every network-touching method is a seam until implemented
    for call in (live.list_datasources, live.list_workbooks):
        with pytest.raises(NotImplementedError):
            call()
    with pytest.raises(NotImplementedError):
        live.read_datasource("anything")
    with pytest.raises(NotImplementedError):
        live.read_workbook("anything")
    with pytest.raises(NotImplementedError):
        live._resolve_pat()
    with pytest.raises(NotImplementedError):
        live._signin("token-secret")


def test_live_source_describe_exposes_config_without_secrets(clean_live_env):
    live = LiveTableauSource(
        server_url="https://tableau.example.com", site="finance",
        key_vault_name="vault-x", pat_secret_name="pat-secret", pat_name="migrator",
        datasource_names=["Superstore"], fabric_workspace="workspace-x",
    )
    desc = live.describe()
    # describe() is an exact allowlist of names/pointers -- no secret-bearing key can sneak in
    assert set(desc) == {
        "kind", "server_url", "site", "key_vault", "pat_secret_name", "pat_name",
        "fabric_workspace", "datasource_names", "workbook_names", "api_version", "implemented",
    }
    assert desc["kind"] == "LiveTableauSource"
    assert desc["implemented"] is False
    assert desc["key_vault"] == "vault-x"
    assert desc["pat_secret_name"] == "pat-secret"
    assert desc["fabric_workspace"] == "workspace-x"
    assert desc["datasource_names"] == ["Superstore"]
    assert desc["workbook_names"] is None  # omitted + env cleared -> deterministically None
    # only the secret *name* is recorded, never a resolved token / X-Tableau-Auth value
    blob = json.dumps(desc)
    assert "pat-secret" in blob
    assert "X-Tableau-Auth" not in blob


def test_live_source_reads_config_from_environment(clean_live_env):
    clean_live_env.setenv("TABLEAU_SERVER_URL", "https://env.example.com")
    clean_live_env.setenv("TABLEAU_SITE", "env-site")
    clean_live_env.setenv("TABLEAU_MIGRATION_KEYVAULT", "env-vault")
    clean_live_env.setenv("TABLEAU_MIGRATION_PAT_SECRET", "env-secret")
    clean_live_env.setenv("FABRIC_WORKSPACE", "env-workspace")
    clean_live_env.setenv("TABLEAU_DATASOURCE_NAMES", "Superstore, Orders ")
    live = LiveTableauSource()
    assert live.server_url == "https://env.example.com"
    assert live.site == "env-site"
    assert live.key_vault_name == "env-vault"
    assert live.pat_secret_name == "env-secret"
    assert live.fabric_workspace == "env-workspace"
    # comma-separated env list is parsed and trimmed
    assert live.datasource_names == ["Superstore", "Orders"]
    # explicit args win over the environment; an explicit [] suppresses the env filter
    assert LiveTableauSource(server_url="https://explicit").server_url == "https://explicit"
    assert LiveTableauSource(datasource_names=[]).datasource_names == []


def test_select_by_name_filters_catalog_offline():
    catalog = [
        {"id": "luid-1", "name": "Superstore"},
        {"id": "luid-2", "name": "People"},
        {"id": "luid-3", "name": "superstore"},  # case-variant duplicate name
        {"name": "no-id-skipped"},               # missing id -> skipped
    ]
    # case-insensitive match; both Superstore variants returned, sorted by name then id
    picked = LiveTableauSource._select_by_name(catalog, ["superstore"])
    assert picked == [("luid-1", "Superstore"), ("luid-3", "superstore")]
    # a name not present yields nothing
    assert LiveTableauSource._select_by_name(catalog, ["Returns"]) == []
    # no filter (None or all-blank) -> everything with an id, deterministically sorted
    everything = [("luid-2", "People"), ("luid-1", "Superstore"), ("luid-3", "superstore")]
    assert LiveTableauSource._select_by_name(catalog, None) == everything
    assert LiveTableauSource._select_by_name(catalog, ["   "]) == everything


def test_inmemory_source_is_the_live_double(tmp_path):
    # The offline fake stands in for LiveTableauSource so the orchestrator is fully testable.
    src = InMemoryTableauSource(datasources={"Widget Sales": WIDGET_SALES_TDS})
    report = migrate_estate(src, str(tmp_path / "b"))
    assert report["source"]["kind"] == "InMemoryTableauSource"
    assert report["summary"]["datasources_total"] == 1


# -- folder safety / determinism ----------------------------------------------
def test_safe_folder_sanitizes_and_dedupes():
    used = set()
    assert me._safe_folder('A:B*C?', used) == "A_B_C_"
    assert me._safe_folder("Sales", used) == "Sales"
    assert me._safe_folder("Sales", used) == "Sales_2"
    assert me._safe_folder("Sales", used) == "Sales_3"
    assert me._safe_folder("", used) == "datasource"


def test_no_credentials_leak_into_bundle(fixtures_dir, tmp_path):
    # widget_sales.tds carries username='svc_widget'; it must not appear anywhere in the bundle.
    out = str(tmp_path / "b")
    migrate_estate(LocalFilesSource(fixtures_dir), out)
    leaked = []
    for root, _dirs, files in os.walk(out):
        for f in files:
            blob = open(os.path.join(root, f), encoding="utf-8").read()
            if "svc_widget" in blob:
                leaked.append(os.path.join(root, f))
    assert leaked == []


def test_rerun_clears_stale_semantic_model(fixtures_dir, tmp_path):
    out = str(tmp_path / "b")
    src = LocalFilesSource(fixtures_dir)
    migrate_estate(src, out)
    stale = (tmp_path / "b" / "semantic_models" / "widget_sales.SemanticModel" /
             "definition" / "tables" / "Stale.tmdl")
    stale.write_text("stale", encoding="utf-8")
    migrate_estate(src, out)  # rerun must drop the stale part
    assert not stale.exists()


# -- CLI ----------------------------------------------------------------------
def test_cli_main_runs_offline(fixtures_dir, tmp_path, capsys):
    out = str(tmp_path / "b")
    rc = me.main(["-i", fixtures_dir, "-o", out])
    assert rc == 0
    assert os.path.isfile(os.path.join(out, "report.json"))
    printed = capsys.readouterr().out
    assert "Datasources:" in printed
    assert "Bundle written to:" in printed
