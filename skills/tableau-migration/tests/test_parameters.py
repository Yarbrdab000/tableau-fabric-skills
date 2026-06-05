"""Tableau parameter -> Power BI translation tests (Stream H).

Offline / stdlib / deterministic: every fixture is inline XML, no network or secrets. The two
real-customer fixtures (Facility Name list, Spider / Buffer integer list) are baked in verbatim;
synthetic numeric-range, real-range and date-range fixtures cover the remaining Tier-1 shapes plus
the storage-mode and guardrail contract.
"""
import pytest

from parameters import (
    CLASS_CALC_COLUMN,
    CLASS_MANUAL_UNBOUNDED,
    CLASS_MEASURE_VALUE,
    CLASS_TOP_N,
    CLASS_VISUAL_FILTER,
    STRAT_DEFAULT_ONLY,
    classify_parameter,
    emit_parameter,
    extract_parameters,
    param_order_column,
    param_ref_name,
    param_slicer_column,
    param_table_name,
    param_table_tmdl,
    param_value_column,
    param_value_measure,
)

# -- real-customer fixtures (verbatim) ----------------------------------------
_FACILITY_MEMBERS = [
    "Altran Clinic",
    "Blake View Hospital",
    "Grace Doctors Group",
    "Heart Life Clinic",
    "New Rochelle Health Clinic",
    "New York State Hospital",
    "Park Planion Medical Center",
    "St. Johns Hospital",
]


def _facility_members_xml():
    return "".join('<member value=\'"%s"\' />' % m for m in _FACILITY_MEMBERS)


# A realistic synthetic Parameters datasource carrying both real fixtures plus synthetic ranges.
PARAMS_XML = (
    "<workbook>"
    "<datasources>"
    "<datasource name='Parameters' hasconnection='false' inline='true'>"
    # 1. Facility Name Parameter -- string list (real).
    "<column caption='Facility Name Parameter' name='[Facility Name Parameter]' "
    "datatype='string' param-domain-type='list' value='\"New York State Hospital\"'>"
    "<calculation class='tableau' formula='\"New York State Hospital\"' />"
    "<members>" + _facility_members_xml() + "</members>"
    "</column>"
    # 2. Spider / Buffer -- integer list (real).
    "<column caption='Spider / Buffer' name='[Parameter 1]' datatype='integer' "
    "param-domain-type='list' value='1'>"
    "<calculation class='tableau' formula='1' />"
    "<members><member value='1' /><member value='2' /></members>"
    "</column>"
    # 3. Top Count -- integer range (synthetic).
    "<column caption='Top Count' name='[Top Count]' datatype='integer' "
    "param-domain-type='range' value='10'>"
    "<range min='1' max='100' granularity='1' />"
    "</column>"
    # 4. Growth Rate -- real range (synthetic; float precision/inclusive-end coverage).
    "<column caption='Growth Rate' name='[Growth Rate]' datatype='real' "
    "param-domain-type='range' value='0.1'>"
    "<range min='0' max='1' granularity='0.1' />"
    "</column>"
    # 5. As Of Date -- date range (synthetic).
    "<column caption='As Of Date' name='[As Of Date]' datatype='date' "
    "param-domain-type='range' value='#2020-06-01#'>"
    "<range min='#2020-01-01#' max='#2020-12-31#' granularity='1' />"
    "</column>"
    "</datasource>"
    "</datasources>"
    "</workbook>"
)


@pytest.fixture
def specs():
    return extract_parameters(PARAMS_XML)


@pytest.fixture
def by_caption(specs):
    return {s.caption: s for s in specs}


# -- extraction ----------------------------------------------------------------
def test_extracts_all_parameters(specs):
    assert len(specs) == 5
    captions = {s.caption for s in specs}
    assert captions == {
        "Facility Name Parameter", "Spider / Buffer", "Top Count", "Growth Rate", "As Of Date",
    }


def test_facility_decoded(by_caption):
    s = by_caption["Facility Name Parameter"]
    assert s.name == "[Facility Name Parameter]"
    assert s.datatype == "string"
    assert s.domain_type == "list"
    # string default is unwrapped from Tableau's surrounding quotes.
    assert s.default == "New York State Hospital"
    values = [v for v, _a in s.members]
    assert values == _FACILITY_MEMBERS
    # no aliases authored -> alias is None for every member.
    assert all(a is None for _v, a in s.members)


def test_spider_buffer_decoded(by_caption):
    s = by_caption["Spider / Buffer"]
    assert s.name == "[Parameter 1]"
    assert s.datatype == "integer"
    assert s.domain_type == "list"
    assert s.default == 1
    assert [v for v, _a in s.members] == [1, 2]


def test_ranges_decoded(by_caption):
    top = by_caption["Top Count"]
    assert top.domain_type == "range"
    assert top.range.min == 1 and top.range.max == 100 and top.range.step == 1
    growth = by_caption["Growth Rate"]
    assert growth.datatype == "real"
    assert growth.range.min == 0.0 and growth.range.max == 1.0
    assert abs(growth.range.step - 0.1) < 1e-9
    asof = by_caption["As Of Date"]
    assert asof.datatype == "date"
    assert asof.default == "2020-06-01"
    assert asof.range.min == "2020-01-01" and asof.range.max == "2020-12-31"


def test_bom_and_entity_tolerant():
    xml = (
        "\ufeff<datasource name='Parameters'>"
        "<column caption='BOM Param' datatype='string' param-domain-type='list' "
        "value='&quot;X&quot;'><members><member value='&quot;X&quot;' /></members></column>"
        "</datasource>"
    )
    specs = extract_parameters(xml)
    assert len(specs) == 1
    assert specs[0].default == "X"
    assert specs[0].members == [("X", None)]


def test_malformed_and_empty_return_empty():
    assert extract_parameters("") == []
    assert extract_parameters("<not-closed") == []
    assert extract_parameters("<workbook></workbook>") == []


# -- name helpers (the cross-stream contract) ---------------------------------
def test_name_helpers(by_caption):
    s = by_caption["Facility Name Parameter"]
    assert param_table_name(s) == "Facility Name Parameter"
    assert param_ref_name(s) == "Facility Name Parameter Value"
    assert param_value_column(s) == "Facility Name Parameter"
    assert param_slicer_column(s) == "Facility Name Parameter"
    assert param_order_column(s) == "Facility Name Parameter Order"


def test_range_value_columns(by_caption):
    assert param_value_column(by_caption["Top Count"]) == "Value"
    assert param_value_column(by_caption["As Of Date"]) == "Date"


# -- value measure (single-select-safe) ---------------------------------------
def test_facility_value_measure(by_caption):
    name, dax = param_value_measure(by_caption["Facility Name Parameter"])
    assert name == "Facility Name Parameter Value"
    assert dax == (
        "IF(HASONEVALUE('Facility Name Parameter'[Facility Name Parameter]), "
        "SELECTEDVALUE('Facility Name Parameter'[Facility Name Parameter]), "
        '"New York State Hospital")'
    )


def test_spider_value_measure_integer_default(by_caption):
    name, dax = param_value_measure(by_caption["Spider / Buffer"])
    assert name == "Spider / Buffer Value"
    assert dax == (
        "IF(HASONEVALUE('Spider / Buffer'[Spider / Buffer]), "
        "SELECTEDVALUE('Spider / Buffer'[Spider / Buffer]), 1)"
    )


def test_date_range_value_measure(by_caption):
    name, dax = param_value_measure(by_caption["As Of Date"])
    assert name == "As Of Date Value"
    assert dax == (
        "IF(HASONEVALUE('As Of Date'[Date]), SELECTEDVALUE('As Of Date'[Date]), "
        "DATE(2020, 6, 1))"
    )


def test_ref_name_matches_measure_name(by_caption):
    for s in by_caption.values():
        assert param_ref_name(s) == param_value_measure(s)[0]


# -- Tier-1 table emission -----------------------------------------------------
def test_facility_datatable(by_caption):
    tmdl = param_table_tmdl(by_caption["Facility Name Parameter"])
    assert "table 'Facility Name Parameter'" in tmdl
    assert "partition 'Facility Name Parameter' = calculated" in tmdl
    assert "mode: import" in tmdl
    assert "type: calculatedTableColumn" in tmdl
    # ordinal Sort-By column preserving authored order.
    assert "column 'Facility Name Parameter Order'" in tmdl
    assert "sortByColumn: 'Facility Name Parameter Order'" in tmdl
    # DATATABLE header + every member with its 1-based ordinal.
    assert (
        'DATATABLE("Facility Name Parameter", STRING, '
        '"Facility Name Parameter Order", INTEGER, {' in tmdl
    )
    assert '{ "Altran Clinic", 1 }' in tmdl
    assert '{ "New York State Hospital", 6 }' in tmdl
    assert '{ "St. Johns Hospital", 8 }' in tmdl


def test_spider_datatable_integer(by_caption):
    tmdl = param_table_tmdl(by_caption["Spider / Buffer"])
    assert 'DATATABLE("Spider / Buffer", INTEGER, "Spider / Buffer Order", INTEGER, {' in tmdl
    assert "{ 1, 1 }" in tmdl
    assert "{ 2, 2 }" in tmdl


def test_integer_range_generateseries(by_caption):
    tmdl = param_table_tmdl(by_caption["Top Count"])
    assert "source = GENERATESERIES(1, 100, 1)" in tmdl
    assert "column Value" in tmdl
    assert "dataType: int64" in tmdl
    # a range needs no ordinal column.
    assert "Order" not in tmdl


def test_real_range_generateseries(by_caption):
    tmdl = param_table_tmdl(by_caption["Growth Rate"])
    assert "source = GENERATESERIES(0, 1, 0.1)" in tmdl
    assert "dataType: double" in tmdl


def test_date_range_uses_calendar_not_generateseries(by_caption):
    tmdl = param_table_tmdl(by_caption["As Of Date"])
    assert "source = CALENDAR(DATE(2020, 1, 1), DATE(2020, 12, 31))" in tmdl
    assert "GENERATESERIES" not in tmdl
    assert "column Date" in tmdl


# -- storage-mode awareness ----------------------------------------------------
def test_import_has_no_storage_note(by_caption):
    tmdl = param_table_tmdl(by_caption["Top Count"], storage_mode="import")
    assert not tmdl.startswith("///")


def test_directquery_flags_composite(by_caption):
    tmdl = param_table_tmdl(by_caption["Top Count"], storage_mode="DirectQuery")
    assert tmdl.startswith("/// ")
    assert "composite" in tmdl.lower()


def test_directlake_unsupported_note_and_not_deploy_ready(by_caption):
    s = by_caption["Top Count"]
    tmdl = param_table_tmdl(s, storage_mode="Direct Lake")
    assert "Direct Lake" in tmdl
    cc = classify_parameter(s, {"measure"}, storage_mode="Direct Lake")
    assert cc.deploy_ready is False
    assert any("Direct Lake" in w for w in cc.warnings)


# -- classification guardrails -------------------------------------------------
def test_classify_measure_value(by_caption):
    cc = classify_parameter(by_caption["Facility Name Parameter"], {"measure"})
    assert cc.name == CLASS_MEASURE_VALUE
    assert cc.tier == 1
    assert cc.deploy_ready is True


def test_classify_calc_column_is_loud_manual(by_caption):
    cc = classify_parameter(by_caption["Spider / Buffer"], {"calc_column"})
    assert cc.name == CLASS_CALC_COLUMN
    assert cc.deploy_ready is False
    assert any("calculated column" in w.lower() for w in cc.warnings)


def test_classify_top_n(by_caption):
    cc = classify_parameter(by_caption["Top Count"], {"top_n"})
    assert cc.name == CLASS_TOP_N
    assert cc.tier == 3


def test_classify_filter(by_caption):
    cc = classify_parameter(by_caption["Facility Name Parameter"], {"filter"})
    assert cc.name == CLASS_VISUAL_FILTER
    assert cc.tier == 2


def test_no_usage_warns_but_defaults_to_value(by_caption):
    cc = classify_parameter(by_caption["Facility Name Parameter"], None)
    assert cc.name == CLASS_MEASURE_VALUE
    assert any("no workbook usage" in w.lower() for w in cc.warnings)


# -- unbounded ('all') ---------------------------------------------------------
ALL_XML = (
    "<datasource name='Parameters'>"
    "<column caption='Free Text' name='[Free Text]' datatype='string' "
    "param-domain-type='all' value='\"hello\"'>"
    "<calculation class='tableau' formula='\"hello\"' /></column>"
    "</datasource>"
)


def test_unbounded_is_default_only_not_deploy_ready():
    s = extract_parameters(ALL_XML)[0]
    cc = classify_parameter(s, {"measure"})
    assert cc.name == CLASS_MANUAL_UNBOUNDED
    assert cc.strategy == STRAT_DEFAULT_ONLY
    assert cc.deploy_ready is False
    # no enumerable table; constant-only value measure.
    assert param_table_tmdl(s) == ""
    name, dax = param_value_measure(s)
    assert name == "Free Text Value"
    assert dax == '"hello"'


# -- alias value|label two-column mode ----------------------------------------
ALIAS_XML = (
    "<datasource name='Parameters'>"
    "<column caption='Region Param' name='[Region Param]' datatype='string' "
    "param-domain-type='list' value='\"E\"'>"
    "<members>"
    "<member value='\"E\"' alias='East' />"
    "<member value='\"W\"' alias='West' />"
    "</members></column>"
    "</datasource>"
)


def test_alias_two_column_table_and_slicer_binding():
    s = extract_parameters(ALIAS_XML)[0]
    assert param_slicer_column(s) == "Region Param Label"
    assert param_value_column(s) == "Region Param"
    tmdl = param_table_tmdl(s)
    assert (
        'DATATABLE("Region Param", STRING, "Region Param Label", STRING, '
        '"Region Param Order", INTEGER, {' in tmdl
    )
    assert '{ "E", "East", 1 }' in tmdl
    assert '{ "W", "West", 2 }' in tmdl
    # value column hidden (measure reads it); label drives the slicer.
    assert "column 'Region Param'" in tmdl
    assert "column 'Region Param Label'" in tmdl
    # value measure still reads the underlying value column, not the label.
    _name, dax = param_value_measure(s)
    assert "'Region Param'[Region Param]" in dax


def test_duplicate_caption_detected():
    xml = (
        "<datasource name='Parameters'>"
        "<column caption='Dup' name='[Dup]' datatype='string' param-domain-type='list' "
        "value='\"A\"'><members>"
        "<member value='\"A\"' alias='Same' />"
        "<member value='\"B\"' alias='Same' />"
        "</members></column></datasource>"
    )
    s = extract_parameters(xml)[0]
    cc = classify_parameter(s, {"measure"})
    assert any("duplicate" in w.lower() and "caption" in w.lower() for w in cc.warnings)


def test_default_not_in_members_warns():
    xml = (
        "<datasource name='Parameters'>"
        "<column caption='Stale' name='[Stale]' datatype='string' param-domain-type='list' "
        "value='\"Z\"'><members>"
        "<member value='\"A\"' /><member value='\"B\"' />"
        "</members></column></datasource>"
    )
    s = extract_parameters(xml)[0]
    cc = classify_parameter(s, {"measure"})
    assert any("not among the listed members" in w for w in cc.warnings)


# -- convenience bundle --------------------------------------------------------
def test_emit_parameter_bundle(by_caption):
    out = emit_parameter(by_caption["Facility Name Parameter"], {"measure"}, storage_mode="import")
    assert out["table_name"] == "Facility Name Parameter"
    assert out["ref_name"] == "Facility Name Parameter Value"
    assert out["value_column"] == "Facility Name Parameter"
    assert out["slicer_column"] == "Facility Name Parameter"
    assert out["deploy_ready"] is True
    assert "DATATABLE" in out["table_tmdl"]
    assert out["value_measure"][0] == "Facility Name Parameter Value"
