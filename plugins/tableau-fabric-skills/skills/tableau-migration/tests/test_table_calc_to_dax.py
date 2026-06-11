"""Tests for the table-calc consumer (``TableCalcUsage`` -> faithful DAX | Tier-1 handoff).

The consumer is deliberately conservative: only an explicit Tableau ``Field`` scope ("Specific
Dimensions") whose addressing is *unambiguous* takes the deterministic path; everything else --
scope-relative tokens (``Pane`` / ``Rows`` / ``Columns`` / the compound ones), an order-sensitive
calc addressed by more than one dimension, a sort by an aggregate, a date-grain partition, a
secondary (stacked) calculation, Rank, and relative-bound moving windows -- hands off with its
recovered addressing facts intact. These tests pin both halves of that contract using synthetic
:class:`TableCalcUsage` records (the consumer is duck-typed) and a simple resolver.
"""
import pytest

from workbook_table_calcs import Pill, TableCalcUsage, extract_table_calc_usages
from table_calc_to_dax import (
    translate_table_calc_usage,
    translate_table_calc_usages,
    _intent_for,
)


# -- a minimal resolver over a Superstore-shaped model -------------------------
_MEASURES = {"Sales", "Profit"}
_DATES = {"Order Date"}


def resolver(caption):
    """``caption -> (table, column, tmdl_type)`` for the synthetic 'Orders' table."""
    col = caption.replace(" ", "_")
    if caption in _MEASURES:
        return ("Orders", col, "double")
    if caption in _DATES:
        return ("Orders", col, "dateTime")
    return ("Orders", col, "string")


def _pill(column, derivation="None"):
    return Pill(instance=f"{derivation}:{column}", column=column, derivation=derivation)


def _usage(**kw):
    """Build a TableCalcUsage with sensible defaults for the Sheet-8 calibration layout."""
    defaults = dict(
        worksheet="WS", instance="i", column="Profit", caption="Profit", kind="quick",
        calc_type="CumTotal", aggregation="Sum", ordering_type="Field",
        rows=[_pill("Category"), _pill("Sub-Category"), _pill("Segment")],
        cols=[_pill("Profit", "Sum")],
    )
    defaults.update(kw)
    return TableCalcUsage(**defaults)


# -- the faithful path ---------------------------------------------------------
def test_field_single_dim_cumtotal_translates_as_running_total():
    u = _usage(ordering_fields=["Category"])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "translated"
    assert t.translated_by == "deterministic (workbook addressing)"
    # checked dim addresses; the two unchecked row dims partition.
    assert t.partition_by == ("Sub-Category", "Segment")
    assert t.order_by == (("Category", "ASC"),)
    # running total = WINDOW from partition start (1, ABS) to current row (0, REL).
    assert "WINDOW(1, ABS, 0, REL" in t.dax
    assert "ORDERBY('Orders'[Category], ASC)" in t.dax
    assert "PARTITIONBY('Orders'[Sub-Category], 'Orders'[Segment])" in t.dax
    assert "CALCULATE(SUM('Orders'[Profit]))" in t.dax


def test_field_order_insensitive_window_translates_with_multiple_dims():
    # WINDOW_SUM over the full partition is order-independent, so >1 addressing dim is fine.
    u = _usage(
        kind="field", calc_type=None, column="Calc1", caption="Window Sum", derivation="User",
        formula="WINDOW_SUM(SUM([Sales]))", aggregation=None,
        ordering_fields=["Category", "Sub-Category"],
        cols=[_pill("Calc1", "User")],
    )
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "translated"
    assert t.partition_by == ("Segment",)               # the one unchecked dim
    assert t.order_by == (("Category", "ASC"), ("Sub-Category", "ASC"))
    assert "WINDOW(1, ABS, -1, ABS" in t.dax            # whole partition
    assert "CALCULATE(SUM('Orders'[Sales]))" in t.dax


# -- the handoff contract ------------------------------------------------------
def test_field_multi_dim_order_sensitive_hands_off():
    u = _usage(ordering_fields=["Segment", "Category"])  # CumTotal is order-sensitive
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "multiple dimensions" in t.reason
    assert t.handoff["ordering_fields"] == ["Segment", "Category"]
    assert t.handoff["intent"] == "running total (cumulative)"


def test_field_sort_by_aggregate_hands_off():
    u = _usage(
        kind="field", calc_type=None, column="Calc2", caption="Index", derivation="User",
        formula="INDEX()", aggregation=None,
        ordering_fields=["Sub-Category"], sort_field="Sales", sort_direction="DESC",
        rows=[_pill("Sub-Category")], cols=[_pill("Sales", "Sum")],
    )
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "aggregate/derived field" in t.reason


def test_field_date_grain_partition_hands_off():
    # partition would include a Year-derived date pill -> needs date-table modeling.
    u = _usage(
        ordering_fields=["Category"],
        rows=[_pill("Category")],
        cols=[_pill("Profit", "Sum"), _pill("Order Date", "Year")],
    )
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "date-grain dimension" in t.reason


@pytest.mark.parametrize("token", ["Pane", "Rows", "Columns", "ColumnInPane",
                                    "PaneCol", "CellInPane", "Cell", "Table"])
def test_scope_relative_tokens_hand_off(token):
    u = _usage(ordering_type=token, ordering_fields=[])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "scope-relative addressing" in t.reason
    assert t.handoff["ordering_type"] == token


def test_secondary_stacked_calc_hands_off():
    u = _usage(ordering_fields=["Category"], secondary=True)
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "secondary" in t.reason.lower()
    assert t.handoff["secondary"] is True


def test_rank_quick_calc_hands_off():
    u = _usage(calc_type="Rank", aggregation=None, rank_options="Unique,Descending",
               ordering_fields=["Sub-Category"])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "Rank" in t.reason


def test_moving_window_relative_bounds_hands_off():
    u = _usage(calc_type="WindowTotal", aggregation="Avg", window_from=-2, window_to=0,
               ordering_fields=["Category"])
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert "moving window" in t.reason
    assert t.intent == "moving window"


# -- shape / batch / intent ----------------------------------------------------
def test_translation_to_dict_roundtrips():
    u = _usage(ordering_fields=["Category"])
    d = translate_table_calc_usage(u, resolver).to_dict()
    assert d["status"] == "translated"
    assert d["partition_by"] == ["Sub-Category", "Segment"]
    assert d["order_by"] == [["Category", "ASC"]]
    assert d["handoff"] is None


def test_handoff_to_dict_carries_facts():
    u = _usage(ordering_type="Pane", ordering_fields=[])
    d = translate_table_calc_usage(u, resolver).to_dict()
    assert d["status"] == "handoff"
    assert d["dax"] is None
    assert d["handoff"]["shelf_rows"] == [
        ["Category", "None"], ["Sub-Category", "None"], ["Segment", "None"]]


def test_batch_translate_mixes_outcomes():
    translated = _usage(ordering_fields=["Category"])
    handed_off = _usage(ordering_type="Pane", ordering_fields=[])
    out = translate_table_calc_usages([translated, handed_off], resolver)
    assert [t.status for t in out] == ["translated", "handoff"]


def test_intent_labels():
    assert _intent_for(_usage()) == "running total (cumulative)"
    assert _intent_for(_usage(calc_type="WindowTotal", window_from=-2, window_to=0)) == "moving window"
    win = _usage(kind="field", calc_type=None, formula="WINDOW_AVG(SUM([Sales]))")
    assert _intent_for(win) == "window aggregate (partition or moving)"


# -- end-to-end: raw .twb XML -> extractor -> consumer -> DAX -------------------
# These guard the extractor<->consumer *seam*: the consumer's other tests build TableCalcUsage
# by hand, so nothing else proves the shape the extractor actually emits (e.g. bracket-free
# field ids, the Field-scope <order> list) is the shape the consumer consumes.
E2E_RUNNING_TOTAL_TWB = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <worksheets>
    <worksheet name='Running Total'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column aggregation='Sum' datatype='real' name='[Profit]' role='measure' type='quantitative' caption='Profit' />
            <column datatype='string' name='[Category]' role='dimension' type='nominal' caption='Category' />
            <column datatype='string' name='[Sub-Category]' role='dimension' type='nominal' caption='Sub-Category' />
            <column datatype='string' name='[Segment]' role='dimension' type='nominal' caption='Segment' />
            <column-instance column='[Profit]' derivation='Sum' name='[cum:sum:Profit:qk]' pivot='key' type='quantitative'>
              <table-calc aggregation='Sum' level-break='[ds0].[Category]' ordering-type='Field' type='CumTotal'>
                <order field='[ds0].[none:Category:nk]' />
              </table-calc>
            </column-instance>
            <column-instance column='[Category]' derivation='None' name='[none:Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[Sub-Category]' derivation='None' name='[none:Sub-Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[Segment]' derivation='None' name='[none:Segment:nk]' pivot='key' type='nominal' />
          </datasource-dependencies>
        </view>
        <rows>([ds0].[none:Category:nk] / ([ds0].[none:Sub-Category:nk] / [ds0].[none:Segment:nk]))</rows>
        <cols>[ds0].[cum:sum:Profit:qk]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>
"""


def test_end_to_end_xml_to_running_total_dax():
    [u] = extract_table_calc_usages(E2E_RUNNING_TOTAL_TWB)
    # the extractor emits a bare (bracket-free) column -- the exact contract the consumer assumes.
    assert u.column == "Profit"
    assert u.ordering_type == "Field"
    assert u.ordering_fields == ["Category"]

    t = translate_table_calc_usage(u, resolver)
    assert t.status == "translated"
    assert t.partition_by == ("Sub-Category", "Segment")
    assert t.order_by == (("Category", "ASC"),)
    assert t.dax == (
        "SUMX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Category], ASC), "
        "PARTITIONBY('Orders'[Sub-Category], 'Orders'[Segment])), "
        "CALCULATE(SUM('Orders'[Profit])))"
    )


E2E_SECONDARY_TWB = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <worksheets>
    <worksheet name='Stacked'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column aggregation='Sum' datatype='real' name='[Profit]' role='measure' type='quantitative' caption='Profit' />
            <column datatype='string' name='[Sub-Category]' role='dimension' type='nominal' caption='Sub-Category' />
            <column-instance column='[Profit]' derivation='Sum' name='[pcto:cum:sum:Profit:qk]' pivot='key' type='quantitative'>
              <table-calc aggregation='Sum' level-break='[ds0].[Sub-Category]' ordering-type='Field' type='CumTotal'>
                <order field='[ds0].[none:Sub-Category:nk]' />
              </table-calc>
              <table-calc level-address='[ds0].[none:Sub-Category:nk]' ordering-type='Field' type='PctTotal'>
                <order field='[ds0].[none:Sub-Category:nk]' />
              </table-calc>
            </column-instance>
            <column-instance column='[Sub-Category]' derivation='None' name='[none:Sub-Category:nk]' pivot='key' type='nominal' />
          </datasource-dependencies>
        </view>
        <rows>[ds0].[none:Sub-Category:nk]</rows>
        <cols>[ds0].[pcto:cum:sum:Profit:qk]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>
"""


def test_end_to_end_xml_secondary_calc_hands_off():
    [u] = extract_table_calc_usages(E2E_SECONDARY_TWB)
    assert u.secondary is True
    t = translate_table_calc_usage(u, resolver)
    assert t.status == "handoff"
    assert t.dax is None
    assert "secondary" in (t.reason or "")
    assert t.handoff["secondary"] is True


def test_synthesize_tolerates_bracketed_column():
    # a caller passing a *bracketed* field id must not double-wrap into "[[Profit]]" and degrade
    # to a misleading parser handoff -- it yields the same faithful DAX as the bare id.
    bare = translate_table_calc_usage(_usage(column="Profit", ordering_fields=["Category"]), resolver)
    bracketed = translate_table_calc_usage(_usage(column="[Profit]", ordering_fields=["Category"]), resolver)
    assert bracketed.status == "translated"
    assert bracketed.dax == bare.dax
