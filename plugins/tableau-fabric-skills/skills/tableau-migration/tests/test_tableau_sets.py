"""A Tableau SET becomes a boolean model column, so it stops being an unresolvable reference.

A set is a named, reusable membership test over one dimension, serialised as a ``<group>`` holding
a nested ``<groupfilter>`` tree. It is NOT a ``<column>`` and carries no ``formula``, so
``extract_calcs`` skips it by design and every reference to it fails field resolution.

Measured on the corpus, that is not theoretical:

    0078_top_n_and_other   column Names = BLANK()
      annotation TableauFormula = IF [Set 1] THEN [Customer Name] ELSE ... "Other" END
      fallback_reason        unresolved/ambiguous field [Set 1]

-- in the workbook whose entire subject is that idiom.

WHAT THIS RELEASE DOES AND DOES NOT DO. It emits the set as a boolean calculated column, which
makes it a usable Power BI model object (sliceable, filterable, referenceable in DAX). It does NOT
yet make the CALC TRANSLATOR resolve ``[Set 1]``: that resolver is built from the descriptor's
physical columns, and registering a synthetic column there also feeds the M query that selects from
the source. So ``Names`` above still stubs. Tracked separately rather than claimed here.

SCOPE IS FAIL-CLOSED, deliberately. Only a Top-N set whose ordering expression is a simple
aggregate over a single field and whose count resolves to an integer is emitted. A wrong membership
test is worse than an unresolved one, because it renders a plausible chart.

POPULATION (34-workbook corpus): 65 groups, of which 33 are Tableau's own dashboard
Action/Tooltip/Highlight machinery -- correctly NOT translated, since that is Power BI's native
cross-filtering rather than a model object. Of the 32 authored, 28 are Top-N and only 3 are
referenced anywhere at all, which is why emission is gated on being referenced.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import tmdl_generate as T  # noqa: E402


TOPN_XML = """
<group name='[Set 1]' caption='Top N?'>
  <groupfilter count='[Parameters].[Parameter 3]' end='top' function='end' units='records'>
    <groupfilter direction='DESC' expression='AVG([Sales])' function='order'>
      <groupfilter function='level-members' level='[Customer Name]' />
    </groupfilter>
  </groupfilter>
</group>
"""

PARAMS = [{"caption": "Top N?", "internal_name": "[Parameter 3]",
           "datatype": "integer", "default": "10"}]


def _group(xml=TOPN_XML):
    import xml.etree.ElementTree as ET
    return ET.fromstring(xml)


def _resolve(token):
    """A field resolver in the shape the model builder passes: token -> (table, column)."""
    return {"Customer Name": ("Orders$", "Customer_Name"),
            "Sales": ("Orders$", "Sales")}.get(str(token).strip().strip("[]"))


# ------------------------------------------------------------------------------ parsing

def test_a_top_n_set_parses_with_everything_needed_to_translate_it():
    s = T._parse_set_object(_group())
    assert s["kind"] == "top-n"
    assert s["level"] == "Customer Name"
    assert s["detail"]["expression"] == "AVG([Sales])"
    assert s["detail"]["direction"] == "DESC"
    assert s["detail"]["end"] == "top"
    assert s["detail"]["count"] == "[Parameters].[Parameter 3]"


def test_dashboard_machinery_is_recognised_as_auto():
    """``Action (...)`` / ``Tooltip (...)`` groups are generated for dashboard actions.

    They are Power BI's native cross-filtering, not a model object, so translating them would
    invent a column no author asked for. 33 of 65 groups in the corpus are these.
    """
    for name in ("Action (Region)", "Tooltip (True,False)", "Highlight (Segment)"):
        s = T._parse_set_object(_group(TOPN_XML.replace("[Set 1]", "[%s]" % name)))
        assert s["auto"] is True, name
    assert T._parse_set_object(_group())["auto"] is False


# ---------------------------------------------------------------------------- the count

def test_the_count_resolves_from_a_parameters_current_value():
    assert T._set_count_value("[Parameters].[Parameter 3]", PARAMS) == (10, None)


def test_a_literal_count_resolves():
    assert T._set_count_value("25", PARAMS) == (25, None)


def test_an_unknown_parameter_is_refused_with_a_reason_not_a_default():
    n, why = T._set_count_value("[Parameters].[Nope]", PARAMS)
    assert n is None and "not found" in why


def test_a_non_numeric_count_is_refused():
    n, why = T._set_count_value("[Customer Name]", PARAMS)
    assert n is None and why


# ------------------------------------------------------------------------- the emitted DAX

def test_a_top_n_set_emits_a_boolean_column():
    placement, entry = T.resolve_set_object(T._parse_set_object(_group()), _resolve, PARAMS)
    assert placement is not None, entry
    table, block = placement
    assert table == "Orders$"
    assert "dataType: boolean" in block
    assert "RANKX" in block and "__rank <= 10" in block
    assert "AVERAGE('Orders$'[Sales])" in block
    assert entry["n"] == 10 and entry["end"] == "top" and entry["direction"] == "DESC"


def test_the_rank_uses_an_explicit_value_and_allexcept():
    """Both are load-bearing on a FACT table, which has many rows per member.

    A bare ``RANKX(ALL(T[Col]), <expr>)`` in a calculated column ranks the row's own context rather
    than the member's aggregate, so it would silently produce a wrong membership test -- the exact
    failure mode that is worse than not translating at all.
    """
    _p, _e = T.resolve_set_object(T._parse_set_object(_group()), _resolve, PARAMS)
    block = _p[1]
    assert "ALLEXCEPT('Orders$', 'Orders$'[Customer_Name])" in block
    assert re.search(r"RANKX\(ALL\('Orders\$'\[Customer_Name\]\).*__self, DESC\)", block)


def test_bottom_n_flips_the_order():
    xml = TOPN_XML.replace("end='top'", "end='bottom'")
    _p, e = T.resolve_set_object(T._parse_set_object(_group(xml)), _resolve, PARAMS)
    assert e["end"] == "bottom" and e["direction"] == "ASC"


def test_the_count_source_is_disclosed():
    """A calculated column is evaluated at REFRESH, so it cannot follow an interactive parameter.

    The emitted set is fixed at that parameter's current value, and a reader has to be told rather
    than left to discover that changing the parameter does nothing.
    """
    _p, e = T.resolve_set_object(T._parse_set_object(_group()), _resolve, PARAMS)
    assert e["count_source"].startswith("parameter"), e["count_source"]
    _p2, e2 = T.resolve_set_object(
        T._parse_set_object(_group(TOPN_XML.replace("[Parameters].[Parameter 3]", "7"))),
        _resolve, PARAMS)
    assert e2["count_source"] == "literal" and e2["n"] == 7


# ------------------------------------------------------------------- what it must REFUSE

def test_an_auto_set_is_refused():
    s = T._parse_set_object(_group(TOPN_XML.replace("[Set 1]", "[Action (Region)]")))
    placement, entry = T.resolve_set_object(s, _resolve, PARAMS)
    assert placement is None and "machinery" in entry["reason"]


def test_a_non_top_n_set_is_refused_by_name():
    s = T._parse_set_object(_group())
    s["kind"] = "range"
    placement, entry = T.resolve_set_object(s, _resolve, PARAMS)
    assert placement is None and "range" in entry["reason"]


def test_a_complex_ordering_expression_is_refused_rather_than_guessed():
    xml = TOPN_XML.replace("AVG([Sales])", "SUM([Sales]) - SUM([Profit])")
    placement, entry = T.resolve_set_object(T._parse_set_object(_group(xml)), _resolve, PARAMS)
    assert placement is None and "simple aggregate" in entry["reason"]


def test_an_unresolvable_dimension_is_refused():
    placement, entry = T.resolve_set_object(
        T._parse_set_object(_group()), lambda tok: None, PARAMS)
    assert placement is None and "resolve" in entry["reason"]


def test_a_cross_table_set_is_refused():
    """The dimension and the ordering field must live on one table, or the DAX is wrong."""
    def split(token):
        return {"Customer Name": ("Orders$", "Customer_Name"),
                "Sales": ("Other$", "Sales")}.get(str(token).strip().strip("[]"))
    placement, entry = T.resolve_set_object(T._parse_set_object(_group()), split, PARAMS)
    assert placement is None and "different tables" in entry["reason"]


# ------------------------------------------------------------------------ only USED sets

def test_only_referenced_sets_are_collected():
    """A workbook may define many sets and use none -- 25 of 28 authored Top-N sets in the corpus.

    Emitting an unused boolean column per set would add a column to every model for no fidelity
    gain, so collection is gated on the set being referenced beyond its own definition.
    """
    defined_only = "<datasource><group name='[Unused]'>%s</group></datasource>" % (
        "<groupfilter function='end' count='5' end='top'>"
        "<groupfilter function='order' direction='DESC' expression='SUM([Sales])'>"
        "<groupfilter function='level-members' level='[Customer Name]'/></groupfilter></groupfilter>")
    assert T.parse_model_objects(defined_only)["sets"] == []

    used = defined_only.replace("</datasource>",
                                "<column name='X'><calculation class='tableau' "
                                "formula='IF [Unused] THEN 1 ELSE 0 END'/></column></datasource>")
    names = [s["name"] for s in T.parse_model_objects(used)["sets"]]
    assert names == ["Unused"], names
