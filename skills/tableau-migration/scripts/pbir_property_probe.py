"""Discover the PBIR JSON for a Power BI feature by asking Power BI Desktop, not a schema.

Read-only, stdlib-only, offline.

**Why this exists.** Every published description of PBIR formatting properties lags Power BI Desktop
by at least a month, so at the moment a feature ships there is no authoritative source for the JSON
you must write:

===========================================  ==========================  ====================
source                                       version seen 2026-08-24      corresponds to
===========================================  ==========================  ====================
``@microsoft/powerbi-core-visual-schema``    0.1.1 (latest published)     no release tag at all
PBIR ``visualContainer`` json-schema         2.9.0                        May 2026
Report Theme JSON Schema                     ``reportThemeSchema-2.156``  July 2026
Power BI Desktop (installed)                 2.157.627.0                  **August 2026**
===========================================  ==========================  ====================

Worse, the PBIR schema cannot help even in principle: a visual's ``objects`` member resolves to
``DataViewObjectDefinitions``, which permits ARBITRARY object names and ARBITRARY properties. So
``powerbi-report-author validate`` returns 0 errors for a formatting property that does not exist,
and would do the same for one you invented. Measured: a hand-written slicer carrying
``data.relativeRange`` validated clean, and so would ``data.relativeRangeXYZZY``.

**Desktop itself is the oracle.** It knows every property in the release it shipped, and when it
saves a ``.pbip`` it writes them as PBIR JSON. So:

1. ``snapshot``  -- capture the report's formatting BEFORE touching anything;
2. a human sets the feature in Desktop's format pane and saves;
3. ``diff``      -- print exactly which JSON properties appeared or changed, with the literal
   values Desktop wrote, ready to copy into the emitter.

That is one human click away from an authoritative answer, on the day a feature ships, for any
feature -- which is worth more than any single property name.

``schema`` is the offline half: point it at a release-tagged Report Theme JSON Schema you already
have on disk and it lists a visual type's property names. Prefer it over the npm catalog the
``powerbi-report-author`` CLI reads -- measured on the same day, the theme schema knew
``outerPadding``, ``accentBar`` and a real ``centerValue`` that the npm catalog did not.

Usage::

    py -3.11 scripts/pbir_property_probe.py snapshot <report-dir> <baseline.json>
    py -3.11 scripts/pbir_property_probe.py diff     <report-dir> <baseline.json> [--all]
    py -3.11 scripts/pbir_property_probe.py schema   <themeSchema.json> <visualType> [pattern]

``<report-dir>`` is the ``.Report`` folder (or anything above it -- the first one found is used).
"""
from __future__ import annotations

import json
import os
import re
import sys

# Keys that change on every save without expressing a formatting CHOICE. Excluded from the diff by
# default so a real discovery is not buried; ``--all`` keeps them.
_NOISE_KEYS = frozenset({"name", "tabOrder", "z", "queryRef", "nativeQueryRef", "lineageTag"})


def find_report_dir(start):
    """The ``.Report`` directory at or under ``start`` (first match, deterministic order)."""
    start = os.path.abspath(start)
    if os.path.basename(start).endswith(".Report"):
        return start
    for base, dirs, _files in os.walk(start):
        for d in sorted(dirs):
            if d.endswith(".Report"):
                return os.path.join(base, d)
    raise SystemExit("no .Report directory found under %s" % start)


def _visual_files(report_dir):
    out = []
    for base, _dirs, files in os.walk(report_dir):
        for f in sorted(files):
            if f in ("visual.json", "page.json", "pages.json", "report.json"):
                out.append(os.path.join(base, f))
    return sorted(out)


def _flatten(node, path, out):
    """``{json path: scalar}`` for every leaf, so a diff can name the exact property."""
    if isinstance(node, dict):
        for k, v in node.items():
            _flatten(v, "%s/%s" % (path, k), out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _flatten(v, "%s[%d]" % (path, i), out)
    else:
        out[path] = node


def snapshot(report_dir):
    """``{relative file: {json path: scalar}}`` for every definition file in the report."""
    report_dir = find_report_dir(report_dir)
    snap = {}
    for p in _visual_files(report_dir):
        rel = os.path.relpath(p, report_dir).replace("\\", "/")
        try:
            doc = json.loads(open(p, encoding="utf-8-sig").read())
        except Exception as exc:  # a half-written file during a save -> record, never raise
            snap[rel] = {"<unreadable>": str(exc)}
            continue
        flat = {}
        _flatten(doc, "", flat)
        snap[rel] = flat
    return {"report_dir": report_dir, "files": snap}


def _is_noise(path):
    return any(seg in _NOISE_KEYS for seg in re.split(r"[/\[\]]", path) if seg)


def diff(baseline, current, include_noise=False):
    """``(added, changed, removed)`` -- each a list of ``(file, path, before, after)``."""
    added, changed, removed = [], [], []
    b_files, c_files = baseline["files"], current["files"]
    for rel in sorted(set(b_files) | set(c_files)):
        b, c = b_files.get(rel, {}), c_files.get(rel, {})
        for path in sorted(set(b) | set(c)):
            if not include_noise and _is_noise(path):
                continue
            if path not in b:
                added.append((rel, path, None, c[path]))
            elif path not in c:
                removed.append((rel, path, b[path], None))
            elif b[path] != c[path]:
                changed.append((rel, path, b[path], c[path]))
    return added, changed, removed


def _object_path(path):
    """``/visual/objects/<object>/[i]/properties/<prop>`` -> ``<visualType?>.<object>.<prop>``.

    Returns ``None`` for a path that is not a formatting property, so the report can lead with the
    ones that answer "what JSON do I emit".
    """
    m = re.search(r"/visual/objects/([^/\[]+)(?:\[\d+\])?/properties/([^/\[]+)", path)
    if m:
        return "%s.%s" % (m.group(1), m.group(2))
    m = re.search(r"/visual/visualContainerObjects/([^/\[]+)(?:\[\d+\])?/properties/([^/\[]+)", path)
    if m:
        return "VCO %s.%s" % (m.group(1), m.group(2))
    return None


def render_diff(added, changed, removed, stream=sys.stdout):
    """Human-readable report, formatting properties first -- that is the answer being looked for."""
    def _emit(title, rows):
        fmt = [(f, p, a, b) for (f, p, a, b) in rows if _object_path(p)]
        other = [(f, p, a, b) for (f, p, a, b) in rows if not _object_path(p)]
        stream.write("%s: %d (%d formatting)\n" % (title, len(rows), len(fmt)))
        for f, p, before, after in fmt:
            stream.write("   %-46s %s\n" % (_object_path(p), os.path.basename(os.path.dirname(f)) or f))
            if before is not None:
                stream.write("        before: %s\n" % json.dumps(before)[:120])
            stream.write("        after : %s\n" % json.dumps(after)[:120])
            stream.write("        path  : %s\n" % p)
        for f, p, before, after in other[:12]:
            stream.write("   (non-formatting) %s %s\n" % (f, p))
        if len(other) > 12:
            stream.write("   ... %d more non-formatting\n" % (len(other) - 12))
        stream.write("\n")

    _emit("ADDED", added)
    _emit("CHANGED", changed)
    _emit("REMOVED", removed)
    if not (added or changed or removed):
        stream.write("No differences. Did Desktop actually SAVE? "
                     "(File > Save, not just apply the format-pane change.)\n")


def schema_lookup(schema_path, visual_type, pattern=None):
    """``[(dotted path, title)]`` for a visual type in a release-tagged Report Theme JSON Schema.

    Offline by design: point this at a schema file already on disk. The repo never downloads.
    """
    doc = json.loads(open(schema_path, encoding="utf-8-sig").read())
    want = "visual-%s" % visual_type
    hits = []
    rx = re.compile(pattern, re.I) if pattern else None

    def walk(node, path, inside):
        if isinstance(node, dict):
            for k, v in node.items():
                here = inside or (k == want)
                if inside and isinstance(v, dict) and ("title" in v or "type" in v):
                    parts = [s for s in (path + "/" + k).split("/")
                             if s and s not in ("properties", "allOf", "anyOf", "oneOf", "items")
                             and not re.fullmatch(r"(allOf|anyOf|oneOf|items)\[\d+\]", s)]
                    # Drop everything up to and including ``visual-<type>``: the caller named the
                    # visual type, so the useful answer is the path they must EMIT (``centerValue.show``),
                    # not where it sits inside the theme schema.
                    if want in parts:
                        parts = parts[parts.index(want) + 1:]
                    dotted = ".".join(parts) if parts else k
                    title = str(v.get("title") or "")
                    if not rx or rx.search(dotted) or rx.search(title):
                        hits.append((dotted, title))
                walk(v, path + "/" + k, here)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i), inside)

    walk(doc, "", False)
    seen, out = set(), []
    for d, t in hits:
        if d not in seen:
            seen.add(d)
            out.append((d, t))
    return sorted(out)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "snapshot" and len(argv) >= 3:
        snap = snapshot(argv[1])
        with open(argv[2], "w", encoding="utf-8") as fh:
            json.dump(snap, fh)
        n = sum(len(v) for v in snap["files"].values())
        print("snapshot: %d files, %d leaf values -> %s" % (len(snap["files"]), n, argv[2]))
        print("Now set the feature in Power BI Desktop's format pane and SAVE, then run `diff`.")
        return 0
    if cmd == "diff" and len(argv) >= 3:
        baseline = json.loads(open(argv[2], encoding="utf-8").read())
        current = snapshot(argv[1])
        a, c, r = diff(baseline, current, include_noise="--all" in argv)
        render_diff(a, c, r)
        return 0
    if cmd == "schema" and len(argv) >= 3:
        pat = argv[3] if len(argv) > 3 else None
        for dotted, title in schema_lookup(argv[1], argv[2], pat):
            print("   %-58s %s" % (dotted[:58], title[:50]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
