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
