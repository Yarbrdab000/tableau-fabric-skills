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

### Fixed

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

### Fixed

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

### Fixed

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

### Fixed

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

### Fixed

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

### Fixed

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

### Added

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

### Fixed

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

### Fixed

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

### Fixed

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

### Fixed

- **tableau-migration (skill `2.100.0` -> `2.101.0`): an axis the author hid has no title either.**
  `visual.objects.<axis>.show: false` suppresses an axis's line, ticks and labels but NOT its title —
  the code assumed otherwise and said so in a comment. Measured on a 300x300 KPI tile whose source
  hides every axis: the plot lost its ticks and labels as asked, and Power BI went on drawing a
  rotated `Sales` caption down the left edge that the source does not have, eating a fifth of the
  plot width. A hidden axis now also emits `showAxisTitle: false`. An authored caption is still
  preserved on the object (`titleText`) so nothing is lost from the file — it is simply not shown on
  an axis the author turned off.

### Fixed

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

### Fixed

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
### Fixed

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
### Fixed

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
### Fixed

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
### Changed

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
