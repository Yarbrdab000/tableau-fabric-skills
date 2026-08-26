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
import inspect

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


# TRIMMED ON INTEGRATION. The parameter-dispatcher... no: the measure-value-colouring session
# shipped this file with three tests; two of them duplicate coverage that landed independently
# in test_changelog_version_chain (test_a_broken_parser_fails_rather_than_skipping and
# test_the_sentinel_is_wider_than_the_parser_it_guards) and were coupled to that lanes exact
# assertion wording. Only the third is unique, and it is the one that matters: it proves the
# failure reaches the MODULES OWN CHECKS rather than only the helper.


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
    # Only functions pytest could call with no fixtures. The chain module also carries tests taking
    # ``tmp_path`` / ``monkeypatch``, and calling those bare raises TypeError rather than exercising
    # anything -- which would make this pin fail for a reason unrelated to the property it guards.
    # Filtering by signature rather than by name keeps it working as that module grows.
    checks = []
    for name in dir(chain):
        if not name.startswith("test_"):
            continue
        fn = getattr(chain, name)
        if not callable(fn):
            continue
        try:
            required = [p for p in inspect.signature(fn).parameters.values()
                        if p.default is inspect.Parameter.empty
                        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)]
        except (TypeError, ValueError):
            continue
        if not required:
            checks.append(fn)
    assert checks, "no fixture-free test functions found in the chain module; this pin needs re-deriving"

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

