"""Regression guard for the assisted-tier RUNBOOKS (calc Tier-1 + dashboard Tier-3).

A real 1.61 migration run failed because the agent did not understand that IT *is* the second
compiler: it hunted for a nonexistent authoring script, fought the ``--approved-dax`` JSON format,
and — worst — the runtime guidance and the runbook told it to "re-run Tier 0" / "re-translate" for the
resolvable categories, so it tried to escape into a deterministic re-run instead of authoring the DAX.
Tier 0 had already run and *stubbed* those calcs; there is no deterministic re-run to fall back on.

These tests pin the fix so the rot cannot creep back:

  * the runtime ``translation_router`` guidance shipped to the agent must NOT tell it to re-run Tier 0
    or re-translate (both categories now frame the action as "YOU author the corrected DAX");
  * ``second-compiler.md`` must carry the unmissable "you ARE the second compiler / there is no script /
    you author the DAX" identity anchors and the exact ``--approved-dax`` landing shape; and
  * ``dashboard-audit.md`` + the SKILL Post-Migration steps must give Tier 3 the SAME gated
    offer -> GO -> adjudicate -> monotonic-gate contract, honestly scoped to today's machinery.

Doc-content assertions only (no engine imports beyond the router), so they stay fast and additive.
"""
import os
import re

import translation_router as R  # noqa: E402  (conftest puts scripts/ on sys.path)

_HERE = os.path.dirname(os.path.abspath(__file__))
_RES = os.path.normpath(os.path.join(_HERE, "..", "resources"))
_SKILL = os.path.normpath(os.path.join(_HERE, "..", "SKILL.md"))

# The phrases that caused the regression: they told the agent to escape into a deterministic re-run
# for a category the agent is actually meant to AUTHOR. Banned from runtime guidance + the runbook.
_BANNED = ("re-run tier 0", "try tier 0 again", "re-translate")


def _read(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()


# --------------------------------------------------------------- (a) runtime router guidance is clean
def test_router_guidance_never_tells_agent_to_rerun_tier0():
    for cat, text in R._GUIDANCE.items():
        low = text.lower()
        for phrase in _BANNED:
            assert phrase not in low, f"router guidance[{cat}] must not say {phrase!r}: {text!r}"


def test_resolvable_categories_frame_authoring_not_a_rerun():
    # The two "cheapest win" categories are exactly where the agent used to bail to a re-run; they must
    # now tell it that IT authors the corrected DAX.
    for cat in (R.UNRESOLVED_REFERENCE, R.TYPE_OR_SHAPE_MISMATCH):
        low = R._GUIDANCE[cat].lower()
        assert "author" in low or "you write" in low or "write the" in low, R._GUIDANCE[cat]


def test_every_category_still_has_nonempty_guidance():
    # Do not let the purge accidentally empty a category (would break the router contract).
    for cat in R.CATEGORIES:
        assert cat in R._GUIDANCE and R._GUIDANCE[cat].strip()


# ------------------------------------------------------------------ (b) calc runbook identity + shape
def test_second_compiler_runbook_has_identity_anchors():
    text = _read(os.path.join(_RES, "second-compiler.md"))
    low = text.lower()
    # "you ARE the compiler" identity, stated unmissably.
    assert "you are the second compiler" in low
    # "there is no script that authors" — kills the hunt for a CLI that writes the DAX.
    assert "there is no script that authors" in low


def test_second_compiler_runbook_shows_exact_approved_dax_shape():
    text = _read(os.path.join(_RES, "second-compiler.md"))
    # The estate landing seam and its file, verbatim, so the agent does not fight the format.
    assert "--approved-dax" in text
    assert "approved_dax.json" in text
    # Both accepted value shapes are shown: the flat "name": "DAX" and the {"dax":..., "table":...} dict.
    assert '"dax"' in text and '"table"' in text
    # The seam is a {calc name -> DAX} object (not a list / not a bare string).
    assert "calc name -> DAX" in text or "{calc_name: dax}" in text


def test_second_compiler_runbook_kills_the_no_data_reconcile_trap():
    text = _read(os.path.join(_RES, "second-compiler.md")).lower()
    # A local-only run of a .twbx still has embedded extract data to reconcile against.
    assert "does not mean" in text and "deployed to fabric" in text


def test_calc_runbook_has_no_banned_rerun_language():
    low = _read(os.path.join(_RES, "second-compiler.md")).lower()
    for phrase in _BANNED:
        assert phrase not in low, f"second-compiler.md must not reintroduce {phrase!r}"


# ------------------------------------------------------------ (c) Tier-3 gets the SAME gated contract
def test_dashboard_audit_runbook_exists_with_identity_anchor():
    path = os.path.join(_RES, "dashboard-audit.md")
    assert os.path.isfile(path), "the Tier-3 dashboard-audit playbook must exist"
    low = _read(path).lower()
    assert "you are the dashboard auditor" in low
    assert "there is no script that redesigns" in low


def test_dashboard_audit_runbook_is_gated_and_gate_validated():
    low = _read(os.path.join(_RES, "dashboard-audit.md")).lower()
    # Same gated offer -> GO as the calc tier.
    assert "`go`" in low or " go " in low
    # The monotonic fidelity gate is the authority (the viz twin of check_candidate_dax + oracle).
    assert "monotonic" in low and "land_dashboard_audit" in low
    # Source-field truth is inviolate (the viz twin of faithful-or-stub).
    assert "never drop, add, or re-role" in low


def test_dashboard_audit_runbook_is_honest_about_the_landing_seam():
    # v1.63.0 ships the gated runbook over existing machinery; the on-disk --approved-viz re-emit seam
    # is a later increment. The doc must say so rather than imply a landing path that does not exist.
    low = _read(os.path.join(_RES, "dashboard-audit.md")).lower()
    assert "--approved-viz" in low
    assert "later" in low or "separate" in low


def test_dashboard_audit_runbook_opens_with_runnable_quickstart():
    # A real run looped: the agent re-narrated "I will build the audit bundle" 4+ times without ever
    # executing it, because the concrete `audit_tier.py` command sat ~130 lines down under heavy
    # "there is no script, do not go looking for a command" doctrine. Pin an actionable, copy-paste
    # quickstart NEAR THE TOP so the agent hits a runnable command before the philosophy.
    text = _read(os.path.join(_RES, "dashboard-audit.md"))
    head = text[:2500]  # the first ~40 lines
    low = head.lower()
    # An unmistakable run-it-now marker and the actual build command must be up top.
    assert "run this now" in low
    assert "audit_tier.py" in head
    assert "--prompt" in head
    # And it must actively discourage re-narrating instead of executing.
    assert "do not re-narrate" in low or "do not describe it" in low


def test_dashboard_audit_runbook_does_not_fight_the_build_command():
    # The clarified doctrine must keep BOTH truths: no script *decides* for you, but you DO run
    # audit_tier.py to produce the bundle. The old flat "do not go looking for one" (with no build
    # carve-out) is what stalled the agent.
    low = _read(os.path.join(_RES, "dashboard-audit.md")).lower()
    flat = re.sub(r"\s+", " ", low)  # collapse line-wraps so the phrase matches regardless of wrapping
    assert "you absolutely do run `audit_tier.py`" in flat
    # ...while still preserving the identity anchor the earlier test pins.
    assert "there is no script that redesigns a visual for you" in flat


# --------------------------------------------------------------- (d) SKILL Post-Migration wires both
def test_skill_offers_both_gated_tiers():
    text = _read(_SKILL)
    low = text.lower()
    # Calc second compiler, gated on the stub signal.
    assert "second compiler" in low and "needs_review_total" in text
    # Tier-3 dashboard audit, gated on the warned-visual signal, pointing at the new playbook.
    assert "dashboard audit" in low
    assert "dashboard-audit.md" in text
    assert "visuals_flagged" in text or "warned" in low


# ------------------------------------------------ (e) the .pbip-is-a-JSON-pointer-not-a-ZIP contract
# A real run corrupted correct, openable output because the agent assumed a ~300-byte .pbip was a
# "broken un-zipped stub" and re-zipped it (every sibling format .pbix/.twbx/.tdsx IS a zip). These
# pin the loud contract into the docs so the reflex is pre-empted and the agent is pointed at the
# deterministic --verify-pbip check instead of guessing.
def test_gotchas_teach_pbip_is_a_pointer_not_a_zip():
    text = _read(os.path.join(_RES, "migration-gotchas.md"))
    low = text.lower()
    flat = re.sub(r"\s+", " ", low)
    # The positive truth: a tiny .pbip is CORRECT, and it is a JSON pointer, not an archive.
    assert "json" in low and "pointer" in low
    assert "not a zip" in flat or "not a zip archive" in flat
    # The exact failure signature and the deterministic escape hatch.
    assert "unable to translate bytes" in low
    assert "--verify-pbip" in text
    # And the anti-panic rule: a WARN/degraded run is a legitimate result, not a rebuild trigger.
    assert "stop-and-ask" in flat or "never something to fix by hand" in flat


def test_skill_output_section_warns_never_to_zip_a_pbip():
    text = _read(_SKILL)
    low = text.lower()
    flat = re.sub(r"\s+", " ", low)
    # The loud callout must carry the positive truth + the never-zip rule + the verifier command.
    assert "json" in flat and "pointer" in flat
    assert "never repackage it" in flat or "never zip" in flat
    assert "--verify-pbip" in text
