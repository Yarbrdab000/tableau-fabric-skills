"""Regression guard for the render-verify OPERATING DISCIPLINE in ``fidelity-oracle.md``.

The render bridge only yields a trustworthy candidate PNG if it is driven in the right order.
The four operating rules below are deterministic (not judgment calls) and were hard-won on a real
migration (the Salesforce-Nonprofit benchmark); dropping any one produces a *confidently wrong*
oracle input — a blank or stale render scored as if it were the real report. These tests pin the
rules into the oracle doc so they cannot silently regress.

Doc-content assertions only (no engine imports); whitespace-tolerant; they avoid the section
header's typographic hyphen/arrow characters and anchor on plain-text rule phrases.
"""
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_ORACLE = os.path.normpath(os.path.join(_HERE, "..", "resources", "fidelity-oracle.md"))


def _flat():
    with open(_ORACLE, "r", encoding="utf-8-sig") as fh:
        return re.sub(r"\s+", " ", fh.read().lower())


def test_discipline_section_exists():
    flat = _flat()
    assert "operating discipline" in flat


def test_l7_reload_refresh_before_screenshot():
    flat = _flat()
    # The blank-first-screenshot fix: full refresh + wait BEFORE capture, never immediately post-reload.
    assert "never screenshot immediately after a reload" in flat
    assert "even though the model has data" in flat
    assert "refreshwithxmla" in flat
    assert "wait for it to complete" in flat


def test_l6_recursive_tables_copy_on_export_reload():
    flat = _flat()
    # Export-then-reload must recurse tables\, never a flat copy, or the report shows the old schema.
    assert "flat copy" in flat
    assert "old schema" in flat
    assert "recursive" in flat


def test_l11_theme_edit_needs_a_cache_busting_name_bump():
    flat = _flat()
    assert "caches themes by internal name" in flat
    assert "bump the theme" in flat
    assert "in the same write" in flat


def test_l15_pin_target_pid_and_reverify_every_cycle():
    flat = _flat()
    assert "pin the target" in flat
    assert "foreign pid" in flat
    assert "powerbi-desktop status" in flat
    assert "hasunsavedchanges" in flat
