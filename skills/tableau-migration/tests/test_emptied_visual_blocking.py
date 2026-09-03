"""An EMPTIED visual renders BLANK and is now `blocking`; a PARTIAL drop is not (#189).

``_crosscheck_report_refs`` drops a projection whose model object was never emitted. When EVERY
binding on a visual goes, it is ``emptied`` -- the visual renders BLANK in Power BI, and the report
STILL PASSES ``powerbi-report-author validate`` cleanly. There is no structural signal anywhere
downstream, so it is visible only by opening the report or by reading this field.

Reported estate-wide at 2.353.0: 23 emptied visuals, **0** worklist items at ``severity: blocking``
(54 high, 17 medium), while 77 items DID earn blocking. ``no_field_bindings`` already blocks on the
same end state reached a different way, which is what made this an omission rather than a policy. In
one workbook, 15 blank visuals sat inside a 152-item worklist while a consumer triaging by severity
saw ``blocking: 7`` and worked those first -- all 15 outside that set.

TWO SIGNALS, and the order matters:

* ``pbip_ref_drops[].severity`` is set STRUCTURALLY at the drop site, from the ``emptied`` boolean
  the engine already computed. That is the authoritative one, and it lets a consumer reading only
  that list triage without cross-referencing the worklist.
* the worklist rule keys on the ``"(visual emptied)"`` tail, because the worklist's only input IS
  prose. It is how the structural fact reaches that surface, not the source of truth.

THE DISTINCTION THIS MUST NOT LOSE: a PARTIAL reference drop emits the same warning WITHOUT that
tail, and must stay non-blocking -- the visual still renders, with fewer fields. Blocking both would
make the blocking set useless for exactly the triage the severity exists for.

SCOPE: our 34-workbook corpus produces ZERO ``pbip_ref_drops`` entries, so every assertion here is
against fixtures and the reporter's estate evidence. Stated rather than implied.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import migrate_estate as M  # noqa: E402
import remediation_worklist as RW  # noqa: E402

EMPTIED = ("visual 'v-page-Sales01' dropped 3 reference(s) the model did not emit: "
           "a, b, c (visual emptied)")
PARTIAL = ("visual 'v-page-Sales01' dropped 1 reference(s) the model did not emit: a")


# ----------------------------------------------------------------- the worklist band

def test_an_emptied_visual_is_blocking():
    cat, sev = RW._classify_warning(EMPTIED.lower())
    assert (cat, sev) == ("emptied_visual", "blocking")


def test_a_PARTIAL_drop_is_not_blocking():
    """The load-bearing carve-out. Same warning, no `(visual emptied)` tail, and the visual still
    renders -- so it must not enter the blocking set."""
    cat, sev = RW._classify_warning(PARTIAL.lower())
    assert sev != "blocking", (cat, sev)


def test_emptied_ranks_with_the_other_render_blockers():
    """`no_field_bindings` already blocks on the same end state reached another way; this makes the
    two agree rather than inventing a new band."""
    assert RW.SEVERITY_RANK["blocking"] < RW.SEVERITY_RANK["high"]
    blocking = {cat for _needles, cat, sev in RW._WARNING_RULES if sev == "blocking"}
    assert {"emptied_visual", "no_field_bindings", "unsupported_visual"} <= blocking


def test_the_remediation_text_names_the_SYMPTOM_and_the_silence():
    """A reader who only sees `blocking` needs to know the visual is blank AND that nothing else
    will tell them -- the clean `validate` is the whole reason this was invisible."""
    hint = RW._remediation("emptied_visual")
    assert "BLANK" in hint
    assert "validates clean" in hint
    assert hint != RW._remediation("other")


def test_the_rule_is_not_absorbed_by_a_looser_one():
    """Ordered table, first match wins. If a looser rule matched `dropped` or `reference` first,
    this would be down-ranked back to where it started."""
    names = [c for _n, c, _s in RW._WARNING_RULES]
    i = names.index("emptied_visual")
    for later in ("field_binding", "filter", "other") :
        if later in names:
            assert i < max(j for j, n in enumerate(names) if n == later), later


# ----------------------------------------------------------------- the structural severity

MODEL = {"definition/tables/Sales.tmdl":
         "table Sales\n\n\tcolumn Amount\n\t\tdataType: double\n\n"
         "\tmeasure Total = SUM(Sales[Amount])\n"}


def _drop_entries(visual_query, model_parts=None):
    """Run the REAL cross-check over one visual and return its `pbip_ref_drops` entries.

    ``model_parts`` must declare at least one object: the cross-check returns early on an empty
    inventory (``no model object inventory -> do not risk false drops``), so passing ``{}`` yields
    zero drops and would make every assertion below vacuous. The first version of this fixture did
    exactly that and the test failed loudly rather than passing on nothing.
    """
    import json
    path = "definition/pages/p/visuals/v/visual.json"
    doc = {"name": "v-page-Sales01",
           "visual": {"visualType": "clusteredBarChart", "query": visual_query}}
    parts, drops, _rebinds = M._crosscheck_report_refs(
        {path: json.dumps(doc)}, MODEL if model_parts is None else model_parts)
    return drops


def _proj(entity, prop, kind="Measure"):
    return {"field": {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}}


def test_the_drop_entry_carries_its_own_severity():
    """The reporter's second ask, and the more robust of the two: a consumer reading only
    `pbip_ref_drops` can triage without cross-referencing the worklist, and the judgement does not
    depend on matching prose."""
    q = {"queryState": {"Y": {"projections": [_proj("_Measures", "NotEmitted")]}}}
    drops = _drop_entries(q)
    assert len(drops) == 1, drops
    d = drops[0]
    assert d["emptied"] is True
    assert d["severity"] == "blocking"
    # additive: the pre-existing keys are untouched
    assert set(d) >= {"visual", "dropped", "emptied"}


def test_a_partially_dropped_visual_gets_high_not_blocking():
    """`high` is the module's own band -- "a data / binding gap that changes what is shown".

    The dropped role must be one the visual type does NOT require. My first fixture dropped `Y`
    from a `clusteredBarChart` and the visual came back `emptied: true` -- correctly, because #143
    empties a visual that loses a REQUIRED role (a chart keeping `Category` and losing `Y` fails
    `validate` with PBIR_ROLE_REQUIRED_MISSING). So `Tooltips` is the honest partial case: the
    reference goes, every required role survives, and the visual still renders.

    Asserted rather than skipped: a skip here would hide the exact case the carve-out exists for.
    """
    q = {"queryState": {
        "Category": {"projections": [_proj("Sales", "Amount", "Column")]},
        "Y": {"projections": [_proj("Sales", "Total")]},
        "Tooltips": {"projections": [_proj("_Measures", "NotEmitted")]}}}
    drops = _drop_entries(q)
    assert len(drops) == 1, drops
    d = drops[0]
    assert d["emptied"] is False, "fixture emptied the visual -- the partial path is untested"
    assert d["severity"] == "high"


def test_losing_a_REQUIRED_role_still_empties_and_therefore_blocks():
    """The #143 interaction, pinned so the two rules stay consistent.

    A visual that keeps `Category` and loses `Y` is emptied on purpose -- keeping it would ship
    PBIR that fails `validate` with PBIR_ROLE_REQUIRED_MISSING. Since it is emptied, it renders
    blank, so it must also carry `blocking`. If these two ever disagree, a blank visual would be
    filed as a partial drop.
    """
    q = {"queryState": {
        "Category": {"projections": [_proj("Sales", "Amount", "Column")]},
        "Y": {"projections": [_proj("_Measures", "NotEmitted")]}}}
    drops = _drop_entries(q)
    assert len(drops) == 1, drops
    assert drops[0]["emptied"] is True
    assert drops[0]["severity"] == "blocking"


def test_severity_is_derived_from_emptied_not_guessed():
    """Pins the two-value mapping at the source, so a refactor cannot quietly make everything
    blocking (which would be the same defect in the other direction)."""
    import inspect
    src = inspect.getsource(M)
    assert '"severity": "blocking" if emptied else "high"' in src
