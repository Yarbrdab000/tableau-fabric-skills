"""Tableau's AUTOMATIC continuous colour ramp is a curated constant -- pin it to its evidence.

When the author keeps the default palette, Tableau serialises the colour encoding
(``type='interpolated'``) but NO ``<color-palette>`` element, so the exact ramp is NOT recoverable
from the workbook XML. The corpus's own rule table classifies "Tableau's built-in default palette
endpoints" as tier2: *not in the XML; needs a curated constant table plus a confidence flag*.

These stops are MEASURED from Tableau's own rendered output. Two corpus workbooks serialise an
``interpolated`` encoding with no palette AND ship a reference render; pixel-sampling both gives one
coherent GREEN family (an earlier pair of generic ColorBrewer "Blues"/"RdBu" stand-ins had the right
DIRECTION but the wrong HUE):

* ``0063_remove_null_and_all`` -- SUM(Sales), positive-only, filled map: every mark GREEN, palest
  ``#dde4bc`` through mid ``#95cb7d``. No red anywhere.
* ``0064_waterfall_chart`` -- SUM(Profit), signed, bar chart: dark green ``#076229`` at the maximum
  (+38.4k), near-white around zero, red ``#cc1617`` at the minimum (-25.1k).

ONE palette explains both: red at the negative extreme, near-white at zero, green at the positive
extreme -- an all-positive measure never reaches the red arm, which is exactly the single-hue green
0063 shows.

These tests exist so the constants cannot be silently re-flavoured without revisiting that evidence,
and so the DIRECTION and the disclosure flag stay intact.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import twb_to_pbir as T  # noqa: E402


def _rgb(hex_str):
    h = hex_str.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _luma(hex_str):
    r, g, b = _rgb(hex_str)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _is_green(hex_str):
    r, g, b = _rgb(hex_str)
    return g > r and g > b


def _is_red(hex_str):
    r, g, b = _rgb(hex_str)
    return r > g and r > b


# -- the curated constants ------------------------------------------------------------------


def test_sequential_default_is_the_measured_green_arm():
    assert T._DEFAULT_SEQUENTIAL_COLORS == ("#dde4bc", "#076229")


def test_diverging_default_is_the_measured_red_white_green():
    assert T._DEFAULT_DIVERGING_COLORS == ("#cc1617", "#f7f7f7", "#076229")


# -- the properties that made them right ----------------------------------------------------


def test_both_defaults_are_the_same_green_family():
    """The sequential ramp is the diverging palette's white->green arm. If the two ever disagree,
    an all-positive measure and a signed one would render in different hues from the SAME Tableau
    default -- which is not what the reference renders show."""
    assert T._DEFAULT_SEQUENTIAL_COLORS[-1] == T._DEFAULT_DIVERGING_COLORS[-1]


def test_direction_is_low_light_to_high_dark():
    """Never in doubt, and independent of hue: the low end must be lighter than the high end. A
    flipped ramp reads as the exact inverse of the source."""
    lo, hi = T._DEFAULT_SEQUENTIAL_COLORS
    assert _luma(lo) > _luma(hi)


def test_diverging_ends_are_red_and_green_with_a_near_neutral_centre():
    low, mid, high = T._DEFAULT_DIVERGING_COLORS
    assert _is_red(low), low
    assert _is_green(high), high
    r, g, b = _rgb(mid)
    assert max(r, g, b) - min(r, g, b) < 16, f"centre {mid} should be near-neutral"
    assert _luma(mid) > _luma(low) and _luma(mid) > _luma(high), "the centre must be the lightest"


def test_no_default_stop_is_blue_dominant():
    """Guards the actual regression: the previous ColorBrewer stand-ins were blue-dominant
    (``#08519c`` / ``#0571b0``), which matched neither reference render."""
    for stop in tuple(T._DEFAULT_SEQUENTIAL_COLORS) + tuple(T._DEFAULT_DIVERGING_COLORS):
        r, g, b = _rgb(stop)
        assert not (b > r and b > g), f"{stop} is blue-dominant"


# -- the confidence flag must survive -------------------------------------------------------


def test_a_default_ramp_is_always_disclosed():
    """The constant is curated, not parsed, so every use MUST stay flagged for the emitter to warn
    on (warn-never-wrong). Silent use would present a guess as a faithful read."""
    spec = T._default_continuous_gradient({"field": "[ds].[sum:Sales:qk]"})
    assert spec["default_palette"] is True
    assert spec["colors"] == list(T._DEFAULT_SEQUENTIAL_COLORS)
    assert spec["palette_type"] == "ordered-sequential"


def test_an_unnamed_signed_domain_uses_the_diverging_default():
    spec = T._default_continuous_gradient(
        {"field": "[ds].[sum:Profit:qk]", "min": "-25088.27", "max": "38381.50"})
    assert spec["palette_type"] == "ordered-diverging"
    assert spec["colors"] == list(T._DEFAULT_DIVERGING_COLORS)
    assert spec["default_palette"] is True


def test_an_explicitly_named_palette_still_wins_over_the_default():
    """The curated constant is the FALLBACK. A palette Tableau named must never be overridden by
    it -- that would throw away a real, parsed author choice."""
    spec = T._default_continuous_gradient({"field": "[ds].[sum:X:qk]",
                                           "palette": "orange_blue_diverging_10_0"})
    assert spec["colors"] != list(T._DEFAULT_DIVERGING_COLORS)
    assert spec["colors"] != list(T._DEFAULT_SEQUENTIAL_COLORS)


def test_reverse_still_flips_the_default_ramp():
    fwd = T._default_continuous_gradient({"field": "[ds].[sum:Sales:qk]"})
    rev = T._default_continuous_gradient({"field": "[ds].[sum:Sales:qk]", "reverse": "true"})
    assert rev["colors"] == list(reversed(fwd["colors"]))
