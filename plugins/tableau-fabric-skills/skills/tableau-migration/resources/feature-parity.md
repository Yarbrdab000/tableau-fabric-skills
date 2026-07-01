# Feature Parity

What v1 of `tableau-migration` rebuilds, approximates, and does not touch. Use this to set expectations
before a migration and to populate the "Not migrated" section of the report.

> **Migration shape:** unlike the source-code migration peers (`synapse-`/`databricks-`/`hdinsight-migration`,
> which rewrite notebook code), Tableau migration is **artifact reconstruction**: datasource → semantic
> model, calc → DAX, viz → report (preview — Tier-1 structure). Fidelity is high for the model and
> partial-by-design for calcs and report structure.

---

## Data model

| Tableau construct | Power BI target | v1 status |
|---|---|---|
| Published data source | Semantic model (TMDL) | ✅ Rebuilt |
| Physical table | Model table | ✅ One per `table`/`custom_sql` relation |
| Column + data type | Typed model column | ✅ Typed from source schema |
| Hidden join keys | Model relationship | ✅ Inferred, oriented by real cardinality |
| Extract (`.hyper`) | Import model | ✅ |
| Live connection | DirectQuery model | ✅ (`Sql.Database` family + Oracle/Snowflake/Databricks emit deploy-ready M; Teradata/BigQuery flagged scaffold — live-navigator M not yet verified) |
| Custom SQL | `Value.NativeQuery` partition | ✅ Preserved; folding requested (verify before refresh) |
| Generic ODBC + Custom SQL (e.g. MinIO/object storage behind a driver) | `Odbc.Query` Import partition | ✅ Engine-agnostic; reconstructed from the driver/DSN facts (install the ODBC driver where the model runs) |
| Native query engine (Spark / Presto / Trino / Starburst) | `Odbc.Query` (custom SQL) / `Odbc.DataSource` (table) Import partition | ✅ Bound over ODBC, **never landed in Delta**; the per-engine ODBC driver name is assumed and flagged confirm-required |
| Federated / blended / join tree | — | ⚠️ Fallback to land-to-Delta + DirectLake |

---

## Calculations

The deterministic translator emits DAX only where the mapping is **provably faithful**; everything
else stays an inert `= 0` stub with the original formula preserved as a `TableauFormula` annotation.
Coverage of the in-scope function catalog is currently ~74% and growing.

| Tableau construct | v1 status |
|---|---|
| `SUM/AVG/MIN/MAX/MEDIAN/COUNT/COUNTD/STDEV/VAR/PERCENTILE` + arithmetic | ✅ Translated to DAX measures |
| `IF/ELSEIF/ELSE/END`, `IIF` (3-arg), comparisons, `AND`/`OR`/`NOT`, `IN` | ✅ |
| `ZN` / `IFNULL` / `ISNULL`, string literals | ✅ |
| Scalar math & trig (`ABS`/`ROUND`/`CEILING`/`FLOOR`/`POWER`/`SQRT`/`LOG`/`LN`/`EXP`/`SIN`/`COS`/`TAN`…) | ✅ Over aggregated or row-level operands |
| Scalar date/string fns (`YEAR`/`QUARTER`/`MONTH`/`DATEADD`/`DATETRUNC`/`DATEDIFF`/`LEFT`/`RIGHT`/`MID`/`UPPER`/`LOWER`/`PROPER`/`ASCII`/`CHAR`/`ISOWEEK`…) | ✅ As calculated columns (row level), or over aggregated operands in a measure; date attributes can bind to the generated Date table |
| Row-level calculated fields (dimension-role) | ✅ Translated as DAX **calculated columns** |
| LOD `{FIXED …}` and table-scoped `{AGG(…)}` | ✅ Translated (`CALCULATE(…, ALLEXCEPT/ALL)`) |
| LOD `{INCLUDE …}` / `{EXCLUDE …}` | ❌ Stub (viz-filter-context dependent) |
| Table calcs `WINDOW_*`/`RUNNING_*`/`RANK`/`INDEX`/`LOOKUP`/`SIZE`/`FIRST`/`LAST` | 🟡 The translator handles the subset whose addressing (Compute Using) is recoverable from a `.twb`/`.twbx`; a datasource-only migration preserves them as stubs |
| `CASE`/`WHEN` (value mapping) | ✅ Translated to DAX `SWITCH` — searched `CASE WHEN c THEN r …` → `SWITCH(TRUE(), c, r, …)`, simple `CASE x WHEN v …` → `SWITCH(x, v, r, …)` (a 2-way collapses to `IF(EXACT(…))`, preserving Tableau's case-sensitive match). A *simple*-form `CASE` with a bare **row-level** comparand only stubs in *measure* mode — it translates as a calculated **column** |
| `[Parameters].[X]`-driven `CASE`/`IF` field/measure swap | 🟡 Deterministic field-parameter + what-if-parameter emitters exist (`parameters.py`: `detect_field_swap` → `emit_field_parameters`/`emit_value_parameters`) and are exercised via the parameters / second-compiler path. The default datasource auto-run leaves the calc as a stub and routes it to the `model_object_parameter` handoff. Measure swap currently emits a **field parameter** (a calculation group is the documented richer alternative, not yet built) |
| Regex (`REGEXP_*`), arbitrary-format `DATEPARSE` | ❌ Stub (no faithful DAX equivalent) |
| Cross-table calcs | ❌ Stub (filter context not guaranteed) |

Every stub is an inert `= 0` with the original formula kept as a `TableauFormula` annotation, ready for
a human — or the assisted second compiler — to finish. See [calc-to-dax.md](calc-to-dax.md).

---

## Model objects

| Tableau construct | Power BI target | v1 status |
|---|---|---|
| Drill path | TMDL `hierarchy` | ✅ Auto-derived when all levels resolve to one table; else skipped + reported |
| Field folder | `displayFolder` on column / measure | ✅ Auto-derived (flat folders) |
| User filter (wired RLS) | TMDL `role` | ✅ `[Field] = USERNAME()` → `USERPRINCIPALNAME()`; anything else fails closed (`FALSE()` + manual-review annotation), never guessed |

Auto-derived from the `.tds` by `migrate_tds_to_semantic_model`; every object is resolved or reported in
`report["model_objects"]`, never silently dropped. See [model-enrichment.md](model-enrichment.md).

---

## Not migrated by v1 (not rebuilt)

Calculated columns, sets / groups / bins, what-if parameters, calc groups, field parameters, perspectives,
and other governance objects. The scripts do not auto-detect these; when the agent has the Tableau metadata
it should enumerate any present and list them as manual follow-ups so the customer adds them deliberately.
(Hierarchies, display folders, and RLS roles **are** rebuilt — see **Model objects** above and
[security-governance.md](security-governance.md) for the RLS safety boundary.)

---

## Worksheets & dashboards (supported — preview, Tier-1 structure)

Worksheet / dashboard → Power BI **report (PBIR)** ships as a **preview** that rebuilds Tier-1
*structure* — the right chart type, **exact** field bindings to the migrated model, position/layout,
filters/parameters → slicers, and default cross-filter — into an **openable, model-bound** `.pbip`.
The viz grammar (marks, shelves, filters, chart types) lives in the workbook `.twb`/`.twbx` XML (not
the Metadata API); `scripts/twb_to_pbir.py` parses it into a normalized IR and emits PBIR parts, and
`scripts/migrate_estate.py` binds each rebuilt report to a model rebuilt from the workbook's own
embedded datasource (`pbip/<Workbook>/<Workbook>.pbip`). See [viz-rebuild.md](viz-rebuild.md).

| Tableau viz | Power BI target | status |
|---|---|---|
| Bar / column, line, area, text table, matrix, pie, scatter, filled + symbol map, card / multi-row card | PBIR `clusteredColumnChart` / `clusteredBarChart` / `lineChart` / `areaChart` / `tableEx` / `pivotTable` / `pieChart` / `scatterChart` / `filledMap` / `map` / `card` / `multiRowCard` | ✅ Rebuilt (structure + exact bindings) |
| Worksheet / dashboard filters, what-if / field parameters | `slicer` | ✅ Surfaced as slicers (review scope after import) |
| Dashboard layout (zones) | Report page layout | ✅ Zones scaled into the page |
| Visual **formatting** — specific colors, fonts, title/label styling, legends, conditional-format palettes, theme JSON, tooltips | — | ❌ Deferred to a later (Tier-2) pass |
| Ambiguous chart-type adjudication, custom-geometry / density maps, KPI target/trend, table-calc-dependent layouts | — | ⚠️ Reported as a structured `warning`, never rebuilt wrong |

Everything the rebuild can't do faithfully is emitted as a structured warning (`viz_fidelity` /
`pbip_warnings`) rather than a wrong visual.

---

## Honest framing for stakeholders

- The **data model** migrates with high fidelity.
- **Calculations** migrate for a safe, type-checked subset; the rest are preserved-formula stubs a human
  finishes — and [reconciliation](validation-reconciliation.md) proves the translated ones equal Tableau.
- **Dashboards** rebuild at Tier-1 *structure* (chart type, exact bindings, layout, slicers) as a
  preview, into an openable model-bound `.pbip`; visual *formatting* (colors, fonts, legends) is a
  later pass. Never claim full parity.
