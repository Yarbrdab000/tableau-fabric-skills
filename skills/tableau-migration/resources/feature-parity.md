# Feature Parity

What v1 of `tableau-migration` rebuilds, approximates, and does not touch. Use this to set expectations
before a migration and to populate the "Not migrated" section of the report.

> **Migration shape:** unlike the source-code migration peers (`synapse-`/`databricks-`/`hdinsight-migration`,
> which rewrite notebook code), Tableau migration is **artifact reconstruction**: datasource → semantic
> model, calc → DAX, (v2) viz → report. Fidelity is high for the model and partial-by-design for calcs.

---

## Data model

| Tableau construct | Power BI target | v1 status |
|---|---|---|
| Published data source | Semantic model (TMDL) | ✅ Rebuilt |
| Physical table | Model table | ✅ One per `table`/`custom_sql` relation |
| Column + data type | Typed model column | ✅ Typed from source schema |
| Hidden join keys | Model relationship | ✅ Inferred, oriented by real cardinality |
| Extract (`.hyper`) | Import model | ✅ |
| Live connection | DirectQuery model | ✅ (`Sql.Database` family fully; Snowflake/BigQuery scaffold) |
| Custom SQL | `Value.NativeQuery` partition | ✅ Preserved; folding requested (verify before refresh) |
| Federated / blended / join tree | — | ⚠️ Fallback to land-to-Delta + DirectLake |

---

## Calculations

| Tableau construct | v1 status |
|---|---|
| `SUM/AVG/MIN/MAX/MEDIAN/COUNT/COUNTD` + arithmetic | ✅ Translated to DAX measures |
| `IF/ELSEIF/ELSE/END`, `IIF` (3-arg) | ✅ |
| Comparisons, `AND`/`OR`/`NOT` | ✅ |
| `ZN` / `IFNULL` / `ISNULL`, string literals | ✅ |
| LOD `{FIXED/INCLUDE/EXCLUDE}` | ❌ Stub (formula preserved) |
| Table calcs `WINDOW_*`/`RUNNING_*`/`RANK`/`LOOKUP`/`INDEX` | ❌ Stub |
| `CASE`/`WHEN`, scalar date/string/regex functions | ❌ Stub |
| Row-level calculated fields | ❌ Stub (measure context only) |
| Cross-table calcs | ❌ Stub (filter context not guaranteed) |

Every stub is an inert `= 0` with the original formula kept as a `TableauFormula` annotation. See
[calc-to-dax.md](calc-to-dax.md).

---

## Not migrated by v1 (not rebuilt)

Hierarchies, calculated columns, sets / groups / bins, what-if parameters, calc groups, field parameters,
**RLS** and other governance objects, perspectives, and display folders. The scripts do not auto-detect
these; when the agent has the Tableau metadata it should enumerate any present and list them as manual
follow-ups so the customer adds them deliberately (RLS especially is left manual on purpose — see
[security-governance.md](security-governance.md)).

---

## Worksheets & dashboards (roadmap — v2)

Worksheet / dashboard → Power BI **report (PBIR)** is **not** in v1. The viz grammar (marks, shelves,
filters, chart types) lives in the workbook `.twb`/`.twbx` XML — not the Metadata API — and needs a new
parser plus a Tableau-viz → PBIR mapper. The output half is already proven (the bridge toolkit's Play 5
PBIR generator), and v1 rebuilds the measures those visuals will bind to, so v2 starts from a wireframe-level
report bound to v1's model.

---

## Honest framing for stakeholders

- The **data model** migrates with high fidelity.
- **Calculations** migrate for a safe, type-checked subset; the rest are preserved-formula stubs a human
  finishes — and [reconciliation](validation-reconciliation.md) proves the translated ones equal Tableau.
- **Dashboards** are roadmap. Never claim full parity.
