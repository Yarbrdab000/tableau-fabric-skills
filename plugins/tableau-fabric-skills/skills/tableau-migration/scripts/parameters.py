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


def _dec(value):
    """Like ``_num`` but ALWAYS decimal-typed (``20`` -> ``20.0``). A double what-if series must
    stay decimal so a ``UNION`` with an off-grid decimal default can't coerce an integer series and
    truncate the default (e.g. 19.47 -> 19)."""
    f = float(value)
    if f == int(f):
        return f"{int(f)}.0"
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
            "format": col.get("default-format") or col.get("format"),
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


def _value_table_tmdl(table_name, *, display_col, source_col, tmdl_type, fmt, source_expr,
                      measure_name, default_literal, measure_fmt, label_col=None,
                      label_source=None):
    """One disconnected what-if table: a value column, a SELECTEDVALUE measure, a calculated
    partition (GENERATESERIES / DATATABLE), modelled on the existing _Measures/Date tables.

    The column's *display* name (``display_col``) may differ from the *physical* partition column
    it binds to (``source_col``): GENERATESERIES always names its column ``Value`` while the model
    column is named after the parameter, so ``sourceColumn`` must point at the physical name or the
    table will not load. The SELECTEDVALUE measure reads the column by its display (model) name.

    When ``label_col`` is given (a discrete LIST param whose members carry display aliases) a second
    string column bound to ``label_source`` is emitted so a slicer can show the friendly labels while
    the value measure still reads the underlying value column.
    """
    def _column_block(name, col_type, col_fmt, src):
        lines = [f"\tcolumn {q(name)}", f"\t\tdataType: {col_type}"]
        if col_fmt:
            lines.append(f"\t\tformatString: {col_fmt}")
        lines += [
            f"\t\tlineageTag: {uuid.uuid4()}",
            "\t\tsummarizeBy: none",
            f"\t\tsourceColumn: [{src}]",
            "",
            "\t\tannotation SummarizationSetBy = Automatic",
        ]
        return "\n".join(lines)

    blocks = [_column_block(display_col, tmdl_type, fmt, source_col)]
    if label_col:
        blocks.append(_column_block(label_col, "string", None, label_source))
    col_block = "\n" + "\n\n".join(blocks) + "\n"

    col_ref = dax_ref(table_name, display_col)
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
        f"\n\tannotation PBI_Id = {uuid.uuid4().hex}\n"
    )


def _on_grid(value, minv, maxv, step):
    """Whether ``value`` lands exactly on the GENERATESERIES grid ``min..max`` stepped by ``step``
    (within float tolerance). An off-grid Tableau default (e.g. 19.47 on a 1..100 step-1 series)
    must be unioned into the series or it cannot be selected."""
    try:
        v, lo, hi, st = float(value), float(minv), float(maxv), float(step)
    except (TypeError, ValueError):
        return True  # no usable default -> nothing to union in
    if st == 0:
        return True
    if v < lo - 1e-9 or v > hi + 1e-9:
        return False
    k = round((v - lo) / st)
    return abs(lo + k * st - v) <= 1e-9


def _emit_numeric_list_value_param(param, table_name, value_col, measure_name, tmdl_type, dtype):
    """A discrete numeric (integer/real) LIST parameter -> a DATATABLE of its EXACT members, never
    a GENERATESERIES range (a range would offer every step in between, which is wrong for a picker
    of named choices). When the members carry display aliases (e.g. ``15.`` -> "Current Orders") a
    second string ``<value_col> Label`` column is emitted so a slicer shows the friendly names while
    the value measure still reads the underlying number. Returns the value-param 4-tuple
    ``(tmdl, dtype, warnings, picker_column)`` where ``picker_column`` is the Label column when
    aliases are present, else the value column itself."""
    members = param.get("members") or []
    aliases = {_canon_num_key(k): v for k, v in (param.get("aliases") or {}).items()}
    nfmt = _dec if tmdl_type == "double" else _num
    dax_type = "DOUBLE" if tmdl_type == "double" else "INTEGER"
    fmt = _format_string(param, tmdl_type)

    default_raw = param.get("default")
    try:
        default_literal = nfmt(default_raw)
    except (TypeError, ValueError):
        default_literal = nfmt(members[0])

    labels = [aliases.get(_canon_num_key(m)) for m in members]
    if any(lbl is not None for lbl in labels):
        rows = ", ".join(
            "{" + nfmt(m) + ", " + _dax_string(lbl if lbl is not None else _num(m)) + "}"
            for m, lbl in zip(members, labels))
        source = f'DATATABLE("Value", {dax_type}, "Label", STRING, {{{rows}}})'
        label_col = value_col + " Label"
        tmdl = _value_table_tmdl(
            table_name, display_col=value_col, source_col="Value", tmdl_type=tmdl_type,
            fmt=fmt, source_expr=source, measure_name=measure_name,
            default_literal=default_literal, measure_fmt=fmt,
            label_col=label_col, label_source="Label")
        return tmdl, dtype, [], label_col

    rows = ", ".join("{" + nfmt(m) + "}" for m in members)
    source = f'DATATABLE("Value", {dax_type}, {{{rows}}})'
    tmdl = _value_table_tmdl(
        table_name, display_col=value_col, source_col="Value", tmdl_type=tmdl_type,
        fmt=fmt, source_expr=source, measure_name=measure_name,
        default_literal=default_literal, measure_fmt=fmt)
    return tmdl, dtype, [], value_col


_DATE_DEFAULT_RE = re.compile(
    r"^#(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2}):(\d{1,2}))?#$")


def _date_default_literal(param):
    """Parse a Tableau date/datetime default (``#2020-01-01#`` or ``#2020-01-01 13:30:00#``) into a
    DAX ``DATE(y, m, d)`` literal (plus ``+ TIME(h, mi, s)`` when a non-midnight time part is
    present). Returns ``None`` when the default is missing or is not a Tableau date literal, so the
    caller fails closed rather than inventing a non-deterministic date anchor."""
    raw = (param.get("default") or "").strip()
    m = _DATE_DEFAULT_RE.match(raw)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    lit = f"DATE({y}, {mo}, {d})"
    if m.group(4) is not None:
        hh, mi, ss = int(m.group(4)), int(m.group(5)), int(m.group(6))
        if (hh, mi, ss) != (0, 0, 0):
            lit = f"{lit} + TIME({hh}, {mi}, {ss})"
    return lit


def _emit_one_value_param(param, table_name, column_name, measure_name):
    """Build the TMDL for one value param. Returns ``(tmdl_text, dtype, warnings, picker_column)``
    or ``(None, None, [], None)`` if the param can't be represented as a value control.
    ``picker_column`` is the table column a slicer should bind to (the friendly Label column for an
    aliased numeric list, else the value column)."""
    datatype = param.get("datatype", "string")
    tmdl_type, dtype = _TYPE_MAP.get(datatype, ("string", "text"))

    if datatype in ("integer", "real"):
        # A discrete LIST parameter enumerates explicit members -> a DATATABLE of exactly those
        # values. Only a true 'range' parameter (a slider) keeps the GENERATESERIES path below.
        if param.get("domain") == "list" and (param.get("members") or []):
            return _emit_numeric_list_value_param(
                param, table_name, column_name, measure_name, tmdl_type, dtype)
        minv, maxv, step, default, warnings = _synth_range(param)
        # A double series stays decimal-typed so the UNION below can't coerce/truncate the default.
        nfmt = _dec if tmdl_type == "double" else _num
        series = f"GENERATESERIES({nfmt(minv)}, {nfmt(maxv)}, {nfmt(step)})"
        # Keep an off-grid Tableau default selectable by unioning it into the series.
        if not _on_grid(default, minv, maxv, step):
            series = f'DISTINCT(UNION({series}, ROW("Value", {nfmt(default)})))'
        fmt = _format_string(param, tmdl_type)
        default_literal = nfmt(default)
        # GENERATESERIES emits its column literally named "Value".
        tmdl = _value_table_tmdl(
            table_name, display_col=column_name, source_col="Value", tmdl_type=tmdl_type,
            fmt=fmt, source_expr=series, measure_name=measure_name,
            default_literal=default_literal, measure_fmt=fmt)
        return tmdl, dtype, warnings, column_name

    if datatype == "string":
        members = param.get("members") or []
        if not members:
            return None, None, [], None
        rows = ", ".join("{" + _dax_string(m) + "}" for m in members)
        # DATATABLE names its column "Value"; the model column carries the param's display name.
        source = f'DATATABLE("Value", STRING, {{{rows}}})'
        default_literal = _dax_string(_unescape_member(param.get("default")))
        tmdl = _value_table_tmdl(
            table_name, display_col=column_name, source_col="Value", tmdl_type="string",
            fmt=None, source_expr=source, measure_name=measure_name,
            default_literal=default_literal, measure_fmt=None)
        return tmdl, dtype, [], column_name

    if datatype in ("date", "datetime"):
        # A Tableau date parameter has no member list -- it is a free date picker. Model it as a
        # DISCONNECTED single-column date table spanning the model's own date range (CALENDARAUTO,
        # which auto-derives min..max from every date column in the model) so a slicer offers the
        # real data domain, plus a SELECTEDVALUE capture measure that falls back to the Tableau
        # default date. The table stays disconnected (value-param tables are never wired into
        # relationships), so a downstream calc reads the picked date as a VALUE via the measure --
        # without filtering through a relationship, the invariant a date-range filter needs to
        # compute (e.g.) a prior period outside the selected range. CALENDARAUTO names its column
        # "Date". Fail closed on a default that is not a Tableau date literal (no guessed anchor).
        default_literal = _date_default_literal(param)
        if default_literal is None:
            return None, None, [], None
        tmdl = _value_table_tmdl(
            table_name, display_col=column_name, source_col="Date", tmdl_type="dateTime",
            fmt=None, source_expr="CALENDARAUTO()", measure_name=measure_name,
            default_literal=default_literal, measure_fmt=None)
        return tmdl, dtype, [], column_name

    return None, None, [], None
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
# A swap branch's compared value must be a lone literal (quoted string or number); a
# compound/typed condition (``... AND ...``, another comparison) is NOT a clean swap.
_SIMPLE_MEMBER = re.compile(r"""^\s*("[^"]*"|'[^']*'|-?\d+(?:\.\d+)?)\s*$""")


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


def _canon_num_key(s):
    """Canonical key for matching a swap branch literal to a parameter alias key. Numeric values
    compare by value so a branch ``1`` matches an alias keyed ``1.`` or ``1.0`` (Tableau commonly
    serializes integer alias keys with a trailing dot); everything else compares case-insensitively.
    Uses ``float`` directly so trailing-dot forms like ``"1."`` (which a strict regex rejects) still
    canonicalize."""
    t = (s or "").strip()
    try:
        f = float(t)
    except (TypeError, ValueError):
        return t.lower()
    return str(int(f)) if f == int(f) else ("%.10g" % f)


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


def _parse_if_condition(cond):
    """Pull ``(controller, member_value)`` from a single IF-branch condition that compares
    ``[Parameters].[X]`` to a simple literal, accepting EITHER operand order
    (``[Parameters].[X] = "A"`` or ``"A" = [Parameters].[X]``). Returns ``None`` for
    compound/typed conditions (``AND``/``OR``, a non-literal value, a field-to-field
    comparison) so non-swap IFs fall through to normal translation."""
    c = cond.strip()
    m = re.match(r"(?is)^\[Parameters\]\.\[([^\]]+)\]\s*=\s*(.+)$", c)
    if m and _SIMPLE_MEMBER.match(m.group(2)):
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"(?is)^(.+?)\s*=\s*\[Parameters\]\.\[([^\]]+)\]$", c)
    if m and _SIMPLE_MEMBER.match(m.group(1)):
        return m.group(2).strip(), m.group(1).strip()
    return None


def _detect_if_swap(f, role):
    # Tableau accepts both one-word ``ELSEIF`` and two-word ``ELSE IF`` (a nested IF, each
    # closed by its own END). Normalise ``ELSE IF`` -> ``ELSEIF`` and strip the resulting run
    # of trailing ENDs so both spellings parse identically.
    norm = re.sub(r"(?is)\belse\s+if\b", "ELSEIF", f)
    body = re.sub(r"(?is)(?:\s*\bend\b)+\s*$", "", norm).strip()
    body = re.sub(r"(?is)^if\b\s*", "", body)
    segs = re.split(r"(?is)\belseif\b", body)
    branches, else_field, controllers = [], None, []
    for i, seg in enumerate(segs):
        parts = re.split(r"(?is)\belse\b", seg)
        m = re.match(r"(?is)^\s*(.+?)\s*\bthen\b\s*\[([^\]]+)\]\s*$", parts[0])
        if not m:
            return None
        cond = _parse_if_condition(m.group(1))
        if not cond:
            return None
        controllers.append(cond[0])
        branches.append({"label": _unescape_member(cond[1]), "field": m.group(2).strip()})
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


class MeasureSynthesizer:
    """Allocates aggregating measures for measure-swap candidates that resolve to raw columns.

    A field parameter can only ``NAMEOF`` a column or a measure, and a ``NAMEOF``'d *raw column* is
    consumed as a GROUP-BY (row-level detail) in the visual -- never aggregated -- so a measure-role
    swap pointed at a base column explodes the table to row grain instead of collapsing to the
    selected dimension. This allocates one default-``SUM`` measure per distinct ``(table, column)``
    (named ``Total <column>``) so every measure-swap option aggregates; identical candidates shared
    across several swaps reuse one measure. Collected ``definitions`` are emitted into ``_Measures``.
    """

    def __init__(self, measures_table="_Measures", *, reserved_names=None, agg="SUM"):
        self.measures_table = measures_table
        self.agg = agg
        self._used = reserved_names if reserved_names is not None else set()
        self._cache = {}
        self.definitions = []

    def aggregate(self, table, column):
        """Return ``(measures_table, measure_name)`` for an aggregating measure over
        ``table[column]`` -- creating and recording it on first request, reusing it after."""
        key = ((table or "").lower(), (column or "").lower())
        name = self._cache.get(key)
        if name is None:
            name = _uniquify(f"Total {column}", self._used)
            self._cache[key] = name
            self.definitions.append({
                "name": name,
                "dax": f"{self.agg}({dax_ref(table, column)})",
                "agg": self.agg,
                "source_table": table,
                "source_column": column,
                "tableau_formula": f"{self.agg}([{column}])",
            })
        return (self.measures_table, name)


def emit_field_parameter(display_name, swap, *, field_locator, used_names, label_aliases=None,
                         measure_synth=None):
    """Build a Power BI field-parameter table for one Tableau swap calc.

    ``field_locator(field) -> (table, column, is_measure) | None`` resolves a bare Tableau field
    ref to its landed model home. Branches whose field does not resolve are dropped (fail-closed);
    if fewer than 2 survive the swap is NOT converted (``ok=False``) and the caller leaves the calc
    for normal translation (which stubs it). Display labels are de-duplicated.

    For a MEASURE-role swap, a candidate that resolves to a raw column is aggregated through a
    synthesized default-``SUM`` measure (via ``measure_synth``) so the swap collapses the visual to
    the selected grain instead of listing row-level values. Returns a dict
    ``{ok, table_name, part_filename, tmdl, role, display_col, entries, measures, warnings}`` where
    ``measures`` are the aggregating measures this swap created (to emit into ``_Measures``).
    """
    warnings = []
    branches = swap.get("branches") or []
    role = swap.get("role", "measure")
    label_aliases = label_aliases or {}

    table_name = _uniquify(display_name, used_names)
    norm_aliases = {_canon_num_key(k): v for k, v in label_aliases.items()}

    # phase 1 -- resolve each branch field to its model home (drop unresolved, fail-closed)
    resolved, used_labels = [], set()
    for br in branches:
        field = br.get("field")
        loc = field_locator(field) if field_locator else None
        if not loc:
            warnings.append(
                f"field-swap '{display_name}': branch field [{field}] did not resolve to a model "
                f"column; branch dropped")
            continue
        table, col, is_measure = loc
        raw = br.get("label")
        if br.get("is_else") or raw is None:
            label = col
        elif _canon_num_key(raw) in norm_aliases:
            label = norm_aliases[_canon_num_key(raw)]
        elif _is_numeric_literal(raw):
            label = col  # numeric measure-swap selector -> use the field's own name
        else:
            label = raw
        label = _uniquify_label(label, used_labels, display_name, warnings)
        resolved.append((label, table, col, bool(is_measure)))

    if len(resolved) < 2:
        used_names.discard(table_name.lower())
        warnings.append(
            f"field-swap '{display_name}': fewer than 2 branches resolved; not converted to a "
            f"field parameter (left for normal translation)")
        return {"ok": False, "table_name": None, "part_filename": None, "tmdl": None,
                "role": role, "display_col": None, "entries": [], "measures": [],
                "warnings": warnings}

    # phase 2 -- build the NAMEOF refs; for a measure swap, point each raw-column candidate at a
    # synthesized aggregating measure (only now that the swap is confirmed convertible).
    synth = measure_synth if measure_synth is not None else MeasureSynthesizer()
    before = len(synth.definitions)
    entries, struct_entries = [], []
    for order_i, (label, table, col, is_measure) in enumerate(resolved):
        if role == "measure" and not is_measure:
            e_table, e_col = synth.aggregate(table, col)
            e_is_measure = True
        else:
            e_table, e_col, e_is_measure = table, col, is_measure
        entries.append((label, dax_ref(e_table, e_col, measure=e_is_measure), order_i))
        struct_entries.append({"label": label, "table": e_table, "column": e_col,
                               "is_measure": e_is_measure, "order": order_i})
    measures_created = synth.definitions[before:]

    if role == "measure":
        warnings.append(
            f"field-swap '{display_name}': measure swap aggregates each field with a synthesized "
            f"SUM measure; verify non-additive measures (AVG/COUNTD/ratios)")

    # ``display_col`` == ``table_name``: the canonical field-parameter table names its visible
    # display column after the table itself (see ``_field_param_table_tmdl``); the report-side
    # ``fieldParameters`` expansion binds each slot to '<table_name>'[<table_name>].
    return {"ok": True, "table_name": table_name,
            "part_filename": _safe_filename(table_name),
            "role": role, "display_col": table_name, "entries": struct_entries,
            "measures": measures_created,
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

    # synthesized aggregating measures share one allocator across all swaps so a candidate offered by
    # several measure swaps (e.g. Sales) reuses one ``Total Sales`` measure. Reserve against model
    # table names and the non-swap calc names (which become their own measures) to avoid clashes.
    synth_reserved = set(used)
    for c in (calcs or []):
        nm = (c.get("name") or c.get("caption") or "").strip().lower()
        if nm and nm not in swap_names_lower:
            synth_reserved.add(nm)
    synth = MeasureSynthesizer(reserved_names=synth_reserved)

    parts, table_names, consumed, used_files, specs = [], [], set(), set(), []
    for name, sw in swaps:
        aliases = label_aliases_by_controller.get((sw.get("controller") or "").lower(), {})
        res = emit_field_parameter(name, sw, field_locator=field_locator, used_names=used,
                                   label_aliases=aliases, measure_synth=synth)
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
        # report-side spec: lets a visual EXPAND this parameter (seed projection + fieldParameters
        # block). Kept in detection order so the self-service table's slot order is stable.
        specs.append({"calc_name": name, "table_name": res["table_name"],
                      "display_col": res["display_col"], "role": res.get("role"),
                      "controller": sw.get("controller"),
                      "entries": res.get("entries") or []})
    return {"parts": parts, "table_names": table_names, "consumed": consumed,
            "specs": specs, "measures": synth.definitions, "warnings": warnings}


# =============================================================================
# VALUE PARAMETERS  (Tableau scalar param used inside a calc -> what-if table + measure)
# =============================================================================

def _uniquify_reserved(name, reserved_lower, *, prefer_suffix=None):
    """Pick a model-unique name against a shared lowercased ``reserved_lower`` set spanning every
    table/column/measure already in the model (Tabular forbids a measure name equal to ANY column
    name anywhere). Tries the bare ``name`` first; if it is taken and ``prefer_suffix`` is given,
    tries ``name + prefer_suffix`` next; then appends incrementing integers. The chosen name is
    added to ``reserved_lower``.
    """
    candidates = [name] + ([name + prefer_suffix] if prefer_suffix else [])
    for cand in candidates:
        if cand.lower() not in reserved_lower:
            reserved_lower.add(cand.lower())
            return cand
    base, final, i = candidates[-1], candidates[-1], 2
    while final.lower() in reserved_lower:
        final, i = f"{base} {i}", i + 1
    reserved_lower.add(final.lower())
    return final


def emit_value_parameters(params, *, calcs, reserved_names=None):
    """Emit a disconnected what-if table + value measure for every scalar parameter that any
    ``calcs`` formula references via ``[Parameters].[X]``.

    Returns ``{parts:[(filename, tmdl)], table_names:[...], measure_names:[...], param_resolver,
    consumed_params:[...], warnings:[...]}``. ``param_resolver(name)`` maps a parameter reference
    (by caption or bracket-less internal name) to a ``(measure_ref, dtype)`` tuple -- the value
    measure ``[<Param> Value]`` plus the param's comparison dtype (the translator's canonical
    ``number``/``text``/``bool``/``date`` vocabulary) so a calc that compares the param against a
    typed column type-checks. The calc translator inlines the selection and deliberately registers
    NO table in ``tables_used`` so the
    host expression stays single-table (e.g. ``SUM('Orders'[Sales]) * [Sales Multiplier Value]`` does
    not trip the cross-table fallback). ``consumed_params`` lists the SOURCE parameters that became
    what-if tables (``{caption, internal_name, table, measure, picker_column}``) so the model manifest
    can tag them kind="value" (model-owned); ``picker_column`` is the table column a slicer should bind
    to (the friendly Label column for an aliased numeric list, else the value column).
    ``reserved_names`` is a shared lowercased set of every existing
    table/column/measure name so emitted names never collide; the caller seeds it with the data
    table + its columns + the translated measure names BEFORE calling this.

    Pass only the NON-consumed calcs: a param referenced solely by a consumed field-swap (a swap
    *controller*) is correctly excluded, while a param behind a deferred/stubbed calc still gets its
    slicer table so the report can keep the control.
    """
    reserved = reserved_names if reserved_names is not None else set()
    wanted = referenced_parameters(params, calcs)

    parts, table_names, measure_names, warnings = [], [], [], []
    resolver_map, used_files, consumed_params = {}, set(), []
    for p in wanted:
        caption = p.get("caption") or p.get("internal_name") or "Parameter"
        datatype = p.get("datatype", "string")
        # Reserve a globally-unique measure and a table whose display column shares its name.
        measure_name = _uniquify_reserved(caption + " Value", reserved)
        table_name = _uniquify_reserved(caption, reserved, prefer_suffix=" Parameter")
        column_name = table_name
        tmdl, _dtype, warn, picker_column = _emit_one_value_param(
            p, table_name, column_name, measure_name)
        if tmdl is None:
            reserved.discard(measure_name.lower())
            reserved.discard(table_name.lower())
            warnings.append(
                f"parameter '{caption}' (datatype {datatype}) is not a representable value "
                f"control (no range/members); left unmodelled and its calcs will stub (=0)")
            continue
        warnings.extend(warn)
        fn = _safe_filename(table_name)
        base, ext = (fn[:-5], ".tmdl") if fn.endswith(".tmdl") else (fn, "")
        final, i = fn, 2
        while final.lower() in used_files:
            final, i = f"{base}_{i}{ext}", i + 1
        used_files.add(final.lower())
        parts.append((final, tmdl))
        table_names.append(table_name)
        measure_names.append(measure_name)
        # Record the SOURCE parameter that this what-if table consumed, so the model manifest can
        # tag it kind="value" (model-owned) and the report/viz layer never re-emits it as a slicer.
        consumed_params.append({"caption": p.get("caption"),
                                "internal_name": p.get("internal_name"),
                                "table": table_name, "measure": measure_name,
                                "picker_column": picker_column})
        ref = dax_ref(None, measure_name, measure=True)
        # Register the param's COMPARISON dtype alongside its measure ref. ``_dtype`` is already the
        # translator's canonical vocab (number/text/bool/date -- see _TYPE_MAP), so a calc that
        # compares the param against a typed column -- e.g. an inlined date-window filter comparing a
        # date column to a date parameter -- type-checks instead of failing "incomparable types".
        # The translator's tolerant reader accepts either this (ref, dtype) tuple or a bare string.
        for k in _param_keys(p):
            resolver_map[k] = (ref, _dtype)

    def param_resolver(name):
        return resolver_map.get((name or "").strip().lower())

    return {"parts": parts, "table_names": table_names, "measure_names": measure_names,
            "param_resolver": param_resolver, "consumed_params": consumed_params,
            "warnings": warnings}


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


def field_locator_from_resolver(resolve, *, measure_names=None):
    """Adapt a model field resolver into the ``field_locator`` ``emit_field_parameters`` expects.

    ``resolve(caption) -> (table, clean_col, tmdl_type) | None`` is the orchestrator's M field
    resolver (``connection_to_m.build_m_field_resolver``). The returned
    ``locate(field) -> (table, column, is_measure) | None`` binds a bare swap-branch field to its
    landed home so a ``NAMEOF`` target can be emitted:

    * a field whose name matches a known model **measure** (``measure_names``) resolves to that
      measure (model-global, ``is_measure=True``), preserving an explicit aggregation; otherwise
    * it binds to its base **column** (``is_measure=False``). Dropping a base column into a visual
      aggregates it by its ``summarizeBy`` (typically ``SUM``), matching Tableau's drop-and-aggregate
      for a bare measure-swap field. ``emit_field_parameter`` already warns about non-additive cases.

    A field that does not resolve returns ``None`` (the branch is dropped fail-closed).
    """
    by_name = {(m or "").strip().lower(): m for m in (measure_names or [])}

    def locate(field):
        key = (field or "").strip().lower()
        actual = by_name.get(key)
        if actual is not None:
            return (None, actual, True)
        hit = resolve(field) if resolve else None
        if not hit:
            return None
        return (hit[0], hit[1], False)

    return locate
