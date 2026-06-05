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
| `is_extract` | Whether a `.hyper` extract is enabled |
| `named_connection_count` | >1 ⇒ federated multi-connection (fallback) |
| `relations` | One entry per logical table: `kind`, `name`, `schema`/`item`, typed `columns` |
| `unsupported_reasons` | Shape problems found during parsing |

```python
from connection_to_m import parse_tds
descriptor = parse_tds(open("datasource.tds", encoding="utf-8-sig").read())
```

> Always open `.tds` with `encoding="utf-8-sig"` — Tableau writes a UTF-8 BOM.

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
| `teradata` | `Teradata.Database` | Fully supported (server-only) |
| `snowflake` | `Snowflake.Databases` | Fully supported (server + warehouse) |
| `databricks` | `Databricks.Catalogs` | Fully supported (host + HTTP path) |
| `microsoft_fabric_sql_endpoint` | `Sql.Database` | Fully supported (TDS protocol) |
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
| `server_only` | `Fn(#"Server", [HierarchicalNavigation=false])` | `Source{[Schema=…, Item=…]}[Data]` | Oracle, Teradata |
| `server_warehouse` | `Snowflake.Databases(#"Server", #"Warehouse")` | `[Name=…, Kind="Database"]` → `[Name=…, Kind="Schema"]` → `[Name=…, Kind="Table"]` | Snowflake |
| `server_httppath` | `Databricks.Catalogs(#"Server", #"HttpPath")` | `[Name=…, Kind="Database"]` (catalog) → `[Name=…, Kind="Schema"]` → `[Name=…, Kind="Table"]` | Databricks |

Oracle and Teradata are server-only because the database/service is carried in the server string (so
no unused `#"Database"` parameter is emitted), and `HierarchicalNavigation=false` is set explicitly so
the flat `Schema`/`Item` selector is correct rather than default-reliant. Snowflake adds a
`#"Warehouse"` parameter and reaches the table by `database → schema → table` navigation. Databricks
adds a `#"HttpPath"` parameter (the SQL-warehouse HTTP path) and uses the **same** `[Name, Kind]`
navigation — the Unity Catalog catalog is the first hop, keyed `Kind="Database"`. Snowflake and
Databricks both scaffold a relation rather than guess when the `.tds` doesn't carry a resolvable
database/catalog + schema.

> **Verification status.** Oracle, Teradata, Databricks, and the `(server, database)` family (incl.
> Synapse and Fabric via the SQL Server protocol) are doc-verified against the official Power Query M
> function/connector references — Oracle's and Teradata's `Fn(server, [options])` signatures and shared
> `HierarchicalNavigation=false` flat `[Schema, Item]` behavior are confirmed by their M function
> reference pages, and Databricks' `Databricks.Catalogs(host, httpPath, [options])` signature plus
> catalog/schema/table `[Name, Kind]` navigation come straight from the Microsoft connector doc.
> Snowflake's navigation is doc-informed (no M function reference page exists). The `azure_sql_dw` and
> `microsoft_fabric_sql_endpoint` class strings are web-verified (a wrong class string only causes a
> safe fallback; the TDS→`Sql.Database` mapping is the verified fact). Oracle, Teradata, Snowflake, and
> Databricks have **no live instance** in the validation environment (Azure SQL only) — **live
> reconciliation pending**; for Databricks the `#"HttpPath"` value and catalog name are not stored
> portably in the `.tds` and are surfaced as a manual follow-up.

**Scaffold** connector `bigquery` maps to the right M function *name* (`GoogleBigQuery.Database`), but
the BigQuery connector has **no M function reference page**, so neither its project/dataset/table
navigation selectors nor its billing-project vs project identifier mapping (it has no server) can be
verified from an official source — the partition is emitted as a clearly-flagged `// TODO` that names
the intended connector and never a guessed call. It is deferred for promotion until a primary-doc shape
or a real BigQuery datasource confirms the navigation.

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
}
```

`bind_type` is mapped for the SQL family (including Azure Synapse `azure_sql_dw` and the Fabric SQL
endpoint `microsoft_fabric_sql_endpoint`) plus Oracle, Teradata, Snowflake, Databricks, and BigQuery
(`SQL`, `PostgreSql`, `Oracle`, `MySql`, `AmazonRedshift`, `Teradata`, `Snowflake`, `Databricks`,
`GoogleBigQuery`). A binding adapter flattens `path` to the connector's exact requirement; the
structured fields are preserved so nothing is lost for non-SQL connectors.

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
fallback (bridge Play 2/3/4) instead. See [storage-mode-selection.md](storage-mode-selection.md).
