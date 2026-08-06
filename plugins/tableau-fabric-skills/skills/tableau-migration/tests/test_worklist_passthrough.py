"""The estate path must CARRY the remediation worklist, not recompute-then-drop it (issue #95).

``twb_to_pbir`` already builds the per-visual remediation worklist
(``remediation_worklist.build_worklist`` over the same warnings + candidate records that
``_viz_fidelity`` summarises) and hands it back on ``result["worklist"]``. But ``migrate_estate`` --
the driver an agent or CI job actually runs -- had ZERO references to it, so the richest
machine-readable artifact the engine produces was computed and then dropped exactly where it would
be read.

The practical cost: a consumer saw ``pending_gates[{gate: "dashboard_audit", count: N}]`` -- HOW MANY
visuals need attention but not WHICH -- and had to re-derive the targets from the emitted PBIR, which
is precisely the drift the worklist exists to prevent.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import migrate_estate as M  # noqa: E402


_WORKLIST = {
    "version": 1,
    "kind": "tableau-fabric-remediation-worklist",
    "summary": {"visuals_total": 2, "visuals_flagged": 1, "items_total": 1},
    "visuals": [{"visual": "v-A", "worksheet": "A"}],
    "items": [{"id": "wl-0000", "severity": "medium", "category": "other",
               "visual": "v-A", "worksheet": "A", "reason": "x"}],
}


def test_a_viz_result_worklist_is_carried_through():
    assert M._viz_worklist({"worklist": _WORKLIST}) == _WORKLIST


def test_the_worklist_is_the_full_per_item_audit_not_a_count():
    """The whole point of the ask: WHICH visuals, not how many. A summary-only projection would
    leave the consumer re-deriving targets from the PBIR."""
    carried = M._viz_worklist({"worklist": _WORKLIST})
    assert carried["items"], "per-item detail must survive"
    assert carried["items"][0]["visual"] == "v-A"
    assert carried["visuals"], "per-visual detail must survive"


def test_absent_or_unusable_worklists_degrade_quietly():
    """Fail-closed and additive: the worklist module is imported with a graceful fallback in
    ``twb_to_pbir``, so a build without it must behave exactly as before rather than raise."""
    assert M._viz_worklist({}) is None
    assert M._viz_worklist({"worklist": None}) is None
    assert M._viz_worklist(None) is None
    assert M._viz_worklist("not a dict") is None
    assert M._viz_worklist({"worklist": ["not", "a", "dict"]}) is None


def test_it_is_not_the_same_thing_as_viz_fidelity():
    """``viz_fidelity`` is a THINNER projection (one row per worksheet); both are kept, because the
    summary is what the report's rollups count and the worklist is what a remediator acts on."""
    result = {
        "worklist": _WORKLIST,
        "ir": {"worksheets": [{"name": "A"}]},
        "warnings": [{"scope": "worksheet", "target": "A",
                      "reason": "manual attention required: something"}],
    }
    fidelity = M._viz_fidelity(result)
    worklist = M._viz_worklist(result)
    assert isinstance(fidelity, list)
    assert isinstance(worklist, dict)
    assert worklist is not fidelity
    # the worklist carries an item id + severity the fidelity summary does not
    assert "severity" in worklist["items"][0]
    assert all("severity" not in row for row in fidelity)
