"""Wiring tests: Tableau geo ``semantic-role`` -> Power BI column ``dataCategory``.

Covers the decode core ``tableau_geo_role_to_data_category``, the join builder
``_geo_categories_by_physical``, the additive ``data_category`` parameter on
``generate_column_tmdl``, and the end-to-end flow through ``parse_tds`` + ``emit_table_tmdl_m``
(the M / live-connection column path). A geographic data category lets Power BI map visuals
(e.g. the shapeMap a Tableau filled map migrates to) geocode the column unambiguously.
"""
import xml.etree.ElementTree as ET

from connection_to_m import (
    _choose_datasource,
    _geo_categories_by_physical,
    emit_table_tmdl_m,
    parse_tds,
)
from tmdl_generate import clean_col, generate_column_tmdl, tableau_geo_role_to_data_category

# Geo roles, isolated so the fixture and the assertions stay in sync.
_STATE_ROLE = "semantic-role='[State].[Name]'"
_COUNTRY_ROLE = "semantic-role='[Country].[ISO3166_2]'"

# A structurally faithful live .tds: <cols> logical->physical maps, column metadata-records, and
# authoring <column> elements. STATE carries a State geo role, COUNTRY a Country geo role, REGION
# none (the never-categorize control -- a non-geographic dimension).
_BASE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='GeoTest' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='snow' name='snowflake.1'>
        <connection class='snowflake' dbname='DB' server='x.snowflakecomputing.com' warehouse='' />
      </named-connection>
    </named-connections>
    <relation connection='snowflake.1' name='ORDERS' table='[DB].[PUBLIC].[ORDERS]' type='table' />
    <cols>
      <map key='[STATE]' value='[ORDERS].[STATE]' />
      <map key='[COUNTRY]' value='[ORDERS].[COUNTRY]' />
      <map key='[REGION]' value='[ORDERS].[REGION]' />
    </cols>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>STATE</remote-name><local-name>[STATE]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>COUNTRY</remote-name><local-name>[COUNTRY]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>REGION</remote-name><local-name>[REGION]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='State' datatype='string' name='[STATE]' role='dimension' type='nominal' {state} />
  <column caption='Country' datatype='string' name='[COUNTRY]' role='dimension' type='nominal' {country} />
  <column caption='Region' datatype='string' name='[REGION]' role='dimension' type='nominal' />
</datasource>""".format(state=_STATE_ROLE, country=_COUNTRY_ROLE)

# Same datasource with both geo roles removed -> no column carries a dataCategory.
_STRIPPED = _BASE.replace(_STATE_ROLE, "").replace(_COUNTRY_ROLE, "")

# An extract-shaped datasource: a .hyper extract inlines the physical layer and carries NO
# <cols><map> logical->physical block -- only metadata-records + authoring <column> geo roles.
# The join must fall back to the metadata-record identity so the category still lands.
_EXTRACT = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='GeoExtract' version='18.1'>
  <connection class='sqlproxy'>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>State/Province</remote-name><local-name>[State/Province]</local-name>
        <parent-name>[sqlproxy]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Country/Region</remote-name><local-name>[Country/Region]</local-name>
        <parent-name>[sqlproxy]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[sqlproxy]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='State/Province' datatype='string' name='[State/Province]' role='dimension' type='nominal' semantic-role='[State].[Name]' />
  <column caption='Country/Region' datatype='string' name='[Country/Region]' role='dimension' type='nominal' semantic-role='[Country].[ISO3166_2]' />
  <column caption='Region' datatype='string' name='[Region]' role='dimension' type='nominal' />
</datasource>"""

# An extract-backed federated .tds (excel-direct): the live <cols><map> is DUPLICATED for the
# .hyper cache twin table (<Base>_<hex32>), so every logical id maps to BOTH the base table and its
# object-id twin. The join must collapse the twin (a LOCAL cache of the same logical table, not a
# second upstream) instead of reading the base+twin pair as a false ambiguity and dropping the geo
# category -- the real-world Superstore extract case the len>1 ambiguity guard silently dropped.
_TWIN = "Orders_ABCDEF0123456789ABCDEF0123456789"
_EXTRACT_TWIN = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='GeoTwin' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='xl' name='excel.1'>
        <connection class='excel-direct' filename='Superstore.xlsx' />
      </named-connection>
    </named-connections>
    <relation connection='excel.1' name='Orders' table='[Orders]' type='table' />
    <cols>
      <map key='[State/Province]' value='[Orders].[State/Province]' />
      <map key='[Region]' value='[Orders].[Region]' />
      <map key='[State/Province]' value='[{twin}].[State/Province]' />
      <map key='[Region]' value='[{twin}].[Region]' />
    </cols>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>State/Province</remote-name><local-name>[State/Province]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='State/Province' datatype='string' name='[State/Province]' role='dimension' type='nominal' semantic-role='[State].[Name]' />
  <column caption='Region' datatype='string' name='[Region]' role='dimension' type='nominal' />
</datasource>""".format(twin=_TWIN)


def _geo_map(xml):
    return _geo_categories_by_physical(_choose_datasource(ET.fromstring(xml), None))


def _orders_relation(d):
    return next(r for r in d["relations"] if r.get("name") == "ORDERS")


# -- tableau_geo_role_to_data_category (the decode core) -----------------------
def test_decode_maps_each_supported_geo_area():
    assert tableau_geo_role_to_data_category("[State].[Name]") == "StateOrProvince"
    assert tableau_geo_role_to_data_category("[Country].[ISO3166_2]") == "Country"
    assert tableau_geo_role_to_data_category("[City].[Name]") == "City"
    assert tableau_geo_role_to_data_category("[County].[Name]") == "County"
    assert tableau_geo_role_to_data_category("[ZipCode].[Name]") == "PostalCode"


def test_decode_returns_none_for_points_and_unknowns():
    # Generated lat/lon point roles and any non-mappable area carry no category.
    assert tableau_geo_role_to_data_category("[Latitude].[Name]") is None
    assert tableau_geo_role_to_data_category("[Longitude].[Name]") is None
    assert tableau_geo_role_to_data_category("[CBSA/MSA].[Name]") is None
    assert tableau_geo_role_to_data_category("[Area Code].[Name]") is None
    assert tableau_geo_role_to_data_category("not-a-role") is None
    assert tableau_geo_role_to_data_category("") is None
    assert tableau_geo_role_to_data_category(None) is None


# -- _geo_categories_by_physical (the lid -> physical join builder) ------------
def test_join_keys_geo_category_by_physical_identity():
    m = _geo_map(_BASE)
    assert m[("ORDERS", clean_col("STATE"))] == "StateOrProvince"
    assert m[("ORDERS", clean_col("COUNTRY"))] == "Country"


def test_join_omits_column_without_geo_role():
    # REGION has no semantic-role -> never categorized (it is a plain dimension).
    assert ("ORDERS", clean_col("REGION")) not in _geo_map(_BASE)


def test_join_omits_unmapped_geo_area():
    # A geo role with no faithful Power BI category (Area Code) is skipped entirely.
    xml = _BASE.replace(_COUNTRY_ROLE, "semantic-role='[Area Code].[Name]'")
    assert ("ORDERS", clean_col("COUNTRY")) not in _geo_map(xml)


def test_join_omits_unmapped_logical_id():
    # A geo <column> whose lid has NO <cols> map entry is skipped (no physical to key on).
    extra = ("  <column caption='Orphan' datatype='string' name='[ORPHAN]' role='dimension' "
             "type='nominal' semantic-role='[City].[Name]' />\n</datasource>")
    assert "City" not in _geo_map(_BASE.replace("</datasource>", extra)).values()


def test_join_omits_ambiguously_mapped_logical_id():
    # Same lid mapped to two physical columns -> fail closed, never guess which.
    xml = _BASE.replace(
        "<map key='[STATE]' value='[ORDERS].[STATE]' />",
        "<map key='[STATE]' value='[ORDERS].[STATE]' />\n"
        "      <map key='[STATE]' value='[ORDERS].[REGION]' />")
    assert ("ORDERS", clean_col("STATE")) not in _geo_map(xml)


def test_join_empty_when_no_geo_roles_anywhere():
    assert _geo_map(_STRIPPED) == {}


def test_join_falls_back_to_metadata_identity_for_extracts():
    # Extract with no <cols><map>: the geo category must still resolve, via the metadata-record
    # identity (parent, clean_col(remote)) the column emitter keys on. This is the real-world
    # .hyper case the <cols><map>-only join silently dropped.
    m = _geo_map(_EXTRACT)
    assert m[("sqlproxy", clean_col("State/Province"))] == "StateOrProvince"
    assert m[("sqlproxy", clean_col("Country/Region"))] == "Country"
    assert ("sqlproxy", clean_col("Region")) not in m  # no geo role -> never categorized


def test_join_collapses_object_id_extract_twin():
    # Excel-direct extract: [State/Province] maps to BOTH [Orders] and its .hyper object-id twin
    # [Orders_<hex32>] (a local cache of the same table). The base+twin pair must collapse to ONE
    # identity so the category lands under the base table -- not be dropped as a false ambiguity.
    # This is the real-world Superstore .tdsx case the len>1 guard silently dropped.
    m = _geo_map(_EXTRACT_TWIN)
    assert m[("Orders", clean_col("State/Province"))] == "StateOrProvince"
    assert ("Orders", clean_col("Region")) not in m  # no geo role -> never categorized


def test_join_collapsed_twin_emits_data_category_end_to_end():
    # Full path: parse_tds -> relation columns -> emit_table_tmdl_m emits the category for the
    # twin-collapsed extract, the way the Superstore Import model now does.
    d = parse_tds(_EXTRACT_TWIN)
    rel = next(r for r in d["relations"] if r.get("name") == "Orders")
    assert "dataCategory: StateOrProvince" in emit_table_tmdl_m(rel, d, "Import")


def test_join_still_fails_closed_on_distinct_base_tables():
    # Twin-collapse strips ONLY the object-id hash: two GENUINELY different base tables stay
    # ambiguous and the category is dropped (never guess across real upstreams).
    xml = _EXTRACT_TWIN.replace(
        "value='[{0}].[State/Province]'".format(_TWIN),
        "value='[People].[State/Province]'")
    assert ("Orders", clean_col("State/Province")) not in _geo_map(xml)


# -- generate_column_tmdl(data_category=...) — the additive serializer param ----
def test_generate_column_tmdl_no_category_omits_line():
    # Omitting the new arg and passing None both leave the column without a dataCategory.
    assert "dataCategory" not in generate_column_tmdl("State", "string", "none", False)
    assert "dataCategory" not in generate_column_tmdl("State", "string", "none", False, None, None)


def test_generate_column_tmdl_emits_data_category():
    out = generate_column_tmdl("State", "string", "none", False, None, "StateOrProvince")
    assert "\t\tdataCategory: StateOrProvince\n" in out
    # placed within the column block, before the trailing annotation
    assert out.index("dataCategory") < out.index("SummarizationSetBy")


# -- end-to-end: parse_tds -> relation columns -> emit_table_tmdl_m ------------
def test_parse_tds_attaches_data_category_to_columns():
    cols = {c["model_name"]: c for c in _orders_relation(parse_tds(_BASE))["columns"]}
    assert cols[clean_col("STATE")]["data_category"] == "StateOrProvince"
    assert cols[clean_col("COUNTRY")]["data_category"] == "Country"
    assert "data_category" not in cols[clean_col("REGION")]


def test_emit_table_tmdl_m_applies_geo_data_category():
    d = parse_tds(_BASE)
    tmdl = emit_table_tmdl_m(_orders_relation(d), d, "DirectQuery")
    assert "dataCategory: StateOrProvince" in tmdl   # State geocodes as a state/province
    assert "dataCategory: Country" in tmdl           # Country geocodes as a country


def test_emit_table_tmdl_m_unchanged_without_geo_roles():
    # Never-regress: with the roles stripped, no column carries a dataCategory.
    d = parse_tds(_STRIPPED)
    tmdl = emit_table_tmdl_m(_orders_relation(d), d, "DirectQuery")
    assert "dataCategory" not in tmdl
