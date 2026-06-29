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
datasources, and connectors whose M we can't yet emit with verified correctness are detected
and flagged (scaffold / fallback) rather than guessed. Credentials are NEVER read from or
written to the output.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

try:  # works whether imported as a package or run with scripts/ on sys.path
    from .tmdl_generate import (clean_col, generate_column_tmdl, q, tableau_default_format_to_pbi,
                                tableau_geo_role_to_data_category)
    from .storage_mode import (
        ANALYSIS_SERVICES_CLASSES, DIRECT_CONNECTORS, FLAT_FILE_CLASSES,
        NATIVE_QUERY_CATALOG_DRILL, PARTIAL_LIVE_CONNECTORS, connector_spec)
except ImportError:
    from tmdl_generate import (clean_col, generate_column_tmdl, q, tableau_default_format_to_pbi,
                               tableau_geo_role_to_data_category)
    from storage_mode import (
        ANALYSIS_SERVICES_CLASSES, DIRECT_CONNECTORS, FLAT_FILE_CLASSES,
        NATIVE_QUERY_CATALOG_DRILL, PARTIAL_LIVE_CONNECTORS, connector_spec)


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


def _findall_object_graph(elem):
    """All ``object-graph`` elements, tolerant of Tableau's wrapped tag name.

    Tableau Desktop's logical model can emit the object graph under a feature-flagged tag such as
    ``_.fcp.ObjectModelEncapsulateLegacy.true...object-graph`` instead of a plain ``object-graph``.
    Match on the local name's suffix so both spellings resolve; the nested ``<objects>`` /
    ``<relationships>`` children are always plain, so only this outermost tag needs the tolerance.
    """
    return [c for c in elem.iter() if _local(c.tag).endswith("object-graph")]


_BRACKET_THREE = re.compile(
    r"^\[(?P<catalog>[^\[\]]+)\]\.\[(?P<schema>[^\[\]]+)\]\.\[(?P<item>[^\[\]]+)\]$")
_BRACKET_PAIR = re.compile(r"^\[(?P<schema>[^\[\]]+)\]\.\[(?P<item>[^\[\]]+)\]$")
_BRACKET_ONE = re.compile(r"^\[(?P<item>[^\[\]]+)\]$")


def _parse_table_name(raw):
    """Conservatively split a relation ``table`` attribute into ``(catalog, schema, item)``.

    Handles the three bracketed shapes Tableau emits, widest first:

    * ``[catalog].[schema].[item]`` -- the Tableau 2023+ object-model shape over three-part-name
      backends (Snowflake ``DB.SCHEMA.TABLE``, Databricks Unity ``catalog.schema.table``); the
      first segment is the catalog/database, reached by the connector's first navigation hop.
    * ``[schema].[item]``           -- the classic two-part relational shape.
    * ``[item]``                    -- a bare table name.

    Anything else returns ``(None, None, None)`` so the caller falls back rather than guessing a
    wrong schema/item. ``catalog`` is ``None`` for the two- and one-part shapes.
    """
    if not raw:
        return None, None, None
    raw = raw.strip()
    m = _BRACKET_THREE.match(raw)
    if m:
        return m.group("catalog"), m.group("schema"), m.group("item")
    m = _BRACKET_PAIR.match(raw)
    if m:
        return None, m.group("schema"), m.group("item")
    m = _BRACKET_ONE.match(raw)
    if m:
        return None, None, m.group("item")
    return None, None, None


def _strip_brackets(name):
    if name and name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return name


def escape_m_string(s):
    """Escape a string for embedding inside a Power Query M double-quoted literal."""
    return (s or "").replace('"', '""')


# -- Custom SQL de-escape ------------------------------------------------------
# Tableau serializes a Custom SQL relation by doubling EVERY angle bracket in the query
# text ('<' -> '<<', '>' -> '>>'). It is a blind, global, per-character substitution -- it
# also rewrites the inside of line/block comments and string literals -- used to escape the
# angle brackets that delimit Tableau's own parameter syntax (<Parameters.[Name]>); Tableau
# halves the brackets back on read/execute, so the query that actually runs is single-operator
# and correct. A migration tool that reads the raw .tds XML therefore sees the DOUBLED form,
# and emitting it verbatim corrupts the query: on Spark/Databricks '<<'/'>>' are the bitwise
# shiftleft/shiftright operators, so a comparison predicate like (Profit << 0) fails at refresh
# with DATATYPE_MISMATCH while the deploy itself looks clean. The inverse of a clean
# per-character double is a global halve. Verified against controlled Databricks Superstore
# diagnostic saves: an operator matrix (< <= > >= <> -> << <<= >> >>= <<>>), contamination of
# comment + string-literal text, an all-even bracket-run invariant, and an executable .hyper
# (proving Tableau itself halves on read). See resources/migration-gotchas.md.
_TABLEAU_PARAM_REF = re.compile(r"<+\s*Parameters\s*\.\s*(\[[^\]]+\])\s*>+", re.IGNORECASE)
_DEESCAPED_PARAM_REF = re.compile(r"<\s*Parameters\s*\.\s*\[[^\]]+\]\s*>", re.IGNORECASE)


def _deescape_custom_sql(sql):
    """Reverse Tableau's on-disk angle-bracket doubling for Custom SQL text.

    Apply EXACTLY ONCE, at the .tds parse boundary -- a global halve is NOT idempotent. A
    genuine source ``<<`` (e.g. a real Spark bitwise shift) is stored as ``<<<<`` and a single
    halve correctly recovers ``<<``; a second halve would wrongly collapse it to ``<``. The
    relation descriptor's ``sql`` field is the single canonical home for this, so every
    downstream stage (M emission, profiling, comparison) only ever sees the recovered
    single-operator form.

    Parameter-aware: a Tableau parameter reference (``<Parameters.[Name]>``) uses angle brackets
    as delimiter syntax. Its exact stored bracketing is not re-verified here, so each token is
    masked out before the halve and restored to canonical single-bracket form afterwards. The
    mask also prevents a doubled operator sitting flush against a parameter delimiter from
    forming an odd-length run that a blind halve would mangle.
    """
    if not sql:
        return sql
    masked = []

    def _stash(m):
        masked.append(m.group(1))  # the [Name] portion
        return f"\x00P{len(masked) - 1}\x00"

    work = _TABLEAU_PARAM_REF.sub(_stash, sql)
    work = work.replace("<<", "<").replace(">>", ">")
    for i, inner in enumerate(masked):
        work = work.replace(f"\x00P{i}\x00", f"<Parameters.{inner}>")
    return work


def custom_sql_parameter_refs(sql):
    """Distinct canonical Tableau parameter tokens (``<Parameters.[Name]>``) in de-escaped
    Custom SQL.

    A recovered parameter reference cannot yet be translated into a Power BI / Power Query
    parameter, and the source engine cannot run it as-is, so a surviving token is a real
    needs-review signal rather than something to emit silently.
    """
    out = []
    for m in _DEESCAPED_PARAM_REF.finditer(sql or ""):
        tok = m.group(0)
        if tok not in out:
            out.append(tok)
    return out


# -- parsing -------------------------------------------------------------------
def _named_connections(datasource):
    """Return the live ``<named-connection>`` elements (those under a ``<named-connections>``
    block). Scoping here avoids counting connections nested inside an ``<extract>`` and
    misreading an extracted datasource as a multi-connection one."""
    out = []
    for holder in _findall_local(datasource, "named-connections"):
        out.extend(_children_local(holder, "named-connection"))
    return out


# Tableau spells the Databricks SQL-warehouse HTTP path differently across driver/connector
# versions; check each known attribute (newest first) so a real .tds resolves regardless.
_HTTP_PATH_ATTRS = ("v-http-path", "http-path", "httppath", "http_path")


def _http_path_of(conn):
    """Return the Databricks SQL-warehouse HTTP path from whichever attribute carries it, or None."""
    for attr in _HTTP_PATH_ATTRS:
        v = conn.get(attr)
        if v:
            return v
    return None


def _live_connection(datasource):
    """Return ``(class, server, dbname, warehouse, http_path, auth_method, named_connection_count)``.

    Descends through a ``federated`` wrapper into the inner named-connection. Falls back to
    a direct ``<connection>`` on the datasource for the older non-federated layout. ``warehouse``
    is the Snowflake compute warehouse; ``http_path`` is the Databricks SQL-warehouse HTTP path
    (read from whichever attribute carries it -- ``None`` when absent / for other connectors).
    ``auth_method`` is the inner connection's ``authentication`` attribute LABEL ONLY (a non-secret
    hint for the Fabric credential type, e.g. 'Username Password' or 'oauth'); NO secret attribute
    (username / password / token / oauth-config-id / instanceurl) is ever read.
    """
    named = _named_connections(datasource)
    inner_conns = []
    for nc in named:
        inner_conns.extend(_children_local(nc, "connection"))
    if inner_conns:
        c = inner_conns[0]
        return (c.get("class"), c.get("server"), c.get("dbname"),
                c.get("warehouse"), _http_path_of(c), c.get("authentication"), len(named))
    # non-federated: first <connection> that is not the federated wrapper
    for c in _children_local(datasource, "connection"):
        if (c.get("class") or "").lower() != "federated":
            return (c.get("class"), c.get("server"), c.get("dbname"),
                    c.get("warehouse"), _http_path_of(c), c.get("authentication"), 1)
    return (None, None, None, None, None, None, len(named))


# Non-secret routing facts lifted from one inner <connection>. A federated datasource can carry
# several named connections (one per upstream), each driving its OWN connector/navigation, so we
# capture each connection's facts to route per relation. STRICT secret boundary: only the class,
# server, database, warehouse, HTTP path, schema, and the authentication LABEL are read -- never
# username / password / token / oauth-config-id / instanceurl.
def _connection_facts(c):
    return {
        "connection_class": c.get("class"),
        "server": c.get("server"),
        "database": c.get("dbname"),
        "warehouse": c.get("warehouse"),
        "http_path": _http_path_of(c),
        "schema": c.get("schema"),
        "auth_method": c.get("authentication"),
        "filename": c.get("filename"),
        "directory": c.get("directory"),
    }


def _flatfile_location(datasource):
    """The inner connection's flat-file location ``(filename, directory)``.

    Descended the same way as ``_live_connection`` (federated named-connection inner first, then a
    direct non-federated ``<connection>``). Flat-file sources (Excel / text) carry the workbook or
    CSV path here; both are ``None`` for live database connections. Only non-secret path attributes
    are read.
    """
    for nc in _named_connections(datasource):
        for c in _children_local(nc, "connection"):
            if c.get("filename") or c.get("directory"):
                return c.get("filename"), c.get("directory")
    for c in _children_local(datasource, "connection"):
        if (c.get("class") or "").lower() != "federated" and (
                c.get("filename") or c.get("directory")):
            return c.get("filename"), c.get("directory")
    return None, None


def _flatfile_join(directory, filename):
    """Join a flat-file ``directory`` + ``filename`` into a single path (forward-slash, M-safe).

    Returns ``None`` when there is no filename. The path Tableau stored is RELATIVE to the workbook;
    a driver overrides ``flatfile_path`` with the absolute path of the copied data file before
    assembly (relative paths aren't portable in a deployed PBIP).
    """
    if not filename:
        return None
    if directory:
        return directory.rstrip("/\\") + "/" + filename
    return filename


def _named_connection_map(datasource):
    """Map each ``<named-connection>`` id -> its inner connection's non-secret routing facts.

    A relation's ``connection`` attribute is a named-connection id; this map lets a federated
    datasource bind EACH relation to its own upstream connection (so a multi-connector source picks
    the right connector function / navigation per table). Only non-secret attributes are read.
    """
    out = {}
    for nc in _named_connections(datasource):
        nc_id = nc.get("name")
        inner = _children_local(nc, "connection")
        if nc_id and inner:
            out[nc_id] = _connection_facts(inner[0])
    return out


def _default_formats_by_physical(datasource):
    """Map ``(table, model_col) -> Power BI formatString`` from ``<column @default-format>``.

    Tableau persists an author's explicit per-field number format as a ``default-format``
    code on the logical ``<column>`` element (e.g. ``default-format='c"$"#,##0;("$"#,##0)'``).
    Each such ``<column name='[lid]'>`` is joined to its physical ``(table, column)`` through
    the ``<cols><map key='[lid]' value='[TABLE].[COL]'>`` logical->physical mapping, the code
    is decoded to a Power BI ``formatString``, and the result is keyed by
    ``(table, clean_col(physical))`` -- the SAME identity the ``<metadata-record>`` column
    descriptors carry -- so the M-path column emitter can apply it. A column whose code is
    undecodable, or whose logical id is unmapped / ambiguously mapped, is omitted so the
    caller keeps its type-derived floor (never a guess, never a regression).
    """
    lid_to_phys = {}
    for cols in _findall_local(datasource, "cols"):
        for m in _children_local(cols, "map"):
            key = _strip_brackets((m.get("key") or "").strip())
            _cat, table, col = _parse_table_name((m.get("value") or "").strip())
            if key and table and col:
                lid_to_phys.setdefault(key, set()).add((table, col))
    out = {}
    for col in _children_local(datasource, "column"):
        code = col.get("default-format")
        if not code:
            continue
        fmt = tableau_default_format_to_pbi(code)
        if not fmt:
            continue
        lid = _strip_brackets((col.get("name") or "").strip())
        phys = lid_to_phys.get(lid)
        if not phys or len(phys) != 1:
            continue  # unmapped / ambiguously mapped -> never guess
        table, physical_col = next(iter(phys))
        out[(table, clean_col(physical_col))] = fmt
    return out


def _metadata_identity_index(datasource):
    """Map a logical column name -> its UNIQUE physical ``(parent, model_col)`` identity.

    Built from ``<metadata-record class='column'>`` descriptors: a record's ``local-name`` (the
    bracketed logical id, e.g. ``[State/Province]``) and its ``clean_col(remote-name)`` both index
    the ``(parent, clean_col(remote))`` identity that ``_columns_by_parent`` emits under. A name
    resolving to more than one distinct identity is poisoned (dropped) so an ambiguous name is never
    guessed. This recovers the logical->physical join for ``.hyper`` extracts, which inline the
    physical layer and carry no live-connection ``<cols><map>`` mapping.
    """
    by_name = {}
    for rec in _findall_local(datasource, "metadata-record"):
        if (rec.get("class") or "").lower() != "column":
            continue
        def _txt(tag):
            els = _children_local(rec, tag)
            return els[0].text if els and els[0].text is not None else None
        parent = _strip_brackets((_txt("parent-name") or "").strip()) or None
        remote = (_txt("remote-name") or "").strip() or None
        if not parent or not remote:
            continue
        local = (_txt("local-name") or "").strip() or None
        ident = (parent, clean_col(remote))
        names = {clean_col(remote)}
        if local:
            names.add(_strip_brackets(local))
        for nm in names:
            if not nm:
                continue
            if nm not in by_name:
                by_name[nm] = ident
            elif by_name[nm] != ident:
                by_name[nm] = None  # same name, two identities -> never guess
    return {k: v for k, v in by_name.items() if v is not None}


_OID_HASH_RE = re.compile(r"_[0-9A-Fa-f]{32}$")


def _strip_oid_hash(table):
    """Drop a trailing Tableau object-id hash from a physical table name.

    An extract-backed ``.tds`` duplicates every ``<cols><map>`` for the ``.hyper`` cache twin
    ``<Base>_<hex32>`` (e.g. ``Orders_ECFCA1FB690A41FE803BC071773BA862``) -- a LOCAL cache of the
    same logical table, never an independent upstream. Collapsing the suffix lets the geo join treat
    the base table and its extract twin as ONE identity instead of a false ambiguity, while leaving
    an un-suffixed live table name unchanged.
    """
    return _OID_HASH_RE.sub("", table or "")


def _geo_categories_by_physical(datasource):
    """Map ``(table, model_col) -> Power BI dataCategory`` from a column's geo ``semantic-role``.

    Each logical ``<column semantic-role=...>`` carrying a geographic role (State/Country/City/
    County/PostalCode) is joined to its physical ``(table, column)`` and keyed by
    ``(table, clean_col(physical))`` -- the SAME identity the ``<metadata-record>`` descriptors
    carry, so the column emitter can apply it. The join consults the live-connection ``<cols><map>``
    mapping first (collapsing object-id-hash ``.hyper`` twins of the same table so a base+twin pair
    is not read as a false ambiguity); when that mapping is SILENT for a column (a ``.hyper`` extract
    inlines the physical layer and carries no ``<cols><map>``), it falls back to the metadata-record
    identity by name. A genuinely ambiguous ``<cols><map>`` (a lid mapped to several DISTINCT
    physical columns) fails closed -- it is NOT overridden by the fallback -- and a role with no
    faithful Power BI category, or a name that resolves nowhere, is omitted (never a guess, never a
    regression).
    """
    lid_to_phys = {}
    for cols in _findall_local(datasource, "cols"):
        for m in _children_local(cols, "map"):
            key = _strip_brackets((m.get("key") or "").strip())
            _cat, table, col = _parse_table_name((m.get("value") or "").strip())
            if key and table and col:
                lid_to_phys.setdefault(key, set()).add((table, col))
    name_to_identity = _metadata_identity_index(datasource)
    out = {}
    for col in _children_local(datasource, "column"):
        cat = tableau_geo_role_to_data_category(col.get("semantic-role"))
        if not cat:
            continue
        lid = _strip_brackets((col.get("name") or "").strip())
        phys = lid_to_phys.get(lid)
        if phys is not None:
            # <cols><map> spoke for this lid. Collapse object-id-hash twins first: an extract
            # duplicates every map for a <Base>_<hex32> .hyper cache of the SAME logical table, so a
            # base+twin pair is ONE identity, not a false ambiguity. A single surviving identity
            # resolves; several genuinely-distinct ones fail closed (never guess); neither defers to
            # the metadata fallback.
            collapsed = {(_strip_oid_hash(table), clean_col(col)) for table, col in phys}
            if len(collapsed) == 1:
                out[next(iter(collapsed))] = cat
            continue
        # <cols><map> silent (extract): fall back to the metadata-record identity by name.
        ident = name_to_identity.get(lid)
        if ident:
            out[ident] = cat
    return out


def _columns_by_parent(datasource):
    """Map relation item-name -> [ {remote_name, model_name, tmdl_type, local_name} ].

    Built from ``<metadata-record class='column'>`` entries, grouped by ``<parent-name>``.
    Columns whose Tableau type is unsupported are dropped (None tmdl_type). A column that
    carries an author's explicit ``default-format`` (joined via ``_default_formats_by_physical``)
    additionally gets a ``format_string`` key; the key is simply absent otherwise.
    """
    out = {}
    fmt_by_physical = _default_formats_by_physical(datasource)
    geo_by_physical = _geo_categories_by_physical(datasource)
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
        model_name = clean_col(remote)
        col = {
            "remote_name": remote,
            "model_name": model_name,
            "tmdl_type": tmdl_type,
            "local_name": _strip_brackets(local) if local else remote,
        }
        fmt = fmt_by_physical.get((parent, model_name))
        if fmt:
            col["format_string"] = fmt
        cat = geo_by_physical.get((parent, model_name))
        if cat:
            col["data_category"] = cat
        out.setdefault(parent, []).append(col)
    return out


def _logical_fields(datasource):
    """Bridge Tableau's LOGICAL field layer to physical columns, for calc->DAX resolution.

    A live (non-extract) ``.tds`` over a case-sensitive backend (Snowflake / Databricks Unity)
    keeps the physical column names verbatim in ``<metadata-records>`` (e.g. ``SALES``), so the
    metadata-record ``local-name`` equals the ``remote-name`` and carries no friendly caption.
    Calc formulas, however, reference the user-facing caption (``[Sales]``). The caption->physical
    mapping lives in two sibling structures Tableau writes for the logical model:

    * ``<column caption='Sales' datatype='real' name='[SALES]' .../>`` -- caption -> logical id + type
    * ``<cols><map key='[SALES]' value='[ORDERS].[SALES]' /></cols>``  -- logical id -> table.physical

    Joining them yields ``caption -> (table, physical_col, tmdl_type)``. Calculated fields (a
    ``<column>`` with a nested ``<calculation>``) are skipped -- they carry no ``<cols>`` map entry
    and must translate from their formula, not bind as a physical column. Object/table columns
    (``datatype='table'``) type to ``None`` and are skipped. Returns ``[]`` when the ``.tds`` has no
    logical layer (e.g. the metadata-record-only fixtures), so callers degrade to the physical path.
    """
    # logical id -> set of (table, physical_col). A set so a duplicate/conflicting <map key>
    # (multiple <cols> blocks, or a key remapped in two scopes) is detected and the field is
    # dropped rather than bound to whichever mapping parsed last (fail closed).
    logical_to_physical = {}
    for cols in _findall_local(datasource, "cols"):
        for m in _children_local(cols, "map"):
            key = _strip_brackets((m.get("key") or "").strip())
            _cat, table, col = _parse_table_name((m.get("value") or "").strip())
            if key and table and col:
                logical_to_physical.setdefault(key, set()).add((table, col))

    out = []
    for col in _children_local(datasource, "column"):
        if _children_local(col, "calculation"):
            continue  # calculated field -- translated from formula, not a physical binding
        caption = (col.get("caption") or "").strip()
        lid = _strip_brackets((col.get("name") or "").strip())
        if not caption or not lid:
            continue
        phys = logical_to_physical.get(lid)
        if not phys or len(phys) != 1:
            continue  # unmapped, or ambiguously mapped -> never guess
        tmdl_type = tableau_type_to_tmdl(col.get("datatype"))
        if tmdl_type is None:
            continue
        table, physical_col = next(iter(phys))
        out.append({
            "caption": caption,
            "logical_id": lid,
            "table": table,
            "physical_col": physical_col,
            "model_col": clean_col(physical_col),
            "tmdl_type": tmdl_type,
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


def _is_extract_cache_relation(entry):
    """True for a Tableau *extract-cache* table relation -- a ``[Extract].[...]`` twin.

    When a datasource has a stored extract, Tableau Server materializes each live/logical relation
    a second time in its reserved ``Extract`` namespace (``table='[Extract].[orders (...)_HASH]'``)
    as the ``.hyper`` cache. That twin is **never an independent upstream**: it is a local cache of
    a live relation. When the live relation is present the twin is a pure duplicate, and in a
    DirectLake rebuild it would bind to a non-existent Delta entity (the mangled ``..._HASH`` name).
    Identified conservatively by Tableau's reserved ``Extract`` catalog/schema token.
    """
    if entry.get("kind") not in ("table", "custom_sql"):
        return False
    return "extract" in (
        (entry.get("catalog") or "").lower(),
        (entry.get("schema") or "").lower(),
    )


def _extract_relations(datasource, cols_by_parent, nc_map=None):
    """Walk ``<relation>`` elements into a flat, de-duplicated descriptor list.

    Handles the modern Tableau "object model" ``.tds`` shape, where the same physical tables
    appear twice -- once under a ``<relation type='collection'>`` container (the physical
    layer) and once under the logical ``<properties>`` layer:

    * ``collection`` containers are dropped; their child tables are emitted as INDEPENDENT
      model tables (multi-sheet Excel / multi-table sources become multiple model tables).
    * ``join``/``union`` trees collapse to a single combination entry; their leaf tables are
      consumed (never leaked as standalone tables) so the policy can fall back cleanly.
    * duplicate physical/logical copies of the same table (same ``item``) are de-duplicated,
      preferring the copy that actually resolves column metadata, while preserving a resolved
      per-relation ``connection`` from whichever copy carried it.
    * an extract ``.tds`` pulled from Tableau Server also carries a parallel ``[Extract].[...]``
      cache layer; those cache twins are dropped in favour of the live/logical relation (see
      ``_is_extract_cache_relation``), but ONLY when a live table relation survives to represent
      them -- an extract-ONLY datasource keeps its ``[Extract]`` tables, since they are all it has.
    """
    nc_map = nc_map or {}
    parent_map = _build_parent_map(datasource)

    # First pass: classify every candidate relation (skipping benign collection containers and the
    # leaves consumed by a join/union tree) so the extract-twin decision can be made with
    # whole-datasource knowledge before any table is emitted.
    candidates = []
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
        candidates.append(_classify_relation(rel, cols_by_parent, nc_map))

    # Only drop ``[Extract]`` cache twins when at least one live (non-extract) table remains to
    # carry the data; an extract-only source must keep them.
    has_live_table = any(
        e["kind"] in ("table", "custom_sql") and not _is_extract_cache_relation(e)
        for e in candidates)

    relations = []
    table_index = {}  # dedupe key -> index into `relations`
    for entry in candidates:
        if entry["kind"] in ("table", "custom_sql"):
            if has_live_table and _is_extract_cache_relation(entry):
                continue  # prefer the live/logical relation over its extract-cache twin
            # De-dup on the fully-qualified path so the physical + logical copies of ONE table
            # collapse, but two genuinely different tables that merely share a leaf name (different
            # catalog/schema) stay distinct.
            key = (
                (entry.get("catalog") or "").lower(),
                (entry.get("schema") or "").lower(),
                (entry.get("item") or entry.get("name") or "").lower(),
            )
            if key in table_index:
                idx = table_index[key]
                prev = relations[idx]
                if not prev.get("columns") and entry.get("columns"):
                    # Upgrade a column-less duplicate, but never lose a connection either copy had.
                    if not entry.get("connection") and prev.get("connection"):
                        entry["connection"] = prev["connection"]
                    relations[idx] = entry
                elif not prev.get("connection") and entry.get("connection"):
                    prev["connection"] = entry["connection"]
                continue
            table_index[key] = len(relations)
        relations.append(entry)
    return relations


def _classify_relation(rel, cols_by_parent, nc_map=None):
    """Classify one ``<relation>`` element into a descriptor entry.

    When the relation carries a ``connection`` attribute that resolves in ``nc_map``, the resolved
    non-secret connection facts are attached as ``connection`` so a multi-connector federated source
    can route this table to its own upstream.
    """
    nc_map = nc_map or {}
    conn = nc_map.get(rel.get("connection")) if rel.get("connection") else None
    rtype = (rel.get("type") or "").lower()
    name = rel.get("name")
    # A join/union is either an explicit type or a relation that nests child relations.
    if rtype in ("join", "union") or _children_local(rel, "relation"):
        return {"kind": rtype or "join", "name": name}
    if rtype == "text":  # custom SQL
        item_key = _strip_brackets(name) if name else None
        entry = {
            "kind": "custom_sql",
            "name": name,
            "sql": _deescape_custom_sql((rel.text or "").strip()),
            "columns": cols_by_parent.get(item_key, []),
        }
        if conn:
            entry["connection"] = conn
        return entry
    if rtype == "table" or rel.get("table"):
        catalog, schema, item = _parse_table_name(rel.get("table"))
        if item is None:
            return {"kind": "unknown", "name": name, "raw_table": rel.get("table")}
        cols = cols_by_parent.get(item) or cols_by_parent.get(_strip_brackets(name) if name else "", [])
        entry = {
            "kind": "table",
            "name": name,
            "raw_table": rel.get("table"),
            "catalog": catalog,
            "schema": schema,
            "item": item,
            "columns": cols,
        }
        if conn:
            entry["connection"] = conn
        return entry
    return {"kind": "unknown", "name": name, "raw_table": rel.get("table")}


def _table_display(rel):
    """The display name we emit for a table/custom-SQL relation (``table <name>`` in TMDL)."""
    return rel.get("name") or rel.get("item")


def _columns_index(relations):
    """Case-insensitive map of emitted-table display name -> its column list."""
    idx = {}
    for r in relations:
        if r.get("kind") in ("table", "custom_sql"):
            name = _table_display(r)
            if name:
                idx[name.lower()] = r.get("columns") or []
    return idx


def _object_table_map(datasource, relations):
    """Map ``<object-graph>`` object-id -> the emitted table display name it refers to.

    Each ``<object>`` nests the same ``<relation name=...>`` that becomes a parsed table, so we
    resolve the object's nested relation ``name`` (falling back to ``caption`` then the ``id``
    attribute -- never empty), then snap it to an ACTUAL parsed table display name. An object that
    doesn't line up with a parsed table is left unresolved so its relationships are skipped rather
    than pointed at a non-existent table.
    """
    disp = {}
    for r in relations:
        if r.get("kind") in ("table", "custom_sql"):
            name = _table_display(r)
            if name:
                disp.setdefault(name.lower(), name)
    out = {}
    for og in _findall_object_graph(datasource):
        for obj in _findall_local(og, "object"):
            oid = obj.get("id")
            if not oid:
                continue
            nested = _findall_local(obj, "relation")
            cand = (nested[0].get("name") if nested else None) or obj.get("caption") or oid
            out[oid] = disp.get((cand or "").lower())  # None when it doesn't match a parsed table
    return out


# A relationship operand carrying a trailing Tableau rename caption, e.g. 'Region (people)'. The
# last parenthetical is the disambiguating caption; the base before it is the field name. Tried
# only AFTER an exact (verbatim) match, so a column whose real name contains parentheses survives.
_REL_CAPTION_SUFFIX = re.compile(r"^(?P<base>.+?)\s*\([^()]*\)$")


def _resolve_rel_column(raw_op, columns):
    """Resolve a relationship operand like ``[Region (people)]`` to the EMITTED model column name.

    Matches case-insensitively against each column's local / remote / model name (so a case-only or
    rename-caption difference still binds -- Power BI relationships are case-insensitive), and
    returns the column's ``model_name`` (the identifier actually emitted in TMDL) so a downstream
    relationship references a real column. Returns ``None`` when nothing matches, so the caller skips
    the relationship and records a warning rather than emitting a dangling reference.
    """
    if not raw_op:
        return None
    name = _strip_brackets(raw_op.strip())
    lookup = {}
    for c in columns:
        for key in (c.get("local_name"), c.get("remote_name"), c.get("model_name")):
            if key:
                lookup.setdefault(key.lower(), c.get("model_name"))
    hit = lookup.get((name or "").lower())
    if hit:
        return hit
    m = _REL_CAPTION_SUFFIX.match(name or "")
    if m:
        base = m.group("base").rstrip()
        hit = lookup.get(base.lower())
        if hit:
            return hit
    return None


def _equality_operands(relationship):
    """Return the two leaf column operands of a single-column ``=`` relationship, else ``None``.

    Only the relationship's SINGLE top-level ``<expression op='='>`` with exactly two ``[Column]``
    leaf operands is accepted. A composite predicate (an ``AND``/``OR`` wrapper, multiple top-level
    expressions, a calculated operand, or any non-equality op) returns ``None`` so the caller warns
    and skips rather than silently emitting only one arm of a multi-column join.
    """
    tops = _children_local(relationship, "expression")
    if len(tops) != 1:
        return None
    expr = tops[0]
    if (expr.get("op") or "") != "=":
        return None
    kids = _children_local(expr, "expression")
    if len(kids) != 2:
        return None
    if not all((k.get("op") or "").startswith("[") for k in kids):
        return None  # an operand is a nested/calculated expression, not a bare [Column]
    return kids[0].get("op"), kids[1].get("op")


def _extract_relationships(datasource, relations):
    """Parse ``<object-graph><relationships>`` into ``[{from_table, from_col, to_table, to_col}]``.

    Endpoints are resolved to emitted table display names and operands to emitted model column
    names; a relationship is emitted ONLY when both tables and both columns resolve to real emitted
    identifiers (operand order is validated, swapping if the authored order is reversed). Anything
    that can't be resolved cleanly -- unknown endpoint, composite/calculated key, a column that
    isn't an emitted column -- is skipped and recorded in the returned warnings list (kept OUT of
    ``unsupported_reasons`` so a fuzzy relationship never forces the whole datasource to fall back).

    Returns ``(relationships, warnings)``.
    """
    oid_to_table = _object_table_map(datasource, relations)
    cols_index = _columns_index(relations)
    out, warnings, seen = [], [], set()
    for og in _findall_object_graph(datasource):
        for rship in _findall_local(og, "relationship"):
            fep = _findall_local(rship, "first-end-point")
            sep = _findall_local(rship, "second-end-point")
            if not fep or not sep:
                warnings.append("relationship is missing an end-point; skipped")
                continue
            from_table = oid_to_table.get(fep[0].get("object-id"))
            to_table = oid_to_table.get(sep[0].get("object-id"))
            if not from_table or not to_table:
                warnings.append(
                    "relationship endpoint did not resolve to a parsed table "
                    f"({fep[0].get('object-id')!r} / {sep[0].get('object-id')!r}); skipped")
                continue
            ops = _equality_operands(rship)
            if not ops:
                warnings.append(
                    f"relationship '{from_table}'<->'{to_table}' is not a single-column equality "
                    "(composite / calculated / non-'=' predicate); skipped")
                continue
            op1, op2 = ops
            from_cols = cols_index.get(from_table.lower(), [])
            to_cols = cols_index.get(to_table.lower(), [])

            def _orient(a, b):
                fc = _resolve_rel_column(a, from_cols)
                tc = _resolve_rel_column(b, to_cols)
                return (fc, tc) if (fc and tc) else None

            # Tableau does not pin operand order to end-point order, so resolve BOTH orientations.
            forward = _orient(op1, op2)              # op1 on from-table, op2 on to-table
            reverse = _orient(op2, op1)              # authored in reverse order
            if forward and reverse and forward != reverse:
                # Both readings resolve to DIFFERENT column pairs (e.g. both keys exist on both
                # tables): genuinely ambiguous -> skip rather than pick a possibly-wrong pairing.
                warnings.append(
                    f"relationship '{from_table}'<->'{to_table}' columns ({op1} / {op2}) are "
                    "ambiguous (both orientations resolve differently); skipped")
                continue
            resolved = forward or reverse
            if not resolved:
                warnings.append(
                    f"relationship '{from_table}'<->'{to_table}' columns ({op1} / {op2}) did "
                    "not resolve to emitted columns; skipped")
                continue
            from_col, to_col = resolved
            dedup = (from_table.lower(), from_col.lower(), to_table.lower(), to_col.lower())
            if dedup in seen:
                continue
            seen.add(dedup)
            out.append({"from_table": from_table, "from_col": from_col,
                        "to_table": to_table, "to_col": to_col})
    return out, warnings


class AmbiguousDatasourceError(ValueError):
    """Raised when a workbook exposes more than one real datasource and none was selected.

    The message lists the available datasource labels so a caller (or agent) can re-invoke with an
    explicit ``select=`` (``parse_tds``/``extract_calcs``) or ``datasource=`` (``migrate_datasource``)
    choice. A single-datasource ``.tds`` never triggers this.
    """


def _is_substantive_datasource(ds):
    """True if a ``<datasource>`` is a real definition (not a worksheet-level reference stub).

    A ``.twb`` repeats each datasource as a lightweight ``<datasource name='...' />`` reference inside
    every worksheet/dashboard that uses it. Those stubs carry no ``<connection>`` and no ``<column>``
    -- only the top-level definition under ``<datasources>`` does. We treat a datasource as
    substantive when it has a direct ``<connection>`` child OR any ``<column>`` children, which keeps
    the genuine definitions (including the ``Parameters`` pseudo-datasource, filtered separately) and
    drops the empty reference stubs that would otherwise show up as duplicate, column-less entries.
    """
    children = list(ds)
    if any(_local(c.tag) == "connection" for c in children):
        return True
    return any(_local(c.tag) == "column" for c in children)


def _is_parameters_datasource(ds):
    """True for Tableau's ``Parameters`` pseudo-datasource (never a migration target).

    Tableau emits parameters in a fixed datasource named exactly ``Parameters`` that carries no
    ``<connection>`` and only ``<column param-domain-type=...>`` entries. Matched primarily by that
    reserved name, with a structural fallback (no connection child + only parameter columns) so an
    oddly-named export is still recognized and skipped.
    """
    if (ds.get("name") or "") == "Parameters":
        return True
    cols = _children_local(ds, "column")
    if not cols:
        return False
    has_conn_child = any(_local(c.tag) == "connection" for c in list(ds))
    all_params = all((c.get("param-domain-type") or "").strip() for c in cols)
    return (not has_conn_child) and all_params


def _datasource_label(ds):
    """The human-facing label for a datasource: caption, else formatted-name, else internal name."""
    return ds.get("caption") or ds.get("formatted-name") or ds.get("name") or ""


def _real_datasources(root):
    """The selectable (non-Parameters) ``<datasource>`` elements of a workbook/datasource document.

    A document whose root IS a ``<datasource>`` (an exported ``.tds``) yields just that element. A
    workbook (``.twb``) yields every embedded datasource that is a real definition -- skipping the
    ``Parameters`` pseudo-datasource and the empty per-worksheet reference stubs -- de-duplicated by
    internal ``name`` (which is unique per workbook) so a datasource used on many sheets is returned
    once, in document order.
    """
    if _local(root.tag) == "datasource":
        return [root]
    out, seen = [], set()
    for ds in _findall_local(root, "datasource"):
        if _is_parameters_datasource(ds) or not _is_substantive_datasource(ds):
            continue
        key = ds.get("name") or id(ds)
        if key in seen:
            continue
        seen.add(key)
        out.append(ds)
    return out


def _choose_datasource(root, select=None):
    """Select one ``<datasource>`` from a parsed document, skipping the ``Parameters`` pseudo-source.

    ``select`` (a caption / formatted-name / internal name, case-insensitive) picks a specific
    datasource and raises ``AmbiguousDatasourceError`` if it matches none. With no ``select`` the
    first real datasource is returned -- so a single-datasource workbook is unambiguous -- and the
    caller (``migrate_datasource`` / ``list_workbook_datasources``) is responsible for prompting on
    a genuine multi-datasource ambiguity.
    """
    real = _real_datasources(root)
    if not real:
        # No real datasource (only Parameters, or an unexpected shape): fall back to the raw root.
        all_ds = [] if _local(root.tag) == "datasource" else _findall_local(root, "datasource")
        return root if _local(root.tag) == "datasource" else (all_ds or [root])[0]
    if select is not None:
        want = str(select).strip().lower()
        for ds in real:
            labels = {(ds.get("caption") or "").lower(),
                      (ds.get("formatted-name") or "").lower(),
                      (ds.get("name") or "").lower()}
            if want in {lbl for lbl in labels if lbl}:
                return ds
        avail = ", ".join(repr(_datasource_label(ds)) for ds in real)
        raise AmbiguousDatasourceError(
            f"no datasource named {select!r} in this workbook; available: {avail}")
    return real[0]


def workbook_datasources(xml_text):
    """List the selectable datasources in a ``.tds``/``.twb`` document (Parameters excluded).

    Returns ``[{"name", "caption", "label", "connection_class", "named_connection_count",
    "table_count"}]`` -- the lightweight inventory an agent shows so a user can pick which datasource
    to migrate from a multi-datasource workbook. ``label`` is the value to pass back as ``select=``.
    """
    root = ET.fromstring(xml_text)
    out = []
    for ds in _real_datasources(root):
        cls, _server, _db, _wh, _hp, _auth, nconns = _live_connection(ds)
        cols_by_parent = _columns_by_parent(ds)
        nc_map = _named_connection_map(ds)
        relations = _extract_relations(ds, cols_by_parent, nc_map)
        tables = [r for r in relations if r.get("kind") in ("table", "custom_sql")]
        out.append({
            "name": ds.get("name"),
            "caption": ds.get("caption"),
            "label": _datasource_label(ds),
            "connection_class": cls,
            "named_connection_count": nconns,
            "table_count": len(tables),
        })
    return out


def parse_tds(xml_text, select=None):
    """Parse Tableau ``.tds``/``.twb`` XML into a normalized connection descriptor (dict).

    The descriptor is JSON-serializable (suitable for a migration report) and contains NO
    credentials. ``unsupported_reasons`` collects shape problems found during parsing so the
    storage-mode policy can fall back cleanly. Additive context keys: ``connections`` (named-
    connection id -> non-secret routing facts), ``relationships`` (inferred table->table joins from
    the object graph), and ``relationship_warnings`` (relationships that could not be resolved).

    For a workbook (``.twb``) with several embedded datasources the ``Parameters`` pseudo-datasource
    is always skipped and the first real datasource is used; pass ``select=`` (caption / name) to
    target a specific one (raises ``AmbiguousDatasourceError`` if it matches none).
    """
    root = ET.fromstring(xml_text)
    datasource = _choose_datasource(root, select)

    cls, server, dbname, warehouse, http_path, auth_method, nconns = _live_connection(datasource)
    cols_by_parent = _columns_by_parent(datasource)
    nc_map = _named_connection_map(datasource)

    relations = _extract_relations(datasource, cols_by_parent, nc_map)
    relationships, relationship_warnings = _extract_relationships(datasource, relations)
    ff_filename, ff_directory = _flatfile_location(datasource)

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
        "warehouse": warehouse,
        "http_path": http_path,
        "auth_method": auth_method,
        "is_extract": is_extract,
        "named_connection_count": nconns,
        "connections": nc_map,
        "flatfile_filename": ff_filename,
        "flatfile_directory": ff_directory,
        "flatfile_path": _flatfile_join(ff_directory, ff_filename),
        "relations": relations,
        "relationships": relationships,
        "relationship_warnings": relationship_warnings,
        "logical_fields": _logical_fields(datasource),
        "unsupported_reasons": unsupported,
    }


def extract_calcs(xml_text, select=None):
    """Pull Tableau calculated fields from a ``.tds``/``.twb`` as ``[{"name", "formula", "role"}]``.

    This is the calc list the assembler's ``calcs=`` argument expects, so a caller can go straight
    from a downloaded ``.tds`` to a model *with measures* without hand-parsing the XML::

        calcs = extract_calcs(tds_text)
        out = migrate_tds_to_semantic_model(tds_text, model_name="X", calcs=calcs)

    A calculated field is a logical ``<column>`` whose nested ``<calculation class='tableau'>``
    carries a ``formula``. Excluded on purpose:

    * **Parameters** -- a ``<column>`` with a ``param-domain-type``; the migration handles
      ``[Parameters].[X]`` references separately (they become preserved ``= 0`` stubs).
    * **Non-formula calculations** -- bins / groups / sets, whose ``<calculation class>`` is not
      ``tableau`` (e.g. ``bin`` / ``categorical-bin``) and which carry no ``formula``.

    The field name is the user-facing ``caption`` (falling back to the de-bracketed internal
    ``name``); the Tableau ``role`` (``dimension`` / ``measure``) is carried through when present.
    The de-bracketed internal ``Calculation_*`` name -- what OTHER calcs reference -- is included
    as ``internal_name`` when it differs from ``name``, so cross-calc references resolve downstream.
    Formula text comes back already XML-unescaped (``&gt;`` -> ``>`` etc.), ready for the translator.
    Names are de-duplicated case-insensitively, keeping the first occurrence.

    ``select`` chooses a datasource by caption/name in a multi-datasource workbook (Parameters is
    always skipped); without it the first real datasource is used.
    """
    root = ET.fromstring(xml_text)
    datasource = _choose_datasource(root, select)
    out = []
    seen = set()
    for col in _children_local(datasource, "column"):
        if (col.get("param-domain-type") or "").strip():
            continue  # a parameter, not a calculated field
        formula = None
        for c in _children_local(col, "calculation"):
            if (c.get("class") or "").strip().lower() == "tableau" and c.get("formula") is not None:
                formula = c.get("formula")
                break
        if formula is None or not formula.strip():
            continue
        internal = _strip_brackets((col.get("name") or "").strip())
        name = (col.get("caption") or "").strip() or internal
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        entry = {"name": name, "formula": formula}
        # The internal Calculation_* name is what OTHER calcs reference in their formulas
        # (a caption is for display only). Carry it so cross-calc references -- e.g. an
        # argmax calc pointing at a separate "max" calc -- can be resolved downstream.
        if internal and internal.lower() != key:
            entry["internal_name"] = internal
        role = (col.get("role") or "").strip()
        if role:
            entry["role"] = role
        out.append(entry)
    return out


# -- M / TMDL emission ---------------------------------------------------------
_PARAM_META = 'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'


def emit_connection_parameters(descriptor):
    """Emit ``expression Server``/``Database``/``Warehouse``/``HttpPath`` parameter TMDL for a
    relational descriptor.

    Returns an empty string when there is no server/database (e.g. flat files), so callers can
    concatenate unconditionally. ``Database`` is emitted only when it is an actual connect
    argument (the ``(server, database)`` family); a server-only connector (Oracle) reaches its
    database through the server string, while Snowflake and Databricks reach it by navigation, so
    no unused ``#"Database"`` parameter is carried for them. ``Warehouse`` is emitted for
    Snowflake; ``HttpPath`` for Databricks (read from whichever driver/connector attribute carries
    it -- see ``_HTTP_PATH_ATTRS``; a real ``.tds`` typically does carry the SQL-warehouse HTTP
    path, but it may be absent on some exports, in which case the parameter is emitted empty and
    requires manual completion).
    """
    spec = connector_spec(descriptor.get("connection_class"))
    connect_style = spec[1] if spec else None
    no_database = ("server_only", "server_warehouse", "server_httppath")
    lines = []
    if descriptor.get("server"):
        lines.append(f'expression Server = "{escape_m_string(descriptor["server"])}" {_PARAM_META}\n')
    if descriptor.get("database") and connect_style not in no_database:
        lines.append(f'expression Database = "{escape_m_string(descriptor["database"])}" {_PARAM_META}\n')
    if connect_style == "server_warehouse":
        raw_warehouse = (descriptor.get("warehouse") or "").strip()
        warehouse = escape_m_string(raw_warehouse)
        wh_line = f'expression Warehouse = "{warehouse}" {_PARAM_META}\n'
        if not raw_warehouse:
            # The .tds carried no compute warehouse (Snowflake stores it as warehouse=''). Keep the
            # #"Warehouse" parameter so Snowflake.Databases(#"Server", #"Warehouse") stays a valid
            # call, but attach a TMDL description (///, documented + deploy-safe) flagging that an
            # empty warehouse cannot run queries and must be set before refresh. Combined into one
            # element so the description sits immediately above the expression it annotates.
            wh_line = (
                '/// TODO: the Snowflake warehouse was empty in the .tds; set #"Warehouse" '
                "to a valid compute warehouse before refresh\n" + wh_line)
        lines.append(wh_line)
    if connect_style == "server_httppath":
        http_path = escape_m_string(descriptor.get("http_path") or "")
        lines.append(f'expression HttpPath = "{http_path}" {_PARAM_META}\n')
    return "\n".join(lines)


def _m_mode_keyword(mode):
    return "directQuery" if (mode or "").lower() == "directquery" else "import"


def _scaffold_source(cls, intended, detail):
    """Return a clearly-flagged, valid-but-incomplete partition source.

    Used for any connector/relation we will not auto-emit. It must be DEPLOY-valid TMDL, not
    merely structurally present: the body is a SINGLE ``let ... in`` expression (the ``// TODO``
    note lives INSIDE the block, so it is one expression with one child, never a bare comment
    sibling that the TMDL parser rejects with ``UnknownKeyword: 'let' is not a supported child
    object``). The ``Source`` is an empty typed table, so even refreshing an un-completed scaffold
    yields an empty table rather than a null-conversion error -- strictly better than the prior
    ``Source = null`` while still obviously a stub that names its intended connector as a hint.
    """
    hint = f" using {intended}" if intended else ""
    return (
        "let\n"
        f"\t\t\t\t// TODO: complete the M partition for connector class "
        f"'{cls or 'unknown'}'{hint} ({detail})\n"
        "\t\t\t\tSource = #table(type table [], {})\n"
        "\t\t\tin\n"
        "\t\t\t\tSource"
    )


# Flat-file column types: a TMDL dataType -> the Power Query ascription used in
# Table.TransformColumnTypes. (``Int64.Type`` is the M type value for a 64-bit integer; the rest
# use the ``type <primitive>`` form.)
_M_TYPE = {
    "int64": "Int64.Type",
    "double": "type number",
    "decimal": "type number",
    "dateTime": "type datetime",
    "boolean": "type logical",
    "string": "type text",
}


def _excel_sheet_name(relation):
    """The Excel sheet name to navigate for a relation (``[Orders$]`` -> ``Orders``).

    Tableau exposes a worksheet as ``[<sheet>$]`` (the ODBC sheet convention); Power Query's
    ``Excel.Workbook`` navigation keys the sheet by its bare name with ``Kind="Sheet"``.
    """
    raw = relation.get("raw_table") or relation.get("item") or relation.get("name") or ""
    s = _strip_brackets(raw).strip()
    return s[:-1] if s.endswith("$") else s


def _flatfile_path_for(conn):
    """Resolve the flat-file path from either a descriptor (single-connection) or a per-connection
    facts dict (federated). A driver-set absolute ``flatfile_path`` wins; otherwise it's rebuilt
    from the captured filename/directory."""
    return conn.get("flatfile_path") or _flatfile_join(
        conn.get("flatfile_directory") or conn.get("directory"),
        conn.get("flatfile_filename") or conn.get("filename"))


def extract_bundled_flatfile(packaged_source, descriptor, dest_dir):
    """Lift a packaged datasource's BUNDLED flat-file (Excel/CSV) out to an ABSOLUTE on-disk path.

    A Tableau ``.tdsx``/``.twbx`` is a zip that bundles its flat-file data under ``Data/`` while the
    ``<connection>`` element stores only a path RELATIVE to the workbook (e.g.
    ``Data/Datasources/Sample - Superstore.xlsx``). Power BI's ``File.Contents`` rejects a relative
    path -- *"The supplied file path must be a valid absolute path"* -- so an Import model emitted
    straight from that relative path OPENS but loads NO data. This copies the bundled member to an
    absolute location the emitted M can read, so the ``.pbip`` opens AND loads.

    Returns the absolute path of the extracted file, or ``None`` -- in which case the caller keeps the
    existing (relative) path, i.e. behavior is UNCHANGED. ``None`` is returned whenever there is
    nothing to extract: a live database connection (Snowflake/Databricks/SQL Server/... carries no
    bundled file, so ``flatfile_filename`` is absent); ``packaged_source`` is not a zip (a bare
    ``.tds``/``.twb`` XML path or in-memory XML text); or the member is missing/ambiguous. The helper
    is fail-closed and never raises.
    """
    import io as _io
    import os as _os
    import zipfile as _zip

    filename = (descriptor or {}).get("flatfile_filename")
    if not filename:  # not a flat-file source (live DB / federated SQL) -> nothing to extract
        return None

    raw = None
    if isinstance(packaged_source, (bytes, bytearray)):
        raw = bytes(packaged_source)
    else:
        try:
            p = _os.fspath(packaged_source)
        except TypeError:
            p = None
        if isinstance(p, str) and "\n" not in p and "<" not in p:
            try:
                if _os.path.isfile(p):
                    with open(p, "rb") as fh:
                        raw = fh.read()
            except (OSError, ValueError):
                raw = None
    if not raw or raw[:2] != b"PK":  # not a zip archive (.tdsx/.twbx) -> keep the relative path
        return None

    directory = (descriptor or {}).get("flatfile_directory") or ""
    rel = (directory.rstrip("/\\") + "/" + filename) if directory else filename
    rel_norm = rel.replace("\\", "/").lstrip("./").lower()
    base_norm = _os.path.basename(filename.replace("\\", "/")).lower()
    try:
        with _zip.ZipFile(_io.BytesIO(raw)) as zf:
            member = None
            for n in zf.namelist():  # exact relative-path match first (most precise)
                if n.replace("\\", "/").lower() == rel_norm:
                    member = n
                    break
            if member is None:  # fall back to a UNIQUE basename match only (never guess)
                cands = [n for n in zf.namelist()
                         if _os.path.basename(n.replace("\\", "/")).lower() == base_norm]
                if len(cands) == 1:
                    member = cands[0]
            if member is None:
                return None
            data = zf.read(member)
    except Exception:  # fail-closed: any zip/read problem -> keep the relative path unchanged
        return None

    try:
        _os.makedirs(dest_dir, exist_ok=True)
        out_path = _os.path.join(dest_dir, _os.path.basename(filename.replace("\\", "/")))
        with open(out_path, "wb") as fh:
            fh.write(data)
    except OSError:
        return None
    return _os.path.abspath(out_path)


def emit_flatfile_source(relation, conn, cls):
    """Emit a real, typed Import ``let ... in`` body for an Excel/CSV ("full data") relation.

    Builds a deterministic, deploy-ready Power Query: read the file, promote the header row, set
    each column's type from the parsed Tableau metadata, and rename the promoted headers to the
    model column names (``clean_col``) so they match each column's ``sourceColumn`` in the TMDL.
    Returns ``None`` (caller falls back to a scaffold) when the file path or columns are unknown,
    so a flat file we can't fully resolve is never emitted as a silently-empty partition.

    A per-RELATION ``flatfile_path`` (set by the local-POC import path, where each table maps to its
    OWN local CSV extracted from the ``.hyper``) takes precedence over the datasource-level path, so
    a multi-table extract can point each partition at a different CSV. Absent that key the behavior
    is unchanged (the datasource-level path is used).
    """
    path = relation.get("flatfile_path") or _flatfile_path_for(conn)
    cols = relation.get("columns") or []
    connector = FLAT_FILE_CLASSES.get((cls or "").lower())
    if not path or not cols or connector is None:
        return None

    p = escape_m_string(path)
    steps = []
    if connector == "Excel.Workbook":
        sheet = escape_m_string(_excel_sheet_name(relation))
        steps.append(f'Source = Excel.Workbook(File.Contents("{p}"), null, true)')
        steps.append(f'Navigation = Source{{[Item="{sheet}", Kind="Sheet"]}}[Data]')
        steps.append("Promoted = Table.PromoteHeaders(Navigation, [PromoteAllScalars=true])")
    else:  # Csv.Document
        steps.append(
            f'Source = Csv.Document(File.Contents("{p}"), '
            '[Delimiter=",", Encoding=1252, QuoteStyle=QuoteStyle.Csv])')
        steps.append("Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])")
    prev = "Promoted"

    # Type by the RAW promoted header (the Tableau remote name), then rename to the model name so
    # the query output column names equal each TMDL column's sourceColumn (clean_col of remote).
    type_pairs, rename_pairs = [], []
    for c in cols:
        remote = c.get("remote_name") or c["model_name"]
        mt = _M_TYPE.get(c["tmdl_type"])
        if mt:
            type_pairs.append(f'{{"{escape_m_string(remote)}", {mt}}}')
        if remote != c["model_name"]:
            rename_pairs.append(
                f'{{"{escape_m_string(remote)}", "{escape_m_string(c["model_name"])}"}}')
    if type_pairs:
        steps.append(f"Typed = Table.TransformColumnTypes({prev}, {{{', '.join(type_pairs)}}})")
        prev = "Typed"
    if rename_pairs:
        steps.append(f"Renamed = Table.RenameColumns({prev}, "
                     f"{{{', '.join(rename_pairs)}}}, MissingField.Ignore)")
        prev = "Renamed"

    body = ",\n\t\t\t\t".join(steps)
    return f"let\n\t\t\t\t{body}\n\t\t\tin\n\t\t\t\t{prev}"


def _native_query_rename_pairs(relation):
    """Build ``Table.RenameColumns`` pairs that map each native-query output column (the true
    remote name, e.g. ``Order ID`` / ``Country/Region``) to its model name (``sourceColumn`` =
    ``clean_col`` of the remote, e.g. ``Order_ID`` / ``Country_Region``).

    This is the SAME remote->model rename the flat-file path already emits (see
    ``emit_flatfile_source``): a native query returns the raw source headers, so without it the
    underscored model columns wouldn't bind. Pairs where the remote name already equals the model
    name are skipped, so a query whose columns need no aliasing yields NO rename step and the
    emitted M is byte-identical to the pre-rename form (the no-op guarantee).
    """
    pairs = []
    for c in (relation.get("columns") or []):
        remote = c.get("remote_name")
        model = c.get("model_name")
        if remote and model and remote != model:
            pairs.append(
                f'{{"{escape_m_string(remote)}", "{escape_m_string(model)}"}}')
    return pairs


def _connect_expr(connector, connect_style):
    """Build the right-hand side of ``Source = ...`` for a fully-supported connector.

    Exhaustive on ``connect_style`` -- an unrecognized style raises rather than silently falling
    back to the ``(server, database)`` form (which would emit wrong M for a different connector).
    """
    if connect_style == "server_database":  # SQL Server protocol family
        return f'{connector}(#"Server", #"Database")'
    if connect_style == "server_only":
        # Oracle: the service/SID lives in #"Server" and there is no separate database argument;
        # HierarchicalNavigation defaults false, so we set it explicitly so the flat Schema/Item
        # selector is correct rather than default-reliant.
        return f'{connector}(#"Server", [HierarchicalNavigation=false])'
    if connect_style == "server_warehouse":
        return f'{connector}(#"Server", #"Warehouse")'
    if connect_style == "server_httppath":  # Databricks SQL warehouse (host, httpPath)
        return f'{connector}(#"Server", #"HttpPath")'
    raise ValueError(f"unhandled connect_style {connect_style!r} for connector {connector!r}")


def _effective_connection(relation, descriptor):
    """Return the connection facts to bind THIS relation against.

    For a federated datasource with MORE THAN ONE named connection, each relation routes to its OWN
    upstream connection (so a per-table connector function / navigation is chosen from the relation's
    own class + database). For the single-connection case the global descriptor is returned
    unchanged, so emitted M is byte-identical to the pre-routing behavior.

    NOTE: the shared ``#"Server"`` / ``#"Database"`` / ``#"Warehouse"`` / ``#"HttpPath"`` parameters
    are still emitted once per datasource by ``emit_connection_parameters``; full multi-connection
    deployment additionally needs per-connection parameters. Multi-connection sources are routed to
    the land-to-Delta fallback by ``select_storage_mode`` today, so this routing is groundwork that
    is never the deployed artifact on its own.
    """
    if descriptor.get("named_connection_count", 1) > 1 and relation.get("connection"):
        return relation["connection"]
    return descriptor


def emit_m_partition_source(relation, descriptor, mode):
    """Emit the ``source = let ... in ...`` body for one relation's M partition.

    Deploy-ready, doc-verified M is emitted for the connectors in ``DIRECT_CONNECTORS`` (each
    with its own connect signature + navigation); any other connector returns a clearly-commented
    scaffold so the structure is valid TMDL but obviously needs manual completion (never silently
    wrong). Custom SQL is emitted deploy-ready for the ``(server, database)`` family (where
    ``Value.NativeQuery`` folds against the database handle) and for connectors in
    ``NATIVE_QUERY_CATALOG_DRILL`` (where it folds against a drilled ``Kind="Database"`` handle);
    everything else scaffolds. For a multi-connection federated source each relation is bound
    against its OWN connection (see ``_effective_connection``).

    Thin wrapper over ``_emit_m_partition_review`` returning only the M body. Callers that also
    need to know whether the body is a needs-manual-completion scaffold use
    ``m_partition_review_reason`` (same inputs), so this function's return value and output stay
    byte-for-byte unchanged.
    """
    return _emit_m_partition_review(relation, descriptor, mode)[0]


def m_partition_review_reason(relation, descriptor, mode):
    """Return the human-readable reason this relation's partition is a needs-manual-completion
    scaffold, or ``None`` when a real, deploy-ready partition was emitted.

    Lets the model assembler fail LOUD at build time -- counting stubbed partitions and listing
    them in ``needs_review`` -- instead of a scaffold silently passing the build and only failing
    at deploy. Pure function of the same inputs as ``emit_m_partition_source``.
    """
    return _emit_m_partition_review(relation, descriptor, mode)[1]


def _scaffold_review(cls, intended, detail):
    """``(scaffold_source, reason)`` pair so each scaffold site reports why it stubbed."""
    return _scaffold_source(cls, intended, detail), detail


def _emit_m_partition_review(relation, descriptor, mode):
    """Core of ``emit_m_partition_source``: return ``(source, stub_reason)`` where ``stub_reason``
    is ``None`` for a real, deploy-ready partition and a short explanation for a scaffold."""
    conn = _effective_connection(relation, descriptor)
    cls = (conn.get("connection_class") or "").lower()
    if cls in ANALYSIS_SERVICES_CLASSES:
        # SSAS / MSOLAP is already a tabular/multidimensional model -- never emit a naive M
        # partition for it; flag it for the separate model-migration path.
        return _scaffold_review(
            cls, None,
            "Microsoft Analysis Services is already a tabular/multidimensional semantic model; "
            "migrate the model directly (XMLA endpoint / semantic-model import), not as an M partition")
    spec = connector_spec(cls)
    if spec is None:
        if cls in FLAT_FILE_CLASSES:
            flat = emit_flatfile_source(relation, conn, cls)
            if flat is not None:
                return flat, None
        intended = PARTIAL_LIVE_CONNECTORS.get(cls) or FLAT_FILE_CLASSES.get(cls)
        if cls in PARTIAL_LIVE_CONNECTORS:
            detail = "recognized connector, but its navigation/identifiers aren't verified offline; complete manually"
        elif cls in FLAT_FILE_CLASSES:
            detail = f"flat-file source; set the file path (and sheet/range) for the {intended} partition"
        else:
            detail = "connector class not mapped for direct M; route to land-to-Delta + DirectLake"
        return _scaffold_review(cls, intended, detail)

    connector, connect_style, nav_style = spec

    if relation["kind"] == "custom_sql":
        sql = escape_m_string(relation.get("sql", ""))
        if connect_style == "server_database":
            # SQL Server family: Value.NativeQuery folds against the database handle directly.
            steps = [f'Source = {connector}(#"Server", #"Database")']
            nq_target = "Source"
        elif nav_style == "database_schema_table" and cls in NATIVE_QUERY_CATALOG_DRILL:
            # Databricks (live-verified): the connector's ROOT collection rejects native queries
            # ("Native queries aren't supported by this value"), so we MUST drill to a
            # Kind="Database" handle first and fold the native query against THAT handle -- never
            # against the Catalogs() root. The catalog comes from the relation's three-part name
            # when present, else the connection's database; without it we can't drill, so scaffold.
            database = relation.get("catalog") or conn.get("database")
            if not database:
                return _scaffold_review(
                    cls, connector,
                    "custom SQL needs the catalog/database for the native-query drill; "
                    "not resolvable from this .tds")
            steps = [
                f'Source = {_connect_expr(connector, connect_style)}',
                f'Catalog = Source{{[Name="{escape_m_string(database)}", Kind="Database"]}}[Data]',
            ]
            nq_target = "Catalog"
        else:
            return _scaffold_review(
                cls, connector,
                "custom SQL native query for this connector isn't verified; complete it manually")
        # EnableFolding lets DirectQuery push the native query down to the source.
        steps.append(
            f'Result = Value.NativeQuery({nq_target}, "{sql}", null, [EnableFolding=true])')
        prev = "Result"
        # Align native-query output (raw remote headers) to each column's sourceColumn. No-ops
        # (emits no step) when every remote name already equals its model name.
        rename_pairs = _native_query_rename_pairs(relation)
        if rename_pairs:
            steps.append(f"Renamed = Table.RenameColumns({prev}, "
                         f"{{{', '.join(rename_pairs)}}}, MissingField.Ignore)")
            prev = "Renamed"
        body = ",\n\t\t\t\t".join(steps)
        # The operators are already de-escaped at parse, so a real native query is emitted. The
        # one thing we cannot complete is a recovered Tableau parameter reference
        # (<Parameters.[Name]>): the source can't run it and we don't translate it to a Power
        # Query parameter yet, so flag it for review (the partition is still emitted) rather than
        # ship a query that fails at refresh.
        param_reason = None
        params = custom_sql_parameter_refs(relation.get("sql", ""))
        if params:
            param_reason = (
                "custom SQL contains Tableau parameter reference(s) "
                f"{', '.join(params)} that are not translated to a Power Query parameter; "
                "replace them with a literal or a bound parameter before refresh")
        return f"let\n\t\t\t\t{body}\n\t\t\tin\n\t\t\t\t{prev}", param_reason

    source = _connect_expr(connector, connect_style)

    if nav_style == "database_schema_table":
        # Snowflake / Databricks: database(or catalog) -> schema -> table, each hop keyed by
        # [Name, Kind] (the catalog level is keyed Kind="Database"). The catalog comes from the
        # relation's three-part [catalog].[schema].[item] name when present, else the connection's
        # database; without the catalog + schema the navigation can't be resolved, so we scaffold
        # rather than guess.
        database = relation.get("catalog") or conn.get("database")
        schema = relation.get("schema")
        item = relation["item"]
        if not database or not schema:
            return _scaffold_review(
                cls, connector,
                f"{connector} navigation needs the database/catalog + schema names; "
                "not resolvable from this .tds")
        db, sch, it = escape_m_string(database), escape_m_string(schema), escape_m_string(item)
        return (
            "let\n"
            f'\t\t\t\tSource = {source},\n'
            f'\t\t\t\tDb = Source{{[Name="{db}", Kind="Database"]}}[Data],\n'
            f'\t\t\t\tSchema = Db{{[Name="{sch}", Kind="Schema"]}}[Data],\n'
            f'\t\t\t\tData = Schema{{[Name="{it}", Kind="Table"]}}[Data]\n'
            "\t\t\tin\n"
            "\t\t\t\tData"
        ), None

    if nav_style != "schema_item":
        raise ValueError(f"unhandled nav_style {nav_style!r} for connector {connector!r}")

    # schema_item: flat ADO.NET navigation (SQL Server family + Oracle). These bind one database via
    # Sql.Database(server, database) (or reach it through the server string), so a three-part
    # [catalog].[schema].[item] name whose catalog differs from (or has no) connection database is a
    # cross-database reference we can't scope safely -> scaffold rather than silently query the
    # connection's default database. A catalog that equals the database is just a redundant
    # qualifier and is dropped.
    catalog = relation.get("catalog")
    database = conn.get("database")
    if catalog and (not database or catalog.lower() != database.lower()):
        return _scaffold_review(
            cls, connector,
            f"table is qualified to catalog '{catalog}' but the connection database is "
            f"'{database or '(none)'}'; cross-database references aren't auto-emitted for the "
            f"{connector}(server, database) navigation")
    schema = relation.get("schema") or "dbo"
    item = relation["item"]
    nav = f'Source{{[Schema="{escape_m_string(schema)}", Item="{escape_m_string(item)}"]}}[Data]'
    return (
        "let\n"
        f'\t\t\t\tSource = {source},\n'
        f"\t\t\t\tData = {nav}\n"
        "\t\t\tin\n"
        "\t\t\t\tData"
    ), None


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
        columns_tmdl += generate_column_tmdl(
            c["model_name"], c["tmdl_type"], summarize, False, c.get("format_string"),
            c.get("data_category"))

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

    Two resolution layers, tried in order so existing behavior is byte-for-byte unchanged:

    1. **metadata-record** -- the column's friendly ``local-name`` (SQL Server / extract .tds keep
       a title-case ``[Sales]`` here, so a calc's ``[Sales]`` binds directly).
    2. **logical layer** (case-insensitive) -- consulted only on a miss. A live ``.tds`` over a
       case-sensitive backend stores the physical name verbatim (``SALES``) in the metadata-record
       while the calc references the caption (``[Sales]``); the ``<column caption>`` + ``<cols>``
       map bridges ``[Sales] -> [ORDERS].[SALES]``. Lowercasing the lookup also lets a formula use
       either the caption (``[Sales]``) or the physical/logical id (``[SALES]``). The caption
       disambiguates a physical-name collision that the physical layer alone cannot (``Region`` ->
       ``ORDERS.REGION`` while ``Region (People)`` -> ``PEOPLE.REGION``).
    """
    cap_to = {}   # (table, caption) -> (clean_col, tmdl_type)
    counts = {}   # (table, clean_col) -> set(captions)  (collision detector)
    phys_exact = {}   # (table, remote) -> (table, clean_col, tmdl_type)  -- exact, case-sensitive
    phys_ci = {}      # (lower(table), lower(remote)) -> set of those targets (case collisions)
    for rel in descriptor.get("relations", []):
        if rel.get("kind") not in ("table", "custom_sql"):
            continue
        table = rel.get("name") or rel.get("item")
        for c in rel.get("columns", []):
            cap = c.get("local_name") or c.get("remote_name")
            cc = c["model_name"]
            cap_to[(table, cap)] = (cc, c["tmdl_type"])
            counts.setdefault((table, cc), set()).add(cap)
            remote = c.get("remote_name")
            if table and remote:
                target = (table, cc, c["tmdl_type"])
                phys_exact[(table, remote)] = target
                phys_ci.setdefault((table.strip().lower(), remote.strip().lower()),
                                   set()).add(target)

    tables = {(rel.get("name") or rel.get("item"))
              for rel in descriptor.get("relations", [])
              if rel.get("kind") in ("table", "custom_sql")}

    def _phys_target(table, physical):
        """Resolve a logical map's (table, physical) to the EMITTED relation column target.

        Exact (case-sensitive) match wins; only on an exact miss is a case-insensitive match
        accepted, and ONLY when it is unique (a backend can expose ``ID`` and ``id`` as distinct
        columns, so a case-folded collision must fail closed rather than guess). Returns ``None``
        when nothing provably emitted matches -- never an invented target.
        """
        hit = phys_exact.get((table, physical))
        if hit is not None:
            return hit
        bucket = phys_ci.get((table.strip().lower(), physical.strip().lower()))
        return next(iter(bucket)) if bucket and len(bucket) == 1 else None

    # Logical caption/id -> target, built from the <column caption> + <cols> bridge. A key that
    # maps to more than one distinct target stays ambiguous (fail-closed, never guess). A logical
    # field whose physical column is not provably emitted is dropped (no invented binding).
    logical = {}   # lower(caption|logical_id) -> set of (table, clean_col, tmdl_type)
    for lf in descriptor.get("logical_fields", []):
        target = _phys_target(lf["table"], lf["physical_col"])
        if target is None:
            continue
        for key in (lf["caption"], lf["logical_id"]):
            k = (key or "").strip().lower()
            if k:
                logical.setdefault(k, set()).add(target)

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
        if len(hits) == 1:
            return hits[0]
        # Exact metadata-record resolution was empty OR ambiguous: defer to the logical layer,
        # which is the authoritative disambiguator for a caption / logical-id reference (e.g. a
        # physical ``REGION`` present in two joined tables resolves by the caption ``Region`` ->
        # ORDERS vs ``Region (People)`` -> PEOPLE). Only an unambiguous logical hit binds.
        bucket = logical.get((caption or "").strip().lower())
        if bucket and len(bucket) == 1:
            return next(iter(bucket))
        return None

    return resolve_field


# Power BI "List Item Connections" data-source types keyed by Tableau connector class.
_BIND_TYPE = {
    "sqlserver": "SQL",
    "azure_sqldb": "SQL",
    "azure_sql_dw": "SQL",        # Azure Synapse Analytics binds via the SQL data-source type
    "microsoft_fabric_sql_endpoint": "SQL",   # Fabric Warehouse / Lakehouse SQL endpoint (TDS)
    "postgres": "PostgreSql",
    "oracle": "Oracle",
    "mysql": "MySql",
    "redshift": "AmazonRedshift",
    "teradata": "Teradata",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "bigquery": "GoogleBigQuery",
}


# Non-secret Tableau ``authentication`` label -> Fabric/Power BI credential kind. Used only to
# advise which credential type to configure; we map the labels we can verify and return None
# otherwise rather than guessing. NO secret is ever read -- only the method label.
_AUTH_TO_CREDENTIAL = {
    "username password": "Basic",
    "oauth": "OAuth2",
}


def _fabric_credential_kind(auth_method):
    """Map a Tableau ``authentication`` label to a Fabric credential kind, or None if unknown."""
    if not auth_method:
        return None
    return _AUTH_TO_CREDENTIAL.get(auth_method.strip().lower())


def connection_details_for_bind(descriptor):
    """Return structured connection details for the Bind Semantic Model Connection API.

    A later binding adapter flattens ``path`` per the connector's exact requirement; the
    structured fields are kept so nothing is lost for non-SQL connectors. ``auth_method`` is the
    non-secret Tableau authentication label and ``credential_kind`` is its mapped Fabric credential
    type (advisory only) -- no secret value is ever included.
    """
    cls = (descriptor.get("connection_class") or "").lower()
    server = descriptor.get("server")
    database = descriptor.get("database")
    path = ";".join(p for p in (server, database) if p) or None
    auth_method = descriptor.get("auth_method")
    return {
        "connector": cls or None,
        "bind_type": _BIND_TYPE.get(cls),
        "server": server,
        "database": database,
        "path": path,
        "auth_method": auth_method,
        "credential_kind": _fabric_credential_kind(auth_method),
    }
