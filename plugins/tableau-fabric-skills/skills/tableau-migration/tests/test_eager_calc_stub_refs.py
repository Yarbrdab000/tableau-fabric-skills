"""An eagerly-evaluated calculated table must not reference a stub column (#134).

A connector with no translation emits an honest placeholder partition —
`#table(type table [], {})` plus a `TODO`. That is fine on its own: the table declares its schema,
the rest of the model builds, and the operator completes the M by hand.

What is not fine is the **generated Date calendar** folding `MIN`/`MAX` over literal `'Table'[Column]`
references that land in one. A calculated table is evaluated **eagerly at model LOAD**, so unlike an
Import column — which errors on refresh — this fails the moment the file is opened in Desktop, before
any refresh and before any credential is needed:

    Column 'X' in table 'Y' cannot be found or may not be used in this expression.

Reported at 11 of ~44 field models, and invisible to the definition-of-done report.

## Three gates that are not the same thing

The reporter's framing, and the reason this needed a new check rather than a change to an old one:

    1. deserializes  -- TmdlSerializer / powerbi-report-author validate. Shape only.
    2. OPENS         -- eager evaluation of calculated tables.            <- this defect
    3. refreshes     -- M execution and column binding.

Structural validation (1) passes. Refresh checks (3) never run, because the file will not open. Only
(2) catches it, and nothing automated covered (2) for this pattern.

## Two fixes, because prevention and detection are different jobs

**Prevention** — the calendar span now skips stub-backed tables (`_stub_backed_tables`). This is right
even where the reference happens to resolve, which is the case the reporter measured locally: their
stubs *did* declare columns, so all 48 eager references bound cleanly and nothing failed. A stub holds
no rows, so its `MIN`/`MAX` is BLANK and it can never contribute a bound — it is pure exposure with no
benefit. Dropping it narrows the span; if that empties the span, the existing `CALENDARAUTO()` fallback
takes over.

**Detection** — `check_model_openability` gains `eager_calc_refs_resolve`, so the class is named rather
than merely avoided in the one generator known to hit it. `CALENDARAUTO()` names no columns and cannot
trip it, which is why the check keys on literal references rather than on the presence of a calendar.

Note the engine's avoidance of `CALENDARAUTO()` is deliberate and stays: it scans every dateTime column
model-wide, so one birthdate drags the calendar back decades (measured: a 1941 calendar for 2017+ data).
"So just use CALENDARAUTO" is not the fix.
"""
import openability_gate as G
import assemble_model as A


_STUB = (
    "table Stub\n"
    "\tcolumn 'When'\n"
    "\t\tdataType: dateTime\n"
    "\n"
    "\tpartition Stub = m\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    "\t\t\t\tSource = #table(type table [], {})\n"
    "\t\t\tin\n"
    "\t\t\t\tSource\n")

_STUB_UNDECLARED = (
    "table Stub\n"
    "\n"
    "\tpartition Stub = m\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    "\t\t\t\tSource = #table(type table [], {})\n"
    "\t\t\tin\n"
    "\t\t\t\tSource\n")

_REAL = (
    "table Orders\n"
    "\tcolumn 'When'\n"
    "\t\tdataType: dateTime\n"
    "\n"
    "\tpartition Orders = m\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    "\t\t\t\tSource = Sql.Database(\"s\", \"d\")\n"
    "\t\t\tin\n"
    "\t\t\t\tSource\n")


def _calendar(over):
    return (
        "table Date\n"
        "\tcolumn 'Date'\n"
        "\t\tdataType: dateTime\n"
        "\n"
        "\tpartition Date = calculated\n"
        "\t\tmode: import\n"
        "\t\tsource = CALENDAR(DATE(YEAR(MIN('%s'[When])), 1, 1), "
        "DATE(YEAR(MAX('%s'[When])), 12, 31))\n" % (over, over))


_CALENDARAUTO = (
    "table Date\n"
    "\tcolumn 'Date'\n"
    "\t\tdataType: dateTime\n"
    "\n"
    "\tpartition Date = calculated\n"
    "\t\tmode: import\n"
    "\t\tsource = CALENDARAUTO()\n")


def test_a_calendar_over_an_UNDECLARED_stub_column_is_caught():
    """The whole defect: this file does not open, and nothing else reports it.

    The stub declares no columns, so the eager reference cannot resolve.
    """
    r = G.check_model_openability({"definition/tables/Stub.tmdl": _STUB_UNDECLARED,
                                   "definition/tables/Date.tmdl": _calendar("Stub")})
    assert r["ok"] is False
    assert r["checks"]["eager_calc_refs_resolve"] is False
    issue = next(i for i in r["issues"] if i["check"] == "eager_calc_refs_resolve")
    assert "OPENED" in issue["detail"]
    assert "cannot be found" in issue["detail"]


def test_a_calendar_over_a_DECLARED_stub_column_opens_and_is_not_flagged():
    """The condition the reporter asked about, settled by cold-opening a real corpus model.

    `0083_previous_workday`'s Date calendar spans a `textscan` stub that DECLARES 6 columns. Opened
    in Desktop it does not fail — it opens degraded, banners reading "One or more calculated objects
    need to be manually refreshed" and "Some of the tables have incomplete or no data".

    So a declared column resolves and the check must stay quiet, or it fires on every
    degraded-but-openable model — a different and already-reported condition. The first version of
    this check did exactly that and failed the 29-workbook corpus, which is how this was found.
    """
    r = G.check_model_openability({"definition/tables/Stub.tmdl": _STUB,
                                   "definition/tables/Date.tmdl": _calendar("Stub")})
    assert r["ok"] is True
    assert r["checks"]["eager_calc_refs_resolve"] is True


def test_a_calendar_over_a_real_table_passes():
    r = G.check_model_openability({"definition/tables/Orders.tmdl": _REAL,
                                   "definition/tables/Date.tmdl": _calendar("Orders")})
    assert r["ok"] is True


def test_calendarauto_beside_a_stub_passes():
    """CALENDARAUTO names no columns, so it cannot trip this -- the check must not over-fire.

    Uses the UNDECLARED stub, so the test would fail if the check keyed on the stub's presence
    rather than on an unresolvable reference.
    """
    r = G.check_model_openability({"definition/tables/Stub.tmdl": _STUB_UNDECLARED,
                                   "definition/tables/Date.tmdl": _CALENDARAUTO})
    assert r["ok"] is True
    assert r["checks"]["eager_calc_refs_resolve"] is True


def test_a_stub_nobody_references_is_not_flagged():
    """The reporter's own 3 field models that opened fine: stub present, nothing eager points at it."""
    r = G.check_model_openability({"definition/tables/Stub.tmdl": _STUB_UNDECLARED,
                                   "definition/tables/Orders.tmdl": _REAL})
    assert r["ok"] is True


def test_a_model_with_no_stub_skips_the_check_entirely():
    """Cost-free on the overwhelming majority of models, and byte-identical in its verdict."""
    r = G.check_model_openability({"definition/tables/Orders.tmdl": _REAL})
    assert r["ok"] is True
    assert "eager_calc_refs_resolve" not in r["checks"]


def test_an_m_partition_referencing_a_stub_is_not_eager():
    """Only CALCULATED tables evaluate at load; an M partition fails at refresh, not on open."""
    m_ref = _REAL.replace("Sql.Database(\"s\", \"d\")", "Sql.Database(\"s\", \"d\") // 'Stub'[When]")
    r = G.check_model_openability({"definition/tables/Stub.tmdl": _STUB,
                                   "definition/tables/Orders.tmdl": m_ref})
    assert r["ok"] is True


# -- prevention side: the calendar span must not reach into a stub ---------------------------------

def test_a_stub_relation_is_identified_from_its_descriptor():
    tables = [
        {"kind": "table", "name": "Real", "columns": [{"remote_name": "When"}]},
        {"kind": "table", "name": "Stubby", "columns": []},
        {"kind": "table", "name": "Flagged", "columns": [{"remote_name": "X"}],
         "stub_partition": True},
    ]
    stubs = A._stub_backed_tables(tables)
    assert stubs == {"Stubby", "Flagged"}


def test_stub_detection_is_fail_safe():
    """Anything unrecognisable is simply not called a stub, so today's behaviour is kept."""
    assert A._stub_backed_tables(None) == set()
    assert A._stub_backed_tables([]) == set()
    assert A._stub_backed_tables(["not-a-dict", 7]) == set()
    # a container/marker relation is not a table and never a stub
    assert A._stub_backed_tables([{"kind": "join", "name": "J"}]) == set()
