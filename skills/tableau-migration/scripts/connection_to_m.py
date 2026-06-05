"""Tableau ``.tds`` connection parsing + Power Query M emission (offline, stdlib-only).

Turns a Tableau **Download Data Source** ``.tds`` XML document into a normalized connection
*descriptor*, then emits the Power BI artifacts needed to rebuild the datasource pointing
directly at its ORIGINAL upstream source (Import / DirectQuery), instead of only the
land-to-Delta + DirectLake path:

* ``parse_tds(xml_text)``            -> descriptor (connector class, server, database, relations,
                                        per-table columns+types from ``<metadata-records>``, extract flag)
* ``emit_connection_parameters``     -> ``expression Server/Database`` parameter TMDL
* ``emit_table_tmdl_m``              -> full ``table`` TMDL (typed columns + ``= m`` partition)
* ``build_m_field_resolver``         -> caption -> (table, clean_col, tmdl_type) for calc->DAX
* ``connection_details_for_bind``    -> structured details for the Bind Semantic Model Connection API

Honesty boundaries (validated by design review): column types come from Tableau metadata,
never deferred to "Power BI will infer it"; join/union relation trees, multi-connection
datasources, and connectors outside the Sql.Database family are detected and flagged for
fallback rather than guessed. Credentials are NEVER read from or written to the output.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

try:  # works whether imported as a package or run with scripts/ on sys.path
    from .tmdl_generate import clean_col, generate_column_tmdl, q
    from .storage_mode import FLAT_FILE_CLASSES, PARTIAL_LIVE_CONNECTORS, SQL_DATABASE_FAMILY
except ImportError:
    from tmdl_generate import clean_col, generate_column_tmdl, q
    from storage_mode import FLAT_FILE_CLASSES, PARTIAL_LIVE_CONNECTORS, SQL_DATABASE_FAMILY


# -- type mapping --------------------------------------------------------------
# Tableau metadata-record <local-type> -> TMDL column dataType. This is the Import/DQ
# analog of spark_type_to_tmdl (which types the DirectLake path from landed Delta).
def tableau_type_to_tmdl(local_type):
    """Map a Tableau ``<local-type>`` to a TMDL dataType (or None if unsupported)."""
    t = (local_type or "").lower().strip()
    return {
        "integer": "int64",
        "real": "double",
        "string": "string",
        "boolean": "boolean",
        "date": "dateTime",
        "datetime": "dateTime",
    }.get(t)  # 'table'/'spatial'/unknown -> None (skip the column)


# -- XML helpers (namespace-agnostic) -----------------------------------------
def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _findall_local(elem, name):
    return [c for c in elem.iter() if _local(c.tag) == name]


def _children_local(elem, name):
    return [c for c in list(elem) if _local(c.tag) == name]


_BRACKET_PAIR = re.compile(r"^\[(?P<schema>[^\[\]]+)\]\.\[(?P<item>[^\[\]]+)\]$")
_BRACKET_ONE = re.compile(r"^\[(?P<item>[^\[\]]+)\]$")


def _parse_table_name(raw):
    """Conservatively split a relation ``table`` attribute into (schema, item).

    Handles the common ``[schema].[item]`` and ``[item]`` shapes. Anything else returns
    ``(None, None)`` so the caller falls back rather than guessing a wrong schema/item.
    """
    if not raw:
        return None, None
    raw = raw.strip()
    m = _BRACKET_PAIR.match(raw)
    if m:
        return m.group("schema"), m.group("item")
    m = _BRACKET_ONE.match(raw)
    if m:
        return None, m.group("item")
    return None, None


def _strip_brackets(name):
    if name and name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return name


def escape_m_string(s):
    """Escape a string for embedding inside a Power Query M double-quoted literal."""
    return (s or "").replace('"', '""')


# -- parsing -------------------------------------------------------------------
def _named_connections(datasource):
    """Return the live ``<named-connection>`` elements (those under a ``<named-connections>``
    block). Scoping here avoids counting connections nested inside an ``<extract>`` and
    misreading an extracted datasource as a multi-connection one."""
    out = []
    for holder in _findall_local(datasource, "named-connections"):
        out.extend(_children_local(holder, "named-connection"))
    return out


def _live_connection(datasource):
    """Return (class, server, dbname, named_connection_count) for the live source.

    Descends through a ``federated`` wrapper into the inner named-connection. Falls back to
    a direct ``<connection>`` on the datasource for the older non-federated layout.
    """
    named = _named_connections(datasource)
    inner_conns = []
    for nc in named:
        inner_conns.extend(_children_local(nc, "connection"))
    if inner_conns:
        c = inner_conns[0]
        return (c.get("class"), c.get("server"), c.get("dbname"), len(named))
    # non-federated: first <connection> that is not the federated wrapper
    for c in _children_local(datasource, "connection"):
        if (c.get("class") or "").lower() != "federated":
            return (c.get("class"), c.get("server"), c.get("dbname"), 1)
    return (None, None, None, len(named))


def _columns_by_parent(datasource):
    """Map relation item-name -> [ {remote_name, model_name, tmdl_type, local_name} ].

    Built from ``<metadata-record class='column'>`` entries, grouped by ``<parent-name>``.
    Columns whose Tableau type is unsupported are dropped (None tmdl_type).
    """
    out = {}
    for rec in _findall_local(datasource, "metadata-record"):
        if (rec.get("class") or "").lower() != "column":
            continue
        def _txt(tag):
            els = _children_local(rec, tag)
            return els[0].text if els and els[0].text is not None else None
        parent = _strip_brackets((_txt("parent-name") or "").strip()) or None
        remote = (_txt("remote-name") or "").strip() or None
        local = (_txt("local-name") or "").strip() or None
        tmdl_type = tableau_type_to_tmdl(_txt("local-type"))
        if not parent or not remote or tmdl_type is None:
            continue
        out.setdefault(parent, []).append({
            "remote_name": remote,
            "model_name": clean_col(remote),
            "tmdl_type": tmdl_type,
            "local_name": _strip_brackets(local) if local else remote,
        })
    return out


def _build_parent_map(root):
    """Map ``id(child) -> parent`` (ElementTree elements have no parent pointer)."""
    parent = {}
    for p in root.iter():
        for c in list(p):
            parent[id(c)] = p
    return parent


def _nearest_relation_ancestor(rel, parent_map):
    p = parent_map.get(id(rel))
    while p is not None:
        if _local(p.tag) == "relation":
            return p
        p = parent_map.get(id(p))
    return None


def _is_combination_relation(rel):
    """True for a ``join``/``union`` tree (or any non-collection relation that nests child
    relations): these collapse their leaves into ONE logical table and are reported as a
    single combination entry so the storage-mode policy can fall back. A ``collection`` is
    NOT a combination -- it is a container of INDEPENDENT tables."""
    rtype = (rel.get("type") or "").lower()
    if rtype in ("join", "union"):
        return True
    if rtype == "collection":
        return False
    return bool(_children_local(rel, "relation"))


def _extract_relations(datasource, cols_by_parent):
    """Walk ``<relation>`` elements into a flat, de-duplicated descriptor list.

    Handles the modern Tableau "object model" ``.tds`` shape, where the same physical tables
    appear twice -- once under a ``<relation type='collection'>`` container (the physical
    layer) and once under the logical ``<properties>`` layer:

    * ``collection`` containers are dropped; their child tables are emitted as INDEPENDENT
      model tables (multi-sheet Excel / multi-table sources become multiple model tables).
    * ``join``/``union`` trees collapse to a single combination entry; their leaf tables are
      consumed (never leaked as standalone tables) so the policy can fall back cleanly.
    * duplicate physical/logical copies of the same table (same ``item``) are de-duplicated,
      preferring the copy that actually resolves column metadata.
    """
    parent_map = _build_parent_map(datasource)
    relations = []
    table_index = {}  # dedupe key -> index into `relations`
    for rel in _findall_local(datasource, "relation"):
        if (rel.get("type") or "").lower() == "collection":
            continue  # benign container; its child tables are emitted independently
        # Skip any leaf nested inside a join/union tree: the top combination represents it.
        anc = _nearest_relation_ancestor(rel, parent_map)
        consumed = False
        while anc is not None:
            if _is_combination_relation(anc):
                consumed = True
                break
            anc = _nearest_relation_ancestor(anc, parent_map)
        if consumed:
            continue
        entry = _classify_relation(rel, cols_by_parent)
        if entry["kind"] in ("table", "custom_sql"):
            key = (entry.get("item") or entry.get("name") or "").lower()
            if key in table_index:
                prev = relations[table_index[key]]
                if not prev.get("columns") and entry.get("columns"):
                    relations[table_index[key]] = entry  # upgrade a column-less duplicate
                continue
            table_index[key] = len(relations)
        relations.append(entry)
    return relations


def _classify_relation(rel, cols_by_parent):
    """Classify one ``<relation>`` element into a descriptor entry."""
    rtype = (rel.get("type") or "").lower()
    name = rel.get("name")
    # A join/union is either an explicit type or a relation that nests child relations.
    if rtype in ("join", "union") or _children_local(rel, "relation"):
        return {"kind": rtype or "join", "name": name}
    if rtype == "text":  # custom SQL
        item_key = _strip_brackets(name) if name else None
        return {
            "kind": "custom_sql",
            "name": name,
            "sql": (rel.text or "").strip(),
            "columns": cols_by_parent.get(item_key, []),
        }
    if rtype == "table" or rel.get("table"):
        schema, item = _parse_table_name(rel.get("table"))
        if item is None:
            return {"kind": "unknown", "name": name, "raw_table": rel.get("table")}
        cols = cols_by_parent.get(item) or cols_by_parent.get(_strip_brackets(name) if name else "", [])
        return {
            "kind": "table",
            "name": name,
            "raw_table": rel.get("table"),
            "schema": schema,
            "item": item,
            "columns": cols,
        }
    return {"kind": "unknown", "name": name, "raw_table": rel.get("table")}


def parse_tds(xml_text):
    """Parse Tableau ``.tds`` XML into a normalized connection descriptor (dict).

    The descriptor is JSON-serializable (suitable for a migration report) and contains NO
    credentials. ``unsupported_reasons`` collects shape problems found during parsing so the
    storage-mode policy can fall back cleanly.
    """
    root = ET.fromstring(xml_text)
    datasource = root if _local(root.tag) == "datasource" else (
        _findall_local(root, "datasource") or [root])[0]

    cls, server, dbname, nconns = _live_connection(datasource)
    cols_by_parent = _columns_by_parent(datasource)

    relations = _extract_relations(datasource, cols_by_parent)

    is_extract = False
    for ex in _findall_local(datasource, "extract"):
        if (ex.get("enabled") or "true").lower() != "false":
            is_extract = True
            break

    unsupported = []
    table_like = [r for r in relations if r["kind"] in ("table", "custom_sql")]
    for r in table_like:
        if not r.get("columns"):
            unsupported.append(f"relation '{r.get('name')}' has no resolvable columns")

    return {
        "datasource_name": datasource.get("formatted-name") or datasource.get("name"),
        "connection_class": cls,
        "server": server,
        "database": dbname,
        "is_extract": is_extract,
        "named_connection_count": nconns,
        "relations": relations,
        "unsupported_reasons": unsupported,
    }


# -- M / TMDL emission ---------------------------------------------------------
_PARAM_META = 'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'


def emit_connection_parameters(descriptor):
    """Emit ``expression Server/Database`` parameter TMDL for a relational descriptor.

    Returns an empty string when there is no server/database (e.g. flat files), so callers
    can concatenate unconditionally.
    """
    lines = []
    if descriptor.get("server"):
        lines.append(f'expression Server = "{escape_m_string(descriptor["server"])}" {_PARAM_META}\n')
    if descriptor.get("database"):
        lines.append(f'expression Database = "{escape_m_string(descriptor["database"])}" {_PARAM_META}\n')
    return "\n".join(lines)


def _m_mode_keyword(mode):
    return "directQuery" if (mode or "").lower() == "directquery" else "import"


def emit_m_partition_source(relation, descriptor, mode):
    """Emit the ``source = let ... in ...`` body for one relation's M partition.

    Only the Sql.Database connector family is emitted as deploy-ready M; other connectors
    return a clearly-commented scaffold so the structure is valid TMDL but obviously needs
    manual completion (never silently wrong).
    """
    cls = (descriptor.get("connection_class") or "").lower()
    connector = SQL_DATABASE_FAMILY.get(cls)
    if connector is None:
        # Recognized-but-partial / flat-file / unknown: name the intended connector as a hint
        # but never emit a guessed `(server, database)` call when the real signature differs.
        intended = PARTIAL_LIVE_CONNECTORS.get(cls) or FLAT_FILE_CLASSES.get(cls)
        hint = f" using {intended}" if intended else ""
        return ("\t\t\t// TODO: complete the M partition for connector class "
                f"'{cls or 'unknown'}'{hint} "
                "(signature/navigation differs from the supported (server, database) family; "
                "not auto-emitted in v1)\n"
                '\t\t\tlet Source = null in Source')

    if relation["kind"] == "custom_sql":
        sql = escape_m_string(relation.get("sql", ""))
        # EnableFolding lets DirectQuery push the native query down to the source.
        return (
            "let\n"
            f'\t\t\t\tSource = {connector}(#"Server", #"Database"),\n'
            f'\t\t\t\tResult = Value.NativeQuery(Source, "{sql}", null, [EnableFolding=true])\n'
            "\t\t\tin\n"
            "\t\t\t\tResult"
        )

    schema = relation.get("schema") or "dbo"
    item = relation["item"]
    nav = f'Source{{[Schema="{escape_m_string(schema)}", Item="{escape_m_string(item)}"]}}[Data]'
    return (
        "let\n"
        f'\t\t\t\tSource = {connector}(#"Server", #"Database"),\n'
        f"\t\t\t\tData = {nav}\n"
        "\t\t\tin\n"
        "\t\t\t\tData"
    )


def emit_table_tmdl_m(relation, descriptor, mode):
    """Emit a full ``table`` TMDL block (typed columns + ``= m`` partition) for one relation.

    Columns and types come from the parsed Tableau metadata, so the model is deterministic
    and deploy-ready without relying on Power BI schema inference. Returns ``None`` for a
    relation with no resolvable columns (caller should fall back).
    """
    cols = relation.get("columns") or []
    if not cols:
        return None
    table_display = relation.get("name") or relation.get("item") or "Table"
    columns_tmdl = ""
    for c in cols:
        summarize = "sum" if c["tmdl_type"] in ("int64", "double", "decimal") else "none"
        # In the M path the model column name == its sourceColumn (the remote source name).
        columns_tmdl += generate_column_tmdl(c["model_name"], c["tmdl_type"], summarize, False)

    partition_name = relation.get("item") or clean_col(table_display)
    source_body = emit_m_partition_source(relation, descriptor, mode)
    return (
        f"table {q(table_display)}\n"
        f"{columns_tmdl}\n"
        f"\tpartition {q(partition_name)} = m\n"
        f"\t\tmode: {_m_mode_keyword(mode)}\n"
        f"\t\tsource =\n"
        f"\t\t\t{source_body}\n\n"
    )


def build_m_field_resolver(descriptor):
    """Build ``resolve_field(caption) -> (table, clean_col, tmdl_type) | None`` for the M path.

    Mirrors the DirectLake field resolver, but sources columns/types from the parsed Tableau
    metadata instead of landed Delta. Resolves only when exactly one table exposes the caption
    unambiguously (the column's Tableau ``local-name``), so a measure never binds to the wrong
    column.
    """
    cap_to = {}   # (table, caption) -> (clean_col, tmdl_type)
    counts = {}   # (table, clean_col) -> set(captions)  (collision detector)
    for rel in descriptor.get("relations", []):
        if rel.get("kind") not in ("table", "custom_sql"):
            continue
        table = rel.get("name") or rel.get("item")
        for c in rel.get("columns", []):
            cap = c.get("local_name") or c.get("remote_name")
            cc = c["model_name"]
            cap_to[(table, cap)] = (cc, c["tmdl_type"])
            counts.setdefault((table, cc), set()).add(cap)

    tables = {(rel.get("name") or rel.get("item"))
              for rel in descriptor.get("relations", [])
              if rel.get("kind") in ("table", "custom_sql")}

    def resolve_field(caption):
        hits = []
        for table in tables:
            got = cap_to.get((table, caption))
            if got is None:
                continue
            cc, tmdl_type = got
            if len(counts.get((table, cc), ())) != 1:
                continue
            hits.append((table, cc, tmdl_type))
        return hits[0] if len(hits) == 1 else None

    return resolve_field


# Power BI "List Item Connections" data-source types keyed by Tableau connector class.
_BIND_TYPE = {
    "sqlserver": "SQL",
    "azure_sqldb": "SQL",
    "postgres": "PostgreSql",
    "oracle": "Oracle",
    "mysql": "MySql",
    "redshift": "AmazonRedshift",
    "teradata": "Teradata",
    "snowflake": "Snowflake",
    "bigquery": "GoogleBigQuery",
}


def connection_details_for_bind(descriptor):
    """Return structured connection details for the Bind Semantic Model Connection API.

    A later binding adapter flattens ``path`` per the connector's exact requirement; the
    structured fields are kept so nothing is lost for non-SQL connectors.
    """
    cls = (descriptor.get("connection_class") or "").lower()
    server = descriptor.get("server")
    database = descriptor.get("database")
    path = ";".join(p for p in (server, database) if p) or None
    return {
        "connector": cls or None,
        "bind_type": _BIND_TYPE.get(cls),
        "server": server,
        "database": database,
        "path": path,
    }
