# Clean-Room Provenance & Authorship Statement

This document records how `tableau-fabric-skills` was built, so its provenance is transparent
to downstream reviewers — in particular for a potential contribution to
[`microsoft/skills-for-fabric`](https://github.com/microsoft/skills-for-fabric), which requires
contributors to attest that their contribution is their **own original work**.

It is the engineering-process companion to [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Skills in this collection

This repository packages three skills; the clean-room concern below centers on the
`tableau-migration` calc → DAX translator (the substantive original IP). The others are
straightforward:

- **`tableau-datasource-profiler`** — calls Tableau's own REST / Metadata / VizQL Data Service APIs
  and signs a Connected App JWT with the Python standard library (its one runtime dependency is
  `requests`). No third-party code copied; original work.
- **`tableau-mcp-landing-zone`** — **wraps the official `ghcr.io/tableau/tableau-mcp` image
  unmodified** (it does not fork or reimplement it) behind an original auth sidecar. The deploy
  assets vendored under its `assets/` folder are original infrastructure-as-code synced from the
  bridge repo; the official image is pulled at deploy time, not vendored. See
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
- **`tableau-migration`** — original parser/emitter; provenance detailed below.

## Summary attestation

> All source code, tests, prose, mapping tables, and resource documents in this repository are
> **original work** authored for this project. Third-party projects were studied only to understand
> **unprotectable facts, methods, and language-to-language equivalences** (17 U.S.C. § 102(b)). No
> third-party **expression** — source files, functions, regular expressions, lookup tables,
> comments, test fixtures, prose, or a compilation's specific arrangement — was copied, vendored,
> machine-translated, or adapted into this repository.

## The standard we hold ourselves to

Copyright protects **expression**, not **ideas, facts, methods, or systems of operation**
(17 U.S.C. § 102(b); the idea/expression dichotomy). Concretely, for this project:

| Free to use (facts / ideas / methods) | Protected (expression) — NOT used |
|---|---|
| That Tableau `IF/THEN/ELSE` corresponds to DAX `IF`, `ZN(x)` to a null-coalesce, `/` to `DIVIDE`, `AND`/`OR` to `&&`/`||` | Another project's literal converter code, regexes, or AST classes |
| That a `{FIXED dim : AGG}` LOD corresponds to `CALCULATE(AGG, ALLEXCEPT(...))` | Another project's specific function bodies that emit that pattern |
| That a connector class maps to a Power Query M data-access function | Another project's M-builder source or its connector lookup dictionary |
| The general shape of an extract → convert → import pipeline | Another project's CLI/module structure, naming, or file layout |
| Public Microsoft TMDL / DAX / Power Query / PBIR grammar | Any third party's emitter source for that grammar |

**Reading to understand a mechanism, then writing original code, is not infringement** — the same
way reading documentation for a SQL function and then writing your own query is not infringement.
What we refuse is copying *expression* or mirroring a protected *arrangement*.

## Sources consulted (and how)

### Primary sources (authoritative — drove the implementation)
- **Tableau workbook/datasource XML** (`.tds` / `.twb`) — the authoritative structure for
  connections, relations, column metadata, calculated-field formulas, drill paths, and worksheet
  encodings. Parsed directly.
- **Microsoft official documentation** — TMDL / Tabular Object Model grammar (tables, columns,
  measures, hierarchies, roles/RLS), DAX function semantics (including window functions
  `WINDOW`/`OFFSET`/`RANK`/`INDEX`), Power Query M data-access functions, the PBIR/`.pbip` report
  format, and Fabric REST APIs.

### Prior-art reference (studied for facts/mechanics only — no expression used)
- **`cyphou/Tableau-To-PowerBI`** (MIT) — surveyed to understand the *space* of Tableau constructs
  that have clean Power BI equivalents and to confirm that a comprehensive translation is
  mechanically achievable. We used its **factual mapping space** and **general method**, and wrote
  our own parser/emitter, our own taxonomy, our own tests, and our own module structure. No file,
  function, regex, lookup table, comment, or test fixture from it appears in this repository.
- **`microsoft/skills-for-fabric`** (MIT) — modeled for **packaging conventions and format only**
  (repo/skill layout, `SKILL.md` frontmatter shape, `resources/` layout, marketplace/plugin
  structure) so this collection stays shaped for an eventual upstream contribution. All prose and
  content in those containers is our own; no skill descriptions, workflow/selector tables, or
  resource text were copied. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Per-area provenance

| Area | What we understood from references / docs | What we authored (original) |
|---|---|---|
| **Calc → DAX** (`scripts/calc_to_dax.py`) | Factual Tableau→DAX equivalences; that LODs and table calcs are translatable | Independent recursive-descent parser over a typed AST with per-node data-type checking and a measure-context invariant; original FIXED-LOD emitter; original fallback-and-annotate policy |
| **Semantic model** (`scripts/tmdl_generate.py`, `assemble_model.py`) | Microsoft TMDL grammar; that types should come from the landed schema, not Tableau metadata | Original TMDL generators, relationship inference from join keys + landed cardinality, DirectLake binding by table+schema |
| **Model objects** (hierarchies / display folders / RLS) | Microsoft TMDL grammar for `hierarchy` / `displayFolder` / `role` + `tablePermission` | Original derivation from Tableau drill paths, folders, and user filters; original not-translatable → scaffold+flag policy |
| **Connectors → M** (`scripts/connection_to_m.py`, `storage_mode.py`) | Factual connector-class → M data-access mappings; Microsoft Power Query M docs | Original dispatch, descriptor model, bind-input emission, and scored storage-mode policy |
| **Viz rebuild** (`scripts/twb_to_pbir.py`) | Microsoft PBIR/`.pbip` format; that common visual types map cleanly | Original `.twb` XML → intermediate representation → PBIR emitter; original unsupported-visual warning policy |
| **Estate orchestration** (`scripts/migrate_estate.py`) | The general idea of an extract→convert→import→reconcile pipeline | Original entry point, source-adapter abstraction, output-bundle layout, and machine-readable report schema |

## Process controls (how we keep the line honest)

1. **No vendored code.** No third-party source files are present in the repo. (Verify:
   `THIRD_PARTY_NOTICES.md` lists prior art as reference-only.)
2. **Original-from-understanding workflow.** Contributors study mechanics/facts, then implement
   from scratch — no paste, no transliteration, no structure/naming/comment mirroring, no copied
   test data or fixtures.
3. **Integrator similarity review.** Work produced across parallel streams is diff-checked by the
   integrator for substantial similarity to any reference before it is merged.
4. **Attribution retained.** `THIRD_PARTY_NOTICES.md` credits the prior art that informed the
   design, even though no code was used — transparency over silence.

## What this project adds beyond the reference

This is original capability, not a re-skin: **executed value reconciliation** — running each
migrated measure against the deployed Fabric model (DAX `ExecuteQueries`) and the live Tableau
source (VizQL Data Service) and comparing results — so equivalence is **verified**, not asserted.
That, plus Fabric-native deployment and clean-room provenance, is the durable differentiator.

---

*If you believe any portion of this repository improperly reproduces third-party material, please
open an issue so it can be corrected promptly.*
