# Image oracle — Tier-2 vision pass for chart-type adjudication

This is the runbook for the **opt-in, agent-driven** image-oracle pass that refines the chart types
the deterministic Tier-1 engine ([`viz-rebuild.md`](viz-rebuild.md)) already emitted. It exists for
the views the XML cannot fully settle from the shelves alone — a Tableau **hack** that *renders* as
one chart but is *wired* as another (a dual-axis pie that reads as a donut, a running-total Gantt
that reads as a waterfall, an INDEX()/RANK() bump that reads as a ribbon), and non-standard
compositions you can only judge from pixels (a donut with a KPI floating in its hole, overlapping
floating containers, z-order).

> **One sentence.** The image confirms or corrects a visual's **chart type** from the small set the
> deterministic engine can faithfully bind — and **nothing else.** Fields stay exact-bound by Tier-1.

The image is the **Tableau-side** rendering — the source view that already exists. There is no
"after" Power BI picture to diff against; the deterministic engine already produced the report. The
picture is a *build aid*, read once.

---

## Why this is safe

The worst case of a vision misread is **right fields / wrong subtype = a warning-grade nudge**, never
a wrong or dangling binding, because the applier is mechanically constrained:

- it may only switch a visual to a type that is already in **that visual's candidate list**;
- it may never add, drop, or rebind a field — the query/`queryState` is asserted byte-identical
  across every type switch;
- with no image for a visual, it does nothing — the deterministic pick stands (warn-never-wrong).

The candidate list, the read-only field truth, the confidence, the `hack` flag, and the faithful
position all come from the additive **candidate record** the engine emits per main visual
(`migrate_twb_to_pbir(...)["candidate_records"]`). The oracle is a pure consumer of that record.

---

## The hard rules (do NOT break these)

1. **Adjudicate the chart TYPE only.** Pick a type **only** from this visual's `candidates` list
   (the deterministic pick is `candidates[0]`). Any other answer is **rejected**, not applied.
2. **NEVER touch fields.** `fields` is read-only truth. Do **not** add, drop, reorder, re-aggregate,
   or rebind anything. The applier refuses any change that would alter the query.
3. **No image ⇒ no opinion.** If a visual has no image, leave `chosen_type` null. Do not guess from
   the field names.
4. **Confirm = keep.** If the image matches the deterministic type, leave `chosen_type` null.
5. **Sheet swaps are invisible.** A single rendered frame shows one swap state only; never infer a
   parameter/sheet swap from pixels (those stay deterministic).
6. **Position is informational.** You may report a corrected position; the applier adopts only the
   numeric `x/y/z/width/height/tabOrder` keys and never a non-candidate type.

---

## Runbook

All steps are offline except the optional live image fetch in Step 2. Scripts are in
[`../scripts/image_oracle.py`](../scripts/image_oracle.py).

### 1. Migrate the workbook (Tier-1) and collect the candidate records

```python
from twb_to_pbir import migrate_twb_to_pbir
res = migrate_twb_to_pbir(twb_xml, dataset_name=model_name, report_name=workbook_name)
parts   = res["parts"]              # the PBIR definition (untouched by the oracle)
records = res["candidate_records"]  # the additive per-visual decision records
```

### 2. Build the adjudication bundle (resolves the image, offline-first)

```python
import image_oracle as io
bundle = io.build_oracle_bundle(
    records,
    twb_xml=twb_xml,            # embedded <thumbnail> PNGs (many real workbooks carry these)
    image_dir=provided_images, # optional: caller-supplied <page_display>.png / <worksheet>.png
    images_out=scratch_dir,    # matched thumbnails are written here for you to open
)
```

Image source priority, per visual: **caller-provided file → embedded thumbnail → none.** Embedded
thumbnails are keyed by the dashboard/worksheet **display** name and matched on the record's
`page_display`, so a dashboard composition gets the rendered *page* as its image. When you have live
Tableau credentials and want a high-resolution render instead, fetch the per-view PNG
(`GET /sites/{site}/views/{view-id}/image?resolution=high`) and drop it in `image_dir` as
`<page_display>.png` — it then wins as a provided image. **Never** put the PNG anywhere near the
PBIR output folder.

### 3. Review each image (this is the LLM vision step — you are the driving agent)

`io.agent_prompt(bundle)` renders the instruction. For **each visual with an image** (the
`reviewable` ones; prioritise `priority: high` = a hack or a non-decisive pick), open the image and
decide: does the deterministic type match what the picture shows? If yes, keep it (null). If no,
choose a better type **from that visual's `candidates`**. Produce a JSON array of answers:

```json
[
  {"page": "...", "visual": "...", "chosen_type": "donutChart", "reason": "ring with a hole, not a full pie"},
  {"page": "...", "visual": "...", "chosen_type": null, "reason": "bar matches"}
]
```

`chosen_type` is either one of that visual's candidates or `null` (keep the deterministic pick). Use
`page`/`visual` from the bundle entry verbatim so the answer keys back to the right visual.

### 4. Apply the answers (deterministic re-bind)

```python
new_parts, report = io.apply_adjudications(parts, records, adjudications)
# report = {"applied": [...], "kept": [...], "rejected": [...]}
```

`new_parts` is the PBIR with the agreed type switches (and any valid position corrections) applied;
the input `parts` is left untouched. Inspect `report["rejected"]` — a rejection means the answer
named a type outside the candidate list, an unknown visual, a missing part, or a change that would
have altered the query; that visual keeps its deterministic type. Write `new_parts` as the report
exactly as Tier-1 would (the estate orchestrator's openable-project writer).

---

## What the oracle never does

- It never invents or changes a **field binding** — fields are exact-bound by Tier-1 and read-only.
- It never **routes** an unsupported worksheet into a supported one (that stays a Tier-1 warning).
- It never reproduces **formatting** — colours, fonts, legends, label/title styling, palettes, theme
  JSON, tooltips. Those are a separate Tier-2 styling pass.
- It never runs **without an image** for a visual, and it never overrides a type that is not one of
  the candidates the deterministic engine could faithfully bind.
