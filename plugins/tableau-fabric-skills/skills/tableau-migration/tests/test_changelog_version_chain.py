"""The CHANGELOG's declared version chain must match reality.

Two agents working in parallel hit the SAME failure inside one hour, on the same rebase, and
neither was careless: a cross-session rebase merges the ``VERSION`` stamp cleanly while the PROSE
describing that stamp goes stale silently. Both wrote an entry reading ``(skill X -> Y)`` that was
true when written, and became false the moment another session's commits landed underneath and
changed the actual predecessor. Nothing anywhere checked it, so only a human reading two numbers
would ever have caught it.

It is mechanically checkable from the CHANGELOG alone, so it should be a gate rather than a habit:

* **the chain is continuous** -- each entry's declared predecessor equals the declared successor of
  the entry below it, reading newest-first. Deliberately heading-agnostic: the file interleaves
  ``### Added`` and ``### Fixed`` and a release's category says nothing about its ordering.
* **no version is claimed twice** -- two entries producing the same version means one of them
  renumbered without renaming, which is the other half of the same rebase hazard.
* **the newest entry matches the shipped stamp** -- this is the one that actually breaks users. The
  self-update runbook compares the installed ``VERSION`` against the raw ``VERSION`` on ``main`` and
  reinstalls only when main is NEWER, so a CHANGELOG that documents 2.156.0 above a ``VERSION`` file
  still reading 2.154.0 leaves every client that reached the higher number permanently deaf to
  everything after it.

Skips rather than fails when the repo layout is absent (an installed-skill context has no root
``CHANGELOG.md``), matching ``test_mirror_parity``.
"""

import os
import re

import pytest

# ``- **`tableau-migration` (skill `2.154.0` -> `2.155.0`): ...`` -- the arrow is a Unicode RIGHTWARDS
# ARROW in the committed file; ASCII ``->`` is accepted too so a hand-typed entry is still checked.
_ENTRY_RE = re.compile(
    r"\(skill\s*`(?P<frm>\d+\.\d+\.\d+)`\s*(?:->|\u2192)\s*`(?P<to>\d+\.\d+\.\d+)`\)")


def _find_repo_root():
    cur = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isdir(os.path.join(cur, "skills")) and os.path.isdir(
                os.path.join(cur, "plugins")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _entries():
    """``[(line_no, frm, to)]`` newest-first, one per skill-version bullet."""
    root = _find_repo_root()
    if root is None:
        pytest.skip("repo layout not present (installed-skill context)")
    path = os.path.join(root, "CHANGELOG.md")
    if not os.path.isfile(path):
        pytest.skip("no root CHANGELOG.md")
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    out = []
    for i, line in enumerate(lines, 1):
        # Only a top-level bullet starts an entry; a version mentioned mid-paragraph is prose.
        if not line.lstrip().startswith("- "):
            continue
        m = _ENTRY_RE.search(line)
        if m:
            out.append((i, m.group("frm"), m.group("to")))
    if not out:
        pytest.skip("no versioned CHANGELOG entries to check")
    return out


def _skill_version():
    root = _find_repo_root()
    path = os.path.join(root, "skills", "tableau-migration", "VERSION")
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read().strip()


def test_the_declared_version_chain_is_continuous():
    entries = _entries()
    breaks = []
    for (line, frm, _to), (below_line, _bfrm, below_to) in zip(entries, entries[1:]):
        if frm != below_to:
            breaks.append(
                "L%d declares predecessor %s, but the entry below it (L%d) ends at %s"
                % (line, frm, below_line, below_to))
    assert not breaks, (
        "CHANGELOG version chain is broken -- an entry's declared predecessor must equal the "
        "version produced by the entry beneath it:\n  " + "\n  ".join(breaks))


def test_no_version_is_produced_by_two_entries():
    entries = _entries()
    seen = {}
    dupes = []
    for line, _frm, to in entries:
        if to in seen:
            dupes.append("%s claimed at L%d and again at L%d" % (to, seen[to], line))
        seen[to] = line
    assert not dupes, "duplicate CHANGELOG versions:\n  " + "\n  ".join(dupes)


def test_the_newest_entry_matches_the_shipped_version_stamp():
    """The half that actually reaches users, via the self-update comparison."""
    entries = _entries()
    _line, _frm, newest = entries[0]
    stamp = _skill_version()
    assert newest == stamp, (
        "the newest CHANGELOG entry documents %s but skills/tableau-migration/VERSION reads %s -- "
        "self-update compares the STAMP, so a client that reached the higher number would never "
        "reinstall again" % (newest, stamp))


def test_the_chain_detects_a_stale_predecessor():
    """The gate must actually fire -- pin it against the exact shape both agents shipped."""
    entries = [(10, "2.154.0", "2.156.0"), (40, "2.154.0", "2.155.0")]
    breaks = [1 for (_l, frm, _t), (_bl, _bf, bto) in zip(entries, entries[1:]) if frm != bto]
    assert breaks, "a stale predecessor (2.154.0 over an entry ending 2.155.0) must be detected"
