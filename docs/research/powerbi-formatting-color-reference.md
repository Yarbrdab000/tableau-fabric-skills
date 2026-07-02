# Power BI Formatting & Color — Serialization Reference

> **Purpose.** An exhaustive, evidence-grounded reference for how Power BI \*\*serializes and
> resolves visual formatting and color\*\*, extracted from a real corpus of 52 `.pbix` dashboards
> (plus one enhanced‑format `.pbip`). It is written for the \*\*Tableau → Power BI dashboard
> migration tool\*\*: it favors exact property paths, value grammars, defaults, and precedence over
> prose. Every claim is tagged with its evidence class. Its machine-readable companion is
> [`powerbi-formatting-inventory.json`](./powerbi-formatting-inventory.json) — load that for the
> flattened `object → property → type → default → layer → Tableau‑equivalent → sources` index.

## Evidence conventions (read first)

| Tag | Meaning |
| --- | --- |
| **[CORPUS]** | Observed directly in the extracted `.pbix` corpus. A source is cited as `file → page → visual → object`. |
| **[SCHEMA]** | From the official Power BI **report theme JSON schema** / formatting docs. May be valid but not observed applied in this corpus. |
| **[PBIR]** | Enhanced report format (`definition/`) — grounded in the one `.pbip` attachment's *structure*; that file carried **no applied formatting objects**, so PBIR object *paths* are schema-grounded, not value-observed. |
| **[INFERRED]** | Our interpretation (e.g., an enum value). Flagged so the migration tool can verify before relying on it. |
| **⇄ Tableau** | Running Tableau-equivalence annotation for the migration consumer. "**GAP**" = no clean Tableau counterpart. |

The corpus is treated as **ground truth for what appears in the wild**, not merely what docs
permit. Where corpus and schema disagree, or the corpus uses something undocumented, it is flagged
in §12.

---

## 0. Corpus & extraction method

### 0.1 Files (52 `.pbix`, legacy format) + 1 `.pbip` (PBIR)

All 52 `.pbix` serialize in the **legacy `Report/Layout`** format. They are the union of two
sources, deduplicated **by MD5** (6 byte-identical duplicates collapsed, e.g. the repo's
`new-power-bi-service-samples/` vs `powerbi-service-samples/` folders and a `Sales and Marketing`
copy): **9 curated Microsoft sample dashboards** + \*\*49 `.pbix` from `powerbi-desktop-samples`\*\*
(monthly "Blog Demo" builds 2018SU04→2020SU11, the Sample Reports set, the service-samples set, DAX
& AdventureWorks samples). A few are utility/demo files with little or no report formatting
(`2018SU10 Data Profiling Demo`, `2018SU10 Fuzzy Matching Demo`, `PerformanceAnalyzerExportReport`,
`customerfeedback`) — kept for completeness; they surface as empty rows in the §13 coverage table.
The lone enhanced‑format sample is the user's `.pbip` attachment (Superstore rebuild), used only to
document PBIR structure. Version spread of the legacy files: `1.11 → 1.31`. See §13 for the full
per-file list.

### 0.2 A `.pbix` is a ZIP — the parts that matter for formatting

```javascript
Report/Layout                                   # THE report definition (UTF‑16 LE)
Report/StaticResources/SharedResources/BaseThemes/<name>.json   # system base theme (full schema)
Report/StaticResources/BuiltInThemes/<name>.json                # named built-in theme (colors only, usually)
Report/StaticResources/RegisteredResources/<file>.json          # user custom theme (partial or full)
Report/StaticResources/RegisteredResources/<image>.png|jpg      # wallpaper / background / button images
Report/CustomVisuals/<id>/                       # imported custom visuals (19/52 files)
```

### 0.3 Three parsing gotchas (a migration reader MUST handle all three)

1. **`Report/Layout` is UTF‑16 LE** (with BOM). Decode as `utf-16` or you get mojibake. Theme
   JSONs are UTF‑8, frequently **with a BOM** — read them `utf-8-sig`.
2. **Each `visualContainer.config` is a JSON *string* nested inside the layout JSON.** You must
   parse the outer object, then `json.loads()` each `config` string again (double-parse). The same
   is true of each `section.config`. **[CORPUS]**
3. **Container ("chrome") formatting lives under `singleVisual.vcObjects`, *not* **`config.vcObjects`.****
   Across all **8,905** visual containers in the corpus, `config.vcObjects` was empty; container
   formatting was under `singleVisual.vcObjects` (populated on **7,805**; data-role
   `singleVisual.objects` on **8,717**). This is easy to get wrong and silently return zero container
   formatting. **[CORPUS]**

### 0.4 Legacy layout object map

```javascript
layout (root)
├─ config (stringified JSON) → { themeCollection:{ baseTheme, customTheme }, ... }
├─ sections[]  (= pages/tabs)
│   ├─ config (stringified JSON) → { objects: { background, outspace, displayArea, ... } }   # PAGE formatting
│   └─ visualContainers[]
│        └─ config (stringified JSON)
│             └─ singleVisual  (or singleVisualGroup for grouped visuals)
│                  ├─ visualType : "barChart" | "pivotTable" | "card" | ...
│                  ├─ objects   : { <dataRoleObject>: [ {properties, selector?} ] }   # DATA-ROLE formatting
│                  └─ vcObjects : { <containerObject>: [ {properties, selector?} ] }  # CONTAINER formatting
```

- `singleVisual.objects` → **data-role formatting** (axes, labels, data colors, legend, values…).
- `singleVisual.vcObjects` → **container/chrome formatting** (background, border, title, shadow,
  visual header, tooltip…).
- `section.config.objects` → **page/canvas formatting**.
- Every object value is an **array of instances**; each instance = `{ properties:{…}, selector?:{…} }`.
  A **selector** narrows the instance to a series/category/measure/data point (see §1.4).

### 0.5 Enhanced (PBIR) object map **[PBIR]**

```javascript
<name>.pbip                         # pointer file
definition.pbir                     # dataset reference (version 4.0)
definition/report.json              # { themeCollection:{ baseTheme:"CY24SU10" }, ... }
definition/pages/pages.json         # { pageOrder[], activePageName }
definition/pages/<pageId>/page.json # { displayName, displayOption:"FitToPage", height:720, width:1280, ... }
definition/pages/<pageId>/visuals/<visualId>/visual.json
     └─ { position:{x,y,z,width,height,tabOrder}, visual:{ visualType, query,
            objects:{…},                    # DATA-ROLE formatting (was singleVisual.objects)
            visualContainerObjects:{…} } }  # CONTAINER formatting (was vcObjects)
```

PBIR is plain UTF‑8, one folder per page and per visual, with `$schema` URLs pointing at the
Fabric `.../report/definition/...` schemas. **Object *names* are shared with the legacy format**;
the two structural renames are `vcObjects → visualContainerObjects` and the flattening of the
stringified `config` into real nested JSON. Because the attachment's visuals carried no applied
formatting objects, all PBIR object paths below are \*\*[PBIR]/[SCHEMA]\*\*‑grounded, not value-observed.

⇄ **Tableau:** the whole `.pbix`/`.pbip` container ≈ a Tableau `.twbx`/`.twb`; `Report/Layout`
sections ≈ Tableau **dashboards**, `visualContainers` ≈ dashboard **zones/worksheets**. Tableau
stores formatting as XML on worksheets/dashboards; Power BI stores it as the JSON object bags above.

---

## 1. Architecture, serialization & value grammar

### 1.1 The four formatting layers (lowest → highest authority)

```javascript
(1) Base theme defaults        BaseThemes/<name>.json  (structural colors + textClasses)
(2) Applied custom theme       themeCollection.customTheme  (RegisteredResources/ or BuiltInThemes/)
(3) Theme visualStyles         theme.visualStyles[visualType|'*'][selector|'*'][object][]
(4) Per-visual object property  singleVisual.objects / singleVisual.vcObjects  (per container)
(5) Per-instance selector       an object instance carrying a data/metadata selector (per series/point/measure)
```

Full resolution order and worked example: **§8 Precedence**. This layering is the single most
important thing for the migration tool — the same logical property (say, a bar's fill) can be set
at four different layers with different serializations.

⇄ **Tableau:** Tableau has only two comparable tiers — **Workbook > Format** (font/line defaults,
≈ theme `textClasses`/`visualStyles`) and **per-worksheet formatting + Marks card** (≈ per-visual
objects). Tableau has **no per-visual-*type* default layer** equivalent to `visualStyles[visualType]`.

### 1.2 THREE value encodings (the core grammar)

Power BI serializes the *same* conceptual value three different ways depending on layer. A faithful
writer must emit the right one per target. **[CORPUS]**

**(A) Visual-container object encoding** — used in `singleVisual.objects`, `singleVisual.vcObjects`,
and `section.config.objects`. Scalars are **expression-wrapped**:

```json
"fontSize":   { "expr": { "Literal": { "Value": "11" } } }
"show":       { "expr": { "Literal": { "Value": "true" } } }
"titleText":  { "expr": { "Literal": { "Value": "'Sales for Top 5 Categories'" } } }
```

**(B) Theme `visualStyles` encoding** — **raw JSON**, *not* expression-wrapped:

```json
"gridlineStyle": "dotted",   "transparency": 0,   "strokeWidth": 3,   "titleWrap": true
```

**(C) Rich-text `textStyle` encoding** — CSS-like, with **unit suffixes**:

```json
"textStyle": { "fontSize": "12pt", "color": "#ffffff" }
```

### 1.3 Literal value grammar (inside encoding A)

| Type | Serialization | Example |
| --- | --- | --- |
| String | single-quoted **inside** the value string | `"Value":"'Top'"` |
| Boolean | lowercase word | `"Value":"true"` / `"false"` |
| Integer | plain | `"Value":"12"` |
| Double | trailing `D` | `"Value":"0.2D"`, `"Value":"100D"` |
| Long | trailing `L` | `"Value":"12L"` |
| Null | word | `"Value":"null"` |

**Type inconsistencies to expect [CORPUS]:** `fontSize` appears as a **string** (`"11"`) in some
objects and an **int** in others; some objects use `fontSize`, others `textSize` (e.g. `grid.textSize`,
`items.textSize` on slicers/tables) for the same idea; `fontFamily` values are **CSS font stacks**,
e.g. `"'Segoe UI Bold', wf_segoe-ui_bold, helvetica, arial, sans-serif"`.

### 1.4 Color encodings

```json
// literal hex (frozen at author time — does NOT follow the theme)
{ "solid": { "color": { "expr": { "Literal": { "Value": "'#118DFF'" } } } } }

// theme palette reference (FOLLOWS the theme when re-themed)
{ "solid": { "color": { "expr": { "ThemeDataColor": { "ColorId": 0, "Percent": 0 } } } } }

// theme visualStyles color (encoding B): plain, OR expr-wrapped ThemeDataColor
{ "solid": { "color": "#ffffff" } }
```

- **`ThemeDataColor.ColorId`** indexes the winning `dataColors[]` palette (0-based). **[CORPUS]**
- **`ThemeDataColor.Percent`** = tint/shade in `[-1, 1]`: negative = darker (shade), positive =
  lighter (tint), `0` = exact palette color. Observed values include `-0.1`, `-0.2`, `-0.25`.
  Example: `border.color = themeColor[0] @ -0.1` (10% darker than palette slot 0). **[CORPUS]**
- Literal vs. ThemeDataColor is **migration-critical**: Tableau colors are literal RGB, so a naïve
  import yields **frozen literal hex** that will *not* respond to a Power BI theme swap. To make
  migrated colors theme-aware you must deliberately re-bind them to `ThemeDataColor` refs (§3.2, §8).

### 1.5 Selectors — how one object instance targets part of the data

Each object instance may carry a `selector`. Selector census across the corpus **[CORPUS]**:

| Selector shape | Count | Meaning |
| --- | ---: | --- |
| `{ id }` | 32,314 | a specific container element (shapes/buttons/tooltip parts) |
| `{ metadata }` | 1,095 | one **measure/column** (per-field formatting; the CF anchor) |
| `{ dataScope }` (Scope/Expr) | 952 | one category / series value |
| `metadata + dataScope` | 609 | a measure within a category scope |
| `{ dataWildcard }` | 42 | **all** series (apply to every data point) |
| `metadata + id` | 36 | measure within a named element |
| `metadata + dataWildcard` | 21 | measure across all series |

An instance **with no selector** is the object default; a **selector-scoped** instance overrides it
for the matching scope (§8). Presence of a `metadata`/`dataScope`/`dataWildcard` selector is the
signal that a color/label is **conditional or per-series** rather than global.

⇄ **Tableau:** selectors ≈ how Tableau scopes formatting to a **field on the Marks card** vs. a
specific **header/pane** in the Format pane. `dataWildcard` ≈ "apply to all"; `metadata` ≈ a
measure pill; `dataScope` ≈ a discrete dimension member.

### 1.6 Value-kind census (whole corpus) **[CORPUS]**

The distribution of leaf value kinds tells the migration tool what it will actually encounter:

| Value kind | Count | Notes |
| --- | ---: | --- |
| `literal:bool` | 38,837 | show/enable toggles dominate |
| `literal:str` | 29,735 | positions, alignments, names, font stacks |
| `literal:int` | 21,036 | sizes, transparency, margins |
| `solid:themeColor` | 12,052 | ThemeDataColor refs (theme-following) |
| `solid:literal:hex` | 10,077 | frozen hex colors |
| `raw` | 1,740 | rich-text paragraph trees, misc nested |
| `literal:double` | 549 | e.g. thresholds `0.2D`, widths |
| `fieldRef` | 402 | value bound to a measure/column (incl. field-driven color/title) |
| `expr:ResourcePackageItem` | 392 | image references (wallpaper/button/logo) |
| `image` | 223 | image fill blocks |
| `object:propertyDefinitionKind,value,context` | 185 | bound/parametrized properties |
| `solid:conditional` | 180 | rule-based `Conditional.Cases` colors |
| `object:filter` | 164 | filter blobs on general objects |
| `object:positiveColor,negativeColor,axisColor,reverseDirection` | 130 | **table data bars** |
| `solid:fillRule/gradient` | 52 | color-scale gradients |
| `object:linearGradient3` / `linearGradient2` | 39 / 24 | 3-stop / 2-stop scales |
| `solid:literal:str` | 34 | color-as-string (named/sentinel) |
| `solid:fieldRef` | 9 | color bound directly to a field |
| `geoJson` | 7 | shape-map geography |
| `object:algorithm,parameters` | 7 | anomaly/forecast transforms |
| `object:exprs,kind` | 3 | anomaly `explainBy` |

---

## 2. Theme layer reference

### 2.1 Theme locations & selection

Themes live in three places; the **active** theme is chosen by `config.themeCollection` in the
report (or `section` config for a page-scoped theme). **[CORPUS]**

- `.../SharedResources/BaseThemes/<name>.json` — the **base/system** theme. Carries the \*\*full
  schema\*\* (structural colors + sentiment + divergent + `textClasses` + `visualStyles`). Names
  observed: `CY17SU12, CY18SU07, CY19SU06, CY19SU12, CY20SU09, CY21SU04, Fluent 2 (Preview)`.
- `.../BuiltInThemes/<name>.json` — a **named gallery** theme (\`Electric, Solar, NewExecutive,
  Elektra, Sunflower Twilight\`).
  Usually **colors only** (`dataColors[]`, no `textClasses`/`visualStyles`).
- `.../RegisteredResources/<file>.json` — a **user custom** theme. May be **partial or full**
  (`theme created by powerbi.tips…`, `Fidex, Miguel, Music Charts, Classroom, Custom, Sunset`).

`config.themeCollection = { baseTheme: <BaseThemes name>, customTheme?: <applied override> }`. When
`customTheme` is present its keys win over `baseTheme` (§8, rank 2). 25/52 files apply a custom theme.

### 2.2 Report theme JSON schema (end to end)

Every key below is either observed in a corpus base theme **[CORPUS]** or documented **[SCHEMA]**.
Defaults shown are the Power BI factory defaults (`CY…SU…` base themes).

| Key | Type | Role | Default (base) | ⇄ Tableau |
| --- | --- | --- | --- | --- |
| `name` | string | theme name | — | workbook/theme name (no direct artifact) |
| `dataColors` | hex[] | **ordered** categorical accent palette (see §3.1) | 8–41 colors | discrete color palette (`Preferences.tps`) + Color on Marks |
| `foreground` | hex | default text/label color | `#252423` | Format > Font default color |
| `foregroundNeutralSecondary` | hex | axis/legend label text | `#605E5C` [SCHEMA] | secondary label color |
| `foregroundNeutralTertiary` | hex | faint text/disabled | `#B3B0AD` [SCHEMA] | — |
| `background` | hex | canvas/visual background | `#FFFFFF` | worksheet/dashboard shading |
| `backgroundLight` | hex | banding/secondary fill | `#F3F2F1` [SCHEMA] | row banding light |
| `backgroundNeutral` | hex | neutral fill | `#C8C6C4` [SCHEMA] | — |
| `tableAccent` | hex | table/matrix grid accent | `#118DFF` | table borders / banding color |
| `good` | hex | positive sentiment (KPI ↑, waterfall increase) | `#1AAB40` | **GAP** — build via calc |
| `neutral` | hex | neutral sentiment | `#D9B300` | **GAP** |
| `bad` | hex | negative sentiment (KPI ↓, decrease) | `#D64554` | **GAP** |
| `maximum` | hex | color-scale **max** endpoint | `#118DFF` | continuous legend max |
| `center` | hex | diverging **center** | `#D9B300` | diverging palette center |
| `minimum` | hex | color-scale **min** endpoint | `#DEEFFF` | continuous legend min |
| `null` | hex | color for null in a scale | `#FF7F48` | null/special-value color |
| `hyperlink` | hex | hyperlink text | `#0000FF` [SCHEMA] | — |
| `visitedHyperlink` | hex | visited hyperlink | `#551A8B` [SCHEMA] | — |
| `textClasses` | object | named text styles (see §2.4) | see §2.4 | Workbook > Format font defaults (partial) |
| `visualStyles` | object | per-visual-type default overrides (see §2.5) | varies | **GAP** — no per-type style tier |

**Structural color families observed** in the corpus base themes: \`foreground(+NeutralSecondary/
Tertiary)`,` background(+Light/Neutral)`,` tableAccent`. **Sentiment**:` good/neutral/bad\`.
**Divergent**: `maximum/center/minimum/null`. **[CORPUS]**

### 2.3 `dataColors[]` — ordering & wrap

- The array is **ordered**; series/category *N* takes `dataColors[N]` (0-based), **wrapping** when
  the palette is exhausted. Re-ordering the array re-colors every series that uses palette refs.
- Observed lengths: **0, 8, 10, 24, 32, 40, 41, and 480**. Factory `CY…SU…` themes carry **41**
  (except the two oldest, `CY17SU12` and `CY18SU07`, which ship **0** `dataColors` — the palette then
  comes from the built-in/custom theme layered over them); built-ins `Electric`=10, `Solar`=24,
  `NewExecutive`=32, `Sunflower Twilight`=32, `Elektra`=40; `Fluent 2 (Preview)`=32. Custom themes
  from **powerbi.tips** (\`Classroom, Fidex, Miguel, Music Charts, Sunset, theme created by
  powerbi.tips…\`) carry **480-color** palettes (see §12 — undocumented practice). **[CORPUS]**

⇄ **Tableau:** `dataColors[]` ≈ a Tableau **discrete/categorical color palette**; ordering ≈
Tableau's palette assignment order on a discrete color legend. Tableau continuous palettes map to
`minimum/center/maximum` (§3.3), not `dataColors`.

### 2.4 `textClasses` — the text-style system

Real base-theme block (`2020SU11 Blog Demo - November.pbix → BaseThemes/CY19SU12.json`) **[CORPUS]**:

```json
"textClasses": {
  "callout": { "fontSize": 45, "fontFace": "DIN",                "color": "#252423" },
  "title":   { "fontSize": 12, "fontFace": "DIN",                "color": "#252423" },
  "header":  { "fontSize": 12, "fontFace": "Segoe UI Semibold",  "color": "#252423" },
  "label":   { "fontSize": 10, "fontFace": "Segoe UI",           "color": "#252423" }
}
```

- Each class = `{ fontFace, fontSize, color }`. `fontSize` here is an **int point size** (contrast
  with the string/`pt` forms elsewhere — §1.3).
- The corpus base themes serialize the **four primary classes** above; the full documented set is
  larger **[SCHEMA]**: \`callout, title, header, label, largeTitle, largeLabel, smallLabel,
  lightLabel, boldLabel, semiboldLabel, lightSmallLabel, labelInfoText\`. Unserialized classes
  inherit factory defaults.
- Visuals map their text to a class (e.g. a card's big number → `callout`, axis labels → `label`,
  table column headers → `header`). Setting the class in the theme restyles every visual that
  inherits it.

⇄ **Tableau:** `textClasses` ≈ Tableau **Workbook > Format** font defaults (Worksheet/Tooltip/Title
fonts) — *partial*: Tableau's defaults are per-format-area, not a named reusable class system.

### 2.5 `visualStyles` — per-visual-type default overrides

Structure: `visualStyles[ visualType | "*" ][ selector | "*" ][ objectName ] = [ { prop: value } ]`.
Values use **encoding B** (raw). Real base-theme excerpt (`CY19SU12`) **[CORPUS]**:

```json
"visualStyles": {
  "*": {
    "*": {
      "*":            [ { "transparency": 0, "wordWrap": true } ],
      "categoryAxis": [ { "showAxisTitle": true, "gridlineStyle": "dotted" } ],
      "valueAxis":    [ { "showAxisTitle": true, "gridlineStyle": "dotted" } ],
      "title":        [ { "titleWrap": true } ],
      "lineStyles":   [ { "strokeWidth": 3 } ],
      "background":   [ { "show": true, "transparency": 0 } ],
      "outspacePane": [ { "backgroundColor": {"solid":{"color":"#ffffff"}},
                          "foregroundColor": {"solid":{"color":"#252423"}},
                          "transparency": 0, "border": true,
                          "borderColor": {"solid":{"color":"#B3B0AD"}} } ]
    }
  },
  "page": { "*": { "outspace":  [ { "color": {"solid":{"color":"#FFFFFF"}} } ],
                   "background": [ { "transparency": 100 } ] } },
  "lineChart": { "*": { "general": [ { "responsive": true } ] } }
}
```

Custom-theme excerpt (`theme created by powerbi.tips`), showing palette-ref colors in `visualStyles`
and the very common **`visualHeader.show=false`** default **[CORPUS]**:

```json
"*": {
  "*":            [ { "fontFamily":"Segoe UI", "titleFontSize":"12", "titleFontFamily":"Segoe UI" } ],
  "visualHeader": [ { "show": false } ],
  "title":        [ { "show": true, "alignment":"Left", "fontSize":"12", "fontFamily":"Segoe UI",
                      "fontColor": {"solid":{"color":{"expr":{"ThemeDataColor":{"ColorId":1,"Percent":0}}}}} } ],
  "valueAxis":    [ { "show": true, "showAxisTitle": true, "fontSize":"10", "fontFamily":"Segoe UI",
                      "labelColor": {"solid":{"color":{"expr":{"ThemeDataColor":{"ColorId":1,"Percent":0}}}}} } ]
}
```

Key takeaways for the tool: (a) `visualStyles` is where a theme sets \*\*gridline style, default
fonts, whether the visual header shows, default background transparency\*\* — not just colors; (b) the
`"*" / "*"` wildcard applies to **all visual types / all selectors**; (c) `fontSize` in `visualStyles`
is a **string**; (d) colors here can be plain (`#hex`) or expr-wrapped `ThemeDataColor`.

### 2.6 Observed themes (76 theme parts across the 52 files; 19 distinct `name × location`) **[CORPUS]**

Because most files share a handful of factory/built-in themes, the raw 76 theme parts collapse to
**19 distinct `(name, location)` pairs**. Distilled (sorted BaseThemes → BuiltInThemes →
RegisteredResources), with the number of files each appears in:

| Theme name | Location | dataColors | textClasses | visualStyles keys | # files |
| --- | --- | ---: | --- | ---: | ---: |
| CY17SU12 | BaseThemes | 0 | no | 3 | 14 |
| CY18SU07 | BaseThemes | 0 | no | 17 | 14 |
| CY19SU06 | BaseThemes | 41 | yes | 25 | 7 |
| CY19SU12 | BaseThemes | 41 | yes | 26 | 4 |
| CY20SU09 | BaseThemes | 41 | yes | 28 | 5 |
| CY21SU04 | BaseThemes | 41 | yes | 29 | 2 |
| Fluent 2 (Preview) | BaseThemes | 32 | yes | 37 | 1 |
| Electric | BuiltInThemes | 10 | no | 0 | 3 |
| Elektra | BuiltInThemes | 40 | no | 0 | 5 |
| NewExecutive | BuiltInThemes | 32 | no | 0 | 2 |
| Solar | BuiltInThemes | 24 | no | 0 | 1 |
| Sunflower Twilight | BuiltInThemes | 32 | no | 0 | 3 |
| Classroom | RegisteredResources | 480 | yes | 0 | 3 |
| Custom | RegisteredResources | 8 | yes | 2 | 1 |
| Fidex | RegisteredResources | 480 | yes | 1 | 2 |
| Miguel | RegisteredResources | 480 | no | 3 | 1 |
| Music Charts | RegisteredResources | 480 | no | 2 | 1 |
| Sunset | RegisteredResources | 480 | no | 0 | 5 |
| theme created by powerbi.tips… | RegisteredResources | 480 | no | 2 | 1 |

Every file resolves a **base theme**; **25/52** additionally apply a **custom theme**. Custom themes
are frequently **partial** (colors only, no `textClasses`). Note the two oldest base themes
(`CY17SU12`, `CY18SU07`) carry **0** `dataColors` and only a few `visualStyles` keys — early base
themes that leaned on built-in palettes; from `CY19SU06` on, the base theme is a full 41-color schema
with `textClasses`. The per-file `(name, location)` breakdown is in the companion JSON
(`themesObserved`).

---

## 3. Coloring deep dive

### 3.1 Palette assignment & ordering

Series/category colors resolve, in order: an explicit per-datapoint `dataPoint.fill` (with a
`dataScope`/`metadata` selector) → `dataPoint.defaultCategoryColor` → the theme `dataColors[]` slot
by series index (wrapping). `dataPoint.fill` is by far the most common data-color carrier — observed
with **all five** color kinds: `themeColor, literal:hex, fillRule/gradient, conditional, fieldRef`,
across `dataScope`(833), `metadata`(164), `dataWildcard`(42) selectors. Source:
`2018SU09 Blog Demo → Overview → donutChart → data.dataPoint`. **[CORPUS]**

⇄ **Tableau:** `dataPoint.fill` ≈ **Color on the Marks card**. A per-`dataScope` fill ≈ assigning a
specific color to a discrete dimension member; palette-index assignment ≈ Tableau's automatic
categorical color legend.

### 3.2 Literal vs. theme-following color (migration-critical)

| Encoding | Follows theme? | Migration behavior |
| --- | --- | --- |
| `Literal "'#118DFF'"` | **No** — frozen | A Tableau color imported as-is lands here → theme-independent. |
| `ThemeDataColor {ColorId,Percent}` | **Yes** | Re-themes automatically; requires mapping the color to a palette slot. |

Recommendation for the tool: import Tableau literal colors as **literal hex** for fidelity, and
*optionally* offer to re-bind to the nearest `dataColors[]` slot to make the report theme-aware.

### 3.3 Conditional formatting — the three forms

Power BI conditional color attaches to a **target property** (`dataPoint.fill`,
`values.backColor`/`fontColor`, `columnFormatting.fontColor`, `dataBars.dataBarColor`, etc.) via a
per-measure `metadata` selector. Three serialization forms **[CORPUS]**:

**(a) Rule-based — `expr.Conditional.Cases[]`.** Real (\`2018SU09 Blog Demo → Sales tooltip →
pivotTable → data.values.backColor\`):

```json
"backColor": { "solid": { "color": { "expr": { "Conditional": { "Cases": [
  { "Condition": { "Comparison": {
        "ComparisonKind": 2,
        "Left":  { "Measure": { "Expression": { "SourceRef": { "Entity": "Sales" } }, "Property": "YoY" } },
        "Right": { "Literal": { "Value": "0.2D" } } } },
    "Value": { "Literal": { "Value": "'#63c345'" } } },
  { "Condition": { "And": { "Left": { "Comparison": { … } }, "Right": { "Comparison": { … } } } },
    "Value": { "Literal": { "Value": "…" } } }
] } } } }
```

- Each case = `{ Condition, Value }`. `Condition` is a `Comparison` or a boolean `And`/`Or` tree of
  Comparisons. `Left` is a `Measure`/`Column` ref; `Right` is a `Literal` threshold (`0.2D`).
  `Value` is a color literal (or a field ref).
- **`ComparisonKind` enum [INFERRED]:** \`0 = Equal, 1 = GreaterThan, 2 = GreaterThanOrEqual,
  3 = LessThan, 4 = LessThanOrEqual`. The corpus uses` 2\` (≥). Verify per case before relying on it.

**(b) Gradient / color-scale — `fillRule.linearGradient2|3`.** Two-stop (min/max) or three-stop
(min/mid/max), each `{ color }`, plus `nullColoringStrategy.strategy` (only `'asZero'` observed).
Real 3-stop (`2018SU09 Blog Demo → Overview → scatterChart → data.dataPoint`) **[CORPUS]**:

"fillRule": { "linearGradient3": {
  "min": { "color": { "expr": { "Literal": { "Value": "'#e82c3a'" } } } },
  "mid": { "color": { "expr": { "Literal": { "Value": "'#f2c811'" } } } },
  "max": { "color": { "expr": { "Literal": { "Value": "'#63c345'" } } } },
  "nullColoringStrategy": { "strategy": { "expr": { "Literal": { "Value": "'asZero'" } } } } } }

- Colors may be **literal hex** *or* the sentinels \*\*`'minColor'` / `'maxColor'`\*\*, which defer to
  the theme's `minimum`/`maximum` scale colors. Real (`2018SU09 Blog Demo → Germany → columnChart`):
  `linearGradient2 { min:'minColor', max:'maxColor', nullColoringStrategy:'asZero' }`. **[CORPUS]**
- A gradient can co-exist with a base `fill` (used as the categorical fallback). Real
  (`Competitive Marketing Analysis → Return on Investment → map → data.dataPoint`): \`fill =
  ThemeDataColor{ColorId:2} `**and**` fillRule = linearGradient2{minColor,maxColor}\`. **[CORPUS]**
- Also seen on `shapeMap` (`2020SU11 → Sales Summary`: `linearGradient3 #d6d07f→#70aa99→#437938`).

**(c) Field-bound — color driven directly by a measure/column** (\`solid.color.expr.Measure|
Aggregation|Column`, value-kind` fieldRef`/`solid:fieldRef\`). Used when a measure \*returns a hex
string\*. Observed in 5/52 files (§13 `FieldClr`). **[CORPUS]**

**(d) Data bars — in-cell bars, not a text color.** `columnFormatting.dataBars` =
`{ positiveColor, negativeColor, axisColor, reverseDirection }` on a table/matrix column
(`2018SU09 Blog Demo → Overview → pivotTable`, 24 instances). The Decomposition Tree exposes its own
`dataBars.dataBarColor` as a `fillRule/gradient` (\`Supply Chain Sample → Exploratory Analysis →
decompositionTreeVisual\`). **[CORPUS]**

⇄ **Tableau:** (a) rule-based ≈ a **discrete calculated field on Color** (or Color legend "Edit
Colors" with stepped rules); (b) gradient ≈ a **continuous color legend** (sequential = 2-stop,
diverging = 3-stop with center); `minColor`/`maxColor` sentinels ≈ Tableau letting the palette
endpoints drive; (c) field-bound ≈ **Color on a computed field**; (d) data bars ≈ Tableau \*\*in-cell
bar charts\*\* / a bar Mark in a text table (loose). Tableau's `nullColoringStrategy` ≈ "Special
Values" placement on a continuous legend (partial).

### 3.4 Sentiment & KPI coloring

- **KPI visual**: `status = { goodColor, neutralColor, badColor, direction }` (`data.status`) — a
  direct consumer of the theme's `good/neutral/bad`. `indicator`/`goals` carry the value/target
  formatting. **[CORPUS]** (`kpi` in 10/52 — §13.)
- **Waterfall**: `sentimentColors = { increaseFill, decreaseFill, totalFill }` (`data.sentimentColors`).
  **[CORPUS]**
- **Gauge**: `calloutValue.color` + `dataPoint.fill`/`target`. **[CORPUS]** (`gauge` in 7/52.)

⇄ **Tableau:** **GAP** — Tableau has no native KPI/gauge/waterfall sentiment. Recreate via calculated
fields + a manual red/yellow/green palette, or shape marks. Flag as a rebuild, not a property map.

### 3.5 Alpha / transparency

`transparency` is an **integer 0–100** where **0 = fully opaque, 100 = fully transparent** (note the
inversion vs. "opacity"). Appears on `background`, `visualHeader`, `dropShadow`, shape `fill`/`line`,
`outspace`, `page.background`. Real: `page.background.transparency = 26`; theme default
`page.background.transparency = 100` (i.e., transparent canvas by default). **[CORPUS]** Color
`Percent` (tint/shade) is a *separate* concept from `transparency` and only applies to
`ThemeDataColor` refs (§1.4).

⇄ **Tableau:** `transparency` ≈ Tableau's **Opacity** slider on the Color Marks card / shading —
**but inverted** (PBI 100 = invisible ≈ Tableau opacity 0%). The tool must convert \`opacity =
100 − transparency\`.

---

## 4. Typography

### 4.1 Font families observed **[CORPUS]**

- Default UI font: **`Segoe UI`** (and `Segoe UI Semibold`, `Segoe UI Bold`). Base-theme display
  font: **`DIN`** (used for `callout`/`title` in `CY19SU12`).
- `fontFamily` in visual objects is a **CSS font stack**, e.g.
  `"'Segoe UI Bold', wf_segoe-ui_bold, helvetica, arial, sans-serif"`. The first quoted name is the
  intended face; the rest are web fallbacks. \*\*Bold/italic are frequently encoded by \*font face
  name\*\*\* (`Segoe UI Bold`, `Segoe UI Semibold`) rather than a separate weight property.
- Custom themes set a global face via `visualStyles."*"."*"."*".fontFamily` (e.g. powerbi.tips →
  `"Segoe UI"`), and `titleFontFamily`/`titleFontSize` for titles.

### 4.2 Font size — three serializations (recap)

| Where | Property | Serialization | Example |
| --- | --- | --- | --- |
| Visual object (encoding A) | `fontSize` | expr-wrapped, string **or** int | `{"expr":{"Literal":{"Value":"11"}}}` |
| Theme `visualStyles` (B) | `fontSize` / `titleFontSize` | raw **string** | `"fontSize":"10"` |
| Theme `textClasses` | `fontSize` | raw **int** (points) | `"fontSize": 12` |
| Rich text (C) | `fontSize` | string with **`pt`** | `"fontSize":"12pt"` |
| Tables/slicers | `textSize` | (same as fontSize) int/string | `grid.textSize`, `items.textSize` |

The tool must normalize `fontSize`↔`textSize` and strip/append the `pt` suffix per target. **[CORPUS]**

### 4.3 Weight / style / decoration

- Rich text `textStyle` carries CSS-like `fontWeight`, `fontStyle`, `textDecoration` (underline),
  and `color` (plain hex). Real (`2020SU11 → Introduction → textbox`): \`{"fontSize":"12pt",
  "color":"#ffffff"}\`. **[CORPUS]**
- Elsewhere bold is via the **bold font face** (§4.1). There is no universal `bold:true` in the
  observed visual objects.

### 4.4 Per-visual title/label/axis font overrides

Every axis/label/legend/title object exposes its own font trio. Representative properties
**[CORPUS]**: `categoryAxis.fontFamily/fontSize/labelColor/titleFontFamily/titleFontSize/titleColor`;
`valueAxis.*` (same); `labels.fontFamily/fontSize/color`; `legend.fontSize/labelColor`;
`vc::title.fontFamily/fontSize/fontColor/heading` (`heading` = named level, e.g. `Heading2`).

⇄ **Tableau:** all of the above ≈ Tableau **Format > Font** for the corresponding area
(Worksheet/Rows/Columns/Marks-labels/Title/Legend). Tableau bold/italic are explicit toggles →
map to the Power BI **bold font face** or rich-text `fontWeight`. Font *stacks* have no Tableau
analog (Tableau stores a single face) — take the first quoted name.

---

## 5. Per-visual-type formatting matrix

Below is the **as-observed** object inventory per visual type present in the corpus (DATA =
`singleVisual.objects`, CONTAINER = `singleVisual.vcObjects`). Property lists are the union seen in
the corpus for that type; they are **not exhaustive of the product**, but they are exhaustive of the
corpus. The companion JSON `byVisualType` carries the same data machine-readably. **[CORPUS]**

Universal CONTAINER objects (available on almost every type): `background {show,color,transparency}`,
`border {show,color,radius,width}`, \`title {show,text,fontColor,fontFamily,fontSize,alignment,
background,heading,titleWrap}`,` dropShadow {show,preset,position,angle,shadowBlur,shadowDistance,
shadowSpread,color,transparency}`,` visualHeader {show + 16 showX toggles + background/border/
foreground/transparency}`,` visualHeaderTooltip {type,text,section,titleFontColor}`,` visualTooltip
{show,type,section,background,titleFontColor,valueFontColor}`,` stylePreset {name}`,` general
{altText,keepLayerOrder}\`.

### 5.1 Cards & KPI

| Type | DATA objects → key properties |
| --- | --- |
| `card` | `labels {color,fontFamily,fontSize,labelDisplayUnits,labelPrecision}`, `categoryLabels {show,color,fontFamily,fontSize}`, `wordWrap {show}` |
| `multiRowCard` | `card {barColor,barShow,barWeight,cardPadding}`, `cardTitle {color,fontFamily,fontSize}`, `categoryLabels {…}`, `dataLabels {color,fontFamily,fontSize}` |
| `cardVisual` (new card) | `layout {orientation}` (new-card visual; most styling via theme `visualStyles`) |
| `kpi` | `indicator {fontFamily,fontSize,horizontalAlignment,verticalAlignment,indicatorDisplayUnits,indicatorPrecision}`, `goals {goalText,goalFontFamily,fontSize,distanceLabel}`, `status {goodColor,neutralColor,badColor,direction}`, `trendline {show}` |
| `gauge` | `axis {min,max,target}`, `dataPoint {fill,target}`, `calloutValue {show,color,labelDisplayUnits}`, `labels {show,color,fontFamily,fontSize,labelPrecision}`, `target {show,fontFamily,fontSize,labelPrecision}` |

⇄ **Tableau:** `card`/`multiRowCard` ≈ a **BAN** (big-ass-number) text worksheet; `card.barColor` ≈
a divider/border (partial). `kpi`/`gauge` = **GAP** (rebuild).

### 5.2 Tables & matrix

| Type | DATA objects |
| --- | --- |
| `pivotTable` (matrix) | `values`, `columnHeaders`, `rowHeaders`, `columnFormatting`, `columnTotal`, `rowTotal`, `subTotals`, `total`, `grid`, `columnWidth`, `general` |
| `tableEx` (new table) | `values`, `columnHeaders`, `columnFormatting`, `total`, `grid`, `columnWidth` |
| `table` (legacy) | `columnHeaders {wordWrap}`, `general {columnWidth,totals}` |

Representative property sets **[CORPUS]**:
- \`values {backColor, backColorPrimary, backColorSecondary, bandedRowHeaders, fontColor,
  fontColorPrimary, fontColorSecondary, fontFamily, fontSize, outline, outlineStyle, valuesOnRow,
  wordWrap} `—` backColorPrimary`/`Secondary `= **row banding**;` outline\` = cell borders.
- \`columnHeaders {alignment, autoSizeColumnWidth, backColor, columnAdjustment, fontColor,
  fontFamily, fontSize, outline, outlineStyle, wordWrap}\`.
- \`columnFormatting {alignment, dataBars, fontColor, labelDisplayUnits, labelPrecision, styleHeader,
  styleSubtotals, styleValues}\` — the **conditional-formatting + data-bars** anchor (per-measure
  `metadata` selector).
- \`grid {gridHorizontal, gridHorizontalColor, gridHorizontalWeight, gridVertical, gridVerticalColor,
  gridVerticalWeight, outlineColor, outlineWeight, rowPadding, imageHeight, textSize}\`.
- `subTotals`/`total`/`columnTotal`/\`rowTotal {applyToHeaders, backColor, fontColor, fontSize,
  rowSubtotals, columnSubtotals, levelSubtotalEnabled, perRowLevel}\`.

⇄ **Tableau:** matrix/table ≈ a Tableau **text table (crosstab)**. `values.backColorPrimary/Secondary`
≈ **row banding** (Format > Shading, banded); `grid.*` ≈ **Format > Borders/Lines**; `columnHeaders`
≈ field-label/header formatting; `columnFormatting.dataBars` ≈ in-cell bars; `subTotals`/`total` ≈
**Analysis > Totals** formatting. Per-cell CF ≈ **color encoding on a text-table Marks card**.

### 5.3 Cartesian charts (bar / column / line / area / combo / scatter)

Shared DATA objects: `categoryAxis`, `valueAxis`, `dataPoint`, `labels`, `legend`, `general`
(`responsive`). **[CORPUS]**

- \`categoryAxis {show, axisType, concatenateLabels, fontFamily, fontSize, labelColor, innerPadding,
  maxMarginFactor, preferredCategoryWidth, showAxisTitle, titleColor, titleFontFamily, titleFontSize,
  gridlineColor, gridlineStyle, gridlineThickness, start, treatNullsAsZero}\`.
- \`valueAxis {show, showAxisTitle, fontFamily, fontSize, labelColor, labelDisplayUnits, titleText,
  titleColor, titleFontSize, gridlineShow, gridlineColor, gridlineStyle, gridlineThickness, start,
  invertAxis, logAxisScale, alignZeros, switchAxisPosition, axisStyle}\`.
- `dataPoint {fill, fillRule, defaultCategoryColor, showAllDataPoints}` (colors + CF — §3).
- `labels` (data labels) \`{show, color, fontFamily, fontSize, labelDisplayUnits, labelPrecision,
  labelPosition, labelOrientation, labelOverflow, enableBackground, showAll, showSeries}\`.
- `legend {show, position, showTitle, titleText, fontSize, labelColor, showGradientLegend}`.
- Line/area extras: \`lineStyles {lineStyle, strokeWidth, showMarker, markerShape, stepped,
  shadeArea, strokeLineJoin}`;` seriesLabels {show}\`.
- Analytics overlays: `y1AxisReferenceLine`/\`xAxisReferenceLine {show, displayName, lineColor,
  position, style}`;` anomalyDetection {…markerColor, confidenceBandColor…}`;` forecast {show,
  lineColor, transform}`;` zoom {show, categoryMin/Max, valueMin/Max}\`.
- Scatter extras: `bubbles {bubbleSize, markerShape, preventOverflow, showSeries}`, \`fillPoint
  {style}`,` colorBorder {show}`,` categoryLabels {show}\`.
- Small multiples: `smallMultiplesLayout {gridLineColor, gridLineType}` (2/52 — §13).
- Ribbon chart: `ribbonChart` DATA = `dataPoint {fill}`; CONTAINER adds `stylePreset {name}` (a named
  ribbon style, e.g. `minimal`) alongside the usual \`background/dropShadow/title/visualHeader/
  visualTooltip\`. Renders as a stacked column whose ribbons re-rank between ordinal periods; colors
  resolve exactly like a stacked column. Present in the monthly blog-demo builds. **[CORPUS]**

`gridlineStyle` values observed: `dotted` (theme default via `visualStyles`), `solid`, `dashed`
[SCHEMA]. Axis titles default **on** in `CY19SU12` (`showAxisTitle:true`). **[CORPUS]**

⇄ **Tableau:** `categoryAxis`/`valueAxis` ≈ **Format axis / Edit axis** + gridlines via \*\*Format >
Lines\*\*; `dataPoint.fill` ≈ Color on Marks; `labels` ≈ **Label** on Marks; `legend` ≈ color/size
legend cards; `lineStyles.strokeWidth` ≈ line **Size**; `y1AxisReferenceLine` ≈ \*\*Analytics >
Reference Line\*\*; `forecast`/`anomalyDetection` ≈ Tableau **Forecast** / no anomaly equivalent (GAP).

### 5.4 Part-to-whole (pie / donut / treemap / waterfall / funnel)

| Type | DATA objects |
| --- | --- |
| `pieChart` | `labels {show,fontFamily,fontSize,labelStyle}`, `legend {…}` |
| `donutChart` | `dataPoint {fill}`, `labels {show,color,position,labelStyle,…}`, `legend {…}`, `slices {innerRadiusRatio}` |
| `treemap` | `dataPoint {fill}`, `labels {…}`, `categoryLabels {fontFamily,fontSize}`, `legend {…}` |
| `waterfallChart` | `sentimentColors {increaseFill,decreaseFill,totalFill}`, `categoryAxis`, `valueAxis`, `labels`, `legend` |
| `funnel` | `dataPoint {fill,showAllDataPoints}`, `labels {show,color,fontSize,funnelLabelStyle,labelDisplayUnits}`, `percentBarLabel {show,color,fontFamily,fontSize}`, `categoryAxis {show,color,fontSize}` |

⇄ **Tableau:** pie/donut ≈ **Pie mark** (donut = dual-axis hack); `slices.innerRadiusRatio` has no
clean Tableau analog; treemap ≈ Tableau **treemap**; waterfall = **GAP** (Gantt-bar rebuild).

### 5.5 Maps

| Type | DATA objects |
| --- | --- |
| `map` (bubble) | `dataPoint {fill,fillRule}`, `bubbles {bubbleSize}`, `legend`, `mapControls {autoZoom,centerLatitude,centerLongitude,zoomLevel,showZoomButtons,showLassoButton}`, `mapStyles {mapTheme}` |
| `filledMap` (choropleth) | `dataPoint {fill,fillRule,showAllDataPoints}`, `labels {show}`, `legend`, `mapStyles {mapTheme}` |
| `shapeMap` | `dataPoint {fillRule}`, `defaultColors {defaultColor,borderColor,borderThickness}`, `shape {map,projectionEnum}`, `zoom {manualZoom}` |
| `azureMap` | `bubbleLayer {clusteredBubbleRadius,clusteringEnabled}`, `mapControls {defaultStyle,zoom,center…,showNavigationControls,showStylePicker}`, `categoryLabels {show}`, `legend` |
| `esriVisual` | `mapObject {value}` (ArcGIS; opaque blob) |

Maps are the **most common non-trivial type** (19/52 — §13). `dataPoint.fillRule` on maps is the
choropleth **color scale** (§3.3). **[CORPUS]**

⇄ **Tableau:** `map`/`filledMap` ≈ Tableau **symbol/filled maps**; `mapStyles.mapTheme`/
`mapControls` ≈ **Map > Background Maps / Map Options**; `bubbles.bubbleSize` ≈ **Size** on Marks;
`shapeMap`/`azureMap`/`esriVisual` ≈ Tableau custom-geocoding/ArcGIS (partial → GAP).

### 5.6 Slicers

`slicer` DATA objects **[CORPUS]**: `data {mode, startDate, endDate, numericStart, numericEnd}`,
`header {show, fontColor, fontFamily, textSize, showRestatement}`, \`items {background, fontColor,
textSize}`,` slider {show, color} `(range),` numericInputStyle {background, fontColor, textSize}\`,
`date {background, textSize}`, `selection {selectAllCheckboxEnabled}`, \`general {responsive,
selfFilterEnabled, filter}`. CONTAINER:` background, border, title, visualHeader, visualHeaderTooltip\`.

⇄ **Tableau:** slicer ≈ a **Filter card** (or parameter control). `data.mode` (list/dropdown/
between) ≈ filter card **type**; `header` ≈ filter card title; `items` ≈ filter card items; `slider`
≈ **range** filter. Tableau lacks a direct `selectAllCheckboxEnabled` toggle (partial).

### 5.7 AI visuals (Decomposition Tree / Key Influencers / Q&A)

All three = **GAP** in Tableau; documented here so the tool can *detect and report* them rather than
attempt a map. **[CORPUS]** (18/52 files carry at least one AI visual.)

- `decompositionTreeVisual`: \`tree {accentColor, connectorDefaultColor, connectorType, density,
  barsPerLevel, defaultClickAction}`,` dataBars {dataBarColor, positiveBarColor, negativeBarColor,
  dataBarBackgroundColor, dataBarWidthPercent}`,` levelHeader {levelHeaderBackgroundColor,
  levelTitleFontColor/Family/Size, levelSubtitleFontColor/Size, showSubtitles}`,` categoryLabels
  {categoryLabelFontColor}`,` dataLabels {dataLabelFontColor,dataLabelFontFamily}`,` analysis
  {aiEnabled,aiMode}`,` insights {isAINode}\`.
- `keyDriversVisual` (Key Influencers): \`keyInfluencersVisual {canvasColor, primaryColor,
  primaryFontColor, secondaryColor, secondaryFontColor}`,` keyDrivers {…}`,` keyDriversDrillVisual
  {defaultColor, referenceLineColor}\`.
- `qnaVisual`: `inputBox {background, acceptedColor, errorColor}`, `suggestions {cardBackground}`,
  `hiddenProperties {savedUtterance}`.

### 5.8 Shapes, buttons, images, text boxes

| Type | DATA objects |
| --- | --- |
| `actionButton` | `fill {fillColor,image,show,transparency}`, `icon {shapeType,lineColor,lineWeight,lineTransparency,padding,*Margin,horizontalAlignment,verticalAlignment}`, `outline {show,lineColor,weight,roundEdge,transparency}`, `text {show,text,fontColor,fontFamily,fontSize,*Margin,alignment}`, `shape {roundEdge}` |
| `basicShape` | `fill {fillColor,show,transparency}`, `line {lineColor,weight,transparency}`, `general {shapeType}`, `rotation {angle}` |
| `shape` | `fill {fillColor,show}`, `outline {show}`, `shape {tileShape}`, `rotation {shapeAngle}` |
| `image` | `general {imageUrl}`, `image {backgroundEnabled}`, `imageScaling {imageScalingType}` |
| `textbox` | `general {paragraphs}` (rich text — §4.3), `values {expr, formatString}` (bound dynamic text) |

`actionButton` also uses CONTAINER `visualLink` (navigation) — see §6.4. Buttons/shapes are the
biggest users of the `{id}` selector (32,314 instances). **[CORPUS]**

⇄ **Tableau:** shapes/buttons ≈ Tableau **dashboard objects** (Buttons, Text, Image, floating
shapes). `fill`/`line`/`outline` ≈ item background/border; `text` ≈ text-object font; `visualLink`
≈ **Dashboard Actions** (Navigate/URL); `image.imageScalingType` ≈ image **Fit** options. Note
Tableau navigation actions live at the **dashboard** level, not on the object (partial).

### 5.9 Imported custom visuals (opaque configs)

19/52 files import at least one **custom visual** (an AppSource / org visual packaged under
`Report/CustomVisuals/<id>/`). Their `visualType` is a GUID-suffixed token and their formatting is a
**private, self-defined object bag** — not the standard object catalog. Observed in the corpus
**[CORPUS]**: `CardBrowser8D7CFFDA…`, `ClusterMap1652434605854`, `FlowVisual_C29F1DCC…`,
`ImageGrid_FC5183B9…`, `PBI_CV_885EF3C3…`, `PBI_CV_EB3A4088…`, `PowerApps_PBI_CV_C29F1DCC…`,
`simpleImageEBC4593F…`, `textFilter25A4896A…`. Two custom-visual object families surfaced in the flat
inventory — `data::presentation` and `data::circle` — but the property set is visual-specific and
**not portable**.

⇄ **Tableau:** **GAP** — there is no reliable mapping. The migration tool should **detect** a custom
visual (non-standard `visualType` + a `CustomVisuals/` entry), record it, and surface it as a manual
rebuild rather than attempt to translate its formatting.

---

## 6. Page / canvas formatting

Page formatting lives in `section.config.objects` (legacy) / `page.json` (PBIR). Observed objects
**[CORPUS]**:

| Object | Properties | Example value |
| --- | --- | --- |
| `background` | `color, image, transparency` | `color #EDEDED`, `transparency 26` |
| `outspace` (wallpaper) | `color, image, transparency` | `color #cbcbcb`, `transparency 12` |
| `displayArea` | `verticalAlignment` | `Middle` |
| `filterCard` | `border` | `true` |
| `outspacePane` (filter pane) | `width` | `222` |

### 6.1 `background` vs. `outspace` (a subtle but important distinction)

- **`background`** = the **page canvas** fill (the area the visuals sit on).
- **`outspace`** = the **wallpaper**, i.e. the area *outside* the page/canvas (the "gray" margin
  when the canvas is smaller than the window). **[CORPUS]** Theme default: \`background.transparency
  = 100 `(transparent canvas),` outspace.color = #FFFFFF\`. 37/52 files set a page background; 26/52
  set wallpaper (§13).

### 6.2 Image fills

`background.image` / `outspace.image` reference a packaged resource:
`{ "name": {expr Literal}, "url": {expr ResourcePackageItem}, "scaling": … }` — the image bytes live
in `RegisteredResources/`. Value-kind `expr:ResourcePackageItem` (392) / `image` (223). **[CORPUS]**

### 6.3 Page size / type **[PBIR]/[SCHEMA]**

Legacy `section` carries `width`/`height`/`displayOption` on the section object; the attachment's
PBIR `page.json` shows `displayOption:"FitToPage"`, `height:720`, `width:1280`. Standard canvas is
**1280×720 (16:9)**; `displayOption` ∈ `FitToPage | FitToWidth | ActualSize` [SCHEMA].

### 6.4 Container-level navigation & tooltip (chrome that spans page/visual)

- \`vc::visualLink {type: Bookmark|WebUrl|PageNavigation|Drillthrough, bookmark, webUrl,
  navigationSection, drillthroughSection, tooltip, enabledTooltip, disabledTooltip, show}\`. **[CORPUS]**
- \`vc::visualTooltip {show, type: Default|Canvas, section, background, titleFontColor,
  valueFontColor} `—` type:Canvas `+` section\` = a **report-page tooltip**. **[CORPUS]**

⇄ **Tableau:** page `background`/`outspace` ≈ **dashboard background** color/image (Tableau has no
true "outside canvas" fill — partial); `displayArea.verticalAlignment` ≈ dashboard vertical
alignment; `visualLink` ≈ **Dashboard Actions**; `visualTooltip type:Canvas` ≈ **Viz in Tooltip**;
`filterCard`/`outspacePane` ≈ the **filter-cards container**.

---

## 7. Number formatting & display units

Where visual formatting meets number formatting **[CORPUS]**:

| Property | Where | Type | Meaning |
| --- | --- | --- | --- |
| `formatString` | `textbox.values`, model measure | string | .NET/OData format string, e.g. `"0.0%"`, `"\\$#,0"` |
| `labelDisplayUnits` | `labels`, `columnFormatting`, `calloutValue` | int enum | scaling/abbreviation |
| `indicatorDisplayUnits` | `kpi.indicator` | int enum | KPI value scaling |
| `labelPrecision` | `labels`, `columnFormatting`, `target`, `gauge.labels` | int | decimal places |

**`labelDisplayUnits` enum [INFERRED]:** `0 = None (no abbreviation)`, `1000 = Thousands (K)`,
`1000000 = Millions (M)`, `1000000000 = Billions (bn)`, `1000000000000 = Trillions (T)`. The literal
value is the divisor. Verify (some builds use `-1`/`0` for "Auto"). `labelPrecision` unset ⇒ **auto**.

⇄ **Tableau:** `formatString` ≈ Tableau **Format > Numbers** (custom); `labelDisplayUnits` ≈
Tableau's **Display Units** (Thousands/Millions…); `labelPrecision` ≈ **Decimal places**. .NET format
strings and Tableau's custom formats overlap heavily but are **not identical** — translate, don't
copy (e.g. Tableau `▲`/color-in-format tricks have no .NET analog).

---

## 8. Precedence model (the resolution order)

When base theme, custom theme, `visualStyles`, a per-visual property, and a selector-scoped instance
all touch the **same** property, Power BI resolves lowest→highest:

| Rank | Layer | Where | Beats |  |  |
| ---: | --- | --- | --- | --- | --- |
| 1 | Base theme default | `BaseThemes/<name>.json` structural keys + `textClasses` | — |  |  |
| 2 | Applied custom theme | `themeCollection.customTheme` | 1 |  |  |
| 3 | Theme `visualStyles` | \\`theme.visualStyles[type | '\\*'][sel | '\\*'][object]\\` | 1–2 |
| 4 | Per-visual explicit property | `singleVisual.objects` / `vcObjects` (no selector) | 1–3 |  |  |
| 5 | Per-instance selector | object instance with `metadata`/`dataScope`/`dataWildcard` | 1–4 (for its scope) |  |  |

**Worked example (bar fill).** Theme `dataColors[0]=#118DFF` (ranks 1–2) → a per-visual
`dataPoint.fill = ThemeDataColor{ColorId:3}` (rank 4) recolors all bars to palette slot 3 → a
selector-scoped `dataPoint.fill` with `{dataScope: "East"}` (rank 5) overrides just the "East" bar →
a `fillRule` gradient (rank 5, CF) overrides by value. **[CORPUS-derived]**

**Critical nuances for the migration tool:**
1. **Literal vs. ThemeDataColor resolution.** A rank-4/5 value that is a **literal hex** is frozen
   and ignores the theme; a **ThemeDataColor** ref is resolved against the *winning* palette (after
   ranks 1–2). Migrating Tableau's literal colors therefore produces theme-independent output unless
   deliberately re-bound (§3.2).
2. **`wildcard vs. specific`** in `visualStyles`: `[type][*]` beats `["*"][*]`; a specific
   `objectName` beats `"*"`. Emit the most specific selector you can.
3. **Selector scope**: a selector-scoped instance only wins **within its scope**; the no-selector
   instance remains the default for everything else in the visual.

⇄ **Tableau:** Tableau's precedence is roughly \*\*Workbook format defaults → Worksheet format →
Field/Marks-card override → per-cell/Edit-Colors override\*\*. Map theme (1–3) → Workbook defaults;
per-visual (4) → worksheet/Marks; selector (5) → per-field/per-member overrides. There is \*\*no
Tableau analog for the theme-following (`ThemeDataColor`) vs. frozen (literal) distinction\*\* — call
it out as a migration decision point.

---

## 9. Accessibility

- **`altText`** (`vc::general.altText`) — screen-reader alt text on a visual. Real value observed:
  `"GitHub"`. **[CORPUS]** ⇄ Tableau: worksheet/dashboard-object **Caption**/title (partial; Tableau
  alt-text support is limited).
- **High-contrast mode** is a **Desktop/OS render-time** behavior, not serialized in `Report/Layout`
  — no corpus artifact. **[INFERRED/SCHEMA]** The tool cannot migrate it from/to Tableau (neither
  side stores it); note as out-of-scope.
- **Color-scale accessibility.** Several files use **red→yellow→green** 3-stop gradients (e.g.
  `#e82c3a → #f2c811 → #63c345`, `2018SU09 → Overview → scatterChart`). These are \*\*not
  colorblind-safe\*\*; flag when carrying such a scale across (Tableau's default "Red-Green Diverging"
  has the same issue — preserve the author's intent but surface a warning). **[CORPUS]**
- **Reliance on color alone.** KPI `status` and waterfall `sentimentColors` encode meaning purely by
  color (good/neutral/bad). When rebuilding these in Tableau (which lacks the native visuals),
  pair color with shape/label to stay accessible. **[CORPUS]**
- **Theme sentiment/divergent keys** (`good/neutral/bad`, `minimum/center/maximum/null`) centralize
  these choices — migrating them once at the theme level is safer than per-visual literal colors.

---

## 10. Corpus findings & recurring patterns

1. **Report format is uniformly legacy.** All 52 `.pbix` = `Report/Layout` (UTF‑16, stringified
   `config`). Enhanced **PBIR** appears only in the `.pbip` attachment. A migration tool targeting
   *authoring* should emit **PBIR** (the current Fabric format) but must be able to *read* legacy.
2. **A base theme is always present; custom themes in 25/52.** Even "un-themed" older files resolve a
   factory `CY…SU…` base theme. Custom themes are often **partial** (colors only). The two oldest base
   themes (`CY17SU12`, `CY18SU07`) even ship **0** `dataColors`, deferring the palette to a layered
   built-in/custom theme.
3. **`visualHeader.show=false` is a near-universal pattern** (34/52 files, and set globally by
   custom themes). Authors routinely hide the on-visual icon strip. Migrated visuals should default
   the header hidden to match the common look.
4. **Gridlines default to `dotted`** via `visualStyles."*"` in factory themes; axis titles default
   **on**. Don't assume solid gridlines.
5. **`dropShadow` is frequently present but `show:false`** — i.e., configured-but-disabled. Read the
   `show` flag; don't infer a shadow from the object's mere presence.
6. **Backgrounds are often transparent** (`transparency:100`) at the theme level; explicit page
   backgrounds (37/52) override this.
7. **Three color-value populations dominate**: theme refs (12,052) > frozen hex (10,077) > everything
   else. Expect to handle both theme-following and literal colors on every visual.
8. **Conditional formatting clusters on tables/matrix and maps.** Rule-based CF (23/52), gradients
   (34/52), table CF (33/52), data bars (32/52). Cards/KPI/gauge are comparatively static.
9. **480-color custom palettes** (powerbi.tips) show tools generating huge `dataColors[]` arrays —
   handle arbitrarily long palettes; don't cap at the \~8 the UI shows (§12).
10. **Rich text is near-universal (49/52)** — almost every report has at least one `textbox` with
    `paragraphs` (titles/annotations); the 3 exceptions are utility/demo files. The `pt`-suffixed CSS
    encoding (§4.2) must be handled.
11. **Custom visuals appear in 19/52 files.** A migration tool must detect a non-standard
    `visualType` + `CustomVisuals/` entry and flag it as a manual rebuild (§5.9) — its formatting is
    a private object bag with no standard catalog.

---

## 11. Tableau → Power BI mapping table

`✓` = clean mapping · `~` = partial/lossy · **GAP** = no faithful equivalent (detect & report).

| Tableau construct | Power BI target (path) | Fit | Notes |
| --- | --- | --- | --- |
| Workbook > Format (fonts/lines defaults) | theme `textClasses` + `visualStyles` | \\~ | PBI adds a per-visual-*type* tier Tableau lacks. |
| Discrete color palette (`Preferences.tps`) | theme `dataColors[]` (ordered) | ✓ | Preserve order; wrap semantics match. |
| Continuous (sequential) palette | theme `minimum`/`maximum` + `dataPoint.fillRule.linearGradient2` | ✓ | 2-stop scale. |
| Diverging palette (with center) | `minimum`/`center`/`maximum` + `linearGradient3` | ✓ | 3-stop; keep center. |
| Color on Marks (discrete member) | `dataPoint.fill` + `{dataScope}` selector | ✓ | Literal vs. ThemeDataColor decision (§3.2). |
| Color on Marks (computed field) | `dataPoint.fill` field-bound / `Conditional.Cases` | \\~ | Rule vs. field-return-hex. |
| Color legend "Edit Colors" stepped rules | `Conditional.Cases[]` | ✓ | Map thresholds → `Comparison`/`And`/`Or`. |
| Opacity slider | `transparency` (**inverted**: `100−opacity`) | ✓ | Must invert. |
| Label on Marks | visual `labels` object | ✓ | Fonts/units/precision map. |
| Tooltip (Worksheet) / Viz-in-Tooltip | `vc::visualTooltip` (`type:Default`/`Canvas`+`section`) | ✓ | Canvas tooltip = report-page tooltip. |
| Size on Marks (bubbles) | `bubbles.bubbleSize` / map `bubbles` | ✓ |  |
| Shape on Marks | `fillPoint`/`lineStyles.markerShape` / shapeMap | \\~ | Custom shapes lossy. |
| Axis (Format/Edit axis) | `categoryAxis`/`valueAxis` | ✓ | Titles, ranges, log, units. |
| Gridlines / zero lines (Format > Lines) | `*.gridlineColor/Style/Thickness`, `gridlineShow` | ✓ | Default `dotted` in PBI. |
| Reference line (Analytics) | `y1AxisReferenceLine`/`xAxisReferenceLine` | ✓ |  |
| Forecast (Analytics) | `forecast` | \\~ | Params differ. |
| Trend line | chart `trendline`/reference line | \\~ |  |
| Text table (crosstab) | `pivotTable`/`tableEx` | ✓ | banding=`backColorPrimary/Secondary`. |
| Row banding / borders (Format > Shading/Borders) | `values.*` + `grid.*` | ✓ |  |
| In-cell bar chart | `columnFormatting.dataBars` | \\~ |  |
| Totals/Subtotals formatting | `total`/`subTotals`/`columnTotal`/`rowTotal` | ✓ |  |
| Filter card | `slicer` (`data.mode`,`header`,`items`,`slider`) | ✓ | `selectAll` toggle \\~ . |
| Parameter control | `slicer` / field parameter | \\~ | Different model concept. |
| Dashboard (sheet) | `section` (page) | ✓ | 1280×720 canvas. |
| Dashboard background color/image | `page.background` (+ `outspace` wallpaper) | \\~ | No true "outside canvas" fill in Tableau. |
| Dashboard object (Text) | `textbox` (rich text `paragraphs`) | ✓ | `pt` font encoding. |
| Dashboard object (Image) | `image` visual | ✓ | `imageScalingType` ≈ Fit. |
| Dashboard object (Button) | `actionButton` | ✓ |  |
| Dashboard Action (Navigate/URL/Filter) | `vc::visualLink` (Bookmark/WebUrl/PageNavigation/Drillthrough) | \\~ | PBI attaches to the object, not dashboard. |
| Floating-object drop shadow | `vc::dropShadow` | \\~ | PBI has presets/spread Tableau lacks. |
| Pie / treemap | `pieChart`/`donutChart` / `treemap` | ✓ | donut inner-radius no analog. |
| Maps (symbol/filled) | `map`/`filledMap` (`mapStyles`,`mapControls`) | \\~ | Basemap providers differ. |
| — (no Tableau native) → | `kpi`, `gauge`, `waterfallChart` sentiment | **GAP** | Rebuild via calcs/shapes. |
| — (no Tableau native) → | `decompositionTreeVisual`, `keyDriversVisual`, `qnaVisual` (AI) | **GAP** | Detect & report only. |
| — (no Tableau native) → | on-visual `visualHeader` icon strip | **GAP** | PBI chrome; nothing to map. |
| — (no Tableau native) → | theme `visualStyles[visualType]` per-type tier | **GAP** | No Tableau per-type default layer. |
| — (no Tableau native) → | `ThemeDataColor` theme-following semantics | **GAP** | Tableau colors are always literal. |

### 11.1 Explicit migration gaps (author-facing)

- **KPI / Gauge / Waterfall sentiment** — no native Tableau visual; rebuild.
- **AI visuals** (Decomposition Tree, Key Influencers, Q&A) — no equivalent; report and skip.
- **Visual header** icons — Power BI-only chrome.
- **Per-visual-type theme defaults** — collapse into Tableau workbook defaults + per-sheet formatting.
- **Theme-following colors** — decide per-color whether to freeze (literal) or bind (`ThemeDataColor`).
- **Wallpaper (`outspace`)** — Tableau has no distinct outside-canvas fill.

---

## 12. Open questions, undocumented behavior & edge cases

1. **`ComparisonKind` enum is inferred** (§3.3). The corpus only exercises `2` (≥). Confirm the full
   mapping (`0..4`) against a workbook that uses `<`, `=`, `≤` before the tool relies on it.
2. **480-color `dataColors[]`** (powerbi.tips themes) is **undocumented practice** — the theme
   *schema* imposes no length cap, but the UI surfaces only a handful. The tool must not assume 8/24.
3. **`fontSize` typing is inconsistent** — string in objects/`visualStyles`, int in `textClasses`,
   `pt`-suffixed in rich text; some objects use `textSize` instead. Normalize on read/write. **[CORPUS]**
4. **`minColor`/`maxColor` sentinel strings** in `fillRule` defer to the theme's scale colors — this
   is an undocumented indirection worth special-casing (don't treat them as literal hex). **[CORPUS]**
5. **`nullColoringStrategy.strategy`** only ever `'asZero'` in the corpus; other strategies
   (`asBlank`/a specific color) are plausible **[SCHEMA/INFERRED]** but unobserved.
6. **`vcObjects` storage location** (`singleVisual.vcObjects`, not `config.vcObjects`) is
   **reverse-engineered** from the corpus (the `.pbix` layout is not an officially documented format).
   Flagged as such. **[CORPUS]**
7. **PBIR object application is unverified.** The attachment established PBIR *structure* and object
   *names* (`objects`/`visualContainerObjects`) but carried **no applied formatting values**. All
   PBIR value examples would need a formatted PBIR sample to confirm. **[PBIR]**
8. **`Percent` (tint/shade) range** assumed `[-1,1]`; corpus only shows `[-0.25, 0]`. **[INFERRED]**
9. **`heading` levels** (`vc::title.heading = Heading2`) map to an accessibility heading level; the
   full enum (`Heading1..6`) is **[SCHEMA/INFERRED]**.
10. **`esriVisual.mapObject.value`** and custom-visual configs are **opaque blobs** — treat as
    non-portable. **[CORPUS]**

---

## 13. Coverage table (which of the 52 files exhibit which features)

Column legend: **CustTheme** custom theme applied · **ThemeRef** uses `ThemeDataColor` refs ·
**LitHex** frozen hex colors · **RuleCF** rule-based conditional formatting · **GradCF** gradient/
color-scale · **FieldClr** field-bound color · **TblCF** table/matrix CF · **DataBars** ·
**KPI** · **Gauge** · **Maps** · **AI** AI visuals · **RichTxt** rich-text box · **Shadow** drop
shadow present · **HdrHide** visual header hidden · **PgBg** page background · **Wall** wallpaper
(`outspace`) · **CustViz** imported custom visual · **SmMult** small multiples · **RefLine**
reference line. **[CORPUS]**

| File | CustTheme | ThemeRef | LitHex | RuleCF | GradCF | FieldClr | TblCF | DataBars | KPI | Gauge | Maps | AI | RichTxt | Shadow | HdrHide | PgBg | Wall | CustViz | SmMult | RefLine |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2018SU04 Blog Demo - April |  |  | ✓ |  | ✓ |  | ✓ | ✓ |  |  |  |  | ✓ |  |  | ✓ |  | ✓ |  |  |
| 2018SU05 Blog Demo - May |  |  | ✓ |  | ✓ |  | ✓ | ✓ |  |  |  |  | ✓ |  |  | ✓ |  | ✓ |  |  |
| 2018SU06 Blog Demo - June |  |  | ✓ |  | ✓ |  | ✓ | ✓ |  |  |  |  | ✓ |  |  | ✓ |  | ✓ |  |  |
| 2018SU07 Blog Demo - July |  |  | ✓ |  | ✓ |  | ✓ | ✓ |  |  |  |  | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |
| 2018SU08 Blog Demo - August |  |  | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |  |  | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |
| 2018SU09 Blog Demo - September - New Fo… | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |  |  | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |
| 2018SU09 Blog Demo - September | ✓ |  | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |  |  | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |
| 2018SU10 Blog Demo - October | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  |  |  |  | ✓ |  | ✓ | ✓ | ✓ |  |  |  |
| 2018SU10 Data Profiling Demo - October |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 2018SU10 Fuzzy Matching Demo - October |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 2018SU11 Blog Demo - November | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  |  |  |  | ✓ |  | ✓ | ✓ | ✓ |  |  |  |
| 2018SU12 Blog Demo - December | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  |  |  |  | ✓ |  | ✓ | ✓ | ✓ |  |  |  |
| 2019SU01 Blog Demo - February | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  |  |  | ✓ | ✓ |  | ✓ | ✓ | ✓ |  |  |  |
| 2019SU03 Blog Demo - March | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  |  | ✓ |  | ✓ |  | ✓ | ✓ | ✓ |  |  |  |
| 2019SU04 Blog Demo - April | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  |  |  | ✓ | ✓ |  | ✓ | ✓ | ✓ |  |  |  |
| 2019SU05 Blog Demo - May | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ | ✓ |  |  | ✓ | ✓ |  | ✓ | ✓ | ✓ |  |  |  |
| 2019SU06 Blog Demo - June | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  | ✓ | ✓ | ✓ |  |  |  |
| 2019SU07 Blog Demo - July | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ | ✓ |  |  | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |
| 2019SU08 Blog Demo - August |  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  |  | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |
| 2019SU09 Blog Demo - September |  | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |
| 2019SU10 Blog Demo - October |  | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |
| 2019SU11 Blog Demo - November |  | ✓ | ✓ |  | ✓ |  | ✓ | ✓ | ✓ |  |  | ✓ | ✓ |  | ✓ | ✓ |  | ✓ |  |  |
| 2019SU12 Blog Demo - December |  | ✓ | ✓ |  | ✓ |  | ✓ | ✓ | ✓ |  |  | ✓ | ✓ |  | ✓ | ✓ |  | ✓ |  |  |
| 2020SU09 Blog Demo - September | ✓ | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |  |  |
| 2020SU11 Blog Demo - November | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  |  | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |  |  |
| Adventure Works DW 2020 |  |  |  |  |  |  |  |  |  |  |  |  | ✓ |  |  |  |  |  |  |  |
| AdventureWorks Sales | ✓ | ✓ | ✓ |  |  |  | ✓ | ✓ |  |  | ✓ |  | ✓ | ✓ |  |  |  |  |  |  |
| Artificial Intelligence Sample | ✓ | ✓ | ✓ |  |  |  |  | ✓ |  |  |  | ✓ | ✓ | ✓ | ✓ | ✓ |  |  | ✓ |  |
| COVID Bakeoff | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  |  | ✓ | ✓ | ✓ |  | ✓ | ✓ |  | ✓ | ✓ | ✓ |
| COVID-19 US Tracking Sample |  | ✓ | ✓ |  | ✓ |  | ✓ | ✓ |  |  | ✓ |  | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |
| Competitive Marketing Analysis |  | ✓ | ✓ |  | ✓ |  |  |  |  | ✓ | ✓ |  | ✓ |  | ✓ | ✓ | ✓ |  |  |  |
| Corporate Spend | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |  |  | ✓ | ✓ | ✓ |  | ✓ | ✓ | ✓ |  |  |  |
| Customer Profitability Sample PBIX |  |  | ✓ |  | ✓ |  |  |  |  |  | ✓ |  | ✓ |  |  |  |  |  |  |  |
| Employee Hiring and History | ✓ | ✓ | ✓ | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |  | ✓ | ✓ | ✓ |  |  |  |
| Human Resources Sample PBIX (Sample Rep… |  |  | ✓ |  |  |  |  |  |  |  |  |  | ✓ |  |  |  |  |  |  |  |
| Human Resources Sample PBIX |  |  |  |  |  |  |  |  |  |  |  |  | ✓ |  |  |  |  |  |  |  |
| IT Spend Analysis Sample PBIX |  |  |  |  |  |  |  |  |  |  |  |  | ✓ |  |  |  |  |  |  |  |
| Life expectancy v202009 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Opportunity Analysis Sample PBIX |  |  |  |  |  |  |  |  |  |  |  |  | ✓ |  |  |  |  |  |  |  |
| PerformanceAnalyzerExportReport | ✓ | ✓ | ✓ | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |  |  |  |  |  |  |  |
| Procurement Analysis Sample PBIX |  |  |  |  |  |  |  |  |  |  | ✓ |  | ✓ |  |  |  |  |  |  |  |
| Regional Sales Sample | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Retail Analysis Sample PBIX |  |  |  |  |  |  |  |  |  |  | ✓ |  | ✓ |  |  |  |  |  |  |  |
| Revenue Opportunities (new-power-bi-ser… | ✓ | ✓ | ✓ |  | ✓ |  |  |  |  |  | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |  |  |
| Revenue Opportunities | ✓ | ✓ | ✓ |  | ✓ |  |  |  |  |  | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |  |  |
| Sales & Returns Sample v201912 | ✓ | ✓ | ✓ | ✓ |  |  | ✓ | ✓ |  |  |  | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ |  | ✓ |
| Sales and Marketing Sample PBIX |  |  | ✓ |  | ✓ |  |  |  |  |  | ✓ |  | ✓ |  |  |  |  |  |  |  |
| Store Sales (powerbi-service-samples) |  | ✓ | ✓ | ✓ |  |  | ✓ | ✓ |  | ✓ | ✓ |  | ✓ |  | ✓ | ✓ | ✓ |  |  | ✓ |
| Store Sales | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ | ✓ |  |  | ✓ |
| Supplier-Quality-Analysis-Sample-PBIX |  |  |  |  |  |  |  |  |  |  | ✓ |  | ✓ |  |  |  |  |  |  |  |
| Supply Chain Sample |  | ✓ | ✓ |  | ✓ |  |  | ✓ |  |  | ✓ | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ |  |  |
| customerfeedback |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |

**Feature totals:** CustTheme 25 · ThemeRef 33 · LitHex 42 · RuleCF 23 · GradCF 34 · FieldClr 5 · TblCF 33 · DataBars 32 · KPI 10 · Gauge 7 · Maps 19 · AI 18 · RichTxt 49 · Shadow 9 · HdrHide 34 · PgBg 37 · Wall 26 · CustViz 19 · SmMult 2 · RefLine 4 (of 52).

---

## Appendix A — Companion JSON (`powerbi-formatting-inventory.json`)

Load this for programmatic pattern-matching. Top-level keys:

| Key | Contents |
| --- | --- |
| `meta` | corpus counts, report-format tally, value-kind & selector census, PBIR notes |
| `valueGrammar` | the three encodings, literal grammar, color forms, `ComparisonKind` enum |
| `themeSchema` | every theme key → type/role/default/Tableau-equiv + full `textClasses` list |
| `precedenceModel` | the 5-rank order + resolution notes |
| `categoryToTableau` | property-category → Tableau area shortcuts |
| `objectIndex` | **113** `container::object` entries → per-property `{valueKinds, categories, selectors, settableAt, default, exampleValues, sourceExamples}` + object-level `tableauEquivalent` |
| `byVisualType` | per-visual-type DATA/CONTAINER object → property map |
| `themesObserved` | the 76 theme parts (colors capped for size) |
| `coverageMatrix` | per-file feature flags (source of §13) |

**Grounding contract:** aggregate facts in `objectIndex`/`byVisualType`/`themesObserved`/`meta`/
`coverageMatrix` are **corpus-extracted**; `valueGrammar`/`themeSchema`/`precedenceModel`/
`tableauEquivalent`/`default` fields are **curated** (flagged `"source":"curated"` or `"schema/docs"`).
Object paths for **PBIR** are schema-grounded (the corpus is legacy `Report/Layout`).

## Appendix B — Reproduction

Extraction scripts live in the session scratch workspace (not committed): `pass1_structure.py`
(format/visual census), `pass3_deep.py` (deep property flattener → `deep_inventory.json`),
`pass4_snippets.py` (citations), `pass5_matrix.py` (per-type matrix), `build_inventory.py`
(emits the companion JSON), `emit_tables.py` (coverage/theme tables). Re-run `build_inventory.py`
against `deep_inventory.json` to regenerate the JSON deliverable.


