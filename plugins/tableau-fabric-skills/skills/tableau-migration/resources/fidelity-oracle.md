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

**Aggregate** = mean per‑visual score × coverage (the fraction of source worksheets that found a
peer). An unmatched worksheet drags the aggregate down — a faithful rebuild leaves none behind.

### Advisory bands

| Band | Aggregate | Read it as |
|---|---|---|
| `faithful` | ≥ 0.95 | Indistinguishable within cross‑engine noise. |
| `strong` | ≥ 0.85 | Minor, explainable divergence. |
| `review` | ≥ 0.60 | A human should eyeball it. |
| `divergent` | < 0.60 | Materially different — likely a real rebuild gap. |

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
  *erroring* measure is itself a fidelity defect the structural tier cannot see).
- **Tier 3 — image** (`image_tier`): tolerance‑banded *perceptual* similarity of a Tableau
  reference PNG and a PBI render PNG (SSIM via optional numpy/Pillow). Cross‑engine literal
  pixel‑equality is impossible, so this tier reports a similarity **band**, never pass/fail.
  It also compares SSIM against an advisory **acceptance floor** (`--image-threshold`, default
  `0.80`) and emits a `meets_target` verdict — a faithful rebuild is expected to clear it.

```powershell
# optional tiers — DAX-value (needs a live Power BI Desktop) and image (needs numpy + Pillow)
py -3.11 scripts\fidelity_oracle.py `
  "<path>\workbook.twb" "<out>\reports\<Workbook>.Report" `
  --dax --dax-port 57006 `                 # omit --dax-port to auto-discover when only one is live
  --expected "<path>\expected_values.json" `  # optional {measure: value} map
  --image-ref  "<ref>\tableau_view.png" `   # server-rendered Tableau view (RLS applied)
  --image-cand "<out>\powerbi_render.png" ` # Power BI export/screenshot
  --image-threshold 0.80 `
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
