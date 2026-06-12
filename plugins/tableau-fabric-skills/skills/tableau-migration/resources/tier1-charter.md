# Tier-1 Charter — what the second compiler owns, and why

This is the **constitution** of the two-compiler architecture: the rule that decides whether a
Tableau construct is translated by the deterministic **Tier 0** engine
([`calc-to-dax.md`](calc-to-dax.md)) or handed to the **Tier-1** agent-as-second-compiler
([`second-compiler.md`](second-compiler.md)). The router
([`scripts/translation_router.py`](../scripts/translation_router.py)) implements the *labels* this
charter defines; this doc is the *why* behind them, so the boundary stays principled instead of
accreting ad-hoc exceptions.

> **One sentence:** Tier 0 owns everything that is **provably faithful AND fully determined by the
> `.tds`/`.twb`**; Tier 1 owns everything whose faithful DAX form needs an **intent the files don't
> carry**, or for which **no faithful native DAX form exists at all.**

---

## The two-axis decision

Every construct is routed by answering two independent questions.

- **Axis A — does a faithful deterministic DAX form EXIST?**
  Whether DAX *can* reproduce the Tableau value exactly (modulo documented cross-engine float
  rounding). This was settled empirically: each construct class was checked offline against an
  independent ground-truth (computed directly over the source data) versus the DAX evaluated on a
  live engine, accepting only matches to floating-point precision. It is **non-circular** evidence —
  the ground truth is not produced by the translator under test.

- **Axis B — can the intent be UNAMBIGUOUSLY recovered from the files?**
  Whether the partition / sort order / scope direction / outer re-aggregation needed to make the
  form well-defined is actually present in the `.tds` (datasource) or `.twb` (workbook) XML. A value
  that is only defined relative to a *worksheet's layout* fails axis B when that layout isn't
  carried.

| Axis A | Axis B | Route | Meaning |
|:---:|:---:|---|---|
| ✓ | ✓ | **Tier 0** | Faithful form exists and is fully determined → deterministic, by default. |
| ✓ | ✗ | **Tier 1** | Faithful DAX exists, but the agent must **supply the missing intent**, then emit the proven form. |
| ✗ | — | **Tier 1** | No faithful native DAX form → an **approximation** (or an honest stub). |

### Headline finding that shapes the whole design

**Axis A is almost never the blocker.** Across the construct classes probed, essentially all have a
faithful DAX form — window functions, the `*X` iterators, and `VAR/RETURN` closed forms are
expressive enough to reproduce nearly the entire Tableau calc surface *once the addressing is known*.
**The real boundary is Axis B (intent recovery) plus a small set of genuine DAX language gaps.**

That is the thesis of the second compiler: **it is an *intent-recovery* problem, not a
*DAX-expressiveness* problem.** The agent's job is rarely "invent clever DAX"; it is "recover the one
fact the files don't state, then emit the form Tier 0 would have emitted if it had known."

---

## The four reasons a construct is a Tier-1 member

Every Tier-1 case reduces to one of these. They map 1:1 onto the router categories in
[`translation_router.py`](../scripts/translation_router.py) (the names in `code font` are the stable
category strings).

1. **Missing addressing intent** — `missing_addressing_intent`
   A table calculation whose "Compute Using" (partition + order + across/down direction) lives in a
   worksheet, not the `.tds`. Axis A ✓, Axis B ✗. The agent recovers the addressing (from `.twb`
   context when available) and emits the already-proven windowed DAX.

2. **Missing outer aggregation** — `missing_outer_aggregation`
   An `INCLUDE` / `EXCLUDE` LOD (or a bare LOD needing an outer re-aggregate) whose result depends on
   the visual's dimensionality. Axis A ✓ for the inner aggregate, Axis B ✗ for the outer grain. The
   agent decides the intended grain and outer aggregation.

3. **DAX language gap** — `dax_language_gap`
   A construct with **no faithful native DAX form** (regex family, arbitrary-format `DATEPARSE`,
   general `SPLIT`, `FINDNTH`, …). Axis A ✗. Any agent output is an **approximation** — it must be
   flagged as such and **oracle-verified**, or the honest stub stays.

4. **Cross-cutting model objects** — `model_object_parameter`
   A Tableau **parameter** is a Power BI *model object* (calculation group / field parameter /
   what-if table), not a single expression. The agent models the object — reusing the deterministic
   emitters in [`parameters.py`](../scripts/parameters.py) — then rebinds the calc to it.

The remaining router categories are **convergence aids**, not true Tier-1 ownership:
`type_or_shape_mismatch` and `unresolved_reference` are usually fixed by a cast / a binding and then
**re-run through Tier 0** (often needing no bespoke DAX at all); `unsupported_other` is the honest
catch-all that still gets assessed for a faithful closed form.

---

## The boundary map by family

Routing for each construct family, distilled from the Axis-A / Axis-B evidence. "Pull-back" marks
forms that are oracle-certified Tier-0 candidates — the deterministic ceiling is higher than the
wiring, so these move *into* Tier 0 as the seam is extended (they are not permanent Tier-1 members).

### Table calculations

| Class | A | B | Route |
|---|:---:|:---:|---|
| `RUNNING_SUM/AVG/MIN/MAX`, `WINDOW_SUM/AVG/MIN/MAX`, `INDEX` | ✓ | ✓ | **Tier 0** (emitted) |
| `RUNNING_COUNT`, `WINDOW_COUNT/MEDIAN/STDEV/STDEVP/VAR/VARP`, `FIRST`/`LAST`/`SIZE` | ✓ | ✓ | **Tier 0** (pull-back as seam grows) |
| `WINDOW_PERCENTILE` / `PERCENTILE` | ✓ | ✓ | **Tier 0, guarded** (`PERCENTILEX.INC`; linear-interp note) |
| `LOOKUP(expr, ±k)` / `PREVIOUS_VALUE` (single addressing dim) | ✓ | ✓ | **Tier 0** (`OFFSET`) |
| `RANK` family, moving windows (relative `-k..+j` bounds) | ✓ | ✓ | **Tier 0** *(needs seam work — measure-ORDERBY / relative frame)* |
| Scope-relative addressing (Table/Pane/Cell, across/down) | ✓ | ✗ | **Tier 1** — direction is the missing intent |
| Order-sensitive calc with ≥2 addressing dims | ✓ | ✗ | **Tier 1** — slowest→fastest order not recoverable |
| Secondary / stacked table calc | ✓ | ✗ | **Tier 1** — second addressing pass not modeled |
| Sort-by-aggregate addressing | ✓ | ◐ | **Tier 1** (today; overlaps the RANK seam work) |

### Aggregates

| Class | A | B | Route |
|---|:---:|:---:|---|
| `SUM/AVG/MIN/MAX/COUNT`, `COUNTD`, `MEDIAN/STDEV/STDEVP/VAR/VARP` | ✓ | ✓ | **Tier 0** |
| `PERCENTILE(x, p)` | ✓ | ✓ | **Tier 0, guarded** (`PERCENTILE.INC`) |
| `CORR` / `COVAR` / `COVARP` | ✓ | ✓ | **Tier 0 candidate** — verbose `VAR/RETURN` closed form (no native DAX) |
| `ATTR(x)` | ✓ | ✓ | **Tier 0 candidate** — `IF(MIN=MAX, MIN)`; the `*` sentinel needs a typing decision |

### Level-of-detail (LOD)

| Class | A | B | Route |
|---|:---:|:---:|---|
| `FIXED [dims] : agg`, `FIXED : agg` (grand total) | ✓ | ✓ | **Tier 0** (`CALCULATE` + `ALLEXCEPT`/`ALL`) |
| Nested `FIXED` (both fixed) | ✓ | ✓ | **Tier 0 candidate** — composable; verify the parser nests |
| `INCLUDE [dims] : agg`, `EXCLUDE [dims] : agg` | ✓ (inner) | ✗ | **Tier 1** — outer re-aggregation depends on the view |

### Logical / String / Date / Type

| Class | A | Route |
|---|:---:|---|
| `IF`/`CASE`/`IIF`/logical ops; `UPPER/LOWER/TRIM/LEFT/RIGHT/MID/LEN/REPLACE/CONTAINS/…` | ✓ | **Tier 0** |
| `REGEXP_MATCH/EXTRACT/EXTRACT_NTH/REPLACE` | ✗ | **Tier 1** — DAX has no regex engine |
| `DATEPARSE(fmt, str)` (arbitrary format), general `SPLIT(str, delim, n)` | ✗ / ◐ | **Tier 1** — only fixed-format / `PATHITEM` special-cases are deterministic |
| `MAKEPOINT` / spatial / `RAWSQL` / `SCRIPT_*` / `MODEL_*` | ✗ | **Out of scope** (declared up front) |

### Parameters — cross-cutting model objects (all Tier 1)

| Tableau usage | Power BI analog | Today's emitter |
|---|---|---|
| Dimension swap | **Field parameter** | `emit_field_parameters` (`detect_field_swap`) |
| What-if value | **Numeric-range (what-if) parameter** | `emit_value_parameters` → `GENERATESERIES` table + `SELECTEDVALUE` measure + `param_resolver` |
| Measure swap | **Field parameter today** (a **calculation group** is the richer alternative) | `emit_field_parameters`, with a caveat warning that a measure swap uses each field's default aggregation |

> The measure-swap → **calculation group** upgrade is a deliberately-deferred enhancement, **not a
> bug**: field parameters are faithful for additive measures and carry an explicit caveat for
> non-additive ones; the calc group is the modeling-richer form to add when warranted.

---

## How a Tier-1 member flows through the system

The charter only *classifies*; these components *act* (full contract in
[`second-compiler.md`](second-compiler.md)):

1. **Tier 0** keeps the inert stub + an honest `fallback_reason` (never force-fit DAX).
2. **Router** — `classify_fallback(reason, role, fields)` → one charter category + guidance.
3. **Handoff manifest** — `translation_handoff_artifact` (in
   [`assemble_model.py`](../scripts/assemble_model.py)) packages each fallback into a structured
   request (formula, typed fields, target grain, category, guidance).
4. **Agent** supplies the missing intent for the category and authors the **leanest faithful**
   candidate (the leanness ladder in `second-compiler.md`).
5. **Validation gate (always):** `check_candidate_dax` — balanced delimiters, not an inert stub, no
   leftover Tableau idioms.
6. **Reconciliation oracle (when data is landed):**
   [`translation_reconcile.py`](../scripts/translation_reconcile.py) evaluates the candidate against
   the live model and compares to the Tableau value at a fixed grain within tolerance (see
   [`validation-reconciliation.md`](validation-reconciliation.md)). For a `dax_language_gap`
   approximation this match is **mandatory** before landing.
7. **Landing** — only a human-approved candidate flips live via `approved_calc_dax` (the `!` prefix
   marks "not from the trusted deterministic path").

---

## Charter invariants (binding on all tiers)

1. **Faithful-or-stub.** Anything correct only in a narrow context is a stub or an approval-gated
   suggestion — never silent live DAX.
2. **Tier 0 is untouched by Tier 1.** The second compiler only adds approval-gated candidates; it
   never changes the deterministic output or its guarantees.
3. **A default run adds ZERO live assisted objects.** Candidates surface as suggestions and go live
   only on explicit `approved_calc_dax`.
4. **Provenance is the source of truth.** Always preserve `TableauFormula`; stamp `TranslatedBy`; the
   `!` prefix is a derived display signal, not the only record.
5. **The boundary is evidence-driven.** A construct moves between tiers only on Axis-A/Axis-B
   grounds — a new faithful form (with oracle proof) is a Tier-0 pull-back; a newly-recoverable
   intent retires a Tier-1 member. Convenience is never a reason to cross the line.
