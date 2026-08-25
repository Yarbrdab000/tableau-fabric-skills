"""No git conflict marker may be committed to a tracked text file.

Found in ``CHANGELOG.md`` at the tip of an integration branch carrying eighteen merged releases.
Three orphaned markers sat inside the 2.306.0 and 2.308.0 entries, and **every gate stayed green**:
the suite passed, the version-chain gate parsed 145 entries and reported 0 breaks, the mirror was
byte-identical, and both anchors resolved. Nothing in the repo reads for markers, and the chain
parser is line-oriented, so a stray marker line is simply not an entry header and is skipped.

That is the inverse of the rule this repo already carries. "A clean auto-merge is not evidence of
correctness" is about a merge git believed it resolved. This is the other half: **a merge git
REFUSED to resolve, resolved by hand, with the markers left behind** -- and every downstream signal
still agreed, because each was measuring something the markers do not disturb.

The specific damage is instructive. There was no separator line left, only the outer two markers, so
the file did not look obviously broken to a reader either: one marker split a bullet's header line in
half, and the other pair wrapped a ``### Added`` heading that read perfectly well. A conflict that
LOOKS resolved is what survives review.

Three checks, and the second and third exist because the first can only ever report an absence:

* **the gate** -- no tracked text file contains a marker line.
* **it scanned something** -- a walk that matches no files reports the same clean zero as a healthy
  repo. The count is asserted, not printed.
* **the predicate can fire, and is not trigger-happy** -- it must flag real markers in synthetic
  content, and must stay silent on prose that merely mentions or indents them, because this repo's
  own documentation discusses merge conflicts.

The marker strings are BUILT rather than written literally, so this module does not flag itself --
the same trap as a format-string check matching inside the annotation that preserves a source
formula.
"""

import os
import re

import pytest

# Built by repetition so no literal marker run appears in this file's own source.
_OURS = "<" * 7
_THEIRS = ">" * 7
_SPLIT = "=" * 7

# A marker is anchored at column 0. ``<<<<<<< `` / ``>>>>>>> `` carry a branch label after a space;
# the separator is the run alone on its line. Anchoring is what keeps prose safe: an indented example
# inside a fenced code block, or a run quoted mid-sentence, does not match.
_MARKER_RE = re.compile(
    r"^(?:%s |%s |%s\s*$)" % (re.escape(_OURS), re.escape(_THEIRS), re.escape(_SPLIT)))

_TEXT_SUFFIXES = (".md", ".py", ".json", ".txt", ".toml", ".yml", ".yaml", ".cfg", ".ini",
                  ".tmdl", ".tmd", ".ps1", ".sh")
_TEXT_NAMES = ("VERSION", "LICENSE", ".gitignore", ".gitattributes")
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", ".mypy_cache"}


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


def _text_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(_TEXT_SUFFIXES) or name in _TEXT_NAMES:
                yield os.path.join(dirpath, name)


def _scan():
    """``(hits, files_scanned)`` -- ``hits`` is ``[(relpath, line_no, line)]``."""
    root = _find_repo_root()
    if root is None:
        pytest.skip("repo layout not present (installed-skill context)")
    hits, scanned = [], 0
    for path in _text_files(root):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                lines = fh.read().split("\n")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        for i, line in enumerate(lines, 1):
            if _MARKER_RE.match(line):
                hits.append((os.path.relpath(path, root), i, line[:60]))
    return hits, scanned


def test_no_committed_conflict_markers():
    hits, _scanned = _scan()
    assert not hits, (
        "git conflict markers are committed -- a merge was resolved by hand and the markers were "
        "left in. Every other gate passes on this: the version chain still parses, the mirror is "
        "still byte-identical, the suite is still green. Remove the marker lines and re-read the "
        "surrounding entries, because a marker can split a header in half without looking wrong:\n  "
        + "\n  ".join("%s:%d  %s" % h for h in hits))


def test_the_scan_actually_reads_files():
    """A walk matching nothing reports the same clean zero as a healthy repo.

    The gate above can only ever report an absence, so its pass is worthless unless the population it
    swept is known to be non-empty. This is the failure that made the marker survive in the first
    place -- a signal that agrees with a healthy state for a reason unrelated to health.
    """
    _hits, scanned = _scan()
    assert scanned > 50, (
        "the conflict-marker scan read only %d file(s); it is not covering the repo, so its clean "
        "result says nothing" % scanned)


def test_the_predicate_flags_real_markers_and_spares_prose():
    """Bidirectional, from one harness: it must fire on markers and stay silent on prose about them.

    Silence alone is indistinguishable from a dead predicate, and a predicate that fires on prose
    would make this repo's own merge documentation unmergeable.
    """
    must_flag = [
        _OURS + " HEAD",
        _THEIRS + " some-branch-name",
        _SPLIT,
    ]
    must_not_flag = [
        "    " + _OURS + " HEAD",                  # indented, e.g. inside a fenced block
        "resolve any " + _OURS + " marker by hand",  # mentioned mid-sentence
        "- a merge left " + _THEIRS + " in the file",
        "=" * 5,                                    # a short rule, not a separator
        "-" * 40,                                   # a markdown horizontal rule
    ]
    flagged = [s for s in must_flag if not _MARKER_RE.match(s)]
    assert not flagged, "the predicate FAILED to flag real markers: %r" % flagged

    spurious = [s for s in must_not_flag if _MARKER_RE.match(s)]
    assert not spurious, "the predicate fires on prose or indented examples: %r" % spurious
