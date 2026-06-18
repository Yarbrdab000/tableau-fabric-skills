# Comparison methodology

How `compare.py` decides whether a Tableau datasource "already exists" in Fabric. The engine is
**pure and offline** — it consumes the two inventory JSON shapes and emits a ranked report. Original
work; this document is the spec.

## The four signals

Each Tableau datasource is scored against every Fabric semantic model on four independent signals, each
normalised to `0..1`:

| Signal | Default weight | Definition |
|---|---:|---|
| `name`   | 0.20 | Jaccard over **name tokens** (lower-cased, split on non-alphanumerics, common stopwords like `datasource`/`data`/`source` dropped). An exact normalised-name match short-circuits to `1.0`. |
| `column` | 0.35 | Jaccard over **normalised field/column names** (so `Row_ID`, `Row ID`, and `[Row ID]` all collapse to `rowid`). |
| `type`   | 0.15 | Of the columns that overlap by name, the share whose Tableau type is **compatible** with the Fabric (TMDL) type. |
| `source` | 0.30 | Overlap of the **underlying physical source** — see the three-tier scheme below. |

The overall score is the weighted average of the signals that could actually be measured (see
*Obscured sources*). Weights are overridable with `--weights name=..,column=..,type=..,source=..`.

## Type compatibility

Tableau Metadata-API / `.tds` data types are upper-case (`INTEGER`, `REAL`, `STRING`, `DATE`,
`DATETIME`, `BOOLEAN`, …); TMDL column types are camelCase (`int64`, `double`, `string`, `dateTime`,
`boolean`, …). A small original map declares the compatible set per Tableau type (e.g. `INTEGER` →
`{int64, decimal, double}`). An **unknown** Tableau type maps to "compatible with anything" so we never
penalise on missing information rather than a real mismatch.

## The `source` signal: three tiers, best wins

Physical-source overlap is the hardest signal because the same data is reached very differently on each
platform. We compute three candidate scores and take the **maximum**:

1. **Strict** — Jaccard over `(connector, database, table)` keys. Both sides agree on the catalog *and*
   the table. Full credit.
2. **Loose** — Jaccard over `(connector, table)` keys, weighted `× 0.85`. Same connector and table,
   different database name (dev vs prod, a renamed catalog).
3. **Table-name** — Jaccard over **bare table names only**, weighted `× 0.70`. Connector- and
   database-agnostic; this is the tier that survives a platform move.

Connector strings are folded to canonical tokens first (`azure_sqldb`, `Microsoft SQL Server`, `mssql`
→ `sqlserver`; `postgresql` → `postgres`; `spark` → `databricks`; …) so SQL Server on the Tableau side
lines up with `Sql.Database` on the Fabric side.

### Why the table-name tier exists — the lakehouse intermediary

A Fabric semantic model frequently reads from a **Lakehouse or Warehouse that mirrors** the primary
source, while the Tableau datasource connects to that primary source **directly**. The connector and
database therefore never match — only the **table names** do. Without the table-name tier these real
overlaps would score `source = 0` and be misclassified as "needs rebuild". The Fabric side draws table
names from both the parsed M source *and* the model's own `tables` list, so even a model whose partition
source is fully obscured still contributes its table names.

### Helper-table filtering

Model scaffolding — a `Date`/`Calendar` dimension, a `_Measures` holder, `Parameters`, and
field-parameter `… Swap` tables — are **not** physical source tables and would dilute the table-name
Jaccard. They are excluded from the table-name set so the signal stays precise.

## Obscured upstream sources

The physical source can be hidden on **either** side:

- **Fabric:** composite / DirectQuery models over an AnalysisServices/Power BI dataset or a dataflow,
  and Databricks/Snowflake M expressions we cannot resolve to a concrete table.
- **Tableau:** a datasource that references **another published datasource**, or whose lineage Catalog
  never indexed.

When **neither side yields any usable table name**, scoring `source = 0` would wrongly bury a genuine
schema-level match. Instead the `source` signal is **dropped** (`null`) and its weight is redistributed
across `name`, `column`, and `type`. Every match carries a `source_compared` flag; the Markdown report
renders the source sub-score as `n/a` when it is `false`.

## Tiers and the estate rollup

The best score per datasource is banded high-to-low:

| Tier | Default threshold | Rollup bucket |
|---|---:|---|
| `Exact`   | ≥ 0.85 | `already_exists` |
| `Strong`  | ≥ 0.65 | `already_exists` |
| `Partial` | ≥ 0.40 | `partial` |
| `Weak`    | ≥ 0.15 | `rebuild` |
| `None`    | < 0.15 | `rebuild` |

- **already_exists** → reuse the existing model; verify before retiring the Tableau datasource.
- **partial** → a related model exists; reconcile added/renamed columns or source drift before reuse.
- **rebuild** → no real equivalent; hand to the `tableau-migration` skill.

## Tuning notes

- Fabric models commonly add measures and calculated columns, which **inflates the column count** and
  lowers the column Jaccard versus the Tableau source. If your estate does this heavily, raise the
  `name`/`source` weights or lower the `Strong` threshold.
- Re-scoring is free once inventories are cached to JSON — iterate on `--weights` without re-pulling.
- The bands are deliberately conservative: a `Strong` is "very likely the same data", not a guarantee.
  Treat the report as a ranked worklist a human confirms, not an automated decision.
