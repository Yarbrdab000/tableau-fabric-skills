"""Reference capture must survive a mid-loop Tableau Cloud session death (issue #97).

``acquire_reference_images`` signed in ONCE and reused that token for every worksheet. On Tableau
Cloud a single REST session starts returning ``401002`` on the view-export endpoints after an
unpredictable number of exports, and it does NOT recover on that token. Because the loop catches
per-sheet exceptions, the run then **completed "successfully" having captured only the first few
worksheets** -- and the returned manifest looked structurally complete, so the image tier silently
had no reference for most sheets.

Measured by the reporter on Tableau Cloud (10ax, REST 3.29, 8 views, strictly sequential):
one sign-in captured **1/8**; a fresh sign-in per export captured **8/8**. Three root-cause
hypotheses were tested and disproved, so only the behaviour and the remedy are relied on here.

Also pinned: the two failure modes must stay distinguishable in the manifest, and a run that never
loses its session must return exactly the previous shape.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import fidelity_reference as F  # noqa: E402


_401002 = ("<tsResponse><error code='401002'><summary>Unauthorized Access</summary>"
           "<detail>Invalid authentication credentials were provided.</detail></error></tsResponse>")


class _FakeTds:
    """Stand-in for the ``fetch_tds`` module: counts sign-ins, records sign-outs."""

    def __init__(self):
        self.sign_ins = 0
        self.sign_outs = 0

    def sign_in(self, server, version, site, pat_name=None, pat_secret=None, jwt=None):
        self.sign_ins += 1
        return (f"token{self.sign_ins}", "site-1")

    def sign_out(self, server, version, token):
        self.sign_outs += 1


def _install(monkeypatch, tmp_path, *, image_side_effects):
    """Wire the module's collaborators. ``image_side_effects`` is consumed one call at a time; an
    ``Exception`` instance is raised, anything else is returned as the PNG bytes."""
    tds = _FakeTds()
    monkeypatch.setattr(F, "_tds", tds)
    monkeypatch.setattr(F, "list_views", lambda *a, **k: [
        {"id": f"id-{n}", "name": n} for n in ("A", "B", "C", "D")])
    monkeypatch.setattr(F, "match_views",
                        lambda views, names: {v["name"]: v for v in views if v["name"] in names})

    calls = {"n": 0}
    seq = list(image_side_effects)

    def _fetch(server, site_id, token, view_id, rest_version=None, resolution=None):
        i = calls["n"]
        calls["n"] += 1
        out = seq[i] if i < len(seq) else b"PNG"
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(F, "fetch_view_image", _fetch)
    return tds, calls


# -- the reported truncation ----------------------------------------------------------------


def test_a_mid_loop_session_death_no_longer_truncates_the_capture(monkeypatch, tmp_path):
    """THE BUG: sheets B..D all failed once the session died, and the run reported success."""
    tds, _calls = _install(monkeypatch, tmp_path, image_side_effects=[
        b"PNG",                  # A succeeds
        RuntimeError(_401002),   # B: session dies -> re-auth -> retry succeeds
        b"PNG",
        b"PNG",                  # C
        b"PNG",                  # D
    ])
    out = F.acquire_reference_images(
        server="https://x", site_content_url="s", workbook_id="w",
        worksheet_names=["A", "B", "C", "D"], output_dir=str(tmp_path),
        pat_name="p", pat_secret="s")

    assert out["available"] is True
    assert sorted(out["saved"]) == ["A", "B", "C", "D"], out["results"]
    assert out["session_recoveries"] == 1
    assert tds.sign_ins == 2, "exactly one re-authentication"
    assert out["results"]["B"]["session_recovered"] is True


def test_every_remaining_sheet_survives_after_one_death(monkeypatch, tmp_path):
    """The class the per-sheet catch could not see: once the session dies, EVERY remaining sheet
    would have failed. All of them must now be captured."""
    tds, _ = _install(monkeypatch, tmp_path, image_side_effects=[
        b"PNG",
        RuntimeError(_401002), b"PNG",
        RuntimeError(_401002), b"PNG",
        RuntimeError(_401002), b"PNG",
    ])
    out = F.acquire_reference_images(
        server="https://x", site_content_url="s", workbook_id="w",
        worksheet_names=["A", "B", "C", "D"], output_dir=str(tmp_path),
        pat_name="p", pat_secret="s")
    assert sorted(out["saved"]) == ["A", "B", "C", "D"]
    assert out["session_recoveries"] == 3


# -- fail-closed: a bad sheet is still a bad sheet -------------------------------------------


def test_a_non_session_error_is_still_a_per_sheet_error_and_never_re_authenticates(
        monkeypatch, tmp_path):
    tds, _ = _install(monkeypatch, tmp_path, image_side_effects=[
        b"PNG", RuntimeError("500 Internal Server Error"), b"PNG", b"PNG"])
    out = F.acquire_reference_images(
        server="https://x", site_content_url="s", workbook_id="w",
        worksheet_names=["A", "B", "C", "D"], output_dir=str(tmp_path),
        pat_name="p", pat_secret="s")

    assert out["results"]["B"]["status"] == "error"
    assert sorted(out["saved"]) == ["A", "C", "D"]
    assert tds.sign_ins == 1, "a bad sheet must not burn a re-authentication"
    assert "session_recoveries" not in out


def test_a_retry_that_fails_again_is_recorded_as_session_lost(monkeypatch, tmp_path):
    """Distinguishing the two failure modes is the other half of the ask: a truncated capture must
    be VISIBLE rather than looking like an unrelated per-sheet problem."""
    tds, _ = _install(monkeypatch, tmp_path, image_side_effects=[
        b"PNG", RuntimeError(_401002), RuntimeError(_401002), b"PNG", b"PNG"])
    out = F.acquire_reference_images(
        server="https://x", site_content_url="s", workbook_id="w",
        worksheet_names=["A", "B", "C", "D"], output_dir=str(tmp_path),
        pat_name="p", pat_secret="s")

    assert out["results"]["B"]["status"] == "session_lost"
    assert out["session_lost"] == ["B"]
    assert "B" not in out["saved"]


def test_a_clean_run_returns_the_previous_shape(monkeypatch, tmp_path):
    """Additive-only: an on-prem run that never loses its session must be byte-identical."""
    tds, _ = _install(monkeypatch, tmp_path, image_side_effects=[b"PNG"] * 4)
    out = F.acquire_reference_images(
        server="https://x", site_content_url="s", workbook_id="w",
        worksheet_names=["A", "B", "C", "D"], output_dir=str(tmp_path),
        pat_name="p", pat_secret="s")

    assert set(out) == {"available", "site_id", "results", "saved", "not_found"}
    assert tds.sign_ins == 1
    assert all("session_recovered" not in r for r in out["results"].values())


def test_sign_out_still_runs_after_a_recovery(monkeypatch, tmp_path):
    """The re-auth rebinds ``token``; sign-out must release the CURRENT session, not a dead one."""
    tds, _ = _install(monkeypatch, tmp_path, image_side_effects=[
        RuntimeError(_401002), b"PNG", b"PNG", b"PNG", b"PNG"])
    F.acquire_reference_images(
        server="https://x", site_content_url="s", workbook_id="w",
        worksheet_names=["A", "B", "C", "D"], output_dir=str(tmp_path),
        pat_name="p", pat_secret="s")
    assert tds.sign_outs == 1


# -- the recogniser -------------------------------------------------------------------------


def test_session_loss_is_recognised_by_code_not_prose():
    assert F._is_session_loss(RuntimeError(_401002))
    assert F._is_session_loss(RuntimeError("401002"))
    # a different 401 is NOT the session-expired code
    assert not F._is_session_loss(RuntimeError("<error code='401001'>Login error</error>"))
    assert not F._is_session_loss(RuntimeError("Unauthorized Access"))
    assert not F._is_session_loss(RuntimeError("403007 forbidden"))


def test_the_recogniser_never_raises():
    class _Bad(Exception):
        def __str__(self):
            raise ValueError("unprintable")

    assert F._is_session_loss(_Bad()) is False
