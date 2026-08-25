"""A function name that does not exist in DAX -- the defect that broke a real customer deliverable.

Nothing else in this repo validates a function NAME. Every other check resolves *references*
(``[Measure]``, ``'Table'[Column]``), and a bad function name is neither, so all nine openability
checks passed and the model failed at the customer's Desktop.

**All five** objects in that file with a direct syntax error called ``TEXT(``, which is Excel. DAX
is ``FORMAT``. (Two further measures were broken by *cascade* off those five and call nothing
invalid themselves; they clear the moment the five compile — so the file had 7 broken objects and
5 causes.) Power BI reports it as::

    The syntax for '(' is incorrect. (VAR _val = 0 RETURN SWITCH([display_format], ...))

-- a message naming neither the function nor the problem. The agent that authored the file spent
its entire investigation hunting an unresolved Tableau calculation id that was never involved, and
its own explanation carried the defect: *"STR() → easily converts to TEXT()"*. It converts to
``FORMAT()``.

Population: **0 across 248 corpus measures** (the deterministic translator cannot emit these)
against **5/5** of the customer file's primary breaks. An assisted-path defect, like the
bare-reference class of 2.306.0, and both landed in objects annotated ``human-approved``.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import openability_gate as G  # noqa: E402


def _model(objs):
    """``objs`` is ``(kind, name, expression)`` where kind is ``measure`` or ``column``."""
    body = ["table _Measures", ""]
    for kind, name, expr in objs:
        body.append("\t%s '%s' = %s" % (kind, name, expr))
        body.append("\t\tlineageTag: 00000000-0000-0000-0000-000000000000")
        body.append("")
    return {"definition/tables/_Measures.tmdl": "\n".join(body)}


def _flags(parts):
    return [i for i in G.check_model_openability(parts)["issues"]
            if i["check"] == "dax_functions_exist"]


def test_the_customer_defect_TEXT_is_caught_and_FORMAT_is_named():
    """The measured defect, and the failure message must name the fix.

    Naming the real cause is most of this check's value: it converts an engine error that says
    ``The syntax for '(' is incorrect`` into a one-line fix.
    """
    parts = _model([("measure", "Metric Calc Display",
                     'VAR _val = [Metric Calc] RETURN TEXT(_val, "0.00")')])
    flags = _flags(parts)
    assert len(flags) == 1, flags
    d = flags[0]["detail"]
    assert "TEXT()" in d and "FORMAT" in d, d
    assert "syntax for '('" in d, d


def test_a_broken_calculated_COLUMN_is_caught_too():
    """Two of the customer's seven broken objects were calculated columns, not measures.

    A measure-only scan would have reported 5/7 and left the reader believing the remaining two had
    a different cause.
    """
    parts = _model([("column", "Current Lag",
                     'TEXT(DATEDIFF(\'T\'[d], TODAY(), DAY), "0") & " days"')])
    flags = _flags(parts)
    assert len(flags) == 1 and "column" in flags[0]["detail"], flags


def test_annotations_are_never_scanned():
    """THE control that had to be measured before writing the check, not after it misfired.

    ``annotation TableauFormula`` preserves the Tableau source, which legitimately contains
    ``TEXT(``, ``IIF(``, ``WINDOW_MAX(``. Scanning expression-plus-annotations yields **77 false
    positives on a clean corpus** -- enough to make the check unreadable on its first run and get it
    switched off.
    """
    part = "\n".join([
        "table _Measures",
        "",
        "\tmeasure 'Live' = FORMAT([X], \"0.00\")",
        "\t\tannotation TableauFormula = STR(WINDOW_MAX(ZN([Sales]))) + IIF(ISNULL([X]),'a','b')",
        "\t\tannotation TranslatedBy = deterministic",
        "",
    ])
    assert _flags({"definition/tables/_Measures.tmdl": part}) == []


def test_valid_DAX_functions_are_never_flagged():
    """The list must contain only names DAX genuinely lacks.

    ``INDEX``, ``WINDOW``, ``OFFSET``, ``RANK`` and ``ROWNUMBER`` are DAX **window functions** and an
    earlier draft of the denylist wrongly included ``INDEX``. A denylist entry that is actually
    valid DAX is the one failure this design cannot tolerate, because every hit is supposed to be
    unarguable.
    """
    parts = _model([
        ("measure", "W", "WINDOW(1, ABS, 0, REL, ORDERBY([D]))"),
        ("measure", "I", "INDEX(1, ORDERBY([D]))"),
        ("measure", "O", "OFFSET(-1, ORDERBY([D]))"),
        ("measure", "R", "RANK(DENSE, ORDERBY([D]))"),
        ("measure", "N", "ROWNUMBER(ORDERBY([D]))"),
        ("measure", "S", "MID(LEFT(TRIM([A]), 3), 1, 2) & SUBSTITUTE([B], \"x\", \"y\")"),
        ("measure", "V", "VALUE([A]) + MEDIAN('T'[C]) + SEARCH(\"a\", [B], 1, 0)"),
        ("measure", "C", "CONCATENATE(UPPER([A]), LOWER([B])) & FORMAT([N], \"0\")"),
    ])
    assert _flags(parts) == []


def test_tableau_table_calc_names_say_a_rename_will_not_fix_them():
    """``WINDOW_SUM`` has no DAX equivalent, and saying "use X instead" would be wrong.

    A suggestion that cannot work is worse than none -- it sends the reader to a rename when the
    expression needs rebuilding.
    """
    parts = _model([("measure", "Cumulative", "RUNNING_SUM(SUM('T'[Amt]))")])
    flags = _flags(parts)
    assert len(flags) == 1, flags
    assert "no equivalent" in flags[0]["detail"], flags[0]["detail"]
    assert "use " not in flags[0]["detail"].split("--")[1], flags[0]["detail"]


def test_each_distinct_function_is_reported_once_per_object():
    """A measure calling the same bad function twice is one defect, not two."""
    parts = _model([("measure", "M", 'TEXT([A], "0") & TEXT([B], "0") & IIF([C], 1, 0)')])
    got = sorted(f["detail"].split("calls ")[1].split("(")[0] for f in _flags(parts))
    assert got == ["IIF", "TEXT"], got


def test_a_measure_NAMED_after_a_table_calc_is_not_mistaken_for_a_call():
    """0088 really defines ``WINDOW_MAX(Avg. Days Participation)*1.2`` as a measure NAME.

    Referencing it as ``[WINDOW_MAX(...)]`` must not read as a call. NOTE: this control is
    **defensive, not validated** -- measured with and without the guard and the corpus returns 0
    either way, because nothing currently references those measures from an expression. It should
    not be cited as evidence the corpus exercised it.
    """
    parts = _model([
        ("measure", "WINDOW_MAX(Avg. Days Participation)*1.2", "BLANK()"),
        ("measure", "Ref", "CALCULATE([WINDOW_MAX(Avg. Days Participation)*1.2], ALL('T'))"),
    ])
    assert _flags(parts) == []


def test_the_check_appears_in_the_checks_map_and_gates_the_verdict():
    clean = G.check_model_openability(_model([("measure", "X", "FORMAT([A], \"0\")")]))
    assert clean["checks"]["dax_functions_exist"] is True

    dirty = G.check_model_openability(_model([("measure", "X", "TEXT([A], \"0\")")]))
    assert dirty["checks"]["dax_functions_exist"] is False
    assert dirty["ok"] is False


def test_matching_is_case_insensitive_and_tolerates_space_before_the_paren():
    """DAX is case-insensitive, and an author may write ``Text (`` or ``text(``."""
    for expr in ('Text([A], "0")', 'text ([A], "0")', 'TEXT  ([A], "0")'):
        parts = _model([("measure", "M", expr)])
        assert len(_flags(parts)) == 1, (expr, _flags(parts))


def test_a_bad_name_embedded_in_a_longer_identifier_is_not_flagged():
    """``MYTEXT(`` and ``Table.TEXT(`` are not calls to ``TEXT``."""
    parts = _model([("measure", "M", 'MYTEXT([A]) + CONTEXTUAL([B])')])
    assert _flags(parts) == []
