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
| `sqlserver` / `azure_sqldb` | `Sql.Database` | Fully supported |
| `postgres` | `PostgreSQL.Database` | Fully supported |
| `mysql` | `MySQL.Database` | Fully supported |
| `redshift` | `AmazonRedshift.Database` | Fully supported |
| `oracle` | `Oracle.Database` | Fully supported (server-only) |
| `snowflake` | `Snowflake.Databases` | Fully supported (server + warehouse) |
| `teradata` | `Teradata.Database` | Scaffold (navigation selector unverified) |
| `bigquery` | `GoogleBigQuery.Database` | Scaffold (no server; identifier mapping unverified) |
| `excel-direct` / `excel` | `Excel.Workbook` | Flat file (needs path) |
| `textscan` / `csv` | `Csv.Document` | Flat file (needs path) |
| anything else | — | Fall back to land-to-Delta |

Each **fully supported** connector is emitted as deploy-ready M from a verified fact, recorded in
the `DIRECT_CONNECTORS` registry as `(function, connect_style, nav_style)`:

| Connect style | First step | Navigation | Connectors |
|---|---|---|---|
| `server_database` | `Fn(#"Server", #"Database")` | `Source{[Schema=…, Item=…]}[Data]` | Sql / PostgreSQL / MySQL / AmazonRedshift |
| `server_only` | `Oracle.Database(#"Server", [HierarchicalNavigation=false])` | `Source{[Schema=…, Item=…]}[Data]` | Oracle |
| `server_warehouse` | `Snowflake.Databases(#"Server", #"Warehouse")` | `[Name=…, Kind="Database"]` → `[Name=…, Kind="Schema"]` → `[Name=…, Kind="Table"]` | Snowflake |

Oracle is server-only because its service/SID is carried in the server string (so no unused
`#"Database"` parameter is emitted), and `HierarchicalNavigation=false` is set explicitly so the
flat `Schema`/`Item` selector is correct rather than default-reliant. Snowflake adds a `#"Warehouse"`
parameter and reaches the table by `database → schema → table` navigation; if the `.tds` doesn't
carry a resolvable database it falls back to a scaffold rather than guess the first hop.

> **Verification status.** Oracle and the `(server, database)` family are doc-verified against the
> official Power Query M function reference. Snowflake's navigation is doc-informed (the connector
> doc confirms Server + Warehouse and the database/schema/table hierarchy, but Snowflake has no M
> function reference page) — **live reconciliation pending** (no live Snowflake/Oracle instance in
> the validation environment, which has Azure SQL only).

**Scaffold** connectors (`teradata`, `bigquery`) map to the right M function *name*, but a fact we
need is not verifiable offline — Teradata's exact navigation selector, and BigQuery's billing-project
vs project identifier mapping (it has no server) — so the partition is emitted as a clearly-flagged
`// TODO` that names the intended connector and never a guessed call. They are deferred for promotion
once a real datasource confirms the shape.

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

`bind_type` is mapped for the SQL family plus Oracle, Teradata, Snowflake, and BigQuery (`SQL`, `PostgreSql`,
`Oracle`, `MySql`, `AmazonRedshift`, `Teradata`, `Snowflake`, `GoogleBigQuery`). A binding adapter flattens
`path` to the connector's exact requirement; the structured fields are preserved so nothing is lost for
non-SQL connectors.

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
