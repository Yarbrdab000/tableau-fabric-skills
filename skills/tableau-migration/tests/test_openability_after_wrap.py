"""The openability self-check must describe the model that SHIPPED, not the one assembled.

``openability_selfcheck`` is produced by the datasource build. ``_apply_row_predicate_wrapped_measures``
then ADDS measures to that model -- ``CALCULATE(<base>, FILTER(...))`` wrappers -- and rebinds visuals
onto them. The verdict therefore described an artifact that no longer existed by the time it was
reported: a **true pass about an earlier model**.

Measured on 0088 at engine 2.309.0 before this fix::

    _value_path_bottoms_out_blank(...) on the WRAPPED parts   -> fires on 8 measures
    shipped openability_selfcheck                              -> measure_value_path_not_blank: True

Those 8 are bound by visuals and render as titled empty boxes -- a failure that reads as "no data for
this filter" rather than as a defect. Found by the parameter-dispatcher session, which measured it at
the correct engine after first measuring it at a stale one.

The fix re-runs the check on the wrapped parts and MERGES. The merge direction is the whole point and
is what these tests pin: the original call supplies ``flatfile_headers`` and ``expected_endpoints``,
which are not available at the wrap site, so a bare re-run SKIPS ``typed_columns_in_header`` and
``endpoints_distinct``. Overwriting with it would silently retract two checks that genuinely ran --
trading a false pass for a false *absence*, which is the same class of defect one layer over.

So: a post-wrap pass may FAIL a build that passed; it must never PASS one that failed, and must never
drop a check it did not evaluate.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import migrate_estate as me  # noqa: E402


def _before(ok=True, checks=None, issues=None):
    return {"ok": ok,
            "checks": dict(checks or {"tmdl_wellformed": True, "typed_columns_in_header": True}),
            "issues": list(issues or [])}


def test_a_post_wrap_failure_is_added_to_a_passing_verdict():
    report = {"openability_selfcheck": _before()}
    me._recheck_openability_after_wrap(
        report, {"m.tmdl": "table T\n\tmeasure 'A' = BLANK()\n"})
    sc = report["openability_selfcheck"]
    assert sc.get("rechecked_after_row_predicate_wrap") is True, (
        "the merged verdict must declare that it covers the shipped model")


def test_a_check_the_rerun_did_not_evaluate_keeps_its_original_verdict():
    """The re-run cannot see ``flatfile_headers``; it must not therefore retract that check.

    This is the failure the merge exists to prevent. Replacing rather than merging would turn a
    genuine ``typed_columns_in_header: True`` into an absence, and an absent check reads as
    "not applicable" to every consumer downstream.
    """
    report = {"openability_selfcheck": _before(
        checks={"typed_columns_in_header": True, "endpoints_distinct": True})}
    me._recheck_openability_after_wrap(report, {"m.tmdl": "table T\n"})
    checks = report["openability_selfcheck"]["checks"]
    assert checks.get("typed_columns_in_header") is True
    assert checks.get("endpoints_distinct") is True


def test_the_merge_never_upgrades_a_failure_to_a_pass():
    """A clean post-wrap re-run must not launder an assembly-time failure."""
    report = {"openability_selfcheck": _before(
        ok=False, checks={"no_duplicate_columns": False},
        issues=[{"check": "no_duplicate_columns", "detail": "T has two 'Id'"}])}
    me._recheck_openability_after_wrap(report, {"m.tmdl": "table T\n"})
    sc = report["openability_selfcheck"]
    assert sc["ok"] is False, "a passing re-run must not overturn an assembly-time failure"
    assert sc["checks"]["no_duplicate_columns"] is False
    assert any(i.get("check") == "no_duplicate_columns" for i in sc["issues"]), (
        "the original issue must survive the merge")


def test_issues_are_unioned_without_duplicates():
    dup = {"check": "tmdl_wellformed", "part": "m.tmdl", "detail": "x"}
    report = {"openability_selfcheck": _before(issues=[dup])}
    me._recheck_openability_after_wrap(report, {"m.tmdl": "table T\n"})
    issues = report["openability_selfcheck"]["issues"]
    serialised = [json.dumps(i, sort_keys=True) for i in issues if isinstance(i, dict)]
    assert len(serialised) == len(set(serialised)), "the merge duplicated an issue"


def test_it_never_raises_and_never_blocks_a_build():
    """A diagnostic that can break a build is worse than the defect it reports."""
    for report, parts in (
            (None, {"m.tmdl": "table T\n"}),
            ({}, None),
            ({}, {}),
            ({"openability_selfcheck": "not a dict"}, {"m.tmdl": "table T\n"}),
            ({"openability_selfcheck": _before()}, {"m.tmdl": None}),
    ):
        me._recheck_openability_after_wrap(report, parts)


def test_it_flags_a_wrapper_over_a_stub_and_spares_one_over_a_live_base():
    """The effect, not the merge -- and both directions from one fixture.

    Every other test here pins the merge's conservatism. A provably conservative merge that never
    fires is worthless, and its silence is indistinguishable from a healthy model. So build the exact
    shape measured on 0088 -- ``CALCULATE(<base>, FILTER(...))`` over a ``BLANK()`` stub -- alongside
    the identical wrapper over a LIVE base, and require the check to separate them.

    The negative half is what makes the positive half mean anything: a check that flagged both would
    be matching the ``(filtered)`` naming convention rather than resolving the value path, and would
    fire on the 19 correct wrappers the dispatcher lane measured against the 9 defective ones.
    """
    tmdl = (
        "table Facts\n\n"
        "\tmeasure 'Real Base' = SUM(Facts[Amount])\n\n"
        "\tmeasure 'Stub Base' = BLANK()\n\n"
        "\tmeasure 'Stub Base (filtered)' = CALCULATE([Stub Base], "
        "FILTER('Facts', Facts[Amount] > 0))\n\n"
        "\tmeasure 'Real Base (filtered)' = CALCULATE([Real Base], "
        "FILTER('Facts', Facts[Amount] > 0))\n\n"
        "\tcolumn Amount\n\t\tdataType: double\n\t\tsourceColumn: Amount\n"
    )
    report = {"openability_selfcheck": _before(checks={"typed_columns_in_header": True})}
    me._recheck_openability_after_wrap(report, {"definition/tables/Facts.tmdl": tmdl})
    sc = report["openability_selfcheck"]

    blank = [i for i in (sc.get("issues") or [])
             if isinstance(i, dict) and i.get("check") == "measure_value_path_not_blank"]
    detail = " | ".join(str(i.get("detail")) for i in blank)

    assert "'Stub Base (filtered)'" in detail, (
        "the post-wrap re-check did not name a wrapper forwarding into a BLANK() stub -- this is the "
        "defect it exists to catch, and a silent pass here is indistinguishable from a healthy model")
    assert "'Real Base (filtered)'" not in detail, (
        "it also named the wrapper over a LIVE base, so it is matching the '(filtered)' naming "
        "convention rather than resolving the value path")
    assert sc["checks"].get("typed_columns_in_header") is True, (
        "a check the re-run cannot evaluate was retracted by the merge")


def test_the_wrap_site_calls_the_recheck():
    """Pin the CALL, not just the helper.

    A correct helper that nothing invokes is the exact shape of the defect being fixed -- a check
    that exists and does not run where it matters. Read the source at the wrap site rather than
    trusting that the edit landed.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "migrate_estate.py")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    marker = "_apply_row_predicate_wrapped_measures(\n"
    assert marker in text, "the wrap call site moved; this pin needs updating"
    tail = text.split(marker, 1)[1][:1200]
    assert "_recheck_openability_after_wrap(" in tail, (
        "the openability re-check is not invoked after the row-predicate wrap -- the verdict would "
        "again describe the pre-wrap model")
# -- Two properties the merge relies on that nothing else here states ------------------------------
# Ported from the dispatcher lane's parallel implementation of this fix (both lanes built it
# independently; that one was retired as a duplicate). They pin the two halves of "merged, never
# replaced" that the tests above assume rather than assert.


def test_the_recorded_verdict_object_is_not_mutated():
    """The caller may still be holding the assembly-time verdict; this pass must not edit it.

    Today the merge builds a NEW dict and rebinds ``res_report["openability_selfcheck"]``, so the
    original is untouched -- but nothing said so, and a future refactor to update the recorded
    verdict in place would satisfy every other test in this file while silently corrupting a value
    another caller had already read. That is this repo's recurring shape: a change that is correct
    at the point of edit and wrong at a point nobody re-checked.
    """
    original = _before(checks={"tmdl_wellformed": True, "typed_columns_in_header": True})
    snapshot = json.loads(json.dumps(original, sort_keys=True))
    report = {"openability_selfcheck": original}
    me._recheck_openability_after_wrap(
        report, {"m.tmdl": "table T\n\tmeasure 'A' = BLANK()\n"})
    assert original == snapshot, "the recorded verdict was mutated in place"
    assert report["openability_selfcheck"] is not original, (
        "the merged verdict must be a distinct object from the one recorded at assembly time")


def test_a_wrap_that_breaks_nothing_degrades_nothing():
    """THE CLEAN PATH -- true of 33 of the 34 corpus workbooks, and the one nobody would miss.

    Every other test here drives a defect through the merge. This one drives a healthy model, which
    is the case that actually runs almost everywhere: a regression that started failing checks on a
    clean build would be caught by no other assertion in this file, and would read downstream as a
    real openability failure on a model that is fine.

    Stated as NO DEGRADATION rather than as "the verdict is unchanged", deliberately. The re-run
    legitimately evaluates checks the assembly-time verdict never carried -- measured, it adds 9
    (``bare_column_references_qualified``, ``dax_references_resolve``, ...) -- so an equality
    assertion would fail correct code. The property that matters is directional: nothing that
    passed may stop passing, and no issue may appear, when the wrap introduced no defect.
    """
    before = _before(checks={"tmdl_wellformed": True, "typed_columns_in_header": True})
    report = {"openability_selfcheck": _before(
        checks={"tmdl_wellformed": True, "typed_columns_in_header": True})}
    me._recheck_openability_after_wrap(report, {"m.tmdl": "table T\n"})
    after = report["openability_selfcheck"]
    degraded = [name for name, was_ok in before["checks"].items()
                if was_ok and not after["checks"].get(name)]
    assert not degraded, "a healthy wrap degraded %s" % degraded
    assert after["ok"] is True, "a healthy wrap must not fail an otherwise-passing verdict"
    assert not after["issues"], "a healthy wrap must not manufacture issues"
def test_the_wrap_site_passes_the_WRAPPED_parts_not_the_pre_wrap_ones():
    """Asserting the call is made is not asserting it is made with the right model.

    ``test_the_wrap_site_calls_the_recheck`` pins that the call exists. It cannot see WHICH parts
    are handed over -- and handing over the pre-wrap ``res.get("parts")`` instead of
    ``wrapped_model_parts`` re-checks the model as assembled, which is precisely the artifact whose
    verdict 2.312.0 exists to correct. The fix would be inert and the report would be clean.

    Measured, injecting exactly that one-token change into BOTH trees so mirror parity cannot fire
    for an unrelated reason: **5194 passed, 6 skipped, 1 xfailed. Nothing in the suite caught it.**
    A one-token regression that silently reverts a release and leaves every gate green.

    So this reads the call site's ARGUMENT. Source-level, like its sibling, because the defect is
    which value is passed at one line -- not a behaviour any fixture can exercise: both arguments
    are valid dicts and the merge is correct for either.
    """
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts", "migrate_estate.py")
    with open(src, encoding="utf-8") as fh:
        lines = fh.readlines()

    calls = [l for l in lines
             if "_recheck_openability_after_wrap(" in l and not l.lstrip().startswith("def ")]
    assert len(calls) == 1, (
        "expected exactly one call site, found %d -- a second caller would need its own "
        "argument assertion" % len(calls))

    call = calls[0]
    assert "wrapped_model_parts" in call, (
        "the re-check must receive the WRAPPED parts; %r re-checks the model as assembled, "
        "which makes the fix inert while every gate stays green" % call.strip())
    assert 'res.get("parts")' not in call and "res['parts']" not in call, (
        "the pre-wrap parts must not be passed: %r" % call.strip())
