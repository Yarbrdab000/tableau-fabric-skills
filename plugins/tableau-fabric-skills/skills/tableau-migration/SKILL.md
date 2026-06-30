---
name: tableau-migration
description: >-
  Rebuild Tableau datasources as Microsoft Fabric / Power BI semantic models.
  Recreates the data model as TMDL (typed columns, inferred relationships),
  translates the safe subset of Tableau calculated fields into working DAX measures
  (preserving each original formula as an annotation), and auto-selects a storage
  mode per datasource (extract to Import, live connection to DirectQuery). Accepts a
  .tds/.tdsx datasource or a .twb/.twbx workbook. Use to migrate Tableau datasources
  to Power BI semantic models, convert Tableau calculated fields to DAX, or repoint a
  migrated model at its original SQL Server / Snowflake / Postgres source.
  Triggers: "migrate from tableau", "tableau to fabric", "tableau to power bi",
  "tableau datasource to semantic model", "tableau workbook to power bi",
  "convert tableau calculation to dax", "tableau calculated field to dax",
  "rebuild tableau datasource in fabric".
---

> **AUTH MODEL — tableau-migration**
> **PAT (default, recommended).** Connected App (Direct Trust) **JWT only if the user explicitly
> selects D5=B.** Never silently switch auth modes or downgrade. The bundled scripts default to
> `--auth pat`; JWT requires the Connected App client/secret to be supplied on purpose.

---

## ▶ RUN CONTRACT — read before doing anything

This skill is a **gated, deterministic runbook**, not a freeform task. Follow the gates in order;
do not improvise flags or infer answers. The detailed reference body begins after the
"Run contract ends" marker further down.

### GATE RULES (non-negotiable)

1. **First turn = the Decision Menu, verbatim.** On invocation your FIRST message MUST be the
   Phase 0A Decision Menu below — issue **no** tool call, shell command, or file read in that turn.
2. **No defaults inferred, no question skipped.** Every decision (D1–D5) and every credential comes
   from the user. A blank or ambiguous answer = **STOP and ASK**, never guess.
3. **Do not run STEP 1 until the Confirmation Ledger (Phase 0C) is filled and the user replies
   `GO`.** No early script execution.

### Phase 0A — Decision Menu (present verbatim; defaults marked)

```
Before I migrate anything, confirm these 5 choices (e.g. reply "D1=A, D2=all, D3=A, D4=C, D5=A"):

D1 — SOURCE
   A) Live pull from Tableau Server/Cloud   (datasources and/or workbooks; needs Tableau creds)
   B) Local files I already have            (.tds/.tdsx datasources or .twb/.twbx workbooks)

D2 — SCOPE   (name datasources, workbooks, or both)
   all)      migrate every datasource / workbook found in .\tds
   <names>)  a subset — list the datasource or workbook names

D3 — OUTPUTS  (forces both-vs-one)
   A) Fabric + local bundle   (deploy AND keep the TMDL on disk)
   B) Fabric only             (deploy, don't keep local)
   C) Local only              (build the bundle, do NOT deploy)

D4 — CONFLICTS (a model of the same name already exists in the workspace)
   A) overwrite      B) skip      C) stop and ask   [default C]

D5 — AUTH  (forces the auth choice)
   A) PAT                       (default, recommended)
   B) Connected App JWT (Direct Trust)
```

### Phase 0B — Credentials form (simple 2-file pattern)

Collect the values below, then write them into a **git-ignored** local vars file — never paste the
PAT/Connected-App secret into chat; it lives in Key Vault.

| Variable | Meaning |
|---|---|
| `SITE_URL` | Tableau host, e.g. `10ay.online.tableau.com` (no `https://`) |
| `SITE_NAME` | site contentUrl (URL slug; empty string for Default) |
| `PAT_NAME` | Personal Access Token name (D5=A) |
| `KV_NAME` | Azure Key Vault holding the secret value |
| `SECRET_NAME` | the Key Vault secret whose value is the PAT (or Connected-App) secret |
| `FABRIC_WORKSPACE` | target Fabric workspace name or GUID (only if D3 ≠ C) |

If **D5=B**, also collect the Connected App `CLIENT_ID`, `SECRET_ID`, and impersonation
`JWT_USERNAME` (the secret value still comes from Key Vault) instead of `PAT_NAME`.

Set up the local vars file (mirrors the repo's `.env.example` → `.env` convention):

```powershell
Copy-Item .\migration.vars.example.ps1 .\migration.vars.local.ps1   # once
# fill migration.vars.local.ps1 with the real values (it is git-ignored), then:
. .\migration.vars.local.ps1
```

`migration.vars.example.ps1` is committed with **placeholders**; `migration.vars.local.ps1` holds
the **real** values and is git-ignored — never commit or mirror it.

> **No Azure Key Vault? (local / POC runs.)** Key Vault is the default for the live pull, but it is
> **not required**. `LiveTableauSource` resolves the PAT secret through a layered, Key-Vault-free
> resolver (`scripts/credential_resolver.py`) that tries, in order: an explicit value, the
> **`TABLEAU_PAT`** environment variable, that same key in a git-ignored `.env` file, an OS-keyring
> secret (Windows Credential Manager / macOS Keychain / Secret Service via the optional
> `pip install keyring`), then — only if `allow_prompt=True` and a console is attached — an
> interactive `getpass` prompt. So a POC can authenticate with just `set TABLEAU_PAT=<secret>` (or a
> `.env`), and falls back to the Key Vault path only when no local layer is configured. The secret is
> returned to the caller only — never logged, persisted, or written to the report; only a value-free
> layer label (`_pat_source`) is retained.

### Phase 0C — Confirmation Ledger (the run gate)

Echo the resolved choices + resources back, then wait for `GO`:

```
LEDGER — confirm, then reply GO
  source     : <D1 A live / B local>   from <SITE_URL/SITE_NAME  or  .\tds>
  scope      : <all | datasource and/or workbook names>
  outputs    : <D3 A both / B Fabric only / C local only>
  conflicts  : <D4 overwrite | skip | stop>
  auth       : <D5 PAT | Connected App JWT>   (secret from KV <KV_NAME>/<SECRET_NAME>)
  fabric ws  : <FABRIC_WORKSPACE>             (omit if D3=C)
```

Run nothing until the user replies `GO`.

### The 3-step runbook (literal flags — do not alter)

> Flags below are exactly what the bundled scripts accept (`--help`-verified). `fetch_tds.py`
> downloads **one datasource per call** (there is no `--all`) and writes with `--out`;
> `migrate_estate.py` takes `-i/-o` and emits `<out>/semantic_models/<Name>.SemanticModel` +
> `report.json` + `summary.md`; `deploy_to_fabric.py` deploys **one** `--model-dir` per call.

PowerShell (Windows lead). Dot-source the vars first: `. .\migration.vars.local.ps1`.

**STEP 1 — assemble `.\tds` (one `.tds`/`.tdsx` per datasource, or a `.twb`/`.twbx` per workbook)**

- **D1=B (local):** drop your exported files into `.\tds` — `.tds`/`.tdsx` **datasources** and/or
  `.twb`/`.twbx` **workbooks** — then go to STEP 2. For a **flat-file or extract-backed** source
  (Excel/CSV, or a `… - Extract` source carrying a `.hyper`), export the **packaged** form
  (`.tdsx`/`.twbx`) so the data travels inside the file — STEP 2 lifts it to an absolute path.
- **D1=A (live):** pull the secret from Key Vault, then loop `fetch_tds.py` per datasource name:

```powershell
$env:TABLEAU_PAT_VALUE = az keyvault secret show --vault-name $KV_NAME --name $SECRET_NAME --query value -o tsv
New-Item -ItemType Directory -Force -Path .\tds | Out-Null
foreach ($ds in @("<Datasource A>","<Datasource B>")) {   # D2 scope
  py -3.11 scripts\fetch_tds.py --server $SITE_URL --site $SITE_NAME `
    --datasource-name $ds --auth pat --pat-name $PAT_NAME --out .\tds
}
```

D5=B (JWT): replace `--auth pat --pat-name $PAT_NAME` with
`--auth jwt --client-id $CA_CLIENT_ID --secret-id $CA_SECRET_ID --secret-value $env:TABLEAU_PAT_VALUE --jwt-username $JWT_USERNAME`.

- **D1=A (live workbook + its embedded datasource):** `fetch_tds.py` also downloads a published
  **workbook** — in the loop above swap `--datasource-name $ds` for `--workbook-name "<Workbook>"`
  (or `--workbook-luid <luid>`) and keep `--out .\tds`. STEP 2's `migrate_estate.py` ingests the
  `.twb`/`.twbx` from `.\tds` and rebuilds the embedded datasource as a semantic model **and** the
  workbook as a report — no separate datasource fetch is needed for an embedded source.

> **`--include-extract` is REQUIRED for a flat-file / extract-backed source.** Add it to the
> `fetch_tds.py` call for any workbook or datasource whose data is an Excel/CSV file or a Tableau
> extract (`.hyper`). It downloads the **packaged** `.twbx`/`.tdsx` with the data inside; STEP 2 then
> materializes that data to an **absolute** path (Excel/CSV lifted as-is; a `.hyper` extract read to
> one CSV per table) so the Import model loads rows. Omit it and only the metadata travels, so the
> model opens **empty** with a relative-path error. (A live DB source — SQL Server / Snowflake /
> Postgres — needs no extract; it repoints at the live connection.)

**Checkpoint 1:** `.\tds` holds one `.tds` per requested datasource (or the requested `.twb`/`.twbx`
per workbook). Fewer than expected → STOP.

**STEP 2 — build the Fabric bundle**

```powershell
py -3.11 scripts\migrate_estate.py -i .\tds -o .\out
```

**Checkpoint 2:** `.\out\semantic_models` has one `*.SemanticModel` per datasource and
`.\out\report.json` shows `summary.datasources_migrated > 0`. For a **workbook** source, `.\out\pbip`
also holds an openable `<Workbook>.pbip` (the rebuilt report bound to its model), and
`report.json` lists the workbook with its `flatfile_data.landed` status — confirm it is `true` for a
flat-file/extract source (if `false`, re-fetch with `--include-extract`). Empty / `0` → STOP and read
`.\out\summary.md`.

**STEP 3 — deploy (skip entirely if D3=C / local only)**

Deploy each model folder (one `--model-dir` per call):

```powershell
Get-ChildItem .\out\semantic_models -Directory | ForEach-Object {
  py -3.11 scripts\deploy_to_fabric.py --model-dir $_.FullName --workspace $FABRIC_WORKSPACE --use-az
}
```

D4 handling: overwrite redeploys same-named models; skip → exclude existing names from the loop;
stop → halt on the first conflict and ask. If a model binds an on-prem source, add
`--gateway-id <id>`.

**Checkpoint 3:** each deploy completes its LRO without error. Any failure → STOP, do not continue.
If D3=B (Fabric only), remove `.\out` after a clean deploy; if D3=A, keep it.

bash equivalent: same flags with `python3.11` instead of `py -3.11`; export the same variables in
your shell (or a local, git-ignored file you `source`) and read the secret with
`az keyvault secret show --vault-name "$KV_NAME" --name "$SECRET_NAME" --query value -o tsv` into
`TABLEAU_PAT_VALUE`.

<!-- ===== Run contract ends; detailed reference body below ===== -->

---

> **Updating this skill — only when the user asks**
> There is **no** mandatory per-session update check. When the user asks to *check for updates / update / upgrade / refresh the `tableau-migration` skill* (or "update yourself"), follow [`resources/self-update.md`](resources/self-update.md). It is a **version-aware reinstaller**, not a guess:
> - **Source of truth:** repo `https://github.com/Yarbrdab000/tableau-fabric-skills`, skill subpath `skills/tableau-migration`, version stamp `skills/tableau-migration/VERSION`. **Install target:** the folder this `SKILL.md` was loaded from (canonical); `~/.copilot/skills/tableau-migration` is a manual-only fallback.
> - **Compare, then act:** read installed `VERSION` → read remote `VERSION` → only reinstall if remote is newer (or the user forces). Install is an **explicit wholesale overwrite** (`scripts/` + `resources/` + `SKILL.md` + `VERSION`), then a **fail-loud verification** (assert `migrate_datasource` / `extract_calcs` / `fetch_tds` exist + run `pytest`; on failure, restore the backup and stop). Finish by reporting the delta (e.g. `1.2.1 → 1.3.0`).
> - **Mid-session caveat:** skills load at session start, so the update is not live until a **new** session.

> **CRITICAL NOTES**
> 1. To find the workspace details (including its ID) from a workspace name: list all workspaces, then use JMESPath filtering.
> 2. To find the item details (including its ID) from workspace ID, item type, and item name: list all items of that type in that workspace, then use JMESPath filtering.
> 3. **Column types are driven by the source schema, never guessed.** The DirectLake path types columns from the landed Delta schema; the Import/DirectQuery path types them from the Tableau `.tds` `<metadata-records>`. A datasource with no resolvable column metadata falls back to the land-to-Delta path — it is never deployed with inferred types.
> 4. **Calculated-field translation is a deterministic safe subset, not full coverage.** Anything outside the subset stays an inert `= 0` stub; the original Tableau formula is ALWAYS preserved as a `TableauFormula` annotation so a human (or an optional validation-gated LLM pass) can finish it. Never claim full DAX parity.
> 5. **Credentials and on-premises gateways are a manual security boundary.** This skill emits the model, the connection parameters, and the structured **bind inputs** (`connection_details_for_bind`), and can deploy the model itself via the bundled `scripts/deploy_to_fabric.py` (or delegate to `semantic-model-authoring`) — but the user enters credentials and selects/sets up the gateway. On a credential error, stop and have the user configure the connection.

# Tableau → Microsoft Fabric Semantic Model Migration

This skill packages a proven Tableau → Fabric toolkit as a reusable migration skill. The **north star
is estate-wide rebuild** — point at a Tableau deployment and rebuild its datasources, calculated fields,
and workbooks as equivalent Fabric / Power BI assets, with **executed reconciliation** verifying the
numbers actually match. **Available today:** the semantic-model path — rebuild the datasource (data model
+ relationships), translate calculated fields to DAX, and wire the connection — **plus single-entry
estate orchestration** and a **preview of workbook / worksheet → Power BI (PBIR) report rebuild**:
Tier-1 *structure* (chart type, exact field bindings, position/layout, filters/parameters → slicers,
default cross-filter, structural titles/axis names) rebuilt into an openable, model-bound `.pbip`.
**Actively landing:** model-object enrichment (hierarchies / display folders / RLS) and visual
*formatting* (specific colors, fonts, legends, conditional formats — deferred to a later pass). See
[§ Feature Parity](#feature-parity-reference) for current vs. in-progress coverage.

## Inputs — Locate the Datasource FIRST

> **The datasource to migrate is supplied by the user. Do NOT assume it lives in the current repo or working directory.** This skill is the migration *toolkit*, not a datasource — a fresh checkout contains no `.tds`. Do **not** search the working directory, find nothing, and stall. Before any other phase, establish the input by asking the user which route applies:
>
> - **(A) Local file** *(simplest — no Tableau credentials)* — the user has a Tableau file. Ask for the path to a `.tds`, `.tdsx`, `.twb`, or `.twbx`. `.tdsx`/`.twbx` are zips: extract the inner `.tds`/`.twb` first. Always read with `encoding="utf-8-sig"` (the files carry a UTF-8 BOM).
> - **(B) Live published datasource** — the user names a datasource published on Tableau Server / Cloud (a *name*, not a file path). Pull it down first with the **`tableau-datasource-profiler`** skill (or the Tableau **Download Data Source** REST API + Metadata API) using a PAT or Connected-App JWT; that yields the `.tds` this skill consumes, plus field/lineage metadata and reconciliation values.
>
> If the user just says "migrate my Tableau datasource" without specifying, **ask which route** (file path vs. published-datasource name + Tableau connection) rather than guessing. Once you hold the `.tds`, continue to the Migration Phases below.
>
> **Workbooks may embed several datasources.** A `.twb`/`.twbx` can contain more than one datasource (worksheet reference stubs and the `Parameters` pseudo-datasource are ignored). Call `list_workbook_datasources(source)` (or `workbook_datasources(xml)`) to enumerate the real ones; if there's exactly one, it's used automatically, otherwise pass `datasource="<name>"` to `migrate_datasource` to pick. Selecting an ambiguous workbook without a `datasource=` raises `AmbiguousDatasourceError` listing the choices.

## Prerequisite Knowledge

This skill is **self-contained** — the bundled scripts cover the full migration (parse → TMDL → calc→DAX → connection → deploy). Fabric token audiences and the deploy REST flow are documented inline below and in `scripts/deploy_to_fabric.py`. When the optional peer skills (`semantic-model-authoring`, `semantic-model-consumption`) are installed alongside this one, they provide deeper general-Fabric REST / `az` references and best-practice analysis — but they are **not required**.

> **This skill can deploy the model itself via the bundled `scripts/deploy_to_fabric.py`, or delegate model deploy / edit / refresh / best-practice analysis and connection binding to the `semantic-model-authoring` skill, with DAX round-trip validation via `semantic-model-consumption` (FabricIQ `ExecuteQuery`).** It owns the Tableau-side reconstruction (datasource → TMDL, calc → DAX, connection → M).

---

## Table of Contents

| Topic | Reference |
|---|---|
| **Migration Orchestrator** | [migration-orchestrator.md](resources/migration-orchestrator.md) |
| API-Driven Migration Workflow | [§ API-Driven Migration Workflow](#api-driven-migration-workflow) |
| Migration Phases (ordered) | [§ Migration Phases](#migration-phases-execute-in-order) |
| Migration Workload Map | [§ Migration Workload Map](#migration-workload-map) |
| Storage-Mode Selection (extract/live/custom-SQL) | [storage-mode-selection.md](resources/storage-mode-selection.md) |
| Semantic Model Rebuild (TMDL, types, relationships) | [semantic-model-rebuild.md](resources/semantic-model-rebuild.md) |
| Calculated Field → DAX | [calc-to-dax.md](resources/calc-to-dax.md) |
| Second Compiler (Tier-1 assisted translation) | [second-compiler.md](resources/second-compiler.md) |
| Tier-1 Charter (Tier-0 vs Tier-1 boundary) | [tier1-charter.md](resources/tier1-charter.md) |
| Connection → M Partition & Binding | [connection-binding.md](resources/connection-binding.md) |
| Validation & Reconciliation (ExecuteQuery vs VDS) | [validation-reconciliation.md](resources/validation-reconciliation.md) |
| Migration Gotchas | [migration-gotchas.md](resources/migration-gotchas.md) |
| Security & Governance | [security-governance.md](resources/security-governance.md) |
| Migration Report | [migration-report.md](resources/migration-report.md) |
| Updating / upgrading this skill | [self-update.md](resources/self-update.md) |
| Feature Parity Reference | [§ Feature Parity Reference](#feature-parity-reference) + [feature-parity.md](resources/feature-parity.md) |
| Must / Prefer / Avoid | [§ Must / Prefer / Avoid](#must--prefer--avoid) |

### Context Loading Guide

> **IMPORTANT — Load only what you need.** Do NOT read all resource files upfront. Load the specific file for the phase you are executing:

| When | Read This File | Lines |
|---|---|---|
| User asks to migrate a datasource (full orchestration) | [migration-orchestrator.md](resources/migration-orchestrator.md) | ~210 |
| Deciding storage mode for a datasource | [storage-mode-selection.md](resources/storage-mode-selection.md) | ~150 |
| Generating the TMDL model (types, columns, relationships) | [semantic-model-rebuild.md](resources/semantic-model-rebuild.md) | ~180 |
| Translating calculated fields | [calc-to-dax.md](resources/calc-to-dax.md) | ~200 |
| A calc fell back / handling the Tier-1 handoff | [second-compiler.md](resources/second-compiler.md) | ~200 |
| Why a construct is Tier-0 vs Tier-1 (the boundary) | [tier1-charter.md](resources/tier1-charter.md) | ~190 |
| Emitting M partitions / binding the connection | [connection-binding.md](resources/connection-binding.md) | ~170 |
| Validating the migrated model | [validation-reconciliation.md](resources/validation-reconciliation.md) | ~140 |
| Troubleshooting failures | [migration-gotchas.md](resources/migration-gotchas.md) | ~120 |
| Production security setup | [security-governance.md](resources/security-governance.md) | ~110 |
| Generating the migration report | [migration-report.md](resources/migration-report.md) | ~90 |
| User asks to **update / upgrade this skill** | [self-update.md](resources/self-update.md) | ~110 |
| Feature parity / what is NOT migrated | [feature-parity.md](resources/feature-parity.md) | ~80 |

### Bundled Scripts

The pure-Python cores are offline, deterministic, and stdlib-only (no Spark / pandas required to run them):

| Script | Purpose |
|---|---|
| [`scripts/fetch_tds.py`](scripts/fetch_tds.py) | **Tableau-side download** (stdlib-only): REST sign-in (PAT **or** Connected-App JWT), find a published **datasource _or_ workbook** by name (or LUID), download it, and extract the inner `.tds` from a `.tdsx` (`inner_tds_from_zip`) **or** the inner `.tds`/`.twb` from any Tableau archive incl. `.twbx` (`inner_doc_from_zip`). CLI (`--datasource-name`/`--datasource-luid`/`--workbook-name`/`--workbook-luid`, `--include-extract`, `--out`) **and** importable (`sign_in`, `resolve_datasource_luid`, `download_datasource`, `resolve_workbook_luid`, `download_workbook`). Use this instead of hand-writing Tableau REST. |
| `calc_to_dax.py` | Deterministic, typed Tableau calc → DAX translator. Recursive-descent parser: single-field aggregations + arithmetic, `IF`/`ELSEIF`/`IIF` conditionals, comparison + `AND`/`OR`/`NOT`, and `ZN`/`IFNULL`/`ISNULL`; `None` on fallback. Plus `suggest_assisted_dax` — opt-in idiom suggestions (e.g. argmax-over-a-dimension) emitted for human approval, never silently live. |
| [`scripts/translation_router.py`](scripts/translation_router.py) | **Tier-0 → Tier-1 support layer** (pure, dependency-free). `classify_fallback(reason, role, fields)` — the **router** — maps the deterministic engine's honest `fallback_reason` to a stable charter category (`model_object_parameter` / `missing_addressing_intent` / `missing_outer_aggregation` / `dax_language_gap` / `type_or_shape_mismatch` / `unresolved_reference` / `unsupported_other`) + agent guidance; drives `translation_handoff` (the second-compiler input). `check_candidate_dax(dax, request)` — the **syntactic gate** — vets a second-compiler candidate (balanced parens/brackets/quotes, not an inert stub, no leftover `{FIXED}`/`[Parameters]` idioms) before approval. See [second-compiler.md](resources/second-compiler.md). |
| [`scripts/tmdl_generate.py`](scripts/tmdl_generate.py) | TMDL generators: typed columns, tables, measures, relationship inference, model files. |
| [`scripts/field_resolver.py`](scripts/field_resolver.py) | Unambiguous caption → column resolver for the DirectLake (landed-Delta) path. |
| [`scripts/storage_mode.py`](scripts/storage_mode.py) | Per-datasource storage-mode auto-selection (pure policy). |
| [`scripts/connection_to_m.py`](scripts/connection_to_m.py) | Parse Tableau `.tds`/`.twb` → descriptor (`parse_tds(text, select=None)`); **`extract_calcs`** (calculated fields → `calcs=`); **`workbook_datasources`** (list selectable datasources, skipping `Parameters` + worksheet stubs); emit M partitions + bind details (`connection_details_for_bind`); M-path field resolver. |
| [`scripts/assemble_model.py`](scripts/assemble_model.py) | Tier-1 orchestrator: `.tds`/`.twb` → full Fabric SemanticModel definition (TMDL parts + `.platform` + `.pbism`), base64 deploy payload. **One-call `migrate_datasource(.tdsx/.tds/.twbx/.twb/text, datasource=None)` → `{parts, report, bind}`** (auto-extracts calcs; `datasource=` selects from a multi-datasource workbook; a genuine fallback returns `parts={}` + `report["landing_plan"]` via `directlake_landing_plan`); `list_workbook_datasources`, `write_model_folder` / **`write_local_pbip`** for local output. |
| [`scripts/deploy_to_fabric.py`](scripts/deploy_to_fabric.py) | Self-contained Fabric REST deploy (stdlib-only urllib): createOrUpdate / updateDefinition of the SemanticModel, 202 LRO polling, optional refresh + gateway bind. Importable `acquire_token` (handles `az` on Windows) + `refresh_dataset` for post-deploy ops. Lets the skill finish **in Fabric** without depending on a peer skill. |

For exact signatures and a copy-paste **download → migrate → deploy** snippet, see [public-api.md](resources/public-api.md).

Run the test suite with `pytest` from `skills/tableau-migration/` (900+ offline assertions).

---

## API-Driven Migration Workflow

This skill rebuilds Tableau artifacts via REST APIs — no Tableau or Fabric UI required.

### Authentication

| Target | Token Audience |
|---|---|
| Tableau REST / Metadata / VizQL Data Service | Tableau PAT or Connected-App JWT (per the Tableau server) |
| Fabric REST API (deploy, bind) | `https://api.fabric.microsoft.com` |
| Power BI dataset refresh | `https://analysis.windows.net/powerbi/api` |

> The bundled `scripts/deploy_to_fabric.py` acquires the Fabric / Power BI token for you (`--token`, the `FABRIC_TOKEN` env var, or `--use-az` → `az account get-access-token`). Tableau tokens come from the source Tableau Server/Cloud.

> **Source extraction**: the Tableau **Download Data Source** REST API returns a `.tds` (or `.tdsx` zip) — the authoritative source for connection class, server, database, relations, and column types. The **Metadata API** (GraphQL) supplies datasource/field/lineage metadata. The **VizQL Data Service** supplies real values used for reconciliation. Treat all downloaded artifacts as **sensitive plaintext**.

### Migration Phases (Execute in Order)

| Phase | Tableau Source | Fabric Target | Resource |
|---|---|---|---|
| Phase 0 | Connectivity (REST/Metadata/VDS auth) | — | [migration-orchestrator.md](resources/migration-orchestrator.md) |
| Phase 1 | Datasource metadata + `.tds` connection | Normalized descriptor | [storage-mode-selection.md](resources/storage-mode-selection.md) |
| Phase 2 | Datasource shape → storage mode | Import / DirectQuery / DirectLake decision | [storage-mode-selection.md](resources/storage-mode-selection.md) |
| Phase 3 | Schema + fields | TMDL tables, typed columns, relationships | [semantic-model-rebuild.md](resources/semantic-model-rebuild.md) |
| Phase 4 | Calculated fields | DAX measures (+ preserved formula annotations) | [calc-to-dax.md](resources/calc-to-dax.md) |
| Phase 5 | Connection | M partitions + Fabric connection bind | [connection-binding.md](resources/connection-binding.md) |
| Phase 6 | Deploy & refresh | Semantic model (bundled `scripts/deploy_to_fabric.py`; or delegate to `semantic-model-authoring`) | [migration-orchestrator.md](resources/migration-orchestrator.md) |
| Final | Validation & reconciliation | Verified model | [validation-reconciliation.md](resources/validation-reconciliation.md) |
| Optional | Security & Governance | — | [security-governance.md](resources/security-governance.md) |

> **Phase order matters**: the storage-mode decision (Phase 2) determines how columns are typed (Phase 3) and how the connection is wired (Phase 5). The DirectLake fallback path additionally requires the data to be landed as Delta first.

---

## Migration Workload Map

| Tableau Component | Fabric / Power BI Target | Notes |
|---|---|---|
| **Published Data Source** (`.tds` / `.tdsx`) | **Semantic Model** (TMDL) | The core migration unit. |
| **Physical table relation** | **Model table + partition** | One table per relation; storage mode per [storage-mode-selection.md](resources/storage-mode-selection.md). |
| **Extract** (`.hyper`) | **Import** model | Snapshot-to-snapshot; live DirectQuery offered as an alternative when the source is supported. |
| **Live connection** (SQL Server/Snowflake/Postgres/…) | **DirectQuery** model | Live-to-live via an M partition + Fabric Data Connection. |
| **Custom SQL** in a connection | **`Value.NativeQuery`** partition | Native query preserved with `[EnableFolding=true]`. SQL Server family folds against the database handle; Databricks folds against a drilled `Kind="Database"` catalog handle (never the `Catalogs()` root) and the output is aliased back to the model's `sourceColumn`s. Other connectors (e.g. Snowflake) emit a deploy-valid scaffold flagged `needs_review` for manual completion. |
| **Calculated field** (safe subset) | **DAX measure** | Aggregations (`SUM/AVG/MIN/MAX/MEDIAN/COUNT/COUNTD`) + arithmetic, `IF`/`ELSEIF`/`IIF`, comparisons + `AND`/`OR`/`NOT`, `ZN`/`IFNULL`/`ISNULL`; everything else → preserved-formula stub. |
| **Hidden join keys** (`<Base> (<Table>)`) | **Model relationship** | Direction inferred from real landed cardinality. |
| **Worksheet / Dashboard** | **Power BI report (PBIR)** | ✅ **Supported (preview)** — Tier-1 *structure* rebuilt (chart type, exact field bindings, position/layout, filters/parameters → slicers) into an openable, model-bound `.pbip`; visual *formatting* (colors, fonts, legends) is deferred to a later pass. |

### Decision Tree: Which storage mode?

```text
Tableau datasource
├── single relation that is a cross-engine join/union tree, OR a multi-connection table that
│     can't be routed to a specific upstream, OR no column metadata → FALL BACK: land-to-Delta + DirectLake
├── unknown/unmapped connector class                         → FALL BACK: land-to-Delta + DirectLake
├── flat file (Excel/CSV)                                    → Import (set file path)
├── extract enabled                                          → Import (snapshot); offer live DirectQuery if source supported
└── live relational (SQL Server/Azure SQL DB/Postgres/MySQL/Redshift) → DirectQuery (M fully emitted)
    ├── multiple named connections (each table → its own source) → DirectQuery rebuild + model relationships (DEFAULT, not a fallback)
    ├── Oracle / Snowflake / Databricks                       → DirectQuery mode; deploy-ready per-connector M emitted
    └── Teradata / BigQuery                                   → DirectQuery mode; flagged scaffold until a live navigator verifies the M
```

> **Default-direct policy.** Each table is rebuilt against its own source — **including** a federated
> datasource with several named connections, because Power BI relates the tables in the model layer.
> Land-to-Delta + DirectLake is an explicit **option**, auto-suggested only for the genuinely-undoable
> shapes above; when it triggers, `migrate_datasource` returns a `report["landing_plan"]` to act on.

> **Local-data POC (opt-in, no Fabric).** For a laptop demo — or a customer whose source connector
> has no live Power BI equivalent (S3 / MinIO, generic ODBC, Web Data Connector) and so would
> otherwise only get a `landing_plan` — pass `migrate_datasource(..., local_data=...)` to build a
> **clickable local Import model backed by real data in local CSV files**, with no Fabric workspace,
> lakehouse, or Azure Key Vault. `local_data` accepts a `{table: csv_path}` map, a directory of
> `*.csv`, a single `.csv`, a `.hyper`/`.tdsx`/`.twbx` file, or `True` (auto-extract the source's own
> `.hyper`). It reuses the proven `Csv.Document` Import generator (typed columns, calc→DAX, Date
> dimension, relationships, parameters) and reports under the additive `report["local_import"]` key.
> Auto-extracting a `.hyper` needs the optional `tableauhyperapi` wheel (`pip install tableauhyperapi`);
> bringing your own CSVs needs no extra dependency. **Limitation:** column types/renames line up only
> when the CSV headers match the `.tds` `<metadata-records>` remote names — otherwise the data still
> loads (headers promoted) but those columns stay untyped. When `local_data` is omitted the run is a
> byte-identical no-op.

See [storage-mode-selection.md](resources/storage-mode-selection.md) for the full policy and `scripts/storage_mode.py` for the executable version.

---

## Must / Prefer / Avoid

### MUST DO
- **Type every column from the source schema** (landed Delta for DirectLake, `.tds` `<metadata-records>` for Import/DirectQuery). Never deploy a model with inferred/guessed types — fall back instead.
- **Preserve every original Tableau formula** as a `TableauFormula` annotation on its measure, translated or not. This is the audit/repair safety net.
- **Default to a direct per-table rebuild** — each table binds to its own source, and Power BI relates multi-source tables in the model layer (so a federated, multi-connection datasource rebuilds direct, not via a lakehouse). Land-to-Delta + DirectLake is the explicit **option**, used only when a shape genuinely can't be rebuilt directly: a cross-engine join/union relation tree, a multi-connection table that can't be routed to a specific upstream, an unmapped connector, or missing column metadata. On that path `migrate_datasource` returns `report["landing_plan"]`.
- **Land data as Delta before generating a DirectLake model** — DirectLake binds to OneLake Delta, so the tables must exist first.
- **Deploy with the bundled `scripts/deploy_to_fabric.py`** (self-contained Fabric REST) so the migration finishes in Fabric without a peer-skill dependency; **or delegate deploy / bind / refresh / best-practice analysis** to `semantic-model-authoring` when that skill is available. Either way, do not hand-roll the `createItem` request inline.
- **Validate translated measures** by reconciling `ExecuteQuery` results against Tableau VDS values before declaring parity (see [validation-reconciliation.md](resources/validation-reconciliation.md)).

### PREFER
- **The lowest-friction storage mode per datasource** (extract→Import, live→DirectQuery) over forcing one mode across the estate.
- **`DIVIDE()` over `/`** and fully qualified `'Table'[Column]` references in generated DAX — the translator already emits these, aligning to standard Power BI DAX best practices (and `semantic-model-authoring`'s dax-guidelines when that peer skill is installed) so measures pass best-practice analysis.
- **DirectQuery native query with `[EnableFolding=true]`** for custom SQL so the query folds to the source.
- **A validation-gated LLM fallback** (opt-in) for stub measures — attempt a translation grounded by the preserved formula + DAX guidelines, accept it **only** if reconciliation passes, otherwise keep the inert stub.

### AVOID
- **Do not type Power BI columns from Tableau field roles or names** — use the physical source schema.
- **Do not claim a calculated field was translated** unless the deterministic translator produced DAX (or a gated LLM pass was reconciliation-verified). A stub is `= 0`, not a translation.
- **Do not emit a blind `(server, database)` call for Oracle/Teradata/Snowflake/BigQuery** — their signature/navigation differs; emit the verified per-connector M, or a flagged scaffold, but never a guessed 2-arg call.
- **Do not expand a Tableau join/union tree into independent Power BI tables** — that changes grain and breaks measures. Fall back.
- **Do not put credentials in the model, M code, `.tds` artifacts, or the migration report** — binding links IDs only; credentials are entered by the user on the connection.

---

## Examples

See the resource files for full walkthroughs. Key quick references:

**Calculated field → DAX (safe subset)**

```text
Tableau:  SUM([Profit]) / SUM([Sales])
DAX:      DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))
```

**Conditional + null handling → DAX (still inside the subset)**

```text
Tableau:  IF SUM([Sales]) > 0 THEN ZN(SUM([Profit])) / SUM([Sales]) ELSE 0 END
DAX:      IF(SUM('Orders'[Sales]) > 0, DIVIDE(COALESCE(SUM('Orders'[Profit]), 0), SUM('Orders'[Sales])), 0)
```

**Calculated field → preserved stub (outside the subset)**

```tmdl
measure 'Profit Bucket' = 0
    annotation TableauFormula = IF [Profit] > 0 THEN "Gain" ELSE "Loss" END
```

**Assisted translation → labeled suggestion → human approval (opt-in)**

When a calc falls back to a stub, an **idiom registry** (`suggest_assisted_dax`) is consulted for
higher-level patterns whose faithful DAX is a *semantic* rewrite — e.g. **argmax-over-a-dimension**
("the city with the most sales", `IF [max city sales] = {FIXED [State],[City]:SUM([Sales])} THEN [City] END`).
A match is emitted as a **non-binding suggestion** on the still-inert measure — never silently live —
and surfaced in `report["assisted_suggestions"]`:

```tmdl
measure 'city with the most sales' = 0
    annotation TableauFormula = IF [Calculation_99] = {FIXED [State],[City]:SUM([Sales])} THEN [City] END
    annotation TranslationSuggestion = VAR __detail = CALCULATETABLE(ADDCOLUMNS(SUMMARIZE('Orders', 'Orders'[State], 'Orders'[City]), "@value", CALCULATE(SUM('Orders'[Sales]))), ALLEXCEPT('Orders', 'Orders'[State])) VAR __max = MAXX(__detail, [@value]) RETURN CONCATENATEX(FILTER(__detail, [@value] = __max), 'Orders'[City], ", ")
    annotation TranslationSuggestionPattern = argmax-dimension
```

Approval is **batch, not per-calc**: review the `assisted_suggestions` list, then re-run with the
approved subset to flip them into real measures in one pass (tagged `TranslatedBy = assisted
translation (human-approved)`). The deterministic safe-subset behavior is unchanged for everything else.

```python
from assemble_model import migrate_tds_to_semantic_model

# Pass 1 — see what the idiom registry can offer (nothing is live yet):
out = migrate_tds_to_semantic_model(tds_text, model_name="Superstore", calcs=calcs)
pending = out["report"]["assisted_suggestions"]   # [{measure, pattern, dax, confidence, caveats}, ...]

# Human approves (all / by pattern / a subset). Pass 2 — flip the approved ones into real measures:
approved = {s["measure"]: s["dax"] for s in pending}   # or filter by s["pattern"] == "argmax-dimension"
final = migrate_tds_to_semantic_model(tds_text, model_name="Superstore",
                                      calcs=calcs, approved_calc_dax=approved)
```

**Live SQL Server datasource → DirectQuery M partition**

```tmdl
expression Server = "myserver.database.windows.net" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
expression Database = "Superstore" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]

table Orders
    column Sales
        dataType: double
        sourceColumn: Sales
    partition Orders = m
        mode: directQuery
        source =
            let
                Source = Sql.Database(#"Server", #"Database"),
                Data = Source{[Schema="dbo", Item="Orders"]}[Data]
            in
                Data
```

**Storage-mode decision (script)**

```python
from connection_to_m import parse_tds
from storage_mode import select_storage_mode

descriptor = parse_tds(open("datasource.tds", encoding="utf-8").read())
decision = select_storage_mode(descriptor)
# decision -> {'mode': 'DirectQuery', 'connector': 'Sql.Database', 'fully_supported': True, ...}
```

---

## Feature Parity Reference

Full matrix in [feature-parity.md](resources/feature-parity.md). Headline parity:

| Capability | Status |
|---|---|
| Datasource → semantic model (tables, typed columns) | ✅ High parity (types from source schema). |
| Relationship inference (hidden join keys) | ✅ Inferred from real landed cardinality (DirectLake path). |
| Calculated field → DAX | ⚠️ **Safe subset only** — aggregations + arithmetic, `IF`/`ELSEIF`/`IIF`, comparisons + boolean logic, and null handling (`ZN`/`IFNULL`/`ISNULL`); LOD expressions, table calcs, and row-level/date/string functions are preserved stubs. |
| Storage mode / upstream connection | ✅ Auto-selected; `Sql.Database` family (SQL Server/Azure SQL DB/Postgres/MySQL/Redshift) plus Oracle, Snowflake, and Databricks emit deploy-ready per-connector M; Teradata/BigQuery are flagged scaffolds (live-navigator M not yet verified). |
| LOD expressions (FIXED/INCLUDE/EXCLUDE), table calcs (WINDOW_*/RUNNING_*) | ❌ Not translated — preserved as stubs for manual/LLM completion. |
| Worksheet / dashboard → Power BI report (PBIR) | ⚠️ **Supported (preview)** — Tier-1 *structure* (chart type, exact field bindings, position/layout, filters/parameters → slicers, default cross-filter, structural titles/axis names) rebuilt into an openable, model-bound `.pbip`; visual *formatting* (colors, fonts, legends, conditional formats) is not yet applied (deferred to a later pass). |
| Row-level security (wired user filters) | ⚠️ Translatable `USERNAME()` filters → TMDL `role`; group/compound logic fails closed (`FALSE()` + manual-review). |
| Parameters, sets, groups | ❌ Not migrated in v1 — flagged in the report. |

> **Key gaps**: calc coverage is a deterministic safe subset (not full); dashboard/worksheet rebuild is preview-level (Tier-1 *structure* only — chart type, exact field bindings, layout, slicers — with visual *formatting* such as colors/fonts/legends deferred to a later pass); parameters/sets/groups are **not rebuilt** as model objects (parameter-driven slicers are, however, surfaced on the rebuilt report). RLS is partially automated — wired `USERNAME()` filters become roles, while group/compound logic fails closed for deliberate review. The preserved `TableauFormula` annotations make every translated/stubbed measure auditable and repairable.

---

## Migration Gotchas — Quick Reference

Full guide in [migration-gotchas.md](resources/migration-gotchas.md).

| # | Flag ID | Issue | Blocks? | Resolution Summary |
|---|---|---|---|---|
| G1 | `TYPE_FROM_TABLEAU_METADATA` | Column typed from Tableau role/name instead of the physical schema → DirectLake bind fails | Yes | Type from landed Delta / `.tds` metadata; if absent, fall back. |
| G2 | `CALC_FALLBACK_STUB` | Calculated field outside the safe subset emitted as `= 0` | No | Expected — original formula preserved; repair manually or via gated LLM. |
| G3 | `JOIN_TREE_UNSUPPORTED` | Federated join/union tree treated as one logical table | Yes | Fall back to land-to-Delta + DirectLake; do not split into tables. |
| G4 | `CONNECTOR_NOT_EMITTED` | Teradata/BigQuery navigation not yet verified against a live navigator (Oracle/Snowflake/Databricks emit deploy-ready M) | Partial | Emit deploy-ready M where verified, else a flagged scaffold; never a guessed 2-arg call. |
| G5 | `NATIVE_QUERY_NO_FOLD` | Custom SQL native query won't fold in DirectQuery | Partial | Keep `[EnableFolding=true]`; if it still fails, switch that table to Import. |
| G6 | `CREDENTIALS_MANUAL` | Bind succeeds but refresh fails (no credentials) | Yes | User configures credentials on the connection; bind links IDs only. |
| G7 | `GATEWAY_REQUIRED` | DirectQuery to an on-premises source needs a data gateway | Yes | User sets up / selects a gateway for the connection. |

---

## Validation & Testing

See [validation-reconciliation.md](resources/validation-reconciliation.md). The migration is validated by:

1. **Structural** — model deploys and refreshes (DirectLake frames / Import loads / DirectQuery connects) without error.
2. **Translation self-tests** — `pytest` runs 900+ offline tests (translator subset + fallbacks + TMDL render + storage-mode policy + `.tds`/`.twb` parsing + workbook-datasource selection + landing-plan fallback + deploy payload builders).
3. **Value reconciliation (highest value)** — run each translated measure via `semantic-model-consumption` (`ExecuteQuery`) and compare to the Tableau VDS value pulled by the profiler. A measure is "verified" only when the numbers match.

---

## Security & Governance

See [security-governance.md](resources/security-governance.md). Key boundaries:

- **Credentials never leave the user.** Downloaded `.tds`/`.tdsx`/workbook artifacts are sensitive plaintext — do not commit them, embed them in the model/report, or include them in the migration report.
- **Binding links connection IDs only**; the user supplies credentials on the Fabric connection and sets up any on-prem gateway.
- **Never bind a source credential for the user — even via API.** A semantic model's TMDL has no password field; credentials live on a separate Fabric data connection the model binds to by ID. Setting them via REST still means transmitting the secret *and* requires the gateway's asymmetric (RSA-OAEP) credential flow — out of bounds. If a user pastes a secret in chat, do not write it anywhere and advise them to rotate it.
- **Least privilege** for the Tableau token (read/download scope) and the Fabric identity (`SemanticModel.ReadWrite.All` / `Item.ReadWrite.All`, model owner).

### After deploy: the credential-binding wall (expected)

A freshly deployed Import/DirectQuery model has **no credential bound**, so the first refresh fails with
`ModelRefreshFailed_CredentialsNotSpecified`. **This is success, not a bug** — the model is correct; the
human-owned bind is the only thing left. Hand off, then offer to re-trigger the refresh via API once bound:

1. Portal route — workspace → semantic model → **Settings → Data source credentials → Edit** (Basic auth + gateway if the source isn't publicly reachable).
2. **Licensing reality:** editing data-source credentials needs a **Pro / Fabric per-user** license — **F2 (or any capacity) alone is not enough**, and a trial may be expired. If the per-dataset Settings page is gated, try **Manage connections and gateways** (capacity-backed) to create a cloud connection and bind by ID, or have any Pro/Fabric-licensed colleague bind it once (it persists on the connection, not per-user).
3. Once bound by any route, re-run the refresh via the Power BI REST API (no portal needed for that step).

---

## Migration Report

See [migration-report.md](resources/migration-report.md). Every run produces an auditable report: per-datasource storage-mode decision + rationale, per-measure translation status (translated / stub + reason + preserved formula), **assisted-translation suggestions** (`report["assisted_suggestions"]` — labeled idiom matches awaiting batch human approval, never live until approved), inferred relationships, skipped tables, and the manual follow-ups (credentials, gateway, stub repair). This report is the trust artifact — it makes every gap explicit.

---

## Output: deploy to Fabric **or** write a local `.pbip`

The assemblers return `parts` (a TMDL `dict`). Three ways to land it — the agent should **not** improvise the layout (a prior pilot hand-rolled the `.pbip` and set the wrong `$schema`, which Power BI Desktop rejects):

- **Deploy to Fabric** — `fabric_definition_payload(parts)` → base64 parts for `scripts/deploy_to_fabric.py` (Fabric REST `createOrUpdate`).
- **Local semantic-model folder** — `write_model_folder(parts, "<Name>.SemanticModel")` writes a complete, valid **TMDL `.SemanticModel`** item (opens in Tabular Editor, git-reviewable, deployable). This alone is the model deliverable.
- **Openable Power BI project (`.pbip`)** — call the bundled helper; do **not** assemble the scaffold by hand:

```python
from assemble_model import write_local_pbip
write_local_pbip(parts, dest_dir, model_name="Superstore")   # → Superstore.pbip (double-click → Desktop)
```

It writes the proven layout with the **exact** schemas baked in (the part agents get wrong):

```
<Name>.pbip                  # $schema .../fabric/pbip/pbipProperties/1.0.0/schema.json ; artifacts→<Name>.Report
<Name>.SemanticModel/        # from write_model_folder(...) — the deliverable
<Name>.Report/               # thin one-page shell; definition.pbir datasetReference.byPath = ../<Name>.SemanticModel
```

The `.pbir` **`datasetReference.byPath`** is the report→model link. The default `.Report` is a thin
shell — the dataset is fully functional on its own — but the estate orchestrator now passes
`report_parts=` (from `twb_to_pbir`) to supply a **real rebuilt report** per workbook (see the note
below), and `project_name=` to name the project after the source asset. See
[semantic-model-rebuild.md](resources/semantic-model-rebuild.md).

> **Estate / local runs emit `.pbip` by default.** The one-button estate orchestrator
> (`scripts/migrate_estate.py`) writes an openable `pbip/<Name>/<Name>.pbip` for **every** migrated
> datasource — alongside (never replacing) the canonical `semantic_models/<Name>.SemanticModel/` — so a
> user can double-click straight into Power BI Desktop to explore and test each datasource. Pass
> `pbip=False` (CLI `--no-pbip`) to emit only the `semantic_models/` folders.

> **Workbooks emit an openable, model-bound `.pbip` too.** For every workbook with a rebuildable
> embedded datasource, the estate also writes a self-contained `pbip/<Workbook>/<Workbook>.pbip` — the
> Tier-1 rebuilt report (`twb_to_pbir`) bound *by path* to a sibling model rebuilt from the workbook's
> **own embedded datasource** — so the dashboard opens directly in Power BI Desktop. The per-workbook
> `viz_fidelity` list reports each visual as `rebuilt` or `warned`; anything that can't be faithfully
> bound (a lakehouse-fallback datasource, secondary datasources a single PBIR report can't bind) is
> recorded in `pbip_warnings` rather than mis-bound. The `semantic_models/` folders remain the
> canonical deploy target; the workbook `pbip/` is a self-contained local-open copy (by design).

---

## Post-Migration: What's Next

1. **Deploy** with the bundled `scripts/deploy_to_fabric.py` (self-contained Fabric REST), or **deploy & manage** with `semantic-model-authoring` when available (best-practice analysis, refresh, edits).
2. **Query & explore** with `semantic-model-consumption` and `fabriciq` (natural-language analysis over the migrated model).
3. **Offer the second compiler for any stubbed calc (end-of-run check-in).** When a run finishes with stubbed calculations (`report["summary"]["needs_review_total"] > 0`, also surfaced in `summary.md`'s **Next step** section and the per-datasource `report["datasources"][n]["translation_handoff"]`), **don't silently stop at the stubs** — proactively present a short check-in: list each stubbed calculation (name · role · why it stubbed) and **ask whether to run them through the second compiler now**. If yes, run the Tier-1 loop — author the leanest *faithful* candidate DAX → `check_candidate_dax` (the syntactic gate) → land the approved set via `approved_calc_dax` → redeploy — per [second-compiler.md](resources/second-compiler.md). If no, leave the inert stubs (the original `TableauFormula` is preserved for later). The **faithful-or-stub** charter still binds: never land a guessed or semantically-altered measure silently.
   > _Phrasing template:_ "This migration translated N of M calculations. K fell back to stubs (the original formulas are preserved): `<Calc A>`, `<Calc B>`, … Would you like me to run these through the second compiler now (author candidate DAX → validate → land)?"
4. **Open the rebuilt reports (preview)** — each workbook with a rebuildable embedded datasource already ships as an openable `pbip/<Workbook>/<Workbook>.pbip` (Tier-1 *structure* — chart type, exact field bindings, layout, slicers — bound to the model). Open it in Power BI Desktop to review the rebuilt pages; check the per-workbook `viz_fidelity` for any `warned` visuals and apply visual *formatting* (colors, fonts, legends) by hand for now — that styling layer is a later pass.
5. **(Optional) Run the image oracle to settle ambiguous chart types** — for a workbook with non-standard / "hacky" views (a dual-axis pie that renders as a donut, a running-total Gantt that reads as a waterfall, an INDEX()/RANK() bump, a donut with a KPI floating in its hole), an opt-in **agent-driven vision pass** can confirm or correct each visual's *chart type* against the original Tableau rendering — **without ever touching field bindings**. It consumes the additive per-visual `candidate_records` `twb_to_pbir` already emits, resolves an offline-first image (caller-provided file → embedded `.twb`/`.twbx` thumbnail → none), and re-binds a visual's type **only** to a type in its candidate list. Follow the numbered runbook in [image-oracle.md](resources/image-oracle.md). Sheet swaps and field bindings stay deterministic; the Tier-1 report stands on its own if you skip this.

## Related skills

- [`tableau-datasource-profiler`](../tableau-datasource-profiler/SKILL.md) — run FIRST to inventory
  fields and assess migration readiness (calculated-field count, unsupported custom SQL, RLS/user
  references) before rebuilding the datasource here.
- [`tableau-mcp-landing-zone`](../tableau-mcp-landing-zone/SKILL.md) — after migrating, stand up the
  official Tableau MCP server so business users can natural-language-query Tableau from Copilot /
  Copilot Studio.
