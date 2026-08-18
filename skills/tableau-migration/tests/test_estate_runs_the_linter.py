"""#144 follow-up -- the linter must actually RUN during a migration, not only in pytest.

``pbir_lint`` grew rules R3-R9 (unknown visualType, theme-name mismatch, card display units,
nativeQueryRef uniqueness, empty pageOrder, dangling SelectRef, missing required role), and
``lint_pbir_parts`` is the entry point that applies them. Until this change **the estate path never
called it**: ``migrate_estate`` invoked only ``lint_visual_model_bindings`` and read
``REQUIRED_ROLES``, so every one of those rules was inert during an actual migration. They were
exercised in pytest against one representative workbook and nowhere else.

Found by a per-emit-path corpus reach census, which counted how many of the 29 corpus workbooks
reach each decision point. ``lint_pbir_parts`` was reached by **0 of 29** -- alongside
``_lint_required_roles`` (R9) and ``_lint_dangling_select_refs`` (R8), the two rules most recently
added *specifically* to close a "the DoD cannot detect what the engine emits" issue. The fix for
#144 therefore did not run when the engine emitted, which is the #141 shape once more: the value of
a check is decided by whether anything calls it.

This module guards the WIRING rather than the rules. The rules have their own tests; what none of
them could catch is the call site going missing, because every one of them calls ``lint_pbir_parts``
directly.
"""
import re
import os

import pbir_lint


def _migrate_estate_source():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "migrate_estate.py")
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_estate_path_calls_the_linter_entry_point():
    """The defect this module exists for: no production caller at all.

    Deliberately a source-level assertion. A functional test would have to run a full migration,
    and -- more importantly -- would still pass if the call were moved somewhere that never
    executes. What must be true is that the shipping code path names it.
    """
    src = _migrate_estate_source()
    assert re.search(r"lint_pbir_parts\s*\(", src), (
        "migrate_estate must call pbir_lint.lint_pbir_parts -- without it R3-R9 are inert during a "
        "migration and only run in pytest")


def test_the_linter_result_is_recorded_on_the_workbook_entry():
    """A gate whose result is discarded is the same defect one layer along."""
    src = _migrate_estate_source()
    assert '"viz_lint"' in src, "lint findings must be recorded in the report, not just computed"


def test_the_call_is_fail_safe():
    """The linter runs on every migration; a defect in it must not break the build.

    Pins that the call sits inside a ``try`` whose handler yields an empty result, matching the
    existing ``lint_visual_model_bindings`` call beside it.
    """
    src = _migrate_estate_source()
    m = re.search(r"try:\s*\n\s*_lint\s*=\s*_pbir_lint\.lint_pbir_parts\([^\n]*\)\s*\n"
                  r"\s*except Exception:\s*\n\s*_lint\s*=\s*\[\]", src)
    assert m, "the lint_pbir_parts call must be wrapped fail-safe, like its sibling"


def test_every_rule_reachable_from_the_entry_point():
    """Each rule must be wired into ``lint_pbir_parts``, not merely defined.

    This is the same class of defect one level down: a rule that exists but is never called from the
    entry point is invisible for exactly the same reason the entry point being uncalled was.
    """
    import inspect

    body = inspect.getsource(pbir_lint.lint_pbir_parts)
    for rule in ("_lint_visual_types", "_lint_theme", "_lint_card_display_units",
                 "_lint_native_query_refs", "_lint_page_order", "_lint_required_roles",
                 "_lint_dangling_select_refs"):
        assert rule in body, "%s is defined but not reachable from lint_pbir_parts" % rule


def test_the_entry_point_is_clean_on_a_well_formed_report():
    """Sanity: wiring a linter that fires on good output would be worse than not wiring it.

    Measured on the 29-workbook corpus after wiring: 0 workbooks with lint problems.
    """
    parts = {
        "definition/pages/pages.json": '{"pageOrder": ["p"], "activePageName": "p"}',
        "definition/pages/p/visuals/v/visual.json":
            '{"name": "v", "visual": {"visualType": "clusteredColumnChart", "query": '
            '{"queryState": {"Category": {"projections": [{"queryRef": "T.C"}]}, '
            '"Y": {"projections": [{"queryRef": "T.M"}]}}}}}',
    }
    assert pbir_lint.lint_pbir_parts(parts) == []
