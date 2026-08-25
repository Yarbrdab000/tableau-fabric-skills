"""No test body may be SWALLOWED by the test above it.

THE FAILURE THIS EXISTS FOR. An edit that replaces a ``def test_x():`` line -- a rename, a
reordering, an insertion whose ``old_str`` happened to end at that line -- deletes the header and
leaves the body behind. Those statements land INSIDE the preceding test function, at the same
indentation, and they keep running. If their assertions still hold, **the suite stays green and one
test has silently ceased to exist**.

That is the shape of every silent defect in this repo: the output met expectation, so nothing
objected. Here the "output" is the pass count, which is exactly the wrong signal -- orphaned
assertions pass just as happily inside whatever function absorbed them. **A green suite cannot tell
you a test still exists.** The only signal that can is the set of COLLECTED TEST IDs, or -- what this
module does, so it works without a baseline to diff against -- the structural fingerprint the lost
``def`` leaves behind.

MEASURED, TWICE, WITHIN AN HOUR:

  * A new test in ``test_compiler_routing.py`` swallowed the body of the test below it. It PASSED,
    because both sets of assertions were independently true. Only a collected-node-ID diff against
    the previous commit caught it -- the pass count went UP, so every count-based check was happy.
  * ``test_migrate_estate.py`` was already carrying one: the ``_definition_of_done`` precedence and
    ``_dod_banner`` assertions were running as trailing statements of
    ``test_dod_openability_failure_helper_tolerates_missing_and_ok``, a test about an unrelated
    helper. This gate found it on its first run; the repair is the sibling
    ``test_dod_status_precedence_and_warn_banner``.

THE FINGERPRINT, and why it is this one. Module-level definitions are separated by exactly two blank
lines, so when a ``def`` is eaten its body arrives after a run of >= 2 blank lines *inside* the
previous function. Legitimate code effectively never does this: measured over the whole suite the
predicate flagged **1 function in 4194**, and that one was a real swallowed test rather than a false
positive. A predicate is only worth trusting once its rate on known-good input is known, so that
number is the reason this gate is a hard assertion and not a warning.

Deliberately NOT a count or a manifest: an expected-count baseline needs updating on every added
test, which turns it into a rubber stamp, and it cannot say WHICH test vanished. This reads the
source and names the function and line.

Skips when the tests directory is absent, matching ``test_mirror_parity`` and
``test_changelog_version_chain``.
"""

import ast
import os

import pytest

_MAX_BLANK_RUN = 1          # >= 2 consecutive blank lines inside a test body is the fingerprint


def _tests_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    return here if os.path.isdir(here) else None


def _test_files():
    d = _tests_dir()
    if not d:
        return []
    return [os.path.join(d, f) for f in sorted(os.listdir(d))
            if f.startswith("test_") and f.endswith(".py")]


def _function_end(node):
    return max((getattr(n, "end_lineno", n.lineno) for n in ast.walk(node)
                if hasattr(n, "lineno")), default=node.lineno)


def _swallow_suspects(path):
    """``[(function, def_line, orphan_line, text, blank_run)]`` for one module."""
    with open(path, encoding="utf-8-sig") as fh:
        src = fh.read()
    lines = src.split("\n")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:                                   # pragma: no cover - defensive
        pytest.fail("%s does not parse: %s" % (os.path.basename(path), exc))

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_") or not node.body:
            continue
        run = 0
        for i in range(node.body[0].lineno - 1, min(_function_end(node), len(lines))):
            if lines[i].strip() == "":
                run += 1
                continue
            if run > _MAX_BLANK_RUN:
                out.append((node.name, node.lineno, i + 1, lines[i].strip()[:80], run))
                break
            run = 0
    return out


def test_no_test_body_was_swallowed():
    files = _test_files()
    if not files:                                                # pragma: no cover - layout guard
        pytest.skip("no tests directory in this layout")

    examined, violations = 0, []
    for path in files:
        with open(path, encoding="utf-8-sig") as fh:
            tree = ast.parse(fh.read())
        examined += sum(1 for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.name.startswith("test_"))
        for fn, def_ln, orphan_ln, text, run in _swallow_suspects(path):
            violations.append(
                "%s::%s (def line %d) -- %d blank lines then line %d: %s"
                % (os.path.basename(path), fn, def_ln, run, orphan_ln, text))

    # Prove the sweep actually reached the suite: a pass over ZERO functions is not a clean result,
    # it is a broken predicate, and it would look identical to success.
    assert examined > 100, (
        "only %d test functions were examined across %d files -- the sweep is not reaching the "
        "suite, so a clean result here would prove nothing" % (examined, len(files)))

    assert not violations, (
        "a test body appears to have been SWALLOWED by the function above it -- the lost `def` "
        "leaves its statements running inside the previous test, where they keep passing:\n  "
        + "\n  ".join(violations)
        + "\n\nRestore the missing `def test_...():` header. If the gap is deliberate, it still "
          "reads as a lost definition to every future editor -- close it to one blank line.")


def test_the_swallow_detector_can_actually_see_one():
    """Negative control: a section that cannot go red is indistinguishable from one that found
    nothing. Feed the detector a synthetic swallow and require it to fire, and a clean module and
    require it not to."""
    import tempfile

    swallowed = (
        "def test_a():\n"
        "    assert True\n"
        "\n"
        "\n"
        "    # body of the test whose def was eaten\n"
        "    assert 1 == 1\n"
    )
    clean = (
        "def test_a():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_b():\n"
        "    assert 1 == 1\n"
    )
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "test_bad.py")
        good = os.path.join(d, "test_good.py")
        for p, s in ((bad, swallowed), (good, clean)):
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write(s)
        assert _swallow_suspects(bad), "the detector did not see a synthetic swallowed body"
        assert not _swallow_suspects(good), "the detector fired on a clean module"
