"""A PBIR with no pages does not open EMPTY -- it CRASHES Power BI Desktop.

Measured: a report whose ``pages.json`` carries ``"pageOrder": []`` makes Desktop throw
``TypeError: Cannot read properties of undefined (reading 'visualContainers')`` and refuse the
project outright. That is worse than an empty report, because the semantic model built beside it in
the same ``.pbip`` becomes unreachable too -- a correct model, lost to a missing page.

A workbook reaches that state whenever every worksheet defers (an unsupported mark, a chart missing
a required role, a Measure Names shape we decline to guess). Measured on ``Logic example 4`` at this
branch's base (``56a7ef5``, skill 2.141.0): its shipped ``.pbip`` carried ``pageOrder: []`` with no
page folders at all, and the definition-of-done reported it as ``warn`` rather than a failure.

Three guards, because the first alone is not enough to keep it from coming back:

1. The emitter ships one placeholder page when nothing was rebuilt -- SCOPED so the emit gate's own
   contract is untouched: an unsupported mark still emits NO visual. The placeholder adds the
   container Desktop requires, never a visual the gate refused.
2. ``pbir_lint`` flags an empty ``pageOrder``. ``powerbi-report-author validate`` does catch this
   (``PBIR_PAGE_ORDER_EMPTY``), but it is an opt-in npm pre-gate an ordinary run never reaches,
   whereas the hermetic linter runs in the always-on pytest gate.
3. The definition-of-done reports a page-less report as ``failed``, not softened to a fidelity
   ``warn`` -- it is an openability failure, the same class as a model that will not load.
"""
import json

import migrate_estate as E
import pbir_lint
from pbir_lint import lint_pbir_parts
from twb_to_pbir import emit_pbir, parse_twb

from test_twb_to_pbir import _INST, _visual_parts, _workbook, _worksheet


def _fully_deferred_ir():
    """A workbook whose only worksheet defers -- a Gantt mark, which has no Power BI equivalent."""
    ws = _worksheet("Gantt Only", "Gantt",
                    rows="[federated.abc].[sum:Sales:qk]",
                    cols="[federated.abc].[none:Category:nk]",
                    deps_extra=_INST)
    return parse_twb(_workbook(ws))


class TestTheEmitterAlwaysShipsAPage:
    def test_a_fully_deferred_workbook_still_declares_a_page(self):
        ir = _fully_deferred_ir()
        assert ir["worksheets"][0]["visual_type"] == "unsupported"
        pages = json.loads(emit_pbir(ir)["definition/pages/pages.json"])
        assert pages["pageOrder"], "a page-less PBIR crashes Desktop on open"
        assert pages["activePageName"] == pages["pageOrder"][0]

    def test_the_placeholder_carries_no_visual_so_the_emit_gate_is_unchanged(self):
        # THE SCOPING. These deferrals are correct and must stay correct: the guard supplies a
        # container, never a visual the gate refused.
        parts = emit_pbir(_fully_deferred_ir())
        assert _visual_parts(parts) == {}

    def test_the_placeholder_page_is_schema_shaped_and_self_describing(self):
        parts = emit_pbir(_fully_deferred_ir())
        page_paths = [p for p in parts if p.endswith("page.json")]
        assert len(page_paths) == 1
        page = json.loads(parts[page_paths[0]])
        assert page["displayName"] == "No visuals rebuilt"
        assert page["name"] == json.loads(parts["definition/pages/pages.json"])["pageOrder"][0]

    def test_the_placeholder_is_disclosed_not_silent(self):
        ir = _fully_deferred_ir()
        emit_pbir(ir)
        assert any(w["scope"] == "workbook" and "placeholder page" in w["reason"]
                   for w in ir["warnings"])

    def test_a_workbook_that_rebuilds_normally_gains_no_placeholder(self):
        ws = _worksheet("Sales by Category", "Bar",
                        rows="[federated.abc].[sum:Sales:qk]",
                        cols="[federated.abc].[none:Category:nk]",
                        deps_extra=_INST)
        parts = emit_pbir(parse_twb(_workbook(ws)))
        pages = json.loads(parts["definition/pages/pages.json"])
        assert len(pages["pageOrder"]) == 1
        assert "No visuals rebuilt" not in json.dumps(
            [json.loads(v) for k, v in parts.items() if k.endswith("page.json")])


class TestTheLinterCatchesItComingBack:
    def _parts(self, order):
        return {"definition/pages/pages.json": json.dumps(
            {"pageOrder": order, "activePageName": order[0] if order else ""})}

    def test_an_empty_page_order_is_flagged(self):
        problems = lint_pbir_parts(self._parts([]))
        assert len(problems) == 1
        assert "pageOrder is empty" in problems[0]
        assert "visualContainers" in problems[0], "name the Desktop crash it prevents"

    def test_a_report_with_a_page_is_clean(self):
        assert lint_pbir_parts(self._parts(["p"])) == []

    def test_a_build_with_no_report_at_all_is_not_a_page_less_report(self):
        assert lint_pbir_parts({}) == []
        assert pbir_lint._lint_page_order({"definition/model.tmdl": "table x"}) == []

    def test_a_malformed_pages_json_is_skipped_not_raised_on(self):
        assert pbir_lint._lint_page_order({"definition/pages/pages.json": "{not json"}) == []

    def test_the_emitted_placeholder_satisfies_the_linter(self):
        assert [p for p in lint_pbir_parts(emit_pbir(_fully_deferred_ir())) if "pageOrder" in p] == []


class TestTheDefinitionOfDoneFailsLoud:
    def test_a_page_less_report_is_an_openability_failure(self):
        reason = E._dod_openability_failure({"pbip_status": "built", "pbip_page_count": 0})
        assert reason and "no pages" in reason
        assert "not openable" in reason

    def test_a_report_with_pages_is_not_a_failure(self):
        assert E._dod_openability_failure({"pbip_status": "built", "pbip_page_count": 3}) is None

    def test_an_unknown_page_count_never_manufactures_a_failure(self):
        # Fail-safe: an unreadable report must not invent an openability failure.
        assert E._dod_openability_failure({"pbip_status": "built"}) is None
        assert E._dod_openability_failure({"pbip_status": "built", "pbip_page_count": None}) is None

    def test_a_model_openability_failure_still_wins(self):
        reason = E._dod_openability_failure({
            "openability_selfcheck": {"ok": False, "issues": [{"detail": "duplicate column",
                                                              "table": "Orders"}]},
            "pbip_page_count": 2})
        assert "duplicate column" in reason


    def test_a_dangling_visual_binding_is_a_loud_failure(self):
        # A visual bound to a model object that does not exist renders EMPTY (or, for a conditional
        # format, silently unpainted) and validates with 0 errors. The lint always DETECTED it; it
        # was softened to a fidelity warning, so a workbook could ship bound to nothing and be
        # called done. Escalated only once the corpus measured 0 such references, so it is inert on
        # green and can fire only on a regression.
        reason = E._dod_openability_failure({
            "pbip_status": "built", "pbip_page_count": 1,
            "viz_dangling_bindings": {"count": 2, "problems": [
                "PBIR_VISUAL_REF_MISSING: ... binds measure 'New Max1? (colour)' ..."]}})
        assert reason and "do not exist" in reason
        assert "New Max1? (colour)" in reason, "name the first offender"

    def test_no_dangling_bindings_is_not_a_failure(self):
        assert E._dod_openability_failure(
            {"pbip_status": "built", "pbip_page_count": 1}) is None
        assert E._dod_openability_failure(
            {"pbip_status": "built", "pbip_page_count": 1,
             "viz_dangling_bindings": {"count": 0, "problems": []}}) is None

    def test_the_page_check_still_runs_after_the_dangling_check(self):
        # ordering guard: the dangling check must not short-circuit the page-less check below it
        reason = E._dod_openability_failure(
            {"pbip_status": "built", "pbip_page_count": 0,
             "viz_dangling_bindings": {"count": 0}})
        assert reason and "no pages" in reason