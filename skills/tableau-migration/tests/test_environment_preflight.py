"""A machine-level blocker must be reported BEFORE the user blames the migration.

A customer trial lost time to Power BI Desktop refusing to open the emitted ``.pbip`` with
``Method not found: 'Void Newtonsoft.Json.JsonSerializerSettings..ctor'``. It refused a brand-new
BLANK ``.pbip`` too, so the artifact was never implicated -- an outdated ``Newtonsoft.Json`` in the
machine's GAC was, because .NET binds a GAC assembly ahead of the copy an application ships. Two of
three users in that trial hit it, and each began by suspecting the migration, because that is what
they had just run.

WHAT IS AND IS NOT GATED HERE, because the distinction is the design:

* DETECTION is gated. The GAC layout is a directory naming convention, so it can be injected and
  asserted on any machine -- including this one, which has no ``Newtonsoft.Json`` registered at all.
* THE REMEDY IS NOT, and is deliberately not implemented. It is machine-wide (the GAC changes binding
  for every .NET application on the box), needs administrator elevation, and needs ``gacutil.exe``
  from the Windows SDK -- absent from a normal analyst machine and absent from the machine this was
  written on. An automatic fix would therefore be untested surgery on someone else's environment.

So these tests pin that the tool DIAGNOSES precisely and NEVER MODIFIES. ``test_module_never_writes``
is the load-bearing one: it is what stops a future well-meaning change from turning a read-only
diagnosis into a machine mutation.
"""
import os

import pytest

import environment_preflight as EP


def _fake_gac(tmp_path, *versions):
    """A GAC root laid out the way .NET does: ``<assembly>/v4.0_<version>__<token>``."""
    root = tmp_path / "GAC_MSIL"
    asm = root / "Newtonsoft.Json"
    asm.mkdir(parents=True)
    for v in versions:
        (asm / ("v4.0_%s__30ad4fe6b2a6aeed" % v)).mkdir()
    return str(root)


def test_an_outdated_assembly_is_reported(tmp_path):
    """The exact shape that broke the customer trial."""
    finding = EP.newtonsoft_gac_finding(gac_root=_fake_gac(tmp_path, "6.0.0.0"), is_windows=True)
    assert finding is not None
    assert finding["check"] == "newtonsoft_gac_outdated"
    assert finding["newest"] == "6.0.0.0"
    # the detail must name the ACTUAL error text, so a user searching for it lands here
    assert "JsonSerializerSettings" in finding["detail"]
    # and must say the artifact is not implicated, which is the whole point
    assert "blank" in finding["detail"].lower()


def test_a_current_assembly_is_not_reported(tmp_path):
    assert EP.newtonsoft_gac_finding(gac_root=_fake_gac(tmp_path, "13.0.3"), is_windows=True) is None


def test_the_newest_version_decides(tmp_path):
    """Side-by-side registration is normal; an old copy beside a current one is not a blocker."""
    root = _fake_gac(tmp_path, "6.0.0.0", "13.0.3")
    assert EP.newtonsoft_gac_finding(gac_root=root, is_windows=True) is None


def test_absent_assembly_is_not_reported(tmp_path):
    """No GAC entry means nothing overrides Desktop's own copy -- the healthy case."""
    (tmp_path / "GAC_MSIL").mkdir()
    assert EP.newtonsoft_gac_finding(gac_root=str(tmp_path / "GAC_MSIL"), is_windows=True) is None


def test_a_missing_gac_root_is_not_an_error(tmp_path):
    assert EP.newtonsoft_gac_finding(gac_root=str(tmp_path / "nope"), is_windows=True) is None


def test_non_windows_is_skipped(tmp_path):
    """The GAC is a Windows concept; a Linux runner must not manufacture a finding."""
    assert EP.newtonsoft_gac_finding(gac_root=_fake_gac(tmp_path, "6.0.0.0"),
                                     is_windows=False) is None


def test_malformed_folder_names_are_ignored(tmp_path):
    """Read-only parsing of someone else's filesystem must never raise."""
    root = tmp_path / "GAC_MSIL" / "Newtonsoft.Json"
    root.mkdir(parents=True)
    for junk in ("not-a-version", "v4.0___", "v4.0_x.y.z__abc", ""):
        if junk:
            (root / junk).mkdir()
    assert EP.gac_assembly_versions(gac_root=str(tmp_path / "GAC_MSIL")) == []
    assert EP.newtonsoft_gac_finding(gac_root=str(tmp_path / "GAC_MSIL"), is_windows=True) is None


def test_environment_findings_is_a_list_on_a_healthy_machine(tmp_path):
    """Callers read it unconditionally, so it must never be None."""
    out = EP.environment_findings(gac_root=str(tmp_path / "nope"), is_windows=True)
    assert out == []


def test_module_never_writes():
    """THE LOAD-BEARING TEST: this module diagnoses, it does not repair.

    Registering or removing a GAC assembly changes .NET binding for every application on the
    machine, needs elevation, and needs a Windows SDK tool that is not present on a normal analyst
    box -- so it is not ours to do silently, and it could not be tested here even if it were. This
    asserts the source contains no mutation or elevation primitive, which is what stops a future
    well-meaning change from crossing that line quietly.
    """
    src = open(EP.__file__, encoding="utf-8-sig").read()
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    # Genuine mutation / elevation / execution primitives only. Deliberately NOT string matches like
    # "gacutil" or "-i", which appear legitimately in the remedy TEXT this module hands the user --
    # the first version of this test flagged its own documentation, which is a test asserting a
    # convention rather than a behaviour.
    for forbidden in ("subprocess", "os.system", "os.popen", "shutil.", "os.remove", "os.unlink",
                      "os.rmdir", "os.makedirs", "os.mkdir", "ctypes", "win32api", "ShellExecute"):
        assert forbidden not in body, (
            "environment_preflight must stay READ-ONLY; found %r. Detecting is ours; modifying a "
            "machine-wide assembly cache is not." % forbidden)
    # Positive form: the only filesystem call it makes is a directory listing.
    assert "os.listdir" in body
    assert "open(" not in body, "this module reads directory NAMES only; it opens no file"
