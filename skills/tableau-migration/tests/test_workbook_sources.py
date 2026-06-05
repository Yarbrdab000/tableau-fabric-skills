"""Workbook-embedded datasource rebuild tests (offline, stdlib-only, deterministic).

Fixtures are built as real Tableau-shaped ``.twb`` / ``.twbx`` documents (with the UTF-8 BOM
Tableau writes) in a temp dir, so the path-loading, BOM-stripping, zip-reading, and packaged
CSV header auto-detection paths are all exercised without any network or credentials.
"""
import json
import os
import zipfile

import pytest

from workbook_sources import (
    enumerate_workbook_datasources,
    rebuild_workbook_models,
    build_flatfile_model,
    _manifest,
    main,
)
from connection_to_m import parse_tds


# -- fixtures (structurally faithful, trimmed) --------------------------------
EXCEL_DS = """
    <datasource name='excel.1' caption='Sample - Superstore'>
      <connection class='excel-direct' filename='Sample - Superstore.xlsx' directory='C:\\\\Data'>
        <relation type='collection'>
          <relation connection='excel-direct.0' name='Orders' table='[Orders$]' type='table'/>
          <relation connection='excel-direct.0' name='People' table='[People$]' type='table'/>
        </relation>
        <metadata-records>
          <metadata-record class='column'><remote-name>Row ID</remote-name>
            <local-name>[Row ID]</local-name><parent-name>[Orders$]</parent-name><local-type>integer</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Sales</remote-name>
            <local-name>[Sales]</local-name><parent-name>[Orders$]</parent-name><local-type>real</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Order Date</remote-name>
            <local-name>[Order Date]</local-name><parent-name>[Orders$]</parent-name><local-type>date</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Person</remote-name>
            <local-name>[Person]</local-name><parent-name>[People$]</parent-name><local-type>string</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""

CSV_DS = """
    <datasource name='csv.1' caption='Sales Commission'>
      <connection class='textscan' filename='Sales Commission.csv' directory='C:\\\\Data'
                  separator=',' charset='utf-8'>
        <relation name='Sales Commission' table='[Sales Commission#csv]' type='table'/>
        <metadata-records>
          <metadata-record class='column'><remote-name>Rep</remote-name>
            <local-name>[Rep]</local-name><parent-name>[Sales Commission#csv]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Commission Rate</remote-name>
            <local-name>[Commission Rate]</local-name><parent-name>[Sales Commission#csv]</parent-name><local-type>real</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""

SQLPROXY_DS = """
    <datasource name='federated.pub' caption='Corp Sales (Published)'>
      <connection class='sqlproxy' server='tableau.example.com' dbname='analytics-site'
                  server-ds-friendly-name='Corp Sales' port='443' channel='https'/>
    </datasource>"""

SQLSERVER_DS = """
    <datasource name='federated.sql' caption='Live Orders'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='srv' name='sqlserver.0'>
            <connection class='sqlserver' dbname='Sales' server='srv.example.com' username='svc'/>
          </named-connection>
        </named-connections>
        <relation connection='sqlserver.0' name='Orders' table='[dbo].[Orders]' type='table'/>
        <metadata-records>
          <metadata-record class='column'><remote-name>Order ID</remote-name>
            <local-name>[Order ID]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Quantity</remote-name>
            <local-name>[Quantity]</local-name><parent-name>[Orders]</parent-name><local-type>integer</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""

PARAMETERS_DS = """
    <datasource name='Parameters' hasconnection='false' caption='Parameters'>
      <column caption='Top N' name='[Parameters].[Param 1]' datatype='integer' role='measure'/>
    </datasource>"""

# A published .tds the resolution seam hands back (a normal standalone SQL Server datasource).
RESOLVED_TDS = """<?xml version='1.0' encoding='utf-8'?>
<datasource formatted-name='Corp Sales' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.r'>
        <connection class='sqlserver' dbname='Corp' server='corp.database.windows.net'/>
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.r' name='Sales' table='[dbo].[Sales]' type='table'/>
    <metadata-records>
      <metadata-record class='column'><remote-name>Amount</remote-name>
        <local-name>[Amount]</local-name><parent-name>[Sales]</parent-name><local-type>real</local-type></metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


def _workbook(*datasources):
    inner = "".join(datasources)
    return (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<workbook version='18.1'>\n"
        f"  <datasources>{inner}\n  </datasources>\n"
        "</workbook>\n"
    )


def _write_bom(path, text):
    """Write ``text`` with the UTF-8 BOM Tableau emits (forces the utf-8-sig read path)."""
    with open(path, "wb") as fh:
        fh.write(b"\xef\xbb\xbf" + text.encode("utf-8"))


# -- enumeration ---------------------------------------------------------------
def test_enumeration_skips_parameters_and_classifies():
    wb = _workbook(PARAMETERS_DS, EXCEL_DS, CSV_DS, SQLPROXY_DS, SQLSERVER_DS)
    entries = enumerate_workbook_datasources(wb)

    captions = {e["caption"] for e in entries}
    assert "Parameters" not in captions          # pseudo-datasource skipped
    assert len(entries) == 4

    by_caption = {e["caption"]: e for e in entries}
    assert by_caption["Sample - Superstore"]["classification"] == "flat_file"
    assert by_caption["Sample - Superstore"]["file_type"] == "Excel"
    assert by_caption["Sales Commission"]["classification"] == "flat_file"
    assert by_caption["Sales Commission"]["file_type"] == "CSV"
    assert by_caption["Corp Sales (Published)"]["classification"] == "published_reference"
    assert by_caption["Live Orders"]["classification"] == "relational"


def test_enumeration_captures_typed_columns_from_metadata():
    entries = enumerate_workbook_datasources(_workbook(EXCEL_DS))
    rels = {r["name"]: r for r in entries[0]["relations"]}
    # multi-sheet Excel collection -> independent tables, each typed from metadata
    assert set(rels) == {"Orders", "People"}
    orders_types = {c["model_name"]: c["tmdl_type"] for c in rels["Orders"]["columns"]}
    assert orders_types["Row_ID"] == "int64"
    assert orders_types["Sales"] == "double"
    assert orders_types["Order_Date"] == "dateTime"


def test_enumeration_accepts_file_path_with_bom(tmp_path):
    twb = tmp_path / "wb.twb"
    _write_bom(str(twb), _workbook(EXCEL_DS, CSV_DS))
    entries = enumerate_workbook_datasources(str(twb))
    assert {e["caption"] for e in entries} == {"Sample - Superstore", "Sales Commission"}


# -- flat-file Excel -----------------------------------------------------------
def test_excel_flatfile_m_shape_and_filepath_parameter():
    res = rebuild_workbook_models(_workbook(EXCEL_DS))
    model = res["models"]["Sample - Superstore"]
    assert model["status"] == "migrated_with_followups"

    orders = model["parts"]["definition/tables/Orders.tmdl"]
    assert 'Excel.Workbook(File.Contents(#"FilePath"), null, true)' in orders
    assert 'Source{[Item="Orders", Kind="Sheet"]}[Data]' in orders     # $ stripped, sheet nav
    assert "Table.PromoteHeaders(Navigation, [PromoteAllScalars=true])" in orders
    # raw header renamed to the clean model column name the report binds to
    assert '{"Row ID", "Row_ID"}' in orders
    # typed from metadata, not deferred to Power BI inference
    assert "dataType: int64" in orders
    assert '{"Row_ID", Int64.Type}' in orders

    exprs = model["parts"]["definition/expressions.tmdl"]
    assert "expression FilePath =" in exprs
    assert "IsParameterQuery=true" in exprs
    assert "Sample - Superstore.xlsx" in exprs   # default = original directory/filename

    model_tmdl = model["parts"]["definition/model.tmdl"]
    assert "ref table Orders" in model_tmdl
    assert "ref table People" in model_tmdl
    assert "ref table _Measures" in model_tmdl
    assert 'PBI_QueryOrder = ["FilePath"]' in model_tmdl


def test_excel_flatfile_has_repoint_followup():
    res = rebuild_workbook_models(_workbook(EXCEL_DS))
    msgs = [f["message"] for f in res["followups"] if f["datasource"] == "Sample - Superstore"]
    assert any("Repoint the FilePath parameter" in m for m in msgs)


# -- flat-file CSV -------------------------------------------------------------
def test_csv_flatfile_m_shape():
    res = rebuild_workbook_models(_workbook(CSV_DS))
    tmdl = res["models"]["Sales Commission"]["parts"]["definition/tables/Sales Commission.tmdl"]
    assert 'Csv.Document(File.Contents(#"FilePath")' in tmdl
    assert 'Delimiter=","' in tmdl
    assert "Columns=2" in tmdl
    assert "Encoding=65001" in tmdl           # utf-8 -> code page
    assert "Table.PromoteHeaders(Source, [PromoteAllScalars=true])" in tmdl
    assert '{"Commission Rate", "Commission_Rate"}' in tmdl
    assert '{"Commission_Rate", type number}' in tmdl


# -- published reference + resolution seam ------------------------------------
def test_sqlproxy_marked_published_unresolved_without_resolver():
    res = rebuild_workbook_models(_workbook(SQLPROXY_DS))
    model = res["models"]["Corp Sales (Published)"]
    assert model["status"] == "published_unresolved"
    assert model["parts"] == {}
    assert model["report"]["referenced_name"] == "Corp Sales"
    assert any("Corp Sales" in f["message"] for f in res["followups"])


def test_sqlproxy_resolution_seam_runs_the_spine():
    seen = {}

    def resolver(name):
        seen["name"] = name
        return RESOLVED_TDS

    res = rebuild_workbook_models(_workbook(SQLPROXY_DS), resolve_published=resolver)
    model = res["models"]["Corp Sales (Published)"]
    assert seen["name"] == "Corp Sales"                 # seam called with the referenced name
    assert model["status"] in ("migrated", "migrated_with_followups")
    sales = model["parts"]["definition/tables/Sales.tmdl"]
    assert 'Sql.Database(#"Server", #"Database")' in sales
    assert model["report"]["resolved_published"] == "Corp Sales"


# -- relational embedded datasource -> spine ----------------------------------
def test_relational_federated_rebuilds_via_spine():
    res = rebuild_workbook_models(_workbook(SQLSERVER_DS))
    model = res["models"]["Live Orders"]
    assert model["status"] == "migrated"               # sqlserver is fully supported
    orders = model["parts"]["definition/tables/Orders.tmdl"]
    assert 'Sql.Database(#"Server", #"Database")' in orders
    assert "mode: directQuery" in orders


# -- per-source error isolation -----------------------------------------------
def test_one_failing_source_does_not_abort_the_rest():
    def exploding_resolver(name):
        raise RuntimeError("tableau server unreachable")

    res = rebuild_workbook_models(
        _workbook(SQLPROXY_DS, EXCEL_DS, SQLSERVER_DS),
        resolve_published=exploding_resolver)

    assert res["models"]["Corp Sales (Published)"]["status"] == "error"
    assert "tableau server unreachable" in res["models"]["Corp Sales (Published)"]["error"]
    # the other two datasources still migrate cleanly
    assert res["models"]["Sample - Superstore"]["status"] == "migrated_with_followups"
    assert res["models"]["Live Orders"]["status"] == "migrated"


# -- structural / honesty fallbacks -------------------------------------------
def test_spatial_source_falls_back_not_guessed():
    spatial = """
    <datasource name='spatial.1' caption='Territories'>
      <connection class='ogrdirect' filename='zones.shp'>
        <relation name='zones' table='[zones]' type='table'/>
      </connection>
    </datasource>"""
    res = rebuild_workbook_models(_workbook(spatial))
    model = res["models"]["Territories"]
    assert model["status"] == "fallback"
    assert model["parts"] == {}


def test_flatfile_clean_name_collision_falls_back():
    # 'Order ID' and 'Order_ID' both clean to 'Order_ID' -> ambiguous, refuse to guess.
    collide = """
    <datasource name='excel.c' caption='Collide'>
      <connection class='excel-direct' filename='c.xlsx'>
        <relation name='T' table='[T$]' type='table'/>
        <metadata-records>
          <metadata-record class='column'><remote-name>Order ID</remote-name>
            <local-name>[Order ID]</local-name><parent-name>[T$]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Order_ID</remote-name>
            <local-name>[Order_ID]</local-name><parent-name>[T$]</parent-name><local-type>string</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""
    res = rebuild_workbook_models(_workbook(collide))
    assert res["models"]["Collide"]["status"] == "fallback"


# -- packaged .twbx: zip read + CSV delimiter auto-detection -------------------
def _make_twbx(path, workbook_xml, data_files):
    """Write a real .twbx zip: inner .twb (BOM) + packaged data files under Data/."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("workbook.twb", b"\xef\xbb\xbf" + workbook_xml.encode("utf-8"))
        for name, content in data_files.items():
            zf.writestr(name, content)


def test_twbx_reads_inner_workbook_and_autodetects_csv_delimiter(tmp_path):
    # textscan with NO separator attribute -> delimiter must be sniffed from the packaged header.
    csv_ds = """
    <datasource name='csv.semi' caption='Regional'>
      <connection class='textscan' filename='regional.csv' directory='Data' charset='utf-8'>
        <relation name='Regional' table='[regional#csv]' type='table'/>
        <metadata-records>
          <metadata-record class='column'><remote-name>Region</remote-name>
            <local-name>[Region]</local-name><parent-name>[regional#csv]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>Total</remote-name>
            <local-name>[Total]</local-name><parent-name>[regional#csv]</parent-name><local-type>real</local-type></metadata-record>
        </metadata-records>
      </connection>
    </datasource>"""
    twbx = tmp_path / "estate.twbx"
    _make_twbx(str(twbx), _workbook(csv_ds),
               {"Data/regional.csv": b"Region;Total;Share\nWest;100;0.5\n",
                "Data/ignored.xlsx": b"PK\x03\x04 not a real xlsx, only here to exist"})

    entries = enumerate_workbook_datasources(str(twbx))
    assert entries[0]["caption"] == "Regional"
    assert entries[0]["package_path"] == str(twbx)

    res = rebuild_workbook_models(str(twbx))
    tmdl = res["models"]["Regional"]["parts"]["definition/tables/Regional.tmdl"]
    assert 'Delimiter=";"' in tmdl          # auto-detected from the packaged header line
    assert "Columns=3" in tmdl              # header had 3 fields


# -- CLI manifest is summary-only (no GUIDs / secrets) ------------------------
def test_manifest_has_no_guids_or_raw_parts():
    res = rebuild_workbook_models(_workbook(EXCEL_DS, SQLSERVER_DS))
    manifest = _manifest(res)
    blob = json.dumps(manifest)
    assert "lineageTag" not in blob          # no TMDL part text / GUIDs leak into the manifest
    assert "Sql.Database" not in blob
    assert manifest["model_count"] == 2
    statuses = {m["folder"]: m["status"] for m in manifest["models"]}
    assert statuses["Sample - Superstore.SemanticModel"] == "migrated_with_followups"


def test_cli_writes_folders(tmp_path, capsys):
    twb = tmp_path / "wb.twb"
    _write_bom(str(twb), _workbook(EXCEL_DS, SQLSERVER_DS))
    out = tmp_path / "bundle"
    rc = main([str(twb), "-o", str(out)])
    assert rc == 0

    # one <caption>.SemanticModel folder per migrated datasource, with the core parts
    assert (out / "Sample - Superstore.SemanticModel" / "definition" / "model.tmdl").exists()
    assert (out / "Live Orders.SemanticModel" / "definition" / "tables" / "Orders.tmdl").exists()

    printed = json.loads(capsys.readouterr().out)
    assert printed["model_count"] == 2


# -- folder de-duplication for repeated captions ------------------------------
def test_duplicate_captions_get_distinct_folders():
    res = rebuild_workbook_models(_workbook(SQLSERVER_DS, SQLSERVER_DS.replace("federated.sql", "federated.sql2")))
    folders = [rec["folder"] for rec in res["models"].values()]
    assert len(folders) == len(set(folders))   # no collision / silent overwrite
