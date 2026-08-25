"""A bare `[Name]` that is a COLUMN, not a measure — the shape that ships broken and reads fine.

Power BI accepts an unqualified column reference inside a ROW CONTEXT and rejects it everywhere
else. `openability_gate` used to resolve a bare `[Name]` against measures OR columns and pass either
— defensible in the abstract, and it let two confirmed defects through.

**A customer deliverable.** `Metric Calc Display` emitted:

    VAR _val = 0 RETURN SWITCH([display_format], "$2", "$" & TEXT(_val, "0.00"), ...)

`display_format` is a column on 'Custom SQL Query'. Power BI: **SemanticError**, "The syntax for '('
is incorrect". Five measures broken — three primary, two cascaded — in a model whose annotation read
`TranslatedBy: assisted translation (human-approved)`. The approval had certified *the formula looks
right*, not *the measure compiles*.

**This repo's own corpus, 0088.** `Client per Staff Max Goal (filtered)` emitted:

    CALCULATE([Client per Staff Max Goal], FILTER('Case', ... [Start Date Value] ...))

Only the column and the what-if measure `Client per Staff Max Goal Value` exist. Power BI: *"The
value for 'Client per Staff Max Goal' cannot be determined. Either the column doesn't exist, or
there is no current row for this column."* The same expression gets `[Start Date Value]` right, so
it is a dropped suffix rather than a convention the author chose.

That second one was found by treating a single "false positive" as a lead instead of allowlisting
it. Across 248 corpus measures exactly one bare reference resolved to a column and not a measure —
and it was a genuine break. The row-context allowance was covering nothing real while hiding two.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import openability_gate as G  # noqa: E402


def _model(*measures, column="display_format", coltype="string"):
    body = ["table T",
            "\tcolumn %s" % column,
            "\t\tdataType: %s" % coltype,
            "\t\tsourceColumn: \"%s\"" % column,
            ""]
    body += ["\tmeasure %s" % m for m in measures]
    return {"definition/tables/T.tmdl": "\n".join(body) + "\n"}


def _flags(parts):
    v = G.check_model_openability(parts)
    return v, [i for i in v["issues"] if i["check"] == "bare_column_references_qualified"]


# --- the two measured defects ------------------------------------------------------------


def test_the_customer_defect_is_caught():
    """`SWITCH([display_format], ...)` where display_format is a column."""
    v, f = _flags(_model('Bad = SWITCH([display_format], "$2", 1, 2)'))
    assert v["checks"]["bare_column_references_qualified"] is False
    assert v["ok"] is False
    assert "display_format" in f[0]["detail"]
    assert "COLUMN, not a measure" in f[0]["detail"]


def test_the_corpus_defect_shape_is_caught():
    """`CALCULATE([X], ...)` where only the column X and the measure `X Value` exist -- a dropped
    what-if suffix, which is how 0088 shipped a broken measure."""
    parts = _model("'X Value' = SELECTEDVALUE('T'[X], 7)",
                   "'X (filtered)' = CALCULATE([X], FILTER('T', 'T'[X] > 0))",
                   column="X", coltype="int64")
    v, f = _flags(parts)
    assert v["checks"]["bare_column_references_qualified"] is False
    assert any("[X]" in i["detail"] for i in f)


# --- the negatives that stop it failing good models --------------------------------------


def test_a_bare_reference_to_a_real_measure_is_fine():
    v, f = _flags(_model("M = SUM('T'[display_format])", "N = [M] * 2"))
    assert v["checks"]["bare_column_references_qualified"] is True
    assert f == []


def test_a_qualified_column_reference_is_fine():
    v, f = _flags(_model("Good = SUM('T'[display_format])"))
    assert v["checks"]["bare_column_references_qualified"] is True


def test_an_unknown_name_stays_the_OTHER_check_s_business():
    """A name that is neither measure nor column is unresolvable, not merely unqualified.

    Kept as `dax_references_resolve` deliberately: the two failures need different sentences. This
    one means "the object does not exist"; the new one means "the object exists and you referenced
    it in a position that forbids it". Reporting both as unresolvable would send a reader hunting a
    missing column that is right there.
    """
    v, f = _flags(_model("Bad = [not_a_thing] + 1"))
    assert f == [], "an unknown name is not a bare-column violation"
    assert v["checks"]["dax_references_resolve"] is False


def test_a_preserved_tableau_formula_is_never_scanned():
    """`annotation TableauFormula` legitimately holds Tableau's own bare refs. Scanning it was a
    real error in the probe that led here -- it reported 370 violations across 248 known-good
    corpus measures, which would have condemned the whole check."""
    parts = {"definition/tables/T.tmdl":
             "table T\n"
             "\tcolumn Sales\n"
             "\t\tdataType: double\n"
             "\t\tsourceColumn: \"Sales\"\n"
             "\n"
             "\tmeasure Ratio = DIVIDE(SUM('T'[Sales]), 2)\n"
             "\t\tannotation TableauFormula = SUM([Sales])/SUM([Profit])\n"}
    v, f = _flags(parts)
    assert f == [], "the preserved Tableau formula must not be treated as DAX"
    assert v["checks"]["dax_references_resolve"] is True


def test_a_model_with_no_measures_passes():
    v, f = _flags(_model())
    assert v["checks"]["bare_column_references_qualified"] is True
