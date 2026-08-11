"""An ``azureMap`` has no ``dataPoint`` object, so a flat mark colour there is a HARD error.

The cartesian channel for "the author chose one colour for every mark" is
``dataPoint.defaultColor`` (plus ``dataPoint.transparency``). Applying it to a map is not merely
ineffective -- it fails PBIR validation outright:

    PBIR_FORMATTING_PROP_UNKNOWN: Unknown property "defaultColor" in formatting object
    "dataPoint" for azureMap

which is an ERROR, not a warning, so the whole report is rejected rather than losing one colour.
Measured on a Salesforce case-management workbook: 5 such errors across 3 maps took the run from
clean to failed. An azureMap draws its marks through LAYERS -- ``bubbleLayer`` / ``heatMapLayer`` /
``referenceLayer``, alongside ``mapControls`` -- and the bubble layer owns the fill, so the flat mark
colour rides ``bubbleLayer.fillColor``. Confirmed by re-validating a patched copy: errorCount 5 -> 0.

Transparency is dropped rather than guessed onto the layer. The map's own default opacity is a
defensible rendering; an invented property name is another hard error, and the whole point of this
fix is that an unknown property costs the entire report.
"""
import json

import twb_to_pbir as R


def _ws(color="#4E79A7", transparency=None):
    ws = {"name": "Map", "mark_color": color}
    if transparency:
        ws["mark_transparency"] = transparency
    return ws


def test_azure_map_uses_the_bubble_layer_not_datapoint():
    objs = R._constant_mark_color_objects(_ws(), "azureMap")
    assert objs is not None
    assert "bubbleLayer" in objs
    # dataPoint on an azureMap is PBIR_FORMATTING_PROP_UNKNOWN -- an error, not a lost colour
    assert "dataPoint" not in objs
    assert "#4E79A7" in json.dumps(objs)


def test_azure_map_never_emits_transparency():
    """Guessed onto the layer it would be a second unknown property, i.e. a second hard error."""
    objs = R._constant_mark_color_objects(_ws(transparency=40), "azureMap")
    assert "transparency" not in json.dumps(objs)


def test_a_cartesian_chart_still_uses_datapoint():
    """The fix must be surgical: every non-map visual keeps the channel it always had."""
    objs = R._constant_mark_color_objects(_ws(), "clusteredBarChart")
    assert "dataPoint" in objs
    assert "defaultColor" in json.dumps(objs["dataPoint"])
    assert "bubbleLayer" not in objs


def test_a_cartesian_chart_still_carries_transparency():
    objs = R._constant_mark_color_objects(_ws(transparency=40), "clusteredBarChart")
    assert "ransparency" in json.dumps(objs["dataPoint"])


def test_transparency_is_named_per_visual_type():
    """Power BI does not agree with itself, and the wrong name fails the WHOLE report.

    A bar/column fill is ``fillTransparency``; an area/line surface is ``transparency``. Emitting
    ``transparency`` on a clusteredColumnChart is ``PBIR_FORMATTING_PROP_UNKNOWN`` -- caught only by
    an end-to-end validate of a real corpus workbook (0085_time_series_style_palette), and
    pre-existing, so the unit suite had been green over it for a long time.
    """
    col = R._constant_mark_color_objects(_ws(transparency=40), "clusteredColumnChart")
    assert "fillTransparency" in json.dumps(col["dataPoint"])
    area = R._constant_mark_color_objects(_ws(transparency=40), "areaChart")
    body = json.dumps(area["dataPoint"])
    assert "transparency" in body and "fillTransparency" not in body


def test_a_visual_with_no_transparency_property_gets_none():
    """scatterChart exposes no dataPoint transparency; inventing one is a hard error."""
    objs = R._constant_mark_color_objects(_ws(transparency=40), "scatterChart")
    assert "defaultColor" in json.dumps(objs["dataPoint"])
    assert "ransparency" not in json.dumps(objs)


def test_a_visual_with_no_default_colour_emits_nothing():
    """treemap / waterfallChart have no dataPoint.defaultColor -- drop the colour, keep the report."""
    for vt in ("treemap", "waterfallChart"):
        assert R._constant_mark_color_objects(_ws(), vt) is None


def test_no_mark_colour_emits_nothing_anywhere():
    assert R._constant_mark_color_objects({"name": "Map"}, "azureMap") is None
    assert R._constant_mark_color_objects({"name": "Bar"}, "clusteredBarChart") is None
