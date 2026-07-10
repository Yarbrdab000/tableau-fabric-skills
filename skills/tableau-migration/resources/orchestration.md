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
- **`LiveTableauSource(...)`** — *(documented seam; network calls not built yet)*. The method
  surface **and** the configuration surface for a real Tableau Server / Cloud connection are fixed.
  Construction does no I/O and stores **only names/pointers — never a secret or GUID**: a Key Vault
  name + secret name (to fetch a PAT at run time), the token name, the Tableau `server_url` / `site`,
  the asset **names** to migrate (`datasource_names` / `workbook_names`), and the target
  `fabric_workspace`. Each falls back to an environment variable (see below). The pure
  `_select_by_name(catalog, names)` helper that implements *discovery by name* is built and tested;
  only the REST catalog/download/auth calls remain. The `list_*` / `read_*` / `_resolve_pat` /
  `_signin` methods raise `NotImplementedError` today. See
  [Finishing `LiveTableauSource`](#finishing-livetableausource).

`.tds` files are treated as **datasources** (semantic-model path); `.twb` files are treated as
**workbooks** (viz path). To keep estate **counts** unambiguous, the orchestrator does not
*auto-fan-out* a workbook into its embedded datasources during enumeration — one `.twb` counts as one
workbook, not N datasources.

That is an **enumeration-scoping choice, not an engine limit.** The per-datasource path fully extracts
and migrates a datasource embedded in a `.twb`/`.twbx`: enumerate them with
`list_workbook_datasources(source)`, then migrate a chosen one with
`migrate_datasource(source, model_name=..., datasource="<caption or name>")` — it reads the inner
`.twb` from a `.twbx` (`fetch_tds.inner_doc_from_zip`), skips the `Parameters` pseudo-datasource and
per-worksheet reference stubs, and raises `AmbiguousDatasourceError` when several are present and none
is chosen. See [public-api.md](public-api.md) and the SKILL's "Inputs — Locate the Datasource FIRST".

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
   (`migrate_workbook`, `build_pbir`, `twb_to_pbir`, `build_report`) is bound lazily. (These names are
   looked up on the **`twb_to_pbir` module** — the viz renderer — not to be confused with the public
   `migrate_workbook` primitive documented below, which lives in `migrate_estate`.)
3. If neither is available, each workbook is recorded `viz_status="warned"` and the run continues.

A stage may return `{"parts": {path: text}}` to have a `<Name>.Report` folder written, and/or a
`"note"`. A stage that raises is isolated as a per-workbook `error`.

---

## Single-workbook migration (`migrate_workbook`)

`migrate_workbook` is the public single-workbook primitive — the workbook analog of
`migrate_datasource`. It rebuilds one workbook's embedded datasource(s) into semantic model(s) **and**
the workbook's report bound to them, producing an openable project:

```python
from migrate_estate import migrate_workbook
detail = migrate_workbook(r"C:\exports\Exec Dashboard.twbx", write_to=r"C:\out\exec")
# -> C:\out\exec\pbip\Exec Dashboard\Exec Dashboard.pbip   (model + report bound by path)
#  + C:\out\exec\reports\Exec Dashboard.Report              (the bare rebuilt report)
```

`source` is a filesystem path to a `.twb`/`.twbx`, raw workbook XML (`str`/`bytes`), or — for the
estate — a live `TableauSource` plus a `wb_id`. `write_to` (required) is the output project directory;
`name=` overrides the display name of a standalone workbook (default: the file stem, or `"workbook"`
for raw XML); `pbip=False` writes only the bare `reports/<Name>.Report`. Returns the same per-workbook
**detail dict** the estate reports (`name`, `viz_status`, `pbip_status`, `bound_model` /
`bound_datasource`, `pbip_folder`, `viz_fidelity`, …); a multi-datasource workbook consolidates every
embedded datasource into one model (disconnected table islands, each bound to its own connection) with a
single report bound to it. Only invalid **arguments** raise (e.g. a missing `write_to`) — a per-workbook
migration failure is reported on the returned detail, never raised.

**One code path.** `migrate_estate` loops exactly this function once per workbook, so a standalone
workbook migration and an estate workbook migration are the same operation — the estate simply runs it
more times (and first migrates the estate's standalone datasources to populate the published-datasource
match catalog it threads in as `ds_catalog`).

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
- A live run's `report.json` carries `source` *config names* (Tableau server/site, Key Vault name,
  secret name, Fabric workspace) for reconciliation — never a resolved PAT/token. These are runtime
  values; the emitted bundle from a real run is an output artifact and should not be committed.

---

## Finishing `LiveTableauSource`

The orchestrator already runs end-to-end against files and the in-memory fake; the only remaining
work to pull straight from a live site is wiring this adapter's REST/Metadata calls. The
configuration surface is already in place, and the pipeline downstream does not change.

### Configuration (names/pointers only — nothing secret is committed)

```python
LiveTableauSource(
    server_url="https://<pod>.online.tableau.com",
    site="<site-content-url>",
    key_vault_name="<your-key-vault-name>",   # vault NAME, not a secret
    pat_secret_name="<secret-holding-the-PAT>",
    pat_name="<tableau-PAT-token-name>",
    datasource_names=["Superstore"],       # discover BY NAME, not LUID/GUID
    workbook_names=[...],
    fabric_workspace="Tableau-migration-wps",
)
```

Every argument also reads from an environment variable when omitted, so nothing site-specific is
baked into source or tests: `TABLEAU_SERVER_URL`, `TABLEAU_SITE`, `TABLEAU_MIGRATION_KEYVAULT`,
`TABLEAU_MIGRATION_PAT_SECRET`, `TABLEAU_MIGRATION_PAT_NAME`, `FABRIC_WORKSPACE`,
`TABLEAU_DATASOURCE_NAMES` / `TABLEAU_WORKBOOK_NAMES` (comma-separated). `describe()` echoes these
names into the report's `source` block (handy for reconciliation) but **never** the resolved token.

### Implementation steps

1. **Resolve the PAT at run time** (`_resolve_pat`). Read the secret from **Azure Key Vault** with
   the `az` login already on the box —
   `az keyvault secret show --vault-name <key_vault_name> --name <pat_secret_name> --query value -o tsv` —
   or `azure-identity` `DefaultAzureCredential` + `azure-keyvault-secrets` `SecretClient`. Return the
   string; never log, persist, or report it.
2. **Authenticate** (`_signin`). `POST /api/<ver>/auth/signin` with `tokenName=pat_name` +
   the resolved secret to exchange it for a site-scoped `X-Tableau-Auth` token.
3. **List + filter by name.** `GET /api/<ver>/sites/<site-id>/datasources` and `.../workbooks`
   (paged) → a `[{"id", "name"}, ...]` catalog, then `_select_by_name(catalog, self.datasource_names)`
   (already implemented + tested) to narrow to the requested names and populate the id→name map.
4. **Download each.** `GET .../datasources/<id>/content` and `.../workbooks/<id>/content`; a
   `.tdsx` / `.twbx` is a zip — extract the inner `.tds` / `.twb` (root or `Data/`) and decode as
   `utf-8-sig`.
5. **(Optional) enrich.** Pull lineage / relationship metadata from the Tableau **Metadata API**
   (GraphQL) to feed relationship inference and the report.
6. **Deploy target.** `fabric_workspace` records where the emitted bundle should land; the
   integrator's deploy/import step (outside this seam) publishes the `*.SemanticModel` folders there.

Credentials and any on-premises gateway setup stay with the user (security boundary). Until the
network calls are built, substitute `InMemoryTableauSource` (or `LocalFilesSource`) for offline runs.
