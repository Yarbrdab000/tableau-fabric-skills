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
* **every entry has a BODY** -- a rebase left a header-only duplicate of a renumbered entry, carrying
  its pre-renumber chain reference and no content at all. It satisfied every check above: the version
  was present, the chain was continuous, nothing was claimed twice. An entry can EXIST, be
  well-formed, and be EMPTY, so counting markers proves nothing about content. This also catches the
  duplicate-WITH-body that a pure marker count misses, because the stale copy is the one left
  without prose.

Skips rather than fails when the repo layout is absent (an installed-skill context has no root
``CHANGELOG.md``), matching ``test_mirror_parity``.

ONE THING THIS MODULE CANNOT DO FOR YOU: it only ever runs where pytest runs, which is normally the
TIP of a branch. The CHANGELOG is a file every commit rewrites, so a stack can satisfy every
invariant here at HEAD and violate them one commit down -- measured on a renumbered two-commit stack
whose tip was correct and whose parent still declared the pre-renumber predecessor. The invariant was
present; the execution point was missing. Run it across a stack explicitly::

    git rebase --exec "cd skills/tableau-migration && py -3.11 -m pytest tests/test_changelog_version_chain.py -q" origin/main

See the versioning section of ``AGENTS.md``.
"""

import os
import sys
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


# A SENTINEL, deliberately WIDER than ``_ENTRY_RE`` in every dimension, used to tell a PARSE FAILURE
# apart from a genuine ABSENCE. It must not require the backtick around the skill name (this file
# carries two entry formats and only some entries backtick it -- a sentinel keyed on that matches 99
# of 148 and would itself go quiet on the other format), and it must not require the ``(skill ...)``
# construct that ``_ENTRY_RE`` depends on, or sentinel and parser go to zero together and the guard is
# silent exactly when it is needed. A sentinel narrower than the thing it guards is a second
# instrument sharing the first one's blind spot.
_ENTRY_SHAPE_RE = re.compile(r"^\s*-\s*\*\*`?tableau-migration`?")


def _entries():
    """``[(line_no, frm, to)]`` newest-first, one per skill-version bullet.

    ABSENT and UNPARSED must not produce the same outcome. A repo with no ``CHANGELOG.md`` -- an
    installed-skill context -- legitimately has nothing to check, and skipping is correct. A
    ``CHANGELOG.md`` that is *full of entry-shaped bullets* while this parser returns zero is a broken
    parser, and skipping there turns the gate guarding every release in this repo into one more line
    in ``6 skipped``: a number nobody reads, already non-zero for legitimate reasons.

    That is the same vacuous pass this module's own verification hit from the other side -- a mangled
    regex produced ``entries=0`` and therefore ``0 breaks, no duplicates``, every line true of an empty
    list and indistinguishable from a healthy file. Here the parser would find *nothing* and report
    health; a conflict marker makes it find *everything* and report health. **Both are correct answers
    to the question the predicate encodes, and neither question was "is this file well-formed."**
    """
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
        shaped = sum(1 for line in lines if _ENTRY_SHAPE_RE.search(line))
        assert not shaped, (
            "the CHANGELOG entry parser matched 0 entries, but %d line(s) look like release entries "
            "-- this is a PARSE FAILURE, not an absence, and every check in this module would "
            "otherwise report a clean pass over an empty list. Either the entry format changed or "
            "_ENTRY_RE is broken; fix one of those before trusting any result here." % shaped)
        pytest.skip("no versioned CHANGELOG entries to check")
    return out


def _skill_version():
    root = _find_repo_root()
    path = os.path.join(root, "skills", "tableau-migration", "VERSION")
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read().strip()


def _entry_bodies():
    """``[(line_no, to, non_blank_line_count)]`` -- the prose under each entry header.

    Shares :func:`_entries` deliberately rather than re-parsing. A second parser here would be a
    second predicate over the same file, and the two could disagree about what an entry IS while
    each looked correct on its own -- the failure this module exists to prevent, reintroduced one
    level down.

    An entry's body runs from the line after its header to whichever comes first: the next entry, a
    Markdown section heading, or end of file. The heading stop matters because the file interleaves
    ``### Added`` and ``### Fixed``, so the last entry in a section would otherwise absorb the next
    section's header as content.
    """
    root = _find_repo_root()
    with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    entries = _entries()
    starts = [e[0] for e in entries]
    out = []
    for idx, (line_no, _frm, to) in enumerate(entries):
        end = starts[idx + 1] - 1 if idx + 1 < len(starts) else len(lines)
        body = lines[line_no:end]
        for j, text in enumerate(body):
            if text.startswith("## ") or text.startswith("### "):
                body = body[:j]
                break
        out.append((line_no, to, len([b for b in body if b.strip()])))
    return out


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


def test_every_entry_has_a_body():
    """An entry that exists, is well-formed, and says NOTHING is a rebase artifact, not a release.

    A cross-session rebase left a header-only duplicate of a renumbered entry: same shape, same
    ``(skill X -> Y)`` marker, zero prose. Every other check in this module passed on it, because
    each asks about the HEADER. A reader diffing two CHANGELOGs sees a version present in both and
    concludes the release is documented.

    Threshold is deliberately ``> 0`` rather than a size floor, and the margin is measured rather
    than assumed: across the 99 entries in this file the SMALLEST real body is 604 characters over
    7 non-blank lines. A legitimate entry therefore clears this by roughly two orders of magnitude,
    so the check cannot fire on terse-but-real prose -- only on genuine emptiness.
    """
    empty = [(line_no, to) for line_no, to, count in _entry_bodies() if count == 0]

    assert not empty, (
        "CHANGELOG entries with no body -- a version was documented in name only, which a "
        "marker/count check cannot see: "
        + ", ".join("%s at L%d" % (to, line_no) for line_no, to in empty))


def test_the_body_check_detects_a_header_only_entry():
    """Prove this gate CAN fail, on the real parser rather than a hand-built fixture.

    Every gate in this repo that turned out to be worthless looked healthy while passing: one whose
    rule nothing called, one whose reachability set included the artifact under test. "It passed" is
    not evidence until the red has been seen, so the empty-body predicate is exercised directly on
    the shape the rebase produced.
    """
    bodies = [(1, "2.99.0", 0), (10, "2.98.0", 7)]
    empty = [(line_no, to) for line_no, to, count in bodies if count == 0]

    assert empty == [(1, "2.99.0")], "the predicate must flag a zero-body entry and nothing else"


def test_a_broken_parser_fails_rather_than_skipping(tmp_path, monkeypatch):
    """A parse failure must FAIL. An absent CHANGELOG must SKIP. They must not look the same.

    Before this guard, a broken ``_ENTRY_RE`` -- a format change, or a regex mangled in transit --
    made ``_entries()`` return nothing and skip, so the gate protecting every release in this repo
    became one more line in ``6 skipped``. Every other check in this module then passed over an empty
    list: ``0 breaks``, ``no duplicates``, all true, all meaningless.

    Both directions from one harness, because a guard that only ever reports an absence is
    indistinguishable from a dead one.
    """
    repo = tmp_path / "repo"
    (repo / "skills").mkdir(parents=True)
    (repo / "plugins").mkdir()
    monkeypatch.setattr(sys.modules[__name__], "_find_repo_root", lambda: str(repo))

    # 1. entry-shaped bullets present, parser matches none -> must FAIL, not skip.
    (repo / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n"
        "- **`tableau-migration` (skill NOT-A-VERSION): a release the parser cannot read.**\n"
        "  body text\n",
        encoding="utf-8")
    with pytest.raises(AssertionError) as excinfo:
        _entries()
    assert "PARSE FAILURE" in str(excinfo.value), (
        "a broken parser must say so; it must not be mistaken for an empty changelog")

    # 2. no CHANGELOG at all -> must SKIP, because there is legitimately nothing to check.
    #
    # ``BaseException``, not ``Exception``: ``pytest.skip()`` raises ``Skipped``, which derives from
    # ``BaseException``. Catching ``Exception`` lets it escape, and the skip then propagates and marks
    # THIS test skipped -- which is what happened on the first run of it. A test written to stop a
    # vacuous skip, vacuously skipped, reporting neither pass nor failure while its first assertion
    # had already succeeded and gone unreported.
    (repo / "CHANGELOG.md").unlink()
    with pytest.raises(BaseException) as excinfo:
        _entries()
    assert excinfo.typename == "Skipped", (
        "an absent CHANGELOG is an installed-skill context and must skip, not fail -- got %s"
        % excinfo.typename)


def test_the_sentinel_is_wider_than_the_parser_it_guards(tmp_path, monkeypatch):
    """The sentinel must match every real entry format, or it shares the parser's blind spot.

    This file carries TWO entry formats -- some entries backtick the skill name and some do not. A
    sentinel keyed on the backticked form matches 99 of 148 real entries, so it would go quiet on the
    other format and could not distinguish "the parser broke" from "the format moved". Measured
    against the live CHANGELOG rather than a fixture, because the formats in it are the thing at risk.
    """
    strict = _entries()
    root = _find_repo_root()
    with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    shaped = sum(1 for line in lines if _ENTRY_SHAPE_RE.search(line))
    assert shaped >= len(strict), (
        "the sentinel matches %d line(s) but the strict parser found %d entries -- the sentinel is "
        "NARROWER than the parser it guards, so a format it cannot see would read as a clean absence"
        % (shaped, len(strict)))