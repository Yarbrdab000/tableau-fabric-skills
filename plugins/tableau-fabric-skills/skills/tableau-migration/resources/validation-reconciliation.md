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

## The deterministic core — `scripts/translation_reconcile.py`

The comparison logic above is implemented as a pure, dependency-free, never-raises module so it can be
unit-tested with no network. **Backends are injected, not embedded:** you pass a
`fabric_oracle(dax_query) -> result` callable (the real one wraps `semantic-model-consumption`'s
`ExecuteQuery` / Power BI `executeQueries`) and the Tableau ground truth (`tableau_value=` or a
`tableau_oracle()` callable wrapping VizQL Data Service). Nothing runs automatically — the orchestrator
or agent calls it explicitly in the Final phase.

```python
from translation_reconcile import reconcile, reconcile_all

rec = reconcile("Profit Ratio",
                "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
                fabric_oracle=run_dax,            # query -> result
                tableau_value=0.12564,            # VizQL ground truth at this grain
                grain_filters=None)               # e.g. ["'Orders'[Region] = \"West\""]
# rec["state"] in {"verified", "mismatch", "not-evaluated"}
```

What the module guarantees, matching the policy above:

- It runs `translation_router.check_candidate_dax` **first** and refuses to send a candidate that
  fails the syntactic gate to the backend (`not-evaluated`, never a malformed query).
- It builds the `EVALUATE ROW("value", …)` probe, wrapping the expression in `CALCULATE(…, <filters>)`
  when `grain_filters` fix the grain so both sides share one filter context.
- It reads the scalar out of a bare value, a `{"value": …}`/`{"rows": […]}` shape, **or** the real
  `executeQueries` envelope, then applies the tolerances (exact for `kind` in
  count/countd/integer; relative epsilon otherwise; the `blank_equals_zero` policy, recorded in
  `detail`).
- A missing oracle, an oracle that throws, an unreadable result, or no ground truth all become a
  `not-evaluated` record with a reason — it never raises.

`reconcile_request(request, dax, …)` is the convenience that takes a `translation_handoff` request
(pulls `name`, infers an exact/float `kind` from the formula, and marks a `dax_language_gap` record
`approximation=True` — the category for which an oracle match is *mandatory* before landing).
`reconcile_all(items, …)` reconciles a batch and returns `{"records", "summary"}` for the report.

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
