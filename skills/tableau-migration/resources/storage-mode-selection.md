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
| `named_connection_count` | >1 ⇒ federated multi-connection (unsupported for direct rebuild) |
| `relations[].kind` | `table` / `custom_sql` / `join` / `union` / `unknown` |
| `relations[].columns` | Resolvable, typed columns (empty ⇒ cannot type the model) |
| `unsupported_reasons` | Shape problems already found during parsing |

---

## Decision policy (first match wins)

```text
1. Structurally unsupported  → mode = None, fallback = land-to-delta-directlake
     - >1 named connection (federated multi-source)
     - a join / union relation tree (one logical table spans multiple relations)
     - an 'unknown' relation (unparseable table reference)
     - no table/custom-SQL relation, or none with resolvable columns
2. Unknown / unmapped connector class → mode = None, fallback = land-to-delta-directlake
3. Flat file (Excel / CSV)   → Import   (set the file path on the M partition)
4. Extract enabled           → Import   (snapshot); offer live DirectQuery if the source is supported
5. Live relational           → DirectQuery (live-to-live)
```

> A `collection` relation (a container of **independent** sheets/tables, e.g. multi-sheet Excel) is NOT a
> join/union — the parser drops the container and emits each child as its own table, so it stays a clean
> multi-table Import.

### Connector support tiers

| Tier | Connector classes | M emission |
|---|---|---|
| **Fully supported** | `sqlserver`/`azure_sqldb`/`azure_sql_dw` (Synapse)→`Sql.Database`, `postgres`→`PostgreSQL.Database`, `mysql`→`MySQL.Database`, `redshift`→`AmazonRedshift.Database` (server+database), `oracle`→`Oracle.Database` (server-only), `snowflake`→`Snowflake.Databases` (server+warehouse, db→schema→table nav), `databricks`→`Databricks.Catalogs` (host+HTTP path, catalog→schema→table nav) | Deploy-ready M |
| **Partial (scaffold)** | `teradata`→`Teradata.Database`, `bigquery`→`GoogleBigQuery.Database` | Mode chosen, M emitted as a clearly-flagged scaffold |
| **Flat file** | `excel-direct`/`excel`→`Excel.Workbook`, `textscan`/`csv`→`Csv.Document` | Import; path-based scaffold (needs file path) |
| **Analysis Services** | `msolap`, `sqlserver-analysis-services` | Not an M rebuild — routed to `analysis-services-model-migration` (migrate the model directly via XMLA / semantic-model import) |
| **Unmapped** | anything else | Fall back to land-to-Delta + DirectLake |

> **All Microsoft TDS-protocol sources are Fully supported via `Sql.Database`.** Azure SQL Database
> (`azure_sqldb`), Azure Synapse Analytics — dedicated and serverless (`azure_sql_dw`), Azure SQL
> Managed Instance, and the Microsoft Fabric Warehouse / Lakehouse SQL endpoint all speak the SQL
> Server protocol; Managed Instance and the Fabric endpoint arrive as Tableau class `sqlserver`
> (already mapped), Synapse as `azure_sql_dw`.

> Tier membership is gated on a verified fact (from the Power Query M docs). The `(server, database)`
> family (including Synapse), Oracle (`Oracle.Database(server, [options])`, server-only with
> `HierarchicalNavigation=false`), Snowflake (`Snowflake.Databases(server, warehouse)` then `[Name, Kind]`
> navigation), and Databricks (`Databricks.Catalogs(host, httpPath, [options])` then catalog→schema→table
> `[Name, Kind]` navigation) are **Fully supported** — each emitted from its own verified signature, never
> a guessed call. Oracle, Databricks, and the `(server, database)` family are doc-verified; Snowflake is
> doc-informed (no M function reference page exists). Oracle, Snowflake, and Databricks have **no live
> instance** in the validation environment (Azure SQL only), so **live reconciliation is pending**; for
> Databricks the HTTP path value and Unity Catalog name aren't carried portably in the `.tds`, so they are
> surfaced as a manual follow-up rather than guessed. `Teradata.Database`'s exact navigation selector and
> BigQuery's billing-project/project identifiers (it has no server) aren't verifiable offline, so they are
> recognized but emitted as flagged scaffolds, never wrong M.

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
| `fully_supported` | `True` for a doc-verified deploy-ready connector (the `(server, database)` family incl. Synapse, plus Oracle, Snowflake, and Databricks); `False` ⇒ scaffold |
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
| Recognized scaffold connector (Teradata/BigQuery) | 60 |
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
    route_to_land_to_delta(descriptor)            # bridge Play 2/3/4
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
- **Fallback → land-to-Delta + DirectLake.** Join/union trees, multi-connection datasources, missing
  column metadata, and unmapped connectors are landed as Delta first (Play 2/3) and bound via DirectLake
  (Play 4) — the proven path that does not depend on guessing the upstream shape.

## Friction the skill removes vs. what stays manual

| Automated | Manual (security boundary) |
|---|---|
| Storage-mode choice | Entering connection **credentials** |
| M / connection parameters | On-prem **gateway** setup for DirectQuery |
| Column typing, relationship inference | Reviewing custom-SQL folding before refresh |
| Connection bind inputs | Repairing calc → DAX stubs |

## DirectLake is never auto-selected here

DirectLake is only reached via the explicit fallback (it binds to OneLake Delta, so the data must be landed
first). The friction-minimizing default is to point the model directly at the original upstream source via
Import/DirectQuery. A Shortcut/Mirror-into-OneLake + DirectLake path remains available as a deliberate
alternative for enterprise sources, but is not the default.
