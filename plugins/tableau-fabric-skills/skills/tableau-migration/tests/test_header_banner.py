"""Dashboard title banner (Lever B) + workbook-derived brand theme (Lever A).

A Tableau dashboard's header is a full-width ``type='text'`` zone at the very top, filled with the
brand colour and holding the white dashboard title. These tests lock:

  * PARSE   -- ``_parse_dashboard`` captures that zone as ``db["title_banner"]`` (fill / text /
               text_color / geometry), and only the real header -- not a narrow tinted callout or a
               low annotation box -- is chosen.
  * BRAND   -- ``_derive_brand_color`` returns the banners' fill (the most frequent when they differ).
  * EMIT    -- ``emit_pbir`` rebuilds each banner as a schema-shaped PBIR ``textbox`` visual carrying
               the fill + title, and the bundled theme leads ``dataColors`` with the brand.
  * GUARD   -- a dashboard with NO banner leaves the theme byte-identical and emits no textbox
               (the additive / never-regress contract).

Every fixture is inline XML (offline, deterministic). One file-gated test additionally parses the
real Salesforce proof workbook when it is present on this machine, and skips otherwise.
"""
import json
import os
import zipfile

import pytest

import twb_to_pbir as R
from twb_to_pbir import SCHEMA_VISUAL, emit_pbir, parse_twb
from test_twb_to_pbir import _INST, _visual_parts, _workbook, _worksheet

_DEFAULT_THEME = {"name": "Tableau", "dataColors": list(R._TABLEAU_10 + R._TABLEAU_EXTRA)}


# -- inline fixture builders ---------------------------------------------------
def _banner_zone(text="Intake", fill="#ac145a", color="#ffffff",
                 x=0, y=0, w=100000, h=9245, zid="99"):
    run = (f"<run bold='true' fontcolor='{color}' fontsize='24'>{text}</run>"
           if text else "<run />")
    return (f"<zone type-v2='text' h='{h}' w='{w}' x='{x}' y='{y}' id='{zid}'>"
            f"<formatted-text>{run}</formatted-text>"
            "<zone-style><format attr='border-style' value='none' />"
            f"<format attr='background-color' value='{fill}' /></zone-style></zone>")


def _ws_zone(name="WsA", x=5000, y=15000, w=90000, h=40000, zid="2"):
    return f"<zone h='{h}' w='{w}' x='{x}' y='{y}' name='{name}' id='{zid}' />"


def _container(*inner):
    return "<zone h='100000' w='100000' x='0' y='0'>" + "".join(inner) + "</zone>"


def _dashboard(inner, name="Intake"):
    return f"<dashboard name='{name}'><zones>{inner}</zones></dashboard>"


def _ws(name="WsA"):
    return _worksheet(name, "Bar", "[federated.abc].[sum:Sales:qk]",
                      "[federated.abc].[none:Category:nk]", deps_extra=_INST)


def _ir(inner, ws_names=("WsA",), name="Intake"):
    ws = "".join(_ws(n) for n in ws_names)
    return parse_twb(_workbook(ws, _dashboard(inner, name)))


# -- PARSE ---------------------------------------------------------------------
def test_title_banner_parsed_from_top_full_width_filled_text_zone():
    db = _ir(_container(_ws_zone()) + _banner_zone())["dashboards"][0]
    b = db["title_banner"]
    assert b is not None
    assert b["fill"] == "#ac145a"
    assert b["text"] == "Intake"
    assert b["text_color"] == "#ffffff"
    assert (b["x"], b["y"], b["w"], b["h"]) == (0.0, 0.0, 100000.0, 9245.0)


def test_title_banner_text_color_defaults_white_when_run_has_no_font_color():
    # a filled banner whose run declares no fontcolor still rebuilds with a legible white title
    zone = ("<zone type-v2='text' h='9245' w='100000' x='0' y='0' id='99'>"
            "<formatted-text><run bold='true' fontsize='24'>Intake</run></formatted-text>"
            "<zone-style><format attr='background-color' value='#ac145a' /></zone-style></zone>")
    b = _ir(_container(_ws_zone()) + zone)["dashboards"][0]["title_banner"]
    assert b["text_color"] == "#ffffff"


def test_text_zone_without_fill_is_not_a_banner():
    # a plain (unfilled) text zone at the top carries no brand signal -> not a header band
    zone = ("<zone type-v2='text' h='9245' w='100000' x='0' y='0' id='99'>"
            "<formatted-text><run>Just a note</run></formatted-text></zone>")
    db = _ir(_container(_ws_zone()) + zone)["dashboards"][0]
    assert db["title_banner"] is None


def test_narrow_filled_text_zone_is_not_selected_as_banner():
    # a thin tinted separator / callout (~15% width) is not the full-width header
    narrow = _banner_zone(text="Note", fill="#123456", x=1000, y=1000, w=15000, h=6000, zid="55")
    db = _ir(_container(_ws_zone()) + narrow)["dashboards"][0]
    assert db["title_banner"] is None


def test_low_filled_text_zone_is_not_selected_as_banner():
    # a filled full-width box near the BOTTOM (a footer) is not the top header band
    low = _banner_zone(text="Footer", fill="#123456", x=0, y=90000, w=100000, h=6000, zid="55")
    db = _ir(_container(_ws_zone()) + low)["dashboards"][0]
    assert db["title_banner"] is None


def test_topmost_widest_banner_wins_when_several_qualify():
    # two filled top bands both qualify -> the topmost (then widest) is the deterministic header
    lower = _banner_zone(text="Sub", fill="#0000ff", x=0, y=6000, w=100000, h=5000, zid="98")
    top = _banner_zone(text="Intake", fill="#ac145a", x=0, y=0, w=100000, h=6000, zid="99")
    db = _ir(_container(_ws_zone()) + lower + top)["dashboards"][0]
    assert db["title_banner"]["text"] == "Intake"
    assert db["title_banner"]["fill"] == "#ac145a"


def test_title_banner_does_not_become_a_worksheet_zone():
    # the header text zone must never leak into the worksheet ``zones`` list (it is decoration)
    db = _ir(_container(_ws_zone()) + _banner_zone())["dashboards"][0]
    assert [z["worksheet"] for z in db["zones"]] == ["WsA"]


# -- BRAND ---------------------------------------------------------------------
def test_brand_color_derives_from_banner_fill():
    ir = _ir(_container(_ws_zone()) + _banner_zone())
    assert R._derive_brand_color(ir) == "#ac145a"


def test_brand_is_the_most_frequent_banner_fill_across_dashboards():
    d1 = _dashboard(_container(_ws_zone("WsA")) + _banner_zone(fill="#ac145a", zid="91"), "D1")
    d2 = _dashboard(_container(_ws_zone("WsB")) + _banner_zone(fill="#ac145a", zid="92"), "D2")
    d3 = _dashboard(_container(_ws_zone("WsC")) + _banner_zone(fill="#0000ff", zid="93"), "D3")
    ws = _ws("WsA") + _ws("WsB") + _ws("WsC")
    ir = parse_twb(_workbook(ws, d1 + d2 + d3))
    assert R._derive_brand_color(ir) == "#ac145a"     # 2x crimson beats 1x blue


def test_brand_is_none_when_no_dashboard_has_a_banner():
    ir = _ir(_container(_ws_zone()))
    assert R._derive_brand_color(ir) is None


# -- EMIT ----------------------------------------------------------------------
def test_emit_pbir_emits_one_banner_textbox_carrying_fill_and_title():
    parts = emit_pbir(_ir(_container(_ws_zone()) + _banner_zone()))
    banners = [v for v in _visual_parts(parts).values()
               if v["visual"]["visualType"] == "textbox"]
    assert len(banners) == 1
    b = banners[0]
    # same structural PBIR envelope the engine stamps (and validates) for every visual
    assert b["$schema"] == SCHEMA_VISUAL
    assert {"$schema", "name", "position", "visual"} <= set(b)
    assert {"x", "y", "width", "height", "tabOrder"} <= set(b["position"])
    # the full-width top strip — spans the real per-dashboard page width (§13 geometry)
    page = next(json.loads(v) for k, v in parts.items() if k.endswith("page.json"))
    assert b["position"]["x"] == 0 and b["position"]["y"] == 0
    assert b["position"]["width"] == page["width"]
    # the white bold title text
    run = b["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"][0]
    assert run["value"] == "Intake"
    assert run["textStyle"]["color"] == "#ffffff"
    assert run["textStyle"]["fontWeight"] == "bold"
    # the crimson fill, as a single-quoted hex literal on the container background
    fill = (b["visual"]["visualContainerObjects"]["background"][0]["properties"]
            ["color"]["solid"]["color"]["expr"]["Literal"]["Value"])
    assert fill == "'#ac145a'"


def test_emit_theme_leads_with_brand_when_a_banner_is_present():
    parts = emit_pbir(_ir(_container(_ws_zone()) + _banner_zone()))
    theme = json.loads(parts["StaticResources/RegisteredResources/" + R._TABLEAU_THEME_FILE])
    assert theme["dataColors"][0] == "#ac145a"
    assert theme["dataColors"][1:] == list(R._TABLEAU_10 + R._TABLEAU_EXTRA)


def test_banner_only_dashboard_still_emits_a_page():
    # a dashboard that is JUST a header band (no supported worksheet) still yields a page + banner
    parts = emit_pbir(parse_twb(_workbook(_ws("Unplaced"),
                                          _dashboard(_banner_zone(), "HeaderOnly"))))
    pages = [k for k in parts if k.endswith("page.json")]
    header_pages = [k for k in pages if "HeaderOnly" in k]
    assert header_pages, "the banner-only dashboard must still get a page"
    banners = [v for v in _visual_parts(parts).values()
               if v["visual"]["visualType"] == "textbox"]
    assert len(banners) == 1


# -- GUARD (never-regress) -----------------------------------------------------
def test_dashboard_without_banner_keeps_default_theme_and_emits_no_textbox():
    ir = _ir(_container(_ws_zone()))
    assert ir["dashboards"][0]["title_banner"] is None
    parts = emit_pbir(ir)
    theme = json.loads(parts["StaticResources/RegisteredResources/" + R._TABLEAU_THEME_FILE])
    assert theme == _DEFAULT_THEME                       # byte-identical to today
    assert not any(v["visual"]["visualType"] == "textbox"
                   for v in _visual_parts(parts).values())


# -- real proof workbook (file-gated; runs where present, skips elsewhere) ------
_SALESFORCE_TWBX = r"C:\tfsg\in\Salesforce_Nonprofit_Case_Management.twbx"


@pytest.mark.skipif(not os.path.exists(_SALESFORCE_TWBX),
                    reason="Salesforce proof workbook not present on this machine")
def test_salesforce_proof_workbook_banner_and_brand_end_to_end():
    z = zipfile.ZipFile(_SALESFORCE_TWBX)
    twb = [n for n in z.namelist() if n.lower().endswith(".twb")][0]
    ir = parse_twb(z.read(twb).decode("utf-8-sig"))
    # every dashboard carries a crimson banner with a non-empty title
    for db in ir["dashboards"]:
        b = db["title_banner"]
        assert b is not None and b["fill"] == "#ac145a" and b["text"].strip()
    assert R._derive_brand_color(ir) == "#ac145a"
    parts = emit_pbir(ir)
    theme = json.loads(parts["StaticResources/RegisteredResources/" + R._TABLEAU_THEME_FILE])
    assert theme["dataColors"][0] == "#ac145a"
    textboxes = [v for v in _visual_parts(parts).values()
                 if v["visual"]["visualType"] == "textbox"]
    # every dashboard still contributes exactly one rebuilt title banner...
    n_banners = sum(1 for db in ir["dashboards"] if db["title_banner"])
    assert n_banners == len(ir["dashboards"]) == 5
    # ...and §12 dashboard text objects (caption bars / instruction lines) now ALSO rebuild as their
    # own textboxes, so the total textbox count is additively banners + every captured text object.
    n_text_objects = sum(len(db["text_objects"]) for db in ir["dashboards"])
    assert len(textboxes) == n_banners + n_text_objects
    for b in textboxes:
        assert b["$schema"] == SCHEMA_VISUAL
        run = b["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"][0]
        assert run["value"].strip()
