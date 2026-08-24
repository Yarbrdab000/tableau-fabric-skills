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
reported as a *needs-storage-decision* fallback (default: rebuild direct-to-source as Import;
land-to-Delta + DirectLake is an explicit opt-in, never auto-selected) rather than emitted wrong.
No credentials are read, stored, or written anywhere in the bundle.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timezone

try:  # works whether imported as a package or run with scripts/ on sys.path
    from .connection_to_m import (parse_tds, locale_dependent_flatfile_relations, extract_bundled_flatfile, extract_calcs,
                                  combine_descriptors)
    from .storage_mode import (select_storage_mode, normalize_storage_decision,
                                FALLBACK_LAND_TO_DELTA, FALLBACK_NEEDS_DECISION)
    from .assemble_model import (assemble_import_model, assemble_local_import_model,
                                 materialize_bundled_flatfile_data, write_model_folder,
                                 write_local_pbip, migrate_datasource, list_workbook_datasources,
                                 _extract_is_only_data, _win_long_path)
    from .parameters import parse_parameters
    from .workbook_table_calcs import extract_table_calc_usages, load_workbook_xml
    from .workbook_calc_usage import workbook_calc_usage
    from .tmdl_generate import tableau_measure_format_to_pbi
    from . import fetch_tds as F
except ImportError:
    from connection_to_m import (parse_tds, locale_dependent_flatfile_relations, extract_bundled_flatfile, extract_calcs,
                                 combine_descriptors)
    from storage_mode import (select_storage_mode, normalize_storage_decision,
                               FALLBACK_LAND_TO_DELTA, FALLBACK_NEEDS_DECISION)
    from assemble_model import (assemble_import_model, assemble_local_import_model,
                                materialize_bundled_flatfile_data, write_model_folder,
                                write_local_pbip, migrate_datasource, list_workbook_datasources,
                                _extract_is_only_data, _win_long_path)
    from parameters import parse_parameters
    from workbook_table_calcs import extract_table_calc_usages, load_workbook_xml
    from workbook_calc_usage import workbook_calc_usage
    from tmdl_generate import tableau_measure_format_to_pbi
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
    a folder, the asset is processed ONCE: a datasource keeps the unpacked ``.tds`` copy, but a
    WORKBOOK keeps the packaged ``.twbx`` -- only the archive carries the dashboard image/asset bytes
    (logos, icons) a bare ``.twb`` drops, and the engine reads the workbook XML transparently from
    either. This matters because a live/server workbook download writes BOTH twins side by side (the
    archive plus its extracted ``.twb``), so choosing the ``.twbx`` is what keeps its images. Ids are
    absolute file paths; the display name is the file stem.
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
    def _dedup_by_stem(paths, prefer_packaged=False):
        # A packaged export (.tdsx/.twbx) and its unpacked twin (.tds/.twb) describe ONE asset; emit it
        # once so the output bundle has no duplicate datasource / name collision. Which twin wins:
        #   * datasources (``prefer_packaged`` False) -- the unpacked ``.tds`` wins: already text, and
        #     the copy a user is most likely editing.
        #   * workbooks (``prefer_packaged`` True) -- the packaged ``.twbx`` wins: ONLY the archive
        #     carries the dashboard image/asset bytes (logos, icons); a bare ``.twb`` twin drops them,
        #     so preferring it would silently lose every image. The engine reads the workbook XML
        #     transparently from either, so choosing the archive costs nothing. A live/server download
        #     writes BOTH twins into the input folder, which is exactly when this preference matters.
        # Order-independent: the choice never depends on which twin ``os.walk`` happened to list first.
        chosen = {}
        for p in paths:
            stem, ext = os.path.splitext(os.path.basename(p))
            key = (os.path.dirname(p), stem.lower())
            packaged = ext.lower() in (".tdsx", ".twbx")
            if key not in chosen:
                chosen[key] = (p, packaged)
                continue
            stored_packaged = chosen[key][1]
            if prefer_packaged:
                if packaged and not stored_packaged:
                    chosen[key] = (p, packaged)
            elif stored_packaged and not packaged:
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
        # Prefer the ``.twbx`` twin: only the archive carries the dashboard image bytes (see
        # ``_dedup_by_stem``), so a server download that lands both twins keeps its logos/icons.
        return self._dedup_by_stem(self._discover(".twb") + self._discover(".twbx"),
                                   prefer_packaged=True)

    def read_workbook(self, wb_id):
        # ``load_workbook_xml`` transparently handles both a bare ``.twb`` and a packaged ``.twbx``.
        return load_workbook_xml(wb_id)

    def asset_name(self, asset_id):
        # The filename stem, minus any transfer-layer UUID prefix (see _TRANSFER_UUID_PREFIX).
        # Naming only -- ``asset_id`` stays the real path, so discovery and reads are untouched.
        return strip_transfer_uuid(os.path.splitext(os.path.basename(asset_id))[0])

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


def _island_qualified_calc_name(caption, datasource, used):
    """Island-qualified name for a repeat calc caption whose FORMULA differs, or ``None``.

    Tableau scopes calculated fields per DATASOURCE, so one consolidated multi-datasource workbook
    can legitimately declare the same caption twice with *different* formulas -- typically a
    dashboard copied to a second datasource whose calc was then edited (a swap calc repointed at
    that island's own parameter, an ``Age`` redefined as an LOD, ...). Collapsing those into one
    flat name space keeps the first and silently hands every OTHER island's worksheets the wrong
    formula: a wrong answer with no error, the worst failure class we ship.

    The suffix mirrors the island tag ``combine_descriptors`` already puts on colliding TABLE names
    (``pmdm__Program__c (Intake)``), so the same workbook reads the same way at every layer, and
    ``twb_to_pbir`` can rebuild the key from a pill's own datasource caption.

    Returns ``None`` when there is no datasource to name the repeat by (a worksheet-local
    ``<datasource-dependencies>`` copy), or when the qualified name is already taken -- the caller
    then drops the repeat exactly as before. Fail-closed: never invent an ambiguous second name.
    """
    if not (caption and datasource):
        return None
    name = f"{caption} ({datasource})"
    return None if name.lower() in used else name


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

    De-duplication is per ``(caption, formula)``, not per caption: a repeat of a caption ALREADY
    seen with the SAME formula is skipped as a duplicate (the common case -- worksheet-local
    ``<datasource-dependencies>`` copies of the same field), but a repeat whose formula DIFFERS is
    a genuinely different calculation belonging to another datasource island and is kept under an
    island-qualified name (``"<caption> (<datasource>)"``, see
    :func:`_island_qualified_calc_name`). Without this the first island's formula silently answered
    for every island. Repeats we cannot name by island fall back to the original drop.

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

    # caption.lower() -> {formula: emitted name}. Keyed by BOTH caption and formula so a repeat of
    # the same calc collapses (as before) while a same-captioned calc with a DIFFERENT formula --
    # another datasource island's own version -- survives under an island-qualified name.
    seen = {}
    used = set()  # every emitted name, lower-cased; keeps an island-qualified name unique
    # Map each <column> element (by object identity) to its owning datasource caption. In a
    # multi-datasource workbook the same caption means different things per datasource, so a calc's
    # home island must be recorded for the M field resolver to be scoped to it. ``root`` stays alive
    # for the whole function, so id(col) below matches id(c) here. A None value (column under no
    # <datasource>, or single-datasource run) degrades to global resolution -- byte-identical.
    col_ds = {}
    for ds_el in (e for e in root.iter() if _local(e.tag) == "datasource"):
        ds_name = ds_el.get("caption") or ds_el.get("name")
        if not ds_name:
            continue
        for c in (x for x in ds_el.iter() if _local(x.tag) == "column"):
            col_ds.setdefault(id(c), ds_name)

    def _claim(caption, formula, col_el):
        """Reserve an emitted name for this calc, or ``None`` when it is a true duplicate.

        First sighting of a caption keeps the caption verbatim (byte-identical to the previous
        caption-only dedup). A repeat with the same formula is a duplicate. A repeat whose formula
        differs is another island's calc and gets an island-qualified name when one is available.
        """
        low = caption.lower()
        forms = seen.get(low)
        if forms is None:
            seen[low] = {formula: caption}
            used.add(low)
            return caption
        if formula in forms:
            return None
        alt = _island_qualified_calc_name(caption, col_ds.get(id(col_el)), used)
        if alt is None:
            return None
        forms[formula] = alt
        used.add(alt.lower())
        return alt

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
            emitted = _claim(caption, formula, col)
            if emitted is None:
                skipped.append({"name": caption, "reason": "duplicate calculated-field name"})
                continue
            dim_entry = {"name": emitted, "formula": formula, "role": role}
            if internal_name and internal_name.lower() != caption.lower():
                dim_entry["internal_name"] = internal_name
            if emitted != caption:
                dim_entry["base_name"] = caption
            dim_entry["datasource"] = col_ds.get(id(col))
            dim_calcs.append(dim_entry)
            continue
        emitted = _claim(caption, formula, col)
        if emitted is None:
            skipped.append({"name": caption, "reason": "duplicate calculated-field name"})
            continue
        entry = {"name": emitted, "formula": formula}
        if internal_name and internal_name.lower() != caption.lower():
            entry["internal_name"] = internal_name
        if emitted != caption:
            entry["base_name"] = caption
        entry["datasource"] = col_ds.get(id(col))
        # Tableau's declared result type, carried so the model build can recognise a STRING-valued
        # measure (a categorical label calc such as ``IF SUM([Profit]) < 0 THEN "negative" ELSE
        # "positive" END``) and give it a colour twin. Additive; absent when undeclared.
        _dt = (col.get("datatype") or "").strip()
        if _dt:
            entry["datatype"] = _dt
        # Author's explicit number format (currency/percent/precision) declared on the calc
        # <column @default-format>. Conservatively decoded (explicit c/n/p/* codes only; the
        # ambiguous built-in C<lcid>% form is declined) so the measure keeps the author's format
        # instead of degrading to the raw number. Additive: absent when there is no decodable code.
        fmt = tableau_measure_format_to_pbi(col.get("default-format"))
        if fmt:
            entry["format_string"] = fmt
        calcs.append(entry)

    return (calcs, skipped, dim_calcs) if include_dimensions else (calcs, skipped)


# -- orchestration helpers -----------------------------------------------------
_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# Longest folder/report base name this tool will emit. A rebuilt PBIR project nests the SAME base
# name TWICE (``pbip/<base>/<base>.Report/...``) before a deeply-nested
# ``definition/pages/<page>/visuals/<visual>/visual.json`` tail, so an untrimmed base is spent twice
# against the Windows MAX_PATH budget. Measured on a real customer workbook: a 77-char title pushed
# ``visual.json`` to 278 chars. The files were written correctly (the writer uses ``\\?\``), but
# Power BI Desktop could not READ them, so the project opened with an empty canvas -- the worst
# possible failure, because every artifact is present and correct on disk.
#
# 64 keeps both copies plus the ~101-char structural tail inside the budget for any reasonable output
# root, while staying long enough that real titles remain recognisable. Truncation is deterministic
# and collision-safe: a content hash of the FULL name is appended, so two workbooks sharing a long
# prefix still land in distinct folders and the same workbook always yields the same folder.
_MAX_FS_BASE = 64

# A canonical UUID stamped on the FRONT of a filename by a TRANSFER layer -- chat/Copilot
# attachments, portal and ticketing downloads, SharePoint -- e.g.
# ``0e7f6d6d-3d7b-44b3-bbb2-83cd83194c13-Network Operational PowerBI Mock - 24Jul26 ORC.twbx``.
# It is never part of a Tableau author's name, and it does real damage rather than just looking
# untidy: 36 characters plus a separator consume most of the ``_MAX_FS_BASE`` budget, so the
# author's ACTUAL name is truncated and a disambiguation hash appended. Measured on a real run --
# ``0e7f6d6d-3d7b-44b3-bbb2-83cd83194c13-Network Operationa-ac65b89d`` -- where the meaningful part
# of the name survived as the word "Operationa". It also defeats the name-based
# workbook<->datasource rebind index, because two attachments of the same asset carry DIFFERENT
# uuids and so no longer match each other.
#
# Strict by design: the canonical 8-4-4-4-12 hex shape, anchored at position 0, followed by a
# separator, and applied only when a non-empty remainder survives. Local-file source ONLY -- a
# live/server asset name comes from Tableau itself and is authoritative, so it is never rewritten.
_TRANSFER_UUID_PREFIX = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}[-_ ]+")


def strip_transfer_uuid(stem):
    """Drop a transfer-layer UUID prefix from a filename stem. A no-op when none is present."""
    stripped = _TRANSFER_UUID_PREFIX.sub("", stem or "", count=1).strip()
    return stripped or (stem or "")


def _fs_safe(name, default="model"):
    """A filesystem-safe base for a name (no estate-wide de-duplication).

    Long names are truncated to ``_MAX_FS_BASE`` with a short content hash appended, so the emitted
    path stays inside the Windows MAX_PATH budget (see ``_MAX_FS_BASE``) without ever collapsing two
    distinct names onto one folder.
    """
    safe = _INVALID_FS.sub("_", name or "").strip().rstrip(".") or default
    if len(safe) <= _MAX_FS_BASE:
        return safe
    digest = hashlib.sha256(safe.encode("utf-8")).hexdigest()[:8]
    keep = _MAX_FS_BASE - len(digest) - 1
    return f"{safe[:keep].strip().rstrip('.')}-{digest}"


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


def _viz_adapter(cand, layout=None):
    """Adapt a viz entry point to the orchestrator's ``callable(twb_text, name) -> dict`` contract.

    Stream B's ``migrate_twb_to_pbir(text, *, report_name, dataset_name)`` takes the target name as
    keyword-only args, while a generic plugin may take ``(text, name)`` positionally. Inspect the
    signature so the workbook display name flows through as the report/dataset name either way.

    ``layout`` names the zone-layout engine (``"legacy"`` / ``"solver"``). It is BOUND HERE rather
    than passed at each call site, so every path through the orchestrator picks it up and none can
    silently drop it. Capability-gated like every other optional binding: a viz stage whose
    signature has no ``layout`` parameter is called exactly as before, so an injected or older
    plugin keeps working and the default run stays byte-identical.
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
    supports_column = "column_binding" in params
    supports_resources = "resources" in params
    supports_layout = "layout" in params
    def _call(twb_text, name, date_binding=None, measure_binding=None, row_count_binding=None,
              param_binding=None, model_table=None, field_map=None, column_binding=None,
              resources=None):
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
            if supports_column and column_binding is not None:
                kwargs["column_binding"] = column_binding
            if supports_resources and resources:
                kwargs["resources"] = resources
            if supports_layout and layout:
                kwargs["layout"] = layout
            return cand(twb_text, **kwargs)
        return cand(twb_text, name)
    return _call


def _resolve_viz_stage(injected, layout=None):
    """Resolve the optional workbook viz stage without ever hard-depending on it.

    An injected callable wins. Otherwise, if a ``twb_to_pbir`` module is importable (Stream B),
    bind the first recognized entry point. Returns a ``callable(twb_text, name) -> dict`` or
    ``None`` when no viz stage is available.

    ``layout`` selects the zone-layout engine and is bound into the adapter, so it reaches every
    call site through one seam. An injected stage is returned untouched -- a caller that supplies
    its own viz stage owns its own configuration.
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
            return _viz_adapter(cand, layout=layout)
    return None


def _migrate_one_datasource(source, ds_id, sm_dir, used_folders, pbip_dir=None, ds_catalog=None,
                            approved_calc_dax=None, storage_decisions=None):
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
    decision = select_storage_mode(
        descriptor,
        storage_decision=_storage_decision_for(descriptor.get("datasource_name") or name, 
                                              storage_decisions))
    detail.update(connector=connector, skipped_calcs=skipped_calcs, dim_calcs=dim_calcs)

    if decision.get("mode") is None:
        detail.update(status="fallback", storage_mode=None, storage_decision=decision,
                      reason=decision.get("rationale"),
                      fallback_path=decision.get("fallback") or FALLBACK_NEEDS_DECISION)
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

    # Flat-file Import (Excel/CSV or extract bundled inside a .tdsx/.twbx): materialize the embedded
    # data to an ABSOLUTE path so the emitted M's File.Contents loads in Power BI Desktop. A relative
    # path opens but loads NO data ("The supplied file path must be a valid absolute path"). A bundled
    # Excel/CSV is lifted out verbatim; an EXTRACT-backed source (only a .hyper packaged) is read to
    # one CSV per table and built as a local-CSV Import model. A live DB source (Snowflake/Databricks/
    # SQL Server/...) carries no flatfile_filename -> no-op; its connection string is left as-is.
    # Resolve the output folder name up-front (mutates used_folders -> call exactly once) so flat-file
    # data can land INSIDE the .pbip project below.
    safe_base = _safe_folder(name, used_folders)

    flatfile_path = None
    table_csv_paths = None
    ff_mat = None
    if (descriptor.get("flatfile_filename") or decision.get("import_from_extract")
            or _extract_is_only_data(descriptor, decision)):
        if pbip_dir is not None:
            # Land the data INSIDE the openable project (pbip/<name>/<name>.Data, beside the
            # .SemanticModel) so the whole folder is self-contained + portable; a relocatable
            # SourceFolder parameter (set below) points the emitted File.Contents at it.
            data_dir = os.path.join(pbip_dir, safe_base, safe_base + ".Data")
        else:
            data_dir = os.path.join(os.path.dirname(os.path.abspath(sm_dir)), "data",
                                    re.sub(r"[^\w.-]+", "_", name) or "ds")
        try:
            if os.path.isdir(data_dir):
                shutil.rmtree(data_dir)  # clean rerun: never mix stale data files
        except OSError:
            pass
        try:
            ff_mat = materialize_bundled_flatfile_data(ds_id, descriptor, data_dir, model_name=name)
        except Exception:
            ff_mat = None
        if ff_mat and ff_mat.get("kind") == "flatfile":
            flatfile_path = ff_mat.get("flatfile_path")
        elif ff_mat and ff_mat.get("kind") == "csv":
            table_csv_paths = ff_mat.get("table_csv_paths")
        # Data landed inside the .pbip -> emit the relocatable SourceFolder parameter (default = the
        # absolute .Data folder) so moving/zipping the project only needs that one value re-pointed.
        if pbip_dir is not None and (flatfile_path or table_csv_paths):
            descriptor["flatfile_source_folder"] = os.path.abspath(data_dir)
    detail["flatfile_landed"] = flatfile_path
    if ff_mat is not None:
        detail["flatfile_data"] = {
            "landed": ff_mat.get("kind") is not None,
            "kind": ff_mat.get("kind"),
            "reason": ff_mat.get("reason"),
            "hyper_present": ff_mat.get("hyper_present", False),
        }

    # Extract-backed SaaS (import_from_extract) whose bundled .hyper did NOT materialize to CSV: fail
    # closed to the honest needs-storage-decision fallback rather than emitting a dataless/broken
    # model for an unmapped connector (the estate would otherwise write a model that opens with no
    # data). Mirrors the mode-None fallback above; the honest flatfile_data record is preserved.
    if decision.get("import_from_extract") and not table_csv_paths:
        detail.update(status="fallback", storage_mode=None, storage_decision=decision,
                      reason=(ff_mat or {}).get("reason") or decision.get("rationale"),
                      fallback_path=decision.get("fallback") or FALLBACK_NEEDS_DECISION)
        return detail

    try:
        if table_csv_paths:
            out = assemble_local_import_model(descriptor, model_name=name,
                                              table_csv_paths=table_csv_paths, calcs=calcs,
                                              dim_calcs=dim_calcs, parameters=parameters,
                                              approved_calc_dax=approved_calc_dax)
        else:
            out = assemble_import_model(descriptor, model_name=name, calcs=calcs, dim_calcs=dim_calcs,
                                        parameters=parameters, approved_calc_dax=approved_calc_dax,
                                        flatfile_path=flatfile_path)
    except ValueError as exc:  # storage policy / no-columns -> documented needs-storage-decision fallback
        detail.update(status="fallback", storage_mode=None, storage_decision=decision,
                      reason=str(exc),
                      fallback_path=decision.get("fallback") or FALLBACK_NEEDS_DECISION)
        return detail
    except Exception as exc:
        detail.update(status="error", storage_decision=decision,
                      error=f"{type(exc).__name__}: {exc}")
        return detail

    folder = safe_base + ".SemanticModel"
    dest = os.path.join(sm_dir, folder)
    try:
        if os.path.isdir(dest):
            shutil.rmtree(_win_long_path(dest))  # clear stale parts so a rerun never leaves renamed/dropped tables
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
        data_child = safe_base + ".Data"
        try:
            if os.path.isdir(ds_pbip_dir):
                # Clear stale project parts but KEEP the freshly-materialized <name>.Data folder
                # (landed above) so the flat-file data stays bundled inside the project.
                for _child in os.listdir(ds_pbip_dir):
                    if _child == data_child:
                        continue
                    _p = os.path.join(ds_pbip_dir, _child)
                    shutil.rmtree(_win_long_path(_p)) if os.path.isdir(_p) else os.remove(_win_long_path(_p))
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

    # Honest flat-file data follow-up: a flat-file source whose data did NOT materialize to an
    # absolute path yields a model that opens but loads no rows. Record it as a follow-up (and force
    # the with-followups status) so the run never silently reports a clean migration of empty tables.
    followups = list(decision.get("manual_followups", []))
    if detail.get("flatfile_data") and not detail["flatfile_data"].get("landed"):
        _reason = detail["flatfile_data"].get("reason")
        _hint = {
            "hyperapi_unavailable": "bundles a .hyper extract but tableauhyperapi is not installed "
                                    "(pip install tableauhyperapi), so its data was not landed",
            "no_bundled_data": "bundles neither the source file nor a .hyper extract -- re-export "
                               "the .tdsx/.twbx with its extract included",
        }.get(_reason, f"data not materialized ({_reason})")
        followups.append(f"flat-file source {_hint}; the model opens but loads no rows until the "
                         "data file is supplied at an absolute path")
        fully = False

    # Header reconciliation follow-up: a Tableau alias whose physical header could not be located
    # positionally is emitted as a warning (never a wrong binding) -- surface it so the user can
    # confirm the source column mapping. Successful remaps need no follow-up (they load correctly).
    _hdr = report.get("flatfile_header_reconcile")
    if _hdr and _hdr.get("mismatches"):
        detail["flatfile_header_reconcile"] = _hdr
        for _mm in _hdr["mismatches"]:
            followups.append(
                f"flat-file column '{_mm.get('model_name')}' (Tableau source name "
                f"'{_mm.get('source_column')}') did not match any physical header in "
                f"'{_mm.get('relation')}' -- verify the source column name")
        fully = False
    elif _hdr and _hdr.get("remaps"):
        detail["flatfile_header_reconcile"] = _hdr

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
        column_prune=report.get("column_prune"),
        manual_followups=followups,
    )
    if ds_catalog is not None:
        entry = {"name": name, "text": text, "safe_base": safe_base,
                 "flatfile_path": flatfile_path,
                 "table_csv_paths": table_csv_paths}
        # Index under EVERY name this datasource answers to, not just the one the file happens to be
        # called. A published datasource travels to a workbook as a ``sqlproxy`` stub whose caption is
        # the datasource's DISPLAY NAME on the server ("Meridian Sales (Live Snowflake)"), while the
        # exported ``.tds`` is usually named for the content ("MeridianSales.tds") -- so keying only
        # the file stem missed a match that was sitting right there, and the workbook was skipped
        # ("relation 'sqlproxy' has no resolvable columns") even though its datasource had migrated
        # successfully in the SAME RUN. Measured at 9 of 38 workbooks on a live site (issue #105), and
        # the fraction GROWS with governance: a shared published datasource is the recommended
        # Tableau pattern, so a well-run estate is mostly sqlproxy.
        #
        # A key that two different datasources both claim is AMBIGUOUS and is withheld rather than
        # letting whichever migrated last win -- the same rule the field and row-count resolvers use.
        for alias in _datasource_catalog_aliases(name, text):
            key = _norm_ds(alias)
            if not key:
                continue
            if key in ds_catalog and ds_catalog[key].get("name") != name:
                ds_catalog[key] = _AMBIGUOUS_CATALOG_ENTRY
            elif key not in ds_catalog or ds_catalog[key] is _AMBIGUOUS_CATALOG_ENTRY:
                ds_catalog[key] = entry
        ds_catalog[_norm_ds(name)] = entry      # the file's own name always wins for itself
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


_FIDELITY_DEFERRAL_MARKERS = (
    "aggregate/measure filter on ",   # B7: dropped aggregate/measure filter (visual renders without it)
    "grain not applied",              # date-grain approximation, fail-closed (visual still renders)
    "default continuous palette",     # colour scale fell back to Tableau's default palette (approx)
    "default palette",
)


def _fidelity_tier(status, visual_type, reason):
    """Additive tier for a viz-fidelity row: ``rebuilt`` | ``rebuilt_with_deferrals`` | ``degraded`` | ``empty``.

    A strictly additive refinement of ``status`` (never mutates it) so a visual that renders minus a
    deferred, fail-closed feature (a dropped aggregate/measure filter, a date-grain approximation, a
    default colour palette -- the documented faithful-or-stub deferrals) stops being conflated with an
    outright failure. ``empty`` = no faithful visual emitted; ``degraded`` = a rendered visual whose
    warning is a genuine degradation, not a known safe deferral; ``rebuilt`` = a clean rebuild.
    """
    reason = reason or ""
    if visual_type in (None, "unsupported"):
        return "empty"
    if status == "rebuilt":
        return "rebuilt_with_deferrals" if reason else "rebuilt"
    if any(m in reason for m in _FIDELITY_DEFERRAL_MARKERS):
        return "rebuilt_with_deferrals"
    return "degraded"


def _viz_worklist(result):
    """The per-visual remediation worklist a viz result already carries, or ``None``.

    ``twb_to_pbir`` COMPUTES this (``remediation_worklist.build_worklist`` over the same warnings +
    candidate records ``_viz_fidelity`` summarises) and hands it back on ``result["worklist"]`` -- but
    nothing on the ESTATE path carried it into ``report.json``, so the richest machine-readable
    artifact the engine produces was computed and then dropped exactly where an agent or CI job would
    read it. A consumer saw ``pending_gates[{gate: "dashboard_audit", count: N}]`` -- HOW MANY visuals
    need attention but not WHICH -- and had to re-derive the targets from the emitted PBIR, which is
    the drift the worklist exists to prevent.

    ``viz_fidelity`` is a thinner projection of the same facts (one row per worksheet); this is the
    full per-ITEM audit with severity, category and remediation text. Both are kept: the summary is
    what the report's own rollups count, the worklist is what a remediator acts on.

    Returns ``None`` when the worklist module was unimportable or the viz stage produced none, so a
    run without it is byte-identical to before (additive, never raises).
    """
    if not isinstance(result, dict):
        return None
    wl = result.get("worklist")
    return wl if isinstance(wl, dict) else None


def _viz_fidelity(result):
    """Per-worksheet rebuild fidelity from a viz result: ``[{worksheet, visual_type, status, reason, tier}]``.

    ``status`` is ``"rebuilt"`` for a worksheet emitted cleanly and ``"warned"`` for one the viz
    stage flagged (or an unsupported visual type). Dashboard-scope or unmatched warnings are kept as
    their own ``warned`` rows so nothing is dropped. Reasons reuse the engine's
    ``"manual attention required: "`` prefix. ``tier`` is an ADDITIVE refinement of ``status`` (see
    ``_fidelity_tier``) -- ``status`` itself is unchanged, so existing consumers are byte-identical.
    """
    ir = result.get("ir") if isinstance(result, dict) else None
    warnings = (result.get("warnings") if isinstance(result, dict) else None) or []
    worksheets = (ir or {}).get("worksheets", []) if isinstance(ir, dict) else []
    ws_names = {w.get("name") for w in worksheets}

    # One worksheet can fail in SEVERAL distinct ways -- an unbound row count, a dropped filter, a
    # deferred visual -- and only the FIRST was ever carried into the report; the rest were discarded
    # here, silently. That is the honesty contract inverted: the reader is told a worksheet is warned
    # and given one cause, with no signal that others exist. It also hides the most specific finding
    # whenever a coarser one happens to be recorded first (measured: a `table-calc filter` diagnosis
    # disappeared behind a generic "no usable field bindings" note on the same sheet).
    #
    # Kept ADDITIVE: `reason` still carries the first warning byte-for-byte, so every existing
    # consumer is unchanged, and the remainder are exposed under a new `additional_reasons` key that
    # is present only when there IS more to say.
    warned_ws, warned_more, extra = {}, {}, []
    for w in warnings:
        if w.get("scope") == "worksheet" and w.get("name") in ws_names:
            nm = w.get("name")
            if nm not in warned_ws:
                warned_ws[nm] = w.get("reason")
            elif w.get("reason") not in (warned_ws[nm],) + tuple(warned_more.get(nm, ())):
                warned_more.setdefault(nm, []).append(w.get("reason"))
        else:
            extra.append(w)

    fidelity = []
    for ws in worksheets:
        nm, vt = ws.get("name"), ws.get("visual_type")
        if nm in warned_ws:
            _row = {"worksheet": nm, "visual_type": vt,
                    "status": "warned", "reason": warned_ws[nm],
                    "tier": _fidelity_tier("warned", vt, warned_ws[nm])}
            if warned_more.get(nm):
                _row["additional_reasons"] = list(warned_more[nm])
            fidelity.append(_row)
        elif vt in (None, "unsupported"):
            _r = "manual attention required: unsupported visual type"
            fidelity.append({"worksheet": nm, "visual_type": vt, "status": "warned",
                             "reason": _r, "tier": _fidelity_tier("warned", vt, _r)})
        else:
            _note = ws.get("fidelity_note")
            fidelity.append({"worksheet": nm, "visual_type": vt,
                             "status": "rebuilt", "reason": _note,
                             "tier": _fidelity_tier("rebuilt", vt, _note)})
    for w in extra:
        fidelity.append({"worksheet": w.get("name"), "visual_type": w.get("scope"),
                         "status": "warned", "reason": w.get("reason"),
                         "tier": _fidelity_tier("warned", w.get("scope"), w.get("reason"))})
    return fidelity


def _visual_calc_rollup(result):
    """Additive routing-decision rollup for the view-only quick-table-calc -> Visual-Calculation path.

    Summarizes the per-visual ``visual_calc`` facts the viz stage recorded on its candidate records
    (see ``twb_to_pbir._apply_visual_calcs``): how many worksheets were emitted as Power BI Visual
    Calculations (split by marks role and by calc family), how many carried a hidden inner calc in a
    two-pass chain, and how many were routed to review (with reasons). Purely a CONSUMER of facts the
    viz stage already produced -- it never re-derives a calc. Returns ``None`` when no visual-calc
    facts were recorded, so the report key is added ONLY when the path actually fired (byte-identical
    otherwise).
    """
    records = result.get("candidate_records") if isinstance(result, dict) else None
    facts = [r.get("visual_calc") for r in (records or [])
             if isinstance(r, dict) and isinstance(r.get("visual_calc"), dict)]
    if not facts:
        return None
    emitted = [f for f in facts if f.get("status") == "emitted"]
    review = [f for f in facts if f.get("status") == "review"]
    families = {}
    for f in emitted:
        fam = f.get("family") or "unknown"
        families[fam] = families.get(fam, 0) + 1
    rollup = {
        "emitted_total": len(emitted),
        "review_total": len(review),
        "by_role": {
            "value": sum(1 for f in emitted if f.get("role") == "value"),
            "color": sum(1 for f in emitted if f.get("role") == "color"),
        },
        "chained": sum(1 for f in emitted
                       if any(vc.get("is_inner") for vc in (f.get("visual_calcs") or []))),
        "families": families,
        "worksheets": [
            {"worksheet": f.get("worksheet"), "status": f.get("status"),
             "role": f.get("role"), "family": f.get("family"),
             "axis": f.get("axis"), "reason": f.get("reason")}
            for f in facts],
    }
    return rollup


def _color_scale_rollup(result):
    """Additive disclosure rollup for heat-scale fills that rode Tableau's DEFAULT continuous palette.

    When a table/matrix colour gradient carried no serialised ``<color-palette>`` (the author left the
    heatmap on Tableau's default automatic ramp, which serialises no colours), the viz stage synthesises
    a faithful-direction default gradient and stamps ``default_palette`` on the per-visual
    conditional-format / visual-calculation fact (see ``twb_to_pbir._parse_color_gradient`` and
    ``_disclose_default_palette``). The colour IS emitted -- strictly better than the prior silent drop --
    but it is an APPROXIMATION of the source, so this rollup names the affected worksheets in the report.
    The per-worksheet disclosure warning can be collapsed by ``_viz_fidelity``'s one-reason-per-worksheet
    summary (e.g. a heatmap that also warns on date grain), so this rollup GUARANTEES the approximation
    stays visible. Purely a CONSUMER of facts the viz stage already produced -- it never re-derives a
    palette; returns ``None`` when no default palette was synthesised (report byte-identical otherwise).
    """
    records = result.get("candidate_records") if isinstance(result, dict) else None
    worksheets = []
    for r in (records or []):
        if not isinstance(r, dict):
            continue
        cf, vc = r.get("conditional_format"), r.get("visual_calc")
        defaulted = ((isinstance(cf, dict) and cf.get("default_palette"))
                     or (isinstance(vc, dict) and vc.get("default_palette")))
        if defaulted:
            nm = r.get("worksheet")
            if nm and nm not in worksheets:
                worksheets.append(nm)
    if not worksheets:
        return None
    return {
        "count": len(worksheets),
        "worksheets": worksheets,
        "note": ("background colour scale used Tableau's default continuous palette (no serialised "
                 "colours); a default gradient was applied -- verify the colours against the source"),
    }


def _measure_filter_rollup(result):
    """Additive disclosure rollup for aggregate/measure filters the viz stage dropped to review.

    A Tableau worksheet filter on an aggregate (``SUM(Sales)``) or a calculated measure has no
    faithful slicer mapping -- ``twb_to_pbir._parse_filters`` warns ("aggregate/measure filter on
    '<field>' is not mapped to a slicer") and does NOT emit a possibly-wrong control (warn-never-wrong).
    That is the honest stub, but such a filter CHANGES THE NUMBERS a visual shows, and
    ``_viz_fidelity``'s one-reason-per-worksheet summary can collapse the warning behind another (e.g.
    a date-grain note on the same worksheet). This rollup scans the viz warnings and GUARANTEES every
    dropped aggregate/measure filter stays visible in the report, so a reviewer re-applies it manually.
    Purely a CONSUMER of warnings the viz stage already produced -- it emits nothing into the PBIR and
    never re-derives a filter; returns ``None`` when none were dropped (report byte-identical otherwise).
    """
    warnings = (result.get("warnings") if isinstance(result, dict) else None) or []
    seen, items = set(), []
    for w in warnings:
        if not isinstance(w, dict):
            continue
        reason = w.get("reason") or ""
        if "aggregate/measure filter on " not in reason:
            continue
        key = (w.get("name"), reason)
        if key in seen:
            continue
        seen.add(key)
        items.append({"worksheet": w.get("name"), "reason": reason})
    if not items:
        return None
    return {
        "count": len(items),
        "worksheets": items,
        "note": ("worksheet filter on an aggregate/calculated measure was left to review (no faithful "
                 "slicer mapping); it changes the values shown -- re-apply it as a visual-level filter "
                 "in Power BI"),
    }


_PBIP_WARN = "manual attention required: "


# The visualType -> required-roles table lives in ``pbir_lint`` (its R9 rule is the standing gate
# for this defect) and is READ here rather than copied. One table, two consumers: the emitter that
# must not produce an invalid visual, and the linter that must not let one through. Two copies would
# drift, and a gate drifting away from the emitter it guards is exactly what #137 was.


def _required_roles_table():
    """The shared table, or ``{}`` when ``pbir_lint`` cannot be imported.

    Fail-safe: an empty table means no visual is ever judged, which is the pre-#143 behaviour. A
    missing sibling module must not start emptying visuals.
    """
    try:
        import pbir_lint as _pl
        return _pl.REQUIRED_ROLES
    except Exception:  # pragma: no cover - sibling module is always importable in-package
        return {}


def _missing_required_role(vis, query_state):
    """Name a required role this visual no longer projects, or ``None`` when it is still valid.

    A visual whose ``visualType`` is unknown here returns ``None`` -- unknown means "we cannot
    judge", and emptying a visual we merely failed to recognise would be worse than the defect.
    """
    required = _required_roles_table().get((vis or {}).get("visualType"))
    if not required:
        return None
    for role in required:
        spec = (query_state or {}).get(role) or {}
        if not (spec.get("projections") or spec.get("fieldParameters")):
            return role
    return None


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


def _fp_swap_lookup(swap_specs):
    """``{lower-name: spec}`` for every convertible field-parameter swap.

    Keyed by the swap's ``calc_name``, ``display_col`` AND ``table_name`` (all lower-cased) so a
    report projection that named the parameter-driven measure calc under any of those resolves to
    its spec. Only specs that carry at least one resolvable ``entries`` candidate (a seed field) are
    indexed -- a spec with no seed cannot be rebound and must fall through to the drop path. Returns
    ``{}`` for ``None``/empty specs (the byte-identical no-swap path).
    """
    lut = {}
    for spec in (swap_specs or []):
        if not (isinstance(spec, dict) and (spec.get("entries") or [])):
            continue
        for key in (spec.get("calc_name"), spec.get("display_col"), spec.get("table_name")):
            if key:
                lut.setdefault(key.lower(), spec)
    return lut


def _ref_entity(field):
    """Return the ``SourceRef`` entity of a PBIR projection field node, or ``None``."""
    node = field if isinstance(field, dict) else {}
    if "Aggregation" in node:
        node = (node["Aggregation"] or {}).get("Expression", {}) or {}
    inner = (node.get("Measure") or node.get("Column") or {}) if isinstance(node, dict) else {}
    expr = (inner or {}).get("Expression") or {}
    return ((expr.get("SourceRef") or {}).get("Entity")) if isinstance(expr, dict) else None


def _fp_display_lookup(swap_specs):
    """``{(entity_lower, display_col_lower): spec}`` for every convertible field-parameter swap.

    Identifies a projection that names a field parameter's own DISPLAY column -- i.e. an axis the
    report bound as if the parameter table were an ordinary dimension table. Only specs with at
    least one resolvable candidate are indexed (an expansion needs a seed field). ``{}`` for
    ``None``/empty specs, which keeps the no-swap path byte-identical.
    """
    lut = {}
    for spec in (swap_specs or []):
        if not (isinstance(spec, dict) and (spec.get("entries") or [])):
            continue
        table, col = spec.get("table_name"), spec.get("display_col")
        if table and col:
            lut.setdefault((table.lower(), col.lower()), spec)
    return lut


def _strip_objects_for_refs(visual, dropped_refs):
    """Remove formatting objects scoped to a queryRef the visual no longer projects.

    PBIR scopes a per-column formatting rule with ``selector.metadata = <queryRef>``. When the
    reference crosscheck removes a projection -- typically a measure whose Tableau calc did not
    translate -- any rule scoped to it survives as a DANGLING reference into a measure the model
    does not contain. The visual then ships a conditional fill for a column that is not there.

    Exact-match only: an object is removed solely when its metadata is one of the queryRefs just
    dropped, so rules on surviving columns, and rules with no metadata scope at all (which apply to
    the whole visual), are left untouched. Returns the number of objects removed.
    """
    objects = visual.get("objects")
    if not (isinstance(objects, dict) and dropped_refs):
        return 0
    removed = 0
    for key in list(objects):
        entries = objects[key]
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries
                if not (isinstance(e, dict)
                        and ((e.get("selector") or {}).get("metadata") in dropped_refs))]
        removed += len(entries) - len(kept)
        if kept:
            objects[key] = kept
        else:
            del objects[key]
    if not objects:
        visual.pop("objects", None)
    return removed


def _crosscheck_report_refs(report_parts, model_parts, swap_specs=None):
    """Reconcile viz projections against the emitted model: REBIND a measure the model remodeled as
    a field parameter, else DROP a reference the model did not emit.

    ``twb_to_pbir._resolve_field`` binds a calculated-field reference optimistically to
    ``_Measures[<caption>]`` without validating it against the emitted model (the field index
    only knows physical columns). So a calc that the model rebuilt as a *column* (a dimension-role
    calc), stubbed, or dropped leaves a **dangling** ``_Measures[X]`` reference -- a "missing field"
    in Power BI. At this seam both halves are in hand, so we deterministically verify every
    projection against the real model: a measure ref must name an emitted measure, a column ref an
    emitted column.

    A dangling measure ref gets one rescue before it is dropped: when the model remodeled that
    parameter-driven measure calc into a Power BI **field parameter** (``swap_specs`` from
    ``report["field_parameters"]["specs"]``), the projection is REBOUND to that field parameter --
    the role gets a seed projection (the parameter's first candidate measure) plus a sibling
    ``fieldParameters`` binding to the parameter's display column, exactly the expansion
    ``twb_to_pbir.field_parameter_table_visual`` emits. The visual then shows the selected measure
    (driven by a slicer on the field parameter) instead of silently losing its Y/Values. This is
    additive: with no ``swap_specs`` (or no matching swap) the reference falls through to the same
    drop path as before, so the no-swap result is byte-identical.

    A projection that is VALID but names a field parameter's own DISPLAY column is EXPANDED here
    for the same reason. A calc dimension the model remodeled into a field parameter resolves
    through ``column_binding`` to ``'<param>'[<param>]``, which is a real column -- so it passes the
    validity check and used to ship as a plain category axis. Power BI then groups by the
    parameter's OPTION LABELS ("Program Name", "Owner", ...) and repeats the same total against
    each: a silently wrong chart, not an error. The fix is the same expansion the measure rescue
    already builds -- a seed projection on the parameter's first candidate field plus a sibling
    ``fieldParameters`` binding -- which is what makes the axis actually swap. SLICERS are excluded:
    a slicer bound to that display column IS the picker (``field_parameter_slicer``), and visuals
    that already carry a ``fieldParameters`` block are skipped as before.

    Anything still unresolved is dropped (warn-never-wrong: drop rather than mis-bind); a visual that
    loses every projection is emptied to a placeholder zone so it never renders broken. Visuals that
    ALREADY encode a field parameter are skipped (a separately validated construct). Returns
    ``(report_parts, drops, rebinds)`` where ``drops`` is
    ``[{"visual", "dropped": [...], "emptied": bool}]`` and ``rebinds`` is
    ``[{"visual", "rebound": [...]}]``.
    """
    measures, columns = _model_object_names(model_parts)
    drops, rebinds = [], []
    if not (measures or columns):
        return report_parts, drops, rebinds  # no model object inventory -> do not risk false drops
    fp_lut = _fp_swap_lookup(swap_specs)
    fp_disp = _fp_display_lookup(swap_specs)
    seed_fn = None
    if fp_lut or fp_disp:  # only reach for the seed builder when a rebind/expansion is possible
        try:
            from . import twb_to_pbir as _tp
        except ImportError:
            try:
                import twb_to_pbir as _tp
            except ImportError:
                _tp = None
        seed_fn = getattr(_tp, "_fp_seed_projection", None) if _tp else None
        dflt_fn = getattr(_tp, "_fp_default_entry", None) if _tp else None
        if not callable(seed_fn):
            fp_lut, fp_disp = {}, {}  # cannot build a valid seed -> disable both, fall back to drop
        if not callable(dflt_fn):     # seed on the Tableau default when available, else branch 0
            dflt_fn = lambda spec: ((spec or {}).get("entries") or [None])[0]
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
        # A slicer bound to a field parameter's display column IS the picker, never an expansion.
        is_slicer = "slicer" in str(vis.get("visualType") or "").lower()
        # Whether this visual ALREADY lacked a required role on the way in (#143). Only a role that
        # THIS pass empties is ours to act on -- a visual that arrived incomplete for some other
        # reason is out of scope, and emptying it here would be an unrelated behaviour change.
        was_missing_required = _missing_required_role(vis, qs)
        dropped, rebound = [], []
        dropped_refs = set()
        for role, spec in list(qs.items()):
            if not isinstance(spec, dict):
                continue
            kept, fp_binds = [], list(spec.get("fieldParameters") or [])
            for p in spec.get("projections", []):
                field = (p or {}).get("field") or {}
                name, kind = _ref_name_kind(field)
                # STRIP as well as lowercase. The model names a measure from the calc caption
                # ``.strip()``-ed, but the report keeps the caption VERBATIM -- so a Tableau field
                # whose name carries a trailing space (authors leave them constantly; Tableau shows
                # no difference) produced ``'Weighted Rank Score '`` in the report against
                # ``'Weighted Rank Score'`` in the model. The names then failed to match, the
                # crosscheck concluded the model never emitted it, and the column was silently
                # dropped from the visual -- a whole column missing from an otherwise correct table,
                # with a correctly-translated measure sitting unused in the model.
                low = name.strip().lower() if isinstance(name, str) else None
                ok = (low in measures if kind == "measure"
                      else low in columns if kind == "column"
                      else True)  # unknown ref shape -> keep (conservative)
                if ok:
                    ent = _ref_entity(field) if (kind == "column" and low and not is_slicer) else None
                    disp = fp_disp.get((ent.lower(), low)) if (ent and seed_fn) else None
                    if disp:  # a field parameter bound as a plain axis -> expand so it can swap
                        fp_binds.append({
                            "parameterExpr": {"Column": {
                                "Expression": {"SourceRef": {"Entity": disp["table_name"]}},
                                "Property": disp["display_col"]}},
                            "index": len(kept), "length": 1})
                        kept.append(seed_fn(dflt_fn(disp)))
                        rebound.append(f"{role}:column {name!r} -> field parameter expansion "
                                       f"{disp['table_name']}[{disp['display_col']}]")
                        continue
                    kept.append(p)
                    continue
                fp = fp_lut.get(low) if (kind == "measure" and low and seed_fn) else None
                if fp:  # model remodeled this measure calc as a field parameter -> rebind, not drop
                    fp_binds.append({
                        "parameterExpr": {"Column": {
                            "Expression": {"SourceRef": {"Entity": fp["table_name"]}},
                            "Property": fp["display_col"]}},
                        "index": len(kept), "length": 1})
                    kept.append(seed_fn(dflt_fn(fp)))
                    rebound.append(f"{role}:measure {name!r} -> field parameter "
                                   f"{fp['table_name']}[{fp['display_col']}]")
                else:
                    dropped.append(f"{role}:{kind or '?'} {name!r}")
                    if p.get("queryRef"):
                        dropped_refs.add(p["queryRef"])
            spec["projections"] = kept
            if fp_binds:
                spec["fieldParameters"] = fp_binds
            if not kept:
                del qs[role]
        if dropped or rebound:
            # A visual that lost EVERY role is emptied, as before. But losing only SOME roles can
            # still leave structurally invalid PBIR: a clusteredColumnChart that keeps `Category`
            # and loses `Y` fails `powerbi-report-author validate` with PBIR_ROLE_REQUIRED_MISSING
            # and renders broken in Desktop (#143). Emptying it is lossier but always valid, which
            # is the reporter's own second option and the only one available here -- the first
            # ("bind the stub measure") cannot apply, because a reference only reaches this branch
            # when the model did NOT emit that object, so there is nothing to bind to.
            emptied = not qs or (was_missing_required is None
                                 and _missing_required_role(vis, qs) is not None)
            if emptied:
                vis.pop("query", None)
            # A formatting object scoped to a projection we just dropped is now a dangling
            # reference: its ``selector.metadata`` names a queryRef the visual no longer projects.
            # Conditional fills are the common case -- a measure whose calc did not translate keeps
            # its per-column backColor rule -- and the leftover rule points at a measure the model
            # does not contain. Pruned by EXACT queryRef match against what was removed, so no
            # object scoped to a surviving projection is ever touched.
            if dropped_refs:
                _strip_objects_for_refs(vis, dropped_refs)
            report_parts[path] = json.dumps(j, indent=2)
            if dropped:
                drops.append({"visual": j.get("name"), "dropped": dropped, "emptied": emptied})
            if rebound:
                rebinds.append({"visual": j.get("name"), "rebound": rebound})
    return report_parts, drops, rebinds


_WRAP_AGG_DAX = {
    0: "SUM",
    1: "AVERAGE",
    2: "DISTINCTCOUNTNOBLANK",
    3: "MIN",
    4: "MAX",
    5: "COUNTA",
    6: "MEDIAN",
}


def _dax_table_ref(name):
    return "'" + str(name or "").replace("'", "''") + "'"


def _dax_bracket_ref(name):
    return "[" + str(name or "").replace("]", "]]") + "]"


def _projection_base_dax(field):
    """The DAX scalar/aggregate a PBIR projection field currently represents, or ``None``.

    This is the producer side of the row-predicate wrapper seam: a Tableau boolean calc filter with
    ``member='true'`` is a ROW keep, so the faithful Power BI form is
    ``CALCULATE(<this base>, FILTER('<table>', <predicate>))``. Only measure/aggregation projections
    are wrapped; categories stay untouched. Fail-closed: an unknown shape returns ``None`` so the
    caller can leave the visual on its standing behavior rather than half-rewrite it.
    """
    node = field if isinstance(field, dict) else {}
    meas = node.get("Measure") if isinstance(node, dict) else None
    if isinstance(meas, dict):
        expr = meas.get("Expression") or {}
        ent = (expr.get("SourceRef") or {}).get("Entity") if isinstance(expr, dict) else None
        prop = meas.get("Property")
        if ent and prop:
            if ent == "_Measures":
                return _dax_bracket_ref(prop)
            return f"{_dax_table_ref(ent)}{_dax_bracket_ref(prop)}"
        return None
    agg = node.get("Aggregation") if isinstance(node, dict) else None
    if isinstance(agg, dict):
        expr = agg.get("Expression") or {}
        col = expr.get("Column") if isinstance(expr, dict) else None
        func = _WRAP_AGG_DAX.get(agg.get("Function"))
        if not (isinstance(col, dict) and func):
            return None
        inner = col.get("Expression") or {}
        ent = (inner.get("SourceRef") or {}).get("Entity") if isinstance(inner, dict) else None
        prop = col.get("Property")
        if ent and prop:
            return f"{func}({_dax_table_ref(ent)}{_dax_bracket_ref(prop)})"
    return None


_TMDL_MEASURE_RE = re.compile(
    r"\n\tmeasure\s+('?)(?P<name>[^'\n=]+?)\1\s*=\s*(?P<body>.*?)"
    r"(?=\n\tmeasure |\n\tcolumn |\n\tpartition |\n\thierarchy |\Z)", re.S)
_TMDL_COLUMN_RE = re.compile(
    r"\n\tcolumn\s+('?)(?P<name>[^'\n=]+?)\1\s*(?:=|\n)(?P<body>.*?)"
    r"(?=\n\tmeasure |\n\tcolumn |\n\tpartition |\n\thierarchy |\Z)", re.S)
_TMDL_TABLE_RE = re.compile(r"^table\s+('?)(?P<name>.+?)\1\s*$", re.M)
_TMDL_FORMAT_RE = re.compile(r"^\s*formatString:\s*(?P<fmt>.+?)\s*$", re.M)


def _declared_format_index(model_parts):
    """Map every model measure and column to the ``formatString`` it declares, if any.

    The row-predicate wrapper replaces a projection with a NEW measure, and a new measure inherits
    nothing -- so without this the author's declared currency / percent / precision is silently
    dropped and Power BI falls back to a general format (an average renders as
    ``28.4285714285714`` where the author asked for ``#,##0``). For an AGGREGATION projection the
    loss is a true regression rather than an omission: ``SUM('T'[Col])`` picks up ``T[Col]``'s
    format automatically, but ``CALCULATE(SUM('T'[Col]), ...)`` bound as a measure does not.

    Returns ``{"measures": {name: fmt}, "columns": {(table, column): fmt}}``, holding only entries
    that actually declare a format so a caller can treat a miss and an absent format alike.
    """
    measures, columns = {}, {}
    for text in (model_parts or {}).values():
        if not isinstance(text, str) or "\n\t" not in text:
            continue
        tm = _TMDL_TABLE_RE.search(text)
        table = tm.group("name").strip() if tm else None
        for m in _TMDL_MEASURE_RE.finditer(text):
            fmt = _TMDL_FORMAT_RE.search(m.group("body"))
            if fmt:
                measures.setdefault(m.group("name").strip(), fmt.group("fmt"))
        if not table:
            continue
        for m in _TMDL_COLUMN_RE.finditer(text):
            fmt = _TMDL_FORMAT_RE.search(m.group("body"))
            if fmt:
                columns.setdefault((table, m.group("name").strip()), fmt.group("fmt"))
    return {"measures": measures, "columns": columns}


def _inherited_format_string(field, index):
    """The ``formatString`` a wrapped projection should keep from what it replaces, else ``None``.

    Mirrors ``_projection_base_dax``'s two accepted shapes so the two can never disagree about what
    a projection stands for: a measure reference inherits that measure's declared format, and an
    aggregation inherits its source COLUMN's. Fail-open by design -- an unknown shape or an
    undeclared format simply yields ``None``, which reproduces the previous output exactly.
    """
    node = field if isinstance(field, dict) else {}
    measures = (index or {}).get("measures") or {}
    columns = (index or {}).get("columns") or {}
    meas = node.get("Measure")
    if isinstance(meas, dict):
        prop = meas.get("Property")
        expr = meas.get("Expression") or {}
        ent = (expr.get("SourceRef") or {}).get("Entity") if isinstance(expr, dict) else None
        if not prop:
            return None
        # A measure lives in _Measures by construction, but a model that carries it on its own
        # table must still resolve -- so try the qualified column key as a fallback, never instead.
        return measures.get(prop) or (columns.get((ent, prop)) if ent else None)
    agg = node.get("Aggregation")
    if isinstance(agg, dict):
        expr = agg.get("Expression") or {}
        col = expr.get("Column") if isinstance(expr, dict) else None
        if not isinstance(col, dict):
            return None
        inner = col.get("Expression") or {}
        ent = (inner.get("SourceRef") or {}).get("Entity") if isinstance(inner, dict) else None
        prop = col.get("Property")
        if ent and prop:
            return columns.get((ent, prop))
    return None


def _append_measure_blocks_to_measures_table(table_tmdl, measure_blocks):
    """Insert additive measure TMDL blocks just before the canonical ``_Measures`` partition."""
    if not (isinstance(table_tmdl, str) and measure_blocks):
        return table_tmdl
    marker = "\tpartition _Measures = calculated\n"
    idx = table_tmdl.find(marker)
    if idx < 0:
        return table_tmdl
    block = "".join(measure_blocks)
    if block and not block.endswith("\n"):
        block += "\n"
    return table_tmdl[:idx] + block + table_tmdl[idx:]


def _strip_flag_measure_filters(filter_config, measure_names):
    """Drop only the flag-measure containers we superseded with wrapped measures."""
    if not (isinstance(filter_config, dict) and measure_names):
        return filter_config, []
    kept, removed = [], []
    for fc in (filter_config.get("filters") or []):
        meas = (((fc.get("field") or {}).get("Measure") or {}).get("Property")
                if isinstance(fc, dict) else None)
        if meas in measure_names:
            removed.append(meas)
            continue
        kept.append(fc)
    if not removed:
        return filter_config, []
    if not kept:
        return None, removed
    out = dict(filter_config)
    out["filters"] = kept
    return out, removed


def _wrapper_measure_name(projection, row_filters, reserved_lower, tokens=None):
    """Reader-facing name for one wrapped projection.

    This name is NOT internal: several visual types (cards, multi-row cards, map tooltips) label a
    field with the MODEL MEASURE NAME rather than the projection's ``nativeQueryRef``, so whatever is
    chosen here is read by a human looking at the report. An opaque content hash surfaced as
    ``Number of Clients (filtered ed813d64)`` -- machine noise on the face of the report -- and
    naming every contributing filter surfaced as a two-line label that crowded the card. The measure
    therefore carries the SHORT, stable suffix ``(filtered)``; the full provenance (which Tableau
    filters narrowed it, and the predicate itself) lives in the measure's TMDL annotations, where an
    author inspecting the model can read it without it intruding on the page. A content hash is
    appended ONLY to break a genuine collision, so the common case stays legible and deterministic.
    """
    seed = (projection.get("nativeQueryRef") or projection.get("queryRef") or "Measure")
    seed = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 _.\-]+", " ", str(seed))).strip(" ._-")
    seed = (seed or "Measure")[:80]
    base = "%s (filtered)" % seed
    if base.lower() not in reserved_lower:
        reserved_lower.add(base.lower())
        return base
    suffix = hashlib.sha1(json.dumps({
        "field": projection.get("field"),
        "row_filters": row_filters,
    }, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    base = "%s (filtered %s)" % (seed, suffix)
    name, i = base, 2
    while name.lower() in reserved_lower:
        name = f"{base} {i}"
        i += 1
    reserved_lower.add(name.lower())
    return name


def _apply_row_predicate_wrapped_measures(report_parts, model_parts, result, res_report):
    """Rewrite affected visuals onto wrapped measures and append those measures to ``_Measures``.

    A Tableau boolean calc filter pinned ``member='true'`` is a ROW-level keep. The earlier
    ``filter_bindings`` seam carried it into PBIR as a visual-level keep-flag measure filter
    (``[Flag] == 1``), but that is evaluated at the VISUAL GROUP grain and is vacuously true on
    ungrouped cards. The faithful shape is a model-side wrapper:

        ``CALCULATE(<existing projection>, FILTER('<table>', <row predicate>))``

    This pass consumes only model-confirmed ``row_filter`` metadata in ``report["filter_bindings"]``
    plus the viz stage's own ``candidate_records`` (worksheet -> visual mapping). It rewrites a
    visual ONLY when every measure/aggregation projection on that visual can be wrapped; otherwise it
    leaves the current build untouched (fail-closed). The superseded flag filter containers are
    removed from that visual's ``filterConfig`` only on success.
    """
    fb = (res_report or {}).get("filter_bindings")
    records = (result or {}).get("candidate_records")
    if not (isinstance(fb, dict) and isinstance(records, list)
            and isinstance(report_parts, dict) and isinstance(model_parts, dict)):
        return report_parts, model_parts, []
    ws_bindings = {}
    for token, spec in fb.items():
        if not isinstance(spec, dict):
            continue
        row_filter = spec.get("row_filter") if isinstance(spec.get("row_filter"), dict) else None
        status = (spec.get("status") or "").lower()
        visuals = list(spec.get("visuals") or [])
        if status not in ("translated", "assisted-approved") or not row_filter or not visuals:
            continue
        table = row_filter.get("table")
        pred = row_filter.get("predicate_dax")
        if not table or not pred:
            continue
        for ws in visuals:
            ws_bindings.setdefault(ws, []).append({
                "token": token,
                "flag_measure": spec.get("measure_name") or spec.get("measure"),
                "table": table,
                "predicate_dax": pred,
            })
    if not ws_bindings:
        return report_parts, model_parts, []
    measures_tmdl = model_parts.get("definition/tables/_Measures.tmdl")
    if not isinstance(measures_tmdl, str):
        return report_parts, model_parts, []

    try:
        from . import tmdl_generate as _tg
    except ImportError:
        try:
            import tmdl_generate as _tg
        except ImportError:
            return report_parts, model_parts, []

    visual_paths = {}
    for path in report_parts:
        if not (isinstance(path, str) and path.endswith("visual.json")):
            continue
        norm = path.replace("\\", "/").split("/")
        if len(norm) >= 2 and norm[-2]:
            visual_paths.setdefault(norm[-2], path)
    if not visual_paths:
        return report_parts, model_parts, []

    existing_measures, _ = _model_object_names(model_parts)
    reserved_lower = set(existing_measures or ())
    out_report = dict(report_parts)
    out_model = dict(model_parts)
    format_index = _declared_format_index(model_parts)
    measure_blocks = []
    wrap_cache = {}
    wrapped = []

    for rec in records:
        if not isinstance(rec, dict):
            continue
        ws_name = rec.get("worksheet")
        specs = ws_bindings.get(ws_name)
        vis_name = rec.get("visual")
        path = visual_paths.get(vis_name)
        if not specs or not vis_name or not path:
            continue
        try:
            j = json.loads(out_report[path])
        except (TypeError, ValueError):
            continue
        vis = j.get("visual") or {}
        qs = ((vis.get("query") or {}).get("queryState")) or {}
        if not qs or any(isinstance(role, dict) and role.get("fieldParameters")
                         for role in qs.values()):
            continue

        row_filters = sorted(
            [{"table": s["table"], "predicate_dax": s["predicate_dax"]} for s in specs],
            key=lambda s: (s["table"], s["predicate_dax"]),
        )
        pending = []
        failed = False
        for role_spec in qs.values():
            if not isinstance(role_spec, dict):
                continue
            for proj in (role_spec.get("projections") or []):
                field = (proj or {}).get("field")
                if not (isinstance(field, dict) and ("Measure" in field or "Aggregation" in field)):
                    continue
                base_dax = _projection_base_dax(field)
                if not base_dax:
                    failed = True
                    break
                key = (
                    json.dumps(field, sort_keys=True),
                    tuple((rf["table"], rf["predicate_dax"]) for rf in row_filters),
                )
                pending.append((proj, key, base_dax))
            if failed:
                break
        if failed or not pending:
            continue

        for proj, key, base_dax in pending:
            wrap = wrap_cache.get(key)
            if wrap is None:
                srcs = sorted({s.get("token") for s in specs if s.get("token")})
                name = _wrapper_measure_name(proj, row_filters, reserved_lower)
                filters = ", ".join(
                    f"FILTER({_dax_table_ref(rf['table'])}, {rf['predicate_dax']})"
                    for rf in row_filters
                )
                dax = f"CALCULATE({base_dax}, {filters})"
                # The Tableau filter(s) this wrapper stands for travel in the measure's OWN
                # provenance annotation rather than in its name: an author inspecting the model can
                # see exactly what narrowed the number, while the page keeps a short, legible label.
                block = _tg.generate_measure_tmdl(
                    name,
                    "filtered by %s" % ", ".join(srcs) if srcs else "",
                    dax,
                    translated_by="deterministic (row-predicate visual wrapper)",
                    format_string=_inherited_format_string(proj.get("field"), format_index))
                wrap = {"name": name, "block": block, "dax": dax}
                wrap_cache[key] = wrap
                measure_blocks.append(block)
            proj["field"] = {
                "Measure": {
                    "Expression": {"SourceRef": {"Entity": "_Measures"}},
                    "Property": wrap["name"],
                }
            }

        new_fc, removed = _strip_flag_measure_filters(
            j.get("filterConfig"),
            {s.get("flag_measure") for s in specs if s.get("flag_measure")},
        )
        if removed:
            if new_fc is None:
                j.pop("filterConfig", None)
            else:
                j["filterConfig"] = new_fc
        out_report[path] = json.dumps(j, indent=2)
        wrapped.append({
            "visual": vis_name,
            "worksheet": ws_name,
            "wrapped": len(pending),
            "removed_flag_filters": sorted(set(removed)),
        })

    if not measure_blocks:
        return report_parts, model_parts, []
    out_model["definition/tables/_Measures.tmdl"] = _append_measure_blocks_to_measures_table(
        measures_tmdl, measure_blocks)
    return out_report, out_model, wrapped


def _date_binding_from_model(res_report):
    """Derive the report binder's ``date_binding`` from the model build's date-table report.

    Purely a CONSUMER of facts the datasource-migration build already produced (it never re-detects
    dates): the marked Date table name and which fact date column the calendar relates to ACTIVELY
    (``assemble_model._select_primary_date`` refuses to guess when ambiguous, so ``active`` is empty
    then). Returns ``None`` when there is no usable marked Date table or no active date -- the report
    then keeps binding date axes to the source column (warn-never-wrong). ``grain_columns`` is left
    to the binder's standard calendar-column default, so the contract stays minimal.

    **Per-island models** (several datasources -> one calendar each) report ``islands`` instead of a
    single ``table``, so this used to fall straight through the ``dr.get("table")`` gate and return
    ``None`` -- disabling date binding for the whole workbook. Measured cost of that: 0073 went 6 -> 0
    calendar-bound refs, 0088 5 -> 0, 0079 1 -> 0; the axes still rendered correct values on the
    fact's own column, but lost the calendar hierarchy entirely. Those models now emit ``by_island``:
    the datasource CAPTION -> that island's calendar + keys. A resolved report field carries its own
    ``datasource`` (the same caption), so the binder can send each pill to its own island's calendar.
    """
    dr = (res_report or {}).get("date_table") or {}
    if dr.get("generated") and dr.get("per_island") and dr.get("islands"):
        return _per_island_date_binding(dr)
    if not (dr.get("generated") and dr.get("mark_as_date") and dr.get("table")):
        return None
    return _single_calendar_date_binding(dr)


def _per_island_date_binding(dr):
    """``date_binding`` for a model with one calendar per datasource island.

    Keyed on the ISLAND (datasource caption), because that is the one identifier both sides share: a
    resolved report field carries ``datasource``, and the model build tags each relation with
    ``source_datasource``.

    Keying on the workbook RELATION NAME instead was tried and is wrong -- measured, not reasoned:
    ``pmdm__ProgramEngagement__c`` exists in all four Salesforce islands, so the key collides, and
    resolving the collision "first wins" bound an **Intake** pill to the **Service Delivery**
    calendar. That is a cross-island rebind onto a calendar with no active join to the fact: every
    bucket returns the grand total, which is precisely the flat series this split exists to remove.

    The flat single-calendar keys are kept for the first island so a consumer reading ``date_table``
    still sees something coherent, but the binder prefers ``by_island`` and DECLINES when a pill's
    datasource is unknown -- binding it to an arbitrary island would reintroduce the same defect.
    """
    by_island = {}
    first = None
    for isl in dr.get("islands") or []:
        if not (isl.get("mark_as_date") and isl.get("table")):
            continue
        one = _single_calendar_date_binding(isl)
        if one is None:
            continue
        if first is None:
            first = one
        key = (isl.get("island") or "").strip().lower()
        if key:
            by_island[key] = one
    if first is None:
        return None
    out = dict(first)
    out["by_island"] = by_island
    return out


def _single_calendar_date_binding(dr):
    """The original single-calendar derivation, unchanged, reused per island."""
    rels = [r for r in (dr.get("relationships") or []) if r.get("column")]
    active = [r["column"] for r in rels if r.get("active")]
    if not active:
        return None
    # ``ambiguous_keys``: date columns that are ACTIVE on one table and NOT active on another table
    # that also carries them. The binder identifies a date pill by column name (the field's entity is
    # still the workbook's relation name at that point, not the model's table), so for these it
    # cannot tell "the fact that owns the active relationship" from "a different fact with the same
    # column name". Rebinding the wrong one onto the calendar produces a flat time series, so they
    # are named here and declined there. Additive: absent when no name is contested.
    #
    # The contested population is EVERY table carrying the column, not just the tables that received
    # a date relationship. A table the calendar SKIPPED -- a pure dimension, or one that never landed
    # -- has no relationship at all, so it never appears in ``rels``; keying the guard on ``rels``
    # alone therefore left invisible exactly the case the guard exists to catch, because "no
    # relationship" is a stronger version of "inactive relationship", not an exemption from it.
    # ``unrelated_date_columns`` (from ``assemble_model._build_date_dimension``) supplies them.
    # Measured on Salesforce NPSP: ``caseman__Intake__c`` is a pure dim carrying ``CreatedDate``,
    # which is active on three other tables, and its Month axis rebound to ``Date[Month Start]`` on a
    # table the calendar cannot filter -- one visual rendering the grand total in every bucket.
    act_names = {(c or "").strip().lower() for c in active}
    inact_names = {(r["column"] or "").strip().lower()
                   for r in rels if not r.get("active")}
    inact_names |= {(u.get("column") or "").strip().lower()
                    for u in (dr.get("unrelated_date_columns") or ())
                    if isinstance(u, dict) and u.get("column")}
    contested = act_names & inact_names
    ambiguous = sorted(
        {r["column"] for r in rels
         if (r["column"] or "").strip().lower() in contested})
    out = {"date_table": dr["table"], "active_keys": active, "key_column": "Date"}
    if ambiguous:
        out["ambiguous_keys"] = ambiguous
    return out


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


def _parse_tmdl_columns(content):
    """Parse a TMDL table part into ``(table_name, [(column_name, is_calc), ...])``.

    ``is_calc`` marks a column materialised from a Tableau CALCULATED FIELD (a dimension calc), as
    opposed to a raw ``sourceColumn`` passthrough or a model-generated calendar column. Two shapes
    qualify (mirroring how the datasource build emits calc columns):
      * a DAX calculated column (``column X = <expr>``) that ALSO carries an
        ``annotation TableauFormula`` -- the stamp the build puts on every translated Tableau calc.
        Requiring that annotation EXCLUDES model-generated Date/calendar calc columns (``Year =
        YEAR(...)`` etc., which carry no TableauFormula) while INCLUDING real Tableau calc dimensions.
      * a VISIBLE field-parameter / picker column in a ``= calculated`` partition whose
        ``sourceColumn`` is a ``[Value...]`` slot (e.g. a ``Choose Date`` date picker); its hidden
        helper columns (Fields/Order) are excluded by the ``not hidden`` guard.
    Returns ``("", [])`` for a part that declares no table (relationships/model/expressions/culture).
    Pure text parse; never raises.
    """
    if not isinstance(content, str) or not content:
        return "", []
    tm = re.search(r"(?m)^[^\S\n]*table[^\S\n]+(?:'([^']+)'|(\S+))", content)
    table = (tm.group(1) or tm.group(2)) if tm else ""
    if not table:
        return "", []
    calc_partition = bool(re.search(r"(?m)^[^\S\n]*partition\b.*=[^\S\n]*calculated\b", content))
    col_re = re.compile(r"(?m)^[^\S\n]*column[^\S\n]+(?:'([^']+)'|([^\s=]+))([^\S\n]*=)?")
    matches = list(col_re.finditer(content))
    cols = []
    for i, mm in enumerate(matches):
        cname = mm.group(1) or mm.group(2)
        has_expr = bool(mm.group(3))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        block = content[mm.start():end]
        hidden = bool(re.search(r"(?m)^[^\S\n]*isHidden\b", block))
        tabformula = "annotation TableauFormula" in block
        value_src = bool(re.search(r"(?m)^[^\S\n]*sourceColumn:[^\S\n]*\[?Value", block))
        is_calc = (has_expr and tabformula) or (calc_partition and value_src and not hidden)
        if cname:
            cols.append((cname, is_calc))
    return table, cols


def _column_binding_from_model(model_parts):
    """Derive the report binder's ``column_binding`` from the BUILT model's TMDL parts.

    Pure CONSUMER of the model the datasource build just emitted (``res["parts"]``): it reads every
    table part, finds the columns materialised from Tableau CALCULATED FIELDS that are DIMENSIONS
    (see :func:`_parse_tmdl_columns`), and shapes them into the ``{"columns": {name_lower: {"table",
    "column"}}}`` manifest ``twb_to_pbir._lookup_column_binding`` reads. So a calc DIMENSION pill on
    a crosstab axis binds to the REAL model table+column (e.g. ``Sheet1[Director]``, ``'Choose
    Date'[Choose Date]``) instead of the datasource-caption fallback -- which is what keeps a
    calc-dimension crosstab a matrix bound to real fields, not an empty/mis-bound one.

    A calc name that resolves to more than one ``(table, column)`` is AMBIGUOUS and skipped (warn-
    never-wrong: better the caption fallback than a wrong-table bind). Returns ``None`` when the model
    materialised no such calc column, so the report keeps its standing resolution (byte-unchanged).
    """
    if not isinstance(model_parts, dict) or not model_parts:
        return None
    targets = {}
    for path, content in model_parts.items():
        p = str(path).replace("\\", "/")
        if not (isinstance(content, str) and p.endswith(".tmdl")):
            continue
        table, cols = _parse_tmdl_columns(content)
        if not table:
            continue
        for cname, is_calc in cols:
            if is_calc and cname:
                targets.setdefault(cname.lower(), set()).add((table, cname))
    columns = {}
    for low, tset in targets.items():
        if len(tset) == 1:
            tbl, col = next(iter(tset))
            columns[low] = {"table": tbl, "column": col}
    return {"columns": columns} if columns else None


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
            break
    else:
        shaped = None

    # COLUMN targets, for the row-level constant the model landed as a calculated column of 1s
    # (``summarizeBy: sum``) rather than a COUNTROWS measure. Aggregating that column on the shelf
    # reproduces EVERY Tableau aggregation exactly (SUM -> n*k, AVG -> k, CNT -> n), which one
    # COUNTROWS measure cannot; without this the implicit-row-count channel finds no measure, warns
    # "left unbound", and the pill is DROPPED -- which took a matrix visual's last binding with it
    # and emitted a page-less report. Purely additive: measure targets keep absolute priority, so a
    # model that supplies COUNTROWS binds byte-identically to before.
    rcc = (rr.get("model_manifest") or {}).get("row_count_columns") or rr.get("row_count_columns")
    if isinstance(rcc, dict) and rcc:
        cols = {}
        for name, c in (rcc.get("columns") or {}).items():
            if isinstance(c, dict) and c.get("entity") and c.get("column"):
                cols[name] = {"entity": c["entity"], "column": c["column"]}
        dflt = rcc.get("default_column")
        if cols or isinstance(dflt, dict):
            shaped = dict(shaped or {})
            if cols:
                shaped["columns"] = cols
            if isinstance(dflt, dict) and dflt.get("entity") and dflt.get("column"):
                shaped["default_column"] = {"entity": dflt["entity"], "column": dflt["column"]}
    return shaped or None


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
    "flags": {<tableau filter token>: {"entity", "measure", "status", "value"}},
    "values": {<param id>: {"table", "measure", "caption"}}}`` or ``None`` when the model exposed
    nothing bindable (so the report keeps its precise warnings, byte-unchanged). ``values`` names
    each what-if parameter's ``SELECTEDVALUE`` measure, which is what a consumer needs when it
    COMPARES against a parameter instead of slicing on it.

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
        out = {"table": table, "column": column, "single_select": single}
        # Optional: a picker whose SELECTION lands on a different column than the one projected
        # (a field parameter is projected on its display column but selected through its hidden
        # group-by column). Carried verbatim; absent for every other picker shape.
        sel = spec.get("select")
        if isinstance(sel, dict) and sel.get("column") and sel.get("value") is not None:
            out["select"] = {"column": sel["column"], "value": sel["value"]}
        return out

    direct = rr.get("param_binding")
    if isinstance(direct, dict) and (direct.get("slicers") or direct.get("flags")
                                     or direct.get("values")):
        return {"slicers": dict(direct.get("slicers") or {}),
                "flags": dict(direct.get("flags") or {}),
                "values": dict(direct.get("values") or {})}

    manifest = rr.get("model_manifest") or {}
    slicers, flags, values = {}, {}, {}

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
        # Additive, and independent of the slicer: a what-if parameter's SCALAR reader. A consumer
        # that compares against a parameter (rather than slicing on it) needs this and cannot use
        # the picker column. Keyed the same way as ``slicers`` so both sides look up alike.
        val = p.get("value")
        if pid and isinstance(val, dict) and val.get("table") and val.get("measure"):
            values[pid] = {"table": val["table"], "measure": val["measure"],
                           "caption": caption}

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

    if not slicers and not flags and not values:
        return None
    return {"slicers": slicers, "flags": flags, "values": values}


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
    # Which SECONDARIES are themselves published (#174). ``is_published`` above is deliberately
    # about the PRIMARY -- that is what ``kind`` describes -- but a workbook can have an EMBEDDED
    # primary and a PUBLISHED secondary, and that shape used to be invisible: ``secondary_datasources``
    # carried bare labels, so nothing downstream (or reading the handover) could tell a published
    # dependency from an ordinary one. The workbook then built with an empty ``sqlproxy`` proxy table
    # pointed at ``localhost``, while sibling workbooks whose PRIMARY was published gated honestly.
    # Recorded as its own key rather than by widening ``is_published``: widening it would relabel the
    # workbook's ``kind`` as published, which is false -- its primary is embedded, and every consumer
    # that reads ``kind`` to decide rebind-vs-rebuild would be told the wrong thing to fix a
    # reporting gap.
    secondary_published = [
        s.get("label") or s.get("caption") or s.get("name")
        for s in secondaries
        if (s.get("connection_class") or "").strip().lower() == "sqlproxy"
    ]
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
        "secondary_published_datasources": secondary_published,
        "view_local_calcs": view_local_calcs,
        "recommendation": recommendation,
        "note": note,
    }


_BLEND_INSTANCE_RE = re.compile(r"^\[(?P<ds>[^\]]+)\]\.\[(?P<inst>[^\]]+)\]$")


def _blend_field_caption(instance):
    """The base field caption inside a Tableau column-instance token.

    ``[none:Category:nk]`` -> ``Category``; ``[mn:Order Date:ok]`` -> ``Order Date``. A token that is
    not in the ``<derivation>:<field>:<kind>`` shape is returned as-is (minus brackets), so a plain
    field reference still names its field.
    """
    tok = _strip_brackets(str(instance or "").strip())
    parts = tok.split(":")
    if len(parts) >= 3:
        return ":".join(parts[1:-1]).strip()
    return tok


def _workbook_blend_links(twb_text):
    """Tableau's DECLARED cross-datasource blend links -> ``[{source, target, pairs}]``.

    A Tableau data BLEND is not a join inside either datasource -- it is a workbook-level link, and
    Tableau records it explicitly in ``<datasource-relationships>``::

        <datasource-relationship source='federated.10nn...' target='federated.0hgp...'>
          <column-mapping>
            <map key='[federated.10nn...].[none:Category:nk]'
                 value='[federated.0hgp...].[none:Category:nk]' />

    Nothing read this block, so a blended secondary datasource landed in the model related to
    NOTHING but the Date dimension, and any visual slicing it returned the whole table's total
    identically for every member -- measured on Superstore at 4.4x high and constant, while the
    fact's own measures on the very same rows matched Tableau exactly (issue #101). Because the
    links are declared, the join keys are GROUND TRUTH rather than a name-match heuristic.

    ``pairs`` is ``[(source field caption, target field caption)]`` de-duplicated in document order;
    Tableau writes one ``<map>`` per DERIVATION of the same field (``mn:`` / ``yr:`` / ``tmn:`` of one
    date), which are all the same underlying link. Returns ``[]`` for a workbook with no blend, or
    one that will not parse.
    """
    try:
        root = ET.fromstring((twb_text or "").lstrip("\ufeff"))
    except Exception:
        return []
    out = []
    for rel in root.iter():
        if _local(rel.tag) != "datasource-relationship":
            continue
        src, tgt = (rel.get("source") or "").strip(), (rel.get("target") or "").strip()
        if not src or not tgt:
            continue
        pairs = []
        for mp in rel.iter():
            if _local(mp.tag) != "map":
                continue
            km = _BLEND_INSTANCE_RE.match((mp.get("key") or "").strip())
            vm = _BLEND_INSTANCE_RE.match((mp.get("value") or "").strip())
            if not km or not vm:
                continue
            pair = (_blend_field_caption(km.group("inst")), _blend_field_caption(vm.group("inst")))
            if all(pair) and pair not in pairs:
                pairs.append(pair)
        if pairs:
            out.append({"source": src, "target": tgt, "pairs": pairs})
    return out


def _workbook_datasource_captions(twb_text):
    """``{internal datasource name -> caption}`` for every datasource the workbook declares.

    A blend link names its two sides by INTERNAL name (``federated.0hgpf...``), while the model's
    ``table_map`` is keyed by datasource CAPTION -- so the two only meet through this map.
    """
    try:
        root = ET.fromstring((twb_text or "").lstrip("\ufeff"))
    except Exception:
        return {}
    out = {}
    for el in root.iter():
        if _local(el.tag) != "datasource":
            continue
        name, caption = (el.get("name") or "").strip(), (el.get("caption") or "").strip()
        if name and caption:
            out.setdefault(name, caption)
    return out


def _blend_link_warnings(twb_text, res_report):
    """Warn for each DECLARED blend whose two sides landed as UNRELATED model tables.

    A blend is Tableau's answer to "these live in different datasources"; Power BI's answer is a
    relationship, and one cannot be invented safely -- Tableau blends on a COMPOSITE key at a chosen
    date grain, which has no single-column equivalent, and a wrong relationship returns a number that
    renders perfectly. So this reports the link with the exact columns Tableau declared, which is a
    precise instruction rather than a name-match guess: the operator knows which two tables and which
    keys, and can add the relationship (or a composite key column) deliberately.

    Each side is resolved through its OWN DATASOURCE (via ``table_map``), never through the bare
    caption: ``naming`` is first-writer-wins on a caption, so both sides of a blend on ``Category``
    resolve to whichever datasource was written first and the link looks like a self-join. Silent
    when the two sides share a landed table, or when the workbook declares no blend.
    """
    links = _workbook_blend_links(twb_text)
    if not links:
        return []
    captions = _workbook_datasource_captions(twb_text)
    tables_by_ds = {}
    for key, consolidated in ((res_report or {}).get("table_map") or {}).items():
        ds, _, _rel = str(key).partition("||")
        if ds.strip() and consolidated:
            tables_by_ds.setdefault(ds.strip(), set()).add(consolidated)

    warns = []
    for link in links:
        src_tables = tables_by_ds.get(captions.get(link["source"], ""), set())
        tgt_tables = tables_by_ds.get(captions.get(link["target"], ""), set())
        if not src_tables or not tgt_tables or (src_tables & tgt_tables):
            continue        # unresolved, or already one table -- nothing to relate
        keys = [src if src == tgt else f"{src} <-> {tgt}" for src, tgt in link["pairs"]]
        warns.append(
            _PBIP_WARN + ("Tableau BLENDS %r with %r on %s, but the tables they landed as (%s | %s) "
                          "have no relationship between them -- a measure from one sliced by the "
                          "other's columns returns that table's GRAND TOTAL identically for every "
                          "member. A blend is a composite-key link with no single-column Power BI "
                          "equivalent, so it is reported rather than guessed: add a relationship (or "
                          "a COMBINEVALUES key column on both tables) using exactly these columns"
                          % (captions.get(link["source"], link["source"]),
                             captions.get(link["target"], link["target"]),
                             ", ".join(repr(k) for k in keys),
                             ", ".join(sorted(src_tables)), ", ".join(sorted(tgt_tables)))))
    return warns


# Tableau's own cross-project disambiguation suffix on a published-datasource caption:
# ``DS_Tail_Level | Project : Enterprise Dashboards``. It is metadata about WHERE the datasource
# lives, not part of its name, and the server appends it only when the name would otherwise be
# ambiguous across projects (#145). Stripped before the alphanumeric squeeze because that squeeze
# removes the ``|`` and ``:`` that identify it. Tolerant of spacing, anchored to the END so a name
# that merely contains the word "project" is untouched.
_PROJECT_SUFFIX_RE = re.compile(r"\s*\|\s*project\s*:\s*.*$", re.IGNORECASE)


def _norm_ds(name):
    """Connector-agnostic match key: lowercased with all non-alphanumerics removed, so a workbook's
    published-datasource name ('Superstore - Extract') matches the migrated datasource it became
    ('Superstore-Extract.tds' -> 'Superstore_Extract').

    Tableau's cross-project suffix is dropped first (#145). It does not survive the alphanumeric
    squeeze as punctuation -- it becomes WORDS -- so ``DS_Tail_Level`` keyed ``dstaillevel`` while
    ``DS_Tail_Level | Project : Enterprise Dashboards`` keyed
    ``dstaillevelprojectenterprisedashboards``, and the two could never be equal. Measured in the
    field: 4 of 12 workbooks in one estate build were skipped as "co-migrate its published
    datasource" while the datasource each needed had migrated successfully in that same run.

    Applied INSIDE this function so both sides of every comparison are normalised identically --
    stripping only at the lookup site would reintroduce the asymmetry that #138 was, one layer up.

    Safe against the ambiguity it exists to encode: two same-named datasources in different projects
    now collapse to one key, and the catalog already fails closed on that
    (:data:`_AMBIGUOUS_CATALOG_ENTRY`), so the workbook is skipped with an honest reason rather than
    bound to whichever migrated last.
    """
    return re.sub(r"[^a-z0-9]", "", _PROJECT_SUFFIX_RE.sub("", (name or "")).lower())


# A catalog key two different datasources both answer to. Recorded rather than resolved: binding a
# workbook to whichever one migrated last would attach a model with the wrong schema, and it would
# render perfectly. The lookup treats this exactly like a miss.
_AMBIGUOUS_CATALOG_ENTRY = {"__ambiguous__": True}


def _datasource_catalog_aliases(name, text):
    """Every name a migrated datasource can legitimately be looked up by.

    A published datasource is referenced from a workbook by its DISPLAY NAME on the server, which is
    the ``.tds``'s own ``caption`` -- but the file it is exported to is usually named for the content
    instead, so the file stem and the caption routinely differ. Indexing both (plus the internal
    ``formatted-name``, which is what the workbook's ``<datasource name=...>`` carries) is what lets
    the join happen at all; keying only the stem is why it did not.

    Best-effort and never raises: a ``.tds`` that will not parse contributes only its file name.
    """
    aliases = [name]
    try:
        root = ET.fromstring((text or "").lstrip("\ufeff"))
    except Exception:
        return [a for a in aliases if a]
    els = [root] if _local(root.tag) == "datasource" else []
    els += [d for d in root.iter() if _local(d.tag) == "datasource"]
    for el in els[:8]:
        for attr in ("caption", "formatted-name", "name"):
            val = (el.get(attr) or "").strip()
            if val and val not in aliases:
                aliases.append(val)
    return [a for a in aliases if a]


def _rebuild_from_published_match(detail, twb_text, model_safe, ds_catalog, approved_calc_dax=None):
    """Rebuild a published-datasource workbook's model from the matching ALREADY-MIGRATED published
    datasource (its real schema) instead of the workbook's own unusable ``sqlproxy`` proxy stub --
    carrying BOTH the workbook's own calculated fields AND the published datasource's own calculated
    fields so the attached model holds every calculation either side defines (workbook-local calcs win
    on a name clash). Returns a ``migrate_datasource`` result bound to the real schema, or ``None``
    when there is no faithful name match (the caller then keeps the honest skip). Never raises.
    """
    if not ds_catalog:
        return None
    sig = detail.get("binding_signal") or {}
    if sig.get("kind") != "published":
        return None
    match = ds_catalog.get(_norm_ds(sig.get("published_ds_name")))
    if not match or match.get("__ambiguous__"):
        # A name two migrated datasources both answer to identifies neither. Binding to whichever
        # migrated last would attach a model with the WRONG schema, and it would render perfectly --
        # so an ambiguous name keeps the honest skip.
        return None
    try:
        wb_calcs, _skipped, wb_dim_calcs = extract_calculations(twb_text, include_dimensions=True)
    except Exception:
        wb_calcs, wb_dim_calcs = None, None
    # Union the PUBLISHED datasource's OWN calculated fields (from its real ``.tds``) with the
    # workbook's. A workbook caches only the calcs it actually places on a shelf, so a published
    # calc the workbook never referenced would otherwise be dropped from the rebuilt model. Pulling
    # the ``.tds``'s own calcs guarantees the model attached to a published-datasource workbook
    # carries BOTH the datasource's and the workbook's calculations -- by construction, not
    # contingent on Tableau's cache. Workbook-local calcs WIN on a caption clash (they are this
    # workbook's authored intent). Fail-closed: a parse hiccup leaves the workbook-only calcs
    # exactly as before. ``match["text"]`` is the published ``.tds`` XML text (same value already
    # passed to ``migrate_datasource`` below), so ``extract_calcs`` parses it directly.
    try:
        own_calcs = extract_calcs(match["text"])
    except Exception:
        own_calcs = []
    if own_calcs:
        wb_calcs = list(wb_calcs or [])
        wb_dim_calcs = list(wb_dim_calcs or [])
        have = {(c.get("name") or "").strip().lower()
                for c in (*wb_calcs, *wb_dim_calcs) if c.get("name")}
        for c in own_calcs:
            nm = (c.get("name") or "").strip().lower()
            if not nm or nm in have:
                continue
            entry = {"name": c["name"], "formula": c["formula"]}
            if c.get("internal_name"):
                entry["internal_name"] = c["internal_name"]
            if (c.get("role") or "measure").strip().lower() == "dimension":
                entry["role"] = "dimension"
                wb_dim_calcs.append(entry)
            else:
                wb_calcs.append(entry)
            have.add(nm)
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
    """Build ``(model_table, field_map, ambiguous)`` for the viz re-run from the model build's
    authoritative naming map, so a published-datasource workbook's column pills bind to the REAL
    migrated tables (``Orders``/``Date``) instead of the workbook's own unusable ``sqlproxy`` proxy
    entity.

    ``field_map`` keys VERBATIM on each column's Tableau field caption / remote name (the same
    ``model_manifest['naming']`` join convention the model->viz contract guarantees never dangles)
    and carries only ``{entity, property}`` -- never ``binding`` -- so an aggregation pill
    (``SUM([Sales])``) keeps its aggregation while its entity is corrected to the fact table.
    ``model_table`` is the fact table (the one owning the most columns) and acts as the fallback for
    any column pill not present in the map. Measures are intentionally EXCLUDED here -- the
    token-keyed ``measure_binding`` already rebinds them onto ``_Measures``. Returns
    ``(None, None, [])`` when no naming map is available (the re-run then keeps its standing field
    bindings).

    DATASOURCE-QUALIFIED KEYS. A bare caption is not a unique name when a workbook consolidates
    SEVERAL embedded datasources into one model. Tableau duplicates its datasource per dashboard, so
    the same physical table arrives many times; the model keeps one copy per datasource, suffixing
    the later ones (``pmdm__Program__c`` + ``pmdm__Program__c (Intake)``). ``naming`` is built with
    ``setdefault`` on the bare ref, so the FIRST datasource claims every shared caption and the rest
    never enter the map -- every worksheet in every other datasource then binds to a table that has
    no relationship to its own facts. Measured on the Salesforce Nonprofit workbook (4 datasources:
    Service Delivery / Intake / Client Enrollment and participation / Assessments): the Intake
    dashboard's Program and Owner slicers filtered NOTHING and grouping by Owner returned the grand
    total on every row, because they bound Service Delivery's ``pmdm__Program__c`` / ``User`` while
    the relationships run to ``pmdm__Program__c (Intake)`` and ``Case`` has no path to ``User`` at
    all. Only tables whose name happens to be unique across all four (``Case``) bound correctly.

    So this ALSO emits ``"<datasource>||<relation>||<caption>"`` keys -- qualified by the pill's own
    Tableau RELATION as well as its datasource, because a bare caption is not even unique WITHIN one
    datasource (a Salesforce model has a ``Name`` column on Program, User, Contact and Case alike).
    They are built from ``model_manifest['columns']`` (a LIST -- every datasource's columns survive
    there, unlike ``naming``) joined to ``table_map`` (``"<datasource>||<relation>" -> <consolidated
    table>``), which is exactly the mapping :func:`assemble_model.resolve_consolidated_column` uses.
    The bare keys are kept untouched as the fallback, so a single-datasource workbook resolves
    exactly as before.
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
        return None, None, []
    fact_table = max(counts, key=counts.get)

    # A consolidated table may be reached from more than one (datasource, relation) pair -- identical
    # tables are de-duplicated -- so every owning pair gets its own qualified key.
    pairs_by_table = {}
    for key, consolidated in ((res_report or {}).get("table_map") or {}).items():
        ds, _, relation = str(key).partition("||")
        if ds.strip() and relation.strip() and consolidated:
            pairs_by_table.setdefault(consolidated, []).append((ds.strip(), relation.strip()))
    ds_scoped = {}      # "<ds>||<caption>" -> [ {entity, property}, ... ]  (ambiguity detector)
    for col in manifest.get("columns") or []:
        model_table = col.get("model_table")
        model_name = col.get("model_name")
        if not model_table or not model_name:
            continue
        target = {"entity": model_table, "property": model_name}
        for ds, relation in pairs_by_table.get(model_table, ()):
            for ref in (col.get("tableau_field"), col.get("source_column")):
                if ref:
                    field_map.setdefault("%s||%s||%s" % (ds, relation, ref), target)
                    ds_scoped.setdefault("%s||%s" % (ds, ref), []).append(target)
    # DATASOURCE-SCOPED fallback, for when the relation name does not match. An EXTRACTED datasource
    # carries TWO relations for the same logical table -- the live one (``Sales Commission.csv``) and
    # the extract materialisation (``Extract``) -- and the model keys the live name while a worksheet
    # bound to the extract carries ``Extract``. The relation-qualified key then misses and resolution
    # fell through to the BARE caption, which in a multi-table model is claimed by whichever table
    # happened to be written first. Measured on Superstore (issue #103): the Commission dashboard's
    # ``Sales`` bound ``Orders[Sales]`` (2,326,534) instead of ``Sales Commission.csv[Sales]``
    # (15,357,898) -- a 6.6x error that renders perfectly, on a page where every sibling projection
    # used the right table.
    #
    # Only recorded where the caption is UNAMBIGUOUS within its own datasource. Where a datasource
    # genuinely has the same column name on two tables there is nothing to prefer, so the key is
    # withheld rather than guessed, and the caller falls through to the bare caption exactly as
    # before. Those captions are reported (see ``ambiguous_by_name``) so a silent guess is visible.
    ambiguous = []
    for key, targets in ds_scoped.items():
        distinct = {(t["entity"], t["property"]) for t in targets}
        if len(distinct) == 1:
            field_map.setdefault(key, targets[0])
        else:
            ambiguous.append(key)
    return fact_table, field_map, sorted(ambiguous)


# -- Windows MAX_PATH guard for the openable .pbip write ----------------------------------------
# A PBIR report nests deeply (``.Report/definition/pages/<page>/visuals/<visual>/visual.json``), so a
# long output root can push a file path past the Windows MAX_PATH (260) limit -- where the OS raises a
# cryptic ``WinError 3`` mid-write and the project lands half-written. We PROJECT the longest path
# ``write_local_pbip`` will create and fail fast with an actionable message BEFORE writing, and we
# CLASSIFY a write-time ``OSError`` as a path-length cause so a real failure is reported LOUD (failed),
# never masked as a benign skip. (The ``\\?\`` long-path writer that would REMOVE the limit outright is
# deliberately out of scope for this change -- the writer stays untouched here.)
MAX_PATH = 260  # Windows limit incl. the terminating null -> a usable path length is 259 chars.


def _projected_pbip_paths(dest, model_name, parts, report_name, report_parts):
    """Yield every absolute file path :func:`write_local_pbip` will create under ``dest``.

    Mirrors that writer's layout exactly: model parts land under
    ``<dest>/<model_name>.SemanticModel/<rel>``, report parts under ``<dest>/<report_name>.Report/<rel>``,
    plus the ``<dest>/<report_name>.pbip`` pointer (the workbook call site passes ``project_name`` ==
    ``report_name``). Read-only; the writer itself is not touched.
    """
    root = os.path.abspath(dest)
    model_dir = os.path.join(root, model_name + ".SemanticModel")
    report_dir = os.path.join(root, report_name + ".Report")
    for rel in (parts or {}):
        yield os.path.join(model_dir, rel.replace("/", os.sep))
    for rel in (report_parts or {}):
        yield os.path.join(report_dir, rel.replace("/", os.sep))
    yield os.path.join(root, report_name + ".pbip")


def _longest_projected_path(dest, model_name, parts, report_name, report_parts):
    """The single longest projected ``.pbip`` file path (the MAX_PATH budget proxy). Never raises."""
    longest = os.path.abspath(dest)
    for p in _projected_pbip_paths(dest, model_name, parts, report_name, report_parts):
        if len(p) > len(longest):
            longest = p
    return longest


def _classify_pbip_write_error(exc=None, projected=None):
    """Classify a ``.pbip`` write failure as ``"path_too_long"`` or ``"write_error"``. Read-only.

    A projected path at/over the Windows MAX_PATH budget, or a Windows ``WinError`` 206
    (ERROR_FILENAME_EXCED_RANGE) / 3 (ERROR_PATH_NOT_FOUND -- the symptom of a too-long path once the
    parent dirs already exist), or a POSIX ``ENAMETOOLONG`` -> path-length. Anything else -> generic.
    """
    if projected is not None and len(projected) >= MAX_PATH:
        return "path_too_long"
    if getattr(exc, "winerror", None) in (3, 206):
        return "path_too_long"
    import errno as _errno
    if getattr(exc, "errno", None) == getattr(_errno, "ENAMETOOLONG", 36):
        return "path_too_long"
    return "write_error"


def _record_pbip_write_failure(entry, warns, *, cause, dest, projected=None, exc=None):
    """Mark ``entry`` a LOUD ``.pbip`` write failure and record why (additive ``pbip_write_error``).

    A failed write must never masquerade as a benign skip. The definition-of-done reads
    ``pbip_write_error`` and reports the workbook FAILED *before* the published-datasource carve-out (so
    a MAX_PATH failure on a published-DS workbook is not mis-reported as "published DS not in scope").
    The actionable message is built once and stored so the DoD banner + ``summary.md`` surface the exact
    cause and remedy. Additive: ``pbip_write_error`` is a new key; the ``pbip_warnings`` note is kept.
    """
    if cause == "path_too_long":
        loc = f" ({len(projected)} chars: {projected})" if projected else ""
        message = ("workbook .pbip output path exceeds the Windows MAX_PATH (260) limit" + loc +
                   " -- re-run with a shorter output root (e.g. -o C:\\tfmig) or enable Windows long "
                   "paths")
    elif exc is not None:
        message = f"workbook .pbip write failed ({exc})"
    else:
        message = "workbook .pbip write failed"
    err = {"cause": cause, "message": message, "path": os.path.abspath(dest)}
    if projected is not None:
        err["projected_path"] = projected
        err["projected_length"] = len(projected)
    winerr = getattr(exc, "winerror", None)
    if winerr is not None:
        err["winerror"] = winerr
    entry["pbip_write_error"] = err
    entry["pbip_status"] = "failed"
    warns.append(_PBIP_WARN + message)


def _twbx_images(wb_id):
    """Return the packaged image bytes from a ``.twbx`` as ``{archive_path: bytes}`` (else ``{}``).

    A ``.twbx`` is a zip; its dashboard logos/icons live under ``Image/`` (occasionally ``Assets/``
    or with an ``image/`` prefix). ``wb_id`` is the workbook source id -- a filesystem path for a
    local source. Anything that is not a readable zip on disk (a live-source LUID, a bare ``.twb``,
    an in-memory fake) yields ``{}`` so the caller simply emits no image visuals (never-regress).
    """
    import zipfile
    try:
        if not (isinstance(wb_id, str) and os.path.isfile(wb_id)):
            return {}
        if not zipfile.is_zipfile(wb_id):
            return {}
        out = {}
        with zipfile.ZipFile(wb_id) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                low = name.lower()
                if not low.rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "gif", "bmp", "svg"):
                    continue
                out[name] = zf.read(info)
        return out
    except Exception:
        return {}


def _land_combined_flatfiles(packaged_source, descriptor, dest_dir):
    """Land EVERY flat-file island's bundled data for a consolidated multi-datasource workbook model.

    A ``.twbx`` that embeds several datasources consolidates into ONE model whose relations each route
    to their OWN named connection (``_effective_connection`` returns ``relation["connection"]`` when
    ``named_connection_count > 1``). Each of those per-relation connections carries only the workbook-
    RELATIVE flat-file path (e.g. ``Data/Superstore/Sample - Superstore.xlsx``), which Power BI's
    ``File.Contents`` rejects -- so the emitted partition opens but loads NO data, and only the primary
    island's file was ever lifted to disk. This lifts EACH relation's bundled Excel/CSV out of the
    package to an ABSOLUTE on-disk path and pins it on the relation as ``flatfile_path`` (which the
    emitter prefers over the connection's relative path), so every island's data both lands and loads.

    Reuses the proven, fail-closed :func:`extract_bundled_flatfile` per relation (deduped by absolute
    destination, so tables sharing a workbook -- Orders/People/Returns -> one Excel -- lift it once).
    Returns the list of absolute paths landed. Fail-closed: a relation whose file can't be lifted is
    left untouched (keeps its relative path, exactly as before), and the helper never raises. A single-
    connection descriptor has no per-relation ``connection`` facts, so this is a no-op there.
    """
    landed = []
    if not packaged_source or not dest_dir or not isinstance(descriptor, dict):
        return landed
    for rel in (descriptor.get("relations") or []):
        if rel.get("flatfile_path"):  # already absolute (e.g. seeded from a sibling) -> leave it
            continue
        conn = rel.get("connection") or {}
        filename = conn.get("flatfile_filename") or conn.get("filename")
        if not filename:  # live-DB / non-flat-file relation -> nothing bundled to lift
            continue
        directory = conn.get("flatfile_directory") or conn.get("directory")
        mini = {"flatfile_filename": filename, "flatfile_directory": directory}
        try:
            abs_path = extract_bundled_flatfile(packaged_source, mini, dest_dir)
        except Exception:
            abs_path = None
        if abs_path:
            rel["flatfile_path"] = abs_path
            if abs_path not in landed:
                landed.append(abs_path)
    return landed


def _scatter_keys_from_ir(result):
    """The scatter composite grain keys this workbook's IR requires, or ``[]``.

    Pure reader of the first viz pass. Never raises: an IR that cannot be read simply requests no
    keys, and the scatter then degrades to the single-pill behaviour with its own warning.
    """
    try:
        ir = (result or {}).get("ir") if isinstance(result, dict) else None
        if not ir:
            return []
        try:
            from . import twb_to_pbir as _tp
        except ImportError:
            import twb_to_pbir as _tp
        return _tp.scatter_composite_keys(ir)
    except Exception:
        return []


def _date_usage_from_ir(result):
    """``{date column (lowered): shelf uses}`` this workbook's IR reports, or ``{}``.

    Pure reader of the first viz pass, mirroring ``_scatter_keys_from_ir`` / ``_colour_palettes_from_ir``:
    only the report layer can see which date field the author actually put on a shelf, and only the
    model can choose the calendar's ACTIVE relationship, so the fact travels report -> model.

    This is what lets ``assemble_model._select_primary_date`` stop guessing from naming conventions.
    Never raises: an IR that cannot be read supplies no usage, and the model build falls back to the
    conventions exactly as before.
    """
    try:
        ir = (result or {}).get("ir") if isinstance(result, dict) else None
        if not ir:
            return {}
        try:
            from . import twb_to_pbir as _tp
        except ImportError:
            import twb_to_pbir as _tp
        return _tp.date_field_usage(ir) or {}
    except Exception:
        return {}


def _colour_palettes_from_ir(result):
    """The AUTHORED discrete-colour palettes this workbook's IR carries, or ``{}``.

    Pure reader of the first viz pass, mirroring ``_scatter_keys_from_ir``: only the report layer
    can see a worksheet's ``<map to='#hex'>`` colour assignment, and only the model can own the
    hex-returning twin measure, so the fact travels report -> model. Never raises: an IR that
    cannot be read supplies no palette, and the twin falls back to Tableau's own default
    categorical ramp (which is what an unauthored worksheet actually renders).
    """
    try:
        ir = (result or {}).get("ir") if isinstance(result, dict) else None
        if not ir:
            return {}
        try:
            from . import twb_to_pbir as _tp
        except ImportError:
            import twb_to_pbir as _tp
        return _tp.discrete_colour_palettes(ir) or {}
    except Exception:
        return {}


_COLOUR_TWIN_SUFFIX = " (colour)"
_COLOUR_TWIN_DECL_RE = re.compile(r"^\tmeasure '([^']+ \(colour\))'\s*=", re.M)


def _phantom_published_proxy_tables(model_parts):
    """Emitted tables bound to a published datasource's ``sqlproxy`` proxy rather than to real data.

    A published Tableau datasource connects through a federated proxy whose connection class is
    ``sqlproxy`` and whose server is the literal ``localhost`` -- an internal Tableau address, not an
    endpoint anything can reach. When the PRIMARY datasource is that proxy the estate path already
    gates honestly (``pbip_status: skipped``, needs-storage-decision). When a SECONDARY is, nothing
    did: the workbook built, looked complete, and carried an empty disconnected table whose M points
    at ``localhost`` (#174, on a 12-workbook estate where sibling workbooks gated correctly).

    THE SILENCE IS THE DEFECT. The model opens, validates, and binds; the table is simply always
    empty. That is the same family as a ``= BLANK()`` measure (2.227.0) and a dangling ``SelectRef``:
    structurally valid, semantically absent, and invisible to every structural check. So this reads
    the EMITTED artifact -- the connection parameters actually written -- rather than re-deriving
    intent from the workbook, because what ships is what matters.

    Keyed on the ``sqlproxy`` parameter suffix that ``emit_connection_parameters`` writes for a
    federated proxy connection, together with the ``localhost`` server value. Both must be present:
    a real connector that happens to be on localhost is a legitimate local database, and a
    ``_sqlproxy`` parameter with a real host is a resolved rebind rather than a phantom.

    Returns ``[{"parameter", "database"}]`` sorted, empty when nothing qualifies -- so a build with
    no published secondary is unaffected.
    """
    if not isinstance(model_parts, dict):
        return []
    out = []
    for path, text in sorted(model_parts.items()):
        if not (isinstance(path, str) and path.endswith("expressions.tmdl")
                and isinstance(text, str)):
            continue
        # ``expression Server_sqlproxy = "localhost" meta [...]`` and its Database_ sibling.
        servers = dict(re.findall(
            r'(?m)^expression\s+(Server_sqlproxy\w*)\s*=\s*"([^"]*)"', text))
        dbs = dict(re.findall(
            r'(?m)^expression\s+(Database_sqlproxy\w*)\s*=\s*"([^"]*)"', text))
        for name, host in sorted(servers.items()):
            if host.strip().lower() != "localhost":
                continue
            suffix = name[len("Server"):]
            out.append({"parameter": name, "database": dbs.get("Database" + suffix)})
    return out


def _visuals_projecting_stub_measures(model_parts, report_parts):
    """Visuals that project a measure whose whole expression is an inert ``BLANK()`` stub.

    THE FAMILY THIS BELONGS TO: structurally valid, semantically absent. When a calc cannot be
    translated the model emits ``measure 'X' = BLANK()`` so the reference still resolves -- and it
    resolves *perfectly*. The visual binds, `pbir_lint` is clean, `lint_visual_model_bindings` is
    clean (the measure genuinely exists), `powerbi-report-author validate` returns 0 errors, and the
    chart renders EMPTY. Measured on corpus workbook 0136 before 2.225.0: Sheet 3 projected
    ``complex nested``, which was a stub, while ``viz_fidelity`` recorded
    ``{"status": "rebuilt", "reason": null}``. The MODEL layer knew (the translation handoff lists
    the calc as needs-review); the VISUAL layer never repeated it.

    Every existing gate here asks "is this well-formed"; none asks "does it SAY anything". That is
    why no structural check can find this and why it has to read the measure's EXPRESSION.

    Fail-closed, and narrowly: only an expression that is EXACTLY the stub form counts. A measure
    that returns blank conditionally -- ``IF(<cond>, 1)``, the shape every keep-flag uses -- is
    doing its job, and flagging it would fire on correct output constantly. A stub is recognised by
    ``= BLANK()`` alone, optionally parenthesised/whitespaced, never by "contains BLANK".

    Returns ``[{"visual", "page", "measure"}]`` sorted, empty when nothing qualifies -- so a build
    with no stubbed calc is unaffected.
    """
    if not isinstance(model_parts, dict) or not isinstance(report_parts, dict):
        return []
    stub_re = re.compile(r"(?m)^\s*measure\s+(?:'([^']+)'|(\S+))\s*=\s*\(?\s*BLANK\s*\(\s*\)\s*\)?\s*$")
    stubs = set()
    for path, text in model_parts.items():
        if not (isinstance(path, str) and path.endswith(".tmdl") and isinstance(text, str)):
            continue
        for m in stub_re.finditer(text):
            name = m.group(1) or m.group(2)
            if name:
                stubs.add(name)
    if not stubs:
        return []

    out = []
    for path, content in sorted(report_parts.items()):
        if not (isinstance(path, str) and path.endswith("visual.json")
                and isinstance(content, str)):
            continue
        try:
            doc = json.loads(content)
        except (TypeError, ValueError):
            continue
        norm = path.replace("\\", "/").split("/")
        page = norm[-4] if len(norm) >= 4 else None
        named = set()
        for ref in _iter_measure_property_names(doc):
            if ref in stubs:
                named.add(ref)
        for measure in sorted(named):
            out.append({"visual": doc.get("name") or (norm[-2] if len(norm) >= 2 else None),
                        "page": page, "measure": measure})
    return out


def _iter_measure_property_names(node):
    """Every ``Measure.Property`` string anywhere in a visual document."""
    if isinstance(node, dict):
        meas = node.get("Measure")
        if isinstance(meas, dict) and isinstance(meas.get("Property"), str):
            yield meas["Property"]
        for value in node.values():
            for name in _iter_measure_property_names(value):
                yield name
    elif isinstance(node, list):
        for value in node:
            for name in _iter_measure_property_names(value):
                yield name


def _retire_unreferenced_colour_twins(model_parts, report_parts):
    """Drop colour twins the SHIPPING report does not reference -> ``(parts, [retired names])``.

    A colour twin is a hex-returning measure the report binds through Field-value conditional
    formatting (rung 3). Rungs 1 and 4 -- a native ``Conditional`` and a declared Visual Calculation
    -- paint the same encoding while referencing NO model object, so wherever one of them wins the
    twin is dead weight in the model and a stray entry in Desktop's field list.

    KEYED ON THE EMITTED ARTIFACT, deliberately, and this is the third form this decision has taken:

      1. a PROXY -- re-derive "would a rule win?" from the formula in the model build. Two predicates
         that must agree forever, which is the assemble/emit divergence this collection keeps fixing.
      2. a SHARED FACT -- have the report emit its decision and the model read it. Correct in
         principle, but MEASURED INERT: the report->model channel runs off the FIRST viz pass, which
         carries facts true of the SOURCE (a worksheet's palette is in the IR before anything binds)
         and cannot carry facts true of the OUTPUT (which rung wins is decided at emit time, from
         resolver state that pass does not have). Instrumented on ``0070_new_max``: 3 candidate
         records, ZERO colour facts.
      3. THIS -- ask the shipped bytes. No predicate at all, so there is nothing for two layers to
         disagree about, and it self-corrects if a future rung changes what it references.

    Runs on ``report_parts`` AFTER the reference cross-check, because that is the ``.pbip`` the user
    opens -- the same reason :func:`pbir_lint` is run there rather than on the first pass.

    Fail-closed in both directions: a twin is retired only when its name appears nowhere in the
    report AND nowhere else in the model (another measure may reference it), the name match is a
    substring test so an over-match KEEPS the twin, and any problem leaves ``model_parts``
    untouched.
    """
    try:
        if not report_parts:
            # No report to ask. Absence of evidence is not evidence of absence: retiring here would
            # strip every twin whenever report emission produced nothing, which is exactly when the
            # model is most likely to still be needed. Fail closed.
            return model_parts, []
        report_blob = "".join(str(v) for v in (report_parts or {}).values())
        names = set()
        for text in (model_parts or {}).values():
            names.update(_COLOUR_TWIN_DECL_RE.findall(str(text)))
        if not names:
            return model_parts, []

        model_blob = "".join(str(v) for v in (model_parts or {}).values())
        retired, out = [], dict(model_parts or {})
        for name in sorted(names):
            if name in report_blob:
                continue
            # A reference from elsewhere in the MODEL keeps it too. Its own declaration and its own
            # ``TableauFormula`` annotation both contain the name, so count references to the
            # DAX form ``[name]`` instead, which a declaration never produces.
            if ("[%s]" % name) in model_blob:
                continue
            retired.append(name)

        for name in retired:
            for path, text in list(out.items()):
                new = _drop_tmdl_measure_block(str(text), name)
                if new is not None:
                    out[path] = new
        return out, retired
    except Exception:
        return model_parts, []


def _drop_tmdl_measure_block(text, name):
    """Remove one ``\\tmeasure '<name>' = ...`` block from TMDL, or ``None`` if it is not there.

    A block owns its declaration line plus every following line that is blank or MORE indented; the
    next line at the measure's own indent starts a sibling. Written this way rather than by counting
    a fixed number of property lines because a measure carries a variable set of them (lineageTag,
    formatString, annotations), and a fixed count would silently eat a sibling's first line.
    """
    lines = str(text).split("\n")
    head = "\tmeasure '%s' =" % name
    start = next((i for i, l in enumerate(lines) if l.startswith(head)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith("\t\t")):
        end += 1
    # Keep one blank separator if the block was followed by a sibling, so the file does not
    # accumulate blank lines where twins were removed.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    if end < len(lines) and not lines[end].strip():
        end += 1
    return "\n".join(lines[:start] + lines[end:])


def _storage_decision_subject(label, descriptor=None, combine_datasources=None):
    """Name the thing a storage decision is owed FOR, in a message a reader can act on.

    A workbook's embedded datasources are consolidated into one model, and ``label`` is only the
    RANKED PRIMARY's caption -- so naming it as the subject of a failure a SECONDARY island owns
    sends the reader to the wrong datasource. Reported with #124, where the message read

        embedded datasource 'Big Data Source' needs a storage decision
          (... relation 'Orders.csv' has no resolvable columns ...)

    while both column-less relations belonged to 'Small Data Source' -- 'Big Data Source' was the one
    datasource in that workbook with nothing wrong with it. When the model spans several islands the
    subject is the consolidation, and every island is named; the per-island attribution of each
    individual reason rides inside the rationale (see ``combine_descriptors``).
    """
    if descriptor is None or not combine_datasources or len(combine_datasources) < 2:
        return "embedded datasource %r" % (label,)
    caps = [(d.get("caption") or d.get("name") or d.get("label")) for d in combine_datasources]
    caps = [c for c in caps if c]
    return "the consolidated model for %d embedded datasources (%s)" % (
        len(combine_datasources), ", ".join(repr(c) for c in caps))


def _build_datasource_pbip(entry, wb_detail, twb_text, result, ds, *, label, model_safe, dest,
                           folder_rel, report_base, viz_name, viz=None, ds_catalog=None,
                           approved_calc_dax=None, wb_id=None, pbip_dir=None,
                           descriptor=None, combine_datasources=None, storage_decisions=None,
                           semantic_colours=False):
    """Rebuild ONE embedded datasource into a self-contained ``.pbip`` and record it on ``entry``.

    Extracted verbatim from ``_attach_workbook_pbip`` so a workbook with several embedded datasources
    can build one PBIP per datasource with per-datasource error isolation -- a failure here marks only
    THIS ``entry`` skipped-loud (its own ``pbip_warnings``); sibling datasources still build. ``entry``
    is the per-datasource result dict; for the single/primary datasource the caller passes the
    workbook's own ``detail`` so the top-level keys stay byte-identical. ``wb_detail`` is always the
    workbook-level detail -- ``_rebuild_from_published_match`` reads the workbook ``binding_signal``
    from it. ``dest`` is the absolute output folder, ``folder_rel`` the reported ``pbip_folder``,
    ``report_base`` names the ``.Report``/``.pbip``, and ``viz_name`` is the viz report name. Never
    raises; appends honest ``pbip_warnings`` for every case it cannot faithfully bind.
    """
    entry.setdefault("pbip_warnings", [])
    entry.setdefault("pbip_ref_drops", [])
    warns = entry["pbip_warnings"]
    entry["bound_datasource"] = label
    # Flat-file Import (Excel/CSV bundled inside the .twbx): extract the embedded data to an ABSOLUTE
    # path under the bundle's data/ dir so the workbook .pbip opens AND loads. ``wb_id`` is the packaged
    # workbook (the .twbx path for a local source); a live DB embedded source has no flatfile_filename,
    # so this is a no-op there. ``migrate_datasource`` does the extraction (fail-closed).
    _ff_dest = None
    if wb_id is not None and pbip_dir:
        _ff_dest = os.path.join(os.path.dirname(os.path.abspath(pbip_dir)), "data", model_safe)
    # Reuse a sibling datasource's already-materialized flat-file data. A .twbx usually does NOT bundle
    # its extract -- the data lives in the published/sibling .tdsx that the estate migrated separately
    # (datasources are migrated before workbooks). When that datasource already landed its Excel/CSV at
    # an absolute path (``flatfile_path``) or read its .hyper to CSV (``table_csv_paths``), bind the
    # workbook's model to the SAME data so the workbook .pbip loads, instead of leaving the relative
    # path Power BI Desktop cannot open. When there is no sibling match, migrate_datasource still tries
    # to materialize data bundled in the .twbx itself (Excel/CSV, or an embedded .hyper extract).
    ff_path = None
    local_data = None
    if descriptor is not None and combine_datasources:
        # Consolidated model: each island's flat-file data was materialized when its datasource was
        # migrated separately (datasources run before workbooks). Merge every island's landed CSV set
        # into one ``local_data`` dict so the single combined model loads them all; the first Excel
        # ``flatfile_path`` seeds the scalar (per-relation connection facts route each table).
        merged_local = {}
        for _d in combine_datasources:
            cat = (ds_catalog or {}).get(
                _norm_ds(_d.get("caption") or _d.get("name") or _d.get("label")))
            if cat and not cat.get("__ambiguous__"):
                if cat.get("table_csv_paths"):
                    merged_local.update(cat["table_csv_paths"])
                if ff_path is None and cat.get("flatfile_path"):
                    ff_path = cat.get("flatfile_path")
        local_data = merged_local or None
        # >>> PATCH: embedded-only workbook -> ds_catalog is empty, so nothing backfilled the non-
        # primary islands. Lift EVERY island's bundled flat file straight from the .twbx and pin an
        # absolute per-relation ``flatfile_path`` so all islands' data lands AND loads. No-op when
        # there is nothing bundled to lift (live-DB islands, or a sibling supplied absolute paths).
        _land_combined_flatfiles(wb_id, descriptor, _ff_dest)
        # <<< PATCH
    elif ds_catalog:
        cat = ds_catalog.get(_norm_ds(ds.get("caption") or ds.get("name") or label))
        if cat and not cat.get("__ambiguous__"):
            ff_path = cat.get("flatfile_path")
            local_data = cat.get("table_csv_paths")
    # Consolidated model (combined descriptor): migrate_datasource would auto-extract calcs scoped to
    # the FIRST datasource island only (extract_calcs(tds_text, datasource=None)), silently dropping
    # every calculated field defined on a later island. Extract calcs GLOBALLY across all islands here
    # and pass them explicitly so the ONE consolidated model carries every island's measures and
    # dimension calcs. Fail-closed: any extraction error leaves them None -> migrate_datasource's own
    # auto-extraction (unchanged). The single-datasource branch (descriptor is None) passes calcs=None/
    # dim_calcs=None, byte-identical to omitting them, so its behaviour is untouched.
    calcs = dim_calcs = None
    if descriptor is not None:
        try:
            _m, _skipped, _dims = extract_calculations(twb_text, include_dimensions=True)
            calcs, dim_calcs = _m, _dims
        except Exception:
            calcs = dim_calcs = None
    try:
        res = migrate_datasource(twb_text, model_name=model_safe,
                                 storage_decision=_storage_decision_for(label, storage_decisions),
                                 datasource=(None if descriptor is not None else label),
                                 descriptor=descriptor,
                                 calcs=calcs, dim_calcs=dim_calcs,
                                 approved_calc_dax=approved_calc_dax,
                                 packaged_source=wb_id, flatfile_dest_dir=_ff_dest,
                                 flatfile_path=ff_path, local_data=local_data,
                                 # Composite grain keys for any scatter Tableau grains by 2+ Detail
                                 # dimensions. Read from the FIRST viz pass's IR, so the model emits
                                 # the key column before the second pass binds it. Empty (and the
                                 # model byte-identical) unless such a scatter exists.
                                 scatter_keys=_scatter_keys_from_ir(result),
                                 # The author's own discrete colour assignments, read from the
                                 # first viz pass so the model's colour twin paints the members the
                                 # colours the workbook actually declares. Empty -> Tableau's own
                                 # default categorical ramp, which is what an unauthored worksheet
                                 # renders.
                                 colour_palettes=_colour_palettes_from_ir(result),
                                 # Which date field the author actually put on a SHELF, read from the
                                 # first viz pass. The model build uses it to pick each fact's ACTIVE
                                 # calendar relationship instead of guessing from a naming
                                 # convention -- Salesforce writes pmdm__StartDate__c /
                                 # SystemModstamp, which match no convention, so those facts got NO
                                 # active date and lost their whole calendar hierarchy. Empty -> the
                                 # naming conventions, byte-for-byte as before.
                                 date_usage=_date_usage_from_ir(result),
                                 semantic_colours=semantic_colours)
    except Exception as exc:
        warns.append(_PBIP_WARN + f"could not rebuild embedded datasource {label!r} "
                     f"({type(exc).__name__}: {exc}) -- workbook .pbip skipped")
        return

    res_report = res.get("report") or {}
    # Honest flat-file data signal: when the embedded datasource names a flat file but the data could
    # NOT be materialized to an absolute path (no bundled file / a .hyper present but tableauhyperapi
    # not installed / fetched without includeExtract), the emitted model OPENS but loads no data. Warn
    # explicitly rather than silently shipping a broken model. Successful landings stay quiet.
    _ffd = res_report.get("flatfile_data")
    if _ffd:
        entry["flatfile_data"] = {"landed": bool(_ffd.get("landed")),
                                   "kind": _ffd.get("kind"), "reason": _ffd.get("reason"),
                                   "hyper_present": _ffd.get("hyper_present")}
    if _ffd and not _ffd.get("landed"):
        _why = {
            "hyperapi_unavailable": "the workbook bundles a .hyper extract but the optional "
                                    "tableauhyperapi is not installed (pip install tableauhyperapi)",
            "no_bundled_data": "the workbook bundles neither the source file nor a .hyper extract -- "
                               "re-fetch the workbook with --include-extract",
            "not_a_package": "the embedded datasource carries no bundled data to land",
        }.get(_ffd.get("reason"), _ffd.get("reason") or "data could not be materialized")
        warns.append(_PBIP_WARN + f"embedded datasource {label!r} is flat-file but its data was not "
                     f"landed to an absolute path -- the model opens but loads no rows ({_why})")
    if res_report.get("fallback"):
        # Published-datasource workbook: its own embedded copy is a sqlproxy proxy stub with no
        # usable schema, so rebuilding it routes to the needs-storage-decision fallback. When the
        # estate already built the matching published datasource, rebuild the model from THAT real
        # schema -- carrying the workbook's own calculated fields so its view-local measures
        # translate -- and bind the report to it. Never guesses (a real datasource-name match is
        # required); any failure keeps the honest skip below (warn-never-wrong).
        #
        # NO ``descriptor is None`` GATE (#155). It used to skip recovery whenever a combined /
        # federated descriptor was present, on the rationale that "its islands are already real
        # schemas -- a fallback there is a genuinely-undoable shape". A reader measured the
        # counter-example: a published datasource PLUS one small embedded federated datasource is
        # both things at once, and the workbook was skipped entirely with "co-migrate its published
        # datasource" while the published rebuild was sitting there available -- 52 measures / 86.5%
        # translated once the gate was bypassed.
        #
        # Removing it cannot loosen the safety model, and the reason is structural rather than a
        # judgement call: this branch only runs when ``res_report["fallback"]`` is already set, i.e.
        # the model HAS ALREADY FAILED. The gate therefore never protected a good model -- it only
        # decided whether to attempt a recovery on a build that had nothing left to lose. The three
        # guards that do the protecting live in the callee, which takes no ``descriptor`` at all: a
        # catalog must exist, the binding signal must be ``published``, and the name must match
        # exactly one non-ambiguous entry. It returns None otherwise and the honest skip below
        # stands. A caller-side condition the callee never asked for is a second predicate that can
        # disagree with the first -- the same shape as a gate keyed on a proxy.
        recovered = _rebuild_from_published_match(wb_detail, twb_text, model_safe, ds_catalog,
                                                  approved_calc_dax=approved_calc_dax)
        if recovered is not None:
            res = recovered
            res_report = res.get("report") or {}
        if res_report.get("fallback"):
            _dec = res_report.get("storage_decision") or {}
            rationale = _dec.get("rationale") or "undoable shape"
            subject = _storage_decision_subject(label, descriptor, combine_datasources)
            # An OPERATOR-supplied DirectLake decision is an outcome, not an unanswered question:
            # the answer WAS given, and what it produces is a landing plan rather than a model. The
            # plan has to reach disk -- promising one in a warning and writing nothing is exactly
            # the silent gap #116 reported. Everything else keeps the original demand wording.
            if _dec.get("fallback") == FALLBACK_LAND_TO_DELTA and res_report.get("landing_plan"):
                plan_rel = f"landing_plans/{model_safe}.landing_plan.json"
                try:
                    plan_abs = os.path.join(pbip_dir or dest, os.pardir, plan_rel)
                    plan_abs = os.path.normpath(plan_abs)
                    os.makedirs(os.path.dirname(plan_abs), exist_ok=True)
                    with open(plan_abs, "w", encoding="utf-8") as fh:
                        json.dump(res_report["landing_plan"], fh, indent=2)
                    entry["landing_plan"] = plan_rel
                    warns.append(_PBIP_WARN + f"{subject} was resolved by an OPERATOR DECISION to "
                                 f"land-to-Delta + DirectLake, so a landing plan was written to "
                                 f"{plan_rel} instead of a direct-upstream model ({rationale})")
                except OSError as exc:
                    warns.append(_PBIP_WARN + f"{subject} chose land-to-Delta + DirectLake but its "
                                 f"landing plan could not be written ({exc}) ({rationale})")
                return
            warns.append(_PBIP_WARN
                         + f"{subject} "
                         f"needs a storage decision ({rationale}) "
                         f"-- workbook .pbip skipped (model lands separately)")
            return

    report_parts = _rebind_report_byPath(result["parts"], model_safe)
    # Authoritative emitted-model facts for the workbook path (additive). Recorded HERE, after any
    # published-datasource fallback recovery has settled which model actually won, and taken from
    # what the build PRODUCED rather than from the source inventory -- so the storage mode and the
    # table/column counts a consumer reads are the ones in the emitted TMDL, not an estimate of them.
    _sd = res_report.get("storage_decision") or {}
    _manifest = res_report.get("model_manifest") or {}
    _tabs = _manifest.get("tables") or res_report.get("tables") or []
    entry["model_facts"] = {
        "storage_mode": _sd.get("mode"),
        "connector": _sd.get("connector"),
        "table_count": len(_tabs),
        "column_count": len(_manifest.get("columns") or []),
    }
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
    field_model_table, field_map, _ambiguous_names = _field_map_from_model(res_report)
    # A DECLARED Tableau blend whose two sides landed as unrelated model tables. Reported here
    # because the model manifest (which says where each field landed) only exists once the model is
    # built. See ``_blend_link_warnings``: the keys are quoted from Tableau, not guessed.
    warns.extend(_blend_link_warnings(twb_text, res_report))
    # A landed table with NO relationship to anything returns its GRAND TOTAL identically on every
    # row of a breakdown, and every structural gate passes. Where the source declares a join we
    # already recover it, so an orphan means the source declared none -- which cannot be invented,
    # only reported (issue #107).
    for _orphan in (res_report or {}).get("orphan_tables") or []:
        _shared = _orphan.get("shared_with_fact") or []
        _extra = ("; it shares %s with %r, which are the candidate join keys"
                  % (", ".join(repr(c) for c in _shared[:6]), _orphan.get("fact_table"))
                  if _shared and _orphan.get("fact_table") else "")
        _dupe = (" This also means the model carries TWO date tables: this one from the source, and "
                 "the synthetic Date table the fact is actually related to."
                 if _orphan.get("duplicate_date_dimension") else "")
        warns.append(
            _PBIP_WARN + ("table %r landed with NO relationship to any other table -- a measure "
                          "broken down by its columns returns the whole table's GRAND TOTAL "
                          "identically for every member, and no structural gate can see that. The "
                          "source declared no join for it, so none was invented%s.%s"
                          % (_orphan.get("table"), _extra, _dupe)))
    # ISSUE #103, option (b). Where one datasource genuinely carries the same caption on two of its
    # tables, no datasource-scoped key could be emitted (there is nothing to prefer), so resolution
    # falls through to the bare caption -- i.e. the FIRST table written claims it. That is a guess,
    # and a guess that renders perfectly while being arithmetically wrong is the worst failure mode
    # this pipeline has. Report it, so the reader can check the one pill instead of trusting silence.
    for _amb in _ambiguous_names or []:
        _ds, _, _cap = str(_amb).partition("||")
        warns.append(
            _PBIP_WARN + ("field %r is ambiguous within datasource %r (the same column name exists "
                          "on more than one of its tables) -- any visual using it without a "
                          "matching Tableau relation name binds to whichever table was written "
                          "first; verify its table is the one you expect"
                          % (_cap or _amb, _ds)))
    # column_binding -- calc DIMENSION pills (a crosstab axis built from a Tableau calculated field)
    # rebind to the REAL model table+column the datasource build emitted, so a calc-dimension crosstab
    # stays a matrix bound to real fields (e.g. Sheet1[Director]) instead of the datasource-caption
    # fallback that ships an empty/mis-bound visual. Read from the BUILT model parts (res["parts"]);
    # None when the model materialised no such calc column (byte-unchanged standing resolution).
    column_binding = _column_binding_from_model(res.get("parts"))
    # Packaged dashboard images (logos / export-filter-info icons stored inside the .twbx). Extracted
    # once here and threaded to the viz stage so each image object rebuilds as a positioned PBIR image
    # visual. Empty for a bare .twb / live source (never-regress: viz emits no image visuals).
    wb_images = _twbx_images(wb_id)
    if (date_binding or measure_binding or row_count_binding or param_binding
            or field_map or column_binding or wb_images) and viz is not None:
        try:
            rebuilt = viz(twb_text, viz_name,
                          date_binding=date_binding, measure_binding=measure_binding,
                          row_count_binding=row_count_binding, param_binding=param_binding,
                          model_table=field_model_table, field_map=field_map,
                          column_binding=column_binding, resources=wb_images or None)
            if isinstance(rebuilt, dict) and rebuilt.get("parts"):
                report_parts = _rebind_report_byPath(rebuilt["parts"], model_safe)
                report_parts, wrapped_model_parts, row_wraps = _apply_row_predicate_wrapped_measures(
                    report_parts, res.get("parts"), rebuilt, res_report)
                if row_wraps:
                    res["parts"] = wrapped_model_parts
                    entry["row_predicate_wrap"] = {
                        "visuals": len(row_wraps),
                        "projections": sum(r.get("wrapped", 0) for r in row_wraps),
                        "worksheets": sorted({r.get("worksheet") for r in row_wraps
                                              if r.get("worksheet")}),
                    }
                if date_binding:
                    entry["date_rebind"] = {"date_table": date_binding["date_table"],
                                             "active_keys": date_binding["active_keys"]}
                if measure_binding:
                    entry["measure_rebind"] = {
                        "count": len((measure_binding.get("measures") or {}))}
                if row_count_binding:
                    entry["row_count_rebind"] = {
                        "count": len((row_count_binding.get("measures") or {}))
                        + (1 if row_count_binding.get("default") else 0)}
                if param_binding:
                    entry["param_rebind"] = {
                        "slicers": len((param_binding.get("slicers") or {})),
                        "flags": len((param_binding.get("flags") or {}))}
                if field_map:
                    entry["field_rebind"] = {
                        "count": len(field_map), "model_table": field_model_table}
                if column_binding:
                    entry["column_rebind"] = {
                        "count": len((column_binding.get("columns") or {}))}
                # The rebound report -- not the pre-rebind first pass -- is what lands in the
                # openable .pbip, so refresh the per-worksheet fidelity + implicit-row-count tally
                # from it. Now-bound row counts / measures / params clear their warnings here, so the
                # reported fidelity matches the project the user actually opens (warn-never-wrong: any
                # warning the rebound run still emits is carried, never masked).
                entry["viz_fidelity"] = _viz_fidelity(rebuilt)
                # Carry the full per-visual remediation worklist too (see ``_viz_worklist``): the
                # rebound pass is what lands in the openable .pbip, so its worklist is the one a
                # remediator should act on.
                _wl = _viz_worklist(rebuilt)
                if _wl is not None:
                    entry["remediation_worklist"] = _wl
                entry["viz_implicit_row_count"] = sum(
                    1 for w in (rebuilt.get("warnings") or [])
                    if "implicit row count" in (w.get("reason") or ""))
                # The visual-calculation rollup must likewise reflect the rebound pass -- the
                # first pass has no row-count binding, so view-only quick calcs whose base is the
                # implicit COUNT(*) only resolve here (base measure Count Orders binds in pass 2).
                _vc_rollup = _visual_calc_rollup(rebuilt)
                if _vc_rollup:
                    entry["visual_calculations"] = _vc_rollup
                _cs_rollup = _color_scale_rollup(rebuilt)
                if _cs_rollup:
                    entry["color_scale_defaults"] = _cs_rollup
                _mf_rollup = _measure_filter_rollup(rebuilt)
                if _mf_rollup:
                    entry["measure_filters_needs_review"] = _mf_rollup
        except Exception as exc:
            warns.append(_PBIP_WARN + f"model-fact rebind skipped ({type(exc).__name__}: {exc}) -- "
                         f"report binds to the standing source/deferred fields")
    # M1.3 ref cross-check: now that the real model is in hand, drop any viz projection that
    # references a measure/column the model did not emit (an optimistic `_Measures[caption]` bind
    # that dangles), so the whole viz layer is warn-never-wrong on field references -- not just MV.
    report_parts, ref_drops, ref_rebinds = _crosscheck_report_refs(
        report_parts, res.get("parts"),
        swap_specs=(res_report.get("field_parameters") or {}).get("specs") or None)
    if ref_rebinds:
        entry["pbip_ref_rebinds"] = ref_rebinds
        for r in ref_rebinds:
            warns.append(_PBIP_WARN + f"visual {r['visual']!r} rebound {len(r['rebound'])} "
                         f"measure reference(s) to a field parameter (the model remodeled a "
                         f"parameter-driven measure): {', '.join(r['rebound'])} -- keep a slicer on "
                         f"the field parameter to drive which measure is shown")
    if ref_drops:
        entry["pbip_ref_drops"] = ref_drops
        for d in ref_drops:
            tail = " (visual emptied)" if d["emptied"] else ""
            warns.append(_PBIP_WARN + f"visual {d['visual']!r} dropped {len(d['dropped'])} "
                         f"reference(s) the model did not emit: {', '.join(d['dropped'])}{tail}")
    # A colour twin nothing in the SHIPPING report references is dead weight -- rungs 1 and 4 paint
    # the same encoding without any model object. Keyed on the emitted bytes rather than on a
    # predicate, so no two layers can disagree about it. Runs here, after the cross-check, because
    # this is the .pbip the user opens, and before anything is written -- a fresh build emits no
    # .pbi/cache.abf at all (measured: 0 across the corpus), so there is no cache to invalidate.
    _pruned_parts, _retired_twins = _retire_unreferenced_colour_twins(
        res.get("parts"), report_parts)
    if _retired_twins:
        res["parts"] = _pruned_parts
        entry["colour_twins_retired"] = list(_retired_twins)
    # DISCLOSE A VISUAL THAT PROJECTS AN INERT STUB. A `= BLANK()` measure resolves perfectly, so
    # every structural gate above passes while the chart renders empty -- the model layer knows the
    # calc could not be translated and the visual layer never repeated it. Read on the SHIPPING
    # parts, after the cross-check and after twin retirement, so it describes the .pbip the user
    # opens rather than an intermediate.
    _stub_visuals = _visuals_projecting_stub_measures(res.get("parts"), report_parts)
    if _stub_visuals:
        entry["visuals_projecting_stub_measures"] = _stub_visuals
    # A published datasource reached only as a SECONDARY used to land as an empty table pointed at
    # localhost, with no gate and no flag, while sibling workbooks whose PRIMARY was published gated
    # honestly (#174). Read on the SHIPPING model parts so it describes what the user opens.
    _phantom = _phantom_published_proxy_tables(res.get("parts"))
    if _phantom:
        entry["phantom_published_proxy_tables"] = _phantom
        _names = ", ".join(sorted({str(p.get("database")) for p in _phantom if p.get("database")}))
        warns.append(
            _PBIP_WARN + "the model carries a published-datasource proxy bound to 'localhost' "
            f"({_names or 'unnamed'}) -- an internal Tableau address, not a reachable endpoint, so "
            "that table opens EMPTY and every visual over it is blank. Co-migrate the published "
            "datasource and rebind, or remove the dependency; a workbook whose PRIMARY datasource is "
            "published is gated for this reason, and this one reached it as a SECONDARY")
    # PROVE the cross-check above actually left nothing dangling, on the BYTES THAT SHIP.
    # ``_crosscheck_report_refs`` is supposed to drop or rebind every reference the model did not
    # emit; this asserts the result rather than trusting it. reference_gate proves the same invariant
    # for the DAX the second compiler writes -- nothing proved it for PBIR, where the failure is
    # WORSE: a visual bound to a column or measure the model does not contain neither errors nor
    # fails validation, it just renders EMPTY, so it reads as a data problem rather than a binding
    # problem, and ``powerbi-report-author validate`` reports 0 errors for it.
    #
    # Checked on ``report_parts`` AFTER the cross-check, because that is the .pbip the user opens.
    # Linting the pre-crosscheck parts instead reports references this stage has already removed --
    # the same first-pass-vs-shipped-artifact trap that makes ``out/reports/`` look wrong when the
    # project beside it is correct. The surface comes from the emitted TMDL, not ``model_manifest``:
    # the manifest covers data tables, so it does not know the generated Date table's calculated
    # columns or the parameter tables, and flags every valid reference to them (measured: 48 false
    # positives on one workbook). Fail-safe: any problem here leaves the run exactly as it was.
    try:
        import pbir_lint as _pbir_lint
        import reference_gate as _ref_gate
        _dangling = _pbir_lint.lint_visual_model_bindings(
            report_parts or {},
            _ref_gate.build_model_surface(tmdl_parts=(res or {}).get("parts") or {}))
    except Exception:
        _dangling = []
    if _dangling:
        entry["viz_dangling_bindings"] = {"count": len(_dangling), "problems": _dangling[:20]}
        warns.append(_PBIP_WARN + f"{len(_dangling)} visual field reference(s) name a model object "
                     f"that does not exist -- those visuals render EMPTY and report no error; "
                     f"first: {_dangling[0]}")
    # THE REST OF THE LINTER, on the same shipped bytes (#144 follow-up). Until now the estate path
    # called ONLY ``lint_visual_model_bindings``, so R3-R9 -- unknown visualType, theme-name
    # mismatch, card display units, nativeQueryRef uniqueness, empty pageOrder, dangling SelectRef,
    # missing required role -- were inert during an actual migration. They ran in pytest against one
    # representative workbook and nowhere else.
    #
    # Found by a per-emit-path corpus reach census: ``lint_pbir_parts`` was reached by 0 of 29
    # workbooks. That made the #144 fix incomplete in its own terms -- the issue was "the engine DoD
    # cannot detect structurally-invalid PBIR it emits", and the rule added for it did not run when
    # the engine emitted. Same exposure applied to R8. It is the #141 shape once more: a check whose
    # value is decided by whether anything calls it.
    #
    # Reported as a WARN rather than a hard failure on first wiring, deliberately: these rules have
    # never run against real estate output, so their true firing rate is unmeasured on anything but
    # the corpus. Escalating a never-executed check straight to a build failure is the mistake the
    # 2.154.0 sequencing note exists to prevent -- fix what fires, prove zero, then escalate.
    try:
        _lint = _pbir_lint.lint_pbir_parts(report_parts or {})
    except Exception:
        _lint = []
    # ``lint_visual_model_bindings`` runs inside ``lint_pbir_parts`` too when a surface is supplied;
    # here it is called without one, so the two result sets are disjoint by construction.
    if _lint:
        entry["viz_lint"] = {"count": len(_lint), "problems": _lint[:20]}
        warns.append(_PBIP_WARN + f"{len(_lint)} PBIR validity violation(s) in the emitted report "
                     f"(pbir_lint R3-R9); first: {_lint[0]}")
    projected = _longest_projected_path(dest, model_safe, res.get("parts"), report_base, report_parts)
    if os.name == "nt" and len(projected) >= MAX_PATH:
        # 1a (long-path era): the writer lifts MAX_PATH via ``\\?\`` so the build no longer FAILS on a
        # long path -- but a LOCAL .pbip nested this deep may not OPEN in Power BI Desktop unless Windows
        # long paths are enabled. Warn (non-fatal) and proceed; a shorter -o yields a locally-openable
        # project. A genuine write failure is still caught + reported LOUD below.
        warns.append(_PBIP_WARN + (
            f"workbook .pbip output path is {len(projected)} chars, at/over the Windows MAX_PATH "
            f"({MAX_PATH}) limit -- the build proceeds via long-path (\\\\?\\) writes, but to OPEN this "
            f".pbip locally in Power BI Desktop re-run with a shorter output root (e.g. -o C:\\tfmig) or "
            f"enable Windows long paths"))
    try:
        if os.path.isdir(dest):
            shutil.rmtree(_win_long_path(dest))
        write_local_pbip(res["parts"], dest, model_name=model_safe, report_name=report_base,
                         report_parts=report_parts, project_name=report_base)
    except OSError as exc:
        # 1c: a write failure is reported LOUD (failed), classified (path-length vs generic), never a
        # silent skip -- so a MAX_PATH failure on a published-DS workbook is not masked as "published
        # DS not in scope".
        cause = _classify_pbip_write_error(exc, projected)
        _record_pbip_write_failure(entry, warns, cause=cause, dest=dest, projected=projected, exc=exc)
        return

    entry.update(pbip_status="built",
                  pbip_folder=folder_rel,
                  bound_model=model_safe,
                  column_prune=res_report.get("column_prune"),
                  model_translation_handoff=res_report.get("translation_handoff"))
    # How many pages the emitted report declares. A report is only openable if it declares at least
    # one -- Desktop crashes on an empty ``pageOrder`` rather than opening an empty report -- so this
    # feeds the definition-of-done's loud openability gate. Additive; read from the written PBIR so
    # it reflects what actually landed on disk, not what the emitter intended.
    entry["pbip_page_count"] = _pbir_page_count(dest)
    # Surface the model's structural openability self-check (produced by the datasource build) onto the
    # entry so the workbook definition-of-done can FAIL LOUD when a report bound to a non-openable model
    # (e.g. a duplicate column that survived to TMDL) is produced -- a built .pbip is not the same as an
    # openable one. Additive; absent/malformed -> no axis contribution.
    _selfcheck = res_report.get("openability_selfcheck")
    if isinstance(_selfcheck, dict):
        entry["openability_selfcheck"] = _selfcheck
    # Honest disclosure (additive): any island that landed as a needs-review M partition scaffold
    # (an unmapped connector consolidated alongside mapped ones) is surfaced here so a stubbed-not-
    # dropped table is visible at the estate level -- for a consolidated workbook the model is built
    # in this path, so its stubbed partitions are not counted in the datasource-level rollup.
    _needs_review = res_report.get("partitions_needs_review")
    if _needs_review:
        entry["partitions_needs_review"] = _needs_review


def _connection_facts(descriptor):
    """Every distinct upstream connection a descriptor binds, as credential-gate facts.

    A Tableau datasource can federate SEVERAL live systems at once (Azure SQL + Snowflake +
    Databricks in one datasource is a real, shipped shape), and the emitted model binds each table
    to its own connection. Reporting only the datasource's primary ``connection_class`` therefore
    understates what a migration will actually touch -- for a credential gate that is not merely
    incomplete, it reads as "one system to authenticate" when the true answer is three.
    """
    conns = (descriptor or {}).get("connections") or {}
    out = []
    for cid in sorted(conns):
        c = conns[cid]
        if not isinstance(c, dict):
            continue
        out.append({
            "connection_class": c.get("connection_class"),
            "server": c.get("server") or None,
            "database": c.get("database") or None,
            "warehouse": c.get("warehouse") or None,
            "schema": c.get("schema") or None,
            "auth_method": c.get("auth_method") or None,
        })
    return out


def _embedded_datasource_telemetry(twb_text, all_ds):
    """Per-embedded-datasource connection telemetry for the WORKBOOK path.

    The datasource path reports what it connected to (``connector``, ``storage_mode``, counts) from
    ``ds_details``; a workbook has no standalone datasource asset, so that block stayed empty even
    though the workbook path had already parsed every embedded datasource and emitted correct,
    distinct connectors for them. Anything consuming ``report.json`` to answer "what live systems
    will this model touch?" got ``[]`` and had to re-derive the answer by parsing connector function
    names back out of the emitted TMDL -- fragile, and a duplicate of work already done here.

    Best-effort by construction: a datasource whose descriptor will not parse still reports the
    inventory facts it does have, so one bad datasource cannot blank the whole block.
    """
    out = []
    for ds in all_ds or []:
        label = ds.get("label") or ds.get("caption") or ds.get("name")
        entry = {
            "caption": ds.get("caption") or ds.get("name") or label,
            "label": label,
            "connection_class": ds.get("connection_class"),
            "named_connection_count": ds.get("named_connection_count"),
            "table_count": ds.get("table_count"),
            "connections": [],
        }
        try:
            entry["connections"] = _connection_facts(parse_tds(twb_text, label))
        except Exception:
            pass
        out.append(entry)
    return out


def _locale_dependent_flatfile_warnings(twb_text, all_ds):
    """Warnings for relations whose typed M would depend on the machine's ambient locale.

    A legacy ACE workbook (``.xls``/``.xlsb``) returns its cells as text ALREADY RENDERED in the
    host's locale, so ``Table.TransformColumnTypes`` parses them with whatever locale refreshes the
    model. On a comma-decimal host every decimal column is silently corrupted by ``10^decimals`` --
    measured at 493x on one column and 6,285x on another IN THE SAME TABLE, with the model building,
    refreshing, and passing TMDL deserialization, M syntax, the openability self-check and a
    persisted cache (issue #110). Only a numeric oracle caught it.

    No culture can be PROVEN for that source -- the rendering locale is not knowable at generation
    time and not observable from within M -- and a wrong guess is worse than none, so this reports
    it with the remedy instead. Every other flat file has its culture pinned at emission.
    """
    warns = []
    for ds in all_ds or []:
        label = ds.get("label") or ds.get("caption") or ds.get("name")
        try:
            rows = locale_dependent_flatfile_relations(parse_tds(twb_text, label))
        except Exception:
            continue
        for row in rows:
            warns.append(
                _PBIP_WARN + ("table %r reads a LEGACY Excel workbook (%s), whose cells arrive as "
                              "text already rendered in the refreshing machine's locale -- on a "
                              "comma-decimal host every decimal column inflates by 10^decimals and "
                              "every structural gate still passes. Convert the source to .xlsx or "
                              "CSV (or add an explicit culture to this partition) before trusting "
                              "its numbers"
                              % (row["table"], os.path.basename(row.get("path") or "") or "unknown")))
    return warns


def _attach_workbook_pbip(detail, twb_text, result, safe_base, pbip_dir, viz=None, ds_catalog=None,
                          approved_calc_dax=None, wb_id=None, storage_decisions=None,
                          semantic_colours=False):
    """Build ONE openable, self-contained workbook ``.pbip`` project and record it on ``detail``.

    Every embedded datasource in the workbook is rebuilt into a SINGLE semantic model. A workbook with
    one datasource yields it directly; a workbook with several has their descriptors combined into one
    model whose tables are disconnected islands -- each bound to its own upstream connection, exactly
    like a federated multi-connection datasource -- sharing only the assembler's synthesized Date
    dimension. Either way the layout is the established flat ``pbip/<WB>/{<Model>.SemanticModel,
    <WB>.Report, <WB>.pbip}`` and the single rebuilt report binds to that one model by path, so a
    dashboard whose views span datasources rebuilds in one pass instead of being split. Purely additive:
    it never alters the bare ``reports/`` write. Sets ``pbip_status``/``pbip_folder``/``bound_model``/
    ``bound_datasource``/``model_translation_handoff`` and appends honest ``pbip_warnings`` for every
    case it cannot faithfully bind (no embedded datasource, a datasource that will not parse, a
    needs-storage-decision fallback, write failure). Never raises.
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
    all_ds = [primary] + secondaries
    label = primary.get("label") or primary.get("caption") or primary.get("name")
    model_safe = _fs_safe(primary.get("caption") or primary.get("name") or label, "Model")
    detail["bound_datasource"] = label
    viz_name = detail.get("name") or safe_base
    # Connection telemetry for the workbook path (additive). Recorded BEFORE the single/multi split
    # so both shapes report identically, and before any build step that can bail out -- a workbook
    # whose model fails to land still tells a consumer which systems it would have touched.
    detail["embedded_datasources"] = _embedded_datasource_telemetry(twb_text, all_ds)
    warns.extend(_locale_dependent_flatfile_warnings(twb_text, all_ds))
    # SINGLE embedded datasource (the common case): keep the established FLAT ``pbip/<WB>/`` layout so
    # the top-level detail keys and the on-disk paths stay byte-identical. The report binds to the one
    # rebuilt model.
    if len(all_ds) == 1:
        _build_datasource_pbip(detail, detail, twb_text, result, primary, label=label,
                               model_safe=model_safe, dest=os.path.join(pbip_dir, safe_base),
                               folder_rel=f"pbip/{safe_base}/{safe_base}.pbip", report_base=safe_base,
                               viz_name=viz_name, viz=viz, ds_catalog=ds_catalog,
                               approved_calc_dax=approved_calc_dax, wb_id=wb_id, pbip_dir=pbip_dir,
                               storage_decisions=storage_decisions,
                               semantic_colours=semantic_colours)
        return

    # MULTIPLE embedded datasources: rebuild ALL of them into ONE semantic model as disconnected table
    # islands -- each table bound to its OWN upstream connection, exactly like a federated multi-
    # connection datasource (Power BI keeps the islands as separate tables sharing only the assembler's
    # synthesized Date dimension). A single PBIR report then binds to that ONE model in a single pass, so
    # a dashboard whose views span datasources rebuilds faithfully instead of being split across per-
    # datasource projects. Combining the parsed descriptors up front is the WHOLE change -- the model +
    # report build is the SAME single-datasource path fed a pre-combined descriptor. Zero silent drops:
    # combine_descriptors is a total union, and a datasource that fails to parse is recorded loud and
    # excluded while the rest still land.
    model_safe = _fs_safe(detail.get("name") or safe_base, "Model")
    descriptors = []
    captions = []
    for ds in all_ds:
        ds_label = ds.get("label") or ds.get("caption") or ds.get("name")
        try:
            descriptors.append(parse_tds(twb_text, ds_label))
            captions.append(ds.get("caption") or ds.get("name") or ds_label)
        except Exception as exc:
            warns.append(_PBIP_WARN + f"could not parse embedded datasource {ds_label!r} "
                         f"({type(exc).__name__}: {exc}) -- excluded from the combined model")
    if not descriptors:
        warns.append(_PBIP_WARN + "no embedded datasource could be parsed -- workbook .pbip skipped")
        return
    combined = combine_descriptors(descriptors, captions=captions)
    # Audit trail (additive): the island captions folded into the ONE model. Proves zero silent drops
    # (every parsed datasource is listed) and drives the summary's ``workbooks_multi_datasource`` stat.
    detail["consolidated_datasources"] = list(captions)
    _build_datasource_pbip(detail, detail, twb_text, result, primary, label=label,
                           model_safe=model_safe, dest=os.path.join(pbip_dir, safe_base),
                           folder_rel=f"pbip/{safe_base}/{safe_base}.pbip", report_base=safe_base,
                           viz_name=viz_name, viz=viz, ds_catalog=ds_catalog,
                           approved_calc_dax=approved_calc_dax, wb_id=wb_id, pbip_dir=pbip_dir,
                           descriptor=combined, combine_datasources=all_ds,
                           storage_decisions=storage_decisions,
                           semantic_colours=semantic_colours)


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
                          ds_catalog=None, approved_calc_dax=None, viz_advice=False,
                          storage_decisions=None, semantic_colours=False):
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
                shutil.rmtree(_win_long_path(dest))
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

    # The full per-visual remediation worklist (see ``_viz_worklist``) -- additive; absent when the
    # worklist module is unimportable, so a run without it is byte-identical to before.
    worklist = _viz_worklist(result)
    if worklist is not None:
        detail["remediation_worklist"] = worklist

    # NOTE: the pre-rebind detail path. Its parts are NOT what ships when the estate re-runs the viz
    # stage bound to the model, so this summarises the routing DECISION only -- see the open
    # ``visual-calc-report-unverified`` finding.
    vc_rollup = _visual_calc_rollup(result)
    if vc_rollup:
        detail["visual_calculations"] = vc_rollup

    cs_rollup = _color_scale_rollup(result)
    if cs_rollup:
        detail["color_scale_defaults"] = cs_rollup

    mf_rollup = _measure_filter_rollup(result)
    if mf_rollup:
        detail["measure_filters_needs_review"] = mf_rollup

    signal = _workbook_binding_signal(text, result.get("ir") if isinstance(result, dict) else None)
    if signal is not None:
        detail["binding_signal"] = signal

    if viz_advice and parts and safe_base is not None:
        _attach_viz_advice(detail, result, safe_base, reports_dir)

    if parts and pbip_dir is not None:
        _attach_workbook_pbip(detail, text, result, safe_base, pbip_dir, viz=viz,
                          storage_decisions=storage_decisions,
                          semantic_colours=semantic_colours,
                              ds_catalog=ds_catalog, approved_calc_dax=approved_calc_dax, wb_id=wb_id)
    return detail


def _looks_like_path(source):
    """True iff ``source`` is a filesystem path that exists (tolerant of raw-XML strings)."""
    if isinstance(source, (bytes, bytearray)) or not isinstance(source, (str, os.PathLike)):
        return False
    try:
        return os.path.exists(source)
    except (ValueError, OSError):  # e.g. an over-long or NUL-bearing raw-XML string
        return False


def _single_workbook_source(source, name=None):
    """Wrap a standalone workbook as a one-workbook :class:`TableauSource`.

    Returns ``(source, wb_id)`` ready for :func:`_migrate_one_workbook`. A filesystem path to a
    ``.twb`` / ``.twbx`` is served by :class:`LocalFilesSource` with the ABSOLUTE path as the id, so a
    packaged ``.twbx``'s bundled flat-file data is extracted at full fidelity (the downstream model
    build reads the bundle via that same path). Raw workbook XML (``str``/``bytes``, incl. ``.twbx``
    zip bytes) is served in memory; a bare XML body carries no bundled extract, so flat-file data
    honestly degrades (there is nothing to land). The display name is the file stem for a path, else
    ``name`` (default ``"workbook"``).
    """
    if _looks_like_path(source):
        path = os.path.abspath(os.fspath(source))
        return LocalFilesSource(os.path.dirname(path)), path
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        text = F.inner_doc_from_zip(data) if F.is_zip(data) else data.decode("utf-8-sig")
    else:
        text = source
    key = name or "workbook"
    return InMemoryTableauSource(workbooks={key: text}), key


def _second_compile_guards(workbook_text, output_dir):
    """Best-effort model-aware GUARD bundle for the second-compiler landing chokepoint, or ``None``.

    Assembles the reference gate + reconciliation oracle from the PRIOR build's on-disk artifacts:

    * reference-gate SURFACE = every ``*.tmdl`` under ``output_dir`` (a superset surface is safe for a
      rejection-only guard -- a bigger surface can only make the reference gate MORE permissive, never
      wrongly block; on a typical single-workbook re-run there is exactly one model anyway). This is the
      generated model's REAL, dedup'd names, so it catches the ``(copy)_NNNN`` duplicate-name trap a
      descriptor-built surface would miss;
    * oracle TABLES = every landed ``*.csv`` under ``output_dir`` (``materialize_bundled_flatfile_data``
      writes one CSV per table), keyed by CSV stem;
    * resolver = ONE combined ``caption -> (model_table, model_column, type)`` over the workbook's
      datasources, so the oracle can evaluate the Tableau formula in model terms.

    Meaningfully active ONLY on the opt-in ``--second-compile`` RE-RUN over an already-built output dir:
    the prepass runs BEFORE this run writes any model, so a FRESH dir holds no prior TMDL/CSV, every
    discovery step yields nothing, and :func:`second_compiler.build_guards` returns ``None`` -> the driver
    stays byte-identical to the unguarded pass. Every step fails closed to the absent half (never a raise);
    the guards only ever REJECT a candidate, so an imperfect bundle degrades to inert, never to a wrong
    landing (the oracle also evaluates BOTH sides against the SAME tables, so a CSV-stem/model-name
    mismatch makes it INCONCLUSIVE, never a false FAIL).
    """
    if not output_dir or not os.path.isdir(output_dir):
        return None
    try:
        try:  # scripts/ is on sys.path both as a CLI run and in tests
            from . import second_compiler as _sc
            from .connection_to_m import (workbook_datasources as _wds,
                                          build_m_field_resolver as _bmfr)
        except ImportError:
            import second_compiler as _sc
            from connection_to_m import (workbook_datasources as _wds,
                                         build_m_field_resolver as _bmfr)
    except Exception:
        return None

    tmdl_parts = {}
    table_csv_paths = {}
    try:
        for root, _dirs, files in os.walk(output_dir):
            for fn in files:
                low = fn.lower()
                if low.endswith(".tmdl"):
                    fp = os.path.join(root, fn)
                    try:
                        with open(fp, encoding="utf-8-sig") as fh:
                            tmdl_parts[os.path.relpath(fp, output_dir)] = fh.read()
                    except OSError:
                        pass
                elif low.endswith(".csv"):
                    table_csv_paths.setdefault(os.path.splitext(fn)[0], os.path.join(root, fn))
    except Exception:
        tmdl_parts, table_csv_paths = {}, {}
    tmdl_parts = tmdl_parts or None
    table_csv_paths = table_csv_paths or None

    resolver = None
    try:
        descriptors = []
        for ds in (_wds(workbook_text) or []):
            label = ds.get("label") or ds.get("caption") or ds.get("name")
            try:
                descriptors.append(parse_tds(workbook_text, label))
            except Exception:
                pass
        if not descriptors:
            try:
                descriptors = [parse_tds(workbook_text)]
            except Exception:
                descriptors = []
        if descriptors:
            combined = combine_descriptors(descriptors) if len(descriptors) > 1 else descriptors[0]
            resolver = _bmfr(combined)
    except Exception:
        resolver = None

    try:
        return _sc.build_guards(tmdl_parts=tmdl_parts, table_csv_paths=table_csv_paths, resolver=resolver)
    except Exception:
        return None


def _second_compile_prepass(single, wb_id, approved_calc_dax, authored, output_dir=None):
    """Opt-in Spec-4 pre-pass: land keystone-dependent stub calcs as faithful DAX and merge them
    UNDER any explicit ``approved_calc_dax`` (a human-approved entry always wins on a name clash).

    Fail-closed and side-effect-free: any error (workbook unreadable, driver import/runtime failure)
    yields the *unchanged* approved map plus a detail note, so turning the pre-pass on can never break
    a run. Returns ``(merged_approved_or_None, detail)`` where ``detail`` is the additive
    ``second_compile`` report record.

    ``output_dir`` (optional) is the run's output project directory. When it holds a PRIOR build's TMDL
    and/or landed CSVs (the opt-in ``--second-compile`` re-run over an existing ``.\\out``), a model-aware
    GUARD bundle is assembled from them and passed to the landing driver so a candidate that names a
    non-existent model reference (the ``(copy)_NNNN`` trap) or numerically diverges from its Tableau
    formula is REJECTED. On a fresh run the dir holds no prior artifacts -> guards ``None`` -> byte-
    identical to the unguarded pass. Guards act purely as rejection filters and never author/alter a
    candidate.
    """
    try:
        text = single.read_workbook(wb_id)
    except Exception as exc:
        return approved_calc_dax, {"landed": [], "count": 0,
                                   "note": f"workbook unreadable: {type(exc).__name__}: {exc}"}
    try:
        try:  # scripts/ is on sys.path both as a CLI run and in tests
            from . import second_compiler as _sc
        except ImportError:
            import second_compiler as _sc
        guards = _second_compile_guards(text, output_dir)
        rep = _sc.land_report(text, authored=authored, guards=guards)
    except Exception as exc:
        return approved_calc_dax, {"landed": [], "count": 0,
                                   "note": f"second-compile unavailable: {type(exc).__name__}: {exc}"}

    supplement = rep.get("approved") or {}
    if supplement:
        merged = dict(supplement)
        merged.update(approved_calc_dax or {})  # explicit human-approved DAX wins on a name clash
    else:
        merged = approved_calc_dax
    detail = {
        "landed": sorted(supplement),
        "count": len(supplement),
        "authored": rep.get("authored", []),
        "detectors": rep.get("detectors", []),
        "cascaded": rep.get("cascaded", []),
        "gate_failures": sorted(rep.get("gate_failures") or {}),
    }
    return merged, detail


def migrate_workbook(source, *, write_to=None, wb_id=None, name=None, viz_stage=None,
                     approved_calc_dax=None, viz_advice=False, pbip=True,
                     ds_catalog=None, used_folders=None,
                     second_compile=False, authored=None, layout=None, storage_decisions=None,
                     semantic_colours=False):
    """Migrate ONE Tableau workbook into an openable Power BI project (model + bound report).

    This is the public workbook primitive -- the same faithful rebuild+bind the estate performs per
    workbook, callable for a single workbook. :func:`migrate_estate` loops exactly this function, so a
    standalone workbook migration and an estate workbook migration share ONE code path.

    ``source`` is either a standalone workbook -- a filesystem path to a ``.twb`` / ``.twbx`` or raw
    ``.twb`` XML (``str``/``bytes``) -- or, for the estate, a live :class:`TableauSource` plus a
    ``wb_id`` selecting the workbook within it. Set ``name`` to override the display name of a
    standalone workbook (default: the file stem, or ``"workbook"`` for raw XML).

    ``write_to`` (required) is the output project directory: the rebuilt report is written under
    ``<write_to>/reports/<Name>.Report`` and, unless ``pbip=False``, the openable project under
    ``<write_to>/pbip/<Name>/`` (model rebuilt from the workbook's own embedded datasource + report
    bound to it by path). Returns the workbook detail dict (``name``, ``viz_status``, ``pbip_status``,
    ``bound_model`` / ``bound_datasource``, ``pbip_folder``, ``viz_fidelity`` ...). Never raises for a
    per-workbook migration failure -- the failure is reported on the detail dict (as ``_migrate_one_
    workbook`` does); only invalid ARGUMENTS raise ``ValueError``.

    ``ds_catalog`` / ``used_folders`` are the estate's shared caches (a published-datasource match
    catalog and the set of already-claimed output folder names). Standalone callers omit them.

    ``second_compile`` / ``authored`` (opt-in) turn on the Spec-4 SECOND-COMPILER landing pre-pass.
    When ``second_compile`` is true (or ``authored`` overrides are supplied) the driver
    (:mod:`second_compiler`) lands keystone-dependent stub calcs as faithful, gated DAX -- seeded from
    the engine's own idiom detectors plus any ``authored`` ``{calc_name: dax}`` overrides -- and merges
    the result UNDER ``approved_calc_dax`` (a human-approved entry always wins), so the very same
    ``--approved-dax`` landing seam carries them into every model build. The landed set is reported on
    the additive ``detail["second_compile"]`` key. When both are omitted the run is byte-identical.

    ``layout`` selects the dashboard zone-layout engine (``"legacy"``, the default, or ``"solver"``).
    Omitted / ``None`` keeps the established legacy scale, so the default run is byte-identical.
    """
    if not write_to:
        raise ValueError(
            "migrate_workbook writes an openable project (model + bound report); pass write_to=<dir>")

    if isinstance(source, TableauSource):
        single = source
        if wb_id is None:
            workbooks = source.list_workbooks()
            if len(workbooks) != 1:
                raise ValueError("pass wb_id to select which workbook to migrate from a "
                                 "multi-workbook source")
            wb_id = workbooks[0]
    else:
        single, wb_id = _single_workbook_source(source, name=name)

    reports_dir = os.path.join(write_to, "reports")
    pbip_dir = os.path.join(write_to, "pbip") if pbip else None
    os.makedirs(write_to, exist_ok=True)

    viz = _resolve_viz_stage(viz_stage, layout=layout)
    if used_folders is None:
        used_folders = set()

    sc_detail = None
    if second_compile or authored:
        approved_calc_dax, sc_detail = _second_compile_prepass(
            single, wb_id, approved_calc_dax, authored, output_dir=write_to)

    detail = _migrate_one_workbook(single, wb_id, viz, reports_dir, used_folders, pbip_dir,
                                   ds_catalog=ds_catalog, approved_calc_dax=approved_calc_dax,
                                   viz_advice=viz_advice, storage_decisions=storage_decisions,
                                   semantic_colours=semantic_colours)
    if sc_detail is not None:
        detail["second_compile"] = sc_detail
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


def _sha256_file(path, _chunk=1 << 20):
    """Streaming SHA-256 of a file's bytes (constant memory, so a large ``.twbx`` is fine)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_chunk), b""):
            h.update(block)
    return h.hexdigest()


def _build_input_manifest(source, ds_ids, wb_ids):
    """Record the identity (path, size, sha256, mtime) of every LOCAL input file the run consumed.

    Purely additive forensic artifact. It answers "*which bytes* did this run actually migrate" so an
    "use the exact copy I attached" request is auditable after the fact, and -- the tripwire the VS
    Code sessions asked for -- it surfaces filename COLLISIONS: the same asset stem discovered at two
    DIFFERENT paths. That is the signature of an input folder that was not staged clean (a stale prior
    copy sitting next to the freshly attached one), the one situation where an estate scan can migrate
    bytes the operator did not intend.

    Collisions are REPORTED, never fatal. This deliberately does not fail closed on the whole run: this
    is an estate scanner (``migrate_estate`` walks a folder and migrates every workbook it finds), and a
    genuine estate legitimately holds like-named workbooks in different subfolders -- one ambiguous pair
    must not abort the other 200 assets. The clean-input GUARANTEE lives in the runbook (stage each
    attached file into a fresh, empty, per-run input dir); this manifest is the audit trail + loud
    signal when that discipline slipped. ``_dedup_by_stem`` already merged same-directory packaged/bare
    twins upstream, so any collision seen here is genuinely cross-directory.

    Only :class:`LocalFilesSource` has real on-disk asset ids; a live PULL (Tableau API bytes, no chat
    attachment) is explicitly out of scope and yields an assets-less manifest.
    """
    manifest = {
        "source_kind": type(source).__name__,
        "verified_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verifier": "migrate_estate/input_identity/1",
        "assets": [],
        "collisions": [],
        "duplicate_bytes": [],
    }
    if not isinstance(source, LocalFilesSource):
        return manifest
    manifest["root"] = str(source.root)
    seen = {}
    by_digest = {}
    for kind, asset_id in ([("datasource", i) for i in ds_ids]
                           + [("workbook", i) for i in wb_ids]):
        rec = {"kind": kind, "name": source.asset_name(asset_id),
               "staged_input_path": os.path.abspath(asset_id)}
        try:
            st = os.stat(asset_id)
            rec["size_bytes"] = st.st_size
            rec["mtime_utc"] = datetime.fromtimestamp(
                st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["sha256"] = _sha256_file(asset_id)
        except OSError as exc:  # unreadable input: record the reason, never crash the run
            rec["error"] = str(exc)
        manifest["assets"].append(rec)
        # Collision key is (kind, stem): two workbooks named "Sales" at different paths are ambiguous,
        # but a datasource and a workbook that happen to share a stem are not (they land in distinct
        # output families), so they must not raise a false collision.
        stem = os.path.splitext(os.path.basename(asset_id))[0].lower()
        seen.setdefault((kind, stem), set()).add(rec["staged_input_path"])
        if rec.get("sha256"):
            by_digest.setdefault((kind, rec["sha256"]), set()).add(rec["staged_input_path"])
    manifest["collisions"] = [
        {"kind": kind, "stem": stem, "paths": sorted(paths)}
        for (kind, stem), paths in sorted(seen.items()) if len(paths) > 1
    ]
    # The SAME BYTES staged twice under different NAMES. The name-stem check above cannot see this,
    # and it is the more damaging case: the scanner migrates both copies, so every count in the
    # report doubles (workbooks, calcs, stubs, warned visuals) and the operator reads a doubled
    # ledger as fact. Measured 2026-08-07: one input folder held `<uuid>-Network Ops.twbx` and
    # `Network Ops.twbx` -- byte-identical, 116,779 bytes, one sha256 -- and reported 2 workbooks /
    # 40 calcs / 6 stubs where the truth was 1 / 20 / 3. ``collisions`` was `[]` throughout, because
    # the transfer-layer uuid prefix made the two stems differ.
    #
    # The digest was already being computed and simply never compared. Identical sha256 is PROOF of
    # a duplicate, not an inference, so this needs no heuristic. Reported, never fatal -- same
    # rationale as ``collisions``: one ambiguous pair must not abort an estate of 200 assets.
    manifest["duplicate_bytes"] = [
        {"kind": kind, "sha256": digest, "paths": sorted(paths)}
        for (kind, digest), paths in sorted(by_digest.items()) if len(paths) > 1
    ]
    return manifest


def migrate_estate(source, output_dir, *, viz_stage=None, pbip=True, rebind_plan=None,
                   rebind_bind_stage=None, approved_calc_dax=None, viz_advice=False,
                   second_compile=False, authored=None, layout=None, storage_decisions=None,
                   semantic_colours=False):
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

    ``second_compile`` / ``authored`` (optional, opt-in) turn on the Spec-4 SECOND-COMPILER landing
    pre-pass per workbook (see :func:`migrate_workbook`): keystone-dependent stub calcs are landed as
    faithful, gated DAX -- from the engine's own idiom detectors plus any ``authored``
    ``{calc_name: dax}`` overrides -- and merged UNDER ``approved_calc_dax`` (a human-approved entry
    always wins) so the same landing seam carries them into every model build. Each workbook detail
    gains an additive ``second_compile`` record. When both are omitted the run is byte-identical --
    this opt-in IS the spec's "automatic" second-compiler behavior, kept opt-in so the default remains
    byte-for-byte the committed baseline.

    ``layout`` (optional) selects the dashboard zone-layout engine: ``"legacy"`` (the default) keeps
    the established per-zone absolute scale, while ``"solver"`` resolves the whole zone TREE so
    sibling zones cannot overlap by construction. It is bound into the viz stage once, so every
    workbook in the run uses the same engine. Omitted / ``None`` leaves the run byte-identical.
    """
    sm_dir = os.path.join(output_dir, "semantic_models")
    pbip_dir = os.path.join(output_dir, "pbip") if pbip else None
    os.makedirs(output_dir, exist_ok=True)

    viz = _resolve_viz_stage(viz_stage, layout=layout)
    used_folders = set()

    ds_catalog = {}
    ds_ids = source.list_datasources()
    wb_ids = source.list_workbooks()
    ds_details = [_migrate_one_datasource(source, ds_id, sm_dir, used_folders, pbip_dir,
                                          ds_catalog=ds_catalog,
                                          approved_calc_dax=approved_calc_dax,
                                          storage_decisions=storage_decisions)
                  for ds_id in ds_ids]
    wb_details = [migrate_workbook(source, write_to=output_dir, wb_id=wb_id, viz_stage=viz,
                                   approved_calc_dax=approved_calc_dax, viz_advice=viz_advice,
                                   pbip=pbip, ds_catalog=ds_catalog, used_folders=used_folders,
                                   second_compile=second_compile, authored=authored,
                                   storage_decisions=storage_decisions,
                                   semantic_colours=semantic_colours)
                  for wb_id in wb_ids]

    summary = _summarize(ds_details, wb_details, viz is not None)
    fallbacks = [
        {"datasource": d["name"],
         "source_id": d.get("source_id"),
         "reason": d.get("reason"),
         "fallback_path": d.get("fallback_path") or FALLBACK_NEEDS_DECISION}
        for d in ds_details if d.get("status") == "fallback"
    ]

    report = {
        "tool": "migrate_estate",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source.describe(),
        "summary": summary,
        "datasources": ds_details,
        "workbooks": wb_details,
        "definition_of_done": _definition_of_done(wb_details, pbip_dir is not None),
        "pending_gates": _pending_gates(summary),
        "fallbacks": fallbacks,
    }
    # MACHINE-level blockers, additive and read-only. Distinct from every other signal in this
    # report: these say nothing about the workbook, the model or the emitted PBIR -- they say this
    # BOX will fail to open the handover. Surfaced because a customer trial lost time to exactly
    # that: Power BI Desktop refused to open the .pbip (and a blank one) because an outdated
    # Newtonsoft.Json sat in the machine's GAC, and every user's first assumption was that the
    # migration had produced something broken. Detection only -- the remedy is machine-wide, needs
    # elevation and the Windows SDK, and is never performed here. See environment_preflight.
    try:
        import environment_preflight as _envpre
        report["environment"] = {"findings": _envpre.environment_findings()}
    except Exception:
        report["environment"] = {"findings": []}
    # Absolute, copy-pasteable paths to every openable .pbip (additive; [] under --no-pbip). Resolved
    # here where output_dir is in scope, so the report/summary/stdout can hand the user a REAL path
    # instead of the run-relative pbip/<Name>/<Name>.pbip stored per detail.
    report["openable_outputs"] = _openable_outputs(report, output_dir)
    # Input identity manifest (additive): proves which local bytes were consumed and surfaces any
    # cross-directory filename collision (a not-clean input folder). Set BEFORE the summary render so
    # its collision banner can fire; written as its own artifact so existing outputs are untouched.
    report["input_manifest"] = _build_input_manifest(source, ds_ids, wb_ids)
    with open(os.path.join(output_dir, "input_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(report["input_manifest"], fh, indent=2, sort_keys=True)

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
    columns_pruned_hidden_total = 0

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
            _cp = d.get("column_prune") or {}
            columns_pruned_hidden_total += int(_cp.get("columns_pruned_hidden") or 0)
        elif status == "fallback":
            fallback += 1
            modes["fallback"] += 1
        else:
            error += 1

    # WORKBOOK-PATH datasource telemetry. A workbook owns no standalone datasource asset, so nothing
    # above this point contributes to the datasource block and every field in it stayed empty -- while
    # the workbook path had already parsed each embedded datasource and emitted correct, distinct
    # connectors for them. For a consumer asking "what live systems will this model touch?" an empty
    # ``connectors_seen`` does not read as "unknown", it reads as "nothing to authenticate", which is
    # the one wrong answer a credential gate must never be given. Folded in from facts the pipeline
    # already had: connectors from every embedded datasource AND every federated connection inside
    # them (a single datasource can bind three different systems), storage mode and table/column
    # counts from what the model build actually emitted.
    embedded_total = 0
    for w in wb_details:
        emb = w.get("embedded_datasources") or []
        embedded_total += len(emb)
        for e in emb:
            if e.get("connection_class"):
                connectors.add(e["connection_class"])
            for c in (e.get("connections") or []):
                if c.get("connection_class"):
                    connectors.add(c["connection_class"])
        facts = w.get("model_facts") or {}
        mode = facts.get("storage_mode")
        if mode in modes:
            modes[mode] += 1
        elif emb:
            # The workbook had embedded datasources but produced no model storage mode: its build
            # routed to the needs-storage-decision fallback (recorded loud in ``pbip_warnings``).
            # Counting it keeps the mode tally a complete census of attempted models rather than
            # quietly omitting the ones that did not land -- the fallback total is exactly the
            # number a reader is looking for when the estate under-delivers.
            modes["fallback"] += 1
        tables += facts.get("table_count") or 0
        columns += facts.get("column_count") or 0

    # Workbook-model calc rollup. A consolidated workbook builds its OWN semantic model (from the
    # workbook's embedded/published datasources); that model's calc-translation summary lives on
    # ``model_translation_handoff`` -- NOT in ``ds_details``. Without this fold-in, a workbook's calcs
    # never reach the top-level ``summary`` and the mandatory second-compiler gate
    # (``needs_review_total``) reads 0 even when dozens of workbook calcs are stubbed. Fold the
    # workbook ``needs_review`` into the existing gate and expose additive ``workbook_calcs_*`` totals
    # (never touches ``measures_*``). Empty ``wb_details`` (a datasource-only run) leaves every value
    # at 0, so a datasource-only summary is byte-for-byte unchanged.
    workbook_calcs_total = workbook_calcs_translated = 0
    workbook_calcs_stubbed = workbook_calcs_needs_review = 0
    for w in wb_details:
        wsum = (w.get("model_translation_handoff") or {}).get("summary") or {}
        workbook_calcs_total += int(wsum.get("total") or 0)
        workbook_calcs_translated += int(wsum.get("live") or 0)
        workbook_calcs_stubbed += int(wsum.get("stub") or 0)
        wb_nr = int(wsum.get("needs_review") or 0)
        workbook_calcs_needs_review += wb_nr
        needs_review_total += wb_nr
        # Fold each workbook model's hidden-column prune into the estate total. A consolidated workbook
        # prunes inside its OWN model build (``column_prune`` on the workbook detail), NOT via
        # ``ds_details`` -- so a pure-workbook run (no standalone datasources) would otherwise report
        # ``columns_pruned_hidden_total: 0`` even though the physical collapse fired. Additive and
        # None-safe: a workbook that pruned nothing contributes 0, so a no-hidden estate is unchanged.
        _wcp = w.get("column_prune") or {}
        columns_pruned_hidden_total += int(_wcp.get("columns_pruned_hidden") or 0)
    workbook_calcs_coverage_pct = (
        round(100.0 * workbook_calcs_translated / workbook_calcs_total, 1)
        if workbook_calcs_total else None)

    wb_built = sum(1 for w in wb_details if w.get("viz_status") == "built")
    wb_warned = sum(1 for w in wb_details if w.get("viz_status") == "warned")
    wb_error = sum(1 for w in wb_details if w.get("viz_status") == "error")
    wb_pbip_built = sum(1 for w in wb_details if w.get("pbip_status") == "built")
    # Per-workbook PBIP rollup. A multi-datasource workbook now consolidates ALL its embedded
    # datasources into ONE openable project (flat pbip/<WB>/), so it counts as a single built project
    # -- the same as a single-datasource workbook. ``consolidated_datasources`` records the island
    # captions folded into that one model (the anti-silent-drop audit trail). The legacy
    # ``datasource_pbips`` branch is kept only for backward compatibility with any older detail shape.
    datasource_pbips_total = 0
    datasource_pbips_built = 0
    for w in wb_details:
        entries = w.get("datasource_pbips")
        if entries:
            datasource_pbips_total += len(entries)
            datasource_pbips_built += sum(1 for e in entries if e.get("pbip_status") == "built")
        elif w.get("pbip_status"):
            # one consolidated project per workbook (single- or multi-datasource)
            datasource_pbips_total += 1
            datasource_pbips_built += 1 if w.get("pbip_status") == "built" else 0
    workbooks_multi_datasource = sum(
        1 for w in wb_details
        if len(w.get("consolidated_datasources") or []) > 1 or w.get("datasource_pbips"))
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
        "datasources_embedded_total": embedded_total,
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
        "workbook_calcs_total": workbook_calcs_total,
        "workbook_calcs_translated": workbook_calcs_translated,
        "workbook_calcs_stubbed": workbook_calcs_stubbed,
        "workbook_calcs_needs_review": workbook_calcs_needs_review,
        "workbook_calcs_coverage_pct": workbook_calcs_coverage_pct,
        "needs_review_total": needs_review_total,
        "partitions_stubbed_total": partitions_stubbed_total,
        "columns_pruned_hidden_total": columns_pruned_hidden_total,
        "workbooks_total": len(wb_details),
        "workbooks_viz_built": wb_built,
        "workbooks_viz_warned": wb_warned,
        "workbooks_viz_error": wb_error,
        "workbooks_pbip_built": wb_pbip_built,
        "datasource_pbips_total": datasource_pbips_total,
        "datasource_pbips_built": datasource_pbips_built,
        "workbooks_multi_datasource": workbooks_multi_datasource,
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


def _dod_fail_reason(w):
    """A short, human reason a workbook produced no openable, model-bound report.

    Prefers the concrete viz/pbip signal already recorded on the workbook detail: a hard viz error,
    else the first ``pbip_warnings`` entry (with the ``manual attention required:`` prefix stripped),
    else a viz warning, else a generic fallback. Read-only; never raises.
    """
    if w.get("viz_status") == "error":
        return (w.get("note") or "viz rebuild failed").strip()
    wperr = w.get("pbip_write_error")
    if wperr and wperr.get("message"):
        return wperr["message"]
    for warn in (w.get("pbip_warnings") or []):
        if warn:
            return warn[len(_PBIP_WARN):] if warn.startswith(_PBIP_WARN) else warn
    if w.get("viz_status") == "warned":
        return (w.get("note") or "viz rebuild warned").strip()
    return "no openable, model-bound report was produced"


def _dod_warn_reasons(w):
    """Concise fidelity concerns for a workbook that built an openable ``.pbip`` but is NOT faithful.

    A built ``.pbip`` is not the same as a faithful one: a stubbed calculated field, a warned or
    reference-dropped visual, or a review-stub table partition all mean the report opens but
    under-represents the source. Surfacing these degrades the definition-of-done from PASS to WARN
    (soft; exit status unchanged) so a run never reports a green PASS while silently under-delivering.
    Read-only; never raises; returns ``[]`` for a clean, fully-faithful build.
    """
    reasons = []
    summary = (w.get("model_translation_handoff") or {}).get("summary") or {}
    needs_review = summary.get("needs_review") or 0
    if needs_review:
        reasons.append(f"{needs_review} calculated field(s) not faithfully translated (needs review)")
    warned = sum(1 for f in (w.get("viz_fidelity") or []) if (f or {}).get("status") == "warned")
    if warned:
        reasons.append(f"{warned} visual(s) rebuilt with warnings")
    drops = w.get("pbip_ref_drops") or []
    if drops:
        reasons.append(f"{len(drops)} visual(s) dropped a model reference")
    parts = w.get("partitions_needs_review") or []
    if parts:
        reasons.append(f"{len(parts)} table(s) landed as a needs-review partition stub")
    return reasons


def _pbir_page_count(pbip_dest):
    """Pages declared by the PBIR written under ``pbip_dest``, or ``None`` when unreadable.

    Reads the report's own ``pages.json`` ``pageOrder`` from disk, so the count reflects what
    actually landed rather than what the emitter meant to write. ``None`` (not ``0``) on any
    absent/malformed input, so an unreadable report never manufactures a false openability failure.
    """
    import glob

    try:
        hits = glob.glob(os.path.join(pbip_dest, "*.Report", "definition", "pages", "pages.json"))
        if not hits:
            return None
        with open(hits[0], encoding="utf-8-sig") as fh:
            order = json.load(fh).get("pageOrder")
        return len(order) if isinstance(order, list) else None
    except Exception:
        return None


def _dod_openability_failure(w):
    """A loud reason a workbook's bound ``.pbip`` is structurally NOT openable, or ``None``.

    Reads the ``openability_selfcheck`` (``{"ok", "checks", "issues"}``) recorded on the workbook detail
    (single-datasource path) AND on each ``datasource_pbips`` entry (consolidated path). A built ``.pbip``
    whose model fails the self-check (e.g. a duplicate column survived to TMDL, or M types a column no
    ``column`` declares) OPENS but will not load -- so this must fail the definition-of-done LOUD, not be
    softened to a fidelity warning. Returns the first failing check's concise detail, else ``None``.
    Read-only; tolerates a missing/malformed self-check (treated as no signal); never raises.
    """
    checks = [w.get("openability_selfcheck")]
    for e in (w.get("datasource_pbips") or []):
        checks.append((e or {}).get("openability_selfcheck"))
    for sc in checks:
        if not isinstance(sc, dict) or sc.get("ok") is not False:
            continue
        issues = sc.get("issues") or []
        first = issues[0] if issues and isinstance(issues[0], dict) else {}
        detail = first.get("detail")
        table = first.get("table") or first.get("part")
        if detail:
            return f"model is not openable: {detail}" + (f" (table {table})" if table else "")
        failed = [name for name, ok in (sc.get("checks") or {}).items() if ok is False]
        if failed:
            return "model is not openable: failed " + ", ".join(sorted(failed))
        return "model is not openable"
    # A visual that names a model object the model does not contain is the same class of defect on
    # the REPORT side: the visual renders EMPTY (or, for a conditional format, silently unpainted),
    # `powerbi-report-author validate` returns 0 errors, and the run reported success. The binding
    # lint has always DETECTED this (``viz_dangling_bindings``); it was softened to a fidelity
    # warning, so a workbook could ship with a visual bound to nothing and still be called done.
    #
    # Escalated only now that the corpus is clean: measured 4 dangling references across 3 of the 29
    # workbooks before 2.152.0 and 0 after, so this is INERT on green and can only fire on a real
    # regression. Landing it first would have taken the corpus gate 29/29 -> 26/29 and left every
    # later run measuring against a red baseline.
    dangling = w.get("viz_dangling_bindings") or {}
    problems = dangling.get("problems") if isinstance(dangling, dict) else None
    if isinstance(dangling, dict) and dangling.get("count"):
        first = (problems or ["(unnamed)"])[0]
        return ("report binds %d model object(s) that do not exist: %s"
                % (dangling["count"], str(first)[:220]))
    # A dangling ``SelectRef`` is the same class again, one level further in: a formatting property
    # points at a projection the visual does not declare, so the property resolves to nothing and
    # the visual renders with its DEFAULT colours -- validate returns 0 errors and the run reports
    # success. pbir_lint R8 has detected this since 2.176.0, but it only began running during an
    # actual migration in 2.190.0; until then it guarded the test suite. Escalated only now that it
    # both RUNS and is measured green: 0 of 29 workbooks report a lint problem, so this is inert on
    # green and can only fire on a real regression -- the same sequencing as the dangling-binding
    # escalation above (fix what fires, prove zero, THEN escalate).
    #
    # Which findings are fatal is owned by ``pbir_lint`` (``SILENT_RENDER_FINDINGS``) rather than
    # decided here, so a rule added there becomes fatal with no change on this side and the two can
    # never disagree about it.
    lint = w.get("viz_lint") or {}
    if isinstance(lint, dict) and lint.get("problems"):
        try:
            import pbir_lint as _pl
            fatal_marks = tuple(_pl.SILENT_RENDER_FINDINGS)
        except Exception:
            fatal_marks = ()
        for problem in lint.get("problems") or []:
            text = str(problem)
            if any(mark in text for mark in fatal_marks):
                return ("report is not faithfully bound: %s" % text[:260])
    # A page-less REPORT is the same class of defect on the other side of the project: Power BI
    # Desktop does not open it as an empty report, it throws ``TypeError: Cannot read properties of
    # undefined (reading 'visualContainers')`` and the whole project -- including the model that IS
    # correct -- becomes unreachable. So it fails LOUD rather than being softened to "N visual(s)
    # rebuilt with warnings", which is what let a crashing project report a green-ish warn.
    pages = w.get("pbip_page_count")
    if isinstance(pages, int) and pages <= 0:
        return ("report is not openable: it declares no pages -- Power BI Desktop crashes on a PBIR "
                "with an empty pageOrder rather than opening it empty")
    return None


def _definition_of_done(wb_details, pbip_enabled):
    """A machine definition-of-done ledger for workbook inputs (additive; never raises).

    A Tableau *workbook* migration is only complete when its dashboards are rebuilt and bound into an
    openable ``.pbip`` -- not when its semantic model alone lands. This classifies every workbook:

    - **pass** -- an openable, model-bound report was produced AND it is fully faithful (no stubbed
      calc, no warned/reference-dropped visual, no review-stub partition).
    - **warn** -- an openable, model-bound report was produced but with fidelity gaps that need review
      before the migration is trusted (see ``_dod_warn_reasons``). Soft: exit status is unchanged.
    - **skipped** -- either openable projects were disabled (``--no-pbip``), or the workbook connects
      to a *published* Tableau datasource that was not co-migrated in the same run (the one honest
      carve-out: its ``.tds`` must be in scope to bind an openable report).
    - **failed** -- a workbook that should have produced a bound report did not: either an orphaned
      report, a hard ``.pbip`` write failure (e.g. a Windows MAX_PATH violation, recorded as
      ``pbip_write_error`` and reported LOUD before the published carve-out so it is never masked), or a
      report bound to a structurally NON-OPENABLE model (the ``openability_selfcheck`` failed -- the
      ``.pbip`` opens but will not load; see ``_dod_openability_failure``), which fails LOUD ahead of
      the warn/pass branch so a green PASS is never reported over a model that will not open.

    The overall status is ``not_applicable`` (no workbook inputs), then by precedence ``failed`` (any
    failure -- the loud case) > ``warn`` (any fidelity gap) > ``pass`` (all clean) > ``skipped``.
    Purely a report key: it changes no behaviour and never alters exit status (soft-but-loud).
    """
    workbooks = []
    for w in wb_details:
        bound = (w.get("pbip_status") == "built") or any(
            (e or {}).get("pbip_status") == "built" for e in (w.get("datasource_pbips") or []))
        if not pbip_enabled:
            status = "skipped"
            reason = "openable .pbip projects disabled (--no-pbip)"
        elif bound:
            openability_fail = _dod_openability_failure(w)
            if openability_fail:
                # A report bound to a structurally non-openable model is a LOUD failure -- it opens but
                # will not load (e.g. a duplicate column survived to TMDL). Checked before warn/pass so
                # a run never reports a green PASS over a model that will not open.
                status, reason = "failed", openability_fail
            else:
                warn_reasons = _dod_warn_reasons(w)
                if warn_reasons:
                    status, reason = "warn", "; ".join(warn_reasons)
                else:
                    status, reason = "pass", ""
        elif w.get("pbip_write_error"):
            # A hard .pbip write failure (e.g. a Windows MAX_PATH violation) is a LOUD failure, checked
            # BEFORE the published carve-out so it is never mis-reported as a benign skip.
            status, reason = "failed", _dod_fail_reason(w)
        elif (w.get("binding_signal") or {}).get("kind") == "published":
            status = "skipped"
            reason = ("published-datasource workbook -- co-migrate its published datasource (.tds) "
                      "in the same run to bind an openable report")
        else:
            status, reason = "failed", _dod_fail_reason(w)
        workbooks.append({
            "workbook": w.get("name"),
            "report_bound": bool(bound),
            "bound_model": w.get("bound_model"),
            "pbip_folder": w.get("pbip_folder"),
            "status": status,
            "reason": reason,
        })

    reports_bound = sum(1 for e in workbooks if e["report_bound"])
    reports_failed = sum(1 for e in workbooks if e["status"] == "failed")
    reports_warned = sum(1 for e in workbooks if e["status"] == "warn")
    if not wb_details:
        overall = "not_applicable"
    elif reports_failed:
        overall = "failed"
    elif not pbip_enabled:
        overall = "skipped"
    elif reports_warned:
        overall = "warn"
    elif reports_bound:
        overall = "pass"
    else:
        overall = "skipped"
    return {
        "applicable": bool(wb_details),
        "status": overall,
        "workbooks_total": len(wb_details),
        "reports_bound": reports_bound,
        "reports_failed": reports_failed,
        "reports_warned": reports_warned,
        "workbooks": workbooks,
    }


def _dod_banner(dod):
    """Render the definition-of-done section for ``summary.md`` as a list of lines.

    Returns ``[]`` for a run with no workbook inputs, so a pure datasource run's summary head stays
    byte-identical. A ``failed`` run gets a loud banner naming each unbound workbook; a ``warn`` run
    gets a loud banner naming each low-fidelity workbook; ``pass`` and ``skipped`` get a one-line
    status. Emoji is safe here (``summary.md`` is written UTF-8).
    """
    if not dod or not dod.get("applicable"):
        return []
    status = dod.get("status")
    total = dod.get("workbooks_total", 0)
    if status == "failed":
        failed = [w for w in dod.get("workbooks", []) if w.get("status") == "failed"]
        out = [
            "## \u26d4 DEFINITION OF DONE: FAILED",
            "",
            (f"{len(failed)} of {total} workbook input(s) produced no openable, model-bound Power BI "
             "report. A Tableau workbook migration is not complete until its dashboards are rebuilt "
             "and bound into a `.pbip` (see the Workbooks table below)."),
            "",
        ]
        out += [f"- **{w.get('workbook')}** -- {w.get('reason')}" for w in failed]
        out.append("")
        return out
    if status == "warn":
        warned = [w for w in dod.get("workbooks", []) if w.get("status") == "warn"]
        out = [
            "## \u26a0\ufe0f DEFINITION OF DONE: WARN",
            "",
            (f"{len(warned)} of {total} workbook report(s) were rebuilt and bound into an openable "
             "`.pbip`, but with fidelity gaps that need review before the migration is trusted. The "
             "report opens, but under-represents the source until these are resolved (see the "
             "Workbooks table below)."),
            "",
        ]
        out += [f"- **{w.get('workbook')}** -- {w.get('reason')}" for w in warned]
        out.append("")
        return out
    if status == "pass":
        return [f"## \u2705 DEFINITION OF DONE: PASS -- {dod.get('reports_bound', 0)} of {total} "
                "workbook report(s) rebuilt and bound into an openable `.pbip`.", ""]
    return [f"## \u2139\ufe0f DEFINITION OF DONE: SKIPPED -- no workbook report was bound "
            "(see the Workbooks table for why).", ""]


def _pending_gates(summary):
    """Structured list of user-gated review offers still owed after the deterministic pass.

    A deterministic run can leave stubbed calculations (``needs_review_total``) or low-fidelity
    visuals (``visuals_warned``); each carries an explicit, user-gated follow-up offer -- the
    LLM-assisted **second compiler** and the Tier-3 **dashboard audit** respectively -- that MUST be
    *offered* before the migration is reported as done (the user may decline, which is a complete,
    honest outcome, but the offer is owed). Emitting the owed offers as a first-class artifact key
    means the report itself prevents a premature "complete" claim, rather than relying on the agent
    to remember ``SKILL.md`` steps 3 and 5. Additive: ``[]`` when nothing is owed, so a clean run's
    artifacts are unchanged.
    """
    gates = []
    needs_review = summary.get("needs_review_total", 0) or 0
    if needs_review > 0:
        gates.append({
            "gate": "second_compiler",
            "count": needs_review,
            "trigger": "summary.needs_review_total",
            "skill_step": 3,
            "runbook": "resources/second-compiler.md",
            "offer": (f"OFFER the LLM-assisted second compiler for {needs_review} stubbed "
                      "calculation(s) before declaring the migration done -- present them and run "
                      "only on an explicit GO. If declined, the deterministic result ships as-is "
                      "(each stub keeps its preserved TableauFormula)."),
        })
    warned = summary.get("visuals_warned", 0) or 0
    if warned > 0:
        gates.append({
            "gate": "dashboard_audit",
            "count": warned,
            "trigger": "summary.visuals_warned",
            "skill_step": 5,
            "runbook": "resources/dashboard-audit.md",
            "offer": (f"OFFER the LLM-assisted Tier-3 dashboard audit for {warned} warned "
                      "visual(s) before declaring the migration done -- present them and run only "
                      "on an explicit GO. If declined, the deterministic rebuild ships as-is (every "
                      "faithfully-bound visual intact). OFFER THE FORMATTING TOUCH-UP IN THE SAME "
                      "BREATH -- the two are independent, so the user may take either, both or "
                      "neither: \"Do you want the adjudication report, as well as a formatting "
                      "touch-up?\""),
        })
    # Layout polish is offered on EVERY rebuilt report, not only a warned one. Tableau lays a filter
    # band out with a layout-flow container and Power BI has only absolute rects, so the rebuild
    # recomputes what the container computed -- and small per-card differences accumulate into a
    # visibly ragged band even when every rect came from a faithful reading of the source. There has
    # never been an output that could not use polish, so the gate does not wait for a warning.
    #
    # ADDITIVE beside the audit, never a replacement: it runs only on its own explicit GO, rewrites
    # only ``position`` rects (so no field, filter, measure or visual type can move and no number can
    # change), and a run that declines it is byte-identical to one from before polish existed. It is
    # also proven-improving per page -- a page that would come out worse is restored untouched.
    if (summary.get("workbooks_pbip_built", 0) or 0) > 0:
        gates.append({
            "gate": "layout_polish",
            "count": summary.get("workbooks_pbip_built", 0) or 0,
            "trigger": "summary.workbooks_pbip_built",
            "skill_step": 5,
            "runbook": "resources/dashboard-audit.md",
            "offer": ("OFFER the Tier-3 FORMATTING TOUCH-UP alongside the adjudication report -- "
                      "run only on an explicit GO. It normalises each page's control bands "
                      "(uniform card size, aligned rows, even gutters, no band drawn over the "
                      "content below) against the source layout, and keeps a page's new geometry "
                      "ONLY when the measured defect count falls. Geometry only: no binding, no "
                      "number, nothing but position rects. Run: "
                      "py -3.11 \"$SKILL\\scripts\\polish_layout.py\" \"<...>.Report\" "
                      "(add --dry-run to measure first)."),
        })
    return gates


def _environment_banner(environment):
    """Machine-level blockers, ABOVE the gates banner and below the definition of done.

    Placed high deliberately: this is the one section that says the handover will not open on THIS
    BOX for a reason that has nothing to do with the migration. A user who reads it after opening
    the .pbip has already spent the time this exists to save.
    """
    findings = ((environment or {}).get("findings")) or []
    if not findings:
        return []
    lines = ["> [!WARNING]",
             "> **This machine will not open the output — and it is not the output's fault.**", ">"]
    for f in findings:
        lines.append("> - **%s**: %s" % (f.get("check"), f.get("detail")))
    lines.append("")
    return lines


def _pending_gates_banner(gates):
    """Render the loud 'not done until offered' section for ``summary.md``; ``[]`` when none owed."""
    if not gates:
        return []
    label = {"second_compiler": "Second compiler (stubbed calcs)",
             "dashboard_audit": "Tier-3 dashboard audit (warned visuals)",
             "layout_polish": "Tier-3 formatting touch-up (layout polish)"}
    out = [
        "## \u23f3 PENDING REVIEW GATES -- the migration is NOT done until these are offered",
        "",
        ("The deterministic pass completed, but it left follow-ups that each carry an explicit, "
         "user-gated offer. **Do not report the migration as complete until every offer below has "
         "been made** (the user may decline any of them -- that is a complete, honest outcome -- "
         "but the offer is owed):"),
        "",
    ]
    out += [f"- **{label.get(g['gate'], g['gate'])}** ({g['count']}) -- {g['offer']}" for g in gates]
    out.append("")
    return out


def _openable_outputs(report, output_dir):
    """Absolute, copy-pasteable paths to every openable ``.pbip`` the run produced (plus its project
    folder and sibling ``.Report`` / ``.SemanticModel``), so the caller can hand the user a REAL path
    instead of a bare filename or a ``pbip/<Name>/<Name>.pbip`` template. Additive: ``[]`` when no
    ``.pbip`` was built (e.g. ``--no-pbip``), so a suppressed-pbip run's report/summary head stays
    byte-identical. Never raises -- a path that can't be resolved or is absent on disk is skipped.

    ``report`` stores each ``pbip_folder`` as a run-relative path (``pbip/<Name>/<Name>.pbip``);
    joining it onto ``os.path.abspath(output_dir)`` yields the absolute path the user opens. Covers
    datasource projects, workbook projects, and the per-datasource projects nested under a
    multi-datasource workbook.
    """
    base_abs = os.path.abspath(output_dir)
    out, seen = [], set()

    def _add(name, kind, rel):
        if not rel:
            return
        pbip_abs = os.path.normpath(os.path.join(base_abs, rel))
        if pbip_abs in seen or not os.path.exists(pbip_abs):
            return
        seen.add(pbip_abs)
        proj = os.path.dirname(pbip_abs)
        entry = {"name": name or "(unnamed)", "kind": kind,
                 "pbip": pbip_abs, "project_folder": proj}
        try:
            for sub in sorted(os.listdir(proj)):
                full = os.path.join(proj, sub)
                if not os.path.isdir(full):
                    continue
                if sub.endswith(".Report") and "report_folder" not in entry:
                    entry["report_folder"] = full
                elif sub.endswith(".SemanticModel") and "model_folder" not in entry:
                    entry["model_folder"] = full
        except OSError:
            pass
        out.append(entry)

    for w in report.get("workbooks") or []:
        _add(w.get("name"), "workbook", w.get("pbip_folder"))
        for e in w.get("datasource_pbips") or []:
            base = w.get("name")
            label = f"{base} / {e.get('datasource')}" if base else e.get("datasource")
            _add(label, "workbook", e.get("pbip_folder"))
    for d in report.get("datasources") or []:
        _add(d.get("name"), "datasource", d.get("pbip_folder"))
    return out


def _openable_outputs_md(outs):
    """Render the top-of-summary 'Openable output(s)' section listing each absolute ``.pbip`` path;
    ``[]`` when none, so a ``--no-pbip`` run's summary head stays byte-identical."""
    if not outs:
        return []
    lines = [
        "## Openable output(s)",
        "",
        ("Open in Power BI Desktop by **double-clicking the `.pbip`** below (it is a small JSON "
         "pointer -- correct and complete; never zip it). Absolute paths, copy-pasteable as-is:"),
        "",
    ]
    lines += [f"- **{o['name']}** ({o['kind']}): `{o['pbip']}`" for o in outs]
    lines.append("")
    return lines


def _input_collision_banner(report):
    """Render a loud ``summary.md`` warning when the input folder was not staged clean.

    Two distinct signals, because they have different causes and different blast radii:

    * **Same asset NAME at two paths** -- the run may have migrated a different copy than intended.
    * **Same BYTES staged twice under different names** -- worse, because the scanner migrates BOTH
      copies and every count in the report doubles. A reader has no way to tell a doubled ledger from
      a real one.

    ``[]`` (byte-identical summary) whenever inputs were clean -- the overwhelming case. Not a
    definition-of-done gate and not fatal: it just makes a not-clean input folder impossible to miss.
    """
    manifest = report.get("input_manifest") or {}
    collisions = manifest.get("collisions") or []
    duplicates = manifest.get("duplicate_bytes") or []
    if not collisions and not duplicates:
        return []
    out = []
    if duplicates:
        out += [
            "## \u26a0\ufe0f INPUT IDENTITY WARNING -- the SAME FILE was staged more than once",
            "",
            ("Two or more input files are **byte-identical** (same SHA256) under different names, so "
             "this run migrated the same asset more than once and **every count below is inflated** "
             "-- workbooks, calculations, stubs and warned visuals are all multiplied by the number "
             "of copies. Do not read these totals as fact. Re-run with exactly one copy staged in a "
             "fresh, empty input folder. A common cause is a chat/portal download whose transfer-layer "
             "UUID prefix makes one copy *look* like a different asset."),
            "",
        ]
        for d in duplicates:
            out.append(f"- **{d['kind']}** sha256 `{d['sha256'][:16]}...` staged at:")
            out += [f"  - `{p}`" for p in d["paths"]]
        out.append("")
    if collisions:
        out += [
            "## \u26a0\ufe0f INPUT IDENTITY WARNING -- same asset name found at multiple paths",
            "",
            ("The input folder contained more than one file with the same asset name in different "
             "directories, so this run may have migrated a **different copy than you intended** (for "
             "example a stale file left over from a prior run). If you meant to migrate an exact file "
             "you attached, re-run with that file staged **alone** in a fresh, empty input folder. "
             "Every path and hash actually consumed is recorded in `input_manifest.json`."),
            "",
        ]
        for c in collisions:
            out.append(f"- **{c['stem']}** ({c['kind']}) found at:")
            out += [f"  - `{p}`" for p in c["paths"]]
        out.append("")
    return out


def _render_summary_md(report):
    """Render the human-readable ``summary.md`` from the report dict."""
    s = report["summary"]
    lines = [
        "# Tableau -> Fabric Estate Migration Report",
        "",
        f"_Generated {report['generated_at']} by `{report['tool']}` "
        f"from {report['source'].get('kind')}._",
        "",
        *_dod_banner(report.get("definition_of_done")),
        *_environment_banner(report.get("environment")),
        *_pending_gates_banner(report.get("pending_gates")),
        *_input_collision_banner(report),
        *_openable_outputs_md(report.get("openable_outputs")),
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
        *([f"- **Workbook calcs:** {s['workbook_calcs_total']} total -> "
           f"{s['workbook_calcs_translated']} translated, "
           f"{s['workbook_calcs_stubbed']} stubbed, "
           f"{s['workbook_calcs_needs_review']} need review "
           f"({s['workbook_calcs_coverage_pct']}% coverage)"]
          if s.get('workbook_calcs_total') else []),
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
            "`pbip/<Name>/<Name>.pbip` — double-click to explore and test it in Power BI Desktop. "
            "Each `.pbip` is a small (~300-byte) JSON **pointer** file — that is correct and complete; "
            "the report and model live in the sibling `.Report/` and `.SemanticModel/` folders. "
            "**Never zip or repackage a `.pbip`** (unlike `.pbix`/`.twbx`, it is not an archive). "
            "To confirm a bundle is healthy: "
            "`py -3.11 scripts/deploy_to_fabric.py --verify-pbip pbip/<Name>`.",
        ]

    review = [
        dict(r, datasource=d["name"])
        for d in report["datasources"]
        for r in ((d.get("translation_handoff") or {}).get("needs_review") or [])
    ]
    if review:
        lines += [
            "",
            "## Next step — second compiler (optional; offer to run)",
            "",
            f"{len(review)} calculation(s) fell back to inert stubs (the original Tableau formula is "
            "preserved). The second compiler is an **opt-in** stage: offer it to the user, then run it "
            "only on an explicit GO. If they decline, this deterministic result ships as-is. Once "
            "authorized: for each calc author a candidate DAX, validate it with "
            "`check_candidate_dax` (and the reconciliation oracle when data is landed), then land every "
            "validated candidate via `approved_calc_dax` and redeploy. Anything with no faithful DAX "
            "form stays an inert stub. See "
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
        lines += ["", "## Fallbacks (need a storage decision -- Import default / DirectLake opt-in)", ""]
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
            note = w.get("note") or ""
            entries = w.get("datasource_pbips")
            consolidated = w.get("consolidated_datasources") or []
            if entries:
                built = sum(1 for e in entries if e.get("pbip_status") == "built")
                note = (note + " " if note else "") + (
                    f"{len(entries)} datasources → {built} project(s) built, one per datasource")
            elif len(consolidated) > 1:
                note = (note + " " if note else "") + (
                    f"{len(consolidated)} datasources consolidated into one model")
            lines.append(
                f"| {w['name']} | {w.get('viz_status', '')} | {rebuilt}/{warned} "
                f"| {w.get('pbip_folder') or '-'} | {w.get('bound_model') or '-'} "
                f"| {note} |")
        # For multi-datasource workbooks, list each nested per-datasource project so the split is
        # explicit (a single PBIR report binds one model, so each datasource gets its own project).
        multi = [w for w in report["workbooks"] if w.get("datasource_pbips")]
        if multi:
            lines += ["", "### Per-datasource projects (multi-datasource workbooks)", ""]
            for w in multi:
                lines.append(f"- **{w['name']}**")
                for e in w["datasource_pbips"]:
                    tag = "primary" if e.get("is_primary") else "secondary"
                    where = e.get("pbip_folder") or f"skipped ({tag})"
                    lines.append(f"  - {e.get('datasource')} [{e.get('pbip_status')}]: {where}")
        if any(w.get("pbip_folder") for w in report["workbooks"]):
            lines += [
                "",
                "> **Open locally:** each rebuilt workbook with a bound model has a self-contained, "
                "openable Power BI project at `pbip/<Workbook>/<Workbook>.pbip` (report + a model "
                "rebuilt from the workbook's own embedded datasource) — double-click to open it in "
                "Power BI Desktop. A workbook with several embedded datasources instead gets one "
                "project per datasource nested at `pbip/<Workbook>/<Datasource>/` (a single report "
                "binds one model, so dashboards spanning datasources are split across them). The "
                "`semantic_models/` folders remain the canonical deploy target.",
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
        vc_workbooks = [w for w in report["workbooks"] if w.get("visual_calculations")]
        if vc_workbooks:
            vc_emitted = sum(w["visual_calculations"].get("emitted_total", 0)
                             for w in vc_workbooks)
            vc_review = sum(w["visual_calculations"].get("review_total", 0)
                            for w in vc_workbooks)
            lines += [
                "",
                f"> **Visual Calculations:** {vc_emitted} view-only quick table calc(s) across "
                f"{len(vc_workbooks)} workbook(s) were rebuilt as Power BI **Visual Calculations** — "
                "the report-layer twin of a Tableau quick table calc (RUNNINGSUM / MOVINGAVERAGE / "
                "RANK / PREVIOUS evaluated over the visual's own matrix axis), preserving the "
                "original Tableau addressing. "
                + (f"{vc_review} routed to review. " if vc_review else "")
                + "Per-worksheet family / axis / role detail is in `report.json` under each "
                "workbook's `visual_calculations`.",
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
def _load_storage_decisions(path, accept_recommended=False):
    """Load the operator's answers to ``needs-storage-decision`` (issue #116).

    ``needs-storage-decision`` was terminal on the batch path: the message correctly demanded a
    choice and there was nowhere to put the answer, so 37% of a real 38-workbook estate ended with
    no model and no report. This is the seam that carries the answer, mirroring ``--approved-dax``
    (which already supplies caller decisions per calc).

    The JSON maps a datasource caption/name to ``"Import"``, ``"DirectQuery"``, ``"DirectLake"`` or
    ``"recommended"``; the key ``"*"`` sets a default for every datasource that has no explicit
    entry. ``accept_recommended`` is the blanket opt-in (``--accept-recommended-storage``) and is
    exactly ``{"*": "recommended"}`` -- an explicit per-datasource entry still wins over it.

    Returns ``None`` when nothing was supplied, so the run is byte-identical to today's. Raises
    ``ValueError`` (fail-fast) on a missing/unreadable file or an unrecognised mode -- a typo must
    not silently reproduce the dead end the flag exists to resolve. Tolerates a UTF-8 BOM.
    """
    data = {}
    if path:
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise ValueError(f"--storage-decision file not found: {path}")
        except (OSError, ValueError) as exc:  # ValueError covers json.JSONDecodeError
            raise ValueError(f"--storage-decision file is not readable JSON ({path}): {exc}")
        if not isinstance(data, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            raise ValueError(
                "--storage-decision JSON must map datasource name -> one of Import, DirectQuery, "
                f'DirectLake, recommended (use "*" for a default) ({path})')
    out = {}
    for key, value in data.items():
        try:
            out[key.strip().lower()] = normalize_storage_decision(value)
        except ValueError as exc:
            raise ValueError(f"--storage-decision {key!r}: {exc}")
    if accept_recommended:
        out.setdefault("*", "recommended")
    return out or None


def _storage_decision_for(name, decisions):
    """The operator's answer for one datasource: its own entry, else the ``"*"`` default, else None."""
    if not decisions:
        return None
    return decisions.get((name or "").strip().lower()) or decisions.get("*")


def _load_approved_dax(path):
    """Load a mapping of human-approved assisted translations from a JSON file.

    Each value may be the flat ``"DAX"`` string form, or the additive dict form
    ``{"dax": "DAX", "table": "TargetTable"}`` -- the latter lets an approval also name a calc's
    home table (honored by the column-mode landing; not applicable to measures, which live in the
    shared ``_Measures`` table).

    Returns ``None`` when ``path`` is falsy (the run is then byte-identical to a no-approval run).
    Raises ``ValueError`` when the file is missing, unreadable, not JSON, or not an object of
    ``str -> (str | {"dax": str, "table"?: str})`` -- a fail-fast so a typo never silently drops an
    approval. Tolerates a UTF-8 BOM (the file is often hand-authored on Windows).
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

    def _valid_value(v):
        if isinstance(v, str):
            return True
        if isinstance(v, dict):
            tbl = v.get("table")
            return isinstance(v.get("dax"), str) and (tbl is None or isinstance(tbl, str))
        return False

    if not isinstance(data, dict) or not all(
            isinstance(k, str) and _valid_value(v) for k, v in data.items()):
        raise ValueError(
            "--approved-dax JSON must map calc name -> DAX string (or "
            '{"dax": ..., "table": ...}) ' f"({path})")
    return data or None


def _load_authored(path):
    """Load a ``{calc_name: dax_string}`` mapping of authored keystone DAX for the second-compiler
    pre-pass from a JSON file. Returns ``None`` when ``path`` is falsy. Raises ``ValueError`` (a
    fail-fast) when the file is missing, unreadable, not JSON, or not an object of ``str -> str`` --
    so a typo never silently drops a keystone. Tolerates a UTF-8 BOM."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ValueError(f"--author file not found: {path}")
    except (OSError, ValueError) as exc:  # ValueError covers json.JSONDecodeError
        raise ValueError(f"--author file is not readable JSON ({path}): {exc}")
    if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError(f"--author JSON must map calc name -> DAX string ({path})")
    return data or None


def scan_estate(source):
    """Read-only pre-build discovery -- the datasource-before-workbook gate.

    For every workbook in ``source``, report whether it binds to a PUBLISHED Tableau datasource (and
    name it), and flag any published datasource that is NOT yet present in the input scope. This lets
    the runbook fetch a workbook's published datasource FIRST, so the workbook is never built before
    its datasource is in scope (which would rebind to nothing and ship an empty report).

    Presence is computed with the SAME ``_norm_ds`` key the build uses to populate ``ds_catalog``
    (keyed by each datasource's file stem via :meth:`asset_name`), so ``datasource_present`` means
    exactly "the build will find it and rebind the workbook to it". No build, no network, no creds.

    Returns::

        {"datasources_present": [names],
         "workbooks": [{"name", "kind", "published_ds_name", "datasource_present"}],
         "missing_published_datasources": [names]}
    """
    ds_present = {}
    for ds_id in source.list_datasources():
        nm = source.asset_name(ds_id)
        ds_present[_norm_ds(nm)] = nm

    workbooks = []
    missing = {}
    for wb_id in source.list_workbooks():
        entry = {"name": source.asset_name(wb_id), "kind": None,
                 "published_ds_name": None, "datasource_present": None}
        try:
            signal = _workbook_binding_signal(source.read_workbook(wb_id), None)
        except Exception:
            signal = None
        if signal:
            entry["kind"] = signal.get("kind")
            pub = signal.get("published_ds_name")
            entry["published_ds_name"] = pub
            if signal.get("kind") == "published" and pub:
                present = _norm_ds(pub) in ds_present
                entry["datasource_present"] = present
                if not present:
                    missing[_norm_ds(pub)] = pub
        workbooks.append(entry)

    return {
        "datasources_present": sorted(ds_present.values()),
        "workbooks": workbooks,
        "missing_published_datasources": sorted(missing.values()),
    }


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
                        help="path to a JSON file of human-approved second-compiler "
                             "(assisted-translation) results, mapping calc name -> DAX string (or "
                             '{"dax": ..., "table": ...} to also name a calc column\'s home table); '
                             "each name-matching stub lands as a live, audit-stamped measure/calc "
                             "column instead of an inert stub")
    parser.add_argument("--viz-advice", action="store_true",
                        help="also write a reports/<Name>.viz-advice.json sidecar per workbook with "
                             "ranked alternative chart types per visual (Tier-2 viz advisor; "
                             "deterministic, additive, never alters the rebuilt PBIR)")
    parser.add_argument("--semantic-colours", "--semantic-colors", dest="semantic_colours",
                        action="store_true",
                        help="paint an UNAUTHORED two-member polarity colour domain "
                             "(negative/positive, loss/profit, fail/pass, ...) semantic red/green "
                             "instead of Tableau's default categorical ramp. Off by default: a "
                             "workbook that authors no palette still RENDERS in Tableau's own "
                             "colours, so the default reproduces the source rather than "
                             "reinterpreting it. An explicitly authored palette always wins over "
                             "both, with or without this flag")
    parser.add_argument("--second-compile", action="store_true",
                        help="turn on the SECOND-COMPILER landing pre-pass per workbook: land "
                             "keystone-dependent stub calcs as faithful, gated DAX (from the engine's "
                             "own idiom detectors + fix-point cascade) and feed them through the same "
                             "--approved-dax landing seam. Opt-in; the default run is byte-identical")
    parser.add_argument("--author", metavar="JSON",
                        help="path to a JSON file of authored keystone DAX (calc name -> DAX string) "
                             "for the second-compiler pre-pass; implies --second-compile. Each entry "
                             "is gate-checked and used to seed the cascade so its dependents land too")
    parser.add_argument("--scan", action="store_true",
                        help="PRE-BUILD DISCOVERY ONLY (no build): report each workbook's datasource "
                             "binding (embedded/published) and flag any PUBLISHED datasource not yet "
                             "in the input folder, so it can be fetched FIRST. Writes "
                             "<output>/scan.json. Exits non-zero when a published datasource is "
                             "missing (do not build until this exits 0).")
    parser.add_argument("--force", "--overwrite", action="store_true", dest="force",
                        help="build even if <output> already holds a prior report.json (overwrite "
                             "in place); the default is to STOP so a new run never silently mixes "
                             "with a previous run's stale outputs")
    parser.add_argument("--storage-decision", metavar="JSON",
                        help="path to a JSON file supplying the operator's answer to a "
                             "'needs-storage-decision' outcome, mapping datasource name -> one of "
                             "Import, DirectQuery, DirectLake, recommended (use \"*\" for a "
                             "default). Only an outcome that ASKED for a decision is overridden; a "
                             "mode the engine chose confidently is left alone, and a datasource "
                             "whose schema could not be read is refused (supply a connection "
                             "instead). DirectLake emits a landing plan, never data")
    parser.add_argument("--accept-recommended-storage", action="store_true",
                        help="blanket opt-in: apply each needs-storage-decision datasource's own "
                             "already-computed recommended_mode. Equivalent to "
                             '--storage-decision with {"*": "recommended"}; an explicit '
                             "per-datasource entry still wins")
    parser.add_argument("--layout", choices=("legacy", "solver"), default="solver",
                        help="dashboard zone-layout engine. 'solver' (default) resolves the whole "
                             "zone TREE, so sibling zones cannot overlap by construction and fewer "
                             "visuals are squashed to their minimum size; 'legacy' scales each "
                             "zone's absolute rect independently and repairs collisions afterwards. "
                             "'legacy' is also the per-zone fallback inside the solver, so it is "
                             "never fully bypassed -- pass it explicitly only to reproduce a "
                             "pre-solver migration.")
    args = parser.parse_args(argv)

    try:
        approved_calc_dax = _load_approved_dax(args.approved_dax)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        authored = _load_authored(args.author)
    except ValueError as exc:
        parser.error(str(exc))
    second_compile = bool(args.second_compile or authored)

    try:
        storage_decisions = _load_storage_decisions(args.storage_decision,
                                                    args.accept_recommended_storage)
    except ValueError as exc:
        parser.error(str(exc))

    # Fail fast on an input path that cannot possibly hold Tableau exports. Without this a typo'd
    # -i silently produced an EMPTY bundle and exited 0 -- "Bundle written to: ..." with 0/0
    # workbooks reads as success, so the mistake surfaces much later (or never). An input folder
    # that exists but holds no Tableau file is a different, recoverable case and still builds.
    if not os.path.isdir(args.input):
        what = "is not a directory" if os.path.exists(args.input) else "does not exist"
        parser.error(f"--input {os.path.abspath(args.input)} {what}. "
                     "Point -i at a folder of Tableau exports (.twb/.twbx/.tds/.tdsx).")

    source = LocalFilesSource(args.input)

    if args.scan:
        manifest = scan_estate(source)
        os.makedirs(args.output, exist_ok=True)
        scan_path = os.path.join(args.output, "scan.json")
        with open(scan_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        for wb in manifest["workbooks"]:
            if wb["kind"] == "published":
                state = "present" if wb["datasource_present"] else "MISSING"
                print(f"  {wb['name']}: published datasource "
                      f"{wb['published_ds_name']!r} [{state}]")
            else:
                print(f"  {wb['name']}: {wb['kind'] or 'no datasource detected'}")
        missing = manifest["missing_published_datasources"]
        if missing:
            print(f"[ACTION] Fetch these published datasource(s) into "
                  f"{os.path.abspath(args.input)} BEFORE building, then re-scan: {missing}")
            print('  e.g. python fetch_tds.py --datasource-name "<name>" '
                  "--include-extract --out <input folder>")
        else:
            print("[OK] All workbook datasources are in scope -- safe to build (STEP 2).")
        print(f"Scan manifest written to: {os.path.abspath(scan_path)}")
        return 1 if missing else 0

    # Fail-loud stale-output guard: refuse a FRESH build into an -o that already holds a prior
    # report.json, so a new migration never silently mixes with a previous run's outputs (the
    # AAR's stale-$RUN foot-gun, on the OUTPUT side; new_run.py fixes the input side). An
    # intentional re-run that LANDS calcs into the same bundle (--approved-dax / --author /
    # --second-compile -- the documented second-compiler loop) is exempt, as is an explicit
    # --force overwrite-in-place.
    prior_report = os.path.join(args.output, "report.json")
    landing_rerun = bool(args.approved_dax or second_compile)
    if not args.force and not landing_rerun and os.path.isfile(prior_report):
        print(f"[STOP] Refusing to build: {os.path.abspath(prior_report)} already exists -- "
              f"'{os.path.abspath(args.output)}' holds a prior migration's output.")
        print("       Building here would mix this run with a previous run's stale outputs. Point "
              "-o at a FRESH, empty folder")
        print(r'       (mint one with: py -3.11 "$SKILL\scripts\new_run.py" --root C:\tfmig), or '
              "pass --force to overwrite in place.")
        return 2

    report = migrate_estate(source, args.output, pbip=not args.no_pbip,
                            approved_calc_dax=approved_calc_dax, viz_advice=args.viz_advice,
                            second_compile=second_compile, authored=authored,
                            layout=args.layout, storage_decisions=storage_decisions,
                            semantic_colours=args.semantic_colours)
    s = report["summary"]
    print(
        f"Datasources: {s['datasources_migrated']}/{s['datasources_total']} migrated "
        f"({s['datasources_fallback']} fallback, {s['datasources_error']} error) | "
        f"Measures: {s['measures_translated']}/{s['measures_total']} translated | "
        f"Workbooks: {s['workbooks_viz_built']}/{s['workbooks_total']} viz built"
    )
    if s.get("workbook_calcs_total"):
        print(
            f"Workbook calcs: {s['workbook_calcs_translated']}/{s['workbook_calcs_total']} "
            f"translated ({s['workbook_calcs_coverage_pct']}% coverage), "
            f"{s['workbook_calcs_needs_review']} need review"
        )
    print(f"Bundle written to: {os.path.abspath(args.output)}")
    if not (s.get("workbooks_total") or s.get("datasources_total")):
        # An input folder with nothing Tableau-shaped in it. The bundle is valid but EMPTY, and
        # "Bundle written to: ..." on its own reads as success -- say plainly that nothing was
        # found, and name the extensions that would have been, so the mistake is caught here.
        print(f"[WARN] Nothing to migrate: no .twb/.twbx/.tds/.tdsx found under "
              f"{os.path.abspath(args.input)} (searched recursively). The bundle is empty.")
    openable = report.get("openable_outputs") or []
    if openable:
        # Emit REAL absolute paths automatically -- the agent should never have to be asked for the
        # output filepath, and can copy these straight to the user.
        print(f"Openable projects: {len(openable)} (double-click the .pbip in Power BI Desktop)")
        for o in openable:
            print(f"  {o['name']} ({o['kind']}): {o['pbip']}")
        # A project whose deepest file sits past the Windows MAX_PATH budget is written correctly
        # (the writer uses ``\\?\``) but Power BI Desktop CANNOT READ it -- it opens showing an empty
        # canvas, with every artifact present and correct on disk. Calling that "openable" on stdout
        # while the explanation sits buried in report.json is the worst of both worlds, so surface it
        # here, next to the claim it contradicts.
        _too_long = [w for wb in (report.get("workbooks") or [])
                     for w in (wb.get("pbip_warnings") or [])
                     if "MAX_PATH" in w]
        for w in _too_long:
            print(f"[WARN] {w}")
    if s.get("needs_review_total"):
        print(f"Next step: OFFER the second-compiler pass -- {s['needs_review_total']} calculation(s) "
              f"stubbed -> present them to the user and run the second compiler only on an explicit GO "
              f"(see summary.md 'Next step'); if declined, this deterministic result ships as-is. Land "
              f"any validated results by re-running with --approved-dax <file.json>.")
    if s.get("visuals_warned"):
        print(f"Next step: OFFER the Tier-3 dashboard audit -- {s['visuals_warned']} visual(s) warned "
              f"-> present them to the user and run the audit only on an explicit GO (see summary.md "
              f"'PENDING REVIEW GATES' and resources/dashboard-audit.md); if declined, this "
              f"deterministic rebuild ships as-is. Do NOT report the migration as done until this and "
              f"any second-compiler offer above have been made.")
    if s.get("partitions_stubbed_total"):
        print(f"Next step: {s['partitions_stubbed_total']} table partition(s) need manual M "
              f"completion -> see summary.md ('manual M partition completion'); the original SQL "
              f"is preserved in report.json.")
    dod = report.get("definition_of_done") or {}
    if dod.get("applicable"):
        # ASCII markers only -- Windows cp1252 stdout raises on emoji. Soft-but-loud: exit stays 0.
        marker = {"failed": "[FAIL]", "pass": "[OK]", "warn": "[WARN]",
                  "skipped": "[--]"}.get(dod.get("status"), "[--]")
        print(f"{marker} Definition of done: {dod.get('status')} -- {dod.get('reports_bound', 0)}/"
              f"{dod.get('workbooks_total', 0)} workbook report(s) rebuilt and bound "
              f"(see summary.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
