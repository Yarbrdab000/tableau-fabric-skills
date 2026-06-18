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
- **tableau-migration:** row-level / dimension calc translation is now wired into the
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
