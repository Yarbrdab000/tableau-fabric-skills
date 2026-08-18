"""A dangling `SelectRef` fails the definition of done, and `pbir_lint` owns which findings are fatal.

R8 detects a formatting property pointing at a projection the visual does not declare. The property
then resolves to nothing, the visual renders with its DEFAULT colours, `powerbi-report-author
validate` returns zero errors, and the run reports success. That is the validate-clean/render-wrong
class this engine keeps getting burned by, so it must fail LOUD rather than be softened to a fidelity
warning.

Two things are pinned here, and the second is the more important:

  * the escalation itself -- a dangling `SelectRef` in `viz_lint` makes a workbook `failed`;
  * that the roster of fatal findings is owned by `pbir_lint.SILENT_RENDER_FINDINGS` and merely READ
    by the definition of done. Deciding it here by grepping a message would be a proxy for "which
    rule fired", which is the same proxy-versus-artifact mistake `REQUIRED_ROLES` exists to avoid,
    and it would let the two sides drift.

Context for the sequencing: R8 shipped in 2.176.0 but `lint_pbir_parts` was reached by 0 of 29
workbooks during an actual migration until 2.190.0 wired it -- so until then the rule guarded the
test suite, not the estate path. This escalates only now that it both runs and is measured green.
"""

import pbir_lint
import migrate_estate as M


DANGLING = ("definition/pages/p/visuals/v1/visual.json: SelectRef names 'colourRule', which this "
            "visual does not project -- the property resolves to nothing, the visual renders with "
            "its defaults and reports no error")
BENIGN = "definition/pages/p/visuals/v1/visual.json: some other advisory finding"


class TestPbirLintOwnsWhichFindingsAreFatal:
    def test_the_roster_is_exported(self):
        assert isinstance(pbir_lint.SILENT_RENDER_FINDINGS, tuple)
        assert pbir_lint.SILENT_RENDER_FINDINGS, "an empty roster silently disables the escalation"

    def test_r8_stamps_the_exported_signature_into_its_finding(self):
        """The roster and the message must not be able to drift apart."""
        visual = {
            "name": "v1",
            "visual": {
                "visualType": "clusteredColumnChart",
                "query": {"queryState": {"Y": {"projections": [{"queryRef": "m0"}]}}},
                "objects": {"dataPoint": [{"properties": {"fill": {"solid": {"color": {
                    "expr": {"SelectRef": {"ExpressionName": "colourRule"}}}}}}}]},
            },
        }
        import json
        problems = pbir_lint.lint_pbir_parts(
            {"definition/pages/p/visuals/v1/visual.json": json.dumps(visual)})
        hits = [p for p in problems if "SelectRef" in p]

        assert hits, "R8 must fire on this shape"
        assert any(mark in hits[0] for mark in pbir_lint.SILENT_RENDER_FINDINGS)


class TestTheDefinitionOfDoneEscalatesIt:
    def test_a_dangling_select_ref_fails_the_workbook(self):
        reason = M._dod_openability_failure({"viz_lint": {"count": 1, "problems": [DANGLING]}})

        assert reason is not None
        assert "not faithfully bound" in reason
        assert "colourRule" in reason, "name the reference so it can be found"

    def test_a_non_fatal_lint_finding_does_not_fail_the_workbook(self):
        """Only findings whose failure mode is silent are fatal; the rest stay fidelity warnings."""
        assert M._dod_openability_failure({"viz_lint": {"count": 1, "problems": [BENIGN]}}) is None

    def test_a_fatal_finding_among_benign_ones_still_fails(self):
        reason = M._dod_openability_failure(
            {"viz_lint": {"count": 3, "problems": [BENIGN, DANGLING, BENIGN]}})

        assert reason is not None

    def test_a_clean_workbook_is_unaffected(self):
        assert M._dod_openability_failure({}) is None
        assert M._dod_openability_failure({"viz_lint": {"count": 0, "problems": []}}) is None
        assert M._dod_openability_failure({"viz_lint": None}) is None

    def test_malformed_lint_never_raises(self):
        for bad in ("nope", [], {"problems": "not-a-list"}, {"problems": [None]}):
            assert M._dod_openability_failure({"viz_lint": bad}) is None


class TestTheEscalationOrderIsPreserved:
    def test_an_unopenable_model_still_outranks_a_lint_finding(self):
        """The model check must not be short-circuited by the new one -- it is the louder failure."""
        reason = M._dod_openability_failure({
            "openability_selfcheck": {"ok": False, "issues": [{"detail": "duplicate column 'X'"}]},
            "viz_lint": {"count": 1, "problems": [DANGLING]},
        })

        assert "model is not openable" in reason

    def test_a_dangling_binding_still_outranks_a_lint_finding(self):
        reason = M._dod_openability_failure({
            "viz_dangling_bindings": {"count": 1, "problems": ["visual binds [Nope]"]},
            "viz_lint": {"count": 1, "problems": [DANGLING]},
        })

        assert "model object(s) that do not exist" in reason

    def test_a_page_less_report_is_still_reported_when_the_lint_is_clean(self):
        reason = M._dod_openability_failure({"pbip_page_count": 0, "viz_lint": {"problems": []}})

        assert "declares no pages" in reason
