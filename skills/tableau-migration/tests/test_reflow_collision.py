"""The shown-state reflow must fire on a REAL collision and only on a real one.

`_reflow_worksheets_below_slicers` reproduces what Tableau does when you click "Show Filters" on a
dashboard whose filter band is collapsed: sheets authored at the hidden-state position sit where the
band will be, so they are pushed below it and compressed. That is a genuine and necessary pass.

Its collision test asked only about Y. Two rectangles collide only if they overlap on BOTH axes, so
a sheet merely sitting at the band's HEIGHT -- in a side column the band never reaches -- triggered a
full-page reflow. And because the emit step FLOORS a slicer's height, a band can also grow past a
sheet the solver placed clear of it, so a grazing overlap is often the emitter reacting to its own
inflation rather than to anything the author wrote.

Measured on Salesforce NPSP, both failures on one page:

  * a full-height left column at x=0..439 (y=72..768) against a filter band at x=505..962 --
    ZERO horizontal overlap -- compressed every other visual to 87.5% and pushed it down 88px;
  * the parameter band, floored from a solved 45px to 57px, then claimed a 4.0px overlap with the
    month-chart row (2.9% of the sheet, and 0.0px against its own solved extent) and compressed the
    page to 61%.

The reflow's own arithmetic predicted 11 of that page's 12 emitted rects to within 1.5px, so it
accounted for the entire vertical error.
"""
import twb_to_pbir as T


def _v(x, y, w, h, z):
    return {"position": {"x": float(x), "y": float(y), "width": float(w),
                         "height": float(h), "z": z}}


def _slicer(x, y, w, h):
    return _v(x, y, w, h, T._Z_SLICER)


def _sheet(x, y, w, h):
    return _v(x, y, w, h, T._Z_CONTENT)


def _ys(visuals):
    return [round(v["position"]["y"], 1) for v in visuals]


PAGE_H = 768.0


def test_the_genuine_hidden_band_case_still_reflows():
    """The behaviour this pass exists for, and the one that must not regress: a sheet authored
    UNDER the band, sharing its column. Shape and numbers from the docstring's own worked example
    -- a card at y=241 beneath a band at 211..320."""
    band = [_slicer(500.0, 211.0, 200.0, 109.0)]
    sheet = _sheet(500.0, 241.0, 200.0, 110.0)
    visuals = band + [sheet]
    T._reflow_worksheets_below_slicers(visuals, PAGE_H)
    assert sheet["position"]["y"] > 320.0, \
        "a sheet sitting under the band was not pushed below it (y=%.1f)" % sheet["position"]["y"]


def test_a_sheet_in_a_column_the_band_never_reaches_is_left_alone():
    """The x-axis guard. Same y-range as the band, no horizontal overlap at all."""
    band = [_slicer(505.0, 93.0, 457.0, 59.0)]
    side = _sheet(0.0, 72.0, 439.0, 696.0)          # full-height left column, x=0..439
    other = _sheet(505.0, 400.0, 400.0, 200.0)
    visuals = band + [side, other]
    before = _ys([side, other])
    T._reflow_worksheets_below_slicers(visuals, PAGE_H)
    assert _ys([side, other]) == before, \
        "a column the band does not reach triggered a reflow: %s -> %s" % (before, _ys([side, other]))


def test_a_graze_from_our_own_slicer_floor_does_not_reflow():
    """The materiality guard. The emit step floors a slicer 45 -> 57px, so the band's bottom moves
    from 326 to 338 and grazes a sheet the solver placed at 334 -- 4px, 7% of the band."""
    band = [_slicer(460.0, 281.0, 578.0, 57.0)]      # floored; solved height was 45
    sheet = _sheet(460.0, 334.0, 267.0, 136.0)
    visuals = band + [sheet]
    T._reflow_worksheets_below_slicers(visuals, PAGE_H)
    assert sheet["position"]["y"] == 334.0, \
        "a 4px graze reflowed the page (y=%.1f)" % sheet["position"]["y"]


def test_a_substantial_overlap_in_the_same_column_still_reflows():
    """The control that keeps the materiality guard from switching the pass off. Same geometry as
    the graze test but the sheet genuinely sits IN the band."""
    band = [_slicer(460.0, 281.0, 578.0, 57.0)]
    sheet = _sheet(460.0, 290.0, 267.0, 136.0)       # overlaps 281..338 by 48px = 84% of the band
    visuals = band + [sheet]
    T._reflow_worksheets_below_slicers(visuals, PAGE_H)
    assert sheet["position"]["y"] > 338.0, \
        "a real collision was not reflowed (y=%.1f)" % sheet["position"]["y"]


def test_the_threshold_sits_between_the_two_measured_cases():
    """The constant is not tuned to one instance -- state the interval it has to separate, so a
    later change to it has to argue with the measurements rather than with a taste."""
    assert 0.07 < T._REFLOW_MIN_OVERLAP < 0.72, (
        "the false positive overlapped 7%% of the band and the genuine case 72%%; "
        "_REFLOW_MIN_OVERLAP=%r does not separate them" % T._REFLOW_MIN_OVERLAP)


def test_the_bands_x_extent_spans_every_slicer_in_it():
    """A band is a STRIP of slicers, so its horizontal extent is the union of all of them. Taking
    only the first one's x-range would leave a sheet sitting under the far end of the strip
    unreflowed -- and on the dashboard that motivated this the strips are 3 and 4 slicers wide."""
    left = _slicer(100.0, 200.0, 100.0, 60.0)     # x 100..200
    right = _slicer(300.0, 200.0, 100.0, 60.0)    # x 300..400, same band
    sheet = _sheet(320.0, 210.0, 100.0, 120.0)    # overlaps the RIGHT slicer only
    visuals = [left, right, sheet]
    T._reflow_worksheets_below_slicers(visuals, PAGE_H)
    assert sheet["position"]["y"] > 260.0, (
        "a sheet under the far end of the strip was not reflowed (y=%.1f) -- the band's x-extent "
        "did not span every slicer" % sheet["position"]["y"])


def test_slicers_and_decoration_are_never_moved():
    band = [_slicer(500.0, 211.0, 200.0, 109.0)]
    banner = _v(0.0, 0.0, 1366.0, 72.0, T._Z_BANNER)
    sheet = _sheet(500.0, 241.0, 200.0, 110.0)
    visuals = band + [banner, sheet]
    T._reflow_worksheets_below_slicers(visuals, PAGE_H)
    assert band[0]["position"]["y"] == 211.0
    assert banner["position"]["y"] == 0.0


def test_a_page_with_no_slicers_or_no_content_is_a_noop():
    only_slicers = [_slicer(0.0, 10.0, 100.0, 50.0)]
    T._reflow_worksheets_below_slicers(only_slicers, PAGE_H)
    assert only_slicers[0]["position"]["y"] == 10.0
    only_content = [_sheet(0.0, 10.0, 100.0, 50.0)]
    T._reflow_worksheets_below_slicers(only_content, PAGE_H)
    assert only_content[0]["position"]["y"] == 10.0


def test_the_reflow_picks_the_band_it_actually_collides_with():
    """Banding behaviour is unchanged: with a top filter strip the content clears and a lower
    parameter strip it does not, the LOWER band is the one that reflows."""
    top = _slicer(505.0, 93.0, 457.0, 59.0)
    low = _slicer(460.0, 281.0, 578.0, 57.0)
    sheet = _sheet(460.0, 290.0, 267.0, 136.0)
    visuals = [top, low, sheet]
    T._reflow_worksheets_below_slicers(visuals, PAGE_H)
    assert sheet["position"]["y"] > 338.0
