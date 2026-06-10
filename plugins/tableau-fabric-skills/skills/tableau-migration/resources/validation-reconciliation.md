# Validation & Reconciliation

How the skill proves a migrated semantic model is **correct**, not just deployed — by executing each
translated measure against the live Fabric model and comparing the result to the real Tableau value. This is
the orchestrator's **Final** phase. DAX execution is **delegated** to `semantic-model-consumption`
(`ExecuteQuery`); Tableau values come from the **VizQL Data Service** (VDS) the profiler already uses.

> **A measure is "verified" only when the numbers match.** Deployment success is not correctness. Without
> this step the migration ships measures that *look* right; with it, each one is proven equal to Tableau.

---

## Why this is the highest-value step

The calc → DAX translator is deterministic and type-checked, but two real-world gaps remain that only
*execution* can close:

1. **Semantic edge cases** — e.g. the documented DAX BLANK-coercion vs Tableau three-valued NULL difference
   (see [calc-to-dax.md](calc-to-dax.md)). Reconciliation catches any case where it actually changes a value.
2. **Connection / typing drift** — a column that landed or folded differently than expected shows up as a
   mismatch immediately.

This converts "we think the DAX is right" into "we proved it equals Tableau."

---

## The reconciliation loop

For each **translated** measure (those carrying a `TranslatedBy` annotation):

```text
1. Tableau value  ← VizQL Data Service: aggregate the source field the calc references
2. Fabric value   ← ExecuteQuery (semantic-model-consumption): EVALUATE ROW("v", [Measure])
3. Compare with a tolerance (exact for integer counts; relative epsilon for floats)
4. Record verified | mismatch | could-not-evaluate  → migration report
```

```dax
EVALUATE ROW("Profit Ratio", [Profit Ratio])
```

> Use the **same filter context** on both sides (e.g. a total, or the same single dimension value). Comparing
> a Tableau grand total against a DAX value computed under a different filter is a false mismatch, not a bug.

---

## What to reconcile

| Target | Reconcile? | Why |
|---|---|---|
| Translated measures (`TranslatedBy`) | **Yes** | The core trust check |
| Stub measures (`= 0`, formula preserved) | No (report only) | Known-incomplete by design; flag for manual repair |
| Row counts per table | **Yes** | Cheap, catches connection/landing problems early |
| Typed column min/max/null-rate | Optional | Surfaces typing or folding drift |

---

## Tolerances

- **Counts / `COUNTD`** — require exact equality.
- **Sums / averages / ratios (floats)** — compare with a small relative epsilon (floating-point and
  rounding differ across engines); a difference beyond epsilon is a real mismatch to investigate.
- **Empty vs zero** — an empty Tableau aggregation may read as BLANK in DAX; decide per measure whether
  BLANK ↔ 0 is acceptable, and note it in the report.

---

## Optional: validation-gated LLM fallback

For measures that currently fall back to a `= 0` stub, an agent — grounded by the preserved `TableauFormula`
annotation and `semantic-model-authoring`'s `dax-guidelines` — may *attempt* a DAX translation. **Accept it
only if this reconciliation passes**; otherwise keep the inert stub. This widens effective coverage without
weakening the deterministic core, but it is **opt-in** because it introduces an LLM into an otherwise
deterministic pipeline.

---

## Output

Every measure ends in one of three states — `verified`, `mismatch`, or `not-evaluated` — which feed directly
into [migration-report.md](migration-report.md). Mismatches are not failures of the migration so much as the
precise, auditable list of what a human needs to look at next.
