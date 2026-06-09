# Model-Object Enrichment (Hierarchies, Display Folders, RLS)

How the skill rebuilds the **semantic-model objects** that the core table/column/measure rebuild
([semantic-model-rebuild.md](semantic-model-rebuild.md)) does not emit: **hierarchies**, **display
folders**, and **row-level security (RLS) roles**. The generators live in `scripts/tmdl_generate.py`; the
orchestrator wires them in `scripts/assemble_model.py`. This is additive and **fully backward compatible** —
a datasource with none of these objects produces byte-for-byte the same model as before.

> **Primary source for the TMDL grammar:** Microsoft's official Tabular Model Definition Language docs —
> [TMDL overview](https://learn.microsoft.com/en-us/analysis-services/tmdl/tmdl-overview) and the
> [Tabular Object Model (TOM)](https://learn.microsoft.com/en-us/analysis-services/tom/introduction-to-the-tabular-object-model-tom-in-analysis-services-amo).
> Hierarchies and display folders live **inside** their parent table file; each role is its **own** file
> under `definition/roles/` and is referenced from `model.tmdl` with `ref role`.

---

## What is auto-derived vs. flagged

`migrate_tds_to_semantic_model(tds_text, ...)` auto-derives all three object kinds from the `.tds`, resolves
their field references against the rebuilt model, and emits TMDL. Every object is **resolved or reported** —
nothing is silently dropped.

| Object | Auto-derived when… | Flagged / skipped when… |
|---|---|---|
| **Hierarchy** | A Tableau drill path's levels **all** resolve to columns in **one** table | A level can't resolve, or levels span >1 table → skipped + reported |
| **Display folder** | A folder member resolves to a rebuilt column **or** an emitted measure | The member resolves to nothing → reported as unresolved |
| **RLS role** | A user filter is **wired** as a data-source filter | A `USERNAME()`/`USERDOMAIN()`/`ISMEMBEROF` calc is **not** wired → reported as `unwired`, no role |

The per-run audit lives in `report["model_objects"]` with `display_folders`, `hierarchies`, and `rls`
sub-reports (`translated` / `manual_review` / `unwired`).

### Field-reference shapes

Every field reference parsed from the `.tds` — drill-path levels, folder items, calculation column
names, and filter `column` attributes — is normalized to its **local token** before resolution. Real
Tableau documents reference a field in several shapes, all of which collapse to the same local token:

- **Bare** — `[Category]`.
- **Caption-qualified** — `[Parameters].[Base Salary]`, `[Sample - Superstore].[Sales]`. The qualifier
  is a datasource caption and may contain **spaces and dots**; the brackets are the only delimiter, so
  the parser never splits on `.`.
- **Internal federated-id qualified** — `[federated.0hgpf0j1fdpvv316shikk0mmdlec].[Sales Target]`. Blend
  / secondary references use the **internal** datasource id (not the caption), and that id contains a
  dot *inside* the brackets. Only the trailing bracketed segment (`Sales Target`) is taken.

The same normalization is applied to the calc column name and the data-source `<filter column>`, so RLS
wiring matches regardless of whether either side is qualified. Folder `role` attributes and other
decorative attributes are ignored. A formula table-scan (used for fail-closed RLS) is likewise
qualifier-aware, so a qualifier segment is never mistaken for a field.

**Case-insensitive fallback.** Field captions resolve **exactly** first (unchanged behavior). On an
exact miss the token is retried **case-insensitively** against the rebuilt columns — real workbooks
reference one physical field with drifting case across sheets and blends (`[Order_ID]` vs `[ORDER_ID]`).
The fallback resolves **only when exactly one** column matches case-insensitively; a lowercase name
shared by two columns stays unresolved (fail-closed) rather than being guessed between.

> **Boundary.** The trailing-token + case-insensitive resolution covers field-name drift, but it does
> **not** use the qualifier to pick between same-named fields in different datasources (e.g. a blend
> rename like `[Region (people)]`). Disambiguating duplicate field names by *relation* depends on the
> shared field resolver (`connection_to_m.build_m_field_resolver`); when a blend reference is genuinely
> ambiguous the object is reported unresolved / fails closed rather than binding to the wrong table.

---

## Hierarchies (Tableau drill paths → TMDL `hierarchy`)

A `<drill-paths><drill-path name='…'><field>[…]</field>…` becomes a table-child `hierarchy` whose ordered
`level`s reference the rebuilt (cleaned) column names:

```tmdl
	hierarchy 'Product Hierarchy'
		lineageTag: <guid>

		level Category
			lineageTag: <guid>
			column: Category

		level 'Sub-Category'
			lineageTag: <guid>
			column: Sub-Category
```

The block is injected **before** the table's `partition`. A hierarchy is emitted **only** if every level
resolves to a column in a **single** table (TMDL hierarchies cannot span tables) — otherwise it is skipped
and reported, never partially emitted. Hierarchy and level names are de-duplicated within their parent.

---

## Display folders (Tableau field folders → `displayFolder`)

A `<folder name='…'><folder-item name='[…]'/></folder>` maps each member to the TMDL `displayFolder`
property on the corresponding **column** or **measure**:

```tmdl
	column Sales
		displayFolder: "Financials"
		dataType: double
		…
```

Members are resolved by Tableau field token: a database column resolves directly by its local name; a
calculated field resolves via its internal name (`Calculation_xxx`) → caption → emitted measure. Folder
values are always double-quoted with embedded quotes doubled (valid for every TMDL text value). Nested
folder paths are not reconstructed in v1 (folders are treated as flat).

---

## Row-level security (Tableau user filters → TMDL `role`)

Tableau enforces row-level security with a **boolean calc** (e.g. `[Region] = USERNAME()`) **wired as a
data-source filter**. The skill only treats a calc as RLS when **both** are true: the formula references a
user function (`USERNAME` / `USERDOMAIN` / `ISMEMBEROF` / `ISUSERNAME` / `FULLNAME`) **and** a datasource
`<filter>` references it. This is the enforcement boundary — an unused user-function calc is reported as
`unwired`, never turned into a security object.

### Translatable filters

`[Field] = USERNAME()` (either operand order) maps to a DAX table-permission filter. `USERNAME()` becomes
`USERPRINCIPALNAME()`:

```tmdl
role 'Region Access'
	modelPermission: read
	lineageTag: <guid>

	tablePermission Orders = 'Orders'[Region] = USERPRINCIPALNAME()

	annotation TableauUserFilter = [Region] = USERNAME()
	annotation TableauIdentityFunction = USERNAME() mapped to USERPRINCIPALNAME(); verify the column holds the UPN
```

DAX table and column references are escaped independently of TMDL identifier quoting (`'` doubled in table
names, `]` doubled in column names).

### Manual-review filters (fail closed)

Anything without a safe deterministic DAX equivalent — `ISMEMBEROF` group logic, `USERDOMAIN()`, compound
boolean expressions, or an unresolvable field — is **never guessed** and **never dropped**. It becomes a
**fail-closed** scaffold that denies all rows until a human completes it:

```tmdl
role 'Manager Access'
	modelPermission: read
	lineageTag: <guid>

	tablePermission Orders = FALSE()

	annotation TableauUserFilter = ISMEMBEROF("Managers")
	annotation RequiresManualReview = true
	annotation ManualReviewReason = unsupported user-filter expression (no safe DAX equivalent)
```

`FALSE()` is applied to **every** emitted data table (not just one), so an untranslatable role
cannot leak rows from an unfiltered table. The skill never emits an annotation-only role with no
`tablePermission` (that would read as unrestricted): if no data tables are known to restrict, the
resolver **refuses to emit the role** (raises) rather than producing one that silently grants full
access.

### RLS manual-review caveats

- **Identity semantics.** Tableau `USERNAME()` may return a username, email, or domain-qualified identity
  depending on the Tableau authentication configuration; Power BI `USERPRINCIPALNAME()` returns the UPN. A
  translated rule is **syntactically** correct but only **behaves** correctly if the referenced column holds
  the UPN. Verify the entitlement column during reconciliation (the `TableauIdentityFunction` annotation
  flags this).
- **Group membership.** `ISMEMBEROF('Group')` has no row-filter equivalent in Power BI — RLS groups are
  assigned to roles in the workspace/tenant, not expressed in DAX. These always require manual completion.
- **Re-creating RLS incorrectly is worse than not creating it.** The fail-closed scaffold preserves the
  original intent and forces a deliberate review rather than approximating a security boundary. See
  [security-governance.md](security-governance.md).

---

## Public API (for the integrator)

All new keyword arguments are **optional** and default to `None`; omitting them reproduces the prior
behavior exactly. Existing positional parameters and return shapes are unchanged.

### `tmdl_generate.py`

| Function | Purpose |
|---|---|
| `parse_model_objects(tds_text)` | Parse RAW hierarchies / folders / user filters + a field index from a `.tds` |
| `resolve_model_objects(parsed, resolve_field, *, calcs=None, data_tables=None)` | Resolve RAW objects to a model; returns resolved structures + an audit `report` |
| `make_case_insensitive_resolver(resolve_field, ci_index)` | Wrap a resolver with an unambiguous case-insensitive fallback (exact match still wins) |
| `translate_user_filter_to_dax(formula, resolve_field)` | `(dax \| None, table \| None, reason)` for a user filter |
| `generate_hierarchy_tmdl(name, levels)` | Render one table-child `hierarchy` block |
| `generate_role_tmdl(role)` | Render one `role` file |
| `enrich_table_tmdl(table_tmdl, *, display_folders=None, hierarchies=None)` | Inject `displayFolder`/`hierarchy` into a rendered table |
| `generate_model_tmdl(table_names, expr_name, role_names=None)` | DirectLake `model.tmdl`, now with optional `ref role` |

### `assemble_model.py`

| Function | New optional kwargs | Notes |
|---|---|---|
| `migrate_tds_to_semantic_model(tds_text, *, model_name, calcs, relationships, hierarchies, display_folders, rls_roles)` | `hierarchies`, `display_folders`, `rls_roles` | **Auto-derives** all three from the `.tds` when none are passed; passing any one disables auto-derivation. Adds `report["model_objects"]`. |
| `assemble_import_model(descriptor, *, …, hierarchies, display_folders, rls_roles)` | same | Accepts **resolved** structures: `display_folders={table:{member:folder}}`, `hierarchies={table:[…]}`, `rls_roles=[role,…]`. |
| `assemble_directlake_model(*, …, hierarchies, display_folders, rls_roles)` | same | Same resolved shapes, keyed by the caller's display names + landed Delta column names. |

**Resolved shapes**

```python
display_folders = {"Orders": {"Sales": "Financials"}, "_Measures": {"Profit Ratio": "Financials"}}
hierarchies     = {"Orders": [{"name": "Product Hierarchy", "levels": [("Category", "Category"), ...]}]}
rls_roles       = [{"name": "Region Access",
                    "table_permissions": [("Orders", "'Orders'[Region] = USERPRINCIPALNAME()")],
                    "annotations": [("TableauUserFilter", "[Region] = USERNAME()")],
                    "requires_manual_review": False}]
```

---

## IP provenance

The hierarchy / role / displayFolder grammar comes from Microsoft's official TMDL documentation (linked
above). The Tableau drill-path ↔ hierarchy, folder ↔ displayFolder, and user-filter ↔ RLS correspondences
are factual language-to-language equivalences. No third-party source code, structure, naming, comments, or
test data were copied. See `THIRD_PARTY_NOTICES.md` at the repository root.
