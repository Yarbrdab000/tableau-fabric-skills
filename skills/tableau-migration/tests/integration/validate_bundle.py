r"""End-to-end **bundle** validator for the Tableau -> Fabric/Power BI migration skill.

This is the INTEGRATION level: the unit suite already proves the individual generators; this tool
proves that the WHOLE emitted bundle hangs together and faithfully mirrors the source -- the class of
failure unit tests miss ("the bundle won't open in Power BI Desktop / a visual binding dangles / the
referenced semantic model is missing / the replica doesn't match the source workbook").

It drives the real orchestrator (``migrate_estate``'s ``LocalFilesSource``) over a folder of Tableau
``.tds`` / ``.twb`` files, then runs six validation targets against the bundle ON DISK + the re-parsed
source, and prints a structured per-target PASS / FAIL / NA report with precise diagnostics:

  1. BUNDLE GENERATES            -- orchestrator runs without throwing; no asset errored.
  2. PBIR STRUCTURAL VALIDITY    -- every .Report has the full required part tree; every JSON parses;
                                    required keys present; page/visual cross-references resolve.
  3. MODEL PRESENCE + REF INTEGRITY -- a report's byPath model EXISTS (with definition.pbism +
                                    model.tmdl) and stays inside the bundle. (KNOWN open-blocker.)
  4. BINDING INTEGRITY           -- every visual field binding resolves to a table+column or measure
                                    the emitted semantic model actually defines (case-insensitive).
  5. FAITHFULNESS COUNTS         -- tables == source relations; measures == translated+stubbed calcs;
                                    pages / per-page visuals == the source viz layout (no silent drop).
  6. CROSS-DB STORAGE-MODE-AGNOSTIC -- a cross-DB federated datasource yields per-side source
                                    descriptors + join-key pairs emitted as MODEL RELATIONSHIPS.

Everything is offline / stdlib / deterministic. The validator parses the emitted TMDL/PBIR with its
OWN tolerant readers (it never reuses the producer's identifier helpers) so a shared bug can't make a
broken artifact pass.  Importing the skill scripts is only to RUN the migration.

Usage::

    py validate_bundle.py --input <folder of .tds/.twb> [--output <bundle dir>] [--json]

Exit code is 0 when every applicable target PASSes, else 1 (so a CI gate / a human both see the truth).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET


# -- make the skill's scripts importable (only to RUN the orchestrator) --------
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import migrate_estate as _me                       # noqa: E402
from connection_to_m import parse_tds              # noqa: E402
from migrate_estate import extract_calculations    # noqa: E402
from twb_to_pbir import parse_twb, VT_UNSUPPORTED  # noqa: E402


# =============================================================================
# Result container
# =============================================================================
PASS, FAIL, NA = "PASS", "FAIL", "NA"


class TargetResult:
    """One target's outcome: a status plus a list of human-readable diagnostic strings."""

    def __init__(self, key, title):
        self.key = key
        self.title = title
        self.status = NA
        self.diagnostics = []
        self.tags = set()          # machine tags (e.g. "bypath-layout-mismatch") for precise xfail gating

    def ok(self, msg=None):
        self.status = PASS
        if msg:
            self.diagnostics.append("ok: " + msg)
        return self

    def fail(self, msg, *, tag=None):
        self.status = FAIL
        self.diagnostics.append(msg)
        if tag:
            self.tags.add(tag)
        return self

    def na(self, msg):
        self.status = NA
        self.diagnostics.append(msg)
        return self

    def note(self, msg):
        self.diagnostics.append(msg)
        return self

    def to_dict(self):
        return {"key": self.key, "title": self.title, "status": self.status,
                "tags": sorted(self.tags), "diagnostics": list(self.diagnostics)}


# =============================================================================
# Tolerant TMDL reader (INDEPENDENT of the producer's helpers)
# =============================================================================
_TMDL_DECL = re.compile(r"^(\t*)(table|column|measure)\s+(.*)$")


def _read_tmdl_name(rest):
    """Read a TMDL identifier: a single-quoted name (with '' escape) or a bare token."""
    rest = rest.strip()
    if rest.startswith("'"):
        out, i = [], 1
        while i < len(rest):
            ch = rest[i]
            if ch == "'":
                if i + 1 < len(rest) and rest[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(ch)
            i += 1
        return "".join(out)
    m = re.match(r"[^\s=]+", rest)
    return m.group(0) if m else ""


def parse_table_tmdl(text):
    """Return ``(table_name, columns_set, measures_set)`` parsed from one table TMDL part.

    Only declarations at their canonical indent are taken: ``table`` at column 0, ``column`` /
    ``measure`` at exactly one tab -- so deeper M-partition / annotation lines are never mistaken
    for a column or measure.
    """
    table = None
    cols, meas = set(), set()
    for line in text.splitlines():
        m = _TMDL_DECL.match(line)
        if not m:
            continue
        indent, kw, rest = m.groups()
        if kw == "table" and indent == "":
            table = _read_tmdl_name(rest)
        elif kw == "column" and indent == "\t":
            cols.add(_read_tmdl_name(rest))
        elif kw == "measure" and indent == "\t":
            meas.add(_read_tmdl_name(rest))
    return table, cols, meas


def parse_model_tables(model_dir):
    """Parse a ``*.SemanticModel`` dir -> ``{table_name: {"columns": set, "measures": set}}``."""
    tables = {}
    tdir = os.path.join(model_dir, "definition", "tables")
    if not os.path.isdir(tdir):
        return tables
    for fn in sorted(os.listdir(tdir)):
        if not fn.endswith(".tmdl"):
            continue
        with open(os.path.join(tdir, fn), encoding="utf-8") as fh:
            name, cols, meas = parse_table_tmdl(fh.read())
        if name:
            tables[name] = {"columns": cols, "measures": meas}
    return tables


def generated_date_tables(model_dir):
    """Names of synthetic Date-dimension tables (calculated calendar tables).

    These are ADDITIVE scaffolding the generator injects for date intelligence -- not derived
    from any source relation -- so the source<->bundle faithfulness count excludes them exactly
    like the generated ``_Measures`` table. The signature (a calculated partition sourced from
    ``CALENDARAUTO()`` for Import, or a fixed-range ``CALENDAR(DATE(...), DATE(...))`` for
    DirectQuery) is specific enough that a wrongly-calculated SOURCE table is NOT hidden.
    """
    names = set()
    tdir = os.path.join(model_dir, "definition", "tables")
    if not os.path.isdir(tdir):
        return names
    for fn in sorted(os.listdir(tdir)):
        if not fn.endswith(".tmdl"):
            continue
        with open(os.path.join(tdir, fn), encoding="utf-8") as fh:
            text = fh.read()
        if "CALENDARAUTO()" in text or "= CALENDAR(DATE(" in text:
            name, _cols, _meas = parse_table_tmdl(text)
            if name:
                names.add(name)
    return names


# =============================================================================
# PBIR helpers
# =============================================================================
REQUIRED_REPORT_PARTS = (
    ".platform",
    "definition.pbir",
    "definition/report.json",
    "definition/version.json",
    "definition/pages/pages.json",
)
REQUIRED_MODEL_PARTS = ("definition.pbism", "definition/model.tmdl", ".platform")


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _ci_lookup(name, names):
    """Case-insensitive membership (per Tabular): return the matched name or None."""
    low = name.casefold()
    for n in names:
        if n.casefold() == low:
            return n
    return None


def collect_bindings(node, out):
    """Recursively collect every field binding in a visual: ``(kind, entity, property)``.

    Handles ``{"Column": ...}`` and ``{"Measure": ...}`` anywhere in the JSON, including columns
    nested inside an ``{"Aggregation": {"Expression": {"Column": ...}}}`` wrapper, so a partially
    migrated visual can't hide a dangling reference in a deep projection.
    """
    if isinstance(node, dict):
        for kind in ("Column", "Measure"):
            inner = node.get(kind)
            if isinstance(inner, dict):
                entity = ((inner.get("Expression") or {}).get("SourceRef") or {}).get("Entity")
                prop = inner.get("Property")
                if entity and prop:
                    out.append((kind, entity, prop))
        for v in node.values():
            collect_bindings(v, out)
    elif isinstance(node, list):
        for v in node:
            collect_bindings(v, out)


def _report_dirs(output_dir):
    base = os.path.join(output_dir, "reports")
    if not os.path.isdir(base):
        return []
    return [os.path.join(base, d) for d in sorted(os.listdir(base)) if d.endswith(".Report")]


def _model_dirs(output_dir):
    base = os.path.join(output_dir, "semantic_models")
    out = {}
    if not os.path.isdir(base):
        return out
    for d in sorted(os.listdir(base)):
        if d.endswith(".SemanticModel"):
            out[d[: -len(".SemanticModel")]] = os.path.join(base, d)
    return out


def _read_pbir_bypath(report_dir):
    """Return the raw ``datasetReference.byPath.path`` string for a report, or None."""
    pbir = os.path.join(report_dir, "definition.pbir")
    if not os.path.isfile(pbir):
        return None
    try:
        ref = _load_json(pbir).get("datasetReference") or {}
        return (ref.get("byPath") or {}).get("path")
    except (json.JSONDecodeError, OSError):
        return None


def _page_visual_counts(report_dir):
    """Return ``{page_display_name: {"main": int, "slicer": int}}`` for an emitted report."""
    out = {}
    pages_dir = os.path.join(report_dir, "definition", "pages")
    if not os.path.isdir(pages_dir):
        return out
    for page_name in sorted(os.listdir(pages_dir)):
        page_dir = os.path.join(pages_dir, page_name)
        page_json = os.path.join(page_dir, "page.json")
        if not os.path.isfile(page_json):
            continue
        try:
            display = _load_json(page_json).get("displayName", page_name)
        except (json.JSONDecodeError, OSError):
            display = page_name
        main = slicer = 0
        vis_dir = os.path.join(page_dir, "visuals")
        if os.path.isdir(vis_dir):
            for v in sorted(os.listdir(vis_dir)):
                vj = os.path.join(vis_dir, v, "visual.json")
                if not os.path.isfile(vj):
                    continue
                try:
                    vtype = (_load_json(vj).get("visual") or {}).get("visualType")
                except (json.JSONDecodeError, OSError):
                    vtype = None
                if vtype == "slicer":
                    slicer += 1
                else:
                    main += 1
        out[display] = {"main": main, "slicer": slicer}
    return out


# =============================================================================
# Context: run the orchestrator once, gather everything the targets need
# =============================================================================
class BundleContext:
    def __init__(self, source_dir, output_dir):
        self.source_dir = source_dir
        self.output_dir = output_dir
        self.report = None
        self.gen_error = None
        self.models = {}        # name -> model dir
        self.reports = []       # report dirs

    def run(self):
        try:
            self.report = _me.migrate_estate(_me.LocalFilesSource(self.source_dir), self.output_dir)
        except Exception as exc:  # noqa: BLE001 -- target 1 records the throw as a hard failure
            self.gen_error = f"{type(exc).__name__}: {exc}"
            return self
        self.models = _model_dirs(self.output_dir)
        self.reports = _report_dirs(self.output_dir)
        return self

    def datasource_details(self):
        return (self.report or {}).get("datasources", [])

    def workbook_details(self):
        return (self.report or {}).get("workbooks", [])


# =============================================================================
# Targets
# =============================================================================
def target_1_bundle_generates(ctx):
    r = TargetResult("1", "Bundle generates without throwing")
    if ctx.gen_error is not None:
        return r.fail(f"migrate_estate raised: {ctx.gen_error}")
    if not os.path.isfile(os.path.join(ctx.output_dir, "report.json")):
        return r.fail("no report.json written to the bundle")
    errored = [d["name"] for d in ctx.datasource_details() if d.get("status") == "error"]
    errored += [d["name"] for d in ctx.workbook_details() if d.get("viz_status") == "error"]
    if errored:
        return r.fail(f"assets errored during generation: {errored}")
    fallbacks = [d["name"] for d in ctx.datasource_details() if d.get("status") == "fallback"]
    if fallbacks:
        r.note(f"info: {len(fallbacks)} datasource(s) routed to fallback (not an error): {fallbacks}")
    n_models = len(ctx.models)
    n_reports = len(ctx.reports)
    return r.ok(f"bundle generated: {n_models} semantic model(s), {n_reports} report(s)")


def _validate_one_report_structure(report_dir, r):
    """Append diagnostics for one .Report; return True if structurally valid."""
    rel = os.path.basename(report_dir)
    ok = True
    for part in REQUIRED_REPORT_PARTS:
        if not os.path.isfile(os.path.join(report_dir, *part.split("/"))):
            r.fail(f"{rel}: missing required part '{part}'")
            ok = False
    # every JSON part must parse
    for base, _dirs, files in os.walk(report_dir):
        for fn in files:
            if fn.endswith(".json") or fn == ".platform" or fn == "definition.pbir":
                p = os.path.join(base, fn)
                try:
                    _load_json(p)
                except (json.JSONDecodeError, OSError) as exc:
                    r.fail(f"{rel}: part '{os.path.relpath(p, report_dir)}' does not parse: {exc}")
                    ok = False
    # .platform sanity
    try:
        plat = _load_json(os.path.join(report_dir, ".platform"))
        meta = plat.get("metadata") or {}
        if meta.get("type") != "Report":
            r.fail(f"{rel}: .platform metadata.type is {meta.get('type')!r}, expected 'Report'")
            ok = False
        if not meta.get("displayName"):
            r.fail(f"{rel}: .platform metadata.displayName missing")
            ok = False
        if (plat.get("config") or {}).get("logicalId") is None:
            r.fail(f"{rel}: .platform config.logicalId missing")
            ok = False
    except (json.JSONDecodeError, OSError, AttributeError):
        ok = False  # already reported above
    # definition.pbir must carry a datasetReference.byPath.path
    if _read_pbir_bypath(report_dir) is None:
        r.fail(f"{rel}: definition.pbir has no datasetReference.byPath.path")
        ok = False
    # pages.json <-> page.json <-> visual.json cross-references
    try:
        pages = _load_json(os.path.join(report_dir, "definition", "pages", "pages.json"))
        order = pages.get("pageOrder")
        active = pages.get("activePageName")
        if not isinstance(order, list) or not order:
            r.fail(f"{rel}: pages.json pageOrder is empty/invalid")
            ok = False
            order = order if isinstance(order, list) else []
        if active not in order:
            r.fail(f"{rel}: pages.json activePageName {active!r} not in pageOrder")
            ok = False
        for page in order:
            page_dir = os.path.join(report_dir, "definition", "pages", page)
            pj = os.path.join(page_dir, "page.json")
            if not os.path.isfile(pj):
                r.fail(f"{rel}: page '{page}' in pageOrder has no page.json")
                ok = False
                continue
            pjd = _load_json(pj)
            if pjd.get("name") != page:
                r.fail(f"{rel}: page '{page}' page.json name is {pjd.get('name')!r}")
                ok = False
            if not pjd.get("displayName"):
                r.fail(f"{rel}: page '{page}' page.json has no displayName")
                ok = False
            visuals = []
            vis_dir = os.path.join(page_dir, "visuals")
            if os.path.isdir(vis_dir):
                visuals = [v for v in os.listdir(vis_dir)
                           if os.path.isfile(os.path.join(vis_dir, v, "visual.json"))]
            if not visuals:
                r.fail(f"{rel}: page '{page}' has no visual.json parts")
                ok = False
            for v in visuals:
                vjd = _load_json(os.path.join(vis_dir, v, "visual.json"))
                for key in ("name", "position", "visual"):
                    if key not in vjd:
                        r.fail(f"{rel}: visual '{v}' missing key '{key}'")
                        ok = False
                if "visualType" not in (vjd.get("visual") or {}):
                    r.fail(f"{rel}: visual '{v}' has no visual.visualType")
                    ok = False
    except (json.JSONDecodeError, OSError) as exc:
        r.fail(f"{rel}: page tree invalid: {exc}")
        ok = False
    return ok


def target_2_pbir_structure(ctx):
    r = TargetResult("2", "PBIR structural validity")
    built = sum(1 for d in ctx.workbook_details() if d.get("viz_status") == "built")
    if not ctx.reports:
        if built:
            return r.fail(f"report.json says {built} workbook(s) built but no .Report folders on disk")
        return r.na("no reports emitted (no workbooks with supported visuals)")
    all_ok = True
    for rd in ctx.reports:
        if not _validate_one_report_structure(rd, r):
            all_ok = False
    if all_ok:
        r.ok(f"{len(ctx.reports)} report(s) structurally valid")
    return r


def _model_structure_issues(model_dir):
    """Return a list of reasons an emitted ``*.SemanticModel`` would fail to open (empty == sound).

    Goes a level deeper than mere file-presence so a referenced-but-malformed model (a real
    "won't open in Power BI Desktop" cause) is caught rather than passed.
    """
    issues = []
    try:
        plat = _load_json(os.path.join(model_dir, ".platform"))
        ptype = (plat.get("metadata") or {}).get("type")
        if ptype != "SemanticModel":
            issues.append(f".platform metadata.type is {ptype!r}, expected 'SemanticModel'")
    except (json.JSONDecodeError, OSError, AttributeError):
        issues.append(".platform missing or does not parse")
    try:
        _load_json(os.path.join(model_dir, "definition.pbism"))
    except (json.JSONDecodeError, OSError):
        issues.append("definition.pbism missing or does not parse")
    tables = parse_model_tables(model_dir)
    if not [t for t in tables if t != "_Measures"]:
        issues.append("no parseable data tables under definition/tables")
    return issues


def _find_pbip(output_dir):
    """Return ``[(path, parsed_json_or_None), ...]`` for every ``*.pbip`` manifest in the bundle.

    Power BI Desktop opens a *project* via its ``.pbip`` manifest; the emitted PBIR/TMDL folders are
    not openable as a project without one. Surfacing its presence (and artifact resolution) is the
    second half of the "won't open in Desktop" check, distinct from byPath resolution.
    """
    hits = []
    for base, _dirs, files in os.walk(output_dir):
        for fn in files:
            if fn.endswith(".pbip"):
                p = os.path.join(base, fn)
                try:
                    hits.append((p, _load_json(p)))
                except (json.JSONDecodeError, OSError):
                    hits.append((p, None))
    return hits


def target_3_model_presence(ctx):
    r = TargetResult("3", "Model presence + byPath reference integrity")
    if not ctx.reports:
        return r.na("no reports emitted -> no dataset references to resolve")
    out_root = os.path.abspath(ctx.output_dir)
    all_ok = True
    for rd in ctx.reports:
        rel = os.path.basename(rd)
        bypath = _read_pbir_bypath(rd)
        if bypath is None:
            r.fail(f"{rel}: no datasetReference.byPath to resolve")
            all_ok = False
            continue
        if os.path.isabs(bypath):
            r.fail(f"{rel}: byPath '{bypath}' is absolute (must be bundle-relative)", tag="bypath-absolute")
            all_ok = False
            continue
        resolved = os.path.abspath(os.path.join(rd, bypath))
        # containment: a resolved reference must stay inside the bundle root
        if os.path.commonpath([resolved, out_root]) != out_root:
            r.fail(f"{rel}: byPath '{bypath}' escapes the bundle root -> {resolved}",
                   tag="bypath-escapes-bundle")
            all_ok = False
        present = (os.path.isdir(resolved)
                   and all(os.path.isfile(os.path.join(resolved, *p.split("/")))
                           for p in REQUIRED_MODEL_PARTS))
        if present:
            issues = _model_structure_issues(resolved)
            if issues:
                all_ok = False
                r.fail(f"{rel}: byPath resolves to '{os.path.relpath(resolved, out_root)}' but that "
                       f"model is structurally invalid (would not open):", tag="model-structure")
                for issue in issues:
                    r.note("    " + issue)
            else:
                r.note(f"ok: {rel}: byPath resolves to a complete model at "
                       f"{os.path.relpath(resolved, out_root)}")
            continue
        # FAIL: pin the exact paths + where the model actually lives + the correct relative path.
        all_ok = False
        leaf = os.path.basename(bypath)
        name = leaf[: -len(".SemanticModel")] if leaf.endswith(".SemanticModel") else leaf
        actual = ctx.models.get(name) or _ci_model_dir(ctx.models, name)
        msg = (f"{rel}: report references byPath '{bypath}', which resolves to "
               f"'{os.path.relpath(resolved, out_root)}' -- but NO semantic model exists there.")
        if actual:
            # the model of this name EXISTS, just at the wrong directory level -> the pure layout bug.
            r.fail(msg, tag="bypath-layout-mismatch")
            correct = os.path.relpath(actual, rd).replace(os.sep, "/")
            r.note(f"    the model named '{name}' actually lives at "
                   f"'{os.path.relpath(actual, out_root)}'; the byPath should be '{correct}'.")
            r.note("    root cause: the PBIR emitter assumes the canonical PBIP layout where "
                   "<name>.Report and <name>.SemanticModel are SIBLINGS (byPath "
                   "'../<name>.SemanticModel', per resources/viz-rebuild.md), but the orchestrator "
                   "nests them under reports/ and semantic_models/. Fix EITHER by emitting the two "
                   "folders as root siblings (preferred, canonical PBIP) OR by rewriting byPath to "
                   "the relative path above.")
        else:
            # NO model of this name exists anywhere -> a genuinely missing referenced model. Do NOT
            # tag this as the known layout bug, so a regression that drops the model fails CI loudly.
            r.fail(msg, tag="missing-model")
            r.tags.add("dataset-name-mismatch")
            r.note(f"    no emitted semantic model is named '{name}' at all (the orchestrator names "
                   f"the dataset after the WORKBOOK file stem, not the bound datasource).")

    # PBIP project openability: Power BI Desktop opens a *.pbip manifest. Without one, the emitted
    # .Report / .SemanticModel folders cannot be opened as a Desktop project regardless of byPath.
    pbips = _find_pbip(ctx.output_dir)
    if not pbips:
        all_ok = False
        r.fail("no *.pbip project manifest anywhere in the bundle -- Power BI Desktop opens a .pbip, "
               "so the emitted .Report / .SemanticModel folders cannot be opened as a project as-is.",
               tag="no-pbip")
        r.note("    canonical PBIP: a '<Project>.pbip' at the bundle root, e.g. "
               '{"version":"1.0","artifacts":[{"report":{"path":"<name>.Report"}}]}, alongside '
               "sibling '<name>.Report' and '<name>.SemanticModel' folders.")
    else:
        for p, data in pbips:
            rel_p = os.path.relpath(p, out_root)
            if data is None:
                all_ok = False
                r.fail(f"{rel_p}: .pbip does not parse as JSON", tag="pbip-invalid")
                continue
            artifacts = data.get("artifacts") or []
            report_refs = [((a.get("report") or {}).get("path")) for a in artifacts]
            report_refs = [ref for ref in report_refs if ref]
            if not report_refs:
                all_ok = False
                r.fail(f"{rel_p}: .pbip lists no report artifact (artifacts[].report.path)",
                       tag="pbip-no-artifact")
                continue
            for ref in report_refs:
                if not os.path.isdir(os.path.join(os.path.dirname(p), *ref.split("/"))):
                    all_ok = False
                    r.fail(f"{rel_p}: artifact report path '{ref}' does not resolve to an emitted "
                           f".Report folder", tag="pbip-dangling-artifact")
    if all_ok:
        r.ok("every report's byPath resolves to a complete semantic model; .pbip project manifest "
             "present and its artifacts resolve")
    return r


def _ci_model_dir(models, name):
    hit = _ci_lookup(name, models.keys())
    return models.get(hit) if hit else None


def target_4_binding_integrity(ctx):
    r = TargetResult("4", "Binding integrity")
    if not ctx.reports:
        return r.na("no reports emitted -> no visual bindings to resolve")
    all_ok = True
    any_checked = False
    for rd in ctx.reports:
        rel = os.path.basename(rd)
        bypath = _read_pbir_bypath(rd)
        model_dir = None
        if bypath:
            cand = os.path.abspath(os.path.join(rd, bypath))
            if os.path.isdir(cand):
                model_dir = cand
        used_fallback = False
        if model_dir is None:
            # byPath dangles (the target-3 bug): fall back to resolving the model by its leaf name so
            # binding integrity is still checked -- but say so, so a PASS is never read as "it opens".
            leaf = os.path.basename(bypath or "")
            name = leaf[: -len(".SemanticModel")] if leaf.endswith(".SemanticModel") else leaf
            model_dir = ctx.models.get(name) or _ci_model_dir(ctx.models, name)
            used_fallback = model_dir is not None
        if model_dir is None:
            r.fail(f"{rel}: dataset reference resolves to NO emitted model -> all bindings dangle")
            all_ok = False
            continue
        if used_fallback:
            r.note(f"note: {rel}: byPath dangled; bindings checked against the model resolved by name "
                   f"('{os.path.relpath(model_dir, ctx.output_dir)}'). Does NOT imply the report opens.")
        tables = parse_model_tables(model_dir)
        dangling = []
        report_main_visuals = 0
        report_bindings = 0
        for base, _dirs, files in os.walk(os.path.join(rd, "definition", "pages")):
            for fn in files:
                if fn != "visual.json":
                    continue
                vjd = _load_json(os.path.join(base, fn))
                vtype = (vjd.get("visual") or {}).get("visualType")
                if vtype != "slicer":
                    report_main_visuals += 1
                bindings = []
                collect_bindings(vjd, bindings)
                report_bindings += len(bindings)
                vname = vjd.get("name", fn)
                for kind, entity, prop in bindings:
                    any_checked = True
                    tname = _ci_lookup(entity, tables.keys())
                    if tname is None:
                        dangling.append(f"{vname}: {kind} -> entity '{entity}' is not an emitted table")
                        continue
                    pool = tables[tname]["measures"] if kind == "Measure" else tables[tname]["columns"]
                    if _ci_lookup(prop, pool) is None:
                        kindword = "measure" if kind == "Measure" else "column"
                        dangling.append(
                            f"{vname}: {kind} -> '{entity}'[{prop}] has no matching {kindword} "
                            f"in table '{tname}'")
        if report_main_visuals and report_bindings == 0:
            # the visuals exist but bind to nothing -> empty shells; the source viz was not reproduced
            all_ok = False
            r.fail(f"{rel}: {report_main_visuals} non-slicer visual(s) carry ZERO field bindings "
                   f"-- empty shells, the source visuals were not reproduced.", tag="no-bindings")
        elif dangling:
            all_ok = False
            r.fail(f"{rel}: {len(dangling)} dangling binding(s):")
            for d in dangling:
                r.note("    " + d)
        else:
            r.note(f"ok: {rel}: all visual bindings resolve to emitted tables/columns/measures")
    if all_ok:
        r.ok("every visual binding resolves" + ("" if any_checked else " (no bindings present)"))
    return r


def _expected_report_layout(ir):
    """Mirror the documented PBIR placement rules -> ``[(display_name, n_main_visuals), ...]``.

    One page per dashboard that has >=1 supported worksheet zone (visuals == supported zones), plus
    one page per supported worksheet not placed on any dashboard (1 visual). Used only as the
    generic, source-derived expectation; the committed pytest also asserts hand-authored counts.
    """
    ws_by_name = {w["name"]: w for w in ir["worksheets"]}
    pages, placed = [], set()
    for db in ir["dashboards"]:
        supported = []
        for z in db["zones"]:
            w = ws_by_name.get(z["worksheet"])
            if w and w["visual_type"] != VT_UNSUPPORTED:
                supported.append(z["worksheet"])
                placed.add(z["worksheet"])
        if supported:
            pages.append((db["name"], len(supported)))
    for w in ir["worksheets"]:
        if w["name"] in placed or w["visual_type"] == VT_UNSUPPORTED:
            continue
        pages.append((w["name"], 1))
    return pages


def _read_source(path):
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def target_5_faithfulness(ctx):
    r = TargetResult("5", "Faithfulness counts (source <-> bundle)")
    checked = False
    all_ok = True

    # -- datasource side: tables == relations, measures == translated+stubbed calcs --------------
    for d in ctx.datasource_details():
        if d.get("status") not in ("migrated", "migrated_with_followups"):
            continue
        src_id = d.get("source_id")
        out_folder = d.get("output_folder")
        if not src_id or not out_folder or not os.path.isfile(src_id):
            continue
        checked = True
        desc = parse_tds(_read_source(src_id))
        exp_tables = [rel for rel in desc.get("relations", [])
                      if rel.get("kind") in ("table", "custom_sql") and rel.get("columns")]
        calcs, _ = extract_calculations(_read_source(src_id))
        model_dir = os.path.join(ctx.output_dir, *out_folder.split("/"))
        tables = parse_model_tables(model_dir)
        aux = generated_date_tables(model_dir)
        emitted_tables = [t for t in tables if t != "_Measures" and t not in aux]
        emitted_measures = len(tables.get("_Measures", {}).get("measures", set()))
        name = d.get("name")
        if len(emitted_tables) != len(exp_tables):
            all_ok = False
            r.fail(f"{name}: emitted {len(emitted_tables)} table(s) but source has "
                   f"{len(exp_tables)} relation(s): emitted={sorted(emitted_tables)}")
        if emitted_measures != len(calcs):
            all_ok = False
            r.fail(f"{name}: emitted {emitted_measures} measure(s) but source has "
                   f"{len(calcs)} measure calc(s)")
        translated = d.get("measures_translated", 0)
        stubbed = d.get("measures_stubbed", 0)
        if translated + stubbed != emitted_measures:
            all_ok = False
            r.fail(f"{name}: report says {translated} translated + {stubbed} stubbed != "
                   f"{emitted_measures} emitted measure(s)")
        if all_ok:
            r.note(f"ok: {name}: {len(emitted_tables)} table(s), {emitted_measures} measure(s) "
                   f"({translated} translated / {stubbed} stubbed) match source")

    # -- workbook side: pages + per-page visuals == source viz layout (no silent drop) -----------
    built = {os.path.basename(d.get("output_folder") or ""): d
             for d in ctx.workbook_details()
             if d.get("viz_status") == "built" and d.get("output_folder")}
    for rd in ctx.reports:
        d = built.get(os.path.basename(rd))
        if not d or not d.get("source_id") or not os.path.isfile(d["source_id"]):
            continue
        checked = True
        ir = parse_twb(_read_source(d["source_id"]))
        expected = _expected_report_layout(ir)
        emitted = _page_visual_counts(rd)
        name = d.get("name")
        if len(emitted) != len(expected):
            all_ok = False
            r.fail(f"{name}: emitted {len(emitted)} page(s) but source layout expects "
                   f"{len(expected)} (emitted displays={sorted(emitted)})")
        exp_by_display = {disp: n for disp, n in expected}
        for disp, n_main in exp_by_display.items():
            got = emitted.get(disp)
            if got is None:
                all_ok = False
                r.fail(f"{name}: expected page '{disp}' not emitted (worksheet/dashboard dropped)")
            elif got["main"] != n_main:
                all_ok = False
                r.fail(f"{name}: page '{disp}' emitted {got['main']} visual(s), expected {n_main}")
        extra = set(emitted) - set(exp_by_display)
        if extra:
            all_ok = False
            r.fail(f"{name}: unexpected page(s) emitted: {sorted(extra)}")
        if all_ok:
            r.note(f"ok: {name}: {len(emitted)} page(s) with matching per-page visual counts")

    if not checked:
        return r.na("no migrated datasources or built reports to reconcile")
    if all_ok:
        r.ok("bundle counts faithfully mirror the source")
    return r


# -- cross-DB extraction (validator parses the SOURCE federated .tds itself) -------------------
def _crossdb_expectation(tds_text):
    """Parse a federated .tds -> per-side connection classes + join-key pairs from <relationships>.

    Independent of the skill: reads <named-connection> classes, the relation->connection mapping,
    and each <relationship>'s <expression op='='> operand fields. Returns None if the datasource is
    not a multi-connection federation (so the target is N/A for ordinary datasources).
    """
    try:
        root = ET.fromstring(tds_text.lstrip("\ufeff"))
    except ET.ParseError:
        return None

    def local(tag):
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    def itertag(name):
        return [e for e in root.iter() if local(e.tag) == name]

    # named-connection name -> inner connection class
    class_by_conn = {}
    for nc in itertag("named-connection"):
        inner = [c for c in list(nc) if local(c.tag) == "connection"]
        if inner:
            class_by_conn[nc.get("name")] = inner[0].get("class")
    if len(class_by_conn) <= 1:
        return None

    # relation alias (name) -> connection class
    side_classes = {}
    for rel in itertag("relation"):
        if rel.get("connection") and rel.get("name"):
            side_classes[rel.get("name")] = class_by_conn.get(rel.get("connection"))

    join_keys = []
    for rship in itertag("relationship"):
        eqs = [e for e in rship.iter()
               if local(e.tag) == "expression" and (e.get("op") or "") == "="]
        for eq in eqs:
            operands = [c.get("op") for c in list(eq) if local(c.tag) == "expression"]
            fields = [_strip_brackets(o) for o in operands if o]
            if len(fields) == 2:
                join_keys.append(tuple(fields))
    return {"side_classes": side_classes, "join_keys": join_keys,
            "named_connection_count": len(class_by_conn)}


def _strip_brackets(name):
    name = (name or "").strip()
    if name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return name


def _norm_col(s):
    """Normalize an identifier for case/punctuation-insensitive comparison (mirrors clean_col shape
    without importing the producer): runs of non-alphanumerics collapse to one '_', then casefold."""
    return re.sub(r"[^0-9A-Za-z]+", "_", s or "").strip("_").casefold()


_REL_CAPTION_SUFFIX = re.compile(r"^(?P<base>.+?)\s*\([^()]*\)$")


def _col_match_candidates(col):
    """Normalized forms a join-key operand may take among EMITTED model columns.

    A Tableau relationship operand can carry a disambiguating rename caption -- e.g. ``Region
    (people)`` -- while the emitted Power BI column keeps the source (remote) name ``Region``. So,
    mirroring the producer's ``_resolve_rel_column``, try the verbatim normalized name AND, when the
    operand ends in a parenthetical caption, the base name with that caption stripped.
    """
    base = _strip_brackets(col)
    cands = [_norm_col(base)]
    m = _REL_CAPTION_SUFFIX.match(base or "")
    if m:
        cands.append(_norm_col(m.group("base")))
    return [c for c in cands if c]


def _verify_crossdb_relationships(model_dir, join_keys):
    """Return ``(n_relationships, [issue, ...])`` for an emitted cross-DB model.

    Holds a rebuilt cross-DB model to the real bar: a ``relationships.tmdl`` part with at least one
    relationship per source join key, AND every join-key column must (a) exist as an emitted model
    column and (b) be referenced in the relationships part -- compared case/punctuation-insensitively
    so the ``[Order_ID]``/``[ORDER_ID]`` case-mismatch collapses to one column and the renamed
    ``[Region (people)]``/``[Region]`` keys both resolve. Prevents a false PASS from bogus relationships.
    """
    rel_part = os.path.join(model_dir, "definition", "relationships.tmdl")
    if not os.path.isfile(rel_part):
        return 0, ["no definition/relationships.tmdl emitted -- joins were not turned into "
                   "model relationships"]
    with open(rel_part, encoding="utf-8") as fh:
        rel_text = fh.read()
    n_rel = len(re.findall(r"(?m)^\s*relationship\b", rel_text))
    issues = []
    if n_rel < len(join_keys):
        issues.append(f"emitted {n_rel} relationship(s) but source has {len(join_keys)} "
                      f"join-key pair(s)")
    model_cols = set()
    for info in parse_model_tables(model_dir).values():
        for c in info["columns"]:
            model_cols.add(_norm_col(c))
    rel_norm = _norm_col(rel_text)
    for a, b in join_keys:
        for col in (a, b):
            cands = _col_match_candidates(col)
            if not cands:
                continue
            if not any(n in model_cols for n in cands):
                issues.append(f"join column [{col}] has no matching emitted model column")
            elif not any(n in rel_norm for n in cands):
                issues.append(f"join column [{col}] is not referenced in relationships.tmdl")
    return n_rel, issues


def target_6_crossdb(ctx):
    r = TargetResult("6", "Cross-DB storage-mode-agnostic model relationships")
    crossdb = None
    for d in ctx.datasource_details():
        src_id = d.get("source_id")
        if not src_id or not os.path.isfile(src_id):
            continue
        exp = _crossdb_expectation(_read_source(src_id))
        if exp is not None:
            crossdb = (d, exp)
            break
    if crossdb is None:
        return r.na("no multi-connection (cross-DB federated) datasource in this estate")

    d, exp = crossdb
    name = d.get("name")
    n_sides = exp["named_connection_count"]
    n_keys = len(exp["join_keys"])
    r.note(f"source '{name}': {n_sides} named connections "
           f"({_fmt_sides(exp['side_classes'])}); {n_keys} join-key pair(s): "
           f"{_fmt_keys(exp['join_keys'])}")

    status = d.get("status")
    out_folder = d.get("output_folder")
    model_dir = os.path.join(ctx.output_dir, *out_folder.split("/")) if out_folder else None

    if status == "fallback" or not model_dir or not os.path.isdir(model_dir):
        r.fail(f"{name}: the cross-DB federation was routed to '{d.get('reason') or status}' -- it "
               f"collapses to land-to-Delta + DirectLake, so NO semantic model, NO per-side source "
               f"descriptors, and NO model relationships are emitted.", tag="crossdb-fallback")
        r.note(f"    expected (storage-mode-agnostic): {n_sides} per-side source descriptors "
               f"{sorted(exp['side_classes'].values())} and {n_keys} MODEL RELATIONSHIP(S) for the "
               f"join keys {_fmt_keys(exp['join_keys'])} (incl. case-mismatch / renamed keys).")
        return r

    # If a model IS emitted, hold it to the full bar: relationships that faithfully reflect the
    # source join graph (one per join key, endpoints tied to real emitted columns, case-insensitive).
    n_rel, issues = _verify_crossdb_relationships(model_dir, exp["join_keys"])
    if issues:
        r.fail(f"{name}: a model was emitted but its relationships do not faithfully reflect the "
               f"source join graph:", tag="crossdb-relationship-mismatch")
        for issue in issues:
            r.note("    " + issue)
        return r
    r.ok(f"{name}: cross-DB joins emitted as {n_rel} model relationship(s) tied to the source join keys")
    return r


def _fmt_sides(side_classes):
    return ", ".join(f"{k}={v}" for k, v in sorted(side_classes.items()))


def _fmt_keys(join_keys):
    return ", ".join(f"[{a}]=[{b}]" for a, b in join_keys) or "(none)"


ALL_TARGETS = (
    target_1_bundle_generates,
    target_2_pbir_structure,
    target_3_model_presence,
    target_4_binding_integrity,
    target_5_faithfulness,
    target_6_crossdb,
)


def validate(source_dir, output_dir=None):
    """Run the full pipeline + all targets. Returns ``(ctx, [TargetResult, ...])``."""
    tmp = None
    if output_dir is None:
        tmp = tempfile.mkdtemp(prefix="tableau_bundle_")
        output_dir = os.path.join(tmp, "bundle")
    ctx = BundleContext(source_dir, output_dir).run()
    results = []
    for i, fn in enumerate(ALL_TARGETS):
        if ctx.gen_error is not None and fn is not target_1_bundle_generates:
            title = (fn.__doc__ or fn.__name__).strip().splitlines()[0]
            res = TargetResult(str(i + 1), title)
            res.na("skipped: bundle did not generate")
            results.append(res)
            continue
        results.append(fn(ctx))
    return ctx, results


# =============================================================================
# Reporting / CLI
# =============================================================================
def format_report(source_dir, output_dir, results):
    lines = ["", "=" * 78,
             "Tableau -> Fabric/PBI  ::  INTEGRATION BUNDLE VALIDATION",
             "=" * 78,
             f"source: {source_dir}",
             f"bundle: {output_dir}",
             "-" * 78]
    counts = {PASS: 0, FAIL: 0, NA: 0}
    for res in results:
        counts[res.status] = counts.get(res.status, 0) + 1
        mark = {PASS: "PASS", FAIL: "FAIL", NA: " NA "}[res.status]
        lines.append(f"[{mark}] Target {res.key}: {res.title}")
        for diag in res.diagnostics:
            lines.append(f"        {diag}")
    lines.append("-" * 78)
    lines.append(f"summary: {counts[PASS]} PASS, {counts[FAIL]} FAIL, {counts[NA]} NA")
    lines.append("=" * 78)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="validate_bundle",
        description="Validate a Tableau->Fabric migration BUNDLE end-to-end (offline).")
    parser.add_argument("-i", "--input", required=True,
                        help="folder of exported Tableau .tds / .twb files")
    parser.add_argument("-o", "--output", default=None,
                        help="bundle output folder (default: a temp dir)")
    parser.add_argument("--json", action="store_true",
                        help="emit the structured result as JSON instead of the text report")
    args = parser.parse_args(argv)

    ctx, results = validate(args.input, args.output)
    if args.json:
        print(json.dumps({"source": args.input, "output": ctx.output_dir,
                          "targets": [r.to_dict() for r in results]}, indent=2))
    else:
        print(format_report(args.input, ctx.output_dir, results))
    return 0 if all(r.status != FAIL for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
