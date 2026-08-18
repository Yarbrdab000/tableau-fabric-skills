"""Environment preflight: conditions on the MACHINE that break the output we hand over.

WHY THIS EXISTS. A customer trial lost time to a failure that had nothing to do with the migration:
Power BI Desktop refused to open the emitted ``.pbip`` with

    Method not found: 'Void Newtonsoft.Json.JsonSerializerSettings..ctor'
    (Newtonsoft.Json.JsonSerializerSettings)'.

It refused to open a **brand-new blank** ``.pbip`` too, so the artifact was never the problem -- an
outdated ``Newtonsoft.Json`` registered in the machine's Global Assembly Cache was, because .NET
binds a GAC assembly in preference to the copy an application ships. Two of three users in that
trial hit it. Every one of them started by suspecting the migration, because that is what they had
just run.

WHAT THIS MODULE DOES, AND DELIBERATELY DOES NOT DO
---------------------------------------------------
It **detects and instructs. It never modifies the GAC.** That boundary is a deliberate engineering
judgement, not timidity:

* the GAC is **machine-wide**. Registering or removing an assembly there changes binding for *every*
  .NET application on the box, not just Power BI Desktop. A Tableau migration tool has no business
  making that decision silently;
* the remedy needs **administrator elevation**, which a migration run should never assume or request;
* it needs ``gacutil.exe``, which ships with the **Windows SDK** -- not with .NET, and not with
  Power BI Desktop. It is absent from a normal analyst machine (verified: absent from the
  development machine this was written on);
* and it is **not testable here**. No repro machine exists for this collection, so an automatic fix
  would be untested surgery on someone else's environment. Detection *is* testable, because the
  filesystem layout can be injected.

So the value delivered is the diagnosis, not the repair: the user is told **before** they open the
output that this machine will fail for a reason unrelated to their workbook, and given the exact
command. That converts an hour of suspecting the wrong component into a known, named prerequisite.

READ-ONLY: this module only lists directory names under the GAC root. It loads nothing, registers
nothing and requires no elevation.
"""
import os
import re

# Where the .NET 4.x GAC keeps side-by-side assemblies. Each child is named
# ``v4.0_<version>__<publicKeyToken>``.
_GAC_MSIL = r"C:\Windows\Microsoft.NET\assembly\GAC_MSIL"
# The legacy (.NET 2.0-3.5) GAC. Its on-disk layout is not enumerable the same way, but its presence
# is worth reporting when the modern one is empty and a failure is still being seen.
_GAC_LEGACY = r"C:\Windows\assembly"

_ASSEMBLY = "Newtonsoft.Json"

# The version measured to RESOLVE the failure in the field. Deliberately expressed as "the version
# known to work" rather than as "the minimum Power BI Desktop requires" -- we have one data point,
# not a documented floor, and claiming the latter would be inventing a threshold we never measured.
_KNOWN_GOOD = (13, 0, 0)

_VERSION_RE = re.compile(r"^v[\d.]+_(?P<version>\d+(?:\.\d+)*)__(?P<token>[0-9a-fA-F]+)$")


def _parse_version(text):
    parts = []
    for chunk in (text or "").split("."):
        if not chunk.isdigit():
            return ()
        parts.append(int(chunk))
    return tuple(parts)


def gac_assembly_versions(assembly=_ASSEMBLY, gac_root=None):
    """``[(version_tuple, version_text, folder)]`` for one assembly in the GAC, newest last.

    ``gac_root`` is injectable so the parse is testable without a machine that has the assembly
    registered -- which is the whole reason detection can be gated and the remedy cannot.
    """
    root = os.path.join(gac_root or _GAC_MSIL, assembly)
    found = []
    try:
        entries = os.listdir(root)
    except (OSError, ValueError):
        return found
    for name in entries:
        m = _VERSION_RE.match(name)
        if not m:
            continue
        parsed = _parse_version(m.group("version"))
        if parsed:
            found.append((parsed, m.group("version"), os.path.join(root, name)))
    found.sort()
    return found


def newtonsoft_gac_finding(gac_root=None, is_windows=None):
    """Return a finding dict when this machine will fail to open ANY ``.pbip``, else ``None``.

    Three outcomes, and the middle one is the point:

    * assembly absent from the GAC -> ``None``. Nothing overrides Desktop's own copy.
    * present and the NEWEST is below the known-good version -> a finding. This is the shape that
      broke the customer trial.
    * present and current -> ``None``.
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows:
        return None

    versions = gac_assembly_versions(gac_root=gac_root)
    if not versions:
        return None

    newest, newest_text, folder = versions[-1]
    if newest >= _KNOWN_GOOD:
        return None

    return {
        "check": "newtonsoft_gac_outdated",
        "assembly": _ASSEMBLY,
        "found": [text for _v, text, _f in versions],
        "newest": newest_text,
        "known_good": ".".join(str(p) for p in _KNOWN_GOOD),
        "folder": folder,
        "detail": (
            "%s %s is registered in this machine's GAC. .NET binds a GAC assembly in preference to "
            "the copy an application ships, so Power BI Desktop will fail to open ANY .pbip -- "
            "including a brand-new blank one -- with \"Method not found: 'Void "
            "Newtonsoft.Json.JsonSerializerSettings..ctor'\". This is a MACHINE condition and has "
            "nothing to do with the migrated output; confirm by creating a blank .pbip in Desktop "
            "and opening it. REMEDY, in an ELEVATED PowerShell -- no Windows SDK needed, this ships "
            "with .NET Framework: "
            "Add-Type -AssemblyName System.EnterpriseServices; "
            "(New-Object System.EnterpriseServices.Internal.Publish)"
            ".GacInstall('<path to a %s >= %s Newtonsoft.Json.dll>'). "
            "Prefer this over gacutil.exe, which is part of the Windows SDK and is absent from a "
            "normal analyst machine. Re-run this check afterwards to confirm. Reported by 2 of 3 "
            "users in one customer trial."
            % (_ASSEMBLY, newest_text, _ASSEMBLY, ".".join(str(p) for p in _KNOWN_GOOD))),
    }


def environment_findings(gac_root=None, is_windows=None):
    """Every machine-level condition that would break the handover. ``[]`` on a healthy machine."""
    out = []
    finding = newtonsoft_gac_finding(gac_root=gac_root, is_windows=is_windows)
    if finding:
        out.append(finding)
    return out


def _main(argv):
    findings = environment_findings()
    if not findings:
        print("ENVIRONMENT: OK (no known machine-level blockers detected)")
        return 0
    for f in findings:
        print("ENVIRONMENT BLOCKER: %s" % f["check"])
        print("  found in GAC : %s" % ", ".join(f["found"]))
        print("  known good   : %s" % f["known_good"])
        print("  %s" % f["detail"])
    return 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
