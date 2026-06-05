# Migration Orchestrator

End-to-end order of operations for migrating one Tableau **published data source** to a Microsoft
Fabric / Power BI **semantic model**. Read this when the user asks for a full migration; load the
per-phase resource docs on demand as you reach each phase.

> **Scope reminder (v1):** semantic model only — data model, typed columns, relationships, calculated
> field → DAX, and the upstream connection. Worksheet / dashboard → Power BI report is **roadmap (v2)**.

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
Phase 4  Calc -> DAX ........... translate the safe subset; preserve every formula as annotation
Phase 5  Connection ............ emit M partitions + bind the Fabric Data Connection
Phase 6  Deploy & refresh ...... DELEGATE to semantic-model-authoring
Final    Validate ............... reconcile ExecuteQuery vs VDS ; emit the migration report
```

**Why this order:** the Phase 2 storage-mode decision determines how columns are typed (Phase 3) and how
the connection is wired (Phase 5). Calc → DAX (Phase 4) and relationship inference are storage-mode
agnostic. The DirectLake fallback additionally requires the data to be landed as Delta first (the bridge
toolkit's Play 2/3) before a model can bind.

---

## Phase 0 — Connectivity

- Acquire a Tableau token (PAT name + secret, or Connected-App JWT). Sign in to the REST API to get a
  site-scoped credentials token; keep it out of all output.
- Acquire a Fabric token for `https://api.fabric.microsoft.com` (see [COMMON-CLI](../../common/COMMON-CLI.md)).
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
  Route it to the **land-to-Delta + DirectLake** path (bridge Play 2/3/4) instead.

Record `decision["rationale"]` and `decision["manual_followups"]` for the migration report. Full policy in
`storage-mode-selection.md`.

## Phase 3 — Rebuild the model (TMDL)

Generate one model table per `table` / `custom_sql` relation, with columns typed from the source schema
(never inferred). Infer relationships from Tableau's hidden join keys. See `semantic-model-rebuild.md`.

## Phase 4 — Calculated fields → DAX

For each calculated field, build a field resolver and translate:

```python
from connection_to_m import build_m_field_resolver   # Import/DirectQuery path
# (or field_resolver.build_resolver for the DirectLake/landed-Delta path)
from calc_to_dax import translate_tableau_calc_to_dax

resolve = build_m_field_resolver(descriptor)
dax, reason, _ = translate_tableau_calc_to_dax(formula, resolve)
```

A non-`None` `dax` is a real translation; `None` means the formula is outside the safe subset and must be
emitted as an inert `= 0` stub. **Always** attach the original formula as a `TableauFormula` annotation.
See `calc-to-dax.md`.

## Phase 5 — Connection → M partition + bind

Emit the M partition(s) and connection parameters, then bind the Fabric Data Connection (delegated). See
`connection-binding.md`. Credentials and any on-prem gateway stay with the user.

## Phase 6 — Deploy & refresh (delegate)

> **Delegate to `semantic-model-authoring`** for createOrUpdate of the TMDL model, best-practice analysis
> on the translated measures, connection binding, and refresh. Do not hand-roll Fabric `createItem` when
> that skill is available.

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
