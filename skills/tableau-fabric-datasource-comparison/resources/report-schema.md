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
| `best_match` | object \| null | the winning Fabric candidate (null when nothing scored above 0) |
| `candidates` | array | up to `--top-n` candidates (incl. the best), each a candidate object |

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
| `shared_column_count` | int | number of columns that overlap by normalised name |

## Example

```json
{
  "summary": {
    "tableau_total": 6, "fabric_total": 6,
    "by_tier": {"Exact": 1, "Strong": 5, "Partial": 0, "Weak": 0, "None": 0},
    "already_exist": 6, "partial": 0, "rebuild": 0,
    "weights": {"name": 0.2, "column": 0.35, "type": 0.15, "source": 0.3},
    "bands": [["Exact", 0.85], ["Strong", 0.65], ["Partial", 0.4], ["Weak", 0.15], ["None", 0.0]]
  },
  "matches": [
    {
      "tableau_name": "Azure SQL - Superstore", "project": "default", "tableau_luid": "....",
      "tier": "Strong", "score": 0.83, "bucket": "already_exists", "source_compared": true,
      "best_match": {
        "fabric_name": "Azure SQL - Superstore", "workspace": "Github-Testing-Workspace",
        "workspace_id": "....", "fabric_id": "....", "score": 0.83,
        "signals": {"name": 1.0, "column": 0.64, "type": 1.0, "source": 0.85},
        "source_compared": true, "shared_column_count": 18
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
