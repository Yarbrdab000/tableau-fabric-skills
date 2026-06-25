# Fidelity oracle — advisory structural scorer (Tableau `.twb` ⇄ emitted PBIR)

This is the runbook for the **advisory, tolerance‑banded fidelity oracle** — a *verification*
tool that scores an emitted Power BI **PBIR** report against its Tableau `.twb` source to help
*prove* a faithful (toward pixel‑perfect) rebuild. It is **not** part of the migration engine and
it never changes a single byte of the output. The deterministic engine
([`viz-rebuild.md`](viz-rebuild.md)) owns correctness; this oracle is an **independent second
opinion** that re‑reads *both* sides from disk and grades their agreement.

> **One sentence.** Re‑parse the Tableau workbook and the emitted PBIR with *separate* readers,
> pair their visuals by content, and hand back an advisory `0..1` agreement score plus a per‑visual
> diff — never a pass/fail and never a pixel claim.

It is distinct from, and complementary to, the [image oracle](image-oracle.md): that one reads the
*Tableau‑side* picture once to adjudicate a chart **type** during the build. This one is a
*render‑diff‑style* **structural** scorer that runs *after* the build and compares the two
definitions field‑for‑field, role‑for‑role, zone‑for‑zone.

---

## Why an independent reader (not a round‑trip)

A check that re‑ran the engine against itself would share the engine's blind spots. So the oracle
ships its **own** PBIR JSON reader and its **own** `.twb` viz‑grammar reader and never imports the
engine's parse path. A divergence only surfaces when two independently authored readers *disagree*
about what the two artifacts say — which is exactly the signal we want when proving fidelity.

Everything is **advisory and tolerance‑banded.** Cross‑engine equality is not a binary; Power BI
and Tableau round, lay out, and label differently. The report returns a graded score, a per‑visual
diff (match / missing / extra), and a named **band** — never a hard verdict.

---

## What it scores (structural tier — the deterministic backbone)

Per **paired** visual, four components in `[0, 1]`, weighted:

| Component | Weight | Meaning |
|---|---:|---|
| **fields** | 0.40 | Jaccard overlap of the normalized **source‑field** sets — *did the rebuilt visual bind the same underlying columns/measures?* The strongest, least engine‑coupled signal. |
| **type** | 0.30 | Chart‑type **family** agreement (exact / related / mismatch), from an independent classifier off the Tableau mark + shelf shape. |
| **roles** | 0.20 | Agreement of the dimension‑set vs measure‑set split — *did a field silently flip between an axis/group role and an aggregated value?* |
| **position** | 0.10 | Normalized‑rectangle IoU for dashboard‑placed visuals (Tableau zones normalized by dashboard extent, PBIR by page size), inside a tolerance band. Self‑service pages drop this and the weights renormalize. |

Field names are normalized to lowercase alphanumerics so `Order Date` ≡ `Order_Date` and
`Country/Region` ≡ `Country_Region` match without colliding distinct fields. Calc pills (e.g.
`[Calculation_1368…]`) resolve through the datasource caption index to their display name
(`Profit Ratio`) so they line up with the emitted measure. Tableau internals that are **not**
author fields — the row‑identity object id (`__tableau_internal_object_id__`), `Number of Records`,
and generated `Latitude/Longitude/Geometry` — are excluded from the binding set.

**Pairing** is content‑based, not name‑convention‑based: each Tableau dashboard maps to the PBIR
page sharing its display name, and that dashboard's worksheets are greedily matched to the page's
non‑slicer visuals by `0.7·field‑overlap + 0.3·position`. Worksheets on no dashboard fall back to a
field‑only best match. Slicers are scored separately as **filter fidelity** (does a slicer field
correspond to a Tableau categorical filter on that dashboard?).

The **chart‑family classifier** reads the worksheet's mark + shelf pills independently of the engine.
An explicit mark wins (`Text`→table/card, `Square`+dimensions→highlight‑table→matrix, `Bar`→bar,
geometry→map). Under an `Automatic` mark it applies Tableau's own defaults *conservatively*: a
**continuous (green) date axis** — a date‑truncation pill rendered quantitatively (`tdy:Order Date:qk`)
— is asserted as a **line**, while ambiguous dim+measure shapes stay an *unasserted* bar that earns
partial credit rather than punishing a plausible rebuild. The continuous/discrete distinction comes
from the pill **typekey** (`qk` continuous vs `ok`/`nk` discrete), so the *same* `tdy` derivation is a
line on a continuous axis but a matrix/table axis when discrete (the real Comcast `Line chart` vs
`Segment % Dod`).

**Aggregate** = mean per‑visual score × coverage (the fraction of source worksheets that found a
peer). An unmatched worksheet drags the aggregate down — a faithful rebuild leaves none behind.

### Minute placement — layout fidelity from geometry alone (render‑free)

The `position` **score** above is an IoU inside a tolerance band, so it deliberately rounds small
offsets to a full match. Layout is fundamentally a **zone** problem, not a pixel problem, so to make
positioning *minute* each dashboard‑paired visual also carries an additive **`placement`** block —
**no Power BI render is ever required**. The Tableau dashboard zone (normalized to the dashboard
extent) is projected onto the PBI page canvas in pixels, the authoritative placement target, and
diffed **edge‑by‑edge** against the emitted PBIR visual's own canvas px:

- `tableau_zone_px` / `pbir_px` — the zone target vs the emitted rect, on the canvas.
- `delta_px` — signed `left / top / right / bottom / width / height / center` offsets.
- `max_edge_px` — worst single‑edge drift; `pixel_exact` (≤ 2 px) and `within_tolerance`
  (≤ 1 % of the canvas — the bar a hand‑built faithful copy hits by eye) flags.

Because the deterministic engine derives placement from the *same* Tableau zones, a faithful rebuild
lands **pixel‑exact**: on both calibration cases every visual reports `max_edge_px ≈ 0.00` / IoU
`1.000` (SIMPLE `Sheet 1–4`, COMCAST `Line chart`/`Segment % Dod`/`Line chart (2)`/`(3)`). This is
the layout proof — established purely from the `.twb` zone geometry and the PBIR spec, with the PBI
side **always the spec, never a screenshot**. (Pixel rasterization of a `.pbip` requires Power BI
Desktop or the Service and would only ever speak to *styling* — colors/marks as drawn — which is the
image tier's advisory concern, not layout.)

The per‑visual deltas roll up to a dashboard‑level **`summary.placement`** verdict so layout fidelity
reads at a glance: `evaluated`, `pixel_exact` / `within_tolerance` / `drifted` counts, the
`worst_max_edge_px` and which `worst_worksheet` drifted most, and a `verdict`
(`pixel-exact` → `acceptable` → `drifted`). On both calibration cases the verdict is **`pixel-exact`,
4/4** with a worst edge of `≤ 0.01 px`.

### Non‑worksheet dashboard objects (placement targets, expected extras)

A Tableau dashboard places more than worksheets: **titles, text boxes, legends (color/size/shape),
parameter controls and filter cards** all occupy zones. The oracle captures every such typed,
non‑container zone as a `dashboard_objects` entry — excluding the structural `layout-basic` /
`layout-flow` containers, which hold a region but draw nothing. Each object is projected onto the page
canvas as a **`target_px`** rect: the render‑free placement target the rebuild should hit for that
title/legend/param control. A legend carries its owning `worksheet`; a parameter control carries its
`param` binding (so an emitted param‑control slicer can be cross‑checked against the control it stands
in for). These are **expected extras** — surfaced for completeness and as engine placement targets,
but **never scored and never counted against coverage** (coverage is over source worksheets only).

### Advisory bands

| Band | Aggregate | Read it as |
|---|---|---|
| `faithful` | ≥ 0.95 | Indistinguishable within cross‑engine noise. |
| `strong` | ≥ 0.85 | Minor, explainable divergence. |
| `review` | ≥ 0.60 | A human should eyeball it. |
| `divergent` | < 0.60 | Materially different — likely a real rebuild gap. |

### Remodel/rename advisory (don't misread a faithful star‑schema rebuild)

A genuinely faithful rebuild can score *low* on the name‑based field/role components when the engine
**remodels** the data — e.g. promoting Tableau's `Order Date` column into a star‑schema `Date`
dimension, or naming an implicit `COUNT(Orders)` as a `count orders` measure. The chart type and
on‑dashboard position still match perfectly; only the field *names* diverge, so Jaccard field
overlap craters even though nothing is wrong.

The oracle flags this signature per visual: when **type ≥ 0.95**, **position ≥ 0.85 (or absent)**,
and **field overlap < 0.50**, the visual carries `"diagnosis": "remodel-rename-suspected"`, the
report `summary` exposes a `remodel_rename_suspected` count, and an advisory note is appended.
**Read it as "confirm via the DAX‑value and image tiers"** — those compare numbers and pixels and
are immune to renaming, so they are the authority when a remodel is suspected. A low *structural*
score there reflects naming, not infidelity.

### Resolving the rename: the engine field‑alias map

The remodel flag above *explains* a low field score; the **field‑alias map** lets the structural
tier **see through** it. The engine emits an additive `field_aliases` map on each candidate record —
`{emitted queryRef → Tableau source caption}` (e.g. `Date.Date → Order Date`,
`_Measures.count orders → ` the rebound source measure) — built with the same expression the
projections use, so it keys 1:1 with the refs the oracle reads. Supply it and the oracle rewrites
each emitted ref back to its Tableau caption **before** name overlap, so a faithful rename scores as
a match instead of a miss.

```
# explicit map, a migrate_twb_to_pbir candidate_records list/result, or a flat {ref: caption} JSON
py -3.11 scripts/fidelity_oracle.py source.twb out\Report --candidate-records candidate_records.json
```

In‑process, pass `field_aliases={...}` (or `aliases_from_candidate_records(records)`) to
`score_report` / `run_oracle`. It is **optional and off by default**, never re‑runs the engine, and
no‑ops cleanly on builds whose records predate the producer. The report adds a
`summary.fields_alias_resolved` count, an advisory note, and a markdown line; the original emitted
name is preserved per field as `norm_emitted`. Measured on the real Comcast rebuild, resolving just
`Date.Date → Order Date` lifts the aggregate **0.40 → 0.55** (every visual improves); the residual
is then source‑side (a dropped implicit `COUNT`), not the rename.

### Source‑side fidelity: reference‑line decoration calcs are not worksheet fields

A measure placed on a worksheet's `<lod>`/detail mark channel typically backs a **reference‑line
distribution band** (e.g. a `WINDOW_STDEV` computation feeding a `±1σ` band), not a visible mark
encoding the rebuild must reproduce. The structural tier excludes these `<lod>`‑channel **measures**
from the worksheet's field set so a faithful rebuild is **not** charged for omitting decoration;
genuine detail **dimensions** on `<lod>` are kept (a measure on detail is virtually always a
reference‑band support field, never a disaggregation). On the faithful anchor sheet `Line chart`
this drops the two false `Standard of Deviation` "missing" fields, lifting the sheet's aliased field
overlap **0.25 → 1.00** and role agreement **0.50 → 1.00**.

---

## Calibration — the cross‑engine noise floor

The bands are anchored on a **known‑faithful** rebuild so "good" is a measured number, not a guess.

| Case | Aggregate | Band | What it shows |
|---|---:|---|---|
| **Faithful** (clean engine output, simple workbook) | **0.954** | faithful | The noise floor. 3 of 4 visuals score a perfect `1.000`; the only sub‑1.0 is a choropleth at `0.817` because Power BI's shape map can't carry the Tableau map's `State/Province` LOD detail — an *explainable* simplification, not a bug. |
| **Hand‑built, simplified** (an author's PBIR that rebuilt area→line and renamed the date binding) | **0.868** | strong | Scores **below** our engine output — the oracle correctly judges the engine's rebuild *more* faithful. It flags the area→line as `type-related` partial credit and the date‑field divergence. |
| **Pilot** (a complex real workbook) | **0.587** | divergent | Coverage `0.8` (one worksheet unmatched) and genuine binding gaps on table‑calc / reference‑band constructs. Exactly the "needs work" signal. |

The spread (**0.95 → 0.87 → 0.59**) is the point: the oracle discriminates a faithful rebuild from
a simplified one from a divergent one. Treat **≥ ~0.95 aggregate with per‑visual ≥ ~0.82** as the
faithful envelope for a clean workbook; investigate any visual that bands below `strong`.

> The hand‑built reference was deliberately simplified (area→line, a dropped filter default). Our
> output is intentionally **more** faithful than it, so divergence *from* it is **expected**, not an
> error — the calibration numbers above bear that out.

### Image tier — cross‑engine SSIM floor

The image tier is calibrated separately, on a **real** Tableau‑vs‑Power‑BI render pair. A hand‑built
rebuild that diverged on mark type (area→line), bar sort, basemap style, and a dropped filter scored
**SSIM ≈ 0.64–0.65** (`divergent`) — and the aspect‑ratio distortion accounted for only ~0.01 of
that, so the rest is genuine visual divergence. Crucially that **0.65 sits below the same rebuild's
structural `0.868`**: the image tier *sees* the mark‑type and layout drift the structural tier
smooths over. A genuinely faithful rebuild is therefore expected to clear the advisory **acceptance
floor of `0.80`** (`--image-threshold`, surfaced as `meets_target`); the `0.64–0.65` figure anchors
the **divergent** end, not "good."

---

## How to run

```powershell
# structural tier — offline, stdlib only, no Power BI Desktop needed
py -3.11 scripts\fidelity_oracle.py `
  "<path>\workbook.twb" `
  "<out>\reports\<Workbook>.Report" `
  --engine-report "<out>\report.json" `   # optional: enriches each row with the engine's declared intent
  --format md                              # or: json (default)
```

`report_dir` accepts either the `*.Report` folder or a parent that contains exactly one (including
the estate `reports/` layout). `--engine-report` is optional; when supplied, each visual row is
annotated with the engine's own `viz_fidelity[]` declaration so you can compare the engine's
*intent* against the oracle's *independent* read.

Programmatic use:

```python
import fidelity_oracle as fo
report = fo.run_oracle(twb_path, report_dir, engine_report_path)  # advisory dict
print(fo.render_markdown(report))
```

---

## Optional tiers (lazy, guarded — never required)

The structural tier above is the deterministic priority and runs anywhere. Two optional tiers are
lazily imported and **degrade gracefully** to an `available: false` record when their host or
packages are absent — importing the module never fails offline.

- **Tier 2 — DAX value oracle** (`dax_value_tier`): compares live model **measure values** by
  querying the rendered model through a local Analysis Services (`msmdsrv`) instance via ADOMD.
  Requires a running Power BI Desktop; returns `unavailable` otherwise. Auto-discovers the
  workspace port (asks for an explicit `--dax-port` when several instances are live), filters
  internal/hidden measures, and — given an `--expected` `{measure: value}` map — reports the
  fraction of measures within tolerance (else the fraction that evaluate without error; an
  *erroring* measure is itself a fidelity defect the structural tier cannot see). An expected entry
  may also be **per‑view**: `{label: {measure, expected, filter}}`, where `filter` is caller‑DAX
  that reproduces a worksheet's filter context (e.g. `'Orders'[Country] = "United States"`). This
  is what catches a **filter‑scope mismatch** — a Tableau dashboard whose *map* is US‑only while its
  KPIs/bars include Canada, rebuilt with the filter applied too broadly, shows a few‑percent value
  drift the model‑level total would hide.
- **Tier 3 — image** (`image_tier`): tolerance‑banded *perceptual* similarity of a Tableau
  reference PNG and a PBI render PNG (SSIM via optional numpy/Pillow). Cross‑engine literal
  pixel‑equality is impossible, so this tier reports a similarity **band**, never pass/fail.
  It also compares SSIM against an advisory **acceptance floor** (`--image-threshold`, default
  `0.80`) and emits a `meets_target` verdict — a faithful rebuild is expected to clear it.
  Passing `regions` (fractional crop boxes per zone) adds a **per‑zone SSIM breakdown** +
  `regions_mean_ssim`, which localizes *where* a composite render diverges instead of collapsing it
  into one number (on a real pilot pair: map `0.77`, KPI `0.68`, but sorted/grouped bars `0.48` and
  an area→line time‑series `0.48`). Rather than hand‑estimating those crop boxes, pass
  `--image-auto-regions`: the **structural tier feeds the image tier** — each worksheet's Tableau
  dashboard zone becomes the *reference* crop and its paired PBIR visual position becomes the
  *candidate* crop, so each render is cropped by *its own* layout and the per‑zone SSIM is keyed by
  worksheet name with no manual tuning.

### Local Power BI render + first‑party PBIR validation (deterministic host bridge)

Two first‑party Microsoft CLIs un‑park the parts of the oracle that need a real Power BI **render**
or an authoritative **schema check** — both run locally/offline, so the oracle stays standalone.
Each is wrapped exactly like the tiers above: located on `PATH`, run in a child process with a
timeout, parsed defensively, and degraded to `available: false` when absent. **Neither feeds the
structural aggregate**, so the calibration floor (`0.897` / `0.408`) is identical whether or not
they run. Install both with
`npm i -g @microsoft/powerbi-desktop-bridge-cli @microsoft/powerbi-report-authoring-cli`.

- **PB‑side render** (`render_pbi_report`, `--image-render <…\Workbook.pbip>`) drives the
  `@microsoft/powerbi-desktop-bridge-cli` (`powerbi-desktop`): `open` the `.pbip`, read `status` to
  pick the Desktop **PID** (an explicit `pid`, else a report‑dir match, else the sole instance), then
  `screenshot-all` the pages into a temp dir. The captured page PNG becomes the image tier's
  **candidate** — so the SSIM tier no longer waits on a hand‑exported PNG. Screenshots are per
  **page**; the per‑zone breakdown still comes from `--image-auto-regions` cropping that page by each
  visual's own PBIR px (the same rects the render‑free placement diagnostic already computes).
  Requires Power BI Desktop with **Preview features → "Enable external tool access to Power BI
  Desktop through secure local APIs"** enabled; reload/screenshot run serially per PID. (The Tableau
  **reference** half is still acquired separately — see below.)
- **PBIR validation pre‑gate** (`validate_pbir`, `--validate`) runs the
  `@microsoft/powerbi-report-authoring-cli` (`powerbi-report-author validate`) over the emitted
  `.Report` — first‑party confirmation the PBIR is well‑formed against the authoritative schema,
  catching drift a hand‑rolled reader cannot self‑detect. It is **additive**: a separate
  `pbir_validation` block (`valid` + error/warning **diagnostics** with file + JSON paths) plus a
  compact `summary.pbir_valid` flag, never a 0–1 score blended into the aggregate. A validation
  **error** is a concrete fix‑before‑ship defect; warnings (e.g. an unknown visual type) are usually
  a typo unless a custom visual is intended.

```powershell
# render the PB candidate locally instead of --image-cand, and run the validation pre-gate
py -3.11 scripts\fidelity_oracle.py `
  "<path>\workbook.twb" "<out>\reports\<Workbook>.Report" `
  --image-ref     "<ref>\tableau_dashboard.png" `  # Tableau reference (see below)
  --image-render  "<out>\reports\<Workbook>.pbip" ` # capture the PB render via the Desktop bridge
  --image-auto-regions `
  --validate `                                     # first-party PBIR schema pre-gate (additive)
  --format md
```

### Combined cross‑tier fidelity (advisory headline)

When more than one tier runs, the report also carries a `combined_fidelity` block: a single advisory
headline that fuses the **structural** aggregate (weight `0.5`), the DAX **value** score (`0.3`), and
the **image** SSIM (`0.2`, preferring the per‑zone `regions_mean_ssim` when present). Weights are
**renormalized over only the tiers that actually ran**, so the headline is comparable whether one
tier or all three contributed — while a separate **`confidence`** flag (`high`/`medium`/`low` for
`3`/`2`/`1` tiers) records how much evidence backs it. It is explicitly a triage number, *not* a
gate: a low image score pulling the headline beneath a high structural score is the useful signal,
not an error.

```powershell
# optional tiers — DAX-value (needs a live Power BI Desktop) and image (needs numpy + Pillow)
py -3.11 scripts\fidelity_oracle.py `
  "<path>\workbook.twb" "<out>\reports\<Workbook>.Report" `
  --dax --dax-port 57006 `                 # omit --dax-port to auto-discover when only one is live
  --expected "<path>\expected_values.json" `  # optional {measure: value} map
  --image-ref  "<ref>\tableau_dashboard.png" ` # server-rendered Tableau view (RLS applied)
  --image-cand "<out>\powerbi_render.png" `    # Power BI export/screenshot
  --image-threshold 0.80 `
  --image-auto-regions `                   # derive per-worksheet crops from the dashboard layout
  --format md
```

### Acquiring the Tableau reference images (`fidelity_reference.py`)

The image tier needs a *reference* PNG per worksheet. The optional, network‑only
`scripts/fidelity_reference.py` produces them and makes a missing reference an explicit instruction
rather than a silent gap. It **reuses the skill's Tableau auth by importing `fetch_tds`** (no edits)
and is stdlib‑only.

- **Live / published (preferred):** pulls a server‑rendered
  `.../views/{id}/image?resolution=high` PNG. The server renders **as the authenticated user**, so
  **RLS is applied** — which is why this beats the (RLS‑stripped, usually absent) embedded
  thumbnail.
- **Local‑exclusive (offline / unreproducible RLS):** drop a screenshot per worksheet into a known
  folder; `resolve_local_references` / `build_acquisition_plan` report exactly which files are
  present, which are missing, and the precise name to save each missing one as.
- **Local‑exclusive (consume already‑exported PNGs):** this box often has **no Tableau at all**, so
  the source can't be auto‑rendered here. But Tableau Desktop (on whatever machine the author uses)
  exports a faithful per‑view PNG via **`Worksheet > Export > Image`**, default‑named after the
  view; and a packaged **`.twbx` is just a zip** whose `Image/` folder holds the author‑placed image
  *objects* (logos/backgrounds), extractable with stdlib `zipfile` and **no Tableau at all**.
  `load_exported_references` maps an existing folder of PNGs (or a single PNG) to worksheet names by
  a tolerant filename match; `extract_twbx_images` pulls a `.twbx`'s embedded image objects. Both
  feed the image tier as the *reference* half — the *candidate* half is the local Power BI render
  from the host bridge above. (Tip from Tableau's export UI: a **Fixed‑Size** dashboard layout
  yields predictable, repeatable pixel dimensions, which helps SSIM alignment.)
  > The `.twbx` `Image/` folder holds image *objects*, **not** rendered chart pixels — useful to
  > confirm a logo/background reappears in the rebuilt zone. For chart pixels use the live pull or a
  > manual `Worksheet > Export > Image`.

```powershell
# consume already-exported PNGs (no Tableau/server here): map them to worksheet names
py -3.11 scripts\fidelity_reference.py --from-export "<png_dir>" `
  --worksheets "Sheet 1,Sheet 2,Sheet 3"

# pull embedded image objects out of a packaged .twbx (stdlib zip; no Tableau needed)
py -3.11 scripts\fidelity_reference.py --from-twbx "<workbook>.twbx" --out "<asset_dir>"

# resolve the reference straight from a local source during a scoring run:
py -3.11 scripts\fidelity_oracle.py "<src>.twb" "<out>\...\.Report" `
  --image-render "<out>\...pbip" --image-ref-source "<png_dir_or_.twbx>" --image-ref-name "Sheet 1"
```

```powershell
# see what's present/missing locally (no network) — emits "drop a PNG named X" guidance
py -3.11 scripts\fidelity_reference.py --check-local `
  --worksheets "Sheet 1,Sheet 2,Sheet 3" --out "<ref_dir>"

# live pull (RLS applied); PAT secret comes from an env var and is never logged or committed
$env:TABLEAU_PAT_VALUE = "<secret>"
py -3.11 scripts\fidelity_reference.py `
  --server 10ay.online.tableau.com --site <site-content-url> `
  --pat-name <token-name> --worksheets "Sheet 1,Sheet 2,Sheet 3" --out "<ref_dir>"
```

> Server‑rendered images are **data‑bearing**: they are written only to `--out` and must **never**
> be committed. The PAT secret is read from an env var only — never pass it on the command line.

---

## Guardrails

- **Read‑only.** The oracle never writes to, or re‑runs, the migration output. It only reads.
- **Advisory only.** Output is a graded, banded agreement plus a diff — it is *evidence for a human
  judgment*, not a gate.
- **Quarantined tests.** Its suite lives in `tests_oracle/` (run with `pytest tests_oracle`) so the
  engine's green gate (`pytest tests`) never collects it and can never be broken by it.
- **`fields_missing`** = on the Tableau source but absent from the rebuilt visual;
  **`fields_extra`** = on the rebuilt visual but not on the source.
