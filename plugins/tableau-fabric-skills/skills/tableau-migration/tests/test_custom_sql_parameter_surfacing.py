"""Tests for the Custom SQL parameter classifier (``surfaced_parameter_predicates``).

Tableau lets an author embed a parameter in a Custom SQL relation
(``WHERE `Region` = <[Parameters].[X]>``) because Custom SQL is Tableau's only way to push a
predicate to the source. The token is meaningless to the source engine, so the emitted native query
cannot run at all -- Spark rejects it at parse.

Power BI does not need the token when the filtered column is IN THE RESULT SET: drop the predicate
and let an ordinary slicer on that column filter, which in DirectQuery folds back into a ``WHERE`` at
the source. That is what a Power BI developer actually does, and it avoids Dynamic M Query
Parameters entirely -- a feature that would otherwise drag in no-RLS, no-aggregations,
no-spaces-in-table-or-parameter-names, and a long list of banned slicer operations.

Ground truth: ``workbooks/0136_custom_sql_prefix_and_params`` (corpus), relation 3, built against a
live Databricks warehouse.

THE REFUSALS ARE THE POINT. Accepting a shape we should not rewrite changes which rows come back,
and it does so silently; failing to accept one merely leaves today's honest warning in place. So the
refusal cases outnumber the acceptances here deliberately.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from connection_to_m import surfaced_parameter_predicates  # noqa: E402

P = "<[Parameters].[Parameter 3357119534784517]>"
COLS = [{"remote_name": "Region", "model_name": "Region"},
        {"remote_name": "REGION_SALES", "model_name": "REGION_SALES"}]

# Verbatim from corpus workbook 0136, relation 3.
REAL = ("SELECT `Region`, SUM(`Sales`) AS REGION_SALES\nFROM orders\n"
        "WHERE `Region` = %s\nGROUP BY `Region`" % P)


def _norm(s):
    return " ".join((s or "").split())


def test_the_real_corpus_query_loses_its_predicate_and_names_the_column():
    new, surfaced, unresolved = surfaced_parameter_predicates(REAL, COLS)
    assert _norm(new) == "SELECT `Region`, SUM(`Sales`) AS REGION_SALES FROM orders GROUP BY `Region`"
    assert "[Parameters]" not in new
    assert unresolved == []
    assert len(surfaced) == 1
    # The column the report layer must point a slicer at comes from the SQL itself, not a guess.
    assert surfaced[0]["column"] == "Region"
    assert surfaced[0]["param"] == P


def test_select_star_qualifies_because_the_oracle_is_the_relation_columns():
    """No special case for ``SELECT *``.

    "Is the column in the result set" is answered by the relation's own metadata records -- Tableau's
    account of what the query returns -- not by parsing the projection list. A hand-parsed SELECT
    could disagree with the columns the model actually emits; this cannot.
    """
    new, surfaced, _u = surfaced_parameter_predicates(
        "SELECT * FROM orders WHERE Region = %s" % P, COLS)
    assert _norm(new) == "SELECT * FROM orders"
    assert surfaced[0]["column"] == "Region"


def test_only_the_parameter_conjunct_is_dropped():
    sql = ("SELECT `Region` FROM orders WHERE `Segment` = 'Consumer' AND `Region` = %s "
           "GROUP BY `Region`" % P)
    new, surfaced, _u = surfaced_parameter_predicates(sql, COLS)
    assert "`Segment` = 'Consumer'" in new
    assert "[Parameters]" not in new
    assert len(surfaced) == 1


def test_a_column_absent_from_the_result_set_is_refused():
    """The genuine Dynamic-M case: no model filter can reach a column the query never returns."""
    sql = "SELECT `Region` FROM orders WHERE `Segment` = %s GROUP BY `Region`" % P
    new, surfaced, unresolved = surfaced_parameter_predicates(sql, [{"remote_name": "Region"}])
    assert new == sql
    assert surfaced == []
    assert unresolved == [P]


def test_an_OR_in_the_where_refuses_the_whole_clause():
    """NOTE: this case is refused by the exact-conjunct match, NOT by the OR guard.

    Recording which check does the work matters. Deleting the OR guard leaves this test green, so
    citing it as evidence that the guard works would be wrong. The test below is the one that
    exercises the guard.
    """
    sql = "SELECT * FROM orders WHERE `Region` = %s OR `Segment` = 'Consumer'" % P
    new, surfaced, unresolved = surfaced_parameter_predicates(sql, COLS)
    assert new == sql
    assert surfaced == []
    assert unresolved == [P]


def test_AND_mixed_with_OR_is_refused_because_precedence_makes_the_drop_unsafe():
    """The load-bearing refusal, and the only one that distinguishes the OR guard.

    ``AND`` binds tighter than ``OR``, so ``WHERE A AND B OR C`` means ``(A AND B) OR C``. Splitting
    on ``AND`` yields ``["A", "B OR C"]`` and ``A`` on its own is a clean parameter predicate -- so
    without the guard it is dropped and ``B OR C`` survives. Measured: the query silently becomes
    ``WHERE x = 1 OR y = 2``.

    That is NOT the benign widening that makes the whole Case-A rewrite safe. Dropping a conjunct
    widens the result along the parameter's own column, which a slicer on that column re-narrows
    exactly. Here the surviving ``C`` rows were never constrained by ``Region`` at all, so no slicer
    on ``Region`` can put them back -- the answer is simply different.

    Both operand orders are checked: the parameter predicate can sit before or after the ``OR``.
    """
    for sql in (
        "SELECT * FROM orders WHERE `Region` = %s AND x = 1 OR y = 2" % P,
        "SELECT * FROM orders WHERE y = 2 OR x = 1 AND `Region` = %s" % P,
    ):
        new, surfaced, unresolved = surfaced_parameter_predicates(sql, COLS)
        assert new == sql, sql
        assert surfaced == [], sql
        assert unresolved == [P], sql


def test_a_parameter_inside_a_subquery_is_refused():
    """The predicate's scope is not the outer result set, so the outer column cannot stand in."""
    sql = "SELECT * FROM orders WHERE `Region` IN (SELECT r FROM x WHERE y = %s)" % P
    assert surfaced_parameter_predicates(sql, COLS)[0] == sql


def test_a_non_equality_comparison_is_refused():
    for op in ("<>", ">", "<", ">=", "<=", "LIKE"):
        sql = "SELECT * FROM orders WHERE `Region` %s %s" % (op, P)
        new, surfaced, _u = surfaced_parameter_predicates(sql, COLS)
        assert new == sql, op
        assert surfaced == [], op


def test_a_parameter_outside_a_where_is_refused():
    """A parameter in the projection is not a filter and must not be treated as one."""
    sql = "SELECT `Region`, %s AS P FROM orders" % P
    assert surfaced_parameter_predicates(sql, COLS)[0] == sql


def test_two_where_clauses_are_refused():
    """More than one WHERE means a subquery or a UNION -- the split is ambiguous, so hands off."""
    sql = ("SELECT * FROM (SELECT * FROM orders WHERE x = 1) t WHERE `Region` = %s" % P)
    assert surfaced_parameter_predicates(sql, COLS)[0] == sql


def test_sql_without_a_parameter_is_returned_byte_identical():
    sql = "SELECT * FROM orders WHERE Region = 'West'"
    new, surfaced, unresolved = surfaced_parameter_predicates(sql, COLS)
    assert new == sql and surfaced == [] and unresolved == []


def test_empty_and_missing_input_do_not_raise():
    """A missing query normalizes to the empty string rather than propagating ``None``.

    The callers concatenate this straight into an M ``let`` body, so returning ``None`` would turn a
    merely-absent query into a TypeError at emit time.
    """
    assert surfaced_parameter_predicates("", COLS) == ("", [], [])
    assert surfaced_parameter_predicates(None, None) == ("", [], [])
