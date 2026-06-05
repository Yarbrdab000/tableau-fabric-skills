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
| `classification` | `flat_file` / `published_reference` / `relational` / `spatial` |
| `file_type` | `Excel` / `CSV` / `Spatial` / `None` |
| `relations` | One per source table: `{kind, name, item, columns:[{remote_name, model_name, tmdl_type}]}` |
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

---

## Statuses

Per-datasource status mirrors the estate model, with one documented marker for the published case:

| Status | Meaning |
|---|---|
| `migrated` | Rebuilt and fully supported (e.g. a clean relational DirectQuery/Import) |
| `migrated_with_followups` | Rebuilt but needs manual action — flat-file `FilePath` repointing, or storage caveats |
| `published_unresolved` | A `sqlproxy` reference with no resolver result; the referenced datasource is a follow-up |
| `fallback` | Not safely rebuildable — spatial, structurally unsupported shape, or a clean-name column collision |
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

- **Spatial** (`ogrdirect`) datasources are routed to `fallback` rather than approximated.
- **Packaged data extraction** — packaged files are read only to confirm columns and auto-detect the CSV
  delimiter; the `FilePath` parameter still defaults to the original Tableau path and is a repoint follow-up.
- Hierarchies, calculated columns, sets/groups/bins, parameters, RLS, and other model objects are **not**
  generated here — they remain follow-ups exactly as in [feature-parity.md](feature-parity.md).
