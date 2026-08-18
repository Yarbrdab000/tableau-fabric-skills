# Migration Gotchas

Failure modes the agent will actually hit, and the deterministic response for each. Load this when a
migration step errors or produces something unexpected.

---

## Parsing the `.tds`

| Symptom | Cause | Response |
|---|---|---|
| Garbled first characters / parse error | UTF-8 BOM | Open with `encoding="utf-8-sig"` — Tableau always writes a BOM |
| `.tdsx` won't parse | It's a zip, not XML | Unzip; the `.tds` is at the root or under `Data/` |
| Relations come back `unknown` | `table` attribute isn't `[schema].[item]` / `[item]` | Parser returns `(None, None)` and flags it — route to fallback, don't guess a schema |
| A table appears twice | Modern "object model" `.tds` duplicates tables under `<properties>` and wraps them in `<relation type='collection'>` | Already handled: the parser promotes collection children as independent tables and dedupes copies |

---

## Storage mode

| Symptom | Cause | Response |
|---|---|---|
| `select_storage_mode` returns `mode = None` | Join/union tree, >1 named connection, unmapped connector, or no column metadata | Expected — route to land-to-Delta + DirectLake |
| Connector emits a "scaffold" | Snowflake/BigQuery navigation differs from the `Sql.Database` family | Review the M before refresh; the mode is right, the navigation needs a glance |
| Flat-file model has no path | Excel/CSV needs a file path the `.tds` doesn't carry | Supply the path on the M partition |

---

## Calculated fields → DAX

| Symptom | Cause | Response |
|---|---|---|
| A measure is `= 0` with only a `TableauFormula` annotation | Formula outside the safe subset (LOD, table calc, CASE, scalar date/string fn, 4-arg IIF, cross-table) | Expected stub — repair manually or via a validation-gated LLM pass |
| A simple-looking calc still stubs | Bare row-level field in a measure (e.g. `[Sales]` not `SUM([Sales])`), or mixed-type IF branches | Measure context requires aggregations; make branch types consistent |
| `COUNTD` is off by one vs Tableau | Plain `DISTINCTCOUNT` counts BLANK | Already handled — translator emits `DISTINCTCOUNTNOBLANK` |
| `COUNT` over a text column errors | DAX `COUNT` is numeric-only | Already handled — translator emits `COUNTA` |
| An empty aggregation reads as 0 not NULL | DAX BLANK coercion vs Tableau three-valued NULL | Known difference; reconciliation flags it (see [calc-to-dax.md](calc-to-dax.md)) |

---

## Connection binding

| Symptom | Cause | Response |
|---|---|---|
| Refresh fails on credentials | Credentials are a manual boundary | **Stop** and have the user configure the connection; never enter credentials for them |
| DirectQuery to on-prem fails | No gateway | User selects/sets up an on-prem data gateway |
| A custom-SQL table is slow / materializes | The native query didn't fold | Review the `Value.NativeQuery(..., [EnableFolding=true])`; fix the SQL so it folds |
| Custom SQL has doubled comparison operators (`Profit << 0`, `<<>>`, `<<=`) and refresh fails on Databricks with `DATATYPE_MISMATCH` | Tableau **doubles every literal `<`/`>`** in Custom SQL when it serializes the `.tds` (a global replace that also hits comments + string literals), then halves them back on read; on Spark `<<`/`>>` are bitwise shift operators. Parameter-reference delimiters are the exception — they serialize with **single** brackets | The migrator reverses this **once at the parse boundary** (`_deescape_custom_sql`: global halve, parameter-aware) so the emitted query is single-operator. If you hand-extract SQL from a raw `.tds`, halve `<<`→`<` and `>>`→`>` yourself — never emit the doubled form |
| Custom SQL still contains a `<[Parameters].[Name]>` token after de-escape | A Tableau parameter reference (single delimiters, bracketed `Parameters`, e.g. `<[Parameters].[Parameter 0014036665946123]>`); we don't yet translate it to a Power Query parameter | The partition is still emitted but flagged `needs_review` with the token named — replace it with a literal or a bound parameter before refresh |
| Databricks/Snowflake custom SQL: "Native queries aren't supported by this value" | The native query was folded against the connector's **root collection** (`Databricks.Catalogs(...)`), which doesn't expose that capability | Drill to a `Kind="Database"` handle first (`Catalog = Source{[Name=<catalog>, Kind="Database"]}[Data]`) and run `Value.NativeQuery` against **that** handle — this is what the migrator now auto-emits for Databricks |
| Custom-SQL columns load blank / "column not found" on refresh | A native query returns the **raw source headers** (`Order ID`, `Country/Region`) but the model binds underscored `sourceColumn`s (`Order_ID`) | The migrator appends `Table.RenameColumns(..., MissingField.Ignore)` remote→model so the output names match; complete the same rename by hand on any still-scaffolded partition |
| First open of a custom-SQL model shows a Run/Cancel "approve this native query" prompt | A deliberate Power BI **native-query security gate**, not a failure | Click Run once (Desktop) or set the dataset's native-query/data-source security setting (Service). It can't be suppressed at the M level — expect it for any `Value.NativeQuery` |

---

## The `.pbip` is a JSON *pointer*, NOT a ZIP — never repackage it

> **Read this before you ever conclude a `.pbip` is "broken."** A real run corrupted correct,
> openable output because the agent assumed a `.pbip` was a zip and re-zipped it. It is not a zip.

A `.pbip` is a **small (~300-byte) plain-text JSON pointer file**. This is the **correct, complete,
openable** output — a tiny `.pbip` is *by design*, not a truncated or "un-zipped stub." The actual
report and model live in the **sibling folders** next to it:

```
Simple_Example.pbip              ← ~300-byte JSON pointer (CORRECT — do not touch)
Simple_Example.Report/           ← the rebuilt report (.platform, definition.pbir, definition/…)
Superstore Datasource.SemanticModel/  ← the model (database.tmdl, model.tmdl, tables/…)
```

Every *sibling* format an agent knows **is** a zip — `.pbix`, `.twbx`, `.tdsx`, `.hyper` — so the
reflex "it's small, it must be an un-zipped stub, I'll zip it" is wrong here and **destroys the
output**.

| Symptom | Cause | Response |
|---|---|---|
| The `.pbip` is "only ~300 bytes / a tiny JSON stub" | That is exactly what a correct `.pbip` is — a JSON pointer to the sibling folders | **Nothing is wrong.** Do **not** zip, repackage, or "fix" it. Double-click it in Power BI Desktop |
| Power BI: `Unable to translate bytes [XX] at index N` on open | The `.pbip` was overwritten with a **ZIP** (its binary `PK..` header is being fed to a JSON parser) | You (or a prior step) zipped the pointer. **Restore it**: re-run the migration, or rewrite the pointer with `assemble_model.write_local_pbip(...)`. Never zip a `.pbip` |
| Not sure whether a produced `.pbip` is healthy | — | **Check, don't guess:** `py -3.11 scripts/deploy_to_fabric.py --verify-pbip <bundle-dir-or-.pbip>`. It reports the pointer's kind + size and whether the sibling folders are intact (exit 0 = openable, exit 1 = a real problem with the specific fix) |
| A run finished `WARN` / "degraded" (some calcs need review, a visual dropped) | A legitimate, actionable outcome — **not** a broken bundle | Read `summary.md` and report the gaps. Do **not** hand-rebuild, re-zip, or re-run to "fix" it — a shortfall is a STOP-and-ask, never something to fix by hand |

---

## Editing the output (`.pbip` reload semantics)

| Symptom | Cause | Response |
|---|---|---|
| Edited a `.tmdl`/`.m` file but Power BI Desktop still runs the old (broken) query | Desktop compiles the `.pbip` **once at open** and does **not** watch the files for changes; the live session keeps the compiled in-memory model | **Close and reopen** the `.pbip` to force a fresh read from disk (Tabular Editor, which writes into the live model, is the exception) |
| A Fabric (Service) redeploy worked but the local Desktop copy didn't change | The published model and the local `.pbip` are **separate artifacts** that drift | Reload the `.pbip` after any out-of-band edit/redeploy; don't assume one reflects the other |

---

## Deploy & validate

| Symptom | Cause | Response |
|---|---|---|
| `createOrUpdate` rejects the definition | Hand-rolled payload drift | Delegate deploy to `semantic-model-authoring`; don't hand-roll `createItem` |
| Measure value ≠ Tableau | Different filter context on the two sides, or a real semantic gap | Match the filter context first; a genuine gap is a real mismatch to investigate |
| Float values differ slightly | Cross-engine rounding | Compare with a relative epsilon, not exact equality (see [validation-reconciliation.md](validation-reconciliation.md)) |

---

## Verifying a rebuild

| Symptom | Cause | Response |
|---|---|---|
| A render "proves" the feature works, but so would the bug | The broken and working hypotheses predict the **same picture** | Find a control that forces them apart (see below) |
| Screenshot comes back empty | Captured before the model was refreshed | `NO_DATA` and "renders nothing" look identical — refresh, then capture |
| Two Desktop instances disagree | You identified the wrong one | Read each process's **command line** (`Get-CimInstance Win32_Process`); `MainWindowTitle` is the file *name*, which two builds of one workbook share |

**Verify by render — but a render is only evidence if the failure mode would look different.** If the
broken and the working hypothesis predict the same picture, that picture is worth no more than a clean
`validate`. Find the control that forces them apart: a second build, a second page, or a deliberately
altered input whose effect you can predict in advance.

There are **three distinct ways** to force the hypotheses apart, and the worked examples below are one
of each — a reader who has all three has a template rather than three anecdotes:

1. **Vary the input, predicting the output in advance.**
2. **Vary the mechanism under fixed data.**
3. **Vary the build under fixed everything.**

A fourth is *opportunistic, not a method*: **when the artifact already contains its own control** —
two views identical but for the encoding under test — prefer it, because it holds everything constant
except the thing in question and needs no second build to argue about. **Look** for one before
building a second build, but do not assume one exists. (The case that produced this was a tutorial
workbook that happens to ship `Challenge` and `Solution` pages over the same data, one with the colour
calc and one without. That is luck of the corpus, not a technique you can invoke.)

Three measured examples of a render that looked like proof and was not:

* A parameter-thresholded colour rendered **all one colour**. Consistent with "the parameter
  evaluated" *and* with "the whole `Conditional` silently fell through to `DefaultValue`" — a mode
  already measured on `Or` nodes and inline visual calculations. The control was a threshold that
  *must* split the rows (100 → 100,000): the split then landed exactly on the parameter's value.
* A view-scoped colour painted **all four bars orange**, which is correct for a running max over a
  monotonically rising series — and identical to an ignored `SelectRef` plus an authored orange mark
  colour. The control was repointing the window bound from running (`WINDOW(1, ABS, 0, REL)`) to
  whole-partition (`WINDOW(1, ABS, -1, ABS)`): exactly one bar stayed orange.
* A calendar fix looked clean on every static signal — 29/29 built, `validate` 0 errors, self-check
  green. The control was cold-opening **both** builds: the pre-change model loaded a fabricated
  year-2000 calendar, the post-change one opened correctly.

The same rule applies to the measuring apparatus, not just the artifact:

* **Prove the gate CAN FAIL, not merely that it runs.** Those differ, and the difference is not
  academic: a rollback-anchor gate here ran on every commit, reported clean, and *could never fail* —
  its reachable set came from `git rev-list --all`, which includes `refs/tags`, so every anchor
  vouched for itself. Probing it with a deliberately-orphaned anchor is what exposed it. The general
  form is worth memorising, because it is not git-specific: **any check whose input set is defined by
  something the artifact under test contributes to is tautological by construction**, and it will
  pass with total confidence forever.
* **A test that forbids a shape real history contains is inventing a convention, not checking one.**
  The same anchor gate's naming test initially rejected `rollback/pre-v1.9.0-comparison`, a legitimate
  label the repo had used for a year. Same error as a gate keyed on a proxy: asserting what you assume
  instead of what exists.
* **The tell is a result inconsistent with what the change could possibly do.** This is the heuristic
  that makes the rest actionable, because it tells you *when* to distrust a number: a report-only
  change cannot touch a `.tmdl`. A diff reporting 74 differing `.tmdl` files for such a change was
  disbelieved on exactly that ground, and the harness — not the engine — turned out to be wrong.
* **Build baselines; never copy them.** A copied tree is a build of a *different* commit wearing the
  right directory name, and every absolute path inside it still says so. Masking substitutes the root
  you pass in, so it misses all of them.
* **A baseline you have opened in Power BI Desktop is no longer a pure build of its commit.** A render
  check writes `.pbi/cache.abf` into the tree and it appears in the next diff. Render from a throwaway
  copy.
* **Name both operands of a diff, never just the delta.** A whole-tree diff whose two roots were
  built from the *same* tree returns `0 differing` — guaranteed, carrying zero information, and
  indistinguishable from a real result. Report "`b178` (built from `d1c35e6`) vs `v178` (built from
  `HEAD`): 1384 vs 1378".
* **Masking normalises representations; any value *derived* from the masked thing escapes it.** A
  path length already rendered into prose (`"output path is 290 chars"`) survives masking of the
  path itself, so give both builds output roots of **equal length**. Same for row counts, byte sizes,
  elapsed times.
* **`added > 0` with `removed == 0` is suspicious, not a result.** A contiguous one-sided block of
  "added" files is the signature of an enumeration failure — `os.walk` silently stops at `MAX_PATH`
  (260) unless the root is passed as `\\?\...`, and if the two roots differ in length the truncation
  hits one side only.
* **A substitution that reports success by returning a string is not a substitution.** PowerShell's
  `-replace` treats `$` in the replacement as a group reference; Python's `str.replace` returns the
  input unchanged on no match. Both silently no-op. Assert the match count, and read back what you
  wrote.

---

## Security

| Symptom | Cause | Response |
|---|---|---|
| Secret almost committed | `.tds`/`.tdsx`/`.twb`/`.hyper` are plaintext and may embed connection info | They are git-ignored — keep them out of the model, the report, and the repo (see [security-governance.md](security-governance.md)) |
