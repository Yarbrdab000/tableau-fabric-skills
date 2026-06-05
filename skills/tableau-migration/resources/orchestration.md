# Estate Orchestration (`migrate_estate.py`)

The one-button entry point that turns the skill's individual generators into a complete estate
migration. Point it at a set of Tableau assets, run one command, and get a bundle of equivalent
Microsoft Fabric / Power BI semantic models plus a rich, machine-readable migration report.

> **Where this fits:** [migration-orchestrator.md](migration-orchestrator.md) describes the
> *per-datasource* phase flow (parse → storage mode → rebuild → calc → connection). This document
> describes the *estate-level* driver that runs that flow across **many** datasources and
> workbooks at once and assembles the output bundle + report. The orchestrator binds only to the
> existing public APIs — it never re-implements connection, type, calc, or TMDL logic.

---

## One-button flow

```text
TableauSource ──> for each .tds ─┐
                                 ├─ parse_tds(text)              (connection_to_m)
                                 ├─ extract_calculations(text)   (this module)
                                 ├─ select_storage_mode(desc)    (storage_mode)
                                 ├─ assemble_import_model(...)    (assemble_model)  ── parts ──┐
                                 └─ write_model_folder(parts)                                  │
              for each .twb ─────── viz stage (optional, pluggable) ── parts? ─────────────────┤
                                                                                               ▼
                                                              <output>/  semantic_models/  reports/
                                                                         report.json        summary.md
```

CLI:

```powershell
py scripts\migrate_estate.py --input <folder-of-tds-twb> --output <bundle-folder>
```

Library:

```python
from migrate_estate import LocalFilesSource, migrate_estate
report = migrate_estate(LocalFilesSource(r"C:\exports"), r"C:\out\bundle")
```

The run is **offline-first** and **resilient**: it needs no live credentials, and a single
unreadable / malformed / unsupported asset is isolated as an `error` (or `fallback`) detail rather
than aborting the whole bundle.

---

## Source adapters (`TableauSource`)

The orchestrator reads assets through a small abstraction so *where* the Tableau content lives is
independent of the pipeline. The contract:

| Method | Returns |
|---|---|
| `list_datasources()` / `list_workbooks()` | stable, sorted list of asset ids |
| `read_datasource(id)` / `read_workbook(id)` | raw `.tds` / `.twb` XML **text** |
| `asset_name(id)` | display / model name |
| `describe()` | small dict for the report's `source` block |

Three adapters ship:

- **`LocalFilesSource(root)`** — *(built + tested)*. Recursively discovers `*.tds` / `*.twb`
  (case-insensitive) under a folder and reads them with `encoding="utf-8-sig"` so Tableau's UTF-8
  BOM is consumed transparently. Ids are absolute paths; names are file stems.
- **`InMemoryTableauSource(datasources=, workbooks=)`** — *(offline fake)*. Serves `.tds`/`.twb`
  text from in-memory `{name: xml}` maps. It is the unit-test double for a live source, so the
  whole orchestrator is exercised with no files, network, or credentials.
- **`LiveTableauSource(...)`** — *(documented seam, not implemented in v1)*. The method surface
  for a real Tableau Server / Cloud connection is fixed; every method raises `NotImplementedError`
  today. See [Finishing `LiveTableauSource`](#finishing-livetableausource).

`.tds` files are treated as **datasources** (semantic-model path); `.twb` files are treated as
**workbooks** (viz path). Extracting datasources embedded *inside* a `.twb` is intentionally out of
v1 scope to keep estate counts unambiguous.

---

## Calculated-field extraction

Calculated fields are not in the connection descriptor — they live in the `.tds`/`.twb` XML as
`<column caption=.. role=..><calculation class=.. formula=../></column>`. `extract_calculations(xml)`
returns `(calcs, skipped)`:

- `calcs` → `[{"name", "formula"}]` for **measure-role** fields with a non-empty formula, handed
  straight to `assemble_import_model(calcs=...)`. The deterministic translator turns the safe
  subset into DAX and leaves everything else an inert `= 0` stub (formula preserved).
- `skipped` records every field deliberately left out **with a reason** — bins
  (`class='categorical-bin'`), empty formulas, caption-less fields, non-measure (dimension) calcs,
  and duplicate names — so nothing disappears silently.

---

## Output bundle layout

```text
<output_dir>/
  semantic_models/
    <Name>.SemanticModel/            one per migrated datasource (Fabric item definition)
      .platform
      definition.pbism
      definition/model.tmdl
      definition/database.tmdl
      definition/expressions.tmdl    (relational sources)
      definition/relationships.tmdl  (when relationships were inferred)
      definition/tables/<Table>.tmdl
      definition/tables/_Measures.tmdl
  reports/
    <Name>.Report/                   only when the optional viz stage emits parts
  report.json                        rich, machine-readable result (see schema below)
  summary.md                         human-readable stakeholder summary
```

Folder names are sanitized for Windows and de-duplicated (`Sales`, `Sales_2`, …). Each
`<Name>.SemanticModel` folder is cleared before a (re)write so a rerun never leaves stale,
renamed, or dropped table parts behind.

---

## Workbook viz stage (optional, pluggable)

Viz rebuild is a **pluggable, never-hard-wired** stage so this branch's tests pass standalone:

1. An injected `viz_stage=callable(twb_text, name) -> dict` wins if provided.
2. Otherwise, if a `twb_to_pbir` module is importable (Stream B), the first recognized entry point
   (`migrate_workbook`, `build_pbir`, `twb_to_pbir`, `build_report`) is bound lazily.
3. If neither is available, each workbook is recorded `viz_status="warned"` and the run continues.

A stage may return `{"parts": {path: text}}` to have a `<Name>.Report` folder written, and/or a
`"note"`. A stage that raises is isolated as a per-workbook `error`.

---

## Report schema (`report.json`)

Top level: `tool`, `generated_at` (UTC), `source` (`describe()`), `summary`, `datasources[]`,
`workbooks[]`, `fallbacks[]`. JSON is written with `sort_keys=True`; lists are emitted in
adapter-sorted order for deterministic diffs.

### `summary`

| Field | Meaning |
|---|---|
| `datasources_total` / `_migrated` / `_partial` / `_fallback` / `_error` | datasource outcome counts (`_partial` = migrated but `fully_supported=false`, i.e. needs manual follow-ups) |
| `tables_translated`, `columns_translated` | totals across migrated datasources |
| `measures_total`, `measures_translated`, `measures_stubbed` | calc → DAX outcome totals |
| `workbooks_total`, `workbooks_viz_built`, `workbooks_viz_warned`, `workbooks_viz_error` | workbook viz-stage counts |
| `connectors_seen` | sorted Tableau connector classes encountered |
| `storage_modes` | `{Import, DirectQuery, fallback}` counts |
| `viz_stage_available` | whether a viz stage was resolved |

### `datasources[]` (per datasource)

`name`, `source_id`, `status` (`migrated` | `migrated_with_followups` | `fallback` | `error`),
`connector` (Tableau class), and for migrated items: `m_connector` (Power Query connector),
`storage_mode`, `storage_decision` (the full decision dict incl. `rationale` /
`manual_followups`), `output_folder`, `tables`, `skipped_tables`, `table_count`, `column_count`,
`measures[]` (each `measure`, `status`, `reason`, `dax`, `tableau_formula`), `measures_translated`,
`measures_stubbed`, `skipped_calcs[]`, `fully_supported`. Fallback/error items carry `reason` /
`error` (and `fallback_path` for fallbacks).

### `fallbacks[]`

One entry per datasource routed to land-to-Delta + DirectLake: `datasource`, `reason`,
`fallback_path` (`land-to-delta-directlake`). This is the backbone of the integrator's
reconciliation story — every approximation is enumerated, never silently emitted wrong.

### `workbooks[]`

`name`, `source_id`, `viz_status` (`built` | `warned` | `error`), `note`, `output_folder`.

---

## Audit guarantees

- Column types come from the Tableau source schema, never inferred.
- Every calculated field's original formula is preserved as a `TableauFormula` annotation in the
  model; translated measures carry `TranslatedBy`, stubs stay inert `= 0`.
- Fallbacks are listed with a reason; nothing is emitted wrong silently.
- **No credentials** are read, stored, or written anywhere in the bundle (the parser never captures
  usernames/passwords; the report carries only model structure, formulas, and DAX).

---

## Finishing `LiveTableauSource`

The orchestrator already runs end-to-end against files and the in-memory fake; the only remaining
work to pull straight from a live site is implementing this adapter's four methods. The rest of the
pipeline does not change.

1. **Authenticate.** Pull a PAT (name + secret) from **Azure Key Vault** — never inline
   credentials — and `POST /api/<ver>/auth/signin` to exchange it for a site-scoped
   `X-Tableau-Auth` token. Keep the token out of all output.
2. **List datasources.** `GET /api/<ver>/sites/<site-id>/datasources` (paged) → ids / LUIDs.
3. **List workbooks.** `GET /api/<ver>/sites/<site-id>/workbooks` (paged) → ids / LUIDs.
4. **Download each.** `GET .../datasources/<id>/content` and `.../workbooks/<id>/content`; a
   `.tdsx` / `.twbx` is a zip — extract the inner `.tds` / `.twb` (root or `Data/`) and decode as
   `utf-8-sig`.
5. **(Optional) enrich.** Pull lineage / relationship metadata from the Tableau **Metadata API**
   (GraphQL) to feed relationship inference and the report.

Credentials and any on-premises gateway setup stay with the user (security boundary). Until this is
implemented, substitute `InMemoryTableauSource` (or `LocalFilesSource`) for offline runs.
