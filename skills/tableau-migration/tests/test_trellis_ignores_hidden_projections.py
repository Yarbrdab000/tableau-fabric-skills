"""A measure trellis is a property of the SOURCE SHELF, so a hidden projection can never be a band.

Tableau lays several measures side by side by concatenating separate measure pills with ``+`` on one
shelf; each pill gets its own pane. The rebuild fans those into N side-by-side charts. The signature
is therefore about what the WORKSHEET declares, not about what the query happens to compute.

``0060_adjustable_fixed_axis``'s ``Challenge`` worksheet declares exactly one measure pill --
``pcto:sum:Sales:qk``, a single percent-of-total quick table calc -- so Tableau draws ONE pane. The
rebuild emitted TWO charts, because the quick-calc path keeps the raw base measure as a HIDDEN
projection purely so its Visual Calculation can reference it, and the trellis detector counted it.
The second chart drew a projection explicitly marked ``hidden: true``.

That is the defect these tests pin: a trellis inferred from something the source shelf does not
describe. It predates the conditional-colour work, and any emitter that adds a hidden projection
(the colour Visual Calculation now does) would spread it further.
"""

import twb_to_pbir as R


def _state(y, cat=1):
    """A query state with ``y`` = the Y projections and ``cat`` category columns."""
    return {
        "Y": {"projections": list(y)},
        "Category": {"projections": [{"queryRef": "c%d" % i} for i in range(cat)]},
    }


def _ws(**over):
    ws = {"visual_type": R.VT_BAR, "uses_measure_values": False, "dual_axis": False}
    ws.update(over)
    return ws


VISIBLE_A = {"queryRef": "m0", "nativeQueryRef": "Sum of Sales"}
VISIBLE_B = {"queryRef": "m1", "nativeQueryRef": "Sum of Profit"}
HIDDEN = {"queryRef": "m2", "nativeQueryRef": "Sum of Sales", "hidden": True}


class TestAHiddenProjectionIsNeverATrellisBand:
    def test_one_visible_measure_beside_a_hidden_one_is_not_a_trellis(self):
        """0060's exact shape: one percent-of-total pill + the hidden base measure it references."""
        pct = {"queryRef": "m0", "nativeQueryRef": "Percent of Total"}

        assert R._detect_measure_trellis(_ws(), _state([HIDDEN, pct])) is None

    def test_the_hidden_projection_is_not_returned_as_a_band(self):
        """Even when a genuine trellis IS present, a hidden projection is not one of its panes."""
        bands = R._detect_measure_trellis(_ws(), _state([VISIBLE_A, HIDDEN, VISIBLE_B]))

        assert bands is not None
        assert [b["queryRef"] for b in bands] == ["m0", "m1"]
        assert all(not b.get("hidden") for b in bands)

    def test_two_visible_measures_are_still_a_trellis(self):
        """The fix must be surgical -- a real measure trellis is unchanged."""
        bands = R._detect_measure_trellis(_ws(), _state([VISIBLE_A, VISIBLE_B]))

        assert bands is not None
        assert len(bands) == 2

    def test_only_hidden_measures_is_not_a_trellis(self):
        assert R._detect_measure_trellis(_ws(), _state([HIDDEN, dict(HIDDEN, queryRef="m3")])) is None

    def test_a_single_visible_measure_is_still_not_a_trellis(self):
        assert R._detect_measure_trellis(_ws(), _state([VISIBLE_A])) is None

    def test_the_other_guards_are_untouched(self):
        """Each pre-existing guard still declines on its own, with two visible measures present."""
        two = _state([VISIBLE_A, VISIBLE_B])

        assert R._detect_measure_trellis(_ws(visual_type=R.VT_LINE), two) is None
        assert R._detect_measure_trellis(_ws(uses_measure_values=True), two) is None
        assert R._detect_measure_trellis(_ws(dual_axis=True), two) is None
        assert R._detect_measure_trellis(_ws(), _state([VISIBLE_A, VISIBLE_B], cat=0)) is None

    def test_a_series_split_still_declines(self):
        state = _state([VISIBLE_A, VISIBLE_B])
        state["Series"] = {"projections": [{"queryRef": "s0"}]}

        assert R._detect_measure_trellis(_ws(), state) is None
