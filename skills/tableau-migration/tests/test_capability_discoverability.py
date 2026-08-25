"""A capability nobody can find does not exist.

The skill ships 50+ scripts. An agent only ever learns a script exists by reading `SKILL.md` or a
`resources/*.md` runbook -- it does not list `scripts/`, and it should not have to. So a RUNNABLE
script that no prose mentions is invisible: the capability is present, paid for, tested, and never
used.

This is not hypothetical. `scripts/pbip_desktop_reload.py` (2.265.0) turns a ~115 s
edit-restart-verify cycle into ~1 s, and the very next question asked about it was "how do I make
another session aware of that?" -- which is exactly the failure this gate exists to prevent. It was
in SKILL.md's resource table, and still absent from `fidelity-oracle.md`, the one page an agent
actually reads when it sits down to do render verification.

WHAT COUNTS AS RUNNABLE, and why the distinction carries the whole test: a module that other scripts
import (``colour_rules``, ``zone_tree``, ``layout_solve``) needs no runbook -- its callers are its
documentation. A script with a ``__main__`` and an argument parser is a thing a human or an agent is
meant to INVOKE, and invoking it requires knowing it is there. Judging all 52 scripts identically
would demand prose for 14 internal modules and make the gate noise; judging none of them lets the
next 1-second-loop go missing.

The allowlist below is a debt ledger, not an exemption. Four scripts predate this gate; naming them
explicitly is what lets the rule land green and fire on anything NEW, which is the same reasoning
the repo already applies to escalating a check only once the corpus is clean. Shrink it; never
extend it.
"""
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "scripts")
_RESOURCES = os.path.join(_ROOT, "resources")

# Runnable scripts that were already undiscoverable when this gate was written (2026-08-25).
# Each is a real capability an agent currently cannot find. Documenting one = deleting a line here.
_PRE_EXISTING_UNDOCUMENTED = frozenset({
    "geometry_audit.py",        # scores emitted geometry: overlaps / containment / displacement
    "polish_layout.py",         # post-emit layout tidy pass
    "tmdl_lint.py",             # TMDL well-formedness (runs inside openability_gate, also a CLI)
    "workbook_calc_usage.py",   # where each calc is actually placed in the workbook
})


def _prose():
    """Every word an agent could plausibly read: SKILL.md plus every runbook."""
    out = []
    skill = os.path.join(_ROOT, "SKILL.md")
    if os.path.isfile(skill):
        out.append(open(skill, encoding="utf-8-sig", errors="replace").read())
    if os.path.isdir(_RESOURCES):
        for name in sorted(os.listdir(_RESOURCES)):
            if name.endswith(".md"):
                out.append(open(os.path.join(_RESOURCES, name),
                                encoding="utf-8-sig", errors="replace").read())
    return "\n".join(out)


def _is_runnable(path):
    """Has a ``__main__`` entry point AND an argument surface -- i.e. meant to be invoked."""
    try:
        src = open(path, encoding="utf-8-sig", errors="replace").read()
    except OSError:
        return False
    return "__main__" in src and ("argparse" in src or "sys.argv" in src)


def _runnable_scripts():
    if not os.path.isdir(_SCRIPTS):
        return []
    return sorted(f for f in os.listdir(_SCRIPTS)
                  if f.endswith(".py") and _is_runnable(os.path.join(_SCRIPTS, f)))


def test_every_runnable_script_is_reachable_from_the_prose():
    """A new runnable script must be named in SKILL.md or a runbook, or nobody will ever run it."""
    scripts = _runnable_scripts()
    if not scripts:
        pytest.skip("no runnable scripts in this checkout")
    prose = _prose()
    if not prose.strip():
        pytest.skip("no SKILL.md / resources in this checkout (installed-skill context)")

    missing = [f for f in scripts
               if f not in _PRE_EXISTING_UNDOCUMENTED
               and f not in prose and f[:-3] not in prose]

    assert not missing, (
        "runnable script(s) that no prose mentions -- an agent reads SKILL.md and resources/, never "
        "a directory listing, so these capabilities are invisible and will never be used:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd a row to SKILL.md's resource table AND a pointer from the runbook an agent is "
          "actually reading when it would need the script. Being listed in a table is not enough: "
          "pbip_desktop_reload.py was in the table and still missing from fidelity-oracle.md, which "
          "is the page you land on when you sit down to do render verification.")


def test_the_undocumented_allowlist_does_not_grow_silently():
    """The ledger is a debt list. A stale entry hides a script that no longer exists, and an entry
    for a script that IS now documented quietly buys back an exemption nobody needs."""
    scripts = set(_runnable_scripts())
    if not scripts:
        pytest.skip("no runnable scripts in this checkout")
    stale = sorted(_PRE_EXISTING_UNDOCUMENTED - scripts)
    assert not stale, ("allowlist names script(s) that no longer exist: %s -- delete them" % stale)

    prose = _prose()
    if not prose.strip():
        pytest.skip("no prose in this checkout")
    now_documented = sorted(f for f in _PRE_EXISTING_UNDOCUMENTED
                            if f in prose or f[:-3] in prose)
    assert not now_documented, (
        "these are documented now, so remove them from _PRE_EXISTING_UNDOCUMENTED and let the gate "
        "protect them: %s" % now_documented)


def test_the_render_loop_is_reachable_from_the_render_runbook():
    """The specific regression that motivated this file.

    `fidelity-oracle.md` owns the render-verify discipline. An agent about to reload Desktop reads
    THAT page, not the resource table -- so the fast reload has to be named there or the ~115 s
    cycle silently stays the default.
    """
    path = os.path.join(_RESOURCES, "fidelity-oracle.md")
    if not os.path.isfile(path):
        pytest.skip("fidelity-oracle.md not present")
    text = open(path, encoding="utf-8-sig", errors="replace").read()
    assert "pbip_desktop_reload" in text, (
        "fidelity-oracle.md describes the reload -> refresh -> screenshot cycle but never names "
        "scripts/pbip_desktop_reload.py, so an agent following it will keep paying a full Desktop "
        "restart per iteration")


def test_the_reload_runbook_states_what_reload_does_NOT_do():
    """Half of this capability is its limits. A reader who thinks reload refreshes data, persists
    the cache, or replaces a cold open will ship an unopenable or empty model and believe it was
    verified -- which is precisely how the pageOrder crash and the ambiguous-path refusal reached a
    user."""
    path = os.path.join(_RESOURCES, "desktop-bridge-reload.md")
    if not os.path.isfile(path):
        pytest.skip("desktop-bridge-reload.md not present")
    text = open(path, encoding="utf-8-sig", errors="replace").read().lower()
    for claim in ("does not refresh data", "does not persist", "cold open"):
        assert claim in text, "the runbook must state its limit: %r" % claim
