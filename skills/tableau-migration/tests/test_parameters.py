"""Field-parameter translation tests: Tableau swap calcs -> Power BI field parameters.

A Tableau `CASE/IF [Parameters].[X] WHEN <lit> THEN [field] ...` calc that *swaps which
field* is shown maps to a Power BI **field parameter** (a 3-column calculated table whose
Fields column carries `ParameterMetadata = {"version":3,"kind":2}`). These tests cover
detection (the safe grammar + its guards), emission (the verified TMDL markers + DAX-escaped
NAMEOF + label/de-dup rules), and end-to-end assembly (consumed, additive, never related).
"""
import pytest

import parameters as P
from assemble_model import assemble_import_model, assemble_directlake_model
from connection_to_m import parse_tds
from test_connection_to_m import LIVE_SQLSERVER


# -- a stub field locator: resolves a fixed set of Tableau fields to model columns ----------
_COLS = {
    "Segment": ("Orders", "Segment", False),
    "Sub_Category": ("Orders", "Sub_Category", False),
    "Region": ("Orders", "Region", False),
    "Sales": ("Orders", "Sales", False),
    "Profit": ("Orders", "Profit", False),
    "Quantity": ("Orders", "Quantity", False),
}


def _loc(field):
    return _COLS.get(field)


# -- detection ------------------------------------------------------------------------------
def test_detect_case_dimension_swap():
    f = ('case [Parameters].[Parameter 1] when "Segment" then [Segment] '
         'when "Sub Category" then [Sub_Category] when "Region" then [Region] END')
    sw = P.detect_field_swap(f, role="dimension")
    assert sw is not None
    assert sw["controller"] == "Parameter 1"
    assert sw["role"] == "dimension"
    assert sw["form"] == "case"
    assert [(b["label"], b["field"]) for b in sw["branches"]] == [
        ("Segment", "Segment"), ("Sub Category", "Sub_Category"), ("Region", "Region")]


def test_detect_case_measure_swap_numeric_labels():
    f = 'Case [Parameters].[m] when 1 then [Sales] when 2 then [Profit] when 3 then [Quantity] END'
    sw = P.detect_field_swap(f, role="measure")
    assert sw is not None
    assert [b["label"] for b in sw["branches"]] == ["1", "2", "3"]
    assert [b["field"] for b in sw["branches"]] == ["Sales", "Profit", "Quantity"]


def test_detect_glued_end_keyword():
    # Tableau often glues END onto the last field ref: `[Region]END`.
    f = 'case [Parameters].[p] when "A" then [Segment] when "B" then [Region]END'
    sw = P.detect_field_swap(f, role="dimension")
    assert sw is not None
    assert sw["branches"][-1]["field"] == "Region"


def test_detect_if_elseif_else_swap():
    f = ('IF [Parameters].[p] = "A" THEN [Segment] ELSEIF [Parameters].[p] = "B" THEN [Region] '
         'ELSE [Sales] END')
    sw = P.detect_field_swap(f, role="dimension")
    assert sw is not None
    assert sw["form"] == "if"
    assert sw["branches"][-1]["is_else"] is True
    assert sw["branches"][-1]["field"] == "Sales"
    assert [b["field"] for b in sw["branches"]] == ["Segment", "Region", "Sales"]


@pytest.mark.parametrize("formula", [
    "SUM([Sales]) / SUM([Profit])",                                    # not a CASE/IF at all
    'case [Parameters].[p] when "A" then [Sales] + 1 END',            # branch not a bare field
    'case [Parameters].[p] when "A" then [Sales] END',               # only one branch
    'case [Other].[p] when "A" then [Sales] when "B" then [Profit] END',  # controller not Parameters
    'case [Parameters].[p] junk when "A" then [Sales] when "B" then [Profit] END',  # stray tokens
])
def test_detect_rejects_non_swaps(formula):
    assert P.detect_field_swap(formula, role="measure") is None


def test_detect_if_requires_single_controller():
    # Two different parameters in one IF chain is not a single-parameter swap.
    f = 'IF [Parameters].[a] = "A" THEN [Segment] ELSEIF [Parameters].[b] = "B" THEN [Region] END'
    assert P.detect_field_swap(f, role="dimension") is None


# -- DAX escaping ---------------------------------------------------------------------------
def test_dax_ref_escapes_table_and_field():
    assert P.dax_ref("Sales' Data", "Sub]Cat") == "'Sales'' Data'[Sub]]Cat]"
    assert P.dax_ref("Orders", "Sales") == "'Orders'[Sales]"


def test_dax_ref_measure_has_no_table_qualifier():
    assert P.dax_ref("Orders", "Total Sales", measure=True) == "[Total Sales]"


# -- emission: structure --------------------------------------------------------------------
def test_emit_field_parameter_markers():
    sw = P.detect_field_swap(
        'case [Parameters].[p] when "Segment" then [Segment] when "Region" then [Region] END',
        role="dimension")
    res = P.emit_field_parameter("Dim calc 1", sw, field_locator=_loc, used_names={"orders"})
    assert res["ok"] is True
    assert res["table_name"] == "Dim calc 1"
    assert res["part_filename"] == "Dim calc 1.tmdl"
    t = res["tmdl"]
    # three columns mapped to the canonical Value1/Value2/Value3 source columns
    assert "sourceColumn: [Value1]" in t and "sourceColumn: [Value2]" in t and "sourceColumn: [Value3]" in t
    # the field-parameter marker lives on the (hidden) Fields column
    assert '"version": 3' in t and '"kind": 2' in t
    assert "extendedProperty ParameterMetadata =" in t
    # display sorts by Order and groups by Fields
    assert "sortByColumn: 'Dim calc 1 Order'" in t
    assert "groupByColumn: 'Dim calc 1 Fields'" in t
    # DAX-escaped NAMEOF tuples in declaration order
    assert '("Segment", NAMEOF(\'Orders\'[Segment]), 0)' in t
    assert '("Region", NAMEOF(\'Orders\'[Region]), 1)' in t
    # the two hidden columns
    assert t.count("isHidden") == 2


def test_emit_measure_swap_labels_and_warning():
    sw = P.detect_field_swap(
        'case [Parameters].[m] when 1 then [Sales] when 2 then [Profit] END', role="measure")
    res = P.emit_field_parameter("Measure Calc", sw, field_locator=_loc, used_names=set())
    assert res["ok"] is True
    # numeric selectors fall back to the field's own display name
    assert '("Sales", NAMEOF(\'Orders\'[Sales]), 0)' in res["tmdl"]
    assert '("Profit", NAMEOF(\'Orders\'[Profit]), 1)' in res["tmdl"]
    assert any("default column aggregation" in w for w in res["warnings"])


def test_emit_measure_swap_uses_aliases_when_given():
    sw = P.detect_field_swap(
        'case [Parameters].[m] when 1 then [Sales] when 2 then [Profit] END', role="measure")
    res = P.emit_field_parameter("Measure Calc", sw, field_locator=_loc, used_names=set(),
                                 label_aliases={"1": "Revenue", "2": "Margin"})
    assert '("Revenue", NAMEOF(\'Orders\'[Sales]), 0)' in res["tmdl"]
    assert '("Margin", NAMEOF(\'Orders\'[Profit]), 1)' in res["tmdl"]


def test_emit_deduplicates_duplicate_labels():
    sw = {"controller": "p", "role": "dimension", "form": "case",
          "branches": [{"label": "Geo", "field": "Segment"}, {"label": "Geo", "field": "Region"}]}
    res = P.emit_field_parameter("Calc", sw, field_locator=_loc, used_names=set())
    assert res["ok"] is True
    assert '("Geo", NAMEOF(\'Orders\'[Segment]), 0)' in res["tmdl"]
    assert '("Geo (2)", NAMEOF(\'Orders\'[Region]), 1)' in res["tmdl"]
    assert any("duplicate option label" in w for w in res["warnings"])


def test_emit_drops_unresolved_fields_and_fails_closed():
    sw = {"controller": "p", "role": "dimension", "form": "case",
          "branches": [{"label": "A", "field": "Segment"}, {"label": "B", "field": "DoesNotExist"}]}
    res = P.emit_field_parameter("Calc", sw, field_locator=_loc, used_names=set())
    # only one branch resolved -> not converted; the calc is left for normal translation
    assert res["ok"] is False
    assert res["table_name"] is None
    assert any("did not resolve" in w for w in res["warnings"])


def test_emit_uniquifies_table_name_against_existing():
    sw = P.detect_field_swap(
        'case [Parameters].[p] when "A" then [Segment] when "B" then [Region] END', role="dimension")
    used = {"dim calc 1"}
    res = P.emit_field_parameter("Dim calc 1", sw, field_locator=_loc, used_names=used)
    assert res["table_name"] == "Dim calc 1 2"


# -- orchestration: emit_field_parameters ---------------------------------------------------
def test_emit_field_parameters_consumes_and_warns_on_dependency():
    calcs = [
        {"name": "Dim calc 1", "role": "dimension",
         "formula": 'case [Parameters].[p] when "A" then [Segment] when "B" then [Region] END'},
        {"name": "Measure Calc", "role": "measure",
         "formula": 'case [Parameters].[m] when 1 then [Sales] when 2 then [Profit] END'},
        {"name": "Value calc", "role": "measure",
         "formula": '{fixed [Dim calc 1]: SUM([Sales])}'},
    ]
    out = P.emit_field_parameters(calcs, field_locator=_loc, existing_tables=["Orders", "_Measures"])
    assert out["consumed"] == {"Dim calc 1", "Measure Calc"}
    assert len(out["parts"]) == 2
    assert set(out["table_names"]) == {"Dim calc 1", "Measure Calc"}
    # the downstream calc that references a consumed swap is flagged (it will stub)
    assert any("Value calc" in w and "field parameter" in w for w in out["warnings"])


def test_emit_field_parameters_deduplicates_part_filenames():
    calcs = [
        {"name": "A/B", "role": "dimension",
         "formula": 'case [Parameters].[p] when "x" then [Segment] when "y" then [Region] END'},
        {"name": "A:B", "role": "dimension",
         "formula": 'case [Parameters].[q] when "x" then [Sales] when "y" then [Profit] END'},
    ]
    out = P.emit_field_parameters(calcs, field_locator=_loc, existing_tables=[])
    files = [fn for fn, _ in out["parts"]]
    # both sanitise to "A_B.tmdl"; the second must be de-duplicated
    assert files[0] == "A_B.tmdl"
    assert files[1] == "A_B_2.tmdl"
    assert len(set(files)) == 2


# -- extraction: dimension-role swaps survive (extract_calculations drops them) -------------
def test_extract_field_swap_calcs_includes_dimension_role():
    xml = """<?xml version='1.0' encoding='utf-8'?>
    <workbook>
      <datasource>
        <column caption='Dim calc 1' name='[Calculation_1]' role='dimension' datatype='string'>
          <calculation class='tableau'
            formula='case [Parameters].[p] when "A" then [Segment] when "B" then [Region] END'/>
        </column>
        <column caption='Plain Measure' name='[Calculation_2]' role='measure'>
          <calculation class='tableau' formula='SUM([Sales])'/>
        </column>
      </datasource>
    </workbook>"""
    swaps = P.extract_field_swap_calcs(xml)
    assert [s["name"] for s in swaps] == ["Dim calc 1"]
    assert swaps[0]["role"] == "dimension"


# -- end-to-end assembly --------------------------------------------------------------------
def test_assemble_import_model_emits_field_parameter():
    calcs = [
        {"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"},
        {"name": "Metric", "formula": "CASE [Parameters].[m] WHEN 1 THEN [Sales] WHEN 2 THEN [Quantity] END"},
    ]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore", calcs=calcs)
    parts, report = out["parts"], out["report"]

    # the swap calc became a field-parameter table part, not a measure
    assert "definition/tables/Metric.tmdl" in parts
    fp = parts["definition/tables/Metric.tmdl"]
    assert "extendedProperty ParameterMetadata =" in fp
    assert "NAMEOF('Orders'[Sales])" in fp and "NAMEOF('Orders'[Quantity])" in fp
    assert "Metric" not in parts["definition/tables/_Measures.tmdl"]

    # it is listed in the model but never wired into relationships
    assert "ref table Metric" in parts["definition/model.tmdl"]
    assert "Metric" not in parts.get("definition/relationships.tmdl", "")

    # the still-translatable calc remains a measure
    assert "Profit Ratio" in parts["definition/tables/_Measures.tmdl"]

    # the report records the consumption
    assert "Metric" in report["field_parameters"]["consumed"]
    assert report["field_parameters"]["tables"] == ["Metric"]


def test_assemble_directlake_model_injects_field_parameters():
    sw = P.detect_field_swap(
        'case [Parameters].[p] when "A" then [Segment] when "B" then [Region] END', role="dimension")
    fp = P.emit_field_parameters(
        [{"name": "Dim calc 1", "role": "dimension",
          "formula": 'case [Parameters].[p] when "A" then [Segment] when "B" then [Region] END'}],
        field_locator=_loc, existing_tables=["Orders"])
    out = assemble_directlake_model(
        model_name="DL", tables=[("Orders", "ds_orders", "")], measures_tmdl="",
        expression_name="DL", directlake_url="https://x", field_parameters=fp)
    parts = out["parts"]
    assert "definition/tables/Dim calc 1.tmdl" in parts
    assert "ParameterMetadata" in parts["definition/tables/Dim calc 1.tmdl"]
    # registered in model.tmdl just before _Measures, not in relationships
    model = parts["definition/model.tmdl"]
    assert "ref table 'Dim calc 1'" in model or "ref table Dim calc 1" in model
