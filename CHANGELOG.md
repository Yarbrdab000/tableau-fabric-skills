# Changelog

All notable changes to this collection are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
collection follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) at the
**collection level** — the four packaging manifests
(`.claude-plugin/marketplace.json`, `.github/plugin/marketplace.json`,
`plugins/tableau-fabric-skills/.claude-plugin/plugin.json`, and the deprecated
`tableau-migration` plugin alias) share one version. Each skill additionally carries its
own `VERSION` stamp (`skills/<name>/VERSION`).

## [Unreleased]

### Added
- **tableau-fabric-datasource-comparison:** the Fabric semantic-model inventory
  (`fabric_inventory.py`) now additively carries parsed **`relationships`**
  (`[{fromTable, fromColumn, toTable, toColumn, isActive}]`, both `'Table'[Column]` and `Table.Column`
  ref forms, `isActive` default-true) and a detected **`date_table`** object describing each model's
  marked or inferred date dimension (`{table, key_column, active_keys[], inactive_keys[],
  grain_columns[], marked}`; `null` when none). A date table is detected as **marked** via table-level
  `dataCategory: Time`, else **inferred** from relationships whose `toColumn` is a dateTime-typed key
  column. Producer-only (no consumer wired); the existing `tables`/`columns`/`measures`/`sources` keys
  are unchanged. `resources/report-schema.md` documents the new keys. Skill `VERSION` `1.7.0` → `1.8.0`; collection `0.9.0` → `0.10.0`.
- **tableau-migration:** the deterministic calc→DAX compiler (`scripts/calc_to_dax.py`) gains
  faithful, type-checked translations for more Tableau functions — `ATAN2`, `DATENAME` (all date
  parts, not just weekday), `ISOYEAR`, `DATETIME`, `ATTR`, `GROUP_CONCAT`, and the table
  calculations `RANK_MODIFIED`, `RANK_PERCENTILE`, and `TOTAL` — each with a probe/test and the
  original formula preserved as a `TableauFormula` annotation. The tie-aware
  **argmax-over-a-dimension** suggestion now also recognizes the real workbook shape where the
  per-partition max and the per-member detail are **separate named calcs** (e.g. "Highest Selling
  City By State Sales"), in addition to the inline and single-reference forms. Functions with **no
  provably-faithful DAX target** stay deliberately *fail-closed* (regex `REGEXP_*`; one-sided /
  internal-whitespace `TRIM`/`LTRIM`/`RTRIM`; start-of-week- or ISO-dependent `WEEK`/`ISOQUARTER`;
  `MAKETIME`/`MAKEDATETIME`; `HEXBINX`/`HEXBINY`; culture-sensitive `STR`; addressing-order
  `RANK_UNIQUE`; …), and the translation router (`scripts/translation_router.py`) now routes each
  to **honest, actionable guidance** — a DAX-language-gap note that explains *why* no faithful form
  exists, and (for a bare row-level expression used where a measure is required, e.g.
  `IF [Region]="east" THEN [Sales] END`) a missing-aggregation hint pointing to the `SUM(...)` /
  calculated-column fix — instead of an over-optimistic catch-all. Additive only; the migration
  suite stays green. Skill `VERSION` `1.3.0` → `1.4.0`.
- **tableau-migration:** estate/local runs now emit an **openable Power BI project (`.pbip`)** per
  migrated datasource by default (`pbip/<Name>/<Name>.pbip` via `assemble_model.write_local_pbip`),
  alongside the canonical `semantic_models/<Name>.SemanticModel/`, so each datasource opens directly
  in Power BI Desktop to explore and test. `migrate_estate.py` gains `--no-pbip` to suppress. Skill
  `VERSION` `1.2.1` → `1.3.0`.
- **tableau-migration:** end-of-run **second-compiler check-in** — when a run leaves stubbed
  calculations (`report["summary"]["needs_review_total"] > 0`, also surfaced in `summary.md`'s new
  **Next step** section and the per-datasource `translation_handoff`), the skill now offers to run the
  stubs through the second compiler instead of silently stopping. SKILL.md,
  `resources/second-compiler.md`, and `resources/migration-report.md` document the check-in.
- **docs:** [`INSTALL.md`](INSTALL.md) gains an **Updating** section (plugin and manual-folder update
  paths, the `tableau-migration` version-gated runbook, and the not-live-until-a-new-session caveat);
  [`UNINSTALL.md`](UNINSTALL.md) gains a **Clean up what removal leaves behind** section for the side
  effects a folder/plugin delete doesn't remove (the MCP landing zone's Azure resources, MCP client
  config and Copilot Studio connector, the local Docker stack, and downloaded Tableau artifacts /
  self-update backups).
- **tableau-fabric-datasource-comparison (new skill):** read-only estate comparison that inventories
  every published Tableau datasource and every Fabric / Power BI semantic model in a tenant and ranks
  each datasource from "already in Fabric" to "needs rebuild". Scores a weighted blend of four signals
  (name, column overlap, type compatibility, physical source) into tiers (`Exact / Strong / Partial /
  Weak / None`) and an estate rollup. The physical-source signal takes the best of strict
  `(connector, database, table)`, loose `(connector, table)`, and a connector-agnostic **table-name**
  tier, so it survives a **lakehouse intermediary** (Fabric reads a mirror; Tableau connects directly)
  and falls back gracefully when the upstream source is **obscured** (composite/DirectQuery models,
  referenced datasources) by dropping the source signal and redistributing its weight. The Tableau
  inventory adds a **Catalog-independent `.tds` fallback** (downloads the descriptor without its
  extract and parses columns + relation tables) so cloud-connected datasources the Metadata API can't
  see still produce a full schema. Standard-library only; offline-testable scoring core; never modifies
  Tableau or Fabric. Registered additively in all four packaging manifests (collection `0.3.0` →
  `0.4.0`).
  - **LLM-optional adjudication ("second matcher"):** every comparison now emits an additive
    `report["adjudication"]` queue (`scripts/adjudicate.py`) that routes the not-confidently-matched
    datasources — renamed columns, a renamed asset, an obscured/lakehouse source, a near tie, or a
    coincidental overlap of generic column names — to an agent for a **semantic** verdict, modelled on
    the `tableau-migration` skill's *second compiler*. The deterministic verdict stays authoritative;
    `--apply-adjudication` folds the agent's `match` / `partial` / `no-match` calls back in as advisory
    `agent_review` annotations and an `adjudicated_summary` rollup **without** changing any
    deterministic tier/score. Adds `--save-adjudication` / `--apply-adjudication` and
    `resources/llm-adjudication.md`; skill `VERSION` `1.0.0` → `1.1.0`.
  - **Migration-priority signal:** the comparison now also ranks *which* rebuilds matter by
    **downstream impact** (`scripts/priority.py`). Each datasource's usage — attached workbooks plus
    the sheets/dashboards built on it — is gathered from the Tableau **Metadata API** as the trusted
    primary source, with a thin REST workbook-connection fallback for the not-yet-indexed tail
    (`--usage {auto,metadata,rest,off}`). Usage bands (`High/Medium/Low/Unused/Unknown`) fuse with the
    verdict into an actionable `migration_priority` (`already_exists` → *Reuse*; otherwise `P1
    migrate-first` … `P4 retire candidate`), so a datasource with **0–1 attached workbook is
    deprioritized** even if it needs a full rebuild. Adds `matches[].usage` / `.priority` /
    `.migration_priority`, `summary.by_priority` / `by_migration_priority` / `usage_thresholds`, a
    Markdown "Migration priority" section, and `resources/migration-priority.md`; all additive. Skill
    `VERSION` `1.1.0` → `1.2.0`.
  - **Robustness & reliability pass (counting correctness, precision, source coverage):** all additive.
    (1) *Counting correctness* — the comparison now detects when several Tableau datasources claim the
    **same** Fabric model (`matches[].contested` / `contested_with`, `summary.contested_models`),
    reports `summary.distinct_fabric_matched` (distinct models behind the "already exists" bucket), adds
    a greedy **one-to-one** `summary.assignment` rollup (`assigned_match` / `assigned_tier`) so the
    estate can be sized without double-counting a shared model, and adds reverse `summary.fabric_coverage`
    (Fabric models no Tableau datasource maps to). (2) *Precision* — the column signal **down-weights
    ubiquitous generic names** (curated stoplist blended with an estate IDF penalty, gated to estates of
    ≥ 8 assets) so a coincidental generic overlap can't manufacture a match; a capped **fuzzy name**
    fallback (`difflib`) rescues near-miss spellings without ever outranking a true exact match; and each
    match carries a deterministic one-line `reason`. (3) *Source coverage* — Fabric M parsing gains
    **Lakehouse / Warehouse / Dataflow / Excel / CSV** connectors and `[Id=…]` / `[entity=…]` table
    navigation plus native-SQL `Value.NativeQuery` FROM/JOIN extraction, and the Tableau `.tds` parser
    now mines **custom SQL** (`<relation type='text'>`) FROM/JOIN tables — both directly strengthening
    the source signal across a lakehouse intermediary. Identical-asset scores are unchanged (every exact
    match still scores `1.0`). Comparison suite `65` → `82` tests. Skill `VERSION` `1.2.0` → `1.3.0`;
    collection `0.4.0` → `0.5.0`.
  - **Lineage-graph source matching (containment + table-name provenance):** all additive. The
    connector-agnostic table-name tier now scores **containment** — `coverage = |tableau ∩ fabric| /
    |tableau|`, anchored on the Tableau side — instead of a symmetric Jaccard, so a **consolidated**
    Fabric model that *covers* all of a datasource's upstream tables matches at full strength even when
    it is a strict superset (the dominant many-datasources→one-model migration pattern), where Jaccard
    would have diluted it to a partial. The superset boost only applies when a **distinctive**
    (non-generic) table is shared — a lone generic name (`data`/`staging`/`export`/…) falls back to
    plain Jaccard — and `coverage ≥ Jaccard` always, so no previously-computed score drops (identical
    assets still score `1.0`). Each candidate now exposes the matched `shared_tables` and
    `source_coverage`, and the per-match `reason` **names the shared source tables**, making the source
    verdict auditable. The Tableau inventory also **backfills `database`/`schema` from a table's
    `fullName`** when the Metadata API leaves them empty (common for cloud connectors), so the strict
    `(connector, database, table)` tier fires instead of dropping to the looser table-only signal.
    Comparison suite `82` → `90` tests. Skill `VERSION` `1.3.0` → `1.4.0`; collection `0.5.0` → `0.6.0`.
  - **Durability test pass (resilience contract):** locked the comparison engine's graceful-degradation
    behaviour against hostile / malformed / edge-case input with **+33 tests** (comparison suite `90` →
    `123`): None-valued fields and sources, empty and tableau-only estates, malformed records, Unicode /
    emoji / non-Latin names, determinism and input-order independence, a 120×120 estate, duplicate names
    on both sides, and partial signal dicts; plus parser-resilience for CRLF/tab/blank-line and truncated
    TMDL/M, very-long M input, bad-base64 / missing-`definition` payloads, corrupt and `.tds`-less ZIP
    archives, malformed `.tds` XML, pathological `fullName`, and out-of-range / non-numeric usage counts.
    Two small **additive** hardenings surfaced by the tests: Markdown table cells now neutralise `|` /
    newlines in attacker-influenced names so a hostile name can't break the ranked-matches table, and the
    adjudication apply path drops non-`dict` decision entries (`None` / strings / ints) instead of
    raising. No report key renamed or removed; identical-asset scores unchanged. Skill `VERSION` `1.4.0`
    → `1.4.1`; collection `0.6.0` → `0.6.1`.
  - **Empirical verification (`--verify`, Tier-2, opt-in/advisory):** promotes a match from "looks the
    same (schema/lineage)" to "the **data** agrees" by running read-only **aggregate** probes on both
    sides (Tableau **VizQL Data Service** + Fabric **`executeQueries`** DAX) and checking they line up.
    Built around **windowed-overlap agreement** so it is not fooled by volume: it `MIN`/`MAX`es a shared
    date/numeric key to find each side's range and their **common overlap window**, then compares
    `SUM`/`DISTINCTCOUNT` **only inside that overlap** — so a Fabric model with extra history (e.g.
    2019–2026 vs Tableau 2021–2026) **verifies** instead of looking like a mismatch. Verdicts:
    `verified` / `compatible` (one-side-superset, no window column) / `mismatch` (overlap disagrees or
    ranges disjoint) / `inconclusive`. Adds `match.verification` + `match.verification_note` and a
    `summary.verification` rollup, plus a new "Empirical verification" report section — all **additive**;
    the deterministic tier/score/bucket are never changed (a `mismatch` is advisory). New CLI flags
    `--verify`, `--verify-top-n` (10), `--verify-max-cols` (4), `--verify-rtol` (0.01), and
    `--powerbi-token` / `POWERBI_TOKEN` (a **distinct** Power BI audience from the Fabric token; or
    `--use-az` mints it). Read-only and aggregate-only — no row-level data leaves either platform; needs
    live Tableau and degrades gracefully (cached inventory, missing token, 404/429/401/403/paused
    capacity → *skipped*/*inconclusive*). New `resources/empirical-verification.md`; comparison suite
    `123` → `171` tests. Skill `VERSION` `1.4.1` → `1.5.0`; collection `0.6.1` → `0.7.0`.
  - **Empirical verification — actionable "Fabric returned no data" detection (live-dry-test
    hardening):** when an `--verify` match comes back `inconclusive` purely because the Fabric model
    returned nothing while Tableau returned real values, the verdict now says **why**, and never reads
    it as a mismatch. A new `match.verification.reason_code` distinguishes `fabric_no_data` (model held
    no rows / explicit *"needs to be recalculated or refreshed"* — refresh it) from `fabric_unreadable`
    (every probe errored, e.g. a DirectQuery source not configured or a paused capacity — resolve it),
    each with a fix-it `verification_note`; rolled up as `summary.verification.fabric_no_data` /
    `fabric_unreadable` and a plain-language callout in the report. Gated on *Fabric returned nothing
    for any probe **and** Tableau returned data*, so a per-column quirk is never mislabelled. The 400
    `executeQueries` error detail is now surfaced (`extract_executequeries_error`) instead of a generic
    code. All **additive** — no key renamed/removed; deterministic tier/score/bucket unchanged.
    Verified end-to-end against the live 10ay Tableau + Fabric F2 mirror estate (6/6 already-exist;
    all 6 models correctly reported as refresh/connection-pending, not mismatches). Comparison suite
    `171` → `178` tests. Skill `VERSION` `1.5.0` → `1.5.1`; collection `0.7.0` → `0.7.1`.
  - **Empirical verification — offline transport-seam tests (reliability hardening):** the thin
    live-only transports and the probe closures that turn raw HTTP into `(value, error)` are now
    exercised offline. New `tests/test_transport.py` mocks each network seam (`fabric_inventory._http`
    / `_request` / `acquire_powerbi_token`'s `subprocess.run`, `TableauClient._request`,
    `fab.execute_dax`) and **replays the exact response envelopes observed live** — Fabric
    `executeQueries` 200+scalar, 200+`null` (Import model never refreshed), 400 *"...needs to be
    recalculated or refreshed"*, the generic 400 *"Failed to execute the DAX query."* (DirectQuery
    source not configured), 429/401; Tableau VDS 200 / 404 (feature off) / 429 / error — so the
    `(value, error)` mapping and the `reason_code` triggers (`fabric_no_data` vs `fabric_unreadable`)
    are regression-locked without a live tenant. Tests only — no behavior or schema change. Comparison
    suite `178` → `203` tests. Skill `VERSION` `1.5.1` → `1.5.2`; collection `0.7.1` → `0.7.2`.
  - **Empirical verification — measures are never used as a window axis (false-mismatch fix):** an
    additive **measure** (e.g. `Sales`) is no longer eligible as the `MIN`/`MAX` overlap-window axis.
    Ranging a measure by its own bounds and then filtering its `SUM` to that overlap is
    self-referential and could flag a pure Fabric superset (the *same* datasource, just more rows) as a
    false `mismatch` — exactly the "same data, more history" trap windowing exists to avoid. Window
    candidacy is now gated on the Tableau Metadata-API `role`: `role == "measure"` columns are excluded
    as axes (dates and numeric *dimensions* — year / key / id — remain valid axes), while measures are
    still compared as `SUM` equality probes *inside* whatever window a dimension establishes. When only
    measures are shared, no window is built and verification drops to the conservative **containment**
    read (which never emits a `mismatch` from magnitude alone) instead of a bogus self-referential
    window. All **additive** — no key renamed/removed; deterministic tier/score/bucket unchanged.
    Comparison suite `203` → `206` tests. Skill `VERSION` `1.5.2` → `1.5.3`; collection `0.7.2` →
    `0.7.3`.
  - **Business-logic parity (calculated fields → measures) — closes the "structurally identical ≠
    logically equivalent" gap:** the four structural signals (name / column / type / source) say nothing
    about whether a datasource's **calculated fields** were re-expressed as Fabric **measures**, so two
    datasources with identical columns but different logic both scored "already exists." Each match now
    carries an additive, **name-level** `logic_parity` (`{status, tableau_calc_count, fabric_measure_count,
    matched, unmatched[]}`, `status ∈ none / likely / partial / unverified`) comparing Tableau calc names
    against model measure names, plus a `summary.logic_parity` rollup whose `review_needed` counts
    already-exists / partial matches whose calculations are **not** confirmed as measures — so an
    "already exists" verdict is never mistaken for "safe to retire." It deliberately does **not** compare
    formulas (that is the `tableau-migration` translator's job); it only flags where logic likely still
    needs rebuilding. Inputs: Tableau `fields[].is_calculated` (Metadata-API `__typename ==
    "CalculatedField"`, or a `<calculation>` child in the `.tds` fallback) and model-level `measures`
    parsed from TMDL. The Markdown report renders a **Business-logic parity** section only when a matched
    datasource has calculated fields; otherwise output is byte-for-byte unchanged. All **additive** — no
    key renamed/removed; deterministic tier/score/bucket unchanged. Comparison suite `206` → `218` tests.
    Skill `VERSION` `1.5.3` → `1.5.4`; collection `0.7.3` → `0.7.4`.
  - **Executive CSV / XLSX export (`--export-csv` / `--export-xlsx`) — share the result outside the
    terminal:** the finished report (whatever layers ran — verification, adjudication, logic-parity) now
    renders to two share-ready artifacts via a new `scripts/export.py` (**standard-library only** — the
    `.xlsx` is hand-assembled OOXML / SpreadsheetML, no `openpyxl` / `pandas` dependency). `--export-csv`
    writes one rectangular table — one row per Tableau datasource (verdict / tier / score / best Fabric
    match + workspace / usage / priority / logic parity / reason), the analyst pivot source, UTF-8 with a
    BOM so Excel opens it cleanly. `--export-xlsx` writes a three-sheet workbook: a **Summary** estate-
    sizing headline (already-in-Fabric vs. needs-rebuild counts **with percentages**, distinct models,
    one-to-one assignment, net-new models, the logic-parity review count, and the by-tier /
    by-migration-priority / verification breakdowns), a **Datasources** detail sheet (the same per-
    datasource rows with `Score` as a real number so it sorts), and a **Fabric coverage** sheet (models
    nothing in Tableau maps to). Both are **read-only over the report and purely additive** — they never
    alter a report key; the Markdown / JSON output is unchanged. Comparison suite `218` → `240` tests.
    Skill `VERSION` `1.5.4` → `1.5.5`; collection `0.7.4` → `0.7.5`.
  - **Verdict confidence — a decision-grade trust layer:** a new `scripts/confidence.py` fuses the
    independent evidence the engine already computes (score band, margin over the runner-up, how many
    of name / column / physical-source signals *independently* agree, mutual-best **reciprocity** on a
    contested model, and — when `--verify` ran — the empirical data check) into one `High` / `Medium` /
    `Low` confidence **per verdict**. It is symmetric: `High` means *confidently reuse* on an
    already-in-Fabric verdict and *confidently rebuild* on a needs-rebuild verdict (a borderline score
    just under the partial threshold is flagged `Low` instead). Each match gains
    `confidence.{level, drivers[], cautions[], margin, corroborating_signals, reciprocal_best}`; the
    rollup adds `summary.confidence.{high, medium, low, high_confidence_already_exists,
    low_confidence_review}`. The Markdown report gains a **Verdict confidence** headline near the top
    and a **Lowest-confidence verdicts (review these first)** table; the CSV/XLSX export gains a
    `Confidence` column and two Summary metrics. **Deterministic, additive and read-only** — never
    changes a `tier` / `score` / `bucket`; re-synthesised after `--verify` so the data check folds in.
    Comparison suite `240` → `267` tests. Skill `VERSION` `1.5.5` → `1.5.6`; collection `0.7.5` →
    `0.7.6`.
  - **Artifact importance & connected assets — value/blast-radius + usage telemetry:** a new
    `scripts/importance.py` fuses three independent value signals gathered during inventory — **reach**
    (dependent workbooks + dashboards), **consumption** (total **view count**), and **endorsement**
    (**certified**) — into a `Critical` / `High` / `Moderate` / `Low` rating per datasource (`Unknown`
    only when there is no usage evidence; weights renormalise over present signals). Distinct from
    migration **priority** (rebuild order): importance is *how much it matters and what breaks if it
    moves*. The Tableau inventory now best-effort-enriches each `usage` block with `view_count` (summed
    from per-workbook REST view statistics), `certified`, `has_quality_warning`, the extract refresh
    timestamps, `updated_at`, and `connected_assets` (the **names** of dependent workbooks / dashboards)
    via a **separate** Metadata-API query kept isolated from the proven downstream-count query, so a
    rejected field only loses enrichment. Each match gains `importance.{level, score, drivers[]}`; the
    rollup adds `summary.importance.{by_level, critical, high, total_views, certified_datasources,
    datasources_with_quality_warning}`. The Markdown report gains an **Artifact importance & connected
    assets** section (highest-value datasources with their views, dependent assets and last refresh);
    the CSV/XLSX export gains `Importance` / `Views` / `Certified` columns, importance Summary metrics,
    and a fourth **Connected assets** sheet (one row per dependent asset, when telemetry was gathered).
    Connected-asset names are **deduped** (the Metadata API returns an asset once per sheet path) so the
    deliverable never shows the same workbook/dashboard twice. **Deterministic, additive and
    read-only** — never changes a `tier` / `score` / `bucket` / `priority`. **Live-verified** end-to-end
    against a real Tableau Cloud site (the richer Metadata-API query and the view-statistics REST
    endpoint both resolve; importance section + connected-assets export render with real data).
    Comparison suite `267` → `306` tests. Skill `VERSION` `1.5.6` → `1.5.7`; collection
    `0.7.6` → `0.7.7`.
- **tableau-fabric-datasource-comparison:** new **borderline decision-review** layer
  (`scripts/borderline.py`) for the datasources sitting on the **reuse-vs-rebuild fence** — where the
  structural evidence is genuinely close, so the customer can decide from a diff instead of trusting an
  automatic verdict. Selection is deliberately inclusive (flagged when **any** trigger fires: the
  `partial` bucket, score within `--review-band` of the reuse/rebuild cutoff, a `Low`-confidence
  verdict, or calcs not yet confirmed as measures); a clean rebuild with no Fabric candidate is never
  borderline. Each flagged match gains `match.borderline` — the field-level diff (shared / Tableau-only
  / Fabric-only columns, type mismatches, shared/unique upstream tables, source coverage, logic-parity
  caveat) plus an advisory `recommendation_hint` (`lean_reuse` / `lean_rebuild` /
  `reuse_with_logic_review`) — and the rollup gains `summary.borderline.{count, band, strong_cut,
  partial_cut, by_origin_bucket, reasons, hints, names}`. The Markdown report adds a **Borderline
  review** headline + per-datasource diff section; the `--export-xlsx` workbook adds a **Borderline**
  sheet (when `count > 0`). New CLI flags `--review-band` (default `0.08`, fence half-width) and
  `--review-top-n` (default `25`, printed-diff cap). The `recommendation_hint` **never** overrides the
  verdict. **Deterministic, additive and read-only** — never changes a `tier` / `score` / `bucket`.
  Comparison suite `306` → `327` tests (+21). Skill `VERSION` `1.5.7` → `1.6.0`; collection
  `0.7.7` → `0.8.0`.
- **tableau-fabric-datasource-comparison:** new **embedded-datasource rebind/consolidation engine**
  — the skill now plans the **workbooks** with embedded (in-`.twb`, never-published) datasources, not
  only the published datasources. Four new pure, offline scripts: `embedded_inventory.py` enumerates
  every embedded datasource (+ its **workbook-local object list** — calcs / sets / groups / bins /
  LODs — keyed by `workbook_luid`) via the Metadata API with a `.twb`/`parse_tds` download fallback
  and a local-files mode; `embedded_cluster.py` fingerprints + clusters near-duplicates so the same
  datasource copied into dozens of workbooks collapses to **one** asset; `embedded_score.py` scores
  each embedded ds against the Fabric models **and** the published Tableau datasources by **reusing
  `compare.score_pair` / `compare.band_for`** (no scoring reinvented); `embedded_plan.py` emits a
  **`rebind-plan.json`** (frozen cross-skill `schema_version "1.0"`) assigning every workbook an
  `action` (`rebind_to_published` / `consolidate_new_model` / `rebind_to_rebuilt` / `convert_embedded`),
  a logical `model_id`, and a `binding_target` tagged by `binding_status` (`existing_fabric` →
  `byConnection` identity straight from the comparison, **excluded from the rebuild set**;
  `built_local` → `byPath`; `needs_attention` → unbound), plus overlap `evidence`, `caveats`, the
  `source_id ↔ workbook_luid` map (never assumes they are equal), an optional `date_table` slot
  reserved on every bound target (safe-default `null`; enriched later by the Fabric-inventory pass or
  the calc-compiler write-back), a per-entry `label` sibling — the caption-preferred selector the
  migration skill's `migrate_datasource(datasource=label)` accepts to pick an embedded datasource out
  of its workbook (derived from the RAW `<datasource name>` in the no-caption case to mirror
  migration's raw match), with `source_ref` kept as the `source_id` string — an optional per-entry
  `drift` fingerprint `{table_count, column_count, calc_count}`, and a Markdown rollup + analyst CSV.
  Two locked gates: `apply_view_dependency_feedback` downgrades a rebind to `convert_embedded` **only**
  when a dropped reference names an object the embedded datasource *actually contains*
  (presence-in-source), and existing-Fabric bindings are excluded from the rebuild set. Additive CLI
  on `compare_estate.py`: `--embedded-inventory-json`, `--rebind-plan-out` / `-md` / `-csv`,
  `--rebind-strong-cut` (default `0.65`), `--rebind-cluster-threshold` (default `0.80`),
  `--view-dependency-report` (existing flags untouched). New
  `resources/rebind-plan-contract.md` documents the contract. **Deterministic, additive and
  read-only** — never changes a `tier` / `score` / `bucket`; the migration guard suite is untouched
  (`956` passed / `1` skipped / `1` xfailed). Comparison suite `327` → `383` tests (+56). Skill
  `VERSION` `1.6.0` → `1.7.0`; collection `0.8.0` → `0.9.0`.
  orchestrator. Dimension-role and row-level calculated fields translate to DAX **calculated
  columns** end-to-end; previously the translator's column mode existed but was never called, so
  those calcs were dropped before translation was attempted.
- **tableau-migration:** table-calculation → DAX translator for the subset whose addressing
  (Compute Using) is recoverable from a `.twb`/`.twbx` — `WINDOW_*`/`RUNNING_*` families plus
  `RANK`/`RANK_DENSE`, `INDEX`, `LOOKUP`, `FIRST`/`LAST`/`SIZE` — fed by a workbook addressing
  extractor and consumer. `RANK`/`RANK_DENSE` are certified against a live Fabric model
  (0/616 mismatches; Skip-vs-Dense tie semantics confirmed on-engine). A datasource-only
  migration still preserves table calcs as stubs.
- **tableau-migration:** Tier-1 "second compiler" for calcs the deterministic Tier-0 compiler
  punts on — a deterministic router (`translation_router.py`) classifying each stub into a stable
  fallback taxonomy, a candidate-DAX validation gate (`check_candidate_dax`), a structured
  translation-handoff manifest, parameter model-object emitters (`parameters.py`: field
  parameters + what-if value parameters from `[Parameters].[X]`-driven `CASE`/`IF` swaps),
  `approved_calc_dax` landing, and a reconciliation value-oracle (`translation_reconcile.py`).
  Boundary documented in `resources/tier1-charter.md`. All report additions are additive.
- **Packaging / install:** self-verifying installers (`install.ps1` / `install.sh`) that register
  the plugin and **prove** it loaded (`copilot plugin list`), plus canonical `INSTALL.md` and
  `UNINSTALL.md` (recommended plugin path, surface matrix, verification, and the demoted manual
  folder-copy with a no-auto-scan warning).
- **Drift guards:** `tests/test_mirror_parity.py` now covers all three skills (parametrized), and a
  new `tests/test_manifest_sync.py` asserts the paired `marketplace.json` / `plugin.json` manifests
  are byte-identical, parse, and resolve their `source` + skill paths.
- **tableau-migration:** the skill now leads with a **gated runbook** (GATE RULES, Phase 0A
  Decision Menu D1–D5, credentials form, Confirmation Ledger, and a 3-step
  fetch → migrate → deploy sequence with `--help`-verified flags and per-step checkpoints), plus a
  committed `migration.vars.example.ps1` template (git-ignored `migration.vars.local.ps1` for real
  values).
- **All skills:** an `AUTH MODEL` banner at the top of each `SKILL.md` to stop cross-skill auth
  bleed (migration = PAT default / JWT opt-in; profiler = PAT or Connected-App JWT; landing-zone =
  Connected App via the sidecar).

### Changed
- **tableau-migration:** the `TranslatedBy` provenance annotation on deterministically-translated
  measures now reads `deterministic`, matching the calculated-column path (previously an internal
  project codename leaked into emitted TMDL). No report keys were renamed or removed.
- **tableau-migration:** refreshed `resources/feature-parity.md` Calculations section to reflect
  the translator's actual behavior — `FIXED` and table-scoped LOD, row-level calculated columns,
  scalar date/string functions as columns, and `CASE`/`WHEN` → DAX `SWITCH` all translate;
  `INCLUDE`/`EXCLUDE` LOD and regex remain stubs; parameter-driven `CASE`/`IF` swaps map to field
  parameters via the second-compiler path.
- **tableau-migration:** internal terminology cleanup across code comments, docstrings, and
  resource docs (removed internal play-numbering; the Tableau-Fabric-AI-Bridge attribution is
  retained).
- **README / install docs:** the plugin marketplace path is now Option 1 ("Recommended — works on
  current GitHub Copilot CLI"); the folder-copy method is demoted with an explicit warning that
  current GitHub Copilot does **not** auto-scan `~/.copilot/skills/`. Added a surface matrix and
  replaced "ask the agent what skills it has" with a real `/plugin list` + `/skills list` check.
- **Agent convention files:** `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, and `.windsurfrules` gained
  a short "Install / consume (for agents)" block with the two install commands and a link to
  `INSTALL.md`.
- **tableau-datasource-profiler:** `SKILL.md` now references its bundled scripts by skill-relative
  paths (`requirements.txt`, `scripts/...`) instead of hardcoded `.github/skills/...` paths.
- **tableau-migration:** `resources/self-update.md` wording standardized so the loaded-folder is the
  canonical install location and `~/.copilot/skills/tableau-migration` is a manual-only fallback.

### Fixed
- **All three skills:** trimmed every `SKILL.md` `description` to fit GitHub Copilot's 1024-char
  frontmatter cap (they were 1369 / 1331 / 1333 chars). Over-limit descriptions are dropped
  silently — the plugin installs and `plugin list` shows it, but the skills never register in a
  session, so the agent fell back to reading the repo and improvising instead of running the
  skill. Verified the trimmed skills now load via the plugin path. Added
  `tests/test_skill_frontmatter.py` to assert `name` <= 60 and `description` <= 1024 for every
  SKILL.md (canonical and mirrored) so this can't regress.
- **tableau-mcp-landing-zone:** corrected the default `tableauMcpImage` pin. The previous
  default `:2.4.3` returns `MANIFEST_UNKNOWN` on GHCR (published stable tags jump 2.2.4 ->
  2.7.4), so a fresh deploy could not pull the image. Now defaults to the readable tag
  `:2.7.4` (still overridable) consistently across `main.bicep`, `azuredeploy.json`,
  `main.parameters.json`, and `deploy.ps1`, with the resolved `@sha256:` digest recorded as a
  hardening opt-in (template comment + `deploy-azure.md`).
- **tableau-mcp-landing-zone:** fixed the sidecar `UPSTREAM_MCP_URL` path. tableau-mcp 2.x
  serves Streamable HTTP at `/tableau-mcp` (older tags used `/mcp`); the stale path returned an
  Express 404 ("Cannot POST"). Updated in `main.bicep`, `azuredeploy.json`, and the local
  `docker-compose.yml`.
- **tableau-mcp-landing-zone:** set `ENABLE_MCP_SITE_SETTINGS=false` for the official server.
  2.7.x runs a startup site-settings probe needing the `tableau:mcp_site_settings:read` scope a
  direct-trust Connected App typically lacks, which 500'd the `initialize` handshake; disabling
  it skips only that read (the curated tool set still registers). Verified end-to-end against a
  live 2.7.4 deploy.

## [0.3.0] - 2026-06-10

A minor, additive release on the collection's own track (independent of any upstream
versioning). The four packaging manifests move 0.2.0 -> 0.3.0; per-skill stamps move
`tableau-migration` 1.1.0 -> 1.2.0 and both `tableau-datasource-profiler` and
`tableau-mcp-landing-zone` 1.0.0 -> 1.0.1. The deprecated `tableau-migration` plugin alias is
retained.

### Added
- **tableau-migration:** additive `relationship_confidence` report artifact — per-relationship
  endpoint connectors, `cross_source` flag, weaker-of-two confidence (ID-key equality scores
  high; coarse string-dimension joins score low with a many-to-many risk note), deduped risks,
  and skipped-relationship reasons. Existing report keys are unchanged.
- **tableau-migration:** additive `calc_coverage` report artifact — per-calculated-field
  bucket (translated / assisted-approved are live; assisted-suggested / stub are inert),
  live-vs-inert totals, and deterministic and live coverage percentages (null when there are
  no calculated fields).
- **tableau-mcp-landing-zone:** `resources/mcp-clients.md` — wiring guide for the three
  code-running Copilots (GitHub Copilot CLI, Claude Code, Cursor) to the deployed or local
  MCP endpoint, plus a Workflow Selector entry.
- Repository convention files: `CHANGELOG.md`, `SECURITY.md`, `.gitleaks.toml`, `AGENTS.md`,
  `CLAUDE.md`, `.cursorrules`, and `.windsurfrules` (original content).
- Credited `microsoft/skills-for-fabric` as the packaging/convention model (structure and
  format only) in `THIRD_PARTY_NOTICES.md` and `CLEANROOM.md`.

### Changed
- **tableau-datasource-profiler:** normalized the `SKILL.md` frontmatter `description` to the
  enumerated "Use when the user wants to: (1)(2)(3)" + quoted `Triggers:` shape used across the
  other two skills; added a `## Related skills` cross-link section. Added the same within-
  collection cross-links to `tableau-migration`.

### Fixed
- **tableau-datasource-profiler:** corrected the README API list (it referenced a "Hyper" API
  the profiler does not use, and had a stray double space).

## [0.2.0] - 2026-06-10

### Added
- Aggregated the three skills (`tableau-datasource-profiler`, `tableau-mcp-landing-zone`,
  `tableau-migration`) into a single standalone collection with marketplace and plugin
  packaging.
- Vendored the Tableau MCP deploy bundle (Azure Bicep/ARM, Copilot Studio swagger, local
  docker-compose) into `tableau-mcp-landing-zone/assets/`.
- Kept a deprecated `tableau-migration` plugin alias so pre-0.2.0 installs keep resolving.

### Changed
- Rewrote `README.md`, `CLEANROOM.md`, `THIRD_PARTY_NOTICES.md`, `requirements.txt`, and all
  four JSON manifests for the aggregated collection (version 0.2.0).
- **tableau-migration** reached content version 1.1.0: workbook inputs, multi-datasource
  selection, and default-direct rebuild with a land-to-Delta fallback.

## [0.1.0] - pre-aggregation baseline

- Initial standalone packaging of the individual skills, before they were aggregated into one
  collection. The migration skill shipped its deterministic safe-subset calc-to-DAX translator,
  TMDL generation from landed schema, and self-contained Fabric deploy; the profiler and MCP
  landing-zone skills shipped their first read-only and deploy workflows respectively.
