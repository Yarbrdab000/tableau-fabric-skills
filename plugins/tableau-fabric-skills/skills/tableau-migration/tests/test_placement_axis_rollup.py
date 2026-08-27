"""Placement drift is reported PER AXIS, with sign (#169).

THE REPORT. Four unrelated workbooks in one estate showed a large dead vertical gap between the
page header and the content, and the reporting engineer's coordinate-level lead was that **X
translates ~1:1 while Y visibly drifts**. They filed it explicitly unreproduced and asked what
would settle it.

WHY OUR INSTRUMENT COULD NOT ANSWER. ``summary.placement`` already existed and is **axis-blind**:
``max_edge_px`` is a max over all four edges, ``mean_max_edge_px`` averages that. A claim of the
form "one axis is fine and the other is not" is not expressible in it, so neither the reporter nor
we could confirm or refute the one specific lead the report contained. Same shape as every other
absence-blind gate here: the metric enumerates a quantity that structurally cannot carry the
distinction being asked about.

WHAT THE PER-AXIS ROLLUP THEN MEASURED, 34-workbook corpus at 2.331.0, 104 zone-to-visual pairs:

* **21 of 29** scorable workbooks are pixel-exact on BOTH axes;
* **6** show the reported shape (Y mean error exceeding X by >5 px), and on **4 of those X is
  exactly 0.0 px** while Y is 60-256 px out -- e.g. ``0067_global_filter``, X 8/8 exact at 0.0 px,
  Y 3/8 exact at 60.3 px;
* every affected workbook is a MULTI-ZONE dashboard; the pixel-perfect ones are mostly single-visual.

AND THE HALF THAT ABSOLUTE FIGURES WOULD HAVE GOT WRONG. Across the corpus the drift is **not** a
constant downward offset: 37 down against 23 up, signed mean +4.3 px, and per workbook it runs
+108, +65, -56, +7.6. The report's hypothesised mechanism -- a header band subtracted from one axis
-- predicts a consistent sign, and the sign is not consistent. Reporting only ``mean_abs_px`` would
have corroborated a mechanism the signed figures rule out, which is why ``mean_signed_px`` and the
direction counts are part of the rollup rather than an afterthought.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from fidelity_oracle import _placement_rollup  # noqa: E402


def _vis(ws, left, top, right=None, bottom=None):
    right = left if right is None else right
    bottom = top if bottom is None else bottom
    m = max(abs(left), abs(top), abs(right), abs(bottom))
    return {"worksheet": ws, "placement": {
        "delta_px": {"left": left, "top": top, "right": right, "bottom": bottom,
                     "width": 0.0, "height": 0.0, "center": 0.0},
        "max_edge_px": m, "pixel_exact": m <= 1.0, "within_tolerance": m <= 64.0}}


def test_no_placement_data_still_returns_none():
    assert _placement_rollup([]) is None
    assert _placement_rollup([{"worksheet": "A"}]) is None


def test_the_existing_axis_blind_fields_are_unchanged():
    """Additive. Anything already reading this rollup must see exactly what it saw before."""
    r = _placement_rollup([_vis("A", 0.0, 40.0), _vis("B", 0.0, 0.0)])
    for k in ("evaluated", "pixel_exact", "within_tolerance", "drifted",
              "worst_max_edge_px", "mean_max_edge_px", "worst_worksheet", "verdict"):
        assert k in r, k
    assert r["evaluated"] == 2 and r["pixel_exact"] == 1
    assert r["worst_max_edge_px"] == 40.0


def test_the_reported_shape_is_now_expressible():
    """THE POINT. X perfect, Y drifting -- the exact claim the axis-blind metric could not carry."""
    r = _placement_rollup([_vis("A", 0.0, 108.0), _vis("B", 0.0, 65.0), _vis("C", 0.0, 60.0)])
    x, y = r["by_axis"]["x"], r["by_axis"]["y"]
    assert x["exact"] == 3 and x["mean_abs_px"] == 0.0
    assert y["exact"] == 0 and y["mean_abs_px"] > 60
    # The old fields cannot distinguish this from the mirror image; assert that directly so the
    # motivation for by_axis is pinned, not just described in a docstring.
    mirrored = _placement_rollup([_vis("A", 108.0, 0.0), _vis("B", 65.0, 0.0), _vis("C", 60.0, 0.0)])
    assert mirrored["worst_max_edge_px"] == r["worst_max_edge_px"]
    assert mirrored["mean_max_edge_px"] == r["mean_max_edge_px"]
    assert mirrored["by_axis"]["x"]["mean_abs_px"] != r["by_axis"]["x"]["mean_abs_px"]


def test_sign_separates_a_systematic_offset_from_scatter():
    """The half absolute figures get wrong. These two populations have IDENTICAL magnitudes and
    demand completely different investigations -- one is an offset, one is noise."""
    offset = _placement_rollup([_vis("A", 0.0, 50.0), _vis("B", 0.0, 50.0), _vis("C", 0.0, 50.0)])
    scatter = _placement_rollup([_vis("A", 0.0, 50.0), _vis("B", 0.0, -50.0), _vis("C", 0.0, 50.0)])
    assert offset["by_axis"]["y"]["mean_abs_px"] == scatter["by_axis"]["y"]["mean_abs_px"]
    assert offset["by_axis"]["y"]["mean_signed_px"] == 50.0
    assert round(scatter["by_axis"]["y"]["mean_signed_px"], 2) == 16.67
    assert (offset["by_axis"]["y"]["positive"], offset["by_axis"]["y"]["negative"]) == (3, 0)
    assert (scatter["by_axis"]["y"]["positive"], scatter["by_axis"]["y"]["negative"]) == (2, 1)


def test_median_and_worst_are_both_reported():
    """A mean alone is dominated by one bad pair; a median alone hides that a pair is 600 px out."""
    r = _placement_rollup([_vis("A", 0.0, 0.0), _vis("B", 0.0, 0.0), _vis("C", 0.0, 600.0)])
    y = r["by_axis"]["y"]
    assert y["median_abs_px"] == 0.0
    assert y["worst_abs_px"] == 600.0
    assert y["mean_abs_px"] == 200.0


def test_both_axes_are_always_present_together():
    """A consumer comparing axes must never get one of them; that reads as 'the other is fine'."""
    r = _placement_rollup([_vis("A", 3.0, 4.0)])
    assert set(r["by_axis"]) == {"x", "y"}
    assert r["by_axis"]["x"]["evaluated"] == r["by_axis"]["y"]["evaluated"] == 1
