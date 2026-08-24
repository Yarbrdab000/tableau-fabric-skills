"""#173: a per-visual status must say WHAT WAS CHECKED, not just that our code ran.

``status: "rebuilt"`` asserts only that the visual emitter completed without raising and without
attaching a warning. It is a claim about the ENGINE, not about the emitted artifact -- so a visual
can be reported ``rebuilt`` and still fail to render. Reported from the field as a scatter marked
``rebuilt`` while Desktop showed a live ``DataViewMappingError_ScatterGroupingValues``.

That is the same distinction recorded in migration-gotchas.md as *"read every confirmation at the
artifact, never at the mechanism"* -- and it is worse here than in the other instances, because this
one is PUBLISHED as a per-visual verdict that downstream consumers reasonably read as "this visual is
fine".

The honest answer was already computed and discarded: ``lint_pbir_parts`` runs on the SHIPPED report
bytes a few lines below where ``viz_fidelity`` is built. ``rebuilt`` could not see it purely because
of ORDERING.

Additive by design, in the same spirit as ``tier``: ``status`` is not narrowed and no existing key
changes. Narrowing ``rebuilt`` would be more honest still, but it is a breaking change to a field
other teams consume, so it is theirs to opt into rather than ours to impose.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from migrate_estate import _annotate_fidelity_evidence  # noqa: E402


def _rows():
    return [{"worksheet": "Scatter by Hour", "status": "rebuilt", "tier": "rebuilt"},
            {"worksheet": "Regular Sum", "status": "rebuilt", "tier": "rebuilt"}]


RECORDS = [{"worksheet": "Scatter by Hour", "visual": "v-ScatterbyHour1a2b"},
           {"worksheet": "Regular Sum", "visual": "v-RegularSum9f8e"}]

PROBLEM = ("definition/pages/page-ws-Scatter/visuals/v-ScatterbyHour1a2b/visual.json: "
           "SelectRef names 'foo' which no projection declares")


def test_a_linted_clean_visual_says_so():
    rows = _annotate_fidelity_evidence(_rows(), [], RECORDS, True)
    assert [r["evidence"] for r in rows] == ["emitted+linted", "emitted+linted"]


def test_a_visual_a_lint_finding_NAMES_is_flagged_even_though_status_stays_rebuilt():
    """The false-green case, which is the whole point of the issue.

    ``status`` deliberately still reads ``rebuilt`` -- narrowing it is a breaking change -- so the
    disclosure has to live in the new field, and the pair must be readable together.
    """
    rows = _annotate_fidelity_evidence(_rows(), [PROBLEM], RECORDS, True)
    scatter = next(r for r in rows if r["worksheet"] == "Scatter by Hour")
    clean = next(r for r in rows if r["worksheet"] == "Regular Sum")
    assert scatter["status"] == "rebuilt"          # unchanged: additive, nothing breaks
    assert scatter["evidence"] == "lint_failed"    # ...but no longer a silent green
    assert clean["evidence"] == "emitted+linted"   # the sibling is unaffected


def test_when_the_lint_did_not_run_nothing_claims_it_did():
    """THE LOAD-BEARING REFUSAL.

    Claiming ``emitted+linted`` because no problems were found -- when the reason no problems were
    found is that nothing looked -- would recreate the exact defect this fixes, one level up. An
    absent check and a passed check must never produce the same output.
    """
    rows = _annotate_fidelity_evidence(_rows(), [], RECORDS, False)
    assert [r["evidence"] for r in rows] == ["emitted", "emitted"]


def test_an_unattributable_lint_problem_does_not_flag_an_innocent_worksheet():
    """A finding naming a visual we cannot map back to a worksheet must not be smeared across all of
    them: a wrong attribution is worse than none, because it sends someone to the wrong sheet."""
    rows = _annotate_fidelity_evidence(
        _rows(), ["definition/pages/page-x/visuals/v-Unknown0000/visual.json: something"],
        RECORDS, True)
    assert all(r["evidence"] == "emitted+linted" for r in rows)


def test_status_and_tier_are_never_modified():
    """Additive contract, asserted rather than assumed -- existing consumers read these."""
    before = _rows()
    after = _annotate_fidelity_evidence(_rows(), [PROBLEM], RECORDS, True)
    for b, a in zip(before, after):
        assert a["status"] == b["status"]
        assert a["tier"] == b["tier"]


def test_missing_or_malformed_input_does_not_raise():
    assert _annotate_fidelity_evidence(None, [], RECORDS, True) == []
    assert _annotate_fidelity_evidence([], [], None, True) == []
    rows = _annotate_fidelity_evidence([{"worksheet": "W"}], None, None, True)
    assert rows[0]["evidence"] == "emitted+linted"
