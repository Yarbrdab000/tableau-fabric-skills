"""One-button Tableau -> Microsoft Fabric **estate** orchestrator (offline-first).

This is the single entry point that turns the skill's library of focused generators
(``parse_tds`` -> ``select_storage_mode`` -> ``assemble_import_model`` -> ``write_model_folder``)
into a complete, repeatable estate migration: point at a set of Tableau assets, run one
command, and get a bundle of equivalent Fabric / Power BI semantic models plus a rich,
machine-readable migration report.

It binds ONLY to the existing public pipeline APIs and never re-implements connection,
storage-mode, type, calc, or TMDL logic:

    for each datasource (.tds):
        descriptor = parse_tds(text)
        decision   = select_storage_mode(descriptor)
        parts      = assemble_import_model(descriptor, model_name=, calcs=).parts
        write_model_folder(parts, <Name>.SemanticModel)

    for each workbook (.twb):
        run an OPTIONAL, pluggable viz stage (Stream B's ``twb_to_pbir`` if present, or an
        injected callable) -- never a hard dependency.

Sources are abstracted behind :class:`TableauSource` with two real adapters:

* :class:`LocalFilesSource` -- a folder of exported ``.tds`` / ``.twb`` files (built + tested).
* :class:`LiveTableauSource` -- the documented seam for a live Tableau Server / Cloud
  connection (PAT from Key Vault -> REST + Metadata API). The network surface is defined but
  intentionally NOT implemented in v1.

A :class:`InMemoryTableauSource` fake implements the same contract so the whole orchestrator
is exercised offline, with no files, network, or credentials.

Honesty boundaries are inherited from the cores: column types come from Tableau metadata,
only the safe subset of calcs becomes DAX (everything else stays an inert ``= 0`` stub with the
original formula preserved), and any datasource whose shape is not safe to rebuild directly is
reported as a land-to-Delta + DirectLake *fallback* rather than emitted wrong. No credentials
are read, stored, or written anywhere in the bundle.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timezone

try:  # works whether imported as a package or run with scripts/ on sys.path
    from .connection_to_m import parse_tds
    from .storage_mode import select_storage_mode, FALLBACK_LAND_TO_DELTA
    from .assemble_model import assemble_import_model, write_model_folder
except ImportError:
    from connection_to_m import parse_tds
    from storage_mode import select_storage_mode, FALLBACK_LAND_TO_DELTA
    from assemble_model import assemble_import_model, write_model_folder


# -- source adapters -----------------------------------------------------------
class TableauSource(ABC):
    """Read-only contract the orchestrator drives, independent of WHERE assets live.

    A datasource/workbook *id* is an opaque handle (a file path, a Tableau LUID, an in-memory
    key); :meth:`asset_name` turns it into a human/model-friendly display name. ``read_*``
    returns the raw ``.tds`` / ``.twb`` XML *text* (already decoded; callers must strip any BOM).
    """

    @abstractmethod
    def list_datasources(self):
        """Return a list of datasource ids (stable, sorted by the adapter)."""

    @abstractmethod
    def read_datasource(self, ds_id):
        """Return the ``.tds`` XML text for ``ds_id``."""

    @abstractmethod
    def list_workbooks(self):
        """Return a list of workbook ids (stable, sorted by the adapter)."""

    @abstractmethod
    def read_workbook(self, wb_id):
        """Return the ``.twb`` XML text for ``wb_id``."""

    def asset_name(self, asset_id):
        """Display / model name for an id. Default: the id itself."""
        return str(asset_id)

    def describe(self):
        """A small JSON-serializable description of this source (for the report)."""
        return {"kind": type(self).__name__}


class LocalFilesSource(TableauSource):
    """Enumerate a folder of exported ``.tds`` / ``.twb`` files and hand their text to the pipeline.

    Files are discovered recursively (case-insensitive extension) and read with
    ``encoding="utf-8-sig"`` so Tableau's UTF-8 BOM is consumed transparently. Ids are absolute
    file paths; the display name is the file stem.
    """

    def __init__(self, root):
        self.root = root

    def _discover(self, ext):
        ext = ext.lower()
        found = []
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                if os.path.splitext(fn)[1].lower() == ext:
                    found.append(os.path.join(dirpath, fn))
        return sorted(found)

    def _read(self, path):
        with open(path, "r", encoding="utf-8-sig") as fh:
            return fh.read()

    def list_datasources(self):
        return self._discover(".tds")

    def read_datasource(self, ds_id):
        return self._read(ds_id)

    def list_workbooks(self):
        return self._discover(".twb")

    def read_workbook(self, wb_id):
        return self._read(wb_id)

    def asset_name(self, asset_id):
        return os.path.splitext(os.path.basename(asset_id))[0]

    def describe(self):
        return {"kind": type(self).__name__, "root": str(self.root)}


class InMemoryTableauSource(TableauSource):
    """Offline fake: serve ``.tds`` / ``.twb`` text from in-memory ``{name: xml}`` maps.

    Used by the test suite (and usable as the unit-test double for :class:`LiveTableauSource`)
    so the orchestrator runs end-to-end with no files, network, or credentials.
    """

    def __init__(self, datasources=None, workbooks=None):
        self._datasources = dict(datasources or {})
        self._workbooks = dict(workbooks or {})

    def list_datasources(self):
        return sorted(self._datasources)

    def read_datasource(self, ds_id):
        return self._datasources[ds_id]

    def list_workbooks(self):
        return sorted(self._workbooks)

    def read_workbook(self, wb_id):
        return self._workbooks[wb_id]


class LiveTableauSource(TableauSource):
    """Documented SEAM for a live Tableau Server / Cloud connection -- NOT implemented in v1.

    The orchestrator already runs end-to-end against :class:`LocalFilesSource` /
    :class:`InMemoryTableauSource`; finishing this adapter is the only remaining work to make the
    one-button flow pull straight from a live site. The method surface is fixed here so the rest
    of the pipeline never has to change.

    Intended implementation path (offline-safe seam -- no network calls are made today):

    1. **Authenticate.** Pull a Personal Access Token (PAT name + secret) from Azure Key Vault --
       never inline credentials -- and POST to ``/api/<ver>/auth/signin`` to exchange it for a
       site-scoped credentials token (``X-Tableau-Auth``). Keep the token out of all output.
    2. **List datasources.** GET ``/api/<ver>/sites/<site-id>/datasources`` (paged) -> ids/LUIDs.
    3. **List workbooks.** GET ``/api/<ver>/sites/<site-id>/workbooks`` (paged) -> ids/LUIDs.
    4. **Download each.** GET ``.../datasources/<id>/content`` and ``.../workbooks/<id>/content``;
       a ``.tdsx`` / ``.twbx`` is a zip -- extract the inner ``.tds`` / ``.twb`` (root or
       ``Data/``) and decode as ``utf-8-sig``.
    5. **(Optional) enrich.** Pull lineage / relationship metadata from the Tableau **Metadata
       API** (GraphQL) to feed relationship inference and the report.

    Credentials and on-prem gateway setup stay with the user (security boundary). Until this is
    implemented, every method raises :class:`NotImplementedError`; unit tests substitute
    :class:`InMemoryTableauSource`.
    """

    def __init__(self, server_url=None, site=None, pat_name=None, key_vault_secret=None,
                 api_version="3.21"):
        # Configuration only -- constructing this object performs NO network I/O.
        self.server_url = server_url
        self.site = site
        self.pat_name = pat_name
        self.key_vault_secret = key_vault_secret  # a Key Vault reference, never a literal secret
        self.api_version = api_version

    def _not_implemented(self, what):
        return NotImplementedError(
            f"LiveTableauSource.{what} is a v1 seam: implement Tableau REST/Metadata-API "
            f"access (see the class docstring and resources/orchestration.md). Use "
            f"InMemoryTableauSource or LocalFilesSource for offline runs."
        )

    def list_datasources(self):
        raise self._not_implemented("list_datasources")

    def read_datasource(self, ds_id):
        raise self._not_implemented("read_datasource")

    def list_workbooks(self):
        raise self._not_implemented("list_workbooks")

    def read_workbook(self, wb_id):
        raise self._not_implemented("read_workbook")


# -- calculated-field extraction ----------------------------------------------
def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _strip_brackets(name):
    if name and name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return name


# Viz-stage entry-point names tried (in order) when auto-loading Stream B's module.
_VIZ_ENTRY_POINTS = ("migrate_workbook", "build_pbir", "twb_to_pbir", "build_report")


def extract_calculations(xml_text):
    """Pull measure calculated fields out of ``.tds`` / ``.twb`` XML.

    Returns ``(calcs, skipped)`` where ``calcs`` is a list of ``{"name", "formula"}`` ready to
    hand to ``assemble_import_model(calcs=...)`` and ``skipped`` records every calculated field
    deliberately left out, with a reason -- so nothing disappears silently.

    Calculated fields live as ``<column caption=.. role=..><calculation class=.. formula=../></column>``.
    Only *measure*-role calcs become DAX measures; bins (``class='categorical-bin'``), empty
    formulas, caption-less fields, non-measure (dimension) calcs, and duplicate names are skipped
    and reported. Parsing is namespace-agnostic and tolerant of a leading BOM.
    """
    calcs = []
    skipped = []
    try:
        root = ET.fromstring((xml_text or "").lstrip("\ufeff"))
    except ET.ParseError:
        return calcs, skipped

    seen = set()
    for col in (e for e in root.iter() if _local(e.tag) == "column"):
        calc_el = next((c for c in list(col) if _local(c.tag) == "calculation"), None)
        if calc_el is None:
            continue
        caption = col.get("caption") or _strip_brackets(col.get("name") or "") or ""
        cls = (calc_el.get("class") or "tableau").lower()
        formula = calc_el.get("formula") or ""
        role = (col.get("role") or "measure").lower()

        if cls == "categorical-bin" or not formula.strip():
            skipped.append({"name": caption, "reason": "no formula / bin calculation"})
            continue
        if not caption:
            skipped.append({"name": "", "reason": "calculated field without a caption/name"})
            continue
        if role != "measure":
            skipped.append({"name": caption, "reason": f"non-measure calculated field (role={role})"})
            continue
        if caption in seen:
            skipped.append({"name": caption, "reason": "duplicate calculated-field name"})
            continue
        seen.add(caption)
        calcs.append({"name": caption, "formula": formula})

    return calcs, skipped


# -- orchestration helpers -----------------------------------------------------
_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_folder(name, used):
    """A filesystem-safe, de-duplicated folder base for a model/report name."""
    base = _INVALID_FS.sub("_", name or "").strip().rstrip(".") or "datasource"
    candidate = base
    i = 2
    while candidate.lower() in used:
        candidate = f"{base}_{i}"
        i += 1
    used.add(candidate.lower())
    return candidate


def _table_display(rel):
    return rel.get("name") or rel.get("item") or "Table"


def _eligible_tables(descriptor):
    """Relations that ``assemble_import_model`` will emit as model tables (have columns)."""
    return [r for r in descriptor.get("relations", [])
            if r.get("kind") in ("table", "custom_sql") and r.get("columns")]


def _resolve_viz_stage(injected):
    """Resolve the optional workbook viz stage without ever hard-depending on it.

    An injected callable wins. Otherwise, if a ``twb_to_pbir`` module is importable (Stream B),
    bind the first recognized entry point. Returns a ``callable(twb_text, name) -> dict`` or
    ``None`` when no viz stage is available.
    """
    if injected is not None:
        return injected
    try:
        if importlib.util.find_spec("twb_to_pbir") is None:
            return None
        mod = importlib.import_module("twb_to_pbir")
    except Exception:
        return None
    for fn in _VIZ_ENTRY_POINTS:
        cand = getattr(mod, fn, None)
        if callable(cand):
            return lambda text, name, _c=cand: _c(text, name)
    return None


def _migrate_one_datasource(source, ds_id, sm_dir, used_folders):
    """Drive the full per-datasource pipeline. Returns a report detail dict (never raises)."""
    name = source.asset_name(ds_id)
    detail = {"name": name, "source_id": str(ds_id)}

    try:
        text = source.read_datasource(ds_id)
        descriptor = parse_tds(text)
    except Exception as exc:  # unreadable / malformed asset -> isolate it, keep the estate going
        detail.update(status="error", error=f"{type(exc).__name__}: {exc}")
        return detail

    connector = descriptor.get("connection_class") or None
    calcs, skipped_calcs = extract_calculations(text)
    decision = select_storage_mode(descriptor)
    detail.update(connector=connector, skipped_calcs=skipped_calcs)

    if decision.get("mode") is None:
        detail.update(status="fallback", storage_mode=None, storage_decision=decision,
                      reason=decision.get("rationale"),
                      fallback_path=decision.get("fallback") or FALLBACK_LAND_TO_DELTA)
        return detail

    # Preflight: model-table display names must each map to a distinct, writable TMDL part.
    # Case-insensitive duplicates (same file on Windows) or path-unsafe characters would
    # silently overwrite or nest parts -> refuse rather than emit a broken model.
    disp = [_table_display(r) for r in _eligible_tables(descriptor)]
    lowered = [d.lower() for d in disp]
    dups = sorted({d for d in disp if lowered.count(d.lower()) > 1})
    unsafe = sorted({d for d in disp if _INVALID_FS.search(d)})
    if dups or unsafe:
        problems = []
        if dups:
            problems.append(f"duplicate table display names {dups}")
        if unsafe:
            problems.append(f"path-unsafe table display names {unsafe}")
        detail.update(status="error", storage_decision=decision,
                      error="; ".join(problems) + "; cannot emit a clean model")
        return detail

    try:
        out = assemble_import_model(descriptor, model_name=name, calcs=calcs)
    except ValueError as exc:  # storage policy / no-columns -> documented land-to-Delta fallback
        detail.update(status="fallback", storage_mode=None, storage_decision=decision,
                      reason=str(exc),
                      fallback_path=decision.get("fallback") or FALLBACK_LAND_TO_DELTA)
        return detail
    except Exception as exc:
        detail.update(status="error", storage_decision=decision,
                      error=f"{type(exc).__name__}: {exc}")
        return detail

    folder = _safe_folder(name, used_folders) + ".SemanticModel"
    dest = os.path.join(sm_dir, folder)
    try:
        if os.path.isdir(dest):
            shutil.rmtree(dest)  # clear stale parts so a rerun never leaves renamed/dropped tables
        write_model_folder(out["parts"], dest)
    except OSError as exc:
        detail.update(status="error", storage_decision=decision, error=f"write failed: {exc}")
        return detail

    report = out["report"]
    decision = report.get("storage_decision", decision)  # canonical decision from the assembler
    eligible = _eligible_tables(descriptor)
    measures = report.get("measures", [])
    translated = sum(1 for m in measures if m.get("status") == "translated")
    stubbed = sum(1 for m in measures if m.get("status") == "stub")
    fully = bool(decision.get("fully_supported"))

    detail.update(
        status="migrated" if fully else "migrated_with_followups",
        fully_supported=fully,
        storage_mode=decision.get("mode"),
        storage_decision=decision,
        m_connector=decision.get("connector"),
        output_folder=f"semantic_models/{folder}",
        tables=report.get("tables", []),
        skipped_tables=report.get("skipped_tables", []),
        table_count=len(report.get("tables", [])),
        column_count=sum(len(r.get("columns", [])) for r in eligible),
        measures=measures,
        measures_translated=translated,
        measures_stubbed=stubbed,
        manual_followups=decision.get("manual_followups", []),
    )
    return detail


def _migrate_one_workbook(source, wb_id, viz, reports_dir, used_folders):
    """Run the optional viz stage for one workbook. Returns a report detail dict (never raises)."""
    name = source.asset_name(wb_id)
    detail = {"name": name, "source_id": str(wb_id)}

    try:
        text = source.read_workbook(wb_id)
    except Exception as exc:
        detail.update(viz_status="error", note=f"{type(exc).__name__}: {exc}")
        return detail

    if viz is None:
        detail.update(viz_status="warned",
                      note="viz stage not available (no twb_to_pbir module and no injected stage)")
        return detail

    try:
        result = viz(text, name) or {}
    except Exception as exc:
        detail.update(viz_status="error", note=f"viz stage failed: {type(exc).__name__}: {exc}")
        return detail

    parts = result.get("parts") if isinstance(result, dict) else None
    output_folder = None
    if parts:
        folder = _safe_folder(name, used_folders) + ".Report"
        dest = os.path.join(reports_dir, folder)
        try:
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            write_model_folder(parts, dest)
            output_folder = f"reports/{folder}"
        except OSError as exc:
            detail.update(viz_status="error", note=f"viz write failed: {exc}")
            return detail

    detail.update(viz_status="built",
                  note=result.get("note") if isinstance(result, dict) else None,
                  output_folder=output_folder)
    return detail


def migrate_estate(source, output_dir, *, viz_stage=None):
    """Run the whole estate migration and write the output bundle. Returns the report dict.

    ``source`` is any :class:`TableauSource`. ``output_dir`` receives::

        <output_dir>/semantic_models/<Name>.SemanticModel/...   one per migrated datasource
        <output_dir>/reports/<Name>.Report/...                  only if a viz stage emits parts
        <output_dir>/report.json                                rich, machine-readable result
        <output_dir>/summary.md                                 human-readable summary

    ``viz_stage`` (optional) is a ``callable(twb_text, name) -> dict`` plugged in for workbook
    viz rebuild; when omitted the orchestrator auto-detects Stream B's ``twb_to_pbir`` if present
    and otherwise records each workbook as ``warned``. The run is resilient: a single bad asset is
    isolated as an ``error`` detail rather than aborting the bundle.
    """
    sm_dir = os.path.join(output_dir, "semantic_models")
    reports_dir = os.path.join(output_dir, "reports")
    os.makedirs(output_dir, exist_ok=True)

    viz = _resolve_viz_stage(viz_stage)
    used_folders = set()

    ds_details = [_migrate_one_datasource(source, ds_id, sm_dir, used_folders)
                  for ds_id in source.list_datasources()]
    wb_details = [_migrate_one_workbook(source, wb_id, viz, reports_dir, used_folders)
                  for wb_id in source.list_workbooks()]

    summary = _summarize(ds_details, wb_details, viz is not None)
    fallbacks = [
        {"datasource": d["name"],
         "source_id": d.get("source_id"),
         "reason": d.get("reason"),
         "fallback_path": d.get("fallback_path") or FALLBACK_LAND_TO_DELTA}
        for d in ds_details if d.get("status") == "fallback"
    ]

    report = {
        "tool": "migrate_estate",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source.describe(),
        "summary": summary,
        "datasources": ds_details,
        "workbooks": wb_details,
        "fallbacks": fallbacks,
    }

    with open(os.path.join(output_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    with open(os.path.join(output_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(_render_summary_md(report))
    return report


def _summarize(ds_details, wb_details, viz_available):
    """Roll per-asset details up into the report's machine-readable ``summary`` block."""
    modes = {"Import": 0, "DirectQuery": 0, "fallback": 0}
    connectors = set()
    migrated = partial = fallback = error = 0
    tables = columns = measures_total = measures_translated = measures_stubbed = 0

    for d in ds_details:
        if d.get("connector"):
            connectors.add(d["connector"])
        status = d.get("status")
        if status in ("migrated", "migrated_with_followups"):
            migrated += 1
            if status == "migrated_with_followups":
                partial += 1
            mode = d.get("storage_mode")
            if mode in modes:
                modes[mode] += 1
            tables += d.get("table_count", 0)
            columns += d.get("column_count", 0)
            measures_total += len(d.get("measures", []))
            measures_translated += d.get("measures_translated", 0)
            measures_stubbed += d.get("measures_stubbed", 0)
        elif status == "fallback":
            fallback += 1
            modes["fallback"] += 1
        else:
            error += 1

    wb_built = sum(1 for w in wb_details if w.get("viz_status") == "built")
    wb_warned = sum(1 for w in wb_details if w.get("viz_status") == "warned")
    wb_error = sum(1 for w in wb_details if w.get("viz_status") == "error")

    return {
        "datasources_total": len(ds_details),
        "datasources_migrated": migrated,
        "datasources_partial": partial,
        "datasources_fallback": fallback,
        "datasources_error": error,
        "tables_translated": tables,
        "columns_translated": columns,
        "measures_total": measures_total,
        "measures_translated": measures_translated,
        "measures_stubbed": measures_stubbed,
        "workbooks_total": len(wb_details),
        "workbooks_viz_built": wb_built,
        "workbooks_viz_warned": wb_warned,
        "workbooks_viz_error": wb_error,
        "connectors_seen": sorted(connectors),
        "storage_modes": modes,
        "viz_stage_available": viz_available,
    }


def _render_summary_md(report):
    """Render the human-readable ``summary.md`` from the report dict."""
    s = report["summary"]
    lines = [
        "# Tableau -> Fabric Estate Migration Report",
        "",
        f"_Generated {report['generated_at']} by `{report['tool']}` "
        f"from {report['source'].get('kind')}._",
        "",
        "## Summary",
        "",
        f"- **Datasources:** {s['datasources_total']} total -> "
        f"{s['datasources_migrated']} migrated "
        f"({s['datasources_partial']} need manual follow-ups), "
        f"{s['datasources_fallback']} fallback, {s['datasources_error']} error",
        f"- **Tables:** {s['tables_translated']} | **Columns:** {s['columns_translated']}",
        f"- **Measures:** {s['measures_total']} total -> "
        f"{s['measures_translated']} translated, {s['measures_stubbed']} stubbed",
        f"- **Storage modes:** Import {s['storage_modes']['Import']}, "
        f"DirectQuery {s['storage_modes']['DirectQuery']}, "
        f"fallback {s['storage_modes']['fallback']}",
        f"- **Connectors seen:** {', '.join(s['connectors_seen']) or '(none)'}",
        f"- **Workbooks:** {s['workbooks_total']} total -> "
        f"{s['workbooks_viz_built']} viz built, {s['workbooks_viz_warned']} warned, "
        f"{s['workbooks_viz_error']} error "
        f"(viz stage {'available' if s['viz_stage_available'] else 'not available'})",
        "",
        "## Datasources",
        "",
        "| Datasource | Status | Mode | Tables | Columns | Measures (tr/stub) | Output |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in report["datasources"]:
        meas = f"{d.get('measures_translated', 0)}/{d.get('measures_stubbed', 0)}"
        lines.append(
            f"| {d['name']} | {d.get('status', '')} | {d.get('storage_mode') or '-'} "
            f"| {d.get('table_count', 0)} | {d.get('column_count', 0)} | {meas} "
            f"| {d.get('output_folder') or '-'} |"
        )

    if report["fallbacks"]:
        lines += ["", "## Fallbacks (route to land-to-Delta + DirectLake)", ""]
        for f in report["fallbacks"]:
            lines.append(f"- **{f['datasource']}** ({f['fallback_path']}): {f['reason']}")

    if report["workbooks"]:
        lines += ["", "## Workbooks", "", "| Workbook | Viz | Note |", "|---|---|---|"]
        for w in report["workbooks"]:
            lines.append(f"| {w['name']} | {w.get('viz_status', '')} | {w.get('note') or ''} |")

    lines += [
        "",
        "## Audit guarantees",
        "",
        "- Column types come from the Tableau source schema, never inferred.",
        "- Every calculated field's original formula is preserved as a `TableauFormula` "
        "annotation; translated measures carry `TranslatedBy`, stubs stay inert `= 0`.",
        "- Fallback datasources are listed with a reason; nothing is emitted wrong silently.",
        "- No credentials are read, stored, or written anywhere in this bundle.",
        "",
    ]
    return "\n".join(lines)


# -- CLI -----------------------------------------------------------------------
def main(argv=None):
    """One-command estate migration over a local folder of ``.tds`` / ``.twb`` files (offline)."""
    parser = argparse.ArgumentParser(
        prog="migrate_estate",
        description="One-button Tableau -> Microsoft Fabric estate migration (offline-first).",
    )
    parser.add_argument("-i", "--input", required=True,
                        help="folder of exported Tableau .tds / .twb files")
    parser.add_argument("-o", "--output", required=True,
                        help="output bundle folder (semantic models + report.json + summary.md)")
    args = parser.parse_args(argv)

    source = LocalFilesSource(args.input)
    report = migrate_estate(source, args.output)
    s = report["summary"]
    print(
        f"Datasources: {s['datasources_migrated']}/{s['datasources_total']} migrated "
        f"({s['datasources_fallback']} fallback, {s['datasources_error']} error) | "
        f"Measures: {s['measures_translated']}/{s['measures_total']} translated | "
        f"Workbooks: {s['workbooks_viz_built']}/{s['workbooks_total']} viz built"
    )
    print(f"Bundle written to: {os.path.abspath(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
