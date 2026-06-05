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

def generate_model_tmdl(table_names, expression_source_name):
    refs = "\n".join([f"ref table {q(t)}" for t in table_names])
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
