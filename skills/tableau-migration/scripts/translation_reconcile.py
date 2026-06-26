"""Deterministic reconciliation core for the Tier-1 second compiler (the *oracle* half).

The syntactic gate in :mod:`translation_router` proves a candidate DAX string is *well-formed*; it
cannot prove it is *numerically faithful*. That proof is empirical: evaluate the candidate against
the live Fabric model and compare the result to the real Tableau value, **at the same grain**,
within tolerance. Per the playbook this is the non-circular proof of faithfulness and the mandatory
step before a ``dax_language_gap`` approximation may land (see
``resources/validation-reconciliation.md`` and ``resources/second-compiler.md``).

This module is the **offline, deterministic core** of that loop. It deliberately does NOT talk to
any backend: the actual DAX execution (Power BI ``executeQueries`` via ``semantic-model-consumption``)
and the Tableau ground-truth fetch (VizQL Data Service) are injected as plain callables, so the
comparison logic is pure and fully unit-testable without a network. The proven runtime backend is a
``fabric_oracle(dax_query) -> result`` hook whose result this module knows how to read.

Design contract (mirrors :mod:`translation_router`):
  * **Pure and side-effect-free.** Builds a query string, calls injected hooks, compares numbers.
    Emits NO DAX measures and NO model objects; it only *labels* a translation ``verified`` /
    ``mismatch`` / ``not-evaluated``.
  * **Never raises.** A missing oracle, an oracle that throws, an unreadable result, or a
    candidate that fails the syntactic gate all yield a ``not-evaluated`` record with a reason --
    never an exception.
  * **Additive.** Reconciliation is advisory verification metadata; it never changes Tier-0 output
    or the live/inert status of any calc. Nothing here runs automatically -- the orchestrator/agent
    invokes it explicitly with an injected oracle during the Final phase.
"""
from __future__ import annotations

try:
    from .translation_router import check_candidate_dax, DAX_LANGUAGE_GAP
except ImportError:  # flat-module import (scripts dir on sys.path)
    from translation_router import check_candidate_dax, DAX_LANGUAGE_GAP

# --------------------------------------------------------------------------- verification states
VERIFIED = "verified"          # both sides evaluated and the numbers match within tolerance
MISMATCH = "mismatch"          # both sides evaluated but differ beyond tolerance
NOT_EVALUATED = "not-evaluated"  # one/both values unavailable (no oracle, error, failed gate, ...)

STATES = (VERIFIED, MISMATCH, NOT_EVALUATED)

# Aggregations that must match EXACTLY (a row count can't be "close"). Anything else is a float
# comparison with a relative epsilon, because sums/ratios/averages legitimately differ in the last
# bits across engines.
_EXACT_KINDS = frozenset({"count", "countd", "distinctcount", "integer", "int", "exact"})

# Sentinel so an explicit ``tableau_value=None`` (a genuine Tableau BLANK to compare against) is
# distinguishable from "no ground truth was provided".
_UNSET = object()

# Default tolerances. Money sums over Superstore-scale data reconcile comfortably inside these; a
# difference larger than this is a real discrepancy a human should see, not float noise.
DEFAULT_REL_TOL = 1e-6
DEFAULT_ABS_TOL = 1e-9


# --------------------------------------------------------------------------- value helpers
def _is_blank(x):
    """A value that reads as an empty Tableau aggregation / a DAX BLANK (``None`` or ``""``)."""
    return x is None or (isinstance(x, str) and x.strip() == "")


def _to_number(x):
    """Return ``(float, True)`` if ``x`` is numeric (or a numeric string), else ``(None, False)``.

    ``bool`` is accepted as 1.0 / 0.0 so a Tableau boolean reconciles against DAX TRUE()/FALSE().
    """
    if isinstance(x, bool):
        return (1.0 if x else 0.0, True)
    if isinstance(x, (int, float)):
        return (float(x), True)
    if isinstance(x, str):
        s = x.strip().replace(",", "")
        try:
            return (float(s), True)
        except ValueError:
            return (None, False)
    return (None, False)


def _norm_text(x):
    """Canonical text form for an exact non-numeric compare (booleans normalize to ``true``/``false``)."""
    if isinstance(x, bool):
        return "true" if x else "false"
    return "" if x is None else str(x).strip()


def compare_scalars(tableau_value, fabric_value, *, kind=None,
                    rel_tol=DEFAULT_REL_TOL, abs_tol=DEFAULT_ABS_TOL, blank_equals_zero=True):
    """Compare a Tableau value to a Fabric value and return ``{"state", "detail"[, "delta"]}``.

    Pure and total (never raises). The policy follows ``validation-reconciliation.md``:

      * **both blank** -> ``verified`` (an empty Tableau aggregation equals a DAX BLANK).
      * **one blank** -> ``verified`` only when ``blank_equals_zero`` and the present side is 0
        (the BLANK<->0 decision, recorded in ``detail`` so it is auditable); otherwise ``mismatch``.
      * **exact kinds** (counts / integers) -> equality required.
      * **floats** -> match iff ``|a-b| <= rel_tol*max(|a|,|b|) + abs_tol``.
      * **non-numeric** -> case-sensitive exact text compare.

    ``state`` is :data:`VERIFIED` or :data:`MISMATCH` (this function never returns
    ``not-evaluated`` -- that is for missing values, decided by :func:`reconcile`).
    """
    t_blank = _is_blank(tableau_value)
    f_blank = _is_blank(fabric_value)

    if t_blank and f_blank:
        return {"state": VERIFIED, "detail": "both blank"}
    if t_blank or f_blank:
        present = fabric_value if t_blank else tableau_value
        pn, ok = _to_number(present)
        if blank_equals_zero and ok and pn == 0.0:
            return {"state": VERIFIED,
                    "detail": "one side blank, the other 0 (blank_equals_zero)"}
        return {"state": MISMATCH,
                "detail": "one side blank (tableau=%r, fabric=%r)" % (tableau_value, fabric_value)}

    tn, tok = _to_number(tableau_value)
    fn, fok = _to_number(fabric_value)

    if tok and fok:
        delta = abs(tn - fn)
        if kind in _EXACT_KINDS:
            if tn == fn:
                return {"state": VERIFIED, "detail": "exact match (%s)" % (kind or "exact")}
            return {"state": MISMATCH,
                    "detail": "exact compare differs (tableau=%s, fabric=%s)" % (tn, fn),
                    "delta": delta}
        allowed = rel_tol * max(abs(tn), abs(fn)) + abs_tol
        if delta <= allowed:
            return {"state": VERIFIED,
                    "detail": "within tolerance (delta=%.3g, allowed=%.3g)" % (delta, allowed)}
        return {"state": MISMATCH,
                "detail": "beyond tolerance (delta=%.3g, allowed=%.3g)" % (delta, allowed),
                "delta": delta}

    if tok != fok:
        return {"state": MISMATCH,
                "detail": "type mismatch (tableau=%r, fabric=%r)" % (tableau_value, fabric_value)}

    # Both non-numeric: exact text compare.
    if _norm_text(tableau_value) == _norm_text(fabric_value):
        return {"state": VERIFIED, "detail": "text match"}
    return {"state": MISMATCH,
            "detail": "text differs (tableau=%r, fabric=%r)" % (tableau_value, fabric_value)}


# --------------------------------------------------------------------------- query construction
def evaluate_query(expr, *, value_name="value", filters=None):
    """Build the single-row ``EVALUATE ROW(...)`` probe for ``expr`` (a measure ref or full DAX).

    ``filters`` is an optional list of DAX boolean predicates that fix the grain (e.g.
    ``"'Orders'[Region] = \\"West\\""``); when present the expression is wrapped in ``CALCULATE`` so
    both sides are compared under the SAME filter context (a Tableau grand total vs a differently
    filtered DAX value is a false mismatch, not a bug).
    """
    inner = str(expr).strip()
    if filters:
        inner = "CALCULATE(%s, %s)" % (inner, ", ".join(filters))
    safe_name = str(value_name).replace('"', "'")
    return 'EVALUATE\nROW("%s", %s)' % (safe_name, inner)


def extract_scalar(result, *, prefer="value"):
    """Read a single scalar (and an optional error string) out of a Fabric-oracle result.

    Returns ``(value, error)``: exactly one is meaningful. Tolerant of several shapes so the same
    code works with a simple test lambda AND the real Power BI ``executeQueries`` JSON:

      * a bare scalar (``int``/``float``/``str``/``bool``/``None``) -> used directly;
      * ``{"error": ...}`` (truthy) -> ``(None, str(error))``;
      * ``{"value": v}`` -> ``v``;
      * ``{"rows": [...]}`` or a bare ``[...]`` list -> first row (recursing into a dict/scalar);
      * the executeQueries envelope ``{"results":[{"tables":[{"rows":[{col: v}]}]}]}`` -> dig in;
      * a row ``dict`` -> the ``prefer`` key (or ``"[prefer]"``), else the sole / first value.
    """
    if result is None:
        return (None, None)
    if isinstance(result, (int, float, bool, str)):
        return (result, None)

    if isinstance(result, dict):
        if result.get("error"):
            return (None, str(result["error"]))
        if "value" in result:
            return (result["value"], None)
        if "results" in result:  # Power BI executeQueries envelope
            try:
                rows = result["results"][0]["tables"][0]["rows"]
            except (IndexError, KeyError, TypeError):
                return (None, "unrecognized executeQueries envelope")
            return extract_scalar(rows, prefer=prefer)
        if "rows" in result:
            return extract_scalar(result["rows"], prefer=prefer)
        return _scalar_from_row(result, prefer)

    if isinstance(result, (list, tuple)):
        if not result:
            return (None, "empty result set")
        return extract_scalar(result[0], prefer=prefer)

    return (None, "unreadable oracle result of type %s" % type(result).__name__)


def _scalar_from_row(row, prefer):
    """Pull the single measure value out of one result row (a ``{column: value}`` dict)."""
    if not row:
        return (None, "empty row")
    for key in (prefer, "[%s]" % prefer):
        if key in row:
            return (row[key], None)
    values = list(row.values())
    if len(values) == 1:
        return (values[0], None)
    return (values[0], None)  # multi-column row: first column is the probed value


# --------------------------------------------------------------------------- the reconciliation
def reconcile(name, candidate_dax, *, fabric_oracle=None, tableau_value=_UNSET, tableau_oracle=None,
              kind=None, grain_filters=None, gate=True, value_name="value",
              rel_tol=DEFAULT_REL_TOL, abs_tol=DEFAULT_ABS_TOL, blank_equals_zero=True):
    """Verify one candidate DAX translation against the Tableau ground truth via injected oracles.

    The Tableau value comes from ``tableau_value`` (precomputed) or ``tableau_oracle()`` (a no-arg
    callable). The Fabric value comes from ``fabric_oracle(dax_query)`` -- a callable returning
    anything :func:`extract_scalar` can read. Both are optional; whatever is missing yields a
    ``not-evaluated`` record rather than a guess.

    Returns a verification record (never raises)::

        {
          "name", "candidate_dax", "state",        # state in VERIFIED|MISMATCH|NOT_EVALUATED
          "tableau_value", "fabric_value",
          "query",                                 # the EVALUATE probe sent to the Fabric oracle
          "kind", "tolerance": {...}, "detail",
          "gate": {...},                           # present iff the syntactic gate ran
          "delta": float,                          # present on a numeric mismatch
        }
    """
    record = {
        "name": name,
        "candidate_dax": candidate_dax,
        "state": NOT_EVALUATED,
        "tableau_value": None if tableau_value is _UNSET else tableau_value,
        "fabric_value": None,
        "query": None,
        "kind": kind,
        "tolerance": {
            "rel_tol": rel_tol, "abs_tol": abs_tol,
            "blank_equals_zero": blank_equals_zero,
            "exact": kind in _EXACT_KINDS,
        },
        "detail": None,
    }

    # 1. Never reconcile a candidate that fails the deterministic syntactic gate -- there is nothing
    #    faithful to verify, and we must not push malformed DAX at the backend.
    if gate:
        verdict = check_candidate_dax(candidate_dax)
        record["gate"] = verdict
        if not verdict.get("ok", False):
            record["detail"] = "candidate failed the syntactic gate; not evaluated"
            return record

    # 2. Resolve the Tableau ground-truth value (precomputed value wins; else call the oracle).
    truth = tableau_value
    if truth is _UNSET:
        if tableau_oracle is None:
            record["detail"] = "no Tableau ground-truth value or oracle provided"
            return record
        try:
            truth = tableau_oracle()
        except Exception as exc:  # noqa: BLE001 -- never raise out of reconciliation
            record["detail"] = "tableau oracle raised: %r" % (exc,)
            return record
    record["tableau_value"] = truth

    # 3. Evaluate the candidate against the Fabric model.
    if fabric_oracle is None:
        record["detail"] = "no Fabric oracle provided; candidate not executed"
        return record
    query = evaluate_query(candidate_dax, value_name=value_name, filters=grain_filters)
    record["query"] = query
    try:
        raw = fabric_oracle(query)
    except Exception as exc:  # noqa: BLE001
        record["detail"] = "fabric oracle raised: %r" % (exc,)
        return record
    fabric_value, err = extract_scalar(raw, prefer=value_name)
    record["fabric_value"] = fabric_value
    if err:
        record["detail"] = "fabric oracle error: %s" % err
        return record

    # 4. Compare at last.
    cmp = compare_scalars(truth, fabric_value, kind=kind,
                          rel_tol=rel_tol, abs_tol=abs_tol, blank_equals_zero=blank_equals_zero)
    record["state"] = cmp["state"]
    record["detail"] = cmp["detail"]
    if "delta" in cmp:
        record["delta"] = cmp["delta"]
    return record


def _infer_kind(formula):
    """Best-effort exact-vs-float hint from a formula (COUNTD/COUNT -> exact). ``None`` when unsure."""
    if not formula:
        return None
    up = str(formula).upper()
    if "COUNTD(" in up or "COUNT_DISTINCT" in up:
        return "countd"
    if "COUNT(" in up:
        return "count"
    return None


def reconcile_request(request, candidate_dax, *, fabric_oracle=None, tableau_value=_UNSET,
                      tableau_oracle=None, grain_filters=None, **kw):
    """Reconcile a candidate for a Tier-1 handoff ``request`` dict (from ``translation_handoff``).

    Thin convenience over :func:`reconcile`: takes ``name`` from the request and, when ``kind`` is
    not supplied, infers an exact/float hint from the request ``formula``. A ``dax_language_gap``
    request is annotated with ``approximation=True`` on the record because for that category an
    oracle match is *mandatory* before the candidate may land (per the playbook).
    """
    req = request or {}
    kind = kw.pop("kind", None)
    if kind is None:
        kind = _infer_kind(req.get("formula"))
    rec = reconcile(req.get("name"), candidate_dax, fabric_oracle=fabric_oracle,
                    tableau_value=tableau_value, tableau_oracle=tableau_oracle,
                    kind=kind, grain_filters=grain_filters, **kw)
    rec["category"] = req.get("category")
    if req.get("category") == DAX_LANGUAGE_GAP:
        rec["approximation"] = True
    return rec


def summarize(records):
    """Roll a list of verification records into a counts summary (mirrors the handoff summary)."""
    records = list(records or [])
    counts = {VERIFIED: 0, MISMATCH: 0, NOT_EVALUATED: 0}
    for rec in records:
        state = (rec or {}).get("state", NOT_EVALUATED)
        counts[state] = counts.get(state, 0) + 1
    total = len(records)
    return {
        "total": total,
        "verified": counts[VERIFIED],
        "mismatch": counts[MISMATCH],
        "not_evaluated": counts[NOT_EVALUATED],
        # fraction of candidates that were actually checked and passed (None when nothing landed)
        "verified_pct": round(100.0 * counts[VERIFIED] / total, 1) if total else None,
    }


def reconcile_all(items, *, fabric_oracle=None, tableau_oracle=None, **kw):
    """Reconcile a batch of ``{name, dax|candidate_dax, tableau_value?, kind?, grain_filters?}`` items.

    Per-item keys override the batch defaults. Returns ``{"records": [...], "summary": {...}}`` --
    the artifact the migration report's Final phase consumes. Never raises (each item is reconciled
    independently; a bad item becomes a ``not-evaluated`` record).
    """
    records = []
    for item in items or []:
        item = item or {}
        dax = item.get("dax", item.get("candidate_dax"))
        per = dict(kw)
        if "kind" in item:
            per["kind"] = item["kind"]
        if "grain_filters" in item:
            per["grain_filters"] = item["grain_filters"]
        if "value_name" in item:
            per["value_name"] = item["value_name"]
        tv = item.get("tableau_value", _UNSET)
        records.append(reconcile(item.get("name"), dax, fabric_oracle=fabric_oracle,
                                 tableau_value=tv, tableau_oracle=tableau_oracle, **per))
    return {"records": records, "summary": summarize(records)}


# --------------------------------------------------------------------------- candidate ranking
# Confidence labels for a ranked candidate. The vocabulary matches the ``confidence`` field a
# ``suggest_assisted_dax`` suggestion already carries, so the agent/orchestrator sees one scale.
RANK_HIGH = "high"      # empirically VERIFIED: evaluates to the Tableau ground truth within tolerance
RANK_MEDIUM = "medium"  # well-formed (passed the gate) but not empirically reconciled (no oracle/truth)
RANK_LOW = "low"        # proven wrong (mismatch) OR malformed (failed the syntactic gate)

# Sort tiers (lower == better). VERIFIED first; then gate-passed-but-unevaluated; then the rejected
# bucket (a proven-wrong mismatch or a malformed gate failure) last -- a human approves neither.
_TIER_VERIFIED = 0
_TIER_UNEVALUATED = 1
_TIER_REJECTED = 2


def _candidate_confidence(record):
    """Map one reconcile ``record`` to ``(tier, confidence, reason)`` for ranking. Pure; never raises.

    This is the SEMANTIC-equivalence score -- it reads the oracle verdict, not the candidate string.
    A VERIFIED candidate is high; a candidate that merely passed the syntactic gate but could not be
    reconciled (no oracle / no ground truth / an oracle error) is medium (plausible, still unproven);
    a candidate the oracle proved wrong (mismatch) or that the gate rejected (malformed) is low.
    """
    rec = record or {}
    state = rec.get("state", NOT_EVALUATED)
    gate = rec.get("gate") or {}
    if state == VERIFIED:
        return _TIER_VERIFIED, RANK_HIGH, "verified against the Tableau ground truth within tolerance"
    if state == MISMATCH:
        return _TIER_REJECTED, RANK_LOW, rec.get("detail") or "evaluated but disagreed with Tableau"
    # NOT_EVALUATED: a FAILED gate (malformed) is rejected; an otherwise-unevaluated candidate
    # (clean gate, just no oracle/truth) stays plausible-but-unproven.
    if gate and not gate.get("ok", True):
        issues = "; ".join(gate.get("issues") or []) or "failed the syntactic gate"
        return _TIER_REJECTED, RANK_LOW, "rejected by the syntactic gate: " + issues
    return _TIER_UNEVALUATED, RANK_MEDIUM, rec.get("detail") or "well-formed but not reconciled"


def _confidence_signals(record):
    """Decompose a reconcile ``record`` into the AUDITABLE signals behind its confidence grade.

    Returns ``{"gate", "oracle", "category"}`` -- the syntactic-gate verdict (``"pass"``/``"fail"``),
    the numeric-oracle verdict (the reconcile ``state``: verified / mismatch / not-evaluated), and the
    router ``category`` (present when the candidate was ranked for a classified handoff request, else
    ``None``). This is the machine-readable form of the one-line ``reason`` -- it lets a consumer see
    WHY a candidate earned its grade and apply category-specific acceptance rules. Pure; never raises.
    """
    rec = record or {}
    gate = rec.get("gate") or {}
    return {
        "gate": "pass" if gate.get("ok", True) else "fail",
        "oracle": rec.get("state", NOT_EVALUATED),
        "category": rec.get("category"),
    }


def _requires_oracle(record):
    """True when a candidate is an approximation the playbook accepts ONLY once empirically verified.

    A ``dax_language_gap`` calc has no faithful native DAX form, so ``second-compiler.md`` makes the
    oracle match **mandatory** before it may be proposed ("an unverifiable approximation stays a
    stub"). Ranking must therefore NOT auto-select such a candidate as ``best`` until the oracle
    confirms it -- even though it passed the syntactic gate. Other categories carry no such bar. Pure.
    """
    rec = record or {}
    return rec.get("category") == DAX_LANGUAGE_GAP and rec.get("state", NOT_EVALUATED) != VERIFIED


def _candidate_dax(candidate):
    """Extract the DAX text from one ranking candidate, always as a **string**.

    Accepts a raw DAX **string**, or a mapping carrying it under ``dax`` (the
    :func:`calc_to_dax.suggest_assisted_dax` suggestion shape an agent collects idiom candidates
    from) or ``candidate_dax`` (the :func:`reconcile_all` item shape). Anything we cannot resolve to
    DAX text -- a non-string / non-mapping, or a mapping with no recognized DAX key -- becomes the
    empty string: a TYPE-correct non-candidate the syntactic gate rejects ("candidate DAX is empty"),
    so a malformed candidate can never be graded plausible nor leak through as a non-string ``best``.
    Pure.
    """
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, dict):
        for key in ("dax", "candidate_dax"):
            value = candidate.get(key)
            if isinstance(value, str):
                return value
    return ""


def rank_candidates(name, candidates, *, fabric_oracle=None, tableau_value=_UNSET,
                    tableau_oracle=None, kind=None, request=None, **reconcile_kw):
    """Rank N agent-authored candidate DAX translations for ONE calc by SEMANTIC equivalence.

    The second compiler (the agent running this skill) proposes one or more candidate DAX
    translations for a calc the deterministic tier fell back on; this ranks them **best-first** by
    the empirical oracle, not by string matching. Each candidate is vetted with :func:`reconcile`
    (syntactic gate -> numeric oracle vs. the Tableau ground truth) and scored:

      * :data:`RANK_HIGH`   -- VERIFIED: evaluates to the Tableau value within tolerance.
      * :data:`RANK_MEDIUM` -- passed the gate but not empirically reconciled (no oracle/ground truth).
      * :data:`RANK_LOW`    -- proven wrong (mismatch) or malformed (failed the gate).

    Order: VERIFIED, then gate-passed-unevaluated, then the rejected bucket; ties keep the agent's
    submission order (stable). This is the optional acceleration tier's selection step -- it never
    lands anything; the chosen candidate still flows through the normal human-approval gate.

    Each ``candidate`` may be a raw DAX **string** or a mapping carrying it under ``dax`` (the
    :func:`calc_to_dax.suggest_assisted_dax` suggestion shape) / ``candidate_dax``; the emitted
    ``candidate_dax`` and ``best`` are always the resolved DAX **string**, so ``best`` is directly
    landable via ``approved_calc_dax``.

    ``request`` (an optional handoff dict) is echoed back and its ``category`` annotated on each
    record. ``tableau_value`` / ``tableau_oracle`` / ``fabric_oracle`` / ``kind`` and any extra
    ``reconcile_kw`` (e.g. ``grain_filters``, ``value_name``, ``rel_tol``) pass through to
    :func:`reconcile`. Returns (never raises)::

        {
          "name", "request",
          "ranked": [ {"candidate_dax", "rank", "confidence", "reason",
                       "signals", "requires_oracle", "record"}, ... ],  # best-first
          "best": <top candidate_dax, or None when none is acceptable>,
          "summary": {... summarize() ...},
        }

    ``signals`` is the auditable ``{gate, oracle, category}`` breakdown behind the grade.
    ``requires_oracle`` is ``True`` for an unverified ``dax_language_gap`` approximation -- the
    playbook makes its oracle match MANDATORY, so such a candidate is **never** chosen as ``best``
    until VERIFIED (it is still listed, with its medium grade, for the agent to reconcile or revise).
    """
    scored = []
    for idx, candidate in enumerate(list(candidates or [])):
        dax = _candidate_dax(candidate)
        rec = reconcile(name, dax, fabric_oracle=fabric_oracle, tableau_value=tableau_value,
                        tableau_oracle=tableau_oracle, kind=kind, **reconcile_kw)
        if request is not None:
            rec.setdefault("category", (request or {}).get("category"))
        tier, confidence, reason = _candidate_confidence(rec)
        scored.append((tier, idx, dax, rec, confidence, reason))
    scored.sort(key=lambda s: (s[0], s[1]))  # tier asc, then stable submission order
    ranked = []
    for i, (_tier, _idx, dax, rec, conf, reason) in enumerate(scored, start=1):
        requires_oracle = _requires_oracle(rec)
        if requires_oracle:
            reason = ("unverified dax_language_gap approximation -- a mandatory oracle match is "
                      "required before it may be proposed; " + reason)
        ranked.append({
            "candidate_dax": dax, "rank": i, "confidence": conf, "reason": reason,
            "signals": _confidence_signals(rec), "requires_oracle": requires_oracle, "record": rec,
        })
    # best = the top candidate that is neither low-confidence nor an unverified mandatory-oracle gap.
    best = next((r["candidate_dax"] for r in ranked
                 if r["confidence"] != RANK_LOW and not r["requires_oracle"]), None)
    return {
        "name": name,
        "request": request,
        "ranked": ranked,
        "best": best,
        "summary": summarize([r["record"] for r in ranked]),
    }
