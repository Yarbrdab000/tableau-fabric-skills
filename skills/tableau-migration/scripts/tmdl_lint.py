"""Pure-Python TMDL well-formedness linter -- a dependency-free openability guard.

TMDL (the text serialization the model build emits under ``definition/``) must satisfy
a couple of structural invariants or the whole model fails to open in Power BI Desktop /
TOM (reported as ``BLOCKED``). The two defects that have actually bitten us:

  1. A MULTI-LINE expression (e.g. a ``VAR ... RETURN ... SWITCH`` measure body) emitted
     inline after ``measure 'X' = `` drops its continuation lines to column 0, so they are
     no longer read as part of the expression -> invalid TMDL.
  2. An EMPTY-VALUE annotation (``annotation TableauFormula = `` with nothing after the
     ``=``) has no valid TMDL form and fails to parse.

This module re-checks emitted TMDL text for exactly those two invariants, with **zero
external dependencies** (no TOM/pythonnet), so it can run inside the ordinary pytest gate
as a fast regression guard. It is intentionally conservative: it only flags the
bare-``=`` structural openers the serializer actually produces (``measure`` / ``column`` /
``source`` / ``calculationItem``), so valid single-line DAX and ordinary annotation values
never trip it.

It is a guard, not a parser: a clean result means "free of the two known openability
defects", not "provably openable" (that stronger check is the fidelity oracle's TOM Gate 0).
"""

import re

# A structural assignment opener whose right-hand side is deferred to an indented block:
# the line ends in a bare ``=`` (optionally trailing whitespace). Only the keywords the
# serializer emits as multi-line-capable assignments are considered, so an arbitrary DAX
# continuation line that happens to end in ``=`` (a wrapped comparison) is never mistaken
# for an opener.
_STRUCT_OPENER_RE = re.compile(
    r"^\t*(?:measure|column|source|calculationItem|expression)\b[^\n]*=[ \t]*$"
)

# ``annotation <name> =`` with no value after the ``=``. The name is a single identifier
# token, so a populated annotation whose value merely contains ``=`` is never matched.
_EMPTY_ANNOT_RE = re.compile(r"^\t*annotation\s+\S+\s*=[ \t]*$")

# Keywords that may legitimately begin a column-0 (zero-indent) line in TMDL. Everything
# else in a well-formed file is indented at least one tab, so a column-0 line that is not
# one of these is almost always an orphaned expression/M continuation that has fallen out
# of its block (exactly the multi-line defect the serializer fix addresses).
_TOP_LEVEL_KEYWORDS = frozenset({
    "model", "table", "relationship", "expression", "ref", "database", "role",
    "annotation", "cultureInfo", "queryGroup", "perspective", "extendedProperty",
    "createOrReplace",
})


def _indent_depth(line):
    """Number of leading TAB characters (TMDL indents with tabs)."""
    return len(line) - len(line.lstrip("\t"))


def _is_comment(stripped):
    return stripped.startswith(("/*", "*/", "*", "//"))


def _is_allowed_top_level(line):
    """True if a zero-indent ``line`` is a legitimate TMDL top-level declaration (or comment)."""
    stripped = line.lstrip("\t").rstrip()
    if not stripped or _is_comment(stripped):
        return True
    return stripped.split(None, 1)[0] in _TOP_LEVEL_KEYWORDS


def lint_tmdl_text(text):
    """Return a list of human-readable well-formedness violations for ``text``.

    An empty list means the TMDL is free of the two known openability defects
    (empty-value annotations and column-0 / sibling-level multi-line continuations).
    """
    violations = []
    lines = text.split("\n")
    n = len(lines)
    for i, line in enumerate(lines):
        if not line.strip():
            continue

        if _EMPTY_ANNOT_RE.match(line):
            violations.append(
                "line {0}: empty-value annotation has no valid TMDL form: {1!r}".format(
                    i + 1, line.strip()
                )
            )
            continue

        if _indent_depth(line) == 0 and not _is_allowed_top_level(line):
            violations.append(
                "line {0}: unexpected column-0 line -- not a TMDL top-level declaration, "
                "likely an orphaned expression/M continuation: {1!r}".format(
                    i + 1, line.strip()
                )
            )
            continue

        if _STRUCT_OPENER_RE.match(line):
            opener_indent = _indent_depth(line)
            keyword = line.lstrip("\t").split(None, 1)[0]
            # ``source`` is itself a property-level assignment (a property of a ``partition``):
            # its value block -- an M ``let``/``in`` partition body, or a calculated-table
            # expression -- sits exactly one level deeper than the ``source`` line, which is
            # the standard form TOM opens. Every other opener
            # (``measure`` / ``column`` / ``calculationItem`` / ``expression``) is an object
            # declaration whose sibling PROPERTIES sit at opener+1, so its multi-line body must
            # be deeper still (>= opener+2) or it would be read as a sibling property.
            max_sibling_indent = opener_indent if keyword == "source" else opener_indent + 1
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j >= n:
                violations.append(
                    "line {0}: assignment opener ends in '=' with no body block: {1!r}".format(
                        i + 1, line.strip()
                    )
                )
            elif _indent_depth(lines[j]) <= max_sibling_indent:
                violations.append(
                    "line {0}: multi-line body is not indented deeper than the property "
                    "level of its opener at line {1} (column-0 / sibling-level "
                    "continuation breaks openability): {2!r}".format(
                        j + 1, i + 1, lines[j].strip()
                    )
                )
    return violations


def lint_tmdl_file(path):
    """Lint a single ``.tmdl`` file (read BOM-tolerant). Returns the violation list."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        return lint_tmdl_text(fh.read())


def _main(argv):
    import glob
    import os

    targets = []
    for arg in argv:
        if os.path.isdir(arg):
            targets.extend(
                glob.glob(os.path.join(arg, "**", "*.tmdl"), recursive=True)
            )
        else:
            targets.append(arg)

    total = 0
    for path in targets:
        problems = lint_tmdl_file(path)
        if problems:
            total += len(problems)
            print(path)
            for p in problems:
                print("  " + p)
    if total:
        print("FAIL: {0} TMDL well-formedness violation(s)".format(total))
        return 1
    print("OK: {0} file(s) clean".format(len(targets)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main(sys.argv[1:]))
