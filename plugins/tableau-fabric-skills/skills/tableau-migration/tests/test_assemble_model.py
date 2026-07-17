"""Orchestrator tests: .tds -> complete Fabric semantic model definition."""
import base64
import json
import re

import pytest

from assemble_model import (
    assemble_import_model,
    calc_coverage_artifact,
    fabric_definition_payload,
    migrate_tds_to_semantic_model,
    relationship_confidence_manifest,
    resolve_consolidated_column,
    write_model_folder,
    _date_axis_order_resolver,
    _approved_entry,
    _calc_columns_part,
    _split_calcs_by_role,
    _is_stock_row_count_calc,
    _approved_dtype,
    _win_long_path,
    _reroute_row_level_measure_calcs,
    _build_column_refs,
    _calc_field_tokens,
    _best_scoped_resolver,
    _sibling_anchored_resolver,
    _anchoring_rc,
    _ROW_LEVEL_IN_MEASURE_REASON,
    _INLINE_REF_SENTINEL,
    _unique_source_name,
    _plan_source_suffix_renames,
    _apply_source_suffix_renames,
    _rename_relationship_endpoints,
)
from calc_to_dax import translate_tableau_calc_to_dax
from connection_to_m import parse_tds, combine_descriptors
from openability_gate import check_model_openability
from workbook_table_calcs import TableCalcUsage, Pill
from test_connection_to_m import (
    EXCEL_COLLECTION,
    LIVE_SQLSERVER,
    JOIN_TREE,
    PHYSICAL_JOIN_KEYS,
    FEDERATED_STAR,
    FEDERATED_REL_EDGECASE,
    DATABRICKS_CUSTOM_SQL,
    DATABRICKS_CUSTOM_SQL_DOUBLED,
    DATABRICKS_CUSTOM_SQL_PARAM,
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
        # REGEXP_MATCH has no faithful DAX (no native regex) -> irreducible stub in either mode.
        {"name": "Profit Bucket", "formula": 'REGEXP_MATCH([Region], "^A")'},
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


def test_row_level_measure_role_calc_reroutes_to_column():
    # Tableau labels a purely row-level numeric calc a *measure* (by output type), so it lands on the
    # measure path where its bare field references correctly stub. The faithful Power BI form is a DAX
    # calculated COLUMN (summed in the visual by default, matching Tableau). The additive pre-router
    # reclassifies such a calc onto the column path -- but ONLY when the column translator renders it,
    # so a stub is never merely relocated. A genuine aggregate measure is untouched.
    calcs = [
        {"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"},  # real measure -> stays
        {"name": "Margin", "formula": "[Sales] * 2"},                         # row-level -> reroutes
    ]
    out = migrate_tds_to_semantic_model(LIVE_SQLSERVER, model_name="Superstore", calcs=calcs)
    measures = {r["measure"]: r for r in out["report"]["measures"]}
    columns = {r["column"]: r for r in out["report"]["calc_columns"]}

    # the real aggregate stays a measure; the row-level calc left the measure path entirely
    assert measures["Profit Ratio"]["status"] == "translated"
    assert "Margin" not in measures

    # ... and lands as a faithful, translated calculated column on the fact table
    assert columns["Margin"]["status"] == "translated"
    assert columns["Margin"]["table"] == "Orders"
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "column Margin =" in orders
    assert "'Orders'[Sales] * 2" in orders
    # the original Tableau formula is preserved as an annotation (provenance intact)
    assert "[Sales] * 2" in orders


def test_row_level_measure_role_calc_with_approved_dax_stays_a_measure():
    # The pre-router must DEFER to a designated measure landing: a calc given human-approved /
    # second-compiler DAX via approved_calc_dax stays on the measure path to receive it (that opt-in
    # tier owns the measure-vs-column choice), even though column mode could also render it.
    calcs = [{"name": "Margin", "formula": "[Sales] * 2"}]
    approved = {"Margin": "SUMX('Orders', 'Orders'[Sales] * 2)"}
    out = migrate_tds_to_semantic_model(
        LIVE_SQLSERVER, model_name="Superstore", calcs=calcs, approved_calc_dax=approved)
    measures = {r["measure"]: r for r in out["report"]["measures"]}
    columns = {r["column"] for r in out["report"]["calc_columns"]}

    assert measures["Margin"]["status"] == "assisted-approved"
    assert measures["Margin"]["dax"] == "SUMX('Orders', 'Orders'[Sales] * 2)"
    assert "Margin" not in columns  # NOT rerouted


# -- Fix B: cascade-aware reroute fix-point ----------------------------------
def _phys_resolver(cols):
    """A physical-column resolver: name (case-insensitive) -> (table, column, tmdl_type)."""
    idx = {k.strip().lower(): v for k, v in cols.items()}

    def r(name):
        return idx.get((name or "").strip().lower())
    return r


def test_build_column_refs_cascades_a_two_level_dim_chain():
    # The shared cascade builder factored out of _calc_columns_part: a dim calc that references
    # ANOTHER dim calc resolves only after the sibling is itself recorded (fix-point). L1 is a bool
    # over a physical column; L2 references L1 by caption; both must land as single-home typed refs.
    resolve = _phys_resolver({"Region": ("F", "Region", "string"), "Qty": ("F", "Qty", "int64")})
    dims = [
        {"name": "Is West", "formula": '[Region] = "West"'},          # L1 (bool)
        {"name": "West Qty", "formula": "IF [Is West] THEN [Qty] END"},  # L2 -> refs L1
    ]
    refs = _build_column_refs(dims, lambda _c: resolve, {"F"})
    assert "is west" in refs and refs["is west"][0] == "F"
    assert "west qty" in refs and refs["west qty"][0] == "F"   # only resolvable AFTER L1 recorded

    # An untranslatable sibling is NOT a reference target (fail-closed) -- no faithful DAX form.
    bad = [{"name": "Bad", "formula": 'REGEXP_MATCH([Region], "^A")'}]
    assert _build_column_refs(bad, lambda _c: resolve, {"F"}) == {}


def test_reroute_moves_row_level_measure_that_references_a_sibling_dim_calc():
    # THE CORE FIX B CASE. A measure-role calc that is genuinely ROW-LEVEL but references a
    # translatable sibling DIM calc must reroute onto the column path -- which requires the column
    # probe to see column_refs built over the dim calcs. (Before the fix the probe passed no
    # column_refs, so the sibling was unresolved, column mode failed, and the calc stayed a stub.)
    resolve = _phys_resolver({"Region": ("F", "Region", "string"), "Qty": ("F", "Qty", "int64")})
    dims = [{"name": "Is West", "formula": '[Region] = "West"'}]           # translatable sibling
    measures = [{"name": "West Qty", "formula": "IF [Is West] THEN [Qty] END"}]  # row-level measure
    keep, new_dims, moved = _reroute_row_level_measure_calcs(
        measures, dims, resolve, known_tables={"F"})
    assert moved == ["West Qty"]
    assert not keep
    assert {c["name"] for c in new_dims} == {"Is West", "West Qty"}


def test_reroute_cascades_a_diff_of_two_calcs_via_fixpoint():
    # THE 2-LEVEL CASCADE (the reason the reroute must ITERATE to a fix-point). A difference calc
    # [CYQ] - [PYQ] references two OTHER row-level measure calcs. It can only reroute AFTER its two
    # operands have themselves been moved onto the column path -- so round 1 moves CYQ/PYQ, round 2
    # rebuilds column_refs to include them and moves the diff. A single pass would leave the diff
    # stubbed forever.
    resolve = _phys_resolver({
        "Region": ("F", "Region", "string"),
        "Qty": ("F", "Qty", "int64"),
        "Sales": ("F", "Sales", "decimal"),
    })
    dims = [{"name": "Is CY", "formula": '[Region] = "West"'}]
    measures = [
        {"name": "CYQ", "formula": "IF [Is CY] THEN [Qty] END"},     # round 1 (refs dim Is CY)
        {"name": "PYQ", "formula": "IF [Is CY] THEN [Sales] END"},   # round 1
        {"name": "Diff", "formula": "[CYQ] - [PYQ]"},                # round 2 (refs CYQ + PYQ)
    ]
    keep, new_dims, moved = _reroute_row_level_measure_calcs(
        measures, dims, resolve, known_tables={"F"})
    assert set(moved) == {"CYQ", "PYQ", "Diff"}
    assert "Diff" in moved and moved.index("Diff") > moved.index("CYQ")  # moved AFTER its operands
    assert not keep
    assert {c["name"] for c in new_dims} == {"Is CY", "CYQ", "PYQ", "Diff"}


def test_reroute_fails_closed_when_sibling_is_untranslatable():
    # A row-level measure calc whose referenced sibling has NO faithful DAX form (or is absent) must
    # stay an honest measure stub -- the reroute only moves a calc the column translator can render.
    resolve = _phys_resolver({"Region": ("F", "Region", "string"), "Qty": ("F", "Qty", "int64")})
    dims = [{"name": "Bad Sib", "formula": 'REGEXP_MATCH([Region], "^A")'}]   # untranslatable
    measures = [{"name": "Uses Bad", "formula": "IF [Bad Sib] THEN [Qty] END"}]
    keep, new_dims, moved = _reroute_row_level_measure_calcs(
        measures, dims, resolve, known_tables={"F"})
    assert moved == []
    assert keep is measures            # nothing moved -> inputs returned unchanged (byte-identical)
    assert new_dims is dims


def test_reroute_never_moves_a_genuine_aggregate_measure():
    # A real aggregate measure (SUM over a physical column) translates in MEASURE mode, so it never
    # hits the row-level reason gate and is never rerouted -- even though its operands resolve.
    resolve = _phys_resolver({"Qty": ("F", "Qty", "int64")})
    measures = [{"name": "Total Qty", "formula": "SUM([Qty])"}]
    keep, new_dims, moved = _reroute_row_level_measure_calcs(
        measures, [], resolve, known_tables={"F"})
    assert moved == []
    assert keep is measures            # unchanged fast path
    assert {c["name"] for c in keep} == {"Total Qty"}


# --- Sibling-anchored ambiguity resolution (the corpus `Days to Close` lever) -------------------
#
# A row-level calc frequently combines a UNIQUE business field with an AMBIGUOUS system field read
# from the SAME record -- e.g. `DATEDIFF('day', [Close Date], [Created Date])` where `[Close Date]`
# resolves to exactly one table (Intake) but `[Created Date]` is a system column present on many
# tables, so it resolves to None and the whole calc stubs. Because both operands are one record, the
# ambiguous field must live on a table a resolved sibling already pins. `resolve_in_tables` restricts
# a caption to a pinned table-set (fail-closed: 0 or >1 hits -> None). These tests exercise the REAL
# translators end to end so the assertions reflect genuine engine behaviour, not a synthetic double.

_ANCHOR_INTAKE = "caseman__Intake__c"


def _anchoring_resolver(unique_cols, ambiguous_col, ambiguous_tables):
    """A physical resolver whose ambiguous system caption fails closed at top level but binds via a
    `resolve_in_tables(caption, table_set)` primitive iff EXACTLY ONE pinned table carries it.

    ``unique_cols``    -- {caption: (table, column, type)} that resolve uniquely at top level.
    ``ambiguous_col``  -- (caption, column, type) the base resolver returns None for.
    ``ambiguous_tables`` -- the set of tables that physically carry ``ambiguous_col`` (>1 -> ambiguous).
    """
    idx = {k.strip().lower(): v for k, v in unique_cols.items()}
    amb_cap, amb_col, amb_type = ambiguous_col
    amb_key = amb_cap.strip().lower()

    def r(name):
        return idx.get((name or "").strip().lower())

    def resolve_in_tables(name, table_set):
        if (name or "").strip().lower() == amb_key:
            hits = [t for t in table_set if t in ambiguous_tables]
            if len(hits) == 1:
                return (hits[0], amb_col, amb_type)
        return None

    r.resolve_in_tables = resolve_in_tables
    return r


def test_sibling_anchor_reroutes_ambiguous_datediff_to_a_column():
    # THE CORPUS `Days to Close` CASE, through the real reroute. `[Close Date]` uniquely pins Intake;
    # `[Created Date]` is a system field on many tables (base resolver -> None). The sibling anchor
    # binds `[Created Date]` to Intake (the sole pinned table carrying it), so the row-level DATEDIFF
    # reroutes to a faithful calculated COLUMN instead of staying a measure stub.
    resolve = _anchoring_resolver(
        {"Close Date": (_ANCHOR_INTAKE, "CloseDate", "dateTime")},
        ("Created Date", "CreatedDate", "dateTime"),
        {_ANCHOR_INTAKE, "Case", "Contact"},
    )
    measures = [{"name": "Days to Close",
                 "formula": "DATEDIFF('day', [Close Date], [Created Date])"}]
    keep, new_dims, moved = _reroute_row_level_measure_calcs(
        measures, [], resolve, known_tables={_ANCHOR_INTAKE, "Case", "Contact"})
    assert moved == ["Days to Close"]
    assert not keep
    assert {c["name"] for c in new_dims} == {"Days to Close"}


def test_sibling_anchor_is_load_bearing_stub_without_resolve_in_tables():
    # Prove the anchor is what flips it: the SAME resolver WITHOUT the `resolve_in_tables` primitive
    # leaves `[Created Date]` unresolved, the column probe fails, and the calc stays a measure stub
    # (identity-preserving fast path -- byte-identical to the pre-anchor behaviour).
    resolve = _anchoring_resolver(
        {"Close Date": (_ANCHOR_INTAKE, "CloseDate", "dateTime")},
        ("Created Date", "CreatedDate", "dateTime"),
        {_ANCHOR_INTAKE, "Case", "Contact"},
    )
    delattr(resolve, "resolve_in_tables")   # strip the primitive -> no anchoring possible
    measures = [{"name": "Days to Close",
                 "formula": "DATEDIFF('day', [Close Date], [Created Date])"}]
    keep, new_dims, moved = _reroute_row_level_measure_calcs(
        measures, [], resolve, known_tables={_ANCHOR_INTAKE, "Case", "Contact"})
    assert moved == []
    assert keep is measures        # nothing moved -> inputs returned unchanged
    assert new_dims == []


def test_sibling_anchor_fails_closed_when_two_pinned_tables_carry_the_caption():
    # Fail-closed at the resolver: when TWO unique siblings pin two different tables that BOTH carry
    # the ambiguous system column, `resolve_in_tables` sees >1 hit and returns None -> no anchor, so
    # the wrapper is a pass-through that still cannot resolve the ambiguous caption.
    resolve = _anchoring_resolver(
        {"Close Date": (_ANCHOR_INTAKE, "CloseDate", "dateTime"),
         "Amount": ("Case", "Amount", "double")},
        ("Created Date", "CreatedDate", "dateTime"),
        {_ANCHOR_INTAKE, "Case", "Contact"},   # both pinned tables (Intake, Case) carry it
    )
    wrapped = _sibling_anchored_resolver(
        "IF [Amount] > 0 THEN DATEDIFF('day', [Close Date], [Created Date]) END", resolve)
    assert wrapped is resolve                     # two pins -> ambiguous -> nothing anchored -> identity
    assert wrapped("Created Date") is None        # still unresolved (no false anchor)


# --- Cross-table row-level reroute needs `relationships` for the LOOKUPVALUE flip ---------------
#
# The real-workbook shape is NOT the sibling-anchor same-table collapse above. On the live consolidated
# model `[Created Date]` resolves DIRECTLY to Case (Case is the anchor table carrying a real
# `CreatedDate`), so `DATEDIFF('month', [Close Date], [Created Date])` is genuinely CROSS-TABLE
# ({Case, caseman__Intake__c}). Its faithful column form pulls the foreign `[Close Date]` into the Case
# home row via `LOOKUPVALUE(...)` -- which the column translator can only emit when it is given the
# model `relationships`. So the reroute's column probe MUST be handed `relationships`; without them the
# probe stubs `cross-table` and the calc wrongly stays a measure `= 0` stub. These tests lock that
# threading (the historical bug: the reroute called the column probe with NO `relationships=`).

_XT_REL_CASE_INTAKE = {  # Case (child/FK) -> caseman__Intake__c (parent/PK) -- the real Target-1 edge
    "from_table": "Case", "from_col": "caseman__Intake__c",
    "to_table": "caseman__Intake__c", "to_col": "Id", "cardinality": "many_to_one",
}


def _xt_datediff_measures():
    return [{"name": "Days to Close",
             "formula": "ZN(DATEDIFF('month', [Close Date], [Created Date]))"}]


def _xt_resolver():
    # Both captions resolve at top level, to DIFFERENT tables (no anchoring) -> a real cross-table calc.
    return _phys_resolver({
        "Close Date": ("caseman__Intake__c", "caseman__CloseDate__c", "dateTime"),
        "Created Date": ("Case", "CreatedDate", "dateTime"),
    })


def test_reroute_cross_table_datediff_stays_measure_without_relationships():
    # WITHOUT relationships the column probe cannot anchor the foreign `[Close Date]` -> it stubs
    # `cross-table` -> the calc stays on the measure path (fail-closed / byte-identical to before).
    measures = _xt_datediff_measures()
    keep, new_dims, moved = _reroute_row_level_measure_calcs(
        measures, [], _xt_resolver(), known_tables={"Case", "caseman__Intake__c"})
    assert moved == []
    assert keep is measures            # nothing moved -> inputs returned unchanged (identity fast path)
    assert new_dims == []


def test_reroute_cross_table_datediff_moves_to_column_with_relationships():
    # WITH relationships the column probe emits the LOOKUPVALUE flip -> `cdax` is not None -> the calc
    # reroutes off the measure path onto a faithful calculated COLUMN. This is the whole fix: the
    # reroute threads `relationships` into its column probe.
    measures = _xt_datediff_measures()
    keep, new_dims, moved = _reroute_row_level_measure_calcs(
        measures, [], _xt_resolver(), known_tables={"Case", "caseman__Intake__c"},
        relationships=[_XT_REL_CASE_INTAKE])
    assert moved == ["Days to Close"]
    assert not keep
    assert {c["name"] for c in new_dims} == {"Days to Close"}


def test_reroute_cross_table_datediff_unrelated_relationships_stays_measure():
    # Relationships are supplied but none bridge Case<->Intake -> no home -> the column probe still
    # stubs -> the calc stays a measure (never a guess when the tables are disconnected).
    unrelated = {"from_table": "People", "from_col": "RegionId",
                 "to_table": "Region", "to_col": "Id", "cardinality": "many_to_one"}
    measures = _xt_datediff_measures()
    keep, new_dims, moved = _reroute_row_level_measure_calcs(
        measures, [], _xt_resolver(), known_tables={"Case", "caseman__Intake__c"},
        relationships=[unrelated])
    assert moved == []
    assert {c["name"] for c in keep} == {"Days to Close"}
    assert new_dims == []


def test_sibling_anchored_resolver_identity_when_nothing_anchors():
    # Unit-level fail-closed guarantees on the wrapper itself: it returns the SAME object (never a
    # wrapper) when it cannot help -- no `resolve_in_tables`, fewer than 2 field tokens, or no
    # unresolved token to anchor.
    base = _anchoring_resolver(
        {"Close Date": (_ANCHOR_INTAKE, "CloseDate", "dateTime")},
        ("Created Date", "CreatedDate", "dateTime"),
        {_ANCHOR_INTAKE},
    )
    # No resolve_in_tables attr -> identity.
    plain = _phys_resolver({"A": ("F", "A", "int64")})
    assert _sibling_anchored_resolver("DATEDIFF('day',[A],[B])", plain) is plain
    # Fewer than 2 field tokens -> identity.
    assert _sibling_anchored_resolver("MAX([Close Date])", base) is base
    # Every token already resolves (no unresolved token) -> identity.
    both = _anchoring_resolver(
        {"Close Date": (_ANCHOR_INTAKE, "CloseDate", "dateTime"),
         "Created Date": (_ANCHOR_INTAKE, "CreatedDate", "dateTime")},
        ("Nonexistent", "X", "int64"), {_ANCHOR_INTAKE},
    )
    assert _sibling_anchored_resolver(
        "DATEDIFF('day',[Close Date],[Created Date])", both) is both


def test_sibling_anchored_resolver_never_overrides_a_base_hit_and_forwards_primitive():
    # The wrapper tries the base resolver FIRST (never overrides a real hit) and FORWARDS
    # `resolve_in_tables` so it composes with a later wrap.
    resolve = _anchoring_resolver(
        {"Close Date": (_ANCHOR_INTAKE, "CloseDate", "dateTime")},
        ("Created Date", "CreatedDate", "dateTime"),
        {_ANCHOR_INTAKE, "Case"},
    )
    wrapped = _sibling_anchored_resolver(
        "DATEDIFF('day',[Close Date],[Created Date])", resolve)
    assert wrapped is not resolve                                   # something anchored -> a wrapper
    assert wrapped("Close Date") == (_ANCHOR_INTAKE, "CloseDate", "dateTime")   # base hit preserved
    assert wrapped("Created Date") == (_ANCHOR_INTAKE, "CreatedDate", "dateTime")  # anchored
    assert getattr(wrapped, "resolve_in_tables", None) is resolve.resolve_in_tables


def test_anchoring_rc_lifts_a_per_calc_selector():
    # `_anchoring_rc(rc)` turns a `rc(calc) -> resolver` selector into one that also sibling-anchors,
    # using the calc's OWN formula to find the sibling pins.
    resolve = _anchoring_resolver(
        {"Close Date": (_ANCHOR_INTAKE, "CloseDate", "dateTime")},
        ("Created Date", "CreatedDate", "dateTime"),
        {_ANCHOR_INTAKE, "Case"},
    )
    arc = _anchoring_rc(lambda _calc: resolve)
    r = arc({"formula": "DATEDIFF('day',[Close Date],[Created Date])"})
    assert r("Created Date") == (_ANCHOR_INTAKE, "CreatedDate", "dateTime")
    # A calc whose formula gives no sibling pin -> the same base resolver (byte-identical).
    assert arc({"formula": "MAX([Close Date])"}) is resolve


def test_reroute_row_level_measure_cascade_end_to_end():
    # End-to-end through migrate_tds_to_semantic_model: a dimension calc L1 + two measure-role but
    # genuinely ROW-LEVEL calcs (L2 references L1; L3 differences two L2-siblings) all land as
    # faithful, translated calculated COLUMNS on the fact table -- the whole cascade unlocked by the
    # fix-point. (LIVE_SQLSERVER's Orders carries Sales(real) + Quantity(integer).)
    dim_calcs = [{"name": "Big Order", "formula": "[Quantity] > 5"}]        # L1 (dimension, bool)
    calcs = [
        {"name": "Big Qty", "formula": "IF [Big Order] THEN [Quantity] END"},  # L2 -> refs L1
        {"name": "Small Qty", "formula": "IF [Big Order] THEN [Sales] END"},   # L2 -> refs L1
        {"name": "Qty Gap", "formula": "[Big Qty] - [Small Qty]"},             # L3 -> refs 2x L2
    ]
    out = migrate_tds_to_semantic_model(
        LIVE_SQLSERVER, model_name="Superstore", calcs=calcs, dim_calcs=dim_calcs)
    measures = {r["measure"] for r in out["report"]["measures"]}
    columns = {r["column"]: r for r in out["report"]["calc_columns"]}

    # none of the row-level calcs remained a measure
    assert not ({"Big Qty", "Small Qty", "Qty Gap"} & measures)
    # all three cascaded onto the column path as translated columns on Orders
    for name in ("Big Qty", "Small Qty", "Qty Gap"):
        assert columns[name]["status"] == "translated", name
        assert columns[name]["table"] == "Orders", name
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "column 'Qty Gap' =" in orders
    assert "'Orders'[Big Qty] - 'Orders'[Small Qty]" in orders   # the diff resolved via the cascade


# ---------------------------------------------------------------------------
# FIX 1 -- smart island-scope selection (fail-closed doc-order-tag correction)
# ---------------------------------------------------------------------------

def test_calc_field_tokens_extracts_bracketed_fields():
    assert _calc_field_tokens("COUNTD(IF [Flag] THEN [Id (Contact)] END)") == {"Flag", "Id (Contact)"}
    assert _calc_field_tokens("{FIXED [Contact ID],[Program Name] : MAX([Total Score])}") == {
        "Contact ID", "Program Name", "Total Score"}
    # calc-ref + parameter tokens are captured (they are neutral -- resolve to None everywhere)
    assert _calc_field_tokens("[Calculation_1] + [Parameters].[Sel]") == {
        "Calculation_1", "Parameters", "Sel"}
    assert _calc_field_tokens("") == set()
    assert _calc_field_tokens(None) == set()
    assert _calc_field_tokens("SUM([Sales]) - LAST()") == {"Sales"}   # LAST() has no bracket


def _fake_resolver(mapping):
    """caption -> resolver returning (table, col, type) for known captions, else None."""
    def _r(name):
        return mapping.get((name or "").strip())
    return _r


def test_best_scoped_resolver_promotes_when_exactly_one_island_resolves_the_gap():
    # Tagged island (SD) leaves [Id (Contact)] unresolved; Intake resolves it -> promote to Intake.
    sd = _fake_resolver({"Flag": ("SDFlag", "F", "boolean")})               # no Id (Contact)
    intake = _fake_resolver({"Flag": ("IntakeFlag", "F", "boolean"),
                             "Id (Contact)": ("Contact", "Id", "int64")})
    base = _fake_resolver({})   # pooled: ambiguous -> None for both
    resolve_for = {"SD": sd, "Intake": intake}.get
    chosen = _best_scoped_resolver(
        "COUNTD(IF [Flag] THEN [Id (Contact)] END)", "SD", resolve_for, {"SD", "Intake"}, base)
    assert chosen is intake
    assert chosen("Id (Contact)") == ("Contact", "Id", "int64")


def test_best_scoped_resolver_keeps_tag_when_it_fully_resolves():
    # The tag already resolves every field -> UNCHANGED (byte-identical for translating calcs),
    # even though another island would ALSO resolve them (no promotion when there is no gap).
    sd = _fake_resolver({"Flag": ("SDFlag", "F", "boolean"),
                         "Id (Contact)": ("SDContact", "Id", "int64")})
    intake = _fake_resolver({"Flag": ("IntakeFlag", "F", "boolean"),
                             "Id (Contact)": ("Contact", "Id", "int64")})
    resolve_for = {"SD": sd, "Intake": intake}.get
    chosen = _best_scoped_resolver(
        "COUNTD(IF [Flag] THEN [Id (Contact)] END)", "SD", resolve_for, {"SD", "Intake"},
        _fake_resolver({}))
    assert chosen is sd


def test_best_scoped_resolver_fails_closed_when_two_islands_tie():
    # Two islands each resolve the missing field equally well -> ambiguous -> keep the tag.
    sd = _fake_resolver({})                                                  # tag resolves nothing
    intake = _fake_resolver({"Id (Contact)": ("Contact", "Id", "int64")})
    enroll = _fake_resolver({"Id (Contact)": ("Enroll", "Id", "int64")})
    resolve_for = {"SD": sd, "Intake": intake, "Enroll": enroll}.get
    chosen = _best_scoped_resolver(
        "COUNTD([Id (Contact)])", "SD", resolve_for, {"SD", "Intake", "Enroll"}, sd)
    assert chosen is sd   # >1 improving island -> fail closed


def test_best_scoped_resolver_keeps_tag_when_no_island_improves():
    sd = _fake_resolver({})
    intake = _fake_resolver({})   # nobody resolves the field
    resolve_for = {"SD": sd, "Intake": intake}.get
    chosen = _best_scoped_resolver("COUNTD([Nowhere])", "SD", resolve_for, {"SD", "Intake"}, sd)
    assert chosen is sd


def test_best_scoped_resolver_inert_without_resolve_for():
    # Single/standalone datasource (resolve_for=None) -> always the base resolver (byte-identical).
    base = _fake_resolver({"Sales": ("Orders", "Sales", "double")})
    chosen = _best_scoped_resolver("SUM([Sales])", "SD", None, {"SD"}, base)
    assert chosen is base


def test_best_scoped_resolver_promotes_from_base_when_tag_has_no_scoped_resolver():
    # A calc whose datasource tag has no scoped resolver falls back to base; if exactly one island
    # resolves its otherwise-unresolved field, promote to that island (still fail-closed).
    base = _fake_resolver({})                                               # pooled: None
    intake = _fake_resolver({"Amount": ("Intake", "Amount", "double")})
    resolve_for = {"Intake": intake}.get                                    # "SD" -> None
    chosen = _best_scoped_resolver("SUM([Amount])", "SD", resolve_for, {"SD", "Intake"}, base)
    assert chosen is intake


def test_measures_part_promotes_mistagged_calc_to_resolving_island_end_to_end():
    # Integration through _measures_part: a measure tagged to the WRONG island (SD, where [Amount]
    # is unresolved) is retagged at resolve-time to Intake (where it resolves) and TRANSLATES,
    # instead of stubbing -- proving FIX 1 flips a doc-order-mis-tag stub into a real measure.
    from assemble_model import _measures_part
    sd = _fake_resolver({})                                                  # SD: [Amount] -> None
    intake = _fake_resolver({"Amount": ("Contact", "Amount", "double")})     # Intake resolves it
    resolve_for = {"SD": sd, "Intake": intake}.get
    calcs = [{"name": "Total Amount", "formula": "SUM([Amount])", "datasource": "SD"},
             {"name": "_probe", "formula": "SUM([Amount])", "datasource": "Intake"}]  # seeds island set
    _, report, _ = _measures_part(calcs, sd, resolve_for=resolve_for, known_tables={"Contact"})
    rows = {r["measure"]: r for r in report}
    assert rows["Total Amount"]["status"] == "translated"
    assert "SUM('Contact'[Amount])" in rows["Total Amount"]["dax"]


def test_measures_part_mistag_stubs_without_the_fix_when_only_pooled_resolver():
    # Control: the SAME mis-tagged calc, with NO resolve_for (pooled resolver only), stays a stub --
    # confirming the promotion (not some incidental resolution) is what translates it above.
    from assemble_model import _measures_part
    base = _fake_resolver({})     # pooled cannot resolve [Amount]
    calcs = [{"name": "Total Amount", "formula": "SUM([Amount])", "datasource": "SD"}]
    _, report, _ = _measures_part(calcs, base, resolve_for=None, known_tables={"Contact"})
    rows = {r["measure"]: r for r in report}
    assert rows["Total Amount"]["status"] != "translated"


def test_measures_part_conformed_hub_flips_countd_if_and_cascades_cy_py():
    # Wiring + cascade proof at the _measures_part boundary. A cross-table COUNTD-IF (Current Year
    # Clients) is falsely ambiguous ONLY because the generated Date calendar is a shared transit hub
    # (SD -> Date -> PE -> Contact spurious paths alongside the real SD -> PE -> Contact). Passing
    # conformed_hubs={"Date"} excludes Date as a transit node -> the root flips to translated -> and
    # the CY-PY difference dependent that referenced it cascades to translated via measure_refs.
    from assemble_model import _measures_part
    from calc_to_dax import build_table_adjacency
    resolve = _fake_resolver({
        "Active Flag": ("SD", "Stage", "string"),
        "Id (Contact)": ("Contact", "Id", "string"),
    })
    adj = build_table_adjacency([
        {"from_table": "SD", "from_col": "PE_Id", "to_table": "PE", "to_col": "Id"},
        {"from_table": "PE", "from_col": "Contact_Id", "to_table": "Contact", "to_col": "Id"},
        {"from_table": "SD", "from_col": "DeliveryDate", "to_table": "Date", "to_col": "Date"},
        {"from_table": "PE", "from_col": "EndDate", "to_table": "Date", "to_col": "Date"},
        {"from_table": "PE", "from_col": "StartDate", "to_table": "Date", "to_col": "Date"},
    ])
    calcs = [
        {"name": "Current Year Clients",
         "formula": 'COUNTD(IF [Active Flag] = "Active" THEN [Id (Contact)] END)'},
        {"name": "Client Growth", "formula": "[Current Year Clients] + 100"},  # CY-PY-shaped dependent
    ]
    _, report, _ = _measures_part(
        calcs, resolve, known_tables={"Contact"}, related_tables=adj, conformed_hubs={"Date"})
    rows = {r["measure"]: r for r in report}
    assert rows["Current Year Clients"]["status"] == "translated"
    assert "DISTINCTCOUNTNOBLANK('Contact'[Id])" in rows["Current Year Clients"]["dax"]
    # the dependent cascades because its only blocker (the untranslatable root) now resolves
    assert rows["Client Growth"]["status"] == "translated"
    assert "[Current Year Clients]" in rows["Client Growth"]["dax"]


def test_measures_part_without_conformed_hub_both_countd_if_and_dependent_stub():
    # Control / fail-closed + byte-identical default: the SAME calcs + SAME Date-hub adjacency but NO
    # conformed_hubs leaves the root falsely ambiguous -> stub, and the dependent stubs with it. This
    # is exactly today's pre-fix behaviour, proving the hub exclusion (not something else) is the flip.
    from assemble_model import _measures_part
    from calc_to_dax import build_table_adjacency
    resolve = _fake_resolver({
        "Active Flag": ("SD", "Stage", "string"),
        "Id (Contact)": ("Contact", "Id", "string"),
    })
    adj = build_table_adjacency([
        {"from_table": "SD", "from_col": "PE_Id", "to_table": "PE", "to_col": "Id"},
        {"from_table": "PE", "from_col": "Contact_Id", "to_table": "Contact", "to_col": "Id"},
        {"from_table": "SD", "from_col": "DeliveryDate", "to_table": "Date", "to_col": "Date"},
        {"from_table": "PE", "from_col": "EndDate", "to_table": "Date", "to_col": "Date"},
        {"from_table": "PE", "from_col": "StartDate", "to_table": "Date", "to_col": "Date"},
    ])
    calcs = [
        {"name": "Current Year Clients",
         "formula": 'COUNTD(IF [Active Flag] = "Active" THEN [Id (Contact)] END)'},
        {"name": "Client Growth", "formula": "[Current Year Clients] + 100"},
    ]
    _, report, _ = _measures_part(
        calcs, resolve, known_tables={"Contact"}, related_tables=adj)  # conformed_hubs omitted
    rows = {r["measure"]: r for r in report}
    assert rows["Current Year Clients"]["status"] != "translated"
    assert rows["Client Growth"]["status"] != "translated"


def test_measure_report_carries_source_identity_for_viz_binding():
    # Cross-layer contract (additive): every measure row carries a deterministic `source` so the
    # viz/report layer can join a worksheet calc token -> this emitted measure. The Tableau internal
    # name (e.g. Calculation_xxxx) threads through as calc_instance_token; status decides bind-vs-degrade.
    calcs = [
        {"name": "Count Orders", "formula": "ZN(SUM([Quantity]))",
         "internal_name": "Calculation_0014172369248279"},
        {"name": "Profit Bucket", "formula": 'REGEXP_MATCH([Region], "^A")'},  # no internal_name; irreducible stub
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
        {"name": "Profit Bucket", "formula": 'REGEXP_MATCH([Region], "^A")'},  # stub, no token
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


def _rank_quick_usage(**kw):
    """A Rank quick table calc ranking 'Order ID' marks by Sum(Sales), Field scope. A Rank QTC has no
    ``aggregation`` attr -- the inner aggregate comes from the pill derivation ('Sum')."""
    d = dict(
        worksheet="Ranking", instance="rank:sum:Sales:qk", column="Sales", caption="Sales",
        kind="quick", calc_type="Rank", aggregation=None, derivation="Sum",
        rank_options="Competition,Descending", ordering_type="Field", ordering_fields=["Order ID"],
        rows=[Pill("none:Order ID:nk", "Order ID", "None")],
        cols=[Pill("sum:Sales:qk", "Sales", "Sum")],
    )
    d.update(kw)
    return TableCalcUsage(**d)


def test_rank_quick_calc_lands_as_translated_rankx_measure():
    # A Rank quick table calc lands as a derived, intent-suffixed _Measures measure carrying RANKX
    # (synthesized RANK(SUM([Sales]),'desc') -> the window seam), bound by its full instance token.
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=[], table_calc_usages=[_rank_quick_usage()])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    name = "Sales (rank within partition)"
    assert "measure 'Sales (rank within partition)' =" in measures
    assert "RANKX(ALLSELECTED('Orders'[Order_ID]), CALCULATE(SUM('Orders'[Sales])), , DESC, Skip)" \
        in measures
    pr = {r["measure"]: r for r in out["report"]["measures"]}[name]
    assert pr["status"] == "translated"
    src = pr["source"]
    assert src["kind"] == "table_calc"
    assert src["calc_instance_token"] == "rank:sum:Sales:qk"
    assert src["calc_id"] is None                  # a QTC never claims the bare base token
    assert src["intent"] == "rank within partition"
    assert src["partition_by"] == []
    assert src["order_by"] == [["Order ID", "ASC"]]
    # the heat-grid-style binder joins the rank by its full instance token
    assert out["report"]["calc_bindings"]["rank:sum:Sales:qk"]["measure_name"] == name


def test_rank_quick_calc_unique_ties_fails_closed():
    # Tableau's 'Unique' ranking breaks ties by addressing order -> not faithful in DAX; the rank
    # must NOT emit a measure (fail-closed), leaving the base model otherwise unchanged.
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=[], table_calc_usages=[
                                    _rank_quick_usage(rank_options="Unique,Descending")])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "rank within partition" not in measures
    assert not any(r["measure"] == "Sales (rank within partition)"
                   for r in out["report"]["measures"])


def _moving_quick_usage(**kw):
    """A moving-window quick table calc: trailing-3 average of Sum(Sales) across 'Order ID', Field
    scope. The integer from/to bounds make it a sliding (order-sensitive) frame."""
    d = dict(
        worksheet="Trend", instance="win:avg:Sales:qk", column="Sales", caption="Sales",
        kind="quick", calc_type="WindowTotal", aggregation="Avg", window_from=-2, window_to=0,
        ordering_type="Field", ordering_fields=["Order ID"],
        rows=[Pill("none:Order ID:nk", "Order ID", "None")],
        cols=[Pill("sum:Sales:qk", "Sales", "Sum")],
    )
    d.update(kw)
    return TableCalcUsage(**d)


def test_moving_window_quick_calc_lands_as_translated_measure():
    # A moving WindowTotal lands as a derived 'moving window' _Measures measure carrying the seam's
    # relative WINDOW(-2, REL, 0, REL, ...) frame, bound by its full instance token.
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=[], table_calc_usages=[_moving_quick_usage()])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    name = "Sales (moving window)"
    assert "measure 'Sales (moving window)' =" in measures
    assert "WINDOW(-2, REL, 0, REL, ORDERBY('Orders'[Order_ID], ASC))" in measures
    assert "CALCULATE(AVERAGE('Orders'[Sales]))" in measures
    pr = {r["measure"]: r for r in out["report"]["measures"]}[name]
    assert pr["status"] == "translated"
    src = pr["source"]
    assert src["kind"] == "table_calc"
    assert src["calc_instance_token"] == "win:avg:Sales:qk"
    assert src["intent"] == "moving window"
    assert src["order_by"] == [["Order ID", "ASC"]]
    assert out["report"]["calc_bindings"]["win:avg:Sales:qk"]["measure_name"] == name


def test_moving_window_quick_calc_multi_dim_fails_closed():
    # a moving frame is order-sensitive, so >1 addressing dim leaves the order ambiguous -> no measure.
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=[], table_calc_usages=[_moving_quick_usage(
                                    ordering_fields=["Order ID", "Quantity"],
                                    rows=[Pill("none:Order ID:nk", "Order ID", "None"),
                                          Pill("none:Quantity:nk", "Quantity", "None")])])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "moving window" not in measures
    assert not any(r["measure"] == "Sales (moving window)"
                   for r in out["report"]["measures"])


def _diff_quick_usage(**kw):
    """A difference-from-prior quick table calc: X - LOOKUP(X,-1) over Sum(Sales), addressed across
    'Order ID' (Field scope). Order-sensitive (looks back one row)."""
    d = dict(
        worksheet="Sales Diff", instance="diff:sum:Sales:qk", column="Sales", caption="Sales",
        kind="quick", calc_type="Difference", aggregation="Sum",
        ordering_type="Field", ordering_fields=["Order ID"],
        rows=[Pill("none:Order ID:nk", "Order ID", "None")],
        cols=[Pill("sum:Sales:qk", "Sales", "Sum")],
    )
    d.update(kw)
    return TableCalcUsage(**d)


def test_difference_quick_calc_lands_as_translated_measure():
    # A Difference quick table calc lands as a derived 'difference from a prior row' _Measures measure
    # carrying the OFFSET(-1,...) prior-row lookup with Tableau's null first row guarded by ISBLANK.
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=[], table_calc_usages=[_diff_quick_usage()])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    name = "Sales (difference from a prior row)"
    assert "measure 'Sales (difference from a prior row)' =" in measures
    assert "VAR _prev = CALCULATE(SUM('Orders'[Sales]), OFFSET(-1, ORDERBY('Orders'[Order_ID], ASC)))" \
        in measures
    assert "RETURN IF(ISBLANK(_prev), BLANK(), (SUM('Orders'[Sales])) - _prev)" in measures
    pr = {r["measure"]: r for r in out["report"]["measures"]}[name]
    assert pr["status"] == "translated"
    src = pr["source"]
    assert src["kind"] == "table_calc"
    assert src["calc_instance_token"] == "diff:sum:Sales:qk"
    assert src["intent"] == "difference from a prior row"
    assert out["report"]["calc_bindings"]["diff:sum:Sales:qk"]["measure_name"] == name


def test_difference_quick_calc_multi_dim_fails_closed():
    # Difference looks back one row, so it is order-sensitive: >1 addressing dim leaves the order
    # ambiguous -> no measure (never a guessed order), base model otherwise unchanged.
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=[], table_calc_usages=[_diff_quick_usage(
                                    ordering_fields=["Order ID", "Quantity"],
                                    rows=[Pill("none:Order ID:nk", "Order ID", "None"),
                                          Pill("none:Quantity:nk", "Quantity", "None")])])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "difference from a prior row" not in measures
    assert not any(r["measure"] == "Sales (difference from a prior row)"
                   for r in out["report"]["measures"])


def _pct_total_quick_usage(**kw):
    """A percent-of-total quick table calc: X / TOTAL(X) over Sum(Sales), addressed across 'Order ID'
    (Field scope). Order-INSENSITIVE (a whole-scope re-aggregation)."""
    d = dict(
        worksheet="Share of Total", instance="pctt:sum:Sales:qk", column="Sales", caption="Sales",
        kind="quick", calc_type="PercentOfTotal", aggregation="Sum",
        ordering_type="Field", ordering_fields=["Order ID"],
        rows=[Pill("none:Order ID:nk", "Order ID", "None")],
        cols=[Pill("sum:Sales:qk", "Sales", "Sum")],
    )
    d.update(kw)
    return TableCalcUsage(**d)


def test_percent_of_total_quick_calc_lands_as_translated_measure():
    # A PercentOfTotal quick table calc lands as a derived 'percent-of-scope ratio' _Measures measure
    # carrying DIVIDE(X, CALCULATE(X, ALLSELECTED(<scope>))), bound by its full instance token.
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=[], table_calc_usages=[_pct_total_quick_usage()])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    name = "Sales (percent-of-scope ratio)"
    assert "measure 'Sales (percent-of-scope ratio)' =" in measures
    assert "DIVIDE(SUM('Orders'[Sales]), CALCULATE(SUM('Orders'[Sales]), " \
        "ALLSELECTED('Orders'[Order_ID])))" in measures
    pr = {r["measure"]: r for r in out["report"]["measures"]}[name]
    assert pr["status"] == "translated"
    src = pr["source"]
    assert src["kind"] == "table_calc"
    assert src["calc_instance_token"] == "pctt:sum:Sales:qk"
    assert src["intent"] == "percent-of-scope ratio"
    assert out["report"]["calc_bindings"]["pctt:sum:Sales:qk"]["measure_name"] == name


def test_percent_of_total_quick_calc_multi_dim_translates():
    # The differentiator from Difference: percent-of-total is order-INSENSITIVE, so two addressing
    # dimensions stay faithful (the scope total spans both) -- it does NOT fail closed.
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=[], table_calc_usages=[_pct_total_quick_usage(
                                    ordering_fields=["Order ID", "Quantity"],
                                    rows=[Pill("none:Order ID:nk", "Order ID", "None"),
                                          Pill("none:Quantity:nk", "Quantity", "None")])])
    measures = out["parts"]["definition/tables/_Measures.tmdl"]
    name = "Sales (percent-of-scope ratio)"
    assert "measure 'Sales (percent-of-scope ratio)' =" in measures
    assert "ALLSELECTED('Orders'[Order_ID], 'Orders'[Quantity])" in measures
    pr = {r["measure"]: r for r in out["report"]["measures"]}[name]
    assert pr["status"] == "translated"


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


# -- B9: sibling calculated-column reference cascade (the column-mode peer of the measures'
#    measure_refs fix-point). A dimension calc that references ANOTHER dimension calc column resolved
#    to an unresolved stub before, because the sibling is being created and is absent from the
#    datasource-metadata resolver. The pre-scan cascade in _calc_columns_part seeds each single-home
#    deterministic translation so a bare [Sibling Calc] resolves to 'Home'[Sibling Calc] with its type.
def _sibling_chain_model(dim_calcs):
    return assemble_import_model(
        parse_tds(LIVE_SQLSERVER), model_name="Superstore", dim_calcs=dim_calcs)


def test_sibling_calc_chain_resolves_two_deep():
    # "Order Label" references sibling "Order Code" (which references the base [Order ID]). Declared
    # consumer-BEFORE-producer to prove the fix-point is order-independent (not a single forward pass).
    out = _sibling_chain_model([
        {"name": "Order Label", "formula": '[Order Code] + " (x)"'},
        {"name": "Order Code", "formula": "UPPER([Order ID])"},
    ])
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "column 'Order Code' = UPPER('Orders'[Order_ID])" in orders
    # the sibling reference now binds to the real model column instead of stubbing.
    assert "'Orders'[Order Code]" in orders
    rows = {r["column"]: r for r in out["report"]["calc_columns"]}
    assert rows["Order Label"]["status"] == "translated"
    assert rows["Order Label"]["table"] == "Orders"
    assert rows["Order Code"]["status"] == "translated"


def test_sibling_calc_chain_resolves_three_deep():
    out = _sibling_chain_model([
        {"name": "C", "formula": '[B] + "!"'},
        {"name": "B", "formula": '[Order Code] + "?"'},
        {"name": "Order Code", "formula": "UPPER([Order ID])"},
    ])
    rows = {r["column"]: r for r in out["report"]["calc_columns"]}
    assert rows["Order Code"]["status"] == "translated"
    assert rows["B"]["status"] == "translated"
    assert rows["C"]["status"] == "translated"
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "'Orders'[B]" in orders          # C resolved its sibling B
    assert "'Orders'[Order Code]" in orders  # B resolved its sibling Order Code


def test_no_sibling_chain_is_byte_identical_to_prior_single_pass():
    # When no calc references a sibling, the cascade records nothing and the main loop is unchanged.
    out = _dim_calc_model()
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert 'column \'Sales Flag\' = IF(\'Orders\'[Sales] > 0, "Y", "N")' in orders
    assert "column 'Order Code' = UPPER('Orders'[Order_ID])" in orders
    assert "column 'Avg Sale Col' = BLANK()" in orders  # aggregation still an honest stub


def test_cross_table_sibling_reference_stays_stub():
    # A calc that references a sibling on a DIFFERENT table than its other term spans >1 table -> the
    # single-table row-level guard falls back to an honest stub (never emits unfaithful cross-table DAX).
    base = {"Category": ("Orders", "Category", "string"),
            "Region": ("People", "Region", "string")}
    resolve = lambda c: base.get(c)
    dim = [
        {"name": "SibOrders", "formula": "UPPER([Category])"},
        {"name": "X", "formula": "[SibOrders] + [Region]"},
    ]
    _, report, _ = _calc_columns_part(dim, resolve, "Orders", known_tables={"Orders", "People"})
    rows = {r["column"]: r for r in report}
    assert rows["SibOrders"]["status"] == "translated"
    assert rows["X"]["status"] == "stub"
    assert rows["X"]["dax"] is None


def test_token_keyed_sibling_reference_resolves():
    # Real Tableau auto-named calcs are referenced in sibling formulas by their internal
    # ``[Calculation_*]`` token, NOT their caption. The producer now registers its ``column_refs`` entry
    # under both the caption AND the internal_name token, so a consumer that references it by token
    # resolves to the real ``'Table'[Caption]`` column (the stored column_name is the CAPTION, so the
    # emitted reference is correct regardless of which key matched).
    base = {"Order ID": ("Orders", "Order_ID", "string")}
    resolve = lambda c: base.get(c)
    dim = [
        # consumer references the producer by its internal token, declared BEFORE the producer to
        # prove the fix-point is order-independent.
        {"name": "Order Label", "formula": '[Calculation_9911] + " (x)"'},
        {"name": "Order Code", "internal_name": "Calculation_9911", "formula": "UPPER([Order ID])"},
    ]
    by_table, report, _ = _calc_columns_part(dim, resolve, "Orders", known_tables={"Orders"})
    rows = {r["column"]: r for r in report}
    assert rows["Order Code"]["status"] == "translated"
    assert rows["Order Label"]["status"] == "translated"
    # the token reference binds to the real calculated column by its CAPTION, never the raw token.
    orders_tmdl = by_table["Orders"]
    assert "'Orders'[Order Code]" in orders_tmdl
    # the token is never emitted as a DAX column reference (it survives only in the preserved
    # ``TableauFormula`` annotation of the original formula, which is expected).
    assert "'Orders'[Calculation_9911]" not in orders_tmdl


def test_token_keyed_reference_to_untranslatable_sibling_stays_stub():
    # Fail-closed is preserved: a token that points at a sibling which cannot be faithfully translated
    # (here an aggregation, invalid in a row-level column) is NOT recorded as a reference target, so the
    # consumer stubs instead of emitting an unfaithful reference.
    base = {"Sales": ("Orders", "Sales", "number")}
    resolve = lambda c: base.get(c)
    dim = [
        {"name": "Avg Col", "internal_name": "Calculation_7", "formula": "AVG([Sales])"},
        {"name": "Uses Avg", "formula": "[Calculation_7] + 1"},
    ]
    _, report, _ = _calc_columns_part(dim, resolve, "Orders", known_tables={"Orders"})
    rows = {r["column"]: r for r in report}
    assert rows["Avg Col"]["status"] == "stub"      # aggregation is an honest column-mode stub
    assert rows["Uses Avg"]["status"] == "stub"     # its token consumer stays a stub too
    assert rows["Uses Avg"]["dax"] is None


def test_sibling_calc_chain_resolves_by_internal_token_end_to_end():
    # End-to-end through the full assemble path (not just _calc_columns_part) to prove ``internal_name``
    # is threaded intact so a token-referencing chain resolves in a real model build.
    out = _sibling_chain_model([
        {"name": "Order Label", "formula": '[Calculation_9911] + " (x)"'},
        {"name": "Order Code", "internal_name": "Calculation_9911", "formula": "UPPER([Order ID])"},
    ])
    rows = {r["column"]: r for r in out["report"]["calc_columns"]}
    assert rows["Order Code"]["status"] == "translated"
    assert rows["Order Label"]["status"] == "translated"
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "'Orders'[Order Code]" in orders   # the token consumer bound to the caption column


def _model_with_calc_cols_and_measures(dim_calcs, calcs):
    return assemble_import_model(
        parse_tds(LIVE_SQLSERVER), model_name="Superstore",
        dim_calcs=dim_calcs, calcs=calcs)


def test_measure_lod_grain_over_calc_column_resolves_end_to_end():
    # v1.36.0 CASCADE: a calculated column is a genuine row-level model column, so a MEASURE whose
    # FIXED-LOD grain is that calc dimension must bind to it. The orchestrator layers the emitted
    # calc-column identities under the datasource resolver it hands the measure path, so
    # ``{ FIXED : AVG({ FIXED [Order Bucket] : SUM([Sales]) }) }`` -- where ``[Order Bucket]`` is
    # itself a calc column -- translates instead of stubbing on ``unresolved/ambiguous LOD dimension``.
    # This is the deterministic form of the real "Avg. Sales by Month LOD" nested-LOD-over-a-calc-date
    # witness. The inner LOD re-aggregates over the calc column's grain (SUMMARIZE) and the outer
    # table-scoped LOD averages it, ignoring filter context (ALL).
    out = _model_with_calc_cols_and_measures(
        dim_calcs=[{"name": "Order Bucket", "formula": "UPPER([Order ID])"}],
        calcs=[{"name": "Avg Sales by Bucket",
                "formula": "{ FIXED : AVG({ FIXED [Order Bucket] : SUM([Sales]) }) }"}])
    cols = {r["column"]: r for r in out["report"]["calc_columns"]}
    assert cols["Order Bucket"]["status"] == "translated"   # the grain is a real calc column
    meas = {r["measure"]: r for r in out["report"]["measures"]}
    assert meas["Avg Sales by Bucket"]["status"] == "translated"
    assert meas["Avg Sales by Bucket"]["dax"] == (
        "CALCULATE(AVERAGEX(SUMMARIZE('Orders', 'Orders'[Order Bucket]), "
        "CALCULATE(SUM('Orders'[Sales]))), ALL('Orders'))")


def test_measure_aggregation_over_numeric_calc_column_resolves_end_to_end():
    # A measure may AGGREGATE a calculated column: SUM([Double Qty]) where [Double Qty] = [Quantity]*2
    # is a real numeric column, so it binds to SUM('Orders'[Double Qty]) via the layered resolver.
    out = _model_with_calc_cols_and_measures(
        dim_calcs=[{"name": "Double Qty", "formula": "[Quantity] * 2"}],
        calcs=[{"name": "Total Double", "formula": "SUM([Double Qty])"}])
    meas = {r["measure"]: r for r in out["report"]["measures"]}
    assert meas["Total Double"]["status"] == "translated"
    assert meas["Total Double"]["dax"] == "SUM('Orders'[Double Qty])"


def test_measure_bare_reference_to_calc_column_reroutes_to_faithful_column_end_to_end():
    # v1.41.0 (Fix B) CONSISTENCY: a bare row-level reference ``[Order Bucket]`` in a measure-role calc
    # is genuinely ROW-LEVEL, so -- exactly like a DIRECT row-level measure calc such as
    # ``UPPER([Order ID])`` already does -- it reroutes onto the faithful calculated-COLUMN path (both
    # stub in measure mode with the same ``bare row-level field not valid in a measure`` reason; the
    # cascade-aware reroute resolves the sibling ``[Order Bucket]`` via ``column_refs`` and moves it).
    # The DEEPER invariant is preserved and asserted below: measure mode ITSELF never emits phantom
    # measure DAX for a bare row-level ref -- the reroute is what carries it to a column, not a
    # measure-resolver trick. Pre-Fix-B this calc stayed a stub ONLY because the reroute probe lacked
    # ``column_refs`` to see the sibling; that was an artifact, not a faithfulness boundary.
    out = _model_with_calc_cols_and_measures(
        dim_calcs=[{"name": "Order Bucket", "formula": "UPPER([Order ID])"}],
        calcs=[{"name": "Bare Ref", "formula": "[Order Bucket]"}])
    meas = {r["measure"] for r in out["report"]["measures"]}
    cols = {r["column"]: r for r in out["report"]["calc_columns"]}
    # no phantom measure was emitted -- the calc left the measure path entirely
    assert "Bare Ref" not in meas
    # it landed as a faithful, translated calc column (identical row-level value)
    assert cols["Bare Ref"]["status"] == "translated"
    assert cols["Bare Ref"]["table"] == "Orders"
    # fail-closed guard still holds at the source: measure mode alone stubs the bare row-level ref
    # (the augmented resolver never turns a bare calc-column ref into valid measure DAX)
    mdax, mreason, _ = translate_tableau_calc_to_dax(
        "[Order Bucket]", _phys_resolver({"Order Bucket": ("Orders", "Order Bucket", "string")}),
        known_tables={"Orders"})
    assert mdax is None and mreason == _ROW_LEVEL_IN_MEASURE_REASON


# --- case-insensitive calc/physical column collision -> rename the physical to "<name> (source)" --
# Power BI's engine is case-insensitive, so a calc dimension that case-aliases a physical column
# (calc ``sales`` beside physical ``Sales``) would declare TWO columns whose names differ only by
# case on one table -> Desktop refuses to open the .pbip. The generator keeps the report-facing calc
# name and renames the PHYSICAL column's MODEL name to "<name> (source)", leaving its sourceColumn /
# M header bound to the real DB header so data still loads. All downstream consumers (TMDL emit,
# resolver, calc DAX, gate) then agree by construction.

def test_unique_source_name_bumps_on_collision():
    # the base suffix is "<name> (source)"; a taken casefold bumps to "(source 2)", "(source 3)", ...
    assert _unique_source_name("Sales", set()) == "Sales (source)"
    assert _unique_source_name("Sales", {"sales (source)"}) == "Sales (source 2)"
    assert _unique_source_name("Sales", {"sales (source)", "sales (source 2)"}) == "Sales (source 3)"


def test_plan_source_suffix_renames_empty_when_no_collision():
    # a dim-calc whose name does NOT casefold-match any physical column plans no rename (common path)
    desc = parse_tds(LIVE_SQLSERVER)
    tables = [r for r in desc.get("relations", []) if r["kind"] in ("table", "custom_sql")]
    plan = _plan_source_suffix_renames(
        desc, tables, [{"name": "Sales Label", "formula": "[Sales]"}], None)
    assert plan == {}


def test_plan_source_suffix_renames_flags_case_collision():
    # calc ``sales`` casefold-matches physical ``Sales`` on ``Orders`` -> plan renames the PHYSICAL
    # column's model name (keyed by table-display + physical model name) to "Sales (source)"
    desc = parse_tds(LIVE_SQLSERVER)
    tables = [r for r in desc.get("relations", []) if r["kind"] in ("table", "custom_sql")]
    plan = _plan_source_suffix_renames(
        desc, tables, [{"name": "sales", "formula": "[Sales]"}], None)
    assert plan == {("Orders", "Sales"): "Sales (source)"}


def test_calc_case_aliases_physical_column_renames_source_end_to_end():
    # END-TO-END: calc ``sales`` = [Sales] beside physical ``Sales`` -> the physical column is emitted
    # as ``column 'Sales (source)'`` with ``sourceColumn: Sales`` (real header UNCHANGED, data loads),
    # the calc keeps its name ``sales`` and its DAX references the renamed physical column, and the
    # openability gate sees exactly ONE column per case-folded name -> the model opens.
    out = _model_with_calc_cols_and_measures(
        dim_calcs=[{"name": "sales", "formula": "[Sales]"}], calcs=[])
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "column 'Sales (source)'" in orders          # physical column renamed (model name only)
    assert "sourceColumn: Sales" in orders              # ... but its DB header binding is untouched
    assert "column sales = 'Orders'[Sales (source)]" in orders   # calc keeps its name, refs the rename
    cols = {r["column"]: r for r in out["report"]["calc_columns"]}
    assert cols["sales"]["status"] == "translated"
    assert cols["sales"]["dax"] == "'Orders'[Sales (source)]"
    verdict = check_model_openability(out["parts"])
    assert verdict["checks"]["no_duplicate_columns"] is True
    assert verdict["ok"] is True


def test_non_colliding_calc_leaves_physical_column_unrenamed_end_to_end():
    # a calc whose name does NOT case-collide with a physical column leaves every physical column's
    # model name byte-identical -- the "(source)" suffix appears nowhere (common-path no-op).
    out = _model_with_calc_cols_and_measures(
        dim_calcs=[{"name": "Sales Label", "formula": "[Sales]"}], calcs=[])
    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "(source)" not in orders
    assert "column Sales\n" in orders                    # physical column kept its original model name
    cols = {r["column"]: r for r in out["report"]["calc_columns"]}
    assert cols["Sales Label"]["dax"] == "'Orders'[Sales]"


# --- the rename follows onto the relationship endpoint (an atomic rename) -------------------------
# When the case-colliding physical column is ALSO a join key, Part A's rename of its model name to
# "<name> (source)" must be followed onto the relationship endpoint. Power BI resolves an endpoint by
# column name CASE-INSENSITIVELY, so a stale ``SALE.REGION`` endpoint would silently re-bind the join
# to the case-colliding calc that triggered the rename (harmless for a value-identical alias, wrong
# for a calc that reshapes the key). Rewriting the endpoint the same way makes the rename atomic --
# exactly how Power BI Desktop rewrites every reference when a column is renamed.

def test_rename_relationship_endpoints_follows_plan():
    # unit: only the endpoint whose (table, column) is a plan key is rewritten; the input is never
    # mutated (new dicts), and an empty plan returns an equal copy (the common no-collision path).
    rels = [
        {"from_table": "SALE", "from_col": "REGION", "to_table": "REP", "to_col": "REGION"},
        {"from_table": "SALE", "from_col": "Order_Key", "to_table": "RMA", "to_col": "Order_Key"},
    ]
    plan = {("SALE", "REGION"): "REGION (source)"}
    out = _rename_relationship_endpoints(rels, plan)
    assert out[0]["from_col"] == "REGION (source)" and out[0]["to_col"] == "REGION"  # SALE side only
    assert out[1]["from_col"] == "Order_Key" and out[1]["to_col"] == "Order_Key"     # unrelated join
    assert rels[0]["from_col"] == "REGION"                    # input not mutated
    assert _rename_relationship_endpoints(rels, {}) == rels   # empty plan -> equal copy


def test_calc_case_aliases_join_key_rewrites_relationship_endpoint_estate():
    # END-TO-END estate path (relationships auto-wired from the .tds): a dim calc ``region`` = [REGION]
    # case-aliases the physical join key ``REGION`` on SALE. The physical column is renamed to
    # ``REGION (source)`` AND the SALE-side relationship endpoint follows, so the join still points at
    # the real key. The non-colliding REP side is untouched; the openability backstop is satisfied.
    out = migrate_tds_to_semantic_model(
        FEDERATED_STAR, model_name="Star",
        dim_calcs=[{"name": "region", "formula": "[REGION]"}])
    sale = out["parts"]["definition/tables/SALE.tmdl"]
    assert "column 'REGION (source)'" in sale                    # physical join key renamed (Part A)
    rels = out["parts"]["definition/relationships.tmdl"]
    assert "fromColumn: SALE.'REGION (source)'" in rels          # endpoint follows the rename
    assert "fromColumn: SALE.REGION\n" not in rels               # the stale endpoint is gone
    assert "toColumn: REP.REGION" in rels                        # the non-colliding side is untouched
    verdict = check_model_openability(out["parts"])
    assert verdict["checks"]["relationship_columns_exist"] is True
    assert verdict["ok"] is True


def test_calc_case_aliases_join_key_rewrites_relationship_endpoint_explicit():
    # Same hardening via the EXPLICIT relationships path: when the caller passes ``relationships=``,
    # all_rels reads that local list, so the rename must be followed onto it at the call site too.
    # The caller's own list is not mutated in place (the rewrite returns a new list).
    rels_in = [{"from_table": "SALE", "from_col": "REGION", "to_table": "REP", "to_col": "REGION"}]
    out = migrate_tds_to_semantic_model(
        FEDERATED_STAR, model_name="Star",
        dim_calcs=[{"name": "region", "formula": "[REGION]"}], relationships=rels_in)
    rels = out["parts"]["definition/relationships.tmdl"]
    assert "fromColumn: SALE.'REGION (source)'" in rels
    assert "fromColumn: SALE.REGION\n" not in rels
    assert "toColumn: REP.REGION" in rels
    assert rels_in[0]["from_col"] == "REGION"                    # caller's list untouched (pure)


# --- v1.37.0: Tableau's stock "Number of Records" -> a Sum-aggregated column of 1s ----------------

def test_is_stock_row_count_calc_detects_stock_fields_and_fails_closed():
    # Detector unit: the classic name and the modern "Count of <Table>" both match ONLY with the
    # literal-1 formula; anything else (borrowed name, different formula, empty) fails closed.
    assert _is_stock_row_count_calc({"name": "Number of Records", "formula": "1"})
    assert _is_stock_row_count_calc({"name": "number of records", "formula": " 1 "})
    assert _is_stock_row_count_calc({"name": "Count of Orders", "formula": "1"})
    assert not _is_stock_row_count_calc({"name": "Number of Records", "formula": "2"})
    assert not _is_stock_row_count_calc({"name": "Always One", "formula": "1"})
    assert not _is_stock_row_count_calc({"name": "Count", "formula": "1"})   # "count of <table>" only
    assert not _is_stock_row_count_calc({"name": "Number of Records", "formula": ""})


def test_split_calcs_by_role_reroutes_stock_number_of_records_to_column():
    # v1.37.0: the stock 1-per-row field carries role=measure, but emitting it as a measure yields a
    # nonsense ``measure = 1`` (always 1, never the row count). The role splitter recognises it and
    # reroutes it to COLUMN mode, where it becomes a real column of 1s that Sum-aggregates to the
    # row count. A genuine measure alongside it is untouched.
    measures, dims = _split_calcs_by_role([
        {"name": "Number of Records", "formula": "1", "role": "measure"},
        {"name": "Total Sales", "formula": "SUM([Sales])", "role": "measure"},
    ])
    assert [c["name"] for c in measures] == ["Total Sales"]
    assert [c["name"] for c in dims] == ["Number of Records"]


def test_split_calcs_by_role_reroutes_modern_count_of_table_to_column():
    # Newer Tableau renames the stock field to ``Count of <Table>`` -- same 1-per-row semantics, so
    # it reroutes the same way.
    measures, dims = _split_calcs_by_role([
        {"name": "Count of Orders", "formula": "1", "role": "measure"},
    ])
    assert measures == []
    assert [c["name"] for c in dims] == ["Count of Orders"]


def test_split_calcs_by_role_only_reroutes_the_genuine_stock_field():
    # Fail-closed: a user measure that merely evaluates to 1 but is NOT the stock field stays a
    # measure, and a field that borrows the stock NAME but computes something else (formula != 1)
    # stays a measure too. Surgical -- neither is silently turned into a column of 1s.
    measures, dims = _split_calcs_by_role([
        {"name": "Always One", "formula": "1", "role": "measure"},          # not the stock name
        {"name": "Number of Records", "formula": "2", "role": "measure"},   # stock name, wrong formula
    ])
    assert {c["name"] for c in measures} == {"Always One", "Number of Records"}
    assert dims == []


def test_stock_number_of_records_emits_as_sum_aggregated_column_of_ones():
    # End-to-end emission: the rerouted stock field lands as a real ``column 'Number of Records' = 1``
    # on the fact (anchor) table -- int64 + summarizeBy sum so dropping it into a visual SUMs to the
    # row count (Tableau's auto-aggregation). It is emitted as a COLUMN, never a ``measure = 1``.
    out = _model_with_calc_cols_and_measures(
        dim_calcs=[{"name": "Number of Records", "formula": "1"}], calcs=[])
    cols = {r["column"]: r for r in out["report"]["calc_columns"]}
    assert cols["Number of Records"]["status"] == "translated"
    assert cols["Number of Records"]["dax"] == "1"
    assert cols["Number of Records"]["table"] == "Orders"

    orders = out["parts"]["definition/tables/Orders.tmdl"]
    assert "column 'Number of Records' = 1" in orders
    # scope the type + default-aggregation assertions to the Number of Records column block
    blk = orders[orders.index("column 'Number of Records'"):]
    nxt = re.search(r"\n\t(?:column|measure|partition|hierarchy) ", blk[30:])
    blk = blk[: (30 + nxt.start()) if nxt else len(blk)]
    assert "dataType: int64" in blk
    assert "summarizeBy: sum" in blk

    # NEVER a measure = 1
    assert "measure 'Number of Records'" not in out["parts"].get(
        "definition/tables/_Measures.tmdl", "")



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


def test_argmax_and_argmin_nested_lod_measures_translate_deterministically_as_columns():
    # v1.34.0 CASCADE: once column mode translates FIXED LODs in place, a measure-LABELLED but
    # genuinely row-level nested-FIXED-LOD argmax/argmin (IF {FIXED d : MAX({FIXED d,e : agg})} =
    # {FIXED d,e : agg} THEN [dim] END) is reclassified onto the calc-column path by the row-level
    # pre-router and translates DETERMINISTICALLY + faithfully -- so it no longer needs the assisted
    # idiom registry. Deterministic beating the assisted tier is the correct priority, and it is the
    # nested-calc "cascading win": the secondary compiler no longer has to sift this nesting chain.
    calcs = [
        {"name": "Top City", "formula": _ARGMAX_MEASURE_FORMULA, "internal_name": "Calc_argmax"},
        {"name": "Bottom City", "formula": _ARGMIN_MEASURE_FORMULA, "internal_name": "Calc_argmin"},
    ]
    out = assemble_import_model(parse_tds(_ARGMAX_MODEL_TDS), model_name="Superstore", calcs=calcs)
    cols = {r["column"]: r for r in out["report"]["calc_columns"]}
    assert cols["Top City"]["status"] == "translated"
    assert cols["Top City"]["table"] == "Orders"
    assert cols["Top City"]["dax"] == (
        "IF(CALCULATE(MAXX(SUMMARIZE('Orders', 'Orders'[State], 'Orders'[City]), "
        "CALCULATE(SUM('Orders'[Sales]))), ALLEXCEPT('Orders', 'Orders'[State])) = "
        "CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[State], 'Orders'[City])), "
        "'Orders'[City])")
    assert cols["Bottom City"]["status"] == "translated"
    assert cols["Bottom City"]["dax"] == (
        "IF(CALCULATE(MINX(SUMMARIZE('Orders', 'Orders'[State], 'Orders'[City]), "
        "CALCULATE(SUM('Orders'[Sales]))), ALLEXCEPT('Orders', 'Orders'[State])) = "
        "CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[State], 'Orders'[City])), "
        "'Orders'[City])")
    # deterministic won: neither is left on the measure path or handed to the assisted tier
    assert "Top City" not in {r["measure"] for r in out["report"]["measures"]}
    assert out["report"]["assisted_suggestions"] == []


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


# -- approved_calc_dax dict form: an approval may also name the calc column's home table -----------
# (AAR #1 Issue C). A stubbed row-context calc otherwise defaults to the anchor table, where its
# row-context DAX references columns that don't exist -> invalid. The additive dict value
# ({"dax": ..., "table": ...}) lets the approver place the column on its real table.
def test_approved_dim_calc_dict_form_targets_named_table():
    # FEDERATED_STAR has SALE (anchor) + REP + RMA. A stubbed dim calc lands on SALE by default; the
    # dict form redirects it onto REP, and it must NOT also remain on the computed home.
    dim_calcs = [{"name": "Geo Tag", "formula": "AVG([REGION])"}]
    out = assemble_import_model(
        parse_tds(FEDERATED_STAR), model_name="Star", dim_calcs=dim_calcs,
        approved_calc_dax={"Geo Tag": {"dax": '"US"', "table": "REP"}})
    row = {r["column"]: r for r in out["report"]["calc_columns"]}["Geo Tag"]
    assert row["status"] == "assisted-approved"
    assert row["table"] == "REP"                 # redirected off the anchor SALE
    assert row["approved_target"] == "REP"
    assert '"US"' in out["parts"]["definition/tables/REP.tmdl"]
    assert '"US"' not in out["parts"]["definition/tables/SALE.tmdl"]
    assert "annotation TranslatedBy = assisted translation (human-approved)" in \
        out["parts"]["definition/tables/REP.tmdl"]


def test_approved_dim_calc_dict_form_unknown_table_keeps_computed_home():
    # A named table that isn't a real model table would be silently dropped by the per-table inject,
    # so the column keeps its computed home (SALE) and the miss is flagged, never lost.
    dim_calcs = [{"name": "Geo Tag", "formula": "AVG([REGION])"}]
    out = assemble_import_model(
        parse_tds(FEDERATED_STAR), model_name="Star", dim_calcs=dim_calcs,
        approved_calc_dax={"Geo Tag": {"dax": '"US"', "table": "Nope"}})
    row = {r["column"]: r for r in out["report"]["calc_columns"]}["Geo Tag"]
    assert row["status"] == "assisted-approved"
    assert row["table"] == "SALE"                 # unknown target ignored -> computed home kept
    assert row["approved_target"] == "Nope"
    assert row["approved_target_unknown"] == "Nope"
    assert '"US"' in out["parts"]["definition/tables/SALE.tmdl"]


def test_approved_dim_calc_string_form_lands_on_computed_home_unchanged():
    # The flat string form is byte-compatible: no target table -> the column stays on its home (SALE),
    # and no approved_target/approved_target_unknown key is added.
    dim_calcs = [{"name": "Geo Tag", "formula": "AVG([REGION])"}]
    out = assemble_import_model(
        parse_tds(FEDERATED_STAR), model_name="Star", dim_calcs=dim_calcs,
        approved_calc_dax={"Geo Tag": '"US"'})
    row = {r["column"]: r for r in out["report"]["calc_columns"]}["Geo Tag"]
    assert row["status"] == "assisted-approved"
    assert row["table"] == "SALE"
    assert "approved_target" not in row and "approved_target_unknown" not in row
    assert '"US"' in out["parts"]["definition/tables/SALE.tmdl"]


def test_approved_entry_normalises_string_and_dict_forms():
    # string form -> (dax, None)
    assert _approved_entry('"US"') == ('"US"', None)
    # dict form -> (dax, table); table whitespace trimmed
    assert _approved_entry({"dax": "X", "table": "  REP "}) == ("X", "REP")
    # dict with a blank/absent table -> (dax, None)
    assert _approved_entry({"dax": "X", "table": "  "}) == ("X", None)
    assert _approved_entry({"dax": "X"}) == ("X", None)
    # fail-closed: no usable dax, or an unexpected type -> (None, None)
    assert _approved_entry({"table": "REP"}) == (None, None)
    assert _approved_entry({"dax": "   "}) == (None, None)
    assert _approved_entry(None) == (None, None)
    assert _approved_entry(5) == (None, None)


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


# -- Spec 6: base-table -> consolidated-name map + column resolver ----------------------------

def test_report_table_map_and_resolve_consolidated_column():
    # Two datasources, each with an 'Orders' table -> the second is renamed on consolidation.
    # The report surfaces the map, and resolve_consolidated_column disambiguates a colliding
    # caption using the datasource, returning the EXACT emitted 'table'[col] the model wrote.
    a = parse_tds(LIVE_SQLSERVER)
    b = parse_tds(LIVE_SQLSERVER)
    combined = combine_descriptors([a, b], captions=["Sales", "Finance"])
    assert combined["table_map"] == {"Sales||Orders": "Orders",
                                     "Finance||Orders": "Orders (Finance)"}
    out = assemble_import_model(combined, model_name="Consolidated")
    report = out["report"]
    assert report["table_map"] == combined["table_map"]
    # 'Sales' caption exists on both Orders tables -> resolver picks the right one per datasource.
    assert resolve_consolidated_column(report, "Sales", "Sales") == "'Orders'[Sales]"
    assert resolve_consolidated_column(report, "Finance", "[Sales]") == "'Orders (Finance)'[Sales]"
    # unknown caption fails closed
    assert resolve_consolidated_column(report, "Sales", "Nope") is None


def test_single_datasource_report_has_no_table_map():
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore")
    assert "table_map" not in out["report"]
    # helper still degrades gracefully (single-table caption resolves via the manifest alone)
    assert resolve_consolidated_column(out["report"], "Superstore", "Sales") == "'Orders'[Sales]"
    assert resolve_consolidated_column(None, "x", "y") is None


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


# -- Roots-first foundation-inline: a zero-physical-table row-invariant calc (TODAY()) must be laid
#    as a column FIRST so its dependents can inline it, killing the bare-row-level stub family. -----
def test_row_invariant_foundation_calc_inlines_into_dependent_column_end_to_end():
    """A zero-physical-table foundation calc (``Today = TODAY()``) and its DATEDIFF dependent
    (``Age = DATEDIFF('year', [Order Date], [Today])``) BOTH land as translated calc columns, with
    ``[Today]`` inlined into ``Age`` as the parenthesized body ``(TODAY())``.

    This is the roots-first / dependency-order fix (build the foundation before the dependent), NOT a
    translator-capability gap: ``Today`` renders fine on its own; the only failure without the fix is
    that ``_build_column_refs`` dropped the zero-table foundation, so ``Age``'s ``[Today]`` never
    resolved and ``Age`` collapsed to a ``bare row-level`` stub. The fix registers the foundation's
    rendered DAX behind a NUL sentinel so a row-level dependent inlines it verbatim.
    """
    out = assemble_import_model(
        parse_tds(_DATE_BAND_SQLSERVER), model_name="Superstore",
        dim_calcs=[
            {"name": "Today", "formula": "TODAY()",
             "internal_name": "Calculation_today0001"},
            {"name": "Age", "formula": "DATEDIFF('year', [Order Date], [Today])",
             "internal_name": "Calculation_age00002"},
        ])
    cols = {r["column"]: r for r in out["report"]["calc_columns"]}

    # The foundation lands as its own translated column on the anchor fact table.
    assert cols["Today"]["status"] == "translated"
    assert cols["Today"]["table"] == "Orders"
    assert cols["Today"]["dax"] == "TODAY()"

    # The dependent resolves ONLY because the foundation was laid first: [Today] is inlined as the
    # parenthesized rendered body (TODAY()) — no dangling [Today], no bare-row-level stub.
    assert cols["Age"]["status"] == "translated"
    assert cols["Age"]["table"] == "Orders"
    assert cols["Age"]["dax"] == "DATEDIFF('Orders'[Order_Date], (TODAY()), YEAR)"
    assert "[Today]" not in cols["Age"]["dax"]

    # The emitted TMDL carries both columns, and the NUL registration sentinel never leaks into it.
    orders_tmdl = next(t for p, t in out["parts"].items() if p.endswith("Orders.tmdl"))
    assert "column Today = TODAY()" in orders_tmdl
    assert "column Age = DATEDIFF('Orders'[Order_Date], (TODAY()), YEAR)" in orders_tmdl
    assert _INLINE_REF_SENTINEL not in orders_tmdl


def test_measure_over_row_invariant_foundation_stays_stub_end_to_end():
    """FIX-C fail-closed guard: a MEASURE that references the zero-physical-table foundation
    (``MAX([Today])`` over ``Today = TODAY()``) must stay an honest stub — the measure resolver
    must never see the NUL inline sentinel.

    Without the sentinel-strip in the measure path, ``MAX([Today])`` mis-unpacks the 3-tuple sentinel
    entry and emits GARBAGE DAX ``MAX('<NUL>__inline_row_invariant__'[TODAY()])``. ``MAX`` (not
    ``SUM``) is the load-bearing witness here: ``SUM([Today])`` would fail its numeric typecheck first
    and stub either way (a vacuous test), whereas ``MAX`` accepts a date and would emit the garbage.
    """
    out = assemble_import_model(
        parse_tds(_DATE_BAND_SQLSERVER), model_name="Superstore",
        dim_calcs=[{"name": "Today", "formula": "TODAY()",
                    "internal_name": "Calculation_today0001"}],
        calcs=[{"name": "Latest Today", "formula": "MAX([Today])",
                "internal_name": "Calculation_latest001"}])
    rows = {r["measure"]: r for r in out["report"]["measures"]}

    assert rows["Latest Today"]["status"] == "stub"
    assert not rows["Latest Today"]["dax"]
    assert "[Today]" in rows["Latest Today"]["reason"]

    # The NUL sentinel must never leak into ANY emitted TMDL part (the real fail-closed guarantee).
    for part_text in out["parts"].values():
        assert _INLINE_REF_SENTINEL not in part_text


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
    assert "needs-storage-decision" in str(ei.value).lower()


def test_assemble_physical_join_builds_multi_table_model_with_relationships():
    # A physical join tree WITH real join clauses + columns now rebuilds as a multi-table model --
    # one table per surfaced leaf, wired by the relationships recovered from its <clause> keys --
    # instead of collapsing to an opaque combination the storage policy could only skip.
    out = migrate_tds_to_semantic_model(PHYSICAL_JOIN_KEYS, model_name="Joined")
    parts = out["parts"]
    assert "definition/tables/Orders.tmdl" in parts
    assert "definition/tables/Customer.tmdl" in parts
    # the role-playing alias surfaces as its own model table.
    assert "definition/tables/Manager.tmdl" in parts
    rels = parts["definition/relationships.tmdl"]
    assert "fromColumn: Orders.CustomerId" in rels and "toColumn: Customer.Id" in rels
    assert "fromColumn: Orders.ManagerId" in rels and "toColumn: Manager.Id" in rels
    reported = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"])
                for r in out["report"]["relationships"]}
    assert reported == {
        ("Orders", "CustomerId", "Customer", "Id"),
        ("Orders", "ManagerId", "Manager", "Id"),
    }


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


def test_authored_relationships_emit_many_to_many():
    # A Tableau object-graph relationship (the "noodle") is an ad-hoc, uniqueness-agnostic join:
    # Tableau never requires a unique key on either side. Power BI's DEFAULT many-to-one DOES, and
    # on a non-unique authored target it rejects the relationship and cancels the WHOLE batch on
    # first refresh -- collateral-dropping the generated Date join. So every authored relationship
    # is emitted many-to-many (single-direction dim->fact cross filter), which Power BI accepts
    # without a uniqueness check. This is connection-type-agnostic (pure .tds metadata). [cause #1]
    out = migrate_tds_to_semantic_model(FEDERATED_STAR, model_name="Star")
    rels = out["parts"]["definition/relationships.tmdl"]
    # both authored joins (SALE->REP, SALE->RMA) are many-to-many
    assert rels.count("toCardinality: many") == 2
    assert rels.count("crossFilteringBehavior: oneDirection") == 2
    # the cardinality is carried additively on every reported (authored) relationship
    assert out["report"]["relationships"]
    assert all(r.get("cardinality") == "many_to_many"
               for r in out["report"]["relationships"])


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
    # NO rename in the M partition: the spaced remote name binds via a quoted sourceColumn in the
    # TMDL (fold-safe -- a rename above the folded native query breaks at query time in Fabric).
    assert "Table.RenameColumns" not in part
    assert 'column Order_ID' in part and 'sourceColumn: "Order ID"' in part
    # a real, deploy-ready partition is NOT flagged for review (additive report keys present)
    report = out["report"]
    assert report["partitions_stubbed"] == 0
    assert report["partitions_needs_review"] == []


# -- Windows long-path (\\?\) writer: lift the 260 MAX_PATH limit for deep PBIR/TMDL writes ----------
def test_win_long_path_prefixes_on_windows_noop_elsewhere():
    import os
    p = os.path.join(os.getcwd(), "a", "b", "c", "deep.json")
    out = _win_long_path(p)
    if os.name == "nt":
        assert out == "\\\\?\\" + os.path.abspath(p)  # extended-length prefix on Windows
        assert _win_long_path(out) == out            # idempotent -- never double-prefixes
    else:
        assert out == os.path.abspath(p)             # pure no-op off Windows
    assert _win_long_path("") == ""                  # falsy passthrough
    assert _win_long_path(None) is None


def test_win_long_path_unc_form():
    import os
    if os.name != "nt":
        pytest.skip("UNC \\\\?\\UNC form is Windows-only")
    unc = "\\\\server\\share\\deep\\model.tmdl"
    assert _win_long_path(unc) == "\\\\?\\UNC\\server\\share\\deep\\model.tmdl"


def test_write_model_folder_writes_past_max_path():
    # The canonical writer lands a part whose absolute path exceeds MAX_PATH (260). On Windows this only
    # succeeds via the \\?\ long-path handling; the returned path list stays CLEAN (no prefix leak).
    import os
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="wmf1b_")
    try:
        dest = os.path.join(base, "M" * 40 + ".SemanticModel")
        deep_rel = "definition/tables/" + "T" * 90 + "/" + "C" * 90 + ".tmdl"
        parts = {deep_rel: "table body", "definition.pbism": "{}"}
        full = os.path.abspath(os.path.join(dest, *deep_rel.split("/")))
        assert len(full) >= 260                       # genuinely over the MAX_PATH budget
        written = write_model_folder(parts, dest)
        assert os.path.exists(_win_long_path(full))   # the deep file really landed on disk
        assert all(not w.startswith("\\\\?\\") for w in written)  # reported paths are clean
    finally:
        shutil.rmtree(_win_long_path(base), ignore_errors=True)


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


def test_databricks_doubled_custom_sql_emits_clean_partition_and_report():
    # Tableau's on-disk bracket doubling is reversed at the parse boundary, so neither the emitted
    # M partition nor the SQL surfaced in the report carries '<<'/'>>'. No parameter -> no review.
    out = migrate_tds_to_semantic_model(DATABRICKS_CUSTOM_SQL_DOUBLED, model_name="DbxSQL")
    part = out["parts"]["definition/tables/Custom SQL Query.tmdl"]
    assert "<<" not in part and ">>" not in part
    assert "WHERE o.Profit < 0" in part
    assert "Value.NativeQuery(Catalog, " in part
    report = out["report"]
    assert report["partitions_stubbed"] == 0
    assert report["partitions_needs_review"] == []


def test_databricks_custom_sql_parameter_is_flagged_needs_review():
    # A recovered <[Parameters].[Threshold]> token can't be translated yet: the partition still emits
    # a real native query, but the datasource is flagged needs_review (additively) with the
    # de-escaped SQL preserved -- not silently shipped to fail at refresh.
    out = migrate_tds_to_semantic_model(DATABRICKS_CUSTOM_SQL_PARAM, model_name="DbxSQL")
    report = out["report"]
    assert report["partitions_stubbed"] == 1
    entry = report["partitions_needs_review"][0]
    assert entry["table"] == "Custom SQL Query"
    assert entry["kind"] == "m_partition"
    assert "<[Parameters].[Threshold]>" in entry["reason"]
    assert "<[Parameters].[Threshold]>" in entry["sql"]      # de-escaped SQL carried for the reviewer
    # the emitted partition is still a real native query (deploy-valid), not a scaffold
    part = out["parts"]["definition/tables/Custom SQL Query.tmdl"]
    assert "Value.NativeQuery(Catalog, " in part
    assert "#table(type table [], {})" not in part



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
    # Two single-field aggregates translate; the REGEXP calc (no DAX regex) stays an inert stub.
    calcs = [
        {"name": "Profit Ratio", "formula": "SUM([Sales])/SUM([Quantity])"},
        {"name": "Avg Sale", "formula": "AVG([Sales])"},
        {"name": "Profit Bucket", "formula": 'REGEXP_MATCH([Region], "^A")'},
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
    assert by["Profit Bucket"]["tableau_formula"] == 'REGEXP_MATCH([Region], "^A")'


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


# -- approved-keystone nested cascade seeding (measures side) ---------------------------------------
#    An irreducible base (e.g. a WINDOW_ table calc) authored via approved_calc_dax is seeded into
#    the cross-calc reference map BEFORE the deterministic fix-point, so every dependent that stubbed
#    ONLY because that base was an untranslatable measure now translates by referencing the approved
#    measure -- and so do ITS dependents, to any depth. This is the second-compiler "author one
#    keystone, get the whole chain" cascade. Faithful + fail-closed throughout.
def _keystone_measures(*, approved=None):
    # Slope is a WINDOW_ table calc -> the deterministic tier cannot render it at datasource scope,
    # so it STUBS unless approved. Residuals references it by CAPTION; AboveBelow references Residuals.
    calcs = [
        {"name": "Slope", "formula": "WINDOW_SUM(SUM([Sales]))",
         "role": "measure", "internal_name": "Calculation_11"},
        {"name": "Residuals", "formula": "SUM([Sales]) - [Slope]",
         "role": "measure", "internal_name": "Calculation_22"},
        {"name": "AboveBelow", "formula": "[Residuals] > 0",
         "role": "measure", "internal_name": "Calculation_33"},
    ]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                               calcs=calcs, dim_calcs=[], approved_calc_dax=approved)
    return {r["measure"]: r for r in out["report"]["measures"]}


def test_approved_keystone_cascades_to_dependent_measure():
    # Authoring ONLY the irreducible Slope keystone makes its 1-level dependent Residuals translate
    # deterministically by referencing the approved measure [Slope].
    m = _keystone_measures(approved={"Slope": "CALCULATE(SUM('Orders'[Sales]))"})
    assert m["Slope"]["status"] == "assisted-approved"
    assert m["Residuals"]["status"] == "translated"
    assert m["Residuals"]["dax"] == "SUM('Orders'[Sales]) - [Slope]"
    # provenance preserved on the cascaded dependent
    assert m["Residuals"]["tableau_formula"] == "SUM([Sales]) - [Slope]"


def test_approved_keystone_cascades_two_levels():
    # The seed precedes the fix-point, so a dependent-of-a-dependent (AboveBelow -> Residuals ->
    # Slope) cascades too -- to any depth -- from a single authored keystone.
    m = _keystone_measures(approved={"Slope": "CALCULATE(SUM('Orders'[Sales]))"})
    assert m["Residuals"]["status"] == "translated"
    assert m["AboveBelow"]["status"] == "translated"
    assert m["AboveBelow"]["dax"] == "[Residuals] > 0"


def test_approved_keystone_seeding_inert_without_approvals():
    # Fail-closed / inert: with NO approvals the whole chain stubs exactly as before (the seeding loop
    # skips every calc that has no approved DAX), so the seeding never invents a translation.
    m = _keystone_measures(approved=None)
    assert m["Slope"]["status"] == "stub"
    assert m["Residuals"]["status"] == "stub"
    assert m["AboveBelow"]["status"] == "stub"
    assert m["Residuals"]["dax"] is None


def test_approved_keystone_cascade_by_internal_token():
    # Dependents that reference the keystone by its internal Calculation_* token (not the caption)
    # also cascade -- the keystone is seeded under BOTH keys.
    calcs = [
        {"name": "Slope", "formula": "WINDOW_SUM(SUM([Sales]))",
         "role": "measure", "internal_name": "Calculation_11"},
        {"name": "Residuals", "formula": "SUM([Sales]) - [Calculation_11]",
         "role": "measure", "internal_name": "Calculation_22"},
    ]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                               calcs=calcs, dim_calcs=[],
                               approved_calc_dax={"Slope": "CALCULATE(SUM('Orders'[Sales]))"})
    m = {r["measure"]: r for r in out["report"]["measures"]}
    assert m["Residuals"]["status"] == "translated"
    assert m["Residuals"]["dax"] == "SUM('Orders'[Sales]) - [Slope]"


def test_approved_keystone_dtype_bool_cascades_through_if():
    # A boolean keystone declared via the additive dict-form dtype cascades through an IF condition:
    # authoring Flag (a boolean table calc) lets Counter -> Both (2-level) translate.
    calcs = [
        {"name": "Flag", "formula": "WINDOW_SUM(SUM([Sales])) > 0",
         "role": "measure", "internal_name": "Calculation_A"},
        {"name": "Counter", "formula": "IF [Flag] THEN 1 ELSE 0 END",
         "role": "measure", "internal_name": "Calculation_B"},
        {"name": "Both", "formula": "IF [Flag] AND ([Counter] > 0) THEN 1 ELSE 0 END",
         "role": "measure", "internal_name": "Calculation_C"},
    ]
    approved = {"Flag": {"dax": "CALCULATE(SUM('Orders'[Sales]))>0", "dtype": "boolean"}}
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                               calcs=calcs, dim_calcs=[], approved_calc_dax=approved)
    m = {r["measure"]: r for r in out["report"]["measures"]}
    assert m["Flag"]["status"] == "assisted-approved"
    assert m["Counter"]["status"] == "translated"
    assert m["Counter"]["dax"] == "IF([Flag], 1, 0)"
    assert m["Both"]["status"] == "translated"


def test_approved_keystone_default_dtype_fails_closed_for_boolean():
    # Without the dtype hint the keystone defaults to "number"; a dependent that BRANCHES on it as a
    # boolean can only stub (the type check fails closed) -- never emit wrong DAX.
    calcs = [
        {"name": "Flag", "formula": "WINDOW_SUM(SUM([Sales])) > 0",
         "role": "measure", "internal_name": "Calculation_A"},
        {"name": "Counter", "formula": "IF [Flag] THEN 1 ELSE 0 END",
         "role": "measure", "internal_name": "Calculation_B"},
    ]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                               calcs=calcs, dim_calcs=[],
                               approved_calc_dax={"Flag": "CALCULATE(SUM('Orders'[Sales]))>0"})
    m = {r["measure"]: r for r in out["report"]["measures"]}
    assert m["Flag"]["status"] == "assisted-approved"
    assert m["Counter"]["status"] == "stub"
    assert m["Counter"]["dax"] is None


def test_approved_keystone_dtype_text_cascades_string_concat():
    # A text keystone (declared dtype) cascades a string-concat dependent that would otherwise stub as
    # a number+text type mismatch under the default "number".
    calcs = [
        {"name": "Label", "formula": "LOOKUP(MAX([Order ID]),0)",
         "role": "measure", "internal_name": "Calculation_L"},
        {"name": "Full", "formula": '[Label] + "!"',
         "role": "measure", "internal_name": "Calculation_F"},
    ]
    approved = {"Label": {"dax": "SELECTEDVALUE('Orders'[Order ID])", "dtype": "text"}}
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                               calcs=calcs, dim_calcs=[], approved_calc_dax=approved)
    m = {r["measure"]: r for r in out["report"]["measures"]}
    assert m["Label"]["status"] == "assisted-approved"
    assert m["Full"]["status"] == "translated"
    assert "[Label]" in m["Full"]["dax"]

    # ... and WITHOUT the dtype hint (default number) the same dependent fails closed.
    out2 = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                                calcs=calcs, dim_calcs=[],
                                approved_calc_dax={"Label": "SELECTEDVALUE('Orders'[Order ID])"})
    m2 = {r["measure"]: r for r in out2["report"]["measures"]}
    assert m2["Full"]["status"] == "stub"


def test_approved_keystone_dependent_with_other_unsupported_still_stubs():
    # Fail-closed: seeding the keystone does NOT rescue a dependent that carries ANOTHER unsupported
    # construct (its own table calc). Only the pure keystone-blocked dependents cascade.
    calcs = [
        {"name": "Slope", "formula": "WINDOW_SUM(SUM([Sales]))",
         "role": "measure", "internal_name": "Calculation_11"},
        {"name": "Mixed", "formula": "[Slope] + WINDOW_MAX(SUM([Sales]))",
         "role": "measure", "internal_name": "Calculation_99"},
    ]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                               calcs=calcs, dim_calcs=[],
                               approved_calc_dax={"Slope": "CALCULATE(SUM('Orders'[Sales]))"})
    m = {r["measure"]: r for r in out["report"]["measures"]}
    assert m["Slope"]["status"] == "assisted-approved"
    assert m["Mixed"]["status"] == "stub"
    assert m["Mixed"]["dax"] is None


def test_approved_dtype_helper_normalises_vocabulary():
    # Unit: the dict-form "dtype" is folded onto the translator's canonical tokens
    # (number/text/bool/date); flat string form / absent / blank / unknown -> None (caller defaults).
    assert _approved_dtype({"dax": "x", "dtype": "boolean"}) == "bool"
    assert _approved_dtype({"dax": "x", "dtype": "BOOL"}) == "bool"
    assert _approved_dtype({"dax": "x", "dtype": "string"}) == "text"
    assert _approved_dtype({"dax": "x", "dtype": "Text"}) == "text"
    assert _approved_dtype({"dax": "x", "dtype": "integer"}) == "number"
    assert _approved_dtype({"dax": "x", "dtype": "datetime"}) == "date"
    assert _approved_dtype({"dax": "x", "dtype": "date"}) == "date"
    # unrecognised / absent / blank / flat-string -> None
    assert _approved_dtype({"dax": "x", "dtype": "widget"}) is None
    assert _approved_dtype({"dax": "x"}) is None
    assert _approved_dtype({"dax": "x", "dtype": "  "}) is None
    assert _approved_dtype("CALCULATE(SUM('Orders'[Sales]))") is None


# -- Stage-2 inline_calcs: a parameter-driven date-window boolean DIM calc is inlined into its
#    consuming COUNTD-IF MEASURE so the measure translates end-to-end through assemble_import_model.
#    A row-level column can't read a slicer selection, so the date-window calc STUBS on its own; but
#    the consuming measure CAN read the slicer, and the orchestrator threads the dim-calc body +
#    the date params' resolver into the measure entry point, which inlines the body into the filter.
_CASES_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Cases' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.0a1b2c'>
        <connection authentication='sqlserver' class='sqlserver' dbname='Cases'
                    server='myserver.database.windows.net' username='svc' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0a1b2c' name='Cases' table='[dbo].[Cases]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Case Id</remote-name><local-name>[Case Id]</local-name>
        <parent-name>[Cases]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Intake Created Date</remote-name><local-name>[Intake Created Date]</local-name>
        <parent-name>[Cases]</parent-name><local-type>date</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Cases]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


def _date_param_e2e(caption, internal, default):
    return {"caption": caption, "internal_name": internal, "datatype": "date", "domain": "range",
            "default": default, "format": None, "range": None, "members": [], "aliases": {}}


_CASES_DATE_PARAMS = [_date_param_e2e("Start Date", "[Parameter 5]", "#2020-01-01#"),
                      _date_param_e2e("End Date", "[Parameter 6]", "#2020-12-31#")]
_CASES_DIM_CALCS = [{"name": "Date Filter Case", "internal_name": "Calculation_dw",
                     "formula": "[Intake Created Date] >= [Parameters].[Start Date] AND "
                                "[Intake Created Date] <= [Parameters].[End Date]"}]
_CASES_CONSUMER = [{"name": "Open Cases in Range",
                    "formula": "COUNTD(IF [Date Filter Case] THEN [Case Id] END)"}]
_CASES_INLINED_DAX = (
    "COALESCE(CALCULATE(DISTINCTCOUNTNOBLANK('Cases'[Case_Id]), "
    "FILTER('Cases', ('Cases'[Intake_Created_Date] >= [Start Date Value] && "
    "'Cases'[Intake_Created_Date] <= [End Date Value]))), 0)")


def test_stage2_inline_date_window_measure_translates_end_to_end():
    # The headline: assemble_import_model threads the stubbed date-window dim calc into the COUNTD-IF
    # measure entry point, which inlines it + resolves the date params (Option D) to the value
    # measures, so the measure comes back translated with the exact faithful COALESCE/FILTER DAX.
    out = assemble_import_model(
        parse_tds(_CASES_TDS), model_name="Cases",
        calcs=_CASES_CONSUMER, dim_calcs=_CASES_DIM_CALCS, parameters=_CASES_DATE_PARAMS)
    row = {r["measure"]: r for r in out["report"]["measures"]}["Open Cases in Range"]
    assert row["status"] == "translated"
    assert row["reason"] == "ok"
    assert row["dax"] == _CASES_INLINED_DAX
    # and the exact measure line lands byte-identically in _Measures.tmdl
    meas = out["parts"]["definition/tables/_Measures.tmdl"]
    assert ("measure 'Open Cases in Range' = " + _CASES_INLINED_DAX) in meas
    # the date params modelled a disconnected value table + [<Param> Value] SELECTEDVALUE measure
    assert "definition/tables/Start Date.tmdl" in out["parts"]
    assert "definition/tables/End Date.tmdl" in out["parts"]


def test_stage2_inline_without_dim_calc_measure_stays_stub():
    # The forcing function: the SAME model + measure + date params but WITHOUT the date-window dim
    # calc gives the inliner no body to splice, so the consumer stays an honest fail-closed stub --
    # proving it is the Stage-2 inline (not some other path) that flips the measure to translated.
    out = assemble_import_model(
        parse_tds(_CASES_TDS), model_name="Cases",
        calcs=_CASES_CONSUMER, parameters=_CASES_DATE_PARAMS)
    row = {r["measure"]: r for r in out["report"]["measures"]}["Open Cases in Range"]
    assert row["status"] == "stub"
    assert row["dax"] is None
    assert "[Date Filter Case]" in row["reason"]
    # the faithful inlined DAX must NOT appear anywhere in the emitted measures
    assert _CASES_INLINED_DAX not in out["parts"]["definition/tables/_Measures.tmdl"]

