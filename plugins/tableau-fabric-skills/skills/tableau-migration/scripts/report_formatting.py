"""Pure, inventory-grounded builders for Power BI **report-layer formatting** objects.

These helpers construct the PBIR (``visual.json``) fragments the Tableau -> Power BI migrator emits
for four formatting features that today are detected-and-deferred or dropped:

  * Analytics **reference line**  -> ``visual.objects.y1AxisReferenceLine`` / ``xAxisReferenceLine``
  * Rule-based **conditional fill** -> ``Conditional.Cases[]`` solid-colour form (backColor/fontColor)
  * In-cell **data bars**          -> ``columnFormatting.dataBars`` (table / matrix column)
  * Mark **opacity**               -> object ``transparency`` (inverted: ``transparency = 100 - opacity``)

Every shape here is grounded on REAL ``.pbix`` property serializations catalogued in
``docs/research/powerbi-formatting-inventory.json`` (the ``objectIndex`` raws) and its companion
``powerbi-formatting-color-reference.md`` -- never guessed.  The three value encodings are taken
verbatim from that inventory's ``valueGrammar`` (see :func:`vg_string` / :func:`vg_double`).

Design contract (why this module is pure and standalone):
  * Inputs are **PBIR-natural** -- a number, a hex string, a label, an axis, an opacity, a list of
    threshold cases -- NOT Tableau constructs.  How a given Tableau feature is *detected* and mapped
    onto these inputs is the job of the detection->builder adapters in ``twb_to_pbir`` (wired per
    feature); keeping the emit half here lets it be validated without a Tableau workbook.
  * stdlib-only, no import of ``twb_to_pbir`` -- so ``twb_to_pbir`` may import these builders when the
    wiring lands with no circular dependency.

Confidence notes (verify against a real ``.pbix`` / the migration oracle when wiring):
  * The property VALUE encodings (``value``/``displayName``/``lineColor``/``show``/``style``/
    ``position``; the ``dataBars`` colours; ``transparency``; the ``Conditional.Cases`` fill) are
    HIGH confidence -- copied from real ``objectIndex`` raws.
  * The reference-line object WRAPPER (``{"properties", "selector":{"id"}}``) and the per-column
    data-bar ``selector`` are MEDIUM confidence -- inferred from ``settableAt: PBIR:visual.objects``
    plus the recorded ``selectors:{id}`` / column-scoped metadata; the inventory indexes properties,
    not whole object+selector envelopes.  ``axisColor`` / ``reverseDirection`` inside ``dataBars`` are
    inferred (the real raw is truncated in the index) from the sibling colour form + ``valueGrammar``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# valueGrammar -- the three literal encodings a visualContainer object uses.
# (powerbi-formatting-inventory.json -> valueGrammar.encodings.visualContainerObject)
# ---------------------------------------------------------------------------

def _clean_number(value: Any) -> str:
    """Render a number without float artefacts: ``100`` / ``100.0`` -> ``"100"``, ``-0.5`` -> ``"-0.5"``.

    Whole-valued floats drop the trailing ``.0`` so a threshold of ``1000.0`` serializes as the
    inventory's ``"1000D"`` rather than ``"1000.0D"``.
    """
    if isinstance(value, bool):  # guard: bool is an int subclass
        raise TypeError("numeric literal expected, got bool")
    if isinstance(value, int):
        return str(value)
    f = float(value)
    if f == int(f):
        return str(int(f))
    return repr(f)


def vg_string(value: Any) -> str:
    """A semantic-query string literal: single-quoted, inner apostrophe doubled
    (``O'Brien`` -> ``'O''Brien'``).  Matches ``twb_to_pbir._semantic_string_literal`` byte-for-byte
    (a parity test guards the two)."""
    return "'" + str(value).replace("'", "''") + "'"


def vg_bool(value: Any) -> str:
    """A quoted-boolean literal: ``"true"`` / ``"false"`` (``valueGrammar.literalSyntax.boolean``)."""
    return "true" if value else "false"


def vg_int(value: Any) -> str:
    """A **bare** integer literal (``valueGrammar.literalSyntax.integer`` -> ``"12"``)."""
    return _clean_number(int(value))


def vg_int_l(value: Any) -> str:
    """A semantic-query int64 literal with the ``L`` suffix (``"1L"`` / ``"110L"``), as observed on
    ``columnFormatting.labelPrecision`` / ``dataBars.dataBarWidthPercent`` raws."""
    return _clean_number(int(value)) + "L"


def vg_double(value: Any) -> str:
    """A semantic-query double literal with the trailing ``D`` (``valueGrammar.literalSyntax.double``
    -> ``"12D"`` / ``"0.5D"`` / ``"-0.5D"``).  Used by reference-line ``value`` and ``transparency``,
    whose real raws are the ``D`` form even for whole numbers (``"100D"``)."""
    return _clean_number(value) + "D"


def expr_literal(literal: str) -> Dict[str, Any]:
    """The scalar wrapper ``{"expr": {"Literal": {"Value": <literal>}}}`` every visualContainer
    object property uses."""
    return {"expr": {"Literal": {"Value": literal}}}


def solid_color_literal(hex_color: str) -> Dict[str, Any]:
    """A literal-hex solid colour: ``{"solid": {"color": {"expr": {"Literal": {"Value": "'#RRGGBB'"}}}}}``
    (``valueGrammar.colorForms.literalHex``)."""
    return {"solid": {"color": expr_literal(vg_string(hex_color))}}


# ---------------------------------------------------------------------------
# Comparison / condition primitives (rule-based conditional formatting).
# ComparisonKind ints from the research reference (0=Equal, 1=GT, 2=GTE, 3=LT, 4=LTE);
# these mirror the ints already used by twb_to_pbir's dataPoint selector / measure filters.
# ---------------------------------------------------------------------------

COMPARISON_KIND = {"=": 0, ">": 1, ">=": 2, "<": 3, "<=": 4}


def measure_ref(entity: str, prop: str) -> Dict[str, Any]:
    """A semantic-query measure reference ``{"Measure": {"Expression": {"SourceRef": {"Entity": E}},
    "Property": P}}`` -- the ``Input``/``Left`` operand form seen in real FillRule raws."""
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def comparison(left_expr: Dict[str, Any], op: str, right_literal: str) -> Dict[str, Any]:
    """A ``{"Comparison": {...}}`` condition: ``left_expr`` compared (``op`` in :data:`COMPARISON_KIND`)
    against a right-hand **literal string** (already grammar-encoded, e.g. ``vg_double(1000)``)."""
    if op not in COMPARISON_KIND:
        raise ValueError("unknown comparison op: {0!r}".format(op))
    return {"Comparison": {"ComparisonKind": COMPARISON_KIND[op],
                           "Left": left_expr,
                           "Right": {"Literal": {"Value": right_literal}}}}


def and_(left_cond: Dict[str, Any], right_cond: Dict[str, Any]) -> Dict[str, Any]:
    """Combine two conditions with a boolean ``And`` (``valueGrammar.colorForms.ruleBased`` allows
    ``Comparison|And|Or`` trees)."""
    return {"And": {"Left": left_cond, "Right": right_cond}}


def or_(left_cond: Dict[str, Any], right_cond: Dict[str, Any]) -> Dict[str, Any]:
    """Combine two conditions with a boolean ``Or``."""
    return {"Or": {"Left": left_cond, "Right": right_cond}}


def rule_based_fill(cases: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """A rule-based solid fill: ``{"solid": {"color": {"expr": {"Conditional": {"Cases": [...]}}}}}``.

    ``cases`` is an ordered sequence of ``{"condition": <condition-expr>, "color": "#RRGGBB"}`` (the
    first matching case wins, exactly like Power BI's rules editor).  Each becomes
    ``{"Condition": <condition>, "Value": {"Literal": {"Value": "'#hex'"}}}`` -- the
    ``valueGrammar.colorForms.ruleBased`` shape.  Use :func:`comparison` / :func:`and_` / :func:`or_`
    to build a condition.
    """
    if not cases:
        raise ValueError("rule_based_fill requires at least one case")
    out_cases = []
    for c in cases:
        out_cases.append({"Condition": c["condition"],
                          "Value": {"Literal": {"Value": vg_string(c["color"])}}})
    return {"solid": {"color": {"expr": {"Conditional": {"Cases": out_cases}}}}}


# ---------------------------------------------------------------------------
# Analytics reference line -> y1AxisReferenceLine / xAxisReferenceLine.
# Grounded on objectIndex data::y1AxisReferenceLine raws:
#   value       {"expr":{"Literal":{"Value":"-0.5D"}}}
#   displayName {"expr":{"Literal":{"Value":"'Constant line 1'"}}}
#   lineColor   {"solid":{"color":{"expr":{"Literal":{"Value":"'#..'"}}}}}
#   show        {"expr":{"Literal":{"Value":"true"}}}
#   style       {"expr":{"Literal":{"Value":"'dotted'"}}}   position {"...":"'back'"}
# ---------------------------------------------------------------------------

def reference_line_object(value: Any,
                          *,
                          display_name: Optional[str] = None,
                          line_color_hex: Optional[str] = None,
                          style: Optional[str] = None,
                          position: Optional[str] = None,
                          show: bool = True,
                          line_id: str = "Ref0") -> Dict[str, Any]:
    """One ``{"properties": {...}, "selector": {"id": line_id}}`` reference-line object element.

    Only a **constant** (numeric) reference line is representable here -- ``value`` is a fixed double.
    A Tableau line computed from an aggregation (Average/Median/Total) has no constant to emit and
    must be deferred by the caller, never approximated.
    """
    props: Dict[str, Any] = {
        "show": expr_literal(vg_bool(show)),
        "value": expr_literal(vg_double(value)),
    }
    if display_name:
        props["displayName"] = expr_literal(vg_string(display_name))
    if line_color_hex:
        props["lineColor"] = solid_color_literal(line_color_hex)
    if style:
        props["style"] = expr_literal(vg_string(style))
    if position:
        props["position"] = expr_literal(vg_string(position))
    return {"properties": props, "selector": {"id": line_id}}


def reference_line_objects(lines: Sequence[Dict[str, Any]], axis: str) -> Dict[str, Any]:
    """Assemble constant reference lines onto the correct axis object.

    ``axis`` is ``"value"`` (-> ``y1AxisReferenceLine``, the numeric/Y axis) or ``"category"``
    (-> ``xAxisReferenceLine``, the category/X axis).  ``lines`` is a sequence of kwargs dicts for
    :func:`reference_line_object`; each is given a distinct ``line_id`` when not supplied.
    """
    if axis not in ("value", "category"):
        raise ValueError("axis must be 'value' or 'category', got {0!r}".format(axis))
    key = "y1AxisReferenceLine" if axis == "value" else "xAxisReferenceLine"
    objs = []
    for i, ln in enumerate(lines):
        kwargs = dict(ln)
        kwargs.setdefault("line_id", "Ref{0}".format(i))
        objs.append(reference_line_object(**kwargs))
    return {key: objs}


# ---------------------------------------------------------------------------
# In-cell data bars -> columnFormatting.dataBars (pivotTable / tableEx column).
# Grounded on objectIndex data::columnFormatting.dataBars raw:
#   {"positiveColor":{"solid":{"color":{"expr":{"Literal":{"Value":"'#FDD9AC'"}}}}},
#    "negativeColor":{...}, "axisColor":{...}, "reverseDirection":{...}}
# (axisColor / reverseDirection inferred -- the stored raw is truncated.)
# ---------------------------------------------------------------------------

def data_bars(positive_color_hex: str,
              negative_color_hex: str,
              *,
              axis_color_hex: Optional[str] = None,
              reverse_direction: bool = False) -> Dict[str, Any]:
    """The ``dataBars`` object value ``{"positiveColor", "negativeColor", ["axisColor"],
    "reverseDirection"}`` for a table/matrix column's ``columnFormatting``.  Colours are literal-hex
    solids; ``reverseDirection`` is a quoted-boolean expr literal."""
    out: Dict[str, Any] = {
        "positiveColor": solid_color_literal(positive_color_hex),
        "negativeColor": solid_color_literal(negative_color_hex),
        "reverseDirection": expr_literal(vg_bool(reverse_direction)),
    }
    if axis_color_hex:
        out["axisColor"] = solid_color_literal(axis_color_hex)
    return out


def column_formatting_data_bars(column_query_ref: str,
                                positive_color_hex: str,
                                negative_color_hex: str,
                                *,
                                axis_color_hex: Optional[str] = None,
                                reverse_direction: bool = False) -> Dict[str, Any]:
    """A full ``columnFormatting`` object element scoping :func:`data_bars` to one column.

    The column is targeted by its ``queryRef`` via a metadata selector -- the same column-scoping
    mechanism the existing table conditional-format path uses.  (MEDIUM confidence: selector shape
    verified against the table CF path at wiring time.)
    """
    return {
        "properties": {"dataBars": data_bars(positive_color_hex, negative_color_hex,
                                             axis_color_hex=axis_color_hex,
                                             reverse_direction=reverse_direction)},
        "selector": {"metadata": column_query_ref},
    }


# ---------------------------------------------------------------------------
# Mark opacity -> object `transparency` (fill / line / outline / ...).
# Grounded on real raws: transparency serializes as the DOUBLE form ("100D", "0D", "60D") and is
# INVERTED relative to opacity: PBI transparency 0 = opaque, 100 = fully transparent.
# ---------------------------------------------------------------------------

def transparency_from_opacity(opacity_pct: Any) -> Dict[str, Any]:
    """Map a Tableau **opacity** percentage (0 = invisible .. 100 = solid) to a Power BI
    ``transparency`` property value (``transparency = 100 - opacity``), encoded as the observed
    double literal ``{"expr": {"Literal": {"Value": "<n>D"}}}``.
    """
    o = float(opacity_pct)
    if not 0.0 <= o <= 100.0:
        raise ValueError("opacity_pct must be in [0, 100], got {0!r}".format(opacity_pct))
    transparency = 100.0 - o
    return expr_literal(vg_double(transparency))
