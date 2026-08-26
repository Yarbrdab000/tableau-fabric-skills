"""A DAX reference that resolves but disagrees on CASE is disclosed, never failed (#164).

THE REPORT. A Snowflake estate shipped ~12 measures referencing a custom-SQL column by the SQL's
literal alias text (``Available_Duration``) while the same model's calculated columns and generated
``Date`` table used the Snowflake-folded name (``AVAILABLE_DURATION``). The reporter inferred that a
folding rule "already exists somewhere in the codebase and is applied by at least one emission path"
and asked us to route every emitter through it.

WHAT WE MEASURED, AND WHY THE FIX WOULD HAVE BEEN WRONG.

* **There is no folding rule.** A column's model name comes from Tableau's recorded ``remote-name``
  -- the name the DATABASE itself reported when Tableau profiled the query, already folded by
  Snowflake. Nothing derives it. That is why the behaviour is connector-agnostic by construction and
  why a fix hardcoding "uppercase" would have introduced the exact bug the reporter warned about for
  Postgres (folds lower) and Databricks Unity (lower, case-insensitive).
* **It does not reproduce.** Four synthetic shapes at the real TDS parse path -- both names recorded,
  one recorded, remote unfolded, local folded -- plus 1,284 calc-column and 448 measure references
  across 34 corpus models: **zero** divergences, zero dangling refs.
* **The described defect would not have broken DAX anyway.** Tabular identifiers are case-insensitive,
  which is why ``dax_references_resolve`` compares casefolded and is *right* to. Injecting the exact
  reported shape leaves the model ``ok`` and queryable.

SO WHY DISCLOSE IT AT ALL. Because the engine should never EMIT one. Two emitters can disagree on
case only if one stopped using the recorded name -- a resolver divergence that DAX tolerates and the
M layer, which is case-SENSITIVE, would not (``sourceColumn`` binding fails with *"The column 'X' of
the table wasn't found"*). So this converts an unreproducible field report into a detector that names
the measure and BOTH spellings if it ever happens again.

Deliberately outside ``issues`` and ``checks``: ``ok`` is ``not issues``, and failing a model Power BI
accepts would reject sound output. Present-and-empty on a healthy build, so its absence is never
mistaken for "not evaluated".
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from openability_gate import check_model_openability  # noqa: E402

_TABLE = ("table 'Custom SQL Query'\n"
          "\tcolumn AVAILABLE_DURATION\n\t\tdataType: double\n\t\tsourceColumn: AVAILABLE_DURATION\n"
          "\tpartition 'Custom SQL Query' = m\n\t\tmode: import\n\t\tsource = Source\n")
_MEAS = ("table _Measures\n"
         "\tcolumn _d\n\t\tdataType: int64\n\t\tisHidden\n\t\tsourceColumn: _d\n"
         "\tmeasure 'Avg Avail' = AVERAGE('Custom SQL Query'[%s])\n"
         "\tpartition _Measures = m\n\t\tmode: import\n\t\tsource = Source\n")


def _parts(col):
    return {"definition/database.tmdl": "database Model\n\tcompatibilityLevel: 1606\n",
            "definition/model.tmdl": "model Model\n\tculture: en-US\n",
            "definition/tables/Custom SQL Query.tmdl": _TABLE,
            "definition/tables/_Measures.tmdl": _MEAS % col}


def test_the_engines_own_output_discloses_nothing():
    v = check_model_openability(_parts("AVAILABLE_DURATION"))
    assert v["ok"] is True
    assert v["reference_case_mismatches"] == []


def test_the_key_is_present_even_when_empty():
    """Absence and emptiness must not look the same. A consumer that cannot tell 'clean' from
    'never evaluated' learns nothing from either."""
    assert "reference_case_mismatches" in check_model_openability(_parts("AVAILABLE_DURATION"))


def test_the_reported_shape_is_disclosed_with_both_spellings():
    v = check_model_openability(_parts("Available_Duration"))
    mm = v["reference_case_mismatches"]
    assert len(mm) == 1, mm
    assert mm[0]["measure"] == "Avg Avail"
    assert mm[0]["table"] == "Custom SQL Query"
    # BOTH spellings. Naming only one leaves the reader unable to tell which emitter drifted.
    assert mm[0]["referenced"] == "Available_Duration"
    assert mm[0]["declared"] == "AVAILABLE_DURATION"


def test_a_case_only_difference_does_not_fail_the_model():
    """The load-bearing refusal. Tabular resolves identifiers case-insensitively, so this model
    opens and queries correctly; failing it would reject sound output over a cosmetic difference."""
    good = check_model_openability(_parts("AVAILABLE_DURATION"))
    bad = check_model_openability(_parts("Available_Duration"))
    assert bad["ok"] is good["ok"] is True
    assert bad["checks"]["dax_references_resolve"] is True
    assert not [i for i in bad["issues"] if "case" in str(i).lower()]


def test_a_genuinely_undeclared_column_is_still_a_HARD_failure():
    """Proves the disclosure did not soften the real check. A name that resolves under NO casing is
    a query-time error and must still fail."""
    v = check_model_openability(_parts("NOT_A_COLUMN"))
    assert v["ok"] is False
    assert v["checks"]["dax_references_resolve"] is False
    # ...and it is not double-reported as a case mismatch.
    assert v["reference_case_mismatches"] == []
