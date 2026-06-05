# Workbook-Embedded Datasource Rebuild

How the skill rebuilds the `<datasource>` blocks a Tableau **workbook** carries *inline* — the common case
for real `.twb`/`.twbx` files, including the canonical *Superstore* sample. The spine's published path only
rebuilds standalone `.tds`/`.tdsx`; `twb_to_pbir.py` reads a workbook's embedded datasources for **binding
names only** and never rebuilds them into models. Without a model behind each embedded datasource, the
generated PBIR report's field bindings dangle and the `.pbip` won't open in Power BI Desktop. This is the
gap `scripts/workbook_sources.py` closes.

> **Every embedded datasource becomes its own `<caption>.SemanticModel`.** One model per real datasource,
> keyed by a sanitized, de-duplicated caption folder, so the estate orchestrator can drop the bundle next to
> the report and the report's existing bindings resolve. The module **imports** the spine's public APIs and
> never modifies them: flat-file M emission that the connector does not expose is implemented *inside*
> `workbook_sources.py`.

---

## Enumeration

`enumerate_workbook_datasources(source)` accepts a path to a `.twb` / `.twbx` / `.tds` / `.tdsx` file **or**
raw XML text. For a packaged `.twbx`/`.tdsx` (a zip) it reads the inner `.twb`/`.tds` and keeps the package
path so packaged data files can be inspected later. It returns one entry per **real** datasource — the
`Parameters` pseudo-datasource and any `hasconnection='false'` block are skipped.

Each entry is JSON-serializable and captures:

| Key | Meaning |
|---|---|
| `name` / `caption` | Internal datasource name and its display caption |
| `connection_class` | The **inner** `<connection class=...>` (drives classification) |
| `classification` | `flat_file` / `published_reference` / `relational` / `spatial` / `cross_db_join` |
| `file_type` | `Excel` / `CSV` / `Spatial` / `None` |
| `relations` | One per source table: `{kind, name, item, columns:[{remote_name, model_name, tmdl_type}]}` |
| `named_connections` | The distinct set of named-connections the relations span (cross-DB-join signal) |
| `cross_db_join` | `True` when the relations span >1 connection (not single-partition rebuildable) |
| `published` | `{referenced_name, server, site, ...}` for a published reference, else `None` |
| `flat_file` | `{filename, directory, separator, charset}` for a flat file, else `None` |
| `datasource_xml` | The sliced `<datasource>` element as text (fed back into the spine) |
| `package_path` | The `.twbx`/`.tdsx` path, when the source was packaged |

The schema (relations + typed columns) is read from the XML's `<relation>` and
`<metadata-record class='column'>` records, so it is present even for a plain `.twb` with no packaged data.
Relations and columns are parsed by slicing the `<datasource>` element to XML and running the spine's
`parse_tds` — the same path the standalone rebuild uses — so column typing stays identical to
[semantic-model-rebuild.md](semantic-model-rebuild.md).

---

## Classification

Classification keys off the **inner** connection class (for a `federated` datasource that is the class of the
named connection underneath it, not `federated` itself):

| Inner class | Classification | Rebuild path |
|---|---|---|
| `sqlproxy` | **published reference** | resolution seam (below); not embedded data |
| `excel-direct`, `excel` | **flat file** (Excel) | real Import M, built here |
| `textscan`, `csv` | **flat file** (CSV) | real Import M, built here |
| `ogrdirect`, spatial | **spatial** | `fallback` (deferred) |
| `federated` → relational (`sqlserver`, `snowflake`, `postgres`, …) | **relational** | existing spine |
| relation tree spans >1 `<named-connection>` | **cross-database join** | logical → rebuilt (per-source land + relationships); physical `<clause>` join → `fallback` (deferred) |

A single datasource's connection span is detected **before** the per-class rebuild dispatch: when its
`<relation>` tree references more than one named-connection, the `cross_db_join` classification takes
precedence over whatever its first inner connection class would suggest. The enumeration entry exposes
`named_connections` (the distinct set the relations span) and a `cross_db_join` flag so the orchestrator sees
it up front.

### Published references (`sqlproxy`)

A `sqlproxy` datasource is a pointer to a **published** datasource on a Tableau server (it carries
`server-ds-friendly-name`, server, and site) — it does **not** embed data. By default it is marked
`published_unresolved` with a follow-up naming the referenced datasource.

`rebuild_workbook_models` exposes an optional resolution seam, `resolve_published(referenced_name) -> tds_text
| None`. When a caller supplies one (backed by a local `.tds` cache directory or a live Tableau fetch) and it
returns a `.tds`, that definition is **reclassified and rebuilt** through the normal path — so a published
flat file still gets real M, and a published relational source still goes through the spine. **No network is
performed by this module**; the seam is the only resolution mechanism, which keeps tests fully offline.

### Flat files (Excel / CSV)

The connector's flat-file branch emits only a `null` partition scaffold, so real, deploy-shaped M is built
**inside** `workbook_sources.py` (the connector is never modified):

- **Excel** → `Excel.Workbook(File.Contents(#"FilePath"), null, true)`, then navigate the sheet/table
  (`Source{[Item="<sheet>", Kind="Sheet"]}[Data]`), `Table.PromoteHeaders`, rename remote → cleaned columns,
  then `Table.TransformColumnTypes`.
- **CSV** → `Csv.Document(File.Contents(#"FilePath"), [Delimiter=…, Encoding=…, Columns=…])`, then
  `Table.PromoteHeaders`, rename, then types.

Every flat-file model emits a `FilePath` expression parameter defaulting to the `directory/filename` from the
XML, plus a follow-up telling the user to repoint it at the real file or a OneLake/Lakehouse path. Columns and
Tableau→TMDL types come from the workbook metadata; the renamed model column equals `clean_col(remote_name)`
so it matches the emitted `sourceColumn` and the report binds (see
[connection-binding.md](connection-binding.md)). When the source is a `.twbx`, the packaged file header is
read to confirm the column count and **auto-detect the CSV delimiter**.

### Relational (`federated`)

A relational embedded datasource (Snowflake, SQL Server, Postgres, …) is sliced to XML and rebuilt through the
existing spine unchanged: `parse_tds` → `select_storage_mode` → `assemble_import_model`. Storage-mode policy
and DirectQuery/Import/land-to-Delta selection are exactly as documented in
[storage-mode-selection.md](storage-mode-selection.md).

### Cross-database joins (logical rebuilt; physical join deferred)

A single Tableau `<datasource>` can blend **more than one `<named-connection>`** — each joined leaf relation
carrying a different `connection=` id pointing at a different inner connection class (e.g. Azure SQL ⋈
Snowflake ⋈ Databricks). Detection is a cheap, namespace-agnostic XML walk (`_connection_span`): per datasource
it collects the distinct named-connections the relations reference (unioned with the declared
`<named-connection>` ids); a span greater than one classifies the datasource `cross_db_join` **before** any
per-class rebuild. The enumeration entry exposes `named_connections` and a `cross_db_join` flag up front.

A `cross_db_join` datasource then splits on **how** the blend is expressed:

- **Modern logical / noli model → REBUILT.** When the datasource is a `<relation type='collection'>` of
  **independent** tables (no physical `<clause>` join) with its join keys in a top-level `<relationships>`
  block, each table is independent in the logical layer — the clean Fabric shape. `build_cross_db_model`
  slices out **each side** to a single-connection `<datasource>`, runs it through the shared spine
  (`parse_tds` → `select_storage_mode` → `emit_table_tmdl_m` / `emit_connection_parameters`), and lands it as
  its **own** table via its **own** per-connector M (`Sql.Database` / `Snowflake.Databases` /
  `Databricks.Catalogs`). The four global connection parameters (`Server` / `Database` / `Warehouse` /
  `HttpPath`) are **renamed per side** (e.g. `#"Server_Orders"`, `#"Warehouse_RETURNS"`) so the combined model
  never collides them. The join keys then become **model relationships**: each `<relationship>`'s
  `<expression op='='>` operand pair is resolved by **relation + field, case-insensitively**, so a cross-DB
  case mismatch (`Order_ID` vs `ORDER_ID`) and a Tableau-disambiguated key (`Region` vs the local name
  `Region (people)`) both bind correctly. **No single federated cross-database query is attempted** — the sides
  stay independent (a per-source / composite model). Status is `migrated_with_followups` (credentials, gateway,
  and — when a Snowflake `warehouse=''` — a manual-warehouse prompt remain; the empty warehouse never fails the
  rebuild). A key that cannot be bound to exactly one column per side is **reported** as an unresolved-key
  follow-up rather than emitted wrong, and a partially-landed datasource lists its skipped sides.
- **Physical `<clause>` join / union → deferred `fallback`.** When the relations are collapsed into ONE logical
  table by a physical `join`/`union` tree (`_is_physical_join`), there is a real cross-source join to replicate,
  which cannot be a single Import partition or independent tables. It is routed to `fallback` with a
  `report.reason == "cross_db_join"` (plus per-side `connection_classes`) and a follow-up to land each side to
  Delta and join in the lakehouse, or use a Power BI composite model. That rebuild is **deferred**.

---

## Statuses

Per-datasource status mirrors the estate model, with one documented marker for the published case:

| Status | Meaning |
|---|---|
| `migrated` | Rebuilt and fully supported (e.g. a clean relational DirectQuery/Import) |
| `migrated_with_followups` | Rebuilt but needs manual action — flat-file `FilePath` repointing, storage caveats, or a **logical cross-database** model (per-side credentials / gateway / warehouse + relationship-cardinality review) |
| `published_unresolved` | A `sqlproxy` reference with no resolver result; the referenced datasource is a follow-up |
| `fallback` | Not safely rebuildable — spatial, a **physical** cross-database `<clause>` join, structurally unsupported shape, or a clean-name column collision |
| `error` | An exception was caught for this datasource |

Each datasource is rebuilt inside its own `try/except`, so one bad source (including a resolver that raises)
never aborts the rest of the workbook.

---

## Public seam

```python
from workbook_sources import enumerate_workbook_datasources, rebuild_workbook_models

# 1. Inspect what a workbook embeds (skips Parameters / hasconnection='false').
entries = enumerate_workbook_datasources("Superstore.twbx")

# 2. Rebuild every embedded datasource into a semantic-model definition.
result = rebuild_workbook_models(
    "Superstore.twbx",
    resolve_published=my_cache_lookup,   # optional: (name) -> tds_text | None
    output_dir="out",                    # optional: also write <caption>.SemanticModel folders
)
```

`rebuild_workbook_models` returns:

```python
{
  "models": {                                   # keyed by sanitized, de-duped caption folder
    "<caption>": {"parts", "report", "status", "classification",
                  "connection_class", "folder", "followups"},
  },
  "followups": [{"datasource", "message"}],     # flat, across all datasources
  "datasources": [ <enumeration entry minus datasource_xml> ],
}
```

`parts` is a deploy-ready set of TMDL parts (plus `.platform` and `definition.pbism`) in the same shape the
spine produces, so the estate orchestrator can hand it to `write_model_folder` / the Fabric payload helpers
just like a standalone rebuild. When `output_dir` is given, each model with parts is also written to
`<output_dir>/<caption>.SemanticModel`.

### CLI

```text
py scripts/workbook_sources.py <workbook> [-o OUT]
```

Prints a JSON **manifest** (folder, classification, connection class, status, table names, column/follow-up
counts, and follow-up messages) and, with `-o`, writes the `.SemanticModel` folders. The manifest is summary
only — no model `parts`, no raw XML, no credentials, secrets, paths, or GUIDs — so it is safe to log.

---

## Deferred scope

- **Cross-database joins** split by shape: a **logical** blend (a `collection` of independent tables with a
  `<relationships>` key block) is **rebuilt** — each side landed via its own per-connector M, with the join
  keys emitted as model relationships (a per-source / composite model; no federated cross-DB query). Only a
  **physical** `<clause>` join/union tree (one logical table whose rows come from a cross-source join) is
  **deferred** to `fallback` with land-to-Delta / composite-model guidance. Relationship **cardinality** is
  emitted as the default many-to-one (Tableau's logical model encodes no cardinality) and flagged for review.
- **Spatial** (`ogrdirect`) datasources are routed to `fallback` rather than approximated.
- **Packaged data extraction** — packaged files are read only to confirm columns and auto-detect the CSV
  delimiter; the `FilePath` parameter still defaults to the original Tableau path and is a repoint follow-up.
- Hierarchies, calculated columns, sets/groups/bins, parameters, RLS, and other model objects are **not**
  generated here — they remain follow-ups exactly as in [feature-parity.md](feature-parity.md).
