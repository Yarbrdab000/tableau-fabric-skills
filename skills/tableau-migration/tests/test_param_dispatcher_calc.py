"""A parameter DISPATCHER must not blank every selection because ONE branch is unmigrated.

``CASE [Parameters].[P] WHEN 1 THEN <metric a> WHEN 2 THEN <metric b> ... END`` is how a Tableau
dashboard builds its control surface -- a slicer picks the metric, and the charts project the
dispatcher. Translation was all-or-nothing: one branch the translator could not render stubbed the
WHOLE measure to ``BLANK()``, so every selection rendered empty, not just the one.

That is the "structurally valid, semantically absent" family. Nothing catches it: the measure
exists, the visual binds, ``pbir_lint`` is clean, validation returns zero errors, and the chart is
blank. Measured on corpus workbook 0088 (salesforce_nonprofit_case_mgmt) at 2.275.0 --
``Sort By`` had 3 of its 4 branches already translated and ``Select Metric`` 3 of 4, yet both
emitted ``BLANK()``; between them they were 5 of the 12 visuals corpus-wide that projected a stub,
the single largest cause of empty charts. Closes #168 / #171.

What ships instead: the dispatcher is rebuilt from the branches that DO translate, and a branch
naming a sibling calc the model emits keeps its slot pointing at that sibling's measure -- blank
today (the sibling is its own ``BLANK()`` stub), correct for free once that sibling's lane lands.

The two boundaries that keep this honest are asserted below, because they are what stop the repair
from becoming the defect: a dispatcher with NO genuinely translated branch stays a stub rather than
becoming a live measure that says nothing, and a repaired dispatcher's still-blank selections are
disclosed in both the model file and the report.
"""
import assemble_model as A
from test_connection_to_m import LIVE_SQLSERVER


def _params():
    return [{
        "caption": "Metric",
        "internal_name": "[Parameter 1]",
        "datatype": "integer",
        "domain": "list",
        "default": "1",
        "range": None,
        "members": ["1", "2", "3"],
        "aliases": {},
    }]


def _build(calcs):
    return A.migrate_tds_to_semantic_model(
        LIVE_SQLSERVER, model_name="Dispatch", calcs=calcs, parameters=_params())


def _measures_tmdl(out):
    return "".join(t for p, t in out["parts"].items() if p.endswith("_Measures.tmdl"))


# [Unmigrated] is a nested FIXED LOD the deterministic tier cannot render -- the real 0088 shape.
_UNMIGRATED = {"name": "Unmigrated",
               "formula": "AVG({FIXED [Region]: MAX({FIXED [Region]: MAX([Sales])})})"}
_LIVE_B = {"name": "Live B", "formula": "SUM([Quantity])"}
# Branch 1 is an AGGREGATE EXPRESSION rather than a bare measure reference, mirroring 0088's
# ``Select Metric`` (``WHEN 1 THEN AVG([Calculation_...])``). That distinction is load-bearing:
# a dispatcher whose every branch is a bare whole-measure reference is claimed UPSTREAM by
# ``detect_field_swap`` and becomes a field-parameter table instead, never reaching the measure
# path at all. This rescue only ever sees the dispatchers that swap detection declined.
_DISPATCHER = {
    "name": "Selected Metric",
    "formula": ("CASE [Parameters].[Metric] WHEN 1 THEN AVG([Sales]) "
                "WHEN 2 THEN [Unmigrated] WHEN 3 THEN [Live B] END"),
}


def test_one_unmigrated_branch_no_longer_blanks_the_whole_dispatcher():
    out = _build([_LIVE_B, _UNMIGRATED, _DISPATCHER])
    tmdl = _measures_tmdl(out)
    # The defect was that this line read `= BLANK()`.
    assert ("measure 'Selected Metric' = SWITCH([Metric Value], 1, AVERAGE('Orders'[Sales]), "
            "2, [Unmigrated], 3, [Live B])") in tmdl
    # ...and the branch that could not translate points at the sibling's own measure, which is
    # still an honest stub. That is what makes the blank selection self-heal later.
    assert "measure Unmigrated = BLANK()" in tmdl
    row = next(r for r in out["report"]["measures"] if r["measure"] == "Selected Metric")
    assert row["status"] == "translated"


def test_a_repaired_dispatcher_discloses_its_blank_selection():
    # A repaired dispatcher counts as translated and so LEAVES needs_review -- if the still-blank
    # selection were not restated here it would vanish from the report entirely, which is the same
    # silent-output failure the repair exists to remove.
    out = _build([_LIVE_B, _UNMIGRATED, _DISPATCHER])
    tmdl = _measures_tmdl(out)
    assert "annotation TranslatedBy = deterministic (parameter dispatcher; 2 of 3 branches live; " \
           "blank until its own calc translates: WHEN 2 -> [Unmigrated])" in tmdl
    ho = out["report"]["translation_handoff"]
    assert ho["summary"]["partial_fidelity"] == 1
    entry = ho["partial_fidelity"][0]
    assert entry["name"] == "Selected Metric"
    assert entry["kind"] == "parameter_dispatcher"
    assert entry["live_branches"] == ["1", "3"]
    assert [b["branch"] for b in entry["blank_branches"]] == ["2"]
    assert entry["blank_branches"][0]["measure"] == "Unmigrated"
    assert entry["dropped_branches"] == []
    # the unmigrated sibling itself is still openly a needs-review calc
    assert "Unmigrated" in [n["name"] for n in ho["needs_review"]]


def test_a_dispatcher_with_no_live_branch_stays_a_stub():
    # THE fail-closed boundary. Rebuilding this would produce SWITCH(..., 1, [Unmigrated], 2,
    # [Unmigrated 2]) -- blank for every selection, exactly as before, but counted as translated
    # and hidden from the review list. Worse than the stub, so it must not happen.
    other = {"name": "Unmigrated 2",
             "formula": "AVG({FIXED [Region]: MAX({FIXED [Region]: MIN([Sales])})})"}
    dead = {"name": "Dead Dispatcher",
            "formula": ("CASE [Parameters].[Metric] WHEN 1 THEN MAX([Unmigrated]) "
                        "WHEN 2 THEN [Unmigrated 2] END")}
    out = _build([_UNMIGRATED, other, dead])
    assert "measure 'Dead Dispatcher' = BLANK()" in _measures_tmdl(out)
    ho = out["report"]["translation_handoff"]
    assert ho["partial_fidelity"] == []
    assert "Dead Dispatcher" in [n["name"] for n in ho["needs_review"]]


def test_a_fully_translatable_dispatcher_is_untouched():
    # The repair is a RESCUE: it only ever runs after the ordinary translation failed, so a
    # dispatcher that always worked keeps its ordinary provenance and gains no disclosure.
    whole = {"name": "Whole Dispatcher",
             "formula": ("CASE [Parameters].[Metric] WHEN 1 THEN AVG([Sales]) "
                         "WHEN 2 THEN [Live B] END")}
    out = _build([_LIVE_B, whole])
    tmdl = _measures_tmdl(out)
    assert ("measure 'Whole Dispatcher' = SWITCH([Metric Value], 1, AVERAGE('Orders'[Sales]), "
            "2, [Live B])") in tmdl
    assert "parameter dispatcher;" not in tmdl
    assert out["report"]["translation_handoff"]["partial_fidelity"] == []


def test_a_build_without_any_dispatcher_gains_nothing():
    # additive: the new channel is present but empty, and no measure gains the new provenance
    out = _build([_LIVE_B, _UNMIGRATED])
    assert "parameter dispatcher;" not in _measures_tmdl(out)
    ho = out["report"]["translation_handoff"]
    assert ho["partial_fidelity"] == []
    assert ho["summary"]["partial_fidelity"] == 0
