"""Tableau parameter -> Power BI translation.

A Tableau *parameter* is a single-value control the user sets at runtime (`[Parameters].[X]`
in calcs). Power BI has no native scalar parameter object, so the faithful equivalent is the
community-standard **disconnected table + DAX measure** pattern (the same pattern Power BI
Desktop's own "What-if parameter" generates): a one-column calculated table the user filters
with a slicer, and a ``SELECTEDVALUE`` measure that reads the current selection (with the
Tableau default as the fallback). The tables are intentionally **disconnected** (never wired
into ``relationships.tmdl``) so they do not relate the data tables to each other.

This module handles the **value** parameters (a param used as a scalar inside a calc, e.g.
``[Sales] * (1 - [Parameters].[Churn Rate])`` or ``... = [Parameters].[New Quota]``):

* ``parse_parameters(xml)`` -> a list of parameter descriptors read from a workbook/datasource
  ``Parameters`` pseudo-datasource (every ``<column>`` carrying ``param-domain-type``).
* ``emit_value_parameters(params, *, existing_tables, existing_measures, calcs)`` -> the TMDL
  parts (one calculated table + its value measure per param), the new table/measure names, a
  ``param_resolver`` the calc translator consults to turn ``[Parameters].[X]`` into the value
  measure reference, and any migration warnings (e.g. a synthesized max for an open-ended
  Tableau range). Only the params actually referenced by ``calcs`` are emitted, keeping each
  model lean.

The value measure is named ``"<Param> Value"`` (never just ``"<Param>"``) so it never collides
with the same-named column in its own table -- Power BI requires a measure name to differ from
every column in the table that hosts it, and to be unique across the whole model.
"""
from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET

try:
    from .tmdl_generate import q
except ImportError:  # flat-module import (scripts dir on sys.path)
    from tmdl_generate import q

# Tableau datatype -> (TMDL column dataType, the calc translator's static dtype vocab).
_TYPE_MAP = {
    "integer": ("int64", "number"),
    "real": ("double", "number"),
    "string": ("string", "text"),
    "boolean": ("boolean", "bool"),
    "date": ("dateTime", "date"),
    "datetime": ("dateTime", "date"),
}

_ROW_CAP = 10000  # guardrail: never emit a GENERATESERIES table wider than this many rows


def _localname(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _num(value):
    """Format a numeric literal cleanly for DAX (strip float noise, keep ints int-looking)."""
    f = float(value)
    if f == int(f):
        return str(int(f))
    # %.10g drops Tableau's float artefacts: 0.10000000000000001 -> 0.1, 18.3999999.. -> 18.4
    return "%.10g" % f


def _dax_string(value):
    return '"' + str(value).replace('"', '""') + '"'


def _unescape_member(raw):
    """A Tableau member/value string: strip the surrounding quotes and any backslash escapes."""
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return re.sub(r"\\(.)", r"\1", s)


def parse_parameters(xml):
    """Parse Tableau parameters from a workbook (or ``Parameters`` datasource) XML string.

    Returns a list of dicts: ``{caption, internal_name, datatype, domain (range|list),
    default, range:{min,max,step}|None, members:[...], aliases:{key:label}}``. ``internal_name``
    is the raw ``name`` attribute WITH brackets (e.g. ``"[Parameter 2]"``) -- both it and the
    caption are accepted by the resolver so a calc can reference the param either way.
    """
    root = ET.fromstring((xml or "").lstrip("\ufeff"))
    out, seen = [], set()
    for col in root.iter():
        if _localname(col.tag) != "column" or col.get("param-domain-type") is None:
            continue
        name = col.get("name")
        if name in seen:
            continue
        seen.add(name)

        rng, members, aliases = None, [], {}
        for ch in col:
            t = _localname(ch.tag)
            if t == "range":
                rng = {
                    "min": ch.get("min"),
                    "max": ch.get("max"),
                    "step": ch.get("granularity"),
                }
            elif t == "members":
                members = [_unescape_member(m.get("value"))
                           for m in ch if _localname(m.tag) == "member" and m.get("value") is not None]
            elif t == "aliases":
                for a in ch:
                    if _localname(a.tag) == "alias":
                        aliases[a.get("key")] = a.get("value")

        out.append({
            "caption": col.get("caption") or name,
            "internal_name": name,
            "datatype": (col.get("datatype") or "string").lower(),
            "domain": col.get("param-domain-type"),
            "default": col.get("value"),
            "range": rng,
            "members": members,
            "aliases": aliases,
        })
    return out


def _param_keys(param):
    """The lookup keys a calc may use to reference this param: caption + bracket-less name."""
    keys = set()
    cap = (param.get("caption") or "").strip().lower()
    if cap:
        keys.add(cap)
    raw = (param.get("internal_name") or "").strip()
    keys.add(raw.lower())
    keys.add(raw.strip("[]").strip().lower())
    return keys


def referenced_parameters(params, calcs):
    """The subset of ``params`` that any calc formula references via ``[Parameters].[...]``."""
    formulas = " \n ".join((c.get("formula") or "") for c in (calcs or []))
    refs = {m.strip().lower()
            for m in re.findall(r"\[Parameters\]\.\[([^\]]+)\]", formulas)}
    out = []
    for p in params:
        cap = (p.get("caption") or "").strip().lower()
        raw = (p.get("internal_name") or "").strip()
        if cap in refs or raw.strip("[]").strip().lower() in refs or raw.lower() in refs:
            out.append(p)
    return out


def _default_number(param):
    d = param.get("default")
    try:
        return float(d)
    except (TypeError, ValueError):
        return None


def _format_string(param, tmdl_type):
    """A best-effort Power BI formatString. Percent ONLY when the Tableau format starts with
    'p' (a true 0..1 fraction); a number that merely *displays* a % suffix (e.g. 18.4) stays
    plain so it never renders as 1840%."""
    fmt = (param.get("format") or "").strip().lower()
    if fmt.startswith("p"):
        return "0.00%" if "00" in fmt else "0%"
    if tmdl_type == "int64":
        return "0"
    if tmdl_type in ("double", "decimal"):
        return "0.00"
    return None


def _synth_range(param):
    """Resolve (min, max, step, default, warnings) for a numeric range param.

    Tableau open-ended ranges (no max) get a synthesized max = max(default*4, min + step*20).
    The Tableau step is preserved; if the row count would exceed the cap the MAX is lowered
    (never the step) so granularity stays faithful. Returns floats + a list of warnings.
    """
    rng = param.get("range") or {}
    warnings = []
    minv = float(rng["min"]) if rng.get("min") not in (None, "") else 0.0
    step = float(rng["step"]) if rng.get("step") not in (None, "") else 1.0
    if step <= 0:
        step = 1.0
    default = _default_number(param)
    if default is None:
        default = minv

    if rng.get("max") not in (None, ""):
        maxv = float(rng["max"])
    else:
        synth = max(default * 4, minv + step * 20)
        # Snap up to a whole number of steps above min, and ensure the default is included.
        steps = max(1, round((synth - minv) / step))
        maxv = minv + step * steps
        if maxv < default:
            maxv = minv + step * (int((default - minv) / step) + 1)
        warnings.append(
            f"parameter '{param.get('caption')}' had an open-ended Tableau range; "
            f"synthesized max={_num(maxv)} (min={_num(minv)}, step={_num(step)})")

    rows = int((maxv - minv) / step) + 1
    if rows > _ROW_CAP:
        maxv = minv + step * (_ROW_CAP - 1)
        warnings.append(
            f"parameter '{param.get('caption')}' range exceeded {_ROW_CAP} rows; "
            f"capped max={_num(maxv)} (step preserved)")
    return minv, maxv, step, default, warnings


def _uniquify(name, used_lower):
    final, i = name, 2
    while final.lower() in used_lower:
        final, i = f"{name} {i}", i + 1
    used_lower.add(final.lower())
    return final


def _value_table_tmdl(table_name, column_name, tmdl_type, fmt, source_expr,
                      measure_name, default_literal, measure_fmt):
    """One disconnected what-if table: a value column, a SELECTEDVALUE measure, a calculated
    partition (GENERATESERIES / DATATABLE), modelled on the existing _Measures/Date tables."""
    col = [f"\tcolumn {q(column_name)}", f"\t\tdataType: {tmdl_type}"]
    if fmt:
        col.append(f"\t\tformatString: {fmt}")
    col += [
        f"\t\tlineageTag: {uuid.uuid4()}",
        "\t\tsummarizeBy: none",
        f"\t\tsourceColumn: [{column_name}]",
        "",
        "\t\tannotation SummarizationSetBy = Automatic",
    ]
    col_block = "\n" + "\n".join(col) + "\n"

    col_ref = f"{q(table_name)}[{column_name}]"
    measure = [f"\n\tmeasure {q(measure_name)} = SELECTEDVALUE({col_ref}, {default_literal})"]
    if measure_fmt:
        measure.append(f"\t\tformatString: {measure_fmt}")
    measure.append(f"\t\tlineageTag: {uuid.uuid4()}")
    measure.append("\t\tannotation SummarizationSetBy = Automatic")
    measure_block = "\n".join(measure) + "\n"

    partition = (
        f"\tpartition {q(table_name)} = calculated\n"
        f"\t\tmode: import\n"
        f"\t\tsource = {source_expr}\n"
    )
    return (
        f"table {q(table_name)}\n"
        f"\tlineageTag: {uuid.uuid4()}\n"
        f"{col_block}"
        f"{measure_block}\n"
        f"{partition}"
        f"\n\tannotation PBI_Id = {q(table_name)}\n"
    )


def _emit_one_value_param(param, table_name, measure_name):
    """Build the TMDL for one value param. Returns (tmdl_text, dtype) or (None, None) if the
    param can't be represented as a value control."""
    datatype = param.get("datatype", "string")
    tmdl_type, dtype = _TYPE_MAP.get(datatype, ("string", "text"))

    if datatype in ("integer", "real"):
        minv, maxv, step, default, _warn = _synth_range(param)
        source = f"GENERATESERIES({_num(minv)}, {_num(maxv)}, {_num(step)})"
        fmt = _format_string(param, tmdl_type)
        default_literal = _num(default)
        # GENERATESERIES emits its column literally named "Value".
        return _value_table_tmdl(table_name, "Value" if False else param["caption"],
                                 tmdl_type, fmt, source, measure_name,
                                 default_literal, fmt), dtype

    if datatype == "string":
        members = param.get("members") or []
        if not members:
            return None, None
        rows = ", ".join("{" + _dax_string(m) + "}" for m in members)
        col = param["caption"]
        source = f'DATATABLE("{col}", STRING, {{{rows}}})'
        default_literal = _dax_string(_unescape_member(param.get("default")))
        return _value_table_tmdl(table_name, col, "string", None, source,
                                 measure_name, default_literal, None), dtype

    return None, None


# =============================================================================
# FIELD PARAMETERS  (Tableau dimension/measure *swap* calcs -> Power BI field parameter)
# =============================================================================
#
# When a Tableau calc is `CASE/IF [Parameters].[X] WHEN <lit> THEN [bareFieldA]
# WHEN <lit> THEN [bareFieldB] ... END` -- i.e. the parameter chooses *which field*
# to show -- the faithful Power BI construct is a **field parameter**: a 3-column
# calculated table (Display / Fields / Order) whose Fields column carries
# `extendedProperty ParameterMetadata = {"version":3,"kind":2}` and whose partition
# is a list of `("Display", NAMEOF('Table'[Field]), order)` tuples. The user picks a
# value with a slicer and every visual that uses the field-parameter column swaps the
# underlying field. The table is named after the **calc** it replaces (the user drops
# the calc on the shelf in Tableau), NOT the parameter. Field-parameter tables go in
# model.tmdl's table list but are NEVER wired into relationships.tmdl.

_FIELD_ONLY = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_NUMERIC_LITERAL = re.compile(r"^-?\d+(?:\.\d+)?$")
_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def dax_ref(table, field, *, measure=False):
    """A DAX column/measure reference with DAX (not TMDL) escaping.

    DAX escapes a table name's `'` by doubling it and a column/measure name's `]` by
    doubling it -- this is DIFFERENT from TMDL identifier quoting (``q``), so a dedicated
    helper is required for the ``NAMEOF`` targets inside a field-parameter partition.
    A measure reference is model-global and carries no table qualifier.
    """
    fld = (field or "").replace("]", "]]")
    if measure:
        return f"[{fld}]"
    tbl = (table or "").replace("'", "''")
    return f"'{tbl}'[{fld}]"


def _is_numeric_literal(s):
    return bool(_NUMERIC_LITERAL.match((s or "").strip()))


def _safe_filename(name):
    """A filesystem-safe ``.tmdl`` part filename for a field-parameter table whose model
    name may contain `` / \\ : * ? " < > | `` (calc captions are user-authored)."""
    base = _INVALID_FS.sub("_", name or "").strip().rstrip(".") or "field_parameter"
    return base + ".tmdl"


def detect_field_swap(formula, *, role="measure"):
    """Recognise a Tableau field-*swap* calc and return its structure, else ``None``.

    Returns ``{controller, branches:[{label, field, is_else?}], role, form}`` only when the
    formula is a clean ``[Parameters].[X]``-driven ``CASE`` or ``IF`` whose every branch is a
    BARE field reference (no arithmetic/extra tokens) and there are >= 2 branches. Keywords are
    case-insensitive and a glued ``]END`` is tolerated. Anything else (arithmetic, nested calls,
    a non-parameter controller) returns ``None`` so it falls through to normal calc translation.
    """
    if not formula or not formula.strip():
        return None
    f = formula.strip()
    low = f.lower()
    if low.startswith("case"):
        return _detect_case_swap(f, role)
    if low.startswith("if"):
        return _detect_if_swap(f, role)
    return None


def _detect_case_swap(f, role):
    head = re.match(r"(?is)^case\s*\[Parameters\]\.\[([^\]]+)\]\s*(.*)$", f)
    if not head:
        return None
    controller = head.group(1).strip()
    body = re.sub(r"(?is)\bend\b\s*$", "", head.group(2)).strip()
    chunks = re.split(r"(?is)\bwhen\b", body)
    if chunks and chunks[0].strip():
        return None  # content before the first WHEN -> not a clean swap
    clauses = chunks[1:]
    if len(clauses) < 2:
        return None
    branches, else_field = [], None
    for i, clause in enumerate(clauses):
        parts = re.split(r"(?is)\belse\b", clause)
        m = re.match(r"(?is)^\s*(.+?)\s*\bthen\b\s*\[([^\]]+)\]\s*$", parts[0])
        if not m:
            return None
        branches.append({"label": _unescape_member(m.group(1).strip()), "field": m.group(2).strip()})
        if len(parts) > 1:
            if i != len(clauses) - 1 or len(parts) > 2:
                return None
            em = _FIELD_ONLY.match(parts[1])
            if not em:
                return None
            else_field = em.group(1).strip()
    if else_field:
        branches.append({"label": None, "field": else_field, "is_else": True})
    if len(branches) < 2:
        return None
    return {"controller": controller, "branches": branches,
            "role": (role or "measure").lower(), "form": "case"}


def _detect_if_swap(f, role):
    body = re.sub(r"(?is)\bend\b\s*$", "", f).strip()
    body = re.sub(r"(?is)^if\b\s*", "", body)
    segs = re.split(r"(?is)\belseif\b", body)
    branches, else_field, controllers = [], None, []
    for i, seg in enumerate(segs):
        parts = re.split(r"(?is)\belse\b", seg)
        m = re.match(
            r"(?is)^\s*\[Parameters\]\.\[([^\]]+)\]\s*=\s*(.+?)\s*\bthen\b\s*\[([^\]]+)\]\s*$",
            parts[0])
        if not m:
            return None
        controllers.append(m.group(1).strip())
        branches.append({"label": _unescape_member(m.group(2).strip()), "field": m.group(3).strip()})
        if len(parts) > 1:
            if i != len(segs) - 1 or len(parts) > 2:
                return None
            em = _FIELD_ONLY.match(parts[1])
            if not em:
                return None
            else_field = em.group(1).strip()
    if else_field:
        branches.append({"label": None, "field": else_field, "is_else": True})
    if len({c.lower() for c in controllers}) != 1 or len(branches) < 2:
        return None
    return {"controller": controllers[0], "branches": branches,
            "role": (role or "measure").lower(), "form": "if"}


def _uniquify_label(label, used_labels, owner, warnings):
    base, i, final = label, 2, label
    while final.lower() in used_labels:
        final, i = f"{base} ({i})", i + 1
    if final != label:
        warnings.append(
            f"field-swap '{owner}': duplicate option label '{label}' renamed to '{final}'")
    used_labels.add(final.lower())
    return final


def _field_param_table_tmdl(table_name, entries):
    """Render the canonical 3-column field-parameter table. ``entries`` is a list of
    ``(display_label, dax_ref_string, order_int)``; the Fields column carries the
    ``ParameterMetadata`` extended property that marks the table as a field parameter."""
    fields_col = f"{table_name} Fields"
    order_col = f"{table_name} Order"
    tq, fq, oq = q(table_name), q(fields_col), q(order_col)

    display = (
        f"\n\tcolumn {tq}\n"
        f"\t\tdataType: string\n"
        f"\t\tlineageTag: {uuid.uuid4()}\n"
        f"\t\tsummarizeBy: none\n"
        f"\t\tsourceColumn: [Value1]\n"
        f"\t\tsortByColumn: {oq}\n"
        f"\t\trelatedColumnDetails\n"
        f"\t\t\tgroupByColumn: {fq}\n"
        f"\n\t\tannotation SummarizationSetBy = Automatic\n"
    )
    fields = (
        f"\n\tcolumn {fq}\n"
        f"\t\tdataType: string\n"
        f"\t\tisHidden\n"
        f"\t\tlineageTag: {uuid.uuid4()}\n"
        f"\t\tsummarizeBy: none\n"
        f"\t\tsourceColumn: [Value2]\n"
        f"\t\tsortByColumn: {oq}\n"
        f"\t\textendedProperty ParameterMetadata =\n"
        f"\t\t\t\t{{\n"
        f'\t\t\t\t  "version": 3,\n'
        f'\t\t\t\t  "kind": 2\n'
        f"\t\t\t\t}}\n"
        f"\n\t\tannotation SummarizationSetBy = Automatic\n"
    )
    order = (
        f"\n\tcolumn {oq}\n"
        f"\t\tdataType: int64\n"
        f"\t\tisHidden\n"
        f"\t\tformatString: 0\n"
        f"\t\tlineageTag: {uuid.uuid4()}\n"
        f"\t\tsummarizeBy: sum\n"
        f"\t\tsourceColumn: [Value3]\n"
        f"\n\t\tannotation SummarizationSetBy = Automatic\n"
    )
    rows = ",\n".join(
        f"\t\t\t\t({_dax_string(label)}, NAMEOF({ref}), {order_i})"
        for (label, ref, order_i) in entries)
    partition = (
        f"\tpartition {tq} = calculated\n"
        f"\t\tmode: import\n"
        f"\t\tsource =\n"
        f"\t\t\t\t{{\n"
        f"{rows}\n"
        f"\t\t\t\t}}\n"
    )
    return (
        f"table {tq}\n"
        f"\tlineageTag: {uuid.uuid4()}\n"
        f"{display}"
        f"{fields}"
        f"{order}"
        f"\n{partition}"
        f"\n\tannotation PBI_Id = {uuid.uuid4().hex}\n"
    )


def emit_field_parameter(display_name, swap, *, field_locator, used_names, label_aliases=None):
    """Build a Power BI field-parameter table for one Tableau swap calc.

    ``field_locator(field) -> (table, column, is_measure) | None`` resolves a bare Tableau field
    ref to its landed model home. Branches whose field does not resolve are dropped (fail-closed);
    if fewer than 2 survive the swap is NOT converted (``ok=False``) and the caller leaves the calc
    for normal translation (which stubs it). Display labels are de-duplicated. Returns a dict:
    ``{ok, table_name, part_filename, tmdl, warnings}``.
    """
    warnings = []
    branches = swap.get("branches") or []
    role = swap.get("role", "measure")
    label_aliases = label_aliases or {}

    table_name = _uniquify(display_name, used_names)
    entries, used_labels = [], set()
    for br in branches:
        field = br.get("field")
        loc = field_locator(field) if field_locator else None
        if not loc:
            warnings.append(
                f"field-swap '{display_name}': branch field [{field}] did not resolve to a model "
                f"column; branch dropped")
            continue
        table, col, is_measure = loc
        ref = dax_ref(table, col, measure=bool(is_measure))
        raw = br.get("label")
        if br.get("is_else") or raw is None:
            label = col
        elif raw in label_aliases:
            label = label_aliases[raw]
        elif _is_numeric_literal(raw):
            label = col  # numeric measure-swap selector -> use the field's own name
        else:
            label = raw
        label = _uniquify_label(label, used_labels, display_name, warnings)
        entries.append((label, ref, len(entries)))

    if len(entries) < 2:
        used_names.discard(table_name.lower())
        warnings.append(
            f"field-swap '{display_name}': fewer than 2 branches resolved; not converted to a "
            f"field parameter (left for normal translation)")
        return {"ok": False, "table_name": None, "part_filename": None, "tmdl": None,
                "warnings": warnings}

    if role == "measure":
        warnings.append(
            f"field-swap '{display_name}': measure swap uses each field's default column "
            f"aggregation (typically SUM); verify non-additive measures (AVG/COUNTD/ratios)")

    return {"ok": True, "table_name": table_name,
            "part_filename": _safe_filename(table_name),
            "tmdl": _field_param_table_tmdl(table_name, entries), "warnings": warnings}


def emit_field_parameters(calcs, *, field_locator, used_names=None, existing_tables=None,
                          label_aliases_by_controller=None):
    """Detect every field-swap calc in ``calcs`` and emit a field-parameter table per swap.

    Returns ``{parts:[(filename, tmdl)], table_names:[...], consumed:set(names), warnings:[...]}``.
    ``consumed`` is the set of calc names that became field-parameter tables -- the caller must NOT
    also translate them as measures/columns. A non-swap calc that references a consumed swap calc
    cannot use it as a scalar; that dependency is reported as a warning (the dependent will stub).
    ``used_names`` (a shared lowercased set) keeps table names unique across the whole model.
    """
    used = used_names if used_names is not None else set()
    if used_names is None and existing_tables:
        for t in existing_tables:
            used.add((t or "").lower())
    label_aliases_by_controller = label_aliases_by_controller or {}

    swaps, swap_names_lower = [], set()
    for c in (calcs or []):
        name = c.get("name") or c.get("caption")
        sw = detect_field_swap(c.get("formula") or "", role=(c.get("role") or "measure"))
        if sw and name:
            swaps.append((name, sw))
            swap_names_lower.add(name.lower())

    warnings = []
    for c in (calcs or []):
        name = c.get("name") or c.get("caption") or ""
        if name.lower() in swap_names_lower:
            continue
        refs = {r.strip().lower() for r in re.findall(r"\[([^\]]+)\]", c.get("formula") or "")}
        hit = swap_names_lower & refs
        if hit:
            warnings.append(
                f"calc '{name}' references field-swap calc(s) {sorted(hit)} which become report-only "
                f"field parameters; '{name}' cannot reference them as a scalar and will stub (=0)")

    parts, table_names, consumed, used_files = [], [], set(), set()
    for name, sw in swaps:
        aliases = label_aliases_by_controller.get((sw.get("controller") or "").lower(), {})
        res = emit_field_parameter(name, sw, field_locator=field_locator, used_names=used,
                                   label_aliases=aliases)
        warnings.extend(res.get("warnings") or [])
        if not res.get("ok"):
            continue
        fn = res["part_filename"]
        base, ext = (fn[:-5], ".tmdl") if fn.endswith(".tmdl") else (fn, "")
        final, i = fn, 2
        while final.lower() in used_files:
            final, i = f"{base}_{i}{ext}", i + 1
        used_files.add(final.lower())
        parts.append((final, res["tmdl"]))
        table_names.append(res["table_name"])
        consumed.add(name)
    return {"parts": parts, "table_names": table_names, "consumed": consumed, "warnings": warnings}


def extract_field_swap_calcs(xml):
    """Pull *swap* calculated fields (ANY role) out of workbook/datasource XML, role-tagged.

    Returns ``[{name, formula, role}]`` for calcs whose formula is a ``[Parameters]``-driven
    CASE/IF field swap -- crucially INCLUDING dimension-role calcs, which ``extract_calculations``
    drops as "non-measure". Tolerant of a leading BOM and XML namespaces.
    """
    out, seen = [], set()
    try:
        root = ET.fromstring((xml or "").lstrip("\ufeff"))
    except ET.ParseError:
        return out
    for col in (e for e in root.iter() if _localname(e.tag) == "column"):
        if col.get("param-domain-type") is not None:
            continue  # a Tableau parameter, not a swap calc
        calc_el = next((c for c in list(col) if _localname(c.tag) == "calculation"), None)
        if calc_el is None:
            continue
        formula = calc_el.get("formula") or ""
        if not formula.strip():
            continue
        name = col.get("caption") or (col.get("name") or "").strip("[]")
        if not name or name in seen:
            continue
        role = (col.get("role") or "measure").lower()
        if detect_field_swap(formula, role=role):
            seen.add(name)
            out.append({"name": name, "formula": formula, "role": role})
    return out
