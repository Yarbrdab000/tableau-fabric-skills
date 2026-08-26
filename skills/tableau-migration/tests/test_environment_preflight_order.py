"""The environment preflight must run BEFORE the work, and the openability failure must be
documented where a stuck user looks.

WHY THIS EXISTS. A user completed a full migration, was told it succeeded, then double-clicked the
`.pbip` into ``Method not found: 'Void Newtonsoft.Json.JsonSerializerSettings..ctor'``. The engine
*had* a detector for exactly that machine condition (``environment_preflight``, shipped 2.207.0) --
it just ran at **report-assembly time, 80% through the run**, so it could not save any of the work
it was meant to protect. A "preflight" that runs last is a post-mortem.

Two failure modes are pinned here, and they are different in kind:

1. **ORDER** -- the check must execute before the first migration call, and its findings must reach
   stderr immediately. Verified by driving ``migrate_estate`` with a faked finding and asserting the
   *sequence*, not merely that the key exists in the report. A test that only asserted
   ``report["environment"]`` would pass with the check in its old, useless position.

2. **REACHABILITY** -- ``troubleshooting.md`` is where a user with a broken ``.pbip`` goes. Before
   this release it had no row for *"the run says the model is not openable"*: the gate's own
   ``detail`` text is excellent (mechanism, consequence, and the exact Desktop error string) but it
   lands in ``report.json``. Correct, durable, and filed where the reader is not.

Note the ``sys`` trap this nearly shipped with: ``migrate_estate`` has **no module-level
``import sys``**, so a bare ``sys.stderr.write`` in the preflight raised ``NameError`` -- and only
when a finding EXISTED, i.e. only on the machines the code exists to help, never in a clean-machine
test. ``test_preflight_survives_a_finding`` is the control for that.
"""
import os
import re
import subprocess
import sys
import tempfile
import textwrap

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_SKILL_DIR, "scripts")
_TROUBLESHOOTING = os.path.join(_SKILL_DIR, "resources", "troubleshooting.md")


def _read(path):
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def test_preflight_is_called_before_the_first_migration_call():
    src = _read(os.path.join(_SCRIPTS, "migrate_estate.py"))
    lines = src.splitlines()

    def line_of(pattern):
        for i, l in enumerate(lines, 1):
            if re.search(pattern, l):
                return i
        return None

    call = line_of(r"_env_findings = _envpre\.environment_findings\(\)")
    work = line_of(r"^    ds_details = \[_migrate_one_datasource")
    assert call, "the preflight call site is gone"
    assert work, "the first migration call is gone -- re-anchor this test"
    assert call < work, (
        "environment_findings() is called at L%d but migration work starts at L%d. A preflight "
        "that runs after the work cannot save it -- that is the defect this release fixed." % (call, work)
    )


def test_preflight_has_exactly_one_call_site():
    src = _read(os.path.join(_SCRIPTS, "migrate_estate.py"))
    n = len(re.findall(r"environment_findings\(\)", src))
    assert n == 1, (
        "expected exactly 1 environment_findings() call site, found %d -- a second one means the "
        "old report-time check was left behind and the machine is probed twice" % n
    )


def test_preflight_survives_a_finding():
    """Control for the `sys` trap: drive a REAL run with a faked finding present.

    A happy-path test cannot see this. The NameError only fired when the findings list was
    non-empty, so the bug was invisible on every clean machine -- including CI.
    """
    driver = textwrap.dedent(
        """
        import sys, tempfile
        sys.path.insert(0, %r)
        import environment_preflight as ep
        ORDER = []
        ep.environment_findings = lambda: (
            ORDER.append("preflight"),
            [{"code": "FAKE", "detail": "FAKE machine blocker"}])[1]
        import migrate_estate as M
        class Src:
            def describe(self): return {"kind": "control"}
            def list_datasources(self):
                ORDER.append("work"); return []
            def list_workbooks(self):
                ORDER.append("work"); return []
        rep = M.migrate_estate(Src(), tempfile.mkdtemp(prefix="envctl_"), pbip=False)
        print("ORDER=%%s" %% ORDER)
        print("FINDINGS=%%d" %% len((rep.get("environment") or {}).get("findings") or []))
        """
        % _SCRIPTS
    )
    path = os.path.join(tempfile.mkdtemp(prefix="envdrv_"), "driver.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(driver)
    proc = subprocess.run([sys.executable, path], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, (
        "migrate_estate raised with a finding present -- this is the NameError trap:\n%s"
        % (proc.stderr or "")[-800:]
    )
    assert "[environment]" in (proc.stderr or ""), "the finding never reached stderr"
    assert "ORDER=['preflight', 'work'" in (proc.stdout or ""), (
        "the preflight did not run first; got %r" % (proc.stdout or "").strip()
    )
    assert "FINDINGS=1" in (proc.stdout or ""), "the finding was not recorded in the report"


def _rows(text):
    """The section-7 table rows, one string each."""
    start = text.find("## 7) The output looks wrong")
    assert start != -1, "section 7 is gone -- re-anchor this test"
    end = text.find("## 8)", start)
    body = text[start: end if end != -1 else len(text)]
    return [l for l in body.splitlines() if l.startswith("|")]


def test_troubleshooting_has_a_row_for_the_run_saying_not_openable():
    """Anchor on the ROW, not on the file.

    An earlier version asserted ``"openability_selfcheck" in text``. A positive control showed that
    passes even when the not-openable row is gutted, because the token also appears in a
    neighbouring row -- presence *somewhere* is not presence *where the reader is looking*. Same
    class as counting matches instead of reading captures.
    """
    rows = _rows(_read(_TROUBLESHOOTING))
    hits = [r for r in rows if "not openable" in r.lower() or "openability_selfcheck" in r]
    assert hits, (
        "section 7 has no row for the run reporting a non-openable model. The gate's own detail "
        "text is precise, but it lands in report.json -- and a stuck user opens this file."
    )
    joined = "\n".join(hits)
    assert "openability_selfcheck" in joined, (
        "the not-openable row does not name the report key that carries the diagnosis"
    )
    assert "issues" in joined, (
        "the row does not point at openability_selfcheck.issues[], which is where the cause is"
    )


def test_the_stub_driven_case_routes_to_the_second_compiler():
    rows = _rows(_read(_TROUBLESHOOTING))
    hits = [r for r in rows if "needs_review_total" in r or "Stubbed calcs left the model" in r]
    assert hits, (
        "section 7 has no row for stubbed calcs leaving the model non-openable -- the exact "
        "sequence a user hit: 16 stubs, model FAILED, and no documented route to the remedy"
    )
    joined = "\n".join(hits)
    assert "second-compiler.md" in joined, (
        "the stub row does not link second-compiler.md, which is the documented remedy"
    )
    assert "approved_dax" in joined, (
        "the stub row does not name approved_dax.json, so the reader is told a pass exists but "
        "not how to run it"
    )


def test_troubleshooting_index_mentions_the_run_saying_not_openable():
    text = _read(_TROUBLESHOOTING)
    head = text[: text.find("## 1)")]
    assert "not openable" in head.lower(), (
        "the section-7 index line does not mention the run reporting a non-openable model, so a "
        "user scanning the top of the file has no reason to open section 7"
    )
