"""Tests for the optional ``.hyper`` extract reader (local-POC import path).

Two layers, mirroring the module's design:

* the archive locator, the table-name/value normalizers, and the CSV writer are pure standard
  library and are tested directly -- no optional dependency required;
* the ``hyper_to_csv`` orchestration is exercised with an INJECTED fake ``tableauhyperapi`` so the
  control flow (schema -> table -> definition -> query -> CSV) is covered even on a machine without
  the wheel, and a final round-trip test runs against the REAL dependency only when it is installed
  (skipped otherwise, so the suite stays hermetic and no ``.hyper`` is committed).
"""
import csv
import io
import os
import zipfile

import pytest

import hyper_reader as hr


# -- archive handling ---------------------------------------------------------
def _make_archive(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return str(path)


def test_list_hyper_in_archive_finds_members(tmp_path):
    arc = _make_archive(tmp_path / "wb.twbx", {
        "wb.twb": "<workbook/>",
        "Data/extract/extract.hyper": b"HYPERBINARY",
        "Data/notes.txt": "hello",
    })
    assert hr.list_hyper_in_archive(arc) == ["Data/extract/extract.hyper"]


def test_list_hyper_in_archive_empty_for_live(tmp_path):
    arc = _make_archive(tmp_path / "live.tdsx", {"live.tds": "<datasource/>"})
    assert hr.list_hyper_in_archive(arc) == []


def test_list_hyper_in_archive_rejects_non_zip(tmp_path):
    p = tmp_path / "plain.tds"
    p.write_text("<datasource/>", encoding="utf-8")
    with pytest.raises(ValueError):
        hr.list_hyper_in_archive(str(p))


def test_find_hyper_passes_through_hyper_path(tmp_path):
    p = tmp_path / "extract.hyper"
    p.write_bytes(b"HYPER")
    assert hr.find_hyper_in_archive(str(p)) == str(p)


def test_find_hyper_extracts_from_archive(tmp_path):
    arc = _make_archive(tmp_path / "wb.tdsx", {
        "Data/extract.hyper": b"HYPERPAYLOAD",
    })
    dest = tmp_path / "out"
    got = hr.find_hyper_in_archive(arc, dest_dir=str(dest))
    assert os.path.isfile(got)
    assert got.lower().endswith(".hyper")
    with open(got, "rb") as fh:
        assert fh.read() == b"HYPERPAYLOAD"


def test_find_hyper_raises_when_no_extract(tmp_path):
    arc = _make_archive(tmp_path / "live.twbx", {"live.twb": "<workbook/>"})
    with pytest.raises(FileNotFoundError):
        hr.find_hyper_in_archive(arc, dest_dir=str(tmp_path / "out"))


# -- pure normalizers ---------------------------------------------------------
def test_safe_table_filename_strips_quotes_and_dots():
    assert hr._safe_table_filename('"Extract"."Pending Jobs"') == "Extract_Pending Jobs"
    assert hr._safe_table_filename("[Extract].[Truck/Rolls]") == "Extract_Truck_Rolls"
    assert hr._safe_table_filename('""') == "table"


def test_csv_value_normalization():
    assert hr._csv_value(None) == ""
    assert hr._csv_value(True) == "true"
    assert hr._csv_value(False) == "false"
    assert hr._csv_value(42) == "42"
    assert hr._csv_value("Beltway") == "Beltway"


def test_write_rows_csv_round_trips(tmp_path):
    out = tmp_path / "data" / "t.csv"
    written = hr.write_rows_csv(
        ["Region", "Pending", "Active"],
        [["Beltway", 32000, True], ["Florida", None, False]],
        str(out))
    assert os.path.isabs(written)
    with open(written, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["Region", "Pending", "Active"]
    assert rows[1] == ["Beltway", "32000", "true"]
    assert rows[2] == ["Florida", "", "false"]


# -- orchestration via an injected fake tableauhyperapi ------------------------
class _FakeName:
    def __init__(self, text):
        self.unescaped = text


class _FakeColumn:
    def __init__(self, text):
        self.name = _FakeName(text)


class _FakeTableDef:
    def __init__(self, colnames):
        self.columns = [_FakeColumn(c) for c in colnames]


class _FakeTable:
    def __init__(self, text):
        self._text = text

    def __str__(self):
        return self._text


class _FakeCatalog:
    def __init__(self, tables):
        # tables: {qualified_name: {"columns":[...], "rows":[...]}}
        self._tables = tables

    def get_schema_names(self):
        return ["public"]

    def get_table_names(self, schema):
        return [_FakeTable(name) for name in self._tables]

    def get_table_definition(self, table):
        return _FakeTableDef(self._tables[str(table)]["columns"])


class _FakeConnection:
    def __init__(self, *, endpoint, database, tables):
        self._tables = tables
        self.catalog = _FakeCatalog(tables)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute_list_query(self, query):
        # honor a trailing LIMIT N so the row_limit path is covered
        name = query.split("FROM", 1)[1].strip().split(" LIMIT ")[0].strip()
        rows = self._tables[name]["rows"]
        if " LIMIT " in query:
            n = int(query.rsplit(" LIMIT ", 1)[1])
            rows = rows[:n]
        return rows


class _FakeProcess:
    def __init__(self, *, telemetry):
        self.endpoint = "fake-endpoint"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeTelemetry:
    DO_NOT_SEND_USAGE_DATA = "off"


def _fake_hapi(tables):
    import types
    mod = types.SimpleNamespace()
    mod.Telemetry = _FakeTelemetry
    mod.HyperProcess = _FakeProcess
    mod.Connection = lambda *, endpoint, database: _FakeConnection(
        endpoint=endpoint, database=database, tables=tables)
    return mod


def test_hyper_to_csv_orchestrates_with_fake(tmp_path):
    tables = {
        "snapshot": {
            "columns": ["Region", "Pending"],
            "rows": [["Beltway", 32000], ["Florida", 3500], ["Chicago", 9100]],
        },
    }
    out = tmp_path / "data"
    res = hr.hyper_to_csv("ignored.hyper", str(out), hapi=_fake_hapi(tables))
    assert set(res) == {"snapshot"}
    entry = res["snapshot"]
    assert entry["columns"] == ["Region", "Pending"]
    assert entry["row_count"] == 3
    assert os.path.isfile(entry["csv_path"])
    with open(entry["csv_path"], newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["Region", "Pending"]
    assert rows[3] == ["Chicago", "9100"]


def test_hyper_to_csv_row_limit(tmp_path):
    tables = {"snapshot": {"columns": ["A"], "rows": [[1], [2], [3], [4]]}}
    res = hr.hyper_to_csv("x.hyper", str(tmp_path), hapi=_fake_hapi(tables), row_limit=2)
    assert res["snapshot"]["row_count"] == 2


# -- missing-dependency contract ----------------------------------------------
def _hyperapi_installed():
    import importlib.util
    return importlib.util.find_spec("tableauhyperapi") is not None


@pytest.mark.skipif(_hyperapi_installed(),
                    reason="dependency present; the missing-dep path can't be exercised")
def test_missing_dependency_raises_friendly(tmp_path):
    with pytest.raises(hr.HyperApiUnavailable) as exc:
        hr.hyper_to_csv("x.hyper", str(tmp_path))
    assert "pip install tableauhyperapi" in str(exc.value)


# -- real round-trip (only when the optional dependency is installed) ----------
@pytest.mark.skipif(not _hyperapi_installed(),
                    reason="tableauhyperapi not installed (optional POC dependency)")
def test_real_hyper_round_trip(tmp_path, monkeypatch):
    import tableauhyperapi as hapi
    # hyperd writes a ``hyperd.log`` into the process CWD; run from tmp_path so the real
    # HyperProcess (here and in hyper_to_csv) never pollutes the source tree (mirror parity).
    monkeypatch.chdir(tmp_path)
    hyper_path = tmp_path / "demo.hyper"
    table = hapi.TableName("Extract", "Snapshot")
    # The Telemetry enum member was renamed across tableauhyperapi releases
    # (DO_NOT_SEND_USAGE_DATA -> DO_NOT_SEND_USAGE_DATA_TO_TABLEAU); accept either.
    telemetry = (getattr(hapi.Telemetry, "DO_NOT_SEND_USAGE_DATA_TO_TABLEAU", None)
                 or getattr(hapi.Telemetry, "DO_NOT_SEND_USAGE_DATA"))
    with hapi.HyperProcess(telemetry=telemetry) as process:
        with hapi.Connection(endpoint=process.endpoint, database=str(hyper_path),
                             create_mode=hapi.CreateMode.CREATE_AND_REPLACE) as conn:
            conn.catalog.create_schema("Extract")
            tdef = hapi.TableDefinition(table, [
                hapi.TableDefinition.Column("Region", hapi.SqlType.text()),
                hapi.TableDefinition.Column("Pending", hapi.SqlType.int()),
            ])
            conn.catalog.create_table(tdef)
            with hapi.Inserter(conn, tdef) as inserter:
                inserter.add_rows([["Beltway", 32000], ["Florida", 3500]])
                inserter.execute()
    res = hr.hyper_to_csv(str(hyper_path), str(tmp_path / "data"))
    key = next(iter(res))
    assert res[key]["row_count"] == 2
    assert "Region" in res[key]["columns"]
