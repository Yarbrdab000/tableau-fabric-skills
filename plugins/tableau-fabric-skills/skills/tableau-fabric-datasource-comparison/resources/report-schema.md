# Report schema

`compare_estate.py --format json` emits the object returned by `compare.compare_inventories()`. The
Markdown report (`--format md`) is a rendering of the same data. **Schema discipline: additive only** —
new keys/artifacts may be added, but existing keys are never renamed or removed.

## Top level

```json
{ "summary": { ... }, "matches": [ ... ] }
```

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
