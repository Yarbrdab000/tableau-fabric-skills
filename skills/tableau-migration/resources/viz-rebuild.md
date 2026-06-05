# Visual wireframe rebuild (`.twb` → PBIR)

`scripts/twb_to_pbir.py` is a clean-room, stdlib-only converter that reads a Tableau
workbook (`.twb`, UTF-8 BOM XML) and emits a **PBIR** (Power BI Enhanced Report) wireframe
that binds to the semantic model produced by the rest of this skill. It is deliberately a
**small, correct slice**: a handful of chart types are rebuilt faithfully and *everything
else is reported as a structured warning* — never a silently wrong visual.

It is built only from primary sources: the Tableau workbook XML grammar (worksheets,
`<datasources>`, `<mark class>`, rows/cols shelves, encodings, filters) and Microsoft's
public PBIR JSON schemas.

## Pipeline

```
.twb XML ──parse_twb──▶ normalized IR (plain dicts) ──emit_pbir──▶ { path: text } PBIR parts
```

- `parse_twb(xml_text) -> {"worksheets": [...], "dashboards": [...], "warnings": [...]}`
  Accepts `str` or `bytes`; BOM is stripped / decoded with `utf-8-sig`.
- `emit_pbir(ir, *, dataset_name, report_name, model_table=None, field_map=None) -> {path: text}`
- `migrate_twb_to_pbir(xml_text, ...) -> {"ir", "parts", "warnings"}` (convenience wrapper).

## Command line (live validation)

The module is also runnable, so a real exported workbook can be converted and the resulting
`<report>.Report` folder opened in Power BI Desktop or deployed to a Fabric workspace. It is
purely local — it reads a `.twb` file (or stdin) and writes JSON files; **no network, no
credentials, no secrets**, and every target name comes from an argument / environment
variable, never the code:

```
py twb_to_pbir.py <input.twb> -o <out-dir> --dataset "Superstore" --report "Superstore Report"
py twb_to_pbir.py - --dataset Superstore        # read XML from stdin, print a JSON manifest
```

- `-o/--out` writes the parts under `<out-dir>/<report>.Report/…`; without it a JSON manifest
  (part paths + warnings) is printed to stdout for a no-write dry run.
- Defaults also read `TWB_PBIR_OUT` / `TWB_PBIR_DATASET` / `TWB_PBIR_REPORT` /
  `TWB_PBIR_MODEL_TABLE` from the environment.
- Warnings are printed to stderr (when writing) or included in the dry-run manifest.

The committed pytest suite stays fully offline/deterministic (synthetic `.twb` string
fixtures, no disk, no network); the live open/deploy is a separate manual pass.

## Supported visual types

| Tableau mark + shelf layout                         | IR `visual_type` | PBIR `visualType`      | Data roles            |
| --------------------------------------------------- | ---------------- | ---------------------- | --------------------- |
| Bar, dimension on **columns**, measure on **rows**  | `column`         | `clusteredColumnChart` | Category / Y / Series |
| Bar, dimension on **rows**, measure on **columns**  | `bar`            | `clusteredBarChart`    | Category / Y / Series |
| Line (needs ≥1 measure)                             | `line`           | `lineChart`            | Category / Y / Series |
| Text, dimensions on **one** axis                    | `table`          | `tableEx`              | Values                |
| Text, dimensions on **both** axes                   | `matrix`         | `pivotTable`           | Rows / Columns / Values |
| Categorical / date / numeric **filter**             | (slicer)         | `slicer`               | Values                |

`Automatic` marks are inferred from the shelves (dim+measure → column; dims only → table or
matrix). A `color` encoding on a dimension populates the **Series** role.

## Binding contract (matches the v1 model exactly)

The `.twb` embeds the full datasource (`<relation>` + `<metadata-records>`), so bindings are
resolved from the workbook itself rather than guessed from captions:

- **Table** (`Entity` / `SourceRef.Entity`) = the Tableau `<relation name=...>`.
- **Column** (`Property`) = `clean_col(<remote-name>)` — the *source* column name run through
  the same `clean_col` imported from `tmdl_generate`, so names match the generated model even
  when the workbook renames the field's caption.
- **Measure** = the calculated field's caption, in the `_Measures` table.

Fields are matched by their internal id (e.g. `[Sales]`), so a workbook-side caption rename
still binds to the right model column. Callers can override binding precisely with a
`field_map` `{caption: {"entity", "property", "binding"}}`, or pin every column to one table
with `model_table=`.

### Field expressions (semantic query)

- Column: `{"Column": {"Expression": {"SourceRef": {"Entity": T}}, "Property": C}}`
- Measure: `{"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}}, "Property": M}}`
- Aggregation wraps a Column with a function code:
  `Sum=0, Avg=1, DistinctCount=2, Min=3, Max=4, Count=5, Median=6`.

## PBIR output layout

One `.Report` folder per workbook, paths relative:

```
definition.pbir                                   (datasetReference byPath ../<dataset>.SemanticModel)
definition/version.json                           (versionMetadata 1.0.0)
definition/report.json                            (report 1.0.0)
definition/pages/pages.json                       (pagesMetadata 1.0.0: pageOrder, activePageName)
definition/pages/<page>/page.json                 (page 1.0.0)
definition/pages/<page>/visuals/<v>/visual.json   (visualContainer 1.0.0)
.platform
```

- **One page per dashboard.** Dashboard zones whose name matches a worksheet become visuals;
  zone `x/y/w/h` (Tableau internal coordinate units) are scaled into the `1280×720` page.
- A worksheet **not** placed on any dashboard gets its own page (one visual filling the page).
- Object names are sanitized to word-chars/hyphen with a short hash suffix for uniqueness, and
  each visual's `queryRef`s are de-duplicated.

## Unsupported handling (→ `warnings`, never a wrong visual)

Every warning is `{"scope": "worksheet"|"dashboard", "name": <name>, "reason": "manual attention required: ..."}`.
Cases that degrade to a warning instead of a visual/binding:

- **Unsupported marks**: pie, area, polygon, shape, map / filled map, density/heatmap,
  Gantt (non-bar), circle/square scatter, etc. → the worksheet emits **no** visual.
- **Scatter and card/KPI** are out of scope for this slice (deferred to a later pass).
- **Table calculations** and other window/running derivations (e.g. `WindowSum`) → field skipped.
- **Aggregation/type mismatch**: `Sum`/`Avg`/`Median` on a non-numeric column, or `Min`/`Max`
  on a non-numeric/non-date column → field skipped.
- **Date parts** (`Year`, `Month`, `Quarter`, …) → approximated as a plain date column; the
  date grain is *not* applied (flagged so it can be set manually).
- **Calculated field on an axis** (a measure where a category is required) → skipped.
- **Caption fallback**: when a field has no embedded metadata record, it is bound by caption as
  a best effort and flagged to verify against the model's table/column names.
- **Tableau pseudo-fields** (`Measure Names`, `Measure Values`, `Number of Records`) → skipped.

### Filters → slicers (wireframe placeholders)

Worksheet filters are surfaced as **slicer** visuals so the field and intent survive the
migration: categorical → list slicer, date / relative-date → date slicer, numeric →
range slicer. These are **placeholders** — Tableau's filter *scope* (worksheet / dashboard /
context / data-source) and actions do not map 1:1 to Power BI slicer interactions, so slicer
wiring should be reviewed after import.

## Tests

`tests/test_twb_to_pbir.py` is fully offline (inline `.twb` XML string fixtures, no disk, no
network). It asserts the normalized IR (entity/property/aggregation per visual), the emitted
PBIR JSON structure (report scaffold, page-per-dashboard, orphan-worksheet page, role
projections, field expressions, unique queryRefs, zone scaling within page bounds) and that
unsupported marks/derivations/filters produce warnings rather than visuals.
