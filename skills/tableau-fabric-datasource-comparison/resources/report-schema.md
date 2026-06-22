# Report schema

`compare_estate.py --format json` emits the object returned by `compare.compare_inventories()`. The
Markdown report (`--format md`) is a rendering of the same data. **Schema discipline: additive only** —
new keys/artifacts may be added, but existing keys are never renamed or removed.

## Top level

```json
{ "summary": { ... }, "matches": [ ... ], "adjudication": { ... } }
```

`adjudication` is **additive and always present** — the LLM-optional review queue (see
[`llm-adjudication.md`](llm-adjudication.md)). After `--apply-adjudication`, two more additive keys
appear: each reviewed `matches[]` gains an `agent_review`, and a top-level `adjudicated_summary` is
added. The deterministic `summary` / `tier` / `score` / `bucket` are never modified.

## `summary`

| Key | Type | Meaning |
|---|---|---|
| `tableau_total` | int | number of Tableau datasources compared |
| `fabric_total` | int | number of Fabric semantic models considered |
| `by_tier` | object | count per tier: `{Exact, Strong, Partial, Weak, None}` |
| `already_exist` | int | datasources in the `already_exists` bucket (Exact+Strong) |
| `partial` | int | datasources in the `partial` bucket (Partial) |
| `rebuild` | int | datasources in the `rebuild` bucket (Weak+None) |
| `weights` | object | the signal weights used (`name/column/type/source`) |
| `bands` | array | the `[label, min_score]` band table used |
| `by_priority` | object | count per usage label: `{High, Medium, Low, Unused, Unknown}` (additive — see [`migration-priority.md`](migration-priority.md)) |
| `by_migration_priority` | object | count per fused action: `{P1…, P2…, P3…, P4…, Reuse…, Unprioritized}` |
| `usage_thresholds` | object | the workbook-count thresholds used (`{high, medium}`) |
| `distinct_fabric_matched` | int | count of **distinct** Fabric models backing the `already_exists` bucket (additive — counting correctness) |
| `contested_models` | array | Fabric models claimed as best by more than one Tableau datasource: `[{fabric_name, workspace, claimed_by[]}]` (additive) |
| `assignment` | object | greedy **one-to-one** estate sizing (each model claimed once): `{by_tier, already_exist, partial, rebuild}` (additive) |
| `fabric_coverage` | object | reverse (Fabric→Tableau) coverage: `{fabric_total, matched_models, unmatched_models, unmatched_model_names:[{fabric_name, workspace}]}` (additive) |
| `logic_parity` | object | business-logic-parity rollup (additive): `{none, likely, partial, unverified, review_needed}` — counts of matched datasources by [logic-parity](#logic-parity-calculated-fields--measures) status, plus `review_needed` = matches that look already-in-Fabric/partial **but whose calculated fields are not confirmed as measures** |
| `confidence` | object | verdict-confidence rollup (additive): `{high, medium, low, high_confidence_already_exists, low_confidence_review}` — how trustworthy each **verdict** is once the independent signals are fused. `low_confidence_review` counts `already_exists`/`partial` verdicts that landed **Low** (a human should look). See [confidence](#verdict-confidence) |

## `matches[]` (sorted most-comparable first)

| Key | Type | Meaning |
|---|---|---|
| `tableau_name` | string | the Tableau datasource name |
| `project` | string | its Tableau project |
| `tableau_luid` | string | its Tableau LUID |
| `tier` | string | `Exact / Strong / Partial / Weak / None` |
| `score` | float | best score `0..1` |
| `bucket` | string | `already_exists` / `partial` / `rebuild` |
| `source_compared` | bool | `false` when the physical source was obscured on either side (then the source sub-score is `n/a`) |
| `usage` | object \| null | downstream impact: `{workbook_count, sheet_count, dashboard_count, source}` (additive — `source` is `metadata`/`rest`/`none`; counts are `null` when not gathered) |
| `priority` | string | usage label `High / Medium / Low / Unused / Unknown` (additive) |
| `migration_priority` | string | fused action `P1 - migrate first` … `P4 - retire candidate` / `Reuse (already in Fabric)` / `Unprioritized` (additive) |
| `contested` | bool | this match's best Fabric model is also another datasource's best match (additive — counting correctness) |
| `contested_with` | array | the other Tableau datasource names that also picked this model (additive) |
| `assigned_match` | object \| null | the candidate this datasource holds under the one-to-one assignment (a candidate object, or `null` if it lost every contested model) (additive) |
| `assigned_tier` | string | the tier of `assigned_match` (`Exact … None`) (additive) |
| `reason` | string | deterministic one-line explanation of the verdict (name/column/source drivers + contested flag) (additive) |
| `best_match` | object \| null | the winning Fabric candidate (null when nothing scored above 0) |
| `candidates` | array | up to `--top-n` candidates (incl. the best), each a candidate object |
| `logic_parity` | object \| null | business-logic-parity for this match (additive; `null` when there is no Fabric candidate): `{status, tableau_calc_count, fabric_measure_count, matched, unmatched[]}` where `status` is `none` / `likely` / `partial` / `unverified`. Name-level only — see [logic-parity](#logic-parity-calculated-fields--measures) |
| `confidence` | object | verdict confidence for this match (additive): `{level, drivers[], cautions[], margin, corroborating_signals, reciprocal_best}` where `level` is `High` / `Medium` / `Low`. Read-only over the verdict — never changes `tier`/`score`/`bucket`. See [confidence](#verdict-confidence) |

### candidate object (`best_match` and each `candidates[]`)

| Key | Type | Meaning |
|---|---|---|
| `fabric_name` | string | the semantic model name |
| `workspace` | string | its workspace name |
| `workspace_id` | string | its workspace id |
| `fabric_id` | string | the semantic model id |
| `score` | float | this candidate's score `0..1` |
| `signals` | object | `{name, column, type, source}` sub-scores; `source` is `null` when not comparable |
| `source_compared` | bool | whether the source signal was measured for this candidate |
| `source_coverage` | float \| null | containment of the datasource's upstream tables in this model: `|tab ∩ fab| / |tab|`; `null` when the source was not comparable (additive) |
| `shared_tables` | array | normalised names of the upstream source tables shared by both sides (drives the source rationale; `[]` when none/obscured) (additive) |
| `shared_column_count` | int | number of columns that overlap by normalised name |

## Example

```json
{
  "summary": {
    "tableau_total": 6, "fabric_total": 6,
    "by_tier": {"Exact": 1, "Strong": 5, "Partial": 0, "Weak": 0, "None": 0},
    "already_exist": 6, "partial": 0, "rebuild": 0,
    "distinct_fabric_matched": 6,
    "contested_models": [],
    "assignment": {"by_tier": {"Exact": 1, "Strong": 5, "Partial": 0, "Weak": 0, "None": 0},
                   "already_exist": 6, "partial": 0, "rebuild": 0},
    "fabric_coverage": {"fabric_total": 6, "matched_models": 6, "unmatched_models": 0,
                        "unmatched_model_names": []},
    "weights": {"name": 0.2, "column": 0.35, "type": 0.15, "source": 0.3},
    "bands": [["Exact", 0.85], ["Strong", 0.65], ["Partial", 0.4], ["Weak", 0.15], ["None", 0.0]]
  },
  "matches": [
    {
      "tableau_name": "Azure SQL - Superstore", "project": "default", "tableau_luid": "....",
      "tier": "Strong", "score": 0.83, "bucket": "already_exists", "source_compared": true,
      "contested": false, "contested_with": [], "assigned_tier": "Strong",
      "reason": "exact name; 64% weighted column overlap; shared physical source -- Strong.",
      "best_match": {
        "fabric_name": "Azure SQL - Superstore", "workspace": "Github-Testing-Workspace",
        "workspace_id": "....", "fabric_id": "....", "score": 0.83,
        "signals": {"name": 1.0, "column": 0.64, "type": 1.0, "source": 0.85},
        "source_compared": true, "source_coverage": 1.0, "shared_tables": ["orders", "returns"],
        "shared_column_count": 18
      },
      "candidates": [ "..." ]
    }
  ]
}
```

## Consuming the rollup

- Feed the **`rebuild`** bucket (`matches[].bucket == "rebuild"`) to the `tableau-migration` skill.
- Treat **`already_exists`** as a reuse/verify list — confirm the candidate before retiring the Tableau
  datasource.
- **`partial`** needs human reconciliation (added/renamed columns, source drift) before reuse.
- Order the rebuild work by **`matches[].migration_priority`** — see below.

## Counting correctness (distinct / one-to-one / reverse coverage)

The greedy per-datasource verdict (`tier` / `bucket`) lets several datasources claim the same model,
which can over-count a naive estate total. These additive keys make the count trustworthy without
changing the per-datasource verdict — full method in
[`comparison-methodology.md`](comparison-methodology.md):

- `summary.distinct_fabric_matched` vs `summary.already_exist` — distinct models vs datasource count.
  When they differ, models are shared; `summary.contested_models` (and per-match `contested` /
  `contested_with`) names which.
- `summary.assignment` — the **one-to-one** estate sizing (each model claimed once). Use this for a
  "how many must we still build" total that does not double-count a shared model; `assigned_match` /
  `assigned_tier` show each datasource's assigned model.
- `summary.fabric_coverage.unmatched_model_names` — Fabric models nothing in Tableau maps to (net-new
  in Fabric), so the estate view is bidirectional.

## Migration priority (downstream-impact ranking)

The comparison answers *"does it already exist in Fabric?"*; the migration-priority signal answers
*"which rebuilds matter, and in what order?"*. Each datasource's downstream **usage** (attached
workbooks plus the sheets/dashboards built on it) is gathered by `tableau_inventory.py` — the Tableau
**Metadata API** is the trusted primary source, with a thin REST workbook-connection fallback for any
datasource Catalog has not indexed yet (`--usage {auto,metadata,rest,off}`). Full method in
[`migration-priority.md`](migration-priority.md).

- `usage` rides along on each `matches[]` row; `priority` bands it (`High ≥ usage_thresholds.high`
  workbooks, `Medium ≥ usage_thresholds.medium`, `Low = 1`, `Unused = 0`, `Unknown` = not gathered).
- `migration_priority` fuses bucket + usage: `already_exists` → `Reuse (already in Fabric)`; otherwise
  `High→P1`, `Medium→P2`, `Low→P3 (deprioritize)`, `Unused→P4 (retire candidate)`, `Unknown→Unprioritized`.
  A datasource with **0–1 attached workbook** is deprioritized even if it needs a full rebuild.
- These keys are **always present** (annotation runs unconditionally); when usage was not gathered
  everything is `Unknown` / `Unprioritized` and the Markdown priority section is omitted.

## `adjudication` (LLM-optional review queue)

The deterministic verdict is authoritative, but a structural matcher is blind to **semantic**
equivalence (renamed columns, a renamed asset, a lakehouse mirror, or a coincidental overlap of
generic column names). `adjudication` is the additive handoff that routes the uncertain tail to an
agent acting as a "second matcher" — full contract in [`llm-adjudication.md`](llm-adjudication.md).

| Key | Type | Meaning |
|---|---|---|
| `summary.total_reviewed` | int | datasources flagged for agent review |
| `summary.auto_confident` | int | datasources the deterministic matcher is confident about (no review) |
| `summary.categories` | object | count per uncertainty category |
| `needs_review[]` | array | concise `{tableau_name, tier, score, category, deterministic_bucket}` list |
| `requests[]` | array | one structured record per reviewed datasource (below) |

Each `requests[]` record carries `category`, a `category_guidance` string, a `deterministic` block
(the Tier-0 verdict), the Tableau side's typed `tableau_columns` + `tableau_sources`, and the top-K
Fabric `candidates` — **each enriched** with its own `columns`, `tables`, and `sources` — so the
agent can adjudicate without re-pulling either inventory. Categories: `near_tie`,
`renamed_columns_suspected`, `obscured_source`, `borderline_band`, `likely_rebuild`.

### After `--apply-adjudication` (advisory, additive)

- Each reviewed `matches[]` gains `agent_review`: `{verdict, fabric_id, confidence, rationale,
  adjudicated_bucket}` where `verdict` is `match` / `partial` / `no-match`.
- A top-level `adjudicated_summary` is added: `{reviews_applied, already_exist, partial, rebuild,
  delta:{…}}` — the rollup **after** semantic review, with the delta versus the deterministic count.
- The deterministic `summary` and each row's `tier` / `score` / `bucket` are **unchanged**; the two
  rollups sit side by side.

## `verification` (empirical `--verify`, opt-in/advisory)

When `--verify` runs, the top confident/partial matches are probed on both sides and checked on their
**overlapping data window** (so a Fabric superset still verifies). All keys are **additive**; the
deterministic tier / score / bucket are never changed. Full model in
[`empirical-verification.md`](empirical-verification.md).

| Key | Type | Meaning |
|---|---|---|
| `summary.verification.enabled` | bool | `true` when probes ran; `false` + `reason` when skipped (e.g. cached Tableau, no Power BI token) |
| `summary.verification.attempted` | int | matches verification was attempted on |
| `summary.verification.verified` | int | overlap probes agreed (relationship may be equal/subset/superset) |
| `summary.verification.compatible` | int | no shared window column, but raw totals consistent with one-side-superset |
| `summary.verification.mismatch` | int | overlap disagreed, or ranges were disjoint (advisory flag) |
| `summary.verification.inconclusive` | int | nothing comparable ran |
| `summary.verification.fabric_no_data` | int | matches where Fabric returned **no rows** (model not yet refreshed) while Tableau had data — actionable, **not** a mismatch |
| `summary.verification.fabric_unreadable` | int | matches where **every** Fabric probe errored (capacity paused / DirectQuery source not configured) while Tableau had data — actionable, **not** a mismatch |
| `summary.verification.probes_run` | int | total aggregate probes issued across both sides |
| `summary.verification.top_n` / `max_cols` / `rtol` | scalar | the run's bounds/tolerance |

Each verified `matches[]` row gains:

- `verification` — `{verdict, method, relationship, reason_code, window_column, range, probes_run,
  probes_agreed, probes_disagreed, probes_inconclusive, agreement, probes:[…], notes:[…]}`. `verdict`
  is `verified` / `compatible` / `mismatch` / `inconclusive`; `method` is `windowed` or `containment`;
  `relationship` is `equal` / `subset` / `superset` / `partial` / `disjoint`; each `probes[]` entry is
  `{column, function, tableau, fabric, windowed, outcome}`. `reason_code` is `null` for a normal
  verdict, or — when an `inconclusive` is purely because the Fabric model returned nothing while
  Tableau returned data — `fabric_no_data` (Fabric held no rows / not refreshed) or `fabric_unreadable`
  (every Fabric probe errored: paused capacity / source connection not configured). Both are
  **data-state conditions, never a mismatch** — the schema/lineage match still stands.
- `verification_note` — a one-line human summary (e.g. *"empirically verified (2/2 overlap probes
  agree; Fabric is a superset)"*, *"VERIFY MISMATCH — SUM(sales) on overlap: …"*, or *"inconclusive —
  Fabric model holds no data (not yet refreshed); refresh the semantic model in Fabric, then re-run
  --verify"*).

Clear `rebuild` matches are skipped (nothing to verify); the deterministic verdict is authoritative.

## Logic parity (calculated fields → measures)

Structural matching compares **columns, types and physical sources** — it says nothing about whether
a datasource's **calculated fields** were re-expressed as Fabric **measures**. `logic_parity` is a
deliberately conservative, **name-level** signal (Tableau calc names vs model measure names) that
flags the dangerous case where the columns line up yet the business logic almost certainly did not
come across, so a structural *"already exists"* is never mistaken for *"safe to retire"*. It does
**not** compare formulas — proving a Tableau calc equals a DAX measure is the `tableau-migration`
translator's job. All keys are **additive**; the deterministic tier / score / bucket are unchanged.

`matches[].logic_parity.status` (and the `summary.logic_parity` rollup):

| Status | Meaning |
|---|---|
| `none` | the datasource has no calculated fields — nothing to verify |
| `likely` | every calc name has a same-named measure — logic probably carried over |
| `partial` | some calc names line up, some do not |
| `unverified` | calcs exist but the model exposes no measures, or none line up by name — the calculations likely still need to be rebuilt |

`summary.logic_parity.review_needed` is the headline risk: matches in the `already_exists` / `partial`
bucket whose status is `partial` or `unverified`. The Markdown report renders a **Business-logic
parity** section (with this callout and a per-datasource table) only when at least one matched
datasource carries calculated fields; otherwise the report is byte-for-byte unchanged.

> Inputs: the Tableau side flags `fields[].is_calculated` (Metadata-API `__typename == "CalculatedField"`,
> or a `<calculation>` child in the `.tds` fallback); the Fabric side carries model-level `measures`
> (names) parsed from TMDL.

## Verdict confidence

Tier and score answer *"what is the best match?"*. **Confidence** answers a different, decision-grade
question — *"how much should the customer trust this line in the migration plan?"* — and it does so
for **both** sides of the verdict: a `High` on an `already_exists` verdict means *confidently reuse*;
a `High` on a `rebuild` verdict means *confidently rebuild* (nothing in Fabric comes close). It is
**deterministic, additive and read-only**: it never changes a `tier` / `score` / `bucket`.

Confidence fuses the independent evidence the engine already computed — each an *independent*
corroborator, so agreement compounds:

- **score level** — how strong the absolute match is (the band it lands in);
- **margin over the runner-up** — decisive win vs. a coin-flip near-tie;
- **signal corroboration** — how many of name / column / physical source *independently* support it
  (a verdict resting on a single signal is weaker than three signals agreeing);
- **reciprocity** — a *mutual best* match on a **contested** model (the model's strongest suitor is
  this very datasource). Trivial reciprocity on an uncontested model does not count;
- **empirical verification** — when `--verify` ran, a `verified` / `compatible` lifts confidence; a
  `mismatch` caps it at `Low` regardless of structure.

| `level` | `already_exists` / `partial` | `rebuild` |
|---|---|---|
| `High` | empirically verified, or ≥2 independent signals agree with no near-tie/contested model | no comparable model at all, or score at/below the Weak floor |
| `Medium` | one signal supports it (or a clean partial) | a clear non-match in the mid-range |
| `Low` | rests on a single signal / near-tie / contested model / empirical mismatch | **borderline** — score sits just below the partial threshold (might be a real partial) |

`matches[].confidence` carries `{level, drivers[], cautions[], margin, corroborating_signals,
reciprocal_best}`; `drivers[]` are the human-readable reasons it is trusted, `cautions[]` the reasons
it is not. `summary.confidence.low_confidence_review` is the headline action item — `already_exists` /
`partial` verdicts that landed `Low`. The Markdown report renders a **Verdict confidence** headline
near the top and, when any verdict is `Low`, a **Lowest-confidence verdicts (review these first)**
table; both are omitted when confidence was not synthesised.

## Executive export (`--export-csv` / `--export-xlsx`)

These flags render the **same** finished report (whatever layers ran — verification, adjudication,
logic-parity) into share-ready artifacts. They are **read-only over the report and purely additive** —
they never alter any key above. Standard-library only (the `.xlsx` is hand-assembled OOXML; no
`openpyxl` / `pandas`).

**CSV** (`--export-csv`) — one rectangular table, one row per Tableau datasource (the analyst pivot
source), written UTF-8 **with BOM** so Excel auto-detects the encoding. **XLSX** (`--export-xlsx`) — a
three-sheet workbook (`Summary`, `Datasources`, `Fabric coverage`). The CSV and the workbook's
`Datasources` sheet share these columns, in this order:

| Column | Source key | Notes |
|---|---|---|
| `Tableau datasource` | `matches[].tableau_name` | |
| `Project` | `matches[].project` | |
| `Verdict` | `matches[].bucket` | friendly label: `Already in Fabric` / `Partial overlap` / `Needs rebuild` |
| `Tier` | `matches[].tier` | `Exact … None` |
| `Score` | `matches[].score` | numeric (sorts as a real number in Excel) |
| `Best Fabric match` | `matches[].best_match.fabric_name` | blank when nothing scored |
| `Fabric workspace` | `matches[].best_match.workspace` | |
| `Source compared` | `matches[].source_compared` | `Yes` / `No` |
| `Shared columns` | `matches[].best_match.shared_column_count` | |
| `Usage (workbooks)` | `matches[].usage.workbook_count` | blank when usage not gathered |
| `Priority` | `matches[].priority` | |
| `Migration priority` | `matches[].migration_priority` | |
| `Logic parity` | `matches[].logic_parity.status` | `none / likely / partial / unverified` |
| `Calc fields` | `matches[].logic_parity.tableau_calc_count` | |
| `Calcs matched as measures` | `matches[].logic_parity.matched` | |
| `Verification` | `matches[].verification.verdict` | present only after `--verify` |
| `Confidence` | `matches[].confidence.level` | `High` / `Medium` / `Low` — trust in the verdict |
| `Reason` | `matches[].reason` | one-line deterministic explanation |

The XLSX **`Summary`** sheet is the estate-sizing headline (a `Metric` / `Value` list): datasource and
model totals, already-in-Fabric / partial / needs-rebuild counts **with percentages**,
`distinct_fabric_matched`, the one-to-one assignment counts, net-new Fabric models, the logic-parity
`review_needed` count, and the `by_tier` / `by_migration_priority` / verification breakdowns (each
rendered only when present). The **`Fabric coverage`** sheet lists
`summary.fabric_coverage.unmatched_model_names` (`Fabric model`, `Workspace`).


