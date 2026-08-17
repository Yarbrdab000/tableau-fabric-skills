"""A map's basemap is a per-worksheet property, so a module-level constant cannot be right.

Reported in #128. `_AZURE_MAP_DEFAULT_STYLE = "blank_accessible"` was applied to EVERY emitted
`azureMap` regardless of what the source draws, so a Tableau satellite or dark basemap rebuilt as
marks floating on white. The structural argument is the one that settles it: **one workbook can
contain satellite, dark and light basemaps at once**, so no single constant can serve them — the
style has to come from the worksheet.

The reporter's evidence is Tableau's own embedded `<thumbnail>` PNGs, which are Tableau's render
rather than anyone's interpretation. Confirmed independently here on `0063_remove_null_and_all`
(a corpus workbook): its `Solution 02` thumbnail shows a **light grey basemap with Canada, Mexico
and country labels** under a green choropleth — which is exactly what `grayscale_light` draws, and
exactly what the old code comment rejected `grayscale_light` *for* ("drew a grey basemap with
Canada/Mexico"). The rejection criterion was inverted: the style was refused for reproducing the
reference faithfully.

Render-verified after the change: that workbook's map now draws the basemap, grey Canada/Mexico,
water and state labels, instead of polygons on white.

The mapping keys are harvested from real workbooks, not guessed — across the corpora on this
machine: `light` x20, `tableau-light-gray` x7, `satellite` x1, plus two custom `mapbox://` styles.
The values are checked against the live enum reported by
`powerbi-report-author formatting describe-object azureMap mapControls`.

**Deliberately NOT changed: the no-signal default.** A worksheet that declares no `map-style` has
not told us it wants a blank basemap — it means the author never moved off Tableau's default.
Changing that would alter every map this engine has emitted, and `blank_accessible` is the one value
that was actually compared against a Tableau reference in Desktop. That case is left for a
render-verified change of its own rather than folded in on inference.
"""
import pytest

import twb_to_pbir as T


def _ws(raw_style, name="Map"):
    return {"name": name, "map_style_raw": raw_style}


@pytest.mark.parametrize("raw,expected", [
    ("satellite", "satellite"),
    ("tableau-light-gray", "grayscale_light"),
    ("light", "grayscale_light"),
    ("tableau-z-black", "night"),
    ("dark", "grayscale_dark"),
    ("normal", "road"),
    ("streets", "road"),
    ("outdoors", "road_shaded_relief"),
])
def test_a_declared_basemap_maps_to_its_power_bi_equivalent(raw, expected):
    assert T._tableau_map_style(_ws(raw)) == expected


def test_the_match_is_case_insensitive():
    assert T._tableau_map_style(_ws("SATELLITE")) == "satellite"
    assert T._tableau_map_style(_ws("  Tableau-Light-Gray  ")) == "grayscale_light"


def test_every_mapped_value_is_a_real_power_bi_style():
    """The enum reported by `formatting describe-object azureMap mapControls`.

    A typo here emits a style Power BI does not know, which is invisible to PBIR validation and
    shows up only as a map that will not draw.
    """
    valid = {"road", "satellite", "satellite_road_labels", "grayscale_dark", "night",
             "grayscale_light", "road_shaded_relief", "blank", "blank_accessible",
             "high_contrast_dark", "high_contrast_light"}
    assert set(T._TABLEAU_MAP_STYLE_TO_AZURE.values()) <= valid
    assert T._AZURE_MAP_DEFAULT_STYLE in valid


def test_the_no_signal_default_is_untouched():
    """Never-regress: absence of a map-style is NOT evidence of a blank basemap."""
    assert T._tableau_map_style(_ws(None)) is None
    assert T._tableau_map_style(_ws("")) is None
    assert T._tableau_map_style({}) is None
    assert T._tableau_map_style(None) is None


def test_a_custom_mapbox_style_is_refused_rather_than_approximated():
    """An arbitrary third-party design; a near-miss would silently misrepresent it."""
    raw = "mapbox://styles/vizwiz/cj5fuyjrg2nw12rmijbxgmcf7"
    assert T._tableau_map_style(_ws(raw)) is None
    assert T._tableau_map_style_raw(_ws(raw)) == raw


def test_an_unknown_token_fails_closed():
    """A Tableau version that spells a style differently keeps today's behaviour, never a guess."""
    assert T._tableau_map_style(_ws("tableau-some-future-style")) is None


def test_the_emitted_map_carries_the_derived_style():
    """End to end through the objects builder, which is what actually ships."""
    objs = T._azure_map_objects(_ws("satellite"), T.VT_MAP)
    literal = objs["mapControls"][0]["properties"]["defaultStyle"]["expr"]["Literal"]["Value"]
    assert literal == "'satellite'"


def test_a_map_with_no_signal_still_emits_the_default():
    objs = T._azure_map_objects(_ws(None), T.VT_MAP)
    literal = objs["mapControls"][0]["properties"]["defaultStyle"]["expr"]["Literal"]["Value"]
    assert literal == "'%s'" % T._AZURE_MAP_DEFAULT_STYLE


def test_two_worksheets_in_one_workbook_can_differ():
    """The structural point: one constant cannot serve a workbook that mixes basemaps."""
    sat = T._azure_map_objects(_ws("satellite", "Mapbox"), T.VT_MAP)
    dark = T._azure_map_objects(_ws("tableau-z-black", "Dark Map"), T.VT_MAP)

    def style(o):
        return o["mapControls"][0]["properties"]["defaultStyle"]["expr"]["Literal"]["Value"]

    assert style(sat) != style(dark)
    assert style(sat) == "'satellite'" and style(dark) == "'night'"
