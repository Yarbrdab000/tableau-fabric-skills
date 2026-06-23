# Fabric introspection

How `fabric_inventory.py` turns a Fabric tenant into the inventory shape `compare.py` consumes. All
network calls are **read-only**; the TMDL/M parsing helpers are pure and unit-tested without a tenant.

## Auth

A bearer token for the Fabric REST API audience `https://api.fabric.microsoft.com`:

- `--token <jwt>` or the `FABRIC_TOKEN` environment variable, or
- `--use-az`, which shells out to `az account get-access-token --resource https://api.fabric.microsoft.com`.

The token is sent only as an `Authorization: Bearer` header and is never logged.

## REST calls (all GET/POST reads)

| Step | Endpoint |
|---|---|
| List workspaces | `GET /v1/workspaces` (paged via `continuationToken`) |
| List semantic models | `GET /v1/workspaces/{workspaceId}/semanticModels` |
| Get model definition | `POST /v1/workspaces/{workspaceId}/semanticModels/{modelId}/getDefinition?format=TMDL` |

`--workspaces` filters to a comma-separated list of workspace names or ids; `--max-models` caps the
number of models scanned (useful for a quick estate sample). 429s are retried with backoff.

### getDefinition is a long-running operation

`getDefinition` may answer **200** (definition inline) or **202** (a long-running operation). On 202 we
poll the `Location` operation URL until it succeeds, then fetch the result. The definition is returned as
a set of **base64-encoded parts** (TMDL files plus a `definition.pbism`); we decode the parts and parse
the `*.tmdl` payloads.

## TMDL → tables, columns, types

Each `table` block in the decoded TMDL is parsed for its name and its `column` declarations, capturing
the TMDL `dataType` (`int64`, `double`, `string`, `dateTime`, `boolean`, …). The model inventory keeps:

- `tables`  — the de-duplicated list of table names (used by the comparison's table-name source tier),
- `columns` — `[{table, name, dataType}]` for the column-overlap and type signals.

## M (Power Query) → physical source

A table partition's `source` is an M expression. `parse_m_sources` recognises the common database
connector functions and pulls `(connector, server, database, schema, table)` from each:

| M function | Canonical connector |
|---|---|
| `Sql.Database` / `Sql.Databases` | `sqlserver` |
| `Snowflake.Databases` | `snowflake` |
| `PostgreSQL.Database` | `postgres` |
| `AmazonRedshift.Database` | `redshift` |
| `GoogleBigQuery.Database` | `bigquery` |
| `Databricks.Catalogs` / `Databricks.Query` | `databricks` |
| `Oracle.Database`, `MySQL.Database`, … | `oracle`, `mysql`, … |
| `Lakehouse.Contents` | `lakehouse` |
| `Fabric.Warehouse` / `DataWarehouse.Contents` | `warehouse` |
| `PowerPlatform.Dataflows` / `Dataflows.Contents` | `dataflow` |
| `Excel.Workbook` | `excel` |
| `Csv.Document` | `csv` |

Connector names are folded with the **same** `canonical_connector()` used on the Tableau side (imported
locally from `compare.py`), so the two clouds' source keys line up. Table names also come from
`Item="…"` / `Name="…"` navigation steps and from the schema/table pair in `{[Schema="dbo"],[Item="Orders"]}`
record access.

### Fabric-native and native-query shapes

Beyond classic database connectors, `parse_m_sources` also resolves the table from the Fabric-native and
file navigation idioms, and from native SQL:

- **Lakehouse / Warehouse** — `{[Id="Orders", ItemKind="Table"]}` navigation yields `table = Orders`
  (the `[workspaceId=…]` / `[lakehouseId=…]` hops above it are ignored). A Warehouse that exposes
  `{[Schema="dbo",Item="Customers"]}` resolves schema + table directly. This matters for the central
  lakehouse-intermediary case: a Lakehouse-backed model now contributes a connector **and** a real
  table name, not just its TMDL table names.
- **Dataflow** — `{[entity="SalesFact"]}` yields `table = SalesFact`.
- **Excel** — `{[Item="Sheet1", Kind="Sheet"]}` yields the sheet/table name.
- **Native SQL** — `Value.NativeQuery(Sql.Database(…), "select … from dbo.FactSales join dim.Customer …")`
  has its `FROM` / `JOIN` tables mined (schema-qualified, quoting/brackets stripped) so a native-query
  partition still resolves concrete tables.

### When the source is obscured

Composite / DirectQuery models (over an AnalysisServices or Power BI dataset, or a dataflow) and some
Databricks/Snowflake expressions don't resolve to a concrete table — `parse_m_sources` yields a source
with an empty `table`. That's expected: the comparison's obscured-source fallback and the model's own
`tables` list keep such a model matchable (see `comparison-methodology.md`).

## Output shape

```json
{
  "name": "Azure SQL - Superstore",
  "workspace": "Github-Testing-Workspace",
  "workspaceId": "....",
  "id": "....",
  "tables": ["Orders", "People", "Returns"],
  "columns": [{"table": "Orders", "name": "Sales", "dataType": "double"}],
  "sources": [{"connectionType": "sqlserver", "server": "", "database": "", "schema": "dbo", "table": "Orders"}]
}
```

Run `fabric_inventory.py --dry-run` to print the exact calls without touching the network.
