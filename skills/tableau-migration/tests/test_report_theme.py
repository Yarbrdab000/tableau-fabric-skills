"""Report-level Tableau-palette custom theme.

Locks the ``report.json`` ``customTheme`` + ``RegisteredResources`` wiring (shape verified against
the ``report/1.0.0`` schema and a real Microsoft enhanced-format report) and the emitted theme file,
plus the additive contract: ``report_json_part()`` with no argument is byte-identical to the prior
baseTheme-only output, and the thin ``.pbip`` shell never gains a dangling customTheme.
"""
import json

import assemble_model as A
import twb_to_pbir as R
from test_twb_to_pbir import _INST, _workbook, _worksheet


def test_report_json_part_default_is_base_theme_only():
    # additive contract: no arg => byte-identical to the prior baseTheme-only report.json
    part = R.report_json_part()
    assert set(part["themeCollection"]) == {"baseTheme"}
    assert "resourcePackages" not in part


def test_report_json_part_with_custom_theme_wires_registered_resource():
    part = R.report_json_part(custom_theme_name="TableauPalette.json")
    ct = part["themeCollection"]["customTheme"]
    assert ct == {"name": "TableauPalette.json", "reportVersionAtImport": "5.61",
                  "type": "RegisteredResources"}
    # the base theme is retained (the loader requires it); customTheme layers on top
    assert part["themeCollection"]["baseTheme"]["name"] == "CY24SU10"
    pkg = part["resourcePackages"][0]
    assert pkg["name"] == "RegisteredResources" and pkg["type"] == "RegisteredResources"
    assert pkg["items"] == [{"name": "TableauPalette.json", "path": "TableauPalette.json",
                             "type": "CustomTheme"}]


def test_tableau_theme_dict_leads_with_tableau_10_in_order():
    t = R.tableau_theme_dict()
    assert t["name"] == "Tableau"
    # positions 1-10 are Tableau 10 EXACTLY -- Tableau's default automatic categorical assignment,
    # so a two-series chart rebuilds blue+orange (never blue+light-blue from a Tableau-20 interleave)
    assert t["dataColors"][:10] == [
        "#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
        "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#BAB0AC"]
    # 20 distinct colours; the extras (11-20) never collide with the first ten
    assert len(t["dataColors"]) == 20 == len(set(t["dataColors"]))


def test_tableau_theme_dict_brand_none_is_byte_identical_to_default():
    # additive contract (Lever A): brand=None (and no extra palette) => byte-identical to today's dict
    default = {"name": "Tableau", "dataColors": list(R._TABLEAU_10 + R._TABLEAU_EXTRA)}
    assert R.tableau_theme_dict() == default
    assert R.tableau_theme_dict(brand=None) == default
    assert R.tableau_theme_dict(brand=None, extra_palette=None) == default
    # a malformed brand is ignored (not a #rrggbb) -> still the default
    assert R.tableau_theme_dict(brand="crimson") == default


def test_tableau_theme_dict_brand_leads_dataColors_and_keeps_tableau_tail():
    # a workbook brand colour leads dataColors so auto-coloured charts rebuild in the brand, while
    # the full Tableau 10/20 sequence trails in EXACT order as the multi-category fallback
    t = R.tableau_theme_dict(brand="#ac145a")
    assert t["dataColors"][0] == "#ac145a"
    assert t["dataColors"][1:] == list(R._TABLEAU_10 + R._TABLEAU_EXTRA)
    assert len(t["dataColors"]) == 21
    # no duplicate colour (case-insensitive)
    assert len(t["dataColors"]) == len({c.lower() for c in t["dataColors"]})


def test_tableau_theme_dict_brand_matching_a_tableau_hex_is_deduplicated():
    # when the brand IS one of the Tableau hues it must lead exactly once, not appear twice
    t = R.tableau_theme_dict(brand="#4E79A7")            # == Tableau 10[0]
    assert t["dataColors"][0] == "#4E79A7"
    assert len(t["dataColors"]) == 20
    assert [c for c in t["dataColors"] if c.lower() == "#4e79a7"] == ["#4E79A7"]


def test_tableau_theme_dict_brand_dedup_is_case_insensitive():
    # a lower-cased brand still de-dups its upper-cased Tableau twin (derived hexes are lower-cased)
    t = R.tableau_theme_dict(brand="#4e79a7")
    assert t["dataColors"][0] == "#4e79a7"
    assert len(t["dataColors"]) == 20
    assert sum(c.lower() == "#4e79a7" for c in t["dataColors"]) == 1


def test_tableau_theme_dict_extra_palette_inserts_after_brand_before_tail():
    # reserved extra_palette (later per-member lever) slots after the brand, ahead of the Tableau
    # tail, likewise de-duplicated; unchanged when omitted (proven above)
    t = R.tableau_theme_dict(brand="#ac145a", extra_palette=["#00b2a9", "#4E79A7"])
    assert t["dataColors"][:3] == ["#ac145a", "#00b2a9", "#4E79A7"]
    # #4E79A7 pulled to the front is not repeated in the trailing Tableau sequence
    assert len(t["dataColors"]) == len({c.lower() for c in t["dataColors"]})
    assert t["dataColors"].count("#4E79A7") == 1


def test_emit_pbir_bundles_theme_file_and_references_it():
    ws = _worksheet("Profit by Region", "Bar",
                    rows="[federated.abc].[none:Region:nk]",
                    cols="[federated.abc].[sum:Profit:qk]",
                    deps_extra=_INST)
    parts = R.emit_pbir(R.parse_twb(_workbook(ws)))
    key = "StaticResources/RegisteredResources/" + R._TABLEAU_THEME_FILE
    assert key in parts, "the theme file must be bundled into the report parts"
    theme = json.loads(parts[key])
    assert theme["dataColors"][0] == "#4E79A7"
    report = json.loads(parts["definition/report.json"])
    # the referenced customTheme name matches the bundled file's basename (the path resolves)
    assert report["themeCollection"]["customTheme"]["name"] == R._TABLEAU_THEME_FILE
    item = report["resourcePackages"][0]["items"][0]
    assert item["path"] == R._TABLEAU_THEME_FILE and item["type"] == "CustomTheme"


def test_thin_shell_report_stays_base_theme_only():
    # the thin .pbip shell has no real visuals to recolour; it must NOT gain a customTheme that
    # references a theme file the shell never writes (that would be a broken report). Guard the leak.
    parts = A.build_thin_report_parts("Superstore")
    report = json.loads(parts["definition/report.json"])
    assert set(report["themeCollection"]) == {"baseTheme"}
    assert not any(k.startswith("StaticResources/") for k in parts)
