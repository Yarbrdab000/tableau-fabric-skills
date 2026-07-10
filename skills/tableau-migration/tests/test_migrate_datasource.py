"""Tests for the one-call ``migrate_datasource`` wrapper and ``_read_tds_source``.

These cover the pilot-feedback gaps: a single entry point that (1) accepts a ``.tdsx``/``.tds``
path *or* raw text, (2) **auto-extracts** calculated fields (no hand-rolled XML walker), (3)
returns the credential-free ``bind`` target, and (4) optionally persists a model folder or an
openable ``.pbip`` -- so a future agent's job is download -> migrate -> deploy.
"""
import io
import json
import os
import sys
import zipfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import assemble_model as A  # noqa: E402
from test_connection_to_m import LIVE_SQLSERVER, MULTI_CONN  # noqa: E402

# An unknown/unmapped connector (saphana) with a real table + columns and a calc: a GENUINE fallback
# that still has substance, so the emitted landing plan carries per-table targets and a calc list.
SAPHANA_FALLBACK = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Hana DS' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='h' name='saphana.1'>
        <connection authentication='Username Password' class='saphana' dbname='HDB'
                    schema='SALES' server='hana.corp' username='svc' />
      </named-connection>
    </named-connections>
    <relation connection='saphana.1' name='Orders' table='[SALES].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order ID</remote-name><local-name>[Order ID]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Total' datatype='real' name='[Calculation_1]' role='measure' type='quantitative'>
    <calculation class='tableau' formula='SUM([Sales])' />
  </column>
</datasource>"""


# LIVE_SQLSERVER assembles cleanly; inject one translatable measure calc so auto-extraction is
# observable as a real DAX measure (the <column> sits at datasource level, after </connection>).
TDS_WITH_CALC = LIVE_SQLSERVER.replace(
    "</datasource>",
    "  <column caption='Total Sales' datatype='real' name='[Calculation_1]' role='measure' "
    "type='quantitative'>\n"
    "    <calculation class='tableau' formula='SUM([Sales])' />\n"
    "  </column>\n</datasource>",
)


# A dimension-role calc must route to a DAX calculated COLUMN (column mode), not a measure.
TDS_WITH_DIM_CALC = LIVE_SQLSERVER.replace(
    "</datasource>",
    "  <column caption='Total Sales' datatype='real' name='[Calculation_1]' role='measure' "
    "type='quantitative'>\n"
    "    <calculation class='tableau' formula='SUM([Sales])' />\n"
    "  </column>\n"
    "  <column caption='Upper Order' datatype='string' name='[Calculation_2]' role='dimension'>\n"
    "    <calculation class='tableau' formula='UPPER([Order ID])' />\n"
    "  </column>\n</datasource>",
)


def _all_text(parts):
    return "\n".join(parts.values())


def test_migrate_datasource_auto_extracts_calcs_from_text():
    out = A.migrate_datasource(TDS_WITH_CALC, model_name="Superstore")  # note: no calcs= passed
    text = _all_text(out["parts"])
    assert "Total Sales" in text
    assert "SUM('Orders'[Sales])" in text  # deterministically translated, not stubbed
    assert isinstance(out["bind"], dict) and "error" not in out["bind"]


def test_migrate_datasource_calcs_empty_emits_no_measures():
    out = A.migrate_datasource(TDS_WITH_CALC, model_name="Superstore", calcs=[])
    assert "Total Sales" not in _all_text(out["parts"])


def test_migrate_datasource_routes_dimension_calc_to_calc_column():
    # Auto-extraction splits by role: the measure calc stays a measure, while the dimension calc
    # becomes a DAX calculated column on its home table -- not a (mis-routed) measure stub.
    out = A.migrate_datasource(TDS_WITH_DIM_CALC, model_name="Superstore")
    report = out["report"]

    cols = {c["column"]: c for c in report["calc_columns"]}
    assert cols["Upper Order"]["table"] == "Orders"
    assert cols["Upper Order"]["status"] == "translated"
    assert "UPPER('Orders'[Order_ID])" in cols["Upper Order"]["dax"]

    # The dimension calc is rendered as a column on Orders, and is NOT in the measures list.
    orders_tmdl = out["parts"]["definition/tables/Orders.tmdl"]
    assert "column 'Upper Order'" in orders_tmdl
    measure_names = {m["measure"] for m in report["measures"]}
    assert "Upper Order" not in measure_names
    assert "Total Sales" in measure_names  # the measure-role calc still routes to a measure


def test_migrate_datasource_explicit_calcs_disables_role_split():
    # An explicit calcs= keeps full caller control: no auto-split, no calc columns invented.
    out = A.migrate_datasource(
        TDS_WITH_DIM_CALC, model_name="Superstore",
        calcs=[{"name": "Total Sales", "formula": "SUM([Sales])"}])
    assert out["report"]["calc_columns"] == []
    assert {m["measure"] for m in out["report"]["measures"]} == {"Total Sales"}


def test_migrate_datasource_writes_model_folder(tmp_path):
    dest = str(tmp_path / "out")
    out = A.migrate_datasource(TDS_WITH_CALC, model_name="Superstore", write_to=dest)
    assert out["model_dir"] == os.path.join(dest, "Superstore.SemanticModel")
    assert os.path.isfile(os.path.join(out["model_dir"], "definition", "model.tmdl"))


def test_migrate_datasource_writes_openable_pbip(tmp_path):
    dest = str(tmp_path / "out")
    out = A.migrate_datasource(TDS_WITH_CALC, model_name="Superstore", write_to=dest, as_pbip=True)
    assert os.path.isfile(out["pbip"])
    proj = json.loads(open(out["pbip"], encoding="utf-8").read())
    assert proj["$schema"].endswith("pbip/pbipProperties/1.0.0/schema.json")
    assert os.path.isdir(os.path.join(dest, "Superstore.SemanticModel"))
    assert os.path.isdir(os.path.join(dest, "Superstore.Report"))


def test_read_tds_source_passthrough_text():
    assert A._read_tds_source(LIVE_SQLSERVER) is LIVE_SQLSERVER


def test_read_tds_source_reads_tds_file(tmp_path):
    p = tmp_path / "ds.tds"
    p.write_text(LIVE_SQLSERVER, encoding="utf-8-sig")  # BOM, as real Tableau files have
    text = A._read_tds_source(str(p))
    assert text.lstrip().startswith("<?xml")
    assert "Orders" in text


def test_read_tds_source_extracts_inner_tds_from_tdsx(tmp_path):
    p = tmp_path / "ds.tdsx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Datasource.tds", LIVE_SQLSERVER)
    p.write_bytes(buf.getvalue())
    text = A._read_tds_source(str(p))
    assert text.lstrip().startswith("<?xml")
    assert "Orders" in text


def test_migrate_datasource_from_tdsx_path(tmp_path):
    p = tmp_path / "Superstore.tdsx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Superstore.tds", TDS_WITH_CALC)
    p.write_bytes(buf.getvalue())
    out = A.migrate_datasource(str(p), model_name="Superstore")
    assert "SUM('Orders'[Sales])" in _all_text(out["parts"])


# == de-default policy: a genuinely-undoable shape returns the storage decision, never a model ===
# DirectLake is OPT-IN ONLY, so the DEFAULT fallback carries NO auto-built landing_plan.

def test_migrate_datasource_fallback_returns_decision_not_model():
    out = A.migrate_datasource(SAPHANA_FALLBACK, model_name="Hana")
    assert out["parts"] == {}                       # no semantic model emitted
    assert out["report"]["fallback"] is True
    assert "landing_plan" not in out["report"]      # DirectLake is opt-in, never auto-built
    assert out["report"]["storage_decision"]["fallback"] == "needs-storage-decision"
    assert "error" not in out["bind"]


def test_migrate_datasource_fallback_writes_no_landing_plan_json(tmp_path):
    dest = str(tmp_path / "out")
    out = A.migrate_datasource(SAPHANA_FALLBACK, model_name="Hana", write_to=dest)
    assert "model_dir" not in out                    # nothing to assemble
    assert "landing_plan_path" not in out            # DirectLake opt-in not taken -> no plan file
    assert not os.path.exists(os.path.join(dest, "Hana.landing_plan.json"))


# == the land-to-Delta + DirectLake capability STAYS, reachable via the explicit opt-in helper ===

def test_directlake_optin_landing_plan_carries_targets_columns_and_calcs():
    # directlake_landing_plan is the Wave-5 opt-in hook; call it directly (as the opt-in would) to
    # cover the plan content that migrate_datasource no longer auto-builds by default.
    plan = A.directlake_landing_plan(
        A.parse_tds(SAPHANA_FALLBACK),
        calcs=[{"name": "Total", "formula": "SUM([Sales])", "role": "measure"}])
    assert plan["target_lakehouse"] == "h1_ultrastore"
    t = {row["source_table"]: row for row in plan["tables"]}["Orders"]
    assert t["delta_table"] == "hana_ds_orders"     # slugified {datasource}_{table} (land-to-Delta naming)
    assert t["connection_class"] == "saphana"
    assert {c["name"] for c in t["columns"]} == {"Order_ID", "Sales"}          # cleaned model names
    assert {c["source_column"] for c in t["columns"]} == {"Order ID", "Sales"}  # raw source names
    assert "VizQL Data Service" in plan["landing_mechanism"]      # snapshot pull on the Tableau PAT
    assert [n["connection_class"] for n in plan["native_cutover"]] == ["saphana"]
    assert plan["calc_inventory"] == [{"name": "Total", "formula": "SUM([Sales])", "role": "measure"}]


def test_directlake_landing_plan_routes_each_table_to_its_own_engine():
    # Standalone helper (the explicit lakehouse OPTION): a multi-connection descriptor lands each
    # table under its own source engine, even though the default policy would rebuild it in place.
    desc = A.parse_tds(MULTI_CONN)
    plan = A.directlake_landing_plan(desc, target_lakehouse="lh_demo")
    assert plan["target_lakehouse"] == "lh_demo"
    by_table = {row["source_table"]: row for row in plan["tables"]}
    assert by_table["SALE"]["connection_class"] == "snowflake"
    assert by_table["DimDate"]["connection_class"] == "sqlserver"
    assert by_table["SALE"]["delta_table"] == "blend_sale"
    assert {n["connection_class"] for n in plan["native_cutover"]} == {"snowflake", "sqlserver"}

