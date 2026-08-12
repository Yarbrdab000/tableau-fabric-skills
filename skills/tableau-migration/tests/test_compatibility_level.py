"""A model emitted below Desktop's compatibility level will not reopen once it has been refreshed.

This is the nastiest shape of defect this gate exists for, because every signal available at build
time says the model is fine and so does the FIRST open. Power BI Desktop silently upgrades an older
model in memory; a refresh then persists ``.pbi/cache.abf`` at the UPGRADED level; and only on the
next COLD open does Tabular refuse:

    There's a problem with the definition content in your Power BI Project.
    Tabular databases do not support CompatibilityLevel downgrade.
    Current CompatibilityLevel: '1606'. Requested CompatibilityLevel: '1604'.

The report does not open at all -- not a degraded visual, not a wrong number, nothing. Measured on a
migrated ATTI/ATTR dashboard that had been built, polished, PBIR-validated with zero errors,
refreshed to 5,000 rows and screenshotted successfully: every one of those checks ran against a
Desktop session that was ALREADY LOADED, so none of them could see it. Closing Desktop and reopening
from disk was the only thing that would have caught it.

Two defences, both hermetic:

* the emitter no longer hardcodes an old level -- it emits ``MODEL_COMPATIBILITY_LEVEL``, matching
  what current Desktop writes, so there is no upgrade and therefore no mismatch to hit;
* this gate fails a model whose declared level is below that floor, so lowering it again cannot slip
  through silently.

Neither needs Power BI installed. The check reads the emitted ``database.tmdl`` and costs
microseconds, which is the point: a defect that only a cold open reveals is still provable offline,
because the condition is static.
"""
import openability_gate as G
import tmdl_generate as T


def test_the_emitter_matches_the_gate_floor():
    """One constant drift and the trap is back -- so the two are pinned to each other."""
    assert T.MODEL_COMPATIBILITY_LEVEL >= G.MIN_COMPATIBILITY_LEVEL


def test_the_emitted_database_passes_its_own_gate():
    r = G.check_model_openability({"definition/database.tmdl": T.generate_database_tmdl()})
    assert r["checks"]["compatibility_level_current"] is True
    assert r["ok"] is True


def test_a_downgrade_level_is_refused():
    """1604 is what shipped, and what produced a report that would not open."""
    r = G.check_model_openability(
        {"definition/database.tmdl": "database\n\tcompatibilityLevel: 1604\n"})
    assert r["checks"]["compatibility_level_current"] is False
    assert r["ok"] is False
    detail = r["issues"][0]["detail"]
    assert "1604" in detail and "downgrade" in detail.lower()


def test_a_missing_level_is_refused():
    r = G.check_model_openability({"definition/database.tmdl": "database\n"})
    assert r["checks"]["compatibility_level_current"] is False


def test_a_higher_level_is_accepted():
    """A newer Desktop may write higher; only BELOW the floor is a trap."""
    r = G.check_model_openability(
        {"definition/database.tmdl": "database\n\tcompatibilityLevel: 1702\n"})
    assert r["checks"]["compatibility_level_current"] is True


def test_the_check_is_skipped_when_no_database_part_is_present():
    """Callers that gate a partial parts dict must not acquire a spurious failure."""
    r = G.check_model_openability({"definition/tables/T.tmdl": "table T\n"})
    assert "compatibility_level_current" not in r["checks"]
