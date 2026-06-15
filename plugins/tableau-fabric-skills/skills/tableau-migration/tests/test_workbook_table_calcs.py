"""Tests for the ``.twb`` / ``.twbx`` table-calc addressing extractor.

The fixture below is a **hand-authored, minimal synthetic** Tableau workbook XML --
it reproduces only the public file-format structure the extractor reads (no real
workbook is committed). It exercises every branch:

* a CumTotal Quick Table Calc (ordering scope ``Pane``),
* a Rank Quick Table Calc (explicit ``ordering-field`` + ``rank-options``, scope ``Field``),
* a WindowTotal Quick Table Calc (relative ``from`` / ``to`` / ``window-options``),
* a user-defined ``WINDOW_SUM`` field whose per-instance ordering override (``Pane``)
  must win over the field definition's default (``Rows``),
* a user-defined ``INDEX()`` field with explicit ``<order>`` + ``<sort>`` (scope ``Field``),
* a plain worksheet with no table calc (must yield nothing).
"""
import json
import warnings
import zipfile

import pytest

from workbook_table_calcs import (
    Pill,
    TableCalcUsage,
    extract_from_file,
    extract_table_calc_usages,
    load_workbook_xml,
)


FIXTURE = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <worksheets>
    <worksheet name='Running'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column aggregation='Sum' datatype='real' name='[Sales]' role='measure' type='quantitative' caption='Sales' />
            <column datatype='date' name='[Order Date]' role='dimension' type='ordinal' />
            <column-instance column='[Sales]' derivation='Sum' name='[cum:sum:Sales:qk]' pivot='key' type='quantitative'>
              <table-calc aggregation='Sum' ordering-type='Pane' type='CumTotal' />
            </column-instance>
            <column-instance column='[Order Date]' derivation='Year' name='[yr:Order Date:ok]' pivot='key' type='ordinal' />
          </datasource-dependencies>
        </view>
        <rows>[ds0].[cum:sum:Sales:qk]</rows>
        <cols>[ds0].[yr:Order Date:ok]</cols>
      </table>
    </worksheet>
    <worksheet name='Ranking'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column aggregation='Sum' datatype='real' name='[Profit]' role='measure' type='quantitative' caption='Profit' />
            <column datatype='string' name='[Sub-Category]' role='dimension' type='nominal' />
            <column-instance column='[Profit]' derivation='Sum' name='[rank:sum:Profit:qk]' pivot='key' type='quantitative'>
              <table-calc ordering-field='[ds0].[none:Sub-Category:nk]' ordering-type='Field' rank-options='Unique,Descending' type='Rank' />
            </column-instance>
            <column-instance column='[Profit]' derivation='Sum' name='[sum:Profit:qk]' pivot='key' type='quantitative' />
            <column-instance column='[Sub-Category]' derivation='None' name='[none:Sub-Category:nk]' pivot='key' type='nominal' />
          </datasource-dependencies>
        </view>
        <rows>[ds0].[sum:Profit:qk]</rows>
        <cols>[ds0].[none:Sub-Category:nk]</cols>
      </table>
    </worksheet>
    <worksheet name='Moving'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column aggregation='Sum' datatype='real' name='[Sales]' role='measure' type='quantitative' caption='Sales' />
            <column datatype='date' name='[Order Date]' role='dimension' type='ordinal' />
            <column-instance column='[Sales]' derivation='Sum' name='[win:sum:Sales:qk]' pivot='key' type='quantitative'>
              <table-calc aggregation='Avg' from='-2' ordering-type='Rows' to='0' type='WindowTotal' window-options='IncludeCurrent' />
            </column-instance>
            <column-instance column='[Order Date]' derivation='Month-Trunc' name='[tmn:Order Date:qk]' pivot='key' type='ordinal' />
          </datasource-dependencies>
        </view>
        <rows>[ds0].[win:sum:Sales:qk]</rows>
        <cols>[ds0].[tmn:Order Date:qk]</cols>
      </table>
    </worksheet>
    <worksheet name='WindowUser'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column caption='Window Sum' datatype='real' name='[Calc1]' role='measure' type='quantitative'>
              <calculation class='tableau' formula='WINDOW_SUM(SUM([Sales]))'>
                <table-calc ordering-type='Rows' />
              </calculation>
            </column>
            <column datatype='string' name='[Sub-Category]' role='dimension' type='nominal' />
            <column-instance column='[Calc1]' derivation='User' name='[usr:Calc1:qk]' pivot='key' type='quantitative'>
              <table-calc ordering-type='Pane' />
            </column-instance>
            <column-instance column='[Sub-Category]' derivation='None' name='[none:Sub-Category:nk]' pivot='key' type='nominal' />
          </datasource-dependencies>
        </view>
        <rows>[ds0].[none:Sub-Category:nk]</rows>
        <cols>[ds0].[usr:Calc1:qk]</cols>
      </table>
    </worksheet>
    <worksheet name='IndexSorted'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column caption='Index' datatype='integer' name='[Calc2]' role='measure' type='quantitative'>
              <calculation class='tableau' formula='INDEX()'>
                <table-calc ordering-type='Rows' />
              </calculation>
            </column>
            <column aggregation='Sum' datatype='real' name='[Sales]' role='measure' type='quantitative' caption='Sales' />
            <column datatype='string' name='[Sub-Category]' role='dimension' type='nominal' />
            <column-instance column='[Calc2]' derivation='User' name='[usr:Calc2:qk]' pivot='key' type='quantitative'>
              <table-calc ordering-type='Field'>
                <order field='[ds0].[none:Sub-Category:nk]' />
                <sort direction='DESC' using='[ds0].[sum:Sales:qk]' />
              </table-calc>
            </column-instance>
            <column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' pivot='key' type='quantitative' />
            <column-instance column='[Sub-Category]' derivation='None' name='[none:Sub-Category:nk]' pivot='key' type='nominal' />
          </datasource-dependencies>
        </view>
        <rows>[ds0].[none:Sub-Category:nk]</rows>
        <cols>[ds0].[sum:Sales:qk]</cols>
      </table>
    </worksheet>
    <worksheet name='Plain'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column aggregation='Sum' datatype='real' name='[Sales]' role='measure' type='quantitative' caption='Sales' />
            <column datatype='string' name='[Sub-Category]' role='dimension' type='nominal' />
            <column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' pivot='key' type='quantitative' />
            <column-instance column='[Sub-Category]' derivation='None' name='[none:Sub-Category:nk]' pivot='key' type='nominal' />
          </datasource-dependencies>
        </view>
        <rows>[ds0].[sum:Sales:qk]</rows>
        <cols>[ds0].[none:Sub-Category:nk]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>
"""


@pytest.fixture(scope="module")
def usages():
    return extract_table_calc_usages(FIXTURE)


@pytest.fixture(scope="module")
def by_ws(usages):
    return {u.worksheet: u for u in usages}


def test_one_usage_per_table_calc_plain_sheet_excluded(usages, by_ws):
    # five table-calc usages; the plain worksheet contributes nothing.
    assert len(usages) == 5
    assert set(by_ws) == {"Running", "Ranking", "Moving", "WindowUser", "IndexSorted"}
    assert "Plain" not in by_ws


def test_cumtotal_quick_calc(by_ws):
    u = by_ws["Running"]
    assert u.kind == "quick"
    assert u.calc_type == "CumTotal"
    assert u.aggregation == "Sum"
    assert u.ordering_type == "Pane"
    assert u.column == "Sales"
    assert u.caption == "Sales"
    assert u.derivation == "Sum"
    assert u.formula is None
    assert u.shelf == "rows"
    # shelf layout recovered as typed pills
    assert [p.column for p in u.rows] == ["Sales"]
    assert [(p.column, p.derivation) for p in u.cols] == [("Order Date", "Year")]


def test_rank_quick_calc_with_ordering_field_and_options(by_ws):
    u = by_ws["Ranking"]
    assert u.kind == "quick"
    assert u.calc_type == "Rank"
    assert u.rank_options == "Unique,Descending"
    assert u.ordering_type == "Field"
    assert u.ordering_fields == ["Sub-Category"]
    assert u.aggregation is None
    # the rank pill sits on neither shelf
    assert u.shelf is None


def test_windowtotal_quick_calc_relative_bounds(by_ws):
    u = by_ws["Moving"]
    assert u.kind == "quick"
    assert u.calc_type == "WindowTotal"
    assert u.aggregation == "Avg"
    assert u.window_from == -2
    assert u.window_to == 0
    assert u.window_options == "IncludeCurrent"
    assert u.ordering_type == "Rows"
    assert u.shelf == "rows"
    assert [(p.column, p.derivation) for p in u.cols] == [("Order Date", "Month-Trunc")]


def test_user_window_field_instance_override_wins(by_ws):
    u = by_ws["WindowUser"]
    assert u.kind == "field"
    assert u.calc_type is None
    assert u.formula == "WINDOW_SUM(SUM([Sales]))"
    assert u.caption == "Window Sum"
    assert u.derivation == "User"
    # the per-instance scope (Pane) overrides the field definition default (Rows)
    assert u.ordering_type == "Pane"
    assert u.shelf == "cols"


def test_user_index_field_explicit_order_and_sort(by_ws):
    u = by_ws["IndexSorted"]
    assert u.kind == "field"
    assert u.calc_type is None
    assert u.formula == "INDEX()"
    assert u.caption == "Index"
    assert u.ordering_type == "Field"
    assert u.ordering_fields == ["Sub-Category"]
    assert u.sort_field == "Sales"
    assert u.sort_direction == "DESC"


def test_to_dict_is_json_serializable(usages):
    blob = json.dumps([u.to_dict() for u in usages])
    restored = json.loads(blob)
    assert len(restored) == 5
    running = next(r for r in restored if r["worksheet"] == "Running")
    assert running["rows"][0]["column"] == "Sales"
    assert running["cols"][0]["derivation"] == "Year"


def test_load_workbook_xml_reads_bom_twb_file(tmp_path):
    twb = tmp_path / "wb.twb"
    twb.write_text(FIXTURE, encoding="utf-8-sig")  # write a UTF-8 BOM
    text = load_workbook_xml(str(twb))
    assert text.lstrip().startswith("<?xml")  # BOM stripped
    assert len(extract_table_calc_usages(text)) == 5


def test_load_workbook_xml_reads_twbx_zip(tmp_path):
    twbx = tmp_path / "wb.twbx"
    with zipfile.ZipFile(twbx, "w") as z:
        z.writestr("wb.twb", FIXTURE)
        z.writestr("Data/extract.hyper", b"\x00\x01binary-extract")
    usages = extract_from_file(str(twbx))
    assert {u.worksheet for u in usages} == {
        "Running", "Ranking", "Moving", "WindowUser", "IndexSorted",
    }


def test_load_workbook_xml_raises_when_no_twb_member(tmp_path):
    twbx = tmp_path / "empty.twbx"
    with zipfile.ZipFile(twbx, "w") as z:
        z.writestr("Data/extract.hyper", b"\x00\x01")
    with pytest.raises(ValueError):
        load_workbook_xml(str(twbx))


def test_dataclass_to_dict_shapes():
    p = Pill(instance="i", column="c", derivation="Sum")
    assert p.to_dict() == {"instance": "i", "column": "c", "derivation": "Sum"}
    u = TableCalcUsage(worksheet="W", instance="i", column="c", caption="c", kind="quick")
    d = u.to_dict()
    assert d["rows"] == [] and d["cols"] == []
    assert d["ordering_type"] == "Table"  # default scope
    assert d["secondary"] is False        # no stacked secondary calc by default


def test_single_table_calc_is_not_flagged_secondary(usages):
    # every usage in the fixture carries exactly one <table-calc> -> no secondary pass.
    assert all(u.secondary is False for u in usages)


# A stacked "Add Secondary Calculation" leaves a second <table-calc> on the same pill. Kept in
# its own minimal XML so the shared FIXTURE counts above are undisturbed. This mirrors the
# VERIFIED encoding from a real "secondary calc example.twbx" (Running Total of Profit, then a
# secondary Percent of Total): the primary pass carries `level-break` + `aggregation`, the
# secondary pass carries `level-address` and no `aggregation`; both are `ordering-type='Field'`
# with their own `<order>` lists, and the primary keeps the `<sort>`.
SECONDARY_FIXTURE = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <worksheets>
    <worksheet name='Stacked'>
      <table>
        <view>
          <datasource-dependencies datasource='ds0'>
            <column aggregation='Sum' datatype='real' name='[Profit]' role='measure' type='quantitative' caption='Profit' />
            <column datatype='string' name='[Category]' role='dimension' type='nominal' />
            <column datatype='string' name='[Sub-Category]' role='dimension' type='nominal' />
            <column datatype='string' name='[Segment]' role='dimension' type='nominal' />
            <column-instance column='[Profit]' derivation='Sum' name='[pcto:cum:sum:Profit:qk:6]' pivot='key' type='quantitative'>
              <table-calc aggregation='Sum' level-break='[ds0].[Sub-Category]' ordering-type='Field' type='CumTotal'>
                <order field='[ds0].[none:Segment:nk]' />
                <order field='[ds0].[Sub-Category]' />
                <order field='[ds0].[Category]' />
                <sort direction='ASC' using='[ds0].[sum:Sales:qk]' />
              </table-calc>
              <table-calc level-address='[ds0].[none:Segment:nk]' ordering-type='Field' type='PctTotal'>
                <order field='[ds0].[none:Segment:nk]' />
                <order field='[ds0].[Category]' />
                <order field='[ds0].[Sub-Category]' />
              </table-calc>
            </column-instance>
            <column-instance column='[Category]' derivation='None' name='[none:Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[Sub-Category]' derivation='None' name='[none:Sub-Category:nk]' pivot='key' type='nominal' />
            <column-instance column='[Segment]' derivation='None' name='[none:Segment:nk]' pivot='key' type='nominal' />
          </datasource-dependencies>
        </view>
        <rows>([ds0].[none:Category:nk] / ([ds0].[none:Sub-Category:nk] / [ds0].[none:Segment:nk]))</rows>
        <cols>[ds0].[pcto:cum:sum:Profit:qk:6]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>
"""


def test_stacked_secondary_calc_is_detected():
    [u] = extract_table_calc_usages(SECONDARY_FIXTURE)
    assert u.secondary is True
    # the primary pass is still read normally (the consumer uses .secondary to hand off).
    assert u.calc_type == "CumTotal"
    assert u.ordering_type == "Field"
    assert u.to_dict()["secondary"] is True


# A worksheet whose ``<table>`` holds a present-but-EMPTY ``<view>`` -- plus a stray
# table-calc instance placed OUTSIDE the view (a direct child of ``<table>``). An empty
# ElementTree element is falsy, so the former ``view = _first(table, "view") or table``
# fell through to the parent ``table`` and would (a) raise a DeprecationWarning on the
# truthiness test and (b) wrongly scan the stray out-of-view instance as a real usage.
# The fix selects the empty view, so extraction yields nothing and never warns.
EMPTY_VIEW_FIXTURE = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <worksheets>
    <worksheet name='EmptyView'>
      <table>
        <view></view>
        <datasource-dependencies datasource='ds0'>
          <column aggregation='Sum' caption='Sales' datatype='real' name='[Sales]' role='measure' type='quantitative' />
          <column-instance column='[Sales]' derivation='Sum' name='[cum:sum:Sales:qk]' pivot='key' type='quantitative'>
            <table-calc aggregation='Sum' ordering-type='Pane' type='CumTotal' />
          </column-instance>
        </datasource-dependencies>
        <rows>[ds0].[cum:sum:Sales:qk]</rows>
        <cols>[ds0].[none:Category:nk]</cols>
      </table>
    </worksheet>
  </worksheets>
</workbook>
"""


def test_empty_view_uses_view_not_parent_table_and_does_not_warn():
    # Escalate warnings to errors so the old falsy-element DeprecationWarning would fail here.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        usages = extract_table_calc_usages(EMPTY_VIEW_FIXTURE)
    # The view is empty, so nothing is addressable; the stray out-of-view instance must NOT
    # be picked up (the buggy ``or table`` fallback would have surfaced it).
    assert usages == []
