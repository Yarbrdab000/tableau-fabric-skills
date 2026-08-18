"""Every ``rollback/pre-vX.Y.Z`` anchor must name a commit that still exists.

The versioning ritual has four artifacts and, until now, a gate on three of them: the CHANGELOG
chain is tested, the ``VERSION`` stamp is tested, mirror parity is tested -- and the rollback anchor
is just a tag someone remembered to move. It was the only step with nothing checking it.

WHY AN ORPHANED ANCHOR IS WORSE THAN A MISSING ONE: it looks usable. ``git reset --hard
rollback/pre-vX.Y.Z`` SUCCEEDS against a tag pointing at an unreachable commit, lands you on a
detached orphan, and nothing warns you. A missing anchor fails loudly and you go and find the right
commit. Observed live: a renumber that was discarded left ``rollback/pre-v2.195.0`` and
``pre-v2.196.0`` pointing at commits (``7365169``, ``c1ad140``) no branch contained.

THE INVARIANT IS *REACHABLE*, NOT *ON MAIN*. An anchor cut for work still in flight legitimately
points at a commit that has not merged yet -- that is the normal state for the duration of a branch,
and asserting "ancestor of main" would fail every anchor between cutting it and landing the release.
What can never be legitimate is an anchor whose commit no ref reaches at all: rollback to it is
impossible, and git will not say so.

Cheap by construction: one ``git rev-list`` builds the reachable set, then each tag is a set
membership test. Skips rather than fails outside a git checkout, matching ``test_mirror_parity``.

``--branches --remotes``, NOT ``--all``: ``--all`` includes ``refs/tags``, so every anchor makes its
own target reachable and the check becomes tautological. The first version used ``--all`` and passed
against a deliberately-orphaned anchor -- a gate that could never fail. Found by probing it with a
real orphan rather than trusting that it worked, which is the only reason it is not still wrong.
"""
import os
import subprocess

import pytest


def _repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    while True:
        if os.path.isdir(os.path.join(cur, ".git")) or os.path.isfile(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _git(root, *args):
    try:
        p = subprocess.run(["git"] + list(args), cwd=root, capture_output=True, text=True,
                           timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout


@pytest.fixture(scope="module")
def repo():
    root = _repo_root()
    if root is None:
        pytest.skip("not a git checkout (installed-skill context)")
    if _git(root, "rev-parse", "--git-dir") is None:
        pytest.skip("git unavailable")
    return root


def _tag_commits(root):
    """``{tag: commit_sha}`` for every rollback anchor, in ONE git call.

    ``git show-ref --tags -d`` emits both the tag object and, for an annotated tag, a ``^{}`` line
    holding the COMMIT it dereferences to -- which is the one we want. Batched deliberately: the
    first version of this test called ``git rev-parse`` per tag and took 39s for 245 anchors,
    almost entirely Windows process-spawn cost. One call is ~1s.
    """
    out = _git(root, "show-ref", "--tags", "-d") or ""
    tags = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        sha, ref = parts[0], parts[1].strip()
        if not ref.startswith("refs/tags/rollback/pre-v"):
            continue
        deref = ref.endswith("^{}")
        name = ref[len("refs/tags/"):]
        if deref:
            name = name[:-3]
            tags[name] = sha          # dereferenced commit always wins
        else:
            tags.setdefault(name, sha)
    return tags


def test_every_rollback_anchor_names_a_reachable_commit(repo):
    tags = _tag_commits(repo)
    if not tags:
        pytest.skip("no rollback anchors in this checkout")

    reachable = set((_git(repo, "rev-list", "--branches", "--remotes") or "").split())
    assert reachable, "could not enumerate reachable commits"

    orphaned = ["%s -> %s (no ref reaches it)" % (t, sha[:8])
                for t, sha in sorted(tags.items()) if sha not in reachable]

    assert not orphaned, (
        "rollback anchor(s) point at commits nothing reaches. `git reset --hard <tag>` would "
        "SUCCEED and land on a detached orphan, which is why this is worse than a missing anchor:\n  "
        + "\n  ".join(orphaned))


def test_anchor_names_parse_as_semver(repo):
    """A malformed anchor name is unfindable by the runbook's own convention.

    An optional trailing ``-<label>`` is permitted because the repo already uses it
    (``rollback/pre-v1.9.0-comparison``) -- the first draft of this test forbade it and failed on
    real history, which is the test inventing a convention rather than checking one.
    """
    import re

    tags = _tag_commits(repo)
    if not tags:
        pytest.skip("no rollback anchors in this checkout")
    pat = re.compile(r"^rollback/pre-v\d+\.\d+\.\d+(-[A-Za-z0-9._-]+)?$")
    bad = sorted(t for t in tags if not pat.match(t))
    assert not bad, "anchor names must be rollback/pre-vX.Y.Z[-label]: %s" % bad
