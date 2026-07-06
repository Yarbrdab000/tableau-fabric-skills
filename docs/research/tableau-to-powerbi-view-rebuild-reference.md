# Tableau → Power BI View & Dashboard Rebuild — Prior-Art Study + LLM-Enhancement Playbook

> **Purpose.** A knowledge-mining reference for the `tableau-migration` skill: how the *view /
> dashboard* side of a Tableau→Power BI migration actually works — chart-type mapping, encoding
> routing, visual/layout formatting fidelity, interactivity, and the semantic-model scaffolding
> that views depend on — plus a running thread on **where an LLM can raise the ceiling of our
> deterministic engine.** This is the sibling of
> [`powerbi-formatting-color-reference.md`](./powerbi-formatting-color-reference.md); read that one
> for how PBIR/theme JSON *serializes* formatting, and this one for how a Tableau *view* is
> *reconstructed* as a Power BI report.

---

## 0. Provenance & clean-room statement (read first)

This study was **informed by studying a third-party open-source tool** — `cyphou/Tableau-To-PowerBI`
(a mature, ~45k-line, stdlib-only Python migrator, MIT-licensed, ~v40) — as **prior art**. Per this
repo's [`CLEANROOM.md`](../../CLEANROOM.md) we deliberately decline that MIT copy grant to keep our
originality attestation intact. Concretely:

- **What was mined:** *unprotectable facts and method* — which Tableau constructs have Power BI /
  DAX equivalents, the general shape of the extract→generate pipeline, and *which problems* a mature
  migrator has to solve. Facts about Tableau's file format and about DAX/PBIR semantics are not
  anyone's property.
- **What was NOT copied:** no source code, regexes, function names as a set, lookup/mapping tables
  in their arrangement, comments, test fixtures, or file/module structure. Every equivalence below
  is **re-derived and independently validated** against DAX / PBIR semantics and **organized in our
  own taxonomy**. Where a specific idea was *prompted* by the prior art, it carries a `[PRIOR-ART]`
  tag so provenance is auditable; the surrounding analysis, confidence ratings, correctness
  critiques, and LLM recommendations are our own.
- **Consequence for form:** unlike the formatting reference (which could quote *neutral Microsoft
  sample* `.pbix` bytes), this document contains **no verbatim third-party source**. It matches that
  document's *depth and rigor*, not its quoting style.

### Tag legend

| Tag | Meaning |
| --- | --- |
| `[FACT]` | Independently verifiable fact about Tableau's format or DAX/PBIR semantics. |
| `[PRIOR-ART]` | An approach/coverage claim *observed in the reference tool's descriptive docs*, restated in our words and independently assessed — not an endorsement that it is correct. |
| `[VALIDATED]` | We checked this against DAX / PBIR / Power BI Desktop behavior. |
| `[OUR-ANALYSIS]` | Our own critique, correction, or recommendation. |
| `⇄` | Tableau ↔ Power BI bridge note (`GAP` = no clean counterpart). |
| `🤖` | **LLM-leverage hook** — a point where deterministic rules hit a wall and model judgment adds value. |

### Confidence rating (used in every catalogue)

| Rating | Meaning for our engine |
| --- | --- |
| ✅ **Faithful** | Semantics preserved; safe to emit deterministically with high confidence. |
| 🟡 **Approximate** | Structurally close but semantic drift is possible; a candidate for LLM review / user confirmation. |
| 🔴 **Lossy / Gap** | No clean equivalent; deterministic output is a placeholder or degraded shape. LLM or human required. |

---

## 1. Orientation — what the prior-art tool is, in one screen

`[PRIOR-ART]` The reference migrator is a **purely deterministic, rules-and-regex engine** (no ML,
zero runtime deps) that runs a three-stage pipeline:

1. **EXTRACT** — parse Tableau `.twb`/`.twbx` XML (and optionally Prep `.tfl`/`.tflx`, or pull from
   Server/Cloud REST) into a set of structured intermediate JSON documents (~20–23 of them), one per
   Tableau object family (worksheets, dashboards, datasources, calculations, parameters, filters,
   actions, sets/groups/bins, hierarchies, sort orders, aliases, custom SQL, user filters, …).
2. **GENERATE** — turn that JSON into a **`.pbip` project**: a **PBIR** report (`definition/pages/*/
   visuals/*/visual.json`) plus a **TMDL** semantic model (tables, columns, measures, relationships,
   roles, cultures), built in ~14 sequential phases. An alternate `--output-format fabric` emits
   Fabric-native artifacts instead.
3. **DEPLOY** *(optional)* — push to Power BI Service or Fabric via Azure AD.

`[OUR-ANALYSIS]` The single most important thing to internalize for **our** roadmap: **because it is
100% deterministic, every place its own docs say "approximated", "needs manual adjustment",
"`BLANK()` fallback", or "migration note" is a hard ceiling of the rules paradigm.** Those are
exactly the coordinates where an LLM-in-the-loop converts a *manual step* into an *automated,
reviewable* one. This document is organized to surface those coordinates. See §10 for the
consolidated playbook.

`[OUR-ANALYSIS]` The prior art's headline coverage numbers (e.g. "190 visual types", "133+ DAX
conversions") are **capability counts, not fidelity guarantees** — the same docs quietly downgrade
many of those entries to "approximated" or "custom-visual-required". Treat big numbers as a *map of
what must be handled*, not as a bar we must clear numerically. Our differentiator is **fidelity and
honesty per construct**, not raw count.

---

## 2. The rebuild problem, decomposed (our framing)

Rebuilding a Tableau *view* in Power BI is not one translation; it is **five loosely-coupled
translations that must agree**. We use this decomposition throughout:

| # | Sub-problem | Tableau side | Power BI side | Where it lives |
| --- | --- | --- | --- | --- |
| R1 | **Chart-type selection** | worksheet mark type (+ "Show Me" intent) | `visual.visualType` | §3 |
| R2 | **Encoding → field wells** | Rows/Columns/Marks shelves (color, size, label, detail, tooltip) | `projections` / data roles | §4 |
| R3 | **Visual formatting fidelity** | worksheet format panes + rich text | PBIR `objects` / `vcObjects` (see formatting ref) | §5 |
| R4 | **Dashboard assembly** | dashboard zones (tiled + floating) | report page + visual `position` | §6 |
| R5 | **Interactivity** | actions, filters, parameters, stories | cross-filter, slicers, bookmarks, field/what-if params | §7 |

`[OUR-ANALYSIS]` These five can each be *individually* correct yet **jointly wrong** — e.g. a
faithful chart type (R1) with mis-routed encodings (R2) renders an empty or nonsensical visual. A
key LLM opportunity (§10) is a **holistic "does this reconstructed view make sense?" pass** that no
per-rule engine can do, because each rule sees only its slice. The deterministic engine optimizes
each Rn locally; the LLM can optimize the *joint*.

A sixth, cross-cutting concern — **the semantic model the view binds to** (measures, calc groups,
field parameters, a date table) — is covered in §8, because a visual is only as faithful as the
measure behind it.

---

## 3. Chart-type mapping (R1) — independently authored catalogue

### 3.1 The mark-type model you're translating

`[FACT]` A Tableau worksheet does **not** store "a bar chart". It stores a **mark class** (`bar`,
`line`, `area`, `square`, `circle`, `shape`, `text`, `map`/`polygon`, `gantt`, `pie`, `automatic`)
plus the **fields on shelves** and their **encodings**. The rendered chart type is *emergent* from
(mark class × what's on Rows/Columns × what's on the Marks card). Two worksheets with mark class
`bar` can be a clustered bar, a stacked bar, or a histogram depending on discrete/continuous fields
and stacking. `[OUR-ANALYSIS]` This is the root cause of most R1 ambiguity: **the mapping is
many-to-many, not a lookup.** A mature deterministic engine handles it by combining the mark class
with heuristics over the shelves; even so, its own docs concede a long tail of "approximated" types.

`[FACT]` Power BI, by contrast, stores a **discrete `visualType`** string per visual container (e.g.
`clusteredColumnChart`, `lineChart`, `matrix`) and expresses the rest through data-role
`projections` and the `objects` formatting bag. So R1 is: *collapse an emergent Tableau chart into
one of Power BI's finite visualType tokens*, then push the nuance into R2/R3.

`⇄` Tableau "Show Me" (the automatic viz-type recommender) has **no runtime equivalent** in Power
BI; it is a design-time affordance. Its *intent signal*, however, is latent in the saved mark class
and is worth mining. `🤖` See §3.4.

### 3.2 Catalogue — by Power BI target family

Ratings are **ours**, from validating each mapping against how the PBI visual actually consumes data
and renders. Tableau source terms are the standard mark/type names `[FACT]`; the target `visualType`
tokens are the PBIR-native identifiers `[VALIDATED]`. Entries marked `[PRIOR-ART]` are ones where the
reference tool claims a mapping we would otherwise not have reached for — flagged so we can decide
independently.

**Bar / column family** — ✅ the strongest area; near-lossless.

| Tableau view | PBI `visualType` | Rating | Notes / caveats (ours) |
| --- | --- | --- | --- |
| Horizontal bar | `clusteredBarChart` | ✅ | Orientation is a *different visualType* in PBI, not a toggle. Detect from which axis holds the dimension. |
| Vertical bar / column | `clusteredColumnChart` | ✅ | |
| Stacked bar / column | `stackedBarChart` / `stackedColumnChart` | ✅ | Requires a field on Color to stack; if stacking is off in Tableau, do **not** pick the stacked token. |
| 100% stacked | `hundredPercentStacked{Bar,Column}Chart` | ✅ | |
| Histogram | `clusteredColumnChart` over a bin column | 🟡 | PBI has no histogram primitive; fidelity depends on the **bin column** being generated in the model (see §8). Bin width must be reconstructed, not assumed. |
| Gantt bar | `ganttChart` (**custom/AppSource**) | 🔴 | `[PRIOR-ART]` Requires an AppSource custom visual GUID; not installed by the file. Time-axis semantics are preserved only if the duration measure exists. |
| Lollipop | `clusteredBarChart` (approx.) | 🔴 | Real lollipops need bar+dot dual-encode; a plain bar loses the dot. |

**Line / area family** — ✅ mostly faithful; ranking-based charts are the exception.

| Tableau view | PBI `visualType` | Rating | Notes (ours) |
| --- | --- | --- | --- |
| Line | `lineChart` | ✅ | Marker visibility is an `objects` formatting concern (R3), not a type concern. |
| Area / stacked area / 100% area | `areaChart` / `stackedAreaChart` / `hundredPercentStackedAreaChart` | ✅ | |
| Ribbon | `ribbonChart` | ✅ | Good native fit — PBI ribbon encodes rank-flow directly. |
| Bump / slope chart | `lineChart` + a generated `RANKX` measure | 🟡 | `[PRIOR-ART]` The line is trivial; the **ranking semantics** are the point and must be injected as a measure. `[OUR-ANALYSIS]` `RANKX(ALL(...), [m], , ASC, Dense)` reproduces position but the *slope-as-story* reading is lost without deliberate axis inversion. |
| Sparkline (in table) | `lineChart` mini-config / table sparkline | 🟡 | Only basic line sparklines survive; area/bar sparklines degrade to line. |

**Part-to-whole family.**

| Tableau view | PBI `visualType` | Rating | Notes (ours) |
| --- | --- | --- | --- |
| Pie | `pieChart` | ✅ | |
| Donut / ring / semicircle / rose | `donutChart` | 🟡 | Rose (Nightingale) and semicircle collapse to a plain donut — the radial-area encoding is **lost**. Flag as fidelity loss, don't silently emit donut. |
| Funnel | `funnel` | ✅ | |
| Treemap | `treemap` | ✅ | |
| Waffle / butterfly | `hundredPercentStackedBarChart` | 🔴 | `[PRIOR-ART]` "Negate one measure to fake symmetry" is a hack; a butterfly/tornado is really two mirrored bar charts. `[OUR-ANALYSIS]` Better handled as a paired-visual layout than a single stacked bar. |

**XY / distribution family.**

| Tableau view | PBI `visualType` | Rating | Notes (ours) |
| --- | --- | --- | --- |
| Scatter (circle/shape/dot plot) | `scatterChart` | ✅ | |
| Packed bubble / strip plot | `scatterChart` with Size role | 🟡 | `[PRIOR-ART]` Packing is not preserved — PBI scatter positions bubbles on axes, so a "packed" cluster becomes a spread. Size encoding must be routed to the Size well or the bubbles are uniform. |
| Box-and-whisker | `boxAndWhisker` | 🔴 | `[OUR-ANALYSIS] **Correction of a prior-art claim:** box & whisker is **not a core Power BI visual** — it is an AppSource **custom** visual. Treat as custom-visual-dependent, same tier as Sankey. Do not present it as native.** |
| Violin | box & whisker / `ViolinPlot` custom | 🔴 | Density shape is lost when degraded to box & whisker. |
| Parallel coordinates | `lineChart` / `ParallelCoordinates` custom | 🔴 | A plain line chart does not reproduce per-axis normalized parallel axes. |

**KPI / single-value family** — ⚠️ high-frequency, high-risk area.

| Tableau view | PBI `visualType` | Rating | Notes (ours) |
| --- | --- | --- | --- |
| KPI text / single number | `card` | ✅ | |
| Multi-row KPI | `multiRowCard` | ✅ | |
| Bullet graph | `gauge` | 🔴 | `[OUR-ANALYSIS] **Disagree with the prior-art mapping.** A bullet graph → `gauge` loses the qualitative range bands *and* the comparative reference marker that are the whole point. A closer PBI reconstruction is a thin `clusteredBarChart` + a `constantLine`/target, or the native **KPI** visual. Rate `gauge` as a last resort.** |
| Radial / speedometer gauge | `gauge` | 🟡 | Genuine gauge fit; still verify min/max/target routing. |
| Tableau "KPI" shape (▲▼) | `card` + conditional icon, or KPI visual | 🟡 | The status *arrow/color* is conditional formatting (R3), not the type. |

**Table / matrix family** — ✅ faithful, but the "heatmap" overload is subtle.

| Tableau view | PBI `visualType` | Rating | Notes (ours) |
| --- | --- | --- | --- |
| Text table (crosstab) | `matrix` | ✅ | Rows+Columns headers → matrix rows/columns; measures → values. |
| Text table (flat / "Automatic") | `table` / `tableEx` | ✅ | Distinguish flat list (table) from pivoted (matrix) by whether a dimension is on Columns. |
| Highlight table | `matrix` + cell conditional formatting | ✅ | The color **is** background conditional formatting on the value — see formatting ref. |
| Heat map (density) | `matrix` (cells) *or* `map` (geographic density) | 🟡 | `[OUR-ANALYSIS]` Overloaded term. A *highlight-table* heat map → matrix; a *marks-density* heat map → a density map. Disambiguate by whether the fields are geographic. `🤖` good LLM disambiguation case. |
| Calendar heat map | `matrix` with date parts on rows/cols | 🟡 | `[PRIOR-ART]` Needs weekday/week-of-year columns generated in the model; the "calendar" shape is emergent, not a type. |

**Geographic family.**

| Tableau view | PBI `visualType` | Rating | Notes (ours) |
| --- | --- | --- | --- |
| Symbol map / point map | `map` (Azure/Bing) | ✅ | Requires lat/long or a geocoded column with the right `dataCategory` (§8). |
| Filled map / choropleth / polygon | `filledMap` | 🟡 | Custom Tableau polygons (custom territories) have **no** PBI equivalent; only standard admin geographies fill correctly. |
| Multipolygon / custom geocoding | `shapeMap` (custom map) or GAP | 🔴 | Custom `.shp`/spatial polygons → manual shape-map setup at best. |

**Flow / network / hierarchy family** — 🔴 almost entirely custom-visual territory.

| Tableau view | PBI target | Rating | Notes (ours) |
| --- | --- | --- | --- |
| Sankey | `sankeyDiagram` (**custom**) | 🔴 | `[PRIOR-ART]` AppSource GUID; not auto-installed. |
| Chord | `chordChart` (**custom**) | 🔴 | Same. |
| Network | `networkNavigator` (**custom**) | 🔴 | Same. |
| Sunburst | `sunburst` (**custom**) | 🔴 | Not native. |
| Decomposition (analytic) | `decompositionTree` (native) | 🟡 | A genuinely native PBI tree; good target when a Tableau hierarchy drill is the intent. |

**Specialized.**

| Tableau view | PBI target | Rating | Notes (ours) |
| --- | --- | --- | --- |
| Waterfall | `waterfallChart` | ✅ | |
| Word cloud | `wordCloud` (**custom**) | 🔴 | Not native. |
| Image | `image` | ✅ | |
| Combo / dual-axis / Pareto | `lineClusteredColumnComboChart` / `lineStackedColumnComboChart` | 🟡 | See §3.3 — the highest-value non-trivial R1 case. |
| Motion chart (play-axis animation) | **no equivalent** | 🔴 | `[FACT]` PBI has no play-axis. Closest is a bookmark auto-advance or the (deprecated) Play-Axis custom visual. Real GAP. |

### 3.3 Dual-axis & combo — the hard, common case

`[FACT]` A Tableau **dual axis** places two continuous fields on the same axis region with
independent scales (optionally synchronized). `⇄` The PBI counterpart is a **single combo visual**
(`lineClusteredColumnComboChart`) with `Y` and `Y2` (secondary) roles — not two overlaid visuals.
`[PRIOR-ART]` The reference tool detects dual/multi-axis worksheets, maps the primary measure to the
column role and the secondary to the line role, and carries per-axis number formatting.
`[OUR-ANALYSIS]` Two correctness traps to encode as tests: (1) **which measure becomes the line vs.
column** is not arbitrary — Tableau's mark-class-per-axis dictates it, and guessing inverts the
chart; (2) **axis synchronization** — if Tableau synced the axes, PBI must share one scale, which a
naive dual-role combo will not do. `🤖` Choosing line-vs-column when the source is ambiguous, and
deciding whether to *merge* dual axes into a combo or *keep them as two stacked visuals*, is a
judgment call an LLM can make from the measures' names/units better than a rule.

### 3.4 `🤖` LLM leverage for R1

The deterministic engine's ceiling is that it maps **mark class → token by rule** and then labels the
residue "approximated". Three LLM hooks:

1. **Intent recovery from ambiguity.** When mark class + shelves under-determine the type (e.g.
   `automatic`, or a `bar` that could be histogram vs. clustered), an LLM given the field names,
   data types, aggregation, and worksheet title can pick the *intended* chart with a rationale —
   effectively reconstructing "Show Me". Feed it the R2 encoding summary, not raw XML.
2. **Degrade-with-explanation for custom visuals.** For every 🔴 custom-visual type (Sankey, word
   cloud, box & whisker, …), an LLM can choose the *best native fallback for this specific data*
   (e.g. Sankey of 2 levels → stacked bar; of N levels → decomposition tree) and write the
   migration note, instead of emitting a hard dependency the user may not install.
3. **Correctness review of the rule's own output.** Cheapest, highest-value: let the deterministic
   mapper propose, and have the LLM *flag* the known traps above (bullet→gauge band loss, rose→donut
   area loss, packed-bubble spread) so they surface as review items rather than silent fidelity
   loss.

---

## 4. Encoding → field-well routing (R2)

### 4.1 The shelf-to-role model

`[FACT]` Tableau encodes data through **shelves**: **Columns** and **Rows** (the axes/headers) and
the **Marks card** (Color, Size, Label/Text, Detail, Tooltip, Shape, Angle, Path). Power BI encodes
through **data roles** (a.k.a. field wells / `projections`) whose names differ **per visualType**
(e.g. a bar chart has `Category`, `Y`, `Legend`, `Tooltips`; a matrix has `Rows`, `Columns`,
`Values`; a scatter has `X`, `Y`, `Legend`, `Size`, `Details`). So R2 is a **per-target remapping
table**, and it can only be filled once R1 has chosen the target. `[OUR-ANALYSIS]` This ordering
dependency (R1 before R2) is why chart-type errors cascade — a wrong token exposes the wrong wells.

### 4.2 The defensible core routing `[VALIDATED]`

These hold across most cartesian visuals and are safe to emit deterministically:

| Tableau shelf/encoding | PBI data role (cartesian) | Rating | Notes (ours) |
| --- | --- | --- | --- |
| Discrete field on Columns/Rows (the axis dimension) | `Category` / axis | ✅ | The primary grouping. |
| Continuous measure on the opposite axis | `Y` (`Values`) | ✅ | Aggregation must match Tableau's (SUM vs AVG…) — carry the agg, don't default to SUM. |
| **Color** (discrete) | `Legend` / `Series` | ✅ | Drives series split + palette; ties to color legend in the formatting ref. |
| **Color** (continuous) | conditional formatting on the measure, **not** a well | 🟡 | `[OUR-ANALYSIS]` A common mis-route: continuous color is a *gradient on the value*, not a Legend field. Getting this wrong produces a categorical legend over a measure. |
| **Size** | `Size` (scatter/map/bubble) | 🟡 | Only some visualTypes expose Size; on those without it, size encoding is dropped. |
| **Label / Text** | `Values` shown as data labels, or the matrix `Values` | 🟡 | On a text table the Text shelf *is* the value; elsewhere it's a data-label toggle (R3). |
| **Detail** | `Details` / extra grouping granularity | 🟡 | Detail raises mark granularity without drawing an axis; nearest PBI notion is an extra `Details`/`Group` field. Frequently lossy. |
| **Tooltip** | `Tooltips` role | ✅ | Fields on Tooltip → Tooltips well; rich viz-in-tooltip is R3/§7. |
| **Shape** | marker-shape formatting or a custom-shape GAP | 🔴 | Custom shape images do not migrate (see §5.4). |
| **Angle** (pie) | `Values` (arc) | ✅ | |
| **Path** (line/polygon order) | line ordering / GAP for polygons | 🔴 | Path-ordered polygons have no PBI well. |

### 4.3 `🤖` LLM leverage for R2

1. **Continuous-vs-categorical color disambiguation** — decide whether a field on Color is a Legend
   split or a gradient, from the field's data type + cardinality + name. A rule uses cardinality
   thresholds; an LLM also reads intent ("Profit" → diverging gradient; "Region" → categorical).
2. **Detail-shelf salvage** — Detail is the least 1:1 shelf. An LLM can decide whether a Detail field
   should become an extra category, a tooltip, or be dropped, per visual.
3. **Aggregation sanity** — flag when a routed measure's default aggregation would silently differ
   from Tableau's (e.g. Tableau `AVG` but PBI implicit `Sum`), which is invisible to a shelf-only
   rule but obvious from the calc metadata.

---

## 5. Visual formatting & "pixel-perfect" fidelity (R3)

> This section is the *view-rebuild* companion to
> [`powerbi-formatting-color-reference.md`](./powerbi-formatting-color-reference.md). That doc is the
> authority on **how** each property serializes into PBIR `objects`/`vcObjects` + theme JSON; here we
> catalogue **what a Tableau worksheet carries** and how faithfully it survives.

### 5.1 The four fidelity axes `[PRIOR-ART]` (restated & assessed)

The reference tool frames visual fidelity as four largely-independent problems. We adopt the framing
because it is a genuinely useful decomposition, and assess each:

1. **Text / typography fidelity** — run-level font attributes (family, size, weight, italic,
   underline, color) from Tableau rich-text runs → PBIR textbox/label payloads. `[FACT]` Tableau
   stores formatted text as a sequence of **runs**, each with its own attributes; a single title can
   hold several runs. `[OUR-ANALYSIS]` The correct unit of migration is therefore the *run*, not the
   string — collapsing to one style per textbox is the common fidelity bug.
2. **Per-visual "chrome"** — the container's **background fill and border** (and title/shadow) from
   Tableau's worksheet/zone format → PBIR `vcObjects` (`background`, `border`, `visualHeader`).
3. **Sentinel / artifact cleanup** — `[FACT]` Tableau XML text runs carry soft-line-break and NBSP
   **sentinel glyphs** (artifacts) that, if passed through, render as stray characters in PBI. A
   fidelity pass must strip them. `[OUR-ANALYSIS]` This is a non-obvious, high-value detail — worth a
   dedicated cleanup + a QA check that asserts zero sentinel glyphs in output.
4. **Layout / overlay fidelity** — z-order and floating-zone overlap (covered under R4/§6).

### 5.2 Text-run fidelity, in detail `[FACT]`/`[VALIDATED]`

| Tableau run attribute | Where it comes from | PBI target | Rating |
| --- | --- | --- | --- |
| Font family | run `fontname` | textbox/label `fontFamily` | ✅ |
| Font size (pt) | run `fontsize` | `fontSize` | ✅ |
| Bold / italic / underline | run flags | `bold`/`italic`/`underline` | ✅ |
| Color | run `fontcolor` (+ alpha) | `color` (hex) | ✅ |
| **Per-paragraph horizontal alignment** | Tableau `fontalignment` code `1`=left, `2`=center, `3`=right, `4`=justify `[FACT]` | paragraph alignment in textbox payload | 🟡 |
| **Vertical anchor** (top/middle/bottom) | zone-level text anchor | textbox vertical alignment | 🟡 |
| Mixed alignment within one textbox | multiple paragraphs | per-paragraph alignment array | 🟡 |

`[OUR-ANALYSIS]` The `fontalignment` integer codes are a concrete, verifiable Tableau-format fact
worth hard-coding in our extractor. The 🟡 items are where PBIR's textbox model is *less expressive*
than Tableau's (e.g. justify support, vertical anchoring inside labels), so round-trips can drift.

### 5.3 Per-visual chrome `[VALIDATED]`

| Concept | Tableau source | PBI target (`vcObjects`) | Rating |
| --- | --- | --- | --- |
| Visual background fill + alpha | worksheet/zone shading | `background.color` + `transparency` | ✅ |
| Border (color/width) | zone border | `border.color` / `radius` | 🟡 (PBI border is uniform; Tableau per-side borders don't survive) |
| Title text + style | worksheet title runs | `visualHeader`/`title` | ✅ |
| Drop shadow | zone shadow | `shadow` | 🟡 |
| Row banding (tables) | table format | table/matrix `grid`+`values` alt bg | ✅ |

`[OUR-ANALYSIS]` **Per-side borders** and **partial shading** are the recurring chrome losses —
Tableau allows independent top/bottom/left/right rules; PBIR's `border` is a single spec. Detect and
warn rather than silently pick one side.

### 5.4 Known chrome/format GAPs `[FACT]`

- **Custom shape images** — Tableau custom-shape palettes reference image files; only the *field*
  encoding migrates, not the images. Visuals fall back to default markers. `⇄` GAP.
- **Rich HTML tooltips** — Tableau viz-in-tooltip / HTML layout degrades to run-level text; complex
  layouts are not reproduced (see §7).
- **Custom color palettes per value** — discrete value→specific-color maps beyond gradient/stepped
  are not fully replicated (see formatting ref's conditional-formatting section).

### 5.5 `🤖` LLM leverage for R3

1. **Theme synthesis instead of per-visual replication.** A rule copies each visual's explicit
   format. An LLM can *recognize the workbook's design system* (its recurring palette, title font,
   corporate accent) and propose a **Power BI theme** (`dataColors`, `textClasses`) so most visuals
   inherit correctly and only exceptions carry overrides — closer to how a human rebuilds, and it
   collapses hundreds of per-visual overrides into one theme. This directly leverages the sibling
   formatting reference.
2. **Ambiguous-format resolution** — when Tableau expresses something PBIR can't (per-side border,
   justified text, partial shading), an LLM can pick the closest faithful compromise *and explain the
   tradeoff* in the migration note.
3. **Sentinel/garbage detection beyond the known set** — the deterministic pass strips *known*
   sentinel glyphs; an LLM can catch novel encoding artifacts in titles/labels that no rule
   anticipated.

---

## 6. Dashboard → report-page assembly (R4)

### 6.1 The layout models `[FACT]`

A Tableau **dashboard** composes worksheets into **zones** using two placement modes:

- **Tiled** — zones flow inside a layout tree of horizontal/vertical **containers** that split space
  proportionally. Position is *relational*, not absolute.
- **Floating** — zones have absolute x/y/w/h in dashboard pixels and a **z-order**.

Power BI report pages, by contrast, position every visual with **absolute** `x`/`y`/`z`/`width`/
`height` (in page units) — there is **no container/flow model** in PBIR. `[OUR-ANALYSIS]` So R4 is
fundamentally a **flatten-the-tree** problem: resolve Tableau's proportional container tree into
absolute rectangles, then map into the page's coordinate space. This is lossy in one direction that
matters — **responsive re-flow is gone**; a Power BI page is a fixed canvas.

### 6.2 What survives, what drifts `[PRIOR-ART]`/`[OUR-ANALYSIS]`

| Concern | Behavior | Rating |
| --- | --- | --- |
| Dashboard size (fixed) | → page `width`/`height` | ✅ |
| Tiled container tree | flattened to absolute rects, often **grid-snapped** to clean coordinates | 🟡 — nesting beyond ~3 levels loses precision `[PRIOR-ART]` |
| Floating zones + z-order | absolute position + `z` preserved; overlapping zones **staggered** to avoid exact overlap | 🟡 — see §6.3 |
| Proportional/responsive layout | **lost** — PBI canvas is fixed | 🔴 GAP |
| Device-specific (phone) layouts | → PBI mobile layout (separate) | 🟡 |
| Dashboard background color/image | → page wallpaper/canvas background (formatting ref §page) | ✅ |
| Legends as separate floating zones | can render **beside** instead of overlaying the chart | 🔴 known caveat `[PRIOR-ART]` |

### 6.3 Z-order & overlap — a determinism lesson `[PRIOR-ART]`/`[OUR-ANALYSIS]`

`[PRIOR-ART]` The reference tool had a subtle bug worth stealing the *lesson* from: its overlap-heal
pass iterated visuals in **filesystem/`uuid4` order**, so *which* of two overlapping zones got moved
changed run-to-run (non-deterministic across hash seeds). The fix was to sort by a stable
`(z, tabOrder, name)` key before nudging foreground zones. `[OUR-ANALYSIS]` **Design principle for
our engine:** any layout-repair or de-overlap step must be **total-ordered by a stable key**, never
by dict/listdir iteration — otherwise golden-file tests flake and output isn't reproducible. We
should encode this as a test invariant (same input → byte-identical layout).

### 6.4 `🤖` LLM leverage for R4

1. **Semantic layout reconstruction.** Flattening a container tree to pixels preserves *geometry*
   but not *grouping intent*. An LLM can read the dashboard (titles, zone adjacency, filter zones)
   and propose a **cleaner Power BI page** — aligning to a grid, grouping related visuals, placing
   slicers in a rail — rather than a literal pixel transcription that looks cramped. This is "rebuild
   like a human would", the thing rules can't do.
2. **Legend/annotation placement** — decide when a floating legend/text zone should overlay a chart
   corner vs. sit beside it (the known caveat), from the zone's role and size.
3. **Multi-page splitting** — a dense Tableau dashboard sometimes *should* become 2–3 PBI pages; an
   LLM can recommend the split and a navigation scheme (§7 bookmarks).

---

## 7. Interactivity (R5)

### 7.1 Actions `[FACT]`/`[VALIDATED]`

| Tableau action | PBI counterpart | Rating | Notes (ours) |
| --- | --- | --- | --- |
| **Filter action** (click filters other sheets) | native **cross-filter** between visuals; or a **drill-through page** for select-then-navigate | 🟡 | PBI cross-filter is largely automatic within a page but **less configurable** — Tableau's per-target field mapping and "exclude all values on clear" have no exact knob. `⇄` partial. |
| **Highlight action** | **cross-highlight** | 🟡 | Similar caveat; highlight vs. filter is a per-visual interaction setting. |
| **URL action** | web-URL button / data-bound URL | 🟡 | Data-driven URLs need a measure returning the URL. |
| **Go-to-sheet / navigation** | **page-navigation button/bookmark** | 🟡 | |
| **Parameter/Set actions** (2020.2+) | field parameter / what-if + bookmarks | 🔴 | `[OUR-ANALYSIS]` Set actions (click to add/remove from a set) have no first-class PBI equivalent; approximate with a what-if + measure logic, or flag as GAP. |

### 7.2 Filters `[VALIDATED]`

| Tableau | PBI | Rating |
| --- | --- | --- |
| Categorical (list) filter | Basic filter / slicer | ✅ |
| Quantitative (range) filter | Advanced (numeric) filter / slider slicer | ✅ |
| Relative date filter | Relative-date filter/slicer | ✅ |
| Date range filter | Between date filter | ✅ |
| Top-N filter | Top-N filter | 🟡 — reference-field & ties handling differ |
| Wildcard/match filter | search filter | 🟡 |
| **Context filter** | report-level filter (approx.) | 🟡 — `[OUR-ANALYSIS]` context filters also change LOD/compute scope in Tableau; a report-level filter changes *filter scope* only, so a context filter feeding a FIXED LOD is **not** faithfully reproduced by a report filter alone. |
| Filter scope: worksheet / dashboard / datasource | visual-level / page-level / (model) | ✅ mapping of scope tiers |

`[FACT]` **Filter-scope tiering is a clean, faithful mapping**: worksheet→visual-level,
dashboard→page-level, context→report-level, datasource→model/query. This tier correspondence is one
of the most reliable R5 facts.

### 7.3 Parameters `[VALIDATED]`

| Tableau parameter | PBI construct | Rating | Notes (ours) |
| --- | --- | --- | --- |
| Numeric range | **What-If parameter** (`GENERATESERIES` table + slicer) | ✅ | Referenced in DAX via `SELECTEDVALUE('P'[P Value], default)`. |
| List (of values) | field parameter / query parameter table | 🟡 | Dimension-switching lists → **field parameters** (`NAMEOF`); value-lists → what-if. |
| Date | query parameter / date slicer | 🟡 | |
| String (free text) | query parameter | 🟡 | No true free-text runtime parameter on a report; approximate. |
| **Dimension-switch parameter** (swap the field on a shelf) | **Field Parameter** table | ✅ | `⇄` This is the *canonical* modern PBI answer and a strong fit. |
| **Measure-swap parameter** | **Calculation Group** or field parameter | 🟡 | Calc groups are the faithful answer but need external-tools/TMDL authoring. |

`[OUR-ANALYSIS]` The parameter→(field parameter / calc group / what-if) fork is one of the highest-
leverage *modeling* decisions in the whole migration, and it's genuinely hard to pick by rule.
`🤖` strong LLM case (§7.5).

### 7.4 Stories `[FACT]`

| Tableau story element | PBI counterpart | Rating |
| --- | --- | --- |
| Story | a **bookmark collection** + navigator | 🟡 |
| Story point | a **bookmark** (captured state) | 🟡 |
| Caption | bookmark name / text | 🟡 |
| Navigation | previous/next bookmark buttons | 🟡 |

`[OUR-ANALYSIS]` Stories map *structurally* to bookmarks but **not experientially** — a Tableau story
point captures a whole annotated narrative frame; a bookmark captures visual/filter state. Expect to
rebuild the narrative text as textboxes. `⇄` partial.

### 7.5 `🤖` LLM leverage for R5

1. **Parameter strategy selection** — given a Tableau parameter's usage sites (does it swap a
   dimension, a measure, or feed a numeric threshold?), pick field-parameter vs. calc-group vs.
   what-if and generate the matching model object. This needs reading *how the parameter is used
   across calcs and shelves* — exactly the cross-cutting context an LLM handles and a local rule
   misses.
2. **Action → interaction intent** — decide whether a Tableau filter action should become in-page
   cross-filtering or a drill-through page, from the action's source/target topology.
3. **Story narrative reconstruction** — turn story captions + point states into a coherent bookmark
   tour *plus* the explanatory textboxes, preserving the narrative a bare bookmark loses.

---

## 8. The semantic model a view binds to

A visual is only as faithful as the field/measure behind it. Rebuilding views therefore drags in a
chunk of model construction. `[PRIOR-ART]` The reference tool builds its TMDL model in ~14 sequential
phases; the *view-relevant* ones are below (we ignore pure plumbing like dedup and cultures here).

### 8.1 Model objects that views depend on

| Model object | Why the view needs it | PBI construct | Rating |
| --- | --- | --- | --- |
| **Measures** (from Tableau calcs) | every value on a shelf | DAX measure | see §8.2 |
| **Bins** | histogram / binned axis | calculated column (bin) | ✅ |
| **Sets** | set-based color/filter | calc column (boolean membership) | 🟡 |
| **Groups** | grouped dimension | calc column (SWITCH/mapping) | ✅ |
| **Hierarchies** | drill path | PBI hierarchy | ✅ |
| **Date / Calendar table** | any time-intel measure, date-part axes | generated Calendar + date hierarchy | ✅ |
| **Field parameters** | dimension-switch params (§7.3) | field-parameter table (`NAMEOF`) | ✅ |
| **Calculation groups** | measure-swap / reused format | calc-group table | 🟡 (needs TMDL authoring) |
| **RLS roles** | user-filtered views | TMDL `role` + filter expr | 🟡 |
| **Relationships** | cross-table visuals | model relationships (inferred) | 🟡 — see §8.4 |

### 8.2 Calc → DAX — our validated taxonomy

`[OUR-ANALYSIS]` We organize by **translation difficulty**, not alphabetically, because difficulty is
what drives test priority and LLM routing. Every row is independently validated against DAX
semantics; `[PRIOR-ART]` marks a specific handling we saw claimed and then checked.

**Tier A — direct / near-direct (✅, deterministic-safe).**

- Aggregations: `SUM/MIN/MAX/AVG→AVERAGE`, `COUNT`, `COUNTD→DISTINCTCOUNT`, `MEDIAN`,
  `STDEV→STDEV.S`, `VAR→VAR.S`. `[VALIDATED]` The only trap is **sample-vs-population** (`STDEV.S` vs
  `STDEV.P`); Tableau's default is sample, so `.S` is correct — encode it, don't guess.
- Math: `ABS, ROUND, SQRT, POWER, EXP, LOG` map 1:1; `CEILING→ROUNDUP(x,0)`,
  `FLOOR→ROUNDDOWN(x,0)` `[VALIDATED]` (note DAX also has `CEILING`, but the round-form avoids a
  significance-arg mismatch).
- Text: `LEFT/RIGHT/MID/UPPER/LOWER/LEN/TRIM` 1:1; `REPLACE→SUBSTITUTE` (name/semantics differ —
  Tableau `REPLACE` is substring-by-value, matching `SUBSTITUTE`, **not** DAX `REPLACE` which is
  positional — an easy, dangerous mix-up `[OUR-ANALYSIS]`); `CONTAINS→CONTAINSSTRING`.
- Type-aware operators: `=`↔`=`, `!=`/`==`→`<>`/`=`, `AND/OR/NOT`→`&&`/`||`/`NOT`.

**Tier B — structural rewrites (✅/🟡, deterministic but test-heavy).**

- `IF/THEN/ELSEIF/ELSE/END` → nested `IF(...)`; `CASE WHEN` → `SWITCH(TRUE(), …)`. ✅
- **`AGG(IF(...))` → iterator**: `SUM(IF(...))`→`SUMX(table, IF(...))`, likewise `AVERAGEX`,
  `MINX`… `[VALIDATED]` Necessary because DAX `SUM` takes a column, not a row expression. This is one
  of the **most impactful** rewrites — many Tableau measures are `SUM(IF …)`.
- **`AGG(expr)` → `AGGX`** when the arg is an expression (`SUM(a*b)`→`SUMX(T, a*b)`).
- String concatenation `+` → `&` — **only when operands are strings** `[OUR-ANALYSIS]` this requires
  *type inference*; a purely lexical `+`→`&` corrupts numeric addition. The reference tool leans on a
  datatype flag; we should treat "is this `+` string or numeric?" as a **typed** decision and, when
  the type is unknown, a `🤖` review point.
- Column qualification: bare `[col]`→`'Table'[col]`; cross-table→`RELATED('T'[c])` for many-to-one,
  `LOOKUPVALUE(...)` for many-to-many. `[VALIDATED]` Correct — but *depends on the relationship
  cardinality being right* (§8.4), so a modeling error silently corrupts calcs.

**Tier C — LOD expressions (🟡, correctness-critical).**

`[VALIDATED]` The defensible core:

| Tableau LOD | DAX pattern | Note (ours) |
| --- | --- | --- |
| `{FIXED : AGG}` (no dims) | `CALCULATE(AGG, ALL('T'))` | grand total ignoring filters |
| `{FIXED [a],[b] : AGG}` | `CALCULATE(AGG, ALLEXCEPT('T','T'[a],'T'[b]))` | keep only a,b |
| `{INCLUDE [a] : AGG}` | context-add; often `CALCULATE(AGG)` in a finer grouping | weakest 1:1 |
| `{EXCLUDE [a] : AGG}` | `CALCULATE(AGG, REMOVEFILTERS('T'[a]))` | remove a from context |

`[OUR-ANALYSIS]` Two hazards to bake into tests: (1) **`INCLUDE` has no crisp DAX twin** — it depends
on the *visual's* grouping, so a model-only translation is inherently approximate; (2) **nested LODs**
(`{FIXED … : {INCLUDE … : AGG}}`) parsed by text/brace-matching are fragile — deep nesting can
produce wrong `CALCULATE` nesting. Both are `🤖` prime candidates (§8.5).

**Tier D — table calcs / window functions (🟡, semantic drift likely).**

- `RUNNING_SUM/AVG/COUNT` → `CALCULATE(AGG, FILTER(ALLSELECTED(...), …))`; `WINDOW_SUM/AVG/MIN/MAX`
  → `CALCULATE(inner, ALL/ALLEXCEPT)` with `OFFSET`-based frame bounds; `TOTAL(x)`→
  `CALCULATE(x, ALL('T'))`. `[VALIDATED]` Structurally reasonable; **frame semantics (ROWS vs RANGE,
  partition/addressing direction)** are where drift creeps in.
- `RANK`/`RANK_DENSE`/`RANK_MODIFIED`/`RANK_PERCENTILE` → `RANKX` variants; `INDEX()`→`ROWNUMBER()`
  (2024+); `SIZE()`→`COUNTROWS(ALLSELECTED())`; `PREVIOUS_VALUE`/`LOOKUP`→`OFFSET`-based patterns.
  `[OUR-ANALYSIS]` The **partition/addressing ("Compute Using")** dimension is the crux — Tableau
  table calcs are defined *relative to the viz layout*, which a model-side DAX measure cannot see. Any
  table-calc translation that ignores compute-using is a coin-flip.

**Tier E — approximated (🟡→🔴).**

- Regex: `REGEXP_MATCH/EXTRACT/REPLACE` → smart-detected `LEFT/RIGHT/CONTAINSSTRING/SUBSTITUTE` for
  *simple* patterns; complex PCRE → `BLANK()` or a Power Query `Text.Regex*` fallback. 🔴 for real
  regex. `[FACT]` DAX has no native regex.
- Stats: `CORR/COVAR/COVARP` → `SUMX`/`VAR` Pearson expansions 🟡; `ATTR()`→`SELECTEDVALUE()` 🟡
  (returns blank on multiple values — matches ATTR's `*`); `SPLIT`→`PATHITEM(SUBSTITUTE(...))` 🟡
  (abuses a pipe delimiter, breaks if data contains it — `[OUR-ANALYSIS]` fragile).

**Tier F — no DAX equivalent (🔴, hard GAP).**

- **Spatial**: `MAKEPOINT, MAKELINE, DISTANCE, BUFFER, AREA, INTERSECTION, HEXBINX, HEXBINY,
  COLLECT` → `0`/`BLANK()` placeholder. `[FACT]` No spatial algebra in DAX. Workaround = Azure Maps /
  R-Python visual.
- **Analytics-extension scripts**: `SCRIPT_BOOL/INT/REAL/STR` (R/Python) → a Python/R **script
  visual** carrying the code + a `BLANK()` measure for non-visual use. 🔪 requires runtime setup.

### 8.3 A worked correctness critique `[OUR-ANALYSIS]`

To demonstrate the *independent-validation* posture (and why we don't just trust prior art): a naive
`ISNULL`+`ZN` composite like `IF ISNULL([d]) THEN 0 ELSE ZN([d]) END` can deterministically expand to
a DAX form that **evaluates `ISBLANK([d])` twice** (once for the outer IF, once inside the `ZN`
expansion). It's *correct* but **redundant**, and on a heavy measure that double-touch is wasted work.
Our engine should include a **simplifier pass** (or an LLM cleanup) that folds
`IF(ISBLANK(x),0,IF(ISBLANK(x),0,x))` → `IF(ISBLANK(x),0,x)`. This is the kind of thing a rule
*produces* and an optimizer/LLM should *clean*.

### 8.4 Relationship inference `[PRIOR-ART]`/`[OUR-ANALYSIS]`

Tableau workbooks don't always carry an explicit star schema, so the model's relationships are
**inferred** — by column-name similarity, key markers (`*_id`), type compatibility, and cardinality
from column-overlap (Jaccard) scoring, with cycle prevention. `[OUR-ANALYSIS]` This is inherently
probabilistic and **wrong-cardinality silently corrupts every cross-table calc** (§8.2 Tier B). It is
therefore both a top fidelity risk **and** a top `🤖` opportunity: an LLM reading table+column names
and sample semantics can judge "is Orders→Customers many-to-one?" with more context than a name-
similarity score, and can explain low-confidence guesses for user confirmation.

### 8.5 `🤖` LLM leverage for the model

1. **LOD & table-calc translation with context.** Give the LLM the calc *plus* where it's used
   (which viz, grouping, compute-using) and let it produce/verify the `CALCULATE`/`OFFSET` shape —
   the context a model-only rule lacks. Keep the deterministic output as the default and use the LLM
   as a validator/repairer, not a from-scratch author (preserves testability).
2. **Relationship cardinality adjudication** (§8.4).
3. **DAX simplification & time-intelligence naming** — fold redundant patterns (§8.3), and name/shape
   generated YTD/PY/YoY measures the way an analyst would.
4. **Parameter-object selection** (§7.5) — field parameter vs. calc group vs. what-if.

---

## 9. The gap map — what deterministic rebuild cannot do

This is the single highest-value section for de-risking our roadmap: a consolidated, severity-ranked
inventory of **where any rules engine stops**, drawn from the prior art's own admissions
(`[PRIOR-ART]`) and our validation (`[OUR-ANALYSIS]`). Each row names the **LLM/human escape hatch**.

### 9.1 🔴 HIGH — no clean Power BI / DAX equivalent exists

| Gap | Tableau feature | Deterministic output | Escape hatch |
| --- | --- | --- | --- |
| Spatial functions (9) | `MAKEPOINT/MAKELINE/DISTANCE/BUFFER/AREA/INTERSECTION/HEXBIN*/COLLECT` | `0`/`BLANK()` + note | Azure Maps or R/Python visual; LLM writes the note + suggests visual |
| Analytics-extension scripts | `SCRIPT_*` (R/Python) | script-visual + `BLANK()` | preserve code in Python/R visual; needs runtime |
| Motion / play-axis animation | animated play axis | dropped | bookmark auto-advance approximation |
| Custom-visual charts | Sankey, Chord, Network, Sunburst, Word Cloud, Violin, Parallel Coords, box & whisker, Gantt | custom-visual GUID (not installed) | LLM chooses best **native** fallback for the data + note |
| Custom shape images | shape-encoded marks | field only, default markers | GAP — flag |
| Custom polygons / geocoding | custom territories | none/`shapeMap` manual | GAP — flag |

### 9.2 🟡 MEDIUM — approximated / partial

| Gap | Why it drifts | Escape hatch |
| --- | --- | --- |
| Complex regex calcs | DAX has no regex; only simple patterns fold | Power Query `Text.Regex*`; LLM to translate pattern |
| Nested / `INCLUDE` LODs | text-parsed; context-dependent | LLM validate/repair with usage context |
| Table calcs w/ compute-using | model DAX can't see viz layout | LLM + explicit partition mapping |
| Data blending | cross-datasource `[ref.xxx]` links partial | LLM to wire blend → merge/relationship |
| Context filters feeding LODs | report filter ≠ compute-scope change | flag; may need model rework |
| Dual-axis line/column assignment & sync | which measure is line vs column; shared scale | LLM from measure names/units (§3.3) |
| Rich HTML tooltips | degrade to run-level text | LLM to rebuild layout in a tooltip page |
| Set actions | no first-class PBI twin | what-if + measure logic or GAP |
| Relationship cardinality | probabilistic inference | LLM adjudication (§8.4) |
| OAuth/SSO connector auth | tokens stripped by design | manual re-auth (out of scope for view rebuild) |

### 9.3 🔵 LOW — cosmetic / edge

Proportional→pixel layout is not pixel-perfect; per-side borders collapse; rose/semicircle→donut area
loss; floating legends may sit beside not over; sparkline variants degrade to line. `[OUR-ANALYSIS]`
Individually minor, collectively they're the difference between "recognizable" and "faithful" — a good
place for an LLM *polish* pass rather than per-rule effort.

### 9.4 Reading the gap map

`[OUR-ANALYSIS]` Notice the shape: **the deepest gaps are not in chart-type tokens (R1) — those are
mostly solved — but in (a) calc semantics that depend on viz context (LOD/table-calc), (b) modeling
judgment (relationships, parameter strategy), and (c) "make it look intentional" layout/theme work.**
That tells us where LLM investment pays off: *not* re-deriving the mark→visual table (a rule does that
fine), but the context-dependent and judgment-dependent middle.

---

## 10. The LLM-enhancement playbook (the payoff)

> The consolidated answer to "how do we use our LLM to enhance the deterministic engine?" The unifying
> thesis: **keep the deterministic engine as the fast, testable, reproducible spine; add the LLM as a
> bounded, auditable co-processor at the exact points where rules provably stop.** Never let the LLM
> free-author what a rule can do deterministically — that sacrifices testability, the prior art's
> greatest strength.

### 10.1 The architectural pattern — *propose → review → repair*, not *generate*

`[OUR-ANALYSIS]` The safest, highest-yield integration is a **three-role loop** per artifact
(measure, visual, page):

1. **Deterministic PROPOSE** — the rules engine emits its best-effort artifact *plus a confidence
   signal* (it already knows when it fell back to `BLANK()`, a custom-visual GUID, or an "approximated"
   path).
2. **LLM REVIEW** — the model sees the proposal + source context and either ✅ passes it, or flags a
   specific defect from the known catalogue (this doc's 🟡/🔴 rows). Cheap, bounded, and it *never*
   replaces a passing deterministic result.
3. **LLM REPAIR (only on flag)** — for flagged items, the LLM produces a corrected artifact that is
   then **re-validated deterministically** (DAX parses? visualType valid? PBIR schema ok?) before
   acceptance. The rule engine is the gate on the LLM's output, closing the loop.

This preserves determinism for the 80–90% the rules nail, spends tokens only on the residue, and keeps
every LLM edit behind a machine check.

### 10.2 The confidence router

`[OUR-ANALYSIS]` Make the hand-off **signal-driven**, not blanket. Route to the LLM only when the
deterministic stage raises one of:

- `FALLBACK` — emitted a placeholder (`0`, `BLANK()`, `#table()` stub, uninstalled custom visual).
- `APPROX` — took a known-approximate path (regex fold, table-calc frame, INCLUDE LOD, bullet→gauge).
- `AMBIGUOUS` — under-determined choice (chart type from `automatic`, line-vs-column, continuous-vs-
  categorical color, relationship cardinality, parameter strategy).
- `LOW-FIDELITY-JOINT` — all sub-rules passed locally but the joint view looks wrong (empty visual,
  role/type mismatch) — the §2 "does this make sense?" check.

Everything else ships deterministically untouched. This is how you get LLM quality at rules cost.

### 10.3 The twelve concrete hooks (consolidated from §3–§8)

| # | Hook | Stage | Section | Value |
| --- | --- | --- | --- | --- |
| 1 | Chart-type intent recovery ("reconstruct Show Me") | AMBIGUOUS | §3.4 | High |
| 2 | Best native fallback for custom-visual charts + note | FALLBACK | §3.4 | High |
| 3 | Correctness review of mapped chart (trap flags) | REVIEW | §3.4 | High |
| 4 | Continuous-vs-categorical color routing | AMBIGUOUS | §4.3 | High |
| 5 | Detail-shelf salvage | AMBIGUOUS | §4.3 | Med |
| 6 | Aggregation-mismatch flag | REVIEW | §4.3 | High |
| 7 | **Workbook theme synthesis** (palette/text → PBI theme) | REPAIR | §5.5 | **Very high** |
| 8 | Ambiguous-format compromise + note (per-side border, justify) | REPAIR | §5.5 | Med |
| 9 | **Semantic layout reconstruction** (rebuild page like a human) | REPAIR | §6.4 | **Very high** |
| 10 | Parameter-strategy selection (field param/calc group/what-if) | AMBIGUOUS | §7.5, §8.5 | High |
| 11 | LOD / table-calc translation w/ usage context + repair | APPROX | §8.5 | High |
| 12 | Relationship-cardinality adjudication | AMBIGUOUS | §8.4 | High |

`[OUR-ANALYSIS]` If we could only build **three**, build **#7 (theme synthesis)**, **#9 (semantic
layout)**, and **#3/#6 (review flags)** — they convert the largest amount of silent fidelity loss into
either automatic wins (7, 9) or surfaced review items (3, 6), and none of them requires the LLM to
author DAX from scratch.

### 10.4 Guardrails

- **Determinism budget.** The LLM must never touch an artifact the router didn't flag; golden-file
  tests run on the deterministic path so regressions stay visible.
- **Always re-validate LLM output** through the same schema/DAX/visualType validators the rules pass.
- **Preserve provenance.** Every LLM-touched artifact carries a note (original formula/type + why it
  was changed), mirroring the migration-note discipline the prior art already uses — this is also how
  a reviewer trusts the output.
- **Prefer flag-over-fix when uncertain.** A surfaced review item beats a confident wrong repair.

---

## 11. Method lessons — what to emulate, what to diverge from

`[OUR-ANALYSIS]` Independent of any code, the *engineering posture* of a mature migrator is itself
mineable knowledge. What we should adopt, and where we should deliberately differ:

### 11.1 Emulate

- **Intermediate JSON contract.** Extract Tableau into a stable set of typed JSON documents *before*
  generating anything. It decouples parsing from emission, makes each side testable in isolation, and
  gives the LLM a clean, structured surface to reason over (far better than raw `.twb` XML).
- **Phased model build.** Constructing the semantic model in explicit ordered phases (tables →
  relationships → sets/groups → date table → params → RLS → cleanup) makes dependencies and failure
  points legible. Mirror the *idea*, author our own phases.
- **Migration notes as a first-class artifact.** Every approximation carries a machine-readable note.
  This is what makes the output trustworthy and is the natural home for LLM provenance (§10.4).
- **A QA report card with hard gates.** The prior art ships a small, concrete post-migration check
  set — e.g. *zero sentinel glyphs, zero empty visuals, full format coverage, all zones matched, no
  orphan filters, fidelity ≥ threshold* — wired to a strict exit code. `[OUR-ANALYSIS]` We should ship
  an equivalent skill-level QA card; it doubles as the acceptance test for LLM repairs (§10.1).
- **Determinism as a test invariant.** The z-order lesson (§6.3): total-order every repair by a stable
  key so `same input → byte-identical output`. Non-negotiable for golden tests.
- **Self-healing fallback cascade.** Degrade a failed visual to a simpler type rather than emit a
  broken one — but (our addition) *record the degrade* so the LLM review can reconsider it.

### 11.2 Diverge

- **Don't chase capability-count vanity.** "190 visual types / 133 conversions" mixes faithful,
  approximate, and custom-visual-dependent into one number. We report **fidelity per construct**
  (this doc's ratings), not a headline count.
- **Don't treat approximations as done.** The prior art marks many 🟡 items "✅ IMPROVED" while the
  underlying semantics remain approximate (table-calc frames, INCLUDE LODs, regex). We keep them
  visibly 🟡 and route them to review.
- **Add the LLM layer they structurally can't.** A stdlib-only, zero-dep engine *cannot* do intent
  recovery, theme synthesis, or semantic layout. That's precisely our differentiator (§10), not a
  reimplementation of their rules.
- **Correct the mismappings.** Ship the fixes this doc validated: box & whisker is **custom** not
  native; bullet graph should prefer a bar+target/KPI over `gauge`; `REPLACE`→`SUBSTITUTE` (never DAX
  `REPLACE`); `STDEV`→`.S`.

---

## 12. Quick-reference — construct coverage matrix

`[OUR-ANALYSIS]` Independently authored summary; ratings are ours (§ links carry the reasoning).
"Escape" = the intended LLM/human hook when deterministic fidelity is 🟡/🔴.

| Area | Construct | Rating | Escape (LLM hook #) |
| --- | --- | --- | --- |
| R1 chart | Bar/column/line/area/pie/funnel/treemap/waterfall/scatter/table/matrix/combo-core | ✅ | — |
| R1 chart | Histogram, bump/slope, calendar heatmap, dual-axis assignment | 🟡 | #1, #3, #11 |
| R1 chart | Bullet(→bar/KPI), rose/semicircle, packed bubble, filled/custom map | 🟡 | #2, #3 |
| R1 chart | Sankey/Chord/Network/Sunburst/WordCloud/Violin/ParCoords/BoxWhisker/Gantt (custom) | 🔴 | #2 |
| R1 chart | Motion/animation | 🔴 | GAP |
| R2 encoding | Axis dim, measure Y, discrete color→legend, tooltip, angle | ✅ | — |
| R2 encoding | Continuous color, size, detail, shape, path | 🟡/🔴 | #4, #5, #6 |
| R3 format | Run-level font, background, title, row banding | ✅ | #7 |
| R3 format | Per-paragraph align, vertical anchor, per-side border, shadow | 🟡 | #7, #8 |
| R3 format | Custom shapes, rich HTML tooltip, per-value palettes | 🔴 | #8 |
| R4 layout | Fixed size, tiled(≤3 deep), floating+z, backgrounds | 🟡 | #9 |
| R4 layout | Responsive re-flow, deep nesting, floating legend overlay | 🔴 | #9 |
| R5 interact | Filter-scope tiers, cross-filter, relative/range/topN filters | ✅/🟡 | — |
| R5 interact | Highlight/URL/nav actions, wildcard, context-filter-as-LOD | 🟡 | #10 |
| R5 interact | Set actions, stories(narrative) | 🔴 | #10 |
| R5 params | Numeric→what-if, dimension-switch→field param | ✅ | — |
| R5 params | List/date/string, measure-swap→calc group | 🟡 | #10 |
| Model calc | Tier A (aggs/math/text), Tier B (IF/CASE, AGG(IF), qualify) | ✅ | #11 (review) |
| Model calc | Tier C LOD (FIXED/EXCLUDE) | 🟡 | #11 |
| Model calc | Tier C INCLUDE/nested, Tier D table calcs/window | 🟡 | #11 |
| Model calc | Tier E regex/stats/split, Tier F spatial/script | 🔴 | #11, GAP |
| Model | Bins/groups/hierarchies/date table/field params | ✅ | — |
| Model | Relationships (inferred), calc groups, RLS | 🟡 | #12 |

**How to read it:** ✅ rows are the deterministic engine's job — build them well and test them hard.
🟡/🔴 rows are the LLM's job via the §10 router. The document's spine *is* the build plan: solve R1→R5
deterministically, instrument confidence signals, and attach the twelve hooks at the marked seams.

---

## 13. Open questions & things to validate before relying on this

`[OUR-ANALYSIS]` Honest edges — resolve these with our own PBIR/DAX testing, not by trusting prior art:

1. **PBIR schema currency.** Target visualType tokens and `objects`/`vcObjects` paths must be
   re-checked against the *current* PBIR schema our skill emits (the formatting reference is the
   authority; keep them in sync). Custom-visual GUIDs especially rot.
2. **Custom-visual native fallbacks.** For each 🔴 custom chart, empirically pick our default native
   fallback (e.g. Sankey→? by level count) and add a golden test — don't inherit an assumption.
3. **Table-calc compute-using fidelity.** Build a small battery of Tableau table calcs with known
   partition/addressing and diff the DAX numerically. This is the least-trustworthy translation tier.
4. **Field parameter vs. calc group thresholds.** Define when a Tableau parameter *should* become each
   PBI object; encode as the router's decision inputs for hook #10.
5. **Joint-view "sensibility" check.** Prototype the §2/§10.2 `LOW-FIDELITY-JOINT` detector (empty
   visual / role-type mismatch) — measure how many real defects it catches vs. false positives.
6. **Theme-synthesis ROI.** Validate hook #7 on a real multi-visual workbook: does one synthesized
   theme + few overrides beat per-visual replication on both fidelity and file cleanliness?

---

### Provenance footer

Study derived from *facts and method* observed in the `cyphou/Tableau-To-PowerBI` prior art
(descriptive docs: architecture, mapping/coverage references, gap analysis, known limitations,
changelog) plus independent validation against Power BI / DAX / PBIR semantics and this repo's
[`powerbi-formatting-color-reference.md`](./powerbi-formatting-color-reference.md). **No third-party
source code, mapping tables, regexes, fixtures, or file structure were copied**; equivalences were
re-derived and re-organized, and every correctness rating, critique, and LLM recommendation is our
own. `[PRIOR-ART]` tags mark ideas whose *existence* was prompted by the prior art; `[OUR-ANALYSIS]`
marks our independent contribution. Per [`CLEANROOM.md`](../../CLEANROOM.md), treat this as a
grounding reference, not shipped code.
