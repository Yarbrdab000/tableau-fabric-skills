---
name: tableau-migration
description: >
  Rebuild Tableau datasources as Microsoft Fabric / Power BI semantic models.
  Recreates the data model as TMDL (typed columns, inferred relationships), translates the
  safe subset of Tableau calculated fields into working DAX measures (preserving every
  original formula as an annotation), and auto-selects a storage mode per datasource —
  extract -> Import, live connection -> DirectQuery. By default each table is rebuilt bound
  directly to its own source (even a federated, multi-connection datasource — Power BI relates
  the tables in the model layer), with land-to-Delta + DirectLake offered as an explicit option
  only for shapes that genuinely can't be rebuilt directly. Accepts a `.tds`/`.tdsx` datasource
  or a `.twb`/`.twbx` workbook (pick one of several embedded datasources). Use when the user wants to:
  (1) migrate Tableau datasources / published data sources to Power BI semantic models,
  (2) convert Tableau calculated fields to DAX,
  (3) repoint a migrated model at its original SQL Server / Snowflake / Postgres source.
  Triggers: "migrate from tableau", "tableau to fabric", "tableau to power bi",
  "tableau datasource to semantic model", "tableau workbook to power bi",
  "convert tableau calculation to dax", "tableau calculated field to dax",
  "rebuild tableau datasource in fabric".
---

> **Updating this skill — only when the user asks**
> There is **no** mandatory per-session update check. When the user asks to *check for updates / update / upgrade / refresh the `tableau-migration` skill* (or "update yourself"), follow [`resources/self-update.md`](resources/self-update.md). It is a **version-aware reinstaller**, not a guess:
> - **Source of truth:** repo `https://github.com/Yarbrdab000/tableau-fabric-skills`, skill subpath `skills/tableau-migration`, version stamp `skills/tableau-migration/VERSION`. **Install target** (Copilot user scope) `~/.copilot/skills/tableau-migration` — or the folder this `SKILL.md` was loaded from.
> - **Compare, then act:** read installed `VERSION` → read remote `VERSION` → only reinstall if remote is newer (or the user forces). Install is an **explicit wholesale overwrite** (`scripts/` + `resources/` + `SKILL.md` + `VERSION`), then a **fail-loud verification** (assert `migrate_datasource` / `extract_calcs` / `fetch_tds` exist + run `pytest`; on failure, restore the backup and stop). Finish by reporting the delta (`1.2.0 → 1.4.0`).
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
+ relationships), translate calculated fields to DAX, and wire the connection. **Actively landing:**
workbook / worksheet → Power BI (PBIR) report rebuild, single-entry estate orchestration, and
model-object enrichment (hierarchies / display folders / RLS). See
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
| [`scripts/fetch_tds.py`](scripts/fetch_tds.py) | **Tableau-side download** (stdlib-only): REST sign-in (PAT **or** Connected-App JWT), find a published datasource by name, download it, and extract the inner `.tds` from a `.tdsx` (`inner_tds_from_zip`) **or** the inner `.tds`/`.twb` from any Tableau archive incl. `.twbx` (`inner_doc_from_zip`). CLI **and** importable (`sign_in`, `resolve_datasource_luid`, `download_datasource`). Use this instead of hand-writing Tableau REST. |
| `calc_to_dax.py` | Deterministic, typed Tableau calc → DAX translator. Recursive-descent parser: single-field aggregations + arithmetic, `IF`/`ELSEIF`/`IIF` conditionals, comparison + `AND`/`OR`/`NOT`, and `ZN`/`IFNULL`/`ISNULL`; `None` on fallback. Plus `suggest_assisted_dax` — opt-in idiom suggestions (e.g. argmax-over-a-dimension) emitted for human approval, never silently live. |
| [`scripts/translation_router.py`](scripts/translation_router.py) | **Tier-0 → Tier-1 support layer** (pure, dependency-free). `classify_fallback(reason, role, fields)` — the **router** — maps the deterministic engine's honest `fallback_reason` to a stable charter category (`model_object_parameter` / `missing_addressing_intent` / `missing_outer_aggregation` / `dax_language_gap` / `type_or_shape_mismatch` / `unresolved_reference` / `unsupported_other`) + agent guidance; drives `translation_handoff` (the second-compiler input). `check_candidate_dax(dax, request)` — the **syntactic gate** — vets a second-compiler candidate (balanced parens/brackets/quotes, not an inert stub, no leftover `{FIXED}`/`[Parameters]` idioms) before approval. See [second-compiler.md](resources/second-compiler.md). |
| [`scripts/tmdl_generate.py`](scripts/tmdl_generate.py) | TMDL generators: typed columns, tables, measures, relationship inference, model files. |
| [`scripts/field_resolver.py`](scripts/field_resolver.py) | Unambiguous caption → column resolver for the DirectLake (landed-Delta) path. |
| [`scripts/storage_mode.py`](scripts/storage_mode.py) | Per-datasource storage-mode auto-selection (pure policy). |
| [`scripts/connection_to_m.py`](scripts/connection_to_m.py) | Parse Tableau `.tds`/`.twb` → descriptor (`parse_tds(text, select=None)`); **`extract_calcs`** (calculated fields → `calcs=`); **`workbook_datasources`** (list selectable datasources, skipping `Parameters` + worksheet stubs); emit M partitions + bind details (`connection_details_for_bind`); M-path field resolver. |
| [`scripts/assemble_model.py`](scripts/assemble_model.py) | Tier-1 orchestrator: `.tds`/`.twb` → full Fabric SemanticModel definition (TMDL parts + `.platform` + `.pbism`), base64 deploy payload. **One-call `migrate_datasource(.tdsx/.tds/.twbx/.twb/text, datasource=None)` → `{parts, report, bind}`** (auto-extracts calcs; `datasource=` selects from a multi-datasource workbook; a genuine fallback returns `parts={}` + `report["landing_plan"]` via `directlake_landing_plan`); `list_workbook_datasources`, `write_model_folder` / **`write_local_pbip`** for local output. |
| [`scripts/deploy_to_fabric.py`](scripts/deploy_to_fabric.py) | Self-contained Fabric REST deploy (stdlib-only urllib): createOrUpdate / updateDefinition of the SemanticModel, 202 LRO polling, optional refresh + gateway bind. Importable `acquire_token` (handles `az` on Windows) + `refresh_dataset` for post-deploy ops. Lets the skill finish **in Fabric** without depending on a peer skill. |

For exact signatures and a copy-paste **download → migrate → deploy** snippet, see [public-api.md](resources/public-api.md).

Run the test suite with `pytest` from `skills/tableau-migration/` (700+ offline assertions).

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
| **Custom SQL** in a connection | **`Value.NativeQuery`** partition | Native query preserved with `[EnableFolding=true]`. |
| **Calculated field** (safe subset) | **DAX measure** | Aggregations (`SUM/AVG/MIN/MAX/MEDIAN/COUNT/COUNTD`) + arithmetic, `IF`/`ELSEIF`/`IIF`, comparisons + `AND`/`OR`/`NOT`, `ZN`/`IFNULL`/`ISNULL`; everything else → preserved-formula stub. |
| **Hidden join keys** (`<Base> (<Table>)`) | **Model relationship** | Direction inferred from real landed cardinality. |
| **Worksheet / Dashboard** | **Power BI report (PBIR)** | **Roadmap (v2)** — not migrated by v1. |

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
    └── Oracle / Teradata / Snowflake / BigQuery            → DirectQuery mode; verified per-connector M in progress (flagged scaffold until then)
```

> **Default-direct policy.** Each table is rebuilt against its own source — **including** a federated
> datasource with several named connections, because Power BI relates the tables in the model layer.
> Land-to-Delta + DirectLake is an explicit **option**, auto-suggested only for the genuinely-undoable
> shapes above; when it triggers, `migrate_datasource` returns a `report["landing_plan"]` to act on.

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
2. **Translation self-tests** — `pytest` runs 717 offline tests (translator subset + fallbacks + TMDL render + storage-mode policy + `.tds`/`.twb` parsing + workbook-datasource selection + landing-plan fallback + deploy payload builders).
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

The `.pbir` **`datasetReference.byPath`** is the report→model link. The `.Report` is a thin shell until
report rebuild ships (v2) — the dataset is fully functional on its own; pass `report_parts=` (e.g. from
`twb_to_pbir`) to supply a real rebuilt report. See [semantic-model-rebuild.md](resources/semantic-model-rebuild.md).

---

## Post-Migration: What's Next

1. **Deploy** with the bundled `scripts/deploy_to_fabric.py` (self-contained Fabric REST), or **deploy & manage** with `semantic-model-authoring` when available (best-practice analysis, refresh, edits).
2. **Query & explore** with `semantic-model-consumption` and `fabriciq` (natural-language analysis over the migrated model).
3. **Repair stubs** — work the migration report's stub list, optionally with the validation-gated LLM pass.
4. **(v2) Rebuild reports** — once measures are trusted, regenerate Tableau worksheets/dashboards as Power BI report pages (roadmap).

## Related skills

- [`tableau-datasource-profiler`](../tableau-datasource-profiler/SKILL.md) — run FIRST to inventory
  fields and assess migration readiness (calculated-field count, unsupported custom SQL, RLS/user
  references) before rebuilding the datasource here.
- [`tableau-mcp-landing-zone`](../tableau-mcp-landing-zone/SKILL.md) — after migrating, stand up the
  official Tableau MCP server so business users can natural-language-query Tableau from Copilot /
  Copilot Studio.
