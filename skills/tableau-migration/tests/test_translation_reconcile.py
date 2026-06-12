"""Tests for the Tier-1 reconciliation core (``translation_reconcile``).

These lock the *offline* half of the validation loop: build the EVALUATE probe, read a Fabric-oracle
result, and compare it to the Tableau ground truth with the documented tolerance policy, producing a
``verified`` / ``mismatch`` / ``not-evaluated`` record. The oracles are injected as plain callables,
so no network is touched. The contract under test is the record SHAPE + STATE, not exact DAX text.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

import translation_reconcile as RC  # noqa: E402
import translation_router as R  # noqa: E402


_GOOD = "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))"


# --------------------------------------------------------------------------- compare_scalars
def test_compare_floats_within_tolerance_verifies():
    res = RC.compare_scalars(0.125640, 0.1256400001)
    assert res["state"] == RC.VERIFIED


def test_compare_floats_beyond_tolerance_mismatches_with_delta():
    res = RC.compare_scalars(100.0, 101.0)
    assert res["state"] == RC.MISMATCH
    assert res["delta"] == 1.0


def test_compare_counts_require_exact_equality():
    assert RC.compare_scalars(10194, 10194, kind="count")["state"] == RC.VERIFIED
    near = RC.compare_scalars(10194, 10193, kind="count")
    assert near["state"] == RC.MISMATCH          # one row off is a real mismatch for a count
    # the same near-miss would pass as a float, proving the kind gate matters
    assert RC.compare_scalars(10194, 10193)["state"] == RC.MISMATCH  # still beyond default rel_tol


def test_compare_both_blank_verifies():
    assert RC.compare_scalars(None, None)["state"] == RC.VERIFIED
    assert RC.compare_scalars("", None)["state"] == RC.VERIFIED


def test_compare_blank_vs_zero_policy():
    # default: an empty Tableau aggregation equals a DAX BLANK that reads as 0
    assert RC.compare_scalars(None, 0)["state"] == RC.VERIFIED
    assert RC.compare_scalars(None, 0, blank_equals_zero=False)["state"] == RC.MISMATCH
    # blank vs a non-zero value is always a mismatch
    assert RC.compare_scalars(None, 5)["state"] == RC.MISMATCH


def test_compare_text_exact():
    assert RC.compare_scalars("West", "West")["state"] == RC.VERIFIED
    assert RC.compare_scalars("West", "East")["state"] == RC.MISMATCH


def test_compare_numeric_strings_coerce():
    assert RC.compare_scalars("1,234.5", 1234.5)["state"] == RC.VERIFIED


def test_compare_type_mismatch():
    assert RC.compare_scalars(5, "abc")["state"] == RC.MISMATCH


# --------------------------------------------------------------------------- evaluate_query
def test_evaluate_query_basic():
    q = RC.evaluate_query("[Profit Ratio]")
    assert q.startswith("EVALUATE")
    assert 'ROW("value", [Profit Ratio])' in q


def test_evaluate_query_with_grain_filters_wraps_calculate():
    q = RC.evaluate_query("SUM('Orders'[Sales])",
                          filters=["'Orders'[Region] = \"West\""])
    assert "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Region] = \"West\")" in q


# --------------------------------------------------------------------------- extract_scalar
def test_extract_scalar_shapes():
    assert RC.extract_scalar(42) == (42, None)
    assert RC.extract_scalar({"value": 3.14}) == (3.14, None)
    assert RC.extract_scalar({"rows": [{"[value]": 7}]}) == (7, None)
    assert RC.extract_scalar([{"value": 9}]) == (9, None)
    # the real Power BI executeQueries envelope
    env = {"results": [{"tables": [{"rows": [{"[value]": 2326534.35}]}]}]}
    assert RC.extract_scalar(env) == (2326534.35, None)


def test_extract_scalar_errors_and_empties():
    val, err = RC.extract_scalar({"error": "boom"})
    assert val is None and "boom" in err
    val, err = RC.extract_scalar([])
    assert val is None and err


# --------------------------------------------------------------------------- reconcile (happy path)
def test_reconcile_verified():
    oracle = lambda q: {"rows": [{"[value]": 0.12564}]}  # noqa: E731
    rec = RC.reconcile("Profit Ratio", _GOOD, fabric_oracle=oracle, tableau_value=0.12564)
    assert rec["state"] == RC.VERIFIED
    assert rec["fabric_value"] == 0.12564
    assert rec["query"].startswith("EVALUATE")
    assert rec["gate"]["ok"] is True


def test_reconcile_mismatch():
    oracle = lambda q: 0.20   # noqa: E731  -- wrong number
    rec = RC.reconcile("Profit Ratio", _GOOD, fabric_oracle=oracle, tableau_value=0.12564)
    assert rec["state"] == RC.MISMATCH
    assert "delta" in rec


def test_reconcile_tableau_oracle_callable():
    rec = RC.reconcile("Profit Ratio", _GOOD,
                       fabric_oracle=lambda q: 0.12564,
                       tableau_oracle=lambda: 0.12564)
    assert rec["state"] == RC.VERIFIED


# --------------------------------------------------------------------------- reconcile (not-evaluated)
def test_reconcile_blocks_candidate_that_fails_gate():
    bad = "DIVIDE(SUM('Orders'[Sales])"   # unbalanced
    called = {"n": 0}

    def oracle(q):
        called["n"] += 1
        return 1.0

    rec = RC.reconcile("X", bad, fabric_oracle=oracle, tableau_value=1.0)
    assert rec["state"] == RC.NOT_EVALUATED
    assert rec["gate"]["ok"] is False
    assert called["n"] == 0                 # we must not hit the backend with malformed DAX


def test_reconcile_no_fabric_oracle():
    rec = RC.reconcile("X", _GOOD, tableau_value=1.0)
    assert rec["state"] == RC.NOT_EVALUATED
    assert "no Fabric oracle" in rec["detail"]


def test_reconcile_no_ground_truth():
    rec = RC.reconcile("X", _GOOD, fabric_oracle=lambda q: 1.0)
    assert rec["state"] == RC.NOT_EVALUATED
    assert "ground-truth" in rec["detail"]


def test_reconcile_oracle_raises_is_caught():
    def boom(q):
        raise RuntimeError("network down")

    rec = RC.reconcile("X", _GOOD, fabric_oracle=boom, tableau_value=1.0)
    assert rec["state"] == RC.NOT_EVALUATED
    assert "raised" in rec["detail"]


def test_reconcile_oracle_error_result_is_caught():
    rec = RC.reconcile("X", _GOOD, fabric_oracle=lambda q: {"error": "bad DAX"},
                       tableau_value=1.0)
    assert rec["state"] == RC.NOT_EVALUATED
    assert "bad DAX" in rec["detail"]


def test_reconcile_explicit_blank_ground_truth_is_compared_not_skipped():
    # tableau_value=None is a genuine BLANK to compare against (distinct from "no ground truth")
    rec = RC.reconcile("X", _GOOD, fabric_oracle=lambda q: 0, tableau_value=None)
    assert rec["state"] == RC.VERIFIED          # blank == 0 by default policy


# --------------------------------------------------------------------------- reconcile_request
def test_reconcile_request_infers_count_kind_and_pulls_name():
    req = {"name": "Distinct Customers", "formula": "COUNTD([Customer ID])",
           "category": R.UNSUPPORTED_OTHER}
    # an off-by-one would pass as a float but must FAIL as an inferred count
    rec = RC.reconcile_request(req, "DISTINCTCOUNT('Orders'[Customer ID])",
                               fabric_oracle=lambda q: 792, tableau_value=793)
    assert rec["name"] == "Distinct Customers"
    assert rec["kind"] == "countd"
    assert rec["state"] == RC.MISMATCH


def test_reconcile_request_flags_dax_language_gap_as_approximation():
    req = {"name": "San Flag", "formula": "REGEXP_MATCH([City], '^San')",
           "category": R.DAX_LANGUAGE_GAP}
    rec = RC.reconcile_request(req, 'IF(LEFT(\'Orders\'[City], 3) = "San", TRUE(), FALSE())',
                               fabric_oracle=lambda q: True, tableau_value=True)
    assert rec["approximation"] is True
    assert rec["category"] == R.DAX_LANGUAGE_GAP
    assert rec["state"] == RC.VERIFIED


# --------------------------------------------------------------------------- batch
def test_reconcile_all_summary_and_overrides():
    items = [
        {"name": "Sales", "dax": "SUM('Orders'[Sales])", "tableau_value": 100.0},
        {"name": "Rows", "dax": "COUNTROWS('Orders')", "tableau_value": 10, "kind": "count"},
        {"name": "Bad", "dax": "SUM('Orders'[Sales]", "tableau_value": 1.0},   # fails gate
    ]
    # one oracle answering all three; the count is intentionally wrong by one
    def oracle(q):
        if "COUNTROWS" in q:
            return 11
        return 100.0
    out = RC.reconcile_all(items, fabric_oracle=oracle)
    s = out["summary"]
    assert s["total"] == 3
    assert s["verified"] == 1          # Sales
    assert s["mismatch"] == 1          # Rows (count off by one)
    assert s["not_evaluated"] == 1     # Bad (gate)
    assert s["verified_pct"] == round(100.0 / 3, 1)


def test_summarize_empty():
    s = RC.summarize([])
    assert s["total"] == 0 and s["verified_pct"] is None
