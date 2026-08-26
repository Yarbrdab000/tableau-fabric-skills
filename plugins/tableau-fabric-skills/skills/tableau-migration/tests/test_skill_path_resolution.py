"""The skill must not tell an agent to GUESS where it lives, and self-update must not
leave a decoy beside the live skill.

WHY THIS EXISTS. ``SKILL.md`` establishes ``$SKILL`` once and then uses it ~22 times
(``py -3.11 "$SKILL\\scripts\\<name>.py"``). For a long time the line read::

    $SKILL = "<the folder holding this SKILL.md>"

-- a placeholder the agent had to resolve by inference, verified nowhere. That is only safe if the
answer is unique. It is not: measured on one machine, **27** ``tableau-migration`` folders existed
across versions 1.2.1-2.141.0 -- self-update backups written as ``<skill>.bak-<timestamp>``
SIBLINGS of the live skill, cloned repos under ``~/.copilot/chats``, personal ``~/.claude/skills``
copies, and ``~/.copilot/skill-backups``. An agent that picks the wrong one runs a months-old
engine, reads months-old playbooks, and re-asks the user for a path before every command.

The backups are inert to the LOADER -- ``plugin.json`` enumerates skills explicitly, so a
``.bak-*`` folder is never loaded. That was checked, and it is why this is about *path discovery*
rather than *skill resolution*. The two are easy to conflate; only the second is safe.

These tests pin the two halves that stop it recurring:

1. ``SKILL.md`` resolves ``$SKILL`` to a concrete path AND proves it with a ``Test-Path`` guard
   before the first script call.
2. ``self-update.md`` writes its backup outside the skills tree, in BOTH the PowerShell and the
   POSIX block -- a fix applied to one shell only would silently leave the other producing decoys.
"""
import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_HERE)
_SKILL_MD = os.path.join(_SKILL_DIR, "SKILL.md")
_SELF_UPDATE = os.path.join(_SKILL_DIR, "resources", "self-update.md")


def _read(path):
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def test_skill_md_does_not_ask_the_agent_to_guess_its_own_location():
    text = _read(_SKILL_MD)
    # The exact placeholder that shipped for months. Its return would restore the defect.
    assert '$SKILL = "<the folder holding this SKILL.md>"' not in text, (
        "SKILL.md is back to asking the agent to infer $SKILL. On a machine with install history "
        "that is a choice among dozens of folders spanning hundreds of versions."
    )


def test_skill_md_resolves_skill_to_a_concrete_path():
    text = _read(_SKILL_MD)
    assigns = re.findall(r"^\s*\$SKILL\s*=\s*(.+)$", text, re.M)
    assert assigns, "SKILL.md no longer assigns $SKILL at all"
    # At least one assignment must be a real path, not a <placeholder>.
    concrete = [a for a in assigns if "installed-plugins" in a]
    assert concrete, (
        "no $SKILL assignment names a concrete install path; found: %r" % (assigns,)
    )


def test_skill_md_proves_the_path_before_using_it():
    text = _read(_SKILL_MD)
    idx_assign = text.find("$SKILL =")
    assert idx_assign != -1, "SKILL.md no longer assigns $SKILL"
    # The guard must appear AFTER the assignment and BEFORE the first script invocation, or it is
    # decoration rather than a gate.
    guard = text.find('Test-Path "$SKILL\\scripts\\new_run.py"', idx_assign)
    assert guard != -1, (
        "SKILL.md assigns $SKILL but never proves it with a Test-Path on a known script"
    )
    first_call = text.find('py -3.11 "$SKILL\\scripts\\', idx_assign)
    assert first_call != -1, "SKILL.md no longer calls any $SKILL script"
    assert guard < first_call, (
        "the Test-Path guard is at %d but the first $SKILL script call is at %d -- the guard must "
        "come first or an unresolved path is used before it is checked" % (guard, first_call)
    )


def test_self_update_never_backs_up_beside_the_live_skill():
    text = _read(_SELF_UPDATE)
    # PowerShell form: `"$Install.bak-..."` writes a SIBLING of the live skill.
    assert '"$Install.bak-' not in text, (
        "self-update.md writes its PowerShell backup as a sibling of the live skill; that decoy is "
        "what put 4 stale copies next to the real one"
    )
    # POSIX form: `"$Install.bak-$(date ...)"`.
    assert '"$Install.bak-$(date' not in text, (
        "self-update.md writes its POSIX backup as a sibling of the live skill"
    )


@pytest.mark.parametrize("shell,pattern", [
    # PowerShell: the Copy-Item destination must be built from the backup ROOT, not from $Install.
    ("powershell", r"\$Backup\s*=\s*Join-Path\s+\$BackupRoot"),
    # POSIX: the `cp -a "$Install" <dest>` destination must be under the backup root.
    ("posix", r'cp -a "\$Install" "\$HOME/\.copilot/skill-backups/'),
])
def test_self_update_backs_up_outside_the_skills_tree_in_both_shells(shell, pattern):
    text = _read(_SELF_UPDATE)
    # Assert the COMMAND, not a mention count. An earlier version of this test counted occurrences
    # of "skill-backups"; a positive control showed that reverting the POSIX `cp` destination left
    # the neighbouring `mkdir -p .../skill-backups` untouched, so the count held and the test passed
    # on a reverted fix. Count the captures, never the matches.
    assert re.search(pattern, text), (
        "the %s backup does not write into ~/.copilot/skill-backups; a fix applied to one shell "
        "only leaves the other producing decoy folders beside the live skill" % shell
    )

