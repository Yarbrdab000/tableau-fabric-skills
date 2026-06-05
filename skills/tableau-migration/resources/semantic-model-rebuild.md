# Semantic-Model Rebuild (TMDL)

How the skill reconstructs a Tableau datasource's **data model** — tables, typed columns, and relationships
— as TMDL, the text format of a Power BI semantic model. The generators live in `scripts/tmdl_generate.py`
(model objects) and `scripts/connection_to_m.py` (the Import/DirectQuery table emitter). This is the
orchestrator's **Phase 3**; calculated fields (Phase 4) are covered in [calc-to-dax.md](calc-to-dax.md).

> **Types are driven by the source schema, never guessed.** The DirectLake path types columns from the
> landed Delta schema (`spark_type_to_tmdl`); the Import/DirectQuery path types them from the Tableau `.tds`
> `<metadata-records>` (`tableau_type_to_tmdl`). A datasource with no resolvable column metadata falls back
> to land-to-Delta — it is never deployed with inferred types.

---

## One model table per relation

`parse_tds` returns a descriptor whose `relations` list has one entry per logical source table. Only
relations of `kind` `table` or `custom_sql` become model tables; `join`/`union`/`unknown` trees trigger the
fallback (see [storage-mode-selection.md](storage-mode-selection.md)).

Each `table`/`custom_sql` relation carries a `columns` list, where every column is:

| Key | Meaning |
|---|---|
| `remote_name` | The source column name (the Tableau remote column) |
| `model_name` | `clean_col(remote_name)` — the model-facing column name, **and** the emitted `sourceColumn` |
| `tmdl_type` | `int64` / `double` / `string` / `boolean` / `dateTime` |
| `local_name` | The Tableau caption used to resolve calc references |

`emit_table_tmdl_m(relation, descriptor, mode)` renders a complete `table` block: one typed `column` per
entry (numeric columns get `summarizeBy: sum`, others `none`) plus the `= m` partition that points at the
source. It returns `None` when a relation has no resolvable columns, signalling fallback.

> The emitted `sourceColumn` is the cleaned `model_name`, so it matches the source column only when
> `clean_col` is an identity for that name. Source columns whose names contain characters `clean_col`
> rewrites (spaces, parentheses, etc.) are a known edge to verify against the live source during refresh.

---

## Column typing

| Path | Source of truth | Mapper |
|---|---|---|
| Import / DirectQuery | Tableau `<metadata-records>` `<local-type>` | `tableau_type_to_tmdl` |
| DirectLake (landed Delta) | Landed Delta/Spark schema | `spark_type_to_tmdl` |

Unsupported Tableau types (`table`, `spatial`, unknown) map to `None` and the column is **skipped** rather
than mis-typed. `clean_col` normalizes names for TMDL (the M path keeps the model column name equal to its
`sourceColumn`).

---

## Relationship inference

Tableau hides its join structure as `<Base> (<Table>)` field pairs. `infer_relationships(meta_fields,
landed_tables, count_fn)` reconstructs model relationships from those hidden join keys, using **real landed
cardinality** (`count_fn`) to orient each relationship rather than assuming a direction.

`generate_relationships_tmdl(rels)` emits one TMDL `relationship` per inferred join:

```tmdl
relationship <guid>
    fromColumn: 'Orders'.'Customer_ID'
    toColumn: 'People'.'Customer_ID'
```

Default cardinality is **many-to-one** (`from = many → to = one`), which is the common star-schema shape, so
no explicit cardinality property is emitted. Relationship **emission** is storage-mode agnostic, but the
*inference* (`infer_relationships`) needs landed cardinality (`count_fn`) to orient each join — so it is run
**separately** and the resulting relationships are passed into the assembler via its `relationships`
argument. `assemble_import_model` does not infer relationships itself; it emits the ones it is given.

---

## Assembling the full model

`scripts/assemble_model.py` is the Tier-1 orchestrator that turns descriptor + calcs into a complete Fabric
**SemanticModel** definition:

```python
from assemble_model import migrate_tds_to_semantic_model
result = migrate_tds_to_semantic_model(tds_text, model_name="Superstore", calcs=calcs, relationships=rels)
parts, report = result["parts"], result["report"]
```

`parts` is every TMDL part plus `.platform` and `definition.pbism`; `report` summarizes the storage
decision, tables emitted, skipped tables, measures (translated vs stubbed), and relationships. Two helpers
turn `parts` into a deploy or a local inspection:

- `fabric_definition_payload(parts)` → base64 `InlineBase64` parts for the Fabric `createOrUpdate` REST call.
- `write_model_folder(parts, dest_dir)` → a TMDL folder on disk for git / Desktop / review.

`assemble_import_model` (Import/DirectQuery) and `assemble_directlake_model` (the landed-Delta fallback) are
the two assemblers underneath. Deploy itself is **delegated** — see
[migration-orchestrator.md](migration-orchestrator.md) Phase 6 and `semantic-model-authoring`.

---

## What is NOT rebuilt

Calculated columns, sets/groups/bins, what-if parameters, calc groups, field parameters, and perspectives
are **not generated** by v1. The scripts do **not** auto-detect them; when the agent has the Tableau
metadata it should enumerate any the datasource uses and list them as manual follow-ups — they are never
approximated.

**Hierarchies, display folders, and RLS roles _are_ rebuilt** (additively) by the model-object enrichment
layer — auto-derived from the `.tds`, resolved against the rebuilt model, and reported in
`report["model_objects"]`. See [model-enrichment.md](model-enrichment.md) and
[feature-parity.md](feature-parity.md).
