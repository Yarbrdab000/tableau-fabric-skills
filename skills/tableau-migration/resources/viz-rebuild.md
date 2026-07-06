# Visual wireframe rebuild (`.twb` → PBIR)

`scripts/twb_to_pbir.py` is a clean-room, stdlib-only converter that reads a Tableau
workbook (`.twb`, UTF-8 BOM XML) and emits a **PBIR** (Power BI Enhanced Report) wireframe
that binds to the semantic model produced by the rest of this skill. It is deliberately a
**small, correct slice**: a handful of chart types are rebuilt faithfully and *everything
else is reported as a structured warning* — never a silently wrong visual.

It is built only from primary sources: the Tableau workbook XML grammar (worksheets,
`<datasources>`, `<mark class>`, rows/cols shelves, encodings, filters) and Microsoft's
public PBIR JSON schemas.

## Clean-room methodology

This module was written independently from primary specifications — the Tableau `.twb` XML
grammar (Tableau docs) for the input and Microsoft's PBIR / `.pbip` JSON schema for the output.
Reference implementations were consulted **only to fact-check discrete mappings** (e.g. a mark
class → a PBIR `visualType`, a data-role name); no expression, file/function structure, naming,
comments, or test data were copied. Third-party attribution lives in the repository-root
`THIRD_PARTY_NOTICES`.

## Pipeline

```
.twb XML ──parse_twb──▶ normalized IR (plain dicts) ──emit_pbir──▶ { path: text } PBIR parts
```

- `parse_twb(xml_text) -> {"worksheets": [...], "dashboards": [...], "warnings": [...]}`
  Accepts `str` or `bytes`; BOM is stripped / decoded with `utf-8-sig`.
- `emit_pbir(ir, *, dataset_name, report_name, model_table=None, field_map=None) -> {path: text}`
- `migrate_twb_to_pbir(xml_text, ...) -> {"ir", "parts", "warnings"}` (convenience wrapper).

## Command line (live validation)

The module is also runnable, so a real exported workbook can be converted and the resulting
`<report>.Report` folder opened in Power BI Desktop or deployed to a Fabric workspace. It is
purely local — it reads a `.twb` file (or stdin) and writes JSON files; **no network, no
credentials, no secrets**, and every target name comes from an argument / environment
variable, never the code:

```
py twb_to_pbir.py <input.twb> -o <out-dir> --dataset "Superstore" --report "Superstore Report"
py twb_to_pbir.py - --dataset Superstore        # read XML from stdin, print a JSON manifest
```

- `-o/--out` writes the parts under `<out-dir>/<report>.Report/…`; without it a JSON manifest
  (part paths + warnings) is printed to stdout for a no-write dry run.
- Defaults also read `TWB_PBIR_OUT` / `TWB_PBIR_DATASET` / `TWB_PBIR_REPORT` /
  `TWB_PBIR_MODEL_TABLE` from the environment.
- Warnings are printed to stderr (when writing) or included in the dry-run manifest.

The committed pytest suite stays fully offline/deterministic (synthetic `.twb` string
fixtures, no disk, no network); the live open/deploy is a separate manual pass.

## Supported visual types

| Tableau mark + shelf layout                         | IR `visual_type` | PBIR `visualType`      | Data roles            |
| --------------------------------------------------- | ---------------- | ---------------------- | --------------------- |
| Bar, dimension on **columns**, measure on **rows**  | `column`         | `clusteredColumnChart` (`stackedColumnChart` with a colour-legend dim) | Category / Y / Series |
| Bar, dimension on **rows**, measure on **columns**  | `bar`            | `clusteredBarChart` (`stackedBarChart` with a colour-legend dim) | Category / Y / Series |
| Line (needs ≥1 measure)                             | `line`           | `lineChart`            | Category / Y / Series / Small multiples |
| Area (needs ≥1 measure)                             | `area`           | `areaChart`            | Category / Y / Series / Small multiples |
| Dual-axis combo: a column-family measure + a line-family measure on one shelf | `combo` | `lineClusteredColumnComboChart` | Category / Y (columns) / Y2 (lines) / Series |
| Text, dimensions on **one** axis                    | `table`          | `tableEx`              | Values                |
| Text, dimensions on **both** axes                   | `matrix`         | `pivotTable`           | Rows / Columns / Values |
| Square (highlight table), dimensions on **both** axes + measure on colour/label | `matrix` | `pivotTable` | Rows / Columns / Values |
| Pie, legend dimension + angle measure               | `pie`            | `pieChart`             | Category / Y          |
| Circle/square/shape/point, measure on **both** axes + a disaggregating dimension | `scatter` | `scatterChart` | X / Y / Category / Series / Size |
| Geo-role dimension on **detail** + measure (area/`Map`/`Automatic`+spatial signal) | `filled_map` | `filledMap` | Location / Color |
| Geo-role dimension on **detail** + measure, **point** mark (`Circle`/…+spatial signal) | `map` | `map` | Location / Size / Color |
| Measure(s) with **no** dimension anywhere (one)     | `card`           | `card`                 | Values                |
| Measure(s) with **no** dimension anywhere (≥2)      | `card`           | `multiRowCard`         | Values                |
| Categorical / date / numeric **filter**             | (slicer)         | `slicer`               | Values                |

`Automatic` marks are inferred from the shelves (dim+measure → column; dims only → table or
matrix; two measures + a dimension → scatter). A `color` encoding on a dimension populates the
**Series** role; a `detail`/level-of-detail dimension disaggregates a scatter (**Category**); a
measure on the `label`/`text` or `size` encoding with empty shelves drives a **card**.

An `Automatic` mark over a **continuous (green) date** axis is Tableau's default **line** chart, so
a continuous date + a measure rebuilds as a `lineChart` rather than a column chart. A continuous
date is a date *truncation* (serialised with a `*-Trunc` derivation, e.g. `Day-Trunc` / `Month-Trunc`,
pill prefixes `tdy:` / `tmn:`); a **discrete** date PART (`Year` / `Month`, the `yr:` / `mn:` pills)
is not continuous and keeps the column/bar default. The field bindings are identical to a line over
the same shelves — only the chart *type* changes. This applies only to the `Automatic` mark: an
explicit `Bar` mark means the author chose bars and stays a column chart even over a continuous date.

A colour-legend **dimension** on a bar/column mark renders as a **stacked** chart
(`stackedColumnChart`/`stackedBarChart`) rather than a clustered one, because Tableau stacks marks
by default when a discrete colour pill is present. Bars without a colour-legend dimension keep the
clustered variant. (If a workbook explicitly turned off *Stack marks*, review the subtype after
import — that non-default setting is not read from the shelves.)

A Tableau **dual axis** that overlays two measures with **different mark families** — a
bar/column measure and a line/area measure — rebuilds as a **combo** chart
(`lineClusteredColumnComboChart`): the column-family measure(s) bind to `Y` (primary axis) and the
line-family measure(s) to `Y2` (secondary axis), against the shared category. Each measure's mark
is read from its own dual-axis pane (`y-axis-name` names the measure; `y-index` marks the secondary
axis). This is deliberately conservative — a dual axis whose two measures share the **same** mark
(e.g. bar + bar) stays an ordinary multi-measure clustered/line chart, and an *area* measure is
treated as line-family (consistent with the area→line default). Only a genuine column + line split
emits the combo, so it never mis-fires on same-mark layouts.

A Tableau **highlight table** — the `Square` mark with dimensions on both axes and the measure on
the **colour** (saturation) encoding — rebuilds as a **matrix** (`pivotTable`): the row/column
dimensions map to `Rows`/`Columns` and the colour measure becomes a `Values` field (a single-axis
highlight table degrades to a `tableEx`). The colour saturation itself is Tier-2 styling, so the
matrix shows the numbers without the heat shading. `Square` marks with **no** axis dimensions
(treemap / packed-bubble / heatmap layouts) are deferred to a warning rather than guessed.

### Sort order

Tableau records an axis sort as `<computed-sort column='[dim]' direction='ASC|DESC' using='[measure]' />`
on the worksheet view. When the sort-by `using` measure is **already bound** in the rebuilt visual,
the engine emits a Power BI `visual.query.sortDefinition` (a sibling of `queryState`) — an ordered
`sort` array of `{field, direction}` with direction `Ascending`/`Descending`, where `field` reuses
the exact same expression as the bound projection (so the sort can never become a dangling
reference). If the `using` measure is **not** bound anywhere in the visual, the sort is dropped
rather than guessed (warn-never-wrong: the visual still renders in faithful default order). A
`<manual-sort>` (an explicit, frozen list of members) has no faithful Power BI sort expression and
is likewise left to the model's default order.

### Worksheet titles (structural text only)

Tableau authors an explicit worksheet title at `worksheet/layout-options/title/formatted-text/run`
(one or more `<run>` segments). When a worksheet carries a non-empty **static** title — including a
title that simply repeats the sheet name — the engine concatenates the run text and emits it as the
Power BI visual's `visualContainerObjects.title` (a single-quoted semantic-query string literal with
`show: true`), and suppresses Power BI's auto field-name subtitle (`subTitle.show: false`) so the
rebuilt visual shows exactly the author's caption and nothing more. This is Tier-1 **text** only —
the per-run styling (bold, font size/colour/name, alignment) is deliberately *not* reproduced; title
formatting is a Tier-2 concern. A **dynamic** title (one that embeds a field or parameter reference,
authored as an escaped `<…>` token between runs) has no faithful static-text equivalent, so it is
deferred with a `… dynamic title …` warning and the rebuilt visual keeps its default title rather
than rendering a broken literal (warn-never-wrong). An unsupported worksheet emits no visual, so its
title is simply dropped (there is nothing to title); slicers built from filters/parameters are not
titled.

### Axis titles (structural axis labels)

Tableau records an author-overridden axis title at
`worksheet/table/style/style-rule[@element='axis']/format[@attr='title']`, where `scope` is `rows`
or `cols` (which shelf's axis) and `value` is the title text — an **empty** `value` meaning the
author hid that axis title. (Quick-filter caption rules live under
`style-rule[@element='quick-filter']` and carry no `scope`, so they are excluded.) For the cartesian
chart types that have a category-vs-value axis pair (`column`, `bar`, `line`, `area`), the engine
reproduces the override on the rebuilt visual's data-plane `visual.objects`: a custom caption →
`categoryAxis`/`valueAxis` `titleText` (a single-quoted semantic-query string literal) with
`showAxisTitle: true`; a blanked title → `showAxisTitle: false` only. The whole-axis `show` toggle is
never touched (hiding the *title* must not hide the axis labels/gridlines). The scope is mapped to a
Power BI axis **by the role of the field(s) on that shelf** — a shelf holding only the dimension
drives `categoryAxis`, a shelf holding only the measure drives `valueAxis` — so the mapping is
orientation-independent (it is correct whether the dimension sits on rows for a bar or on cols for a
column/line/area). A shelf with a mixed or empty role is skipped, and non-cartesian visuals
(matrix/pie/scatter/maps) ignore axis titles entirely (warn-never-wrong: never guess which axis a
title belongs to). Title font/colour/size styling is a Tier-2 concern and is not reproduced.

### Scatter / card / pie role mapping

- **Scatter** (`scatterChart`): the measure on **columns** → `X`, the measure on **rows** → `Y`,
  the disaggregating dimension (`detail` or an axis dim) → `Category`, a `color` dimension →
  `Series`, a `size` measure → `Size`. Two measures with *no* dimension fall back to a card
  (orientation/series would be a guess), per the small-correct-slice rule.
- **Card / KPI** (`card` for a single value, `multiRowCard` for several): a measure on the marks
  shelf (rows/cols) or on the `label`/`size` encoding with no dimension. A bare big-number KPI
  tile (e.g. `SUM(Sales)` on the Text encoding) is detected this way; the dedicated PBIR `kpi`
  visual (with target/trend `Indicator` roles) is left to a later pass since `.twb` carries no
  target/trend metadata.
- **Pie** (`pieChart`): the legend dimension (axis dim or `color`) → `Category`, the angle/size
  measure → `Y`.
- **Dot / strip plot** (`Circle` / `Shape` / `Point` mark): when the layout is exactly one
  category axis vs one measure axis (a strip plot), it routes to a `column`/`bar` — the field
  binding is identical and only the dot glyph differs (Tier-2 styling, cf. an `Area` mark, whose
  fill over a `lineChart` shape is the deferred Tier-2 part of its dedicated `areaChart`). The
  guard is strict: with a second axis dimension (a complex circle crosstab) or no axes at all
  (packed bubble), nothing is guessed — the worksheet stays unsupported and warns. (Two measures
  on the axes + a dimension still route to `scatter` as above.)
- **Single-dimension text list** (`Automatic` / `Text` mark): a lone *categorical* field carried
  only on the marks card (`label`, `colour`, or `detail`) with **no measure anywhere and no axis
  pills** is Tableau's text rendering of that field's distinct values, so it routes to a
  one-column `tableEx` listing that field. The same field dropped on both `colour` and `label`
  is deduped to a single column. A **geographic** dimension is excluded (that is a map, deferred
  to map routing) so a location field is never flattened into a plain list; a measure anywhere
  (`colour`/`detail`/`size`/`label` value, or an axis measure) likewise disqualifies it (that is a
  packed-bubble / KPI / chart layout, handled by those rules instead).

### Geographic maps (basics: filled + symbol)

Tableau tags a geographic field with a `semantic-role` on its column metadata
(`semantic-role='[State].[Name]'`, `[City].[Name]`, `[Country].[ISO3166_2]`, `[ZipCode].[Name]`,
…) and auto-generates `Latitude (generated)` / `Longitude (generated)` helper fields. A map is
recognized from the **geo-role signal**, *not* the generated lat/lon:

- **Trigger:** a geo-role **dimension sits on the `detail` (level-of-detail) encoding** *and* a
  measure is available. A geo dimension on an **axis** stays an ordinary bar/line/table chart, so
  the many Superstore charts that put State/Region/City on an axis are never hijacked.
- **Spatial signal (anti-hijack):** for an ambiguous mark (`Automatic`/empty, or a point mark such
  as `Circle`/`Square`/`Shape`/`Point`) the geo-on-detail trigger additionally requires a spatial
  signal — `Latitude (generated)` **and** `Longitude (generated)` on the axes, or a `<geometry>`
  encoding. Explicit `Map`/`Filled` marks are self-signaling and need no extra signal.
- **Filled map** (`filledMap`, choropleth): an area/`Map`/qualifying `Automatic` mark → the geo
  dimension → `Location`, the (color-preferred) measure → `Color` saturation.
- **Symbol / bubble map** (`map`): a point mark → the geo dimension → `Location`, the
  (size-preferred) measure → `Size`, a distinct color measure → `Color`.

The generated `Latitude`/`Longitude`/`Geometry` helper fields are dropped quietly (they only act
as the spatial signal); the geo dimension binds like any column — `Location` = entity
`<relation>` / property `clean_col(<remote-name>)`.

### Measure Values / Measure Names (N measures in one well)

Tableau's `Measure Values` shelf packs several measures into a single value well and uses the
companion `Measure Names` pill to label/series/split them. Power BI has **no** `Measure Names`
field — dropping the member measures into one value well *auto-produces* the series, column
headers, or card rows. So the rebuild **expands** `[Measure Values]` to its ordered member
measures (each exact-bound through the normal field resolver) and treats `[Measure Names]` as
**implicit**: it is never bound (binding it would be a dangling reference to a column that does
not exist). When a worksheet uses the Measure Values shelf, a `fidelity_note` records the
expansion and the worksheet does **not** emit a false "no model binding" warning for the handled
pseudo-fields.

The ordered member list comes from the worksheet's categorical filter on `[:Measure Names]`
(its `member` entries are in document = shelf order); a `<manual-sort>` dictionary is the
fallback when no such filter is present. Routing by mark + where the (implicit) `Measure Names`
pill sits:

| Tableau pattern                                                        | Result                                  |
| ---------------------------------------------------------------------- | --------------------------------------- |
| `Measure Names` on **Color** (bar/line/automatic)                      | measures as the **Series** (`column`/`bar`/`line`) |
| Measures as columns in a text **crosstab** (names on rows/cols, values on text) | `matrix` (native measures-as-columns) |
| Multiple measures with **no** dimension                                | `card` / `multiRowCard`                 |
| **Path-mark hack** (Line mark + `Measure Names` on **Path**, usually padded by a dummy `0` constant member) | reduced to a faithful `bar`/`column` of the real measure(s); the numeric-literal spacer is dropped |

Two sub-cases are deliberately **deferred** (warn, never a wrong visual):

- **`Measure Names` on Rows/Columns against a chart mark** splits the chart into one pane per
  measure — that is *small multiples* (trellis), handled by a later pass — so it degrades to a
  warning rather than being silently flattened into one chart.
- **Parameter-driven swap members** (a `CASE`/`IF` over `[Parameters]`) are a field-parameter
  pattern; a faithful field-parameter rebuild is deferred and the worksheet warns.

### Table/matrix background colour scale (conditional formatting)

A continuous colour scale on a highlight table / matrix (the mark colour encoding's
`color-palette` with `type="ordered-sequential"` or `"ordered-diverging"`) becomes a Power BI
**cell background FillRule gradient** on `visual.objects.values[0].backColor`. A sequential scale
emits a two-stop `linearGradient2` (min/max); a diverging scale with a centre emits a three-stop
`linearGradient3` whose `mid.value` pins the centre (e.g. `0.0D`). Colours are single-quoted
literals, nulls colour `asZero`, and the `selector.metadata` targets the **displayed** value
column — so "colour the cells by a *different* measure than the one shown" is preserved (the
gradient `Input` is the colour driver, the selector targets the shown column).

**Warn-never-wrong.** The fill is emitted **only** when the colour driver is a clean value-kind
model measure that is already projected in the visual (matched by exact field expression so the
gradient reuses the visual's own `queryRef`). If the driver is a **quick table calc** (running
total, percent-difference, moving average, rank, …) or otherwise has no faithful model measure,
the rebuild emits the visual **without** a fill, raises a `background colour scale deferred …`
warning, and preserves the raw palette (colours + centre) on the candidate record's
`conditional_format` fact (`status: "deferred"`) for a later binding pass. This is intentional:
colouring by a mis-resolved base field would be confidently wrong. Only the background colour
scale is handled here — font colour, data bars, icons, and gradient palette styling remain Tier-2.

### Categorical mark colours (explicit author member → hex palette)

When the author has explicitly assigned a colour to each member of a colour-legend **dimension**
(the mark colour encoding carries `<map to="#hex"><bucket>"Member"</bucket></map>` entries rather
than a continuous `<color-palette>`), that map is reproduced as per-member data colours on
`visual.objects.dataPoint`. Each entry is a `fill` (a single-quoted hex literal) targeted by a
**scope-identity selector** — `selector.data[0].scopeId.Comparison` with `ComparisonKind: 0`
(Equal), `Left` = the coloured column's exact projected expression, and `Right` = the member value
literal. Tableau author order is preserved, and unmapped members keep their Power BI theme colour.
A bare single `mark-color` (Tableau writes one even when the author chose nothing) is **not**
reproduced — only an explicit member map is treated as author intent.

**Warn-never-wrong.** Per-member fills are emitted **only** on the discrete categorical chart types
where they render safely (`column`, `bar`, `pie`, `donut`) **and** when the coloured dimension is
actually projected in that visual (so the selector's column resolves). On any other visual type
(notably `line` / `area`, where an explicit `dataPoint` override can drop the series) or when the
coloured dimension is not bound, the visual emits with **theme** colours, a
`categorical mark colours deferred …` warning names the reason, and the raw palette is preserved on
the candidate record's `mark_colors` fact (`status: "deferred"`) for a later pass. The per-member
scope-identity selector shape is grounded in the Power BI report formatting reference (the
convention/grounding model); the mapping is original work (see *Clean-room methodology*).

### Data labels (Tableau "Show Mark Labels")

Tableau records the mark-label show/hide toggle as `<format attr="mark-labels-show" value="true|false"/>`
inside a `<style-rule element="mark">` — at the worksheet `table/style` level and/or per `pane` (a
dual-axis worksheet carries one per pane). That toggle is reproduced on the PBIR data-plane
`visual.objects.labels` `show` property, applied uniformly (the formatting reference lists `labels`
as a visual-wide object — no selector).

**Warn-never-wrong.** `show: true` is emitted whenever the toggle is unambiguously ON (every captured
pane agrees) — the high-value case that restores the numbers a Tableau view displayed. `show: false`
is emitted **only** for the `pie` / `donut` family, whose Power BI default is *on*, so that an
author who hid labels stays faithful; every other supported chart type already defaults labels *off*,
so an OFF toggle is a no-op (the fact is still recorded, `status: "default_off"`). A table / matrix /
card / map already displays its values, so no label object is produced there. When a dual-axis
worksheet's panes **disagree** (a per-series label difference), no global toggle is guessed — the
visual keeps its default label visibility, a `data labels deferred …` warning discloses it, and the
raw values are preserved on the candidate record's `data_labels` fact (`status: "deferred"`). Only
show/hide is set; label detail (culling, which value, placement) stays Tier-2.

### Legend (show/hide + position)

Whether a worksheet's colour legend is **shown on a dashboard** is a dashboard-scoped fact: Tableau
writes a `<zone type='color' name='<worksheet>'>` (with `x/y/w/h` in the dashboard's 0–100000
coordinate space) only when the author placed that colour legend on the dashboard. A worksheet's own
`<cards>` always carry a `<card type='color'>` when colour is used, but that card is *not* the
placement signal — only the dashboard `<zone>` is. `_parse_dashboard` captures those colour zones
additively (`legend_zones`); the standalone (non-dashboard) page path is untouched, because a
worksheet rendered on its own always shows its legend, which is also Power BI's default.

The legend show/position is reproduced on the PBIR data-plane `visual.objects.legend` — `show` (a
quoted-string boolean) and `position` (a single-quoted enum: `'Right'`/`'Left'`/`'Top'`/`'Bottom'`),
applied uniformly (the formatting reference lists `legend` as a visual-wide object — no selector).

**Warn-never-wrong.** A legend object is considered **only** for cartesian/part-to-whole types that
carry a categorical colour **series** (`column`/`bar`/`line`/`area`/`pie`/`donut`/`scatter`/`combo`/
`ribbon` with a `category` colour encoding); a continuous colour ramp, or a matrix / table / card /
map, produces no legend object (its colour legend is a Tier-2 gradient concern). Within that set:
(1) a **present** colour zone whose geometry clears exactly one side of its worksheet zone (5%
tolerance) emits `show: true` + that `position` (`status: "emitted"`); (2) a **present** zone whose
geometry is ambiguous (overlap / corner — zero or two-plus sides qualify) emits **no** object and a
`legend position deferred` note (`status: "position_deferred"`), leaving Power BI's default position
rather than guessing; (3) a categorical-colour worksheet **placed on a dashboard with no colour zone
for it** means the author did not show that legend on the dashboard, so `show: false` is emitted
(`status: "hidden"`) to match what the dashboard renders. Side geometry is read from the **raw**
(pre-scale) zone coordinates so the worksheet and its legend share one space. Each decision is
recorded on the candidate record's additive `legend` fact. Only show/position is set; legend
title, font, and swatch styling stay Tier-2.

### Title font styling (uniform size / colour / weight / family)

A worksheet's static title (the structural text captured per [Worksheet titles](#worksheet-titles-structural-text-only))
may carry per-run font styling on its `<run>` elements (`fontsize`, `fontcolor`, `bold`, `fontname`,
`fontalignment`, …). `_parse_title_style` reads those attributes and contributes the **font** of the
visual's container title — emitted into the same `visualContainerObjects.title` properties block that
already carries `show`/`text`. The schema-grounded container-title font properties reproduced are
`fontSize` (a numeric `"Nd"` literal — points pass through unchanged, the same unit Tableau uses),
`fontColor` (a solid single-quoted `#rrggbb` literal), `bold` (a quoted-boolean weight), and
`fontFamily` (a single-quoted real font face). Shapes verified against the Microsoft PBIR visual-title
reference (`visualContainerObjects.title` with `fontSize`/`fontColor`/`bold`/`fontFamily`).

**Warn-never-wrong.** Power BI applies **one** font to the whole title, but a Tableau title is rich
text (multiple independently-styled runs). A font property is therefore emitted **only when every
text-bearing run agrees**; a title whose runs disagree — or where some runs omit the property (so an
inherited default would apply) — defers that property and keeps the structural title text. Specifically:
a `fontcolor` that is not a clean 6-hex `#rrggbb` (e.g. an 8-hex alpha colour) is deferred; `bold` is
emitted only when **every** text run is bold (mixed weight defers); `fontFamily` is emitted only for a
uniform **real** font — Tableau's internal `Tableau Bold` / `Tableau Semibold` / `Tableau Book` faces
have no Power BI equivalent, so they defer rather than emit an unresolvable face. `italic` / `underline`
(unconfirmed container-title props) and paragraph **alignment** (an unconfirmed alignment enum — a wrong
guess would visibly mis-align the title) are **always** deferred. Every deferred property is recorded on
the additive `title_style` candidate-record fact (`{font_size?, font_color?, bold?, font_family?,
deferred: [...]}`) for a future Tier-2 pass — never emitted. A dynamic title is already deferred
wholesale (no static text), so it carries no `title_style`.

**Axis-label font styling** has **zero** signal across the corpus (the only axis format Tableau writes is
`tick-color`, a tick-mark/chrome colour — mostly transparent — not a label/title font), so it is not
built. An axis label/title font override, if one ever appears, is simply left at the model/theme default
(warn-never-wrong).

## Binding contract (matches the v1 model exactly)

The `.twb` embeds the full datasource (`<relation>` + `<metadata-records>`), so bindings are
resolved from the workbook itself rather than guessed from captions:

- **Table** (`Entity` / `SourceRef.Entity`) = the Tableau `<relation name=...>`.
- **Column** (`Property`) = `clean_col(<remote-name>)` — the *source* column name run through
  the same `clean_col` imported from `tmdl_generate`, so names match the generated model even
  when the workbook renames the field's caption.
- **Measure** = the calculated field's caption, in the `_Measures` table.

Fields are matched by their internal id (e.g. `[Sales]`), so a workbook-side caption rename
still binds to the right model column. Callers can override binding precisely with a
`field_map` `{caption: {"entity", "property", "binding"}}`, or pin every column to one table
with `model_table=`.

### Tableau internal / auto-generated pseudo-fields (silenced)

Tableau injects helper fields the author never created and that have **no user model binding**.
They surface as worksheet shelf / filter refs, so they must be recognised and dropped *silently*
— warning on them is false noise, not a real coverage gap. Two authoritative signals are used
(not fragile caption matching):

- **`__tableau_internal_object_id__`** — Tableau's object-model row-count internal (a reserved
  double-underscore namespace, never a user field); matched anywhere in the field id.
- **`user:auto-column` declarations** — dashboard filter/set **action** groups
  (`user:auto-column='sheet_link'`), viz-in-tooltip and forecast helpers. Their ids are collected
  from the datasource once (language-independent) and dropped on resolve.

The silencing is **targeted**: a genuine (non-internal) field that cannot be resolved still emits
the `could not resolve field '<id>' (skipped)` warning, so the noise fix never masks a real
missing binding.

### Field expressions (semantic query)

- Column: `{"Column": {"Expression": {"SourceRef": {"Entity": T}}, "Property": C}}`
- Measure: `{"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}}, "Property": M}}`
- Aggregation wraps a Column with a function code:
  `Sum=0, Avg=1, DistinctCount=2, Min=3, Max=4, Count=5, Median=6`.

## PBIR output layout

One `.Report` folder per workbook, paths relative:

```
definition.pbir                                   (datasetReference byPath ../<dataset>.SemanticModel)
definition/version.json                           (versionMetadata 1.0.0)
definition/report.json                            (report 1.0.0)
definition/pages/pages.json                       (pagesMetadata 1.0.0: pageOrder, activePageName)
definition/pages/<page>/page.json                 (page 1.0.0)
definition/pages/<page>/visuals/<v>/visual.json   (visualContainer 1.0.0)
.platform
```

- **One page per dashboard.** Dashboard zones whose name matches a worksheet become visuals;
  zone `x/y/w/h` (Tableau internal coordinate units) are scaled into the `1280×720` page.
- A worksheet **not** placed on any dashboard gets its own page (one visual filling the page).
- Object names are sanitized to word-chars/hyphen with a short hash suffix for uniqueness, and
  each visual's `queryRef`s are de-duplicated.

## Openable workbook project (estate orchestrator)

On its own, `emit_pbir` writes only an **unbound** `.Report` folder. The estate orchestrator
(`scripts/migrate_estate.py`) wraps these parts into a **self-contained, openable** `.pbip` per
workbook:

1. It enumerates the workbook's embedded datasources (`list_workbook_datasources`) and picks the
   **primary** (the one the most worksheets bind to), rebuilding it into a semantic model with the
   same datasource pipeline used for published `.tds` files — so calculated fields are auto-extracted
   and role-split exactly as on the direct path (not silently dropped).
2. It rewrites the report's `definition.pbir` `datasetReference.byPath` to point at that model as a
   **sibling** (`../<Datasource>.SemanticModel`) and writes `pbip/<Workbook>/` containing the model,
   the rebuilt `<Workbook>.Report`, and the `<Workbook>.pbip` pointer (named after the workbook via
   `write_local_pbip(project_name=...)`) — double-click to open in Power BI Desktop, no external
   model needed.

Because the report binds to a model rebuilt from the workbook's **own** embedded datasource, every
`Entity`/`Property` in the wireframe resolves to a real table/column in the bundled model. Per-visual
status is reported as `viz_fidelity` (`rebuilt` / `warned`); anything that can't be bound faithfully
(a datasource that routes to the lakehouse fallback, secondary datasources a single PBIR report can't
bind, a missing `definition.pbir`) is recorded in `pbip_warnings` and the `.pbip` is skipped rather
than mis-bound. See [migration-report.md](migration-report.md) for the exact report keys.

## Visual Calculations (view-only quick table calcs)

A Tableau **quick table calc** applied on a pill via the pill menu (Running Total, YTD, YTD Growth,
Moving Average, Percentile, Compound Growth, Percent Difference, Percent of Total, Year-over-Year,
Difference) is a **report/view-layer** transform: it re-shapes the worksheet's own result matrix
along an axis with a restart scope, and has **no model equivalent**. The measure pipeline
deliberately hands these off, so historically the base aggregate survived on the value shelf but the
transform was emitted as neither a measure nor a calc, and the visual was judged incomplete and
skipped. Power BI **Visual Calculations** are the structurally identical home — also stored in the
visual (`queryState` projections carrying a `NativeVisualCalculation` DAX field) and evaluated over
the visual's own matrix along `ROWS`/`COLUMNS` with a partition/reset scope — so a view-only quick
calc rebuilds as a view-only Visual Calculation: same layer in, same layer out, no model measure.

**Precedence (never a double-emit).** Three paths coexist with strict ordering: the datasource
model and the **model-level** table-calc measure engine (named calc-field table calcs →
`WINDOW`/`OFFSET`/`ROWNUMBER`/`RANKX` measures) are first-class and untouched. The Visual-Calculation
path fires **only** when a pill is a quick table calc *and* the measure path did not bind a measure
for it (`measure_rebound`). When it doesn't fire, output is byte-identical.

**Compiler, not pattern-matcher.** `workbook_table_calcs.extract_table_calc_usages` recovers each
usage's addressing facts (including the `level-break`/`level-address`/`diff-options` reset-and-grain
facts and any stacked secondary pass); `visual_calc_spec` normalizes them into a small view-layer IR
(family + axis + reset + offset + scope + role + chain); `visual_calc_emitter` renders that IR into
faithful DAX (`RUNNINGSUM`, `MOVINGAVERAGE`, `RANK`, `PREVIOUS`, `FIRST`, `ROWNUMBER`, `COLLAPSE`/
`COLLAPSEALL`). The axis is derived from the **view** — the shelf carrying the ordering/date
dimension — not the raw ordering token (so the corpus' "computed Down" twin, whose token is
`Columns`, correctly flips to axis `ROWS`). An above-leaf offset is a resolved calendar ratio
(Year-over-Quarter = 4 periods). Any calc whose axis, calendar ratio, or chain shape cannot be
pinned from the workbook facts routes to **review** with a reason rather than a guess.

**One shared addressing decomposition.** `visual_calc_spec.resolve_addressing(usage)` is the single
view-layer superset every Visual-Calculation consumer reads from. It splits the visual's dimension
pills into **addressed** (the calc runs/sums/offsets along these) and **partition** (it restarts/
subtotals within these): `Rows` → addressed on the Cols shelf, partition on Rows; `Columns` →
addressed on Rows, partition on Cols (the token a model measure hands off, but a matrix-relative
Visual Calculation can resolve); `Table` → all addressed, no partition; `Field` → the ordering
fields addressed, the rest partition. A compound/pane token that the shelves alone don't disambiguate
fails closed to a review reason. `partition ≠ ∅` → `COLLAPSE` (a partition subtotal); `partition = ∅`
→ `COLLAPSEALL` (the grand total). The dim-vs-measure classification comes from the shared
`workbook_table_calcs.AGG_DERIVATIONS` set / `Pill.is_dimension`, so both paths agree on the edges
(`Cntd`/`Attr`/`Stdev`/a `User` LOD reference are not partition dimensions).

**Matrix vs cartesian chart.** A **matrix** carries its base measure on the `Values` shelf and its
dimensions on `Rows`/`Columns`, so the axis is derived from those shelves as above. A **cartesian
chart** (bar / column / line / area) carries its base measure on the `Y` role and its dimensions on a
single **Category** axis, which is the "rows" of the chart's result matrix — so any chart Visual
Calculation runs along `ROWS` regardless of the Tableau ordering token (a structural fact of chart
geometry). When a chart percent-of-total collapses to a partition subtotal (`COLLAPSE`, not
`COLLAPSEALL`), `COLLAPSE(m, ROWS)` removes the **innermost** Category level, so the Category
projections are re-nested **partition-outer / addressed-inner** (via a side-effect-free
projection-count split over the same `resolve_addressing`, never fragile pill↔name matching) so the
collapse lands on the addressed dimension. A chart has no colour-role conditional-format concept, so
its calc is always the shown value and it carries no `backColor` fill.

**Roles + visibility.** A *plain* table/chart hides the base measure and shows the calc (role
`value`); a *conditionally-formatted* table keeps the base measure visible and hides the calc, which
only drives the `backColor` heat scale (role `color`, detected from the marks colour encoding). The
`backColor` FillRule's `Input` is the outer Visual Calculation's `queryRef`; `selector.metadata`
binds the fill to whichever measure column is actually shown (a fill anchored to a hidden column
paints nothing) — the base cell for a colour-role table, the calc cell for a value-role one. A
stacked secondary calc (e.g. YTD → Year-over-Year growth) emits as a two-pass chain with the inner
calc always hidden. `report.json` / `summary.md` gain an additive `visual_calculations` routing
rollup (emitted / review, by role and calc family).

**Number format.** A *visible* percent-family calc (Percent of Total, Percent Difference,
Year-over-Year, YTD Growth, Compound Growth, Percentile) also carries a Power BI `format` string
(`0.00%`) on its projection's schema-verified `format` property (PBIR `RoleProjection`), so it
renders as a percentage rather than a bare ratio. A hidden colour-driver calc and the absolute-valued
families (Running Total, Moving Average, Difference, YTD) are left at the column default — matching
the hand-built oracle, which formats only the shown percent.

## Unsupported handling (→ `warnings`, never a wrong visual)

Every warning is `{"scope": "worksheet"|"dashboard", "name": <name>, "reason": "manual attention required: ..."}`.
Cases that degrade to a warning instead of a visual/binding:

- **Unsupported marks**: area, polygon, density/heatmap, Gantt (non-bar), etc. → the worksheet
  emits **no** visual.
- **Spatial / custom-geometry maps are deferred** (basics only — filled + symbol map are
  supported, see above). A worksheet degrades to a warning when it needs custom geometry rather
  than a plain geo-role binding: `Multipolygon`/custom spatial polygons, `MAKEPOINT`/`MAKELINE`/
  `BUFFER` constructed geometry, density/heatmap layers, and dual-axis (layered) maps. The real
  Superstore "Sale Map" (a `Multipolygon` mark) defers this way rather than being rebuilt wrong.
- **KPI target/trend**: a single measure with no dimension becomes a `card`/`multiRowCard`; the
  richer PBIR `kpi` visual (with `Indicator`/`TrendAxis`/`TargetValue` roles) is deferred to a
  Tier-2 analytics pass. When the worksheet carries an explicit **reference / target / trend line**
  (a Tableau `<reference-line>`/`<trend-line>` annotation — e.g. a sales goal, an average band, a
  fitted trend), a **constant line** (`formula='constant'` with a fixed numeric `value`) on a
  **value-axis cartesian chart** (`column` / `line` / `area`, where the measure is unambiguously the
  Y axis) is now **rebuilt** as a Power BI analytics reference line: a `y1AxisReferenceLine` object
  (`{properties:{show, value, [displayName]}, selector:{id}}`, the `value` a `D`-suffixed double
  literal, an author-typed custom label a single-quoted string) is merged onto `visual.objects` so the
  goal line renders on the canvas. The emittable constants are recorded on the worksheet IR
  (`reference_line_constants`) alongside the unchanged `reference_lines` descriptors. Every **other**
  annotation is still **detected and disclosed, never guessed** — a computed line
  (average/median/min/max/total), a parameter-driven line, a percentage-band distribution, a trend
  fit, or a constant on a non-value-axis visual (a horizontal `bar`'s ambiguous measure axis, a
  `scatter`'s dual axes, a `card`) keeps the faithful value/visual and raises a `… deferred (Tier-2
  analytics): <target> …` warning naming exactly the dropped overlay (phrased as a *KPI target/goal*
  on a card, a *reference/target/trend line* on a chart). Drawing those remaining overlays stays
  Tier-2.
- **Table calculations** and other window/running derivations (e.g. `WindowSum`) → field skipped
  **here**, but note this is now the *fallback of last resort*: a view-only **quick** table calc is
  first rebuilt as a Power BI **Visual Calculation** (see the section above), and a named model-level
  table-calc field is bound to a `WINDOW`/`OFFSET`/`RANKX` **measure** by the measure engine. A field
  is only skipped when neither path can pin it (an off-substrate calc — `SCRIPT_*`, `PREVIOUS_VALUE`
  recursion, a filters-shelf calc — or an axis/chain that the workbook facts don't fix), in which case
  the reason is disclosed rather than guessed.
- **Aggregation/type mismatch**: `Sum`/`Avg`/`Median` on a non-numeric column, or `Min`/`Max`
  on a non-numeric/non-date column → field skipped.
- **Date parts** (`Year`, `Month`, `Quarter`, …) → approximated as a plain date column; the
  date grain is *not* applied (flagged so it can be set manually).
- **Calculated field on an axis** (a measure where a category is required) → skipped.
- **Empty worksheet**: a structurally bare sheet (no resolved fields and no raw pills on any
  shelf or encoding — a blank/text/image placeholder a dashboard uses for spacing or a title) is
  classified precisely as `empty worksheet (no fields ...) -> nothing to rebuild`, distinct from
  an *unsupported mark*. This keeps intentional blanks out of the unsupported-coverage count; a
  sheet whose pills merely fail to resolve still keeps its generic "not supported" + resolve
  warnings (it is a real gap, never silently called "empty").
- **Caption fallback**: when a field has no embedded metadata record, it is bound by caption as
  a best effort and flagged to verify against the model's table/column names.
- **Tableau pseudo-fields** (`Measure Names`, `Measure Values`) → skipped
  when standalone. **Exception:** a worksheet built on the `Measure Values` shelf is *expanded*
  to its N member measures (see "Measure Values / Measure Names" above), so those worksheets are
  rebuilt — not skipped — and `Measure Names` is bound implicitly (never as a column).
- **Implicit row counts** (Tableau's silent `COUNT(*)` over `[__tableau_internal_object_id__]`, and
  the legacy `[Number of Records]` field) → recognised, **warned, never dropped or dangling**. Neither
  has a real model column, so the binder names the **fact table** the count belongs to (resolved from
  the object-id column caption, falling back to the relation name) and emits a `… implicit row count …
  add a COUNTROWS measure …` warning. A *bare* `[__tableau_internal_object_id__]` artifact with no
  `COUNT` instance (e.g. an internal filter pill) is **not** a row count and stays silently dropped.
  Full recovery is a cross-layer follow-up: a model-side `COUNTROWS` measure on that fact table, after
  which the binder exact-binds the count instead of warning. The estate report rolls the unbound count
  up under `viz_implicit_row_count` / `implicit_row_count_unbound` / `workbooks_implicit_row_count`.

### Filters → slicers (wireframe placeholders)

Worksheet filters are surfaced as **slicer** visuals so the field and intent survive the
migration: categorical → list slicer, date / relative-date → date slicer, numeric →
range slicer. These are **placeholders** — Tableau's filter *scope* (worksheet / dashboard /
context / data-source) and actions do not map 1:1 to Power BI slicer interactions, so slicer
wiring should be reviewed after import.

#### Applied selections → slicer `filterConfig`

When a worksheet filter narrows a field to specific members (or a numeric range), that
selection is carried onto the rebuilt slicer's top-level `filterConfig` so the report opens on
the **same filtered view** as the original. The emitted JSON uses the verified PBIR shapes
(categorical `In` / inverted `Not … In` with `isInvertedSelectionMode`; numeric `Advanced`
`Comparison` with `>=` / `<=` bounds). Warn-never-wrong governs *which* selections emit — a
wrong pre-filter would show wrong data, so only faithfully-bindable, JSON-verified shapes are
written:

| Tableau filter                                  | Emitted                                      |
| ----------------------------------------------- | -------------------------------------------- |
| categorical keep-list on a **string** dimension | `Categorical` `In` (members as literals)     |
| categorical exclude on a **string** dimension   | `Categorical` `Not … In` + inverted flag     |
| numeric range (min/max) on a numeric column     | `Advanced` `And` of `>=` / `<=` comparisons  |
| date-part categorical (e.g. month `'4'`)        | *deferred* → slicer shows all + fidelity note |
| `%null%`-sentinel-only selection                | *deferred* → slicer shows all + fidelity note |
| fixed date range (datetime-literal shape)       | *deferred* → slicer shows all + fidelity note |

Deferred cases leave the slicer at its faithful "show all" default and record a structured
`filter`-scope warning rather than risk a possibly-wrong pre-filter.

#### Dashboard parameter controls (`paramctrl` zones)

A dashboard can host a Tableau **parameter** as an on-canvas control (the "hamburger" picker;
`<zone type-v2='paramctrl' param='[Parameters].[…]'>`). These are captured structurally rather
than silently dropped: `parse_twb` resolves each control to the parameter's caption + datatype
and records it on the additive `ir["parameter_controls"]`
(`{param_id, caption, datatype, dashboard, position}`), de-duplicated across the primary and
phone/tablet `<devicelayouts>` so a control is counted once. A faithful slicer needs the
parameter's *target* (which model column it drives) — that binding is owned by the migrated
model, so it is supplied to the rebuild through the optional `param_binding` consumer contract:

```
param_binding = {
  "slicers": { "<parameter internal_name>": {"table", "column", "single_select"?, "caption"?} },
  "flags":   { "<tableau filter token>":    {"entity", "measure", "value", "visuals", "status"?} }
}
```

When a control's parameter appears in `param_binding["slicers"]` (matched bracket-tolerantly, so
the orchestrator's bracketed `[Parameter …]` keys resolve against the bracket-stripped control id),
it is **rebuilt as a categorical slicer** on `table[column]`, placed at the control's captured
(dashboard-scaled) position; the rebuild is recorded on the additive `ir["parameter_slicers"]`
(`{param_id, caption, dashboard, page, visual, kind, target, single_select}`) and the control's
standing warning is dropped. `single_select` is **recorded** for a later (Tier-2) selection-mode
pass but no single-select object is emitted yet (no verified PBIR shape — warn-never-wrong defers
the toggle rather than risk an unfaithful visual). A value-picker (disconnected picker-table)
parameter arrives in the same `{table, column}` slicer shape and rebuilds identically.

The `flags` map carries a **model keep-flag** — a parameter-driven keep calc (e.g. a relative-date
window selector that returns a keep-value to KEEP a mark and is `BLANK` otherwise) that the model
build translated into a measure. Each entry is keyed by the calc token and names its home `entity`,
the model `measure`, the keep `value`, and the `visuals` it scopes (the Tableau worksheet names the
calc filtered, sourced from the workbook's calc usage). Every named worksheet's rebuilt visual then
gets a **visual-level measure filter** `[measure] == value` — a top-level `filterConfig` `Advanced`
filter whose `field` is a `Measure` ref (bound by `Entity`) and whose `Where` comparison
(`ComparisonKind` 0 = Equal) reaches the measure through the `From` source alias — so the visual
opens on the same windowed rows, and the now-obsolete `aggregate/measure filter on '<token>'`
worksheet warning is dropped for it. The applied measure is recorded on the candidate record's
additive `flag_filters` fact. Warn-never-wrong governs the edges: a flag with a non-numeric value,
an empty/absent `visuals` scope, or a scope naming a worksheet the workbook lacks is left
**unapplied** with an honest warning — a filter is never applied to a guessed set of visuals.

Until the model identifies a target (no `param_binding`, or an entry missing its column), each
control emits one honest `dashboard`-scope warning (surfaced as a `warned` fidelity row) and
**no** slicer is written. Warn-never-wrong: the control is never reconstructed as a slicer
against a guessed target.

### Cross-visual interactions (default cross-filter / cross-highlight)

Power BI cross-highlights / cross-filters every visual on a page **by default**, and in PBIR that
default is **implicit** — you only write a page-level `visualInteractions` override (an array of
`{source, target, type}` with `type` ∈ `Default` / `DataFilter` / `HighlightFilter` / `NoFilter`)
when you want to *change* a specific source→target pair, and a report-level
`settings.defaultFilterActionIsDataFilter` flag only when you want every visual to filter rather
than highlight. The rebuilt report deliberately emits **neither**: every dashboard / worksheet page
is written with no `visualInteractions` and no `defaultFilterActionIsDataFilter`, so all visuals
(charts and slicers) cross-interact out of the box — a slicer filters its page, a mark selection
cross-highlights the rest. Tuning specific interactions (filter-vs-highlight, disabling a pair, or
wiring a Tableau filter/highlight *action* 1:1) is a Tier-2 concern and is left to the default.

### Per-visual candidate record (Tier-2 image-oracle seam)

The deterministic engine commits to exactly one visual type per worksheet. Alongside the PBIR it
also returns an **additive** decision record per emitted main visual on
`migrate_twb_to_pbir(...)["candidate_records"]` (also on `ir["candidate_records"]`) for a later,
agent-driven **image-oracle** pass. Each record carries `page` (the sanitized PBIR page id),
`page_display` (the dashboard / worksheet caption — the key the offline thumbnail is matched on),
`visual` (the PBIR visual name), `worksheet`, the chosen `visual_type`, a ranked `candidates` list
(chosen first — the *only* types the oracle is allowed to switch to), a `confidence` (`high` where
the shelf layout is decisive; `medium` for a heuristic / hack reroute or a genuine visual look-alike
an image can disambiguate), a `hack` flag for non-standard compositions (`dual-axis pie/donut`,
`running-total Gantt`, `bump/rank`, `dual-axis combo`), the read-only `fields` truth
(`{role: [queryRef]}` — the oracle must **never** rebind fields, which are exact-bound to the
model), and the faithful `position` (incl. `z` / `tabOrder` for overlap / z-order analysis). It is
purely additive: **nothing** about the record is written into the PBIR definition, so the rebuilt
report is byte-for-byte unchanged whether or not the oracle runs.

### Image-oracle harness + applier (Tier-2, opt-in)

`scripts/image_oracle.py` turns those candidate records into an agent-driven vision pass and applies
its answers — without ever touching field bindings. It is **opt-in**: the Tier-1 PBIR stands on its
own; the oracle only ever *refines a chart type* (and, optionally, a position) the engine already
emitted. The full numbered runbook (with the bold do-NOTs) is in
[`resources/image-oracle.md`](image-oracle.md). In brief:

- **`build_oracle_bundle(candidate_records, twb_xml=, image_dir=, images_out=)`** assembles a
  JSON-serialisable *adjudication bundle*: per main visual, the candidate list + an **offline-first**
  image reference (caller-provided file → embedded `.twb`/`.twbx` `<thumbnail>` PNG → none), the
  read-only field truth, and a pre-filled answer template. Embedded thumbnails are keyed by the
  dashboard/worksheet *display* name (matched via `page_display`), so a dashboard composition (e.g. a
  donut with a KPI floating in its hole) gets the rendered page as its picture. No image ⇒ the visual
  is reported `present: false` and the deterministic pick simply stands.
- **`agent_prompt(bundle)`** renders the driving-agent instruction (no API key, no tool call): the
  hard invariants, then each reviewable visual with its image path and the closed set of types it may
  choose from.
- **`apply_adjudications(parts, candidate_records, adjudications)`** deterministically re-binds a
  visual's `visualType` **only** to a type already in its candidate list (and optionally adopts a
  corrected position), returning a new parts dict plus an `{applied, kept, rejected}` report. A
  non-candidate type, an unknown visual, or anything that would alter the query/fields is **rejected,
  never applied** — the query/`queryState` is asserted byte-identical across every type switch.

### Viz advisor — recommend visuals from data semantics (Tier-2, opt-in)

`scripts/viz_advisor.py` is the *forward* peer of the image oracle. Where the oracle **corrects** the
type of a worksheet the Tier-1 engine already rebuilt, the advisor **proposes** visuals from data
semantics alone — given a set of model fields (a measure / calc selection), it ranks candidate charts
for laying out a new report. It is the recommender analogue of the calc compiler's Tier-2
assisted-translation loop (deterministic floor → out-of-band suggestion → gated landing), and it is
**opt-in and additive**: it writes nothing into the PBIR and the main bind path is unchanged.

It holds the same **warn-never-wrong** contract in three layers:

- **Deterministic floor.** `recommend_visuals(fields, intent=, max_suggestions=6)` encodes known-good
  viz facts (which data shape suits which chart) as an authored rule table and returns a ranked list of
  `{visual_type, encodings, confidence, reasoning, rank}`. It **never** calls a model. Each field is a
  `{name, role, data_type, semantic_role?, cardinality?}` dict; `normalize_field` derives `temporal`
  (date/time types) or `geo` (place-name cues, matched as **whole word tokens** — split on separators
  and camelCase — so `Country`/`Postal Code`/`CustomerCity` read as geographic while a field that
  merely embeds a cue, e.g. `Ethnicity`/`Relationship`/`Real Estate`, does not) when not given, and
  raises on a name-less / role-less
  field (the advisor never guesses a field's identity). The rule table covers: a lone measure → `card`
  (several → `multiRowCard`); a geo dimension + measure → `shapeMap` / `map`; a temporal dimension +
  measure → `lineChart` / `areaChart`; one categorical dimension + measure → `clusteredColumnChart` /
  `clusteredBarChart` (+ `pieChart` only when low-cardinality); one dimension + several measures →
  grouped columns / combo; two dimensions + measure → `pivotTable` / `stackedColumnChart`; two measures
  → `scatterChart`; and a universal `tableEx` fallback so the result is never empty. Every recommended
  type is in the closed `PBIR_VISUAL_TYPES` vocabulary (mirrors `twb_to_pbir`'s `_VT_TO_PBIR`).
- **Out-of-band assist (never auto-lands).** `build_advice_bundle` / `advice_prompt` hand the
  read-only field truth, the deterministic candidate set (`top_pick` = `candidates[0]`), the hard rules,
  and a pre-filled answer template to the **separate** agent / vision pass to re-rank or refine by user
  intent. No API key, no embedded tool call; deterministic tests inject the agent's answer.
- **Gated landing.** `validate_suggestion(suggestion, fields, candidates)` accepts an answer **only**
  when its chart type is in the deterministic candidate set *and* every encoded field exists in the
  provided set on a role-compatible slot (a measure on a value/size/angle/X-Y slot; a dimension on a
  category/legend/axis/location slot). `apply_advice(candidates, answer, fields)` keeps the
  deterministic top pick on a null / out-of-range / failing-validation answer, lands a valid non-top
  choice otherwise, and returns an `{applied, kept, rejected}` audit report — a suggestion is never
  silently emitted. `refine_with_feedback` is the multi-turn hook (per-type up/down re-rank, clamped).

**Pipeline hook (opt-in).** The advisor plugs into the estate run as an additive sidecar: pass
`--viz-advice` (or `migrate_estate(..., viz_advice=True)`) and each workbook gains a
`reports/<Name>.viz-advice.json` written *beside* (never inside) its `.Report` folder. It is built
from the viz stage's read-only `candidate_records` via `build_report_advice` — for every rebuilt
visual it lists ranked **alternative** chart types for that visual's *existing* fields (role inferred
from the PBIR slot; a `field_types` map refines temporal/geo when the model types are known). It never
proposes a rebinding, and a visual whose field roles cannot be reliably recovered (e.g. the universal
detail table's mixed `Values` well) is reported `advisable: false` with a reason and no suggestion.
The hook writes nothing into the PBIR definition and `report.json` only gains a per-workbook
`viz_advice` key, so a run **without** the flag is byte-identical.

## Tests

`tests/test_twb_to_pbir.py` is fully offline (inline `.twb` XML string fixtures, no disk, no
network). It asserts the normalized IR (entity/property/aggregation per visual), the emitted
PBIR JSON structure (report scaffold, page-per-dashboard, orphan-worksheet page, role
projections, field expressions, unique queryRefs, zone scaling within page bounds) and that
unsupported marks/derivations/filters produce warnings rather than visuals.

`tests/test_image_oracle.py` and `tests/test_viz_advisor.py` are likewise fully offline (no model /
LLM call): the latter pins the advisor's deterministic ranking per rule-table branch, the bundle /
prompt shape, the `validate_suggestion` gate (reject a non-candidate type, an unknown field, or a
role-incompatible slot), `apply_advice` (keep the top pick on a null / out-of-range / invalid answer;
land a valid choice), and `refine_with_feedback` re-ranking.
