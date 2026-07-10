"""Hermetic model-openability self-check (a machine definition-of-done for the model build).

The migration pipeline can, in rare defect paths, emit a semantic model that is structurally
BROKEN -- one that Power BI Desktop / TOM refuses to open, or that opens but fails to load data.
Two real After-Action-Report incidents produced exactly this: a local-CSV import with a duplicate
column declaration (invalid TMDL), and a phantom column typed in M against a header the physical
file never had (load failure). In both cases the run still reported "success".

This module is the backstop: a pure-Python, dependency-free structural gate over the ALREADY-BUILT
model ``parts`` (the ``{path: text}`` dict ``assemble_import_model`` returns). It never opens a file,
never touches TOM/.NET, and never modifies anything -- so it is safe to run inside the ordinary
pytest gate and on every migration. It surfaces its verdict as the additive
``report["openability_selfcheck"]`` key so a run can no longer *claim* success while emitting a model
that will not open.

It is deliberately DISTINCT from ``fidelity_oracle.openability_tier`` (the heavy, opt-in TOM "Gate 0"
that actually loads the model in the AS engine and owns ``report["openability"]``). This is the cheap,
always-on, hermetic sibling -- the two never collide on a report key.

Checks (each conservative / warn-never-wrong -- a check only fails on a genuine structural defect):

* ``tmdl_wellformed``       -- every ``.tmdl`` part passes :func:`tmdl_lint.lint_tmdl_text` (no
                               empty-value annotations, no column-0 / under-indented multi-line body).
                               These are the exact defects that have left a model unopenable in TOM.
* ``no_duplicate_columns``  -- no table declares the same ``column`` name twice (a duplicate makes the
                               table object invalid).
* ``typed_columns_declared``-- every column named in an M ``Table.TransformColumnTypes(...)`` is
                               declared as a column in that same table (by ``sourceColumn`` or display
                               name) -- so the M step and the column set agree.
* ``typed_columns_in_header`` (only when physical headers are supplied) -- every column the M step
                               types is an actual header of the landed flat file. This is the machine
                               enforcement of the local-CSV dedupe guarantee: a phantom typed column
                               (typed but absent from the CSV) is caught regardless of code path.

Fail-safe throughout: a table with no columns, no ``Table.TransformColumnTypes``, or (for the header
check) no readable header is simply skipped, never flagged.
"""
from __future__ import annotations

import re

try:
    from tmdl_lint import lint_tmdl_text
except Exception:  # pragma: no cover - tmdl_lint is a sibling module, always importable in-package
    def lint_tmdl_text(_text):  # type: ignore
        return []

_TABLE_PART_RE = re.compile(r"^definition/tables/.+\.tmdl$")
# a top-level ``table <name>`` declaration (name bare or quoted)
_TABLE_DECL_RE = re.compile(r"^table\s+(?P<name>'(?:[^']|'')*'|\"[^\"]*\"|\S+)", re.MULTILINE)
# a table-level ``column <name>`` declaration: exactly one leading tab, then ``column``.
# The name may be bare or quoted and a calc column adds `` = <expr>`` which we strip.
_COLUMN_DECL_RE = re.compile(r"^\tcolumn\s+(?P<name>'(?:[^']|'')*'|\"[^\"]*\"|[^\t\n=]+?)\s*(?:=|$)", re.MULTILINE)
# a ``sourceColumn: <value>`` property (bare or quoted)
_SOURCE_COL_RE = re.compile(r"^\t+sourceColumn:\s*(?P<name>'(?:[^']|'')*'|\"[^\"]*\"|\S+)", re.MULTILINE)
# the first quoted string inside each ``{ "Col", <type> }`` pair of a column-type list
_TYPE_PAIR_RE = re.compile(r"\{\s*\"((?:[^\"\\]|\\.)*)\"\s*,")


def _unquote(token):
    """Normalise a TMDL identifier: strip surrounding ``'..'``/``".."`` and unescape a doubled quote."""
    if token is None:
        return ""
    t = token.strip()
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        return t[1:-1].replace("''", "'")
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1]
    return t


def _table_name(text):
    m = _TABLE_DECL_RE.search(text)
    return _unquote(m.group("name")) if m else None


def _declared_columns(text):
    """Ordered list of declared column display names in a table part."""
    return [_unquote(m.group("name")) for m in _COLUMN_DECL_RE.finditer(text)]


def _source_columns(text):
    """Set of ``sourceColumn`` values declared in a table part (the physical source names)."""
    return {_unquote(m.group("name")) for m in _SOURCE_COL_RE.finditer(text)}


def _typed_columns(text):
    """Column names typed by every ``Table.TransformColumnTypes(...)`` step in a partition's M.

    Scopes extraction to the balanced ``{...}`` type-list argument of each
    ``Table.TransformColumnTypes`` call so that column names from OTHER M steps (e.g.
    ``Table.RenameColumns``) are never mistaken for typed columns.
    """
    names = []
    for call in re.finditer(r"Table\.TransformColumnTypes\s*\(", text):
        # find the first '{' after the opening paren, then walk to its matching '}'
        start = text.find("{", call.end())
        if start == -1:
            continue
        depth = 0
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            continue
        segment = text[start:end + 1]
        names.extend(_TYPE_PAIR_RE.findall(segment))
    return names


def _duplicates(seq):
    seen = set()
    dups = []
    for item in seq:
        if item in seen and item not in dups:
            dups.append(item)
        seen.add(item)
    return dups


def check_model_openability(parts, flatfile_headers=None):
    """Structurally validate a built model's ``parts`` dict; return a verdict.

    ``parts`` -- the ``{relative_path: tmdl_text}`` mapping ``assemble_import_model`` returns.
    ``flatfile_headers`` -- optional ``{table_display_name: [physical_header, ...]}`` map; when a
    table's headers are supplied the ``typed_columns_in_header`` check runs for it.

    Returns ``{"ok": bool, "checks": {name: bool}, "issues": [{"check", "table"/"part", "detail"}]}``.
    ``ok`` is True iff no issue was found. Purely diagnostic -- never raises, never mutates ``parts``.
    """
    parts = parts or {}
    flatfile_headers = flatfile_headers or {}
    issues = []

    wellformed = True
    for path in sorted(parts):
        if not path.endswith(".tmdl"):
            continue
        for violation in lint_tmdl_text(parts[path] or ""):
            wellformed = False
            issues.append({"check": "tmdl_wellformed", "part": path, "detail": violation})

    no_dupes = True
    typed_declared = True
    typed_in_header = True
    header_check_ran = False

    for path in sorted(parts):
        if not _TABLE_PART_RE.match(path):
            continue
        text = parts[path] or ""
        table = _table_name(text) or path
        declared = _declared_columns(text)

        for dup in _duplicates(declared):
            no_dupes = False
            issues.append({
                "check": "no_duplicate_columns",
                "table": table,
                "detail": "column %r is declared more than once" % dup,
            })

        typed = _typed_columns(text)
        if typed:
            source_names = _source_columns(text)
            declared_set = set(declared)
            for tc in typed:
                if tc not in source_names and tc not in declared_set:
                    typed_declared = False
                    issues.append({
                        "check": "typed_columns_declared",
                        "table": table,
                        "detail": "M types column %r but no column declares it" % tc,
                    })

            headers = flatfile_headers.get(table)
            if headers is not None:
                header_check_ran = True
                header_set = set(headers)
                for tc in typed:
                    if tc not in header_set:
                        typed_in_header = False
                        issues.append({
                            "check": "typed_columns_in_header",
                            "table": table,
                            "detail": "M types column %r which is not a physical header of the landed file" % tc,
                        })

    checks = {
        "tmdl_wellformed": wellformed,
        "no_duplicate_columns": no_dupes,
        "typed_columns_declared": typed_declared,
    }
    if header_check_ran:
        checks["typed_columns_in_header"] = typed_in_header

    return {"ok": not issues, "checks": checks, "issues": issues}
