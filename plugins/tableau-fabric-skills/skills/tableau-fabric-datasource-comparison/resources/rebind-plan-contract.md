# Rebind-plan contract — `schema_version "1.0"`

The embedded-datasource engine emits a **`rebind-plan.json`** that two other skills consume, so its
shape is a **frozen cross-skill contract**. This document is the authoritative reference for
`schema_version "1.0"`. Schema discipline matches the rest of the skill: **additive only** — new
keys may be added, but the keys below are never renamed, removed, or repurposed.

Producer: `scripts/embedded_plan.py` (`build_rebind_plan` / `generate_plan`), wired into
`compare_estate.py` via `--embedded-inventory-json` + `--rebind-plan-out`.
Consumers:

- the **migration / calc-compiler** skill, which builds the semantic models and **writes back** the
  resolved model identity, and
- the **dashboard** skill, which binds each report to its model.

## How the plan is produced

```
embedded_inventory.py  → embedded datasources (+ workbook-local object lists) keyed by workbook_luid
embedded_cluster.py    → fingerprint + cluster near-duplicates (one asset, not fourteen)
embedded_score.py      → score each embedded ds vs Fabric models AND published Tableau datasources
embedded_plan.py       → assign an action + binding target per workbook → rebind-plan.json
```

Scoring **reuses the comparison engine** (`compare.score_pair` / `compare.band_for`); nothing about
tiers, weights, or bands is reinvented for embedded datasources.

## Top level

```json
{
  "schema_version": "1.0",
  "summary":  { ... },
  "source_map": [ { "source_id": "...", "workbook_luid": "..." } ],
  "clusters": [ ... ],
  "models":   { "<model_id>": { ... } },
  "plan":     [ ... ]
}
```

### `source_map` — the luid ↔ source_id linkage (never assume they are equal)

Each entry pairs a `source_id` (the id the download/feed step recorded — a **filename or index** for
local-file runs, the **workbook luid** for a live estate) with the `workbook_luid` (the native key,
empty for local-file runs). **Consumers must not assume `source_id == workbook_luid`.** The plan
carries the map explicitly so a local-file run (where `workbook_luid` is empty) and a live run (where
they coincide) are both unambiguous.

### `models` — the model registry (the calc-compiler writes back here)

Keyed by `model_id` (the logical id the plan assigns; the calc-compiler resolves it to a real model).
The producer seeds each entry; the **calc-compiler writes back** `resolved_model_name` and
`model_path`:

| Key | Written by | Meaning |
|---|---|---|
| `model_id` | plan | logical id referenced by every `plan[]` entry that binds to this model |
| `origin` | plan | `existing_fabric` / `published` / `consolidated_new_model` / `embedded_convert` |
| `resolved_model_name` | **calc-compiler** | the **bare** base model name, no suffix (`null` until built) |
| `model_path` | **calc-compiler** | root-relative `semantic_models/<resolved_model_name>.SemanticModel`; **`null`** when storage falls back (then the workbook's `binding_status` becomes `landed_to_delta`) |
| `connection` | plan | present only for `origin == existing_fabric`: the live `byConnection` identity (see Gate 2) |

The calc-compiler also writes back, **per workbook**, `{workbook_luid, source_id,
resolved_report_folder, bound_model_id}` (the report-folder + the model each report ends up bound to).
The plan does not compute these.

## `plan[]` — one entry per embedded datasource

Each entry is keyed to a workbook (a workbook with several embedded datasources yields several
entries that share its `workbook_luid`). The **required** contract keys:

| Key | Type | Meaning |
|---|---|---|
| `workbook_luid` | string | the native workbook key (empty for local-file runs) |
| `source_ref` | string | = the `source_id` the feed step recorded (see `source_map`) |
| `action` | string | one of the four actions below |
| `model_id` | string | the logical model this workbook binds to (resolves via `models`) |
| `binding_status` | string | drives the consumer — see below; consumers key off this **first** |
| `binding_target` | object | a **tagged union** by `binding_status` (below) |
| `evidence` | object | the overlap evidence behind the decision (`fabric` / `published` / `cluster`) |
| `caveats` | array | human-readable caveats (reuse exclusions, consolidation notes, Gate-1 downgrades) |

Additive context also carried: `workbook_name`, `datasource_id`, `datasource_name`, `cluster_id`, and
`objects` (the embedded datasource's **workbook-local object list** — calcs / sets / groups / bins /
LODs — which is what Gate 1 tests presence against).

### `action` (the migration verb)

| Action | When | Model produced / reused |
|---|---|---|
| `rebind_to_published` | the embedded ds overlaps a **published** Tableau datasource at/above the strong cut | binds to that published datasource's model (`mdl-published-…`) |
| `consolidate_new_model` | the **representative** of a multi-workbook duplicate cluster with no published / Fabric home | builds **one** consolidated model for the whole group (`mdl-cluster-…`) |
| `rebind_to_rebuilt` | a duplicate member rebinding to a model resolved elsewhere in the plan — the consolidated model, or an **existing Fabric** model (reuse) | binds to that already-resolved model |
| `convert_embedded` | a unique embedded ds with no published / Fabric home (or a Gate-1 downgrade) | converts the embedded ds to its own model (`mdl-embedded-…`) |

### `binding_status` + `binding_target` (the tagged union consumers key off **first**)

| `binding_status` | `binding_target` | Consumer behaviour |
|---|---|---|
| `existing_fabric` | `{ "kind": "byConnection", "workspace_id", "semantic_model_id", "dataset_name" }` | dashboard binds **byConnection**; **excluded from the rebuild set** (Gate 2) |
| `built_local` | `{ "kind": "byPath", "model_id", "model_path": null }` | dashboard binds **byPath** using `relpath(model_path, report_dir)` — off `model_path` (written back), **not** the name |
| `landed_to_delta` | `{ "kind": "byPath", "model_id", "model_path": null }` | set on **write-back** when the calc-compiler's `model_path` is `null` (storage fell back); report is left **unbound** |
| `needs_attention` | `{ "kind": "unbound", "reason": "..." }` | unbound; a human must look (e.g. an embedded ds with no fields or sources) |

The `existing_fabric` identity (`workspace_id` / `semantic_model_id` / `dataset_name`) is supplied
**straight from the comparison** `best_match.{workspace_id, fabric_id, fabric_name}` — those reports
bind live and are **never** part of the calc-compiler build set.

### `evidence`

```json
{
  "fabric":   { "tier", "score", "fabric_name", "workspace", "workspace_id", "fabric_id", "shared_tables", "shared_column_count" },
  "published":{ "tier", "score", "published_name", "published_luid", "project", "shared_tables", "shared_column_count" },
  "cluster":  { "cluster_id", "size", "is_duplicate_group" }
}
```

`fabric` / `published` are `null` when that axis had no positive candidate.

## The two locked gates

### Gate 1 — view-dependency feedback downgrade (presence-in-embedded-source)

After the dashboard skill binds the reports it emits a `view_dependency_report`
(`{refs_total, refs_dropped, dropped[], visuals_emptied}` per binding).
`embedded_plan.apply_view_dependency_feedback(plan, report)` folds it back in and downgrades a
`rebind_*` entry to `convert_embedded` **only when a dropped reference names an object the embedded
`<datasource>` actually contains** — a workbook-local calc / set / group / bin / LOD present in that
entry's `objects` list. This is a **presence** test, not a drop-volume test: a reference that is
merely untranslatable in the *published* model (and absent from the embedded source) would reproduce
the same stub under `convert_embedded`, so it is **not** a downgrade trigger. A downgrade rewrites the
entry's `action`, `model_id` (to `mdl-embedded-<cluster_id>`), `binding_status` (`built_local`), and
`binding_target`, appends a Gate-1 caveat, and bumps `summary.gate1_downgrades`.

The report may be supplied either as `{ key: {dropped:[...]} }` (keyed by `workbook_luid` or
`source_ref`) or as `{ "bindings": [ {workbook_luid|source_ref, dropped:[...]} ] }`.

### Gate 2 — existing-Fabric reuse is excluded from the rebuild set

An `existing_fabric` binding carries the live `byConnection` identity and is **excluded from the
calc-compiler build set** ("don't rebuild what already exists in a mature Fabric estate"). Its model
registry entry has `origin: "existing_fabric"` and a `connection` block; the calc-compiler skips it.

## `summary` — the weighting rollup

| Key | Meaning |
|---|---|
| `schema_version` | `"1.0"` |
| `embedded_total` | number of embedded datasources planned |
| `workbook_total` | distinct workbooks (by `workbook_luid` or `source_ref`) |
| `cluster_total` / `duplicate_group_count` | clusters, and how many are multi-member duplicate groups |
| `model_total` / `consolidated_model_total` | distinct models referenced, and how many are new consolidated models |
| `by_action` | count per action |
| `by_binding_status` | count per binding status |
| `rebind_to_published` / `existing_fabric_reuse` / `consolidated_members` / `convert_in_place` | the headline counts |
| `strong_cut` | the score cut above which an overlap counts as "an equivalent already exists" (default `0.65` = the comparison engine's Strong band) |
| `headline` | the one-line rollup (`"Of N embedded datasources across W workbooks: M overlap a published datasource (rebind), R already exist in Fabric (reuse, excluded from rebuild), K cluster into J new consolidated models, C convert in place."`) |
| `gate1_downgrades` | present only after a Gate-1 pass: how many entries were downgraded |

## Renderings

`embedded_plan.render_markdown(plan)` produces a Markdown rollup (headline + by-action / by-binding /
duplicate-group / per-workbook tables); `embedded_plan.write_export_csv(plan, path)` writes a flat CSV
(one row per plan entry) as the analyst pivot source. Both are additive and read-only over the plan.
