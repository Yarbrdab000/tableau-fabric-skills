# Changelog

All notable changes to this collection are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
collection follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) at the
**collection level** — the four packaging manifests
(`.claude-plugin/marketplace.json`, `.github/plugin/marketplace.json`,
`plugins/tableau-fabric-skills/.claude-plugin/plugin.json`, and the deprecated
`tableau-migration` plugin alias) share one version. Each skill additionally carries its
own `VERSION` stamp (`skills/<name>/VERSION`).

## [Unreleased]

### Added

- **`tableau-migration` (skill `2.313.0` → `2.314.0`): a zero-entry CHANGELOG parse is now a
  FAILURE, not a skip — the gate that guards every release could stop guarding it silently.**
  Before this, a broken `_ENTRY_RE` — a format change, or a pattern mangled in transit — made every
  check in `test_changelog_version_chain` **skip**. Measured by injection: with the entry pattern
  broken the module reported **6 passed, 0 failed, 0 skipped**. A green module over a parser that
  had matched nothing.

  In a 5000-test run a skip surfaces as one more line in an already non-zero `skipped` count, so
  the chain gate — the only thing preventing a rebased stack from silently renumbering — would go
  quiet in a way no reader could distinguish from health.

  **Absent and unparsed must never produce the same output.** A missing `CHANGELOG.md` is a real
  absence and still skips (an installed skill has no repo root); a file *full of entry-shaped
  bullets* that the strict parser cannot read is a **parser failure** and now fails, printing the
  count, the first offending line, and the pattern that stopped matching.

  **The sentinel must be strictly WIDER than the parser it guards, in every dimension.** Measured
  against 148 strict entries:

  | candidate | matches | |
  |---|---|---|
  | ``- **`tableau-migration` `` | **99** | narrower — misses the second entry format this file uses |
  | `^\s*-\s.*\(skill\s` | **148** | **equal** — depends on `(skill`, which the parser also needs |
  | ``- **`?tableau-migration`? `` | **153** | wider, along the axis that has actually varied |

  The equal-width candidate is the instructive rejection: it would fall silent *for the same reason
  the parser does*, so both go to zero together — **a second instrument sharing the first one's
  blind spot, i.e. false corroboration inside a single test.** The first candidate was this
  release's own first draft, and it was caught by measuring rather than by reading.

  After the guard, the same injection reports **4 failed, 0 skipped** — every check that consumes
  `_entries()` now speaks.

  Pinned bidirectionally from one harness, so the conflation is not simply rebuilt one level up: a
  genuinely absent CHANGELOG must still **skip**, a present-but-unparsed one must **fail**, and the
  two must say different things. A third test asserts the module's *own* checks go red rather than
  merely proving the helper raises — the original defect was a green **module**, not a quiet helper.

  One near-miss worth recording: that third test was first written as a subprocess run behind an
  env-var injection hook, and would have **skipped whenever the hook was absent** — a test that
  cannot fail, added to fix a check that could not fail. Rewritten in-process.

  **Also folds in a docstring correction of the same class** (a property holds and nothing states
  it), on 2.311.0's non-DAX-function pattern. Ablation:

  | variant | `TOTALYTD` | `LOOKUPVALUE` | `MYTEXT([a])` |
  |---|---|---|---|
  | lookbehind + `\s*\(` (shipped) | silent | silent | silent |
  | no lookbehind, keeps `\s*\(` | silent | silent | **TEXT** — breaks |
  | lookbehind + `\b` instead of `\s*\(` | silent | silent | silent — safe |
  | no boundary at all | **TOTAL** | **LOOKUP** | **TEXT** — breaks badly |

  The **leading lookbehind** is the uniquely load-bearing property: only it blocks a longer
  identifier ending in a listed name. A *trailing* boundary is what protects `TOTALYTD`, and
  `\s*\(` is not special there — a plain `\b` is equivalent. The length-descending sort affects
  only which name a message reports (`TEXTJOIN` over `TEXT`); both are entries, so it is precision,
  not correctness.

  The comment previously called that guard *"defensive, not validated"*, having measured only one
  of its **two** jobs — the unexercised one. The other is exercised by an existing test. **A
  property is not validated or unvalidated; each of its jobs is, separately** — and this was
  under-claiming produced by care about over-claiming, which is a failure mode whose defence causes
  it.

- **`tableau-migration` (skill `2.312.0` → `2.313.0`): a committed git conflict marker is now a
  test failure.** Three of them sat in `CHANGELOG.md` at the tip of an integration branch carrying
  eighteen merged releases, and **every gate stayed green**: the suite passed, the version-chain gate
  parsed 145 entries and reported 0 breaks, the mirror was byte-identical, both anchors resolved.

  This is the inverse of a rule this repo already carries. *"A clean auto-merge is not evidence of
  correctness"* is about a merge git believed it resolved. This is the other half: **a merge git
  REFUSED to resolve, resolved by hand, with the markers left behind** — and every downstream signal
  still agreed, because each was measuring something the markers do not disturb. The chain parser is
  line-oriented, so a marker line is simply not an entry header and is skipped.

  The damage was invisible to a reader too. No separator line survived, only the outer two markers, so
  the file did not look broken: one marker split a bullet's header line in half, and the other pair
  wrapped a `### Added` heading that read perfectly well. **A conflict that LOOKS resolved is what
  survives review.**

  Three checks, and the second and third exist because the first can only ever report an absence:
  the gate itself; **that the scan read anything at all** (a walk matching no files reports the same
  clean zero as a healthy repo); and that the predicate fires on real markers while staying silent on
  indented or quoted ones, because this repo documents merge conflicts in prose. Proven by injecting a
  marker into a real file: fails naming file and line, passes again on removal.

  The marker strings are BUILT by repetition rather than written literally, so the module does not
  flag itself — the same trap as a format-string check matching inside the annotation that preserves
  a source formula.

  Found by a parallel session rebuilding at this engine. The markers themselves were removed
  separately and unversioned, `CHANGELOG.md` being a root file.

- **`tableau-migration` (skill `2.311.0` → `2.312.0`): the model openability self-check now
  describes the model that SHIPPED, not the one assembled before the row-predicate wrap.** The check
  was correct. It ran too early.

  `openability_selfcheck` is produced by the datasource build. `_apply_row_predicate_wrapped_measures`
  then ADDS measures to that model — `CALCULATE(<base>, FILTER(...))` wrappers standing in for a
  Tableau row-level boolean keep — and rebinds visuals onto them. The verdict therefore described an
  artifact that no longer existed by the time it was reported: a **true pass about an earlier model**.

  ```
  _value_path_bottoms_out_blank(...) on the WRAPPED parts   -> fires on 8 measures
  shipped openability_selfcheck                              -> measure_value_path_not_blank: True
  ```

  Those 8 are bound by visuals and render as **titled empty boxes with an axis label** — which reads
  as "no data for this filter", the most dismissible failure mode available. Measured on 0088 at
  engine 2.309.0.

  **The merge direction is the design, not an implementation detail.** The original call supplies
  `flatfile_headers` and `expected_endpoints`, which are not available at the wrap site, so a bare
  re-run SKIPS `typed_columns_in_header` and `endpoints_distinct`. Overwriting with it would silently
  retract two checks that genuinely ran — trading a false pass for a false *absence*, the same class
  of defect one layer over. So the merge only ever ADDS: booleans are ANDed, a check absent from the
  re-run keeps its original verdict, issues are unioned. A post-wrap pass can fail a build that
  passed; it can never pass one that failed.

  **Three fixes were considered and two were wrong, both plausibly.** Adding an `also_projected_as`
  index to `partial_fidelity` would have disclosed 2 wrappers while 8 identical ones stayed silent.
  Re-routing the wrapper rebinding through the label-derivation choke point — so binding and label
  finally agreed — would have rebound all 55 projections across 39 visuals to the **unfiltered**
  base, silently removing the date-window filter, and would have *looked* like a clean fix. The
  label/binding divergence is deliberate and documented at the site: `nativeQueryRef` is the
  user-facing Tableau name, `Measure.Property` the internal implementation measure.

  Found by a parallel session that measured the ordering at the correct engine after first measuring
  it at a stale one, and that **declined the authorised work** on discovering the approved fix was
  the wrong one. Tests pin both the merge's conservatism and its effect, the latter from one fixture
  carrying a wrapper over a `BLANK()` stub and an identical wrapper over a live base — a check that
  flagged both would be matching the `(filtered)` naming convention rather than resolving the value
  path, and would fire on the 19 correct wrappers alongside the 9 defective ones.

- **`tableau-migration` (skill `2.310.0` → `2.311.0`): a function name that does not exist in DAX is
  now an openability failure.** Nothing in this repo validated one. Every other check resolves
  *references* (`[Measure]`, `'Table'[Column]`), and a bad **function name** is neither — so all
  nine openability checks passed and the model failed at a customer's Desktop.

  **`TEXT()` is Excel. DAX is `FORMAT()`.** All **five** objects with a direct syntax error in a
  real customer deliverable called `TEXT(` — three measures and two calculated columns. Two further
  measures were broken by *cascade* off those five and call nothing invalid themselves, so the file
  held **7 broken objects and 5 causes**.

  Power BI reports it as `The syntax for '(' is incorrect.` — a message naming neither the function
  nor the problem. The agent that authored the file spent its entire investigation hunting an
  unresolved Tableau calculation id that was never involved, and **its own explanation carried the
  defect**: *"STR() → easily converts to TEXT()"*. It converts to `FORMAT()`. The failure message
  therefore names the function and the replacement, which turns a misleading engine error into a
  one-line fix — that is most of this check's value.

  **A denylist, and the reason is error direction rather than maintenance.** A missing entry is a
  false negative and tolerable; an allowlist of valid DAX functions would fire on every legitimate
  function nobody remembered to add — fatal for a gate, and it would decay every time DAX gains a
  function. Entries carry the DAX replacement where one exists (`IIF`→`IF`, `ISNULL`→`ISBLANK`,
  `IFNULL`→`COALESCE`, `COUNTD`→`DISTINCTCOUNT`, `ATTR`→`SELECTEDVALUE`); the Tableau table-calc
  family (`WINDOW_*`, `RUNNING_*`) says explicitly that **no rename will fix it**, because a
  suggestion that cannot work is worse than none.

  **`INDEX`, `WINDOW`, `OFFSET`, `RANK` and `ROWNUMBER` are DAX window functions and are NOT
  listed** — an earlier draft of the list wrongly included `INDEX`. A denylist entry that is
  actually valid DAX is the one failure this design cannot tolerate, since every hit is supposed to
  be unarguable. Pinned by a test that calls all five plus `MID`/`LEFT`/`TRIM`/`SUBSTITUTE`/
  `SEARCH`/`VALUE`/`MEDIAN`/`CONCATENATE` and asserts silence.

  **Expressions only, never annotations** — measured *before* the check was written rather than
  after it misfired. `annotation TableauFormula` preserves the Tableau source, which legitimately
  contains `TEXT(`, `IIF(`, `WINDOW_MAX(`; scanning expression-plus-annotations yields **77 false
  positives on a clean corpus**, enough to make the check unreadable on its first run and get it
  switched off.

  Population: **0 across 248 corpus measures and all 34 workbooks** — the deterministic translator
  cannot emit these — against **5/5** of the customer file's primary breaks. An assisted-path
  defect, like the bare-reference class of 2.306.0, and both landed in objects annotated
  `TranslatedBy: assisted translation (human-approved)`.

  One guard is **defensive, not validated**, and is labelled as such in place: a negative lookbehind
  stops a measure *named* `WINDOW_MAX(Avg. Days Participation)*1.2` being read as a call. Measured
  with and without it, the corpus returns 0 either way, because nothing currently references those
  measures from an expression. It should not be cited as evidence the corpus exercised it.

- **`tableau-migration` (skill `2.309.0` → `2.310.0`): 2.308.0's exclusion of disclosed parameter
  dispatchers is now pinned — bidirectionally, so it cannot be satisfied by a dead gate.** The
  exclusion was real but stated nowhere. It held only as an *emergent* consequence of
  `_calculate_value_arg` returning `None` for a computed value: a `SWITCH` is computed, so the walk
  stops before reaching the blank branch.

  **The contract at risk.** A partially-rebuilt parameter dispatcher (2.290.0) keeps its slot
  pointing at a sibling's stub — *"blank today, correct for free the moment that sibling lands"* —
  and the engine announces it in `partial_fidelity` with the measure, the branch number, the
  awaited sibling and the reason. 2.308.0's claim is *"this renders blank and nothing told you"*, so
  firing on a disclosed partial is a **different population**, and a gate that fires on what the
  engine already announced trains its reader to skim it.

  **Why it needed pinning rather than trusting.** The obvious future improvement — *follow `SWITCH`
  branches, those branches really can be blank* — is a **true** sentence, is an improvement by its
  own lights, and silently breaks the contract. A true statement doing the work of a different,
  false one. This is the mirror of the anchor incident (2.299.0): there a gate asserted a **proxy**
  and satisfying it moved the real property the wrong way; here the right property held and
  **nothing asserted it at all**. Two failure modes of one axis — asserting a proxy, and asserting
  nothing. The second has the worse signature, because there is no green check to mislead you, only
  silence, until a reasonable-looking change lands.

  **Pinned as a test, not an explicit code branch.** An early return would duplicate a mechanism the
  general rule already handles, giving two places that can disagree after the next refactor — and,
  more decisively, an explicit branch states a *proxy* (*"this path returns early"*) where the test
  states the *property* (*"the dispatcher shape is not flagged"*). Same distinction the gate itself
  turns on, applied to where the pin goes.

  **Bidirectional from one harness, which is the part that makes it load-bearing.** A test asserting
  only that the dispatcher is *not* flagged passes just as happily when the gate has stopped working
  altogether — silence from a working check and silence from a dead one are identical. So the same
  run asserts a shape that **must** be flagged. Verified by injection, not by assertion: widening
  the walk to follow `SWITCH` branches fails the exclusion, and disabling the value walk entirely
  fails the positive control, **with different messages naming different causes**.

  Also pins the delta as a shape: 0088 flags **11** on a pre-2.290.0 build and **9** on a current
  one, and the difference is exactly `Sort By (filtered)` and `Select Metric (filtered)` — the two
  the dispatcher rebuilt. Two sessions measured 11 and 9 independently and both filed it as build
  age; it was the release. **When two builds disagree, the delta may be the release you shipped.**

- **`tableau-migration` (skill `2.308.0` → `2.309.0`): a forwarding wrapper that drops its base's
  `formatString` renders a CORRECT value as a wrong number — now gated.** The inverse of every
  other check here. Everything else asks whether a value is absent or wrong; this asks whether a
  *right* value is being presented as a wrong one.

  Measured on corpus workbook 0088 and verified against the running model:
  `EXCLUDE Days Since Goal Created Date Goals Completed by (filtered)` evaluates to **0.1732** and
  renders **`0`** on the card, because it declares no `formatString` while the measure it forwards
  declares `0.0%`. A reader sees a zero. Every value-level instrument reports it healthy —
  *correctly*, because it **is** healthy. Only the presentation is missing.

  Worth stating beside the customer defect behind 2.306.0, because they are opposite halves of one
  class and both read as "zero" to whoever is looking:

  | | |
  |---|---|
  | `VAR _val = 0` through currency formatting | a **wrong** value formatted **confidently** |
  | `0.1732` with no format at all | a **right** value formatted into a **wrong** one |

  **Scoped to forwarding wrappers**, which is what makes the rule safe rather than a matter of
  taste: `CALCULATE([X], <filters>)` returns the same *kind* of number as `X`, so inheriting its
  format is the only correct answer. A measure that **computes** its value — `DIVIDE`, arithmetic,
  an aggregation — legitimately owns a different format from its inputs and is never flagged.
  Propagation is not broken in general: `Clients per Staff (filtered)` inherits `#,##0;-#,##0`
  correctly. It is the EXCLUDE/LOD path that drops it.

  **Narrowed to percent formats on measurement, not taste.** A percent format scales what the
  reader sees by 100, so losing it turns 0.1732 into `0` — the only case where a lost format is a
  *correctness* bug rather than a styling one. The unnarrowed rule fires **5** times on the corpus:
  2 percent (the verified defect and its intermediate wrapper) and 3 forwarding an integer `0`
  format, where 7 still reads as 7. Those 3 are real fidelity losses and are deliberately **not**
  claimed — failing a build's openability for a lost thousands separator would invite exactly the
  allowlisting this repo refuses, and would dilute a check whose entire value is that every hit is
  a wrong number on screen. Recorded in the tests rather than dropped.

  An earlier hand-measurement of this population said **1**, because it resolved only a single
  `CALCULATE([X], ...)` level and missed both the alias form and the nested chain. Third instance
  in one session of a one-level instrument against a multi-level structure, and the reason the
  check resolves the value path rather than pattern-matching a wrapper.

- **`tableau-migration` (skill `2.307.0` → `2.308.0`): a measure can render blank while its
  expression is not `BLANK()` — now a hard openability failure.** Found by opening a build,
  refreshing it and looking at it. No static check in this repo could see it, and the reason is
  structural rather than careless: every stub-accounting instrument matched
  `expression == "BLANK()"`, and these measures are live `CALCULATE(...)` expressions.

  ```dax
  Avg. Days Participation (filtered) =
      CALCULATE( [Avg. Days Participation],                  -- a BLANK() stub
                 FILTER('Case', 'Case'[CreatedDate] >= [Start Date Value]), ... )
  ```

  It resolves, it validates, its `State` is `Ready`, and it renders empty. **A one-level detector
  against a two-level defect** — the population, one indirection down.

  Measured on corpus workbook 0088: 163 measures, 31 direct stubs, and **11 more** reachable only
  through a wrapper, **all 11 projected by a visual**. Confirmed four independent ways before being
  written down — a static TMDL parse, `INFO.MEASURES()` against the running model, a DAX query
  returning `null` for all seven owners, and finally the render: a five-measure card entirely blank
  and a bar chart with no bars. **Two symptoms that had been tracked as separate unexplained
  defects turned out to be one measure.** The new check reproduces 11/11 on the corpus and flags
  nothing in the other 33 workbooks.

  **Only `CALCULATE`'s first argument carries the value**, and that is the whole discriminator. A
  first cut at this analysis asked whether *every* referenced measure was blank, which is false for
  every real wrapper, because filter arguments routinely name live measures
  (`'Case'[CreatedDate] >= [Start Date Value]`). That probe reported **zero** affected models
  against one since measured to have eleven — the wrong population, inside the instrument built to
  find wrong populations. It is pinned as a test rather than described.

  **The population is derived, not named.** The models that exposed this used an `X (filtered)`
  suffix, and keying on that suffix would have worked on them while silently covering nothing
  anywhere else. Nothing in the check reads a measure name — it resolves each measure's value path.
  The independent reproduction surfaced a wrapper over a measure literally named `1`, which a
  name-shaped matcher would have skipped and which this one catches.

  Deliberately conservative in two directions, because this check spends its life reporting
  silence. The honest `= BLANK()` stub is **not** flagged — it is disclosed by design and is the
  convention the check protects. And the chain stops once a value is *computed* rather than
  forwarded: `CALCULATE(DIVIDE([Stub], [Live]))` is not reported, because `DIVIDE(BLANK(), 5)` is
  blank but `COALESCE([Stub], 0)` is **zero** and `ISBLANK([Stub])` is **TRUE**. Guessing there
  would produce exactly the confident-wrong-answer this repo treats as worse than silence.

### Fixed

- **`tableau-migration` (skill `2.306.0` → `2.307.0`): the last narrow CHANGELOG reader is widened —
  a gate certified "every released version has an anchor" while examining 89 of 140.**
  `test_every_released_version_has_an_anchor` matched only the Unicode arrow. The file carries **two**
  entry formats — 89 newer entries with a backticked skill name and `→`, and 51 older ones with a bare
  name and ASCII `->` — so the roster it built silently omitted **51 releases**, and reported that
  every version in it had an anchor.

  **The tell was inside the function.** Its own docstring says it reads the ``(skill `A` -> `B`)``
  chain — writing the **ASCII** arrow — directly above a pattern that accepts only the Unicode one.
  *The prose already specified the grammar the code did not implement.*

  This is the sibling of the reader fixed in 2.305.0, in the same module. Fixing one reader in a
  module with two would have been the same shape as a repair that satisfies a gate without fixing the
  thing it stands for — so the population was re-derived rather than the named case patched: **239
  python files swept, exactly one narrow reader remaining, now zero.**

  **Measured safe before the change**: roster 89 → 140, missing anchors **0 either way**. The widening
  buys 51 newly-judged releases and surfaces no new violation, so this is coverage, not a repair.

  The general rule, which this is the third instance of today: **a verifier narrower than its subject
  does not merely miss the region it cannot see — it CERTIFIES it**, with the authority of an
  independent check. Write the predicate from the subject's grammar, never from the sample in front
  of you.




### Added

- **`tableau-migration` (skill `2.305.0` → `2.306.0`): a measure that names a COLUMN unqualified is
  now a hard openability failure — the model opens, and the measure fails when queried.** Found in a
  real customer deliverable, not in the corpus. Its agent reported that a Tableau calc id "didn't
  resolve to a column in the migrated model" and said it had therefore chosen a conservative stub.
  Every part of that was refuted by the file itself: a sibling calc id in the same model *had*
  resolved deterministically, the referenced calculation sat in the same table with `state: Ready`
  and its formula attached, and the emitted body was **`VAR _val = 0`** — not `BLANK()`.

  That distinction is the point, and it is why this ships as a gate rather than a note. This repo's
  stub convention is `= BLANK()`: structurally valid, semantically absent, and it *looks* empty. A
  hardcoded `0` run through the same measure's currency and percentage formatting renders **`$0.00`**
  and **`0.000%`** — a confident wrong number in every visual that touches it. A customer reads
  `$0.00` as "zero dollars", not as "we declined to translate this". The agent's own justification
  was that it wanted to avoid *silently producing wrong outputs*, while emitting the one shape that
  does exactly that.

  The actual break was mechanical: `SWITCH([display_format], ...)` where `display_format` is a
  **column** on `'Custom SQL Query'`. Power BI: `SemanticError`, *"The syntax for '(' is
  incorrect"*. Five measures broken — three primary, two rebound onto them. Corrected and verified
  against the real 17,705-row model: NSD Availability `$0.00` → **77.4%**, Outage Volume → **1.11K**,
  HFC QC → **98.51**.

  `dax_references_resolve` (2.230.0) could not see it. It accepted an unqualified `[Name]` that
  resolved against measures **or** columns, on the reasoning that Power BI allows an unqualified
  column reference in a row context. Both defects' names *are* columns, so both passed a check
  written to catch exactly this class. **The allowance was measured before it was removed, not
  after**: across 248 corpus measures exactly **one** bare reference resolves to a column and not a
  measure — and that one is itself a defect (0088, `CALCULATE([Client per Staff Max Goal], ...)`
  where only the column and the what-if measure `... Value` exist; Power BI: *"The value for
  'Client per Staff Max Goal' cannot be determined."*). The same expression gets `[Start Date Value]`
  right, so it is a dropped suffix, not a house style. False-positive rate **0/248**.

  Reported as its own check, `bare_column_references_qualified`, rather than folded into
  `dax_references_resolve`: the reference *does* resolve to a model object, so calling it
  unresolvable would misdescribe it and send a reader hunting for a missing column that exists.

  **The systemic finding is the provenance stamp.** All three primary breaks carry
  `TranslatedBy: assisted translation (human-approved)`; the deterministic path qualifies its
  references correctly in the same file. The approval certified *"the formula looks right"*, not
  *"the measure compiles"* — this session's population rule applied to a **trust label**, which is
  worse than applying it to a metric, because a metric gets re-measured and a provenance stamp gets
  relied upon. A measure sitting in `SemanticError` shipped to a customer carrying a human's
  approval, and the defect is specifically in the path that has a human in it, which is precisely
  where nobody thinks to add a machine check.

  Two notes for whoever extends this. The matcher must scan the **expression only**: a first cut
  scooped `annotation TableauFormula` into the body and reported **370 violations across 248
  known-good measures**, because a preserved Tableau formula legitimately names Tableau ids —
  expression-plus-annotations was not the claim's population. And the check is non-vacuous by
  injection, not by assertion: a bare reference to a real column in a clean corpus model is caught,
  the qualified form of the same measure is not, and the same name appearing only inside an
  annotation is not.

  Shipped alongside it, in the same release: **the anchor-predecessor gate (2.299.0) no longer fires
  on anchors this branch cannot reach** — that gate's own flaw, exposed on its own author's branch
  within a day. It compares a **shared
  mutable resource** (`refs/tags`, one namespace across every worktree) against a **branch-local
  file** (`CHANGELOG.md`). When the integrator re-points an anchor for the *merged* world — the
  correct repair this gate asks for — every lane branch immediately sees a merged-world anchor beside
  its own lane-world chain and goes red through no fault of the lane. Observed concretely:
  `rollback/pre-v2.299.0` was re-pointed to a commit stamped 2.298.0, right for the merged line where
  2.296–2.298 sit between, while this branch's chain still declares 2.295.0, right here. Neither is
  wrong; they describe different histories, and only one of them contains the anchor's commit.
  Ancestry is what tells them apart, so an anchor whose commit is not reachable from `HEAD` is
  skipped as undecidable — the same skip the companion test already made, arriving from the opposite
  direction and missed when this gate was written.

### Fixed

- **`tableau-migration` (skill `2.304.0` → `2.305.0`): a rollback anchor must be an ANCESTOR of the
  release it anchors — the previous invariant was wrong and its repair advice damaged six anchors.**
  2.299.0 asserted that `VERSION` at `rollback/pre-vX` equals the predecessor X's CHANGELOG entry
  declares, and told the integrator to fix any mismatch by re-pointing at the merged parent. That
  advice was followed. It broke every anchor it touched.

  ```
  archived original -> re-pointed value      is it an ancestor of its own release?
  2.274.0  9d822fc7 -> 181d3bbd                       True -> False
  2.293.0  181d3bbd -> c0c3b30d                       True -> False
  2.296.0  c0c3b30d -> bb4026d2                       True -> False
  2.299.0  bb4026d2 -> 026c83d0                       True -> False
  2.300.0  c55e6644 -> 0e5fbf1b                       True -> False
  2.302.0  84345a3f -> 3c3a9818                       True -> False
  ```

  **Six for six.** `git reset --hard rollback/pre-v2.302.0` landed on a parallel lane, not on any
  history leading to 2.302.0 — and **the stamp gate went green the whole time.** This is the sharpest
  instance of a rule this repo already carries: *satisfying a check is not evidence about the
  property, and optimising for the check can move the property the wrong way.*

  **Why the two disagree.** An interleaved merge leaves the repo carrying **two orders**: the
  **narrative** order (the CHANGELOG chain, which readers and version gates use) and the **causal**
  order (git ancestry, which `reset --hard` obeys). *"One release before"* is well defined in each
  and they **name different commits**. A stamp comparison silently picks the narrative order for a
  tag whose only executable meaning is causal. The founding defect that motivated 2.299.0 —
  `pre-v2.293.0` "landing two releases short" — was itself measured on the narrative chain:
  2.291.0 and 2.292.0 are absent from that anchor **and equally absent from 2.293.0's own release
  ancestry**, so relative to 2.293.0 the original lost nothing.

  Blast radius was tried as a tie-breaker and **does not discriminate** — narrative 69 vs causal 67
  over the 14 most recent pairs, each winning on different anchors — because both candidate targets
  sit on lanes. Recorded so the next reader does not re-litigate it by "just measuring".

  **The replacement is stated in ancestry alone**, so it needs no choice between the two orders and
  **no exception ledger**: the anchor must be an ancestor of its own release commit. 139 chain pairs
  judged, 139 satisfy. The two historic double-introduced versions (`1.23.0`, `1.25.0`) that any
  introducer-based formulation would have had to except are simply not a special case here — the
  invariant never asks a version to have a unique introducer.

  **`_INTERLEAVE_DEBT` is retired, and its existence was the tell.** It was a ledger of exceptions to
  a proxy, designed to shrink as they were "paid" — and every payment made the repo worse. *A gate
  that needs no exceptions is usually stating the property; one that accumulates them is usually
  stating a proxy.* The failure message now explicitly warns against the repair that caused this.

  Also fixes `_chain_pairs`, which matched only the Unicode arrow and so judged **88 of 139** entries
  while reporting nothing amiss — the same narrow-verifier defect that this module's sibling gate
  exists to catch, living inside this module. Both arrow forms now, per the subject's grammar.

  Verified red by injection (an anchor pointed at `HEAD`, restored immediately) with the five sibling
  gates staying green, and a negative control asserting the detector distinguishes a bad pairing from
  a good one — it spends its life reporting silence, which is where a broken detector hides.

- **`tableau-migration` (skill `2.303.0` → `2.304.0`): a review reason now names the ROUTE, not the
  byte — the old wording was accurate and reliably sent readers the wrong way.**
  `unsupported character '<'` is literally true, and it produced the same wrong inference **twice in
  one afternoon in the same reader**: *"there is a comparison-operator parsing gap, close it."* There
  is a parsing gap — `SUM([Sales]) < 5`, with no reference at all, fails in the tokenizer before any
  resolution runs — but **closing it would be the wrong move**, because this compiler's subset is
  deliberately arithmetic and a calc needing boolean logic has no Visual-Calculation form here at all.

  **A diagnostic that is accurate and misleading is worse than a vague one, because its precision is
  what earns the trust.** The reason now answers the question a reader actually has — *is this a gap
  to close, or a route I should not be on?*

  ```
  [Above avg.?]: unsupported character '>'
  [Above avg.?]: boolean/conditional logic ('>') is outside the arithmetic Visual-Calculation
                 subset; a calc that needs it belongs on the model measure path
  ```

  One cause was arriving as **three different-looking messages**, which is why it read as three
  separate gaps: `'<'` / `'='` / `'>'` from the tokenizer, and `expected '('` from an `IF` lexed as a
  function call (plus `trailing tokens after expression` for a bare `AND`). All now resolve to the
  boundary wording. Corpus-wide that is **10 of 27** review rows.

  Scoped deliberately: an LOD brace keeps the plain `unsupported character '{'`, because it is a
  different refusal with a different remedy — over-broad diagnostics are the failure this removes,
  not one to relocate.

  Disclosure only, proven at the artifact: rebuilt all 34 corpus workbooks — `emitted_total` 11 → 11,
  `review_total` 16 → 16, `visuals_projecting_stub_measures` 5 → 5, and every PBIR report definition
  byte-identical (0 files differ across 0070, 0074, 0075 and 0088).

- **`tableau-migration` (skill `2.302.0` → `2.303.0`): a swallowed test body is now a hard failure,
  and the gate found one already in the tree on its first run.** An edit that replaces a
  `def test_x():` line — a rename, a reorder, an insertion whose `old_str` happened to end there —
  deletes the header and leaves the body behind. Those statements land **inside the preceding test**
  at the same indentation and keep running. If their assertions still hold, the suite stays green
  and **one test has silently ceased to exist**.

  The pass count is exactly the wrong signal, because orphaned assertions pass just as happily
  inside whatever function absorbed them — and the count can go *up* while a test disappears. This
  is the same shape as every silent defect in this collection: the output met expectation, so
  nothing objected. **A green suite cannot tell you a test still exists.**

  Measured twice within one hour. A new test in `test_compiler_routing.py` swallowed the body of the
  test below it and **passed**, caught only by diffing collected node IDs against the previous
  commit. Then this gate, on its first run, found a pre-existing one: the `_definition_of_done`
  precedence and `_dod_banner` assertions in `test_migrate_estate.py` had been running as trailing
  statements of `test_dod_openability_failure_helper_tolerates_missing_and_ok`, a test about an
  unrelated helper. Repaired as the sibling `test_dod_status_precedence_and_warn_banner` — the
  restored `def` is the only change; not one assertion was touched.

  **The fingerprint, and why the predicate was measured before it was trusted.** Module-level
  definitions are separated by exactly two blank lines, so an eaten `def` leaves its body after a run
  of ≥ 2 blank lines *inside* the previous function. Swept over the whole suite the predicate flagged
  **1 function in 4194** — and that one was a real swallowed test, not a false positive. That rate is
  why this ships as a hard assertion rather than a warning.

  Deliberately **not** an expected-count baseline: a manifest needs updating on every added test,
  which turns it into a rubber stamp, and it cannot say *which* test vanished. This reads the source
  and names the function and line. The gate also carries its own negative control — a synthetic
  swallow it must detect and a clean module it must not fire on, because a check that cannot go red
  is indistinguishable from one that found nothing — and asserts it examined > 100 functions, since a
  sweep that reaches zero of them would look identical to success.

- **`tableau-migration` (skill `2.301.0` → `2.302.0`): the interleave-debt ledger drops 4 → 1,
  because the debt was PAID rather than re-labelled.** 2.299.0 shipped the anchor-predecessor gate
  green with four known violations parked in `_INTERLEAVE_DEBT`. That was the correct sequence —
  build the instrument first, so a repair has something to prove itself against — and this is the
  repair.

  **The gate immediately caught its own integrator doing half an operation.** Merging 2.299.0
  produced *no conflict* in `CHANGELOG.md` and silently placed the entry ABOVE 2.300.0. Re-ordering
  and re-chaining that boundary then created **two fresh violations that did not exist before the
  merge**:

  ```
  2.300.0 declares predecessor 2.299.0, but rollback/pre-v2.300.0 was stamped 2.275.0
  2.299.0 declares predecessor 2.298.0, but rollback/pre-v2.299.0 was stamped 2.295.0
  ```

  That is the gate's thesis demonstrated on the gate's own merge: **re-chaining a CHANGELOG boundary
  without re-pointing its anchor is half an operation**, and the omitted half is the one that decides
  where a rollback actually lands. The debt does not sit still — every integration manufactures more
  of it, which is why a documented ledger alone would have rotted.

  Five anchors re-pointed at the release commit of their declared predecessor, each original first
  preserved as `archive/anchor-pre-vX-preintegration`:

  ```
  pre-v2.274.0  9d822fc7 (2.266.0) -> 181d3bbd (2.270.0)
  pre-v2.293.0  181d3bbd (2.270.0) -> c0c3b30d (2.292.0)
  pre-v2.296.0  c0c3b30d (2.292.0) -> bb4026d2 (2.295.0)
  pre-v2.299.0  bb4026d2 (2.295.0) -> 026c83d0 (2.298.0)
  pre-v2.300.0  c55e6644 (2.275.0) -> 0e5fbf1b (2.299.0)
  ```

  Archiving is not ceremony: re-pointing is the only irreversible step in this ritual, and every old
  target was **accurate on the lane it was cut on**. The re-point makes it accurate on the integrated
  line instead; both facts are worth keeping.

  **`2.143.0` is deliberately left in the ledger.** It predates this work and no merge in this series
  created it, so re-pointing it would be speculation about someone else's intent — the same reach for
  another session's anchor the gate exists to make visible.

  **What proved the repair was not the suite going green.** The companion test
  `test_the_interleave_debt_ledger_stays_honest` went RED and named the three paid entries, demanding
  their deletion. That demand is the evidence; deleting them is what hands each boundary to the gate.
  A ledger that silently kept passing after its debt was paid would be an allowlist, which is the
  failure mode the referential formulation was rejected for.

### Added

- **`tableau-migration` (skill `2.300.0` → `2.301.0`): a comment shipped in `2.300.0` asserted
  something measurably FALSE about why a formula table calc stubs, and the correction reframes when
  the Visual-Calculation route is the right one.** The claim was that a formula-authored table calc
  "carries NO addressing/partitioning intent (that lives on the worksheet)". Measured across all 34
  corpus workbooks, over the 46 formula (`kind='field'`) usages: `ordering_type` is populated on
  **46/46** and the rows/cols shelf layout on **46/46** — `has NEITHER: 0/46`. Both sources the
  Tier-1 guidance names are already parsed onto every usage. `translation_router` scopes
  `missing_addressing_intent` to what the bare **`.tds`** cannot carry, not to what is unknowable,
  and the guidance for that category says to recover the addressing from the `.twb` and emit windowed
  model DAX (`RANKX` over the partition, `COUNTROWS`/`RANKX` over `ALLSELECTED`, `OFFSET`).

  The false version is the dangerous kind: it explains the stub, it reads as a principled refusal,
  and nothing else in the tree contradicted it. It is now **pinned by a test** rather than only
  corrected in prose.

  The real reason the axis is a faithful substitute is narrower and still holds: a Visual Calculation
  runs over the visual's result matrix in DISPLAY order, so the axis reproduces that worksheet's
  addressing *structurally* rather than by inference, and stays correct when the user re-sorts — which
  a baked model `ORDERBY` does not.

  That advantage is also the limit, and the limit is now documented at both seams with the two
  measured cases the route **cannot** serve:

  * **a reference line / band.** Corpus 0088 carries **17** `<reference-line>` elements whose bounds
    *are* these calcs (`WINDOW_MAX([Count of Engagements]) * 1.2` is a band's upper bound). A
    reference line is not a projection, so this path correctly declines with *"displayed calc is not
    the shown value"*. Those need model-level windowed DAX.
  * **a model measure that references the calc.** Corpus 0074's
    `Outliers = SUM([Sales]) < [Upper] AND SUM([Sales]) > [Lower]` is a model measure over the two
    band calcs. DAX cannot reference a Visual Calculation, so rebuilding the bands on the view side
    makes them render while leaving `Outliers = BLANK()` unresolvable **by that route**.

  Corrects the record on a related claim too: 0088's stubs are not visually consumed **today** (the
  emitted PBIR mentions those measure names in 0 of 174 JSON files — reference-line rebuild is an
  unbuilt feature), which makes them *latent*, not irrelevant. Prose, comments and one new test only;
  no behaviour change, and the corpus artifacts are unchanged from `2.300.0`.

- **`tableau-migration` (skill `2.299.0` → `2.300.0`): a formula-authored table calc no longer ships
  as an inert `BLANK()` stub — it rebuilds as a Power BI **Visual Calculation**.** Corpus 0074's
  control chart projected `Upper` and `Lower`, both `= BLANK()`: the report validated clean, `pbir_lint`
  was clean, and **both bands rendered EMPTY**. Structurally valid, semantically absent — the family a
  green suite cannot see.

  The model layer's refusal was *correct*. A table calc written inside a calculated field's formula
  carries no addressing/partitioning intent, because that intent lives on the **worksheet**; a model
  measure is one expression, so if several worksheets consume the calc with different partitions there
  is no single right answer. `missing_addressing_intent` is the honest verdict. What was missing is
  that a **Visual Calculation takes its partition from the visual for free**, which is exactly the
  intent Tableau evaluated — so the calc had a faithful home the whole time and never reached it.

  Three seams were closed, and only together do they change an artifact (each was measured alone and
  moved nothing):

  * **The `WINDOW_*` family had no visual-calc form at all.** The compiler's closed subset was
    `RUNNING_SUM` / `RANK` / `RANK_DENSE` / `TOTAL`, so 14 of the corpus's 24 order-independent
    formula table calcs died on `unsupported function WINDOW_MAX` before any other question arose.
    The whole-partition heads now render as `<X>(WINDOW(1, ABS, -1, ABS, axis), base)` — the model
    seam's certified frame (`resources/calc-to-dax.md`) transposed to the view dialect by swapping the
    addressing spec for the axis, exactly as `RUNNINGSUM(m, axis)` already does. The moving form,
    `WINDOW_PERCENTILE` and `WINDOW_CORR` still fail closed, and the `COLLAPSEALL` line is untouched:
    `TOTAL` re-evaluates over underlying rows, `WINDOW_*` aggregates per-mark, and the two diverge for
    a non-additive inner.
  * **Every in-scope calc was recursed into as a nested Visual Calculation, even when it was not a
    table calc.** `WINDOW_MAX([Count of Engagements])` failed entirely because its dependency was
    `COUNTD(IF [Status] = "Closed" THEN [Case ID] END)` — a construct that has no view-layer form and
    never needed one. Tableau evaluates a non-table-calc field in the **query that produces the marks**,
    so it now binds to the model measure the visual already projects. Fails over to the previous
    nested-calc behaviour when no measure resolves, so every chain that compiled before still does.
  * **Single-level formula table calcs never reached the path**, which admitted only calc-references-calc
    chains, and only the **first** usage per worksheet was ever attempted — so a control chart could
    never rebuild both of its bands. Admission is candidacy only; the compiler and the shown-value guard
    still decide. `[Parameters].[X]` now resolves to the what-if parameter's `SELECTEDVALUE` measure,
    added as a hidden projection so the calc can read it and the slider stays live.

  A reused base measure is now hidden **only when it is not a plotted axis series**. The reuse-then-hide
  rule was written for a nested chain whose bases are encoding-only feeders; applied to a control chart
  it hid `SUM([Sales])` — trading two blank bands for a missing line, the same defect class in the
  opposite direction.

  Measured on all 34 corpus workbooks at engine `2.275.0` → this build: `visuals_projecting_stub_measures`
  12 → 10 (0074's `['Lower', 'Upper']` → `[]`), visual calculations emitted 10 → 11. **Exactly one
  `visual.json` in the corpus changed**; 0070, 0076 and 0088 are byte-identical and merely gained honest
  review disclosures where a silent model stub used to be the only record.

- **`tableau-migration` (skill `2.298.0` → `2.299.0`): an anchor left behind by an interleaved merge
  is now caught — "strictly lower" was never enough.** The existing gate (2.266.0) asserts `VERSION`
  at `rollback/pre-vX.Y.Z` is strictly *less* than `X.Y.Z`. Found by the integrator:
  `rollback/pre-v2.293.0` points at a commit stamped **2.270.0** — true on the lane it was cut in,
  where the state before 2.293.0 really was 2.270.0 — but after merge 2.291.0 and 2.292.0 sit
  between. Resetting there lands **two releases short of what the tag claims**, and every gate
  passes: the tag exists, resolves, is reachable, and 2.270.0 is dutifully lower than 2.293.0.

  The new gate asserts **equality**: `VERSION` at the anchor must be exactly the predecessor the
  version's own CHANGELOG entry declares. Measured on the integration branch — 82 chain entries,
  **78 pass, 4 mismatch, 0 missing** — and the four are precisely the interleave boundaries
  (2.143.0, 2.274.0, 2.293.0, 2.296.0), with no false positives. Reproduced independently by the
  integrator from a different starting revision: same four, nothing named in advance.

  **Why this is buildable where its predecessor was not**, which is the part worth keeping. The
  obvious formulation — "every anchor must name a version that exists" — was measured and
  **rejected**: 244 of 319 anchors fail it against the CHANGELOG (which retains ~75 versions by
  design), and 35 still fail against every `VERSION` ever stamped on any ref. Those 35 are *correct*
  behaviour: `rollback/pre-v2.216.0`..`pre-v2.224.0` all point at one commit, all cut inside one
  minute — a whole block claimed under the old block rule and never shipped. A claimed-but-unused
  anchor and a renumbered-away anchor are indistinguishable by construction, so that gate could only
  have shipped with a 35-entry exceptions file that grew with every claim.

  This gate judges **CHANGELOG entries, not anchors**, so an orphan block anchor has no entry and is
  never judged. The exclusion is **by construction rather than by allowlist** — the difference
  between a gate and a gate with a rotting exceptions file.

  **Stated limit, so the coverage is not overclaimed:** a version *renumbered away* has no entry
  either, so it is not judged here. This catches anchors whose meaning went **stale**, not anchors
  that are **missing**. Overstating a gate's reach is the same defect class as a `rebuilt` status
  that only means "the emitter did not raise".

  Fixing a violation is an **integrator** operation — re-point the boundary anchor at the merged
  parent — which is exactly the act a session must never perform on another session's tag, and is
  legitimate for whoever owns the merge, because the merge is what changed the anchor's meaning.
  Whoever re-chains the CHANGELOG must also re-point the anchor; those two halves have always been
  one operation and nobody had noticed. The four known boundaries ship as a documented debt ledger
  so the gate lands green and can only fire on something new; a companion test flags a ledger entry
  whose debt has been *paid*, which is how the count drops for real instead of quietly staying put.

  That companion test was wrong twice first, in opposite directions, and both failures are recorded
  in it: asserting every ledgered version *appears* in the chain fails on every lane branch (other
  lanes' boundaries are not merged yet), and asserting every ledgered version *currently mismatches*
  fails on a lane too (on its own lane 2.293.0 satisfies the invariant — the debt is latent there
  and active only after the merge). It now skips where the question is undecidable rather than
  answering it wrongly.

  Verified red by injection on a real mismatch, with the three sibling anchor gates staying green.

### Fixed

- **`tableau-migration` (skill `2.297.0` → `2.298.0`): the measurement-side gotchas section had a wrong
  count — in the section about wrong counts (docs-only).** 2.296.0 said the uppercase-only triage regex
  corrupted **five** entries. Re-derived over the whole population it is **seven**, and the third
  failure mode is worse than either recorded:

  ```
  VANISHED  'unsupported function size'   -> [A-Z_]+ matches nothing         1
  MANGLED   'unsupported function Total'  -> captures 'T'                    4
            'unsupported function Sales'  -> captures 'S'  ┐ merged into one
            'unsupported function Stage'  -> captures 'S'  ┘ bogus bucket    2
  ```

  `Sales` and `Stage` **collapse into a single category `S` that never existed in the source**,
  carrying a wholly plausible count of `2`. So the tally does not merely lose or mis-file rows — it
  **fabricates a category**, and anyone auditing the breakdown would be checking a name the instrument
  invented. Of the seven corruptions, a match-count assertion catches exactly **one**; the rule as
  first written was necessary and badly insufficient.

  Two rules added, both earned by this correction:

  * **Validate the captures, not just the match count** — assert each capture round-trips to a value
    the source could actually have produced.
  * **A corpus count must cite the engine version it was measured at**, or it is a claim with a hidden
    expiry date. The same query returns 71 needs-review calcs at 2.275.0 and 69 at 2.291.0; the delta
    is two calcs a release *fixed*. The existing corpus figure in this section is now dated.

  And the meta-rule this correction is itself an instance of: **re-derive the SCOPE, not just the
  arithmetic.** 2.296.0 correctly refused a reported “25 vs 30” and re-measured it — but re-measured
  only the two function names the report happened to mention, so it got the arithmetic right and the
  answer wrong. Inheriting someone else's *question* is the same defect as inheriting their answer,
  and far harder to see, because the work you did was correct. The verifier for this release derives
  over the whole population and names nothing in advance.

- **`tableau-migration` (skill `2.296.0` → `2.297.0`): the corrected `blocked_by` figure now names the
  PREDICATE it counts, and a test pins it there.** 2.292.0 corrected “8 of the 11” to “9 of the 11” in
  the prose and the CHANGELOG but left the same wrong figure in `_unmigrated_dependency_index`'s
  summary-counter comment in `assemble_model.py`. Corrected here.

  A bare corrected number would have drifted straight back, because **8 and 9 are both true of
  different questions** over the same 11 calcs: 9 is the count of entries with a non-empty
  `blocked_by` (what the counter actually sums), 8 is the count `_triage_stubs` independently calls
  `cascadable`. They differ because triage re-translates with the *global* resolver while the build
  uses a per-calc island-scoped one. The comment now states which predicate it counts and why the
  neighbouring one differs, so re-deriving it from the wrong source is self-correcting.

  Prose alone was not enough for a figure that had already drifted once, so the distinction is now
  **executable**: `test_blocked_by_does_not_inherit_triage_s_verdict` asserts
  `summary.blocked_by_unmigrated_calc` against a fixture where the two predicates disagree — the calc
  is blocked but *not* cascadable, so a counter wired to triage reports `0` and the test fails.
  Verified red by neutering the counter.

  No behaviour changed; the only non-test edit is a comment.

### Added

- **`tableau-migration` (skill `2.295.0` → `2.296.0`): the measurement-side rule the day's four defects
  all shared, recorded once (docs-only).**
  [`resources/migration-gotchas.md`](skills/tableau-migration/resources/migration-gotchas.md) gains
  *“Its mirror image: a MEASUREMENT that is well-formed and says nothing”*, directly under the existing
  silent-output section — because it is the same defect turned on the instrument. A filter over a
  population emits partial output that is indistinguishable from complete output, since the rows it
  failed to match emit nothing to notice.

  Two rules, the second of which is a **design** constraint rather than reading advice:

  1. A filter must assert its match count against its population — and check the *captures*, not just
     the match count, because a greedy-enough pattern will match and hand back a truncated token
     rather than fail.
  2. **When a summary list and a payload list sit side by side, any field that changes a decision must
     be on BOTH.** You cannot fix this downstream by telling readers to look elsewhere: the reader who
     needs telling is precisely the one who never got there. `translation_handoff` puts `blocked_by` on
     both `needs_review` and `requests` for this reason; `category_guidance` sits on `requests` alone,
     and that asymmetry caused a reported defect. Cross-linked from
     [`second-compiler.md`](skills/tableau-migration/resources/second-compiler.md).

  The worked example is sharper than the one first written for it, because re-deriving it from the
  corpus contradicted the received account. `unsupported (?:function|table calculation) ([A-Z_]+)`
  fails **two different silent ways at once**: `unsupported function size` matches nothing and
  vanishes, while `unsupported function Total` **does** match — `[A-Z_]+` captures just `T` — and is
  tallied under a function named `T`. So four of the five affected stubs were *present but
  miscategorised*, not missing, which is the harder half to notice: the total stays right and only the
  per-name breakdown is wrong.

  Written against the artifact rather than the account: the first draft of this section repeated a
  reported “25 vs 30, `TOTAL` and `SIZE` invisible”, and the re-derivation refused to reproduce it —
  the drop is 1, the miscapture is 4, and the source string is lower-case `size`. Enshrining another
  session's arithmetic in a permanent rules doc would have been the very failure the section describes.

### Fixed

- **`tableau-migration` (skill `2.294.0` → `2.295.0`): a stale measured constant stops overriding
  every authored filter card.** With the cross-axis fix in place the solver correctly produced the
  authored **57px** for a Tableau filter card — and the emitter then floored it back up to **76px**,
  so the dead band under every dropdown survived a fix that had already solved it.

  The 76 was not a guess. It is the arithmetic of Power BI's **default ~12pt** dropdown chrome
  (header 28 + selector 32 + padding 8/8), measured against a real clipping defect: issue #100, 16
  slicers emitted between 45px and 64px, every one clipped. It was correct when it was measured.

  It was measured **before this emitter began stamping the source's 9pt point size** — which was the
  other half of that same fix, and which shrinks the chrome the floor was protecting. So a constant
  with genuine provenance quietly outlived its premise and started doing the opposite of its job:
  instead of rescuing a degenerate card, it grew every faithfully-sized one by a third.

  **A stale measured constant is more dangerous than a guessed one**, because its provenance is the
  reason nobody re-checks it. Render-verified at 9pt: a 57px card shows its full label *and* its
  selector, no clipping. The floor stays — a degenerate tiny card still has to render its control —
  but is now set to the smallest height shown to work at the font actually emitted.

  `layout_solve.MIN_SLICER` moves with it: an existing test asserts the solve reserves exactly what
  emit will use, and moving one without the other would leave the solve under-reserving and
  overrunning whatever it seated below.

  The floor test now asserts the **constant** rather than a literal, and separately asserts the floor
  actually bound. That literal had gone stale twice already — 64 → 76 → 57 — and each time turned a
  deliberate re-measurement into a failure that said nothing about behaviour.

### Added

- **`tableau-migration` (skill `2.293.0` → `2.294.0`): an object is rebuilt at the size its author
  drew it, not stretched to fill the container it sits in.** Reported as "we STILL cannot figure out
  how to properly size things like filters and parameters", with a screenshot of a Tableau filter
  card next to our rebuild of it — ours a third taller, with a dead band under every dropdown. The
  reporter's own diagnosis was exactly right: *"a massive part of the issue is us not being able to
  distinguish objects from the containers they are in."*

  Tableau states both numbers outright. On the Salesforce NPSP "Staff Capacity" dashboard
  (1366×768, `sizing-mode='fixed'`) a filter card is authored `h=7422` → **57px**, inside a
  `layout-flow` authored `h=12890` → **99px**. We emitted **99.12px**: the container's height, not
  the control's. The leftover 42px is Tableau's card padding, and Tableau does not give it to the
  control.

  The cause is one line of flexbox semantics. `layout_solve.allocate` distributes a flow container's
  box among its children **by fraction on the main axis** — and then hands every child the
  container's **entire cross-axis extent**, which is what a CSS flex container does and what a
  Tableau layout does not. So the defect was never specific to slicers: the KPI row's worksheets were
  inflated from their authored 235px to the container's 257px by the same line.

  Now the cross axis maps the child's own source fraction into the allocated box — the flow-axis twin
  of what `_scale_abs` already did for a `frame`. The main axis is untouched, so every fixed/min/
  squeeze rule above it is unchanged; this only ever affects the axis that was previously ignoring
  the source entirely. Fail-safe: a node whose `src` extent is unusable falls back to the old
  stretch, the size is clamped up to the child's own minimum, and the offset is clamped so
  `offset + size <= extent` — so cross-axis containment becomes unconditional, matching the promise
  the module already makes on the main axis.

  Measured on that dashboard, authored-vs-emitted across every object that pairs unambiguously:

  | | before | after |
  |---|---|---|
  | objects within 10px of authored **size** | **0 of 16** | 3 of 16 |
  | median object displacement | 44px | **22px** |
  | objects within 10px of authored **position** | 4 of 16 | **7 of 16** |
  | emitted visuals overlapping each other | 0 | 0 |

  The `0 of 16` is the number worth keeping: not one object on that page matched the size its author
  drew, and the report validated clean throughout. Render-verified on a fresh build.

- **`tableau-migration` (skill `2.292.0` → `2.293.0`): a Tableau donut rebuilds as a donut instead of
  a card reading `(Blank)`.** Reported on the Salesforce NPSP "Staff Capacity" dashboard, where the
  *Program Engagement Stage* ring — 320 engagements across seven stages — arrived as a
  `multiRowCard` showing `(Blank) / 1 (filtered) / 320`.

  A Tableau donut is several stacked Pie panes: one draws the ring, the others draw the number in
  the hole. The emitter already knew to drive the worksheet off a Pie pane rather than the primary
  one — its own comment says so verbatim, "so its legend (colour) + angle (wedge-size) encodings are
  read". But it selected the **first** pane carrying a Pie mark, and when *every* pane is a Pie that
  is a coin toss. It lost. This worksheet's three panes carry `text` / `color=[Stage]` +
  `wedge-size` / `text`, and pane 1 won.

  So the ring's colour dimension never reached `encodings`; `latent_color` stayed False; the
  pie/donut router in `_card_collapse_alternatives` could not fire; and the worksheet fell through to
  `card`. Every gate passed — the JSON is well-formed, the visual type is real, the fields resolve.
  It is wrong only against the source.

  The fix selects the pane carrying the encodings that *define* a ring (`color`, `wedge-size` or
  `angle`), falling back to the first Pie pane when none stands out, so a workbook without such a
  pane behaves exactly as before. Render-verified on a fresh build: the donut draws its stage
  segments with labels (85 / 26.56%, 142 / 44.38%, 25, 17, 7), matching the Tableau reference.

  The hole is still empty — the `320` is `donutChart.centerValue`, which Power BI defaults off and
  the engine does not yet emit. Separate increment, deliberately not bundled here.

  (Version jumps `2.270.0` → `2.293.0`: this branch was at 2.270.0 while the integration branch had
  reached 2.291.0, so the block was claimed above that tip at the release step.)

- **`tableau-migration` (skill `2.291.0` → `2.292.0`): the handover prose now tells a reader how to
  read the stub manifest without being misled by it (docs-only).** 2.291.0 added `blocked_by` so a
  cascade names the calc that actually failed, but left `fallback_reason` untouched — correctly, since
  the string is the literal translator error and is pinned by the row-level reroute router's contract.
  The consequence was that a reader who reads only `fallback_reason` is *still* misdirected; they now
  have to know to read `blocked_by` too, and nothing told them so.

  Measured while writing it, a second and worse instance of the same adjacency defect: `category_guidance`,
  `fields`, `formula` and `target_table` ship on `translation_handoff.requests` **only**, never on the
  concise `needs_review` list. A reader inspecting `needs_review` for routing advice finds none and
  concludes the engine shipped none — it ships 547 characters of it, on a sibling list. (`guidance` is
  empty on **0 of 69** requests corpus-wide, not on 30 as reported.)

  [`resources/second-compiler.md`](skills/tableau-migration/resources/second-compiler.md) gains a
  *“Two ways this manifest will mislead you”* block, an accurate shipped-shape sample (`blocked_by`,
  `triage`, `partial_fidelity`, `summary.blocked_by_unmigrated_calc`), a **Triage** section recording
  that triage re-translates with the *global* resolver while the build uses a per-calc island-scoped
  one — so on a multi-datasource workbook it can call a cascade irreducible — and a **Partial fidelity**
  section for live-but-incomplete objects that are absent from `needs_review` by design. The estate-CLI
  loop gains an explicit **“author roots first”** step, and its verification line now reads *at least*
  the count landed rather than *exactly*, because clearing a root cascades its dependents.
  [`resources/migration-report.md`](skills/tableau-migration/resources/migration-report.md) and
  [`SKILL.md`](skills/tableau-migration/SKILL.md) get the short form at their own reader surfaces.

  No code changed. `translation_router.py` was inspected and needed no fix.


- **`tableau-migration`: the 2.291.0 entry below said “8 of the 11”; the correct figure is “9 of the
  11”.** 8 is the count of entries `triage` independently called cascadable; 9 is the count carrying a
  non-empty `blocked_by`. Both predicates are real and they differ by one, and the sentence is about
  `blocked_by`. Caught by a check that re-derives every numeric claim in the prose from the corpus
  export rather than from the previous sentence. The same wrong figure remains in a code comment in
  `assemble_model.py` (`_unmigrated_dependency_index`'s summary counter) and is being handed to the
  session that owns that file rather than edited across an active lane boundary.

### Corrected

- **`tableau-migration` (skill `2.290.0` → `2.291.0`): a stub now names the calc that ACTUALLY failed,
  instead of inheriting its dependency's error (#173 family).** A calc that falls back only because a
  calc it REFERENCES is unmigrated reported the dependency's translator error as its own
  `fallback_reason`. The classic shape is a dispatcher or comparison measure pointing at an unmigrated
  LOD: it reports `bare row-level field [..] not valid in a measure` while containing no row-level
  field at all. A reader triaging the flat `needs_review` list was sent hunting inside the wrong
  measure, and the broken one was never named — silent because the entry looks complete.

  Measured on the 34-workbook corpus at 2.275.0: **13** needs-review entries carried that reason,
  **9 of them were cascades by the engine's own triage**, and **none of the 13 named a dependency**,
  because no such key existed in the export. So this was a reporting gap, not an engine one — the
  engine computed the split and shipped it in the same `report.json`, in a sibling `triage` block
  keyed only by name.

  Every `needs_review` and `requests` entry now carries an additive **`blocked_by`**:
  `[{caption, name, role}]` naming the referenced calcs that are themselves needs-review, so the
  dependency chain is walkable to its root (corpus 0088:
  `Avg Days Participation same as Goal ●` → `Avg. Days Participation vs Goal` →
  `Avg. Days Participation`, the nested LOD). `summary.blocked_by_unmigrated_calc` counts them:
  **13 of 69** at 2.291.0, and **9 of the 11** entries carrying `bare row-level field [..]` — leaving
  only 2 roots in that class, so ranking it by raw `fallback_reason` count measures leaves, not work.
  *(Corrected in 2.292.0: this originally read “8 of the 11”, which was the count of entries `triage`
  independently called cascadable, not the count carrying a non-empty `blocked_by`. Both predicates
  are real and they differ by one; the sentence is about `blocked_by`, so 9 is the right number.)*

  `blocked_by` asserts only the FACT *“these referenced calcs are also unmigrated”*, deliberately not
  the prediction *“this would translate once they are fixed”*. That prediction is `triage`'s job, and
  triage is measurably fallible: it re-translates with the single global resolver while the build
  uses a per-calc island-scoped one, so on a multi-datasource workbook it can call a cascade
  irreducible (0088's `Select Metric` at 2.275.0). Deriving `blocked_by` from triage would inherit
  that; it is derived from the already-computed report rows instead, so it cannot. The two signals
  genuinely disagree in one corpus case (0073's `Difference` references an unmigrated calc AND has
  its own irreducible problem) — both are true, which is the argument for reporting the fact rather
  than a boolean.

  `fallback_reason` is deliberately unchanged: the string is the literal translator error and is
  pinned by `_ROW_LEVEL_IN_MEASURE_REASON`, the row-level reroute router's contract.

- **`tableau-migration` (skill `2.275.0` → `2.290.0`): one unmigrated branch no longer blanks EVERY
  selection of a parameter dispatcher (#168, #171).** A Tableau control-surface calc —
  `CASE [Parameters].[P] WHEN 1 THEN <metric a> WHEN 2 THEN <metric b> … END` — was translated
  all-or-nothing: a single branch the translator could not render stubbed the **whole** measure to
  `BLANK()`, so every selection rendered empty, not just the one. Structurally valid, semantically
  absent — the measure exists, the visual binds, `pbir_lint` is clean, validation returns zero
  errors, and the chart is blank.

  Measured on corpus workbook 0088 (`salesforce_nonprofit_case_mgmt`) at 2.275.0: `Sort By` had
  **3 of its 4** branches already translated and `Select Metric` **3 of 4**, yet both emitted
  `BLANK()`. Between them they were **5 of the 12** visuals corpus-wide that projected a stub — the
  single largest cause of empty charts.

  The dispatcher is now rebuilt from the branches that DO translate. A failing branch that names a
  sibling calc this model emits keeps its slot, pointing at that sibling's own measure: blank today
  (the sibling is its own honest `BLANK()` stub) and correct for free the moment that sibling's
  translation lands, with no further change here. Any other failing branch is pruned, so that one
  selection returns blank while the rest work.

  Fail-closed in the way that matters: a dispatcher with **no** genuinely translated branch stays a
  stub rather than becoming a live measure that says nothing for every input — which would be the
  same defect wearing a green badge (0088's `Select Metric Decimal`, whose only branch is blank,
  correctly stays a stub). No DAX is composed by the new code; it prunes and re-points branches and
  hands the result to the same parser, type check and emit guardrail as any other measure, so a
  mistyped sibling is rejected and pruned rather than emitted.

  Because a repaired dispatcher counts as translated and therefore LEAVES the needs-review list, its
  still-blank selections are disclosed in two places or the repair would reproduce the silence it
  fixes: the model file's `TranslatedBy` annotation names them, and an additive `partial_fidelity`
  list (plus a `summary.partial_fidelity` count) in `model_translation_handoff` carries the
  structured form. Both are empty on any build with no such calc.

  Corpus effect, whole estate, 2.275.0 → 2.290.0: `visuals_projecting_stub_measures` **12 → 7**,
  workbook calcs translated **216 → 218**, stubbed **71 → 69**; exactly one workbook changed and no
  workbook lost a translated calc, gained a stub-projecting visual, or changed viz/pbip/openability
  status.

- **`tableau-migration` (skill `2.274.0` → `2.275.0`): a per-visual status now says what was actually
  CHECKED, so a false green is visible (#173).** `status: "rebuilt"` asserts only that the visual
  emitter completed without raising and without attaching a warning. It is a claim about **our code**,
  not about the emitted artifact — so a visual can be reported `rebuilt` and still fail to render.
  Reported from the field as a scatter marked `rebuilt` while Desktop showed a live
  `DataViewMappingError_ScatterGroupingValues`.

  This is the rule already recorded in `migration-gotchas.md` — *read every confirmation at the
  artifact, never at the mechanism* — applied to **our own published verdict**, which is worse than
  the earlier instances because downstream consumers reasonably read a per-visual status as *"this
  visual is fine"* and gate on it.

  **The honest answer was already computed and discarded.** `lint_pbir_parts` runs on the SHIPPED
  report bytes a few lines *below* where `viz_fidelity` is built, and its findings carry the part
  path, so they are attributable. `rebuilt` could not see them purely because of **ordering**. Each
  fidelity row now carries `evidence`:

  * `"emitted"` — the emitter ran cleanly. Everything `rebuilt` used to mean.
  * `"emitted+linted"` — additionally, the shipped bytes were structurally linted and no finding
    names this worksheet's visual.
  * `"lint_failed"` — a finding **does** name it. The false-green case, now visible while `status`
    still reads `rebuilt`.

  **Additive, and `status` is deliberately NOT narrowed.** Narrowing it would be more honest still,
  but it is a breaking change to a field other teams consume — so it was offered on the issue as
  their choice rather than imposed. Same spirit as `tier`.

  **The fail-closed refusal is the load-bearing part**: when the lint did not run, every row stays
  `"emitted"`. Claiming `"emitted+linted"` because nothing was found, when the reason nothing was
  found is that nothing looked, would recreate this exact defect one level up. An unattributable
  finding also flags nobody — a wrong attribution sends someone to the wrong sheet, which is worse
  than none.

  **A closed-key-set test caught the change and that is it working.** `viz_fidelity`'s row shape is
  asserted as an exact set, precisely so a shape change is a deliberate act with a CHANGELOG entry
  rather than something that leaks in. It falsified the loose phrasing of "additive, nothing breaks":
  adding a key *is* a contract change. Updated in the same way `tier` was added before it, with the
  allowed values and the non-contradiction invariant pinned alongside.

  Suite 5008 → **5014**.

- **`tableau-migration` (skill `2.270.0` → `2.274.0`): a published datasource reached only as a
  SECONDARY no longer ships silently as a `localhost` phantom (#174).** A published Tableau
  datasource connects through a federated proxy whose connection class is `sqlproxy` and whose server
  is the literal `localhost` — an internal Tableau address, not an endpoint anything can reach. When
  the **primary** datasource is that proxy the estate path already gates honestly (`pbip_status:
  skipped`, needs-storage-decision). When a **secondary** is, nothing did: the workbook built, looked
  complete, and carried an empty disconnected table pointed at `localhost`, while three sibling
  workbooks in the same estate gated correctly.

  **Verified independently before acting**, since the report was code-level: `secondary_datasources`
  appears **exactly once** in the whole engine — written at its construction site, read nowhere — and
  `storage_mode.py` contains **zero** references to `secondary`. Both of the reporter's measurements
  were exact.

  The write-only field turns out to be **deliberate**: `_workbook_binding_signal` documents itself as
  *"records a SIGNAL; changes no routing today"*, pending a cross-skill catalog contract. So the
  inert signal is not the bug. The bug is that the workbook then **ships a phantom with no
  disclosure** — and the engine's own handover named the dependency, so it knew and said nothing
  actionable.

  Two changes, deliberately separate:

  * `binding_signal` now records **`secondary_published_datasources`** — which secondaries are
    themselves published. `secondary_datasources` carried bare labels, so nothing downstream (or
    anyone reading the handover) could tell a published dependency from an ordinary one. Recorded as
    its own key rather than by widening `is_published`: widening it would relabel the workbook's
    `kind` as published, which is **false** — its primary is embedded — and every consumer that reads
    `kind` to choose rebind-vs-rebuild would be told the wrong thing to fix a reporting gap.
  * a new `phantom_published_proxy_tables` finding plus a warning, read from the **emitted**
    connection parameters rather than re-derived from the workbook, naming the datasource and saying
    the table opens empty.

  **The silence is the defect, not the stub.** The model opens, validates and binds; the table is
  simply always empty — the same family as a `= BLANK()` measure (2.227.0) and a dangling
  `SelectRef`: structurally valid, semantically absent. Which is also why this is *third* instance of
  the pattern in one file: `_workbook_blend_links` carries a comment recording issue #101, where a
  declared block nothing read left a blended secondary related to nothing at all.

  **Refusals are the feature.** `localhost` alone is an ordinary local SQL Server or Postgres, so
  keying on the host would fire on valid models constantly; only the combination with the `sqlproxy`
  parameter suffix means a published proxy. A `_sqlproxy` parameter pointing at a **real** host is a
  successful rebind and is not reported. Both pinned by test.

  **Clause 3 clean, both trees patched from the start:** disabling the detector gives
  `3 failed, 5005 passed` — every failure one of the new tests, so nothing else in the suite notices
  a phantom shipping. That count *is* the measurement of the silence.

  Suite 5001 → **5008**.

### Added

- **`tableau-migration` (skill `2.269.0` → `2.270.0`): a capability nobody can find no longer counts
  as shipped.** The skill carries 50+ scripts, and an agent only ever learns one exists by reading
  `SKILL.md` or a `resources/*.md` runbook — it never reads a directory listing. So a **runnable**
  script that no prose mentions is invisible: present, tested, paid for, and never used.

  Not hypothetical. `scripts/pbip_desktop_reload.py` (2.265.0) turns a ~115 s
  edit → restart → verify cycle into ~1 s, and the very next question asked about it was *"how do I
  make another session aware of that?"* It **was** in SKILL.md's resource table — and still absent
  from `fidelity-oracle.md`, which is the page an agent actually lands on when it sits down to do
  render verification. Being listed is not the same as being findable at the moment of need.

  `fidelity-oracle.md`'s render-verify section now leads with the fast reload, why the packaged CLI
  appears not to work (`reloadModelDefinition` hard-coded false), and — equally important — what
  reload does **not** do: no data refresh, no `cache.abf`, and **no substitute for a cold open**,
  which is the only thing that proves a file opens at all. It also records, as one measurement on
  one model rather than a new rule, that a definition reload preserved loaded rows
  (`COUNTROWS` 9,994 before and after), so the refresh-every-iteration rule can be relaxed *with
  evidence* while a blank frame after a reload still means "not ready", never "the answer".

  `tests/test_capability_discoverability.py` makes it an invariant: every script with a `__main__`
  and an argument surface must be named in the prose. Internal modules are deliberately exempt —
  their callers are their documentation, and judging all 52 scripts alike would demand runbooks for
  14 importables and turn the gate into noise. Four pre-existing undocumented scripts
  (`geometry_audit`, `polish_layout`, `tmdl_lint`, `workbook_calc_usage`) sit in an explicit debt
  ledger so the rule lands green and can only fire on something new; a second test stops that ledger
  going stale or quietly buying back an exemption. Verified red under three injections — a new
  undocumented script, the render runbook losing its pointer, and the reload runbook dropping a
  stated limit — each caught by the test named for it.

### Fixed

- **`tableau-migration` (skill `2.268.0` → `2.269.0`): a model that reads from the ORIGINAL AUTHOR'S
  LAPTOP is no longer reported as `built`.** Found by opening all 34 corpus workbooks in Power BI
  Desktop and looking at all 98 rendered pages — 13 workbooks had visibly broken pages that every
  static gate had passed. The largest cluster was four workbooks rendering every page as bare
  headers under *"Some of the tables have incomplete or no data"*, and interrogating the live model
  gave the cause in one line:

  ```
  Could not find a part of the path
  'C:\Users\bshonk\AppData\Local\Temp\TableauTemp\...\Clipboard_20121219T112939.xls'
  ```

  A `.twbx` records the upstream file its author originally loaded from. When the bundled payload
  cannot be read — here a legacy `.tde` extract, which the engine has no reader for — the emitter
  falls back to that recorded path. `0071_numerical_dates` shipped pointing at a stranger's **temp
  folder from 2012**; `0084_rounding_minutes_to_quarters` at their **desktop**. Both models open,
  "refresh" to zero rows, render every visual empty — and the pipeline called both plain `built`,
  with no warning of any kind.

  Nothing static could have seen it: the M is syntactically perfect and the TMDL is valid. The path
  is wrong only relative to a filesystem, which no schema knows about. The contrast that makes the
  gap precise: `0083_previous_workday` reaches the SAME blank pages by a different route (an
  unfinished `TODO` partition stub) and **is** honestly reported. The defect was never the dead
  path — it was the silence.

  `openability_gate` grows `local_source_paths`, which fails definition-of-done when a partition
  reads a path under another user's profile. Whether a path is FOREIGN is decidable from the text
  alone, so the gate stays hermetic and the verdict travels with the model; whether it EXISTS needs
  a filesystem and is left to the caller. Conservative in both directions — a plain data folder is
  never judged, `Public`/`Default` are not accounts, and the current user's own profile is fine
  (case-insensitively). Also fixes the diagnosis wording: a dead path makes a model **"open but load
  NO DATA"**, not "not openable", and telling a reader the wrong one sends them hunting a corruption
  that isn't there.

  Corpus: flags exactly the 2 affected workbooks out of 34, 0 false positives. A/B over all 1612
  emitted files — 3 changed, all three the summary/manifest carrying the new diagnostic.

  **Instrument note (this one invalidates an earlier claim's evidence, not its conclusion).** The
  corpus differ used `os.walk` on a bare Windows path, which SILENTLY STOPS DESCENDING past
  MAX_PATH — no error, the tree just looks smaller. Two byte-identical builds reported 1470 vs 1579
  files purely because their output roots differed by ONE CHARACTER. So the "all 1470 files" quoted
  for 2.267.0 and 2.268.0 was really **1470 of 1612 (91%)**; re-run with a long-path-safe walk, both
  conclusions are unchanged. This is the second form of this hole in the same tool — the first was
  *reading* a long path, fixed one release earlier, and fixing the read did nothing for the walk.

- **`tableau-migration` (skill `2.267.0` → `2.268.0`): a rebuilt Salesforce model no longer refuses
  to OPEN with "ambiguous paths".** Reported from the field as a Power BI Desktop frown: *"There's a
  problem with the definition content in your Power BI Project. There are ambiguous paths between
  'pmdm__ServiceDelivery__c' and 'Date (Service Delivery)'."* Power BI does not render this badly —
  it declines the entire project, which takes the semantic model sitting beside the report out of
  reach too. This ranks with invalid TMDL, not with fidelity.

  **A regression from the per-datasource calendar work (2.260.0 / 2.261.0), and measured as one**:
  the pre-change engine emitted 28 active / 11 inactive relationships and **0** ambiguous pairs; the
  post-change engine emitted 33 active / 6 inactive and **4**. Giving every fact its own active date
  edge is safe until two facts are joined to *each other* as well as to the same calendar — then
  `calendar→SD` and `calendar→PE→SD` are two filter paths. Neither edge is wrong on its own; the
  defect exists only in the *relation between* them, which is precisely the shape no per-object
  validator can see. `powerbi-report-author validate`, `pbir_lint` and definition-of-done all
  passed on a file that would not open.

  Two independent layers now close it. The **emitter** (`_activate_without_ambiguity`) activates a
  fact's date edge only when doing so introduces no second path, walking candidates in a stable
  order so the active graph stays a forest by construction; a fact that loses its direct edge is
  still date-filtered through its neighbour and gets a warning naming `USERELATIONSHIP`. The
  **openability gate** grows `unambiguous_relationship_paths`, so an ambiguity reaching the output
  by any other route fails the build instead of shipping.

  Filter DIRECTION is the whole check, not a detail: a relationship runs many→one while a filter
  propagates one→many. Modelled undirected, the same model reports **90** ambiguous pairs instead of
  4 — and an ordinary star (one calendar, two facts) looks broken, so the gate would fail every
  healthy build. A test pins that negative.

  Calibrated against Desktop in both directions: the gate fires on the exact build Desktop refused
  and passes the rebuilt one, which Desktop then **opened**. Corpus A/B over all 1470 emitted files:
  **4 changed — 3 timestamps and only `0088`'s `relationships.tmdl`**, 0 added, 0 removed. Across all
  23 corpus models: 1 unopenable before, **0 after**.

- **`tableau-migration` (skill `2.266.0` → `2.267.0`): a Tableau donut's white "hole" colour no
  longer paints the whole report's data invisible.** `_harvest_workbook_palette` promotes every mark
  colour a workbook uses to the FRONT of the report theme's `dataColors`, so single-series visuals
  rebuild in the author's colours instead of Power BI blue. But a Tableau workbook legitimately
  contains mark colours whose entire purpose is to be **invisible** — the classic donut is a pie
  with a white circle punched through its middle, and spacer/halo marks are painted in the canvas
  colour on purpose. Harvested as ordinary mark colours, one of those can land at `dataColors[0]`
  and become the default series colour for every visual in the report.

  Measured on `0090_small_multiples`, whose donut hack paints `#ffffff`: that white reached
  position 0 and silently erased **five bar charts entirely** (every mark, white on white), **the
  donut's own fourth slice** (503.17K / 21.63%, leaving a ring that merely looked broken), and **one
  of the two series in all four time-series panels**. The report validated clean, `pbir_lint` passed,
  and definition-of-done reported success throughout — the defect is invisible to every gate we have,
  because a valid hex in a valid theme is exactly what all of them check for. It was found by opening
  the build and looking at it.

  Lead colours (brand + harvested) are now dropped when they are indistinguishable from the page
  background, by WCAG contrast ratio below 1.2. **The rule is about contrast, not about white**: on
  a dark dashboard a white mark is the author's most visible colour and is kept, while a near-black
  one is dropped — a plain "strip `#ffffff`" fix would have been the same defect inverted, and a test
  asserts exactly that. The curated Tableau 10/20 tail is deliberately NOT filtered, so a workbook
  with no brand and no harvested colours stays byte-identical (the never-regress contract).

  Corpus A/B across all 34 workbooks, every one of the 1470 emitted files compared: **7 changed —
  the 4 intended theme files, and 3 that differ only by their generation timestamp.** 0 files added
  or removed. `0090` dropped `#ffffff` (21→20 colours, position 0 now Tableau blue); `0088` dropped
  `#f0f0f0` and `#ffffff`, both invisible on its `#f5f5f5` canvas, with its brand colour untouched at
  position 0. Nothing added, relative order preserved. Re-verified by cold-opening the rebuilt `.pbip`
  and looking: all five bar charts populated, the donut whole, both time-series measures visible.

### Added

- **`tableau-migration` (skill `2.265.0` → `2.266.0`): a rollback anchor that points at the wrong
  commit is now caught, not just one that is missing or unreachable.** The two existing anchor gates
  ask whether an anchor EXISTS and whether git can RESOLVE it. Neither asks whether it points
  anywhere *useful* — and that is the gap where the damage lands. When two parallel sessions collide
  on a version they also collide on its anchor NAME, because `refs/tags` is one namespace shared by
  every worktree; the loser's release step re-points the tag at its own commit. The anchor is then
  still present and still reachable, both gates pass, and `git reset --hard rollback/pre-vX.Y.Z`
  quietly lands you on work that already contains X.Y.Z. Rollback stops meaning rollback with no
  signal at all. Observed live twice, most recently when a batch `git tag -d` of three
  "obviously mine" anchors removed two whose *versions* belonged to another session.

  The new gate asserts the thing that actually defines a rollback: **`VERSION` at the anchor must be
  strictly less than the version the anchor names.** The tempting stricter rule — anchor ==
  parent of the bump commit — was measured and rejected: 4 of 268 anchors violate it *legitimately*
  because the parent is a PR merge commit, two with a byte-identical tree. Rollback is not a claim
  about commit identity but about the state you land in, and the VERSION stamp is exactly that
  state, so comparing stamps tolerates merges, rebases and renumbers while still catching every
  re-point. Measured across all history: **306 anchors, 306 satisfy it, 0 violations** — an exact
  property of the repo, not a rule retrofitted onto it. Two git calls total; labelled anchors
  belonging to other skills are skipped rather than mis-compared.

  Verified red under injection, with the three sibling anchor gates staying green — the standard
  that a gate is unproven until it fails on something its neighbours miss. Also records the
  non-destructive way to probe it: **add** a bogus low-numbered anchor, never re-point a real one.
  The first probe re-pointed a live anchor and was recoverable only because that tag happened to be
  on `origin`.

### Fixed

- **`tableau-migration` (skill `2.264.0` → `2.265.0`): an edited PBIP now reaches a running Power BI
  Desktop in about a second, instead of costing a ~115 s restart.** The repo's recorded wisdom was
  that `powerbi-desktop reload` "does NOT re-read edited TMDL" — it returns `{"success": true}` and
  the old measure expressions stay live — so only closing and reopening picks up a model change.
  That observation was accurate; the conclusion drawn from it, that Desktop cannot do this, was not.
  The Bridge's `file.reload/v1` takes a `reloadModelDefinition` parameter Microsoft documents as
  defaulting to **true**, and the packaged CLI
  (`@microsoft/powerbi-desktop-bridge-cli` 0.1.2, `dist/index.js` line 561) hard-codes it **false**.
  One flag in a wrapper hid the capability for a release. `scripts/pbip_desktop_reload.py` sends
  `true` over the same named pipe, stdlib only and offline.

  Measured 2026-08-24 on a real migrated model: the **same** measure edit, reloaded two ways, read at
  the artifact (`INFO.MEASURES()`) rather than at the return value — this script landed it, the stock
  CLI left the old expression live, and **both printed `success: true`**. Neither run could be told
  apart by its own output, which is precisely why the defect went unnoticed. Loaded data survives
  (`COUNTROWS` read 9,994 before and after), so no re-refresh is needed to keep querying. Elapsed
  3.9 s with a definition change, 0.6 s without.

  It **does not** replace the cold open, which is the only thing that proves a file opens from
  nothing — the class that produced the `pageOrder: []` crash. It speeds up the iterations between
  cold opens. It also refreshes no data and persists no cache; `pbip-model-refresh` still owns both.
  Refuses to guess between multiple Desktop instances, and `--require-saved` refuses to overwrite a
  human's unsaved edits. Runbook: `resources/desktop-bridge-reload.md`; the now-corrected
  close-and-reopen advice is in `resources/migration-gotchas.md`.

- **`tableau-migration` (skill `2.262.0` → `2.264.0`): a repeatable way to ask Power BI Desktop
  which JSON a formatting feature actually writes, instead of guessing the property name.**
  PBIR `visual.objects` resolves to `DataViewObjectDefinitions`, which permits **arbitrary**
  property names — so `powerbi-report-author validate` can never catch a wrong formatting property,
  and a misspelled or invented name validates clean and renders nothing. Every published schema also
  lags Desktop by at least a month, so a new feature is unnameable from the catalog on the day it
  ships. `scripts/pbir_property_probe.py` closes that gap by treating Desktop as the oracle:
  `snapshot` a report, toggle the feature in the UI, `snapshot` again, and `diff` names the exact
  JSON path that changed. `schema` cross-checks a candidate name against the release-tagged theme
  schema, which is the richer of the two oracles — it knew `outerPadding`, `accentBar` and the real
  `centerValue` shape that the npm catalog behind `validate` did not. Stdlib only, offline.
  Runbook: `resources/pbir-property-discovery.md`. Verified end-to-end by recovering
  `centerValue.show` from an injected edit.

- **`tableau-migration` (skill `2.261.0` → `2.262.0`): a workbook federating TWO bundled files now
  loads data instead of none.** Power BI refuses a relative `File.Contents` path outright — *"The
  supplied file path must be a valid absolute path"* — so a model emitted from Tableau's in-archive
  path opens and loads **nothing**. `materialize_bundled_flatfile_data` exists precisely to prevent
  that, by lifting the bundled file to an absolute location.

  It lifted **one**: the datasource-level `flatfile_filename`. A **federated** datasource joins tables
  from several connections, each carrying its own bundled file, so every table belonging to any other
  connection kept its relative path — and one unresolvable partition is enough to fail the refresh.

  **Why the corpus never caught it.** `_effective_connection` returns the **descriptor** when a
  datasource has a single named connection, and the descriptor is exactly where that one absolute
  path was written. All 34 corpus workbooks are single-connection, so the whole corpus passed while
  the federated shape was broken. The corpus diff for this fix is **byte-identical apart from
  timestamps** — real evidence of no regression, and equally that the corpus gives this path **zero**
  coverage.

  Measured on a user-supplied `Date Joins.twbx` — two `excel-direct` connections
  (`Sample - Superstore.xlsx` joined to `Book1.xlsx` **on a date column**):

  | | before | after |
  |---|---|---|
  | bundled files landed | 1 of 2 | **2 of 2** |
  | partitions with an absolute path | **0 of 5** | **5 of 5** |
  | refresh | `DataFormat.Error` | **`DATA_OK + PERSISTED`**, 10,194 rows |

  And the join works end to end. Filtering `Orders` directly to 30 Dec 2026 gives **8** rows;
  filtering `Sheet11` and propagating **through the date join** gives **8** — against 10,194
  unfiltered, so a broken relationship would look visibly different rather than merely absent.

  Per-connection paths are keyed by FILENAME and stamped onto each `relation["connection"]`, because
  that inline dict is what `_effective_connection` hands the emitter on a multi-connection source; a
  stamp confined to `descriptor["connections"]` is never read on the one shape this exists for. Two
  bundled files sharing a basename land on distinct paths — overwriting would give one table the
  other's data, a model that loads, refreshes, and is wrong.

  **The tests initially did not catch this.** They called the helpers directly, so disabling the call
  site left the suite green at 5013 passed with the bug injected — proving a helper works is not
  proving anything reads it. A test against the public `materialize_bundled_flatfile_data` was added;
  the injection then fails exactly 2 tests, both new.

  Note the Tableau join was `full` outer. A Power BI relationship is filter propagation rather than a
  join, so unmatched rows survive on both sides — closer to Tableau's full outer than an inner join
  would be, but not identical, and not yet disclosed.

### Added

- **`tableau-migration` (skill `2.260.0` → `2.261.0`): the calendar's ACTIVE date is chosen from what
  the workbook CHARTS, not from what its columns are called.** Power BI allows one active
  relationship between a fact and a calendar, so the model build must pick each fact's business date.
  It picked by naming convention — literally `Date`, an `order`-date, or a `created`-date — and
  refused when nothing matched, emitting **every** date relationship inactive.

  Refusing is not neutral. A fact with no active date cannot be filtered by the calendar at all, so
  every date-axis visual over it returns the grand total in every bucket and renders as a flat line.
  And real schemas do not follow the convention: Salesforce writes `pmdm__StartDate__c`,
  `pmdm__EndDate__c`, `SystemModstamp`, `caseman__AssessmentCompletedDate__c`. Nothing matched, so
  those facts lost their time axis entirely — `Date (Client Enrollment and participation)` had **four
  date joins and zero active**.

  The workbook already answers the question the convention was approximating: **the author put the
  business date on a shelf.** `twb_to_pbir.date_field_usage` counts, per date column, how many
  rows/cols/filters shelves place it; `_select_primary_date` prefers the clear winner and falls back
  to the conventions when usage cannot decide. It travels the same report → model channel that
  `scatter_keys` and `colour_palettes` already use.

  **Measured on Salesforce NPSP** — of the 10 facts the calendars relate, 5 already had an active
  date and usage resolves the other **5**, each with exactly one of its date columns used anywhere
  (`pmdm__StartDate__c` ×2 while `pmdm__EndDate__c` is charted nowhere), leaving **0** unresolved:

  | calendar | before | after |
  |---|---|---|
  | Assessments | 1 active / 4 inactive | **3 / 2** |
  | Client Enrollment and participation | **0** / 4 | **2 / 2** |
  | Intake | 2 / 1 | 2 / 1 |
  | Service Delivery | 2 / 2 | **3 / 1** |
  | **total** | **5 / 11** | **10 / 6** |

  Every fact now has an active date; the 6 that remain inactive are the genuine role-playing
  secondaries (`EndDate`, `SystemModstamp`, `ClosedDate`) — correct, since only one can be active.
  Calendar-bound field refs on that report rise 5 → **8**, with **0** cross-island flat series.

  **Fail-closed where the evidence is genuinely absent.** A usage TIE does not break the tie by
  position — it falls through to the conventions, and if those cannot decide either, every
  relationship stays inactive. Breaking a tie arbitrarily is exactly the "silently picking the wrong
  business date" this has always refused to do.

  **Corpus of 34:** 1612 → 1612 files, 0 added, 0 removed, **7 differing** — 4 in `0088` plus 3
  metadata. **33 workbooks untouched**, calc coverage identical at 216/287. Corpus-wide date
  relationships go 28 active / 22 inactive → **33 / 17**, the entire delta inside `0088`.

  One trap worth recording: `migrate_datasource` forwards `**kwargs`, so adding the parameter to
  `assemble_import_model` alone made `migrate_tds_to_semantic_model` (explicit signature, no
  `**kwargs`) raise `TypeError` — which the caller catches, marking the whole `.pbip` **skipped**
  rather than failing loudly. 22 tests caught it; without them a threading mistake would have
  presented as workbooks quietly not building.

- **`tableau-migration` (skill `2.258.0` → `2.260.0`): separate Tableau datasources now get separate
  calendars — the months-old blocker, root-caused.** A Tableau workbook can hold several
  datasources, and Tableau never lets one datasource's filters reach another's marks. Power BI has no
  such boundary and *requires* a marked date table, so the rebuild fabricated **one** calendar and
  wired it to facts in every datasource — silently giving a date slicer reach the source never had.
  Measured on Salesforce NPSP: one `Date` table related to **10 facts across all four** datasources.

  The per-datasource fix was written months ago and switched **off**, because enabling it cost three
  calculations (119 → 116 translated). The recorded suspicion was calc *field-resolution
  tie-breaking*. **That was the wrong mechanism.** The emitted table sets are identical apart from the
  calendars, and `Record ID` — the field blamed for it — resolves to no table in *either* build.

  The real cause is one line:

  ```python
  conformed_hubs = {date_name} if date_name else None      # date_name = _date_built[0][0]
  ```

  A generated calendar is a **degenerate hub**: every fact joins its dates into the shared key, so any
  two facts look "connected" by same-calendar co-occurrence. `_unique_countd_path` therefore excludes
  calendars as transit nodes — but the exclusion set was built from **one** name. With four calendars
  it named the first and left three live hubs, so the spurious paths returned, `COUNTD(IF ...)` saw
  false ambiguity, and three calcs stubbed. Naming **every** calendar as a hub restores **119/158**,
  with a `needs_review` set byte-identical to the single-calendar build — the same three calcs, not
  merely the same total.

  Enabling the split then exposed a second defect: `_date_binding_from_model` gated on `table`
  (singular), which a per-island report does not carry, so date binding switched **off** for the whole
  workbook — `0073` 6 → 0, `0088` 5 → 0, `0079` 1 → 0 calendar-bound refs. Per-island reports now emit
  `by_island`, and the binder sends each pill to its own island's calendar.

  **Keyed on the DATASOURCE — the first attempt keyed on the relation name and was wrong.**
  `pmdm__ProgramEngagement__c` is a table in all four Salesforce islands, so an entity key collides;
  resolving it "first wins" bound an **Intake** pill to the **Service Delivery** calendar — a calendar
  with no active join to that fact, i.e. the flat series this split exists to remove. It presented as
  an *improvement* (the corpus count read 6, up from 5). A resolved field carries its own
  `datasource`, and the model tags each relation with `source_datasource`; that pair is the only
  identifier both sides genuinely share. An unattributable pill now **declines** rather than binding
  to an arbitrary calendar.

  **Corpus of 34:** 1606 → 1612 files, 11 added / 5 removed (each `Date.tmdl` → one calendar per
  datasource), 24 differing — confined to the **5** multi-datasource workbooks plus 3 metadata files.
  **29 workbooks untouched.** Calc coverage identical at **216/287**; calendar-bound refs identical at
  **59**; cross-island flat series on `0088`: **0**. Injecting the one-calendar hub set fails exactly
  one test out of 4,989 — the new one — so no neighbour already catches it.

  Still open, and deliberately not claimed as fixed: this restores **isolation**, not the inactive
  date joins. `0088` keeps 5 active / 11 inactive, because a table with two date columns (`StartDate`
  *and* `EndDate`) can still have only one active relationship to a given calendar.

- **`tableau-migration` (skill `2.257.0` → `2.258.0`): Snowflake custom SQL emits the drilled native
  query instead of an empty-table scaffold (#162).** A Snowflake `custom_sql` relation landed as
  `Source = #table(type table [], {})` — a model that opens, validates, and returns nothing. In one
  reader's 46-asset estate that literal marker appeared in **40 tables across 33 of 46 assets
  (~72%)**, making it the largest single source of manual completion on a Snowflake migration.

  **Measured before changing anything, at the artifact rather than the gate:** emitting both
  partitions for a synthetic Snowflake descriptor showed the TABLE path *already* producing the exact
  drill (`Source{[Name=<db>, Kind="Database"]}[Data]`) while the custom-SQL path scaffolded. So the
  branch was reaching the scaffold on `cls in NATIVE_QUERY_CATALOG_DRILL` alone, not for want of an
  emitter — one set membership.

  **The exclusion was deliberate and its promotion bar was written down**: *"unverified against live
  … promotion is a one-line addition once a connector's drilled native query is confirmed live."*
  #162 supplies exactly that, and the comment now records **whose** live instance, because it changes
  what the claim is worth: a reader's SHIPPED model emits this shape across 2 workbooks / 10+ tables
  including ~90-line multi-join SQL, and the same shape was derived independently from the
  connector's navigation semantics. The three recorded doubts are each answered rather than waived —
  the drilled-handle capability *is* the shipped shape; the mandatory warehouse is already threaded
  (`Snowflake.Databases(#"Server", #"Warehouse")` on the table path today); and uppercase identifier
  folding is untouched, because the SQL passes through verbatim apart from M string escaping.

  Emitted output is shape-identical to the reader's shipped model, and the native query runs against
  the **drilled** handle — the root collection rejects native queries outright.

  **Fail-closed preserved:** a custom-SQL relation has no three-part name, so it can only take the
  connection's database; when that is absent the partition still scaffolds with a specific reason
  rather than inventing a catalog. Pinned by test.

  **Two existing tests asserted the opposite and were rewritten, not deleted** —
  `test_emit_snowflake_custom_sql_still_scaffolds` and
  `test_snowflake_custom_sql_is_flagged_needs_review`. Both were *correct when written*; the
  assertion changed because the evidence changed, and each now says so in place. Their real intent —
  *an unverified connector fails loud at build time* — is preserved against **Oracle**, which still
  hits the same branch. That exemplar was itself chosen by measurement: the first attempt used
  Redshift and failed, because Redshift is a `server_database` connector reaching an
  already-verified branch and proving nothing about this gate.

  **Inert on the corpus by construction, not by diff:** parsing every `<connection class=…>` across
  the 34 staged workbooks gives **0 using Snowflake** (29 excel-direct, 9 federated, 1 databricks…).
  A change that cannot fire is a stronger statement than a diff that happens to show nothing — and it
  names the real gap: this path has no corpus coverage at all, exactly like Custom SQL before `0136`.

  Suite 4988 → **4995**.

- **`tableau-migration` (skill `2.254.0` → `2.257.0`): a keyword search can only disprove the word you
  chose, and the block protocol gains a timing rule.** Docs-only; no code, no corpus change.

  **The search that closed an investigation.** A parked note read *"grand total at TOP and whole
  numbers — neither is in the `.twb` XML; searched: `grand` appears only in product names"*, and the
  work stayed closed for days on it. Tableau writes `<rows onTop='true' total='true'>` — `total` is
  the on/off, `onTop` is the position — and one parse found both instantly. The failure mode is
  specific enough to name separately from the other inference errors already recorded: **the search
  term came from the TARGET system's vocabulary** (Power BI says *"grand total"*) **and was run
  against the SOURCE system's serialisation**, which uses different words for the same concept. A
  negative keyword result is evidence about your guess, never about the artifact. Parsing has no such
  mode because it enumerates what is present rather than asking whether one guess is.

  Two habits recorded with it: enumerate the attributes actually on the elements you care about
  before concluding absent, and **look the target property up** —
  `powerbi-report-author formatting describe-object <visualType> <object>` is a real schema oracle
  that also answers *"does this visual type even have this object"*.

  **Emitting nothing is a decision, not neutrality.** Power BI's table shows a total row by default,
  so 42 emitted grids inherited a row their Tableau source never displayed. An addition is harder to
  notice than an omission because it looks like data. Wherever a platform default exists, "we did not
  set it" and "we chose the default" are the same artifact.

  **Block timing (`AGENTS.md`).** Publishing a block's whole extent up front made its size visible;
  it did not stop blocks dying, because a block dies the instant the other party's tip passes it
  whatever its size. The complement is *when*: blocks claimed at the start of a lane died **four
  times in one day**; a block claimed seconds before committing survived. So **publish fully when you
  claim, and claim at the release step.** Also recorded there: `refs/tags` is shared across every
  worktree, so a version collision is also an anchor collision — never `git tag -d` an anchor you did
  not create, leave a dead block's anchors alone (deletion is the only irreversible step in the
  ritual), and remember an anchor tells you a version is *claimed*, never that it is unlanded.

  Suite 4988, unchanged (docs-only).

### Fixed

- **`tableau-migration` (skill `2.251.0` → `2.254.0`): a rebuilt table no longer invents a grand total
  the workbook never showed.** Tableau writes the grand total on the shelf element and never uses the
  word "grand": `<rows onTop='true' total='true'>`. `total` turns it on; `onTop` puts the row at the
  TOP. Searching a `.twb` for "grand" finds product names — which is exactly how both facts had been
  recorded as *"not in the .twb XML"*. That note was an inference from a keyword Tableau does not use;
  parsing the shelf attributes found both immediately.

  The asymmetry is what makes this matter: **Power BI's table shows a Total row by DEFAULT**, so
  emitting no toggle is not neutrality, it is a decision — and the wrong one for nearly every view.
  Measured: **11 of 162 `<rows>` elements across the corpus of 34 declare a grand total** (3
  workbooks), while **all 42 emitted grid visuals set no toggle** and inherited the default. An extra
  row of plausible numbers is harder to spot than a missing feature, because it looks like data.

  `total.totals` is now written in **both directions** from the shelf: 61 tables suppress a total
  their source never declared, 8 keep one it did. The property was looked up in the visual-type
  schema rather than assumed — a flat `tableEx` exposes exactly one total control, `total.totals`
  (bool).

  **Position is a platform limit, so it is disclosed rather than silently applied.** `tableEx` has no
  total-position property at all, and a matrix's `rowSubtotalsPosition` governs per-group *subtotals*,
  not the grand total. A view that asked for the total on top is rebuilt with it at the bottom and
  warned, because quietly relocating the one row a reader looks at first is the failure this avoids.

  **Verified by render, as a one-variable A/B** — the same built report, byte-identical but for that
  one property:

  | `totals` | last row of the table |
  |---|---|
  | `true` | `Total  292,296.81  2,326,534.35  38,654` |
  | `false` | `Tables  -17,753.21  208,020.18  1,261` — no Total row |

  Everything else identical: 17 rows, same values, same conditional colours, same slicers. That is
  what wrong would have looked like, recorded so the choice is defensible later. A first attempt at
  this render was worthless and nearly misread — the model was unrefreshed, so the table was empty and
  "no total" was indistinguishable from "no data"; the A/B only became evidence after a refresh +
  `reload`.

  **Corpus of 34:** 1606 → 1606 files, 0 added, 0 removed, 72 differing — 69 `tableEx` visuals across
  the `pbip` and `reports` trees whose only delta is the `totals` property, plus 3 metadata files.
  Matrices deliberately untouched: their grand-total visibility has no documented toggle, so guessing
  one would be the confidently-wrong move this release exists to stop.

### Added

- **`tableau-migration` (skill `2.241.0` → `2.251.0`): when you find a defect, record the nearest
  artifact that does NOT have it.** Docs-only; no code, no corpus change.

  2.241.0 fixed a whole Tableau filter scope that was being dropped, and it was diagnosed in **one
  probe** because the corpus already held the control: `0132` and `0133` differ in that scope and
  almost nothing else. Instrumenting both at the same seam showed the dashboard zone tokens
  byte-identical and only the resolver map differing — 3 entries against 0 — which converted *"filter
  cards are flaky on this workbook"* into *"the parse never produced the filters"*.

  The reusable part is that **the note filed weeks earlier was wrong about the cause** — it blamed
  the multi-dashboard structure, when the real difference was one Tableau menu choice (*Apply to →
  All Worksheets Using This Data Source*, which hoists the filter into a workbook-level
  `<shared-views>` element). The mistaken theory cost nothing because what had been written down was
  the **pair**. A control you already have outlives an explanation you may have to retract, and it is
  the cheapest form of "vary one thing": you are not building a control, you are noticing one.

  **Reach independently re-measured** rather than quoted, and with a better denominator: parsing
  every `<shared-view>` across the 34 staged workbooks gives **1** using this scope (`0133`, 3 shared
  filters, 0 worksheet-local). But **18 of 34 carry no filters at all**, so among workbooks that
  actually filter it is 1 of 16 — thin either way, and worth a purpose-built workbook, since `0133`
  is currently the only thing standing between this path and a silent regression.

  Suite 4980, unchanged (docs-only).

### Fixed

- **`tableau-migration` (skill `2.236.0` → `2.241.0`): a filter scoped to *All Worksheets Using This
  Data Source* is no longer lost, and its dashboard cards rebuild.** Tableau serialises filter scope
  **structurally**, and the two shapes live in different places: *Only This Worksheet* writes
  `<filter>` inside the worksheet's own `<view>`, while *All Worksheets Using This Data Source*
  **hoists** the filter out to a workbook-level `<shared-views><shared-view name='<datasource>'>` and
  leaves each participating sheet only a `<slices><column>` naming the sliced field.

  The parser read worksheet-local `<filter>` elements only. Every filter authored in the second
  (entirely ordinary) scope therefore vanished — and with it every dashboard filter **card**, because
  a card is little more than a `(datasource, field-instance)` token that has to resolve through a
  matching worksheet filter.

  **The control is what makes this provable.** Corpus workbooks `0132` and `0133` differ in this
  scope and almost nothing else. `0132` keeps three filters on worksheet `Profit` and rebuilt all
  three slicers; `0133` hoists the byte-identical three into `<shared-views>` and rebuilt **none** —
  nine cards across three dashboards. Instrumenting both showed the dashboard zone tokens are
  **identical**; only the resolver map differed, **3 entries against 0**. That is why the failure
  presented as a filter-card problem and was really a parse problem, and why the fix belongs at the
  parse seam rather than at the card.

  A sheet inherits a hoisted filter exactly when its own `<slices>` names the column — Tableau's
  per-sheet record of what it is sliced by — so a sheet that opted out is never handed a filter it
  does not have, and the inference stays evidence-based rather than "every sheet on this datasource".
  A worksheet-level filter on the same token always **wins**, so the specific beats the inherited and
  no column yields two slicers.

  **Corpus of 34:** 1588 → 1606 files, **18 added** (9 cards × the `pbip` and `reports` trees), 0
  removed, 3 differing (timestamps + the warning count). Thirty-three workbooks untouched. Warned
  visuals **118 → 109**, exactly the nine. The `added > 0, removed == 0` shape is normally this
  project's enumeration-failure signature; here the roots were equal-length and the additions are
  named slicer visuals, so it is genuine codegen.

  Verified at the artifact: each of `0133`'s three dashboards now emits Category + Segment +
  `Date.Year` beside its existing parameter slicer, laid out non-overlapping — and the date filter
  correctly rebinds to the calendar's `Year` column rather than the raw datetime.

  **Reach, stated honestly:** only **1 of 34** corpus workbooks uses this scope, so coverage here is
  thin — but it is a single menu choice in Tableau's filter card, so its real-world frequency is far
  higher than the corpus implies. What wrong looks like: the broken form does not render a broken
  slicer, it renders **no** slicer on a page that otherwise looks complete, so the reader never learns
  an interaction was authored.

### Added

- **`tableau-migration` (skill `2.230.0` → `2.236.0`): two verification rules that only became
  visible by working in parallel.** Docs-only; no code, no corpus change.

  **A per-commit gate is structurally blind to a defect that lives in the RELATION between two
  artifacts.** The three clauses recorded in 2.226.0 all ask whether a gate can detect a defect *in*
  something. This is a different axis. Observed live: two parallel sessions produced CHANGELOG
  entries declaring `2.227.0 → 2.230.0` and `2.227.0 → 2.228.0`. **Each passed the chain gate in
  isolation** — it checks predecessor *equality*, not increment-by-one — and the chain was broken
  only at the seam where the branches met. So `git rebase --exec` proves every commit green
  independently and says nothing about the order they land in; *"green at every commit"* is a weaker
  claim than it sounds. Not fixable inside the gate, which can only ever see one commit's copy of the
  file — it needs the integration step to look at both sides. Recorded so the claim stops being
  overstated.

  **Prefer the failure mode a reader can detect.** When no faithful translation exists the choice is
  between two wrongs, and the tiebreak is which one is legible to the person looking at the report.
  Two decisions reached this from opposite directions in one day: `ATTR()` was being *dropped*, and
  rebuilding it as `MIN` is wrong only where Tableau itself prints `*` — *a reader cannot notice a
  missing value, but can notice a minimum*. A date axis on a table the calendar had skipped was being
  *bound anyway*, producing a flat line at the grand total; declining the binding gives a plainer
  axis — *a flat series looks like data*. One prefers visible-but-imperfect over absent, the other
  plain over plausible; both pick the outcome whose wrongness is **detectable**.

  Suite 4971, unchanged (docs-only).

### Fixed

- **`tableau-migration` (skill `2.229.0` → `2.230.0`): a date axis on a table the calendar SKIPPED no
  longer rebinds to the calendar and flatten.** The report binder identifies a date pill by COLUMN
  NAME — at that point the field's entity is still the workbook's relation name, not the model's table
  — so `migrate_estate._date_binding_from_model` publishes `ambiguous_keys`: date columns that are
  active on one table and not on another that also carries them. The binder declines those, because
  rebinding the wrong fact onto a calendar that cannot filter it returns the grand total in every
  bucket: a flat line, unwarned and validate-clean.

  That guard computed its contested population from the date RELATIONSHIPS, so it could only ever see
  tables that received one. A table the calendar deliberately **skips** — a pure dimension, or one
  that never landed — has no relationship at all, so it never appeared, and the guard was blind to
  exactly the shape it exists to catch. *No relationship* is a stronger version of *inactive
  relationship*, not an exemption from it.

  Measured on Salesforce NPSP: `caseman__Intake__c` is only ever the `one` side of
  `Case → caseman__Intake__c.Id`, so `_build_date_dimension` excludes it as a pure dimension — yet it
  carries `CreatedDate`, which is ACTIVE on `Case`, `caseman__Goal__c` and
  `pmdm__ProgramEngagement__c (Intake)`. Its Month axis rebound to `Date[Month Start]` on a table the
  calendar cannot filter. Before: 9 calendar-bound visuals, 7 correct, **1 flat series**. After: 5
  calendar-bound, 4 correct, **0 flat**. The 4 that gave up their calendar binding fall back to the
  fact's own date column — correct values without the calendar hierarchy, which is the guard's
  existing miss-over-wrong tradeoff applied consistently rather than a new policy.

  `assemble_model._build_date_dimension` now reports `unrelated_date_columns` (table + column for
  every date column on a table it skipped); `_date_binding_from_model` folds those into the contested
  set. Both keys are additive and absent when nothing was skipped, so every model whose date-bearing
  tables all joined the calendar keeps its report byte-for-byte.

  Same shape as 2.226.0 one layer down: the guard was keyed on a PROXY population (relationships)
  instead of the real one (every table carrying the column name). What wrong looks like, so this is
  defensible later: the failing visual rendered a *solid block at the grand total* across all months
  rather than a monthly series — visibly different from the correct render, not merely absent.

- **`tableau-migration` (skill `2.228.0` → `2.229.0`): `ATTR()` rebuilds as `MIN` instead of being
  dropped.** Tableau's `ATTR([x])` returns the value when it is unique across the mark's rows and the
  literal `*` when it is not. Power BI has no such aggregate, and the pill fell through to the
  unsupported-derivation branch — which **drops it**. Measured on corpus workbook
  `0135_aggregation_types`: its `ATTR` worksheet emitted a table with only its row dimension and **no
  value at all**, and `Bar chart Example` — three pills of the same field at three aggregations on
  one shelf — fanned into **2** side-by-side charts instead of 3.

  **`MIN` is the faithful choice, not the convenient one.** `ATTR` is written precisely when the
  author expects the value to be constant within the mark, and wherever it *is* constant `MIN(x)`
  **is** `x` — identical, not approximate. The two differ only when the value is not unique, which is
  exactly the case Tableau itself flags with `*`. So the degradation is confined to the case the
  source already calls ambiguous, and it is warned with what changes. Emitting a pill that is wrong
  in one case beats dropping it in every case: a reader cannot notice a missing value, but can
  notice a minimum.

  Deliberately **not** added to `_AGG_FUNC`, which would have been the tempting one-liner: that path
  inherits the `Min`/`Max` type restriction refusing non-numeric columns, and would have dropped
  precisely the commonest `ATTR` — the one over a string (`ATTR([Region])`), where a text `Min` is
  both valid in PBIR and exactly the intended answer.

  **The pill count is the proof, which is why `0135` is in the corpus.** After: the `ATTR` sheet
  carries `Min(Orders.Sales)`, and the trellis emits **3** charts — Sum, disaggregated Column, and
  Min. A dropped aggregation was a missing chart, not a subtly wrong one, so the gap was countable in
  the output rather than a matter of judgement.

  **Corpus of 34:** 1586 → 1588 files, **2 added** (the third trellis chart, in both the `pbip` and
  `reports` trees), 0 removed, 9 differing — every one inside `0135`. Thirty-three workbooks
  untouched. Roots equal-length (209/209), so the one-sided add is genuine codegen rather than the
  enumeration artifact that shape usually signals.

  Suite 4963 → **4966**.

- **`tableau-migration` (skill `2.227.0` → `2.228.0`): a caller-side gate stricter than the callee it
  guards no longer discards whole workbooks (#155).** `_build_datasource_pbip` wrapped the
  published-datasource recovery in `if descriptor is None:`, skipping it whenever a combined /
  federated descriptor was present. The reader measured the counter-example: **a published datasource
  PLUS one small embedded federated datasource is both things at once**, and such a workbook was
  skipped entirely — *"published-datasource workbook — co-migrate its published datasource"* — while
  the published rebuild sat there available. Bypassing the gate and changing nothing else produced
  **52 measures / 86.5% translated, 16 calculated columns / 93.8%**.

  Verified against current code before acting, not taken from the report (it was filed against
  2.151.0): the gate is still at the call site, and `_rebuild_from_published_match` still takes no
  `descriptor` parameter at all.

  **Removing it cannot loosen the safety model, and the argument is structural rather than a
  judgement call.** That branch only runs when `res_report["fallback"]` is already set — *the model
  has already failed*. The gate therefore never protected a good model; it only decided whether to
  attempt a recovery on a build with nothing left to lose. All the protection lives in the callee,
  which is fail-closed three ways: a catalog must exist, the binding signal must be `published`, and
  the name must match exactly one non-ambiguous entry. Anything else returns `None` and the honest
  skip stands.

  The general shape is the one this repo keeps rediscovering: **a caller-side condition the callee
  never asked for is a second predicate that can disagree with the first**, and it will be the one
  nobody re-derives. Its stated rationale — *"its islands are already real schemas, so a fallback
  there is a genuinely-undoable shape"* — was a plausible inference that a single real workbook
  falsified.

  Tests pin the safety where it actually lives: the callee's signature carries no `descriptor`, each
  of its three guards refuses independently, and the caller no longer gates on `descriptor`. That
  last one reads the calling function's source and **says so** — a behavioural test would need a
  published + federated workbook and the corpus has none, so it asserts the narrower thing it can
  prove rather than implying more.

  **Clause 3 clean on the first attempt** (both trees patched from the start): reintroducing the gate
  gives `1 failed, 4962 passed` — the single failure is the new test, so nothing else in the suite
  notices.

  Suite 4957 → **4963**.

### Added

- **`tableau-migration` (skill `2.226.0` → `2.227.0`): a visual that projects an inert `BLANK()` stub
  is now disclosed.** The first instrument built for the *structurally valid, semantically absent*
  family named in 2.226.0.

  When a calc cannot be translated the model emits `measure 'X' = BLANK()` so the reference still
  resolves — and it resolves **perfectly**. The visual binds, `pbir_lint` is clean,
  `lint_visual_model_bindings` is clean (the measure genuinely exists),
  `powerbi-report-author validate` returns 0 errors, and the chart renders **empty**. Measured on
  corpus workbook `0136` before 2.225.0: Sheet 3 projected `complex nested`, a stub, while
  `viz_fidelity` recorded `{"status": "rebuilt", "reason": null}`. The MODEL layer knew — the
  translation handoff listed the calc as needs-review — and the VISUAL layer never repeated it.

  Every other gate here asks *is this well-formed*; this one asks *does it say anything*, which is
  why it reads the measure's EXPRESSION rather than its existence. Reported as
  `visuals_projecting_stub_measures` (visual, page, measure) on the SHIPPING parts, after the
  cross-check and after twin retirement, so it describes the `.pbip` the user opens.

  **The narrowness is the feature.** Only an expression that is exactly the stub form counts. A
  measure that returns blank *conditionally* — `IF(<cond>, 1)`, the shape every keep-flag in this
  project emits — is doing its job; matching "contains BLANK" would fire on correct output
  constantly and the finding would be worthless within one release.

  **Proved by control and injection on the same real workbook**, not a fixture: the 2.225.0 build
  reports `null`, and disabling the alias fix — which restores `complex nested` as a projected stub —
  makes it emit
  `[{"measure": "complex nested", "page": "page-ws-Sheet3…", "visual": "v-Sheet38c3a8a7b"}]`.
  Same data, same build, only the defect differing.

  **Clause 3 run properly, after getting it wrong twice.** Disabling the detector in **both** trees
  gives `3 failed, 4954 passed` — every failure one of the new tests, so nothing else in the suite
  catches this. The first two attempts patched canonical only, and mirror parity failed alongside,
  making the count uninterpretable. Same rider, learned twice: in this repo a source-level injection
  is a two-tree edit whether you want it to be or not.

  Suite 4950 → **4957**.

- **`tableau-migration` (skill `2.225.0` → `2.226.0`): the day's verification findings collapse into
  one rule, recorded where the next person will read it.** Docs-only; no code, no corpus change.

  **Read every confirmation at the artifact, never at the mechanism.** A gate going red, a trace
  firing, a test passing, a validator returning zero errors — all four confirm only that *something
  you built responded*. None says the file a user opens changed. Four instances, one day, four lanes:
  a gate keyed on a proxy that passed forever because its input set included the artifact under test
  (`git rev-list --all` counts `refs/tags`, so every anchor vouched for itself); a trace confirming a
  helper firing exactly as designed while the emitted measure stayed `= BLANK()`, because the
  consumer read a different list; eighteen green tests on a feature that was completely inert; an
  isolated emitter returning three correct objects onto a page that was still wrong.

  The proving sequence, with the clause that actually gets broken: **(1)** it passed → no evidence
  until seen red; **(2)** it went red → no evidence until the defect is one its *neighbours* do not
  already catch; **(3)** (2) is only measurable on the **full** suite, with an injection valid in
  every *other* respect. Both parallel sessions violated clause 3 independently, in the same hour,
  while stating clauses 1 and 2 — because running the single file is *correct* while iterating and
  wrong only at the moment of claiming. The discipline attaches to the proof step, not the person.
  A rider learned by getting it wrong: in this repo a source-level injection is a **two-tree edit**,
  so patching canonical alone fails mirror parity too and the count stops being interpretable.

  **Structurally valid, semantically absent** — the defect family no structural gate can see, named
  from three sightings in three unrelated lanes: a calc stubbing to `= BLANK()` (it *binds*, so every
  binding check passes while the visual renders empty), a CHANGELOG entry that is a header with no
  body, and a visual whose `SelectRef` names a projection that no longer resolves. Ask whether the
  emitted artifact **says** anything, not whether it is well-formed.

- **`tableau-migration` (skill `2.215.0` → `2.225.0`): a calc that reaches across a declared join no
  longer stubs to `BLANK()` and renders an empty chart.** In corpus workbook
  `0136_custom_sql_prefix_and_params`, the calc `complex nested` mixes a relation-qualified reference
  (`[Region (Custom SQL Query2)]`) with plain ones (`[Sales]`, `[Sub-Category]`). That spans two
  tables, so the translator refuses it — *"SUM(expr) must reference exactly one table"* — correctly,
  because a row expression cannot be evaluated across an unjoined pair. The calc then stubs to
  `= BLANK()`, **which binds normally**, so Sheet 3 rendered an empty bar chart while `viz_fidelity`
  recorded `{"status": "rebuilt", "reason": null}`. Loudly refused in the model, silently wrong in
  the report.

  **The workbook itself declares the way out.** Its object-graph relationship predicate is literally
  `[Region] = [Region (Custom SQL Query2)]`, so on every row the join produces the two hold the same
  value — by the author's declaration, not by inference. Substituting the plain caption is therefore
  faithful *and* makes the expression single-table. Measured through the real translator, same
  inputs, only the aliased reference differing:

  | | result | tables |
  |---|---|---|
  | before | `None` — *"SUM(expr) must reference exactly one table"* | 2 |
  | after | `CALCULATE(SUMX('Custom SQL Query', IF(EXACT(…) && EXACT(…), …[Sales])), ALLEXCEPT(…))` | 1 |

  **Why not `RELATED()`**, which is the obvious answer: it needs a many-to-one direction to traverse,
  and an authored object-graph relationship is emitted **many-to-many on purpose** (see
  `generate_relationships_tmdl` — it is uniqueness-agnostic, so an m:m join cannot be rejected for a
  non-unique target and cancel the batch). There is no ONE side, so the reach would have to invent a
  cardinality the source never stated.

  **Keyed on the declaration, never on the name.** An alias is recorded only when a relationship's
  single-column `=` predicate names both captions. Two plain captions, two differently-qualified
  captions, and any non-`=` predicate all yield nothing — collapsing columns the workbook has not
  declared equal would silently change the answer, and a look-alike name is exactly what that would
  look like. Substitution is bracket-delimited and longest-first, so `[Regional Manager]` survives an
  alias on `Region`.

  Workbook calc coverage on 0136: **1/4 → 2/4**. The two that remain are the defensible ones — on no
  worksheet, so no aggregation is observable anywhere in the artifact, and Tableau records no default
  aggregation on a calc column; inventing `SUM` would be a guess.

  **Measured, both operands and the count named.** `corpus_b216` (built at `d0f16e7`) vs
  `corpus_v216`, same **34**-workbook input: 1586 files vs 1586, **0 added, 0 removed, 3 differing** —
  the one `_Measures.tmdl` that gained the measure, plus `report.json`/`summary.md`. Thirty-three
  workbooks byte-identical.

  **A wiring bug worth recording, because the trace lied by omission.** The first attempt rewrote the
  merged `all_calcs`, and an instrumented run confirmed the helper firing exactly as designed —
  4 calcs in, 2 aliases, 1 rewritten. The measure still emitted `BLANK()`: measure emission reads
  `calcs` **directly**, so the alias was visible to the flag pipelines and invisible to
  `_measures_part`. A trace proving your own function ran is not evidence that its effect reached the
  output; only the emitted artifact is.

  Suite 4938 → **4950**.

- **`tableau-migration` (skill `2.214.0` → `2.215.0`): a CHANGELOG entry that exists but says
  nothing fails the suite.** A cross-session rebase left a header-only duplicate of a renumbered
  entry -- same shape, same `(skill X → Y)` marker, zero prose. Every existing check passed on it,
  because each asks about the HEADER: the version was present, the chain was continuous, nothing was
  claimed twice. An entry can EXIST, be WELL-FORMED, and be EMPTY.

  * **Shares the existing parser rather than re-parsing.** A second parser over the same file would
    be a second predicate, and the two could disagree about what an entry IS while each looked
    correct alone -- the exact failure this module exists to prevent, reintroduced one level down.
    An entry's body runs from its header to the next entry, the next Markdown heading, or EOF; the
    heading stop matters because the file interleaves `### Added` and `### Fixed`, so the last entry
    in a section would otherwise absorb the next section's header as content.

  * **The threshold is measured, not assumed.** `> 0` rather than a size floor, because across the
    99 entries in this file the smallest real body is **604 characters over 7 non-blank lines**. A
    legitimate entry clears the check by roughly two orders of magnitude, so it cannot fire on
    terse-but-real prose -- only on genuine emptiness.

  * **Proved RED, and proved to catch what the others cannot.** First injection reproduced the rebase
    artifact exactly and fired three checks at once -- correct, but not evidence that this one adds
    anything, since a duplicate also breaks the chain. Second injection was a header-only entry that
    is otherwise PERFECT: unique version, continuous chain (`2.214.0 → 2.215.0` above
    `2.213.0 → 2.214.0`), and matching the shipped stamp. Chain, duplicate and stamp checks all
    PASSED; only the body check fired, naming `2.215.0 at L17`. That is the discriminating result --
    a gate proved only against a defect its neighbours also catch has not been shown to be worth
    anything.

- **`tableau-migration` (skill `2.213.0` → `2.214.0`): a released version with no rollback anchor now
  fails the suite.** The 2.205.0 gate proves every anchor that EXISTS names a reachable commit. It
  cannot prove one exists — and absence turned out to be the live failure, with a cause neither
  parallel session had named.

  `git rev-parse --git-common-dir` is the **same `.git` for every worktree**, so `refs/tags` is a
  single GLOBAL namespace shared by all parallel sessions. Two sessions colliding on a version number
  therefore also collide on its anchor name: one slot, last writer wins. Reconstructed from tagger
  dates: session A cut `rollback/pre-v2.211.0`; session B's identical `git tag -a` failed with
  *"already exists"*, B read it as leftover from its own discarded renumber, deleted it and re-cut at
  B's commit; A later renumbered away from 2.211.0 and deleted that tag during cleanup — by then B's.
  **Each session destroyed the other's anchor while truthfully reporting its own as verified**, and
  each then read the other's as "reported but absent".

  `test_every_released_version_has_an_anchor` reads the CHANGELOG's own `(skill A → B)` chain as the
  roster of shipped versions — so the roster cannot drift from what was actually released — and
  asserts each has a matching `rollback/pre-v` tag. **Proved it can fail** by deleting a real anchor
  and watching it name the exact version, then restoring it; a gate that has never been seen to fail
  is indistinguishable from one that cannot.

  The two rules this makes enforceable rather than remembered: never `git tag -d` an anchor you did
  not create (`%(taggerdate)` identifies the owner in one call), and a version with no anchor is a
  release with no rollback.

- **`tableau-migration` (skill `2.212.0` → `2.213.0`): a Tableau parameter inside Custom SQL no
  longer ships a query the source cannot parse.** Corpus workbook
  **`0136_custom_sql_prefix_and_params`** — built to spec by the tool's own user against a live
  **Databricks** warehouse — is added, and with it the first Custom SQL coverage this project has
  ever had: **0 of 31** corpus workbooks contained a `<relation type='text'>` before it landed,
  measured by parsing every relation rather than by regex. The engine's whole Custom SQL path
  (`Value.NativeQuery`, `Odbc.Query`, the DirectQuery storage-forcing rule, parameter detection, the
  2.155.0 stub gate) had never run against a real workbook. It is also the first corpus workbook on
  Databricks and the first to emit `mode: directQuery` partitions.

  Its relation 3 embedded a parameter the way Tableau authors do, because Custom SQL is Tableau's
  only way to push a predicate to the source:

  ```sql
  SELECT `Region`, SUM(`Sales`) AS REGION_SALES FROM orders
  WHERE `Region` = <[Parameters].[Parameter 3357119534784517]> GROUP BY `Region`
  ```

  That token reached the emitted `Value.NativeQuery` verbatim, so Spark rejects the query at parse
  and the DirectQuery table cannot answer at all.

  **The fix is a classification, not a translation.** Power BI *does* have a direct equivalent —
  Dynamic M Query Parameters — and it is deliberately NOT used here. When the filtered column is in
  the query's result set, the idiomatic rebuild is to drop the predicate and let an ordinary slicer
  on that column filter; in DirectQuery the slicer folds back into a `WHERE` at the source, so
  nothing is lost. Reaching for a dynamic M binding in that case would ship an exotic construct
  (strictly 1:1, no RLS, no aggregations, not in Report Server, banned Top-N/contains/exclude/
  cross-highlight/drill-down slicer operations, and no spaces permitted in the parameter OR table
  name) to answer a native question. The genuine dynamic-M case — a filtered column absent from the
  result set, which no model filter can reach — stays warned, but the warning now names the exact
  Desktop step (*Properties → Advanced → Bind to parameter*) plus its preconditions, because
  research could not confirm that binding's on-disk form from any Microsoft primary source and a
  guessed annotation breaks a model at OPEN time, silently.

  The oracle for "is the column in the result set" is the relation's own metadata records — Tableau's
  account of what the query returns — not a parse of the SELECT list. `SELECT *` then needs no
  special case, and a hand-parsed projection cannot disagree with the columns the model emits.

  **The refusals are the feature.** Left alone entirely: an `OR` anywhere in the `WHERE`, a parameter
  inside a subquery, a non-equality comparison, a parameter outside a `WHERE`, and any query with
  more than one `WHERE`. The rewrite is also **disclosed** rather than silent: stripping the
  predicate widens what comes back until a slicer narrows it, and trading a loud failure for a quiet
  difference would be strictly worse than the bug.

  **Measured, both operands named.** Corpus `corpus_b212` (built at `63b34c3`) vs `corpus_v212`,
  same 32-workbook input: 1504 files vs 1504, **0 added, 0 removed, 2 differing** — the one table
  that carried the parameter, and `report.json`. Thirty-one workbooks byte-identical.

  **A second tautological guard was found and fixed before shipping**, the same way 2.211.0's was.
  Disabling the `OR`/subquery guard left the entire new test file green: every case was refused by
  some *other* check (the exact-conjunct match, or the two-`WHERE` test). The input that
  distinguishes it is `WHERE A AND B OR C`, where SQL precedence means `(A AND B) OR C` — splitting
  on `AND` makes `A` look like a clean parameter predicate, and dropping it silently yields
  `WHERE x = 1 OR y = 2`. That is not the benign widening the whole rewrite relies on: the surviving
  `C` rows were never constrained by the parameter's column, so no slicer can put them back. Both
  operand orders are now pinned, and the weaker `OR` test is explicitly labelled as NOT exercising
  the guard so no future reader cites it as evidence.

  Suite 4896 → **4908**.

- **`tableau-migration` (skill `2.211.0` → `2.212.0`): a parameter filter whose predicate is an
  AGGREGATE now actually filters.** Two workbooks supplied by the tool's own user, built to carry
  shapes a customer field trial reported, are added to the corpus as
  **`0134_parameter_filters`** and **`0135_aggregation_types`** — closing a hole worth stating
  plainly: the corpus previously contained **zero** workbooks that filter on a parameter, measured
  two ways (a parameter directly on the filter shelf: 0 of 29; a `member='true'` filter on a calc
  that references a parameter: 0 of 29). Every parameter-to-filter seam in the engine was untested
  against real output.

  `0134` filters five worksheets on five differently-shaped boolean calcs. Four already migrated
  correctly and are kept as **controls**; the fifth,
  `IF SUM([Sales]) > [Parameters].[Sales Param] THEN TRUE ELSE FALSE END`, emitted a chart with no
  `filterConfig` and no wrapper measure — entirely unfiltered. It was warned, so it was visible
  rather than silent, but the chart was wrong.

  The cause is that `_param_predicate_flags` is **row-level by construction**: it asks the *column*
  translator for a pure boolean and wraps the result in `COUNTROWS(FILTER(...))`. An aggregate cannot
  be evaluated per row, so it can never pass that gate. Widening the gate would have been the wrong
  repair — the faithful Power BI shape for an aggregate is the *opposite* one, a keep-flag measure
  evaluated at the visual's own grain, which is exactly Tableau's semantics (the aggregate is
  computed at the viz level of detail, so a chart grouped by customer keeps the customers whose own
  `SUM([Sales])` clears the parameter). So `_aggregate_predicate_flags` ships as a third sibling of
  the date-window and row-level pipelines, emitting `IF(<measure-mode boolean>, 1)`.

  Its binding deliberately carries **no** `row_filter`, and that absence is load-bearing:
  `_apply_row_predicate_wrapped_measures` keys on exactly that field, and rewriting an aggregate into
  `CALCULATE(<agg>, FILTER(<table>, <pred>))` would push it to row grain and quietly answer a
  different question. A missing filter is visibly wrong; a row-wrapped aggregate would be
  *invisibly* wrong, so the two failure modes are not equally bad and the tests say so.

  Disjointness is asserted rather than assumed: a calc the column translator can render as a boolean
  is refused here even with an empty skip set, so the row-level and aggregate paths cannot both claim
  one calc regardless of call order.

  **Measured, both operands named.** Corpus `corpus_b209` (built at `ac4f925`) vs `corpus_v209`
  (built at this change), same 31-workbook input: 1466 files vs 1466, **0 added, 0 removed, 4
  differing** — the one visual that gained the filter, the one `_Measures.tmdl` that gained the flag
  measure, and `report.json`/`summary.md` (visuals warned 100 → 99). Nothing changed in the other 30
  workbooks.

  **A test that could not fail was found and fixed before shipping.** The disjointness test first
  used `[Param] = [Segment]` as its row-level exemplar and passed with the guard deleted — the
  *measure* gate refuses that calc outright, so the guard was never reached. Measuring both
  translators across ten shapes showed only a parameter-only comparison (`[Param] > 5`) types as
  `bool` in **both** modes, and it is now the exemplar. Both load-bearing assertions were then
  proven to go red when their guard is removed, per "prove a gate CAN fail, not merely that it runs".

  Suite 4885 → **4896**.

- **`tableau-migration` (skill `2.210.0` → `2.211.0`): a container-stitched pseudo-table is MERGED
  into one visual, and the style cascade stops overwriting conditional formats.** N worksheets in a
  contiguous band, each contributing one measure, with the row labels hidden on all but the leading
  sheet, is Tableau faking a single table. Power BI has the real thing, so the rebuild now emits ONE
  visual with N value columns -- the row-label column appears once, as the author intended, instead
  of N times.

  * **Each column keeps its OWN conditional format.** Every member's rule is computed against that
    member's own state so its selector names only its own measure, and the Visual Calculation it
    declares is ported onto the merged state so the `SelectRef` resolves. The refs collide by
    construction -- every member state starts empty, so each claims the same first free name -- and
    a colliding ref whose EXPRESSION differs is renamed with the member's own references rewritten.
    Ported naively, the last member would win and every column would paint from whichever DAX
    arrived first, resolving cleanly and reporting nothing.

  * **A LATENT DEFECT THIS EXPOSED: the style cascade was overwriting conditional formats.** The
    font pass did `objects[k][0]["properties"].update(...)`, replacing whatever the first entry
    already set. Where entry 0 was a rule bound to a column, its `fontColor` was silently replaced
    by the flat cascade colour. It went unnoticed because entry 0 was normally the row-dimension
    entry, which has no rule to lose; merging promoted a real rule into that slot and one column
    came back uncoloured while the others painted correctly -- reading as "one column didn't work"
    rather than as a collision. The cascade is now a DEFAULT: keys the entry already sets are left
    alone, keys it does not set are still applied, so a gradient entry takes the cascade's font
    exactly as before.

  * **Render-verified against the author's Tableau screenshot.** One table, `Sub-Category` once,
    Profit / Sales / Quantity as three columns, one Total row, and each column bucketing
    INDEPENDENTLY -- green on Copiers/Phones/Accessories/Paper in Profit, red on Art in Sales, green
    on Binders in Quantity, matching the oracle cell for cell.

  * **Corpus, operands named.** `corpus/b212` (built from `1f12c94`) vs `corpus/v212`: 1468 files vs
    1460, **0 added, 8 removed, 6 differing**. The removals are the follower tables the merge
    absorbs (two per workbook, in both the pre-rebind and the final tree); the differences are the
    merged leader plus `report.json`/`summary.md`. **No workbook outside `0132`/`0133` changed.**

- **`tableau-migration` (skill `2.209.0` → `2.210.0`): a container-stitched pseudo-table is
  detected from the source and disclosed.** Tableau cannot put several independently
  table-calculated measures in one view, so authors fake a single table: N worksheets in a
  contiguous horizontal container, each contributing one measure, with the row labels hidden on
  every sheet but the leading one. The dashboard then READS as one table. The rebuild emits one
  table per sheet, so the row-label column repeats N times and the illusion breaks -- visibly on the
  page, and invisibly to every validator.

  * **The signature is exact in the source; no image, heuristic or model call.** Contiguous
    horizontal band (same `y`, same `h`, each `x` continuing where the previous ended), the same row
    dimension on every member, and the row labels hidden on the TRAILING members while the leader
    keeps its own. That asymmetry is the whole gate: it separates a stitched pseudo-table from
    tables that merely sit side by side.

  * **The negatives are what make it trustworthy, and the source workbook supplies both.** A
    single-sheet MEASURE TRELLIS renders an almost identical picture from completely different
    source, and a BAR-mark band is already rebuilt correctly because the engine suppresses its
    category axes. Detection fires on the stitched case and declines both -- verified on the real
    workbook, not a fixture. This is also the concrete argument for classifying from XML rather than
    from a rendered image: the image cannot separate cases that look the same.

  * **`row_labels_hidden` exists because the axis path drops the fact for a crosstab.**
    `_parse_hidden_axes` maps a hide onto a Power BI AXIS, which needs the shelf's role; it resolves
    for a cartesian chart and yields nothing for a table, because a table has no category axis. The
    author's hide was therefore parsed and then discarded for exactly the visual type this idiom
    uses. Added as a separate, additive signal rather than by widening the axis function, so the
    chart path is untouched.

  * **Disclosed, not silently rebuilt, and NOT shipped as inert plumbing.** The detector feeds a
    remediation-worklist item naming the sheets, the leader, and the native Power BI answer (one
    matrix with N value columns). A detector with no consumer is the same shape as a gate nothing
    calls, so it is wired on arrival rather than left for later.

  * **Corpus, operands named.** `corpus/b210` (built from `5e6d921`) vs `corpus/v210`: 1468 files
    vs 1468, 0 added, 0 removed, **2 differing -- `report.json` and `summary.md` only.** No
    `visual.json` and no `.tmdl` changed, which is the correct blast radius for a disclosure.

  * **Still open:** the merge itself. The measure on these crosstabs is not a plain shelf field, so
    building the merged matrix needs work this increment deliberately does not guess at.

- **`tableau-migration` (skill `2.208.0` → `2.209.0`): Tableau's "how many marks are in this
  view" idiom, and its escaped colour-map members.** Two independent defects from one real dashboard
  -- a dynamic-quartile view that ranks rows into Top 25% / middle / Bottom 25% and colours them.
  Either one alone lost the whole encoding, and neither produced an error anywhere.

  * **`WINDOW_COUNT(COUNTD([<dimension>]))` is now recognised as the view's mark count.**
    `WINDOW_COUNT` counts the MARKS in its frame for which the argument is non-null -- it does not
    aggregate the argument -- and a `COUNTD` of a dimension is never null at a mark. Lowering it
    through the generic aggregator path required resolving `COUNTD([D])` to a projected column,
    which cannot succeed: the visual GROUPS BY that dimension rather than projecting a distinct
    count of it. So the operand returned nothing, the whole colour rule declined, and the dashboard
    rendered with no colour at all. Lowered to the same `COUNTROWS(<frame>)` that Tableau's own
    `SIZE()` already produced, with the frame taken from the call so explicit bounds are honoured.
    Gated to `COUNTD` only -- `WINDOW_SUM(COUNTD(x))` is a real sum and still needs its operand.

  * **An escaped colour-map member no longer loses the author's palette.** Tableau serialises the
    member as `"Top 25\%"` while the calculation writes `"Top 25%"`, so a literal comparison
    missed and the member fell through to the default categorical ramp. Measured on the source
    workbook: `middle` matched and kept `#000000`, while `Top 25%` and `Bottom 25%` silently became
    `#E15759` and `#4E79A7` -- the author's green and red replaced by two arbitrary hues, with no
    warning. Both the raw and the unescaped spelling are now indexed, so a member whose real name
    contains a backslash still matches exactly as before.

  * **Corpus: 31 workbooks, and the blast radius is exactly the two that exercise it.** Two new
    workbooks were added to the corpus for this pattern (`0132_container-formatting-hidden-headers`,
    `0133_container-formatting-variations`), each with the Tableau oracle image and a verified
    `lessons.json`. Masked diff, `corpus/b206` (built from `ac4f925`) vs `corpus/v206`: 1468 files
    vs 1468, 0 added, 0 removed, 19 differing -- **every one of them inside `0132`/`0133` plus
    `report.json`. None of the original 29 workbooks changed.**

  * **Render-verified against the author's own Tableau screenshot.** Ground truth was computed
    independently from the emitted values (17 members; Top 25% = rank <= 4.25, Bottom 25% = rank >
    12.75) and matched the oracle cell for cell: green = Copiers / Phones / Accessories / Paper, red
    = Machines / Fasteners / Supplies / Bookcases / Tables. Confirmed on BOTH mark types -- font
    colour on the text tables, bar fill on the bar variants -- and each measure column buckets
    independently, so a member is black in Profit and red in Sales exactly as the source does it.

- **`tableau-migration` (skill `2.207.0` → `2.208.0`): the remedy we tell the user to run is one they
  can actually run.** `2.207.0` shipped detection of the Newtonsoft/GAC blocker together with a
  remedy naming **`gacutil.exe`** — which is part of the **Windows SDK**, absent from a normal
  analyst machine and absent from the machine that wrote it. That was noted in the same commit and
  still shipped as the instruction, which made it a remedy the reader mostly cannot follow.

  `System.EnterpriseServices.Internal.Publish` exposes `GacInstall` / `GacRemove` and ships with
  **.NET Framework itself** — so it is present on every machine that runs Power BI Desktop, by
  definition. Verified available on the development machine, which has no Windows SDK. The finding
  now leads with:

  ```powershell
  Add-Type -AssemblyName System.EnterpriseServices
  (New-Object System.EnterpriseServices.Internal.Publish).GacInstall('<path to Newtonsoft.Json.dll>')
  ```

  `gacutil` is still mentioned as the inferior alternative, and a test pins that the SDK-free form
  comes **first** — because the defect here was not the detection, it was the *instruction*, and an
  instruction the reader cannot execute is worth very little.

  **The detect-don't-repair boundary is unchanged, and the guidance around it is now explicit**:
  *offer* to run it, never run it unasked. The GAC is machine-wide and the fix needs elevation, so
  consent is the point — but consent given to an agent that then runs the command in front of the
  user is a materially better outcome than handing them a link and walking away.

  Documentation and one detail string — no engine code, no emitted-output change.

- **`tableau-migration` (skill `2.206.0` → `2.207.0`): the run now DETECTS the machine condition that
  makes its own output unopenable, instead of only documenting it.** `2.206.0` wrote the
  Newtonsoft/GAC blocker into troubleshooting; this makes the tool say it, unprompted, at the moment
  it hands the work over.

  `environment_preflight.py` reads the GAC (directory names only — it opens no file) and reports when
  an outdated `Newtonsoft.Json` is registered. .NET binds a GAC assembly ahead of the copy an
  application ships, so Power BI Desktop then fails to open **any** `.pbip`, including a brand-new
  blank one. Surfaced as an additive `report.json` → `environment.findings` and as a loud
  `summary.md` banner placed **above** the pending-gates section, because a user who reads it *after*
  opening the output has already spent the time it exists to save. Also runnable on demand:
  `py -3.11 scripts/environment_preflight.py`.

  **It detects and instructs. It deliberately never repairs, and that boundary is the design.** The
  GAC is machine-wide — registering or removing an assembly there changes binding for *every* .NET
  application on the box, not just Desktop. The remedy needs administrator elevation, and it needs
  `gacutil.exe` from the **Windows SDK**, which is not installed by default and was absent from the
  machine this was written on. Most decisively: there is no repro machine for this collection, so an
  automatic fix would be **untested surgery on someone else's environment** — the one thing this
  codebase has been consistently unwilling to ship. Detection *is* testable, because the GAC layout
  is a directory naming convention that can be injected; nine tests do exactly that, on a machine
  with no `Newtonsoft.Json` registered at all.

  `test_module_never_writes` is the load-bearing one: it asserts the module contains no mutation,
  elevation or process-execution primitive, and that its only filesystem call is a directory listing.
  That is what stops a future well-meaning change from quietly turning a read-only diagnosis into a
  machine mutation. (Its first version was too crude and flagged the module's own *remedy text* —
  a test asserting a convention rather than a behaviour, corrected before landing.)

  Corpus: 29/29, definition of done unchanged, and the only file that differs from the previous build
  is `report.json` carrying the new additive key. On a healthy machine the finding list is empty and
  the banner does not render.

- **`tableau-migration` (skill `2.205.0` → `2.206.0`): the blockers a real customer trial actually hit
  are documented, and two verification rules are corrected.**

  **The `.pbip` would not open — including a blank one.** Power BI Desktop failed with
  `Method not found: 'Void Newtonsoft.Json.JsonSerializerSettings..ctor'` because an old
  `Newtonsoft.Json` was registered in the machine's GAC. **Nothing about the migration is involved**,
  which is exactly why it wasted time: it presents as "the migrated file is broken". **Two of three
  users in one customer trial hit it.** `troubleshooting.md` §7 now leads with it, gives the
  one-line proof that it is environmental (open a *blank* `.pbip` — if that fails too, it is the
  machine), and the `gacutil -i` remedy with the assembly path.

  Two more from the same trial, both previously undocumented from the user's point of view:

  - **a table whose query is `#table(type table [], {})` and has no data** — the scaffold partition,
    most often a published/shared Tableau datasource (`sqlproxy`) that carries no query of its own.
    Disclosed by the run under `needs_review`, but a user staring at an empty table had nowhere to
    look it up.
  - **an ODBC table that will not load until the connection string is edited** — the connection
    arrives with Tableau's own driver string; replacing the first `Odbc.Query` parameter with a
    resolvable DSN loads the data, and the SQL text carries over intact.

  Section 7's menu entry now says *".pbip won't OPEN at all"*, because that is what the user types.

  **Two verification rules corrected**, both earned by the parallel session probing this collection's
  own work:

  - **Prove a gate CAN FAIL, not merely that it runs.** The `2.205.0` anchor gate ran on every commit,
    reported clean, and could never fail — its reachable set came from `git rev-list --all`, which
    includes `refs/tags`, so every anchor vouched for itself. The general form is not git-specific:
    **any check whose input set is defined by something the artifact under test contributes to is
    tautological by construction.**
  - **The fourth control is opportunistic, not a method.** The within-build control (two views
    identical but for the encoding under test) is the strongest available — but it was *noticed*, not
    designed: a tutorial workbook happened to ship `Challenge` and `Solution` pages over the same
    data. Documented as *look for one, do not assume one exists*, since promising it as a technique
    would send someone hunting for a control that is not there.

  Documentation only — no engine code, no emitted-output change.

- **`tableau-migration` (skill `2.196.0` → `2.205.0`): rollback anchors are gated — the last artifact
  in the release ritual with nothing checking it.** Proposed by the parallel colour session after two
  of their own anchors were found pointing at commits from a discarded renumber that no branch
  contained.

  The ritual has four artifacts and had gates on three: the CHANGELOG chain is tested, the `VERSION`
  stamp is tested, mirror parity is tested — and the anchor was a tag someone remembered to move.

  **An orphaned anchor is worse than a missing one, because it looks usable.**
  `git reset --hard rollback/pre-vX.Y.Z` *succeeds* against a tag pointing at an unreachable commit,
  lands you on a detached orphan, and nothing warns you. A missing anchor fails loudly and sends you
  to find the right commit.

  **The invariant is *reachable*, not *on main*.** An anchor cut for work still in flight legitimately
  points at a commit that has not merged — that is the normal state for the life of a branch, and
  asserting "ancestor of main" would fail every anchor between cutting it and landing the release.
  What can never be legitimate is a commit no ref reaches at all.

  **The obvious implementation is tautological, and the first one shipped here was.** `git rev-list
  --all` includes `refs/tags`, so every anchor makes its own target reachable and the check can never
  fail — verified by probing it with a deliberately-orphaned anchor, which it passed. Corrected to
  `--branches --remotes`, then re-probed: it now names the orphan and its short SHA. Also batched
  from 245 `git rev-parse` calls to a single `git show-ref --tags -d`, cutting the runtime from **39s
  to 2s** — almost all of it Windows process-spawn cost.

  A second test pins anchor naming, and it was corrected too: the first version forbade a trailing
  label and failed on real history (`rollback/pre-v1.9.0-comparison`) — the test inventing a
  convention rather than checking one. Now permits an optional `-<label>`.

  Audited on adoption: **245 local and 243 pushed anchors, all reachable, zero problems.**

- **`tableau-migration` (skill `2.195.0` → `2.196.0`): a dangling `SelectRef` fails the definition
  of done, and `pbir_lint` owns which findings are fatal.** R8 detects a formatting property pointing
  at a projection the visual does not declare: the property resolves to nothing, the visual renders
  with its DEFAULT colours, `powerbi-report-author validate` returns zero errors and the run reports
  success. That is the validate-clean/render-wrong class, so it must fail LOUD rather than be
  softened to a fidelity warning.

  * **Sequenced deliberately, and only possible now.** R8 shipped in `2.176.0`, but `lint_pbir_parts`
    was reached by 0 of 29 workbooks during an actual migration until `2.190.0` wired it -- so until
    then the rule guarded the test suite, not the estate path. This escalates only now that it both
    RUNS and is measured green: 0 of 29 workbooks report a lint problem, so it is inert on green and
    can only fire on a real regression. Same order as the `2.154.0` dangling-binding escalation: fix
    what fires, prove zero, THEN escalate.

  * **Which findings are fatal is owned by `pbir_lint`, not decided by the consumer.**
    `SILENT_RENDER_FINDINGS` is exported and R8 stamps its own signature into the message it emits,
    so the roster and the text cannot drift apart. Deciding it in `migrate_estate` by matching prose
    would be a proxy for "which rule fired" -- the same proxy-versus-artifact mistake `REQUIRED_ROLES`
    exists to avoid. A rule added to the roster becomes fatal with no change on the other side.

  * **Escalation ORDER is pinned by test.** An unopenable model and a dangling model binding both
    still outrank the new check, and a page-less report is still reported when the lint is clean --
    a gate that silently stops evaluating is the defect this whole line keeps re-finding.

  * **Proved by injecting the real defect into a REAL emitted visual, not a fixture.** Control: the
    unmodified corpus output of `0070_new_max` yields 0 lint problems and no failure. Injection:
    renaming the declared projection on that same visual so its `SelectRef` no longer resolves
    yields 1 problem and a `failed` definition of done naming the file and the reference. A gate
    proved only against a synthetic fixture repeats the `#137` mistake -- a test asserting a signal
    whose production never happens.

- **`tableau-migration` (skill `2.190.0` → `2.195.0`): a colour twin the shipped report does not
  reference is retired.** A colour twin is a hex-returning model measure the report binds through
  Field-value conditional formatting (rung 3). Rungs 1 and 4 -- a native `Conditional`, and a
  declared Visual Calculation -- paint the same encoding while referencing NO model object, so
  wherever one of them wins the twin is dead weight in the model and a stray entry in Desktop's
  field list. Measured before building anything: **all 6 colour twins in the corpus were referenced
  by nothing.**

  * **KEYED ON THE EMITTED ARTIFACT, and that is the third form this decision took.** (1) A PROXY --
    re-derive "would a rule win?" in the model build: two predicates that must agree forever, which
    is the assemble/emit divergence class. (2) A SHARED FACT -- the report emits its decision, the
    model reads it: correct in principle and **measured inert**, because the report→model channel
    runs off the FIRST viz pass, which carries facts true of the SOURCE (a worksheet's palette is in
    the IR before anything binds) and cannot carry facts true of the OUTPUT (which rung wins is
    decided at emit time). Instrumented on `0070_new_max`: 3 candidate records, ZERO colour facts.
    That version was built, measured, and reverted rather than shipped, because it failed CLOSED --
    a tested, plumbed-through feature that never fires reads as "implemented". (3) THIS -- ask the
    shipped bytes: retire a twin iff its name appears nowhere in the emitted report. No predicate at
    all, so nothing can disagree, and it self-corrects if a future rung changes what it references.

  * **Fail-closed in every direction.** A twin survives if the report references it, if any other
    model measure references it as `[name]`, if the name over-matches (substring test, so ambiguity
    keeps it), or if there is no report to ask -- absence of evidence is not evidence of absence,
    and stripping every twin when report emission produced nothing is exactly the wrong failure.
    That last case was a real fail-open in the first draft, caught by its own test.

  * **Runs before anything is written, and the cache concern does not apply.** Measured across a
    fresh corpus build: **0 `cache.abf` files, 0 `.pbi` directories**. There is no cache at build
    time to invalidate; that hazard belongs to a later lifecycle stage, after a human or a refresh
    script has persisted one.

  * **Corpus, operands named.** `corpus/b195` (built from `6ada43e`) vs `corpus/v195` (built from
    this commit): 1378 files vs 1378, 0 added, 0 removed, **4 differing** -- the three
    `_Measures.tmdl` that carried twins, plus `report.json` (which now discloses
    `colour_twins_retired`). No `visual.json` changed. Colour twins corpus-wide **6 → 0**, and
    dangling measure references stayed **0**.

  * **Render-verified, because TMDL text surgery can produce a file Desktop refuses to open.**
    `0070_new_max` rebuilt with 2 of its 5 measures retired, opened COLD, refreshed (9,426 rows,
    persisted): the model opens, and the rung-4 colouring still paints. Both failure modes are
    distinguishable -- invalid TMDL would not open at all, and an over-pruned twin would render in
    the default blue the workbook's own `Challenge` control page shows.

- **`tableau-migration` (skill `2.189.0` → `2.190.0`): the PBIR linter now actually runs during a
  migration — R3–R9 were inert in the estate path.** A defect in this collection's own `2.167.0` fix
  for #144, found by the per-emit-path corpus reach census and reported here rather than quietly
  corrected.

  `migrate_estate` called `lint_visual_model_bindings` and read `REQUIRED_ROLES`, but **never called
  `lint_pbir_parts`** — the entry point that applies every other rule. So unknown `visualType` (R4),
  theme-name mismatch (R3), card display units (R5), `nativeQueryRef` uniqueness (R6), empty
  `pageOrder` (R7), dangling `SelectRef` (R8) and missing required role (R9) did not run when the
  engine emitted a report. They ran in pytest against one representative workbook and nowhere else.

  That made the `2.167.0` fix incomplete **in its own terms**. #144 was *"the engine DoD cannot
  detect structurally-invalid PBIR it emits"*; the rule added to close it did not execute at emit
  time. It is the #141 shape a third time: the value of a check is decided by whether anything calls
  it. R8 had the same exposure, shipped as a permanent gate one release earlier.

  **How it was found, since the method is the reusable part.** A census instrumented 22 decision
  points and counted how many of the 29 corpus workbooks reach each. `lint_pbir_parts`,
  `_lint_required_roles` and `_lint_dangling_select_refs` all read **0 of 29**. The census's own first
  run was wrong in the opposite direction — it reported 10 zero-coverage paths including
  `select_storage_mode`, which demonstrably runs everywhere — because callers do
  `from storage_mode import select_storage_mode` and bind the name at import, so patching the
  defining module misses them. Corrected by rebinding every attribute in every loaded engine module
  that points at the function object; five false zeros resolved to 29.

  Wired fail-safe alongside its sibling, recorded on the workbook entry as an additive `viz_lint`
  key, and reported as a **warning rather than a hard failure on first wiring** — deliberately.
  These rules have never executed against real estate output, so escalating a never-executed check
  straight to a build failure is precisely the mistake the `2.154.0` sequencing note exists to
  prevent: fix what fires, prove zero, *then* escalate. Measured after wiring: **0 of 29 workbooks
  produce a lint problem**, and the definition of done is unchanged (29 bound, 0 failed, 22 warned).

  The new tests guard the **wiring**, not the rules — the rules have their own tests, and every one
  of them calls `lint_pbir_parts` directly, so none could ever catch the call site going missing.
  One additionally asserts each rule is reachable *from* the entry point, which is the same defect
  one level down.

- **`tableau-migration` (skill `2.188.0` → `2.189.0`): the measurement rules sharpened by three
  harness failures, and the concurrent-release rules sharpened by three renumbers.** Follow-up to
  `2.186.0`, from findings on both sides of a two-session integration.

  **The heuristic that makes the apparatus rules actionable** — the one that tells you *when* to
  distrust a number — is now stated first: **a result inconsistent with what the change could possibly
  do is the tell.** A report-only change cannot touch a `.tmdl`; a diff claiming 74 differing `.tmdl`
  files for one was disbelieved on that ground alone, and the harness rather than the engine turned
  out to be wrong. Three more rules join it:

  - **Build baselines; never copy them.** A copied tree is a build of a *different* commit wearing the
    right directory name, and every absolute path inside it still says so — masking substitutes the
    root you pass, so it misses all of them.
  - **A baseline opened in Power BI Desktop is no longer a pure build of its commit.** A render check
    writes `.pbi/cache.abf` into the tree and it shows up in the next diff. (Found here, landing
    someone else's change: my own render of `0060` contaminated the baseline I was measuring against.)
  - Three distinct **ways to force two hypotheses apart** now head the worked examples — vary the
    *input* and predict the output; vary the *mechanism* under fixed data; vary the *build* under
    fixed everything. The three recorded cases are one of each, which makes them a template rather
    than three anecdotes.

  **The block rule is corrected, because its shorthand produced two opposite errors in one exchange.**
  "Claim above the tip" implies a block lives or dies whole. It does not: **a block is alive only for
  its portion above `HEAD`, and erodes from below as the tip advances.** One session's range died
  entirely (all of it fell below the tip) while the other's survived with only its head consumed —
  same rule, opposite outcomes, and each party misread it in the direction that suited their own case.

  **And a new rule that removes the cause rather than the symptom: never assign a version number to
  another session.** Every collision in this run traces to a number travelling in a message — the one
  artifact neither party can keep current, since by the time it is read the sender may already have
  consumed it. Allocate only from your own block; the integrator lands what arrives and objects only
  on an actual collision.

  Documentation only — no engine code, no emitted-output change.

- **`tableau-migration` (skill `2.187.0` → `2.188.0`): rung 4 is wired -- a view-scoped colour
  driver now paints, through a declared Visual Calculation.** "Highlight the bar that set a new
  record" compares a mark against the OTHER marks in the view, so it has no rung-1 form and was
  deferred. It now emits, bound by `SelectRef`; the inline form was refuted by render (validates
  clean, paints nothing). Lands on top of `2.186.0`, never over the active trellis defect.

  * **THE EARLIER DIAGNOSIS WAS WRONG, and the stale text is corrected here.** The reverted first
    attempt was recorded as "a projection appended to the query state does not survive -- the emit
    sites build it more than once, so the mutation lands on a discarded object", and that claim
    reached a shipped CHANGELOG entry, two code comments and a test comment. Measured directly by
    appending a marker projection: it DOES survive, into both the pre-rebind and the final tree.
    `emit_pbir` does run twice, but over two output trees, each with its own state. The actual
    cause was that the append targeted a single hard-coded role -- a chart's measures live in `Y`,
    a matrix's in `Values` -- so on every visual lacking that role the append silently no-opped
    while the formatting property still emitted a `SelectRef` naming it. That is the unexplained
    "HALF the visuals". The refuting experiment took one run; the wrong explanation was an
    inference recorded in the register reserved for measurement.

  * **`_declare_colour_projection` picks a role that exists, or returns `None`.** Measure roles
    only (`Values`, `Y`, `Y2`, `X`) -- declaring a calculation in a dimension role validates clean
    and is semantically wrong. `None` means DEFER, never "emit anyway", pinned by test. The
    declaration is idempotent, so an emitter reached twice reuses the calculation instead of
    computing the same window twice in the field list.

  * **`pbir_lint` R8 caught its own author.** The first wiring in this change shipped a genuine
    dangling `SelectRef` and R8 failed the suite on it. That is the strongest available argument
    against scoping R8 down later.

  * **Proven by render, with a DISCRIMINATING probe.** The shipped semantics paint all four bars of
    `0070_new_max` orange, which is CORRECT -- sales rise monotonically, so under a running max
    every year set a record -- and proves nothing on its own, because an ignored `SelectRef` plus
    an authored orange mark colour looks identical. Two controls force the hypotheses apart. The
    workbook's own `Challenge` page (same data, same chart, no colour calc) renders BLUE. Then the
    built DAX was repointed from the running max `WINDOW(1, ABS, 0, REL)` to the whole-partition
    max `WINDOW(1, ABS, -1, ABS)` and reopened cold: exactly ONE bar (2013, the tallest) came back
    orange, three blue. That establishes per-mark evaluation, a reference that resolves, and the
    window BOUND selecting which marks paint -- and independently re-confirms that reading
    `WINDOW_MAX(x, FIRST(), 0)` as the whole partition turns "every bar that set a record" into
    "only the tallest".

  * **Corpus, with its operands named.** `corpus/b187` (built from `2.187.0`) vs `corpus/v187`
    (built from this commit): only `0070_new_max`'s two visuals differ, plus `report.json` and
    `summary.md`; no shape change, since `2.187.0` already carries the trellis fix. `SelectRef` was
    0 of 519 emitted visuals and is now 2, with **0 dangling** corpus-wide -- one entry off the
    zero-coverage roster.

### Fixed

- **`tableau-migration` (skill `2.186.0` → `2.187.0`): a measure trellis is inferred from the
  SOURCE SHELF, so a hidden projection can never be a band.** A pre-existing defect, found while
  wiring view-scoped colour and fixed first so that work cannot land on top of it.

  * **The rebuild invented panes the worksheet does not declare.** Tableau lays measures side by
    side by concatenating measure pills with `+` on one shelf, one pane per pill.
    `0060_adjustable_fixed_axis`'s `Challenge` worksheet declares exactly ONE measure pill --
    `pcto:sum:Sales:qk`, a single percent-of-total quick table calc -- so Tableau draws one pane.
    `_detect_measure_trellis` counted the projections in the query instead, which include the raw
    base measure the quick-calc path keeps as a HIDDEN projection purely so its Visual Calculation
    can reference it. Two pills were inferred from one, and the rebuild emitted TWO side-by-side
    charts, the second of them drawing a projection explicitly marked `hidden: true`. `0088` had
    the same shape.

  * **The signature is a property of the shelf, not of the query.** A projection the visual
    computes but does not show can never be one of the concatenated pills, so hidden projections
    are excluded from the count and never returned as bands. The pre-existing guards (mark type,
    `[Measure Values]`, dual axis, series split, category present) are untouched, and a genuine
    two-measure trellis is unchanged -- both pinned by test.

  * **Corpus effect is a change of visual SHAPE, stated with its operands.** `corpus/b186` (built
    from `d1c35e6`) vs `corpus/v186` (built from this commit): 1384 files vs 1378, 6 added, 12
    removed, 2 differing (`report.json`, `summary.md`). Three pages each collapse two charts into
    one, in both the pre-rebind and the final tree. pbip-tree visuals **276 → 273** -- the
    misdetection was inventing three bands.

  * **Render-confirmed, because a collapsed band looks fine in a file listing and wrong on a
    page.** `0060` reopened cold after refresh: one clustered bar chart, `Percent of Total` on a
    0-40% axis, three product categories, the hidden base measure not drawn.

### Added

- **`tableau-migration` (skill `2.185.0` → `2.186.0`): the verification rules this collection learned
  the hard way are written down, in `migration-gotchas.md`.** A new *Verifying a rebuild* section,
  prompted by the parallel colour session and stated in their words: **"verify by render — but a
  render is only evidence if the failure mode would look different."** If the broken and the working
  hypothesis predict the same picture, that picture is worth no more than a clean `validate`.

  Three measured cases are recorded, each one a render that looked like proof and was not: a
  parameter-thresholded colour that came back all one colour (equally consistent with "the parameter
  evaluated" and "the whole `Conditional` fell through to `DefaultValue`"); a view-scoped colour that
  painted all four bars orange (equally consistent with an ignored `SelectRef` plus an authored mark
  colour); and a calendar fix that was clean on every static signal while the pre-change build loaded
  a fabricated year-2000 calendar. In each case the control that forced the hypotheses apart is named.

  The same discipline is recorded for the **measuring apparatus**, because four separate incidents in
  this series were the instrument failing quietly rather than the artifact being wrong:

  - **Name both operands of a diff, never just the delta.** A whole-tree diff whose roots were built
    from the *same* tree returns `0 differing` — guaranteed, carrying zero information, and
    indistinguishable from a real result.
  - **Masking normalises representations; any value *derived* from the masked thing escapes it.** A
    path length rendered into prose survives masking of the path, so both roots must be
    **equal-length**.
  - **`added > 0` with `removed == 0` is suspicious, not a result** — the signature of `os.walk`
    truncating at `MAX_PATH` on the longer of two roots.
  - **A substitution that reports success by returning a string is not a substitution** — PowerShell
    `-replace` treats `$` as a group reference, Python `str.replace` returns the input unchanged on no
    match. Both silently no-op.

  Plus two operational traps that cost real time: a screenshot taken before a refresh is
  indistinguishable from "renders nothing", and two Desktop instances must be told apart by process
  **command line**, never `MainWindowTitle` — which is the file name, and two builds of one workbook
  share it.

  Documentation only — no engine code, no emitted-output change.

- **`tableau-migration` (skill `2.177.0` → `2.185.0`): the CHANGELOG chain gate gets the execution
  point it was missing, and the concurrent-release protocol is written down.** Both come from a
  defect the parallel colour session found in its own renumbered stack, and the diagnosis is the
  useful part: the gate shipped in `2.157.0` was never missing an invariant — it was missing a place
  to run.

  `tests/test_changelog_version_chain.py` runs where pytest runs, which is the **tip** of a branch.
  The CHANGELOG is a file every commit rewrites, so **the tip masks its own history**: a two-commit
  stack was correct at HEAD and stale one commit down, because the renumber landed as an `--amend` of
  the tip while the parent kept its pre-renumber predecessor. Checking out that parent and running the
  gate there reported the failure immediately. The fix is one flag, now in the `AGENTS.md` versioning
  ritual and in the test module's own docstring:

  ```
  git rebase --exec "cd skills/tableau-migration && py -3.11 -m pytest tests/test_changelog_version_chain.py -q" origin/main
  ```

  ~0.13s per commit, verified on both the broken stack (stops at the offending commit) and a clean one.

  Two traps are recorded with it, because each cost real time: an aborted `--exec` leaves a
  `rebase-merge` directory, and a later `git rebase --abort` then rewinds the *branch* to that stale
  state; and a CHANGELOG resolver using `str.replace(old, new, 1)` reports success by returning a
  string whether or not it matched — the same silent-no-op class as PowerShell's `-replace` treating
  `$` as a group reference, which no-opped a defect injection earlier in this series and briefly made
  a gate look like it had fired when it had not.

  **Concurrent releases now have a written protocol**, after four version collisions between two
  sessions in one day. Each session claims a contiguous block and allocates only inside it, with two
  rules that are the whole content: claim the block **above the current tip** (a block below `HEAD`'s
  version is spent, because the `VERSION` stamp must stay monotonic — which is why the earlier
  `2.165`–`2.174` claim had to be re-claimed as `2.185`–`2.194`), and on a collision **the pushed side
  wins**. The second is deliberately asymmetric: it is decidable without either party knowing the
  other's state, which is the only property that survives a race, whereas alternating who absorbs the
  cost needs shared memory of whose turn it is.

  Docs and one test docstring only — no engine code, no emitted-output change.

- **`tableau-migration` (skill `2.176.0` → `2.177.0`): a conditional-colour rule can compare
  against a Tableau PARAMETER.** "Colour it red when it is above `[Threshold]`" is the canonical
  parameter-driven colour in Tableau, and the rule declined on it: the rung-1 resolver knew
  `AGG([Field])` and a bare `[Field]`, and a `[Parameters].[X]` operand returned `None`, which
  aborts the whole rule (fail-closed). It is also the specific reason corpus `0088` declined.

  * **A what-if parameter binds to its `SELECTEDVALUE` measure, not to its picker column.** The
    model turns a value parameter into a disconnected table of CANDIDATE rows plus a scalar that
    reads the current selection. Only the scalar can stand in a comparison; binding the picker
    column instead validates clean and renders, and silently compares every mark against the whole
    domain. `_classify_parameters` therefore publishes an additive `value` `{table, measure}`
    alongside the existing `picker` `{table, column}`, `param_binding` carries a `values` map,
    and the IR carries the normalised result the same way it already carries `parameter_controls`.
    Keyed by BOTH internal name and caption, normalised through `_norm_param_key`, because a
    formula may spell `[Parameters].[X]` either way and a bracket difference at the model/viz seam
    is a silent near-miss.

  * **Fail-closed is preserved end to end.** A parameter the model never consumed, a half-built
    `value` record, and a qualified reference that is not a parameter (`[Datasource].[Field]`) each
    resolve to nothing and decline the rule, rather than guessing at a binding.

  * **Proven by render, with a DISCRIMINATING probe.** The first render was *not* evidence: every
    row's `SUM([Profit])` exceeded the threshold, so "the parameter evaluated" and "the whole
    `Conditional` silently fell through to `DefaultValue`" produced the identical all-orange
    picture -- the same validation-invisible failure already recorded for `Or` nodes and inline
    visual calculations. Re-probed with a threshold that must split the rows (`SELECTEDVALUE`
    default 100 → 100,000): Consumer (136,371) orange, Corporate (94,249) and Home Office
    (61,675) blue. Only the parameter's default changed between the two renders, and the split
    lands exactly on its value.

  * **Zero corpus coverage, by measurement.** Masked corpus diff against `main`: 1384 files vs
    1384, 0 added, 0 removed, **0 differing** -- no workbook exercises a parameter-driven colour,
    so this path rests entirely on its unit tests and the render proof. Fourth named instance of
    the corpus-coverage gap (after Custom SQL relations, `CALENDARAUTO`-on-a-stub, and
    `SelectRef`).

  * **Masking cannot reach a DERIVED SCALAR.** The previous corpus diff reported one differing
    file; the whole delta was the engine's own MAX_PATH warning reading `287 chars` vs `290`, and
    one workbook sat exactly on the 260 boundary. Masking replaces the path STRING but cannot
    touch a path LENGTH already rendered into prose as an integer. Fixed upstream of the diff by
    giving both builds output roots of EQUAL LENGTH, which took this run to 0 differing.

- **`tableau-migration` (skill `2.175.0` → `2.176.0`): a boolean colour driver is a two-member
  domain, Tableau's window bounds are honoured, and a dangling `SelectRef` is now a lint error.**
  Rung 4 (view-scoped colour via a Visual Calculation) was wired during this work and then
  **deliberately reverted** — see below. The pure pieces it needed are sound, tested, and shipped.

  * **A bare boolean expression is a colour rule.** `SUM([Sales]) = WINDOW_MAX(SUM([Sales]))` — the
    "highlight the bar that set a new max" idiom, and the commonest boolean driver in the corpus —
    is not an `IF` chain, so the compiler declined it entirely. Tableau paints exactly two swatches
    for such a pill, so it *is* a categorical encoding, written shorter. Given the declared
    `datatype == "boolean"`, it now reads as `IF <expr> THEN True ELSE False`. A boolean-declared
    *field reference* alone is still refused — it is not a predicate.
  * **Tableau's window bounds change the answer.** `WINDOW_MAX(x, FIRST(), 0)` is a **running**
    maximum; reading it as the whole partition turns *"every bar that set a new record"* into
    *"only the tallest bar"* — 4 marks versus 1 on a monotonically rising series, which is exactly
    `0070_new_max`'s shape. `FIRST()`/`LAST()`/integer offsets now lower to the matching DAX
    `WINDOW(...)` frame, and a bound that cannot be read declines rather than guessing.
  * **`pbir_lint` R8: a `SelectRef` must name a projection the same visual declares** -- and it
    is NOT colour-compiler-specific. The emitter already wrote `SelectRef` from another site
    that predates this gate (the continuous-gradient path anchors a table/matrix `backColor`
    FillRule to an outer Visual Calculation's queryRef), sharing the same hazard: one code path
    naming a projection a different code path must declare. Measured across the corpus: 519
    emitted `visual.json` files, **0** SelectRef references -- neither path fires on corpus
    input, so the exposure is real and has no regression coverage. The gate is the only thing
    standing under it. A property
    pointing at an in-visual expression that no projection carries resolves to nothing — the visual
    renders with its defaults, reports no error, and passes `validate`. `lint_visual_model_bindings`
    could never have caught it: it proves MODEL references, and a Visual Calculation is not in the
    model.

  **Why rung 4 is still not wired.** `lower_to_visual_calc` produces correct DAX, but binding it
  needs a *declared* projection, and appending one to the query state inside the colour emitter does
  not survive — the emit sites build that state more than once, so the mutation lands on an object
  that is discarded. Measured on `0070_new_max`: **half the visuals shipped a dangling `SelectRef`**,
  the same class of defect `2.152.0`/`2.154.0` exist to prevent, and every existing gate stayed
  silent. Reverted rather than shipped half-working; the projection has to be threaded to the emit
  site instead. R8 is the gate that would have caught it, and now will.

  Corpus unchanged (29/29, dangling 0, no visual drift).

- **`tableau-migration` (skill `2.167.0` → `2.175.0`): the conditional-colour compiler is WIRED —
  a string-member colour calc now paints cells and marks natively, with nothing added to the
  model.** The three preceding releases were inert by construction; this is the one that changes
  what ships.

  A Tableau calc that outputs string members and sits on Colour —
  `IF SUM([Profit]) < 0 THEN "negative" ELSE "positive" END`, or a five-branch `ELSEIF` chain, or a
  `CASE` over a dimension — is now compiled to a PBIR **Rules** `Conditional` and bound directly to
  the channel the mark implies: `values[].fontColor` / `backColor` for a matrix or table cell,
  `dataPoint.fill` for a chart mark. Both sides emit the *same* expression; only the channel differs.

  **The members never reach Power BI.** They collapse into `Value` literals, so the rebuild emits
  no synthetic string measure and no colour twin, and the result opens in Desktop's Conditional
  formatting dialog as rules a user can read and edit. Render-verified on `Logic example 4`: cells
  paint per-row blue/orange exactly as the twin version did, from a `Conditional` comparing
  `Sum(Orders[Profit])` to `0` — identical output, no model objects.

  Two things only the emitter can supply are wired here, and both reuse existing machinery rather
  than re-deriving it:

  * **leaf binding** — `AGG([Field])` and bare `[Field]` are routed through `_field_expression`, the
    same code path that projects the visual's own columns, and are matched against the worksheet's
    already-resolved fields first, so a rule can never bind a subtly different object than the
    column beside it;
  * **the palette** — authored `<map to='#hex'>` first, else Tableau's default categorical ramp in
    sorted member order, the same precedence the model colour twin uses, so a rule and a twin can
    never paint one workbook two ways.

  **The colour twin remains the fallback, and the split is by what the calc RETURNS.** A boolean
  driver (`SUM([Profit]) > 0`) has no members to collapse into a palette and keeps the hex-returning
  twin it has had since `2.127.0`. View-scoped and untranslatable drivers keep their `2.152.0`
  deferrals. A native rebuild is deliberately *not* warned about — it is faithful, not a degradation.

  Corpus: 29/29, dangling references still 0, and **no visual changed** — the corpus's own
  string-member calcs are declined correctly (one has no `ELSE`, so its domain is not closed;
  another compares against a `[Parameters].[…]` operand the resolver does not yet bind). Fail-closed
  throughout: anything the compiler cannot express falls to the rung below it, never to a partial
  rule.

- **`tableau-migration` (skill `2.166.0` → `2.167.0`): the linter catches structurally-invalid PBIR
  the engine can emit — `pbir_lint` R9, required roles.** Raised in #144 as the systemic gap that let
  #143 ship green: a run graded `definition_of_done: warn` / `0 error` / `Viz=built` over a report
  that `powerbi-report-author validate` refused with `PBIR_ROLE_REQUIRED_MISSING` and exit 1.

  The reporter framed the boundary exactly right, and R9 is the missing half of a pair that is worth
  reading together:

  - `lint_visual_model_bindings` covers *validate is blind and we can see* — a binding to a model
    object that does not exist validates clean and renders **empty**.
  - **R9** covers the reverse, *validate can see and we were blind* — a missing required role is
    **structurally invalid**, and the only tool that reported it was an opt-in npm pre-gate an
    ordinary run never reaches.

  **Their recommended option 1 was not taken, because it rests on the same premise #143 disproved.**
  "Don't drop — bind the stub" assumes a stubbed calc is what gets dropped. Measured: an emitted
  `= BLANK()` stub **binds normally**; only a reference the model did not emit *at all* is dropped,
  and there is nothing to bind in that case. Option 2 (this rule) is what shipped, as the standing
  gate behind the 2.166.0 emitter fix. Option 3 (default-on, binding `--validate`) is a separate
  defaults decision and is not taken here.

  `REQUIRED_ROLES` now lives in `pbir_lint` as the **single source of truth** and `migrate_estate`
  reads it rather than keeping a copy — two tables would drift, and a gate drifting away from the
  emitter it guards is precisely what #137 was. A test asserts both consumers read the same object.

  Scoped so that "cannot judge" never becomes "declare invalid": an unrecognised `visualType` is not
  judged; a role held by a `fieldParameters` binding counts as occupied (the rescue path builds
  exactly that); and a visual emitted with **no query at all** — the deliberate placeholder an
  emptied visual becomes — is not flagged, otherwise the 2.166.0 fix would trip the 2.167.0 gate and
  the two would deadlock.

  **Two existing test fixtures were structurally invalid and are corrected, not the assertions.**
  R9 flagged a `clusteredColumnChart` with no `Y` and a `barChart` with no `Y` in
  `test_pbir_lint.py`; both were incidental to what those tests assert. The fixtures now carry a
  known-good `Y` that resolves cleanly, so **every assertion is unchanged** — no test was weakened to
  accommodate the rule. A malformed-input test also caught a real robustness bug in the new rule (a
  non-dict `query` raised), now hardened.

  Corpus: 29/29, definition of done unchanged, **R9 fires on 0 of 29 workbooks**, and a masked
  whole-tree diff against the previous build is **empty** (1384 files vs 1384, 0 differing).

- **`tableau-migration` (skill `2.165.0` → `2.166.0`): a visual that loses a required role is
  emptied, not shipped as structurally invalid PBIR.** Raised in #143. The symptom is real and
  reproduced: `powerbi-report-author validate` fails pristine engine output with
  `PBIR_ROLE_REQUIRED_MISSING` for a `clusteredColumnChart` carrying `Category` and no `Y`.

  **The reported cause is not the mechanism, and that changes which fix is available.** The issue
  states the rule as *"when a calc falls back to a stub, the engine drops its projection from the
  visual instead of binding it"*. Measured against `_crosscheck_report_refs`: an emitted stub —
  `measure 'Regional Revenue (FIXED)' = BLANK()` — **binds normally and is not dropped**. Only a
  reference the model did not emit *at all* is dropped. So the reporter's preferred fix, "bind the
  stub measure into the required role", cannot apply on this path: a projection reaches the drop
  branch precisely when there is nothing in the model to bind it to. (Why their measure was absent
  rather than stubbed is not determined here — their run used the datasource-first split, so the
  measure may live in the standalone `.SemanticModel` rather than the parts this cross-check
  compares against. Worth a follow-up with the artifacts.)

  What *is* general is the **outcome**, and that is what is fixed. Dropping a projection deletes its
  role, and a visual was emptied to a placeholder only when it lost **every** role (`emptied = not
  qs`). Losing just one *required* role left a partial `queryState` — valid JSON, invalid PBIR,
  broken in Desktop. This is the reporter's own second option ("drop the whole visual and record it
  as dropped — lossier, but still valid PBIR"), applied wherever a required role goes missing
  regardless of cause.

  `_REQUIRED_ROLES` is **harvested** from `powerbi-report-author catalog describe` (v0.1.4) — the
  same tool that raises the diagnostic — across all 38 visual types that declare required roles, so
  it encodes what the validator enforces rather than what we believe it enforces. An unrecognised
  `visualType` is never emptied on this account: "cannot judge" must not become "delete it".

  **Narrowed after an over-fire that an existing test caught.** The first version emptied any visual
  left missing a required role, which regressed
  `test_field_parameter_on_a_chart_axis_is_expanded` — a field-parameter axis expansion legitimately
  builds that shape. The guard now fires only when **this pass** is what emptied the role: a visual
  that arrived incomplete for unrelated reasons is left exactly as before. That narrowing is itself
  pinned by a test, and the pre-existing test was not weakened to accommodate the change.

  Corpus: 29/29, definition of done unchanged, and a masked whole-tree diff against the previous
  build is **empty** (1384 files vs 1384, 0 differing) — no corpus visual loses a required role this
  way, so the guard is inert on known-good input and lives only where the defect lives.

- **`tableau-migration` (skill `2.162.0` → `2.165.0`): Tableau's cross-project caption suffix no
  longer makes a published datasource unmatchable.** Raised in #145. Tableau appends
  `" | Project : <name>"` to a published-datasource caption when the name alone would be ambiguous
  across projects. `_norm_ds` strips punctuation and case but not **words**, so the suffix survived
  the alphanumeric squeeze as text:

  ```
  DS_Tail_Level                                    -> dstaillevel
  DS_Tail_Level | Project : Enterprise Dashboards  -> dstaillevelprojectenterprisedashboards
  ```

  Those can never be equal, so an estate build **silently skipped** workbooks with *"published-datasource
  workbook — co-migrate its published datasource"* while the datasource each one needed had migrated
  successfully in that same run. Measured in the field at **4 of 12 workbooks**.

  The strip lives **inside `_norm_ds`**, not at the lookup site, so both sides of every comparison
  are normalised identically — stripping only where the lookup happens would have reintroduced the
  exact asymmetry #138 was, one layer up. The pattern is anchored to the end and requires the `|`
  delimiter, so a name that merely contains the word "project" (`Project Apollo`,
  `Capital Projects 2026`) is untouched, and it tolerates the server's spacing variants.

  **Safe against the ambiguity the suffix exists to encode.** Two same-named datasources in different
  projects now collapse to one key — which is correct *only* because the catalog already records a
  contested key as `_AMBIGUOUS_CATALOG_ENTRY` and the lookup treats that as a miss. So such a workbook
  is skipped with an honest reason rather than bound to whichever migrated last, which would attach a
  wrong-schema model that renders perfectly. A test pins that failure-closed behaviour rather than
  assuming it.

  Corpus: 29/29, and a masked whole-tree diff against the previous build is **empty** (1384 files vs
  1384, 0 differing) — no corpus caption carries the suffix, so this is inert on known-good input.

- **`tableau-migration` (skill `2.160.0` → `2.162.0`): a self-check that evaluated nothing no longer
  reports an affirmative pass.** Raised in #141, and the reporter did two unusually careful things
  worth recording: they explicitly withdrew the larger change I had declined on #133 rather than
  re-litigating it, and they asked me to spend a minute confirming on the corpus rather than let them
  claim a result they had not run. Both were right calls.

  `endpoints_distinct` has **three** ways of not running, and only two were detectable. A caller
  supplying no expected count, or a count of `1`, leaves the key **absent** — recoverable by an
  operator who knows to look. But entering the branch and resolving no parameter groups still wrote
  `endpoints_distinct: true`, so *"evaluated, model is clean"* and *"could not evaluate anything"*
  were indistinguishable. Since this check's own failure text is *"this model refreshes successfully
  and returns wrong data"*, overstating how often it ran is the worst available direction to be wrong.

  **Confirmed on the corpus, and the answer is worse than the report predicted.** The reporter
  expected the three flat-file multi-datasource workbooks named in my own code comment. Measured:
  those three do report an affirmative pass having read nothing — they emit **no
  `definition/expressions.tmdl` at all**, so the regex scans an empty string. But the aggregate is
  starker. Of 29 corpus workbooks, `endpoints_distinct` **genuinely evaluated on zero**: 26 never
  enter the branch (single upstream) and 3 enter it and resolve nothing. The operator question the
  issue poses — *"on how many of my models did the collapse check actually evaluate anything?"* — had
  no answer before, and on this corpus the honest answer is **none of them**.

  **The flat-file exemption is correct and untouched.** An island reaching its source through a
  literal `File.Contents(...)` path legitimately declares zero parameter groups, and reading that as
  "collapsed to zero endpoints" would be a false positive. Only the reporting of the non-answer
  changed.

  Implemented as the reporter's **option 2** — an additive sibling key — because that is also what
  the repo's own schema contract requires: report changes add keys, they never rename or remove
  them. Their option 1 changes the value's type and option 3 removes the key in case 3, which would
  silently change what an *absent* key means for anything already reading the payload. `ok`,
  `checks` and `issues` are untouched, and the corpus confirms it: **the `checks` dict changed on 0
  of 29 workbooks** and the definition of done is identical (29 bound, 0 failed, 23 warned).

  Entries follow the same shape as `issues` (`check` + prose) rather than the flat list suggested,
  for consistency with the existing payload; the flat roster an aggregate wants is
  `[e["check"] for e in selfcheck["not_evaluated"]]`, and a test pins that. The guard that matters
  most is `test_a_genuine_collapse_still_fails_and_is_not_excused`: the whole risk of this change is
  muting a true failure by routing it through the new key, so a real collapse must still fail, still
  raise its issue, and still be absent from `not_evaluated`.

- **`tableau-migration` (skill `2.159.0` → `2.160.0`): on-prem Tableau Server is stated as supported
  and partly gated, instead of working by accident.** Raised in #140, where the reporter did the
  unusual and useful thing of correcting their own field report before filing — the help text is not
  literally Cloud-only, it already offers a generic `https://host` form. Their real question was the
  one worth answering: *is Server deliberately in the test matrix, or does it merely work?*

  **The honest answer was "supported by construction, but untested", and it is now written down.**
  The module docstring states it plainly rather than reassuringly: no test exercised a Server host,
  so nothing would have caught an on-prem regression; a successful live on-prem run exists but is a
  field report, not a gate.

  **Their instinct about where Server and Cloud diverge was exactly right, and the finding is worse
  than the docs implied.** They flagged API-version negotiation as the likely divergence, "since
  on-prem Servers can run substantially older REST API versions than Cloud ever does". Measured:
  **there is no negotiation at all.** `fetch_tds.DEFAULT_REST_VERSION` pins `3.24` and no `serverinfo`
  call is ever made, so the version is never discovered from the host. The mitigation is explicit
  rather than automatic — `--rest-version` is already a first-class flag on both scripts — and that is
  now the documented first thing to try when sign-in fails against an older Server.

  Part of the gap is closed rather than only described. `test_onprem_server_support.py` covers what is
  genuinely checkable offline — on-prem host shapes (bare host, explicit scheme, trailing slash, and
  plain `http` on a non-default port, which an internal Server frequently is), identical REST URL
  construction for Cloud and Server, and that `--rest-version` actually reaches the URL, because a
  documented flag that silently did nothing would send a user to debug auth instead. It does **not**
  claim a live on-prem round trip. One test additionally fails if a `serverinfo` call ever appears,
  so the docstring's claim about itself cannot quietly become untrue.

  Docs: the `--server` help now names both forms (`10ay.online.tableau.com` (Cloud) /
  `https://tableau.example.com` (Server)). The session-expiry comment kept its Cloud provenance
  rather than being generalised away — the *handling* is not Cloud-specific, but the measurement was
  ("intermittently after 1 to 58 calls", on Cloud), and whether an on-prem Server expires on the same
  cadence is unknown. Recording which half is measured is more useful than a tidier sentence.

- **`tableau-migration` (skill `2.158.0` → `2.159.0`): datasource selection normalises both sides, so
  a caption with incidental whitespace can be selected at all.** Raised in #138 from a live customer
  estate whose real caption was `'DS_Visitor _Device '` — note the trailing space. `_choose_datasource`
  stripped the **requested** name but not the **candidate** labels, and an asymmetric normalisation
  can never match, so the datasource was unselectable however correctly it was spelled and the
  workbook was skipped outright.

  **It broke this module's own documented contract, which is sharper than the filed report.**
  `workbook_datasources()` returns `label` and documents it as *"the value to pass back as
  `select=`"*. Measured: handing that exact string straight back was **rejected**, and because both
  sides print through `repr` the failure read

  ```
  no datasource named 'DS_Visitor _Device ' in this workbook; available: 'DS_Visitor _Device '
  ```

  — two **byte-identical** strings, one of which is reported not to exist. So this was never really
  about a user mistyping a name: an agent following the API exactly could not select the datasource,
  and the reporter's workaround was to edit the customer's own `.twbx` to get past it.

  Fixed by stripping the candidate labels too. A near-miss hint is added for what symmetric stripping
  deliberately still refuses: when the request matches a candidate only after **internal** whitespace
  is collapsed, the error names the closest candidate and says how it differs. That is reporting
  only — `_squash_ws` is unreachable from the matching path, because collapsing internal whitespace
  could make two genuinely distinct captions identical and selecting one of those would be a guess.

  **The reporter's two follow-up questions, answered with measurements rather than opinion.**
  *Should whitespace be normalised at parse time instead, since captions presumably flow into table
  names and emitted identifiers?* Measured: no. `parse_tds` names the descriptor from the datasource's
  internal `name` (`federated.abc`), not its caption, and no descriptor value carries the untrimmed
  string — so the emitted model never sees it, and a parse-time strip would silently rename the
  author's object for no benefit. *Does the same asymmetry exist elsewhere?* `_choose_datasource` is
  the only selection site. One **latent** instance does exist — `build_m_field_resolver` stores
  `source_datasource` raw while `assemble_model` strips it when grouping islands, and a stripped probe
  against a raw tag loses island scoping entirely (the calc drops to a stub). It is **not live**: the
  island tags and the calcs' `datasource` values both derive from the same raw caption, so the
  comparison is raw-vs-raw in production. Deliberately left alone rather than "fixed", since
  normalising there could collapse two captions that differ only in whitespace into a single island.

- **`tableau-migration` (skill `2.157.0` → `2.158.0`): the view-scoped rung of the conditional-colour
  back end — lowering to a Visual Calculation.** Still unwired (nothing calls it, emitted output
  unchanged). This is the rung with no alternative: when a predicate needs a value that does not
  exist until the visual is evaluated — *"the lowest of the displayed bars"*, *"the 90th percentile
  of what is on screen"* — no model measure can serve it. `2.152.0` measured why: the model-measure
  form of a `WINDOW` comparison orders by a row-level column while the visual's axis is a grouped
  one, so it is false on **every** mark.

  `lower_to_visual_calc(spec, palette, resolve)` emits the DAX of a hex-returning Visual Calculation
  — Microsoft's own documented mechanism for driving conditional formatting — as one nested `IF` per
  branch in authored order, so the calculation reads alongside the Tableau formula it came from.
  Tableau's view-scoped vocabulary is rewritten to DAX window functions:

  | Tableau | DAX |
  |---|---|
  | `WINDOW_MIN/MAX/SUM/AVG/MEDIAN(x)`, `TOTAL(x)` | `MINX/MAXX/SUMX/AVERAGEX/MEDIANX(WINDOW(1, ABS, -1, ABS), x)` |
  | `RUNNING_*(x)` | same aggregators over `WINDOW(1, ABS, 0, REL)` — first-row-to-current, a *different frame* |
  | `WINDOW_PERCENTILE(x, p)` | `PERCENTILEX.INC(WINDOW(1, ABS, -1, ABS), x, p)` |
  | `RANK(x)` | `RANK(DENSE, ORDERBY(x, DESC))` |
  | `INDEX()` / `SIZE()` | `ROWNUMBER()` / `COUNTROWS(WINDOW(1, ABS, -1, ABS))` |

  Operands are the visual's **projected column names** (`[Sum of Profit]`), not model measures,
  because a Visual Calculation addresses the visual's own matrix — the caller supplies that naming.
  Unlike rung 1 this keeps `||` for disjunction: DAX has a working OR, so no DNF expansion is needed
  here. The DAX only is returned, not the projection, keeping PBIR assembly with the emitter.

  Two shapes were **refuted by render** and are therefore never emitted, both passing `validate`
  with 0 errors: a `NativeVisualCalculation` placed *inline* in a formatting property (silently
  ignored — it must be a declared, hidden projection referenced by `SelectRef`), and an `Or` node.

  Fail-closed and all-or-nothing, as rung 1: an unsupported spec, an open member domain, a missing
  palette entry, or any operand the resolver cannot bind returns `None` — never a calculation with a
  hole in it. The two rungs deliberately overlap on aggregate-scope predicates; the router (next)
  prefers rung 1 because it adds nothing to the model.

- **`tableau-migration` (skill `2.156.0` → `2.157.0`): the CHANGELOG's declared version chain is a
  gate, not a habit.** Two agents working in parallel shipped the *same* defect within an hour, on
  the same rebase, and neither was careless: a cross-session rebase merges the `VERSION` stamp
  cleanly while the **prose describing that stamp** goes stale silently. Both wrote an entry reading
  `(skill X → Y)` that was true when written and false the moment the other session's commits landed
  underneath and changed the actual predecessor. Nothing checked it, so only a human comparing two
  numbers would ever have caught it.

  It is mechanically checkable from the CHANGELOG alone, so it is now checked — three invariants,
  each pinned to a real failure rather than a tidiness preference:

  - **the chain is continuous** — each entry's declared predecessor equals the successor declared by
    the entry beneath it, reading newest-first. Deliberately heading-agnostic: the file interleaves
    `### Added` and `### Fixed`, and a release's category says nothing about its ordering.
  - **no version is produced twice** — the other half of the same rebase hazard, where an entry is
    renumbered without being renamed.
  - **the newest entry matches the shipped `VERSION`** — the one that actually reaches users. The
    self-update runbook compares installed `VERSION` against the raw `VERSION` on `main` and
    reinstalls only when main is **newer**, so a CHANGELOG documenting a version above a lower stamp
    leaves every client that reached the higher number permanently deaf to everything after it.

  Verified by injecting the exact defect both agents shipped into the real file — a top entry
  declaring predecessor `2.154.0` above an entry ending `2.155.0` — and confirming the gate names
  both line numbers and both versions, rather than only proving it against a synthetic fixture.

  **One pre-existing break fixed, measured first.** Running the check over the full history (70
  entries, `2.87.0` → `2.156.0`) found the version *set* already complete — no gaps, no duplicates —
  but three adjacent chain breaks, all caused by a single displaced entry: `2.141.0 → 2.142.0` (the
  caption-padding fix) sat below `2.139.0 → 2.140.0` because it was authored before a parallel merge
  and landed after it. Relocating that one entry to its chronological position resolves all three,
  so the gate ships with **zero exemptions** — nothing is grandfathered and no history was rewritten
  beyond moving the block.

- **`tableau-migration` (skill `2.155.0` → `2.156.0`): the conditional-colour compiler back end —
  lowering to Power BI's own "Rules" conditional formatting.** Still unwired (no emitter calls it
  yet, output byte-for-byte unchanged), but this is the rung that makes the rebuild *native* rather
  than synthesised.

  `lower_to_conditional(spec, palette, resolve)` turns an analysed colour calculation into a PBIR
  `Conditional{Cases, DefaultValue}` expression — the JSON behind Desktop's **Rules** format style.
  The Tableau string members never reach Power BI at all: they collapse into `Value` literals, so
  the rebuild adds **nothing to the model** — no string measure, no colour twin — and opens in the
  Conditional formatting dialog as rules a user can edit. Today the same encoding costs two
  synthetic measures and is unreachable from the UI.

  Every shape it emits was render-verified against Desktop first, because all of them pass
  `powerbi-report-author validate` when they are wrong:

  * `Comparison` × 5 kinds, measure-vs-literal, measure-vs-measure;
  * `Arithmetic` inside a comparison (`SUM([Discount]) * 200 > SUM([Profit])`);
  * `And`, `Not`;
  * N ordered `Cases` + `DefaultValue`, which is what makes arbitrarily nested `IF`/`ELSEIF` free.

  **Disjunction is never emitted as a node.** `{"Or": …}` is silently ignored by Power BI — the
  whole `Conditional` falls through to its default, with a clean validation pass. Since each `Case`
  already maps one predicate to one colour, `a OR b -> "X"` is simply two `Cases` both yielding
  `colour("X")`, so predicates are normalised to **DNF** and one `Case` is emitted per disjunct.

  Fail-closed and all-or-nothing: an unsupported spec, an open member domain (members that are
  *data*, not literals), a missing palette entry, or any operand the caller's resolver cannot bind
  returns `None` — never a rule with a hole in it. A caller that gets `None` falls to the next rung.

- **`tableau-migration` (skill `2.154.0` → `2.155.0`): the calendar-span stub exclusion is keyed on
  the emitted artifact, so it actually fires.** Raised in #137 as a follow-up to #134, reproduced
  exactly as filed: `m_partition_review_reason()` returns a reason ("this is a scaffold") while
  `_stub_backed_tables()` returns an empty set — the emitter and the calendar gate disagreeing about
  the same relation in the same build. The reporter's diagnosis was correct: `stub_partition` had
  exactly two occurrences in the tree (one `.get()` read and one hand-set *test fixture*), and
  `placeholder` was assigned nowhere, so the predicate silently collapsed to its rarer zero-column
  branch and the fabricated year-2000 calendar was still emitted for the column-declaring stub that
  motivated #134. The zero-column branch was verified intact (the reporter listed it as unverified).

  **The suggested one-line fix was not taken, because it is over-broad and the corpus cannot show
  it.** Stamping `stub_partition` whenever `m_partition_review_reason()` returns a reason conflates
  *needs review* with *is a stub*. Measured: a surviving Tableau parameter in Custom SQL is a review
  reason on a partition that still emits a real `Odbc.Query` (generic ODBC) or `Value.NativeQuery`
  (SQL Server family) and **does** carry rows. Stamping those would drop a populated fact table out
  of the calendar span and silently narrow the model's date range — the same class of harm #134
  fixed, in the opposite direction. `connection_to_m.py`'s own docstring already says as much: a
  surviving parameter reference "is a needs-review reason (the partition is still emitted)".

  The discriminator is therefore *"the emitted partition **is** the scaffold"*, answered by
  inspecting the TMDL that actually ships for the `#table(type table [], {})` literal — the same
  string `openability_gate._STUB_PARTITION_RE` already matches. The **detection** gate
  (`eager_calc_refs_resolve`, #134) and the **prevention** gate (`_stub_backed_tables`, #137) now
  share one definition of "stub" and are pinned together by a test, since drift between them *is*
  this defect. Both of the reporter's structural points were adopted: the stamp is applied from the
  assembly loop (not `_scaffold_source()`, which has no relation in scope and 9 call sites), and the
  tests assert through the emitter rather than a hand-set flag — a test that supplies a signal
  production never emits is what let this survive in the first place.

  Also recorded, because it explains why this had to be found in the field: **the 29-workbook corpus
  contains zero Custom SQL relations** (relation types are `table` ×168, `join` ×98, `collection` ×3;
  no `<relation type='text'>` anywhere, confirmed by XML parse and by raw byte scan). The whole
  Custom SQL path rests on synthetic-descriptor unit tests, which is precisely the condition that
  produced this bug.

  **Corpus effect, measured and Desktop-verified.** 29/29 still build and the definition of done is
  unchanged (29 bound, 0 failed, 23 warned). A masked whole-tree diff against the previous build
  (masking the full absolute output root — never a bare token — plus timestamps and lineage GUIDs)
  shows exactly **one** file changed: `0083_previous_workday`'s `Date.tmdl`, whose only data table
  *is* a stub. Its calendar goes from `CALENDAR(DATE(YEAR(MIN(stub[Date])), 1, 1), …)` to the
  documented `CALENDARAUTO()` fallback, since excluding the stub empties the span. Verified by cold
  Desktop open of both builds, identified by process command line rather than by "the instance that
  is running": the pre-change model loads a **fabricated year-2000 calendar** (`Date` starts
  `1/1/2000`) — the exact artifact #134/#137 exist to eliminate — while the post-change model opens
  cleanly with an empty calendar that will derive correctly once the stub is completed and
  refreshed. `EVALUATE TOPN(1, 'Date')` *executed* rather than erroring, which is what rules out the
  risk that `CALENDARAUTO()` over a rows-less column stops the file opening.

- **`tableau-migration` (skill `2.153.0` → `2.154.0`): a visual bound to a model object that does
  not exist now FAILS the definition of done.** `lint_visual_model_bindings` has always *detected*
  this — the report records `viz_dangling_bindings` and names each offender exactly — but it was
  softened to a fidelity warning, so a workbook could ship with a visual bound to nothing and still
  be reported as done. The visual renders EMPTY (or, for a conditional format, silently unpainted)
  and `powerbi-report-author validate` returns 0 errors, which is precisely why a soft signal was
  the wrong strength.

  **Sequenced deliberately.** Landing this before `2.152.0` would have taken the corpus gate from
  29/29 to 26/29 and left every later run measuring against a red baseline: 4 dangling references
  across 3 of the 29 workbooks (`0070_new_max` ×2, `0080_calculate_slope_and_residuals`,
  `0081_correlation_r_squared`), all reporting `built`. Measured after `2.152.0`: **0**. So the
  escalation is inert on green — corpus `29/29 bound, 0 failed, 23 warned`, byte-identical to the
  run before it — and can fire only on a real regression.

  Ordering guard included: the dangling check must not short-circuit the page-less check that
  follows it (it did, on first write, and a test now pins it).

- **`tableau-migration` (skill `2.152.0` → `2.153.0`): the conditional-colour compiler front end.**
  Analysis only — nothing is wired to an emitter yet, so output is byte-for-byte unchanged. This is
  the piece that makes conditional colour *general* rather than a set of recognised templates.

  Tableau's commonest conditional-formatting idiom is a calculation that outputs a **string /
  dimension value**, placed on Colour, with each output painted its own colour. The structural
  insight the new `scripts/colour_rules.py` is built on is that **the string output is an
  intermediate Power BI never needs**:

  ```
  IF p1 THEN "A" ELSEIF p2 THEN "B" ELSE "C" END
      ==>  Cases = [ p1 -> colour("A"), p2 -> colour("B") ],  Default = colour("C")
  ```

  The members collapse into the palette — no string measure, no colour twin, nothing added to the
  model. All the difficulty therefore lives in the **predicates**, and depth costs nothing because
  `Conditional.Cases` is an ordered list a nested `IF`/`ELSEIF` chain flattens onto 1:1.

  `analyse_colour_calc(formula)` answers the three questions that decide the rebuild, from the
  formula's own **properties** rather than by matching templates — so a calculation the engine has
  never seen still routes:

  1. **What are the branches?** Ordered `(predicate, member)` pairs plus a default, flattened out of
     arbitrarily nested `IF`/`ELSEIF` and `CASE`/`WHEN` forms. A `CASE <subject> WHEN <v>` arm is
     normalised to the predicate `<subject> = <v>`, so downstream code sees one predicate shape
     whichever surface form it came from. Structural depth counts `IF`/`CASE`…`END` as well as
     brackets, so a conditional nested in a THEN arm never captures the outer scan.
  2. **Is the member domain CLOSED?** Every outcome a string literal (a static palette can be
     built), or does an outcome return DATA — `IF x THEN [Category] ELSE "Other" END` — where no
     static member→colour map exists and a different mechanism is required.
  3. **What SCOPE does each predicate need?** A lattice, `constant < parameter < row < aggregate <
     lod < view`, with the least upper bound taken across *all* branches so a cheap first predicate
     cannot hide an expensive later one. `view` is the consequential one: `WINDOW_*` / `RUNNING_*` /
     `RANK*` / `TOTAL` / `INDEX` / `FIRST` / `LAST` compare a mark against the **other marks in the
     view**, which `2.152.0` measured cannot survive as a standalone model measure.

  Fail-closed throughout: an unreadable formula returns `supported=False` with a reason and an empty
  branch list, never a half-parsed guess.

- **`tableau-migration` (skill `2.151.0` → `2.152.0`): a boolean colour driver is usually a
  comparison, and a view-scoped one must not be painted at all.** Two halves of one defect, both
  measured on `0070_new_max` — the "highlight the bar that set a new max" workbook.

  **The twin was never generated.** `_boolean_colour_twin_measures` fired only when the translated
  DAX contained a literal `TRUE()`/`FALSE()`. The commonest boolean calc is a *comparison* —
  `SUM([Sales]) = WINDOW_MAX(SUM([Sales]), FIRST(), 0)` → `SUM(…) = MAXX(WINDOW(…), …)` — which is
  boolean-valued and contains neither literal. So no twin was emitted while the report confidently
  emitted a `Field value` reference to one. It now also triggers on the Tableau-declared
  `datatype == "boolean"`, which required carrying that datatype onto table-calc measure rows (they
  are built from a worksheet *usage*, which has none). The literal trigger is retained so a measure
  whose datatype the extractor did not supply keeps the twin it has always had.

  **Even with the twin, the colour was wrong.** Queried on the rebuilt model, `New Max2?` returned
  `False` for all four years — on a monotonically rising series where *every* year sets a new
  maximum. The window orders by row-level `Order_Date` while the visual's axis is `Date[Year]`, so
  the comparison never aligns: a view-scoped table calc cannot survive as a standalone model
  measure, exactly as `_is_view_level_calc` already documented and exactly why the CONTINUOUS colour
  paths have always refused such a driver. The discrete path did not, and that asymmetry is what let
  a confidently wrong colour ship. Fixing the first half alone would have been **worse than the
  bug** — it turns "no colour" into "a plausible colour that is backwards" — so both land together.

  Two deferrals now guard the discrete paths (chart marks and matrix/table cells alike), each naming
  its cause and its remedy rather than failing quietly:

  * the driver is a **view-level table calc** (`WINDOW_*` / `RUNNING_*` / `RANK` / `INDEX` …) — it
    compares each mark against the other marks in the view, which needs a Visual Calculation;
  * the driver **has no translated model measure**, so the twin it would be painted from does not
    exist. Scoped by a new `model_consulted` stamp so it fires only on the model-bound pass — the
    pre-rebind pass and every direct `parse_twb` caller have no model by construction and are
    unchanged.

  Corpus: **dangling colour references 4 → 0**, 29/29 still built. Three visuals stop painting a
  colour that referenced a measure the model never contained; two models gain an (inert, additive)
  colour twin; `workbook_calcs_translated` 198 → 201.

- **`tableau-migration` (skill `2.150.0` → `2.151.0`): an eagerly-evaluated calculated table must not
  reference an undeclared stub column.** Raised in #134, and the answer is the one the reporter asked
  for: *"the most useful thing a maintainer could tell me is which condition makes the difference."*

  **Settled by experiment.** A real corpus model turned out to have the exact shape they described —
  `0083_previous_workday`, whose generated `Date` calendar spans a `textscan` stub. Cold-opening it in
  Desktop:

  | stub | result |
  |---|---|
  | **declares** the referenced column | **opens**, degraded — *"One or more calculated objects need to be manually refreshed"* / *"Some of the tables have incomplete or no data"* |
  | declares **nothing** | the eager reference cannot resolve → the calculated table fails at model LOAD → **the file does not open** |

  Their hypothesis was right. The discriminator is the undeclared column, not the presence of a stub.

  Two changes, because prevention and detection are different jobs:

  - **Prevention** — the calendar span skips stub-backed tables (`_stub_backed_tables`). Correct even
    where the reference resolves: a stub holds no rows, so its `MIN`/`MAX` is BLANK and it can never
    contribute a bound. It is pure exposure with no benefit. If that empties the span, the existing
    `CALENDARAUTO()` fallback takes over.
  - **Detection** — `check_model_openability` gains `eager_calc_refs_resolve`, naming the class rather
    than merely avoiding it in the one generator known to hit it. This is the reporter's own framing
    of why it needed a new check: *deserializes* (validate) passes, *refreshes* never runs because the
    file will not open, and only *opens* catches it — which nothing automated covered.

  **The corpus caught my first version.** It keyed on the presence of a stub and failed
  `0083_previous_workday`, a model that opens fine. That over-fire is what prompted the cold-open
  experiment above, and the check now keys on the unresolvable reference.

  Deliberately unchanged: the engine's avoidance of `CALENDARAUTO()`. It scans every dateTime column
  model-wide, so one birthdate drags the calendar back decades (measured: a 1941 calendar for 2017+
  data). "Just use `CALENDARAUTO`" is not the fix, and the reporter said so first.

  Corpus: 29/29 built, **zero drift** across 695 emitted files.

### Fixed

- **`tableau-migration` (skill `2.149.0` → `2.150.0`): a join across incompatible connectors now
  reaches the storage-mode gate that was built for it.** Reported in #133, with the mechanism traced
  in code by the reporter and confirmed here end to end — which is exactly the step they said they
  could not take (*"I have NOT confirmed on a fixture that the `relations` list consumed by
  `select_storage_mode()` is the one built at connection_to_m.py:1400"*). It is. On their repro shape
  (a `federated` datasource whose top-level relation is a `join` spanning a `sqlserver` named
  connection and an `excel-direct` one):

  | | kinds | mode | fallback |
  |---|---|---|---|
  | before | `{table}` | `DirectQuery` | `None` |
  | after | `{join, table}` | `None` | `needs-storage-decision` |

  The gate was never missing — `storage_mode` refuses any descriptor whose relation kinds include a
  container. It became **unreachable** when `_extract_relations` started `continue`-ing past the
  container before `_classify_relation` could give it a `kind`, leaving the old contract stranded in
  `_is_combination_relation`'s docstring (*"reported as a single combination entry so the
  storage-mode policy can fall back"*). That surviving docstring is the tell the reporter spotted.

  **The harm is sharper than "bound to one upstream."** Each table does route to its own upstream
  correctly — but the storage *mode* is chosen once per datasource, so the reported shape emitted:

  ```
  Flat.tmdl   mode: directQuery   Source = Excel.Workbook(File.Contents("//host/share/f.xlsx"))
  ```

  An Excel workbook is not a DirectQuery-capable source at all. The model builds, validates, and is
  bound wrong.

  **The discriminator is connector CLASS, not connection count, and the corpus is why.** The first
  version gated on "leaves span more than one named connection" and regressed
  `0086_hex_tile_maps` — which joins two separate `excel-direct` workbooks (two connections, both
  Import, rebuilding correctly as two related tables) — from built to skipped, 29/29 → 28/29. The
  corpus gate caught it. The predicate is narrowed to the case that genuinely cannot share one mode:
  leaves straddling the flat-file / live-relational line. Single-connection joins, two-flat-file
  joins and two-relational joins are all untouched.

  Also answers the reporter's point 3 (*"consider whether `storage_mode.py:341` should still
  reference `join`/`union` at all — if no code path can produce those kinds, the branch is
  misleading"*): the branch is now fed rather than narrowed, and a test asserts it is reachable.

  Note this is no longer a dead end for the operator: **2.140.0** added `--storage-decision` /
  `--accept-recommended-storage`, so a gated datasource can be answered rather than merely refused.

  Corpus: 29/29 built, **zero drift** across 695 emitted files.

- **`tableau-migration` (skill `2.148.0` → `2.149.0`): the last two slicers that pre-selected
  nothing, and a duplicate filter name that a warning-only gate lets through.** Investigated from
  #130, whose reporter filed the general claim and then — commendably — filed a correction against
  themselves, found counter-evidence, could not reproduce their own headline, and asked that the
  issue not be worked until they could run a controlled experiment. This is that experiment's
  result.

  **The general claim did not hold.** `_slicer_preselection_object` has emitted the open-on selection
  into `objects.general[].properties.filter` since **2.51.0**, which predates the 2.126.0 they
  tested. Measured on the 29-workbook corpus at 2.148.0:

  | | count |
  |---|---|
  | `general.filter`-only (correct) | **50** |
  | `filterConfig`-only (the reported bug) | **2** |
  | both | 0 |
  | neither (no default) | 22 |

  which matches their own counter-evidence (330 slicers: 36 `general.filter`-only, 0
  `filterConfig`-only) rather than the headline. But the experiment found the residual two, and both
  are real:

  **1. A boolean column never pre-selected.** The gate accepted `string`, integer date-parts and real
  dates and declined everything else, so a boolean selection fell through to `filterConfig` and
  pre-selected nothing — the reported symptom exactly, surviving in one narrow shape. Both remaining
  cases were boolean DAX calculated columns (`'X'[a] = 'X'[b]`). The literal must be bare
  `true`/`false`: those slicers were emitting the STRING `'true'` against a boolean column, which
  matches no row and reports no error.

  **2. A duplicate filter `name` report-wide.** The reporter listed this as encoding detail #1 and
  was right, including about why it hides: it emits `PBIR_FILTER_NAME_DUPLICATE_GLOBAL` as a
  **warning**, so an `errorCount`-only gate passes it. Confirmed live on
  `0088_salesforce_nonprofit_case_mgmt` *before* any change here, so it is pre-existing rather than
  introduced — `_inherit_flag_filters` deep-copies one worksheet's filterConfig onto every visual
  derived from that worksheet, name included. Each stamped copy now carries a visual-unique name.

  After both: that workbook validates **0 errors / 0 warnings** where it previously carried the
  duplicate-name warning, and the corpus emits **zero** `filterConfig`-only slicers (52
  `general.filter`, 22 with no default). Drift across 695 emitted files is 3 files, all in the one
  workbook that had the defect.

- **`tableau-migration` (skill `2.147.0` → `2.148.0`): a map's basemap is a per-worksheet property,
  so a module-level constant cannot be right.** Reported in #128. `_AZURE_MAP_DEFAULT_STYLE =
  "blank_accessible"` was applied to **every** emitted `azureMap` regardless of what the source
  draws, so a Tableau satellite or dark basemap rebuilt as marks floating on white.

  The structural argument is the one that settles it: **one workbook can contain satellite, dark and
  light basemaps at once**, so no single constant can serve them. The style now comes from the
  worksheet's own `<style-rule element='map'><format attr='map-style'>`.

  **The old acceptance criterion was inverted, and Tableau's own render proves it.** The constant's
  comment recorded that `grayscale_light` was rejected for *"drawing a grey basemap with
  Canada/Mexico"*. Confirmed independently on the corpus workbook `0063_remove_null_and_all`: its
  embedded Tableau `<thumbnail>` for `Solution 02` — Tableau's render, not anyone's interpretation —
  shows exactly that, a light grey basemap with grey Canada and Mexico and country labels beneath a
  green choropleth. The style had been refused for reproducing the reference faithfully.

  Render-verified after the change: that workbook's map draws the basemap, grey Canada/Mexico, water
  and state labels, where before it drew polygons on white.

  Mapping keys are harvested from real workbooks, not guessed — across the corpora on this machine:
  `light` ×20, `tableau-light-gray` ×7, `satellite` ×1, plus two custom `mapbox://` styles. Values
  are checked against the live enum from
  `powerbi-report-author formatting describe-object azureMap mapControls`, and a test asserts every
  emitted value is in it (a typo there is invisible to PBIR validation and shows up only as a map
  that will not draw).

  Two cases refuse rather than approximate, and say so:

  - a **custom Mapbox** basemap (`mapbox://styles/<user>/<id>`) is an arbitrary third-party design no
    stock Azure style reproduces — the map keeps the default and the run warns, naming the style;
  - an **unrecognised token** fails closed, so a Tableau version that spells a style differently
    keeps today's behaviour rather than being mapped by guesswork.

  **Deliberately NOT changed: the no-signal default.** A worksheet that declares no `map-style` has
  not told us it wants a blank basemap — it means the author never moved off Tableau's default.
  Changing that would alter every map this engine has emitted, and `blank_accessible` is the one
  value that was actually compared against a Tableau reference in Desktop. Left for a render-verified
  change of its own rather than folded in here on inference. (The reporter also notes our own
  `powerbi-report-gotchas` skill gave the original advice and has since been corrected; that half is
  theirs and is done.)

  Corpus: 29/29 built, and the diff across **695** emitted files is exactly the three maps in
  `0063_remove_null_and_all` moving `blank_accessible` → `grayscale_light`. Nothing else moved.

- **`tableau-migration` (skill `2.146.0` → `2.147.0`): a BIFF8 `.xls` navigation table has no
  `Item`/`Kind` columns, so that key can never match.** Reported in #129 as a sibling of #108 — same
  symptom at refresh, different cause one level down, and the report was right on every point.

  `Excel.Workbook` returns a **different navigation table per container format**:

  ```
  OOXML (.xlsx/.xlsm)        columns: Name, Item, Kind, Hidden, Data   -> [Item=…, Kind=…] works
  BIFF8 / OLE2 (legacy .xls) columns: Name, Data                       -> [Item=…] matches NOTHING
  ```

  The emit site was unconditional, so no return value from `_excel_navigation` could ever produce a
  `Name=` key. #108 fixed *which sheet* (stripping the ACE `$`); this fixes *which key shape*. The
  two compose, and a test asserts them together, because fixing either alone still dies at refresh.

  **Measured on the reference corpus, not a synthetic fixture.** `0063_remove_null_and_all` packages
  a genuine OLE2 workbook (magic `D0 CF 11 E0 A1 B1 1A E1`, verified byte-wise):

  | navigation key | refresh result |
  |---|---|
  | `[Item="Sheet1", Kind="Sheet"]` | `Expression.Error: The key didn't match any rows` — **0 rows** |
  | `[Name="Sheet1"]` | `DATA_OK + PERSISTED` — **8,399 rows** |

  The branch is on the **container**, never the extension, because the extension lies in both
  directions: a `.xls` may be OOXML (Excel opens a renamed one, and export tools emit them) and an
  `.xlsx` is never BIFF8. That was the reporter's argument and it is the right one. Fail-closed — an
  unreadable path keeps today's `Item`/`Kind` emission, which is correct for every OOXML workbook.

  This is another member of the class the reporter named precisely: the model validates, opens, and
  satisfies the definition of done, and *only then* fails at refresh. Three green gates describing a
  model that could not load a row.

  **Not changed, and deliberately so:** the same issue reported a missing `culture` on that
  partition. That is by design and already handled. `flatfile_culture` returns
  `legacy-ace-host-rendered` for a legacy ACE workbook because the cells arrive as text already
  rendered in the *refreshing* machine's locale, which is not observable at generation time and not
  observable from within M either. Rather than guess a locale onto the user's data, the run emits a
  loud per-table warning naming the remedy, and it fires on exactly this workbook:
  *"table 'Sheet1$' reads a LEGACY Excel workbook … on a comma-decimal host every decimal column
  inflates by 10^decimals and every structural gate still passes. Convert the source to .xlsx or CSV
  (or add an explicit culture to this partition)…"*

  Corpus: 29/29 built, and the diff against the pre-change baseline is exactly the two intended
  navigation lines and nothing else.

### Added

- **`tableau-migration` (skill `2.145.0` → `2.146.0`): the discrete colour palette is the AUTHOR's,
  with an opt-in semantic red/green for polarity domains.** The colour twin landed in `2.144.0`
  always used Tableau's default categorical ramp. That is right for an unauthored domain — and
  wrong the moment the author opened Tableau's colour editor, because their assignment lives in the
  worksheet (`<style-rule element='mark'><encoding attr='color'><map to='#hex'><bucket>`) where only
  the report layer can see it, while the twin is a DAX measure only the model can own.

  So the first viz pass now exports `discrete_colour_palettes(ir)` and the model build consumes it —
  the same report-informs-model channel the scatter composite grain key already uses. Resolution is
  three-tier and the tier that fired is recorded on the measure
  (`TranslatedBy = deterministic (categorical colour measure, <origin> palette)`), so a default is
  never presented as the author's choice:

  1. **`authored`** — the workbook's own per-member colours. Always wins. A *partial* assignment is
     refused rather than mixed with defaults: filling the gaps from the ramp would shift the
     members the author *did* choose into different slots and silently recolour them.
  2. **`semantic`** — opt-in via `--semantic-colours`: red `#D62728` / green `#2CA02C` for a domain
     of exactly two recognised, opposite polarity members (`negative`/`positive`, `loss`/`profit`,
     `fail`/`pass`, `below`/`above`, …). Both poles must be recognised, so `East`/`West` is never
     painted as if it meant good and bad.
  3. **`tableau_default`** — Tableau's own categorical ramp in sorted member order. **The default**,
     because a workbook that authors no palette is not colourless: Tableau paints it from that ramp
     and that is what the source actually renders, so reproducing it keeps the rebuild faithful.

  Verified by render both ways on the same workbook: the default build draws negative rows blue and
  positive orange (matching the Tableau reference exactly), and `--semantic-colours` draws them red
  and green. In both, the colour is **discrete** — one solid colour per member, bound as
  `Field value`. Explicitly regression-guarded against the `2.127.0` trap: a string domain must
  never acquire a `linearGradient`/`FillRule`, on a matrix or on a chart, because Power BI evaluates
  MIN/MAX over the fill input to find a ramp's endpoints, cannot do that to a string, and kills the
  visual at query time through a validation that reports zero errors.

  The same single twin drives every family — matrix cells (`values[].fontColor`/`backColor`, chosen
  by the mark) and chart marks (`dataPoint.fill`) — so bars, circles and cells all follow the one
  encoding. Across the 29-workbook corpus this changes no colour and no metric: the one existing
  twin gains its `tableau_default` disclosure and nothing else moves.

- **`tableau-migration` (skill `2.144.0` → `2.145.0`): a report with no pages CRASHES Power BI
  Desktop, so one is never emitted again.** A PBIR whose `pages.json` carries `"pageOrder": []`
  does not open as an empty report — Desktop throws
  `TypeError: Cannot read properties of undefined (reading 'visualContainers')` and refuses the
  project outright. That is worse than an empty report, because the semantic model built beside it
  in the same `.pbip` becomes unreachable too: a correct model, lost to a missing page.

  Measured on `Logic example 4` at this branch's base (`56a7ef5`, skill `2.141.0`): its shipped
  `.pbip` carried `pageOrder: []` with no page folders at all — its one worksheet had been refused —
  and the definition-of-done reported this as `warn`, not a failure. (`2.143.0` fixes the *cause*
  for that workbook; this guards the failure mode itself, which any fully-deferred workbook can
  still reach.)

  Scope of the corpus check, stated plainly: **the guard catches no crashing `.pbip` in the
  29-workbook corpus** — every shipped project there declares pages both before and after this
  change. The only page-less artifact is `reports/0068_market_basket.Report`, the **pre-rebind** viz
  pass, which is not what ships (its `.pbip` has three pages, unchanged by this work). That is still
  worth guarding, because it is a malformed PBIR and it is the folder a report upload consumes, but
  it is not corpus evidence of the crash. The justification is the reproduced `Logic example 4`
  case above.

  Three guards, because the first alone would not keep it from coming back:

  1. The emitter ships one placeholder page (`No visuals rebuilt`) when nothing was rebuilt —
     **scoped** so the emit gate's own contract is untouched: an unsupported mark, a chart missing a
     required role and a deliberately deferred shape still emit **no visual**. The placeholder adds
     the container Desktop requires, never a visual the gate refused.
  2. `pbir_lint` flags an empty `pageOrder` (validity R7). `powerbi-report-author validate` does
     catch this as `PBIR_PAGE_ORDER_EMPTY`, but it is an opt-in npm pre-gate an ordinary run never
     reaches, whereas the hermetic linter runs in the always-on pytest gate.
  3. The definition-of-done reports a page-less report as **`failed`**, alongside a model that will
     not load, rather than softening it to a fidelity `warn`. Fail-safe: an unreadable page count is
     never treated as zero, so it cannot manufacture a false failure.

- **`tableau-migration` (skill `2.143.0` → `2.144.0`): a text table's conditional colouring is
  carried, and it colours the TEXT — not the cell background.** Tableau's ordinary way to
  colour-code a crosstab is a calc that returns a *label* —
  `IF SUM([Profit]) < 0 THEN "negative" ELSE "positive" END` — dropped on Colour. Nothing in the
  rebuild attempted it, so every number came out black: the source's whole point, lost, with no
  warning.

  Power BI cannot drive a native categorical legend from a MEASURE — a legend needs a grouping
  COLUMN, a column is row-level, and a row-level split changes the aggregate grain and the row
  count — so this reuses the pattern already proven for boolean colour: a DAX measure that
  **returns a colour**, bound through conditional formatting as the `Field value` format style,
  editable in Desktop's `fx` dialog rather than unreachable JSON. The model gains a twin
  (`Sign (colour) = SWITCH([Sign], "negative", "#4E79A7", "#F28E2B")`) whose palette is Tableau's
  default categorical ramp assigned in **sorted member order** — which is how Tableau assigns it,
  so blue/orange land on the same members the source drew.

  **The channel follows the MARK, because Tableau's Colour shelf paints the mark.** On a `Text` (or
  `Automatic`) crosstab the mark IS the number, so the colour is `fontColor` and the cell background
  is left alone. On a `Square` highlight table the mark is a filled rectangle, so the same encoding
  is `backColor`. Painting a text table's background reproduces neither half: every cell gains a
  fill the source never drew while the numbers stay black. One entry is emitted per value column —
  Tableau colours the whole row from one mark colour, and a single unscoped entry colours only the
  first column — each carrying the `dataViewWildcard` selector, without which Power BI evaluates the
  expression in one context and paints every cell identically, *with a clean validation pass*.

  The colour driver is also no longer projected as a matrix column: it is a STRING measure, so
  leaving it in the query rendered a literal `negative`/`positive` column beside the numbers.

  Verified by render, not by metric: the rebuilt matrix draws negative-profit rows entirely blue and
  positive rows entirely orange, backgrounds untouched, matching the Tableau source. Gated on the
  Tableau-declared `datatype`, so a numeric measure that merely mentions a string (a `FORMAT`
  pattern, a `SWITCH` label) can never acquire a bogus twin. Across the corpus this adds exactly one
  measure and changes no existing visual.

- **`tableau-migration` (skill `2.142.0` → `2.143.0`): an unfiltered Measure Names level means EVERY
  measure, so the most ordinary text table migrates at all.** Tableau writes a bare
  `<groupfilter function='level-members' level='[:Measure Names]'/>` the moment Measure Names lands
  on a shelf with nothing filtered out. The emitter classified it alongside `except` — and the two
  are **opposites**: `except` lists the members that were REMOVED, `level-members` means every
  member of the level. So the worksheet was refused as "an Exclude filter whose displayed set
  cannot be derived", and when it was the workbook's only sheet the report came out with **zero
  pages** — while `powerbi-report-author validate` reported 0 errors and the definition-of-done
  reported PASS. A trivial crosstab of `Segment / Ship Mode / Order ID` against eight measures did
  not migrate at all.

  Tableau records no explicit member list for the unfiltered case, so the members are recovered from
  the view's own `<column-instance>` declarations: every `quantitative` pill it depends on, minus
  every pill it spends on a named shelf or encoding (a measure parked on Tooltip is not a displayed
  column). Ordering is alphabetical **by caption**, which is how Tableau renders an unsorted Measure
  Names header — the declaration order it was recovered from is Tableau's internal id sort
  (`cnt:` &lt; `none:` &lt; `sum:` &lt; `usr:`), which would scatter the calcs to the end of the table.

  Fail-closed and narrow: only a *childless* `level-members` is read as "all members". `except`, a
  narrowed `level-members`, and a non-manual `union` keep deferring exactly as before, so the guard
  against surfacing the wrong measure set is untouched. Across the 29-workbook corpus this changes
  no existing output (every Measure Names filter there is an authoritative `op='manual'` keep-list).

- **`tableau-migration` (skill `2.141.0` → `2.142.0`): a caption sized in Tableau does not fit in
  Power BI, because Power BI adds padding Tableau has not.** Power BI reserves 8px above *and* below
  a textbox's text by default, so a box's usable height is `height - 16`. Tableau reserves nothing.
  An author who drew a 24px caption strip drew it to fit 12pt text, and it does — in Tableau.
  Emitted verbatim, those 24px leave 8px for a line that needs 19, and the band renders **clipped**:
  descenders sheared off, a scrollbar stub where the text should be.

  Found by rendering a real network-operations dashboard, not by a metric. Two bands on one page:
  `"Sort By = Network Score | Region = All | Fiscal Month ="` at 24px/12pt, and a section header at
  31px/16pt. The build validated with **zero errors** — and our own gate already knew, because
  `powerbi-report-author validate` warns `PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR` using exactly the
  renderer's formula. We were emitting geometry the gate then told us was wrong, one step too late
  to matter.

  **The fix is not to grow the box, and that distinction is the whole of it.** Growing it is what
  the layout solver deliberately refuses to do (`layout_solve._clamp_to_authored`), for a measured
  reason: a readability floor propagated up a zone tree makes a frame scale the WHOLE canvas to
  satisfy it — eleven pixels of caption once cost five hundred pixels of page, with every object on
  it 50% taller. The first version of this fix did exactly that and
  `test_thin_caption_sizes_to_content_not_inflated_to_floor` caught it, correctly.

  The 16px is not the author's, it is **ours**: a default we never asked for, on a box we emit. So
  the room comes out of our own padding first, down to zero, and the authored geometry is never
  touched. A textbox with room to spare emits no padding block at all and is byte-identical to
  before. Applies to dashboard text objects, caption-only worksheets, and the title banner alike.

  Verified by render: the reported dashboard now shows both bands in full, and the report validates
  **0 errors / 0 warnings** where it previously carried two.

- **`tableau-migration` (skill `2.140.0` → `2.141.0`): `estate_survey.py --json` declares a schema
  contract, so a rename cannot fail silently.** Raised in #114 — not a defect report, a heads-up that
  a downstream assessment tier shells out to this script and builds its migration-order graph from
  specific key paths. What made it worth acting on is the failure mode, which is the same
  wrong-direction failure `estate_survey` itself exists to prevent:

  > If that key is renamed, our parser finds zero edges and reports *"migration order unknown"* —
  > which is indistinguishable from a site that genuinely has no published datasources. A workbook
  > whose datasource has not landed then rebuilds to an **empty report**.

  They already refuse tolerant fallbacks on their side, deliberately, having been bitten once by
  their own guess at `datasource`/`name` parsing **zero** edges and reporting "order unknown". Their
  ask was small ("a note on this issue is enough") with an offer: *"If you would rather we pinned to
  a schema version field instead, we are happy to consume one."*

  Taken up, plus the half a note cannot give:

  * the payload now carries **`schema_version`** (`SURVEY_SCHEMA_VERSION`, `"1.0"`) — MINOR for an
    added key, MAJOR for a renamed/removed/retyped one, so a consumer can refuse a payload it does
    not understand;
  * **`SURVEY_CONTRACT_KEYS`** names every consumed path, with
    `workbooks[].published_dependencies[].datasource_name` — the load-bearing one — first among
    equals;
  * `tests/test_estate_survey_contract.py` **walks that list against a real payload**, so the list
    cannot rot into a stale comment and a rename is a test failure with the contract attached rather
    than a review diff that looks harmless.

  The emitted keys are unchanged; `schema_version` is purely additive.

- **`tableau-migration` (skill `2.139.0` → `2.140.0`): `needs-storage-decision` is no longer
  terminal — the decision it demands can now be supplied.** Reported in #116, with both halves
  traced in code rather than inferred from behaviour, and both exactly right:

  * `select_storage_mode(descriptor)` took only a descriptor, and no `migrate_estate.py` flag
    carried a storage decision, so an operator who *had* made the choice had nowhere to put it;
  * `FALLBACK_LAND_TO_DELTA` was **read** by `assemble_model` and **assigned by nothing** — the
    documented "explicit opt-in" to DirectLake branched on a value the engine could not produce.

  Measured by the reporter at **14 of 38 workbooks — 37% of a real estate** — ending with no model
  and no report, while the message told them a choice was available.

  Two routes, either of which closes it (the issue's own suggestions 1 and 2):

  ```
  --storage-decision <json>        {"Big Data Source": "DirectLake", "*": "Import"}
  --accept-recommended-storage     apply each datasource's already-computed recommended_mode
  ```

  An answer is honoured only where one was actually demanded. A mode the engine chose confidently is
  returned untouched — this seam supplies a MISSING decision, it does not second-guess a made one —
  and a `schema-not-visible` datasource is refused outright, because "Import" is not a choice you can
  make about a model that cannot be typed at all. A misspelt answer raises instead of being ignored,
  since silently dropping it would reproduce the very dead end the flag exists to clear. Every
  outcome is stamped `storage_decision` / `storage_decision_applied` / `storage_decision_note`, and
  the original rationale is prepended to rather than replaced, so the summary still says why a
  decision was demanded.

  **What is deliberately NOT changed: DirectLake is still never auto-selected.** Silently landing a
  customer's data in Delta because a CSV path went stale is a far worse failure than stopping.
  Nothing here fires without an explicit operator answer.

  Verified end to end on the reported shape: `--accept-recommended-storage` takes a previously
  terminal workbook **0/1 → 1/1** with an openable `.pbip`, and `--storage-decision {"*":
  "DirectLake"}` writes a real 8-table landing plan to `landing_plans/<wb>.landing_plan.json`. That
  last part was a second gap found while fixing the first: the DirectLake branch computed a plan and
  never wrote it anywhere, so the warning promised an artifact that did not exist. It is now written,
  recorded on the entry, and the message reports an outcome ("was resolved by an OPERATOR DECISION
  to…") instead of still demanding a decision that was already given.

  **Corpus: zero drift.** With no flag passed the run is a strict no-op — 29/29 built, every summary
  metric identical, and all 136 emitted table files byte-identical once per-run `lineageTag` GUIDs
  and the output path are masked.

### Fixed

- **`tableau-migration` (skill `2.138.0` → `2.139.0`): a storage-decision failure named the one
  datasource with nothing wrong with it.** Reported alongside #124. A workbook's embedded
  datasources are consolidated into ONE model, and the fallback message named the **ranked primary**
  — not the island whose relations actually failed:

  ```
  embedded datasource 'Big Data Source' needs a storage decision
    (Direct-upstream rebuild not safe (relation 'Orders.csv' has no resolvable columns;
     relation 'Orders_Archive.csv' has no resolvable columns))
  ```

  Both column-less relations belonged to `Small Data Source`. A reader following the only actionable
  fact in that sentence would open `Big Data Source`, find three cleanly-typed tables, and be no
  closer to the cause. Two independent things were wrong and both are fixed: the **subject** named
  one island for a model spanning several, and the **reasons** lost their island as
  `combine_descriptors` merged them. The same failure now reads:

  ```
  the consolidated model for 2 embedded datasources ('Big Data Source', 'Small Data Source')
    needs a storage decision
    (Direct-upstream rebuild not safe (Small Data Source: relation 'Orders.csv' has no resolvable
     columns; Small Data Source: relation 'Orders_Archive.csv' has no resolvable columns))
  ```

  Verified end to end by reproducing the original failure (the union's own metadata removed so
  promotion declines). Attribution happens only on the consolidation path — `combine_descriptors`
  returns a lone descriptor unchanged — so a single-datasource workbook's message is byte-identical
  to before, and re-attribution is idempotent.

- **`tableau-migration` (skill `2.137.0` → `2.138.0`): a UNION is one table, so its container —
  not its members — is the relation.** Reported in #124. Tableau writes the two container kinds
  with opposite metadata, and that difference is the whole defect:

  ```
  union   <relation type='union' name='Orders.csv+'>            <- ALL 11 columns filed under
              <relation type='table' name='Orders.csv'/>           [Orders.csv+]; the members get
              <relation type='table' name='Orders_Archive.csv'/>   NO metadata parent at all

  join    <relation type='join'>                                <- each member keeps its OWN
              <relation type='table' name='Customers.csv'/>        [Customers.csv] parent and
              <relation type='table' name='Customers_Details.csv'/>   its own columns
  ```

  That is the semantics, not a quirk: a union produces the SAME columns with MORE rows
  (`Table.Combine`), so there is one column list and it belongs to the container. A join produces a
  WIDER row from real tables — which is exactly why we surface join leaves individually and rebuild
  the join keys as model relationships.

  We surfaced BOTH kinds of leaves individually, so every union member came out column-less. That is
  survivable while the whole datasource is column-less — the extract collapse or the multi-parent
  expansion then re-types everything from the `.hyper` — and fatal the moment the datasource is
  PARTIALLY typed, because both of those rescues open with `any(r["columns"]) -> return None`. A
  union sitting beside a join is precisely that shape, and the reporter's control ("the join works,
  the union does not") was exactly right:

  ```
  embedded datasource needs a storage decision
    (Direct-upstream rebuild not safe (relation 'Orders.csv' has no resolvable columns;
     relation 'Orders_Archive.csv' has no resolvable columns))
    -- workbook .pbip skipped
  ```

  No model, `definition_of_done: failed` — while the union's own 11 typed columns sat under
  `[Orders.csv+]` the entire time. **The diagnosis moved during the fix:** the abort is at
  `connection_to_m.py:1210` (`any(r.get("columns"))`), not the one-to-one extract match at `:1231`,
  because `Customers.csv` types and short-circuits the expansion before matching ever runs. So no
  extract mapping was needed at all — the columns were already in the XML, filed under a name we
  were discarding.

  Measured on the reported workbook (`Section 09 - Filtering Data.twbx`): **0/1 → 1/1**,
  `failed` → `warn`, 9 tables / 51 columns, 5/5 workbook calcs (100%), PBIR validating with zero
  errors and zero unresolved report entities. The two union workbooks that already built are
  unchanged in substance — 21 columns and byte-identical data (2,316 KB manual / 2,425 KB wildcard)
  — and now name the table `Union`, which is what Tableau's own data pane calls it, instead of
  leaking the extract's internal `Extract` name or standing the datasource caption in for a table.
  **Corpus: 29/29 built, 29/29 PBIR-validate, and zero drift** — every summary metric identical to
  the baseline and 0 of 29 workbooks differing in table or column structure.

  Promotion is fail-safe: the container must resolve columns under its own name AND no member may
  resolve any, so a union whose members are real typed tables is left alone, and a wholly-untyped
  union still belongs to the extract collapse/expansion untouched. Members are dropped by element
  IDENTITY, never by name, so a typed relation that merely shares a member's display name survives.

- **`tableau-migration`: `CONTAINER_RELATION_TYPES` was imported in the flat branch only.** A
  latent break shipped with 2.137.0: the constant was added to `connection_to_m`'s
  `except ImportError` branch but not its `from .storage_mode import ...` twin, so a package-style
  import succeeded and then raised `NameError` at the first container check. Tests run with
  `scripts/` on `sys.path` and always took the working branch, so nothing caught it. Both branches
  now bind the same names, and a test parses the import block with `ast` and fails if they ever
  diverge again.

- **tableau-migration (skill `2.136.0` -> `2.137.0`): a WILDCARD union (`batch-union`) is a container,
  and it carries no member relations at all** (reported in #124). Tableau writes a union two ways and
  only one of them looks like a container: a manual union is `type='union'` with its members listed as
  child `<relation>` elements, while a wildcard union is `type='batch-union'` with **no children** —
  its members are a filename pattern (`is-recursive` / `include-siblings` / `path`) resolved at
  connect time.
  That defeated every check we had, in the one way that mattered: it is not
  `type in ("join", "union")`, and the fallback test — *does it nest child relations?* — also fails.
  So it survived as a relation beside the extract's own materialised table, the datasource looked like
  one logical table spanning several relations, storage-mode selection classed that
  `shape-not-directly-rebuildable`, and the whole workbook was skipped with *"needs a storage
  decision"* — no PBIP, no model, `definition_of_done: failed` — while 21 typed columns and 2.4 MB of
  unioned rows sat in the packaged `.hyper` the entire time.
  Measured on a matched pair built from the same four CSVs: the **manual**-union workbook migrated
  1/1, its **wildcard** twin 0/1. Same data, same extract shape (`[Union]` 22 columns live,
  `[Extract]` 21 extracted) — the relation type was the only difference. After the fix the wildcard
  twin builds a 21-column model with 2,426 KB of unioned data and PBIR-validates with zero errors,
  and the manual twin is unchanged.
  The fix is a single shared `CONTAINER_RELATION_TYPES` rather than a tuple repeated at six call
  sites, because that duplication is *why* this got through: six places independently decided what a
  container was, so adding a Tableau type meant remembering all six, and `batch-union` was added to
  none. It lives in `storage_mode` (the lower-level module, avoiding an import cycle) and is imported
  by the connection parser, so there is now exactly one list to extend — asserted by identity, not
  equality, plus a guard that fails if any call site re-introduces the literal pair.
  Note for #124: this is a *sibling* of the multi-table-extract union case reported there. A union
  whose extract materialises a single `[Extract]` parent already worked; the remaining reported shape
  — a union inside a **multi-parent** extract, where the non-leftmost member matches no parent and
  aborts the whole expansion — is still open.

- **tableau-migration (skill `2.135.0` -> `2.136.0`): per-island Date dimensions switched OFF, and a
  calc-coverage floor so this class cannot recur silently.** Generating one Date dimension per
  datasource island (2.132.0) was correct in isolation — a single shared calendar wired to facts in
  every datasource lets a date slicer filter islands Tableau keeps apart — but it cost **three
  calculations** on the workbook it was built for. Salesforce NPSP went **118/157 -> 115/157**, with
  `Count of Waitlisted Engagements`, the same `... in Date Range`, and `Sort by Intake` newly refused
  on *"must reference exactly one table"*. Calc coverage is the headline number of a migration, and a
  silent date-slicer bleed is narrower than three dead measures, so the feature is disabled behind
  `PER_ISLAND_DATE_ENABLED` rather than deleted — the code and its tests are retained (exercised with
  the switch forced on) so the proper fix has a verified starting point.
  **The dead end is recorded so it is not re-run.** The obvious theory — that the shared calendar
  BRIDGED the islands in the relationship graph, letting cross-island calcs resolve through it — was
  tested by excluding the generated Date relationships from calc path-finding while leaving them in
  the emitted model. Coverage stayed at 115, so that is *not* the mechanism. The remaining suspect is
  field-resolution tie-breaking: with four calendars reserved instead of one, `Record ID` resolves to
  no table while `pmdm__Stage__c` binds to the Intake island's copy of ProgramEngagement. The proper
  fix is **island-scoped field resolution** — a calc binds within its own datasource however many
  calendars exist — which keeps both wins.
  **A coverage floor, because nothing was watching.** The regression passed 4,451 tests, built the
  corpus 29/29, and PBIR-validated to zero errors; it surfaced only because a human compared a number
  against one printed earlier the same day. Every existing gate asks *"did it build, and is it
  well-formed?"* — none asked *"did it translate as much as it used to?"*, and a model can lose a
  third of its measures and still build, open and render, because a stubbed calc is a perfectly
  well-formed `BLANK()`. `tests/test_calc_coverage_floor.py` now pins a per-workbook floor; raising
  one is the normal outcome of a fix, lowering one fails loudly and must be done deliberately with a
  stated reason. Verified to fire on the exact regression it was written for.

- **tableau-migration (skill `2.134.0` -> `2.135.0`): a model emitted below Desktop's compatibility
  level will not REOPEN once it has been refreshed.** The emitter hardcoded `compatibilityLevel:
  1604`. Power BI Desktop silently upgrades an older model in memory, a refresh then persists
  `.pbi/cache.abf` at the UPGRADED level, and the next COLD open fails outright:
  *"Tabular databases do not support CompatibilityLevel downgrade. Current CompatibilityLevel:
  '1606'. Requested CompatibilityLevel: '1604'."* The report does not open at all — not a degraded
  visual, not a wrong number, nothing.
  It is invisible to every check made against a still-loaded session, which is how it shipped: a
  migrated dashboard was built, polished, PBIR-validated with zero errors, refreshed to 5,000 rows
  and screenshotted successfully, and each of those ran against a Desktop instance that was already
  open. Only closing Desktop and reopening from disk reveals it.
  Two defences, both hermetic — neither needs Power BI installed, because the condition is static
  even though the symptom only appears on a cold open. The emitter now uses
  `tmdl_generate.MODEL_COMPATIBILITY_LEVEL` (1606, what Desktop 2.157 writes) so there is no upgrade
  and therefore no mismatch; and `openability_gate` fails any model declaring a level below
  `MIN_COMPATIBILITY_LEVEL`, so lowering it again cannot slip through. The two constants are pinned
  to each other by test.
  Verified by the sequence that produced the bug: build -> open -> refresh -> **close Desktop** ->
  reopen cold, which now succeeds. Corpus 29/29 built, all 29 emitting 1606, 29/29 PBIR-validate
  with zero errors.

- **tableau-migration (skill `2.133.0` -> `2.134.0`): layout polish must never push a control band
  into the content below it.** Caught by rendering the reporter's dashboard through the shipped
  2.132.0 polish: the pass cleared a row-on-row overlap by moving row 2 from bottom `347.7` to
  `369.0` — and the matrix below starts at `362.2`, so the filter row was drawn 7px over the
  `ATTI (Days)` header. It traded a cosmetic collision for one that HIDES DATA, which is strictly
  worse, and the per-page score called it an improvement (6 -> 2) while the render was visibly
  wrong. Exactly the failure mode of trusting a measurement over a render.
  A band is now CLAMPED above the content beneath it: it may be restacked to clear the band above
  only while it still finishes clear of that content. When there is no room it keeps its authored
  top — never worse than it shipped — but is still regularised horizontally, because the uniform
  width, aligned tops and even gutters do not depend on vertical position. (The first attempt at
  this clamp skipped such a band entirely and took the corpus from 54 defects fixed down to 1.)
  The pass that SHIFTED content downwards to make room is removed outright. Moving the reader's
  matrices to accommodate a filter row is redesign, not polish, and it changes a layout the author
  placed deliberately.
  Across the 29 corpus reports: layout defects **54 -> 10**, still with **zero reports made worse**;
  the remainder are band-on-band overlaps that cannot be resolved without pushing into content, so
  they are correctly left alone.

- **tableau-migration (skill `2.132.0` -> `2.133.0`): a field-swap branch may point at a CALCULATED
  column, not just a physical one.** A Power BI field parameter is built by resolving each branch of
  a Tableau swap calc to its landed model home so a `NAMEOF` target can be emitted, and a branch that
  does not resolve is dropped fail-closed. But the swap is assembled BEFORE the calcs are translated
  — the build reserves every name up front so emitted objects cannot collide — so at that moment the
  resolver knows only the PHYSICAL columns. Any branch naming a calculated column resolved to
  nothing and was dropped, silently, so the selector simply came out shorter than the author wrote.
  Measured on an ATTI/ATTR dashboard whose `Choose Date` swap offers Daily / Weekly / Monthly:
  `completedatedt` and `FiscalMonth` are physical and survived, while
  `Complete Date (Week numbers)` — a Tableau week date-bin, and a calculated column — vanished, so
  the reader could pick Daily or Monthly but never Weekly.
  The build order is NOT changed (it exists for a reason). A calc column's name and home table are
  both knowable up front even though its DAX is not yet translated, and a `NAMEOF` target needs
  nothing more, so the planned homes are handed to the locator as a fallback consulted **only** when
  the physical resolver comes back empty — a physical column and a measure both still win. A calc
  whose own field references span several tables (or none) is omitted rather than guessed: a
  `NAMEOF` aimed at the wrong table is worse than a dropped branch.
  Corpus `NAMEOF` target count is unchanged (6 before, 6 after), so this only recovers branches that
  were being lost.

- **tableau-migration (skill `2.131.0` -> `2.132.0`): Tier 3 gains a formatting touch-up beside the
  adjudication report.** ADDITIVE in the strict sense: the adjudication path is untouched and
  byte-identical, polish is a separate capability that runs only on its own explicit GO, and a run
  that declines it produces exactly what it produced before this existed. The gate now offers both,
  independently — *"Do you want the adjudication report, as well as a formatting touch-up?"* — and
  the polish offer fires on EVERY rebuilt report rather than only a warned one, because there has
  never been an output that could not use it.
  **Why every rebuild needs it.** Tableau lays a filter band out with a layout-flow container: the
  author never types coordinates, the container distributes them. Power BI has only absolute rects,
  so the rebuild has to compute what the container computed, and small per-card differences (a
  longer caption, a fixed-size zone, a scaled dashboard) accumulate into a visibly ragged band even
  though every rect came from a faithful reading of the source. Measured on an ATTI/ATTR dashboard:
  row 1 ran `x=8 w=157` then eight cards at `w=131.4` (gutter 15.0 once, 22.0 after), row 2 started
  at `x=15` — and row 1's bottom (287) sat below row 2's top (271.7), so the two rows **overlapped
  by 15.3px** and the second row's captions were drawn under the first row's controls.
  **What it fixes**, worst first because overlap HIDES content: band-on-band and band-on-content
  collisions, non-uniform card size within a band, misaligned left edges and tops, uneven gutters.
  The band keeps its own authored extent — only the distribution inside it is regularised — so a
  band the author placed narrow stays narrow, it just stops being ragged.
  **Proven-improving or nothing.** The page is scored, changed on a snapshot, scored again, and the
  new geometry is kept ONLY when the defect count actually falls; a page that would come out worse
  is restored exactly and reported unchanged. This is not defensive padding — the first version
  improved one page 6 -> 3 while pushing another 3 -> 6 by shoving a band into content it had
  previously cleared. Geometry only: nothing but `position` rects is written, so no field, filter,
  measure or visual type can move and no number can change. Deterministic and idempotent (every
  decision is a median or derived pitch over the band's own members), so the gain is provable by
  re-measuring rather than asserted.
  Across the 29 corpus reports: layout defects **54 -> 0**, with **zero reports made worse**.
  New `scripts/polish_layout.py` (offline, stdlib-only, `--dry-run` to measure without writing).

- **tableau-migration (skill `2.130.0` -> `2.131.0`): `DATETRUNC('week', ...)` translates, so a
  Tableau date bin stops stubbing to `BLANK()`.** Tableau's date-bin builder writes exactly
  `DATE(DATETRUNC('week', [SomeDate]))` when an author picks a *Week numbers* bin (the column is
  stamped `user:agg-type='Week-Trunc'`), so this is what a two-click UI gesture generates, not an
  exotic hand-typed function. It was refused with *"unsupported DATETRUNC part 'week'"* and the whole
  column stubbed to `BLANK()`. The damage did not stop there: on an ATTI/ATTR technician-hierarchy
  dashboard that column was one branch of a Daily / Weekly / Monthly field parameter, and a branch
  pointing at a blank column is dropped -- so the reader's date selector silently offered only Daily
  and Monthly. A refused calc is rarely contained to its own cell.
  Tableau truncates to the START of the week, default start day SUNDAY, so the offset is subtracted
  directly: `WEEKDAY(d, 1)` numbers Sunday=1..Saturday=7, hence `d - (WEEKDAY(d, 1) - 1)` lands on
  that week's Sunday, dropping any time component exactly as `DATETRUNC` does. `quarter` is still
  refused deliberately -- it needs fiscal-year-aware arithmetic, and a wrong quarter boundary is
  worse than an honest fallback.
  Known remaining gap on that dashboard: the Weekly branch still does not reach the field parameter,
  because `assemble_model` builds the parameter swap BEFORE translating calcs, so a branch pointing
  at a *calculated* column cannot resolve while the two physical ones can. That is an ordering fix,
  tracked separately.

- **tableau-migration (skill `2.129.0` -> `2.130.0`): a hidden parameter control is a control, not
  furniture — and a filter card wears its own sheet's face.** Two long-standing defects on a reader's
  ATTI/ATTR technician-hierarchy dashboard, both of which deleted authored interaction silently.
  **The `Date Selection` parameter was dropped entirely.** `hidden-by-user='true'` marks a Tableau
  object collapsed behind a show/hide toggle, and the skip that honours it exempted `filter` zones
  with a stated rule: occluding CONTENT is skipped, a usable CONTROL is kept. `paramctrl` had simply
  never been added to that exemption, with no reason recorded — so the zone was removed 74 lines
  before reaching the `paramctrl` branch whose own comment promises it is *"never silently dropped"*.
  The reader lost the Monthly/Weekly/Daily control that drives the matrix column grain, and the
  rebuild fell back to raw daily dates. A parameter control is small, interactive and cannot paint
  over anything, so it now sits on the same side of the line as a filter card. The test asserting the
  old behaviour carried no docstring, unlike its documented `filter` sibling; it now asserts the
  control survives, with the reasoning written down.
  **Filter cards resolved their formatting from the wrong worksheet.** Tableau's `quick-filter-title`
  / `quick-filter` style rules live on a WORKSHEET, and a dashboard card wears the face of the sheet
  it belongs to — which the zone names. Style was instead keyed by field token alone, and since one
  field is filtered on many sheets, it landed on whichever sheet parsed first. Measured: the cards
  belong to `Trend ATTI` (Segoe UI / bold / `#5a23b9` / 9pt) but resolved through `tech filters`,
  whose only rule is `font-size 6` — so **55 of 57 captions rebuilt as unreadable 6pt grey**. The
  zone's owning worksheet is now carried through and its style applied: 4 styled captions -> 58, and
  every header 9pt instead of 6pt.
  Both are additive: no corpus workbook has a hidden parameter control or declares filter styling, so
  corpus output is unchanged (29/29 built, 29/29 validating with zero errors, slicer count and styled
  count identical before and after).

- **tableau-migration (skill `2.128.0` -> `2.129.0`): a Tableau physical join is one flat rowset, so
  it must filter both ways — plus per-island calendars and two hard formatting errors.** A reader's
  Salesforce case-management workbook rebuilt with *every number on its headline chart wrong*, and
  the cause was one architectural mismatch repeated everywhere.
  **Physical joins now cross-filter bidirectionally.** Tableau's model has two layers that map onto
  Power BI exactly: a **physical join** (`<relation type='join'>`) pre-joins rows into ONE
  denormalized rowset so a filter on any column restricts every column, while a **logical
  relationship** (the 2020.2+ "noodle") joins per query exactly as a Power BI relationship does. Both
  were emitted one-directional. Because a Power BI relationship propagates only lookup -> fact, any
  measure aggregating a lookup-side column and broken down by a fact-side column silently returned
  the GRAND TOTAL on every mark — validating clean and rendering fine. Measured: "Clients by
  Engagement Stage" read 2,638 on all seven bars instead of 708/85/30/25/24/20/17. Emitting
  `crossFilteringBehavior: bothDirections` for physical joins only fixes every number with **no change
  to any measure**. Logical relationships are untouched.
  **An ambiguity guard, because Power BI refuses ambiguous models outright.** The first attempt marked
  23 joins bidirectional and Desktop rejected the entire project: *"There are ambiguous paths between
  'Contact' and 'Date'"*. Direction is what makes that possible — several facts on one Date hub is
  unambiguous one-directionally because nothing travels back UP into Date. So the guard runs over the
  FULL relationship set, after the generated calendar exists, keeping bidirectional edges only while
  they form a forest; inactive relationships are ignored (they carry no filter). A demoted
  relationship still filters lookup -> fact exactly as before and is reported, so the per-measure
  `CROSSFILTER` fallback is a visible choice.
  **One Date dimension per datasource island.** Separate Tableau datasources are islands — one never
  filters another — but a single shared calendar related to facts in every island meant a date slicer
  silently filtered all four dashboards at once. It was redundant too: Tableau's mechanism for a
  cross-datasource filter is a **parameter**, which already translates (a disconnected what-if table
  whose `[Start Date Value]`/`[End Date Value]` measures each island's own row-filter flag reads), so
  splitting the calendar removes a filter path Tableau never had. Fewer than two islands takes the
  original single-calendar path under the original `Date` name, so single-datasource workbooks are
  byte-identical.
  **Two hard formatting errors fixed.** An `azureMap` has no `dataPoint` object at all — its marks are
  drawn by layers — so a flat mark colour there was `PBIR_FORMATTING_PROP_UNKNOWN`, an **error** that
  fails the whole report rather than losing one colour (5 of them on the reporter's workbook). It now
  rides `bubbleLayer.fillColor`. Separately, and pre-existing since long before this release, Power BI
  does not agree with itself on what transparency is called: a bar/column/pie fill is
  `fillTransparency`, an area/line surface is `transparency`, and scatter/funnel have neither. The name
  is now taken per visual type from the visual catalog, and a flat colour is dropped entirely for
  `treemap`/`waterfallChart`/`azureMap`, which have no `dataPoint.defaultColor`. This took the corpus
  from 28/29 to **29/29 validating with zero errors**.

- **tableau-migration (skill `2.127.0` -> `2.128.0`): aggregating a table-scoped LOD is Tableau
  syntax, not arithmetic — so the DAX drops it.** The reporter's workbook highlights its best
  sub-category with
  `IF SUM([Profit]) = SUM({MAX({FIXED [Sub-Category] : SUM([Profit])})}) THEN TRUE ELSE FALSE END`,
  and the engine stubbed it on *"re-aggregating a table-scoped LOD is not supported"* — so that bar
  chart shipped one flat colour while its sibling sheet, which expresses the identical intent as a
  table calculation (`WINDOW_MAX(SUM([Profit]))`), highlighted correctly.
  **The outer aggregate is inert, and Tableau documents both halves of that.** It is present only
  because *"Level of detail expressions are always automatically wrapped in an aggregate when they
  are added to a shelf in the view"* — Tableau's aggregate/non-aggregate mixing rule needs a wrapper
  and Tableau types one in for you. And it computes nothing because *"When no aggregation is needed
  (because the expression's level of detail is coarser than the view's), the aggregation you
  specified is still shown when the expression is on a shelf, but it is ignored."* A **table-scoped**
  LOD is the coarsest value that exists — one number for the whole table, identical on every row — so
  it is coarser than every possible view grain and that clause always holds. Which outer aggregate
  was written is therefore irrelevant, and it is discarded rather than modelled.
  **This is the one LOD shape where the collapse is unconditionally safe**, which is what keeps the
  notorious "SUM of a FIXED LOD multiplies by the row count" gotcha out of scope: that needs a grain
  BETWEEN row level and the view — a partly-replicated value a real SUM then double-counts — and a
  table-scoped LOD has no such middle grain. A *dimensioned* LOD still takes the SUMMARIZE
  re-aggregation path untouched, and re-aggregating a table-scoped INCLUDE/EXCLUDE still falls back,
  since the justification is FIXED's semantics specifically.
  **ALL, not ALLSELECTED — and the same workbook proves the difference is real.** FIXED *"ignores all
  the filters in the view other than context filters, data source filters, and extract filters"*
  because Tableau evaluates it before dimension filters, so it maps to `ALL`; `WINDOW_MAX` runs over
  the marks in the partition, so it maps to `ALLSELECTED`. Identical unfiltered, divergent the moment
  a reader touches a slicer — two Tableau constructs, two different DAX functions.
  Emits `CALCULATE(MAXX(SUMMARIZE('Orders', 'Orders'[Sub-Category]), CALCULATE(SUM('Orders'[Profit]))),
  ALL('Orders'))`. Rendered in Power BI Desktop against a refreshed model, the collapsed LOD and the
  table-calc sibling independently highlight the same bar — the cross-check that the collapse really
  computes what Tableau computed. No corpus workbook used this shape (61 stubbed calcs before and
  after, 29/29 fixtures), which is exactly why it survived to a reader.

- **tableau-migration (skill `2.126.0` -> `2.127.0`): a boolean on the Colour shelf stops killing the
  visual, and a multi-dimension scatter stops losing its grain.** Two defects a reader supplied a
  workbook for, both of which shipped clean through validation:
  **A discrete measure on Colour is no longer rebuilt as a continuous gradient.** Tableau's ordinary
  idiom for "colour these marks two ways" is a boolean calc on Colour
  (`IF SUM([Profit]) > 0 THEN TRUE ELSE FALSE END`). It is a *discrete* pill -- Tableau paints two
  swatches and offers no ramp -- but any unbound calc on Colour was classified continuous and became a
  `linearGradient3`. Power BI evaluates `MIN` over the fill input to find the ramp's endpoints, cannot
  do that to a boolean, and the visual dies at query time with *"Error fetching data for this visual"*.
  The JSON is well-formed, so PBIR validation passed and every one of the reporter's 7 sheets was dead
  on open. The classifier now reads the pill's own role code (`:nk` discrete / `:qk` continuous, which
  `_is_continuous_pill` already parsed but nothing consulted) plus the calc's datatype, and an
  unconditional crash guard in `_chart_continuous_fill` refuses a gradient over a non-numeric driver
  even if a future caller bypasses the classifier. A genuinely continuous numeric measure keeps the
  gradient it always had.
  **The rebuild is the idiomatic Power BI one, not merely a working one.** The model now emits a
  hex-returning **colour twin** for every boolean measure (`good/bad (colour)` =
  `IF([good/bad], "#4E79A7", "#F28E2B")`, Tableau's own default pair) and the visual binds it as
  conditional formatting -- Microsoft's documented approach, *"a DAX measure that returns color values
  based on your business logic"*. Because the twin lives in the model it appears in the field list and
  round-trips **editably** into Desktop's `fx` dialog, rather than being unreachable injected JSON. The
  alternative rebuild -- putting a column on Legend -- is deliberately not offered even as an opt-in: a
  column is row-level, so it changes the mark grain (one bar becomes stacked segments) and silently
  alters the numbers.
  **The `dataViewWildcard` selector is emitted, and is asserted by test.** Power BI honours a
  `dataPoint.fill` colour expression without it -- validation passes, the visual renders, nothing warns
  -- but evaluates it in ONE context and paints every mark identically. Proven by render: without the
  selector every bar was one colour; with it, Bookcases/Supplies/Tables split from the profitable
  sub-categories. This is a validation-invisible failure mode, so there is a test for the selector
  itself.
  **A scatter grained by several Detail dimensions gets one composite key.** A Power BI `scatterChart`
  takes exactly one field in Values (`maxPerRole = 1`) while Tableau grains marks by the distinct
  combination of every Detail dimension, so the rebuilt report failed to open with
  `PBIR_ROLE_MAX_EXCEEDED` -- losing the whole page. Capping to one pill is worse than the failure: it
  validates clean and renders, having collapsed ~5,000 marks into 3. No positional rule is safe either
  (the identifying dimension is first in the reporter's workbook and last in corpus fixtures
  `0081_correlation_r_squared` and `0090_small_multiples`). The dimensions are instead folded into one
  hidden calculated column -- Microsoft's own documented workaround, *"create a field to concatenate
  your x and y values together ... unique for each point you want to plot"* -- which by construction
  has exactly Tableau's distinct-tuple count. The key is written directly rather than through the calc
  translator on purpose: the translator wraps string concatenation in `ISBLANK` guards that would
  collapse the whole key to BLANK on any blank component, merging exactly the marks it exists to
  separate. Report and model derive the key's name from the same function, so no handshake table can
  drift, and the case that cannot be a column (a grain spanning two tables) fails closed with a
  warning.

- **tableau-migration (skill `2.125.0` -> `2.126.0`): a density map is a heat layer, a pie-on-a-map
  keeps its geography, and a flattened dual-axis map says which layers it lost.** Three map gaps that
  each ended with output that looked finished (issues #111, #112):
  **Density / Heatmap no longer disappears.** The mark was classed "no faithful offline Power BI
  home" and deferred, which produced **no page at all** for the worksheet -- Tableau's own *6-1 Maps*
  sample lost its entire Density Map sheet. azureMap has a native `heatMapLayer`, which is exactly
  this mark, so it is rebuilt: Location on Category, the weighting measure on Size as the layer's
  intensity field, and the bubble layer switched off so points do not double-draw over the surface.
  **A pie-on-a-map keeps its map.** A `Pie` mark over a geography fell through to the chart
  heuristics and emitted a plain `pieChart` with the geography **silently dropped** -- worse than a
  degraded map, because the result looks complete. It now rebuilds as a bubble map and states that
  the per-slice split is what was lost.
  **A dual-axis map names the real loss.** Tableau stacks several mark layers in one worksheet (a
  Multipolygon choropleth plus Pie marks at a finer LOD); one Power BI map has ONE Location well and
  ONE Legend well, so the extra layers are dropped. The only thing reported was *"categorical mark
  colours deferred"* -- true, but a palette detail that reads like a nit, so a reader would never
  guess two of three layers were gone. The collapse is now named explicitly, lists the mark classes,
  says it is a structural loss rather than a styling one, and is ranked **first** among that
  worksheet's warnings.
  Also fixes a latent drop the azureMap switch introduced: `_query_state_complete` still required the
  now-nonexistent `Gradient` role, so a symbol map whose only extra encoding was a colour measure
  would have been judged degenerate and skipped.

- **tableau-migration (skill `2.124.0` -> `2.125.0`): every map is an `azureMap` -- and `shapeMap`
  was not merely deprecated, it was rendering BLANK.** A 4-visual control page in Power BI Desktop
  established, by render rather than inference: `azureMap` draws basemap + bubbles; `azureMap` with a
  data-bound `referenceLayer` draws a real choropleth; and a **byte-identical `shapeMap`** -- what
  this engine emitted for every measure choropleth -- drew **completely blank**, same machine, same
  data, same shared `usa.states.topo` resource. So the main choropleth path was shipping an empty
  visual, while the location-only path shipped a Bing `filledMap` that now raises a *"Bing map visuals
  are going away"* modal in Desktop (issues #106, #112).
  All three map routes now emit `azureMap` with the render-proven encoding: the shading measure moves
  off `Value`/`Gradient` onto **`Tooltips`** (azureMap has neither -- `catalog describe azureMap` gives
  Category/Y/X/Series/Size/Tooltips/PathID/PointOrder -- and Tooltips is a real MEASURE role, so the
  FillRule's `Input` resolves); `referenceLayer` is a **two-entry** array, the layer plus a
  `dataViewWildcard`-selected `polygonFillColor` (one merged entry does not shade); `bubbleLayer.show`
  is forced **false** on a choropleth, without which Azure Maps draws a bubble on every centroid ON TOP
  of the polygons; and `mapControls.defaultStyle` is `blank_accessible` with a `#D9D9D9` stroke, the
  closest of four rendered variants to Tableau's own white-background, no-chrome map.
  Fail-closed where it must be: a SYMBOL map gets no reference layer (its geography is the point, not
  an area) and keeps its bubbles, and a non-US or coarser geography gets basemap + bubbles rather than
  a polygon layer keyed on names we have not proven line up. The choropleth depends on a PUBLIC GeoJSON
  URL, so it now WARNS that an offline or locked-down tenant must re-point `referenceLayerUrl`.
  Ships the reporter's suggested static guard: no route in `_VT_TO_PBIR` may resolve to `map`,
  `filledMap` or `shapeMap`, and `azureMap` must be reachable so the guard cannot pass by emitting no
  map at all. Corpus: **5 `shapeMap` + 1 `map` -> 6 `azureMap`**, 29/29.

- **tableau-migration (skill `2.123.0` -> `2.124.0`): a table that landed related to NOTHING says
  so.** An unrelated dimension does not error -- it returns that table's GRAND TOTAL identically on
  every row of a breakdown, while `relationship_columns_exist`, TMDL deserialization, open and
  refresh all pass. Reported on a Snowflake datasource where `DIM_CUSTOMER` and `DIM_DATE` both
  landed orphaned and the fact related only to the synthetic `Date` table, so a *revenue by region*
  and a *customer segment breakdown* would each have shown one repeated number (issue #107).
  Verified first that a DECLARED join is recovered -- it is -- so an orphan here means the source
  declared none, which is common for a warehouse datasource whose joins live in the warehouse. That
  cannot be invented (a guessed relationship returns a different wrong number rather than the right
  one), so each orphan is reported with the columns it SHARES with the largest other table: evidence
  for a human, never an automatic join. The fact for a given orphan is the largest OTHER table, so a
  table is never compared with itself -- the first cut listed a table's own columns as its candidate
  join keys.
  `duplicate_date_dimension` flags the sharper signal the issue identified: a source-provided date
  dimension landed orphaned while a synthetic `Date` table also exists and took the fact's
  relationship, so the model carries two date tables and the real one is unusable.
  Additive `report["orphan_tables"]`. Found orphans in **7 of 29** corpus workbooks, every one
  previously silent.

- **tableau-migration (skill `2.122.0` -> `2.123.0`): Tableau DECLARES its blend links, and nothing
  read them.** A blended secondary datasource landed in the model related to **nothing but Date**, so
  any visual slicing it returned the whole table's grand total identically for every member --
  measured on Superstore at **4.4x high and constant** (Consumer 13,625 against Tableau's 3,086),
  while the fact's own measures on the very same rows matched Tableau exactly, which is what made it
  hard to see: the chart is half right. The same root cause refused three calcs as *"qualified
  reference ... (unmodeled)"* for a field that WAS modelled, as `Sheet1[Sales_Target]`, 4,603 rows.
  The join keys were never a guess -- Tableau writes them in `<datasource-relationships>` with an
  explicit `<column-mapping>`, and that block was not parsed anywhere in the engine. It is now, and
  a declared blend whose two sides landed as UNRELATED tables is reported with the exact columns
  Tableau declared, de-duplicated across the per-derivation `<map>` entries Tableau writes for one
  date field. Each side resolves through its OWN datasource via `table_map`, never the bare caption:
  `naming` is first-writer-wins on a caption, so resolving `Category` by name puts both sides on one
  datasource and the link reads as a self-join (measured -- the first cut reported nothing).
  The relationship is deliberately NOT invented: a blend is a composite-key link at a chosen date
  grain with no single-column Power BI equivalent, and a wrong relationship returns a number that
  renders perfectly. The refusal reason now names where the field landed and what to add, instead of
  denying data the model contains. Found a **second, previously silent** case in the corpus
  (`0073_comparing_attributes_within_a_dimension`).

- **tableau-migration (skill `2.121.0` -> `2.122.0`): a published datasource is indexed under EVERY
  name it answers to, so a `sqlproxy` workbook binds the model its datasource just produced.** The
  rebind machinery already existed; the JOIN did not fire. A published datasource travels to a
  workbook as a `sqlproxy` stub whose caption is the datasource's DISPLAY NAME on the server
  (`Meridian Sales (Live Snowflake)`), while the exported `.tds` is normally named for the content
  (`MeridianSales.tds`) -- and the catalog was keyed on the FILE STEM alone, so
  `meridiansaleslivesnowflake` missed `meridiansales` and the workbook was skipped with *"relation
  'sqlproxy' has no resolvable columns"* while its datasource sat migrated in the same run.
  Measured at **9 of 38 workbooks (24%)** on a live site (issue #105), and the fraction GROWS with
  governance -- a shared published datasource is the recommended Tableau pattern, so a well-run
  estate is mostly `sqlproxy`.
  The catalog now indexes the file stem, the `.tds`'s own `caption`, and its `formatted-name`. A key
  that two different datasources both answer to identifies neither, so it is **withheld** rather than
  letting whichever migrated last win -- binding the wrong schema would render perfectly -- and every
  lookup treats an ambiguous key exactly like a miss. Verified on the reported shape: a published
  `.tds` plus the workbook that binds it now goes from `0/1` reports bound (`definition_of_done:
  skipped`, no `.pbip`) to **1/1 bound, `pass`, and an openable project**, with the recovered model
  carrying the real Snowflake-bound schema and the published datasource's own calculated field.

- **tableau-migration (skill `2.120.0` -> `2.121.0`): a needs-decision message says WHICH kind of
  problem it is.** *"I cannot see the schema"* and *"I can see it but will not choose a storage mode
  for you"* read identically, and they need opposite responses from the operator (issue #109). The
  rationale now names the state: a `schema-not-visible` case says the columns could not be read from
  anything available offline and asks for a connection or a typed artifact; anything else says the
  schema IS readable and that what is missing is a storage-mode choice.
  The issue's primary case -- a JOINED flat-file datasource reporting *"relation 'Orders.csv' has no
  resolvable columns"* and skipping the workbook -- is resolved by the 2.120.0 multi-table extract
  expansion; verified here on the reported shape (a nested 3-way CSV join, extracted), which now
  types all three relations from their own extract tables and reports `mode=Import`.

- **tableau-migration (skill `2.119.0` -> `2.120.0`): a MULTI-table extract types each relation
  from its own parent instead of skipping the workbook.** A single-table extract collapses onto its
  one materialised table. A multi-table extract correctly refuses to collapse -- folding three tables
  onto one would silently discard two -- but that refusal used to END the story: every relation was
  reported as *"has no resolvable columns"*, the datasource was declared un-typable, and the whole
  workbook's `.pbip` was **skipped** with `definition_of_done: failed`, while a complete typed schema
  and 11,807 rows sat in the bundled `.hyper` the entire time. Measured at **4 of 6** workbooks in a
  standard Tableau training corpus (issue #104), and the shape is common -- an analyst unions a few
  CSVs and publishes an extract.
  The information was already there: the extract files **one parent per table**, each with its own
  typed `metadata-record` columns. What was missing was a per-relation mapping, not a schema. Each
  column-less relation is now typed from its OWN parent, matched on the GUID-stripped, case- and
  punctuation-folded name (`Orders.csv` <-> `Orders.csv_96FB...`), and stamped with that parent's own
  `.hyper` identity so the materialiser reads the right rows per table.
  Every guarantee the original guard protected is kept, and the mapping must be **one-to-one or
  nothing**: an unmatched relation, an ambiguous name, two relations claiming one parent, or a parent
  with no typed columns each leave the relations untouched and degrade to exactly the previous
  behaviour. Verified with those negative controls plus the positive: the reported datasource now
  reports `mode=Import` where it previously reported "needs a storage decision". Corpus 29/29 with
  identical status counts -- the construct does not occur there, which is why it went unnoticed.

- **tableau-migration (skill `2.118.0` -> `2.119.0`): generated M no longer depends on the ambient
  locale.** `Table.TransformColumnTypes` with no culture parses with the locale of whichever machine
  REFRESHES the model, so a dot-decimal source is silently corrupted on a comma-decimal host: issue
  #110 measured `SUM(Sales)` at **1,131,591,720 against a true 2,297,200.86**, with 25/75 numeric
  oracle checks passing while TMDL deserialization, M syntax, the openability self-check and a
  persisted cache were all green. The inflation ratio is `10^decimals`, so it DIFFERS PER COLUMN --
  493x on one, 6,285x on another in the same table -- which is the fingerprint.
  A culture is now pinned wherever the rendering can be **proven**, and only there:
  a CSV this engine wrote (`hyper_reader.write_rows_csv` renders with Python `str()`, which is
  invariant) gets `en-US`; a user's CSV has its convention **read off the file** -- `Csv.Document`
  passes text through unchanged, so the convention is a property of the file, not a guess -- and gets
  `en-US` or `de-DE` accordingly. That sniff parses with the `csv` module rather than splitting on
  commas, because a comma-decimal file writes its numbers QUOTED (`"1.234,56"`) and a naive split
  tears that into `1.234` + `56`, classifying a European file as American: the exact inversion the
  check exists to prevent (measured while building it).
  A **legacy ACE workbook** (`.xls`/`.xlsb`) gets NO culture and a loud warning instead. Its reader
  returns cells already rendered in the host's locale; that locale is not knowable at generation time
  and not observable from within M (`Culture.Current` returns `sourceQueryCulture`, not the Windows
  locale), so no culture can be proven -- and the obvious guess is actively wrong, since pinning
  `en-US` on a comma-decimal host is a no-op that leaves the values corrupt. OOXML `.xlsx`/`.xlsm`
  are unaffected: they store numbers as invariant doubles, so nothing is parsed from text.
  Corpus 29/29: culture coverage `0/70` -> `11` pinned, every remaining unpinned CSV partition has
  **zero decimal columns** (no exposure), and all **3** legacy `.xls` partitions now warn, with no
  false positives.

- **tableau-migration (skill `2.117.0` -> `2.118.0`): an Excel navigation key is decided from the
  ACE identifier, KIND included -- and can no longer carry a `$`.** Tableau records an Excel object
  with the legacy ACE/OLEDB identifier (`table='[Orders$]'`), which `Excel.Workbook` does not
  accept: the model validates, opens and passes the definition of done, then fails at **refresh**
  in Desktop with *"The key didn't match any rows in the table"* -- a failure no structural gate
  can see, and which cost a reported ~90 minutes of an agent sitting on it (issue #108).
  `_excel_navigation` now returns `(item, kind)` from one reading of the identifier and covers the
  forms that previously slipped through: a quoted-then-bracketed `'[Orders$]'` (only one peel was
  applied, so the brackets survived), a workbook-qualified `[Book].[Orders$]` (mangled), a RANGE
  `[Sheet1$A1:D100]` (emitted verbatim -- the range bounds are not a navigation key, so its SHEET
  is), and a **named range** `[MyNamedRange]`, which is not a worksheet at all and needs
  `Kind="DefinedName"` -- navigating it as `Kind="Sheet"` failed identically. A bare `name`
  fallback carries no kind information, so it stays `Sheet` rather than being guessed.
  `_is_excel_path` now recognises the LEGACY binary formats (`.xls`/`.xlsb`) -- #108 was filed
  against a `.xls`, and treating it as not-Excel skipped the sheet decision for exactly the files
  that need it most. Sheet READING is split out into `_is_zip_readable_excel_path`, since a binary
  workbook is not a zip, so header reconciliation still degrades fail-closed instead of mis-parsing.
  Ships with an emitter-level guard that strips a surviving `$` unconditionally: no Excel sheet name
  may contain `$`, so it cannot fire on legitimate input, and it turns "this path normalises" into a
  guarantee that holds however the relation reached the emitter. Corpus 29/29 with **zero** change to
  any navigation key and no `$`-suffixed key anywhere in emitted output.

- **tableau-migration (skill `2.116.0` -> `2.117.0`): a lost Tableau session must not read as
  "this workbook has no dependencies".** The `401002` handling from #97 lived in
  `fidelity_reference` only, so `estate_survey` -- written later on the same transport -- inherited
  none of it, and its failure mode was the exact one it exists to eliminate: a **silent zero**. Once
  a Cloud session died mid-run, every remaining workbook recorded `[]`, which reads downstream as
  *independent, migrate in any order*; `connection_read_errors` reached neither `summary` nor the
  exit code, so the run wrote a clean-looking `survey.json` and **exited 0**. Separately, a `401002`
  on page 3 of 5 escaped `paged_list` entirely and the script died with **no survey written at all**.
  Fixed in the SHARED layer so every current and future script inherits it (issue #99):
  `_http` now returns the synthetic status `0` for a network fault (reset connection, DNS blip, read
  timeout) instead of letting a raw `OSError` escape; `classify_http_failure` sorts a failure into
  transient / session_loss / credential / fatal, testing **transient first** so a gateway `503` whose
  body happens to contain the word "authentication" is still retried; `_http_json` retries the
  transient class with bounded exponential backoff honouring `Retry-After`, recovers a session loss
  through a caller-supplied re-auth hook, and **never retries** `400081` /
  `FederatedDataSourceException` -- Tableau itself cannot query that source, so no retry conjures a
  credential and the remedy is surfaced instead. `fidelity_reference` now sources the code from the
  shared constant so the two cannot drift again.
  `estate_survey` distinguishes **unknown from empty**: an unread workbook is marked
  `dependencies_unknown` and counted as understated rather than independent, a partial listing is
  reported as an INCOMPLETE estate rather than crashing, and the survey carries a `degraded` flag
  that the report text and the **exit code** both honour.

- **tableau-migration (skill `2.115.0` -> `2.116.0`): a Tableau aggregation over a ROW-LEVEL SCALAR
  is `n*k`, not `k` -- and two derivations of one calc are two columns, not one.** A calc built only
  from parameters and literals references no column, so its value is identical on every row; Tableau
  still evaluates it PER ROW and aggregates it on the shelf, so `SUM` is `n*k` while a DAX measure
  evaluates once and returns `k`. Measured on Superstore (issue #103): the `OTE` table reported
  **$142K against Tableau's $5.82M**, and because a `Sum` pill and an `Avg` pill over that one calc
  resolved to the SAME measure reference, the second projection was de-duplicated away -- Tableau's
  *Avg. OTE* column was **missing entirely**, on a visual the engine reported as `rebuilt`.
  The model now emits `SUMX`/`COUNTX` companions for exactly the two derivations that differ
  (`Avg`/`Min`/`Max` ARE the scalar, and keep binding the base measure), and the viz binder selects
  the companion for the pill's own derivation. The companion iterates the calc's **own datasource
  island** -- a global anchor counts another datasource's rows, which renders perfectly and is wrong
  -- and is withheld entirely when the island does not name exactly one landed table, so an
  ambiguous case keeps today's binding rather than inventing a number.
  Root cause of the collapse was broader than the grain error: **the Measure Values path resolved its
  members without any of the model's binding channels**, so every member fell back to the standing
  caption resolution instead of the authoritative model measure. All four channels are now threaded
  through it. Verified across every available workbook: 22 aggregated pills over parameter-only
  scalars in **20 of 83** workbooks, 7 carrying the same scalar at two or more derivations. Corpus
  29/29 with **zero** change to pages, visuals, projections or measure counts (the construct does not
  occur there), and the reported workbook now emits both columns with `SUMX('Sales Commission.csv',
  [OTE (Variable)])` for the `Sum` member.

- **tableau-migration (skill `2.114.0` -> `2.115.0`): a row-level CONSTANT is a column of `k`, not
  `measure = k`.** Tableau evaluates a row-level calc once PER ROW and aggregates it on the shelf,
  so `SUM([Number of Records])` (formula `1`) is the row count -- but a DAX `measure = 1` evaluates
  ONCE and returns 1, discarding row multiplicity entirely. Measured on the corpus: **6 constant
  measures** shipped across **5 of 29 models**, with **7 visuals projecting one**, every one showing
  1 instead of a count. A handler existed but was gated on the NAME (`Number of Records` /
  `Count of <Table>`) *and* only ran when calcs were auto-extracted, so the workbook path -- the main
  path -- never reached it, and a renamed field (measured: `1 (Intake)`) missed it regardless.
  Routing now happens at the shared pre-router chokepoint and is keyed on the **formula**, never the
  name. Literals only: a parameter-referencing scalar stays a measure, because a calculated column is
  baked at refresh and would freeze against the what-if slicer.
  Ships with the paired BINDER change that makes it safe: the viz layer's implicit-row-count channel
  knew only how to find a COUNTROWS measure, so moving the calc left it unbound -- it warned and
  DROPPED the pill, which took a matrix visual's last binding with it and emitted a **page-less**
  report. The model now offers the constant column as an equally faithful target (`row_count_columns`,
  an additive manifest section) and the pill binds it with its OWN shelf aggregation, so SUM -> n*k,
  AVG -> k and CNT -> n all land exactly -- which a single COUNTROWS measure cannot do. A real
  COUNTROWS measure keeps absolute priority, and an object-id count never binds to a constant column.
  Corpus-wide before/after: constant measures **6 -> 0**, and **zero** lost pages, visuals or
  projections -- net **+1 page, +1 visual, +5 projections** that were previously dropped.

- **tableau-migration (skill `2.113.0` -> `2.114.0`): a field belongs to the datasource the PILL came
  from, even when the relation name does not match.** An EXTRACTED datasource carries TWO relations
  for one logical table -- the live one (`Sales Commission.csv`) and the extract materialisation
  (`Extract`) -- and the model keys the live name while a worksheet bound to the extract carries
  `Extract`. The (datasource, relation, caption) key then missed and resolution fell through to the
  BARE caption, which in a multi-table model is claimed by whichever table was written first.
  Measured on Superstore (issue #103): the Commission dashboard's `Sales` bound `Orders[Sales]`
  (2,326,534) instead of `Sales Commission.csv[Sales]` (15,357,898) -- a **6.6x error that renders
  perfectly**, on a page where every sibling projection used the right table, and which the engine
  reported as `status: "rebuilt"` with an empty work order. Adds a DATASOURCE-SCOPED fallback
  between the two, recorded only where the caption is unambiguous within that datasource; where a
  datasource genuinely carries the same caption on two tables the key is WITHHELD rather than
  guessed and the caption is reported as an ambiguous binding (the issue's option (b)), so a silent
  guess becomes visible. Verified globally rather than on the reported workbook: 48 of 83 available
  workbooks carry an extract, and across 10 of them the new path fired **212 times** -- 209
  confirmed the binding the bare caption already gave (hence a byte-identical 29/29 corpus,
  normalised for lineage tags), and **3 corrected a wrong table**, with no case where it disagreed
  with a previously-correct answer.

- **tableau-migration (skill `2.112.0` -> `2.113.0`): the PBIR objects and roles we emit have to be
  the ones the visual actually installs** (issue #100). Every name below was checked against the
  installed visual capabilities (`catalog describe` / `formatting describe-object`), and the result
  measured with Microsoft's own PBIR validator across the corpus:
  **10 of 29 reports had errors -> 3; 32 total errors -> 4** (the remaining 4 are a pre-existing,
  unrelated `PBIR_ROLE_MAX_EXCEEDED`).
  - **Small multiples used an invented role and object.** The role is `Rows` (displayName "Small
    multiples"), not `SmallMultiple`; the card is `smallMultiplesLayout` with `layoutType` /
    `rowCount` / `columnCount`, not `smallMultiple` with `layoutMode` / `maxItemsPerRow` /
    `showEmptyItems`. Both were rejected outright, so **the paning dimension was lost on every
    trellis** and the chart collapsed to one aggregated panel. This also answers the issue's own
    open question: `stackedAreaChart` **does** support small multiples.
  - **`Rows` is an overloaded role, which the rename exposed.** A `pivotTable`/matrix also has a
    `Rows` role — its ROW HEADERS — and installs no `smallMultiplesLayout`, so keying the card on the
    role alone leaked it onto every matrix. The card is now gated on the visual TYPE, from a list
    where each entry was checked for both the role and the object (`scatterChart` and
    `waterfallChart` were in the first draft and have neither).
  - **`labels` is not universal.** `scatterChart` installs no `labels` object — its point labels are
    `categoryLabels` — and `pivotTable`/`tableEx` install neither. The data-label toggle now routes
    by what the target visual installs.
  - **`dataPoint` is not universal either.** `card`, `multiRowCard`, `pivotTable`, `tableEx`,
    `waterfallChart` and `slicer` install no such object, so a mark-colour block aimed at one was
    discarded.
  - **`lineStyles.strokeColor` does not exist on the COMBO charts.** It is real on
    `lineChart`/`areaChart`/`stackedAreaChart` (the original finding stands for those), but emitting
    it on `lineClusteredColumnComboChart` discarded the whole `lineStyles` card — taking `strokeWidth`
    with it.
  - **Dropdown slicers were emitted below the height their own chrome needs.** The floor is 76px
    (header 28 + selector 32 + padding 8/8); below it the header or the selector is clipped and the
    control is unusable. The previous 64.0 was an estimate of where clipping starts; 76 is the
    arithmetic. **The floor now lives at the single point every slicer is built** — it had been in
    the filter-card layout only, so raising the constant fixed the filter slicers and left nine
    PARAMETER-CONTROL slicers (a different emitter) still clipped at 44-75px. A test asserts the
    floor over every dropdown slicer, so a future third emitter is covered the day it is written.

- **tableau-migration (skill `2.111.0` -> `2.112.0`): the report must name a measure the way the
  model does.** 2.108.0 taught the MODEL to strip DAX identifier brackets from a measure name; the
  REPORT still bound calcs by their raw Tableau caption, so the two ends disagreed and the reference
  named an object the model no longer contained. Caught by the binding gate landed one release
  earlier, on a workbook every other gate passes.
  - **The failure was silent, which is why it needed a gate to find.** The report/model cross-check
    dutifully DROPPED the dangling projection, so nothing errored and nothing failed validation — a
    chart simply lost its Y measure. Measured on the Salesforce Nonprofit workbook: references
    dropped went **0 -> 1** at 2.108.0 and **1 -> 0** with this fix.
  - The report now applies the same bracket-stripping rule at the single point it binds a calc by
    caption. It is a deliberate small duplicate rather than an import, because the report layer does
    not depend on the model layer — and a test asserts the two implementations agree on a spread of
    real names, so they cannot drift apart again.
  - Also **moves the 2.111.0 binding gate onto the bytes that actually ship**: it now lints
    `report_parts` AFTER `_crosscheck_report_refs`, which is the `.pbip` the user opens. Linting the
    pre-crosscheck parts reported references that stage had already removed — the same
    first-pass-vs-shipped-artifact confusion that makes `out/reports/` look wrong while the project
    beside it is correct. A dangling reference that survives the cross-check now also raises a
    warning, not just a report entry.

- **tableau-migration (skill `2.110.0` -> `2.111.0`): a gate that proves every VISUAL's model
  references resolve.** `reference_gate` has always proved this invariant for the DAX the second
  compiler writes. Nothing proved it for PBIR — where the same defect is **worse**: a visual bound to
  a column or measure the model does not contain neither errors nor fails validation, it just renders
  **EMPTY**, so it reads as a data problem rather than a binding problem.
  `powerbi-report-author validate` returns 0 errors for it, because a reference to a missing object
  is structurally well-formed JSON.
  - `pbir_lint.lint_visual_model_bindings(parts, surface)` resolves every `Column` / `Measure`
    reference against a `reference_gate.build_model_surface` result. Wired into every migration
    against the **rebound** pass (what actually lands in the `.pbip`), reported as
    `viz_dangling_bindings`. Additive and fail-safe: it records, it does not fail a build.
  - **Two mistakes of my own that the discipline caught before shipping**, both worth recording
    because either would have made the gate lie:
    (1) building the surface from `model_manifest` reported **48 false positives** on one workbook —
    the manifest covers data tables, so it does not know the generated `Date` table's calculated
    columns or the parameter tables. The surface is built from the emitted **TMDL**, which is what
    Power BI actually loads. (2) Reading the visual JSON with a regex reported **8 more** — a
    visual.json escapes non-ASCII as `\uXXXX`, so a measure named `... above Goal ▲` was compared as
    an escape sequence against its decoded model name. The parsed document is walked instead.
  - Proven to FIRE, not just to pass: negative controls corrupt a `Property` and an `Entity` in real
    emitted output and assert each is caught. A check that has never fired proves nothing.
  - **It immediately found two real dangling references** on a workbook every other gate passes,
    including one introduced by 2.110.0's own predecessor — logged for fix, not silently absorbed.

- **tableau-migration (skill `2.109.0` -> `2.110.0`): the trellis collapse, fixed for BOTH spellings
  this time.** 2.105.0 fixed only half of it. Tableau writes "another axis in the same rectangle"
  **two** ways — a FOLD (two axes over different measures) and an **INDEX** (two axes over the same
  measure) — and 2.105.0 gated only the fold. The index branch still set "this sheet is dual-axis"
  unconditionally, so a trellis whose **first column happens to be internally dual** collapsed
  exactly as before: measured on `Engagements by Dimension` (the Staff Capacity dashboard), where a
  single `x-index='1'` pane rebuilt a four-chart block as ONE combo chart spanning the dashboard.
  - **One rule now replaces both branches**, so there is no third spelling left to miss: count
    RECTANGLES as `distinct axis names - folded ones`, and the sheet is a dual axis only when they
    collapse to one. An index needs no subtraction — it repeats a name already counted, and
    subtracting it too would erase a rectangle that genuinely exists.
  - **Why the two spellings are treated differently rather than symmetrically:** across every
    workbook available, exactly ONE sheet pairs an index with 2+ distinct axis names — and it is the
    trellis above. Every real different-measure dual axis (SUM+AVG, pareto, control chart,
    previous-vs-current-year) carries a fold and no index. One long-standing test fixture asserted
    the index-only spelling for a Bar+Bar dual axis; it has been corrected to the fold its own source
    sheet (`0085 "Small Bar (2)"`) actually writes, because relying on a shape Tableau does not emit
    is what let this through.
  - **Verified globally, not on the reported workbook.** A classifier reads the source XML of every
    worksheet in every corpus workbook, computes the rectangle count, and cross-checks the emitted
    output: **27 multi-axis sheets across 9 workbooks — 20 dual, 7 trellis — and zero trellises
    collapse into a wide combo.** Staff Capacity's block went from 1 combo to 4 side-by-side bar
    charts, matching the Tableau render. Suite 4300, corpus 29/29.

- **tableau-migration (skill `2.108.0` -> `2.109.0`): a sentinel date column must not bound the
  calendar** (issue #102, part 2). A small lookup whose date column is a PLACEHOLDER — every row the
  same `1/1/02` or `1900-01-01`, a very common Excel/CSV idiom — is still a related fact date column,
  so it set the calendar's lower bound. Measured on a Superstore rebuild: a 41-row commission lookup
  with **one distinct date** stretched `Date` to `2002-01-01..2027-12-31` (~9,496 rows) for data
  spanning 2021-2024. Nothing is numerically wrong; every Year/Quarter slicer just carries ~21 empty
  years, which is the first thing a business reviewer points at.
  - **Structural test, not a heuristic about which dates "look like" sentinels:** a column whose
    `MIN` equals its `MAX` has one distinct value and therefore carries no range information, so it
    cannot bound anything. Such columns drop out of the bounds; `COALESCE` falls back to the plain
    fold if that would leave nothing, so the calendar is never empty.
  - **Verified in live DAX** against a refreshed model: the unguarded fold over a sentinel returned
    `2002-01-01`, the guarded form returned `2017-01-07` (the real data's start), and an
    all-degenerate span fell back rather than producing an empty calendar. The whole expression was
    confirmed to process — the model refreshes with data.
  - **Only applied with 2+ contributing columns.** A lone column is the only bound there is, so
    excluding it would be meaningless — and skipping the guard keeps every single-fact model
    byte-for-byte unchanged, which confines the blast radius to models that actually have another
    date column to fall back on. Confirmed inert where nothing is degenerate: on a 16-column
    Salesforce model the span was unchanged (2008 comes from real `ClosedDate` data). Corpus 29/29.

- **tableau-migration (skill `2.107.0` -> `2.108.0`): a measure name has to be referenceable in DAX**
  (issue #102, part 1). Tableau names an unnamed calc after its own FORMULA, so measures landed
  called `SUM([Sales])-SUM([Sales Target].[Sales Target])`. TMDL round-trips that and every
  structural gate passes — but `[` and `]` **delimit an identifier in DAX**, so any query that
  references the measure by name dies with `Invalid token, Line 8, Offset 66, ]`, far from the cause.
  Latent while the measure is a stub; a hard failure the moment real DAX is authored for it or a
  visual binds it by name.
  - Brackets are **removed**, not substituted: `SUM(Sales)-SUM(Sales Target.Sales Target)` is what a
    person would have called it anyway. The workbook's original caption is preserved verbatim on the
    `TableauFormula` annotation, and the naming map now keys the ORIGINAL caption to the emitted
    name, so everything that joins on the caption still resolves.
  - Applied at the single point every measure is emitted, so it covers all eleven emission paths;
    the report rows, the cross-calc reference targets (`measure_refs`) and the calc bindings all
    record the name the measure was actually emitted under, so a renamed measure can never be
    referenced as something the model does not contain.
  - Stripping can make two names collide, which is safe because the emitter already de-duplicates
    measure names (2.106.0).
  - **Measured across the corpus: 17 un-referenceable measure names -> 0**, with 29/29 workbooks
    still building. Locked by a test asserting the invariant over the whole `_Measures` table rather
    than the one observed name, plus a purity/idempotence test on the helper.

- **tableau-migration (skill `2.106.0` -> `2.107.0`): a worksheet field belongs to its OWN
  datasource's copy of a table.** On a workbook that consolidates several embedded datasources, a
  dashboard's slicers filtered **nothing** and grouping by a dimension returned the **grand total on
  every row** — silently, because the emitted reference resolves against a real table; just the wrong
  one.
  - **Cause.** Tableau duplicates its datasource per dashboard, so the same physical table arrives
    several times and the model keeps one copy each, suffixing the later ones (`pmdm__Program__c` +
    `pmdm__Program__c (Intake)`). The model->viz join map is built with `setdefault` on the BARE
    Tableau caption, so the first datasource claimed every shared caption and the rest never entered
    the map. A field bound correctly only when its table name happened to be unique across all
    datasources. Measured on the Salesforce Nonprofit workbook (Service Delivery / Intake / Client
    Enrollment and participation / Assessments): `Case` and `caseman__Intake__c` are unique and were
    right, while the Intake dashboard's Program and Owner slicers bound Service Delivery's
    `pmdm__Program__c` / `User`, which have no relationship to `Case` at all.
  - **Fix.** The map now also carries `"<datasource>||<relation>||<caption>"` keys, and a pill
    resolves on its own (datasource, relation, caption) before falling back to the bare caption — so
    single-datasource workbooks are byte-identical. Qualifying by datasource ALONE is not enough: a
    Salesforce model has a `Name` column on Program, User, Contact and Case alike, so the relation is
    part of the key. Built from `model_manifest['columns']` (a LIST — every datasource's columns
    survive there, unlike the collapsed `naming` map) joined to `table_map`, the same surface
    `resolve_consolidated_column` already used.
  - **Verified functionally, not just structurally.** Grouping by `pmdm__Program__c (Intake)[Name]`
    in the rebuilt model now returns 119 / 148 / 591 / 2 / 240 — the Tableau reference values —
    where before every row returned the grand total.
  - **Blast radius:** 34 of 272 emitted visuals changed, confined to the four multi-datasource
    workbooks in the corpus, and every change moves a binding onto the correct copy. It also
    corrected two cross-table mis-bindings that were rendering confident wrong numbers: `Case`'s
    `Status` had resolved to `pmdm__Program__c[pmdm__Status__c]` (different table AND column), and a
    global-filter chart plotted `Orders$[Region]` against `factTable[Sales]`. Corpus 29/29.
  - Also corrects two test fixtures that passed `ds_caption` as a bare string where production always
    passes a dict — they would have masked this.

- **tableau-migration (skill `2.105.0` -> `2.106.0`): two calcs sharing one caption must not become
  two measures with one name.** A duplicate measure name is not cosmetic — TMDL *merges* two objects
  that declare the same name, so the second one's `expression` collides and Power BI Desktop refuses
  to open the project at all: *"TMDL objects cannot be merged because both declare the same property:
  expression"*. The migration reports success and produces a file nobody can open.
  - **Reachable from ordinary workbooks.** Tableau identifies a calc by its INTERNAL name and happily
    lets two calcs share a caption; we name measures by caption. Observed on the Salesforce Nonprofit
    "(Intake Only)" workbook, which carries two copies of
    `IF LAST()=0 THEN RUNNING_SUM([Closed Inbound Referrals])END` — same caption, same formula,
    different internal names.
  - **Two collisions, two answers.** Same caption + same expression is the same calculation, so it is
    emitted once and both binder entries resolve to it. Same caption + different expression is two
    different calculations, so the later is renamed rather than dropped — keeping the model loadable
    without losing a translation.
  - Enforced at the single point every measure is emitted, so it holds for all eleven emission paths
    (translated calcs, stubs, assisted suggestions, approved DAX, workbook table calcs, date-window
    flag measures, visual-calculation base measures, measure-swap aggregations). Locked by a test
    asserting the invariant over the whole `_Measures` table, not just the observed case.

- **tableau-migration (skill `2.104.0` -> `2.105.0`): a side-by-side measure trellis is not a dual
  axis.** Regression shipped in 2.103.0 and caught on the Salesforce Nonprofit "Intake" dashboard:
  a block that 2.102.0 rebuilt as **five side-by-side bar charts** came back as **one combo chart
  spanning the whole dashboard**, all five measures crammed into Y/Y2, horizontal bars rotated to
  vertical columns.
  - **Cause.** 2.103.0 made the measure-axis read orientation-aware, so a worksheet with measures on
    **Cols** started reporting its `x-axis-name` panes. The pre-existing rule "2+ distinct axis names
    means a second measure axis" (2.99.0) then fired on all five. That rule was derived from a real
    `SUM(Sales)` + `AVG(Sales)` sheet and is right for it — but distinct axis names are ALSO exactly
    how Tableau spells a side-by-side measure trellis. The two are otherwise byte-identical: a
    leading blank pane, one `*-axis-name` pane per measure, and **no index on any of them**.
  - **The discriminator.** Tableau writes no `dual-axis` attribute anywhere in a `.twb` — searched
    every workbook in the corpus; the only literal "dual" is in product names. What it writes is
    `<style><style-rule element='axis'><encoding attr='space' ... fold='true' scope='rows|cols'>`
    on the SECONDARY axis: "fold this axis into the other's rectangle". So the axis-rectangle count
    is `distinct axis names - folded ones`; one rectangle with 2+ names is a genuine dual axis, two
    or more rectangles is a trellis.
  - **Verified against every ambiguous sheet in the corpus** (18 of them, across 8 workbooks). Every
    sheet independently identifiable as an overlay carries a fold (pareto, control chart, cumulative
    distribution, the `SUM`+`AVG` sheet, four Previous-Year-vs-Current-Year sheets); every trellis
    carries none (`Intake Details` 5, `Service Provider Details` 6, small-multiples' "In line Bar
    chart" 5, crosstab-with-sparklines 3). A simple measure COUNT would have been wrong — two of the
    trellises have exactly two measures.
  - **Blast radius:** corpus 29/29, **zero visuals changed in place**. 10 combo charts that had
    swallowed a trellis became 40 separate bar charts, across the four workbooks that own those
    sheets; every genuine dual-axis combo survived untouched, as did the whole 2.102-2.104 KPI-card
    and lollipop family (KPI Cards workbook: 22 visuals, 0 changed).
  - **Known limit, unchanged from the 2.102.0 baseline:** a trellis whose columns are each
    internally dual (`Service Provider Details`: 6 names, 3 folded -> 3 rectangles) splits per axis
    NAME, giving 6 charts where Tableau draws 3 overlaid columns. Restoring the baseline is what
    this release does; splitting per rectangle is a separate additive change.

- **tableau-migration (skill `2.103.0` -> `2.104.0`): a dashboard's stored zone rects ARE the layout
  Tableau resolved, and a KPI's trend arrow is part of its line.**
  - **Zone geometry.** `fixed-size` is an INPUT to Tableau's layout engine; `x/y/w/h` are its
    OUTPUT. The solver treated every `is-fixed` child's rect as merely "nominal" and re-flowed the
    container from the pin. Measured on a real 1000x800 dashboard whose four top-strip columns are
    stored at x = 800 / 25400 / 50000 / 74600, each w = 24600 (8 / 254 / 500 / 746 px, each 246
    wide): pixel-measuring Tableau's own render puts the column gutters at 254 / 493 / 740 and the
    map's top rule at 174 — the stored rects are exactly what Tableau drew. The pins read
    166 / 264 / 239, which re-flowed the strip to 166 / 264 / 239 / 291 at 8 / 182 / 454 / 701:
    every column the wrong width, three of the four in the wrong place. The rebuild now lands at
    8 / 256 / 504 / 752.
  - **The pin premise still holds where it was earned.** A pin is kept when it describes an
    INTRINSIC control size (a filter or parameter card, a legend, a text block, an image — things
    that need N pixels to be usable at any dashboard size), and when it simply RESTATES the stored
    rect's own proportion (releasing it there would trade a squeeze-proof ratio for a min-clamped
    approximation of the same answer). It is discarded only for content — a worksheet or a nested
    container — whose siblings already tile the container and whose pin contradicts that tiling.
  - **The trend arrow.** Tableau writes a delta KPI as "vs Last Year: `<number>` `<Arrow Up>`
    `<Arrow down>`", where the paired calcs return a glyph or `""` so exactly one shows. They are
    STRING measures, so they were excluded from the card binding — and thereby dropped entirely,
    silently removing the up/down indicator the KPI exists to show. They are now rebuilt as narrow,
    title-less tiles beside the number, in their authored colour. A numeric format string is no
    longer applied to a string measure.
  - Tableau's `mark-transparency` is an ALPHA BYTE (0..255), not the 0..100 percentage its UI shows
    — confirmed against two renders (stems written `70` draw at 27% opacity; an area fill written
    `91` at 36%; `255` is opaque). It was ignored entirely, so every translucent Tableau mark came
    back at full strength. It now maps to `dataPoint.transparency`.

- **tableau-migration (skill `2.102.0` -> `2.103.0`): a horizontal dual axis is still a dual axis,
  and its member names survive.** Tableau names a pane's measure axis after the shelf the measures
  sit on — `y-axis-name` / `y-index` for Rows (a vertical chart), `x-axis-name` / `x-index` for Cols
  (a HORIZONTAL one). Only `y` was read, so every horizontal dual-axis sheet was invisible to the
  detector: a horizontal lollipop, whose stick and head both live on Cols, came back as an ordinary
  bar chart in the wrong colour with no names and in the wrong order.
  - **Orientation.** The pane axis read now follows the measure shelf. Measures on BOTH shelves (a
    scatter — each axis its own measure, neither a second axis over the other) keeps the
    conservative y-only read.
  - **An `Automatic` head is still a head.** Tableau writes `class="Automatic"` on a pane whose mark
    the author never picked by hand, so a real lollipop reaches us as Bar + Automatic. What
    identifies the second pane as the head without resolving what Automatic means is its SIZE: two
    bars over the same measure where the one drawn second is the WIDER and fully opaque is not a
    chart anyone builds — it would hide the first completely.
  - **No horizontal combo, so no rotation.** Power BI's only combo draws its columns vertically;
    rerouting a horizontal lollipop there would trade a missing dot layer for a wrong orientation.
    The sticks are rebuilt as a `clusteredBarChart` in the source's own colour and the missing head
    layer is disclosed.
  - **An axis pane outranks pane 0 for the mark colour.** A dual-axis worksheet writes a leading
    pane that owns no axis — Tableau's all-panes default, which draws nothing once per-axis panes
    exist — and it can hold a stale colour. The lollipop was painted the cyan pane 0 still
    remembered while both drawing panes said green.
  - **A recent Tableau writes the sort differently.** Newer builds serialise an axis sort as
    `<shelf-sort-v2 dimension-to-sort=… measure-to-sort-by=… direction=…>` instead of
    `<computed-sort using=…>`. Reading only the older spelling shipped those sheets in the model's
    own order with no warning — nothing was missing from the file, it was only unread.
  - **A category header hidden only because its members are drawn inside the marks must stay.**
    Tableau turns the row header off and writes each member's NAME into the bar as a mark label;
    Power BI has no such label (its data labels show the MEASURE), so honouring the hide deleted the
    only copy of the names. The axis is kept — moving the names beside the bars rather than inside
    them — and its auto field-name title is suppressed, because the author showed no header at all.
  - A `shapeMap` now warns that Power BI Desktop ships the shape map visual OFF (Options → Preview
    features): measured, a schema-valid US-state choropleth rendered as an empty rectangle on a
    default install while the same query on a `filledMap` drew a real map. Nothing in the file is
    wrong and nothing the emitter can write turns it on, so it is disclosed instead.

- **tableau-migration (skill `2.101.0` -> `2.102.0`): a KPI title's headline number is found by what
  it RENDERS at, and rebuilt in proportion.** Tableau writes a big-number KPI INTO the worksheet
  title — a static caption run plus a live `<[ds].[Calculation…]>` run. The detector required an
  explicit `fontsize >= 18` on that run, but Tableau omits `fontsize` entirely on a run that uses the
  title's own default. Three KPI tiles in one workbook therefore came out titled with the SHEET NAME
  ("Bar Chart", "Sheet 6") and no number anywhere.
  - **Rendered size, not declared size.** A run's size is now resolved through the same font cascade
    every other element uses (workbook → worksheet → `worksheet-title`, over Tableau's documented
    15pt). A reference qualifies as the headline when it renders LARGER than every static caption run
    ("Days Left In Sales Year" at 12pt over a 15pt number) **or** stands ALONE on its own line
    ("Total Sales" / "2,326,534", both 15pt). An inline, no-larger reference ("Sales for `<Region>`")
    is still a caption with a live token, not a KPI.
  - **The card shows the measure the title names.** Binding was hard-wired to the worksheet's own
    primary measure. That is right only for a view-level table calc (`TOTAL(…)` / `RUNNING_*` — no
    model measure to bind, and a card has no axis for a window to run along), and wrong the moment a
    title names a different metric: "Days Left In Sales Year", whose number is
    `DATEDIFF('day', TODAY(), {MAX([Order Date])})` = 145, was rebuilt as the sheet's SUM(Sales) =
    2,326,534 — a real number, in the right place, measuring the wrong thing.
  - **Several lines are several cards.** A multi-metric title ("Current: `<this year>`" / "vs Last
    Year: `<delta>`") is split on the line breaks the author wrote, one card per line with its own
    label; previously every metric after the first was dropped. A paired STRING measure (Tableau's
    "Arrow Up" / "Arrow down" glyph calcs) is not a headline number and is excluded.
  - **A title-only worksheet is its cards.** A sheet with no rows and no cols — its entire content
    the numbers in its title — classified unsupported and was dropped by the caption path too (the
    text still held a field ref), so a whole tile came out EMPTY. Tableau blanks such a sheet with an
    empty-string calc on Text, and that Text encoding alone was enough to mark it "plottable"; the
    gate is now empty shelves.
  - **Proportion and size.** The card band was a fixed 58% of the zone, which floated a 15pt number
    in a half-empty plate and squashed the chart into the rest; it is now the height of the text it
    holds. Sizes are emitted at what the source renders at instead of being deferred (Power BI's
    defaults are a ~45pt callout and its own title size), the container title is styled from the
    CAPTION runs only, and where the authored zone is too short for a stacked caption+callout both
    scale by the same factor — keeping the authored contrast while fitting the band.
  - **Tableau's "Automatic" number format is not Power BI's.** A measure that declares no format
    renders under Automatic, which suppresses decimals on an aggregate; Power BI prints the raw
    double. A headline read `2,326,534.35` where the source shows `2,326,534`. An unformatted KPI
    callout now carries `#,0` (confirmed against four ground-truth numbers); an authored format still
    wins.
  - Tableau's `Æ` layout sentinel was being counted as a text run by `_parse_title_style`, so a title
    whose real runs all agreed still deferred its styling.

- **tableau-migration (skill `2.100.0` -> `2.101.0`): an axis the author hid has no title either.**
  `visual.objects.<axis>.show: false` suppresses an axis's line, ticks and labels but NOT its title —
  the code assumed otherwise and said so in a comment. Measured on a 300x300 KPI tile whose source
  hides every axis: the plot lost its ticks and labels as asked, and Power BI went on drawing a
  rotated `Sales` caption down the left edge that the source does not have, eating a fifth of the
  plot width. A hidden axis now also emits `showAxisTitle: false`. An authored caption is still
  preserved on the object (`titleText`) so nothing is lost from the file — it is simply not shown on
  an axis the author turned off.

- **tableau-migration (skill `2.99.0` -> `2.100.0`): a table calc transforms the pill it sits on,
  not the first one the sheet happens to declare.** `datasource-dependencies` lists every table-calc
  instance a worksheet DECLARES, which is not the same as every one it PLOTS — Tableau parks a pill
  on Detail (an `<lod>` encoding, drawing nothing) in that same list alongside the pills on Rows and
  Cols. Taking `usages[0]` unconditionally let a parked calc hijack the axis.
  - **Measured.** A sparkline whose Rows shelf holds the RAW `sum:Sales`, with `cum:sum:Sales` parked
    on Detail, was rebuilt with `RUNNINGSUM` over its Y measure: it rendered a smooth cumulative ramp
    where the source draws a jagged monthly series — the wrong shape and the wrong numbers.
  - **The binding.** A quick table calc DOES carry its token onto the pill it transforms
    (`[cum:sum:Sales:qk]`, `[win:sum:Sales:qk]` — confirmed on three real worksheets across two
    workbooks), and `_resolve_field` keeps it as the field's `instance`. The calc applied is now the
    one whose instance IS the shown pill.
  - **Fail-open where the file is silent.** A base pill that is itself a table calc keeps the first
    usage (instances can legitimately differ across encodings); a base pill recording no instance at
    all keeps the long-standing behaviour rather than lose a real calc. Only a PLAIN measure pill —
    which has no calc of its own — is now left exactly as the author plotted it.
  - `test_emit_pbir_projects_visual_calculation_for_a_quick_calc_worksheet` asserted the opposite
    premise ("the quick-calc token does not survive onto the resolved value pill"); it now plots the
    calc's own pill, with the disproving evidence recorded in the test.

- **tableau-migration (skill `2.98.0` -> `2.99.0`): a second measure axis is a second SCALE, and a
  line overlaid with an area is a filled line.** Tableau spells "another measure axis in the same
  pane" TWO ways and only one was being read, so a whole family of dual-axis sheets was
  misclassified.
  - **Detection.** `y-index >= 1` distinguishes two axes over the SAME measure (a line + its fill, a
    lollipop's stick + head), where the name cannot. TWO OR MORE DISTINCT `y-axis-name` values spell
    two axes over DIFFERENT measures, where no index is written. Only the first was detected.
  - **A dual axis is not a trellis.** Both put 2+ measure pills on one shelf, but a trellis gives
    each its OWN pane while a dual axis overlays them in ONE. A `SUM(Sales) + AVG(Sales)` sheet was
    being split into separate charts; pixel-measuring the Tableau render showed both series spanning
    the full plot height from a shared baseline, i.e. one pane. The trellis now declines on a dual
    axis.
  - **Same-family dual axis still needs two axes.** With both panes drawing bars the family split
    finds nothing, yet the point of the second axis is its own scale: the average is ~1/40th of the
    sum, so on one shared Power BI axis it rendered as an invisible sliver where Tableau draws it at
    a third of the plot height. An invisible series is the same failure as an error tile, so the
    secondary-axis measure now goes to Y2 and keeps its scale (Power BI draws Y2 as a line -- one
    mark type traded for both series being visible). Gated on the PRIMARY axis already being a
    column family, because Power BI's combo always draws Y as columns; without that guard a
    three-line running-total sheet came back as stacked columns.
  - **Line + area over the same measure is Tableau's filled-line idiom.** The second pane exists only
    to draw the fill under the first, and Power BI's `areaChart` IS a line with the region below
    filled, so the two-pane construct collapses to one `areaChart`. Reading only the primary pane's
    mark left a bare line where the source draws a filled mountain.
  - Verified end to end: the Area sheet now matches its Tableau reference, the two-measure sheet
    renders both series on their own scales, and the three-line running total is unchanged. Suite
    4285 passed / 6 skipped / 1 xfailed; corpus 29/29.

- **tableau-migration (skill `2.97.0` -> `2.98.0`): a measure trellis fans along the shelf its
  measures sit on.** Tableau splits the pane along whichever shelf the `+`-concatenated measure pills
  are on: measures on ROWS (vertical bars) draw one pane ABOVE the other sharing the category axis at
  the bottom, measures on COLUMNS (horizontal bars) draw them left and right sharing the labels down
  the left. The emitter fanned horizontally for BOTH, so a two-measure column sheet came out as two
  unrelated charts side by side instead of the stacked pair the source draws.
  - The label gutter is now asymmetric, because the two axes need different room. Category labels
    down the LEFT carry member names and need real WIDTH, so that band keeps its double-width slot.
    Category labels along the BOTTOM are a single row of text Power BI draws inside the visual's own
    rectangle, so the stacked bands are simply EQUAL -- matching Tableau's equal panes. Giving the
    last band a double slot there handed a third of the chart to a strip of month names.
  - Confirmed the trellis signature is "two measure PILLS on one shelf", not "two distinct columns":
    `SUM(Sales) + AVG(Sales)` -- the same field under two aggregations -- fans exactly like
    `SUM(Sales) + SUM(Profit)`, in both orientations, each band carrying its own aggregation. Locked
    by test in all four combinations.
  - Verified end to end: the two-aggregation sheet renders as stacked panes sharing the month axis,
    matching the Tableau reference. Suite 4284 passed / 6 skipped / 1 xfailed; corpus 29/29.

- **tableau-migration (skill `2.96.0` -> `2.97.0`): running totals and moving averages accumulate
  again -- and a colour-split one no longer renders blank.** Two separate defects combined to defeat
  every view-only quick table calc on a rebound axis.
  - **The model measure's frozen ordering.** A model-side table-calc measure hard-codes
    `ORDERBY('Orders'[Order_Date])` at BUILD time, and a DAX window can only order by a column that
    is in the query's group-by. The report binds a month truncation to `Date[Month Start]`, so the
    window ordered by a column that is not on the axis and nothing accumulated -- the chart showed
    raw values under a name that promised a running total. The report layer now takes the transform
    back as a Visual Calculation (which follows the visual's own axis by construction) whenever the
    pill carries a view-only token (`cum:`, `rsum:`, `movavg:`, `win:`, `pcto:`, ...), re-pointing the
    base projection at the RAW measure first so the calc cannot double-apply. The swap is all or
    nothing: every step is verified before it is made, because a half-applied rewrite is what blanks
    a visual.
  - **The legend/measure limiter was eating the calc.** That rule exists because Power BI cannot
    cross-join a legend with several measure COLUMNS. A Visual Calculation is an expression evaluated
    INSIDE the visual over a projection that is already present, so it adds no column -- but it was
    being counted as one. Since a reclaimed calc projects its base HIDDEN and the calc VISIBLE,
    dropping `projections[1:]` deleted exactly the visible half, leaving a legend and one hidden
    measure: an empty chart. Measured on two colour-split running-total sheets that both rendered
    blank while the same calc on a legend-free sheet rendered correctly.
  - Verified end to end on a nine-worksheet workbook: the two running-total sheets render smooth
    cumulative curves matching the Tableau reference, and the moving-average sheet loses the spike it
    had been drawing from raw values. Suite 4281 passed / 6 skipped / 1 xfailed; corpus 29/29.

- **tableau-migration (skill `2.95.0` -> `2.96.0`): the author's mark colour reached a chart only if
  it was on a DASHBOARD.** A worksheet is emitted by two paths -- one per dashboard zone, one per
  standalone worksheet page -- and the constant-mark-colour step existed on the dashboard path only.
  A workbook of loose worksheets (or any sheet not placed on a dashboard) therefore rebuilt every
  chart in Power BI's default blue, discarding the colour the author chose, while the SAME sheet on a
  dashboard came out correct. Measured on a nine-sheet workbook: three orange charts all rendered
  blue, and a line lost its stroke colour as well as its fill.
  - The step is now a single shared function (`_with_constant_mark_color`) called by BOTH paths, so
    they cannot drift apart again -- the fix is one call site, not two copies of the same four lines.
  - It now also DECLINES when the sheet colours by an ENCODING. Tableau writes a `mark-color` rule on
    every worksheet, including the inert default emitted when the author chose nothing; once a field
    sits on the Color shelf the marks are coloured per member and that flat value is dead metadata.
    Applying it would repaint a whole segmented chart one colour. `mark_colors` alone cannot guard
    this -- it holds only an EXPLICIT member palette, so a Color encoding with no authored palette
    leaves it empty while still owning the colour.
  - Verified end to end on a nine-worksheet workbook: the three orange sheets render orange, the
    three Segment-coloured sheets keep their per-member greens. Suite 4277 passed / 6 skipped /
    1 xfailed; corpus 29/29.

- **tableau-migration (skill `2.94.0` -> `2.95.0`): a date TRUNCATION is a scalar grain column, not a
  drill hierarchy -- and the scrollbar it caused was hiding half the data.** A Tableau green `t*:`
  pill is `DATETRUNC`: one DATE VALUE per period. It was binding to the shared Date table's Calendar
  drill hierarchy (`Month-Trunc` -> `Year` + `Month`), on the stated grounds that "this is what a
  Desktop-authored rebuild does". Render disproved that. A hierarchy binding is CATEGORICAL by
  construction, so Power BI builds `Year x Month` category slots, refuses a Scalar axis, and PAGES
  the surplus behind a grey scrollbar. Measured on a 45-month 3x3 dashboard: **21 months drawn, 24
  silently hidden, on every one of the nine tiles** -- a data-loss defect that validated clean and
  looked merely cosmetic. It also rendered nested Year/Month headers Tableau never shows for a single
  truncation pill, and left running-total windows ordering by a column that is not on the axis.
  - The generated Date dimension gains a scalar column per truncation grain -- `Year Start`,
    `Quarter Start`, `Month Start`, `Week Start` -- each explicitly `dataType: dateTime` +
    `formatString: Short Date`, because an inferred type lets Desktop treat it as text and the Scalar
    axis silently falls back to categorical. `Day-Trunc` needs none: it IS the key column.
  - Every `*-Trunc` pill now binds to its grain column whatever colour it is. The Calendar hierarchy
    stays correct for date PARTS, which really are drill levels.
  - The pill's own CONTINUITY, read from Tableau's encoding (the trailing role code on the instance:
    `:qk` continuous / `:ok`,`:nk` discrete -- the derivation is `Month-Trunc` for BOTH spellings, so
    nothing else can tell them apart), now decides only how the axis is DRAWN:
    `categoryAxis.axisType: 'Scalar'` for a continuous pill.
  - Scrollbar suppression completed on every cartesian visual: `zoom` (the slider control),
    `general.responsive: false` (responsive layout reflows by paging the axis), and
    `categoryAxis.preferredCategoryWidth: 1D` (the default reserves per-category width). All three
    are required -- builds carrying only the first, then only the first two, were rendered and kept
    every scrollbar. Literal types differ and a wrong one no-ops silently: unquoted `false`, the
    typed double `1D`, the quoted string `'Scalar'`.
  - Verified end to end on `0085_time_series_style_palette`: all nine tiles lose their scrollbar and
    render the full 45-month series. Suite 4275 passed / 6 skipped / 1 xfailed; corpus 29/29.

- **tableau-migration (skill `2.93.0` -> `2.94.0`): the workbook's own colours, on every chart.**
  Four related fixes, all verified by rendering the rebuild and comparing it to the source image.
  (1) **Flat mark colour, every visual type.** A Tableau author who colours the marks without binding
  a field to Colour writes `<format attr='mark-color'>` on a PANE-level `style-rule`, one level
  below the worksheet `table/style` every existing reader looked at, so it was never seen: nine
  charts whose author picked orange, green and cyan all rebuilt in Power BI's default blue. Now read
  for all types (it was previously wired only to the lollipop), as `dataPoint.defaultColor` -- plus
  `lineStyles.strokeColor` on a line/area, because a line's colour IS its stroke. (2) **Per-member
  palettes now apply to LINE and AREA.** They were excluded on the belief that a `dataPoint`
  override "can drop the line"; the adjudicated rebuild of the corpus's own workbook carries a
  `dataPoint` fill on both its line and its area, and what was actually missing was the stroke. A
  three-series green line had been rebuilding as one flat colour. (3) **The report theme carries the
  workbook's palette and canvas.** `dataColors` now leads with every mark colour the workbook
  actually uses, in document order, so a MULTI-series visual -- which takes series colours from the
  theme positionally and which no per-visual override can address -- reproduces the source; and
  `background`/`foreground` come from the dashboard canvas (foreground chosen by luminance for
  contrast, never assumed white), because every visual inherits its default label/axis colour from
  the theme, so a dark page without it renders near-black text on near-black. (4) **A Legend plus
  several measures is refused.** Power BI renders that combination as a full-tile error -- *"There's
  too many columns in the Legend bucket"* -- showing nothing, and it validates clean, so only a
  render catches it. Tableau allows it; the faithful rebuild keeps the legend (a series per colour
  member, which is what the source looks like) and drops the extra measures with a warning. Corpus
  29/29 openable.

- **tableau-migration (skill `2.92.0` -> `2.93.0`): a model-measure rebind is authoritative over
  the caption-keyed `field_map`.** The estate runs the viz stage TWICE -- once bare to build the
  model, once rebound to it -- and only the SECOND pass ships as the openable `.pbip`. In that
  second pass `_apply_override` treated `date_rebound` and `column_rebound` as authoritative but
  NOT `measure_rebound`, so a calc correctly bound to its own model measure fell through to
  `field_map`, which is keyed by CAPTION and whose targets are always model COLUMNS. A quick table
  calc over `[Sales]` is captioned `Sales`, so it was retargeted onto the raw `Orders[Sales]`
  column AND flipped `measure` -> `column`, after which the value-pill aggregation recovery
  re-emitted it as a plain `Sum(Orders.Sales)`. Measured 2026-08-07: the model held a real
  `Sales (running total (cumulative))` measure, the emitted `_Measures.tmdl` contained it, and
  **no visual referenced it** -- the chart showed the raw un-accumulated number and nothing warned,
  because every layer believed it had succeeded. Corpus: visuals bound to a table-calc measure
  **0 -> 4**, 29/29 still openable.

- **tableau-migration (skill `2.91.0` -> `2.92.0`): container backgrounds -- the dashboard canvas
  and each chart's own canvas.** Tableau spells "the background of this whole container" exactly one
  way, a `style-rule` whose `element` is `table`, and the container it hangs from decides what
  gets painted: under `<dashboard>` it is the page canvas, under `<worksheet><table>` it is that
  one chart's canvas. Neither was read. Both are now, by one reader. Per-tile `zone-style` fills and
  the `header` / `pane` / `quick-filter` part fills were already handled -- this adds the
  surface BEHIND them, which is the layer a viewer notices first: a dark workbook previously rebuilt
  entirely white on both layers. The page paints `objects.background` **and** `objects.outspace`
  in the same colour, because Tableau has one background where Power BI has a canvas plus the margin
  shown around it whenever the viewport aspect differs; a chart paints
  `visualContainerObjects.background` with an explicit `show: true`. Both target shapes are
  verified against 59 adjudicated `page.json` files and the corpus's own adjudicated dark rebuild,
  including the two details that fail SILENTLY when wrong -- the quoted hex literal and the unquoted
  `0D` transparency (Power BI's page background is transparent by default, so a colour without it
  can render as nothing). Partial-alpha canvases are declined rather than blended: there is no
  faithful single-hex form, and inventing one would shift every colour composited over it. Corpus
  delta: pages with a background **0 -> 12**, visuals **70 -> 90**, 29/29 still openable, and the
  rebuild verified by rendering it in Desktop and looking at it.

- **tableau-migration (skill `2.90.0` -> `2.91.0`): an unplaced calc borrows addressing from its
  IDENTICAL placed twin.** Tableau recovers a table calc's Compute-Using (partition + order) from
  where the pill sits on a worksheet, so a calc authored but never dropped on a shelf has no
  addressing and stubs as `missing_addressing_intent`. That is right when nothing else knows the
  answer -- but when a sibling calc with a byte-identical formula IS placed, the answer is already
  recovered and sitting in the same model. Measured 2026-08-07: `Rank GMC` and `Rank GMC %` are
  both `rank([Calculation_2768024947633754122], 'desc')` -- same function, same operand, same
  direction -- and the placed one shipped as a live partitioned `RANKX` while its twin shipped as a
  placeholder. On that workbook this takes the deterministic result from **3 stubs to 1**, and the
  remaining stub is genuinely irreducible (it references a field that exists nowhere in the
  workbook); `Weight Raw Score` translated for free once its operand went live. A lookup, not an
  inference -- identical formula + identical addressing is identical DAX by construction, and the
  emitted twin DAX is verified byte-identical to its donor's. Fails closed both ways that could make
  it a guess: a calc with its OWN placement is never overridden, and placed twins that DISAGREE on
  addressing lend nothing rather than being resolved by picking one.

- **tableau-migration (skill `2.89.0` -> `2.90.0`): the input guard now compares BYTES, not just
  names.** `input_manifest.json` already recorded a SHA256 per input and never compared them: its
  collision check keyed on the filename stem alone. So the same file staged twice under different
  names sailed through with `"collisions": []` while the estate scanner migrated BOTH copies --
  and every count in the report doubled. Measured 2026-08-07: an input folder holding
  `<uuid>-Network Ops.twbx` and `Network Ops.twbx` (byte-identical, 116,779 bytes, one SHA256)
  reported 2 workbooks / 40 calcs / 6 stubs where the truth was 1 / 20 / 3, and a reader has no way
  to tell a doubled ledger from a real one. New additive `duplicate_bytes` reports it, and the
  `summary.md` banner names exactly which totals are inflated. Reported, never fatal -- same
  rationale as `collisions`: one ambiguous pair must not abort an estate of 200 assets.

- **tableau-migration (skill `2.88.0` -> `2.89.0`): a transfer-layer UUID prefix no longer
  becomes the asset name.** Chat/Copilot attachments, portal and ticketing downloads and SharePoint
  stamp a canonical UUID on the front of a filename. It is never part of a Tableau author's name, and
  it does real damage rather than looking untidy: 36 characters plus a separator consume most of the
  64-char filesystem-name budget, so the author's ACTUAL name is truncated and a disambiguation hash
  appended. Measured on a real run, `…-Network Operational PowerBI Mock - 24Jul26 ORC.twbx` emitted
  as `0e7f6d6d-…-c13-Network Operationa-ac65b89d` -- the meaningful part of the name survived as the
  word "Operationa". It also defeats the name-based workbook<->datasource rebind index, because two
  attachments of one asset carry DIFFERENT uuids and stop matching each other. Stripped for the
  LOCAL-FILE source only (a live/server name comes from Tableau and is authoritative), and
  deliberately strict -- the canonical 8-4-4-4-12 shape, anchored at position 0, with a separator, and
  only when a non-empty remainder survives, so a date-prefixed name like `2026-08-07-Monthly Report`
  is untouched.

- **tableau-migration (skill `2.87.0` -> `2.88.0`): an untranslated measure is `BLANK()`, never
  `0`.** A stub emitted as `= 0` is a MEASUREMENT: it renders on a card as a confident number, and
  nothing about "CSAT 0%" says the calculation was never migrated. Measured 2026-08-06 on a real
  workbook whose true value was 96%; it was the first thing every downstream agent chased. It also
  poisons dependents, because a year-over-year over a zero denominator yields an infinity or a divide
  error rather than an absence. `BLANK()` renders empty, propagates as absence through arithmetic,
  and cannot be mistaken for data. This is what a stubbed calculated COLUMN has always emitted
  (`generate_calc_column_tmdl`) -- measures were the lone inconsistency. Provenance is unchanged:
  the Tableau formula is still on the `TableauFormula` annotation.

- **tableau-migration (skill `2.86.0` -> `2.87.0`): the Tier-3 work order is REMOVED.** It shipped
  across 2.84.0-2.86.0 and is deleted here, along with its tests and its `SKILL.md` entry, because
  it was never validated against the agent it exists for and the evidence we do have argues against
  it. Recorded rather than quietly dropped, because the reasoning generalises.

  The benchmark was invalid: every measurement was taken on a generic sub-agent, not on the
  downstream migration agent the document is written for. That agent, working from the Tableau
  reference image and the Tier-2 rebuild ALONE, produced a near-perfect dashboard in 1h40m -- it
  independently found and fixed every class the work order reports (placeholder measures, an injected
  percent-of-total visual calculation, missing slicers, sort order, palette). So the document's value
  was never "it finds what the agent misses"; at best it was "sooner", and that was never measured.

  Against that, two harms were measured, both on the generic agent and both intrinsic to the format.
  A section asserting some visuals were "checked and CORRECT" was derived from "the worklist did not
  flag them" -- but the worklist records TRANSLATION failures and has no opinion on visual fidelity,
  and the correlation runs backwards, since a visual that translated cleanly is the one still wearing
  the default theme. It named the three visuals the unaided agent rebuilt. Separately, a document
  listing N items anchors a reader to those N items; the arm given the work order spent 3h and broke
  a working visual, while the unaided baseline did better in half the time.

  **The reference image remains the handoff.** `reference_images.py` and the D7 / STEP 1.6 wiring
  (2.81.0-2.83.0) are untouched: capturing the original dashboard is what the downstream agent
  demonstrably needs, and it is a fact rather than an opinion about what to do with it.

## [0.3.0] - 2026-06-10

A minor, additive release on the collection's own track (independent of any upstream
versioning). The four packaging manifests move 0.2.0 -> 0.3.0; per-skill stamps move
`tableau-migration` 1.1.0 -> 1.2.0 and both `tableau-datasource-profiler` and
`tableau-mcp-landing-zone` 1.0.0 -> 1.0.1. The deprecated `tableau-migration` plugin alias is
retained.

### Added
- **tableau-migration:** additive `relationship_confidence` report artifact — per-relationship
  endpoint connectors, `cross_source` flag, weaker-of-two confidence (ID-key equality scores
  high; coarse string-dimension joins score low with a many-to-many risk note), deduped risks,
  and skipped-relationship reasons. Existing report keys are unchanged.
- **tableau-migration:** additive `calc_coverage` report artifact — per-calculated-field
  bucket (translated / assisted-approved are live; assisted-suggested / stub are inert),
  live-vs-inert totals, and deterministic and live coverage percentages (null when there are
  no calculated fields).
- **tableau-mcp-landing-zone:** `resources/mcp-clients.md` — wiring guide for the three
  code-running Copilots (GitHub Copilot CLI, Claude Code, Cursor) to the deployed or local
  MCP endpoint, plus a Workflow Selector entry.
- Repository convention files: `CHANGELOG.md`, `SECURITY.md`, `.gitleaks.toml`, `AGENTS.md`,
  `CLAUDE.md`, `.cursorrules`, and `.windsurfrules` (original content).
- Credited `microsoft/skills-for-fabric` as the packaging/convention model (structure and
  format only) in `THIRD_PARTY_NOTICES.md` and `CLEANROOM.md`.

### Changed
- **tableau-datasource-profiler:** normalized the `SKILL.md` frontmatter `description` to the
  enumerated "Use when the user wants to: (1)(2)(3)" + quoted `Triggers:` shape used across the
  other two skills; added a `## Related skills` cross-link section. Added the same within-
  collection cross-links to `tableau-migration`.

### Fixed
- **tableau-datasource-profiler:** corrected the README API list (it referenced a "Hyper" API
  the profiler does not use, and had a stray double space).

## [0.2.0] - 2026-06-10

### Added
- Aggregated the three skills (`tableau-datasource-profiler`, `tableau-mcp-landing-zone`,
  `tableau-migration`) into a single standalone collection with marketplace and plugin
  packaging.
- Vendored the Tableau MCP deploy bundle (Azure Bicep/ARM, Copilot Studio swagger, local
  docker-compose) into `tableau-mcp-landing-zone/assets/`.
- Kept a deprecated `tableau-migration` plugin alias so pre-0.2.0 installs keep resolving.

### Changed
- Rewrote `README.md`, `CLEANROOM.md`, `THIRD_PARTY_NOTICES.md`, `requirements.txt`, and all
  four JSON manifests for the aggregated collection (version 0.2.0).
- **tableau-migration** reached content version 1.1.0: workbook inputs, multi-datasource
  selection, and default-direct rebuild with a land-to-Delta fallback.

## [0.1.0] - pre-aggregation baseline

- Initial standalone packaging of the individual skills, before they were aggregated into one
  collection. The migration skill shipped its deterministic safe-subset calc-to-DAX translator,
  TMDL generation from landed schema, and self-contained Fabric deploy; the profiler and MCP
  landing-zone skills shipped their first read-only and deploy workflows respectively.
