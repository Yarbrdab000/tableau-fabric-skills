"""Row-invariant inlining of a referenced dimension calc that has no model column.

``_Parser._try_inline_calc`` is reached only when a bare ``[field]`` in a MEASURE resolves to
NO model column -- i.e. the referenced dimension calc was left a stub by the calculated-column
path. Historically the hook accepted only a pure ``bool`` body, so a *parameter-only* dimension
calc such as ``MAKEDATE([Parameters].[Year Parameter], [Parameters].[Parameter 1], 1)`` -- which
the column path refuses to emit ON PURPOSE, because ``SELECTEDVALUE`` would freeze at its default
in refresh-time row context -- blocked every measure that compared a date column against it.

The widened rule: a non-boolean body may be inlined iff it is ROW-INVARIANT (the sub-parse
registered no physical table), because a value constant across every row is indistinguishable
from a column holding that constant. A row-VARIANT non-boolean body still fails closed.

These tests pin BOTH directions: the newly-admitted row-invariant shapes translate, and every
previously-rejected row-variant / malformed / cyclic shape still stubs.
"""
import pytest

from calc_to_dax import translate_tableau_calc_to_dax


_TABLE = "Flights"

# caption -> (table, clean_col, tmdl_type)
_FIELDS = {
    "date": (_TABLE, "date", "dateTime"),
    "airline_name": (_TABLE, "airline_name", "string"),
    "distance_km": (_TABLE, "distance_km", "double"),
    "flight_id": (_TABLE, "flight_id", "string"),
    "seats": (_TABLE, "seats", "int64"),
    # A SECOND table, so a cross-table inline body can be exercised.
    "country": ("Airports", "country", "string"),
}

# Tableau parameter name -> (DAX measure ref, comparison dtype), as emit_value_parameters builds it.
_PARAMS = {
    "year parameter": ("[Year Parameter Value]", "number"),
    "parameter 1": ("[Month Parameter Value]", "number"),
    "airline name parameter": ("[Airline Name Parameter Value]", "text"),
}


def _resolver(caption):
    return _FIELDS.get((caption or "").strip().lower())


def _param_resolver(name):
    return _PARAMS.get((name or "").strip().lower())


def _tr(formula, inline_calcs=None, param_resolver=_param_resolver):
    """Translate in MEASURE mode. Returns (dax, status)."""
    dax, why, _ = translate_tableau_calc_to_dax(
        formula, _resolver, param_resolver=param_resolver,
        known_tables={_TABLE, "Airports"}, inline_calcs=inline_calcs or {})
    return dax, why


def _tr_tables(formula, inline_calcs=None):
    """Same, but returns the TABLES the translation registered."""
    _, _, tables = translate_tableau_calc_to_dax(
        formula, _resolver, param_resolver=_param_resolver,
        known_tables={_TABLE, "Airports"}, inline_calcs=inline_calcs or {})
    return tables


# The real-world shape: a parameter-only date anchor referenced by its Tableau INTERNAL name.
_ANCHOR_KEY = "calculation_2324420414664089602"
_ANCHOR = {_ANCHOR_KEY: "MAKEDATE([Parameters].[Year Parameter],[Parameters].[Parameter 1],1)"}
_CONSUMER = ("COUNT(IF DATETRUNC('month',[date]) = [Calculation_2324420414664089602] "
             "AND [airline_name] = [Parameters].[Airline Name Parameter] THEN [flight_id] END)")


class TestRowInvariantAdmitted:
    """Bodies that are constant per row now inline instead of stubbing."""

    def test_parameter_only_date_anchor_unblocks_consuming_measure(self):
        dax, why = _tr(_CONSUMER, _ANCHOR)
        assert why == "ok"
        assert dax is not None

    def test_inlined_anchor_keeps_selectedvalue_measures_so_it_tracks_the_slicer(self):
        # The whole point: the parameter must stay LIVE. If the anchor were frozen into a
        # calculated column these refs would be absent and the KPI would pin to the default.
        dax, _ = _tr(_CONSUMER, _ANCHOR)
        assert "[Year Parameter Value]" in dax
        assert "[Month Parameter Value]" in dax

    def test_anchor_body_is_translated_not_pasted_verbatim(self):
        dax, _ = _tr(_CONSUMER, _ANCHOR)
        assert "DATE([Year Parameter Value], [Month Parameter Value], 1)" in dax
        assert "MAKEDATE" not in dax

    def test_row_invariant_number_body_inlines(self):
        dax, why = _tr("SUM(IF [seats] > [Cutoff] THEN [seats] END)",
                       {"cutoff": "[Parameters].[Year Parameter] * 0"})
        assert why == "ok"
        assert "[Year Parameter Value]" in dax

    def test_row_invariant_text_body_inlines(self):
        dax, why = _tr("COUNT(IF [airline_name] = [Chosen] THEN [flight_id] END)",
                       {"chosen": "[Parameters].[Airline Name Parameter]"})
        assert why == "ok"
        assert "[Airline Name Parameter Value]" in dax

    def test_constant_literal_body_inlines(self):
        # No parameter at all -- a literal is trivially row-invariant.
        dax, why = _tr("SUM(IF [seats] > [Floor Seats] THEN [seats] END)",
                       {"floor seats": "100"})
        assert why == "ok"
        assert dax is not None

    def test_row_invariant_inline_does_not_add_a_foreign_home_table(self):
        # A row-invariant body registers no table, so the consumer stays single-home and the
        # aggregate binds to the fact table rather than tripping a cross-table guard.
        dax, why = _tr(_CONSUMER, _ANCHOR)
        assert why == "ok"
        assert "'%s'" % _TABLE in dax


class TestPreExistingBehaviourPreserved:
    """The boolean path and every fail-closed rejection are unchanged."""

    def test_pure_boolean_body_still_inlines(self):
        dax, why = _tr("COUNT(IF [In Window] THEN [flight_id] END)",
                       {"in window": "[date] >= #2024-01-01#"})
        assert why == "ok"
        assert dax is not None

    def test_row_variant_non_boolean_body_still_fails_closed(self):
        # [distance_km] * 2 is a real row-level column expression, NOT constant per row.
        # Inlining it under an aggregate is unproven, so it must still stub.
        dax, why = _tr("SUM(IF [seats] > [Doubled Distance] THEN [seats] END)",
                       {"doubled distance": "[distance_km] * 2"})
        assert dax is None
        assert "Doubled Distance" in why or "unresolved" in why.lower()

    def test_missing_inline_entry_still_raises(self):
        dax, why = _tr("SUM(IF [seats] > [Nowhere] THEN [seats] END)", {})
        assert dax is None

    def test_unparseable_body_fails_closed(self):
        dax, why = _tr("SUM(IF [seats] > [Broken] THEN [seats] END)",
                       {"broken": "1 +"})
        assert dax is None

    def test_trailing_tokens_body_fails_closed(self):
        # Two expressions juxtaposed: not a single clean expression.
        dax, why = _tr("SUM(IF [seats] > [Junk] THEN [seats] END)",
                       {"junk": "1 2"})
        assert dax is None

    def test_self_referential_body_fails_closed_without_recursing(self):
        dax, why = _tr("SUM(IF [seats] > [Loop] THEN [seats] END)",
                       {"loop": "[Loop] + 1"})
        assert dax is None

    def test_mutually_recursive_bodies_fail_closed(self):
        dax, why = _tr("SUM(IF [seats] > [A] THEN [seats] END)",
                       {"a": "[B] + 1", "b": "[A] + 1"})
        assert dax is None

    def test_parameter_body_without_param_resolver_still_stubs(self):
        # No param_resolver -> the anchor body cannot translate at all, so the consumer stubs.
        dax, why = _tr(_CONSUMER, _ANCHOR, param_resolver=None)
        assert dax is None

    def test_real_model_column_wins_over_the_inline_hook(self):
        # The hook is only consulted when the resolver misses. A caption that IS a real column
        # must bind to that column even when an inline body of the same name exists.
        dax, why = _tr("SUM([distance_km])", {"distance_km": "999999"})
        assert why == "ok"
        assert "'%s'[distance_km]" % _TABLE in dax
        assert "999999" not in dax

    def test_inline_key_lookup_is_case_and_whitespace_insensitive(self):
        # Tableau captions arrive in mixed case; the map is normalized lowercase.
        dax, why = _tr("COUNT(IF [  AiRlInE nAmE pArAm  ] = [airline_name] THEN [flight_id] END)",
                       {"airline name param": "[Parameters].[Airline Name Parameter]"})
        assert why == "ok"
        assert "[Airline Name Parameter Value]" in dax


class TestSubParseTablesReachTheCaller:
    """The sub-parse's tables must be merged back, or cross-table guards go blind.

    A boolean body that touches a FOREIGN table is still inlined (that is the historical
    contract), and it is the merged-back table set that then trips the consumer's single-table
    guard. If the merge were dropped, the consumer would look single-table and emit DAX that
    silently spans two unrelated tables.
    """

    _CROSS = {"in region": "[country] = 'US'"}

    def test_cross_table_bool_inline_registers_the_foreign_table(self):
        tables = _tr_tables("COUNTD(IF [In Region] THEN [flight_id] END)", self._CROSS)
        assert "Airports" in tables
        assert _TABLE in tables

    def test_cross_table_bool_inline_trips_the_countd_single_table_guard(self):
        dax, why = _tr("COUNTD(IF [In Region] THEN [flight_id] END)", self._CROSS)
        assert dax is None
        assert "one table" in why

    def test_cross_table_bool_inline_trips_the_sum_single_table_guard(self):
        dax, why = _tr("SUM(IF [In Region] THEN [seats] END)", self._CROSS)
        assert dax is None
        assert "one table" in why

    def test_row_invariant_inline_registers_no_table(self):
        # The complement: a parameter-only body must NOT contribute a table, so the consumer
        # stays single-home. This is what makes the anchor safe to inline anywhere.
        tables = _tr_tables("SUM([seats])", {})
        assert tables == {_TABLE}
        with_anchor = _tr_tables(_CONSUMER, _ANCHOR)
        assert with_anchor == {_TABLE}

