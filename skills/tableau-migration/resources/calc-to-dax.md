# Calculated Field → DAX

How the skill turns Tableau calculated fields into working DAX **measures**, deterministically and with no
LLM. The engine is `scripts/calc_to_dax.py`; this doc explains the supported subset, the safety rules, and
how fallbacks are handled. Run it from the orchestrator's **Phase 4**.

> **Honesty rule:** translation is a **safe, type-checked subset — not full DAX parity.** Anything outside
> the subset becomes an inert `= 0` stub, and the original Tableau formula is **always** preserved as a
> `TableauFormula` annotation so a human (or an optional validation-gated LLM pass) can finish it. Never
> claim a datasource's calcs were translated "completely."

---

## Public API

```python
from calc_to_dax import translate_tableau_calc_to_dax
dax, reason, tables_used = translate_tableau_calc_to_dax(formula, resolver)
```

- `resolver(caption) -> (table_display_name, clean_col, tmdl_type) | None` — resolves a Tableau field
  caption to a single landed column. Use `connection_to_m.build_m_field_resolver` for the Import/DirectQuery
  path or `field_resolver.build_field_resolver` for the DirectLake/landed-Delta path.
- `dax` is a DAX string on success, or `None` when the formula is outside the subset.
- `reason` is `"ok"` on success, otherwise a short human-readable cause (goes in the migration report).
- `tables_used` is the set of model tables the measure references.

---

## What translates (the safe subset)

| Tableau construct | DAX emitted | Notes |
|---|---|---|
| `SUM/AVG/MIN/MAX/MEDIAN/COUNT/COUNTD([field])` | `SUM/AVERAGE/MIN/MAX/MEDIAN/COUNTA/DISTINCTCOUNTNOBLANK('T'[Col])` | Single **bare** field only |
| Arithmetic `+ - * /`, unary `-`, parentheses | same, with `/` → `DIVIDE(...)` | Operands must be numeric |
| `IF c THEN a ELSEIF c2 THEN b ELSE z END` | nested `IF(c, a, IF(c2, b, z))` | No `ELSE` → 2-arg `IF` (BLANK when unmatched) |
| `IIF(cond, a, b)` | `IF(cond, a, b)` | 4-arg `IIF` is **not** supported |
| `CASE WHEN c THEN r … [ELSE z] END` | `SWITCH(TRUE(), c, r, …, z)` | Searched form; no `ELSE` → BLANK default |
| `CASE e WHEN v THEN r … [ELSE z] END` | `SWITCH(e, v, r, …, z)` | Simple form; `e` and values must be aggregated/literal |
| `ABS/SQRT/SIGN/EXP/LN/LOG(x)` | same name, `FN(x)` | `x` numeric; `LOG` is 1-arg base-10 |
| `ROUND(x)` / `ROUND(x, n)` | `ROUND(x, 0)` / `ROUND(x, n)` | Tableau 1-arg `ROUND` → 0 decimals |
| `CEILING(x)` / `FLOOR(x)` | `CEILING(x, 1)` / `FLOOR(x, 1)` | DAX requires a significance step |
| `POWER(x, n)` | `POWER(x, n)` | Both operands numeric |
| `= == <> != > >= < <=` | `=` / `<>` / `>` … | `==`→`=`, `!=`→`<>` |
| `AND` / `OR` / `NOT(x)` | `&&` / `||` / `NOT(x)` | Operands must be boolean |
| `ZN(x)` | `COALESCE(x, 0)` | |
| `IFNULL(a, b)` | `COALESCE(a, b)` | Branch types must match |
| `ISNULL(x)` | `ISBLANK(x)` | |
| String literals `"..."` / `'...'` | `"..."` (quotes doubled) | Backslash escapes → fallback |

Two aggregation choices are deliberate and worth knowing:

- **`COUNT` → `COUNTA`** — Tableau `COUNT` counts non-null values of *any* type; DAX `COUNT` errors on text.
- **`COUNTD` → `DISTINCTCOUNTNOBLANK`** — plain `DISTINCTCOUNT` counts BLANK as a value, which is off by one
  versus Tableau.

---

## The measure-context invariant (core safety rule)

The output is a DAX **measure**, so every leaf operand must be an **aggregation or a literal**. A bare
row-level field reference (e.g. `[Sales]` outside an aggregation) is invalid in a measure and **always
falls back**. This is enforced structurally: a `[field]` token can only appear inside an aggregation, so a
row-level reference is a parse error.

```text
SUM([Sales]) - [Discount]      → stub   (bare [Discount] is row-level)
SUM([Sales]) - SUM([Discount]) → DIVIDE-free arithmetic, translates
```

To get a row-level calc, the customer would author a **calculated column** upstream; this skill targets
measures.

---

## Static type checking

The parser tracks a data type per node — `number`, `text`, `date`, or `bool` — and falls back on any
mismatch, so it never emits DAX that would error or silently coerce:

- Arithmetic requires numeric operands; comparisons require two like, ordered/equatable types (never two
  booleans); `AND`/`OR`/`NOT` require booleans.
- `IF` / `IIF` / `IFNULL` branches must all return the **same** type.
- Scalar math functions (`ABS`, `ROUND`, `CEILING`, `FLOOR`, `POWER`, `SQRT`, `SIGN`, `EXP`, `LOG`, `LN`)
  require **numeric** operands, so a row-level field, a text/date operand, or wrong arity falls back.
- `CASE` → `SWITCH` needs **one** consistent result type across every `THEN`/`ELSE`; the simple form also
  requires each `WHEN` value to match the comparand's type. `CASE` is parsed like `IF` (it self-terminates
  at `END` and does not compose into surrounding arithmetic).
- Aggregates are rejected on the wrong column type (`SUM/AVG/MEDIAN` need numeric; `MIN/MAX` need
  numeric or date — `MIN/MAX` on a `dateTime` yields a `date`).

```text
IF SUM([Sales]) > 0 THEN SUM([Profit]) ELSE "n/a" END   → stub (number vs text branches)
IF SUM([Sales]) > 0 THEN SUM([Profit]) ELSE 0 END        → IF(SUM('Orders'[Sales]) > 0, SUM('Orders'[Profit]), 0)
```

---

## What falls back (stub, formula preserved)

LOD expressions (`{FIXED/INCLUDE/EXCLUDE}`), table calcs (`WINDOW_*`, `RUNNING_*`, `RANK`, `LOOKUP`,
`INDEX`), scalar date/string/regex functions, a row-level operand inside a scalar math function or `CASE`,
a 2-arg `LOG`, a `CASE` with mixed result types, nested arithmetic *inside* an aggregation,
4-arg `IIF`, references to other calcs, unresolved/ambiguous fields, and **cross-table** terms (a formula
whose fields span more than one model table) all return `None`.

> **Cross-table fallback is intentional.** Even when a relationship path exists, the DAX filter context is
> not guaranteed to reproduce Tableau's blended result, so those measures are stubbed rather than guessed.

---

## Known semantic difference: BLANK coercion

Emitted comparison/arithmetic operators follow DAX's BLANK coercion — an empty aggregation behaves as
`0`/`""`/`FALSE` in an operator — which differs from Tableau's three-valued NULL logic in the edge case of a
fully-empty aggregation. This matches the universal Tableau→DAX operator mapping that every comparable tool
uses. Such measures are flagged with a `TranslatedBy` annotation and are exactly what the
[validation-reconciliation](validation-reconciliation.md) step verifies against the real Tableau value.

---

## Output guardrail

Before a measure ships, `validate_dax(text)` checks the emit is structurally sound (balanced parentheses and
string quotes). The recursive-descent emitter already guarantees this; the guardrail backstops future edits.
It deliberately does **not** scan for keyword "leakage" (a legitimate column named `[END]` would
false-positive). A failing emit is downgraded to a stub.

---

## How the renderer uses the result

`tmdl_generate.generate_measure_tmdl(field_name, formula, dax=None)` does the right thing automatically:

- `dax` present → emits `measure '<name>' = <dax>` plus `annotation TranslatedBy` and
  `annotation TableauFormula = <original>`.
- `dax is None` → emits `measure '<name>' = 0` plus `annotation TableauFormula = <original>` only.

So every measure — translated or stubbed — carries its original Tableau formula for audit and repair.

---

## DAX quality alignment (delegated)

The translator already prefers `DIVIDE()` over `/` and fully qualifies every column as `'Table'[Column]`,
matching `semantic-model-authoring`'s
[dax-guidelines](../../semantic-model-authoring/references/dax-guidelines.md). After deploy, run that skill's
best-practice analysis on the translated measures so they pass DAX BPA out of the box.
