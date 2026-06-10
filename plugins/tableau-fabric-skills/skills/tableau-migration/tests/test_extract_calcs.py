"""Offline tests for ``extract_calcs`` -- pulling calculated fields out of a ``.tds``.

Verifies the helper returns exactly the ``[{"name", "formula", "role"}]`` shape the assembler's
``calcs=`` argument expects, and that it excludes parameters and non-formula calculations (bins).
"""
import connection_to_m as C
from assemble_model import extract_calcs as extract_calcs_reexport


# A compact .tds logical layer: one physical column, two real calcs, a parameter, and a bin.
TDS = """<?xml version='1.0' encoding='utf-8'?>
<datasource formatted-name='demo' inline='true' version='18.1'>
  <column caption='Sales' datatype='real' name='[SALES]' role='measure' type='quantitative' />
  <column caption='Profit Ratio' datatype='real' name='[Calculation_1]' role='measure' type='quantitative'>
    <calculation class='tableau' formula='SUM([Profit]) / SUM([Sales])' />
  </column>
  <column caption='Is Big Order' datatype='boolean' name='[Calculation_2]' role='dimension' type='nominal'>
    <calculation class='tableau' formula='IF [Sales] &gt; 100 THEN True ELSE False END' />
  </column>
  <column caption='Region Param' datatype='string' name='[Parameter 1]' role='measure'
          type='nominal' param-domain-type='list'>
    <calculation class='tableau' formula='&quot;East&quot;' />
  </column>
  <column caption='Sales (bin)' datatype='integer' name='[Sales (bin)]' role='dimension' type='ordinal'>
    <calculation class='bin' decompose='false' formula='[Sales]' size='[Sales (bin size)]' />
  </column>
  <column datatype='real' name='[Calculation_NoCaption]' role='measure' type='quantitative'>
    <calculation class='tableau' formula='AVG([Discount])' />
  </column>
</datasource>"""


def test_extracts_only_real_calculated_fields():
    calcs = C.extract_calcs(TDS)
    names = [c["name"] for c in calcs]
    # the two captioned calcs + the caption-less one (name fallback); NOT the param, bin, or physical col
    assert names == ["Profit Ratio", "Is Big Order", "Calculation_NoCaption"]


def test_formula_is_xml_unescaped():
    by_name = {c["name"]: c for c in C.extract_calcs(TDS)}
    assert by_name["Profit Ratio"]["formula"] == "SUM([Profit]) / SUM([Sales])"
    # &gt; -> > comes back ready for the translator
    assert by_name["Is Big Order"]["formula"] == "IF [Sales] > 100 THEN True ELSE False END"


def test_role_is_carried_through():
    by_name = {c["name"]: c for c in C.extract_calcs(TDS)}
    assert by_name["Profit Ratio"]["role"] == "measure"
    assert by_name["Is Big Order"]["role"] == "dimension"


def test_caption_falls_back_to_debracketed_name():
    by_name = {c["name"]: c for c in C.extract_calcs(TDS)}
    assert "Calculation_NoCaption" in by_name
    assert by_name["Calculation_NoCaption"]["formula"] == "AVG([Discount])"


def test_internal_name_carried_for_captioned_calcs():
    # A captioned calc carries its de-bracketed internal Calculation_* name -- what OTHER
    # calcs reference in their formulas -- so cross-calc references resolve downstream.
    by_name = {c["name"]: c for c in C.extract_calcs(TDS)}
    assert by_name["Profit Ratio"]["internal_name"] == "Calculation_1"
    assert by_name["Is Big Order"]["internal_name"] == "Calculation_2"
    # when the display name already IS the internal name, it is not duplicated as internal_name
    assert "internal_name" not in by_name["Calculation_NoCaption"]


def test_parameter_and_bin_are_excluded():
    names = [c["name"] for c in C.extract_calcs(TDS)]
    assert "Region Param" not in names   # param-domain-type -> parameter, not a calc
    assert "Sales (bin)" not in names    # calculation class='bin', no tableau formula


def test_no_calcs_returns_empty_list():
    plain = ("<datasource formatted-name='x'>"
             "<column caption='Sales' datatype='real' name='[SALES]' role='measure' /></datasource>")
    assert C.extract_calcs(plain) == []


def test_dedupes_case_insensitively_keeping_first():
    dup = """<datasource formatted-name='x'>
      <column caption='Margin' name='[c1]' role='measure'>
        <calculation class='tableau' formula='SUM([Profit])' /></column>
      <column caption='margin' name='[c2]' role='measure'>
        <calculation class='tableau' formula='SUM([Sales])' /></column>
    </datasource>"""
    calcs = C.extract_calcs(dup)
    assert [c["name"] for c in calcs] == ["Margin"]
    assert calcs[0]["formula"] == "SUM([Profit])"  # first occurrence wins


def test_reexported_from_assemble_model():
    # the agent reads assemble_model first, so the helper must be importable from there too
    assert extract_calcs_reexport(TDS) == C.extract_calcs(TDS)
