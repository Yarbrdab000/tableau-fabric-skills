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
  ├── priority.py (pure)    → ranks the rebuild set by downstream usage (attached workbooks)
  ├── importance.py (pure)  → artifact importance from reach + views + certification (value/blast radius)
  ├── confidence.py (pure)  → fuses the signals into a per-verdict High/Medium/Low trust rating
  └── borderline.py (pure)  → diffs the on-the-fence reuse-vs-rebuild datasources (shared/only columns, types, source)
        → report (Markdown or JSON), ranked most-comparable first, plus an adjudication queue
```

1. **Tableau inventory** — sign in, list every published datasource (REST), and for each pull its
   fields (name + dataType) and **upstream physical tables** from the Metadata API. When Tableau
   Catalog has not indexed a datasource (common on Tableau Cloud for cloud-connected sources, where
   the Metadata API returns nothing), it falls back to downloading the datasource's `.tds` — **without
   its extract** — and parsing columns + relation tables directly. Always signs out.
2. **Fabric inventory** — acquire a token, page all visible workspaces (or `--workspaces`), list the
   semantic models, and `getDefinition` each one to parse its TMDL tables / columns / types and the
   partition `source` (M expression → connector + database + table). Each model additionally carries an
   **additive** `relationships[]` graph and a detected `date_table` (its marked or inferred date
   dimension — `null` when none); see `resources/report-schema.md`. These are producer-only signals (no
   consumer is wired) and leave the existing `tables`/`columns`/`measures`/`sources` keys unchanged.
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

## Empirical verification (`--verify`) — does the *data* agree?

Everything above reasons from **metadata** (names, columns, types, lineage). That is strong, but it
can't tell a same-shape / different-data pair apart from a true match. `--verify` adds an opt-in,
advisory **Tier-2** layer that *probes the values*: for the most-comparable confident/partial matches
it runs a handful of **read-only aggregate** queries on **both** sides (Tableau **VizQL Data Service**
+ Fabric **`executeQueries`** DAX) and checks the answers line up — promoting a match from "looks the
same" to "the data agrees," and catching false positives a human would otherwise have to chase.

The catch it is built around: **you cannot compare unbounded totals as equality.** A Fabric model
holding 2019–2026 and a Tableau datasource holding 2021–2026 are the **same** source — Fabric simply
carries more history — yet `SUM(Sales)` / `COUNT` / `DISTINCTCOUNT` legitimately differ. So instead of
naive equality, `--verify` uses **windowed-overlap agreement**:

1. **Establish each side's range** — `MIN`/`MAX` a shared **date** (preferred) or **numeric** key
   column, then compute the common **overlap window** and classify the relationship (*equal / subset /
   superset / partial / disjoint*).
2. **Compare only inside the overlap** — windowed `SUM` / `DISTINCTCOUNT` on both sides. On the shared
   slice the same dataset agrees within tolerance regardless of how much extra history either side
   carries. `MIN`/`MAX` only *establish* the window; they are never pass/fail checks.
3. **Superset is a PASS, not a mismatch** — `verified` (the overlap agrees, whatever the nesting),
   `compatible` (no shared time/key column to window on, but the raw totals are consistent with one
   side being a superset), `mismatch` (the overlap genuinely disagrees, or the ranges are disjoint),
   `inconclusive` (nothing comparable ran). A `mismatch` is **advisory** — it flags a pair for a human
   and never changes the deterministic tier/score.

Verification needs **live Tableau** (VDS) and a **second token** for Power BI `executeQueries`
(audience `https://analysis.windows.net/powerbi/api`, distinct from the Fabric API token) — pass
`--powerbi-token`, set `POWERBI_TOKEN`, or let `--use-az` mint it. Every probe is a single scalar
aggregate; **no row-level data** leaves either platform. Full model: `resources/empirical-verification.md`.

## Business-logic parity (calculated fields → measures)

The four signals match on **columns, types and physical sources** — they are blind to whether a
datasource's **calculated fields** were ever re-expressed as Fabric **measures**. Two datasources
with identical columns but different business logic both score "already exists," so a clean structural
match can still hide a pile of unmigrated calculations. To stop *"already exists"* being mistaken for
*"safe to retire,"* every match carries a conservative, **name-level** `logic_parity` signal: it lines
up the Tableau datasource's calculated-field **names** against the Fabric model's measure **names** and
reports `none` (no calcs), `likely` (all calc names have a measure), `partial` (some do), or
`unverified` (calcs exist but no measures line up — the logic almost certainly still needs rebuilding).
It deliberately **does not compare formulas** — proving a Tableau calc equals a DAX measure is the
`tableau-migration` translator's job; this only flags *where to look*. The report rolls the risky rows
(already-exists / partial matches that are `partial` or `unverified`) into `summary.logic_parity.review_needed`
and renders a **Business-logic parity** section only when at least one matched datasource has calculated
fields. Purely additive — it never changes the deterministic tier/score/bucket.

## Verdict confidence — which lines to trust

Tiers rank *how good* a match is; **confidence** answers the decision-grade question *"which verdicts
can I act on without a second look?"* A new `scripts/confidence.py` fuses the independent evidence the
engine already produced — the score band, the **margin** over the runner-up, how many of name / column
/ physical-source signals *independently* agree, mutual-best **reciprocity** on a contested model, and
(when `--verify` ran) the empirical data check — into one `High` / `Medium` / `Low` rating **per
verdict**. It is symmetric: `High` means *confidently reuse* on an already-in-Fabric verdict and
*confidently rebuild* on a needs-rebuild one, while a score sitting just below the partial threshold is
flagged `Low` (borderline — it might be a real partial). Each match gains
`confidence.{level, drivers[], cautions[], …}` and the rollup gains `summary.confidence` (including
`low_confidence_review` — the already-exists/partial verdicts that came back `Low` and want a human
pass). The report adds a **Verdict confidence** headline and a **Lowest-confidence verdicts** table;
the export adds a `Confidence` column. Deterministic, additive, read-only — re-synthesised after
`--verify` so the data check folds in, and it never changes a tier/score/bucket. See
[`resources/report-schema.md`](resources/report-schema.md#verdict-confidence).

## Artifact importance & connected assets — what to protect first

Knowing a datasource *already exists* in Fabric is half the story; a migration team also needs to know
*how much it matters and what depends on it*. A new `scripts/importance.py` fuses three independent
value signals gathered during inventory — **reach** (dependent workbooks + dashboards), **consumption**
(total **view count**), and **endorsement** (**certified**) — into a `Critical` / `High` / `Moderate` /
`Low` rating per datasource (`Unknown` only when there is no usage evidence at all; weights renormalise
over whichever signals are present so a missing one never drags the score down). This is distinct from
migration **priority** (rebuild order from workbook count): importance is business value / blast radius.
Each match gains `importance.{level, score, drivers[]}`, the rollup gains `summary.importance`, and the
inventory enriches each `usage` block with the underlying telemetry — `view_count`, `certified`,
`has_quality_warning`, the extract refresh timestamps, and `connected_assets` (the **names** of the
dependent workbooks / dashboards). The report adds an **Artifact importance & connected assets** section
that spotlights the highest-value datasources with their real views, dependent assets and last refresh;
the export adds `Importance` / `Views` / `Certified` columns and a **Connected assets** sheet. All
best-effort, deterministic, additive and read-only — it never changes a tier/score/bucket/priority. See
[`resources/report-schema.md`](resources/report-schema.md#artifact-importance--connected-assets).

## Borderline decision review — the reuse-vs-rebuild fence

Most datasources bucket cleanly into *reuse*, *reconcile*, or *rebuild*. A minority sit on the
**fence** between reusing a Fabric model and rebuilding it — close enough that an automatic verdict is
exactly where a migration lead wants **evidence, not a coin-flip**. A new `scripts/borderline.py`
isolates that on-the-fence set and attaches a **side-by-side field diff**: shared vs. Tableau-only vs.
Fabric-only columns, type mismatches, shared/unique upstream tables and source coverage, plus the
logic-parity caveat. Selection is deliberately inclusive — a match is flagged when *any* trigger fires
(it's a `partial`; its score is within `--review-band` of the reuse or rebuild cutoff; confidence rated
it `Low`; or its calcs aren't confirmed as measures) because surfacing one extra datasource to glance
at beats silently skipping a real one. A clean rebuild with no Fabric candidate is never borderline.
Each flagged match gains `match.borderline` (the diff + an advisory `recommendation_hint` that *never*
overrides the verdict) and the rollup gains `summary.borderline`. The report prints a **Borderline
review** headline (*"N datasources are on the fence — here's exactly how each differs"*) and the
per-datasource diffs; the `--export-xlsx` workbook adds a **Borderline** sheet. Tune the fence with
`--review-band` (default `0.08`) and the printed detail with `--review-top-n`. Deterministic, additive,
read-only — it never changes a tier/score/bucket. See
[`resources/report-schema.md`](resources/report-schema.md#borderline-decision-review-the-reuse-vs-rebuild-fence).

## Embedded-datasource rebind plan — workbooks, not just published datasources

Everything above inventories **published** datasources. But a real estate also has hundreds-to-
thousands of **workbooks** whose datasource is **embedded** in the `.twb` (never published) — those
are migration assets too, and the same one is routinely copied into dozens of workbooks. The embedded
engine enumerates them, collapses the near-duplicates, scores each against the estate, and emits a
**`rebind-plan.json`** that says, per workbook, whether to **rebind** it to something that already
exists or **rebuild** its model:

```
embedded_inventory.py → embedded datasources (+ each one's workbook-local calcs/sets/groups/bins/LODs)
                        keyed by workbook_luid; Metadata API + a .twb/parse_tds fallback
embedded_cluster.py   → fingerprint + cluster near-duplicates (14 copies of "Superstore" → one asset)
embedded_score.py     → score each embedded ds vs Fabric models AND published datasources (reuses score_pair)
embedded_plan.py      → assign an action + binding target per workbook → rebind-plan.json (+ Markdown + CSV)
```

Each workbook gets one of four **actions** — `rebind_to_published` (its embedded copy overlaps a
published datasource), `consolidate_new_model` (it leads a duplicate group → build **one** shared
model), `rebind_to_rebuilt` (a duplicate member rebinding to that shared model, or to an existing
Fabric model), or `convert_embedded` (a unique embedded ds with no home) — plus a `binding_target`
tagged by `binding_status` (`existing_fabric` → bind live `byConnection`, **excluded from the rebuild
set**; `built_local` → bind `byPath`; `needs_attention` → unbound). The plan reuses the comparison
engine's scoring wholesale and is a **frozen cross-skill contract** (`schema_version "1.0"`) consumed
by the migration/calc-compiler skill (which builds the models and writes back their resolved
identity) and the dashboard skill (which binds the reports). It honours two gates: a dashboard
`view_dependency_report` downgrades a rebind to `convert_embedded` **only** when a dropped reference
names an object the embedded datasource *actually contains* (presence-in-source, not drop volume),
and existing-Fabric bindings are excluded from the rebuild set. Deterministic, offline, additive.
Full reference: [`resources/rebind-plan-contract.md`](resources/rebind-plan-contract.md).

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

# Hand an exec team a workbook: the Markdown report plus a sizing .xlsx and an analyst CSV:
py -3.11 scripts/compare_estate.py `
    --tableau-inventory-json tableau.json --fabric-inventory-json fabric.json `
    --out report.md --export-xlsx estate.xlsx --export-csv estate.csv

# Embedded-datasource rebind plan: inventory the workbooks first, then plan them against the estate:
py -3.11 scripts/embedded_inventory.py --out embedded.json    # (live; --dry-run to preview)
py -3.11 scripts/compare_estate.py `
    --tableau-inventory-json tableau.json --fabric-inventory-json fabric.json `
    --embedded-inventory-json embedded.json `
    --rebind-plan-out rebind-plan.json --rebind-plan-md rebind-plan.md --rebind-plan-csv rebind-plan.csv `
    --out report.md
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
- `--verify` — empirically verify the top matches (read-only aggregate probes on both sides, compared
  on their overlapping data window); requires `--tableau-live` and a Power BI token. Tune with
  `--verify-top-n` (default 10), `--verify-max-cols` (default 4), `--verify-rtol` (default 0.01).
- `--powerbi-token` / `POWERBI_TOKEN` — Power BI token for `executeQueries` (else `--use-az` mints it);
  a distinct audience from the Fabric token. See `resources/empirical-verification.md`.
- `--review-band FLOAT` — half-width of the reuse-vs-rebuild **fence** for the borderline diff layer
  (default `0.08`); widen to surface more on-the-fence datasources for review, narrow to surface fewer.
- `--review-top-n INT` — cap how many full borderline diffs the Markdown report prints (default `25`).
  See `resources/report-schema.md#borderline-decision-review-the-reuse-vs-rebuild-fence`.
- `--weights`, `--top-n`, `--format {md,json}`, `--out`, `--max-models`,
  `--save-tableau-inventory`, `--save-fabric-inventory`.
- `--export-csv PATH` — also write an executive **CSV**: one row per Tableau datasource (verdict, tier,
  score, best Fabric match + workspace, usage, priority, logic parity, reason) — the analyst pivot
  source. UTF-8 with a BOM so Excel opens it cleanly.
- `--export-xlsx PATH` — also write an executive **`.xlsx`** workbook: a *Summary* sizing headline
  (already-in-Fabric vs. needs-rebuild, %, distinct models, logic-parity review count), a *Datasources*
  detail sheet (the same per-datasource rows, sortable), and a *Fabric coverage* sheet (net-new models).
  Hand-assembled OOXML — **no `openpyxl`/`pandas` dependency**. Composes with everything (`--verify`,
  adjudication) since it just reads the finished report.
- `--embedded-inventory-json PATH` — load a cached **embedded**-datasource inventory (from
  `scripts/embedded_inventory.py`) and emit a **rebind plan** for the workbooks against the gathered
  Fabric + published-Tableau estates. Pair with `--rebind-plan-out PATH` (the `rebind-plan.json`),
  `--rebind-plan-md PATH` (Markdown rollup), `--rebind-plan-csv PATH` (analyst CSV). Tune with
  `--rebind-strong-cut` (default `0.65` — the score above which an embedded ds counts as already having
  an equivalent) and `--rebind-cluster-threshold` (default `0.80` — how aggressively near-duplicates
  collapse). `--view-dependency-report PATH` folds a dashboard feedback report back in (Gate 1). See
  [`resources/rebind-plan-contract.md`](resources/rebind-plan-contract.md).

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
- **Empirical verification** (only with `--verify`): a `verified / compatible / mismatch / inconclusive`
  rollup and a per-pair table, each row noting whether the data agreed on the shared overlap window.
- **Verdict confidence**: a headline (*N of M verdicts are high-confidence*) plus, when any verdict is
  uncertain, a **Lowest-confidence verdicts (review these first)** table that names *why* each is
  shaky (near tie, single signal, contested model, borderline score, empirical mismatch).
- **Artifact importance & connected assets** (when usage telemetry was gathered): a headline of the
  Critical/High-importance datasources and total views, plus a top table with each high-value
  datasource's views, dependent workbooks/dashboards (by name) and last refresh.
- **Borderline review** (when any datasource is on the reuse-vs-rebuild fence): a headline of how many
  are borderline, plus a per-datasource **diff** — shared / Tableau-only / Fabric-only columns, type
  mismatches, shared/unique source tables, and an advisory lean — so the customer decides reuse vs.
  rebuild from the actual differences.
- **Recommended actions**: grouped by tier, pointing the rebuild set at the `tableau-migration` skill.

When `--embedded-inventory-json` is supplied the run also writes a **rebind plan** for the workbooks
(`--rebind-plan-out` / `-md` / `-csv`): a headline weighting rollup (*"of N embedded datasources
across W workbooks: M rebind to a published datasource, R already exist in Fabric, K cluster into J
new consolidated models, C convert in place"*), per-workbook action + binding-target rows, and the
duplicate-group clusters — the `rebind-plan.json` handed to the migration and dashboard skills
(`resources/rebind-plan-contract.md`).

After an `--apply-adjudication` pass the report also shows an **After semantic review** rollup
(deterministic vs. agent-adjudicated counts, with the delta) — advisory, the deterministic verdict is
unchanged.

### Executive export (`--export-csv` / `--export-xlsx`)

For sharing the result outside the terminal, the same report renders to two analyst-/exec-friendly
artifacts (standard-library only — no `openpyxl`/`pandas`):

- **CSV** — one rectangular table, one row per Tableau datasource, ready to pivot in Excel / Sheets.
- **XLSX** — a multi-sheet workbook: **Summary** (the estate-sizing headline — how many datasources
  already exist in Fabric vs. need rebuilding, with percentages, distinct-model counts, the
  logic-parity review count, and importance metrics), **Datasources** (the full per-datasource detail,
  score as a real number so it sorts), **Fabric coverage** (models nothing in Tableau maps to),
  **Connected assets** (one row per dependent workbook / dashboard — when telemetry was gathered), and
  **Borderline** (one row per on-the-fence datasource with its column/source diff counts — when any
  datasource is borderline). All byte-stable and read-only over the report. Column reference:
  `resources/report-schema.md`.

## Caveats

- **Read-only, but it does pull definitions.** Tenant-wide `getDefinition` is rate-limited (LRO); the
  scanner backs off on 429 and supports `--max-models`. Cache inventories to JSON so scoring re-runs
  need no network.
- **Heuristic, not authoritative.** Scores rank likely overlap; a human verifies before reuse. Tune
  weights/bands per estate (`resources/comparison-methodology.md`). `--verify` raises confidence by
  checking the data agrees, but it is still **advisory** and aggregate-only — a `mismatch` flags a pair
  for review, it does not change the deterministic verdict.
- **Connector coverage** centers on the connectors the migration skill handles (SQL Server / Azure SQL,
  Snowflake, Postgres, Databricks, BigQuery, Redshift) plus Fabric-native sources (Lakehouse, Warehouse,
  Dataflow) and Excel/CSV, and resolves tables from native-SQL `Value.NativeQuery` and Tableau custom
  SQL; it degrades gracefully to a schema-only signal for anything else.
- **Never commit** a downloaded `.tds`/`.tdsx`, a PAT, or a Fabric token. The scripts write only
  inventory/report JSON you choose with `--save-*` / `--out`.
