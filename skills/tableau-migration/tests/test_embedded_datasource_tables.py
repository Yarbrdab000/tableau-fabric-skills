"""Workbook-path telemetry names its tables, not just counts them (#182).

THE ASYMMETRY. The datasource path's ``ds_details`` has always emitted
``tables: ["FACT_ORDERS", "DIM_CUSTOMER", ...]``; the workbook path's
``embedded_datasources`` emitted only ``table_count``. Everything needed for the list was already
computed to produce the count -- ``len(tables)`` threw the names away one line after building them.

WHY A COUNT IS NOT A SMALL VERSION OF A LIST. A workbook model that mixes a preserved connector with
one materialised table can only be reported *"cannot attribute -- inspect by hand"*, because nothing
says which table belonged to which source. The identical shape on the datasource path yields a
precise per-table verdict. The reporter ran a blind review of their own fidelity checker against
2.339.0 output and found **three separate false passes**, all on the workbook path, all traceable to
having a count where the sibling path gives names.

THE PROPERTY THAT MAKES IT USEFUL IS THE JOIN, NOT THE PRESENCE. The names come from
``_table_display`` -- the same function the emitted TMDL filename is derived from -- so they match
the model with no mapping step. A list that needed translating would be a count with extra work.
``test_the_names_join_to_the_emitted_tmdl_filenames`` asserts that against a real build rather than
asserting the names look plausible.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from assemble_model import migrate_tds_to_semantic_model  # noqa: E402
from connection_to_m import workbook_datasources  # noqa: E402
from migrate_estate import _embedded_datasource_telemetry  # noqa: E402

_WB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook>
 <datasources>
  <datasource caption='Star' name='federated.abc' inline='true'>
    <connection class='federated'>
      <named-connections><named-connection caption='a' name='snowflake.0a1b'>
        <connection class='snowflake' dbname='MERIDIAN' schema='SALES' warehouse='WH'
                    server='acme.snowflakecomputing.com' username='svc' />
      </named-connection></named-connections>
      <relation connection='snowflake.0a1b' join='inner' type='join'>
        <clause type='join'><expression op='='>
          <expression op='[FACT_ORDERS].[CUSTOMER_KEY]' />
          <expression op='[DIM_CUSTOMER].[CUSTOMER_KEY]' />
        </expression></clause>
        <relation connection='snowflake.0a1b' name='FACT_ORDERS' table='[SALES].[FACT_ORDERS]' type='table' />
        <relation connection='snowflake.0a1b' name='DIM_CUSTOMER' table='[SALES].[DIM_CUSTOMER]' type='table' />
      </relation>
      <metadata-records>
        <metadata-record class='column'><remote-name>ORDER_KEY</remote-name>
          <local-name>[ORDER_KEY]</local-name><parent-name>[FACT_ORDERS]</parent-name>
          <local-type>string</local-type></metadata-record>
        <metadata-record class='column'><remote-name>REGION</remote-name>
          <local-name>[REGION]</local-name><parent-name>[DIM_CUSTOMER]</parent-name>
          <local-type>string</local-type></metadata-record>
      </metadata-records>
    </connection>
  </datasource>
 </datasources>
</workbook>"""


def test_the_inventory_names_its_tables():
    inv = workbook_datasources(_WB)
    assert len(inv) == 1, inv
    assert sorted(inv[0]["tables"]) == ["DIM_CUSTOMER", "FACT_ORDERS"]


def test_the_count_and_the_list_can_never_disagree():
    """They are derived from one expression. A consumer that trusted the count and re-derived the
    list would otherwise have two answers to one question."""
    for d in workbook_datasources(_WB):
        assert d["table_count"] == len(d["tables"])


def test_the_names_join_to_the_emitted_tmdl_filenames():
    """THE POINT. The datasource path's list is usable because it matches the model directly. A
    list that needed a mapping step would be a count with extra work."""
    names = workbook_datasources(_WB)[0]["tables"]
    parts = migrate_tds_to_semantic_model(_WB, model_name="Star")["parts"]
    emitted = {p.split("/")[-1][: -len(".tmdl")] for p in parts
               if p.startswith("definition/tables/")}
    assert names, "vacuous -- no names to join"
    assert not [n for n in names if n not in emitted], \
        "telemetry names %s do not appear among emitted tables %s" % (sorted(names), sorted(emitted))


def test_the_telemetry_block_carries_the_names_through():
    """The inventory is one hop from what ``report.json`` actually publishes; a field added to the
    first and dropped by the second would satisfy every test above and change nothing downstream."""
    rows = _embedded_datasource_telemetry(_WB, workbook_datasources(_WB))
    assert rows and sorted(rows[0]["tables"]) == ["DIM_CUSTOMER", "FACT_ORDERS"]


def test_the_key_is_present_even_when_empty():
    """Absent and empty must not look the same: a consumer has to tell 'no tables' from 'this
    engine predates the key', which is the same tri-state discipline #141/#183 exist for."""
    rows = _embedded_datasource_telemetry("<workbook/>", [{"label": "X", "caption": "X"}])
    assert rows and rows[0]["tables"] == []


def test_the_existing_fields_are_unchanged():
    """Strictly additive -- every field a 2.339.0 consumer reads must still be there."""
    row = _embedded_datasource_telemetry(_WB, workbook_datasources(_WB))[0]
    for k in ("caption", "label", "connection_class", "named_connection_count",
              "table_count", "connections"):
        assert k in row, k
