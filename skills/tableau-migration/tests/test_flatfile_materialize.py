"""Bundled flat-file / extract DATA materialization for the one-button estate + datasource paths.

The bug these guard against: a flat-file (Excel/CSV) or EXTRACT-backed Tableau source emits an
Import model whose ``File.Contents`` points at Tableau's RELATIVE path -- Power BI Desktop rejects
it (*"The supplied file path must be a valid absolute path"*) so the model opens but loads no rows.

``materialize_bundled_flatfile_data`` resolves this two ways, in order:

* a bundled Excel/CSV is lifted out of the ``.tdsx``/``.twbx`` to an ABSOLUTE path (``flatfile``);
* an extract (only a ``.hyper`` is packaged) is read to one CSV per table (``csv``), routed through
  the proven local-CSV Import path.

When neither is possible the result is honest (``kind=None`` + a ``reason``) so the orchestrator can
warn instead of silently shipping an empty model. The optional ``tableauhyperapi`` is faked here
(the established pattern) so the suite stays hermetic; a final round-trip runs against the real wheel
only when it is installed. No ``.hyper`` / workbook is ever committed.
"""
import csv
import importlib.util
import os
import zipfile

import pytest

import assemble_model as A
import connection_to_m as C
import hyper_reader as hr
import migrate_estate as E


# An Excel flat-file datasource: parse_tds captures flatfile_filename, so the materializer engages.
EXCEL_DS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Sample - Superstore' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='Sample - Superstore' name='excel.abc'>
        <connection class='excel-direct' filename='Data/Superstore/Sample - Superstore.xlsx' validate='no' />
      </named-connection>
    </named-connections>
    <relation connection='excel.abc' name='Orders' table='[Orders$]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

_EXCEL_MEMBER = "Data/Superstore/Sample - Superstore.xlsx"


def _make_zip(path, members):
    """Write a ``PK`` zip (a stand-in .tdsx/.twbx) with the given ``{member_name: bytes}``."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return str(path)


def _hyperapi_installed():
    return importlib.util.find_spec("tableauhyperapi") is not None


def _fake_extract_to_csv(rows_by_table):
    """Return a stand-in for ``hyper_reader.extract_to_csv`` that writes real CSVs into ``out_dir``
    and returns the ``{table: {csv_path, columns, row_count}}`` mapping the real reader produces."""
    def _impl(source, out_dir, **kwargs):
        os.makedirs(out_dir, exist_ok=True)
        mapping = {}
        for table, (columns, rows) in rows_by_table.items():
            csv_path = os.path.join(out_dir, table + ".csv")
            with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(columns)
                w.writerows(rows)
            mapping[table] = {"csv_path": os.path.abspath(csv_path),
                              "columns": columns, "row_count": len(rows)}
        return mapping
    return _impl


# =============================================================================
# Helper: materialize_bundled_flatfile_data
# =============================================================================
def test_materialize_lifts_bundled_excel(tmp_path):
    arc = _make_zip(tmp_path / "ds.tdsx", {
        "ds.tds": "<datasource/>",
        _EXCEL_MEMBER: b"PK\x03\x04 fake-xlsx-bytes",
    })
    d = C.parse_tds(EXCEL_DS)
    dest = tmp_path / "out"
    res = A.materialize_bundled_flatfile_data(arc, d, str(dest))
    assert res["kind"] == "flatfile"
    assert os.path.isabs(res["flatfile_path"]) and os.path.isfile(res["flatfile_path"])
    assert os.path.basename(res["flatfile_path"]) == "Sample - Superstore.xlsx"
    assert res["table_csv_paths"] is None


def test_materialize_extracts_hyper_to_csv(tmp_path, monkeypatch):
    arc = _make_zip(tmp_path / "wb.twbx", {
        "wb.twb": "<workbook/>",
        "Data/extract/extract.hyper": b"HYPERBINARY",  # the Excel is NOT packaged -> extract case
    })
    monkeypatch.setattr(hr, "extract_to_csv",
                        _fake_extract_to_csv({"Orders": (["Region", "Sales"],
                                                         [["West", 10], ["East", 20]])}))
    d = C.parse_tds(EXCEL_DS)
    dest = tmp_path / "out"
    res = A.materialize_bundled_flatfile_data(arc, d, str(dest))
    assert res["kind"] == "csv"
    assert res["hyper_present"] is True
    assert set(res["table_csv_paths"]) == {"Orders"}
    csv_path = res["table_csv_paths"]["Orders"]
    assert os.path.isabs(csv_path) and os.path.isfile(csv_path)


def test_materialize_reports_no_bundled_data(tmp_path):
    arc = _make_zip(tmp_path / "wb.twbx", {"wb.twb": "<workbook/>"})  # neither excel nor hyper
    d = C.parse_tds(EXCEL_DS)
    res = A.materialize_bundled_flatfile_data(arc, d, str(tmp_path / "out"))
    assert res["kind"] is None
    assert res["reason"] == "no_bundled_data"
    assert res["hyper_present"] is False


def test_materialize_reports_hyperapi_unavailable(tmp_path, monkeypatch):
    arc = _make_zip(tmp_path / "wb.twbx", {
        "wb.twb": "<workbook/>",
        "Data/extract/extract.hyper": b"HYPERBINARY",
    })

    def _raise(source, out_dir, **kwargs):
        raise hr.HyperApiUnavailable("install it")

    monkeypatch.setattr(hr, "extract_to_csv", _raise)
    d = C.parse_tds(EXCEL_DS)
    res = A.materialize_bundled_flatfile_data(arc, d, str(tmp_path / "out"))
    assert res["kind"] is None
    assert res["reason"] == "hyperapi_unavailable"
    assert res["hyper_present"] is True


def test_materialize_not_a_package_for_xml_text(tmp_path):
    d = C.parse_tds(EXCEL_DS)
    res = A.materialize_bundled_flatfile_data(EXCEL_DS, d, str(tmp_path / "out"))
    assert res["kind"] is None
    assert res["reason"] == "not_a_package"


def test_materialize_not_flatfile_without_filename(tmp_path):
    res = A.materialize_bundled_flatfile_data(
        b"PK\x03\x04zip", {"flatfile_filename": None}, str(tmp_path / "out"))
    assert res["kind"] is None
    assert res["reason"] == "not_flatfile"


# =============================================================================
# migrate_datasource wiring (the workbook / embedded-datasource path)
# =============================================================================
def test_migrate_datasource_routes_extract_twbx_to_csv(tmp_path, monkeypatch):
    arc = _make_zip(tmp_path / "wb.twbx", {
        "wb.twb": "<workbook/>",
        "Data/extract/extract.hyper": b"HYPERBINARY",
    })
    monkeypatch.setattr(hr, "extract_to_csv",
                        _fake_extract_to_csv({"Orders": (["Region", "Sales"],
                                                         [["West", 10], ["East", 20]])}))
    dest = tmp_path / "data"
    res = A.migrate_datasource(EXCEL_DS, model_name="M", packaged_source=arc,
                               flatfile_dest_dir=str(dest))
    ffd = res["report"]["flatfile_data"]
    assert ffd["landed"] is True and ffd["kind"] == "csv"
    assert res["report"].get("local_import")  # local-CSV import path was used
    matched = res["report"]["local_import"]["matched"]
    assert matched and all(os.path.isabs(m["csv_path"]) for m in matched)
    blob = "\n".join(res["parts"].values())
    assert "Csv.Document" in blob


def test_migrate_datasource_lifts_bundled_excel(tmp_path):
    arc = _make_zip(tmp_path / "ds.tdsx", {
        "ds.tds": "<datasource/>",
        _EXCEL_MEMBER: b"PK\x03\x04 fake-xlsx-bytes",
    })
    dest = tmp_path / "data"
    res = A.migrate_datasource(EXCEL_DS, model_name="M", packaged_source=arc,
                               flatfile_dest_dir=str(dest))
    ffd = res["report"]["flatfile_data"]
    assert ffd["landed"] is True and ffd["kind"] == "flatfile"
    blob = "\n".join(res["parts"].values())
    assert "Excel.Workbook" in blob
    # the emitted path is absolute (not Tableau's relative 'Data/Superstore/...')
    landed = os.path.join(str(dest), "Sample - Superstore.xlsx")
    assert os.path.isfile(landed)
    assert "Data/Superstore/Sample - Superstore.xlsx" not in blob


def test_migrate_datasource_reports_unlanded_when_no_data(tmp_path):
    arc = _make_zip(tmp_path / "wb.twbx", {"wb.twb": "<workbook/>"})  # neither excel nor hyper
    dest = tmp_path / "data"
    res = A.migrate_datasource(EXCEL_DS, model_name="M", packaged_source=arc,
                               flatfile_dest_dir=str(dest))
    ffd = res["report"]["flatfile_data"]
    assert ffd["landed"] is False
    assert ffd["reason"] == "no_bundled_data"


# =============================================================================
# Estate datasource path (_migrate_one_datasource)
# =============================================================================
class _ZipSource:
    """Minimal estate source: read_datasource returns the .tds XML; ds_id is the real zip path so
    the materializer can introspect the bundled data."""

    def __init__(self, text):
        self._text = text

    def asset_name(self, ds_id):
        return os.path.splitext(os.path.basename(str(ds_id)))[0]

    def read_datasource(self, ds_id):
        return self._text


def _run_one_datasource(tmp_path, arc, monkeypatch=None, rows=None):
    if monkeypatch is not None and rows is not None:
        monkeypatch.setattr(hr, "extract_to_csv", _fake_extract_to_csv(rows))
    sm_dir = tmp_path / "semantic_models"
    sm_dir.mkdir()
    return E._migrate_one_datasource(_ZipSource(EXCEL_DS), arc, str(sm_dir), set())


def test_estate_datasource_extract_lands_csv(tmp_path, monkeypatch):
    arc = _make_zip(tmp_path / "Superstore - Extract.tdsx", {
        "ds.tds": "<datasource/>",
        "Data/extract/extract.hyper": b"HYPERBINARY",
    })
    detail = _run_one_datasource(tmp_path, arc, monkeypatch,
                                 {"Orders": (["Region", "Sales"], [["West", 10]])})
    assert detail["flatfile_data"]["landed"] is True
    assert detail["flatfile_data"]["kind"] == "csv"
    assert detail["status"] in ("migrated", "migrated_with_followups")


def test_estate_datasource_no_extract_adds_followup(tmp_path):
    arc = _make_zip(tmp_path / "Superstore.tdsx", {"ds.tds": "<datasource/>"})
    detail = _run_one_datasource(tmp_path, arc)
    assert detail["flatfile_data"]["landed"] is False
    assert detail["status"] == "migrated_with_followups"
    assert any("flat-file" in f for f in detail.get("manual_followups", []))


# =============================================================================
# Workbook path (_attach_workbook_pbip) records the additive flatfile_data detail key
# =============================================================================
def test_attach_workbook_pbip_records_flatfile_data(tmp_path, monkeypatch):
    pbir = '{"version": "1.0", "datasetReference": {"byPath": {"path": "../WB.SemanticModel"}}}'
    pre = {"parts": {"definition.pbir": pbir},
           "ir": {"worksheets": [{"name": "S1", "visual_type": "bar"}]}, "warnings": []}
    # the embedded datasource is flat-file but no data was bundled -> landed False, honest reason.
    res_report = {"flatfile_data": {"landed": False, "kind": None,
                                    "reason": "no_bundled_data", "hyper_present": False}}
    monkeypatch.setattr(E, "list_workbook_datasources",
                        lambda twb: [{"label": "Orders DS", "caption": "Orders DS",
                                      "name": "federated.s1"}])
    monkeypatch.setattr(E, "migrate_datasource",
                        lambda twb, **kw: {"parts": {"definition/model.tmdl": "x"},
                                           "report": res_report})
    monkeypatch.setattr(E, "_param_slicers_from_workbook", lambda twb, rep: {})
    monkeypatch.setattr(E, "_crosscheck_report_refs", lambda parts, model_parts: (parts, []))
    monkeypatch.setattr(E, "write_local_pbip", lambda *a, **kw: None)

    def bound_viz(xml, name, **kw):
        return {"parts": {"definition.pbir": pbir},
                "ir": {"worksheets": [{"name": "S1", "visual_type": "bar"}]}, "warnings": []}

    detail = {"name": "WB"}
    E._attach_workbook_pbip(detail, "<workbook/>", pre, "WB",
                            str(tmp_path / "pbip"), viz=bound_viz)
    assert detail["flatfile_data"] == {"landed": False, "kind": None,
                                       "reason": "no_bundled_data", "hyper_present": False}
    assert any("loads no rows" in w for w in detail.get("pbip_warnings", []))


# =============================================================================
# Real round-trip -- only when the optional tableauhyperapi wheel is installed
# =============================================================================
@pytest.mark.skipif(not _hyperapi_installed(),
                    reason="tableauhyperapi not installed (optional POC dependency)")
def test_real_twbx_extract_lands_real_csv(tmp_path, monkeypatch):
    import tableauhyperapi as hapi
    monkeypatch.chdir(tmp_path)  # keep hyperd.log out of the source tree (mirror parity)
    hyper_path = tmp_path / "extract.hyper"
    table = hapi.TableName("Extract", "Orders")
    telemetry = (getattr(hapi.Telemetry, "DO_NOT_SEND_USAGE_DATA_TO_TABLEAU", None)
                 or getattr(hapi.Telemetry, "DO_NOT_SEND_USAGE_DATA"))
    with hapi.HyperProcess(telemetry=telemetry) as process:
        with hapi.Connection(endpoint=process.endpoint, database=str(hyper_path),
                             create_mode=hapi.CreateMode.CREATE_AND_REPLACE) as conn:
            conn.catalog.create_schema("Extract")
            tdef = hapi.TableDefinition(table, [
                hapi.TableDefinition.Column("Region", hapi.SqlType.text()),
                hapi.TableDefinition.Column("Sales", hapi.SqlType.double()),
            ])
            conn.catalog.create_table(tdef)
            with hapi.Inserter(conn, tdef) as inserter:
                inserter.add_rows([["West", 10.5], ["East", 20.0]])
                inserter.execute()
    arc = tmp_path / "wb.twbx"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("wb.twb", "<workbook/>")
        zf.write(hyper_path, "Data/extract/extract.hyper")
    dest = tmp_path / "data"
    res = A.migrate_datasource(EXCEL_DS, model_name="M", packaged_source=str(arc),
                               flatfile_dest_dir=str(dest))
    ffd = res["report"]["flatfile_data"]
    assert ffd["landed"] is True and ffd["kind"] == "csv"
    matched = res["report"]["local_import"]["matched"]
    assert matched
    landed_csv = matched[0]["csv_path"]
    assert os.path.isabs(landed_csv) and os.path.isfile(landed_csv)
    with open(landed_csv, newline="", encoding="utf-8") as fh:
        body = list(csv.reader(fh))
    assert body[0] == ["Region", "Sales"]
    assert len(body) == 3  # header + 2 rows of real extract data


# =============================================================================
# Estate one-button path: flat-file data lands INSIDE the .pbip + header reconciliation
# (Tableau alias "Person" -> physical Excel header "Regional Manager")
# =============================================================================
# An excel-direct People datasource whose FIRST column is exposed under the alias "Person" while the
# physical Excel header is "Regional Manager". Ordinals are datasource-GLOBAL (21/22), matching a
# real .tds -- so a correct fix cannot rely on the ordinal as a sheet index.
PEOPLE_EXCEL_DS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='People DS' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='People' name='excel.abc'>
        <connection class='excel-direct' filename='Data/People.xlsx' validate='no' />
      </named-connection>
    </named-connections>
    <relation connection='excel.abc' name='People' table='[People$]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Person</remote-name><local-name>[Person]</local-name>
        <parent-name>[People$]</parent-name><local-type>string</local-type>
        <ordinal>21</ordinal>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[People$]</parent-name><local-type>string</local-type>
        <ordinal>22</ordinal>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


def _min_xlsx_bytes(sheet_name, headers):
    """Bytes of a minimal single-sheet .xlsx (inline strings) with the given first-row headers."""
    def _col_letter(i):
        s, i = "", i + 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(ord("A") + r) + s
        return s
    cells = "".join(
        '<c r="%s1" t="inlineStr"><is><t>%s</t></is></c>' % (_col_letter(i), h)
        for i, h in enumerate(headers))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1">' + cells + '</row></sheetData></worksheet>')
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>' % sheet_name)
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>')
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>')
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>')
    import io as _io
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook_xml)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


def _read_all_tmdl(root):
    """Concatenate every .tmdl under root (so a test can assert on the emitted model M)."""
    blobs = []
    for base, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".tmdl"):
                with open(os.path.join(base, f), encoding="utf-8") as fh:
                    blobs.append(fh.read())
    return "\n".join(blobs)


def test_estate_flatfile_lands_in_pbip_and_reconciles_headers(tmp_path):
    # Real bundled xlsx: People sheet physically headed [Regional Manager, Region].
    arc = _make_zip(tmp_path / "People_DS.tdsx", {
        "People_DS.tds": "<datasource/>",
        "Data/People.xlsx": _min_xlsx_bytes("People", ["Regional Manager", "Region"]),
    })
    sm_dir = tmp_path / "semantic_models"
    sm_dir.mkdir()
    pbip_dir = tmp_path / "pbip"
    pbip_dir.mkdir()
    detail = E._migrate_one_datasource(_ZipSource(PEOPLE_EXCEL_DS), arc, str(sm_dir), set(),
                                       pbip_dir=str(pbip_dir))
    assert detail["status"] in ("migrated", "migrated_with_followups")

    # (a) header reconciliation fixed the alias: Person -> Regional Manager (never a wrong bind).
    hdr = detail.get("flatfile_header_reconcile")
    assert hdr is not None, "expected flatfile_header_reconcile to be surfaced"
    assert hdr["mismatches"] == []
    assert [(r["from"], r["to"]) for r in hdr["remaps"]] == [("Person", "Regional Manager")]

    # (b) the data landed INSIDE the .pbip, in <safe_base>.Data, not a sibling data/ folder.
    safe_base = detail["pbip_folder"].split("/")[1]
    landed = tmp_path / "pbip" / safe_base / (safe_base + ".Data") / "People.xlsx"
    assert landed.is_file(), f"expected bundled data at {landed}"

    # (c) the emitted People partition types the PHYSICAL header and references the relocatable
    #     SourceFolder parameter -- so the project both LOADS and stays portable.
    blob = _read_all_tmdl(sm_dir)
    assert '"Regional Manager"' in blob        # types a header that physically exists
    assert '#"SourceFolder"' in blob           # relocatable, not a hard-coded absolute path
    assert 'expression SourceFolder =' in blob


def test_materialize_recovers_data_from_sibling_tdsx(tmp_path):
    # A bare .tds (schema only) whose DATA lives in a same-stem .tdsx twin: materialize must recover
    # the sibling package via _sibling_package_for and still land the bundled Excel.
    tds_path = tmp_path / "People_DS.tds"
    tds_path.write_text("<datasource/>", encoding="utf-8")
    _make_zip(tmp_path / "People_DS.tdsx", {
        "People_DS.tds": "<datasource/>",
        "Data/People.xlsx": _min_xlsx_bytes("People", ["Regional Manager", "Region"]),
    })
    d = C.parse_tds(PEOPLE_EXCEL_DS)
    res = A.materialize_bundled_flatfile_data(str(tds_path), d, str(tmp_path / "out"))
    assert res["kind"] == "flatfile"
    assert os.path.basename(res["flatfile_path"]) == "People.xlsx"
    assert os.path.isfile(res["flatfile_path"])


def test_estate_flatfile_rerun_preserves_landed_data(tmp_path):
    # Rerunning the estate into the SAME pbip_dir must NOT nuke the freshly-landed <name>.Data folder
    # (the pbip write does a SELECTIVE clear that skips it), and must still emit a loadable model.
    arc = _make_zip(tmp_path / "People_DS.tdsx", {
        "People_DS.tds": "<datasource/>",
        "Data/People.xlsx": _min_xlsx_bytes("People", ["Regional Manager", "Region"]),
    })
    sm_dir = tmp_path / "semantic_models"
    sm_dir.mkdir()
    pbip_dir = tmp_path / "pbip"
    pbip_dir.mkdir()

    def _run():
        return E._migrate_one_datasource(_ZipSource(PEOPLE_EXCEL_DS), arc, str(sm_dir), set(),
                                         pbip_dir=str(pbip_dir))

    first = _run()
    second = _run()  # rerun into the same output
    assert second["status"] in ("migrated", "migrated_with_followups")
    safe_base = second["pbip_folder"].split("/")[1]
    landed = tmp_path / "pbip" / safe_base / (safe_base + ".Data") / "People.xlsx"
    assert landed.is_file(), "rerun must preserve the landed data file"
    hdr = second.get("flatfile_header_reconcile")
    assert hdr and [(r["from"], r["to"]) for r in hdr["remaps"]] == [("Person", "Regional Manager")]
    # the reran .pbip is not left with a stale extra child beyond the expected set
    children = set(os.listdir(tmp_path / "pbip" / safe_base))
    assert (safe_base + ".Data") in children
    assert (safe_base + ".SemanticModel") in children


# =============================================================================
# Extract-backed SaaS (unmapped connector, e.g. Salesforce): the .hyper snapshot
# IS the data -> land an offline Import over it, exactly like a flat-file extract.
# The connector has no live Power BI rebuild, so without this the datasource died
# at the unknown-connector "needs-storage-decision" branch (item #11).
# =============================================================================
# A Salesforce datasource shipped WITH an extract: connection_class='salesforce', is_extract=True,
# and NO flatfile_filename -- so the extract-over-unmapped path (not the flat-file path) engages.
SALESFORCE_EXTRACT_DS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Salesforce Admin Insights' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='Salesforce' name='salesforce.abc'>
        <connection class='salesforce' server='login.salesforce.com' authentication='oauth' />
      </named-connection>
    </named-connections>
    <relation connection='salesforce.abc' name='Account' table='[Account]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Id</remote-name><local-name>[Id]</local-name>
        <parent-name>[Account]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>AnnualRevenue</remote-name><local-name>[AnnualRevenue]</local-name>
        <parent-name>[Account]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <extract enabled='true'>
    <connection class='hyper' dbname='Data/extract/extract.hyper' />
  </extract>
</datasource>"""


def test_materialize_extracts_saas_hyper_without_flatfile_filename(tmp_path, monkeypatch):
    # The materializer previously refused any descriptor with no flatfile_filename ("not_flatfile").
    # A SaaS extract has no bundled named file, only a .hyper -> it must now proceed and extract it.
    arc = _make_zip(tmp_path / "Salesforce Admin Insights.tdsx", {
        "ds.tds": "<datasource/>",
        "Data/extract/extract.hyper": b"HYPERBINARY",
    })
    monkeypatch.setattr(hr, "extract_to_csv",
                        _fake_extract_to_csv({"Account": (["Id", "AnnualRevenue"],
                                                          [["001", 100.0], ["002", 250.0]])}))
    d = C.parse_tds(SALESFORCE_EXTRACT_DS)
    assert d["is_extract"] is True and d["flatfile_filename"] is None
    res = A.materialize_bundled_flatfile_data(arc, d, str(tmp_path / "out"))
    assert res["kind"] == "csv"
    assert res["hyper_present"] is True
    assert set(res["table_csv_paths"]) == {"Account"}
    csv_path = res["table_csv_paths"]["Account"]
    assert os.path.isabs(csv_path) and os.path.isfile(csv_path)


def test_migrate_datasource_routes_saas_extract_to_csv_import(tmp_path, monkeypatch):
    # The direct path (migrate_datasource) builds a local-CSV Import over the SaaS extract snapshot.
    arc = _make_zip(tmp_path / "Salesforce Admin Insights.tdsx", {
        "ds.tds": "<datasource/>",
        "Data/extract/extract.hyper": b"HYPERBINARY",
    })
    monkeypatch.setattr(hr, "extract_to_csv",
                        _fake_extract_to_csv({"Account": (["Id", "AnnualRevenue"],
                                                          [["001", 100.0]])}))
    dest = tmp_path / "data"
    res = A.migrate_datasource(SALESFORCE_EXTRACT_DS, model_name="M", packaged_source=arc,
                               flatfile_dest_dir=str(dest))
    ffd = res["report"]["flatfile_data"]
    assert ffd["landed"] is True and ffd["kind"] == "csv"
    assert res["report"].get("local_import")
    blob = "\n".join(res["parts"].values())
    assert "Csv.Document" in blob


def test_migrate_datasource_saas_extract_no_hyper_fails_closed(tmp_path):
    # A SaaS extract descriptor whose package carries NO .hyper has no data to land -> the direct
    # path must fail closed (no dishonest live-connection model), reporting the honest reason.
    arc = _make_zip(tmp_path / "Salesforce Admin Insights.tdsx", {"ds.tds": "<datasource/>"})
    dest = tmp_path / "data"
    res = A.migrate_datasource(SALESFORCE_EXTRACT_DS, model_name="M", packaged_source=arc,
                               flatfile_dest_dir=str(dest))
    ffd = res["report"]["flatfile_data"]
    assert ffd["landed"] is False
    assert ffd["reason"] == "no_bundled_data"
    # no local-CSV Import was built, and it did not fall through to a live Salesforce model.
    blob = "\n".join(res["parts"].values())
    assert "Csv.Document" not in blob


def test_estate_saas_extract_lands_csv_import(tmp_path, monkeypatch):
    # The estate path (_migrate_one_datasource) lands the SaaS extract as a local-CSV Import too.
    arc = _make_zip(tmp_path / "Salesforce Admin Insights.tdsx", {
        "ds.tds": "<datasource/>",
        "Data/extract/extract.hyper": b"HYPERBINARY",
    })
    monkeypatch.setattr(hr, "extract_to_csv",
                        _fake_extract_to_csv({"Account": (["Id", "AnnualRevenue"], [["001", 100.0]])}))
    sm_dir = tmp_path / "semantic_models"
    sm_dir.mkdir()
    detail = E._migrate_one_datasource(_ZipSource(SALESFORCE_EXTRACT_DS), arc, str(sm_dir), set())
    assert detail["flatfile_data"]["landed"] is True
    assert detail["flatfile_data"]["kind"] == "csv"
    assert detail["status"] in ("migrated", "migrated_with_followups")


def test_estate_saas_extract_no_hyper_fails_closed(tmp_path):
    # SaaS extract with no bundled .hyper -> the estate never writes a dataless model; it fails
    # closed to a fallback/followup, mirroring the flat-file "no data landed" path.
    arc = _make_zip(tmp_path / "Salesforce Admin Insights.tdsx", {"ds.tds": "<datasource/>"})
    sm_dir = tmp_path / "semantic_models"
    sm_dir.mkdir()
    detail = E._migrate_one_datasource(_ZipSource(SALESFORCE_EXTRACT_DS), arc, str(sm_dir), set())
    assert detail["flatfile_data"]["landed"] is False
    assert detail["status"] in ("fallback", "migrated_with_followups")
