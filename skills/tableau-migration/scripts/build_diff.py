#!/usr/bin/env python3
"""Compare two migration output trees and say what SUBSTANTIVELY changed.

Re-running a migration and diffing the two outputs is a normal thing to want -- after an engine
upgrade, when reproducing a defect, or to check that a change did what it claimed. Done naively it
is close to useless, because a handful of terms differ between any two runs for reasons that carry
no information:

===============================================================================================
term                what it is                                          why it differs
-----------------------------------------------------------------------------------------------
output root         the destination path, embedded in each model's M    you built to two
                    ``Source = Excel.Workbook(File.Contents("<root>\\   different directories
                    data\\..."))`` line, in three spellings: plain
                    Windows, POSIX, and JSON-escaped
lineageTag          Power BI's identity GUID for a column / table /     minted per build by any
                    measure                                             path that splices TMDL
                                                                        after stabilization
timestamp           ``generated_at`` / ``verified_at_utc`` and friends  wall clock
===============================================================================================

THIS MODULE CLASSIFIES; IT DOES NOT HIDE. Each term is counted and reported separately, so
"0 GUID differences" is distinguishable from "GUIDs were masked". That distinction is the whole
point: a differ that silently masks a term makes its own blind spot invisible, and the blind spot
then survives every comparison anyone runs with it.

WHY THIS IS A SHARED FUNCTION RATHER THAN A HABIT. In one day, five independent probes -- written
by two people who had each explicitly told the other about these terms -- re-implemented this
normalisation and got it wrong:

    missed lineageTag + root      reported 85 changed model files for a pill-binding change
    missed JSON-escaped root      reported 1 "unexplained" residual
    missed output root            reported 82 files of "non-determinism" that were two dir names
    masked root inside one tool   left every FRESH probe blind, because the mask lived in the
                                  tool rather than in a shared place

A normalisation that lives in one tool does not protect the next tool you write.

Usage::

    python build_diff.py <tree-a> <tree-b> [--show N] [--strict]

Exits 0 when nothing substantive differs, 1 otherwise (so it is usable as a gate), and 2 on a
usage/instrument error -- which is deliberately distinct from "found differences".
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

# A lineageTag / any identity GUID.
GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# ISO-8601 wall clock, with or without seconds.
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z?")

ROOT_TOKEN = "<BUILD-ROOT>"
GUID_TOKEN = "<GUID>"
TIMESTAMP_TOKEN = "<TIMESTAMP>"

# An emitted artifact names its own build root, and does so in a regular, self-identifying way:
# a model's M partition writes ``File.Contents("<root>\data\...")`` and the run metadata writes
# ``"model_folder": "<root>\\pbip\\..."``. That matters because the root embedded in the CONTENT is
# the root the tree was BUILT at, which stops being the tree's current location the moment anyone
# copies, archives or moves it -- which is exactly what people do to keep a baseline around.
_EMBEDDED_ROOT_RES = (
    re.compile(r'File\.Contents\("([^"]+?)[\\/]{1,2}data[\\/]'),
    re.compile(r'"(?:model_folder|project_folder|report_folder|pbip)"\s*:\s*"([^"]+?)[\\/]{1,2}pbip[\\/]'),
)


def infer_roots(text):
    """Build roots named by the artifact's own content.

    Returns a set. A tree that was built in place yields its own path; a tree that was relocated
    yields the path it was built at, which is the one actually written into every model file.
    Without this, comparing against an archived baseline reports every model file as a substantive
    change -- 82 of them on a byte-identical copy, measured.
    """
    found = set()
    for rx in _EMBEDDED_ROOT_RES:
        for m in rx.finditer(text):
            raw = m.group(1).replace("\\\\", "\\").rstrip("\\/")
            if raw:
                found.add(raw)
    return found


def root_spellings(root):
    """Every spelling of a build root that appears in an emitted artifact.

    A JSON file escapes a Windows separator, so ``C:\\tfmig\\out`` is written ``C:\\\\tfmig\\\\out``.
    Missing that one spelling is what turned a clean comparison into a single "unexplained"
    residual that looked like a real defect. Longest first, so the escaped form is consumed before
    the plain form can match half of it.

    Accepts a single root or an iterable of them; the returned list is ordered longest-first across
    all of them together, which is what makes a mixed set safe to apply in sequence.
    """
    roots = [root] if isinstance(root, str) else list(root or [])
    forms = set()
    for r in roots:
        r = str(r).rstrip("\\/")
        if not r:
            continue
        alt = r.replace("/", "\\")
        forms.update({alt.replace("\\", "\\\\"), alt, r.replace("\\", "/")})
    return sorted(forms, key=len, reverse=True)


def normalize(text, root):
    """Replace the three non-substantive terms. Returns ``(text, {term: hits})``.

    ``root`` may be one path or several. The hit counts are returned rather than discarded so a
    caller can assert a normaliser actually matched. A normaliser that matches nothing has told you
    about its own predicate, not about the data -- and it fails silently, because the un-normalised
    text simply compares unequal and looks like a real difference.
    """
    hits = {"root": 0, "guid": 0, "timestamp": 0}
    for form in root_spellings(root):
        n = text.count(form)
        if n:
            hits["root"] += n
            text = text.replace(form, ROOT_TOKEN)
    text, hits["guid"] = GUID_RE.subn(GUID_TOKEN, text)
    text, hits["timestamp"] = TIMESTAMP_RE.subn(TIMESTAMP_TOKEN, text)
    return text, hits


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _scan(root):
    """{relpath: raw bytes} for every file under ``root``."""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, root)] = _read(full)
    return out


def _classify(raw_a, raw_b, roots_a, roots_b):
    """Which terms explain the difference between two files?

    Applies the terms CUMULATIVELY and records the first point at which the two agree, so a file is
    attributed to the narrowest explanation that accounts for it rather than to whichever term
    happens to be checked first.
    """
    try:
        ta = raw_a.decode("utf-8-sig")
        tb = raw_b.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "binary", {}

    stats = {}
    for form in root_spellings(roots_a):
        ta = ta.replace(form, ROOT_TOKEN)
    for form in root_spellings(roots_b):
        tb = tb.replace(form, ROOT_TOKEN)
    if ta == tb:
        return "root", stats

    ta2, tb2 = GUID_RE.sub(GUID_TOKEN, ta), GUID_RE.sub(GUID_TOKEN, tb)
    if ta2 == tb2:
        stats["guid_tokens"] = sum(1 for x, y in zip(GUID_RE.findall(ta), GUID_RE.findall(tb))
                                   if x != y)
        return "guid", stats

    if TIMESTAMP_RE.sub(TIMESTAMP_TOKEN, ta2) == TIMESTAMP_RE.sub(TIMESTAMP_TOKEN, tb2):
        return "timestamp", stats

    return "substantive", stats


def _roots_for(tree_dir, files, override=None):
    """The set of build roots to normalise for one tree.

    The tree's own path AND every root its content names. They differ exactly when the tree has
    been relocated -- copied to an archive, moved, restored from a backup -- and in that case the
    embedded root is the one written into every model file, so keying only on the directory
    argument reports the whole model layer as changed.
    """
    roots = {str(tree_dir).rstrip("\\/")}
    if override:
        roots.add(str(override).rstrip("\\/"))
    for raw in files.values():
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        roots |= infer_roots(text)
    return {r for r in roots if r}


def compare(dir_a, dir_b, root_a=None, root_b=None):
    """Compare two migration output trees.

    Returns a dict with ``added`` / ``removed`` / ``changed`` path lists, a ``by_reason`` breakdown
    of the changed files, ``normalised`` hit counts per tree, and the ``roots`` actually used.
    Raises ``ValueError`` when either tree is empty -- a comparison of nothing and a comparison that
    found nothing print identically, and the first is the more likely outcome of a wrong path.

    ``root_a`` / ``root_b`` are optional explicit overrides. They are rarely needed, because the
    build root is inferred from the artifacts themselves; the override exists for a tree whose
    model layer was stripped, where there is nothing left to infer from.
    """
    a, b = _scan(dir_a), _scan(dir_b)
    if not a:
        raise ValueError("tree A is empty: %s" % dir_a)
    if not b:
        raise ValueError("tree B is empty: %s" % dir_b)

    roots_a = _roots_for(dir_a, a, root_a)
    roots_b = _roots_for(dir_b, b, root_b)

    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    common = sorted(set(a) & set(b))

    by_reason = {"root": [], "guid": [], "timestamp": [], "substantive": [], "binary": []}
    guid_tokens = 0
    for path in common:
        if a[path] == b[path]:
            continue
        reason, stats = _classify(a[path], b[path], roots_a, roots_b)
        by_reason[reason].append(path)
        guid_tokens += stats.get("guid_tokens", 0)

    changed = sorted(sum(by_reason.values(), []))
    # A file is attributed to exactly one reason, so the partition must be exact. A silently
    # overlapping partition would let a substantive change hide inside a noise bucket.
    if sum(len(v) for v in by_reason.values()) != len(changed):
        raise AssertionError("reason partition overlaps -- a changed file was counted twice")

    norm_a = {"root": 0, "guid": 0, "timestamp": 0}
    norm_b = {"root": 0, "guid": 0, "timestamp": 0}
    for src, roots, acc in ((a, roots_a, norm_a), (b, roots_b, norm_b)):
        for raw in src.values():
            try:
                _t, hits = normalize(raw.decode("utf-8-sig"), roots)
            except UnicodeDecodeError:
                continue
            for k in acc:
                acc[k] += hits[k]

    # A tree whose root normaliser matched NOTHING while the other matched plenty is the tell for a
    # relocated tree the inference could not resolve. Surfaced rather than left in the numbers: the
    # earlier version printed exactly this asymmetry (root=250 vs root=0) beside 82 phantom
    # "substantive" differences, and nothing consulted it.
    warnings = []
    if bool(norm_a["root"]) != bool(norm_b["root"]):
        warnings.append(
            "one tree's build root matched nothing (A=%d, B=%d) while the other matched. If a tree "
            "was copied or archived, pass --root-a/--root-b with the path it was BUILT at; "
            "otherwise its model files will read as substantive changes."
            % (norm_a["root"], norm_b["root"]))

    return {
        "files_a": len(a),
        "files_b": len(b),
        "added": added,
        "removed": removed,
        "changed": changed,
        "by_reason": by_reason,
        "guid_tokens_differing": guid_tokens,
        "normalised": {"a": norm_a, "b": norm_b},
        "roots": {"a": sorted(roots_a), "b": sorted(roots_b)},
        "warnings": warnings,
        "substantive": by_reason["substantive"] + added + removed,
    }


def format_report(res, show=20):
    lines = []
    lines.append("tree A: %d files   tree B: %d files" % (res["files_a"], res["files_b"]))
    na, nb = res["normalised"]["a"], res["normalised"]["b"]
    lines.append("normalised in A: root=%d guid=%d timestamp=%d" % (na["root"], na["guid"], na["timestamp"]))
    lines.append("normalised in B: root=%d guid=%d timestamp=%d" % (nb["root"], nb["guid"], nb["timestamp"]))
    lines.append("build roots A: %s" % ", ".join(res["roots"]["a"]))
    lines.append("build roots B: %s" % ", ".join(res["roots"]["b"]))
    for w in res.get("warnings") or []:
        lines.append("WARNING: %s" % w)
    lines.append("")
    lines.append("added   %d" % len(res["added"]))
    lines.append("removed %d" % len(res["removed"]))
    lines.append("changed %d" % len(res["changed"]))
    for reason in ("root", "guid", "timestamp", "binary", "substantive"):
        paths = res["by_reason"][reason]
        label = {
            "root": "build root only (not a defect -- you built to two directories)",
            "guid": "identity GUID only",
            "timestamp": "timestamp only",
            "binary": "binary, not compared",
            "substantive": "SUBSTANTIVE",
        }[reason]
        lines.append("   %-9s %4d   %s" % (reason, len(paths), label))
    if res["guid_tokens_differing"]:
        lines.append("   (%d individual GUID tokens differ)" % res["guid_tokens_differing"])
    lines.append("")
    subs = res["substantive"]
    if subs:
        lines.append("SUBSTANTIVE differences (%d):" % len(subs))
        for p in subs[:show]:
            lines.append("   %s" % p)
        if len(subs) > show:
            lines.append("   ... and %d more" % (len(subs) - show))
    else:
        lines.append("No substantive differences.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tree_a")
    ap.add_argument("tree_b")
    ap.add_argument("--show", type=int, default=20, help="how many substantive paths to list")
    ap.add_argument("--strict", action="store_true",
                    help="also treat identity-GUID differences as substantive")
    ap.add_argument("--root-a", help="the path tree A was BUILT at, if it has since been moved "
                                     "(rarely needed -- normally inferred from the artifacts)")
    ap.add_argument("--root-b", help="the path tree B was BUILT at, if it has since been moved")
    args = ap.parse_args(argv)

    for d in (args.tree_a, args.tree_b):
        if not os.path.isdir(d):
            sys.stderr.write("not a directory: %s\n" % d)
            return 2
    try:
        res = compare(args.tree_a, args.tree_b, root_a=args.root_a, root_b=args.root_b)
    except ValueError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    print(format_report(res, show=args.show))
    bad = list(res["substantive"])
    if args.strict:
        bad += res["by_reason"]["guid"]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
