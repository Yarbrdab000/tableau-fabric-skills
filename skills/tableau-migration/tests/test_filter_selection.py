"""Applied Tableau filter selections -> the PBIR slots that actually restrict data.

Two distinct slots are easy to conflate, and writing to the wrong one is silent:

* ``filterConfig.filters[]`` (root-level sibling of ``visual``). On a **chart** this IS the data
  filter. On a **slicer** it only constrains which members are OFFERED -- it never filters the
  page. A selection written here alone left the slicer reading "All" *and* every other visual
  unfiltered, so the report showed a number the source workbook never showed.
* ``visual.objects.general[].properties.filter.filter`` (doubly nested). This is what the slicer
  opens SELECTED on, and the only one whose selection propagates to the rest of the page.

The two are **alternatives, never partners**: pairing them offers a one-item list the reader
cannot change, which is strictly less faithful than Tableau (where the card shows every member
with some ticked).

The second half is a scoping rule. Tableau applies a worksheet filter whether or not a
quick-filter card is shown for it, so:

* a **surfaced** filter is the reader's to change  -> selection on the slicer, and it must NOT
  also be baked into the visuals (that would pin the numbers while the slicer appeared to move);
* an **unsurfaced** filter is a fixed property of the worksheet -> visual-level ``filterConfig``.

Rebuilding only the surfaced ones silently widened those visuals to the whole table.
"""
import json

import pytest

from twb_to_pbir import (
    _applied_filter_config_for,
    _applied_selection_is_bindable,
    _merge_filter_configs,
    _slicer_preselection_object,
    _surfaced_filter_keys,
    emit_pbir,
    parse_twb,
)

from test_twb_to_pbir import (  # noqa: F401  -- shared inline-XML fixture builders
    _INST,
    _slicer_filter_configs,
    _slicer_preselect_in,
    _slicer_preselections,
    _visual_parts,
    _workbook,
    _worksheet,
)

_REGION = "[federated.abc].[none:Region:nk]"
_CATEGORY = "[federated.abc].[none:Category:nk]"
_SALES = "[federated.abc].[sum:Sales:qk]"


def _member_filter(column, member, level=None):
    return ("<filter class='categorical' column='%s'><groupfilter function='member' "
            "level='%s' member='&quot;%s&quot;' /></filter>"
            % (column, level or column.split(".")[-1].strip("[]"), member))


def _card_zone(name, zid, x=5000, y=5000):
    return ("<zone h='40000' w='90000' x='%d' y='%d' name='%s' id='%s' />"
            % (x, y, name, zid))


def _dashboard(inner, name="D"):
    return ("<dashboard name='%s'><size maxheight='800' maxwidth='1200' />"
            "<zones><zone h='100000' w='100000' x='0' y='0'>%s</zone></zones></dashboard>"
            % (name, inner))


def _filter_card(column, zid="9"):
    return ("<zone h='8000' w='20000' x='5000' y='90000' id='%s' type-v2='filter' "
            "param='%s' />" % (zid, column))


# --------------------------------------------------------------------------------------------
# unit -- _slicer_preselection_object
# --------------------------------------------------------------------------------------------

def _field(**kw):
    f = dict(entity="Orders", property="Region", binding="column", datatype="string",
             caption="Region", selection={"mode": "include", "values": ["West"]})
    f.update(kw)
    return f


def _pre(field, model_table="Orders", field_map=None):
    """The inner filter object, unwrapped from its ``objects.general[]`` entry."""
    got = _slicer_preselection_object(field, model_table, field_map or {})
    return None if got is None else got["properties"]["filter"]["filter"]


def test_preselection_object_builds_an_in_condition_over_a_source_alias():
    obj = _pre(_field())
    assert obj["Version"] == 2
    assert obj["From"] == [{"Name": "f", "Entity": "Orders", "Type": 0}]
    cond = obj["Where"][0]["Condition"]["In"]
    # SourceRef inside a filter Where MUST be {"Source": alias}. Using {"Entity": ...} there is
    # not a silent no-op -- Power BI's SQExprValidationVisitor throws while rewriting the query
    # ("Cannot read properties of undefined (reading 'accept')") and the WHOLE report renders
    # blank. Projections use Entity; filters use Source. They are not interchangeable.
    assert cond["Expressions"] == [
        {"Column": {"Expression": {"SourceRef": {"Source": "f"}}, "Property": "Region"}}]
    assert cond["Values"] == [[{"Literal": {"Value": "'West'"}}]]


def test_preselection_object_carries_every_selected_member():
    obj = _pre(_field(selection={"mode": "include", "values": ["West", "East"]}))
    vals = [row[0]["Literal"]["Value"] for row in obj["Where"][0]["Condition"]["In"]["Values"]]
    assert vals == ["'West'", "'East'"]


def test_preselection_object_escapes_an_embedded_quote():
    obj = _pre(_field(selection={"mode": "include", "values": ["O'Brien"]}))
    assert (obj["Where"][0]["Condition"]["In"]["Values"]
            == [[{"Literal": {"Value": "'O''Brien'"}}]])


def test_preselection_object_declines_exclude_mode():
    # On screen Not(In(...)) is indistinguishable from no-selection, so a render can never tell a
    # working exclusion from a dropped one -- and every exclude in the two proof workbooks is a
    # '%null%' sentinel that reduces to nothing anyway. Declining keeps the existing behaviour.
    assert _slicer_preselection_object(
        _field(selection={"mode": "exclude", "values": ["West"]}), "Orders", {}) is None


def test_preselection_object_declines_a_non_column_binding():
    assert _slicer_preselection_object(
        _field(binding="measure"), "Orders", {}) is None


def test_preselection_object_declines_when_there_is_no_selection():
    assert _slicer_preselection_object(_field(selection=None), "Orders", {}) is None
    assert _slicer_preselection_object(
        _field(selection={"mode": "include", "values": []}), "Orders", {}) is None


# --------------------------------------------------------------------------------------------
# unit -- _merge_filter_configs
# --------------------------------------------------------------------------------------------

def _cont(name, prop="Keep"):
    return {"name": name, "field": {"Column": {"Property": prop}}}


def test_merge_filter_configs_concatenates_and_drops_none():
    got = _merge_filter_configs(None, {"filters": [_cont("a")]}, None,
                                {"filters": [_cont("b")]})
    assert [c["name"] for c in got["filters"]] == ["a", "b"]


def test_merge_filter_configs_is_none_when_everything_is_empty():
    assert _merge_filter_configs(None, {"filters": []}, None) is None


def test_merge_filter_configs_keeps_the_first_of_a_duplicated_name():
    # Two containers sharing a name would collide in the report; the first wins so a keep-flag
    # already applied by the flag path is never doubled by the worksheet-filter path.
    got = _merge_filter_configs({"filters": [_cont("dup", "First")]},
                                {"filters": [_cont("dup", "Second")]})
    assert len(got["filters"]) == 1
    assert got["filters"][0]["field"]["Column"]["Property"] == "First"


# --------------------------------------------------------------------------------------------
# seam -- _surfaced_filter_keys / _applied_filter_config_for
# --------------------------------------------------------------------------------------------

def _parse_one(ws_xml, dash):
    return parse_twb(_workbook(ws_xml, dash))


def test_surfaced_filter_keys_reports_the_resolved_model_column_not_the_raw_token():
    # Matching on (entity, property) rather than the token mirrors what _emit_dashboard_slicers
    # already does, so a field carded once but filtered under several per-sheet tokens still
    # counts as surfaced exactly once.
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West"))
    db = _parse_one(ws, _dashboard(_card_zone("W", "2") + _filter_card(_REGION)))
    keys = _surfaced_filter_keys(db["worksheets"], db["dashboards"][0])
    assert ("Orders", "Region") in keys


def test_surfaced_filter_keys_is_empty_when_the_dashboard_shows_no_card():
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West"))
    db = _parse_one(ws, _dashboard(_card_zone("W", "2")))
    assert _surfaced_filter_keys(db["worksheets"], db["dashboards"][0]) == set()


def test_applied_filter_config_binds_an_unsurfaced_selection():
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West"))
    db = _parse_one(ws, _dashboard(_card_zone("W", "2")))
    warn = []
    fc = _applied_filter_config_for(db["worksheets"][0], set(), "Orders", {}, warn)
    assert fc is not None
    cont = fc["filters"][0]
    assert cont["field"]["Column"]["Property"] == "Region"
    inx = cont["filter"]["Where"][0]["Condition"]["In"]
    assert [r[0]["Literal"]["Value"] for r in inx["Values"]] == ["'West'"]


def test_applied_filter_config_skips_a_filter_the_dashboard_surfaces():
    # THE scoping rule. Baking a surfaced filter into the visual would pin the numbers while the
    # slicer appeared to move -- worse than dropping it, because it looks interactive and is not.
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West"))
    db = _parse_one(ws, _dashboard(_card_zone("W", "2")))
    assert _applied_filter_config_for(
        db["worksheets"][0], {("Orders", "Region")}, "Orders", {}, []) is None


def test_applied_filter_config_is_none_when_no_filter_carries_a_selection():
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST)
    db = _parse_one(ws, _dashboard(_card_zone("W", "2")))
    assert _applied_filter_config_for(db["worksheets"][0], set(), "Orders", {}, []) is None


def test_applied_filter_config_emits_one_container_per_distinct_column():
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West") + _member_filter(_CATEGORY, "Tables"))
    db = _parse_one(ws, _dashboard(_card_zone("W", "2")))
    fc = _applied_filter_config_for(db["worksheets"][0], set(), "Orders", {}, [])
    assert sorted(c["field"]["Column"]["Property"] for c in fc["filters"]) == ["Category", "Region"]


def test_applied_filter_config_dedupes_two_filters_on_the_same_column():
    # A worksheet can carry more than one filter that resolves to the SAME model column -- the
    # per-sheet token differs (duplicated datasource, re-filtered field) even though the binding
    # does not. Emitting both produces two containers over one column: the second silently
    # overrides the first in Power BI, so the restriction actually applied is whichever happened
    # to be last. First wins, once.
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West") + _member_filter(_REGION, "East"))
    db = _parse_one(ws, _dashboard(_card_zone("W", "2")))
    fc = _applied_filter_config_for(db["worksheets"][0], set(), "Orders", {}, [])
    assert [c["field"]["Column"]["Property"] for c in fc["filters"]] == ["Region"]
    inx = fc["filters"][0]["filter"]["Where"][0]["Condition"]["In"]
    assert [r[0]["Literal"]["Value"] for r in inx["Values"]] == ["'West'"]


def test_applied_filter_containers_have_distinct_names_per_column():
    # _merge_filter_configs dedupes by container name, so two columns sharing a name would
    # collapse to one and quietly drop a restriction.
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West") + _member_filter(_CATEGORY, "Tables"))
    db = _parse_one(ws, _dashboard(_card_zone("W", "2")))
    fc = _applied_filter_config_for(db["worksheets"][0], set(), "Orders", {}, [])
    names = [c.get("name") for c in fc["filters"]]
    assert len(set(names)) == len(names) == 2


# --------------------------------------------------------------------------------------------
# type safety -- a mistyped literal on a CHART is an error tile, not a looser filter
# --------------------------------------------------------------------------------------------

def test_applied_selection_is_bindable_accepts_a_string_member():
    assert _applied_selection_is_bindable(
        {"datatype": "string", "selection": {"mode": "include", "values": ["West"]}})


@pytest.mark.parametrize("dt", ["boolean", "integer", "real", "date", "datetime", ""])
def test_applied_selection_is_bindable_declines_every_non_string_member(dt):
    # RENDER-CAUGHT REGRESSION. Tableau writes a boolean member as the STRING "true", so the
    # emitter happily produced Literal "'true'" against a rebuilt column that is int64 -- and one
    # of the two corpus specimens is a BLANK() stub no literal could ever match. On a slicer that
    # only mis-limits the offered members; on a chart Power BI rejects the visual outright with
    # "Something's wrong with one or more filters" and the card that read 2703 became an error
    # tile. Fail closed: no filter (the previously shipped behaviour) beats a broken visual.
    assert not _applied_selection_is_bindable(
        {"datatype": dt, "selection": {"mode": "include", "values": ["true"]}})


def test_applied_selection_is_bindable_allows_a_rebound_integer_date_part():
    # A date-part member rebound onto an INTEGER calendar column is literal-safe by construction:
    # the member value equals the DAX part-function output verbatim.
    assert _applied_selection_is_bindable(
        {"datatype": "integer", "date_rebound": True, "property": "Month",
         "selection": {"mode": "include", "values": ["4"]}})


def test_applied_selection_is_bindable_leaves_ranges_to_the_binder():
    # _slicer_filter_config types ranges itself (numeric ones bind, date ones warn), so this gate
    # must not pre-empt it.
    assert _applied_selection_is_bindable({"datatype": "integer", "range": {"min": 1, "max": 9}})


def test_boolean_worksheet_filter_is_not_applied_and_is_warned():
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "true"))
    db = _parse_one(ws, _dashboard(_card_zone("W", "2")))
    hit = 0
    for f in db["worksheets"][0].get("filters") or []:
        if f.get("selection"):
            f["datatype"] = "boolean"
            hit += 1
    assert hit == 1, "fixture must produce exactly one applied filter to retype"
    warn = []
    assert _applied_filter_config_for(db["worksheets"][0], set(), "Orders", {}, warn) is None
    assert any("not carried onto the visual" in (w.get("reason") or "") for w in warn)


def test_applied_filter_config_honours_a_partially_surfaced_worksheet():
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West") + _member_filter(_CATEGORY, "Tables"))
    db = _parse_one(ws, _dashboard(_card_zone("W", "2") + _filter_card(_REGION)))
    fc = _applied_filter_config_for(
        db["worksheets"][0], {("Orders", "Region")}, "Orders", {}, [])
    assert [c["field"]["Column"]["Property"] for c in fc["filters"]] == ["Category"]


# --------------------------------------------------------------------------------------------
# integration -- through emit_pbir, the shape that actually ships
# --------------------------------------------------------------------------------------------

def _emit(ws_xml, dash):
    return emit_pbir(parse_twb(_workbook(ws_xml, dash)))


def _applied_by_prop(parts):
    out = {}
    for v in _visual_parts(parts).values():
        if v["visual"]["visualType"] == "slicer":
            continue
        for c in ((v.get("filterConfig") or {}).get("filters") or []):
            fld = c.get("field") or {}
            if "Column" not in fld:
                continue
            inx = c["filter"]["Where"][0]["Condition"]["In"]
            out[fld["Column"]["Property"]] = [r[0]["Literal"]["Value"] for r in inx["Values"]]
    return out


def test_surfaced_selection_opens_the_slicer_on_it_and_leaves_the_visual_unfiltered():
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West"))
    parts = _emit(ws, _dashboard(_card_zone("W", "2") + _filter_card(_REGION)))
    inx = _slicer_preselect_in(parts)
    assert inx["Expressions"][0]["Column"]["Property"] == "Region"
    assert [r[0]["Literal"]["Value"] for r in inx["Values"]] == ["'West'"]
    # ...and NOT also on the chart: the reader owns this one.
    assert _applied_by_prop(parts) == {}


def test_a_preselected_slicer_carries_no_filter_config():
    # Render-proved: with filterConfig present the slicer offers ONLY the selected member and the
    # reader cannot widen it. Dropping it gives the faithful shape -- full member list, authored
    # member ticked -- and the page genuinely filters.
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West"))
    parts = _emit(ws, _dashboard(_card_zone("W", "2") + _filter_card(_REGION)))
    assert _slicer_preselections(parts)          # the selection is there...
    assert _slicer_filter_configs(parts) == []   # ...instead of, never alongside


def test_unsurfaced_selection_lands_on_the_visual_and_fabricates_no_slicer():
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West"))
    parts = _emit(ws, _dashboard(_card_zone("W", "2")))
    assert _slicer_preselections(parts) == []
    assert [v for v in _visual_parts(parts).values()
            if v["visual"]["visualType"] == "slicer"] == []
    assert _applied_by_prop(parts) == {"Region": ["'West'"]}


def test_an_unsurfaced_filter_scopes_only_to_the_worksheet_that_carries_it():
    # Tableau scopes a worksheet filter to its own sheet. Applying it page-wide would silently
    # narrow neighbours the source workbook never narrowed.
    ws1 = _worksheet("Filtered", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                     filters=_member_filter(_REGION, "West"))
    ws2 = _worksheet("Plain", "Bar", _SALES, _CATEGORY, deps_extra=_INST)
    parts = _emit(ws1 + ws2,
                  _dashboard(_card_zone("Filtered", "2") + _card_zone("Plain", "3", y=50000)))
    filtered = [v for v in _visual_parts(parts).values()
                if v["visual"]["visualType"] != "slicer" and v.get("filterConfig")]
    assert len(filtered) == 1


def test_exclude_selection_neither_preselects_nor_applies():
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=("<filter class='categorical' column='%s'>"
                             "<groupfilter function='except'><groupfilter function='member' "
                             "level='Region' member='&quot;West&quot;' /></groupfilter>"
                             "</filter>" % _REGION))
    parts = _emit(ws, _dashboard(_card_zone("W", "2") + _filter_card(_REGION)))
    assert _slicer_preselections(parts) == []


def test_no_selection_leaves_the_slicer_open_on_all():
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=("<filter class='categorical' column='%s'>"
                             "<groupfilter function='level-members' level='Region' />"
                             "</filter>" % _REGION))
    parts = _emit(ws, _dashboard(_card_zone("W", "2") + _filter_card(_REGION)))
    assert _slicer_preselections(parts) == []
    assert _applied_by_prop(parts) == {}


def test_applied_and_keep_flag_filters_compose_on_one_visual():
    # The worksheet-filter containers share filterConfig.filters[] with the keep-flag ones, so a
    # visual carrying both must end up with both -- neither path may overwrite the other.
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West") + _member_filter(_CATEGORY, "Tables"))
    parts = _emit(ws, _dashboard(_card_zone("W", "2")))
    assert _applied_by_prop(parts) == {"Region": ["'West'"], "Category": ["'Tables'"]}


def test_report_json_separates_applied_columns_from_measure_keep_flags():
    # ``flag_filters`` is documented as keep-flag MEASURES. Categorical column containers now
    # share the same array, so they are reported under their own additive key -- reading a Column
    # container as a Measure one raises KeyError and kills the whole emit.
    ws = _worksheet("W", "Bar", _SALES, _CATEGORY, deps_extra=_INST,
                    filters=_member_filter(_REGION, "West"))
    parts = _emit(ws, _dashboard(_card_zone("W", "2")))
    recs = [r for p in parts.values() if isinstance(p, dict)
            for r in (p.get("visuals") or []) if isinstance(r, dict)]
    blob = json.dumps(parts, default=str)
    assert "flag_filters" not in blob or "applied_filters" in blob
