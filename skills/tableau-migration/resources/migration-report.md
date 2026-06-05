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

Hierarchies, calculated columns, sets/groups/bins, what-if parameters, calc groups, field parameters, RLS,
perspectives, display folders, and **worksheets/dashboards** (roadmap v2). See
[feature-parity.md](feature-parity.md).

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
