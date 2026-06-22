---
name: tableau-fabric-datasource-comparison
description: >-
  Read-only estate comparison that matches every published Tableau datasource against
  every Power BI / Fabric semantic model in a tenant and ranks each from "already in
  Fabric" to "needs rebuild", so a migration team can size what already exists versus
  what to recreate. Inventories both sides (Tableau REST + Metadata API, with a
  Catalog-independent .tds fallback; Fabric REST + semantic-model getDefinition / TMDL /
  M parsing) and scores name, column, data-type, and physical-source overlap into tiered
  matches. Tolerates obscured sources -- composite / DirectQuery models, lakehouse
  intermediaries, referenced datasources -- via connector-agnostic table-name matching so
  real overlaps are not missed. Never modifies Tableau or Fabric. Triggers: "compare
  tableau and fabric datasources", "what tableau datasources already exist in fabric",
  "which datasources do we need to rebuild", "tableau to fabric migration inventory",
  "datasource comparison tableau fabric", "map tableau datasources to power bi semantic
  models".
---

> **AUTH MODEL — tableau-fabric-datasource-comparison**
> **Tableau:** PAT (default) *or* Connected App (Direct Trust) JWT (`--auth jwt`). **Read-only** —
> always signs out, never modifies content.
> **Fabric:** an Azure AD bearer token for `https://api.fabric.microsoft.com` (pass `--token`,
> set `FABRIC_TOKEN`, or `--use-az` to mint one with the Azure CLI). Uses only **read** endpoints
> (`list workspaces`, `list semantic models`, `getDefinition`).

# Tableau → Fabric datasource comparison

Answers one migration-planning question across the **whole estate**: *"What have we already built in
Fabric, and what do we still need to recreate?"* It inventories every published Tableau datasource and
every Fabric / Power BI semantic model, scores each Tableau datasource against every model on four
independent signals, and ranks the result from **most comparable → no comparison**.

Self-contained: standard-library only, no imports from the other skills in this collection, so the
folder is independently movable. Talks to Tableau and Fabric over their REST APIs directly (no
`tableauserverclient`), so it works against **Tableau Cloud and Tableau Server**.

## When to use this skill

Use it when the user asks to:
- See how many Tableau datasources **already exist** in Fabric vs. need rebuilding.
- Produce a migration inventory / wave plan that maps each datasource to its Fabric counterpart.
- Find overlap they might miss because the Fabric source is **obscured** (a composite/DirectQuery
  model, or a Lakehouse that mirrors the primary source Tableau connects to directly).
- Hand the "needs rebuild" set to the **`tableau-migration`** skill and the "already exists" set to a
  reuse/verify workflow.

## How it works

```
compare_estate.py (CLI orchestrator)
  ├── tableau_inventory.py  → [{name, project, luid, fields[], sources[]}]
  ├── fabric_inventory.py   → [{name, workspace, id, tables[], columns[], sources[]}]
  ├── compare.py (pure)     → best Fabric match + tier + 4 signals per datasource; estate rollup
  ├── adjudicate.py (pure)  → LLM-optional "second matcher": routes the uncertain tail to an agent
  └── priority.py (pure)    → ranks the rebuild set by downstream usage (attached workbooks)
        → report (Markdown or JSON), ranked most-comparable first, plus an adjudication queue
```

1. **Tableau inventory** — sign in, list every published datasource (REST), and for each pull its
   fields (name + dataType) and **upstream physical tables** from the Metadata API. When Tableau
   Catalog has not indexed a datasource (common on Tableau Cloud for cloud-connected sources, where
   the Metadata API returns nothing), it falls back to downloading the datasource's `.tds` — **without
   its extract** — and parsing columns + relation tables directly. Always signs out.
2. **Fabric inventory** — acquire a token, page all visible workspaces (or `--workspaces`), list the
   semantic models, and `getDefinition` each one to parse its TMDL tables / columns / types and the
   partition `source` (M expression → connector + database + table).
3. **Compare** — pure, offline scoring (see `resources/comparison-methodology.md`). Each datasource
   gets its best-matching model, a tier (`Exact / Strong / Partial / Weak / None`), and an estate
   rollup of `already_exists / partial / rebuild`.

## The four signals

| Signal | Weight | What it measures |
|---|---:|---|
| `name`   | 0.20 | token-set similarity of the asset names |
| `column` | 0.35 | name overlap of fields/columns (Jaccard) |
| `type`   | 0.15 | data-type compatibility across the overlapping columns |
| `source` | 0.30 | overlap of the underlying physical source |

The `source` signal takes the **best of three tiers**: a strict `(connector, database, table)` match, a
looser `(connector, table)` match, and a **connector-agnostic table-name** match. The table-name tier
is what catches the **lakehouse-intermediary** case — when a Fabric model reads from a Lakehouse/
Warehouse that mirrors the primary source while Tableau connects to that source directly, the connector
and database never line up and only the table names survive the move. That tier scores **containment**
(`coverage = |tableau ∩ fabric| / |tableau|`), not symmetric Jaccard, so the common **consolidated
model** — one broad Fabric model unioning many sources, each datasource using a few — is matched at full
strength instead of being diluted to a partial; the matched `shared_tables` and `source_coverage` are
reported so the verdict is auditable. (A generic-only table overlap gets no superset boost.) When the
physical source is **obscured on either side** (no resolvable table at all), the `source` signal is
dropped and its weight is redistributed across name/column/type, so a genuine schema-level overlap is
never buried.

## Counting correctness & precision

The deterministic matcher is hardened so the **estate count** is trustworthy, not just the
per-datasource verdict (all additive — see `resources/comparison-methodology.md`):

- **Distinct, not double-counted.** Several Tableau datasources can each pick the *same* Fabric model
  as their best match. The report flags `contested` models, reports `distinct_fabric_matched` (how
  many distinct models actually back the "already exists" bucket), and adds a **one-to-one
  `assignment`** rollup (each model claimed once) so "already exists" can't quietly over-count.
- **Reverse coverage.** `fabric_coverage` lists Fabric models that *no* Tableau datasource maps to —
  net-new assets already built in Fabric — so the estate view is bidirectional.
- **Precision guards.** Ubiquitous generic columns (`id`/`date`/`region`/`name`) are **down-weighted**
  (curated stoplist + an estate IDF penalty) so a coincidental generic overlap can't manufacture a
  match, while a capped **fuzzy name** fallback rescues near-miss spellings (`SalesOrders` ≈ `Sales
  Order`) without ever outranking a true exact match. Every match carries a one-line `reason`.

## LLM-optional adjudication (the "second matcher")

The four signals are a **structural** matcher — strong on overlap it can measure, blind to **semantic**
equivalence. Two assets can be the same dataset with **renamed columns** (a lakehouse that snake-cases
or re-friendlies the source), a **renamed asset**, or — the inverse risk — a coincidental overlap of
**generic column names** (`Date`/`Region`/`Sales`) that describe different data. The costly mistake for
a migration plan is a **false rebuild**: telling a customer to recreate something Fabric already covers
under different labels.

So, mirroring the `tableau-migration` skill's *second compiler*, every run emits an additive
**adjudication queue** (`report["adjudication"]`) that routes the not-confidently-matched datasources to
an agent acting as a second matcher, with the typed Tableau columns and the top candidates' columns/
sources attached for a semantic judgement. The deterministic verdict stays authoritative; the agent's
verdict (`match` / `partial` / `no-match` + confidence + rationale) is **advisory** and folded in only
on an explicit `--apply-adjudication` pass — it never rewrites the deterministic tier/score, and a
default run adds zero agent verdicts. Full contract: `resources/llm-adjudication.md`.

## Migration priority (what to rebuild first)

"Does it already exist in Fabric?" and "how much does it matter?" are different questions. The skill
adds a second axis — **downstream impact** — so the rebuild set is ranked, not just counted. For each
datasource it gathers `usage` (attached workbooks, plus the sheets / dashboards built on them) and
fuses that with the comparison verdict into a `migration_priority`:

- `already_exists` → **Reuse (already in Fabric)** (never needs migrating, whatever its usage).
- otherwise ordered by usage: **P1 migrate-first** (≥5 workbooks) → **P2** → **P3 deprioritize** (1) →
  **P4 retire candidate** (0). A datasource with **0–1 attached workbook is deprioritized** even if it
  needs a full rebuild; `Unknown` usage stays `Unprioritized`.

Usage gathering trusts the Tableau **Metadata API** as the primary source (in a real migration effort
the assets that matter are catalogued) and uses a thin REST workbook-connection count only for the
not-yet-indexed tail. `--usage {auto,metadata,rest,off}` selects the strategy. Full method:
`resources/migration-priority.md`.

## Usage

```powershell
# Tableau (PAT)
$env:TABLEAU_SERVER   = "https://your-pod.online.tableau.com"
$env:TABLEAU_SITE     = "your-site-content-url"     # "" for the Default site
$env:TABLEAU_PAT_NAME = "your-pat-name"
$env:TABLEAU_PAT_VALUE = "your-pat-secret"

# Whole estate, live on both sides; mint the Fabric token with the Azure CLI:
py -3.11 scripts/compare_estate.py --tableau-live --fabric-live --use-az --format md --out report.md

# Limit Fabric to specific workspaces and cache both inventories for offline re-scoring:
py -3.11 scripts/compare_estate.py --tableau-live --fabric-live --use-az `
    --workspaces "Sales,Finance" `
    --save-tableau-inventory tableau.json --save-fabric-inventory fabric.json --out report.md

# Offline: re-score cached inventories with different weights (no network):
py -3.11 scripts/compare_estate.py `
    --tableau-inventory-json tableau.json --fabric-inventory-json fabric.json `
    --weights "name=0.15,column=0.40,type=0.15,source=0.30" --format json --out report.json
```

Each inventory script also runs standalone (`tableau_inventory.py`, `fabric_inventory.py`) and supports
`--dry-run` to print the calls it would make without touching the network.

### Key flags

- `--tableau-live` / `--tableau-inventory-json PATH` — pull live or load a cached Tableau inventory.
- `--fabric-live` / `--fabric-inventory-json PATH` — pull live or load a cached Fabric inventory.
- `--use-az` / `--token` — acquire the Fabric token via Azure CLI, or pass one explicitly.
- `--workspaces` — comma-separated Fabric workspace names/ids (default: all visible).
- `--tds-fallback {auto,never}` — download+parse a datasource's `.tds` when the Metadata API is empty
  (default `auto`).
- `--usage {auto,metadata,rest,off}` — gather downstream impact (attached workbooks/sheets/dashboards)
  to rank migration priority: `auto` (Metadata API primary + REST tail, default), `metadata` only,
  `rest` only, or `off`. See `resources/migration-priority.md`.
- `--save-adjudication PATH` — write the agent adjudication queue (the review handoff) as JSON.
- `--apply-adjudication PATH` — fold an agent-verdicts JSON back in as advisory annotations (the
  deterministic tier/score are never changed); see `resources/llm-adjudication.md`.
- `--weights`, `--top-n`, `--format {md,json}`, `--out`, `--max-models`,
  `--save-tableau-inventory`, `--save-fabric-inventory`.

## Output

A Markdown (or JSON) report — see `resources/report-schema.md`:
- **Estate rollup**: counts of already-in-Fabric / partial / needs-rebuild, plus a by-tier breakdown.
- **Ranked matches**: every Tableau datasource, its best Fabric match (model + workspace), tier, score,
  and the four signal sub-scores; `src = n/a` flags an obscured-source match.
- **Counting correctness**: a distinct-model rollup, the contested models (one model claimed by several
  datasources), the one-to-one assignment view, and the Fabric models with no Tableau counterpart.
- **Agent adjudication queue**: the not-confidently-matched datasources, each with *why* it was flagged
  (renamed columns, obscured source, near tie, …) for an LLM-optional semantic review.
- **Migration priority**: the rebuild/partial set ranked P1 → P4 by downstream usage, plus a
  by-migration-priority rollup (omitted when usage was not gathered).
- **Recommended actions**: grouped by tier, pointing the rebuild set at the `tableau-migration` skill.

After an `--apply-adjudication` pass the report also shows an **After semantic review** rollup
(deterministic vs. agent-adjudicated counts, with the delta) — advisory, the deterministic verdict is
unchanged.

## Caveats

- **Read-only, but it does pull definitions.** Tenant-wide `getDefinition` is rate-limited (LRO); the
  scanner backs off on 429 and supports `--max-models`. Cache inventories to JSON so scoring re-runs
  need no network.
- **Heuristic, not authoritative.** Scores rank likely overlap; a human verifies before reuse. Tune
  weights/bands per estate (`resources/comparison-methodology.md`).
- **Connector coverage** centers on the connectors the migration skill handles (SQL Server / Azure SQL,
  Snowflake, Postgres, Databricks, BigQuery, Redshift) plus Fabric-native sources (Lakehouse, Warehouse,
  Dataflow) and Excel/CSV, and resolves tables from native-SQL `Value.NativeQuery` and Tableau custom
  SQL; it degrades gracefully to a schema-only signal for anything else.
- **Never commit** a downloaded `.tds`/`.tdsx`, a PAT, or a Fabric token. The scripts write only
  inventory/report JSON you choose with `--save-*` / `--out`.
