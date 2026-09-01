"""An implicit ``COUNT(*)`` column on a shelf becomes a real Power BI measure.

Tableau's object model gives every relation a hidden row identity. Dragging "Count of Orders" onto a
shelf emits ``COUNT([__tableau_internal_object_id__].[Orders_<32-hex>])`` -- a pill with no
calculated field and no formula. Two independent defects made every such column drop:

A. ``twb_to_pbir._classify_row_count`` asked a WORKSHEET-WIDE sweep which relations were counted and
   required the answer to be unique, so the ordinary Measure Values shape (Count of Orders + Count of
   People + Count of Returns side by side) reported three candidates for EVERY pill and bound none --
   even though each pill names its own relation in ``base_id``.

B. ``assemble_model`` synthesised a ``COUNTROWS`` measure only for a view-only QUICK TABLE CALC, so
   even a single unambiguous pill had nothing to bind to.

Either alone leaves the column blank, so the two are tested separately: a fix to one must not be
able to make the other's test pass.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import assemble_model as A  # noqa: E402
import twb_to_pbir as T  # noqa: E402
import workbook_table_calcs as W  # noqa: E402


OID = "__tableau_internal_object_id__"


def _oid_col(table, h="ECFCA1FB690A41FE803BC071773BA862"):
    return f"{OID}].[{table}_{h}"


def _instances(*tables):
    """The worksheet-dependency shape ``_classify_row_count`` reads."""
    return {("ds", f"cnt:{t}"): {"column": _oid_col(t), "derivation": "Count"} for t in tables}


# --------------------------------------------------------------------------------------
# A. the viz classifier reads the pill's OWN relation
# --------------------------------------------------------------------------------------

def test_a_pill_naming_its_own_relation_is_not_ambiguous_beside_its_siblings():
    """The regression: three count pills on one sheet, each naming a different relation."""
    inst = _instances("Orders", "People", "Returns")
    got = [T._classify_row_count("ds", f"cnt:{t}", _oid_col(t), "Count", {}, inst)
           for t in ("Orders", "People", "Returns")]
    assert [g["table"] for g in got] == ["Orders", "People", "Returns"]
    assert all(g["kind"] == "object_id" for g in got)


def test_a_the_candidate_list_still_reports_every_counted_relation():
    """``candidates`` is unchanged -- a consumer reading it sees the whole sweep, as before."""
    inst = _instances("Orders", "People", "Returns")
    got = T._classify_row_count("ds", "cnt:Orders", _oid_col("Orders"), "Count", {}, inst)
    assert got["candidates"] == ["Orders", "People", "Returns"]


def test_a_single_relation_worksheet_is_unchanged():
    inst = _instances("Orders")
    got = T._classify_row_count("ds", "cnt:Orders", _oid_col("Orders"), "Count", {}, inst)
    assert got == {"kind": "object_id", "table": "Orders", "candidates": ["Orders"]}


def test_a_a_pill_whose_relation_the_sheet_does_not_count_stays_ambiguous():
    """The sweep remains the gate: a pill naming a relation no count instance covers must NOT bind.

    Without this, ``own`` could name anything and the "is this a genuine count pill" check that
    keeps bare object-id filter artifacts on the silent-drop path would be bypassed.
    """
    inst = _instances("Orders", "People")
    got = T._classify_row_count("ds", "cnt:Shipping", _oid_col("Shipping"), "Count", {}, inst)
    assert got["table"] is None
    assert got["candidates"] == ["Orders", "People"]


def test_a_a_bare_object_id_artifact_with_no_count_instance_is_still_dropped_silently():
    got = T._classify_row_count("ds", "none:x", _oid_col("Orders"), "None", {}, {})
    assert got is None


def test_a_number_of_records_is_still_classified_numrec():
    got = T._classify_row_count("ds", T._NUMBER_OF_RECORDS, T._NUMBER_OF_RECORDS, "Sum", {}, {})
    assert got["kind"] == "numrec"


def test_a_the_caption_path_wins_over_the_stripped_relation_id():
    """``_oid_table`` prefers the object-id column's CAPTION -- a Union's friendly name.

    Asserted explicitly because the fix would still pass its other tests by falling through to the
    suffix strip, silently losing the renamed-relation case.
    """
    col = _oid_col("Union1")
    base_cols = {("ds", col): {"caption": "All Regions"}}
    inst = {("ds", "cnt:U"): {"column": col, "derivation": "Count"}}
    assert T._oid_table("ds", col, base_cols) == "All Regions"
    got = T._classify_row_count("ds", "cnt:U", col, "Count", base_cols, inst)
    assert got["table"] == "All Regions"


# --------------------------------------------------------------------------------------
# B. the model synthesises the measure the pill binds to
# --------------------------------------------------------------------------------------

def test_b_extractor_finds_every_counted_relation_with_its_caption():
    xml = """<workbook><worksheets><worksheet name='S'><table><view>
      <datasource-dependencies datasource='ds'>
        <column name='[{oid}].[Orders_ECFCA1FB690A41FE803BC071773BA862]' caption='Orders'/>
        <column-instance name='[cnt:a:qk]' column='[{oid}].[Orders_ECFCA1FB690A41FE803BC071773BA862]' derivation='Count'/>
        <column-instance name='[cnt:b:qk]' column='[{oid}].[People_D73023733B004CC1B3CB1ACF62F4A965]' derivation='Count'/>
      </datasource-dependencies></view></table></worksheet></worksheets></workbook>""".format(oid=OID)
    got = W.extract_implicit_count_tables(xml)
    assert [g["table"] for g in got] == ["Orders", "People"]
    assert got[0]["caption"] == "Orders"


def test_b_a_datasource_without_worksheets_extracts_nothing():
    """A bare ``.tds`` must stay byte-for-byte identical."""
    assert W.extract_implicit_count_tables("<workbook><datasource name='x'/></workbook>") == []


def test_b_a_non_count_object_id_instance_is_not_a_row_count():
    xml = """<workbook><worksheets><worksheet name='S'><table><view>
      <datasource-dependencies datasource='ds'>
        <column-instance name='[none:a:nk]' column='[{oid}].[Orders_ECFCA1FB690A41FE803BC071773BA862]' derivation='None'/>
      </datasource-dependencies></view></table></worksheet></worksheets></workbook>""".format(oid=OID)
    assert W.extract_implicit_count_tables(xml) == []


def test_b_synthesises_one_countrows_measure_per_counted_relation():
    rows = A._implicit_count_base_measures(
        [{"table": "Orders", "caption": "Orders"}, {"table": "People", "caption": None}],
        {"Orders", "People"}, [])
    assert [r["measure"] for r in rows] == ["Count Orders", "Count People"]
    assert rows[0]["dax"] == "COALESCE(COUNTROWS('Orders'), 0)"
    assert rows[0]["status"] == "translated"


def test_b_a_relation_that_is_not_a_model_table_is_dropped_never_guessed():
    rows = A._implicit_count_base_measures([{"table": "Ghost", "caption": None}], {"Orders"}, [])
    assert rows == []


def test_b_the_caption_is_preferred_over_the_relation_id():
    rows = A._implicit_count_base_measures(
        [{"table": "Union1", "caption": "All Regions"}], {"All Regions", "Union1"}, [])
    assert [r["measure"] for r in rows] == ["Count All Regions"]
    assert rows[0]["dax"] == "COALESCE(COUNTROWS('All Regions'), 0)"


def test_b_a_table_that_already_has_a_row_count_measure_is_skipped():
    existing = [{"measure": "My Count", "status": "translated", "dax": "COUNTROWS('Orders')"}]
    rows = A._implicit_count_base_measures([{"table": "Orders", "caption": None}], {"Orders"}, existing)
    assert rows == []


def test_b_an_existing_measure_owning_the_name_is_not_overwritten():
    existing = [{"measure": "Count Orders", "status": "translated", "dax": "1"}]
    rows = A._implicit_count_base_measures([{"table": "Orders", "caption": None}], {"Orders"}, existing)
    assert rows == []


def test_b_no_counted_relations_synthesises_nothing():
    assert A._implicit_count_base_measures([], {"Orders"}, []) == []
    assert A._implicit_count_base_measures(None, {"Orders"}, []) == []


def test_b_the_synthesised_measure_is_discoverable_as_a_row_count_target():
    """The whole point of the DAX shape: ``_row_count_targets`` must recognise it, because that is
    what becomes the viz layer's ``row_count_binding``."""
    rows = A._implicit_count_base_measures([{"table": "Orders", "caption": None}], {"Orders"}, [])
    targets = A._row_count_targets(rows)
    assert targets["measures"].get("Orders") == "Count Orders"


def test_b_a_relation_counted_twice_yields_one_measure():
    rows = A._implicit_count_base_measures(
        [{"table": "Orders", "caption": None}, {"table": "Orders", "caption": None}], {"Orders"}, [])
    assert len(rows) == 1


# --------------------------------------------------------------------------------------
# C. the count respects the visual's filters (Tableau counts RELATED rows)
# --------------------------------------------------------------------------------------
# Power BI propagates a relationship one -> many only, from ``to_col``'s table (the dimension) to
# ``from_col``'s (the fact), so a count of the DIMENSION never sees the fact's filters and returns
# the whole table. ``CROSSFILTER(..., BOTH)`` supplies the missing direction for that measure alone.

def _rel(ft, fc, tt, tc, active=True):
    return {"from_table": ft, "from_col": fc, "to_table": tt, "to_col": tc, "is_active": active}


STAR = [_rel("Orders", "Region", "People", "Region"),
        _rel("Orders", "Order_ID", "Returns", "Order_ID")]


def test_c_a_dimension_count_gets_the_missing_filter_direction():
    got = A._row_count_crossfilter("People", STAR)
    assert got == "CROSSFILTER('Orders'[Region], 'People'[Region], BOTH)"


def test_c_a_fact_count_is_left_alone():
    """Orders is never a ``to_table``, so its own row count already respects the filters -- which is
    why it matched Tableau before this existed. Wrapping it would be a change with no cause."""
    assert A._row_count_crossfilter("Orders", STAR) is None


def test_c_no_relationship_means_no_crossfilter():
    """``CROSSFILTER`` errors when the named columns are not part of a relationship -- and a count
    across no relationship cannot reproduce Tableau's related-row semantics under any behaviour."""
    assert A._row_count_crossfilter("Lonely", STAR) is None


def test_c_two_relationships_to_the_same_table_stay_plain():
    """The other documented error: "the arguments belong to different relationships"."""
    rels = STAR + [_rel("Shipments", "Region", "People", "Region")]
    assert A._row_count_crossfilter("People", rels) is None


def test_c_an_inactive_relationship_is_not_a_filter_path():
    rels = [_rel("Orders", "Region", "People", "Region", active=False)]
    assert A._row_count_crossfilter("People", rels) is None


def test_c_endpoints_are_read_off_the_matched_relationship_not_the_table_names():
    """A role-playing date puts TWO relationships between one pair of tables (``Order_Date`` active,
    ``Ship_Date`` inactive). Naming one relationship's column against the other's is an error even
    though the active path is unambiguous, so the columns must come off the matched relationship."""
    rels = [_rel("Orders", "Order_Date", "Date", "Date"),
            _rel("Orders", "Ship_Date", "Date", "Date", active=False)]
    got = A._row_count_crossfilter("Date", rels)
    assert got == "CROSSFILTER('Orders'[Order_Date], 'Date'[Date], BOTH)"
    assert "Ship_Date" not in got


def test_c_the_synthesised_dimension_measure_carries_the_clause():
    rows = A._implicit_count_base_measures(
        [{"table": "People", "caption": None}], {"People"}, [], relationships=STAR)
    assert rows[0]["dax"] == ("CALCULATE(COALESCE(COUNTROWS('People'), 0), "
                             "CROSSFILTER('Orders'[Region], 'People'[Region], BOTH))")


def test_c_without_relationships_the_measure_is_unchanged():
    """Byte-for-byte identical to the pre-CROSSFILTER output when nothing can be proven."""
    rows = A._implicit_count_base_measures([{"table": "People", "caption": None}], {"People"}, [])
    assert rows[0]["dax"] == "COALESCE(COUNTROWS('People'), 0)"


def test_c_the_wrapped_measure_is_still_recognised_as_a_row_count():
    """The binding depends on it. Without this the measure is synthesised, the model is valid, and
    the pill silently goes back to unbound with every other signal green."""
    rows = A._implicit_count_base_measures(
        [{"table": "People", "caption": None}], {"People"}, [], relationships=STAR)
    assert A._row_count_targets(rows)["measures"].get("People") == "Count People"


def test_c_a_calculate_carrying_a_value_filter_is_not_a_row_count():
    """``CROSSFILTER`` changes propagation and never removes rows, so it preserves the proof. A
    value filter does not, and must not be admitted by the widened recogniser."""
    assert A._row_count_table_of(
        "CALCULATE(COUNTROWS('People'), 'People'[Region] = \"West\")") is None
    assert A._row_count_table_of(
        "CALCULATE(COUNTROWS('People'), FILTER('People', 'People'[Region] = \"West\"), "
        "CROSSFILTER('Orders'[Region], 'People'[Region], BOTH))") is None


def test_c_the_bare_forms_are_still_recognised():
    assert A._row_count_table_of("COUNTROWS('Orders')") == "Orders"
    assert A._row_count_table_of("COALESCE(COUNTROWS('Orders'), 0)") == "Orders"
    assert A._row_count_table_of("SUM(Orders[Sales])") is None
