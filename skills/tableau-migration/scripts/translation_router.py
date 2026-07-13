"""Deterministic Tier-0 -> Tier-1 support layer (ROUTER + candidate GATE).

The deterministic compiler (Tier 0) emits an honest free-text ``fallback_reason`` whenever it
refuses to translate a Tableau calc faithfully (the "faithful-or-stub" contract). This module hosts
the two pure, dependency-free helpers the second compiler is built on:

  * :func:`classify_fallback` -- the **router**. Turns a fallback reason (plus light structural
    signals already in the handoff request) into a STABLE *category* from the Tier-1 charter
    taxonomy, with concrete *guidance* on what intent to supply and which DAX shape to aim for.
  * :func:`check_candidate_dax` -- the **syntactic gate**. The first half of the validation gate the
    playbook promises: a deterministic, offline check an agent runs on its candidate DAX *before*
    proposing it for approval (balanced delimiters, not an inert stub, no leftover Tableau idioms).
    The empirical half -- the reconciliation oracle -- is environment-specific and lives in
    ``resources/validation-reconciliation.md`` / ``resources/second-compiler.md``.

Design contract (mirrors ``translation_handoff_artifact``):
  * **Pure and dependency-free.** Reads only strings already in the handoff request / a candidate
    string; emits NO DAX and NO model objects -- it merely *labels* and *checks*, so the second
    compiler (and any telemetry) acts on a fixed vocabulary instead of re-parsing prose.
  * **Never raises.** Unrecognized reasons fall through to ``UNSUPPORTED_OTHER``; a malformed
    candidate yields ``ok=False`` with issues, never an exception.
  * **Additive.** This is advisory metadata layered onto an existing request; it never changes
    Tier-0 behavior or the live/inert status of any calc.

The taxonomy is deliberately a small set of *distinct agent playbooks* -- each category maps to a
different action the second compiler should take, not merely a different error string.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- category vocabulary
# A Tableau parameter drives the calc -> it is a MODEL OBJECT in Power BI, not a single expression
# (measure swap -> calculation group; dimension swap -> field parameters; what-if -> numeric-range
# parameter). The agent models the object, then rebinds the calc to it.
MODEL_OBJECT_PARAMETER = "model_object_parameter"

# A table calc whose partition/order/scope (Tableau "Compute Using" addressing) is not recoverable
# from the bare ``.tds`` -- the value is well-defined only against a worksheet's layout. The agent
# supplies the addressing (from workbook context when a ``.twb`` is available) then emits the
# windowed DAX (running total / OFFSET / RANKX / partition aggregate).
MISSING_ADDRESSING_INTENT = "missing_addressing_intent"

# An LOD whose result depends on the visual's dimensionality (INCLUDE/EXCLUDE, a bare LOD needing
# an outer re-aggregation, a nested LOD that does not fix a superset). The agent decides the
# intended grain / outer aggregation (CALCULATE adding a group, or REMOVEFILTERS for EXCLUDE) and
# confirms it against the oracle.
MISSING_OUTER_AGGREGATION = "missing_outer_aggregation"

# A construct with NO faithful native DAX form at all (regex family; arbitrary-format DATEPARSE;
# general SPLIT; FINDNTH; case-sensitive ordered text comparison; an exotic date part). Any agent
# output here is an APPROXIMATION -- it must be flagged as such and oracle-verified, else the stub
# stays.
DAX_LANGUAGE_GAP = "dax_language_gap"

# A typing / parse / shape mismatch the deterministic engine refused on (inconsistent IF/CASE
# branch types, incomparable operands, the 4-arg IIF unknown branch, an aggregate inside a
# row-level column calc, ...). Frequently resolvable: the agent supplies an explicit cast or
# restructures (e.g. re-routes an aggregating "column" calc to a measure) and re-translates.
TYPE_OR_SHAPE_MISMATCH = "type_or_shape_mismatch"

# A referenced field / dimension / calc that could not be bound (unresolved or ambiguous name,
# terms spanning multiple tables, an unsupported field type). The agent supplies the correct table
# binding / relationship, or translates the referenced calc first.
UNRESOLVED_REFERENCE = "unresolved_reference"

# Anything not matched above. A faithful DAX form may still exist (e.g. CORR/COVAR via a VAR/RETURN
# closed form); the agent assesses, authors a candidate, and validates before proposing it.
UNSUPPORTED_OTHER = "unsupported_other"

CATEGORIES = (
    MODEL_OBJECT_PARAMETER,
    MISSING_ADDRESSING_INTENT,
    MISSING_OUTER_AGGREGATION,
    DAX_LANGUAGE_GAP,
    TYPE_OR_SHAPE_MISMATCH,
    UNRESOLVED_REFERENCE,
    UNSUPPORTED_OTHER,
)

_GUIDANCE = {
    MODEL_OBJECT_PARAMETER: (
        "This calc is driven by a Tableau parameter, which is a Power BI MODEL OBJECT rather than a "
        "single expression. Identify the usage: a dimension swap maps to field parameters; a what-if "
        "value maps to a numeric-range parameter (GENERATESERIES + a [<Param> Value] SELECTEDVALUE "
        "measure); a measure swap also maps to field parameters today (a calculation group is the "
        "richer alternative). Do NOT hand-author the model object: reuse the tested deterministic "
        "emitters in parameters.py -- detect_field_swap() classifies a swap, emit_field_parameters() "
        "builds a field-parameter table per swap, and emit_value_parameters() builds the what-if "
        "table + value measure and returns a param_resolver that inlines the selection. Parse the "
        "parameter definitions from the .twb/.tds with parse_parameters() first. Rebind the calc to "
        "the selected value; oracle-verify at a fixed selection when data is landed."
    ),
    MISSING_ADDRESSING_INTENT: (
        "This is a table calculation whose partition/order/scope (Tableau 'Compute Using') is not "
        "carried by the .tds. Recover the addressing from worksheet context (the .twb 'ordering-type' "
        "+ <order>/<sort> and the rows/cols shelf layout) when available, then emit the windowed DAX: "
        "cumulative -> running total or time-intelligence, prior value/offset -> OFFSET, rank -> RANKX "
        "over the partition, partition size/row number -> COUNTROWS/RANKX over ALLSELECTED. State the "
        "inferred partition + order for review; if addressing is unrecoverable, keep the stub."
    ),
    MISSING_OUTER_AGGREGATION: (
        "This LOD's result depends on the visual's dimensionality, so the deterministic tier will not "
        "guess. Decide the intended grain and outer aggregation: INCLUDE adds a group to the current "
        "context (CALCULATE over a SUMMARIZE/added column), EXCLUDE removes dimensions "
        "(CALCULATE(..., REMOVEFILTERS(dims))), and a bare LOD usually needs an explicit outer "
        "aggregate. Choose the leanest faithful shape and oracle-verify it before proposing."
    ),
    DAX_LANGUAGE_GAP: (
        "No faithful native DAX form exists for this construct: regex (no DAX engine); "
        "arbitrary-format DATEPARSE/ISDATE; general SPLIT / nth-occurrence FINDNTH; TRIM/LTRIM/RTRIM "
        "(DAX TRIM collapses INTERNAL whitespace and has no one-sided form); start-of-week- or "
        "ISO-dependent WEEK/ISOQUARTER; MAKETIME/MAKEDATETIME (DAX time epoch differs); HEXBINX/HEXBINY "
        "grid snap; culture-sensitive STR number formatting; RANK_UNIQUE (tie-break follows Tableau's "
        "internal addressing/row order); case-sensitive ordered text comparison. Any candidate is an "
        "APPROXIMATION -- author it only if the real usage is narrow enough to be safe (e.g. a fixed "
        "delimiter via PATH/SUBSTITUTE, a known date format), mark it approximate with a clear caveat, "
        "and oracle-verify. If it cannot be made faithful, keep the honest stub."
    ),
    TYPE_OR_SHAPE_MISMATCH: (
        "The deterministic engine refused on a typing/parse/shape mismatch (inconsistent IF/CASE "
        "branch types, incomparable operands, the 4-arg IIF unknown branch, an aggregate used "
        "inside a row-level column calc, or a bare row-level field used where a measure -- which "
        "needs an aggregation -- is required). This is often resolvable: supply an explicit cast, "
        "align the branch types, re-route an aggregating calc to a measure instead of a calculated "
        "column, or -- for a row-level expression with no aggregation (e.g. "
        "IF [Region]=\"east\" THEN [Sales] END) -- either wrap it in the intended aggregation (SUM/"
        "MIN/...) to make a measure, exactly as the SUM(...) West-Sales form already translates, or "
        "emit it as a calculated column (the row-level translator handles it directly). Then "
        "re-translate and validate."
    ),
    UNRESOLVED_REFERENCE: (
        "A referenced field, dimension, or calc could not be bound (unresolved/ambiguous name, terms "
        "spanning multiple tables, or an unsupported field type). Supply the correct table binding or "
        "relationship, or translate the referenced calc first, then re-run Tier 0 -- this may need no "
        "second-compiler DAX at all once the reference resolves."
    ),
    UNSUPPORTED_OTHER: (
        "Not matched to a specific category. A faithful DAX form may still exist (for example "
        "CORR/COVAR/COVARP via a VAR/RETURN closed form, or an aggregate the deterministic tier has "
        "not yet wired). Assess the formula, author a candidate at the right grain, and validate "
        "(oracle when data is landed) before proposing it; otherwise keep the stub."
    ),
}

# Tableau table-calc functions: in measure/column mode the deterministic engine reports these as
# "unsupported function <NAME>" because they are addressing expressions, not measure-valid calls.
# RANK_UNIQUE is deliberately NOT here: its tie-break follows Tableau's internal addressing/row
# order, which has no faithful DAX equivalent, so it is a DAX-language gap (below), not a
# recoverable-addressing case.
_TABLE_CALC_FUNCS = frozenset({
    "WINDOW_SUM", "WINDOW_AVG", "WINDOW_MIN", "WINDOW_MAX", "WINDOW_COUNT", "WINDOW_MEDIAN",
    "WINDOW_STDEV", "WINDOW_STDEVP", "WINDOW_VAR", "WINDOW_VARP", "WINDOW_PERCENTILE",
    "WINDOW_CORR", "WINDOW_COVAR", "WINDOW_COVARP",
    "RUNNING_SUM", "RUNNING_AVG", "RUNNING_MIN", "RUNNING_MAX", "RUNNING_COUNT",
    "RANK", "RANK_DENSE", "RANK_MODIFIED", "RANK_PERCENTILE",
    "INDEX", "SIZE", "FIRST", "LAST", "LOOKUP", "PREVIOUS_VALUE", "TOTAL",
})

# Functions with no faithful native DAX target. The engine fails these closed on purpose; the
# router labels them so the report says WHY rather than implying a closed form might exist.
_DAX_GAP_FUNCS = frozenset({
    # DAX has no regex engine.
    "REGEXP_MATCH", "REGEXP_EXTRACT", "REGEXP_EXTRACT_NTH", "REGEXP_REPLACE",
    # Format/locale-dependent parsing, or an unbounded split / nth-occurrence search.
    "DATEPARSE", "ISDATE", "SPLIT", "FINDNTH",
    # Trims that diverge from DAX: DAX TRIM also collapses INTERNAL whitespace runs, and there is no
    # native leading-only / trailing-only trim.
    "TRIM", "LTRIM", "RTRIM",
    # Start-of-week- / ISO-quarter-dependent date parts (DATEPART('week') is excluded for the same
    # reason), and constructors whose DAX time epoch differs from Tableau's.
    "WEEK", "ISOQUARTER", "MAKETIME", "MAKEDATETIME",
    # Exotic hex-bin grid snap; culture-sensitive number->string formatting; a rank whose tie-break
    # follows Tableau's internal addressing/row order (no deterministic DAX equivalent).
    "HEXBINX", "HEXBINY", "STR", "RANK_UNIQUE",
})

_UNSUPPORTED_FN_PREFIXES = ("unsupported function ", "unsupported table calculation ")


def _unsupported_function_name(reason_lower, reason):
    """If ``reason`` is ``unsupported function <NAME>`` / ``unsupported table calculation <NAME>``
    return the bare uppercased NAME, else None."""
    for prefix in _UNSUPPORTED_FN_PREFIXES:
        if reason_lower.startswith(prefix):
            name = reason[len(prefix):].strip()
            # the engine emits a single bare token here; guard against stray trailing text
            name = name.split()[0] if name else ""
            return name.upper().strip("[]") or None
    return None


def classify_fallback(reason, *, role=None, fields=None, has_suggestion=False):
    """Map a Tier-0 ``fallback_reason`` to a stable Tier-1 ``{"category", "guidance"}``.

    ``fields`` is the request's resolved field list (each ``{"caption", "kind", ...}``); a
    ``parameter`` kind routes to :data:`MODEL_OBJECT_PARAMETER` even when the reason text does not
    mention it. ``role``/``has_suggestion`` are accepted for forward compatibility and to keep the
    signature stable; they do not change the result today. Never raises.
    """
    reason = "" if reason is None else str(reason)
    rl = reason.lower()
    fields = fields or []

    # 1. Parameters are a model-object decision -- detect structurally (a [Parameters].[x] ref) OR
    #    from the reason text, and take precedence (the parameter shapes the whole translation).
    if any((f or {}).get("kind") == "parameter" for f in fields) or "parameter reference" in rl:
        return {"category": MODEL_OBJECT_PARAMETER, "guidance": _GUIDANCE[MODEL_OBJECT_PARAMETER]}

    fn = _unsupported_function_name(rl, reason)

    # 2. LOD grain / outer-aggregation dependence (INCLUDE/EXCLUDE, re-aggregation, nested superset,
    #    a bare LOD inside a row-level column calc).
    if ("include/exclude" in rl
            or "re-aggregate" in rl
            or "re-aggregating" in rl
            or "nested fixed lod" in rl
            or "nested inside another lod" in rl
            or "enclosing aggregation" in rl
            or "at least one [dimension]" in rl
            or "table-scoped lod" in rl
            or "over an lod" in rl
            or "lod expression not valid" in rl):
        return {"category": MISSING_OUTER_AGGREGATION, "guidance": _GUIDANCE[MISSING_OUTER_AGGREGATION]}

    # 3. Table-calc addressing intent (a window/running/rank/offset function, or the seam asking for
    #    an explicit order-by / partition it cannot recover).
    if (fn in _TABLE_CALC_FUNCS
            or "order-by" in rl
            or "partition field" in rl
            or "not a table calculation" in rl):
        return {"category": MISSING_ADDRESSING_INTENT, "guidance": _GUIDANCE[MISSING_ADDRESSING_INTENT]}

    # 4. Genuine DAX-language gaps (no faithful native form).
    if (fn in _DAX_GAP_FUNCS
            or "no faithful dax form" in rl
            or rl.startswith("unsupported datepart ")
            or rl.startswith("unsupported datediff ")
            or rl.startswith("unsupported dateadd ")
            or rl.startswith("unsupported datetrunc ")):
        return {"category": DAX_LANGUAGE_GAP, "guidance": _GUIDANCE[DAX_LANGUAGE_GAP]}

    # 5. Unresolved / cross-table references (do this before the generic type/shape bucket so a
    #    "requires a numeric field" binding problem is reported as a reference issue).
    if ("unresolved" in rl
            or "ambiguous" in rl
            or "cross-table" in rl
            or "unsupported field type" in rl
            or "requires a numeric field" in rl
            or "requires a numeric/date field" in rl):
        return {"category": UNRESOLVED_REFERENCE, "guidance": _GUIDANCE[UNRESOLVED_REFERENCE]}

    # 6. Typing / parse / shape mismatches the engine refused on but an agent can often repair.
    if ("inconsistent types" in rl
            or "incomparable types" in rl
            or "case-sensitive" in rl
            or "not valid in a row-level column calc" in rl
            or "not valid in a measure" in rl
            or "4-arg iif" in rl
            or "booleans support only" in rl
            or "in requires a non-boolean" in rl
            or "in list element type" in rl
            or rl.startswith("expected ")
            or "unterminated" in rl
            or "string literal with escape" in rl
            or "unsupported character" in rl):
        return {"category": TYPE_OR_SHAPE_MISMATCH, "guidance": _GUIDANCE[TYPE_OR_SHAPE_MISMATCH]}

    # 7. Fallback bucket.
    return {"category": UNSUPPORTED_OTHER, "guidance": _GUIDANCE[UNSUPPORTED_OTHER]}


# ---------------------------------------------------------------------------
# Candidate-DAX gate (the syntactic half of the playbook's validation gate)
# ---------------------------------------------------------------------------

# DAX has no curly-brace syntax; an LOD brace surviving into a candidate means the agent pasted a
# Tableau idiom verbatim instead of translating it.
_LEFTOVER_TABLEAU_TOKENS = (
    "{fixed",
    "{include",
    "{exclude",
    "[parameters]",
)

# Candidates equal to one of the engine's inert stubs are not translations -- they are the stub.
_INERT_STUBS = ("0", "blank()")

# Closers paired to their openers for a single balance + nesting-order scan.
_PAIRS = {")": "(", "]": "["}


def _delimiter_issues(text):
    """Return a list of balance/quote problems in ``text`` (empty == clean).

    A self-contained scan (so the module stays dependency-free): tracks ``"`` string state and a
    stack of ``(`` / ``[`` openers, reporting unbalanced or mis-nested delimiters and an unterminated
    string. Brackets are checked here in addition to parens/quotes, which is stricter than the
    Tier-0 ``validate_dax`` syntactic check.
    """
    issues = []
    stack = []
    in_string = False
    for ch in text:
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("(", "["):
            stack.append(ch)
        elif ch in _PAIRS:
            if not stack:
                issues.append("unbalanced '%s' (no matching '%s')" % (ch, _PAIRS[ch]))
            elif stack[-1] != _PAIRS[ch]:
                issues.append("mismatched '%s' closing a '%s'" % (ch, stack[-1]))
                stack.pop()
            else:
                stack.pop()
    if in_string:
        issues.append("unterminated string literal")
    if stack:
        issues.append("unclosed '%s'" % "".join(stack))
    return issues


def check_candidate_dax(dax, *, request=None):
    """Syntactically vet an agent's candidate DAX *before* it is proposed for approval.

    This is the deterministic, offline first gate the second-compiler playbook requires the agent to
    run on every candidate. It is **not** the empirical check -- the reconciliation oracle (compare
    the candidate's value to the Tableau ground truth) is environment-specific and runs at landing
    time. Passing here means "this string is well-formed DAX that is not obviously the inert stub or
    an un-translated Tableau idiom", never "this is numerically faithful".

    Returns a verdict dict (never raises)::

        {
          "ok": bool,             # True iff there are no blocking issues
          "issues": [str, ...],   # blocking problems (empty when ok)
          "warnings": [str, ...], # advisory, non-blocking notes
        }

    ``request`` is the optional handoff request the candidate answers; when its ``category`` is
    ``dax_language_gap`` a warning reminds the caller that an approximation MUST be oracle-verified
    before landing (per the playbook).
    """
    issues = []
    warnings = []

    text = "" if dax is None else str(dax)
    stripped = text.strip()

    if not stripped:
        issues.append("candidate DAX is empty")
        return {"ok": False, "issues": issues, "warnings": warnings}

    if stripped.lower() in _INERT_STUBS:
        issues.append(
            "candidate is the inert stub %r -- that is not a translation" % stripped)

    issues.extend(_delimiter_issues(text))

    low = text.lower()
    for tok in _LEFTOVER_TABLEAU_TOKENS:
        if tok in low:
            issues.append(
                "leftover Tableau idiom %r -- DAX has no equivalent literal; translate it" % tok)

    # Advisory: a category hint, if the caller threaded the request through, lets us remind about
    # the mandatory empirical step for approximations without blocking the syntactic verdict.
    category = None
    if isinstance(request, dict):
        category = request.get("category")
    if category == DAX_LANGUAGE_GAP:
        warnings.append(
            "category is dax_language_gap: this is an approximation -- it MUST be oracle-verified "
            "against the Tableau ground-truth value before landing.")

    return {"ok": not issues, "issues": issues, "warnings": warnings}
