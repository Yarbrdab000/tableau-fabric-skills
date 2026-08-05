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

try:
    from tmdl_generate import parse_relationships_tmdl
except Exception:  # pragma: no cover - tmdl_generate is a sibling module, always importable in-package
    def parse_relationships_tmdl(_text):  # type: ignore
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

# An `expression <Name> = ...` declaration in expressions.tmdl, and a `#"Name"` reference to one
# from anywhere else in the model. Together they decide whether every M parameter resolves.
_EXPRESSION_DECL_RE = re.compile(
    r"^expression\s+(?P<q>'?)(?P<name>[^'\s=]+)(?P=q)\s*=", re.MULTILINE)
_M_PARAM_REF_RE = re.compile(r'#"([^"\n]+)"')

# DAX IDENTIFIER REFERENCES inside a measure or calculated-column expression.
# ``'Table'[Column]`` / ``Table[Column]`` is a fully-qualified column; a bare ``[Name]`` is a
# measure reference (or an unqualified column in the declaring table's own row context).
_DAX_QUALIFIED_REF_RE = re.compile(r"(?:'((?:[^']|'')+)'|\b([A-Za-z_][\w ]*?))\[([^\]\r\n]+)\]")
_DAX_BARE_REF_RE = re.compile(r"(?<![\w'\]])\[([^\]\r\n]+)\]")
# A measure/calc-column declaration and the expression that follows it, so an unresolved reference
# can be reported against the object that carries it. Captures single-line and `= ```` block forms.
_MEASURE_DECL_RE = re.compile(
    r"^\tmeasure\s+(?P<name>'(?:[^']|'')*'|\"[^\"]*\"|[^\t\n=]+?)\s*=(?P<expr>.*?)(?=^\t(?:measure|column|partition|hierarchy)\s|\Z)",
    re.MULTILINE | re.DOTALL)
# Lines inside an object body that are metadata, not expression -- excluded before scanning for
# references so a preserved Tableau formula (`annotation TableauFormula = rank([Calculation_123])`)
# is never mistaken for a live DAX reference. That annotation legitimately names Tableau's internal
# ids, which do not and should not exist in the model.
_NON_EXPR_LINE_RE = re.compile(
    r"^\s*(?:annotation|lineageTag|formatString|displayFolder|description|isHidden|"
    r"dataType|summarizeBy|sourceColumn|dataCategory|///)\b.*$", re.MULTILINE)


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
    """Names that collide CASE-INSENSITIVELY within ``seq`` (each colliding occurrence past the
    first). Power BI's engine treats a table's column names as case-insensitive, so ``director`` and
    ``Director`` on one table are a genuine duplicate that makes the table object invalid and the
    ``.pbip`` unopenable ("Item 'Director' already exists in the collection"). Matching the engine --
    not a case-sensitive ``set`` -- is what lets this check catch that variant instead of reporting a
    false clean. An exact duplicate is just the trivial case of a case-insensitive one, so this stays
    a strict superset of the prior behaviour."""
    seen = set()
    dups = []
    for item in seq:
        key = item.casefold()
        if key in seen and item not in dups:
            dups.append(item)
        seen.add(key)
    return dups


def _expression_body(text):
    """Strip metadata lines from an object body, leaving only DAX expression text.

    A measure legitimately PRESERVES its Tableau source as
    ``annotation TableauFormula = rank([Calculation_123], 'desc')``. Those brackets name Tableau's
    internal ids, which do not exist in the model and must not -- scanning them as live references
    would make every faithfully-annotated measure look broken."""
    return _NON_EXPR_LINE_RE.sub("", text or "")


def _dax_references(expr):
    """``(qualified, bare)`` identifier references in a DAX expression.

    ``qualified`` is ``{(table, column)}`` from ``'Table'[Column]`` / ``Table[Column]``; ``bare`` is
    ``{name}`` from a standalone ``[Name]`` -- a measure reference, or a column named without its
    table inside its own row context. String literals are removed first so a quoted ``"[not a ref]"``
    is never scanned."""
    body = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr or "")
    qualified, bare = set(), set()
    for m in _DAX_QUALIFIED_REF_RE.finditer(body):
        tbl = _unquote("'%s'" % m.group(1)) if m.group(1) is not None else (m.group(2) or "").strip()
        if tbl:
            qualified.add((tbl, m.group(3).strip()))
    for m in _DAX_BARE_REF_RE.finditer(body):
        bare.add(m.group(1).strip())
    return qualified, bare


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
    declared_cf_by_table = {}   # table_name.casefold() -> {declared column display name.casefold()}

    for path in sorted(parts):
        if not _TABLE_PART_RE.match(path):
            continue
        text = parts[path] or ""
        table = _table_name(text) or path
        declared = _declared_columns(text)
        declared_cf_by_table[table.casefold()] = {d.casefold() for d in declared}

        for dup in _duplicates(declared):
            no_dupes = False
            issues.append({
                "check": "no_duplicate_columns",
                "table": table,
                "detail": "column %r collides case-insensitively with another column on this table" % dup,
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

    # relationship_columns_exist -- every relationship endpoint must reference a column that actually
    # exists (case-insensitively) on its table. Defense-in-depth for the case-collision rename: a
    # renamed physical join key whose relationship endpoint was NOT rewritten to match would dangle
    # onto a non-existent column -- this catches that loud instead of shipping a broken join. It is
    # secondary to the root-cause endpoint rewrite in assemble_model (which prevents the dangle in the
    # first place). Fail-safe: only runs when a relationships part exists; an endpoint whose table is
    # not among the parsed table parts is skipped (never flagged), and a malformed part never raises.
    rels_present = "definition/relationships.tmdl" in parts
    rels_ok = True
    if rels_present:
        try:
            parsed_rels = parse_relationships_tmdl(parts.get("definition/relationships.tmdl") or "")
        except Exception:
            parsed_rels = []
        for rel in parsed_rels:
            for tbl, col in ((rel.get("from_table"), rel.get("from_col")),
                             (rel.get("to_table"), rel.get("to_col"))):
                if not tbl or not col:
                    continue
                cols_cf = declared_cf_by_table.get(tbl.casefold())
                if cols_cf is None:
                    continue  # endpoint table not among parsed table parts -> fail-safe skip
                if col.casefold() not in cols_cf:
                    rels_ok = False
                    issues.append({
                        "check": "relationship_columns_exist",
                        "table": tbl,
                        "detail": "relationship references column %r not declared on table %r" % (col, tbl),
                    })

    # M PARAMETER REACHABILITY. Every partition that references #"Name" needs a matching
    # `expression Name` in expressions.tmdl, or the model cannot refresh -- it fails with
    # `The name 'Warehouse' wasn't recognized`. Nothing else here catches that: such a model is
    # syntactically valid TMDL, lints clean, and opens fine, so the defect surfaces far from its
    # cause and is easily misread as a credential or binding problem. Checked over the EMITTED
    # parts rather than the descriptor, so it holds regardless of which connector or code path
    # produced them, and catches any future emitter gap for free.
    params_ok = True
    defined_params = set()
    for m in _EXPRESSION_DECL_RE.finditer(parts.get("definition/expressions.tmdl") or ""):
        defined_params.add(m.group("name"))
    referenced_params = {}
    for path in sorted(parts):
        text = parts[path]
        if not (isinstance(text, str) and path.endswith(".tmdl")):
            continue
        if path.endswith("expressions.tmdl"):
            continue
        for m in _M_PARAM_REF_RE.finditer(text):
            referenced_params.setdefault(m.group(1), set()).add(path)
    for name in sorted(referenced_params):
        if name in defined_params:
            continue
        params_ok = False
        issues.append({
            "check": "m_parameters_defined",
            "part": sorted(referenced_params[name])[0],
            "detail": 'M parameter #"%s" is referenced by %s but never defined in '
                      "expressions.tmdl -- the model cannot refresh"
                      % (name, ", ".join(sorted(referenced_params[name]))),
        })

    # DAX REFERENCE RESOLUTION. Every ``[Measure]`` and ``'Table'[Column]`` a measure names must
    # exist in the model. Nothing else here catches an undefined identifier: such a model is
    # syntactically valid TMDL, lints clean, deserializes, and OPENS -- it fails only when the visual
    # runs the query, so the defect surfaces far from its cause and reads like a data problem. This
    # is the "deserializes clean, fails at query time" trap SKILL.md warns about, and it was the one
    # check an operator could reasonably believe `openability_selfcheck: ok` already covered.
    #
    # Measured on a real run: an assisted-translation pass authored DAX against Tableau's internal
    # calc ids (`[Calculation_2768024947633754122]`) instead of the resolved model names. Five
    # measures landed referencing objects that exist nowhere, three more were rebound onto them, and
    # the report announced 95% coverage with `openability_selfcheck.ok = true`. Ten honest inert
    # stubs had been traded for eight silent query-time errors, and coverage went UP.
    #
    # Deliberately conservative -- it must never fire on a sound model:
    #   * an unqualified `[Name]` resolves against measures OR any declared column (Power BI accepts
    #     an unqualified column reference in a row context);
    #   * comparison is case-insensitive, matching the engine;
    #   * `annotation`/metadata lines are stripped first, so a preserved Tableau formula (which
    #     legitimately names Tableau ids) is never scanned;
    #   * a reference to a table the model does not declare at all is left alone here -- that is a
    #     different failure and is not this check's business to guess at.
    refs_ok = True
    all_measures = set()
    all_columns = set()
    columns_by_table = {}
    for path in sorted(parts):
        text = parts[path]
        if not (isinstance(text, str) and path.endswith(".tmdl")):
            continue
        tname = _table_name(text)
        for c in _declared_columns(text):
            all_columns.add(c.casefold())
            if tname:
                columns_by_table.setdefault(tname.casefold(), set()).add(c.casefold())
        for m in _MEASURE_DECL_RE.finditer(text):
            all_measures.add(_unquote(m.group("name")).casefold())

    for path in sorted(parts):
        text = parts[path]
        if not (isinstance(text, str) and path.endswith(".tmdl")):
            continue
        for m in _MEASURE_DECL_RE.finditer(text):
            owner = _unquote(m.group("name"))
            qualified, bare = _dax_references(_expression_body(m.group("expr")))
            for tbl, col in sorted(qualified):
                known = columns_by_table.get(tbl.casefold())
                if known is None:
                    continue          # unknown table -- not this check's call
                if col.casefold() in known or col.casefold() in all_measures:
                    continue
                refs_ok = False
                issues.append({
                    "check": "dax_references_resolve",
                    "part": path,
                    "detail": "measure %r references '%s'[%s], which the model does not declare -- "
                              "the model opens but errors when the visual queries it"
                              % (owner, tbl, col),
                })
            for name in sorted(bare):
                if name.casefold() in all_measures or name.casefold() in all_columns:
                    continue
                refs_ok = False
                issues.append({
                    "check": "dax_references_resolve",
                    "part": path,
                    "detail": "measure %r references [%s], which is neither a measure nor a column "
                              "in the model -- the model opens but errors when the visual queries it"
                              % (owner, name),
                })

    checks = {
        "tmdl_wellformed": wellformed,
        "no_duplicate_columns": no_dupes,
        "typed_columns_declared": typed_declared,
        "m_parameters_defined": params_ok,
        "dax_references_resolve": refs_ok,
    }
    if header_check_ran:
        checks["typed_columns_in_header"] = typed_in_header
    if rels_present:
        checks["relationship_columns_exist"] = rels_ok

    return {"ok": not issues, "checks": checks, "issues": issues}
