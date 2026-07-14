"""Model-aware reference gate -- the *structural* half of the second-compiler validation gate.

``check_candidate_dax`` (in :mod:`translation_router`) is the SYNTACTIC gate: it proves a candidate
is well-formed DAX that is not the inert stub. The reconciliation oracle (:mod:`reconciliation_oracle`)
is the EMPIRICAL gate: it proves the candidate is numerically faithful to the original Tableau calc.
NEITHER proves that every ``[Measure]`` / ``'Table'[Column]`` the candidate names actually EXISTS in
the generated model. That STRUCTURAL proof is what this module provides.

The v1.43 operator After-Action Report named the exact failure this catches: a well-formed candidate
that references ``[Quantity Difference vs Previous Year]`` when the model only holds the DISTINCT
``[Quantity Difference vs Previous Year (copy)_1]`` -- a confident, wrong landing. The gate resolves
every reference against a model *surface* (built from the run's ``model_manifest`` or, independently,
from the emitted TMDL), and:

  * BLOCKS a reference to a non-existent table / column / measure (with a did-you-mean suggestion,
    and an explicit ``duplicate-name trap`` note when the near-miss is a ``(copy)`` sibling),
  * WARNS -- but resolves, ``ok`` stays True -- on an inert (``stub`` / ``assisted-suggested``)
    resolution, an unqualified or ambiguous bare column, a table-qualified measure, or a bare ref
    that merely collides with a locally-defined extension column (an ``ADDCOLUMNS`` ``"@value"``),
  * NEVER raises -- any internal error becomes an advisory warning, exactly like the syntactic gate.

The wiring layer calls BOTH ``check_candidate_dax`` (syntactic) AND ``check_candidate_references``
(this) before a second-compiler candidate may land. This module is a standalone, additive primitive:
pure standard library (``re`` + ``difflib``), no Tableau/Fabric access, no imports from the rest of
the skill. The ONLY thing that raises is ``build_model_surface`` on invalid arguments (no source).
"""
from __future__ import annotations

import difflib
import re

__all__ = ["build_model_surface", "check_candidate_references"]

# Inert statuses: a measure/column carrying one of these was emitted as an inert ``= 0`` / ``BLANK()``
# placeholder (the deterministic stub or an unapproved assisted suggestion). A candidate MAY reference
# it (it exists in the model), but doing so almost always means the author reached for a placeholder
# instead of a live object -> resolve, but WARN. Matches the hyphenated status vocabulary the model
# manifest carries (assemble_model ``_COVERAGE_BUCKET``: ``stub`` / ``assisted-suggested`` are the
# ``= 0`` placeholders; ``translated`` / ``assisted-approved`` are live).
_INERT_STATUSES = frozenset({"stub", "assisted-suggested"})


# =================================================================================================
# String masking -- run BEFORE reference extraction so a bracket/quote inside a DAX string literal is
# never mis-read as a reference, and a bare ref that merely collides with a locally-defined extension
# column (an ADDCOLUMNS/SELECTCOLUMNS ``"@name"``) can be DOWNGRADED to a warning.
# =================================================================================================
def _mask_strings(text):
    """Return ``(set_of_literal_contents, masked_text)``.

    Walk char-by-char honoring DAX ``"..."`` string literals with ``""`` escaping; replace every
    in-string character with a space (length-aligned) so the masked text has the same positions but
    no literal content. The collected literal contents let a bare ref that equals a locally-defined
    extension column be downgraded rather than blocked.
    """
    out = []
    literals = set()
    cur = []
    in_str = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':  # "" -> escaped quote, stay in-string
                    cur.append('"')
                    out.append("  ")
                    i += 2
                    continue
                in_str = False
                literals.add("".join(cur))
                cur = []
                out.append(" ")
                i += 1
                continue
            cur.append(ch)
            out.append(" ")
            i += 1
            continue
        if ch == '"':
            in_str = True
            cur = []
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    if in_str:  # unterminated literal -> keep what we gathered
        literals.add("".join(cur))
    return literals, "".join(out)


# =================================================================================================
# Reference extraction -- ordered alternation over the MASKED text: 'Table' or 'Table'[Col];
# bare Table[Col]; or a bare [Name]. A bare identifier NOT followed by ``[`` (a function name, an
# unquoted table used as a SUMX first arg) is deliberately NOT extracted -- only explicit
# ``'T'`` / ``T[C]`` / ``[X]`` forms are validated.
# =================================================================================================
_REF_RE = re.compile(
    r"'(?P<qtable>(?:[^']|'')*)'\s*(?:\[(?P<qcol>[^\]]*)\])?"     # 'Table'  or  'Table'[Col]
    r"|(?P<btable>[A-Za-z_][A-Za-z0-9_]*)\[(?P<bcol>[^\]]*)\]"    # Table[Col]  (bare table)
    r"|\[(?P<bare>[^\]]*)\]"                                       # [Name]  (bare)
)


def _unq_single(s):
    """Un-double the ``''`` escapes inside a single-quoted table name."""
    return (s or "").replace("''", "'")


def _extract_refs(masked):
    refs = []
    for m in _REF_RE.finditer(masked):
        if m.group("qtable") is not None:
            tbl = _unq_single(m.group("qtable"))
            qcol = m.group("qcol")
            if qcol is not None:
                refs.append({"kind": "column", "table": tbl, "column": qcol, "raw": m.group(0)})
            else:
                refs.append({"kind": "table", "table": tbl, "raw": m.group(0)})
        elif m.group("btable") is not None:
            refs.append({"kind": "column", "table": m.group("btable"),
                         "column": m.group("bcol"), "raw": m.group(0)})
        elif m.group("bare") is not None:
            refs.append({"kind": "bare", "name": m.group("bare"), "raw": m.group(0)})
    return refs


# =================================================================================================
# Model surface -- a JSON-friendly, case-insensitive index of every table / column / measure the
# candidate may legally reference. Built from the run's model_manifest (preferred) or, independently,
# by parsing the emitted TMDL (which automates the operator's manual ``_Measures.tmdl`` hand-parse).
# =================================================================================================
def _blank_surface(source):
    return {
        "tables": {},           # lower -> original casing
        "columns": {},          # table_lower -> {col_lower -> {table, name, status}}
        "measures": {},         # name_lower -> {table, name, status}   (first wins on a name clash)
        "measure_by_table": {},  # table_lower -> {name_lower -> {name, status}}
        "column_index": {},     # col_lower -> [ {table, name, status}, ... ]   (bare-ref + ambiguity)
        "source": source,
    }


def _ensure_table(surface, name):
    if name:
        surface["tables"].setdefault(name.lower(), name)


def _add_column(surface, table, name, status):
    if not table or not name:
        return
    _ensure_table(surface, table)
    tbl_cols = surface["columns"].setdefault(table.lower(), {})
    if name.lower() in tbl_cols:   # already recorded -> dedupe (keeps column_index accurate)
        return
    rec = {"table": table, "name": name, "status": status}
    tbl_cols[name.lower()] = rec
    surface["column_index"].setdefault(name.lower(), []).append(rec)


def _add_measure(surface, table, name, status):
    if not name:
        return
    table = table or "_Measures"
    _ensure_table(surface, table)
    surface["measures"].setdefault(name.lower(), {"table": table, "name": name, "status": status})
    surface["measure_by_table"].setdefault(table.lower(), {}).setdefault(
        name.lower(), {"name": name, "status": status})


def _surface_from_manifest(manifest):
    surface = _blank_surface("manifest")
    for t in manifest.get("tables") or []:
        _ensure_table(surface, t)
    for c in manifest.get("columns") or []:
        _add_column(surface, c.get("model_table"), c.get("model_name"), c.get("status"))
    for m in manifest.get("measures") or []:
        _add_measure(surface, m.get("model_table"), m.get("model_name"), m.get("status"))
    return surface


# ---- TMDL parse path (independent surface, no manifest needed) ----------------------------------
_NAME = r"(?:'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_\-]*)"
_TABLE_DECL_RE = re.compile(r"^table\s+(" + _NAME + r")\s*$")
_MEASURE_DECL_RE = re.compile(r"^[ \t]+measure\s+(" + _NAME + r")\s*=\s*(.*)$")
_COLUMN_DECL_RE = re.compile(r"^[ \t]+column\s+(" + _NAME + r")\s*(?:=\s*(.*))?$")


def _unq(name):
    """Strip a single-quoted TMDL object name and un-double its ``''`` escapes."""
    name = (name or "").strip()
    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        name = name[1:-1].replace("''", "'")
    return name


def _tmdl_status_from_rhs(rhs):
    """An assignment RHS of ``0`` / ``BLANK()`` marks an inert stub; anything else is live."""
    if rhs is None:
        return None
    r = rhs.strip().casefold()
    if r in ("0", "blank()"):
        return "stub"
    return None


def _surface_from_tmdl(tmdl_parts):
    surface = _blank_surface("tmdl")
    for text in (tmdl_parts or {}).values():
        if not text:
            continue
        lines = text.splitlines()
        cur = None
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            mt = _TABLE_DECL_RE.match(line)
            if mt:
                cur = _unq(mt.group(1))
                _ensure_table(surface, cur)
                i += 1
                continue
            mm = _MEASURE_DECL_RE.match(line)
            if mm and cur is not None:
                rhs = mm.group(2)
                if rhs is not None and rhs.strip() == "" and i + 1 < n:
                    rhs = lines[i + 1]   # block form: value is on the next indented line
                _add_measure(surface, cur, _unq(mm.group(1)), _tmdl_status_from_rhs(rhs))
                i += 1
                continue
            mc = _COLUMN_DECL_RE.match(line)
            if mc and cur is not None:
                rhs = mc.group(2)
                if rhs is not None and rhs.strip() == "" and i + 1 < n:
                    rhs = lines[i + 1]
                _add_column(surface, cur, _unq(mc.group(1)), _tmdl_status_from_rhs(rhs))
                i += 1
                continue
            i += 1
    return surface


def build_model_surface(*, model_manifest=None, tmdl_parts=None):
    """Build the reference surface from a ``model_manifest`` dict OR a ``{path: tmdl_text}`` map.

    ``model_manifest`` (the ``report["model_manifest"]`` shape from ``assemble_model``) is preferred;
    if both are given, the manifest wins. Raises ``ValueError`` if neither is supplied -- the only
    invalid-argument case. Every lookup on the returned surface is case-insensitive.
    """
    if model_manifest is not None:
        return _surface_from_manifest(model_manifest)
    if tmdl_parts is not None:
        return _surface_from_tmdl(tmdl_parts)
    raise ValueError("build_model_surface requires either model_manifest= or tmdl_parts=")


# =================================================================================================
# Duplicate-name normalization + did-you-mean suggestions. ``_dupe_normalize`` collapses the exact
# ``(copy)_NNNN`` trap so a candidate that names the base object surfaces the DISTINCT ``(copy)``
# sibling that actually exists.
# =================================================================================================
def _dupe_normalize(s):
    x = (s or "").casefold().replace("(copy)", " ")
    x = re.sub(r"[_\s]+\d+\s*$", "", x)     # drop a trailing ``_NNN`` / `` NNN`` dedupe suffix
    x = re.sub(r"[\s_]+", " ", x).strip()   # collapse whitespace/underscore runs
    return x


def _suggest(name, candidates):
    """Up to 3 did-you-mean candidates: dedupe-normalized equals first, then fuzzy close matches."""
    if not name:
        return []
    target = _dupe_normalize(name)
    exact = [c for c in candidates if _dupe_normalize(c) == target]
    close = difflib.get_close_matches(name, list(candidates), n=3, cutoff=0.7)
    out = []
    for c in exact + close:
        if c not in out:
            out.append(c)
    return out[:3]


def _did_you_mean(suggestions):
    if not suggestions:
        return ""
    return "; did you mean %s?" % " / ".join("'%s'" % s for s in suggestions)


def _dupe_trap_note(name, suggestions):
    """When the top suggestion is a ``(copy)`` dedupe-sibling of the missing name, say so explicitly."""
    if suggestions and _dupe_normalize(suggestions[0]) == _dupe_normalize(name):
        return (" -- note '%s' is a DISTINCT model object (a (copy) duplicate-name trap), not the "
                "object you named" % suggestions[0])
    return ""


def _inert_warn(kind, table, name, status):
    if status in _INERT_STATUSES:
        return ("%s '%s'[%s] resolves to an inert placeholder (status '%s'; returns 0/BLANK) -- "
                "verify this is the object you intended" % (kind, table, name, status))
    return None


def _live_near_duplicate(surface, name):
    """A non-inert measure sharing ``name``'s dedupe-normalized form (the live twin of a stub copy)."""
    target = _dupe_normalize(name)
    for rec in surface["measures"].values():
        if (rec.get("status") not in _INERT_STATUSES
                and rec["name"].lower() != name.lower()
                and _dupe_normalize(rec["name"]) == target):
            return rec["name"]
    return None


def _all_measure_names(surface):
    return [rec["name"] for rec in surface["measures"].values()]


def _all_column_names(surface):
    names = []
    for recs in surface["column_index"].values():
        for r in recs:
            names.append(r["name"])
    return names


# =================================================================================================
# Per-reference validation. Message keywords ("unknown table", "not found", "no such measure or
# column", "duplicate-name trap", "inert", "unqualified", "ambiguous", "table-qualified",
# "near-duplicate", "string literal") are STABLE substrings the wiring + tests assert on.
# =================================================================================================
def _validate_ref(ref, surface, literals, issues, warnings, references):
    tables = surface["tables"]
    columns = surface["columns"]
    measures = surface["measures"]
    measure_by_table = surface["measure_by_table"]
    column_index = surface["column_index"]
    kind = ref["kind"]

    if kind == "table":
        tbl = ref["table"]
        if tbl.lower() in tables:
            references.append({"kind": "table", "table": tables[tbl.lower()]})
        else:
            issues.append("unknown table '%s'%s"
                          % (tbl, _did_you_mean(_suggest(tbl, list(tables.values())))))
        return

    if kind == "column":
        tbl = ref["table"]
        col = ref["column"]
        if tbl.lower() not in tables:
            issues.append("unknown table '%s' (for column [%s])%s"
                          % (tbl, col, _did_you_mean(_suggest(tbl, list(tables.values())))))
            return
        otbl = tables[tbl.lower()]
        tcols = columns.get(tbl.lower(), {})
        if col.lower() in tcols:
            rec = tcols[col.lower()]
            references.append({"kind": "column", "table": otbl, "name": rec["name"]})
            w = _inert_warn("column", otbl, rec["name"], rec.get("status"))
            if w:
                warnings.append(w)
            return
        mbt = measure_by_table.get(tbl.lower(), {})
        if col.lower() in mbt:
            mrec = mbt[col.lower()]
            references.append({"kind": "measure", "table": otbl, "name": mrec["name"]})
            warnings.append("table-qualified MEASURE reference '%s'[%s] -- a measure is normally "
                            "referenced bare as [%s]" % (otbl, mrec["name"], mrec["name"]))
            w = _inert_warn("measure", otbl, mrec["name"], mrec.get("status"))
            if w:
                warnings.append(w)
            return
        note = ""
        if col.lower() in column_index:
            note = (" (a column [%s] exists on %s)"
                    % (col, ", ".join("'%s'" % r["table"] for r in column_index[col.lower()])))
        issues.append("column [%s] not found in table '%s'%s%s"
                      % (col, otbl,
                         _did_you_mean(_suggest(col, [c["name"] for c in tcols.values()])), note))
        return

    # bare [Name]
    name = ref["name"]
    low = name.lower()
    if low in measures:
        mrec = measures[low]
        references.append({"kind": "measure", "table": mrec["table"], "name": mrec["name"]})
        w = _inert_warn("measure", mrec["table"], mrec["name"], mrec.get("status"))
        if w:
            warnings.append(w)
            live = _live_near_duplicate(surface, mrec["name"])
            if live:
                warnings.append("a near-duplicate LIVE measure '%s' exists -- did you mean that "
                                "instead of the inert (copy) duplicate-name trap?" % live)
        return
    if low in column_index:
        recs = column_index[low]
        if len(recs) == 1:
            r = recs[0]
            references.append({"kind": "column", "table": r["table"], "name": r["name"]})
            warnings.append("unqualified reference [%s] resolves to column '%s'[%s] -- qualify it "
                            "as '%s'[%s]" % (name, r["table"], r["name"], r["table"], r["name"]))
            w = _inert_warn("column", r["table"], r["name"], r.get("status"))
            if w:
                warnings.append(w)
        else:
            references.append({"kind": "column", "table": None, "name": name})
            warnings.append("ambiguous unqualified reference [%s] -- a column of that name exists on "
                            "%s; qualify it"
                            % (name, ", ".join("'%s'" % r["table"] for r in recs)))
        return
    if name in literals:
        warnings.append("[%s] matches a string literal in the candidate -- treating it as a "
                        "locally-defined column (e.g. an ADDCOLUMNS/SELECTCOLUMNS extension column); "
                        "it cannot be verified against the model surface" % name)
        return
    # genuinely unknown -> BLOCK. This is the (copy)_NNNN duplicate-name trap the AAR named.
    sugg = _suggest(name, _all_measure_names(surface) + _all_column_names(surface))
    issues.append("unknown reference [%s] -- no such measure or column%s%s"
                  % (name, _did_you_mean(sugg), _dupe_trap_note(name, sugg)))


def check_candidate_references(candidate_dax, surface, *, request=None):
    """Validate every model reference in ``candidate_dax`` against ``surface``.

    Returns ``{"ok": bool, "issues": [...], "warnings": [...], "references": [...]}``. ``ok`` is
    False iff any reference names a non-existent object (a BLOCK). Inert / unqualified / ambiguous /
    table-qualified / extension-column resolutions are WARN (``ok`` stays True). NEVER raises: any
    internal error becomes an advisory warning. ``request`` is accepted for forward-compatibility
    (the second-compiler translation request) and is not required.
    """
    issues = []
    warnings = []
    references = []
    try:
        text = "" if candidate_dax is None else str(candidate_dax)
        if not text.strip():
            return {"ok": True, "issues": [], "references": [],
                    "warnings": ["candidate DAX is empty -- no references to check"]}
        literals, masked = _mask_strings(text)
        for ref in _extract_refs(masked):
            _validate_ref(ref, surface, literals, issues, warnings, references)
    except Exception as exc:  # never raises -- advisory only, mirroring check_candidate_dax
        warnings.append("reference gate could not fully analyze the candidate (%s: %s); verify "
                        "references by hand" % (type(exc).__name__, exc))
    return {"ok": not issues, "issues": issues, "warnings": warnings, "references": references}
