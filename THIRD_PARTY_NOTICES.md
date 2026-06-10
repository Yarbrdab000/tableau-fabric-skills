# Third-Party Notices

This project (`tableau-fabric-skills`) is licensed under the MIT License (see [`LICENSE`](LICENSE)).
It does **not** bundle, vendor, copy, or redistribute any third-party **source code**. This file
documents (a) prior art that informed the design and (b) third-party artifacts the skills *deploy*
or *depend on at runtime*, so provenance is transparent for downstream reviewers (including a
potential contribution to `microsoft/skills-for-fabric`).

## Prior art (reference only — no code used)

### cyphou/Tableau-To-PowerBI
- **Project:** https://github.com/cyphou/Tableau-To-PowerBI
- **License:** MIT
- **How it was used:** Surveyed as a reference to understand the *space* of Tableau constructs that
  have clean Power BI / DAX / Power Query equivalents (e.g. Tableau `IF/THEN/ELSE` to DAX `IF`,
  `ZN(x)` to a null-coalesce, `/` to `DIVIDE`, `AND`/`OR` to `&&`/`||`). These are **factual
  language-to-language equivalences**, which are not protected by copyright.
- **What was NOT used:** No source files, functions, regular expressions, lookup tables, comments,
  test fixtures, or other expressive content were copied or adapted. The translator in
  [`skills/tableau-migration/scripts/calc_to_dax.py`](skills/tableau-migration/scripts/calc_to_dax.py)
  is an independent recursive-descent parser/emitter with a different architecture.

## Conventions & packaging model (structure modeled, no content copied)

### microsoft/skills-for-fabric
- **Project:** https://github.com/microsoft/skills-for-fabric
- **License:** MIT
- **How it was used:** Adopted as the *convention and packaging model* for this collection so that
  it stays shaped for an eventual upstream contribution. We model its **structure and formats** only
  — the `skills/<name>/SKILL.md` + `resources/` layout, the `SKILL.md` frontmatter shape (a `name`
  plus a `description` that ends with an enumerated "Use when the user wants to: (1)(2)(3)" and a
  quoted `Triggers:` list), and the marketplace/plugin fractal packaging. Conventions, formats, and
  methods of this kind are not protected by copyright.
- **What was NOT used:** No skill descriptions, workflow/selector tables, resource prose, or other
  expressive content were copied or adapted; all such content in this repository is original. If any
  file is ever copied verbatim in the future, its MIT copyright and license notice will be retained.

## Deployed / referenced third-party artifacts (not vendored)

### Official Tableau MCP server image — `ghcr.io/tableau/tableau-mcp`
- **Project:** https://github.com/tableau/tableau-mcp
- **License:** Apache-2.0
- **How it is used:** The `tableau-mcp-landing-zone` skill **deploys the official image unmodified**
  (it does not fork or rebuild it) behind an auth sidecar. The image is pulled from its registry at
  deploy time; none of its source is vendored in this repository. The auth sidecar's source and tests
  live in the bridge repo and ship as a separate published image.

## Runtime dependencies (installed via pip, not vendored)

- **`requests`** (Apache-2.0) — the only runtime dependency of `tableau-datasource-profiler`
  (`skills/tableau-datasource-profiler/requirements.txt`). `tableau-migration`'s scripts are Python
  standard-library only.

If you believe any portion of this repository improperly reproduces third-party material, please open
an issue so it can be corrected promptly.