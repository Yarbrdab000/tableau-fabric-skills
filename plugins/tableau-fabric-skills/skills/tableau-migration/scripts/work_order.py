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

# Corpus classes reachable only by MECHANISM, never by our emitted visual type.
#
# The gap this closes is an inversion, and it is the worst one available here. Cards are normally
# found by mapping the type we emitted -- but when the engine emitted NOTHING (mark class unsupported,
# no usable field bindings) there is no type to map, so the hardest item on the page got zero corpus
# support while nine already-working charts each got a card. Help was densest where it was least
# needed.
#
# ``customvisual`` is exactly the right destination for those: it is where the corpus records what to
# do when the NATIVE route is exhausted (its lollipop card documents the native combo-chart attempt
# failing across three runs before a Deneb/Vega-Lite build worked). ``page`` and ``shape`` are
# likewise unreachable from any visual type, because no visual carries them.
MECHANISM_CORPUS_CLASS = {
    "unrebuilt-visual": "customvisual",
    "hidden-zone": "page",
    "filter-card-binding": "slicer",
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


# =====================================================================================
# Selection: what belongs in this document at all
# =====================================================================================
# The scope rule, and the reason the first version of this file failed.
#
# Measured 2026-08-06 on `Sales Operations Cases Portfolio`: the downstream agent working ALONE, with
# only the Tableau PNG and the rebuilt report, reached near-perfect fidelity in 1h40m. The same agent
# handed a comprehensive "audit" work order took 3h and produced a WORSE report. The document had
# negative value.
#
# Two causes, both design errors:
#
#   1. It asserted a section of visuals were "checked and CORRECT -- do not re-audit", derived from
#      "the engine's worklist did not flag them". But the worklist records TRANSLATION failures, and
#      says nothing about whether a visual LOOKS like the target. Those are different claims. Worse,
#      the correlation runs backwards: a visual that translated cleanly is exactly the one still
#      wearing the default theme, so the section suppressed the highest-value work on the page. It
#      named the three visuals the solo agent rebuilt.
#
#   2. It spent its length on things the reader can SEE -- palette, axis titles, sort order, layout.
#      The agent reads those off the reference image faster and more accurately than we can describe
#      them, so every such line is pure cost.
#
# So this document now carries ONLY what the picture cannot tell you: a number that is a placeholder
# rather than a measurement, a construct that is missing with no trace to notice, and a number that
# is quietly wrong while looking completely reasonable. The image is the specification for everything
# else, and it is a better one than we could write.
_SILENT_WRONG = re.compile(
    r"routed to review|emitted with the base value only|does not decompose", re.I)

_NOT_REBUILT = re.compile(
    r"no visual emitted|no usable field bindings|not supported|skipped|"
    r"resolved to no model field|not rebuilt", re.I)


def stub_requests(workbook_entry):
    """Calculations the engine could not translate, with the Tableau source it refused.

    These are the highest-value facts available. A stub is emitted as a measure returning ``0``, so on
    the canvas it renders as a confident, plausible number -- ``0`` where the truth is ``96%``. No
    amount of looking at the two images tells you that tile is a placeholder rather than a
    measurement; you have to be told.
    """
    handoff = workbook_entry.get("model_translation_handoff") or {}
    return [r for r in (handoff.get("requests") or []) if isinstance(r, dict)]


def stub_usage(report_dir, stub_names):
    """``{stub_name: [visual dicts]}`` -- which tile on the canvas is showing each placeholder.

    This is the join the reader would otherwise do by hand, and it is what converts "8 measures failed
    to translate" (a fact about a model) into "THIS card is lying to you" (a fact about the picture).
    """
    usage = {}
    if not report_dir:
        return usage
    for page_info in (index_visuals(report_dir) or {}).values():
        for vis in page_info.get("visuals", []):
            try:
                with open(vis["path"], encoding="utf-8-sig") as fh:
                    blob = fh.read()
            except Exception:
                continue
            for name in stub_names:
                # Substring match on the measure name as it appears in the projection. Deliberately
                # loose: a false positive costs the reader one glance, a false negative hides a lying
                # tile, and those costs are nowhere near equal.
                if name and name in blob:
                    usage.setdefault(name, []).append(vis)
    return usage


def partition_items(items):
    """Split the worklist into the two classes the picture cannot reveal, dropping the rest.

    ``not_rebuilt``  -- Tableau had it, the report has nothing. A gap is visible, but not WHAT is
                       missing or why the deterministic route refused, which is the expensive part.
    ``silent_wrong`` -- something WAS emitted and renders plausibly, but the number is not the
                       number. The worst class in the document: nothing about it looks wrong.

    Everything else (deferred colours, theme palettes, axis titles, layout) is deliberately dropped.
    The reader can see all of it, and listing it is what made the previous version slower AND worse.
    """
    not_rebuilt, silent_wrong = [], []
    seen = set()
    for item in items or []:
        reason = item.get("reason") or ""
        key = (item.get("worksheet"), item.get("visual"), reason[:80])
        if key in seen:
            continue
        seen.add(key)
        if _SILENT_WRONG.search(reason):
            silent_wrong.append(item)
        elif _NOT_REBUILT.search(reason):
            not_rebuilt.append(item)
    return not_rebuilt, silent_wrong


def exhausted_route_cards(corpus_root, limit=1):
    """Corpus precedent for marks with NO native Power BI equivalent.

    Narrow on purpose. Almost all corpus knowledge is about styling, which the reader gets from the
    image for free. The exception is the record of which native routes were already tried and found
    to fail -- a lollipop rebuilt three times as a combo chart before a Deneb build worked. That is a
    negative result, it is invisible in both images, and rediscovering it costs hours.
    """
    return load_corpus_cards(corpus_root, "customvisual", limit=limit)


# =====================================================================================
# Rendering
# =====================================================================================
_HEADER = """## What this is

The rebuilt Power BI report is open and the original Tableau dashboard is captured as an image. Your
job is to make the report match that image.

**This document is not an audit, and it is not a task list.** It is the short list of things that are
true about this migration but are IMPOSSIBLE TO SEE in either image. Everything else -- colours,
fonts, layout, sort order, chart styling, spacing -- you should take from the reference image, which
is a more accurate specification than anything written here.

Read this once, keep it in mind, then work from the picture.

**Nothing here is a claim that any other part of the report is correct.** Absence from this document
means only that the deterministic engine had nothing to say -- most often because the visual
translated cleanly, which usually means it is still wearing the default theme.
"""

_CLOSING = """## When is this done?

When the report looks like the image. Not when this document is exhausted -- it is a handful of facts
the engine happened to know, not a definition of done.

There is no time limit and no step budget. Correctness is the only target.
"""


def render(order):
    L = ["# %s -- what the picture cannot tell you" % order["dashboard"], ""]
    L.append("> Workbook: `%s`  ·  generated %s  ·  schema `%s`"
             % (order["workbook"], order["generated"], SCHEMA_VERSION))
    L.append("")
    L.append(_HEADER)

    ref = order.get("reference_image")
    path, confidence = (ref if isinstance(ref, (tuple, list)) else (ref, None)) if ref else (None, None)
    L.append("## The reference image")
    L.append("")
    if path:
        # The manifest stores a bare filename. Emit an absolute path: this is the ONE file the reader
        # must open before doing anything, and making them hunt for it is the exact cost this
        # document exists to remove.
        if order.get("reference_dir") and not os.path.isabs(str(path)):
            path = os.path.join(order["reference_dir"], str(path))
        L.append("- **Tableau (the target):** `%s`%s"
                 % (path, ("  _(match confidence: %s)_" % confidence) if confidence else ""))
    else:
        L.append("- _No reference image was captured for this dashboard._ Everything below still "
                 "applies, but you have no target to match against -- say so rather than guessing.")
    if order.get("page_name"):
        L.append("- The Power BI page for this dashboard is `%s`." % order["page_name"])
    L.append("")

    stubs = order["stubs"]
    L.append("## 1. Numbers that are placeholders, not measurements (%d)" % len(stubs))
    L.append("")
    if not stubs:
        L.append("_None -- every calculation translated._")
    else:
        L.append("The engine could not translate these Tableau calculations, so each was emitted as a "
                 "measure returning **0**. On the canvas that renders as a confident number. A card "
                 "reading `0` here is not a measurement of zero -- it is a gap. **This is the single "
                 "thing in this migration you cannot discover by looking.**")
        L.append("")
        for s in stubs:
            L.append("### `%s`" % s.get("name"))
            L.append("")
            L.append("- **Renders now as:** `0`")
            L.append("- **Tableau source:** `%s`" % _clip(s.get("formula"), 400))
            L.append("- **Refused because:** %s" % (s.get("fallback_reason") or "unknown"))
            fields = s.get("fields")
            if isinstance(fields, list) and fields:
                cols = ", ".join(
                    "`%s`.`%s`" % (f.get("table"), f.get("column"))
                    for f in fields if isinstance(f, dict) and f.get("column"))
                if cols:
                    L.append("- **Model columns it needs:** %s" % _clip(cols, 300))
            for vis in s.get("used_by") or []:
                L.append("- **Showing this placeholder:** `%s` (`%s`)"
                         % (vis.get("name"), vis.get("type")))
                L.append("  - `%s`" % vis.get("path"))
            if not (s.get("used_by") or []):
                L.append("- _No visual on this page projects it -- fixing it changes nothing visible; "
                         "treat it as lower priority than the ones that do._")
            if s.get("category_guidance"):
                L.append("- **Why this class is hard:** %s" % _clip(s["category_guidance"], 320))
            L.append("")

    wrong = order["silent_wrong"]
    L.append("## 2. Numbers that render plausibly but are WRONG (%d)" % len(wrong))
    L.append("")
    if not wrong:
        L.append("_None._")
    else:
        L.append("Something WAS emitted for each of these, and it looks entirely reasonable -- but the "
                 "engine dropped part of the calculation, so the value is not the value Tableau "
                 "shows. Check each against the reference image; nothing about them looks wrong.")
        L.append("")
        for it in wrong:
            L.append("- %s" % (it.get("reason") or ""))
            if it.get("worksheet"):
                L.append("  - source worksheet: `%s`" % it["worksheet"])
            for p in it.get("paths") or []:
                L.append("  - `%s`" % p)
            L.append("")

    missing = order["not_rebuilt"]
    L.append("## 3. In Tableau, absent from the report (%d)" % len(missing))
    L.append("")
    if not missing:
        L.append("_None -- every zone was rebuilt._")
    else:
        L.append("The engine emitted nothing for these. You can see a gap in the image; what you "
                 "cannot see is what was supposed to be there or why the deterministic route refused "
                 "-- which is the part that costs time to rediscover.")
        L.append("")
        for it in missing:
            L.append("- %s" % (it.get("reason") or ""))
            if it.get("worksheet"):
                L.append("  - source worksheet: `%s` -- find it in the reference image to see what it "
                         "should look like" % it["worksheet"])
            L.append("")
        for card in order.get("route_cards") or []:
            L.append("<details><summary>Before hand-building an unusual mark, read this "
                     "(corpus `%s`)</summary>" % card.get("id"))
            L.append("")
            L.append("A previously harvested build of a mark Power BI has no native equivalent for. "
                     "It records which native routes were tried and FAILED, which is the part you "
                     "cannot see in any image and would otherwise pay for in runs.")
            L.append("")
            for line in (card.get("props") or [])[:4]:
                L.append("- `%s`" % _clip(line, 420))
            for line in (card.get("gotchas") or [])[:3]:
                L.append("- ⚠ %s" % _clip(line, 420))
            if card.get("exemplar"):
                L.append("- copy from: `%s`" % card["exemplar"])
            L.append("")
            L.append("</details>")
            L.append("")

    L.append(_CLOSING)
    return "\n".join(L)


def build_order(workbook_entry, dashboard, pages, references, corpus_root=None, run_dir=None):
    report_dir = find_report_dir(workbook_entry, run_dir)
    page_name, page_info = page_for_dashboard(pages, dashboard)
    worklist = (workbook_entry.get("remediation_worklist") or {})
    # The engine stamps ``page_display`` with EITHER the Tableau dashboard name or the emitted PBIR
    # page id, depending on which layer raised the item -- dashboard-scope findings (filter cards,
    # hidden zones) carry the page id. Accepting only one form silently dropped five real items here,
    # and a silent drop is the worst outcome available: the reader cannot tell the difference between
    # "nothing was wrong" and "we forgot to tell you".
    keys = {k for k in (dashboard, page_name) if k}
    items = [i for i in (worklist.get("items") or [])
             if not i.get("page_display") or i.get("page_display") in keys]

    not_rebuilt, silent_wrong = partition_items(items)
    by_name, by_type = _visual_lookup(page_info or {})
    for it in silent_wrong:
        paths, exact = locate(it, by_name, by_type)
        it["paths"] = paths if exact else []

    stubs = [dict(s) for s in stub_requests(workbook_entry)]
    usage = stub_usage(report_dir, [s.get("name") for s in stubs])
    for s in stubs:
        s["used_by"] = usage.get(s.get("name")) or []
    # A stub nothing projects cannot change the picture, so it must not head the list.
    stubs.sort(key=lambda s: (not s["used_by"], s.get("name") or ""))

    return {
        "schema": SCHEMA_VERSION,
        "workbook": workbook_entry.get("name") or workbook_entry.get("workbook") or "?",
        "dashboard": dashboard,
        "generated": _now(),
        "page_name": page_name,
        "reference_image": reference_image_for(references, dashboard),
        "reference_dir": os.path.join(run_dir, "out", "reference_images") if run_dir else None,
        "stubs": stubs,
        "silent_wrong": silent_wrong,
        "not_rebuilt": not_rebuilt,
        "route_cards": exhausted_route_cards(corpus_root) if (corpus_root and not_rebuilt) else [],
    }


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

    The worklist records the emitted visual NAME, which is the same string as the visual's folder on
    disk, so most items resolve to exactly one file. An item without one falls back to matching on
    visual TYPE, which narrows to a candidate SET rather than a file -- ``exact`` says which happened,
    and only an exact hit is ever printed as a path. Printing a candidate set as if it were the answer
    sends the reader to edit visuals that are not broken.
    """
    name = item.get("visual")
    if name and name in by_name:
        return [by_name[name]["path"]], True
    return [v["path"] for v in by_type.get((item.get("visual_type") or "").lower(), [])], False


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(run_dir, corpus_root=None, outdir=None):
    """Write one work order per dashboard. Returns ``[(dashboard, path)]``. Never raises."""
    data = load_run(run_dir)
    references = data["references"]
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
            safe = re.sub(r"[^0-9A-Za-z]+", "_", dash).strip("_") or "dashboard"
            path = os.path.join(outdir, safe + ".md")
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(render(order))
                written.append((dash, path))
            except Exception as exc:
                # Report the failure instead of letting the caller read an empty result as "this
                # dashboard was clean". A swallowed exception here previously surfaced as
                # "nothing to write (no dashboards found)", which is a different and false claim.
                print("[WORK ORDER] FAILED for %r: %s: %s" % (dash, type(exc).__name__, exc))
                continue
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the Tier-3 work order for a finished run.")
    ap.add_argument("--run", required=True)
    ap.add_argument("--corpus")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    written = generate(args.run, args.corpus, args.out)
    if not written:
        print("[WORK ORDER] nothing to write (no dashboards found)")
        return 0
    for dash, path in written:
        print("[WORK ORDER] %-46s -> %s" % (_clip(dash, 46), path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
