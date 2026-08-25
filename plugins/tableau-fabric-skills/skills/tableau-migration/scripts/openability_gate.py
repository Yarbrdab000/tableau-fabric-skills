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
# The placeholder partition an untranslatable connector emits (#134): a table that DECLARES a schema
# but has no real query behind it. Whitespace-tolerant, because the emitted M is indented.
_STUB_PARTITION_RE = re.compile(r"#table\s*\(\s*type\s+table\s*\[\s*\]\s*,\s*\{\s*\}\s*\)")
# A calculated table/partition -- the eagerly-evaluated kind, which fails at model LOAD rather than
# at refresh. TMDL spells the mode either as ``mode: calculated`` or ``source = <DAX>`` under a
# ``partition ... = calculated`` header.
_CALCULATED_PARTITION_RE = re.compile(r"=\s*calculated\b|mode:\s*calculated\b")
# A literal ``'Table'[Column]`` reference inside DAX. Only the quoted form is matched: an unquoted
# table reference cannot contain the characters that make this ambiguous, and matching it would
# start flagging measure names.
_EAGER_TABLE_REF_RE = re.compile(r"'((?:[^']|'')+)'\[([^\]]+)\]")
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

# One connection-parameter declaration: its KIND (which routing fact), its per-upstream SUFFIX
# (empty for a single-upstream model, which keeps the bare names) and its literal VALUE. Used by the
# ``endpoints_distinct`` check to reassemble how many DISTINCT upstreams the emitted model actually
# resolves. Value is read non-greedily to the closing quote so the trailing ``meta [...]`` is ignored.
_ENDPOINT_DECL_RE = re.compile(
    r"^expression\s+'?(?P<kind>Server|Database|Warehouse|HttpPath)(?:_(?P<suffix>[^'\s=]+))?'?\s*="
    r'\s*"(?P<value>[^"]*)"',
    re.MULTILINE)

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


# The lowest model compatibility level that survives a refresh + cold open on a current Power BI
# Desktop. Kept in step with tmdl_generate.MODEL_COMPATIBILITY_LEVEL; a model emitted below this is
# upgraded in memory and then refuses to reopen once a refresh has written cache.abf.
MIN_COMPATIBILITY_LEVEL = 1606

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


def _split_qualified(token):
    """``'Date (Service Delivery)'.Date`` or ``Table.Column`` -> ``(table, column)``.

    TMDL quotes any name containing a space or punctuation, so a naive split on the first ``.``
    truncates ``'Date (Service Delivery)'`` and silently attributes the relationship to a table that
    does not exist -- which makes the ambiguity check report confident nonsense rather than fail.
    """
    token = (token or "").strip()
    if token.startswith("'"):
        end = token.find("'", 1)
        if end < 0:
            return token.strip("'"), ""
        table, rest = token[1:end], token[end + 1:].lstrip(".")
    else:
        table, _, rest = token.partition(".")
    rest = rest.strip()
    if rest.startswith("'") and rest.endswith("'") and len(rest) > 1:
        rest = rest[1:-1]
    return table, rest


def _parse_relationships(text):
    """``relationships.tmdl`` -> ``[{from_table, to_table, active, both}, ...]``. Never raises."""
    out = []
    for blk in re.split(r"(?m)^relationship\s+", text or "")[1:]:
        fm = re.search(r"(?m)^\s*fromColumn:\s*(.+)$", blk)
        tm = re.search(r"(?m)^\s*toColumn:\s*(.+)$", blk)
        if not (fm and tm):
            continue
        out.append({
            "from_table": _split_qualified(fm.group(1))[0],
            "to_table": _split_qualified(tm.group(1))[0],
            "active": not re.search(r"(?m)^\s*isActive:\s*false", blk),
            "both": bool(re.search(r"(?m)^\s*crossFilteringBehavior:\s*bothDirections", blk)),
        })
    return out


def _ambiguous_relationship_pairs(text, limit=12):
    """``[(source, target, [path_a, path_b]), ...]`` for pairs with >1 ACTIVE filter path.

    Filter propagation runs ONE -> MANY, i.e. ``toColumn``'s table -> ``fromColumn``'s table, with a
    bidirectional relationship traversable both ways. Paths are capped in length and count so a
    dense model cannot make this expensive; the goal is to name the defect, not to enumerate it.
    """
    adjacency = {}
    for rel in _parse_relationships(text):
        if not (rel["active"] and rel["from_table"] and rel["to_table"]):
            continue
        adjacency.setdefault(rel["to_table"], set()).add(rel["from_table"])
        if rel["both"]:
            adjacency.setdefault(rel["from_table"], set()).add(rel["to_table"])

    nodes = sorted(set(adjacency) | {t for v in adjacency.values() for t in v})
    found = []
    for src in nodes:
        for dst in nodes:
            if src == dst or len(found) >= limit:
                continue
            paths, stack = [], [(src, [src])]
            while stack and len(paths) < 2:
                node, path = stack.pop()
                for nxt in sorted(adjacency.get(node, ())):
                    if nxt in path:
                        continue
                    if nxt == dst:
                        paths.append(path + [nxt])
                        if len(paths) >= 2:
                            break
                    elif len(path) < 6:
                        stack.append((nxt, path + [nxt]))
            if len(paths) > 1:
                found.append((src, dst, paths))
    return found


def check_model_openability(parts, flatfile_headers=None, expected_endpoints=None):
    """Structurally validate a built model's ``parts`` dict; return a verdict.

    ``parts`` -- the ``{relative_path: tmdl_text}`` mapping ``assemble_import_model`` returns.
    ``flatfile_headers`` -- optional ``{table_display_name: [physical_header, ...]}`` map; when a
    table's headers are supplied the ``typed_columns_in_header`` check runs for it.
    ``expected_endpoints`` -- optional count of DISTINCT upstreams the source datasource declares;
    when supplied the ``endpoints_distinct`` check runs (see below). Omitted -> that check is
    skipped and the verdict is byte-identical to before.

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

    # ENDPOINT DISTINCTNESS. Every check above asks whether the model is well FORMED. This one asks
    # whether it points at the right NUMBER OF PLACES -- the one question nothing else was asking.
    #
    # A model whose tables have been collapsed onto a single upstream is structurally perfect: valid
    # TMDL, lints clean, passes every other check here, opens in Desktop, and REFRESHES SUCCESSFULLY.
    # It then reads the wrong server and returns wrong data, with no signal anywhere. That is one rung
    # further from the cause than the `m_parameters_defined` case (which at least fails at refresh),
    # and it is the rung where nothing at all was watching. ``m_parameters_defined`` cannot see it:
    # it asks whether each REFERENCED parameter is DEFINED, and in a collapsed model both are.
    #
    # This is not hypothetical -- it shipped. A workbook consolidating two plain single-connection
    # datasources on DIFFERENT servers emitted one shared parameter set, so the second fact silently
    # read the first server's data (fixed in 2.70.0). The corpus could not catch it either: it is
    # entirely flat-file, and a flat-file partition references no connection parameters at all.
    #
    # Counted by SUFFIX GROUP because that is the mechanism the emitter uses -- each distinct upstream
    # gets its own ``Server_<x>`` / ``Database_<x>`` / ``Warehouse_<x>`` / ``HttpPath_<x>`` set, and a
    # single-upstream model keeps the bare unsuffixed names. Grouping then deduping by VALUE TUPLE
    # matches ``_connection_identity``'s content identity, so two named connections that genuinely
    # describe the same upstream legitimately collapse to one group and do NOT trip the check.
    # Fail-closed: skipped entirely unless the caller supplies ``expected_endpoints``.
    #
    # ``not_evaluated`` (#141): this check has THREE ways of not running, and before that key existed
    # only two of them were detectable. A caller that supplies no count, or a count of 1, leaves the
    # key ABSENT -- recoverable by an operator who knows to look. But the third way, entering the
    # branch and finding no parameter groups, still wrote ``endpoints_distinct: true`` at the
    # assembly site below, so "evaluated, model is clean" and "could not evaluate anything" were
    # indistinguishable. Measured on the 29-workbook corpus: 3 models report an affirmative pass
    # having read nothing at all -- they emit no ``expressions.tmdl`` whatsoever. Since this check's
    # own failure text is "this model refreshes successfully and returns wrong data", overstating how
    # often it ran is the single worst direction to be wrong in.
    endpoints_ok = True
    endpoints_not_evaluated = None
    if expected_endpoints is None:
        endpoints_not_evaluated = ("the caller supplied no expected endpoint count, so there is "
                                   "nothing to compare the model against")
    elif int(expected_endpoints) <= 1:
        endpoints_not_evaluated = ("the source declares a single upstream, so endpoints cannot "
                                   "collapse onto each other")
    if expected_endpoints is not None and int(expected_endpoints) > 1:
        groups = {}
        for m in _ENDPOINT_DECL_RE.finditer(parts.get("definition/expressions.tmdl") or ""):
            groups.setdefault(m.group("suffix") or "", {})[m.group("kind")] = m.group("value")
        resolved = {tuple(sorted(g.items())) for g in groups.values() if g}
        # A model that emits NO endpoint parameters at all cannot be judged this way: a FLAT-FILE
        # island reaches its upstream through a literal ``File.Contents("...")`` path inside the
        # partition, not through a shared parameter, so several distinct files legitimately produce
        # zero parameter groups. Reading that as "collapsed to 0 endpoints" is a false positive --
        # measured on the corpus, which is entirely flat-file and where three multi-datasource
        # workbooks (Excel+Access, and two multi-file consolidations) tripped it. The check is about
        # PARAMETERISED endpoints; with none present it has nothing to say and stays silent.
        #
        # The exemption is correct and stays. What changes is only how the NON-ANSWER is reported:
        # silence is now recorded as such rather than published as a pass.
        if not resolved:
            endpoints_not_evaluated = (
                "the model declares no parameterised endpoints, so there is nothing to compare -- a "
                "flat-file island reaches its source by literal path inside the partition rather "
                "than through a shared parameter, so zero groups is legitimate, not a collapse")
        elif len(resolved) < int(expected_endpoints):
            endpoints_ok = False
            issues.append({
                "check": "endpoints_distinct",
                "part": "definition/expressions.tmdl",
                "detail": "the source declares %d distinct upstream(s) but the model resolves only "
                          "%d -- tables have been collapsed onto a shared endpoint and will read "
                          "the wrong source (this model refreshes successfully and returns wrong "
                          "data)" % (int(expected_endpoints), len(resolved)),
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
    if expected_endpoints is not None and int(expected_endpoints) > 1:
        checks["endpoints_distinct"] = endpoints_ok
    if header_check_ran:
        checks["typed_columns_in_header"] = typed_in_header
    if rels_present:
        checks["relationship_columns_exist"] = rels_ok

    # COMPATIBILITY LEVEL. A level BELOW what the local Desktop uses does not fail at build time and
    # does not fail on the first open -- Desktop silently upgrades the model in memory. It fails on
    # the NEXT COLD OPEN, after a refresh has persisted ``.pbi/cache.abf`` at the upgraded level:
    #
    #   Tabular databases do not support CompatibilityLevel downgrade.
    #   Current CompatibilityLevel: '1606'. Requested CompatibilityLevel: '1604'.
    #
    # The report then does not open at all. This is exactly the shape this gate exists for -- a model
    # that is structurally broken in a way no build-time signal reports -- and it is invisible to any
    # check made against a still-loaded Desktop session, which is how it shipped. Hermetic: reads the
    # emitted ``database.tmdl`` only, so it costs microseconds and needs no Desktop.
    db_part = next((p for p in parts if p.endswith("database.tmdl")), None)
    if db_part:
        _m = re.search(r"compatibilityLevel:\s*(\d+)", parts[db_part] or "")
        _lvl = int(_m.group(1)) if _m else None
        cl_ok = _lvl is not None and _lvl >= MIN_COMPATIBILITY_LEVEL
        checks["compatibility_level_current"] = cl_ok
        if not cl_ok:
            issues.append({
                "check": "compatibility_level_current",
                "part": db_part,
                "detail": (
                    "compatibilityLevel %s is below the %d Power BI Desktop writes. Desktop will "
                    "upgrade the model in memory, a refresh will persist cache.abf at the upgraded "
                    "level, and the next COLD open will fail with 'Tabular databases do not support "
                    "CompatibilityLevel downgrade' -- the report will not open."
                    % (_lvl if _lvl is not None else "(absent)", MIN_COMPATIBILITY_LEVEL)),
            })

    # EAGER CALCULATED-TABLE REFERENCES INTO A STUB. A distinct failure class from the dangling /
    # collapsed-upstream categories above, and worth its own check because of WHEN it fires (#134).
    #
    # A calculated table is evaluated EAGERLY at model LOAD. An Import column errors on refresh; a
    # calculated table errors on OPEN -- before any refresh, before any credential -- with
    #     Column 'X' in table 'Y' cannot be found or may not be used in this expression.
    # so the file simply does not open and no later gate ever runs. Reported at 11 of ~44 field
    # models, and invisible to the definition-of-done report.
    #
    # Three gates that are not the same thing, and this sits in the gap between them:
    #   1. deserializes -- TmdlSerializer / powerbi-report-author validate. Shape only.
    #   2. OPENS in Desktop -- eager evaluation of calculated tables.   <- this check
    #   3. refreshes -- M execution and column binding.
    #
    # Conservative by construction: only a table whose partition IS the placeholder counts as a stub,
    # and only a LITERAL 'Table'[Column] reference inside a calculated table's own expression counts
    # as eager. Anything it cannot parse is skipped rather than flagged.
    # Only a stub whose referenced COLUMN is undeclared can break the open. Settled by experiment,
    # which is what the reporter asked for ("the most useful thing a maintainer could tell me is
    # which condition makes the difference -- most likely a variant where the Tableau schema yielded
    # ZERO columns for the stub"). Their hypothesis was right:
    #
    #   stub DECLARES the column  -> the reference RESOLVES; Desktop opens, degraded, showing
    #                                "One or more calculated objects need to be manually refreshed"
    #                                and "Some of the tables have incomplete or no data".
    #                                Measured by cold-opening 0083_previous_workday, whose Date
    #                                calendar spans a textscan stub that declares 6 columns.
    #   stub declares NOTHING     -> the reference cannot resolve; the calculated table fails during
    #                                eager evaluation at model LOAD and the file does not open.
    #
    # So the check keys on the undeclared column, not on the mere presence of a stub -- otherwise it
    # fires on every degraded-but-openable model, which is a different (and already reported)
    # condition. The first version of this check did exactly that and failed the corpus.
    stub_columns = {}
    for path in sorted(parts):
        if not _TABLE_PART_RE.match(path):
            continue
        text = parts[path] or ""
        if not _STUB_PARTITION_RE.search(text):
            continue
        tname = _table_name(text)
        if tname:
            stub_columns[tname.casefold()] = {c.casefold() for c in _declared_columns(text)}

    eager_ok = True
    if stub_columns:
        for path in sorted(parts):
            if not _TABLE_PART_RE.match(path):
                continue
            text = parts[path] or ""
            if not _CALCULATED_PARTITION_RE.search(text):
                continue
            owner = _table_name(text) or path
            for ref_table, ref_col in _EAGER_TABLE_REF_RE.findall(text):
                key = ref_table.replace("''", "'").casefold()
                if key not in stub_columns:
                    continue
                if ref_col.strip().casefold() in stub_columns[key]:
                    continue          # declared -> resolves -> opens (degraded), not a break
                eager_ok = False
                issues.append({
                    "check": "eager_calc_refs_resolve",
                    "table": owner,
                    "part": path,
                    "detail": (
                        "calculated table %r references '%s'[%s], and that STUB table (placeholder "
                        "partition) does not DECLARE that column. A calculated table is evaluated "
                        "eagerly at model LOAD, so this fails when the file is OPENED -- before any "
                        "refresh and before any credential -- with \"Column '%s' in table '%s' "
                        "cannot be found or may not be used in this expression\"."
                        % (owner, ref_table, ref_col, ref_col, ref_table)),
                })
        checks["eager_calc_refs_resolve"] = eager_ok

    # ``unambiguous_relationship_paths`` -- Power BI REFUSES TO OPEN a project in which two tables
    # have more than one ACTIVE filter path between them: *"There's a problem with the definition
    # content in your Power BI Project. There are ambiguous paths between 'X' and 'Y'"*. The model
    # sitting beside that report becomes unreachable too, so this is the same severity as invalid
    # TMDL, not a fidelity nit.
    #
    # Reported from the field on a Salesforce NPSP rebuild: `Date (Service Delivery)` reached
    # `pmdm__ServiceDelivery__c` BOTH directly and through `pmdm__ProgramEngagement__c`. Nothing was
    # malformed -- every relationship was individually correct and correctly typed, and the defect
    # existed only in the RELATION BETWEEN two of them, which is precisely the shape no per-object
    # check can see. `validate` passed, `pbir_lint` passed, definition-of-done said warn/ok, and the
    # file did not open.
    #
    # Direction is the whole check: a relationship runs `from` (MANY) -> `to` (ONE), and a filter
    # propagates the other way, ONE -> MANY. Modelled undirected, a legal star (one calendar, two
    # facts) looks ambiguous and this gate would fail every healthy model -- measured, 90 false pairs
    # on the very model that has 4 real ones.
    rel_text = "\n".join(parts[p] or "" for p in sorted(parts)
                         if p.endswith("relationships.tmdl"))
    unambiguous = True
    if rel_text.strip():
        for a, b, paths in _ambiguous_relationship_pairs(rel_text):
            unambiguous = False
            issues.append({
                "check": "unambiguous_relationship_paths",
                "table": a,
                "detail": ("two active filter paths reach %r from %r (%s | %s) -- Power BI refuses "
                           "to OPEN a project with an ambiguous path; make one of the relationships "
                           "on the second route inactive"
                           % (b, a, " -> ".join(paths[0]), " -> ".join(paths[1]))),
            })
    checks["unambiguous_relationship_paths"] = unambiguous

    # ``not_evaluated`` (#141) states a NON-ANSWER positively instead of leaving it to be inferred.
    # Additive by construction: ``ok``, ``checks`` and ``issues`` are untouched and keep their exact
    # meanings, so no existing consumer changes behaviour. Entries follow the same shape as
    # ``issues`` (``check`` + prose), and the flat roster an aggregate wants is
    # ``[e["check"] for e in selfcheck["not_evaluated"]]``.
    #
    # Note ``endpoints_distinct`` is still written to ``checks`` exactly as before when the branch is
    # entered -- deliberately. Removing it there would be the smaller diff and gives the tidier
    # invariant ("present => evaluated"), but it would change the meaning of an ABSENT key for
    # anything already reading this payload, and the report schema is additive-only. Cross-reference
    # the two keys for the tri-state: a check named here did not run, whatever ``checks`` says.
    not_evaluated = []
    if endpoints_not_evaluated:
        not_evaluated.append({"check": "endpoints_distinct",
                              "reason": endpoints_not_evaluated})
    return {"ok": not issues, "checks": checks, "issues": issues,
            "not_evaluated": not_evaluated}
