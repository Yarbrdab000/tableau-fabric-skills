"""Wiring tests: Tableau ``<column @default-format>`` on a *calc* -> Power BI measure formatString.

A measure (aggregate calculated field) previously emitted with NO ``formatString`` at all, so an
author's declared percent/currency/precision was lost (e.g. Profit Ratio rendered ``0.13`` instead
of ``12.6%``). This covers the conservative decoder ``tableau_measure_format_to_pbi``, the additive
``format_string`` parameter on ``generate_measure_tmdl``, the ``format_string`` stamp on both calc
extractors (``migrate_estate.extract_calculations`` + ``connection_to_m.extract_calcs``), and the
end-to-end flow through ``assemble_model._measures_part``.

The physical-column path is covered by ``test_default_format_wiring.py``; the shared decode core by
``test_default_format_decode.py``.
"""
from assemble_model import _measures_part
from connection_to_m import extract_calcs
from migrate_estate import extract_calculations
from tmdl_generate import generate_measure_tmdl, tableau_measure_format_to_pbi


# -- tableau_measure_format_to_pbi (the conservative measure decoder) ----------
def test_measure_decode_accepts_explicit_lowercase_codes():
    assert tableau_measure_format_to_pbi("p0.0%") == "0.0%"
    assert tableau_measure_format_to_pbi("p0%") == "0%"
    assert tableau_measure_format_to_pbi('c"$"#,##0;("$"#,##0)') == '"$"#,##0;("$"#,##0)'
    assert tableau_measure_format_to_pbi("n#,##0.0") == "#,##0.0"
    assert tableau_measure_format_to_pbi("*00000") == "00000"


def test_measure_decode_declines_ambiguous_builtin_uppercase_form():
    # C<lcid>% is DELIBERATELY declined for measures: in the wild it appears on BOTH currency
    # and percent measures, so decoding it to 0% would mis-render a dollar figure as a percentage.
    assert tableau_measure_format_to_pbi("C1033%") is None
    assert tableau_measure_format_to_pbi("N1033") is None


def test_measure_decode_none_for_empty_or_undecodable():
    assert tableau_measure_format_to_pbi(None) is None
    assert tableau_measure_format_to_pbi("") is None
    assert tableau_measure_format_to_pbi("   ") is None
    assert tableau_measure_format_to_pbi("zzz-not-a-format") is None


# -- generate_measure_tmdl(format_string=...) — the additive serializer param --
def test_generate_measure_tmdl_emits_format_string():
    out = generate_measure_tmdl("Profit Ratio", "sum([Profit])/sum([Sales])",
                                "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
                                format_string="0.0%")
    assert "formatString: 0.0%\n" in out
    # Ordered as a property: after the expression, before the lineageTag.
    assert out.index("formatString: 0.0%") < out.index("lineageTag:")


def test_generate_measure_tmdl_none_is_byte_identical():
    # Omitting the arg and passing None both emit NO formatString line (prior behavior).
    a = generate_measure_tmdl("M", "SUM([x])", "SUM('T'[x])")
    b = generate_measure_tmdl("M", "SUM([x])", "SUM('T'[x])", format_string=None)
    assert "formatString" not in a
    assert "formatString" not in b


# -- extract_calculations (the .twb measure calc extractor) --------------------
_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook>
  <datasource caption='DS' name='ds'>
    <column caption='Profit Ratio' role='measure' datatype='real' name='[Calculation_1]'
            default-format='p0.0%'>
      <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />
    </column>
    <column caption='Sales per Customer' role='measure' datatype='real' name='[Calculation_2]'
            default-format='C1033%'>
      <calculation class='tableau' formula='SUM([Sales])/COUNTD([Customer])' />
    </column>
    <column caption='Count Orders' role='measure' datatype='integer' name='[Calculation_3]'>
      <calculation class='tableau' formula='COUNT([Order ID])' />
    </column>
  </datasource>
</workbook>"""


def test_extract_calculations_stamps_explicit_format():
    calcs, _ = extract_calculations(_TWB)
    by_name = {c["name"]: c for c in calcs}
    assert by_name["Profit Ratio"]["format_string"] == "0.0%"


def test_extract_calculations_skips_ambiguous_and_absent():
    calcs, _ = extract_calculations(_TWB)
    by_name = {c["name"]: c for c in calcs}
    # The ambiguous built-in C1033% is declined (never a wrong percent), and a calc with no
    # default-format never gains the key -- both keep their type-derived floor downstream.
    assert "format_string" not in by_name["Sales per Customer"]
    assert "format_string" not in by_name["Count Orders"]


# -- connection_to_m.extract_calcs (the .tds calc extractor) -------------------
_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='TdsFmt' version='18.1'>
  <column caption='Profit Ratio' role='measure' datatype='real' name='[Calculation_1]'
          default-format='p0.0%'>
    <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />
  </column>
  <column caption='Plain Total' role='measure' datatype='real' name='[Calculation_9]'>
    <calculation class='tableau' formula='SUM([Sales])' />
  </column>
</datasource>"""


def test_extract_calcs_tds_stamps_explicit_format():
    by_name = {c["name"]: c for c in extract_calcs(_TDS)}
    assert by_name["Profit Ratio"]["format_string"] == "0.0%"
    assert "format_string" not in by_name["Plain Total"]


# -- end-to-end through _measures_part ----------------------------------------
def _resolver(mapping):
    def _r(name):
        return mapping.get((name or "").strip())
    return _r


def test_measures_part_applies_format_to_the_right_measure():
    resolve = _resolver({
        "Profit": ("Orders", "Profit", "double"),
        "Sales": ("Orders", "Sales", "double"),
    })
    calcs = [
        {"name": "Profit Ratio", "formula": "SUM([Profit])/SUM([Sales])", "format_string": "0.0%"},
        {"name": "Plain Sales", "formula": "SUM([Sales])"},
    ]
    tmdl, _report, _sugg = _measures_part(calcs, resolve, known_tables={"Orders"})
    # The formatted calc carries its formatString; the unformatted one does not -> exactly one
    # formatString across the emitted measures (the hidden Value column carries none).
    assert "formatString: 0.0%" in tmdl
    assert tmdl.count("formatString:") == 1


def test_measures_part_byte_identical_without_any_format():
    resolve = _resolver({"Sales": ("Orders", "Sales", "double")})
    calcs = [{"name": "Plain Sales", "formula": "SUM([Sales])"}]
    tmdl, _r, _s = _measures_part(calcs, resolve, known_tables={"Orders"})
    assert "formatString:" not in tmdl
