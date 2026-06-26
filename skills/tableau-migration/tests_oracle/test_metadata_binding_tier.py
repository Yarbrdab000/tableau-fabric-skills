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


# ================================================= TIER 3: RESOLVABLE-but-CROSS-TABLE window ORDERBY
# The harder, real OFFSET/WINDOW defect class: every column reference RESOLVES (so pure binding
# resolution is blind to it), yet a window navigation's ORDERBY column lives in a DIFFERENT table than
# the partition/aggregate the window walks. DAX requires the ORDERBY (and PARTITIONBY) columns to come
# from the same table expression being windowed; a cross-table ORDERBY errors at query/refresh time
# even though the model opens (Gate 0) and every ref binds. These are the cases the recent
# positional-measure fix (ORDERBY redirected from the related date dim back to the fact's own date
# column) was about; the binding tier alone would have passed them, so they get their own signal.
#
# Model inventory (``_orders_model``): Orders[Sales, City, State_Province, Order_Date, Region] is the
# fact; Calendar[Date] is the related date DIM. A window aggregating Orders but ordering by
# 'Calendar'[Date] is the cross-table defect; ordering by 'Orders'[Order_Date] is the faithful form.
_CROSS_WINDOW_CASES = [
    pytest.param(
        "STDEVX.S(WINDOW(1, ABS, -1, ABS, ORDERBY('Calendar'[Date], ASC)), "
        "CALCULATE(COUNTROWS('Orders')))",
        True, id="window_orderby_dim_aggregate_fact_no_partition",
        # (a) WINDOW over COUNTROWS('Orders') but ORDERBY('Calendar'[Date]); (b) every ref resolves,
        # so binding stays clean; (c) the ORDERBY must sit on the windowed table (Orders), not the
        # related dim; (d) locally: window_consistency drops and the finding names Calendar vs Orders.
    ),
    pytest.param(
        "DIVIDE(COUNTROWS('Orders') - CALCULATE(COUNTROWS('Orders'), "
        "OFFSET(-1, ORDERBY('Calendar'[Date], ASC))), 1)",
        True, id="offset_orderby_dim_aggregate_fact_no_partition",
        # OFFSET prior-row over an Orders aggregate ordered by the dim date -> cross-table.
    ),
    pytest.param(
        "COUNTROWS(WINDOW(1, ABS, -1, ABS, ORDERBY('Calendar'[Date], ASC), "
        "PARTITIONBY('Orders'[Region])))",
        True, id="window_orderby_dim_partition_fact",
        # ORDERBY('Calendar'[Date]) but PARTITIONBY('Orders'[Region]) -> order/partition table
        # mismatch, the clearest cross-table signal.
    ),
    pytest.param(
        "STDEVX.S(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC)), "
        "CALCULATE(COUNTROWS('Orders')))",
        False, id="window_orderby_same_table_is_clean",
        # POSITIVE CONTROL (the corrected form): ORDERBY on the fact's own Order_Date -> no finding.
    ),
    pytest.param(
        "COUNTROWS(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC), "
        "PARTITIONBY('Orders'[Region])))",
        False, id="window_order_and_partition_same_table_clean",
        # POSITIVE CONTROL: ORDERBY + PARTITIONBY both on Orders -> faithful, no finding.
    ),
    pytest.param(
        "INDEX(1, ORDERBY([Total Sales]))",
        False, id="bare_orderby_ref_not_flagged",
        # CONSERVATIVE: a bare [..] ORDERBY carries no table -> undecidable -> never flagged (no
        # false alarm), even though a fact aggregate is nowhere in sight.
    ),
    pytest.param(
        "OFFSET(-1, ORDERBY('Orders'[Order_Date] ASC, 'Calendar'[Date] DESC), "
        "PARTITIONBY('Orders'[Region]))",
        True, id="multi_key_orderby_one_cross_table",
        # GOLDEN: a two-key ORDERBY whose first key is the fact's own date (fine) but whose second
        # reaches into the related dim -> the cross-table key is flagged. Every column resolves, so
        # binding stays clean; the sort-direction keywords must not perturb table extraction.
    ),
    pytest.param(
        "COUNTROWS(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date] DESC), "
        "PARTITIONBY('Orders'[Region])))",
        False, id="orderby_desc_same_table_clean",
        # GOLDEN (positive control): a DESC keyword on a single-table ORDERBY + same-table
        # PARTITIONBY -> faithful, no finding.
    ),
]


@pytest.mark.parametrize("dax, flagged", _CROSS_WINDOW_CASES)
def test_metadata_tier_cross_table_window_cases(tmp_path, dax, flagged):
    model_dir = _orders_model(tmp_path, measures=[("Probe", dax)])
    res = fo.metadata_tier(model_dir=model_dir)
    hits = [f for f in res["cross_table_windows"] if f["object"] == "Probe"]
    if flagged:
        assert hits, "expected a cross-table window finding for: %s" % dax
        # the defect resolves -- it must NOT also appear as an unresolved binding
        assert [u for u in res["unresolved_bindings"] if u["object"] == "Probe"] == []
    else:
        assert hits == [], "expected NO cross-table window finding for: %s (got %r)" % (dax, hits)


def test_cross_table_window_does_not_move_binding_or_overall(tmp_path):
    # The separation invariant: a resolvable-but-cross-table window is reported on its OWN channel and
    # must never perturb binding / overall (so the established calibration is untouched).
    clean = _orders_model(tmp_path / "clean", measures=[
        ("Pos", "STDEVX.S(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC)), "
                "CALCULATE(COUNTROWS('Orders')))")])
    cross = _orders_model(tmp_path / "cross", measures=[
        ("Pos", "STDEVX.S(WINDOW(1, ABS, -1, ABS, ORDERBY('Calendar'[Date], ASC)), "
                "CALCULATE(COUNTROWS('Orders')))")])
    rc = fo.metadata_tier(model_dir=clean)
    rx = fo.metadata_tier(model_dir=cross)
    # identical binding inventory -> identical binding + overall, regardless of the window finding
    assert rc["scores"]["binding"] == rx["scores"]["binding"] == 1.0
    assert rc["scores"]["overall"] == rx["scores"]["overall"]
    assert rc["unresolved_bindings"] == rx["unresolved_bindings"] == []
    # but the window-consistency signal DOES separate them
    assert rc["scores"]["window_consistency"] == 1.0
    assert rx["scores"]["window_consistency"] == 0.0
    assert len(rx["cross_table_windows"]) == 1
    f = rx["cross_table_windows"][0]
    assert f["orderby_table"] == "Calendar" and f["anchor_tables"] == ["Orders"]


def test_cross_table_window_finding_shape_and_window_count(tmp_path):
    model_dir = _orders_model(tmp_path, measures=[
        ("Plain", "SUM('Orders'[Sales])"),
        ("Cross", "COUNTROWS(WINDOW(1, ABS, -1, ABS, ORDERBY('Calendar'[Date], ASC), "
                  "PARTITIONBY('Orders'[Region])))")])
    res = fo.metadata_tier(model_dir=model_dir)
    assert res["window_objects_total"] == 1  # only the WINDOW measure counts as a window object
    f = [x for x in res["cross_table_windows"] if x["object"] == "Cross"][0]
    assert set(f) >= {"table", "object", "kind", "orderby_table", "anchor_tables",
                      "partition_tables", "reason"}
    assert f["partition_tables"] == ["Orders"]
    assert "cross-table ORDERBY" in f["reason"]


def test_window_cross_table_helper_handles_garbage(tmp_path):
    # The detector must never raise on empty / non-window / unbalanced input.
    assert fo._window_cross_table_findings("T", "measure", "x", "") == []
    assert fo._window_cross_table_findings("T", "measure", "x", "SUM('Orders'[Sales])") == []
    assert fo._window_cross_table_findings("T", "measure", "x",
                                           "OFFSET(-1, ORDERBY('Calendar'[Date]") == []


def test_window_detector_masks_string_literals(tmp_path):
    # A DAX string literal can quote anything -- a fake ORDERBY clause, brackets, a single quote.
    # The detector masks string literals first, so a clause buried in a string is never parsed as a
    # real navigation argument (no false alarm), while a genuine cross-table window is still caught.
    fake = ('VAR lbl = "see ORDERBY(' + "'Calendar'[Date]" + ')" '
            "RETURN OFFSET(-1, ORDERBY('Orders'[Order_Date]), PARTITIONBY('Orders'[Region]))")
    assert fo._window_cross_table_findings("Orders", "measure", "m", fake) == []
    # a benign string literal does NOT suppress a real cross-table finding
    real = ('VAR note = "prior-row pct" '
            "RETURN OFFSET(-1, ORDERBY('Calendar'[Date]), PARTITIONBY('Orders'[Region]))")
    hits = fo._window_cross_table_findings("Orders", "measure", "m", real)
    assert [f["orderby_table"] for f in hits] == ["Calendar"]
    # a nested window function with an inner cross-table ORDERBY is still flagged
    nested = ("COUNTROWS(WINDOW(1, ABS, -1, ABS, OFFSET(-1, ORDERBY('Calendar'[Date])), "
              "PARTITIONBY('Orders'[Region])))")
    nhits = fo._window_cross_table_findings("Orders", "measure", "m", nested)
    assert [f["orderby_table"] for f in nhits] == ["Calendar"]


def test_mask_dax_string_literals_unit():
    m = fo._mask_dax_string_literals
    # identity when there is no string literal; empty / None are safe
    assert m("SUM('Orders'[Sales])") == "SUM('Orders'[Sales])"
    assert m("") == "" and m(None) == ""
    # length is preserved exactly (downstream balanced-paren index math depends on it)
    s = "A & " + '"x ORDERBY(' + "'D'[d]" + ')"' + " & B"
    assert len(m(s)) == len(s)
    # the masked text carries no parseable clause or bracket reference from inside the string
    assert "ORDERBY" not in m(s).upper() and "[" not in m(s)
    # an escaped inner quote ("") keeps the parser inside the string; surrounding code is preserved
    masked = m('X="a""b"Y')
    assert masked.startswith("X=") and masked.endswith("Y")
    assert "a" not in masked and "b" not in masked and len(masked) == len('X="a""b"Y')


def test_binding_resolver_ignores_refs_inside_string_literals(tmp_path):
    # The binding resolver must not be fooled by a column/table reference quoted INSIDE a DAX string
    # literal -- e.g. an error-message or label string that contains "'Orders'[NoSuchCol]". Such a
    # phantom would otherwise resolve to nothing and raise a FALSE unresolved-binding finding,
    # wrongly lowering the binding / overall score of an otherwise-faithful model.
    dax = "VAR x = \"missing 'Orders'[NoSuchCol] here\" RETURN SUM('Orders'[Sales])"
    # the gap is real: a raw scan sees the phantom ref; the masked scan does not
    assert ("Orders", "NoSuchCol") in set(fo._extract_dax_refs(dax))
    assert ("Orders", "NoSuchCol") not in set(fo._extract_dax_refs(fo._mask_dax_string_literals(dax)))
    res = fo.metadata_tier(model_dir=_orders_model(tmp_path, measures=[("Label", dax)]))
    assert res["scores"]["binding"] == 1.0
    assert [u for u in res["unresolved_bindings"] if u["object"] == "Label"] == []
    # the in-string token must not inflate the window-object count either
    assert res["window_objects_total"] == 0


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
    assert "cross_table_windows" in report["metadata"]