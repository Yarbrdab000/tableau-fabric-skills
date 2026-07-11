"""Tests for the local-POC CSV Import path (``assemble_local_import_model`` + ``local_data=``).

These cover the customer scenario where the source connector is UNMAPPED (S3 / generic ODBC / Web
Data Connector) but the published datasource carries an extract: today that routes to the
needs-storage-decision fallback (no runnable model, DirectLake is opt-in), and the opt-in
``local_data=`` instead builds a clickable Import model backed by local CSV files -- no Fabric, no
lakehouse, no credentials.

All inline ``.tds`` documents are authored here (the repo git-ignores real Tableau artifacts) and
all CSVs are written to pytest ``tmp_path``; nothing is committed.
"""
import json
import os

import pytest

import assemble_model as A


# An extract-backed datasource over an UNMAPPED connector (generic ODBC, like Comcast's MinIO feed
# reached via ODBC). One snapshot table + a measure calc. The <extract> marks it extract-enabled.
PENDING_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Pending Truck Rolls' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='minio' name='odbc.cc11'>
        <connection class='genericodbc' dbname='dx' server='data.comcast.com' />
      </named-connection>
    </named-connections>
    <relation connection='odbc.cc11' name='PendingJobSnapshot'
              table='[dx].[PendingJobSnapshot]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[PendingJobSnapshot]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>PendingJobs</remote-name><local-name>[PendingJobs]</local-name>
        <parent-name>[PendingJobSnapshot]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>SnapshotDate</remote-name><local-name>[SnapshotDate]</local-name>
        <parent-name>[PendingJobSnapshot]</parent-name><local-type>date</local-type>
      </metadata-record>
    </metadata-records>
    <extract enabled='true' />
  </connection>
  <column caption='Total Pending' datatype='integer' name='[Calculation_1]' role='measure'>
    <calculation class='tableau' formula='SUM([PendingJobs])' />
  </column>
</datasource>"""

# A two-table extract over an unmapped (S3) connector, no join tree.
TWO_TABLE_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Two Table Feed' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='s3' name='s3.dd22'>
        <connection class='s3' dbname='lake' server='minio.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='s3.dd22' name='Orders' table='[lake].[Orders]' type='table' />
    <relation connection='s3.dd22' name='Regions' table='[lake].[Regions]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>OrderId</remote-name><local-name>[OrderId]</local-name>
        <parent-name>[Orders]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>RegionName</remote-name><local-name>[RegionName]</local-name>
        <parent-name>[Regions]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
    <extract enabled='true' />
  </connection>
</datasource>"""

# A supported-connector extract (SQL Server) -- local_data must still override it to a CSV partition.
SQLSERVER_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Widget Sales' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='warehouse' name='sqlserver.aa11'>
        <connection authentication='sqlserver' class='sqlserver' dbname='WidgetDW'
                    server='widgetdw.database.windows.net' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.aa11' name='Sales' table='[dbo].[Sales]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Amount</remote-name><local-name>[Amount]</local-name>
        <parent-name>[Sales]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
    <extract enabled='true' />
  </connection>
</datasource>"""


# An extract-backed unmapped-connector datasource that ALSO carries a what-if (range) parameter and a
# calc that inlines it (``SUM([Sales]) * [Parameters].[Growth Rate]``). The consolidation/local-CSV path
# assembles straight from the descriptor (no source text in hand), so unless ``migrate_datasource``
# parses + threads the parameters here, ``param_resolver`` is None and this calc stubs "unmodeled".
PARAM_LOCAL_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Param Feed' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='minio' name='odbc.cc11'>
        <connection class='genericodbc' dbname='dx' server='data.comcast.com' />
      </named-connection>
    </named-connections>
    <relation connection='odbc.cc11' name='Orders' table='[dx].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
    <extract enabled='true' />
  </connection>
  <column caption='Growth Rate' datatype='real' name='[Growth]' param-domain-type='range' value='1.0'>
    <range min='0.0' max='2.0' granularity='0.1' />
  </column>
  <column caption='Boosted Sales' datatype='real' name='[Calculation_1]' role='measure'>
    <calculation class='tableau' formula='SUM([Sales]) * [Parameters].[Growth Rate]' />
  </column>
</datasource>"""


def _write_csv(path, header, rows):
    import csv
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    return os.path.abspath(path)


def _table_part(result):
    """Return the first non-Date, non-Measures table TMDL body in the result parts."""
    for path, text in result["parts"].items():
        if path.startswith("definition/tables/") and "_Measures" not in path and "Date" not in path:
            return text
    return ""


def _count_columns(body, name):
    """Count ``column <name>`` declarations in a TMDL table body (exact line match)."""
    return sum(1 for ln in body.splitlines() if ln.strip() == "column {}".format(name))


# -- the blocker: unmapped extract without local_data falls back (no runnable model) -------------
def test_unmapped_extract_falls_back_without_local_data():
    result = A.migrate_datasource(PENDING_TDS, model_name="Pending")
    assert result["parts"] == {}
    assert result["report"]["fallback"] is True
    # de-default: an unmapped extract with no local_data is a needs-storage-decision fallback --
    # DirectLake is opt-in, so NO landing_plan is auto-built.
    assert "landing_plan" not in result["report"]
    assert result["report"]["storage_decision"]["fallback"] == "needs-storage-decision"


# -- local_data dict builds a real CSV-backed Import model ---------------------------------------
def test_local_data_dict_builds_csv_import(tmp_path):
    csv_path = _write_csv(str(tmp_path / "snap.csv"),
                          ["Region", "PendingJobs", "SnapshotDate"],
                          [["Beltway", 32000, "2024-01-01"], ["Florida", 3500, "2024-01-01"]])
    result = A.migrate_datasource(
        PENDING_TDS, model_name="Pending",
        local_data={"PendingJobSnapshot": csv_path})

    assert result["parts"], "a real model (non-empty parts) must be produced"
    assert "landing_plan" not in result["report"]
    assert result["report"].get("fallback") is not True
    assert result["report"]["storage_decision"]["mode"] == "Import"

    li = result["report"]["local_import"]
    assert li["data_source"] == "local-csv"
    assert li["matched_count"] == 1
    assert li["unmatched_tables"] == []

    body = _table_part(result)
    assert "Csv.Document" in body
    assert "snap.csv" in body
    # the measure survived into a _Measures table
    assert any("_Measures" in p for p in result["parts"])


def test_local_data_single_csv_path_binds_via_single_default(tmp_path):
    # a single .csv whose stem does NOT match the table name still binds (1 table, 1 csv).
    csv_path = _write_csv(str(tmp_path / "whatever.csv"), ["Region", "PendingJobs"],
                          [["Beltway", 1]])
    result = A.migrate_datasource(PENDING_TDS, model_name="Pending", local_data=csv_path)
    assert result["report"]["local_import"]["matched_count"] == 1
    assert "Csv.Document" in _table_part(result)


def test_local_data_directory_of_csvs(tmp_path):
    data_dir = tmp_path / "data"
    _write_csv(str(data_dir / "anything.csv"), ["Region", "PendingJobs"], [["Beltway", 1]])
    result = A.migrate_datasource(PENDING_TDS, model_name="Pending", local_data=str(data_dir))
    assert result["report"]["local_import"]["matched_count"] == 1


def test_local_data_writes_openable_pbip(tmp_path):
    csv_path = _write_csv(str(tmp_path / "snap.csv"), ["Region", "PendingJobs"], [["Beltway", 1]])
    out = tmp_path / "out"
    result = A.migrate_datasource(
        PENDING_TDS, model_name="Pending", local_data={"PendingJobSnapshot": csv_path},
        write_to=str(out), as_pbip=True)
    assert os.path.isfile(result["pbip"])
    assert os.path.isdir(os.path.join(str(out), "Pending.SemanticModel"))


def test_local_data_overrides_supported_connector(tmp_path):
    # SQL Server extract -> normally Sql.Database; local_data forces a CSV partition instead.
    csv_path = _write_csv(str(tmp_path / "sales.csv"), ["Amount"], [[10.5]])
    result = A.migrate_datasource(SQLSERVER_TDS, model_name="Sales",
                                  local_data={"Sales": csv_path})
    body = _table_part(result)
    assert "Csv.Document" in body
    assert "Sql.Database" not in body


# -- Spec 3: parameters reach the calc translator on the local-CSV (consolidation) path -----------
def test_param_driven_calc_translates_on_local_csv_path(tmp_path):
    # A what-if-parameter-driven measure must land as a REAL measure -- not a stub -- when the model is
    # built through the local-CSV path (the extract-backed consolidation route). This is the live-DB
    # analog of the direct migrate_tds_to_semantic_model path, which auto-parses parameters from the
    # source text; here migrate_datasource must parse + thread them itself so param_resolver is built.
    csv_path = _write_csv(str(tmp_path / "orders.csv"), ["Sales"], [[10.0], [20.0]])
    result = A.migrate_datasource(
        PARAM_LOCAL_TDS, model_name="Orders", local_data={"Orders": csv_path})

    measures = {m["measure"]: m for m in result["report"]["measures"]}
    boosted = measures["Boosted Sales"]
    assert boosted["status"] == "translated", boosted
    # the parameter is inlined as its what-if value measure, not left as an unmodeled reference
    assert "[Growth Rate Value]" in boosted["dax"]
    assert "[Parameters]" not in boosted["dax"]
    # the what-if table backing the parameter was emitted into the model
    assert any("Growth Rate" in p for p in result["parts"])


def test_local_csv_param_absent_leaves_calc_stub_and_no_whatif_table(tmp_path):
    # Fail-closed / caller-wins guard: an explicit parameters=[] (the pre-fix behaviour) is honoured --
    # the param stays unmodelled, the calc stubs, and no what-if table is fabricated. Proves the
    # auto-parse threading is load-bearing (it only engages when the caller did not supply parameters).
    csv_path = _write_csv(str(tmp_path / "orders.csv"), ["Sales"], [[10.0]])
    result = A.migrate_datasource(
        PARAM_LOCAL_TDS, model_name="Orders", local_data={"Orders": csv_path}, parameters=[])

    boosted = {m["measure"]: m for m in result["report"]["measures"]}["Boosted Sales"]
    assert boosted["status"] == "stub"
    assert "[Parameters].[Growth Rate]" in (boosted.get("reason") or "")
    assert not any("Growth Rate" in p for p in result["parts"])


def test_multi_table_unmatched_is_reported_not_dropped(tmp_path):
    csv_path = _write_csv(str(tmp_path / "orders.csv"), ["OrderId"], [[1]])
    result = A.migrate_datasource(TWO_TABLE_TDS, model_name="TwoTable",
                                  local_data={"Orders": csv_path})
    li = result["report"]["local_import"]
    assert li["table_count"] == 2
    assert li["matched_count"] == 1
    assert li["unmatched_tables"] == ["Regions"]
    # both tables are still emitted (nothing silently dropped)
    table_parts = [p for p in result["parts"] if p.startswith("definition/tables/")]
    assert any("Orders" in p for p in table_parts)
    assert any("Regions" in p for p in table_parts)


# -- helper resolution --------------------------------------------------------------------------
def test_resolve_local_csv_paths_dict_passthrough():
    m = A._resolve_local_csv_paths({"T": "/x/y.csv"}, source=None, model_name="M", write_to=None)
    assert m == {"T": "/x/y.csv"}


def test_resolve_local_csv_paths_directory(tmp_path):
    _write_csv(str(tmp_path / "a.csv"), ["c"], [[1]])
    _write_csv(str(tmp_path / "b.csv"), ["c"], [[2]])
    m = A._resolve_local_csv_paths(str(tmp_path), source=None, model_name="M", write_to=None)
    assert set(m) == {"a", "b"}
    assert all(os.path.isabs(p) for p in m.values())


def test_resolve_local_csv_paths_rejects_garbage():
    with pytest.raises(ValueError):
        A._resolve_local_csv_paths(123, source=None, model_name="M", write_to=None)


def test_assemble_local_import_model_directly(tmp_path):
    from connection_to_m import parse_tds
    csv_path = _write_csv(str(tmp_path / "snap.csv"), ["Region", "PendingJobs"], [["Beltway", 1]])
    desc = parse_tds(PENDING_TDS)
    result = A.assemble_local_import_model(
        desc, model_name="Pending", table_csv_paths={"PendingJobSnapshot": csv_path})
    assert result["report"]["local_import"]["matched_count"] == 1
    assert result["report"]["storage_decision"]["connector"] == "Csv.Document"


# -- rm-local-csv-column-dedupe: phantom-drop + dedupe against the real CSV header ---------------
# A .tds whose metadata lists a column absent from the materialized CSV (``SnapshotDate``) and a
# duplicate physical column (``Region`` twice -- an object-id-twin artifact). Emitting either makes
# the Import model dead-on-arrival: Power BI's ``Csv.Document`` type-transform references a header
# that isn't in the file, and a duplicate TMDL column name is invalid.
PHANTOM_DUP_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Snap' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='minio' name='odbc.cc11'>
        <connection class='genericodbc' dbname='dx' server='data.comcast.com' />
      </named-connection>
    </named-connections>
    <relation connection='odbc.cc11' name='Snap' table='[dx].[Snap]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Snap]</parent-name><local-type>string</local-type><ordinal>0</ordinal>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>PendingJobs</remote-name><local-name>[PendingJobs]</local-name>
        <parent-name>[Snap]</parent-name><local-type>integer</local-type><ordinal>1</ordinal>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>SnapshotDate</remote-name><local-name>[SnapshotDate]</local-name>
        <parent-name>[Snap]</parent-name><local-type>date</local-type><ordinal>2</ordinal>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region (copy)]</local-name>
        <parent-name>[Snap]</parent-name><local-type>string</local-type><ordinal>3</ordinal>
      </metadata-record>
    </metadata-records>
    <extract enabled='true' />
  </connection>
</datasource>"""

# A .tds that exposes a column under a Tableau ALIAS (``Person``) that never appears as a physical
# header (physically ``Regional Manager``). This must be REMAPPED to the real header (kept), never
# mistaken for a phantom and dropped.
ALIAS_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Team' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='minio' name='odbc.cc11'>
        <connection class='genericodbc' dbname='dx' server='data.comcast.com' />
      </named-connection>
    </named-connections>
    <relation connection='odbc.cc11' name='Team' table='[dx].[Team]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>EmployeeId</remote-name><local-name>[EmployeeId]</local-name>
        <parent-name>[Team]</parent-name><local-type>integer</local-type><ordinal>0</ordinal>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Person</remote-name><local-name>[Person]</local-name>
        <parent-name>[Team]</parent-name><local-type>string</local-type><ordinal>1</ordinal>
      </metadata-record>
    </metadata-records>
    <extract enabled='true' />
  </connection>
</datasource>"""


def test_local_csv_drops_phantom_column(tmp_path):
    # SnapshotDate is in the .tds metadata but NOT in the materialized CSV header.
    csv_path = _write_csv(str(tmp_path / "snap.csv"), ["Region", "PendingJobs"], [["Beltway", 1]])
    result = A.migrate_datasource(PHANTOM_DUP_TDS, model_name="Snap",
                                  local_data={"Snap": csv_path})
    body = _table_part(result)
    # the phantom must not be emitted as a column nor typed in the M partition (both are DOA)
    assert _count_columns(body, "SnapshotDate") == 0
    assert '"SnapshotDate"' not in body
    # ...and it is disclosed, never silently dropped
    dropped = result["report"]["local_import"]["column_reconcile"]["dropped"]
    assert any(d["source_column"] == "SnapshotDate" for d in dropped)
    # the real columns survive
    assert _count_columns(body, "PendingJobs") == 1


def test_local_csv_dedupes_duplicate_columns(tmp_path):
    # Region appears twice in the metadata but is a single physical header. The twin now collapses at
    # PARSE (_columns_by_parent, keep-first), so the model is openable regardless of whether a CSV
    # header is readable; the flat-file reconcile no longer has to dedupe it.
    csv_path = _write_csv(str(tmp_path / "snap.csv"), ["Region", "PendingJobs"], [["Beltway", 1]])
    result = A.migrate_datasource(PHANTOM_DUP_TDS, model_name="Snap",
                                  local_data={"Snap": csv_path})
    body = _table_part(result)
    assert _count_columns(body, "Region") == 1        # duplicate column collapsed to one
    assert body.count('"Region"') == 1                # and typed once in the M partition
    gate = result["report"].get("openability_selfcheck") or {}
    assert gate.get("checks", {}).get("no_duplicate_columns") is True


# A MAPPED (sqlserver) source whose metadata carries a TWIN column record -- one logical record with
# no <ordinal>, one from the extract cache with an <ordinal> -- both cleaning to the same model_name.
# There is no local CSV here, so the flat-file column_reconcile never runs; parse-level dedup in
# _columns_by_parent is the ONLY thing that keeps the model openable (no duplicate column).
TWIN_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Orders' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.0a1b2c'>
        <connection authentication='sqlserver' class='sqlserver' dbname='Sales'
                    server='srv.database.windows.net' username='svc' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type><ordinal>1</ordinal>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type><ordinal>0</ordinal>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


def test_twin_metadata_column_deduped_at_parse_live_db():
    # No local_data: the flat-file reconcile never runs, so parse-level dedup is the only safety net.
    result = A.migrate_datasource(TWIN_TDS, model_name="Orders")
    body = _table_part(result)
    assert _count_columns(body, "Region") == 1        # the twin collapsed to a single column
    gate = result["report"].get("openability_selfcheck") or {}
    assert gate.get("checks", {}).get("no_duplicate_columns") is True


def test_local_csv_keeps_columns_when_header_unreadable(tmp_path):
    # An unreadable CSV header must never trigger a drop (fail-safe: we can't confirm absence).
    from connection_to_m import parse_tds
    missing = str(tmp_path / "does_not_exist.csv")
    result = A.assemble_local_import_model(
        parse_tds(PHANTOM_DUP_TDS), model_name="Snap", table_csv_paths={"Snap": missing})
    body = _table_part(result)
    assert _count_columns(body, "SnapshotDate") == 1  # phantom retained (header unknown)
    cr = result["report"]["local_import"]["column_reconcile"]
    assert cr["dropped"] == [] and cr["deduped"] == []


def test_local_csv_aliased_column_remapped_not_dropped(tmp_path):
    # An aliased source name (Person -> physical "Regional Manager") is remapped, never dropped.
    csv_path = _write_csv(str(tmp_path / "team.csv"),
                          ["EmployeeId", "Regional Manager"], [[1, "Ada"]])
    result = A.migrate_datasource(ALIAS_TDS, model_name="Team",
                                  local_data={"Team": csv_path})
    body = _table_part(result)
    assert _count_columns(body, "Person") == 1        # kept (remapped), not dropped
    assert '"Regional Manager"' in body               # typed against the real header
    cr = result["report"]["local_import"]["column_reconcile"]
    assert any(r.get("to") == "Regional Manager" for r in cr["remapped"])
    assert cr["dropped"] == []


# -- openability self-check is wired into the build and agrees the model is open ----------------
def test_openability_selfcheck_present_and_ok_on_local_build(tmp_path):
    csv_path = _write_csv(str(tmp_path / "snap.csv"),
                          ["Region", "PendingJobs", "SnapshotDate"],
                          [["Beltway", 32000, "2024-01-01"]])
    result = A.migrate_datasource(
        PENDING_TDS, model_name="Pending", local_data={"PendingJobSnapshot": csv_path})

    gate = result["report"]["openability_selfcheck"]
    assert gate["ok"] is True
    assert gate["issues"] == []
    assert gate["checks"]["no_duplicate_columns"] is True
    assert gate["checks"]["typed_columns_declared"] is True
    # the physical-header check actually ran against the real landed CSV (not merely skipped)
    assert gate["checks"]["typed_columns_in_header"] is True


def test_openability_selfcheck_ok_after_phantom_dedupe(tmp_path):
    # PHANTOM_DUP_TDS would emit a duplicate + a phantom column; the local-CSV reconcile fixes both.
    # The gate must independently confirm the RESULT is open (no dup, no typed phantom vs header).
    csv_path = _write_csv(str(tmp_path / "snap.csv"), ["Region", "PendingJobs"], [["Beltway", 1]])
    result = A.migrate_datasource(PHANTOM_DUP_TDS, model_name="Snap",
                                  local_data={"Snap": csv_path})
    gate = result["report"]["openability_selfcheck"]
    assert gate["ok"] is True, gate["issues"]
    assert gate["checks"]["no_duplicate_columns"] is True
    assert gate["checks"]["typed_columns_in_header"] is True

