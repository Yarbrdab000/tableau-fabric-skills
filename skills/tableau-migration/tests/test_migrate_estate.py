"""Estate-orchestrator tests: enumerate assets -> bundle of semantic models + migration report.

Fully offline and self-contained. The ``.tds`` / ``.twb`` samples are authored inline (the repo
deliberately git-ignores Tableau artifacts as sensitive), and the file-backed adapter tests
materialize them into a temp folder *with a UTF-8 BOM* so the real ``LocalFilesSource`` + utf-8-sig
read path is exercised without committing any artifact files. The orchestrator is driven through
both real adapters and an injected viz stage, asserting on the emitted folder structure, the
machine-readable ``report.json``, fallback handling, the viz seam, and the no-credentials guarantee.
"""
import io
import json
import os
import zipfile

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

# A measure-role swap over AGGREGATIONS driven by a Tableau parameter. Translating it needs the
# what-if "value parameter" table synthesized from the datasource's <column param-domain-type=..>,
# so the estate path must thread the parsed parameters into the assembler exactly like the direct
# migrate_datasource path does -- otherwise the swap measure stubs "parameter ... (unmodeled)".
MEASURE_SWAP_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Swap DS' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='warehouse' name='sqlserver.aa11'>
        <connection authentication='sqlserver' class='sqlserver' dbname='WidgetDW'
                    server='widgetdw.database.windows.net' username='svc_widget' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.aa11' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Profit</remote-name><local-name>[Profit]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Measure Swap' datatype='integer' name='[Param Swap]' param-domain-type='list'
          role='measure' type='quantitative' value='1'>
    <members><member value='1' /><member value='2' /></members>
  </column>
  <column caption='Swap Measure' datatype='real' name='[Calculation_900]' role='measure'>
    <calculation class='tableau'
       formula='case [Parameters].[Param Swap] when 1 then AVG([Sales]) when 2 then AVG([Profit]) end' />
  </column>
</datasource>"""

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


def test_extract_calculations_captures_internal_token_for_binding():
    # The Tableau internal field name (name='[Calculation_xxxx]') is captured as `internal_name` --
    # the deterministic cross-layer join key the viz/report layer binds on (set only when it differs
    # from the caption, matching connection_to_m.extract_calcs). Additive; caption unchanged.
    calcs, _ = extract_calculations(WIDGET_SALES_TDS)
    by_name = {c["name"]: c for c in calcs}
    assert by_name["Total Amount"]["internal_name"] == "Calculation_001"
    assert by_name["Avg Price"]["internal_name"] == "Calculation_002"
    # dimension calcs carry it too (for the calc-column binding path)
    _, _, dim_calcs = extract_calculations(WIDGET_SALES_TDS, include_dimensions=True)
    assert dim_calcs[0]["name"] == "Category Label"
    assert dim_calcs[0]["internal_name"] == "Calculation_004"


def test_extract_calculations_default_shape_unchanged_without_opt_in():
    # The opt-in must not perturb the default: same 2-tuple, same contents.
    assert extract_calculations(WIDGET_SALES_TDS) == extract_calculations(
        WIDGET_SALES_TDS, include_dimensions=False)[:2]
    calcs, skipped = extract_calculations(WIDGET_SALES_TDS)
    assert [c["name"] for c in calcs] == ["Total Amount", "Avg Price", "Running Amount"]
    assert any(s["name"] == "Category Label" for s in skipped)


def test_extract_calculations_include_dimensions_surfaces_dim_calcs():
    calcs, skipped, dim_calcs = extract_calculations(WIDGET_SALES_TDS, include_dimensions=True)

    # Measure path is byte-for-byte identical to the default.
    assert [c["name"] for c in calcs] == ["Total Amount", "Avg Price", "Running Amount"]

    # The dimension calc is now surfaced (not dropped into skipped) with role + formula.
    assert [d["name"] for d in dim_calcs] == ["Category Label"]
    assert dim_calcs[0]["formula"] == '[Category] + " (cat)"'
    assert dim_calcs[0]["role"] == "dimension"
    assert not any(s["name"] == "Category Label" for s in skipped)

    # A dimension-role *bin* is still skipped (caught before the role gate), never a calc column.
    assert any(s["name"] == "Amount Bin" for s in skipped)
    assert "Amount Bin" not in {d["name"] for d in dim_calcs}

    # Malformed XML still never raises -> empty 3-tuple under the opt-in.
    assert extract_calculations("<broken", include_dimensions=True) == ([], [], [])


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


def test_extract_calculations_skips_embedded_parameters():
    # A Tableau parameter embedded in a real datasource as a <column param-domain-type=..>
    # whose <calculation> formula is just its default value -- it must NOT become a measure.
    xml = ("<datasource>"
           "<column caption='Real Measure' role='measure'><calculation formula='SUM([Sales])'/></column>"
           "<column caption='measure parameter' name='[Parameter 1]' role='measure' "
           "param-domain-type='list'><calculation class='tableau' formula='1.'/></column>"
           "</datasource>")
    calcs, skipped = extract_calculations(xml)
    assert [c["name"] for c in calcs] == ["Real Measure"]
    assert any(s["name"] == "measure parameter" and "parameter" in s["reason"].lower()
               for s in skipped)


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


def _packaged_zip_bytes(arcname, text):
    """Pack one BOM-encoded member into an in-memory zip -- a ``.tdsx``/``.twbx`` IS a zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(arcname, text.encode("utf-8-sig"))
    return buf.getvalue()


def test_local_files_source_discovers_packaged_tdsx_and_twbx(tmp_path):
    # A local UPLOAD commonly hands us the PACKAGED exports (.tdsx/.twbx = zip archives); they must
    # work exactly like the bare .tds/.twb a live pull lands (local==live parity). The inner document
    # is extracted from the zip in memory and never written to disk.
    root = tmp_path / "packaged"
    root.mkdir()
    (root / "widget_sales.tdsx").write_bytes(
        _packaged_zip_bytes("widget_sales.tds", WIDGET_SALES_TDS))
    (root / "widget_dashboard.twbx").write_bytes(
        _packaged_zip_bytes("Dashboard/widget_dashboard.twb", WIDGET_DASHBOARD_TWB))

    src = LocalFilesSource(str(root))
    ds = src.list_datasources()
    wb = src.list_workbooks()

    assert [src.asset_name(p) for p in ds] == ["widget_sales"]
    assert [src.asset_name(p) for p in wb] == ["widget_dashboard"]
    assert src.read_datasource(ds[0]).startswith("<?xml")
    assert "Widget Sales" in src.read_datasource(ds[0])
    assert "<workbook" in src.read_workbook(wb[0])


def test_local_files_source_dedups_packaged_and_unpacked_twin(tmp_path):
    # When a packaged export and its unpacked twin coexist, the asset is enumerated ONCE (the
    # unpacked .tds/.twb wins) so the output bundle has no duplicate datasource / name collision.
    root = tmp_path / "mixed"
    root.mkdir()
    with open(root / "widget_sales.tds", "w", encoding="utf-8-sig") as fh:
        fh.write(WIDGET_SALES_TDS)
    (root / "widget_sales.tdsx").write_bytes(
        _packaged_zip_bytes("widget_sales.tds", WIDGET_SALES_TDS))

    ds = LocalFilesSource(str(root)).list_datasources()
    assert [os.path.basename(p) for p in ds] == ["widget_sales.tds"]


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
    assert s["calc_columns_total"] == 1          # Category Label (dimension calc -> calc column)
    assert s["calc_columns_translated"] == 1
    assert s["calc_columns_stubbed"] == 0
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
    # The categorical-bin (no formula) is still skipped, but the dimension calc is no longer
    # dropped: column mode now routes it to a DAX calculated column on its home table.
    assert "Amount Bin" in skipped
    assert "Category Label" not in skipped
    by_status = {m["measure"]: m["status"] for m in detail["measures"]}
    assert by_status["Total Amount"] == "translated"
    assert by_status["Running Amount"] == "stub"

    # Dimension calc -> calculated column on the Sales table (the column-mode wiring, A4).
    calc_cols = {c["column"]: c for c in detail["calc_columns"]}
    assert calc_cols["Category Label"]["table"] == "Sales"
    assert calc_cols["Category Label"]["status"] == "translated"
    assert detail["calc_columns_translated"] == 1
    assert detail["calc_columns_stubbed"] == 0


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


def test_migrate_estate_translates_parameter_swap_measure(tmp_path):
    # Regression guard for the estate-vs-direct wiring gap: a measure swap over aggregations
    # references a Tableau parameter, which only resolves once the assembler is handed the parsed
    # parameters (to synthesize the what-if value table + SWITCH). The estate orchestrator must
    # thread them, so this measure translates here exactly as it does via direct migrate_datasource.
    src = InMemoryTableauSource(datasources={"Swap DS": MEASURE_SWAP_TDS})
    report = migrate_estate(src, str(tmp_path / "b"))

    detail = next(d for d in report["datasources"] if d["name"] == "Swap DS")
    by_measure = {m["measure"]: m for m in detail["measures"]}
    swap = by_measure["Swap Measure"]
    assert swap["status"] == "translated", swap.get("reason")
    assert "SWITCH(" in swap["dax"]
    assert "AVERAGE('Orders'[Sales])" in swap["dax"]
    assert "AVERAGE('Orders'[Profit])" in swap["dax"]
    assert detail["measures_translated"] >= 1


# -- default openable .pbip + end-of-run second-compiler check-in -------------
def test_migrate_estate_emits_openable_pbip_by_default(fixtures_dir, tmp_path):
    # Each migrated datasource additionally gets an openable Power BI project under pbip/<Name>/ so
    # users can double-click straight into Power BI Desktop. The canonical semantic_models/ tree is
    # unaffected -- the .pbip is purely additive.
    out = str(tmp_path / "bundle")
    report = migrate_estate(LocalFilesSource(fixtures_dir), out)

    pbip = tmp_path / "bundle" / "pbip" / "widget_sales"
    assert (pbip / "widget_sales.pbip").is_file()
    assert (pbip / "widget_sales.SemanticModel" / "definition" / "model.tmdl").is_file()
    assert (pbip / "widget_sales.Report" / "definition.pbir").is_file()
    # canonical semantic_models/ output still emitted alongside (additive, not a replacement)
    assert (tmp_path / "bundle" / "semantic_models" / "widget_sales.SemanticModel").is_dir()

    detail = next(d for d in report["datasources"] if d["name"] == "widget_sales")
    assert detail["pbip_folder"] == "pbip/widget_sales/widget_sales.pbip"
    summary_md = (tmp_path / "bundle" / "summary.md").read_text(encoding="utf-8")
    assert "pbip/<Name>/<Name>.pbip" in summary_md  # the "Open locally" note


def test_migrate_estate_no_pbip_suppresses_pbip_tree(fixtures_dir, tmp_path):
    out = str(tmp_path / "bundle")
    report = migrate_estate(LocalFilesSource(fixtures_dir), out, pbip=False)

    assert not (tmp_path / "bundle" / "pbip").exists()
    # opting out of pbip never touches the canonical semantic-model output
    assert (tmp_path / "bundle" / "semantic_models" / "widget_sales.SemanticModel").is_dir()
    detail = next(d for d in report["datasources"] if d["name"] == "widget_sales")
    assert detail["pbip_folder"] is None


def test_migrate_estate_summary_offers_second_compiler_when_stubs_exist(fixtures_dir, tmp_path):
    # The estate threads each datasource's translation_handoff into the report, and summary.md grows
    # a "Next step" section naming every stubbed calc + the second-compiler recipe -- the durable,
    # testable half of the end-of-run check-in.
    out = str(tmp_path / "bundle")
    report = migrate_estate(LocalFilesSource(fixtures_dir), out)

    assert report["summary"]["needs_review_total"] >= 1
    detail = next(d for d in report["datasources"] if d["name"] == "widget_sales")
    handoff = detail["translation_handoff"]
    assert handoff is not None
    assert any(r.get("name") == "Running Amount" for r in handoff.get("needs_review", []))

    summary_md = (tmp_path / "bundle" / "summary.md").read_text(encoding="utf-8")
    assert "## Next step" in summary_md
    assert "Running Amount" in summary_md          # the stubbed calc is named
    assert "check_candidate_dax" in summary_md      # the recipe references the gate
    assert "approved_calc_dax" in summary_md
    assert "second-compiler.md" in summary_md


def test_migrate_estate_summary_omits_next_step_when_no_stubs(tmp_path):
    # A datasource whose calcs all translate has nothing to offer -> no "Next step" section, even
    # though the openable pbip is still emitted (pbip is independent of the stub check-in).
    src = InMemoryTableauSource(datasources={"Orders DS": LIVE_TDS})
    out = str(tmp_path / "b")
    report = migrate_estate(src, out)

    assert report["summary"]["needs_review_total"] == 0
    summary_md = (tmp_path / "b" / "summary.md").read_text(encoding="utf-8")
    assert "## Next step" not in summary_md
    assert (tmp_path / "b" / "pbip" / "Orders DS" / "Orders DS.pbip").is_file()


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


# -- openable workbook .pbip (rebuilt embedded model + bound report) -----------
# A structurally faithful workbook: an embedded SQL Server datasource (so it rebuilds as an Import
# model) plus a worksheet + dashboard that bind to its columns. Driven through the REAL twb_to_pbir
# viz stage (none injected), so these exercise the full workbook -> openable .pbip round-trip.
SUPERSTORE_DASHBOARD_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2023.1' version='18.1'>
  <datasources>
    <datasource caption='Superstore' inline='true' name='federated.abc' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='warehouse' name='sqlserver.aa11'>
            <connection class='sqlserver' dbname='Superstore'
                        server='superstore.database.windows.net' username='svc' />
          </named-connection>
        </named-connections>
        <relation connection='sqlserver.aa11' name='Orders' table='[dbo].[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Category</remote-name><local-name>[Category]</local-name>
            <parent-name>[Orders]</parent-name><local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Sales Amount</remote-name><local-name>[Sales]</local-name>
            <parent-name>[Orders]</parent-name><local-type>real</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Order Date</remote-name><local-name>[Order Date]</local-name>
            <parent-name>[Orders]</parent-name><local-type>datetime</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Sales by Category'>
      <table>
        <view>
          <datasources>
            <datasource caption='Superstore' name='federated.abc' />
          </datasources>
          <datasource-dependencies datasource='federated.abc'>
            <column caption='Category' datatype='string' name='[Category]' role='dimension' type='nominal' />
            <column caption='Sales' datatype='real' name='[Sales]' role='measure' type='quantitative' />
            <column-instance column='[Category]' derivation='None' name='[none:Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
        </view>
        <panes><pane><mark class='Bar' /></pane></panes>
        <rows>[federated.abc].[sum:Sales:qk]</rows>
        <cols>[federated.abc].[none:Category:nk]</cols>
      </table>
    </worksheet>
  </worksheets>
  <dashboards>
    <dashboard name='Overview'>
      <size maxwidth='1200' maxheight='800' />
      <zones>
        <zone name='Sales by Category' x='0' y='0' w='100000' h='100000' />
      </zones>
    </dashboard>
  </dashboards>
</workbook>"""


def _viz_ds(caption, ds_name, conn_name, conn_class, table):
    """An embedded ``<datasource>`` block (single table, two columns) for a workbook fixture."""
    return f"""
    <datasource caption='{caption}' inline='true' name='{ds_name}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='c' name='{conn_name}'>
            <connection class='{conn_class}' dbname='DB' server='srv.example.com' username='svc' />
          </named-connection>
        </named-connections>
        <relation connection='{conn_name}' name='{table}' table='[dbo].[{table}]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Category</remote-name><local-name>[Category]</local-name>
            <parent-name>[{table}]</parent-name><local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Amount</remote-name><local-name>[Amount]</local-name>
            <parent-name>[{table}]</parent-name><local-type>real</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""


def _viz_ws(ws_name, ds_name, caption):
    """A worksheet that binds ``Amount`` (sum) by ``Category`` from the named embedded datasource."""
    return f"""
    <worksheet name='{ws_name}'>
      <table>
        <view>
          <datasources><datasource caption='{caption}' name='{ds_name}' /></datasources>
          <datasource-dependencies datasource='{ds_name}'>
            <column caption='Category' datatype='string' name='[Category]' role='dimension' type='nominal' />
            <column caption='Amount' datatype='real' name='[Amount]' role='measure' type='quantitative' />
            <column-instance column='[Category]' derivation='None' name='[none:Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[Amount]' derivation='Sum' name='[sum:Amount:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
        </view>
        <panes><pane><mark class='Bar' /></pane></panes>
        <rows>[{ds_name}].[sum:Amount:qk]</rows>
        <cols>[{ds_name}].[none:Category:nk]</cols>
      </table>
    </worksheet>"""


def _viz_wb(ds_blocks, ws_blocks):
    return ("<?xml version='1.0' encoding='utf-8' ?>\n"
            "<workbook source-build='2023.1' version='18.1'>"
            + "<datasources>" + ds_blocks + "</datasources>"
            + "<worksheets>" + ws_blocks + "</worksheets>"
            + "</workbook>")


# Embedded SAP HANA datasource -> select_storage_mode routes it to the land-to-Delta fallback, so
# the bound .pbip cannot be assembled (the model lands separately) and must be skipped with a warning.
SAPHANA_WORKBOOK_TWB = _viz_wb(
    _viz_ds("Hana Source", "federated.hana", "saphana.bb22", "saphana", "Stock"),
    _viz_ws("Stock by Category", "federated.hana", "Hana Source"))

# Two embedded SQL Server datasources: a single PBIR report binds one model, so the primary is bound
# and each remaining datasource is reported as a warning (never mis-bound silently).
MULTI_SOURCE_TWB = _viz_wb(
    _viz_ds("Sales Source", "federated.s1", "sqlserver.s1", "sqlserver", "Sales")
    + _viz_ds("Inventory Source", "federated.s2", "sqlserver.s2", "sqlserver", "Inventory"),
    _viz_ws("Sales by Category", "federated.s1", "Sales Source")
    + _viz_ws("Inventory by Category", "federated.s2", "Inventory Source"))


# A workbook whose embedded datasource carries a calculated MEASURE (Profit Ratio) that a worksheet
# puts on a shelf. The estate migration must auto-extract + translate that calc into the emitted model
# AND the rebuilt visual must bind to it -- the regression guard for using migrate_datasource (which
# extracts calcs) over the calc-less migrate_tds_to_semantic_model convenience entry point.
CALC_MEASURE_WORKBOOK_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2023.1' version='18.1'>
  <datasources>
    <datasource caption='Sales' inline='true' name='federated.calc' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='warehouse' name='sqlserver.aa11'>
            <connection class='sqlserver' dbname='Superstore'
                        server='superstore.database.windows.net' username='svc' />
          </named-connection>
        </named-connections>
        <relation connection='sqlserver.aa11' name='Orders' table='[dbo].[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Category</remote-name><local-name>[Category]</local-name>
            <parent-name>[Orders]</parent-name><local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Sales Amount</remote-name><local-name>[Sales]</local-name>
            <parent-name>[Orders]</parent-name><local-type>real</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Profit Amount</remote-name><local-name>[Profit]</local-name>
            <parent-name>[Orders]</parent-name><local-type>real</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
      <column caption='Profit Ratio' datatype='real' name='[Calculation_1]'
              role='measure' type='quantitative'>
        <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />
      </column>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Ratio by Category'>
      <table>
        <view>
          <datasources><datasource caption='Sales' name='federated.calc' /></datasources>
          <datasource-dependencies datasource='federated.calc'>
            <column caption='Category' datatype='string' name='[Category]' role='dimension' type='nominal' />
            <column caption='Profit Ratio' datatype='real' name='[Calculation_1]' role='measure' type='quantitative'>
              <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />
            </column>
            <column-instance column='[Category]' derivation='None' name='[none:Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[Calculation_1]' derivation='None' name='[none:Calculation_1:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
        </view>
        <panes><pane><mark class='Bar' /></pane></panes>
        <rows>[federated.calc].[none:Calculation_1:qk]</rows>
        <cols>[federated.calc].[none:Category:nk]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>"""


def test_workbook_pbip_embeds_calculated_measure_and_binds_it(tmp_path):
    # An "openable" pbip whose model silently dropped every calc would open to broken/empty charts.
    # This asserts the whole chain: calc auto-extraction -> DAX translation in the emitted model ->
    # the rebuilt visual binding to that measure.
    src = InMemoryTableauSource(workbooks={"Calc WB": CALC_MEASURE_WORKBOOK_TWB})
    report = migrate_estate(src, str(tmp_path / "b"))
    wb = report["workbooks"][0]
    assert wb["pbip_status"] == "built"

    fid = next(f for f in wb["viz_fidelity"] if f["worksheet"] == "Ratio by Category")
    assert fid["status"] == "rebuilt"

    # 1) the calculated measure survives into the emitted embedded model as real (non-stub) DAX.
    measures_tmdl = (tmp_path / "b" / "pbip" / "Calc WB" / "Sales.SemanticModel"
                     / "definition" / "tables" / "_Measures.tmdl").read_text(encoding="utf-8")
    assert "measure 'Profit Ratio'" in measures_tmdl
    assert "DIVIDE(" in measures_tmdl                  # SUM([Profit])/SUM([Sales]) -> DIVIDE(...), not = 0

    # 2) the rebuilt visual references that measure -- so the chart is not empty in Desktop.
    report_dir = tmp_path / "b" / "pbip" / "Calc WB" / "Calc WB.Report"
    blob = ""
    for p in report_dir.rglob("*"):
        if p.is_file():
            try:
                blob += p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                pass
    assert "Profit Ratio" in blob


def test_workbook_pbip_is_openable_and_bound_bypath(tmp_path):
    src = InMemoryTableauSource(workbooks={"Exec Dashboard": SUPERSTORE_DASHBOARD_TWB})
    report = migrate_estate(src, str(tmp_path / "b"))
    wb = report["workbooks"][0]

    assert wb["viz_status"] == "built"
    assert wb["pbip_status"] == "built"
    assert wb["pbip_warnings"] == []
    assert wb["pbip_ref_drops"] == []          # happy path: every viz ref resolves -> nothing dropped
    assert wb["bound_model"] == "Superstore"
    assert wb["bound_datasource"] == "Superstore"
    assert wb["pbip_folder"] == "pbip/Exec Dashboard/Exec Dashboard.pbip"

    root = tmp_path / "b" / "pbip" / "Exec Dashboard"
    assert (root / "Exec Dashboard.pbip").is_file()
    report_dir = root / "Exec Dashboard.Report"
    assert (report_dir / ".platform").is_file()
    pbir = report_dir / "definition.pbir"
    assert pbir.is_file()

    # the workbook's own datasource is embedded as a sibling model and the report binds to it by a
    # relative path that actually resolves inside the bundle (an openable, self-contained project).
    model_dir = root / "Superstore.SemanticModel"
    assert (model_dir / "definition" / "model.tmdl").is_file()
    ref = json.loads(pbir.read_text(encoding="utf-8"))["datasetReference"]["byPath"]["path"]
    assert ref == "../Superstore.SemanticModel"
    assert (report_dir / ref).resolve() == model_dir.resolve()

    s = report["summary"]
    assert s["workbooks_pbip_built"] == 1
    assert s["visuals_rebuilt"] >= 1


# A workbook whose only date column (Order Date) becomes the model's ACTIVE calendar date. The
# rebuilt report's date axis must rebind to the shared marked Date table, not the Orders fact's raw
# date column, so time intelligence runs through the calendar.
DATE_AXIS_WORKBOOK_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2023.1' version='18.1'>
  <datasources>
    <datasource caption='Superstore' inline='true' name='federated.abc' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='warehouse' name='sqlserver.aa11'>
            <connection class='sqlserver' dbname='Superstore'
                        server='superstore.database.windows.net' username='svc' />
          </named-connection>
        </named-connections>
        <relation connection='sqlserver.aa11' name='Orders' table='[dbo].[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Sales Amount</remote-name><local-name>[Sales]</local-name>
            <parent-name>[Orders]</parent-name><local-type>real</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Order Date</remote-name><local-name>[Order Date]</local-name>
            <parent-name>[Orders]</parent-name><local-type>datetime</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Sales Trend'>
      <table>
        <view>
          <datasources>
            <datasource caption='Superstore' name='federated.abc' />
          </datasources>
          <datasource-dependencies datasource='federated.abc'>
            <column caption='Sales' datatype='real' name='[Sales]' role='measure' type='quantitative' />
            <column caption='Order Date' datatype='datetime' name='[Order Date]' role='dimension' type='ordinal' />
            <column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' pivot='key' type='quantitative' />
            <column-instance column='[Order Date]' derivation='Month' name='[mn:Order Date:ok]' pivot='key' type='ordinal' />
          </datasource-dependencies>
        </view>
        <panes><pane><mark class='Line' /></pane></panes>
        <rows>[federated.abc].[sum:Sales:qk]</rows>
        <cols>[federated.abc].[mn:Order Date:ok]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>"""


def test_workbook_pbip_rebinds_date_axis_to_model_date_table(tmp_path):
    src = InMemoryTableauSource(workbooks={"Trend WB": DATE_AXIS_WORKBOOK_TWB})
    report = migrate_estate(src, str(tmp_path / "b"))
    wb = report["workbooks"][0]
    assert wb["pbip_status"] == "built"
    assert wb["pbip_ref_drops"] == []          # the rebound Date[Month] resolves -> nothing dropped
    # the consumer recorded which calendar + active date it rebound (from the model build's facts)
    assert wb["date_rebind"]["date_table"] == "Date"
    assert any("order" in k.lower() and "date" in k.lower()
               for k in wb["date_rebind"]["active_keys"])

    # the rebuilt visual projects the date axis from the marked Date table, not the Orders fact table
    report_dir = tmp_path / "b" / "pbip" / "Trend WB" / "Trend WB.Report"
    visual = next(json.loads(p.read_text(encoding="utf-8"))
                  for p in report_dir.rglob("visual.json"))
    cat = visual["visual"]["query"]["queryState"]["Category"]["projections"][0]["field"]["Column"]
    assert cat["Expression"]["SourceRef"]["Entity"] == "Date"
    assert cat["Property"] == "Month"
    # and the marked Date table really is in the bound model (so the ref can't dangle)
    model_dir = tmp_path / "b" / "pbip" / "Trend WB" / "Superstore.SemanticModel" / "definition"
    model_blob = "".join(p.read_text(encoding="utf-8") for p in model_dir.rglob("*.tmdl"))
    assert "dataCategory: Time" in model_blob


# -- binding signal (published vs embedded datasource; would-break-if-rebound calcs) -----------
# A PUBLISHED Tableau datasource (connection_class 'sqlproxy') with TWO workbook-local calcs: one
# referenced by the worksheet (Profit Margin -> a would-break-if-rebound dependency) and one defined
# but never placed on a shelf (Unused Calc -> must be filtered out of the dependency set).
PUBLISHED_DS_WORKBOOK_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2023.1' version='18.1'>
  <datasources>
    <datasource caption='Superstore (Published)' name='sqlproxy.18xyz' version='18.1'>
      <connection class='sqlproxy' dbname='Superstore' directory='Superstore'
                  server='https://tableau.example.com'>
        <relation name='sqlproxy' table='[sqlproxy]' type='table' />
      </connection>
      <column caption='Profit Margin' datatype='real' name='[Calculation_123]' role='measure'
              type='quantitative'>
        <calculation class='tableau' formula='SUM([Profit]) / SUM([Sales])' />
      </column>
      <column caption='Unused Calc' datatype='real' name='[Calculation_999]' role='measure'
              type='quantitative'>
        <calculation class='tableau' formula='SUM([Discount])' />
      </column>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Margin by Region'>
      <table>
        <view>
          <datasources>
            <datasource caption='Superstore (Published)' name='sqlproxy.18xyz' />
          </datasources>
          <datasource-dependencies datasource='sqlproxy.18xyz'>
            <column caption='Region' datatype='string' name='[Region]' role='dimension' type='nominal' />
            <column caption='Profit Margin' datatype='real' name='[Calculation_123]' role='measure' type='quantitative'>
              <calculation class='tableau' formula='SUM([Profit]) / SUM([Sales])' />
            </column>
            <column-instance column='[Region]' derivation='None' name='[none:Region:nk]' pivot='key' type='nominal' />
            <column-instance column='[Calculation_123]' derivation='Sum' name='[sum:Calculation_123:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
        </view>
        <panes><pane><mark class='Bar' /></pane></panes>
        <rows>[sqlproxy.18xyz].[sum:Calculation_123:qk]</rows>
        <cols>[sqlproxy.18xyz].[none:Region:nk]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>"""


# Same published datasource but the worksheet references ONLY base columns -- no workbook-local calc
# dependency, so the report is a clean candidate to rebind to the migrated published model.
PUBLISHED_DS_NO_LOCAL_CALC_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2023.1' version='18.1'>
  <datasources>
    <datasource caption='Superstore (Published)' name='sqlproxy.18xyz' version='18.1'>
      <connection class='sqlproxy' dbname='Superstore' directory='Superstore'
                  server='https://tableau.example.com'>
        <relation name='sqlproxy' table='[sqlproxy]' type='table' />
      </connection>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Sales by Region'>
      <table>
        <view>
          <datasources>
            <datasource caption='Superstore (Published)' name='sqlproxy.18xyz' />
          </datasources>
          <datasource-dependencies datasource='sqlproxy.18xyz'>
            <column caption='Region' datatype='string' name='[Region]' role='dimension' type='nominal' />
            <column caption='Sales' datatype='real' name='[Sales]' role='measure' type='quantitative' />
            <column-instance column='[Region]' derivation='None' name='[none:Region:nk]' pivot='key' type='nominal' />
            <column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
        </view>
        <panes><pane><mark class='Bar' /></pane></panes>
        <rows>[sqlproxy.18xyz].[sum:Sales:qk]</rows>
        <cols>[sqlproxy.18xyz].[none:Region:nk]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>"""


def test_binding_signal_published_with_view_local_calc():
    sig = me._workbook_binding_signal(PUBLISHED_DS_WORKBOOK_TWB, None)
    assert sig["kind"] == "published"
    assert sig["connection_class"] == "sqlproxy"
    assert sig["published_ds_name"] == "Superstore (Published)"
    # only the SHELF-referenced calc is a binding dependency; the unused calc is filtered out
    names = [c["name"] for c in sig["view_local_calcs"]]
    assert names == ["Profit Margin"]
    assert sig["view_local_calcs"][0]["formula"] == "SUM([Profit]) / SUM([Sales])"
    assert sig["recommendation"] == "review_rebind"


def test_binding_signal_published_without_local_calc_is_rebind_candidate():
    sig = me._workbook_binding_signal(PUBLISHED_DS_NO_LOCAL_CALC_TWB, None)
    assert sig["kind"] == "published"
    assert sig["view_local_calcs"] == []
    assert sig["recommendation"] == "candidate_rebind_to_published"


def test_binding_signal_embedded_datasource_recommends_rebuild():
    sig = me._workbook_binding_signal(SUPERSTORE_DASHBOARD_TWB, None)
    assert sig["kind"] == "embedded"
    assert sig["connection_class"] == "sqlserver"
    assert sig["published_ds_name"] is None
    assert sig["recommendation"] == "rebuild_embedded"


def test_binding_signal_surfaced_in_estate_report_and_summary(tmp_path):
    src = InMemoryTableauSource(workbooks={
        "Published WB": PUBLISHED_DS_WORKBOOK_TWB,
        "Embedded WB": SUPERSTORE_DASHBOARD_TWB,
    })
    report = migrate_estate(src, str(tmp_path / "b"))
    by_name = {w["name"]: w for w in report["workbooks"]}

    pub = by_name["Published WB"]["binding_signal"]
    assert pub["kind"] == "published"
    assert [c["name"] for c in pub["view_local_calcs"]] == ["Profit Margin"]

    emb = by_name["Embedded WB"]["binding_signal"]
    assert emb["kind"] == "embedded"

    s = report["summary"]
    assert s["workbooks_published_ds"] == 1
    assert s["workbooks_embedded_ds"] == 1
    # the published workbook has a view-local calc -> review_rebind, not a clean rebind candidate
    assert s["workbooks_rebind_candidate"] == 0


def test_estate_summary_rolls_up_unbound_implicit_row_counts(tmp_path):
    # An object-id COUNT(*) with no model-side COUNTROWS target (the cross-layer gap) is warned,
    # never silently dropped or dangling -- and the estate summary rolls up the volume additively.
    oid = "__tableau_internal_object_id__"
    hexv = "ECFCA1FB690A41FE803BC071773BA862"
    ws = f"""
    <worksheet name='Row Count'>
      <table>
        <view>
          <datasources><datasource caption='Sales DS' name='federated.s1' /></datasources>
          <datasource-dependencies datasource='federated.s1'>
            <column caption='Category' datatype='string' name='[Category]' role='dimension' type='nominal' />
            <column caption='Sales' datatype='integer' name='[{oid}].[Sales_{hexv}]' role='measure' type='quantitative' />
            <column-instance column='[Category]' derivation='None' name='[none:Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[{oid}].[Sales_{hexv}]' derivation='Count' name='[cnt:Sales_{hexv}:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
        </view>
        <panes><pane><mark class='Bar' /></pane></panes>
        <rows>[federated.s1].[{oid}].[cnt:Sales_{hexv}:qk]</rows>
        <cols>[federated.s1].[none:Category:nk]</cols>
      </table>
    </worksheet>"""
    twb = _viz_wb(_viz_ds("Sales DS", "federated.s1", "sqlserver.s1", "sqlserver", "Sales"), ws)
    src = InMemoryTableauSource(workbooks={"Counts WB": twb})
    report = migrate_estate(src, str(tmp_path / "b"))
    wb = report["workbooks"][0]
    assert wb["viz_implicit_row_count"] == 1
    s = report["summary"]
    assert s["implicit_row_count_unbound"] == 1
    assert s["workbooks_implicit_row_count"] == 1
    assert any("implicit row count" in (f.get("reason") or "")
               for f in (wb["viz_fidelity"] or []))
    # never a dangling object-id projection in the rebuilt report.
    assert oid not in json.dumps(report)


def test_attach_workbook_pbip_refreshes_fidelity_from_rebound_run(tmp_path, monkeypatch):
    # The reported viz_fidelity / viz_implicit_row_count must describe the REBOUND report that
    # actually lands in the openable .pbip -- not the pre-rebind first pass. Here the model build
    # supplies a COUNTROWS row-count binding, so the bound re-run clears the "implicit row count"
    # warning; the detail keys (seeded with the stale pre-rebind values, as _migrate_one_workbook
    # does) must be refreshed to the bound state instead of reporting the now-fixed gap.
    pbir = json.dumps({"version": "1.0",
                       "datasetReference": {"byPath": {"path": "../WB.SemanticModel"}}})
    unbound_warn = {"scope": "worksheet", "name": "Row Count",
                    "reason": ("manual attention required: implicit row count COUNT('Orders') has "
                               "no model binding -- needs a row-count (COUNTROWS) measure on table "
                               "'Orders' (left unbound)")}
    pre = {"parts": {"definition.pbir": pbir},
           "ir": {"worksheets": [{"name": "Row Count", "visual_type": "bar"}]},
           "warnings": [unbound_warn]}
    detail = {"name": "Counts WB",
              "viz_fidelity": me._viz_fidelity(pre),
              "viz_implicit_row_count": 1}
    # sanity: the seeded pre-rebind state reports the unbound row count.
    assert detail["viz_implicit_row_count"] == 1
    assert any("implicit row count" in (f.get("reason") or "") for f in detail["viz_fidelity"])

    res_report = {"row_count_binding": {
        "measures": {"Orders": {"entity": "_Measures", "measure": "count orders"}}}}
    monkeypatch.setattr(me, "list_workbook_datasources",
                        lambda twb: [{"label": "Orders DS", "caption": "Orders DS",
                                      "name": "federated.s1"}])
    monkeypatch.setattr(me, "migrate_datasource",
                        lambda twb, **kw: {"parts": {"definition/model.tmdl": "x"},
                                           "report": res_report})
    monkeypatch.setattr(me, "_param_slicers_from_workbook", lambda twb, rep: {})
    monkeypatch.setattr(me, "_crosscheck_report_refs", lambda parts, model_parts: (parts, []))
    monkeypatch.setattr(me, "write_local_pbip", lambda *a, **kw: None)

    def bound_viz(xml, name, date_binding=None, measure_binding=None, row_count_binding=None,
                  param_binding=None, model_table=None, field_map=None):
        # the row count is bound now -> the rebound report carries no implicit-row-count warning.
        assert row_count_binding  # the model-derived binding reached the single re-run
        return {"parts": {"definition.pbir": pbir},
                "ir": {"worksheets": [{"name": "Row Count", "visual_type": "bar"}]},
                "warnings": []}

    me._attach_workbook_pbip(detail, "<workbook/>", pre, "Counts WB",
                             str(tmp_path / "pbip"), viz=bound_viz)

    assert detail["pbip_status"] == "built"
    assert detail["row_count_rebind"]["count"] == 1
    # refreshed to the rebound truth: the implicit-row-count warning is gone in both tallies.
    assert detail["viz_implicit_row_count"] == 0
    assert not any("implicit row count" in (f.get("reason") or "")
                   for f in detail["viz_fidelity"])


def test_workbook_pbip_bypath_resolves_for_caption_with_spaces_and_punctuation(tmp_path):
    # byPath footgun guard: the rewritten ../<model>.SemanticModel must resolve to the SAME folder
    # write_local_pbip actually creates -- even when the datasource caption has spaces/hyphens/periods
    # that get sanitized. A string-equality check would pass over a dangling path; this resolves it to
    # a real sibling dir. Both sides derive from one model_safe token, so they can never diverge.
    twb = _viz_wb(
        _viz_ds("Sample - Superstore (FY.2024)", "federated.s1", "sqlserver.s1", "sqlserver", "Sales"),
        _viz_ws("Sales by Category", "federated.s1", "Sample - Superstore (FY.2024)"))
    src = InMemoryTableauSource(workbooks={"Q1 Review": twb})
    report = migrate_estate(src, str(tmp_path / "b"))
    wb = report["workbooks"][0]
    assert wb["pbip_status"] == "built"

    root = tmp_path / "b" / "pbip" / "Q1 Review"
    report_dir = root / "Q1 Review.Report"
    model_dir = root / f"{wb['bound_model']}.SemanticModel"
    assert model_dir.is_dir()                                    # the model folder was actually written
    ref = json.loads((report_dir / "definition.pbir").read_text(
        encoding="utf-8"))["datasetReference"]["byPath"]["path"]
    resolved = (report_dir / ref).resolve()
    assert resolved == model_dir.resolve()                       # byPath points at that real sibling dir
    assert resolved.is_dir()


def test_workbook_pbip_filename_follows_workbook_not_model(tmp_path):
    # the project pointer is named after the workbook while the embedded model keeps its own name,
    # proving the additive write_local_pbip(project_name=...) kwarg is wired through.
    src = InMemoryTableauSource(workbooks={"Exec Dashboard": SUPERSTORE_DASHBOARD_TWB})
    migrate_estate(src, str(tmp_path / "b"))
    root = tmp_path / "b" / "pbip" / "Exec Dashboard"
    assert (root / "Exec Dashboard.pbip").is_file()
    assert not (root / "Superstore.pbip").exists()
    pbip = json.loads((root / "Exec Dashboard.pbip").read_text(encoding="utf-8"))
    assert pbip["artifacts"][0]["report"]["path"] == "Exec Dashboard.Report"


def test_workbook_viz_fidelity_section_shape(tmp_path):
    src = InMemoryTableauSource(workbooks={"Exec Dashboard": SUPERSTORE_DASHBOARD_TWB})
    report = migrate_estate(src, str(tmp_path / "b"))
    fid = report["workbooks"][0]["viz_fidelity"]
    assert isinstance(fid, list) and fid
    entry = next(f for f in fid if f["worksheet"] == "Sales by Category")
    assert entry["visual_type"] == "column"
    assert entry["status"] == "rebuilt"
    assert entry["reason"] is None
    for f in fid:
        assert set(f) == {"worksheet", "visual_type", "status", "reason"}
        assert f["status"] in {"rebuilt", "warned"}


def test_workbook_pbip_skipped_on_fallback_datasource(tmp_path):
    src = InMemoryTableauSource(workbooks={"Hana WB": SAPHANA_WORKBOOK_TWB})
    report = migrate_estate(src, str(tmp_path / "b"))
    wb = report["workbooks"][0]
    # the bare reports/ rebuild still happens; only the bound, openable .pbip is skipped
    assert wb["viz_status"] == "built"
    assert wb["pbip_status"] == "skipped"
    assert wb["pbip_folder"] is None
    assert any("lakehouse fallback" in w for w in wb["pbip_warnings"])
    assert all(w.startswith("manual attention required: ") for w in wb["pbip_warnings"])
    assert not (tmp_path / "b" / "pbip" / "Hana WB").exists()
    assert report["summary"]["workbooks_pbip_built"] == 0


def test_workbook_pbip_warns_on_secondary_datasource(tmp_path):
    src = InMemoryTableauSource(workbooks={"Multi WB": MULTI_SOURCE_TWB})
    report = migrate_estate(src, str(tmp_path / "b"))
    wb = report["workbooks"][0]
    assert wb["pbip_status"] == "built"          # the primary still binds
    assert wb["bound_model"]                       # a primary datasource was chosen
    secondary = [w for w in wb["pbip_warnings"] if "secondary datasource" in w]
    assert len(secondary) == 1
    assert (tmp_path / "b" / "pbip" / "Multi WB" / "Multi WB.pbip").is_file()


def test_workbook_pbip_skipped_without_pbir_definition(tmp_path):
    # a viz stage that yields report parts but no PBIR project file cannot be opened -> honest skip,
    # and the new pbip keys never disturb the existing bare reports/ write.
    def viz(text, name):
        return {"parts": {"definition/report.json": "{}"}}

    src = InMemoryTableauSource(workbooks={"Exec": SUPERSTORE_DASHBOARD_TWB})
    report = migrate_estate(src, str(tmp_path / "b"), viz_stage=viz)
    wb = report["workbooks"][0]
    assert wb["viz_status"] == "built"
    assert wb["output_folder"] == "reports/Exec.Report"
    assert wb["pbip_status"] == "skipped"
    assert any("no PBIR report definition" in w for w in wb["pbip_warnings"])


def test_crosscheck_drops_dangling_refs_and_empties_orphan_visual():
    # M1.3: a measure/column reference the model did not emit (the optimistic `_Measures[caption]`
    # bind) is dropped at the seam; a visual that loses all refs is emptied to a placeholder zone.
    model_parts = {
        "definition/tables/_Measures.tmdl":
            "table _Measures\n\tmeasure 'Total Sales' = SUM(Orders[Sales_Amount])\n",
        "definition/tables/Orders.tmdl":
            "table Orders\n\tcolumn Sales_Amount\n\t\tdataType: double\n",
    }

    def meas(prop):
        return {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}},
                                      "Property": prop}}, "queryRef": f"_Measures.{prop}"}

    def col(prop):
        return {"field": {"Aggregation": {"Function": 0, "Expression": {"Column": {
            "Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": prop}}}},
            "queryRef": f"Sum(Orders.{prop})"}

    def visual(name, vtype, state):
        return json.dumps({"name": name,
                           "visual": {"visualType": vtype, "query": {"queryState": state}}})

    report_parts = {
        # a card mixing two real refs with a dangling `_Measures[Param Swap]`
        "definition/pages/p/visuals/a/visual.json": visual(
            "a", "multiRowCard",
            {"Values": {"projections": [meas("Total Sales"), col("Sales_Amount"), meas("Param Swap")]}}),
        # a card whose ONLY ref is dangling -> must be emptied
        "definition/pages/p/visuals/b/visual.json": visual(
            "b", "card", {"Values": {"projections": [meas("Ghost")]}}),
        # a field-parameter visual is a separately validated construct -> left untouched
        "definition/pages/p/visuals/fp/visual.json": visual(
            "fp", "tableEx",
            {"Values": {"projections": [col("Nonexistent")], "fieldParameters": [{"index": 0}]}}),
    }
    new_parts, drops = me._crosscheck_report_refs(report_parts, model_parts)

    a = json.loads(new_parts["definition/pages/p/visuals/a/visual.json"])
    kept = [p["queryRef"] for p in a["visual"]["query"]["queryState"]["Values"]["projections"]]
    assert kept == ["_Measures.Total Sales", "Sum(Orders.Sales_Amount)"]   # dangling one removed
    b = json.loads(new_parts["definition/pages/p/visuals/b/visual.json"])
    assert "query" not in b["visual"]                                       # orphan visual emptied
    fp = json.loads(new_parts["definition/pages/p/visuals/fp/visual.json"])
    assert fp["visual"]["query"]["queryState"]["Values"]["projections"]     # FP visual untouched

    by = {d["visual"]: d for d in drops}
    assert set(by) == {"a", "b"}
    assert by["a"]["emptied"] is False and by["b"]["emptied"] is True


def test_crosscheck_no_model_inventory_is_a_noop():
    # defensive: with no parseable model objects, never risk a false drop -> parts returned as-is
    parts = {"definition/pages/p/visuals/a/visual.json": json.dumps(
        {"name": "a", "visual": {"visualType": "card", "query": {"queryState": {
            "Values": {"projections": [{"field": {"Measure": {"Expression": {
                "SourceRef": {"Entity": "_Measures"}}, "Property": "X"}}}]}}}}})}
    out, drops = me._crosscheck_report_refs(dict(parts), {})
    assert drops == [] and out == parts


def test_workbook_pbip_disabled_when_pbip_false(tmp_path):
    src = InMemoryTableauSource(workbooks={"Exec Dashboard": SUPERSTORE_DASHBOARD_TWB})
    report = migrate_estate(src, str(tmp_path / "b"), pbip=False)
    wb = report["workbooks"][0]
    assert wb["viz_status"] == "built"
    assert "pbip_status" not in wb            # no pbip attempted at all
    assert not (tmp_path / "b" / "pbip").exists()


# -- LiveTableauSource seam ---------------------------------------------------
_LIVE_ENV_VARS = (
    "TABLEAU_SERVER_URL", "TABLEAU_SITE", "TABLEAU_MIGRATION_KEYVAULT",
    "TABLEAU_MIGRATION_PAT_SECRET", "TABLEAU_MIGRATION_PAT_NAME",
    "FABRIC_WORKSPACE", "TABLEAU_DATASOURCE_NAMES", "TABLEAU_WORKBOOK_NAMES",
    "TABLEAU_PAT", "TABLEAU_MIGRATION_PAT_ENV_VAR", "TABLEAU_MIGRATION_ENV_FILE",
    "TABLEAU_MIGRATION_KEYRING_SERVICE",
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


def test_resolve_pat_uses_explicit_value_without_key_vault(clean_live_env):
    # A POC with no Azure Key Vault: an explicit PAT value resolves and is NOT stored on describe().
    live = LiveTableauSource(server_url="https://t.example.com", site="s", pat_value="poc-token")
    assert live._resolve_pat() == "poc-token"
    assert live._pat_source == "argument"        # value-free trace of which layer answered
    assert "poc-token" not in json.dumps(live.describe())


def test_resolve_pat_reads_env_var(clean_live_env):
    clean_live_env.setenv("TABLEAU_PAT", "env-token")
    live = LiveTableauSource(server_url="https://t.example.com", site="s")
    assert live._resolve_pat() == "env-token"
    assert live._pat_source == "env:TABLEAU_PAT"


def test_resolve_pat_reads_dotenv_file(clean_live_env, tmp_path):
    env_path = tmp_path / "poc.env"
    env_path.write_text("# poc creds\nTABLEAU_PAT = 'file-token'\n", encoding="utf-8")
    live = LiveTableauSource(server_url="https://t.example.com", site="s",
                             env_file=str(env_path))
    assert live._resolve_pat() == "file-token"
    assert live._pat_source.startswith("dotenv:")


def test_resolve_pat_falls_back_to_key_vault_seam_when_nothing_local(clean_live_env):
    # No local layer configured but a Key Vault is named -> the enterprise seam (NotImplemented).
    live = LiveTableauSource(server_url="https://t.example.com", site="s",
                             key_vault_name="vault-x", pat_secret_name="pat-secret")
    with pytest.raises(NotImplementedError):
        live._resolve_pat()


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
    assert os.path.isdir(os.path.join(out, "pbip"))  # pbip projects emitted by default
    printed = capsys.readouterr().out
    assert "Datasources:" in printed
    assert "Bundle written to:" in printed
    assert "Openable projects:" in printed  # pbip hint surfaced
    assert "Next step:" in printed          # stubbed-calc check-in surfaced (widget_sales stubs one)


def test_cli_main_no_pbip_flag_suppresses_projects(fixtures_dir, tmp_path, capsys):
    out = str(tmp_path / "b")
    rc = me.main(["-i", fixtures_dir, "-o", out, "--no-pbip"])
    assert rc == 0
    assert not os.path.isdir(os.path.join(out, "pbip"))
    assert "Openable projects:" not in capsys.readouterr().out


# -- measure_binding producer (model build's calc->measure facts -> viz consumer map) ----------
# `_measure_binding_from_model` is a pure CONSUMER of the datasource-migration report: it shapes the
# model build's calc->measure identity into the {"measures": {key: entry}} map twb_to_pbir reads.
def test_measure_binding_from_model_passes_through_calc_bindings_index():
    # The model build's consolidated `calc_bindings` index (token + caption keyed) is forwarded
    # verbatim so the join token stays byte-identical to what the model stamped.
    res_report = {"calc_bindings": {
        "pcdf:usr:Calculation_0014172369735704:qk": {
            "model_table": "_Measures", "measure_name": "Percent Difference (DoD)",
            "status": "translated"},
        "count orders": {"model_table": "_Measures", "measure_name": "count orders",
                         "status": "translated"},
    }}
    mb = me._measure_binding_from_model(res_report)
    inner = mb["measures"]
    assert inner["pcdf:usr:Calculation_0014172369735704:qk"]["measure_name"] == "Percent Difference (DoD)"
    assert inner["count orders"]["status"] == "translated"


def test_measure_binding_from_model_derives_from_source_tokens_when_no_index():
    # Pre-`calc_bindings` shape: only rows carrying an explicit source token/id/caption are keyed,
    # under EACH present key (instance token, bare calc id, field caption) -> same entry.
    res_report = {"measures": [
        {"measure": "Standard of Deviation", "status": "translated",
         "source": {"calc_instance_token": "usr:Calculation_0014172373577763:qk",
                    "calc_id": "Calculation_0014172373577763",
                    "field_caption": "Standard of Deviation", "model_table": "_Measures"}},
    ]}
    inner = me._measure_binding_from_model(res_report)["measures"]
    for key in ("usr:Calculation_0014172373577763:qk", "Calculation_0014172373577763",
                "Standard of Deviation"):
        assert inner[key]["measure_name"] == "Standard of Deviation"
        assert inner[key]["model_table"] == "_Measures"
        assert inner[key]["status"] == "translated"


def test_measure_binding_from_model_ignores_rows_without_source():
    # A plain <column> calc row (no `source` tag, no `calc_bindings`) is NOT keyed -- it keeps its
    # existing caption-based _Measures binding in the viz layer, so behaviour is byte-unchanged.
    res_report = {"measures": [
        {"measure": "Revenue Sum", "status": "translated", "tableau_formula": "SUM([Revenue])"},
    ]}
    assert me._measure_binding_from_model(res_report) is None


def test_measure_binding_from_model_none_when_no_measures():
    assert me._measure_binding_from_model({}) is None
    assert me._measure_binding_from_model(None) is None
    assert me._measure_binding_from_model({"calc_bindings": {}}) is None


def test_viz_adapter_forwards_measure_binding_only_when_supported():
    # The adapter passes measure_binding through to a viz fn that declares it, and silently omits it
    # for one that does not -- so the seam stays additive against older viz entry points.
    seen = {}

    def viz_with(text, *, report_name, dataset_name, date_binding=None, measure_binding=None):
        seen["with"] = {"date": date_binding, "measure": measure_binding}
        return {"parts": {}}

    def viz_without(text, *, report_name, dataset_name, date_binding=None):
        seen["without"] = {"date": date_binding}
        return {"parts": {}}

    mb = {"measures": {"Calculation_1": {"model_table": "_Measures",
                                         "measure_name": "X", "status": "translated"}}}
    me._viz_adapter(viz_with)("<twb/>", "WB", date_binding=None, measure_binding=mb)
    me._viz_adapter(viz_without)("<twb/>", "WB", date_binding=None, measure_binding=mb)
    assert seen["with"]["measure"] == mb
    assert "without" in seen  # called without raising despite no measure_binding param


# `_row_count_binding_from_model` is a pure CONSUMER too: it shapes the model build's per-fact
# COUNTROWS measures into the {"measures": {<table>: {entity, measure}}, "default": {...}} map the
# viz layer's implicit-row-count path reads, so an object-id COUNT(*) pill binds by FACT TABLE.
def test_row_count_binding_from_model_passes_through_consumer_shape():
    # An explicit consumer-shape `row_count_binding` is normalised + forwarded so the table->measure
    # identity is byte-identical to what the model emitted.
    res_report = {"row_count_binding": {
        "measures": {"Orders": {"entity": "_Measures", "measure": "count orders"}},
        "default": {"entity": "_Measures", "measure": "Number of Records"},
    }}
    rcb = me._row_count_binding_from_model(res_report)
    assert rcb["measures"]["Orders"] == {"entity": "_Measures", "measure": "count orders"}
    assert rcb["default"] == {"entity": "_Measures", "measure": "Number of Records"}


def test_row_count_binding_from_model_normalizes_convenience_map():
    # A convenience `row_count_measures` map: dict targets pass through; a bare measure NAME defaults
    # to the _Measures table; a model_table/measure_name aliasing is accepted.
    res_report = {"row_count_measures": {
        "Orders": {"entity": "_Measures", "measure": "count orders"},
        "Returns": "Returns Row Count",
        "Shipments": {"model_table": "Fact", "measure_name": "Shipment Count"},
    }}
    rcb = me._row_count_binding_from_model(res_report)
    m = rcb["measures"]
    assert m["Orders"] == {"entity": "_Measures", "measure": "count orders"}
    assert m["Returns"] == {"entity": "_Measures", "measure": "Returns Row Count"}
    assert m["Shipments"] == {"entity": "Fact", "measure": "Shipment Count"}
    assert "default" not in rcb


def test_row_count_binding_from_model_carries_numrec_default():
    # The legacy single-fact (numrec) row count binds via `default`, not a named table.
    res_report = {"row_count_measures": {"default": {"entity": "_Measures", "measure": "Rows"}}}
    rcb = me._row_count_binding_from_model(res_report)
    assert rcb == {"default": {"entity": "_Measures", "measure": "Rows"}}


def test_row_count_binding_from_model_reads_model_manifest_row_count():
    # The fact-table -> COUNTROWS map nested inside the model build's additive `model_manifest`
    # (the likely emit site) is read too -- both the nested consumer shape and a flat convenience
    # map -- so the seam lights up wherever the model surfaces the target.
    nested = {"model_manifest": {"row_count": {
        "measures": {"Orders": {"entity": "_Measures", "measure": "count orders"}}}}}
    flat = {"model_manifest": {"row_count": {
        "Orders": {"entity": "_Measures", "measure": "count orders"}}}}
    for rep in (nested, flat):
        rcb = me._row_count_binding_from_model(rep)
        assert rcb["measures"]["Orders"] == {"entity": "_Measures", "measure": "count orders"}


def test_row_count_binding_from_model_reads_model_manifest_verbatim_shape():
    # Pins the model build's REAL `model_manifest.row_count` shape (verified against the live
    # model emit): `measures` values are BARE measure-name STRINGS (entity is always `_Measures`,
    # since every measure lives there) and `default` carries `{table, measure}` (the single-fact
    # fallback -- `table` is informational, the bind is `measure` @ `_Measures`). The normalizer
    # lifts both to the consumer's `{entity, measure}` target shape, so the seam binds with no
    # extra/duplicated top-level key on the model side (single source of truth).
    rep = {"model_manifest": {"row_count": {
        "measures": {"Orders": "count orders"},
        "default": {"table": "Orders", "measure": "count orders"}}}}
    rcb = me._row_count_binding_from_model(rep)
    assert rcb["measures"]["Orders"] == {"entity": "_Measures", "measure": "count orders"}
    assert rcb["default"] == {"entity": "_Measures", "measure": "count orders"}


def test_row_count_binding_from_model_top_level_wins_over_manifest():
    # An explicit top-level `row_count_binding` takes priority over the manifest copy.
    rep = {"row_count_binding": {"measures": {"Orders": {"entity": "_Measures", "measure": "A"}}},
           "model_manifest": {"row_count": {"Orders": {"entity": "_Measures", "measure": "B"}}}}
    assert me._row_count_binding_from_model(rep)["measures"]["Orders"]["measure"] == "A"


def test_row_count_binding_from_model_ignores_scalar_manifest_row_count():
    # `model_manifest["row_count"]` may instead be a diagnostic row TOTAL (a scalar) or a non-target
    # map -- never bind to that; only real table->measure targets count.
    assert me._row_count_binding_from_model({"model_manifest": {"row_count": 9994}}) is None
    assert me._row_count_binding_from_model(
        {"model_manifest": {"row_count": {"Orders": 9994}}}) is None


def test_row_count_binding_from_model_none_when_absent_or_empty():
    assert me._row_count_binding_from_model({}) is None
    assert me._row_count_binding_from_model(None) is None
    assert me._row_count_binding_from_model({"row_count_measures": {}}) is None
    # a malformed entry (no measure) yields no binding rather than a dangling target
    assert me._row_count_binding_from_model(
        {"row_count_measures": {"Orders": {"entity": "_Measures"}}}) is None


def test_viz_adapter_forwards_row_count_binding_only_when_supported():
    # The adapter passes row_count_binding to a viz fn that declares it, and silently omits it for an
    # older entry point that does not -- additive against viz fns predating the row-count seam.
    seen = {}

    def viz_with(text, *, report_name, dataset_name, date_binding=None,
                 measure_binding=None, row_count_binding=None):
        seen["with"] = {"row_count": row_count_binding}
        return {"parts": {}}

    def viz_without(text, *, report_name, dataset_name, date_binding=None, measure_binding=None):
        seen["without"] = True
        return {"parts": {}}

    rcb = {"measures": {"Orders": {"entity": "_Measures", "measure": "count orders"}}}
    me._viz_adapter(viz_with)("<twb/>", "WB", row_count_binding=rcb)
    me._viz_adapter(viz_without)("<twb/>", "WB", row_count_binding=rcb)
    assert seen["with"]["row_count"] == rcb
    assert seen.get("without") is True  # called without raising despite no row_count_binding param


def test_field_map_from_model_builds_entity_property_from_naming_columns():
    # _field_map_from_model turns the model build's authoritative `model_manifest.naming` map into a
    # caption-keyed field_map carrying ONLY {entity, property} (never `binding`, so an aggregation
    # pill keeps its aggregation) for column-kind refs, and picks the fact table (most columns) as
    # model_table -- so a published-DS workbook's column pills bind to Orders/People, not `sqlproxy`.
    res_report = {"model_manifest": {"naming": {
        "Sales": {"model_table": "Orders", "model_name": "Sales", "kind": "column"},
        "Order Date": {"model_table": "Orders", "model_name": "Order_Date", "kind": "column"},
        "Segment": {"model_table": "Orders", "model_name": "Segment", "kind": "column"},
        "Regional Manager": {"model_table": "People", "model_name": "Regional_Manager",
                             "kind": "column"},
        "Profit Ratio": {"model_table": "_Measures", "model_name": "Profit Ratio",
                         "kind": "measure"},
        "Choose Metric": {"model_table": "Measure Swap calc 1",
                          "model_name": "Measure Swap calc 1", "kind": "parameter"},
    }}}
    model_table, field_map = me._field_map_from_model(res_report)
    # fact table = the one owning the most columns (Orders: 3 vs People: 1)
    assert model_table == "Orders"
    # columns are mapped with {entity, property} and NO binding override (aggregations survive)
    assert field_map["Sales"] == {"entity": "Orders", "property": "Sales"}
    assert field_map["Order Date"] == {"entity": "Orders", "property": "Order_Date"}
    assert field_map["Regional Manager"] == {"entity": "People", "property": "Regional_Manager"}
    assert "binding" not in field_map["Sales"]
    # measures + parameters are excluded -- measure_binding / field-parameter paths own those
    assert "Profit Ratio" not in field_map
    assert "Choose Metric" not in field_map


def test_field_map_from_model_skips_incomplete_entries():
    # A naming entry missing model_table or model_name is skipped rather than emitting a dangling
    # {entity:None}/{property:None} override.
    res_report = {"model_manifest": {"naming": {
        "Good": {"model_table": "Orders", "model_name": "Good", "kind": "column"},
        "NoTable": {"model_table": None, "model_name": "X", "kind": "column"},
        "NoName": {"model_table": "Orders", "model_name": None, "kind": "column"},
    }}}
    model_table, field_map = me._field_map_from_model(res_report)
    assert model_table == "Orders"
    assert field_map == {"Good": {"entity": "Orders", "property": "Good"}}


def test_field_map_from_model_none_when_no_columns():
    # No usable column naming -> (None, None) so the viz re-run keeps its standing field bindings
    # (warn-never-wrong; byte-unchanged until a real map exists).
    assert me._field_map_from_model(None) == (None, None)
    assert me._field_map_from_model({}) == (None, None)
    assert me._field_map_from_model({"model_manifest": {"naming": {}}}) == (None, None)
    only_measure = {"model_manifest": {"naming": {
        "M": {"model_table": "_Measures", "model_name": "M", "kind": "measure"}}}}
    assert me._field_map_from_model(only_measure) == (None, None)


def test_viz_adapter_forwards_model_table_and_field_map_only_when_supported():
    # The adapter passes model_table + field_map to a viz fn that declares them (the published-DS
    # column rebind seam), and silently omits them for an older entry point that does not.
    seen = {}

    def viz_with(text, *, report_name, dataset_name, model_table=None, field_map=None):
        seen["with"] = {"model_table": model_table, "field_map": field_map}
        return {"parts": {}}

    def viz_without(text, *, report_name, dataset_name):
        seen["without"] = True
        return {"parts": {}}

    fm = {"Sales": {"entity": "Orders", "property": "Sales"}}
    me._viz_adapter(viz_with)("<twb/>", "WB", model_table="Orders", field_map=fm)
    me._viz_adapter(viz_without)("<twb/>", "WB", model_table="Orders", field_map=fm)
    assert seen["with"] == {"model_table": "Orders", "field_map": fm}
    assert seen.get("without") is True  # called without raising despite no model_table/field_map


# -- parameter-as-filter -> direct single-select slicer resolution ------------------------------
# A parameter used purely as a single-column equality filter ([Col] = [Parameters].[P]) is most
# faithfully a plain slicer on that real column -- never a disconnected what-if table. These cover
# the orchestrator-side resolver that turns such a parameter into a `param_binding.slicers` entry
# keyed by the parameter's internal name (the same key the report binder consumes).

def test_filter_param_target_field_single_column_equality_both_orientations():
    # The canonical "use a parameter as a filter" idiom resolves to the ONE compared column, in
    # either orientation, and the `OR [Parameters].[P] = "All"` show-everything escape (a string
    # literal, never a field) does not contribute a spurious target.
    f1 = '[Region] = [Parameters].[Parameter 1] OR [Parameters].[Parameter 1] = "All"'
    f2 = '[Parameters].[P] = [Sub-Category]'
    assert me._filter_param_target_field(f1, "Parameter 1") == "Region"
    assert me._filter_param_target_field(f2, "P") == "Sub-Category"
    # the match is case-insensitive on the parameter's inner name
    assert me._filter_param_target_field(f1, "parameter 1") == "Region"


def test_filter_param_target_field_rejects_zero_or_multiple_columns():
    # Zero compared columns (pure "All" escape), more than one distinct column, or an empty inner
    # name all fail closed -> None, so the parameter stays an unresolved slicer (warn-never-wrong)
    # rather than binding to a guessed column.
    assert me._filter_param_target_field('[Parameters].[P] = "All"', "P") is None
    two = '[A] = [Parameters].[P] OR [B] = [Parameters].[P]'
    assert me._filter_param_target_field(two, "P") is None
    assert me._filter_param_target_field('[Region] = [Parameters].[P]', "") is None
    # the parameter's own [Parameters].[P] tail bracket is never read back as a target field
    assert me._filter_param_target_field('[Parameters].[P] = "All"', "P") is None


_PARAM_SLICER_TWB = """<?xml version='1.0'?>
<workbook><datasources><datasource name='ds'>
 <column caption='Region Parameter' name='[Parameter 1]' datatype='string' role='measure'
         type='nominal' param-domain-type='list' value='&quot;Central&quot;'>
   <calculation class='tableau' formula='&quot;Central&quot;' /></column>
 <column caption='Region Filter' name='[Calculation_900]' datatype='boolean' role='dimension'
         type='ordinal'>
   <calculation class='tableau'
     formula='[Region] = [Parameters].[Parameter 1] OR [Parameters].[Parameter 1] = &quot;All&quot;' />
 </column>
</datasource></datasources></workbook>"""


def test_param_slicers_from_workbook_resolves_direct_column_slicer():
    # End to end: a list parameter whose filter calc targets [Region] becomes a single-select slicer
    # on the model's real Orders[Region] column, keyed by the parameter's bracketed internal name so
    # it merges cleanly with `_param_binding_from_model` output.
    rr = {"model_manifest": {"naming": {
        "Region": {"model_table": "Orders", "model_name": "Region", "kind": "column"}}}}
    out = me._param_slicers_from_workbook(_PARAM_SLICER_TWB, rr)
    assert out == {"[Parameter 1]": {"table": "Orders", "column": "Region",
                                     "single_select": True, "caption": "Region Parameter"}}


def test_param_slicers_from_workbook_fail_closed_paths():
    # No usable column naming, a target the model never emitted, or no parameters at all -> {} so the
    # report keeps its precise "not rebuilt as a slicer yet" warning instead of a dangling slicer.
    assert me._param_slicers_from_workbook(_PARAM_SLICER_TWB, {"model_manifest": {"naming": {}}}) == {}
    # naming has columns but not the targeted one
    other = {"model_manifest": {"naming": {
        "Segment": {"model_table": "Orders", "model_name": "Segment", "kind": "column"}}}}
    assert me._param_slicers_from_workbook(_PARAM_SLICER_TWB, other) == {}
    # a workbook with no parameters yields nothing
    assert me._param_slicers_from_workbook("<workbook><datasources/></workbook>", other) == {}


def test_param_slicers_from_workbook_ignores_measure_naming_targets():
    # The resolved field must be a column-kind naming entry; a same-named measure/parameter entry is
    # not a valid slicer target (a slicer binds a column, never a measure).
    rr = {"model_manifest": {"naming": {
        "Region": {"model_table": "_Measures", "model_name": "Region", "kind": "measure"}}}}
    assert me._param_slicers_from_workbook(_PARAM_SLICER_TWB, rr) == {}


def test_param_binding_from_model_emits_value_picker_slicer():
    # A kind="value" what-if param exposing a disconnected picker table becomes a single-select
    # value-picker slicer on the picker's friendly column, so a scalar parameter the model consumed
    # still gets an operable control (the model owns the picker; the viz just places it).
    rr = {"model_manifest": {"parameters": [
        {"name": "Date Selection", "internal_name": "[Parameter 0014172370878491]",
         "kind": "value", "model_object": "Date Selection",
         "picker": {"table": "Date Selection", "column": "Date Selection Label"}}]}}
    pb = me._param_binding_from_model(rr)
    assert pb["slicers"]["[Parameter 0014172370878491]"] == {
        "table": "Date Selection", "column": "Date Selection Label",
        "single_select": True, "caption": "Date Selection"}
    assert pb["flags"] == {}


def test_param_binding_from_model_flag_carries_visuals():
    # A translated date-window keep-flag measure binds as a visual-level ``flag = 1`` filter, and the
    # binding carries the scoped worksheet names (set upstream by _scope_flag_visuals) so the viz
    # layer applies the filter to exactly those visuals instead of the whole page.
    rr = {"filter_bindings": {"Date Filter": {
        "model_table": "_Measures", "measure_name": "Date Filter", "status": "translated",
        "predicate": {"op": "==", "value": 1}, "value": 1, "calc_id": "Calculation_900",
        "visuals": ["Line chart", "Line chart (2)", "Line chart (3)", "Segment % Dod"]}}}
    pb = me._param_binding_from_model(rr)
    assert pb["flags"]["Date Filter"] == {
        "entity": "_Measures", "measure": "Date Filter", "status": "translated", "value": 1,
        "visuals": ["Line chart", "Line chart (2)", "Line chart (3)", "Segment % Dod"]}


def test_param_binding_from_model_flag_visuals_default_empty():
    # A flag binding with no scoped visuals (the calc was never matched to a worksheet) still binds,
    # with an empty visuals list -- the consumer then falls back to its own known scope.
    rr = {"filter_bindings": {"Date Filter": {
        "model_table": "_Measures", "measure_name": "Date Filter", "status": "translated",
        "predicate": {"op": "==", "value": 1}}}}
    pb = me._param_binding_from_model(rr)
    assert pb["flags"]["Date Filter"]["visuals"] == []


def test_scope_flag_visuals_attaches_worksheets(monkeypatch):
    # The flag's source calc_id is mapped, via workbook_calc_usage, to the worksheets that placed the
    # source Tableau filter calc; those names are written into the binding's ``visuals`` list.
    rr = {"filter_bindings": {"Date Filter": {
        "model_table": "_Measures", "measure_name": "Date Filter", "status": "translated",
        "predicate": {"op": "==", "value": 1}, "value": 1,
        "calc_id": "Calculation_0014172371238940", "param_internal": "[Parameter 1]"}}}
    monkeypatch.setattr(me, "workbook_calc_usage", lambda _x: {"calcs": {
        "Calculation_0014172371238940": {"worksheets": [
            "Line chart", "Line chart (2)", "Line chart (3)", "Segment % Dod"]}}})
    me._scope_flag_visuals("<workbook/>", rr)
    assert rr["filter_bindings"]["Date Filter"]["visuals"] == [
        "Line chart", "Line chart (2)", "Line chart (3)", "Segment % Dod"]


def test_scope_flag_visuals_fail_closed(monkeypatch):
    # No filter_bindings -> no-op without even consulting the workbook. An unreferenced calc, or a
    # workbook_calc_usage parse error, leaves ``visuals`` absent (never raises) so the consumer keeps
    # its own scope.
    sentinel = {"called": False}
    monkeypatch.setattr(me, "workbook_calc_usage",
                        lambda _x: sentinel.__setitem__("called", True) or {"calcs": {}})
    me._scope_flag_visuals("<workbook/>", {})
    me._scope_flag_visuals("<workbook/>", {"filter_bindings": {}})
    assert sentinel["called"] is False  # short-circuited before parsing
    # calc_id not present in usage -> visuals not set
    rr = {"filter_bindings": {"X": {"measure_name": "X", "status": "translated",
                                    "calc_id": "Calculation_NOPE"}}}
    me._scope_flag_visuals("<workbook/>", rr)
    assert "visuals" not in rr["filter_bindings"]["X"]

    # a parse error inside workbook_calc_usage is swallowed
    def _boom(_x):
        raise ValueError("bad xml")
    monkeypatch.setattr(me, "workbook_calc_usage", _boom)
    rr2 = {"filter_bindings": {"X": {"calc_id": "C", "measure_name": "X", "status": "translated"}}}
    me._scope_flag_visuals("<workbook/>", rr2)
    assert "visuals" not in rr2["filter_bindings"]["X"]


def test_rebuild_from_published_match_threads_parameters(monkeypatch):
    # The published-DS rebuild must thread the WORKBOOK's parameters into the model build -- without
    # it a parameter-driven flag measure (a Date Selection band) never reaches assemble on the
    # published path, so the flag + its filter_bindings would silently never fire.
    captured = {}

    def _fake_migrate(text, **kw):
        captured.update(kw)
        return {"report": {"fallback": False}}

    monkeypatch.setattr(me, "migrate_datasource", _fake_migrate)
    twb = ("<workbook><datasources><datasource name='ds'>"
           "<column caption='Date Selection' name='[Parameter 1]' datatype='real' role='measure'"
           " param-domain-type='list' value='15.'>"
           "<calculation class='tableau' formula='15.' /></column>"
           "</datasource></datasources></workbook>")
    detail = {"binding_signal": {"kind": "published", "published_ds_name": "Sales DS"}}
    catalog = {me._norm_ds("Sales DS"): {"text": "<datasource/>", "name": "Sales DS"}}
    res = me._rebuild_from_published_match(detail, twb, "Model", catalog)
    assert res is not None
    params = captured.get("parameters")
    assert isinstance(params, list)
    assert any(p.get("caption") == "Date Selection" for p in params)
