"""A ``scopeId`` colour comparison against a BOOLEAN column must use a bare boolean literal (#178).

In PBIR literal encoding ``'x'`` denotes a **string**, so ``{"Literal": {"Value": "'true'"}}`` is the
string ``true``, not the boolean. Compared against a boolean column the comparison never matches --
and Power BI does not complain. It silently drops the entire colour encoding and every series falls
back to the theme colour.

REPORTED AND RENDER-VERIFIED BY THE REPORTER, who also corrected their own initial framing:

    crashes Desktop   no
    errors on open    no
    validate          0 errors, 0 warnings -- BEFORE and AFTER
    actual effect     the whole colour channel is silently dropped

So nothing static sees it, on either side. They also ruled out the obvious alternative (a per-point
colour over an unprojected field): the colour driver IS projected in the Series role.

Reproduced here on the public Superstore sample at engine 2.358.0, same two visual ids they cite:

    90 visual.json, 825 Literal values
    4 quoted-boolean literals in 2 stackedAreaChart visuals on page-Overview
    v-page-Overview3a0e30b33   'true' / 'false'
    v-page-Overview3d0164b68   'true' / 'false'

After the fix, the same build emits 4 BARE booleans and the 3 genuine quoted strings on that page
are unchanged.

THE ENGINE ALREADY KNEW. Sibling literals in the SAME file are all encoded correctly -- bare
``false`` for ``showAxisTitle``, ``1D`` for a Double, ``'Scalar'`` and ``'#4e79a7'`` for real
strings. This was one path that never consulted the column type, not a missing encoder.

WHY THIS KEYS ON ``datatype`` AND NEVER ON THE VALUE: a genuine string dimension may legitimately
have members named ``"true"``/``"false"``. Encoding those as bare booleans would break a comparison
that works today -- the same defect in the other direction, which is what
``test_a_string_column_with_true_false_members_stays_quoted`` exists to prevent.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import twb_to_pbir as T  # noqa: E402


BOOL_COL = {"caption": "Order Profitable?", "kind": "category", "binding": "column",
            "datatype": "boolean", "is_calc": True, "role": "dimension"}
STR_COL = {"caption": "Region", "kind": "category", "binding": "column",
           "datatype": "string", "is_calc": False, "role": "dimension"}


def test_a_boolean_column_gets_a_bare_boolean_literal():
    assert T._scope_member_literal("true", BOOL_COL) == "true"
    assert T._scope_member_literal("false", BOOL_COL) == "false"


def test_the_bare_literal_is_not_quoted():
    """The whole defect in one assertion: ``'x'`` means STRING in PBIR literal encoding."""
    for v in ("true", "false"):
        out = T._scope_member_literal(v, BOOL_COL)
        assert not out.startswith("'") and not out.endswith("'"), out


def test_case_and_whitespace_are_tolerated():
    assert T._scope_member_literal("True", BOOL_COL) == "true"
    assert T._scope_member_literal(" FALSE ", BOOL_COL) == "false"


def test_a_string_column_with_true_false_members_stays_quoted():
    """The defect in the OTHER direction, and the reason this keys on datatype rather than value.

    A real string dimension may have members literally named "true". Emitting those bare would
    break a comparison that is correct today.
    """
    assert T._scope_member_literal("true", STR_COL) == T._semantic_string_literal("true")
    assert T._scope_member_literal("true", STR_COL).startswith("'")


def test_an_ordinary_member_is_byte_identical_to_the_previous_behaviour():
    """Everything outside the boolean case must be unchanged, so the blast radius is exactly the bug."""
    for v in ("West", "Consumer", "2023", "", "O'Brien"):
        assert T._scope_member_literal(v, STR_COL) == T._semantic_string_literal(v)
        assert T._scope_member_literal(v, BOOL_COL) == T._semantic_string_literal(v)


def test_a_boolean_column_with_an_unrecognised_member_stays_quoted():
    """Fail CLOSED. Do not invent a boolean for a value that is not one."""
    assert T._scope_member_literal("maybe", BOOL_COL) == T._semantic_string_literal("maybe")


def test_a_missing_or_untyped_colour_field_stays_quoted():
    """The colour field is optional upstream; absence must not change the encoding."""
    assert T._scope_member_literal("true", None) == T._semantic_string_literal("true")
    assert T._scope_member_literal("true", {}) == T._semantic_string_literal("true")
    assert T._scope_member_literal("true", {"datatype": None}) == T._semantic_string_literal("true")


def test_the_emitter_passes_the_colour_field_through():
    """That the type is CONSULTED at the call site, not merely that the encoder can consult it.

    The encoder is correct in isolation whether or not anything hands it the column, and a call
    that passed no field would run clean and emit the quoted form on every build.
    """
    import inspect

    src = inspect.getsource(T._data_point_colors)
    assert "_scope_member_literal(m[\"value\"], color)" in src, (
        "the scopeId emitter no longer passes the colour field to the literal encoder; "
        "the boolean case will silently revert to a quoted string")
