"""Field-parameter translation tests: Tableau swap calcs -> Power BI field parameters.

A Tableau `CASE/IF [Parameters].[X] WHEN <lit> THEN [field] ...` calc that *swaps which
field* is shown maps to a Power BI **field parameter** (a 3-column calculated table whose
Fields column carries `ParameterMetadata = {"version":3,"kind":2}`). These tests cover
detection (the safe grammar + its guards), emission (the verified TMDL markers + DAX-escaped
NAMEOF + label/de-dup rules), and end-to-end assembly (consumed, additive, never related).
"""
import pytest

import parameters as P
from assemble_model import (
    assemble_import_model,
    assemble_directlake_model,
    migrate_datasource,
)
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


def test_detect_if_two_word_else_if_swap():
    # Tableau's two-word ``ELSE IF`` is a nested IF (each closed by its own END); it must
    # parse identically to the one-word ELSEIF form.
    f = ('IF [Parameters].[p] = "A" THEN [Segment] ELSE IF [Parameters].[p] = "B" THEN [Region] '
         'ELSE [Sales] END END')
    sw = P.detect_field_swap(f, role="dimension")
    assert sw is not None
    assert sw["form"] == "if"
    assert sw["controller"] == "p"
    assert [b["field"] for b in sw["branches"]] == ["Segment", "Region", "Sales"]
    assert sw["branches"][-1]["is_else"] is True


def test_detect_if_reversed_operand_swap():
    # Some authors put the literal on the left: ``"A" = [Parameters].[p]``.
    f = ('IF "A" = [Parameters].[p] THEN [Segment] ELSEIF "B" = [Parameters].[p] THEN [Region] END')
    sw = P.detect_field_swap(f, role="dimension")
    assert sw is not None
    assert sw["controller"] == "p"
    assert [(b["label"], b["field"]) for b in sw["branches"]] == [
        ("A", "Segment"), ("B", "Region")]


def test_detect_if_rejects_compound_condition():
    # A compound condition (extra AND clause) is not a clean single-parameter swap.
    f = 'IF [Parameters].[p] = "A" AND [x] = "y" THEN [Segment] ELSE [Region] END'
    assert P.detect_field_swap(f, role="dimension") is None


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


def test_emit_field_parameter_exposes_structured_entries_for_report_side():
    # the report-side fieldParameters expansion needs the resolved candidate fields, not just TMDL
    sw = P.detect_field_swap(
        'case [Parameters].[p] when "Segment" then [Segment] when "Region" then [Region] END',
        role="dimension")
    res = P.emit_field_parameter("Dim calc 1", sw, field_locator=_loc, used_names=set())
    assert res["role"] == "dimension"
    assert res["display_col"] == "Dim calc 1"  # display column is named after the table
    assert res["entries"] == [
        {"label": "Segment", "table": "Orders", "column": "Segment", "is_measure": False, "order": 0},
        {"label": "Region", "table": "Orders", "column": "Region", "is_measure": False, "order": 1},
    ]


def test_emit_measure_swap_entries_flag_is_measure():
    sw = P.detect_field_swap(
        'case [Parameters].[m] when 1 then [Sales] when 2 then [Profit] END', role="measure")
    res = P.emit_field_parameter("Measure Calc", sw, field_locator=_loc, used_names=set())
    # a measure swap over raw columns synthesizes aggregating measures and points the entries at them
    assert all(e["is_measure"] is True for e in res["entries"])
    assert [(e["label"], e["table"], e["column"], e["order"]) for e in res["entries"]] == [
        ("Sales", "_Measures", "Total Sales", 0), ("Profit", "_Measures", "Total Profit", 1)]
    # the synthesized SUM measures are returned for emission into _Measures
    assert [(m["name"], m["dax"]) for m in res["measures"]] == [
        ("Total Sales", "SUM('Orders'[Sales])"), ("Total Profit", "SUM('Orders'[Profit])")]


def test_emit_field_parameter_failed_swap_has_empty_entries():
    sw = {"controller": "p", "role": "dimension", "form": "case",
          "branches": [{"label": "A", "field": "Segment"}, {"label": "B", "field": "DoesNotExist"}]}
    res = P.emit_field_parameter("Calc", sw, field_locator=_loc, used_names=set())
    assert res["ok"] is False
    assert res["entries"] == [] and res["display_col"] is None


def test_emit_measure_swap_labels_and_warning():
    sw = P.detect_field_swap(
        'case [Parameters].[m] when 1 then [Sales] when 2 then [Profit] END', role="measure")
    res = P.emit_field_parameter("Measure Calc", sw, field_locator=_loc, used_names=set())
    assert res["ok"] is True
    # numeric selectors fall back to the field's own display name; the NAMEOF target is the
    # synthesized aggregating measure (so the swap aggregates instead of grouping by a raw column)
    assert '("Sales", NAMEOF([Total Sales]), 0)' in res["tmdl"]
    assert '("Profit", NAMEOF([Total Profit]), 1)' in res["tmdl"]
    assert any("synthesized" in w and "SUM" in w for w in res["warnings"])


def test_emit_measure_swap_uses_aliases_when_given():
    sw = P.detect_field_swap(
        'case [Parameters].[m] when 1 then [Sales] when 2 then [Profit] END', role="measure")
    res = P.emit_field_parameter("Measure Calc", sw, field_locator=_loc, used_names=set(),
                                 label_aliases={"1": "Revenue", "2": "Margin"})
    assert '("Revenue", NAMEOF([Total Sales]), 0)' in res["tmdl"]
    assert '("Margin", NAMEOF([Total Profit]), 1)' in res["tmdl"]


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


def test_emit_field_parameters_returns_report_specs_in_detection_order():
    # the report side consumes ``specs`` to render the self-service table's slots
    calcs = [
        {"name": "Dim calc 1", "role": "dimension",
         "formula": 'case [Parameters].[p] when "A" then [Segment] when "B" then [Region] END'},
        {"name": "Measure Calc", "role": "measure",
         "formula": 'case [Parameters].[m] when 1 then [Sales] when 2 then [Profit] END'},
    ]
    out = P.emit_field_parameters(calcs, field_locator=_loc, existing_tables=["Orders", "_Measures"])
    specs = out["specs"]
    assert [s["calc_name"] for s in specs] == ["Dim calc 1", "Measure Calc"]  # detection order
    # each spec records its controlling parameter (the model manifest tags that param kind="field")
    assert [s["controller"] for s in specs] == ["p", "m"]
    dim = specs[0]
    assert dim["table_name"] == "Dim calc 1" and dim["display_col"] == "Dim calc 1"
    assert dim["role"] == "dimension"
    assert [(e["label"], e["column"]) for e in dim["entries"]] == [
        ("A", "Segment"), ("B", "Region")]



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


# -- end-to-end assembly: a swap calc now becomes a field parameter (consumed, not a stub) ----
def test_assemble_import_model_wires_swap_calc_to_field_parameter():
    calcs = [
        {"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"},
        {"name": "Metric", "formula": "CASE [Parameters].[m] WHEN 1 THEN [Sales] WHEN 2 THEN [Quantity] END"},
    ]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore", calcs=calcs)
    parts, report = out["parts"], out["report"]

    # the parameter-driven swap calc is now wired as a field-parameter table
    assert "definition/tables/Metric.tmdl" in parts
    assert "ParameterMetadata" in parts["definition/tables/Metric.tmdl"]

    # it is CONSUMED -- it must NOT also appear as a measure (no inert `= 0` stub for it)
    measures = parts["definition/tables/_Measures.tmdl"]
    assert "measure Metric" not in measures
    assert "Metric" not in {r["measure"] for r in report["measures"]}

    # the still-translatable calc remains a real translated measure
    assert "Profit Ratio" in measures
    assert "DIVIDE(" in measures

    # the additive report keys record the consumed swap; it is registered before _Measures,
    # never wired into relationships
    assert "Metric" in report["field_parameters"]["consumed"]
    assert "Metric" in report["field_parameters"]["tables"]
    model = parts["definition/model.tmdl"]
    assert "ref table 'Metric'" in model or "ref table Metric" in model
    assert "Metric" not in (parts.get("definition/relationships.tmdl") or "")


def test_assemble_import_model_unresolvable_swap_stays_stub():
    # a swap whose branch fields don't resolve to model columns is NOT converted; the calc falls
    # through normal translation and lands as a preserved `= 0` stub (faithful-or-stub).
    calcs = [
        {"name": "Metric", "formula": "CASE [Parameters].[m] WHEN 1 THEN [Nope1] WHEN 2 THEN [Nope2] END"},
    ]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore", calcs=calcs)
    parts, report = out["parts"], out["report"]
    assert "definition/tables/Metric.tmdl" not in parts
    measures = parts["definition/tables/_Measures.tmdl"]
    assert "measure Metric = 0" in measures
    assert "CASE [Parameters].[m] WHEN 1 THEN [Nope1] WHEN 2 THEN [Nope2] END" in measures
    assert report["field_parameters"]["consumed"] == []
    statuses = {r["measure"]: r["status"] for r in report["measures"]}
    assert statuses["Metric"] == "stub"


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


# -- value / what-if parameters (scalar param used inside a calc) ----------------------------
from calc_to_dax import translate_tableau_calc_to_dax  # noqa: E402


def _num_param(caption="Sales Multiplier", internal="[Parameter 7]", default="19.470149254",
               mn="1.0", mx="100.0", step=None, fmt="n#,##0.00"):
    return {"caption": caption, "internal_name": internal, "datatype": "real", "domain": "range",
            "default": default, "format": fmt, "range": {"min": mn, "max": mx, "step": step},
            "members": [], "aliases": {}}


def _str_param(caption="Segment Parameter", internal="[Parameter 2]", default='"Consumer"',
               members=("Consumer", "Corporate", "Home Office")):
    return {"caption": caption, "internal_name": internal, "datatype": "string", "domain": "list",
            "default": default, "format": None, "range": None, "members": list(members),
            "aliases": {}}


def _date_param(caption="Start Date", internal="[Parameter 5]", default="#2020-01-01#"):
    # Mirrors the real Salesforce "Start Date" / "End Date": a free date picker (no member list),
    # datatype date, default a Tableau ``#YYYY-MM-DD#`` literal.
    return {"caption": caption, "internal_name": internal, "datatype": "date", "domain": "range",
            "default": default, "format": None, "range": None, "members": [], "aliases": {}}


def test_canon_num_key_trailing_dot():
    # Tableau serializes integer alias keys with a trailing dot; they must canonicalize by value.
    assert P._canon_num_key("1.") == P._canon_num_key("1") == P._canon_num_key("1.0") == "1"
    assert P._canon_num_key("Consumer") == "consumer"


def test_on_grid():
    assert P._on_grid(50, 1, 100, 1) is True
    assert P._on_grid(19.47, 1, 100, 1) is False      # off the integer grid
    assert P._on_grid(150, 1, 100, 1) is False         # out of range
    assert P._on_grid(None, 1, 100, 1) is True         # no usable default -> nothing to union


def test_parse_parameters_captures_format():
    xml = ('<workbook><datasource name="Parameters"><column name="[P]" caption="Rate" '
           'datatype="real" param-domain-type="range" value="0.1" default-format="p0.00%">'
           '<range min="0" max="1"/></column></datasource></workbook>')
    assert P.parse_parameters(xml)[0]["format"] == "p0.00%"


def test_emit_value_parameter_numeric_whatif():
    calc = {"name": "Adj Sales", "role": "measure",
            "formula": "SUM([Sales]) * [Parameters].[Sales Multiplier]"}
    res = P.emit_value_parameters([_num_param()], calcs=[calc],
                                  reserved_names={"orders", "sales"})
    assert res["table_names"] == ["Sales Multiplier"]        # no collision -> bare caption
    assert res["measure_names"] == ["Sales Multiplier Value"]
    _fn, tmdl = res["parts"][0]
    assert "GENERATESERIES(1.0, 100.0, 1.0)" in tmdl
    assert 'ROW("Value", 19.47014925)' in tmdl               # off-grid default unioned in
    assert "sourceColumn: [Value]" in tmdl                   # physical partition column is "Value"
    assert "SELECTEDVALUE('Sales Multiplier'[Sales Multiplier], 19.47014925)" in tmdl
    assert res["param_resolver"]("Sales Multiplier") == ("[Sales Multiplier Value]", "number")
    assert res["param_resolver"]("Parameter 7") == ("[Sales Multiplier Value]", "number")   # by internal name


def test_emit_value_parameter_string_slicer():
    calc = {"name": "Seg Filter", "role": "dimension",
            "formula": "IF [Segment] = [Parameters].[Segment Parameter] THEN True ELSE False END"}
    res = P.emit_value_parameters([_str_param()], calcs=[calc], reserved_names=set())
    assert res["table_names"] == ["Segment Parameter"]
    _fn, tmdl = res["parts"][0]
    assert "DATATABLE(" in tmdl
    assert all(s in tmdl for s in ('"Consumer"', '"Corporate"', '"Home Office"'))
    assert "SELECTEDVALUE(" in tmdl                          # default fallback is "Consumer"


def test_emit_value_parameter_date_whatif():
    # A DATE parameter is a free picker: a DISCONNECTED single-column date table spanning the model's
    # own dates (CALENDARAUTO) + a SELECTEDVALUE capture measure defaulting to the Tableau #...# date.
    calc = {"name": "In Range", "role": "dimension",
            "formula": "[Closed Date] >= [Parameters].[Start Date]"}
    res = P.emit_value_parameters([_date_param()], calcs=[calc], reserved_names=set())
    assert res["table_names"] == ["Start Date"]
    assert res["measure_names"] == ["Start Date Value"]
    _fn, tmdl = res["parts"][0]
    assert "CALENDARAUTO()" in tmdl                          # disconnected model-date domain
    assert "dataType: dateTime" in tmdl
    assert "sourceColumn: [Date]" in tmdl                    # CALENDARAUTO's column is named "Date"
    assert "SELECTEDVALUE('Start Date'[Start Date], DATE(2020, 1, 1))" in tmdl
    # the capture measure is resolvable by caption AND bracket-less internal name
    assert res["param_resolver"]("Start Date") == ("[Start Date Value]", "date")
    assert res["param_resolver"]("Parameter 5") == ("[Start Date Value]", "date")


def test_emit_value_parameter_datetime_default_carries_time():
    p = _date_param(caption="Cutoff", internal="[Parameter 9]", default="#2021-03-31 13:30:00#")
    calc = {"name": "c", "role": "measure", "formula": "[Parameters].[Cutoff]"}
    res = P.emit_value_parameters([p], calcs=[calc], reserved_names=set())
    _fn, tmdl = res["parts"][0]
    assert "SELECTEDVALUE('Cutoff'[Cutoff], DATE(2021, 3, 31) + TIME(13, 30, 0))" in tmdl


def test_emit_value_parameter_date_unparseable_default_stubs():
    # No usable Tableau date literal -> fail closed (no table, no guessed anchor); the calc stubs.
    p = _date_param(default="")
    calc = {"name": "c", "role": "measure", "formula": "[Parameters].[Start Date]"}
    res = P.emit_value_parameters([p], calcs=[calc], reserved_names=set())
    assert res["table_names"] == []
    assert res["param_resolver"]("Start Date") is None
    assert any("not a representable value" in w for w in res["warnings"])


def test_option_d_date_param_vs_date_column_type_checks_end_to_end():
    # Option D across BOTH halves: the REAL param_resolver from emit_value_parameters returns a
    # ("[Start Date Value]", "date") tuple, and the translator uses that dtype to type-check the
    # comparison. A date column vs the date param is comparable -> the consuming COUNTD-IF inlines
    # its date-window body and translates; a TEXT column vs the date param is incomparable -> the
    # inlined body rejects and the consumer stays a fail-closed stub (never a guess).
    from calc_to_dax import translate_tableau_calc_to_dax
    body_calc = {"name": "Date Window", "role": "dimension",
                 "formula": "[Order Date] >= [Parameters].[Start Date]"}
    res = P.emit_value_parameters([_date_param()], calcs=[body_calc], reserved_names=set())
    presolve = res["param_resolver"]
    assert presolve("Start Date") == ("[Start Date Value]", "date")

    resolver = {"Order Date": ("Orders", "Order_Date", "dateTime"),
                "Region": ("Orders", "Region", "string")}.get
    # date column vs date param -> comparable -> the COUNTD-IF consumer translates
    dax, reason, _ = translate_tableau_calc_to_dax(
        "COUNTD(IF [Date Window] THEN [Region] END)", resolver,
        param_resolver=presolve, known_tables={"Orders"},
        inline_calcs={"date window": "[Order Date] >= [Parameters].[Start Date]"})
    assert reason == "ok"
    assert "[Start Date Value]" in dax
    assert "'Orders'[Order_Date] >= [Start Date Value]" in dax
    # TEXT column vs date param -> incomparable -> fail closed, no unfaithful DAX
    dax2, _reason2, _ = translate_tableau_calc_to_dax(
        "COUNTD(IF [Bad Window] THEN [Region] END)", resolver,
        param_resolver=presolve, known_tables={"Orders"},
        inline_calcs={"bad window": "[Region] >= [Parameters].[Start Date]"})
    assert dax2 is None


def test_emit_value_parameter_name_collision_gets_suffix():
    # A param caption equal to an existing measure name must be pushed to "<caption> Parameter"
    # (Tabular forbids a measure name equal to any column name anywhere in the model).
    calc = {"name": "x", "role": "measure",
            "formula": "SUM([Sales]) * [Parameters].[Sales Multiplier]"}
    res = P.emit_value_parameters([_num_param()], calcs=[calc],
                                  reserved_names={"sales multiplier"})
    assert res["table_names"] == ["Sales Multiplier Parameter"]
    assert res["measure_names"] == ["Sales Multiplier Value"]


def test_emit_value_parameter_only_referenced_params():
    # A param referenced by NO provided calc is skipped (keeps the model lean; a swap controller
    # whose only references are consumed swaps is naturally excluded this way).
    res = P.emit_value_parameters(
        [_num_param(caption="Unused", internal="[Parameter 9]")],
        calcs=[{"name": "m", "role": "measure", "formula": "SUM([Sales])"}], reserved_names=set())
    assert res["parts"] == [] and res["table_names"] == []


def test_emit_value_parameter_reports_consumed_source_params():
    # Additive: the emitter records WHICH source parameters became what-if tables so the model
    # manifest can tag them kind="value" (model-owned) rather than a dashboard slicer.
    calc = {"name": "Adj Sales", "role": "measure",
            "formula": "SUM([Sales]) * [Parameters].[Sales Multiplier]"}
    res = P.emit_value_parameters([_num_param()], calcs=[calc],
                                  reserved_names={"orders", "sales"})
    assert res["consumed_params"] == [
        {"caption": "Sales Multiplier", "internal_name": "[Parameter 7]",
         "table": "Sales Multiplier", "measure": "Sales Multiplier Value",
         "picker_column": "Sales Multiplier"}]
    # a param that is NOT representable as a value control is not reported consumed
    res2 = P.emit_value_parameters(
        [_num_param(caption="Unused", internal="[Parameter 9]")],
        calcs=[{"name": "m", "role": "measure", "formula": "SUM([Sales])"}], reserved_names=set())
    assert res2["consumed_params"] == []


def _numlist_param(caption="Date Selection", internal="[Parameter 0014172370878491]",
                   members=("15.", "30.", "41."), default="15.", aliases="__default__"):
    # Mirrors the real Comcast "Date Selection": a real-typed LIST param whose integer-ish members
    # (Tableau serializes the keys with a trailing dot) carry friendly display aliases.
    if aliases == "__default__":
        aliases = {"15.": "Current Orders", "30.": "Previous Orders", "41.": "All Orders"}
    return {"caption": caption, "internal_name": internal, "datatype": "real", "domain": "list",
            "default": default, "format": None, "range": None, "members": list(members),
            "aliases": aliases}


def test_emit_value_parameter_numeric_list_aliases_datatable_with_labels():
    # A discrete numeric LIST param (Tableau's aliased {15,30,41} "Date Selection") must become a
    # DATATABLE of EXACTLY its members -- never a GENERATESERIES range -- with a friendly Label
    # column a slicer can show, while the value measure still reads the underlying number.
    calc = {"name": "Date Filter", "role": "measure",
            "formula": "CASE [Parameters].[Date Selection] WHEN 15 THEN 1 END"}
    res = P.emit_value_parameters([_numlist_param()], calcs=[calc], reserved_names=set())
    assert res["table_names"] == ["Date Selection"]
    _fn, tmdl = res["parts"][0]
    assert "GENERATESERIES" not in tmdl                       # NOT a contiguous range
    assert 'DATATABLE("Value", DOUBLE, "Label", STRING' in tmdl
    for v in ("15.0", "30.0", "41.0"):
        assert v in tmdl
    for lbl in ("Current Orders", "Previous Orders", "All Orders"):
        assert ('"' + lbl + '"') in tmdl
    # two real columns: the typed value + the friendly label (slicer shows the label)
    assert "column 'Date Selection'" in tmdl
    assert "column 'Date Selection Label'" in tmdl
    assert "sourceColumn: [Value]" in tmdl and "sourceColumn: [Label]" in tmdl
    # the value measure reads the VALUE column (not the label), defaulting to the Tableau default
    assert "SELECTEDVALUE('Date Selection'[Date Selection], 15.0)" in tmdl
    # the model manifest learns the slicer should bind to the friendly Label column
    assert res["consumed_params"] == [
        {"caption": "Date Selection", "internal_name": "[Parameter 0014172370878491]",
         "table": "Date Selection", "measure": "Date Selection Value",
         "picker_column": "Date Selection Label"}]


def test_emit_value_parameter_numeric_list_without_aliases_single_column():
    # Same discrete-members rule, but with no aliases -> a single value column; the slicer binds to
    # it directly (no Label column synthesized).
    p = _numlist_param(caption="Threshold", internal="[Parameter T]",
                       members=("10", "20", "30"), default="20", aliases={})
    p["datatype"] = "integer"
    calc = {"name": "Gate", "role": "measure",
            "formula": "IF [Sales] > [Parameters].[Threshold] THEN 1 END"}
    res = P.emit_value_parameters([p], calcs=[calc], reserved_names=set())
    _fn, tmdl = res["parts"][0]
    assert "GENERATESERIES" not in tmdl
    assert 'DATATABLE("Value", INTEGER, {{10}, {20}, {30}})' in tmdl
    assert "column 'Threshold Label'" not in tmdl             # no aliases -> no second column
    assert "SELECTEDVALUE('Threshold'[Threshold], 20)" in tmdl
    assert res["consumed_params"][0]["picker_column"] == "Threshold"


def _sales_resolver(field):
    return {"sales": ("Orders", "Sales", "double")}.get((field or "").strip().lower())


def test_value_param_resolver_inlines_into_measure():
    calc = {"name": "Adj Sales", "role": "measure",
            "formula": "SUM([Sales]) * [Parameters].[Sales Multiplier]"}
    res = P.emit_value_parameters([_num_param()], calcs=[calc],
                                  reserved_names={"orders", "sales"})
    dax, _reason, tables = translate_tableau_calc_to_dax(
        calc["formula"], _sales_resolver, param_resolver=res["param_resolver"])
    assert dax == "SUM('Orders'[Sales]) * [Sales Multiplier Value]"
    assert tables == {"Orders"}             # the disconnected param table is NOT added to tables_used


def test_value_param_without_resolver_stubs():
    dax, reason, _ = translate_tableau_calc_to_dax(
        "SUM([Sales]) * [Parameters].[X]", _sales_resolver)
    assert dax is None and "parameter reference" in reason


def test_param_collapses_inside_value_preserving_aggregation():
    # A parameter is a scalar; a value-preserving aggregate over that singleton (SUM/MIN/MAX/AVG/
    # MEDIAN) equals the scalar, so it collapses to the same SELECTEDVALUE param measure the bare
    # scalar position emits (Tableau authors wrap a param in an aggregate only as a measure-context
    # formality).
    dax, reason, _ = translate_tableau_calc_to_dax(
        "SUM([Parameters].[X])", _sales_resolver, param_resolver=lambda n: "[X Value]")
    assert reason == "ok"
    assert dax == "[X Value]"


def test_param_not_resolved_inside_counting_aggregation():
    # COUNT of a singleton = 1 (not the value) -> a counting/spread aggregate over a param must
    # still stub even WITH a resolver (the fail-closed guard the collapse deliberately excludes).
    dax, _reason, _ = translate_tableau_calc_to_dax(
        "COUNT([Parameters].[X])", _sales_resolver, param_resolver=lambda n: "[X Value]")
    assert dax is None


def test_value_params_merge_into_directlake_model():
    # Value-param tables are injected (before _Measures, never related) just like field params.
    res = P.emit_value_parameters(
        [_num_param()],
        calcs=[{"name": "Adj Sales", "role": "measure",
                "formula": "SUM([Sales]) * [Parameters].[Sales Multiplier]"}],
        reserved_names={"orders"})
    out = assemble_directlake_model(
        model_name="DL", tables=[("Orders", "ds_orders", "")], measures_tmdl="",
        expression_name="DL", directlake_url="https://x", field_parameters=res)
    parts = out["parts"]
    assert "definition/tables/Sales Multiplier.tmdl" in parts
    model = parts["definition/model.tmdl"]
    assert "ref table 'Sales Multiplier'" in model
    assert "Sales Multiplier" not in parts.get("definition/relationships.tmdl", "")


# -- orchestrator wiring: assemble_import_model wires swaps + value params end-to-end ---------
def test_assemble_import_model_full_parameter_wiring():
    descriptor = parse_tds(LIVE_SQLSERVER)
    params = [
        {"caption": "Measure Picker", "internal_name": "[mp]", "datatype": "string",
         "domain": "list", "default": "1", "format": None, "range": None,
         "members": ["1", "2"], "aliases": {"1": "Total Sales", "2": "Units"}},
        {"caption": "Dim Selector", "internal_name": "[ds]", "datatype": "string",
         "domain": "list", "default": "1", "format": None, "range": None,
         "members": ["1", "2"], "aliases": {"1": "By Order", "2": "By Sales"}},
        _num_param(caption="Sales Multiplier", internal="[sm]", default="1.0",
                   mn="0.0", mx="2.0", step="0.1", fmt=None),
    ]
    calcs = [
        {"name": "Boost", "formula": "SUM([Sales]) * [Parameters].[Sales Multiplier]"},
        {"name": "Measure Swap",
         "formula": "CASE [Parameters].[Measure Picker] WHEN 1 THEN [Sales] WHEN 2 THEN [Quantity] END"},
    ]
    dim_calcs = [
        {"name": "Dim Swap", "role": "dimension",
         "formula": "CASE [Parameters].[Dim Selector] WHEN 1 THEN [Order ID] WHEN 2 THEN [Sales] END"},
        {"name": "Seg Flag", "role": "dimension",
         "formula": "[Parameters].[Sales Multiplier] > 1"},
    ]
    out = assemble_import_model(descriptor, model_name="Superstore",
                               calcs=calcs, dim_calcs=dim_calcs, parameters=params)
    parts, report = out["parts"], out["report"]

    # measure swap -> field-parameter table; NAMEOF targets are synthesized aggregating measures
    ms = parts["definition/tables/Measure Swap.tmdl"]
    assert '("Total Sales", NAMEOF([Total Sales]), 0)' in ms
    assert '("Units", NAMEOF([Total Quantity]), 1)' in ms

    # dimension swap -> its own field-parameter table with aliased labels
    ds = parts["definition/tables/Dim Swap.tmdl"]
    assert '"By Order"' in ds and '"By Sales"' in ds

    # both swaps are CONSUMED: not also emitted as a measure
    measures = parts["definition/tables/_Measures.tmdl"]
    assert "measure Measure Swap" not in measures
    assert sorted(report["field_parameters"]["consumed"]) == ["Dim Swap", "Measure Swap"]

    # the synthesized SUM measures backing the measure swap land in _Measures and are reported
    assert "SUM('Orders'[Sales])" in measures
    assert "SUM('Orders'[Quantity])" in measures
    assert report["field_parameters"]["measures"] == ["Total Sales", "Total Quantity"]

    # what-if value param -> disconnected table + measure; Boost inlines the value measure
    assert "definition/tables/Sales Multiplier.tmdl" in parts
    assert report["value_parameters"]["tables"] == ["Sales Multiplier"]
    assert report["value_parameters"]["measures"] == ["Sales Multiplier Value"]
    assert "SUM('Orders'[Sales]) * [Sales Multiplier Value]" in measures

    # a row-level parameter reference (Seg Flag) correctly stays an inert stub -- a calculated
    # column has no filter context for SELECTEDVALUE -- and is NOT consumed as a field parameter
    col_status = {r["column"]: r["status"] for r in report["calc_columns"]}
    assert col_status["Seg Flag"] == "stub"
    assert "Seg Flag" not in report["field_parameters"]["consumed"]

    # every parameter table is disconnected (never wired into relationships)
    rels = parts.get("definition/relationships.tmdl", "")
    for nm in ("Measure Swap", "Dim Swap", "Sales Multiplier"):
        assert nm not in rels


def test_assemble_import_model_no_parameters_is_unchanged():
    # The wiring is inert without parameters/swaps: no extra tables are added beyond the data
    # tables + _Measures, the additive report keys are present but empty, and normal calcs still
    # translate. (A full byte compare isn't meaningful -- each table carries a random lineageTag.)
    descriptor = parse_tds(LIVE_SQLSERVER)
    calcs = [{"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"}]
    out = assemble_import_model(descriptor, model_name="S", calcs=calcs, parameters=[])
    parts, report = out["parts"], out["report"]
    table_parts = {p for p in parts if p.startswith("definition/tables/")}
    assert table_parts == {"definition/tables/Orders.tmdl", "definition/tables/_Measures.tmdl"}
    assert report["field_parameters"]["count"] == 0
    assert report["value_parameters"]["count"] == 0
    assert report["field_parameters"]["consumed"] == []
    statuses = {r["measure"]: r["status"] for r in report["measures"]}
    assert statuses["Profit Ratio"] == "translated"


def test_migrate_datasource_auto_parses_and_wires_parameters():
    # End-to-end through the auto-extracting one-call entry: a workbook carrying a Parameters
    # pseudo-datasource + a measure-swap calc auto-wires the swap to a field parameter (no explicit
    # calcs= or parameters= needed).
    xml = """<?xml version='1.0' encoding='utf-8'?>
    <workbook>
      <datasource name='Parameters'>
        <column name='[mp]' caption='Measure Picker' datatype='integer'
                param-domain-type='list' value='1'>
          <members><member value='1'/><member value='2'/></members>
          <aliases><alias key='1' value='Total Sales'/><alias key='2' value='Units'/></aliases>
        </column>
      </datasource>
      <datasource formatted-name='Superstore' inline='true' version='18.1'>
        <connection class='federated'>
          <named-connections>
            <named-connection caption='myserver' name='sqlserver.0a1b2c'>
              <connection authentication='sqlserver' class='sqlserver' dbname='Superstore'
                          server='myserver.database.windows.net' username='svc'/>
            </named-connection>
          </named-connections>
          <relation connection='sqlserver.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table'/>
          <metadata-records>
            <metadata-record class='column'><remote-name>Sales</remote-name>
              <local-name>[Sales]</local-name><parent-name>[Orders]</parent-name>
              <local-type>real</local-type></metadata-record>
            <metadata-record class='column'><remote-name>Quantity</remote-name>
              <local-name>[Quantity]</local-name><parent-name>[Orders]</parent-name>
              <local-type>integer</local-type></metadata-record>
          </metadata-records>
        </connection>
        <column caption='Measure Swap' name='[Calculation_1]' role='measure'>
          <calculation class='tableau'
            formula='CASE [Parameters].[Measure Picker] WHEN 1 THEN [Sales] WHEN 2 THEN [Quantity] END'/>
        </column>
      </datasource>
    </workbook>"""
    out = migrate_datasource(xml, model_name="Superstore", datasource="Superstore")
    parts, report = out["parts"], out["report"]
    assert "definition/tables/Measure Swap.tmdl" in parts
    assert "Measure Swap" in report["field_parameters"]["consumed"]
    assert "Total Sales" in parts["definition/tables/Measure Swap.tmdl"]
