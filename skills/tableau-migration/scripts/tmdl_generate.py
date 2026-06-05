"""TMDL generators for Tableau -> Fabric semantic models.

Ported verbatim from the Tableau-Fabric-AI-Bridge Play 4 notebook (cell 3 / cell 3c).
The logic is unchanged; only the module-level imports were added so the generators
run as a standalone, offline-testable module.

Two families of generators live here:

* **Type mapping + column / table / measure / model / relationship TMDL** — the
  DirectLake-over-Delta path (types driven by the ACTUAL landed Delta schema).
* **Relationship inference** from Tableau's hidden disambiguated join keys.

The ``generate_measure_tmdl`` renderer is storage-mode agnostic: it preserves the
original Tableau formula as a ``TableauFormula`` annotation whether or not a DAX
translation was produced, and tags translated measures with ``TranslatedBy``.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
import xml.etree.ElementTree as ET

# -- TYPE MAPPING --------------------------------------------------------------
# Types are driven by the ACTUAL Delta schema (authoritative), NOT Tableau metadata.
# This is the core Play 4 fix: a DirectLake column's dataType must match the physical
# Parquet/Delta column, or the model fails to bind (the prior dateTime-over-varchar bug).
def spark_type_to_tmdl(t):
    """Map a Spark/Delta simpleString type to a TMDL column dataType (or None to skip)."""
    t = (t or "").lower().strip()
    if t.startswith("decimal"):
        return "decimal"
    base = {
        "string": "string", "varchar": "string", "char": "string",
        "byte": "int64", "short": "int64", "integer": "int64", "int": "int64",
        "long": "int64", "bigint": "int64",
        "float": "double", "double": "double",
        "boolean": "boolean",
        "date": "dateTime", "timestamp": "dateTime", "timestamp_ntz": "dateTime",
    }
    if t in base:
        return base[t]
    if t in ("binary", "null", "void") or t.startswith(("array", "map", "struct")):
        return None  # unsupported as a DirectLake model column
    return "string"

def slugify(s):
    s = s.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    return s.strip('_')

def make_delta_table_name(datasource_name, table_name):
    """Match the naming convention used by Play 3."""
    return f"{slugify(datasource_name)}_{slugify(table_name)}"

def clean_col(name):
    for ch in ["(", ")", " ", ",", ";", "{", "}", "/", "\\", "\n", "\t", "="]:
        name = name.replace(ch, "_")
    return name.strip("_")

# -- TMDL identifier quoting ---------------------------------------------------
# Quote any name with a char outside [A-Za-z0-9_-] or a leading digit (hyphens are
# valid unquoted, e.g. `Sub-Category`). Single-quote and escape embedded quotes.
_UNQUOTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")

def q(name):
    if _UNQUOTED.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"

def _format_string(tmdl_type, summarize):
    if tmdl_type == "dateTime":
        return "Short Date"
    if tmdl_type == "int64":
        return "#,0"
    if tmdl_type in ("double", "decimal") and summarize == "sum":
        return "#,0.00"
    return None

def generate_column_tmdl(col_name, tmdl_type, summarize, is_hidden):
    """One column. col_name is the ACTUAL Delta column name (sourceColumn must match)."""
    lines = [f"\tcolumn {q(col_name)}", f"\t\tdataType: {tmdl_type}"]
    if is_hidden:
        lines.append("\t\tisHidden")
    fmt = _format_string(tmdl_type, summarize)
    if fmt:
        lines.append(f"\t\tformatString: {fmt}")
    lines.append(f"\t\tlineageTag: {uuid.uuid4()}")
    lines.append(f"\t\tsourceLineageTag: {col_name}")
    lines.append(f"\t\tsummarizeBy: {summarize}")
    lines.append(f"\t\tsourceColumn: {col_name}")
    lines.append("")
    lines.append("\t\tannotation SummarizationSetBy = Automatic")
    return "\n" + "\n".join(lines) + "\n"

def generate_table_tmdl(table_display_name, delta_table_name, columns_tmdl, expression_source):
    return (
        f"table {q(table_display_name)}\n"
        f"\tlineageTag: {uuid.uuid4()}\n"
        f"\tsourceLineageTag: [dbo].[{delta_table_name}]\n"
        f"{columns_tmdl}\n"
        f"\tpartition {delta_table_name} = entity\n"
        f"\t\tmode: directLake\n"
        f"\t\tsource\n"
        f"\t\t\tentityName: {delta_table_name}\n"
        f"\t\t\tschemaName: dbo\n"
        f"\t\t\texpressionSource: {q(expression_source)}\n\n"
    )

def tmdl_annotation_value(name, value, indent="\t\t"):
    """Render an `annotation <name> = <value>` line. TMDL reads annotation values
    verbatim to end-of-line, so the formula text is preserved literally (quotes,
    brackets and braces are fine unquoted). Internal line breaks / whitespace runs
    are collapsed to single spaces so the value always stays on one physical line --
    guaranteed-valid TMDL. Translated measures are single-line and round-trip
    byte-for-byte; only multi-line fallback formulas (inert stubs) are normalized."""
    v = " ".join((value or "").split())
    return f"{indent}annotation {name} = {v}\n"

def generate_measure_tmdl(field_name, formula, dax=None):
    """One measure for the _Measures table. When `dax` is provided the measure carries
    the translated DAX expression; otherwise it stays an inert `= 0` stub. EITHER WAY
    the original Tableau formula is ALWAYS preserved as a TableauFormula annotation --
    the unconditional audit/repair safety net for any mistranslation."""
    expr = dax if dax else "0"
    out = (
        f"\n\tmeasure {q(field_name)} = {expr}\n"
        f"\t\tlineageTag: {uuid.uuid4()}\n"
    )
    out += tmdl_annotation_value("TableauFormula", formula)
    if dax:
        out += tmdl_annotation_value("TranslatedBy", "Play4 deterministic translator")
    out += "\t\tannotation SummarizationSetBy = Automatic\n"
    return out

def generate_measures_table_tmdl(measures_tmdl):
    # Canonical measures-holder: a single-row calculated table with one hidden column.
    # The calculated partition (NOT a DirectLake entity) is what made the prior model
    # valid -- measure stubs need a home table that doesn't require a Delta binding.
    column = (
        "\n\tcolumn Value\n"
        "\t\tdataType: string\n"
        "\t\tisHidden\n"
        f"\t\tlineageTag: {uuid.uuid4()}\n"
        "\t\tsummarizeBy: none\n"
        "\t\tsourceColumn: [Value]\n"
        "\t\ttype: calculatedTableColumn\n"
    )
    partition = (
        "\tpartition _Measures = calculated\n"
        "\t\tmode: import\n"
        '\t\tsource = Row("Value", BLANK())\n'
    )
    return (
        f"table _Measures\n"
        f"\tlineageTag: {uuid.uuid4()}\n"
        f"{column}"
        f"{measures_tmdl}\n"
        f"{partition}\n"
        f"\tannotation PBI_Id = _Measures\n\n"
    )

def generate_expressions_tmdl(expression_name, directlake_url):
    return (
        f"expression {q(expression_name)} =\n"
        f"\t\tlet\n"
        f'\t\t    Source = AzureStorage.DataLake("{directlake_url}", [HierarchicalNavigation=true])\n'
        f"\t\tin\n"
        f"\t\t    Source\n"
        f"\tlineageTag: {uuid.uuid4()}\n\n"
        f"\tannotation PBI_IncludeFutureArtifacts = False\n\n"
    )

def generate_model_tmdl(table_names, expression_source_name, role_names=None):
    refs = "\n".join([f"ref table {q(t)}" for t in table_names])
    if role_names:
        refs += "\n" + "\n".join(f"ref role {q(r)}" for r in role_names)
    return (
        f"model Model\n"
        f"\tculture: en-US\n"
        f"\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        f"\tsourceQueryCulture: en-US\n"
        f"\tdataAccessOptions\n"
        f"\t\tlegacyRedirects\n"
        f"\t\treturnErrorValuesAsNull\n\n"
        f'annotation PBI_QueryOrder = ["{expression_source_name}"]\n\n'
        f"annotation __PBI_TimeIntelligenceEnabled = 0\n\n"
        f'annotation PBI_ProTooling = ["DirectLakeOnOneLakeInWeb","WebModelingEdit"]\n\n'
        f"{refs}\n"
    )

def generate_database_tmdl():
    return "database\n\tcompatibilityLevel: 1604\n"

# -- RELATIONSHIP INFERENCE ----------------------------------------------------
# Tableau encodes cross-table joins as HIDDEN, disambiguated key fields named
# "<Base> (<Table>)" (e.g. "Region (People)", "Order ID (Returns)"). The matching
# base field "<Base>" lives in the partner table. We pair them, then use the ACTUAL
# landed data to decide which side is unique (the "one" side), so the relationship
# direction (many -> one) is correct regardless of how the join was authored.
_JOINKEY_RE = re.compile(r"^(?P<base>.+) \((?P<tbl>[^()]+)\)$")

def infer_relationships(meta_fields, landed_tables, count_fn):
    """
    meta_fields   : list of dicts with field_name, source_table, field_type, is_hidden
    landed_tables : {table_name: {clean_col: tmdl_type}} actually present in Delta
    count_fn(table_name, clean_col) -> (total, distinct) or None
    Returns list of {from_table, from_col, to_table, to_col, kind}.
    Guards: requires hidden disambiguated key; suffix table must match the key's own
    table; both columns must have landed with COMPATIBLE dtypes; skips self-joins; and
    emits at most ONE relationship per unordered table pair (Fabric allows one active
    path) -- extra candidate keys for an already-linked pair are dropped.
    """
    def _s(v):  # normalize pandas NaN / blanks to None
        if v is None or (isinstance(v, float) and v != v):
            return None
        s = str(v).strip()
        return s or None

    def _truthy(v):
        if v is None or (isinstance(v, float) and v != v):
            return False
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
        return bool(v)

    base_index = {}  # non-disambiguated caption -> set(tables exposing it)
    for f in meta_fields:
        if _s(f.get("field_type") or f.get("__typename")) != "ColumnField":
            continue
        nm, st = _s(f.get("field_name") or f.get("name")), _s(f.get("source_table"))
        if not nm or not st or _JOINKEY_RE.match(nm):
            continue
        base_index.setdefault(nm, set()).add(st)

    candidates = []
    for f in meta_fields:
        if _s(f.get("field_type") or f.get("__typename")) != "ColumnField":
            continue
        nm, owner = _s(f.get("field_name") or f.get("name")), _s(f.get("source_table"))
        if not nm or not owner or not _truthy(f.get("is_hidden")):
            continue  # cross-table join keys are always hidden
        m = _JOINKEY_RE.match(nm)
        if not m:
            continue
        base = m.group("base").strip()
        tbl_suffix = _s(m.group("tbl"))
        if tbl_suffix and tbl_suffix.lower() != owner.lower():
            continue  # the "(<Table>)" suffix names the key's own table
        partners = base_index.get(base, set()) - {owner}
        if len(partners) != 1:
            continue  # ambiguous or no partner -> skip
        partner = next(iter(partners))
        if partner == owner:
            continue  # self-join guard
        owner_cols, partner_cols = landed_tables.get(owner, {}), landed_tables.get(partner, {})
        owner_col, base_col = clean_col(nm), clean_col(base)
        if owner_col not in owner_cols or base_col not in partner_cols:
            continue
        if owner_cols.get(owner_col) != partner_cols.get(base_col):
            continue  # dtype mismatch would fail the model deploy
        oc, pc = count_fn(owner, owner_col), count_fn(partner, base_col)
        owner_unique = bool(oc) and oc[0] > 0 and oc[0] == oc[1]
        partner_unique = bool(pc) and pc[0] > 0 and pc[0] == pc[1]
        if owner_unique and not partner_unique:
            frm, frmc, to, toc, kind = partner, base_col, owner, owner_col, "many_to_one"
        elif partner_unique and not owner_unique:
            frm, frmc, to, toc, kind = owner, owner_col, partner, base_col, "many_to_one"
        elif owner_unique and partner_unique:
            frm, frmc, to, toc, kind = partner, base_col, owner, owner_col, "one_to_one"
        else:
            continue  # neither side unique -> many-to-many, skip (avoid a bad model)
        candidates.append({"from_table": frm, "from_col": frmc, "to_table": to,
                           "to_col": toc, "kind": kind})

    # one active relationship per unordered table pair (first wins); drop extras
    rels, used_pairs, seen = [], set(), set()
    for r in candidates:
        key = (r["from_table"], r["from_col"], r["to_table"], r["to_col"])
        pair = frozenset((r["from_table"], r["to_table"]))
        if key in seen or pair in used_pairs:
            continue
        seen.add(key)
        used_pairs.add(pair)
        rels.append(r)
    return rels

def generate_relationships_tmdl(rels):
    """One TMDL relationship per inferred join. Default cardinality is many-to-one,
    which matches from=many -> to=one, so no explicit cardinality props are required."""
    if not rels:
        return None
    blocks = []
    for r in rels:
        blocks.append("\n".join([
            f"relationship {uuid.uuid4()}",
            f"\tfromColumn: {q(r['from_table'])}.{q(r['from_col'])}",
            f"\ttoColumn: {q(r['to_table'])}.{q(r['to_col'])}",
        ]))
    return "\n\n".join(blocks) + "\n"

def generate_pbism():
    return json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2",
        "settings": {}
    }, indent=2)

def generate_platform(display_name):
    return json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "SemanticModel", "displayName": display_name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"}
    }, indent=2)

def encode(text):
    return base64.b64encode(text.encode('utf-8')).decode('utf-8')


# == MODEL OBJECT ENRICHMENT ===================================================
# Hierarchies, display folders, and row-level-security (RLS) roles are first-class
# Tabular model objects that the core table/column/measure rebuild does not emit.
# This section parses them out of the Tableau ``.tds`` XML, resolves their field
# references against the rebuilt model, and renders the corresponding TMDL:
#
#   * drill paths  -> table ``hierarchy`` blocks (ordered ``level``/``column`` refs)
#   * field folders -> the ``displayFolder`` property on columns and measures
#   * user filters  -> ``role`` blocks with ``tablePermission`` DAX filters
#
# TMDL grammar follows Microsoft's official Tabular Model Definition Language docs.
# Everything here is additive and OPTIONAL: with no model objects present the output
# is byte-for-byte identical to the un-enriched model.

_USER_FUNC_RE = re.compile(
    r"\b(USERNAME|USERDOMAIN|ISMEMBEROF|ISUSERNAME|FULLNAME)\s*\(", re.IGNORECASE)


def _ns_local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter_local(root, name):
    return [e for e in root.iter() if _ns_local(e.tag) == name]


def _children_local(elem, name):
    return [c for c in list(elem) if _ns_local(c.tag) == name]


def _field_token(s):
    """Normalize a Tableau field reference to its bare local token.

    Tableau references a field as ``[Name]`` and frequently QUALIFIES it with one or more
    leading segments (``[connection].[Name]``, ``[Orders].[Category]``) in real ``.tds``
    documents. The trailing bracketed segment is the field's local name, so this returns the
    inner text of the LAST bracketed segment -- which also leaves a simple ``[Name]`` or a
    bare ``Name`` untouched. Applying it uniformly to drill-path levels, folder items, calc
    column names, and filter columns keeps wiring consistent across qualified/unqualified
    forms.
    """
    s = (s or "").strip()
    if not s:
        return ""
    segments = re.findall(r"\[([^\]]*)\]", s)
    if segments:
        return segments[-1].strip()
    return s


def parse_model_objects(tds_text):
    """Parse hierarchies, display folders, and user filters out of a Tableau ``.tds``.

    Returns a credential-free dict of RAW (caption/internal-name) structures::

        {
          "hierarchies":    [{"name": str, "levels": [field_token, ...]}],
          "display_folders": {field_token: folder_name},
          "field_index":     {internal_name: caption},   # for calc/internal-name lookups
          "user_filters":    {"wired": [calc, ...], "unwired": [calc, ...]},
        }

    ``field_token`` is the bracket-stripped Tableau field reference (a database column's
    local name, or a calculation's internal ``Calculation_xxx`` name). The caller resolves
    those tokens to rebuilt model columns/measures via :func:`resolve_model_objects`.
    A ``calc`` is ``{"internal", "name", "formula"}``; it is ``wired`` when a datasource
    ``<filter>`` references it (an enforced row filter) and ``unwired`` otherwise.
    """
    empty = {"hierarchies": [], "display_folders": {}, "field_index": {},
             "user_filters": {"wired": [], "unwired": []}}
    try:
        root = ET.fromstring(tds_text)
    except ET.ParseError:
        return empty

    hierarchies = []
    for dp in _iter_local(root, "drill-path"):
        name = dp.get("name")
        levels = [_field_token(f.text) for f in _children_local(dp, "field")
                  if (f.text or "").strip()]
        if name and levels:
            hierarchies.append({"name": name, "levels": levels})

    folders = {}
    for fld in _iter_local(root, "folder"):
        fname = (fld.get("name") or "").strip()
        if not fname:
            continue
        for item in _children_local(fld, "folder-item"):
            member = _field_token(item.get("name"))
            if member:
                folders[member] = fname

    field_index = {}
    user_calcs = []
    for col in _iter_local(root, "column"):
        internal = _field_token(col.get("name"))
        if not internal:
            continue
        field_index.setdefault(internal, col.get("caption") or internal)
        calc = _children_local(col, "calculation")
        if calc:
            formula = calc[0].get("formula") or ""
            if _USER_FUNC_RE.search(formula):
                user_calcs.append({"internal": internal,
                                   "name": col.get("caption") or internal,
                                   "formula": formula})

    wired_cols = {_field_token(f.get("column"))
                  for f in _iter_local(root, "filter") if f.get("column")}
    wired = [c for c in user_calcs if c["internal"] in wired_cols]
    unwired = [c for c in user_calcs if c["internal"] not in wired_cols]

    return {"hierarchies": hierarchies, "display_folders": folders,
            "field_index": field_index,
            "user_filters": {"wired": wired, "unwired": unwired}}


# -- RLS DAX translation -------------------------------------------------------
# Tableau row-level user filters are most commonly a boolean calc of the shape
# ``[Field] = USERNAME()`` wired as a data-source filter. That maps cleanly to a DAX
# table-permission filter ``'Table'[Column] = USERPRINCIPALNAME()``. Anything richer
# (ISMEMBEROF group logic, USERDOMAIN, compound boolean, an unresolvable field) has no
# safe deterministic DAX equivalent and is deliberately NOT guessed -- it becomes a
# fail-closed manual-review scaffold instead (see :func:`resolve_model_objects`).
# A field reference may be qualified (``[connection].[Field]``); the trailing bracketed
# segment is the local field name, so the capture group always lands on that segment.
_UF_FIELD = r"(?:\[[^\]]+\]\.)*\[(?P<f>[^\]]+)\]"
_UF_EQ_LEFT = re.compile(r"^" + _UF_FIELD + r"\s*=\s*USERNAME\s*\(\s*\)$", re.IGNORECASE)
_UF_EQ_RIGHT = re.compile(r"^USERNAME\s*\(\s*\)\s*=\s*" + _UF_FIELD + r"$", re.IGNORECASE)
_FIELD_REF_RE = re.compile(r"\[([^\]]+)\]")


def _dax_table_ref(name):
    """A DAX table reference: single-quoted, embedded single quotes doubled."""
    return "'" + str(name).replace("'", "''") + "'"


def _dax_column_ref(name):
    """A DAX column reference: bracketed, embedded closing brackets doubled."""
    return "[" + str(name).replace("]", "]]") + "]"


def translate_user_filter_to_dax(formula, resolve_field):
    """Translate a Tableau user-filter formula to a DAX table-permission expression.

    Returns ``(dax | None, table | None, reason)``. Only the safe ``[Field] = USERNAME()``
    equality (either operand order) is translated; everything else returns ``None`` with a
    human-readable reason so the caller emits a manual-review scaffold rather than a guess.
    """
    norm = " ".join((formula or "").split())
    m = _UF_EQ_LEFT.match(norm) or _UF_EQ_RIGHT.match(norm)
    if not m:
        return None, None, "unsupported user-filter expression (no safe DAX equivalent)"
    caption = m.group("f")
    resolved = resolve_field(caption)
    if not resolved:
        return None, None, f"could not unambiguously resolve field [{caption}]"
    table, col = resolved[0], resolved[1]
    dax = f"{_dax_table_ref(table)}{_dax_column_ref(col)} = USERPRINCIPALNAME()"
    return dax, table, "translated"


def _tables_from_formula(formula, resolve_field):
    """Distinct rebuilt tables referenced by ``[Field]`` tokens in a formula (ordered)."""
    out = []
    for caption in _FIELD_REF_RE.findall(formula or ""):
        resolved = resolve_field(caption)
        if resolved and resolved[0] not in out:
            out.append(resolved[0])
    return out


# -- field-token resolution ----------------------------------------------------
def _resolve_member(token, resolve_field, field_index):
    """Resolve a Tableau field token to a rebuilt ``(table, column)`` or ``None``.

    Tries the token directly (a database column's local name resolves as-is), then its
    caption via ``field_index`` (calculations are referenced by an internal name whose
    caption is the user-facing field name).
    """
    resolved = resolve_field(token)
    if resolved:
        return resolved[0], resolved[1]
    caption = field_index.get(token)
    if caption and caption != token:
        resolved = resolve_field(caption)
        if resolved:
            return resolved[0], resolved[1]
    return None


def _unique(name, used, fallback="Object"):
    base = name or fallback
    final, i = base, 2
    while final in used:
        final, i = f"{base} {i}", i + 1
    used.add(final)
    return final


def _append_hierarchy(bucket, name, levels):
    """Append a hierarchy to a table bucket, de-duplicating hierarchy and level names."""
    used_names = {h["name"] for h in bucket}
    final = _unique(name, set(used_names), "Hierarchy")
    seen, out = set(), []
    for level_name, col in levels:
        out.append((_unique(level_name, seen, "Level"), col))
    bucket.append({"name": final, "levels": out})


def _build_role(user_filter, resolve_field, data_tables, used_names):
    """Build a role descriptor for one wired user filter (translated or manual-review).

    A translatable filter yields a single ``tablePermission`` on the referenced table.
    Anything else fails CLOSED: ``FALSE()`` on every emitted data table (never an
    unrestricted, annotation-only role), annotated with the original Tableau formula and a
    ``RequiresManualReview`` flag so the intent is preserved and obvious, never dropped.
    """
    name = _unique(user_filter["name"], used_names, "Role")
    formula = user_filter["formula"]
    dax, table, reason = translate_user_filter_to_dax(formula, resolve_field)
    if dax and table:
        return {
            "name": name,
            "table_permissions": [(table, dax)],
            "annotations": [
                ("TableauUserFilter", formula),
                ("TableauIdentityFunction",
                 "USERNAME() mapped to USERPRINCIPALNAME(); verify the column holds the UPN"),
            ],
            "requires_manual_review": False,
            "reason": "translated",
        }
    fail_closed_tables = list(data_tables) or _tables_from_formula(formula, resolve_field)
    if not fail_closed_tables:
        # A manual-review role with no table permissions reads as UNRESTRICTED in TMDL,
        # which would defeat the fail-closed guarantee. Refuse rather than emit a role that
        # silently grants full access; the caller must supply the emitted data-table set.
        raise ValueError(
            f"cannot emit fail-closed RLS role '{name}': no data tables are known to "
            f"restrict (pass the emitted data-table list via data_tables); refusing to "
            f"emit an unrestricted role for untranslatable filter: {formula}"
        )
    return {
        "name": name,
        "table_permissions": [(t, "FALSE()") for t in fail_closed_tables],
        "annotations": [
            ("TableauUserFilter", formula),
            ("RequiresManualReview", "true"),
            ("ManualReviewReason", reason),
        ],
        "requires_manual_review": True,
        "reason": reason,
    }


def resolve_model_objects(parsed, resolve_field, *, calcs=None, data_tables=None):
    """Resolve RAW parsed model objects against the rebuilt model.

    ``parsed`` is the output of :func:`parse_model_objects`; ``resolve_field`` is the
    descriptor field resolver (caption -> ``(table, column, type)``); ``calcs`` are the
    measures being emitted (so calc fields can land in display folders); ``data_tables``
    are the emitted data-table display names (the fail-closed target set for RLS).

    Returns RESOLVED structures ready for emission plus an audit ``report``::

        {
          "display_folders": {table: {member_name: folder}},
          "hierarchies":     {table: [{"name", "levels": [(level_name, column)]}]},
          "roles":           [role_descriptor, ...],
          "report": {"display_folders": {...}, "hierarchies": {...}, "rls": {...}},
        }
    """
    field_index = parsed.get("field_index") or {}
    measure_names = {c.get("name") for c in (calcs or []) if c.get("name")}
    data_tables = list(data_tables or [])

    resolved_folders = {}
    folder_report = {"resolved": [], "unresolved": []}
    for member, folder in (parsed.get("display_folders") or {}).items():
        target = _resolve_member(member, resolve_field, field_index)
        if target:
            resolved_folders.setdefault(target[0], {})[target[1]] = folder
            folder_report["resolved"].append(member)
            continue
        caption = field_index.get(member, member)
        measure = caption if caption in measure_names else (
            member if member in measure_names else None)
        if measure is not None:
            resolved_folders.setdefault("_Measures", {})[measure] = folder
            folder_report["resolved"].append(member)
        else:
            folder_report["unresolved"].append(member)

    resolved_hier = {}
    hier_report = {"emitted": [], "skipped": []}
    for h in (parsed.get("hierarchies") or []):
        levels, tables, ok = [], set(), True
        for token in h["levels"]:
            target = _resolve_member(token, resolve_field, field_index)
            if not target:
                ok = False
                break
            tables.add(target[0])
            levels.append((field_index.get(token, token), target[1]))
        if ok and len(tables) == 1 and levels:
            _append_hierarchy(resolved_hier.setdefault(next(iter(tables)), []),
                              h["name"], levels)
            hier_report["emitted"].append(h["name"])
        else:
            reason = ("level resolves to more than one table" if len(tables) > 1
                      else "no resolvable levels" if not levels
                      else "a level could not be resolved to a model column")
            hier_report["skipped"].append({"name": h["name"], "reason": reason})

    roles = []
    used_role_names = set()
    rls_report = {"translated": [], "manual_review": [],
                  "unwired": [c["name"]
                              for c in (parsed.get("user_filters") or {}).get("unwired", [])]}
    for uf in (parsed.get("user_filters") or {}).get("wired", []):
        role = _build_role(uf, resolve_field, data_tables, used_role_names)
        roles.append(role)
        if role["requires_manual_review"]:
            rls_report["manual_review"].append({"name": role["name"], "reason": role["reason"]})
        else:
            rls_report["translated"].append(role["name"])

    return {
        "display_folders": resolved_folders,
        "hierarchies": resolved_hier,
        "roles": roles,
        "report": {"display_folders": folder_report,
                   "hierarchies": hier_report,
                   "rls": rls_report},
    }


# -- TMDL emission for model objects -------------------------------------------
def _quote_text_value(value):
    """A TMDL text property value, always double-quoted with embedded quotes doubled.

    Always quoting is valid for every text value and side-steps the leading/trailing
    whitespace and special-character rules entirely (TMDL strips the wrapping quotes).
    """
    return '"' + str(value).replace('"', '""') + '"'


def _read_identifier(text):
    """Read the leading TMDL identifier from ``text`` (a single-quoted name or bare token)."""
    text = text.lstrip()
    if not text:
        return None
    if text[0] == "'":
        i, buf = 1, []
        while i < len(text):
            ch = text[i]
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                return "".join(buf)
            buf.append(ch)
            i += 1
        return "".join(buf)
    token = []
    for ch in text:
        if ch.isspace():
            break
        token.append(ch)
    return "".join(token) or None


def _decl_name(line, keyword):
    """Return the declared object name if ``line`` is a ``<keyword> <name>`` declaration."""
    prefix = "\t" + keyword + " "
    if not line.startswith(prefix):
        return None
    return _read_identifier(line[len(prefix):])


def generate_hierarchy_tmdl(name, levels):
    """Render one TMDL ``hierarchy`` block (a table child object).

    ``levels`` is an ordered list of ``(level_name, column_name)``; the emitted
    indentation matches the table-child style used by the column/measure generators.
    Returns an empty string when there are no levels (a hierarchy needs at least one).
    """
    if not levels:
        return ""
    out = [f"\thierarchy {q(name)}", f"\t\tlineageTag: {uuid.uuid4()}"]
    for level_name, column_name in levels:
        out.append("")
        out.append(f"\t\tlevel {q(level_name)}")
        out.append(f"\t\t\tlineageTag: {uuid.uuid4()}")
        out.append(f"\t\t\tcolumn: {q(column_name)}")
    return "\n" + "\n".join(out) + "\n"


def generate_role_tmdl(role):
    """Render one TMDL ``role`` block (a model-level object written to its own file).

    ``role`` is a descriptor from :func:`resolve_model_objects`: a ``name``, a list of
    ``(table, dax_filter)`` table permissions, and ``(name, value)`` annotations (the
    original Tableau formula and, for manual-review scaffolds, the review flag).
    """
    lines = [f"role {q(role['name'])}",
             "\tmodelPermission: read",
             f"\tlineageTag: {uuid.uuid4()}"]
    for table, expr in role.get("table_permissions") or []:
        lines.append("")
        lines.append(f"\ttablePermission {q(table)} = {expr}")
    for ann_name, ann_value in role.get("annotations") or []:
        lines.append("")
        lines.append(f"\tannotation {ann_name} = {' '.join(str(ann_value).split())}")
    return "\n".join(lines) + "\n"


def _inject_display_folders(table_tmdl, folders):
    """Add a ``displayFolder`` property to each matching column/measure declaration."""
    out = []
    for line in table_tmdl.split("\n"):
        out.append(line)
        name = _decl_name(line, "column")
        if name is None:
            name = _decl_name(line, "measure")
        if name is not None and name in folders:
            out.append(f"\t\tdisplayFolder: {_quote_text_value(folders[name])}")
    return "\n".join(out)


def _inject_hierarchies(table_tmdl, hierarchies):
    """Insert hierarchy blocks just before the table's first ``partition`` declaration."""
    block = "".join(generate_hierarchy_tmdl(h["name"], h["levels"]) for h in hierarchies)
    if not block:
        return table_tmdl
    idx = table_tmdl.find("\tpartition ")
    if idx == -1:
        return table_tmdl + block
    line_start = table_tmdl.rfind("\n", 0, idx) + 1
    return table_tmdl[:line_start] + block + table_tmdl[line_start:]


def enrich_table_tmdl(table_tmdl, *, display_folders=None, hierarchies=None):
    """Enrich an already-rendered ``table`` TMDL string with model objects.

    ``display_folders`` is ``{member_name: folder}`` for columns/measures in this table;
    ``hierarchies`` is a list of ``{"name", "levels": [(level_name, column)]}``. Both are
    optional -- with neither supplied the string is returned unchanged, so callers can
    enrich unconditionally without altering un-enriched output.
    """
    if display_folders:
        table_tmdl = _inject_display_folders(table_tmdl, display_folders)
    if hierarchies:
        table_tmdl = _inject_hierarchies(table_tmdl, hierarchies)
    return table_tmdl
