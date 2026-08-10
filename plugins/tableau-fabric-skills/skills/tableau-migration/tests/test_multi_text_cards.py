"""Every Text/Label pill on a Tableau mark must survive into the PBIR card.

A Tableau KPI "BAN" (Big Ass Number) is ONE mark carrying MANY ``<text>`` encodings, arranged by a
``<formatted-text>`` template into a rich label: a static caption, the big number, and a set of
MUTUALLY EXCLUSIVE coloured delta measures of which exactly one is non-blank in any given period
(its colour carrying the direction -- green up, red down, grey flat).

``_parse_encodings`` collapsed that whole set with ``if enc[role] is None: enc[role] = f`` -- first
pill wins, pills 2..N silently discarded. When the live value sat in a later slot the card rendered
``(Blank)``, which reads as a broken model rather than as dropped metadata. The retention is
ADDITIVE (``label_fields`` beside ``label``) so every other role is byte-for-byte unchanged; only
the card path widens, and ``_pbir_vtype`` then resolves >=2 values to a native ``multiRowCard``.

Measured generality: this fires on any workbook using the idiom. On an unrelated Salesforce estate
it recovered 8 dropped measures across 2 cards with zero change to any other visual.
"""

import xml.etree.ElementTree as ET

import pytest

from scripts.twb_to_pbir import (
    VT_CARD,
    _build_query_state,
    _parse_encodings,
    _pbir_vtype,
)

DS = "federated.1abc"


# --------------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------------

def _meas(name, agg="Sum"):
    """A value-kind IR field as ``_resolve_field`` would produce for a measure pill."""
    return {"entity": "Facts", "property": name, "binding": "measure", "aggregation": agg,
            "caption": name, "field_id": name, "instance": "usr:%s:qk" % name,
            "kind": "value", "is_calc": False}


def _cat(name):
    return {"entity": "Facts", "property": name, "binding": "column", "aggregation": None,
            "caption": name, "field_id": name, "instance": "none:%s:nk" % name,
            "kind": "category", "is_calc": False}


def _card_ws(label_fields, label=None, rows=None, cols=None):
    enc = {"color": None, "size": None, "detail": None, "angle": None,
           "geo_levels": [], "detail_dims": [],
           "label": label if label is not None else (label_fields[0] if label_fields else None)}
    if label_fields is not None:
        enc["label_fields"] = label_fields
    return {"name": "BAN", "visual_type": VT_CARD, "rows": rows or [], "cols": cols or [],
            "encodings": enc}


def _pane(pills):
    """A ``<pane>`` carrying ``<encodings>`` with the given ``(tag, column)`` children."""
    pane = ET.Element("pane")
    holder = ET.SubElement(pane, "encodings")
    for tag, column in pills:
        ET.SubElement(holder, tag, {"column": column})
    return pane


def _parse(pills, ds=DS):
    """Run ``_parse_encodings`` over a fabricated pane, resolving pills as plain measures.

    ``instances``/``base_cols`` are keyed ``(ds, field_id)``; supplying a matching entry for each
    pill lets ``_resolve_field`` bind it without a warning, so the fixture exercises the real
    resolution path rather than a stub.
    """
    ids = [c.split(".")[-1].strip("[]") for _, c in pills]
    base_cols = {(ds, i): {"caption": i, "datatype": "real", "role": "measure",
                           "type": "quantitative", "is_calc": False, "formula": None,
                           "table": "Facts", "hidden": False} for i in ids}
    instances = {}
    index = {(ds, i): {"entity": "Facts", "property": i, "caption": i,
                       "datatype": "real", "role": "measure", "table": "Facts"} for i in ids}
    warnings = []
    enc = _parse_encodings(_pane(pills), ds, base_cols, instances, index, {ds: "Facts"},
                           "BAN", warnings)
    return enc, warnings


# --------------------------------------------------------------------------------------------
# Parse side -- retention
# --------------------------------------------------------------------------------------------

class TestLabelRetention:
    """``_parse_encodings`` keeps EVERY Text pill, in template order."""

    def test_five_text_pills_are_all_retained(self):
        """The real BAN shape: caption + big number + three mutually exclusive deltas."""
        pills = [("text", "[%s].[Caption]" % DS),
                 ("text", "[%s].[CM Count]" % DS),
                 ("text", "[%s].[Pos MoM]" % DS),
                 ("text", "[%s].[Neg MoM]" % DS),
                 ("text", "[%s].[Footnote]" % DS)]
        enc, _ = _parse(pills)
        assert [f["caption"] for f in enc["label_fields"]] == [
            "Caption", "CM Count", "Pos MoM", "Neg MoM", "Footnote"]

    def test_first_pill_still_wins_the_scalar_label(self):
        """``label`` is untouched, so every non-card reader keeps its existing behaviour."""
        enc, _ = _parse([("text", "[%s].[First]" % DS), ("text", "[%s].[Second]" % DS)])
        assert enc["label"]["caption"] == "First"

    def test_label_fields_begins_with_the_scalar_label(self):
        """The widened list is a SUPERSET of the old behaviour -- slot 0 is the old answer."""
        enc, _ = _parse([("text", "[%s].[First]" % DS), ("text", "[%s].[Second]" % DS)])
        assert enc["label_fields"][0] is enc["label"]

    def test_single_text_pill_yields_exactly_one(self):
        enc, _ = _parse([("text", "[%s].[Only]" % DS)])
        assert len(enc["label_fields"]) == 1
        assert enc["label_fields"][0]["caption"] == "Only"

    def test_no_encodings_yields_an_empty_list(self):
        enc, _ = _parse([])
        assert enc["label_fields"] == []

    def test_label_tag_spelling_is_retained_too(self):
        """Tableau writes both ``<text>`` and ``<label>``; both map to the label role."""
        enc, _ = _parse([("label", "[%s].[A]" % DS), ("text", "[%s].[B]" % DS)])
        assert [f["caption"] for f in enc["label_fields"]] == ["A", "B"]

    def test_repeated_identical_pill_is_deduped(self):
        """Tableau can serialise the same field twice; a duplicate value column is not faithful."""
        enc, _ = _parse([("text", "[%s].[Dup]" % DS), ("text", "[%s].[Dup]" % DS)])
        assert len(enc["label_fields"]) == 1

    def test_distinct_pills_are_not_deduped(self):
        enc, _ = _parse([("text", "[%s].[A]" % DS), ("text", "[%s].[B]" % DS)])
        assert len(enc["label_fields"]) == 2

    def test_other_roles_do_not_populate_label_fields(self):
        """Colour/size/detail are separate wells; leaking them into the card would add measures."""
        enc, _ = _parse([("color", "[%s].[C]" % DS),
                         ("size", "[%s].[S]" % DS),
                         ("lod", "[%s].[D]" % DS)])
        assert enc["label_fields"] == []

    def test_other_roles_are_unaffected_by_the_retention(self):
        enc, _ = _parse([("color", "[%s].[C]" % DS), ("text", "[%s].[T]" % DS)])
        assert enc["color"]["caption"] == "C"
        assert enc["label"]["caption"] == "T"

    def test_missing_encodings_holder_returns_the_key(self):
        """Callers index ``label_fields`` unconditionally, so the key must always exist."""
        warnings = []
        enc = _parse_encodings(ET.Element("pane"), DS, {}, {}, {}, {DS: "Facts"}, "BAN", warnings)
        assert enc["label_fields"] == []

    def test_none_pane_returns_the_key(self):
        warnings = []
        enc = _parse_encodings(None, DS, {}, {}, {}, {DS: "Facts"}, "BAN", warnings)
        assert enc["label_fields"] == []


# --------------------------------------------------------------------------------------------
# Query side -- the card widens
# --------------------------------------------------------------------------------------------

class TestCardBindsEveryLabelMeasure:

    def test_four_measures_produce_four_value_projections(self):
        fields = [_meas("CM Count"), _meas("Pos MoM"), _meas("Neg MoM"), _meas("Prior")]
        state = _build_query_state(_card_ws(fields), "Facts", {}, [])
        assert len(state["Values"]["projections"]) == 4

    def test_projection_order_follows_the_template(self):
        """Slot order carries meaning (big number first, deltas after); do not re-sort it."""
        fields = [_meas("Big"), _meas("Pos"), _meas("Neg")]
        state = _build_query_state(_card_ws(fields), "Facts", {}, [])
        refs = [p.get("nativeQueryRef") for p in state["Values"]["projections"]]
        assert refs == sorted(refs, key=lambda r: ["Big", "Pos", "Neg"].index(
            next(n for n in ["Big", "Pos", "Neg"] if n in r)))

    def test_single_measure_is_unchanged(self):
        """The pre-existing single-pill card must emit exactly what it always did."""
        state = _build_query_state(_card_ws([_meas("Only")]), "Facts", {}, [])
        assert len(state["Values"]["projections"]) == 1

    def test_category_label_pills_are_excluded(self):
        """A static caption pill is a dimension; projecting it would group the card."""
        fields = [_cat("Caption"), _meas("Big"), _cat("Footnote")]
        state = _build_query_state(_card_ws(fields), "Facts", {}, [])
        assert len(state["Values"]["projections"]) == 1

    def test_all_category_labels_emit_no_values_role(self):
        state = _build_query_state(_card_ws([_cat("A"), _cat("B")]), "Facts", {}, [])
        assert "Values" not in state

    def test_legacy_ir_without_label_fields_falls_back_to_the_scalar(self):
        """An IR produced before this change (no ``label_fields`` key) must still bind."""
        ws = _card_ws([_meas("Solo")])
        del ws["encodings"]["label_fields"]
        state = _build_query_state(ws, "Facts", {}, [])
        assert len(state["Values"]["projections"]) == 1

    def test_empty_label_fields_falls_back_to_the_scalar(self):
        """Empty list is falsy, so the fallback keeps a hand-built IR working."""
        ws = _card_ws([], label=_meas("Solo"))
        state = _build_query_state(ws, "Facts", {}, [])
        assert len(state["Values"]["projections"]) == 1

    def test_no_label_at_all_emits_no_values_role(self):
        ws = _card_ws([], label=None)
        state = _build_query_state(ws, "Facts", {}, [])
        assert "Values" not in state

    def test_shelf_measures_are_still_projected_alongside(self):
        """Rows/Cols measures must not be displaced by the widened label set."""
        ws = _card_ws([_meas("Label")], rows=[_meas("OnRows")])
        state = _build_query_state(ws, "Facts", {}, [])
        refs = " ".join(p.get("nativeQueryRef") or "" for p in state["Values"]["projections"])
        assert "OnRows" in refs and "Label" in refs

    def test_a_measure_on_both_shelf_and_label_is_deduped(self):
        m = _meas("Shared")
        ws = _card_ws([m], rows=[dict(m)])
        state = _build_query_state(ws, "Facts", {}, [])
        assert len(state["Values"]["projections"]) == 1


class TestVisualTypeEscalation:
    """Two or more values is Power BI's native row-of-big-numbers, not a single-number card."""

    def test_one_value_stays_a_card(self):
        assert _pbir_vtype(VT_CARD, {"Values": {"projections": [{}]}}) == "card"

    def test_two_values_becomes_a_multi_row_card(self):
        assert _pbir_vtype(VT_CARD, {"Values": {"projections": [{}, {}]}}) == "multiRowCard"

    def test_four_values_becomes_a_multi_row_card(self):
        assert _pbir_vtype(VT_CARD, {"Values": {"projections": [{}] * 4}}) == "multiRowCard"


class TestNonCardVisualsAreUntouched:
    """The retention is additive; only ``VT_CARD`` reads ``label_fields``."""

    @pytest.mark.parametrize("vt", ["bar", "column", "line", "pie"])
    def test_extra_label_pills_do_not_reach_other_visual_types(self, vt):
        fields = [_meas("First"), _meas("Second"), _meas("Third")]
        ws = _card_ws(fields, rows=[_cat("Month")])
        ws["visual_type"] = vt
        state = _build_query_state(ws, "Facts", {}, [])
        values = [p for role in ("Y", "Values") for p in
                  (state.get(role) or {}).get("projections", [])]
        refs = " ".join(p.get("nativeQueryRef") or "" for p in values)
        assert "Second" not in refs and "Third" not in refs
