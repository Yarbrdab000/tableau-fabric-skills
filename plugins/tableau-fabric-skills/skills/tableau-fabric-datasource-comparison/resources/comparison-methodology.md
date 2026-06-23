# Comparison methodology

How `compare.py` decides whether a Tableau datasource "already exists" in Fabric. The engine is
**pure and offline** — it consumes the two inventory JSON shapes and emits a ranked report. Original
work; this document is the spec.

## The four signals

Each Tableau datasource is scored against every Fabric semantic model on four independent signals, each
normalised to `0..1`:

| Signal | Default weight | Definition |
|---|---:|---|
| `name`   | 0.20 | Jaccard over **name tokens** (lower-cased, split on non-alphanumerics, common stopwords like `datasource`/`data`/`source` dropped). An exact normalised-name match short-circuits to `1.0`; a capped fuzzy fallback rescues near-miss spellings (see *Precision refinements*). |
| `column` | 0.35 | **Weighted** Jaccard over **normalised field/column names** (so `Row_ID`, `Row ID`, and `[Row ID]` all collapse to `rowid`); ubiquitous generic names are down-weighted (see *Precision refinements*). |
| `type`   | 0.15 | Of the columns that overlap by name, the share whose Tableau type is **compatible** with the Fabric (TMDL) type. |
| `source` | 0.30 | Overlap of the **underlying physical source** — see the three-tier scheme below. |

The overall score is the weighted average of the signals that could actually be measured (see
*Obscured sources*). Weights are overridable with `--weights name=..,column=..,type=..,source=..`.

## Type compatibility

Tableau Metadata-API / `.tds` data types are upper-case (`INTEGER`, `REAL`, `STRING`, `DATE`,
`DATETIME`, `BOOLEAN`, …); TMDL column types are camelCase (`int64`, `double`, `string`, `dateTime`,
`boolean`, …). A small original map declares the compatible set per Tableau type (e.g. `INTEGER` →
`{int64, decimal, double}`). An **unknown** Tableau type maps to "compatible with anything" so we never
penalise on missing information rather than a real mismatch.

## The `source` signal: three tiers, best wins

Physical-source overlap is the hardest signal because the same data is reached very differently on each
platform. We compute three candidate scores and take the **maximum**:

1. **Strict** — Jaccard over `(connector, database, table)` keys. Both sides agree on the catalog *and*
   the table. Full credit. When the Metadata API populates only a table's `fullName` (common for cloud
   connectors), the missing `database` is recovered from it so this tier still fires instead of dropping
   to a looser one.
2. **Loose** — Jaccard over `(connector, table)` keys, weighted `× 0.85`. Same connector and table,
   different database name (dev vs prod, a renamed catalog).
3. **Table-name containment** — over **bare table names only**, weighted `× 0.70`. Connector- and
   database-agnostic; this is the tier that survives a platform move. Rather than a symmetric Jaccard it
   uses **containment** — `coverage = |tab ∩ fab| / |tab|`, anchored on the *Tableau* side — so a
   datasource whose every upstream table is present still scores full credit even when the Fabric model
   is a strict **superset** (see below). The superset boost only applies when a *distinctive*
   (non-generic) table is shared; an overlap of only generic names (`data`, `staging`, `export`, …)
   falls back to plain Jaccard so a lone generic table cannot carry a match. `coverage ≥ Jaccard`
   always, so this never lowers a previously-computed score.

Connector strings are folded to canonical tokens first (`azure_sqldb`, `Microsoft SQL Server`, `mssql`
→ `sqlserver`; `postgresql` → `postgres`; `spark` → `databricks`; …) so SQL Server on the Tableau side
lines up with `Sql.Database` on the Fabric side.

### Why containment beats Jaccard — the consolidated model

The dominant real migration pattern is **many Tableau datasources → one broad Fabric model**: a single
semantic model unions a dozen source tables, and each Tableau datasource uses a handful of them. A
symmetric Jaccard punishes this (a datasource using 2 of the model's 12 tables scores `2/12 ≈ 0.17`)
and would mislabel a fully-covered datasource as "needs rebuild". Containment asks the migration
question directly — *are all of this datasource's source tables already present in the model?* — so a
2-of-2 overlap inside a 12-table model reads as full coverage. Each match also reports the actual
`shared_tables` (and `source_coverage`), and the report's rationale names them, so the verdict is
auditable rather than a bare number.

### Why the table-name tier exists — the lakehouse intermediary

A Fabric semantic model frequently reads from a **Lakehouse or Warehouse that mirrors** the primary
source, while the Tableau datasource connects to that primary source **directly**. The connector and
database therefore never match — only the **table names** do. Without the table-name tier these real
overlaps would score `source = 0` and be misclassified as "needs rebuild". The Fabric side draws table
names from both the parsed M source *and* the model's own `tables` list, so even a model whose partition
source is fully obscured still contributes its table names.

### Helper-table filtering

Model scaffolding — a `Date`/`Calendar` dimension, a `_Measures` holder, `Parameters`, and
field-parameter `… Swap` tables — are **not** physical source tables and would dilute the table-name
Jaccard. They are excluded from the table-name set so the signal stays precise.

## Obscured upstream sources

The physical source can be hidden on **either** side:

- **Fabric:** composite / DirectQuery models over an AnalysisServices/Power BI dataset or a dataflow,
  and Databricks/Snowflake M expressions we cannot resolve to a concrete table.
- **Tableau:** a datasource that references **another published datasource**, or whose lineage Catalog
  never indexed.

When **neither side yields any usable table name**, scoring `source = 0` would wrongly bury a genuine
schema-level match. Instead the `source` signal is **dropped** (`null`) and its weight is redistributed
across `name`, `column`, and `type`. Every match carries a `source_compared` flag; the Markdown report
renders the source sub-score as `n/a` when it is `false`.

## Tiers and the estate rollup

The best score per datasource is banded high-to-low:

| Tier | Default threshold | Rollup bucket |
|---|---:|---|
| `Exact`   | ≥ 0.85 | `already_exists` |
| `Strong`  | ≥ 0.65 | `already_exists` |
| `Partial` | ≥ 0.40 | `partial` |
| `Weak`    | ≥ 0.15 | `rebuild` |
| `None`    | < 0.15 | `rebuild` |

- **already_exists** → reuse the existing model; verify before retiring the Tableau datasource.
- **partial** → a related model exists; reconcile added/renamed columns or source drift before reuse.
- **rebuild** → no real equivalent; hand to the `tableau-migration` skill.

### Ranking the rollup by downstream impact

The rollup says *how much* to rebuild; the **migration-priority** signal orders *what to rebuild
first*. Each datasource's downstream usage (attached workbooks + sheets/dashboards, gathered from the
Tableau Metadata API with a REST fallback) bands it `High/Medium/Low/Unused/Unknown`, then fuses with
the bucket: `already_exists` → *Reuse*; otherwise `High→P1` … `Unused→P4 (retire candidate)`, so a
datasource with **0–1 attached workbook is deprioritized** even when it needs a full rebuild. This is
additive — see [`migration-priority.md`](migration-priority.md).

## Counting correctness — distinct, one-to-one, and reverse coverage

The headline tiers above score **each Tableau datasource independently against its best Fabric
model** (a greedy verdict). That is the right per-datasource answer, but a naive estate *count* of
it can mislead, so the report adds three additive correctness signals — none of which change the
greedy per-datasource verdict:

- **Collision detection.** Several Tableau datasources can each pick the **same** Fabric model as
  their best match (e.g. `Sales East` and `Sales West` both map to one `Sales` model). Every match
  carries `contested` / `contested_with`, and `summary.contested_models` lists each shared model and
  who claimed it. `summary.distinct_fabric_matched` reports the count of **distinct** Fabric models
  backing the `already_exists` bucket — so "12 already exist" cannot quietly mean "12 datasources all
  point at the same 3 models".
- **One-to-one assignment.** A greedy **stable assignment** (sort all (datasource, model, score)
  descending; each Fabric model can be claimed once) gives a non-double-counted estate sizing. Each
  match carries `assigned_match` / `assigned_tier`; `summary.assignment` rolls these up. When two
  datasources contest one model, the lower-scoring one drops to its next-best free model (often
  `rebuild`) — the realistic "you still have to rebuild one of them" answer.
- **Reverse coverage.** `summary.fabric_coverage` reports the Fabric models that **no** Tableau
  datasource maps to (`unmatched_model_names`) — net-new assets already built in Fabric — so the
  estate view is bidirectional, not just Tableau→Fabric.

## Precision refinements

Two refinements harden the `name` and `column` signals against the classic false-positive — a
coincidental overlap of generic columns or a near-miss name — without disturbing distinctive matches
(identical assets still score `1.0`, so every exact-match guarantee above is preserved):

- **Generic-column down-weighting.** The column Jaccard is **weighted**: ubiquitous column names
  (`id`, `date`, `name`, `region`, `amount`, …) contribute a fraction of a distinctive name's weight,
  via a curated stoplist blended with an estate **document-frequency (IDF)** penalty — a column that
  appears in nearly every asset carries little information. The IDF half only engages once the estate
  is large enough to be informative (≥ 8 assets); below that the stoplist alone applies. Two assets
  that share *only* `id`/`date`/`region`/`name` no longer look like a match; two that also share
  `net_bookings`/`fiscal_period` still do.
- **Fuzzy name fallback.** When token-set name overlap is low, a capped character-level similarity
  (`difflib`) rescues abbreviations / spacing / pluralisation (`SalesOrders` ≈ `Sales Order`). It
  only contributes above a similarity floor (so unrelated names stay at `0`) and is capped below
  `1.0` (so it can never outrank a true exact-name match).
- **Per-match `reason`.** Every match carries a deterministic one-line `reason` (exact name; weighted
  column overlap %; shared vs obscured source; contested) that renders next to each recommendation,
  so the ranked worklist explains *why*.

## Business-logic parity — calculated fields vs. measures

The four signals above match on **structure** (columns, types, physical source). They are silent on
one thing that decides whether an "already exists" verdict is actually *safe to act on*: did the
datasource's **calculated fields** make it across as Fabric **measures**? Two datasources can share
every column yet encode completely different business logic, and both will score "already exists."

`logic_parity` adds a deliberately conservative, **name-level** check on top of the deterministic
verdict (it never changes tier/score/bucket):

- The Tableau side flags each field as calculated — `fields[].is_calculated` from the Metadata API's
  `__typename == "CalculatedField"`, or a `<calculation>` child element in the `.tds` fallback.
- The Fabric side carries the model's **measure names**, parsed from TMDL (`measure 'Name' = …`).
- For a match, the calc names are lined up against the measure names by the same normalized token used
  everywhere else, yielding a status: `none` (no calcs — nothing to verify), `likely` (every calc name
  has a measure), `partial` (some do), or `unverified` (calcs exist but none line up — the logic almost
  certainly still needs to be rebuilt).

The rollup `summary.logic_parity.review_needed` is the headline risk number: matches that look
already-in-Fabric or partial **but** whose calculations are not confirmed as measures. This is
explicitly **name-level only** — it does *not* compare a Tableau formula against a DAX expression
(that is the `tableau-migration` translator's job). Its single purpose is to stop a clean structural
match from hiding a pile of unmigrated calculations, so *"already exists"* is never read as *"safe to
retire."*

## Verdict confidence — how much to trust each line

Tiers and scores rank *how good* a match is. They do not, on their own, tell a migration lead *which
lines they can act on without a second look*. **Confidence** is a separate, deterministic, read-only
layer (it never changes tier/score/bucket) that fuses the independent evidence already computed into a
single `High` / `Medium` / `Low` judgement — and it does so for **both** verdict directions: a `High`
on *already-in-Fabric* means *confidently reuse*; a `High` on *needs-rebuild* means *confidently
rebuild* (nothing in Fabric is close).

The evidence is treated as **independent corroborators**, so agreement compounds and a verdict resting
on a single signal is explicitly distrusted:

- **score level** — the absolute band the match lands in;
- **margin over the runner-up** — a decisive lead vs. a near-tie between two Fabric models;
- **signal corroboration** — how many of name / column overlap / physical source *independently*
  support the match (one signal is weak even when it scores high);
- **reciprocity** — a *mutual best* match on a **contested** model (the model's strongest suitor is
  this datasource). Trivial reciprocity on an uncontested model is ignored so it can't inflate a clean
  1:1 estate;
- **empirical verification** — when `--verify` ran, `verified` / `compatible` lifts to `High`; a
  `mismatch` caps at `Low` no matter how clean the structure looked.

For a **rebuild** verdict the logic inverts: *no candidate at all* (or a score at/below the Weak floor)
is `High` confidence — we are sure nothing fits — while a score sitting just **below** the partial
threshold is `Low` (borderline: it might be a real partial we are wrongly rejecting). The headline
action number is `summary.confidence.low_confidence_review` — the already-in-Fabric/partial verdicts
that came back `Low` and deserve a human pass. Each match carries `drivers[]` (why it is trusted) and
`cautions[]` (why it is not). See [`report-schema.md`](report-schema.md#verdict-confidence) for the
exact keys.

## Artifact importance — what to protect first

Tiers, priority and confidence all reason about *the match*. **Importance** reasons about *the
datasource itself*: how much business value it carries and how big the blast radius is if a migration
gets it wrong. It is a separate, deterministic, read-only layer (it never changes
tier/score/bucket/priority) that fuses three independent value signals attached during inventory:

- **reach** — dependent workbooks + dashboards (`workbook_count + 2·dashboard_count`, saturating);
- **consumption** — total **view count** across those workbooks (observed usage, saturating);
- **endorsement** — whether the datasource is **certified**.

The signals blend to a `0..1` score (weights renormalised over whichever are present, so a missing
signal never silently drags the result toward zero) that bands into `Critical` / `High` / `Moderate` /
`Low`, or `Unknown` **only** when there was no usage evidence at all — it is never guessed. This is
deliberately distinct from migration **priority**: priority answers *"in what order do we rebuild?"*
from attached-workbook count alone, while importance answers *"how much does this matter, and what
breaks if we move it?"* across reach + consumption + endorsement. The report's **Artifact importance &
connected assets** section spotlights the top datasources with their real views, the named workbooks /
dashboards that depend on them, and the last refresh — so the highest-value assets are migrated and
verified first. See [`report-schema.md`](report-schema.md#artifact-importance--connected-assets) for
the exact keys.

## Borderline decision review — the on-the-fence datasources

Tiers, confidence and importance all answer *how good / how trusted / how valuable* a match is. The
final, decision-grade question a migration lead asks is narrower: *"the engine says reuse this Fabric
model, but it's close — what exactly is different?"* Most datasources bucket cleanly into reuse,
reconcile, or rebuild; a minority sit on the **reuse-vs-rebuild fence**, where an automatic verdict is
precisely where a human wants evidence, not a coin-flip. The borderline layer isolates that set and
attaches a **side-by-side field diff** so the call rests on the actual differences.

It is deterministic, additive and read-only (it never changes tier/score/bucket), and it is
deliberately **inclusive** — a match is flagged when *any* independent trigger fires (it sits in the
`partial` bucket; its score is within `--review-band` of the reuse or rebuild cutoff; the confidence
layer rated it `Low`; or its calculated fields are not yet confirmed as measures). Surfacing one extra
datasource for a human to glance at is far cheaper than silently skipping a real one, so the selection
errs toward review. A clean rebuild with no Fabric candidate is never borderline — there is nothing to
adjudicate.

For each flagged datasource the diff recovers the column-level delta (shared columns, Tableau-only,
Fabric-only, and type mismatches) and the physical-source delta (shared vs. unique upstream tables,
plus the source coverage), and echoes the logic-parity caveat. An advisory `recommendation_hint`
(`lean_reuse` / `lean_rebuild` / `reuse_with_logic_review`) summarises which way the evidence leans
**without** overriding the verdict — the human makes the call. The report prints a **Borderline review**
headline (*"N datasources are on the fence — here's exactly how each differs"*) and the per-datasource
diffs; the `--export-xlsx` workbook adds a **Borderline** sheet. Widen or tighten the fence with
`--review-band` (default `0.08`) and bound the printed detail with `--review-top-n`. See
[`report-schema.md`](report-schema.md#borderline-decision-review-the-reuse-vs-rebuild-fence) for the
exact keys.

## Tuning notes

- Fabric models commonly add measures and calculated columns, which **inflates the column count** and
  lowers the column Jaccard versus the Tableau source. If your estate does this heavily, raise the
  `name`/`source` weights or lower the `Strong` threshold.
- Re-scoring is free once inventories are cached to JSON — iterate on `--weights` without re-pulling.
- The bands are deliberately conservative: a `Strong` is "very likely the same data", not a guarantee.
  Treat the report as a ranked worklist a human confirms, not an automated decision.

## Beyond the deterministic tier — the LLM-optional second matcher

This engine is a **structural** matcher: it is strong on overlap it can *measure* but blind to
**semantic** equivalence. Two assets can be the same dataset with **renamed columns** (a lakehouse
that snake-cases or re-friendlies the source), a **renamed asset**, or — the inverse risk — a
coincidental overlap of **generic column names** (`Date` / `Region` / `Sales`) that look identical
but describe different data. The dangerous outcome for a migration plan is a **false rebuild**:
telling a customer to recreate something a Fabric model already covers under different labels.

So, mirroring the `tableau-migration` skill's *second compiler*, every comparison emits an additive
**adjudication** packet (`report["adjudication"]`) that routes the not-confidently-matched tail to an
agent acting as a "second matcher". The deterministic verdict stays authoritative; the agent's
verdict is **advisory** and folded in only on an explicit `--apply-adjudication` pass. Full contract,
category taxonomy, and the output record: [`llm-adjudication.md`](llm-adjudication.md).
