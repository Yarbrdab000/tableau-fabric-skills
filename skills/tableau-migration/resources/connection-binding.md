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
| `oracle` | `Oracle.Database` | Scaffold (server-only signature) |
| `teradata` | `Teradata.Database` | Scaffold (server-only signature) |
| `snowflake` | `Snowflake.Databases` | Scaffold (server + warehouse navigation) |
| `bigquery` | `GoogleBigQuery.Database` | Scaffold (project/dataset navigation) |
| `excel-direct` / `excel` | `Excel.Workbook` | Flat file (needs path) |
| `textscan` / `csv` | `Csv.Document` | Flat file (needs path) |
| anything else | — | Fall back to land-to-Delta |

**Fully supported** is gated on one verified fact from the Power Query M docs: the connector takes the
`<Connector>.Database(server, database)` signature, so the two-argument call + `Source{[Schema=…, Item=…]}[Data]`
navigation is correct rather than guessed. **Scaffold** connectors map to the right M function *name*, but
their real signature differs — `Oracle.Database(server, [options])` and `Teradata.Database(server, [options])`
take a server only (no `database` positional), and `Snowflake.Databases` / `GoogleBigQuery.Database` use
multi-level navigation — so the partition is emitted as a clearly-flagged `// TODO` that names the intended
connector but never a wrong `(server, database)` call.

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
