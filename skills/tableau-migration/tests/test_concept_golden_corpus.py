"""T3.2 -- concept-regression golden corpus (offline, stdlib, synthetic; additive / test-only).

Pins the deterministic Tableau->DAX translator's EXACT current behavior for the nine analytical
concepts of the Tableau<->Power BI concept crosswalk (the WOW2026 migration handoff Rosetta).
Each concept is encoded verbatim from the crosswalk with its documented badge; the representative
Tableau formula(s) are driven through the REAL translator and asserted to either emit exact DAX
(the safe-subset / shipped-LOD concepts) or fail closed to an inert stub (``dax is None`` with a
documented reason keyword) -- the crown of the faithfulness-over-coverage contract: never wrong DAX,
stub-when-unsure.

Every expected value below was captured from the live translator at authoring time (probe
``_probe_concept_dax.py``), NOT assumed. The crosswalk itself is a volatile, out-of-repo asset;
only the distilled facts (formulas + expected DAX/stub) are baked in here so the committed suite is
fully offline/synthetic.

The forcing function ``test_all_nine_concepts_present_and_badge_consistent`` guards the corpus: all
nine concepts must be present, each badge must be one of the documented classes, and -- crucially --
the badge must be CONSISTENT with what the translator actually did (deterministic => every case emits
exact DAX; stub/visual-calc => every case fails closed; mixed => at least one of each; not-a-calc =>
no formula driven). So a translator change that silently flips a concept's category fails loudly here.
"""
import pytest

from calc_to_dax import (
    translate_tableau_calc_to_column_dax,
    translate_tableau_calc_to_dax,
)

# Self-contained resolver: caption -> (table_display_name, clean_col, tmdl_type).
_FIELDS = {
    "Sales": ("Orders", "Sales", "decimal"),
    "Profit": ("Orders", "Profit", "decimal"),
    "Order Date": ("Orders", "Order_Date", "dateTime"),
    "Customer Name": ("Orders", "Customer_Name", "string"),
    "Order ID": ("Orders", "Order_ID", "string"),
    "Dates": ("Orders", "Dates", "int64"),
    "Film_1_Rating": ("Films", "Film_1_Rating", "decimal"),
    "Film_2_Rating": ("Films", "Film_2_Rating", "decimal"),
    "Film_3_Rating": ("Films", "Film_3_Rating", "decimal"),
    "Film_4_Rating": ("Films", "Film_4_Rating", "decimal"),
}


def _resolver(caption):
    return _FIELDS.get(caption)


def _drive(mode, formula):
    """Run the real translator in the given mode; return (dax|None, reason)."""
    if mode == "measure":
        dax, reason, _tables = translate_tableau_calc_to_dax(formula, _resolver)
    elif mode == "column":
        dax, reason, _tables = translate_tableau_calc_to_column_dax(formula, _resolver)
    else:  # pragma: no cover - guarded by the corpus shape test
        raise AssertionError("unknown drive mode %r" % mode)
    return dax, reason


# ``expect`` is one of:
#   ("dax",  "<exact DAX>")           -- translated: assert byte-for-byte
#   ("stub", "<reason keyword>")      -- fail closed: assert dax is None + keyword in reason
# Glyph literals use \u escapes so the committed source + plugin mirror stay pure ASCII.
_ARROW_DAX = ('IF(SUM(\'Orders\'[Profit]) > 0, "\u25b2", '
              'IF(SUM(\'Orders\'[Profit]) < 0, "\u25bc", "\u25ac"))')

CONCEPTS = [
    {
        "num": 1,
        "name": "Difference from a selected member",
        "badge": "stub",              # current-engine classification (drives the consistency guard)
        "crosswalk_badge": "stub -> second compiler",
        "note": "EXCLUDE-LOD + ATTR + a parameter read -> outside the safe subset; W25's "
                "CALCULATE([m], dim = SELECTEDVALUE(...)) is the assisted-tier target.",
        "cases": [
            ("Difference from Selected LOD (ATTR over EXCLUDE with param)", "measure",
             "AVG([Sales]) - ATTR({ EXCLUDE [Order Date] : "
             "AVG(IF YEAR([Order Date]) = [Parameters].[Parameter 3] THEN [Sales] END) })",
             ("stub", "ATTR")),
        ],
    },
    {
        "num": 2,
        "name": "Year-over-year / prior period",
        "badge": "visual-calc",       # a view-only quick table calc -> rebuilt as a report visual calc
        "crosswalk_badge": "visual-calc (or manual DAX)",
        "note": "LOOKUP is a table calculation -> not a datasource-scope measure; rebuilt as a Power "
                "BI Visual Calculation, or hand-authored CALCULATE(FILTER(ALL(Year), ...)) (W27).",
        "cases": [
            ("YoY anchor LOOKUP table calc", "measure",
             "LOOKUP(YEAR(MIN([Order Date])), 0)",
             ("stub", "LOOKUP")),
        ],
    },
    {
        "num": 3,
        "name": "Conditional direction indicator",
        "badge": "deterministic",
        "crosswalk_badge": "deterministic",
        "note": "IF/ELSEIF/ELSE over an aggregate -> nested DAX IF(); squarely in the safe subset.",
        "cases": [
            ("direction arrow IF/ELSEIF/ELSE glyphs", "measure",
             'IF SUM([Profit]) > 0 THEN "\u25b2" ELSEIF SUM([Profit]) < 0 '
             'THEN "\u25bc" ELSE "\u25ac" END',
             ("dax", _ARROW_DAX)),
        ],
    },
    {
        "num": 4,
        "name": "Read a parameter / current selection",
        "badge": "stub",
        "crosswalk_badge": "param not migrated; slicer surfaced",
        "note": "A parameter has no 1:1 model object; a calc that reads one stubs (resolve to "
                "SELECTEDVALUE over the surfaced slicer column).",
        "cases": [
            ("read a parameter (bare boolean)", "measure",
             '[Parameters].[Parameter 3] = "Map"',
             ("stub", "parameter")),
            ("read a parameter (inside IF)", "measure",
             'IF [Parameters].[Parameter 3] = "Map" THEN 1 ELSE 0 END',
             ("stub", "parameter")),
        ],
    },
    {
        "num": 5,
        "name": "LOD re-grain (FIXED / INCLUDE / EXCLUDE)",
        "badge": "mixed",             # v1.39.0: FIXED + INCLUDE-reagg + EXCLUDE translate; bare INCLUDE stubs
        "crosswalk_badge": "stub -> second compiler",
        "note": "Post-v1.39.0 the deterministic tier translates bare FIXED, re-aggregated INCLUDE, and "
                "bare EXCLUDE; a bare INCLUDE with no enclosing aggregation still fails closed.",
        "cases": [
            ("FIXED bare -> CALCULATE + ALLEXCEPT", "measure",
             "{FIXED [Customer Name] : SUM([Sales])}",
             ("dax", "CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[Customer_Name]))")),
            ("INCLUDE re-aggregated -> SUMX + SUMMARIZE", "measure",
             "SUM({INCLUDE [Order ID] : SUM([Sales])})",
             ("dax", "SUMX(SUMMARIZE('Orders', 'Orders'[Order_ID]), CALCULATE(SUM('Orders'[Sales])))")),
            ("EXCLUDE bare -> CALCULATE + REMOVEFILTERS", "measure",
             "{EXCLUDE [Order Date] : SUM([Sales])}",
             ("dax", "CALCULATE(SUM('Orders'[Sales]), REMOVEFILTERS('Orders'[Order_Date]))")),
            ("bare INCLUDE (no outer agg) fails closed", "measure",
             "{INCLUDE [Order ID] : SUM([Sales])}",
             ("stub", "INCLUDE")),
        ],
    },
    {
        "num": 6,
        "name": "Reshape data (pivot / unpivot)",
        "badge": "not-a-calc",
        "crosswalk_badge": "not a calc (Power Query)",
        "note": "A data-prep reshape (Tableau Pivot <-> Table.UnpivotOtherColumns / Table.Pivot in M); "
                "lives in the connection/partition layer, never in calc->DAX.",
        "cases": [],
    },
    {
        "num": 7,
        "name": "Row-wise aggregate across columns",
        "badge": "mixed",             # row-level arithmetic translates; across-columns MAX({...}) stubs
        "crosswalk_badge": "arithmetic deterministic; table-constructor stub",
        "note": "Row-level INT/%/ arithmetic translates as a calc column; the across-columns "
                "MAX([a],[b],...) table-constructor idiom fails closed (outside the row-level subset).",
        "cases": [
            ("row-wise packed-date math (% and /)", "column",
             "INT([Dates] % 10000 / 100)",
             ("dax", "TRUNC(DIVIDE(MOD('Orders'[Dates], 10000), 100))")),
            ("row-wise packed-date div", "column",
             "INT([Dates] / 10000)",
             ("dax", "TRUNC(DIVIDE('Orders'[Dates], 10000))")),
            ("across-columns MAX table constructor fails closed", "column",
             "MAX([Film_1_Rating], [Film_2_Rating], [Film_3_Rating], [Film_4_Rating])",
             ("stub", "row-level column")),
        ],
    },
    {
        "num": 8,
        "name": "Two-point / dumbbell (dual-axis) viz",
        "badge": "not-a-calc",
        "crosswalk_badge": "not a calc (viz structure)",
        "note": "Chart structure (dual-axis / synchronized marks / imported custom visual), not a "
                "formula; handled by the Tier-1 viz rebuild, not calc->DAX.",
        "cases": [],
    },
    {
        "num": 9,
        "name": "Running / window ranking",
        "badge": "visual-calc",
        "crosswalk_badge": "visual-calc",
        "note": "RUNNING_/WINDOW_/INDEX are table calculations -> rebuilt as Power BI Visual "
                "Calculations on the report, not datasource-scope measures.",
        "cases": [
            ("RUNNING_SUM table calc", "measure",
             "RUNNING_SUM(SUM([Sales]))",
             ("stub", "RUNNING_SUM")),
            ("WINDOW_AVG table calc", "measure",
             "WINDOW_AVG(SUM([Sales]))",
             ("stub", "WINDOW_AVG")),
        ],
    },
]

_ALLOWED_BADGES = {"deterministic", "visual-calc", "stub", "mixed", "not-a-calc"}
_STUB_ONLY_BADGES = {"stub", "visual-calc"}

# Flatten (concept_num, name, mode, formula, expect) for parametrization.
_ALL_CASES = [
    (c["num"], label, mode, formula, expect)
    for c in CONCEPTS
    for (label, mode, formula, expect) in c["cases"]
]


@pytest.mark.parametrize(
    "num,label,mode,formula,expect",
    _ALL_CASES,
    ids=["c%d:%s" % (n, lbl) for (n, lbl, _m, _f, _e) in _ALL_CASES],
)
def test_concept_case_translates_exactly_or_fails_closed(num, label, mode, formula, expect):
    dax, reason = _drive(mode, formula)
    kind, payload = expect
    if kind == "dax":
        assert dax == payload, (
            "concept %d %r: expected exact DAX %r, got %r (reason=%r)"
            % (num, label, payload, dax, reason))
    else:  # stub
        assert dax is None, (
            "concept %d %r: expected a fail-closed stub, but got DAX %r" % (num, label, dax))
        assert reason, "concept %d %r: a stub must carry a reason" % (num, label)
        assert payload.lower() in reason.lower(), (
            "concept %d %r: stub reason %r should mention %r" % (num, label, reason, payload))


def test_all_nine_concepts_present_and_badge_consistent():
    """Forcing function: the corpus must cover concepts 1..9, and each concept's documented badge
    must match what the translator ACTUALLY did across its cases."""
    nums = sorted(c["num"] for c in CONCEPTS)
    assert nums == list(range(1, 10)), "the corpus must cover exactly concepts 1..9, got %r" % nums

    for c in CONCEPTS:
        badge = c["badge"]
        assert badge in _ALLOWED_BADGES, "concept %d has undocumented badge %r" % (c["num"], badge)
        assert c.get("note"), "concept %d must carry a rationale note" % c["num"]
        assert c.get("crosswalk_badge"), "concept %d must record its crosswalk badge" % c["num"]

        outcomes = []
        for (_label, mode, formula, expect) in c["cases"]:
            dax, _reason = _drive(mode, formula)
            outcomes.append("dax" if dax is not None else "stub")

        if badge == "not-a-calc":
            assert not c["cases"], "concept %d is not-a-calc but drives a formula" % c["num"]
            continue

        assert c["cases"], "concept %d (%s) must exercise at least one formula" % (c["num"], badge)
        translated = [o for o in outcomes if o == "dax"]
        stubbed = [o for o in outcomes if o == "stub"]

        if badge == "deterministic":
            assert not stubbed, (
                "concept %d is 'deterministic' but a case fell back: %r" % (c["num"], outcomes))
        elif badge in _STUB_ONLY_BADGES:
            assert not translated, (
                "concept %d is %r but a case emitted DAX: %r" % (c["num"], badge, outcomes))
        elif badge == "mixed":
            assert translated and stubbed, (
                "concept %d is 'mixed' but did not show BOTH a translated and a stubbed case: %r"
                % (c["num"], outcomes))


def test_corpus_is_non_vacuous_across_categories():
    """Guard against a corpus that silently collapses to one behavior class."""
    badges = [c["badge"] for c in CONCEPTS]
    assert badges.count("deterministic") >= 1
    assert badges.count("mixed") >= 1
    assert badges.count("not-a-calc") >= 2
    assert "stub" in badges and "visual-calc" in badges
    # at least one exact-DAX assertion and one stub assertion actually exist in the driven cases
    kinds = {expect[0] for (_n, _l, _m, _f, expect) in _ALL_CASES}
    assert kinds == {"dax", "stub"}, "the corpus must assert BOTH exact-DAX and fail-closed stubs"
