"""Golden-loop regression harness for the Tier-1 assisted (second-compiler) tier.

The individual Tier-1 pieces are unit-tested elsewhere -- the idiom detector in
``test_assisted_translate.py``, the router in ``test_translation_router.py``, the syntactic gate +
numeric oracle in ``test_translation_reconcile.py``. What none of them does is drive a *corpus* of
known-good translations through the **whole** loop end-to-end:

    suggest_assisted_dax  ->  check_candidate_dax (syntactic gate)  ->  reconcile (numeric oracle)

This harness does, against a small CORPUS of real, ground-truth-bearing entries:

  * every shipped IDIOM detector (today: argmax-over-a-dimension) -- detected, gated, and reconciled
    against a representative value, with NON-VACUITY proof (a wrong oracle value MISMATCHes; a
    corrupted / inert candidate fails the gate WITHOUT touching the backend); and
  * the canonical human-approved SIDECAR pairs the fleet validated live (C1 "Highest Selling City By
    State Sales" = 1,221,139.3614; C2 "...(name)") -- locking the shipped bytes against the gate and,
    for the scalar measure, the reconciliation oracle.

It is pure additive TEST infrastructure -- it imports the real engine entry points and asserts on
their output; it changes no emit logic. The corpus is the extensibility seam: **every new idiom
detector added to ``_ASSISTED_DETECTORS`` must add a corpus row** (``test_corpus_covers_every_
registered_detector`` fails until it does), so each new idiom automatically inherits the full
detect -> gate -> reconcile guard.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

from calc_to_dax import suggest_assisted_dax, _ASSISTED_DETECTORS  # noqa: E402
import translation_router as R  # noqa: E402
import translation_reconcile as RC  # noqa: E402


# --------------------------------------------------------------------------- shared resolver
# caption -> (table_display_name, clean_col, tmdl_type), matching test_assisted_translate.py.
_FIELDS = {
    "Sales": ("Orders", "Sales", "decimal"),
    "State": ("Orders", "State", "string"),
    "City": ("Orders", "City", "string"),
}


def _resolver(caption):
    return _FIELDS.get(caption)


# --------------------------------------------------------------------------- idiom formulas
# The canonical argmax idiom: "the city with the most sales in each state".
_ARGMAX_DETAIL = "{FIXED [State], [City] : SUM([Sales])}"
_ARGMAX_MAX = "{FIXED [State] : MAX({FIXED [State], [City] : SUM([Sales])})}"
_ARGMAX_INLINE = f"IF {_ARGMAX_MAX} = {_ARGMAX_DETAIL} THEN [City] END"
# The argmin twin (MIN selector) -- same structural idiom, MINX/__min emit.
_ARGMIN_MIN = "{FIXED [State] : MIN({FIXED [State], [City] : SUM([Sales])})}"
_ARGMIN_INLINE = f"IF {_ARGMIN_MIN} = {_ARGMAX_DETAIL} THEN [City] END"


# --------------------------------------------------------------------------- canonical sidecar DAX
# The human-approved, model-namespace (State_Province) bytes the fleet validated live. C1 is a scalar
# MEASURE (reconcilable to its known total); C2 is a row-level column calc (gate-only -- a per-row
# name has no single scalar to reconcile here).
_C1_SALES_DAX = (
    "SUMX(\n"
    "    'Orders',\n"
    "    VAR cityTotal = CALCULATE(SUM('Orders'[Sales]), "
    "ALLEXCEPT('Orders','Orders'[State_Province],'Orders'[City]))\n"
    "    VAR stateMax  = CALCULATE(MAXX(VALUES('Orders'[City]), CALCULATE(SUM('Orders'[Sales]))), "
    "ALLEXCEPT('Orders','Orders'[State_Province]))\n"
    "    RETURN IF(cityTotal = stateMax, 'Orders'[Sales])\n"
    ")"
)
_C2_NAME_DAX = (
    "VAR cityTotal = CALCULATE(SUM('Orders'[Sales]), "
    "ALLEXCEPT('Orders','Orders'[State_Province],'Orders'[City]))\n"
    "VAR stateMax  = CALCULATE(MAXX(VALUES('Orders'[City]), CALCULATE(SUM('Orders'[Sales]))), "
    "ALLEXCEPT('Orders','Orders'[State_Province]))\n"
    "RETURN IF(cityTotal = stateMax, 'Orders'[State_Province])"
)


# --------------------------------------------------------------------------- the corpus
# Each entry is one known-good translation with a representative ground-truth value.
#   kind="detect"  -> the engine must DETECT it via suggest_assisted_dax (an idiom); ``pattern`` is
#                     the expected suggestion pattern. ``truth``/``wrong`` drive the oracle.
#   kind="sidecar" -> a human-approved candidate supplied directly as ``dax`` (no detection).
#                     ``truth`` reconciles a scalar; ``gate_only`` skips the numeric reconcile.
_CORPUS = [
    {
        "name": "argmax: city with the most sales per state",
        "kind": "detect",
        "pattern": "argmax-dimension",
        "formula": _ARGMAX_INLINE,
        "calc_lookup": None,
        "truth": "New York City",
        "wrong": "Los Angeles",
        "value_kind": None,            # text -> exact compare
    },
    {
        "name": "argmin: city with the least sales per state",
        "kind": "detect",
        "pattern": "argmin-dimension",
        "formula": _ARGMIN_INLINE,
        "calc_lookup": None,
        "truth": "Burlington",
        "wrong": "New York City",
        "value_kind": None,            # text -> exact compare
    },
    {
        "name": "Highest Selling City By State Sales",   # C1 canonical sidecar (scalar measure)
        "kind": "sidecar",
        "dax": _C1_SALES_DAX,
        "truth": 1221139.3614,
        "wrong": 1221139.3614 * 1.10,
        "value_kind": None,            # money -> float compare within rel tol
    },
    {
        "name": "Highest Selling City By State (name)",  # C2 canonical sidecar (row-level column)
        "kind": "sidecar",
        "dax": _C2_NAME_DAX,
        "gate_only": True,
    },
]

_DETECT = [e for e in _CORPUS if e["kind"] == "detect"]
_SIDECAR = [e for e in _CORPUS if e["kind"] == "sidecar"]


def _candidate_dax(entry):
    """The DAX under test -- detected for an idiom entry, supplied for a sidecar entry."""
    if entry["kind"] == "detect":
        s = suggest_assisted_dax(entry["formula"], _resolver, calc_lookup=entry.get("calc_lookup"))
        assert s is not None, "%s: idiom not detected" % entry["name"]
        return s["dax"], s
    return entry["dax"], None


def _wrap(value):
    """A Power BI executeQueries-style oracle envelope extract_scalar can read."""
    return {"value": value}


# --------------------------------------------------------------------------- detected idioms
def test_each_detected_idiom_suggests_expected_pattern_and_passes_gate():
    for entry in _DETECT:
        dax, s = _candidate_dax(entry)
        assert s["pattern"] == entry["pattern"], entry["name"]
        assert s["requires_approval"] is True, entry["name"]
        verdict = R.check_candidate_dax(dax)
        assert verdict["ok"] is True, "%s gate issues: %r" % (entry["name"], verdict.get("issues"))


def test_each_detected_idiom_reconciles_against_ground_truth():
    for entry in _DETECT:
        dax, _ = _candidate_dax(entry)
        rec = RC.reconcile(entry["name"], dax,
                           fabric_oracle=lambda q, v=entry["truth"]: _wrap(v),
                           tableau_value=entry["truth"], kind=entry.get("value_kind"))
        assert rec["state"] == RC.VERIFIED, "%s: %r" % (entry["name"], rec.get("detail"))


# --------------------------------------------------------------------------- non-vacuity
def test_wrong_oracle_value_is_caught_as_mismatch():
    # If the loop "verified" no matter what, it would be worthless. A wrong Fabric value must MISMATCH.
    for entry in _DETECT + _SIDECAR:
        if entry.get("gate_only") or "wrong" not in entry:
            continue
        dax, _ = _candidate_dax(entry)
        rec = RC.reconcile(entry["name"], dax,
                           fabric_oracle=lambda q, v=entry["wrong"]: _wrap(v),
                           tableau_value=entry["truth"], kind=entry.get("value_kind"))
        assert rec["state"] == RC.MISMATCH, "%s should mismatch on a wrong value" % entry["name"]


def test_corrupt_or_inert_candidate_fails_gate_without_hitting_oracle():
    # A malformed or inert candidate must be caught by the syntactic gate BEFORE any backend call.
    hits = {"n": 0}

    def counting_oracle(q):
        hits["n"] += 1
        return _wrap(1.0)

    for entry in _DETECT + _SIDECAR:
        dax, _ = _candidate_dax(entry)
        corrupt = dax[:-1] if dax.endswith(")") else dax + "("   # unbalance the delimiters
        rec = RC.reconcile(entry["name"], corrupt, fabric_oracle=counting_oracle, tableau_value=1.0)
        assert rec["state"] == RC.NOT_EVALUATED, entry["name"]
        assert rec["gate"]["ok"] is False, entry["name"]

    # an inert stub is likewise refused
    inert = RC.reconcile("inert", "0", fabric_oracle=counting_oracle, tableau_value=1.0)
    assert inert["state"] == RC.NOT_EVALUATED
    assert inert["gate"]["ok"] is False
    assert hits["n"] == 0, "the backend oracle must never be hit with a candidate that fails the gate"


# --------------------------------------------------------------------------- canonical sidecar lock
def test_approved_sidecar_canonical_pairs_pass_gate_and_reconcile():
    for entry in _SIDECAR:
        verdict = R.check_candidate_dax(entry["dax"])
        assert verdict["ok"] is True, "%s gate issues: %r" % (entry["name"], verdict.get("issues"))
        if entry.get("gate_only"):
            continue
        rec = RC.reconcile(entry["name"], entry["dax"],
                           fabric_oracle=lambda q, v=entry["truth"]: _wrap(v),
                           tableau_value=entry["truth"], kind=entry.get("value_kind"))
        assert rec["state"] == RC.VERIFIED, "%s: %r" % (entry["name"], rec.get("detail"))


# --------------------------------------------------------------------------- registry sync guard
def test_corpus_covers_every_registered_detector():
    # Forcing function: every detector in the registry must fire on at least one corpus formula, so a
    # newly-registered idiom cannot ship without a golden detect -> gate -> reconcile entry.
    for detector in _ASSISTED_DETECTORS:
        fired = any(
            detector(e["formula"], _resolver, e.get("calc_lookup")) is not None for e in _DETECT)
        assert fired, "detector %s has no golden corpus entry" % detector.__name__
    # The argmax/argmin family is one detector emitting two patterns -- lock that both are exercised.
    patterns = {e["pattern"] for e in _DETECT}
    assert {"argmax-dimension", "argmin-dimension"} <= patterns, sorted(patterns)
