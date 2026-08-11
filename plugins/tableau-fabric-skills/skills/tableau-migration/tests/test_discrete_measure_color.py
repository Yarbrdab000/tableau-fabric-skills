"""A DISCRETE measure on Tableau's Colour shelf must not be rebuilt as a continuous gradient.

Tableau's idiom for "colour these marks two ways" is a boolean calc dropped on Colour
(``IF SUM([Profit]) > 0 THEN TRUE ELSE FALSE END``). It is a *discrete* pill -- Tableau paints two
swatches from it and offers no ramp -- but the emitter classified any unbound calc on Colour as
continuous and built a ``linearGradient3`` over it. Power BI then tries to evaluate ``MIN`` over a
boolean to find the ramp's endpoints, which it cannot do, and the visual dies at query time with
*"Error fetching data for this visual"*. Validation never sees it: the JSON is well-formed and the
failure is in the engine, which is exactly why it survived 29 corpus workbooks.

Three things are asserted here, and the third is the one that would otherwise ship silently.

1. **No gradient over a non-numeric driver.** The classifier reads the pill's own role code
   (``:nk`` discrete / ``:qk`` continuous, already parsed by ``_is_continuous_pill``) plus the calc's
   declared datatype, and a crash guard inside ``_chart_continuous_fill`` refuses the gradient
   unconditionally -- so a future caller cannot reintroduce the crash by bypassing the classifier.

2. **The guard is surgical.** A genuinely continuous numeric measure keeps the gradient it always
   had; that is a heat-map encoding used across the corpus and must be byte-unchanged.

3. **The ``dataViewWildcard`` selector is present.** This is load-bearing and invisible to every
   automated check we have. Power BI HONOURS a ``dataPoint.fill`` colour expression without it --
   validation passes, the visual renders, no warning is raised -- but it evaluates the expression in
   ONE context and paints every mark the same colour. Proven by render: the identical visual without
   the selector drew every bar one colour; with it, the loss-making sub-categories split out. A
   regression here yields a chart that looks plausible and is wrong, so it is asserted directly.

The replacement encoding is the idiomatic Power BI one, not merely a working one: a DAX measure that
RETURNS A COLOUR, applied as conditional formatting. Microsoft's own guidance is *"You can create a
DAX measure that returns color values based on your business logic"*, and because the twin lives in
the model it round-trips into Desktop's ``fx`` dialog where a user can read and edit it. A
column-on-Legend rebuild is deliberately NOT offered even as an opt-in: a row-level column changes
the mark grain (one bar becomes stacked segments), silently altering the numbers.
"""
import json

import twb_to_pbir as R


def _ws(color):
    return {"name": "Sheet 1", "encodings": {"color": color}}


def _color(datatype="boolean", discrete=True, caption="good/bad"):
    return {"kind": "value", "binding": "aggregation", "caption": caption,
            "property": caption, "datatype": datatype, "discrete_measure": discrete,
            "entity": "Orders", "aggregation": "Sum"}


def test_boolean_driver_is_not_gradient_safe():
    assert R._gradient_input_is_safe({"datatype": "integer"}) is True
    assert R._gradient_input_is_safe({"datatype": "real"}) is True
    assert R._gradient_input_is_safe({"datatype": "boolean"}) is False
    assert R._gradient_input_is_safe({"datatype": "string"}) is False


def test_a_discrete_pill_is_refused_even_when_its_datatype_looks_numeric():
    """Discreteness is decided by the pill's role code, not only by its declared datatype."""
    assert R._gradient_input_is_safe({"datatype": "integer", "discrete_measure": True}) is False


def test_an_unknown_datatype_still_reads_as_safe():
    """Fail-open on datatype alone, so an existing numeric heat map cannot be disturbed."""
    assert R._gradient_input_is_safe({"caption": "x"}) is True


def test_continuous_fill_refuses_a_boolean_driver():
    """The crash guard is unconditional: bypassing the classifier still cannot emit the gradient."""
    objects, _fact = R._chart_continuous_fill(_ws(_color()), {}, R.VT_BAR, "Orders", {}, [])
    assert objects is None


def test_discrete_measure_fill_uses_the_colour_twin_and_the_wildcard_selector():
    warnings = []
    objects, fact = R._chart_discrete_measure_fill(
        _ws(_color()), {}, R.VT_BAR, "Orders", {}, warnings)
    assert objects, "a discrete measure colour must produce a fill"
    blob = json.dumps(objects)
    # the model-side twin, not an inline rule -- so it is visible and editable in Desktop's fx dialog
    assert R._discrete_colour_measure_name(_color()) in blob
    # THE load-bearing bit: without it every mark is painted alike and nothing complains
    assert "dataViewWildcard" in blob
    assert fact["kind"] == "chart_discrete_measure_fill"
    # the rebuild is a real fidelity departure (no colour legend), so it must be reported
    assert warnings


def test_a_continuous_pill_is_left_to_the_gradient_path():
    """The discrete path must not capture the heat-map encoding it shares a call site with."""
    objects, fact = R._chart_discrete_measure_fill(
        _ws(_color(datatype="real", discrete=False)), {}, R.VT_BAR, "Orders", {}, [])
    assert objects is None and fact is None


def test_colour_twin_name_is_derived_not_looked_up():
    """Report and model derive the twin's name independently, so no handshake table can drift."""
    assert R._discrete_colour_measure_name(_color(caption="good/bad")) == \
        R.dax_safe_measure_name("good/bad" + R._COLOUR_MEASURE_SUFFIX)


def test_colour_twin_name_follows_the_model_rename_of_a_rebound_measure():
    """Keying on the Tableau caption would miss whenever the model renamed the measure."""
    c = _color()
    c.update(measure_rebound=True, property="good_bad")
    assert R._discrete_colour_measure_name(c) == \
        R.dax_safe_measure_name("good_bad" + R._COLOUR_MEASURE_SUFFIX)
