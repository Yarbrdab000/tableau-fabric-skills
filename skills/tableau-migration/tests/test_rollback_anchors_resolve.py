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


def test_every_released_version_has_an_anchor(repo):
    """Every version in the CHANGELOG must HAVE a ``rollback/pre-v`` tag.

    The sibling test above proves each anchor that EXISTS is reachable. It cannot prove one exists,
    and absence is the failure this catches -- discovered the hard way.

    WHY AN ANCHOR VANISHES WITHOUT ANYONE MISREPORTING: ``git rev-parse --git-common-dir`` is the
    SAME ``.git`` for every worktree, so ``refs/tags`` is a single GLOBAL namespace shared by every
    parallel session. Two sessions that collide on a version number therefore also collide on its
    anchor name -- one slot, last writer wins. Observed live: session A cut
    ``rollback/pre-v2.211.0``; session B's identical ``git tag -a`` failed with "already exists",
    B read it as leftover from its own discarded renumber, deleted it and re-cut at B's commit;
    A later renumbered away from 2.211.0 and deleted that tag in cleanup -- by then B's. Each
    session destroyed the other's anchor while truthfully reporting its own as verified.

    Two rules follow, and the second is why this test exists:
      * never ``git tag -d`` an anchor you did not create (``%(taggerdate)`` tells you in one call);
      * a version with no anchor is a release with no rollback, so assert it rather than remember it.

    Reads the CHANGELOG's own ``(skill `A` -> `B`)`` chain as the roster of shipped versions, so the
    roster cannot drift from what was actually released.
    """
    import io
    import re

    path = os.path.join(repo, "CHANGELOG.md")
    if not os.path.isfile(path):
        pytest.skip("no CHANGELOG.md in this checkout")
    text = io.open(path, encoding="utf-8").read()
    released = []
    for m in re.finditer(r"\(skill\s+`(\d+\.\d+\.\d+)`\s*\u2192\s*`(\d+\.\d+\.\d+)`\)", text):
        if m.group(2) not in released:
            released.append(m.group(2))
    if not released:
        pytest.skip("no versioned CHANGELOG entries found")

    tags = set(_tag_commits(repo))
    missing = [v for v in released if ("rollback/pre-v" + v) not in tags]

    assert not missing, (
        "released version(s) with no rollback anchor -- `git reset --hard rollback/pre-vX.Y.Z` has "
        "nothing to resolve, so these releases cannot be backed out:\n  "
        + "\n  ".join(missing)
        + "\n\nrefs/tags is shared across ALL worktrees of this repo (one .git), so a parallel "
          "session can delete an anchor it believes is its own. Re-cut at the release's PARENT "
          "commit, and never delete an anchor you did not create.")


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
