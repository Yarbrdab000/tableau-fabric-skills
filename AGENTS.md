# Agent instructions — tableau-fabric-skills

Guidance for AI coding agents (GitHub Copilot CLI, Claude Code, Cursor, Windsurf, and similar)
working in this repository. Human contributors should read it too. The tool-specific files
(`CLAUDE.md`, `.cursorrules`, `.windsurfrules`) repeat the critical rules and point here; this
file is the source of truth.

## Install / consume (for agents)

To make these skills actually load in a client, register the **plugin** — do **not** copy folders
into `~/.copilot/skills/` (current GitHub Copilot CLI does not auto-scan it, so it no-ops
silently):

```
/plugin marketplace add Yarbrdab000/tableau-fabric-skills
/plugin install tableau-fabric-skills@tableau-collection
```

Start a new session, then verify with `/plugin list` (expect `tableau-fabric-skills`) and
`/skills list`. Full details / uninstall: [`INSTALL.md`](INSTALL.md) / [`UNINSTALL.md`](UNINSTALL.md).

## What this repository is

`tableau-fabric-skills` is a standalone collection of three install-and-go agent skills that
move Tableau assets to Microsoft Fabric / Power BI:

- **`tableau-datasource-profiler`** — read-only profiling and migration-readiness assessment of
  a published Tableau datasource (Tableau REST + Metadata API + optional VizQL Data Service).
- **`tableau-mcp-landing-zone`** — deploy the official Tableau MCP server behind an auth sidecar
  on Azure Container Apps (plus a local-dev route) for Copilot / Copilot Studio.
- **`tableau-migration`** — rebuild Tableau datasources as Fabric / Power BI semantic models
  (typed TMDL, a deterministic calc-to-DAX translator, storage-mode auto-select).

It is developed standalone; eventual contribution to `microsoft/skills-for-fabric` is a target
but not a current dependency.

## Repository layout

- `skills/<name>/` — the canonical skill sources (each has `SKILL.md`, `resources/`, and usually
  `scripts/`).
- `plugins/tableau-fabric-skills/skills/<name>/` — **byte-identical mirror copies** of each
  skill, used by the plugin/marketplace install path.
- `.claude-plugin/marketplace.json`, `.github/plugin/marketplace.json`,
  `plugins/tableau-fabric-skills/.claude-plugin/plugin.json` — packaging manifests.
- Root docs: `README.md`, `CLEANROOM.md`, `THIRD_PARTY_NOTICES.md`, `CHANGELOG.md`, `SECURITY.md`.

### Fractal packaging rule (important)

Every file under `skills/<name>/` is duplicated under
`plugins/tableau-fabric-skills/skills/<name>/`. **If you edit a canonical skill file, re-mirror
it into the plugin copy before committing**, excluding caches:

```
robocopy "skills\<name>" "plugins\tableau-fabric-skills\skills\<name>" /MIR /XD __pycache__ .pytest_cache /XF *.pyc *.log /NFL /NDL /NJH /NJS /NP
```

(robocopy exit codes 0–7 are success; 8 or higher is an error.) Root files such as this one are
not mirrored.

## Environment

- Windows + PowerShell. Do not use PowerShell 7-only syntax (`&&`, `||`, `??`, `?.`). PowerShell
  has no heredocs — write a temp file and run it.
- Use `py -3.11` for Python. A bare `py` resolves to 3.14 here and lacks pytest.
- Some files carry a UTF-8 BOM — read them as `utf-8-sig`. Write JSON manifests as UTF-8
  **without** a BOM.

## Tests and validation

- Run the migration suite from the skill folder:
  ```
  cd skills\tableau-migration; py -3.11 -m pytest tests -q
  ```
  Keep it green. The current baseline is **5123 passed / 6 skipped / 1 xfailed** (it advances with
  almost every release — treat it as "must not go DOWN", and re-read it from the tip rather than
  trusting this line, which has been stale before).
- Keep report-schema changes **additive** — add new keys or artifacts; do not rename or remove
  existing report keys. Add tests; never delete passing tests to make a change pass.
- **A filter that finds nothing has told you about its own predicate, not about the world — and a
  filter that finds the RIGHT NUMBER of things can still be lying about what they are.** When you
  measure a population by matching strings, assert both the match count *and the captures*, or you
  silently report on a subset, or on a corrupted breakdown of the whole. Measured on the 34-workbook
  corpus, the pattern `unsupported (?:function|table calculation) ([A-Z_]+)` fails **two different
  silent ways at once** against 71 `needs_review` reasons:
  - `unsupported function size` — lower-case, so `[A-Z_]+` matches **nothing** and the entry
    *vanishes*. 1 case. This one moves the count.
  - `unsupported function Total` — `[A-Z_]+` **matches**, capturing just `"T"`. The entry is present
    and *miscategorised*. 6 cases (`Total`→`T` ×4, `Sales`→`S`, `Stage`→`S`), and note that `Sales`
    and `Stage` **merged into one bogus `S` bucket** — distinct values collapsed, not just renamed.

  Seven entries corrupted; the match count differs by **one**. So "assert the match count" is
  necessary and **insufficient** — it would have caught 1 of the 7. Validate what you captured
  against the values you expect to exist.
- **Three sibling instruments failed the same way in one session, none of them raising:**
  `len(x) if isinstance(x, (list, tuple))` against a value that is a **dict**, reporting 0 visual
  calculations where there were 3; comparing a `blocked_by` **dict** against a set of name strings,
  reporting 8 of 8 dangling pointers where 0 dangle; and `os.path.isdir()` on `pbip_folder`, which is
  a relative path to a **file**, zeroing every per-workbook count. The failure is never that the
  predicate was wrong — it is that a non-match emits nothing to notice, so partial output looks
  complete. Count what you examined, check what you captured, and inject a known-bad case to watch
  the detector go red, before believing any clean result.
- **"No conflict" is not evidence of correctness; it is the absence of one specific alarm.** The
  general form of most of the rules below: *the failure is not that something was lost, it is that
  something plausible was produced.* A clean three-way merge and a regex that fabricates an `S`
  bucket are the same event — an operation that succeeds, emits confident output, and is wrong in a
  way the output itself cannot show. `validate` returning zero, a lint passing, a suite going green,
  a merge without conflicts, a pattern matching: each rules out one failure mode and says nothing
  about the rest. Three CHANGELOG chain breaks were introduced today by **clean** auto-merges with
  nothing to notice, and caught only because a gate re-derived the property afterwards.

  **Why this class needs a MECHANICAL check and cannot be handled by care.** Every instance shares a
  shape: the operation succeeded and the output was confident. A `= BLANK()` stub is a translation
  that "worked", producing a measure that binds and says nothing. `[A-Z_]+` matched, producing a
  category that never existed in the source. A merge completed, producing a chain break. Correct
  arithmetic on an inherited scope produced a wrong answer. **None of these is detectable by looking
  harder at the output, because the output is exactly what success looks like** — a human read
  compares output against expectation, and in every one of these cases the output *met* expectation.
  They are only detectable by asking a question the output cannot answer about itself, which is what
  "read at the artifact" means in practice and why it has to be executable. Five findings in one day
  came from checks; none came from reading, including a line dropped mid-sentence in the very commit
  that added these rules.
- **A ledger built on `refs/tags` shows what has been CLAIMED, never what has been BUILT.** An
  unpushed commit on another worktree's branch is invisible to you by construction, so a
  tag-derived view of "what has shipped" is stale the moment anyone commits locally. Centralised
  allocation fixes version collisions and does nothing for this. Either have sessions report their
  shipped set, or read branches rather than tags — and note that the integrator wrote the anchor
  rule ("an anchor tells you a version is CLAIMED, never that it is unlanded") and then built a
  ledger on exactly the surface it warns about.
- **Re-derive the SCOPE, not just the arithmetic.** Inheriting someone else's *question* is the same
  defect as inheriting their answer, and far harder to see, because the work you did was genuinely
  correct. Measured: one session correctly refused a reported figure and re-measured it from the
  corpus — but re-measured only the two function names the report happened to mention. The arithmetic
  was right; the scope was inherited, and the answer was still wrong (5 corruptions found, 7 present,
  and the worst one missed entirely). A verifier should derive over the **whole population and name
  nothing in advance** — "find every reason string where the strict pattern's capture differs from
  the permissive one's, then report what is there" surfaces the cases nobody thought to ask about.
- **A red result is a question, not a verdict.** A verifier that fails has told you nothing until you
  know *which side* is wrong. Three reds in one investigation: one was a genuine doc error, one was a
  check asserting a phrase as contiguous when the document hard-wraps it across a newline, and one
  was a regex reconstruction that was itself broken — and chasing *that* one produced the best
  finding of the day. The corollary to "a verifier that passes immediately has told you nothing yet"
  is that a verifier that fails has told you exactly one thing: look.
- **When a summary list and a payload list sit side by side, a reader measures whichever they find
  first.** `model_translation_handoff` carries `needs_review` (summary) and `requests` (payload) at
  equal length; `category_guidance`, `fields`, `formula` and `target_table` exist **only** on
  `requests`. Reading the summary and concluding "guidance is empty on all 30" was wrong — it is
  empty on **none of them**, in every build checked. If you add a field that changes a triaging
  reader's decision, put it on **both** lists; you cannot fix this downstream, because the reader who
  needs telling is precisely the one who never got to the other list.
- **Cite the build alongside any corpus count.** The same true measurement reads as an error later:
  `needs_review` is 71 at engine 2.275.0 and 69 at 2.291.0, the delta being two dispatchers that
  2.290.0 rescued. A count without its engine version is a claim with a hidden expiry date.
- Before committing, confirm packaging is valid: every `SKILL.md` frontmatter parses, the four
  JSON manifests parse, and relative links resolve.

## Secret discipline

- Never commit a real `.env`, a Tableau workbook or extract
  (`*.tds` / `*.twb` / `*.twbx` / `*.tdsx` / `*.hyper`), a PAT, a Connected App secret, or a
  sidecar API key. Only `.env.example` templates are committed.
- Use placeholder secrets in demos and scrub them afterward. See [`SECURITY.md`](SECURITY.md)
  and the bundled [`.gitleaks.toml`](.gitleaks.toml).

## Clean-room / IP discipline

This collection attests (in [`CLEANROOM.md`](CLEANROOM.md)) that its code — especially the
calc-to-DAX translator and the connector mapping — is original work. Two external references are
governed by **opposite** rules:

- **`cyphou/Tableau-To-PowerBI` is reference-only — copy no expression**, regardless of its MIT
  license (we deliberately decline the copy permission to keep the attestation intact). Consistent
  with [`CLEANROOM.md`](CLEANROOM.md) and the idea/expression dichotomy (17 U.S.C. § 102(b)), you
  may study its **unprotectable facts and general method** — *which* Tableau constructs/connectors
  have Power BI equivalents **and the conceptual approach** to a given translation — then
  **independently author our own** faithful, type-checked, tested version. Treat every mapping as a
  hypothesis to validate against DAX semantics + our tests, and note provenance in a comment where
  a specific idiom was informed by it. Never copy its source, functions, regexes, lookup/mapping
  tables, comments, fixtures, or arrangement — no paste, transliteration, or structure/naming
  mirroring. Run the CLEANROOM integrator similarity review before committing any translator or
  connector change.
- **`microsoft/skills-for-fabric` is the packaging/convention model:** mirror its **structure
  and formats** (frontmatter shape, `resources/` layout, manifest/marketplace layout, these
  convention files), but author your own prose. Retain the MIT notice on any file ever copied
  verbatim.

## Versioning & rollback

**Every change under `skills/<name>/` ships as a versioned release — this is mandatory, not optional.**
A code/resource/doc change that lands on `main` without a version bump + CHANGELOG entry + rollback tag
is an incomplete release (clients' self-update sees no newer stamp and skips it, and there is no clean
anchor to revert a bad release). Do all three, every time:

1. **Bump the skill `VERSION`.** Edit `skills/<name>/VERSION` (semver). This collection uses
   **MINOR-only skill bumps, one focused version per feature** (e.g. `1.61.0 → 1.62.0`); never a bare
   PATCH. Re-mirror so `plugins/tableau-fabric-skills/skills/<name>/VERSION` is **byte-identical**
   (enforced by `test_mirror_parity`).
2. **Add a `CHANGELOG.md` entry** under `[Unreleased]` (newest first) noting the skill delta and what
   changed — e.g. **`tableau-migration (skill \`1.61.0\` → \`1.62.0\`): …`**. Keep entries additive.
3. **Cut the rollback anchor BEFORE the change lands.** Create an annotated tag on the *pre-change*
   commit, matching the existing `rollback/pre-vX.Y.Z` series, and push it:
   ```
   git tag -a rollback/pre-v1.62.0 <pre-change-commit> -m "Pre-v1.62.0 anchor (<feature>)"
   git push origin rollback/pre-v1.62.0
   ```
   Rollback is then a one-liner: `git reset --hard rollback/pre-v1.62.0`.

4. **Run the CHANGELOG chain gate on EVERY commit of a rebased stack, not just the tip.**
   `tests/test_changelog_version_chain.py` asserts that each entry's declared predecessor equals the
   version produced by the entry beneath it, and that the newest entry matches the shipped `VERSION`.
   But the suite normally runs only at the tip, and **the CHANGELOG is a file every commit rewrites,
   so the tip masks its own history**: a stack can be correct at HEAD and stale one commit down.
   Measured — a two-commit stack whose tip declared the right predecessor had the wrong one at the
   commit below it, and the gate caught it instantly once actually executed there:

   ```
   git rebase --exec "cd skills/tableau-migration && py -3.11 -m pytest tests/test_changelog_version_chain.py -q" origin/main
   ```

   ~0.13s per commit. Run it on any stack you renumber or rebase. The invariant was never missing —
   the **execution point** was.

   Two traps worth knowing before you use it: an aborted `--exec` leaves a `rebase-merge` directory
   behind, and a later `git rebase --abort` then rewinds the *branch* to that stale state; and a
   resolver that patches the CHANGELOG with `str.replace(old, new, 1)` reports success by returning a
   string whether or not it matched, so **assert the match count** rather than trusting the call.

### Concurrent releases: ask the INTEGRATOR for a version number

**Version allocation is centralised. Do not claim a block. At your release step, ask the integrating
session for a number; it reads `refs/tags` and hands you one.** If the integrator is unavailable,
fall back to **take the next free number above every anchor, and renumber without ceremony if you
collide** — see "renumbering is the cheap path" below. What follows is why, because the previous rule
was not obviously broken and is still worth understanding.

The old rule was: each session claims a contiguous block above the tip, and publishes that block's
extent by cutting **every** anchor in it at claim time — which does make the extent visible in
`refs/tags`, the only namespace all worktrees share. The mechanism was sound. It failed anyway,
**twice in fifteen minutes, in opposite directions**, because it requires a multi-step ritual
executed perfectly under time pressure:

* Session A declared `2.290`–`2.294` but cut anchors only to `2.292`. Session B read the low-water
  mark of `2.292` and correctly took `2.293` — inside A's block.
* Session B then declared `2.293`–`2.302` but cut anchors only to `2.295`. Session A read *that*
  low-water mark and took `2.296` — inside B's block.

Both sessions did the right thing with the information actually present in `refs/tags`. Neither was
careless; one cut 3 of 10 anchors while shipping fast. **A coordination rule whose correctness
depends on every participant completing a ritual under pressure will fail precisely when the repo is
busiest, which is when collisions are possible at all.** Centralised allocation does not depend on
anyone's discipline, and costs one message.

**This also retires the only legitimate source of orphan anchors, which matters later.** Cutting a
whole block's anchors up front, combined with "leave a dead block's anchors in place", *deliberately
manufactures anchors for versions that will never exist* — measured, 35 of them, including nine cut
against a single commit within one minute (`pre-v2.216.0`–`pre-v2.224.0` → `232fed7`). That is the
rule working as designed. The cost is that **a claimed-but-unused anchor and a renumbered-away
anchor become indistinguishable by construction**, so no gate can tell a harmless orphan from a
rollback path that silently restores the wrong state. Once allocation is centralised and blocks are
gone, every anchor should name a version that really exists — and only then is that gate buildable.

**Anchors cut on a branch line do not survive an INTERLEAVED merge with their meaning intact.**
`rollback/pre-v2.293.0` points at a commit stamped `2.270.0`: an ancestor, strictly lower, passing
every existing gate — and still wrong, because on its own branch the state before `2.293.0` really
was `2.270.0`, while in merged history it is `2.292.0`. Rolling back there lands you missing two
releases. When two sessions interleave into one range, re-check what each anchor now means.

**Renumbering is the cheap path; a reservation is the expensive one.** Measured across one busy
afternoon: two collisions, both renumbered in under a minute, zero lost work, zero coordination.
Against that, the tempting fix — cut a **marker tag** for the top of your block so the extent is
visible — is a claim with **no expiry, in a namespace nobody prunes.** Dead blocks would accumulate
as permanent no-go zones, which is the hidden-expiry problem (see the corpus-count rule above)
relocated into the coordination layer, where it is worse because nothing ever re-derives it. So:
**do not reserve ranges.** A renumber costs a minute; a stale reservation costs a version range
forever.

Two rules survive unchanged because they are decidable without any shared state, and both held twice
under collision: **claim as late as possible** (at the release step, not the start of the lane), and
**on a collision the committed/pushed side wins and the other renumbers.**

The historical block rules, kept because old anchors and CHANGELOG entries still reflect them:

* **Claim the block ABOVE the current tip, and expect it to erode from below.** The `VERSION` stamp
  must stay monotonic, so a block is alive **only for its portion above `HEAD`** — it is not a
  reservation that lives or dies whole, it shrinks from the bottom as the tip advances. A block whose
  whole range has fallen below the tip is dead even if none of it was used; a block whose head was
  consumed is still good for the rest. Both misreadings of this happened in one exchange, in opposite
  directions, from the shorthand "claim above the tip".
* **On a collision, the PUSHED side wins and the unpushed side renumbers.** Decidable without either
  party knowing the other's state, which is the only property that survives a race; a rule that
  alternates who absorbs the cost needs shared memory of whose turn it is, and a race is precisely
  the absence of that.
* **Never assign a version number to another session.** Allocate only from your own block. Every
  collision in the run that produced these rules traces to a number travelling in a message, which is
  the one artifact neither party can keep current — by the time it is read, the sender may already
  have consumed it. The integrator lands what arrives and objects only on an actual collision.
* **Publish the block's whole EXTENT when you claim it, and claim it as LATE as possible.** Cut every
  anchor in the block at the current tip the moment you claim, and move each to its real parent as
  that release lands. Otherwise the block is only ever *partially* published while it is being built
  — an anchor can only be cut once its predecessor exists — so a reader sees a block's low-water mark
  and never its extent, and claims into the middle of it. That happened, and the anchors then
  collided too (see below). The complement is timing: a block dies the instant the other party's tip
  passes it, whatever its size, so **claim at the release step, not at the start of the lane.**
  Blocks claimed early died four times in one day; blocks claimed seconds before committing survived.
  The two rules are compatible — the block is fully published the instant it is claimed, that instant
  is just moved later.
* **`refs/tags` is SHARED across every worktree** (`git rev-parse --git-common-dir` is one `.git`), so
  a version collision is also an ANCHOR collision: one name, one slot, last writer wins. **Never
  `git tag -d` an anchor you did not create** — `git for-each-ref --format='%(taggername)
  %(taggerdate)'` identifies the owner in one call, and a BATCH delete is where this happens, because
  the batch reads as a single intent ("my old numbers") while acting on a global namespace. Leave a
  dead block's anchors in place: an unused anchor is inert to every gate here, while a wrongly deleted
  one destroys a rollback path and reads to the next person as "reported but absent". Deletion is the
  only irreversible step in this ritual.
* **An anchor tells you a version is CLAIMED, never that it is unlanded.** Three distinct anchor
  failures showed up in one day and none was predictable from the others: pointing at an orphan,
  absent because another session deleted it, and present-but-already-shipped.
* **`refs/stash` is shared too, and it is the more dangerous of the two** because the reflex that
  reaches for it (`git stash pop`) is a WRITE to your working tree, not a read. One `stash@{0}`
  currently sits in the common `.git` — *"parked last-mile edits (stale 1.4.0 base)"* — touching
  `assemble_model.py`, `migrate_estate.py` and `deploy_to_fabric.py` in both trees, from roughly 290
  minor versions ago. Popping it would drop a 1.4.0 file on top of live work, and because the change
  would arrive in YOUR worktree under YOUR name it would read as your own mistake. Do not pop, apply,
  or drop a stash you did not create; the same rule as anchors, for the same reason. When auditing
  what a lane might collide with, `git stash list` belongs next to `git branch` and `git tag -l`.
* **A local branch existing on `origin` does NOT mean deleting it is safe — check that it MATCHES.**
  Deleting three abandoned branches, two of which `git ls-remote` confirmed were on `origin`, would
  still have stranded one commit: `powerbi-formatting-research` was locally AHEAD of its remote. The
  predicate "is it recoverable?" is `local tip == remote tip`, not "does the remote ref exist".
  Archive-tag anything you delete (`archive/<branch>`), which costs nothing and makes the only
  irreversible step in branch cleanup reversible.


When several features shipped unversioned, catch up per feature: assign each its own MINOR version +
CHANGELOG entry + `rollback/pre-v` tag at that feature's pre-change ancestor (see the 1.58.0–1.61.0
catch-up for the pattern).

**Collection version is decoupled.** The four packaging manifests share one **collection** version
(`.claude-plugin/marketplace.json`, `.github/plugin/marketplace.json`, and the two `plugin.json`s,
currently `0.12.0`). A skill-only change bumps the skill `VERSION` **only** — do **not** bump the
collection manifests for it (they move separately, less often, for collection-level packaging changes).

The self-update runbook (`skills/tableau-migration/resources/self-update.md`) is the consumer side of
this contract: it compares installed `VERSION` against the raw `VERSION` on `main` and only reinstalls
when `main` is newer. If you forget the bump, no client ever updates.

## Commits

- Make the **user** the commit author, and append the trailer:
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  ```
- Do not push unless explicitly asked. Re-mirror the plugin copy and pass the green-suite +
  validation gate before each commit.
