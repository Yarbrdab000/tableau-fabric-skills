# Migration Report

What to hand the customer at the end of a migration: a concise, auditable account of what was rebuilt,
what was approximated, and what they must finish. Emit this in the orchestrator's **Final** phase, after
[validation-reconciliation.md](validation-reconciliation.md).

> **Be honest about gaps.** The report's value is that every approximation and every stub is listed, with the
> original Tableau formula preserved, so nothing migrates silently wrong. Never report "100% migrated."

---

## Sections

### 1. Summary

- Datasource name, target Fabric workspace + semantic model name.
- Storage mode chosen and the one-line `decision["rationale"]`.
- Counts: tables, columns, relationships, measures (translated vs stubbed), measures verified.

### 2. Model

| Item | Migrated | Notes |
|---|---|---|
| Tables | n / n | one per `table`/`custom_sql` relation |
| Columns | n | typed from source schema |
| Relationships | n | inferred from hidden join keys, oriented by real cardinality |

#### Relationship confidence (`relationship_confidence`)

The report carries a machine-readable `relationship_confidence` manifest that explains, per relationship,
**why it was created** and **how much to trust it** — so a reviewer can sanity-check the join graph instead
of taking it on faith. It is additive: it sits alongside the existing `relationships` list and grades the
same edges one-for-one.

- **`created[]`** — one entry per authored single-column equality lifted from Tableau's object-graph
  `<relationships>`. Each records both endpoints' **own** connector (`from_connector` / `to_connector`) and a
  `cross_source` flag, so a heterogeneous federation (e.g. Azure SQL + Snowflake + Databricks in one
  composite model) is reported per table, never collapsed to a single datasource-level class.
- **`confidence`** — `high` / `medium` / `low`, taken as the **weaker** of the two endpoint keys (an edge is
  only as strong as its softer side). An ID-like name or an integer key grades `high`; a numeric/date key is
  `medium`; a coarse string/boolean dimension key grades `low` and gets an explicit many-to-many note in
  `risks[]`. Example: `Orders.Order_ID = RETURNS.ORDER_ID` → `high`; `Orders.Region = people.Region` → `low`
  with a "potential many-to-many" risk a reviewer should confirm.
- **`skipped[]`** — candidates the resolver dropped (composite/calculated key, unresolved endpoint, ambiguous
  orientation), each with the reason verbatim, so nothing is silently discarded.
- **`summary`** — counts of created/skipped edges and the high/medium/low confidence breakdown.

Surface the `low`-confidence and `skipped` rows in the customer report as relationships to review.

### 3. Calculated fields

One row per calc:

| Tableau field | Status | DAX / reason |
|---|---|---|
| Profit Ratio | ✅ translated · verified | `DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))` |
| Sales LOD | ⚠️ stub | `unsupported character '{'` — formula preserved |

Pull `status` from the translator (`reason`) and the reconciliation result (verified / mismatch /
not-evaluated). Every stub keeps its `TableauFormula` annotation in the model.

### 4. Connection

- Connector, server/database, mode (Import / DirectQuery), and whether a native query was preserved.
- `manual_followups`: credentials to enter, gateway to set up, custom SQL to review for folding.

### 5. Reconciliation

- Verified measures (numbers matched Tableau VDS).
- Mismatches (with both values + the filter context used).
- Could-not-evaluate (and why).

### 6. Not migrated (by design — v1)

Calculated columns, sets/groups/bins, what-if parameters, calc groups, field parameters, perspectives, and
**worksheets/dashboards** (roadmap v2). (Hierarchies, display folders, and RLS roles **are** rebuilt — see
[model-enrichment.md](model-enrichment.md).) See [feature-parity.md](feature-parity.md).

---

## Audit guarantees to state explicitly

- **Types** came from the source schema, never inferred.
- **Every** calculated field's original formula is preserved as a `TableauFormula` annotation — translated
  or not.
- Translated measures carry `TranslatedBy`; stubs are inert `= 0` until a human repairs them.
- No credentials are stored anywhere in the model, the report, or the repo.

---

## Format

Plain Markdown is fine. Keep raw `.tds`/`.twb` contents and any credentials **out** of the report (see
[security-governance.md](security-governance.md)). The report should be safe to share with stakeholders.
