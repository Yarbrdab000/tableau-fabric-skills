# Migration Report

What to hand the customer at the end of a migration: a concise, auditable account of what was rebuilt,
what was approximated, and what they must finish. Emit this in the orchestrator's **Final** phase, after
[validation-reconciliation.md](validation-reconciliation.md).

> **Be honest about gaps.** The report's value is that every approximation and every stub is listed, with the
> original Tableau formula preserved, so nothing migrates silently wrong. Never report "100% migrated."

---

## Sections

### 1. Summary

- Datasource name, target Fabric workspace + semantic model name.
- Storage mode chosen and the one-line `decision["rationale"]`.
- Counts: tables, columns, relationships, measures (translated vs stubbed), measures verified.

### 2. Model

| Item | Migrated | Notes |
|---|---|---|
| Tables | n / n | one per `table`/`custom_sql` relation |
| Columns | n | typed from source schema |
| Relationships | n | inferred from hidden join keys, oriented by real cardinality |

#### Relationship confidence (`relationship_confidence`)

The report carries a machine-readable `relationship_confidence` manifest that explains, per relationship,
**why it was created** and **how much to trust it** — so a reviewer can sanity-check the join graph instead
of taking it on faith. It is additive: it sits alongside the existing `relationships` list and grades the
same edges one-for-one.

- **`created[]`** — one entry per authored single-column equality lifted from Tableau's object-graph
  `<relationships>`. Each records both endpoints' **own** connector (`from_connector` / `to_connector`) and a
  `cross_source` flag, so a heterogeneous federation (e.g. Azure SQL + Snowflake + Databricks in one
  composite model) is reported per table, never collapsed to a single datasource-level class.
- **`confidence`** — `high` / `medium` / `low`, taken as the **weaker** of the two endpoint keys (an edge is
  only as strong as its softer side). An ID-like name or an integer key grades `high`; a numeric/date key is
  `medium`; a coarse string/boolean dimension key grades `low` and gets an explicit many-to-many note in
  `risks[]`. Example: `Orders.Order_ID = RETURNS.ORDER_ID` → `high`; `Orders.Region = people.Region` → `low`
  with a "potential many-to-many" risk a reviewer should confirm.
- **`skipped[]`** — candidates the resolver dropped (composite/calculated key, unresolved endpoint, ambiguous
  orientation), each with the reason verbatim, so nothing is silently discarded.
- **`summary`** — counts of created/skipped edges and the high/medium/low confidence breakdown.

Surface the `low`-confidence and `skipped` rows in the customer report as relationships to review.

### 3. Calculated fields

One row per calc:

| Tableau field | Status | DAX / reason |
|---|---|---|
| Profit Ratio | ✅ translated · verified | `DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))` |
| Sales LOD | ⚠️ stub | `unsupported character '{'` — formula preserved |

Pull `status` from the translator (`reason`) and the reconciliation result (verified / mismatch /
not-evaluated). Every stub keeps its `TableauFormula` annotation in the model.

#### Calc coverage (`calc_coverage`)

Alongside the per-measure `measures` rows, the report carries a machine-readable `calc_coverage`
artifact so coverage can be consumed programmatically (gate a pipeline, drive a dashboard) instead of
scraped from stdout. It is additive — the `measures` rows are unchanged.

- **`measures[]`** — one row per calc with its `bucket`, a `live` flag, the translator `reason`, a
  `has_suggestion` flag, and the original `tableau_formula`.
- **buckets** — `translated` (deterministic safe subset) and `assisted_approved` (a human-approved
  assisted suggestion) emit **live** DAX; `assisted_suggested` (an idiom was recognized but not yet
  approved) and `stub` remain inert `= 0` placeholders.
- **`summary`** — per-bucket counts plus `live` / `inert` totals and two honest percentages:
  `deterministic_coverage_pct` (the safe-subset translator alone) and `live_coverage_pct` (including
  approved assists). Both are `null` when the model has no calculated fields — coverage is undefined,
  never a misleading 0% or 100%.

### 4. Connection

- Connector, server/database, mode (Import / DirectQuery), and whether a native query was preserved.
- `manual_followups`: credentials to enter, gateway to set up, custom SQL to review for folding.

### 5. Reconciliation

- Verified measures (numbers matched Tableau VDS).
- Mismatches (with both values + the filter context used).
- Could-not-evaluate (and why).

### 6. Not migrated (by design)

Calculated columns, sets/groups/bins, what-if parameters, calc groups, field parameters, perspectives, and
the **visual formatting** of rebuilt worksheets/dashboards (specific colors, fonts, legends, conditional
formats — a later pass). Worksheet/dashboard **structure** *is* rebuilt at Tier-1 — see
[§ Workbook rebuild](#workbook-rebuild-reportworkbooksn) below and [viz-rebuild.md](viz-rebuild.md).
(Hierarchies, display folders, and RLS roles **are** rebuilt — see
[model-enrichment.md](model-enrichment.md).) See [feature-parity.md](feature-parity.md).

---

## Audit guarantees to state explicitly

- **Types** came from the source schema, never inferred.
- **Every** calculated field's original formula is preserved as a `TableauFormula` annotation — translated
  or not.
- Translated measures carry `TranslatedBy`; stubs are inert `= 0` until a human repairs them.
- No credentials are stored anywhere in the model, the report, or the repo.

---

## Estate run: the output bundle

The one-button estate orchestrator (`scripts/migrate_estate.py`) writes a self-describing bundle:

- `semantic_models/<Name>.SemanticModel/` — the canonical TMDL model per datasource (the deliverable).
- `pbip/<Name>/<Name>.pbip` — an **openable Power BI project** per datasource (emitted by default;
  `--no-pbip` / `pbip=False` to skip), so each datasource opens directly in Power BI Desktop.
- `pbip/<Workbook>/<Workbook>.pbip` — an **openable, self-contained workbook project** per workbook with
  a rebuildable embedded datasource: the Tier-1 rebuilt report bound *by path* to a sibling model rebuilt
  from the workbook's own embedded datasource.
- `reports/<Name>.Report/` — the bare (unbound) rebuilt report parts, kept for back-compatibility.
- `report.json` + `summary.md` — the machine- and human-readable audit report.

### Workbook rebuild (`report["workbooks"][n]`)

Each workbook detail carries the bare report rebuild plus — when its embedded datasource can be rebuilt —
an openable, self-contained `.pbip`. The keys are **additive** (existing `name`, `source_id`, `viz_status`,
`note`, `output_folder` are unchanged):

- **`viz_fidelity[]`** — one row per rebuilt worksheet: `{worksheet, visual_type, status, reason}` with
  `status ∈ {"rebuilt", "warned"}`. A `warned` row (unsupported visual, no usable bindings) keeps the
  engine's `"manual attention required: …"` reason; `rebuilt` rows have `reason: null`.
- **`pbip_status`** — `"built"` when an openable workbook project was written, `"skipped"` when it could
  not be bound faithfully (the key is absent when `pbip=False`).
- **`pbip_folder`** — `pbip/<Workbook>/<Workbook>.pbip` when built, else `null`.
- **`bound_model`** / **`bound_datasource`** — the sibling model folder name and the embedded datasource
  label the report was bound to.
- **`model_translation_handoff`** — the embedded model's calc translation hand-off (stubbed calcs to
  review), surfaced so the workbook model is reported as honestly as a standalone datasource.
- **`pbip_warnings[]`** — every reason a faithful binding was declined (a lakehouse-fallback datasource,
  secondary datasources a single PBIR report can't bind, a missing PBIR definition), each prefixed
  `"manual attention required: "`. Nothing is mis-bound silently.
- **`pbip_ref_drops[]`** — the seam's field-reference cross-check: one row per visual that referenced a
  measure/column the rebuilt model did not emit (an optimistic `_Measures[caption]` bind that dangles),
  as `{visual, dropped:[...], emptied}`. The offending projections are removed (warn-never-wrong: dropped
  rather than mis-bound) and a visual that loses every projection is `emptied` to a placeholder zone;
  each drop also adds a matching `pbip_warnings` line. Empty (`[]`) when every reference resolves.
- **`date_rebind`** — present only when the report's date axes were rebound to the model's marked **Date**
  table (time intelligence runs through the calendar instead of the fact's raw date column):
  `{date_table, active_keys:[…]}`. The binder consumes the model build's date facts and rebinds ONLY the
  single ACTIVE business date; a secondary/inactive date stays on its fact column (+ warns) and a
  continuous-TRUNC trend is deferred — so the key is absent when there is no usable marked Date table.
- **`binding_signal`** — an additive, **routing-neutral** decision record about which model the report
  *should* bind to (the dashboard migration still always rebuilds + binds the embedded model today;
  this is the consumer-side signal the estate-comparison + datasource-migration skills need to decide
  rebind-vs-rebuild). Shape: `{kind: "published"|"embedded", connection_class, primary_datasource,
  published_ds_name, secondary_datasources:[…], view_local_calcs:[{name, formula, role}], recommendation,
  note}`. `kind` is `published` when the primary datasource is a Tableau **published** datasource
  (`connection_class == "sqlproxy"`), else `embedded`. `view_local_calcs` is the *would-break-if-rebound*
  set: workbook-local calculated fields actually referenced by a worksheet, which a published/shared model
  may not carry. `recommendation ∈ {"rebuild_embedded", "candidate_rebind_to_published", "review_rebind"}`
  — `review_rebind` when a published datasource has view-local calc dependencies (rebind only if the
  bound model satisfies them, else rebuild the embedded model).
- **`viz_implicit_row_count`** — count of **implicit row-count** measures the rebuilt report could not
  bind. Tableau silently provides a row count two ways: an object-id `COUNT(*)` (`COUNT` over
  `[__tableau_internal_object_id__]`) and the legacy `[Number of Records]` field. Neither has a column in
  the rebuilt model, so the binder recognises the count, names its **fact table** (resolved from the
  object-id column caption), and emits a `"manual attention required: … implicit row count … add a
  COUNTROWS measure …"` warning instead of a dangling reference. Full recovery needs a model-side
  `COUNTROWS` measure on that fact table (a cross-layer follow-up); until then the count is reported,
  never mis-bound. `0` when the workbook has no implicit counts.

The `summary` block adds `workbooks_pbip_built`, `visuals_rebuilt`, `visuals_warned`, the binding-signal
roll-ups `workbooks_published_ds`, `workbooks_embedded_ds`, and `workbooks_rebind_candidate`, plus the
implicit-row-count roll-ups `implicit_row_count_unbound` (total across the estate) and
`workbooks_implicit_row_count` (workbooks affected) — all additive.

> **Two model copies by design.** A workbook's `pbip/<Workbook>/` embeds its own rebuilt model so the
> project is self-contained and openable offline; the canonical `semantic_models/<Name>.SemanticModel/`
> stays the single deploy target. This duplication is intentional, not drift.

When any calculation fell back to a stub, `summary["needs_review_total"] > 0` and `summary.md` carries
a **Next step — assisted (second-compiler) translation** section naming each stubbed calc (datasource ·
name · role · category · reason · whether a suggestion is ready). Treat that as the cue to **offer the
second-compiler pass** to the user (see [second-compiler.md](second-compiler.md)); don't hand back a
model with silent `= 0` stubs.

Likewise, when a table partition's upstream query couldn't be auto-emitted (e.g. custom SQL on a
connector whose native query isn't yet verified), the build emits a **deploy-valid but incomplete
scaffold** (an empty typed table) rather than failing or guessing. Those partitions are surfaced
additively so the gap is visible at build time, not at deploy: per datasource,
`partitions_stubbed` (count) and `partitions_needs_review` (a list of `{table, kind, reason, sql}`,
preserving the original SQL); estate-wide, `summary["partitions_stubbed_total"]`; and a **Next step —
manual M partition completion** section in `summary.md`. Auto-emitted custom SQL — the SQL Server
family and the catalog-drill `Value.NativeQuery` for verified connectors (Databricks) — reports
`partitions_stubbed: 0`.

---

## Format

Plain Markdown is fine. Keep raw `.tds`/`.twb` contents and any credentials **out** of the report (see
[security-governance.md](security-governance.md)). The report should be safe to share with stakeholders.
