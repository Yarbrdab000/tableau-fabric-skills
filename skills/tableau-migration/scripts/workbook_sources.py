"""Rebuild a Tableau workbook's INLINE ``<datasource>`` definitions into Fabric
semantic models (offline, stdlib-only).

The v1 spine (``parse_tds`` -> ``select_storage_mode`` -> ``assemble_import_model``)
rebuilds *standalone* published ``.tds`` / ``.tdsx`` files. A real workbook -- including
the canonical Tableau "Superstore" sample -- instead **embeds** its datasources inline:
``twb_to_pbir`` reads those embedded ``<datasource>`` blocks for binding names only and
never turns them into models, so a generated report's field bindings dangle. This module
closes that gap by enumerating the embedded datasources and rebuilding each one into a
deployable ``<caption>.SemanticModel`` definition that the report can bind to.

It binds ONLY to the existing public pipeline APIs and never re-implements (or edits) the
connection / storage-mode / TMDL cores:

* ``connection_to_m.parse_tds`` / ``escape_m_string``
* ``storage_mode.select_storage_mode``
* ``assemble_model.assemble_import_model`` / ``write_model_folder``
* ``tmdl_generate`` public generators (``clean_col``, ``generate_column_tmdl``, ``q``,
  ``generate_database_tmdl``, ``generate_pbism``, ``generate_platform``,
  ``generate_measures_table_tmdl``)

The one capability the cores do not expose -- a *deploy-shaped* Excel/CSV ``= m`` partition
(the flat-file branch of the spine only emits a ``null`` scaffold) -- is implemented here,
without touching the connector module.

Classification of each embedded ``<datasource>`` is by its inner ``<connection class>``:

* ``sqlproxy``  -> PUBLISHED REFERENCE. Not embedded data; the real definition lives on a
  Tableau Server / Cloud. Marked ``published_unresolved`` with the referenced name as a
  follow-up, and an optional ``resolve_published(name) -> tds_text`` seam: when it returns a
  ``.tds``, that text is reclassified and rebuilt through the normal logic (no network here).
* ``excel-direct`` / ``textscan`` -> EMBEDDED FLAT FILE. Rebuilt as an Import model whose
  partition M is a real ``Excel.Workbook`` / ``Csv.Document`` expression driven by a
  ``FilePath`` parameter (defaulting to the original path), with typed columns from metadata.
* ``ogrdirect`` (spatial) -> recognized but not auto-emitted (no doc-verified spatial M);
  routed to ``fallback`` with a follow-up rather than a guessed partition.
* anything relational (``sqlserver`` / ``snowflake`` / ``postgres`` / ...) -> rebuilt via the
  existing spine (slice the element -> ``parse_tds`` -> ``assemble_import_model``).

Honesty boundaries are inherited from the cores: column types come from Tableau metadata,
structurally unsafe shapes (join/union trees, multi-connection datasources) fall back instead
of being emitted wrong, and credentials are never read, stored, or written.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

try:  # works whether imported as a package or run with scripts/ on sys.path
    from .connection_to_m import parse_tds, escape_m_string
    from .storage_mode import select_storage_mode
    from .assemble_model import assemble_import_model, write_model_folder
    from .tmdl_generate import (
        clean_col, generate_column_tmdl, q,
        generate_database_tmdl, generate_pbism, generate_platform,
        generate_measures_table_tmdl)
except ImportError:
    from connection_to_m import parse_tds, escape_m_string
    from storage_mode import select_storage_mode
    from assemble_model import assemble_import_model, write_model_folder
    from tmdl_generate import (
        clean_col, generate_column_tmdl, q,
        generate_database_tmdl, generate_pbism, generate_platform,
        generate_measures_table_tmdl)


# -- connection-class taxonomy -------------------------------------------------
EXCEL_CLASSES = {"excel-direct", "excel"}
CSV_CLASSES = {"textscan", "csv"}
SPATIAL_CLASSES = {"ogrdirect", "spatial"}
PUBLISHED_CLASSES = {"sqlproxy"}

# Tableau metadata TMDL dataType -> Power Query M ascribed type (for the partition's
# Table.TransformColumnTypes step). Unknown types are left untyped rather than guessed.
_M_TYPE = {
    "int64": "Int64.Type",
    "double": "type number",
    "decimal": "type number",
    "string": "type text",
    "boolean": "type logical",
    "dateTime": "type datetime",
    "date": "type date",
}

# Tableau ``charset`` -> Power Query ``Csv.Document`` Encoding code page. Anything not
# recognized omits the Encoding option (let Power Query default) rather than emit a wrong one.
_CODEPAGE = {
    "utf-8": 65001, "utf8": 65001, "utf-8-sig": 65001,
    "windows-1252": 1252, "cp1252": 1252, "ansi": 1252,
    "iso-8859-1": 28591, "latin1": 28591, "latin-1": 28591,
    "utf-16": 1200, "utf-16le": 1200, "ucs-2": 1200, "utf-16be": 1201,
}

_INVALID_FS = re.compile(r'[\\/:*?"<>|]+')

_FILEPATH_META = 'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'


# -- XML helpers (namespace-agnostic) -----------------------------------------
def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _findall_local(elem, name):
    return [c for c in elem.iter() if _local(c.tag) == name]


def _children_local(elem, name):
    return [c for c in list(elem) if _local(c.tag) == name]


def _inner_connection(ds):
    """Return ``(class, connection_element)`` for the datasource's real inner connection.

    Descends a ``federated`` wrapper into its ``<named-connection>`` child, mirroring how the
    connector core resolves the live class; falls back to a direct non-federated ``<connection>``
    (the layout flat files and ``sqlproxy`` use). Returns ``(None, None)`` when none is found.
    """
    for holder in _findall_local(ds, "named-connections"):
        for nc in _children_local(holder, "named-connection"):
            for conn in _children_local(nc, "connection"):
                cls = (conn.get("class") or "").lower()
                if cls:
                    return cls, conn
    for conn in _children_local(ds, "connection"):
        cls = (conn.get("class") or "").lower()
        if cls and cls != "federated":
            return cls, conn
    # federated wrapper with no <named-connections> block: descend one level
    for conn in _children_local(ds, "connection"):
        for sub in _children_local(conn, "connection"):
            cls = (sub.get("class") or "").lower()
            if cls:
                return cls, sub
    return None, None


def _classify(connection_class):
    """Map an inner connection class to a coarse handling category."""
    cls = connection_class or ""
    if cls in PUBLISHED_CLASSES:
        return "published_reference"
    if cls in EXCEL_CLASSES or cls in CSV_CLASSES:
        return "flat_file"
    if cls in SPATIAL_CLASSES:
        return "spatial"
    return "relational"


def _file_type(connection_class):
    if connection_class in EXCEL_CLASSES:
        return "Excel"
    if connection_class in CSV_CLASSES:
        return "CSV"
    if connection_class in SPATIAL_CLASSES:
        return "Spatial"
    return None


# -- source loading ------------------------------------------------------------
def _looks_like_xml(text):
    return isinstance(text, str) and text.lstrip("\ufeff \t\r\n").startswith("<")


def _read_text_member(zf, member):
    with zf.open(member) as fh:
        return fh.read().decode("utf-8-sig")


def _pick_inner_member(names, suffix):
    """Deterministically choose the inner ``.twb`` / ``.tds`` member of a package."""
    matches = sorted(n for n in names if n.lower().endswith(suffix))
    return matches[0] if matches else None


def _load_source(source):
    """Resolve ``source`` (a path or raw XML text) to ``(xml_text, package_path)``.

    ``package_path`` is the ``.twbx`` / ``.tdsx`` zip path when the source is packaged (so
    packaged data files can be inspected for CSV delimiter / column counts), else ``None``.
    Workbook/datasource XML is decoded with ``utf-8-sig`` so the Tableau BOM is stripped.
    """
    if _looks_like_xml(source):
        return source, None
    path = str(source)
    if not os.path.exists(path):
        raise FileNotFoundError(f"workbook source not found: {path}")
    lower = path.lower()
    if lower.endswith((".twbx", ".tdsx", ".zip")):
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            member = _pick_inner_member(names, ".twb") or _pick_inner_member(names, ".tds")
            if not member:
                raise ValueError(f"no .twb/.tds entry inside package: {path}")
            return _read_text_member(zf, member), path
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8-sig"), None


def _read_packaged_header(package_path, directory, filename):
    """Return the first text line of a packaged data file, or ``None`` if unavailable."""
    if not package_path or not filename:
        return None
    try:
        with zipfile.ZipFile(package_path, "r") as zf:
            target = None
            wanted = filename.replace("\\", "/").split("/")[-1].lower()
            for name in zf.namelist():
                if name.replace("\\", "/").split("/")[-1].lower() == wanted:
                    target = name
                    break
            if target is None:
                return None
            with zf.open(target) as fh:
                raw = fh.readline()
            return raw.decode("utf-8-sig", errors="replace").rstrip("\r\n")
    except (zipfile.BadZipFile, OSError, KeyError):
        return None


# -- enumeration ---------------------------------------------------------------
def _is_real_datasource(ds, connection_class):
    """A real, rebuildable datasource: not the ``Parameters`` pseudo-source and connected."""
    name = (ds.get("name") or "").strip()
    if name.lower() == "parameters":
        return False
    if (ds.get("hasconnection") or "").lower() == "false":
        return False
    for conn in _children_local(ds, "connection"):
        if (conn.get("hasconnection") or "").lower() == "false":
            return False
    return connection_class is not None


def _datasource_elements(root):
    holders = _children_local(root, "datasources")
    out = []
    for h in holders:
        out.extend(_children_local(h, "datasource"))
    if not out and _local(root.tag) == "datasource":
        out = [root]
    return out


def enumerate_workbook_datasources(source):
    """Enumerate a workbook's embedded ``<datasource>`` definitions.

    ``source`` is a path to a ``.twb`` / ``.twbx`` / ``.tds`` / ``.tdsx`` file OR raw XML text.
    Returns one entry per REAL datasource (the ``Parameters`` pseudo-datasource and any
    ``hasconnection='false'`` block are skipped). Each entry is JSON-serializable and captures::

        {
          "name": <internal datasource name>,
          "caption": <caption or name>,
          "connection_class": <inner connection class>,
          "classification": "flat_file"|"published_reference"|"relational"|"spatial",
          "file_type": "Excel"|"CSV"|"Spatial"|None,
          "relations": [ {kind, name, item, columns:[{remote_name, model_name, tmdl_type}]} ],
          "published": {referenced_name, server, site, port, channel} | None,
          "flat_file": {filename, directory, separator, charset} | None,
          "datasource_xml": <the sliced <datasource> element as text>,
          "package_path": <.twbx/.tdsx path> | None,
          "parse_error": <str> | None,
        }

    The ``relations`` are produced by the shared ``parse_tds`` core (so column names/types are
    identical to the spine); a per-datasource parse failure is isolated into ``parse_error`` and
    leaves ``relations`` empty rather than aborting the enumeration.
    """
    xml_text, package_path = _load_source(source)
    root = ET.fromstring(xml_text)
    entries = []
    for ds in _datasource_elements(root):
        connection_class, conn = _inner_connection(ds)
        if not _is_real_datasource(ds, connection_class):
            continue
        name = ds.get("name")
        caption = ds.get("caption") or name or connection_class
        sliced = ET.tostring(ds, encoding="unicode")
        classification = _classify(connection_class)

        relations, parse_error = [], None
        try:
            descriptor = parse_tds(sliced)
            relations = _serialize_relations(descriptor)
        except Exception as exc:  # malformed slice -> isolate, keep enumerating
            parse_error = f"{type(exc).__name__}: {exc}"

        published = None
        if classification == "published_reference" and conn is not None:
            published = {
                "referenced_name": conn.get("server-ds-friendly-name") or caption,
                "server": conn.get("server"),
                "site": conn.get("dbname"),
                "port": conn.get("port") or "443",
                "channel": conn.get("channel") or "https",
            }

        flat_file = None
        if classification == "flat_file" and conn is not None:
            flat_file = {
                "filename": conn.get("filename"),
                "directory": conn.get("directory"),
                "separator": conn.get("separator"),
                "charset": conn.get("charset"),
            }

        entries.append({
            "name": name,
            "caption": caption,
            "connection_class": connection_class,
            "classification": classification,
            "file_type": _file_type(connection_class),
            "relations": relations,
            "published": published,
            "flat_file": flat_file,
            "datasource_xml": sliced,
            "package_path": package_path,
            "parse_error": parse_error,
        })
    return entries


def _serialize_relations(descriptor):
    out = []
    for rel in descriptor.get("relations", []):
        out.append({
            "kind": rel.get("kind"),
            "name": rel.get("name"),
            "item": rel.get("item"),
            "columns": [
                {"remote_name": c["remote_name"], "model_name": c["model_name"],
                 "tmdl_type": c["tmdl_type"]}
                for c in rel.get("columns", [])
            ],
        })
    return out


# -- flat-file M emission (the capability the spine does not expose) -----------
def _join_original_path(directory, filename):
    directory = (directory or "").strip()
    filename = (filename or "").strip()
    if directory and filename:
        sep = "" if directory[-1] in ("\\", "/") else "\\"
        return f"{directory}{sep}{filename}"
    return filename or directory


def _normalize_delimiter(separator, header):
    """Return ``(m_delimiter_literal, raw_delimiter)`` for a CSV source.

    Honors an explicit Tableau ``separator`` first; otherwise auto-detects from a packaged
    header line; otherwise defaults to a comma. Tabs are emitted as the M escape ``#(tab)``.
    """
    raw = separator
    if not raw and header:
        raw = _sniff_delimiter(header)
    if not raw:
        raw = ","
    if raw in ("\t", "\\t", "tab"):
        return "#(tab)", "\t"
    return raw, raw


def _sniff_delimiter(header):
    try:
        return csv.Sniffer().sniff(header, delimiters=",;\t|").delimiter
    except (csv.Error, TypeError):
        counts = {d: header.count(d) for d in (",", ";", "\t", "|")}
        best = max(counts, key=counts.get)
        return best if counts[best] else ","


def _codepage(charset):
    return _CODEPAGE.get((charset or "").lower().strip())


def _column_count(rel, header, raw_delimiter):
    if header:
        n = len(header.split(raw_delimiter))
        if n > 0:
            return n
    return len(rel.get("columns") or []) or None


def _rename_pairs(columns):
    return ", ".join(
        '{{"{0}", "{1}"}}'.format(escape_m_string(c["remote_name"]),
                                  escape_m_string(c["model_name"]))
        for c in columns)


def _type_pairs(columns):
    pairs = []
    for c in columns:
        mtype = _M_TYPE.get(c["tmdl_type"])
        if mtype:
            pairs.append('{{"{0}", {1}}}'.format(escape_m_string(c["model_name"]), mtype))
    return ", ".join(pairs)


def _excel_partition_source(rel):
    columns = rel["columns"]
    item = (rel.get("item") or rel.get("name") or "Sheet1").strip()
    if item.endswith("$"):
        nav_name, kind = item[:-1], "Sheet"
    else:
        nav_name, kind = item, "Table"
    steps = [
        "let",
        '\t\t\t\tSource = Excel.Workbook(File.Contents(#"FilePath"), null, true),',
        '\t\t\t\tNavigation = Source{{[Item="{0}", Kind="{1}"]}}[Data],'.format(
            escape_m_string(nav_name), kind),
        "\t\t\t\tPromoted = Table.PromoteHeaders(Navigation, [PromoteAllScalars=true]),",
        "\t\t\t\tRenamed = Table.RenameColumns(Promoted, {{{0}}}, MissingField.Ignore),".format(
            _rename_pairs(columns)),
        "\t\t\t\tData = Table.TransformColumnTypes(Renamed, {{{0}}})".format(
            _type_pairs(columns)),
        "\t\t\tin",
        "\t\t\t\tData",
    ]
    return "\n".join(steps)


def _csv_partition_source(rel, *, m_delimiter, codepage, column_count):
    columns = rel["columns"]
    opts = ['Delimiter="{0}"'.format(escape_m_string(m_delimiter))]
    if column_count:
        opts.append("Columns={0}".format(column_count))
    if codepage:
        opts.append("Encoding={0}".format(codepage))
    opts.append("QuoteStyle=QuoteStyle.Csv")
    steps = [
        "let",
        '\t\t\t\tSource = Csv.Document(File.Contents(#"FilePath"), [{0}]),'.format(", ".join(opts)),
        "\t\t\t\tPromoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),",
        "\t\t\t\tRenamed = Table.RenameColumns(Promoted, {{{0}}}, MissingField.Ignore),".format(
            _rename_pairs(columns)),
        "\t\t\t\tData = Table.TransformColumnTypes(Renamed, {{{0}}})".format(
            _type_pairs(columns)),
        "\t\t\tin",
        "\t\t\t\tData",
    ]
    return "\n".join(steps)


def _flatfile_table_tmdl(rel, source_body):
    """Render one ``table`` TMDL block (typed columns + real ``= m`` Import partition).

    The column/partition layout matches the spine's ``emit_table_tmdl_m`` exactly, so a
    flat-file model is byte-shaped like a relational one; only the partition source differs.
    """
    columns = rel["columns"]
    display = rel.get("name") or rel.get("item") or "Table"
    columns_tmdl = ""
    for c in columns:
        summarize = "sum" if c["tmdl_type"] in ("int64", "double", "decimal") else "none"
        columns_tmdl += generate_column_tmdl(c["model_name"], c["tmdl_type"], summarize, False)
    partition_name = rel.get("item") or clean_col(display)
    return (
        f"table {q(display)}\n"
        f"{columns_tmdl}\n"
        f"\tpartition {q(partition_name)} = m\n"
        f"\t\tmode: import\n"
        f"\t\tsource =\n"
        f"\t\t\t{source_body}\n\n"
    )


def _model_tmdl_import(table_names, expression_names):
    """A minimal valid Import ``model.tmdl`` (the spine's Import header is private)."""
    refs = "\n".join(f"ref table {q(t)}" for t in table_names)
    query_order = ""
    if expression_names:
        items = ",".join(f'"{n}"' for n in expression_names)
        query_order = f"annotation PBI_QueryOrder = [{items}]\n\n"
    return (
        "model Model\n"
        "\tculture: en-US\n"
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tsourceQueryCulture: en-US\n"
        "\tdataAccessOptions\n"
        "\t\tlegacyRedirects\n"
        "\t\treturnErrorValuesAsNull\n\n"
        f"{query_order}"
        "annotation __PBI_TimeIntelligenceEnabled = 0\n\n"
        f"{refs}\n"
    )


def _collision(rel):
    """Return a (model_name, [remote,...]) collision if two remote names share a clean name."""
    by_model = {}
    for c in rel["columns"]:
        by_model.setdefault(c["model_name"], set()).add(c["remote_name"])
    for model_name, remotes in by_model.items():
        if len(remotes) > 1:
            return model_name, sorted(remotes)
    return None


def _flatfile_tables(descriptor):
    return [r for r in descriptor.get("relations", [])
            if r["kind"] in ("table", "custom_sql") and r.get("columns")]


def build_flatfile_model(descriptor, *, model_name, connection_class, flat_file,
                         package_path=None):
    """Build a complete Import ``SemanticModel`` definition for an embedded flat file.

    Returns ``{"status", "parts", "report", "followups"}``. ``status`` is
    ``migrated_with_followups`` on success (the ``FilePath`` parameter still needs repointing),
    or ``fallback`` when the shape is not safely rebuildable (structurally unsupported per the
    storage policy, no typed columns, or a clean-name column collision).
    """
    caption = model_name
    decision = select_storage_mode(descriptor)
    base_followups = list(decision.get("manual_followups", []))

    def _fallback(reason):
        return {"status": "fallback", "parts": {}, "followups": base_followups,
                "report": {"model_name": caption, "reason": reason,
                           "storage_decision": decision}}

    if decision.get("mode") is None:
        return _fallback(decision.get("rationale") or "structurally unsupported for direct rebuild")

    tables = _flatfile_tables(descriptor)
    if not tables:
        return _fallback("no table produced typed columns from the workbook metadata")
    for rel in tables:
        clash = _collision(rel)
        if clash:
            return _fallback(
                f"columns {clash[1]} both clean to '{clash[0]}' in table "
                f"'{rel.get('name') or rel.get('item')}'; cannot emit an unambiguous model")

    flat_file = flat_file or {}
    directory = flat_file.get("directory")
    filename = flat_file.get("filename")
    default_path = _join_original_path(directory, filename)

    header = _read_packaged_header(package_path, directory, filename) if package_path else None

    parts = {}
    table_names = []
    used_files = set()
    for rel in tables:
        if connection_class in CSV_CLASSES:
            m_delim, raw_delim = _normalize_delimiter(flat_file.get("separator"), header)
            source_body = _csv_partition_source(
                rel, m_delimiter=m_delim, codepage=_codepage(flat_file.get("charset")),
                column_count=_column_count(rel, header, raw_delim))
        else:
            source_body = _excel_partition_source(rel)
        display = rel.get("name") or rel.get("item") or "Table"
        table_names.append(display)
        parts[f"definition/tables/{_safe_part(display, used_files)}.tmdl"] = \
            _flatfile_table_tmdl(rel, source_body)

    parts["definition/tables/_Measures.tmdl"] = generate_measures_table_tmdl("")
    parts["definition/expressions.tmdl"] = (
        f'expression FilePath = "{escape_m_string(default_path)}" {_FILEPATH_META}\n')
    parts["definition/model.tmdl"] = _model_tmdl_import(
        table_names + ["_Measures"], ["FilePath"])
    parts["definition/database.tmdl"] = generate_database_tmdl()
    parts["definition.pbism"] = generate_pbism()
    parts[".platform"] = generate_platform(caption)

    repoint = (f'Repoint the FilePath parameter for "{caption}" at the real '
               f'{_file_type(connection_class) or "data"} file '
               f'(or a OneLake / Lakehouse path); the embedded Tableau path '
               f'"{default_path}" will not exist in Fabric.')
    followups = base_followups + [repoint]
    report = {
        "model_name": caption,
        "storage_mode": "Import",
        "storage_decision": decision,
        "tables": table_names,
        "column_count": sum(len(r["columns"]) for r in tables),
        "file_type": _file_type(connection_class),
        "default_file_path": default_path,
    }
    return {"status": "migrated_with_followups", "parts": parts,
            "report": report, "followups": followups}


def _safe_part(name, used):
    base = _INVALID_FS.sub("_", name or "").strip().rstrip(".") or "Table"
    candidate, i = base, 2
    while candidate.lower() in used:
        candidate, i = f"{base}_{i}", i + 1
    used.add(candidate.lower())
    return candidate


# -- relational rebuild (delegates to the spine) ------------------------------
def build_relational_model(descriptor, *, model_name):
    """Rebuild a relational embedded datasource through the existing spine.

    Returns ``{"status", "parts", "report", "followups"}``. ``ValueError`` from the assembler
    (storage policy says land-to-Delta, or no typed columns) becomes a ``fallback``.
    """
    try:
        out = assemble_import_model(descriptor, model_name=model_name)
    except ValueError as exc:
        decision = select_storage_mode(descriptor)
        return {"status": "fallback", "parts": {}, "followups": decision.get("manual_followups", []),
                "report": {"model_name": model_name, "reason": str(exc),
                           "storage_decision": decision}}
    decision = out["report"].get("storage_decision", {})
    status = "migrated" if decision.get("fully_supported") else "migrated_with_followups"
    return {"status": status, "parts": out["parts"], "report": out["report"],
            "followups": list(decision.get("manual_followups", []))}


# -- per-datasource dispatch ---------------------------------------------------
def _rebuild_from_xml(datasource_xml, *, model_name, package_path=None, flat_file=None):
    """Parse a single ``<datasource>`` slice and dispatch it to the right builder.

    Shared by the relational path and the resolved-published path so a published ``.tds`` that
    turns out to be a flat file is rebuilt with real Excel/CSV M, not a null scaffold.
    """
    descriptor = parse_tds(datasource_xml)
    connection_class = (descriptor.get("connection_class") or "").lower()
    classification = _classify(connection_class)
    if classification == "flat_file":
        if flat_file is None:
            flat_file = _flat_file_attrs(datasource_xml)
        return build_flatfile_model(
            descriptor, model_name=model_name, connection_class=connection_class,
            flat_file=flat_file, package_path=package_path)
    if classification == "spatial":
        return {"status": "fallback", "parts": {}, "report": {"model_name": model_name},
                "followups": [f'Spatial source ({connection_class}) for "{model_name}" has no '
                              f"auto-emitted M; rebuild it manually (e.g. via a Shapefile / GeoJSON "
                              f"connector) after landing the geometry."]}
    return build_relational_model(descriptor, model_name=model_name)


def _flat_file_attrs(datasource_xml):
    ds = ET.fromstring(datasource_xml)
    _, conn = _inner_connection(ds)
    if conn is None:
        return {}
    return {"filename": conn.get("filename"), "directory": conn.get("directory"),
            "separator": conn.get("separator"), "charset": conn.get("charset")}


# -- orchestration -------------------------------------------------------------
def _safe_folder(name, used):
    base = _INVALID_FS.sub("_", name or "").strip().rstrip(".") or "datasource"
    candidate, i = base, 2
    while candidate.lower() in used:
        candidate, i = f"{base}_{i}", i + 1
    used.add(candidate.lower())
    return candidate


def rebuild_workbook_models(source, *, resolve_published=None, output_dir=None):
    """Rebuild every embedded datasource in a workbook into a semantic-model definition.

    ``source`` is a path or raw XML (see :func:`enumerate_workbook_datasources`).
    ``resolve_published`` is an optional ``callable(referenced_name) -> tds_text | None`` seam
    used to resolve ``sqlproxy`` published references (e.g. a local ``.tds`` cache or a live
    Tableau fetch); when it returns a ``.tds``, that definition is reclassified and rebuilt. No
    network is performed by this module. When ``output_dir`` is given, each model is also written
    to ``<output_dir>/<caption>.SemanticModel`` via ``write_model_folder``.

    Returns::

        {
          "models": { <caption>: {parts, report, status, classification, connection_class,
                                  folder, followups} },
          "followups": [ {"datasource", "message"} ],      # flat, across all datasources
          "datasources": [ <enumeration entry minus datasource_xml> ],
        }

    Statuses: ``migrated`` / ``migrated_with_followups`` / ``fallback`` / ``error`` mirror the
    estate orchestrator; ``published_unresolved`` additionally marks a published reference with
    no resolver (or one that returned nothing). Each datasource is processed under its own
    try/except, so one bad source never aborts the rest.
    """
    entries = enumerate_workbook_datasources(source)
    _attach_resolver(entries, resolve_published)
    models = {}
    followups = []
    used_folders = set()

    for entry in entries:
        caption = entry["caption"] or entry["name"] or entry["connection_class"]
        folder = _safe_folder(caption, used_folders)
        record = {
            "classification": entry["classification"],
            "connection_class": entry["connection_class"],
            "folder": f"{folder}.SemanticModel",
            "parts": {},
            "report": {},
            "followups": [],
        }
        try:
            result = _rebuild_entry(entry, caption)
        except Exception as exc:  # per-source isolation: never abort the estate
            result = {"status": "error", "parts": {}, "report": {},
                      "followups": [f"{type(exc).__name__}: {exc}"],
                      "error": f"{type(exc).__name__}: {exc}"}
        record.update(result)

        if output_dir and record["parts"]:
            dest = os.path.join(output_dir, record["folder"])
            try:
                write_model_folder(record["parts"], dest)
                record["written_to"] = dest
            except OSError as exc:
                record["status"] = "error"
                record["error"] = f"write failed: {exc}"

        models[folder] = record
        for msg in record.get("followups", []):
            followups.append({"datasource": caption, "message": msg})

    return {
        "models": models,
        "followups": followups,
        "datasources": [{k: v for k, v in e.items()
                         if k not in ("datasource_xml", "_resolver")} for e in entries],
    }


def _rebuild_entry(entry, caption):
    """Rebuild a single enumeration entry into a model result dict (may raise; caller isolates)."""
    classification = entry["classification"]
    if classification == "published_reference":
        return _rebuild_published(entry, caption)
    if classification == "spatial":
        cls = entry["connection_class"]
        return {"status": "fallback", "parts": {}, "report": {"model_name": caption},
                "followups": [f'Spatial source ({cls}) for "{caption}" has no auto-emitted M; '
                              f"rebuild it manually after landing the geometry."]}
    if classification == "flat_file":
        descriptor = parse_tds(entry["datasource_xml"])
        return build_flatfile_model(
            descriptor, model_name=caption,
            connection_class=(entry["connection_class"] or "").lower(),
            flat_file=entry["flat_file"], package_path=entry["package_path"])
    # relational
    return _rebuild_from_xml(entry["datasource_xml"], model_name=caption,
                             package_path=entry["package_path"])


def _rebuild_published(entry, caption):
    """Resolve a published (``sqlproxy``) reference via the seam, or mark it unresolved."""
    published = entry.get("published") or {}
    referenced = published.get("referenced_name") or caption
    resolver = entry.get("_resolver")
    if resolver is not None:
        tds_text = resolver(referenced)
        if tds_text:
            result = _rebuild_from_xml(tds_text, model_name=caption,
                                       package_path=entry.get("package_path"))
            result.setdefault("report", {})["resolved_published"] = referenced
            return result
    return {
        "status": "published_unresolved",
        "parts": {},
        "report": {"model_name": caption, "referenced_name": referenced,
                   "server": published.get("server"), "site": published.get("site")},
        "followups": [
            f'Resolve the published datasource "{referenced}" (referenced by "{caption}") and '
            f"re-run with a resolver, or export it as a .tds and rebuild it standalone."],
    }


# The resolver is threaded onto each published entry just before dispatch so the per-entry
# rebuild stays a pure function of the entry (and so the seam is easy to inject in tests).
def _attach_resolver(entries, resolve_published):
    if resolve_published is None:
        return
    for entry in entries:
        if entry["classification"] == "published_reference":
            entry["_resolver"] = resolve_published


# -- CLI -----------------------------------------------------------------------
def _manifest(result):
    """A summary-only manifest: no TMDL parts, raw XML, GUIDs, file paths, or connection attrs."""
    models = []
    for folder, rec in result["models"].items():
        report = rec.get("report", {})
        models.append({
            "folder": rec["folder"],
            "classification": rec["classification"],
            "connection_class": rec["connection_class"],
            "status": rec.get("status"),
            "tables": report.get("tables", []),
            "column_count": report.get("column_count"),
            "followup_count": len(rec.get("followups", [])),
        })
    return {
        "model_count": len(models),
        "models": models,
        "followups": [f["message"] for f in result["followups"]],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Rebuild a Tableau workbook's embedded datasources into Fabric semantic models.")
    parser.add_argument("workbook", help="path to a .twb / .twbx / .tds / .tdsx file")
    parser.add_argument("-o", "--output", help="write <caption>.SemanticModel folders here")
    args = parser.parse_args(argv)

    result = rebuild_workbook_models(args.workbook, output_dir=args.output)
    print(json.dumps(_manifest(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
