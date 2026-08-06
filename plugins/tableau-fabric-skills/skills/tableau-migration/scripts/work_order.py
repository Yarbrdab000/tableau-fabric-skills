"""Tier-3 work order -- adjudicate a rebuilt dashboard and hand it off ready to act on.

The deterministic engine already knows a great deal it currently throws away: which visuals it
flagged, which calcs it stubbed and why, what it deferred, and which properties it deliberately set.
Today that reaches a human as prose warnings, and reaches an agent as
:func:`audit_tier.audit_prompt` -- a TEXT-ONLY prompt whose reader has never seen the dashboard.

This module turns that knowledge into a **work order**: one document per dashboard that lets the next
agent reach full fidelity by REMOVING work rather than adding instructions.

WHY THIS SHAPE (measured, not assumed)
--------------------------------------
The bottleneck is agent TURNS, not knowledge. One mechanical verify cycle (bridge ``reload`` +
``screenshot``) costs ~4.8s, so an hour of work is ~80 turns, not compute. A retrieval-optimised
corpus measured its own A/B and moved wall clock **2%** -- it made the ANSWERS cheap but never made
the QUESTIONS go away. So this document attacks turns directly, in four ways:

1. **Batch by MECHANISM, not by visual.** Nine auto-themed charts as nine items costs ~35 turns; as
   ONE item with nine paths it is one edit pass and one verify.
2. **Pre-localise.** Every item carries a file path and a JSON pointer, so nothing is ever searched.
3. **Inline the answer.** Citing a reference card costs a turn to open it. The corpus measured an
   agent given a pull-only doc index making ZERO queries against it -- "an agent does not know what
   it does not know, so a pull-only index cannot warn it."
4. **Say what is already correct.** The subtractive lever: a verified-correct list SHRINKS the search
   space, where every other section expands it.

WHAT IS DELIBERATELY ABSENT
---------------------------
**No turn budget, no time limit, no "good enough".** Turns are an output we measure, never an input we
impose -- a budget buys speed by shipping a worse report, the same failure class as a false PASS. The
document's completion criterion is fidelity: *"finishing every item is the START of your judgement."*

**It never edits anything.** It adjudicates and hands off, exactly like the read-only critic pattern.

ANCHORING RISK (the reason PART B and PART D exist)
---------------------------------------------------
A document that says "these nine things are wrong" can anchor a reader into fixing only those nine.
PART B proves everything else was actually examined; PART D explicitly reopens the field. When
benchmarking this, verify the reader STILL finds gaps the work order did not list -- if it stops,
the document is capping quality and must change.

Stdlib only. Reads artifacts, writes markdown; never runs a migration and never calls a model.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone

SCHEMA_VERSION = "work_order/1"
SUBDIR = "work_orders"

# Our emitted PBIR visualType -> the corpus's visual_class folder. The two vocabularies were built
# independently, so this is a real translation, not a formality: an unmapped type silently yields no
# corpus guidance at all, which reads exactly like "the corpus has nothing to say about this".
VISUAL_CLASS_MAP = {
    "barchart": "barchart", "clusteredbarchart": "barchart", "stackedbarchart": "barchart",
    "hundredpercentstackedbarchart": "barchart",
    "columnchart": "columnchart", "clusteredcolumnchart": "columnchart",
    "stackedcolumnchart": "columnchart", "hundredpercentstackedcolumnchart": "columnchart",
    "linechart": "linechart", "areachart": "areachart", "stackedareachart": "areachart",
    "scatterchart": "scatterchart", "piechart": "donutchart", "donutchart": "donutchart",
    "card": "cardvisual", "multirowcard": "cardvisual", "cardvisual": "cardvisual",
    "kpi": "cardvisual",
    "tableex": "tableex", "pivottable": "pivottable", "matrix": "pivottable",
    "slicer": "slicer", "textbox": "textbox", "image": "image",
    "shapemap": "shapemap", "map": "map", "filledmap": "shapemap",
    "lineclusteredcolumncombochart": "combochart", "linestackedcolumncombochart": "combochart",
    "combochart": "combochart",
}

# Worklist categories that share ONE fix mechanism get batched into a single item. The mechanism is
# the unit of work for an editor -- fixing nine identical colour defaults is one pass, not nine.
MECHANISM_OF_CATEGORY = {
    "color_scale": "colour",
    "default_palette": "colour",
    "categorical_color": "colour",
    "date_grain": "date-grain",
    "chart_type": "chart-type",
    "dynamic_title": "title",
    "measure_trellis": "layout",
    "reference_line": "analytics",
    "caption_fallback": "field-binding",
}

_SEVERITY_RANK = {"blocking": 0, "high": 1, "medium": 2, "low": 3}

# The engine files most viz items under category "other", so classifying on category alone leaves
# nearly everything unbatched -- which defeats the point (nine one-visual items cost nine verify
# cycles; one nine-visual batch costs one). These match the REASON text, which is where the actual
# mechanism is stated. Ordered: first match wins.
_REASON_MECHANISM = (
    (re.compile(r"mark colours? deferred|per-member mark colour|theme colours", re.I), "colour"),
    (re.compile(r"colou?r scale|palette|gradient", re.I), "colour"),
    (re.compile(r"measure-trellis|side-by-side bar charts", re.I), "layout"),
    (re.compile(r"date part|date grain|grain not applied", re.I), "date-grain"),
    (re.compile(r"reference/target/trend line", re.I), "analytics"),
    (re.compile(r"caption fallback|bound by caption", re.I), "field-binding"),
    (re.compile(r"chart type used a fallback|approximation", re.I), "chart-type"),
    (re.compile(r"dynamic title", re.I), "title"),
    # Phrases the ENGINE emits verbatim for a whole class of findings, so these batch correctly on
    # any workbook rather than only on the one they were first observed in. Each is a distinct fix
    # procedure, which is the test for whether a batch is real: one procedure, applied N times.
    (re.compile(r"filter card .*resolved to no model|filter card .*no matching", re.I),
     "filter-card-binding"),
    (re.compile(r"nested formula table calc routed to review", re.I), "nested-table-calc"),
    (re.compile(r"quick table calc routed to review|ordering scope .* does not decompose", re.I),
     "table-calc-scope"),
    (re.compile(r"author-hidden zone", re.I), "hidden-zone"),
    (re.compile(r"unsupported derivation", re.I), "field-binding"),
    (re.compile(r"no usable field bindings|shelf layout not supported|no visual emitted", re.I),
     "unrebuilt-visual"),
)


def mechanism_of(item):
    """The FIX MECHANISM an item belongs to, or ``None`` to keep it standalone.

    Category first (cheap and explicit), then the reason text (where the engine actually says what it
    did). An item with no known mechanism is deliberately NOT force-fitted into a batch: a batch
    claims "these share one fix", and a wrong batch is worse than no batch.
    """
    mech = MECHANISM_OF_CATEGORY.get(item.get("category"))
    if mech:
        return mech
    reason = item.get("reason") or ""
    for pattern, name in _REASON_MECHANISM:
        if pattern.search(reason):
            return name
    return None


# =====================================================================================
# Reading the run's own artifacts
# =====================================================================================
def _load(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return None


def load_run(run_dir):
    """Gather every artifact a work order draws on. Missing pieces degrade to ``None``/``{}``."""
    out = os.path.join(run_dir, "out")
    return {
        "run_dir": run_dir,
        "report": _load(os.path.join(out, "report.json")) or {},
        "references": _load(os.path.join(out, "reference_images", "manifest.json")) or {},
    }


def workbook_entries(report):
    return [w for w in (report.get("workbooks") or []) if isinstance(w, dict)]


def dashboards_of(workbook, references):
    """Dashboard names to write an order for, preferring the reference manifest's declared list.

    The manifest read the workbook itself, so it knows every DECLARED dashboard -- including ones with
    no image. Falling back to the worklist's pages would silently drop a dashboard that produced no
    flagged visual, which is exactly the dashboard someone would assume was fine.
    """
    declared = list(references.get("declared_dashboards") or [])
    if declared:
        return declared
    seen, out = set(), []
    for item in ((workbook.get("remediation_worklist") or {}).get("items") or []):
        name = item.get("page_display") or item.get("page")
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def reference_image_for(references, dashboard):
    """``(png_path, confidence)`` for a dashboard, joined on the EXACT Tableau name.

    Never joins on a filename: this repo, the REST path and the PBIR emitter all sanitize differently
    and none can be derived from another.
    """
    for rec in (references.get("images") or []):
        if rec.get("dashboard") == dashboard:
            png = rec.get("png")
            base = os.path.dirname(references.get("_manifest_path") or "")
            return (os.path.join(base, png) if base and png else png), rec.get("confidence")
    return None, None


# =====================================================================================
# PBIR: locate the real file behind a flagged visual, so nothing is ever searched for
# =====================================================================================
def find_report_dir(workbook_entry, run_dir):
    """The ``*.Report`` folder for a built workbook, or ``None``.

    ``pbip_folder`` in report.json is the path to the ``.pbip`` FILE, not a directory -- so resolve
    from its parent. Getting this wrong is silent and expensive: every downstream item loses its file
    path, and PART B (the verified-correct list) collapses to "everything is flagged", which is not
    merely unhelpful but FALSE.
    """
    pbip = workbook_entry.get("pbip_folder") or ""
    if not pbip:
        return None
    path = pbip if os.path.isabs(pbip) else os.path.join(run_dir, "out", pbip)
    root = path if os.path.isdir(path) else os.path.dirname(path)
    if not os.path.isdir(root):
        return None
    for entry in sorted(os.listdir(root)):
        if entry.lower().endswith(".report") and os.path.isdir(os.path.join(root, entry)):
            return os.path.join(root, entry)
    return None


def index_visuals(report_dir):
    """``{page_name: {"display": str, "visuals": [{name, type, path}]}}`` from PBIR on disk.

    Read from the emitted artifacts rather than reconstructed from the report, because the PATH is the
    point: an item that names the file it lives in costs the reader zero turns to locate.
    """
    pages = {}
    if not report_dir:
        return pages
    base = os.path.join(report_dir, "definition", "pages")
    if not os.path.isdir(base):
        return pages
    for page_name in sorted(os.listdir(base)):
        page_dir = os.path.join(base, page_name)
        page_json = _load(os.path.join(page_dir, "page.json")) or {}
        visuals = []
        vdir = os.path.join(page_dir, "visuals")
        if os.path.isdir(vdir):
            for vname in sorted(os.listdir(vdir)):
                vpath = os.path.join(vdir, vname, "visual.json")
                vjson = _load(vpath) or {}
                visuals.append({
                    "name": vname,
                    "type": ((vjson.get("visual") or {}).get("visualType")),
                    "path": os.path.normpath(vpath),
                    "objects": sorted(((vjson.get("visual") or {}).get("objects") or {}).keys()),
                })
        pages[page_name] = {"display": page_json.get("displayName") or page_name,
                            "visuals": visuals}
    return pages


def page_for_dashboard(pages, dashboard):
    """The PBIR page whose ``displayName`` IS the Tableau dashboard name (the documented join)."""
    for name, info in pages.items():
        if info.get("display") == dashboard:
            return name, info
    return None, None


# =====================================================================================
# Corpus: pull the answer IN, never send the reader out to fetch it
# =====================================================================================
def corpus_class_for(visual_type):
    return VISUAL_CLASS_MAP.get(re.sub(r"[^a-z]", "", (visual_type or "").lower()))


# The procedure for a whole mechanism. The engine's own per-item ``remediation`` often falls back to
# "Review this item against the source and remediate", which tells a reader nothing they did not
# already know -- reproducing it faithfully would make this document a reformatted worklist. These say
# what to actually DO, once, for the batch. Keyed on mechanism, so they can never drift onto a
# workbook-specific special case.
_PROCEDURE = {
    "colour": (
        "Tableau assigned explicit per-member mark colours; the emitted visual fell back to the theme "
        "palette. Read the member->hex pairs off the Tableau image, then add a "
        "`visual.objects.dataPoint` entry per member -- each a `fill` targeted by a `scopeId` data "
        "selector (ComparisonKind 0 Equal; Left = the coloured column, Right = the member literal). "
        "Hex literals inside a fill are QUOTED (`'#5CA35C'`)."
    ),
    "nested-table-calc": (
        "The engine parsed the base value but refused the nested formula, so the visual currently "
        "shows the UNADJUSTED number. Open the source calc named in the reason, write the DAX "
        "equivalent as a model measure, then repoint the visual's projection at it. Check against the "
        "Tableau image -- a base value usually looks plausible, so a wrong number here will not "
        "announce itself."
    ),
    "table-calc-scope": (
        "A quick table calc whose ordering scope does not decompose into a single DAX window. Decide "
        "the partition and the order from the Tableau view, then express it explicitly (`CALCULATE` + "
        "`ALLSELECTED` over the partition columns) rather than relying on visual order."
    ),
    "filter-card-binding": (
        "A Tableau filter card resolved to no model field, so no slicer was emitted. Find the model "
        "column matching the name in the reason (the `federated.*` prefix is a Tableau datasource id, "
        "not part of the field name), add a `slicer` visual bound to it, and place it to match the "
        "filter shelf in the Tableau image."
    ),
    "hidden-zone": (
        "Tableau's show/hide toggle hides these zones on open, and the engine did not rebuild them. "
        "Confirm against the Tableau image FIRST: if the zone is not visible there, the correct action "
        "is to leave it out and say so -- rebuilding it would ADD something the target does not show."
    ),
    "unrebuilt-visual": (
        "No visual was emitted at all, so this is new construction, not a repair. Build it from the "
        "Tableau image and the worksheet's fields; choose the Power BI visual type whose encoding "
        "matches what the image shows, not the one closest in name."
    ),
    "field-binding": (
        "The field did not bind to a model column. Map it to the real table/column, and treat a "
        "caption-only match as unproven until you have confirmed the column exists."
    ),
}

# Which corpus text actually speaks to a mechanism. Cards are indexed by VISUAL CLASS, so a lineChart
# batch about colour otherwise attracts a lineChart card about axis display units -- true, verified,
# and irrelevant. An irrelevant card is not free: it costs reading time and teaches the reader that
# the corpus block is skippable, which is exactly the habit this document depends on not forming.
_MECHANISM_TERMS = {
    "colour": ("color", "colour", "fill", "palette", "datapoint", "gradient"),
    "nested-table-calc": ("measure", "dax", "calculate", "window", "running"),
    "table-calc-scope": ("window", "allselected", "partition", "order"),
    "filter-card-binding": ("slicer", "filter"),
    "field-binding": ("column", "projection", "binding", "displayname"),
}


def card_matches(card, mechanism):
    """Does this corpus card speak to this mechanism? Unknown mechanism -> keep it (fail open)."""
    terms = _MECHANISM_TERMS.get(mechanism)
    if not terms:
        return True
    hay = " ".join(_as_list(card.get("props")) + _as_list(card.get("gotchas"))).lower()
    return any(t in hay for t in terms)


# The engine's own catch-all remediation. Matched so it can be dropped where a batch procedure says
# something real; matched LOOSELY on purpose, because the cost of missing one is a redundant line
# while the cost of over-matching is losing a specific instruction.
_GENERIC_REMEDIATION = re.compile(
    r"^(review (this )?item|review .{0,24}against the source)", re.I)


def _clip(text, limit):
    """Trim to ``limit`` chars on a WORD boundary, marking the cut with an ellipsis.

    A hard character slice ends instructions mid-word ("partition s", "align the bran"), which reads
    as a CORRUPTED document rather than an abbreviated one -- and a reader who cannot tell which it is
    has to go find the full text, spending exactly the turn this document exists to save.
    """
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + " ..."


def _as_list(value):
    """Normalise a corpus field that is sometimes a list and sometimes a string.

    Cards are hand-authored upstream, so ``making_it_props`` / ``build_gotchas`` arrive either way.
    Iterating a string yields ONE BULLET PER CHARACTER -- which renders as plausible-looking markdown
    (`- P`, `- a`, `- l`) rather than an error, so it ships silently. Splitting a string on newlines
    keeps multi-line prose readable.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.splitlines() if p.strip()]
        return parts or ([value.strip()] if value.strip() else [])
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def load_corpus_cards(corpus_root, visual_class, limit=2):
    """Up to ``limit`` cards for a visual class, as ``{id, props, gotchas, exemplar}``.

    Returns the CONTENT, not a citation. Reading a card costs the next agent a turn; reading it here
    costs nothing, and the corpus's own measurement is that a pull-only layer goes unqueried.
    """
    if not corpus_root or not visual_class:
        return []
    folder = os.path.join(corpus_root, "cards", "view", visual_class)
    if not os.path.isdir(folder):
        return []
    cards = []
    for entry in sorted(os.listdir(folder))[:limit]:
        try:
            with open(os.path.join(folder, entry), encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            continue
        blob = {}
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
        if m:
            try:
                blob = json.loads(m.group(1))
            except Exception:
                blob = {}
        cards.append({
            "id": os.path.splitext(entry)[0],
            "props": _as_list(blob.get("making_it_props")),
            "gotchas": _as_list(blob.get("build_gotchas")),
            "exemplar": ((re.search(r"`exemplar_report`:\s*`([^`]+)`", text) or [None, None])[1]
                         if "exemplar_report" in text else None),
        })
    return cards


def load_build_checklist(corpus_root, limit=9):
    """The corpus's cross-cutting checks -- its ONLY push layer.

    Its own preamble is the argument for including it: an agent migrating a workbook ran 10 lookups,
    "none about theme, colour or layout", and shipped a white-background report against a dark
    original. The corpus HELD the answer; retrieval never surfaced it, "because retrieval only answers
    questions that get asked."
    """
    path = os.path.join(corpus_root or "", "BUILD-CHECKLIST.md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return []
    out = []
    for row in re.findall(r"^\|\s*\d+\s*\|\s*\*\*(.+?)\*\*\s*\|\s*(\d+)\s*\|", text, re.M):
        out.append({"ask": row[0].strip(), "anti_patterns": int(row[1])})
    return out[:limit]


# =====================================================================================
# PART A -- batch the flagged work by MECHANISM
# =====================================================================================
def _visual_lookup(page_info):
    """``({visual_name: rec}, {type: [rec]})`` -- exact match first, type as the fallback."""
    by_name, by_type = {}, {}
    for v in (page_info or {}).get("visuals", []):
        if v.get("name"):
            by_name[v["name"]] = v
        by_type.setdefault((v.get("type") or "").lower(), []).append(v)
    return by_name, by_type


def locate(item, by_name, by_type):
    """Resolve an item to its emitted file(s) -> ``(paths, exact)``.

    The worklist records the emitted visual NAME (``item["visual"]``), which is the same string as the
    visual's folder on disk -- so most items resolve to exactly one file and the reader never searches
    or guesses. Only an item with no recorded name falls back to matching on visual TYPE, which
    narrows to a candidate set rather than a file; ``exact`` says which happened so the document can
    be honest about it.
    """
    name = item.get("visual")
    if name and name in by_name:
        return [by_name[name]["path"]], True
    return [v["path"] for v in by_type.get((item.get("visual_type") or "").lower(), [])], False


def batch_items(items, page_info, corpus_root=None):
    """Group worklist items into mechanism batches, each pre-localised to real files.

    One batch = one edit pass = ONE verify cycle. Duplicates are collapsed first: the engine can file
    the same finding twice (once per emitted visual of a split trellis, say), and a reader who fixes
    it once then meets it again spends a turn re-deriving that there is nothing left to do.
    """
    by_name, by_type = _visual_lookup(page_info)
    seen, deduped = set(), []
    for item in items:
        # Key on (emitted visual, mechanism) where the visual is known, else (worksheet, type,
        # mechanism): the engine states the same finding more than one way ("categorical mark colours
        # deferred (the area visual type does not carry a per-member mark colour)" and "the area
        # visual type does not carry a per-member mark colour" are ONE issue). Keying on the prose
        # leaves the reader to notice the duplication itself.
        key = (item.get("visual") or (item.get("worksheet"), item.get("visual_type")),
               mechanism_of(item) or (item.get("reason") or "")[:120])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    batches, singles = {}, []
    for item in deduped:
        mech = mechanism_of(item)
        paths, exact = locate(item, by_name, by_type)
        enriched = dict(item)
        enriched["paths"] = paths
        enriched["exact"] = exact
        if mech:
            batches.setdefault(mech, []).append(enriched)
        else:
            singles.append(enriched)
    out = []
    for mech, group in sorted(batches.items()):
        classes = {corpus_class_for(i.get("visual_type")) for i in group}
        cards = []
        for cls in sorted(c for c in classes if c):
            cards.extend(c for c in load_corpus_cards(corpus_root, cls, limit=2)
                         if card_matches(c, mech))
        out.append({"mechanism": mech, "items": group, "cards": cards[:2],
                    "procedure": _PROCEDURE.get(mech),
                    "severity": min((_SEVERITY_RANK.get(i.get("severity"), 9) for i in group),
                                    default=9)})
    for item in singles:
        cards = load_corpus_cards(corpus_root, corpus_class_for(item.get("visual_type")), limit=1)
        out.append({"mechanism": None, "items": [item], "cards": cards, "procedure": None,
                    "severity": _SEVERITY_RANK.get(item.get("severity"), 9)})
    out.sort(key=lambda b: (b["severity"], b["mechanism"] or "zz"))
    return out


# =====================================================================================
# PART B -- what was checked and is CORRECT (the subtractive section)
# =====================================================================================
def verified_correct(page_info, flagged_names, ambiguous_types):
    """Visuals nothing was flagged against -- examined and raising nothing.

    Two exclusions, and the second is the honest one. A visual is dropped when it is named by an item
    (precise), OR when its TYPE was only narrowed to a candidate set -- because then we cannot prove
    which member of that set is the clean one. This section's whole value is that a reader can trust
    it and skip the re-audit, so listing a visual that might be the broken one would destroy it.
    """
    rows = []
    for v in (page_info or {}).get("visuals", []):
        if v.get("name") in flagged_names:
            continue
        if (v.get("type") or "").lower() in ambiguous_types:
            continue
        rows.append({"visual": v.get("name"), "type": v.get("type"),
                     "objects": v.get("objects") or [], "path": v.get("path")})
    return rows


# =====================================================================================
# Rendering
# =====================================================================================
_PROTOCOL = """## How to use this work order

You are finishing a Tableau -> Power BI dashboard migration that a deterministic engine has already
built. This document is everything that engine knows and would otherwise discard.

1. **Look at both images first** (linked below). Everything else here is a claim about them.
2. **Work PART A top to bottom.** Each item is batched by FIX MECHANISM and carries the file path and
   the property to change, so you never need to search for anything. Fix a whole batch, then verify
   once -- not once per visual.
3. **Trust PART B.** Those were examined and are correct. Re-auditing them is the single largest
   waste of effort available to you.
4. **PART C is honest ignorance,** not an oversight -- the engine could not determine these. They are
   yours to judge.
5. **Then read PART D.** It matters more than the rest.

There is no time limit and no step budget. Correctness is the only target.
"""

_CLOSING = """## PART D -- when is this done?

**Finishing every item above is the START of your judgement, not the end of your work.**

This work order lists what the engine could detect. It cannot see everything. Now compare the two
images yourself, as a whole dashboard rather than visual by visual, and ask the question the engine
cannot: *does this read as the same dashboard?*

Anything you find that is not listed above is a gap this work order MISSED. Fix it, and say so in
your report -- an unlisted gap is the most valuable feedback there is, because it makes the next
work order better.

**Done means the dashboard matches its source. Not "the items are complete."**
"""


def render(order):
    """Render one dashboard's work order as markdown."""
    L = []
    L.append("# Work order -- %s" % order["dashboard"])
    L.append("")
    L.append("> Workbook: `%s`  ·  generated %s  ·  schema `%s`"
             % (order.get("workbook") or "?", order["generated_at_utc"], SCHEMA_VERSION))
    L.append("")
    L.append(_PROTOCOL)

    L.append("## The two images")
    L.append("")
    ref = order.get("reference_image")
    if ref:
        L.append("- **Tableau (the target):** `%s`%s" % (
            ref, "  _(match confidence: %s)_" % order["reference_confidence"]
            if order.get("reference_confidence") else ""))
    else:
        L.append("- **Tableau (the target):** _not captured_ -- you are working without the source "
                 "image. Everything below is structural; the visual comparison is yours to make.")
    L.append("- **Power BI (what was built):** capture it yourself from the open report "
             "(`powerbi-desktop screenshot <page-id>`); the page for this dashboard is `%s`."
             % (order.get("page_name") or "?"))
    L.append("")

    L.append("## PART A -- pre-resolved work (%d batch(es))" % len(order["batches"]))
    L.append("")
    if not order["batches"]:
        L.append("_Nothing was flagged on this dashboard._")
    for i, batch in enumerate(order["batches"], 1):
        n = len(batch["items"])
        if batch["mechanism"]:
            heading = ("**A%d. %s** -- %d item%s; ONE fix procedure, so do them together and verify "
                       "once." % (i, batch["mechanism"], n, "" if n == 1 else "s")) if n > 1 else \
                      ("**A%d. %s**" % (i, batch["mechanism"]))
        else:
            heading = "**A%d.** %s" % (i, batch["items"][0].get("category") or "item")
        L.append(heading)
        L.append("")
        if batch.get("procedure"):
            L.append("> **How to fix this class:** %s" % batch["procedure"])
            L.append("")
        for it in batch["items"]:
            L.append("- _(%s)_ %s" % (it.get("severity") or "?", it.get("reason") or ""))
            # Suppress the engine's generic fallback where a batch procedure already says more. Two
            # instructions where the second is vaguer than the first trains the reader to skim both.
            rem = (it.get("remediation") or "").strip()
            if rem and not (batch.get("procedure") and _GENERIC_REMEDIATION.match(rem)):
                L.append("  - **do:** %s" % rem)
            if it.get("worksheet"):
                L.append("  - source worksheet: `%s`" % it["worksheet"])
            paths = it.get("paths") or []
            if paths and it.get("exact"):
                L.append("  - **file:** `%s`" % paths[0])
            elif paths:
                L.append("  - **%d candidates** of type `%s` -- the worklist recorded no emitted "
                         "visual for this item, so identify the one built from worksheet `%s`:"
                         % (len(paths), it.get("visual_type") or "?", it.get("worksheet") or "?"))
                for p in paths[:12]:
                    L.append("    - `%s`" % p)
            else:
                L.append("  - _(no visual of this type on this page -- it may be a dashboard-scope "
                         "item, or the visual was not emitted)_")
        for card in batch["cards"]:
            if not (card.get("props") or card.get("gotchas")):
                continue
            L.append("")
            L.append("  <details><summary>verified precedent (corpus `%s`)</summary>" % card["id"])
            L.append("")
            for prop in card["props"][:6]:
                L.append("  - `%s`" % prop)
            for g in card["gotchas"][:3]:
                L.append("  - ⚠ %s" % g)
            if card.get("exemplar"):
                L.append("  - copy from: `%s`" % card["exemplar"])
            L.append("")
            L.append("  </details>")
        L.append("")

    L.append("## PART B -- checked and CORRECT (do not re-audit)")
    L.append("")
    ok = order["verified"]
    if ok:
        L.append("%d visual(s) on this page are of a type nothing was flagged against. Their emitted "
                 "property groups are listed so you can see what was actually set." % len(ok))
        L.append("")
        L.append("| visual | type | properties set |")
        L.append("|---|---|---|")
        for r in ok:
            L.append("| `%s` | %s | %s |" % (r["visual"], r["type"] or "?",
                                             ", ".join("`%s`" % o for o in r["objects"]) or "_none_"))
    else:
        L.append("_No visual on this page could be cleared: every emitted visual type appears in at "
                 "least one PART A item. That is a limit of the flagging, not proof that all ten are "
                 "wrong -- see the candidate-set notes above._")
    L.append("")

    L.append("## PART C -- could NOT be determined")
    L.append("")
    if order["undetermined"]:
        for u in order["undetermined"]:
            L.append("- **%s** -- %s" % (u.get("what"), u.get("why")))
    else:
        L.append("_Nothing outstanding._")
    L.append("")

    if order.get("stubs"):
        L.append("### Calculations still stubbed (%d)" % len(order["stubs"]))
        L.append("")
        for s in order["stubs"]:
            L.append("- **`%s`** (%s)" % (s.get("name"), s.get("category") or "?"))
            if s.get("formula"):
                L.append("  - Tableau: `%s`" % _clip(s["formula"], 200))
            if s.get("fallback_reason"):
                L.append("  - why it stubbed: %s" % s["fallback_reason"])
            if s.get("category_guidance"):
                L.append("  - guidance: %s" % _clip(s["category_guidance"], 400))
        L.append("")

    if order.get("checklist"):
        L.append("### Cross-cutting checks (things nobody thinks to ask)")
        L.append("")
        L.append("_Ranked by how often each has actually gone wrong across %d harvested workbooks._"
                 % 41)
        L.append("")
        for c in order["checklist"]:
            L.append("- %s _(%d harvested anti-pattern(s))_" % (c["ask"], c["anti_patterns"]))
        L.append("")

    L.append(_CLOSING)
    return "\n".join(L)


# =====================================================================================
# Assembly
# =====================================================================================
def build_order(workbook_entry, dashboard, pages, references, corpus_root=None, run_dir=None):
    """Assemble one dashboard's work order (data only; :func:`render` turns it into markdown)."""
    page_name, page_info = page_for_dashboard(pages, dashboard)
    worklist = (workbook_entry.get("remediation_worklist") or {})
    # An empty source worksheet has nothing to rebuild, so it is not work -- but the engine files it
    # "blocking", which would put three no-ops at the very top of PART A and cost the reader its
    # first impression on nothing. Route them to PART C as context instead.
    all_items = [i for i in (worklist.get("items") or []) if isinstance(i, dict)]
    empty_ws = [i for i in all_items if i.get("category") == "empty_worksheet"]
    actionable = [i for i in all_items if i.get("category") != "empty_worksheet"]
    items = [i for i in actionable
             if (i.get("page_display") or i.get("page")) in (dashboard, page_name)
             or i.get("scope") in ("dashboard", "worksheet")]
    batches = batch_items(items, page_info, corpus_root)
    flagged_names = {i.get("visual") for b in batches for i in b["items"] if i.get("visual")}
    ambiguous_types = {(i.get("visual_type") or "").lower()
                       for b in batches for i in b["items"]
                       if not i.get("exact") and i.get("visual_type")}

    undetermined = []
    for i in actionable:
        if i.get("severity") == "blocking" and not mechanism_of(i):
            undetermined.append({"what": i.get("category"), "why": i.get("reason")})
    for i in empty_ws:
        undetermined.append({
            "what": "empty source worksheet %r" % (i.get("worksheet") or "?"),
            "why": "no fields on any shelf, so nothing was rebuilt -- confirm it is intentionally "
                   "empty rather than a parse gap"})

    handoff = workbook_entry.get("model_translation_handoff") or {}
    ref_png, ref_conf = reference_image_for(references, dashboard)
    return {
        "schema": SCHEMA_VERSION,
        "dashboard": dashboard,
        "workbook": workbook_entry.get("name"),
        "page_name": page_name,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reference_image": ref_png,
        "reference_confidence": ref_conf,
        "batches": batches,
        "verified": verified_correct(page_info, flagged_names, ambiguous_types),
        "undetermined": undetermined,
        "stubs": list(handoff.get("requests") or []),
        "checklist": load_build_checklist(corpus_root),
    }


def generate(run_dir, corpus_root=None, outdir=None):
    """Write one work order per dashboard. Returns ``[(dashboard, path)]``. Never raises."""
    data = load_run(run_dir)
    references = data["references"]
    references["_manifest_path"] = os.path.join(run_dir, "out", "reference_images", "manifest.json")
    outdir = outdir or os.path.join(run_dir, "out", SUBDIR)
    try:
        os.makedirs(outdir, exist_ok=True)
    except Exception:
        pass
    written = []
    for wb in workbook_entries(data["report"]):
        report_dir = find_report_dir(wb, run_dir)
        pages = index_visuals(report_dir)
        for dash in dashboards_of(wb, references):
            order = build_order(wb, dash, pages, references, corpus_root, run_dir)
            stem = re.sub(r"[^\w.-]+", "_", dash, flags=re.UNICODE).strip("_") or "dashboard"
            path = os.path.join(outdir, stem + ".md")
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(render(order))
                written.append((dash, path))
            except Exception:
                continue
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate a Tier-3 work order per dashboard from a completed migration run.")
    ap.add_argument("--run", required=True, help="run folder (holds out/report.json)")
    ap.add_argument("--corpus", help="tc-corpus root, for inlined precedent + the build checklist")
    ap.add_argument("--out", help="output folder (default: <run>/out/%s)" % SUBDIR)
    args = ap.parse_args(argv)
    written = generate(args.run, args.corpus, args.out)
    for dash, path in written:
        print("[WORK ORDER] %-40s -> %s" % (dash, path))
    if not written:
        print("[WORK ORDER] nothing to write (no report.json, or no dashboards declared)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
