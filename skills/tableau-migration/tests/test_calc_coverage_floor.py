"""Calc coverage is a RATCHET: a change may raise it, never quietly lower it.

Calculation coverage — how many of a workbook's Tableau calcs the deterministic tier translates
rather than stubs — is the headline number of a migration. Everything downstream leans on it: a
stubbed calc is a `BLANK()` measure, and a visual bound to it is empty no matter how faithfully the
chart type, fields and layout were rebuilt.

It is also the number nothing was watching, which is how this file came to exist.

WHAT HAPPENED. A change that generated one Date dimension per datasource island — correct in
isolation, and fixing a real defect where a shared calendar let a date slicer filter datasources
Tableau keeps apart — dropped Salesforce NPSP from **118/157 to 115/157**. Three calculations went
dead: `Count of Waitlisted Engagements`, the same `... in Date Range`, and `Sort by Intake`, all
newly refused on "must reference exactly one table".

Nothing caught it. 4,451 tests passed. The corpus built 29/29. Every report PBIR-validated to zero
errors. The regression surfaced only because a human happened to compare a coverage number against
one printed earlier the same day. That is not a gate; that is luck.

The gap is structural: every existing gate asks "did it build, and is it well-formed?" — none asks
"did it translate as much as it used to?" A model can lose a third of its measures and still build,
validate, open, and render, because a `BLANK()` measure is perfectly well-formed.

So this pins a floor per corpus workbook. Raising a floor is the normal, welcome outcome of a fix and
the number here is expected to move up over time; LOWERING one fails, loudly, naming the workbook and
the delta. If a drop is genuinely intended — a translation was wrong and stubbing it honestly is
better than emitting bad DAX — the floor is edited in the same commit, with the reason. That makes a
coverage loss a deliberate, reviewable act instead of an accident nobody notices.
"""
import json
import os

import pytest

# workbook -> (translated, total) floor, captured from the live engine.
# Raise freely when a fix improves coverage. LOWERING one requires a stated reason in the commit.
COVERAGE_FLOOR = {
    "salesforce_npsp": (118, 157),
    "atti_attr": (14, 15),
    "logic_example_2": (12, 12),
}

_RUNS = {
    "salesforce_npsp": r"C:\tfmig\attrib\C_salesforce\rev\report.json",
    "atti_attr": r"C:\tfmig\attrib\B_atti\out\report.json",
    "logic_example_2": r"C:\tfmig\attrib\A_logic_example\out\report.json",
}


def _coverage(path):
    with open(path, encoding="utf-8-sig") as fh:
        r = json.load(fh)
    s = r.get("summary") or {}
    return s.get("workbook_calcs_translated"), s.get("workbook_calcs_total")


@pytest.mark.parametrize("wb", sorted(COVERAGE_FLOOR))
def test_calc_coverage_never_falls(wb):
    """A translated-calc count below its recorded floor is a regression, not a detail."""
    path = _RUNS[wb]
    if not os.path.isfile(path):
        pytest.skip("no local run for %s (run migrate_estate to populate)" % wb)
    translated, total = _coverage(path)
    floor_t, floor_total = COVERAGE_FLOOR[wb]
    assert total == floor_total, (
        "%s: calc TOTAL moved %d -> %s; the workbook or parser changed, so the floor is no longer "
        "comparable and must be re-captured deliberately." % (wb, floor_total, total))
    assert translated >= floor_t, (
        "%s: calc coverage FELL %d -> %d of %d. Every downstream check still passes when this "
        "happens -- a stubbed calc is a well-formed BLANK() measure -- so nothing else will tell "
        "you. Either fix the regression, or raise/lower this floor in the same commit with the "
        "reason." % (wb, floor_t, translated, total))


def test_the_floor_covers_the_workbook_that_exposed_the_gap():
    """Salesforce NPSP is the case this gate was built from; it must never silently drop out."""
    assert "salesforce_npsp" in COVERAGE_FLOOR
    assert COVERAGE_FLOOR["salesforce_npsp"] == (118, 157)
