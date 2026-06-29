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
- **tableau-migration:** **higher-fidelity Tableau dashboard → Power BI (PBIR) visual rebuilds.** A
  workbook's worksheets and dashboards now reproduce more of their original look: a **dual-axis**
  line/bar measure pair, **per-measure series colours** (each measure keeps its authored colour on
  bar / line / area / combo marks and in KPI multi-row cards), filled/symbol **maps** that carry the
  geographic `dataCategory` and a measure-driven colour gradient, and faithful chart-type / field /
  position binding to the migrated semantic model. Emitted only where it can be bound faithfully;
  anything ambiguous still degrades to a structured warning (warn-never-wrong). Additive; the
  migration suite stays green. Skill `VERSION` `1.10.0` → `1.11.0`.
- **tableau-migration:** **broader deterministic table-calculation → DAX coverage.** The Tier-0
  compiler now translates more Tableau quick table calcs faithfully: **Difference** and **Percent of
  Total**, the **Rank family** (`RANK` / `RANK_DENSE` / `RANK_MODIFIED`), and **moving `WINDOW`**
  aggregates with integer-literal bounds. Each is emitted only when it maps faithfully — e.g. `Unique`
  ranking (whose tiebreak depends on addressing order) and one-sided / non-integer window bounds still
  hand off rather than emit an unfaithful result. Tier-0 guarantees unchanged; the original Tableau
  formula is preserved as an annotation. Additive; the migration suite stays green.
- **tableau-migration:** **a Databricks Custom SQL relation now migrates to a deploy-valid native
  query.** A `<relation type='text'>` custom-SQL connection emits a `Value.NativeQuery(...)` M
  partition against the bound source instead of an unresolvable placeholder, so the generated model
  is structurally valid and deploys. Additive.
- **tableau-migration:** the (advisory, quarantined) fidelity oracle gained a **per-visual
  REPRODUCED / PARTIAL / DEGRADED / MISSING** scorer so a rebuilt report page can be graded visual by
  visual against its Tableau source. Lives in `tests_oracle/` and the optional oracle tooling only —
  no change to the deterministic migration runtime or its report schema.
- **tableau-migration:** **a candidate-ranking step for the assisted (second-compiler) tier**
  (`translation_reconcile.rank_candidates`) — the optional acceleration tier's *selection* helper.
  Given the N candidate DAX translations the agent (the documented second compiler) authors for one
  fallback, it reconciles each through the gate + numeric oracle and returns them **best-first**, each
  with a `confidence` (`high` = verified against the Tableau ground truth · `medium` = passed the gate
  but not yet reconciled · `low` = proven wrong or malformed) and a one-line `reason`, plus `best`
  (the top non-`low` candidate, or `None` when every candidate is low). It ranks by **semantic
  equivalence, not string similarity**, embeds **no LLM API** (the agent proposes; this scores), and
  lands nothing — the chosen candidate still flows through `approved_calc_dax` and the human gate.
  Each ranked entry carries an auditable `signals` breakdown (`{gate, oracle, category}`) and a
  `requires_oracle` flag that enforces the playbook's mandatory-oracle rule — an unverified
  `dax_language_gap` approximation is **never** returned as `best` until the oracle VERIFIES it.
  Accepts each candidate as a raw DAX string or a `suggest_assisted_dax` suggestion dict, and
  degrades gracefully — zero candidates, a `None` list, or a malformed candidate carrying no DAX
  resolve to a gate-rejected empty string, so `best` is always a landable DAX **string** or `None`,
  never a stray dict. Documented in `resources/second-compiler.md`.
- **tableau-migration:** **the assisted (second-compiler) idiom registry now recognizes the
  argmin-over-a-dimension twin** of the existing argmax idiom ("the member of dimension C with the
  *least* AGG([f]) per partition", e.g. the lowest-selling city in each state). The detector
  (`_detect_argmax_dimension` in `calc_to_dax.py`) and its LOD parser (`_parse_max_of_fixed`) now
  accept the `{FIXED P : MIN(...)}` selector and emit the same faithful, tie-aware
  `CALCULATETABLE`/`ADDCOLUMNS`/`SUMMARIZE` shape with `MINX` instead of `MAXX` (pattern
  `argmin-dimension`); the argmax branch is byte-for-byte unchanged. Suggestions remain
  approval-gated — never silently emitted. Original parameterization of our own argmax emitter
  (CLEANROOM pass).
- **tableau-migration:** **a golden-loop regression harness for the assisted tier**
  (`tests/test_assisted_golden_loop.py`) that drives a corpus of known-good translations through the
  whole Tier-1 loop end-to-end — `suggest_assisted_dax` → `check_candidate_dax` (syntactic gate) →
  `reconcile` (numeric oracle) — seeded with the argmax/argmin idioms and the canonical
  human-approved C1/C2 sidecar pair (C1 "Highest Selling City By State Sales" = 1,221,139.3614
  reconciled against ground truth; C2 gate-locked). Proves non-vacuity (a wrong oracle value
  MISMATCHes; a corrupt/inert candidate fails the gate without touching the backend) and adds a
  forcing-function test so every newly-registered idiom detector must carry a golden corpus row.
  Test-only; no engine or report-schema change.
- **tableau-migration:** **an author's explicit per-field number format now survives to the Power BI
  `formatString`.** Tableau persists a column's explicit currency / percent / precision as a
  `default-format` code on the logical `<column>` element (e.g. `c"$"#,##0;("$"#,##0)`); previously
  these were dropped and every numeric/date column fell back to the generic type-derived format. A new
  decoder (`tableau_default_format_to_pbi` in `tmdl_generate.py`) maps the code's one-char type prefix
  (`c` currency / `n` number / `p` percent / `*` zero-pad, plus the uppercase `C<lcid>%` percent form)
  to an Excel/.NET-grammar `formatString`, joined to its physical `(table, column)` through the `<cols>`
  logical→physical map (`_default_formats_by_physical` in `connection_to_m.py`) and applied by
  `generate_column_tmdl` via a new optional `format_string` parameter. An unrecognized / unmapped /
  ambiguously-mapped code is omitted so the column keeps its type-derived floor — additive and never a
  regression; with no decodable code the emitted TMDL is byte-for-byte unchanged. Grounded in a 29-`.twb`
  corpus decode table (11 distinct codes / 461 occurrences); decode logic is original (CLEANROOM pass).
- **tableau-migration:** **a pure-Python TMDL well-formedness linter** (`scripts/tmdl_lint.py`)
  plus pytest coverage (`tests/test_tmdl_lint.py`) that guards the serializer's *openability*
  invariants in-suite. It flags the three failure modes that make a generated `.tmdl` fail to open
  in Power BI / TOM — empty-value annotations, column-0 / sibling-level orphan lines outside the
  top-level keyword allowlist, and a multi-line object body (`measure` / `column` /
  `calculationItem` / `expression`) that is not indented deeper than its opener's property level
  (while correctly accepting a `source` partition value-block — an M `let`/`in` or calculated-table
  expression — at the standard one-level-deeper indent, the form TOM opens) — over both raw TMDL
  text and the real generator output. Purely a developer/CI safety net for serializer regressions;
  no runtime, report-schema, or generated-output behavior changes. — the column-mode peer of the measures' `approved_calc_dax` channel — exposed on
  the estate CLI as **`--approved-dax <file.json>`**. A `{calc_name: dax}` approval flips an inert
  calculated-column stub into a live, byte-validated calculated column
  (`TranslatedBy = assisted translation (human-approved)`, status `assisted-approved`), consulted
  **only** when the deterministic tier produced no DAX so a faithful Tier-0 column is never
  overridden; the original Tableau formula is preserved as `TableauFormula`. `approved_calc_dax` is
  threaded end-to-end through `migrate_estate` (`_migrate_one_datasource`,
  `_rebuild_from_published_match`, `_attach_workbook_pbip`), and the dimension-calc coverage rollup
  gains an additive `assisted_approved` bucket + `live_coverage_pct` (existing keys preserved). With
  no approval supplied the run is byte-for-byte unchanged; the migration suite stays green.
- **tableau-migration:** **a local `.twbx` / `.tdsx` upload is now discovered and read** by the
  file-backed estate source, so the "just upload the packaged workbook / datasource" path behaves like a
  live pull instead of silently finding nothing. `migrate_estate.LocalFilesSource` previously matched only
  the *bare* `.tds` / `.twb` extensions (an exact `splitext` compare) and read every file as UTF-8 text, so
  a packaged export — which is a **zip** — was skipped entirely (a `.twbx`-only folder reported `0/0`
  everything). It now also discovers `.tdsx` / `.twbx`, extracts the inner document **in memory** via the
  tested `fetch_tds` / `workbook_table_calcs` zip helpers (never written to disk), and de-duplicates a
  packaged export against its unpacked twin (preferring the unpacked copy) so a mixed folder yields no
  duplicate datasource. Additive; existing bare-file behavior is unchanged. (Local==live parity, discovery
  half; published / `sqlproxy` schema recovery is tracked separately.)
- **tableau-migration:** **table-calc measures now translate on the live / published-datasource path,
  reaching parity with a local `.twbx` upload.** When a workbook connects to a published Tableau Cloud
  datasource (`sqlproxy`), `migrate_estate._rebuild_from_published_match` rebuilds the model from the
  matched, already-migrated published `.tds` — which is **schema only and carries no worksheets**, so the
  table-calc *addressing* (partition / order, recovered from the worksheet shelves) was previously lost and
  positional measures (`WINDOW_STDEV`, percent-difference-from-prior, `LAST`) stubbed to `= 0`. The rebuild
  now extracts `table_calc_usages` from the **workbook** (`twb_text`) and threads them through a new additive
  `table_calc_usages=` override on `assemble_model.migrate_tds_to_semantic_model` (default `None` keeps the
  prior auto-extraction from the source text; `[]` disables it; a list overrides it). With the addressing in
  hand the existing addressed-measure path emits faithful DAX (`STDEVX.S(WINDOW(…ORDERBY…))`,
  `DIVIDE(… - CALCULATE(…, OFFSET(-1, ORDERBY…)), ABS(…))`, `COUNTROWS(WINDOW(…PARTITIONBY…)) - ROWNUMBER(…)`)
  and cross-calc references (`2 * [Standard of Deviation]`, `Difference coloring`) resolve against them. A
  local `.twbx` whose embedded model already carries its own worksheets was unaffected (it self-extracts);
  this brings the credential-based live path to the same fidelity. Genuinely un-addressable shapes
  (nested-`FIXED` LOD argmax, parameter-case filters) still fail closed. Additive; the migration suite stays
  green. Skill `VERSION` `1.9.0` → `1.10.0`.
- **tableau-migration:** the rebuilt **report page now binds its columns to the migrated model**
  instead of the workbook's embedded placeholder entity. When `_attach_workbook_pbip` recovers a
  model from a matched published datasource, a new `_field_map_from_model` helper derives a
  `field_map` from the report's `model_manifest.naming` (column entries → `{entity, property}`, the
  fact table that owns the most columns) and threads it — alongside the fact `model_table` — into the
  single `twb_to_pbir` re-run, so report columns resolve to the real model tables rather than the
  source's phantom `sqlproxy` / caption entity. Aggregation pills keep their aggregation (the
  `field_map` entries carry no `binding`), and a date axis already rebound to the model's `Date`
  table stays authoritative via a `date_rebound` guard in `_apply_override`. The report records a
  `field_rebind` detail (rebound count + model table). Additive; the migration suite stays green.
- **tableau-migration:** the deterministic **calc→DAX compiler v2** — broader faithful function
  coverage across the String / Date / Aggregate / Type-Conversion families and deeper **row-level and
  table-calculation** translation (running-total and ordered `WINDOW_*` windows, percent-difference,
  positional offsets), each preserving the original Tableau formula as a `TableauFormula` annotation
  and **failing closed** (an honest, routable fallback reason) when no faithful DAX target exists. The
  model build now also stamps deterministic **model-facts on the migration report** — a
  `model_manifest` (typed model summary + parameter classification into value / field / filter) and
  `row_count` measure facts — so the report-page build can bind slicers, visual filters and measures
  to the rebuilt semantic model **by calc id**. Additive; the migration suite stays green. Skill
  `VERSION` `1.8.0` → `1.9.0`.
- **tableau-migration:** the **Tableau dashboard → Power BI report-page (PBIR) viz consumer** now
  binds those model-facts. `migrate_estate._attach_workbook_pbip` derives date / measure / row-count /
  parameter bindings from the freshly rebuilt model and threads them as keyword arguments into the
  single `twb_to_pbir` re-run, so a migrated report page points its visuals at the real measures and
  columns instead of placeholders. A new read-only, stdlib-only `scripts/workbook_calc_usage.py`
  classifies every workbook-local calc's **intent** (measure / native conditional-formatting / filter
  / row-level column) and where the dashboard uses it, joined back to the model half by the calc's
  bare internal id — the deterministic model↔viz contract. Additive; suite green.
- **tableau-migration:** a **layered, Key-Vault-free credential resolver**
  (`scripts/credential_resolver.py`) so a local / POC migration can authenticate to Tableau with no
  Azure Key Vault. `resolve_secret(...)` resolves a secret (e.g. a Tableau PAT's secret value) from
  the first configured-and-available layer, in order: an explicit value → a process environment
  variable → the same key in a git-ignored `.env` file → an OS-keyring secret (Windows Credential
  Manager / macOS Keychain / Secret Service via the optional `keyring` package, imported lazily) →
  an interactive `getpass` prompt (opt-in and TTY-guarded, so unattended runs never hang). The
  resolved value is returned to the caller only — never logged, persisted, or written to the report;
  the returned `ResolvedSecret` redacts its value in `repr`, and `CredentialNotFound` lists only the
  layers tried. `migrate_estate.LiveTableauSource` gains additive keyword-only params (`pat_value`,
  `pat_env_var` defaulting to `TABLEAU_PAT`, `env_file`, `keyring_service`, `allow_prompt`, each with
  a pointer env-var fallback) and its `_resolve_pat` now delegates to the resolver, falling back to
  the enterprise Azure Key Vault seam (`_resolve_pat_from_key_vault`) only when no local layer is
  configured. `describe()` is unchanged (no secret-bearing keys). Additive; the migration suite stays
  green. Skill `VERSION` `1.7.0` → `1.8.0`.
- **tableau-migration:** an additive, **opt-in local-data POC path** so a Tableau extract whose
  source connector has no live Power BI equivalent (S3 / MinIO, generic ODBC, Web Data Connector)
  can still be turned into a **clickable local Power BI Import model backed by real data** — no
  Microsoft Fabric, no lakehouse, no Azure Key Vault. `migrate_datasource(...)` gains a `local_data=`
  parameter accepting a `{table: csv}` map, a directory of `*.csv`, a single `.csv`, a
  `.hyper`/`.tdsx`/`.twbx` file, or `True` (auto-extract the source's own `.hyper`). When supplied it
  routes the datasource down the proven `Csv.Document` flat-file Import generator
  (`assemble_local_import_model` in `scripts/assemble_model.py`), reusing typed columns, calc→DAX
  measures, the Date dimension, relationships and parameters unchanged, and each table's partition
  points at its matched local CSV. A new optional `scripts/hyper_reader.py` (lazy `tableauhyperapi`,
  stdlib-only at import) writes one CSV per extract table for the auto-extract case; bring-your-own
  CSVs need no extra dependency. Adds the additive `report["local_import"]` key
  (`{data_source, matched, unmatched_tables, table_count, matched_count}`). When `local_data` is
  absent the run is a **byte-identical no-op** — the existing land-to-Delta fallback is unchanged.
  Additive; the migration suite stays green. Skill `VERSION` `1.6.0` → `1.7.0`.
- **tableau-migration:** Tier-1 Tableau **dashboard → Power BI** migration — workbook worksheets
  and dashboards are rebuilt as Power BI report pages in the PBIR/`.pbip` format
  (`scripts/twb_to_pbir.py`), wired into the estate driver (`scripts/migrate_estate.py`) so a
  migrated datasource's report is assembled and bound by-path alongside its semantic model. Adds a
  Tier-2 **image-oracle** verification harness (`scripts/image_oracle.py`, runbook
  `resources/image-oracle.md`) that checks rebuilt-report fidelity, plus viz-engine robustness
  (implicit row-count rollup, structural worksheet titles, additional chart-type mappings) and a
  `list_workbook_datasources` helper / additive `project_name=` argument on
  `write_local_pbip`. Additive; the migration suite stays green. Skill `VERSION` `1.5.0` → `1.6.0`.
- **tableau-migration:** the estate orchestrator (`scripts/migrate_estate.py`) gains an additive,
  **opt-in `rebind_plan=` parameter** that ingests a comparison-emitted `rebind-plan.json`
  (`schema_version "1.0"`) and writes a single `compile-report.json`. When the parameter is absent the
  run is a **byte-identical no-op** (no `compile-report.json`; `report.json` unchanged). When a plan is
  supplied the router consumes the frozen string-form contract (entries under `plan["plan"]`,
  `source_ref` the bare `source_id` join-key string, `label`/`workbook_luid`/`model_id` top-level
  siblings), routes each entry by `binding_status` **first** (`existing_fabric` → byConnection,
  `built_local` → byPath, `landed_to_delta`/`needs_attention` → deferred/unbound), resolves each routed
  source via `migrate_datasource(datasource=label)` reusing the model the estate pass already built,
  and calls the dashboard per-report bind seam through a pluggable/auto-detected callable (passing the
  shared `used_folders` accumulator). The bind seam stays **deferred** until the dashboard stage lands
  its bind function, so routed entries are recorded as deferred rather than guessed — keeping the run
  safe, green, and disjoint from the dashboard's binder functions. The JSON file is the only coupling
  (nothing is shelled); the comparison-owned plan is never mutated. Additive; the migration suite stays
  green. Skill `VERSION` `1.4.0` → `1.5.0`.
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
- **tableau-migration:** **a migrated model's generated `Date` relationship no longer disappears on
  first refresh** (which had silently flatlined every time series). Two independent root causes in the
  Import/M emit path are fixed, both pure `.tds`-metadata so they behave identically for Import,
  DirectQuery, federated and flat-file: (1) authored object-graph relationships are translated as
  **many-to-many, single-direction dim→fact** instead of the default many-to-one — Power BI's
  unique-key check on a non-unique join (e.g. `Returns[Order_ID]` with duplicates) was rejecting the
  whole relationship batch and collateral-dropping the valid `Orders → Date` sibling; and (2) the M
  column emitter no longer writes a bogus `sourceLineageTag` (M columns bind via `sourceColumn`, not a
  schema), which had made Desktop treat the binding as speculative and drop relationships on refresh.
  The generated `Date` relationship stays many-to-one (its key is unique by construction). Additive
  (`report["relationships"]` gains a `cardinality` key); the migration suite stays green.
- **tableau-migration:** **Custom SQL is now de-escaped at the parse boundary, fixing a refresh-time
  type error.** When Tableau serializes a Custom SQL relation it **doubles every literal angle
  bracket** (`<`→`<<`, `>`→`>>`) to escape them from its own `<[Parameters].[Name]>` syntax; emitting
  that doubled form verbatim corrupted the query on Spark/Databricks, where `<<` / `>>` are the bitwise
  shift operators (so a predicate like `Profit < 0` failed `[DATATYPE_MISMATCH]` at refresh even though
  deploy succeeded). A single-chokepoint, parameter-aware global halve recovers the query the user
  actually wrote (proven exact by an even-run invariant); a surviving Tableau parameter reference is
  flagged `needs_review` rather than shipped silently. Connector-independent (also corrects Snowflake /
  SQL Server custom SQL).
- **tableau-migration:** the TMDL serializer now emits an **openable** model when a measure or
  calculated column carries a **multi-line** DAX expression. A deterministic multi-line body (e.g.
  the Date Filter keep-flag's `VAR … RETURN … SWITCH(…)`) was written inline after `measure 'X' = `,
  dropping its continuation lines to **column 0** — invalid TMDL that left the model `BLOCKED`
  (unparseable by TOM / Power BI Desktop). `tmdl_generate` now renders a multi-line expression as an
  indented block (the declaration ends at `=` on its own line, body lines one level deeper than the
  property level); single-line DAX is byte-for-byte unchanged. A second defect is fixed alongside
  it: an **empty-value annotation** (`annotation TableauFormula = ` with no value, e.g. a synthesized
  measure-swap `SUM`) is now **elided** rather than emitted as unparseable TMDL. Adds 4 openability
  regression tests; the migration suite stays green.
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
