"""Tests for ``embedded_inventory.py`` -- enumerating embedded (in-workbook) datasources.

Covers the two enumeration paths (Metadata API + ``.twb``/``.twbx`` fallback), the workbook-local
object-list capture (calcs / sets / groups / bins / LODs), the ``source_id`` <-> ``workbook_luid``
linkage (which must NOT be assumed equal for a local-files run), the ``Parameters`` skip, the gather
orchestration with a fake client, and the ``--dry-run`` / local-files CLI.
"""
import io
import json
import os
import zipfile

import embedded_inventory as emb


# A structurally faithful .twb: a Parameters pseudo-datasource (skipped) and one real embedded
# datasource (SQL Server) with two physical columns plus a calc, an LOD, a bin, a group, and a set.
SAMPLE_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook>
  <datasources>
    <datasource name='Parameters' hasconnection='false' inline='true'>
      <column caption='Target' datatype='real' name='[Parameter 1]' param-domain-type='range'
              role='measure' type='quantitative' value='100'>
      </column>
    </datasource>
    <datasource caption='Superstore' inline='true' name='federated.0abc' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='SalesDW' name='sqlserver.1a2b'>
            <connection authentication='sqlserver' class='sqlserver' dbname='SalesDW'
                        server='sql.contoso.com' />
          </named-connection>
        </named-connections>
        <relation connection='sqlserver.1a2b' name='Orders' table='[dbo].[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Order_ID</remote-name>
            <local-name>[Order_ID]</local-name>
            <parent-name>[Orders]</parent-name>
            <local-type>string</local-type>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>Sales</remote-name>
            <local-name>[Sales]</local-name>
            <parent-name>[Orders]</parent-name>
            <local-type>real</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
      <column caption='Profit Ratio' datatype='real' name='[Calculation_1]' role='measure'
              type='quantitative'>
        <calculation class='tableau' formula='SUM([Profit]) / SUM([Sales])' />
      </column>
      <column caption='Sales per Customer' datatype='real' name='[Calculation_2]' role='measure'
              type='quantitative'>
        <calculation class='tableau' formula='{ FIXED [Customer Name] : SUM([Sales]) }' />
      </column>
      <column caption='Sales (bin)' datatype='integer' name='[Sales (bin)]' role='dimension'
              type='ordinal'>
        <calculation class='bin' decimals='0' field='[Sales]' formula='[Sales]' size='100' />
      </column>
      <group caption='Category Group' name='[Category (group)]' name-style='unqualified'>
        <groupfilter function='union' user:ui-domain='database'>
          <groupfilter function='member' level='[Category]' member='&quot;Furniture&quot;' />
        </groupfilter>
      </group>
      <group caption='Top Customers' name='[Top Customers Set]' name-style='unqualified'>
        <groupfilter function='filter' />
      </group>
    </datasource>
  </datasources>
</workbook>
"""

# A minimal one-datasource .twb used for the fallback / local tests.
TINY_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook>
  <datasources>
    <datasource caption='Ops Extract' inline='true' name='federated.9z'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='postgres.7q'>
            <connection class='postgres' dbname='ops' server='pg.contoso.com' />
          </named-connection>
        </named-connections>
        <relation connection='postgres.7q' name='Tickets' table='[public].[Tickets]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Ticket_ID</remote-name>
            <local-type>integer</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
    </datasource>
  </datasources>
</workbook>
"""

META_PAYLOAD = {
    "workbooksConnection": {
        "nodes": [
            {
                "luid": "wb-1", "name": "Sales Dashboard", "projectName": "Sales",
                "embeddedDatasources": [
                    {
                        "id": "ds-1", "name": "Superstore (embedded)", "hasExtracts": True,
                        "upstreamTables": [
                            {"name": "Orders", "schema": "dbo",
                             "fullName": "[SalesDW].[dbo].[Orders]", "connectionType": "sqlserver",
                             "database": {"name": "SalesDW", "connectionType": "sqlserver"}},
                        ],
                        "fields": [
                            {"__typename": "ColumnField", "name": "Order ID", "isHidden": False,
                             "dataType": "STRING", "role": "DIMENSION"},
                            {"__typename": "ColumnField", "name": "Sales", "isHidden": False,
                             "dataType": "REAL", "role": "MEASURE"},
                            {"__typename": "CalculatedField", "name": "Profit Ratio",
                             "isHidden": False, "dataType": "REAL", "role": "MEASURE",
                             "formula": "SUM([Profit])/SUM([Sales])"},
                            {"__typename": "CalculatedField", "name": "Sales per Customer",
                             "isHidden": False, "dataType": "REAL", "role": "MEASURE",
                             "formula": "{FIXED [Customer]:SUM([Sales])}"},
                            {"__typename": "BinField", "name": "Sales (bin)", "isHidden": False},
                            {"__typename": "GroupField", "name": "Category Group", "isHidden": False},
                            {"__typename": "SetField", "name": "Top Customers", "isHidden": False},
                            {"__typename": "ColumnField", "name": "Hidden Field", "isHidden": True,
                             "dataType": "STRING"},
                        ],
                    },
                ],
            },
            {"luid": "wb-2", "name": "Empty WB", "projectName": "Ops",
             "embeddedDatasources": []},
        ],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
}


class FakeClient:
    def __init__(self, meta_payload, workbooks=None, twb_by_luid=None):
        self._meta = meta_payload
        self._workbooks = workbooks or []
        self._twb = twb_by_luid or {}
        self.downloaded = []
        self.signed_out = False

    def metadata_query(self, query, variables):
        return self._meta

    def list_workbooks(self, page_size=100):
        return self._workbooks

    def download_workbook_twb(self, luid, timeout=180):
        self.downloaded.append(luid)
        return self._twb.get(luid)

    def sign_out(self):
        self.signed_out = True


# ----------------------------------------------------------------------------------- .twb parsing
def test_twb_parse_fields_and_sources():
    rows = emb.embedded_datasources_from_twb(
        SAMPLE_TWB, workbook_luid="wb-9", workbook_name="WB9", project="Demo", source_id="wb-9")
    assert len(rows) == 1  # the Parameters datasource is skipped
    row = rows[0]
    assert row["datasource_name"] == "Superstore"
    assert row["source_path"] == "twb"
    assert row["workbook_luid"] == "wb-9"
    assert row["source_id"] == "wb-9"

    names = {f["name"] for f in row["fields"]}
    assert {"Order_ID", "Sales", "Profit Ratio", "Sales per Customer", "Sales (bin)"} <= names

    assert row["sources"] == [{
        "connectionType": "sqlserver", "database": "SalesDW", "schema": "dbo", "table": "Orders"}]


def test_twb_object_list_classification():
    rows = emb.embedded_datasources_from_twb(SAMPLE_TWB, source_id="local.twb")
    objects = {o["name"]: o["kind"] for o in rows[0]["objects"]}
    assert objects["Profit Ratio"] == "calc"
    assert objects["Sales per Customer"] == "lod"      # {FIXED ...}
    assert objects["Sales (bin)"] == "bin"
    assert objects["Category Group"] == "group"
    assert objects["Top Customers"] == "set"
    # LOD formula is carried (XML-unescaped) for the downstream rebind gate.
    lod = next(o for o in rows[0]["objects"] if o["name"] == "Sales per Customer")
    assert "FIXED" in lod["formula"]


def test_twb_carries_distinct_identity_for_label_selector():
    # caption / raw name / formatted-name captured distinctly; label = caption|formatted-name|name.
    rows = emb.embedded_datasources_from_twb(SAMPLE_TWB, source_id="wb-9")
    row = rows[0]
    assert row["caption"] == "Superstore"
    assert row["name"] == "federated.0abc"        # RAW <datasource> name -- NOT debracketed
    assert row["formatted_name"] == ""
    assert row["label"] == "Superstore"            # caption preferred


def test_twb_parameters_datasource_skipped():
    rows = emb.embedded_datasources_from_twb(SAMPLE_TWB, source_id="x")
    assert all(r["datasource_name"] != "Parameters" for r in rows)


def test_twb_empty_input():
    assert emb.embedded_datasources_from_twb("") == []
    assert emb.embedded_datasources_from_twb("<workbook></workbook>") == []


# ------------------------------------------------------------------------------- metadata shaping
def test_metadata_shaping():
    node = META_PAYLOAD["workbooksConnection"]["nodes"][0]
    rows = emb.shape_embedded_from_metadata(node)
    assert len(rows) == 1
    row = rows[0]
    assert row["workbook_luid"] == "wb-1"
    assert row["source_id"] == "wb-1"
    assert row["datasource_name"] == "Superstore (embedded)"
    assert row["datasource_id"] == "ds-1"
    assert row["has_extract"] is True
    assert row["source_path"] == "metadata"
    # Catalog exposes only the display name -> carried as caption + label; raw name / formatted-name
    # are not available (documented Metadata-API caption-only caveat).
    assert row["caption"] == "Superstore (embedded)"
    assert row["label"] == "Superstore (embedded)"
    assert row["name"] == ""
    assert row["formatted_name"] == ""

    field_names = {f["name"] for f in row["fields"]}
    assert "Hidden Field" not in field_names          # hidden fields dropped
    assert {"Order ID", "Sales", "Profit Ratio"} <= field_names

    assert row["sources"][0]["table"] == "Orders"

    objects = {o["name"]: o["kind"] for o in row["objects"]}
    assert objects == {
        "Profit Ratio": "calc",
        "Sales per Customer": "lod",
        "Sales (bin)": "bin",
        "Category Group": "group",
        "Top Customers": "set",
    }


def test_lod_detection():
    assert emb._is_lod("{FIXED [A]: SUM([X])}")
    assert emb._is_lod("{ INCLUDE [A] : AVG([X]) }")
    assert emb._is_lod("{EXCLUDE [A]:MIN([X])}")
    assert not emb._is_lod("SUM([X]) / SUM([Y])")
    assert not emb._is_lod(None)


# ------------------------------------------------------------------------------------ orchestration
def test_gather_metadata_primary_then_twb_fallback():
    client = FakeClient(
        META_PAYLOAD,
        workbooks=[
            {"luid": "wb-1", "name": "Sales Dashboard", "project": "Sales"},
            {"luid": "wb-2", "name": "Empty WB", "project": "Ops"},
        ],
        twb_by_luid={"wb-2": TINY_TWB},
    )
    rows = emb.gather_embedded_inventory(client)
    by_wb = {r["workbook_luid"]: r for r in rows}
    assert set(by_wb) == {"wb-1", "wb-2"}
    assert by_wb["wb-1"]["source_path"] == "metadata"
    assert by_wb["wb-2"]["source_path"] == "twb"
    # The well-covered workbook is NOT re-downloaded; only the empty one falls back.
    assert client.downloaded == ["wb-2"]


def test_gather_twb_fallback_never():
    client = FakeClient(
        META_PAYLOAD,
        workbooks=[{"luid": "wb-2", "name": "Empty WB", "project": "Ops"}],
        twb_by_luid={"wb-2": TINY_TWB},
    )
    rows = emb.gather_embedded_inventory(client, twb_fallback="never")
    assert [r["workbook_luid"] for r in rows] == ["wb-1"]
    assert client.downloaded == []   # no network fallback when 'never'


def test_gather_metadata_unavailable_falls_back():
    class Broken(FakeClient):
        def metadata_query(self, query, variables):
            raise emb.TableauError("Catalog disabled")

    client = Broken(
        META_PAYLOAD,
        workbooks=[{"luid": "wb-2", "name": "Empty WB", "project": "Ops"}],
        twb_by_luid={"wb-2": TINY_TWB},
    )
    rows = emb.gather_embedded_inventory(client)
    assert [r["workbook_luid"] for r in rows] == ["wb-2"]
    assert rows[0]["source_path"] == "twb"


# -------------------------------------------------------------------------------------- local files
def test_local_twb_file(tmp_path):
    p = tmp_path / "Superstore Sales.twb"
    p.write_text(SAMPLE_TWB, encoding="utf-8")
    rows = emb.gather_embedded_inventory_local([str(p)])
    assert len(rows) == 1
    # Local files carry no server luid -- source_id is the file name and is NOT the luid.
    assert rows[0]["workbook_luid"] == ""
    assert rows[0]["source_id"] == "Superstore Sales.twb"
    assert rows[0]["workbook_name"] == "Superstore Sales"


def test_local_twbx_zip(tmp_path):
    p = tmp_path / "packaged.twbx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("packaged.twb", SAMPLE_TWB)
        zf.writestr("Data/extract.hyper", b"\x00binary-extract")
    p.write_bytes(buf.getvalue())
    rows = emb.gather_embedded_inventory_local([str(p)])
    assert len(rows) == 1
    assert rows[0]["source_id"] == "packaged.twbx"
    assert rows[0]["datasource_name"] == "Superstore"


def test_source_map_links_filename_to_luid():
    rows = [
        {"source_id": "wb-1", "workbook_luid": "wb-1"},
        {"source_id": "Local.twb", "workbook_luid": ""},
    ]
    assert emb.build_source_map(rows) == {"wb-1": "wb-1", "Local.twb": ""}


# --------------------------------------------------------------------------------------------- CLI
def test_cli_dry_run_live(capsys):
    rc = emb.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "/api/metadata/graphql" in out
    assert "workbooks/<id>/content" in out


def test_cli_dry_run_local(capsys, tmp_path):
    p = tmp_path / "wb.twb"
    p.write_text(SAMPLE_TWB, encoding="utf-8")
    rc = emb.main(["--dry-run", "--twb", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "local-files mode" in out
    assert "wb.twb" in out


def test_cli_local_writes_json(tmp_path, capsys):
    src = tmp_path / "Sales.twb"
    src.write_text(SAMPLE_TWB, encoding="utf-8")
    out = tmp_path / "embedded.json"
    rc = emb.main(["--twb", str(src), "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["datasource_name"] == "Superstore"
    assert {o["kind"] for o in data[0]["objects"]} == {"calc", "lod", "bin", "group", "set"}
