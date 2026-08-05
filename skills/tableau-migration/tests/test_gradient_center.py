"""A diverging gradient must never be pinned to a centre the workbook did not declare.

`_gradient_color_stops` only emits the mid stop's VALUE when it is known, precisely so Power BI can
centre the middle colour on the data midpoint when the author never pinned one. That intent was
being defeated upstream: `_default_continuous_gradient` fabricated a centre of ``0`` whenever the
encoding declared no domain, and the emitter then dutifully pinned the mid stop there.

A fabricated centre is not a cosmetic default. The mid stop is an absolute value on the measure's
scale, so when it falls OUTSIDE the data range the entire dataset sits on one side of it and only
HALF the ramp is ever reachable. Every all-positive measure hit this: a rank column over 1..13 with
an authored green -> gold -> red palette rendered gold-through-red, with the green end unreachable,
because 0 sits below the minimum. Confirmed by the fix a reader reached for by hand in Desktop --
tick "Add a middle color" and set Center to the middle of the range -- which restored the authored
ramp.

The rule is that the centre is emitted only when it is KNOWN, and knowledge comes from the
workbook:

  * the author pinned a ``center`` -> emit it (it stays pinned under filtering, as in Tableau);
  * the declared domain straddles zero -> a genuinely signed measure diverges about 0;
  * the domain is declared -> its midpoint is computable;
  * otherwise -> emit NO value, and Power BI recomputes the midpoint from the data currently in the
    visual.

That last case is the one that matters under interaction. Tableau recomputes a continuous colour
domain against the filtered data unless the author fixed the range, so an omitted centre tracks a
filter in Power BI exactly as the source does -- whereas any baked-in number (0, or the unfiltered
midpoint) would silently drift out of the range as soon as the reader filtered.
"""
import twb_to_pbir as R


def _enc(**kw):
    e = {"field": "[ds].[fld]", "type": "interpolated"}
    e.update(kw)
    return e


def _spec(**kw):
    return R._default_continuous_gradient(_enc(**kw))


# -- the defect: a centre invented from nothing ---------------------------------------------

def test_named_diverging_with_no_domain_leaves_the_centre_open():
    assert _spec(palette="red_green_gold_diverging_10_0")["center"] is None


def test_open_centre_emits_no_mid_value():
    stops = R._gradient_color_stops(_spec(palette="red_green_gold_diverging_10_0"))
    assert "value" not in stops["linearGradient3"]["mid"]


def test_open_centre_still_emits_all_three_colours():
    """The hue path is preserved -- only the breakpoint is left to Power BI."""
    g = R._gradient_color_stops(_spec(palette="red_green_gold_diverging_10_0"))["linearGradient3"]
    assert g["min"]["color"]["Literal"]["Value"] == "'%s'" % R._NAMED_HUE_STOPS["red"]
    assert g["mid"]["color"]["Literal"]["Value"] == "'%s'" % R._NAMED_HUE_STOPS["gold"]
    assert g["max"]["color"]["Literal"]["Value"] == "'%s'" % R._NAMED_HUE_STOPS["green"]


# -- the three cases where the centre IS known ----------------------------------------------

def test_authored_centre_is_honoured():
    assert _spec(palette="red_green_gold_diverging_10_0", center="4")["center"] == 4.0


def test_authored_centre_is_pinned_in_the_emitted_stops():
    stops = R._gradient_color_stops(_spec(palette="red_green_gold_diverging_10_0", center="4"))
    assert stops["linearGradient3"]["mid"]["value"]["Literal"]["Value"].startswith("4")


def test_domain_straddling_zero_centres_at_zero():
    assert _spec(palette="orange_blue_diverging_10_0", min="-5", max="5")["center"] == 0.0


def test_declared_domain_uses_its_midpoint():
    assert _spec(palette="red_green_gold_diverging_10_0", min="1", max="13")["center"] == 7.0


def test_all_positive_declared_domain_does_not_snap_to_zero():
    """The regression in miniature: 0 is outside 1..13 and would halve the ramp."""
    assert _spec(palette="red_green_gold_diverging_10_0", min="1", max="13")["center"] != 0.0


# -- a half-declared domain is not enough ----------------------------------------------------

def test_only_a_minimum_leaves_the_centre_open():
    assert _spec(palette="red_green_gold_diverging_10_0", min="1")["center"] is None


def test_only_a_maximum_leaves_the_centre_open():
    assert _spec(palette="red_green_gold_diverging_10_0", max="13")["center"] is None


# -- neighbours that must not move -----------------------------------------------------------

def test_sequential_palette_is_untouched():
    spec = _spec(palette="blue_10_0")
    assert spec["center"] is None
    assert spec["palette_type"] == "ordered-sequential"
    assert "linearGradient2" in R._gradient_color_stops(spec)


def test_tableau_automatic_ramp_keeps_its_zero_centre():
    """The automatic orange-blue default is a separate, render-verified path and is unchanged."""
    spec = R._automatic_color_gradient({"instance": "[ds].[fld]"})
    assert spec["center"] == 0.0
    stops = R._gradient_color_stops(spec)
    assert stops["linearGradient3"]["mid"]["value"]["Literal"]["Value"].startswith("0")
