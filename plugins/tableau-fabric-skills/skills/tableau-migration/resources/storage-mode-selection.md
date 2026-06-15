# Storage-Mode Selection

How the skill picks a Power BI storage mode **per datasource** to rebuild it with the least manual
remapping — or falls back to the proven land-to-Delta + DirectLake path when a direct rebuild is unsafe.
The executable policy is `scripts/storage_mode.py`; this doc explains the *why*.

> **Goal:** minimize the customer's manual remapping. Rather than forcing one storage mode across the
> estate, choose the most feasible mode from each Tableau datasource's own connection semantics.

---

## Inputs

`select_storage_mode(descriptor)` reads only the normalized descriptor from `parse_tds` — never XML. The
fields that drive the decision:

| Field | Meaning |
|---|---|
| `connection_class` | Tableau connector class (`sqlserver`, `snowflake`, `excel-direct`, …) |
| `is_extract` | Whether a `.hyper` extract is enabled |
| `named_connection_count` | >1 ⇒ federated multi-connection; still rebuilt **directly** when every table routes to its own connection (only a table that can't be routed to a specific upstream falls back) |
| `relations[].kind` | `table` / `custom_sql` / `join` / `union` / `unknown` |
| `relations[].columns` | Resolvable, typed columns (empty ⇒ cannot type the model) |
| `unsupported_reasons` | Shape problems already found during parsing |

---

## Decision policy (first match wins)

> **Default is a direct rebuild.** Multiple named connections do **not** force the fallback: a
> federated source whose tables each resolve to their own connection is rebuilt directly as a
> multi-source model, and Tableau's join keys become model **relationships** (Power BI relates the
> tables in the model layer). The land-to-Delta + DirectLake path is an explicit **option** reserved
> for shapes a direct query genuinely can't reproduce.

```text
1. Structurally unsupported  → mode = None, fallback = land-to-delta-directlake
     - a join / union relation tree (one logical table spans multiple relations)
     - a multi-connection table that can't be routed to a specific upstream connection
     - an 'unknown' relation (unparseable table reference)
     - no table/custom-SQL relation, or none with resolvable columns
     NOTE: >1 named connection on its own is NOT unsupported — each table binds to its own source.
2. Unknown / unmapped connector class → mode = None, fallback = land-to-delta-directlake
3. Flat file (Excel / CSV)   → Import   (set the file path on the M partition)
4. Extract enabled           → Import   (snapshot); offer live DirectQuery if the source is supported
5. Live relational           → DirectQuery (live-to-live), INCLUDING a multi-connection federation
                               (each table binds to its own upstream; joins become relationships)
```

> A `collection` relation (a container of **independent** sheets/tables, e.g. multi-sheet Excel) is NOT a
> join/union — the parser drops the container and emits each child as its own table, so it stays a clean
> multi-table Import. The same applies to the **federated cloud-warehouse object model** (Tableau 2023+):
> a real Snowflake / Databricks / Azure SQL `.tds` nests the true class in a `federated` named connection
> and lists its tables as a `collection` of three-part `[catalog].[schema].[table]` relations. The parser
> promotes those to typed `table` relations (validated against real Snowflake and Databricks Superstore
> exports — 3 tables each), so they rebuild as **N DirectQuery tables** instead of falling back to
> land-to-Delta. See [connection-binding.md](connection-binding.md) for the parsing details.

### Connector support tiers

| Tier | Connector classes | M emission |
|---|---|---|
| **Fully supported** | `sqlserver`/`azure_sqldb`/`azure_sql_dw` (Synapse)/`microsoft_fabric_sql_endpoint` (Fabric)→`Sql.Database`, `postgres`→`PostgreSQL.Database`, `mysql`→`MySQL.Database`, `redshift`→`AmazonRedshift.Database` (server+database), `oracle`→`Oracle.Database` (server-only, flat nav), `snowflake`→`Snowflake.Databases` (server+warehouse, db→schema→table nav), `databricks`→`Databricks.Catalogs` (host+HTTP path, catalog→schema→table nav) | Deploy-ready M |
| **Partial (scaffold)** | `bigquery`→`GoogleBigQuery.Database`, `teradata`→`Teradata.Database` | Mode chosen, M emitted as a clearly-flagged scaffold |
| **Flat file** | `excel-direct`/`excel`→`Excel.Workbook`, `textscan`/`csv`→`Csv.Document` | Import; path-based scaffold (needs file path) |
| **Analysis Services** | `msolap`, `sqlserver-analysis-services` | Not an M rebuild — routed to `analysis-services-model-migration` (migrate the model directly via XMLA / semantic-model import) |
| **Unmapped** | anything else | Fall back to land-to-Delta + DirectLake |

> **All Microsoft TDS-protocol sources are Fully supported via `Sql.Database`.** Azure SQL Database
> (`azure_sqldb`), Azure Synapse Analytics — dedicated SQL pool (`azure_sql_dw`), Azure SQL Managed
> Instance, the Synapse **serverless** SQL pool, and the Microsoft Fabric Warehouse / Lakehouse SQL
> endpoint (`microsoft_fabric_sql_endpoint`) all speak the SQL Server protocol; Managed Instance and
> serverless Synapse arrive as Tableau class `sqlserver` / `azure_sqldb` (already mapped), dedicated
> Synapse as `azure_sql_dw`, and the dedicated Fabric endpoint as `microsoft_fabric_sql_endpoint`. The
> `azure_sql_dw` and `microsoft_fabric_sql_endpoint` class strings are web-verified — a wrong class
> string only causes a safe fallback (never wrong M); the TDS→`Sql.Database` mapping is the verified fact.

> Tier membership is gated on a verified fact (from the Power Query M docs). The `(server, database)`
> family (including Synapse + Fabric), Oracle (`Oracle.Database(server, [options])`, server-only with
> `HierarchicalNavigation=false` and flat `[Schema, Item]` navigation), Snowflake
> (`Snowflake.Databases(server, warehouse)` then `[Name, Kind]` navigation), and Databricks
> (`Databricks.Catalogs(host, httpPath, [options])` then catalog→schema→table `[Name, Kind]` navigation)
> are **Fully supported** — each emitted from its own verified signature, never a guessed call. Oracle,
> Databricks, and the `(server, database)` family are doc-verified; Snowflake is doc-informed (no M
> function reference page exists). The `(server, database)` family is **live-verified end-to-end**
> against a real **Azure SQL Database** (all Microsoft TDS-protocol variants — SQL Server, Azure SQL
> DB, Managed Instance, Synapse, Fabric SQL endpoint — share that shape, and a data gateway for an
> on-prem source is a networking step, not a conversion gap), and **Snowflake** and **Databricks**
> have each been reconciled against a **live instance**; all resolve end-to-end. **Oracle** has no
> live instance available, so its live reconciliation is still **pending** (the emitted M is
> doc-verified). For Databricks the HTTP path value and Unity Catalog name aren't carried portably in
> the `.tds`, so they are surfaced as a manual follow-up rather than guessed. `teradata` (`Teradata.Database`) has a documented server-only signature,
> but with **no live navigator** to confirm the emitted body binds it is held at the scaffold tier rather
> than shipped as deploy-ready M; `bigquery` (`GoogleBigQuery.Database`) has no M function reference page,
> so its project/dataset/table navigation and billing-project/project identifiers (it has no server) aren't
> verifiable offline. Both are recognized but emitted as flagged scaffolds, never wrong M.

> **Analysis Services is not a datasource→M rebuild.** `msolap` / `sqlserver-analysis-services` is already a
> tabular/multidimensional semantic model. `select_storage_mode` returns `mode=None` with
> `fallback="analysis-services-model-migration"` (distinct from the relational land-to-Delta fallback) and a
> rationale to migrate the model directly through its XMLA endpoint / semantic-model import;
> `emit_m_partition_source` returns a flagged scaffold rather than a naive M partition.

---

## Decision output

`select_storage_mode` returns a dict:

| Key | Meaning |
|---|---|
| `mode` | `"Import"`, `"DirectQuery"`, or `None` (fall back) |
| `connector` | Power Query connector function, or `None` |
| `fully_supported` | `True` for a doc-verified deploy-ready connector (the `(server, database)` family incl. Synapse + Fabric, plus Oracle, Snowflake, and Databricks); `False` ⇒ scaffold (e.g. Teradata, BigQuery) |
| `uses_native_query` | `True` if a custom-SQL relation is present |
| `direct_upstream_available` | For an extract: a live DirectQuery rebuild is also possible |
| `fallback` | `"land-to-delta-directlake"` when `mode is None` for a relational source; `"analysis-services-model-migration"` for an SSAS/MSOLAP source |
| `score` | Confidence 0–100 in the recommendation (higher ⇒ less manual remapping) |
| `recommended_mode` | The storage mode to default to (`"Import"`/`"DirectQuery"`); `"Import"` when `mode is None` (the manual-rebuild default — the `fallback` pipeline is otherwise authoritative) |
| `rationale` | Human-readable reason (goes into the migration report) |
| `manual_followups` | Security-boundary steps that stay with the user |

### Scored recommendation

`score` ranks **feasibility**, not data quality — how little hand-finishing the rebuild needs:

| Signal | Score |
|---|---|
| Live, fully-supported connector → DirectQuery | 95 |
| Extract over a fully-supported live source → Import | 90 |
| Flat file (Excel/CSV) → Import | 80 |
| Recognized scaffold connector (Teradata, BigQuery) | 60 |
| Unknown / structurally unsupported → fallback | 30 |
| Analysis Services (`msolap`) → model-migration fallback | 30 |
| *Custom-SQL native query present* | −10 (folding review needed) |

`recommended_mode` is always populated so callers have an actionable default: it equals `mode` for a direct
rebuild, and for the **unknown / unsupported** fallback case it defaults to **Import** (the safe choice if the
model is rebuilt directly instead of routed through the land-to-Delta + DirectLake pipeline named by `fallback`).

```python
from connection_to_m import parse_tds
from storage_mode import select_storage_mode

descriptor = parse_tds(open("datasource.tds", encoding="utf-8-sig").read())
decision = select_storage_mode(descriptor)
if decision["mode"] is None:
    route_to_land_to_delta(descriptor)            # land-to-Delta + DirectLake
else:
    rebuild_direct(descriptor, decision["mode"])  # Phases 3–5
```

---

## Why each branch

- **Extract → Import.** A Tableau extract is already a snapshot; an Import model preserves snapshot
  semantics. The Tableau refresh schedule maps to a Power BI dataset refresh. When the extract sits over a
  supported live source, `direct_upstream_available` lets you offer a live DirectQuery rebuild as an
  explicit alternative.
- **Live → DirectQuery.** Keeps the live-to-live contract the Tableau datasource had, via an M partition +
  Fabric Data Connection (plus a gateway if the source is on-premises).
- **Custom SQL → native query (preserved).** The Tableau custom SQL becomes a `Value.NativeQuery(...,
  [EnableFolding=true])` partition so it can fold to the source instead of being re-expressed. Folding is
  requested, not guaranteed — verify it folds before refresh.
- **Fallback → land-to-Delta + DirectLake (the explicit lakehouse option).** Reserved for the shapes
  a direct query can't reproduce: a single cross-engine `join`/`union` relation tree, a multi-connection
  table that can't be routed to a specific upstream, missing column metadata, and unmapped connectors.
  These are landed as Delta first and bound via DirectLake. A **multi-connection
  federation is NOT a fallback** — it rebuilds directly with model relationships. When `migrate_datasource`
  does hit a genuine fallback it returns `parts={}` with a `report["landing_plan"]`
  (`directlake_landing_plan`): the per-table `{datasource}_{table}` Delta names, credential-free bind
  targets, inferred relationships, a VDS snapshot landing mechanism, and per-connector native
  shortcut/mirror cutover guidance — see [public-api.md](public-api.md).

## Friction the skill removes vs. what stays manual

| Automated | Manual (security boundary) |
|---|---|
| Storage-mode choice | Entering connection **credentials** |
| M / connection parameters | On-prem **gateway** setup for DirectQuery |
| Column typing, relationship inference | Reviewing custom-SQL folding before refresh |
| Connection bind inputs | Repairing calc → DAX stubs |
| Auth-method label → advised credential type | Setting a Snowflake **compute warehouse** when the `.tds` carried an empty one |

> **Empty Snowflake warehouse.** When a real Snowflake `.tds` stores `warehouse=''`, the rebuild still
> chooses DirectQuery and emits a valid `#"Warehouse"` parameter, but `manual_followups` flags that a
> compute warehouse must be set before refresh (and the emitted M carries a `///` TMDL description saying
> the same). Snowflake cannot run queries without a warehouse, so this stays a user step.

## DirectLake is never auto-selected here

DirectLake is only reached via the explicit fallback (it binds to OneLake Delta, so the data must be landed
first). The friction-minimizing default is to point the model directly at the original upstream source via
Import/DirectQuery. A Shortcut/Mirror-into-OneLake + DirectLake path remains available as a deliberate
alternative for enterprise sources, but is not the default.
