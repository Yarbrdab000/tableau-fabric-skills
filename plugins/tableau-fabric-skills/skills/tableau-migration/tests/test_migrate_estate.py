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
import tempfile
import zipfile

import pytest

import migrate_estate as me
from migrate_estate import (
    InMemoryTableauSource,
    LiveTableauSource,
    LocalFilesSource,
    extract_calculations,
    migrate_estate,
    migrate_workbook,
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

# An unmapped connector class -> needs-storage-decision fallback (DirectLake is opt-in, not default).
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


# -- Fix 2: read the built model's TMDL back into the report's column_binding manifest ----------
def test_parse_tmdl_columns_flags_only_tableau_calc_dimensions():
    # The subtle discrimination Fix 2 depends on: a DAX calc column is a Tableau calc dimension ONLY
    # when it ALSO carries ``annotation TableauFormula`` -- so a model-generated calendar column
    # (``Year = YEAR(...)``, no such annotation) and a raw ``sourceColumn`` passthrough are NOT calc.
    part = (
        "table Sheet1\n"
        "    column Director\n"
        "        dataType: string\n"
        "        sourceColumn: Director\n"
        "\n"
        "    column Cohort = IF([Sales] > 100, \"Hi\", \"Lo\")\n"
        "        dataType: string\n"
        "        annotation TableauFormula = IF [Sales] > 100 THEN \"Hi\" ELSE \"Lo\" END\n"
        "\n"
        "    column Year = YEAR([Order Date])\n"
        "        dataType: int64\n"
    )
    table, cols = me._parse_tmdl_columns(part)
    assert table == "Sheet1"
    flags = dict(cols)
    assert flags["Director"] is False          # raw passthrough
    assert flags["Cohort"] is True             # Tableau calc dimension (TableauFormula-stamped)
    assert flags["Year"] is False              # model calendar calc column (no TableauFormula)
    # a part that declares no table (relationships / model / culture) yields ("", [])
    assert me._parse_tmdl_columns("relationship abc\n    fromColumn: Sheet1.Region\n") == ("", [])


def test_column_binding_from_model_shapes_unique_calc_dims_and_drops_ambiguous():
    # Shape the manifest twb_to_pbir consumes: a visible field-parameter picker (a ``= calculated``
    # partition with a ``[Value...]`` sourceColumn -- the Choose Date shape) is a calc dimension in
    # its OWN table; a raw column is excluded; a calc name in TWO tables is ambiguous -> dropped.
    sheet1 = (
        "table Sheet1\n"
        "    column Director = IF([Sales] > 0, \"A\", \"B\")\n"
        "        dataType: string\n"
        "        annotation TableauFormula = IF [Sales] > 0 THEN 'A' ELSE 'B' END\n"
        "\n"
        "    column Region\n"
        "        dataType: string\n"
        "        sourceColumn: Region\n"
        "\n"
        "    column Dup = IF(TRUE, 1, 0)\n"
        "        dataType: int64\n"
        "        annotation TableauFormula = 1\n"
    )
    choose_date = (
        "table 'Choose Date'\n"
        "    column 'Choose Date'\n"
        "        dataType: string\n"
        "        sourceColumn: [Value1]\n"
        "\n"
        "    partition 'Choose Date' = calculated\n"
        "        source = {(\"Completed Date\", NAMEOF('Sheet1'[Completed Date]), 0)}\n"
    )
    other = (
        "table Other\n"
        "    column Dup = IF(TRUE, 1, 0)\n"
        "        dataType: int64\n"
        "        annotation TableauFormula = 1\n"
    )
    parts = {
        "definition/tables/Sheet1.tmdl": sheet1,
        "definition/tables/Choose Date.tmdl": choose_date,
        "definition/tables/Other.tmdl": other,
        "definition/relationships.tmdl": "relationship r1\n    fromColumn: Sheet1.Region\n",
    }
    cb = me._column_binding_from_model(parts)
    cols = cb["columns"]
    # unique Tableau calc dimension -> bound to its real (table, column)
    assert cols["director"] == {"table": "Sheet1", "column": "Director"}
    # the field-parameter picker lands in its OWN calculated table (the Choose Date fix)
    assert cols["choose date"] == {"table": "Choose Date", "column": "Choose Date"}
    # a raw passthrough column is not a calc dimension -> absent
    assert "region" not in cols
    # 'Dup' materialised in TWO tables is ambiguous -> dropped (warn-never-wrong, caption fallback wins)
    assert "dup" not in cols
    # a model that materialised no calc dimension returns None (report keeps its standing resolution)
    assert me._column_binding_from_model(
        {"definition/tables/Plain.tmdl": "table Plain\n    column A\n        sourceColumn: A\n"}) is None


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
    assert fb["fallback_path"] == "needs-storage-decision"
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


def test_summarize_rolls_up_workbook_model_calcs_and_folds_needs_review():
    # Fix (2): a consolidated workbook builds its OWN semantic model whose calc summary lives on
    # ``model_translation_handoff`` -- NOT in ds_details. Without the rollup, those calcs never reach
    # the top-level summary and the mandatory second-compiler gate (needs_review_total) reads 0 even
    # when workbook calcs are stubbed. The fixture uses live != translated (live=3, translated=2) to
    # LOCK that ``workbook_calcs_translated`` reads the ``live`` key (translated + assisted_approved),
    # not the raw deterministic ``translated`` -- so live/needs_review is a clean partition of total.
    wb_details = [{
        "model_translation_handoff": {"summary": {
            "total": 4, "live": 3, "translated": 2, "assisted_approved": 1,
            "assisted_suggested": 0, "stub": 1, "needs_review": 1,
        }},
    }]
    s = me._summarize(ds_details=[], wb_details=wb_details, viz_available=True)

    assert s["workbook_calcs_total"] == 4
    # reads ``live`` (3), NOT the raw ``translated`` (2) -- the load-bearing lock
    assert s["workbook_calcs_translated"] == 3
    assert s["workbook_calcs_stubbed"] == 1
    assert s["workbook_calcs_needs_review"] == 1
    # live + needs_review is a clean partition of total (mirrors live = translated + assisted_approved,
    # needs_review = assisted_suggested + stub)
    assert s["workbook_calcs_translated"] + s["workbook_calcs_needs_review"] == s["workbook_calcs_total"]
    assert s["workbook_calcs_coverage_pct"] == 75.0            # round(100 * 3 / 4, 1)
    # the workbook's needs_review is folded into the mandatory second-compiler gate
    assert s["needs_review_total"] == 1


def test_summarize_datasource_only_run_leaves_workbook_calc_keys_inert():
    # Byte-identical guarantee for a datasource-only run: no wb_details -> every additive workbook_calcs
    # key is 0 and coverage is None (not 0.0), and the gate total is untouched.
    s = me._summarize(ds_details=[], wb_details=[], viz_available=True)
    assert s["workbook_calcs_total"] == 0
    assert s["workbook_calcs_translated"] == 0
    assert s["workbook_calcs_stubbed"] == 0
    assert s["workbook_calcs_needs_review"] == 0
    assert s["workbook_calcs_coverage_pct"] is None
    assert s["needs_review_total"] == 0


def test_summarize_folds_workbook_path_column_prune_into_estate_total():
    # Telemetry-gap fix: a consolidated workbook prunes hidden columns inside its OWN model build, so the
    # prune count lives on the workbook detail's ``column_prune`` -- NOT in ds_details. A pure-workbook run
    # (no standalone datasources) must therefore fold wb_details prunes into ``columns_pruned_hidden_total``
    # instead of reporting 0 while the physical collapse fired.
    wb_details = [
        {"column_prune": {"columns_emitted": 233, "columns_pruned_hidden": 2334}},
        {"column_prune": {"columns_emitted": 40, "columns_pruned_hidden": 10}},
        {"column_prune": None},                       # a workbook that pruned nothing contributes 0
        {},                                           # a workbook detail without the key at all
    ]
    s = me._summarize(ds_details=[], wb_details=wb_details, viz_available=True)
    assert s["columns_pruned_hidden_total"] == 2344


def test_summarize_column_prune_total_combines_datasource_and_workbook_paths():
    # Additive: the datasource-path prune (ds_details) and the workbook-path prune (wb_details) both feed
    # the SAME total, so a mixed estate sums both without double counting or dropping either.
    ds_details = [{"status": "migrated",
                   "column_prune": {"columns_emitted": 12, "columns_pruned_hidden": 5}}]
    wb_details = [{"column_prune": {"columns_emitted": 6, "columns_pruned_hidden": 4}}]
    s = me._summarize(ds_details=ds_details, wb_details=wb_details, viz_available=True)
    assert s["columns_pruned_hidden_total"] == 9


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
        raise ValueError("no table produced columns; it needs a storage decision.")

    monkeypatch.setattr(me, "assemble_import_model", boom)
    src = InMemoryTableauSource(datasources={"Orders DS": LIVE_TDS})
    report = migrate_estate(src, str(tmp_path / "b"))
    assert report["summary"]["datasources_fallback"] == 1
    assert report["summary"]["datasources_error"] == 0
    detail = report["datasources"][0]
    assert detail["status"] == "fallback"
    assert "no table produced columns" in detail["reason"]
    assert report["fallbacks"][0]["fallback_path"] == "needs-storage-decision"


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


# -- opt-in Tier-2 viz-advice sidecar (additive; byte-identical no-op when off) ------------------
def _advice_viz(text, name):
    """Injected viz stage that emits parts + candidate records (one typed visual, one detail table)."""
    return {
        "parts": {"definition/report.json": "{}"},
        "warnings": [],
        "ir": {"worksheets": []},
        "candidate_records": [
            {"page": "p1", "visual": "v1", "worksheet": "By Segment",
             "visual_type": "clusteredColumnChart",
             "fields": {"Category": ["Orders.Segment"], "Y": ["Orders.Sales"]}},
            {"page": "p1", "visual": "v2", "worksheet": "Detail",
             "visual_type": "tableEx", "fields": {"Values": ["Orders.Sales"]}},
        ],
    }


def test_viz_advice_off_writes_no_sidecar_and_no_key(tmp_path):
    out = tmp_path / "b"
    src = InMemoryTableauSource(workbooks={"Dash": "<workbook/>"})
    report = migrate_estate(src, str(out), viz_stage=_advice_viz, pbip=False)
    wb = report["workbooks"][0]
    assert "viz_advice" not in wb                                   # byte-identical no-op
    sidecars = [p.name for p in (out / "reports").iterdir() if p.name.endswith(".viz-advice.json")]
    assert sidecars == []


def test_viz_advice_on_writes_sidecar_and_records_summary(tmp_path):
    out = tmp_path / "b"
    src = InMemoryTableauSource(workbooks={"Dash": "<workbook/>"})
    report = migrate_estate(src, str(out), viz_stage=_advice_viz, pbip=False, viz_advice=True)
    wb = report["workbooks"][0]
    assert wb["viz_advice"]["status"] == "written"
    assert wb["viz_advice"]["path"] == "reports/Dash.viz-advice.json"
    assert wb["viz_advice"]["summary"] == {"visuals": 2, "advisable": 1, "with_alternative": 1}

    sidecar = out / "reports" / "Dash.viz-advice.json"
    assert sidecar.is_file()
    body = json.load(open(sidecar, encoding="utf-8"))
    assert body["kind"] == "tableau-to-powerbi-viz-advice"
    assert body["advice"][0]["current_type"] == "clusteredColumnChart"
    assert body["advice"][0]["advisable"] is True
    assert body["advice"][1]["advisable"] is False                 # the detail table is left alone

    # The advice is a SIBLING of the .Report folder; the PBIR definition is never touched by it.
    assert (out / "reports" / "Dash.Report" / "definition" / "report.json").is_file()
    assert not (out / "reports" / "Dash.Report" / "Dash.viz-advice.json").exists()


def test_viz_advice_on_leaves_pbir_definition_byte_identical(tmp_path):
    # The rebuilt report folder must be identical whether or not the advice sidecar is produced.
    src = InMemoryTableauSource(workbooks={"Dash": "<workbook/>"})
    off = tmp_path / "off"
    on = tmp_path / "on"
    migrate_estate(src, str(off), viz_stage=_advice_viz, pbip=False)
    migrate_estate(InMemoryTableauSource(workbooks={"Dash": "<workbook/>"}),
                   str(on), viz_stage=_advice_viz, pbip=False, viz_advice=True)
    rel = os.path.join("reports", "Dash.Report", "definition", "report.json")
    assert open(off / rel, encoding="utf-8").read() == open(on / rel, encoding="utf-8").read()


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


# Embedded SAP HANA datasource -> select_storage_mode routes it to the needs-storage-decision fallback,
# so the bound .pbip cannot be assembled (the model lands separately) and must be skipped with a warning.
SAPHANA_WORKBOOK_TWB = _viz_wb(
    _viz_ds("Hana Source", "federated.hana", "saphana.bb22", "saphana", "Stock"),
    _viz_ws("Stock by Category", "federated.hana", "Hana Source"))

# Two embedded SQL Server datasources: both are consolidated into ONE semantic model as disconnected
# table islands (Sales + Inventory), each bound to its own connection, with a single PBIR report bound
# to that one model -- exactly like a federated multi-connection datasource.
MULTI_SOURCE_TWB = _viz_wb(
    _viz_ds("Sales Source", "federated.s1", "sqlserver.s1", "sqlserver", "Sales")
    + _viz_ds("Inventory Source", "federated.s2", "sqlserver.s2", "sqlserver", "Inventory"),
    _viz_ws("Sales by Category", "federated.s1", "Sales Source")
    + _viz_ws("Inventory by Category", "federated.s2", "Inventory Source"))


# A mixed multi-datasource workbook: one mappable SQL Server datasource (real DirectQuery partition)
# plus a SAP HANA datasource whose connector is not mapped for direct M. Both are consolidated into ONE
# model -- neither is dropped: the SQL Server island lands a real partition, and the SAP HANA island
# lands an honest needs-review M partition scaffold (recorded in the model's ``partitions_needs_review``)
# rather than being silently discarded. Proves zero-drop consolidation with an honest per-island stub.
MIXED_FALLBACK_TWB = _viz_wb(
    _viz_ds("Sales Source", "federated.s1", "sqlserver.s1", "sqlserver", "Sales")
    + _viz_ds("Hana Source", "federated.hana", "saphana.bb22", "saphana", "Stock"),
    _viz_ws("Sales by Category", "federated.s1", "Sales Source")
    + _viz_ws("Stock by Category", "federated.hana", "Hana Source"))


# A single embedded datasource -> keeps the flat pbip/<WB>/ layout (no per-datasource nesting).
SOLO_SOURCE_TWB = _viz_wb(
    _viz_ds("Solo Source", "federated.s1", "sqlserver.s1", "sqlserver", "Sales"),
    _viz_ws("Sales by Category", "federated.s1", "Solo Source"))


# A two-island workbook whose calculated MEASURE lives on the SECOND embedded datasource. When the
# workbook's datasources are consolidated into ONE model, that calc MUST still land. The consolidation
# path used to auto-extract calcs scoped to the FIRST island only (extract_calcs(tds, datasource=None)),
# so every calc defined on a later island was silently dropped -- and translation coverage was falsely
# reported 100% (needs_review empty), so the mandatory second compiler never fired. Island 1 (Sales
# Source) carries no calc; island 2 (Ratio Source) defines Profit Ratio = SUM([Profit])/SUM([Sales]) on
# columns unique to that island so it resolves unambiguously in the combined model and yields real DAX.
_RATIO_ISLAND = """
    <datasource caption='Ratio Source' inline='true' name='federated.s2' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='c' name='sqlserver.s2'>
            <connection class='sqlserver' dbname='DB' server='srv.example.com' username='svc' />
          </named-connection>
        </named-connections>
        <relation connection='sqlserver.s2' name='Orders' table='[dbo].[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Segment</remote-name><local-name>[Segment]</local-name>
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
      <column caption='Profit Ratio' datatype='real' name='[Calculation_ratio1]'
              role='measure' type='quantitative'>
        <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />
      </column>
    </datasource>"""

_RATIO_WS = """
    <worksheet name='Ratio by Segment'>
      <table>
        <view>
          <datasources><datasource caption='Ratio Source' name='federated.s2' /></datasources>
          <datasource-dependencies datasource='federated.s2'>
            <column caption='Segment' datatype='string' name='[Segment]' role='dimension' type='nominal' />
            <column caption='Profit Ratio' datatype='real' name='[Calculation_ratio1]' role='measure' type='quantitative'>
              <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />
            </column>
            <column-instance column='[Segment]' derivation='None' name='[none:Segment:nk]' pivot='key' type='nominal' />
            <column-instance column='[Calculation_ratio1]' derivation='None' name='[none:Calculation_ratio1:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
        </view>
        <panes><pane><mark class='Bar' /></pane></panes>
        <rows>[federated.s2].[none:Calculation_ratio1:qk]</rows>
        <cols>[federated.s2].[none:Segment:nk]</cols>
      </table>
    </worksheet>"""

MULTI_SOURCE_SECOND_ISLAND_CALC_TWB = _viz_wb(
    _viz_ds("Sales Source", "federated.s1", "sqlserver.s1", "sqlserver", "Sales") + _RATIO_ISLAND,
    _viz_ws("Sales by Category", "federated.s1", "Sales Source") + _RATIO_WS)


# Fix (1) island-scoped resolution: two islands reuse the SAME [Amount] caption on DIFFERENT physical
# tables (Sales vs Inventory), and island 2 defines a calc MEASURE that references the colliding
# [Amount]. In the consolidated model the pooled resolver is ambiguous on [Amount] (it lives on both
# tables) -> the calc would STUB and coverage would be falsely reported. Scoping resolution to the
# calc's own island binds [Amount] to THAT island's physical table (Inventory), so the calc translates
# to the CORRECT island. This is the synthetic stand-in for the real Salesforce 4-island collision.
_ISLAND_COLLISION_CALC = """
    <datasource caption='Inventory Source' inline='true' name='federated.s2' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='c' name='sqlserver.s2'>
            <connection class='sqlserver' dbname='DB' server='srv.example.com' username='svc' />
          </named-connection>
        </named-connections>
        <relation connection='sqlserver.s2' name='Inventory' table='[dbo].[Inventory]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Category</remote-name><local-name>[Category]</local-name>
            <parent-name>[Inventory]</parent-name><local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Amount</remote-name><local-name>[Amount]</local-name>
            <parent-name>[Inventory]</parent-name><local-type>real</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
      <column caption='Stock Value' datatype='real' name='[Calculation_stock1]'
              role='measure' type='quantitative'>
        <calculation class='tableau' formula='SUM([Amount]) * 2' />
      </column>
    </datasource>"""

# Island 1 (Sales) shares the identical [Category]/[Amount] captions on its OWN table 'Sales'.
ISLAND_COLLISION_CALC_TWB = _viz_wb(
    _viz_ds("Sales Source", "federated.s1", "sqlserver.s1", "sqlserver", "Sales")
    + _ISLAND_COLLISION_CALC,
    _viz_ws("Sales by Category", "federated.s1", "Sales Source")
    + _viz_ws("Inventory by Category", "federated.s2", "Inventory Source"))


def test_workbook_consolidation_scopes_colliding_caption_to_calc_island():
    # Fix (1) regression: island 2's calc references [Amount], a caption that ALSO exists on island 1's
    # 'Sales' table. Without island-scoped resolution the pooled resolver is ambiguous -> the calc stubs
    # (or, worse, could bind to the wrong island). Island-scoped resolution binds it to THIS island's
    # 'Inventory' table, so the calc translates deterministically to the CORRECT physical table.
    with tempfile.TemporaryDirectory() as out:
        src = InMemoryTableauSource(workbooks={"Multi WB": ISLAND_COLLISION_CALC_TWB})
        report = migrate_estate(src, os.path.join(out, "b"))
        wb = report["workbooks"][0]
        assert wb["pbip_status"] == "built"
        # the colliding-caption calc is SEEN and TRANSLATED (not stubbed away on ambiguity)
        summary = wb["model_translation_handoff"]["summary"]
        assert summary["total"] >= 1
        assert summary["translated"] >= 1
        measures = os.path.join(out, "b", "pbip", "Multi WB", "Multi WB.SemanticModel",
                                "definition", "tables", "_Measures.tmdl")
        assert os.path.isfile(measures), "consolidated model has no _Measures table -- calc was dropped"
        blob = open(measures, encoding="utf-8-sig").read()
        assert "measure 'Stock Value'" in blob
        # bound to the CORRECT island's physical table (Inventory), NOT the colliding Sales table
        assert "SUM('Inventory'[Amount]) * 2" in blob
        assert "'Sales'[Amount]" not in blob
        # deterministic provenance + original formula preserved
        assert "annotation TranslatedBy = deterministic" in blob
        assert "annotation TableauFormula = SUM([Amount]) * 2" in blob


def test_workbook_consolidation_lands_calc_from_second_datasource_island():
    # Defect B regression: a measure calc defined on a NON-first embedded datasource must survive when
    # the workbook's datasources are consolidated into one model. The old path scoped auto-extraction to
    # the first (calc-less) island, silently dropping it and falsely reporting full coverage.
    with tempfile.TemporaryDirectory() as out:
        src = InMemoryTableauSource(workbooks={"Multi WB": MULTI_SOURCE_SECOND_ISLAND_CALC_TWB})
        report = migrate_estate(src, os.path.join(out, "b"))
        wb = report["workbooks"][0]
        assert wb["pbip_status"] == "built"
        # the second island's calc is SEEN by the model build (not scoped away to the first island)
        assert wb["model_translation_handoff"]["summary"]["total"] >= 1
        # and it lands as a real (translated) measure in the ONE consolidated model
        measures = os.path.join(out, "b", "pbip", "Multi WB", "Multi WB.SemanticModel",
                                "definition", "tables", "_Measures.tmdl")
        assert os.path.isfile(measures), "consolidated model has no _Measures table -- calc was dropped"
        blob = open(measures, encoding="utf-8-sig").read()
        assert "measure 'Profit Ratio'" in blob
        assert "DIVIDE(" in blob                            # SUM([Profit])/SUM([Sales]) -> real DAX


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


# -- definition-of-done gate for workbook inputs (rm-dashboard-default-discoverability) ----------
# A Tableau workbook migration is not "done" when only its semantic model lands -- its dashboards
# must be rebuilt and bound into an openable .pbip. The estate CLI now emits a machine
# definition-of-done ledger (report["definition_of_done"]) and a LOUD summary banner when a
# workbook input produced no openable, model-bound report. It is soft-but-loud: additive, never
# raises, never changes exit status. The one honest carve-out is a published-datasource workbook
# whose published datasource was not migrated in the same run (recorded "skipped", not "failed").
def test_definition_of_done_classifier_pass_fail_skip():
    wb_details = [
        {"name": "Bound WB", "pbip_status": "built", "bound_model": "M",
         "pbip_folder": "pbip/Bound WB/Bound WB.pbip"},
        {"name": "Multi WB",
         "datasource_pbips": [{"pbip_status": "built"}, {"pbip_status": "skipped"}]},
        {"name": "Orphan WB", "viz_status": "built", "pbip_status": "skipped",
         "pbip_warnings": ["manual attention required: needs a storage decision -- "
                           "workbook .pbip skipped"]},
        {"name": "Published WB", "pbip_status": "skipped",
         "binding_signal": {"kind": "published"},
         "pbip_warnings": ["manual attention required: needs a storage decision"]},
    ]
    dod = me._definition_of_done(wb_details, pbip_enabled=True)

    assert dod["applicable"] is True
    assert dod["status"] == "failed"                       # any failure -> overall failed
    assert dod["workbooks_total"] == 4
    assert dod["reports_bound"] == 2                        # Bound WB + Multi WB (any DS built)
    assert dod["reports_failed"] == 1                       # Orphan WB only
    by_name = {e["workbook"]: e for e in dod["workbooks"]}
    assert by_name["Bound WB"]["status"] == "pass"
    assert by_name["Bound WB"]["report_bound"] is True
    assert by_name["Multi WB"]["status"] == "pass"          # a multi-DS workbook with any DS bound
    assert by_name["Orphan WB"]["status"] == "failed"
    assert "needs a storage decision" in by_name["Orphan WB"]["reason"]
    assert not by_name["Orphan WB"]["reason"].startswith("manual attention required")  # prefix stripped
    assert by_name["Published WB"]["status"] == "skipped"   # the honest carve-out
    assert "published" in by_name["Published WB"]["reason"].lower()


def test_definition_of_done_classifier_pbip_disabled_and_empty():
    # --no-pbip: the user opted out of openable projects -> skipped, never failed.
    off = me._definition_of_done([{"name": "X", "pbip_status": "built"}], pbip_enabled=False)
    assert off["status"] == "skipped"
    assert off["workbooks"][0]["status"] == "skipped"
    assert "no-pbip" in off["workbooks"][0]["reason"]
    # No workbook inputs at all -> not applicable (a pure datasource run).
    none = me._definition_of_done([], pbip_enabled=True)
    assert none["applicable"] is False
    assert none["status"] == "not_applicable"
    assert none["workbooks"] == []


def test_definition_of_done_pass_end_to_end(tmp_path):
    src = InMemoryTableauSource(workbooks={"Exec Dashboard": SUPERSTORE_DASHBOARD_TWB})
    out = tmp_path / "b"
    report = migrate_estate(src, str(out))

    dod = report["definition_of_done"]
    assert dod["applicable"] is True
    assert dod["status"] == "pass"
    assert dod["reports_bound"] == 1
    assert dod["workbooks"][0]["workbook"] == "Exec Dashboard"
    assert dod["workbooks"][0]["report_bound"] is True

    summary_md = (out / "summary.md").read_text(encoding="utf-8")
    assert "DEFINITION OF DONE: PASS" in summary_md


def test_definition_of_done_failed_end_to_end_is_soft(tmp_path):
    # An injected viz builds report parts, but the workbook has no embedded datasource to bind the
    # rebuilt report to -> an ORPHANED report (the AAR failure). The gate must FAIL LOUD yet stay
    # soft: migrate_estate still returns a report and writes the bundle (exit status unchanged).
    def fake_viz(text, name):
        return {"parts": {"definition/report.json": "{}"}, "note": "rebuilt 1 sheet"}

    src = InMemoryTableauSource(workbooks={"Orphan Dashboard": "<workbook/>"})
    out = tmp_path / "b"
    report = migrate_estate(src, str(out), viz_stage=fake_viz)   # does not raise -> soft

    dod = report["definition_of_done"]
    assert dod["status"] == "failed"
    assert dod["reports_failed"] == 1
    entry = dod["workbooks"][0]
    assert entry["workbook"] == "Orphan Dashboard"
    assert entry["report_bound"] is False
    assert entry["reason"]                                   # a concrete reason is recorded

    summary_md = (out / "summary.md").read_text(encoding="utf-8")
    assert "DEFINITION OF DONE: FAILED" in summary_md
    assert "Orphan Dashboard" in summary_md                  # the failing workbook is named loudly


def test_definition_of_done_published_workbook_without_datasource_is_skipped(tmp_path):
    # A published-datasource (sqlproxy) workbook migrated WITHOUT its published datasource in the run
    # legitimately produces no bound report -- the named honest carve-out. It must be "skipped", not
    # "failed", so a real co-migrate-the-datasource limitation never trips the loud FAILED banner.
    src = InMemoryTableauSource(workbooks={"Published WB": PUBLISHED_DS_WORKBOOK_TWB})
    out = tmp_path / "b"
    report = migrate_estate(src, str(out))

    wb = report["workbooks"][0]
    assert (wb.get("binding_signal") or {}).get("kind") == "published"
    dod = report["definition_of_done"]
    assert dod["status"] == "skipped"
    assert dod["reports_failed"] == 0
    assert dod["workbooks"][0]["status"] == "skipped"

    summary_md = (out / "summary.md").read_text(encoding="utf-8")
    assert "DEFINITION OF DONE: FAILED" not in summary_md


def test_definition_of_done_not_applicable_for_datasource_only_run(tmp_path):
    # A pure datasource run has no workbook inputs -> the gate is not applicable and adds no banner
    # (the datasource-only summary head stays byte-identical to before the gate existed).
    src = InMemoryTableauSource(datasources={"Orders DS": LIVE_TDS})
    out = tmp_path / "b"
    report = migrate_estate(src, str(out))

    assert report["definition_of_done"]["applicable"] is False
    assert report["definition_of_done"]["status"] == "not_applicable"
    summary_md = (out / "summary.md").read_text(encoding="utf-8")
    assert "DEFINITION OF DONE" not in summary_md


def test_definition_of_done_main_exits_zero_with_workbook(tmp_path, capsys):
    # main() wires the gate and prints a loud one-liner, but exit status is unchanged (soft-but-loud).
    src_dir = tmp_path / "in"
    src_dir.mkdir()
    (src_dir / "Exec Dashboard.twb").write_text(SUPERSTORE_DASHBOARD_TWB, encoding="utf-8")
    out_dir = tmp_path / "out"

    rc = me.main(["-i", str(src_dir), "-o", str(out_dir)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "definition of done" in printed.lower()
    assert (out_dir / "summary.md").read_text(encoding="utf-8").count("DEFINITION OF DONE") >= 1


# Defect E -- a workbook that BUILDS an openable .pbip is NOT automatically faithful. If it stubbed a
# calc, warned a visual, dropped a model reference, or landed a review-stub partition, the report opens
# but under-delivers -- so the gate must degrade PASS -> WARN (soft; exit stays 0) and never print a
# green PASS over a low-fidelity result. A clean build still passes.
def test_definition_of_done_built_but_low_fidelity_degrades_to_warn():
    wb_details = [
        {"name": "Clean WB", "pbip_status": "built", "bound_model": "M",
         "pbip_folder": "pbip/Clean WB/Clean WB.pbip"},
        {"name": "Stubbed WB", "pbip_status": "built", "bound_model": "M",
         "pbip_folder": "pbip/Stubbed WB/Stubbed WB.pbip",
         "model_translation_handoff": {"summary": {"total": 5, "needs_review": 2}}},
        {"name": "Warned Viz WB", "pbip_status": "built", "bound_model": "M",
         "pbip_folder": "pbip/Warned Viz WB/Warned Viz WB.pbip",
         "viz_fidelity": [{"worksheet": "S1", "status": "rebuilt"},
                          {"worksheet": "S2", "status": "warned", "reason": "date grain not applied"}]},
        {"name": "Ref Drop WB", "pbip_status": "built", "bound_model": "M",
         "pbip_folder": "pbip/Ref Drop WB/Ref Drop WB.pbip",
         "pbip_ref_drops": [{"visual": "v1", "ref": "Orders.Ghost"}]},
    ]
    dod = me._definition_of_done(wb_details, pbip_enabled=True)

    assert dod["status"] == "warn"                         # no failures, but fidelity gaps -> WARN
    assert dod["reports_bound"] == 4                        # all four still opened + bound
    assert dod["reports_failed"] == 0
    assert dod["reports_warned"] == 3                       # everything except the clean build
    by_name = {e["workbook"]: e for e in dod["workbooks"]}
    assert by_name["Clean WB"]["status"] == "pass"
    assert by_name["Stubbed WB"]["status"] == "warn"
    assert "not faithfully translated" in by_name["Stubbed WB"]["reason"]
    assert by_name["Warned Viz WB"]["status"] == "warn"
    assert "warning" in by_name["Warned Viz WB"]["reason"].lower()
    assert by_name["Ref Drop WB"]["status"] == "warn"
    assert "reference" in by_name["Ref Drop WB"]["reason"].lower()
    # every WARN workbook is still report_bound (it opened) -- WARN is soft, not a failure
    assert all(by_name[n]["report_bound"] is True for n in
               ("Stubbed WB", "Warned Viz WB", "Ref Drop WB"))


def test_definition_of_done_non_openable_model_fails_loud():
    # A report bound to a model that fails the openability self-check (e.g. a duplicate column survived
    # to TMDL) OPENS but will not load -> it must FAIL LOUD, ahead of the warn/pass branch, even when
    # the workbook also carries soft fidelity gaps that would otherwise only WARN.
    dup_issue = {"ok": False,
                 "checks": {"tmdl_wellformed": True, "no_duplicate_columns": False},
                 "issues": [{"check": "no_duplicate_columns", "table": "Orders",
                             "detail": "column 'Region' is declared more than once"}]}
    wb_details = [
        {"name": "Clean WB", "pbip_status": "built", "bound_model": "M",
         "pbip_folder": "pbip/Clean WB/Clean WB.pbip",
         "openability_selfcheck": {"ok": True, "checks": {"no_duplicate_columns": True}, "issues": []}},
        # single-datasource path: the self-check lands on the workbook detail itself
        {"name": "Broken WB", "pbip_status": "built", "bound_model": "M",
         "pbip_folder": "pbip/Broken WB/Broken WB.pbip",
         "openability_selfcheck": dup_issue,
         # also carries a would-be WARN gap -> openability must win (loud > soft)
         "model_translation_handoff": {"summary": {"total": 5, "needs_review": 2}}},
        # consolidated path: the self-check lands on a datasource_pbips entry
        {"name": "Broken Consolidated WB", "pbip_status": "skipped",
         "datasource_pbips": [{"datasource": "A", "pbip_status": "built",
                               "openability_selfcheck": {"ok": True, "checks": {}, "issues": []}},
                              {"datasource": "B", "pbip_status": "built",
                               "openability_selfcheck": dup_issue}]},
    ]
    dod = me._definition_of_done(wb_details, pbip_enabled=True)

    assert dod["status"] == "failed"                        # any non-openable model -> loud overall fail
    by_name = {e["workbook"]: e for e in dod["workbooks"]}
    assert by_name["Clean WB"]["status"] == "pass"
    assert by_name["Broken WB"]["status"] == "failed"
    assert "not openable" in by_name["Broken WB"]["reason"]
    assert "Region" in by_name["Broken WB"]["reason"]       # the concrete issue is named
    assert by_name["Broken Consolidated WB"]["status"] == "failed"
    assert "not openable" in by_name["Broken Consolidated WB"]["reason"]
    # a loud failure banner is rendered (not the green pass line)
    assert "FAILED" in "\n".join(me._dod_banner(dod))


def test_dod_openability_failure_helper_tolerates_missing_and_ok():
    # No signal (missing or ok) -> None; ok is False -> a concise loud reason.
    assert me._dod_openability_failure({"name": "X"}) is None
    assert me._dod_openability_failure(
        {"openability_selfcheck": {"ok": True, "checks": {}, "issues": []}}) is None
    # ok False but no issue detail -> falls back to naming the failed check(s)
    reason = me._dod_openability_failure(
        {"openability_selfcheck": {"ok": False,
                                   "checks": {"typed_columns_declared": False}, "issues": []}})
    assert reason is not None and "typed_columns_declared" in reason



    # A hard failure still wins over a fidelity warning (failed > warn).
    mixed = me._definition_of_done(
        [{"name": "Warn WB", "pbip_status": "built",
          "model_translation_handoff": {"summary": {"needs_review": 1}}},
         {"name": "Fail WB", "pbip_status": "skipped",
          "pbip_warnings": ["manual attention required: needs a storage decision"]}],
        pbip_enabled=True)
    assert mixed["status"] == "failed"

    # The WARN banner names each degraded workbook and does NOT print a green PASS.
    warn_dod = me._definition_of_done(
        [{"name": "Warn WB", "pbip_status": "built",
          "model_translation_handoff": {"summary": {"needs_review": 3}}}],
        pbip_enabled=True)
    banner = "\n".join(me._dod_banner(warn_dod))
    assert "DEFINITION OF DONE: WARN" in banner
    assert "DEFINITION OF DONE: PASS" not in banner
    assert "Warn WB" in banner


# -- Windows MAX_PATH: a hard .pbip write failure is reported LOUD (failed), never masked -----------
# Run-2 AAR: a deep output root pushed a PBIR file past the Windows MAX_PATH (260) limit; the OS raised
# a cryptic WinError 3 mid-write, but the failure was swallowed as a benign "skipped" -- and on a
# published-datasource workbook it was further mis-reported as the "published DS not in scope"
# carve-out. These lock the fix end-to-end: 1a projects the longest path and fails fast; 1c classifies
# the write error and the definition-of-done reports it FAILED *before* the published carve-out.
def test_pbip_write_error_helpers_classify_and_record():
    import errno
    # classification matrix: projected over budget, WinError 206/3, POSIX ENAMETOOLONG -> path-length.
    assert me._classify_pbip_write_error(projected="x" * 260) == "path_too_long"
    assert me._classify_pbip_write_error(projected="x" * 100) == "write_error"
    e206 = OSError("too long"); e206.winerror = 206
    assert me._classify_pbip_write_error(e206, projected="x" * 50) == "path_too_long"
    e3 = OSError("path not found"); e3.winerror = 3
    assert me._classify_pbip_write_error(e3) == "path_too_long"
    assert me._classify_pbip_write_error(OSError(errno.ENAMETOOLONG, "name too long")) == "path_too_long"
    assert me._classify_pbip_write_error(OSError("disk full")) == "write_error"

    # record: marks failed + additive pbip_write_error + a loud warning; _dod_fail_reason prefers it.
    entry, warns = {}, []
    me._record_pbip_write_failure(entry, warns, cause="path_too_long",
                                  dest=os.path.join("C:\\", "deep", "root"), projected="Z" * 275)
    assert entry["pbip_status"] == "failed"
    err = entry["pbip_write_error"]
    assert err["cause"] == "path_too_long"
    assert err["projected_length"] == 275
    assert "MAX_PATH" in err["message"] and "shorter output root" in err["message"]
    assert warns and "MAX_PATH" in warns[0]
    # even with an unrelated pbip_warnings entry present, the write-error message wins the DoD reason.
    entry["pbip_warnings"] = ["some earlier ref-drop warning"]
    assert me._dod_fail_reason(entry) == err["message"]


def test_longest_projected_path_matches_writer_layout(tmp_path):
    parts = {"definition/tables/_Measures.tmdl": "x", "definition.tmdl": "y"}
    report_parts = {"definition/pages/p/visuals/v/visual.json": "z", ".platform": "w"}
    longest = me._longest_projected_path(str(tmp_path), "MyModel", parts, "MyReport", report_parts)
    # the deepest report visual path is the longest, and it lands under the .Report folder.
    assert longest.endswith(os.path.join(
        "MyReport.Report", "definition", "pages", "p", "visuals", "v", "visual.json"))
    assert "MyModel.SemanticModel" not in longest  # a shorter model path is never the longest


def test_definition_of_done_write_failure_not_masked_by_published_carveout():
    # THE regression: a published-datasource workbook whose .pbip write FAILED (MAX_PATH) must be
    # reported FAILED -- not swept into the benign published-DS "skipped" carve-out.
    wb = {"name": "Deep WB", "pbip_status": "failed",
          "binding_signal": {"kind": "published"},
          "pbip_write_error": {
              "cause": "path_too_long",
              "message": ("workbook .pbip output path exceeds the Windows MAX_PATH (260) limit -- "
                          "re-run with a shorter output root")},
          "pbip_warnings": ["manual attention required: ... MAX_PATH ..."]}
    dod = me._definition_of_done([wb], pbip_enabled=True)
    assert dod["status"] == "failed"
    assert dod["reports_failed"] == 1
    entry = dod["workbooks"][0]
    assert entry["status"] == "failed"                       # NOT "skipped"
    assert "MAX_PATH" in entry["reason"]                     # the classified cause surfaces
    banner = "\n".join(me._dod_banner(dod))
    assert "DEFINITION OF DONE: FAILED" in banner
    assert "Deep WB" in banner and "MAX_PATH" in banner


def test_pbip_write_oserror_reported_failed_end_to_end(tmp_path, monkeypatch):
    # A write-time OSError (simulated MAX_PATH WinError 206) is caught, classified, and reported FAILED
    # end-to-end through migrate_estate -- not swallowed as a silent skip. MAX_PATH is raised so the
    # pre-flight never fires and the OSError catch is exercised deterministically on any host/tmp depth.
    monkeypatch.setattr(me, "MAX_PATH", 100_000)

    def _boom(*a, **k):
        e = OSError(206, "The filename or extension is too long")
        e.winerror = 206
        raise e
    monkeypatch.setattr(me, "write_local_pbip", _boom)

    src = InMemoryTableauSource(workbooks={"Exec Dashboard": SUPERSTORE_DASHBOARD_TWB})
    report = migrate_estate(src, str(tmp_path / "b"))
    wb = report["workbooks"][0]
    assert wb["pbip_status"] == "failed"                     # not "skipped"
    assert wb["pbip_write_error"]["cause"] == "path_too_long"
    assert wb["pbip_write_error"]["winerror"] == 206
    dod = report["definition_of_done"]
    assert dod["status"] == "failed"
    assert "MAX_PATH" in dod["workbooks"][0]["reason"]


@pytest.mark.skipif(os.name != "nt", reason="the MAX_PATH long-path warning is Windows-only")
def test_pbip_long_path_downgraded_to_warning_still_builds(tmp_path, monkeypatch):
    # 1b era: a projected path at/over MAX_PATH no longer FAILS the build -- the writer lifts the limit
    # via ``\\?\`` long-path writes, so the .pbip still builds and only a NON-FATAL warning is recorded
    # (recommending a shorter root so the LOCAL .pbip opens in Power BI Desktop). MAX_PATH is lowered so
    # any real tmp path trips the guard; the writer is stubbed to a no-op success so no deep tree is cut.
    monkeypatch.setattr(me, "MAX_PATH", 50)
    hit = {}

    def _ok(*a, **k):
        hit["called"] = True
        return "stub.pbip"

    monkeypatch.setattr(me, "write_local_pbip", _ok)

    src = InMemoryTableauSource(workbooks={"Exec Dashboard": SUPERSTORE_DASHBOARD_TWB})
    report = migrate_estate(src, str(tmp_path / "b"))
    wb = report["workbooks"][0]
    assert hit.get("called")                     # the write PROCEEDED -- it was not aborted pre-flight
    assert wb["pbip_status"] == "built"          # built, NOT "failed"/"skipped"
    assert "pbip_write_error" not in wb          # a warning, not a recorded failure
    assert any("MAX_PATH" in w for w in wb.get("pbip_warnings", []))
    assert report["definition_of_done"]["status"] in ("pass", "warn")  # never "failed"


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


# -- pre-build scan: the datasource-before-workbook gate -----------------------
def test_scan_estate_flags_missing_published_datasource():
    # A published-datasource (sqlproxy) workbook whose datasource is NOT in scope must be flagged as
    # MISSING so the runbook fetches it FIRST -- never building the workbook to an empty report.
    src = InMemoryTableauSource(workbooks={"Published WB": PUBLISHED_DS_WORKBOOK_TWB})
    manifest = me.scan_estate(src)
    assert manifest["missing_published_datasources"] == ["Superstore (Published)"]
    wb = manifest["workbooks"][0]
    assert wb["name"] == "Published WB"
    assert wb["kind"] == "published"
    assert wb["published_ds_name"] == "Superstore (Published)"
    assert wb["datasource_present"] is False


def test_scan_estate_present_when_datasource_in_scope():
    # With the published datasource present (its stem normalizes to the workbook's published caption
    # via the SAME _norm_ds key the build uses), nothing is missing -- safe to build.
    src = InMemoryTableauSource(
        datasources={"Superstore (Published)": "<datasource/>"},
        workbooks={"Published WB": PUBLISHED_DS_WORKBOOK_TWB})
    manifest = me.scan_estate(src)
    assert manifest["missing_published_datasources"] == []
    assert manifest["workbooks"][0]["datasource_present"] is True
    assert manifest["datasources_present"] == ["Superstore (Published)"]


def test_scan_estate_embedded_workbook_reports_nothing_missing():
    # An EMBEDDED-datasource workbook carries its own model -> no published datasource to fetch first.
    src = InMemoryTableauSource(workbooks={"Embedded WB": SUPERSTORE_DASHBOARD_TWB})
    manifest = me.scan_estate(src)
    assert manifest["missing_published_datasources"] == []
    assert manifest["workbooks"][0]["kind"] == "embedded"


def test_scan_cli_writes_manifest_and_gates_on_missing(tmp_path):
    # The --scan CLI writes scan.json and exits NON-ZERO while a published datasource is missing
    # (the runbook's hard gate: do not build until this exits 0), then exits 0 once it is in scope.
    indir = tmp_path / "in"
    outdir = tmp_path / "out"
    indir.mkdir()
    with open(indir / "Published WB.twb", "w", encoding="utf-8-sig") as fh:
        fh.write(PUBLISHED_DS_WORKBOOK_TWB)

    rc = me.main(["-i", str(indir), "-o", str(outdir), "--scan"])
    assert rc == 1  # missing datasource -> gate blocks the build
    manifest = json.loads((outdir / "scan.json").read_text(encoding="utf-8"))
    assert manifest["missing_published_datasources"] == ["Superstore (Published)"]

    # Drop the published datasource into scope; the gate now clears (exit 0).
    with open(indir / "Superstore (Published).tds", "w", encoding="utf-8-sig") as fh:
        fh.write("<datasource/>")
    rc2 = me.main(["-i", str(indir), "-o", str(outdir), "--scan"])
    assert rc2 == 0
    manifest2 = json.loads((outdir / "scan.json").read_text(encoding="utf-8"))
    assert manifest2["missing_published_datasources"] == []


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
                  param_binding=None, model_table=None, field_map=None, column_binding=None,
                  resources=None):
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
    assert entry["tier"] == "rebuilt"          # clean rebuild, no deferral note
    for f in fid:
        assert set(f) == {"worksheet", "visual_type", "status", "reason", "tier"}
        assert f["status"] in {"rebuilt", "warned"}          # status unchanged (additive tier)
        assert f["tier"] in {"rebuilt", "rebuilt_with_deferrals", "degraded", "empty"}
        # the additive tier never contradicts the existing status
        if f["status"] == "rebuilt":
            assert f["tier"] in {"rebuilt", "rebuilt_with_deferrals"}


def test_fidelity_tier_classifies_each_bucket():
    # empty: no faithful visual emitted (unsupported / no visual type), regardless of status.
    assert me._fidelity_tier("warned", None, "manual attention required: unsupported visual type") == "empty"
    assert me._fidelity_tier("warned", "unsupported", "anything") == "empty"
    assert me._fidelity_tier("rebuilt", None, None) == "empty"
    # rebuilt: a clean rebuild with no deferral note.
    assert me._fidelity_tier("rebuilt", "column", None) == "rebuilt"
    assert me._fidelity_tier("rebuilt", "column", "") == "rebuilt"
    # rebuilt_with_deferrals: a rebuilt visual that recorded a deferral note (still renders).
    assert me._fidelity_tier("rebuilt", "column", "implicit row count binds on model rebind") \
        == "rebuilt_with_deferrals"
    # rebuilt_with_deferrals: a WARNED worksheet that still rendered a real visual, warned only by a
    # documented faithful-or-stub deferral (dropped measure filter / date grain / default palette).
    for r in ("aggregate/measure filter on 'SUM(Sales)' is not mapped to a slicer",
              "Day-Trunc grain not applied", "used Tableau's default continuous palette"):
        assert me._fidelity_tier("warned", "matrix", r) == "rebuilt_with_deferrals"
    # degraded: a warned worksheet with a real visual but a non-deferral (genuine) problem.
    assert me._fidelity_tier("warned", "column", "manual attention required: something broke") == "degraded"


def test_viz_fidelity_tiers_deferral_vs_degraded_vs_empty():
    # A synthetic viz result exercising every tier through the real _viz_fidelity.
    result = {
        "ir": {"worksheets": [
            {"name": "Clean", "visual_type": "column"},                       # -> rebuilt
            {"name": "Noted", "visual_type": "column", "fidelity_note": "row count deferred"},  # -> rebuilt_with_deferrals
            {"name": "Deferred", "visual_type": "matrix"},                     # warned by a deferral -> rebuilt_with_deferrals
            {"name": "Broken", "visual_type": "line"},                        # warned, non-deferral -> degraded
            {"name": "NoViz", "visual_type": None},                           # -> empty
        ]},
        "warnings": [
            {"scope": "worksheet", "name": "Deferred",
             "reason": "aggregate/measure filter on 'SUM(Sales)' is not mapped to a slicer"},
            {"scope": "worksheet", "name": "Broken",
             "reason": "manual attention required: axis binding lost"},
        ],
    }
    tiers = {f["worksheet"]: f["tier"] for f in me._viz_fidelity(result)}
    assert tiers == {"Clean": "rebuilt", "Noted": "rebuilt_with_deferrals",
                     "Deferred": "rebuilt_with_deferrals", "Broken": "degraded", "NoViz": "empty"}
    # status stays binary (additive guarantee): the deferral-warned worksheet is still "warned".
    statuses = {f["worksheet"]: f["status"] for f in me._viz_fidelity(result)}
    assert statuses["Deferred"] == "warned" and statuses["Clean"] == "rebuilt"


def test_workbook_pbip_skipped_on_fallback_datasource(tmp_path):
    src = InMemoryTableauSource(workbooks={"Hana WB": SAPHANA_WORKBOOK_TWB})
    report = migrate_estate(src, str(tmp_path / "b"))
    wb = report["workbooks"][0]
    # the bare reports/ rebuild still happens; only the bound, openable .pbip is skipped
    assert wb["viz_status"] == "built"
    assert wb["pbip_status"] == "skipped"
    assert wb["pbip_folder"] is None
    assert any("needs a storage decision" in w for w in wb["pbip_warnings"])
    assert all(w.startswith("manual attention required: ") for w in wb["pbip_warnings"])
    assert not (tmp_path / "b" / "pbip" / "Hana WB").exists()
    assert report["summary"]["workbooks_pbip_built"] == 0


def test_workbook_pbip_consolidates_multiple_datasources_into_one_model():
    # Multiple embedded datasources -> ONE openable project (flat pbip/<WB>/) whose single semantic
    # model carries every datasource's tables as disconnected islands, with one report bound to it.
    # Zero silent drops: both islands land in the one model. The top-level bound_model is the WORKBOOK
    # (not a per-datasource split), and no per-datasource projects are produced.
    with tempfile.TemporaryDirectory() as out:
        src = InMemoryTableauSource(workbooks={"Multi WB": MULTI_SOURCE_TWB})
        report = migrate_estate(src, os.path.join(out, "b"))
        wb = report["workbooks"][0]
        assert wb["pbip_status"] == "built"
        assert wb["bound_model"] == "Multi WB"          # one model named for the workbook
        # every embedded datasource is folded in -- none silently dropped or warned away
        assert not any("secondary datasource" in w for w in wb["pbip_warnings"])
        assert wb["pbip_warnings"] == []
        # the consolidation audit trail lists every island that landed (anti-silent-drop proof)
        assert set(wb["consolidated_datasources"]) == {"Sales Source", "Inventory Source"}
        # no per-datasource split: the legacy nested rollup key is not set
        assert "datasource_pbips" not in wb
        # ONE flat project on disk with ONE model + ONE report
        base = os.path.join(out, "b", "pbip", "Multi WB")
        assert os.path.isfile(os.path.join(base, "Multi WB.pbip"))
        assert os.path.isdir(os.path.join(base, "Multi WB.Report"))
        assert os.path.isdir(os.path.join(base, "Multi WB.SemanticModel"))
        # both islands' tables land in the SINGLE model (the zero-drop guarantee)
        tables = os.path.join(base, "Multi WB.SemanticModel", "definition", "tables")
        assert os.path.isfile(os.path.join(tables, "Sales.tmdl"))
        assert os.path.isfile(os.path.join(tables, "Inventory.tmdl"))


def test_workbook_pbip_consolidated_model_routes_each_island_to_its_own_connector():
    # Each island in the consolidated model keeps its OWN upstream connection -- exactly like a
    # federated multi-connection datasource. Here both are SQL Server, so each table emits its own
    # Sql.Database partition (nothing is collapsed onto a single shared source).
    with tempfile.TemporaryDirectory() as out:
        src = InMemoryTableauSource(workbooks={"Multi WB": MULTI_SOURCE_TWB})
        migrate_estate(src, os.path.join(out, "b"))
        tables = os.path.join(out, "b", "pbip", "Multi WB", "Multi WB.SemanticModel",
                              "definition", "tables")
        for tbl in ("Sales", "Inventory"):
            tmdl = open(os.path.join(tables, f"{tbl}.tmdl"), encoding="utf-8-sig").read()
            assert "Sql.Database(" in tmdl                       # its own DB partition, not dropped
            assert f'Item="{tbl}"' in tmdl                       # bound to its own source table


def test_workbook_pbip_consolidates_mixed_connectors_with_honest_stub():
    # A workbook mixing a mappable connector (SQL Server) with an unmapped one (SAP HANA) consolidates
    # BOTH into one model -- neither is dropped. The SQL Server island lands a real partition; the SAP
    # HANA island lands an honest needs-review M scaffold (recorded in partitions_needs_review) instead
    # of being silently discarded. This is the "stub it, never drop it" guarantee.
    with tempfile.TemporaryDirectory() as out:
        src = InMemoryTableauSource(workbooks={"Mixed WB": MIXED_FALLBACK_TWB})
        report = migrate_estate(src, os.path.join(out, "b"))
        wb = report["workbooks"][0]
        assert wb["pbip_status"] == "built"
        assert wb["bound_model"] == "Mixed WB"
        assert set(wb["consolidated_datasources"]) == {"Sales Source", "Hana Source"}
        # both islands' tables land -- the SAP HANA one is stubbed, never dropped
        tables = os.path.join(out, "b", "pbip", "Mixed WB", "Mixed WB.SemanticModel",
                              "definition", "tables")
        sales = open(os.path.join(tables, "Sales.tmdl"), encoding="utf-8-sig").read()
        stock = open(os.path.join(tables, "Stock.tmdl"), encoding="utf-8-sig").read()
        assert "Sql.Database(" in sales                          # real partition for the mapped island
        assert "saphana" in stock.lower()                        # honest needs-review scaffold, present
        # the incomplete partition is recorded honestly on the workbook detail (not silently passed)
        review = {e.get("table") for e in (wb.get("partitions_needs_review") or [])}
        assert "Stock" in review


def test_summary_counts_consolidated_workbooks():
    # The estate summary counts one consolidated project per workbook, and flags workbooks that folded
    # in multiple datasources (workbooks_multi_datasource) from the consolidation audit trail.
    with tempfile.TemporaryDirectory() as out:
        src = InMemoryTableauSource(workbooks={"Multi WB": MULTI_SOURCE_TWB,
                                               "Solo WB": SOLO_SOURCE_TWB})
        report = migrate_estate(src, os.path.join(out, "b"))
        s = report["summary"]
        assert s["workbooks_multi_datasource"] == 1          # only "Multi WB" consolidated several
        assert s["datasource_pbips_total"] == 2              # one project per workbook
        assert s["datasource_pbips_built"] == 2              # both build
        assert s["workbooks_pbip_built"] == 2


def test_summary_consolidated_mixed_connector_workbook_still_builds():
    # A workbook mixing a mapped and an unmapped connector still consolidates into one BUILT project
    # (the unmapped island is stubbed, not dropped), and is flagged as multi-datasource.
    with tempfile.TemporaryDirectory() as out:
        src = InMemoryTableauSource(workbooks={"Mixed WB": MIXED_FALLBACK_TWB})
        report = migrate_estate(src, os.path.join(out, "b"))
        s = report["summary"]
        assert s["workbooks_multi_datasource"] == 1
        assert s["datasource_pbips_total"] == 1
        assert s["datasource_pbips_built"] == 1              # one consolidated model, built


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


# -- stale-output build guard (refuse a FRESH build over a prior run's report.json) --------------
# Companion to new_run.py: the minter keeps the INPUT side fresh; this guard keeps the OUTPUT side
# fresh, so a second migration never silently mixes into a first migration's bundle. The documented
# second-compiler landing re-run (--approved-dax / --author / --second-compile) and an explicit
# --force are exempt.

def test_cli_refuses_plain_rebuild_over_existing_report(fixtures_dir, tmp_path, capsys):
    out = str(tmp_path / "b")
    assert me.main(["-i", fixtures_dir, "-o", out]) == 0
    original = open(os.path.join(out, "report.json"), "rb").read()
    capsys.readouterr()  # drop the first build's output

    rc = me.main(["-i", fixtures_dir, "-o", out])  # plain rebuild into the SAME dir
    assert rc == 2
    printed = capsys.readouterr().out
    assert "[STOP]" in printed and "Refusing to build" in printed
    # Guard returns BEFORE the build, so the prior report.json is left untouched.
    assert open(os.path.join(out, "report.json"), "rb").read() == original


def test_cli_force_allows_rebuild_over_existing_report(fixtures_dir, tmp_path):
    out = str(tmp_path / "b")
    assert me.main(["-i", fixtures_dir, "-o", out]) == 0
    assert me.main(["-i", fixtures_dir, "-o", out, "--force"]) == 0
    # --overwrite is an accepted alias for --force.
    assert me.main(["-i", fixtures_dir, "-o", out, "--overwrite"]) == 0


def test_cli_approved_dax_rerun_into_existing_bundle_bypasses_guard(fixtures_dir, tmp_path):
    # The documented second-compiler loop: build, author approved DAX, re-run --approved-dax into
    # the SAME bundle to land it. That intentional re-run must NOT trip the stale-output guard.
    out = str(tmp_path / "b")
    assert me.main(["-i", fixtures_dir, "-o", out]) == 0
    approved_json = tmp_path / "approved.json"
    approved_json.write_text(json.dumps({"Running Amount": _APPROVED_RUNNING_AMOUNT_DAX}),
                             encoding="utf-8")
    rc = me.main(["-i", fixtures_dir, "-o", out, "--approved-dax", str(approved_json)])
    assert rc == 0
    on_disk = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
    detail = next(d for d in on_disk["datasources"] if d["name"] == "widget_sales")
    by_status = {m["measure"]: m["status"] for m in detail["measures"]}
    assert by_status["Running Amount"] == "assisted-approved"


def test_cli_second_compile_rerun_into_existing_bundle_bypasses_guard(fixtures_dir, tmp_path):
    out = str(tmp_path / "b")
    assert me.main(["-i", fixtures_dir, "-o", out]) == 0
    assert me.main(["-i", fixtures_dir, "-o", out, "--second-compile"]) == 0


def test_cli_scan_not_blocked_by_existing_report(fixtures_dir, tmp_path, capsys):
    # --scan is read-only pre-build discovery; it legitimately writes scan.json into a folder that
    # already holds a prior report.json and must never be caught by the build guard.
    out = str(tmp_path / "b")
    assert me.main(["-i", fixtures_dir, "-o", out]) == 0
    capsys.readouterr()
    rc = me.main(["-i", fixtures_dir, "-o", out, "--scan"])
    assert rc == 0  # fixtures have no missing published datasource
    assert "[STOP]" not in capsys.readouterr().out
    assert os.path.isfile(os.path.join(out, "scan.json"))


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


# -- second-compiler landing through the estate command (--approved-dax) -------
# The estate orchestrator threads an opt-in ``approved_calc_dax`` ({calc_name: dax}) mapping into
# every model build so a Tier-0 stub whose name matches lands as a LIVE, audit-stamped measure --
# the documented, no-improvisation way to redeploy the assisted (second-compiler) tier in bulk.
# ``Running Amount`` (RUNNING_SUM(...)) is the WIDGET_SALES_TDS measure that stubs on the
# datasource path, so it is the natural subject.
_APPROVED_RUNNING_AMOUNT_DAX = ("CALCULATE ( SUM ( 'Sales'[Amount] ), "
                                "FILTER ( ALLSELECTED ( 'Sales' ), TRUE () ) )")


def _read_measures_tmdl(bundle_root, ds_folder="widget_sales.SemanticModel"):
    path = os.path.join(bundle_root, "semantic_models", ds_folder,
                        "definition", "tables", "_Measures.tmdl")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_load_approved_dax_returns_none_for_falsy_path():
    assert me._load_approved_dax(None) is None
    assert me._load_approved_dax("") is None


def test_load_approved_dax_reads_mapping_and_tolerates_bom(tmp_path):
    p = tmp_path / "approved.json"
    # written WITH a BOM (utf-8-sig) -- hand-authored on Windows is the common case
    p.write_text(json.dumps({"Running Amount": _APPROVED_RUNNING_AMOUNT_DAX}), encoding="utf-8-sig")
    assert me._load_approved_dax(str(p)) == {"Running Amount": _APPROVED_RUNNING_AMOUNT_DAX}


def test_load_approved_dax_empty_object_is_none(tmp_path):
    p = tmp_path / "approved.json"
    p.write_text("{}", encoding="utf-8")
    assert me._load_approved_dax(str(p)) is None


def test_load_approved_dax_missing_file_raises(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        me._load_approved_dax(str(tmp_path / "nope.json"))


def test_load_approved_dax_non_object_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(["Running Amount", "dax"]), encoding="utf-8")
    with pytest.raises(ValueError, match="calc name -> DAX"):
        me._load_approved_dax(str(p))


def test_load_approved_dax_non_string_value_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"Running Amount": 5}), encoding="utf-8")
    with pytest.raises(ValueError, match="calc name -> DAX"):
        me._load_approved_dax(str(p))


def test_load_approved_dax_unreadable_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not readable JSON"):
        me._load_approved_dax(str(p))


def test_load_approved_dax_accepts_dict_form_with_table(tmp_path):
    # additive dict value form: {"dax": ..., "table": ...} lets an approval also name a home table.
    p = tmp_path / "approved.json"
    entry = {"dax": "\"US\"", "table": "REP"}
    p.write_text(json.dumps({"Geo Tag": entry, "Running Amount": _APPROVED_RUNNING_AMOUNT_DAX}),
                 encoding="utf-8-sig")
    got = me._load_approved_dax(str(p))
    assert got == {"Geo Tag": entry, "Running Amount": _APPROVED_RUNNING_AMOUNT_DAX}


def test_load_approved_dax_dict_without_dax_string_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"Geo Tag": {"table": "REP"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="calc name -> DAX"):
        me._load_approved_dax(str(p))


def test_load_approved_dax_dict_non_string_table_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"Geo Tag": {"dax": "\"US\"", "table": 5}}), encoding="utf-8")
    with pytest.raises(ValueError, match="calc name -> DAX"):
        me._load_approved_dax(str(p))


def test_migrate_estate_approved_dax_lands_stub_as_assisted_approved(fixtures_dir, tmp_path):
    out = str(tmp_path / "bundle")
    report = migrate_estate(LocalFilesSource(fixtures_dir), out,
                            approved_calc_dax={"Running Amount": _APPROVED_RUNNING_AMOUNT_DAX})

    detail = next(d for d in report["datasources"] if d["name"] == "widget_sales")
    by_row = {m["measure"]: m for m in detail["measures"]}
    # the formerly-stubbed table calc is now a live, human-approved measure
    assert by_row["Running Amount"]["status"] == "assisted-approved"
    assert by_row["Running Amount"]["dax"] == _APPROVED_RUNNING_AMOUNT_DAX
    # the deterministically-translated measures are untouched
    assert by_row["Total Amount"]["status"] == "translated"
    # it is no longer counted as needing review
    handoff = detail.get("translation_handoff") or {}
    assert not any(r.get("name") == "Running Amount" for r in handoff.get("needs_review", []))

    # the approved DAX + its provenance annotation actually land in the emitted TMDL
    tmdl = _read_measures_tmdl(out)
    assert _APPROVED_RUNNING_AMOUNT_DAX in tmdl
    assert "assisted translation (human-approved)" in tmdl


def test_migrate_estate_approved_dax_non_matching_name_leaves_stub(fixtures_dir, tmp_path):
    # An approval whose name does not match any stub is inert -- the calc stays a stub, never
    # mis-bound to an unrelated measure.
    out = str(tmp_path / "bundle")
    report = migrate_estate(LocalFilesSource(fixtures_dir), out,
                            approved_calc_dax={"Some Other Calc": "SUM ( 'Sales'[Amount] )"})
    detail = next(d for d in report["datasources"] if d["name"] == "widget_sales")
    by_status = {m["measure"]: m["status"] for m in detail["measures"]}
    assert by_status["Running Amount"] == "stub"


def test_migrate_estate_approved_dax_none_is_a_noop(fixtures_dir, tmp_path):
    # The default (None) and an empty mapping both leave the run byte-identical to no approval:
    # Running Amount stays a stub exactly as before the flag existed.
    for idx, approved in enumerate((None, {})):
        out = str(tmp_path / f"bundle_{idx}")
        report = migrate_estate(LocalFilesSource(fixtures_dir), out, approved_calc_dax=approved)
        detail = next(d for d in report["datasources"] if d["name"] == "widget_sales")
        by_status = {m["measure"]: m["status"] for m in detail["measures"]}
        assert by_status["Running Amount"] == "stub"


def test_main_approved_dax_flag_lands_via_cli(fixtures_dir, tmp_path):
    # End-to-end through the documented CLI: --approved-dax <file.json> lands the approved measure.
    out = str(tmp_path / "bundle")
    approved_json = tmp_path / "approved.json"
    approved_json.write_text(json.dumps({"Running Amount": _APPROVED_RUNNING_AMOUNT_DAX}),
                             encoding="utf-8")

    rc = me.main(["-i", fixtures_dir, "-o", out, "--approved-dax", str(approved_json)])
    assert rc == 0

    on_disk = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
    detail = next(d for d in on_disk["datasources"] if d["name"] == "widget_sales")
    by_status = {m["measure"]: m["status"] for m in detail["measures"]}
    assert by_status["Running Amount"] == "assisted-approved"
    assert _APPROVED_RUNNING_AMOUNT_DAX in _read_measures_tmdl(out)


def test_main_approved_dax_missing_file_errors(fixtures_dir, tmp_path):
    # A bad --approved-dax path fails fast (argparse error -> SystemExit) so a typo never silently
    # drops an approval.
    out = str(tmp_path / "bundle")
    with pytest.raises(SystemExit):
        me.main(["-i", fixtures_dir, "-o", out, "--approved-dax", str(tmp_path / "missing.json")])


# -- Spec-4 SECOND-COMPILER landing pre-pass wiring (--second-compile / --author) ----------------
# The opt-in threads ``second_compiler.land_report`` through migrate_workbook (so estate + standalone
# share one path): keystone-dependent stub chains land as gated, faithful DAX and flow through the
# SAME approved-dax landing seam. Default-off must be byte-identical (no report key, no landing).
def _sc_wb(calc_cols):
    """A single-datasource (SQL Server) workbook whose resolver binds Sales/Region/Order Date --
    the LIVE shape the driver needs to translate a keystone."""
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook><datasources>
 <datasource formatted-name='Superstore' caption='Sales' name='fed.sales' version='18.1'>
  <connection class='federated'>
   <named-connections><named-connection caption='myserver' name='sqlserver.0a1b2c'>
     <connection authentication='sqlserver' class='sqlserver' dbname='Superstore'
                 server='myserver.database.windows.net' username='svc' />
   </named-connection></named-connections>
   <relation connection='sqlserver.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
   <metadata-records>
    <metadata-record class='column'><remote-name>Sales</remote-name><local-name>[Sales]</local-name><parent-name>[Orders]</parent-name><local-type>real</local-type></metadata-record>
    <metadata-record class='column'><remote-name>Region</remote-name><local-name>[Region]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
    <metadata-record class='column'><remote-name>Order Date</remote-name><local-name>[Order Date]</local-name><parent-name>[Orders]</parent-name><local-type>date</local-type></metadata-record>
   </metadata-records>
  </connection>
  {calc_cols}
 </datasource>
</datasources></workbook>"""


def _sc_col(caption, name, formula, role="measure"):
    return (f"<column caption='{caption}' name='[{name}]' role='{role}'>"
            f"<calculation class='tableau' formula='{formula}'/></column>")


# Base stubs (SCRIPT_REAL has no faithful DAX); Plus/Ratio form the measure-of-measure chain.
_SC_STUB_CHAIN = "\n".join([
    _sc_col("Base", "Calculation_base", 'SCRIPT_REAL(&quot;x&quot;, SUM([Sales]))'),
    _sc_col("Plus", "Calculation_plus", "[Base] + 1"),
    _sc_col("Ratio", "Calculation_ratio", "[Base] / [Plus]"),
])
# A detector-recognizable keystone (Spec-7 year-gated idiom) needs NO authored override.
_SC_DETECTOR_CHAIN = "\n".join([
    _sc_col("Base", "Calculation_base", "IF YEAR([Order Date]) = 2023 THEN [Sales] END"),
    _sc_col("Plus", "Calculation_plus", "[Base] + 1"),
])


def _measures_tmdl_text(bundle_root, wb_name="Chain WB", model="Sales.SemanticModel"):
    path = os.path.join(bundle_root, "pbip", wb_name, model,
                        "definition", "tables", "_Measures.tmdl")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_second_compile_default_off_is_byte_identical(tmp_path):
    # Default-off: no ``second_compile`` report key, and the keystone base stays an inert stub (its
    # SUM('Orders'[Sales]) DAX never lands). This is the additive/byte-identical guarantee.
    src = InMemoryTableauSource(workbooks={"Chain WB": _sc_wb(_SC_STUB_CHAIN)})
    detail = migrate_workbook(src, write_to=str(tmp_path / "off"), wb_id="Chain WB")
    assert detail["pbip_status"] == "built"
    assert "second_compile" not in detail
    tmdl = _measures_tmdl_text(str(tmp_path / "off"))
    assert "SUM('Orders'[Sales])" not in tmdl          # the base did not land -- it is a stub


def test_second_compile_on_authored_keystone_lands_whole_chain(tmp_path):
    # Opt-in with an authored keystone lands Base + the cascaded Plus/Ratio into the REAL emitted
    # model (assisted-approved), and stamps an additive ``second_compile`` record on the detail.
    src = InMemoryTableauSource(workbooks={"Chain WB": _sc_wb(_SC_STUB_CHAIN)})
    detail = migrate_workbook(src, write_to=str(tmp_path / "on"), wb_id="Chain WB",
                              second_compile=True, authored={"Base": "SUM('Orders'[Sales])"})
    sc = detail["second_compile"]
    assert sc["count"] == 3
    assert set(sc["landed"]) == {"Base", "Plus", "Ratio"}
    assert sc["authored"] == ["Base"]
    assert set(sc["cascaded"]) == {"Plus", "Ratio"}
    tmdl = _measures_tmdl_text(str(tmp_path / "on"))
    assert "measure Base = SUM('Orders'[Sales])" in tmdl
    assert "measure Plus = [Base] + 1" in tmdl
    assert "measure Ratio = DIVIDE([Base], [Plus])" in tmdl
    # provenance preserved: assisted stamp + original Tableau formula annotation kept
    assert "assisted translation (human-approved)" in tmdl
    assert 'TableauFormula = SCRIPT_REAL("x", SUM([Sales]))' in tmdl


def test_second_compile_prepass_human_approved_wins_over_supplement(tmp_path):
    # The pre-pass merges the supplement UNDER approved_calc_dax, so an explicit human-approved entry
    # for the same calc name always wins (explicit intent beats an auto-landed keystone).
    src = InMemoryTableauSource(workbooks={"Chain WB": _sc_wb(_SC_STUB_CHAIN)})
    merged, sc = me._second_compile_prepass(
        src, "Chain WB", {"Base": "SUM('Orders'[Profit])"}, {"Base": "SUM('Orders'[Sales])"})
    assert merged["Base"] == "SUM('Orders'[Profit])"   # the pre-existing approval, not the supplement
    assert merged["Plus"] and merged["Ratio"]          # dependents still cascade in
    assert sc["count"] == 3


def test_second_compile_prepass_fail_closed_on_unreadable_workbook():
    # Any failure in the driver leaves the approved map untouched and records an honest note -- the
    # opt-in never corrupts the build (fail-closed).
    class _Boom:
        def read_workbook(self, wb_id):
            raise RuntimeError("cannot read")

    approved = {"Existing": "SUM('Orders'[Sales])"}
    merged, sc = me._second_compile_prepass(_Boom(), "WB", approved, None)
    assert merged == approved                           # unchanged
    assert sc["count"] == 0 and "note" in sc


# -- second-compiler GUARDS (caller-side wiring: model-aware reference gate + reconciliation oracle) --
# _second_compile_guards builds a rejection-only guard bundle from a PRIOR build's on-disk TMDL/CSV and
# the workbook resolver; _second_compile_prepass threads it into land_report. Guards NEVER author or
# alter a candidate -- they only reject one that names a non-existent model reference (the (copy)_NNNN
# trap) or numerically diverges from its Tableau formula. output_dir with no prior artifacts -> guards
# None -> byte-identical to the unguarded pass.
def test_second_compile_guards_none_without_prior_artifacts(tmp_path):
    # No dir / missing dir / empty dir -> None (the byte-identical guarantee: nothing to guard against).
    wb = _sc_wb(_SC_STUB_CHAIN)
    assert me._second_compile_guards(wb, None) is None
    assert me._second_compile_guards(wb, str(tmp_path / "does-not-exist")) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert me._second_compile_guards(wb, str(empty)) is None


def test_second_compile_guards_csv_seeds_reconciliation_oracle(tmp_path):
    # A dir holding landed CSVs -> the oracle half is active (tables loaded, surface None). The oracle
    # rejects a candidate whose value diverges from the Tableau formula and passes a faithful one.
    import second_compiler as SC
    csvdir = tmp_path / "csv"
    csvdir.mkdir()
    with open(csvdir / "Orders.csv", "w", encoding="utf-8") as fh:
        fh.write("Sales,Profit\n100,10\n200,20\n300,30\n")
    guards = me._second_compile_guards(_sc_wb(_SC_STUB_CHAIN), str(csvdir))
    assert guards is not None
    assert guards["surface"] is None                     # no TMDL -> reference gate inactive
    assert sorted(guards["tables"]) == ["Orders"]
    assert guards["tables"]["Orders"]["columns"] == ["Sales", "Profit"]
    # oracle FIRES: a wrong candidate (Profit total != the Tableau Sales total) is rejected; the right one passes.
    assert SC._gate_ok("SUM('Orders'[Profit])", True, guards=guards, tableau_formula="SUM([Sales])") is False
    assert SC._gate_ok("SUM('Orders'[Sales])",  True, guards=guards, tableau_formula="SUM([Sales])") is True


def test_second_compile_guards_tmdl_seeds_reference_gate(tmp_path):
    # A dir holding a prior build's TMDL -> the reference-gate half is active (surface set, tables None).
    # It blocks a candidate naming a table that isn't in the model and passes one that is.
    import second_compiler as SC
    tdir = tmp_path / "tmdl" / "M.SemanticModel" / "definition" / "tables"
    tdir.mkdir(parents=True)
    with open(tdir / "Orders.tmdl", "w", encoding="utf-8") as fh:
        fh.write("table Orders\n\tcolumn Sales\n\t\tdataType: double\n\t\tsourceColumn: Sales\n")
    guards = me._second_compile_guards(_sc_wb(_SC_STUB_CHAIN), str(tmp_path / "tmdl"))
    assert guards is not None
    assert guards["surface"] is not None                 # TMDL parsed -> reference gate active
    assert guards["tables"] is None                      # no CSV -> oracle inactive
    # reference gate FIRES: a nonexistent table is blocked; a real one passes.
    assert SC._gate_ok("SUM('Nope'[Sales])",   True, guards=guards) is False
    assert SC._gate_ok("SUM('Orders'[Sales])", True, guards=guards) is True


def test_second_compile_prepass_reference_gate_rejects_copy_trap(tmp_path):
    # Headline: on a --second-compile RE-RUN over an existing build, the reference gate catches the
    # (copy)_NNNN duplicate-name trap. An authored keystone that names a non-existent table is REJECTED
    # (in gate_failures, never in merged) -- yet the identical authored DAX lands when guards are off,
    # proving the syntactic gate alone cannot catch it and that the guard is the only difference.
    src = InMemoryTableauSource(workbooks={"Chain WB": _sc_wb(_SC_STUB_CHAIN)})
    out = str(tmp_path / "run")
    migrate_workbook(src, write_to=out, wb_id="Chain WB")   # a real prior build -> on-disk TMDL surface

    bad = {"Base": "SUM('Orders Copy'[Sales])"}             # 'Orders Copy' is not a model table
    merged_g, sc_g = me._second_compile_prepass(src, "Chain WB", {}, bad, output_dir=out)
    assert "Base" not in (merged_g or {})                  # the copy-trap keystone did NOT land
    assert "Base" in sc_g["gate_failures"]                 # it is recorded as a gate failure

    merged_u, _ = me._second_compile_prepass(src, "Chain WB", {}, bad, output_dir=None)
    assert merged_u["Base"] == "SUM('Orders Copy'[Sales])"  # unguarded: the same bad DAX ships (the bug the gate prevents)


def test_second_compile_prepass_empty_output_dir_is_byte_identical(tmp_path):
    # Fresh-run guarantee: a guarded pass whose output_dir holds no prior artifacts (guards -> None)
    # produces the SAME merged map as the fully unguarded pass -- a good-ref keystone cascades identically.
    src = InMemoryTableauSource(workbooks={"Chain WB": _sc_wb(_SC_STUB_CHAIN)})
    good = {"Base": "SUM('Orders'[Sales])"}
    empty = tmp_path / "fresh"
    empty.mkdir()
    merged_empty, sc_empty = me._second_compile_prepass(src, "Chain WB", {}, good, output_dir=str(empty))
    merged_none, sc_none = me._second_compile_prepass(src, "Chain WB", {}, good, output_dir=None)
    assert merged_empty == merged_none                     # byte-identical merged map
    assert merged_empty["Base"] == "SUM('Orders'[Sales])"
    assert set(sc_empty["landed"]) == {"Base", "Plus", "Ratio"} == set(sc_none["landed"])
    assert sc_empty["count"] == sc_none["count"] == 3       # full cascade both ways


def test_main_author_flag_lands_chain_via_cli(tmp_path):
    # End-to-end through the documented CLI: --author <file.json> implies --second-compile and lands
    # the authored keystone + its cascade into the workbook's model.
    indir = tmp_path / "in"
    indir.mkdir()
    with open(indir / "Chain WB.twb", "w", encoding="utf-8-sig") as fh:
        fh.write(_sc_wb(_SC_STUB_CHAIN))
    author_json = tmp_path / "author.json"
    author_json.write_text(json.dumps({"Base": "SUM('Orders'[Sales])"}), encoding="utf-8")
    out = str(tmp_path / "out")

    rc = me.main(["-i", str(indir), "-o", out, "--author", str(author_json)])
    assert rc == 0
    on_disk = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
    wb = next(w for w in on_disk["workbooks"] if w["name"] == "Chain WB")
    assert set(wb["second_compile"]["landed"]) == {"Base", "Plus", "Ratio"}
    assert "measure Base = SUM('Orders'[Sales])" in _measures_tmdl_text(out)


def test_main_second_compile_flag_lands_detector_keystone_via_cli(tmp_path):
    # --second-compile ALONE (no --author) still lands a keystone the engine's OWN Spec-7 detectors
    # recognize (year-gated idiom), proving the flag is wired independently of authored overrides.
    indir = tmp_path / "in"
    indir.mkdir()
    with open(indir / "Chain WB.twb", "w", encoding="utf-8-sig") as fh:
        fh.write(_sc_wb(_SC_DETECTOR_CHAIN))
    out = str(tmp_path / "out")

    rc = me.main(["-i", str(indir), "-o", out, "--second-compile"])
    assert rc == 0
    on_disk = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
    wb = next(w for w in on_disk["workbooks"] if w["name"] == "Chain WB")
    sc = wb["second_compile"]
    assert sc["detectors"] == ["Base"] and sc["authored"] == []
    assert set(sc["landed"]) == {"Base", "Plus"}
    tmdl = _measures_tmdl_text(out)
    assert "CALCULATE(" in tmdl and "YEAR(" in tmdl


def test_main_author_missing_file_errors(tmp_path):
    # A bad --author path fails fast (argparse error -> SystemExit) so a typo never silently drops a
    # keystone.
    indir = tmp_path / "in"
    indir.mkdir()
    with open(indir / "Chain WB.twb", "w", encoding="utf-8-sig") as fh:
        fh.write(_sc_wb(_SC_STUB_CHAIN))
    out = str(tmp_path / "out")
    with pytest.raises(SystemExit):
        me.main(["-i", str(indir), "-o", out, "--author", str(tmp_path / "missing.json")])


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


def test_rebuild_from_published_match_threads_flatfile_path(monkeypatch):
    # A .twbx connected to a published EXTRACT backed by a flat file bundles no data itself -- the
    # Excel lives in the sibling .tdsx the estate already migrated. That datasource's catalog entry
    # carries the ABSOLUTE path of the extracted Excel; the workbook rebuild MUST reuse it, or the
    # workbook .pbip emits a relative File.Contents path that Power BI Desktop opens with NO data.
    # This is the regression lock for "a flat-file workbook must open AND load."
    captured = {}

    def _fake_migrate(text, **kw):
        captured.update(kw)
        return {"report": {"fallback": False}}

    monkeypatch.setattr(me, "migrate_datasource", _fake_migrate)
    twb = ("<workbook><datasources><datasource name='ds' caption='Superstore - Extract'>"
           "</datasource></datasources></workbook>")
    detail = {"binding_signal": {"kind": "published", "published_ds_name": "Superstore - Extract"}}
    abs_xlsx = r"C:\out\data\Superstore_-_Extract\Sample - Superstore.xlsx"
    catalog = {me._norm_ds("Superstore - Extract"):
               {"text": "<datasource/>", "name": "Superstore - Extract", "flatfile_path": abs_xlsx}}

    res = me._rebuild_from_published_match(detail, twb, "Model", catalog)
    assert res is not None
    assert captured.get("flatfile_path") == abs_xlsx     # reuses the sibling's extracted Excel


def test_rebuild_from_published_match_flatfile_path_none_when_catalog_lacks_it(monkeypatch):
    # A published live-DB match (no bundled flat file) has no flatfile_path in its catalog entry ->
    # the rebuild threads None and the connection-string path is left exactly as before (untouched).
    captured = {}

    def _fake_migrate(text, **kw):
        captured.update(kw)
        return {"report": {"fallback": False}}

    monkeypatch.setattr(me, "migrate_datasource", _fake_migrate)
    twb = "<workbook><datasources><datasource name='ds' caption='Sales DS'/></datasources></workbook>"
    detail = {"binding_signal": {"kind": "published", "published_ds_name": "Sales DS"}}
    catalog = {me._norm_ds("Sales DS"): {"text": "<datasource/>", "name": "Sales DS"}}

    res = me._rebuild_from_published_match(detail, twb, "Model", catalog)
    assert res is not None
    assert captured.get("flatfile_path") is None

# --- published-DS workbook carries BOTH the workbook's AND the datasource's own calcs ----------
# A published-DS workbook's rebuilt model must hold every calculation either side defines. The
# workbook only caches the calcs it actually placed on a shelf, so _rebuild_from_published_match
# unions the published .tds's OWN calcs (match["text"]) in too -- workbook-local wins on a clash.

_PUBCALC_WB = (
    "<workbook><datasources><datasource name='ds' caption='Sales DS'>"
    "<column caption='WB Calc' name='[Calculation_wb]' role='measure'>"
    "<calculation class='tableau' formula='SUM([Sales])' /></column>"
    "</datasource></datasources></workbook>"
)


def _capture_migrate(monkeypatch):
    captured = {}

    def _fake_migrate(text, **kw):
        captured.update(kw)
        return {"report": {"fallback": False}}

    monkeypatch.setattr(me, "migrate_datasource", _fake_migrate)
    return captured


def test_rebuild_from_published_match_unions_published_only_calc(monkeypatch):
    # A measure calc that lives in the published .tds but was NEVER placed on a workbook shelf (so
    # the workbook never cached it) must still land on the rebuilt model via the union.
    captured = _capture_migrate(monkeypatch)
    tds = ("<datasource caption='Sales DS'>"
           "<column caption='WB Calc' name='[Calculation_wb]' role='measure'>"
           "<calculation class='tableau' formula='SUM([Sales])' /></column>"
           "<column caption='DS Only Margin' name='[Calculation_ds]' role='measure'>"
           "<calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' /></column>"
           "</datasource>")
    detail = {"binding_signal": {"kind": "published", "published_ds_name": "Sales DS"}}
    catalog = {me._norm_ds("Sales DS"): {"text": tds, "name": "Sales DS"}}
    res = me._rebuild_from_published_match(detail, _PUBCALC_WB, "Model", catalog)
    assert res is not None
    names = [c.get("name") for c in (captured.get("calcs") or [])]
    assert "DS Only Margin" in names      # the published-only calc came across
    assert "WB Calc" in names             # the workbook's own calc is still there


def test_rebuild_from_published_match_dedups_cached_calc_workbook_wins(monkeypatch):
    # When the SAME caption exists on both sides (the workbook cached the published calc), it must
    # appear exactly once and the WORKBOOK's formula wins (it is this workbook's authored intent).
    captured = _capture_migrate(monkeypatch)
    tds = ("<datasource caption='Sales DS'>"
           "<column caption='WB Calc' name='[Calculation_wb]' role='measure'>"
           "<calculation class='tableau' formula='SUM([DS_VERSION])' /></column>"
           "</datasource>")
    detail = {"binding_signal": {"kind": "published", "published_ds_name": "Sales DS"}}
    catalog = {me._norm_ds("Sales DS"): {"text": tds, "name": "Sales DS"}}
    res = me._rebuild_from_published_match(detail, _PUBCALC_WB, "Model", catalog)
    assert res is not None
    everything = (captured.get("calcs") or []) + (captured.get("dim_calcs") or [])
    shared = [c for c in everything if (c.get("name") or "").lower() == "wb calc"]
    assert len(shared) == 1                       # no duplicate
    assert shared[0].get("formula") == "SUM([Sales])"   # workbook formula wins, not SUM([DS_VERSION])


def test_rebuild_from_published_match_routes_published_dimension_calc(monkeypatch):
    # A dimension-role calc that lives only in the published .tds must land as a calculated COLUMN
    # (dim_calcs), never mis-routed into the measure list.
    captured = _capture_migrate(monkeypatch)
    tds = ("<datasource caption='Sales DS'>"
           "<column caption='DS Region Bucket' name='[Calculation_dim]' role='dimension'>"
           "<calculation class='tableau' formula='IF [Sales]&gt;100 THEN \"Hi\" ELSE \"Lo\" END' />"
           "</column></datasource>")
    detail = {"binding_signal": {"kind": "published", "published_ds_name": "Sales DS"}}
    catalog = {me._norm_ds("Sales DS"): {"text": tds, "name": "Sales DS"}}
    res = me._rebuild_from_published_match(detail, _PUBCALC_WB, "Model", catalog)
    assert res is not None
    dim_names = [c.get("name") for c in (captured.get("dim_calcs") or [])]
    measure_names = [c.get("name") for c in (captured.get("calcs") or [])]
    assert "DS Region Bucket" in dim_names
    assert "DS Region Bucket" not in measure_names


def test_rebuild_from_published_match_calc_union_fail_closed(monkeypatch):
    # If the published .tds text can't be parsed, the union degrades to the workbook's own calcs --
    # never raising, never dropping the workbook calcs.
    captured = _capture_migrate(monkeypatch)
    detail = {"binding_signal": {"kind": "published", "published_ds_name": "Sales DS"}}
    catalog = {me._norm_ds("Sales DS"): {"text": "this is not xml <<<", "name": "Sales DS"}}
    res = me._rebuild_from_published_match(detail, _PUBCALC_WB, "Model", catalog)
    assert res is not None
    names = [c.get("name") for c in (captured.get("calcs") or [])]
    assert names == ["WB Calc"]    # exactly the workbook's own calc, unchanged
