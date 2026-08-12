"""Author-hidden dashboard zones (``hidden-by-user='true'``) must not be rebuilt.

Tableau records a dashboard object the author collapsed behind a show/hide toggle as
``hidden-by-user='true'`` on its ``<zone>``. Tableau renders NOTHING for such a zone when the
dashboard opens. Emitting it anyway does not merely add furniture -- it OCCLUDES:

  * a toggled help/guidelines panel is written AFTER the worksheets (see ``_image_z``), so it
    paints over the entire page -- observed covering a whole dashboard end to end;
  * per-airline background plates stack on top of the one that should be showing;
  * hidden parameter-control cards surface as stray slicers over the sidebar.

The rule implemented and locked here is: author-hidden CONTENT is skipped; a ``filter`` zone is
EXEMPT and keeps its long-standing behaviour. That exemption is deliberate and load-bearing -- a
collapsed filter BAND is still a usable control, it does not paint over anything, and Power BI has
no Tier-1 collapse equivalent, so the faithful rebuild surfaces it (flagged ``hidden``). The
distinction is occluding CONTENT versus a usable CONTROL.

Hiding is INHERITED: ``_findall_local`` is a flat walk, so a hidden layout container's children are
visited independently and must be pruned with it -- otherwise a container toggled off as a unit
leaks its contents onto the canvas.

Also locked: a worksheet whose only appearance is a hidden zone is still PLACED, so it never falls
through to the standalone-worksheet pass and gets its own visible report page (which would be the
exact opposite of the author's intent).
"""
import xml.etree.ElementTree as ET

import twb_to_pbir as R

_WS = {"WsA", "WsHidden"}


def _dash(zones_xml, name="D"):
    return ET.fromstring("<dashboard name='%s'><zones>%s</zones></dashboard>" % (name, zones_xml))


def _parsed(xml, ws=_WS, warnings=None):
    return R._parse_dashboard(_dash(xml), ws, warnings if warnings is not None else [])


def _ids(items):
    return {i.get("zone_id") for i in items}


# -- visible zones are untouched (no regression) --------------------------------
_VISIBLE = (
    "<zone id='root' type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
    "<zone id='zw' name='WsA' x='0' y='10000' w='50000' h='30000' />"
    "<zone id='zi' type-v2='bitmap' param='Image/logo.png' x='85000' y='0' w='10000' h='7000' />"
    "<zone id='zp' type-v2='paramctrl' param='[Parameters].[Parameter 1]'"
    " x='70000' y='20000' w='16000' h='9333' />"
    "</zone>"
)


def test_visible_zones_are_all_captured():
    p = _parsed(_VISIBLE)
    assert _ids(p["zones"]) == {"zw"}
    assert _ids(p["image_zones"]) == {"zi"}
    assert _ids(p["param_controls"]) == {"zp"}
    assert p["hidden_zones_skipped"] == []


def test_absent_attribute_never_hides():
    assert _parsed(_VISIBLE)["hidden_zones_skipped"] == []


# -- each hidden content kind is withheld ---------------------------------------
def test_hidden_bitmap_is_skipped():
    p = _parsed("<zone id='zi' type-v2='bitmap' param='Image/bg.png' hidden-by-user='true'"
                " x='0' y='0' w='10000' h='7000' />")
    assert p["image_zones"] == []
    assert _ids(p["hidden_zones_skipped"]) == {"zi"}


def test_hidden_paramctrl_is_still_surfaced():
    """A collapsed parameter control is a usable control -- deliberately NOT dropped.

    This asserted the opposite until it was caught by a reader's dashboard. The rule this skip
    draws is occluding CONTENT (skip) versus a usable CONTROL (keep), and the ``filter`` exemption
    right below spells that out; ``paramctrl`` had simply never been added to it, with no stated
    reason. A parameter control is small, interactive and cannot paint over anything, so it belongs
    on the same side of the line as a filter card.

    Not academic: on an ATTI/ATTR technician-hierarchy dashboard the hidden zone was ``Date
    Selection`` (Monthly / Weekly / Daily), the control driving the matrix column grain. Dropping it
    left the reader no way to change grain and the matrix fell back to raw daily dates. The
    ``paramctrl`` branch that captures these zones even promises they are "never silently dropped" --
    it just sat 74 lines after the skip that removed them.
    """
    p = _parsed("<zone id='zp' type-v2='paramctrl' param='[Parameters].[Parameter 1]'"
                " hidden-by-user='true' x='0' y='0' w='16000' h='9333' />")
    assert [c["param_id"] for c in p["param_controls"]] == ["Parameter 1"]
    assert _ids(p["hidden_zones_skipped"]) == set()


def test_hidden_worksheet_zone_is_skipped():
    p = _parsed("<zone id='zw' name='WsHidden' hidden-by-user='true'"
                " x='0' y='0' w='50000' h='30000' />")
    assert p["zones"] == []
    assert _ids(p["hidden_zones_skipped"]) == {"zw"}


def test_hidden_text_zone_is_skipped():
    p = _parsed("<zone id='zt' type-v2='text' hidden-by-user='true' x='0' y='0' w='30000' h='4000'>"
                "<formatted-text><run fontcolor='#ffffff'>Guide</run></formatted-text></zone>")
    assert p["text_objects"] == []
    assert p["title_banner"] is None
    assert _ids(p["hidden_zones_skipped"]) == {"zt"}


# -- the filter exemption -------------------------------------------------------
_HIDDEN_FILTER = ("<zone id='zf' type-v2='filter' name='WsA'"
                  " param='[federated.abc].[none:Region:nk]' hidden-by-user='true'"
                  " x='70000' y='10000' w='20000' h='8000' />")


def test_hidden_filter_is_still_surfaced():
    """A collapsed filter band is a usable control -- deliberately NOT dropped."""
    p = _parsed(_HIDDEN_FILTER)
    assert _ids(p["filter_zones"]) == {"zf"}
    assert p["filter_field_tokens"]


def test_hidden_filter_records_its_hidden_flag():
    p = _parsed(_HIDDEN_FILTER)
    assert p["filter_zones"][0]["hidden"] is True


def test_hidden_filter_is_not_counted_as_skipped():
    assert _parsed(_HIDDEN_FILTER)["hidden_zones_skipped"] == []


def test_visible_filter_flag_is_false():
    p = _parsed(_HIDDEN_FILTER.replace(" hidden-by-user='true'", ""))
    assert p["filter_zones"][0]["hidden"] is False


# -- inheritance: a hidden container prunes its subtree -------------------------
_HIDDEN_CONTAINER = (
    "<zone id='cont' type-v2='layout-flow' hidden-by-user='true' x='0' y='0' w='90000' h='90000'>"
    "<zone id='cw' name='WsHidden' x='0' y='0' w='40000' h='40000' />"
    "<zone id='ci' type-v2='bitmap' param='Image/guide.png' x='0' y='40000' w='40000' h='40000' />"
    "</zone>"
)


def test_hidden_container_prunes_unmarked_children():
    p = _parsed(_HIDDEN_CONTAINER)
    assert p["zones"] == []
    assert p["image_zones"] == []
    assert _ids(p["hidden_zones_skipped"]) == {"cont", "cw", "ci"}


def test_visible_container_children_survive():
    p = _parsed(_HIDDEN_CONTAINER.replace(" hidden-by-user='true'", ""))
    assert _ids(p["zones"]) == {"cw"}
    assert _ids(p["image_zones"]) == {"ci"}


def test_hidden_container_does_not_hide_a_sibling():
    p = _parsed(_HIDDEN_CONTAINER
                + "<zone id='out' name='WsA' x='90000' y='0' w='10000' h='10000' />")
    assert _ids(p["zones"]) == {"out"}


def test_filter_inside_hidden_container_is_still_exempt():
    p = _parsed("<zone id='cont' type-v2='layout-flow' hidden-by-user='true'"
                " x='0' y='0' w='90000' h='90000'>" + _HIDDEN_FILTER + "</zone>")
    assert _ids(p["filter_zones"]) == {"zf"}


# -- attribute value handling ---------------------------------------------------
def test_false_value_does_not_hide():
    p = _parsed("<zone id='zw' name='WsA' hidden-by-user='false'"
                " x='0' y='0' w='50000' h='30000' />")
    assert _ids(p["zones"]) == {"zw"}
    assert p["hidden_zones_skipped"] == []


def test_empty_value_does_not_hide():
    p = _parsed("<zone id='zw' name='WsA' hidden-by-user=''"
                " x='0' y='0' w='50000' h='30000' />")
    assert _ids(p["zones"]) == {"zw"}


def test_value_is_case_and_whitespace_tolerant():
    p = _parsed("<zone id='zw' name='WsA' hidden-by-user=' TRUE '"
                " x='0' y='0' w='50000' h='30000' />")
    assert p["zones"] == []


def test_arbitrary_truthy_string_does_not_hide():
    """Fail-closed on the SHOWN side: only Tableau's own spelling hides."""
    p = _parsed("<zone id='zw' name='WsA' hidden-by-user='yes'"
                " x='0' y='0' w='50000' h='30000' />")
    assert _ids(p["zones"]) == {"zw"}


# -- diagnostics ----------------------------------------------------------------
def test_skipped_record_carries_type_and_ref():
    p = _parsed("<zone id='zi' type-v2='bitmap' param='Image/bg.png' hidden-by-user='true'"
                " x='0' y='0' w='10000' h='7000' />")
    rec = p["hidden_zones_skipped"][0]
    assert rec["type"] == "bitmap" and rec["ref"] == "Image/bg.png"


def test_worksheet_record_defaults_type_to_worksheet():
    p = _parsed("<zone id='zw' name='WsHidden' hidden-by-user='true'"
                " x='0' y='0' w='50000' h='30000' />")
    rec = p["hidden_zones_skipped"][0]
    assert rec["type"] == "worksheet" and rec["ref"] == "WsHidden"


def test_warning_is_emitted_with_a_count():
    warns = []
    _parsed(_HIDDEN_CONTAINER, warnings=warns)
    hits = [w for w in warns if "author-hidden" in w["reason"]]
    assert len(hits) == 1
    assert "3 author-hidden zone(s)" in hits[0]["reason"]
    assert hits[0]["scope"] == "dashboard" and hits[0]["name"] == "D"


def test_no_warning_when_nothing_is_hidden():
    warns = []
    _parsed(_VISIBLE, warnings=warns)
    assert [w for w in warns if "author-hidden" in w["reason"]] == []


# -- a hidden worksheet must not become its own report page ---------------------
def _ir(dash_xml, ws_names):
    """Minimal IR whose dashboards come from the real parser."""
    return {
        "dashboards": [_parsed(dash_xml, set(ws_names))],
        "worksheets": [],
        "parameters": [],
    }


def test_hidden_worksheet_is_seeded_as_placed():
    """The emit loop seeds ``placed`` from ``hidden_zones_skipped`` so the sheet is not re-emitted
    as a standalone page. Locked structurally: the parser must expose the sheet NAME to seed with."""
    p = _parsed("<zone id='zw' name='WsHidden' hidden-by-user='true'"
                " x='0' y='0' w='50000' h='30000' />")
    refs = {z["ref"] for z in p["hidden_zones_skipped"]}
    assert "WsHidden" in refs


def test_hidden_zones_skipped_key_always_present():
    assert _parsed(_VISIBLE)["hidden_zones_skipped"] == []
    assert isinstance(_parsed(_HIDDEN_CONTAINER)["hidden_zones_skipped"], list)


# -- extent is unaffected (hidden zones still define the canvas frame) ----------
def test_hidden_zone_still_contributes_to_extent():
    """Scaling must not shift when a hidden zone is withheld -- the canvas frame is the author's."""
    xml = ("<zone id='zw' name='WsA' x='0' y='0' w='10000' h='10000' />"
           "<zone id='zh' type-v2='bitmap' param='Image/b.png' hidden-by-user='true'"
           " x='0' y='0' w='100000' h='90000' />")
    p = _parsed(xml)
    assert p["extent"]["w"] == 100000 and p["extent"]["h"] == 90000
