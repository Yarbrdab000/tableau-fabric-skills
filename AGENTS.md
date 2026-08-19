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
  Keep it green. The current baseline is **956 passed / 1 skipped / 1 xfailed**.
- Keep report-schema changes **additive** — add new keys or artifacts; do not rename or remove
  existing report keys. Add tests; never delete passing tests to make a change pass.
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

### Concurrent releases: claim a version BLOCK, above the current tip

When two sessions ship into the same repo, "take the next integer" collides whenever their fetches
straddle a push — fetching narrows that window and cannot close it. So each session claims a
contiguous **block** (e.g. `2.175`–`2.184`) and allocates only inside it, announcing the next block in
the same message as that block's first commit.

Two rules make it work:

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
