"""The openability verdict must describe the model that SHIPPED, not an earlier one.

`check_model_openability` runs during the datasource build. `_apply_row_predicate_wrapped_measures`
then ADDS measures to `_Measures.tmdl` — after that verdict was computed and recorded. So the gate
examined a model state in which a wrapper-introduced defect *cannot exist*, and the build shipped a
clean pass that was true about an earlier artifact and false about the one on disk.

Measured on corpus workbook 0088 at 2.310.0, before this fix:

    openability_selfcheck.measure_value_path_not_blank      : true
    wrapper measures on that same build that render empty   : 8

    openability_selfcheck.bare_column_references_qualified   : true
    wrappers emitting CALCULATE([<a COLUMN>], ...)           : 1

Both checks are correct. Both ran too early. This is not a check that failed to notice a defect —
it is a check whose *satisfaction* was never evidence about the shipped model, because the model
changed underneath it. A passing gate is evidence only about the artifact it examined.

The merge rule is derived rather than enumerated (see `_recheck_openability_after_wrap`): a named
allowlist of "checks the wrap can affect" would freeze a scope decision into a list and silently
stop covering the tip the first time the wrap or the gate grew a new interaction.
"""
import migrate_estate as M

_RECHECK = M._recheck_openability_after_wrap


def _verdict(checks, issues=()):
    return {"ok": all(checks.values()), "checks": dict(checks), "issues": list(issues)}


# A model whose wrapper forwards to a BLANK() stub: live expression, empty value path.
_WRAPPED = {
    "definition/tables/_Measures.tmdl":
        "table _Measures\n"
        "\n\tmeasure 'Base' = BLANK()\n"
        "\n\tmeasure 'Base (filtered)' = CALCULATE([Base], FILTER('T', 'T'[D] >= [Start Value]))\n"
        "\n\tmeasure 'Start Value' = 1\n",
}
_UNWRAPPED = {
    "definition/tables/_Measures.tmdl":
        "table _Measures\n"
        "\n\tmeasure 'Base' = BLANK()\n"
        "\n\tmeasure 'Start Value' = 1\n",
}


def test_a_defect_the_wrap_introduced_is_folded_into_the_verdict():
    # the recorded verdict is the pre-wrap one: clean, and true about a model that was not shipped
    before = _verdict({"measure_value_path_not_blank": True, "tmdl_wellformed": True})
    after = _RECHECK(_WRAPPED, before)
    assert after["checks"]["measure_value_path_not_blank"] is False
    assert after["ok"] is False
    assert any(i.get("check") == "measure_value_path_not_blank" for i in after["issues"])
    assert "Base (filtered)" in " ".join(str(i.get("detail")) for i in after["issues"])


def test_the_recorded_verdict_is_not_mutated():
    # the caller may still be holding it; a diagnostic pass must not edit its input
    before = _verdict({"measure_value_path_not_blank": True})
    snapshot = {"ok": before["ok"], "checks": dict(before["checks"]),
                "issues": list(before["issues"])}
    _RECHECK(_WRAPPED, before)
    assert before["checks"] == snapshot["checks"]
    assert before["ok"] == snapshot["ok"] and before["issues"] == snapshot["issues"]


def test_a_wrap_that_breaks_nothing_leaves_the_verdict_untouched():
    # the byte-identical guarantee for every build whose wrappers are all healthy
    before = _verdict({"measure_value_path_not_blank": True, "tmdl_wellformed": True})
    assert _RECHECK(_UNWRAPPED, before) is before


def test_the_recheck_can_never_CLEAR_a_recorded_failure():
    # THE fail-closed direction. The re-run omits `flatfile_headers` / `expected_endpoints`, so
    # checks needing them are skipped and would otherwise read as "no longer failing" — turning a
    # real defect into a pass, which is strictly worse than the gap this fixes.
    before = _verdict({"measure_value_path_not_blank": True,
                       "typed_columns_in_header": False,
                       "endpoints_distinct": False})
    after = _RECHECK(_WRAPPED, before)
    assert after["checks"]["typed_columns_in_header"] is False
    assert after["checks"]["endpoints_distinct"] is False
    assert after["checks"]["measure_value_path_not_blank"] is False   # the new one still lands


def test_a_check_absent_from_the_rerun_keeps_its_recorded_verdict():
    before = _verdict({"measure_value_path_not_blank": True, "endpoints_distinct": True})
    after = _RECHECK(_WRAPPED, before)
    assert after["checks"]["endpoints_distinct"] is True      # not in the re-run -> untouched


def test_issues_are_appended_never_replaced():
    prior = {"check": "wrapper_keeps_base_format_string", "detail": "a pre-existing finding"}
    before = _verdict({"measure_value_path_not_blank": True,
                       "wrapper_keeps_base_format_string": False}, [prior])
    after = _RECHECK(_WRAPPED, before)
    assert prior in after["issues"]
    assert len(after["issues"]) > 1


def test_malformed_input_returns_the_recorded_verdict_unchanged():
    # a build must never LOSE its openability report because this pass could not run
    before = _verdict({"measure_value_path_not_blank": True})
    for bad in (None, "not a dict", 42, {}):
        assert _RECHECK(bad, before) is before
    assert _RECHECK(_WRAPPED, None) is None
    assert _RECHECK(_WRAPPED, {"checks": "not a dict"}) == {"checks": "not a dict"}
