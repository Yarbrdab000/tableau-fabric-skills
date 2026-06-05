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
| **Fully supported** (`Sql.Database` family) | `sqlserver`→`Sql.Database`, `postgres`→`PostgreSQL.Database`, `oracle`→`Oracle.Database`, `mysql`→`MySQL.Database`, `redshift`→`AmazonRedshift.Database` | Deploy-ready M |
| **Partial (scaffold)** | `snowflake`→`Snowflake.Databases`, `bigquery`→`GoogleBigQuery.Database` | Mode chosen, M emitted as a clearly-flagged scaffold (navigation differs) |
| **Flat file** | `excel-direct`/`excel`→`Excel.Workbook`, `textscan`/`csv`→`Csv.Document` | Import; path-based scaffold (needs file path) |
| **Unmapped** | anything else | Fall back to land-to-Delta + DirectLake |

---

## Decision output

`select_storage_mode` returns a dict:

| Key | Meaning |
|---|---|
| `mode` | `"Import"`, `"DirectQuery"`, or `None` (fall back) |
| `connector` | Power Query connector function, or `None` |
| `fully_supported` | `True` only for the `Sql.Database` family; `False` ⇒ scaffold |
| `uses_native_query` | `True` if a custom-SQL relation is present |
| `direct_upstream_available` | For an extract: a live DirectQuery rebuild is also possible |
| `fallback` | `"land-to-delta-directlake"` when `mode is None` |
| `rationale` | Human-readable reason (goes into the migration report) |
| `manual_followups` | Security-boundary steps that stay with the user |

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
  [EnableFolding=true])` partition so it folds to the source instead of being re-expressed.
- **Fallback → land-to-Delta + DirectLake.** Join/union trees, multi-connection datasources, missing
  column metadata, and unmapped connectors are landed as Delta first (Play 2/3) and bound via DirectLake
  (Play 4) — the proven path that does not depend on guessing the upstream shape.

## Friction the skill removes vs. what stays manual

| Automated | Manual (security boundary) |
|---|---|
| Storage-mode choice | Entering connection **credentials** |
| M / connection parameters | On-prem **gateway** setup for DirectQuery |
| Column typing, relationship inference | Reviewing custom-SQL folding before refresh |
| Connection bind request | Repairing calc → DAX stubs |

## DirectLake is never auto-selected here

DirectLake is only reached via the explicit fallback (it binds to OneLake Delta, so the data must be landed
first). The friction-minimizing default is to point the model directly at the original upstream source via
Import/DirectQuery. A Shortcut/Mirror-into-OneLake + DirectLake path remains available as a deliberate
alternative for enterprise sources, but is not the default.
