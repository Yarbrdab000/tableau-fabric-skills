"""Regression guard for the *info-collection* front matter of the runbook (SKILL.md).

The existing ``test_runbook_regression_guard`` pins the ASSISTED tiers (calc Tier-1 + dashboard
Tier-3) and the mechanical-span narration cap. It does NOT guard the very FIRST thing a run does:
the deterministic **information-collection** span — Gate Rules 1-6, the Phase 0A Decision Menu
(D1-D6), and the Phase 0C confirmation ledger. That span is what makes the opening of every run
*repeatable* instead of improvised; if its load-bearing anchors are silently eroded, the agent
starts varying how it collects source/scope/auth up front (asking differently, inferring defaults,
touching tools before the menu, or scavenging the filesystem for the input).

These tests lock those anchors so the beginning of the runbook cannot drift. They are doc-content
assertions only (no engine imports), tolerant to line-wrapping (whitespace collapsed, lowercased),
and pin *semantics* not exact prose, so legitimate edits stay free while the guarantees hold.
"""
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL = os.path.normpath(os.path.join(_HERE, "..", "SKILL.md"))


def _read(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()


def _flat(path):
    """Lowercased, whitespace-collapsed body — matches regardless of wrapping/indent."""
    return re.sub(r"\s+", " ", _read(path).lower())


# --------------------------------------------------------------- (a) the six non-negotiable gates
def test_gate1_first_turn_is_the_decision_menu_no_tool_call():
    flat = _flat(_SKILL)
    # First turn MUST be the Decision Menu, verbatim, with no tool/shell/file action that turn.
    # (Substring starts after the emphasized "no" so markdown ``**no**`` doesn't break the match.)
    assert "first turn = the decision menu, verbatim" in flat
    assert "tool call, shell command, or file read in that turn" in flat


def test_gate2_no_defaults_inferred_stop_and_ask():
    flat = _flat(_SKILL)
    assert "no defaults inferred, no question skipped" in flat
    # A blank/ambiguous answer stops and asks — never a guess.
    assert "stop and ask" in flat and "never guess" in flat


def test_gate3_go_gates_the_external_steps():
    flat = _flat(_SKILL)
    # `GO` gates STEP 1+; local setup (pinning vars) is allowed before GO, external work is not.
    assert "`go` gates step 1+ only" in flat
    assert "until the confirmation ledger" in flat and "replies `go`" in flat


def test_gate4_workbook_report_is_a_required_output():
    flat = _flat(_SKILL)
    assert "rebuilt report is a required output" in flat
    # The definition-of-done ledger fails loud when the report is missing.
    assert "definition-of-done" in flat or "definition_of_done" in flat


def test_gate5_no_deliberation_in_the_mechanical_span():
    flat = _flat(_SKILL)
    assert "no deliberation in the mechanical span" in flat
    # A checkpoint is a pass/fail glance; narration is capped to one line (mirrors the sibling guard).
    assert "one short status line per step" in flat
    assert "glance" in flat


def test_gate6_never_search_the_filesystem_for_the_input():
    flat = _flat(_SKILL)
    # (Emphasis markers ``*search*`` sit inside the phrase, so anchor on the unmarked spans.)
    assert "never" in flat and "the filesystem for a file" in flat
    assert "every path is pinned or handed to you" in flat
    # The attachment's surfaced absolute path IS the input; a disk-wide scan grabs a stale duplicate.
    assert "that attachment is the input" in flat
    assert "stale duplicate" in flat


# ---------------------------------------------------------------- (b) the Phase 0A Decision Menu
def test_phase0a_decision_menu_present_verbatim():
    flat = _flat(_SKILL)
    assert "phase 0a" in flat and "decision menu" in flat
    # It is to be presented VERBATIM (the anchor that keeps the opening identical run-to-run).
    assert "present verbatim" in flat


def test_decision_menu_has_all_six_axes():
    flat = _flat(_SKILL)
    # The six decisions the agent must collect before anything runs.
    for axis in (
        "d1 — source",
        "d2 — scope",
        "d3 — outputs",
        "d4 — conflicts",
        "d5 — auth",
        "d6 — credential access",
    ):
        assert axis in flat, f"Decision Menu is missing the {axis!r} axis"


def test_decision_menu_pins_the_load_bearing_defaults():
    flat = _flat(_SKILL)
    # PAT is the default/recommended auth; conflicts default to stop-and-ask (C); D6=A is Key Vault.
    assert "pat" in flat and "default, recommended" in flat
    assert "[default c]" in flat
    # The reply example keeps the menu answerable in one line.
    assert "d1=a, d2=all" in flat


# ----------------------------------------------------------------- (c) the Phase 0C run gate (ledger)
def test_phase0c_ledger_gates_the_run_on_go():
    flat = _flat(_SKILL)
    assert "phase 0c" in flat and "confirmation ledger" in flat
    # The ledger is echoed, then the run waits for GO — nothing external happens before it.
    assert "ledger — confirm, then reply go" in flat
    assert "run nothing until the user replies `go`" in flat
