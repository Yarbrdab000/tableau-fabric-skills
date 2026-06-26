"""Tests for ``translation_reconcile.rank_candidates`` -- the Tier-1 candidate selection step.

``rank_candidates`` scores N AGENT-authored candidate DAX strings for ONE translation by SEMANTIC
equivalence (the syntactic gate + the numeric oracle), not by string matching, and returns them
best-first with a confidence label + reasoning. It is fully deterministic and embeds NO LLM API:
the agent (the documented second compiler) proposes the candidates; this picks the trustworthy one
and explains why. These tests pin the ranking contract with injected oracles (no network).
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

import translation_reconcile as RC  # noqa: E402
import translation_router as R  # noqa: E402


def _wrap(v):
    """A Power BI executeQueries-style envelope ``extract_scalar`` can read."""
    return {"value": v}


def _oracle_by_marker(values):
    """Route the returned Fabric value by a marker substring: the candidate DAX is embedded verbatim
    in the ``EVALUATE ROW(...)`` probe, so each candidate can be made to evaluate differently."""
    def _o(query):
        for marker, v in values.items():
            if marker in query:
                return _wrap(v)
        return _wrap(None)
    return _o


# --------------------------------------------------------------------------- core ranking
def test_verified_candidate_outranks_a_mismatch_high_vs_low():
    cands = ["SUM('Orders'[GOOD])", "SUM('Orders'[BAD])"]
    oracle = _oracle_by_marker({"GOOD": 100.0, "BAD": 999.0})
    out = RC.rank_candidates("m", cands, fabric_oracle=oracle, tableau_value=100.0)
    ranked = out["ranked"]
    assert [r["candidate_dax"] for r in ranked] == cands           # GOOD (verified) before BAD (mismatch)
    assert ranked[0]["confidence"] == RC.RANK_HIGH
    assert ranked[0]["record"]["state"] == RC.VERIFIED
    assert ranked[0]["rank"] == 1 and ranked[1]["rank"] == 2
    assert ranked[1]["confidence"] == RC.RANK_LOW
    assert ranked[1]["record"]["state"] == RC.MISMATCH
    assert out["best"] == "SUM('Orders'[GOOD])"
    assert out["summary"]["verified"] == 1 and out["summary"]["mismatch"] == 1


def test_winner_is_semantic_not_positional():
    # Reversing submission order must NOT change the winner -- ranking is by the oracle, not position.
    cands = ["SUM('Orders'[BAD])", "SUM('Orders'[GOOD])"]
    oracle = _oracle_by_marker({"GOOD": 100.0, "BAD": 999.0})
    out = RC.rank_candidates("m", cands, fabric_oracle=oracle, tableau_value=100.0)
    assert out["best"] == "SUM('Orders'[GOOD])"
    assert out["ranked"][0]["candidate_dax"] == "SUM('Orders'[GOOD])"
    assert out["ranked"][0]["confidence"] == RC.RANK_HIGH


def test_text_candidate_verifies_by_value_equivalence():
    # A row-level name translation (C2-style): the winning candidate equals the Tableau text value.
    cands = ["SELECTEDVALUE('Orders'[RIGHT])", "SELECTEDVALUE('Orders'[WRONG])"]
    oracle = _oracle_by_marker({"RIGHT": "New York City", "WRONG": "Los Angeles"})
    out = RC.rank_candidates("c2", cands, fabric_oracle=oracle, tableau_value="New York City")
    assert out["best"] == "SELECTEDVALUE('Orders'[RIGHT])"
    assert out["ranked"][0]["confidence"] == RC.RANK_HIGH
    assert out["ranked"][1]["confidence"] == RC.RANK_LOW


# --------------------------------------------------------------------------- gate interaction
def test_malformed_ranks_below_a_wellformed_unevaluated_candidate():
    # With no oracle/truth a clean candidate is medium (plausible, unproven); a malformed one is low.
    cands = ["SUM('Orders'[Sales])", "SUM('Orders'[Sales]"]   # second is unbalanced
    out = RC.rank_candidates("m", cands)
    ranked = out["ranked"]
    assert ranked[0]["candidate_dax"] == "SUM('Orders'[Sales])"
    assert ranked[0]["confidence"] == RC.RANK_MEDIUM
    assert ranked[1]["candidate_dax"] == "SUM('Orders'[Sales]"
    assert ranked[1]["confidence"] == RC.RANK_LOW
    assert ranked[1]["record"]["gate"]["ok"] is False
    assert out["best"] == "SUM('Orders'[Sales])"


def test_leftover_tableau_idiom_candidate_is_low_confidence():
    cands = ["SUM('Orders'[Sales])", "{FIXED [State] : SUM([Sales])}"]
    out = RC.rank_candidates("m", cands, tableau_value=5.0)   # no oracle -> wellformed stays medium
    by = {r["candidate_dax"]: r for r in out["ranked"]}
    leftover = by["{FIXED [State] : SUM([Sales])}"]
    assert leftover["confidence"] == RC.RANK_LOW
    assert leftover["record"]["state"] == RC.NOT_EVALUATED
    assert leftover["record"]["gate"]["ok"] is False
    assert "leftover Tableau idiom" in "; ".join(leftover["record"]["gate"]["issues"])
    assert leftover["record"]["query"] is None                # the oracle path was never entered
    assert out["best"] == "SUM('Orders'[Sales])"


def test_every_candidate_low_yields_best_none():
    # All candidates malformed or proven wrong -> no trustworthy pick; best is None (agent must revise).
    cands = ["SUM('Orders'[Sales]", "0"]                      # unbalanced + inert stub (both fail the gate)
    out = RC.rank_candidates("m", cands, fabric_oracle=lambda q: _wrap(1.0), tableau_value=5.0)
    assert all(r["confidence"] == RC.RANK_LOW for r in out["ranked"])
    assert out["best"] is None


# --------------------------------------------------------------------------- shape / robustness
def test_each_ranked_entry_explains_its_reasoning():
    cands = ["SUM('Orders'[GOOD])", "SUM('Orders'[BAD])"]
    oracle = _oracle_by_marker({"GOOD": 100.0, "BAD": 999.0})
    out = RC.rank_candidates("m", cands, fabric_oracle=oracle, tableau_value=100.0)
    for r in out["ranked"]:
        assert isinstance(r["reason"], str) and r["reason"].strip()


def test_request_category_is_annotated_on_each_record():
    req = {"name": "m", "category": "dax_language_gap"}
    out = RC.rank_candidates("m", ["SUM('Orders'[Sales])"], request=req)
    assert out["request"] is req
    assert out["ranked"][0]["record"]["category"] == "dax_language_gap"


def test_empty_candidates_is_safe():
    out = RC.rank_candidates("m", [])
    assert out["ranked"] == []
    assert out["best"] is None
    assert out["summary"]["total"] == 0


def test_never_raises_on_a_throwing_oracle():
    # A backend that throws must degrade to not-evaluated/medium, not crash the ranker.
    def boom(q):
        raise RuntimeError("backend down")
    out = RC.rank_candidates("m", ["SUM('Orders'[Sales])"], fabric_oracle=boom, tableau_value=5.0)
    assert out["ranked"][0]["confidence"] == RC.RANK_MEDIUM
    assert out["ranked"][0]["record"]["state"] == RC.NOT_EVALUATED


# --------------------------------------------------------------------------- candidate input shapes
def test_candidate_can_be_a_suggest_assisted_dax_suggestion_dict():
    # The natural producer -- calc_to_dax.suggest_assisted_dax -- yields a dict carrying the DAX under
    # "dax" (plus pattern/confidence/caveats). rank_candidates must read that, not choke on the dict,
    # and emit the resolved DAX *string* so best is directly landable via approved_calc_dax.
    cands = [
        {"pattern": "argmin-dimension", "dax": "MINX('Orders',[BAD])", "confidence": "medium",
         "requires_approval": True, "caveats": []},
        {"pattern": "argmax-dimension", "dax": "MAXX('Orders',[GOOD])", "confidence": "medium",
         "requires_approval": True, "caveats": []},
    ]
    oracle = _oracle_by_marker({"GOOD": 100.0, "BAD": 999.0})
    out = RC.rank_candidates("m", cands, fabric_oracle=oracle, tableau_value=100.0)
    assert out["best"] == "MAXX('Orders',[GOOD])"               # the resolved string, not the dict
    assert isinstance(out["best"], str)
    assert out["ranked"][0]["candidate_dax"] == "MAXX('Orders',[GOOD])"
    assert out["ranked"][0]["confidence"] == RC.RANK_HIGH
    assert out["ranked"][1]["confidence"] == RC.RANK_LOW


def test_candidate_can_be_a_reconcile_all_style_dict():
    # reconcile_all items key the DAX under "candidate_dax"; accept that shape too.
    cands = [{"candidate_dax": "SUM('Orders'[GOOD])"}]
    oracle = _oracle_by_marker({"GOOD": 42.0})
    out = RC.rank_candidates("m", cands, fabric_oracle=oracle, tableau_value=42.0)
    assert out["best"] == "SUM('Orders'[GOOD])"
    assert out["ranked"][0]["confidence"] == RC.RANK_HIGH


def test_mixed_string_and_dict_candidates_rank_together():
    # A raw hand-authored string and a registry suggestion dict can be ranked in one call; the raw
    # string path stays byte-identical (its candidate_dax is the string itself).
    cands = ["SUM('Orders'[BAD])", {"dax": "SUM('Orders'[GOOD])", "pattern": "x"}]
    oracle = _oracle_by_marker({"GOOD": 7.0, "BAD": 999.0})
    out = RC.rank_candidates("m", cands, fabric_oracle=oracle, tableau_value=7.0)
    assert out["best"] == "SUM('Orders'[GOOD])"
    assert out["ranked"][0]["confidence"] == RC.RANK_HIGH
    assert out["ranked"][1]["candidate_dax"] == "SUM('Orders'[BAD])"
    assert out["ranked"][1]["confidence"] == RC.RANK_LOW


# --------------------------------------------------------------------------- category-aware acceptance
def test_unverified_dax_language_gap_is_not_auto_selected_as_best():
    # second-compiler.md: a dax_language_gap approximation needs a MANDATORY oracle match before it may
    # be proposed. With no oracle it passes the gate (medium) but must NOT be picked as best.
    req = {"name": "m", "category": RC.DAX_LANGUAGE_GAP}
    out = RC.rank_candidates("m", ["SUM('Orders'[Sales])"], request=req)   # no oracle -> not verified
    entry = out["ranked"][0]
    assert entry["confidence"] == RC.RANK_MEDIUM           # plausible, but...
    assert entry["requires_oracle"] is True                # ...the mandatory oracle is not satisfied
    assert out["best"] is None                             # so it is NOT auto-selected
    assert "mandatory" in entry["reason"].lower()


def test_verified_dax_language_gap_is_selected_as_best():
    # Once the oracle CONFIRMS the approximation, the mandatory-oracle bar is met -> selectable.
    req = {"name": "m", "category": RC.DAX_LANGUAGE_GAP}
    out = RC.rank_candidates("m", ["SUM('Orders'[GOOD])"], request=req,
                             fabric_oracle=_oracle_by_marker({"GOOD": 12.0}), tableau_value=12.0)
    entry = out["ranked"][0]
    assert entry["confidence"] == RC.RANK_HIGH
    assert entry["requires_oracle"] is False
    assert out["best"] == "SUM('Orders'[GOOD])"


def test_non_gap_medium_candidate_is_still_selectable_as_best():
    # The mandatory-oracle bar is SPECIFIC to dax_language_gap -- an ordinary medium candidate in a
    # different category is still a valid best when no oracle is available.
    req = {"name": "m", "category": "type_or_shape_mismatch"}
    out = RC.rank_candidates("m", ["SUM('Orders'[Sales])"], request=req)
    assert out["ranked"][0]["requires_oracle"] is False
    assert out["best"] == "SUM('Orders'[Sales])"


def test_each_ranked_entry_carries_an_auditable_signal_breakdown():
    req = {"name": "m", "category": "type_or_shape_mismatch"}
    out = RC.rank_candidates("m", ["SUM('Orders'[GOOD])", "SUM('Orders'[BAD])"], request=req,
                             fabric_oracle=_oracle_by_marker({"GOOD": 3.0, "BAD": 9.0}),
                             tableau_value=3.0)
    for r in out["ranked"]:
        sig = r["signals"]
        assert sig["gate"] in ("pass", "fail")
        assert sig["oracle"] in (RC.VERIFIED, RC.MISMATCH, RC.NOT_EVALUATED)
        assert sig["category"] == "type_or_shape_mismatch"
    top = out["ranked"][0]
    assert top["signals"]["oracle"] == RC.VERIFIED and top["signals"]["gate"] == "pass"


def test_real_router_dax_language_gap_classification_enforces_mandatory_oracle():
    # Integration: drive the REAL router classification (not a hand-built category) for a CONFIRMED
    # real gap -- REGEXP_MATCH, which DAX cannot express natively -- and prove rank_candidates honours
    # the mandatory-oracle rule on it. Closes the unit->integration seam: the router category the
    # production handoff actually stamps must engage the (d) enforcement.
    routed = R.classify_fallback("unsupported function REGEXP_MATCH")
    assert routed["category"] == RC.DAX_LANGUAGE_GAP          # the real router agrees it is a gap
    req = {"name": "Is Promo SKU", "category": routed["category"]}

    # An agent's plausible-but-unverified approximation must NOT be auto-selected.
    unverified = RC.rank_candidates("Is Promo SKU", ["SEARCH(\"PROMO\",'Orders'[SKU],1,0)>0"],
                                    request=req)
    assert unverified["ranked"][0]["requires_oracle"] is True
    assert unverified["best"] is None

    # Once the oracle CONFIRMS it against Tableau truth, the mandatory bar is met and it is selectable.
    verified = RC.rank_candidates("Is Promo SKU", ["SEARCH(\"GOOD\",'Orders'[SKU],1,0)>0"],
                                  request=req, fabric_oracle=_oracle_by_marker({"GOOD": True}),
                                  tableau_value=True)
    assert verified["ranked"][0]["requires_oracle"] is False
    assert verified["best"] == "SEARCH(\"GOOD\",'Orders'[SKU],1,0)>0"


# --------------------------------------------------------------------------- graceful degradation
# rank_candidates is the optional acceleration tier and documents "never raises" + "best is always a
# landable DAX string or None". These pin that contract against the degenerate inputs an agent can
# realistically hand it: zero candidates, a None list, and malformed candidates with no DAX payload.
def test_empty_candidate_list_returns_no_best_without_raising():
    out = RC.rank_candidates("m", [])
    assert out["ranked"] == []
    assert out["best"] is None
    assert out["summary"]["total"] == 0


def test_none_candidates_argument_is_handled_gracefully():
    out = RC.rank_candidates("m", None)
    assert out["ranked"] == []
    assert out["best"] is None


def test_malformed_candidate_dict_without_dax_is_rejected_never_best():
    # REGRESSION: a suggestion dict carrying NO dax/candidate_dax key must not masquerade as a
    # translation. It resolves to the empty string (a type-correct non-candidate), is rejected by the
    # gate, graded low, and is NEVER returned as best -- guarding the "best is a landable string"
    # contract (the bug returned the raw dict as best at a plausible medium grade).
    out = RC.rank_candidates("m", [{"pattern": "argmax-dimension", "confidence": "high"}])
    entry = out["ranked"][0]
    assert isinstance(entry["candidate_dax"], str)        # never the raw dict
    assert entry["candidate_dax"] == ""
    assert entry["confidence"] == RC.RANK_LOW
    assert entry["signals"]["gate"] == "fail"
    assert out["best"] is None


def test_none_candidate_in_list_is_gate_rejected_never_best():
    # A None among the candidates is a type-correct empty non-candidate: gate-rejected, low, not best.
    out = RC.rank_candidates("m", [None, "SUM('Orders'[GOOD])"],
                             fabric_oracle=_oracle_by_marker({"GOOD": 5.0}), tableau_value=5.0)
    by_dax = {r["candidate_dax"]: r for r in out["ranked"]}
    assert by_dax[""]["confidence"] == RC.RANK_LOW        # the None became "" and was rejected
    assert out["best"] == "SUM('Orders'[GOOD])"           # the real candidate still wins


