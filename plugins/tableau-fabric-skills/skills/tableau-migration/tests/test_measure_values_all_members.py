"""An UNFILTERED Measure Names level means EVERY measure, not "an Exclude we cannot read".

Tableau writes a bare ``<groupfilter function='level-members' level='[:Measure Names]'/>`` the
moment Measure Names lands on a shelf with nothing filtered out -- which is the most ordinary
Measure Values worksheet there is. The emitter classified it alongside ``except``, and the two are
OPPOSITES: ``except`` lists the REMOVED members, ``level-members`` means every member of the level.

The cost was total. The worksheet was refused as "an Exclude filter whose displayed set cannot be
derived", and when it was the workbook's only sheet the report came out with ZERO pages -- which
Power BI Desktop does not open empty, it crashes on. A trivial text table therefore did not migrate
at all, while ``powerbi-report-author validate`` reported 0 errors and the definition-of-done
reported PASS.

Two things are asserted: that the two filter shapes are now told apart, and that the member set
recovered for the unfiltered case is the RIGHT one -- Tableau records no explicit member list for
it, so the members come from the view's own ``<column-instance>`` declarations, minus every pill
the worksheet spends on another shelf or encoding.
"""
import json

from twb_to_pbir import emit_pbir, parse_twb

from test_twb_to_pbir import _INST, _query_state, _visual_parts, _workbook, _worksheet

# Tableau's "(All)" Measure Names filter: a childless level-members, no member list anywhere.
_MV_ALL_FILTER = ("<filter class='categorical' column='[federated.abc].[:Measure Names]'>"
                  "<groupfilter function='level-members' level='[:Measure Names]' />"
                  "</filter>")

# The Exclude shape, for contrast: the listed member is the REMOVED one.
_MV_EXCLUDE_FILTER = ("<filter class='categorical' column='[federated.abc].[:Measure Names]'>"
                      "<groupfilter function='except'>"
                      "<groupfilter function='level-members' level='[:Measure Names]' />"
                      "<groupfilter function='union' op='manual'>"
                      "<groupfilter function='member' member='[federated.abc].[sum:Profit:qk]' />"
                      "</groupfilter></groupfilter></filter>")

_TEXT_ENC = "<encodings><text column='[federated.abc].[Multiple Values]' /></encodings>"


def _mv_worksheet(name, filters, encodings=_TEXT_ENC, deps_extra=_INST):
    return _worksheet(name, "Automatic",
                      rows="[federated.abc].[none:Region:nk]",
                      cols="[federated.abc].[:Measure Names]",
                      deps_extra=deps_extra, encodings=encodings, filters=filters)


def _values(ir):
    state = _query_state(list(_visual_parts(emit_pbir(ir)).values())[0])
    return [p["queryRef"] for p in state["Values"]["projections"]]


def test_a_bare_level_members_enumerates_every_measure():
    ir = parse_twb(_workbook(_mv_worksheet("All Measures", _MV_ALL_FILTER)))
    assert ir["worksheets"][0]["visual_type"] == "matrix"
    # ordered as Tableau renders an unsorted Measure Names header: alphabetically BY CAPTION.
    # The declaration order it was recovered from is Tableau's internal id sort (cnt: < none: <
    # sum: < usr:), which would scatter the calcs to the end of the table.
    assert _values(ir) == ["Sum(Orders.Profit)", "Sum(Orders.Sales_Amount)"]


def test_an_except_filter_still_defers_because_it_lists_the_removed_members():
    # The behaviour the level-members fix must NOT weaken: an Exclude action's member list is the
    # removed set, so reading it as a keep-list would surface exactly the wrong measures.
    ir = parse_twb(_workbook(_mv_worksheet("Excluded", _MV_EXCLUDE_FILTER)))
    assert ir["worksheets"][0]["visual_type"] == "unsupported"
    assert any(x["scope"] == "worksheet" and "exclude" in x["reason"].lower()
               for x in ir["warnings"])
    assert _visual_parts(emit_pbir(ir)) == {}


def test_a_level_members_that_carries_member_children_is_not_an_all_members_filter():
    # Only a CHILDLESS level-members narrows nothing. One with member children is a narrowing
    # structure we cannot read as a keep-list, so it must keep deferring (fail-closed).
    narrowed = ("<filter class='categorical' column='[federated.abc].[:Measure Names]'>"
                "<groupfilter function='level-members' level='[:Measure Names]'>"
                "<groupfilter function='member' member='[federated.abc].[sum:Profit:qk]' />"
                "</groupfilter></filter>")
    ir = parse_twb(_workbook(_mv_worksheet("Narrowed", narrowed)))
    assert ir["worksheets"][0]["visual_type"] == "unsupported"


def test_a_measure_spent_on_another_encoding_is_not_a_displayed_column():
    # The member set is recovered from the view's declarations, so a measure parked on Tooltip must
    # not be mistaken for a Measure Values column -- an unfiltered crosstab would otherwise grow a
    # column the source never showed.
    enc = ("<encodings><text column='[federated.abc].[Multiple Values]' />"
           "<tooltip column='[federated.abc].[sum:Profit:qk]' /></encodings>")
    ir = parse_twb(_workbook(_mv_worksheet("Tooltip Measure", _MV_ALL_FILTER, encodings=enc)))
    assert _values(ir) == ["Sum(Orders.Sales_Amount)"]


def test_a_dimension_pill_is_never_a_measure_values_member():
    # Measure Values is a continuous-only container. The test is the pill's own `quantitative`
    # type, so the nominal dimension instances declared alongside the measures stay out.
    ir = parse_twb(_workbook(_mv_worksheet("All Measures", _MV_ALL_FILTER)))
    blob = json.dumps(_visual_parts(emit_pbir(ir)))
    assert "Measure Names" not in blob and "Multiple Values" not in blob
    assert _values(ir) == ["Sum(Orders.Profit)", "Sum(Orders.Sales_Amount)"]
