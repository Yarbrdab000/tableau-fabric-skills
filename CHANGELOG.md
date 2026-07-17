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
- **tableau-migration (skill `1.56.0` → `1.57.0`): Dashboard image & button objects — the report
  rebuilder now rebuilds a dashboard's floating `bitmap` image zones and `dashboard-object` buttons
  (with an `<image-path>`) as native PBIR `image` visuals, packaging each referenced PNG/JPG as a
  registered report resource. Additive and fail-closed — a dashboard with no image/button objects, or a
  workbook whose image bytes were never packaged, stays byte-identical.**
  - **Image & button capture.** `_parse_dashboard` additively captures each `type-v2='bitmap'` image zone
    and each `type-v2='dashboard-object'` button zone that carries an `<image-path>` into
    `db["image_zones"]` (kind `image` / `button`), preserving the authored source ref and pixel geometry;
    duplicate references to the same image are deduped to one packaged resource.
  - **Native image visuals + resource packaging.** `_image_visual` emits a PBIR `image` visual bound to a
    `ResourcePackageItem` (`RegisteredResources`) with no data binding, and `report_json_part` registers
    each image under `resourcePackages` so the emitted `definition/report.json` lists the packaged item;
    raw image bytes are written verbatim under `StaticResources/RegisteredResources/`.
  - **Fail-closed byte handling.** `_resolve_resource_bytes` resolves an image reference by exact key or
    case-insensitive basename; when a workbook's image bytes were not packaged with it, the image object
    is skipped with a `manual attention required` warning (never a broken visual), and a run with no
    `resources` supplied emits no image visuals and no warning.
  - Locked by new hermetic tests (`test_twb_to_pbir.py` §13 dashboard image & button objects), inline-XML
    fixtures only; no customer artifact committed. Plugin mirror kept byte-identical.
- **tableau-migration (skill `1.55.0` → `1.56.0`): Dashboard text objects + "Grow to fit" column
  auto-size — the report rebuilder now captures EVERY dashboard `type='text'` zone (not just the top
  title banner) and rebuilds each as its own PBIR `textbox`, and rebuilt matrices actually grow their
  columns to fit. Additive and fail-closed — a banner-only dashboard stays byte-identical.** Verified
  against the real `New Comcast Test` / `ATTI/ATTR Hierarchy` workbooks:
  - **Every dashboard text zone rebuilt as its own textbox.** `_parse_dashboard` now additively
    captures each content-bearing `type='text'` zone — the section-header caption bars (Director /
    Manager / Supervisor / Technician over each matrix) and the fill-less instruction / metric lines a
    dashboard places over its worksheets — into `db["text_objects"]`, and `emit_pbir` emits each as a
    `_text_object_textbox_visual` (`z=900`) carrying the run's own colour / weight / size over the
    zone's authored fill. The single wide+top title banner is still chosen separately and de-duped out,
    so it is never drawn twice.
  - **rgba-aware fill reader.** New `_zone_background_fill2` accepts Tableau's 8-digit `#rrggbbaa`
    (e.g. a `#5a23b9c1` ~76%-opaque caption bar) and splits it into a `#rrggbb` fill plus a transparency
    percent, so a rebuilt caption keeps its authored see-through look; a colour name / `rgba()` /
    malformed value yields no fill (never a guessed blend). `_zone_run_font` reads the caption run's
    colour, weight, and point size for a faithful rebuild.
  - **"Grow to fit" column auto-size.** `_apply_grow_to_fit` now emits the modern `columnAdjustment`
    enum (`'growToFit'`) alongside the legacy `autoSizeColumnWidth` boolean, exactly as a Desktop-saved
    grid writes them — fixing the "Fit to content" default that made every rebuilt matrix's columns
    render wonky.
  - Locked by new hermetic tests (`test_twb_to_pbir.py` §12 dashboard text objects + the grow-to-fit
    assertion), inline-XML fixtures only; no customer artifact committed. Plugin mirror kept
    byte-identical.
- **tableau-migration (skill `1.54.0` → `1.55.0`): Font/formatting fidelity + §13 geometry fidelity —
  the report rebuilder now carries a worksheet's typed fonts, cell/container shading, faithful slicer
  sizing, and each dashboard's real pixel canvas onto the rebuilt PBIR report. Additive and fail-closed —
  byte-identical on any surface that records no font/fill/geometry override.** Verified against the real
  `ATTI/ATTR Hierarchy` workbook:
  - **Typed fonts from the worksheet `<style>`.** New `_parse_style_font` / `_resolve_element_font`
    resolve a per-element (title / header / pane / cell) font face, size, colour, and bold/italic from the
    Tableau worksheet style, and `_grid_font_objects` / `_title_style_props` stamp them onto the emitted
    grid and title. A silent element seeds the Tableau 9pt app default (so every matrix carries a values
    font), never a fabricated face.
  - **Cell + container shading.** `_normalize_fill_hex` / `_parse_style_fill` / `_resolve_element_fill`
    plus `_fill_style_props` / `_container_background_props` translate a recorded worksheet/zone fill into
    the visual's background, and `_parse_zone_padding` reads a zone's authored inset — emitted only when
    the author actually set a face (no fill recorded ⇒ no `<format>` written, matching Tableau).
  - **Faithful slicer sizing.** `_layout_slicers` lays each dashboard filter card out at its own scaled
    position and show mode with inter-card gaps (`SLICER_PAD_X` / `SLICER_ROW_GUTTER`); a Dropdown card's
    height is translated directly and floored at `SLICER_DROPDOWN_MIN_H` (64) so Power BI never clips the
    control, while a List/checklist card keeps its own scaled height (floored at `SLICER_CTRL_H` = 40).
    `_apply_slicer_format` stamps the compact `SLICER_FONT_PT` (9pt) header/item font.
  - **§13 per-dashboard page geometry.** Each PBIR page is emitted at the dashboard's OWN fixed pixel
    canvas from `<size maxwidth/maxheight>` (`_PAGE_W_OVERRIDE` / `_PAGE_H_OVERRIDE`, `_page_w` / `_page_h`)
    — a 1400×1000 dashboard becomes a 1400×1000 page, a sizeless dashboard falls back to Tableau's own
    1000×800 default (`DASH_DEFAULT_W/H`), and the override is reset after the loop so a standalone
    worksheet page keeps the 1280×720 default (no leak).
  - **§13 shown-state reflow.** `_reflow_worksheets_below_slicers` reproduces Tableau's "Show Filters"
    reflow: when a surfaced slicer band overlaps a worksheet authored at its hidden-state position, the
    sheet is pushed below the band bottom and compressed to fit; a band that sits in its own clear space
    is a no-op (never-regress).
  - Locked by new hermetic acceptance tests (`test_twb_to_pbir.py` §13 geometry + `test_header_banner.py`
    font/formatting), all inline-XML fixtures — no customer artifact is committed.
- **tableau-migration (skill `1.53.0` → `1.54.0`): Rebuilt tables and matrices now default to
  "Grow to fit" column widths.** Every `tableEx` (table) and `pivotTable` (matrix) the report rebuilder
  emits — including the self-service field-parameter table — now carries an explicit
  `objects.columnHeaders[].autoSizeColumnWidth = true`, so grids open auto-sized instead of Power BI
  Desktop's absent-value **"Custom"** (fixed-width) default, which rendered columns clipped or over-wide
  until a user manually flipped each visual to *Layout → Column width → Auto-size behavior → Grow to fit*.
  Grid-only, additive, and merge-safe:
  - **Grid-only.** "Grow to fit" is a table/matrix column-width control, so a new `_apply_grow_to_fit`
    guard emits it for `tableEx`/`pivotTable` only — no cartesian chart, card, or slicer is touched
    (they stay byte-identical).
  - **No fixed-width scaffolding.** The per-column `columnWidth[]` "Custom widths" selectors are
    deliberately *not* emitted — adding them (even empty) is what flips a grid toward fixed widths.
  - **Merge-safe.** Applied via `setdefault`, so a table already carrying a `values` background gradient
    keeps it, and a `columnHeaders` object a later formatting pass adds (header font/colour) or an
    explicit `autoSizeColumnWidth=false` is never clobbered.
  - Wired into both grid builders (`twb_to_pbir._visual_json` and `field_parameter_table_visual`);
    7 regression tests lock the default, the no-custom-width guarantee, and non-grid byte-parity.
- **tableau-migration (skill `1.52.0` → `1.53.0`): Rebuild a Tableau crosstab whose Rows/Columns are
  calculated-field *dimensions* as a real Power BI matrix bound to model columns — instead of collapsing it
  to a single card and dropping the axes — and keep a field-parameter axis (a Tableau field-swap like
  `Choose Date`) bound to its own picker table instead of dangling on the fact. Additive and fail-closed —
  byte-identical on any workbook without calc-dimension axes.** Verified against the real
  `ATTI/ATTR Hierarchy` workbook:
  - **Calc-dimension crosstab stays a matrix.** `twb_to_pbir._resolve_field` now detects a calculated field
    used as a discrete axis dimension (`calc_is_axis`) and binds it to a real model column in the category
    well, so `_visual_type` keeps both axes and emits a `matrix`. Previously every calc pill was forced into
    the measure well, leaving zero dimensions and collapsing the crosstab into one `card` (axes dropped).
  - **Field-parameter axis no longer clobbered.** A calc dimension the model materialised into its OWN table
    (a field-parameter picker lands in e.g. `'Choose Date'[Choose Date]`) is stamped `column_rebound`, and
    `_apply_override` now honours that stamp alongside `date_rebound`, so neither `field_map` nor the
    `model_table` fallback can re-pin it onto the sheet's fact and produce a dangling `Sheet1[Choose Date]`.
    Fail-closed: a calc with no model-confirmed binding is never stamped, so `_apply_override` is byte-for-byte
    unchanged for every other field.
  - **Model-confirmed column binding.** `migrate_estate` reads the built model's TMDL back
    (`_parse_tmdl_columns` / `_column_binding_from_model`) into a `column_binding` manifest naming the real
    `(table, column)` each Tableau calc dimension was materialised into — keyed on the `TableauFormula`
    annotation to include real calc dimensions while excluding model calendar columns, and dropping any
    ambiguous name (warn-never-wrong). Without a manifest hit the calc dimension still resolves as a category
    via the caption fallback, never a measure. Locked by 6 new regression tests.
  faithful Power BI slicers — every filter card at its authored position and show mode, row-level dimension
  calcs kept as sliceable columns, and a discrete exact-date display format bound as an ordinary date.
  Additive and fail-closed — byte-identical on any surface without dashboard filter cards.** Three fixes to
  `twb_to_pbir.py`, verified against the real `ATTI/ATTR Hierarchy` workbook:
  - **Filter band, not a truncated stack.** `_parse_dashboard` now retains each `<zone type-v2='filter'>`
    card's real geometry and Tableau show `mode`, and `_emit_dashboard_slicers` places one deduped slicer per
    card at its own `_scale_zone`-scaled grid position with the mapped show mode
    (`_tableau_filter_mode_to_pbi`: `checkdropdown`/`typeindropdown` → `'Dropdown'`, `checklist`/`radiolist` →
    `'Basic'` List, default `'Dropdown'`). This replaces the synthetic right-rail stack that a page-height
    guard truncated to five, so a band of a dozen-plus cards no longer collapses to five vertical List
    slicers. `hidden-by-user` is recorded but **never** used to drop — it is a Tableau collapsible-container
    show/hide toggle (no Tier-1 Power BI equivalent), so a toggled-hidden band still rebuilds its filters,
    usable, at their authored positions. The standalone worksheet-page surface has no card geometry, so its
    original synthetic-stack slicer path is kept byte-for-byte.
  - **Row-level dimension calc kept as a slicer.** `_parse_filters` previously dropped **every** calc filter;
    it now keeps a row-level DIMENSION calc (an `IF`/`CASE` bucket like `Job Type`) — which lands as a real
    sliceable model column — and warns-and-drops only calcs that roll up to a measure or compare against a
    parameter (`[Parameters]` in the formula, whose value is not a bindable column).
  - **Exact-date display format binds as a plain date.** A discrete "exact date" derivation
    (`MDY`/`MDYH`/`MDYHM`/`MDYHMS` — the full date value shown as e.g. "Month, Day, Year", day-grain-or-finer)
    is an ordinary date column, so `_resolve_field` and `_rebind_date_axis` now bind it like a plain date
    (a normal date slicer/axis, `Date[Date]` key) instead of dropping it as an unsupported derivation. A
    display-format choice on a physical date (e.g. a `Fiscal Month` column shown MDY) is never dropped;
    coarser/unknown derivations stay fail-closed (warn+skip). Locked by 12 new regression tests; the report
    schema is unchanged.
  generated `.pbip` refuse to open, and make the rename atomic so a colliding join key is never left dangling.**
  When a calculated field's name case-collides with a physical column on the same table (e.g. a calc `Order ID`
  alongside a physical `order ID`), the model previously declared both on one table; Power BI treats column names
  case-insensitively, so Desktop failed to load the project (`Item 'Order ID' already exists in the collection`)
  even though the build's openability self-check — which deduped case-*sensitively* — reported success.
  `assemble_model.py` now renames the physical/source-backed column's **model name** to `<name> (source)` while
  keeping its `sourceColumn:` and the M `Table.TransformColumnTypes(...)` header **unchanged** (so the file/DB
  data still loads); the calc keeps the report-facing name, and because the field resolver maps the physical
  caption to the renamed model column, every generated DAX reference and PBIR report binding follows
  automatically. The rename now **also** rewrites the relationship join-key endpoints in `relationships.tmdl`
  (`_rename_relationship_endpoints`), wired into both the estate and explicit-argument code paths, so a
  case-colliding column that is also a relationship key is renamed everywhere at once (atomic, the way Power BI
  Desktop rewrites references) rather than leaving the join pointing at the stale name. As fail-loud backstops,
  `openability_gate.py` now detects duplicate columns **case-insensitively** (`casefold`, matching the PBI
  engine) and adds a gated `relationship_columns_exist` check that flags any relationship endpoint whose column
  no table declares. Additive and fail-closed: no rename is planned in the common no-collision case (output is
  byte-identical), the collision handling is scoped strictly per table (identical column names across unrelated
  tables are legal and untouched), and the new gate check runs only when a relationships part exists and never
  raises.
- **tableau-migration (skill `1.49.0` → `1.50.0`): Harvest unambiguous `<cols><map>` pins that carry no
  `<column>` declaration, so a workbook whose only pointer to a physical column is a unique map pin (with no
  caption declaration for the emit loop to bind) still resolves — killing the Assessments `[Contact ID]`
  bare-row-level stubs. Additive and fail-closed — can only ADD a resolution the metadata layer left
  unresolved/ambiguous, never override a working one.** In the Assessments island, `<cols><map>` pins the
  Tableau caption `[Contact ID]` to exactly one physical `('Contact','Id')`, but there is no
  `<column caption='Contact ID' name='[Contact ID]'>` for `_logical_fields`'s emit loop to bind against, so
  the unique pin was never emitted into the resolver's `logical` bucket; the metadata `cap_to` index alone
  was 2-hit ambiguous, so `resolve_field('Contact ID')` returned `None` → `_CalcError` → stub. `_logical_fields`
  in `connection_to_m.py` now appends, after its emit loop, any single-target map pin that (1) has no
  `<column>` declaration, (2) would not shadow an already-emitted caption/logical-id, (3) is not an
  internal/synthetic token (`Calculation_*`/`Parameter*`/`:`-scoped), and (4) does not collide
  case-insensitively with another harvested pin — emitting NEITHER on any ambiguity. The placeholder
  `tmdl_type` is cosmetic: `build_m_field_resolver` derives the real type from the emitted relation column via
  `_phys_target`, and the `logical` bucket is consulted only on a metadata miss/ambiguity. Live-verified on
  the real `Salesforce_Nonprofit_Case_Management.twbx`: `resolve_field('Contact ID')` flips
  `None → ('Contact','Id','string')`, bare-row-level stubs dropped 10 → 8 (Assessments 4 → 2), the whole
  `Count of Contacts` / `% of Contacts with Assessment` / `Select Metric %` family cascaded to translated
  DAX, `Age` flipped stub → translated, and Service Delivery / Intake / Client-Enrollment counts are
  byte-identical (0 regressions). The 2 remaining Assessments stubs are a separate LOD root, unaffected.
- **tableau-migration (skill `1.48.0` → `1.49.0`): Translate the cross-table FIXED-LOD keystone wrapped in an
  outer conditional in column mode, so the Assessments `Total Score First/Last Assessment` calcs stop shipping
  as bare-row-level stubs — cascade-unblocking their dependent tower. Additive and fail-closed — byte-identical
  unless a column-mode FIXED LOD whose inner aggregate reuses a fact already scoped by the outer conditional is
  translated.** For a shape like `IF [factcol] = [factcol] THEN {FIXED xgrain : MIN(IF … THEN [factnum] END)} END`,
  the cross-table FIXED-LOD column-mode path in `calc_to_dax.py` `_lod_core` derived the inner aggregate's fact
  from `inner_tables = self.tables_used - before`, where `before` is snapshotted after the outer conditional
  parses. Because the outer `IF` already references the same fact, the inner aggregate added no new table, the
  subtraction came back empty, and the `len(inner_tables) != 1` guard fired → the whole calc stubbed. The path
  now falls back to the full deferred-grain scoped set (`frozenset(self.tables_used)`) when the subtraction is
  empty — which, under `defer_xgrain`, is exactly `{outer-conditional fact} ∪ {inner fact}`; size 1 means outer
  and inner share one fact (safe), and size > 1 still fails closed. Live-verified on the real
  `Salesforce_Nonprofit_Case_Management.twbx`: bare-row-level stubs dropped 12 → 10 (Assessments 6 → 4), the two
  keystones emit faithful `RELATED(...) + FILTER(ALL(...))` DAX as calc columns, `Up or Down Assessment Score`
  cascaded to translated, and Service Delivery / Intake / Client-Enrollment counts are byte-identical (0
  regressions). Column-mode-scoped; the bare no-outer-conditional path is unchanged.

  a boolean-aggregating root calc stops shipping as a bare-row-level stub — cascade-unblocking its whole
  dependent tower. Additive and fail-closed — byte-identical unless a measure aggregates a boolean column.**
  Tableau `MAX(bool)` is OR-aggregation (TRUE iff any row is TRUE) and `MIN(bool)` is AND (TRUE iff all rows
  are TRUE), but DAX `MIN`/`MAX` reject a boolean column outright (that is `MINA`/`MAXA`), so the single-field
  `AGG([field])` fast-path in `calc_to_dax.py` previously stubbed the whole calc — and every dependent that
  referenced it collapsed with `bare row-level field [..] not valid in a measure`. The fast-path now folds
  each row to `1`/`0` and iterates, emitting `(MAXX/MINX('T', IF('T'[col], 1, 0)) = 1)` with return type
  `bool` so it can feed an `IF` condition — e.g. the flagship root shape `ZN(IF MAX([bool]) THEN [agg] END)`
  → `COALESCE(IF((MAXX(…) = 1), <agg>), 0)`. The explicit `IF(…, 1, 0)` (never a blank-else) is required for
  `MIN`'s AND-semantics. Live-proven on the real `Salesforce_Nonprofit_Case_Management` workbook: the root
  `Previous Year Active Clients` flipped from a stub to a translated `MAXX` measure, and **all 24 "Previous
  Year" / "Active Client" measures now translate (0 stub)** — the root plus its full dependent tower cascade
  via the existing `measure_refs` fix-point in `_measures_part`, dropping the datasource's bare-row-level stub
  count by 11 with zero regressions across the other datasources. **Fail-closed:** the `dtype not in
  ("number", "text", "date")` reject still fires for a genuinely-unmapped aggregate type, so a mis-typed
  aggregate can only leave a dependent stubbed, never emit wrong DAX; the original Tableau formula is always
  preserved as a `TableauFormula` annotation.
- **tableau-migration (skill `1.46.0` → `1.47.0`): Lay a zero-physical-table, row-invariant foundation calc
  as a real column so its dimension-role dependents stop shipping as bare-row-level measure stubs. Additive
  and fail-closed — byte-identical unless a genuinely row-invariant foundation calc is present.** A Tableau
  calc like `Today = TODAY()` has no physical table behind it (its `tables_used` is empty), so the
  orchestrator's `_build_column_refs` never registered it in `column_refs`. Any dependent that referenced it
  — e.g. `Age = DATEDIFF('year', [Birthdate], [Today])` — therefore couldn't resolve `[Today]`, and even
  after `_reroute_row_level_measure_calcs` correctly reclassified the genuinely row-level `Age` from a
  measure to a column, that column probe still failed because its foundation `[Today]` wasn't laid down.
  A new inline sentinel (`_INLINE_REF_SENTINEL`) closes the gap: `_build_column_refs` now registers a
  zero-physical-table, typed foundation calc as a sentinel `column_refs` entry (`calc_to_dax._row_field`
  intercepts the sentinel and inlines the foundation's DAX directly into the dependent, e.g. `TODAY()`),
  and a fail-closed filter strips those sentinel entries before `measure_resolve` so a measure never
  mis-unpacks one. Live-proven on the real `Salesforce_Nonprofit_Case_Management` workbook: the CEP `Age`
  calc flipped from a `bare row-level field [..] not valid in a measure` stub to a translated calc-column
  `DATEDIFF('Contact'[Birthdate], (TODAY()), YEAR)`, and `Today = TODAY()` now lands as a translated column
  in every datasource. Ships alongside a new pure `calc_graph.py` module (calc dependency DAG + roots-first
  topological layering + conservative row-level form inference) that the roots-first resolver builds on.
  Byte-identical where no row-invariant foundation calc exists; the original Tableau formula is always
  preserved as a `TableauFormula` annotation.
- **tableau-migration (skill `1.45.0` → `1.46.0`): Thread relationships into the row-level measure reroute so
  the cross-table `DATEDIFF` → `LOOKUPVALUE` flip actually lands. Additive and fail-closed — byte-identical
  unless relationships strictly help.** The column translator already knew how to rewrite a measure-role
  row-level calc that spans two related tables (e.g. `ZN(DATEDIFF('month', [Close Date], [Created Date]))`
  where `[Close Date]` lives on a parent record and `[Created Date]` on the child) into a faithful
  calculated *column* that reaches across the foreign key with `LOOKUPVALUE`. But the orchestrator's
  row-level reroute (`_reroute_row_level_measure_calcs` in `assemble_model.py`) invoked that column probe
  **without** passing the model's `relationships`, so the cross-table branch could never find the related
  home table — the probe stubbed `'cross-table'`, the calc was never moved off the measure path, and it
  shipped as an inert `measure … = 0`. The reroute now threads `relationships` through to the column probe
  (signature kwarg + call-site `relationships=all_rels`), so a row-level measure calc whose fields span a
  *single, unambiguous* foreign-key relationship flips from a `= 0` measure stub to a translated calc-column
  that emits the `LOOKUPVALUE` reach. Live-proven on the real `Salesforce_Nonprofit_Case_Management`
  workbook: **`Days to Close` and `Days Assessment since Start Date` both flipped stub → deterministic
  calc-column** (`Case.tmdl` and `caseman__Assessment__c.tmdl` now carry the `LOOKUPVALUE` DAX), with no
  double-emit and no regression to the rest of the estate. **Faithful-or-stub held:** when `relationships`
  is omitted, unavailable, or the field-set does not resolve to exactly one relationship home, the reroute
  is byte-identical to before and the calc stays an honest `= 0` stub with its original `TableauFormula`
  preserved — the more general win is that threading relationships through the reroute unblocks the whole
  measure-role cross-table row-level family, not `DATEDIFF` alone.
- **tableau-migration (skill `1.44.0` → `1.45.0`): Two faithfulness-preserving fixes for the dominant
  "bare row-level field not valid in a measure" stub family — a conformed-hub transit exclusion and a
  sibling-anchored field resolver. Additive and fail-closed throughout.** Both target the same recurring
  failure — a real row-level calc that stubbed because a distinct-count path went ambiguous or a field
  token stayed unresolved — and both are byte-identical no-ops unless they strictly help. **(1) Conformed-hub
  transit-node exclusion (`conformed_hubs`)** — the migrator's auto-generated `Date` calendar dimension is a
  *degenerate transit hub*: every fact table joins its date column into the shared `Date[Date]`, so any two
  facts appear "connected" through same-calendar-date co-occurrence rather than a genuine entity foreign-key
  path. In `_unique_countd_path` that manufactured ≥2 simple paths → ambiguous → `None` → stub, killing the
  cross-table `COUNTD(IF …)` cascade behind current-year-vs-previous-year distinct-client counts. The path
  finder now excludes a conformed hub as an *intermediate* transit hop only (never as the start `C` or the
  matched destination `F`), threaded from `assemble_model` as `conformed_hubs = {date_name}`; a genuine entity
  FK path is therefore never removed, and an empty/`None` set restores prior behaviour exactly. **(2)
  Sibling-anchored field resolver** — a row-level calc that combines a *unique* business field with an
  *ambiguous* system field on the same record (e.g. `DATEDIFF('day', [Close Date], [Created Date])` where
  `[Close Date]` pins exactly one table but `[Created Date]`/`CreatedDate` exists on many) previously stubbed
  wholesale. The resolver now collects the tables its *resolved* tokens pin and, for each *unresolved* token,
  re-resolves it restricted to exactly those pinned tables via a new fail-closed `resolve_in_tables` primitive
  (0 or >1 hits → still `None`); it returns the resolver unchanged whenever nothing anchors, tries the plain
  resolve first so a real hit is never overridden, and forwards `resolve_in_tables` so it composes. Live-proven
  on a real Salesforce workbook (`Salesforce_Nonprofit_Case_Management`): **14 measures flipped stub →
  deterministic (73 → 59 stubs) with zero regressions** — the entire CY−PY difference/ratio and ●/▲/▼ KPI-glyph
  cascade, the Goal-variant family, and a bonus Waitlisted-Engagements `COUNTD(IF …)`. The **faithful-or-stub**
  charter held under test: a `MAX`-of-boolean shape with no faithful DAX form correctly stayed an inert `= 0`
  stub, and so did its dependent subtree — the validation gate, not a guess, is what draws the line. Additive —
  no report key was renamed or removed, every original Tableau formula is still preserved, and deterministically
  translated measures stamp `TranslatedBy = deterministic`.
- **tableau-migration (skill `1.43.0` → `1.44.0`): Island-aware caption resolution unblocks cross-table
  `COUNTD(IF …)` on denormalized object models, plus N-hop cross-table reach and the second-compiler
  validation gates — all additive and faithfulness-preserving.** **(1) Island-aware field resolver** — a
  Tableau `<Field> (<Object>)` caption (e.g. `[Id (Contact)]`) whose object joins into the model *twice* (as
  `Contact` and `Contact1`) was ambiguous under the calc's home data-island and silently dropped, stubbing any
  calc that referenced it. The resolver now matches the `(Object)` token to the single owning join relation and
  re-resolves the physical column within it — fail-closed on a zero- or multi-relation match, so an
  unresolvable caption still stubs rather than guesses. Live-proven on a real Salesforce workbook: the headline
  `Count of Active and Enrolled Clients` (`COUNTD(IF [Stage] = "Active" OR [Stage] = "Enrolled" THEN
  [Id (Contact)] END)`) flipped stub → deterministic once `[Id (Contact)]` resolved to `'Contact'[Id]`,
  cascading through its dependent `Clients per Staff` ratio and the ●/▲/▼ KPI-glyph family — workbook calc
  coverage **55 → 61/154 with zero regressions**. **(2) N-hop cross-table `COUNTD(IF …)`** — the `1.43.0`
  direct-relationship `TREATAS` idiom now also fires when the condition table `C` and counted-field table `F`
  are joined by a *unique simple relationship path* of more than one hop, chaining the key set along the path;
  still fail-closed on any ambiguous, disconnected, or multi-condition-table shape (an unstubbed calc is never
  a guess). **(3) Second-compiler validation gates (rejection-only, opt-in)** — two guard primitives now vet an
  assisted-tier candidate before it may land: a **reference gate** that rejects a candidate referencing a
  nonexistent `[Measure]` or `'Table'[Column]` (catching the `(copy)_NNNN` duplicate-name trap), and a
  **reconciliation oracle** that proves numeric faithfulness of the candidate DAX against the original Tableau
  formula over landed CSV rows (returning INCONCLUSIVE rather than a false PASS outside a tight subset). Policy
  is **guard-only** — the gates never author or alter a candidate and the whole pass is user-opt-in; with no
  guards supplied the landing flow is byte-identical to `1.43.0`. **(4) Auto-incrementing run folders** — a
  `new_run.py` minter lays down `runs/NNNN_<label>/{in,out}` so repeat migrations never collide on a stale
  output dir, backed by a fail-loud guard that refuses to overwrite a prior run's `report.json`. Additive
  throughout — no report key was renamed or removed, every original Tableau formula is still preserved, and
  deterministically translated measures stamp `TranslatedBy = deterministic`.
- **tableau-migration (skill `1.42.0` → `1.43.0`): Cross-table `COUNTD(IF …)` translation via `TREATAS` —
  a distinct count whose condition and counted field live on two *directly related* tables now translates
  deterministically instead of stubbing.** Tableau denormalizes its object model, so a calc like
  `COUNTD(IF [Stage on Intake] = … THEN [Case ID] END)` counts distinct `Case` rows filtered by a condition
  on the related `Intake` table. Power BI keeps the tables normalized, so the engine now emits the canonical
  virtual-relationship idiom — `COALESCE(CALCULATE(DISTINCTCOUNTNOBLANK(F[key]), TREATAS(CALCULATETABLE(VALUES(C[c_key]),
  FILTER(C, <cond>)), F[f_key])), 0)` — pushing the condition table's key set onto the fact table's foreign key.
  `DISTINCTCOUNT` is idempotent to join fan-out, so the result is faithful in either relationship direction.
  **Fail-closed gate (unchanged guarantee):** it translates only when the condition references exactly one table
  `C`, `C ≠` the counted field's table `F`, and there is exactly one direct relationship between them; an
  ambiguous, disconnected, or multi-condition-table shape stays an honest `= 0` stub with the original
  `TableauFormula` preserved — never a guess. **Connector-agnostic:** adjacency comes from the datasource's own
  `relationships`, and the emitted `TREATAS` is pure DAX, so it scales across every connector. **Composes with the
  `1.41.0` inline date-window calc** — a parameter-driven `CreatedDate` window nests inside the `TREATAS` filter.
  Live-proven on a real Salesforce workbook: `Open Intakes` and `Open Intakes in Date Range` flipped stub →
  deterministic (physical API columns resolved live), moving workbook calc coverage **53 → 55/154 with zero
  regressions**. Additive — no report key was renamed or removed; the original formula is still preserved and
  measures stamp `TranslatedBy = deterministic`.
- **tableau-migration (skill `1.41.0` → `1.42.0`): Hidden-column prune — physically drops the hidden schema
  columns a workbook never needs while carving out (keeping, flagged) every load-bearing one, and reports how
  many were pruned. Additive and strictly coverage-preserving.** A live embed emitted roughly 11x too many
  physical columns (~2,567 across 42 tables) because most raw `<metadata-record class='column'>` entries are
  hidden schema columns Tableau itself never surfaces as fields. The prune drops those hidden physical columns
  but **carves out** the load-bearing ones — any hidden column a calculated field references, and hidden join
  keys — so no relationship or calc translation is lost. Proven coverage-neutral on a live Salesforce workbook:
  calc coverage was **byte-identical before and after** (53/154 translated, 101 needing review) while physical
  columns collapsed **2,567 → ~233** (42 tables both). It keys on the hidden flag plus calc references, not on
  any connector specifics, so it scales connector-agnostically. **Telemetry (additive):** each workbook /
  datasource detail now carries `column_prune { columns_emitted, columns_pruned_hidden }`, and those fold into a
  new estate rollup `summary.columns_pruned_hidden_total` — no existing report key was renamed or removed. Emit
  path types the kept-hidden columns from the source schema exactly as before; the original Tableau formula is
  still preserved on every measure.
- **tableau-migration (skill `1.40.0` → `1.41.0`): Deterministic parameter-driven date-window translation
  and a cascade-aware row-level reroute — additive and strictly faithfulness-preserving.** Hardens several
  cross-calc paths so more real-workbook calculations translate deterministically, without ever emitting a
  guess. **(1) Option-D date-parameter modelling** — a `date`/`datetime` Tableau parameter (a free date
  picker, no member list) now emits a *disconnected* single-column date table spanning the model's own date
  range (`CALENDARAUTO`) plus a `[<Param> Value]` `SELECTEDVALUE` capture measure that falls back to the
  Tableau default date literal, giving the parameter a real, slicer-drivable model home (the *capability* a
  Tableau date parameter provides, not its mechanism); fail-closed on a default that is not a Tableau date
  literal (no invented anchor). **(2) Date-window inliner (`inline_calcs`)** — a boolean date-window
  dimension calc (a "within the selected window" flag) is inlined into a consuming `COUNTD(IF …)` measure's
  `FILTER`, so a parameter-driven date-range count that previously fail-closed now emits faithful DAX. **(3)
  Aggregate-over-parameter collapse** — a value-preserving aggregate wrapped directly around a bare parameter
  reference (`MIN`/`MAX`/`AVG`/`MEDIAN`/`SUM([Parameters].[P])`, a Tableau scalar formality) collapses to the
  same `SELECTEDVALUE` parameter measure the bare scalar emits; `COUNT`/`COUNTD`/`STDEV`/`VAR` are excluded
  and stay honest stubs. **(4) Cascade-aware row-level reroute** — the measure/row-level reclassification and
  the cross-calc reference maps now run to a fix-point, so a calc that becomes row-level (or resolvable) only
  after a dependency is rerouted is itself picked up. **(5) Per-island field resolvers** — a consolidated
  multi-datasource workbook now resolves each calc against the disconnected table *island* that best resolves
  its physical fields, so identically-captioned fields across merged Salesforce datasources no longer
  cross-resolve (fail-closed: a retag happens only when exactly one island strictly out-resolves the
  document-order tag). Every shape without a single provably-correct target stays an inert stub with its
  original `TableauFormula` preserved. Report schema additive-only; no existing key renamed or removed.
- **tableau-migration (skill `1.39.0` → `1.40.0`): Two test-only correctness-lock harnesses for the
  translator and the report emitter (no engine or report-schema change).** Additive regression coverage that
  pins current, provably-faithful behaviour so a future change cannot silently drift it. **(T3.2) Concept
  golden corpus** (`tests/test_concept_golden_corpus.py`) drives the real deterministic Tableau→DAX translator
  over the nine migration-crosswalk concepts (LOD re-aggregation, parameter references, glyph-`IF` KPI
  strings, integer date-part extraction, positional/table calcs, …) and asserts, per case, either the exact
  emitted DAX (byte-for-byte) or a fail-closed stub with a stable reason keyword — guarded by a
  badge-consistency forcing function that fails loudly if a translator change flips a concept's
  deterministic / stub / mixed category. **(T3.1) PBIR conformance oracle**
  (`tests/test_pbir_conformance.py`) validates the report emitter's PBIR output against the distilled
  Power BI visual-container vocabulary (visual types, role bindings, structural shape). Both suites are
  offline / stdlib / synthetic and pin *our own* emitted output; the crosswalk DAX and the PBIR vocabulary
  serve only as the validated semantic oracle. Report schema is unchanged.
- **tableau-migration (skill `1.38.0` → `1.39.0`): Faithful deterministic translation of INCLUDE / EXCLUDE
  level-of-detail expressions.** `INCLUDE` and `EXCLUDE` LODs were previously fail-closed because, unlike a
  `FIXED` LOD (whose grain is datasource-absolute), their grain is *view-relative* — it depends on the
  dimensions on the worksheet, not on the `.tds`. Two shapes now translate to provably-faithful, filter-context
  DAX: a re-aggregated `INCLUDE` — `AGG({ INCLUDE [d] : inner })` — emits `AGGX(SUMMARIZE('T', 'T'[d]),
  CALCULATE(inner))` (`SUMX`/`AVERAGEX`/`MINX`/`MAXX` per the outer aggregate), precisely "add `d` to the view
  grain, then roll up"; and a bare `EXCLUDE` — `{ EXCLUDE [d] : inner }` — emits `CALCULATE(inner,
  REMOVEFILTERS('T'[d]))`, precisely "the view grain minus `d`" (multi-dimension → `REMOVEFILTERS('T'[d1],
  'T'[d2])`), the same view-adaptive fidelity class as the already-shipped bare `FIXED` → `ALLEXCEPT`. Strictly
  fail-closed on every shape that has no single provably-correct target: a bare `INCLUDE` (genuinely needs an
  outer aggregation the deterministic tier will not guess), a re-aggregated `EXCLUDE`, a no-dimension
  `INCLUDE`/`EXCLUDE`, any `INCLUDE`/`EXCLUDE` nested inside another LOD (or any LOD nested inside their inner
  expression), and `INCLUDE`/`EXCLUDE` in a calculated column (they are view-relative, so there is no row-level
  column form). `FIXED` behaviour is unchanged, and all fail-closed reasons route coherently through the
  translation router's `MISSING_OUTER_AGGREGATION` handoff category. Report schema is additive (no keys renamed
  or removed).
- **tableau-migration (skill `1.37.0` → `1.38.0`): Approved-keystone nested-calc cascade — author one
  irreducible base, get the whole dependent chain deterministically.** When a nested calc→calc→calc chain
  bottoms out at an *irreducible* base — a `WINDOW_`/`RUNNING_` table calc or other construct the
  deterministic tier cannot render at datasource scope — the whole downstream subtree used to stub, even
  though every dependent's own structure is faithfully translatable. Authoring **just that base** via
  `approved_calc_dax` (the human / second-compiler tier) now seeds it into the measure-side cross-calc
  reference map **before** the deterministic fix-point, so every dependent that stubbed *only* because that
  base was an untranslatable measure now translates deterministically by referencing the approved measure —
  and so do *its* dependents, to any depth. The emitted reference is an ordinary DAX measure reference,
  exactly mirroring Tableau's own calc→calc structure. This turns "the second compiler must author the
  entire nesting chain" into "author only the few irreducible keystones; the rest fall out for free."
  Faithful and fail-closed: a dependent carrying **any other** unsupported construct still stubs; the
  approved base's output type defaults to `number` (the dominant aggregate-measure case) and can be declared
  via an additive `"dtype"` key on the dict form of an approval (`{"dax": …, "dtype": "boolean"|"text"|
  "date"|"number"}`, with friendly synonyms folded onto the translator's canonical tokens), so a boolean
  keystone branched on in an `IF`, or a text keystone concatenated by a dependent, cascades correctly — and
  a mis-typed keystone can only make a dependent **stub**, never emit wrong DAX. Inert with no approvals
  (byte-identical output). Measures-side only; the column-mode dimension chains already cascade. Report
  schema is additive (no keys renamed or removed).
- **tableau-migration (skill `1.36.0` → `1.37.0`): Faithful emission of Tableau's stock
  `Number of Records` field — a Sum-aggregated calculated column of 1s.** Tableau's generic 1-per-row
  row-count field (the classic `Number of Records`, or the modern `Count of <Table>`) is defined as the
  literal `1`; dragging it into a view auto-sums that column of 1s to the table's row count. It carries
  `role=measure`, so the migrator previously emitted a nonsense `measure 'Number of Records' = 1` — a measure
  that in Power BI **always returns 1**, never the row count. It now lands faithfully as a real calculated
  **column** `= 1` on the fact table, typed `int64` with `summarizeBy: sum`, so it aggregates to the row count
  exactly as it does in Tableau. Detection is fail-closed: the reclassification fires only when the formula is
  the literal `1` **and** the field carries the stock name, so a user field that merely borrows the name but
  computes something else — or a plain `= 1` measure with a different name — is left untouched. The stock field
  is deliberately **not** registered as a resolvable model column, so a measure's `SUM([Number of Records])`
  keeps using the compiler's existing `COUNTROWS` path (which fails closed on an ambiguous multi-table count).
  Purely additive: a corpus audit over the TableauChallenge solutions shows **0 changes** to any other calc
  (17 translated / 12 stub / 14 addressing-dependent, unchanged). Report schema is additive (no keys renamed
  or removed).
- **tableau-migration (skill `1.35.0` → `1.36.0`): Calc→DAX compiler depth — calculated columns are now
  visible to the measure resolver (nested-LOD-over-a-calculated-dimension).** Additive and faithful-or-stub —
  every measure that already translated is byte-identical; the new work only turns former stubs into
  provably-correct DAX (or leaves them honest stubs). A calculated column is a genuine row-level model column,
  so a measure that references one should resolve to it. Previously the measure-mode resolver knew only
  **physical** columns, so a measure whose FIXED-LOD grain was a calc dimension — e.g.
  `{ FIXED : AVG({ FIXED [Order Date (Months)] : SUM([Sales]) }) }` where `[Order Date (Months)]` is itself a
  calculated column — stubbed with `unresolved/ambiguous LOD dimension`. The orchestrator now layers the
  faithfully-emitted, single-home, typed calc-column identities **under** the datasource resolver it hands the
  measure path: the base physical resolver always wins (so every previously-translated measure is byte-for-byte
  unchanged and the augmented resolver is inert when a datasource has no calc columns), and the calc-column
  identities fill only the references the base could not resolve. The nested LOD then re-aggregates over the
  calc column's grain (`AVERAGEX(SUMMARIZE(...))`) exactly as it would over a physical dimension. Fail-closed is
  preserved: a **bare** row-level calc-column reference in a measure still stubs (a measure must aggregate), and
  the existing type checks are untouched (e.g. `AVG([date column])` still errors). Report schema is additive
  (no keys renamed or removed).
- **tableau-migration (skill `1.34.0` → `1.35.0`): Calc→DAX compiler depth — cross-calc token-keying
  cascade in calculated-column mode, and sub-day `DATETRUNC`.** Additive and faithful-or-stub — every calc
  that already translated is byte-identical; the new work only turns former stubs into provably-correct DAX
  (or leaves them honest stubs). This is the big cascading lever for nested/chained calcs: resolving a
  keystone reference unblocks its whole downstream subtree. Highlights:
  - **A calculated column may now reference a sibling calc by its internal `[Calculation_*]` token, not just
    its caption.** Real Tableau workbooks reference auto-named calcs by their internal id, but the
    column-mode sibling-resolution fix-point keyed its reference table only by caption — so a row-level calc
    that referenced a sibling by token failed closed with `unresolved/ambiguous field [Calculation_…]` even
    though the sibling itself translated cleanly. Each single-home, typed, deterministic sibling translation
    is now registered under **both** its caption and its internal-name token (mirroring the measure-side
    cascade, which already dual-keyed). The stored reference is always the sibling's real
    `'Table'[Caption]` column, so the emitted DAX is correct regardless of which key matched. Fail-closed is
    unchanged: only a faithful single-home typed calc is a valid reference target, so a token that points at
    an untranslatable sibling still stubs.
  - **`DATETRUNC` now supports the sub-day units `hour` / `minute` / `second`** — truncation to
    `(DATE(y,m,d) + TIME(h,[m],[s]))`, parenthesized so it composes safely inside `DATEADD`. Combined with
    the token cascade above, this completes real three-deep date chains (e.g. truncate to the hour → add an
    integer-minute interval → round to the nearest 15 minutes) fully deterministically. `quarter` / `week`
    truncation continue to fall back (they depend on the workbook's start-of-week setting).

  columns, the nested-argmax cascade, and Tableau's stock `[Number of Records]` field.** Additive and
  faithful-or-stub — every calc that already translated is byte-identical; the new work only turns former
  stubs into provably-correct DAX (or leaves them stubs). Highlights:
  - **FIXED LODs now translate in calculated-column (row-level) mode** — a bare `{FIXED d,…: AGG(…)}` or
    table-scoped `{AGG(…)}` level-of-detail expression previously failed closed in a row-level column calc.
    Because an LOD value is row-invariant within its declared grain, it emits the *same*
    `CALCULATE(inner, ALLEXCEPT/ALL('Table'))` scalar as measure mode; and since a calculated column carries
    no visual filter context, the column form is the more faithful home (it avoids even the measure-mode
    divergence-under-a-dimension-filter caveat). A *top-level re-aggregation* of an LOD (e.g. `SUM({FIXED …})`)
    is a viz-grain aggregate and still falls back. This is the symmetric inverse of the 1.33.0 row-level
    pre-router, and it makes measure-labelled-but-genuinely-row-level **nested-FIXED-LOD argmax/argmin**
    calcs (`IF {FIXED d : MAX({FIXED d,e : agg})} = {FIXED d,e : agg} THEN [dim] END`) resolve
    **deterministically** as columns rather than being handed to the assisted idiom registry.
  - **Date arithmetic in calculated columns** — `[date] - [date]` yields the day difference, and
    `[date] ± N` / `N + [date]` shift a date by N days (DAX stores dates as day serials, so these are exact).
    Disallowed combinations (`number - date`, `date + date`, text arithmetic) fail closed, matching Tableau.
  - **`[Number of Records]` synthetic field** — Tableau adds a stock 1-per-row field named
    "Number of Records" to every datasource (renamed to a per-table "Count of `<Table>`" auto-field in
    2020.2+). `SUM`/`COUNT` of it now compiles to `COUNTROWS('<Table>')` and a bare row-level reference to
    the constant `1`. Matched narrowly to the reserved legacy caption and gated fail-safe: it emits only when
    the caption does **not** resolve to a real model column (a genuine same-named column always wins) and the
    counted table is unambiguous (the single table already in play, else the model's sole table, else it
    fails closed). The modern per-table count continues to arrive via the existing object-id `COUNT` path.
  Additive and faithful-or-stub — every calc that already translated is byte-identical; the new work only
  turns former stubs into provably-correct DAX (or leaves them stubs). Highlights:
  - **Arithmetic operators** — Tableau `%` (modulo) now compiles to DAX `MOD(a, b)` (integer-remainder,
    divisor-signed — matching DAX exactly), and `^` (power) to `POWER(base, exp)` (right-associative, and
    honouring Tableau's quirk that unary negation binds tighter than power, so `-x^2` = `(-x)^2`).
  - **Date literals** — `#…#` date/datetime literals compile to `DATE(y, m, d)` (plus `+ TIME(h, mi, s)`
    when a time is present). Only unambiguous forms are accepted — ISO `#YYYY-MM-DD[ HH:MM:SS]#` and long
    `#Month DD, YYYY#`; locale-ambiguous dash/slash forms (e.g. `#01-02-2000#`) **fail closed** rather than
    guess a day/month order. Works inside comparisons and date functions (`DATEDIFF`/`DATEADD`/…) in both
    measure and calculated-column mode.
  - **Row-level calc pre-router** — Tableau types a purely row-level numeric calculation (e.g.
    `- [Profit]`, `[Sales] * 2`) as a *measure* by its output type, which then failed closed in measure
    mode. The assembler now reclassifies such a calc onto the calculated-column path — faithful, because a
    row-level column summed in the visual yields the same result and still respects filter context. The
    reroute is conservative: it fires only when measure mode stubs with the specific "bare row-level field
    not valid in a measure" reason **and** calculated-column mode renders the calc, and it defers to any
    human-/second-compiler-approved measure landing (`approved_calc_dax`) and to field-parameter/flag-source
    pins, so an approved measure is never silently demoted.
- **tableau-migration (skill `1.31.0` → `1.32.0`): Tiered viz-fidelity reporting — the per-worksheet
  rebuild report now carries an additive `tier` field so a visual that renders but merely defers a
  fail-closed feature is no longer conflated with an outright failure. Strictly additive: the existing
  `status` (`rebuilt`/`warned`) is unchanged and every existing consumer/counter is byte-identical.**
  Each `viz_fidelity` row gains `tier` ∈ `rebuilt` (clean), `rebuilt_with_deferrals` (a visual rendered
  but a documented faithful-or-stub deferral was recorded — a dropped aggregate/measure filter, a
  date-grain approximation, or a default colour palette), `degraded` (a rendered visual whose warning is
  a genuine problem, not a known deferral), or `empty` (no faithful visual emitted). Pass/fail counting
  is intentionally left unchanged; making the summary stop degrading on deferral-only visuals is a
  separate, behaviour-changing follow-up.
- **tableau-migration (skill `1.30.0` → `1.31.0`): Report visual-fidelity — a Tableau workbook's brand
  colour and dashboard header now carry through to the rebuilt Power BI report. Additive and
  never-regress: a workbook with no header banner and no brand signal emits a byte-identical report,
  theme, and page set to the prior baseline.** Highlights:
  - **Report theme** — `report.json` now references a bundled `RegisteredResources` custom theme whose
    `dataColors` lead with the Tableau 10 palette in exact order (so a two-series chart rebuilds
    blue+orange, never a Tableau-20 interleave), with Tableau 20 trailing as the multi-category
    fallback. `report_json_part()` with no argument stays byte-identical, and the thin `.pbip` shell
    never gains a dangling `customTheme`.
  - **Workbook brand colour** — when a dashboard carries a branded header band, its fill leads the
    theme `dataColors` (de-duplicated, case-insensitively, against the Tableau tail) so auto-coloured
    visuals rebuild in the workbook's brand; `brand=None` is byte-identical to the default palette.
  - **Dashboard header banner** — the full-width filled title zone at the top of a Tableau dashboard
    rebuilds as a schema-shaped PBIR `textbox` visual carrying the brand fill and the white title text.
    Only the real top-of-page header is selected (a narrow tinted callout or a low footer box is
    ignored); a bannerless dashboard emits no textbox. Verified end-to-end against the real
    5-dashboard Salesforce Nonprofit Case Management workbook (5 crimson banners, brand `#ac145a`).
- **tableau-migration (skill `1.31.0`): Capability-matrix hardening — four additive, never-regress
  faithfulness improvements from a field capability report. Each is faithful-or-stub: it either emits a
  verified-correct shape or leaves the honest review path untouched, and every default-off/unset case is
  byte-identical to the prior baseline.** Highlights:
  - **RANK quick-table-calc → Power BI visual-calculation `RANK`** — a `Rank` quick table calc rebuilds
    as a native visual-calculation `RANK` with the faithful tie rule and direction (Tableau Competition
    → `SKIP`, Dense → `DENSE`, default Descending → `ORDERBY DESC`). Modified/Unique tie modes have no
    faithful native equivalent, so they fail closed to review (never a wrong ranking) — the same posture
    the measure path takes for `RANK_MODIFIED` / `RANK_UNIQUE`.
  - **Sibling calculated-column cascade** — a bare `[X]` that names another calculated column being
    created on the same datasource (absent from the source metadata) now resolves to `'Table'[X]` with
    its recorded type, the column-mode peer of the measures' `measure_refs` fix-point. Fully type-checked
    and single-table-guarded, so a row-level column that would span tables still fails closed.
  - **Extract connector detection** — a bare/federated `.hyper` extract whose connector class did not
    set the extract flag now routes to the offline Import home (rather than dying at needs-decision),
    matching the established extract-backed Import path.
  - **Dropped aggregate/measure filter disclosure** — a worksheet filter on an aggregate (`SUM(Sales)`)
    or calculated measure that has no faithful slicer mapping is now rolled up into an additive
    `measure_filters_needs_review` report entry (per workbook), so a number-changing filter left to
    review stays visible even when the per-worksheet warning is collapsed. Emits nothing into the PBIR;
    filters the model faithfully rebuilt as visual-level measure filters are excluded.
  - **Docs** — the SKILL.md LOD note now states the real boundary: a FIXED LOD is translated, while
    INCLUDE/EXCLUDE and non-additive LODs stub to review.
- **tableau-migration (skill `1.29.0` → `1.30.0`): Model Object Harvest — three additional Tableau
  object kinds now rebuild as faithful semantic-model objects. Additive and backward-compatible; the
  linguistic emission is opt-in and default OFF, so an omitted flag is byte-identical to the prior
  baseline.** Highlights:
  - **Groups → `SWITCH` calc columns** (§3.1): a `<calculation class='categorical-bin'>` group over a
    base column becomes a string calc column `SWITCH(TRUE(), <col> IN {..}, "label", .., <tail>)`, where
    the tail is the base column itself when the group passes unlisted values through (`new-bin='true'`)
    else `BLANK()`. Default-ON (reuses the proven calc-column generators).
  - **Numeric bins → `INT()` calc columns** (§3.2): a `<calculation class='bin'>` becomes
    `INT((<col> - peg) / size) * size + peg` (INT floors toward negative infinity, matching Tableau's
    bin flooring). The width is a literal `size=` or the DEFAULT of a referenced `size-parameter`; an
    unresolvable width leaves an inert `= BLANK()` stub plus a skip reason (a width is never assumed).
    Default-ON.
  - **Field captions → Q&A `cultureInfo` synonyms** (§3.3, `scripts/linguistic.py`, net-new): each
    Tableau `<column caption='...'>` whose caption differs from its model column contributes a Power BI
    Q&A term, packaged as a `definition/cultures/<lang>.tmdl` linguisticMetadata part plus a single
    `ref cultureInfo` line on `model.tmdl`, with an additive `report["model_objects"]["linguistic"]`
    audit. **Opt-in via `migrate_tds_to_semantic_model(..., emit_linguistic=True)`, default OFF** — a
    malformed culture part fails model load, so it ships behind the flag until a Power BI Desktop /
    Tabular Editor round-trip certifies the byte-shape in the target environment. When no caption differs
    from its column, nothing is written and the model is byte-identical.

- **tableau-migration (skill `1.28.1` → `1.29.0`): native-capability spec integration — a batch of
  additive, backward-compatible engine behaviors that reproduce hand-done agent adjudication as
  deterministic build output. All opt-in seams default OFF so an omitted flag is byte-identical to the
  prior baseline.** Highlights:
  - **Second-compiler landing driver** (`scripts/second_compiler.py`, net-new): `land_second_compiler` /
    `land_report` seed keystones from `authored` overrides **plus** the engine's own idiom detectors,
    gate-and-land each via `check_candidate_dax`, then fix-point-cascade every dependent calc back through
    the native translator (≤12 rounds, each still gated) so a measure-of-measure stub chain lands whole
    off one keystone. Returns ONLY the keystone-dependent supplement, preserving `deterministic`
    provenance for purely-deterministic calcs. Wired as an **opt-in** through `migrate_workbook`
    (covers estate + standalone), `migrate_estate`, and the CLI (`--second-compile` / `--author FILE`,
    author implies second-compile); merged UNDER `approved_calc_dax` so a human-approved entry always
    wins; fail-closed; stamps an additive `second_compile` record per workbook.
  - **Assisted idiom detectors** (Spec 7): first/last-by-date and year-gated (`IF YEAR([d]) = 2023 THEN
    [x] END` → `CALCULATE(SUM(...), KEEPFILTERS(YEAR(...) = 2023))`) idioms are recognized and emitted as
    gated DAX, fired only when the deterministic translate returns falsy.
  - **Stub triage** (Spec 5): keystone/cascadable classification (`_stub_shape` + `_triage_stubs`) with an
    additive `triage` key on the translation-handoff artifact, so the assisted tier knows what to author
    first.
  - **Openability + de-dup + local-CSV parameter threading + consolidated table map** (Specs 1, 2, 3, 6):
    openability self-check surfaced on the build detail; datasource de-dup by `(parent, model_name)`;
    workbook parameters parsed and threaded on the local-CSV consolidation path; a consolidated
    `table_map` built from combined descriptors.
  - **Visual-layer latent-dimension candidate seam** (Spec 9a) for report-side card recovery.
- **tableau-migration:** **`migrate_estate.py --scan` — a read-only pre-build discovery gate that stops a
  published-backed workbook from ever being built to an empty report.** Prior runbook guidance only
  surfaced a missing published datasource *after* a build (in `report.json`), so an agent could run the
  build, see an empty pass, and improvise or ask instead of fetching the datasource. `--scan` runs no
  build, needs no credentials, and unzips nothing: it reads each workbook's binding, writes
  `<out>/scan.json` (`{datasources_present, workbooks:[{name,kind,published_ds_name,datasource_present}],
  missing_published_datasources}`), prints a per-workbook summary plus an `[ACTION]`/`[OK]` line, and
  **exits non-zero while any published datasource is missing**. Presence is computed with the *same*
  `_norm_ds` key the build uses to populate its datasource catalog, so `datasource_present` means exactly
  "the build will find it and rebind the workbook to it." `SKILL.md` gains a **STEP 1.5** that runs the
  scan before STEP 2 and hard-gates the build until it exits `0` (fetch each missing name into `.\in`,
  re-scan, repeat), plus a Checkpoint 1.5; the note block, confirmation ledger, STEP 1 download-path
  guardrail, capability table, and Checkpoint 2 are re-sequenced around scan-first / fetch-first. Purely
  additive — no existing report key renamed or removed, and the default (no `--scan`) build path is
  byte-identical.

### Fixed
- **tableau-migration:** **Completed the "DirectLake is opt-in, never the default" contract — the last
  place that still told users the router auto-routes to DirectLake has been corrected.** The storage
  router already routes every unresolved/undoable datasource shape to an honest `needs-storage-decision`
  (Import default; land-to-Delta + DirectLake reachable only via the explicit opt-in), but the M-partition
  emitter's needs-review reason for a wholly-unmapped connector class still read "route to land-to-Delta +
  DirectLake," and two internal comments still described the old auto-land behavior. The scaffold reason
  now points at the same needs-storage-decision path (rebuild direct-to-source once a connection is
  supplied, or opt in to land-to-Delta + DirectLake — never auto-selected), and a regression test locks it.
  Verified the direct-to-Fabric deploy path (`deploy_to_fabric.py`) makes **no** storage decision of its
  own — it deploys the already-assembled artifact — so the de-default holds identically on both the local
  `.pbip` emit and the direct-to-Fabric deploy targets. Wording/test only; no routing behavior changed.
- **tableau-migration:** **A workbook migration no longer stops to ask the user whether the workbook
  embeds its datasource or connects to a published one — STEP 2 auto-detection is the immediate,
  mandatory behavior.** The 1.27.2 runbook tightened the confirmation-ledger `workbook ds:` line so hard
  (it forbade any `auto-detect` deferral and offered only `embedded` / `published "<DS>"`) that the agent
  concluded auto-detection was off the table and interrogated the user instead — the exact opposite of
  the intent, and a contradiction of the runbook's own "never hand-classify" rule. `SKILL.md` now makes
  **auto-detect run immediately for every workbook** (the ledger records the datasource as `auto-detected
  at STEP 2`, and the agent never asks or hand-classifies), while keeping the hard guarantee intact: if
  STEP 2 detects a published datasource it must be in scope so the report binds — fetch it and re-run
  before accepting a local build, and never ship an empty workbook-only rebuild. Also corrects a dangling
  forward reference — the Fabric note pointed at a STEP 3 "decision tree" that does not exist yet — to
  point at STEP 3's actual duplicate-model guidance instead. Documentation only.
- **tableau-migration:** **A published-backed workbook's datasource now co-migrates by default for a
  local output — no more empty reports.** The runbook (`SKILL.md`) let an agent migrate a
  published-datasource workbook without putting that datasource in scope, so the report rebuilt against
  the workbook's unusable `sqlproxy` proxy stub and opened empty. `SKILL.md` now frames a published
  datasource exactly like an **embedded** one — the datasource migrates **first** and the report binds
  to it — and makes this mandatory for local output (no workbook-only migration, no passive
  `auto-detect` defer): if STEP 2 names a published datasource that is not yet in scope, fetch it into
  `.\in` and re-run so it lands first and the workbook binds. Documentation only — the engine already
  rebuilt-and-bound the report from the matching published datasource once it was in scope.
- **tableau-migration:** **Windows `MAX_PATH` (260) no longer breaks — or silently masks — a deep
  workbook `.pbip` build.** A rebuilt PBIR report nests many folders deep
  (`<report>.Report/definition/pages/<page>/visuals/<visual>/visual.json`), so a long output root could
  push a file past the Windows 260-character `MAX_PATH` limit. The write then failed with a raw
  `[WinError 3]`/`FileNotFound`, which the estate caught as a generic warning and left
  `pbip_status="skipped"` — and for a workbook bound to a **published** datasource that skip was
  indistinguishable from the legitimate "published datasource not in scope" carve-out, so a genuine
  failure could be silently swallowed while the definition-of-done still reported the workbook skipped.
  The fix lifts the limit at its source: every deep write (`assemble_model.write_model_folder`, the
  `.pbip` pointer) and every deep read (`deploy_to_fabric.read_model_folder` / `read_report_folder`)
  now routes its OS call through a Windows extended-length (`\\?\`) path helper, so a deep project both
  **writes and deploys** without hitting `MAX_PATH` — no-op off Windows, and the `\\?\` prefix never
  leaks into the returned paths or the Fabric part keys. The estate's pre-write `shutil.rmtree` cleanups
  are long-path-aware too, so re-runs over a previously-written deep tree stay idempotent. Because the
  writer now succeeds at any depth, the earlier hard pre-flight failure is **downgraded to a non-fatal
  warning** (the build proceeds; it recommends a shorter output root so the *local* `.pbip` opens in
  Power BI Desktop even without Windows long paths enabled). A genuine `OSError` at write time is still
  classified (`WinError 3`/`206`, `ENAMETOOLONG`, or an over-limit projected path → `path_too_long`),
  recorded as `pbip_write_error` with `pbip_status="failed"`, and surfaced through the
  definition-of-done banner and `summary.md` — and the definition-of-done still checks for that write
  failure **before** the published-datasource carve-out, so a real failure is always reported `failed`
  and never hidden behind a `skipped`. Also documents (SKILL.md) keeping the working `$RUN` root short
  on Windows, makes `auto-detect` a first-class answer for a workbook's datasource binding (removing the
  earlier contradiction that demanded an embedded-vs-published classification the pipeline resolves
  itself), and warns against spawning a duplicate model for a datasource migrated both standalone and
  inside a workbook (pass `--model-name` to overwrite). Report-schema change is additive only
  (`pbip_write_error`); the plugin mirror is updated in lockstep.
- **tableau-migration:** **post-deploy status and pre-`GO` setup read more honestly, from a clean first
  run of the long-path build.** Four small runbook/reporting gaps surfaced by an after-action review:
  (1) the credential-free **ProcessRecalc is now described — and logged — as asynchronous and
  best-effort, _started_ but not polled to completion** (unlike the model-deploy LRO, which `Checkpoint 3`
  still waits on), so a `202` is reported as _accepted_, not _finished_; (2) `SKILL.md` now states the
  **fresh-process rule** — every PowerShell call is a new process, so the Key Vault secret read and the
  fetch loop must run in the **same** call (never "verify" the read in a separate call, or the
  `TABLEAU_PAT_VALUE` env var evaporates and is re-fetched); (3) an explicit **STEP 4 — value
  reconciliation** names the post-credential-bind `executeQueries`-vs-Tableau-VDS check as the step that
  turns "structurally migrated" into "numbers verified," so it is queued as a follow-up rather than
  silently skipped and a model is never reported "verified" before it is; and (4) an **optional
  `SUBSCRIPTION_ID`** is collected up front (vars template + a guarded `az account set`) so a non-default
  Azure subscription no longer forces an improvised choice mid-run. Documentation plus one cosmetic
  deploy log string; no report-schema or translation change; the plugin mirror is updated in lockstep.
- **tableau-migration:** **the runbook's opening phases are now mechanical — the agent no longer
  deliberates before `GO`.** Three latent `SKILL.md` defects stalled an agent at Phase 0: (1) no
  working-directory anchor — every command used a bare `.\` with no pinned cwd, so the agent had to
  reason about where the installed skill lived and where outputs should land; (2) `GATE RULE 3`
  forbade "early script execution" yet Phase 0B told the agent to write and dot-source
  `migration.vars.local.ps1`, a contradiction it had to resolve itself; and (3) the fetch step wrote
  into `.\tds`, which collides with the skill's bundled sample datasources, so D2 scope could
  silently widen. The runbook now pins `$SKILL` (skill dir) plus a fresh, empty `$RUN` (working dir)
  once in Phase 0B, routes every call as `py -3.11 "$SKILL\scripts\<name>.py"`, and fetches into a
  fresh `.\in` under `$RUN` (never `$SKILL`) — so "scope = whatever is in `-i`" holds by construction.
  `GATE RULE 3` now states that pinning and writing the git-ignored vars file is pre-`GO` local setup,
  while `GO` still gates every STEP 1–3 script. Also adds a one-line second-compiler zero-guard
  (`needs_review_total == 0` → stage auto-satisfied, skip without re-reading the report), a
  `--model-name` / name-derivation note, a `.tds`+`.tdsx` same-stem dedup note, a PowerShell quoting
  note for names with spaces/parens/apostrophes, an honest D4 skip/stop note (deploy is
  createOrUpdate-only, no flag), and `.\in` cleanup guidance for the sensitive fetched inputs.
  Documentation-only: no script, report-schema, or test-behavior change; the plugin mirror is updated
  in lockstep.
- **tableau-migration:** **multi-line Custom SQL no longer emits undeployable TMDL.** The M-string
  escaper (`connection_to_m.escape_m_string`, used by both the `Value.NativeQuery` and `Odbc.Query`
  custom-SQL partition paths) only escaped double quotes, so a Custom SQL query spanning several
  physical lines left its interior lines at column 0 inside indentation-sensitive TMDL — Fabric then
  rejected the model with `Workload_FailedToParseFile — Invalid indentation`. The escaper now renders
  a complete M double-quoted string literal: CRLF/CR are normalised to LF, `"`→`""`, and embedded
  newlines/tabs become the standard M character escapes `#(lf)`/`#(tab)`, keeping the whole query on one
  physical TMDL line (runtime-identical SQL). `#` is deliberately not escaped, so `#temp` table names
  and existing `#(...)` escapes are untouched. Additive: single-line SQL is byte-for-byte unchanged.
- **tableau-migration:** **workbook-datasource consolidation no longer silently drops calculated fields
  from every island but the first.** When a multi-datasource workbook consolidated its embedded
  datasources into one model, the build passed `calcs=None` into `migrate_datasource`, which then
  auto-extracted calcs scoped to the *first* datasource island only — so a `Profit Ratio` (or any calc)
  defined on a second/third island was dropped with no trace, yielding a false `coverage_pct: 100`, an
  empty `needs_review`, and a second-compiler handoff that never fired. `_build_datasource_pbip` now
  extracts calculations globally across the whole workbook (`extract_calculations(…,
  include_dimensions=True)`) and threads the explicit `calcs=`/`dim_calcs=` into the consolidated build,
  so every island's calculated fields are present by construction. The recovered set is always a superset
  of the old first-island-only set (additive/no-regression); single-datasource workbooks pass `None`
  and are byte-identical. Fail-closed: an extraction error falls back to the prior auto-extract.
- **tableau-migration:** **the definition-of-done gate no longer reports a green PASS over a
  low-fidelity migration.** The gate classified a workbook as `pass` purely because an openable `.pbip`
  was *written* (`pbip_status == "built"`) — ignoring whether the report was faithful — so a run that
  stubbed calculated fields, rebuilt visuals with warnings, dropped a model reference, or landed a
  review-stub partition still printed `✅ DEFINITION OF DONE: PASS`. A new additive `warn` tier
  (`_dod_warn_reasons`) degrades such a built-but-not-faithful workbook from `pass` to `warn`, with a
  loud `⚠️ DEFINITION OF DONE: WARN` banner naming each degraded workbook and its concrete fidelity
  gaps, an additive `report["definition_of_done"]["reports_warned"]` count, and a `[WARN]` stdout
  marker. Precedence is `failed > warn > pass > skipped`; the gate stays *soft-but-loud* (exit status
  unchanged) and a fully-faithful build still passes. Additive: no existing report keys renamed or
  removed.

### Added
- **tableau-migration:** **the STEP 1–3 run contract is now an airtight, decision-free script sequence — the agent
  runs the scripts in order and reasons nowhere before the second compiler.** The runbook previously documented the
  scripts' own internal logic (extract-backed vs live, embedded vs published, flat-file materialization, binding) as
  agent-facing conditionals — "add `--include-extract` *if* extract-backed," "swap to `--workbook-name` for a
  workbook," a `sqlproxy` published-datasource branch, checkpoints that said "if `false`, re-fetch." That prose
  invited the agent to re-derive decisions the scripts already make, producing exactly the ad-hoc deliberation the
  contract forbids. STEP 1 now fetches every scoped name — datasource **or** workbook — through one uniform
  `fetch_tds.py` loop with `--include-extract` **always on** (required for extract/flat-file, harmless on live DB);
  the embedded-vs-published classification, storage-mode, and flat-file walls are deleted (auto-detected by
  `migrate_estate.py` in STEP 2); the three checkpoints are pure pass/fail asserts whose only failure action is
  **STOP and ask** — no self-diagnosis, re-fetch, or re-run. A new non-negotiable gate rule ("**No deliberation in
  the mechanical span**") states that between `GO` and the second compiler the agent may not classify a source, pick
  a per-source flag, add error-handling, tune timeouts, or reason about a corrective action; the first place it may
  reason is the second compiler (stubbed calcs). Documentation-only: the scripts and report schema are unchanged.
  project split.** Previously a workbook with several embedded datasources emitted one nested project per
  datasource (`pbip/<WB>/<DS>/`), splitting a dashboard whose views span datasources. It now rebuilds every
  embedded datasource into a *single* model as disconnected table islands — each table bound to its own
  upstream connection, exactly like a federated multi-connection datasource, sharing only the assembler's
  synthesized `Date` dimension — with one PBIR report bound to that one model, in the established flat
  `pbip/<WB>/{<WB>.SemanticModel, <WB>.Report, <WB>.pbip}` layout (single-datasource workbooks are byte-for-byte
  unchanged). This fixes a silent-drop defect where only the *primary* datasource's tables landed: the combined
  descriptor now reaches the table build (`migrate_tds_to_semantic_model` accepts a pre-built `descriptor=`),
  so every island's tables are present — zero datasources dropped. An island on an unmapped connector (e.g. SAP
  HANA alongside SQL Server) lands as an honest needs-review M-partition scaffold, recorded on the workbook
  detail's new additive `partitions_needs_review`, rather than being silently discarded ("stub it, never drop
  it"). Two additive workbook-detail keys — `consolidated_datasources` (the island captions folded into the one
  model, an anti-silent-drop audit trail) and `partitions_needs_review` — plus the estate summary's
  `workbooks_multi_datasource` now counts consolidated workbooks. Additive: existing report keys and the
  single-datasource layout are unchanged.
  estate workbook migration.** Rebuilding a Tableau workbook (its embedded datasource(s) **and** the report bound
  to them, into an openable `pbip/<Name>/` project) was previously only reachable through the private, estate-only
  loop inside `migrate_estate` — so an agent handed a lone `.twb`/`.twbx` had no public entry point and tended to
  hand-roll one, orphaning the report. `migrate_estate.migrate_workbook(source, *, write_to=…, name=None, pbip=True,
  …)` exposes that machinery directly: `source` accepts a `.twb`/`.twbx` path, raw workbook XML (`str`/`bytes`), or
  a live `TableauSource` + `wb_id`; it returns the same per-workbook detail dict the estate reports (`name`,
  `viz_status`, `pbip_status`, `bound_model`/`bound_datasource`, `pbip_folder`, `viz_fidelity`, …),
  consolidating a multi-datasource workbook into a single model (see the consolidation entry above), and only
  raises on invalid arguments (a per-workbook
  migration failure is reported on the detail, never raised). `migrate_estate` now **delegates** its workbook loop
  to this same function, so a standalone workbook and an estate workbook are byte-for-byte the same operation — the
  estate just runs it once per workbook. `migrate_datasource` stays datasource-scoped (model only); SKILL.md, the
  `migrate_datasource` docstring, and `resources/orchestration.md` now steer workbook-with-report inputs to
  `migrate_workbook`. Additive: a new public function and docs only — no existing report keys or behavior changed.
- **tableau-migration:** **extract-backed SaaS datasources (Salesforce, Marketo, ServiceNow, …) now have an
  honest offline Import home instead of falling through to a fallback.** A datasource on an unmapped SaaS
  connector that is extract-backed (a bundled `.hyper` snapshot, no reconstructable live Power BI connector)
  previously produced `mode=None` → needs-storage-decision, so it never landed as a model even though its data
  was sitting in the package. `storage_mode.select_storage_mode` now recognizes this shape (extract enabled +
  connector neither a mapped live class nor a flat file) and returns an `Import` decision marked
  `import_from_extract: True`; the materializer (`assemble_model.materialize_bundled_flatfile_data`) reads the
  bundled `.hyper` to one CSV per table even when there is no `flatfile_filename`, and both the direct
  (`migrate_datasource`) and estate (`migrate_estate`) paths build a local-CSV Import model over the
  materialized snapshot — preserving the point-in-time semantics of a Tableau extract without inventing a live
  SaaS connection. When no `.hyper` is bundled, both paths **fail closed** to the honest
  needs-storage-decision fallback with a `flatfile_data` record (`landed: False`) rather than writing a
  dataless/broken model. A mapped connector that happens to be extract-backed (e.g. SQL Server + extract) is
  unchanged — it still builds Import over its live connector. Additive: `import_from_extract` is a new
  decision key, `flatfile_data` shape is unchanged, and no existing report keys were renamed or removed.
  *(natural pair to the DirectLake-not-default pivot — the offline Import path the de-defaulted router points
  extract-backed SaaS sources toward)*
  datasources now route to an honest needs-storage-decision state instead of being auto-landed as
  Delta + DirectLake.** When the storage router could not map a connector (a structurally-unsupported
  join tree, an ODBC source with no reconstructable driver, a native engine with no server, or an
  unknown connector) it previously stamped `land-to-delta-directlake` and auto-built a lakehouse
  `landing_plan` — silently choosing DirectLake for the user. `storage_mode.select_storage_mode` now
  stamps `needs-storage-decision` for every `mode is None` shape, and the fallback reporting
  (`assemble_model` / `migrate_estate`) carries `storage_decision.fallback ==
  "needs-storage-decision"` / `fallback_path == "needs-storage-decision"` with **no** auto-built
  `landing_plan` and **no** `<model>.landing_plan.json` written — byte-identical in shape to the
  existing SSAS/XMLA fallback. The land-to-Delta + DirectLake capability is unchanged and stays
  reachable via the explicit opt-in helper `assemble_model.directlake_landing_plan(...)` (the
  `FALLBACK_LAND_TO_DELTA` constant and the auto-build gate remain as the opt-in hook). The default
  for an inferable shape is to rebuild direct-to-source as Import/DirectQuery; the residual
  needs-decision fallback points the user at the opt-in rather than choosing it for them. Additive:
  the `fallback` report key is unchanged, `landing_plan` is now emitted **only** through the opt-in,
  and no existing report keys were renamed or removed. *(2026-07-09 storage-model architecture pivot —
  DirectLake is opt-in only)*
- **tableau-migration:** **a workbook migration now fails loud when it produces no openable, model-bound
  report, and report rebuild is framed as a default deliverable instead of a preview.** Across three
  real-world migrations the running agent rebuilt only the semantic model and left the workbook's
  dashboards unbuilt — the skill advertised "semantic models," called report rebuild a *preview*, and had
  no check that a workbook actually yielded a bound report. A new machine definition-of-done gate now
  classifies every workbook input (`_definition_of_done`) and surfaces the verdict three ways: an additive
  `report["definition_of_done"]` ledger (`{applicable, status, workbooks_total, reports_bound,
  reports_failed, workbooks:[…]}`), a `summary.md` banner (a loud **⛔ DEFINITION OF DONE: FAILED**
  section naming each unbound workbook, or a `✅`/`ℹ️` one-line status), and an ASCII `[FAIL]/[OK]/[--]`
  stdout line. The gate is *soft-but-loud*: it never changes the process exit status (stays `0`) and
  honestly **skips** the two legitimate cases — openable projects disabled (`--no-pbip`), and a
  published-datasource workbook whose `.tds` was not co-migrated in the same run. Pure datasource runs are
  unaffected (`applicable: false`, no banner, byte-identical summary head). The frontmatter description,
  title, intro, and a new RUN CONTRACT gate rule are reworded so that whole-workbook → semantic model **+**
  bound Power BI report reads as the unmissable default. No existing report keys renamed or removed.
  *(AAR #1 / #2 / #3 — report rebuild was real but neither discoverable nor enforced by default)*
  `approved_calc_dax` values accept an additive dict form (`{"dax": "<DAX>", "table": "<TargetTable>"}`)
  alongside the existing flat `{name: "<DAX>"}` string form. When the deterministic tier could not place a
  row-context (dimension) calc, it defaulted the column to the anchor table — where the approved DAX
  referenced columns that don't exist, producing an invalid row-context expression. The dict form lets the
  approver land the column on its real table. The named table is honored only when it is a real model table
  (an unknown name would be silently dropped by the per-table inject, so the computed home is kept and the
  miss is recorded on the report row as `approved_target_unknown`); the requested target is echoed as
  `approved_target`. Measures are unaffected — they live in the shared `_Measures` table, so a measure
  approval's `table` is accepted but not applicable. The flat string form stays byte-for-byte identical, and
  `migrate_estate --approved-dax` loads either form (fail-fast on a malformed entry). *(AAR #1 Issue C)*
- **tableau-migration:** **every migrated model now ships a hermetic openability self-check so a run can
  no longer report success while emitting a model that will not open.** A new dependency-free gate
  (`openability_gate.check_model_openability`) validates the built model's TMDL parts — no duplicate
  column declarations, every M-typed column both declared and (when the landed flat file is readable) an
  actual physical header, and every `.tmdl` part well-formed (reusing the `tmdl_lint` openability rules).
  The verdict surfaces as the additive `report["openability_selfcheck"]` (`{ok, checks, issues}`), wired
  into `assemble_import_model` so it covers the datasource, local-CSV and workbook-rebuild paths. It is
  intentionally distinct from the opt-in TOM `report["openability"]` tier — cheap, always-on, and hermetic
  (never opens a file for the structural checks). Fail-safe: a table with no typed columns or no readable
  header is skipped, never mis-flagged.
- **tableau-migration:** **heatmaps that used Tableau's default colour scale now keep their colour
  instead of dropping it silently.** When an author leaves a table/matrix colour gradient on Tableau's
  *default* continuous palette, the workbook serialises no explicit `<color-palette>` element — so the
  viz rebuilder (`twb_to_pbir.py`) previously found no stops, returned no fill, and the conditional
  formatting vanished with no trace. `_parse_color_gradient` now recognises a continuous colour
  encoding that lacks serialised stops and synthesises a faithful-direction default gradient
  (sequential ColorBrewer *Blues* when the encoding has no centre; diverging *RdBu* when it pins a
  centre), so the heat scale is reconstructed on the rebuilt matrix. Because the colours are an
  approximation of the source, every synthesised scale is disclosed: a per-worksheet warning
  (`_disclose_default_palette`) plus a new additive report rollup
  `color_scale_defaults` (`{count, worksheets, note}`) that `migrate_estate.py` attaches to a
  workbook's report whenever the path fires, guaranteeing the approximation stays visible even when
  the per-worksheet warning is collapsed by the fidelity summary. Explicit palettes are unaffected
  (parsed byte-identically, never flagged). Verified end-to-end: a default-palette heatmap now emits a
  `backColor` gradient on its rebuilt matrix and lists the worksheet under `color_scale_defaults`. No
  existing report keys renamed or removed.
- **tableau-migration:** **multi-datasource workbooks now migrate every embedded datasource, not
  just the primary.** A published Tableau workbook can embed several datasources; previously the
  estate migration bound the report to the single most-used one and dropped the rest with a warning.
  `migrate_estate.py` now migrates **each** embedded datasource into its own self-contained,
  openable Power BI project. A single-datasource workbook keeps the established flat
  `pbip/<Workbook>/` layout byte-for-byte; a workbook with several datasources instead emits one
  project per datasource nested at `pbip/<Workbook>/<Datasource>/` — each a full-fidelity model
  rebuilt from that datasource plus a PBIR report rebound to it (a single report binds exactly one
  model, so a dashboard whose views span datasources is split across the per-datasource projects, an
  accepted and documented limitation). Per-datasource error isolation: an unmappable/fallback
  datasource is skipped-loud on its own entry while its siblings still build, and the failure never
  pollutes the primary. Additive report schema only — the primary datasource still mirrors onto the
  existing top-level keys for back-compat, and new keys `datasource_pbips` (per-workbook),
  `datasource_pbips_total`, `datasource_pbips_built`, and `workbooks_multi_datasource` (summary)
  make the per-datasource breakdown explicit; `summary.md` gains a per-datasource projects section.
  Verified end-to-end on a real 3-embedded-datasource workbook (all three projects built,
  self-contained, and open in Power BI Desktop). No existing report keys renamed or removed.
- **tableau-migration:** **deployed models now open without benign "needs refresh" warning
  triangles.** `deploy_to_fabric.py` runs a credential-free ProcessRecalc (Power BI enhanced refresh
  `type: Calculate`) automatically after every model deploy, exposed as the importable
  `recalc_dataset(workspace_id, dataset_id, token)`. A migrated model always carries two
  self-contained Import calc tables — the auto `Date` table (`CALENDAR(...)`) and the `_Measures`
  holder — which a REST `createOrUpdate` deploy leaves *unprocessed*, so a composite (DirectQuery +
  Import) model surfaces benign limited-relationship / "column needs to be recalculated or refreshed"
  triangles in the Fabric model view until its first refresh. ProcessRecalc processes only calculated
  tables/columns, relationships and hierarchies — **no `ProcessData`, so it needs no datasource
  credentials and never queries the DirectQuery source** (verified against an unreachable source) —
  mirroring how Power BI Desktop recalculates a model when opened. On by default and **best-effort**
  (a missing Power BI token skips it with a log line and never fails the deploy); pass `--no-recalc`
  to disable. Distinct from `--refresh`, which is a full data load that DOES need the bound
  credential. Additive: new function + CLI flag + dry-run line; no existing report keys changed.
- **tableau-migration:** **opt-in post-deploy relationship-cardinality upgrade** so a migrated
  composite model ships with correct join cardinality instead of everything conservatively
  many-to-many. `deploy_to_fabric.py` gains `--upgrade-cardinality`: once the model is queryable
  (credentials bound + a first refresh), it reads the deployed `relationships.tmdl` back
  (`get_model_definition`), DAX-probes each DirectQuery `many_to_many` join's **target** column
  (`COUNTROWS` vs `DISTINCTCOUNT` via the new `execute_queries` / `make_dax_count_fn`), and upgrades
  **only** the joins whose target is genuinely unique to the default many-to-one — preserving each
  relationship's GUID and leaving any non-unique, empty, or unprobeable join many-to-many. Exposed as
  the importable `upgrade_cardinality(...)` plus the pure, offline-testable TMDL helpers
  `parse_relationships_tmdl` / `render_relationships_tmdl` / `upgrade_relationship_cardinality` in
  `tmdl_generate.py`. Opt-in, best-effort (any doubt keeps the safe m:m), and **secret-free** — it
  binds only by ID and never reads or writes a credential. A new umbrella flag `--finalize` runs the
  whole secret-free finish chain in one switch: bind (with `--gateway-id`) → recalc → refresh →
  upgrade-cardinality. Additive: new functions + two CLI flags + dry-run lines; no existing behavior
  or report keys changed.
- **tableau-migration:** **percent-family Visual Calculations now carry a Power BI `format`
  string** so a view-only quick table calc that Tableau renders as a percentage (Percent of Total,
  Percent Difference, Year-over-Year, YTD Growth, Compound Growth, Percentile) shows as `0.00%` in
  the rebuilt visual instead of a bare ratio. The format is written on the projection's
  schema-verified `format` property (PBIR `RoleProjection`, `visualContainer` 2.x) and ONLY on a
  *visible* percent calc — a hidden colour-driver calc stays unformatted, matching the hand-built
  oracle. Absolute-valued families (Running Total, Moving Average, Difference, YTD) inherit the
  column default unchanged. Grounded on the PBIR schema plus the formatting-research inventory.
- **tableau-migration:** **constant reference / target lines now rebuild as native Power BI analytics
  reference lines instead of being dropped.** A Tableau reference line with a fixed value
  (`<reference-line formula='constant' value='…'>`) on a value-axis cartesian chart (column / line /
  area — where the measure is unambiguously the Y axis) is emitted as a `y1AxisReferenceLine` object
  (schema-verified `{properties:{show,value,[displayName]}, selector:{id}}`, the numeric `value` a
  `D`-suffixed double literal and the custom label a single-quoted string) so the goal line the author
  drew now renders in the rebuilt visual. **Warn-never-wrong:** only a *constant* on a value-axis chart
  is drawn; a computed line (average / median / min / max / total), a parameter-driven line, a
  percentage-band distribution, a trend fit, or any non-value-axis chart (a horizontal bar's ambiguous
  measure axis, a scatter's dual axes, a card) still defers with a precise message naming exactly which
  overlay was not carried. Strictly **additive** — the `_parse_reference_lines` descriptor and every
  existing deferral test are byte-identical; the emittable constants live in a new
  `reference_line_constants` IR field. Grounded on real workbooks (a Tableau Cloud Migration Readiness
  assessment: `100 GB` on an area chart, `5 minutes` / `2 hours` on column charts) and the
  formatting-research inventory, and validated end to end (the migrated `.pbip` carries the
  `y1AxisReferenceLine` on disk).
- **tableau-migration:** added `report_formatting.py` — pure, inventory-grounded PBIR builders for
  four report-layer formatting features that are currently detected-and-deferred or dropped:
  Analytics **reference lines** (`y1AxisReferenceLine` / `xAxisReferenceLine`), **rule-based
  conditional formatting** (`Conditional.Cases[]` solid fills for backColor / fontColor), in-cell
  **data bars** (`columnFormatting.dataBars`), and mark **opacity** (the inverted `transparency`
  property). Every shape is copied from real `.pbix` serializations catalogued in the
  formatting-research inventory (`objectIndex` raws + `valueGrammar`) and unit-tested against them.
  The **reference-line builder is now wired** (see above); the other three remain **emit-half only**
  — grounding against the formatting inventory shows they are not yet faithfully representable end to
  end (Power BI cartesian data marks expose no `transparency` target for Tableau's mark opacity;
  Power BI's only gradient forms are the continuous 2-/3-stop scales the migrator already emits, with
  no stepped/`num-steps` form; and Tableau has no in-cell data-bar construct to source), so they stay
  detection-independent builders pending a visual type that supports them. Skill `VERSION`
  `1.16.1` → `1.17.0`.
- **tableau-migration:** **the view-only quick-table-calc → Visual Calculation path now covers
  cartesian charts (bar / column / line / area), not just tables and matrices — closing a gap where a
  chart whose measure was a quick table calc emitted no calc at all.** A chart carries its base
  measure on the `Y` role (not the matrix `Values` shelf) and its dimensions on a single **Category**
  axis, so the earlier matrix-only wiring returned early (the base was never found) and no Visual
  Calculation was attempted — a line "moving average" showed the raw measure and a bar "percent of
  region" showed nothing computed. The wiring seam is now chart-aware: it sources the base from `Y`,
  appends the Visual Calculation there (base hidden, calc shown), and — because a chart's Category is
  the "rows" of its result matrix — runs the calc along `ROWS` regardless of the Tableau ordering
  token (a structural fact of chart geometry, not a per-example override). The COLLAPSE/COLLAPSEALL
  choice and the axis all flow from **one shared addressing decomposition**
  (`visual_calc_spec.resolve_addressing`): a partitioned percent-of-total emits `COLLAPSE(m, ROWS)`
  and re-nests the chart's Category **partition-outer / addressed-inner** (via a side-effect-free
  projection-count split, never fragile name matching) so the collapse lands on the addressed
  dimension, while a whole-table one keeps `COLLAPSEALL`. The dim-vs-measure classification is now a
  single shared set (`workbook_table_calcs.AGG_DERIVATIONS` / `Pill.is_dimension`, consumed by both
  the measure and view-layer paths) so the two agree on the edge derivations (`Cntd`/`Attr`/`Stdev`/a
  `User` LOD reference). Validated against a hand-built Power BI oracle: the line rebuilds to
  `MOVINGAVERAGE([Sum of Sales], 3, TRUE, ROWS)` and the bar to
  `DIVIDE([Sum of Sales], COLLAPSE([Sum of Sales], ROWS))` with Category `[Segment, Region]`, both
  matching the oracle. Strictly **additive**: the matrix path is byte-identical (a worksheet with no
  cartesian `visual_type` takes the matrix path unchanged), the measure engine and datasource
  migration are untouched, and precedence still yields to a bound model measure so the two paths never
  double-emit. Skill `VERSION` `1.16.0` → `1.16.1`.
- **tableau-migration:** **view-only quick table calcs now rebuild as Power BI Visual Calculations
  instead of being dropped — closing a fidelity gap that silently deleted whole worksheets.** A
  Tableau *quick table calc* applied on a pill (Running Total, YTD, YTD Growth, Moving Average,
  Percentile, Compound Growth, Percent Difference, Percent of Total, Year-over-Year, Difference) is a
  **report/view-layer** transform with no model equivalent, so it fell through the measure pipeline
  and the viz layer deferred it — the base aggregate survived but the transform was emitted as
  neither a measure nor a calc, and the visual was judged incomplete and skipped (19 of 21 worksheets
  in the ground-truth corpus). A new **additive** path recovers the calc's addressing facts
  (`workbook_table_calcs.extract_table_calc_usages`, extended with the previously-dropped
  `level-break` / `level-address` / `diff-options` reset-and-grain facts and the stacked secondary
  pass), normalizes them into a small view-layer IR (`visual_calc_spec`) and renders that IR into
  faithful **Visual-Calculation DAX** (`visual_calc_emitter`) — `RUNNINGSUM` / `MOVINGAVERAGE` /
  `RANK` / `PREVIOUS` / `FIRST` / `ROWNUMBER` / `COLLAPSEALL` over the visual's own matrix axis. It is
  a **compiler, not a pattern-matcher**: the axis is derived from the *view* (the shelf carrying the
  ordering/date dimension), not the raw ordering token — so the corpus' "computed Down" twin
  correctly flips COLUMNS→ROWS — an above-leaf offset is a resolved calendar ratio (Year-over-Quarter
  = 4 periods), and any calc whose axis, calendar ratio, or chain shape cannot be pinned from the
  workbook routes to **review** with a reason rather than a guess. Strict precedence keeps the three
  paths from colliding: the datasource-migration engine and the model-level table-calc **measure**
  engine are untouched and first-class; the Visual-Calculation path fires only when a pill is a quick
  table calc *and* the measure path did not bind a measure for it (never a double-emit; byte-identical
  output when the path doesn't fire). The base aggregate is materialized once as
  `Count Orders = COALESCE(COUNTROWS(Orders), 0)` so windows and resets match Tableau's densified
  result, the original Tableau spec is preserved as a provenance annotation (mirroring the
  `TableauFormula` / `TranslatedBy` discipline), and `report.json` / `summary.md` gain an **additive**
  `visual_calculations` routing rollup (emitted / review, by role and calc family). Both worksheet
  roles also carry their Tableau colour scale as a matrix `backColor` heat map: a *conditionally
  formatted* table tints its shown base cell (driven by the hidden calc) and a plain table tints its
  shown calc cell (driven by that calc) — the FillRule is bound to whichever column is actually
  visible so the fill renders, and the same white→orange gradient drives both. This boosts
  dashboard-rebuild fidelity toward pixel-parity replicas. Skill `VERSION` `1.15.1` → `1.16.0`.
- **tableau-migration:** **the self-update runbook no longer rolls back a good install on machines
  where an optional fidelity engine is present.** `resources/self-update.md` Step 3 (post-install
  verification) ran an **unscoped** `pytest`, which swept in the environment-optional `tests_oracle/`
  fidelity tiers; one such test only passes when an optional DAX/image engine is *absent*, so on a
  machine where that engine is present the gate failed by environment and the runbook's fail-loud
  rule discarded the freshly-installed skill and restored the older copy. Step 3 (and the
  macOS/Linux note) now scope the gate to the deterministic `pytest tests -q` suite — the same suite
  CI treats as canonical — so a correct install verifies and sticks. Skill `VERSION` `1.15.0` →
  `1.15.1`.
- **tableau-migration:** **a migrated flat-file (Excel/CSV) datasource now produces a `.pbip` that
  both opens locally and actually loads its data — two long-standing load blockers are fixed.**
  (1) *Data lands inside the project.* The one-button estate path now materializes the bundled
  Excel/CSV **inside** the openable project at `pbip/<name>/<name>.Data` (beside the
  `.SemanticModel`) and points the emitted `File.Contents` at a relocatable `SourceFolder` Power
  Query parameter (default = that absolute `.Data` folder) instead of a hard-coded path — so moving
  or zipping the project only needs that one parameter re-pointed, and a bare `.tds` discovered by
  the estate now recovers its data bytes from a same-stem `.tdsx`/`.twbx` twin. (2) *Tableau alias
  vs. physical header.* Tableau can expose a column under an **alias** (its `remote-name`, e.g.
  `Person`) that is not the physical spreadsheet header (`Regional Manager`); the generated M typed
  the alias, so Power BI failed to load (*"The column 'Person' … wasn't found"*). A new deterministic
  **header reconciliation** step reads the landed file's real headers and re-anchors each source
  column: exact-name columns bind first, then any leftover aliased column is paired to the leftover
  physical header (ordered by the `.tds`'s own `<ordinal>`) so the emitted M types a header that
  exists and renames it to the model column — robust even though real `.tds` files number ordinals
  datasource-globally (e.g. `21`/`22`, not `0`/`1`). A column that cannot be resolved unambiguously
  is **never wrong-bound** — it is left as-is and surfaced as a `flatfile_header_reconcile` mismatch
  follow-up. Additive report key `report["flatfile_header_reconcile"]` (`{remaps, mismatches}`); no
  existing report key changed. Skill `VERSION` `1.14.0` → `1.15.0`.
- **tableau-migration:** **the native query engines Spark, Presto, Trino, and Starburst are now
  first-class — they migrate cleanly over ODBC instead of being landed in Delta.** A Tableau
  datasource (or workbook) on a `spark` / `presto` / `trino` / `starburst` connection previously had
  no mappable Power BI connector, so it fell through to the lakehouse (land-to-Delta + DirectLake)
  fallback. These classes now route through the same engine-agnostic ODBC emitter as generic ODBC: a
  **Custom SQL** relation emits `Odbc.Query("<connection string>", "<SQL>")` (the SQL passes straight
  through the driver to the engine, preserving its dialect) and a plain **table** relation scaffolds an
  `Odbc.DataSource(…)` for review — both **Import**, never Delta. The connection string is rebuilt from
  the parsed server/port/catalog; because a native engine `.tds` records no ODBC driver name (Tableau
  used its bundled driver), a per-engine default is supplied — Spark → `Simba Spark ODBC Driver`,
  Presto → `Simba Presto ODBC Driver`, Trino and Starburst → `Starburst ODBC Driver for Trino` — and
  surfaced as a **confirm-required** follow-up (install/confirm the matching ODBC driver where the
  model runs). Both extract-enabled and live native-engine sources take the ODBC path; a source with
  no server fails closed to the lakehouse fallback. The **strict secret boundary** is unchanged — no
  username/password is ever read or emitted, and `emit_connection_parameters` stays empty for these
  classes (the connection string is inlined). Additive — no report-schema change. Skill `VERSION`
  `1.13.0` → `1.14.0`.
- **tableau-migration:** **the live pull can now obtain the Tableau secret without Azure Key Vault, via a
  masked terminal prompt.** The runbook asks an explicit credential-access question — **(A) Azure Key Vault**
  (the default) or **(B) a local secure terminal prompt** — instead of silently assuming Key Vault. When the
  user chooses the local terminal, `fetch_tds.py --prompt-secret` reads the PAT (or Connected-App) secret at
  a hidden `getpass` prompt, exchanges it for a session token, and clears it from the process environment in
  a `finally` block. The secret is held **in memory only** — never echoed, written to disk (`.env`, logs) or
  the report, or shown in chat — an **empty entry is rejected** (fail fast), and `--no-prompt` forbids the
  prompt for unattended/CI runs. This routes `fetch_tds.py` through the existing dependency-free
  `credential_resolver` (explicit → `TABLEAU_PAT_VALUE` env → git-ignored `.env` → OS keyring → masked
  prompt), and adds a value-free `clear_secret_env` cleanup helper. Additive — no report-schema change; the
  Key Vault path is unchanged and remains the default. Folded into skill `VERSION` `1.13.0`.
- **tableau-migration:** **a generic-ODBC datasource running Custom SQL now migrates to a working
  Power BI Import model.** A Tableau `genericodbc` connection that fronts a query engine with Custom
  SQL (for example MinIO object storage reached through an ODBC driver) previously had no mappable
  Power BI connector, so it fell through to the lakehouse fallback and never produced a model. The
  custom-SQL relation now emits an `Odbc.Query("<connection string>", "<SQL>")` M partition: the SQL
  passes straight through the ODBC driver to whatever engine sits behind it, so the tier is
  **engine-agnostic**. The connection string is reconstructed from the parsed connection — a
  `Driver={…};Server=…;Port=…;Database=…` form, or `dsn=<DSN>` when a DSN is present (a DSN wins over
  an inline driver) — and a DSN-only **table** relation instead scaffolds an `Odbc.DataSource(…)` for
  review. **Secrets never leak:** inline credentials in the ODBC connect-string extras
  (`UID` / `PWD` / `username` / `password` / tokens / access keys, case-insensitive) are scrubbed at
  parse time, so neither the emitted M nor the migration descriptor/report carries a credential, and
  `emit_connection_parameters` stays empty for ODBC (the connection string is inlined into
  `Odbc.Query`). **Fail-closed:** when neither a driver nor a DSN can be recovered the run routes to
  land-to-Delta / DirectLake with a manual follow-up rather than emitting an unusable partition, and
  `genericjdbc` is deliberately excluded (Power BI has no JDBC connector). Additive — the migration
  report schema only gains non-secret `odbc_*` routing hints; the migration suite stays green. Skill
  `VERSION` `1.12.0` → `1.13.0`.
  migrated Import model actually loads rows.** Previously an Excel/CSV or extract-backed source (e.g. a
  `… - Extract` datasource whose `.tdsx`/`.twbx` bundles a `.hyper` rather than the original workbook)
  emitted `File.Contents` with Tableau's **relative** path — Power BI Desktop rejected it (*"The supplied
  file path must be a valid absolute path"*) and the model opened **empty**. The estate and workbook
  paths now lift bundled flat-file data to an **absolute** path: a packaged Excel/CSV is copied out
  as-is, and a `.hyper` **extract** is read to one CSV per table (via the optional `tableauhyperapi`)
  and imported with `Csv.Document`. When the data genuinely cannot be landed (no bundled file / extract,
  or `tableauhyperapi` not installed) the run reports it honestly — an additive
  `report["flatfile_data"]` / workbook `flatfile_data` signal (`landed`, `kind`, `reason`,
  `hyper_present`) plus a manual-follow-up that tells you to re-fetch with `--include-extract` or install
  `tableauhyperapi` — instead of silently shipping a model that loads nothing. **Workbooks are now
  first-class in the runbook:** the Phase 0A Decision Menu (D1 SOURCE / D2 SCOPE), the Confirmation
  Ledger, STEP 1, and Checkpoint 2 all present a `.twb`/`.twbx` workbook alongside a datasource, and
  STEP 1 documents that `--include-extract` is **required** for any flat-file/extract source. Additive;
  the migration suite stays green. Skill `VERSION` `1.11.1` → `1.12.0`.
- **tableau-migration:** **a published-datasource workbook's model now carries the calculations from
  BOTH sides — the published datasource's own calculated fields AND the workbook-local calculations.**
  When a workbook connects to a published datasource (`sqlproxy`) and that datasource is co-migrated in
  the same run, the workbook's model is rebuilt on the datasource's real schema; it now unions the
  datasource's own calcs (read from the matched `.tds`) with the workbook's, so a published calc the
  workbook never placed on a shelf (and so never cached) is no longer dropped. Workbook-local definitions
  win on a name clash; fail-closed (a parse hiccup leaves the workbook's own calcs exactly as before).
  The **workbook runbook is also hardened against improvisation:** STEP 1 adds an explicit
  published-datasource-workbook branch (co-migrate the datasource in the same `.\tds`) and a DO / DON'T
  guardrail block (download workbooks only via `fetch_tds.py --workbook-name`; never hand-roll a REST
  downloader or unzip the `.twbx`; never migrate a published-datasource workbook without its datasource),
  the Phase 0A menu + Confirmation Ledger declare the workbook's datasource binding (embedded vs
  published + the co-migrated datasource), and Checkpoint 2 verifies the
  `bound_via: published_catalog_match` signal. Additive; folded into `1.12.0`.
- **tableau-migration:** **`fetch_tds.py` now downloads a published _workbook_, not just a datasource.**
  New `--workbook-name` / `--workbook-luid` selectors fetch the `.twb`/`.twbx` (add `--include-extract`
  for the packaged archive) into the same `.\tds` folder, so a workbook and its embedded datasource
  migrate together through `migrate_estate.py` (which already ingests `.twb`/`.twbx` and rebuilds the
  model **and** the report). Importable helpers `resolve_workbook_luid` / `download_workbook` mirror the
  datasource path; the datasource flags and behaviour are unchanged. Additive. Skill `VERSION` `1.11.0`
  → `1.11.1`.
- **tableau-migration:** **higher-fidelity Tableau dashboard → Power BI (PBIR) visual rebuilds.** A
  workbook's worksheets and dashboards now reproduce more of their original look: a **dual-axis**
  line/bar measure pair, **per-measure series colours** (each measure keeps its authored colour on
  bar / line / area / combo marks and in KPI multi-row cards), filled/symbol **maps** that carry the
  geographic `dataCategory` and a measure-driven colour gradient, and faithful chart-type / field /
  position binding to the migrated semantic model. Emitted only where it can be bound faithfully;
  anything ambiguous still degrades to a structured warning (warn-never-wrong). Additive; the
  migration suite stays green. Skill `VERSION` `1.10.0` → `1.11.0`.
- **tableau-migration:** **broader deterministic table-calculation → DAX coverage.** The Tier-0
  compiler now translates more Tableau quick table calcs faithfully: **Difference** and **Percent of
  Total**, the **Rank family** (`RANK` / `RANK_DENSE` / `RANK_MODIFIED`), and **moving `WINDOW`**
  aggregates with integer-literal bounds. Each is emitted only when it maps faithfully — e.g. `Unique`
  ranking (whose tiebreak depends on addressing order) and one-sided / non-integer window bounds still
  hand off rather than emit an unfaithful result. Tier-0 guarantees unchanged; the original Tableau
  formula is preserved as an annotation. Additive; the migration suite stays green.
- **tableau-migration:** **a Databricks Custom SQL relation now migrates to a deploy-valid native
  query.** A `<relation type='text'>` custom-SQL connection emits a `Value.NativeQuery(...)` M
  partition against the bound source instead of an unresolvable placeholder, so the generated model
  is structurally valid and deploys. Additive.
- **tableau-migration:** the (advisory, quarantined) fidelity oracle gained a **per-visual
  REPRODUCED / PARTIAL / DEGRADED / MISSING** scorer so a rebuilt report page can be graded visual by
  visual against its Tableau source. Lives in `tests_oracle/` and the optional oracle tooling only —
  no change to the deterministic migration runtime or its report schema.
- **tableau-migration:** **a candidate-ranking step for the assisted (second-compiler) tier**
  (`translation_reconcile.rank_candidates`) — the optional acceleration tier's *selection* helper.
  Given the N candidate DAX translations the agent (the documented second compiler) authors for one
  fallback, it reconciles each through the gate + numeric oracle and returns them **best-first**, each
  with a `confidence` (`high` = verified against the Tableau ground truth · `medium` = passed the gate
  but not yet reconciled · `low` = proven wrong or malformed) and a one-line `reason`, plus `best`
  (the top non-`low` candidate, or `None` when every candidate is low). It ranks by **semantic
  equivalence, not string similarity**, embeds **no LLM API** (the agent proposes; this scores), and
  lands nothing — the chosen candidate still flows through `approved_calc_dax` and the human gate.
  Each ranked entry carries an auditable `signals` breakdown (`{gate, oracle, category}`) and a
  `requires_oracle` flag that enforces the playbook's mandatory-oracle rule — an unverified
  `dax_language_gap` approximation is **never** returned as `best` until the oracle VERIFIES it.
  Accepts each candidate as a raw DAX string or a `suggest_assisted_dax` suggestion dict, and
  degrades gracefully — zero candidates, a `None` list, or a malformed candidate carrying no DAX
  resolve to a gate-rejected empty string, so `best` is always a landable DAX **string** or `None`,
  never a stray dict. Documented in `resources/second-compiler.md`.
- **tableau-migration:** **the assisted (second-compiler) idiom registry now recognizes the
  argmin-over-a-dimension twin** of the existing argmax idiom ("the member of dimension C with the
  *least* AGG([f]) per partition", e.g. the lowest-selling city in each state). The detector
  (`_detect_argmax_dimension` in `calc_to_dax.py`) and its LOD parser (`_parse_max_of_fixed`) now
  accept the `{FIXED P : MIN(...)}` selector and emit the same faithful, tie-aware
  `CALCULATETABLE`/`ADDCOLUMNS`/`SUMMARIZE` shape with `MINX` instead of `MAXX` (pattern
  `argmin-dimension`); the argmax branch is byte-for-byte unchanged. Suggestions remain
  approval-gated — never silently emitted. Original parameterization of our own argmax emitter
  (CLEANROOM pass).
- **tableau-migration:** **a golden-loop regression harness for the assisted tier**
  (`tests/test_assisted_golden_loop.py`) that drives a corpus of known-good translations through the
  whole Tier-1 loop end-to-end — `suggest_assisted_dax` → `check_candidate_dax` (syntactic gate) →
  `reconcile` (numeric oracle) — seeded with the argmax/argmin idioms and the canonical
  human-approved C1/C2 sidecar pair (C1 "Highest Selling City By State Sales" = 1,221,139.3614
  reconciled against ground truth; C2 gate-locked). Proves non-vacuity (a wrong oracle value
  MISMATCHes; a corrupt/inert candidate fails the gate without touching the backend) and adds a
  forcing-function test so every newly-registered idiom detector must carry a golden corpus row.
  Test-only; no engine or report-schema change.
- **tableau-migration:** **an author's explicit per-field number format now survives to the Power BI
  `formatString`.** Tableau persists a column's explicit currency / percent / precision as a
  `default-format` code on the logical `<column>` element (e.g. `c"$"#,##0;("$"#,##0)`); previously
  these were dropped and every numeric/date column fell back to the generic type-derived format. A new
  decoder (`tableau_default_format_to_pbi` in `tmdl_generate.py`) maps the code's one-char type prefix
  (`c` currency / `n` number / `p` percent / `*` zero-pad, plus the uppercase `C<lcid>%` percent form)
  to an Excel/.NET-grammar `formatString`, joined to its physical `(table, column)` through the `<cols>`
  logical→physical map (`_default_formats_by_physical` in `connection_to_m.py`) and applied by
  `generate_column_tmdl` via a new optional `format_string` parameter. An unrecognized / unmapped /
  ambiguously-mapped code is omitted so the column keeps its type-derived floor — additive and never a
  regression; with no decodable code the emitted TMDL is byte-for-byte unchanged. Grounded in a 29-`.twb`
  corpus decode table (11 distinct codes / 461 occurrences); decode logic is original (CLEANROOM pass).
- **tableau-migration:** **a pure-Python TMDL well-formedness linter** (`scripts/tmdl_lint.py`)
  plus pytest coverage (`tests/test_tmdl_lint.py`) that guards the serializer's *openability*
  invariants in-suite. It flags the three failure modes that make a generated `.tmdl` fail to open
  in Power BI / TOM — empty-value annotations, column-0 / sibling-level orphan lines outside the
  top-level keyword allowlist, and a multi-line object body (`measure` / `column` /
  `calculationItem` / `expression`) that is not indented deeper than its opener's property level
  (while correctly accepting a `source` partition value-block — an M `let`/`in` or calculated-table
  expression — at the standard one-level-deeper indent, the form TOM opens) — over both raw TMDL
  text and the real generator output. Purely a developer/CI safety net for serializer regressions;
  no runtime, report-schema, or generated-output behavior changes. — the column-mode peer of the measures' `approved_calc_dax` channel — exposed on
  the estate CLI as **`--approved-dax <file.json>`**. A `{calc_name: dax}` approval flips an inert
  calculated-column stub into a live, byte-validated calculated column
  (`TranslatedBy = assisted translation (human-approved)`, status `assisted-approved`), consulted
  **only** when the deterministic tier produced no DAX so a faithful Tier-0 column is never
  overridden; the original Tableau formula is preserved as `TableauFormula`. `approved_calc_dax` is
  threaded end-to-end through `migrate_estate` (`_migrate_one_datasource`,
  `_rebuild_from_published_match`, `_attach_workbook_pbip`), and the dimension-calc coverage rollup
  gains an additive `assisted_approved` bucket + `live_coverage_pct` (existing keys preserved). With
  no approval supplied the run is byte-for-byte unchanged; the migration suite stays green.
- **tableau-migration:** **a local `.twbx` / `.tdsx` upload is now discovered and read** by the
  file-backed estate source, so the "just upload the packaged workbook / datasource" path behaves like a
  live pull instead of silently finding nothing. `migrate_estate.LocalFilesSource` previously matched only
  the *bare* `.tds` / `.twb` extensions (an exact `splitext` compare) and read every file as UTF-8 text, so
  a packaged export — which is a **zip** — was skipped entirely (a `.twbx`-only folder reported `0/0`
  everything). It now also discovers `.tdsx` / `.twbx`, extracts the inner document **in memory** via the
  tested `fetch_tds` / `workbook_table_calcs` zip helpers (never written to disk), and de-duplicates a
  packaged export against its unpacked twin (preferring the unpacked copy) so a mixed folder yields no
  duplicate datasource. Additive; existing bare-file behavior is unchanged. (Local==live parity, discovery
  half; published / `sqlproxy` schema recovery is tracked separately.)
- **tableau-migration:** **table-calc measures now translate on the live / published-datasource path,
  reaching parity with a local `.twbx` upload.** When a workbook connects to a published Tableau Cloud
  datasource (`sqlproxy`), `migrate_estate._rebuild_from_published_match` rebuilds the model from the
  matched, already-migrated published `.tds` — which is **schema only and carries no worksheets**, so the
  table-calc *addressing* (partition / order, recovered from the worksheet shelves) was previously lost and
  positional measures (`WINDOW_STDEV`, percent-difference-from-prior, `LAST`) stubbed to `= 0`. The rebuild
  now extracts `table_calc_usages` from the **workbook** (`twb_text`) and threads them through a new additive
  `table_calc_usages=` override on `assemble_model.migrate_tds_to_semantic_model` (default `None` keeps the
  prior auto-extraction from the source text; `[]` disables it; a list overrides it). With the addressing in
  hand the existing addressed-measure path emits faithful DAX (`STDEVX.S(WINDOW(…ORDERBY…))`,
  `DIVIDE(… - CALCULATE(…, OFFSET(-1, ORDERBY…)), ABS(…))`, `COUNTROWS(WINDOW(…PARTITIONBY…)) - ROWNUMBER(…)`)
  and cross-calc references (`2 * [Standard of Deviation]`, `Difference coloring`) resolve against them. A
  local `.twbx` whose embedded model already carries its own worksheets was unaffected (it self-extracts);
  this brings the credential-based live path to the same fidelity. Genuinely un-addressable shapes
  (nested-`FIXED` LOD argmax, parameter-case filters) still fail closed. Additive; the migration suite stays
  green. Skill `VERSION` `1.9.0` → `1.10.0`.
- **tableau-migration:** the rebuilt **report page now binds its columns to the migrated model**
  instead of the workbook's embedded placeholder entity. When `_attach_workbook_pbip` recovers a
  model from a matched published datasource, a new `_field_map_from_model` helper derives a
  `field_map` from the report's `model_manifest.naming` (column entries → `{entity, property}`, the
  fact table that owns the most columns) and threads it — alongside the fact `model_table` — into the
  single `twb_to_pbir` re-run, so report columns resolve to the real model tables rather than the
  source's phantom `sqlproxy` / caption entity. Aggregation pills keep their aggregation (the
  `field_map` entries carry no `binding`), and a date axis already rebound to the model's `Date`
  table stays authoritative via a `date_rebound` guard in `_apply_override`. The report records a
  `field_rebind` detail (rebound count + model table). Additive; the migration suite stays green.
- **tableau-migration:** the deterministic **calc→DAX compiler v2** — broader faithful function
  coverage across the String / Date / Aggregate / Type-Conversion families and deeper **row-level and
  table-calculation** translation (running-total and ordered `WINDOW_*` windows, percent-difference,
  positional offsets), each preserving the original Tableau formula as a `TableauFormula` annotation
  and **failing closed** (an honest, routable fallback reason) when no faithful DAX target exists. The
  model build now also stamps deterministic **model-facts on the migration report** — a
  `model_manifest` (typed model summary + parameter classification into value / field / filter) and
  `row_count` measure facts — so the report-page build can bind slicers, visual filters and measures
  to the rebuilt semantic model **by calc id**. Additive; the migration suite stays green. Skill
  `VERSION` `1.8.0` → `1.9.0`.
- **tableau-migration:** the **Tableau dashboard → Power BI report-page (PBIR) viz consumer** now
  binds those model-facts. `migrate_estate._attach_workbook_pbip` derives date / measure / row-count /
  parameter bindings from the freshly rebuilt model and threads them as keyword arguments into the
  single `twb_to_pbir` re-run, so a migrated report page points its visuals at the real measures and
  columns instead of placeholders. A new read-only, stdlib-only `scripts/workbook_calc_usage.py`
  classifies every workbook-local calc's **intent** (measure / native conditional-formatting / filter
  / row-level column) and where the dashboard uses it, joined back to the model half by the calc's
  bare internal id — the deterministic model↔viz contract. Additive; suite green.
- **tableau-migration:** a **layered, Key-Vault-free credential resolver**
  (`scripts/credential_resolver.py`) so a local / POC migration can authenticate to Tableau with no
  Azure Key Vault. `resolve_secret(...)` resolves a secret (e.g. a Tableau PAT's secret value) from
  the first configured-and-available layer, in order: an explicit value → a process environment
  variable → the same key in a git-ignored `.env` file → an OS-keyring secret (Windows Credential
  Manager / macOS Keychain / Secret Service via the optional `keyring` package, imported lazily) →
  an interactive `getpass` prompt (opt-in and TTY-guarded, so unattended runs never hang). The
  resolved value is returned to the caller only — never logged, persisted, or written to the report;
  the returned `ResolvedSecret` redacts its value in `repr`, and `CredentialNotFound` lists only the
  layers tried. `migrate_estate.LiveTableauSource` gains additive keyword-only params (`pat_value`,
  `pat_env_var` defaulting to `TABLEAU_PAT`, `env_file`, `keyring_service`, `allow_prompt`, each with
  a pointer env-var fallback) and its `_resolve_pat` now delegates to the resolver, falling back to
  the enterprise Azure Key Vault seam (`_resolve_pat_from_key_vault`) only when no local layer is
  configured. `describe()` is unchanged (no secret-bearing keys). Additive; the migration suite stays
  green. Skill `VERSION` `1.7.0` → `1.8.0`.
- **tableau-migration:** an additive, **opt-in local-data POC path** so a Tableau extract whose
  source connector has no live Power BI equivalent (S3 / MinIO, generic ODBC, Web Data Connector)
  can still be turned into a **clickable local Power BI Import model backed by real data** — no
  Microsoft Fabric, no lakehouse, no Azure Key Vault. `migrate_datasource(...)` gains a `local_data=`
  parameter accepting a `{table: csv}` map, a directory of `*.csv`, a single `.csv`, a
  `.hyper`/`.tdsx`/`.twbx` file, or `True` (auto-extract the source's own `.hyper`). When supplied it
  routes the datasource down the proven `Csv.Document` flat-file Import generator
  (`assemble_local_import_model` in `scripts/assemble_model.py`), reusing typed columns, calc→DAX
  measures, the Date dimension, relationships and parameters unchanged, and each table's partition
  points at its matched local CSV. A new optional `scripts/hyper_reader.py` (lazy `tableauhyperapi`,
  stdlib-only at import) writes one CSV per extract table for the auto-extract case; bring-your-own
  CSVs need no extra dependency. Adds the additive `report["local_import"]` key
  (`{data_source, matched, unmatched_tables, table_count, matched_count}`). When `local_data` is
  absent the run is a **byte-identical no-op** — the existing land-to-Delta fallback is unchanged.
  Additive; the migration suite stays green. Skill `VERSION` `1.6.0` → `1.7.0`.
- **tableau-migration:** Tier-1 Tableau **dashboard → Power BI** migration — workbook worksheets
  and dashboards are rebuilt as Power BI report pages in the PBIR/`.pbip` format
  (`scripts/twb_to_pbir.py`), wired into the estate driver (`scripts/migrate_estate.py`) so a
  migrated datasource's report is assembled and bound by-path alongside its semantic model. Adds a
  Tier-2 **image-oracle** verification harness (`scripts/image_oracle.py`, runbook
  `resources/image-oracle.md`) that checks rebuilt-report fidelity, plus viz-engine robustness
  (implicit row-count rollup, structural worksheet titles, additional chart-type mappings) and a
  `list_workbook_datasources` helper / additive `project_name=` argument on
  `write_local_pbip`. Additive; the migration suite stays green. Skill `VERSION` `1.5.0` → `1.6.0`.
- **tableau-migration:** the estate orchestrator (`scripts/migrate_estate.py`) gains an additive,
  **opt-in `rebind_plan=` parameter** that ingests a comparison-emitted `rebind-plan.json`
  (`schema_version "1.0"`) and writes a single `compile-report.json`. When the parameter is absent the
  run is a **byte-identical no-op** (no `compile-report.json`; `report.json` unchanged). When a plan is
  supplied the router consumes the frozen string-form contract (entries under `plan["plan"]`,
  `source_ref` the bare `source_id` join-key string, `label`/`workbook_luid`/`model_id` top-level
  siblings), routes each entry by `binding_status` **first** (`existing_fabric` → byConnection,
  `built_local` → byPath, `landed_to_delta`/`needs_attention` → deferred/unbound), resolves each routed
  source via `migrate_datasource(datasource=label)` reusing the model the estate pass already built,
  and calls the dashboard per-report bind seam through a pluggable/auto-detected callable (passing the
  shared `used_folders` accumulator). The bind seam stays **deferred** until the dashboard stage lands
  its bind function, so routed entries are recorded as deferred rather than guessed — keeping the run
  safe, green, and disjoint from the dashboard's binder functions. The JSON file is the only coupling
  (nothing is shelled); the comparison-owned plan is never mutated. Additive; the migration suite stays
  green. Skill `VERSION` `1.4.0` → `1.5.0`.
- **tableau-fabric-datasource-comparison:** the Fabric semantic-model inventory
  (`fabric_inventory.py`) now additively carries parsed **`relationships`**
  (`[{fromTable, fromColumn, toTable, toColumn, isActive}]`, both `'Table'[Column]` and `Table.Column`
  ref forms, `isActive` default-true) and a detected **`date_table`** object describing each model's
  marked or inferred date dimension (`{table, key_column, active_keys[], inactive_keys[],
  grain_columns[], marked}`; `null` when none). A date table is detected as **marked** via table-level
  `dataCategory: Time`, else **inferred** from relationships whose `toColumn` is a dateTime-typed key
  column. Producer-only (no consumer wired); the existing `tables`/`columns`/`measures`/`sources` keys
  are unchanged. `resources/report-schema.md` documents the new keys. Skill `VERSION` `1.7.0` → `1.8.0`; collection `0.9.0` → `0.10.0`.
- **tableau-migration:** the deterministic calc→DAX compiler (`scripts/calc_to_dax.py`) gains
  faithful, type-checked translations for more Tableau functions — `ATAN2`, `DATENAME` (all date
  parts, not just weekday), `ISOYEAR`, `DATETIME`, `ATTR`, `GROUP_CONCAT`, and the table
  calculations `RANK_MODIFIED`, `RANK_PERCENTILE`, and `TOTAL` — each with a probe/test and the
  original formula preserved as a `TableauFormula` annotation. The tie-aware
  **argmax-over-a-dimension** suggestion now also recognizes the real workbook shape where the
  per-partition max and the per-member detail are **separate named calcs** (e.g. "Highest Selling
  City By State Sales"), in addition to the inline and single-reference forms. Functions with **no
  provably-faithful DAX target** stay deliberately *fail-closed* (regex `REGEXP_*`; one-sided /
  internal-whitespace `TRIM`/`LTRIM`/`RTRIM`; start-of-week- or ISO-dependent `WEEK`/`ISOQUARTER`;
  `MAKETIME`/`MAKEDATETIME`; `HEXBINX`/`HEXBINY`; culture-sensitive `STR`; addressing-order
  `RANK_UNIQUE`; …), and the translation router (`scripts/translation_router.py`) now routes each
  to **honest, actionable guidance** — a DAX-language-gap note that explains *why* no faithful form
  exists, and (for a bare row-level expression used where a measure is required, e.g.
  `IF [Region]="east" THEN [Sales] END`) a missing-aggregation hint pointing to the `SUM(...)` /
  calculated-column fix — instead of an over-optimistic catch-all. Additive only; the migration
  suite stays green. Skill `VERSION` `1.3.0` → `1.4.0`.
- **tableau-migration:** estate/local runs now emit an **openable Power BI project (`.pbip`)** per
  migrated datasource by default (`pbip/<Name>/<Name>.pbip` via `assemble_model.write_local_pbip`),
  alongside the canonical `semantic_models/<Name>.SemanticModel/`, so each datasource opens directly
  in Power BI Desktop to explore and test. `migrate_estate.py` gains `--no-pbip` to suppress. Skill
  `VERSION` `1.2.1` → `1.3.0`.
- **tableau-migration:** end-of-run **second-compiler check-in** — when a run leaves stubbed
  calculations (`report["summary"]["needs_review_total"] > 0`, also surfaced in `summary.md`'s new
  **Next step** section and the per-datasource `translation_handoff`), the skill now offers to run the
  stubs through the second compiler instead of silently stopping. SKILL.md,
  `resources/second-compiler.md`, and `resources/migration-report.md` document the check-in.
- **docs:** [`INSTALL.md`](INSTALL.md) gains an **Updating** section (plugin and manual-folder update
  paths, the `tableau-migration` version-gated runbook, and the not-live-until-a-new-session caveat);
  [`UNINSTALL.md`](UNINSTALL.md) gains a **Clean up what removal leaves behind** section for the side
  effects a folder/plugin delete doesn't remove (the MCP landing zone's Azure resources, MCP client
  config and Copilot Studio connector, the local Docker stack, and downloaded Tableau artifacts /
  self-update backups).
- **tableau-fabric-datasource-comparison (new skill):** read-only estate comparison that inventories
  every published Tableau datasource and every Fabric / Power BI semantic model in a tenant and ranks
  each datasource from "already in Fabric" to "needs rebuild". Scores a weighted blend of four signals
  (name, column overlap, type compatibility, physical source) into tiers (`Exact / Strong / Partial /
  Weak / None`) and an estate rollup. The physical-source signal takes the best of strict
  `(connector, database, table)`, loose `(connector, table)`, and a connector-agnostic **table-name**
  tier, so it survives a **lakehouse intermediary** (Fabric reads a mirror; Tableau connects directly)
  and falls back gracefully when the upstream source is **obscured** (composite/DirectQuery models,
  referenced datasources) by dropping the source signal and redistributing its weight. The Tableau
  inventory adds a **Catalog-independent `.tds` fallback** (downloads the descriptor without its
  extract and parses columns + relation tables) so cloud-connected datasources the Metadata API can't
  see still produce a full schema. Standard-library only; offline-testable scoring core; never modifies
  Tableau or Fabric. Registered additively in all four packaging manifests (collection `0.3.0` →
  `0.4.0`).
  - **LLM-optional adjudication ("second matcher"):** every comparison now emits an additive
    `report["adjudication"]` queue (`scripts/adjudicate.py`) that routes the not-confidently-matched
    datasources — renamed columns, a renamed asset, an obscured/lakehouse source, a near tie, or a
    coincidental overlap of generic column names — to an agent for a **semantic** verdict, modelled on
    the `tableau-migration` skill's *second compiler*. The deterministic verdict stays authoritative;
    `--apply-adjudication` folds the agent's `match` / `partial` / `no-match` calls back in as advisory
    `agent_review` annotations and an `adjudicated_summary` rollup **without** changing any
    deterministic tier/score. Adds `--save-adjudication` / `--apply-adjudication` and
    `resources/llm-adjudication.md`; skill `VERSION` `1.0.0` → `1.1.0`.
  - **Migration-priority signal:** the comparison now also ranks *which* rebuilds matter by
    **downstream impact** (`scripts/priority.py`). Each datasource's usage — attached workbooks plus
    the sheets/dashboards built on it — is gathered from the Tableau **Metadata API** as the trusted
    primary source, with a thin REST workbook-connection fallback for the not-yet-indexed tail
    (`--usage {auto,metadata,rest,off}`). Usage bands (`High/Medium/Low/Unused/Unknown`) fuse with the
    verdict into an actionable `migration_priority` (`already_exists` → *Reuse*; otherwise `P1
    migrate-first` … `P4 retire candidate`), so a datasource with **0–1 attached workbook is
    deprioritized** even if it needs a full rebuild. Adds `matches[].usage` / `.priority` /
    `.migration_priority`, `summary.by_priority` / `by_migration_priority` / `usage_thresholds`, a
    Markdown "Migration priority" section, and `resources/migration-priority.md`; all additive. Skill
    `VERSION` `1.1.0` → `1.2.0`.
  - **Robustness & reliability pass (counting correctness, precision, source coverage):** all additive.
    (1) *Counting correctness* — the comparison now detects when several Tableau datasources claim the
    **same** Fabric model (`matches[].contested` / `contested_with`, `summary.contested_models`),
    reports `summary.distinct_fabric_matched` (distinct models behind the "already exists" bucket), adds
    a greedy **one-to-one** `summary.assignment` rollup (`assigned_match` / `assigned_tier`) so the
    estate can be sized without double-counting a shared model, and adds reverse `summary.fabric_coverage`
    (Fabric models no Tableau datasource maps to). (2) *Precision* — the column signal **down-weights
    ubiquitous generic names** (curated stoplist blended with an estate IDF penalty, gated to estates of
    ≥ 8 assets) so a coincidental generic overlap can't manufacture a match; a capped **fuzzy name**
    fallback (`difflib`) rescues near-miss spellings without ever outranking a true exact match; and each
    match carries a deterministic one-line `reason`. (3) *Source coverage* — Fabric M parsing gains
    **Lakehouse / Warehouse / Dataflow / Excel / CSV** connectors and `[Id=…]` / `[entity=…]` table
    navigation plus native-SQL `Value.NativeQuery` FROM/JOIN extraction, and the Tableau `.tds` parser
    now mines **custom SQL** (`<relation type='text'>`) FROM/JOIN tables — both directly strengthening
    the source signal across a lakehouse intermediary. Identical-asset scores are unchanged (every exact
    match still scores `1.0`). Comparison suite `65` → `82` tests. Skill `VERSION` `1.2.0` → `1.3.0`;
    collection `0.4.0` → `0.5.0`.
  - **Lineage-graph source matching (containment + table-name provenance):** all additive. The
    connector-agnostic table-name tier now scores **containment** — `coverage = |tableau ∩ fabric| /
    |tableau|`, anchored on the Tableau side — instead of a symmetric Jaccard, so a **consolidated**
    Fabric model that *covers* all of a datasource's upstream tables matches at full strength even when
    it is a strict superset (the dominant many-datasources→one-model migration pattern), where Jaccard
    would have diluted it to a partial. The superset boost only applies when a **distinctive**
    (non-generic) table is shared — a lone generic name (`data`/`staging`/`export`/…) falls back to
    plain Jaccard — and `coverage ≥ Jaccard` always, so no previously-computed score drops (identical
    assets still score `1.0`). Each candidate now exposes the matched `shared_tables` and
    `source_coverage`, and the per-match `reason` **names the shared source tables**, making the source
    verdict auditable. The Tableau inventory also **backfills `database`/`schema` from a table's
    `fullName`** when the Metadata API leaves them empty (common for cloud connectors), so the strict
    `(connector, database, table)` tier fires instead of dropping to the looser table-only signal.
    Comparison suite `82` → `90` tests. Skill `VERSION` `1.3.0` → `1.4.0`; collection `0.5.0` → `0.6.0`.
  - **Durability test pass (resilience contract):** locked the comparison engine's graceful-degradation
    behaviour against hostile / malformed / edge-case input with **+33 tests** (comparison suite `90` →
    `123`): None-valued fields and sources, empty and tableau-only estates, malformed records, Unicode /
    emoji / non-Latin names, determinism and input-order independence, a 120×120 estate, duplicate names
    on both sides, and partial signal dicts; plus parser-resilience for CRLF/tab/blank-line and truncated
    TMDL/M, very-long M input, bad-base64 / missing-`definition` payloads, corrupt and `.tds`-less ZIP
    archives, malformed `.tds` XML, pathological `fullName`, and out-of-range / non-numeric usage counts.
    Two small **additive** hardenings surfaced by the tests: Markdown table cells now neutralise `|` /
    newlines in attacker-influenced names so a hostile name can't break the ranked-matches table, and the
    adjudication apply path drops non-`dict` decision entries (`None` / strings / ints) instead of
    raising. No report key renamed or removed; identical-asset scores unchanged. Skill `VERSION` `1.4.0`
    → `1.4.1`; collection `0.6.0` → `0.6.1`.
  - **Empirical verification (`--verify`, Tier-2, opt-in/advisory):** promotes a match from "looks the
    same (schema/lineage)" to "the **data** agrees" by running read-only **aggregate** probes on both
    sides (Tableau **VizQL Data Service** + Fabric **`executeQueries`** DAX) and checking they line up.
    Built around **windowed-overlap agreement** so it is not fooled by volume: it `MIN`/`MAX`es a shared
    date/numeric key to find each side's range and their **common overlap window**, then compares
    `SUM`/`DISTINCTCOUNT` **only inside that overlap** — so a Fabric model with extra history (e.g.
    2019–2026 vs Tableau 2021–2026) **verifies** instead of looking like a mismatch. Verdicts:
    `verified` / `compatible` (one-side-superset, no window column) / `mismatch` (overlap disagrees or
    ranges disjoint) / `inconclusive`. Adds `match.verification` + `match.verification_note` and a
    `summary.verification` rollup, plus a new "Empirical verification" report section — all **additive**;
    the deterministic tier/score/bucket are never changed (a `mismatch` is advisory). New CLI flags
    `--verify`, `--verify-top-n` (10), `--verify-max-cols` (4), `--verify-rtol` (0.01), and
    `--powerbi-token` / `POWERBI_TOKEN` (a **distinct** Power BI audience from the Fabric token; or
    `--use-az` mints it). Read-only and aggregate-only — no row-level data leaves either platform; needs
    live Tableau and degrades gracefully (cached inventory, missing token, 404/429/401/403/paused
    capacity → *skipped*/*inconclusive*). New `resources/empirical-verification.md`; comparison suite
    `123` → `171` tests. Skill `VERSION` `1.4.1` → `1.5.0`; collection `0.6.1` → `0.7.0`.
  - **Empirical verification — actionable "Fabric returned no data" detection (live-dry-test
    hardening):** when an `--verify` match comes back `inconclusive` purely because the Fabric model
    returned nothing while Tableau returned real values, the verdict now says **why**, and never reads
    it as a mismatch. A new `match.verification.reason_code` distinguishes `fabric_no_data` (model held
    no rows / explicit *"needs to be recalculated or refreshed"* — refresh it) from `fabric_unreadable`
    (every probe errored, e.g. a DirectQuery source not configured or a paused capacity — resolve it),
    each with a fix-it `verification_note`; rolled up as `summary.verification.fabric_no_data` /
    `fabric_unreadable` and a plain-language callout in the report. Gated on *Fabric returned nothing
    for any probe **and** Tableau returned data*, so a per-column quirk is never mislabelled. The 400
    `executeQueries` error detail is now surfaced (`extract_executequeries_error`) instead of a generic
    code. All **additive** — no key renamed/removed; deterministic tier/score/bucket unchanged.
    Verified end-to-end against the live 10ay Tableau + Fabric F2 mirror estate (6/6 already-exist;
    all 6 models correctly reported as refresh/connection-pending, not mismatches). Comparison suite
    `171` → `178` tests. Skill `VERSION` `1.5.0` → `1.5.1`; collection `0.7.0` → `0.7.1`.
  - **Empirical verification — offline transport-seam tests (reliability hardening):** the thin
    live-only transports and the probe closures that turn raw HTTP into `(value, error)` are now
    exercised offline. New `tests/test_transport.py` mocks each network seam (`fabric_inventory._http`
    / `_request` / `acquire_powerbi_token`'s `subprocess.run`, `TableauClient._request`,
    `fab.execute_dax`) and **replays the exact response envelopes observed live** — Fabric
    `executeQueries` 200+scalar, 200+`null` (Import model never refreshed), 400 *"...needs to be
    recalculated or refreshed"*, the generic 400 *"Failed to execute the DAX query."* (DirectQuery
    source not configured), 429/401; Tableau VDS 200 / 404 (feature off) / 429 / error — so the
    `(value, error)` mapping and the `reason_code` triggers (`fabric_no_data` vs `fabric_unreadable`)
    are regression-locked without a live tenant. Tests only — no behavior or schema change. Comparison
    suite `178` → `203` tests. Skill `VERSION` `1.5.1` → `1.5.2`; collection `0.7.1` → `0.7.2`.
  - **Empirical verification — measures are never used as a window axis (false-mismatch fix):** an
    additive **measure** (e.g. `Sales`) is no longer eligible as the `MIN`/`MAX` overlap-window axis.
    Ranging a measure by its own bounds and then filtering its `SUM` to that overlap is
    self-referential and could flag a pure Fabric superset (the *same* datasource, just more rows) as a
    false `mismatch` — exactly the "same data, more history" trap windowing exists to avoid. Window
    candidacy is now gated on the Tableau Metadata-API `role`: `role == "measure"` columns are excluded
    as axes (dates and numeric *dimensions* — year / key / id — remain valid axes), while measures are
    still compared as `SUM` equality probes *inside* whatever window a dimension establishes. When only
    measures are shared, no window is built and verification drops to the conservative **containment**
    read (which never emits a `mismatch` from magnitude alone) instead of a bogus self-referential
    window. All **additive** — no key renamed/removed; deterministic tier/score/bucket unchanged.
    Comparison suite `203` → `206` tests. Skill `VERSION` `1.5.2` → `1.5.3`; collection `0.7.2` →
    `0.7.3`.
  - **Business-logic parity (calculated fields → measures) — closes the "structurally identical ≠
    logically equivalent" gap:** the four structural signals (name / column / type / source) say nothing
    about whether a datasource's **calculated fields** were re-expressed as Fabric **measures**, so two
    datasources with identical columns but different logic both scored "already exists." Each match now
    carries an additive, **name-level** `logic_parity` (`{status, tableau_calc_count, fabric_measure_count,
    matched, unmatched[]}`, `status ∈ none / likely / partial / unverified`) comparing Tableau calc names
    against model measure names, plus a `summary.logic_parity` rollup whose `review_needed` counts
    already-exists / partial matches whose calculations are **not** confirmed as measures — so an
    "already exists" verdict is never mistaken for "safe to retire." It deliberately does **not** compare
    formulas (that is the `tableau-migration` translator's job); it only flags where logic likely still
    needs rebuilding. Inputs: Tableau `fields[].is_calculated` (Metadata-API `__typename ==
    "CalculatedField"`, or a `<calculation>` child in the `.tds` fallback) and model-level `measures`
    parsed from TMDL. The Markdown report renders a **Business-logic parity** section only when a matched
    datasource has calculated fields; otherwise output is byte-for-byte unchanged. All **additive** — no
    key renamed/removed; deterministic tier/score/bucket unchanged. Comparison suite `206` → `218` tests.
    Skill `VERSION` `1.5.3` → `1.5.4`; collection `0.7.3` → `0.7.4`.
  - **Executive CSV / XLSX export (`--export-csv` / `--export-xlsx`) — share the result outside the
    terminal:** the finished report (whatever layers ran — verification, adjudication, logic-parity) now
    renders to two share-ready artifacts via a new `scripts/export.py` (**standard-library only** — the
    `.xlsx` is hand-assembled OOXML / SpreadsheetML, no `openpyxl` / `pandas` dependency). `--export-csv`
    writes one rectangular table — one row per Tableau datasource (verdict / tier / score / best Fabric
    match + workspace / usage / priority / logic parity / reason), the analyst pivot source, UTF-8 with a
    BOM so Excel opens it cleanly. `--export-xlsx` writes a three-sheet workbook: a **Summary** estate-
    sizing headline (already-in-Fabric vs. needs-rebuild counts **with percentages**, distinct models,
    one-to-one assignment, net-new models, the logic-parity review count, and the by-tier /
    by-migration-priority / verification breakdowns), a **Datasources** detail sheet (the same per-
    datasource rows with `Score` as a real number so it sorts), and a **Fabric coverage** sheet (models
    nothing in Tableau maps to). Both are **read-only over the report and purely additive** — they never
    alter a report key; the Markdown / JSON output is unchanged. Comparison suite `218` → `240` tests.
    Skill `VERSION` `1.5.4` → `1.5.5`; collection `0.7.4` → `0.7.5`.
  - **Verdict confidence — a decision-grade trust layer:** a new `scripts/confidence.py` fuses the
    independent evidence the engine already computes (score band, margin over the runner-up, how many
    of name / column / physical-source signals *independently* agree, mutual-best **reciprocity** on a
    contested model, and — when `--verify` ran — the empirical data check) into one `High` / `Medium` /
    `Low` confidence **per verdict**. It is symmetric: `High` means *confidently reuse* on an
    already-in-Fabric verdict and *confidently rebuild* on a needs-rebuild verdict (a borderline score
    just under the partial threshold is flagged `Low` instead). Each match gains
    `confidence.{level, drivers[], cautions[], margin, corroborating_signals, reciprocal_best}`; the
    rollup adds `summary.confidence.{high, medium, low, high_confidence_already_exists,
    low_confidence_review}`. The Markdown report gains a **Verdict confidence** headline near the top
    and a **Lowest-confidence verdicts (review these first)** table; the CSV/XLSX export gains a
    `Confidence` column and two Summary metrics. **Deterministic, additive and read-only** — never
    changes a `tier` / `score` / `bucket`; re-synthesised after `--verify` so the data check folds in.
    Comparison suite `240` → `267` tests. Skill `VERSION` `1.5.5` → `1.5.6`; collection `0.7.5` →
    `0.7.6`.
  - **Artifact importance & connected assets — value/blast-radius + usage telemetry:** a new
    `scripts/importance.py` fuses three independent value signals gathered during inventory — **reach**
    (dependent workbooks + dashboards), **consumption** (total **view count**), and **endorsement**
    (**certified**) — into a `Critical` / `High` / `Moderate` / `Low` rating per datasource (`Unknown`
    only when there is no usage evidence; weights renormalise over present signals). Distinct from
    migration **priority** (rebuild order): importance is *how much it matters and what breaks if it
    moves*. The Tableau inventory now best-effort-enriches each `usage` block with `view_count` (summed
    from per-workbook REST view statistics), `certified`, `has_quality_warning`, the extract refresh
    timestamps, `updated_at`, and `connected_assets` (the **names** of dependent workbooks / dashboards)
    via a **separate** Metadata-API query kept isolated from the proven downstream-count query, so a
    rejected field only loses enrichment. Each match gains `importance.{level, score, drivers[]}`; the
    rollup adds `summary.importance.{by_level, critical, high, total_views, certified_datasources,
    datasources_with_quality_warning}`. The Markdown report gains an **Artifact importance & connected
    assets** section (highest-value datasources with their views, dependent assets and last refresh);
    the CSV/XLSX export gains `Importance` / `Views` / `Certified` columns, importance Summary metrics,
    and a fourth **Connected assets** sheet (one row per dependent asset, when telemetry was gathered).
    Connected-asset names are **deduped** (the Metadata API returns an asset once per sheet path) so the
    deliverable never shows the same workbook/dashboard twice. **Deterministic, additive and
    read-only** — never changes a `tier` / `score` / `bucket` / `priority`. **Live-verified** end-to-end
    against a real Tableau Cloud site (the richer Metadata-API query and the view-statistics REST
    endpoint both resolve; importance section + connected-assets export render with real data).
    Comparison suite `267` → `306` tests. Skill `VERSION` `1.5.6` → `1.5.7`; collection
    `0.7.6` → `0.7.7`.
- **tableau-fabric-datasource-comparison:** new **borderline decision-review** layer
  (`scripts/borderline.py`) for the datasources sitting on the **reuse-vs-rebuild fence** — where the
  structural evidence is genuinely close, so the customer can decide from a diff instead of trusting an
  automatic verdict. Selection is deliberately inclusive (flagged when **any** trigger fires: the
  `partial` bucket, score within `--review-band` of the reuse/rebuild cutoff, a `Low`-confidence
  verdict, or calcs not yet confirmed as measures); a clean rebuild with no Fabric candidate is never
  borderline. Each flagged match gains `match.borderline` — the field-level diff (shared / Tableau-only
  / Fabric-only columns, type mismatches, shared/unique upstream tables, source coverage, logic-parity
  caveat) plus an advisory `recommendation_hint` (`lean_reuse` / `lean_rebuild` /
  `reuse_with_logic_review`) — and the rollup gains `summary.borderline.{count, band, strong_cut,
  partial_cut, by_origin_bucket, reasons, hints, names}`. The Markdown report adds a **Borderline
  review** headline + per-datasource diff section; the `--export-xlsx` workbook adds a **Borderline**
  sheet (when `count > 0`). New CLI flags `--review-band` (default `0.08`, fence half-width) and
  `--review-top-n` (default `25`, printed-diff cap). The `recommendation_hint` **never** overrides the
  verdict. **Deterministic, additive and read-only** — never changes a `tier` / `score` / `bucket`.
  Comparison suite `306` → `327` tests (+21). Skill `VERSION` `1.5.7` → `1.6.0`; collection
  `0.7.7` → `0.8.0`.
- **tableau-fabric-datasource-comparison:** new **embedded-datasource rebind/consolidation engine**
  — the skill now plans the **workbooks** with embedded (in-`.twb`, never-published) datasources, not
  only the published datasources. Four new pure, offline scripts: `embedded_inventory.py` enumerates
  every embedded datasource (+ its **workbook-local object list** — calcs / sets / groups / bins /
  LODs — keyed by `workbook_luid`) via the Metadata API with a `.twb`/`parse_tds` download fallback
  and a local-files mode; `embedded_cluster.py` fingerprints + clusters near-duplicates so the same
  datasource copied into dozens of workbooks collapses to **one** asset; `embedded_score.py` scores
  each embedded ds against the Fabric models **and** the published Tableau datasources by **reusing
  `compare.score_pair` / `compare.band_for`** (no scoring reinvented); `embedded_plan.py` emits a
  **`rebind-plan.json`** (frozen cross-skill `schema_version "1.0"`) assigning every workbook an
  `action` (`rebind_to_published` / `consolidate_new_model` / `rebind_to_rebuilt` / `convert_embedded`),
  a logical `model_id`, and a `binding_target` tagged by `binding_status` (`existing_fabric` →
  `byConnection` identity straight from the comparison, **excluded from the rebuild set**;
  `built_local` → `byPath`; `needs_attention` → unbound), plus overlap `evidence`, `caveats`, the
  `source_id ↔ workbook_luid` map (never assumes they are equal), an optional `date_table` slot
  reserved on every bound target (safe-default `null`; enriched later by the Fabric-inventory pass or
  the calc-compiler write-back), a per-entry `label` sibling — the caption-preferred selector the
  migration skill's `migrate_datasource(datasource=label)` accepts to pick an embedded datasource out
  of its workbook (derived from the RAW `<datasource name>` in the no-caption case to mirror
  migration's raw match), with `source_ref` kept as the `source_id` string — an optional per-entry
  `drift` fingerprint `{table_count, column_count, calc_count}`, and a Markdown rollup + analyst CSV.
  Two locked gates: `apply_view_dependency_feedback` downgrades a rebind to `convert_embedded` **only**
  when a dropped reference names an object the embedded datasource *actually contains*
  (presence-in-source), and existing-Fabric bindings are excluded from the rebuild set. Additive CLI
  on `compare_estate.py`: `--embedded-inventory-json`, `--rebind-plan-out` / `-md` / `-csv`,
  `--rebind-strong-cut` (default `0.65`), `--rebind-cluster-threshold` (default `0.80`),
  `--view-dependency-report` (existing flags untouched). New
  `resources/rebind-plan-contract.md` documents the contract. **Deterministic, additive and
  read-only** — never changes a `tier` / `score` / `bucket`; the migration guard suite is untouched
  (`956` passed / `1` skipped / `1` xfailed). Comparison suite `327` → `383` tests (+56). Skill
  `VERSION` `1.6.0` → `1.7.0`; collection `0.8.0` → `0.9.0`.
  orchestrator. Dimension-role and row-level calculated fields translate to DAX **calculated
  columns** end-to-end; previously the translator's column mode existed but was never called, so
  those calcs were dropped before translation was attempted.
- **tableau-migration:** table-calculation → DAX translator for the subset whose addressing
  (Compute Using) is recoverable from a `.twb`/`.twbx` — `WINDOW_*`/`RUNNING_*` families plus
  `RANK`/`RANK_DENSE`, `INDEX`, `LOOKUP`, `FIRST`/`LAST`/`SIZE` — fed by a workbook addressing
  extractor and consumer. `RANK`/`RANK_DENSE` are certified against a live Fabric model
  (0/616 mismatches; Skip-vs-Dense tie semantics confirmed on-engine). A datasource-only
  migration still preserves table calcs as stubs.
- **tableau-migration:** Tier-1 "second compiler" for calcs the deterministic Tier-0 compiler
  punts on — a deterministic router (`translation_router.py`) classifying each stub into a stable
  fallback taxonomy, a candidate-DAX validation gate (`check_candidate_dax`), a structured
  translation-handoff manifest, parameter model-object emitters (`parameters.py`: field
  parameters + what-if value parameters from `[Parameters].[X]`-driven `CASE`/`IF` swaps),
  `approved_calc_dax` landing, and a reconciliation value-oracle (`translation_reconcile.py`).
  Boundary documented in `resources/tier1-charter.md`. All report additions are additive.
- **Packaging / install:** self-verifying installers (`install.ps1` / `install.sh`) that register
  the plugin and **prove** it loaded (`copilot plugin list`), plus canonical `INSTALL.md` and
  `UNINSTALL.md` (recommended plugin path, surface matrix, verification, and the demoted manual
  folder-copy with a no-auto-scan warning).
- **Drift guards:** `tests/test_mirror_parity.py` now covers all three skills (parametrized), and a
  new `tests/test_manifest_sync.py` asserts the paired `marketplace.json` / `plugin.json` manifests
  are byte-identical, parse, and resolve their `source` + skill paths.
- **tableau-migration:** the skill now leads with a **gated runbook** (GATE RULES, Phase 0A
  Decision Menu D1–D5, credentials form, Confirmation Ledger, and a 3-step
  fetch → migrate → deploy sequence with `--help`-verified flags and per-step checkpoints), plus a
  committed `migration.vars.example.ps1` template (git-ignored `migration.vars.local.ps1` for real
  values).
- **All skills:** an `AUTH MODEL` banner at the top of each `SKILL.md` to stop cross-skill auth
  bleed (migration = PAT default / JWT opt-in; profiler = PAT or Connected-App JWT; landing-zone =
  Connected App via the sidecar).

### Changed
- **tableau-migration:** **the second compiler (Tier-1 assisted calc→DAX translation) is now a mandatory,
  automatic, immediate stage of every migration — no longer an optional end-of-run offer.** Previously the
  runbook framed the assisted pass as an end-of-run check-in that asked the user whether to run it and left
  stubs inert if they declined. It now runs the moment the deterministic (Tier 0) pass leaves any calc
  stubbed (`needs_review_total > 0`): the agent announces a one-line, non-optional gate
  (`▶ Starting second compiler — N of M translated; K need review …`) and immediately proceeds — there is no
  "want me to?" prompt, no decline path, and no configuration that turns it off. Landing shifts from *human
  approval* to *automatic validation-gated*: every candidate that passes the syntactic gate
  (`check_candidate_dax`, always) plus the reconciliation oracle (when data is landed) is landed
  automatically via `approved_calc_dax`; a candidate with no faithful DAX form stays an inert stub with its
  `TableauFormula` preserved (the **faithful-or-stub** invariant now binds at the *landing* step, not the
  *run* step). Documentation-and-framing change only — `SKILL.md` step 3 + the *Assisted translation*
  section, `resources/second-compiler.md`, and `resources/migration-orchestrator.md` Phase 4 were rewritten,
  and the machine-emitted `summary.md` / CLI "Next step" strings now read as a mandatory instruction. No
  report keys renamed or removed; the two-pass `approved_calc_dax` API is unchanged.
- **tableau-migration:** refreshed `resources/feature-parity.md` Calculations section to reflect
  the translator's actual behavior — `FIXED` and table-scoped LOD, row-level calculated columns,
  scalar date/string functions as columns, and `CASE`/`WHEN` → DAX `SWITCH` all translate;
  `INCLUDE`/`EXCLUDE` LOD and regex remain stubs; parameter-driven `CASE`/`IF` swaps map to field
  parameters via the second-compiler path.
- **tableau-migration:** internal terminology cleanup across code comments, docstrings, and
  resource docs (removed internal play-numbering; the Tableau-Fabric-AI-Bridge attribution is
  retained).
- **README / install docs:** the plugin marketplace path is now Option 1 ("Recommended — works on
  current GitHub Copilot CLI"); the folder-copy method is demoted with an explicit warning that
  current GitHub Copilot does **not** auto-scan `~/.copilot/skills/`. Added a surface matrix and
  replaced "ask the agent what skills it has" with a real `/plugin list` + `/skills list` check.
- **Agent convention files:** `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, and `.windsurfrules` gained
  a short "Install / consume (for agents)" block with the two install commands and a link to
  `INSTALL.md`.
- **tableau-datasource-profiler:** `SKILL.md` now references its bundled scripts by skill-relative
  paths (`requirements.txt`, `scripts/...`) instead of hardcoded `.github/skills/...` paths.
- **tableau-migration:** `resources/self-update.md` wording standardized so the loaded-folder is the
  canonical install location and `~/.copilot/skills/tableau-migration` is a manual-only fallback.

### Fixed
- **tableau-migration:** **generated PBIR object names are now much shorter, keeping deep report paths under
  the Windows MAX_PATH (260) limit.** `twb_to_pbir._sanitize` built every page/visual/slicer folder and name
  as a 32-char readable prefix + 8-char md5 (capped at 50), and several visual/slicer names redundantly
  embedded their already-hashed page slug — producing names like `paramslicer-page-Dashboard118f16894ac216`
  that pushed the nested `…Report/definition/pages/<page>/visuals/<visual>/visual.json` path over MAX_PATH on
  real dashboards. The readable prefix is now truncated to 16 chars (max name 16 + 8 = 24), which also strips
  away the redundant embedded page slug automatically. Uniqueness is unchanged — it was always carried by the
  8-char md5 of the full name text, so the shorter prefix costs no collision safety.
- **tableau-migration:** **a join/union datasource now rebuilds directly to source as a multi-table model
  instead of being skipped.** A Tableau physical `join`/`union` tree previously collapsed into one opaque
  "combination" table, which the storage policy could only fall back on — so a join-tree datasource never
  landed as a model. `connection_to_m._extract_relations` no longer collapses the combination: the container
  is dropped and each **leaf table is surfaced as its own independent model table** (exactly like a
  multi-table object-graph source), and a new `_extract_join_relationships` recovers each physical join key
  (`<clause type='join'>` `[Table].[Column] = [Table].[Column]`) as a `many_to_many` model relationship,
  de-duplicated against the object-graph relationships in either orientation. A role-playing **alias** (same
  physical `item`, distinct `name` — e.g. `Contact1` over `[Contact]`) now surfaces as its own model table
  (the display name joined the de-dup key) instead of collapsing into the base table, while genuine
  physical/logical copies of one table still collapse. Fail-closed throughout: a composite/calculated
  predicate, an unqualified operand, or an unresolvable table/column is skipped with a warning rather than
  forcing the whole datasource to fall back. Additive — no existing report keys changed; parity with the
  object-graph relationship path.
- **tableau-migration:** **`MIN([str])` / `MAX([str])` over a text (or date) dimension now translate, so
  the `MIN([A]) + " / " + MIN([B])` tooltip idiom lands as a string concat instead of stubbing.** The
  single-column DAX `MIN`/`MAX` functions accept text (alphabetical order) and dates, matching Tableau's
  `MIN`/`MAX` on those field types, but the deterministic compiler's fast path rejected anything non-numeric/
  non-`dateTime` — so a Tableau author's common trick of wrapping a string dimension in `MIN()` to make it
  aggregate-valid in a tooltip false-stubbed on the aggregate. `MIN`/`MAX` over a bare field now emit for
  number, text, and date columns (a boolean or unmapped type still fails closed), a text result keeps its
  `text` dtype, and the null-propagating string `+` concat (previously column-mode only) is now also valid in
  a measure — where only aggregated/scalar text can reach it (a bare row-level text field still rejects
  upstream). Numeric and date `MIN`/`MAX` output is byte-for-byte unchanged. *(AAR #2 F-10)*
- **tableau-migration:** **the DirectLake table emitter no longer hardcodes `schemaName: dbo`, so a model
  landed to a non-`dbo` or non-schema lakehouse binds instead of silently failing.** `generate_table_tmdl`
  (and `assemble_directlake_model`, which threads it) gained an additive `schema_name` parameter that
  governs how the entity is addressed: a non-empty schema (default `"dbo"` → byte-for-byte identical to
  prior output) emits a schema-qualified `sourceLineageTag: [<schema>].[<delta>]` + `schemaName: <schema>`
  (a custom schema on a schema-enabled lakehouse is now honored verbatim), while `None`/`""` (a non-schema
  "classic" lakehouse) omits the `schemaName` line and emits an unqualified `sourceLineageTag: [<delta>]`.
  Previously the hardcoded `dbo` resolved the entity to a name that doesn't exist on such a lakehouse and
  silently broke the DirectLake binding. Existing callers are unchanged (the `dbo` default). *(AAR #3 G3)*
  its real model column instead of emitting an invalid measure reference.** When the model build hands the
  viz re-run its authoritative naming map (`_field_map_from_model`, which only ever carries model **columns**),
  `twb_to_pbir._apply_override` rebinds a pill's entity/property to the named column but previously kept the
  pill's original `binding` — so a `measure`-kind pill whose caption resolved to a column emitted a
  `{"Measure"}` expression pointing at a column (invalid PBIR). The override now flips a raw `measure`-kind
  ref to `column` when the columns-only field map resolves its caption, while an `aggregation` pill keeps its
  aggregation (`SUM` stays `SUM`) and a plain `column` is unchanged; an explicit override `binding` still
  wins. On today's pipeline the collision is unreachable (measure calcs are rebound via the token-keyed
  `measure_binding`, never the column field map), so this is a defensive invariant that also hardens the seam
  a measure-role calc routed to column mode would otherwise hit — warn-never-wrong, zero behavior change on
  the existing paths. *(AAR #1 Issue I)* When a physical table is added to a Tableau join it
  surfaces as e.g. `1. LoginHistory`, while the migrated model declares the clean table (`LoginHistory`)
  and keys its `COUNTROWS` measure clean. The implicit object-id `COUNT(*)` binding matched table names
  exactly, so the prefixed name missed and the card silently dropped its value. `twb_to_pbir`'s row-count
  binding now normalises a leading `"<n>. "` order prefix on either side (`_strip_table_order_prefix`),
  binding the card to its clean COUNTROWS measure. A normalised match binds only when it is unambiguous
  (two prefixed instances of the same physical table stay unbound and warned) and an exact key still
  wins — warn-never-wrong. *(AAR #1 Issue E)*
- **tableau-migration:** **local-CSV Import models no longer emit phantom or duplicate columns that
  made the model dead-on-arrival in Power BI.** When a `.tds`/`.tdsx` is migrated on the local-CSV
  Import path (`migrate_datasource(local_data=…)`), each table's columns are now reconciled against
  the header the materialized CSV physically contains. Previously a column present in the datasource
  metadata but absent from the CSV (a **phantom** — e.g. a hidden/removed extract column or an
  object-id metadata artifact) was still emitted as a TMDL `column` and typed in the `Csv.Document`
  `Table.TransformColumnTypes` step, so Power BI errored on load referencing a header that isn't in
  the file; and a column that appeared twice (a **duplicate** — e.g. an object-id-twin) produced an
  invalid repeated TMDL column name. `assemble_local_import_model` now alias-remaps first (so a
  renamed source name like `Person` → physical `Regional Manager` is kept, never dropped), then drops
  phantoms and collapses duplicates against the real header, disclosing every change in the additive
  `report["local_import"]["column_reconcile"]` (`dropped` / `deduped` / `remapped`). Fail-safe: a CSV
  whose header can't be read leaves every column untouched, so clean models are byte-identical to
  before.
- **tableau-migration:** **calculated fields containing comments now translate to DAX instead of
  silently falling back to a stub.** Tableau's calc editor allows `//` line comments and
  `/* ... */` block comments, which are documentation only and never affect the computed value. The
  calc tokenizer (`calc_to_dax.py` `_tokenize`) had no comment handling, so it read the first `/` as
  a division operator and the parse failed — every commented calculation false-stubbed with reason
  *"expected a value"* (on real workbooks this dropped the large majority of calcs, since authors
  routinely annotate them). `_tokenize` now strips `//` (to end of line) and `/* ... */` (across
  newlines) comments, placed **after** the string-literal scan (so a `//` or `/*` inside a quoted
  string is preserved as data) and **before** the operator scan (so `/` is never first consumed as
  division). A comment-only formula fails closed as an empty formula, and an unterminated `/*` block
  fails closed with a clear reason — both honest stubs, matching Tableau's own rejection. Division,
  string literals, and every previously-translating formula tokenize byte-identically (the new
  branches only fire on `//` / `/*`). Verified with comment variants (leading / trailing / mid-
  expression / multi-line) each translating to exactly the same DAX as their comment-free form.
  `Expression.Error: The name 't0.Order_Date' doesn't exist in the current context`, which also left
  relationships on those columns showing red validation triangles until manually toggled). The M
  partition used to append a `Table.RenameColumns` step that renamed the raw source headers
  (`Order Date`) to the underscored model names (`Order_Date`) above the query; the Service folds that
  query into SQL and then references the post-rename names against a subquery that still exposes only
  the pre-rename headers, so the fold fails. (It worked in Power BI Desktop because the mashup engine
  applies the rename in-process rather than folding it.) The rename is now removed from **every** M
  path (custom-SQL native query, generic-ODBC custom SQL, and flat-file Excel/CSV); instead each TMDL
  column binds to its **raw** source name via a `sourceColumn` — double-quoted when the name has a
  space/special (`sourceColumn: "Order Date"`), bare otherwise — while the model column NAME stays the
  underscored identifier. The binding is therefore declarative and fold-safe, and DAX, visual bindings,
  and the calc→DAX resolver are all unaffected because the model column name is unchanged. Simple
  names (including hyphenated ones like `Sub-Category`) emit a byte-identical bare `sourceColumn`, and
  the DirectLake path — where `sourceColumn` must equal the Delta column name — is untouched
  (`source_column` defaults to the column name). Its auto-discovery probed *every* local Analysis Services / Power BI
  Desktop instance and could block indefinitely on a stale one (`conn.Open()` never returns when many
  dead port files are present). Three bounded guards fix it: each connect runs on a daemon thread with a
  hard join timeout, the connection string carries a shorter native `Connect Timeout` so `Open()`
  self-terminates cleanly (no abandoned threads), and auto-select stops after a total discovery-time
  budget and degrades with a "pass an explicit port" reason. The inherently-live oracle test is now
  opt-in via `TABLEAU_MIGRATION_LIVE_ORACLE`, so the committed suite is fully hermetic. Optional oracle
  tooling only — no change to the deterministic migration runtime or its report schema.
- **tableau-migration:** **a migrated model's generated `Date` relationship no longer disappears on
  first refresh** (which had silently flatlined every time series). Two independent root causes in the
  Import/M emit path are fixed, both pure `.tds`-metadata so they behave identically for Import,
  DirectQuery, federated and flat-file: (1) authored object-graph relationships are translated as
  **many-to-many, single-direction dim→fact** instead of the default many-to-one — Power BI's
  unique-key check on a non-unique join (e.g. `Returns[Order_ID]` with duplicates) was rejecting the
  whole relationship batch and collateral-dropping the valid `Orders → Date` sibling; and (2) the M
  column emitter no longer writes a bogus `sourceLineageTag` (M columns bind via `sourceColumn`, not a
  schema), which had made Desktop treat the binding as speculative and drop relationships on refresh.
  The generated `Date` relationship stays many-to-one (its key is unique by construction). Additive
  (`report["relationships"]` gains a `cardinality` key); the migration suite stays green.
- **tableau-migration:** **Custom SQL is now de-escaped at the parse boundary, fixing a refresh-time
  type error.** When Tableau serializes a Custom SQL relation it **doubles every literal angle
  bracket** (`<`→`<<`, `>`→`>>`) to escape them from its own `<[Parameters].[Name]>` syntax; emitting
  that doubled form verbatim corrupted the query on Spark/Databricks, where `<<` / `>>` are the bitwise
  shift operators (so a predicate like `Profit < 0` failed `[DATATYPE_MISMATCH]` at refresh even though
  deploy succeeded). A single-chokepoint, parameter-aware global halve recovers the query the user
  actually wrote (proven exact by an even-run invariant); a surviving Tableau parameter reference is
  flagged `needs_review` rather than shipped silently. Connector-independent (also corrects Snowflake /
  SQL Server custom SQL).
- **tableau-migration:** the TMDL serializer now emits an **openable** model when a measure or
  calculated column carries a **multi-line** DAX expression. A deterministic multi-line body (e.g.
  the Date Filter keep-flag's `VAR … RETURN … SWITCH(…)`) was written inline after `measure 'X' = `,
  dropping its continuation lines to **column 0** — invalid TMDL that left the model `BLOCKED`
  (unparseable by TOM / Power BI Desktop). `tmdl_generate` now renders a multi-line expression as an
  indented block (the declaration ends at `=` on its own line, body lines one level deeper than the
  property level); single-line DAX is byte-for-byte unchanged. A second defect is fixed alongside
  it: an **empty-value annotation** (`annotation TableauFormula = ` with no value, e.g. a synthesized
  measure-swap `SUM`) is now **elided** rather than emitted as unparseable TMDL. Adds 4 openability
  regression tests; the migration suite stays green.
- **All three skills:** trimmed every `SKILL.md` `description` to fit GitHub Copilot's 1024-char
  frontmatter cap (they were 1369 / 1331 / 1333 chars). Over-limit descriptions are dropped
  silently — the plugin installs and `plugin list` shows it, but the skills never register in a
  session, so the agent fell back to reading the repo and improvising instead of running the
  skill. Verified the trimmed skills now load via the plugin path. Added
  `tests/test_skill_frontmatter.py` to assert `name` <= 60 and `description` <= 1024 for every
  SKILL.md (canonical and mirrored) so this can't regress.
- **tableau-mcp-landing-zone:** corrected the default `tableauMcpImage` pin. The previous
  default `:2.4.3` returns `MANIFEST_UNKNOWN` on GHCR (published stable tags jump 2.2.4 ->
  2.7.4), so a fresh deploy could not pull the image. Now defaults to the readable tag
  `:2.7.4` (still overridable) consistently across `main.bicep`, `azuredeploy.json`,
  `main.parameters.json`, and `deploy.ps1`, with the resolved `@sha256:` digest recorded as a
  hardening opt-in (template comment + `deploy-azure.md`).
- **tableau-mcp-landing-zone:** fixed the sidecar `UPSTREAM_MCP_URL` path. tableau-mcp 2.x
  serves Streamable HTTP at `/tableau-mcp` (older tags used `/mcp`); the stale path returned an
  Express 404 ("Cannot POST"). Updated in `main.bicep`, `azuredeploy.json`, and the local
  `docker-compose.yml`.
- **tableau-mcp-landing-zone:** set `ENABLE_MCP_SITE_SETTINGS=false` for the official server.
  2.7.x runs a startup site-settings probe needing the `tableau:mcp_site_settings:read` scope a
  direct-trust Connected App typically lacks, which 500'd the `initialize` handshake; disabling
  it skips only that read (the curated tool set still registers). Verified end-to-end against a
  live 2.7.4 deploy.

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
