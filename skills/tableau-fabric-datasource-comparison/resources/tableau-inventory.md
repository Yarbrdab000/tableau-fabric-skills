# Tableau inventory

How `tableau_inventory.py` enumerates a site's published datasources and pulls each one's schema and
underlying source. **Read-only** — the client always signs out.

## Auth

- **PAT (default):** `TABLEAU_PAT_NAME` + `TABLEAU_PAT_VALUE`, plus `TABLEAU_SERVER` and `TABLEAU_SITE`
  (the site **content URL** slug; `""` for the Default site).
- **Connected App (Direct Trust) JWT** (`--auth jwt`): `TABLEAU_CONNECTED_APP_CLIENT_ID`,
  `_SECRET_ID`, `_SECRET_VALUE`, and `TABLEAU_JWT_USERNAME` (or `--jwt-username`). The HS256 JWT is
  built with the standard library — no PyJWT dependency. Use this when you need to act as a Site Admin.

Works against **Tableau Cloud and Tableau Server** because it calls the REST + Metadata APIs directly
(no `tableauserverclient`).

## Two data paths

### 1. Metadata API (preferred)

For each datasource the GraphQL Metadata API returns:

- **fields** — `name`, `dataType`, `role`, `isHidden` (hidden fields are dropped by default), paged via
  `fieldsConnection(first:, after:)` (the `first:` argument is **required** — the API 400s without it).
- **upstreamTables** — `connectionType`, `database { name }`, `schema`, `name` → the physical source.

### 2. `.tds` fallback (Catalog-independent)

Tableau Catalog only indexes some datasources. On Tableau Cloud, cloud-connected datasources (Azure SQL,
Snowflake, Databricks, …) frequently return an **empty** `publishedDatasources` from the Metadata API.
When a datasource comes back with **no fields**, the inventory falls back to:

```
GET /api/{ver}/sites/{siteId}/datasources/{luid}/content?includeExtract=False
```

`includeExtract=False` asks Tableau to omit the (potentially huge) `.hyper` extract and return only the
XML descriptor. The response is either a bare `.tds` or a `.tdsx` ZIP containing a `federated.*.tds`;
`parse_tds` reads it with tolerant, namespace-agnostic regex:

- **connector + database** — from the non-`federated` child `<connection class='…' dbname='…' server='…'>`
  inside each `<named-connection>`.
- **tables** — from `<relation type='table' table='[dbo].[Orders]'>` (schema + table split from the
  bracketed name).
- **custom SQL** — from `<relation type='text'>SELECT … FROM …</relation>`: the embedded SQL's
  `FROM` / `JOIN` tables are mined (schema-qualified, quoting/brackets stripped, de-duplicated) so a
  custom-SQL datasource yields a real physical-source signal instead of an empty one. Each extracted
  table inherits the relation's connector + database.
- **columns + types** — from `<metadata-record class='column'>` using `<remote-name>` (the **source**
  column name, so it lines up with Fabric columns that mirror the source) and `<local-type>`
  (upper-cased to match the Metadata API's casing).

Control it with `--tds-fallback {auto,never}` (default `auto`). The fallback never raises — a failed
download just leaves that datasource with whatever the Metadata API returned.

## Output shape

```json
{
  "name": "Azure SQL - Superstore",
  "project": "default",
  "luid": "....",
  "fields": [{"name": "Sales", "dataType": "REAL", "role": "MEASURE"}],
  "sources": [{"connectionType": "azure_sqldb", "database": "SalesDW", "schema": "dbo", "table": "Orders"}]
}
```

`azure_sqldb` and other connection-class names are folded to canonical connectors (`sqlserver`, …) on
the comparison side, so they line up with the Fabric M connectors.

## Safety / cost

- The client **always signs out** in a `finally` block.
- The `.tds` download is per-datasource and skips the extract, so it stays cheap; it only fires when the
  Metadata API yields nothing.
- **Never commit** a downloaded `.tds`/`.tdsx` or a PAT. Run `tableau_inventory.py --dry-run` to preview
  the calls without touching the network.
