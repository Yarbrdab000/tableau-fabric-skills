"""A volume hint ADVISES the storage decision and never overrides it (#163).

THE GAP. The engine picks a storage mode from connector class and relation shape and has no concept
of how much data a source holds, so -- in the reporter's words -- *"a 42-million-row table and a
10-thousand-row table get the same treatment and the same guidance."* Their field benchmark, taken
against Snowflake directly rather than through Power BI: **1,020,016 rows in 452 s** (~2,255
rows/sec), on an Import table that received exactly the same treatment as the estate's small
dimension tables.

WHAT SHIPPED, AND THE ONE DESIGN DECISION WORTH ARGUING WITH. The hint closes the *guidance* half
and deliberately leaves the *decision* half alone. Three reasons, in order of weight:

* the hint can be silently stale -- an extract's count is as of its last Tableau refresh, and lags
  wherever the source query has a rolling window. The reporter said this themselves: *"a silently
  stale or silently missing number would drive worse decisions than no number"*;
* flipping the mode would make it non-reproducible from the descriptor alone, so two runs of one
  workbook could emit different models for a reason recorded nowhere in either;
* the seam for "the engine should not decide this" already exists -- ``--storage-decision`` -- and an
  operator can use it having READ the advice. Advice composes with that; a silent flip fights it.

ABSENT IS A NORMAL OUTCOME, not an error: a pure live source has no ``.hyper`` to count, so no hint
is the common case. ``row_count=None`` must return the decision byte-identical to today.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from storage_mode import (  # noqa: E402
    apply_volume_hint, normalize_row_count, select_storage_mode, volume_band,
)

SQLSERVER = {"connection_class": "sqlserver", "server": "s.example.com", "database": "DB",
             "relations": [{"kind": "table", "name": "Orders", "table": "[dbo].[Orders]",
                            "columns": [{"name": "Sales", "datatype": "real"}]}]}


def test_no_hint_leaves_the_decision_byte_identical():
    """The whole additive claim. A pure live source will always take this path."""
    base = select_storage_mode(SQLSERVER)
    assert select_storage_mode(SQLSERVER, row_count=None) == base
    assert "volume" not in base


def test_an_unusable_hint_is_ignored_rather_than_guessed_at():
    for bad in ({}, {"value": None}, {"value": "many"}, {"value": -1}, "42", True, False, []):
        assert normalize_row_count(bad) is None, bad
        assert "volume" not in select_storage_mode(SQLSERVER, row_count=bad), bad


def test_the_agreed_provenance_shape_round_trips():
    got = normalize_row_count({"value": 42104338, "source": "hyper", "as_of": "2026-08-01"})
    assert got == {"value": 42104338, "source": "hyper", "as_of": "2026-08-01"}


def test_a_bare_integer_is_accepted_but_stamped_unknown():
    """Not rejected -- a number is still an order of magnitude -- but never laundered into looking
    provenanced. A count with no origin cannot be told apart from a stale extract count."""
    assert normalize_row_count(42104338) == {"value": 42104338, "source": "unknown"}
    d = select_storage_mode(SQLSERVER, row_count=42104338)
    assert d["volume"]["source"] == "unknown"
    assert any("without provenance" in n for n in d["volume"]["advice"])


def test_an_unrecognised_source_degrades_to_unknown_rather_than_passing_through():
    assert normalize_row_count({"value": 5, "source": "vibes"})["source"] == "unknown"


def test_the_bands():
    assert volume_band(0) == "small"
    assert volume_band(99_999) == "small"
    assert volume_band(100_000) == "moderate"
    assert volume_band(9_999_999) == "moderate"
    assert volume_band(10_000_000) == "large"
    assert volume_band(42_104_338) == "large"


def test_the_mode_is_never_changed_by_a_hint():
    """The load-bearing refusal, asserted at every band so a future 'helpful' auto-flip fails here.

    COVERS BOTH MODES DELIBERATELY. The first version of this test used only the SQLSERVER fixture,
    whose mode is **DirectQuery** -- so the ``band == "large" and mode == "Import"`` branch was
    unreachable from it, and a positive control that injected exactly that auto-flip passed clean.
    A refusal test that cannot reach the branch it refuses is the vacuous-fixture trap: green, and
    about nothing. Every mode the advice discriminates on is now exercised.
    """
    base = select_storage_mode(SQLSERVER)
    for n in (10_000, 1_020_016, 42_104_338):
        d = select_storage_mode(SQLSERVER, row_count={"value": n, "source": "live"})
        assert d["mode"] == base["mode"], n
        assert d["recommended_mode"] == base["recommended_mode"], n
        assert d["fallback"] == base["fallback"], n
        assert d["volume"]["applied_to_mode"] is False, n

    # ...and at the unit, where BOTH modes are reachable by construction.
    for mode in ("Import", "DirectQuery"):
        for n in (10_000, 1_020_016, 42_104_338):
            d = apply_volume_hint({"mode": mode, "recommended_mode": mode, "fallback": None},
                                  {"value": n, "source": "live"})
            assert d["mode"] == mode, (mode, n)
            assert d["recommended_mode"] == mode, (mode, n)
            assert d["fallback"] is None, (mode, n)
            assert d["volume"]["applied_to_mode"] is False, (mode, n)


def test_every_advice_branch_is_reachable():
    """Guards the guard. Each branch must fire for SOME input, or a refusal asserted over it proves
    nothing -- which is precisely how the auto-flip control slipped through the first time."""
    fired = set()
    for mode in ("Import", "DirectQuery"):
        for n in (9_000, 500_000, 42_104_338):
            d = apply_volume_hint({"mode": mode}, {"value": n, "source": "live"})
            if d["volume"]["advice"]:
                fired.add((mode, d["volume"]["band"]))
    assert ("Import", "large") in fired
    assert ("Import", "moderate") in fired
    assert ("DirectQuery", "small") in fired


def test_a_large_import_recommends_incremental_refresh():
    d = apply_volume_hint({"mode": "Import"}, {"value": 42_104_338, "source": "live"})
    assert d["volume"]["band"] == "large"
    joined = " ".join(d["volume"]["advice"])
    assert "42,104,338" in joined, "the reader needs the NUMBER, not just the band"
    assert "incremental refresh" in joined


def test_a_small_directquery_says_import_would_be_better():
    """The reporter's other direction: 'DirectQuery becomes the better default past some threshold,
    and a poor one below it.'"""
    d = apply_volume_hint({"mode": "DirectQuery"}, {"value": 9_000, "source": "live"})
    assert "Import would almost certainly perform better" in " ".join(d["volume"]["advice"])


def test_a_hyper_sourced_count_always_carries_its_staleness_warning():
    """An extract count is 'as of its last Tableau refresh'. Omitting that is how a stale number
    gets read as a current one."""
    for n in (5_000, 500_000, 50_000_000):
        d = apply_volume_hint({"mode": "Import"}, {"value": n, "source": "hyper"})
        assert any("last Tableau refresh" in a for a in d["volume"]["advice"]), n


def test_a_live_count_carries_no_staleness_caveat():
    """Proves the caveat is provenance-driven rather than always-on -- an always-on warning teaches
    a reader to skip it."""
    d = apply_volume_hint({"mode": "Import"}, {"value": 50_000_000, "source": "live"})
    assert not any("last Tableau refresh" in a for a in d["volume"]["advice"])
