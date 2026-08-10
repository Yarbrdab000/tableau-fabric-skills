"""A mark-label slot holding MUTUALLY EXCLUSIVE measures collapses to one COALESCE value.

Tableau lets one mark label stack several measures in a single display position: exactly one is
non-blank in any period and its COLOUR carries the direction (green up / red down / grey flat).
Projected as siblings on a Power BI card they become one real row plus N-1 rows reading
``(Blank)`` -- which is exactly what the reference workbook rendered.

``_parse_label_slots`` (already shipped, previously unwired) says which pills share a position;
this wires that answer into the card's ``queryState``: hide the members, project one
``NativeVisualCalculation`` COALESCE over them, and move the authored number format onto it.

TWO INDEPENDENT GUARDS, both required and both structural -- **no name matching anywhere**:

  1. every member must be VALUE-kind, and
  2. the members must carry >= 2 DISTINCT font colours.

Both were chosen by measuring the reference workbook rather than by reasoning: all 14 genuine
delta groups are 3-colour MEASURE groups, and all 7 rejected groups are single-colour DIMENSION
groups (an aircraft tile pairing a label calc with ``aircraft_type``; a summary tile stacking
Distance, Hub City and Hub Country -- three different facts that must all keep rendering).

The asymmetry that justifies the stricter AND: a wrong collapse LOSES data, whereas a declined
collapse merely leaves today's behaviour in place. So this fails closed on every doubt.
"""

import pytest
import xml.etree.ElementTree as ET

from scripts.twb_to_pbir import (
    _collapse_label_slot_projections,
    _parse_encodings,
    _role_projections,
    _slot_group_name,
)


GREEN, RED, GREY = "#49964f", "#e63946", "#b4b4b4"


def _field(caption, token, kind="value"):
    return {"caption": caption, "kind": kind, "label_token": token}


def _proj(nref, fmt=None):
    p = {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}},
                              "Property": nref}},
         "queryRef": "_Measures.%s" % nref, "nativeQueryRef": nref}
    if fmt:
        p["format"] = fmt
    return p


def _slot(tokens, colors):
    return {"tokens": tokens, "colors": colors, "bold": False, "size": None, "text": ""}


DS = "federated.1abc"


def _tok(name):
    return "[%s].[usr:%s:qk]" % (DS, name)


def _member(caption, nref=None, kind="value", fmt=None):
    """A (field, projection) pair as ``_role_projections(pairs_out=...)`` hands it over."""
    nref = nref or caption
    return _field(caption, (DS, "usr:%s:qk" % caption), kind), _proj(nref, fmt)


def _build(captions, colors, kinds=None, fmts=None, extra=()):
    """Return ``(pairs, projections, slots)`` for a single slot over ``captions``."""
    kinds = kinds or ["value"] * len(captions)
    fmts = fmts or [None] * len(captions)
    pairs = [_member(c, kind=k, fmt=f) for c, k, f in zip(captions, kinds, fmts)]
    pairs = list(extra) + pairs
    projections = [p for _, p in pairs]
    slots = [_slot([_tok(c) for c in captions], colors)]
    return pairs, projections, slots


def _ws(slots):
    return {"label_slots": slots, "fidelity_note": None}


def _coalesce(projections):
    for p in projections:
        nvc = (p.get("field") or {}).get("NativeVisualCalculation")
        if nvc and "COALESCE" in nvc["Expression"]:
            return p
    return None


# --------------------------------------------------------------------------------------------
# The real shape: the reference workbook's Pos / Neg / Neut delta trio.
# --------------------------------------------------------------------------------------------

TRIO = ["Pos MoM Total Revenue", "Neg MoM Total Revenue", "Neut MoM Total Revenue"]
TRI_COLORS = [GREEN, RED, GREY]


def test_a_three_measure_slot_collapses_to_one_coalesce():
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    vc = _coalesce(out)
    assert vc is not None
    assert vc["field"]["NativeVisualCalculation"]["Expression"] == (
        "COALESCE([Pos MoM Total Revenue], [Neg MoM Total Revenue], "
        "[Neut MoM Total Revenue])")


def test_every_member_of_a_collapsed_slot_is_hidden():
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    bases = [p for p in out if not (p.get("field") or {}).get("NativeVisualCalculation")]
    assert bases and all(p.get("hidden") is True for p in bases)


def test_the_collapse_adds_exactly_one_projection():
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert len(out) == len(projs) + 1


def test_no_member_projection_is_removed():
    """Hidden, never dropped -- the COALESCE references them by nativeQueryRef, so removing a
    base would leave the DAX dangling and error the visual."""
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    refs = {p.get("nativeQueryRef") for p in out}
    assert set(TRIO) <= refs


def test_the_calculation_is_named_from_what_the_members_share():
    """Naming it after one member would claim a direction the card may not be showing."""
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert _coalesce(out)["nativeQueryRef"] == "MoM Total Revenue"


def test_the_calculation_name_matches_its_native_query_ref():
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    vc = _coalesce(out)
    assert vc["field"]["NativeVisualCalculation"]["Name"] == vc["nativeQueryRef"]


def test_the_calculation_declares_dax():
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert _coalesce(out)["field"]["NativeVisualCalculation"]["Language"] == "dax"


def test_the_calculation_carries_a_visual_calc_query_ref():
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert _coalesce(out)["queryRef"] == "select_vc0"


# --------------------------------------------------------------------------------------------
# GUARD 1 -- value-kind only. A dimension slot stacks genuinely DIFFERENT facts.
# --------------------------------------------------------------------------------------------

def test_a_dimension_slot_never_collapses():
    """The real ``Airline Summary 2`` tile: Distance + Hub City + Hub Country, all shown."""
    caps = ["CM Distance (A)", "CM Hub City (A)", "CM Hub Country (A)"]
    pairs, projs, slots = _build(caps, [GREEN, RED, GREY], kinds=["category"] * 3)
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert out == projs
    assert not any(p.get("hidden") for p in out)


def test_one_dimension_member_poisons_the_whole_slot():
    """The real aircraft tile pairs a label CALC with ``aircraft_type``. Mixed => decline."""
    pairs, projs, slots = _build(["Calculation_1527", "aircraft_type"], [GREEN, RED],
                                 kinds=["value", "category"])
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert _coalesce(out) is None
    assert not any(p.get("hidden") for p in out)


# --------------------------------------------------------------------------------------------
# GUARD 2 -- direction colouring. Same colour => we cannot tell alternatives from siblings.
# --------------------------------------------------------------------------------------------

def test_a_single_colour_measure_slot_never_collapses():
    pairs, projs, slots = _build(TRIO, [GREY, GREY, GREY])
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert out == projs


def test_two_distinct_colours_are_enough():
    pairs, projs, slots = _build(TRIO, [GREEN, RED, RED])
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert _coalesce(out) is not None


def test_a_missing_colour_does_not_count_towards_the_two():
    pairs, projs, slots = _build(TRIO, [GREEN, None, None])
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert out == projs


# --------------------------------------------------------------------------------------------
# Arity / structural preconditions.
# --------------------------------------------------------------------------------------------

def test_a_single_field_slot_is_left_alone():
    pairs, projs, slots = _build(["CM Total Revenue"], [GREEN])
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert out == projs


def test_a_static_caption_slot_is_left_alone():
    pairs, projs, _ = _build(["CM Total Revenue"], [GREEN])
    slots = [{"tokens": [], "colors": [], "bold": False, "size": None, "text": "All Passengers"}]
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert out == projs


def test_a_worksheet_with_no_template_is_left_alone():
    pairs, projs, _ = _build(TRIO, TRI_COLORS)
    out = _collapse_label_slot_projections(pairs, projs, _ws(None))
    assert out == projs


def test_no_projections_is_left_alone():
    _, _, slots = _build(TRIO, TRI_COLORS)
    assert _collapse_label_slot_projections([], [], _ws(slots)) == []


def test_a_slot_whose_members_did_not_project_is_left_alone():
    """A pill the template names but the card never bound cannot be collapsed."""
    pairs, projs, _ = _build(["CM Total Revenue"], [GREEN])
    slots = [_slot([_tok("Absent A"), _tok("Absent B")], [GREEN, RED])]
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert out == projs


def test_only_one_of_the_slot_members_projected_declines():
    pairs, projs, _ = _build(["Pos MoM RPK"], [GREEN])
    slots = [_slot([_tok("Pos MoM RPK"), _tok("Neg MoM RPK")], [GREEN, RED])]
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert out == projs


# --------------------------------------------------------------------------------------------
# Format, ordering, and neighbours.
# --------------------------------------------------------------------------------------------

ARROWS = "\u25b20.0%;\u25bc0.0%"


def test_the_authored_format_moves_onto_the_calculation():
    """The arrows ARE the number format; they belong to the value that is actually SHOWN."""
    pairs, projs, slots = _build(TRIO, TRI_COLORS, fmts=[ARROWS] * 3)
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert _coalesce(out)["format"] == ARROWS


def test_the_format_is_taken_from_the_first_member_that_has_one():
    pairs, projs, slots = _build(TRIO, TRI_COLORS, fmts=[None, ARROWS, "0.0%"])
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert _coalesce(out)["format"] == ARROWS


def test_an_unformatted_group_yields_an_unformatted_calculation():
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert "format" not in _coalesce(out)


def test_the_calculation_lands_where_its_group_began():
    """The card must keep the author's slot order, not append the delta at the end."""
    big = _member("CM Total Revenue")
    pairs, projs, slots = _build(TRIO, TRI_COLORS, extra=[big])
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert out[0]["nativeQueryRef"] == "CM Total Revenue"
    assert out[1] is _coalesce(out)


def test_a_projection_outside_the_slot_is_untouched():
    big = _member("CM Total Revenue")
    pairs, projs, slots = _build(TRIO, TRI_COLORS, extra=[big])
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    kept = [p for p in out if p["nativeQueryRef"] == "CM Total Revenue"]
    assert len(kept) == 1 and "hidden" not in kept[0]


def test_two_slots_on_one_card_each_collapse_independently():
    a = ["Pos MoM RPK", "Neg MoM RPK"]
    b = ["Pos MoM CTK", "Neg MoM CTK"]
    pairs = [_member(c) for c in a + b]
    projs = [p for _, p in pairs]
    slots = [_slot([_tok(c) for c in a], [GREEN, RED]),
             _slot([_tok(c) for c in b], [GREEN, RED])]
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    vcs = [p for p in out
           if (p.get("field") or {}).get("NativeVisualCalculation")]
    assert [v["nativeQueryRef"] for v in vcs] == ["MoM RPK", "MoM CTK"]
    assert [v["queryRef"] for v in vcs] == ["select_vc0", "select_vc1"]


# --------------------------------------------------------------------------------------------
# The audit trail.
# --------------------------------------------------------------------------------------------

def test_a_collapse_records_a_fidelity_note():
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    ws = _ws(slots)
    _collapse_label_slot_projections(pairs, projs, ws)
    assert ws["fidelity_note"] and "COALESCE" in ws["fidelity_note"]


def test_the_note_names_the_measures_it_collapsed():
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    ws = _ws(slots)
    _collapse_label_slot_projections(pairs, projs, ws)
    assert all(c in ws["fidelity_note"] for c in TRIO)


def test_a_declined_slot_records_nothing():
    pairs, projs, slots = _build(TRIO, [GREY, GREY, GREY])
    ws = _ws(slots)
    _collapse_label_slot_projections(pairs, projs, ws)
    assert ws["fidelity_note"] is None


def test_an_existing_note_is_preserved():
    """The collapse is not the only thing that may have reshaped this worksheet."""
    pairs, projs, slots = _build(TRIO, TRI_COLORS)
    ws = _ws(slots)
    ws["fidelity_note"] = "dual-axis combo"
    _collapse_label_slot_projections(pairs, projs, ws)
    assert ws["fidelity_note"].startswith("dual-axis combo; ")


# --------------------------------------------------------------------------------------------
# Name derivation.
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("captions,expected", [
    (["Pos MoM RPK", "Neg MoM RPK"], "MoM RPK"),
    (["Pos MoM RPK (A)", "Neg MoM RPK (A)", "Neut MoM RPK (A)"], "MoM RPK (A)"),
    (["Pos Delta", "Neg Delta"], "Delta"),
])
def test_the_shared_words_become_the_name(captions, expected):
    assert _slot_group_name(captions, set()) == expected


def test_members_sharing_no_word_fall_back_to_the_first_caption():
    assert _slot_group_name(["Alpha", "Beta"], set()) == "Alpha"


def test_the_name_keeps_the_first_members_word_order():
    assert _slot_group_name(["Up Load Factor MoM", "Down MoM Load Factor"],
                            set()) == "Load Factor MoM"


def test_a_name_that_would_collide_is_uniquified():
    """A collision would make the COALESCE's own DAX reference ambiguous."""
    assert _slot_group_name(["Pos MoM RPK", "Neg MoM RPK"], {"MoM RPK"}) == "MoM RPK 2"


def test_uniquifying_keeps_climbing_past_several_collisions():
    taken = {"MoM RPK", "MoM RPK 2", "MoM RPK 3"}
    assert _slot_group_name(["Pos MoM RPK", "Neg MoM RPK"], taken) == "MoM RPK 4"


def test_a_calculation_name_cannot_collide_with_a_base_measure():
    """A base measure literally named after the shared words must not be shadowed."""
    pairs = [_member("MoM RPK")] + [_member(c) for c in ["Pos MoM RPK", "Neg MoM RPK"]]
    projs = [p for _, p in pairs]
    slots = [_slot([_tok("Pos MoM RPK"), _tok("Neg MoM RPK")], [GREEN, RED])]
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert _coalesce(out)["nativeQueryRef"] == "MoM RPK 2"


def test_captions_that_are_all_empty_decline():
    assert _slot_group_name(["", ""], set()) is None


# --------------------------------------------------------------------------------------------
# Token matching -- the template addresses pills by TOKEN, not by caption or position.
# --------------------------------------------------------------------------------------------

def test_a_field_with_no_label_token_is_never_collapsed():
    """A pill that reached the card from Rows/Cols rather than the Text shelf carries no token,
    so the template cannot claim it. Fail closed rather than match by name."""
    pairs = [(dict(f, label_token=None), p) for f, p in
             (_member(c) for c in TRIO)]
    projs = [p for _, p in pairs]
    slots = [_slot([_tok(c) for c in TRIO], TRI_COLORS)]
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    assert out == projs


def test_matching_is_by_token_not_by_caption():
    """Tableau keeps a copied field's ORIGINAL internal name, so the token and the caption
    routinely disagree -- the reference workbook's ``RPK BAN`` is exactly this case."""
    pairs = [(_field(cap, (DS, "usr:%s:qk" % internal)), _proj(cap))
             for cap, internal in (("Pos MoM RPK", "Pos MoM On-Time Perf (copy)_1"),
                                   ("Neg MoM RPK", "Pos MoM RPK (copy)_2"))]
    projs = [p for _, p in pairs]
    slots = [_slot(["[%s].[usr:Pos MoM On-Time Perf (copy)_1:qk]" % DS,
                    "[%s].[usr:Pos MoM RPK (copy)_2:qk]" % DS], [GREEN, RED])]
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    vc = _coalesce(out)
    assert vc is not None
    assert vc["field"]["NativeVisualCalculation"]["Expression"] == (
        "COALESCE([Pos MoM RPK], [Neg MoM RPK])")


# --------------------------------------------------------------------------------------------
# THE TWO SEAMS THAT FEED THIS. Both were found by mutation: the unit tests above build their
# ``pairs`` by hand, so severing either seam left the feature DEAD end to end while every test
# still passed -- the exact failure mode that made the number-format work emit 0 of 243.
# --------------------------------------------------------------------------------------------

def _pane(pills):
    pane = ET.Element("pane")
    holder = ET.SubElement(pane, "encodings")
    for tag, column in pills:
        ET.SubElement(holder, tag, {"column": column})
    return pane


def _parse(pills, ds=DS):
    """Run the REAL ``_parse_encodings`` so the token stamp is exercised, not simulated."""
    ids = [c.split("].[")[-1].strip("[]") for _, c in pills]
    base_cols = {(ds, i): {"caption": i, "datatype": "real", "role": "measure",
                           "type": "quantitative", "is_calc": False, "formula": None,
                           "table": "Facts", "hidden": False} for i in ids}
    index = {(ds, i): {"entity": "Facts", "property": i, "caption": i,
                       "datatype": "real", "role": "measure", "table": "Facts"} for i in ids}
    return _parse_encodings(_pane(pills), ds, base_cols, {}, index, {ds: "Facts"}, "BAN", [])


def test_parse_encodings_stamps_the_source_token_on_every_label_pill():
    """SEAM 1. Without this stamp the template can never be matched back to a resolved field,
    and every collapse silently declines."""
    pills = [("text", "[%s].[Pos MoM RPK]" % DS), ("text", "[%s].[Neg MoM RPK]" % DS)]
    enc = _parse(pills)
    assert [f["label_token"] for f in enc["label_fields"]] == [
        (DS, "Pos MoM RPK"), (DS, "Neg MoM RPK")]


def test_the_stamped_token_is_the_key_the_template_uses():
    """The stamp must be ``_split_token``'s own output, or matching fails on spelling alone."""
    from scripts.twb_to_pbir import _split_token
    enc = _parse([("text", "[%s].[Pos MoM RPK]" % DS)])
    assert enc["label_fields"][0]["label_token"] == _split_token("[%s].[Pos MoM RPK]" % DS)


def test_role_projections_pairs_each_field_with_the_projection_it_became():
    """SEAM 2. ``pairs_out`` is how the collapse addresses a field's projection without
    re-deriving the skip rules; unpopulated, nothing is ever collapsible."""
    fields = [{"entity": "_Measures", "property": n, "binding": "measure", "aggregation": None,
               "caption": n, "kind": "value"} for n in ("Pos MoM RPK", "Neg MoM RPK")]
    pairs = []
    projs = _role_projections(fields, None, None, set(), pairs_out=pairs)
    assert len(pairs) == len(projs)
    assert all(p is q for (_, p), q in zip(pairs, projs))
    assert [f["caption"] for f, _ in pairs] == ["Pos MoM RPK", "Neg MoM RPK"]


def test_role_projections_without_pairs_out_is_unchanged():
    """Purely an OUT parameter: every existing caller must be byte-for-byte unaffected."""
    fields = [{"entity": "_Measures", "property": "X", "binding": "measure",
               "aggregation": None, "caption": "X", "kind": "value"}]
    assert (_role_projections(fields, None, None, set())
            == _role_projections(fields, None, None, set(), pairs_out=[]))


def test_a_skipped_field_is_not_paired():
    """A field that produces no projection must not shift the pairing off by one."""
    fields = [{"entity": "_Measures", "property": "", "binding": "measure", "aggregation": None,
               "caption": "spacer", "kind": "value"},
              {"entity": "_Measures", "property": "Real", "binding": "measure",
               "aggregation": None, "caption": "Real", "kind": "value"}]
    pairs = []
    projs = _role_projections(fields, None, None, set(), pairs_out=pairs)
    assert len(projs) == 1
    assert [f["caption"] for f, _ in pairs] == ["Real"]


def test_the_seams_compose_into_a_collapse():
    """END TO END across both seams: parse the pills, project them, collapse the slot."""
    pills = [("text", "[%s].[Pos MoM RPK]" % DS), ("text", "[%s].[Neg MoM RPK]" % DS)]
    enc = _parse(pills)
    pairs = []
    projs = _role_projections(enc["label_fields"], None, None, set(), pairs_out=pairs)
    slots = [_slot(["[%s].[Pos MoM RPK]" % DS, "[%s].[Neg MoM RPK]" % DS], [GREEN, RED])]
    out = _collapse_label_slot_projections(pairs, projs, _ws(slots))
    vc = _coalesce(out)
    assert vc is not None and vc["nativeQueryRef"] == "MoM RPK"
    assert all(p.get("hidden") for p in out if p is not vc)
