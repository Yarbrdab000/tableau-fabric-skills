# Connection Binding (Tableau → M partition → Fabric)

How the skill points a rebuilt semantic model **directly at the Tableau datasource's original upstream
source** — by emitting Power Query M partitions and the structured details needed to bind a Fabric Data
Connection. The engine is `scripts/connection_to_m.py`. This is the orchestrator's **Phase 5**; the deploy
and the actual bind call are **delegated** to `semantic-model-authoring`.

> **Credentials are a manual security boundary.** The skill emits the connection *parameters* and the *bind
> request*, but it never reads or writes credentials. On a credential error during binding, stop and have
> the user configure the connection. On-premises sources additionally need a user-selected gateway.

---

## The descriptor (output of `parse_tds`)

`parse_tds(xml_text)` returns a JSON-serializable, **credential-free** descriptor:

| Key | Meaning |
|---|---|
| `datasource_name` | Display name from the `.tds` |
| `connection_class` | Tableau connector class (`sqlserver`, `snowflake`, `excel-direct`, …) |
| `server` / `database` | Upstream server + database (from the live connection) |
| `auth_method` | Non-secret authentication *label* from the inner connection (e.g. `Username Password`, `oauth`) — never the credential itself |
| `is_extract` | Whether a `.hyper` extract is enabled |
| `named_connection_count` | >1 ⇒ federated multi-connection (fallback) |
| `relations` | One entry per logical table: `kind`, `name`, `catalog`/`schema`/`item`, typed `columns`, and (federated only) the resolved `connection` facts that table routes through |
| `relationships` | Physical joins lifted from `<object-graph><relationships>`: `[{from_table, from_col, to_table, to_col}]`, using the **emitted model column names** |
| `relationship_warnings` | Joins that could not be resolved cleanly and were skipped (kept **out** of `unsupported_reasons`) |
| `connections` | Federated named-connection map `nc_id → {connection_class, server, database, warehouse, http_path, schema, auth_method}` (non-secret routing facts only) |
| `unsupported_reasons` | Shape problems found during parsing |

```python
from connection_to_m import parse_tds
descriptor = parse_tds(open("datasource.tds", encoding="utf-8-sig").read())
```

> Always open `.tds` with `encoding="utf-8-sig"` — Tableau writes a UTF-8 BOM.

### Federated collection datasources (Tableau 2023+ object model)

Modern cloud-warehouse `.tds` files (real Snowflake / Databricks / Azure SQL exports) wrap the upstream
in a `<connection class='federated'>` whose **named** inner connection carries the true class, and list
tables as a `<relation type='collection'>` of `<relation type='table' table='[catalog].[schema].[table]'/>`
children — physically once and again under the logical object-model layer. The parser:

- promotes those collection table relations to `kind='table'` (they are a flat star, not a join/union),
- parses the **three-part** `[catalog].[schema].[table]` name into `catalog` / `schema` / `item`,
- attaches each relation's typed `columns` (matched on the bare `<parent-name>`), and
- de-duplicates the physical + logical copies on the fully-qualified `(catalog, schema, item)` path.

The result is **N DirectQuery tables** (validated against real Snowflake and Databricks Superstore
exports — 3 tables each, columns intact) instead of the old "no table relations found" land-to-Delta
fallback. A three-part name whose **catalog ≠ the connection database** on a single-database `Sql.Database`
connector is treated as a cross-database reference and scaffolded (never silently bound to the wrong
database); a catalog equal to the database is just dropped as a redundant qualifier.

### Relationships (Tableau object-graph → model relationships)

Tableau 2023+ datasources carry their physical joins in `<object-graph><relationships>`, separate from
the table list. `parse_tds` lifts each single-column equality into `descriptor['relationships']` as
`{from_table, from_col, to_table, to_col}` so the rebuilt model can recreate the star 1:1. Resolution is
deliberately conservative:

- **Endpoints** (`first-end-point` / `second-end-point` object-ids) are resolved to a real **emitted table
  display name** via the `<objects><object id caption>` map; an endpoint that doesn't line up with a parsed
  table is skipped (never pointed at a phantom table).
- **Columns** are matched case-insensitively against each column's local / remote / model name (Power BI
  relationships are case-insensitive) and emitted as the column's **model name** — the identifier the table
  actually emits — so a renamed key like `[Region (People)]` resolves to `Region` and a spaced key like
  `[Order ID]` resolves to `Order_ID`, never a dangling reference.
- Both operand orientations are resolved; if they resolve to **different** column pairs (both keys exist on
  both tables) the join is **ambiguous** and skipped.
- A **composite** (multi-column `AND`), calculated, or non-`=` predicate is skipped rather than emitting only
  one arm of the join.

Anything skipped is recorded in `descriptor['relationship_warnings']` — kept **out** of `unsupported_reasons`,
so a single fuzzy join never demotes an otherwise-supported datasource to the land-to-Delta fallback.
Validated against the real Snowflake and Databricks Superstore exports (Orders↔People on Region, Orders↔Returns
on Order ID), which rebuild as two relationships with no warnings.

### Per-connection routing (federated multi-connection)

When a federated source has **more than one** named connection, each `<relation>` carries its own
`connection=` id. The parser builds `descriptor['connections']` (`nc_id → routing facts`) and attaches the
resolved connection to **each relation**, so `emit_m_partition_source` binds every table against **its own**
upstream connector function and navigation rather than a single global one. Single-connection output is
unchanged (the global descriptor is used), so existing emitted M is byte-identical. This is groundwork: a
multi-connection source is still routed to the land-to-Delta fallback by `select_storage_mode`, and the shared
`#"Server"`/`#"Database"`/`#"Warehouse"`/`#"HttpPath"` parameters are still emitted once per datasource, so the
per-relation routing is never the deployed artifact on its own.

---

## M partition emission

`emit_connection_parameters(descriptor)` emits shared `expression Server`/`expression Database` parameters
(marked `IsParameterQuery`) so the connection is rebindable without editing every table.

`emit_m_partition_source(relation, descriptor, mode)` builds the per-table M, choosing the shape from the
relation kind:

| Relation kind | M emitted |
|---|---|
| `table` | `Source = Connector(#"Server", #"Database")`, then schema/item navigation `Source{[Schema=…, Item=…]}[Data]` |
| `custom_sql` | `Value.NativeQuery(Source, "<sql>", null, [EnableFolding=true])` — the Tableau custom SQL is **preserved**, not re-expressed |

`emit_table_tmdl_m(relation, descriptor, mode)` wraps that into a full `table` block (typed columns + the
`= m` partition with `mode: import` or `mode: directQuery`).

### Connector mapping

| Tableau class | Power Query connector | Tier |
|---|---|---|
| `sqlserver` / `azure_sqldb` | `Sql.Database` | Fully supported (incl. Azure SQL Managed Instance, which arrives as `sqlserver`) |
| `azure_sql_dw` (Azure Synapse Analytics) | `Sql.Database` | Fully supported (TDS protocol — covers both dedicated and serverless SQL pool) |
| `postgres` | `PostgreSQL.Database` | Fully supported |
| `mysql` | `MySQL.Database` | Fully supported |
| `redshift` | `AmazonRedshift.Database` | Fully supported |
| `oracle` | `Oracle.Database` | Fully supported (server-only) |
| `snowflake` | `Snowflake.Databases` | Fully supported (server + warehouse) |
| `databricks` | `Databricks.Catalogs` | Fully supported (host + HTTP path) |
| `microsoft_fabric_sql_endpoint` | `Sql.Database` | Fully supported (TDS protocol) |
| `teradata` | `Teradata.Database` | Scaffold (documented signature, but no live navigator to confirm — held) |
| `bigquery` | `GoogleBigQuery.Database` | Scaffold (no M function reference page; identifiers unverified) |
| `msolap` / `sqlserver-analysis-services` | — | Analysis Services model (migrate directly — see below) |
| `excel-direct` / `excel` | `Excel.Workbook` | Flat file (needs path) |
| `textscan` / `csv` | `Csv.Document` | Flat file (needs path) |
| anything else | — | Fall back to land-to-Delta |

> **All Microsoft TDS-protocol sources bind through `Sql.Database`.** Azure SQL Database
> (`azure_sqldb`), Azure Synapse Analytics — dedicated SQL pool (`azure_sql_dw`), **Azure SQL
> Managed Instance**, and the **Microsoft Fabric** Warehouse / Lakehouse SQL endpoint
> (`microsoft_fabric_sql_endpoint`) all speak the SQL Server protocol. Managed Instance and the
> Synapse **serverless** SQL pool are reached through Tableau's ordinary **SQL Server** / **Azure
> SQL** connector, so they arrive as class `sqlserver` / `azure_sqldb` and are already covered with
> no extra mapping; dedicated Synapse and the dedicated Fabric endpoint have their own classes
> (`azure_sql_dw`, `microsoft_fabric_sql_endpoint`). The `azure_sql_dw` and
> `microsoft_fabric_sql_endpoint` class strings are web-verified, not primary-doc — a wrong class
> string only causes a safe fallback (never wrong M); the TDS→`Sql.Database` mapping is the verified
> fact.

Each **fully supported** connector is emitted as deploy-ready M from a verified fact, recorded in
the `DIRECT_CONNECTORS` registry as `(function, connect_style, nav_style)`:

| Connect style | First step | Navigation | Connectors |
|---|---|---|---|
| `server_database` | `Fn(#"Server", #"Database")` | `Source{[Schema=…, Item=…]}[Data]` | Sql (incl. Synapse + Fabric) / PostgreSQL / MySQL / AmazonRedshift |
| `server_only` | `Fn(#"Server", [HierarchicalNavigation=false])` | `Source{[Schema=…, Item=…]}[Data]` | Oracle |
| `server_warehouse` | `Snowflake.Databases(#"Server", #"Warehouse")` | `[Name=…, Kind="Database"]` → `[Name=…, Kind="Schema"]` → `[Name=…, Kind="Table"]` | Snowflake |
| `server_httppath` | `Databricks.Catalogs(#"Server", #"HttpPath")` | `[Name=…, Kind="Database"]` (catalog) → `[Name=…, Kind="Schema"]` → `[Name=…, Kind="Table"]` | Databricks |

Oracle is server-only because the database/service is carried in the server string (so
no unused `#"Database"` parameter is emitted), and `HierarchicalNavigation=false` is set explicitly so
the flat `Schema`/`Item` selector is correct rather than default-reliant. Snowflake adds a
`#"Warehouse"` parameter and reaches the table by `database → schema → table` navigation. Databricks
adds a `#"HttpPath"` parameter (the SQL-warehouse HTTP path, read from the `.tds` `v-http-path`
attribute) and uses the **same** `[Name, Kind]` navigation — the Unity Catalog catalog is the first hop,
keyed `Kind="Database"`. Snowflake and Databricks both scaffold a relation rather than guess when the
`.tds` doesn't carry a resolvable database/catalog + schema. When a real Snowflake `.tds` carries an
**empty warehouse** (`warehouse=''`), the `#"Warehouse"` parameter is still emitted — so
`Snowflake.Databases(#"Server", #"Warehouse")` stays a valid call — but it is prefixed with a `///` TMDL
description flagging that a compute warehouse must be set before refresh (a `///` description is
deploy-safe; a bare `//` comment is not guaranteed to parse), and `select_storage_mode` adds a matching
follow-up.

> **Verification status.** Oracle, Databricks, and the `(server, database)` family (incl.
> Synapse and Fabric via the SQL Server protocol) are doc-verified against the official Power Query M
> function/connector references — Oracle's `Fn(server, [options])` signature and
> `HierarchicalNavigation=false` flat `[Schema, Item]` behavior are confirmed by its M function
> reference page, and Databricks' `Databricks.Catalogs(host, httpPath, [options])` signature plus
> catalog/schema/table `[Name, Kind]` navigation come straight from the Microsoft connector doc.
> Snowflake's navigation is doc-informed (no M function reference page exists). **Teradata** has a
> documented `Teradata.Database(server, [options])` signature, but with no live Teradata navigator to
> confirm the emitted body actually binds it is held as a **flagged scaffold** (recognized + named,
> never a guessed call) until a real instance reconciles it. The `azure_sql_dw` and
> `microsoft_fabric_sql_endpoint` class strings are web-verified (a wrong class string only causes a
> safe fallback; the TDS→`Sql.Database` mapping is the verified fact). The `Sql.Database` family is
> **live-verified end-to-end** against a real **Azure SQL Database** (the Superstore validation
> datasource), and every Microsoft TDS-protocol variant (SQL Server, Azure SQL DB, Managed Instance,
> Synapse, Fabric SQL endpoint) binds through that same shared shape — so all are deploy-ready; a data
> gateway, where an on-prem source needs one, is a networking step, not a conversion gap. **Snowflake**
> and **Databricks** have each likewise been reconciled against a **live instance** and resolve
> end-to-end. **Oracle** is the one tier with no live instance available, so its live reconciliation
> is still **pending** (the emitted M is doc-verified). For Databricks the `#"HttpPath"` value and
> catalog name are not stored portably in the `.tds` and are surfaced as a manual follow-up.

**Scaffold** connectors `bigquery` and `teradata` map to the right M function *name*
(`GoogleBigQuery.Database`, `Teradata.Database`) but are emitted as a clearly-flagged `// TODO` that
names the intended connector and never a guessed call. BigQuery has **no M function reference page**, so
neither its project/dataset/table navigation selectors nor its billing-project vs project identifier
mapping (it has no server) can be verified from an official source. Teradata's signature *is* documented,
but it has **no live navigator** in the validation environment to confirm the emitted body binds, so it
is held at the scaffold tier rather than shipped as deploy-ready M we have never resolved. Both are
deferred for promotion until a primary-doc shape or a real datasource confirms the navigation.

### Microsoft Analysis Services (SSAS / MSOLAP) — separate handling

`msolap` and `sqlserver-analysis-services` are **not** relational datasources to rebuild. The source
is already a tabular/multidimensional **semantic model**, so emitting an M partition for it would be
wrong. `emit_m_partition_source` returns a clearly-flagged scaffold and `select_storage_mode` routes
it to a dedicated `analysis-services-model-migration` label (not the relational land-to-Delta path),
with the recommendation to migrate the model directly via its **XMLA endpoint / semantic-model import**.

---

## Binding the Fabric Data Connection

`connection_details_for_bind(descriptor)` returns structured details for the Bind Semantic Model Connection
API:

```python
{
  "connector": "sqlserver",
  "bind_type": "SQL",            # Power BI data-source type
  "server":   "myserver.database.windows.net",
  "database": "Superstore",
  "path":     "myserver.database.windows.net;Superstore",
  "auth_method":     "Username Password",  # non-secret label from the .tds
  "credential_kind": "Basic",              # advised Fabric credential type
}
```

`bind_type` is mapped for the SQL family (including Azure Synapse `azure_sql_dw` and the Fabric SQL
endpoint `microsoft_fabric_sql_endpoint`) plus Oracle, Teradata, Snowflake, Databricks, and BigQuery
(`SQL`, `PostgreSql`, `Oracle`, `MySql`, `AmazonRedshift`, `Teradata`, `Snowflake`, `Databricks`,
`GoogleBigQuery`). A binding adapter flattens `path` to the connector's exact requirement; the
structured fields are preserved so nothing is lost for non-SQL connectors.

### Authentication method → Fabric credential type

The descriptor surfaces the inner connection's `authentication` attribute as a **non-secret label**
(`auth_method`), and `connection_details_for_bind` maps it to an advised Fabric `credential_kind`:

| `.tds` `auth_method` label | Advised Fabric credential | Typical source |
|---|---|---|
| `Username Password` | `Basic` | Snowflake (user/password) |
| `oauth` | `OAuth2` | Databricks (Entra), Entra-based sources |
| anything else / absent | `null` (configure manually) | — |

> **Strict secret boundary.** Only the auth *label* is read. The skill never reads or emits username,
> password, token, OAuth config id, or instance URL — verified against the real Snowflake and Databricks
> `.tds` exports (the secret fields are present in the file but absent from the descriptor and bind
> details). The advised `credential_kind` is guidance for configuring the Fabric connection; the user
> still supplies the actual credential.

The bind sequence itself — discover → match → create → bind → validate — is owned by `semantic-model-authoring`'s
connection workflow. Hand it `connection_details_for_bind(...)` and let it drive the Fabric REST calls.

---

## Custom SQL and folding

When a relation is `custom_sql`, the native query is kept verbatim with `[EnableFolding=true]` so it folds
to the source. **Review folding before refresh** — a query that does not fold will materialize in memory.
This is the one place a human should sanity-check the emitted M.

---

## When binding is not possible

If `select_storage_mode` returned `None` (join/union tree, multi-connection, unmapped connector, or no
column metadata), there is no direct upstream to bind — route the datasource to the land-to-Delta + DirectLake
fallback instead. See [storage-mode-selection.md](storage-mode-selection.md).
