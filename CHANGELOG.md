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

- **tableau-migration (skill `2.88.0` -> `2.89.0`): a transfer-layer UUID prefix no longer
  becomes the asset name.** Chat/Copilot attachments, portal and ticketing downloads and SharePoint
  stamp a canonical UUID on the front of a filename. It is never part of a Tableau author's name, and
  it does real damage rather than looking untidy: 36 characters plus a separator consume most of the
  64-char filesystem-name budget, so the author's ACTUAL name is truncated and a disambiguation hash
  appended. Measured on a real run, `…-Network Operational PowerBI Mock - 24Jul26 ORC.twbx` emitted
  as `0e7f6d6d-…-c13-Network Operationa-ac65b89d` -- the meaningful part of the name survived as the
  word "Operationa". It also defeats the name-based workbook<->datasource rebind index, because two
  attachments of one asset carry DIFFERENT uuids and stop matching each other. Stripped for the
  LOCAL-FILE source only (a live/server name comes from Tableau and is authoritative), and
  deliberately strict -- the canonical 8-4-4-4-12 shape, anchored at position 0, with a separator, and
  only when a non-empty remainder survives, so a date-prefixed name like `2026-08-07-Monthly Report`
  is untouched.
- **tableau-migration (skill `2.87.0` -> `2.88.0`): an untranslated measure is `BLANK()`, never
  `0`.** A stub emitted as `= 0` is a MEASUREMENT: it renders on a card as a confident number, and
  nothing about "CSAT 0%" says the calculation was never migrated. Measured 2026-08-06 on a real
  workbook whose true value was 96%; it was the first thing every downstream agent chased. It also
  poisons dependents, because a year-over-year over a zero denominator yields an infinity or a divide
  error rather than an absence. `BLANK()` renders empty, propagates as absence through arithmetic,
  and cannot be mistaken for data. This is what a stubbed calculated COLUMN has always emitted
  (`generate_calc_column_tmdl`) -- measures were the lone inconsistency. Provenance is unchanged:
  the Tableau formula is still on the `TableauFormula` annotation.
- **tableau-migration (skill `2.86.0` -> `2.87.0`): the Tier-3 work order is REMOVED.** It shipped
  across 2.84.0-2.86.0 and is deleted here, along with its tests and its `SKILL.md` entry, because
  it was never validated against the agent it exists for and the evidence we do have argues against
  it. Recorded rather than quietly dropped, because the reasoning generalises.

  The benchmark was invalid: every measurement was taken on a generic sub-agent, not on the
  downstream migration agent the document is written for. That agent, working from the Tableau
  reference image and the Tier-2 rebuild ALONE, produced a near-perfect dashboard in 1h40m -- it
  independently found and fixed every class the work order reports (placeholder measures, an injected
  percent-of-total visual calculation, missing slicers, sort order, palette). So the document's value
  was never "it finds what the agent misses"; at best it was "sooner", and that was never measured.

  Against that, two harms were measured, both on the generic agent and both intrinsic to the format.
  A section asserting some visuals were "checked and CORRECT" was derived from "the worklist did not
  flag them" -- but the worklist records TRANSLATION failures and has no opinion on visual fidelity,
  and the correlation runs backwards, since a visual that translated cleanly is the one still wearing
  the default theme. It named the three visuals the unaided agent rebuilt. Separately, a document
  listing N items anchors a reader to those N items; the arm given the work order spent 3h and broke
  a working visual, while the unaided baseline did better in half the time.

  **The reference image remains the handoff.** `reference_images.py` and the D7 / STEP 1.6 wiring
  (2.81.0-2.83.0) are untouched: capturing the original dashboard is what the downstream agent
  demonstrably needs, and it is a fact rather than an opinion about what to do with it.
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
