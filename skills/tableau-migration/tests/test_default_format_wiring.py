"""Wiring tests: Tableau ``<column @default-format>`` -> Power BI column formatString.

Covers the join builder ``_default_formats_by_physical``, the additive ``format_string``
parameter on ``generate_column_tmdl``, and the end-to-end flow through ``parse_tds`` +
``emit_table_tmdl_m`` (the M / live-connection column path). The decode core itself
(``tableau_default_format_to_pbi``) is exercised by ``test_default_format_decode.py``.
"""
import xml.etree.ElementTree as ET

from connection_to_m import (
    _choose_datasource,
    _default_formats_by_physical,
    emit_table_tmdl_m,
    parse_tds,
)
from tmdl_generate import clean_col, generate_column_tmdl

# The two explicit codes, isolated so the fixture and the strip-helpers stay in sync.
_SALES_FMT = "default-format='c\"$\"#,##0.00;(\"$\"#,##0.00)'"
_POSTAL_FMT = "default-format='*00000'"

# A structurally faithful live-Snowflake .tds: <cols> logical->physical maps, column
# metadata-records, and authoring <column> elements. SALES carries an explicit currency
# default-format, POSTAL a zero-pad, REGION none (the never-regress control).
_BASE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='FmtTest' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='snow' name='snowflake.1'>
        <connection class='snowflake' dbname='DB' server='x.snowflakecomputing.com' warehouse='' />
      </named-connection>
    </named-connections>
    <relation connection='snowflake.1' name='ORDERS' table='[DB].[PUBLIC].[ORDERS]' type='table' />
    <cols>
      <map key='[SALES]' value='[ORDERS].[SALES]' />
      <map key='[REGION]' value='[ORDERS].[REGION]' />
      <map key='[POSTAL]' value='[ORDERS].[POSTAL]' />
    </cols>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>SALES</remote-name><local-name>[SALES]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>REGION</remote-name><local-name>[REGION]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>POSTAL</remote-name><local-name>[POSTAL]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>integer</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Sales' datatype='real' name='[SALES]' role='measure' type='quantitative' {sales} />
  <column caption='Region' datatype='string' name='[REGION]' role='dimension' type='nominal' />
  <column caption='Postal' datatype='integer' name='[POSTAL]' role='dimension' type='ordinal' {postal} />
</datasource>""".format(sales=_SALES_FMT, postal=_POSTAL_FMT)

# Same datasource with both explicit codes removed -> every column falls back to its floor.
_STRIPPED = _BASE.replace(_SALES_FMT, "").replace(_POSTAL_FMT, "")


def _fmt_map(xml):
    return _default_formats_by_physical(_choose_datasource(ET.fromstring(xml), None))


def _orders_relation(d):
    return next(r for r in d["relations"] if r.get("name") == "ORDERS")


# -- _default_formats_by_physical (the lid -> physical join builder) -----------
def test_join_decodes_and_keys_by_physical_identity():
    m = _fmt_map(_BASE)
    assert m[("ORDERS", clean_col("SALES"))] == '"$"#,##0.00;("$"#,##0.00)'
    assert m[("ORDERS", clean_col("POSTAL"))] == "00000"


def test_join_omits_column_without_default_format():
    # REGION has no default-format -> never in the map (its floor is kept downstream).
    assert ("ORDERS", clean_col("REGION")) not in _fmt_map(_BASE)


def test_join_omits_undecodable_code():
    xml = _BASE.replace(_POSTAL_FMT, "default-format='zzz-not-a-format'")
    assert ("ORDERS", clean_col("POSTAL")) not in _fmt_map(xml)


def test_join_omits_unmapped_logical_id():
    # A <column> whose lid has NO <cols> map entry is skipped (no physical to key on).
    extra = ("  <column caption='Orphan' datatype='real' name='[ORPHAN]' role='measure' "
             "type='quantitative' default-format='p0%' />\n</datasource>")
    m = _fmt_map(_BASE.replace("</datasource>", extra))
    assert "0%" not in m.values()


def test_join_omits_ambiguously_mapped_logical_id():
    # Same lid mapped to two physical columns -> fail closed, never guess which.
    xml = _BASE.replace(
        "<map key='[SALES]' value='[ORDERS].[SALES]' />",
        "<map key='[SALES]' value='[ORDERS].[SALES]' />\n"
        "      <map key='[SALES]' value='[ORDERS].[REGION]' />")
    assert ("ORDERS", clean_col("SALES")) not in _fmt_map(xml)


def test_join_empty_when_no_default_formats_anywhere():
    assert _fmt_map(_STRIPPED) == {}


# -- generate_column_tmdl(format_string=...) — the additive serializer param ----
def test_generate_column_tmdl_no_format_keeps_floor():
    # Omitting the new arg and passing None both keep the type-derived floor.
    assert "formatString: #,0.00" in generate_column_tmdl("Sales", "double", "sum", False)
    assert "formatString: #,0.00" in generate_column_tmdl("Sales", "double", "sum", False, None)


def test_generate_column_tmdl_explicit_format_overrides_floor():
    out = generate_column_tmdl("Sales", "double", "sum", False, '"$"#,##0.00')
    assert 'formatString: "$"#,##0.00' in out
    assert "formatString: #,0.00" not in out


def test_generate_column_tmdl_explicit_format_applies_where_floor_is_empty():
    # A string column has no type-derived floor; an explicit format still lands.
    assert "formatString: 00000" in generate_column_tmdl("Postal", "string", "none", False, "00000")


# -- end-to-end: parse_tds -> relation columns -> emit_table_tmdl_m ------------
def test_parse_tds_attaches_format_string_to_columns():
    cols = {c["model_name"]: c for c in _orders_relation(parse_tds(_BASE))["columns"]}
    assert cols[clean_col("SALES")]["format_string"] == '"$"#,##0.00;("$"#,##0.00)'
    assert cols[clean_col("POSTAL")]["format_string"] == "00000"
    assert "format_string" not in cols[clean_col("REGION")]


def test_emit_table_tmdl_m_applies_decoded_default_format():
    d = parse_tds(_BASE)
    tmdl = emit_table_tmdl_m(_orders_relation(d), d, "DirectQuery")
    assert 'formatString: "$"#,##0.00;("$"#,##0.00)' in tmdl   # currency survived
    assert "formatString: 00000" in tmdl                        # zero-pad survived
    assert "formatString: #,0.00\n" not in tmdl                 # SALES floor overridden
    assert "formatString: #,0\n" not in tmdl                    # POSTAL floor overridden


def test_emit_table_tmdl_m_unchanged_without_default_format():
    # Never-regress: with the codes stripped, every column keeps its type-derived floor.
    d = parse_tds(_STRIPPED)
    tmdl = emit_table_tmdl_m(_orders_relation(d), d, "DirectQuery")
    assert '"$"' not in tmdl                       # no currency override leaked in
    assert "formatString: 00000" not in tmdl       # no zero-pad override
    assert "formatString: #,0.00\n" in tmdl        # SALES keeps its double-sum floor
    assert "formatString: #,0\n" in tmdl           # POSTAL keeps its int64 floor
