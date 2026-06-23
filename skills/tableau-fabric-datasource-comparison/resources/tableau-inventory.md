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
- **upstreamTables** — `connectionType`, `database { name }`, `schema`, `name`, `fullName` → the physical
  source. When `database`/`schema` come back empty but `fullName` is populated (common for cloud
  connectors), they are recovered by parsing `fullName` (`[db].[schema].[table]` or dotted), so the
  strict `(connector, database, table)` source tier fires instead of dropping to the looser table-only
  signal.

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

## Usage telemetry & connected assets (best-effort enrichment)

When usage gathering runs (`--usage {auto,metadata,rest}`), each record also picks up a `usage` block
that goes beyond the downstream counts used for migration priority. This powers the
[artifact-importance](comparison-methodology.md#artifact-importance--what-to-protect-first) signal and
the deliverable's "what depends on this datasource" view. It is gathered from two seams, **each in its
own best-effort try/except** so a rejected field or a slow endpoint only loses the enrichment — the
counts and the comparison itself are never affected:

- a **separate** Metadata-API query (`datasource_details_metadata`, kept distinct from the proven
  downstream-count query) for `certified` (`isCertified`), `has_quality_warning` (`hasActiveWarning`),
  the extract refresh timestamps (`extractLastRefreshTime` / `extractLastUpdateTime`), and the **names**
  of the dependent workbooks / dashboards (bounded to `CONNECTED_ASSET_CAP` each);
- REST **view usage statistics** (`view_counts_rest`, `/views?includeUsageStatistics=true`) summed
  per workbook, then rolled up to a datasource by summing across its dependent workbooks → `view_count`.

The REST datasource listing additionally supplies `updated_at` and a `certified` fallback. Every one of
these keys is `null` when its source was unavailable, so the enrichment degrades gracefully on older
servers or tenants without Catalog. The full `usage` shape:

```json
{
  "workbook_count": 12, "sheet_count": 30, "dashboard_count": 4, "source": "metadata",
  "view_count": 1820, "certified": true, "has_quality_warning": false,
  "extract_last_refresh": "2026-06-20T03:00:00Z", "extract_last_update": "2026-06-19T03:00:00Z",
  "updated_at": "2026-06-01T00:00:00Z",
  "connected_assets": {"workbooks": [{"name": "Exec KPIs", "luid": "..."}], "dashboards": [{"name": "Daily Sales"}]}
}
```

## Safety / cost

- The client **always signs out** in a `finally` block.
- The `.tds` download is per-datasource and skips the extract, so it stays cheap; it only fires when the
  Metadata API yields nothing.
- **Never commit** a downloaded `.tds`/`.tdsx` or a PAT. Run `tableau_inventory.py --dry-run` to preview
  the calls without touching the network.
