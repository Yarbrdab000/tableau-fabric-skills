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
- **A backtick inside a `py -3.11 -c "..."` string is not a character.** PowerShell consumes it as an
  escape, and what Python receives is a *different pattern that still runs*. Measured on the same
  file in the same minute: the intended sentinel matched **154**, the mangled one **54** — both
  well-formed, confident numbers. This produced a **vacuous chain parse** (`entries=0`, therefore
  `0 breaks`, `no duplicates` — all true of an empty list) that was caught only by an accidental
  `IndexError`, and it recurred **eight times in one session**, including inside the release written
  to fix it. Any regex with a backtick, `$`, or nested quotes goes in a `.py` file. No exceptions —
  the failure output is always plausible, so you will not notice.
- Use `py -3.11` for Python. A bare `py` resolves to 3.14 here and lacks pytest.
- Some files carry a UTF-8 BOM — read them as `utf-8-sig`. Write JSON manifests as UTF-8
  **without** a BOM.
- **Each shell call is a fresh process, so a status read is not evidence about a preceding command.**
  Verifying `git merge --abort` (or a rebase abort) needs the in-progress markers, not just
  `git status --porcelain` from a later invocation — an abort can leave `MERGE_HEAD` behind under a
  tree that reads clean. **In a worktree `.git` is a FILE**, so `Test-Path .git\MERGE_HEAD` is always
  false and always reassuring; resolve the real directory first:

  ```
  $gd = git rev-parse --git-dir        # e.g. ...\.git\worktrees\<name>
  foreach ($f in 'MERGE_HEAD','CHERRY_PICK_HEAD','REVERT_HEAD','rebase-merge','rebase-apply') {
      if (Test-Path (Join-Path $gd $f)) { "in progress: $f" }
  }
  ```

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
     commit** *(measured at an earlier vintage; the same quantity at `e88386b` is **121 of 344**, and
     both are the same fact — every figure in this file is of its own date and none carry one)*. Two
     integrators can both satisfy the gate and write anchors that reset to materially different trees.
     "The commit where `VERSION` **became** X" (stamped X, no parent stamped X) is near-unique:
     **327 of 329**, with two historic exceptions (`1.23.0`, `1.25.0`) that each have two introducers.

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

**Do not describe your branch to another session. Ask git.** Every coordination question here that
cost more than one message was a *state report* — "I'm at X", "nothing is allocated", "it merges
clean" — and every one of them was true when written and stale when read. A message is a snapshot; a
ref is the state, and this applies to the SENDER as much as the receiver. Measured: seven exchanges on
a single sha, an "unallocated" line that was accurate at the moment it was typed, and a "merges clean"
that was correct against a tip four commits old.

The join is executable and costs one call:

```
git merge-base --is-ancestor <their-tip> HEAD     # already integrated?
git merge-tree $(git merge-base HEAD <their-tip>) HEAD <their-tip>   # compatible? conflicts?
git show <their-tip>:skills/tableau-migration/VERSION                # what number do they claim?
git for-each-ref refs/tags/rollback/                                 # what is already anchored?
```

**An anchor tag is a lane declaring intent, not bookkeeping.** A `rollback/pre-vX` that exists for a
number you are about to take means someone is mid-release on it — twice here that tag was archived and
re-pointed as stale housekeeping, and twice it cost a duplicate implementation.

**And the anchor namespace is SHARED, so the hazard is mutual — it does not care who is allocating.**
Both directions happened within three minutes: the integrator cut `rollback/pre-v2.314.0` at 16:35, a
lane cut the same tag at 16:38, and **the second write silently took a slot the first had claimed.**
The lane had checked `git tag -l "rollback/pre-v2.314.0"` and read "free" — the one check this file
already says is insufficient, because *a tag read is stale the instant it returns*. Allocate against
**ancestry** (`git merge-base --is-ancestor` against the other branch), which shows a lane mid-release;
a namespace lookup does not. Recorded as mutual deliberately: **a lane that reads this as "the
integrator's problem" will skip the check, and it is the check that matters rather than the role.**

**The underlying defect is the ledger's SHAPE, not either party's attention:** `refs/tags/rollback/`
mixes *claimed*, *shipped* and *abandoned*, so it cannot distinguish a collision from routine churn.
The obvious repair is to derive the states — *an anchor whose version no reachable commit stamps is
CLAIMED, i.e. someone is mid-release* — and it turns out **not to be reliable**, which is worth more
than the repair would have been:

```
git log --format=%H -- skills/tableau-migration/VERSION                 327 commits
git log --full-history --format=%H -- skills/tableau-migration/VERSION  464 commits
                                              omitted by simplification 137   (30%)
```

**`git log -- <path>` applies history simplification by default and drops commits whose change arrived
through a merge.** On a history assembled from parallel lanes that is most of them, so the derived
"claimed" set reported seven demonstrably-shipped releases as in-flight. **Use `--full-history` for any
question about which versions exist**, and treat a plain `git log -- <path>` as a sample rather than a
population — the same silent scope decision as a glob, made by a default instead of a flag.

**What made both episodes cheap is that each party archived before overwriting** —
`archive/pre-v2.314.0-prior`, `archive/duplicate-2.312.0-dispatcher-lane`. Nothing recoverable was
lost, and more usefully **the decision stayed checkable**: a duplicate could only be confirmed *fully*
superseded rather than partially because both versions still existed to diff. Retire a superseded tip
by resetting onto the surviving branch rather than leaving it — **a lane head that can never merge is a
trap for whoever picks it up next.**

### A red anchor gate is a question whose answer may be that a rollback path is gone

`test_every_released_version_has_an_anchor` and `test_every_anchor_predates_the_version_it_anchors`
both read the **shared tag namespace**, so a lane can go red for a reason entirely outside its own
branch and green again seconds later. That much is true. **What was concluded from it was not.**

A lane fast-forwarded, ran the suite, got `MISSING anchors: ['2.317.0']`, re-measured 90 seconds later
to find the tag present, and reported *"the tag was cut between my test reading the namespace and my
re-check — nothing was ever wrong."* Reconstructing it from the recovered objects:

```
17:25:45   original anchor cut                     (recovered object 6209f1d)
17:26:00   2.317.0 release lands
   ...     anchor DELETED by another lane          exact time unrecorded
17:35:30   the suite run begins  -> RED, MISSING ['2.317.0']
17:38:15   anchor RESTORED                         (current tag object)
17:38:30   re-run -> GREEN
```

**The red run falls inside the deletion window.** It was not a not-yet-cut tag; it was a **destroyed
rollback path for a shipped release**, observed mid-restore. So the honest tally is not "observed
twice, both innocent":

```
confirmed innocent red-gate instances : 0
confirmed TRUE POSITIVE               : 1
undiagnosed (a morning red on the sibling gate, attributed to timing, never checked) : 1
```

> **The benign and the malign cause produce an identical observable** — missing, then present. A clean
> re-measurement does not discriminate between them; it only confirms the current state. **A re-run
> that goes green tells you the state changed, not that the red was wrong.**

The benign reading was chosen because the second measurement looked clean, then generalised into
*"overwhelmingly a timing artifact"* and relayed onward — a rule about base rates derived from a
sample of one, whose one member was the opposite of what it was taken to be.

**The procedural half survives and is still worth doing:** before filing a defect against another lane
for a red anchor gate, run `git for-each-ref --sort=-taggerdate refs/tags/rollback` and re-run. Do not
"fix" the flake by loosening the gate — reading the shared namespace *is* its purpose. But re-run to
**find out which cause it was**, not to make it go away.

**The gate caught real damage that nobody noticed in either direction**: the deleting lane believed
the tag was its own, and the owning lane did not know it had ever been at risk.

> **A red anchor gate is a reliable verdict about the shared namespace and a poor signal about your
> own branch.** Re-read the namespace before concluding anything about a lane — including before
> concluding nothing happened.

**This file already carried the rule that would have prevented the misreading**, in the verification
section: *"A red result is a question, not a verdict. A verifier that fails has told you nothing until
you know **which side** is wrong."* It was not ignored — it was **inverted**. Read as *"a red is
unreliable, so discount it"* rather than *"a red is undiagnosed, so diagnose it"*, and the second
measurement was then taken as the diagnosis when it was only a fresh reading of a changed state.

> **A rule can fail by being applied backwards, which looks exactly like having applied it.** Where a
> rule says a signal needs interpretation, the failure mode is to treat "needs interpretation" as
> "means nothing" — and that reads as rigour, since the rule was consulted.

**The rule "never delete an anchor you didn't create" does not protect this, and the lane checked.**
It *had* created a tag by that name, so authorship said "mine" — the name was theirs and the object
was not. A name you created can silently become someone else's:

> **Never delete a shared anchor without re-reading its CURRENT target.** Authorship of the name is
> not ownership of the object.

### A tagger date is not a creation date

**I got the cause of that window wrong and wrote a general rule on top of it.** Measuring anchor-cut
times against release-commit times gave 13 negative windows and one positive (+735 s), and I concluded
the outlier was "handled off-script — a collision renumber done by hand, so the step was skipped." It
reads well and it is **false**. Two artifacts decide it:

```
ship_2317.py                            EXISTS -- that release used the SAME script as the others
tag message "Pre-v2.317.0 anchor (...)" my format, my wording, restored verbatim by the other lane
tagger date 17:38:15                    the RESTORE, not the cut
```

The anchor was cut on-script like every other one, deleted by another lane, and recreated. **A tag is
a mutable name: recreating it resets the tagger date, so that field records the last write, not the
first.** I read it as a creation date and inferred a *procedure* from it — the same error this file
already names one section up (*a measurement of an artifact cannot tell you the intent of the code
that produced it*), committed seven minutes after writing that sentence down.

**Consequences, kept separate because they decay differently:**

* The **table is repairable, not merely caveatable.** The displaced tag object survived as a dangling
  object and was recovered — the original was cut at **17:25:45** against a release that landed at
  **17:26:00**, a **−15 s** window in line with the other thirteen. **All 14 are negative; there is no
  outlier.** Report cut times from the object you can still read, not from the current ref.
* **The generalisation is not merely unsupported — it is CONTRADICTED by its own only case.**
  2.317.0 followed the normal path precisely. *"Plausible but unevidenced"* and *"the one case cited
  refutes it"* land very differently on a reader, so state the second. The rule's shape is the
  dangerous kind either way: it sounds earned, and nobody would have questioned it.

### The object you displaced is still there — archive it before gc

**A biased instrument is not a useless one, if you know the direction of the bias.** Before discarding
the window table, note that the error here is **one-directional**: an ordinary rewrite stamps *now*,
so it can only move a date **later**.

```
observed_date   >=  true_creation_date
observed_window >=  true_window
```

* A **negative** observed window is therefore **certain** — a rewrite could only have made it look
  *more* positive, so anything reading negative genuinely was. **All 13 hold.**
* A **positive** observed window is the only uncertain case, because a later rewrite is exactly what
  manufactures one. **That is the single row, and it is the one that failed.**

The flaw lands precisely on the row it corrupted and cannot touch the other thirteen. So *"13 of 14
anchors were cut before their release landed"* survives, and the recovered object promotes it to
**14 of 14**. What dies is the causal story about the exception.

**What does NOT follow from it, though it was drawn at the time:** that a red anchor gate is
"overwhelmingly a timing artifact". **The window measures when anchors were CUT; a red gate measures
whether one is MISSING when the test runs, and a deletion at any later moment is invisible to the
window.** Two populations, no implication between them — and the one diagnosed red-gate instance was
a real deletion (see *a red anchor gate is a question whose answer may be that a rollback path is
gone*). A clean measurement of the wrong population supporting a claim about another is this file's
most repeated defect, committed here inside the paragraph that rescues a different measurement.

**But state the assumption, because monotonicity is not a law — measured, not reasoned:**

```
first cut                     18:16:10
normal rewrite                18:16:11    later
GIT_COMMITTER_DATE forced     2020-01-01  EARLIER, and nothing in the object marks it
```

One environment variable defeats the direction of the bias and leaves no trace. Here the assumption is
checkable (every tag was cut by one of two known scripts, neither of which sets it), so the rescue
holds — **as a stated premise, not as a property of git.**

**`git tag -d` prints the sha of the object it removes, and that output line is the only record it
ever existed.** The dangling object stays readable until something runs `git gc`, so the evidence has
a **hidden expiry that nothing marks** — the same shape as a stale hedge, one layer down in the object
database.

```
git cat-file -p <displaced-sha>              still resolves; tagger epoch is the ORIGINAL cut
git update-ref refs/tags/archive/<name> <sha>   makes it reachable, hence unprunable
```

Recovered here as `archive/original-pre-v2.317.0-precollision`, and the value recorded in prose above
so the finding survives even the archive. **Two rules, and the second is the one that was missed:**

> **When you overwrite or delete a shared ref, the displaced object is still there and still readable
> — archive it deliberately rather than relying on scrollback.** And **record the VALUE, not the sha**,
> because the sha's referent has an expiry and the value does not.

**A footnote on the probe that measured this, because it is the same class one layer up:** its
"anchors whose target already stamps ≥ its own version" check reported two defects, both false.
It compared versions **as strings** — `"2.9.0" >= "2.10.0"` is `True` lexically — so it flagged
`2.10.0` and `2.100.0`, the only two anchors in 341 where semver and lexical order disagree. Correct
predicate, wrong comparison; and it produced a *plausible* two rather than an absurd number. Compare
versions as tuples of ints, never as strings.

**Cut the anchor before the release commit lands** regardless — it is what the scripts already do, and
it costs nothing. Just do not cite the measurement above as evidence that anyone ever failed to.

### "0 missing" from a document-scoped gate is a statement about the document

`test_every_released_version_has_an_anchor` derives its population from `CHANGELOG.md`, and reports
**0 missing anchors** — correctly. Reconciling a count pair the anchor probe printed and I had let
stand (*341 anchors, 344 versions stamped*) shows what that zero covers:

```
VERSION values stamped on reachable commits   344
versions with a CHANGELOG entry (the gate's population)  154   oldest 2.87.0
  of those, missing an anchor                              0
stamped but never given an entry                         190   all older than 2.87.0
```

**The CHANGELOG's entry range begins at 2.87.0**, so the 190 are *absent from the document*, not
unparsed by the gate — checked by scanning for version tokens in **any** shape (166 found, 12 of them
prose mentions like "regressed in 2.51.0", none of them entries below 2.87.0). No defect: the 38
stamped versions with no anchor all predate the convention.

> **A gate whose population comes from a document reports on the document's coverage, and a reader
> will hear it as reporting on the repository.** State the denominator — here, 154 of 344, or 45%.

The transferable habit is smaller than the rule: **reconcile any count pair you print.** `341` and
`344` sat next to each other in my own output, unexplained, in a script I had already handed to
another session.

**Handing a number back is two-sided, and both halves failed here at once:**

> **A number, once given, needs an explicit hand-back before the giver takes it — and a decline must
> name what it declines.**

Three duplicate implementations came out of that pair. The lane declined three *fixes* and never said
"the number is free"; the integrator read the declines as a returned number and shipped it. **Each
party inferred the reasonable thing from what the other actually wrote**, which is what makes it an
ambiguous protocol rather than a mistake by either. And **silence is the one signal that means nothing
at all** — it was read as a hand-back twice, at the cost of a full implementation each time. A yes/no
costs one message.

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
   other reasons never read it. **Proximity is not attention** — a comment adjacent to the code you
   are editing is among the most likely things in the file to be skipped, because it reads as context
   rather than as evidence.
2. **Ask what the artifact would lose**, not whether it looks wrong. `35/35 carry a FILTER(` is a
   one-command question and it ends the argument. **This is the only one of the three that works
   without suspicion already in hand** — it can be asked of a claim nobody is doubting, which matters
   because every error recorded here landed on a claim its author was confident about.
3. **Date the claim.** The comment asserting the labels "can never disagree" was added 38 releases
   *after* the code that makes them disagree — answerable from `git log -S` alone, with no build.

Generalised: **a measurement of an artifact cannot tell you the intent of the code that produced it.**
Before "fixing" a systematic divergence, establish that it is not a contract.

### Carefulness is not a defence — it fails in four directions

The three hardest claims to check are all *careful* ones. Each escapes scrutiny by resembling the
virtue that should prompt it, and no amount of being more careful helps, because being careful is what
produces them.

| shape | how it reads | why it escapes |
|---|---|---|
| **over-claiming** wrapped in self-criticism | rigour | cataloguing your own error *is* a form of authority, and it displaces the evidence rather than accompanying it |
| **under-claiming** a property you only partly measured | modesty | you measured one of its jobs, found that one unexercised, and labelled the whole guard unexercised |
| **a hedge** used instead of an available check | appropriate caution | *"as far as I can tell"* is indistinguishable, to a reader, from having looked |

**They are not symmetric in time, and the third is the dangerous one because it was CORRECT:**

```
over-claim    wrong on arrival
under-claim   wrong on arrival
hedge         RIGHT on arrival, wrong later, and reads as having looked
weak retraction   RIGHT on arrival, leaves the claim ALIVE, and reads as scrupulousness
```

**"Be more careful" defends against the first two and is useless against the third** — there was
nothing to be careful about at the moment of writing. The only defence is re-deriving it once the
check becomes available, which is why the hedge rule carries an expiry rather than a caution. Same
shape as a count that was true at one engine and stopped being true at the next: **a claim that was
right when made, with nothing in the artifact marking when it stopped.**

**The fourth is the hedge aimed at a retraction, and it is worse than the hedge** because a retraction
is the one artifact a reader trusts to be complete. Written here as *"plausible, probably true, no
support from its only cited case"* when the truth was that **the only cited case refutes it** — and
the softer form **invites the reader to keep the rule and go looking for better evidence.** Every word
of it was accurate. It was produced while deliberately trying to be rigorous, which is the mechanism:

> **Retract at the strongest level the evidence supports, not the safest-sounding one.** *Unsupported*
> and *contradicted* are different verdicts, and hedging between them keeps a dead claim alive.

All four were committed here, by different sessions, on different days' worth of otherwise careful
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

### A guard written against a class is not exempt from the class

The two sharpest instances here were both committed *inside the work that fixes them*, minutes after
the rule was written down:

- The first version of the **vacuous-skip test was itself vacuously skipped.** `pytest.skip()` raises
  `Skipped`, which derives from `BaseException`, so a `pytest.raises(Exception)` let it escape and
  marked the test skipped — while its first assertion had already succeeded and gone unreported. A
  test written to stop a silent pass, silently passing, on its first run.
- The first **argument pin could never fail**: `re.search` found the *definition* line before the call
  site, so it read the parameter list rather than the argument list. A pin written to catch "one level
  short of the property" was itself one level short — and the injection script had made the identical
  mistake two minutes earlier, rewriting the definition instead of the call.

Neither is carelessness in any recoverable sense: the predicates were *reasoned*, and the reasoning
was about the same failure the guard targets. **Assume your guard has the defect it guards against,
and prove it red before believing it green** — which is why every gate here carries a positive control,
and why every injection script must **assert its injection landed** before reading the result. Without
that line, a patch that failed to apply plus a green suite reads as *"the guard survived"* when it
means *"nothing was tested."* That happened twice in one hour.

**And the inverse: the presence of an alarm is not evidence either, unless you check WHICH alarm.**
An injection into the canonical tree alone turns the full suite red via **`test_mirror_parity`** — one
tree edited and not the other — and that red reads as *"the suite catches this regression"* when the
suite catches nothing of the kind. Measured on the wrap-site argument: patched canonical-only, the
canonical pin fires and the mirrored copy stays green (correctly, since each resolves its source
relative to itself); patched in both, both fire. **A harness that edits one tree must patch both, or
its result describes the mirror check rather than the property under test.**

This repo already carries *"no conflict is not evidence of correctness"*. This is the same statement
about the other signal: **a green needs to be shown it can go red, and a red needs to be shown it is
the right red.**

### Correct, recorded, and in a place the reader never visits

Distinct from every rule above, because **nothing about it is wrong**, which is why it is hard to see.
Three instances in this repo, found only because a parallel session noticed they were one shape:

| the disclosure | where it lives | where the reader is |
|---|---|---|
| `_wrapper_measure_name` keeps the Tableau name deliberately — *"this name is NOT internal"* | a docstring in `migrate_estate.py` | reading a report where label and binding disagree |
| a dispatcher branch is blank on purpose, with measure, branch number, awaited sibling and reason | the `partial_fidelity` report section | looking at a blank visual in Desktop |
| the artifact rung of the call-site pin is deliberately out of scope, and why | session notes | reading three green tests in the test file |

**All three are correct reasoning, correctly recorded, filed where the person who needs it does not
look.** The reader's next act is identical to the reader's next act if the reasoning had never
existed: conclude the label is a bug, conclude the blank is a defect, conclude the pin is complete.

**Two of the three placements were deliberate and defensible, and that changes nothing about the
outcome** — deliberateness is a fact about the author, not about what the reader ends up believing.
Nor is the remedy always "move it": surfacing the dispatcher disclosure as a *gate* would fire on
what the engine already announced and regress 2.290.0. The remedy is to move it **toward the
reader's surface**, which is sometimes a docstring, sometimes a report line, and sometimes a
rendered annotation — but it is never "it's already documented".

> **Ask where the person who will form the wrong belief is standing, and put the correction there.**
> A disclosure filed anywhere else is a record that you knew, not a control that anyone learns.

**A fifth instance is different in kind and sharpens the rule past "put it where the reader looks."**
The four above were disclosures *someone authored* and misfiled. This one nobody wrote: **`git tag -d`
prints the sha of the object it removes**, correctly, at exactly the right moment — into stdout, a
stream nobody treats as evidence. It was the only record that the displaced anchor had ever existed,
and it survived by accident, in scrollback.

> **A disclosure emitted into a transient stream is not a disclosure, however correct.** The fix is
> never better wording — it is a **durable sink**.

Which is why the recovery here made the ref *before* reading anything from the object: a value you can
still print is not the same as a value that will still be there.

**A further instance arrived ninety seconds after this section was committed, and the author was the
same person who had just written it.** A generalisation of mine was withdrawn (see *a tagger date is
not a creation date*), and the retraction was sent to the lane that had **supplied the correction** —
not to the lane that had **received the claim** and was building on it. That lane endorsed the
withdrawn rule in its next message, from a branch four commits stale that had never contained either
the claim or its retraction. **The error travelled by message; the retraction travelled by message to
someone else; the file was correct for that reader the entire time.** So:

> **A retraction is owed to everyone who received the claim, never only to whoever corrected it.**
> Correcting the record and correcting the recipient are different acts, and the second is the one
> that changes what anyone does.

This is mechanical, unlike most of this file: **list the recipients of the claim, and send to that
list.** It requires no judgement and no noticing.

**One signal was available on the receiving side and neither party used it.** The lane's endorsement
read *"it inverts how I'd have read the data"* — an unusually strong reaction to a rule derived from a
**single row**. That asymmetry is visible without knowing anything about tagger dates:

> **An inference whose weight greatly exceeds its evidence base is detectable from the shape of the
> claim alone.** One data point carrying a general rule about process is worth challenging before you
> know anything about the measurement.

Rare in this file, because it needs no domain knowledge and no access to the artifact — it reads the
*claim*, not the thing claimed.

### Where an invariant can be expressed as equality, express it as equality

The string-vs-semver footnote above cost two false positives in a probe. The two shipped gates that
also compare versions were both immune, **and only one of them for a good reason**:

```
test_rollback_anchors_resolve.py    [int(x) for x in v.split(".")]     parses to int tuples -- correct
test_changelog_version_chain.py     if frm != below_to                 makes no ordering comparison
```

The chain gate asserts each entry's declared predecessor **equals** the version produced beneath it,
so version *ordering* never enters it. **A comparison you do not make cannot be wrong** — and that is
a stronger guarantee than making it correctly, because it cannot be reintroduced by a later refactor
that "improves" the comparator.

> **Ordering needs a correct comparator; equality needs nothing.** Prefer the invariant that has no
> comparator to get wrong.

### Which of these rules actually work

Most of the rules above require you to **notice something first** — that a count is unexplained, that a
claim is unmeasured, that two probes agree suspiciously, that a population might not be the claim's.
Measured across a day of parallel sessions, that is precisely where they fail: **every error caught
landed on a claim its author was confident about, and none landed on anything they were already unsure
of** — because uncertainty triggers the careful behaviour by itself, without a rule.

So the rules that depend on noticing are dead weight in exactly the cases that matter. The ones worth
relying on are **mechanical** — they cost the same whether or not you think you need them:

- **`import` the engine's tested reader instead of writing a probe.** Every ad-hoc probe written
  across three sessions invented a worse predicate than one already in the repo.
- **Print the population and the engine** with any count — the root swept and the build that produced
  the artifact. Both, always, because they fail differently.
- **Assert the population is non-empty** before reading a result over it.
- **Write the expected number into the assertion** (`assert hits, "git grep said 2"`), so a silent zero
  becomes a failure rather than a tidy answer.

Everything else is a description of how these failures look afterwards. Useful for a post-mortem;
unreliable as a defence, because **the check is cheapest exactly when you are most sure you do not need
it.**

The four share a property the list does not make obvious, and it is the reason they work: **each one
fails LOUDLY when the probe is broken.** `assert non-empty` raises; the expected-number assertion
raises; a printed population is visibly wrong to a reader; and importing the engine's tested reader
means there is no probe left to be wrong. **Every rule that requires noticing fails silently** — the
count is plausible, the zero is well-formed, the skip is one line in an already non-zero total. That
is the whole distinction: a rule you must remember to apply protects nothing at the moment you are
confident, and a rule that crashes protects you whether or not you were paying attention.

**And the honest qualification: the mechanical rules did not find most of what was found here.** Every
significant error above was caught by a *different session measuring the same object* — not by a rule
firing. What made that work has two halves, and neither is mechanical, so neither can be written as a
check:

- **Say which of your claims you have measured and which you have only reasoned to**, and correct them
  in public when they break. That makes being wrong cheap enough that declining authorised work, or
  reopening something you already closed, is the obvious move rather than a risky one.
- **Treat a late correction as information rather than as relitigation.** Reopening a closed finding
  is only cheap if the other party never suggests the matter was settled — including when the
  correction is to a retraction, or to a correction.

The rules are what protect you when nobody else is looking at the same object. **They are not what
found these.**

**One exception, and it is the whole case for keeping the gates.** Of roughly a dozen corrections
across the day, the suite produced exactly one — and it was the only one that was not a *belief*:

| what went wrong | caught by |
|---|---|
| ~12 wrong claims, counts, attributions, generalisations | another session measuring the same object |
| a shared anchor tag **silently deleted**, destroying a rollback path | `test_every_released_version_has_an_anchor` |

**No human noticed the deletion, in either direction** — the lane that did it believed the tag was its
own, and I did not know it had ever been at risk. So the division is not "gates are weak":

> **Gates catch corrupted STATE; other readers catch wrong BELIEFS. Neither substitutes for the
> other, and each is nearly blind to the other's class.**

Every belief-level error today survived a 5199-test suite untouched, and the single state-level error
was caught by that suite within one run. Do not read *"the rules did not find these"* as a reason to
invest less in gates — read it as a statement about which of the two failure classes a gate can see.

### Any tool whose scope has a default

The scope class kept recurring, and its most useful form is not *"print your population"* — that
requires noticing — but a place to look:

> **Any tool whose scope has a default is silently answering a narrower question than you asked, and
> its output is well-formed either way.**

That turns a discipline into a search. Non-exhaustive, all seen or adjacent to what was seen here:

```
git log -- <path>          history simplification   dropped 137 of 464 (30%)
Test-Path .git\MERGE_HEAD  .git is a FILE in a worktree; False unconditionally
a glob                     its root -- at least visible in the command
Select-String / grep       single-line matching unless multiline is asked for
a regex                    no re.S, so "." stops at a newline
os.walk on Windows         MAX_PATH truncation
```

**The default is invisible in the invocation *and* in the result**, which is precisely why "be more
careful" never catches these and reading the flag list does. The sharper statement of *why* review
cannot substitute:

> **The filter you didn't write is the one you won't check.** A tool's default is a scope decision you
> never made, and it leaves **no trace in your probe** — rereading your own code carefully cannot find
> it, because it isn't there.

**And two populations can agree under the default and diverge without it**, which is worse than either
being wrong alone — and worse still, the agreement here is **not a coincidence but an identity**:

```
measured at e88386b                simplified   --full-history
COMMITS touching VERSION                  328              465
DISTINCT VERSION values                   328              344
recurring values: 0     non-increasing steps: 0
```

Under simplification every commit returned is one that *changed* `VERSION`; `VERSION` only ever
increases, so no value recurs, so **commit → value is injective and the counts must agree.** A probe
that conflates *how many commits touched this file* with *how many versions exist* is therefore not
merely unlucky at that setting — it is **unfalsifiable** there, and the corroboration it returns
carries zero bits.

> **A check that cannot fail is not a check.** A coincidence can break and will occasionally be
> re-tested; an identity can never disagree with you.

**State the premise, because it is measured rather than guaranteed** — the identity needs monotone
versioning, and one reverted bump that restores an older `VERSION` repeats a value and breaks it. Zero
recurrences and zero non-increasing steps here, so it holds; that is a fact about this repo, of this
date. Same shape as the tag-rewrite rescue: **a sound argument resting on a premise nobody checked.**

**A near-miss worth not chasing:** earlier figures here report 327 commits / 464 full-history where a
later run reports 328 / 465. That is not a discrepancy — a release landed between the two runs.
**Denominators move while you are quoting them**, the same property as a version number in a message,
and the defence is to date the measurement rather than to reconcile it.

### An UNDEFINED population is a different defect from a wrong one

Almost everything above is *correct predicate over the **wrong** population* — the population existed
and the probe measured a different one. There is a harder sibling:

```
wrong population      a boundary exists; naming it exposes the error
undefined population  no boundary exists; nothing can be checked against
```

A closing summary here claimed **"eleven instances between us"** of the wrong-population class. The
number was never computable: *is the string-vs-semver probe one instance or two? does an injection
harness matching a `def` line count separately from the injection script that made the same mistake
two minutes earlier?* **No answer is wrong, because no population was ever defined.**

> **A count whose population needs an argument should not be published.** A wrong population can be
> caught by naming it; an undefined one can only be caught by someone asking *"what did you count?"* —
> and nobody asks that of a summary.

**Which is the half with teeth: the tally was written in the SUMMARY, not in the work.** Every
verification habit in this file was applied to the analysis and none of it to the closing paragraph,
because the checking was supposedly over. **A summary is unchecked by construction** — it is where
numbers go once nobody is looking at them any more. Write *"this kept happening"*: it costs nothing
and cannot be wrong.

### A decline is unfalsifiable unless the decliner shows their work

The most valuable single act recorded here produced **no code, no test, and no version number**: a lane
declined an obvious emitter fix four times, having established it would have stripped `FILTER(` from
55 projections across 39 visuals — **and the report would have looked *more* correct afterwards,
because the labels would finally have agreed.**

That decision is invisible to every measurement available: green suite, clean validation, a
better-looking artifact. **A rejected fix leaves no artifact at all**, so it survives in the record
only because someone asked for the reasoning each time instead of accepting "no".

> **Ask a decline for its evidence exactly as you would a claim.** Optimising a process for artifacts
> systematically deletes its best decisions.

### An elimination is only as strong as the hypotheses its instrument can represent

The undiagnosed red on the sibling gate was later swept: **every archived anchor state was checked
against the predates-gate and none violates it.** That is a real result and it is *not* a diagnosis:

> **A sweep over preserved states cannot see destruction, because destruction removes the state.** So
> "0 archived states violate the gate" is **consistent with** a deleted anchor and is no evidence
> against one — and a deleted anchor is exactly what caused the other red.

The instrument's blind spot is the confirmed cause. Which upgrades the earlier rule — *a gate that can
only report an absence must also prove it looked* — to its mirror: **an instrument that reads what
survives can never rule out what was destroyed.** Report such a result as an **elimination with its
untested hypothesis named**, never as a cause.

**Verifying that sweep took three attempts, and the first two failed the same way the file's central
class describes** — which is worth recording precisely because it happened inside the check written to
confirm someone else's care:

```
attempt 1  swept refs/tags/archive wholesale        -> 4 VIOLATIONS, all false
           archive/ holds archived ANCHORS *and* archived COMMITS; a commit's name
           records the version it SHIPPED, so the anchor predicate does not apply
attempt 2  classified by regex, swept anchors only  -> 16 checked, 0 violations
           but the classifier required `pre-v` after `/` or `anchor-`, so it silently
           dropped `archive/original-pre-v2.317.0-precollision`, a genuine anchor
attempt 3  17 archived anchors, 0 violations        -> the honest figure
```

**Two contaminated populations and one under-inclusive one, in three consecutive scripts, all written
to check a finding about populations.** Neither error changed the verdict, which is the danger: a
population bug that leaves the answer intact leaves nothing to notice. The first was visible only
because four "violations" were implausible; **the second produced no anomaly at all** and was found
only by listing what the classifier had excluded.

> **After filtering a population, print what you EXCLUDED, not only what you kept.** The kept set looks
> correct by construction; the excluded set is where a wrong boundary shows.

### When the rules pay, and the one time it was before the fact

One further note on *when* they pay. Almost every catch recorded here was **retrospective** — an
artifact already existed and someone measured it. Exactly one landed **before the action**: a version
number about to be taken would have left a hole in the release chain, and the cheaper-looking escape
(skip the number, declare a predecessor two releases back) **passes the gate on the day and breaks the
moment the skipped entry is inserted above it**. A green gate at commit time would have been the worst
outcome available, not the safe one.

That is not a better application of the rules — it is the same rule reaching a different point in the
sequence, and it is the only point at which it prevents rather than explains. **Reading a predicate is
the only method available for a defect that has not happened yet.**

## Commits

- Make the **user** the commit author, and append the trailer:
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  ```
- Do not push unless explicitly asked. Re-mirror the plugin copy and pass the green-suite +
  validation gate before each commit.
