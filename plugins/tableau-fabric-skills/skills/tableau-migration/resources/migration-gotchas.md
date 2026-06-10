# Migration Gotchas

Failure modes the agent will actually hit, and the deterministic response for each. Load this when a
migration step errors or produces something unexpected.

---

## Parsing the `.tds`

| Symptom | Cause | Response |
|---|---|---|
| Garbled first characters / parse error | UTF-8 BOM | Open with `encoding="utf-8-sig"` — Tableau always writes a BOM |
| `.tdsx` won't parse | It's a zip, not XML | Unzip; the `.tds` is at the root or under `Data/` |
| Relations come back `unknown` | `table` attribute isn't `[schema].[item]` / `[item]` | Parser returns `(None, None)` and flags it — route to fallback, don't guess a schema |
| A table appears twice | Modern "object model" `.tds` duplicates tables under `<properties>` and wraps them in `<relation type='collection'>` | Already handled: the parser promotes collection children as independent tables and dedupes copies |

---

## Storage mode

| Symptom | Cause | Response |
|---|---|---|
| `select_storage_mode` returns `mode = None` | Join/union tree, >1 named connection, unmapped connector, or no column metadata | Expected — route to land-to-Delta + DirectLake (bridge Play 2/3/4) |
| Connector emits a "scaffold" | Snowflake/BigQuery navigation differs from the `Sql.Database` family | Review the M before refresh; the mode is right, the navigation needs a glance |
| Flat-file model has no path | Excel/CSV needs a file path the `.tds` doesn't carry | Supply the path on the M partition |

---

## Calculated fields → DAX

| Symptom | Cause | Response |
|---|---|---|
| A measure is `= 0` with only a `TableauFormula` annotation | Formula outside the safe subset (LOD, table calc, CASE, scalar date/string fn, 4-arg IIF, cross-table) | Expected stub — repair manually or via a validation-gated LLM pass |
| A simple-looking calc still stubs | Bare row-level field in a measure (e.g. `[Sales]` not `SUM([Sales])`), or mixed-type IF branches | Measure context requires aggregations; make branch types consistent |
| `COUNTD` is off by one vs Tableau | Plain `DISTINCTCOUNT` counts BLANK | Already handled — translator emits `DISTINCTCOUNTNOBLANK` |
| `COUNT` over a text column errors | DAX `COUNT` is numeric-only | Already handled — translator emits `COUNTA` |
| An empty aggregation reads as 0 not NULL | DAX BLANK coercion vs Tableau three-valued NULL | Known difference; reconciliation flags it (see [calc-to-dax.md](calc-to-dax.md)) |

---

## Connection binding

| Symptom | Cause | Response |
|---|---|---|
| Refresh fails on credentials | Credentials are a manual boundary | **Stop** and have the user configure the connection; never enter credentials for them |
| DirectQuery to on-prem fails | No gateway | User selects/sets up an on-prem data gateway |
| A custom-SQL table is slow / materializes | The native query didn't fold | Review the `Value.NativeQuery(..., [EnableFolding=true])`; fix the SQL so it folds |

---

## Deploy & validate

| Symptom | Cause | Response |
|---|---|---|
| `createOrUpdate` rejects the definition | Hand-rolled payload drift | Delegate deploy to `semantic-model-authoring`; don't hand-roll `createItem` |
| Measure value ≠ Tableau | Different filter context on the two sides, or a real semantic gap | Match the filter context first; a genuine gap is a real mismatch to investigate |
| Float values differ slightly | Cross-engine rounding | Compare with a relative epsilon, not exact equality (see [validation-reconciliation.md](validation-reconciliation.md)) |

---

## Security

| Symptom | Cause | Response |
|---|---|---|
| Secret almost committed | `.tds`/`.tdsx`/`.twb`/`.hyper` are plaintext and may embed connection info | They are git-ignored — keep them out of the model, the report, and the repo (see [security-governance.md](security-governance.md)) |
