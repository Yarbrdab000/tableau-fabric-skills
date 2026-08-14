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
                                tableau_measure_format_to_pbi, tableau_geo_role_to_data_category)
    from .storage_mode import (
        ANALYSIS_SERVICES_CLASSES, CONTAINER_RELATION_TYPES, DIRECT_CONNECTORS, FLAT_FILE_CLASSES,
        NATIVE_ODBC_DRIVER, NATIVE_ODBC_ENGINES,
        NATIVE_QUERY_CATALOG_DRILL, ODBC_CLASSES, PARTIAL_LIVE_CONNECTORS,
        UNION_RELATION_TYPES, connector_spec)
except ImportError:
    from tmdl_generate import (clean_col, generate_column_tmdl, q, tableau_default_format_to_pbi,
                               tableau_measure_format_to_pbi, tableau_geo_role_to_data_category)
    from storage_mode import (
        ANALYSIS_SERVICES_CLASSES, CONTAINER_RELATION_TYPES, DIRECT_CONNECTORS, FLAT_FILE_CLASSES,
        NATIVE_ODBC_DRIVER, NATIVE_ODBC_ENGINES,
        NATIVE_QUERY_CATALOG_DRILL, ODBC_CLASSES, PARTIAL_LIVE_CONNECTORS,
        UNION_RELATION_TYPES, connector_spec)


# -- disambiguated caption resolution -----------------------------------------
# Tableau writes a field whose base name collides across joined objects as ``<Field> (<Object>)``
# (e.g. ``Id (Contact)`` -- Id on the Contact object). When an object model joins the SAME object
# twice (Contact + Contact1), BOTH copies expose the identical disambiguated caption, so within one
# island it resolves to two tables and drops to a stub. The ``(<Object>)`` token is Tableau's own
# disambiguator: matching it to the relation literally named ``<Object>`` reclaims the binding.
# The leading space before ``(`` is required, so this matches ``Field (Object)`` -- NEVER ``SUM(x)``.
_OBJECT_SUFFIX_RE = re.compile(r"^(.+) \((.+)\)$")



def _norm_obj(s):
    """Casefold + strip ALL spaces so a caption's ``(Contact 1)`` object token matches the relation
    ``Contact1`` while an island-renamed ``Contact (Intake)`` never collapses onto ``Contact``.
    Exact match (not a prefix); ``None`` -> ``''`` so a nameless relation never matches an object."""
    return (s or "").replace(" ", "").casefold()


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
    """Escape a string for embedding inside a Power Query M double-quoted literal.

    Doubles ``"`` per M and -- critically for multi-line Custom SQL embedded in
    ``Value.NativeQuery`` / ``Odbc.Query`` -- normalizes line endings to a single LF and emits M's
    character escapes (``#(lf)`` for newline, ``#(tab)`` for tab) so the literal stays on ONE
    physical line. A raw newline would otherwise survive into the surrounding TMDL partition block,
    whose grammar is indentation-significant: interior SQL lines land at column 0 and Fabric rejects
    the file with ``Workload_FailedToParseFile -- Invalid indentation`` (and a source ``\\r\\n``
    would double into a blank line). Identifier callers (server, database, catalog, file paths)
    carry none of these characters, so their output is byte-identical.
    """
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    return s.replace('"', '""').replace("\t", "#(tab)").replace("\n", "#(lf)")


# -- Custom SQL de-escape ------------------------------------------------------
# Tableau serializes a Custom SQL relation by doubling every LITERAL angle bracket in the query
# text ('<' -> '<<', '>' -> '>>'). It is a global, per-character substitution -- it also rewrites
# the inside of line/block comments and string literals -- whose purpose is to escape literal
# brackets so they cannot be confused with Tableau's own parameter syntax. The parameter-reference
# delimiters themselves are NOT doubled: a reference is emitted with SINGLE brackets in the verified
# form <[Parameters].[Name]> (note 'Parameters' is itself bracketed). Tableau halves the doubled
# literals back on read/execute and substitutes the parameter value, so the query that actually
# runs is single-operator and correct. A migration tool that reads the raw .tds XML therefore sees
# the DOUBLED form, and emitting it verbatim corrupts the query: on Spark/Databricks '<<'/'>>' are
# the bitwise shiftleft/shiftright operators, so a comparison predicate like (Profit << 0) fails at
# refresh with DATATYPE_MISMATCH while the deploy itself looks clean. The inverse of a clean
# per-character double is a global halve. Verified against controlled Databricks Superstore
# diagnostic saves: an operator matrix (< <= > >= <> -> << <<= >> >>= <<>>), contamination of
# comment + string-literal text, an all-even bracket-run invariant for literals, an executable
# .hyper (proving Tableau itself halves on read), and a live parameterized save in which a
# 'Min Profit Threshold' parameter serialized as <[Parameters].[Parameter 0014036665946123]> with
# single delimiters between two doubled operators ('Profit >> <[Parameters]...> ... Sales << 5000').
# See resources/migration-gotchas.md.
_TABLEAU_PARAM_REF = re.compile(
    r"<\s*\[?\s*Parameters\s*\]?\s*\.\s*(\[[^\]]+\])\s*>", re.IGNORECASE
)
_DEESCAPED_PARAM_REF = re.compile(
    r"<\s*\[?\s*Parameters\s*\]?\s*\.\s*\[[^\]]+\]\s*>", re.IGNORECASE
)


def _deescape_custom_sql(sql):
    """Reverse Tableau's on-disk angle-bracket doubling for Custom SQL text.

    Apply EXACTLY ONCE, at the .tds parse boundary -- a global halve is NOT idempotent. A
    genuine source ``<<`` (e.g. a real Spark bitwise shift) is stored as ``<<<<`` and a single
    halve correctly recovers ``<<``; a second halve would wrongly collapse it to ``<``. The
    relation descriptor's ``sql`` field is the single canonical home for this, so every
    downstream stage (M emission, profiling, comparison) only ever sees the recovered
    single-operator form.

    Parameter-aware: a Tableau parameter reference (``<[Parameters].[Name]>``) uses angle brackets
    as delimiter syntax and -- unlike literal operators -- is stored with SINGLE brackets. Each
    token is masked out before the halve and restored to its canonical single-bracket form
    afterwards. The mask also prevents a doubled operator sitting flush against a parameter
    delimiter from forming an odd-length run that a blind halve would mangle.
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
        work = work.replace(f"\x00P{i}\x00", f"<[Parameters].{inner}>")
    return work


def custom_sql_parameter_refs(sql):
    """Distinct canonical Tableau parameter tokens (``<[Parameters].[Name]>``) in de-escaped
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


# -- generic ODBC connection string -------------------------------------------
# A generic-ODBC <connection> can carry credentials inline (a real .tds may even hold
# password='...'); we NEVER emit them -- Power BI / the gateway supplies credentials at bind
# time -- so any credential-bearing key is dropped from the connect-string extras before it is
# emitted. Names are matched case-insensitively, surrounding whitespace ignored.
_ODBC_SECRET_KEYS = frozenset({
    "uid", "user", "userid", "username", "pwd", "passwd", "password",
    "token", "accesstoken", "apikey", "api_key", "auth", "authentication",
    "accesskeyid", "secretaccesskey", "secretkey", "awsaccesskeyid",
    "awssecretkey", "sessiontoken",
})


def _scrub_odbc_extras(extras):
    """Drop any credential-bearing ``key=value`` pairs from an odbc-connect-string-extras string.

    The extras are semicolon-separated ``key=value`` pairs. A pair whose key (case-insensitive,
    trimmed) is in ``_ODBC_SECRET_KEYS`` is removed entirely, so a ``UID``/``PWD``/``Token`` a user
    left inline in the .tds never lands in the emitted M. Non-secret pairs are preserved verbatim
    in their original order. Returns ``""`` for empty/None input.
    """
    if not extras:
        return ""
    kept = []
    for part in extras.split(";"):
        if not part.strip():
            continue
        key = part.split("=", 1)[0].strip().lower()
        if key in _ODBC_SECRET_KEYS:
            continue
        kept.append(part.strip())
    return ";".join(kept)


def _odbc_connection_string(conn):
    """Reconstruct a Power Query ODBC connection string from NON-SECRET ``.tds`` facts, or None.

    * DSN-based (the .tds carries ``odbc-dsn``) -> ``dsn=<DSN>`` (the DSN encapsulates the driver
      and host, so no server/port/database clauses are added).
    * Driver-based -> ``Driver={<driver>};Server=<server>;Port=<port>;Database=<db>`` -- each
      clause only when its value is present.
    * A native query engine (Spark/Presto/Trino/Starburst) carries NO ``odbc-driver`` attribute (it
      used Tableau's bundled driver), so the per-engine default driver name (``NATIVE_ODBC_DRIVER``)
      is substituted -- flagged confirm-required by the storage-mode follow-up -- and the same
      ``Driver={...};Server=...`` string is built from its server/port/catalog.

    The non-secret ``odbc-connect-string-extras`` are appended after a credential scrub. Returns
    ``None`` when the .tds carries NEITHER a DSN nor a driver (nothing to bind), so the caller
    fails closed to the land-to-Delta fallback rather than emitting an unusable string.

    STRICT secret boundary: username / password are NEVER read or emitted; Power BI / the gateway
    supplies credentials at bind time.
    """
    dsn = (conn.get("odbc_dsn") or "").strip()
    driver = (conn.get("odbc_driver") or "").strip()
    if not dsn and not driver:
        driver = NATIVE_ODBC_DRIVER.get((conn.get("connection_class") or "").lower(), "")
    clauses = []
    if dsn:
        clauses.append(f"dsn={dsn}")
    elif driver:
        clauses.append(f"Driver={{{driver}}}")
        server = (conn.get("server") or "").strip()
        port = str(conn.get("port") or "").strip()
        database = (conn.get("database") or "").strip()
        if server:
            clauses.append(f"Server={server}")
        if port:
            clauses.append(f"Port={port}")
        if database:
            clauses.append(f"Database={database}")
    else:
        return None
    extras = _scrub_odbc_extras(conn.get("odbc_connect_string_extras"))
    if extras:
        clauses.append(extras)
    return ";".join(clauses)


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


# Tableau's "forward-compatible persistence" (FCP) namespace. While a document-format feature is in
# transition, Tableau writes a connection attribute under BOTH spellings in the SAME element:
#
#   _.fcp.DatabricksCatalog.false...dbname      = '/sql/1.0/warehouses/...'   <- legacy slot
#   _.fcp.DatabricksCatalog.true...dbname       = ''                          <- the Unity catalog
#   _.fcp.DatabricksCatalog.true...v-http-path  = '/sql/1.0/warehouses/...'   <- the HTTP path
#
# The two variants carry DIFFERENT MEANINGS, not one value under two names, so a blind prefix-strip
# is worse than not reading them at all: taking `.false...dbname` writes an HTTP path into
# `database` -- a wrong value that looks entirely plausible and passes every structural check.
#
# Which variant is live is recorded in the datasource's <document-format-change-manifest>, and the
# manifest ENTRY is prefixed in exactly the same way while the feature is unpromoted:
#
#   <_.fcp.DatabricksCatalog.true...DatabricksCatalog />   -> unpromoted; the live state is `true`
#   <DatabricksCatalog />                                  -> promoted; attributes are written plain
#
# That gives a deterministic, connector-agnostic rule rather than a per-connector guess: read the
# state the manifest names, ignore every other state. Ground truth: the promoted shape is what
# current Tableau writes (verified across 15 real Databricks / Snowflake / Azure SQL exports, ZERO
# of which carry an FCP attribute); the prefixed shape comes from a real Tableau 2021.3 export.
_FCP_ATTR_RE = re.compile(r"^_\.fcp\.([A-Za-z0-9]+)\.([A-Za-z0-9]+)\.\.\.(.+)$")


def _fcp_live_states(datasource):
    """``{feature: live-state}`` for every UNPROMOTED feature the manifest names.

    A promoted entry (bare ``<DatabricksCatalog />``) is deliberately absent from the result: its
    attributes are already written under their plain names, so there is nothing to resolve.
    """
    states = {}
    for manifest in _findall_local(datasource, "document-format-change-manifest"):
        for entry in list(manifest):
            m = _FCP_ATTR_RE.match(_local(entry.tag))
            if m and m.group(3) == m.group(1):
                states[m.group(1)] = m.group(2)
    return states


def _resolve_fcp_attributes(datasource):
    """Rewrite live FCP-prefixed connection attributes under their plain names, in place.

    Applied once, to the whole datasource subtree, BEFORE any connection reader runs -- so
    ``_live_connection`` / ``_connection_facts`` / ``_http_path_of`` / ``_odbc_facts`` all see the
    resolved attribute without knowing FCP exists.

    Fail-closed on three counts, each of which prevents a plausible-looking wrong value:

    * a feature the manifest does not name as unpromoted is skipped entirely (we do not guess which
      of the two variants is live);
    * only the state the manifest names is read -- the other state's attributes are left untouched
      and never consulted, so ``.false...dbname`` can never become ``database``;
    * an EMPTY live value never overwrites an existing plain attribute (an unset Unity catalog must
      not erase a real one), and the FCP attributes are left in place rather than deleted, so this
      is purely additive.

    No manifest, or no FCP attributes, is a no-op -- which is every currently-shipping Tableau
    export we have measured.
    """
    states = _fcp_live_states(datasource)
    if not states:
        return
    for conn in datasource.iter():
        if _local(conn.tag) != "connection":
            continue
        for raw, value in list(conn.attrib.items()):
            m = _FCP_ATTR_RE.match(raw)
            if not m:
                continue
            feature, state, attr = m.groups()
            if states.get(feature) != state:
                continue
            if value and not conn.get(attr):
                conn.set(attr, value)


def _http_path_of(conn):
    """Return the Databricks SQL-warehouse HTTP path from whichever attribute carries it, or None."""
    for attr in _HTTP_PATH_ATTRS:
        v = conn.get(attr)
        if v:
            return v
    return None


# Non-secret ODBC attributes lifted from one inner <connection class='genericodbc'>. STRICT
# secret boundary: only the driver name, DSN name, the connect-string EXTRAS, the DBMS-name hint,
# and the port are read -- NEVER username / password (a generic ODBC .tds can carry both inline).
# The extras are additionally scrubbed of credential-bearing keys before emission (see
# _odbc_connection_string). Tolerates a None element (returns all-None) so callers need no guard.
def _odbc_facts(c):
    get = (lambda _k: None) if c is None else c.get
    # The connect-string EXTRAS can carry inline credentials (UID/PWD/token); the descriptor is
    # serialized into the migration report, so scrub them HERE -- the descriptor must never carry a
    # secret. _odbc_connection_string scrubs again (idempotent) as defense in depth.
    return {
        "odbc_driver": get("odbc-driver"),
        "odbc_dsn": get("odbc-dsn"),
        "odbc_connect_string_extras": _scrub_odbc_extras(get("odbc-connect-string-extras")) or None,
        "odbc_dbms_name": get("odbc-dbms-name"),
        "port": get("port"),
    }


# Tableau extract-engine connection classes. A datasource whose primary connection is one of these
# IS a materialized extract (a standalone .hyper, or a legacy .tde over the pre-Hyper Data Engine),
# even without an <extract enabled> wrapper -- so ``is_extract`` is set from the class alone.
_EXTRACT_ENGINE_CLASSES = frozenset({"hyper", "dataengine"})


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


def _primary_connection_el(datasource):
    """Return the inner ``<connection>`` element ``_live_connection`` reads (the federated
    named-connection inner first, then a non-federated ``<connection>``), or ``None``.

    Used to lift the primary connection's ODBC attributes onto the descriptor with the SAME
    descent ``_live_connection`` uses, so the descriptor's class/server/database and its ODBC
    facts always come from one consistent connection.
    """
    for nc in _named_connections(datasource):
        inner = _children_local(nc, "connection")
        if inner:
            return inner[0]
    for c in _children_local(datasource, "connection"):
        if (c.get("class") or "").lower() != "federated":
            return c
    return None


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
        **_odbc_facts(c),
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
    seen = {}  # (parent, model_name) -> col dict already emitted, for twin-record dedup
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
        # Physical column position, when Tableau records it. Used to reconcile a Tableau alias
        # (remote-name) back to the real Excel/CSV header at the same position when the two diverge
        # (see reconcile_flatfile_headers) -- a deterministic anchor, not a guess.
        ordinal = _txt("ordinal")
        if ordinal is not None:
            try:
                col["ordinal"] = int(str(ordinal).strip())
            except (TypeError, ValueError):
                pass
        fmt = fmt_by_physical.get((parent, model_name))
        if fmt:
            col["format_string"] = fmt
        cat = geo_by_physical.get((parent, model_name))
        if cat:
            col["data_category"] = cat
        # Tableau emits TWIN <metadata-record> entries for join-key / calc-referenced columns: one
        # federated/logical record (often lacking <ordinal>) and one from the extract's .hyper cache
        # (ordinal set). Both clean to the same model_name, so emitting both yields a duplicate TMDL
        # column -> an invalid, un-openable model. Dedup at this single upstream source (keep first,
        # merge in any anchor a later twin carries) so EVERY downstream consumer -- live-DB and
        # flat-file alike -- sees each physical column once. The flat-file column_reconcile still runs
        # for phantom-drop / header-remap; twin collapse simply no longer depends on a readable header.
        prior = seen.get((parent, model_name))
        if prior is not None:
            for k in ("ordinal", "format_string", "data_category"):
                if k not in prior and k in col:
                    prior[k] = col[k]
            continue
        seen[(parent, model_name)] = col
        out.setdefault(parent, []).append(col)
    return out


# A bracketed field reference inside a calc formula, e.g. ``[Sales]`` or ``[Id (Contact)]``.
_CALC_REF_RE = re.compile(r"\[([^\]]+)\]")


def _lid_to_physical(datasource):
    """Map a logical column id -> the set of physical ``(table, model_col)`` identities it maps to.

    Built from the live-connection ``<cols><map key='[lid]' value='[table].[col]'>`` bridge. Each
    physical endpoint is normalized to the SAME identity ``_columns_by_parent`` emits under --
    object-id-hash ``.hyper`` twins collapsed (``_strip_oid_hash``) and the column ``clean_col``'d --
    so a lid the metadata-record name index alone can't resolve (a caption a calc uses) still lands
    on the emitted column. A lid mapping to several DISTINCT identities is kept as a set so the caller
    can fail closed on genuine ambiguity.
    """
    out = {}
    for cols in _findall_local(datasource, "cols"):
        for m in _children_local(cols, "map"):
            key = _strip_brackets((m.get("key") or "").strip())
            _cat, table, col = _parse_table_name((m.get("value") or "").strip())
            if key and table and col:
                out.setdefault(key, set()).add((_strip_oid_hash(table), clean_col(col)))
    return out


def _hidden_physical_columns(datasource):
    """Set of ``(parent, model_col)`` physical identities the author HID in this datasource.

    A Salesforce (or similar) ``.tds`` exposes the full physical schema but hides most of it via a
    logical ``<column hidden='true'>`` element. This resolves each hidden NON-calc column to the
    ``(parent, clean_col(remote))`` identity ``_columns_by_parent`` emits under -- via the
    live-connection ``<cols><map>`` bridge (object-id twins collapsed) first, else the
    ``<metadata-record>`` identity by name. A hidden ``<column>`` that carries a ``<calculation>`` is
    NOT a physical column and is skipped. An unresolvable or genuinely-ambiguous id is omitted
    (fail-closed: a hidden column we cannot pin to a single emitted identity is simply never pruned,
    so pruning can only ever DROP a column it positively identified as hidden).
    """
    name_to_identity = _metadata_identity_index(datasource)
    lid_to_phys = _lid_to_physical(datasource)
    hidden = set()
    for col in _children_local(datasource, "column"):
        if (col.get("hidden") or "").strip().lower() != "true":
            continue
        if _children_local(col, "calculation"):
            continue  # a hidden calculated field is not a physical column
        lid = _strip_brackets((col.get("name") or "").strip())
        if not lid:
            continue
        phys = lid_to_phys.get(lid)
        if phys is not None:
            if len(phys) == 1:  # <cols><map> spoke unambiguously
                hidden.add(next(iter(phys)))
            continue            # ambiguous cols-map -> fail closed (never over-drop)
        ident = name_to_identity.get(lid)
        if ident:
            hidden.add(ident)
    return hidden


def _calc_referenced_physical(datasource):
    """Set of ``(parent, model_col)`` physical identities referenced by ANY calc formula.

    Every ``<column><calculation formula=...>`` in the datasource is scanned for ``[field]`` tokens;
    each token is resolved to a physical identity through BOTH the ``<metadata-record>`` name index
    AND the ``<cols><map>`` caption bridge (which catches a caption a calc uses that metadata-name
    resolution alone misses). Used to CARVE OUT a hidden physical column a calc depends on -- Tableau
    lets an author reference a field and then hide it, so a hidden calc dependency must survive the
    prune (kept, flagged ``isHidden``) or the calc's DAX would dangle.
    """
    name_to_identity = _metadata_identity_index(datasource)
    lid_to_phys = _lid_to_physical(datasource)
    refs = set()
    for col in _findall_local(datasource, "column"):
        for calc in _children_local(col, "calculation"):
            formula = calc.get("formula")
            if not formula:
                continue
            for raw in _CALC_REF_RE.findall(formula):
                tok = _strip_brackets(raw.strip())
                if not tok:
                    continue
                ident = name_to_identity.get(tok)
                if ident:
                    refs.add(ident)
                phys = lid_to_phys.get(tok)
                if phys is not None and len(phys) == 1:
                    refs.add(next(iter(phys)))
    return refs


def _prune_hidden_physical_columns(datasource, cols_by_parent, relations, relationships):
    """Drop hidden physical columns from the emitted tables, keeping the load-bearing ones hidden.

    A Salesforce-style datasource exposes the full physical schema but HIDES ~90% of it, so emitting
    every ``<metadata-record>`` column yields a model many times wider than the real Tableau
    datasource (e.g. 2,500+ columns vs the ~290 the author actually uses). Keep a physical column iff
    it is (a) NOT hidden, (b) a relationship JOIN KEY, or (c) referenced by a calc formula -- Tableau
    always hides join keys, and a calc can reference a field the author later hid, so both classes are
    load-bearing and must survive (kept but flagged ``is_hidden`` so they emit with ``isHidden``). The
    rest are dropped.

    The prune operates in the ``(parent, model_name)`` identity space ``_columns_by_parent`` groups
    under; join keys are matched in the emitted-table DISPLAY-name space (a role-playing alias that
    shares a physical column list contributes its own display name). Mutates each emitted table's
    column list (and column dicts) IN PLACE -- the relation and ``cols_by_parent`` lists are the same
    objects, so the mutation is seen by every consumer. A table that would be emptied entirely is
    kept intact (as hidden) rather than emit an invalid zero-column table. Returns
    ``{"columns_emitted", "columns_pruned_hidden"}``; returns ``None`` (a byte-identical no-op) when
    the datasource hides nothing (e.g. the Superstore fixtures).
    """
    hidden_set = _hidden_physical_columns(datasource)
    if not hidden_set:
        return None
    calc_ref_set = _calc_referenced_physical(datasource)
    join_key_set = set()
    for r in relationships:
        join_key_set.add((r["from_table"].lower(), r["from_col"]))
        join_key_set.add((r["to_table"].lower(), r["to_col"]))

    # Every emitted-table DISPLAY name that references each shared column-list object. Role-playing
    # aliases (a distinct <name> over the same physical <item>) share ONE list, so join keys resolved
    # under EITHER display name must carve out of that single list.
    displays_by_list = {}
    for r in relations:
        if r.get("kind") not in ("table", "custom_sql"):
            continue
        cols = r.get("columns")
        if not cols:
            continue
        disp = _table_display(r)
        if disp:
            displays_by_list.setdefault(id(cols), set()).add(disp.lower())

    emitted = pruned = 0
    for parent, cols in cols_by_parent.items():
        if not cols:
            continue
        displays = displays_by_list.get(id(cols), set())
        keep_ids = set()
        for c in cols:
            mn = c["model_name"]
            if (parent, mn) not in hidden_set:
                keep_ids.add(id(c))            # visible physical column -> always kept
                continue
            carve = (parent, mn) in calc_ref_set or any((d, mn) in join_key_set for d in displays)
            if carve:
                c["is_hidden"] = True
                keep_ids.add(id(c))            # load-bearing hidden column -> kept, flagged
        new = [c for c in cols if id(c) in keep_ids]
        if not new:
            # Pathological: the whole table is hidden with no load-bearing column. Keep it intact
            # (as hidden) rather than emit an invalid zero-column table.
            for c in cols:
                c["is_hidden"] = True
            new = list(cols)
        pruned += len(cols) - len(new)
        emitted += len(new)
        cols[:] = new
    return {"columns_emitted": emitted, "columns_pruned_hidden": pruned}


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

    # Harvest unambiguous <cols><map> pins that have NO <column> declaration for the emit loop to
    # bind. A Salesforce workbook can pin a caption to exactly one physical column without declaring
    # a matching <column caption=.. name=..>, so the pin is dropped above and the caption stubs at
    # resolve-time (metadata `cap_to` alone can't rescue it when the caption case-folds to two
    # physical tables). We add the pin ONLY when it is provably safe -- four fail-closed gates:
    #   1. exactly one physical target (never guess an ambiguous pin);
    #   2. no <column name='[key]'> already declares it (gate 1) and it does not shadow a caption/id
    #      a declared/emitted entry already resolves (gate 2 -- fail closed against ambiguation);
    #   3. it is a user caption, not an internal/synthetic token (Calculation_*/Parameter*/scoped);
    #   4. no case-insensitive collision against ANOTHER harvest candidate (emit neither if so).
    # The entry's `tmdl_type` is cosmetic -- build_m_field_resolver derives the real type from the
    # emitted relation column via `_phys_target`, so the "string" placeholder is never surfaced. The
    # logical layer is consulted only when metadata resolution misses/is ambiguous, so this can only
    # ADD a resolution, never override a working one.
    declared_ids = set()
    for col in _children_local(datasource, "column"):
        cid = _strip_brackets((col.get("name") or "").strip())
        if cid:
            declared_ids.add(cid)
    declared_resolver_keys = set()
    for e in out:
        declared_resolver_keys.add(e["caption"].strip().lower())
        declared_resolver_keys.add(e["logical_id"].strip().lower())
    harvest_candidates = []  # (key, table, physical_col)
    for key, phys in logical_to_physical.items():
        if len(phys) != 1:
            continue  # gate 1: ambiguous pin -> never guess
        if key in declared_ids:
            continue  # a <column name='[key]'> already handles it
        if key.strip().lower() in declared_resolver_keys:
            continue  # gate 2: would shadow/ambiguate a working caption/id
        if (":" in key) or key.startswith("Calculation_") or key.startswith("Parameter"):
            continue  # gate 3: internal/synthetic token, not a user caption
        table, physical_col = next(iter(phys))
        harvest_candidates.append((key, table, physical_col))
    _low_counts = {}
    for key, _t, _c in harvest_candidates:
        _low_counts[key.strip().lower()] = _low_counts.get(key.strip().lower(), 0) + 1
    for key, table, physical_col in harvest_candidates:
        if _low_counts[key.strip().lower()] != 1:
            continue  # gate 4: harvest-vs-harvest case-insensitive collision -> emit NEITHER
        out.append({
            "caption": key,
            "logical_id": key,
            "table": table,
            "physical_col": physical_col,
            "model_col": clean_col(physical_col),
            "tmdl_type": "string",  # cosmetic: resolver derives the real type from _phys_target
        })
    return out


def _is_combination_relation(rel):
    """True for a ``join``/``union`` tree (or any non-collection relation that nests child
    relations): these collapse their leaves into ONE logical table and are reported as a
    single combination entry so the storage-mode policy can fall back. A ``collection`` is
    NOT a combination -- it is a container of INDEPENDENT tables."""
    rtype = (rel.get("type") or "").lower()
    if rtype in CONTAINER_RELATION_TYPES:
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


def _extract_hyper_bindings(datasource):
    """Every ENABLED ``<extract>``'s bundled ``.hyper`` and the logical tables it materializes.

    Returns ``[{"member", "tables", "families"}]``, one entry per enabled extract:

    * ``member`` -- the ``.hyper``'s path INSIDE the ``.twbx``/``.tdsx`` archive, read from the
      extract's own ``<connection class='hyper' dbname='Data/.../x.hyper'>``. This is the datasource's
      OWN extract: a workbook with several extract-backed datasources bundles one ``.hyper`` per
      datasource, and only this attribute says which is which.
    * ``tables`` -- the physical table identities inside that ``.hyper`` (``[Extract].[Extract]`` for
      the ubiquitous single-table extract).
    * ``families`` -- the ``<family>`` of each ``<metadata-record>``, i.e. the LOGICAL relation each
      extracted column came from. For a single-table extract every family is the one source table's
      name, which is the only link back from the anonymous ``Extract`` table to the relation it backs.

    Non-secret attributes only. Fail-closed: an extract with no resolvable ``dbname`` is skipped, so
    a caller can never bind a relation to an archive member that does not exist.
    """
    out = []
    for ex in _findall_local(datasource, "extract"):
        if (ex.get("enabled") or "true").lower() == "false":
            continue
        for conn in _findall_local(ex, "connection"):
            if (conn.get("class") or "").lower() not in _EXTRACT_ENGINE_CLASSES:
                continue
            member = (conn.get("dbname") or "").strip()
            if not member or not member.lower().endswith(".hyper"):
                continue
            tables, families = [], []
            for rel in _findall_local(conn, "relation"):
                raw = (rel.get("table") or rel.get("name") or "").strip()
                if raw and raw not in tables:
                    tables.append(raw)
            for rec in _findall_local(conn, "metadata-record"):
                for fam in _children_local(rec, "family"):
                    val = (fam.text or "").strip()
                    if val and val not in families:
                        families.append(val)
            out.append({"member": member, "tables": tables, "families": families})
            break  # one extract element materializes exactly one .hyper
    return out


def _extract_materialized_tables(datasource):
    """Each enabled ``<extract>``'s OWN materialized table identity, from its own records.

    Returns ``[{"member", "schema", "item", "raw_table", "parent"}]``. ``parent`` is the
    ``<metadata-record><parent-name>`` those records are grouped under -- the key
    :func:`_columns_by_parent` files the extract's typed columns beneath.

    An extract element describes the table Tableau actually MATERIALIZED: a join/union of live
    relations is flattened into one physical table, so the extract's records are the schema of the
    ``.hyper``, not of the pre-extract logical relations. Returns ``[]`` outright if ANY enabled
    extract is ambiguous (its records span several parents), rather than quietly omitting it --
    otherwise an unambiguous sibling would be the lone candidate and a caller could collapse onto
    it, silently discarding the tables the ambiguous extract describes.
    """
    out = []
    for ex in _findall_local(datasource, "extract"):
        if (ex.get("enabled") or "true").lower() == "false":
            continue
        for conn in _findall_local(ex, "connection"):
            if (conn.get("class") or "").lower() not in _EXTRACT_ENGINE_CLASSES:
                continue
            parents = []
            for rec in _findall_local(conn, "metadata-record"):
                if (rec.get("class") or "").lower() != "column":
                    continue
                els = _children_local(rec, "parent-name")
                txt = (els[0].text or "").strip() if els and els[0].text is not None else ""
                p = _strip_brackets(txt)
                if p and p not in parents:
                    parents.append(p)
            if len(parents) > 1:
                return []  # several tables in one extract -> nothing here is safe to collapse onto
            if not parents:
                break  # no typed records at all -> this extract describes nothing
            rels = _findall_local(conn, "relation")
            raw = (rels[0].get("table") or "").strip() if rels else ""
            out.append({
                "member": (conn.get("dbname") or "").strip() or None,
                "schema": (conn.get("schema") or "").strip() or None,
                "item": (conn.get("tablename") or "").strip() or parents[0],
                "raw_table": raw or None,
                "parent": parents[0],
            })
            break  # one extract element materializes exactly one table
    return out


_EXTRACT_GUID_SUFFIX_RE = re.compile(r"_[0-9A-Fa-f]{32}$")


def _extract_parent_match_key(name):
    """Normalise an extract parent / relation name for one-to-one matching.

    A Tableau extract names each materialised table ``<source>_<32-hex-GUID>`` (``Orders.csv`` ->
    ``Orders.csv_96FB...``), so the GUID is stripped, then punctuation and case are folded. Returns
    ``""`` for anything that normalises away, which never matches.
    """
    s = _strip_brackets(str(name or "").strip())
    s = _EXTRACT_GUID_SUFFIX_RE.sub("", s)
    return re.sub(r"[^0-9a-z]+", "", s.lower())


def _extract_materialized_tables_by_parent(datasource):
    """Every parent an enabled ``<extract>`` materialised, one entry each.

    The sibling :func:`_extract_materialized_tables` answers "is there exactly ONE table to collapse
    everything onto", and deliberately returns ``[]`` the moment an extract spans several parents.
    This answers the different question the multi-table case needs: *which* tables did it
    materialise, so each column-less relation can be mapped onto its OWN one.

    Returns ``[{"member", "schema", "item", "parent"}]``, in document order.
    """
    out = []
    for ex in _findall_local(datasource, "extract"):
        if (ex.get("enabled") or "true").lower() == "false":
            continue
        for conn in _findall_local(ex, "connection"):
            if (conn.get("class") or "").lower() not in _EXTRACT_ENGINE_CLASSES:
                continue
            member = (conn.get("dbname") or "").strip() or None
            schema = (conn.get("schema") or "").strip() or None
            seen = set()
            for rec in _findall_local(conn, "metadata-record"):
                if (rec.get("class") or "").lower() != "column":
                    continue
                els = _children_local(rec, "parent-name")
                txt = (els[0].text or "").strip() if els and els[0].text is not None else ""
                parent = _strip_brackets(txt)
                if parent and parent not in seen:
                    seen.add(parent)
                    out.append({"member": member, "schema": schema,
                                "item": parent, "parent": parent})
            break  # one extract element materialises one .hyper member
    return out


def _expand_untyped_relations_to_extract_tables(datasource, relations, cols_by_parent):
    """Type each column-less relation from its OWN table in a MULTI-table extract.

    The sibling collapse handles the single-table extract. When an extract materialises SEVERAL
    tables it correctly refuses to collapse -- folding three tables onto one would silently discard
    two -- but that refusal used to end the story, and the whole workbook was skipped: *"relation
    'Orders.csv' has no resolvable columns"*, `pbip: skipped`, `definition_of_done: failed`, while a
    complete typed schema and 11,807 rows sat in the bundled ``.hyper`` the entire time. Measured at
    **4 of 6** workbooks in a standard Tableau training corpus (issue #104), and the shape is common:
    an analyst unions a few CSVs and publishes an extract.

    The information needed was already present -- the extract files one parent PER TABLE, each with
    its own typed ``metadata-record`` columns. What was missing was a per-relation mapping, not a
    schema. This supplies it, and keeps every guarantee the collapse guard was protecting:

    * every table relation must be column-less (if any one types, the live layer is intact and is the
      better upstream);
    * each relation must match EXACTLY ONE extract parent, and no two relations may claim the same
      parent -- a one-to-one mapping or nothing;
    * every matched parent must actually carry typed columns.

    Anything short of that returns ``None`` with ``relations`` untouched, so an ambiguous extract
    degrades to exactly the previous behaviour instead of guessing which table is which. Matching is
    on the GUID-stripped, case- and punctuation-folded name, which is how Tableau relates
    ``Orders.csv`` to the ``Orders.csv_96FB...`` it materialised.
    """
    table_like = [r for r in relations if r.get("kind") in ("table", "custom_sql")]
    if len(table_like) < 2 or any(r.get("columns") for r in table_like):
        return None
    parents = _extract_materialized_tables_by_parent(datasource)
    if len(parents) < 2:
        return None      # the single-table case belongs to the collapse, not here

    by_key = {}
    for mat in parents:
        key = _extract_parent_match_key(mat["parent"])
        if not key:
            continue
        by_key.setdefault(key, []).append(mat)

    pairs, claimed = [], set()
    for rel in table_like:
        candidates = []
        for ref in (rel.get("name"), rel.get("item"), rel.get("raw_table")):
            key = _extract_parent_match_key(ref)
            if key and key in by_key:
                candidates = by_key[key]
                break
        if len(candidates) != 1:
            return None                       # ambiguous or unmatched -> never a guess
        mat = candidates[0]
        if mat["parent"] in claimed:
            return None                       # two relations claiming one table
        cols = (cols_by_parent or {}).get(mat["parent"]) or []
        if not cols:
            return None
        claimed.add(mat["parent"])
        pairs.append((rel, mat, cols))

    for rel, mat, cols in pairs:
        rel["columns"] = cols
        rel["schema"] = mat["schema"]
        rel["item"] = mat["item"]
        rel["raw_table"] = f"[{mat['item']}]"
        rel["materialized_from_extract"] = True
        if mat["member"]:
            rel["extract_hyper_member"] = mat["member"]
            rel["extract_hyper_table"] = f"[{mat['item']}]"
    # The join/union containers described the PRE-extract shape; the extract materialised each table
    # separately, so the containers no longer describe anything physical. Relationships between the
    # now-typed tables are re-derived from the extract's own tables by the caller.
    relations[:] = [r for r in relations
                    if r.get("kind") not in CONTAINER_RELATION_TYPES]
    return [rel for rel, _m, _c in pairs]


def _collapse_untyped_relations_to_extract(datasource, relations, cols_by_parent):
    """Replace PRE-EXTRACT relations that carry no columns with the extract's materialized table.

    Tableau rewrites an extracted datasource's column metadata to describe the ``.hyper`` it built,
    filing every ``<metadata-record>`` under the extract's own parent (``[Extract]``). The live
    ``<connection>``'s relations -- the pre-extract logical shape (``Finance``, ``Facts``, a
    ``Sheet3$``, a ``...#csv``) -- are then left with NO resolvable columns, because those tables no
    longer physically exist: the extract flattened them into one table. Whole families of workbooks
    look like this, above all the legacy pre-federation (Tableau 9.x / Public) shape, where the
    logical->physical bridge is a ``<cols><map>`` and there are no per-relation metadata records at
    all.

    Left alone, every such relation is reported as "has no resolvable columns", the datasource is
    declared un-typable, no model is built and the workbook's .pbip is skipped -- even though the
    extract carries a COMPLETE, typed column list for the exact table its bundled ``.hyper`` holds.

    Collapsing is total or nothing, and only when there is nothing to lose:

    * every table relation must be column-less (if any one types, the live layer is intact and is
      the better upstream -- a partial collapse would be a guess);
    * exactly ONE enabled extract must describe exactly one materialized table;
    * that table's parent must actually carry typed columns.

    On success the column-less relations are REPLACED, in place, by a single relation named after
    the datasource and carrying the extract's typed columns; join/union containers are dropped with
    them (their join was materialized into the extract). Returns the new relation, or ``None`` when
    any precondition fails -- in which case ``relations`` is untouched and the caller degrades to
    exactly the previous behaviour.
    """
    table_like = [r for r in relations if r.get("kind") in ("table", "custom_sql")]
    if not table_like or any(r.get("columns") for r in table_like):
        return None
    mats = _extract_materialized_tables(datasource)
    if len(mats) != 1:
        return None
    mat = mats[0]
    cols = (cols_by_parent or {}).get(mat["parent"]) or []
    if not cols:
        return None

    caption = (datasource.get("caption") or datasource.get("formatted-name")
               or datasource.get("name") or mat["parent"])
    new_rel = {
        "kind": "table",
        "name": caption,
        "raw_table": mat["raw_table"] or f"[{mat['item']}]",
        "catalog": None,
        "schema": mat["schema"],
        "item": mat["item"],
        "columns": cols,
        # Provenance: this table IS the bundled extract, so the materializer reads its rows from
        # that member and never has to match the anonymous 'Extract' name across datasources.
        "materialized_from_extract": True,
    }
    if mat["member"]:
        new_rel["extract_hyper_member"] = mat["member"]
        if mat["raw_table"]:
            new_rel["extract_hyper_table"] = mat["raw_table"]
    relations[:] = [r for r in relations
                    if r.get("kind") not in ("table", "custom_sql") + CONTAINER_RELATION_TYPES]
    relations.append(new_rel)
    return new_rel


def _bind_relations_to_extracts(relations, bindings):
    """Stamp each table relation with the bundled ``.hyper`` that actually holds its rows.

    A flat-file workbook that was extracted no longer carries its source CSV/Excel anywhere -- the
    ``<connection>`` keeps only the AUTHOR's directory (``C:/Users/<someone>/Downloads``) and the data
    lives solely in the packaged ``.hyper``. Recording the owning archive member ON THE RELATION lets
    the materializer read the right extract per island **by provenance**, instead of matching on the
    anonymous ``Extract`` table name that every single-table Tableau extract shares (which would
    silently give island 2 island 1's data).

    Sets ``extract_hyper_member`` (+ ``extract_hyper_table`` when the extract names exactly one) on
    each matched relation, in place. Matching, widest-first and never a guess:

    1. exactly ONE enabled extract -> it backs every table relation in this datasource;
    2. several extracts -> a relation binds only when exactly ONE extract's ``families`` names it.

    Returns the number of relations stamped. A relation that cannot be bound uniquely is left
    untouched, so an ambiguous shape degrades to today's behaviour rather than mis-binding.
    """
    table_rels = [r for r in (relations or []) if r.get("kind") in ("table", "custom_sql")]
    usable = [b for b in (bindings or []) if b.get("member")]
    if not table_rels or not usable:
        return 0

    def _stamp(rel, binding):
        rel["extract_hyper_member"] = binding["member"]
        tbls = binding.get("tables") or []
        if len(tbls) == 1:
            rel["extract_hyper_table"] = tbls[0]

    if len(usable) == 1:
        for rel in table_rels:
            _stamp(rel, usable[0])
        return len(table_rels)

    stamped = 0
    for rel in table_rels:
        name = (rel.get("name") or rel.get("item") or "").strip().lower()
        if not name:
            continue
        hits = [b for b in usable
                if any((f or "").strip().lower() == name for f in (b.get("families") or []))]
        if len(hits) == 1:
            _stamp(rel, hits[0])
            stamped += 1
    return stamped


def _union_containers_to_promote(datasource, cols_by_parent):
    """Which ``union``/``batch-union`` containers are themselves the table, and which members to drop.

    A union is not a combination of tables the way a join is -- it is ONE table with the same columns
    and more rows (``Table.Combine``). Tableau writes that fact into the metadata: every column of a
    union is filed under the CONTAINER's parent name (``[Orders.csv+]``), and its members get no
    ``metadata-record`` parent at all. A join is the opposite: each member keeps its own parent and
    its own columns, which is why surfacing join leaves individually is right and surfacing union
    leaves individually is wrong.

    Surfacing the members anyway leaves each of them column-less. That is survivable when the WHOLE
    datasource is column-less -- the extract collapse/expansion then re-types everything from the
    ``.hyper`` -- and fatal when the datasource is PARTIALLY typed, because both of those rescues
    require every table relation to be untyped (``any(r["columns"])`` aborts them). Issue #124 is
    exactly that shape: a union beside a join, where ``Customers.csv`` typed and ``Orders.csv`` /
    ``Orders_Archive.csv`` could not, so no rescue could run and the workbook was skipped with
    *"relation 'Orders.csv' has no resolvable columns"* -- while the union's own 11 typed columns sat
    under ``[Orders.csv+]`` the entire time.

    Returns ``({id(container_element): columns}, {id(member_element), ...})``. A container is promoted
    only when it resolves columns under its OWN name and NO member resolves any -- if a member types
    on its own, the leaf-surfacing path already has real tables to work with and is left alone.
    Membership is tracked by element IDENTITY, so a table that is a union input here and an
    independent relation elsewhere in the same datasource keeps its independent copy.
    """
    promote, member_ids = {}, set()
    for rel in _findall_local(datasource, "relation"):
        if (rel.get("type") or "").lower() not in UNION_RELATION_TYPES:
            continue
        name = rel.get("name")
        cols = (cols_by_parent or {}).get(_strip_brackets(name)) if name else None
        if not cols:
            continue  # nothing filed under the container -- leave today's behaviour alone
        members = _children_local(rel, "relation")
        if any(_classify_relation(m, cols_by_parent).get("columns") for m in members):
            continue  # a member is a real typed table; do not discard it
        promote[id(rel)] = cols
        member_ids.update(id(m) for m in members)
    return promote, member_ids


def _union_container_relation(rel, cols, nc_map=None):
    """Descriptor for a ``union``/``batch-union`` container surfaced as the one table it is.

    The container has no physical ``table=`` identity -- its members do -- so the union's own name
    becomes the item, which is also the name Tableau filed its columns under and the name the
    workbook's field references resolve through. ``union_members`` is recorded for provenance (a
    wildcard union has none: its members are a filename pattern resolved at connect time).
    """
    name = rel.get("name")
    item = _strip_brackets(name) if name else None
    members = _children_local(rel, "relation")
    entry = {
        "kind": "table",
        "name": name,
        "raw_table": "[%s]" % item if item else None,
        "catalog": None,
        "schema": None,
        "item": item,
        "columns": cols,
        "union_of": [m.get("name") for m in members if m.get("name")],
    }
    nc_map = nc_map or {}
    # The container usually carries no ``connection`` of its own; a union's members must all share
    # one connection, so a single agreed member connection is the container's connection.
    refs = [rel.get("connection")] if rel.get("connection") else [
        m.get("connection") for m in members]
    refs = [r for r in refs if r]
    if refs and len(set(refs)) == 1:
        conn = nc_map.get(refs[0])
        if conn:
            entry["connection"] = conn
    return entry


def _extract_relations(datasource, cols_by_parent, nc_map=None):
    """Walk ``<relation>`` elements into a flat, de-duplicated descriptor list.

    Handles the modern Tableau "object model" ``.tds`` shape, where the same physical tables
    appear twice -- once under a ``<relation type='collection'>`` container (the physical
    layer) and once under the logical ``<properties>`` layer:

    * ``collection`` containers are dropped; their child tables are emitted as INDEPENDENT
      model tables (multi-sheet Excel / multi-table sources become multiple model tables).
    * ``join`` trees are NOT collapsed: the combination container is dropped and each leaf table is
      surfaced as its OWN independent model table (its join keys become model relationships via
      ``_extract_join_relationships``), so the source rebuilds directly as a multi-table model --
      exactly like a multi-table object-graph source -- instead of an opaque combination the storage
      policy could only skip.
    * a ``union``/``batch-union`` is the opposite case and IS collapsed, to the ONE table it is:
      Tableau files a union's columns under the CONTAINER's own name, so the container is surfaced
      and its members are dropped (see ``_union_containers_to_promote``).
    * duplicate physical/logical copies of the same table (same ``item``) are de-duplicated,
      preferring the copy that actually resolves column metadata, while preserving a resolved
      per-relation ``connection`` from whichever copy carried it.
    * an extract ``.tds`` pulled from Tableau Server also carries a parallel ``[Extract].[...]``
      cache layer; those cache twins are dropped in favour of the live/logical relation (see
      ``_is_extract_cache_relation``), but ONLY when a live table relation survives to represent
      them -- an extract-ONLY datasource keeps its ``[Extract]`` tables, since they are all it has.
    """
    nc_map = nc_map or {}

    # First pass: classify every candidate relation so the extract-twin decision can be made with
    # whole-datasource knowledge before any table is emitted. A JOIN container is not itself a table
    # -- drop it and let its LEAF tables surface individually (they appear in the same ``<relation>``
    # walk), so a join tree rebuilds as separate model tables related by their join keys (recovered by
    # ``_extract_join_relationships``), exactly like a multi-table object-graph source, instead of
    # collapsing to one opaque combination the storage policy could only skip. A UNION goes the other
    # way: it IS one table, so the container is promoted and its members are skipped.
    promote, member_ids = _union_containers_to_promote(datasource, cols_by_parent)
    candidates = []
    for rel in _findall_local(datasource, "relation"):
        if id(rel) in member_ids:
            continue  # a union input, not a table: the promoted container above it IS the table
        if id(rel) in promote:
            candidates.append(_union_container_relation(rel, promote[id(rel)], nc_map))
            continue
        if (rel.get("type") or "").lower() == "collection":
            continue  # benign container; its child tables are emitted independently
        if _is_combination_relation(rel):
            continue  # a join container; its leaf tables are surfaced individually
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
            # catalog/schema) stay distinct. The display name is part of the key so a role-playing
            # ALIAS (same physical ``item`` but a distinct ``name``, e.g. ``Contact1`` over
            # ``[Contact]``) surfaces as its own model table instead of collapsing into the base
            # table -- physical/logical copies share both item and name, so they still collapse.
            key = (
                (entry.get("catalog") or "").lower(),
                (entry.get("schema") or "").lower(),
                (entry.get("item") or entry.get("name") or "").lower(),
                (_table_display(entry) or "").lower(),
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
    if rtype in CONTAINER_RELATION_TYPES or _children_local(rel, "relation"):
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


# -- data-free "one"-side inference for join predicates ------------------------
# A Tableau join predicate never records cardinality, and we cannot probe the data to find out:
# a live/DirectQuery datasource is routinely migrated without any database credential. So the
# "one" side has to be recovered from NAMES alone, and the cost of being wrong is asymmetric --
# a wrong many-to-one is rejected by Power BI on first refresh and cancels the whole relationship
# batch, while a missed upgrade merely leaves the faithful many-to-many. The rule below is
# therefore deliberately narrow and ABSTAINS unless exactly one operand is unambiguously its own
# table's identity column.

_GENERIC_IDENTITY_COLUMNS = frozenset({"id", "rowid", "oid", "guid", "uid", "objectid"})
_IDENTITY_SUFFIXES = ("id", "key", "guid", "uid", "pk")


def _looks_like_identity_column(table, column):
    """True if ``column`` is unambiguously ``table``'s own identity (primary-key) column.

    Two accepted forms, both judged on names only (never on data):
      * a generic row identifier -- ``Id``, ``RowID``, ``OID``, ``GUID``, ``UID``, ``ObjectId``;
      * the column named after ITS OWN table -- ``CustomerId``/``Customer_Key`` on ``Customer``.

    The table-qualified form is what makes this safe in the common case: a FOREIGN key such as
    ``User[ContactId]`` names a DIFFERENT table, so it does not read as ``User``'s identity and
    the predicate ``Contact[Id] = User[ContactId]`` resolves to the correct side. Comparison is
    case- and separator-insensitive, and a trailing disambiguating suffix on the table display
    name (``Contact (Assessments)`` -> ``Contact``) is ignored.

    Weak, non-identity-implying names (``Code``, ``No``, ``Number``) are deliberately NOT accepted:
    ``Product[ProductCode]`` is routinely non-unique, and a false positive here costs a failed
    refresh whereas a false negative costs nothing but a many-to-many.
    """
    canon = re.sub(r"[^a-z0-9]", "", str(column or "").strip().lower())
    if not canon:
        return False
    if canon in _GENERIC_IDENTITY_COLUMNS:
        return True
    owner = re.sub(r"\s*\([^)]*\)\s*$", "", str(table or "").strip())
    owner = re.sub(r"[^a-z0-9]", "", owner.lower())
    if not owner:
        return False
    return any(canon == owner + suffix for suffix in _IDENTITY_SUFFIXES)


def _orient_relationship_by_identity(from_table, from_col, to_table, to_col):
    """Orient a join so the identity ("one") side is the ``to`` side, and report its cardinality.

    Returns ``(from_table, from_col, to_table, to_col, cardinality)``. When exactly one operand is
    its table's identity column (see :func:`_looks_like_identity_column`) the join is emitted
    ``many_to_one`` with that operand as ``to`` -- swapping the authored operand order when needed,
    because Tableau does not pin the key to either position (``Contact[Id] = Assessment[Client]``
    and ``Assessment[PE] = ProgramEngagement[Id]`` both occur in one workbook). Normalising here is
    what makes ``RELATED()`` legal from the fact side and gives every downstream consumer a single
    invariant: ``to`` is the lookup side.

    When BOTH operands look like identities (a 1:1 key-to-key join) or NEITHER does, the function
    abstains and returns the operands untouched as ``many_to_many`` -- the crash-proof translation.
    """
    from_is_key = _looks_like_identity_column(from_table, from_col)
    to_is_key = _looks_like_identity_column(to_table, to_col)
    if to_is_key and not from_is_key:
        return from_table, from_col, to_table, to_col, "many_to_one"
    if from_is_key and not to_is_key:
        return to_table, to_col, from_table, from_col, "many_to_one"
    return from_table, from_col, to_table, to_col, "many_to_many"


def make_csv_unique_fn(table_csv_paths):
    """Build a ``unique_fn(table, column) -> True | False | None`` over materialized table CSVs.

    ``table_csv_paths`` maps a model table display name to the CSV that table's data was landed to
    (the ``.hyper``-to-CSV / bundled-flat-file materialization the import path already performs).
    Answers are cached per ``(table, column)``. Returns ``None`` -- never a guess -- when the table
    has no CSV, the column is absent from its header, or the file cannot be read, so a caller can
    tell "not unique" apart from "could not tell".

    A key is reported unique only when every row carries a distinct, NON-EMPTY value: Power BI
    rejects a many-to-one whose target contains a blank as readily as one containing a duplicate.
    """
    paths = {str(k).lower(): v for k, v in (table_csv_paths or {}).items()}
    cache = {}

    def unique_fn(table, column):
        import os as _os
        import csv as _csv
        key = (str(table or "").lower(), str(column or "").lower())
        if key in cache:
            return cache[key]
        verdict = None
        path = paths.get(key[0])
        if path and _os.path.exists(path):
            try:
                with open(path, encoding="utf-8-sig", newline="") as fh:
                    reader = _csv.reader(fh)
                    header = next(reader, None) or []
                    idx = next((i for i, h in enumerate(header)
                                if str(h).strip().lower() == key[1]), None)
                    if idx is not None:
                        seen, rows, ok = set(), 0, True
                        for row in reader:
                            rows += 1
                            val = row[idx].strip() if idx < len(row) else ""
                            if not val or val in seen:
                                ok = False
                                break
                            seen.add(val)
                        verdict = ok if rows else None
            except Exception:
                verdict = None
        cache[key] = verdict
        return verdict

    return unique_fn


def resolve_relationship_cardinality(rels, unique_fn=None):
    """Decide each relationship's cardinality and orient its "one" side onto ``to``.

    Tableau records NO cardinality for a join (verified absent from every asset in our corpus), and
    the data often cannot be consulted -- a live/DirectQuery datasource is routinely migrated with no
    database credential at all. So evidence is applied as a ladder, strongest first, and the rung
    that fires is whichever is actually available:

    1. **Measured uniqueness** -- ``unique_fn(table, column) -> True | False | None``, supplied by
       the caller when the data was materialized (see :func:`make_csv_unique_fn`). Definitive and
       INDEPENDENT OF NAMING, so it recognises a key called ``SKU``, ``Email`` or ``AccountNumber``
       that no name rule could ever spot.
    2. **Identity-column naming** (:func:`_looks_like_identity_column`) -- the data-free fallback, so
       a credential-less live datasource still gets a usable model.
    3. **Abstain** -- keep the faithful, crash-proof many-to-many.

    Measurement OVERRIDES naming in both directions: a column merely NAMED ``Id`` that is measurably
    non-unique stays many-to-many, which makes the pair strictly safer than the name rule alone.
    Only a definitive ``True`` promotes; ``False`` and ``None`` never do.

    Returns a NEW list (inputs untouched); relationships carrying an explicit non-``many_to_many``
    cardinality (e.g. the generated Date joins) pass through unchanged.
    """
    resolved = []
    for r in rels:
        if r.get("cardinality") not in (None, "many_to_many"):
            resolved.append(dict(r))
            continue
        ft, fc = r.get("from_table"), r.get("from_col")
        tt, tc = r.get("to_table"), r.get("to_col")

        def measured(table, column):
            if unique_fn is None:
                return None
            try:
                return unique_fn(table, column)
            except Exception:
                return None

        from_u, to_u = measured(ft, fc), measured(tt, tc)
        if to_u is True:
            pair, card = (ft, fc, tt, tc), "many_to_one"
        elif from_u is True:
            pair, card = (tt, tc, ft, fc), "many_to_one"          # swap: keys belong on `to`
        else:
            n_ft, n_fc, n_tt, n_tc, card = _orient_relationship_by_identity(ft, fc, tt, tc)
            pair = (n_ft, n_fc, n_tt, n_tc)
            # Naming proposed a "one" side that measurement DISPROVES -> refuse the upgrade.
            if card == "many_to_one" and measured(n_tt, n_tc) is False:
                pair, card = (ft, fc, tt, tc), "many_to_many"
        out = dict(r)
        out["from_table"], out["from_col"], out["to_table"], out["to_col"] = pair
        out["cardinality"] = card
        resolved.append(out)
    return resolved


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
            # A Tableau object-graph relationship (the "noodle") is an ad-hoc, uniqueness-agnostic
            # join: Tableau keeps each table at its native grain and chooses the join per-viz at
            # query time, never requiring a unique key on either side. Power BI relationships are
            # static, and the DEFAULT many-to-one requires the target ("one" side) to be unique --
            # on a non-unique target (e.g. a duplicate Order ID in returns) Power BI rejects the
            # relationship and cancels the WHOLE relationship batch on first refresh, collateral-
            # dropping sibling relationships (notably the generated Date join). Emitting these as
            # many-to-many -- which Power BI accepts WITHOUT any uniqueness check -- is both the
            # faithful translation of a Tableau relationship and crash-proof for every connection
            # type (import / live / federated / flat-file), since it is pure metadata, not data.
            out.append({"from_table": from_table, "from_col": from_col,
                        "to_table": to_table, "to_col": to_col,
                        "cardinality": "many_to_many"})
    return out, warnings


def _split_qualified_operand(op):
    """Split a physical-join operand ``[Table].[Column]`` into ``(table, '[Column]')``.

    Physical ``<clause type='join'>`` predicates reference their operands fully-qualified, e.g.
    ``[caseman__CasePlan__c].[Id]``. Returns ``(table, '[Column]')`` (the column part keeps its
    brackets so it can flow straight into :func:`_resolve_rel_column`). A bare, unqualified operand
    (``[Column]`` with no table prefix) returns ``(None, op)`` so the caller can skip a predicate it
    cannot attribute to a specific table.
    """
    if not op:
        return None, None
    op = op.strip()
    idx = op.find("].[")
    if idx != -1 and op.startswith("["):
        return op[1:idx], op[idx + 2:]
    return None, op


def _extract_join_relationships(datasource, relations):
    """Recover model relationships from PHYSICAL ``<relation type='join'>`` clause predicates.

    A Tableau physical join tree stores each join key as a ``<clause type='join'><expression op='='>``
    whose two operands are fully-qualified ``[Table].[Column]`` references. Because the leaf tables are
    now surfaced as independent model tables (see :func:`_extract_relations`), every such predicate
    becomes a model relationship between those tables -- the SAME treatment
    :func:`_extract_relationships` gives the object-graph "noodle", so a join tree rebuilds as a star
    of related tables instead of being skipped. Emitted ``many_to_many`` for the same crash-proof
    reason documented in :func:`_extract_relationships` (a Tableau join never guarantees a unique key
    on either side).

    Fail-closed: the table qualifier and both columns must resolve to emitted identifiers; anything
    that does not (a composite/calculated predicate, an unqualified operand, an unknown table/column)
    is skipped and recorded in the returned warnings -- never forcing the whole datasource to fall
    back. Returns ``(relationships, warnings)``.
    """
    name_index = {}
    for r in relations:
        if r.get("kind") in ("table", "custom_sql"):
            disp = _table_display(r)
            if disp:
                name_index.setdefault(disp.lower(), disp)
    cols_index = _columns_index(relations)
    out, warnings, seen = [], [], set()
    for rel in _findall_local(datasource, "relation"):
        if not _is_combination_relation(rel):
            continue
        for clause in _children_local(rel, "clause"):
            ops = _equality_operands(clause)
            if not ops:
                warnings.append(
                    "join clause is not a single-column equality "
                    "(composite / calculated / non-'=' predicate); skipped")
                continue
            (ft, fc_op) = _split_qualified_operand(ops[0])
            (tt, tc_op) = _split_qualified_operand(ops[1])
            if not ft or not tt:
                warnings.append(
                    f"join clause operands ({ops[0]} / {ops[1]}) are not both "
                    "table-qualified; skipped")
                continue
            from_table = name_index.get(ft.lower())
            to_table = name_index.get(tt.lower())
            if not from_table or not to_table:
                warnings.append(
                    f"join clause references a table that did not surface "
                    f"({ft!r} / {tt!r}); skipped")
                continue
            from_col = _resolve_rel_column(fc_op, cols_index.get(from_table.lower(), []))
            to_col = _resolve_rel_column(tc_op, cols_index.get(to_table.lower(), []))
            if not from_col or not to_col:
                warnings.append(
                    f"join clause columns ({fc_op} / {tc_op}) between '{from_table}' and "
                    f"'{to_table}' did not resolve to emitted columns; skipped")
                continue
            dedup = (from_table.lower(), from_col.lower(), to_table.lower(), to_col.lower())
            rdedup = (to_table.lower(), to_col.lower(), from_table.lower(), from_col.lower())
            if dedup in seen or rdedup in seen:
                continue
            seen.add(dedup)
            out.append({"from_table": from_table, "from_col": from_col,
                        "to_table": to_table, "to_col": to_col,
                        "cardinality": "many_to_many",
                        # A PHYSICAL join is one denormalized rowset in Tableau, so filters travel in
                        # every direction. See generate_relationships_tmdl for the full reasoning and
                        # the measured symptom. Demoted back to one-directional by
                        # _guard_bidirectional_ambiguity wherever it would create a second filter path.
                        "cross_filter": "both"})
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

    # Resolve any live FCP-prefixed connection attributes onto their plain names BEFORE any
    # connection reader runs, so a transitional document-format export (e.g. Tableau 2021.3's
    # `_.fcp.DatabricksCatalog.true...v-http-path`) is read exactly like a current one. No-op
    # without an unpromoted manifest entry. See `_resolve_fcp_attributes`.
    _resolve_fcp_attributes(datasource)

    cls, server, dbname, warehouse, http_path, auth_method, nconns = _live_connection(datasource)
    cols_by_parent = _columns_by_parent(datasource)
    nc_map = _named_connection_map(datasource)
    # Non-secret ODBC facts from the primary connection (all-None for non-ODBC sources). A real
    # generic-ODBC .tds is single-named-connection, so _effective_connection returns the descriptor
    # itself -- the ODBC facts must therefore live ON the descriptor for the emitter to read them.
    odbc_facts = _odbc_facts(_primary_connection_el(datasource))

    relations = _extract_relations(datasource, cols_by_parent, nc_map)
    # An EXTRACTED datasource's live relations are its PRE-extract logical shape; Tableau refiles all
    # column metadata under the extract's own table, leaving them untypable. Collapse onto the table
    # the extract actually materialized (the schema of the bundled .hyper) BEFORE relationships are
    # derived, so a join that the extract already flattened is not also emitted as a model
    # relationship between tables that no longer exist. A no-op unless every relation is columnless.
    if _collapse_untyped_relations_to_extract(datasource, relations, cols_by_parent) is None:
        # A MULTI-table extract cannot be collapsed onto one table -- doing so would discard the
        # others -- but it still carries a complete typed schema for each table it materialised, one
        # parent apiece. Map each column-less relation onto its OWN parent instead of declaring the
        # whole datasource un-typable and skipping the workbook (issue #104: 4 of 6 workbooks in a
        # standard training corpus). Fail-closed: anything short of a one-to-one match is a no-op.
        _expand_untyped_relations_to_extract_tables(datasource, relations, cols_by_parent)
    relationships, relationship_warnings = _extract_relationships(datasource, relations)
    # Recover join keys from any PHYSICAL join tree as model relationships and merge them in,
    # de-duplicating against the object-graph relationships in either orientation (a datasource
    # normally uses one representation or the other, but guard against overlap).
    join_relationships, join_rel_warnings = _extract_join_relationships(datasource, relations)
    if join_relationships:
        _rel_seen = set()
        for r in relationships:
            _rel_seen.add((r["from_table"].lower(), r["from_col"].lower(),
                           r["to_table"].lower(), r["to_col"].lower()))
        for r in join_relationships:
            key = (r["from_table"].lower(), r["from_col"].lower(),
                   r["to_table"].lower(), r["to_col"].lower())
            rkey = (r["to_table"].lower(), r["to_col"].lower(),
                    r["from_table"].lower(), r["from_col"].lower())
            if key in _rel_seen or rkey in _rel_seen:
                continue
            _rel_seen.add(key)
            relationships.append(r)
    relationship_warnings = list(relationship_warnings) + join_rel_warnings
    # Decide each join's cardinality and put its "one" side on `to`. Data-free here (parse_tds is a
    # pure metadata parser); a caller holding materialized data can refine the result by re-running
    # `resolve_relationship_cardinality` with a `unique_fn`.
    relationships = resolve_relationship_cardinality(relationships)
    # Prune the hidden physical schema down to what the datasource actually uses (visible columns +
    # calc-referenced + join-key hidden columns kept, flagged isHidden). Runs AFTER relationships are
    # fully merged so every join key is resolved against the FULL column set before any column is
    # dropped; a no-op (None) when nothing is hidden. Mutates the relation/cols_by_parent lists in
    # place, so the emitter sees the pruned tables.
    hidden_prune = _prune_hidden_physical_columns(
        datasource, cols_by_parent, relations, relationships)
    ff_filename, ff_directory = _flatfile_location(datasource)

    is_extract = False
    for ex in _findall_local(datasource, "extract"):
        if (ex.get("enabled") or "true").lower() != "false":
            is_extract = True
            break
    # A bare extract-engine connection (a standalone .hyper / legacy .tde datasource) IS an extract
    # by definition, even when it carries no <extract enabled> wrapper element -- the whole source is
    # the materialized extract. Detect it by connector class so an extract-only .tds routes to the
    # offline-Import-over-extract path (storage_mode branch 1.7) instead of dying at needs-decision.
    if not is_extract and (cls or "").lower() in _EXTRACT_ENGINE_CLASSES:
        is_extract = True

    # Record WHICH bundled .hyper backs each table, so an extract-backed source whose original file
    # is not packaged can still land real rows -- and so a workbook bundling one .hyper per datasource
    # reads the right one per island instead of collapsing them by their shared 'Extract' table name.
    extract_bindings = _extract_hyper_bindings(datasource)
    _bind_relations_to_extracts(relations, extract_bindings)

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
        "extract_hyper_members": [b["member"] for b in extract_bindings],
        "named_connection_count": nconns,
        "connections": nc_map,
        "flatfile_filename": ff_filename,
        "flatfile_directory": ff_directory,
        "flatfile_path": _flatfile_join(ff_directory, ff_filename),
        "relations": relations,
        "relationships": relationships,
        "relationship_warnings": relationship_warnings,
        "hidden_prune": hidden_prune,
        "logical_fields": _logical_fields(datasource),
        "unsupported_reasons": unsupported,
        **odbc_facts,
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
        # Tableau's declared result type. Carried so a downstream consumer can tell a STRING-valued
        # measure (a categorical label calc that drives a colour encoding) from a numeric one
        # without re-reading the XML or guessing from the translated DAX. Additive.
        datatype = (col.get("datatype") or "").strip()
        if datatype:
            entry["datatype"] = datatype
        # Author's explicit number format (currency/percent/precision) from the calc
        # <column @default-format>, conservatively decoded (explicit c/n/p/* codes only).
        # Additive: absent when there is no decodable code, so prior output is unchanged.
        fmt = tableau_measure_format_to_pbi(col.get("default-format"))
        if fmt:
            entry["format_string"] = fmt
        out.append(entry)
    return out


# Descriptor scalar/primary keys copied from the first datasource when several are combined. The
# per-relation ``connection`` refs (preserved by combine_descriptors) drive each table's actual
# partition source, so these primary facts are only the model default -- a mixed-connection /
# mixed-storage combined model still emits every table against its own source.
_COMBINE_SCALAR_KEYS = (
    "datasource_name", "connection_class", "server", "database", "warehouse", "http_path",
    "auth_method", "is_extract", "named_connection_count", "flatfile_filename",
    "flatfile_directory", "flatfile_path",
)


def _self_connection_facts(desc):
    """Reconstruct the lone upstream of a datasource that declares NO ``<named-connection>``.

    Tableau writes a ``<named-connections>`` block only for a FEDERATED datasource. A plain
    single-upstream datasource stores its connection as the scalar attributes of its one
    ``<connection>`` element, so ``parse_tds`` returns ``connections == {}`` and its relations carry
    no ``connection`` attribute -- correctly, because inside that datasource there is nothing to
    disambiguate.

    That becomes a false negative the moment several such datasources are CONSOLIDATED into one
    model: the combined descriptor is multi-connection by construction, and every one of those
    relations then looks unroutable to ``storage_mode._structurally_unsupported_reason``, which
    declines the whole rebuild ("multiple named connections but N table(s) don't resolve to a
    specific connection") -- so a workbook embedding two plain datasources produces NO model and NO
    report at all. Measured on the deterministic corpus: 5 of 29 workbooks, every one of them a
    Challenge/Solution or "(copy)" pair of plain Excel/Access datasources over the SAME file.

    Each such relation's upstream is not ambiguous, it is simply implicit -- it is that datasource's
    single connection -- so rebuild it here in the exact key shape :func:`_connection_facts`
    produces, which is what a resolved federated ``relation["connection"]`` already holds and what
    ``_effective_connection`` / ``_flatfile_path_for`` / ``migrate_estate._land_combined_flatfiles``
    all consume. ``flatfile_path`` is deliberately NOT carried: ``_connection_facts`` has no such
    key, and the relative path it would hold is exactly what ``_flatfile_path_for`` rebuilds from
    ``directory`` + ``filename`` anyway (the driver's ABSOLUTE path is pinned per-relation, above
    the connection).

    Fail-closed: returns ``None`` when the datasource names no connector class, so nothing is
    attributed on a shape we cannot describe.
    """
    cls = (desc.get("connection_class") or "").strip()
    if not cls:
        return None
    facts = {
        "connection_class": cls,
        "server": desc.get("server"),
        "database": desc.get("database"),
        "warehouse": desc.get("warehouse"),
        "http_path": desc.get("http_path"),
        "schema": None,
        "auth_method": desc.get("auth_method"),
        "filename": desc.get("flatfile_filename"),
        "directory": desc.get("flatfile_directory"),
    }
    for key, val in desc.items():
        if key.startswith("odbc") or key in ("driver", "dsn", "port"):
            facts[key] = val
    return facts


def combine_descriptors(descriptors, *, captions=None):
    """Combine several single-datasource descriptors (each from :func:`parse_tds`) into ONE.

    A Tableau *workbook* is one workbook with several embedded datasources. Combining their parsed
    descriptors lets :func:`assemble_model.assemble_import_model` build a SINGLE semantic model that
    holds EVERY datasource's tables -- kept as disconnected islands, sharing only the one Date
    dimension the assembler synthesizes -- instead of one model (and one split report) per
    datasource. The report then binds to that one model in a single pass.

    The combination is a TOTAL union: every input table is carried into the output, so no datasource
    is ever silently dropped. Embedded datasources are independent, so two may share a physical table
    name; because the assembler keys each table file by its display name, a table whose name is
    already taken by an earlier datasource is disambiguated (suffixed with that datasource's caption)
    and EVERY in-descriptor reference to it -- relationship endpoints and logical-field bindings --
    is rewritten to match, so nothing dangles and nothing is overwritten.

    ``captions`` (optional, positional-parallel to ``descriptors``) is the human name used to
    disambiguate a collision; each entry falls back to the descriptor's own ``datasource_name``.
    Returns a single descriptor of the same shape :func:`parse_tds` returns; a one-descriptor input
    is returned unchanged. Per-relation ``connection`` refs are preserved and the named-connection
    maps are unioned, so mixed-connection / mixed-storage sources still emit each table faithfully.
    """
    kept = [d for d in (descriptors or []) if d]
    if not kept:
        raise ValueError("combine_descriptors needs at least one descriptor")
    if len(kept) == 1:
        return kept[0]
    captions = list(captions or [])

    combined = {k: kept[0].get(k) for k in _COMBINE_SCALAR_KEYS}
    # Carry the primary's ODBC facts (odbc_*/driver/dsn) so a generic-ODBC primary still emits.
    for k, v in kept[0].items():
        if k.startswith("odbc") or k in ("driver", "dsn"):
            combined.setdefault(k, v)
    combined.update({
        "relations": [], "relationships": [], "relationship_warnings": [],
        "logical_fields": [], "connections": {}, "unsupported_reasons": [],
    })

    def _display(rel):
        return rel.get("name") or rel.get("item") or "Table"

    taken = set()  # lowercased display names already used across all combined datasources
    # base-table -> consolidated-name map the consolidation *computes* here, keyed
    # "<datasource caption>||<original base name>" so an authoring/second-compiler pass can
    # resolve a field to its final consolidated table (surfaced as report["table_map"], Spec 6).
    table_map = {}
    for i, desc in enumerate(kept):
        caption = ((captions[i] if i < len(captions) else None)
                   or desc.get("datasource_name") or f"ds{i + 1}")
        # A single-connection datasource never stamps a per-relation `connection` (its scalar facts
        # ARE the connection). In the combined multi-connection descriptor each table must route to
        # its OWN upstream, so inherit that lone connection's facts onto this datasource's tables.
        # A PLAIN (non-federated) datasource declares no `<named-connection>` at all, so its map is
        # empty rather than one-entry -- reconstruct its lone upstream from its own scalar facts
        # (see `_self_connection_facts`), otherwise consolidating two such datasources makes every
        # relation look unroutable and the whole workbook rebuild is declined.
        dconns = list((desc.get("connections") or {}).values())
        origin_conn = dconns[0] if len(dconns) == 1 else None
        if origin_conn is None and not dconns:
            origin_conn = _self_connection_facts(desc)
            if origin_conn is not None:
                # Register the reconstructed upstream as a first-class connection of the combined
                # descriptor, not only on its relations. `connection_parameter_suffixes` /
                # `connection_parameter_specs` derive the M parameter SETS from
                # `descriptor["connections"]`, so leaving it empty here would emit ONE shared
                # Server/Database/Warehouse set while each relation routed to its own upstream --
                # exactly the refresh-breaking (and, between two servers of the same class, SILENTLY
                # wrong-data) shape fixed for federated sources in 2.60.0. Keyed by island position
                # so the id is deterministic, unique, and cannot collide with a Tableau
                # named-connection id.
                combined["connections"][f"__island{i}__"] = origin_conn
        rename = {}  # old display -> new display, for THIS datasource only
        for rel in desc.get("relations", []) or []:
            if rel.get("kind") not in ("table", "custom_sql"):
                combined["relations"].append(rel)  # combination markers pass through untouched
                continue
            name = _display(rel)
            orig = name
            if name.lower() in taken:
                new = f"{name} ({caption})"
                n = 2
                while new.lower() in taken:
                    new = f"{name} ({caption} {n})"
                    n += 1
                rename[name] = new
                rel = dict(rel, name=new)
                name = new
            if origin_conn is not None and not rel.get("connection"):
                rel = dict(rel, connection=origin_conn)
            taken.add(name.lower())
            # Tag this relation with its island's datasource caption so the M field resolver can be
            # scoped per island (build_m_field_resolver(descriptor, datasource=caption)). Ride the
            # possibly-renamed/connection-tagged rel; markers (line 1475) are never tagged.
            rel = dict(rel, source_datasource=caption)
            combined["relations"].append(rel)
            # Record base -> consolidated. A same-display self-join within one datasource keys
            # the second (suffixed) copy by its final name so both stay reachable.
            key = f"{caption}||{orig}"
            if key in table_map and table_map[key] != name:
                key = f"{caption}||{name}"
            table_map[key] = name
        for r in desc.get("relationships", []) or []:
            if rename:
                r = dict(r,
                         from_table=rename.get(r.get("from_table"), r.get("from_table")),
                         to_table=rename.get(r.get("to_table"), r.get("to_table")))
            combined["relationships"].append(r)
        for lf in desc.get("logical_fields", []) or []:
            if rename and lf.get("table") in rename:
                lf = dict(lf, table=rename[lf["table"]])
            combined["logical_fields"].append(lf)
        combined["relationship_warnings"].extend(desc.get("relationship_warnings", []) or [])
        # Attribute each island's reason to the datasource that OWNS it. The consolidated model is
        # reported under the RANKED PRIMARY datasource's caption, so an unattributed reason raised by
        # a SECONDARY island is read as the primary's. Reported with #124: the failure message named
        # 'Big Data Source' for two column-less relations that 'Small Data Source' owned, sending the
        # reader to inspect the one datasource in that workbook with nothing wrong with it.
        combined["unsupported_reasons"].extend(
            ("%s: %s" % (caption, r) if caption and not str(r).startswith("%s: " % caption) else r)
            for r in (desc.get("unsupported_reasons", []) or []))
        for cid, facts in (desc.get("connections") or {}).items():
            combined["connections"].setdefault(cid, facts)
    # This descriptor spans every datasource's upstreams, so it IS a multi-named-connection source:
    # the count (>1 by construction, since a single input returns early above) is what makes
    # emit_table_tmdl_m route each relation to its OWN connection via _effective_connection, exactly
    # as it already does for a federated source with several named connections.
    combined["named_connection_count"] = sum(
        int(d.get("named_connection_count") or 1) for d in kept)
    combined["table_map"] = table_map
    # Aggregate each island's hidden-column prune. ``parse_tds`` already pruned every embedded
    # datasource IN PLACE before combining (the physical collapse has happened; the emitted table
    # column lists are the pruned ones), and stamped each descriptor's ``hidden_prune``. Sum them so
    # the consolidated descriptor carries the workbook-wide totals -- without this the combined report
    # would honestly emit the pruned model yet UNDER-report the prune as absent (``column_prune: None``
    # / ``columns_pruned_hidden_total: 0``). Connector-agnostic (keys only on the per-island prune
    # dicts). None when no island hid anything (e.g. all-Superstore workbooks) -> the key is omitted,
    # so a no-hidden combine stays byte-identical.
    prunes = [d.get("hidden_prune") for d in kept if d.get("hidden_prune")]
    if prunes:
        combined["hidden_prune"] = {
            "columns_emitted": sum(int(p.get("columns_emitted") or 0) for p in prunes),
            "columns_pruned_hidden": sum(int(p.get("columns_pruned_hidden") or 0) for p in prunes),
        }
    return combined


# -- M / TMDL emission ---------------------------------------------------------
_PARAM_META = 'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'


def _param_token(text):
    """A TMDL/M-safe token for a parameter-name suffix."""
    return re.sub(r"[^0-9A-Za-z]+", "_", (text or "conn")).strip("_") or "conn"


def _connection_identity(conn):
    """What makes one upstream connection distinct from another, for parameter naming.

    Content, not object identity: the relation's connection dict and the descriptor's entry for it
    are separate objects, and two entries describing the SAME upstream legitimately share one
    parameter set.
    """
    c = conn or {}
    return (c.get("connection_class"), c.get("server"), c.get("database"),
            c.get("warehouse"), c.get("http_path"))


def connection_parameter_suffixes(descriptor):
    """Map each distinct upstream connection to the suffix its M parameters carry.

    Empty for a single-connection datasource -- that case keeps the bare ``Server`` / ``Database`` /
    ``Warehouse`` / ``HttpPath`` names, so its emitted M and TMDL are byte-identical to before.

    A FEDERATED datasource is the reason this exists. Each of its tables binds to its own upstream
    (``_effective_connection``), so a single shared parameter set cannot describe them: two tables
    end up referencing parameters that are never defined, and every table silently points at the
    first connection's host. Suffixing by connector class keeps the names readable
    (``Server_snowflake``, ``Warehouse_snowflake``) and a numeric tail disambiguates the case where
    one datasource federates two connections of the SAME class.
    """
    conns = (descriptor or {}).get("connections") or {}
    if len(conns) <= 1:
        return {}
    used = {}
    out = {}
    for key in sorted(conns):
        ident = _connection_identity(conns[key])
        if ident in out:
            continue
        base = _param_token(conns[key].get("connection_class") if conns[key] else None)
        used[base] = used.get(base, 0) + 1
        out[ident] = base if used[base] == 1 else f"{base}{used[base]}"
    return out if len(out) > 1 else {}


def connection_parameter_specs(descriptor):
    """Every M parameter this descriptor's partitions will reference: ``[(name, value, todo)]``.

    THE single source of truth for the connection-parameter set. Three consumers previously derived
    it independently and disagreed with each other: ``emit_connection_parameters`` wrote the
    definitions, ``_expression_names`` declared them in ``model.tmdl``'s query order, and
    ``_connect_expr`` referenced them from each partition. A Snowflake source, for instance, got
    ``Database`` declared in the query order but never defined (Snowflake reaches its database by
    navigation) while the ``Warehouse`` it does define went undeclared. Deriving all three from one
    function makes those disagreements unrepresentable.
    """
    descriptor = descriptor or {}
    cls = (descriptor.get("connection_class") or "").lower()
    # Generic ODBC (and native engines routed over ODBC) inline the whole connection string inside
    # Odbc.Query/Odbc.DataSource and never reference these parameters, so emitting them would only
    # leave unused expressions.
    if cls in (ODBC_CLASSES | NATIVE_ODBC_ENGINES):
        return []

    suffixes = connection_parameter_suffixes(descriptor)
    if suffixes:
        conns = descriptor.get("connections") or {}
        specs = []
        seen = set()
        for key in sorted(conns):
            ident = _connection_identity(conns[key])
            if ident in seen:
                continue
            seen.add(ident)
            specs.extend(_one_connection_param_specs(conns[key], suffixes.get(ident, "")))
        return specs
    return _one_connection_param_specs(descriptor, "")


def _one_connection_param_specs(conn, suffix):
    """The parameter set for ONE upstream connection, named with ``suffix`` when federated."""
    conn = conn or {}
    sfx = f"_{suffix}" if suffix else ""
    spec = connector_spec(conn.get("connection_class"))
    connect_style = spec[1] if spec else None
    no_database = ("server_only", "server_warehouse", "server_httppath")
    out = []
    if conn.get("server"):
        out.append((f"Server{sfx}", conn["server"], None))
    if conn.get("database") and connect_style not in no_database:
        out.append((f"Database{sfx}", conn["database"], None))
    if connect_style == "server_warehouse":
        raw = (conn.get("warehouse") or "").strip()
        todo = None if raw else (
            f'TODO: the Snowflake warehouse was empty in the .tds; set #"Warehouse{sfx}" '
            "to a valid compute warehouse before refresh")
        out.append((f"Warehouse{sfx}", raw, todo))
    if connect_style == "server_httppath":
        raw = (conn.get("http_path") or "").strip()
        todo = None if raw else (
            f'TODO: the Databricks HTTP path was not found in the .tds; set #"HttpPath{sfx}" '
            "to the SQL warehouse HTTP path before refresh")
        out.append((f"HttpPath{sfx}", raw, todo))
    return out


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

    A FEDERATED datasource emits one such set PER upstream connection (suffixed by connector
    class), because each of its tables binds to its own connection -- see
    ``connection_parameter_specs``.
    """
    lines = []
    for name, value, todo in connection_parameter_specs(descriptor):
        line = f'expression {name} = "{escape_m_string(value or "")}" {_PARAM_META}\n'
        if todo:
            # A TMDL description (///, documented + deploy-safe) sits immediately above the
            # expression it annotates, so an unusable default is flagged where it is read.
            line = f"/// {todo}\n" + line
        lines.append(line)
    # Flat-file Import landed inside the .pbip: a relocatable SourceFolder parameter (default = the
    # absolute .Data folder) that the emitted File.Contents references, so the project survives being
    # moved/zipped -- the recipient re-points this one parameter instead of every hard-coded path.
    if (descriptor or {}).get("flatfile_source_folder"):
        lines.append(
            f'expression SourceFolder = "{escape_m_string(descriptor["flatfile_source_folder"])}" '
            f'{_PARAM_META}\n')
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


def _unquote_tableau_identifier(raw):
    """Strip Tableau's ``[...]`` brackets and, inside them, its identifier QUOTING.

    A name containing a space (or other special) is quoted inside the brackets --
    ``['Master Date List$']`` -- with any inner quote doubled, exactly like a quoted SQL identifier.
    Stripping only the brackets leaves the quotes embedded in the name, which then matches no sheet
    / no table and yields a partition that opens and loads ZERO rows without erroring.

    Fail-closed: the unquote fires only when the FIRST and LAST characters are the SAME quote
    character and something remains between them, so an unquoted name and a name that merely
    contains an apostrophe (``John's Data``) are both left untouched.
    """
    s = _strip_brackets(raw or "").strip()
    for q in ("'", '"'):
        if len(s) > 2 and s[0] == q and s[-1] == q:
            return s[1:-1].replace(q + q, q)
    return s


# The culture emitted for a flat file whose rendering WE control. ``hyper_reader.write_rows_csv``
# renders every cell with Python's ``str()``, which is invariant: ``.`` decimal separator, no group
# separators, ISO-ish dates. ``en-US`` is the closest stock culture to that rendering, and naming it
# explicitly is what makes the artifact locale-PROOF rather than merely correct on the build host.
INVARIANT_CSV_CULTURE = "en-US"

# A decimal value as our own exporter writes it: optional sign, digits, optional ``.`` fraction, and
# never a group separator. Anything else in a decimal column means the file was NOT written by us.
_INVARIANT_DECIMAL_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_COMMA_DECIMAL_RE = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d+$|^-?\d+,\d+$")


def _sniff_csv_decimal_convention(path, sample_bytes=65536):
    """Classify a landed CSV's decimal convention as ``"dot"`` / ``"comma"`` / ``None``.

    ``Csv.Document`` hands Power Query the file's TEXT verbatim -- it does not render anything -- so
    the decimal convention is a property of the FILE and can be read off it deterministically. That
    matters because ``Table.TransformColumnTypes`` with no culture parses that text with the
    REFRESHING machine's locale, so a dot-decimal file silently becomes garbage on a comma-decimal
    host (measured: ``261.96`` read as ``26196``, and an inflation ratio of ``10^decimals`` that
    differs per column -- issue #110).

    Parsed with the ``csv`` module rather than split on commas, because a comma-decimal file writes
    its numbers QUOTED (``"1.234,56"``) -- naive splitting tears that into ``1.234`` and ``56``, and
    the leading fragment then reads as a dot-decimal, classifying a European file as American: the
    exact inversion this function exists to prevent.

    Fail-closed by design: a file that shows BOTH conventions, or neither, returns ``None`` and the
    emitter omits the culture exactly as before rather than guessing a locale onto the user's data.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(sample_bytes)
    except OSError:
        return None
    text = raw.decode("utf-8-sig", "replace")
    lines = text.splitlines()
    if len(lines) < 2:
        return None
    dot = comma = 0
    try:
        import csv as _csv
        rows = list(_csv.reader(lines[1:201]))
    except Exception:
        return None
    for row in rows:
        for cell in row:
            cell = (cell or "").strip()
            if not cell:
                continue
            if _INVARIANT_DECIMAL_RE.match(cell) and "." in cell:
                dot += 1
            elif _COMMA_DECIMAL_RE.match(cell):
                comma += 1
    if dot and not comma:
        return "dot"
    if comma and not dot:
        return "comma"
    return None


def flatfile_culture(path, connector, relation=None):
    """The culture to pin on ``Table.TransformColumnTypes``, plus why -> ``(culture, reason)``.

    Generated M must never depend on the ambient locale of the machine that generated it, nor of the
    machine that refreshes it. A missing culture makes it depend on BOTH, and the failure is
    invisible: the model builds, refreshes, and passes every structural gate while returning numbers
    inflated by orders of magnitude (issue #110 measured ``SUM(Sales)`` at 1,131,591,720 against a
    true 2,297,200.86, with 25/75 numeric oracle checks passing and every structural gate green).

    Three cases, and only the ones we can PROVE get a culture:

    * **A CSV this engine wrote** (an extract materialised through ``hyper_reader``) -- rendering is
      ours and is invariant, so ``en-US`` is provably correct and the artifact becomes portable.
    * **A user's CSV** -- ``Csv.Document`` passes the file's text through unchanged, so the
      convention is a property of the file and is sniffed from it. A dot-decimal file gets ``en-US``;
      a comma-decimal file gets ``de-DE``. An inconclusive sniff yields ``None``.
    * **A legacy ACE workbook** (``.xls``/``.xlsb`` via ``Excel.Workbook``) -- the reader returns
      cells as text ALREADY RENDERED in the host's locale, which is not observable at generation
      time and not observable from within M either (``Culture.Current`` returns the model's
      ``sourceQueryCulture``, not the Windows locale). No culture can be proven, so none is emitted
      and the reason names the remedy. An OOXML ``.xlsx``/``.xlsm`` is unaffected: it stores numbers
      as invariant doubles, so ``Excel.Workbook`` returns real numbers rather than rendered text.
    """
    rel = relation or {}
    if connector == "Excel.Workbook":
        if _is_zip_readable_excel_path(path):
            return None, "ooxml-numeric"
        return None, "legacy-ace-host-rendered"
    if connector != "Csv.Document":
        return None, "not-a-text-source"
    if rel.get("engine_written_csv") or rel.get("materialised_csv"):
        return INVARIANT_CSV_CULTURE, "engine-written"
    convention = _sniff_csv_decimal_convention(path)
    if convention == "dot":
        return INVARIANT_CSV_CULTURE, "sniffed-dot"
    if convention == "comma":
        return "de-DE", "sniffed-comma"
    return None, "inconclusive"


def _excel_navigation(relation):
    """The ``(item, kind)`` pair to navigate an Excel relation with, e.g. ``("Orders", "Sheet")``.

    Tableau records an Excel object with the legacy ACE/OLEDB identifier in the relation's ``table``
    attribute, and Power Query's ``Excel.Workbook`` does not accept that form -- a ``$``-suffixed key
    refreshes as *"The key didn't match any rows in the table"*, a failure that appears ONLY at
    refresh in Desktop, long after every structural gate has passed (issue #108).

    The ACE convention carries the object's KIND, so both halves are decided here from one reading:

    * ``[Orders$]``            -> ``("Orders", "Sheet")``        -- a worksheet.
    * ``[Sheet1$A1:D100]``     -> ``("Sheet1", "Sheet")``        -- a RANGE on a sheet; Power Query
      navigates the sheet, and the range's own bounds are not expressible as a navigation key.
    * ``[MyNamedRange]``       -> ``("MyNamedRange", "DefinedName")`` -- no ``$`` means it is not a
      worksheet, and navigating it as ``Kind="Sheet"`` fails the same way.
    * ``[Book].[Orders$]``     -> ``("Orders", "Sheet")``        -- a schema-qualified identifier
      keeps only its LAST segment.

    The kind is inferred ONLY from the authoritative ``raw_table``/``item`` (which carry the ``$``
    convention). When neither is present the relation's plain ``name`` is used, and that name never
    carried the convention in the first place -- so the kind stays ``Sheet``, which is both the
    overwhelmingly common case and the historical behaviour.
    """
    authoritative = relation.get("raw_table") or relation.get("item") or ""
    raw = authoritative or relation.get("name") or ""
    s = _unquote_tableau_identifier(raw)
    # A quoted-then-bracketed identifier ('[Orders$]') needs both peels, in either order.
    for _ in range(3):
        peeled = _unquote_tableau_identifier(s)
        if peeled == s:
            break
        s = peeled
    if "].[" in s:                      # schema/workbook-qualified -> keep the object itself
        s = _unquote_tableau_identifier(s.rsplit("].[", 1)[-1])
    s = s.strip()
    if not s:
        return "", "Sheet"
    if "$" in s:
        sheet, _, tail = s.partition("$")
        # ``Sheet1$`` is the sheet; ``Sheet1$A1:D100`` is a range ON that sheet. Either way the
        # navigable object is the sheet, and a sheet name cannot itself contain ``$``.
        return (sheet or s), "Sheet"
    if authoritative:
        return s, "DefinedName"
    return s, "Sheet"


def _excel_sheet_name(relation):
    """The Excel sheet name to navigate for a relation (``[Orders$]`` -> ``Orders``).

    Thin wrapper over :func:`_excel_navigation` kept for the header-reconciliation caller, which
    needs the sheet name only. A named range resolves to its own name, which is what
    ``read_flatfile_headers`` should look for.
    """
    return _excel_navigation(relation)[0]


def _access_table_name(relation):
    """The Access table name to navigate for a relation (``[factTable]`` -> ``factTable``).

    Unlike a sheet there is no ``$`` suffix convention, so the name is used as written (minus
    Tableau's brackets/quoting) -- an Access table may legitimately end in ``$``.
    """
    return _unquote_tableau_identifier(
        relation.get("raw_table") or relation.get("item") or relation.get("name") or "")


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


def _col_ref_to_index(letters):
    """Excel column letters -> 0-based index (``A`` -> 0, ``B`` -> 1, ``AA`` -> 26)."""
    idx = 0
    for ch in letters.upper():
        if "A" <= ch <= "Z":
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _read_xlsx_sheet_headers(path):
    """Return ``{sheet_name: [ordered first-row header strings]}`` for an ``.xlsx``/``.xlsm``.

    Stdlib only (an xlsx is a zip of XML parts): resolves shared strings, the workbook's sheet
    order, and each sheet's first row into ordered header text. Fail-closed -- returns ``{}`` on any
    problem so a caller treats the file as unreadable rather than raising.
    """
    import zipfile as _zip
    import xml.etree.ElementTree as _ET
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    RELNS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    out = {}
    try:
        with _zip.ZipFile(path) as z:
            names = set(z.namelist())
            shared = []
            if "xl/sharedStrings.xml" in names:
                sroot = _ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in sroot.findall(f"{NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
            wroot = _ET.fromstring(z.read("xl/workbook.xml"))
            sheets = []
            sheets_el = wroot.find(f"{NS}sheets")
            if sheets_el is not None:
                for sh in sheets_el.findall(f"{NS}sheet"):
                    sheets.append((sh.get("name"), sh.get(f"{RNS}id")))
            rid_target = {}
            if "xl/_rels/workbook.xml.rels" in names:
                rroot = _ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
                for rel in rroot.findall(f"{RELNS}Relationship"):
                    rid_target[rel.get("Id")] = rel.get("Target")
            for sname, rid in sheets:
                target = rid_target.get(rid)
                if not sname or not target:
                    continue
                member = target if target.startswith("xl/") else "xl/" + target.lstrip("/")
                if member not in names:
                    continue
                wsroot = _ET.fromstring(z.read(member))
                data = wsroot.find(f"{NS}sheetData")
                first = data.find(f"{NS}row") if data is not None else None
                cells = []
                if first is not None:
                    for c in first.findall(f"{NS}c"):
                        ref = c.get("r") or ""
                        col_idx = _col_ref_to_index("".join(ch for ch in ref if ch.isalpha()))
                        t = c.get("t")
                        val = ""
                        if t == "s":
                            v = c.find(f"{NS}v")
                            if v is not None and v.text is not None:
                                try:
                                    val = shared[int(v.text)]
                                except (ValueError, IndexError):
                                    val = ""
                        elif t == "inlineStr":
                            is_el = c.find(f"{NS}is")
                            if is_el is not None:
                                val = "".join(tt.text or "" for tt in is_el.iter(f"{NS}t"))
                        else:
                            v = c.find(f"{NS}v")
                            val = v.text if (v is not None and v.text is not None) else ""
                        cells.append((col_idx, val))
                cells.sort(key=lambda x: x[0])
                out[sname] = [v for _, v in cells]
    except Exception:
        return {}
    return out


def _is_excel_path(path):
    """True for any Excel workbook, INCLUDING the legacy binary formats.

    ``.xls``/``.xlsb`` are not readable as zips, so header RECONCILIATION still degrades to "cannot
    read" for them -- but they are unambiguously Excel, and treating them as not-Excel meant the
    sheet-name decision was skipped for exactly the legacy files that need it most (issue #108 was
    filed against a ``.xls``).
    """
    try:
        return str(path).lower().endswith((".xlsx", ".xlsm", ".xls", ".xlsb"))
    except Exception:
        return False


def _is_zip_readable_excel_path(path):
    """True only for the OOXML (zip) Excel formats whose sheets this module can actually read.

    ``read_flatfile_headers`` opens a workbook as a zip; a legacy ``.xls``/``.xlsb`` is OLE/binary
    and simply is not readable that way, so header reconciliation must skip it (fail-closed) rather
    than mis-parse it.
    """
    try:
        return str(path).lower().endswith((".xlsx", ".xlsm"))
    except Exception:
        return False


def read_flatfile_headers(path, *, sheet=None):
    """Ordered physical column headers of a landed flat file, or ``None`` if it can't be read.

    For an ``.xlsx``/``.xlsm`` pass the ``sheet`` name (the bare Excel sheet, e.g. ``People``); for a
    ``.csv``/``.txt`` the first line is split on commas. Fail-closed -- never raises; returns ``None``
    when the file is missing/unreadable (so reconciliation simply no-ops and emission is unchanged).
    """
    import os as _os
    if not path:
        return None
    try:
        if not _os.path.isfile(path):
            return None
    except Exception:
        return None
    if _is_excel_path(path):
        sheets = _read_xlsx_sheet_headers(path)
        if not sheets:
            return None
        if sheet is not None and sheet in sheets:
            return sheets[sheet]
        if len(sheets) == 1:
            return next(iter(sheets.values()))
        return sheets.get(sheet)
    try:
        import csv as _csv
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            for row in _csv.reader(fh):
                return list(row)
        return []
    except Exception:
        return None


def reconcile_flatfile_headers(descriptor):
    """Align each flat-file relation's source column names to the ACTUAL headers in the landed data.

    Tableau can expose a column under an ALIAS (its ``remote-name``, e.g. ``Person``) that never
    appears as a physical Excel/CSV header (physically ``Regional Manager``). Emitting M that types
    the alias makes Power BI fail to load the query (*"The column 'Person' of the table wasn't
    found"*). Because every column of a flat-file relation is DERIVED FROM that file, a column whose
    ``remote_name`` is not a physical header must be an aliased/renamed view of some physical column
    that no exactly-named column claims. This reconciles each relation deterministically:

    * exact-name columns bind first and CLAIM their header (a working column is never stolen);
    * the leftover unmatched columns are paired to the leftover unclaimed headers *positionally* --
      ordered by the ``.tds``'s own physical ``<ordinal>`` (falling back to document order) against
      the file's header order -- but ONLY when the two counts match, so the pairing is unambiguous.
      This is the common real case (one aliased column, e.g. ``Person`` -> ``Regional Manager``) and
      needs no absolute index, so it is robust whether Tableau numbers ordinals per-sheet (``0``) or
      datasource-globally (``21``);
    * if the counts differ (e.g. a physical column was dropped as an unsupported type), each
      remaining column is remapped by absolute ``<ordinal>`` only when that lands on an unclaimed
      header, and is otherwise left untouched and reported as a ``mismatch`` -- never a wrong bind.

    Mutates only a COPY of the affected relations/columns on ``descriptor`` (the caller's original
    relation dicts are untouched) and returns ``{"remaps": [...], "mismatches": [...]}``. A live DB
    source (no flat file) or a file that can't be read is a no-op -> emission byte-identical.
    """
    result = {"remaps": [], "mismatches": []}
    if not descriptor.get("flatfile_filename") and not descriptor.get("flatfile_path"):
        return result
    orig = descriptor.get("relations") or []
    ff_path = descriptor.get("flatfile_path")
    copied = None

    def _ord(col):
        ov = col.get("ordinal")
        return ov if (isinstance(ov, int) and not isinstance(ov, bool)) else None

    for idx, rel in enumerate(orig):
        if rel.get("kind") != "table":
            continue
        cols = rel.get("columns") or []
        if not cols:
            continue
        path = rel.get("flatfile_path") or ff_path
        if not path:
            continue
        sheet = _excel_sheet_name(rel) if _is_zip_readable_excel_path(path) else None
        headers = read_flatfile_headers(path, sheet=sheet)
        if not headers:
            continue
        header_set = set(headers)

        header_pos = {h: i for i, h in enumerate(headers)}

        # 1. Exact-name columns bind to (and claim) their physical header. Each exact anchor that
        #    carries an ordinal votes for a single global->local offset (ordinal - header index):
        #    real .tds ordinals are datasource-GLOBAL, so a bare `headers[ordinal]` would wrong-bind.
        claimed = set()
        unmatched = []  # [(cpos, col)] in document order
        anchor_offsets = []  # ordinal - physical index, one per exact anchor with an ordinal
        for cpos, col in enumerate(cols):
            rn = col.get("remote_name")
            if rn in header_set and rn not in claimed:
                claimed.add(rn)
                ov = _ord(col)
                if ov is not None:
                    anchor_offsets.append(ov - header_pos[rn])
            else:
                unmatched.append((cpos, col))
        if not unmatched:
            continue  # every column already types a real header
        unclaimed = [h for h in headers if h not in claimed]  # physical order

        # A single consistent offset across every exact anchor lets us translate a global ordinal
        # into a physical header index; disagreement (or no anchors) means we cannot trust ordinals.
        offset = anchor_offsets[0] if (anchor_offsets and len(set(anchor_offsets)) == 1) else None

        rel_changes = []  # [(cpos, new_header)]

        def _remap(cpos, col, header, via):
            rel_changes.append((cpos, header))
            claimed.add(header)
            result["remaps"].append({
                "relation": rel.get("name"), "from": col.get("remote_name"), "to": header,
                "model_name": col.get("model_name"), "via": via})

        def _mismatch(col):
            result["mismatches"].append({
                "relation": rel.get("name"), "source_column": col.get("remote_name"),
                "model_name": col.get("model_name"), "headers": list(headers)})

        if len(unmatched) == 1 and len(unclaimed) == 1:
            # 2a. Exactly one leftover column and one leftover header -> unambiguous (no ordinal
            #     needed): every flat-file column derives from the file, so the lone unmatched
            #     column must be an aliased view of the lone remaining header. (The People case.)
            (cpos, col) = unmatched[0]
            _remap(cpos, col, unclaimed[0], "positional")
        elif len(unmatched) == len(unclaimed) and all(_ord(c) is not None for _, c in unmatched):
            # 2b. Equal counts, >1: pair leftover columns to leftover headers positionally. Require a
            #     real ordinal on EVERY unmatched column so we can order them into physical order;
            #     ordinals preserve relative physical order (global or local), so a sort aligns them
            #     with the physically-ordered unclaimed headers. Without ordinals this is unsafe (doc
            #     order may be display order) -> fall through to a warn.
            ordered = sorted(unmatched, key=lambda pc: (_ord(pc[1]), pc[0]))
            for (cpos, col), header in zip(ordered, unclaimed):
                _remap(cpos, col, header, "positional")
        elif offset is not None:
            # 2c. Counts differ (dropped/hidden physical columns): translate each ordinal through the
            #     exact-anchor-derived offset; only bind when it lands on an in-range, unclaimed
            #     header, warn on anything still unresolved.
            for cpos, col in unmatched:
                ov = _ord(col)
                hi = None if ov is None else ov - offset
                if hi is not None and 0 <= hi < len(headers) and headers[hi] not in claimed:
                    _remap(cpos, col, headers[hi], "ordinal")
                else:
                    _mismatch(col)
        else:
            # 2d. No safe basis (no exact-anchor offset, and not an unambiguous/ordinal-ordered
            #     equal-count case) -> never guess; surface every leftover as a mismatch.
            for _cpos, col in unmatched:
                _mismatch(col)

        if rel_changes:
            if copied is None:
                copied = [dict(r) for r in orig]
            new_cols = [dict(c) for c in cols]
            for cpos, header in rel_changes:
                new_cols[cpos] = {**new_cols[cpos], "remote_name": header}
            copied[idx] = {**copied[idx], "columns": new_cols}
    if copied is not None:
        descriptor["relations"] = copied
    return result


def _flatfile_contents_expr(path, conn):
    """The ``File.Contents(...)`` expression for a flat-file ``path``.

    When the descriptor opts into a relocatable source folder (``flatfile_source_folder`` -- an
    absolute directory that CONTAINS this file, set when the data is landed INSIDE the ``.pbip``),
    emit ``File.Contents(#"SourceFolder" & "\\<basename>")`` so the model references the
    ``SourceFolder`` Power Query parameter instead of a hard-coded absolute path. Moving or zipping
    the project then only needs that one parameter re-pointed. A file that is NOT under the source
    folder, or when no folder is set, falls back to the absolute path -- byte-identical to before.
    """
    folder = (conn or {}).get("flatfile_source_folder")
    if folder:
        parts = re.split(r"[\\/]", path)
        base = parts[-1]
        file_dir = "/".join(parts[:-1]).rstrip("/").lower()
        want = re.sub(r"[\\/]+$", "", str(folder)).replace("\\", "/").lower()
        if base and file_dir == want:
            # Windows separator: the emitted .pbip opens in Power BI Desktop on Windows and the
            # SourceFolder default is an absolute Windows directory with no trailing separator.
            return f'File.Contents(#"SourceFolder" & "{escape_m_string(chr(92) + base)}")'
    return f'File.Contents("{escape_m_string(path)}")'


def locale_dependent_flatfile_relations(descriptor):
    """``[{table, path, reason}]`` for relations whose typed M would depend on the AMBIENT locale.

    Generated M must never depend on the locale of the machine that generated it, nor of the machine
    that refreshes it. :func:`flatfile_culture` pins a culture wherever the rendering can be proven,
    which covers a CSV outright. What it cannot cover is a LEGACY ACE workbook (``.xls``/``.xlsb``):
    that reader returns cells as text already rendered in the host's locale, and the rendering
    locale is neither knowable at generation time nor observable from within M -- ``Culture.Current``
    returns the model's ``sourceQueryCulture``, not the Windows locale. So that case is REPORTED
    rather than guessed, because a wrong guess is worse than none: pinning ``en-US`` on a comma-
    decimal host is a no-op that leaves the values corrupt, and pinning the wrong European culture
    fixes the dates while leaving the numbers wrong.

    Returns ``[]`` for every source whose culture is proven or irrelevant, so a clean model reports
    nothing.
    """
    out = []
    conns = {}
    for c in (descriptor.get("connections") or []):
        if isinstance(c, dict) and c.get("name"):
            conns[c["name"]] = c
    for rel in descriptor.get("relations") or []:
        if rel.get("kind") not in ("table", "custom_sql"):
            continue
        conn = conns.get(rel.get("connection")) or descriptor
        cls = (conn.get("class") or conn.get("connection_class")
               or descriptor.get("connection_class") or "").lower()
        connector = FLAT_FILE_CLASSES.get(cls)
        if connector is None:
            continue
        path = (rel.get("flatfile_path") or _flatfile_path_for(conn)
                or _flatfile_path_for(descriptor))
        culture, reason = flatfile_culture(path, connector, rel)
        if culture is None and reason == "legacy-ace-host-rendered":
            out.append({"table": _table_display(rel), "path": path, "reason": reason})
    return out


def emit_flatfile_source(relation, conn, cls):
    """Emit a real, typed Import ``let ... in`` body for an Excel/CSV ("full data") relation.

    Builds a deterministic, deploy-ready Power Query: read the file, promote the header row, and set
    each column's type from the parsed Tableau metadata. The promoted headers keep their RAW names;
    each TMDL column binds to that raw name via its ``sourceColumn`` (no ``Table.RenameColumns`` --
    a rename above the source is unnecessary once the binding is declarative, and would break query
    folding on DirectQuery-capable sources).
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

    contents = _flatfile_contents_expr(path, conn)
    steps = []
    if connector == "Excel.Workbook":
        item, kind = _excel_navigation(relation)
        # Belt AND braces. The identifier normalisation above is the fix; this assertion is what
        # makes it a GUARANTEE rather than a claim, because the failure it prevents is invisible to
        # every structural gate -- the model validates, opens, and passes the definition of done,
        # then fails at refresh in Desktop with "The key didn't match any rows in the table".
        # A ``$``-suffixed key can only mean an un-normalised ACE identifier reached this point, and
        # no Excel sheet name may contain ``$``, so this can never fire on legitimate input.
        if item.endswith("$"):
            item = item[:-1]
        sheet = escape_m_string(item)
        steps.append(f'Source = Excel.Workbook({contents}, null, true)')
        steps.append(f'Navigation = Source{{[Item="{sheet}", Kind="{kind}"]}}[Data]')
        steps.append("Promoted = Table.PromoteHeaders(Navigation, [PromoteAllScalars=true])")
        prev = "Promoted"
    elif connector == "Access.Database":
        # Access.Database(database as binary, optional options) -- doc-verified signature. The
        # `options` record is deliberately OMITTED: its only relevant member,
        # `CreateNavigationProperties`, defaults to false, and setting it true (what the Power BI
        # UI generates) grafts relationship-navigation COLUMNS onto the returned table -- columns
        # the TMDL never declares, so the partition would no longer match the model.
        #
        # An Access table is keyed `[Schema="", Item="<table>"]` -- Access has no schema concept, so
        # the schema is the empty string, NOT absent. There is also NO Table.PromoteHeaders step:
        # unlike a sheet or a CSV, Access returns a real table whose column names are already its
        # own; promoting would consume the first data ROW as the header.
        table = escape_m_string(_access_table_name(relation))
        steps.append(f'Source = Access.Database({contents})')
        steps.append(f'Navigation = Source{{[Schema="", Item="{table}"]}}[Data]')
        prev = "Navigation"
    else:  # Csv.Document
        steps.append(
            f'Source = Csv.Document({contents}, '
            '[Delimiter=",", Encoding=1252, QuoteStyle=QuoteStyle.Csv])')
        steps.append("Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])")
        prev = "Promoted"

    # Type by the RAW promoted header (the Tableau remote name) and STOP there: the query output
    # keeps the raw header names, and each TMDL column binds to that raw name via its sourceColumn.
    # We deliberately do NOT rename to the model name -- a Table.RenameColumns step is unnecessary
    # once the binding is declared in TMDL, and it would break query folding on DirectQuery-capable
    # sources exactly as it does for Value.NativeQuery (post-rename names referenced against the
    # pre-rename subquery).
    type_pairs = []
    for c in cols:
        remote = c.get("remote_name") or c["model_name"]
        mt = _M_TYPE.get(c["tmdl_type"])
        if mt:
            type_pairs.append(f'{{"{escape_m_string(remote)}", {mt}}}')
    if type_pairs:
        # CULTURE. Without a third argument, Table.TransformColumnTypes parses with the AMBIENT
        # locale -- of whichever machine refreshes the model -- so a dot-decimal source silently
        # becomes garbage on a comma-decimal host (issue #110: SUM(Sales) 1,131,591,720 against a
        # true 2,297,200.86, every structural gate green). Pinned only where the rendering can be
        # PROVEN; an unprovable case emits exactly what it did before rather than guessing a locale
        # onto the user's data, which would corrupt a genuinely European file the other way.
        culture, _reason = flatfile_culture(path, connector, relation)
        culture_arg = f', "{escape_m_string(culture)}"' if culture else ""
        steps.append(
            f"Typed = Table.TransformColumnTypes({prev}, "
            f"{{{', '.join(type_pairs)}}}{culture_arg})")
        prev = "Typed"

    body = ",\n\t\t\t\t".join(steps)
    return f"let\n\t\t\t\t{body}\n\t\t\tin\n\t\t\t\t{prev}"


def _connect_expr(connector, connect_style, suffix=""):
    """Build the right-hand side of ``Source = ...`` for a fully-supported connector.

    Exhaustive on ``connect_style`` -- an unrecognized style raises rather than silently falling
    back to the ``(server, database)`` form (which would emit wrong M for a different connector).

    ``suffix`` names the parameters of ONE upstream connection in a federated datasource (see
    ``connection_parameter_suffixes``); it is empty for the single-connection case, which keeps the
    emitted M byte-identical.
    """
    sfx = f"_{suffix}" if suffix else ""
    if connect_style == "server_database":  # SQL Server protocol family
        return f'{connector}(#"Server{sfx}", #"Database{sfx}")'
    if connect_style == "server_only":
        # Oracle: the service/SID lives in #"Server" and there is no separate database argument;
        # HierarchicalNavigation defaults false, so we set it explicitly so the flat Schema/Item
        # selector is correct rather than default-reliant.
        return f'{connector}(#"Server{sfx}", [HierarchicalNavigation=false])'
    if connect_style == "server_warehouse":
        return f'{connector}(#"Server{sfx}", #"Warehouse{sfx}")'
    if connect_style == "server_httppath":  # Databricks SQL warehouse (host, httpPath)
        return f'{connector}(#"Server{sfx}", #"HttpPath{sfx}")'
    raise ValueError(f"unhandled connect_style {connect_style!r} for connector {connector!r}")


def _param_suffix_for(conn, descriptor):
    """The parameter-name suffix THIS relation's connection uses, or ``""`` when not federated."""
    return connection_parameter_suffixes(descriptor).get(_connection_identity(conn), "")


def _effective_connection(relation, descriptor):
    """Return the connection facts to bind THIS relation against.

    For a federated datasource with MORE THAN ONE named connection, each relation routes to its OWN
    upstream connection (so a per-table connector function / navigation is chosen from the relation's
    own class + database). For the single-connection case the global descriptor is returned
    unchanged, so emitted M is byte-identical to the pre-routing behavior.

    NOTE: the shared ``#"Server"`` / ``#"Database"`` / ``#"Warehouse"`` / ``#"HttpPath"`` parameters
    are still emitted once per datasource by ``emit_connection_parameters``; full multi-connection
    deployment additionally needs per-connection parameters. Multi-connection sources are routed to
    the honest needs-storage-decision fallback by ``select_storage_mode`` today (land-to-Delta +
    DirectLake is opt-in only, never auto-selected), so this routing is groundwork that is never the
    deployed artifact on its own.
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


def _emit_odbc_partition(relation, conn, cls):
    """Emit the ``(source, stub_reason)`` for a generic-ODBC relation.

    Engine-agnostic by design: the connection string (rebuilt from the .tds's NON-SECRET driver/
    DSN facts) and the Custom SQL pass straight through the ODBC driver to whatever engine sits
    behind it (e.g. a query engine over object storage), so we never need to know that engine's
    dialect -- the source does the parsing.

    * Custom SQL  -> ``Source = Odbc.Query("<connStr>", "<sql>")`` (folds the query at the source);
      columns bind to the raw query headers via ``sourceColumn`` (no rename -- fold-safe). A
      surviving Tableau parameter reference in the SQL is a needs-review reason (the partition is
      still emitted), mirroring the ``Value.NativeQuery`` path.
    * A plain table -> a flagged ``Odbc.DataSource`` scaffold: generic-ODBC table navigation keys
      are driver-specific and not portable, so we never guess them.

    Fails closed to a scaffold when no connection string can be rebuilt (defensive: the storage-mode
    router already routes such a source to the needs-storage-decision fallback, so this is rarely
    reached).
    """
    conn_str = _odbc_connection_string(conn)
    if not conn_str:
        return _scaffold_review(
            cls, "Odbc.Query",
            "generic ODBC source carried neither a DSN nor a driver name, so no connection string "
            "could be reconstructed; set the ODBC connection manually")
    conn_str_m = escape_m_string(conn_str)

    if relation.get("kind") == "custom_sql":
        sql = escape_m_string(relation.get("sql", ""))
        steps = [f'Source = Odbc.Query("{conn_str_m}", "{sql}")']
        prev = "Source"
        # No Table.RenameColumns: columns bind to the raw ODBC query headers via sourceColumn
        # (fold-safe -- see the Value.NativeQuery path for why renaming in M breaks folding).
        body = ",\n\t\t\t\t".join(steps)
        param_reason = None
        params = custom_sql_parameter_refs(relation.get("sql", ""))
        if params:
            param_reason = (
                "custom SQL contains Tableau parameter reference(s) "
                f"{', '.join(params)} that are not translated to a Power Query parameter; "
                "replace them with a literal or a bound parameter before refresh")
        return f"let\n\t\t\t\t{body}\n\t\t\tin\n\t\t\t\t{prev}", param_reason

    return _scaffold_review(
        cls, "Odbc.DataSource",
        "generic-ODBC table navigation keys are driver-specific and not portable; complete the "
        "Odbc.DataSource navigation manually, or model this table via a Custom SQL relation")


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
    if cls in ODBC_CLASSES or cls in NATIVE_ODBC_ENGINES:
        # Generic ODBC and the native query engines (Spark/Presto/Trino/Starburst) bind the SAME
        # engine-agnostic way: Odbc.Query for custom SQL / a flagged Odbc.DataSource scaffold for a
        # table. Handled before connector_spec (which returns None for these classes).
        return _emit_odbc_partition(relation, conn, cls)
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
            detail = ("connector class not mapped for direct M; this datasource needs a storage "
                      "decision -- rebuild direct-to-source once a connection is supplied, or opt in "
                      "to land-to-Delta + DirectLake (never auto-selected)")
        return _scaffold_review(cls, intended, detail)

    connector, connect_style, nav_style = spec

    if relation["kind"] == "custom_sql":
        sql = escape_m_string(relation.get("sql", ""))
        if connect_style == "server_database":
            # SQL Server family: Value.NativeQuery folds against the database handle directly.
            steps = [f'Source = {_connect_expr(connector, connect_style, _param_suffix_for(conn, descriptor))}']
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
                f'Source = {_connect_expr(connector, connect_style, _param_suffix_for(conn, descriptor))}',
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
        # NO Table.RenameColumns here. The native query returns the RAW source headers, and each
        # TMDL column binds to that raw name via its sourceColumn (declarative, fold-safe). A rename
        # step would sit ABOVE the folded native query, so when Fabric folds a downstream query it
        # references the post-rename names against the pre-rename subquery -> "The name
        # 't0.Order_Date' doesn't exist in the current context" at query time (Import works locally
        # because the mashup applies the rename in-engine, but the Service folds it into SQL).
        body = ",\n\t\t\t\t".join(steps)
        # The operators are already de-escaped at parse, so a real native query is emitted. The
        # one thing we cannot complete is a recovered Tableau parameter reference
        # (<[Parameters].[Name]>): the source can't run it and we don't translate it to a Power
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

    source = _connect_expr(connector, connect_style, _param_suffix_for(conn, descriptor))

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
        # Bind each model column to its RAW remote source name via sourceColumn (declarative, in
        # TMDL) rather than renaming in the M partition. The model column NAME stays underscored
        # (clean_col) so DAX / visual bindings are unaffected; only the source-side binding is the
        # raw name -- which is fold-safe (a Table.RenameColumns above a folded native query breaks
        # at query time in Fabric: "The name 't0.Order_Date' doesn't exist").
        columns_tmdl += generate_column_tmdl(
            c["model_name"], c["tmdl_type"], summarize, bool(c.get("is_hidden")), c.get("format_string"),
            c.get("data_category"), source_column=(c.get("remote_name") or c["model_name"]))

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


def build_m_field_resolver(descriptor, datasource=None):
    """Build ``resolve_field(caption) -> (table, clean_col, tmdl_type) | None`` for the M path.

    ``datasource`` (optional) names one island of a *combined* multi-datasource descriptor (the
    ``source_datasource`` caption :func:`combine_descriptors` stamps on each relation). When given,
    a caption is resolved against THAT island's tables first, so the same caption reused by several
    embedded datasources -- which pools to an ambiguous ``None`` for every leaf across the whole
    workbook -- binds to the right physical table. It is inert for a single (un-combined) descriptor
    or ``datasource=None``: no relation carries a ``source_datasource`` tag, so the scope collapses
    to ``None`` and resolution is byte-for-byte the full-descriptor behavior below.

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
    rel_ds = {}   # table -> source_datasource (only populated in a combined descriptor; scopes resolution)
    for rel in descriptor.get("relations", []):
        if rel.get("kind") not in ("table", "custom_sql"):
            continue
        table = rel.get("name") or rel.get("item")
        sds = rel.get("source_datasource")
        if sds is not None:
            rel_ds[table] = sds
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

    # Restrict resolution to one island's tables when a datasource is named (combined descriptor only).
    # Empty match (single/un-combined descriptor, or a name no relation carries) -> None -> global.
    scoped_tables = None
    if datasource is not None:
        scoped_tables = {t for t in tables if rel_ds.get(t) == datasource} or None

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

    def _resolve_over(caption, table_set):
        hits = []
        for table in table_set:
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
        # ORDERS vs ``Region (People)`` -> PEOPLE). Only an unambiguous logical hit binds. Under a
        # scope, restrict the logical bucket to the island too (byte-identical when table_set IS the
        # full set -- ``table_set is tables`` -- so the global path is unchanged).
        bucket = logical.get((caption or "").strip().lower())
        if bucket and table_set is not tables:
            bucket = {t for t in bucket if t[0] in table_set}
        if bucket and len(bucket) == 1:
            return next(iter(bucket))
        return None

    def _island_aware(caption, table_set):
        # Reclaim a disambiguated caption '<Field> (<Object>)' that a base pass could not resolve,
        # by matching the '(<Object>)' token to the ONE relation literally named <Object>. Runs ONLY
        # after both base passes miss, and only on the '<Field> (<Object>)' shape, so a caption that
        # already resolves (via metadata or the logical bucket) is byte-identical to before.
        m = _OBJECT_SUFFIX_RE.match(caption or "")
        if m is None:
            return None
        field, obj = m.group(1).strip(), m.group(2).strip()
        if not field or not obj:
            return None
        obj_norm = _norm_obj(obj)
        matched = [t for t in table_set if _norm_obj(t) == obj_norm]
        if len(matched) != 1:  # no object, or an ambiguous object -> fail closed (never guess)
            return None
        one = {matched[0]}
        # Prefer the full disambiguated caption on that one table; else the bare field name on it.
        # Both go through _resolve_over so the single-column-per-name guard still holds (a field
        # genuinely absent on the matched object stays None).
        return _resolve_over(caption, one) or _resolve_over(field, one)

    def resolve_field(caption):
        # Island-scoped resolution wins; fall back to full-descriptor resolution so a caption living
        # outside the named island still binds when it is globally unambiguous. With no scope
        # (single descriptor, or datasource=None) this is exactly _resolve_over(caption, tables).
        # Only when every base pass misses do we consult island-aware object-token disambiguation.
        if scoped_tables is not None:
            hit = _resolve_over(caption, scoped_tables)
            if hit is not None:
                return hit
            hit = _resolve_over(caption, tables)
            if hit is not None:
                return hit
            return _island_aware(caption, scoped_tables) or _island_aware(caption, tables)
        hit = _resolve_over(caption, tables)
        if hit is not None:
            return hit
        return _island_aware(caption, tables)

    # Sibling-anchor primitive: resolve a caption restricted to a PINNED set of tables. Passing a
    # FRESH set (so ``table_set is not tables`` at the logical-layer gate is True) both scopes the
    # metadata-record pass and restricts the logical bucket to the pins, so an otherwise-AMBIGUOUS
    # caption (a system field like ``Created Date`` that maps to a column on many tables) binds iff
    # exactly one of the pinned tables carries it -- fail-closed (0 or >1 hits -> None) by reusing
    # ``_resolve_over``. Used by the orchestrator's sibling-anchored resolver; byte-identical to
    # today when the attribute is never read.
    resolve_field.resolve_in_tables = lambda caption, table_set: _resolve_over(caption, set(table_set))

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
