"""Every distinct warning on a worksheet has to reach the report, not just the first one.

`_viz_fidelity` collapsed a worksheet's warnings with `setdefault`, so a sheet that failed in
several distinct ways -- an unbound row count, a dropped filter, a deferred visual -- was reported
with exactly ONE cause and no signal that others existed. That is the honesty contract inverted: a
reader who fixes the reported cause has no way to learn the sheet is still wrong.

It also hid the most SPECIFIC finding whenever a coarser one happened to be recorded first.
Measured: a `table-calc filter` diagnosis -- the class where a wrong rebuild is silently wrong --
disappeared behind a generic "no usable field bindings" note on the same sheet, so the report named
the least useful of the two.

The fix is additive on purpose. `reason` still carries the first warning byte-for-byte, so every
existing consumer and every existing count is unchanged; the remainder appear under a new
`additional_reasons` key that is present only when there is more to say.
"""
import migrate_estate as M


def _result(warnings, worksheets=None):
    return {"ir": {"worksheets": worksheets if worksheets is not None
                   else [{"name": "S1", "visual_type": "matrix"}]},
            "warnings": warnings}


def _warn(name, reason, scope="worksheet"):
    return {"scope": scope, "name": name, "reason": reason}


def _row(rows, ws):
    return next(r for r in rows if r["worksheet"] == ws)


def test_single_warning_row_is_unchanged():
    rows = M._viz_fidelity(_result([_warn("S1", "only cause")]))
    r = _row(rows, "S1")
    assert r["status"] == "warned"
    assert r["reason"] == "only cause"
    assert "additional_reasons" not in r        # absent when there is nothing more to say


def test_second_distinct_warning_is_no_longer_lost():
    rows = M._viz_fidelity(_result([_warn("S1", "first"), _warn("S1", "second")]))
    r = _row(rows, "S1")
    assert r["reason"] == "first"               # primary reason byte-identical
    assert r["additional_reasons"] == ["second"]


def test_all_further_warnings_are_kept_in_order():
    rows = M._viz_fidelity(_result(
        [_warn("S1", "a"), _warn("S1", "b"), _warn("S1", "c"), _warn("S1", "d")]))
    assert _row(rows, "S1")["additional_reasons"] == ["b", "c", "d"]


def test_duplicate_warnings_are_not_repeated():
    rows = M._viz_fidelity(_result(
        [_warn("S1", "a"), _warn("S1", "a"), _warn("S1", "b"), _warn("S1", "b")]))
    assert _row(rows, "S1")["additional_reasons"] == ["b"]


def test_warnings_are_not_leaked_across_worksheets():
    rows = M._viz_fidelity(_result(
        [_warn("S1", "a"), _warn("S2", "x"), _warn("S1", "b"), _warn("S2", "y")],
        worksheets=[{"name": "S1", "visual_type": "matrix"},
                    {"name": "S2", "visual_type": "table"}]))
    assert _row(rows, "S1")["reason"] == "a"
    assert _row(rows, "S1")["additional_reasons"] == ["b"]
    assert _row(rows, "S2")["reason"] == "x"
    assert _row(rows, "S2")["additional_reasons"] == ["y"]


def test_a_clean_worksheet_stays_rebuilt():
    rows = M._viz_fidelity(_result([], worksheets=[{"name": "S1", "visual_type": "matrix"}]))
    r = _row(rows, "S1")
    assert r["status"] == "rebuilt"
    assert "additional_reasons" not in r


def test_non_worksheet_scope_warnings_still_become_their_own_rows():
    """Dashboard-scope and unmatched warnings were never collapsed and must stay that way."""
    rows = M._viz_fidelity(_result(
        [_warn("D1", "dash issue", scope="dashboard"), _warn("Ghost", "unmatched")]))
    reasons = [r["reason"] for r in rows]
    assert "dash issue" in reasons
    assert "unmatched" in reasons
