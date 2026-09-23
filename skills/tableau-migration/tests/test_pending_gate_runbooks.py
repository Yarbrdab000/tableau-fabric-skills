"""Every emitted pending-gate runbook must actually DOCUMENT its gate (#193).

``report.json.pending_gates[]`` presents a decision the user is required to make, and points at a
``runbook`` for the procedure. A pointer to a document that does not mention the gate is worse than
no pointer: it reads as "the answer is over there", so a user who chooses ``GO`` follows it and finds
nothing.

Measured at 2.368.0, which is what #193 reports:

    scripts/polish_layout.py                                 EXISTS, 14.8 KB
    resources/dashboard-audit.md, mentions of layout_polish  ZERO
    emitted runbook for the layout_polish gate               resources/dashboard-audit.md

The gate had an inline ``offer`` carrying a runnable command, which is why this stayed invisible --
it was sufficient for a user who DECLINED. Only someone who said ``GO`` would discover the runbook
could not explain the inputs, the dry-run mode, which files may change, the acceptance condition, or
the geometry-only boundary.

PATH EXISTENCE IS NOT ENOUGH, and that is the whole point of the issue: the broken pointer here was
to a file that exists, is 21 KB, and is the correct document for the SIBLING gate offered in the same
breath. Every check below therefore reads the target's CONTENT.

``_pending_gates(summary)`` is a pure function of a summary dict, so these drive all three gates
directly rather than through a corpus build -- a build only surfaces the gates that happened to fire.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import migrate_estate as M  # noqa: E402

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# A summary that fires EVERY gate at once, so no gate can slip through by not being triggered.
# The trigger keys are read off the gate entries' own ``trigger`` strings, NOT guessed: the first
# version of this fixture used ``calcs_stubbed`` and silently exempted ``second_compiler`` from
# every check below. The control test is what caught it.
ALL_GATES_SUMMARY = {
    "needs_review_total": 3,      # -> second_compiler
    "visuals_warned": 2,          # -> dashboard_audit
    "workbooks_pbip_built": 1,    # -> layout_polish
}


def _gates():
    gates = M._pending_gates(ALL_GATES_SUMMARY)
    assert gates, "no gates emitted -- the trigger keys changed, so nothing below is tested"
    return gates


def _runbook_text(rel):
    path = os.path.join(SKILL_ROOT, rel.replace("/", os.sep))
    assert os.path.isfile(path), "runbook does not exist: %s" % rel
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_summary_fires_every_known_gate():
    """The control for every test below: a gate that never fires is never checked.

    If a new gate is added with a trigger this summary does not satisfy, this fails and tells the
    author to extend the fixture -- rather than silently exempting the new gate from the contract.

    Not hypothetical: the first version of this fixture keyed on ``calcs_stubbed``, which no gate
    reads, so ``second_compiler`` was exempt from every assertion in this file and all of them
    passed.
    """
    names = sorted(g["gate"] for g in _gates())
    assert names == ["dashboard_audit", "layout_polish", "second_compiler"], names


def test_the_fixture_satisfies_each_gates_OWN_declared_trigger():
    """Pins the fixture to the ``trigger`` each gate publishes, rather than to a guessed key.

    Every gate names the summary field it keys on (``summary.needs_review_total`` etc.). Asserting
    the fixture supplies exactly those fields means a renamed trigger fails HERE, with the reason,
    instead of quietly dropping that gate out of the population above.
    """
    for g in _gates():
        trigger = g.get("trigger") or ""
        assert trigger.startswith("summary."), "gate %r has no summary trigger" % g["gate"]
        key = trigger.split(".", 1)[1]
        assert key in ALL_GATES_SUMMARY, (
            "gate %r triggers on summary.%s, which the fixture does not supply -- add it, or this "
            "gate is exempt from every check in this file" % (g["gate"], key))


def test_every_emitted_runbook_path_exists():
    for g in _gates():
        rel = g.get("runbook")
        assert rel, "gate %r emits no runbook" % g["gate"]
        path = os.path.join(SKILL_ROOT, rel.replace("/", os.sep))
        assert os.path.isfile(path), "gate %r points at a missing runbook %r" % (g["gate"], rel)


def test_every_runbook_MENTIONS_the_gate_it_is_cited_for():
    """The assertion #193 is actually about.

    The broken pointer was to a file that exists, is 21 KB, and is the right document for the
    sibling gate offered alongside it. Existence proves nothing; the document has to name the gate.
    """
    for g in _gates():
        text = _runbook_text(g["runbook"]).lower()
        gate = g["gate"]
        alts = {gate, gate.replace("_", " "), gate.replace("_", "-")}
        assert any(a in text for a in alts), (
            "gate %r cites %r, which never mentions it -- a user who answers GO has no procedure"
            % (gate, g["runbook"]))


def test_every_runbook_carries_the_SCRIPT_its_offer_tells_the_user_to_run():
    """A gate whose offer names an executable must have that executable documented where it points.

    Checks the script BASENAME rather than the full command, because the offer interpolates a
    ``$SKILL`` path and a per-workbook target that no static document can contain.
    """
    for g in _gates():
        scripts = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*\.py)", g.get("offer") or ""))
        if not scripts:
            continue
        text = _runbook_text(g["runbook"])
        for s in scripts:
            assert s in text, (
                "gate %r offers `%s` but %r never mentions it" % (g["gate"], s, g["runbook"]))


def test_the_layout_polish_runbook_explains_the_four_things_a_GO_needs():
    """The specific acceptance criteria in #193, pinned so they cannot quietly be dropped.

    Named individually rather than as one 'is it documented' assertion, so a failure says WHICH
    part went missing.
    """
    gate = next(g for g in _gates() if g["gate"] == "layout_polish")
    text = _runbook_text(gate["runbook"])
    low = text.lower()

    assert "--dry-run" in text, "the dry-run mode is not documented"
    assert "position" in low, "the geometry-only boundary (position rects) is not documented"
    assert "optional" in low, "the gate is not described as optional"
    # The acceptance condition: kept only when the measured defect count falls.
    assert re.search(r"defect count\s+\*{0,2}falls", low) or "monotonic" in low, (
        "the measured-defect acceptance condition is not documented")


def test_the_polish_boundary_is_stated_as_what_CANNOT_change():
    """A boundary written only as what the tool touches is read as a feature list.

    The load-bearing half for a user deciding whether to run it unattended is the negative: no
    field, filter, measure, visual type or number can move.
    """
    text = _runbook_text("resources/dashboard-audit.md").lower()
    for token in ("no field", "visual type", "number"):
        assert token in text, "the polish boundary does not state %r cannot change" % token


# ------------------------------------------------------------- the check can see a real break

def test_the_content_check_goes_red_on_a_gate_pointing_at_the_wrong_document():
    """Positive control: prove this suite can FAIL, rather than passing by construction.

    Points a real gate at a real, existing runbook that documents a different gate -- which is
    exactly the #193 shape. A path-existence-only check passes this; the content check must not.
    """
    gate = dict(next(g for g in _gates() if g["gate"] == "layout_polish"))
    gate["runbook"] = "resources/second-compiler.md"

    text = _runbook_text(gate["runbook"]).lower()
    alts = {gate["gate"], gate["gate"].replace("_", " "), gate["gate"].replace("_", "-")}
    assert not any(a in text for a in alts), (
        "control is void: second-compiler.md now mentions layout_polish, so this no longer proves "
        "the content check can fail -- re-point the control at another document")

    path = os.path.join(SKILL_ROOT, gate["runbook"].replace("/", os.sep))
    assert os.path.isfile(path), "the control's target must EXIST, or it proves the wrong thing"
