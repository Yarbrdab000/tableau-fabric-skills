"""Tests for relationship-declared column aliases.

A Tableau field reference carries its relation's name when the caption would otherwise be ambiguous
-- ``[Region (Custom SQL Query2)]`` beside a plain ``[Region]``. A calc mixing the two spans TWO
tables, and the measure translator correctly refuses a multi-table row expression ("SUM(expr) must
reference exactly one table"). The calc then stubs to ``= BLANK()``, which BINDS normally, so the
visual renders empty and no structural gate can see it.

Ground truth: corpus workbook ``0136_custom_sql_prefix_and_params``, calc ``complex nested``.
Measured through the real translator, same inputs, only the aliased reference differing:

    before -> None, "SUM(expr) must reference exactly one table", tables = 2
    after  -> CALCULATE(SUMX('Custom SQL Query', IF(...)), ALLEXCEPT(...)), tables = 1

THE SUBSTITUTION IS THE AUTHOR'S OWN EQUALITY, NOT A NAME GUESS. The workbook's object-graph
relationship predicate is literally ``[Region] = [Region (Custom SQL Query2)]``, so the two hold the
same value on every row the join produces. That is why the refusals below matter more than the
acceptances: collapsing two captions the workbook has NOT declared equal would silently change the
answer, and a look-alike name is exactly what that would look like.
"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from assemble_model import _apply_relationship_column_aliases  # noqa: E402
from connection_to_m import relationship_column_aliases  # noqa: E402


def _ds(relationship_xml):
    return ET.fromstring(
        "<datasource name='d'><object-graph><relationships>"
        + relationship_xml
        + "</relationships></object-graph></datasource>")


def _rel(a, b, op="="):
    return ("<relationship>"
            "<first-end-point object-id='o1'/><second-end-point object-id='o2'/>"
            "<expression op='%s'><expression op='[%s]'/><expression op='[%s]'/></expression>"
            "</relationship>" % (op, a, b))


# ---- extraction -------------------------------------------------------------

def test_a_declared_equality_yields_the_qualified_to_plain_alias():
    aliases = relationship_column_aliases(_ds(_rel("Region", "Region (Custom SQL Query2)")))
    assert aliases == {"Region (Custom SQL Query2)": "Region"}


def test_operand_order_does_not_matter():
    """Tableau does not pin operand order, so the qualified side may be authored either way."""
    a = relationship_column_aliases(_ds(_rel("Region (Custom SQL Query2)", "Region")))
    assert a == {"Region (Custom SQL Query2)": "Region"}


def test_two_relationships_both_contribute():
    xml = (_rel("Region", "Region (Custom SQL Query2)")
           + _rel("Customer Name", "Customer Name (Custom SQL Query1)"))
    assert relationship_column_aliases(_ds(xml)) == {
        "Region (Custom SQL Query2)": "Region",
        "Customer Name (Custom SQL Query1)": "Customer Name",
    }


def test_two_plain_captions_are_not_an_alias():
    """A join between genuinely different columns. They are equal ACROSS the join, but they are not
    the same field, and collapsing them would rewrite what the author asked for."""
    assert relationship_column_aliases(_ds(_rel("Region", "Territory"))) == {}


def test_two_differently_qualified_captions_are_not_an_alias():
    assert relationship_column_aliases(
        _ds(_rel("Region (A)", "Region (B)"))) == {}


def test_a_non_equality_predicate_yields_nothing():
    """``&lt;`` rather than a literal ``<``: the operator lives in an XML attribute, and a raw ``<``
    is not well-formed there -- which is how Tableau writes it too."""
    assert relationship_column_aliases(_ds(_rel("Region", "Region (X)", op="&lt;"))) == {}


def test_no_object_graph_yields_nothing():
    assert relationship_column_aliases(ET.fromstring("<datasource name='d'/>")) == {}


# ---- application ------------------------------------------------------------

COMPLEX = ("{fixed [Sub-Category]: SUM(if [Parameters].[P1] = [Region (Custom SQL Query2)] "
           "AND [Sub-Category] = [Parameters].[P2] then [Sales] END)}")
ALIASES = {"Region (Custom SQL Query2)": "Region"}


def test_the_real_formula_is_collapsed_to_one_table():
    out = _apply_relationship_column_aliases([{"name": "complex nested", "formula": COMPLEX}], ALIASES)
    assert "[Region (Custom SQL Query2)]" not in out[0]["formula"]
    assert "[Region]" in out[0]["formula"]


def test_the_authored_text_is_preserved_for_annotation():
    """The model should still show what the author WROTE, not what we translated."""
    out = _apply_relationship_column_aliases([{"name": "c", "formula": COMPLEX}], ALIASES)
    assert out[0]["formula_original"] == COMPLEX


def test_an_untouched_calc_is_the_SAME_OBJECT_not_a_copy():
    """Inertness asserted by identity, not by equality.

    A workbook with no such relationship must be byte-for-byte unchanged. Returning equal-but-new
    dicts would satisfy an == check while still being a different object graph downstream, so this
    pins identity.
    """
    calcs = [{"name": "plain", "formula": "SUM([Sales])"}]
    assert _apply_relationship_column_aliases(calcs, ALIASES) is calcs
    assert _apply_relationship_column_aliases(calcs, {}) is calcs
    assert _apply_relationship_column_aliases(calcs, None) is calcs


def test_substitution_is_bracket_delimited_not_a_bare_token_replace():
    """A bare ``Region`` -> ``Region`` replace would corrupt the qualified caption itself, and any
    other caption containing the word."""
    calcs = [{"name": "c", "formula": "[Regional Manager] + [Region (Custom SQL Query2)]"}]
    out = _apply_relationship_column_aliases(calcs, ALIASES)
    assert "[Regional Manager]" in out[0]["formula"]
    assert out[0]["formula"] == "[Regional Manager] + [Region]"


def test_a_longer_caption_is_substituted_before_a_shorter_prefix_of_it():
    """Ordering guard: a shorter alias that is a PREFIX of a longer one must not rewrite it first."""
    aliases = {"Region (X)": "Region", "Region (X) Extended": "Region Extended"}
    calcs = [{"name": "c", "formula": "[Region (X) Extended] + [Region (X)]"}]
    out = _apply_relationship_column_aliases(calcs, aliases)
    assert out[0]["formula"] == "[Region Extended] + [Region]"
