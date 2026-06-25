"""Recover per-calc USAGE + INTENT from a Tableau ``.twb`` (the model<->viz contract, viz side).

Read-only, stdlib-only, offline. This is the **viz -> model** channel of the workbook-conversion
contract: for every workbook-local calculated field, it classifies what the dashboard actually
*uses* the calc for (its ``intent``) and on which encoding roles (``used_as``), so the
datasource-migration (model) build can route each calc deterministically:

* ``measure``              -- a value reused on an axis / label / tooltip / colour scale; translate
                              it to a named ``_Measures`` measure (the model build owns the DAX).
* ``formatting``           -- a calc whose only job is to *colour* marks (an ``IF cond THEN
                              "<colour>" ...`` string calc on the colour shelf). Power BI expresses
                              this natively as a **rules-based conditional format**, frequently with
                              **no DAX at all** -- so the model build need not translate it; the viz
                              build emits the native rule (see ``formatting``).
* ``filter``               -- a boolean calc used only to filter a worksheet (often parameter
                              driven); it maps to a slicer / visual filter, not a measure.
* ``native_visual_feature``-- a quick table calc with a native Power BI "show value as" equivalent
                              used on a displayed value with default addressing (a future native
                              well; today the viz emitter still warns).
* ``row_level_column``     -- a row-level dimension calc used as a category.

**Addressing** (Compute Using: partition_by / order_by) for table calcs is owned by
``workbook_table_calcs.extract_table_calc_usages`` and is joined back by ``(worksheet, instance)`` --
this module never re-derives it. Output is keyed by the calc's bare internal id (the same token the
model build reports measures under), so the two halves join deterministically.

Only the public Tableau workbook XML structure was used; original, deterministic, offline.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import xml.etree.ElementTree as ET

from workbook_table_calcs import (
    _children_local,
    _findall_local,
    _first,
    _local,
    _token_instance,
    _unbracket,
    extract_table_calc_usages,
    load_workbook_xml,
)

# Encoding roles a calc pill can sit on (marks card + shelves + filter). The marks-card tag name is
# mapped to a stable role token shared with the rest of the skill.
_ENCODING_ROLE = {
    "color": "color_encoding",
    "size": "size_encoding",
    "text": "label",
    "label": "label",
    "lod": "detail",
    "level-of-detail": "detail",
    "tooltip": "tooltip",
    "wedge-size": "angle",
}

_INTENT_MEASURE = "measure"
_INTENT_FORMATTING = "formatting"
_INTENT_FILTER = "filter"
_INTENT_NATIVE = "native_visual_feature"
_INTENT_ROW_LEVEL = "row_level_column"

# A small, defensive set of comparison operators a threshold rule can carry. Tableau writes ``=`` /
# ``==`` for equality and ``!=`` / ``<>`` for inequality; the rest are the usual relations.
_CMP_RE = re.compile(r"^(?P<lhs>.+?)\s*(?P<op>!=|<>|<=|>=|==|=|<|>)\s*(?P<rhs>.+)$", re.DOTALL)
_FIELD_TOKEN_RE = re.compile(r"\[[^\[\]]+\](?:\.\[[^\[\]]+\])?")
_NUMERIC_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)$")


@dataclass
class CalcUsage:
    """One workbook-local calc, with its classified intent and where the dashboard uses it."""
    calc_id: str
    caption: str
    formula: Optional[str]
    role: str                                   # measure | dimension | unknown (calc def role)
    datatype: Optional[str]
    intent: str
    used_as: List[str] = field(default_factory=list)
    instances: List[str] = field(default_factory=list)
    worksheets: List[str] = field(default_factory=list)
    is_table_calc: bool = False
    native_feature_hint: Optional[str] = None
    formatting: Optional[dict] = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# -- calc definitions + per-view instance maps ---------------------------------
def _calc_definitions(root) -> Dict[str, dict]:
    """``{calc_id: {caption, formula, role, datatype}}`` for every calculated ``<column>``.

    Reads both the datasource-level calc definitions and the per-worksheet
    ``<datasource-dependencies>`` copies (a worksheet carries the calcs it references); the first
    definition with a formula wins, later captions/roles fill gaps.
    """
    defs: Dict[str, dict] = {}
    for col in _findall_local(root, "column"):
        calc = _first(col, "calculation")
        if calc is None or calc.get("class") != "tableau":
            continue
        formula = calc.get("formula")
        if formula is None:
            continue
        cid = _unbracket(col.get("name"))
        if not cid:
            continue
        cur = defs.setdefault(cid, {"caption": None, "formula": None,
                                    "role": None, "datatype": None})
        if cur["formula"] is None:
            cur["formula"] = formula
        cur["caption"] = cur["caption"] or col.get("caption") or cid
        cur["role"] = cur["role"] or col.get("role")
        cur["datatype"] = cur["datatype"] or col.get("datatype")
    return defs


def _worksheet_views(root):
    """Yield ``(worksheet_element, view_element, table_element)`` for every worksheet.

    The ``<datasource-dependencies>`` (instance + calc maps) live under ``<view>``; the marks-card
    ``<encodings>`` and the ``<rows>``/``<cols>`` shelves live under ``<table>`` (a sibling of
    ``<view>``); ``<filter>`` elements live under the worksheet. All three scopes are returned so
    the usage scan reads each from the right place.
    """
    for ws in _findall_local(root, "worksheet"):
        table = _first(ws, "table")
        if table is None:
            continue
        v = _first(table, "view")
        yield ws, (table if v is None else v), table


def _instance_columns(scope) -> Dict[str, str]:
    """``{instance_id: underlying_column_id}`` for every column-instance under ``scope``."""
    out: Dict[str, str] = {}
    for dep in _findall_local(scope, "datasource-dependencies"):
        for ci in _children_local(dep, "column-instance"):
            iid = _unbracket(ci.get("name"))
            if iid:
                out[iid] = _unbracket(ci.get("column"))
    return out


def _column_of_token(token: Optional[str], inst_cols: Dict[str, str]) -> Optional[str]:
    """A ``[ds].[instance]`` (or ``[instance]``) reference -> the underlying field/calc id."""
    if not token:
        return None
    token = token.strip()
    inst = _token_instance(token) if token.startswith("[") and "].[" in token else None
    if inst is None:
        inst = _unbracket(token)
    return inst_cols.get(inst, inst)


# -- formatting (colour-string) calc detection ---------------------------------
def _mask_literals(formula: str):
    """Replace string literals with placeholders so keyword splitting can't break inside them.

    Returns ``(masked_text, [literal0, literal1, ...])`` where each placeholder ``\x00N\x00`` maps
    to the unquoted literal text. Both ``"..."`` and ``'...'`` Tableau string literals are handled.
    """
    lits: List[str] = []

    def _take(m):
        lits.append(m.group(0)[1:-1])
        return "\x00{0}\x00".format(len(lits) - 1)

    masked = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', _take, formula)
    return masked, lits


def _is_literal_placeholder(text: str) -> Optional[int]:
    m = re.fullmatch(r"\x00(\d+)\x00", text.strip())
    return int(m.group(1)) if m else None


def _split_if_chain(masked: str):
    """Split a (literal-masked) ``IF/ELSEIF/THEN/ELSE/END`` formula into branches.

    Returns ``(branches, else_part)`` where ``branches = [(cond, result), ...]`` and ``else_part``
    is the trailing ``ELSE`` text (or ``None``). Returns ``None`` when the formula is not a single
    top-level ``IF`` expression. Keyword matching is word-boundaried and case-insensitive.
    """
    if not re.match(r"\s*if\b", masked, re.IGNORECASE):
        return None
    body = re.sub(r"\bend\b\s*$", "", masked.strip(), flags=re.IGNORECASE)
    body = re.sub(r"^\s*if\b", "", body, flags=re.IGNORECASE)
    parts = re.split(r"\belse\b", body, maxsplit=1, flags=re.IGNORECASE)
    head, else_part = parts[0], (parts[1] if len(parts) > 1 else None)
    branches = []
    for seg in re.split(r"\belseif\b", head, flags=re.IGNORECASE):
        tp = re.split(r"\bthen\b", seg, maxsplit=1, flags=re.IGNORECASE)
        if len(tp) != 2:
            return None
        branches.append((tp[0].strip(), tp[1].strip()))
    if not branches:
        return None
    return branches, (else_part.strip() if else_part is not None else None)


def _resolve_result(text: str, lits: List[str]) -> Optional[str]:
    """A branch result -> its string-literal value, or ``None`` when it is not a bare literal."""
    idx = _is_literal_placeholder(text)
    return lits[idx] if idx is not None else None


def _parse_condition(cond: str, lits: List[str], inst_cols: Dict[str, str]) -> Optional[dict]:
    """``<field> <op> <number>`` -> ``{input_calc, op, value}``; ``None`` when not a simple
    field-vs-number threshold (the only shape that maps to a native PBI numeric rule)."""
    m = _CMP_RE.match(cond.strip())
    if not m:
        return None
    lhs, op, rhs = m.group("lhs").strip(), m.group("op"), m.group("rhs").strip()
    field_side, num_side = None, None
    for side in (lhs, rhs):
        toks = _FIELD_TOKEN_RE.findall(side)
        if len(toks) == 1 and toks[0] == side:
            field_side = side
        elif _NUMERIC_RE.match(side):
            num_side = side
    if field_side is None or num_side is None:
        return None
    # normalise so the operator reads field-op-number (flip if the number was on the left)
    if field_side == rhs:
        op = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=",
              "==": "==", "!=": "!=", "<>": "<>"}[op]
    return {"input_calc": _column_of_token(field_side, inst_cols),
            "op": op, "value": float(num_side)}


def _formatting_spec(formula: str, inst_cols: Dict[str, str]) -> Optional[dict]:
    """Classify a calc formula as a colour/category formatter, returning a native rule spec.

    A *formatting* calc is an ``IF ... THEN "<str>" ... ELSE "<str>" END`` whose every branch result
    is a **string literal** -- it produces a category/colour token, not a number, so in Power BI it
    is a rules-based conditional format (often needing no measure). The returned spec carries the
    ordered rules and an overall ``native_derivable`` flag: ``True`` only when every branch condition
    is a simple ``field <op> number`` threshold over a single shared input (so the viz build can emit
    a faithful native rule); otherwise the formatting intent still stands but the rule is handed off.
    """
    if formula is None:
        return None
    masked, lits = _mask_literals(formula)
    chain = _split_if_chain(masked)
    if chain is None:
        return None
    branches, else_part = chain
    results = [_resolve_result(res, lits) for _cond, res in branches]
    else_result = _resolve_result(else_part, lits) if else_part is not None else None
    # every THEN result must be a bare string literal; the ELSE (if present) too. A numeric/None
    # result means this is a value calc, not a pure formatter.
    if any(r is None for r in results):
        return None
    if else_part is not None and else_result is None:
        return None

    rules, inputs, derivable = [], set(), True
    for (cond, _res), result in zip(branches, results):
        parsed = _parse_condition(cond, lits, inst_cols)
        if parsed is None:
            derivable = False
            rules.append({"op": None, "value": None, "result": result,
                          "condition": cond.strip()})
        else:
            inputs.add(parsed["input_calc"])
            rules.append({"input_calc": parsed["input_calc"], "op": parsed["op"],
                          "value": parsed["value"], "result": result})
    if else_result is not None:
        rules.append({"op": None, "value": None, "result": else_result, "default": True})
    if len(inputs) > 1:
        derivable = False
    return {
        "type": "rules",
        "input_calc": next(iter(inputs)) if len(inputs) == 1 else None,
        "rules": rules,
        "native_derivable": derivable,
    }


# -- usage scan + intent classification ----------------------------------------
def _scan_usages(root):
    """``{calc_id: {"used_as": set, "instances": set, "worksheets": set}}`` across all worksheets.

    Records every encoding role, shelf (``axis``) and ``filter`` placement of a column-instance
    whose underlying column is a calc id. Calc ids are discovered by the caller; here we record any
    column reference and the caller intersects with the known calc set.
    """
    usage: Dict[str, dict] = {}

    def _touch(cid, role, inst, ws_name):
        if not cid:
            return
        slot = usage.setdefault(cid, {"used_as": set(), "instances": set(),
                                      "worksheets": set()})
        slot["used_as"].add(role)
        if inst:
            slot["instances"].add(inst)
        if ws_name:
            slot["worksheets"].add(ws_name)

    for ws_el, view, table in _worksheet_views(root):
        ws_name = ws_el.get("name")
        inst_cols = _instance_columns(ws_el)            # all deps under the worksheet
        # marks-card encodings (colour / size / text / lod / tooltip / wedge) live under <table>
        for enc in _findall_local(table, "encodings"):
            for child in list(enc):
                role = _ENCODING_ROLE.get(_local(child.tag))
                if not role:
                    continue
                inst = _column_inst(child.get("column"))
                _touch(inst_cols.get(inst, inst) if inst else None, role, inst, ws_name)
        # rows / cols shelves -> axis
        for shelf in ("rows", "cols"):
            el = _first(table, shelf)
            text = el.text if el is not None else ""
            for tok in _FIELD_TOKEN_RE.findall(text or ""):
                inst = _token_instance(tok) if "].[" in tok else _unbracket(tok)
                _touch(inst_cols.get(inst, inst) if inst else None, "axis", inst, ws_name)
        # filters (worksheet scope)
        for filt in _findall_local(ws_el, "filter"):
            inst = _column_inst(filt.get("column"))
            _touch(inst_cols.get(inst, inst) if inst else None, "filter", inst, ws_name)
    return usage


def _column_inst(token: Optional[str]) -> Optional[str]:
    """A ``[ds].[instance]`` / ``[instance]`` attribute -> the instance id (no brackets)."""
    if not token:
        return None
    token = token.strip()
    if token.startswith("[") and "].[" in token:
        return _token_instance(token)
    return _unbracket(token)


def _classify_intent(definition, slot, table_calc_instances):
    """Pick the intent + native hint for one calc from its definition + observed usage."""
    used_as = slot["used_as"]
    instances = slot["instances"]
    is_tc = bool(instances & table_calc_instances)
    fmt = _formatting_spec(definition["formula"], {})

    # pure FORMATTING: a colour-string calc used only to colour marks (never a displayed value, an
    # axis, or a filter) -> a native rules-based conditional format, no measure required.
    only_colour = used_as and used_as <= {"color_encoding"}
    if fmt is not None and only_colour:
        return _INTENT_FORMATTING, "conditional_format:rules", fmt

    # FILTER-only: the calc never reaches a visual encoding/axis, just gates rows.
    if used_as and used_as <= {"filter"}:
        return _INTENT_FILTER, None, None

    # a quick table calc reused on the colour scale / FillRule or any displayed value is a MEASURE
    # (the model owns its DAX); a percent-difference QTC carries a native "show value as" hint.
    if is_tc:
        hint = None
        if any("pcdf" in i for i in instances):
            hint = "show_value_as:percent_difference"
        return _INTENT_MEASURE, hint, None

    # a measure-role calc used as a value -> MEASURE; a dimension-role calc used as a category ->
    # a row-level column. A formatter that ALSO appears elsewhere stays a measure-or-rowlevel by
    # role (its colour use is still reported via used_as for the model build to consider).
    if (definition.get("role") or "").lower() == "dimension" and "axis" in used_as:
        return _INTENT_ROW_LEVEL, None, None
    return _INTENT_MEASURE, None, None


def workbook_calc_usage(xml_text: str) -> dict:
    """Classify every workbook-local calc's intent + usage for the model<->viz contract.

    Returns ``{"calcs": {calc_id: CalcUsage.to_dict()}, "formatting": [calc_id, ...]}``. Only calcs
    actually referenced by a worksheet are included (a defined-but-unused calc is omitted). The
    ``formatting`` list names the calcs that reduce to a native Power BI conditional format.
    """
    root = ET.fromstring(xml_text)
    definitions = _calc_definitions(root)
    usage = _scan_usages(root)
    table_calc_instances = {u.instance for u in extract_table_calc_usages(xml_text)}

    calcs: Dict[str, dict] = {}
    formatting: List[str] = []
    for cid, definition in definitions.items():
        slot = usage.get(cid)
        if not slot or not slot["used_as"]:
            continue
        intent, hint, fmt = _classify_intent(definition, slot, table_calc_instances)
        entry = CalcUsage(
            calc_id=cid,
            caption=definition.get("caption") or cid,
            formula=definition.get("formula"),
            role=(definition.get("role") or "unknown"),
            datatype=definition.get("datatype"),
            intent=intent,
            used_as=sorted(slot["used_as"]),
            instances=sorted(slot["instances"]),
            worksheets=sorted(slot["worksheets"]),
            is_table_calc=bool(slot["instances"] & table_calc_instances),
            native_feature_hint=hint,
            formatting=fmt,
        )
        calcs[cid] = entry.to_dict()
        if intent == _INTENT_FORMATTING:
            formatting.append(cid)
    return {"calcs": calcs, "formatting": sorted(formatting)}


def usage_from_file(path: str) -> dict:
    """Convenience: :func:`load_workbook_xml` + :func:`workbook_calc_usage`."""
    return workbook_calc_usage(load_workbook_xml(path))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: workbook_calc_usage.py <workbook.twb|.twbx>", file=sys.stderr)
        return 2
    print(json.dumps(usage_from_file(argv[0]), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
