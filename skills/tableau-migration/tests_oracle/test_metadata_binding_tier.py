"""Quarantined tests for the deterministic metadata / binding fidelity tier of ``fidelity_oracle``.

Why this module exists (Tier 3 -- a fail-loud PRE-FIRE gate)
-----------------------------------------------------------
The recent estate blocker was a CROSS-TABLE / UNRESOLVABLE-REFERENCE class: a DAX body (a measure,
a calculated column, or a window navigation such as ``OFFSET`` / ``WINDOW`` / ``INDEX``) referenced a
column that is not actually in the emitted model -- either a sanitized-name mismatch
(``'Orders'[State/Province]`` landing over a physical ``State_Province``) or an ORDERBY / relation
argument pointing at the wrong table. Such a model can still DESERIALIZE (Gate 0 says "opens"), yet
every query against the bad measure errors at refresh time. The openability gate cannot see it; only
a static binding resolution against the real column inventory can, BEFORE any live ADOMD fire.

These tests are deliberately OUTSIDE ``tests/`` so the engine's ``pytest tests`` green gate never
collects them. They are hermetic: every model + Tableau schema is built inline under ``tmp_path``.
"""
import os

import pytest

import fidelity_oracle as fo


# --------------------------------------------------------------------------- model/schema builders
def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _tmdl_table(name, columns=(), measures=(), calc_columns=(), partition_source=None):
    """Render one ``.tmdl`` table file. ``columns`` = ``(name, dtype, sourceColumn)`` tuples."""
    q = name if " " not in name else "'%s'" % name
    lines = ["table %s" % q, "\tlineageTag: 00000000-0000-0000-0000-000000000000", ""]
    for cname, dtype, src in columns:
        cq = cname if " " not in cname and "/" not in cname else "'%s'" % cname
        lines += ["\tcolumn %s" % cq, "\t\tdataType: %s" % dtype,
                  "\t\tsummarizeBy: none", "\t\tsourceColumn: %s" % src, ""]
    for cname, expr in calc_columns:
        cq = cname if " " not in cname else "'%s'" % cname
        lines += ["\tcolumn %s = %s" % (cq, expr),
                  "\t\tlineageTag: 00000000-0000-0000-0000-000000000001", ""]
    for mname, expr in measures:
        mq = mname if " " not in mname else "'%s'" % mname
        lines += ["\tmeasure %s = %s" % (mq, expr),
                  "\t\tlineageTag: 00000000-0000-0000-0000-000000000002", ""]
    if partition_source is not None:
        lines += ["\tpartition %s = calculated" % q, "\t\tmode: import", "\t\tsource = %s"
                  % partition_source, ""]
    return "\n".join(lines) + "\n"


def _write_model(tmp_path, tables, name="M"):
    """Write a ``<name>.SemanticModel/definition/tables/*.tmdl`` tree; return the model_dir."""
    model_dir = os.path.join(str(tmp_path), "%s.SemanticModel" % name)
    tdir = os.path.join(model_dir, "definition", "tables")
    for tname, spec in tables.items():
        _write(os.path.join(tdir, "%s.tmdl" % tname), _tmdl_table(tname, **spec))
    return model_dir


def _write_twb(tmp_path, columns, name="src.twb"):
    """``columns`` = ``(caption, datatype, role)`` tuples -> a minimal datasource .twb."""
    rows = "".join(
        "      <column name='[%s]' caption='%s' datatype='%s' role='%s'/>\n"
        % (cap, cap, dt, role) for cap, dt, role in columns)
    xml = ("<?xml version='1.0'?>\n<workbook>\n  <datasources>\n"
           "    <datasource name='fed.0' caption='Sample'>\n%s"
           "    </datasource>\n  </datasources>\n</workbook>\n" % rows)
    path = os.path.join(str(tmp_path), name)
    _write(path, xml)
    return path


# A reusable faithful model: an Orders fact (sanitized State_Province), a Calendar dim, _Measures.
def _orders_model(tmp_path, measures=(), calc_columns=(), partition_source=None):
    tables = {
        "Orders": {"columns": [
            ("Sales", "double", "Sales"), ("City", "string", "City"),
            ("State_Province", "string", "State_Province"),
            ("Order_Date", "dateTime", "Order_Date"), ("Region", "string", "Region")]},
        "Calendar": {"columns": [("Date", "dateTime", "Date")]},
        "_Measures": {"columns": [("Value", "string", "[Value]")],
                      "measures": list(measures), "calc_columns": list(calc_columns)},
    }
    if partition_source is not None:
        tables["Dim"] = {"columns": [("K", "string", "[Value1]")],
                         "partition_source": partition_source}
    return _write_model(tmp_path, tables)


# =========================================================================== unit: small helpers
def test_tmdl_unquote_and_assignment():
    assert fo._tmdl_unquote("'Dim Swap Calc 1'") == "Dim Swap Calc 1"
    assert fo._tmdl_unquote("Orders") == "Orders"
    # a header carrying an expression is an assignment; a quoted name with an internal '=' is not
    assert fo._looks_like_assignment("'Total Sales' = SUM('Orders'[Sales])") is True
    assert fo._looks_like_assignment("'A = B'") is False
    assert fo._looks_like_assignment("Row_ID") is False


def test_extract_dax_refs_forms():
    refs = list(fo._extract_dax_refs(
        "SUM('Orders'[Sales]) + Orders[City] + [Total] + NAMEOF('Calendar'[Date])"))
    assert ("Orders", "Sales") in refs
    assert ("Orders", "City") in refs
    assert (None, "Total") in refs
    assert ("Calendar", "Date") in refs


def test_map_tableau_dtype_and_derived_helpers():
    assert fo._map_tableau_dtype("integer") == "int64"
    assert fo._map_tableau_dtype("DATE") == "dateTime"
    assert fo._map_tableau_dtype("nonsense") is None
    assert fo._is_derived_source_name("Sales (copy)") is True
    assert fo._is_derived_source_name("Calculation_123456") is True
    assert fo._is_derived_source_name("Sales") is False
    assert fo._strip_trailing_paren("Region (People)") == "Region"
    assert fo._strip_trailing_paren("Sales") == "Sales"


def test_parse_tableau_schema_reads_columns(tmp_path):
    twb = _write_twb(tmp_path, [("Sales", "real", "measure"), ("City", "string", "dimension"),
                                ("My Calc", "real", "measure")])
    schema = fo._parse_tableau_schema(twb)
    assert schema[fo._norm("Sales")]["datatype"] == "real"
    assert schema[fo._norm("City")]["datatype"] == "string"


def test_parse_tmdl_model_classifies_objects(tmp_path):
    model_dir = _orders_model(
        tmp_path,
        measures=[("Total Sales", "SUM('Orders'[Sales])")],
        calc_columns=[],
    )
    defn = fo._resolve_model_definition(model_dir=model_dir)
    model = fo._parse_tmdl_model(defn)
    assert set(model["tables"]) == {"Orders", "Calendar", "_Measures"}
    orders = model["tables"]["Orders"]
    names = {c["name"] for c in orders["physical_columns"]}
    assert {"Sales", "State_Province", "Order_Date"} <= names
    assert model["tables"]["_Measures"]["measures"][0]["name"] == "Total Sales"


# =========================================================================== tier: faithful base
def test_metadata_tier_unavailable_without_model(tmp_path):
    res = fo.metadata_tier(model_dir=os.path.join(str(tmp_path), "nope"))
    assert res["available"] is False
    assert "no *.SemanticModel" in res["reason"]


def test_metadata_tier_faithful_model_scores_clean(tmp_path):
    model_dir = _orders_model(tmp_path, measures=[
        ("Total Sales", "SUM('Orders'[Sales])"),
        ("West Sales", "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Region] = \"West\")")])
    twb = _write_twb(tmp_path, [("Sales", "real", "measure"), ("City", "string", "dimension"),
                                ("State Province", "string", "dimension"),
                                ("Order Date", "date", "dimension"),
                                ("Region", "string", "dimension"), ("Date", "date", "dimension")])
    res = fo.metadata_tier(model_dir=model_dir, twb_path=twb)
    assert res["available"] is True
    assert res["unresolved_bindings"] == []
    assert res["scores"]["binding"] == 1.0
    assert res["scores"]["metadata"] == 1.0
    assert res["datatype_drift"] == []


# =========================================================================== tier: datatype + coverage
def test_metadata_tier_flags_datatype_drift(tmp_path):
    # Orders.Sales emitted as int64, but the Tableau source says it is a string -> incompatible drift.
    tables = {"Orders": {"columns": [("Sales", "int64", "Sales")]}}
    model_dir = _write_model(tmp_path, tables)
    twb = _write_twb(tmp_path, [("Sales", "string", "dimension")])
    res = fo.metadata_tier(model_dir=model_dir, twb_path=twb)
    drift = res["datatype_drift"]
    assert len(drift) == 1
    assert drift[0]["column"] == "Sales"
    assert drift[0]["emitted_type"] == "int64"
    assert drift[0]["expected_type"] == "string"


def test_metadata_tier_reports_missing_and_extra_columns(tmp_path):
    # Model has Sales + Mystery; source has Sales + Dropped. -> missing=Dropped, extra=Mystery.
    tables = {"Orders": {"columns": [("Sales", "double", "Sales"),
                                     ("Mystery", "string", "Mystery")]}}
    model_dir = _write_model(tmp_path, tables)
    twb = _write_twb(tmp_path, [("Sales", "real", "measure"), ("Dropped", "string", "dimension")])
    res = fo.metadata_tier(model_dir=model_dir, twb_path=twb)
    missing = {m["name"] for m in res["missing_source_columns"]}
    extra = {e["column"] for e in res["extra_model_columns"]}
    assert "Dropped" in missing
    assert "Mystery" in extra


def test_metadata_tier_coverage_sees_through_cosmetic_and_blend_names(tmp_path):
    # Order_Date <-> "Order Date" (cosmetic) and "Region (People)" <-> Region (blend suffix) must
    # NOT read as missing; a Tableau group/copy field is derived and never a coverage gap.
    tables = {"Orders": {"columns": [("Order_Date", "dateTime", "Order_Date"),
                                     ("Region", "string", "Region")]}}
    model_dir = _write_model(tmp_path, tables)
    twb = _write_twb(tmp_path, [("Order Date", "date", "dimension"),
                                ("Region (People)", "string", "dimension"),
                                ("Sales (copy)", "real", "measure")])
    res = fo.metadata_tier(model_dir=model_dir, twb_path=twb)
    assert res["missing_source_columns"] == []


# =========================================================================== TIER 3: OFFSET/WINDOW
# Each case documents (a) the input DAX measure body, (b) expected resolver behavior, (c) the rule
# violated, (d) how it validates locally. ``flagged`` is whether the body should produce at least one
# unresolved binding; ``window`` asserts the unresolved entry is tagged as a window-function ref.
#
# Model inventory for these cases (see ``_orders_model``):
#   Orders[Sales, City, State_Province, Order_Date, Region]  ·  Calendar[Date]  ·  _Measures[...]
_WINDOW_CASES = [
    pytest.param(
        "CALCULATE(SUM('Orders'[Sales]), OFFSET(-1, ORDERBY('Orders'[OrderDate])))",
        True, True,
        id="offset_orderby_typo_column_not_in_table",
        # (a) OFFSET ORDERBY over 'Orders'[OrderDate]; (b) UNRESOLVED -- Orders has Order_Date, not
        # OrderDate; (c) a window ORDERBY must name a real model column; (d) a query would error
        # "column OrderDate cannot be found" -- caught statically here.
    ),
    pytest.param(
        "SUMX('Orders', CALCULATE(SUM('Orders'[Sales]), "
        "WINDOW(0, ABS, 0, REL, ORDERBY('Orders'[State/Province]))))",
        True, True,
        id="window_orderby_sanitized_name_state_province",
        # (a) WINDOW ORDERBY over the Tableau spelling 'Orders'[State/Province]; (b) UNRESOLVED -- the
        # physical column was sanitized to State_Province; (c) DAX is literal, '/' != '_'; (d) this is
        # the exact recent estate defect -- the model opens but the measure errors at refresh.
    ),
    pytest.param(
        "CALCULATE(SUM('Orders'[Sales]), OFFSET(-1, ORDERBY('DateDim'[Date])))",
        True, True,
        id="offset_orderby_unknown_table",
        # (a) OFFSET ORDERBY over 'DateDim'[Date]; (b) UNRESOLVED -- there is no DateDim table (the
        # date dim is Calendar); (c) a cross-table window arg must target a table in the model; (d) a
        # refresh would fail to bind the table.
    ),
    pytest.param(
        "INDEX(1, ORDERBY([NonexistentMeasure]))",
        True, True,
        id="index_bare_ref_not_a_column_or_measure",
        # (a) INDEX ORDERBY over a bare [NonexistentMeasure]; (b) UNRESOLVED -- not a column or a
        # measure anywhere; (c) a bare ref must resolve to a measure or an in-context column; (d) the
        # measure cannot be evaluated locally.
    ),
    pytest.param(
        "CALCULATE(SUM('Orders'[Sales]), WINDOW(0, ABS, 0, REL, ORDERBY('Calendar'[Date])))",
        False, False,
        id="window_orderby_valid_cross_table_column",
        # (a) WINDOW ORDERBY over 'Calendar'[Date], a real cross-table column; (b) RESOLVES -- a
        # faithful window over an existing dim; (c) none; (d) positive control: a correct window must
        # NOT be flagged, or the gate is useless (no false alarms on valid cross-table refs).
    ),
    pytest.param(
        "RANKX(ALLSELECTED('Orders'[City]), CALCULATE(SUM('Orders'[Sales])))",
        False, False,
        id="rankx_all_columns_resolve",
        # (a) RANKX over Orders[City] + Orders[Sales]; (b) RESOLVES; (c) none; (d) positive control
        # for an in-table window-style navigation.
    ),
    pytest.param(
        "SUM('Orders'[Sales])",
        False, False,
        id="plain_measure_resolves",
        # (a) a non-window aggregate; (b) RESOLVES; (c) none; (d) baseline sanity.
    ),
]


@pytest.mark.parametrize("dax, flagged, window", _WINDOW_CASES)
def test_metadata_tier_window_cross_table_cases(tmp_path, dax, flagged, window):
    model_dir = _orders_model(tmp_path, measures=[("Probe", dax)])
    res = fo.metadata_tier(model_dir=model_dir)
    probe = [u for u in res["unresolved_bindings"] if u["object"] == "Probe"]
    if flagged:
        assert probe, "expected an unresolved binding for: %s" % dax
        if window:
            assert any(u["window_function"] for u in probe), \
                "expected a window-function-tagged unresolved ref for: %s" % dax
    else:
        assert probe == [], "expected NO unresolved binding for: %s (got %r)" % (dax, probe)


def test_metadata_tier_object_resolution_score_reflects_one_bad_measure(tmp_path):
    # Two measures: one clean, one with a sanitized-name miss -> 1 of 2 objects resolves.
    model_dir = _orders_model(tmp_path, measures=[
        ("Good", "SUM('Orders'[Sales])"),
        ("Bad", "SUM('Orders'[State/Province])")])
    res = fo.metadata_tier(model_dir=model_dir)
    assert res["objects_total"] == 2
    assert res["objects_resolved"] == 1
    assert res["scores"]["binding"] == 0.5
    bad = [u for u in res["unresolved_bindings"] if u["object"] == "Bad"]
    assert bad and "not found in table 'Orders'" in bad[0]["reason"]


def test_metadata_tier_resolves_calculated_partition_cross_table_refs(tmp_path):
    # A calculated-table partition source that NAMEOFs a real Orders column resolves; a typo does not.
    good = _orders_model(tmp_path, partition_source="{ (\"R\", NAMEOF('Orders'[Region]), 0) }")
    res_ok = fo.metadata_tier(model_dir=good)
    assert [u for u in res_ok["unresolved_bindings"] if u["kind"] == "partition_source"] == []

    bad = _orders_model(tmp_path / "bad", partition_source="{ (\"R\", NAMEOF('Orders'[Regn]), 0) }")
    res_bad = fo.metadata_tier(model_dir=bad)
    part = [u for u in res_bad["unresolved_bindings"] if u["kind"] == "partition_source"]
    assert part and part[0]["ref"] == "'Orders'[Regn]"


def test_metadata_tier_runs_through_run_oracle(tmp_path):
    # End-to-end: run_oracle attaches an additive ``metadata`` record and never raises.
    model_dir = _orders_model(tmp_path, measures=[("Total Sales", "SUM('Orders'[Sales])")])
    # run_oracle needs a twb + report_dir; reuse the model's parent as report_dir (no PBIR pairing
    # needed -- we only assert the metadata tier attaches).
    twb = _write_twb(tmp_path, [("Sales", "real", "measure")])
    report = fo.run_oracle(twb, str(tmp_path), metadata_options={"model_dir": model_dir})
    assert "metadata" in report
    assert report["metadata"]["available"] is True
    assert report["metadata"]["scores"]["binding"] == 1.0
