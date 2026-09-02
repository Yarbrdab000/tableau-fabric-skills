"""The PBIR validity set is DERIVED from the harvested catalog, not hand-maintained (#179).

``pbir_lint`` R4 reports *"unknown visualType %r -- not a valid PBIR built-in visual type (Power BI
renders it as a missing custom visual)"*. Its set was a hand-written literal, while the two role
tables beside it -- ``_REQUIRED_ROLES`` (R9) and ``MEASURE_ROLES`` (R10) -- are HARVESTED from
``powerbi-report-author catalog describe``.

The literal drifted narrower than the catalog. Five types were known to the harvested tables and
absent from the literal, so R4 called each of them invalid:

    cardVisual  filterSlicer  hundredPercentStackedAreaChart  matrix  table

``matrix`` and ``table`` are core Power BI visuals. Our own emitter produces none of the five, which
is exactly why it went unnoticed here -- and why it matters anyway: the linter ships as a tool
consumers run over THEIR OWN reports, and the estate that reported this hand-authors 140
``cardVisual`` visuals in shipped deliverables.

THE FIX IS THE UNION, not five more literals. A hand-maintained set can drift again; a union with
the harvested tables cannot be narrower than them by construction, so adding a type to the catalog
can never again make a valid report look invalid. ``_CURATED_VISUAL_TYPES`` still carries its own
weight: role-LESS types (textbox, image, actionButton, basicShape, and the AI visuals) have no
required roles and appear in neither harvested table.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pbir_lint as PL  # noqa: E402


def _visual(vt):
    return {"definition/pages/p/visuals/v/visual.json":
            json.dumps({"visual": {"visualType": vt}})}


THE_FIVE = ("cardVisual", "filterSlicer", "hundredPercentStackedAreaChart", "matrix", "table")


def test_the_validity_set_is_never_narrower_than_the_harvested_catalog():
    """The invariant, asserted structurally rather than by listing today's members.

    Written this way deliberately: an assertion that names the five types would pass while the
    NEXT type added to the catalog silently reintroduces the same defect. This one cannot.
    """
    valid = set(PL.VALID_VISUAL_TYPES)
    assert set(PL._REQUIRED_ROLES) <= valid, sorted(set(PL._REQUIRED_ROLES) - valid)
    assert set(PL.MEASURE_ROLES) <= valid, sorted(set(PL.MEASURE_ROLES) - valid)
    assert set(PL._CURATED_VISUAL_TYPES) <= valid


def test_the_MEASURE_ROLES_arm_of_the_union_is_currently_REDUNDANT():
    """Records a data fact, and is the reason one of this fix's controls could not go red.

    A control that DELETED the ``set(MEASURE_ROLES)`` arm from the union left every test in this
    file green. That is not a hole in the tests -- it is because ``MEASURE_ROLES`` is a SUBSET of
    ``_REQUIRED_ROLES`` today, so the arm contributes zero members and the assertion
    ``set(MEASURE_ROLES) <= VALID`` above is satisfied vacuously through the other two arms.

    An assertion that cannot fail is not a check, so the honest thing is to name the reason rather
    than let a green run imply coverage it does not have. The arm stays because it costs nothing
    and states the intent (every harvested table bounds the validity set); this test makes the
    redundancy VISIBLE, and the day a harvest adds a measure-only type that is not also in
    ``_REQUIRED_ROLES`` this test fails -- telling you the arm just became load-bearing and the
    control above can finally go red.
    """
    unique = set(PL.MEASURE_ROLES) - set(PL._REQUIRED_ROLES) - set(PL._CURATED_VISUAL_TYPES)
    assert not unique, (
        "MEASURE_ROLES now contributes %r uniquely, so the union's MEASURE_ROLES arm is no longer "
        "redundant. Update this test -- and note the deletion control for that arm can now fail, "
        "which it could not before." % sorted(unique))
    assert set(PL.MEASURE_ROLES) <= set(PL._REQUIRED_ROLES)


def test_the_five_types_that_were_false_positives_are_accepted():
    for vt in THE_FIVE:
        assert vt in PL.VALID_VISUAL_TYPES, vt
        assert not PL._lint_visual_types(_visual(vt)), vt


def test_R4_still_fires_on_a_genuinely_unknown_type():
    """The rule must not have been neutered by widening it.

    Without this, deleting R4's body entirely would satisfy every other test in this file.
    """
    for vt in ("notARealVisual", "barchart", "PivotTable", ""):
        problems = PL._lint_visual_types(_visual(vt))
        if vt == "":
            continue  # an empty type is a different rule's business
        assert problems, "R4 went silent on %r" % vt
        assert "unknown visualType" in problems[0]


def test_the_deliberate_look_alike_exclusions_did_NOT_leak_back_in():
    """The specific hazard of fixing this with a union.

    Power BI spells the stacked variants as the unqualified ``columnChart`` / ``barChart``, so
    ``stackedColumnChart`` / ``stackedBarChart`` are invalid look-alikes the emitter must never
    produce. They are absent from all three source sets, so the union cannot admit them -- asserted
    at every input, because a future harvest that added one would silently un-catch a real defect.
    """
    for vt in ("stackedColumnChart", "stackedBarChart"):
        assert vt not in PL._CURATED_VISUAL_TYPES, vt
        assert vt not in PL._REQUIRED_ROLES, vt
        assert vt not in PL.MEASURE_ROLES, vt
        assert vt not in PL.VALID_VISUAL_TYPES, vt
        assert PL._lint_visual_types(_visual(vt)), vt


def test_role_less_types_come_only_from_the_curated_set():
    """Pins why the curated literal still exists rather than being deleted for the union.

    These have no required data roles, so they are in neither harvested table; dropping the curated
    set would make R4 reject a textbox.
    """
    for vt in ("textbox", "image", "actionButton", "basicShape"):
        assert vt in PL._CURATED_VISUAL_TYPES, vt
        assert vt not in PL._REQUIRED_ROLES and vt not in PL.MEASURE_ROLES, vt
        assert not PL._lint_visual_types(_visual(vt)), vt


def test_everything_our_own_emitter_produces_is_accepted():
    """The property the old comment claimed for the literal ("a strict SUPERSET of what twb_to_pbir
    emits") now holds by construction for the emitter's own vocabulary."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import twb_to_pbir as T
    emitted = {v for k, v in vars(T).items()
               if k.startswith("VT_") and isinstance(v, str)}
    # the internal enum is not the PBIR spelling for every entry, so only assert the ones that ARE
    # PBIR type names -- the corpus-level guarantee is covered by the emitter-clean pytest guard.
    overlap = emitted & set(PL.VALID_VISUAL_TYPES)
    assert overlap, "no VT_* constant matched a PBIR type name -- probe is measuring nothing"
    for vt in sorted(overlap):
        assert not PL._lint_visual_types(_visual(vt)), vt
