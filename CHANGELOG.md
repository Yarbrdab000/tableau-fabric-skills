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
