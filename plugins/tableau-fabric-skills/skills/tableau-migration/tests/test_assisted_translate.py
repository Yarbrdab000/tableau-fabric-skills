"""Tests for the ASSISTED-TRANSLATION layer (opt-in, human-approved).

The deterministic translator only emits DAX for a provable 1:1 subset; everything else
falls back to an inert ``= 0`` stub. This layer runs ONLY on those fallbacks, recognizes a
small registry of higher-level Tableau idioms whose faithful DAX is a *semantic* rewrite
(here: argmax-over-a-dimension), and returns a clearly-labeled SUGGESTION a human approves --
it is never silently emitted as a live measure.

These tests lock:
  * the argmax detector (inline and via a referenced calc), with exact DAX, plus the negatives
    that MUST abstain (so the suggester never fires on something it cannot rewrite faithfully);
  * the orchestrator wiring -- a stub gains a ``TranslationSuggestion`` annotation and a
    ``report["assisted_suggestions"]`` entry, while ``approved_calc_dax`` flips an approved
    suggestion into a real measure tagged ``assisted translation (human-approved)``;
  * that the deterministic safe-subset behavior is unchanged for non-idiom calcs.
"""
import assemble_model as A
from calc_to_dax import suggest_assisted_dax


# Shared resolver: caption -> (table_display_name, clean_col, tmdl_type).
_FIELDS = {
    "Sales": ("Orders", "Sales", "decimal"),
    "Profit": ("Orders", "Profit", "decimal"),
    "State": ("Orders", "State", "string"),
    "City": ("Orders", "City", "string"),
    "Region": ("Orders", "Region", "string"),
    "People Count": ("People", "People_Count", "int64"),
}


def _resolver(caption):
    return _FIELDS.get(caption)


# The canonical idiom: "the city with the most sales in each state".
_DETAIL = "{FIXED [State], [City] : SUM([Sales])}"
_MAX = "{FIXED [State] : MAX({FIXED [State], [City] : SUM([Sales])})}"
_ARGMAX_INLINE = f"IF {_MAX} = {_DETAIL} THEN [City] END"

_EXPECTED_DAX = (
    "VAR __detail =\n"
    "    CALCULATETABLE(\n"
    "        ADDCOLUMNS(\n"
    "            SUMMARIZE('Orders', 'Orders'[State], 'Orders'[City]),\n"
    '            "@value", CALCULATE(SUM(\'Orders\'[Sales]))\n'
    "        ),\n"
    "        ALLEXCEPT('Orders', 'Orders'[State])\n"
    "    )\n"
    "VAR __max = MAXX(__detail, [@value])\n"
    "RETURN\n"
    '    CONCATENATEX(FILTER(__detail, [@value] = __max), \'Orders\'[City], ", ")'
)

# The argmin twin -- "the city with the LEAST sales in each state" -- is byte-identical to argmax
# except MAXX/__max -> MINX/__min, so derive its expectation to keep the two structurally locked.
_EXPECTED_ARGMIN_DAX = _EXPECTED_DAX.replace("__max", "__min").replace("MAXX", "MINX")
_MIN = "{FIXED [State] : MIN({FIXED [State], [City] : SUM([Sales])})}"
_ARGMIN_INLINE = f"IF {_MIN} = {_DETAIL} THEN [City] END"


# --------------------------------------------------------------------------- detector
def test_argmax_inline_detected_with_exact_dax():
    s = suggest_assisted_dax(_ARGMAX_INLINE, _resolver)
    assert s is not None
    assert s["pattern"] == "argmax-dimension"
    assert s["requires_approval"] is True
    assert s["dax"] == _EXPECTED_DAX
    assert any("Ties" in c for c in s["caveats"])


def test_argmin_inline_detected_with_exact_dax():
    # The same structural idiom with MIN -> argmin: MINX/__min, otherwise byte-identical to argmax.
    s = suggest_assisted_dax(_ARGMIN_INLINE, _resolver)
    assert s is not None
    assert s["pattern"] == "argmin-dimension"
    assert s["requires_approval"] is True
    assert s["dax"] == _EXPECTED_ARGMIN_DAX
    assert "MINX(__detail, [@value])" in s["dax"] and "MAXX" not in s["dax"]
    assert any("minimum" in c for c in s["caveats"])
    assert any("BOTTOMN" in c for c in s["caveats"])


def test_argmin_via_referenced_calc():
    # The MIN selector named as a separate calc, mirroring the argmax referenced-calc shape.
    formula = f"IF [Calculation_77] = {_DETAIL} THEN [City] END"
    s = suggest_assisted_dax(formula, _resolver, calc_lookup={"calculation_77": _MIN})
    assert s is not None and s["dax"] == _EXPECTED_ARGMIN_DAX


def test_argmax_unchanged_after_argmin_generalization():
    # Regression guard: the argmax branch stays byte-identical (MAXX/__max) after generalizing.
    s = suggest_assisted_dax(_ARGMAX_INLINE, _resolver)
    assert s["pattern"] == "argmax-dimension"
    assert s["dax"] == _EXPECTED_DAX
    assert "MAXX(__detail, [@value])" in s["dax"] and "MINX" not in s["dax"]


def test_argmax_detected_when_max_is_on_the_left_or_right():
    # The equality may be written either way round; both must detect identically.
    flipped = f"IF {_DETAIL} = {_MAX} THEN [City] END"
    a = suggest_assisted_dax(_ARGMAX_INLINE, _resolver)
    b = suggest_assisted_dax(flipped, _resolver)
    assert a is not None and b is not None
    assert a["dax"] == b["dax"] == _EXPECTED_DAX


def test_argmax_via_referenced_calc():
    # The real Tableau shape: the IF references a SEPARATE "max" calc by its internal name.
    formula = f"IF [Calculation_99] = {_DETAIL} THEN [City] END"
    lookup = {"calculation_99": _MAX, "max city sales": _MAX}
    s = suggest_assisted_dax(formula, _resolver, calc_lookup=lookup)
    assert s is not None and s["dax"] == _EXPECTED_DAX


def test_argmax_both_sides_referenced_calcs():
    # The full "Highest Selling City By State Sales" shape: BOTH the per-state max and the
    # per-city detail are separate named calcs, so the final IF references each by name.
    formula = "IF [Max City Sales] = [City Sales] THEN [City] END"
    lookup = {"max city sales": _MAX, "city sales": _DETAIL}
    s = suggest_assisted_dax(formula, _resolver, calc_lookup=lookup)
    assert s is not None and s["dax"] == _EXPECTED_DAX
    # equality written the other way round resolves identically
    flipped = "IF [City Sales] = [Max City Sales] THEN [City] END"
    assert suggest_assisted_dax(flipped, _resolver, calc_lookup=lookup)["dax"] == _EXPECTED_DAX


def test_argmax_detail_referenced_max_inline():
    # Mixed: detail is a named calc, the per-state max is written inline.
    formula = f"IF {_MAX} = [City Sales] THEN [City] END"
    lookup = {"city sales": _DETAIL}
    s = suggest_assisted_dax(formula, _resolver, calc_lookup=lookup)
    assert s is not None and s["dax"] == _EXPECTED_DAX


def test_argmax_detail_ref_without_lookup_abstains():
    # the detail side is a bare reference but no lookup is supplied -> cannot resolve, abstain
    formula = f"IF {_MAX} = [City Sales] THEN [City] END"
    assert suggest_assisted_dax(formula, _resolver) is None


def test_argmax_detail_ref_to_non_lod_abstains():
    # the referenced "detail" calc is not a FIXED LOD (a plain aggregate) -> abstain, never force-fit
    formula = f"IF {_MAX} = [City Sales] THEN [City] END"
    lookup = {"city sales": "SUM([Sales])"}
    assert suggest_assisted_dax(formula, _resolver, calc_lookup=lookup) is None


def test_argmax_ref_without_lookup_abstains():
    formula = f"IF [Calculation_99] = {_DETAIL} THEN [City] END"
    assert suggest_assisted_dax(formula, _resolver) is None


def test_non_if_formula_abstains():
    assert suggest_assisted_dax("SUM([Sales])", _resolver) is None
    assert suggest_assisted_dax(_MAX, _resolver) is None


def test_multibranch_if_abstains():
    # an ELSE means the THEN branch is not the whole result -> not a faithful argmax
    formula = f"IF {_MAX} = {_DETAIL} THEN [City] ELSE [Region] END"
    assert suggest_assisted_dax(formula, _resolver) is None


def test_aggregate_mismatch_abstains():
    # detail uses SUM, the max calc re-aggregates AVG -> not the same measure, abstain
    bad_max = "{FIXED [State] : MAX({FIXED [State], [City] : AVG([Sales])})}"
    formula = f"IF {bad_max} = {_DETAIL} THEN [City] END"
    assert suggest_assisted_dax(formula, _resolver) is None


def test_field_mismatch_abstains():
    bad_max = "{FIXED [State] : MAX({FIXED [State], [City] : SUM([Profit])})}"
    formula = f"IF {bad_max} = {_DETAIL} THEN [City] END"
    assert suggest_assisted_dax(formula, _resolver) is None


def test_then_dimension_must_be_the_extra_grain_dim():
    # THEN returns [Region], which is NOT the dimension being argmax'd over ([City]) -> abstain
    formula = f"IF {_MAX} = {_DETAIL} THEN [Region] END"
    assert suggest_assisted_dax(formula, _resolver) is None


def test_partition_must_be_strict_subset_abstains():
    # partition == grain (no dimension is actually argmax'd over) -> abstain
    deg_max = "{FIXED [State], [City] : MAX({FIXED [State], [City] : SUM([Sales])})}"
    deg_detail = "{FIXED [State], [City] : SUM([Sales])}"
    formula = f"IF {deg_max} = {deg_detail} THEN [City] END"
    assert suggest_assisted_dax(formula, _resolver) is None


def test_cross_table_abstains():
    # the argmax dimension lives on a different table than the measure -> single-table only
    detail = "{FIXED [Region], [People Count] : SUM([Sales])}"
    mx = "{FIXED [Region] : MAX({FIXED [Region], [People Count] : SUM([Sales])})}"
    formula = f"IF {mx} = {detail} THEN [People Count] END"
    assert suggest_assisted_dax(formula, _resolver) is None


def test_unresolved_field_abstains():
    formula = "IF {FIXED [Nope] : MAX({FIXED [Nope], [Gone] : SUM([Sales])})} = " \
              "{FIXED [Nope], [Gone] : SUM([Sales])} THEN [Gone] END"
    assert suggest_assisted_dax(formula, _resolver) is None


# --------------------------------------------------------------------------- orchestrator wiring
_CALCS = [
    {"name": "max city sales", "formula": _MAX, "internal_name": "Calculation_99"},
    {"name": "city with the most sales",
     "formula": "IF [Calculation_99] = " + _DETAIL + " THEN [City] END",
     "internal_name": "Calculation_100"},
]


def _measures(calcs, **kw):
    lookup = A._calc_lookup_from(calcs)
    return A._measures_part(calcs, _resolver, calc_lookup=lookup, **kw)


def test_measures_part_surfaces_suggestion_and_keeps_stub():
    tmdl, report, suggestions = _measures(_CALCS)
    by = {r["measure"]: r for r in report}
    # the deterministic max calc still translates
    assert by["max city sales"]["status"] == "translated"
    # the argmax calc stays an inert stub but gains a suggestion
    arg = by["city with the most sales"]
    assert arg["status"] == "assisted-suggested"
    assert arg["assisted_suggestion"]["pattern"] == "argmax-dimension"
    assert [s["measure"] for s in suggestions] == ["city with the most sales"]
    # the live measure is STILL inert; the suggestion is a non-binding annotation
    assert "TranslationSuggestion = " in tmdl
    assert "TranslationSuggestionPattern = argmax-dimension" in tmdl
    assert "\tmeasure 'city with the most sales' = 0\n" in tmdl


def test_measures_part_approval_flips_to_real_measure():
    s = suggest_assisted_dax(_CALCS[1]["formula"], _resolver, calc_lookup=A._calc_lookup_from(_CALCS))
    tmdl, report, suggestions = _measures(
        _CALCS, approved_calc_dax={"city with the most sales": s["dax"]})
    by = {r["measure"]: r for r in report}
    assert by["city with the most sales"]["status"] == "assisted-approved"
    # approved DAX is collapsed to a single valid line and tagged as assisted+approved
    assert "annotation TranslatedBy = assisted translation (human-approved)" in tmdl
    assert "\tmeasure 'city with the most sales' = VAR __detail =" in tmdl
    # nothing left pending once approved
    assert suggestions == []
    assert "TranslationSuggestion = " not in tmdl


def test_non_idiom_stub_is_byte_for_byte_unchanged():
    # A stub with no recognized idiom must produce exactly the legacy output: an inert
    # `= 0` with only the TableauFormula annotation -- no suggestion machinery leaks in.
    calcs = [{"name": "weird calc", "formula": "WINDOW_SUM(SUM([Sales]))"}]
    tmdl, report, suggestions = _measures(calcs)
    assert report[0]["status"] == "stub"
    assert suggestions == []
    assert "TranslationSuggestion" not in tmdl
    assert "\tmeasure 'weird calc' = 0\n" in tmdl


# ------------------------------------------------------- first/last-value-by-date detector
# A resolver carrying a date column and a same-table numeric measure, plus a text column.
_DT_FIELDS = {
    "Score": ("Assess", "Score", "decimal"),
    "Assess Date": ("Assess", "Assess_Date", "dateTime"),
    "Client": ("Assess", "Client", "string"),
    "Sales": ("Orders", "Sales", "decimal"),
    "Order Date": ("Orders", "Order_Date", "dateTime"),
}


def _dt_resolver(caption):
    return _DT_FIELDS.get(caption)


_LAST_BY_DATE_DAX = (
    "VAR __d = MAX('Assess'[Assess_Date])\n"
    "RETURN\n"
    "    CALCULATE(AVERAGE('Assess'[Score]), 'Assess'[Assess_Date] = __d)"
)
_FIRST_BY_DATE_DAX = _LAST_BY_DATE_DAX.replace("MAX(", "MIN(")


def test_last_value_by_date_detected_with_exact_dax():
    f = "IF [Assess Date] = WINDOW_MAX([Assess Date]) THEN [Score] END"
    s = suggest_assisted_dax(f, _dt_resolver)
    assert s is not None
    assert s["pattern"] == "last-value-by-date"
    assert s["requires_approval"] is True
    assert s["dax"] == _LAST_BY_DATE_DAX
    assert any("latest" in c for c in s["caveats"])


def test_first_value_by_date_detected_with_exact_dax():
    # WINDOW_MIN -> earliest, and the equality is written the other way round.
    f = "IF WINDOW_MIN([Assess Date]) = [Assess Date] THEN [Score] END"
    s = suggest_assisted_dax(f, _dt_resolver)
    assert s is not None
    assert s["pattern"] == "first-value-by-date"
    assert s["dax"] == _FIRST_BY_DATE_DAX
    assert "MIN('Assess'[Assess_Date])" in s["dax"] and "MAX(" not in s["dax"]


def test_first_last_by_date_gate_passes():
    from translation_router import check_candidate_dax
    for f in ("IF [Assess Date] = WINDOW_MAX([Assess Date]) THEN [Score] END",
              "IF [Assess Date] = WINDOW_MIN([Assess Date]) THEN [Score] END"):
        s = suggest_assisted_dax(f, _dt_resolver)
        assert check_candidate_dax(s["dax"])["ok"] is True


def test_first_last_by_date_text_result_abstains():
    # the THEN result is a text column -> AVERAGE would be invalid, abstain
    f = "IF [Assess Date] = WINDOW_MAX([Assess Date]) THEN [Client] END"
    assert suggest_assisted_dax(f, _dt_resolver) is None


def test_first_last_by_date_else_abstains():
    f = "IF [Assess Date] = WINDOW_MAX([Assess Date]) THEN [Score] ELSE 0 END"
    assert suggest_assisted_dax(f, _dt_resolver) is None


def test_first_last_by_date_mismatched_field_abstains():
    # the windowed date field differs from the compared field -> not the idiom
    f = "IF [Assess Date] = WINDOW_MAX([Order Date]) THEN [Score] END"
    assert suggest_assisted_dax(f, _dt_resolver) is None


def test_first_last_by_date_cross_table_abstains():
    # date on Orders, measure on Assess -> single-table only
    f = "IF [Order Date] = WINDOW_MAX([Order Date]) THEN [Score] END"
    assert suggest_assisted_dax(f, _dt_resolver) is None


# ------------------------------------------------------------------- year-gated detector
def test_year_gated_literal_detected_with_exact_dax():
    f = "IF YEAR([Order Date]) = 2024 THEN [Sales] END"
    s = suggest_assisted_dax(f, _dt_resolver)
    assert s is not None
    assert s["pattern"] == "year-gated-measure"
    assert s["dax"] == (
        "CALCULATE(SUM('Orders'[Sales]), KEEPFILTERS(YEAR('Orders'[Order_Date]) = 2024))")


def test_year_gated_current_year_today_uses_maxyr_anchor():
    f = "IF YEAR([Order Date]) = YEAR(TODAY()) THEN [Sales] END"
    s = suggest_assisted_dax(f, _dt_resolver)
    assert s is not None
    assert s["dax"] == (
        "VAR __y = YEAR(CALCULATE(MAX('Orders'[Order_Date]), "
        "REMOVEFILTERS('Orders'[Order_Date])))\n"
        "RETURN\n"
        "    CALCULATE(SUM('Orders'[Sales]), KEEPFILTERS(YEAR('Orders'[Order_Date]) = __y))")
    assert any("MAX year" in c for c in s["caveats"])


def test_year_gated_current_year_maxdate_signal():
    # YEAR(MAX([d])) on the SAME date field is also a "current year" signal.
    f = "IF YEAR([Order Date]) = YEAR(MAX([Order Date])) THEN [Sales] END"
    s = suggest_assisted_dax(f, _dt_resolver)
    assert s is not None and "VAR __y = YEAR(CALCULATE(MAX(" in s["dax"]


def test_year_gated_current_year_via_referenced_calc():
    # the year signal is a bare calc reference resolving to YEAR(TODAY()).
    f = "IF YEAR([Order Date]) = [This Year] THEN [Sales] END"
    s = suggest_assisted_dax(f, _dt_resolver, calc_lookup={"this year": "YEAR(TODAY())"})
    assert s is not None and "VAR __y = YEAR(CALCULATE(MAX(" in s["dax"]


def test_year_gated_gate_passes():
    from translation_router import check_candidate_dax
    for f in ("IF YEAR([Order Date]) = 2024 THEN [Sales] END",
              "IF YEAR([Order Date]) = YEAR(TODAY()) THEN [Sales] END"):
        s = suggest_assisted_dax(f, _dt_resolver)
        assert check_candidate_dax(s["dax"])["ok"] is True


def test_year_gated_non_date_arg_abstains():
    # YEAR() over a numeric field is not a date gate -> abstain
    f = "IF YEAR([Sales]) = 2024 THEN [Sales] END"
    assert suggest_assisted_dax(f, _dt_resolver) is None


def test_year_gated_text_result_abstains():
    f = "IF YEAR([Order Date]) = 2024 THEN [Client] END"
    assert suggest_assisted_dax(f, _dt_resolver) is None


def test_year_gated_unknown_year_signal_abstains():
    # a non-literal, non-current-year RHS (e.g. prior-year arithmetic) is out of scope -> abstain
    f = "IF YEAR([Order Date]) = YEAR(TODAY()) - 1 THEN [Sales] END"
    assert suggest_assisted_dax(f, _dt_resolver) is None
