"""A dashboard filter CARD must resolve against every worksheet on the page, not just the drawn ones.

Tableau stores a dashboard ``<zone type-v2='filter'>`` as little more than a raw
``(datasource, field-instance)`` token plus geometry. The field it filters is only discoverable from
the *filter shelves* of the worksheets on that dashboard. The emitter used to build that token index
from the worksheets that had successfully produced a visual -- which quietly excluded the one shape
of worksheet most likely to own the cards.

A worksheet that exists PURELY to host filter controls -- no fields on any shelf, no mark, a name
like ``filters Main Summary`` -- is a standard Tableau dashboard-building idiom. It classifies as
``VT_UNSUPPORTED`` and is skipped before the render list is appended to, so every filter it owned
became unresolvable and its cards were dropped. The failure was silent and shape-selective: cards
for a field that *also* happened to be filtered on a drawn sheet still resolved, so a dashboard
would rebuild *some* of its filter band and lose the rest, with nothing in the report to say so.
Observed on a customer workbook: 3 authored controls, 2 rebuilt, no warning.

Two rules are locked here:

  * cards resolve against ALL worksheets the dashboard references (``filter_ws``), so a filter-host
    sheet contributes its filters even though it contributes no visual; and
  * a card that still resolves to nothing WARNS. A missing control is a missing interaction, and
    the reader has to be told which one -- silence was the second half of the defect.

Existing de-duplication is unchanged: one slicer per distinct model column, so a field carded on
both the host sheet and a drawn sheet yields a single control, not a stacked pair.
"""
import twb_to_pbir as R

_DS = "federated.abc"
_REGION = (_DS, "none:Region:nk")
_MONTH = (_DS, "none:FiscalMonth:ok")

MODEL_TABLE = "Data"
FIELD_MAP = {}


def _filter(caption, token, prop):
    return {"caption": caption, "filter_token": token,
            "entity": MODEL_TABLE, "property": prop, "binding": "Column",
            "datatype": "string"}


def _ws(name, visual_type, filters):
    return {"name": name, "visual_type": visual_type, "filters": filters}


def _card(token, zid):
    return {"token": token, "zone_id": zid, "mode": "checkdropdown",
            "x": 0, "y": 0, "w": 10000, "h": 5000}


# The filter-host sheet: empty shelves (so VT_UNSUPPORTED), owns both cards.
_HOST = _ws("filters Main Summary", R.VT_UNSUPPORTED,
            [_filter("Fiscal Month", _MONTH, "FiscalMonth"),
             _filter("Region", _REGION, "Region")])
# The drawn sheet: renders the table, repeats only the month filter.
_DRAWN = _ws("Main Summary", R.VT_MATRIX, [_filter("Fiscal Month", _MONTH, "FiscalMonth")])

_CARDS = [_card(_MONTH, "z1"), _card(_REGION, "z2")]


def _emit(ws_list, filter_ws, warnings):
    return R._emit_slicers(
        ws_list, "page-D", MODEL_TABLE, FIELD_MAP, warnings,
        shown_tokens=set(), filter_zones=_CARDS, ref_w=100000, ref_h=100000,
        filter_ws=filter_ws)


def _props(visuals):
    out = []
    for v in visuals:
        for p in (((v.get("visual") or {}).get("query") or {})
                  .get("queryState", {}).get("Values", {}).get("projections", [])):
            out.append(p["nativeQueryRef"].strip())
    return sorted(out)


def test_filter_host_worksheet_resolves_its_cards():
    """The regression: Region lives only on the empty host sheet and must still emit."""
    warnings = []
    visuals = _emit([_DRAWN], [_HOST, _DRAWN], warnings)
    assert _props(visuals) == ["FiscalMonth", "Region"]
    assert not [w for w in warnings if "resolved to no model field" in w.get("reason", "")]


def test_rendered_only_index_is_the_defect_being_fixed():
    """Guard the fix is load-bearing: resolving against the drawn sheet alone loses Region."""
    warnings = []
    visuals = _emit([_DRAWN], [_DRAWN], warnings)
    assert _props(visuals) == ["FiscalMonth"]


def test_field_carded_on_both_sheets_yields_one_slicer():
    """De-duplication survives the wider index -- no stacked duplicate control."""
    visuals = _emit([_DRAWN], [_HOST, _DRAWN], [])
    assert _props(visuals).count("FiscalMonth") == 1


def test_unresolvable_card_warns_instead_of_vanishing():
    warnings = []
    visuals = R._emit_slicers(
        [_DRAWN], "page-D", MODEL_TABLE, FIELD_MAP, warnings,
        shown_tokens=set(), filter_zones=[_card((_DS, "none:Ghost:nk"), "z9")],
        ref_w=100000, ref_h=100000, filter_ws=[_HOST, _DRAWN])
    assert visuals == []
    hits = [w for w in warnings if "resolved to no model field" in w.get("reason", "")]
    assert len(hits) == 1
    assert "none:Ghost:nk" in hits[0]["reason"]


def test_filter_ws_defaults_to_ws_list():
    """Callers that do not pass ``filter_ws`` keep the previous behaviour exactly."""
    a = _props(R._emit_slicers(
        [_HOST, _DRAWN], "page-D", MODEL_TABLE, FIELD_MAP, [],
        shown_tokens=set(), filter_zones=_CARDS, ref_w=100000, ref_h=100000))
    b = _props(_emit([_HOST, _DRAWN], None, []))
    assert a == b == ["FiscalMonth", "Region"]
