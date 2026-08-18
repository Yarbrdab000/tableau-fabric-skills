"""Tableau's "how many marks are in this view" idiom, and its escaped colour-map members.

Both defects are from the same real workbook -- a dynamic-quartile dashboard that buckets rows into
Top 25% / middle / Bottom 25% by rank and colours them. Each one alone was enough to lose the
encoding entirely, and neither produced an error.

1. `WINDOW_COUNT(COUNTD([<dimension>]))` is how a Tableau author writes "the number of marks in this
   view". `WINDOW_COUNT` counts the marks in the frame for which its argument is non-null -- it does
   not aggregate the argument -- and a `COUNTD` of a dimension is never null at a mark. Lowering it
   through the generic aggregator path requires resolving `COUNTD([D])` to a projected column, which
   fails, because the visual GROUPS BY that dimension rather than projecting a distinct count of it.
   The whole colour rule then declined and the dashboard rendered uncoloured.

2. Tableau backslash-escapes some characters when it serialises a colour-map member: the author's
   palette says `"Top 25\\%"` while the calculation says `"Top 25%"`. A literal comparison misses,
   the member falls through to the default categorical ramp, and the author's own green/red becomes
   two arbitrary hues -- silently, and only on members whose names happen to contain an escaped
   character.
"""

import colour_rules as CR
import twb_to_pbir as T


QUARTILE = (
    'if RANK(SUM([Profit])) <= (WINDOW_COUNT(COUNTD([Swap Calc]))*.25) then "Top 25%"\n'
    'ELSEIF RANK(SUM([Profit])) > (WINDOW_COUNT(COUNTD([Swap Calc]))*.75) then "Bottom 25%"\n'
    'else "middle" END'
)


def _profit_only(toks):
    """A resolver that knows the visual's measure and nothing else -- the real situation."""
    text = "".join(str(t[1]) for t in toks)
    return "[Sum of Profit]" if "Profit" in text else None


class TestTheViewMarkCountIdiom:
    def test_the_quartile_calc_lowers_without_resolving_the_countd(self):
        spec = CR.analyse_colour_calc(QUARTILE)
        dax = CR.lower_to_visual_calc(
            spec, {"Top 25%": "#59a14f", "Bottom 25%": "#e15759", "middle": "#000000"},
            _profit_only)

        assert dax, "the rule must not decline just because COUNTD is not projected"
        assert "COUNTROWS(WINDOW(1, ABS, -1, ABS))" in dax
        assert "COUNTD" not in dax, "the COUNTD must be gone, not passed through"

    def test_both_quartile_bounds_survive(self):
        spec = CR.analyse_colour_calc(QUARTILE)
        dax = CR.lower_to_visual_calc(spec, {m: "#000000" for m in spec.members}, _profit_only)

        assert "* .25" in dax and "* .75" in dax
        assert dax.count("COUNTROWS(WINDOW(1, ABS, -1, ABS))") == 2

    def test_it_is_the_same_answer_as_tableau_SIZE(self):
        """`SIZE()` already meant 'marks in the view'; this idiom must agree with it."""
        size = CR.dax_value([("id", "SIZE"), ("op", "("), ("op", ")")], lambda t: None)
        idiom = CR.dax_value(
            [("id", "WINDOW_COUNT"), ("op", "("), ("id", "COUNTD"), ("op", "("),
             ("field", "Anything"), ("op", ")"), ("op", ")")], lambda t: None)

        assert idiom == size

    def test_window_count_of_something_else_still_needs_its_operand(self):
        """Fail-closed: the shortcut is for COUNTD only, not for WINDOW_COUNT generally."""
        toks = [("id", "WINDOW_COUNT"), ("op", "("), ("id", "SUM"), ("op", "("),
                ("field", "Nope"), ("op", ")"), ("op", ")")]

        assert CR.dax_value(toks, lambda t: None) is None

    def test_another_window_aggregator_over_countd_is_not_shortcut(self):
        """WINDOW_SUM(COUNTD(x)) is a real sum of distinct counts, not a mark count."""
        toks = [("id", "WINDOW_SUM"), ("op", "("), ("id", "COUNTD"), ("op", "("),
                ("field", "D"), ("op", ")"), ("op", ")")]

        assert CR.dax_value(toks, lambda t: None) is None

    def test_explicit_window_bounds_are_honoured(self):
        """The frame comes from the call, not from a hard-coded whole-partition window."""
        toks = [("id", "WINDOW_COUNT"), ("op", "("), ("id", "COUNTD"), ("op", "("),
                ("field", "D"), ("op", ")"), ("op", ","), ("num", "0"), ("op", ","),
                ("num", "0"), ("op", ")")]
        dax = CR.dax_value(toks, lambda t: None)

        assert dax is not None and dax.startswith("COUNTROWS(WINDOW(")


class TestEscapedColourMapMembers:
    @staticmethod
    def _ws(*members):
        return {"mark_colors": {"members": [{"value": v, "color": c} for v, c in members]}}

    def test_an_escaped_member_still_matches_the_calc_member(self):
        ws = self._ws(("Top 25\\%", "#59a14f"), ("Bottom 25\\%", "#e15759"),
                      ("middle", "#000000"))
        palette = T._colour_member_palette(ws, ["Top 25%", "Bottom 25%", "middle"])

        assert palette == {"Top 25%": "#59a14f", "Bottom 25%": "#e15759", "middle": "#000000"}

    def test_without_the_fix_these_would_be_default_ramp_hues(self):
        """Pins the actual failure: the authored colours must not be silently replaced."""
        ws = self._ws(("Top 25\\%", "#59a14f"))
        palette = T._colour_member_palette(ws, ["Top 25%", "Bottom 25%", "middle"])

        assert palette["Top 25%"] == "#59a14f"
        assert palette["Top 25%"] not in T._TABLEAU_10

    def test_an_unescaped_member_still_matches_exactly_as_before(self):
        ws = self._ws(("positive", "#59a14f"), ("negative", "#e15759"))
        palette = T._colour_member_palette(ws, ["positive", "negative"])

        assert palette == {"positive": "#59a14f", "negative": "#e15759"}

    def test_a_member_whose_real_name_contains_a_backslash_is_not_broken(self):
        """Both spellings are indexed, so an exact match is never lost to the unescaping."""
        ws = self._ws(("a\\b", "#123456"))

        assert T._colour_member_palette(ws, ["a\\b"])["a\\b"] == "#123456"

    def test_an_unauthored_member_still_falls_back_to_the_ramp(self):
        ws = self._ws(("Top 25\\%", "#59a14f"))
        palette = T._colour_member_palette(ws, ["Top 25%", "somethingelse"])

        assert palette["Top 25%"] == "#59a14f"
        assert palette["somethingelse"] in T._TABLEAU_10

    def test_no_authored_palette_at_all_is_unchanged(self):
        palette = T._colour_member_palette({}, ["a", "b"])

        assert set(palette) == {"a", "b"}
        assert all(v in T._TABLEAU_10 for v in palette.values())
