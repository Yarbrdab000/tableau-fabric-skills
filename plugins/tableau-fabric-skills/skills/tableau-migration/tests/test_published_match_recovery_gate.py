"""Issue #155: the published-datasource recovery is guarded by the CALLEE, not by the caller.

`_build_datasource_pbip` used to wrap `_rebuild_from_published_match()` in `if descriptor is None:`,
which skipped recovery whenever a combined/federated descriptor was present. A reader measured the
counter-example: a published datasource PLUS one small embedded federated datasource is both things
at once, and the workbook was skipped entirely -- *"published-datasource workbook -- co-migrate its
published datasource"* -- while the published rebuild was available and produced 52 measures / 86.5%
translated once the gate was bypassed.

WHY REMOVING IT CANNOT LOOSEN SAFETY, structurally rather than as a judgement call: that branch only
runs when ``res_report["fallback"]`` is already set -- the model has ALREADY failed. The gate never
protected a good model; it only decided whether to attempt recovery on a build with nothing left to
lose. All the protection lives in the callee, and these tests pin it there so a future change cannot
quietly reintroduce a caller-side condition the callee never asked for.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import migrate_estate as M  # noqa: E402


def _detail(kind="published", name="Sales Extract"):
    return {"binding_signal": {"kind": kind, "published_ds_name": name}}


def test_the_callee_takes_no_descriptor_at_all():
    """The caller's old fourth condition was about a value the callee cannot even see."""
    params = list(inspect.signature(M._rebuild_from_published_match).parameters)
    assert "descriptor" not in params
    assert params[:4] == ["detail", "twb_text", "model_safe", "ds_catalog"]


def test_no_catalog_refuses():
    assert M._rebuild_from_published_match(_detail(), "<workbook/>", "m", None) is None
    assert M._rebuild_from_published_match(_detail(), "<workbook/>", "m", {}) is None


def test_a_non_published_binding_signal_refuses():
    """Only a workbook actually bound to a published datasource may be rebuilt from one."""
    for kind in ("embedded", "flatfile", None, ""):
        assert M._rebuild_from_published_match(
            _detail(kind=kind), "<workbook/>", "m", {"sales extract": {"ok": 1}}) is None, kind


def test_a_name_that_does_not_match_refuses():
    assert M._rebuild_from_published_match(
        _detail(name="Something Else"), "<workbook/>", "m",
        {M._norm_ds("Sales Extract"): {"ok": 1}}) is None


def test_an_AMBIGUOUS_match_refuses():
    """Two published datasources normalising to one name is exactly when guessing would be wrong."""
    cat = {M._norm_ds("Sales Extract"): {"__ambiguous__": True}}
    assert M._rebuild_from_published_match(_detail(), "<workbook/>", "m", cat) is None


def test_the_caller_no_longer_gates_the_recovery_on_descriptor():
    """Reads the source of the calling function, because the condition being ABSENT is the fix.

    A behavioural test would need a published datasource plus a federated embedded one, and the
    corpus has no such workbook -- so this asserts the narrower thing it can actually prove, and says
    so rather than implying more. Pinned because the gate is a single line that reads perfectly
    reasonable and was reintroduced-by-plausibility once already.
    """
    src = inspect.getsource(M._build_datasource_pbip)
    call = src.index("_rebuild_from_published_match(")
    window = src[max(0, call - 400):call]
    assert "if descriptor is None:" not in window, (
        "the published-datasource recovery is gated on `descriptor` again; the callee's own three "
        "guards are the safety model (see #155)")
