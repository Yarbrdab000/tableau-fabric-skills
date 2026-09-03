"""``page_emitted: false`` says structurally what only reason-text could say before (#188).

``viz_fidelity[].tier`` is derived from ``visual_type`` alone. That is a statement about the VISUAL,
and it coincides with page emission at only ONE of the three drop sites in ``twb_to_pbir``:

    site                                    row that reaches viz_fidelity      tier        says no page?
    worksheet classified VT_UNSUPPORTED     visual_type "unsupported"          empty       yes
    dashboard with no supported visuals     visual_type = the warning SCOPE    degraded    NO
    query state incomplete (2 emit loops)   visual_type is REAL ("line")       degraded    NO

At the last two, ``degraded`` asserts *"a rendered visual"* -- the opposite of what happened.
``status`` is ``warned`` for both outcomes so it cannot discriminate either, and reason text can but
is not a contract.

The flag is stamped by ``_warn_no_page`` AT the drop site, immediately before the ``continue``, so it
cannot drift from the branch it describes the way a downstream reason-matcher can.

SCOPE, measured rather than assumed: our 34-workbook corpus exercises exactly ONE of the three sites
(a `column` visual with no usable field bindings on `0075_customers_above_average`). The dashboard
site and the second worksheet loop are covered by fixtures here only. A fourth drop path -- a
structurally EMPTY worksheet -- needs no flag: it is classified `unsupported` in the parser, so tier
and page-emission already agree (which is what the issue's option 1 asks for, already true there).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import migrate_estate as M  # noqa: E402
import twb_to_pbir as T  # noqa: E402


# --------------------------------------------------------------------- the marker

def test_warn_no_page_is_an_ordinary_warning_plus_one_flag():
    """Additive by construction: a consumer reading `scope`/`name`/`reason` sees no change."""
    plain = T._warn("worksheet", "WS", "something")
    flagged = T._warn_no_page("worksheet", "WS", "something")
    assert flagged["scope"] == plain["scope"]
    assert flagged["name"] == plain["name"]
    assert flagged["reason"] == plain["reason"]
    assert flagged["page_emitted"] is False
    assert "page_emitted" not in plain


def test_every_drop_site_uses_the_marker():
    """Pins the call sites, because the flag is only worth anything if it is AT the `continue`.

    Asserted against the source rather than behaviour: two of the three sites are unreachable from
    our corpus, so a behavioural test would pass while one of them silently reverted.
    """
    import inspect
    src = inspect.getsource(T)
    # the three drop sites, each identified by the warning text it emits
    for needle in ("no supported visuals on this dashboard",
                   "visual has no usable field bindings (skipped)"):
        assert needle in src, needle
    # every one of those warnings must be raised through the no-page helper
    assert src.count("_warn_no_page(") >= 4, "expected 1 def + 3 call sites"
    for site in ("""            warnings.append(_warn_no_page("dashboard", db["name"],
                                          "no supported visuals on this dashboard"))
            continue""",
                 """            warnings.append(_warn_no_page(
                "worksheet", ws["name"],
                f"{ws['visual_type']} visual has no usable field bindings (skipped)"))
            continue""",
                 """                warnings.append(_warn_no_page(
                    "worksheet", ws["name"],
                    f"{ws['visual_type']} visual has no usable field bindings (skipped)"))
                continue"""):
        assert site in src, site[:60]


# --------------------------------------------------------------------- the projection

def _fid(warnings, worksheets):
    return M._viz_fidelity({"ir": {"worksheets": worksheets}, "warnings": warnings})


def test_a_dashboard_drop_row_carries_the_flag():
    """The row the issue names first: scope `dashboard`, so `visual_type` is the SCOPE and the tier
    reads `degraded` -- asserting a rendered visual that does not exist."""
    w = T._warn_no_page("dashboard", "Story 1", "no supported visuals on this dashboard")
    rows = _fid([w], [])
    assert len(rows) == 1
    assert rows[0]["page_emitted"] is False
    # the pre-existing fields are untouched, including the tier that motivated the issue
    assert rows[0]["status"] == "warned"
    assert rows[0]["tier"] == "degraded"


def test_a_worksheet_drop_row_carries_the_flag_beside_a_REAL_visual_type():
    """The second mis-describing site: the visual type is real (`line`), so nothing in the row
    hinted that no page was emitted."""
    ws = [{"name": "Pareto (method2)", "visual_type": "line"}]
    w = T._warn_no_page("worksheet", "Pareto (method2)",
                        "line visual has no usable field bindings (skipped)")
    rows = _fid([w], ws)
    row = next(r for r in rows if r["worksheet"] == "Pareto (method2)")
    assert row["page_emitted"] is False
    assert row["visual_type"] == "line"
    assert row["tier"] == "degraded"


def test_an_ordinary_warning_row_does_NOT_carry_the_flag():
    """The flag must mean "no page", not "warned". Without this, a consumer gating on it would
    reject every degraded-but-emitted visual."""
    ws = [{"name": "Sales", "visual_type": "bar"}]
    w = T._warn("worksheet", "Sales", "a colour palette was defaulted")
    row = next(r for r in _fid([w], ws) if r["worksheet"] == "Sales")
    assert "page_emitted" not in row
    assert row["status"] == "warned"


def test_a_clean_row_does_NOT_carry_the_flag():
    row = next(r for r in _fid([], [{"name": "Sales", "visual_type": "bar"}])
               if r["worksheet"] == "Sales")
    assert "page_emitted" not in row
    assert row["status"] == "rebuilt"


def test_the_flag_survives_alongside_additional_reasons():
    """A worksheet can fail several ways; the no-page fact must not be lost to the row that merges
    them."""
    ws = [{"name": "W", "visual_type": "line"}]
    warnings = [T._warn_no_page("worksheet", "W", "line visual has no usable field bindings (skipped)"),
                T._warn("worksheet", "W", "and a second, unrelated complaint")]
    row = next(r for r in _fid(warnings, ws) if r["worksheet"] == "W")
    assert row["page_emitted"] is False
    assert row["additional_reasons"] == ["manual attention required: and a second, unrelated complaint"]


def test_the_flag_is_absent_rather_than_true_when_a_page_WAS_emitted():
    """Deliberately one-sided. The engine knows for certain when it dropped a page; it does not
    separately prove emission at every other site, so claiming `page_emitted: true` would assert
    something unverified. Absent means "not declared dropped"."""
    rows = _fid([T._warn("dashboard", "D", "some dashboard note")], [])
    assert all(r.get("page_emitted") is not True for r in rows)
