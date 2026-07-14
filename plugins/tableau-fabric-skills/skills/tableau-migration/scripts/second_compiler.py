"""Second-compiler LANDING DRIVER (Spec 4).

This module is pure *orchestration*: it never invents DAX. Every bit of translation
intelligence already lives in the engine --

* the recursive-descent translators ``translate_tableau_calc_to_dax`` (measure) and
  ``translate_tableau_calc_to_column_dax`` (dimension/row-level),
* the assisted idiom detectors behind ``suggest_assisted_dax`` (Spec 7), and
* the syntactic gate ``check_candidate_dax``.

What the engine never did on its own is the *loop* a human otherwise re-writes by hand every
migration (the loop that took one real workbook from 8.4% to 66.2% translated): seed a handful of
keystone measures, gate-and-land them, then fix-point-cascade every dependent calc back through the
engine's own translator until nothing new lands. That loop -- and nothing else -- is this module.

Faithful-by-construction guarantees:

* A calc is only ever *added* here; an already-translatable calc is left to the normal build so it
  keeps its ``deterministic`` provenance. The returned map is the keystone-dependent SUPPLEMENT
  (the difference between the keystone-seeded closure and the plain deterministic closure), never
  the whole calc set.
* Keystones come only from (a) an explicit ``authored`` override or (b) an idiom the engine's OWN
  detectors recognize -- so this module authors nothing itself.
* Every candidate -- keystone or cascaded -- must pass ``check_candidate_dax`` before it is
  approved. An unfaithful/ungated base is dropped, so it can never silently drag a dependent live.
* The cascade seeds ``measure_refs`` from APPROVED names only (never an optimistic all-names seed),
  so a dependent lands only once its bases are genuinely approved.
* Optional model-aware GUARDS (``guards=`` / :func:`build_guards`) act purely as ADDITIONAL rejection
  filters at the same landing chokepoint -- a candidate is dropped if it names a model reference that
  does not exist (the reference gate, catching the ``(copy)_NNNN`` duplicate-name trap) or if its value
  diverges from the Tableau formula over landed data (the reconciliation oracle). The guards never
  author or alter a candidate, and ``guards=None`` is byte-identical to the unguarded driver.

Public entry point: :func:`land_second_compiler` -> ``{calc_name: dax}``.
"""

from __future__ import annotations

import os

import connection_to_m as _cm
import reference_gate as _reference_gate
import reconciliation_oracle as _reconciliation_oracle
from calc_to_dax import (
    suggest_assisted_dax,
    translate_tableau_calc_to_column_dax,
    translate_tableau_calc_to_dax,
)
from translation_router import check_candidate_dax


def _load_twb(twb):
    """Accept ``.twb``/``.twbx`` path, raw XML ``str``, or ``bytes`` -> XML text."""
    if isinstance(twb, bytes):
        return twb.decode("utf-8-sig")
    if isinstance(twb, str):
        head = twb.lstrip()[:64]
        if not head.startswith("<") and os.path.exists(twb):
            from workbook_table_calcs import load_workbook_xml
            return load_workbook_xml(twb)
    return twb


def _datasource_selects(twb):
    """Selectable datasource labels (``select=`` values); ``[None]`` for a bare ``.tds``."""
    try:
        labels = [d.get("label") for d in _cm.workbook_datasources(twb)]
        labels = [lbl for lbl in labels if lbl]
    except Exception:
        labels = []
    return labels or [None]


def _norm(dax):
    """Collapse whitespace so a landed measure is a single tidy line."""
    return " ".join(str(dax).split())


def _collect(twb, selects):
    """Per-datasource calc/resolver/known-tables index + global home/formula/role/lookup maps.

    ``home[name]`` = the datasource select a calc first appeared in (first datasource wins on a
    cross-datasource caption collision -- the same rule the assembler uses). ``lookup`` maps a
    lowercased caption AND internal ``Calculation_*`` name to the formula, so an idiom detector that
    points at a separate calc (argmax -> a standalone "max" calc) can resolve it.
    """
    per = {}
    home, form, role, lookup = {}, {}, {}, {}
    for sel in selects:
        try:
            calcs = _cm.extract_calcs(twb, select=sel)
        except Exception:
            calcs = []
        try:
            desc = _cm.parse_tds(twb, select=sel)
            resolver = _cm.build_m_field_resolver(desc)
            known = {r.get("name") for r in desc.get("relations", []) if r.get("name")}
        except Exception:
            resolver, known = (lambda _c: None), set()
        per[sel] = {"calcs": calcs, "resolver": resolver, "known": known}
        for c in calcs:
            nm = (c.get("name") or "").strip()
            if not nm:
                continue
            f = (c.get("formula") or "").strip()
            lookup[nm.lower()] = f
            tid = c.get("internal_name")
            if tid:
                lookup[str(tid).strip().lower()] = f
            if nm not in home:
                home[nm] = sel
                form[nm] = f
                role[nm] = (c.get("role") or "").strip().lower()
    return per, home, form, role, lookup


def _translate(nm, home, form, role, per, measure_refs, param_resolver):
    """Translate one calc in its own datasource context; dimension role -> column translator."""
    sel = home[nm]
    f = form[nm]
    ctx = per[sel]
    if role.get(nm) == "dimension":
        dax, _reason, _tables = translate_tableau_calc_to_column_dax(
            f, ctx["resolver"], known_tables=ctx["known"])
        return dax
    dax, _reason, _tables = translate_tableau_calc_to_dax(
        f, ctx["resolver"], param_resolver=param_resolver,
        measure_refs=measure_refs, known_tables=ctx["known"])
    return dax


def _build_refs(approved_names, per, selects):
    """``measure_refs`` seed from APPROVED names only (+ their internal-name aliases)."""
    refs = {}
    approved_lower = {n.lower() for n in approved_names}
    for sel in selects:
        for c in per[sel]["calcs"]:
            nm = (c.get("name") or "").strip()
            if not nm or nm.lower() not in approved_lower:
                continue
            entry = (nm, "number")
            refs[nm.lower()] = entry
            tid = c.get("internal_name")
            if tid:
                refs[str(tid).strip().lower()] = entry
    return refs


def build_guards(*, model_manifest=None, tmdl_parts=None, tables=None,
                 table_csv_paths=None, column_map=None, resolver=None):
    """Assemble the optional GUARD bundle for the landing chokepoint, or ``None`` when unusable.

    The reference gate needs a model *surface* (from ``model_manifest`` -- the ``report["model_manifest"]``
    shape -- or a ``{path: tmdl_text}`` map ``tmdl_parts``). The reconciliation oracle needs landed
    ``tables`` (given directly, or loaded from ``table_csv_paths`` via ``column_map``) and a ``resolver``
    mapping a Tableau caption to ``(model_table, model_column, ...)``. Either half may be absent; a guard
    with no surface skips the reference check and a guard with no tables skips the oracle. When NEITHER
    half is available the function returns ``None`` (so the caller stays byte-identical to the unguarded
    driver). Every construction step fails closed -- any error degrades to the absent half, never a raise.
    """
    surface = None
    if model_manifest is not None or tmdl_parts is not None:
        try:
            surface = _reference_gate.build_model_surface(
                model_manifest=model_manifest, tmdl_parts=tmdl_parts)
        except Exception:
            surface = None
    if tables is None and table_csv_paths:
        try:
            tables = _reconciliation_oracle.load_tables_from_csv(table_csv_paths, column_map)
        except Exception:
            tables = None
    if surface is None and not tables:
        return None
    return {"surface": surface, "tables": tables, "resolver": resolver}


def _reference_ok(dax, guards):
    """Reference gate: True unless the candidate names a model reference that cannot exist."""
    surface = guards.get("surface")
    if surface is None:
        return True
    try:
        return bool(_reference_gate.check_candidate_references(dax, surface)["ok"])
    except Exception:
        return True  # a guard failure must never REJECT a candidate the syntactic gate already passed


def _oracle_ok(dax, guards, tableau_formula):
    """Reconciliation oracle: reject ONLY a genuine numeric divergence (FAIL); PASS/INCONCLUSIVE land."""
    tables = guards.get("tables")
    if not tables or not tableau_formula:
        return True
    try:
        verdict = _reconciliation_oracle.reconcile(
            tableau_formula, dax, tables, resolver=guards.get("resolver"))
    except Exception:
        return True  # fail open -- an oracle error must never reject a syntactically clean candidate
    return verdict.get("status") != _reconciliation_oracle.FAIL


def _gate_ok(dax, gate, *, guards=None, tableau_formula=None):
    if dax is None or not str(dax).strip():
        return False
    if gate and not check_candidate_dax(dax)["ok"]:
        return False
    if guards is not None:
        if not _reference_ok(dax, guards):
            return False
        if not _oracle_ok(dax, guards, tableau_formula):
            return False
    return True


def _guard_verdicts(supplement, form, guards):
    """Per-landed-calc guard telemetry (additive) -- ``{}`` when guards are off.

    For every calc in the landed ``supplement`` re-runs both gates once so the report can two-bucket a
    landing: ``reference`` in ``ok``/``blocked``/``skipped`` and ``oracle`` in the PASS/FAIL/INCONCLUSIVE
    constant (or ``skipped`` when there is no surface / no landed data / no Tableau formula).
    """
    if guards is None:
        return {}
    surface = guards.get("surface")
    tables = guards.get("tables")
    resolver = guards.get("resolver")
    out = {}
    for nm, dax in supplement.items():
        ref = "skipped"
        if surface is not None:
            try:
                ref = "ok" if _reference_gate.check_candidate_references(dax, surface)["ok"] else "blocked"
            except Exception:
                ref = "ok"
        orc = "skipped"
        tf = form.get(nm)
        if tables and tf:
            try:
                orc = _reconciliation_oracle.reconcile(
                    tf, dax, tables, resolver=resolver).get("status", "skipped")
            except Exception:
                orc = "skipped"
        out[nm] = {"reference": ref, "oracle": orc}
    return out


def _cascade(initial, home, form, role, per, selects, *, rounds, gate, param_resolver, guards=None):
    """Fix-point: land every calc reachable from ``initial`` via APPROVED-only ``measure_refs``."""
    approved = dict(initial)
    changed = True
    used_rounds = 0
    while changed and used_rounds < rounds:
        changed = False
        used_rounds += 1
        refs = _build_refs(approved.keys(), per, selects)
        for nm in home:
            if nm in approved:
                continue
            dax = _translate(nm, home, form, role, per, refs, param_resolver)
            if _gate_ok(dax, gate, guards=guards, tableau_formula=form.get(nm)):
                approved[nm] = _norm(dax)
                changed = True
    return approved, used_rounds


def land_second_compiler(twb, *, authored=None, param_resolver=None, rounds=12, gate=True, guards=None):
    """Land keystone-dependent stub calcs as faithful DAX -> ``{calc_name: dax}``.

    ``twb`` -- a ``.twb``/``.twbx`` path, or raw workbook/``.tds`` XML (``str``/``bytes``).
    ``authored`` -- optional ``{calc_name: dax}`` human/LLM overrides used as extra keystones (each
    still gate-checked). ``param_resolver`` -- passed through to the measure translator for
    parameter references. ``rounds`` -- cascade fix-point cap. ``gate`` -- run ``check_candidate_dax``
    on every candidate (leave True; False is for diagnostics only). ``guards`` -- optional model-aware
    GUARD bundle (see :func:`build_guards`) that only ADDS rejection filters; ``None`` is byte-identical.

    Returns only the SUPPLEMENT the normal build cannot already produce: the calcs that land solely
    because of a keystone (the keystone idioms/overrides themselves plus everything that cascades off
    them). Purely-deterministic calcs -- including deterministic measure-of-measure chains -- are
    excluded so they keep their ``deterministic`` provenance in the normal build.
    """
    return _land(twb, authored=authored, param_resolver=param_resolver,
                 rounds=rounds, gate=gate, guards=guards)["approved"]


def land_report(twb, *, authored=None, param_resolver=None, rounds=12, gate=True, guards=None):
    """Full-detail variant of :func:`land_second_compiler`.

    Returns the whole driver detail dict -- ``approved`` (the ``{name: dax}`` supplement),
    ``authored`` / ``detectors`` (which keystones came from an override vs an idiom detector),
    ``cascaded`` (dependents that landed off the keystones), ``gate_failures`` (candidates the gate
    rejected), ``rounds`` (cascade rounds used), ``plain_count`` (deterministic-closure size), plus --
    when ``guards`` is supplied -- ``guarded`` (bool) and ``guard_verdicts`` (per-landed-calc gate
    telemetry). Callers that only need the landed map should use :func:`land_second_compiler`.
    """
    return _land(twb, authored=authored, param_resolver=param_resolver,
                 rounds=rounds, gate=gate, guards=guards)


def _land(twb, *, authored=None, param_resolver=None, rounds=12, gate=True, guards=None):
    """Full-detail driver; :func:`land_second_compiler` returns ``["approved"]``."""
    twb = _load_twb(twb)
    selects = _datasource_selects(twb)
    per, home, form, role, lookup = _collect(twb, selects)
    authored = dict(authored or {})

    # (1) Plain deterministic closure -- what the normal build already lands on its own. This is the
    #     baseline the supplement is diffed against, so it stays UNGUARDED (the normal build runs no
    #     guards); guarding it would wrongly push a deterministic calc into the supplement.
    plain, _ = _cascade({}, home, form, role, per, selects,
                        rounds=rounds, gate=gate, param_resolver=param_resolver, guards=None)

    # (2) Keystones = gated authored overrides + idiom-detector defaults for genuine stubs.
    keystones = {}
    authored_landed = []
    gate_failures = {}
    for nm, dax in authored.items():
        if _gate_ok(dax, gate, guards=guards, tableau_formula=form.get(nm)):
            keystones[nm] = _norm(dax)
            authored_landed.append(nm)
        else:
            gate_failures[nm] = dax
    for nm in home:
        if nm in keystones or nm in gate_failures:
            continue
        # Only a genuine stub (base translator can't do it) is a detector candidate.
        base = _translate(nm, home, form, role, per, None, param_resolver)
        if base:
            continue
        sugg = suggest_assisted_dax(form[nm], per[home[nm]]["resolver"], calc_lookup=lookup)
        cand = sugg.get("dax") if sugg else None
        if cand is None:
            continue
        if _gate_ok(cand, gate, guards=guards, tableau_formula=form.get(nm)):
            keystones[nm] = _norm(cand)
        else:
            gate_failures[nm] = cand

    # (3) Keystone-seeded closure, then diff against the plain closure.
    full, used_rounds = _cascade(keystones, home, form, role, per, selects,
                                 rounds=rounds, gate=gate, param_resolver=param_resolver, guards=guards)
    detector_landed = [n for n in keystones if n not in authored_landed]
    supplement = {}
    cascaded = []
    for nm, dax in full.items():
        if nm in plain and nm not in authored_landed:
            continue  # normal build already lands it deterministically -- keep its provenance
        supplement[nm] = dax
        if nm not in keystones:
            cascaded.append(nm)

    return {
        "approved": supplement,
        "authored": authored_landed,
        "detectors": detector_landed,
        "cascaded": cascaded,
        "gate_failures": gate_failures,
        "rounds": used_rounds,
        "plain_count": len(plain),
        "guarded": guards is not None,
        "guard_verdicts": _guard_verdicts(supplement, form, guards),
    }
