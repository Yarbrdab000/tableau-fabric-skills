"""Regression: M (Import / DirectQuery) columns must NOT carry a ``sourceLineageTag``.

``sourceLineageTag`` declares that a column is bound to a physical source-system schema --
the DirectLake / DirectQuery-over-a-real-schema lineage identifier. The M table path
(``emit_table_tmdl_m`` -> ``generate_column_tmdl``) builds Import (and M-DirectQuery) tables
whose columns are bound by Power Query through ``sourceColumn``, not by a source schema. When a
bogus ``sourceLineageTag`` is emitted on such a column, Power BI Desktop treats the column
binding as speculative and DROPS relationships into the table on the FIRST refresh (e.g. the
``orders[Order_Date] -> Date[Date]`` join silently disappears). Verified in Desktop: an Import
model with the tag drops the date relationship; the same model with the tag stripped survives,
matching Desktop's own canonical form (it strips these tags when it rewrites the table).

These tests pin the omission so it is never reintroduced. The legitimate DirectLake source
lineage is emitted by a different path (``generate_table_tmdl``) and is unaffected.
"""
from connection_to_m import emit_table_tmdl_m, parse_tds
from tmdl_generate import clean_col, generate_column_tmdl

# A structurally faithful live-Snowflake .tds with an ORDERS relation: an ORDER_DATE dateTime
# (the column whose downstream relationship is the one that drops), an ORDER_ID string, and a
# SALES measure. The M path types these columns from this metadata, deterministically.
_BASE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='LineageTest' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='snow' name='snowflake.1'>
        <connection class='snowflake' dbname='DB' server='x.snowflakecomputing.com' warehouse='' />
      </named-connection>
    </named-connections>
    <relation connection='snowflake.1' name='ORDERS' table='[DB].[PUBLIC].[ORDERS]' type='table' />
    <cols>
      <map key='[ORDER_DATE]' value='[ORDERS].[ORDER_DATE]' />
      <map key='[ORDER_ID]' value='[ORDERS].[ORDER_ID]' />
      <map key='[SALES]' value='[ORDERS].[SALES]' />
    </cols>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>ORDER_DATE</remote-name><local-name>[ORDER_DATE]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>datetime</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>ORDER_ID</remote-name><local-name>[ORDER_ID]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>SALES</remote-name><local-name>[SALES]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Order Date' datatype='datetime' name='[ORDER_DATE]' role='dimension' type='ordinal' />
  <column caption='Order Id' datatype='string' name='[ORDER_ID]' role='dimension' type='nominal' />
  <column caption='Sales' datatype='real' name='[SALES]' role='measure' type='quantitative' />
</datasource>"""


def _orders_relation(d):
    return next(r for r in d["relations"] if r.get("name") == "ORDERS")


# -- unit: generate_column_tmdl (where the omission lives) ---------------------
def test_generate_column_tmdl_omits_source_lineage_tag():
    # Across types / summarize / hidden / explicit format, a sourceLineageTag is NEVER emitted.
    for args in (
        ("Order_Date", "dateTime", "none", False),
        ("Sales", "double", "sum", False),
        ("Order_ID", "string", "none", True),
    ):
        out = generate_column_tmdl(*args)
        assert "sourceLineageTag" not in out


def test_generate_column_tmdl_keeps_binding_and_identity():
    # The real Power Query binding (sourceColumn) and the column identity (lineageTag) stay --
    # only the speculative source-schema lineage is gone.
    out = generate_column_tmdl("Order_Date", "dateTime", "none", False)
    assert "\t\tsourceColumn: Order_Date\n" in out
    assert "\t\tlineageTag: " in out


# -- end-to-end: parse_tds -> emit_table_tmdl_m (the M column path) ------------
def test_emit_table_tmdl_m_has_no_source_lineage_tag():
    d = parse_tds(_BASE)
    tmdl = emit_table_tmdl_m(_orders_relation(d), d, "DirectQuery")
    assert "sourceLineageTag" not in tmdl
    # The columns are still present and bound (sourceColumn), so the table is fully typed.
    assert f"sourceColumn: {clean_col('ORDER_DATE')}" in tmdl
    assert f"column {clean_col('ORDER_DATE')}" in tmdl


# -- unit: _source_column_value quoting rule ----------------------------------
def test_source_column_value_quotes_only_non_simple_names():
    from tmdl_generate import _source_column_value
    # simple identifiers (incl. hyphen) stay bare -- byte-identical to a plain sourceColumn today
    assert _source_column_value("Sales") == "Sales"
    assert _source_column_value("Order_Date") == "Order_Date"
    assert _source_column_value("Sub-Category") == "Sub-Category"
    # spaces / slashes / other specials must be double-quoted or the model won't bind
    assert _source_column_value("Order Date") == '"Order Date"'
    assert _source_column_value("Country/Region") == '"Country/Region"'
    assert _source_column_value("Net & Gross") == '"Net & Gross"'
    # an embedded double-quote is doubled inside the quoted form
    assert _source_column_value('Weird"Name') == '"Weird""Name"'


# -- unit: generate_column_tmdl source_column binding (the Fabric fold-safe fix) ----
def test_generate_column_tmdl_binds_raw_quoted_source_column():
    # A spaced raw source name binds via a QUOTED sourceColumn while the model column NAME stays the
    # underscored identifier -- so DAX/visual bindings are unaffected and no M rename is needed.
    out = generate_column_tmdl("Order_Date", "dateTime", "none", False,
                               source_column="Order Date")
    assert "\tcolumn Order_Date\n" in out
    assert '\t\tsourceColumn: "Order Date"\n' in out
    assert "sourceColumn: Order_Date" not in out          # NOT the underscored name


def test_generate_column_tmdl_source_column_simple_stays_bare():
    # A simple raw name emits a bare sourceColumn (== the underscored model name here).
    out = generate_column_tmdl("Sales", "double", "sum", False, source_column="Sales")
    assert "\t\tsourceColumn: Sales\n" in out


def test_generate_column_tmdl_default_source_column_is_byte_identical():
    # Omitting source_column (the DirectLake path + any caller that hasn't opted in) is byte-for-byte
    # unchanged from before the parameter existed: sourceColumn falls back to the column name.
    # (lineageTag is a fresh GUID per call, so normalize it before comparing.)
    import re as _re
    def _norm(s):
        return _re.sub(r"lineageTag: [0-9a-f-]+", "lineageTag: <id>", s)
    with_none = generate_column_tmdl("Order_Date", "dateTime", "none", False, source_column=None)
    without = generate_column_tmdl("Order_Date", "dateTime", "none", False)
    assert _norm(with_none) == _norm(without)
    assert "\t\tsourceColumn: Order_Date\n" in with_none
