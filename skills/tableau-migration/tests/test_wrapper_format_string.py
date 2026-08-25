"""A forwarding wrapper that drops its base's ``formatString`` renders a correct value wrongly.

The inverse of every other check here. Everything else asks whether a value is absent or wrong;
this asks whether a *right* value is being presented as a wrong number.

Measured on corpus workbook 0088:
``EXCLUDE Days Since Goal Created Date Goals Completed by (filtered)`` evaluates to **0.1732** --
verified against the running model -- and renders **``0``** on the card, because it declares no
``formatString`` while the measure it forwards declares ``0.0%``. A reader sees a zero. Every
value-level instrument reports it healthy, *correctly*, because it is healthy; only the
presentation is missing.

Beside the customer defect that motivated 2.306.0, these are opposite halves of one class, and
both read as "zero" to whoever is looking::

    VAR _val = 0  through currency formatting  ->  a WRONG value formatted CONFIDENTLY
    0.1732 with no format at all               ->  a RIGHT value formatted into a WRONG one

Scoped to FORWARDING wrappers, which is what makes the rule safe rather than a matter of taste:
``CALCULATE([X], <filters>)`` returns the same kind of number as ``X``, so inheriting its format is
the only correct answer. A measure that COMPUTES its value owns its own format and is never
flagged.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import openability_gate as G  # noqa: E402


def _model(measures):
    """``measures`` is ``(name, expression, formatString-or-None)``."""
    body = ["table _Measures", ""]
    for name, expr, fmt in measures:
        body.append("\tmeasure '%s' = %s" % (name, expr))
        if fmt:
            body.append("\t\tformatString: %s" % fmt)
        body.append("\t\tlineageTag: 00000000-0000-0000-0000-000000000000")
        body.append("")
    body += ["\tcolumn 'Value'", "\t\tdataType: int64", "\t\tsourceColumn: Value", ""]
    return {"definition/tables/_Measures.tmdl": "\n".join(body)}


def _flags(parts):
    return [i for i in G.check_model_openability(parts)["issues"]
            if i["check"] == "wrapper_keeps_base_format_string"]


def test_a_forwarding_wrapper_that_drops_a_percent_format_is_flagged():
    """The measured defect: 0.1732 rendering as ``0``."""
    parts = _model([
        ("% Goals Completed by Case Plan", "AVERAGEX('CP', [Ratio])", "0.0%"),
        ("EXCLUDE Goals Completed (filtered)",
         "CALCULATE([% Goals Completed by Case Plan], REMOVEFILTERS('G'[Bin]))", None),
    ])
    flags = _flags(parts)
    assert len(flags) == 1, flags
    assert "EXCLUDE Goals Completed (filtered)" in flags[0]["detail"]
    assert "0.0%" in flags[0]["detail"]


def test_a_wrapper_that_INHERITED_the_format_stays_clean():
    """The negative control taken straight from the corpus.

    Propagation is not broken in general -- ``Clients per Staff (filtered)`` carries
    ``#,##0;-#,##0`` correctly. Only the EXCLUDE/LOD path drops it, so an identical wrapper shape
    that kept its format must not be flagged.
    """
    parts = _model([
        ("Clients per Staff", "DIVIDE([A], [B])", "#,##0;-#,##0"),
        ("Clients per Staff (filtered)",
         "CALCULATE([Clients per Staff], FILTER('Case', 'Case'[C] >= [S]))", "#,##0;-#,##0"),
    ])
    assert _flags(parts) == []


def test_a_wrapper_declaring_a_DIFFERENT_format_is_not_flagged():
    """An explicit override is a decision, not a loss. Only absence is a defect."""
    parts = _model([
        ("Base", "SUM('T'[X])", "0.0%"),
        ("Wrapped", "CALCULATE([Base], REMOVEFILTERS('G'[Bin]))", "#,##0"),
    ])
    assert _flags(parts) == []


def test_a_measure_that_COMPUTES_its_value_owns_its_own_format():
    """``DIVIDE([Count], [Total])`` legitimately formats as a percent though its inputs are counts.

    This is the whole reason the check is scoped to forwarding. A computing measure changes the
    KIND of number, so inheriting an input's format would be wrong, and demanding one would fire
    on most of the model.
    """
    parts = _model([
        ("Count of Goals", "DISTINCTCOUNT('G'[Id])", "#,##0"),
        ("Count of Completed", "CALCULATE(DISTINCTCOUNT('G'[Id]), 'G'[S] = \"C\")", "#,##0"),
        ("Ratio", "DIVIDE([Count of Completed], [Count of Goals])", None),
    ])
    assert _flags(parts) == []


def test_nothing_is_flagged_when_the_base_has_no_format_either():
    """Nothing was lost, so there is nothing to report. Absence of a format is not itself a defect."""
    parts = _model([("Base", "SUM('T'[X])", None),
                    ("Wrapped", "CALCULATE([Base], REMOVEFILTERS('G'[B]))", None)])
    assert _flags(parts) == []


def test_the_format_is_inherited_through_a_nested_wrapper_chain():
    """The measured instance is two wrappers deep -- an LOD wrapper inside a date wrapper.

    Reporting only the outermost would leave the intermediate rendering wrongly too, and the
    intermediate is what an author would open first.
    """
    parts = _model([
        ("Root", "AVERAGEX('CP', [R])", "0.0%"),
        ("Mid", "CALCULATE([Root], REMOVEFILTERS('G'[Bin]))", None),
        ("Outer", "CALCULATE([Mid], FILTER('Case', 'Case'[C] >= [S]))", None),
    ])
    got = sorted(i["detail"].split("'")[1] for i in _flags(parts))
    assert got == ["Mid", "Outer"], got
    assert all("0.0%" in i["detail"] for i in _flags(parts))


def test_a_bare_alias_forwards_the_format_requirement_too():
    """``Alias = [Base]`` forwards the value with no CALCULATE at all, so it forwards the format."""
    parts = _model([("Base", "SUM('T'[X])", "0.00%"), ("Alias", "[Base]", None)])
    flags = _flags(parts)
    assert len(flags) == 1 and "Alias" in flags[0]["detail"], flags


def test_a_reference_cycle_terminates():
    parts = _model([("A", "CALCULATE([B])", None), ("B", "CALCULATE([A])", None)])
    assert _flags(parts) == []


def test_a_blank_stub_wrapper_is_not_reported_here_as_well():
    """A wrapper over a stub is 2.308.0's business, and must not be double-reported.

    ``BLANK()`` stubs carry no formatString, so the base-has-no-format guard already excludes them
    -- but it is worth pinning, because one defect reported by two checks reads as two defects and
    sends an author looking for a second cause that does not exist.
    """
    parts = _model([("Stub", "BLANK()", None),
                    ("Stub (filtered)", "CALCULATE([Stub], FILTER('Case', 'Case'[C] >= [S]))", None)])
    assert _flags(parts) == []


def test_losing_a_NON_percent_format_is_deliberately_not_flagged():
    """An integer or currency format lost is a fidelity loss, not a wrong number.

    Narrowed on measurement, not taste. The unnarrowed rule fires 5 times on the corpus: 2 percent
    (the verified defect and its intermediate) and 3 forwarding an integer ``0`` format, where 7
    still reads as 7. This check's value is that every hit is a wrong number on screen -- failing a
    build for a lost thousands separator would dilute that and invite allowlisting.

    Recorded rather than silently dropped: those 3 remain a real, measured, lesser class.
    """
    parts = _model([
        ("Service Goal Value", "SELECTEDVALUE('P'[V], 0)", "0"),
        ("Service Goal", "CALCULATE([Service Goal Value], REMOVEFILTERS('G'[X]))", None),
        ("Money", "SUM('T'[Amt])", "\\$#,##0.00"),
        ("Money (filtered)", "CALCULATE([Money], REMOVEFILTERS('G'[X]))", None),
    ])
    assert _flags(parts) == []


def test_a_percent_format_is_flagged_whatever_its_exact_pattern():
    """``0.0%``, ``0.00%`` and ``#,##0.0 %`` all scale the displayed number by 100."""
    for pattern in ("0.0%", "0.00%", "#,##0.0 %", "0%;-0%"):
        parts = _model([("B", "AVERAGEX('T', [R])", pattern),
                        ("W", "CALCULATE([B], REMOVEFILTERS('G'[X]))", None)])
        assert len(_flags(parts)) == 1, (pattern, _flags(parts))


def test_the_check_appears_in_the_checks_map_and_gates_the_verdict():
    clean = G.check_model_openability(_model([("X", "SUM('T'[A])", "#,##0")]))
    assert clean["checks"]["wrapper_keeps_base_format_string"] is True

    dirty = G.check_model_openability(_model([
        ("B", "SUM('T'[A])", "0.0%"),
        ("W", "CALCULATE([B], REMOVEFILTERS('G'[X]))", None)]))
    assert dirty["checks"]["wrapper_keeps_base_format_string"] is False
    assert dirty["ok"] is False


def test_a_formatString_inside_an_annotation_is_not_read_as_the_measures_format():
    """The population trap, one axis over.

    A preserved Tableau annotation can legitimately contain the text ``formatString:``. Reading it
    as the measure's own format would make a genuinely unformatted wrapper look formatted -- the
    check would then report silence on exactly the defect it exists to find.
    """
    part = "\n".join([
        "table _Measures",
        "",
        "\tmeasure 'Base' = SUM('T'[A])",
        "\t\tformatString: 0.0%",
        "",
        "\tmeasure 'Wrapped' = CALCULATE([Base], REMOVEFILTERS('G'[X]))",
        "\t\tannotation TableauFormula = formatString: 0.0%",
        "",
    ])
    flags = _flags({"definition/tables/_Measures.tmdl": part})
    assert len(flags) == 1 and "Wrapped" in flags[0]["detail"], flags
