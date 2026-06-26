"""Orchestrator tests: .tds -> complete Fabric semantic model definition."""
import base64
import json

import pytest

from assemble_model import (
    assemble_import_model,
    calc_coverage_artifact,
    fabric_definition_payload,
    migrate_tds_to_semantic_model,
    relationship_confidence_manifest,
    write_model_folder,
    _date_axis_order_resolver,
)
from connection_to_m import parse_tds
from workbook_table_calcs import TableCalcUsage, Pill
from test_connection_to_m import (
    EXCEL_COLLECTION,
    LIVE_SQLSERVER,
    JOIN_TREE,
    FEDERATED_STAR,
    FEDERATED_REL_EDGECASE,
    DATABRICKS_CUSTOM_SQL,
    SNOWFLAKE_CUSTOM_SQL,
)


def _decode(part):
    return base64.b64decode(part["payload"]).decode("utf-8")


# -- Import / DirectQuery assembly --------------------------------------------
def test_assemble_live_sqlserver_full_definition():
    calcs = [
        {"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"},
        {"name": "Avg Sale", "formula": "AVG([Sales])"},
        {"name": "Profit Bucket", "formula": 'IF [Sales]>0 THEN "Y" ELSE "N" END'},
    ]
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore", calcs=calcs)
    parts = out["parts"]

    # required Fabric semantic-model parts are all present
    assert ".platform" in parts
    assert "definition.pbism" in parts
    assert "definition/model.tmdl" in parts
    assert "definition/database.tmdl" in parts
    assert "definition/tables/Orders.tmdl" in parts
    assert "definition/tables/_Measures.tmdl" in parts
    # live SQL Server -> connection parameters become named expressions
    assert "definition/expressions.tmdl" in parts
    assert 'expression Server = "myserver.database.windows.net"' in parts["definition/expressions.tmdl"]

    # the Orders table is a DirectQuery M partition, typed from .tds metadata
    orders = parts["definition/tables/Orders.tmdl"]
    assert "mode: directQuery" in orders
    assert 'Source = Sql.Database(#"Server", #"Database")' in orders
    assert "dataType: int64" in orders   # Quantity

    # model.tmdl references every table including _Measures
    model = parts["definition/model.tmdl"]
    assert "ref table Orders" in model
    assert "ref table _Measures" in model


def test_assemble_measure_report_translates_and_stubs():
    calcs = [
        {"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"},
        {"name": "Profit Bucket", "formula": 'IF [Sales]>0 THEN "Y" ELSE "N" END'},
    ]
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore", calcs=calcs)
    report = {r["measure"]: r for r in out["report"]["measures"]}

    assert report["Profit Ratio"]["status"] == "translated"
    assert report["Profit Ratio"]["dax"] == "DIVIDE(SUM('Orders'[Sales]), SUM('Orders'[Quantity]))"
    assert report["Profit Bucket"]["status"] == "stub"
    assert report["Profit Bucket"]["dax"] is None

    # every formula is preserved as an annotation regardless of translation
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "annotation TableauFormula = SUM([Sales])/SUM([Quantity])" in measures
    assert "measure 'Profit Bucket' = 0" in measures
    assert "TranslatedBy" in measures              # only the translated one


def test_measure_report_carries_source_identity_for_viz_binding():
    # Cross-layer contract (additive): every measure row carries a deterministic `source` so the
    # viz/report layer can join a worksheet calc token -> this emitted measure. The Tableau internal
    # name (e.g. Calculation_xxxx) threads through as calc_instance_token; status decides bind-vs-degrade.
    calcs = [
        {"name": "Count Orders", "formula": "ZN(SUM([Quantity]))",
         "internal_name": "Calculation_0014172369248279"},
        {"name": "Profit Bucket", "formula": 'IF [Sales]>0 THEN "Y" ELSE "N" END'},  # no internal_name
    ]
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore", calcs=calcs)
    report = {r["measure"]: r for r in out["report"]["measures"]}

    src = report["Count Orders"]["source"]
    assert src["kind"] == "calc_column"
    assert src["model_table"] == "_Measures"
    assert src["field_caption"] == "Count Orders"
    assert src["calc_instance_token"] == "Calculation_0014172369248279"
    assert src["intent"] == "measure"
    # a stub still carries source identity (so the binder can degrade-and-warn deterministically)
    stub_src = report["Profit Bucket"]["source"]
    assert stub_src["model_table"] == "_Measures"
    assert stub_src["calc_instance_token"] is None
    assert report["Profit Bucket"]["status"] == "stub"


def test_cross_calc_reference_builds_measure_chain_and_fails_closed():
    # g2: a calc may reference another calc by name. The referent translates first (fixpoint),
    # then the dependent becomes a DAX measure reference -- by caption OR by internal token. A
    # reference to a calc that only STUBS stays a stub (fail-closed, no phantom).
    calcs = [
        {"name": "Count Orders", "formula": "ZN(SUM([Quantity]))",
         "internal_name": "Calculation_0014172369248279"},
        {"name": "Count Plus", "formula": "[Count Orders] + 100"},                 # ref by caption
        {"name": "Count Plus Tid", "formula": "[Calculation_0014172369248279] + 5"},  # ref by token
        {"name": "Profit Bucket", "formula": 'IF [Sales]>0 THEN "Y" ELSE "N" END'},  # stubs
        {"name": "Bad Ref", "formula": "[Profit Bucket] + 100"},                    # ref to a stub
    ]
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore", calcs=calcs)
    report = {r["measure"]: r for r in out["report"]["measures"]}

    assert report["Count Orders"]["status"] == "translated"
    assert report["Count Plus"]["status"] == "translated"
    assert report["Count Plus"]["dax"] == "[Count Orders] + 100"
    assert report["Count Plus Tid"]["status"] == "translated"
    assert report["Count Plus Tid"]["dax"] == "[Count Orders] + 5"
    # fail-closed: referencing a calc that only stubs keeps the dependent inert (no phantom value)
    assert report["Bad Ref"]["status"] == "stub"
    assert report["Bad Ref"]["dax"] is None


def test_calc_bindings_index_keyed_by_token_and_caption():
    # The additive viz-binding manifest: report["calc_bindings"] indexes every emitted measure by
    # BOTH its internal Calculation_* token AND its caption -> {model_table, measure_name, status},
    # so the dashboard binder can join a worksheet calc token to the measure deterministically.
    calcs = [
        {"name": "Count Orders", "formula": "ZN(SUM([Quantity]))",
         "internal_name": "Calculation_0014172369248279"},
        {"name": "Profit Bucket", "formula": 'IF [Sales]>0 THEN "Y" ELSE "N" END'},  # stub, no token
    ]
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore", calcs=calcs)
    bindings = out["report"]["calc_bindings"]

    # joinable by internal token (priority key for the viz side)
    by_token = bindings["Calculation_0014172369248279"]
    assert by_token == {"model_table": "_Measures", "measure_name": "Count Orders",
                        "status": "translated"}
    # and by caption (fallback key) -- same target
    assert bindings["Count Orders"] == by_token
    # a stub is still indexed (so the binder degrades-and-warns deterministically); no token -> caption only
    assert bindings["Profit Bucket"]["status"] == "stub"
    assert bindings["Profit Bucket"]["model_table"] == "_Measures"


def test_object_id_count_calc_lands_as_countrows_measure():
    # End-to-end g1: the pilot's `count orders` = ZN(COUNT(<object-id of Orders>)) must land as a
    # real COUNTROWS measure -- the model build passes its known table names to the translator so
    # the object-model row identity resolves to the 'Orders' table (not a dangling column ref).
    oid = "[__tableau_internal_object_id__].[Orders_ECFCA1FB690A41FE803BC071773BA862]"
    calcs = [{"name": "count orders", "formula": f"ZN(COUNT({oid}))",
              "internal_name": "Calculation_0014172369248279"}]
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore", calcs=calcs)
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "measure 'count orders' = COALESCE(COUNTROWS('Orders'), 0)" in measures
    report = {r["measure"]: r for r in out["report"]["measures"]}
    assert report["count orders"]["status"] == "translated"
    assert report["count orders"]["dax"] == "COALESCE(COUNTROWS('Orders'), 0)"
    # and the binder joins it from the bare Calculation_* token (the dashboard's primary key)
    assert out["report"]["calc_bindings"]["Calculation_0014172369248279"] == {
        "model_table": "_Measures", "measure_name": "count orders", "status": "translated"}


# -- workbook table calcs -> addressed _Measures measures (g9) -----------------
_OID = "[__tableau_internal_object_id__].[Orders_ECFCA1FB690A41FE803BC071773BA862]"


def _sod_usage(**kw):
    """A field table calc shaped like the pilot's 'Standard of Deviation' = WINDOW_STDEV(COUNT(obj)),
    addressed by the worksheet shelves (Rows scope): empty partition, order across a Cols dim. The
    Cols dim here is 'Order ID' (a real resolvable column in LIVE_SQLSERVER), so the seam resolves."""
    d = dict(
        worksheet="Line chart", instance="usr:Calculation_0014172373577763:qk",
        column="Calculation_0014172373577763", caption="Standard of Deviation",
        kind="field", formula=f"WINDOW_STDEV(COUNT({_OID}))",
        ordering_type="Rows", rows=[], cols=[Pill("none:Order ID:nk", "Order ID", "None")],
    )
    d.update(kw)
    return TableCalcUsage(**d)


def test_table_calc_field_usage_lands_as_addressed_measure():
    # A workbook table calc carries the addressing the plain .tds cannot: under the 'Rows' scope the
    # window runs across the Cols dim (Order ID), unpartitioned. It must land as a real _Measures
    # measure (inner object-id COUNT -> COUNTROWS('Orders'), WINDOW_STDEV -> STDEVX.S) with full
    # source identity, NOT a stub -- and the binder must join it by instance token AND bare calc id.
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=[], table_calc_usages=[_sod_usage()])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "measure 'Standard of Deviation' =" in measures
    assert "STDEVX.S" in measures
    assert "COUNTROWS('Orders')" in measures
    assert "ORDERBY('Orders'[Order_ID], ASC)" in measures
    assert "annotation TableauFormula = WINDOW_STDEV(COUNT(" in measures
    row = {r["measure"]: r for r in out["report"]["measures"]}["Standard of Deviation"]
    assert row["status"] == "translated"
    src = row["source"]
    assert src["kind"] == "table_calc"
    assert src["model_table"] == "_Measures"
    assert src["calc_instance_token"] == "usr:Calculation_0014172373577763:qk"
    assert src["calc_id"] == "Calculation_0014172373577763"
    assert src["partition_by"] == []
    assert src["order_by"] == [["Order ID", "ASC"]]
    # binder: both join priorities (full instance token + bare calc id) AND caption resolve here
    b = out["report"]["calc_bindings"]
    target = {"model_table": "_Measures", "measure_name": "Standard of Deviation",
              "status": "translated"}
    assert b["usr:Calculation_0014172373577763:qk"] == target
    assert b["Calculation_0014172373577763"] == target
    assert b["Standard of Deviation"] == target


def test_migrate_tds_threads_table_calc_usages_override():
    # The estate's published-datasource rebuild builds the model from the published .tds (schema only,
    # NO worksheets) while the table-calc addressing lives in the WORKBOOK. ``migrate_tds_to_semantic_model``
    # must therefore honor an explicit ``table_calc_usages=`` override instead of re-extracting from its
    # (worksheet-less) source text. This is the seam that brings the live/published path to parity with a
    # local .twbx whose embedded model already carries its own worksheets. Without the override, the SoD
    # measure could only stub; with it, the addressed STDEVX.S measure lands.
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore",
                                        calcs=[], table_calc_usages=[_sod_usage()])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "measure 'Standard of Deviation' =" in measures
    assert "STDEVX.S" in measures
    assert "COUNTROWS('Orders')" in measures
    row = {r["measure"]: r for r in out["report"]["measures"]}["Standard of Deviation"]
    assert row["status"] == "translated"
    assert row["source"]["kind"] == "table_calc"


def test_migrate_tds_empty_table_calc_usages_disables_extraction():
    # ``None`` (default) auto-extracts from the source text; ``[]`` is an explicit override that DISABLES
    # table calcs. A bare .tds has no worksheets either way, but asserting the explicit-empty path proves
    # the override is honored as a tri-state (None=auto / []=off / list=use), not silently re-extracted.
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore",
                                        calcs=[], table_calc_usages=[])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "measure 'Standard of Deviation' =" not in measures


def test_table_calc_measure_supersedes_plain_stub_and_seeds_cross_calc():
    # The SAME calc appears BOTH as a plain measure-role calc (which only STUBS in measure mode --
    # WINDOW_STDEV has no faithful addressing-less form) AND as an addressed table-calc usage. The
    # addressed form must WIN (exactly one measure, translated -- never a stub twin) and must seed the
    # cross-calc reference so a separate `2 * [Standard of Deviation]` resolves to a measure ref.
    plain = [
        {"name": "Standard of Deviation", "formula": f"WINDOW_STDEV(COUNT({_OID}))",
         "internal_name": "Calculation_0014172373577763"},
        {"name": "Twice Std Dev", "formula": "2 * [Calculation_0014172373577763]",
         "internal_name": "Calculation_0014172374343717"},
    ]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=plain, table_calc_usages=[_sod_usage()])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    rows = {r["measure"]: r for r in out["report"]["measures"]}
    # exactly ONE 'Standard of Deviation' measure, and it is the addressed (translated) one
    assert measures.count("measure 'Standard of Deviation' =") == 1
    assert rows["Standard of Deviation"]["status"] == "translated"
    assert rows["Standard of Deviation"]["source"]["kind"] == "table_calc"
    # the cross-calc ref resolves against the table-calc-seeded measure (g2 over a seeded ref)
    assert rows["Twice Std Dev"]["status"] == "translated"
    assert "[Standard of Deviation]" in rows["Twice Std Dev"]["dax"]


def _pcdf_usage(**kw):
    """The pilot's heat-grid colour pill: a percent-difference quick table calc over the NAMED calc
    ``[count orders] + 100``, addressed across a Cols dim (Order ID here, a resolvable plain column
    in LIVE_SQLSERVER)."""
    d = dict(
        worksheet="Segment % Dod", instance="pcdf:usr:Calculation_0014172369735704:qk",
        column="[Calculation_0014172369735704]", caption="[count orders] + 100",
        kind="quick", calc_type="PctDiff", aggregation=None, ordering_type="Rows",
        rows=[], cols=[Pill("none:Order ID:nk", "Order ID", "None")],
    )
    d.update(kw)
    return TableCalcUsage(**d)


def test_pct_diff_quick_calc_emits_second_measure_keyed_by_instance_token():
    # The pilot's TWO-measure pcdf shape: the NAMED base [count orders] + 100 emits as an ordinary
    # measure under its BARE token, and the percent-difference quick table calc OVER it emits as a
    # SEPARATE derived measure (intent-suffixed name) bound ONLY by its full instance token -- the
    # bare token stays the base's key, so the heat grid never mis-binds to the untransformed base.
    calcs = [
        {"name": "count orders", "formula": f"ZN(COUNT({_OID}))",
         "internal_name": "Calculation_0014172369248279"},
        {"name": "[count orders] + 100", "formula": "[Calculation_0014172369248279] + 100",
         "internal_name": "Calculation_0014172369735704"},
    ]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=calcs, table_calc_usages=[_pcdf_usage()])
    rows = {r["measure"]: r for r in out["report"]["measures"]}

    # the untransformed base measure emits under its own name (the bare-token binding)
    assert rows["[count orders] + 100"]["status"] == "translated"

    # the pcdf emits as a DISTINCT, intent-suffixed measure -- not a duplicate of the base
    pcdf_name = "[count orders] + 100 (percent difference from a prior row)"
    pr = rows[pcdf_name]
    assert pr["status"] == "translated"
    assert pr["dax"].startswith("DIVIDE(")
    assert "COUNTROWS('Orders')" in pr["dax"]     # base inlined to a self-contained aggregate
    assert "+ 100" in pr["dax"]
    src = pr["source"]
    assert src["kind"] == "table_calc"
    assert src["calc_instance_token"] == "pcdf:usr:Calculation_0014172369735704:qk"
    assert src["calc_id"] is None                  # the QTC does NOT claim the bare base token
    assert src["base_calc_id"] == "Calculation_0014172369735704"
    assert src["order_by"] == [["Order ID", "ASC"]]

    # binding: the pcdf joins by its full instance token; the BARE token still resolves to the BASE
    b = out["report"]["calc_bindings"]
    assert b["pcdf:usr:Calculation_0014172369735704:qk"]["measure_name"] == pcdf_name
    assert b["Calculation_0014172369735704"]["measure_name"] == "[count orders] + 100"


def _diff_coloring_usage(**kw):
    """The pilot's Grey/Red colour rule on 'Line chart (2)' -- a PLACED secondary calc that references
    the UNPLACED ``Percent Difference`` (Calculation1). Its worksheet lends Calculation1 a window:
    order across the Cols dim (Order ID here), partition over plain Rows dims only (the Rows pill here
    is a calc token -> excluded -> unpartitioned, the natural line-chart reading)."""
    d = dict(
        worksheet="Line chart (2)", instance="usr:Calculation_0014172376637481:nk",
        column="Calculation_0014172376637481", caption="Difference coloring", kind="field",
        formula='if [Calculation1] <= 0 then "Grey" else "Red" END',
        ordering_type="Rows", secondary=True,
        rows=[Pill("none:Calculation_0014172376367143:nk", "Calculation_0014172376367143", "None")],
        cols=[Pill("none:Order ID:nk", "Order ID", "None")],
    )
    d.update(kw)
    return TableCalcUsage(**d)


def test_unplaced_percent_diff_force_translates_via_consumer_window():
    # The pilot's `Percent Difference` (Calculation1) is NEVER placed on a shelf -- it feeds only a
    # Grey/Red colour rule + a tooltip -- so the plain measure path can only STUB it (LOOKUP needs a
    # window). It is force-translated by INHERITING the colour rule's worksheet window: order across
    # the Cols dim (Order ID), UNPARTITIONED (the consumer's Rows pill is a calc token, excluded).
    calcs = [
        {"name": "Percent Difference",
         "formula": (f"(ZN(COUNT({_OID})) - LOOKUP(ZN(COUNT({_OID})),-1)) "
                     f"/ ABS(LOOKUP(ZN(COUNT({_OID})),-1))"),
         "internal_name": "Calculation1"},
    ]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=calcs, table_calc_usages=[_diff_coloring_usage()])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    rows = {r["measure"]: r for r in out["report"]["measures"]}
    pr = rows["Percent Difference"]
    assert pr["status"] == "translated"            # force-translated, not a stub
    assert pr["dax"].startswith("DIVIDE(")
    assert "COUNTROWS('Orders')" in pr["dax"]
    assert "ORDERBY('Orders'[Order_ID], ASC)" in pr["dax"]
    assert "PARTITIONBY" not in pr["dax"]          # unpartitioned (calc Rows pill excluded)
    # exactly one measure (no stub twin), preserving the original formula as an annotation
    assert measures.count("measure 'Percent Difference' =") == 1
    assert "annotation TableauFormula =" in measures
    src = pr["source"]
    assert src["kind"] == "calc_column"
    assert src["calc_id"] == "Calculation1"
    assert src["calc_instance_token"] == "Calculation1"
    assert src["partition_by"] == []
    assert src["order_by"] == [["Order ID", "ASC"]]
    assert src["addressing_inherited_from"] == "Line chart (2)"
    assert "force-translated" in pr["translated_by"]
    # the binder joins it by the bare Calculation_* token AND its caption
    b = out["report"]["calc_bindings"]
    target = {"model_table": "_Measures", "measure_name": "Percent Difference",
              "status": "translated"}
    assert b["Calculation1"] == target
    assert b["Percent Difference"] == target


def test_unplaced_percent_diff_without_consumer_stays_stub():
    # Fail-closed: with NO placed consumer to lend a window, the unplaced percent-difference calc is
    # NOT force-translated -- it flows through the plain path and stubs (LOOKUP has no addressing), so
    # we never emit a guessed window.
    calcs = [
        {"name": "Percent Difference",
         "formula": (f"(ZN(COUNT({_OID})) - LOOKUP(ZN(COUNT({_OID})),-1)) "
                     f"/ ABS(LOOKUP(ZN(COUNT({_OID})),-1))"),
         "internal_name": "Calculation1"},
    ]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=calcs, table_calc_usages=[])
    pr = {r["measure"]: r for r in out["report"]["measures"]}["Percent Difference"]
    assert pr["status"] != "translated"
    assert pr["dax"] is None


# -- dimension calcs -> DAX calculated columns (column-mode wiring) ------------
def _dim_calc_model():
    measure_calcs = [{"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"}]
    dim_calcs = [
        {"name": "Sales Flag", "formula": 'IF [Sales]>0 THEN "Y" ELSE "N" END'},
        {"name": "Order Code", "formula": "UPPER([Order ID])"},
        {"name": "Avg Sale Col", "formula": "AVG([Sales])"},   # aggregation: not column-legal
    ]
    return assemble_import_model(
        parse_tds(LIVE_SQLSERVER), model_name="Superstore",
        calcs=measure_calcs, dim_calcs=dim_calcs)


def test_dimension_calc_becomes_calculated_column_on_home_table():
    out = _dim_calc_model()
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    # the SAME formula that only STUBS as a measure translates as a row-level calc column.
    assert 'column \'Sales Flag\' = IF(\'Orders\'[Sales] > 0, "Y", "N")' in orders
    assert 'annotation TableauFormula = IF [Sales]>0 THEN "Y" ELSE "N" END' in orders
    assert "annotation TranslatedBy = deterministic" in orders
    assert "column 'Order Code' = UPPER('Orders'[Order_ID])" in orders


def test_untranslatable_dimension_calc_is_inert_blank_stub():
    out = _dim_calc_model()
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    # an aggregation is not valid in a row-level column -> honest inert BLANK() stub on the table.
    assert "column 'Avg Sale Col' = BLANK()" in orders
    rows = {r["column"]: r for r in out["report"]["calc_columns"]}
    assert rows["Avg Sale Col"]["status"] == "stub"
    assert rows["Avg Sale Col"]["dax"] is None
    assert rows["Avg Sale Col"]["table"] == "Orders"


def test_calc_column_report_and_coverage_artifact():
    out = _dim_calc_model()
    rows = {r["column"]: r for r in out["report"]["calc_columns"]}
    assert rows["Sales Flag"]["status"] == "translated"
    assert rows["Sales Flag"]["table"] == "Orders"
    assert rows["Order Code"]["status"] == "translated"
    cov = out["report"]["calc_column_coverage"]["summary"]
    assert cov["total"] == 3
    assert cov["translated"] == 2
    assert cov["stub"] == 1
    assert cov["deterministic_coverage_pct"] == 66.7


# -- assisted (human-approved) landing for a STUBBED dimension calc: the column-mode peer of the
#    measures' approved_calc_dax path -- i.e. the second-compiler loop for a dimension-role calc.
#    Exercises the real Comcast pilot needs_review calc "Highest Selling City By State (name)".
_PILOT_NAME_FORMULA = (
    "IF \n{fixed [State/Province]:Max(\n{fixed [State/Province],[City]: SUM([Sales])}\n)}\n"
    "= \n{fixed [State/Province],[City]: SUM([Sales])}\nthen [State/Province]\nEND")
_PILOT_NAME_DAX = (
    "IF ( CALCULATE ( SUM ( 'Orders'[Sales] ), "
    "ALLEXCEPT ( 'Orders', 'Orders'[State_Province], 'Orders'[City] ) ) "
    "= MAXX ( CALCULATETABLE ( ADDCOLUMNS ( "
    "SUMMARIZE ( 'Orders', 'Orders'[State_Province], 'Orders'[City] ), "
    "\"@cs\", CALCULATE ( SUM ( 'Orders'[Sales] ) ) ), "
    "ALLEXCEPT ( 'Orders', 'Orders'[State_Province] ) ), [@cs] ), "
    "'Orders'[State_Province] )")


def test_approved_dim_calc_lands_as_assisted_approved_calc_column():
    # The deterministic tier STUBS a nested-FIXED-LOD dimension calc; a human-approved assisted DAX
    # flips it into a LIVE calculated column on its home table.
    dim_calcs = [{"name": "Highest Selling City By State (name)", "formula": _PILOT_NAME_FORMULA}]
    out = assemble_import_model(
        parse_tds(LIVE_SQLSERVER), model_name="Superstore", dim_calcs=dim_calcs,
        approved_calc_dax={"Highest Selling City By State (name)": _PILOT_NAME_DAX})
    row = {r["column"]: r for r in out["report"]["calc_columns"]}[
        "Highest Selling City By State (name)"]
    assert row["status"] == "assisted-approved"
    assert row["dax"] == _PILOT_NAME_DAX
    assert row["table"] == "Orders"
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert _PILOT_NAME_DAX in orders
    assert "annotation TranslatedBy = assisted translation (human-approved)" in orders
    # original Tableau formula preserved for audit/repair
    assert "annotation TableauFormula = IF" in orders
    # coverage credits the approved column as LIVE without inflating the deterministic count
    cov = out["report"]["calc_column_coverage"]["summary"]
    assert cov["translated"] == 0
    assert cov["assisted_approved"] == 1
    assert cov["live"] == 1
    assert cov["inert"] == 0
    assert cov["deterministic_coverage_pct"] == 0.0
    assert cov["live_coverage_pct"] == 100.0


# -- AUTO-DETECT (no human approval) of the argmax/argmin idiom on a MEASURE, end-to-end through the
#    real assemble_model resolver. LIVE_SQLSERVER lacks State + City, so the detector (which resolves
#    its fields against the model) could not fire there; this minimal model carries them. This is the
#    peer of the approved-DAX test above: it locks the real suggest_assisted_dax wiring (the path an
#    actual workbook hits) and proves the argmin idiom lands end-to-end, not just in unit tests.
_ARGMAX_MODEL_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='myserver' name='sqlserver.0a1b2c'>
        <connection authentication='sqlserver' class='sqlserver' dbname='Superstore'
                    server='myserver.database.windows.net' username='svc' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>State</remote-name><local-name>[State]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>City</remote-name><local-name>[City]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

_ARGMAX_DETAIL_LOD = "{FIXED [State], [City] : SUM([Sales])}"
_ARGMAX_MEASURE_FORMULA = (
    "IF {FIXED [State] : MAX(%s)} = %s THEN [City] END" % (_ARGMAX_DETAIL_LOD, _ARGMAX_DETAIL_LOD))
_ARGMIN_MEASURE_FORMULA = (
    "IF {FIXED [State] : MIN(%s)} = %s THEN [City] END" % (_ARGMAX_DETAIL_LOD, _ARGMAX_DETAIL_LOD))


def test_argmax_and_argmin_measures_auto_suggest_through_assemble_model():
    # The deterministic tier STUBS a nested-FIXED-LOD argmax/argmin measure; assemble_model must then
    # consult the idiom registry and surface an `assisted-suggested` row + an `assisted_suggestions`
    # review entry carrying the detected pattern -- the real auto-detect wiring an actual workbook hits.
    calcs = [
        {"name": "Top City", "formula": _ARGMAX_MEASURE_FORMULA, "internal_name": "Calc_argmax"},
        {"name": "Bottom City", "formula": _ARGMIN_MEASURE_FORMULA, "internal_name": "Calc_argmin"},
    ]
    out = assemble_import_model(parse_tds(_ARGMAX_MODEL_TDS), model_name="Superstore", calcs=calcs)
    rows = {r["measure"]: r for r in out["report"]["measures"]}
    assert rows["Top City"]["status"] == "assisted-suggested"
    assert rows["Top City"]["assisted_suggestion"]["pattern"] == "argmax-dimension"
    assert rows["Bottom City"]["status"] == "assisted-suggested"
    assert rows["Bottom City"]["assisted_suggestion"]["pattern"] == "argmin-dimension"
    # both surface in the review list the orchestrator reads, each carrying its detected pattern
    sugg = {s["measure"]: s["pattern"] for s in out["report"]["assisted_suggestions"]}
    assert sugg["Top City"] == "argmax-dimension"
    assert sugg["Bottom City"] == "argmin-dimension"


def test_approved_dim_calc_never_overrides_a_deterministic_translation():
    # An approval for a calc Tier 0 ALREADY translates faithfully is ignored -- deterministic wins.
    dim_calcs = [{"name": "Order Code", "formula": "UPPER([Order ID])"}]
    out = assemble_import_model(
        parse_tds(LIVE_SQLSERVER), model_name="Superstore", dim_calcs=dim_calcs,
        approved_calc_dax={"Order Code": '"OVERRIDE"'})
    row = {r["column"]: r for r in out["report"]["calc_columns"]}["Order Code"]
    assert row["status"] == "translated"
    assert row["dax"] == "UPPER('Orders'[Order_ID])"
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "OVERRIDE" not in orders
    assert "annotation TranslatedBy = deterministic" in orders


def test_handoff_artifact_counts_approved_dim_calc_as_live_not_needs_review():
    # The Tier-0 -> Tier-1 handoff must see an approved dimension calc as LIVE, not needs_review.
    dim_calcs = [{"name": "Avg Sale Col", "formula": "AVG([Sales])"}]
    out = assemble_import_model(
        parse_tds(LIVE_SQLSERVER), model_name="Superstore", dim_calcs=dim_calcs,
        approved_calc_dax={"Avg Sale Col": "AVERAGE ( 'Orders'[Sales] )"})
    th = out["report"]["translation_handoff"]
    assert th["summary"]["assisted_approved"] >= 1
    assert all(r["name"] != "Avg Sale Col" for r in th["needs_review"])


def test_no_approval_leaves_dim_calc_stub_byte_identical():
    # Without an approval the stubbed dimension calc is unchanged (the additive channel is inert).
    # lineageTag UUIDs are regenerated per run, so normalize them before comparing structure.
    import re as _re
    norm = lambda t: _re.sub(r"lineageTag: [0-9a-f-]+", "lineageTag: <id>", t)
    base = _dim_calc_model()["parts"]["definition/tables/Orders.tmdl"]
    same = assemble_import_model(
        parse_tds(LIVE_SQLSERVER), model_name="Superstore",
        calcs=[{"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"}],
        dim_calcs=[
            {"name": "Sales Flag", "formula": 'IF [Sales]>0 THEN "Y" ELSE "N" END'},
            {"name": "Order Code", "formula": "UPPER([Order ID])"},
            {"name": "Avg Sale Col", "formula": "AVG([Sales])"},
        ],
        approved_calc_dax={})["parts"]["definition/tables/Orders.tmdl"]
    assert norm(base) == norm(same)


def test_dim_calcs_do_not_disturb_measures_or_default_shape():
    out = _dim_calc_model()
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "measure 'Profit Ratio' = DIVIDE(SUM('Orders'[Sales]), SUM('Orders'[Quantity]))" in measures
    assert "Sales Flag" not in measures      # a dimension calc never leaks into _Measures

    # with no dim_calcs the report keys are present-but-empty and no calc column is emitted.
    base = assemble_import_model(
        parse_tds(LIVE_SQLSERVER), model_name="Superstore",
        calcs=[{"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"}])
    assert base["report"]["calc_columns"] == []
    assert base["report"]["calc_column_coverage"]["summary"]["total"] == 0
    assert base["report"]["calc_column_coverage"]["summary"]["deterministic_coverage_pct"] is None
    assert "column 'Sales Flag'" not in base["parts"]["definition/tables/Orders.tmdl"]


# -- model manifest (additive cohesive view + naming map) ---------------------
def _manifest_param_model():
    """Drive the full parameter taxonomy through the build: a measure-swap (field param), a
    dim-swap (field param), a what-if value param, and a plain FILTER param the model never
    consumes -- plus an object-id COUNT measure so row_count has a faithful target."""
    params = [
        {"caption": "Measure Picker", "internal_name": "[mp]", "datatype": "string",
         "domain": "list", "default": "1", "format": None, "range": None,
         "members": ["1", "2"], "aliases": {"1": "Total Sales", "2": "Units"}},
        {"caption": "Dim Selector", "internal_name": "[ds]", "datatype": "string",
         "domain": "list", "default": "1", "format": None, "range": None,
         "members": ["1", "2"], "aliases": {"1": "By Order", "2": "By Sales"}},
        {"caption": "Sales Multiplier", "internal_name": "[sm]", "datatype": "real",
         "domain": "range", "default": "1.0", "format": None,
         "range": {"min": "0.0", "max": "2.0", "step": "0.1"}, "members": [], "aliases": {}},
        {"caption": "Region Filter", "internal_name": "[rf]", "datatype": "string", "domain": "list",
         "default": '"West"', "format": None, "range": None, "members": ["West", "East"],
         "aliases": {}},
    ]
    calcs = [
        {"name": "Boost", "formula": "SUM([Sales]) * [Parameters].[Sales Multiplier]"},
        {"name": "Measure Swap",
         "formula": "CASE [Parameters].[Measure Picker] WHEN 1 THEN [Sales] WHEN 2 THEN [Quantity] END"},
        {"name": "count orders", "formula": f"ZN(COUNT({_OID}))",
         "internal_name": "Calculation_0014172369248279"},
    ]
    dim_calcs = [
        {"name": "Dim Swap", "role": "dimension",
         "formula": "CASE [Parameters].[Dim Selector] WHEN 1 THEN [Order ID] WHEN 2 THEN [Sales] END"},
    ]
    return assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                 calcs=calcs, dim_calcs=dim_calcs, parameters=params)


def test_model_manifest_has_seven_sections():
    mf = _manifest_param_model()["report"]["model_manifest"]
    assert set(mf) == {"tables", "columns", "measures", "date", "row_count",
                       "parameters", "naming"}
    # tables never lists the _Measures holder; columns carry the original Tableau caption.
    assert "_Measures" not in mf["tables"]
    by_field = {c["tableau_field"]: c for c in mf["columns"]}
    assert by_field["Sales"]["model_table"] == "Orders"
    assert by_field["Sales"]["model_name"] == "Sales"
    assert by_field["Sales"]["calculated"] is False


def test_model_manifest_classifies_parameters_value_field_filter():
    # The dashboard reads manifest.parameters to slice only the plain FILTER params and never
    # double-emit a param the model consumed (the locked contract's micro-item ii).
    mf = _manifest_param_model()["report"]["model_manifest"]
    kinds = {p["name"]: p for p in mf["parameters"]}
    assert kinds["Measure Picker"]["kind"] == "field"
    assert kinds["Measure Picker"]["model_object"] == "Measure Swap"
    assert kinds["Dim Selector"]["kind"] == "field"
    assert kinds["Dim Selector"]["model_object"] == "Dim Swap"
    assert kinds["Sales Multiplier"]["kind"] == "value"
    assert kinds["Sales Multiplier"]["model_object"] == "Sales Multiplier"
    # a what-if value param also exposes its model-owned picker (a range param picks its value col)
    assert kinds["Sales Multiplier"]["picker"] == {"table": "Sales Multiplier",
                                                   "column": "Sales Multiplier"}
    # the plain filter param is model-unowned -> the viz layer slices it
    assert kinds["Region Filter"]["kind"] == "filter"
    assert kinds["Region Filter"]["model_object"] is None
    assert "picker" not in kinds["Region Filter"]          # a model-unowned param has no picker


def test_model_manifest_value_param_carries_label_picker():
    # A numeric LIST what-if param (Tableau's aliased {15,30,41} "Date Selection") lands a value
    # table AND an additive picker pointing at its friendly Label column, so the viz layer can slice
    # the model's own picker (showing Current/Previous/All Orders) instead of re-deriving a slicer.
    params = [
        {"caption": "Date Selection", "internal_name": "[Parameter 0014172370878491]",
         "datatype": "real", "domain": "list", "default": "15.", "format": None, "range": None,
         "members": ["15.", "30.", "41."],
         "aliases": {"15.": "Current Orders", "30.": "Previous Orders", "41.": "All Orders"}},
    ]
    calcs = [{"name": "Date Filter", "role": "measure",
              "formula": "CASE [Parameters].[Date Selection] WHEN 15 THEN 1 END"}]
    mf = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                               calcs=calcs, parameters=params)["report"]["model_manifest"]
    ds = next(p for p in mf["parameters"] if p["name"] == "Date Selection")
    assert ds["kind"] == "value"
    assert ds["model_object"] == "Date Selection"
    assert ds["picker"] == {"table": "Date Selection", "column": "Date Selection Label"}


def test_model_manifest_naming_map_binds_columns_measures_params():
    mf = _manifest_param_model()["report"]["model_manifest"]
    naming = mf["naming"]
    # base column: keyed by Tableau caption AND physical/remote name, both -> the emitted column
    assert naming["Sales"] == {"model_table": "Orders", "model_name": "Sales", "kind": "column"}
    assert naming["Order ID"]["model_name"] == "Order_ID"
    assert naming["Order ID"]["kind"] == "column"
    # measure: keyed by caption AND the bare Calculation_* token
    tgt = {"model_table": "_Measures", "model_name": "count orders", "kind": "measure"}
    assert naming["count orders"] == tgt
    assert naming["Calculation_0014172369248279"] == tgt
    # parameter table: reachable by the controlling parameter AND the swap calc / table name
    assert naming["Dim Selector"]["kind"] == "parameter"
    assert naming["Dim Selector"]["model_table"] == "Dim Swap"
    assert naming["Dim Swap"] == {"model_table": "Dim Swap", "model_name": "Dim Swap",
                                  "kind": "parameter"}


def test_model_manifest_row_count_targets_faithful_countrows():
    mf = _manifest_param_model()["report"]["model_manifest"]
    rc = mf["row_count"]
    # `count orders` = ZN(COUNT(<object-id>)) -> COALESCE(COUNTROWS('Orders'),0): a provable row count
    assert rc["measures"] == {"Orders": "count orders"}
    assert rc["default"] == {"table": "Orders", "measure": "count orders"}


def test_model_manifest_row_count_ignores_non_rowcount_measures():
    # A COUNT over a specific column / a ratio is NOT a whole-table row count -> never offered.
    out = assemble_import_model(
        parse_tds(LIVE_SQLSERVER), model_name="Superstore",
        calcs=[{"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"}])
    assert out["report"]["model_manifest"]["row_count"]["measures"] == {}
    assert out["report"]["model_manifest"]["row_count"]["default"] is None


def test_model_manifest_present_and_inert_without_parameters():
    # Additive + always present: no parameters/calcs still yields a well-formed manifest.
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore")
    mf = out["report"]["model_manifest"]
    assert mf["parameters"] == []
    assert mf["tables"] == ["Orders"]
    assert mf["naming"]["Sales"]["model_name"] == "Sales"


# -- Stage 4: parameter-driven date-window keep-flag measure ----------------------------------
_DATE_BAND_SQLSERVER = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='myserver' name='sqlserver.0a1b2c'>
        <connection authentication='sqlserver' class='sqlserver' dbname='Superstore'
                    server='myserver.database.windows.net' username='svc' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order Date</remote-name><local-name>[Order Date]</local-name>
        <parent-name>[Orders]</parent-name><local-type>datetime</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Quantity</remote-name><local-name>[Quantity]</local-name>
        <parent-name>[Orders]</parent-name><local-type>integer</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


def _date_band_model():
    """A faithful Comcast-shape date band: an aliased numeric LIST param (Date Selection) +
    a band-case "Date Filter" calc whose inner ref resolves to a LAST() calc. The descriptor
    carries an Order Date column so the shared Date dimension (the anchor) actually generates."""
    params = [
        {"caption": "Date Selection", "internal_name": "[Parameter 0014172370878491]",
         "datatype": "real", "domain": "list", "default": "15.", "format": None, "range": None,
         "members": ["15.", "30.", "41."],
         "aliases": {"15.": "Current Orders", "30.": "Previous Orders", "41.": "All Orders"}},
    ]
    calcs = [
        {"name": "Date Filter", "role": "measure",
         "formula": ("case [Parameters].[Parameter 0014172370878491] "
                     "when 15 then [Calculation_0014172370616346] <= 15 "
                     "when 30 then [Calculation_0014172370616346] <= 30 "
                     "and [Calculation_0014172370616346] >= 15 "
                     "when 41 then [Calculation_0014172370616346] <= 41 END"),
         "internal_name": "Calculation_0014172371238940"},
        {"name": "last", "formula": "LAST()",
         "internal_name": "Calculation_0014172370616346"},
    ]
    return assemble_import_model(parse_tds(_DATE_BAND_SQLSERVER), model_name="Superstore",
                                 calcs=calcs, parameters=params)


def test_date_band_emits_keep_flag_measure_and_filter_binding():
    out = _date_band_model()
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    # the synthesized SWITCH keep-flag measure lands, anchored on the fact max date.
    assert "measure 'Date Filter' =" in measures
    assert "VAR anchor = CALCULATE(MAX('Orders'[Order_Date]), ALL('Orders'))" in measures
    assert "VAR sel = SELECTEDVALUE('Date Selection'[Date Selection], 15)" in measures
    assert "sel = 15, IF(d > anchor - 15, 1)" in measures
    assert "sel = 30, IF(d > anchor - 30 && d <= anchor - 15, 1)" in measures
    assert "sel = 41, 1" in measures
    # the original Tableau formula is preserved as an annotation.
    assert "annotation TableauFormula = case [Parameters].[Parameter 0014172370878491]" in measures

    fb = out["report"]["filter_bindings"]
    assert "Date Filter" in fb
    assert fb["Date Filter"]["measure_name"] == "Date Filter"
    assert fb["Date Filter"]["model_table"] == "_Measures"
    assert fb["Date Filter"]["value"] == 1
    assert fb["Date Filter"]["calc_id"] == "Calculation_0014172371238940"
    assert fb["Date Filter"]["param_internal"] == "Parameter 0014172370878491"


def test_date_band_supersedes_plain_stub_and_reports_translated():
    out = _date_band_model()
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    # the band calc must NOT also land as an inert stub measure (only the SWITCH form).
    assert "measure 'Date Filter' = \n" not in measures
    assert "measure 'Date Filter' = BLANK()" not in measures
    rows = {r["measure"]: r for r in out["report"]["measures"]}
    assert rows["Date Filter"]["status"] == "translated"
    assert rows["Date Filter"]["source"]["model_table"] == "_Measures"
    assert rows["Date Filter"]["source"]["calc_instance_token"] == "Calculation_0014172371238940"


def test_no_date_band_means_no_filter_bindings_key():
    # Byte-identical no-flag path: a model with no date-band param omits filter_bindings entirely.
    out = assemble_import_model(
        parse_tds(LIVE_SQLSERVER), model_name="Superstore",
        calcs=[{"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"}])
    assert "filter_bindings" not in out["report"]


# -- ADD #1: date-axis ORDERBY redirect builder ------------------------------------------------
def _date_axis_resolve(caption):
    # A tiny caption resolver: the fact date column + a non-date field.
    return {
        "Order Date": ("Orders", "Order_Date", "dateTime"),
        "Region": ("Orders", "Region", "string"),
    }.get(caption)


def test_date_axis_order_resolver_redirects_active_date_col_to_calendar_key():
    # The active date column's caption redirects to the marked-calendar key Date[Date], carrying
    # the source fact (Orders) as the 4th element so the compiler can guard unrelated aggregates.
    redirect = _date_axis_order_resolver(
        _date_axis_resolve, "Date", {("Orders", "Order_Date")})
    assert redirect("Order Date") == ("Date", "Date", "dateTime", "Orders")
    # a non-date caption is never redirected -> the normal resolver handles it.
    assert redirect("Region") is None
    # an unknown caption resolves to nothing -> no redirect.
    assert redirect("Nope") is None


def test_date_axis_order_resolver_is_none_without_date_dimension():
    # No date table or no active date column -> no redirect at all (byte-identical legacy path).
    assert _date_axis_order_resolver(_date_axis_resolve, "", {("Orders", "Order_Date")}) is None
    assert _date_axis_order_resolver(_date_axis_resolve, "Date", set()) is None
    assert _date_axis_order_resolver(_date_axis_resolve, "Date", None) is None


def test_positional_measure_orderby_is_single_table_not_cross_table_redirect():
    # REGRESSION (live-proven on Fabric, error 0x413A0003): a positional table-calc measure addressed
    # across the continuous DATE axis must emit an OFFSET/WINDOW whose ORDERBY and PARTITIONBY come
    # from a SINGLE table. ADD #1 redirected the ORDERBY to the calendar key Date[Date] while the inner
    # aggregate + partition stayed on the fact (Orders) -> a cross-table window with no <relation>,
    # which the live engine rejects ("all OrderBy and PartitionBy columns must be from the same
    # table"). The model build must order by the fact's OWN date column (Orders[Order_Date]) instead.
    # _DATE_BAND_SQLSERVER carries an Order Date column, so the Date dimension IS generated and the
    # (now-disabled) redirect path is genuinely reachable -- making this a non-vacuous guard.
    sod = _sod_usage(cols=[Pill("none:Order Date:nk", "Order Date", "None")])
    out = assemble_import_model(parse_tds(_DATE_BAND_SQLSERVER), model_name="Superstore",
                                calcs=[], table_calc_usages=[sod])
    assert "definition/tables/Date.tmdl" in out["parts"]   # the redirect's target dimension exists
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "measure 'Standard of Deviation' =" in measures
    # the positional window orders by the FACT date column -- single-table, valid DAX ...
    assert "ORDERBY('Orders'[Order_Date], ASC)" in measures
    # ... and NEVER on the calendar key (the cross-table form the live engine rejects).
    assert "ORDERBY('Date'[Date]" not in measures
    row = {r["measure"]: r for r in out["report"]["measures"]}["Standard of Deviation"]
    assert row["status"] == "translated"
    assert row["source"]["order_by"] == [["Order Date", "ASC"]]


def test_assemble_excel_collection_multi_table():
    out = migrate_tds_to_semantic_model(EXCEL_COLLECTION, model_name="Superstore")
    parts = out["parts"]
    # the collection container yields 3 independent Import tables (no duplicates, no join)
    assert "definition/tables/Orders.tmdl" in parts
    assert "definition/tables/People.tmdl" in parts
    assert "definition/tables/Returns.tmdl" in parts
    assert out["report"]["storage_decision"]["mode"] == "Import"
    # flat file -> no connection-parameter expressions
    assert "definition/expressions.tmdl" not in parts
    assert "mode: import" in parts["definition/tables/Orders.tmdl"]


def test_assemble_join_tree_raises_for_fallback():
    with pytest.raises(ValueError) as ei:
        migrate_tds_to_semantic_model(JOIN_TREE, model_name="Joined")
    assert "land-to-delta" in str(ei.value).lower()


def test_migrate_auto_wires_parsed_relationships():
    # The convenience entry point must emit the joins parse_tds already inferred from the
    # <object-graph><relationships> WITHOUT the caller passing them explicitly -- so a
    # double-clickable model arrives with relationships as declared metadata (no manual draw,
    # no DirectQuery cardinality-detection round-trip).
    out = migrate_tds_to_semantic_model(FEDERATED_STAR, model_name="Star")
    rels = out["parts"]["definition/relationships.tmdl"]
    assert "fromColumn: SALE.REGION" in rels and "toColumn: REP.REGION" in rels
    assert "fromColumn: SALE.Order_Key" in rels and "toColumn: RMA.Order_Key" in rels
    reported = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"])
                for r in out["report"]["relationships"]}
    assert reported == {
        ("SALE", "REGION", "REP", "REGION"),
        ("SALE", "Order_Key", "RMA", "Order_Key"),
    }


def test_migrate_explicit_empty_relationships_opts_out():
    # An explicit list (here empty) takes full control and skips auto-wiring, so a caller can
    # deliberately suppress relationships even when the .tds declares them.
    out = migrate_tds_to_semantic_model(FEDERATED_STAR, model_name="Star", relationships=[])
    assert "definition/relationships.tmdl" not in out["parts"]
    assert out["report"]["relationships"] == []


def test_no_credentials_in_any_part():
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore")
    blob = "\n".join(out["parts"].values())
    assert "username" not in blob and "svc" not in blob


# -- Fabric payload + folder writing ------------------------------------------
def test_fabric_definition_payload_is_base64_roundtrip():
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore")
    payload = fabric_definition_payload(out["parts"])
    parts = payload["definition"]["parts"]
    assert all(p["payloadType"] == "InlineBase64" for p in parts)
    by_path = {p["path"]: p for p in parts}
    # .pbism decodes to valid JSON with the Fabric schema version
    pbism = json.loads(_decode(by_path["definition.pbism"]))
    assert "version" in pbism
    # .platform decodes to the SemanticModel item metadata
    platform = json.loads(_decode(by_path[".platform"]))
    assert platform["metadata"]["type"] == "SemanticModel"
    assert platform["metadata"]["displayName"] == "Superstore"


def test_write_model_folder(tmp_path):
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore")
    written = write_model_folder(out["parts"], str(tmp_path / "Superstore.SemanticModel"))
    assert any(p.endswith("model.tmdl") for p in written)
    assert (tmp_path / "Superstore.SemanticModel" / "definition" / "tables" / "Orders.tmdl").exists()
    assert (tmp_path / "Superstore.SemanticModel" / ".platform").exists()


# -- custom-SQL native query: end-to-end model + fail-loud report keys --------
def test_databricks_custom_sql_emits_real_partition_no_review():
    out = migrate_tds_to_semantic_model(DATABRICKS_CUSTOM_SQL, model_name="DbxSQL")
    part = out["parts"]["definition/tables/Custom SQL Query.tmdl"]
    assert 'Catalog = Source{[Name="tableau_migration_databricks", Kind="Database"]}[Data]' in part
    assert "Value.NativeQuery(Catalog, " in part
    assert '{"Order ID", "Order_ID"}' in part
    # a real, deploy-ready partition is NOT flagged for review (additive report keys present)
    report = out["report"]
    assert report["partitions_stubbed"] == 0
    assert report["partitions_needs_review"] == []


def test_snowflake_custom_sql_is_flagged_needs_review():
    out = migrate_tds_to_semantic_model(SNOWFLAKE_CUSTOM_SQL, model_name="SnowSQL")
    report = out["report"]
    # fail LOUD at build time: the unverified-connector scaffold is counted and listed, with the
    # original SQL preserved for manual completion -- not silently passed to deploy.
    assert report["partitions_stubbed"] == 1
    entries = report["partitions_needs_review"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["table"] == "Custom SQL Query"
    assert entry["kind"] == "m_partition"
    assert "isn't verified" in entry["reason"]
    assert entry["sql"] == 'SELECT "ORDER ID", SALES FROM ORDERS'
    # and the emitted partition is a DEPLOY-valid scaffold (empty typed table, single let..in)
    part = out["parts"]["definition/tables/Custom SQL Query.tmdl"]
    assert "Source = #table(type table [], {})" in part
    assert "Source = null" not in part



# -- Relationship-confidence manifest (additive report artifact) --------------
def _by_key(created):
    return {(c["from_table"], c["from_col"], c["to_table"], c["to_col"]): c for c in created}


def test_relationship_confidence_grades_id_high_and_dimension_low():
    # The authored object-graph joins are graded: an ID-like key (Order_Key) is high confidence;
    # a coarse string-dimension key (REGION) is low and must be flagged for many-to-many risk.
    out = migrate_tds_to_semantic_model(FEDERATED_STAR, model_name="Star")
    manifest = out["report"]["relationship_confidence"]
    created = _by_key(manifest["created"])

    id_rel = created[("SALE", "Order_Key", "RMA", "Order_Key")]
    assert id_rel["confidence"] == "high"
    assert id_rel["risks"] == []
    assert id_rel["origin"] == "authored"

    dim_rel = created[("SALE", "REGION", "REP", "REGION")]
    assert dim_rel["confidence"] == "low"
    assert any("many-to-many" in r for r in dim_rel["risks"])

    assert manifest["summary"]["high"] >= 1 and manifest["summary"]["low"] >= 1
    assert manifest["summary"]["created"] == len(manifest["created"])


def test_relationship_confidence_carries_per_table_connector_and_cross_source():
    # A heterogeneous federation must report EACH endpoint's own connector, not one datasource-
    # level class, and flag a cross-source join. Synthetic descriptor (original, no fixture).
    descriptor = {
        "datasource_name": "Federated",
        "relations": [
            {"kind": "table", "name": "Orders",
             "connection": {"connection_class": "azure_sqldb"},
             "columns": [{"model_name": "Order_ID", "tmdl_type": "int64"}]},
            {"kind": "table", "name": "RETURNS",
             "connection": {"connection_class": "snowflake"},
             "columns": [{"model_name": "ORDER_ID", "tmdl_type": "int64"}]},
        ],
        "relationships": [
            {"from_table": "Orders", "from_col": "Order_ID",
             "to_table": "RETURNS", "to_col": "ORDER_ID"},
        ],
        "relationship_warnings": [],
    }
    manifest = relationship_confidence_manifest(descriptor)
    rel = manifest["created"][0]
    assert rel["from_connector"] == "azure_sqldb"
    assert rel["to_connector"] == "snowflake"
    assert rel["cross_source"] is True
    assert rel["confidence"] == "high"  # integer + ID-like name


def test_relationship_confidence_lists_skipped_reasons():
    # Candidates the resolver dropped (ghost column, composite AND, ambiguous orientation) surface
    # verbatim as skip reasons so a reviewer sees what was NOT wired and why.
    descriptor = parse_tds(FEDERATED_REL_EDGECASE)
    manifest = relationship_confidence_manifest(descriptor)
    assert manifest["summary"]["skipped"] >= 1
    assert manifest["summary"]["skipped"] == len(descriptor["relationship_warnings"])
    assert all(isinstance(s["reason"], str) and s["reason"] for s in manifest["skipped"])


def test_relationship_confidence_is_additive_not_destructive():
    # The manifest is purely additive: every pre-existing report key is still present alongside it.
    out = migrate_tds_to_semantic_model(FEDERATED_STAR, model_name="Star")
    report = out["report"]
    for key in ("model_name", "storage_decision", "tables", "measures",
                "assisted_suggestions", "relationships", "date_table", "roles"):
        assert key in report
    assert "relationship_confidence" in report
    # the created entries match the reported relationships one-for-one
    reported = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"])
                for r in report["relationships"]}
    graded = {(c["from_table"], c["from_col"], c["to_table"], c["to_col"])
              for c in report["relationship_confidence"]["created"]}
    assert reported == graded


# -- Calc-coverage artifact (additive report output) --------------------------
def test_calc_coverage_counts_translated_and_stubbed():
    # Two single-field aggregates translate; the IF/THEN string calc stays an inert stub.
    calcs = [
        {"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"},
        {"name": "Avg Sale", "formula": "AVG([Sales])"},
        {"name": "Profit Bucket", "formula": 'IF [Sales]>0 THEN "Y" ELSE "N" END'},
    ]
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore", calcs=calcs)
    cov = out["report"]["calc_coverage"]
    s = cov["summary"]
    assert s["total"] == 3
    assert s["translated"] == 2
    assert s["stub"] == 1
    assert s["live"] == 2 and s["inert"] == 1
    assert s["deterministic_coverage_pct"] == pytest.approx(66.7)
    assert s["live_coverage_pct"] == pytest.approx(66.7)

    by = {m["measure"]: m for m in cov["measures"]}
    assert by["Profit Ratio"]["live"] is True and by["Profit Ratio"]["bucket"] == "translated"
    assert by["Profit Bucket"]["live"] is False and by["Profit Bucket"]["bucket"] == "stub"
    # every formula is carried for an auditable report
    assert by["Profit Bucket"]["tableau_formula"] == 'IF [Sales]>0 THEN "Y" ELSE "N" END'


def test_calc_coverage_is_additive_and_undefined_without_calcs():
    # No calcs -> measures empty; coverage is undefined (None, not a misleading 0/100), and the
    # artifact sits alongside the still-present measures key.
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore")
    report = out["report"]
    assert "measures" in report and "calc_coverage" in report
    s = report["calc_coverage"]["summary"]
    assert s["total"] == 0
    assert s["deterministic_coverage_pct"] is None
    assert s["live_coverage_pct"] is None


def test_calc_coverage_buckets_assisted_states():
    # Direct unit test over synthetic report rows covering all four buckets, incl. the human-approved
    # assist (live) vs the still-inert suggestion.
    rows = [
        {"measure": "a", "status": "translated", "reason": "ok", "tableau_formula": "SUM([X])"},
        {"measure": "b", "status": "assisted-approved", "reason": "fallback",
         "tableau_formula": "...", "assisted_pattern": "argmax"},
        {"measure": "c", "status": "assisted-suggested", "reason": "fallback",
         "tableau_formula": "...", "assisted_suggestion": {"pattern": "argmax"}},
        {"measure": "d", "status": "stub", "reason": "unsupported", "tableau_formula": "..."},
    ]
    cov = calc_coverage_artifact(rows)
    s = cov["summary"]
    assert (s["translated"], s["assisted_approved"], s["assisted_suggested"], s["stub"]) == (1, 1, 1, 1)
    assert s["live"] == 2 and s["inert"] == 2
    assert s["live_coverage_pct"] == pytest.approx(50.0)
    assert s["deterministic_coverage_pct"] == pytest.approx(25.0)

    by = {m["measure"]: m for m in cov["measures"]}
    assert by["b"]["live"] is True and by["b"]["has_suggestion"] is True
    assert by["c"]["live"] is False and by["c"]["has_suggestion"] is True
    assert by["d"]["has_suggestion"] is False

