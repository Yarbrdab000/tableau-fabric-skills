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
| **Fully supported** (`(server, database)` family) | `sqlserver`/`azure_sqldb`→`Sql.Database`, `postgres`→`PostgreSQL.Database`, `mysql`→`MySQL.Database`, `redshift`→`AmazonRedshift.Database` | Deploy-ready M |
| **Partial (scaffold)** | `oracle`→`Oracle.Database`, `teradata`→`Teradata.Database` (server-only signature), `snowflake`→`Snowflake.Databases`, `bigquery`→`GoogleBigQuery.Database` (multi-level navigation) | Mode chosen, M emitted as a clearly-flagged scaffold |
| **Flat file** | `excel-direct`/`excel`→`Excel.Workbook`, `textscan`/`csv`→`Csv.Document` | Import; path-based scaffold (needs file path) |
| **Unmapped** | anything else | Fall back to land-to-Delta + DirectLake |

> Tier membership is decided by one verified fact (from the Power Query M docs): only connectors whose
> documented signature is `<Connector>.Database(server, database)` are **Fully supported**, so the two-argument
> call is correct rather than guessed. `Oracle.Database(server, [options])` and `Teradata.Database(server,
> [options])` take a server only, and `Snowflake.Databases` / `GoogleBigQuery.Database` navigate differently —
> so they are recognized but emitted as flagged scaffolds, never wrong M.

---

## Decision output

`select_storage_mode` returns a dict:

| Key | Meaning |
|---|---|
| `mode` | `"Import"`, `"DirectQuery"`, or `None` (fall back) |
| `connector` | Power Query connector function, or `None` |
| `fully_supported` | `True` only for the `(server, database)` family; `False` ⇒ scaffold |
| `uses_native_query` | `True` if a custom-SQL relation is present |
| `direct_upstream_available` | For an extract: a live DirectQuery rebuild is also possible |
| `fallback` | `"land-to-delta-directlake"` when `mode is None` |
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
| Recognized scaffold connector (Oracle/Teradata/Snowflake/BigQuery) | 60 |
| Unknown / structurally unsupported → fallback | 30 |
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
