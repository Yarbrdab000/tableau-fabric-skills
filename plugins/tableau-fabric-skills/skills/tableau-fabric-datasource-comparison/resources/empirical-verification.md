# Empirical verification (`--verify`) — the windowed-overlap model

The deterministic engine (`compare.py`) decides *"same dataset?"* from **structure** — name, columns,
types, lineage. That is strong, but it never looks at the **values**, so it can be fooled by a
same-shape / different-data pair, and its confidence is an uncalibrated score rather than evidence.

`--verify` adds an **opt-in, advisory, aggregate-only** Tier-2 layer that probes the *data* on both
sides and checks it lines up — promoting a match from *"looks the same"* to *"the data agrees,"* and
surfacing the false positives a human would otherwise have to hunt for. It is **additive**: it only
attaches `match["verification"]` (+ a short `match["verification_note"]`) and a
`summary["verification"]` rollup. The deterministic tier / score / bucket are never changed.

## Why naive equality is wrong

A Fabric model holding **2019–2026** and a Tableau datasource holding **2021–2026** are the **same**
source — Fabric simply carries more history. Yet `SUM(Sales)`, `COUNT`, and `DISTINCTCOUNT` over the
*whole* of each side legitimately differ. Treating that as a "mismatch" would tell a migration team to
**rebuild something they should reuse** — the exact opposite of this skill's purpose. Unbounded totals
are **not invariant** under subset/superset, so they cannot be compared as equality.

## What we do instead: windowed-overlap agreement

1. **Establish each side's range.** `MIN` / `MAX` a shared **date** (preferred) or **numeric** key
   column on both sides. From the four bounds, compute the **common overlap window** and classify the
   relationship:

   | Relationship | Meaning |
   |---|---|
   | `equal`    | identical ranges |
   | `subset`   | Tableau's range sits inside Fabric's → **Fabric is the superset** (extra history) |
   | `superset` | Tableau's range contains Fabric's |
   | `partial`  | the ranges overlap but neither contains the other |
   | `disjoint` | the ranges do **not** overlap at all |

2. **Compare equality probes only inside the overlap.** Windowed `SUM` (numeric measures) and
   `DISTINCTCOUNT` (any shared column) on both sides, filtered to the overlap window. On that shared
   slice the **same dataset agrees within tolerance** regardless of how much extra history either side
   carries. `MIN` / `MAX` only *establish* the window — they are never pass/fail equality checks.

3. **Verdict semantics.** "One side is a superset" is a **PASS**, not a mismatch:

   | Verdict | When | Note |
   |---|---|---|
   | `verified`     | the overlap probes agree (relationship may be equal/subset/superset) | the data lines up on the shared window |
   | `compatible`   | no shared time/key column to window on, but the raw totals are consistent with one side being a superset | "same data, different volume" — never a hard fail from magnitude alone |
   | `mismatch`     | the overlap genuinely disagrees, **or** the ranges are `disjoint` | advisory — a human should confirm before reuse |
   | `inconclusive` | nothing comparable ran (VDS disabled, no probeable column, transport error) | no evidence either way |

4. **No shared time/key column?** Fall back to a conservative **containment** read of the *unwindowed*
   totals: exactly equal → `verified`; every difference consistent with a single direction (one side
   always ≥ the other) → `compatible`; otherwise → `inconclusive`. Containment mode **never** emits a
   hard `mismatch` from magnitude alone, because without a window we cannot tell "different data" from
   "more data".

## Function map (Tableau VDS ↔ Fabric DAX)

| Probe | Tableau VizQL Data Service | Fabric `executeQueries` (DAX) |
|---|---|---|
| min | `MIN` | `MIN('Table'[Col])` |
| max | `MAX` | `MAX('Table'[Col])` |
| sum | `SUM` | `SUM('Table'[Col])` |
| distinct count | `COUNTD` | `DISTINCTCOUNT('Table'[Col])` |

Windowing is applied as a **range filter**: VDS uses a `QUANTITATIVE_DATE` / `QUANTITATIVE_NUMERICAL`
`RANGE` filter (`minDate`/`maxDate` or `min`/`max`); DAX wraps the aggregate in
`CALCULATE(<agg>, 'Table'[Col] >= … && 'Table'[Col] <= …)`. The VDS has **no `COUNT(*)`**, so row-count
is not used as a hard signal — per-column agreement carries the verdict.

## Transport, tokens, and limits

- **Tableau VDS** — `POST {server}/api/v1/vizql-data-service/query-datasource`. A `404` means VDS is
  disabled or the server predates it (Tableau < 2025.1) → the probe degrades to *inconclusive*; `429`
  → rate-limited → inconclusive. Requires **live** Tableau (`--tableau-live`); a cached
  `--tableau-inventory-json` cannot be probed and `--verify` reports *skipped*.
- **Fabric `executeQueries`** — `POST {powerbi}/v1.0/myorg/groups/{ws}/datasets/{id}/executeQueries`.
  This needs a **second token** whose audience is `https://analysis.windows.net/powerbi/api`
  (**distinct** from the Fabric API token). Provide it with `--powerbi-token`, `POWERBI_TOKEN`, or let
  `--use-az` mint it. A `401`/`403`/paused-capacity/disabled-XMLA response degrades that probe to
  *inconclusive* rather than failing the run.
- **Bounded by construction** — only the top `--verify-top-n` confident/partial matches are probed,
  each capped at `--verify-max-cols` shared columns and a per-pair probe budget; numeric agreement uses
  a relative tolerance (`--verify-rtol`, default `0.01`) to absorb extract drift and float inexactness.

## Guarantees

- **Read-only and aggregate-only.** Every probe is a single scalar aggregate (`EVALUATE ROW("v", …)`
  on the Fabric side; one `fieldAlias` on the Tableau side). **No row-level data** leaves either
  platform.
- **Advisory and additive.** Verification never overrides the deterministic verdict; a `mismatch` is a
  flag for human review, not a re-bucketing. Report keys are added, never renamed or removed.
- **Offline-testable.** The verdict logic is pure and takes the two probes as injected callables, so
  the whole windowed-overlap model is unit-tested with no network (`tests/test_verify.py`).
