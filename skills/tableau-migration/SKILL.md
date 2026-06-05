---
name: tableau-migration
description: >
  Rebuild Tableau datasources as Microsoft Fabric / Power BI semantic models.
  Recreates the data model as TMDL (typed columns, inferred relationships), translates the
  safe subset of Tableau calculated fields into working DAX measures (preserving every
  original formula as an annotation), and auto-selects a storage mode per datasource —
  extract -> Import, live connection -> DirectQuery, or fall back to land-to-Delta + DirectLake —
  so the rebuilt model can point directly at the original upstream source with the least
  manual remapping. Use when the user wants to:
  (1) migrate Tableau datasources / published data sources to Power BI semantic models,
  (2) convert Tableau calculated fields to DAX,
  (3) repoint a migrated model at its original SQL Server / Snowflake / Postgres source.
  Triggers: "migrate from tableau", "tableau to fabric", "tableau to power bi",
  "tableau datasource to semantic model", "convert tableau calculation to dax",
  "tableau calculated field to dax", "rebuild tableau datasource in fabric".
---

> **Update Check — ONCE PER SESSION (mandatory)**
> The first time this skill is used in a session, run the **check-updates** skill before proceeding.
> - **GitHub Copilot CLI / VS Code**: invoke the `check-updates` skill (e.g., `/fabric-skills:check-updates`).
> - **Claude Code / Cowork / Cursor / Windsurf / Codex**: read the local `package.json` version, then compare it against the remote version via `git fetch origin main --quiet && git show origin/main:package.json` (or the GitHub API). If the remote version is newer, show the changelog and update instructions.
> - Skip if the check was already performed earlier in this session.

> **CRITICAL NOTES**
> 1. To find the workspace details (including its ID) from a workspace name: list all workspaces, then use JMESPath filtering.
> 2. To find the item details (including its ID) from workspace ID, item type, and item name: list all items of that type in that workspace, then use JMESPath filtering.
> 3. **Column types are driven by the source schema, never guessed.** The DirectLake path types columns from the landed Delta schema; the Import/DirectQuery path types them from the Tableau `.tds` `<metadata-records>`. A datasource with no resolvable column metadata falls back to the land-to-Delta path — it is never deployed with inferred types.
> 4. **Calculated-field translation is a deterministic safe subset, not full coverage.** Anything outside the subset stays an inert `= 0` stub; the original Tableau formula is ALWAYS preserved as a `TableauFormula` annotation so a human (or an optional validation-gated LLM pass) can finish it. Never claim full DAX parity.
> 5. **Credentials and on-premises gateways are a manual security boundary.** This skill emits the model, the connection parameters, and the structured **bind inputs** (`connection_details_for_bind`), but the user enters credentials and selects/sets up the gateway, and request construction/execution is delegated to `semantic-model-authoring`. On a credential error, stop and have the user configure the connection.

# Tableau → Microsoft Fabric Semantic Model Migration

This skill packages a proven Tableau → Fabric toolkit as a reusable migration skill. The **north star
is estate-wide rebuild** — point at a Tableau deployment and rebuild its datasources, calculated fields,
and workbooks as equivalent Fabric / Power BI assets, with **executed reconciliation** verifying the
numbers actually match. **Available today:** the semantic-model path — rebuild the datasource (data model
+ relationships), translate calculated fields to DAX, and wire the connection. **Actively landing:**
workbook / worksheet → Power BI (PBIR) report rebuild, single-entry estate orchestration, and
model-object enrichment (hierarchies / display folders / RLS). See
[§ Feature Parity](#feature-parity-reference) for current vs. in-progress coverage.

## Prerequisite Knowledge

These companion documents provide general Fabric REST patterns. **Do NOT read them upfront** — reference only when a phase needs a pattern not already covered here:

- [COMMON-CORE.md](../../common/COMMON-CORE.md) — General Fabric REST API patterns, authentication & token audiences, item discovery via JMESPath.
- [COMMON-CLI.md](../../common/COMMON-CLI.md) — `az rest` / `az login` CLI patterns, authentication recipes.

> **This skill delegates model deploy / edit / refresh / best-practice analysis and connection binding to the `semantic-model-authoring` skill, and DAX round-trip validation to `semantic-model-consumption` (FabricIQ `ExecuteQuery`).** It owns the Tableau-side reconstruction (datasource → TMDL, calc → DAX, connection → M); it does not re-implement model deployment.

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
| Connection → M Partition & Binding | [connection-binding.md](resources/connection-binding.md) |
| Validation & Reconciliation (ExecuteQuery vs VDS) | [validation-reconciliation.md](resources/validation-reconciliation.md) |
| Migration Gotchas | [migration-gotchas.md](resources/migration-gotchas.md) |
| Security & Governance | [security-governance.md](resources/security-governance.md) |
| Migration Report | [migration-report.md](resources/migration-report.md) |
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
| Emitting M partitions / binding the connection | [connection-binding.md](resources/connection-binding.md) | ~170 |
| Validating the migrated model | [validation-reconciliation.md](resources/validation-reconciliation.md) | ~140 |
| Troubleshooting failures | [migration-gotchas.md](resources/migration-gotchas.md) | ~120 |
| Production security setup | [security-governance.md](resources/security-governance.md) | ~110 |
| Generating the migration report | [migration-report.md](resources/migration-report.md) | ~90 |
| Feature parity / what is NOT migrated | [feature-parity.md](resources/feature-parity.md) | ~80 |

### Bundled Scripts

The pure-Python cores are offline, deterministic, and stdlib-only (no Spark / pandas required to run them):

| Script | Purpose |
|---|---|
| `calc_to_dax.py` | Deterministic, typed Tableau calc → DAX translator. Recursive-descent parser: single-field aggregations + arithmetic, `IF`/`ELSEIF`/`IIF` conditionals, comparison + `AND`/`OR`/`NOT`, and `ZN`/`IFNULL`/`ISNULL`; `None` on fallback. |
| [`scripts/tmdl_generate.py`](scripts/tmdl_generate.py) | TMDL generators: typed columns, tables, measures, relationship inference, model files. |
| [`scripts/field_resolver.py`](scripts/field_resolver.py) | Unambiguous caption → column resolver for the DirectLake (landed-Delta) path. |
| [`scripts/storage_mode.py`](scripts/storage_mode.py) | Per-datasource storage-mode auto-selection (pure policy). |
| [`scripts/connection_to_m.py`](scripts/connection_to_m.py) | Parse Tableau `.tds` → descriptor; emit M partitions + bind details; M-path field resolver. |
| [`scripts/assemble_model.py`](scripts/assemble_model.py) | Tier-1 orchestrator: `.tds` → full Fabric SemanticModel definition (TMDL parts + `.platform` + `.pbism`), base64 deploy payload. |

Run the test suite with `pytest` from `skills/tableau-migration/` (144 offline assertions).

---

## API-Driven Migration Workflow

This skill rebuilds Tableau artifacts via REST APIs — no Tableau or Fabric UI required.

### Authentication

| Target | Token Audience |
|---|---|
| Tableau REST / Metadata / VizQL Data Service | Tableau PAT or Connected-App JWT (per the Tableau server) |
| Fabric REST API (deploy, bind) | `https://api.fabric.microsoft.com` |
| Power BI dataset refresh | `https://analysis.windows.net/powerbi/api` |

> Use the token-acquisition recipe in [COMMON-CLI § Authentication Recipes](../../common/COMMON-CLI.md#authentication-recipes) for the Fabric/Power BI audiences. Tableau tokens come from the source Tableau Server/Cloud.

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
| Phase 6 | Deploy & refresh | Semantic model (delegate to `semantic-model-authoring`) | [migration-orchestrator.md](resources/migration-orchestrator.md) |
| Final | Validation & reconciliation | Verified model | [validation-reconciliation.md](resources/validation-reconciliation.md) |
| Optional | Security & Governance | — | [security-governance.md](resources/security-governance.md) |

> **Phase order matters**: the storage-mode decision (Phase 2) determines how columns are typed (Phase 3) and how the connection is wired (Phase 5). The DirectLake fallback path additionally requires the data to be landed as Delta first (the bridge toolkit's Play 2/3).

---

## Migration Workload Map

| Tableau Component | Fabric / Power BI Target | Notes |
|---|---|---|
| **Published Data Source** (`.tds` / `.tdsx`) | **Semantic Model** (TMDL) | The core migration unit. |
| **Physical table relation** | **Model table + partition** | One table per relation; storage mode per [storage-mode-selection.md](resources/storage-mode-selection.md). |
| **Extract** (`.hyper`) | **Import** model | Snapshot-to-snapshot; live DirectQuery offered as an alternative when the source is supported. |
| **Live connection** (SQL Server/Snowflake/Postgres/…) | **DirectQuery** model | Live-to-live via an M partition + Fabric Data Connection. |
| **Custom SQL** in a connection | **`Value.NativeQuery`** partition | Native query preserved with `[EnableFolding=true]`. |
| **Calculated field** (safe subset) | **DAX measure** | Aggregations (`SUM/AVG/MIN/MAX/MEDIAN/COUNT/COUNTD`) + arithmetic, `IF`/`ELSEIF`/`IIF`, comparisons + `AND`/`OR`/`NOT`, `ZN`/`IFNULL`/`ISNULL`; everything else → preserved-formula stub. |
| **Hidden join keys** (`<Base> (<Table>)`) | **Model relationship** | Direction inferred from real landed cardinality. |
| **Worksheet / Dashboard** | **Power BI report (PBIR)** | **Roadmap (v2)** — not migrated by v1. |

### Decision Tree: Which storage mode?

```text
Tableau datasource
├── join/union tree, multi-connection, or no column metadata → FALL BACK: land-to-Delta + DirectLake
├── unknown/unmapped connector class                         → FALL BACK: land-to-Delta + DirectLake
├── flat file (Excel/CSV)                                    → Import (set file path)
├── extract enabled                                          → Import (snapshot); offer live DirectQuery if source supported
└── live relational (SQL Server/Azure SQL DB/Postgres/MySQL/Redshift) → DirectQuery (M fully emitted)
    └── Oracle / Teradata / Snowflake / BigQuery            → DirectQuery mode; verified per-connector M in progress (flagged scaffold until then)
```

See [storage-mode-selection.md](resources/storage-mode-selection.md) for the full policy and `scripts/storage_mode.py` for the executable version.

---

## Must / Prefer / Avoid

### MUST DO
- **Type every column from the source schema** (landed Delta for DirectLake, `.tds` `<metadata-records>` for Import/DirectQuery). Never deploy a model with inferred/guessed types — fall back instead.
- **Preserve every original Tableau formula** as a `TableauFormula` annotation on its measure, translated or not. This is the audit/repair safety net.
- **Fall back to land-to-Delta + DirectLake** for any datasource shape that cannot be rebuilt directly: join/union relation trees, multiple named connections, unmapped connectors, or missing column metadata.
- **Run Play 3 (land data as Delta) before generating a DirectLake model** — DirectLake binds to OneLake Delta, so the tables must exist first.
- **Delegate deploy / bind / refresh / best-practice analysis** to `semantic-model-authoring`; do not hand-roll model deployment when that skill is available.
- **Validate translated measures** by reconciling `ExecuteQuery` results against Tableau VDS values before declaring parity (see [validation-reconciliation.md](resources/validation-reconciliation.md)).

### PREFER
- **The lowest-friction storage mode per datasource** (extract→Import, live→DirectQuery) over forcing one mode across the estate.
- **`DIVIDE()` over `/`** and fully qualified `'Table'[Column]` references in generated DAX — align to [`semantic-model-authoring` dax-guidelines](../semantic-model-authoring/references/dax-guidelines.md) so measures pass best-practice analysis.
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
| Storage mode / upstream connection | ✅ Auto-selected; `Sql.Database` family (SQL Server/Azure SQL DB/Postgres/MySQL/Redshift) fully emitted; Oracle/Teradata/Snowflake/BigQuery scaffolded (verified per-connector M in progress). |
| LOD expressions (FIXED/INCLUDE/EXCLUDE), table calcs (WINDOW_*/RUNNING_*) | ❌ Not translated — preserved as stubs for manual/LLM completion. |
| Worksheet / dashboard → Power BI report | ❌ **Roadmap (v2)** — not in v1. |
| Row-level security (wired user filters) | ⚠️ Translatable `USERNAME()` filters → TMDL `role`; group/compound logic fails closed (`FALSE()` + manual-review). |
| Parameters, sets, groups | ❌ Not migrated in v1 — flagged in the report. |

> **Key gaps**: calc coverage is a deterministic safe subset (not full); dashboards are deferred to v2; parameters/sets/groups are **not rebuilt** (the agent flags any present from the Tableau metadata). RLS is partially automated — wired `USERNAME()` filters become roles, while group/compound logic fails closed for deliberate review. The preserved `TableauFormula` annotations make every translated/stubbed measure auditable and repairable.

---

## Migration Gotchas — Quick Reference

Full guide in [migration-gotchas.md](resources/migration-gotchas.md).

| # | Flag ID | Issue | Blocks? | Resolution Summary |
|---|---|---|---|---|
| G1 | `TYPE_FROM_TABLEAU_METADATA` | Column typed from Tableau role/name instead of the physical schema → DirectLake bind fails | Yes | Type from landed Delta / `.tds` metadata; if absent, fall back. |
| G2 | `CALC_FALLBACK_STUB` | Calculated field outside the safe subset emitted as `= 0` | No | Expected — original formula preserved; repair manually or via gated LLM. |
| G3 | `JOIN_TREE_UNSUPPORTED` | Federated join/union tree treated as one logical table | Yes | Fall back to land-to-Delta + DirectLake; do not split into tables. |
| G4 | `CONNECTOR_NOT_EMITTED` | Oracle/Teradata/Snowflake/BigQuery signature/navigation differs from `Sql.Database` | Partial | Emit verified per-connector M when available, else a flagged scaffold; never a guessed 2-arg call. |
| G5 | `NATIVE_QUERY_NO_FOLD` | Custom SQL native query won't fold in DirectQuery | Partial | Keep `[EnableFolding=true]`; if it still fails, switch that table to Import. |
| G6 | `CREDENTIALS_MANUAL` | Bind succeeds but refresh fails (no credentials) | Yes | User configures credentials on the connection; bind links IDs only. |
| G7 | `GATEWAY_REQUIRED` | DirectQuery to an on-premises source needs a data gateway | Yes | User sets up / selects a gateway for the connection. |

---

## Validation & Testing

See [validation-reconciliation.md](resources/validation-reconciliation.md). The migration is validated by:

1. **Structural** — model deploys and refreshes (DirectLake frames / Import loads / DirectQuery connects) without error.
2. **Translation self-tests** — `pytest` runs the 144 offline assertions (translator subset + fallbacks + TMDL render + storage-mode policy + `.tds` parsing).
3. **Value reconciliation (highest value)** — run each translated measure via `semantic-model-consumption` (`ExecuteQuery`) and compare to the Tableau VDS value pulled by the profiler. A measure is "verified" only when the numbers match.

---

## Security & Governance

See [security-governance.md](resources/security-governance.md). Key boundaries:

- **Credentials never leave the user.** Downloaded `.tds`/`.tdsx`/workbook artifacts are sensitive plaintext — do not commit them, embed them in the model/report, or include them in the migration report.
- **Binding links connection IDs only**; the user supplies credentials on the Fabric connection and sets up any on-prem gateway.
- **Least privilege** for the Tableau token (read/download scope) and the Fabric identity (`SemanticModel.ReadWrite.All` / `Item.ReadWrite.All`, model owner).

---

## Migration Report

See [migration-report.md](resources/migration-report.md). Every run produces an auditable report: per-datasource storage-mode decision + rationale, per-measure translation status (translated / stub + reason + preserved formula), inferred relationships, skipped tables, and the manual follow-ups (credentials, gateway, stub repair). This report is the trust artifact — it makes every gap explicit.

---

## Post-Migration: What's Next

1. **Deploy & manage** the model with `semantic-model-authoring` (best-practice analysis, refresh, edits).
2. **Query & explore** with `semantic-model-consumption` and `fabriciq` (natural-language analysis over the migrated model).
3. **Repair stubs** — work the migration report's stub list, optionally with the validation-gated LLM pass.
4. **(v2) Rebuild reports** — once measures are trusted, regenerate Tableau worksheets/dashboards as Power BI report pages (roadmap).
