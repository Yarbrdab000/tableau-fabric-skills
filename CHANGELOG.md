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
