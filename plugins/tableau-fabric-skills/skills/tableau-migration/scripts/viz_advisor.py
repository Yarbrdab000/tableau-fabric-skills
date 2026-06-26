"""Tier-2 LLM-assisted **viz advisor** (deterministic producer + gated applier; offline, stdlib-only).

This is the recommender peer of the calc compiler's Tier-2 assisted-translation loop
(``calc_to_dax`` assisted suggestions -> ``approved_calc_dax`` landing) and of
``image_oracle.py`` (chart-type adjudication of an EXISTING visual from a Tableau-side image).

Where the image oracle *corrects* the type of a worksheet the Tier-1 engine already rebuilt, this
module *proposes* visuals from data SEMANTICS alone: given a set of model fields (each with a role,
data type, and optional geo / temporal / cardinality hint), ``recommend_visuals`` returns a RANKED
list of candidate visualizations -- a chart type, a role-slot field binding, a confidence, and a
machine-readable reasoning string. It is the optional acceleration tier for laying out a report from
a measure / field selection.

Same hard contract as the rest of the engine (**warn-never-wrong**):

* DETERMINISTIC FLOOR -- ``recommend_visuals`` encodes known-good viz facts (which data shape suits
  which chart) as an authored rule table. It NEVER calls a model / LLM and is fully test-covered.
  It always returns at least one recommendation (a faithful table is the universal fallback).
* OUT-OF-BAND LLM ASSIST (never auto-lands) -- ``build_advice_bundle`` / ``advice_prompt`` hand the
  deterministic candidate set to the SEPARATE agent / vision pass to re-rank or refine by user
  intent. No API key, no embedded tool call; deterministic tests inject the agent's answer.
* GATED LANDING -- ``validate_suggestion`` / ``apply_advice`` accept an agent answer ONLY when its
  chart type is in the deterministic candidate set AND every bound field exists in the provided field
  set in a role-compatible slot; otherwise the deterministic top pick stands. A suggestion is never
  silently emitted -- it is a labelled recommendation a human approves, exactly like the calc tier's
  assisted suggestions and the image oracle's adjudications.

Provenance: original, deterministic, offline. The rule table grounds ONLY on this repo's own
``twb_to_pbir`` visual-type vocabulary (the ``VT_*`` constants + ``_VT_TO_PBIR``) and Microsoft PBIR
role semantics. No third-party translator source was consulted.
"""
from __future__ import annotations

import json
import re

ADVISOR_VERSION = 1
ADVISOR_BUNDLE_KIND = "tableau-to-powerbi-viz-advice-request"

# The closed PBIR ``visualType`` vocabulary the advisor may recommend. Mirrors twb_to_pbir's
# ``_VT_TO_PBIR`` plus its default-stacking (stacked*) and card-split (multiRowCard) variants. A
# suggestion whose chart type is outside this set is rejected by the gate.
PBIR_VISUAL_TYPES = frozenset({
    "clusteredColumnChart", "clusteredBarChart", "stackedColumnChart", "stackedBarChart",
    "lineChart", "areaChart", "scatterChart", "pieChart", "donutChart",
    "lineClusteredColumnComboChart", "waterfallChart", "ribbonChart",
    "card", "multiRowCard", "tableEx", "pivotTable", "map", "shapeMap",
})

# Hard invariants surfaced verbatim in the bundle + prompt so they travel WITH the request and cannot
# drift from the applier's enforcement (the warn-never-wrong contract for the advice tier).
ADVISOR_RULES = (
    "Recommend a chart TYPE only from this request's 'candidates' list (the deterministic 'top_pick' "
    "is candidates[0]); any other type is rejected.",
    "Bind ONLY the fields listed in 'fields'; never invent, rename, drop, or re-type a field.",
    "Keep every field in a role-compatible slot: a measure belongs on a value/size/angle/X-Y slot; a "
    "dimension belongs on a category/legend/axis/location slot.",
    "If the data shape is ambiguous, prefer the simpler, more faithful chart and say why in 'reason'.",
    "If you cannot improve on the deterministic top pick, return chosen_index null (keep top_pick).",
)

# -- field semantics -----------------------------------------------------------------------------
_MEASURE_ROLES = frozenset({"measure"})
_DIMENSION_ROLES = frozenset({"dimension"})
_TEMPORAL_TYPES = frozenset({"date", "datetime", "time"})
_NUMERIC_TYPES = frozenset({"integer", "int", "double", "real", "number", "float", "decimal"})

# Role slots and the field role each slot accepts. The measure slots take an aggregatable measure;
# the dimension slots take a categorical / temporal / geo dimension. Used by the gate to reject a
# field placed in an incompatible slot (a dimension on a value axis, a measure on a legend, ...).
_MEASURE_SLOTS = frozenset({"Y", "Y2", "X", "Values", "Size", "Angle"})
_DIMENSION_SLOTS = frozenset({"Category", "Series", "Legend", "Location", "Axis", "Rows", "Columns"})


class VizAdvisorError(ValueError):
    """Raised on a malformed field spec (a name-less or role-less field)."""


def _norm_type(data_type):
    return (data_type or "").strip().lower()


def normalize_field(field):
    """Normalize one field spec dict to ``{name, role, data_type, semantic_role, cardinality}``.

    ``field`` requires ``name`` and ``role`` ("measure"/"dimension"); ``data_type`` is free text we
    lower-case; ``semantic_role`` ("geo"/"temporal") is derived from data type / name when absent;
    ``cardinality`` ("low"/"high" or an int) is optional. Raises :class:`VizAdvisorError` on a
    missing name or an unrecognized role -- the advisor must never guess a field's identity.
    """
    if not isinstance(field, dict):
        raise VizAdvisorError(f"field spec must be a dict, got {type(field).__name__}")
    name = (field.get("name") or "").strip()
    if not name:
        raise VizAdvisorError("field spec missing 'name'")
    role = (field.get("role") or "").strip().lower()
    if role not in _MEASURE_ROLES and role not in _DIMENSION_ROLES:
        raise VizAdvisorError(f"field {name!r} has unrecognized role {role!r} "
                              "(expected 'measure' or 'dimension')")
    dtype = _norm_type(field.get("data_type"))
    semantic = (field.get("semantic_role") or "").strip().lower() or None
    if semantic is None:
        if dtype in _TEMPORAL_TYPES:
            semantic = "temporal"
        elif _looks_geographic(name):
            semantic = "geo"
    card = field.get("cardinality")
    return {"name": name, "role": role, "data_type": dtype,
            "semantic_role": semantic, "cardinality": card}


# Geographic name cues, matched as WHOLE word tokens (never raw substrings). A raw-substring test
# mis-flagged common NON-geographic dimensions -- "Ethnicity"/"Capacity" embed "city", "Relationship"/
# "Inflation"/"Translation" embed "lat", "Real Estate" embeds "state", "Geometry" embeds "geo" --
# which would route them to a confident (and wrong) map, breaking the warn-never-wrong contract.
# Tokenizing the name on separators AND camelCase/letter-digit boundaries and matching whole tokens
# keeps a real place field geographic ("State/Province", "Postal Code", "CustomerCity", "Cities")
# while a field that merely embeds a cue is left alone. These are unprotectable facts about which
# field names denote a place; authored here, not copied.
_GEO_TOKENS = frozenset({
    "country", "countries", "state", "states", "province", "provinces",
    "city", "cities", "region", "regions", "county", "counties",
    "postal", "postcode", "zip", "zipcode",
    "lat", "latitude", "lng", "longitude", "geo", "geography",
})

_WORD_TOKEN_RE = re.compile(r"[A-Za-z]+|[0-9]+")


def _name_tokens(name):
    """Lower-case word tokens of ``name``, split on separators and camelCase / letter-digit seams."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    return [t.lower() for t in _WORD_TOKEN_RE.findall(spaced)]


def _looks_geographic(name):
    return any(tok in _GEO_TOKENS for tok in _name_tokens(name))


def _is_low_cardinality(field):
    """``True`` when a dimension is small enough for a part-to-whole / clustered chart.

    ``cardinality`` may be an int (<= 8 is low) or the strings "low"/"high"; unknown -> not low (so
    the advisor degrades to a bar/table rather than over-promising a pie on a high-card field)."""
    card = field.get("cardinality")
    if isinstance(card, bool):
        return False
    if isinstance(card, (int, float)):
        return card <= 8
    if isinstance(card, str):
        return card.strip().lower() == "low"
    return False


def classify_fields(fields):
    """Summarize a normalized field list into the signals the rule table switches on."""
    norm = [normalize_field(f) for f in (fields or [])]
    measures = [f for f in norm if f["role"] in _MEASURE_ROLES]
    dims = [f for f in norm if f["role"] in _DIMENSION_ROLES]
    temporal = [f for f in dims if f["semantic_role"] == "temporal"]
    geo = [f for f in dims if f["semantic_role"] == "geo"]
    plain_dims = [f for f in dims if f["semantic_role"] not in ("temporal", "geo")]
    return {
        "fields": norm,
        "measures": measures,
        "dimensions": dims,
        "temporal": temporal,
        "geo": geo,
        "plain_dimensions": plain_dims,
        "n_measures": len(measures),
        "n_dimensions": len(dims),
    }


# -- recommendation rule table -------------------------------------------------------------------
def _spec(visual_type, encodings, confidence, reasoning):
    return {"visual_type": visual_type, "encodings": encodings,
            "confidence": round(float(confidence), 3), "reasoning": reasoning}


def _names(fields):
    return [f["name"] for f in fields]


def _recommend_specs(c):
    """The authored deterministic rule table: a classification summary ``c`` -> list of candidate
    specs (unranked). Each branch states WHY in the reasoning string. A faithful table is always
    appended as the universal fallback so the result is never empty."""
    out = []
    measures, dims = c["measures"], c["dimensions"]
    temporal, geo, plain = c["temporal"], c["geo"], c["plain_dimensions"]
    nm, nd = c["n_measures"], c["n_dimensions"]
    m_names = _names(measures)

    # No dimension: a single KPI card, or a multi-row card for several measures.
    if nd == 0 and nm >= 1:
        if nm == 1:
            out.append(_spec("card", {"Values": m_names[:1]}, 0.92,
                             f"A single measure ({m_names[0]}) with no dimension is a KPI card."))
        else:
            out.append(_spec("multiRowCard", {"Values": m_names}, 0.88,
                             f"{nm} measures with no dimension list cleanly in a multi-row card."))

    # Geographic dimension + measure: choropleth (shape map) or symbol map.
    if geo and nm >= 1:
        g = geo[0]["name"]
        out.append(_spec("shapeMap", {"Location": [g], "Values": m_names[:1]}, 0.80,
                         f"A geographic dimension ({g}) shaded by {m_names[0]} is a choropleth."))
        out.append(_spec("map", {"Location": [g], "Size": m_names[:1]}, 0.62,
                         f"A bubble map sizes {m_names[0]} at each {g}."))

    # Temporal dimension + measure(s): a trend line (area as the filled alt).
    if temporal and nm >= 1:
        t = temporal[0]["name"]
        out.append(_spec("lineChart", {"Axis": [t], "Y": m_names}, 0.90,
                         f"A measure over a date dimension ({t}) is a trend line."))
        if nm == 1:
            out.append(_spec("areaChart", {"Axis": [t], "Y": m_names[:1]}, 0.60,
                             f"An area chart fills the {m_names[0]} trend over {t}."))
        out.append(_spec("clusteredColumnChart", {"Category": [t], "Y": m_names}, 0.52,
                         f"Discrete time buckets of {t} can also read as columns."))

    # One plain categorical dimension + one measure: column (with a bar / pie alt).
    if plain and nm == 1 and not temporal and not geo:
        d = plain[0]
        dn, y = d["name"], m_names[:1]
        if nd == 1:
            out.append(_spec("clusteredColumnChart", {"Category": [dn], "Y": y}, 0.80,
                             f"One category ({dn}) by one measure ({y[0]}) compares as columns."))
            out.append(_spec("clusteredBarChart", {"Category": [dn], "Y": y}, 0.72,
                             f"A horizontal bar suits long or many {dn} labels."))
            if _is_low_cardinality(d):
                out.append(_spec("pieChart", {"Legend": [dn], "Y": y}, 0.55,
                                 f"With few {dn} values, a pie shows part-to-whole of {y[0]}."))

    # One categorical dimension + several measures: grouped columns or a combo.
    if plain and nm >= 2 and not temporal and not geo and nd == 1:
        dn = plain[0]["name"]
        out.append(_spec("clusteredColumnChart", {"Category": [dn], "Y": m_names}, 0.70,
                         f"One category ({dn}) across {nm} measures groups as clustered columns."))
        out.append(_spec("lineClusteredColumnComboChart",
                         {"Category": [dn], "Y": m_names[:1], "Y2": m_names[1:]}, 0.58,
                         "Mixed-scale measures read better as a column + line combo."))

    # Two dimensions + one measure: a cross-tab matrix, or stacked columns by the 2nd dim as series.
    if nd == 2 and nm == 1 and not geo:
        d1, d2 = dims[0]["name"], dims[1]["name"]
        out.append(_spec("pivotTable", {"Rows": [d1], "Columns": [d2], "Values": m_names[:1]}, 0.74,
                         f"Two dimensions ({d1} x {d2}) by one measure cross-tab as a matrix."))
        out.append(_spec("stackedColumnChart", {"Category": [d1], "Series": [d2], "Y": m_names[:1]},
                         0.64, f"Stacked columns show {m_names[0]} of {d1} split by {d2}."))

    # Two measures (a relationship) optionally detailed by a dimension: a scatter plot.
    if nm == 2 and nd <= 1:
        enc = {"X": m_names[:1], "Y": m_names[1:2]}
        detail = dims[0]["name"] if dims else None
        if detail:
            enc["Details"] = [detail]
        out.append(_spec("scatterChart", enc, 0.70,
                         f"Two measures ({m_names[0]} vs {m_names[1]}) relate on a scatter plot"
                         + (f", one point per {detail}." if detail else ".")))

    # Universal faithful fallback: an exact detail table (always valid, low confidence).
    all_names = _names(c["fields"])
    if all_names:
        out.append(_spec("tableEx", {"Values": all_names}, 0.40,
                         "A detail table faithfully lists every selected field."))
    return out


def recommend_visuals(fields, *, intent=None, max_suggestions=6):
    """Return a ranked list of candidate visualizations for ``fields`` (deterministic; no LLM).

    ``fields`` is a list of field-spec dicts (see :func:`normalize_field`). ``intent`` is an optional
    free-text hint recorded for the out-of-band pass (it does NOT change the deterministic ranking).
    Each result is ``{visual_type, encodings, confidence, reasoning, rank}`` sorted by confidence
    (then a stable tie-break on the chart name), capped at ``max_suggestions``. Always returns at
    least one entry when at least one field is supplied (the faithful-table fallback).
    """
    c = classify_fields(fields)
    specs = _recommend_specs(c)
    # De-dup by (visual_type, encodings) keeping the highest confidence, then rank.
    best = {}
    for s in specs:
        key = (s["visual_type"], json.dumps(s["encodings"], sort_keys=True))
        if key not in best or s["confidence"] > best[key]["confidence"]:
            best[key] = s
    ranked = sorted(best.values(), key=lambda s: (-s["confidence"], s["visual_type"]))
    ranked = ranked[:max_suggestions]
    for i, s in enumerate(ranked, 1):
        s["rank"] = i
    return ranked


# -- out-of-band agent assist (bundle + prompt; never calls a model) -----------------------------
def build_advice_bundle(fields, *, intent=None, max_suggestions=6):
    """Assemble the additive, JSON-serializable advice request the agent / vision pass consumes.

    Carries the read-only field truth, the deterministic ranked candidates (``top_pick`` = the
    highest-confidence one), the hard rules, and a pre-filled answer template. Never mutates input.
    """
    candidates = recommend_visuals(fields, intent=intent, max_suggestions=max_suggestions)
    norm = classify_fields(fields)["fields"]
    return {
        "version": ADVISOR_VERSION,
        "kind": ADVISOR_BUNDLE_KIND,
        "rules": list(ADVISOR_RULES),
        "intent": intent,
        "fields": norm,
        "summary": {
            "fields": len(norm),
            "candidates": len(candidates),
            "top_pick": candidates[0]["visual_type"] if candidates else None,
        },
        "candidates": candidates,
        "answer": {"chosen_index": None, "reason": ""},
    }


def advice_prompt(bundle):
    """Render the runbook prompt for the out-of-band advice pass (no API key, no tool call)."""
    lines = [
        "You are the Tableau -> Power BI viz advisor (Tier-2). Given the fields and the "
        "deterministic candidate charts below, pick the BEST chart for the data and the stated "
        "intent, choosing ONLY from the candidate list, or keep the deterministic top pick.",
        "",
        "Hard rules:",
    ]
    for i, rule in enumerate(bundle.get("rules", []), 1):
        lines.append(f"  {i}. {rule}")
    intent = bundle.get("intent")
    lines.append("")
    lines.append(f"Intent: {intent!r}" if intent else "Intent: (none given)")
    lines.append("")
    lines.append("Fields (READ-ONLY truth -- never change):")
    for f in bundle.get("fields", []):
        extra = []
        if f.get("semantic_role"):
            extra.append(f["semantic_role"])
        if f.get("cardinality") is not None:
            extra.append(f"card={f['cardinality']}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"  - {f['name']}: {f['role']}/{f['data_type'] or '?'}{suffix}")
    lines.append("")
    lines.append("Candidate charts (choose at most one by index, or keep top_pick):")
    for c in bundle.get("candidates", []):
        lines.append(f"  [{c['rank'] - 1}] {c['visual_type']} (confidence={c['confidence']}) "
                     f"encodings={json.dumps(c['encodings'])}")
        lines.append(f"        why: {c['reasoning']}")
    lines.append("")
    lines.append(
        'Respond with one JSON object: {"chosen_index": <a candidate index, or null to keep the '
        'deterministic top_pick>, "reason": <short data-grounded justification>}.'
    )
    return "\n".join(lines)


# -- gated landing -------------------------------------------------------------------------------
def _field_role_index(fields):
    """``{name: role}`` from a (possibly un-normalized) field list."""
    out = {}
    for f in fields or []:
        try:
            nf = normalize_field(f)
        except VizAdvisorError:
            continue
        out[nf["name"]] = nf["role"]
    return out


def validate_suggestion(suggestion, fields, candidates):
    """``(ok, reason)`` -- a suggestion is acceptable ONLY when it is a faithful, bindable chart.

    Accepts iff: the ``visual_type`` is one of the deterministic ``candidates`` AND in the closed
    :data:`PBIR_VISUAL_TYPES` vocabulary; AND every encoded field exists in ``fields`` in a
    role-compatible slot (a measure on a measure slot, a dimension on a dimension slot). Otherwise
    the deterministic pick must stand (warn-never-wrong).
    """
    if not isinstance(suggestion, dict):
        return False, "suggestion is not an object"
    vtype = suggestion.get("visual_type")
    cand_types = {c.get("visual_type") for c in (candidates or [])}
    if vtype not in cand_types:
        return False, f"chart type {vtype!r} is not in the candidate set {sorted(cand_types)}"
    if vtype not in PBIR_VISUAL_TYPES:
        return False, f"chart type {vtype!r} is not a known PBIR visual type"
    roles = _field_role_index(fields)
    for slot, slot_fields in (suggestion.get("encodings") or {}).items():
        for name in slot_fields:
            if name not in roles:
                return False, f"field {name!r} is not in the provided field set"
            role = roles[name]
            if slot in _MEASURE_SLOTS and role not in _MEASURE_ROLES:
                return False, f"field {name!r} ({role}) is not a measure but sits on slot {slot!r}"
            if slot in _DIMENSION_SLOTS and role not in _DIMENSION_ROLES:
                return False, f"field {name!r} ({role}) is not a dimension but sits on slot {slot!r}"
    return True, "ok"


def apply_advice(candidates, answer, fields):
    """Resolve the agent's ``answer`` against the deterministic ``candidates``; return ``(chosen, report)``.

    ``answer`` is ``{chosen_index, reason}`` (``chosen_index`` null/0 keeps the top pick). The chosen
    candidate is ACCEPTED only when :func:`validate_suggestion` passes; otherwise the deterministic
    top pick (``candidates[0]``) stands and the attempt is recorded as rejected. ``candidates`` is
    never mutated. Returns the chosen spec (or ``None`` when there were no candidates) plus an
    audit report ``{applied, kept, rejected}``.
    """
    report = {"applied": [], "kept": [], "rejected": []}
    if not candidates:
        return None, report
    top = candidates[0]
    answer = answer or {}
    idx = answer.get("chosen_index")
    reason = answer.get("reason")

    if idx is None or idx == 0:
        report["kept"].append({"visual_type": top["visual_type"],
                               "kept_because": "keeps the deterministic top pick"})
        return top, report

    if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(candidates)):
        report["rejected"].append({"chosen_index": idx,
                                   "rejected_because": "index out of range"})
        report["kept"].append({"visual_type": top["visual_type"],
                               "kept_because": "fell back to the deterministic top pick"})
        return top, report

    chosen = candidates[idx]
    ok, why = validate_suggestion(chosen, fields, candidates)
    if not ok:
        report["rejected"].append({"chosen_index": idx, "visual_type": chosen.get("visual_type"),
                                   "rejected_because": why})
        report["kept"].append({"visual_type": top["visual_type"],
                               "kept_because": "fell back to the deterministic top pick"})
        return top, report

    report["applied"].append({"from_type": top["visual_type"], "to_type": chosen["visual_type"],
                              "chosen_index": idx, "reason": reason})
    return chosen, report


def refine_with_feedback(candidates, feedback):
    """Re-rank ``candidates`` from per-type user ``feedback`` (the multi-turn hook; deterministic).

    ``feedback`` is ``{visual_type: "up"|"down"}``. An "up" nudges a type's confidence up, a "down"
    nudges it down; the list is re-sorted and re-ranked. Confidence stays clamped to ``[0, 1]`` and
    unknown types are ignored. Purely additive -- never changes a chart's encodings or invents a
    type. Returns a new, re-ranked list (input untouched).
    """
    delta = {"up": 0.15, "down": -0.25}
    out = []
    for c in candidates or []:
        nc = dict(c)
        nc["encodings"] = dict(c.get("encodings") or {})
        adj = delta.get((feedback or {}).get(c.get("visual_type")))
        if adj is not None:
            nc["confidence"] = round(min(1.0, max(0.0, nc.get("confidence", 0.0) + adj)), 3)
        out.append(nc)
    out.sort(key=lambda s: (-s.get("confidence", 0.0), s.get("visual_type", "")))
    for i, s in enumerate(out, 1):
        s["rank"] = i
    return out


# -- pipeline integration: advice from the deterministic viz candidate records -------------------
# The viz engine emits a read-only candidate record per main visual (twb_to_pbir
# ``ir["candidate_records"]``: ``{visual_type, fields: {slot: [queryRef]}, ...}``). This bridge turns
# that field truth into advisor input so an estate run can emit an OPT-IN ``viz-advice.json`` sidecar
# of ranked ALTERNATIVE charts per already-rebuilt visual. It is the advisor's pipeline seam --
# purely additive (nothing is written into the PBIR), and it never proposes a rebinding: it only
# re-ranks chart TYPES over a visual's EXISTING fields, gated to faithful, role-compatible options.
ADVICE_SIDECAR_KIND = "tableau-to-powerbi-viz-advice"


def _slot_role(slot):
    """``"measure"`` / ``"dimension"`` for a known PBIR slot, else ``None`` (an ambiguous well)."""
    if slot in _MEASURE_SLOTS:
        return "measure"
    if slot in _DIMENSION_SLOTS:
        return "dimension"
    return None


def _ref_field_name(query_ref):
    """``"Orders.Sales" -> "Sales"``; tolerate a measure ref that is already bare."""
    if not isinstance(query_ref, str):
        return ""
    return query_ref.rsplit(".", 1)[-1].strip()


def fields_from_candidate_record(record, *, field_types=None):
    """``(fields, reason)`` -- advisor field specs from a candidate record's slot -> queryRef map.

    Role is inferred from the slot (a measure slot vs a dimension slot); ``field_types`` (a
    ``{queryRef: data_type}`` map) refines temporal / geo detection when the model types are known.
    Returns ``(None, reason)`` when a bound slot is role-ambiguous or no field is bound -- the
    advisor must never re-role a field it cannot place (warn-never-wrong).
    """
    fields_map = (record or {}).get("fields") or {}
    types = field_types or {}
    specs, seen = [], set()
    for slot, refs in fields_map.items():
        role = _slot_role(slot)
        if role is None:
            return None, f"slot {slot!r} has no unambiguous field role"
        for ref in refs or []:
            name = _ref_field_name(ref)
            if not name or name in seen:
                continue
            seen.add(name)
            specs.append({"name": name, "role": role, "data_type": types.get(ref)})
    if not specs:
        return None, "no bound fields to advise on"
    return specs, None


def advise_for_candidate_record(record, *, intent=None, field_types=None, max_alternatives=4):
    """One advice entry for a rebuilt visual: ranked ALTERNATIVE chart types for its same fields.

    Never proposes a rebinding -- it re-ranks chart types over the visual's existing field truth and
    drops the type already chosen. ``advisable`` is ``False`` (with a ``reason``, no suggestions)
    whenever the field roles cannot be reliably recovered -- e.g. the universal detail table mixes
    measure and dimension fields in one ``Values`` well, so a re-rank would be guesswork.
    """
    record = record or {}
    current = record.get("visual_type")
    entry = {
        "page": record.get("page"),
        "visual": record.get("visual"),
        "worksheet": record.get("worksheet"),
        "current_type": current,
    }
    if current == "tableEx":
        entry.update(advisable=False,
                     reason="a detail table mixes measure and dimension fields in one well; "
                            "field roles cannot be reliably recovered for a re-rank")
        return entry
    fields, reason = fields_from_candidate_record(record, field_types=field_types)
    if fields is None:
        entry.update(advisable=False, reason=reason)
        return entry
    try:
        ranked = recommend_visuals(fields, intent=intent, max_suggestions=max_alternatives + 2)
    except VizAdvisorError as exc:
        entry.update(advisable=False, reason=str(exc))
        return entry
    alternatives = [r for r in ranked if r["visual_type"] != current][:max_alternatives]
    entry.update(advisable=True, fields=fields,
                 top_alternative=alternatives[0]["visual_type"] if alternatives else None,
                 suggestions=alternatives)
    return entry


def build_report_advice(candidate_records, *, intent=None, field_types=None):
    """The opt-in ``viz-advice.json`` artifact: per-visual ranked chart alternatives (additive).

    Consumes the viz engine's read-only candidate records and returns a JSON-serializable sidecar
    body. It is purely advisory -- nothing here is written into the PBIR definition, so the rebuilt
    report is byte-for-byte identical whether or not the advice is produced.
    """
    advice = [advise_for_candidate_record(r, intent=intent, field_types=field_types)
              for r in (candidate_records or [])]
    advisable = [a for a in advice if a.get("advisable")]
    return {
        "version": ADVISOR_VERSION,
        "kind": ADVICE_SIDECAR_KIND,
        "intent": intent,
        "rules": list(ADVISOR_RULES),
        "summary": {
            "visuals": len(advice),
            "advisable": len(advisable),
            "with_alternative": sum(1 for a in advisable if a.get("top_alternative")),
        },
        "advice": advice,
    }
