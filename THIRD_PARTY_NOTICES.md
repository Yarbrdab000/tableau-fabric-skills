# Third-Party Notices

This project (`tableau-migration-skill`) is licensed under the MIT License (see
[`LICENSE`](LICENSE)). It does **not** bundle, vendor, copy, or redistribute any
third-party source code. This file documents prior art that informed the design so
the provenance is transparent for downstream reviewers (including a potential
contribution to `microsoft/skills-for-fabric`).

## Prior art (reference only — no code used)

### cyphou/Tableau-To-PowerBI

- **Project:** https://github.com/cyphou/Tableau-To-PowerBI
- **License:** MIT
- **How it was used:** Surveyed as a reference to understand the *space* of Tableau
  constructs that have clean Power BI / DAX / Power Query equivalents (e.g. that
  Tableau `IF/THEN/ELSE` maps to DAX `IF`, `ZN(x)` to a null-coalesce, `/` to
  `DIVIDE`, `AND`/`OR` to `&&`/`||`). These are **factual language-to-language
  equivalences**, which are not protected by copyright.
- **What was NOT used:** No source files, functions, regular expressions, lookup
  tables, comments, test fixtures, or other expressive content were copied or
  adapted from that project. The translator in
  [`skills/tableau-migration/scripts/calc_to_dax.py`](skills/tableau-migration/scripts/calc_to_dax.py)
  is an independent recursive-descent parser/emitter with a different architecture
  (typed AST with per-node data-type checking and a measure-context invariant) than
  the referenced project's approach.

If you believe any portion of this repository improperly reproduces third-party
material, please open an issue so it can be corrected promptly.
