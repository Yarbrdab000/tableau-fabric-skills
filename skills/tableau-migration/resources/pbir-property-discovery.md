# Discovering PBIR properties: ask Desktop, not a schema

**The problem is timing, not knowledge.** Every published description of PBIR formatting properties
lags Power BI Desktop by at least a month, so on the day a feature ships there is no authoritative
source for the JSON you have to emit. Measured 2026-08-24 on one machine:

| source | version | corresponds to |
|---|---|---|
| npm `@microsoft/powerbi-core-visual-schema` | `0.1.1` (latest published) | no release tag at all |
| PBIR `visualContainer` json-schema | `2.9.0` | May 2026 |
| [Report Theme JSON Schema][theme] | `reportThemeSchema-2.156` | July 2026 |
| **Power BI Desktop (installed)** | `2.157.627.0` | **August 2026** |

And the PBIR schema cannot close the gap even in principle. A visual's `objects` member resolves to
`DataViewObjectDefinitions`, which permits **arbitrary** object names and **arbitrary** properties:

> `powerbi-report-author validate` returns **0 errors** for a formatting property that does not
> exist, and would return 0 for one you invented.

Verified: a hand-written slicer carrying `data.relativeRange` validated exactly as clean as a
misspelling would have. **Validation can never tell you a property name is right.**

## The technique

Power BI Desktop knows every property in the release it shipped, and writes them as PBIR JSON when it
saves a `.pbip`. So use Desktop as the oracle:

```
py -3.11 scripts/pbir_property_probe.py snapshot <report-dir> baseline.json
```

Then, **in Power BI Desktop**: set the feature in the format pane and **File > Save** (applying the
format change is not enough — it must be saved to disk).

```
py -3.11 scripts/pbir_property_probe.py diff <report-dir> baseline.json
```

The diff prints the answer:

```
ADDED: 1 (1 formatting)
   centerValue.show                               v-page-Dashboard2567c7ec
        after : "true"
        path  : /visual/objects/centerValue[0]/properties/show/expr/Literal/Value
```

`<report-dir>` is the `.Report` folder, or anything above it. Add `--all` to keep the save-noise
(ids, `tabOrder`, `queryRef`) that is filtered out by default — occasionally the id *is* what you are
investigating.

**A changed value reports both sides**, which is how you learn an enum: `data.mode` going
`'Dropdown'` → `'Relative'` is the answer, not `'Relative'` alone.

## The offline half

When the property is old enough to be published, skip Desktop:

```
py -3.11 scripts/pbir_property_probe.py schema <reportThemeSchema-X.json> pivotTable "expand"
   columnHeaders.showExpandCollapseButtons                    +/- icons
   rowHeaders.showExpandCollapseButtons                       +/- icons
```

Download a release-tagged schema from [powerbi-desktop-samples][theme] once and keep it locally —
**this repo never downloads anything at runtime.**

**Prefer the theme schema over the npm catalog** that `powerbi-report-author formatting` reads.
Measured the same day, the theme schema knew `outerPadding`, `accentBar` and a real `centerValue`
that the npm catalog did not. Using the weaker oracle led to three features being wrongly written off
as unemittable.

## Reading the result honestly

Finding a property name is not proof the feature works. Two failure modes, both measured:

* **Present in the schema, still needs a render.** The property may exist and do nothing in the
  combination you emit.
* **Renders, but does not function.** A slicer written with `data.mode = 'Relative'` plus
  `relativeRange`/`relativePeriod`/`relativeDuration` renders a *perfect* relative-date control —
  `[Last] [6] [Months]` with a live resolved range — and **does not filter the page**. The applied
  selection lives elsewhere. Shipping on the strength of the control alone would have produced a
  slicer that looks right and silently does nothing.

So the order is: **discover the property → emit it → open it cold in Desktop → look at the render**,
and treat every step before the last as a hypothesis.

[theme]: https://github.com/microsoft/powerbi-desktop-samples/tree/main/Report%20Theme%20JSON%20Schema
