# Migration Orchestrator

End-to-end order of operations for migrating one Tableau **published data source** to a Microsoft
Fabric / Power BI **semantic model**. Read this when the user asks for a full migration; load the
per-phase resource docs on demand as you reach each phase.

> **Scope reminder:** this orchestrator covers the semantic-model path — data model, typed columns,
> relationships, calculated field → DAX, and the upstream connection. Worksheet / dashboard → Power BI
> report is **supported as a preview** (Tier-1 structure, bound into an openable `.pbip`) via the estate
> orchestrator and `twb_to_pbir`; visual formatting is a later pass. See
> [viz-rebuild.md](viz-rebuild.md) and [feature-parity.md](feature-parity.md).
>
> **Migrating a workbook (not a bare datasource)?** Follow the strict workbook procedure in
> **SKILL.md STEP 1** — download every workbook with `fetch_tds.py --workbook-name` (never hand-roll a
> REST downloader or unzip the `.twbx`), and when the workbook connects to a **published** datasource,
> co-migrate that datasource in the **same** `.\tds` so the workbook's model carries both the
> datasource's and the workbook's calculations. `migrate_estate.py` auto-detects embedded vs published.

---

## Inputs you need before starting

| Input | How to get it | Used by |
|---|---|---|
| Tableau auth (PAT or Connected-App JWT) | From the source Tableau Server / Cloud | Download Data Source, Metadata API, VDS |
| Datasource `.tds` / `.tdsx` | Tableau **Download Data Source** REST API | Phase 1–5 (connection, schema, calcs) |
| Datasource/field/lineage metadata | Tableau **Metadata API** (GraphQL) | Relationship inference, report |
| Real measure values (for validation) | Tableau **VizQL Data Service** (VDS) | Final reconciliation |
| Fabric workspace + target identity | `https://api.fabric.microsoft.com` token | Phase 6 deploy / bind |

> Treat every downloaded artifact (`.tds`, `.tdsx`, `.twb`) as **sensitive plaintext**. Do not commit
> it, embed it in the model, or paste it into the migration report. See `security-governance.md`.

---

## Phase flow

```text
Phase 0  Connectivity .......... authenticate to Tableau (REST/Metadata/VDS) and Fabric
Phase 1  Extract source ........ Download Data Source -> .tds ; parse_tds() -> descriptor
Phase 2  Storage mode .......... select_storage_mode(descriptor) -> Import | DirectQuery | fallback
Phase 3  Rebuild model ......... TMDL tables + typed columns + inferred relationships
Phase 4  Calc -> DAX ........... Tier 0 translates the safe subset (formula kept as annotation), then the second compiler (Tier 1) runs automatically over any stubs
Phase 5  Connection ............ emit M partitions + bind the Fabric Data Connection
Phase 6  Deploy & refresh ...... bundled deploy_to_fabric.py — model + report (--pbip) (or DELEGATE model to semantic-model-authoring)
Final    Validate ............... reconcile ExecuteQuery vs VDS ; emit the migration report
```

**Why this order:** the Phase 2 storage-mode decision determines how columns are typed (Phase 3) and how
the connection is wired (Phase 5). Calc → DAX (Phase 4) and relationship inference are storage-mode
agnostic. The DirectLake fallback additionally requires the data to be landed as Delta first
before a model can bind.

---

## Phase 0 — Connectivity

- Acquire a Tableau token (PAT name + secret, or Connected-App JWT). Sign in to the REST API to get a
  site-scoped credentials token; keep it out of all output.
- Acquire a Fabric token for `https://api.fabric.microsoft.com` (the bundled `scripts/deploy_to_fabric.py` does this via `--token` / `FABRIC_TOKEN` / `--use-az`).
- Resolve the target Fabric **workspace ID** by listing workspaces and filtering by name (JMESPath).

## Phase 1 — Extract the source

1. Call **Download Data Source** for the published datasource → `.tdsx` (zip) or `.tds` (XML).
2. If `.tdsx`, extract the inner `.tds` (it is a zip; the `.tds` lives at the root or under `Data/`).
3. Parse it:

```python
from connection_to_m import parse_tds
descriptor = parse_tds(open("datasource.tds", encoding="utf-8-sig").read())
```

`descriptor` is JSON-serializable and contains **no credentials**: `connection_class`, `server`,
`database`, `is_extract`, `named_connection_count`, `relations` (each with `kind`, typed `columns`),
and `unsupported_reasons`. See `connection-binding.md` for the descriptor shape.

## Phase 2 — Storage-mode decision

```python
from storage_mode import select_storage_mode
decision = select_storage_mode(descriptor)
```

Branch on `decision["mode"]`:

- `"Import"` / `"DirectQuery"` → continue to Phase 3 with that mode.
- `None` (with `decision["fallback"] == "land-to-delta-directlake"`) → this datasource shape is not safe
  to rebuild directly (join/union tree, multi-connection, unmapped connector, or no column metadata).
  Route it to the **land-to-Delta + DirectLake** path instead.

Record `decision["rationale"]` and `decision["manual_followups"]` for the migration report. Full policy in
`storage-mode-selection.md`.

## Phase 3 — Rebuild the model (TMDL)

Generate one model table per `table` / `custom_sql` relation, with columns typed from the source schema
(never inferred). Infer relationships from Tableau's hidden join keys. See `semantic-model-rebuild.md`.

## Phase 4 — Calculated fields → DAX

For each calculated field, build a field resolver and translate:

```python
from connection_to_m import build_m_field_resolver   # Import/DirectQuery path
# (or field_resolver.build_field_resolver for the DirectLake/landed-Delta path)
from calc_to_dax import translate_tableau_calc_to_dax

resolve = build_m_field_resolver(descriptor)
dax, reason, _ = translate_tableau_calc_to_dax(formula, resolve)
```

A non-`None` `dax` is a real translation; `None` means the formula is outside the safe subset and must be
emitted as an inert `= 0` stub. **Always** attach the original formula as a `TableauFormula` annotation.
See `calc-to-dax.md`.

**Then offer the second compiler — an explicit, user-gated opt-in.** The deterministic pass above is
Tier 0; the moment it leaves any calc as a stub (`needs_review_total > 0`), **present the stubbed calcs
and ask the user whether to run the LLM-assisted second compiler (Tier 1)** — always offer it when a stub
remains, but run it **only** on an explicit `GO`. If the user declines, the deterministic result ships
as-is with every stub's `TableauFormula` preserved. Once the user says `GO`, run the pass in full: for
each stubbed calc, read its categorized handoff request, author the leanest faithful candidate DAX,
**validate** it (`check_candidate_dax` always; the reconciliation oracle when data is landed), and **land
every validated candidate automatically via `approved_calc_dax`** (no per-calc approval). The validation
gate, not a person, is the faithfulness guarantee — a candidate that cannot be validated stays an inert
stub. Full playbook: `second-compiler.md`.

## Phase 5 — Connection → M partition + bind

Emit the M partition(s) and connection parameters, then bind the Fabric Data Connection (delegated). See
`connection-binding.md`. Credentials and any on-prem gateway stay with the user.

## Phase 6 — Deploy & refresh

**Two paths — pick one:**

**A. Self-contained (bundled).** Deploy straight to Fabric with `scripts/deploy_to_fabric.py` (stdlib-only
Fabric REST — `createOrUpdate` / `updateDefinition`, 202 LRO polling, optional refresh + gateway bind):

```bash
# Dry run first — read the model folder, build the payload, print the plan (no network):
py -3.11 scripts/deploy_to_fabric.py --model-dir <ModelFolder>.SemanticModel --workspace "<workspace>" --dry-run

# Real deploy (token from --token, env var, or --use-az → `az account get-access-token`):
py -3.11 scripts/deploy_to_fabric.py --model-dir <ModelFolder>.SemanticModel --workspace "<workspace>" --refresh
```

Inside a **Fabric notebook**, get a token with no `az login`:
`notebookutils.credentials.getToken("https://api.fabric.microsoft.com")` → pass to `deploy_model(token=...)`.

> **Credential-free ProcessRecalc (default).** After deploy the script runs a `type: Calculate`
> refresh (`recalc_dataset`) that processes the model's self-contained Import calc tables (the auto
> `Date` table + `_Measures`) so a composite/DirectQuery model opens without benign "needs refresh"
> warning triangles. It performs **no `ProcessData`** — no datasource credentials, no query to the
> DirectQuery source. On by default (best-effort — skipped with a log line if no Power BI token);
> pass `--no-recalc` to disable. This is distinct from `--refresh` (a full data load that DOES need
> the bound credential).

> **Cardinality upgrade (opt-in, `--upgrade-cardinality`).** Once the model is queryable (credentials
> bound + a first refresh), the script reads `relationships.tmdl` back and DAX-probes each DirectQuery
> many-to-many join's **target** column via `executeQueries`; a genuinely unique target is upgraded to
> many-to-one, others stay m:m. GUID-preserving and best-effort (any doubt keeps the safe m:m); no
> secret is touched. `--finalize` chains bind → recalc → refresh → upgrade-cardinality in one switch.

**Report deploy (migrated workbooks).** For a PBIP bundle (model + `.Report`), add the report as a
Fabric `reports` item — deploy the model first, then rebind the report `byConnection` to it (a `byPath`
reference does not bind over REST). Same script:

```bash
# Deploy the model AND its report from a produced bundle (report rebound byConnection):
py -3.11 scripts/deploy_to_fabric.py --pbip <out>/pbip/<Workbook> --workspace "<workspace>" --use-az

# Or deploy just the report against an already-deployed model (by GUID or name):
py -3.11 scripts/deploy_to_fabric.py --report-dir <Report>.Report --semantic-model-name <Model> --workspace "<workspace>"
```

The rebind is fail-closed: a report with no rebindable `definition.pbir` is **skipped** (recorded with
a reason), never emitted half-bound. Refresh / gateway bind remain model-only.

**B. Delegate.** Use `semantic-model-authoring` for `createOrUpdate` of the TMDL model, best-practice analysis
on the translated measures, connection binding, and refresh.

> **Either path:** entering connection **credentials** and selecting/setting up an on-prem **gateway** stay
> manual (security boundary). The deploy step links connection IDs only — it never enters credentials.

## Final — Validate & report

> **Delegate DAX execution to `semantic-model-consumption` (`ExecuteQuery`).** Run each translated measure
> and reconcile its result against the Tableau VDS value. A measure is "verified" only when the numbers
> match. See `validation-reconciliation.md`, then emit the report (`migration-report.md`).

---

## Decision points (summary)

| Decision | Where | Output |
|---|---|---|
| Direct rebuild vs land-to-Delta fallback | Phase 2 | `decision["mode"]` is `None` → fallback |
| Import vs DirectQuery | Phase 2 | extract → Import; live → DirectQuery |
| Translate vs stub a calc | Phase 4 | `dax is None` → stub (formula preserved) |
| Keep custom SQL as native query | Phase 5 | `relation["kind"] == "custom_sql"` |
| Verified vs unverified measure | Final | ExecuteQuery == VDS value |

## What stays manual (security boundary)

Entering connection **credentials**, setting up / selecting an on-prem **gateway** for DirectQuery, and
**repairing stub measures**. The skill emits everything else; it never enters credentials on the user's
behalf. On a credential error, stop and have the user configure the connection.
