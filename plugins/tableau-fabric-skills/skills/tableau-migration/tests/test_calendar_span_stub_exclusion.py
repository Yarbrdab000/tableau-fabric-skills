"""#137 -- the calendar-span stub exclusion must fire on what the engine ACTUALLY emits.

#134 added ``_stub_backed_tables()`` so a fabricated year-2000 calendar is not folded over a
placeholder partition's date columns. The predicate honoured three conditions, but two of them
(``stub_partition``, ``placeholder``) were assigned NOWHERE in the engine -- the only writer was a
hand-set test fixture. In production it therefore collapsed to "exclude a table with zero declared
columns", which is the rarer branch, and the shape #134 was filed about (Tableau's
``<metadata-record>`` supplies typed columns even when the query cannot be translated) was never
excluded at all.

These tests assert through the EMITTER rather than a hand-set flag, so they observe what production
produces. A test that supplies a signal the engine never emits is exactly what let this survive.

The discriminator is the emitted artifact, NOT ``m_partition_review_reason()``. That function is
strictly broader than "this is a stub": a surviving Tableau parameter in Custom SQL is a
needs-review reason on a partition that still emits a real ``Odbc.Query`` / ``Value.NativeQuery``
and DOES carry rows. ``test_a_review_reason_on_a_real_partition_is_not_a_stub`` pins that
distinction, because keying on the reason would drop a POPULATED fact table out of the calendar
span and silently narrow the model's date range.
"""
import re

import pytest

import assemble_model as A
import openability_gate as OG
from connection_to_m import (emit_m_partition_source, m_partition_review_reason)


def _unmapped_custom_sql():
    """A flat-file custom-SQL relation with no resolvable path: Tableau supplies the typed column
    schema (``<metadata-record>``), but the partition can only be emitted as the scaffold.

    This is the #134 shape -- a stub that DECLARES columns -- in a form that still reaches the
    emit loop. A wholly-unmapped connector class is routed to the needs-storage-decision fallback
    by ``select_storage_mode`` and raises before any table is emitted, so it cannot exercise the
    calendar path end to end.
    """
    rel = {"kind": "custom_sql", "name": "Custom SQL Query",
           "sql": "select * from FLIGHTS",
           "columns": [{"remote_name": "DEPARTURE_DATETIME", "local_name": "DEPARTURE_DATETIME",
                        "model_name": "DEPARTURE_DATETIME", "tmdl_type": "dateTime",
                        "local_type": "datetime"},
                       {"remote_name": "ARRIVAL_DATETIME", "local_name": "ARRIVAL_DATETIME",
                        "model_name": "ARRIVAL_DATETIME", "tmdl_type": "dateTime",
                        "local_type": "datetime"}]}
    descriptor = {"connection_class": "excel-direct", "datasource_name": "IA"}
    return rel, descriptor


def _assemble(descriptor):
    """Run the real assembler and normalise its return to ``(parts, ...)``."""
    out = A.assemble_import_model(descriptor, model_name="IA")
    return out if isinstance(out, tuple) else (out,)


def test_the_emitter_and_the_calendar_gate_agree_a_scaffold_is_a_stub():
    """The bug, stated as the disagreement it was: emitter said stub, gate said not-a-stub."""
    rel, descriptor = _unmapped_custom_sql()

    emitted = emit_m_partition_source(rel, descriptor, "import")
    assert A.emitted_partition_is_stub(emitted), (
        "precondition: this relation must emit the scaffold partition")

    # The gate must now agree -- via the stamp the emit loop applies, exercised below end-to-end.
    rel_stamped = dict(rel)
    rel_stamped["stub_partition"] = A.emitted_partition_is_stub(emitted)
    assert A._stub_backed_tables([rel_stamped]) == {"Custom SQL Query"}


def test_a_column_declaring_stub_is_excluded_after_a_real_assemble():
    """End-to-end: assemble the model, then assert the stamp landed AND the calendar dropped it.

    This is the production path -- no hand-set flag anywhere. Before #137 the relation came out of
    ``assemble_import_model`` with no ``stub_partition`` key and ``_stub_backed_tables`` returned an
    empty set despite the table having emitted ``#table(type table [], {})``, so the generated
    calendar still folded MIN/MAX over the stub's date columns.
    """
    rel, descriptor = _unmapped_custom_sql()
    descriptor = dict(descriptor)
    descriptor["relations"] = [rel]

    parts, *_rest = _assemble(descriptor)

    assert rel.get("stub_partition") is True, (
        "the emit loop must stamp a scaffold-backed relation from its EMITTED tmdl")
    assert A._stub_backed_tables([rel]) == {"Custom SQL Query"}

    # The user-visible outcome: no generated calculated table folds a bound over the stub's
    # columns. A stub holds no rows, so such a reference is pure eager-evaluation exposure with
    # no benefit -- which is the whole point of #134.
    date_parts = [t for p, t in parts.items()
                  if p.startswith("definition/tables/") and "= calculated" in t]
    for text in date_parts:
        assert "'Custom SQL Query'[DEPARTURE_DATETIME]" not in text
        assert "'Custom SQL Query'[ARRIVAL_DATETIME]" not in text


def test_a_review_reason_on_a_real_partition_is_not_a_stub():
    """A needs-review reason is NOT the discriminator -- keying on it would be over-broad.

    A surviving Tableau parameter reference makes the partition need review, but the partition is
    still emitted for real and the table still carries rows. Treating it as a stub would drop a
    populated fact table out of the calendar span, narrowing the model's date range for no reason.
    """
    sql = "select * from t where d > <[Parameters].[Start]>"
    cols = [{"remote_name": "D", "local_type": "datetime"}]
    cases = [
        ("generic ODBC", {"kind": "custom_sql", "name": "OdbcT", "sql": sql, "columns": cols},
         {"connection_class": "genericodbc", "datasource_name": "IA", "odbc_dsn": "MYDSN"}),
        ("SQL Server", {"kind": "custom_sql", "name": "SqlT", "sql": sql, "columns": cols},
         {"connection_class": "sqlserver", "datasource_name": "IA",
          "server": "srv", "dbname": "db"}),
    ]
    for label, rel, descriptor in cases:
        assert m_partition_review_reason(rel, descriptor, "import"), (
            "precondition (%s): this relation must carry a review reason" % label)
        emitted = emit_m_partition_source(rel, descriptor, "import")
        assert not A.emitted_partition_is_stub(emitted), (
            "%s: a surviving-parameter partition is REAL, not a scaffold" % label)
        assert A._stub_backed_tables([rel]) == set(), (
            "%s: a real partition must never be excluded from the calendar span" % label)


def test_a_zero_column_stub_is_still_excluded():
    """The pre-existing branch stays covered -- #137 must not trade one gap for another."""
    rel = {"kind": "custom_sql", "name": "Bare", "sql": "select 1", "columns": []}
    assert A._stub_backed_tables([rel]) == {"Bare"}


def test_stub_marker_definition_matches_openability_gate():
    """The prevention gate and the detection gate must share ONE definition of 'stub'.

    ``openability_gate`` (detection, #134) and ``assemble_model`` (prevention, #137) each carry the
    pattern because they already import each other. Drift between them is precisely the class of
    failure #137 was, so pin them against the same strings rather than trusting two copies.
    """
    assert A._STUB_PARTITION_RE.pattern == OG._STUB_PARTITION_RE.pattern

    stub_forms = [
        "Source = #table(type table [], {})",
        "Source = #table( type  table [ ] , { } )",
        "\t\t\t\tSource = #table(type table [], {})\n",
    ]
    real_forms = [
        'Source = Odbc.Query("dsn=MYDSN", "select 1")',
        'Source = Sql.Database(#"Server", #"Database")',
        "Source = #table(type table [A = text], {{1}})",
    ]
    for s in stub_forms:
        assert A._STUB_PARTITION_RE.search(s), s
        assert OG._STUB_PARTITION_RE.search(s), s
    for s in real_forms:
        assert not A._STUB_PARTITION_RE.search(s), s
        assert not OG._STUB_PARTITION_RE.search(s), s
