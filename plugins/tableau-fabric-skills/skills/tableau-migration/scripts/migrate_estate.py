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
import inspect
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
    from .assemble_model import assemble_import_model, write_model_folder, write_local_pbip
    from .parameters import parse_parameters
except ImportError:
    from connection_to_m import parse_tds
    from storage_mode import select_storage_mode, FALLBACK_LAND_TO_DELTA
    from assemble_model import assemble_import_model, write_model_folder, write_local_pbip
    from parameters import parse_parameters


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


def _csv_env(value):
    """Split a comma-separated environment value into a clean list (or ``None``)."""
    if not value:
        return None
    items = [part.strip() for part in value.split(",") if part.strip()]
    return items or None


class LiveTableauSource(TableauSource):
    """Documented SEAM for a live Tableau Server / Cloud connection -- network calls NOT built yet.

    The orchestrator already runs end-to-end against :class:`LocalFilesSource` /
    :class:`InMemoryTableauSource`; finishing this adapter is the only remaining work to make the
    one-button flow pull straight from a live site. The method surface is fixed here so the rest
    of the pipeline never has to change, and the *configuration* surface already captures the
    three live concerns the integrator wires up -- without ever holding a secret or a GUID:

    * **Runtime PAT from Key Vault.** The object stores only the *names* needed to fetch a
      Personal Access Token at run time (the vault name, the secret name, the token name). The
      token value is resolved lazily by :meth:`_resolve_pat` and is never an attribute, never
      logged, and never written to the report.
    * **Discovery by NAME.** Assets are targeted by human name (``datasource_names`` /
      ``workbook_names``), not by LUID/GUID, so nothing environment-specific is baked in. The
      pure :meth:`_select_by_name` helper does the matching and *is* implemented and unit-tested;
      only the REST catalog fetch around it is the seam.
    * **Fabric target.** ``fabric_workspace`` records the destination workspace *name* so the
      report/deploy step knows where the bundle is headed.

    Intended implementation path (offline-safe seam -- no network calls are made today):

    1. **Authenticate.** :meth:`_resolve_pat` pulls the PAT secret from Azure Key Vault at run
       time (Azure CLI ``az keyvault secret show`` or ``azure-identity`` +
       ``azure-keyvault-secrets``); :meth:`_signin` POSTs ``tokenName`` + that secret to
       ``/api/<ver>/auth/signin`` and exchanges it for a site-scoped ``X-Tableau-Auth`` token.
       Keep the token out of all output.
    2. **List datasources / workbooks.** GET ``/api/<ver>/sites/<site-id>/datasources`` and
       ``.../workbooks`` (paged) -> a ``[{"id", "name"}, ...]`` catalog, then narrow it with
       :meth:`_select_by_name` against ``datasource_names`` / ``workbook_names``.
    3. **Download each.** GET ``.../datasources/<id>/content`` and ``.../workbooks/<id>/content``;
       a ``.tdsx`` / ``.twbx`` is a zip -- extract the inner ``.tds`` / ``.twb`` (root or
       ``Data/``) and decode as ``utf-8-sig``.
    4. **(Optional) enrich.** Pull lineage / relationship metadata from the Tableau **Metadata
       API** (GraphQL) to feed relationship inference and the report.

    Credentials and on-prem gateway setup stay with the user (security boundary). Until the
    network calls are built, the ``list_*`` / ``read_*`` / auth methods raise
    :class:`NotImplementedError`; unit tests substitute :class:`InMemoryTableauSource`.
    """

    def __init__(self, server_url=None, site=None, *, key_vault_name=None, pat_secret_name=None,
                 pat_name=None, datasource_names=None, workbook_names=None,
                 fabric_workspace=None, api_version="3.21"):
        # Configuration only -- constructing this object performs NO network I/O and holds NO
        # secret material: just the *names* used to fetch a PAT and locate assets at run time.
        # Each value falls back to an environment variable so nothing site-specific is hardcoded.
        self.server_url = server_url or os.environ.get("TABLEAU_SERVER_URL")
        self.site = site or os.environ.get("TABLEAU_SITE")
        self.key_vault_name = key_vault_name or os.environ.get("TABLEAU_MIGRATION_KEYVAULT")
        self.pat_secret_name = pat_secret_name or os.environ.get("TABLEAU_MIGRATION_PAT_SECRET")
        self.pat_name = pat_name or os.environ.get("TABLEAU_MIGRATION_PAT_NAME")
        self.fabric_workspace = fabric_workspace or os.environ.get("FABRIC_WORKSPACE")
        self.datasource_names = (list(datasource_names) if datasource_names is not None
                                 else _csv_env(os.environ.get("TABLEAU_DATASOURCE_NAMES")))
        self.workbook_names = (list(workbook_names) if workbook_names is not None
                               else _csv_env(os.environ.get("TABLEAU_WORKBOOK_NAMES")))
        self.api_version = api_version
        # Populated by the real list_* implementation (catalog id -> display name) so asset_name
        # can report human names; empty until the network seam is built.
        self._name_by_id = {}

    @staticmethod
    def _select_by_name(catalog, wanted_names):
        """Pick assets from a fetched catalog *by name* -- pure, deterministic, no I/O.

        ``catalog`` is an iterable of ``{"id":.., "name":..}`` dicts (what a Tableau REST *list*
        call yields). ``wanted_names`` is the names to keep, matched case-insensitively; an empty
        / ``None`` filter keeps everything. Returns a list of ``(id, name)`` sorted by name then
        id. Entries without an id are skipped; duplicate names each yield their own id.

        This is the implemented heart of "discover by name" -- the real ``list_*`` methods only
        have to supply ``catalog`` from the network and store the resulting id->name map.
        """
        wanted = None
        if wanted_names:
            wanted = {str(n).strip().casefold() for n in wanted_names if str(n).strip()}
            if not wanted:  # an all-blank filter is treated as "keep everything"
                wanted = None
        picked = []
        for entry in catalog:
            cid = entry.get("id")
            if cid is None:
                continue
            name = str(entry.get("name", "")).strip()
            if wanted is None or name.casefold() in wanted:
                picked.append((cid, name))
        picked.sort(key=lambda pair: (pair[1].casefold(), str(pair[0])))
        return picked

    def _not_implemented(self, what):
        return NotImplementedError(
            f"LiveTableauSource.{what} is a seam: implement Tableau REST/Metadata-API access "
            f"(see the class docstring and resources/orchestration.md). Use "
            f"InMemoryTableauSource or LocalFilesSource for offline runs."
        )

    def _resolve_pat(self):
        """SEAM: fetch the PAT *secret* from Azure Key Vault at run time.

        Implement with the Azure CLI already on the box::

            az keyvault secret show --vault-name <self.key_vault_name> \\
                --name <self.pat_secret_name> --query value -o tsv

        or ``azure-identity`` ``DefaultAzureCredential`` + ``azure-keyvault-secrets``
        ``SecretClient``. Return the token string; never log it, never persist it, never place it
        in the report. Raises until implemented.
        """
        raise self._not_implemented("_resolve_pat")

    def _signin(self, pat_secret):
        """SEAM: exchange ``self.pat_name`` + ``pat_secret`` for an ``X-Tableau-Auth`` token."""
        raise self._not_implemented("_signin")

    def list_datasources(self):
        # Real impl: catalog = <GET .../datasources, paged>; then
        #   picked = self._select_by_name(catalog, self.datasource_names)
        #   self._name_by_id.update(dict(picked)); return [cid for cid, _ in picked]
        raise self._not_implemented("list_datasources")

    def read_datasource(self, ds_id):
        raise self._not_implemented("read_datasource")

    def list_workbooks(self):
        # Real impl mirrors list_datasources against .../workbooks and self.workbook_names.
        raise self._not_implemented("list_workbooks")

    def read_workbook(self, wb_id):
        raise self._not_implemented("read_workbook")

    def asset_name(self, asset_id):
        return self._name_by_id.get(asset_id, str(asset_id))

    def describe(self):
        # Names and pointers only -- never the PAT value or any secret/GUID.
        return {
            "kind": type(self).__name__,
            "server_url": self.server_url,
            "site": self.site,
            "key_vault": self.key_vault_name,
            "pat_secret_name": self.pat_secret_name,
            "pat_name": self.pat_name,
            "fabric_workspace": self.fabric_workspace,
            "datasource_names": self.datasource_names,
            "workbook_names": self.workbook_names,
            "api_version": self.api_version,
            "implemented": False,
        }


# -- calculated-field extraction ----------------------------------------------
def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _strip_brackets(name):
    if name and name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return name


# Viz-stage entry-point names tried (in order) when auto-loading Stream B's module.
_VIZ_ENTRY_POINTS = ("migrate_workbook", "migrate_twb_to_pbir", "build_pbir", "build_report")


def extract_calculations(xml_text, *, include_dimensions=False):
    """Pull measure calculated fields out of ``.tds`` / ``.twb`` XML.

    Returns ``(calcs, skipped)`` where ``calcs`` is a list of ``{"name", "formula"}`` ready to
    hand to ``assemble_import_model(calcs=...)`` and ``skipped`` records every calculated field
    deliberately left out, with a reason -- so nothing disappears silently.

    Calculated fields live as ``<column caption=.. role=..><calculation class=.. formula=../></column>``.
    Only *measure*-role calcs become DAX measures; bins (``class='categorical-bin'``), empty
    formulas, caption-less fields, non-measure (dimension) calcs, and duplicate names are skipped
    and reported. Parsing is namespace-agnostic and tolerant of a leading BOM.

    ``include_dimensions`` (opt-in, default off) changes nothing about the measure path: when set,
    dimension-role calcs are no longer dropped into ``skipped`` but collected into a third returned
    list and the return shape becomes ``(calcs, skipped, dim_calcs)`` -- each dim entry is
    ``{"name", "formula", "role"}``, destined for ``translate_tableau_calc_to_column_dax`` as a DAX
    calculated column. The default (``include_dimensions=False``) return shape and contents are
    byte-for-byte unchanged.
    """
    calcs = []
    skipped = []
    dim_calcs = []
    try:
        root = ET.fromstring((xml_text or "").lstrip("\ufeff"))
    except ET.ParseError:
        return (calcs, skipped, dim_calcs) if include_dimensions else (calcs, skipped)

    seen = set()
    for col in (e for e in root.iter() if _local(e.tag) == "column"):
        calc_el = next((c for c in list(col) if _local(c.tag) == "calculation"), None)
        if calc_el is None:
            continue
        caption = col.get("caption") or _strip_brackets(col.get("name") or "") or ""
        cls = (calc_el.get("class") or "tableau").lower()
        formula = calc_el.get("formula") or ""
        role = (col.get("role") or "measure").lower()

        if col.get("param-domain-type") is not None:
            # A Tableau PARAMETER embedded as a column (its `<calculation>` formula is just the
            # default value, e.g. `"Sub Category"`). Parameters are handled by the parameter
            # translator, never emitted as measures -- otherwise they become phantom constants.
            skipped.append({"name": caption, "reason": "Tableau parameter (not a measure)"})
            continue
        if cls == "categorical-bin" or not formula.strip():
            skipped.append({"name": caption, "reason": "no formula / bin calculation"})
            continue
        if not caption:
            skipped.append({"name": "", "reason": "calculated field without a caption/name"})
            continue
        if role != "measure":
            if not include_dimensions:
                skipped.append({"name": caption, "reason": f"non-measure calculated field (role={role})"})
                continue
            if caption in seen:
                skipped.append({"name": caption, "reason": "duplicate calculated-field name"})
                continue
            seen.add(caption)
            dim_calcs.append({"name": caption, "formula": formula, "role": role})
            continue
        if caption in seen:
            skipped.append({"name": caption, "reason": "duplicate calculated-field name"})
            continue
        seen.add(caption)
        calcs.append({"name": caption, "formula": formula})

    return (calcs, skipped, dim_calcs) if include_dimensions else (calcs, skipped)


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


def _viz_adapter(cand):
    """Adapt a viz entry point to the orchestrator's ``callable(twb_text, name) -> dict`` contract.

    Stream B's ``migrate_twb_to_pbir(text, *, report_name, dataset_name)`` takes the target name as
    keyword-only args, while a generic plugin may take ``(text, name)`` positionally. Inspect the
    signature so the workbook display name flows through as the report/dataset name either way.
    """
    try:
        params = set(inspect.signature(cand).parameters)
    except (TypeError, ValueError):
        params = set()
    name_kwargs = {"report_name", "dataset_name"} & params
    def _call(twb_text, name):
        if name_kwargs:
            return cand(twb_text, **{k: name for k in name_kwargs})
        return cand(twb_text, name)
    return _call


def _resolve_viz_stage(injected):
    """Resolve the optional workbook viz stage without ever hard-depending on it.

    An injected callable wins. Otherwise, if a ``twb_to_pbir`` module is importable (Stream B),
    bind the first recognized entry point. Returns a ``callable(twb_text, name) -> dict`` or
    ``None`` when no viz stage is available.
    """
    if injected is not None:
        return injected
    try:  # mirror the package-or-flat import strategy used for the sibling modules above
        from . import twb_to_pbir as mod
    except ImportError:
        try:
            import twb_to_pbir as mod
        except ImportError:
            return None
    for fn in _VIZ_ENTRY_POINTS:
        cand = getattr(mod, fn, None)
        if callable(cand):
            return _viz_adapter(cand)
    return None


def _migrate_one_datasource(source, ds_id, sm_dir, used_folders, pbip_dir=None):
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
    calcs, skipped_calcs, dim_calcs = extract_calculations(text, include_dimensions=True)
    # Thread Tableau parameters into the assembler so parameter-driven swap calcs (e.g. a measure
    # swap over aggregations -> SWITCH over a what-if value table) translate here exactly as they do
    # on the direct migrate_datasource path. Sources without parameters yield [], keeping the default
    # semantic-model output byte-identical.
    try:
        parameters = parse_parameters(text)
    except Exception:
        parameters = []
    decision = select_storage_mode(descriptor)
    detail.update(connector=connector, skipped_calcs=skipped_calcs, dim_calcs=dim_calcs)

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
        out = assemble_import_model(descriptor, model_name=name, calcs=calcs, dim_calcs=dim_calcs,
                                    parameters=parameters)
    except ValueError as exc:  # storage policy / no-columns -> documented land-to-Delta fallback
        detail.update(status="fallback", storage_mode=None, storage_decision=decision,
                      reason=str(exc),
                      fallback_path=decision.get("fallback") or FALLBACK_LAND_TO_DELTA)
        return detail
    except Exception as exc:
        detail.update(status="error", storage_decision=decision,
                      error=f"{type(exc).__name__}: {exc}")
        return detail

    safe_base = _safe_folder(name, used_folders)
    folder = safe_base + ".SemanticModel"
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

    # Additive local deliverable: an openable Power BI project (.pbip) per datasource so users can
    # double-click straight into Power BI Desktop. The semantic_models/ folder written above stays
    # the canonical output (byte-identical); this is a self-contained copy under pbip/<name>/ and
    # never alters it. A pbip write failure is non-fatal -- the model already landed, so the
    # datasource stays "migrated" and only pbip_folder is left None.
    pbip_folder = None
    if pbip_dir is not None:
        ds_pbip_dir = os.path.join(pbip_dir, safe_base)
        try:
            if os.path.isdir(ds_pbip_dir):
                shutil.rmtree(ds_pbip_dir)
            write_local_pbip(out["parts"], ds_pbip_dir, model_name=safe_base,
                             swap_specs=(report.get("field_parameters") or {}).get("specs") or None)
            pbip_folder = f"pbip/{safe_base}/{safe_base}.pbip"
        except OSError:
            pbip_folder = None

    eligible = _eligible_tables(descriptor)
    measures = report.get("measures", [])
    translated = sum(1 for m in measures if m.get("status") == "translated")
    stubbed = sum(1 for m in measures if m.get("status") == "stub")
    calc_columns = report.get("calc_columns", [])
    cc_translated = sum(1 for c in calc_columns if c.get("status") == "translated")
    cc_stubbed = sum(1 for c in calc_columns if c.get("status") == "stub")
    fully = bool(decision.get("fully_supported"))

    detail.update(
        status="migrated" if fully else "migrated_with_followups",
        fully_supported=fully,
        storage_mode=decision.get("mode"),
        storage_decision=decision,
        m_connector=decision.get("connector"),
        output_folder=f"semantic_models/{folder}",
        pbip_folder=pbip_folder,
        translation_handoff=report.get("translation_handoff"),
        tables=report.get("tables", []),
        skipped_tables=report.get("skipped_tables", []),
        table_count=len(report.get("tables", [])),
        column_count=sum(len(r.get("columns", [])) for r in eligible),
        measures=measures,
        measures_translated=translated,
        measures_stubbed=stubbed,
        calc_columns=calc_columns,
        calc_columns_translated=cc_translated,
        calc_columns_stubbed=cc_stubbed,
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


def migrate_estate(source, output_dir, *, viz_stage=None, pbip=True):
    """Run the whole estate migration and write the output bundle. Returns the report dict.

    ``source`` is any :class:`TableauSource`. ``output_dir`` receives::

        <output_dir>/semantic_models/<Name>.SemanticModel/...   one per migrated datasource
        <output_dir>/pbip/<Name>/<Name>.pbip                    openable Power BI project (default)
        <output_dir>/reports/<Name>.Report/...                  only if a viz stage emits parts
        <output_dir>/report.json                                rich, machine-readable result
        <output_dir>/summary.md                                 human-readable summary

    ``viz_stage`` (optional) is a ``callable(twb_text, name) -> dict`` plugged in for workbook
    viz rebuild; when omitted the orchestrator auto-detects Stream B's ``twb_to_pbir`` if present
    and otherwise records each workbook as ``warned``. The run is resilient: a single bad asset is
    isolated as an ``error`` detail rather than aborting the bundle.

    ``pbip`` (default ``True``) additionally writes an openable ``.pbip`` Power BI project per
    migrated datasource under ``pbip/<Name>/`` so it can be opened/tested in Power BI Desktop; the
    canonical ``semantic_models/`` output is unchanged. Set ``pbip=False`` to skip it.
    """
    sm_dir = os.path.join(output_dir, "semantic_models")
    reports_dir = os.path.join(output_dir, "reports")
    pbip_dir = os.path.join(output_dir, "pbip") if pbip else None
    os.makedirs(output_dir, exist_ok=True)

    viz = _resolve_viz_stage(viz_stage)
    used_folders = set()

    ds_details = [_migrate_one_datasource(source, ds_id, sm_dir, used_folders, pbip_dir)
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
    calc_columns_total = calc_columns_translated = calc_columns_stubbed = 0
    needs_review_total = 0

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
            calc_columns_total += len(d.get("calc_columns", []))
            calc_columns_translated += d.get("calc_columns_translated", 0)
            calc_columns_stubbed += d.get("calc_columns_stubbed", 0)
            needs_review_total += len((d.get("translation_handoff") or {}).get("needs_review") or [])
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
        "calc_columns_total": calc_columns_total,
        "calc_columns_translated": calc_columns_translated,
        "calc_columns_stubbed": calc_columns_stubbed,
        "needs_review_total": needs_review_total,
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
        f"- **Calc columns:** {s.get('calc_columns_total', 0)} total -> "
        f"{s.get('calc_columns_translated', 0)} translated, "
        f"{s.get('calc_columns_stubbed', 0)} stubbed",
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

    if any(d.get("pbip_folder") for d in report["datasources"]):
        lines += [
            "",
            "> **Open locally:** each migrated datasource also has an openable Power BI project at "
            "`pbip/<Name>/<Name>.pbip` — double-click to explore and test it in Power BI Desktop.",
        ]

    review = [
        dict(r, datasource=d["name"])
        for d in report["datasources"]
        for r in ((d.get("translation_handoff") or {}).get("needs_review") or [])
    ]
    if review:
        lines += [
            "",
            "## Next step — assisted (second-compiler) translation",
            "",
            f"{len(review)} calculation(s) fell back to inert stubs (the original Tableau formula is "
            "preserved). To translate them, run each through the **second compiler**: author a "
            "candidate DAX, validate it with `check_candidate_dax`, then land the approved set via "
            "`approved_calc_dax` and redeploy. See "
            "[second-compiler.md](resources/second-compiler.md).",
            "",
            "| Datasource | Calculation | Role | Category | Fallback reason | Suggestion ready |",
            "|---|---|---|---|---|---|",
        ]
        for r in review:
            lines.append(
                f"| {r.get('datasource')} | {r.get('name')} | {r.get('role') or '-'} "
                f"| {r.get('category') or '-'} | {r.get('fallback_reason') or '-'} "
                f"| {'yes' if r.get('has_suggestion') else 'no'} |"
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
                        help="output bundle folder (semantic models + pbip + report.json + summary.md)")
    parser.add_argument("--no-pbip", action="store_true",
                        help="skip the openable .pbip projects (emit only semantic_models/ folders)")
    args = parser.parse_args(argv)

    source = LocalFilesSource(args.input)
    report = migrate_estate(source, args.output, pbip=not args.no_pbip)
    s = report["summary"]
    print(
        f"Datasources: {s['datasources_migrated']}/{s['datasources_total']} migrated "
        f"({s['datasources_fallback']} fallback, {s['datasources_error']} error) | "
        f"Measures: {s['measures_translated']}/{s['measures_total']} translated | "
        f"Workbooks: {s['workbooks_viz_built']}/{s['workbooks_total']} viz built"
    )
    print(f"Bundle written to: {os.path.abspath(args.output)}")
    if not args.no_pbip:
        print("Openable projects: pbip/<Name>/<Name>.pbip (double-click in Power BI Desktop)")
    if s.get("needs_review_total"):
        print(f"Next step: {s['needs_review_total']} calculation(s) stubbed -> see summary.md "
              f"('Next step') to run them through the second compiler.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
