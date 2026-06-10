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
| `STDEV/STDEVP/VAR/VARP([field])` | `STDEV.S/STDEV.P/VAR.S/VAR.P('T'[Col])` | Tableau STDEV/VAR are the **sample** stats |
| `PERCENTILE([field], n)` | `PERCENTILE.INC('T'[Col], n)` | `n` is the 0..1 fraction |
| `DIV(a, b)` / `MOD(a, b)` | `QUOTIENT(a, b)` / `MOD(a, b)` | Integer division / modulo; numeric |
| Arithmetic `+ - * /`, unary `-`, parentheses | same, with `/` → `DIVIDE(...)` | Operands must be numeric |
| `IF c THEN a ELSEIF c2 THEN b ELSE z END` | nested `IF(c, a, IF(c2, b, z))` | No `ELSE` → 2-arg `IF` (BLANK when unmatched) |
| `IIF(cond, a, b)` | `IF(cond, a, b)` | 4-arg `IIF` is **not** supported |
| `CASE WHEN c THEN r … [ELSE z] END` | `SWITCH(TRUE(), c, r, …, z)` | Searched form; no `ELSE` → BLANK default |
| `CASE e WHEN v THEN r … [ELSE z] END` | `SWITCH(e, v, r, …, z)` | Simple form; `e` and values must be aggregated/literal |
| `ABS/SQRT/SIGN/EXP/LN(x)` | same name, `FN(x)` | `x` must be numeric |
| `SIN/COS/TAN/ASIN/ACOS/ATAN/COT(x)` | same name, `FN(x)` | Trig family; `x` numeric |
| `DEGREES(x)` / `RADIANS(x)` | `DEGREES(x)` / `RADIANS(x)` | Radian↔degree conversion; `x` numeric |
| `LOG(x)` / `LOG(x, base)` | `LOG(x)` / `LOG(x, base)` | 1-arg is base-10 |
| `ROUND(x)` / `ROUND(x, n)` | `ROUND(x, 0)` / `ROUND(x, n)` | Tableau 1-arg `ROUND` → 0 decimals |
| `CEILING(x)` / `FLOOR(x)` | `CEILING(x, 1)` / `FLOOR(x, 1)` | DAX requires a significance step |
| `POWER(x, n)` / `SQUARE(x)` | `POWER(x, n)` / `POWER(x, 2)` | DAX has no `SQUARE` |
| `PI()` | `PI()` | Nullary numeric constant |
| `= == <> != > >= < <=` | `=` / `<>` / `>` … | `==`→`=`, `!=`→`<>`; booleans are equatable (`=`/`<>`) but not ordered |
| `true` / `false` | `TRUE()` / `FALSE()` | Boolean literals; usable in `=`/`<>`, `IF`/`IIF`/`CASE` branches, `AND`/`OR` |
| `AND` / `OR` / `NOT(x)` | `&&` / `||` / `NOT(x)` | Operands must be boolean |
| `x IN (a, b, …)` | numeric/date: `x IN { a, b, … }`; text: `(EXACT(x, a) \|\| EXACT(x, b) …)` | Text uses case-sensitive `EXACT` (DAX set membership is collation/case-insensitive); one consistent element type |
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

To get a row-level calc, the customer would author a **calculated column** upstream. The skill also
provides a companion entry point for exactly that case — see the next section.

---

## Row-level calculated columns (companion path)

Some Tableau calcs are inherently **row-level** (string slicing, date parts, casts) and cannot be a
measure. For those, `translate_tableau_calc_to_column_dax(formula, resolver)` parses in *column context*
and returns the same `(dax, reason, tables_used)` triple, but for a **calculated column** instead of a
measure:

```python
from calc_to_dax import translate_tableau_calc_to_column_dax
dax, reason, tables_used = translate_tableau_calc_to_column_dax(formula, resolver)
```

In column context a bare `[field]` resolves to `'Table'[Col]` (the measure path falls back on it), and the
row-level pack becomes available. **Aggregations, `PERCENTILE`, and LOD expressions are invalid here and
fall back** (use the measure entry point for those). The two entry points are mirror images: a row-level
calc translates as a column but stubs as a measure, and an aggregation translates as a measure but stubs as
a column.

> **Binding contract.** When `tables_used` is a single `{T}`, the caller must materialize the expression as
> a calculated column **on table `T`**. Empty `tables_used` (e.g. `TODAY()`) → no field refs, bindable
> anywhere. More than one table → falls back (a row-level column cannot span tables). The actual binding is
> owned by the orchestrator/renderer; this function only emits the expression behind that clean seam.

| Tableau construct | DAX emitted | Notes |
|---|---|---|
| `[field]` (bare, row-level) | `'T'[Col]` | Resolves instead of falling back |
| `LEN/UPPER/LOWER(s)` | `LEN/UPPER/LOWER(s)` | `s` must be text |
| `LEFT/RIGHT(s, n)` | `LEFT/RIGHT(s, n)` | |
| `MID(s, start)` / `MID(s, start, len)` | `MID(s, start, LEN(s))` / `MID(s, start, len)` | 2-arg runs to end of string |
| `REPLACE(s, old, new)` | `SUBSTITUTE(s, old, new)` | Replaces all occurrences |
| `CONTAINS(s, sub)` | `CONTAINSSTRINGEXACT(s, sub)` | **Case-sensitive** (plain `CONTAINSSTRING` is not) |
| `STARTSWITH/ENDSWITH(s, sub)` | `EXACT(LEFT/RIGHT(s, LEN(sub)), sub)` | Case-sensitive prefix/suffix |
| `FIND(s, sub[, start])` | `FIND(sub, s, start, 0)` | Args reorder; case-sensitive; `0` = not found |
| `s1 + s2` (text) | `IF(ISBLANK(s1) \|\| ISBLANK(s2), BLANK(), s1 & s2)` | Tableau `+` propagates null, unlike `&` |
| `INT(x)` | `TRUNC(x)` | Truncates toward zero (DAX `INT` floors) |
| `FLOAT(x)` | `CONVERT(x, DOUBLE)` | |
| `YEAR/MONTH/DAY(d)` | `YEAR/MONTH/DAY(d)` | |
| `TODAY()` / `NOW()` | `TODAY()` / `NOW()` | |
| `DATEPART("part", d)` | `YEAR/MONTH/DAY/HOUR/MINUTE/SECOND/QUARTER(d)` | `week`/`weekday` fall back (start-of-week) |
| `DATEADD("part", n, d)` | `d + n` … / `EDATE(d, n) + MOD(d, 1)` | `EDATE` form keeps the time-of-day |
| `DATEDIFF("part", d1, d2)` | `DATEDIFF(d1, d2, UNIT)` | Args reorder; `week` falls back |
| `DATETRUNC("part", d)` | `DATE(YEAR(d), MONTH(d), 1)` etc. | `day`/`month`/`year`; `quarter`/`week` fall back |
| `DATE(d)` | `DATE(YEAR(d), MONTH(d), DAY(d))` | Strips the time component |
| `MAKEDATE(y, m, d)` | `DATE(y, m, d)` | Exact, culture-independent; operands must be numeric |

Row-level numeric math (`ABS`, `ROUND`, …) and the full conditional/logical grammar (`IF`, `CASE`,
comparisons, `AND`/`OR`/`NOT`, `ZN`/`IFNULL`/`ISNULL`) also work in column context over row-level fields.

**Deliberately left to fall back** (no faithful DAX equivalent): `TRIM`/`LTRIM`/`RTRIM` (DAX `TRIM` also
collapses internal whitespace), `SPLIT` (no general DAX form), `STR` and `DATE("...")` (culture-sensitive
formatting/parsing), the start-of-week-dependent `DATEPART("week"/"weekday")` / `DATEDIFF("week", …)`,
`DATENAME` (returns a localized part name), and the `MAKETIME`/`MAKEDATETIME` constructors (DAX `TIME` uses
a different epoch date and the multi-arg forms are version-dependent).

---

## Table calculations (addressing seam)

A Tableau **table calc** (`RUNNING_SUM`, `WINDOW_*`, `INDEX`, `LOOKUP`, …) depends on the worksheet's
*Compute-Using* — the partition (addressing) and sort — which lives in the `.twb`, **not** the `.tds`.
So `translate_tableau_table_calc_to_dax(formula, resolver, partition_by, order_by)` is a **seam**: the
caller passes the addressing explicitly and the function emits the modern-DAX window-function pattern.
The orchestrator/viz layer fills in `partition_by`/`order_by` once worksheets are parsed.

```python
from calc_to_dax import translate_tableau_table_calc_to_dax
# partition_by: list of field captions; order_by: captions or (caption, "ASC"|"DESC") pairs
dax, reason, tables_used = translate_tableau_table_calc_to_dax(formula, resolver, partition_by, order_by)
```

An **order spec is required** (the window functions omit their `<relation>` argument, which per the DAX
spec defaults to `ALLSELECTED()` of the `ORDERBY()`/`PARTITIONBY()` columns). The inner expression is
translated in **measure context**, so it must be an aggregation.

| Tableau table calc | DAX emitted (spec = `ORDERBY(…)[, PARTITIONBY(…)]`) | Notes |
|---|---|---|
| `INDEX()` | `ROWNUMBER(spec)` | 1-based row position in the partition |
| `RUNNING_SUM/AVG/MIN/MAX(agg)` | `SUMX/AVERAGEX/MINX/MAXX(WINDOW(1, ABS, 0, REL, spec), CALCULATE(agg))` | Partition start → current row |
| `WINDOW_SUM/AVG/MIN/MAX(agg)` | `…(WINDOW(1, ABS, -1, ABS, spec), CALCULATE(agg))` | Whole partition (first → last) |
| `LOOKUP(agg, offset)` | `CALCULATE(agg, OFFSET(offset, spec))` | Signed offset along the order |

`RANK`/`FIRST`/`LAST`/`PREVIOUS_VALUE` and other forms fall back for now. Cross-table terms (inner +
addressing spanning more than one table) fall back, consistent with the measure path. A missing order
spec falls back with a clear reason.

---

## Static type checking

The parser tracks a data type per node — `number`, `text`, `date`, or `bool` — and falls back on any
mismatch, so it never emits DAX that would error or silently coerce:

- Arithmetic requires numeric operands; comparisons require two like types. Booleans are **equatable**
  (`=` / `<>`, including against `true`/`false` literals) but **not ordered** (`<` `>` `<=` `>=` on a boolean
  falls back); `AND`/`OR`/`NOT` require booleans.
- `IF` / `IIF` / `IFNULL` branches must all return the **same** type.
- Scalar math functions (`ABS`, `ROUND`, `CEILING`, `FLOOR`, `POWER`, `SQUARE`, `SQRT`, `SIGN`, `EXP`,
  `LOG`, `LN`, `DIV`, `MOD`, `PI`, the `SIN`/`COS`/`TAN`/`ASIN`/`ACOS`/`ATAN`/`COT` trig family, and
  `DEGREES`/`RADIANS`) require **numeric** operands, so a row-level field, a text/date operand, or wrong
  arity falls back. `STDEV`, `STDEVP`, `VAR`, `VARP`, and `PERCENTILE` likewise require a numeric field.
- `x IN (a, b, …)` → DAX set membership requires every list element to share the operand's type; a
  boolean operand or a mixed-type list falls back. Numeric/date operands emit `x IN { a, b, … }`; **text**
  operands emit a parenthesised `(EXACT(x, a) || EXACT(x, b) …)` chain because DAX `IN { … }` follows the
  model's (usually case-insensitive) collation, whereas Tableau string comparison is case-sensitive.
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

LOD `INCLUDE`/`EXCLUDE` expressions, unsupported table calcs (`RANK`, `FIRST`/`LAST`, `PREVIOUS_VALUE`)
and table calcs with no addressing spec, `SPLIT`/`TRIM` and other non-faithful row functions, 4-arg
`IIF`, references to other calcs, unresolved/ambiguous fields, and **cross-table** terms (a formula whose
fields span more than one model table) all return `None`. (`{FIXED}` LODs and the supported table-calc and
row-level forms above do translate — via their respective entry points.)

> **Cross-table fallback is intentional.** Even when a relationship path exists, the DAX filter context is
> not guaranteed to reproduce Tableau's blended result, so those measures are stubbed rather than guessed.

### Qualified references `[A].[B]` (tokenized cleanly, fall back with a specific reason)

A dotted bracket reference — a Tableau parameter (`[Parameters].[X]`), a datasource-qualified field, or a
data-blend token (`[federated.<hash>].[field]`) — is tokenized as a **single** qualified reference rather than
choking on the `.`. None of these are modeled by the field-caption resolver yet, so they fall back with a
**specific** reason instead of a cryptic tokenizer error:

- `[Parameters].[X]` → `parameter reference [Parameters].[X] (unmodeled)`
- any other dotted ref → `qualified reference [A].[B] (unmodeled)`

This keeps the stub honest and lets the orchestrator model parameters / cross-source fields later and revisit.
(In measure context a formula like `IF [Region] = [Parameters].[P] …` may hit the bare row-level field
invariant on `[Region]` first — also a clean fallback.)

### Permanent fallbacks (out-of-engine — never translated)

A small set of constructs has **no in-engine DAX equivalent** and is always preserved-as-annotation + stub,
from both entry points: raw upstream SQL (`RAWSQL*`, `RAWSQLAGG*`), external R/Python service calls
(`SCRIPT_BOOL/INT/REAL/STR`), regular expressions (`REGEXP_MATCH/EXTRACT/REPLACE`; DAX has no regex engine),
session identity / security functions (`USERNAME`, `FULLNAME`, `ISMEMBEROF`), and spatial builders
(`MAKEPOINT`, `HEXBINX`, `HEXBINY`). These are the only categories that are out of scope by nature rather
than by caution — the guardrail test `test_out_of_engine_constructs_never_translate` pins the boundary.

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

## Live reconciliation targets (real Superstore)

The public triple `(dax, reason, tables_used)` is exactly the contract the
[validation-reconciliation](validation-reconciliation.md) step binds to. For the live **Superstore**
datasource (Azure SQL; `Orders` / `People` / `Returns`), the real calculated field
**Profit Ratio** = `SUM([Profit])/SUM([Sales])` translates to:

| Signal | Value | Used by reconciliation |
|---|---|---|
| `dax` | `DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))` | `ExecuteQuery`: `EVALUATE ROW("Profit Ratio", [Profit Ratio])` |
| `reason` | `ok` | translated-vs-stub status in the report |
| `tables_used` | `{Orders}` | which table the VizQL Data Service aggregates for the Tableau-side value |

Reconcile under the **same filter context** on both sides — e.g. the grand total `SUM(Profit)/SUM(Sales)`
over all rows — and compare with a small relative epsilon (it is a float ratio). This formula→DAX fact is
pinned as an offline fixture (`REAL_SUPERSTORE_MEASURES` in `tests/test_calc_to_dax.py`); the committed suite
stays fully offline and the integrator runs the single authoritative live pass post-merge. Append further
real calcs to that fixture as they are discovered — each is reconciled the same way.

---

## DAX quality alignment (delegated)

The translator already prefers `DIVIDE()` over `/` and fully qualifies every column as `'Table'[Column]`,
matching `semantic-model-authoring`'s dax-guidelines (when that peer skill is installed). After deploy, run that skill's
best-practice analysis on the translated measures so they pass DAX BPA out of the box.
