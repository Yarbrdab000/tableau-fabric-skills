"""A field's authored Tableau number format must reach the rebuilt visual.

Tableau records it on the ``<column>`` as ``default-format``: one leading marker character selects
the family and the remainder is an Excel-style pattern -- the same dialect Power BI format strings
use, so the pattern passes through verbatim.

    n#,##0;-#,##0                     number
    c"$"#,##0,,.00M;-"$"#,##0,,.00M   currency, millions-scaled
    p0.0%                             percent
    *<up>0.0%;<down>0.0%              custom -- glyphs the author drew INTO the format

The emitter read this attribute NOWHERE, so every measure rendered raw: ``339851`` for
``339,851``, and a month-over-month delta as ``0.0070`` where Tableau drew ``<up>0.7%``. The arrows
in the reference dashboard are not a chart feature at all -- they are the number format, which is
why they only appear once ``default-format`` is honoured.

Value-kind fields only: a measure's format is unambiguous, whereas Tableau's DATE patterns use its
own token vocabulary rather than the .NET/Excel one and would silently mis-render if passed
through. Anything unrecognised yields ``None`` and keeps Power BI's default.
"""

import pytest

from scripts.twb_to_pbir import _role_projections, _tableau_number_format

ARROWS = "\u25b20.0%;\u25bc0.0%"


class TestMarkerFamilies:

    @pytest.mark.parametrize("raw,expected", [
        ("n#,##0;-#,##0", "#,##0;-#,##0"),
        ("n#,##0.0;-#,##0.0", "#,##0.0;-#,##0.0"),
        ("n#,##0,,,.00B;-#,##0,,,.00B", "#,##0,,,.00B;-#,##0,,,.00B"),
        ("p0.0%", "0.0%"),
        ("p0.00%", "0.00%"),
        ("c\"$\"#,##0,,.00M;-\"$\"#,##0,,.00M", "\"$\"#,##0,,.00M;-\"$\"#,##0,,.00M"),
        ("*" + ARROWS, ARROWS),
    ])
    def test_the_marker_is_stripped_and_the_pattern_survives(self, raw, expected):
        assert _tableau_number_format(raw) == expected

    def test_the_arrow_glyphs_are_preserved_exactly(self):
        """The up/down arrows ARE the format; losing them loses the direction cue."""
        out = _tableau_number_format("*" + ARROWS)
        assert "\u25b2" in out and "\u25bc" in out

    def test_the_negative_section_survives(self):
        """Power BI reads ``positive;negative`` the same way, so both sections must pass through."""
        assert _tableau_number_format("n#,##0;-#,##0").count(";") == 1

    def test_quoted_currency_literal_survives(self):
        assert _tableau_number_format("c\"$\"#,##0").startswith("\"$\"")

    def test_scaling_commas_survive(self):
        """``,,`` is the millions scale; dropping it would render 566790000 as-is."""
        assert ",," in _tableau_number_format("c\"$\"#,##0,,.00M")


class TestFailClosed:

    @pytest.mark.parametrize("raw", [None, "", "   ", "n", "p", "*", "c"])
    def test_empty_or_pattern_less_input_yields_none(self, raw):
        assert _tableau_number_format(raw) is None

    @pytest.mark.parametrize("raw", ["n ", "p  ", "*\t", "c   "])
    def test_a_marker_with_a_blank_pattern_yields_none(self, raw):
        """Long enough to pass the length guard but carrying no pattern -- still no format.

        An empty STRING here would be a format string meaning "render nothing", not "leave the
        default alone"; the two are very different at render time.
        """
        assert _tableau_number_format(raw) is None

    def test_a_date_format_is_left_alone(self):
        """Tableau's date tokens are not the .NET/Excel ones; passing them through would lie."""
        assert _tableau_number_format("dMMMM yyyy") is None

    def test_an_unmarked_pattern_is_refused(self):
        """Without the family marker there is nothing to say the dialect matches."""
        assert _tableau_number_format("#,##0") is None

    def test_whitespace_is_tolerated_around_the_value(self):
        assert _tableau_number_format("  p0.0%  ") == "0.0%"


def _field(name, kind="value", fmt=None):
    return {"entity": "Facts", "property": name, "binding": "measure" if kind == "value" else "column",
            "aggregation": "Sum" if kind == "value" else None, "caption": name, "kind": kind,
            "is_calc": False, "number_format": fmt}


class TestProjectionCarriesTheFormat:

    def test_a_measure_projection_gets_its_format(self):
        projs = _role_projections([_field("Revenue", fmt="#,##0")], "Facts", {}, set())
        assert projs[0]["format"] == "#,##0"

    def test_a_measure_without_a_format_gets_no_key(self):
        """Absent means "Power BI default", which is not the same as an empty format string."""
        projs = _role_projections([_field("Revenue")], "Facts", {}, set())
        assert "format" not in projs[0]

    def test_a_category_projection_never_gets_a_format(self):
        projs = _role_projections([_field("Month", kind="category", fmt="#,##0")],
                                  "Facts", {}, set())
        assert "format" not in projs[0]

    def test_the_arrow_format_reaches_the_projection(self):
        projs = _role_projections([_field("MoM", fmt=ARROWS)], "Facts", {}, set())
        assert projs[0]["format"] == ARROWS

    def test_each_projection_keeps_its_own_format(self):
        projs = _role_projections(
            [_field("A", fmt="#,##0"), _field("B", fmt="0.0%"), _field("C")], "Facts", {}, set())
        assert [p.get("format") for p in projs] == ["#,##0", "0.0%", None]

    def test_projection_count_is_unchanged_by_formatting(self):
        """Formatting is decoration: it must never add or drop a projection."""
        fields = [_field("A", fmt="#,##0"), _field("B")]
        assert len(_role_projections(fields, "Facts", {}, set())) == 2
