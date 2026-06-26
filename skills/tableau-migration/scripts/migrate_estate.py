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
    from .connection_to_m import parse_tds, extract_bundled_flatfile
    from .storage_mode import select_storage_mode, FALLBACK_LAND_TO_DELTA
    from .assemble_model import (assemble_import_model, write_model_folder, write_local_pbip,
                                 migrate_datasource, list_workbook_datasources)
    from .parameters import parse_parameters
    from .workbook_table_calcs import extract_table_calc_usages, load_workbook_xml
    from .workbook_calc_usage import workbook_calc_usage
    from . import fetch_tds as F
except ImportError:
    from connection_to_m import parse_tds, extract_bundled_flatfile
    from storage_mode import select_storage_mode, FALLBACK_LAND_TO_DELTA
    from assemble_model import (assemble_import_model, write_model_folder, write_local_pbip,
                                migrate_datasource, list_workbook_datasources)
    from parameters import parse_parameters
    from workbook_table_calcs import extract_table_calc_usages, load_workbook_xml
    from workbook_calc_usage import workbook_calc_usage
    import fetch_tds as F


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
    """Enumerate a folder of exported Tableau files and hand their XML text to the pipeline.

    Both the bare exports (``.tds`` datasource, ``.twb`` workbook) and the packaged exports
    (``.tdsx`` / ``.twbx`` -- zip archives) are discovered recursively (case-insensitive) so a local
    UPLOAD works exactly like a live PULL. A packaged file's inner document is extracted in memory
    (never written to disk); a bare file is read with ``encoding="utf-8-sig"`` so Tableau's UTF-8 BOM
    is consumed transparently. When both a packaged and an unpacked copy of the same asset coexist in
    a folder, the asset is processed ONCE (the unpacked copy wins). Ids are absolute file paths; the
    display name is the file stem.
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

    @staticmethod
    def _dedup_by_stem(paths):
        # A packaged export (.tdsx/.twbx) and its unpacked twin (.tds/.twb) describe ONE asset; emit it
        # once (prefer the unpacked copy -- already text, and the copy a user is most likely editing)
        # so the output bundle has no duplicate datasource / name collision.
        chosen = {}
        for p in paths:
            stem, ext = os.path.splitext(os.path.basename(p))
            key = (os.path.dirname(p), stem.lower())
            packaged = ext.lower() in (".tdsx", ".twbx")
            if key not in chosen or (chosen[key][1] and not packaged):
                chosen[key] = (p, packaged)
        return sorted(p for p, _packaged in chosen.values())

    def list_datasources(self):
        # Packaged ``.tdsx`` is a common local export shape, so discover it alongside the bare ``.tds``.
        return self._dedup_by_stem(self._discover(".tds") + self._discover(".tdsx"))

    def read_datasource(self, ds_id):
        with open(ds_id, "rb") as fh:
            data = fh.read()
        return F.inner_tds_from_zip(data) if F.is_zip(data) else data.decode("utf-8-sig")

    def list_workbooks(self):
        # Packaged ``.twbx`` is a common local export shape, so discover it alongside the bare ``.twb``.
        return self._dedup_by_stem(self._discover(".twb") + self._discover(".twbx"))

    def read_workbook(self, wb_id):
        # ``load_workbook_xml`` transparently handles both a bare ``.twb`` and a packaged ``.twbx``.
        return load_workbook_xml(wb_id)

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
                 fabric_workspace=None, api_version="3.21", pat_value=None,
                 pat_env_var="TABLEAU_PAT", env_file=None, keyring_service=None,
                 allow_prompt=False):
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
        # Key-Vault-free credential layers for local / POC runs (see scripts/credential_resolver.py
        # and _resolve_pat). These are *pointers* (an env-var name, a .env path, a keyring service)
        # plus an optional in-memory value -- never a secret persisted on the instance. pat_value is
        # explicit-only (no env fallback); the rest fall back to a pointer env var so a POC needs no
        # code change. allow_prompt gates the interactive last resort.
        self.pat_value = pat_value
        self.pat_env_var = pat_env_var or os.environ.get("TABLEAU_MIGRATION_PAT_ENV_VAR")
        self.env_file = env_file or os.environ.get("TABLEAU_MIGRATION_ENV_FILE")
        self.keyring_service = keyring_service or os.environ.get("TABLEAU_MIGRATION_KEYRING_SERVICE")
        self.allow_prompt = allow_prompt
        # Value-free trace of which credential layer last answered (set by _resolve_pat); never a
        # token value. None until a PAT is resolved.
        self._pat_source = None
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
        """Resolve the Tableau PAT *secret* at run time, Key-Vault-free first.

        Delegates to the layered resolver in :mod:`credential_resolver`, which tries, in order: an
        explicit ``pat_value``, the ``pat_env_var`` environment variable, that same key in an
        ``env_file`` ``.env``, an OS-keyring secret under ``keyring_service`` (only if the optional
        ``keyring`` package is installed), then -- when ``allow_prompt`` is set and a console is
        attached -- an interactive ``getpass`` prompt. This lets a local / POC run authenticate with
        no Azure Key Vault. The resolved token is returned to the caller only; it is never logged,
        persisted, or stored on the instance (only the value-free ``_pat_source`` layer label is
        kept). When no local layer is configured/available, falls back to the enterprise Key Vault
        seam :meth:`_resolve_pat_from_key_vault`.
        """
        from credential_resolver import resolve_secret, CredentialNotFound
        try:
            resolved = resolve_secret(
                "Tableau personal access token secret",
                explicit=self.pat_value,
                env_var=self.pat_env_var,
                env_file=self.env_file,
                keyring_service=self.keyring_service,
                keyring_username=self.pat_name,
                allow_prompt=self.allow_prompt,
                prompt_text="Tableau personal access token secret: ",
            )
        except CredentialNotFound:
            return self._resolve_pat_from_key_vault()
        self._pat_source = resolved.source
        return resolved.value

    def _resolve_pat_from_key_vault(self):
        """SEAM: fetch the PAT *secret* from Azure Key Vault at run time (enterprise alternative).

        Used only when no local credential layer (see :meth:`_resolve_pat`) is configured or yields a
        value. Implement with the Azure CLI already on the box::

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

    Returns ``(calcs, skipped)`` where ``calcs`` is a list of ``{"name", "formula", "internal_name"?}``
    ready to hand to ``assemble_import_model(calcs=...)`` and ``skipped`` records every calculated
    field deliberately left out, with a reason -- so nothing disappears silently. ``internal_name`` is
    the field's Tableau internal name (e.g. ``Calculation_0014172369248279``), included only when it
    differs from the caption -- an additive cross-layer join key so a translated measure can be bound
    back to its workbook usage. This matches ``connection_to_m.extract_calcs``'s convention so both
    calc extractors stamp the same key the model build reads for source identity / calc_bindings.

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
        internal_name = _strip_brackets(col.get("name") or "") or None
        caption = col.get("caption") or internal_name or ""
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
            dim_entry = {"name": caption, "formula": formula, "role": role}
            if internal_name and internal_name.lower() != caption.lower():
                dim_entry["internal_name"] = internal_name
            dim_calcs.append(dim_entry)
            continue
        if caption in seen:
            skipped.append({"name": caption, "reason": "duplicate calculated-field name"})
            continue
        seen.add(caption)
        entry = {"name": caption, "formula": formula}
        if internal_name and internal_name.lower() != caption.lower():
            entry["internal_name"] = internal_name
        calcs.append(entry)

    return (calcs, skipped, dim_calcs) if include_dimensions else (calcs, skipped)


# -- orchestration helpers -----------------------------------------------------
_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _fs_safe(name, default="model"):
    """A filesystem-safe base for a name (no estate-wide de-duplication)."""
    return _INVALID_FS.sub("_", name or "").strip().rstrip(".") or default


def _safe_folder(name, used):
    """A filesystem-safe, de-duplicated folder base for a model/report name."""
    base = _fs_safe(name, "datasource")
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
    supports_date = "date_binding" in params
    supports_rowcount = "row_count_binding" in params
    supports_measure = "measure_binding" in params
    supports_param = "param_binding" in params
    supports_model_table = "model_table" in params
    supports_field_map = "field_map" in params
    def _call(twb_text, name, date_binding=None, measure_binding=None, row_count_binding=None,
              param_binding=None, model_table=None, field_map=None):
        if name_kwargs:
            kwargs = {k: name for k in name_kwargs}
            if supports_date and date_binding is not None:
                kwargs["date_binding"] = date_binding
            if supports_rowcount and row_count_binding is not None:
                kwargs["row_count_binding"] = row_count_binding
            if supports_measure and measure_binding is not None:
                kwargs["measure_binding"] = measure_binding
            if supports_param and param_binding is not None:
                kwargs["param_binding"] = param_binding
            if supports_model_table and model_table is not None:
                kwargs["model_table"] = model_table
            if supports_field_map and field_map is not None:
                kwargs["field_map"] = field_map
            return cand(twb_text, **kwargs)
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


def _migrate_one_datasource(source, ds_id, sm_dir, used_folders, pbip_dir=None, ds_catalog=None,
                            approved_calc_dax=None):
    """Drive the full per-datasource pipeline. Returns a report detail dict (never raises).

    When ``ds_catalog`` is given, a successfully migrated datasource records its source text +
    folder name under a connector-agnostic key, so a workbook that connects to it as a PUBLISHED
    datasource can later rebuild its model from this real schema (see ``_attach_workbook_pbip``).
    """
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

    # Flat-file Import (Excel/CSV bundled inside a .tdsx/.twbx): extract the embedded data file to an
    # ABSOLUTE path so the emitted M's File.Contents loads in Power BI Desktop. A relative path opens
    # but loads NO data ("The supplied file path must be a valid absolute path"). A live DB source
    # (Snowflake/Databricks/SQL Server/...) carries no flatfile_filename -> no-op; its connection
    # string is left exactly as-is.
    flatfile_path = None
    if descriptor.get("flatfile_filename"):
        data_dir = os.path.join(os.path.dirname(os.path.abspath(sm_dir)), "data",
                                re.sub(r"[^\w.-]+", "_", name) or "ds")
        try:
            flatfile_path = extract_bundled_flatfile(ds_id, descriptor, data_dir)
        except Exception:
            flatfile_path = None
    detail["flatfile_landed"] = flatfile_path

    try:
        out = assemble_import_model(descriptor, model_name=name, calcs=calcs, dim_calcs=dim_calcs,
                                    parameters=parameters, approved_calc_dax=approved_calc_dax,
                                    flatfile_path=flatfile_path)
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
        partitions_needs_review=report.get("partitions_needs_review", []),
        partitions_stubbed=report.get("partitions_stubbed", 0),
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
    if ds_catalog is not None:
        ds_catalog[_norm_ds(name)] = {"name": name, "text": text, "safe_base": safe_base,
                                      "flatfile_path": flatfile_path}
    return detail


def _rank_primary_datasource(inventory, ir):
    """Pick the primary embedded datasource (most worksheet usage) and the rest.

    ``inventory`` is a non-empty ``list_workbook_datasources`` list. When the workbook has a single
    real datasource it is the primary. With several, rank by how many worksheets in the viz IR bind
    to each (by caption or internal name), falling back to inventory order for ties / when no IR is
    available. Returns ``(primary, secondaries)``.
    """
    if len(inventory) == 1:
        return inventory[0], []
    counts = {}
    worksheets = (ir or {}).get("worksheets", []) if isinstance(ir, dict) else []
    for ws in worksheets:
        for key in (ws.get("datasource"), ws.get("datasource_name")):
            k = (key or "").strip().lower()
            if k:
                counts[k] = counts.get(k, 0) + 1

    def _score(d):
        keys = [(d.get("caption") or "").strip().lower(),
                (d.get("label") or "").strip().lower(),
                (d.get("name") or "").strip().lower()]
        return max((counts.get(k, 0) for k in keys if k), default=0)

    order = {id(d): i for i, d in enumerate(inventory)}
    ranked = sorted(inventory, key=lambda d: (-_score(d), order[id(d)]))
    primary = ranked[0]
    return primary, [d for d in inventory if d is not primary]


def _rebind_report_byPath(parts, model_folder_name):
    """Return a copy of viz report ``parts`` whose ``definition.pbir`` is bound to a sibling model.

    The viz stage bakes byPath ``../<dataset_name>.SemanticModel`` (the dataset name defaults to the
    workbook name). A self-contained workbook ``.pbip`` embeds the workbook's OWN datasource as a
    sibling model, so the report must instead point at ``../<model_folder_name>.SemanticModel``.
    Only the byPath target is rewritten; everything else in ``parts`` is untouched. Returns ``None``
    when there is no ``definition.pbir`` to rebind (the report cannot be opened as a project).
    """
    if not isinstance(parts, dict) or "definition.pbir" not in parts:
        return None
    out = dict(parts)
    try:
        doc = json.loads(out["definition.pbir"])
    except (ValueError, TypeError):
        return None
    target = f"../{model_folder_name}.SemanticModel"
    ref = doc.get("datasetReference")
    if isinstance(ref, dict) and isinstance(ref.get("byPath"), dict):
        ref["byPath"]["path"] = target
    else:
        doc["datasetReference"] = {"byPath": {"path": target}}
    out["definition.pbir"] = json.dumps(doc, indent=2)
    return out


def _viz_fidelity(result):
    """Per-worksheet rebuild fidelity from a viz result: ``[{worksheet, visual_type, status, reason}]``.

    ``status`` is ``"rebuilt"`` for a worksheet emitted cleanly and ``"warned"`` for one the viz
    stage flagged (or an unsupported visual type). Dashboard-scope or unmatched warnings are kept as
    their own ``warned`` rows so nothing is dropped. Reasons reuse the engine's
    ``"manual attention required: "`` prefix.
    """
    ir = result.get("ir") if isinstance(result, dict) else None
    warnings = (result.get("warnings") if isinstance(result, dict) else None) or []
    worksheets = (ir or {}).get("worksheets", []) if isinstance(ir, dict) else []
    ws_names = {w.get("name") for w in worksheets}

    warned_ws, extra = {}, []
    for w in warnings:
        if w.get("scope") == "worksheet" and w.get("name") in ws_names:
            warned_ws.setdefault(w.get("name"), w.get("reason"))
        else:
            extra.append(w)

    fidelity = []
    for ws in worksheets:
        nm, vt = ws.get("name"), ws.get("visual_type")
        if nm in warned_ws:
            fidelity.append({"worksheet": nm, "visual_type": vt,
                             "status": "warned", "reason": warned_ws[nm]})
        elif vt in (None, "unsupported"):
            fidelity.append({"worksheet": nm, "visual_type": vt, "status": "warned",
                             "reason": "manual attention required: unsupported visual type"})
        else:
            fidelity.append({"worksheet": nm, "visual_type": vt,
                             "status": "rebuilt", "reason": ws.get("fidelity_note")})
    for w in extra:
        fidelity.append({"worksheet": w.get("name"), "visual_type": w.get("scope"),
                         "status": "warned", "reason": w.get("reason")})
    return fidelity


_PBIP_WARN = "manual attention required: "


def _model_object_names(model_parts):
    """Collect every measure name and column name emitted by the model (lower-cased).

    Used to cross-check that the viz layer's field references resolve to a real model object.
    Names are gathered across *all* TMDL parts (measures live in ``_Measures``; columns in their
    table parts), so the check is robust to whether a table is in its own file or in ``model.tmdl``.
    """
    measures, columns = set(), set()
    for path, content in (model_parts or {}).items():
        if not (isinstance(content, str) and path.endswith(".tmdl")):
            continue
        for q, b in re.findall(r"(?m)^\s*measure\s+(?:'([^']+)'|([^\s=]+))", content):
            measures.add((q or b).lower())
        for q, b in re.findall(r"(?m)^\s*column\s+(?:'([^']+)'|([^\s=]+))", content):
            columns.add((q or b).lower())
    return measures, columns


def _ref_name_kind(field):
    """Return ``(property_name, "measure"|"column"|None)`` for a PBIR projection field node."""
    node = field if isinstance(field, dict) else {}
    if "Aggregation" in node:
        node = (node["Aggregation"] or {}).get("Expression", {}) or {}
    if "Measure" in node:
        return (node["Measure"] or {}).get("Property"), "measure"
    if "Column" in node:
        return (node["Column"] or {}).get("Property"), "column"
    return None, None


def _crosscheck_report_refs(report_parts, model_parts):
    """Drop viz projections that reference a model object the migration did not emit.

    ``twb_to_pbir._resolve_field`` binds a calculated-field reference optimistically to
    ``_Measures[<caption>]`` without validating it against the emitted model (the field index
    only knows physical columns). So a calc that the model rebuilt as a *column* (a dimension-role
    calc), stubbed, or dropped leaves a **dangling** ``_Measures[X]`` reference -- a "missing field"
    in Power BI. At this seam both halves are in hand, so we deterministically verify every
    projection against the real model: a measure ref must name an emitted measure, a column ref an
    emitted column. Unresolved projections are dropped (warn-never-wrong: drop rather than mis-bind);
    a visual that loses every projection is emptied to a placeholder zone so it never renders broken.
    Field-parameter visuals are skipped (a separately validated construct). Returns
    ``(report_parts, drops)`` where ``drops`` is ``[{"visual", "dropped": [...], "emptied": bool}]``.
    """
    measures, columns = _model_object_names(model_parts)
    drops = []
    if not (measures or columns):
        return report_parts, drops  # no model object inventory -> do not risk false drops
    for path, content in list((report_parts or {}).items()):
        if not (isinstance(content, str) and path.endswith("visual.json")):
            continue
        try:
            j = json.loads(content)
        except (ValueError, TypeError):
            continue
        vis = j.get("visual") or {}
        qs = ((vis.get("query") or {}).get("queryState")) or {}
        if not qs or any(isinstance(s, dict) and s.get("fieldParameters") for s in qs.values()):
            continue
        dropped = []
        for role, spec in list(qs.items()):
            if not isinstance(spec, dict):
                continue
            kept = []
            for p in spec.get("projections", []):
                name, kind = _ref_name_kind((p or {}).get("field") or {})
                low = name.lower() if isinstance(name, str) else None
                ok = (low in measures if kind == "measure"
                      else low in columns if kind == "column"
                      else True)  # unknown ref shape -> keep (conservative)
                (kept if ok else dropped).append(p if ok else f"{role}:{kind or '?'} {name!r}")
            spec["projections"] = kept
            if not kept:
                del qs[role]
        if dropped:
            emptied = not qs
            if emptied:
                vis.pop("query", None)
            report_parts[path] = json.dumps(j, indent=2)
            drops.append({"visual": j.get("name"), "dropped": dropped, "emptied": emptied})
    return report_parts, drops


def _date_binding_from_model(res_report):
    """Derive the report binder's ``date_binding`` from the model build's date-table report.

    Purely a CONSUMER of facts the datasource-migration build already produced (it never re-detects
    dates): the marked Date table name and which fact date column the calendar relates to ACTIVELY
    (``assemble_model._select_primary_date`` refuses to guess when ambiguous, so ``active`` is empty
    then). Returns ``None`` when there is no usable marked Date table or no active date -- the report
    then keeps binding date axes to the source column (warn-never-wrong). ``grain_columns`` is left
    to the binder's standard calendar-column default, so the contract stays minimal.
    """
    dr = (res_report or {}).get("date_table") or {}
    if not (dr.get("generated") and dr.get("mark_as_date") and dr.get("table")):
        return None
    active = [r.get("column") for r in (dr.get("relationships") or [])
              if r.get("active") and r.get("column")]
    if not active:
        return None
    return {"date_table": dr["table"], "active_keys": active, "key_column": "Date"}


def _measure_binding_from_model(res_report):
    """Derive the report binder's ``measure_binding`` from the model build's calc->measure facts.

    Pure CONSUMER of the datasource-migration report (it never re-translates a calc): it shapes the
    model build's own calc->measure identity into the ``{"measures": {key: entry}}`` map that
    ``twb_to_pbir._lookup_measure_binding`` reads, so a workbook-local calc / quick-table-calc pill
    the model emitted as a named ``_Measures`` measure rebinds to that real measure -- deterministic
    and token-keyed (the locked model<->viz contract). Each ``entry`` carries ``model_table`` +
    ``measure_name`` + ``status``; the consumer binds ONLY a translated / assisted-approved entry and
    degrades-and-warns on anything else.

    Two sources, in priority:
      1. ``report["calc_bindings"]`` -- the model build's consolidated index keyed by BOTH the calc
         instance token (``pcdf:usr:Calculation_*:qk``) and the bare calc id / caption. Passed
         through verbatim so the join token is byte-identical to what the model stamped (never
         re-derived here).
      2. otherwise, per-measure ``source`` tags on ``report["measures"]`` rows (a pre-``calc_bindings``
         shape): only rows that carry an explicit ``calc_instance_token`` / ``calc_id`` /
         ``field_caption`` are keyed, so plain ``<column>`` calcs keep their existing caption-based
         ``_Measures`` binding untouched.

    Returns ``None`` when the model produced no token-identified calc measure, so the report keeps its
    standing field resolution (warn-never-wrong; byte-unchanged until a real binding exists).
    """
    rr = res_report or {}
    index = rr.get("calc_bindings")
    if isinstance(index, dict):
        entries = {k: v for k, v in index.items() if k and isinstance(v, dict)}
        if entries:
            return {"measures": entries}
    entries = {}
    for row in rr.get("measures") or []:
        if not isinstance(row, dict):
            continue
        name = row.get("measure")
        src = row.get("source")
        if not name or not isinstance(src, dict):
            continue
        entry = {"model_table": src.get("model_table") or "_Measures",
                 "measure_name": name, "status": row.get("status")}
        for key in (src.get("calc_instance_token"), src.get("calc_id"), src.get("field_caption")):
            if key:
                entries.setdefault(key, entry)
    return {"measures": entries} if entries else None


def _row_count_binding_from_model(res_report):
    """Derive the report binder's ``row_count_binding`` from the model build's COUNTROWS facts.

    Pure CONSUMER of the datasource-migration report (it never re-derives a count). A dashboard's
    implicit object-id ``COUNT(*)`` pill (e.g. the pilot's ``COUNT(Orders)`` line value) carries NO
    calc token, so it must bind by FACT TABLE rather than by a calc id -- a channel distinct from
    ``measure_binding``. Once the model build lowers an object-id count to a ``COUNTROWS('<fact>')``
    measure (the g1 lowering) and surfaces it, this shapes that fact into the binder's
    ``row_count_binding`` (the ``twb_to_pbir._row_count_measure_target`` contract):
    ``{"measures": {<table>: {"entity", "measure"}}, "default": {"entity", "measure"}}``. An
    ``object_id`` count binds ONLY on its own table (never via ``default`` -- it names a specific
    fact); the legacy single-fact ``numrec`` count binds via ``default``.

    Two sources, in priority (both additive; passed through, never re-derived):
      1. ``report["row_count_binding"]`` -- already in the consumer shape; normalised + passed
         through verbatim so the table->measure identity is byte-identical to what the model emitted.
      2. ``report["row_count_measures"]`` -- a convenience ``{<table>: {entity, measure}}`` (or
         ``{<table>: "<measure name>"}``) map plus an optional ``"default"``; normalised to the
         shape above (a bare name defaults to the ``_Measures`` table).
      3. ``report["model_manifest"]["row_count"]`` -- the same fact-table -> COUNTROWS-measure
         mapping when the model build surfaces it nested inside its additive ``model_manifest``
         (either the nested ``{"measures": {...}, "default": {...}}`` shape or a flat
         ``{<table>: target}`` map). A scalar / non-mapping value here (e.g. a diagnostic row total)
         is ignored -- only real table->measure targets bind, so this is safe regardless of shape.

    Returns ``None`` when the model exposed no row-count measure, so the report keeps its precise
    "implicit row count ... left unbound" warning (warn-never-wrong; byte-unchanged until a real
    measure exists -- on a model with no such fact this is a no-op).
    """
    rr = res_report or {}

    def _target(m):
        if isinstance(m, str):
            return {"entity": "_Measures", "measure": m} if m else None
        if not isinstance(m, dict):
            return None
        entity = m.get("entity") or m.get("model_table") or "_Measures"
        measure = m.get("measure") or m.get("measure_name")
        return {"entity": entity, "measure": measure} if measure else None

    def _shape(measures_map, default_val):
        measures = {}
        for table, m in (measures_map or {}).items():
            if table == "default":
                continue
            tv = _target(m)
            if table and tv:
                measures[table] = tv
        out = {}
        if measures:
            out["measures"] = measures
        dflt = _target(default_val)
        if dflt:
            out["default"] = dflt
        return out or None

    def _from_obj(obj):
        # Accept either the nested consumer shape ({"measures": {...}, "default": {...}}) or a flat
        # convenience map ({<table>: target, "default": target}). A non-dict (or a dict carrying no
        # bindable target) yields None, so an absent/scalar source is a clean no-op.
        if not isinstance(obj, dict) or not obj:
            return None
        if isinstance(obj.get("measures"), dict):
            return _shape(obj.get("measures"), obj.get("default"))
        return _shape(obj, obj.get("default"))

    for src in (rr.get("row_count_binding"),
                rr.get("row_count_measures"),
                (rr.get("model_manifest") or {}).get("row_count")):
        shaped = _from_obj(src)
        if shaped:
            return shaped
    return None


def _filter_param_target_field(formula, param_inner):
    """Return the SINGLE Tableau field caption a parameter is equated against in the standard
    "parameter-as-filter" idiom, or ``None`` for any other shape.

    Tableau's canonical "use a parameter as a filter" calc compares ONE dimension column to the
    parameter, optionally with an ``OR [Parameters].[P] = "All"`` escape that shows everything::

        IF [Region] = [Parameters].[P] OR [Parameters].[P] = "All" THEN TRUE END
        IF [Parameters].[P] = [Sub-Category] OR [Parameters].[P] = "All" THEN TRUE END

    ``param_inner`` is the (bracket-less) parameter name the formula references. Only a clean,
    single-column equality binds: 0 or >1 distinct compared columns returns ``None`` (the caller then
    leaves the parameter as an unresolved slicer -- warn-never-wrong). The ``"All"`` escape compares
    the parameter to a STRING literal, never a field, so it never contributes a target. The negative
    lookbehind keeps the parameter's own ``[Parameters].[P]`` tail bracket from being read as a field.
    """
    f = formula or ""
    pi = re.escape(param_inner or "")
    if not pi:
        return None
    pat_field_eq_param = re.compile(
        r"(?<!\]\.)\[(?!Parameters?\])([^\]]+)\]\s*=\s*\[Parameters?\]\.\[" + pi + r"\]",
        re.IGNORECASE)
    pat_param_eq_field = re.compile(
        r"\[Parameters?\]\.\[" + pi + r"\]\s*=\s*\[(?!Parameters?\])([^\]]+)\]",
        re.IGNORECASE)
    fields = set()
    for m in pat_field_eq_param.finditer(f):
        fields.add(m.group(1).strip())
    for m in pat_param_eq_field.finditer(f):
        fields.add(m.group(1).strip())
    fields = {x for x in fields if x and x.lower() != "parameters"}
    return next(iter(fields)) if len(fields) == 1 else None


def _param_slicers_from_workbook(twb_text, res_report):
    """Direct single-select slicers for workbook parameters used as a plain column-equality filter.

    The model build classifies every parameter and (for a genuine what-if / field-swap param) emits a
    model object, but a parameter used purely as ``[Col] = [Parameters].[P]`` (optionally with an
    ``OR [Parameters].[P] = "All"`` escape) is most faithfully rebuilt as an ORDINARY single-select
    slicer on that real column -- no disconnected what-if table, no flag measure. This resolves those
    targets from the workbook's OWN filter calcs against the model's authoritative naming map, so a
    slicer only ever lands on a column the model actually emitted.

    Returns ``{<param internal_name>: {"table", "column", "single_select", "caption"}}`` (possibly
    empty), keyed the same way :func:`_param_binding_from_model` keys its slicers so the two merge
    cleanly. Never raises -- any parse problem yields no slicers and the precise "not rebuilt as a
    slicer yet" warning then stands.
    """
    try:
        params = parse_parameters(twb_text)
    except Exception:
        params = []
    if not params:
        return {}
    try:
        calcs, _skipped, dim_calcs = extract_calculations(twb_text, include_dimensions=True)
    except Exception:
        calcs, dim_calcs = [], []
    formulas = [(c.get("formula") or "") for c in (list(calcs or []) + list(dim_calcs or []))
                if isinstance(c, dict)]
    if not formulas:
        return {}
    naming = ((res_report or {}).get("model_manifest") or {}).get("naming") or {}
    col_idx = {}
    for ref, info in naming.items():
        if isinstance(info, dict) and info.get("kind") == "column":
            key = (ref or "").strip().lower()
            if key:
                col_idx.setdefault(key, info)
    if not col_idx:
        return {}
    out = {}
    for p in params:
        pid = p.get("internal_name")
        if not pid:
            continue
        keys = {(p.get("caption") or "").strip().strip("[]").strip().lower(),
                (pid or "").strip().strip("[]").strip().lower()}
        keys.discard("")
        for formula in formulas:
            refs = {m.strip().lower()
                    for m in re.findall(r"\[Parameters?\]\.\[([^\]]+)\]", formula)}
            hit = next((k for k in keys if k in refs), None)
            if not hit:
                continue
            field = _filter_param_target_field(formula, hit)
            if not field:
                continue
            info = col_idx.get(field.strip().lower())
            if info and info.get("model_table") and info.get("model_name"):
                out[pid] = {"table": info["model_table"], "column": info["model_name"],
                            "single_select": True, "caption": p.get("caption") or pid}
                break
    return out


def _scope_flag_visuals(twb_text, res_report):
    """Attach the worksheet names a flag measure scopes to its ``filter_bindings`` entry.

    A date-window / measure flag is applied as a visual-level ``flag = 1`` filter, but only on the
    worksheets that actually placed the source Tableau filter calc -- not the whole page. The model
    build records each flag's source ``calc_id`` in ``report["filter_bindings"]``; this maps that
    calc_id to the worksheets that reference it (via :func:`workbook_calc_usage`, whose calc keys are
    the same unbracketed internal name) and writes those names into the binding's ``visuals`` list,
    so the viz layer can scope the filter to exactly those visuals. Additive + best-effort: a parse
    failure or an unreferenced calc leaves ``visuals`` absent (the consumer then falls back to its
    own known scope). Mutates ``res_report["filter_bindings"]`` in place; never raises.
    """
    fb = (res_report or {}).get("filter_bindings")
    if not isinstance(fb, dict) or not fb:
        return
    try:
        calc_usage = (workbook_calc_usage(twb_text) or {}).get("calcs") or {}
    except Exception:
        return
    for spec in fb.values():
        if not isinstance(spec, dict):
            continue
        cid = spec.get("calc_id")
        entry = calc_usage.get(cid) if cid else None
        if isinstance(entry, dict) and entry.get("worksheets"):
            spec["visuals"] = list(entry["worksheets"])


def _param_binding_from_model(res_report):
    """Derive the report binder's ``param_binding`` from the model build's parameter / filter facts.

    Pure CONSUMER of the datasource-migration report (it never re-derives a parameter). A Tableau
    dashboard parameter control, and a parameter-driven measure/calc filter, have no faithful Tier-1
    rebuild until the model build identifies what the parameter targets -- a real dimension column (a
    plain slicer), a disconnected picker table (a value-picker slicer), or a flag MEASURE that
    encodes a relative-date / measure window (applied as a visual-level ``flag = 1`` filter). This
    shapes those model facts into the ``twb_to_pbir`` consumer contract so the viz layer can emit
    faithful slicers + flag filters instead of the standing "not rebuilt as a slicer yet" /
    "aggregate-measure filter not mapped" warnings (warn-never-wrong: nothing is emitted unless the
    model confirmed the target, and a flag binds only for a translated / assisted-approved measure).

    Returns ``{"slicers": {<param id>: {"table", "column", "single_select", "caption"}},
    "flags": {<tableau filter token>: {"entity", "measure", "status", "value"}}}`` or ``None`` when
    the model exposed nothing bindable (so the report keeps its precise warnings, byte-unchanged).

    Sources (all additive; passed through, never re-derived), in priority:
      1. ``report["param_binding"]`` -- already in the consumer shape; normalised + passed through.
      2. ``report["model_manifest"]["parameters"]`` -- a list of ``{name, internal_name, kind,
         model_object, target_column?, picker?}`` records. A ``kind="filter"`` param with a resolved
         ``target_column`` becomes a plain slicer on that real column; a ``kind="value"`` param with
         a ``picker`` (a disconnected ``{table, column}`` picker table) becomes a value-picker
         slicer. ``model_object``/missing targets bind nothing (degrade-and-warn in viz).
      3. ``report["filter_bindings"]`` (or the same key nested in ``model_manifest``) -- a token-keyed
         ``{<tableau filter token>: {model_table, measure_name, status, predicate}}`` map for the
         flag measures (e.g. a relative-date "Date Window Flag"); bound iff ``status`` is
         ``translated`` / ``assisted-approved``.
    """
    rr = res_report or {}
    _BIND_OK = ("translated", "assisted-approved")

    def _field(spec, *, single):
        if not isinstance(spec, dict):
            return None
        table = spec.get("table") or spec.get("entity") or spec.get("model_table")
        column = spec.get("column") or spec.get("property")
        if not table or not column:
            return None
        return {"table": table, "column": column, "single_select": single}

    direct = rr.get("param_binding")
    if isinstance(direct, dict) and (direct.get("slicers") or direct.get("flags")):
        return {"slicers": dict(direct.get("slicers") or {}),
                "flags": dict(direct.get("flags") or {})}

    manifest = rr.get("model_manifest") or {}
    slicers, flags = {}, {}

    for p in (manifest.get("parameters") or []):
        if not isinstance(p, dict):
            continue
        pid = p.get("internal_name") or p.get("param_id") or p.get("id")
        caption = p.get("name") or p.get("caption")
        # A value-picker (disconnected picker table) wins over a plain target column when both are
        # present; both yield a single-select slicer (a Tableau parameter is a single-value control).
        field = _field(p.get("picker"), single=True) \
            or _field(p.get("target_column") or p.get("target"), single=True)
        if pid and field:
            field["caption"] = caption
            slicers[pid] = field

    fb = rr.get("filter_bindings") or manifest.get("filter_bindings") or {}
    for token, spec in (fb.items() if isinstance(fb, dict) else []):
        if not isinstance(spec, dict):
            continue
        measure = spec.get("measure_name") or spec.get("measure")
        status = (spec.get("status") or "").lower()
        if not measure or status not in _BIND_OK:
            continue
        pred = spec.get("predicate") if isinstance(spec.get("predicate"), dict) else {}
        flags[token] = {
            "entity": spec.get("model_table") or spec.get("entity") or "_Measures",
            "measure": measure,
            "status": status,
            "value": pred.get("value", 1),
            "visuals": list(spec.get("visuals") or []),
        }

    if not slicers and not flags:
        return None
    return {"slicers": slicers, "flags": flags}


def _ds_calc_columns(ds_el):
    """Calculated fields defined directly on a datasource element.

    Returns ``[{"name", "formula", "role", "_internal"}]`` for every ``<column>`` child carrying a
    ``<calculation class='tableau'>`` with a formula (parameters and non-formula bins/groups skipped).
    ``name`` is the user-facing caption (de-bracketed internal name as fallback); ``_internal`` is the
    lowercased ``Calculation_*`` id that worksheet ``<datasource-dependencies>`` reference by.
    """
    out, seen = [], set()
    for col in (c for c in list(ds_el) if _local(c.tag) == "column"):
        if col.get("param-domain-type") is not None:
            continue
        calc_el = next((c for c in list(col) if _local(c.tag) == "calculation"), None)
        if calc_el is None or (calc_el.get("class") or "tableau").strip().lower() != "tableau":
            continue
        formula = calc_el.get("formula")
        if not formula or not formula.strip():
            continue
        internal = _strip_brackets((col.get("name") or "").strip())
        name = (col.get("caption") or "").strip() or internal
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "formula": formula,
                    "role": (col.get("role") or "").strip().lower() or None,
                    "_internal": internal.lower()})
    return out


def _view_referenced_calc_ids(root):
    """Lowercased internal-ids and captions of calc fields referenced by ANY worksheet.

    Reads each ``<worksheet>``'s ``<datasource-dependencies>`` columns that carry a calculation, so a
    calc the user defined but never put on a shelf is not counted as a binding dependency.
    """
    refs = set()
    for ws in (e for e in root.iter() if _local(e.tag) == "worksheet"):
        for dep in (d for d in ws.iter() if _local(d.tag) == "datasource-dependencies"):
            for col in (c for c in list(dep) if _local(c.tag) == "column"):
                if next((c for c in list(col) if _local(c.tag) == "calculation"), None) is None:
                    continue
                cid = _strip_brackets((col.get("name") or "").strip()).lower()
                cap = (col.get("caption") or "").strip().lower()
                if cid:
                    refs.add(cid)
                if cap:
                    refs.add(cap)
    return refs


def _workbook_binding_signal(twb_text, ir):
    """Additive per-workbook binding decision record (records a SIGNAL; changes no routing today).

    Reports whether the workbook's primary datasource is a PUBLISHED Tableau datasource
    (``connection_class == 'sqlproxy'`` -- the federated proxy a published datasource connects
    through) or an EMBEDDED one, plus the view-referenced workbook-local calculated fields whose
    absence would break a rebind to a published/shared model (the *would-break-if-rebound* set). This
    is exactly the consumer-side input the estate-comparison + datasource-migration skills need to
    decide rebind-to-published vs rebuild-embedded; the dashboard migration itself still always
    rebuilds + binds the embedded model (the rebind ROUTING lands once the cross-skill catalog
    contract is frozen). Returns ``None`` when there is no real datasource to characterise.
    """
    try:
        inventory = list_workbook_datasources(twb_text)
    except Exception:
        return None
    if not inventory:
        return None
    primary, secondaries = _rank_primary_datasource(inventory, ir)
    is_published = (primary.get("connection_class") or "").strip().lower() == "sqlproxy"
    label = primary.get("label") or primary.get("caption") or primary.get("name")

    view_local_calcs = []
    try:
        root = ET.fromstring((twb_text or "").lstrip("\ufeff"))
        primary_name = (primary.get("name") or "").strip()
        ds_el = next((d for d in root.iter() if _local(d.tag) == "datasource"
                      and (d.get("name") or "").strip() == primary_name), None)
        if ds_el is not None:
            referenced = _view_referenced_calc_ids(root)
            for c in _ds_calc_columns(ds_el):
                if c["_internal"] in referenced or c["name"].lower() in referenced:
                    view_local_calcs.append({"name": c["name"], "formula": c["formula"],
                                             "role": c["role"]})
    except ET.ParseError:
        view_local_calcs = []

    if is_published and view_local_calcs:
        recommendation = "review_rebind"
        note = (f"published datasource {label!r}; {len(view_local_calcs)} view-referenced "
                "workbook-local calc(s) must be satisfied by the bound model -- rebind to the "
                "migrated published model only if it carries them, else rebuild the embedded model")
    elif is_published:
        recommendation = "candidate_rebind_to_published"
        note = (f"published datasource {label!r} with no view-local calc dependencies -- candidate "
                "to rebind to the migrated published model (pending estate catalog match)")
    else:
        recommendation = "rebuild_embedded"
        note = (f"embedded datasource {label!r} -- rebuild the model from the workbook so it carries "
                "its calculated fields")

    return {
        "kind": "published" if is_published else "embedded",
        "connection_class": primary.get("connection_class"),
        "primary_datasource": label,
        "published_ds_name": label if is_published else None,
        "secondary_datasources": [s.get("label") for s in secondaries],
        "view_local_calcs": view_local_calcs,
        "recommendation": recommendation,
        "note": note,
    }


def _norm_ds(name):
    """Connector-agnostic match key: lowercased with all non-alphanumerics removed, so a workbook's
    published-datasource name ('Superstore - Extract') matches the migrated datasource it became
    ('Superstore-Extract.tds' -> 'Superstore_Extract')."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _rebuild_from_published_match(detail, twb_text, model_safe, ds_catalog, approved_calc_dax=None):
    """Rebuild a published-datasource workbook's model from the matching ALREADY-MIGRATED published
    datasource (its real schema) instead of the workbook's own unusable ``sqlproxy`` proxy stub --
    carrying the workbook's own calculated fields so its view-local measures translate against that
    schema. Returns a ``migrate_datasource`` result bound to the real schema, or ``None`` when there
    is no faithful name match (the caller then keeps the honest skip). Never raises.
    """
    if not ds_catalog:
        return None
    sig = detail.get("binding_signal") or {}
    if sig.get("kind") != "published":
        return None
    match = ds_catalog.get(_norm_ds(sig.get("published_ds_name")))
    if not match:
        return None
    try:
        wb_calcs, _skipped, wb_dim_calcs = extract_calculations(twb_text, include_dimensions=True)
    except Exception:
        wb_calcs, wb_dim_calcs = None, None
    # Table-calc addressing (partition / order) lives in the WORKBOOK's worksheet shelves, never in
    # the published ``.tds`` schema we rebuild from -- so extract the usages from ``twb_text`` and
    # thread them through. Without this, positional measures (WINDOW_STDEV, percent-difference, LAST)
    # would re-extract from the schema-only ``.tds``, find no worksheets, and stub to ``= 0``. This
    # is what brings the live/published path to parity with a local ``.twbx`` whose embedded model
    # already carries its own worksheets.
    try:
        wb_table_calc_usages = extract_table_calc_usages(twb_text)
    except Exception:
        wb_table_calc_usages = None
    # Parameters also live only in the WORKBOOK, never in the published ``.tds`` schema. Without
    # threading them through, a parameter-driven measure (e.g. a Date Selection band that becomes a
    # keep-flag MEASURE) would never reach the model build on the published path, so the flag + its
    # ``filter_bindings`` would silently never fire. Guarded: a parse hiccup degrades to None (the
    # model build then simply has no parameters, exactly as before).
    try:
        wb_params = parse_parameters(twb_text)
    except Exception:
        wb_params = None
    try:
        res = migrate_datasource(match["text"], model_name=model_safe,
                                 calcs=wb_calcs, dim_calcs=wb_dim_calcs,
                                 parameters=wb_params,
                                 table_calc_usages=wb_table_calc_usages,
                                 approved_calc_dax=approved_calc_dax,
                                 flatfile_path=match.get("flatfile_path"))
    except Exception:
        return None
    if (res.get("report") or {}).get("fallback"):
        return None
    detail["bound_via"] = f"published_catalog_match:{match.get('name')}"
    return res


def _field_map_from_model(res_report):
    """Build ``(model_table, field_map)`` for the viz re-run from the model build's authoritative
    naming map, so a published-datasource workbook's column pills bind to the REAL migrated tables
    (``Orders``/``Date``) instead of the workbook's own unusable ``sqlproxy`` proxy entity.

    ``field_map`` keys VERBATIM on each column's Tableau field caption / remote name (the same
    ``model_manifest['naming']`` join convention the model->viz contract guarantees never dangles)
    and carries only ``{entity, property}`` -- never ``binding`` -- so an aggregation pill
    (``SUM([Sales])``) keeps its aggregation while its entity is corrected to the fact table.
    ``model_table`` is the fact table (the one owning the most columns) and acts as the fallback for
    any column pill not present in the map. Measures are intentionally EXCLUDED here -- the
    token-keyed ``measure_binding`` already rebinds them onto ``_Measures``. Returns ``(None, None)``
    when no naming map is available (the re-run then keeps its standing field bindings).
    """
    manifest = (res_report or {}).get("model_manifest") or {}
    naming = manifest.get("naming") or {}
    field_map, counts = {}, {}
    for ref, info in naming.items():
        if (info or {}).get("kind") != "column":
            continue
        model_table = info.get("model_table")
        model_name = info.get("model_name")
        if not ref or not model_table or not model_name:
            continue
        field_map[ref] = {"entity": model_table, "property": model_name}
        counts[model_table] = counts.get(model_table, 0) + 1
    if not field_map:
        return None, None
    fact_table = max(counts, key=counts.get)
    return fact_table, field_map


def _attach_workbook_pbip(detail, twb_text, result, safe_base, pbip_dir, viz=None, ds_catalog=None,
                          approved_calc_dax=None, wb_id=None):
    """Build an openable, self-contained workbook ``.pbip`` and record it on ``detail`` (never raises).

    Rebuilds the workbook's OWN primary embedded datasource into a semantic model (reusing the
    datasource pipeline, so calculated fields are auto-extracted and role-split) and binds the
    rebuilt report to it *by path* as a sibling, yielding ``pbip/<WB>/{<DS>.SemanticModel,
    <WB>.Report, <WB>.pbip}``. Purely additive: it never alters the bare ``reports/`` write. Sets
    ``pbip_status``/``pbip_folder``/``bound_model``/``bound_datasource``/``model_translation_handoff``
    and appends honest ``pbip_warnings`` for every case it cannot faithfully bind (no embedded
    datasource, lakehouse fallback, secondary datasources, write failure).
    """
    detail.update(pbip_status="skipped", pbip_folder=None, bound_model=None,
                  bound_datasource=None, model_translation_handoff=None)
    detail.setdefault("pbip_ref_drops", [])
    warns = detail.setdefault("pbip_warnings", [])

    report_parts = _rebind_report_byPath(result.get("parts") if isinstance(result, dict) else None,
                                         "__placeholder__")
    if report_parts is None:
        warns.append(_PBIP_WARN + "viz stage produced no PBIR report definition -- "
                     "cannot assemble an openable workbook project")
        return

    try:
        inventory = list_workbook_datasources(twb_text)
    except Exception:
        inventory = []
    if not inventory:
        warns.append(_PBIP_WARN + "no embedded datasource found to rebuild -- "
                     "workbook report not bound to a local model")
        return

    primary, secondaries = _rank_primary_datasource(inventory, result.get("ir"))
    label = primary.get("label") or primary.get("caption") or primary.get("name")
    model_safe = _fs_safe(primary.get("caption") or primary.get("name") or label, "Model")
    detail["bound_datasource"] = label
    for sec in secondaries:
        warns.append(_PBIP_WARN + f"secondary datasource {sec.get('label')!r} not bound -- "
                     f"a single PBIR report binds one model; bound the primary {label!r}")

    # Flat-file Import (Excel/CSV bundled inside the .twbx): extract the embedded data to an ABSOLUTE
    # path under the bundle's data/ dir so the workbook .pbip opens AND loads. ``wb_id`` is the packaged
    # workbook (the .twbx path for a local source); a live DB embedded source has no flatfile_filename,
    # so this is a no-op there. ``migrate_datasource`` does the extraction (fail-closed).
    _ff_dest = None
    if wb_id is not None and pbip_dir:
        _ff_dest = os.path.join(os.path.dirname(os.path.abspath(pbip_dir)), "data", model_safe)
    # Reuse a sibling datasource's already-extracted flat-file data. A .twbx usually does NOT bundle
    # its extract -- the data lives in the published/sibling .tdsx that the estate migrated separately
    # (datasources are migrated before workbooks). When that datasource already landed its Excel/CSV at
    # an absolute path, bind the workbook's model to the SAME file (one shared copy) so the workbook
    # .pbip loads, instead of leaving the relative path Power BI Desktop cannot open. When there is no
    # sibling match, migrate_datasource still tries to extract data bundled in the .twbx itself.
    ff_path = None
    if ds_catalog:
        cat = ds_catalog.get(_norm_ds(primary.get("caption") or primary.get("name") or label))
        if cat:
            ff_path = cat.get("flatfile_path")
    try:
        res = migrate_datasource(twb_text, model_name=model_safe, datasource=label,
                                 approved_calc_dax=approved_calc_dax,
                                 packaged_source=wb_id, flatfile_dest_dir=_ff_dest,
                                 flatfile_path=ff_path)
    except Exception as exc:
        warns.append(_PBIP_WARN + f"could not rebuild embedded datasource {label!r} "
                     f"({type(exc).__name__}: {exc}) -- workbook .pbip skipped")
        return

    res_report = res.get("report") or {}
    if res_report.get("fallback"):
        # Published-datasource workbook: its own embedded copy is a sqlproxy proxy stub with no
        # usable schema, so rebuilding it lands in the lakehouse fallback. When the estate already
        # built the matching published datasource, rebuild the model from THAT real schema --
        # carrying the workbook's own calculated fields so its view-local measures translate -- and
        # bind the report to it. Never guesses (a real datasource-name match is required); any
        # failure keeps the honest skip below (warn-never-wrong).
        recovered = _rebuild_from_published_match(detail, twb_text, model_safe, ds_catalog,
                                                  approved_calc_dax=approved_calc_dax)
        if recovered is not None:
            res = recovered
            res_report = res.get("report") or {}
        if res_report.get("fallback"):
            rationale = (res_report.get("storage_decision") or {}).get("rationale") or "undoable shape"
            warns.append(_PBIP_WARN + f"embedded datasource {label!r} routes to the lakehouse fallback "
                         f"({rationale}) -- workbook .pbip skipped (model lands separately)")
            return

    report_parts = _rebind_report_byPath(result["parts"], model_safe)
    # Model-fact rebind: now that the real model is in hand, re-run the viz stage ONCE with the
    # model build's facts so the report binds to what the model actually emitted (the contract is
    # model build -> facts -> single-pass viz). Two consumed facts, both additive + best-effort:
    #  * date_binding -- date axis pills on the ACTIVE business date bind to the shared marked Date
    #    table (Date[Year], ...), routing time intelligence through the calendar instead of the
    #    fact's raw date column.
    #  * measure_binding -- workbook-local calc / quick-table-calc pills the model translated into
    #    named ``_Measures`` measures rebind to those real, token-keyed measures (warn-never-wrong:
    #    only translated/assisted-approved entries bind; anything else degrades-and-warns in viz).
    #  * row_count_binding -- implicit object-id COUNT(*) pills (which carry no calc token) rebind to
    #    the model's per-fact COUNTROWS measure by table name, so a dashboard's row-count value (e.g.
    #    the pilot's COUNT(Orders) line) lands on the real measure instead of being left unbound.
    #  * param_binding -- dashboard parameter controls + parameter-driven measure/calc filters rebind
    #    to faithful slicers (a real dimension column, or the model's disconnected picker table) and a
    #    visual-level flag = 1 filter (a model-owned relative-date / window flag MEASURE), clearing the
    #    "not rebuilt as a slicer yet" / "aggregate-measure filter not mapped" warnings. Warn-never-
    #    wrong: a slicer needs a model-confirmed target column/picker, a flag binds only when the
    #    measure is translated/assisted-approved; anything unconfirmed keeps its standing warning.
    # Either failure (or a model with no usable Date table / no calc measures / no row-count measure)
    # silently keeps the standing source-column / deferred binding.
    date_binding = _date_binding_from_model(res_report)
    measure_binding = _measure_binding_from_model(res_report)
    row_count_binding = _row_count_binding_from_model(res_report)
    # Scope each flag measure's visual-level filter to the worksheets that placed the source calc
    # (additive enrichment of report["filter_bindings"]; no-op when there are no flags).
    _scope_flag_visuals(twb_text, res_report)
    param_binding = _param_binding_from_model(res_report)
    # A parameter used purely as a single-column equality filter ([Col] = [Parameters].[P]) is most
    # faithfully a plain slicer on that real column -- not a disconnected what-if table. Resolve those
    # directly from the workbook's filter calcs and merge them in (these workbook-confirmed column
    # slicers take precedence over any value/field model object for the same parameter).
    wb_slicers = _param_slicers_from_workbook(twb_text, res_report)
    if wb_slicers:
        if not isinstance(param_binding, dict):
            param_binding = {"slicers": {}, "flags": {}}
        merged = dict(param_binding.get("slicers") or {})
        merged.update(wb_slicers)
        param_binding["slicers"] = merged
        param_binding.setdefault("flags", {})
    field_model_table, field_map = _field_map_from_model(res_report)
    if (date_binding or measure_binding or row_count_binding or param_binding
            or field_map) and viz is not None:
        try:
            rebuilt = viz(twb_text, detail.get("name") or safe_base,
                          date_binding=date_binding, measure_binding=measure_binding,
                          row_count_binding=row_count_binding, param_binding=param_binding,
                          model_table=field_model_table, field_map=field_map)
            if isinstance(rebuilt, dict) and rebuilt.get("parts"):
                report_parts = _rebind_report_byPath(rebuilt["parts"], model_safe)
                if date_binding:
                    detail["date_rebind"] = {"date_table": date_binding["date_table"],
                                             "active_keys": date_binding["active_keys"]}
                if measure_binding:
                    detail["measure_rebind"] = {
                        "count": len((measure_binding.get("measures") or {}))}
                if row_count_binding:
                    detail["row_count_rebind"] = {
                        "count": len((row_count_binding.get("measures") or {}))
                        + (1 if row_count_binding.get("default") else 0)}
                if param_binding:
                    detail["param_rebind"] = {
                        "slicers": len((param_binding.get("slicers") or {})),
                        "flags": len((param_binding.get("flags") or {}))}
                if field_map:
                    detail["field_rebind"] = {
                        "count": len(field_map), "model_table": field_model_table}
                # The rebound report -- not the pre-rebind first pass -- is what lands in the
                # openable .pbip, so refresh the per-worksheet fidelity + implicit-row-count tally
                # from it. Now-bound row counts / measures / params clear their warnings here, so the
                # reported fidelity matches the project the user actually opens (warn-never-wrong: any
                # warning the rebound run still emits is carried, never masked).
                detail["viz_fidelity"] = _viz_fidelity(rebuilt)
                detail["viz_implicit_row_count"] = sum(
                    1 for w in (rebuilt.get("warnings") or [])
                    if "implicit row count" in (w.get("reason") or ""))
        except Exception as exc:
            warns.append(_PBIP_WARN + f"model-fact rebind skipped ({type(exc).__name__}: {exc}) -- "
                         f"report binds to the standing source/deferred fields")
    # M1.3 ref cross-check: now that the real model is in hand, drop any viz projection that
    # references a measure/column the model did not emit (an optimistic `_Measures[caption]` bind
    # that dangles), so the whole viz layer is warn-never-wrong on field references -- not just MV.
    report_parts, ref_drops = _crosscheck_report_refs(report_parts, res.get("parts"))
    if ref_drops:
        detail["pbip_ref_drops"] = ref_drops
        for d in ref_drops:
            tail = " (visual emptied)" if d["emptied"] else ""
            warns.append(_PBIP_WARN + f"visual {d['visual']!r} dropped {len(d['dropped'])} "
                         f"reference(s) the model did not emit: {', '.join(d['dropped'])}{tail}")
    dest = os.path.join(pbip_dir, safe_base)
    try:
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        write_local_pbip(res["parts"], dest, model_name=model_safe, report_name=safe_base,
                         report_parts=report_parts, project_name=safe_base)
    except OSError as exc:
        warns.append(_PBIP_WARN + f"workbook .pbip write failed ({exc})")
        return

    detail.update(pbip_status="built",
                  pbip_folder=f"pbip/{safe_base}/{safe_base}.pbip",
                  bound_model=model_safe,
                  model_translation_handoff=res_report.get("translation_handoff"))


def _attach_viz_advice(detail, result, safe_base, reports_dir):
    """Write the opt-in ``<Name>.viz-advice.json`` sidecar (ranked chart alternatives per visual).

    Additive + best-effort: derived from the viz stage's read-only candidate records via the Tier-2
    viz advisor (``viz_advisor.build_report_advice``), written as a SIBLING of the ``.Report`` folder
    (never inside the PBIR definition) so the rebuilt report stays byte-identical. Records a
    ``viz_advice`` summary on ``detail``; never raises (the advisor is fully optional).
    """
    try:
        from viz_advisor import build_report_advice
    except Exception as exc:  # pragma: no cover - advisor is an optional sibling module
        detail["viz_advice"] = {"status": "unavailable", "note": f"{type(exc).__name__}: {exc}"}
        return
    records = result.get("candidate_records") if isinstance(result, dict) else None
    advice = build_report_advice(records or [])
    rel = f"reports/{safe_base}.viz-advice.json"
    try:
        with open(os.path.join(reports_dir, safe_base + ".viz-advice.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(advice, fh, indent=2, sort_keys=True)
    except OSError as exc:
        detail["viz_advice"] = {"status": "error", "note": str(exc)}
        return
    detail["viz_advice"] = {"status": "written", "path": rel, "summary": advice["summary"]}


def _migrate_one_workbook(source, wb_id, viz, reports_dir, used_folders, pbip_dir=None,
                          ds_catalog=None, approved_calc_dax=None, viz_advice=False):
    """Run the optional viz stage for one workbook. Returns a report detail dict (never raises).

    Beyond the back-compatible bare ``reports/<Name>.Report`` write, when ``pbip_dir`` is given the
    workbook's rebuilt dashboard is additionally bundled into an openable, self-contained ``.pbip``
    project (model rebuilt from the workbook's own embedded datasource + report bound to it by path)
    so it can be opened in Power BI Desktop. A ``viz_fidelity`` list reports per-worksheet rebuild
    status; ``pbip_*`` keys report the project binding. Both additions are additive.
    """
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
    safe_base = None
    if parts:
        safe_base = _safe_folder(name, used_folders)
        folder = safe_base + ".Report"
        dest = os.path.join(reports_dir, folder)
        try:
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            write_model_folder(parts, dest)
            output_folder = f"reports/{folder}"
        except OSError as exc:
            detail.update(viz_status="error", note=f"viz write failed: {exc}")
            return detail

    viz_warns = result.get("warnings") if isinstance(result, dict) else None
    rc_unbound = sum(1 for w in (viz_warns or [])
                     if "implicit row count" in (w.get("reason") or ""))
    detail.update(viz_status="built",
                  note=result.get("note") if isinstance(result, dict) else None,
                  output_folder=output_folder,
                  viz_fidelity=_viz_fidelity(result),
                  viz_implicit_row_count=rc_unbound)

    signal = _workbook_binding_signal(text, result.get("ir") if isinstance(result, dict) else None)
    if signal is not None:
        detail["binding_signal"] = signal

    if viz_advice and parts and safe_base is not None:
        _attach_viz_advice(detail, result, safe_base, reports_dir)

    if parts and pbip_dir is not None:
        _attach_workbook_pbip(detail, text, result, safe_base, pbip_dir, viz=viz,
                              ds_catalog=ds_catalog, approved_calc_dax=approved_calc_dax, wb_id=wb_id)
    return detail


# -- rebind plan ingest / routing (opt-in; byte-identical no-op when absent) ---
# The comparison skill writes ``rebind-plan.json`` to the estate output root; this orchestrator
# INGESTS it -- the JSON file is the ONLY coupling (nothing is shelled or invoked). The plan is
# consumed read-only; resolved bindings are written to a SEPARATE ``compile-report.json`` (this
# module is its only writer) so the comparison-owned plan is never mutated.
REBIND_PLAN_SCHEMA = "1.0"

# Per-report bind seam. The dashboard-migration stage owns the actual bind function; this module
# only calls it. Until that function is available the router DEFERS every routed entry (records it
# in compile-report.json with a reason) rather than guessing -- keeping the run safe and green.
_BIND_ENTRY_POINTS = ("bind_report_to_model", "rebind_report", "bind_report")

# Route each entry by ``binding_status`` FIRST (the tagged-union discriminant). ``needs_attention``
# and ``landed_to_delta`` are DEFER keys (the report is left unbound) -- neither is an action.
# ``landed_to_delta`` is a write-back state the calc-compiler sets when a model's storage falls back.
_BINDING_STATUS_ROUTES = {
    "existing_fabric": "byConnection",
    "built_local": "byPath",
    "landed_to_delta": "defer",
    "needs_attention": "defer",
}
# Actions whose freshly built byPath model carries a date table the calc-compiler resolves; the
# orchestrator echoes it onto the write-back record. existing-Fabric / published bindings get their
# date table from a separate Fabric-inventory pass, so they are NOT echoed here.
_DATE_ECHO_ACTIONS = ("rebind_to_rebuilt", "consolidate_new_model")


def _rebind_norm(name):
    """Case-insensitive, whitespace-trimmed key for matching a plan selector to an asset name."""
    return (name or "").strip().lower()


def _load_rebind_plan(rebind_plan):
    """Load a rebind plan from a path or accept an already-parsed mapping.

    Returns ``(plan, errors)`` and never raises into the estate run: a ``None`` input yields
    ``(None, [])`` (the byte-identical no-op path) and an unreadable / malformed file yields
    ``(None, [reason])`` so the caller can record it and keep going. Files are read as ``utf-8-sig``
    so a Tableau-style UTF-8 BOM is consumed transparently.
    """
    if rebind_plan is None:
        return None, []
    if isinstance(rebind_plan, dict):
        return rebind_plan, []
    try:
        with open(rebind_plan, encoding="utf-8-sig") as fh:
            return json.load(fh), []
    except (OSError, ValueError) as exc:
        return None, [f"rebind plan unreadable: {type(exc).__name__}: {exc}"]


def _plan_entries(plan):
    """Return the plan's flat list of entry dicts from the canonical ``plan["plan"]`` array
    (``schema_version "1.0"``); a bare top-level list is tolerated defensively.

    Each entry is self-describing: ``source_ref`` is the per-workbook ``source_id`` join key (a
    STRING -- never assume it equals ``workbook_luid``), and ``workbook_luid`` / ``model_id`` /
    ``label`` are top-level entry siblings.
    """
    entries = plan if isinstance(plan, list) else plan.get("plan")
    if isinstance(entries, list):
        return [e for e in entries if isinstance(e, dict)]
    return []


def _validate_rebind_plan(plan):
    """Validate the plan envelope. Returns structured error strings (additive: unknown keys are
    tolerated; only ``schema_version`` and the basic shape are enforced)."""
    if not isinstance(plan, dict):
        return ["rebind plan is not a JSON object"]
    version = plan.get("schema_version")
    if version != REBIND_PLAN_SCHEMA:
        return [f"unsupported rebind plan schema_version {version!r} "
                f"(expected {REBIND_PLAN_SCHEMA!r})"]
    return []


def _plan_selector(entry):
    """The migrate_datasource selector for an entry: its per-entry ``label`` sibling (the
    caption-preferred display name = ``caption`` | ``formatted-name`` | raw ``name``). A single
    ``label`` is functionally sufficient -- the migration side matches it case-insensitively
    against each datasource's ``{caption, formatted-name, name}`` set."""
    return entry.get("label")


def _bind_adapter(cand):
    """Adapt a dashboard bind callable to a keyword call, forwarding only the kwargs it accepts.

    Mirrors ``_viz_adapter``: the dashboard owns the bind function's exact signature, so inspect it
    and pass through only recognized keyword names (or everything when it accepts ``**kwargs``).
    """
    try:
        sig = inspect.signature(cand)
    except (TypeError, ValueError):
        return lambda **kw: cand(**kw)
    accepts_all = any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values())
    names = set(sig.parameters)

    def _call(**kw):
        if not accepts_all:
            kw = {k: v for k, v in kw.items() if k in names}
        return cand(**kw)
    return _call


def _resolve_bind_stage(injected):
    """Resolve the per-report bind seam without ever hard-depending on it.

    An injected callable wins. Otherwise the first recognized entry point exposed by this module
    (where the dashboard-migration stage's bind function lands) is bound. Returns a keyword-callable
    or ``None`` -- and ``None`` makes the router DEFER every routed entry rather than guess.
    """
    if injected is not None:
        return _bind_adapter(injected)
    for fn in _BIND_ENTRY_POINTS:
        cand = globals().get(fn)
        if callable(cand):
            return _bind_adapter(cand)
    return None


def _migrated_index(ds_details):
    """Map normalized datasource display name -> its migrated report detail, for model reuse."""
    index = {}
    for d in ds_details:
        if d.get("status") in ("migrated", "migrated_with_followups"):
            index.setdefault(_rebind_norm(d.get("name")), d)
    return index


def _asset_index(source):
    """Map normalized asset display name -> ``(kind, asset_id)`` for source resolution by selector."""
    index = {}
    for ds_id in source.list_datasources():
        index.setdefault(_rebind_norm(source.asset_name(ds_id)), ("datasource", ds_id))
    for wb_id in source.list_workbooks():
        index.setdefault(_rebind_norm(source.asset_name(wb_id)), ("workbook", wb_id))
    return index


def _model_name_from_folder(output_folder):
    """``semantic_models/Foo.SemanticModel`` -> bare ``Foo``."""
    base = os.path.basename(output_folder or "")
    suffix = ".SemanticModel"
    return base[:-len(suffix)] if base.endswith(suffix) else base


def _resolve_plan_model(entry, route, source, sm_dir, used_folders, migrated_index, asset_index):
    """Resolve the model an entry binds to. Returns ``(model_info, error)``.

    ``model_info`` is ``{"resolved_model_name", "model_path"}`` -- ``model_path`` is root-relative
    and ``None`` on a storage fallback or an existing-Fabric identity. ``byConnection`` entries bind
    to an existing Fabric model and need no local build. ``byPath`` entries reuse a model the estate
    datasource pass already wrote when the selector matches one, otherwise resolve it through
    ``migrate_datasource(datasource=<caption-preferred selector>)``.
    """
    if route == "byConnection":
        target = entry.get("binding_target") or {}
        return {"resolved_model_name": target.get("dataset_name"), "model_path": None}, None

    selector = _plan_selector(entry)
    if not selector:
        return None, "entry has no label selector"

    reused = migrated_index.get(_rebind_norm(selector))
    if reused is not None:
        of = reused.get("output_folder")
        return {"resolved_model_name": _model_name_from_folder(of),
                "model_path": of or None}, None

    asset = asset_index.get(_rebind_norm(selector))
    if asset is None:
        return None, f"no source asset resolves selector {selector!r}"
    kind, asset_id = asset
    try:
        text = (source.read_workbook(asset_id) if kind == "workbook"
                else source.read_datasource(asset_id))
    except Exception as exc:  # unreadable asset -> defer with a reason, never abort
        return None, f"source {selector!r} unreadable: {type(exc).__name__}: {exc}"

    safe_base = _safe_folder(selector, used_folders)
    try:
        result = migrate_datasource(text, model_name=safe_base, datasource=selector,
                                    write_to=sm_dir)
    except Exception as exc:
        return None, f"migrate_datasource failed for {selector!r}: {type(exc).__name__}: {exc}"
    if (result.get("report") or {}).get("fallback") or not result.get("model_dir"):
        return {"resolved_model_name": safe_base, "model_path": None}, None
    return {"resolved_model_name": safe_base,
            "model_path": f"semantic_models/{safe_base}.SemanticModel"}, None


def _orchestrate_rebind(source, plan, output_dir, used_folders, ds_details, bind_stage,
                        load_errors):
    """Route every plan entry and assemble the ``compile-report`` payload. Never raises -- a bad
    entry or a bind failure is isolated as a ``deferred`` / ``errors`` record, never an abort."""
    errors = list(load_errors) + _validate_rebind_plan(plan)
    by_binding_status, by_action = {}, {}
    models, workbooks, deferred = {}, [], []

    sm_dir = os.path.join(output_dir, "semantic_models")
    migrated_index = _migrated_index(ds_details)
    asset_index = _asset_index(source)
    registry = plan.get("models") if isinstance(plan, dict) else None
    registry = registry if isinstance(registry, dict) else {}

    for entry in _plan_entries(plan):
        source_id = entry.get("source_ref")          # the per-workbook source_id join key (string)
        workbook_luid = entry.get("workbook_luid")   # native workbook key (top-level sibling)
        status = entry.get("binding_status")
        action = entry.get("action")
        by_binding_status[status] = by_binding_status.get(status, 0) + 1
        if action:
            by_action[action] = by_action.get(action, 0) + 1

        route = _BINDING_STATUS_ROUTES.get(status, "defer")
        if route == "defer":
            if status == "needs_attention":
                reason = "needs_attention -> deferred (left unbound)"
            elif status == "landed_to_delta":
                reason = "landed_to_delta -> deferred (storage fell back; report left unbound)"
            else:
                reason = f"unrecognized binding_status {status!r} -> deferred"
            deferred.append({"source_id": source_id, "workbook_luid": workbook_luid,
                             "reason": reason})
            continue
        if bind_stage is None:
            deferred.append({"source_id": source_id, "workbook_luid": workbook_luid,
                             "reason": "per-report bind seam unavailable -> deferred"})
            continue

        model_info, err = _resolve_plan_model(entry, route, source, sm_dir, used_folders,
                                               migrated_index, asset_index)
        if err is not None:
            deferred.append({"source_id": source_id, "workbook_luid": workbook_luid,
                             "reason": err})
            continue

        model_id = entry.get("model_id")
        if model_id is not None:
            record_model = {
                "model_id": model_id,
                "resolved_model_name": model_info.get("resolved_model_name"),
                "model_path": model_info.get("model_path"),
            }
            seed = registry.get(model_id)
            if isinstance(seed, dict) and seed.get("origin") is not None:
                record_model["origin"] = seed.get("origin")
            models.setdefault(model_id, record_model)

        try:
            bind_result = bind_stage(
                entry=entry, binding=route, binding_target=entry.get("binding_target"),
                model_id=model_id, model_path=model_info.get("model_path"),
                resolved_model_name=model_info.get("resolved_model_name"),
                used_folders=used_folders, source=source, output_dir=output_dir,
            ) or {}
        except Exception as exc:
            errors.append(f"bind failed for source_id {source_id!r}: {type(exc).__name__}: {exc}")
            deferred.append({"source_id": source_id, "workbook_luid": workbook_luid,
                             "reason": "bind raised -> deferred"})
            continue

        if isinstance(bind_result, str):
            bind_result = {"resolved_report_folder": bind_result}
        record = {
            "workbook_luid": workbook_luid,
            "source_id": source_id,
            "resolved_report_folder": bind_result.get("resolved_report_folder"),
            "bound_model_id": model_id,
        }
        # Echo date_table only onto a freshly built byPath model (rebuilt / consolidated), which the
        # calc-compiler resolves; byConnection / published bindings get theirs from a Fabric pass.
        if route == "byPath" and action in _DATE_ECHO_ACTIONS:
            record["date_table"] = bind_result.get("date_table", entry.get("date_table"))
        workbooks.append(record)

    return {
        "tool": "migrate_estate.rebind",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": REBIND_PLAN_SCHEMA,
        "models": sorted(models.values(), key=lambda m: str(m.get("model_id"))),
        "workbooks": workbooks,
        "resolved_report_folders": {
            "by_workbook_luid": {w["workbook_luid"]: w["resolved_report_folder"]
                                 for w in workbooks if w.get("workbook_luid") is not None},
            "by_source_id": {w["source_id"]: w["resolved_report_folder"]
                             for w in workbooks if w.get("source_id") is not None},
        },
        "routing": {"by_binding_status": by_binding_status, "by_action": by_action},
        "deferred": deferred,
        "errors": errors,
    }


def _write_compile_report(output_dir, compile_report):
    """Write the single ``compile-report.json`` (BOM-free, deterministic). This module is its only
    writer; the comparison-owned ``rebind-plan.json`` is never mutated."""
    path = os.path.join(output_dir, "compile-report.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(compile_report, fh, indent=2, sort_keys=True)
    return path


def migrate_estate(source, output_dir, *, viz_stage=None, pbip=True, rebind_plan=None,
                   rebind_bind_stage=None, approved_calc_dax=None, viz_advice=False):
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

    ``approved_calc_dax`` (optional, opt-in) is a ``{calc_name: dax}`` mapping of human-approved
    second-compiler (assisted-translation) results. It is threaded into every model build in the
    run -- the datasource pass, the workbook's embedded-datasource rebuild, and the
    published-datasource catalog-match rebuild -- so a Tier-0 stub whose name matches
    (case-insensitive) lands as a LIVE, audit-stamped measure / calc column instead of an inert
    ``= 0`` / ``BLANK()`` stub. This is the documented way to redeploy the fallback tier through the
    estate command (the ``--approved-dax`` CLI flag loads the mapping from a JSON file); when
    omitted the run is byte-identical.

    ``rebind_plan`` (optional, opt-in) is a ``rebind-plan.json`` path or already-parsed mapping
    written by the comparison skill. When given, the orchestrator additionally INGESTS it, routes
    each entry by ``binding_status``, resolves/binds each routed report through the dashboard bind
    seam (``rebind_bind_stage`` wins; otherwise auto-detected, and every routed entry DEFERS until
    it lands), and writes a single ``compile-report.json``. When omitted the run is a byte-identical
    no-op -- no plan is read and no ``compile-report.json`` is written. The JSON file is the only
    coupling; the comparison-owned plan is never mutated.

    ``viz_advice`` (optional, opt-in) turns on the Tier-2 viz advisor: per workbook, a
    ``reports/<Name>.viz-advice.json`` sidecar is written next to the rebuilt report with ranked
    ALTERNATIVE chart types for each visual's existing fields (deterministic; no model/LLM call). It
    is purely additive -- nothing is written into the PBIR definition and ``report.json`` only gains a
    ``viz_advice`` key per workbook -- so when omitted the run is byte-identical.
    """
    sm_dir = os.path.join(output_dir, "semantic_models")
    reports_dir = os.path.join(output_dir, "reports")
    pbip_dir = os.path.join(output_dir, "pbip") if pbip else None
    os.makedirs(output_dir, exist_ok=True)

    viz = _resolve_viz_stage(viz_stage)
    used_folders = set()

    ds_catalog = {}
    ds_details = [_migrate_one_datasource(source, ds_id, sm_dir, used_folders, pbip_dir,
                                          ds_catalog=ds_catalog,
                                          approved_calc_dax=approved_calc_dax)
                  for ds_id in source.list_datasources()]
    wb_details = [_migrate_one_workbook(source, wb_id, viz, reports_dir, used_folders, pbip_dir,
                                        ds_catalog=ds_catalog,
                                        approved_calc_dax=approved_calc_dax,
                                        viz_advice=viz_advice)
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

    # Opt-in rebind routing. Runs strictly AFTER the canonical report.json / summary.md are written
    # so those artifacts stay byte-identical to a no-plan run; the resolved bindings land only in
    # the separate compile-report.json (this module is its only writer).
    if rebind_plan is not None:
        plan, load_errors = _load_rebind_plan(rebind_plan)
        bind_stage = _resolve_bind_stage(rebind_bind_stage)
        compile_report = _orchestrate_rebind(
            source, plan if isinstance(plan, dict) else {}, output_dir, used_folders,
            ds_details, bind_stage, load_errors)
        _write_compile_report(output_dir, compile_report)
    return report


def _summarize(ds_details, wb_details, viz_available):
    """Roll per-asset details up into the report's machine-readable ``summary`` block."""
    modes = {"Import": 0, "DirectQuery": 0, "fallback": 0}
    connectors = set()
    migrated = partial = fallback = error = 0
    tables = columns = measures_total = measures_translated = measures_stubbed = 0
    calc_columns_total = calc_columns_translated = calc_columns_stubbed = 0
    needs_review_total = 0
    partitions_stubbed_total = 0

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
            partitions_stubbed_total += d.get("partitions_stubbed", 0)
        elif status == "fallback":
            fallback += 1
            modes["fallback"] += 1
        else:
            error += 1

    wb_built = sum(1 for w in wb_details if w.get("viz_status") == "built")
    wb_warned = sum(1 for w in wb_details if w.get("viz_status") == "warned")
    wb_error = sum(1 for w in wb_details if w.get("viz_status") == "error")
    wb_pbip_built = sum(1 for w in wb_details if w.get("pbip_status") == "built")
    visuals_rebuilt = sum(1 for w in wb_details for f in (w.get("viz_fidelity") or [])
                          if f.get("status") == "rebuilt")
    visuals_warned = sum(1 for w in wb_details for f in (w.get("viz_fidelity") or [])
                         if f.get("status") == "warned")
    sigs = [w.get("binding_signal") for w in wb_details if w.get("binding_signal")]
    workbooks_published_ds = sum(1 for sig in sigs if sig.get("kind") == "published")
    workbooks_embedded_ds = sum(1 for sig in sigs if sig.get("kind") == "embedded")
    workbooks_rebind_candidate = sum(1 for sig in sigs
                                     if sig.get("recommendation") == "candidate_rebind_to_published")
    # Implicit row counts (object-id COUNT(*) / legacy [Number of Records]) left unbound because the
    # model build did not supply a COUNTROWS measure target. Surfaces the cross-layer gap as an
    # estate roll-up so the volume is explicit (these are warned, never silently dropped/mis-bound).
    implicit_row_count_unbound = sum(w.get("viz_implicit_row_count", 0) for w in wb_details)
    workbooks_implicit_row_count = sum(1 for w in wb_details
                                       if w.get("viz_implicit_row_count", 0) > 0)

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
        "partitions_stubbed_total": partitions_stubbed_total,
        "workbooks_total": len(wb_details),
        "workbooks_viz_built": wb_built,
        "workbooks_viz_warned": wb_warned,
        "workbooks_viz_error": wb_error,
        "workbooks_pbip_built": wb_pbip_built,
        "visuals_rebuilt": visuals_rebuilt,
        "visuals_warned": visuals_warned,
        "workbooks_published_ds": workbooks_published_ds,
        "workbooks_embedded_ds": workbooks_embedded_ds,
        "workbooks_rebind_candidate": workbooks_rebind_candidate,
        "implicit_row_count_unbound": implicit_row_count_unbound,
        "workbooks_implicit_row_count": workbooks_implicit_row_count,
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

    partitions = [
        dict(p, datasource=d["name"])
        for d in report["datasources"]
        for p in (d.get("partitions_needs_review") or [])
    ]
    if partitions:
        lines += [
            "",
            "## Next step — manual M partition completion",
            "",
            f"{len(partitions)} table partition(s) emitted a deploy-valid but incomplete "
            "scaffold (an empty typed table) because the upstream query couldn't be auto-emitted "
            "(e.g. custom SQL on a connector whose native query isn't yet verified). Complete each "
            "partition's M by hand — the original SQL is preserved in `report.json` under the "
            "datasource's `partitions_needs_review`.",
            "",
            "| Datasource | Table | Reason |",
            "|---|---|---|",
        ]
        for p in partitions:
            lines.append(
                f"| {p.get('datasource')} | {p.get('table')} | {p.get('reason') or '-'} |"
            )

    if report["fallbacks"]:
        lines += ["", "## Fallbacks (route to land-to-Delta + DirectLake)", ""]
        for f in report["fallbacks"]:
            lines.append(f"- **{f['datasource']}** ({f['fallback_path']}): {f['reason']}")

    if report["workbooks"]:
        lines += ["", "## Workbooks", "",
                  "| Workbook | Viz | Visuals (rebuilt/warned) | Project (.pbip) | Bound model | Note |",
                  "|---|---|---|---|---|---|"]
        for w in report["workbooks"]:
            fid = w.get("viz_fidelity") or []
            rebuilt = sum(1 for f in fid if f.get("status") == "rebuilt")
            warned = sum(1 for f in fid if f.get("status") == "warned")
            lines.append(
                f"| {w['name']} | {w.get('viz_status', '')} | {rebuilt}/{warned} "
                f"| {w.get('pbip_folder') or '-'} | {w.get('bound_model') or '-'} "
                f"| {w.get('note') or ''} |")
        if any(w.get("pbip_folder") for w in report["workbooks"]):
            lines += [
                "",
                "> **Open locally:** each rebuilt workbook with a bound model has a self-contained, "
                "openable Power BI project at `pbip/<Workbook>/<Workbook>.pbip` (report + a model "
                "rebuilt from the workbook's own embedded datasource) — double-click to open it in "
                "Power BI Desktop. The `semantic_models/` folders remain the canonical deploy target.",
            ]
        if s.get("implicit_row_count_unbound", 0):
            lines += [
                "",
                f"> **Implicit row counts:** {s['implicit_row_count_unbound']} implicit count "
                f"measure(s) across {s['workbooks_implicit_row_count']} workbook(s) "
                "(Tableau's `COUNT(*)` / legacy `Number of Records`) are flagged for manual "
                "attention — add a `COUNTROWS` measure to the fact table and bind it. These are "
                "warned, never emitted as a dangling reference.",
            ]

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
def _load_approved_dax(path):
    """Load a ``{calc_name: dax}`` mapping of human-approved assisted translations from a JSON file.

    Returns ``None`` when ``path`` is falsy (the run is then byte-identical to a no-approval run).
    Raises ``ValueError`` when the file is missing, unreadable, not JSON, or not a flat object of
    string -> string -- a fail-fast so a typo never silently drops an approval. Tolerates a UTF-8
    BOM (the file is often hand-authored on Windows).
    """
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ValueError(f"--approved-dax file not found: {path}")
    except (OSError, ValueError) as exc:  # ValueError covers json.JSONDecodeError
        raise ValueError(f"--approved-dax file is not readable JSON ({path}): {exc}")
    if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError(
            f"--approved-dax JSON must be an object mapping calc name -> DAX string ({path})")
    return data or None


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
    parser.add_argument("--approved-dax", metavar="JSON",
                        help="path to a {calc_name: dax} JSON file of human-approved second-compiler "
                             "(assisted-translation) results; each name-matching stub lands as a "
                             "live, audit-stamped measure/calc column instead of an inert stub")
    parser.add_argument("--viz-advice", action="store_true",
                        help="also write a reports/<Name>.viz-advice.json sidecar per workbook with "
                             "ranked alternative chart types per visual (Tier-2 viz advisor; "
                             "deterministic, additive, never alters the rebuilt PBIR)")
    args = parser.parse_args(argv)

    try:
        approved_calc_dax = _load_approved_dax(args.approved_dax)
    except ValueError as exc:
        parser.error(str(exc))

    source = LocalFilesSource(args.input)
    report = migrate_estate(source, args.output, pbip=not args.no_pbip,
                            approved_calc_dax=approved_calc_dax, viz_advice=args.viz_advice)
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
              f"('Next step') to run them through the second compiler, then re-run with "
              f"--approved-dax <file.json> to land the approved results.")
    if s.get("partitions_stubbed_total"):
        print(f"Next step: {s['partitions_stubbed_total']} table partition(s) need manual M "
              f"completion -> see summary.md ('manual M partition completion'); the original SQL "
              f"is preserved in report.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
