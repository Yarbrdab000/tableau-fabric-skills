"""Recognize a parameter-driven *positional date band* filter and synthesize a faithful
Power BI keep-flag measure for it.

Tableau pattern (the pilot's "Date Filter"):

    case [Parameters].[<P>]
    when v0 then [last] <= v0
    when v1 then [last] <= v1 and [last] >= v0
    ...
    when vN then [last] <= vN
    END

where ``[last]`` is a calc whose formula is ``LAST()`` (a table-positional offset). There is no
faithful row-positional equivalent in a Power BI semantic model, so this is a DELIBERATE,
user-locked semantic REINTERPRETATION: the positional row-bands become *date-day* offsets from
the fact table's max date. The synthesized measure returns ``1`` for rows to keep and ``BLANK``
otherwise, so the report layer can apply it as a visual-level ``keep where == 1`` filter.

Everything here is fail-closed: a calc only becomes a flag when EVERY structural and grounding
check passes (inner resolves to ``LAST()``; ascending bands; first/last branch single, middle
branches bounded with ``lo == previous band``; the controller parameter actually became a what-if
value table whose members equal the bands; exactly one active fact-date anchor; a Date dimension
exists). Otherwise the calc is left to its normal (stub) path and the model is unchanged.

Original work for this skill. stdlib only.
"""
import re

__all__ = ["parse_band_case", "recognize_date_window_flag", "build_dax",
           "build_date_window_flags"]


def _canon_num(s):
    """Canonicalize a Tableau numeric literal to a clean integral/decimal string.

    ``"15."`` -> ``"15"``, ``"30.0"`` -> ``"30"``, ``"41"`` -> ``"41"``. Returns the stripped
    input unchanged if it is not numeric (callers compare canonical forms, so a non-numeric
    simply fails the equality checks downstream).
    """
    t = (s or "").strip()
    try:
        f = float(t)
    except (TypeError, ValueError):
        return t
    if f == int(f):
        return str(int(f))
    return repr(f)


def _strip_brackets(s):
    return (s or "").strip().strip("[]").strip()


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_pred(pred):
    """Parse a band predicate over a single inner ref.

    Single: ``[inner] <= hi``  -> ``{"inner", "hi", "lo": None}``
    Bounded: ``[inner] <= hi and [inner] >= lo`` (either order) -> ``{"inner", "hi", "lo"}``
    Anything else -> ``None`` (fail-closed).
    """
    parts = re.split(r"\s+and\s+", (pred or "").strip(), flags=re.I)
    comps = []
    for part in parts:
        m = re.match(r"^\[([^\]]+)\]\s*(<=|>=)\s*([0-9.]+)\s*$", part.strip())
        if not m:
            return None
        comps.append((m.group(1).strip(), m.group(2), _canon_num(m.group(3))))
    inners = {c[0] for c in comps}
    if len(inners) != 1:
        return None
    inner = comps[0][0]
    if len(comps) == 1:
        if comps[0][1] != "<=":
            return None
        return {"inner": inner, "hi": comps[0][2], "lo": None}
    if len(comps) == 2:
        le = [c for c in comps if c[1] == "<="]
        ge = [c for c in comps if c[1] == ">="]
        if len(le) != 1 or len(ge) != 1:
            return None
        return {"inner": inner, "hi": le[0][2], "lo": ge[0][2]}
    return None


def parse_band_case(formula):
    """Structurally parse the band-case shape. Returns ``{"controller", "branches":[...]}`` or
    ``None``. Does NOT validate grounding (LAST/param/anchor) -- that is ``recognize_*``'s job."""
    if not formula:
        return None
    s = re.sub(r"\s+", " ", formula).strip()
    m = re.match(r"^case\s+\[Parameters\]\.\[([^\]]+)\]\s+(.*?)\s+end$", s, flags=re.I)
    if not m:
        return None
    controller = m.group(1).strip()
    body = m.group(2)
    segs = [seg.strip() for seg in re.split(r"\bwhen\b", body, flags=re.I) if seg.strip()]
    if len(segs) < 2:
        return None
    branches = []
    for seg in segs:
        mm = re.match(r"^([0-9.]+)\s+then\s+(.*)$", seg, flags=re.I)
        if not mm:
            return None
        pred = _parse_pred(mm.group(2))
        if pred is None:
            return None
        branches.append({"when": _canon_num(mm.group(1)), **pred})
    return {"controller": controller, "branches": branches}


def _resolves_to_last(inner_ref, calcs):
    """True iff ``inner_ref`` names a calc whose formula is exactly ``LAST()``."""
    key = _strip_brackets(inner_ref).lower()
    for c in calcs or []:
        names = {(_strip_brackets(c.get("name")) or "").lower(),
                 (_strip_brackets(c.get("internal_name")) or "").lower()}
        if key in names and key:
            return re.sub(r"\s+", "", (c.get("formula") or "")).upper() == "LAST()"
    return False


def _find_by_internal(items, controller):
    key = _strip_brackets(controller).lower()
    for it in items or []:
        if (_strip_brackets(it.get("internal_name")) or "").lower() == key and key:
            return it
    return None


def _single_anchor(active_date_cols):
    """Exactly one active fact-date column -> ``(table, col)``; else ``None``."""
    cols = list(active_date_cols or [])
    if len(cols) != 1:
        return None
    t, c = cols[0]
    if not t or not c:
        return None
    return (t, c)


def _ascending(vals):
    nums = [_num(v) for v in vals]
    if any(n is None for n in nums):
        return False
    return all(nums[i] < nums[i + 1] for i in range(len(nums) - 1))


def recognize_date_window_flag(calc, calcs, parameters, consumed_params,
                               date_name, active_date_cols):
    """Return a recognition dict if ``calc`` is a faithful parameter-driven date band, else
    ``None``. Fail-closed: every structural + grounding check must pass."""
    parsed = parse_band_case(calc.get("formula"))
    if not parsed:
        return None
    branches = parsed["branches"]
    if len(branches) < 2:
        return None

    inners = {b["inner"] for b in branches}
    if len(inners) != 1:
        return None
    if not _resolves_to_last(next(iter(inners)), calcs):
        return None

    vals = [b["when"] for b in branches]
    if not _ascending(vals):
        return None
    for b in branches:               # each band's hi bound is its own when-value
        if b["hi"] != b["when"]:
            return None
    if branches[0]["lo"] is not None:        # first band: single (<= v0)
        return None
    if branches[-1]["lo"] is not None:       # last band: single (<= vN) == "all"
        return None
    for k in range(1, len(branches) - 1):    # middle bands: bounded, lo == previous band
        if branches[k]["lo"] is None or branches[k]["lo"] != vals[k - 1]:
            return None

    cp = _find_by_internal(consumed_params, parsed["controller"])
    if cp is None or not cp.get("table"):    # the param must have become a what-if value table
        return None
    pdict = _find_by_internal(parameters, parsed["controller"])
    if pdict is None:
        return None
    members = sorted(_canon_num(x) for x in (pdict.get("members") or []))
    if members != sorted(vals):              # members must equal the bands exactly
        return None

    anchor = _single_anchor(active_date_cols)
    if anchor is None or not date_name:
        return None

    return {
        "source_caption": calc.get("name"),
        "source_internal": calc.get("internal_name"),
        "controller": parsed["controller"],
        "value_table": cp["table"],
        "value_col": cp["table"],            # value column name == table name (emit_value_parameters)
        "default": vals[0],
        "anchor_table": anchor[0],
        "anchor_col": anchor[1],
        "date_name": date_name,
        "date_key": "Date",                  # date-dim key column (assemble_model _build_date_dimension)
        "bands": vals,
    }


def build_dax(recog):
    """Synthesize the keep-flag DAX (returns 1 to keep / BLANK to drop)."""
    ft, fc = recog["anchor_table"], recog["anchor_col"]
    dn, dk = recog["date_name"], recog["date_key"]
    pt, pc = recog["value_table"], recog["value_col"]
    vals = recog["bands"]
    lines = [
        f"VAR anchor = CALCULATE(MAX('{ft}'[{fc}]), ALL('{ft}'))",
        f"VAR d = SELECTEDVALUE('{dn}'[{dk}])",
        f"VAR sel = SELECTEDVALUE('{pt}'[{pc}], {vals[0]})",
        "RETURN",
        "SWITCH(",
        "    TRUE(),",
    ]
    branch_lines = []
    for k, v in enumerate(vals):
        if k == 0:
            expr = f"IF(d > anchor - {v}, 1)"
        elif k == len(vals) - 1:
            expr = "1"
        else:
            expr = f"IF(d > anchor - {v} && d <= anchor - {vals[k - 1]}, 1)"
        branch_lines.append(f"    sel = {v}, {expr}")
    lines.append(",\n".join(branch_lines))
    lines.append(")")
    return "\n".join(lines)


def _uniquify(candidate, reserved_lower, *, exclude=None):
    taken = {r for r in (reserved_lower or set()) if r not in (exclude or set())}
    if candidate.lower() not in taken:
        return candidate
    base = f"{candidate} (flag)"
    if base.lower() not in taken:
        return base
    i = 2
    while f"{base} {i}".lower() in taken:
        i += 1
    return f"{base} {i}"


def build_date_window_flags(calcs, parameters, consumed_params, *,
                            date_name, active_date_cols, reserved_names=None):
    """Recognize every date-band calc and synthesize its flag measure + filter binding.

    Returns ``(flag_measures, filter_bindings)``:
      * ``flag_measures`` -- consumed by ``_measures_part``: each ``{measure, dax,
        tableau_formula, translated_by, source_calc_name, source_calc_id, report_row}``.
        ``_measures_part`` skips the source calc's plain stub and emits the flag instead.
      * ``filter_bindings`` -- ``{token: {model_table, measure_name, status, predicate, value,
        calc_id, param_internal}}`` keyed by the source calc CAPTION (the stable join token).
        Empty ``{}`` when nothing matched, so no-flag reports stay byte-identical.
    """
    flag_measures, filter_bindings = [], {}
    reserved_lower = {(r or "").lower() for r in (reserved_names or set())}
    for calc in calcs or []:
        recog = recognize_date_window_flag(
            calc, calcs, parameters or [], consumed_params or [], date_name, active_date_cols)
        if not recog:
            continue
        src_caption = recog["source_caption"] or "Date Filter"
        measure_name = _uniquify(src_caption, reserved_lower, exclude={src_caption.lower()})
        reserved_lower.add(measure_name.lower())
        dax = build_dax(recog)
        tab_formula = calc.get("formula", "")
        translated_by = "deterministic (parameter-driven date window)"
        report_row = {
            "measure": measure_name,
            "status": "translated",
            "reason": None,
            "dax": dax,
            "tableau_formula": tab_formula,
            "translated_by": translated_by,
            "source": {
                "kind": "calc_column",
                "model_table": "_Measures",
                "field_caption": src_caption,
                "calc_instance_token": recog["source_internal"],
                "intent": "measure",
            },
        }
        flag_measures.append({
            "measure": measure_name,
            "dax": dax,
            "tableau_formula": tab_formula,
            "translated_by": translated_by,
            "source_calc_name": src_caption,
            "source_calc_id": recog["source_internal"],
            "report_row": report_row,
        })
        filter_bindings[src_caption] = {
            "model_table": "_Measures",
            "measure_name": measure_name,
            "status": "translated",
            "predicate": {"op": "==", "value": 1},
            "value": 1,
            "calc_id": recog["source_internal"],
            "param_internal": recog["controller"],
        }
    return flag_measures, filter_bindings
