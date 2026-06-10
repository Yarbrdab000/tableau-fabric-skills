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
