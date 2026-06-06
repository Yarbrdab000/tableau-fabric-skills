"""Tableau parameters -> Power BI / Fabric semantic-model translation (pure, offline).

Tableau "parameters" are single-value controls (a list of allowed values, a numeric/date
range, or unbounded free input) that a user picks and that calcs / filters / axes read.
Power BI has no 1:1 equivalent, so the faithful rebuild is a well-known idiom: a
**disconnected table** of candidate values + **DAX** that reads the user's selection. There is
NEVER a relationship between the parameter table and the fact table -- the selection is applied
explicitly (SELECTEDVALUE / TREATAS / RANKX), which is exactly how Tableau parameters behave.

This module is the single owner of that translation. It is deliberately pure (XML in, TMDL/DAX
strings out) so it is trivially unit-testable with inline fixtures and no network/secrets.

Three capability tiers (all disconnected-table + DAX):

* **Tier 1 -- value parameter** (read inside a measure): a candidate table
  (``DATATABLE`` list with an ordinal Sort-By column / ``GENERATESERIES`` numeric range /
  ``CALENDAR`` date range) plus a single-select-safe *value measure*
  ``X Value = IF(HASONEVALUE('X'[X]), SELECTEDVALUE('X'[X]), <default>)``.
* **Tier 2 -- dimension-swap / dependent**: a ``TREATAS`` filter measure that pushes the
  disconnected selection onto a real fact column, and a cascading 1/0 flag measure for dependent
  (parent -> child) parameters. Measure-swap is a ``SWITCH`` over the value measure.
* **Tier 3 -- Top-N**: a disconnected N table + a ``RANKX`` ranking measure + a filter
  measure whose "nothing selected = show all" semantics make Top-N a calculation, not a static
  visual filter.

GUARDRAILS (be LOUD; never silently mistranslate):

* Classify a parameter's *usage* before emitting (``classify_parameter``). A parameterized
  row-level calc must NOT become a static calculated column -- calc columns evaluate at refresh,
  not at slicer-time, so the result would be silently wrong.
* An unbounded (``param-domain-type='all'``) parameter cannot be enumerated: emit a default-only
  constant measure and flag it as a manual step (NOT deploy-ready).
* Emission is storage-mode aware: a DAX calculated table is fine for Import, forces a composite
  model under DirectQuery, and is unsupported in a pure Direct Lake model -- each surfaced as a
  manual note rather than silently emitted.

Public contract (kept stable; other streams bind to it -- see resources/parameters.md):

* ``extract_parameters(xml_text) -> list[ParamSpec]``
* ``classify_parameter(spec, usages, storage_mode=None) -> CapabilityClass``
* ``param_table_tmdl(spec, storage_mode="import") -> str``
* ``param_value_measure(spec) -> (measure_name, dax)``
* ``param_ref_name(spec) -> str``  (the value-measure name; a calc resolver rewrites
  ``[Parameters].[X]`` -> ``[X Value]``)
* Name helpers: ``param_table_name`` / ``param_value_column`` / ``param_slicer_column`` /
  ``param_order_column`` so the slicer / model-emit / orchestrator streams agree on identifiers.
* ``emit_parameter(spec, usages=None, storage_mode="import") -> dict``  (convenience bundle).
"""
from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

try:  # works whether imported as a package or run with scripts/ on sys.path
    from .tmdl_generate import q
except ImportError:  # pragma: no cover - exercised via the standalone test path
    from tmdl_generate import q


# -- data model ----------------------------------------------------------------
@dataclass
class RangeSpec:
    """A numeric/date range domain: ``min``/``max`` are decoded scalars, ``step`` is the decoded
    numeric increment (or ``None``), ``granularity`` is the raw Tableau granularity token."""

    min: object
    max: object
    step: object = None
    granularity: Optional[str] = None


@dataclass
class ParamSpec:
    """One Tableau parameter, decoded from the synthetic ``<datasource name='Parameters'>`` block.

    ``members`` is a list of ``(value, alias)`` where ``value`` is a typed Python scalar (str/int/
    float/bool) and ``alias`` is the display label or ``None`` (meaning "same as value").
    ``usage_class`` caches the capability class once ``classify_parameter`` has run.
    """

    name: Optional[str]                 # Tableau internal name, e.g. "[Parameter 1]"
    caption: Optional[str]              # display caption, e.g. "Facility Name Parameter"
    datatype: str                       # string|integer|real|boolean|date|datetime
    domain_type: str                    # list|range|all
    members: List[Tuple[object, Optional[str]]] = field(default_factory=list)
    range: Optional[RangeSpec] = None
    default: object = None              # decoded current/default scalar
    formula: Optional[str] = None       # nested <calculation> formula, if any
    usage_class: Optional[str] = None   # filled by classify_parameter


@dataclass
class CapabilityClass:
    """Result of ``classify_parameter``: how this parameter can be rebuilt, and how loudly we must
    warn. ``tier`` is 1/2/3 (or ``None`` for a manual-only parameter); ``strategy`` is the emission
    idiom; ``deploy_ready`` is False whenever a human must finish or verify the translation."""

    name: str
    tier: Optional[int]
    strategy: str
    deploy_ready: bool
    warnings: List[str] = field(default_factory=list)


# Capability-class name tokens (stable identifiers other streams may switch on).
CLASS_MEASURE_VALUE = "measure-value"
CLASS_VISUAL_FILTER = "visual-filter"
CLASS_DIMENSION_SWAP = "dimension-swap"
CLASS_MEASURE_SWAP = "measure-swap"
CLASS_TOP_N = "top-n"
CLASS_BIN_SIZE = "bin-size"
CLASS_REFERENCE_LINE = "reference-line"
CLASS_CALC_COLUMN = "calculated-column"
CLASS_MANUAL_UNBOUNDED = "manual-unbounded"

# Emission strategies.
STRAT_VALUE_MEASURE = "value-measure"
STRAT_TREATAS_FILTER = "treatas-filter"
STRAT_SWITCH_MEASURE = "switch-measure"
STRAT_TOPN_FILTER = "topn-filter"
STRAT_DEFAULT_ONLY = "default-only"
STRAT_MANUAL = "manual"

# Usage tokens a caller (workbook-parsing stream) supplies to classify_parameter.
USAGE_MEASURE = "measure"
USAGE_FILTER = "filter"
USAGE_AXIS = "axis"
USAGE_DIMENSION = "dimension"
USAGE_MEASURE_SWAP = "measure_swap"
USAGE_TOP_N = "top_n"
USAGE_BIN = "bin"
USAGE_REFERENCE_LINE = "reference_line"
USAGE_CALC_COLUMN = "calc_column"


# -- datatype / scalar decoding ------------------------------------------------
_DATATYPE_ALIASES = {
    "string": "string", "str": "string", "text": "string",
    "integer": "integer", "int": "integer", "long": "integer",
    "real": "real", "float": "real", "double": "real", "decimal": "real",
    "boolean": "boolean", "bool": "boolean",
    "date": "date",
    "datetime": "datetime", "timestamp": "datetime",
}

# Tableau DATATABLE type token per normalized datatype.
_DATATABLE_TOKEN = {
    "string": "STRING",
    "integer": "INTEGER",
    "real": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATETIME",
    "datetime": "DATETIME",
}

# TMDL column dataType per normalized datatype.
_TMDL_TYPE = {
    "string": "string",
    "integer": "int64",
    "real": "double",
    "boolean": "boolean",
    "date": "dateTime",
    "datetime": "dateTime",
}

# Non-daily date granularity tokens: plain CALENDAR (daily) would over-generate, so we warn.
_NON_DAILY_GRAINS = {"year", "quarter", "month", "week", "hour", "minute", "second"}


def _norm_datatype(raw):
    return _DATATYPE_ALIASES.get((raw or "").strip().lower(), "string")


def _decode_numeric(raw, datatype):
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    try:
        if datatype == "integer":
            return int(float(s))
        return float(s)
    except ValueError:
        return None


def _decode_tableau_scalar(raw, datatype):
    """Decode a raw Tableau attribute value into a typed Python scalar.

    Tableau stores string scalars wrapped in double-quotes inside the attribute
    (``value='"New York State Hospital"'``) and doubles embedded quotes; dates as ``#2020-01-01#``;
    booleans as ``true``/``false``. XML entity-decoding has already happened by the time ET hands
    us the attribute, so we only undo Tableau's own wrapping here.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if datatype == "boolean":
        return s.lower() in ("true", "1", "yes")
    if datatype == "integer":
        v = _decode_numeric(s, "integer")
        return v if v is not None else s
    if datatype == "real":
        v = _decode_numeric(s, "real")
        return v if v is not None else s
    if datatype in ("date", "datetime"):
        return s.strip("#").strip()
    # string
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s.replace('""', '"')


def _decode_alias(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s.replace('""', '"')


# -- DAX literal encoders ------------------------------------------------------
def _num(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v)


_DATE_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$"
)


def _dax_date_literal(value, datatype):
    s = str(value).strip().strip("#").strip()
    m = _DATE_RE.match(s)
    if not m:
        esc = s.replace('"', '""')
        return 'DATEVALUE("' + esc + '")'
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if m.group(4) is not None and datatype == "datetime":
        h, mi, se = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
        return f"(DATE({y}, {mo}, {d}) + TIME({h}, {mi}, {se}))"
    return f"DATE({y}, {mo}, {d})"


def _dax_literal(value, datatype):
    """A typed DAX scalar literal (also used verbatim for DATATABLE cells)."""
    if datatype == "boolean":
        truthy = value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes")
        return "TRUE()" if truthy else "FALSE()"
    if datatype == "integer":
        return str(int(value))
    if datatype == "real":
        return _num(value)
    if datatype in ("date", "datetime"):
        return _dax_date_literal(value, datatype)
    esc = str(value).replace('"', '""')
    return '"' + esc + '"'


def _brk(name):
    """Escape a name for use inside a DAX/TMDL ``[...]`` column reference."""
    return str(name).replace("]", "]]")


def _display(value, datatype):
    if datatype == "boolean":
        return "true" if (value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes")) else "false"
    return str(value)


# -- name helpers (public; streams bind to these) ------------------------------
def _base_name(spec):
    if spec.caption and spec.caption.strip():
        return spec.caption.strip()
    n = (spec.name or "").strip()
    if n.startswith("[") and n.endswith("]"):
        n = n[1:-1]
    return n or "Parameter"


def param_table_name(spec):
    """Display name of the disconnected parameter table (= the parameter caption)."""
    return _base_name(spec)


def param_ref_name(spec):
    """The value-measure name (e.g. ``Facility Name Parameter Value``). A calc resolver rewrites
    ``[Parameters].[<caption>]`` -> ``[<this>]``."""
    return _base_name(spec) + " Value"


def param_order_column(spec):
    """Name of the hidden ordinal Sort-By column for a list parameter."""
    return _base_name(spec) + " Order"


def param_value_column(spec):
    """Name of the column the *value measure* reads (the real underlying value).

    For a list it is the caption; for a numeric range it is ``GENERATESERIES``'s ``Value`` column;
    for a date range it is ``CALENDAR``'s ``Date`` column.
    """
    if spec.domain_type == "range":
        return "Date" if spec.datatype in ("date", "datetime") else "Value"
    return _base_name(spec)


def param_slicer_column(spec):
    """Name of the column a slicer should bind to (the display column).

    Same as ``param_value_column`` unless the list carries aliases that differ from their values,
    in which case the slicer shows a separate ``<caption> Label`` column.
    """
    if spec.domain_type == "list" and _has_distinct_aliases(spec):
        return _base_name(spec) + " Label"
    return param_value_column(spec)


# -- domain helpers ------------------------------------------------------------
def _has_distinct_aliases(spec):
    for v, a in spec.members:
        if a is not None and str(a) != _display(v, spec.datatype):
            return True
    return False


def _is_enumerable(spec):
    if spec.domain_type == "range" and spec.range is not None:
        return True
    if spec.domain_type == "list" and spec.members:
        return True
    return False


def _values_unique(members):
    seen = set()
    for v, _a in members:
        if v in seen:
            return False
        seen.add(v)
    return True


def _labels_unique(spec):
    seen = set()
    for v, a in spec.members:
        lab = a if a is not None else _display(v, spec.datatype)
        if lab in seen:
            return False
        seen.add(lab)
    return True


def _default_in_members(spec):
    if spec.domain_type != "list" or not spec.members:
        return True  # not applicable
    return any(v == spec.default for v, _a in spec.members)


# -- extraction ----------------------------------------------------------------
def extract_parameters(xml_text):
    """Parse the synthetic ``<datasource name='Parameters'>`` block(s) into ``ParamSpec`` objects.

    Accepts a full ``.twb``/``.tds`` string or just the Parameters datasource. Tolerant of a
    UTF-8 BOM and of malformed XML (returns ``[]`` on a parse error). A parameter column is
    identified by its ``param-domain-type`` attribute.
    """
    if not xml_text:
        return []
    if xml_text[0] == "\ufeff":
        xml_text = xml_text[1:]
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    cols = []
    for ds in root.iter("datasource"):
        ds_name = (ds.get("name") or ds.get("caption") or "")
        if ds_name == "Parameters":
            for col in ds.iter("column"):
                if col.get("param-domain-type") is not None:
                    cols.append(col)
    if not cols:  # fallback: a parameter column anywhere
        for col in root.iter("column"):
            if col.get("param-domain-type") is not None:
                cols.append(col)

    seen_ids = set()
    specs = []
    for col in cols:
        key = id(col)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        specs.append(_parse_param_column(col))
    return specs


def _parse_param_column(col):
    datatype = _norm_datatype(col.get("datatype"))
    domain = (col.get("param-domain-type") or "list").strip().lower()
    default = _decode_tableau_scalar(col.get("value"), datatype)

    calc = col.find("calculation")
    formula = calc.get("formula") if calc is not None else None

    members = []
    for m in col.iter("member"):
        mv = _decode_tableau_scalar(m.get("value"), datatype)
        ma = _decode_alias(m.get("alias"))
        members.append((mv, ma))

    rng = None
    r = col.find("range")
    if r is None:
        r = col.find(".//range")
    if r is not None:
        rmin = _decode_tableau_scalar(r.get("min"), datatype)
        rmax = _decode_tableau_scalar(r.get("max"), datatype)
        step_raw = r.get("granularity")
        if step_raw is None:
            step_raw = r.get("step")
        step = _decode_numeric(step_raw, datatype)
        rng = RangeSpec(min=rmin, max=rmax, step=step, granularity=step_raw)

    return ParamSpec(
        name=col.get("name"),
        caption=col.get("caption"),
        datatype=datatype,
        domain_type=domain,
        members=members,
        range=rng,
        default=default,
        formula=formula,
    )


# -- classification ------------------------------------------------------------
def _norm_usages(usages):
    if usages is None:
        return set()
    if isinstance(usages, str):
        return {usages.strip().lower()}
    return {str(u).strip().lower() for u in usages}


def _date_granularity_warnings(spec):
    warnings = []
    if spec.domain_type == "range" and spec.datatype in ("date", "datetime") and spec.range is not None:
        gran = (spec.range.granularity or "").strip().lower()
        if gran in _NON_DAILY_GRAINS:
            warnings.append(
                "Date range granularity is '%s' but the candidate table is generated daily "
                "(CALENDAR). Bind the slicer to the matching date hierarchy level, or land a "
                "pre-aggregated date table." % gran
            )
        if spec.datatype == "datetime":
            warnings.append(
                "Datetime range: CALENDAR generates date-only values, so the time-of-day component "
                "is dropped. Verify sub-day precision is not required."
            )
    return warnings


def classify_parameter(spec, usages=None, storage_mode=None):
    """Classify how a parameter can be rebuilt, given how the workbook *uses* it.

    ``usages`` is a usage token or iterable of tokens (``measure``/``filter``/``axis``/
    ``dimension``/``measure_swap``/``top_n``/``bin``/``reference_line``/``calc_column``) supplied by
    the workbook-parsing stream. ``storage_mode`` (optional) lets the classifier fold in a
    composite/Direct-Lake manual note. Returns a :class:`CapabilityClass`.
    """
    u = _norm_usages(usages)
    warnings = []

    # Unbounded: cannot enumerate -> default-only, not deploy-ready (LOUD).
    if spec.domain_type == "all":
        cc = CapabilityClass(
            name=CLASS_MANUAL_UNBOUNDED, tier=None, strategy=STRAT_DEFAULT_ONLY,
            deploy_ready=False,
            warnings=[
                "Unbounded 'all' parameter: arbitrary user input cannot be enumerated as a "
                "disconnected table. Emitting a default-only constant measure; recreate as a "
                "bounded slicer / what-if parameter manually before deploy.",
            ],
        )
        spec.usage_class = cc.name
        return cc

    # List with no members is effectively unbounded for our purposes.
    if spec.domain_type == "list" and not spec.members:
        cc = CapabilityClass(
            name=CLASS_MANUAL_UNBOUNDED, tier=None, strategy=STRAT_DEFAULT_ONLY,
            deploy_ready=False,
            warnings=["List parameter carried no members; emitting a default-only constant measure."],
        )
        spec.usage_class = cc.name
        return cc

    sm_note = _storage_mode_note(storage_mode) if storage_mode is not None else None
    directlake = sm_note is not None and "Direct Lake" in sm_note
    gran_warnings = _date_granularity_warnings(spec)

    def finish(name, tier, strategy, deploy_ready, extra=None):
        w = list(extra or [])
        w.extend(gran_warnings)
        if sm_note:
            w.append(sm_note)
        cc = CapabilityClass(
            name=name, tier=tier, strategy=strategy,
            deploy_ready=deploy_ready and not directlake, warnings=w,
        )
        spec.usage_class = cc.name
        return cc

    # Usage-driven priority: most-constraining / most-dangerous first.
    if USAGE_CALC_COLUMN in u:
        return finish(
            CLASS_CALC_COLUMN, None, STRAT_MANUAL, False,
            extra=[
                "Parameter drives a ROW-LEVEL calculated field. A static Power BI calculated column "
                "evaluates at refresh, not at slicer-time, so it would silently ignore the slicer. "
                "Rebuild the dependent logic as a MEASURE that reads the value measure instead.",
            ],
        )
    if USAGE_BIN in u:
        return finish(
            CLASS_BIN_SIZE, None, STRAT_MANUAL, False,
            extra=[
                "Parameter controls a bin SIZE. Power BI bins are static; a parameter-driven bin "
                "width needs a dynamic grouping measure (band via SWITCH/FLOOR over the value "
                "measure). Flagged for manual rebuild.",
            ],
        )
    if USAGE_TOP_N in u:
        return finish(
            CLASS_TOP_N, 3, STRAT_TOPN_FILTER, True,
            extra=[
                "Top-N is a CALCULATION (RANKX + filter measure), not a static visual filter. "
                "Apply the emitted filter measure as a visual-level filter (= 1).",
            ],
        )
    if USAGE_REFERENCE_LINE in u:
        return finish(
            CLASS_REFERENCE_LINE, 1, STRAT_VALUE_MEASURE, False,
            extra=[
                "Parameter drives a reference line. Emitting the value measure; wire it as a dynamic "
                "(measure-based) line in the Analytics pane where the visual supports it. Flagged "
                "for manual verification.",
            ],
        )
    if USAGE_MEASURE_SWAP in u:
        return finish(
            CLASS_MEASURE_SWAP, 2, STRAT_SWITCH_MEASURE, True,
            extra=[
                "Measure-swap parameter: build a SWITCH measure over the value measure that returns "
                "the chosen metric (param_switch_measure).",
            ],
        )
    if u & {USAGE_AXIS, USAGE_DIMENSION}:
        return finish(
            CLASS_DIMENSION_SWAP, 2, STRAT_TREATAS_FILTER, True,
            extra=[
                "Dimension/axis-swap parameter: apply the selection to the real fact column with "
                "TREATAS (param_filter_measure), or a Field Parameter for an axis swap.",
            ],
        )
    if USAGE_FILTER in u:
        return finish(
            CLASS_VISUAL_FILTER, 2, STRAT_TREATAS_FILTER, True,
            extra=[
                "Visual-filter parameter: apply the selection to the fact table via a "
                "CALCULATE(..., TREATAS(...)) measure (param_filter_measure), not a relationship.",
            ],
        )

    # Default (used inside a measure, or no usage info): Tier-1 value measure.
    extra = ["Single-select control: configure the slicer to single-select so multi-select falls "
             "back to the default deterministically."]
    if not u:
        extra.append("No workbook usage info supplied; defaulting to a Tier-1 value measure. Verify "
                     "the parameter is read inside a measure, not used as an axis/filter/bin.")
    if not _default_in_members(spec):
        extra.append("Default value is not among the listed members; the value measure can return a "
                     "value the slicer cannot select.")
    if spec.domain_type == "list" and not _values_unique(spec.members):
        extra.append("Duplicate member values detected; Sort-By-Column is skipped (a displayed value "
                     "must map to a single ordinal).")
    if spec.domain_type == "list" and _has_distinct_aliases(spec) and not _labels_unique(spec):
        extra.append("Duplicate member captions/aliases detected; the slicer cannot distinguish them.")
    return finish(CLASS_MEASURE_VALUE, 1, STRAT_VALUE_MEASURE, True, extra=extra)


# -- storage-mode awareness ----------------------------------------------------
def _norm_storage_mode(sm):
    s = (sm or "import").strip().lower()
    s = s.replace(" ", "").replace("-", "").replace("_", "")
    if s in ("directquery", "dq"):
        return "directquery"
    if s in ("directlake", "directlakeononelake"):
        return "directlake"
    return "import"


def _storage_mode_note(storage_mode):
    s = _norm_storage_mode(storage_mode)
    if s == "directquery":
        return (
            "DirectQuery model: a DAX calculated table is an Import/Dual island, so adding this "
            "parameter table makes the model composite. Confirm composite mode is acceptable."
        )
    if s == "directlake":
        return (
            "Direct Lake model: DAX calculated tables are not supported in a pure Direct Lake "
            "model. Land this parameter table as a Delta table, or add it as an Import/Dual "
            "(composite) table; it will not refresh as Direct Lake."
        )
    return None


# -- TMDL emission (Tier 1) ----------------------------------------------------
def _calc_column_tmdl(col_name, tmdl_type, hidden=False, sort_by=None, summarize="none"):
    lines = [f"\tcolumn {q(col_name)}", f"\t\tdataType: {tmdl_type}"]
    if hidden:
        lines.append("\t\tisHidden")
    lines.append(f"\t\tlineageTag: {uuid.uuid4()}")
    lines.append(f"\t\tsummarizeBy: {summarize}")
    lines.append(f"\t\tsourceColumn: [{_brk(col_name)}]")
    if sort_by:
        lines.append(f"\t\tsortByColumn: {q(sort_by)}")
    lines.append("\t\ttype: calculatedTableColumn")
    return "\n".join(lines)


def _range_source_and_columns(spec):
    """Return ``(source_dax, [column_tmdl, ...])`` for a range parameter."""
    rng = spec.range
    if spec.datatype in ("date", "datetime"):
        src = "CALENDAR(%s, %s)" % (
            _dax_date_literal(rng.min, spec.datatype),
            _dax_date_literal(rng.max, spec.datatype),
        )
        cols = [_calc_column_tmdl("Date", "dateTime")]
        return src, cols
    step = rng.step if rng.step is not None else 1
    tmdl_type = _TMDL_TYPE[spec.datatype]
    src = "GENERATESERIES(%s, %s, %s)" % (_num(rng.min), _num(rng.max), _num(step))
    cols = [_calc_column_tmdl("Value", tmdl_type)]
    return src, cols


def _list_source_and_columns(spec):
    """Return ``(source_dax, [column_tmdl, ...])`` for a list parameter (DATATABLE)."""
    value_col = param_value_column(spec)
    order_col = param_order_column(spec)
    two_col = _has_distinct_aliases(spec)
    token = _DATATABLE_TOKEN[spec.datatype]
    value_unique = _values_unique(spec.members)
    labels_unique = _labels_unique(spec)

    headers = [(value_col, token)]
    label_col = None
    if two_col:
        label_col = param_slicer_column(spec)
        headers.append((label_col, "STRING"))
    headers.append((order_col, "INTEGER"))

    rows = []
    for i, (val, alias) in enumerate(spec.members, start=1):
        cells = [_dax_literal(val, spec.datatype)]
        if two_col:
            label = alias if alias is not None else _display(val, spec.datatype)
            cells.append(_dax_literal(label, "string"))
        cells.append(str(i))
        rows.append("{ " + ", ".join(cells) + " }")

    header_dax = ", ".join("%s, %s" % (_dax_literal(h, "string"), t) for h, t in headers)
    src = "DATATABLE(%s, { %s })" % (header_dax, ", ".join(rows))

    cols = []
    if two_col:
        # Value column hidden (read by the measure); label column drives the slicer.
        cols.append(_calc_column_tmdl(
            value_col, _TMDL_TYPE[spec.datatype], hidden=True,
            sort_by=order_col if value_unique else None,
        ))
        cols.append(_calc_column_tmdl(
            label_col, "string",
            sort_by=order_col if labels_unique else None,
        ))
    else:
        cols.append(_calc_column_tmdl(
            value_col, _TMDL_TYPE[spec.datatype],
            sort_by=order_col if value_unique else None,
        ))
    cols.append(_calc_column_tmdl(order_col, "int64", hidden=True))
    return src, cols


def param_table_tmdl(spec, storage_mode="import"):
    """Emit the disconnected parameter table as a TMDL ``table ... partition = calculated`` block.

    Returns ``""`` for a non-enumerable parameter (``all`` domain or an empty list) -- those get a
    default-only value measure and a manual flag from :func:`classify_parameter` instead. A
    storage-mode constraint (composite under DirectQuery / unsupported under Direct Lake) is
    prepended as a ``///`` description note.
    """
    if not _is_enumerable(spec):
        return ""

    tname = param_table_name(spec)
    if spec.domain_type == "range":
        source_dax, cols = _range_source_and_columns(spec)
    else:
        source_dax, cols = _list_source_and_columns(spec)

    parts = [f"table {q(tname)}", f"\tlineageTag: {uuid.uuid4()}"]
    for col_block in cols:
        parts.append("")
        parts.append(col_block)
    parts.append("")
    parts.append(f"\tpartition {q(tname)} = calculated")
    parts.append("\t\tmode: import")
    parts.append(f"\t\tsource = {source_dax}")
    body = "\n".join(parts) + "\n"

    note = _storage_mode_note(storage_mode)
    if note:
        body = f"/// {note}\n" + body
    return body


def param_value_measure(spec):
    """Return ``(measure_name, dax)`` for the single-select-safe value measure.

    Enumerable: ``X Value = IF(HASONEVALUE('X'[col]), SELECTEDVALUE('X'[col]), <default>)``.
    Non-enumerable (unbounded / empty list): a constant ``X Value = <default>``.
    """
    name = param_ref_name(spec)
    default_dax = _dax_literal(spec.default, spec.datatype) if spec.default is not None else "BLANK()"
    if not _is_enumerable(spec):
        return name, default_dax
    ref = "%s[%s]" % (q(param_table_name(spec)), _brk(param_value_column(spec)))
    dax = "IF(HASONEVALUE(%s), SELECTEDVALUE(%s), %s)" % (ref, ref, default_dax)
    return name, dax


# -- convenience bundle --------------------------------------------------------
def emit_parameter(spec, usages=None, storage_mode="import"):
    """Bundle the full Tier-1 translation for one parameter.

    Returns a dict with: ``capability`` (CapabilityClass), ``table_name``, ``table_tmdl``,
    ``value_measure`` ((name, dax)), ``ref_name``, ``value_column``, ``slicer_column``,
    ``deploy_ready`` and the merged ``warnings``. Designed for the orchestrator stream to consume
    without re-deriving names.
    """
    capability = classify_parameter(spec, usages, storage_mode=storage_mode)
    table_tmdl = param_table_tmdl(spec, storage_mode=storage_mode)
    value_measure = param_value_measure(spec)
    return {
        "capability": capability,
        "table_name": param_table_name(spec),
        "table_tmdl": table_tmdl,
        "value_measure": value_measure,
        "ref_name": param_ref_name(spec),
        "value_column": param_value_column(spec),
        "slicer_column": param_slicer_column(spec),
        "deploy_ready": capability.deploy_ready,
        "warnings": list(capability.warnings),
    }


# -- Tier 2 / Tier 3 emitters --------------------------------------------------
# These take the real fact/dimension column bindings the workbook-parsing / model-emit streams
# resolve (the parameter XML alone does not name the fact column a parameter drives). They emit the
# DAX text; the caller adds the measures to the model and applies any "= 1" visual-level filters.
def _measure_ref(target):
    """Normalize a measure name or DAX expression into an embeddable DAX reference.

    A bare measure name becomes ``[Name]``; anything already bracketed, quoted, or containing a
    call/operator is treated as a DAX expression and kept verbatim. ``None`` -> ``BLANK()``.
    """
    if target is None:
        return "BLANK()"
    s = str(target).strip()
    if not s:
        return "BLANK()"
    if s[0] in "['" or any(ch in s for ch in "()+-*/"):
        return s
    return "[" + _brk(s) + "]"


def _strip_brackets(name):
    s = str(name).strip()
    if s.startswith("[") and s.endswith("]"):
        return s[1:-1]
    return s


def _value_ref(spec):
    """``[<X> Value]`` -- the Tier-1 value measure reference these tiers build on."""
    return "[" + _brk(param_ref_name(spec)) + "]"


def _table_col_ref(spec):
    return "%s[%s]" % (q(param_table_name(spec)), _brk(param_value_column(spec)))


def param_filter_measure(spec, fact_table, target_column, base_measure, measure_name=None):
    """Tier-2 TREATAS filter measure (dimension-swap / visual-filter).

    Recompute ``base_measure`` with the parameter's selection transferred onto a REAL fact column
    via ``TREATAS`` -- recreating Tableau's param-driven filtering with no relationship::

        <base> (filtered by <X>) =
        CALCULATE([<base>], TREATAS({ [<X> Value] }, '<fact>'[<col>]))
    """
    name = measure_name or "%s (filtered by %s)" % (_strip_brackets(base_measure), _base_name(spec))
    dax = "CALCULATE(%s, TREATAS({ %s }, %s[%s]))" % (
        _measure_ref(base_measure), _value_ref(spec), q(fact_table), _brk(target_column),
    )
    return name, dax


def param_switch_measure(spec, choices, default=None, measure_name=None):
    """Tier-2 measure-swap: a ``SWITCH`` over the value measure that returns the chosen metric.

    ``choices`` is an ordered iterable of ``(member_value, target)`` where ``target`` is a measure
    name or a DAX expression. ``default`` is the ELSE branch (``BLANK()`` when omitted)::

        <X> Selected = SWITCH([<X> Value], "Sales", [Sales], "Profit", [Profit], BLANK())
    """
    name = measure_name or (_base_name(spec) + " Selected")
    parts = [_value_ref(spec)]
    for member_value, target in choices:
        parts.append(_dax_literal(member_value, spec.datatype))
        parts.append(_measure_ref(target))
    parts.append(_measure_ref(default))
    return name, "SWITCH(" + ", ".join(parts) + ")"


def param_dependent_flag_measure(child_spec, parent_spec, bridge_table,
                                 parent_bridge_column, child_bridge_column, measure_name=None):
    """Tier-2 cascading (parent -> child) flag measure, applied as a child-slicer visual filter (= 1).

    Returns 1 only for child values that co-occur with the selected parent value in ``bridge_table``
    (the fact/bridge carrying both columns), so the child slicer shows only values valid for the
    parent selection. When the parent is unfiltered, every child value is allowed.
    """
    name = measure_name or "%s Allowed For %s" % (_base_name(child_spec), _base_name(parent_spec))
    parent_ref = _table_col_ref(parent_spec)
    child_ref = _table_col_ref(child_spec)
    dax = (
        "IF(NOT ISFILTERED(%s), 1, "
        "INT(NOT ISEMPTY(CALCULATETABLE(%s, "
        "TREATAS({ %s }, %s[%s]), "
        "TREATAS(VALUES(%s), %s[%s])))))"
    ) % (
        parent_ref,
        q(bridge_table),
        _value_ref(parent_spec), q(bridge_table), _brk(parent_bridge_column),
        child_ref, q(bridge_table), _brk(child_bridge_column),
    )
    return name, dax


def param_rank_measure(dim_table, dim_column, metric, measure_name=None,
                       scope="ALLSELECTED", ties="SKIP"):
    """Tier-3 ranking measure: ``RANKX`` over a dimension by a metric (descending).

    ``ties`` defaults to ``SKIP`` (competition ranking) so tied members consume rank slots and a
    ``rank <= N`` filter keeps about N members -- ``DENSE`` would let the value after a run of ties
    leak past the cut::

        Rank of <dim> by <metric> = RANKX(ALLSELECTED('<dim table>'[<dim col>]), [<metric>], , DESC, SKIP)
    """
    name = measure_name or "Rank of %s by %s" % (dim_column, _strip_brackets(metric))
    dax = "RANKX(%s(%s[%s]), %s, , DESC, %s)" % (
        scope, q(dim_table), _brk(dim_column), _measure_ref(metric), ties,
    )
    return name, dax


def param_topn_filter_measure(spec, rank_measure, measure_name=None, metric=None):
    """Tier-3 Top-N filter measure, applied as a visual-level filter (= 1).

    "Nothing selected = show all" -- Top-N is a calculation, not a static visual filter. Pass
    ``metric`` to add a blank-metric guard (RANKX ranks a blank metric as 0, which would otherwise
    let no-data members slip into the Top-N)::

        <X> Top N Filter =
        IF(NOT ISFILTERED('<X>'[<X>]), 1, IF([<rank>] <= SELECTEDVALUE('<X>'[<X>]), 1, 0))
    """
    name = measure_name or (_base_name(spec) + " Top N Filter")
    tref = _table_col_ref(spec)
    cond = "%s <= SELECTEDVALUE(%s)" % (_measure_ref(rank_measure), tref)
    if metric is not None:
        cond = "NOT ISBLANK(%s) && %s" % (_measure_ref(metric), cond)
    dax = "IF(NOT ISFILTERED(%s), 1, IF(%s, 1, 0))" % (tref, cond)
    return name, dax
