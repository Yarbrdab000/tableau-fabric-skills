"""Model Object Harvest tests: Tableau Groups and numeric Bins -> calc columns.

Covers the two "model object" kinds the core rebuild did not previously emit, end to end:

* **Groups** (``<calculation class='categorical-bin'>``) -> a ``SWITCH(TRUE(), <col> IN {..},
  "label", .., <tail>)`` string calc column, where the tail is the base column itself when the
  group passes unlisted values through (``new-bin='true'``) else ``BLANK()``.
* **numeric Bins** (``<calculation class='bin'>``) -> an ``INT((<col> - peg) / size) * size + peg``
  calc column (``INT`` floors toward negative infinity, matching Tableau's bin flooring). The width
  is a literal ``size=`` or the DEFAULT of a referenced ``size-parameter``; an unresolvable width
  leaves an inert ``= BLANK()`` stub + a skip reason (a width is NEVER assumed).

The fixtures are original, trimmed-but-structurally-faithful ``.tds`` documents on a live SQL
Server connection (so the model rebuilds as Import/DirectQuery, not the Delta fallback).
"""
import tmdl_generate as T
from assemble_model import migrate_tds_to_semantic_model


# -- fixtures ------------------------------------------------------------------
# Orders on live SQL Server carrying: a categorical-bin GROUP over [Product Name]
# (new-bin='true' => unlisted values pass through as themselves), a numeric BIN with a
# literal width, a numeric BIN whose width comes from a parameter DEFAULT, and a numeric
# BIN whose size-parameter does not exist (=> inert stub).
GROUP_BIN_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.0'>
        <connection class='sqlserver' dbname='Superstore' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'><remote-name>Product Name</remote-name>
        <local-name>[Product Name]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Profit</remote-name>
        <local-name>[Profit]</local-name><parent-name>[Orders]</parent-name><local-type>real</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Sales</remote-name>
        <local-name>[Sales]</local-name><parent-name>[Orders]</parent-name><local-type>real</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Quantity</remote-name>
        <local-name>[Quantity]</local-name><parent-name>[Orders]</parent-name><local-type>integer</local-type></metadata-record>
    </metadata-records>
  </connection>
  <column caption='Bin Size' name='[Parameter 1]' datatype='integer' param-domain-type='list' value='100'>
    <members>
      <member value='50' />
      <member value='100' />
      <member value='200' />
    </members>
  </column>
  <column caption='Manufacturer' datatype='string' name='[Product Name (group)]' role='dimension' type='nominal'>
    <calculation class='categorical-bin' column='[Product Name]' new-bin='true'>
      <bin default-name='false' value='&quot;3M&quot;'>
        <value>&quot;3M Hangers Command Adhesive&quot;</value>
        <value>&quot;3M Office Air Cleaner&quot;</value>
      </bin>
      <bin default-name='false' value='&quot;Acme&quot;'>
        <value>&quot;Acme Trimmer&quot;</value>
      </bin>
    </calculation>
  </column>
  <column caption='Profit (bin)' datatype='integer' name='[Profit (bin)]' role='dimension' type='ordinal'>
    <calculation class='bin' decimals='0' formula='[Profit]' peg='0' size='200' />
  </column>
  <column caption='Sales (bin)' datatype='integer' name='[Sales (bin)]' role='dimension' type='ordinal'>
    <calculation class='bin' decimals='0' formula='[Sales]' peg='0' size-parameter='[Parameters].[Parameter 1]' />
  </column>
  <column caption='Quantity (bin)' datatype='integer' name='[Quantity (bin)]' role='dimension' type='ordinal'>
    <calculation class='bin' decimals='0' formula='[Quantity]' peg='0' size-parameter='[Parameters].[Nonexistent]' />
  </column>
</datasource>"""

# A plain datasource with NO groups/bins: the harvest must be a no-op here.
PLAIN_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Plain' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.1'>
        <connection class='sqlserver' dbname='Plain' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.1' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'><remote-name>Sales</remote-name>
        <local-name>[Sales]</local-name><parent-name>[Orders]</parent-name><local-type>real</local-type></metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


def _orders(out):
    return out["parts"]["definition/tables/Orders.tmdl"]


# -- parsing -------------------------------------------------------------------
def test_parse_model_objects_extracts_groups_and_bins():
    parsed = T.parse_model_objects(GROUP_BIN_TDS)

    assert parsed["groups"] == [{
        "name": "Manufacturer",
        "base": "Product Name",
        "include_others_as_self": True,
        "members": [("3M", ["3M Hangers Command Adhesive", "3M Office Air Cleaner"]),
                    ("Acme", ["Acme Trimmer"])],
    }]
    names = {b["name"] for b in parsed["bins"]}
    assert names == {"Profit (bin)", "Sales (bin)", "Quantity (bin)"}
    profit = next(b for b in parsed["bins"] if b["name"] == "Profit (bin)")
    assert profit["base"] == "Profit" and profit["size_literal"] == "200" and profit["peg"] == "0"
    sales = next(b for b in parsed["bins"] if b["name"] == "Sales (bin)")
    assert sales["size_parameter"] == "Parameter 1"


def test_parse_model_objects_no_groups_or_bins_on_plain_tds():
    parsed = T.parse_model_objects(PLAIN_TDS)
    assert parsed["groups"] == [] and parsed["bins"] == []


# -- Groups (§3.1) -------------------------------------------------------------
def test_group_becomes_switch_calc_column():
    out = migrate_tds_to_semantic_model(GROUP_BIN_TDS, model_name="Superstore")
    orders = _orders(out)

    assert "column Manufacturer =" in orders
    assert "SWITCH(" in orders
    assert '\'Orders\'[Product_Name] IN { "3M Hangers Command Adhesive", "3M Office Air Cleaner" }, "3M"' in orders
    assert '\'Orders\'[Product_Name] IN { "Acme Trimmer" }, "Acme"' in orders
    # new-bin='true' => the tail is the base column (unlisted values map to themselves), not BLANK()
    manu = orders.split("column Manufacturer")[1].split("\n\n")[0]
    # the bare base ref on its own line is the SWITCH tail (IN-clause refs are followed by " IN {")
    assert "'Orders'[Product_Name]\n" in manu and "BLANK()" not in manu
    assert "dataType: string" in manu
    assert "annotation TableauFormula = GROUP([Product Name]" in orders

    groups = out["report"]["model_objects"]["groups"]
    assert groups["emitted"] == ["Manufacturer"] and groups["skipped"] == []


def test_group_tail_is_blank_when_not_pass_through():
    tds = GROUP_BIN_TDS.replace("class='categorical-bin' column='[Product Name]' new-bin='true'",
                                "class='categorical-bin' column='[Product Name]'")
    out = migrate_tds_to_semantic_model(tds, model_name="Superstore")
    block = _orders(out).split("column Manufacturer")[1].split("\n\n")[0]
    # no pass-through -> the SWITCH tail is BLANK(), and the base column is NOT the fallback
    assert "BLANK()" in block


# -- numeric Bins (§3.2) -------------------------------------------------------
def test_numeric_bin_with_literal_width_becomes_int_calc_column():
    out = migrate_tds_to_semantic_model(GROUP_BIN_TDS, model_name="Superstore")
    orders = _orders(out)

    assert "column 'Profit (bin)' = INT(('Orders'[Profit] - 0) / 200) * 200 + 0" in orders
    block = orders.split("column 'Profit (bin)'")[1].split("column")[0]
    assert "dataType: int64" in block
    assert "formatString:" in block
    assert "annotation TableauFormula = BIN([Profit], size=200" in orders

    bins = out["report"]["model_objects"]["bins"]
    assert "Profit (bin)" in bins["emitted"]


def test_numeric_bin_width_from_parameter_default():
    out = migrate_tds_to_semantic_model(GROUP_BIN_TDS, model_name="Superstore")
    orders = _orders(out)

    # width 100 comes from the referenced parameter's DEFAULT value
    assert "column 'Sales (bin)' = INT(('Orders'[Sales] - 0) / 100) * 100 + 0" in orders
    bins = out["report"]["model_objects"]["bins"]
    assert "Sales (bin)" in bins["emitted"]
    note = next((n for n in bins["notes"] if n["name"] == "Sales (bin)"), None)
    assert note is not None and "parameter default" in note["note"]


def test_numeric_bin_missing_size_parameter_is_inert_stub_and_skipped():
    out = migrate_tds_to_semantic_model(GROUP_BIN_TDS, model_name="Superstore")
    orders = _orders(out)

    # the unresolved-width bin lands as an inert BLANK() stub (never assumes a width) ...
    assert "column 'Quantity (bin)' = BLANK()" in orders
    assert "INT(('Orders'[Quantity]" not in orders
    assert "annotation TableauFormula = BIN([Quantity], size=?, peg=0)" in orders
    # ... and is recorded as skipped with an honest reason
    bins = out["report"]["model_objects"]["bins"]
    assert "Quantity (bin)" not in bins["emitted"]
    skip = next((s for s in bins["skipped"] if s["name"] == "Quantity (bin)"), None)
    assert skip is not None and "not found" in skip["reason"]


# -- backward compatibility / no-op --------------------------------------------
def test_harvest_is_a_pure_no_op_without_groups_or_bins():
    out = migrate_tds_to_semantic_model(PLAIN_TDS, model_name="Plain")
    orders = _orders(out)
    assert "SWITCH(" not in orders
    assert "INT((" not in orders
    mo = out["report"]["model_objects"]
    assert mo["groups"]["emitted"] == [] and mo["bins"]["emitted"] == []
    assert mo["groups"]["skipped"] == [] and mo["bins"]["skipped"] == []
