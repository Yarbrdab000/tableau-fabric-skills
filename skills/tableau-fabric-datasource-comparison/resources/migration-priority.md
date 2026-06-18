# Migration priority — what to rebuild first

The comparison engine ([`comparison-methodology.md`](comparison-methodology.md)) answers one
question: *does this Tableau datasource already exist in Fabric?* That tells a customer **how much**
they must rebuild, but not **what to rebuild first**. A datasource that powers 40 dashboards is a
very different migration line item from one with **zero or one** attached workbook — the latter is a
deprioritize / retire candidate even if it needs a full rebuild. The migration-priority signal adds
that second axis: **downstream impact**.

It is computed in [`scripts/priority.py`](../scripts/priority.py) (pure, offline) from a `usage`
block that [`scripts/tableau_inventory.py`](../scripts/tableau_inventory.py) attaches to each
datasource. All output keys are **additive** to the report schema.

## Where usage comes from — Metadata API primary, REST fallback

> **The Tableau Metadata API (Catalog) is the trusted primary source.** In a real migration effort
> the assets that matter are catalogued, so a single `publishedDatasourcesConnection` GraphQL query
> returns, per datasource, its `downstreamWorkbooks`, `downstreamSheets`, and `downstreamDashboards`.
> Those counts are trusted as-is.

A thin REST fallback covers the tail. Catalog indexing lags — a datasource published minutes ago may
not be crawled yet — so for any datasource the Metadata API did **not** return, the gatherer
enumerates the site's workbooks and their `/connections` and counts the ones whose
`connection.datasource.id` is that published datasource's luid. (Embedded connections carry a
different id and are ignored, so only genuine published-datasource usage is counted.)

`--usage {auto,metadata,rest,off}` selects the strategy:

| Mode | Behaviour |
|---|---|
| `auto` (default) | Metadata API for everything it indexed; REST fills only the not-yet-indexed luids. **Never** takes the max of the two — the catalogued count is authoritative. |
| `metadata` | Metadata API only (datasources Catalog has not indexed report `Unknown`). |
| `rest` | REST workbook-connection count only (workbook count only; no sheet/dashboard detail). |
| `off` | Skip usage entirely; everything is `Unknown` / `Unprioritized` and the priority report section is omitted. |

The `usage` block is `{workbook_count, sheet_count, dashboard_count, source}` where `source` is
`metadata`, `rest`, or `none`. REST-sourced rows carry a workbook count only (`sheet_count` /
`dashboard_count` are `null`).

## Banding — `usage_priority`

Workbook count maps to a usage label (thresholds are overridable; defaults shown):

| Label | Rule (default) | Intent |
|---|---|---|
| `High` | `workbook_count ≥ 5` | heavily used — migrate first |
| `Medium` | `workbook_count ≥ 2` | used — migrate |
| `Low` | `workbook_count == 1` | one consumer — **deprioritize** |
| `Unused` | `workbook_count == 0` | orphan — **retire candidate** |
| `Unknown` | not gathered / not catalogued | never guessed |

This encodes the rule directly: **0 or 1 attached workbook is deprioritized.** Sheets and dashboards
are reported for context but do not change the band (a datasource with no workbooks has no
dashboards to lose).

## Fusion with the verdict — `migration_priority`

`priority.py` fuses the comparison **bucket** with the usage label into a single actionable ranking:

| Condition | `migration_priority` |
|---|---|
| bucket `already_exists` (any usage) | `Reuse (already in Fabric)` |
| `rebuild`/`partial` + `High` | `P1 - migrate first` |
| `rebuild`/`partial` + `Medium` | `P2 - migrate` |
| `rebuild`/`partial` + `Low` | `P3 - deprioritize` |
| `rebuild`/`partial` + `Unused` | `P4 - retire candidate` |
| `rebuild`/`partial` + `Unknown` | `Unprioritized` |

A datasource that already has a Fabric equivalent never needs migrating, whatever its usage —
reuse wins. Everything that must be rebuilt or reconciled is ordered by downstream impact, so the
busy datasources surface at the top of the worklist and the orphans fall to the bottom.

## In the report

- `summary.by_priority`, `summary.by_migration_priority`, and `summary.usage_thresholds` roll up the
  estate (always present).
- Each `matches[]` row gains `usage`, `priority`, and `migration_priority`.
- The Markdown report adds a **"Migration priority (what to rebuild first)"** table — the
  rebuild/partial datasources sorted P1 → P4 — plus a one-line `By migration priority:` rollup. Both
  are omitted when no usage was gathered, so the deterministic report is unchanged.
- `priority.rebuild_worklist(result)` returns the same sorted rebuild/partial list for programmatic
  use (e.g. handing P1/P2 to the `tableau-migration` skill first).

## Why no separate regex/LLM tier here

Usage is a **count**, not a semantic judgement — there is nothing for a second matcher to adjudicate.
The only ambiguity is *coverage* (has Catalog indexed this datasource yet?), which the deterministic
Metadata-primary + REST-fallback design resolves directly. The LLM-optional tier
([`llm-adjudication.md`](llm-adjudication.md)) still governs the **match** decision; priority simply
ranks whatever the match verdict produced.
