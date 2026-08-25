"""A zero-entry CHANGELOG parse must FAIL, not skip -- absence and unparsed are different.

The chain gate in ``test_changelog_version_chain`` guards every release in this repo: it asserts
each entry's declared predecessor equals the version produced by the entry beneath it, and that the
newest matches the shipped ``VERSION``. It is the reason a rebased stack cannot silently renumber.

Before the guard this pins, **a broken entry pattern made every check in that module skip**. A
CHANGELOG format change, or a pattern mangled in transit, and the gate stops guarding while the
suite reports one more line in an already non-zero ``skipped`` count. Verified by injection: with
``_ENTRY_RE`` broken the whole module went green, six passed, nothing said otherwise.

That is the sibling of a failure this repo has already shipped once -- a chain parse that reads a
file **with git conflict markers committed in it** and correctly reports ``0 breaks``, because
markers are not bullets. **Both are correct answers to the question the predicate encodes, and in
neither case was the question "is this file well-formed".**

The sentinel that detects it must be **strictly wider than the parser it guards**. Measured against
148 strict entries: the backticked-only form matches **99** (narrower -- it misses the second entry
format this file uses), and a ``(skill`` -dependent form matches **148** (equal, and it fails for
the same reason the parser would, so both go silent together). Only the backtick-optional form,
**153**, is wider along the axis that has actually varied.
"""

import os
import re
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)

sys.path.insert(0, HERE)

import test_changelog_version_chain as chain  # noqa: E402


def _repo_root():
    root = chain._find_repo_root()
    if root is None:
        pytest.skip("repo layout not present (installed-skill context)")
    return root


def test_the_sentinel_is_strictly_wider_than_the_parser_it_guards():
    """A sentinel narrower than its parser shares the parser's blind spot.

    This is the whole design constraint, and it is checked against the live file rather than
    asserted, because the margin is what makes the guard meaningful. A sentinel that merely EQUALS
    the parser's population would fall silent for the same reason the parser does -- two
    instruments, one blind spot, false corroboration inside a single test.
    """
    root = _repo_root()
    with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    strict = len(chain._entries())
    sentinel = len([l for l in lines if chain._ENTRY_SENTINEL_RE.match(l)])
    assert sentinel >= strict, (
        "the sentinel (%d) matches FEWER lines than the strict parser (%d), so a format it cannot "
        "see is a format the guard cannot report" % (sentinel, strict))

    narrow = re.compile(r"^\s*-\s\*\*`tableau-migration`")
    assert len([l for l in lines if narrow.match(l)]) < strict, (
        "the backticked-only form is expected to be NARROWER than the parser -- it is the "
        "measured counter-example this sentinel exists to avoid. If it is no longer narrower the "
        "file has one entry format again, and this test should be re-derived, not deleted")


def test_a_broken_parser_FAILS_while_an_absent_changelog_SKIPS():
    """Both directions, from one harness -- the conflation must not be rebuilt one level up.

    A guard that turns a silent skip into a *different* silent skip has changed nothing. So the
    same run establishes that a genuinely absent CHANGELOG still skips, and a present-but-unparsed
    one now fails, and that the two say different things.
    """
    root = _repo_root()
    path = os.path.join(root, "CHANGELOG.md")
    bak = path + ".vacuity-bak"

    # --- absent: must SKIP (this is the legitimate installed-skill case) ---
    shutil.copyfile(path, bak)
    try:
        os.remove(path)
        with pytest.raises(BaseException) as exc:
            chain._entries()
        assert "Skipped" in type(exc.value).__name__, (
            "an absent CHANGELOG must SKIP, not fail -- an installed skill has no repo root")
    finally:
        shutil.copyfile(bak, path)
        os.remove(bak)

    # --- present but unparsed: must FAIL ---
    original = chain._ENTRY_RE
    try:
        chain._ENTRY_RE = re.compile(r"(?P<frm>\A\Z_never)(?P<to>\A\Z_never)")
        with pytest.raises(AssertionError) as exc:
            chain._entries()
        msg = str(exc.value)
        assert "entry-shaped bullet" in msg and "parser is broken" in msg, msg
        assert "strict pattern" in msg, "the failure must print the pattern that stopped matching"
    finally:
        chain._ENTRY_RE = original

    # --- and the guard has not broken the normal path ---
    assert len(chain._entries()) > 0


def test_the_modules_own_checks_go_RED_not_green_when_the_pattern_breaks():
    """The failure must reach the actual test functions, not just the helper.

    Asserting only that ``_entries()`` raises would prove the helper is loud; it would not prove
    the gate fails. The injection that motivated this guard produced **6 passed** -- a green module
    over a parser that had matched nothing -- so the property to pin is that the module's real
    checks now REPORT, and that they report an assertion rather than a skip.

    Deliberately in-process. An earlier attempt routed this through a subprocess and an env-var
    hook, which would have SKIPPED whenever the hook was absent -- a test that cannot fail, added
    to fix a check that could not fail. Written down because it nearly shipped.
    """
    _repo_root()
    checks = [getattr(chain, n) for n in dir(chain)
              if n.startswith("test_") and callable(getattr(chain, n))]
    assert checks, "no test functions found in the chain module; this pin needs re-deriving"

    for fn in checks:                       # baseline: they must be green before injecting
        fn()

    original = chain._ENTRY_RE
    try:
        chain._ENTRY_RE = re.compile(r"(?P<frm>\A\Z_never)(?P<to>\A\Z_never)")
        outcomes = []
        for fn in checks:
            try:
                fn()
                outcomes.append((fn.__name__, "PASSED"))
            except AssertionError:
                outcomes.append((fn.__name__, "failed"))
            except BaseException as exc:
                outcomes.append((fn.__name__,
                                 "skipped" if "Skipped" in type(exc).__name__ else "error"))
    finally:
        chain._ENTRY_RE = original

    consuming = [(n, o) for n, o in outcomes if o != "PASSED"]
    assert consuming, "no check consumed the broken parser; this pin is measuring nothing"
    silent = [(n, o) for n, o in consuming if o == "skipped"]
    assert not silent, (
        "with the parser broken these checks SKIPPED instead of failing, which is "
        "indistinguishable from passing in a suite run:\n  "
        + "\n  ".join("%s -> %s" % p for p in silent))

