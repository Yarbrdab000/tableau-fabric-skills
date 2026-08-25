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

_VERSION_PATH = "skills/tableau-migration/VERSION"


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


def _version_at(root, shas):
    """``{commit: VERSION-file-contents}`` for many commits, in ONE ``git cat-file --batch``.

    Parsed by DECLARED BYTE SIZE, never by counting lines. ``git cat-file --batch`` emits the
    ``<size>`` content bytes AND a trailing newline, so each entry occupies THREE lines, not two.
    A line-walking parser that advances by two desynchronises by one line per commit -- and that
    does not look like a parse error, because every anchor still receives a plausible semver, just
    the *next* one's. Measured while building this test: it reported 67 broken rollbacks against a
    repo whose anchors are in fact 306/306 clean. Read the size, trust nothing about line counts.
    """
    if not shas:
        return {}
    stdin = "".join("%s:%s\n" % (s, _VERSION_PATH) for s in shas).encode("utf-8")
    try:
        p = subprocess.run(["git", "cat-file", "--batch"], cwd=root, input=stdin,
                           capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return {}
    if p.returncode != 0:
        return {}
    buf, pos, out = p.stdout, 0, {}
    for sha in shas:
        nl = buf.find(b"\n", pos)
        if nl < 0:
            break
        head = buf[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        if len(head) < 3 or head[1] != "blob":
            out[sha] = None            # "<input> missing" -- header only, no body follows
            continue
        size = int(head[2])
        out[sha] = buf[pos:pos + size].decode("utf-8", "replace").strip()
        pos += size + 1
    return out


def test_every_anchor_predates_the_version_it_anchors(repo):
    """``VERSION`` at ``rollback/pre-vX.Y.Z`` must be STRICTLY LESS than ``X.Y.Z``.

    The two gates above ask whether an anchor EXISTS and whether git can RESOLVE it. Neither asks
    whether it points anywhere useful, and that is the gap where the damage happens: an anchor
    re-pointed at a *colliding session's* commit is still present and still reachable, so both pass
    -- while ``git reset --hard rollback/pre-vX.Y.Z`` now lands you on work that already CONTAINS
    X.Y.Z, or on a stranger's branch entirely. Rollback silently stops meaning rollback.

    WHY THIS IS THE RIGHT INVARIANT AND "anchor == parent(bump)" IS NOT. The obvious formulation --
    the anchor must be the parent of the commit that bumped VERSION -- is too strict, and measurably
    so: 4 of 268 anchors violate it legitimately because the parent is a PR **merge** commit, two of
    them with a byte-identical tree. Rollback is not a claim about commit identity. It is a claim
    about the STATE you land in, and the state is exactly what the VERSION stamp records. Comparing
    stamps tolerates merges, rebases and renumbers, and still catches every re-point that matters.

    Measured across all history at the time of writing: 306 anchors, 306 satisfy this, 0 violate it.
    So it is an exact property of the repo, not an aspiration being retrofitted.

    HOW TO PROBE THIS GATE WITHOUT DESTROYING WHAT IT PROTECTS. Verified red by re-pointing a real
    anchor at HEAD -- which meant deleting the real one first, in the single shared ``refs/tags``
    namespace, i.e. committing the exact damage this test exists to catch. It was recoverable only
    because that tag happened to be on ``origin``; an unpushed one would have been gone. The
    additive probe is strictly better and just as red: **create a bogus low-numbered anchor**
    (``git tag rollback/pre-v2.0.0-PROBE`` is not enough -- the label suffix is skipped by design;
    use a bare unused version) pointing at HEAD, run, then delete only the tag you created. Never
    re-point an existing anchor to test something.

    Only bare ``rollback/pre-vX.Y.Z`` names are checked. A labelled anchor
    (``rollback/pre-v1.9.0-comparison``) belongs to a different skill with its own VERSION stamp, and
    comparing it against ``tableau-migration``'s would be a category error -- the same class of
    mistake as the desync documented in ``_version_at``, where a plausible-looking number came from
    the wrong place entirely.
    """
    import re

    tags = _tag_commits(repo)
    bare = {t: t[len("rollback/pre-v"):] for t in tags
            if re.match(r"^rollback/pre-v\d+\.\d+\.\d+$", t)}
    if not bare:
        pytest.skip("no bare rollback anchors in this checkout")

    shas = [tags[t] for t in bare]
    at = _version_at(repo, shas)
    if not at:
        pytest.skip("could not read VERSION at the anchor commits")

    def key(v):
        return [int(x) for x in v.split(".")]

    violations = []
    for tag, ver in sorted(bare.items(), key=lambda kv: key(kv[1])):
        got = at.get(tags[tag])
        if not got or not re.match(r"^\d+\.\d+\.\d+$", got):
            continue                   # predates the VERSION file; nothing to compare
        if key(got) >= key(ver):
            violations.append("%s -> %s, but VERSION there is already %s"
                              % (tag, tags[tag][:8], got))

    assert not violations, (
        "rollback anchor(s) do NOT predate the version they anchor, so resetting to them does not "
        "undo that release:\n  " + "\n  ".join(violations)
        + "\n\nUsual cause: a colliding release re-pointed the tag at its own commit. refs/tags is "
          "one namespace shared by every worktree, so the tag NAME can end up owned by one session "
          "while the VERSION it anchors belongs to another. Re-cut at a commit whose VERSION is "
          "lower than the anchored version -- normally the release's parent.")


# --- the anchor's MEANING, not just its existence -------------------------------------------
# An anchor cut on a lane branch is accurate about the history it was cut in, and can stop being
# accurate the moment that history is merged. Measured on the integration branch:
# `rollback/pre-v2.293.0` points at a commit stamped 2.270.0 -- true on that lane, where the state
# before 2.293.0 really was 2.270.0 -- but 2.291.0 and 2.292.0 now sit between. Resetting there
# lands TWO RELEASES SHORT of what the tag claims, and every gate above passes: the tag exists, it
# resolves, its commit is reachable, and 2.270.0 is dutifully "strictly less than" 2.293.0.
#
# WHY THIS GATE IS BUILDABLE AND ITS PREDECESSOR WAS NOT. The obvious formulation -- "every anchor
# must name a version that exists" -- was measured and REJECTED: 244 of 319 anchors fail it against
# the CHANGELOG (which retains ~75 versions by design), and 35 still fail against every VERSION ever
# stamped on any ref. Those 35 are CORRECT behaviour: `rollback/pre-v2.216.0` .. `pre-v2.224.0` all
# point at one commit, all cut inside one minute -- a whole block claimed under the old block rule
# and never shipped. A claimed-but-unused anchor and a renumbered-away anchor are indistinguishable
# by construction, so that gate could only have shipped with a 35-entry exceptions file that grew
# with every claim.
#
# This one judges CHANGELOG ENTRIES, not anchors. An orphan block anchor has no entry, so it is
# never judged -- the exclusion is BY CONSTRUCTION rather than by allowlist, which is the whole
# difference between a gate and a gate with a rotting exceptions file.
#
# WHAT IT DOES NOT CATCH, stated so the coverage is not overclaimed: a version RENUMBERED AWAY has
# no CHANGELOG entry either, so it is not judged here. This catches anchors whose meaning went
# STALE, not anchors that are MISSING. Overstating a gate's reach is the same defect class as a
# `rebuilt` status that only means "the emitter did not raise".
#
# FIXING a violation is an INTEGRATOR operation: re-point the boundary anchor at the merged parent.
# That is precisely the act a session must never perform on another session's tag, and it is
# legitimate for whoever owns the merge, because the merge is what changed the anchor's meaning.
# Whoever re-chains the CHANGELOG must also re-point the anchor; those two halves have always been
# one operation and nobody had noticed.
#
# The ledger below is debt, not exemption. Each entry is a real interleave boundary whose anchor
# still means what it meant on its lane. Unlike the 35 orphans it is finite and shrinking.
_INTERLEAVE_DEBT_REMOVED = """RETIRED 2026-08-25, and the removal is the record.

This set parked anchors failing a stamp-equality assertion (`VERSION` at the anchor must equal the
predecessor the CHANGELOG declares). That assertion has since been measured wrong, and its
remediation advice -- "re-point the anchor at the merged parent" -- was followed and DAMAGED six
anchors: each was moved onto a parallel lane, so `git reset --hard` on it no longer landed on any
history leading to its own release. All six archived originals satisfied the ancestry invariant that
replaced it; all six re-points broke it, while the stamp gate went green throughout.

The ledger itself was the tell, in hindsight. It existed because the assertion had exceptions, and it
was designed to shrink as they were "paid". They were paid, the count went 4 -> 1, and every payment
made the repo worse. A gate that needs no exceptions is usually stating the property; one that
accumulates them is usually stating a proxy for it."""


_HISTORIC_NOTE = """The interleave-debt ledger that used to live here is gone, and its absence is
the point. It existed to park anchors that failed the stamp-equality assertion -- an assertion since
measured wrong, whose remediation advice damaged six anchors. The ancestry invariant that replaced it
needs no ledger: every one of the 139 chain pairs satisfies it, including the two historic
double-introduced versions (1.23.0, 1.25.0) that any introducer-based formulation would have needed
to except. A gate that needs no exceptions is usually stating the property; one that accumulates them
is usually stating a proxy."""


def _release_commits(root):
    """``{version: sha}`` for each release commit reachable from HEAD, newest wins."""
    import re

    out = {}
    for line in _git(root, "log", "HEAD", "--format=%H%x09%s").splitlines():
        sha, _, subj = line.partition("\t")
        m = re.match(r"tableau-migration (\d+\.\d+\.\d+):", subj)
        if m:
            out.setdefault(m.group(1), sha)
    return out


def _is_ancestor(root, maybe_ancestor, descendant):
    """True iff ``maybe_ancestor`` is reachable from ``descendant``. ``False`` if git cannot answer.

    Uses git's own reachability rather than a hand-rolled walk. A peer session BFS'd this by hand and
    got a confidently wrong answer, because paths go AROUND the anchor into all history -- the
    reported figure was absurd enough to indict the probe rather than the repo, which is the only
    reason it was caught. Let the tested thing judge.

    Returning ``False`` on an unanswerable question is deliberate and is NOT fail-closed in the usual
    sense: callers use this both to assert an invariant AND to decide whether the question is
    decidable here, and in the latter case a git failure that looked like "reachable" would resurrect
    the false positive the skip exists to remove.
    """
    try:
        p = subprocess.run(["git", "merge-base", "--is-ancestor", maybe_ancestor, descendant],
                           cwd=root, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def _chain_pairs(repo):
    """``[(declared_predecessor, produced_version), ...]`` from the CHANGELOG's own chain.

    Accepts BOTH arrow forms. The file carries two entry formats -- 88 newer entries using a
    Unicode arrow and 51 older ones using ASCII ``->`` -- and a Unicode-only pattern judged 88 of
    139 while reporting nothing amiss. That is a verifier narrower than its subject, which does not
    merely miss the region it cannot see: it CERTIFIES it. Write the predicate from the subject's
    grammar, which is what the chain gate itself accepts.
    """
    import io
    import re

    path = os.path.join(repo, "CHANGELOG.md")
    if not os.path.isfile(path):
        return []
    text = io.open(path, encoding="utf-8").read()
    return [(m.group(1), m.group(2)) for m in
            re.finditer(r"\(skill\s+`(\d+\.\d+\.\d+)`\s*(?:->|\u2192)\s*`(\d+\.\d+\.\d+)`\)", text)]


def test_each_anchor_is_an_ancestor_of_the_release_it_anchors(repo):
    """``rollback/pre-vX`` must be an ANCESTOR of X's own release commit.

    This replaces an earlier assertion that ``VERSION`` at the anchor equal the predecessor the
    CHANGELOG declares. That assertion was measured wrong, and its remediation advice actively
    damaged the repo: following it re-pointed six anchors onto parallel lanes, so
    ``git reset --hard rollback/pre-vX`` no longer landed on any history leading to X. All six
    archived originals satisfied THIS invariant; all six re-points broke it. The stamp gate went
    green while doing it -- optimising for a check moved the property the check stood in for.

    The reason the two disagree is that an interleaved merge leaves the repo carrying TWO orders:

    * the NARRATIVE order -- the CHANGELOG chain, which is what a reader and the version gates use;
    * the CAUSAL order -- git ancestry, which is what ``git reset --hard`` actually obeys.

    "One release before" is well defined in each and they name DIFFERENT commits. A stamp comparison
    silently picks the narrative order for a tag whose only executable meaning is causal.

    Stated in ancestry alone, this needs no choice between them, and needs no exception ledger: it
    never asks a version to have a unique introducer, so the two historic double-introduced versions
    (``1.23.0``, ``1.25.0``) are simply not a special case.

    Measured over the full history at the time of writing: 139 chain pairs judged, 139 satisfy.
    """
    pairs = _chain_pairs(repo)
    if not pairs:
        pytest.skip("no versioned CHANGELOG entries in this checkout")
    releases = _release_commits(repo)
    tags = _tag_commits(repo)

    judged, violations = 0, []
    for _pred, made in pairs:
        anchor = tags.get("rollback/pre-v" + made)
        release = releases.get(made)
        if not anchor or not release:
            continue                      # absence is the sibling gate's business, not this one
        if not _is_ancestor(repo, anchor, "HEAD"):
            # THE ANCHOR BELONGS TO A HISTORY THIS BRANCH CANNOT SEE, so the question is undecidable
            # here. Ported from a peer session's fix to the gate this one replaced -- the same defect
            # was about to ship in the replacement.
            #
            # `refs/tags` is SHARED across every worktree; the CHANGELOG chain driving this loop is
            # branch-local. So a lane branch sees other lanes' anchors beside its own chain, and an
            # anchor it cannot reach can never be an ancestor of anything -- it would report a
            # violation caused entirely outside the branch. Observed live: an anchor re-pointed by
            # the integrator turned a lane red seconds later, through no fault of the lane.
            #
            # Note this is the OPPOSITE call from the sibling gate that compares an anchor against
            # its OWN commit's VERSION. That comparison is decidable from anywhere, so filtering it
            # by ancestry would discard signal -- and cross-lane tag hijacking is precisely what it
            # exists to catch. The distinction is what the gate compares against: a branch-local
            # file (skip what you cannot reach) or the anchor's own commit (judge everything).
            continue
        judged += 1
        if not _is_ancestor(repo, anchor, release):
            violations.append(
                "%s: anchor rollback/pre-v%s -> %s is NOT an ancestor of its own release %s"
                % (made, made, anchor[:8], release[:8]))

    assert judged, "judged no anchor/release pair -- a sweep that reached zero looks like success"
    assert not violations, (
        "anchor(s) do not sit on the history that leads to the release they name, so "
        "`git reset --hard` on them lands on a parallel lane:\n  " + "\n  ".join(violations)
        + "\n\nDo NOT 'fix' this by re-pointing at whatever commit is stamped with the declared "
          "predecessor -- that is the narrative order, and doing it is what broke six anchors here. "
          "An anchor is correct where it was CUT; if it looks wrong after an interleaved merge, the "
          "CHANGELOG chain and git ancestry have diverged and the chain is the thing that moved.")


def test_the_ancestry_detector_can_actually_see_a_violation(repo):
    """Negative control: the invariant above spends its life reporting silence, so prove it fires.

    A check that cannot go red is indistinguishable from one that found nothing -- and this one
    currently passes on every pair, which is exactly the condition under which a broken detector is
    invisible. Feeds it a known-bad pairing (an anchor from an unrelated release) and requires a
    violation, and a known-good one and requires none.

    Additive and destroys nothing: it never writes a ref, it evaluates the predicate directly.
    """
    releases = _release_commits(repo)
    tags = _tag_commits(repo)
    pairs = _chain_pairs(repo)
    if not pairs or len(releases) < 3:
        pytest.skip("not enough history in this checkout to construct a control")

    good = [(made, tags.get("rollback/pre-v" + made), releases.get(made))
            for _p, made in pairs
            if tags.get("rollback/pre-v" + made) and releases.get(made)]
    assert good, "no anchor/release pair available -- the control cannot run"

    made, anchor, release = good[0]
    assert _is_ancestor(repo, anchor, release), (
        "control precondition failed: %s's own anchor is not an ancestor of its release" % made)

    # A release far newer than the anchor's own is not an ancestor of it -- the known-bad pairing.
    newest = max(releases, key=lambda v: tuple(int(p) for p in v.split(".")))
    oldest_anchor = min(
        (m for m, a, r in good), key=lambda v: tuple(int(p) for p in v.split(".")))
    bogus = tags.get("rollback/pre-v" + newest)
    target = releases.get(oldest_anchor)
    if not bogus or not target or bogus == tags.get("rollback/pre-v" + oldest_anchor):
        pytest.skip("could not construct a distinct bad pairing in this checkout")

    assert not _is_ancestor(repo, bogus, target), (
        "the detector did NOT fire on a deliberately wrong pairing (anchor of %s against the release "
        "of %s) -- it cannot distinguish a violation from a clean result, so its green runs mean "
        "nothing" % (newest, oldest_anchor))