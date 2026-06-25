"""Tests for ``tableau_default_format_to_pbi`` -- the Tableau ``<column @default-format>``
to Power BI ``formatString`` decoder.

Grounded in a corpus decode table (11 distinct codes / 461 occurrences across 29 .twb).
The decoder is additive: an unrecognized code returns ``None`` so the caller keeps its
type-derived format and a field never regresses.
"""
import pytest

from tmdl_generate import tableau_default_format_to_pbi as decode


# -- the 11 corpus codes: lowercase type-prefix / * are stripped + passed through -------
@pytest.mark.parametrize(
    "code, expected",
    [
        # currency (c): strip the prefix, remainder is a valid .NET custom format
        ('c"$"#,##0;("$"#,##0)', '"$"#,##0;("$"#,##0)'),
        ('c"$"#,##0.00;("$"#,##0.00)', '"$"#,##0.00;("$"#,##0.00)'),
        # currency scaled-to-thousands with a literal K (K is a .NET literal: verbatim)
        ('c"$"#,##0,K;("$"#,##0,K)', '"$"#,##0,K;("$"#,##0,K)'),
        # percent (p): the bare % is the percent OPERATOR -> value scaled x100
        ("p0%", "0%"),
        ("p0.0%", "0.0%"),
        ("p0.00%", "0.00%"),
        # number (n): grouped, signed
        ("n#,##0;-#,##0", "#,##0;-#,##0"),
        ("n#,##0.00;-#,##0.00", "#,##0.00;-#,##0.00"),
        # number with a LITERAL percent glyph -> value NOT scaled (distinct from p)
        ('n#,##0.0"%";-#,##0.0"%"', '#,##0.0"%";-#,##0.0"%"'),
        # * zero-pad (zip / id)
        ("*00000", "00000"),
        # uppercase type+LCID percent -> en-US locale percent
        ("C1033%", "0%"),
    ],
)
def test_corpus_codes_decode(code, expected):
    assert decode(code) == expected


# -- caveat 1: literal-percent (n..."%") vs operator-percent (p...%) stay distinct -----
def test_literal_percent_keeps_quoted_glyph_and_is_not_scaled():
    # quoted "%" -> literal glyph, no x100 scaling
    assert decode('n#,##0.0"%";-#,##0.0"%"') == '#,##0.0"%";-#,##0.0"%"'


def test_operator_percent_is_bare_and_scales():
    # bare % -> Power BI percent operator (x100)
    assert decode("p0.0%") == "0.0%"
    assert '"' not in decode("p0.0%")


# -- caveat 2 / the one judgment call: uppercase C<lcid>% -> percent; other C... falls back
def test_upper_lcid_percent_is_percent():
    assert decode("C1033%") == "0%"
    assert decode("C1036%") == "0%"  # any LCID, trailing % present


@pytest.mark.parametrize("code", ["C1033", "C1033$", "C", "C1033x", "N1033%", "P1033%"])
def test_other_uppercase_forms_fall_back(code):
    # no trailing %, or not the C<digits>% shape -> None (keep the type-derived floor)
    assert decode(code) is None


# -- never-regress fallbacks: empty / whitespace / unknown / empty remainder -----------
@pytest.mark.parametrize("code", [None, "", "   ", "\t", "z123", "123", "#,##0", '"$"#,##0'])
def test_unrecognized_codes_return_none(code):
    assert decode(code) is None


@pytest.mark.parametrize("code", ["c", "n", "p", "*"])
def test_bare_prefix_with_empty_remainder_returns_none(code):
    # a prefix with nothing after it is not a usable format
    assert decode(code) is None


def test_surrounding_whitespace_is_tolerated():
    assert decode("  p0%  ") == "0%"


def test_decode_never_raises_on_odd_input():
    # the decoder must be total over str input (callers pass raw attribute values)
    for code in ["", " ", "c", "%", "()", "c;;;", "*", "C%", "C1033%%"]:
        decode(code)  # must not raise
