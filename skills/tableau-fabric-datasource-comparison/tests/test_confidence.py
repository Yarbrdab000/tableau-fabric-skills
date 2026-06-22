"""Unit tests for the confidence synthesis layer (``confidence.py``).

Confidence is an additive, deterministic, read-only judgement of how trustworthy each *verdict* is.
These tests pin the decision rules (per bucket), reciprocity, the verification fold-in, the cautions,
the rollup, and that it never touches a tier / score / bucket.
"""
import copy
import json

import compare
import confidence


BANDS = [["Exact", 0.85], ["Strong", 0.65], ["Partial", 0.40], ["Weak", 0.15], ["None", 0.0]]


def _match(bucket, score, *, name=0.0, column=0.0, type_=0.0, source=None,
           source_compared=False, contested=False, candidates=None, verification=None,
           has_best=True, tier="Strong", tableau_name="DS", fid="m1"):
    best = None
    if has_best:
        best = {
            "fabric_name": "Model", "fabric_id": fid, "workspace": "WS",
            "signals": {"name": name, "column": column, "type": type_, "source": source},
        }
    m = {
        "tableau_name": tableau_name, "bucket": bucket, "tier": tier, "score": score,
        "best_match": best, "source_compared": source_compared, "contested": contested,
        "candidates": candidates if candidates is not None else ([{"score": score}] if has_best else []),
    }
    if verification is not None:
        m["verification"] = verification
    return m


# --------------------------------------------------------------------------- already_exists rules


def test_strong_already_exists_is_high():
    m = _match("already_exists", 0.9, name=0.95, column=0.7, type_=0.9, source=0.8,
               source_compared=True, tier="Exact")
    r = confidence.match_confidence(m, reciprocal=True, bands=BANDS)
    assert r["level"] == "High"
    assert r["corroborating_signals"] >= 2
    assert r["reciprocal_best"] is True


def test_single_signal_already_exists_is_medium_with_caution():
    m = _match("already_exists", 0.7, name=0.9, column=0.2, type_=0.5, source=None)
    r = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    assert r["level"] == "Medium"
    assert any("single signal" in c for c in r["cautions"])
    assert any("obscured" in c for c in r["cautions"])


def test_near_tie_blocks_high_already_exists():
    m = _match("already_exists", 0.9, name=0.95, column=0.7, type_=0.9, source=0.8,
               source_compared=True, candidates=[{"score": 0.9}, {"score": 0.88}])
    r = confidence.match_confidence(m, reciprocal=True, bands=BANDS)
    assert r["level"] == "Medium"
    assert any("near tie" in c for c in r["cautions"])


def test_contested_blocks_high_already_exists():
    m = _match("already_exists", 0.9, name=0.95, column=0.7, type_=0.9, source=0.8,
               source_compared=True, contested=True)
    r = confidence.match_confidence(m, reciprocal=True, bands=BANDS)
    assert r["level"] == "Medium"
    assert any("another datasource" in c for c in r["cautions"])


def test_zero_signal_already_exists_is_low():
    m = _match("already_exists", 0.66, name=0.3, column=0.1, type_=0.2, source=None)
    r = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    assert r["level"] == "Low"


# --------------------------------------------------------------------------- verification fold-in


def test_verified_lifts_to_high_even_when_structurally_weak():
    m = _match("already_exists", 0.7, name=0.9, column=0.2, type_=0.5, source=None,
               verification={"verdict": "verified"})
    r = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    assert r["level"] == "High"


def test_compatible_lifts_partial_to_high():
    m = _match("partial", 0.5, name=0.7, column=0.4, type_=0.5,
               verification={"verdict": "compatible"})
    r = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    assert r["level"] == "High"


def test_mismatch_caps_at_low():
    m = _match("already_exists", 0.9, name=0.95, column=0.7, type_=0.9, source=0.8,
               source_compared=True, verification={"verdict": "mismatch"})
    r = confidence.match_confidence(m, reciprocal=True, bands=BANDS)
    assert r["level"] == "Low"
    assert any("disagreed" in c for c in r["cautions"])


# --------------------------------------------------------------------------- partial rules


def test_partial_caps_at_medium_without_verification():
    m = _match("partial", 0.55, name=0.9, column=0.6, type_=0.8, source=0.7, source_compared=True)
    r = confidence.match_confidence(m, reciprocal=True, bands=BANDS)
    assert r["level"] == "Medium"


def test_partial_near_tie_is_low():
    m = _match("partial", 0.5, name=0.7, column=0.4, type_=0.5,
               candidates=[{"score": 0.5}, {"score": 0.46}])
    r = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    assert r["level"] == "Low"


# --------------------------------------------------------------------------- rebuild rules


def test_no_candidate_rebuild_is_high():
    m = _match("rebuild", 0.0, has_best=False, tier="None")
    r = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    assert r["level"] == "High"
    assert any("no comparable" in d for d in r["drivers"])


def test_weak_score_rebuild_is_high():
    m = _match("rebuild", 0.12, name=0.2, column=0.1, type_=0.1, tier="None")
    r = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    assert r["level"] == "High"


def test_borderline_rebuild_is_low():
    # Just below the partial threshold (0.40): we might be wrongly rejecting a real partial.
    m = _match("rebuild", 0.36, name=0.4, column=0.3, type_=0.5, tier="Weak")
    r = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    assert r["level"] == "Low"
    assert any("borderline" in c for c in r["cautions"])


def test_midrange_rebuild_is_medium():
    m = _match("rebuild", 0.25, name=0.3, column=0.3, type_=0.4, tier="Weak")
    r = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    assert r["level"] == "Medium"


def test_rebuild_never_gets_near_tie_or_contested_caution():
    m = _match("rebuild", 0.0, has_best=False, tier="None", contested=True,
               candidates=[{"score": 0.0}, {"score": 0.0}])
    r = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    assert not any("near tie" in c for c in r["cautions"])
    assert not any("another datasource" in c for c in r["cautions"])


# --------------------------------------------------------------------------- reciprocity


def test_compute_reciprocity_picks_strongest_suitor():
    matches = [
        {"tableau_name": "A", "score": 0.9, "best_match": {"fabric_id": "m1"}},
        {"tableau_name": "B", "score": 0.6, "best_match": {"fabric_id": "m1"}},
        {"tableau_name": "C", "score": 0.8, "best_match": {"fabric_id": "m2"}},
    ]
    recip = confidence.compute_reciprocity(matches)
    assert recip["m1"] == "A"  # A outscores B for m1
    assert recip["m2"] == "C"


def test_compute_reciprocity_ignores_missing_best():
    matches = [{"tableau_name": "A", "score": 0.9, "best_match": None}]
    assert confidence.compute_reciprocity(matches) == {}


def test_reciprocity_counts_as_corroborator():
    m = _match("already_exists", 0.9, name=0.95, column=0.3, type_=0.9, source=None)
    non_recip = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    recip = confidence.match_confidence(m, reciprocal=True, bands=BANDS)
    assert recip["corroborating_signals"] == non_recip["corroborating_signals"] + 1
    assert any("mutual best" in d for d in recip["drivers"])


# --------------------------------------------------------------------------- annotate / rollup


def test_annotate_attaches_confidence_and_rollup():
    result = {
        "matches": [
            _match("already_exists", 0.9, name=0.95, column=0.7, type_=0.9, source=0.8,
                   source_compared=True, tableau_name="A"),
            _match("rebuild", 0.0, has_best=False, tier="None", tableau_name="B"),
        ],
        "summary": {"bands": BANDS},
    }
    confidence.annotate_confidence(result)
    assert all("confidence" in m for m in result["matches"])
    roll = result["summary"]["confidence"]
    assert roll["high"] >= 1
    assert set(roll) == {"high", "medium", "low", "high_confidence_already_exists",
                         "low_confidence_review"}


def test_annotate_is_idempotent():
    result = {
        "matches": [
            _match("already_exists", 0.9, name=0.95, column=0.7, type_=0.9, source=0.8,
                   source_compared=True, tableau_name="A"),
            _match("rebuild", 0.36, name=0.4, column=0.3, type_=0.5, tier="Weak", tableau_name="B"),
        ],
        "summary": {"bands": BANDS},
    }
    confidence.annotate_confidence(result)
    first = json.dumps(result, sort_keys=True)
    confidence.annotate_confidence(result)
    confidence.annotate_confidence(result)
    assert json.dumps(result, sort_keys=True) == first


def test_annotate_low_confidence_review_counts_match_buckets_only():
    result = {
        "matches": [
            # borderline rebuild -> Low, but NOT counted as review (rebuild bucket)
            _match("rebuild", 0.36, name=0.4, column=0.3, type_=0.5, tier="Weak", tableau_name="R",
                   fid="m1"),
            # zero-signal already_exists -> Low, counted as review (distinct model: no reciprocity lift)
            _match("already_exists", 0.66, name=0.3, column=0.1, type_=0.2, tableau_name="A",
                   fid="m2"),
        ],
        "summary": {"bands": BANDS},
    }
    confidence.annotate_confidence(result)
    assert result["summary"]["confidence"]["low_confidence_review"] == 1


# --------------------------------------------------------------------------- integration + invariants


def _estate():
    tab = [
        {"name": "Sales Orders", "project": "Fin",
         "fields": [{"name": "order_id"}, {"name": "amount"}, {"name": "region"}],
         "sources": [{"connector": "snowflake", "database": "DB", "schema": "P", "table": "ORDERS"}]},
        {"name": "Orphan", "project": "X",
         "fields": [{"name": "zzz1"}, {"name": "zzz2"}],
         "sources": [{"connector": "oracle", "database": "O", "schema": "S", "table": "W"}]},
    ]
    fab = [
        {"name": "Sales Orders", "workspace": "WS", "id": "m1",
         "tables": [{"name": "ORDERS"}],
         "columns": [{"name": "order_id", "table": "ORDERS"}, {"name": "amount", "table": "ORDERS"},
                     {"name": "region", "table": "ORDERS"}],
         "sources": [{"connector": "snowflake", "database": "DB", "schema": "P", "table": "ORDERS"}]},
    ]
    return tab, fab


def test_compare_inventories_attaches_confidence():
    tab, fab = _estate()
    res = compare.compare_inventories(tab, fab)
    assert "confidence" in res["summary"]
    assert all("confidence" in m for m in res["matches"])


def test_confidence_never_changes_tier_score_bucket():
    tab, fab = _estate()
    res = compare.compare_inventories(tab, fab)
    snapshot = [(m["tableau_name"], m["tier"], round(m["score"], 6), m["bucket"]) for m in res["matches"]]
    confidence.annotate_confidence(res)
    after = [(m["tableau_name"], m["tier"], round(m["score"], 6), m["bucket"]) for m in res["matches"]]
    assert snapshot == after


def test_report_renders_confidence_sections():
    tab, fab = _estate()
    res = compare.compare_inventories(tab, fab)
    md = compare.render_markdown(res)
    assert "## Verdict confidence" in md


def test_low_confidence_detail_section_renders_when_low_present():
    result = {
        "summary": {"bands": BANDS, "tableau_total": 1, "fabric_total": 1,
                    "already_exist": 1, "partial": 0, "rebuild": 0,
                    "by_tier": {"Exact": 0, "Strong": 1, "Partial": 0, "Weak": 0, "None": 0}},
        "matches": [
            _match("already_exists", 0.66, name=0.3, column=0.1, type_=0.2, tableau_name="Shaky",
                   tier="Strong"),
        ],
    }
    confidence.annotate_confidence(result)
    md = compare.render_markdown(result)
    assert "Lowest-confidence verdicts" in md
    assert "Shaky" in md


def test_match_confidence_tolerates_garbage_signals():
    m = {"tableau_name": "G", "bucket": "already_exists", "tier": "Strong", "score": "oops",
         "best_match": {"signals": {"name": None, "column": "x", "type": [], "source": {}}},
         "candidates": []}
    r = confidence.match_confidence(m, reciprocal=False, bands=BANDS)
    assert r["level"] in ("High", "Medium", "Low")
