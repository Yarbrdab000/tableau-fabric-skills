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
- **A gate must not conflate a DISCLOSED state with an UNDISCLOSED one — they are semantically
  different and only one is actionable.** Not a noise-reduction argument. *"Nobody knows this measure
  is empty"* and *"the engine knows, has named the dependency, and it self-heals when that sibling
  translates"* demand different responses from a reader, and a check reporting both teaches them that
  a hit might be either — which destroys the property that makes it worth reading, that a named
  object is **certainly** in the reported state.

  Measured instance: a transitively-blank gate passed on two dispatcher measures that render blank
  for one selection each. That looked like a gap. It was not — `partial_fidelity` already named the
  measure, **the branch number**, the awaited sibling, the reason, and **which branches are live**, in
  the report, the summary, and the TMDL the user opens. That is *more* specific than the gate would
  have been: the gate would have said "this measure can be blank"; the disclosure says "blank on
  selection 3, awaiting `Avg. Days Participation`, and 1/2/4 are fine."

  The inference that failed is the familiar one: *"the gate does not flag this"* is true, and was used
  as *"nothing flags this."*
- **THE MOVE THAT FINDS THESE: take the confident sentence and ask what would have to be true.** Not
  "check the work" — check the **claim**. And the question is not *"is this right?"* — every statement
  below was right. It is:

  > **"What would have to be true for this to mean what I am about to use it for?"**

  Because **every silent failure in a high-volume day was a TRUE statement doing the work of a
  different, false one:**

  | the statement | true of | used as |
  |---|---|---|
  | `unsupported character '<'` | the byte the tokenizer hit | "add a comparison parser" |
  | stub count `6 → 1` | visuals projecting a stub | "those four render defects are fixed" |
  | interleave debt `4 → 1` | entries in an exception ledger | "the repair is working" |
  | "every released version has an anchor" | the 89 of 140 it examined | all releases |
  | "the anchor sits one release before" | the version STAMP | where a `reset --hard` lands |
  | `= CALCULATE(...)`, not `BLANK()` | the expression | "this measure has a value" |

  **Not one was a bug in the usual sense, and none would have been caught by more care.** The rules
  below are all specialisations. Reach for them when you have a specific suspicion; reach for **this**
  when you have a sentence that sounds right.

  The cheapest instance overturned the most: **one `git merge-base --is-ancestor`**, run because *"the
  anchor sits one release before"* had never been tested against what a reset actually does. It took
  down a gate, an exception ledger, six anchor repairs and three documentation commits.
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
- **Before a metric can support a claim, verify the metric's POPULATION is the claim's population.
  Ask what the number counts, not whether it moved.** This supersedes "read the detail more
  carefully", which is unactionable — the detail was already on screen when the claim was made.

  The mechanism is narrower and more mechanical than confirmation bias:
  `visuals_projecting_stub_measures` and "visuals that render blank" are **different populations that
  merely overlap**. `6 → 1` was completely true of the first and was never a statement about the
  second. Reading the per-visual ids would have caught it *that time*, but only because the two
  populations happened to be disjoint in that instance — had one stub sat on a blank-rendering
  visual, the detail would have *looked* like confirmation and the claim would still have been wrong.
  **A population mismatch survives any amount of careful reading.**

  It is also why going to the render worked and was not luck: **the render IS the population in
  question.** Same reason `INFO.MEASURES()` settles a "did my model edit land" question — not because
  it is a second opinion, but because it is the only instrument measuring the thing being claimed.

  And it is the general form of absence-blindness one level up: a static detector enumerates a
  population (emitted objects) that **structurally excludes** the defect class (never-emitted
  objects). Same error, one level higher.
- **Detectors that enumerate what is PRESENT cannot see what was never emitted — and no amount of
  care fixes it, because the class is outside what they can express.** Hit twice in one day by two
  unrelated tools: a proximity matcher gave a never-emitted object a "1097px size error" by pairing it
  with an unrelated chart, and `visuals_projecting_stub_measures` reported a workbook clean while 17
  reference lines bound to stubbed calcs went unbuilt. Neither tool was aware of the other's instance.

  This applies to **every static gate in this repo**: the invisible-ink check enumerates emitted
  colours, the foreign-source-path gate enumerates emitted partitions, the ambiguous-relationship
  check enumerates emitted relationships. All are absence-blind **by construction**.

  The consequence for prioritisation is not "render verification is cheaper" — it is that **opening
  the file and looking is the only thing that can see this class at all.** A missing object leaves no
  artifact to inspect, so there is nothing for a static check to be careful about. Two mitigations
  where a static check must stand in: make a matcher report **unpaired** items explicitly, so
  "paired with something implausible" and "not present at all" can never look the same; and derive
  the expected population from the SOURCE (the `.twb`) rather than from the output, so an absence is
  a difference rather than an empty set.
- **A diagnostic that partitions by SYMPTOM makes one problem look like several, and each looks too
  small to fix.** Measured: three review reasons — `unsupported character '<' / '=' / '>'` from the
  tokenizer, `expected '('` from an `IF` lexed as a function call, and `trailing tokens after
  expression` from a bare `AND` — were **one boundary** (boolean/conditional logic is outside the
  arithmetic Visual-Calculation subset) presenting as three unrelated gaps. Together they were **10 of
  27 review rows**; separately each looked like a niche parser complaint not worth a release. The fix
  was not parsing anything; it was noticing they were the same thing.

  This compounds the other counting hazard: a per-function census built from `fallback_reason` is
  already low by an **unbounded** amount, because only the FIRST unsupported head in a formula is
  reported. So a review surface can be simultaneously **undercounted** (hidden heads) and
  **fragmented** (symptom partitioning), and ranking by raw reason counts inherits both. Wherever many
  distinct-looking low-count reasons share a surface, check whether they share a cause before
  believing the ranking.
- **A metric moving is not the render changing, and a count says nothing about WHICH object moved.**
  I read `visuals_projecting_stub_measures` dropping **6 → 1** on one workbook and told another
  session that four of its open render defects were therefore fixed. They checked at the artifact:
  the metric was correct and **not one of the four had changed**. The five cleared stubs were
  `Select Metric` on a *different page* and four `Sort By` measures driving *slicers that had always
  rendered correctly*. Had my word been taken, four genuine open defects would have been closed on a
  page where nothing changed.

  **The disconfirming detail was already in my own output.** The A/B I ran hours earlier printed the
  visual id beside every measure — `v-page-Assessmen…` next to `Select Metric` — and I read past it
  because the aggregate matched what I expected. An aggregate that agrees with your hypothesis is the
  most dangerous kind of confirmation, because the per-item detail that refutes it is right there and
  costs nothing to check. **Before claiming a metric proves a render outcome, name the specific
  objects it moved and confirm they are the ones in question.**
- **A second opinion narrower than the check it verifies is not redundancy.** The shipped CHANGELOG
  chain gate accepts both `->` and `\u2192` and does not require a backticked skill name, so it has always
  examined all **137** entries. My independent probe — written to double-check it — required the
  Unicode arrow *and* the backticked name, and saw **86**. Two formats coexist in that file, the older
  51 using bare `tableau-migration` and ASCII `->`. So the "second opinion" I was relying on could
  never have contradicted the gate on 51 of its entries; had the gate been wrong there, my check would
  have agreed by construction. Verify a check with something at least as broad as the check, and make
  both print their admitted counts so a divergence like 86-vs-137 surfaces as a question rather than
  as agreement.

  **The actionable form: write a verifier's predicate from the SUBJECT'S GRAMMAR, not from what the
  current data happens to look like.** The gate accepts `(?:->|\u2192)` because both formats exist in
  the file; any probe over that file must accept exactly the same, or it certifies a region it cannot
  see. The concrete cause of the 86-vs-137 split was measured by a peer session — who reproduced it
  *inside the message discussing it*: a PowerShell `Select-String` pattern used `.` for the arrow,
  which matches **one** character. `\u2192` is one char; `->` is **two**. So the pattern silently
  dropped all 51 old-format entries:

  ```
  both arrows accepted : 139     unicode only : 88     ascii '->' only : 51
  ```

  "Look at the data and write a pattern that matches it" is how both of us built a narrow verifier;
  the grammar is what the subject actually admits, and only that is a safe predicate.

  **The corollary, which is a real architectural property rather than luck:** the `[Unreleased]`
  normaliser recognises only the newer format, so it absorbs the older 51 as body text and sorts them
  with whatever entry swallowed them. Those survived today only because they all sit *below* the
  entries it parses — but a relocation would **not** stay silent, because the chain gate parses
  **both** formats and a moved entry breaks the strictly-descending order. **The normaliser can
  mangle; the gate is the backstop.** Recorded because "sorting is safe only by luck" reads worse than
  the system is, and a pessimistic record is as much a wrong record as an optimistic one.
- **A known-unreliable instrument's most extreme output is the one most likely to be its artifact —
  and it is the one that gets quoted.** A proximity matcher documented as unreliable for large
  movements paired an object that was *never emitted* against an unrelated chart and produced a
  "1097px size error", which was then reported as the worst defect on the page. The limitation had
  been written down by its own author when it was built. The failure mode and the headline finding
  occupy the same tail of the distribution, so the caveat is never load-bearing until precisely the
  moment it is ignored. Before quoting an outlier, check it against the limitation you already
  documented — and make matchers report unpaired items explicitly, because "paired with something
  implausible" and "not present at all" must never look the same.
- **A glob is a scope decision, and it stops covering the tip silently.** My blast-radius probe
  filtered anchors with `rollback/pre-v2.2[6789]*` — correct when 2.29x was the top of the range, and
  it does not match `2.30x` **at all**. So the worst anchor in the repo was never examined and the
  probe confidently reported a maximum of 5 where the true figure was 12. It did not error, skip, or
  warn; it answered a narrower question than the one asked. Print how many items a filter admitted,
  and the highest one, so a range that has outgrown its pattern announces itself.
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
   Rollback on **that lane, before it is integrated**, is then a one-liner:
   `git reset --hard rollback/pre-v1.62.0`.

   **On an integrated branch, do NOT use `reset --hard` — use `git revert`.** An anchor is a point on
   **one lane's** history, so once lanes merge, moving the tip there necessarily drops everything
   merged from every *other* lane since that lane forked. The anchors do not go stale; the operation
   changes meaning under them. Measured on this repo's integration branch, **all 12 recent anchors
   over-discard, not one destroys only what it names**:

   ```
   rollback/pre-v2.299.0   also destroys 2.274.0 2.275.0 2.290.0 2.291.0 2.292.0 2.296.0 2.297.0 2.298.0
   rollback/pre-v2.298.0   also destroys 2.269.0 2.270.0 2.293.0 2.294.0 2.295.0
   rollback/pre-v2.293.0   also destroys 2.274.0 2.275.0 2.290.0 2.291.0 2.292.0
   ```

   So `reset --hard rollback/pre-v2.298.0` discards **six** releases from three lanes while its name
   promises one. `git revert <that release's commit or merge>` removes what it names and nothing else.

   **No existing gate can see this**, and that is the point: `VERSION`-at-anchor `<` named version
   passes for all 12, and the anchor-predecessor gate passes too. Both are checks about **numbers**;
   this is a fact about **topology**. Necessary, insufficient — the same shape as a match-count
   assertion catching 1 of 7 corrupted captures.

   **Re-pointing helps, does not solve it, and can make it WORSE — measured, because the obvious fix
   looked complete.** Re-pointing five anchors at the release commit of their declared predecessor
   (which is what the anchor-predecessor gate demands) moved the worst case from **8 extra releases to
   5, measured at `1d9e389`**. Date any number here: it moves whenever any lane cuts an anchor or
   ships, and this one was **false 117 seconds later** — invalidated by the very next action of the
   person who measured it, cutting `pre-v2.302.0`. At the time of writing the worst case is **12**.

   The reason it can worsen: **a release commit is itself on a lane.** Satisfying the gate re-points
   an anchor at whichever lane produced its predecessor, which may be *further* from the integration
   line than where it started. `pre-v2.302.0` moved from an integration-line commit to the table-calc
   lane's 2.301.0 commit and went from ~0 to 12. **The anchor-predecessor gate (a claim about version
   stamps) and the over-discard property (a claim about reachability) are not merely different — they
   can pull in opposite directions.** Satisfying one is not evidence about the other. A complete fix
   anchors at the integration line's own commit for that version, not the lane's.

   The over-discard metric counts only releases numbered **below** the anchor's own name: `pre-vX`
   legitimately discards X and everything after it — that is what rolling back means — so only a
   lower-numbered casualty is a defect. Counting all of them inflates the figure and was wrong twice.

   **The two checks are in DIRECT CONFLICT on at least one anchor, measured.** For `pre-v2.302.0`:

   ```
   3c3a981  (table-calc lane's 2.301.0)  stamps 2.301.0  gate PASSES  over-discards 12
   84345a3  (integration-line merge)     stamps 2.300.0  gate FAILS   over-discards  1
   ```

   The target that makes the rollback nearly correct is the one the anchor-predecessor gate rejects,
   and the target the gate demands destroys twelve releases. This is not a tie to be broken by
   preference — it is evidence that **the gate is not a proxy for the property**, and neither should be
   satisfied on the assumption that it delivers the other. Left unresolved deliberately, and recorded,
   rather than silently optimised for whichever is currently green.

   The durable statement, which is the day's shape at its purest:

   > **When a check stands in for a property, satisfying the check is not evidence about the property
   > — and optimising for the check can move the property the wrong way.** Two green anchor gates
   > currently certify a rollback path that would destroy twelve releases.

   **A shared mutable ref is safe to write concurrently iff its correct value is a pure function of
   state both writers can see.** That is the line between the two coordination decisions in this
   file, and it explains why one had to be centralised and the other did not:

   * **Version allocation FAILS the test.** An unpushed commit on another worktree's branch is
     invisible by construction, so no amount of care lets two allocators compute the same answer.
     Centralising was the only available fix.
   * **Anchor targets PASS it — but only under the right formulation.** "A commit stamped with the
     declared predecessor" is **under-determined**: merges and doc commits inherit a `VERSION` without
     changing it, so measured over 710 commits, **112 of 329 versions are carried by more than one
     commit**. Two integrators can both satisfy the gate and write anchors that reset to materially
     different trees. "The commit where `VERSION` **became** X" (stamped X, no parent stamped X) is
     near-unique: **327 of 329**, with two historic exceptions (`1.23.0`, `1.25.0`) that each have two
     introducers.

   So the hazard is not *"more than one candidate integrator"* — it is **an under-specified target
   with more than one candidate integrator.** Fix the specification and the number of writers stops
   mattering. Two integrators converged on the same anchor today only because both independently
   applied the introducer rule without stating it; nothing obliged a third to.

   **The over-discard count is worth keeping as a METRIC, not just a hazard.** An anchor's extra count
   is exactly *how much other-lane work merged since that lane forked* — a direct read on
   **integration lag**. `pre-v2.299.0` scoring 8 did not mean that anchor was broken; it meant that
   lane had run parallel for 8 releases. One `git log` per anchor computes it (never one call per
   anchor×release — that form ran past 400s without finishing, and an instrument too slow to complete
   is one you will skip). The gate shape this suggests, if anyone builds it, is not "is the version
   lower" but **"does this anchor's reachable set differ from the tip by exactly what it names."**

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
collide** — see "renumbering is the cheap path" below.

**Centralising does not delete the failure mode, it MOVES it into one actor** — a better trade, not a
free one. Within four hours of imposing this rule the integrator cut an anchor without reading
`refs/tags` and collided with a lane that had claimed it. The mechanism is worth naming because it is
general: **the author of a rule holds its intent in mind and reads the intent instead of the
artifact.** They did not check the tags because they already knew what the tags would say. That is the
same failure as trusting a return value instead of querying the model, wearing different clothes —
and it is why *the rules most likely to be violated are the ones you wrote*. Authoring a rule creates
the feeling of having internalised it, which is precisely the feeling that stops you checking.

**Allocate against ANCESTRY, not only against the tag namespace.** `refs/tags` shows what is claimed
at the moment you read it, and a read is stale the instant it returns — a tag cut seconds ago, or a
cold worktree ref cache, both produce a confident "free". The decisive question is not *"is this tag
absent?"* but ***"is a commit claiming this version already in the branch that would contain the
work?"*** — `git merge-base --is-ancestor <sha> <branch>`, which needs no coordination and cannot be
stale about your own ancestry. This is the pure-function rule applied to allocation itself: branch
ancestry is state you can see; another session's unpushed commit is not, which is exactly the residue
centralising cannot remove.

What follows is why the previous rule was replaced, because it was not obviously broken.

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
* **Archive before any destructive ref change — not only so it can be undone, but so the DECISION can
  later be checked.** The second reason is the one that pays. Six anchors were re-pointed here to
  satisfy a gate, and the re-points were later found to have broken every one of them. The decisive
  evidence was a one-line comparison between the archived original and the current target — *"the
  original is an ancestor of its own release, the re-point is not."* Without
  `archive/anchor-pre-vX-preintegration` there would have been nothing to compare against, and the
  finding would have been an argument between two readings instead of a fact. An archive is a
  measurement you have not needed yet.
* **On a shared mutable ref, the safe move on disagreement is to REPORT, not to converge** — because
  convergence requires a shared view, and disagreement is the proof you have not got one. Measured:
  two sessions independently adopted each other's *prior* position one message apart, with
  `refs/tags` between them. Both were reasoning from published evidence; neither was careless. The
  only reason it stayed harmless is that **both actors who could write the ref stopped and reported
  instead** — one flagged a red gate without fixing it, the other asked them not to write before they
  could. Had either "just fixed it", six anchors would have been re-broken by whichever write landed
  second, and the tree would have been wrong in a way that looked like a merge conflict.
  Corollary: never leave *"say the word and I'll write it"* on the table for a shared ref — the offer
  is the hazard, because the answer may cross a change that makes it wrong.
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

### Recency is not reachability — never decide file ownership by timestamp

Before editing a shared file while other sessions are live, the question is *"does any lane hold work
in this file that I do not have?"* — a **reachability** question. It is tempting to answer it with
`git log -1 --date=short -- <file>` per branch and read the older dates as "behind". That orders
commits by **recency**, which agrees with reachability most of the time and is a different question.

Measured here on `migrate_estate.py`: a lane whose last edit was a day older than the integration tip
was labelled "BEHIND", and it was **divergent** —

```
lane vs origin/main:  6 behind, 11 ahead      <- both non-zero
merge-base blob 7ecdcc2490
   lane        c96dee7274   changed by lane : True
   origin/main 92d4a1fc7c   changed by main : True   <- both sides moved it
```

"Behind" means **contains nothing the target lacks**. That lane's edit was in the *integration branch*
and not in `origin/main`, so it was content `origin/main` lacked. The conclusion ("safe to edit") was
right for a reason that had not been stated: safe because **the integration already carried the
change**, not because the lane was behind. A session branching from `origin/main` rather than from the
integration tip would have built on a base missing it.

Ask it with reachability, and state which ref the answer is relative to:

```
git merge-base --is-ancestor <lane-tip> <target>          # strictly behind?
git rev-list --left-right --count <target>...<lane-tip>   # both non-zero = DIVERGENT
git rev-parse <ref>:<path>                                # compare blobs against the merge-base
```

This is the general failure one level down: **an instrument answers the question its predicate
encodes, and that question is rarely the one you are asking.** A timestamp comparison encodes *which
is newer*; ownership is about *what is contained where*.

### A gate needs a test that it is CALLED, not only that it WORKS

**Correctness and reachability are independent properties, and the entire test-writing reflex points
at the first.** A gate that never executes passes every test it has, forever, silently.

Measured here: `openability_selfcheck` was computed during model assembly and reported at the end of
the build, while `_apply_row_predicate_wrapped_measures` *added measures to that model in between*. So
two correct, well-tested checks — `measure_value_path_not_blank` and `wrapper_keeps_base_format_string`
— shipped as **true statements about an artifact nobody ships**:

```
_value_path_bottoms_out_blank on the WRAPPED parts   -> fires on 8 measures
shipped openability_selfcheck                        -> measure_value_path_not_blank: True
```

Twelve unit tests, four independent instruments (static parse, `INFO.MEASURES()`, a DAX query, and a
render) and a corpus reproduction all agreed the gate was correct. **Every one of them measured the
function; none measured its position in the pipeline**, so their agreement carried no information
about *when* it runs. Width in technique is not width in scope.

The cheapest possible check was never run: `git grep check_model_openability` returns one call site.

So, for any gate whose verdict is recorded at one stage and read at another:

* **pin the call site**, not just the behaviour — e.g. a test that reads the source at the wrap site
  and asserts the re-check is invoked there. Brittle, and it should say so in its own failure message;
* **pin the ARGUMENT, not only the call.** A pin that asserts the call exists is satisfied by passing
  the *pre*-mutation object, which runs clean and reports healthy;
* **prefer an artifact pin where it is cheap.** A text pin proves the call is *written*; asserting a
  provenance flag (here `openability_selfcheck.rechecked_after_row_predicate_wrap`) on a real build
  proves it *ran*;
* **cover the early-exit path.** A guard like `if not parts: return` no-ops silently if a refactor
  starts passing `{}`, and everything downstream still looks fine.

Put beside the absence rule, the pair is: **an absence claim is only as good as the instrument's
scope, and a positive result is only as good as the instrument having been consulted.**

### Beware a repair whose completion signal is the damage it causes

The hardest failure recorded here is not a true statement doing false work. It is a **fix whose success
criterion and whose defect are the same observable**.

`nativeQueryRef` (a projection's user-facing label) and `field.Measure.Property` (the measure it
actually evaluates) disagree on 55 projections across 39 visuals in one corpus workbook. That reads
exactly like a bug, and the obvious repair is to make them agree by rebinding the projection to the
base measure. Three sessions independently reasoned toward it.

**It would have been a silent data-correctness regression.** The divergence is deliberate:
`migrate_estate._apply_row_predicate_wrapped_measures` points the projection at a
`CALCULATE(<base>, FILTER(...))` wrapper standing in for a Tableau row-level boolean keep, and leaves
the label as the original Tableau name on purpose. Measured: **35 of 35 wrapper measures contain a
`FILTER(`** — unanimous, not typical. Rebinding to the base drops a live date-window predicate every
single time, and every number on the page changes.

And the report would have looked **more** correct afterwards, because the labels would finally agree.
**Checking the result cannot catch this**, because the result is the intended one. Only checking what
the construct is *for* can.

What actually settled it, in order of decisiveness:

1. **Read the code at the site of the change**, not near it. The rationale was in a comment two lines
   above the rewrite, and in `_wrapper_measure_name`'s docstring. Several passes over that file for
   other reasons never read it.
2. **Ask what the artifact would lose**, not whether it looks wrong. `35/35 carry a FILTER(` is a
   one-command question and it ends the argument.
3. **Date the claim.** The comment asserting the labels "can never disagree" was added 38 releases
   *after* the code that makes them disagree — answerable from `git log -S` alone, with no build.

Generalised: **a measurement of an artifact cannot tell you the intent of the code that produced it.**
Before "fixing" a systematic divergence, establish that it is not a contract.

### Carefulness is not a defence — it fails in three directions

The three hardest claims to check are all *careful* ones. Each escapes scrutiny by resembling the
virtue that should prompt it, and no amount of being more careful helps, because being careful is what
produces them.

| shape | how it reads | why it escapes |
|---|---|---|
| **over-claiming** wrapped in self-criticism | rigour | cataloguing your own error *is* a form of authority, and it displaces the evidence rather than accompanying it |
| **under-claiming** a property you only partly measured | modesty | you measured one of its jobs, found that one unexercised, and labelled the whole guard unexercised |
| **a hedge** used instead of an available check | appropriate caution | *"as far as I can tell"* is indistinguishable, to a reader, from having looked |

All three were committed here, by different sessions, on different days' worth of otherwise careful
work:

- a retraction that was scrupulous about *which key names were missed* and silent about *which files
  were read* — the scrupulousness is what bought it a pass;
- `_NON_DAX_CALL_RE`'s leading lookbehind, documented as "defensive, not validated" while its second
  and load-bearing job was exercised by an existing test;
- *"not on the issue as far as I can tell"*, written in a worktree where `gh issue view` was available,
  about an answer that had been posted twenty-seven minutes earlier.

The tests that actually work on all three are mechanical, and both are one command:

> **A hedge is only honest when the check is unavailable** — and only stays honest until it isn't. A
> recorded "plausible cause" that is never revisited ages into folklore with none of the caveat
> surviving, so retire it the moment the check becomes possible. Ask *what would have to be true for
> "as far as I can tell" to mean "I looked"?*
>
> **A property is not validated or unvalidated as a whole — each of its jobs is, separately.**

## Commits

- Make the **user** the commit author, and append the trailer:
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  ```
- Do not push unless explicitly asked. Re-mirror the plugin copy and pass the green-suite +
  validation gate before each commit.
