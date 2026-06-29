#!/usr/bin/env python3
"""CI guardrail: a PR that changes a skill's shipped payload must bump that
skill's ``VERSION`` (semver, strictly increasing).

Why this exists
---------------
Each skill carries a ``skills/<name>/VERSION`` stamp. The skills' self-update
runbook only reinstalls when the *published* VERSION is strictly newer than the
*installed* one. So if a behavioural change lands on ``main`` without bumping
the stamp, every client believes it is already current and silently skips the
update -- exactly how the date-relationship fix shipped but stayed invisible to
the updater. This check makes the (previously manual, easily-forgotten) "bump
the stamp" step mandatory and automatic.

Policy
------
*Payload* = any file under ``skills/<name>/`` or its byte-identical plugin
mirror ``plugins/tableau-fabric-skills/skills/<name>/``, EXCEPT that skill's own
``tests/`` directory and the ``VERSION`` file itself. A test-only change or a
VERSION-only change therefore does not require a bump.

For every skill whose payload changed in the PR, ``skills/<name>/VERSION`` must
also have changed and its semver value must be strictly greater than the base.
A brand-new skill (no VERSION at the base ref) only needs a valid VERSION at
HEAD.

Usage
-----
    check_version_bumps.py --base <git-ref> --head <git-ref>
    check_version_bumps.py --self-test

Exit codes: 0 = OK, 1 = a required bump is missing/invalid, 2 = usage error.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

CANON_PREFIX = "skills/"
MIRROR_PREFIX = "plugins/tableau-fabric-skills/skills/"


def parse_semver(text):
    """Return a comparable ``(major, minor, patch)`` tuple from a semver string.

    Tolerates a leading ``v`` and surrounding whitespace, and ignores any
    ``-pre``/``+build`` suffix. Raises ``ValueError`` on a non-numeric core so a
    typo cannot silently pass the ``>`` comparison.
    """
    core = text.strip().lstrip("vV").split("+", 1)[0].split("-", 1)[0]
    parts = core.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a numeric x.y.z semver: {text.strip()!r}")
    return tuple(int(p) for p in parts)


def skill_of_path(path):
    """Map a repo-relative path to its skill name, or ``None`` if it is not
    under a skill. Handles both the canonical and the plugin-mirror layout."""
    for prefix in (MIRROR_PREFIX, CANON_PREFIX):
        if path.startswith(prefix):
            name = path[len(prefix):].split("/", 1)[0]
            if name:
                return name
    return None


def is_payload_path(path, skill):
    """True if ``path`` is shipped payload for ``skill`` -- i.e. a change that
    should force a VERSION bump. Excludes the skill's ``tests/`` dir and its
    ``VERSION`` file (under either the canonical or the mirror prefix)."""
    for prefix in (MIRROR_PREFIX, CANON_PREFIX):
        head = prefix + skill + "/"
        if path.startswith(head):
            rest = path[len(head):]
            if rest == "VERSION":
                return False
            if rest == "tests" or rest.startswith("tests/"):
                return False
            return True
    return False


def skills_with_payload_changes(changed_files):
    """Set of skill names whose shipped payload changed in ``changed_files``."""
    out = set()
    for path in changed_files:
        skill = skill_of_path(path)
        if skill and is_payload_path(path, skill):
            out.add(skill)
    return out


def evaluate(changed_files, base, head, read_version):
    """Pure policy core (no git, no I/O), so it is unit-testable.

    ``read_version(ref, skill) -> str | None`` returns the raw VERSION contents
    at a ref, or ``None`` if absent. Returns ``(changed_skills, violations)``
    where ``violations`` is a list of human-readable strings (empty == OK).
    """
    violations = []
    changed_skills = sorted(skills_with_payload_changes(changed_files))
    for skill in changed_skills:
        base_raw = read_version(base, skill)
        head_raw = read_version(head, skill)
        if head_raw is None:
            violations.append(
                f"{skill}: shipped payload changed but skills/{skill}/VERSION "
                f"is missing at HEAD -- add a VERSION stamp."
            )
            continue
        try:
            head_ver = parse_semver(head_raw)
        except ValueError as exc:
            violations.append(f"{skill}: VERSION at HEAD is invalid ({exc}).")
            continue
        if base_raw is None:
            # Brand-new skill: no base stamp to compare against, a valid HEAD
            # VERSION is sufficient.
            continue
        try:
            base_ver = parse_semver(base_raw)
        except ValueError as exc:
            violations.append(f"{skill}: VERSION at base is invalid ({exc}).")
            continue
        if not head_ver > base_ver:
            shown_base = ".".join(map(str, base_ver))
            shown_head = ".".join(map(str, head_ver))
            violations.append(
                f"{skill}: shipped payload changed but VERSION was not bumped "
                f"({shown_base} -> {shown_head}). Increase skills/{skill}/VERSION "
                f"(and re-mirror the plugin copy) so the self-update runbook ships it."
            )
    return changed_skills, violations


def _git(args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _changed_files(base, head):
    # Three-dot: diff HEAD against the merge-base of base and head -- exactly the
    # set of files this PR introduces, regardless of how far base has advanced.
    res = _git(["diff", "--name-only", f"{base}...{head}"])
    if res.returncode != 0:
        raise RuntimeError(f"git diff failed: {res.stderr.strip()}")
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]


def _version_at(ref, skill):
    res = _git(["show", f"{ref}:skills/{skill}/VERSION"])
    if res.returncode != 0:
        return None  # file absent at that ref
    return res.stdout


def _run(base, head):
    changed = _changed_files(base, head)
    changed_skills, violations = evaluate(changed, base, head, _version_at)

    if not changed_skills:
        print("version-bump check: no skill payload changed -- nothing to enforce.")
        return 0

    print(f"version-bump check: skill payload changed for: {', '.join(changed_skills)}")
    for skill in changed_skills:
        head_raw = _version_at(head, skill)
        shown = head_raw.strip() if head_raw else "(missing)"
        print(f"  - {skill}: VERSION at HEAD = {shown}")

    if violations:
        print("\nFAILED -- a required VERSION bump is missing or invalid:\n")
        for v in violations:
            print(f"  * {v}")
        print(
            "\nFix: bump the listed skill's skills/<name>/VERSION (semver, strictly "
            "increasing), mirror it into plugins/tableau-fabric-skills/skills/<name>/"
            "VERSION, and add a CHANGELOG entry."
        )
        return 1

    print("\nOK -- every changed skill bumped its VERSION.")
    return 0


def _self_test():
    """Synthetic checks for the pure core. No git required."""
    def reader(table):
        return lambda ref, skill: table.get((ref, skill))

    cases = []

    # 1. payload changed, VERSION not bumped -> violation
    t = {("B", "tableau-migration"): "1.10.0\n", ("H", "tableau-migration"): "1.10.0\n"}
    cs, v = evaluate(["skills/tableau-migration/scripts/x.py"], "B", "H", reader(t))
    cases.append(("payload+no-bump", cs == ["tableau-migration"] and len(v) == 1))

    # 2. payload changed AND bumped -> ok
    t = {("B", "tableau-migration"): "1.10.0", ("H", "tableau-migration"): "1.11.0"}
    cs, v = evaluate(
        ["skills/tableau-migration/scripts/x.py", "skills/tableau-migration/VERSION"],
        "B", "H", reader(t),
    )
    cases.append(("payload+bump", cs == ["tableau-migration"] and v == []))

    # 3. tests-only change -> not payload, ok
    cs, v = evaluate(["skills/tableau-migration/tests/test_x.py"], "B", "H", reader({}))
    cases.append(("tests-only", cs == [] and v == []))

    # 4. VERSION-only change -> not payload, ok
    cs, v = evaluate(["skills/tableau-migration/VERSION"], "B", "H", reader({}))
    cases.append(("version-only", cs == [] and v == []))

    # 5. mirror payload changed AND bumped -> ok (mirror path maps to same skill)
    t = {("B", "tableau-migration"): "1.10.0", ("H", "tableau-migration"): "1.11.0"}
    cs, v = evaluate(
        ["plugins/tableau-fabric-skills/skills/tableau-migration/scripts/x.py"],
        "B", "H", reader(t),
    )
    cases.append(("mirror+bump", cs == ["tableau-migration"] and v == []))

    # 6. mirror tests-only -> not payload, ok
    cs, v = evaluate(
        ["plugins/tableau-fabric-skills/skills/tableau-migration/tests/test_x.py"],
        "B", "H", reader({}),
    )
    cases.append(("mirror-tests-only", cs == [] and v == []))

    # 7. payload changed but VERSION missing at HEAD -> violation
    t = {("B", "foo"): "1.0.0"}
    cs, v = evaluate(["skills/foo/scripts/x.py"], "B", "H", reader(t))
    cases.append(("missing-head-version", cs == ["foo"] and len(v) == 1))

    # 8. downgrade -> violation
    t = {("B", "foo"): "1.11.0", ("H", "foo"): "1.10.0"}
    cs, v = evaluate(["skills/foo/scripts/x.py"], "B", "H", reader(t))
    cases.append(("downgrade", len(v) == 1))

    # 9. brand-new skill (no base VERSION) with payload + a HEAD VERSION -> ok
    t = {("H", "newskill"): "1.0.0"}
    cs, v = evaluate(["skills/newskill/scripts/x.py"], "B", "H", reader(t))
    cases.append(("new-skill", cs == ["newskill"] and v == []))

    # 10. two skills, one bumped one not -> exactly one violation, for "b"
    t = {
        ("B", "a"): "1.0.0", ("H", "a"): "1.1.0",
        ("B", "b"): "2.0.0", ("H", "b"): "2.0.0",
    }
    cs, v = evaluate(
        ["skills/a/scripts/x.py", "skills/b/resources/y.md"], "B", "H", reader(t),
    )
    cases.append(("mixed-two-skills", cs == ["a", "b"] and len(v) == 1 and v[0].startswith("b:")))

    # 11. semver compares numerically, not lexically (1.9.0 < 1.10.0)
    cases.append(("semver-numeric", parse_semver("1.9.0") < parse_semver("1.10.0")))

    ok = True
    for name, passed in cases:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("self-test:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Enforce per-skill VERSION bumps on PRs.")
    ap.add_argument("--base", help="base git ref (PR target)")
    ap.add_argument("--head", help="head git ref (PR source)")
    ap.add_argument("--self-test", action="store_true", help="run synthetic checks and exit")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.base or not args.head:
        ap.error("--base and --head are required unless --self-test is given")
    try:
        return _run(args.base, args.head)
    except RuntimeError as exc:
        print(f"version-bump check: internal error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
