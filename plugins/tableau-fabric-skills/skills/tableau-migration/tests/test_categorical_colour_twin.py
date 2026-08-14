"""A STRING-valued measure on Tableau's Colour shelf needs a hex-returning twin in the MODEL.

Tableau's ordinary way to colour-code a text table is a calc that returns a LABEL --
``IF SUM([Profit]) < 0 THEN "negative" ELSE "positive" END`` -- dropped on Colour. Power BI cannot
drive a categorical legend from a MEASURE: a legend needs a grouping column, a column is row-level,
and a row-level split changes the aggregate grain (and the row count). So the faithful rebuild is
Microsoft's own documented answer -- *"a DAX measure that returns color values based on your
business logic"* -- applied through conditional formatting as the ``Field value`` format style.

This is the categorical sibling of the boolean colour twin, and it is gated on the Tableau-declared
``datatype`` rather than on the SHAPE of the translated DAX: a numeric measure that merely mentions
a string (a ``FORMAT`` pattern, a ``SWITCH`` label) must never acquire a bogus colour twin whose
"hex" values came from a format string.

The palette assignment is not arbitrary. Tableau hands its default categorical ramp to an unsorted
discrete domain in SORTED member order, so ``"negative"`` takes blue and ``"positive"`` orange --
which is what the source workbook renders, and therefore what the rebuild must reproduce.
"""

import assemble_model as A


def _row(measure, dax, datatype="string", status="translated"):
    return {"measure": measure, "dax": dax, "status": status, "datatype": datatype}


class TestDomainExtraction:
    def test_literals_are_ordered_by_first_appearance_and_deduped(self):
        assert A._dax_string_literals('SWITCH(x, "b", "a", "b", "c")') == ["b", "a", "c"]

    def test_an_escaped_quote_does_not_split_a_literal(self):
        assert A._dax_string_literals('IF(x, "say ""hi""", "no")') == ['say "hi"', "no"]

    def test_empty_literals_are_not_domain_members(self):
        # "" is a blank RESULT, not a member Tableau would have given a swatch.
        assert A._dax_string_literals('IF(x, "", "y")') == ["y"]


class TestTwinGeneration:
    def test_a_string_measure_gets_a_switch_twin_in_tableau_palette_order(self):
        rows = A._categorical_colour_twin_measures(
            [_row("Sign", 'IF(SUM(\'Orders\'[Profit]) < 0, "negative", "positive")')])
        assert len(rows) == 1
        twin = rows[0]
        assert twin["measure"] == "Sign (colour)"
        assert twin["status"] == "translated"
        # sorted domain -> palette order: negative (blue) before positive (orange)
        assert twin["dax"] == 'SWITCH([Sign], "negative", "#4E79A7", "#F28E2B")'
        assert twin["source"]["kind"] == "categorical_colour_twin"
        assert twin["source"]["base_measure"] == "Sign"
        assert twin["source"]["domain"] == ["negative", "positive"]

    def test_the_last_member_is_the_default_so_the_twin_never_returns_blank(self):
        # A BLANK colour paints the theme default, silently losing the encoding on any row the
        # domain scan did not anticipate.
        dax = A._categorical_colour_twin_measures(
            [_row("Band", 'SWITCH(TRUE(), a, "low", b, "mid", "high")')])[0]["dax"]
        assert dax.endswith('"#4E79A7")'), dax   # "high" sorts first -> blue is the default
        assert dax.count("SWITCH") == 1

    def test_three_members_take_three_distinct_palette_colours(self):
        dax = A._categorical_colour_twin_measures(
            [_row("Band", 'SWITCH(TRUE(), a, "a1", b, "b2", "c3")')])[0]["dax"]
        assert "#4E79A7" in dax and "#F28E2B" in dax and "#E15759" in dax


class TestItRefusesWhatItCannotStandBehind:
    def test_a_numeric_measure_mentioning_a_string_gets_no_twin(self):
        # THE false positive this gate exists for: the literals here are a format pattern, not a
        # colour domain, and a twin built from them would emit nonsense hexes.
        assert A._categorical_colour_twin_measures(
            [_row("Sales fmt", 'FORMAT(SUM(\'Orders\'[Sales]), "#,##0.00")',
                  datatype="real")]) == []

    def test_a_single_member_domain_gets_no_twin(self):
        # One member is not a colour ENCODING -- there is nothing to distinguish.
        assert A._categorical_colour_twin_measures(
            [_row("Const", 'IF(x, "only")')]) == []

    def test_a_stub_measure_gets_no_twin(self):
        assert A._categorical_colour_twin_measures(
            [_row("Sign", 'IF(x, "a", "b")', status="stub")]) == []

    def test_a_domain_larger_than_the_palette_is_declined(self):
        big = ", ".join('c%d, "m%d"' % (i, i) for i in range(12))
        assert A._categorical_colour_twin_measures(
            [_row("Many", "SWITCH(TRUE(), %s)" % big)]) == []

    def test_an_existing_twin_name_is_never_duplicated(self):
        rows = [_row("Sign", 'IF(x, "a", "b")'),
                _row("Sign (colour)", '"#000000"')]
        assert [r["measure"] for r in A._categorical_colour_twin_measures(rows)] == []

    def test_no_string_measure_means_no_change_at_all(self):
        assert A._categorical_colour_twin_measures(
            [_row("Sales", "SUM('Orders'[Sales])", datatype="real")]) == []
        assert A._categorical_colour_twin_measures([]) == []
        assert A._categorical_colour_twin_measures(None) == []


class TestQuotingIsSafe:
    def test_a_member_containing_a_quote_is_escaped_into_the_switch(self):
        dax = A._categorical_colour_twin_measures(
            [_row("Q", 'IF(x, "say ""hi""", "no")')])[0]["dax"]
        # the member must survive as a valid DAX literal, doubled quotes and all
        assert '"say ""hi"""' in dax


# -- palette resolution: authored > (opt-in) semantic > Tableau's own assignment ----------------
class TestAuthoredPaletteWins:
    def test_the_authors_own_colours_are_used_verbatim(self):
        twin = A._categorical_colour_twin_measures(
            [_row("Sign", 'IF(x, "negative", "positive")')],
            colour_palettes={"Sign": [("negative", "#111111"), ("positive", "#222222")]})[0]
        assert twin["dax"] == 'SWITCH([Sign], "negative", "#111111", "#222222")'
        assert twin["source"]["palette_origin"] == "authored"

    def test_the_match_is_case_insensitive_on_the_member(self):
        twin = A._categorical_colour_twin_measures(
            [_row("Sign", 'IF(x, "Negative", "Positive")')],
            colour_palettes={"sign": [("NEGATIVE", "#111111"), ("positive", "#222222")]})[0]
        assert twin["source"]["palette_origin"] == "authored"
        assert "#111111" in twin["dax"] and "#222222" in twin["dax"]

    def test_a_partial_assignment_is_refused_rather_than_mixed(self):
        # Mixing would silently RECOLOUR the members the author did choose: the unassigned member
        # takes a default slot and shifts the others' positions in the ramp.
        twin = A._categorical_colour_twin_measures(
            [_row("Sign", 'IF(x, "negative", "positive")')],
            colour_palettes={"Sign": [("negative", "#111111")]})[0]
        assert twin["source"]["palette_origin"] == "tableau_default"

    def test_authored_beats_the_semantic_flag(self):
        twin = A._categorical_colour_twin_measures(
            [_row("Sign", 'IF(x, "negative", "positive")')],
            colour_palettes={"Sign": [("negative", "#111111"), ("positive", "#222222")]},
            semantic_colours=True)[0]
        assert twin["source"]["palette_origin"] == "authored"


class TestSemanticPaletteIsOptIn:
    def test_off_by_default_the_source_hues_are_reproduced(self):
        # An unauthored domain is NOT colourless -- Tableau paints it from its own ramp, and that
        # is what the source workbook renders. Inventing red/green by default would make the
        # rebuild differ in hue from the thing it reproduces.
        twin = A._categorical_colour_twin_measures(
            [_row("Sign", 'IF(x, "negative", "positive")')])[0]
        assert twin["dax"] == 'SWITCH([Sign], "negative", "#4E79A7", "#F28E2B")'
        assert twin["source"]["palette_origin"] == "tableau_default"

    def test_on_a_polarity_domain_becomes_red_and_green(self):
        twin = A._categorical_colour_twin_measures(
            [_row("Sign", 'IF(x, "negative", "positive")')], semantic_colours=True)[0]
        assert twin["dax"] == 'SWITCH([Sign], "negative", "#D62728", "#2CA02C")'
        assert twin["source"]["palette_origin"] == "semantic"

    def test_the_pole_follows_the_member_not_its_position(self):
        # positive first in the formula -> green must still land on "positive".
        twin = A._categorical_colour_twin_measures(
            [_row("Sign", 'IF(x, "positive", "negative")')], semantic_colours=True)[0]
        assert twin["dax"] == 'SWITCH([Sign], "positive", "#2CA02C", "#D62728")'

    def test_other_polarity_vocabularies_are_recognised(self):
        for neg, pos in (("loss", "profit"), ("fail", "pass"), ("below", "above")):
            twin = A._categorical_colour_twin_measures(
                [_row("B", 'IF(x, "%s", "%s")' % (neg, pos))], semantic_colours=True)[0]
            assert twin["source"]["palette_origin"] == "semantic"
            assert "#D62728" in twin["dax"] and "#2CA02C" in twin["dax"]

    def test_a_domain_that_is_not_a_polarity_is_left_alone(self):
        # "East"/"West" is two members, not good and bad -- painting it red/green would assert a
        # meaning the author never stated.
        twin = A._categorical_colour_twin_measures(
            [_row("Region", 'IF(x, "East", "West")')], semantic_colours=True)[0]
        assert twin["source"]["palette_origin"] == "tableau_default"

    def test_a_three_member_domain_is_not_a_polarity(self):
        twin = A._categorical_colour_twin_measures(
            [_row("B", 'SWITCH(TRUE(), a, "negative", b, "positive", "flat")')],
            semantic_colours=True)[0]
        assert twin["source"]["palette_origin"] == "tableau_default"

    def test_both_poles_must_be_recognised(self):
        # one recognised member is not a polarity domain
        assert A._semantic_polarity_palette(["negative", "somethingelse"]) is None
        assert A._semantic_polarity_palette(["negative", "loss"]) is None, "same pole twice"