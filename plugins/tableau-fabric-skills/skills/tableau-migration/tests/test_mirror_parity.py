"""Guards the canonical <-> plugin mirror against silent drift.

Each skill ships twice: the canonical tree at ``skills/<name>`` and a
byte-identical mirror at ``plugins/tableau-fabric-skills/skills/<name>``
(the plugin/marketplace install path, a packaging convention mirrored from
``microsoft/skills-for-fabric``). The mirror is regenerated with ``robocopy /MIR``;
this test turns that manual ritual into a guarded artifact -- if any of the three
skill trees ever diverge, the suite fails instead of shipping inconsistent copies.

It runs from either tree: it walks up to the repository root (the directory that
contains BOTH ``skills`` and ``plugins``) and compares the two skill subtrees. When
that layout is absent (e.g. an installed-skill context with no ``plugins`` tree), the
test skips rather than fails.
"""

import hashlib
import os

import pytest

# Build artifacts that legitimately differ between trees (or don't exist in git).
_EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
# ``.log`` is a RUNTIME artifact, never skill content: the Hyper API drops a ``hyperd.log`` into the
# working directory, so any test or CLI run launched from a skill tree left one behind, ``robocopy
# /MIR`` copied it into the plugin package, and this test then failed on a file that is gitignored
# and ships in neither tree. Excluding it here keeps the guard honest about what actually ships.
_EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log")

_FALLBACK_SKILL_NAMES = (
    "tableau-datasource-profiler",
    "tableau-fabric-datasource-comparison",
    "tableau-mcp-landing-zone",
    "tableau-migration",
)


def _canonical_rel(skill):
    return os.path.join("skills", skill)


def _mirror_rel(skill):
    return os.path.join("plugins", "tableau-fabric-skills", "skills", skill)


def _find_repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    while True:
        if os.path.isdir(os.path.join(cur, "skills")) and os.path.isdir(
            os.path.join(cur, "plugins")
        ):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _discover_skills():
    """Every skill in the canonical tree, discovered rather than listed.

    A hardcoded roster silently stops guarding a skill the moment one is added -- which is exactly
    what happened to ``tableau-fabric-datasource-comparison``, mirrored but unchecked. Discovering
    the roster means a new skill is covered the day it lands. Falls back to the known names when the
    repo layout isn't present (installed-skill context), where the test skips anyway.
    """
    root = _find_repo_root()
    if root:
        skills_dir = os.path.join(root, "skills")
        found = sorted(
            name for name in os.listdir(skills_dir)
            if os.path.isfile(os.path.join(skills_dir, name, "SKILL.md"))
        )
        if found:
            return tuple(found)
    return _FALLBACK_SKILL_NAMES


_SKILL_NAMES = _discover_skills()


def _relevant(path_parts, filename):
    if filename.endswith(_EXCLUDE_SUFFIXES):
        return False
    return not any(part in _EXCLUDE_DIRS for part in path_parts)


def _snapshot(root):
    """Map each relevant file's tree-relative path -> sha256 of its bytes."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        rel_dir = os.path.relpath(dirpath, root)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        for name in filenames:
            if not _relevant(parts + [name], name):
                continue
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            with open(os.path.join(dirpath, name), "rb") as fh:
                out[rel.replace(os.sep, "/")] = hashlib.sha256(fh.read()).hexdigest()
    return out


@pytest.mark.parametrize("skill", _SKILL_NAMES)
def test_plugin_mirror_is_byte_identical_to_canonical(skill):
    root = _find_repo_root()
    if root is None:
        pytest.skip("repo root with both skills/ and plugins/ not found")

    canonical_dir = os.path.join(root, _canonical_rel(skill))
    mirror_dir = os.path.join(root, _mirror_rel(skill))
    if not os.path.isdir(mirror_dir):
        pytest.skip("plugin mirror tree not present for %s" % skill)

    canonical = _snapshot(canonical_dir)
    mirror = _snapshot(mirror_dir)

    missing = sorted(set(canonical) - set(mirror))
    extra = sorted(set(mirror) - set(canonical))
    differing = sorted(
        f for f in (set(canonical) & set(mirror)) if canonical[f] != mirror[f]
    )

    assert not missing, f"{skill}: present in canonical but missing from mirror: {missing}"
    assert not extra, f"{skill}: present in mirror but not in canonical: {extra}"
    assert not differing, f"{skill}: files whose bytes differ between trees: {differing}"
