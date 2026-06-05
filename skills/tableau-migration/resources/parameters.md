# Tableau Parameters → Power BI / Fabric

Tableau **parameters** are single-value controls — a user picks one value from a list, a numeric/
date range, or unbounded free input — and calculations, filters, axes and reference lines read that
selection. Power BI has no 1:1 parameter object, so the faithful rebuild is the well-known
**disconnected-table + DAX** idiom: a table of candidate values with **no relationship** to the
fact table, plus DAX that reads the user's slicer selection (`SELECTEDVALUE` / `TREATAS` / `RANKX`).
The "no relationship" part is essential — it is exactly how a Tableau parameter behaves (it filters
nothing on its own; calcs decide what the selection means).

This is implemented by `scripts/parameters.py` and exercised by `tests/test_parameters.py`. The
module is **pure** (XML in → TMDL/DAX strings out), offline, and deterministic.

---

## Where parameters live in the Tableau XML

A synthetic `<datasource name='Parameters'>` block. Each `<column>` is one parameter:

| Attribute / child | Meaning |
|---|---|
| `caption` | Display name (e.g. `Facility Name Parameter`). |
| `name` | Internal name (e.g. `[Facility Name Parameter]`, `[Parameter 1]`). |
| `datatype` | `string` / `integer` / `real` / `boolean` / `date` / `datetime`. |
| `param-domain-type` | `list` (allowed values) / `range` (min–max) / `all` (unbounded). |
| `value` | Current/default value. **Strings are wrapped in quotes** (`value='"New York…"'`); dates as `#2020-01-01#`. |
| `<calculation formula='…'/>` | The default expression. |
| `<member value='…' alias='…'/>` | List members (repeated). `alias` is the optional display label. |
| `<range min='…' max='…' granularity='…'/>` | Range bounds + step. |

Read `.twb`/`.tds` with `encoding="utf-8-sig"` (UTF-8 BOM); `extract_parameters` also strips a
leading BOM defensively and tolerates malformed XML (returns `[]`).

---

## Public contract (stable — other streams bind to this)

```python
extract_parameters(xml_text) -> list[ParamSpec]
classify_parameter(spec, usages, storage_mode=None) -> CapabilityClass
param_table_tmdl(spec, storage_mode="import") -> str        # disconnected table TMDL block ("" if not enumerable)
param_value_measure(spec) -> (measure_name, dax)            # single-select-safe value measure
param_ref_name(spec) -> str                                # the value-measure name
# name helpers so slicer / model-emit / orchestrator streams agree on identifiers:
param_table_name(spec) -> str
param_value_column(spec) -> str        # column the value measure reads
param_slicer_column(spec) -> str       # column a slicer binds to (label column when aliases differ)
param_order_column(spec) -> str        # hidden ordinal Sort-By column (list only)
emit_parameter(spec, usages=None, storage_mode="import") -> dict   # convenience bundle
```

### `ParamSpec`
`name, caption, datatype, domain_type, members[(value, alias)], range, default, formula, usage_class`

- `members` values are **typed** Python scalars (str/int/float/bool); `alias` is the display label
  or `None` (= "same as value").
- `range` is a `RangeSpec(min, max, step, granularity)` (decoded scalars) or `None`.
- `default` is the decoded current value. `usage_class` is filled in by `classify_parameter`.

### `CapabilityClass`
`name, tier, strategy, deploy_ready, warnings` — the verdict on how a parameter can be rebuilt and
how loudly we must warn. `tier` is `1`/`2`/`3` or `None` (manual-only). `deploy_ready` is `False`
whenever a human must finish or verify the translation.

**Calc resolver (Stream A) hook:** rewrite a Tableau `[Parameters].[<caption>]` reference to the
DAX measure reference `[<param_ref_name(spec)>]` (e.g. `[Facility Name Parameter Value]`).

---

## The three tiers (all disconnected table + DAX)

### Tier 1 — value parameter *(implemented)*
A parameter read **inside a measure**.

- **Candidate table** (`param_table_tmdl`), a `partition … = calculated / mode: import` table with
  columns marked `type: calculatedTableColumn`:
  - **list** → `DATATABLE(…)` with a hidden **ordinal `… Order` column** preserving the authored
    member order, set as the value column's `sortByColumn`. When member **aliases differ** from
    their values, a second `… Label` column is emitted (slicer shows the label; the measure reads
    the value), with duplicate-caption detection.
  - **numeric range** → `GENERATESERIES(min, max, step)` (output column `[Value]`).
  - **date range** → `CALENDAR(min, max)` (output column `[Date]`) — **never** `GENERATESERIES`.
- **Value measure** (`param_value_measure`), single-select-safe:
  ```DAX
  Facility Name Parameter Value =
  IF(
      HASONEVALUE('Facility Name Parameter'[Facility Name Parameter]),
      SELECTEDVALUE('Facility Name Parameter'[Facility Name Parameter]),
      "New York State Hospital"
  )
  ```
  (Equivalent to `SELECTEDVALUE(col, default)`; the explicit form is emitted for clarity and to
  make the single-select fallback obvious.)

### Tier 2 — dimension-swap / dependent / measure-swap *(Phase 2)*
- **Measure-swap** → `SWITCH([X Value], "Sales", [Sales], "Profit", [Profit], …)`.
- **Dimension/axis-swap & visual-filter** → push the disconnected selection onto the real fact
  column with `CALCULATE(<measure>, TREATAS(VALUES('X'[X]), Fact[Col]))` (no relationship), or a
  Field Parameter for axis swaps.
- **Cascading (parent → child)** → a dependent `1/0` flag measure used as a visual-level filter so
  the child slicer only shows values valid for the parent selection.

### Tier 3 — Top-N *(Phase 2)*
Disconnected N table (e.g. 5/10/All) + a `RANKX` ranking measure + a filter measure:
```DAX
Top N Filter =
IF(NOT ISFILTERED('Top N'[Top N]), 1,
   IF([Rank] <= SELECTEDVALUE('Top N'[Top N]), 1, 0))
```
applied as a visual-level filter (`= 1`). "Nothing selected = show all" — Top-N is a *calculation*,
not a static visual filter.

---

## Guardrails (be LOUD; never silently mistranslate)

Classify a parameter's **usage** before emitting (`classify_parameter(spec, usages)` where `usages`
is a usage token / set from the workbook-parsing stream: `measure`, `filter`, `axis`/`dimension`,
`measure_swap`, `top_n`, `bin`, `reference_line`, `calc_column`):

- **Row-level calc → never a static calculated column.** A `calc_column` usage is flagged manual:
  a Power BI calculated column evaluates at *refresh*, not at *slicer-time*, so it would silently
  ignore the slicer. Rebuild the dependent logic as a measure that reads the value measure.
- **Unbounded (`all`)** can't be enumerated → `param_table_tmdl` returns `""`, `param_value_measure`
  returns a **constant** `X Value = <default>`, and the class is `manual-unbounded`
  (`deploy_ready=False`).
- **Bin size** → flagged manual (Power BI bins are static; needs a dynamic banding measure).
- **Storage-mode aware.** A DAX calculated table is fine for **Import**, forces a **composite**
  model under **DirectQuery**, and is **unsupported in a pure Direct Lake model** — surfaced as a
  `///` note on the table and a warning from `classify_parameter` (Direct Lake ⇒ `deploy_ready=False`).
- **Single-select** via `HASONEVALUE`; **list order** via the ordinal Sort-By column (skipped, with
  a warning, when displayed values aren't unique); **date ranges** via `CALENDAR` (daily — non-daily
  granularity / `datetime` emit a precision warning); **float ranges** keep the authored step (with a
  note on floating-point inclusive-end behaviour); **aliases** as a `value|label` pair with
  duplicate-caption detection; **default not in members** raises a warning.

---

## References consulted (no code/structure/tests copied)

- The Information Lab — *Power BI Migration* **Part 4** and **Part 5** (the disconnected-table +
  `SELECTEDVALUE`/`GENERATESERIES`/`TREATAS`/`RANKX` parameter idioms). Used for *facts only*; the
  DAX patterns are well-known and were reimplemented from first principles.
- A Tableau→Power BI reference repository that inventories parameters but does not translate them.

These were read for understanding only — no source's code, structure, naming, or tests were copied.
`THIRD_PARTY_NOTICES.md` at the repository root (integrator-owned) records third-party attributions.
