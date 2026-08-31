"""The post-wrap re-check preserves every additive key the gate emits (#183).

THE DEFECT. ``_recheck_openability_after_wrap`` rebuilt ``openability_selfcheck`` from scratch,
naming four keys. Two the gate documents as ALWAYS PRESENT were read from nowhere and vanished:

* ``not_evaluated`` (#141) -- *"cross-reference the two keys for the tri-state: a check named here
  did not run, whatever ``checks`` says"*;
* ``reference_case_mismatches`` (#164) -- *"present and empty on a healthy build, so its absence is
  never mistaken for 'not evaluated'"*.

After the merge, its absence was exactly that. Reported at 2.339.0 over a 45-workbook estate: 43 of
44 payloads carried ``not_evaluated``, the one that did not was the one and only workbook with
``rechecked_after_row_predicate_wrap: true``, and it was missing ``endpoints_distinct`` from
``checks`` -- precisely the case ``not_evaluated`` exists to explain.

**The consumer this hurts most is the careful one.** Reading ``checks.get(name)`` naively is
unaffected; implementing the documented tri-state is what breaks, and it breaks silently.

THE FIX IS THE SHAPE, NOT THE TWO KEYS. Carrying ``not_evaluated`` and ``reference_case_mismatches``
by name would fix the report and reintroduce the defect the next time a key is added, because
nothing fails when a new key is forgotten. The merge now starts from the ``post`` payload and
overrides only the three fields it genuinely computes, so a new key is carried by default and only a
field needing a MERGE rather than a replace costs a line. ``test_a_future_additive_key_survives``
pins that property directly rather than describing it.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from migrate_estate import _recheck_openability_after_wrap  # noqa: E402
from openability_gate import check_model_openability  # noqa: E402

_DB = "database M\n\tcompatibilityLevel: 1606\n"
_MODEL = "model M\n\tculture: en-US\n"
_T = ("table T\n\tcolumn A\n\t\tdataType: string\n\t\tsourceColumn: A\n"
      "\tpartition T = m\n\t\tmode: import\n\t\tsource = Source\n")
_MEAS = ("table _Measures\n\tcolumn _d\n\t\tdataType: int64\n\t\tisHidden\n\t\tsourceColumn: _d\n"
         "\tmeasure 'M1' = COUNTROWS('T')\n"
         "\tpartition _Measures = m\n\t\tmode: import\n\t\tsource = Source\n")


def _parts():
    return {"definition/database.tmdl": _DB, "definition/model.tmdl": _MODEL,
            "definition/tables/T.tmdl": _T, "definition/tables/_Measures.tmdl": _MEAS}


def _recheck(before=None, parts=None):
    parts = parts or _parts()
    res = {"openability_selfcheck": dict(before if before is not None
                                         else check_model_openability(parts))}
    _recheck_openability_after_wrap(res, parts)
    return res["openability_selfcheck"]


def test_the_recheck_actually_ran():
    """Anti-vacuity. Every assertion below is about the merged payload, so a re-check that silently
    returned early would make all of them pass against the untouched original."""
    assert _recheck().get("rechecked_after_row_predicate_wrap") is True


def test_the_two_reported_keys_survive():
    out = _recheck()
    assert "not_evaluated" in out, "#141's tri-state key was dropped"
    assert "reference_case_mismatches" in out, "#164's disclosure key was dropped"


def test_no_key_the_gate_emits_is_lost():
    """Stronger than the two names: whatever the gate produces must still be there afterwards."""
    parts = _parts()
    before = check_model_openability(parts)
    out = _recheck(before, parts)
    lost = [k for k in before if k not in out]
    assert not lost, "the merge dropped %s" % lost


def test_a_future_additive_key_survives():
    """THE POINT. An allow-list of keys to carry would pass every test above and still lose the
    next key somebody adds -- which is exactly how this defect was introduced. Pinning the property
    (unknown keys survive) rather than the instance (these two survive).

    This test failed against my own first fix, which based the merge on ``post`` alone. That carries
    every key the CURRENT gate emits and still drops one that only ``before`` has -- which is what a
    key added to the gate between the two runs looks like. Every test naming the two reported keys
    passed against that version."""
    before = dict(check_model_openability(_parts()))
    before["some_future_disclosure"] = [{"detail": "x"}]
    out = _recheck(before)
    assert out.get("some_future_disclosure") == [{"detail": "x"}]


def test_the_post_run_wins_for_a_key_BOTH_runs_emit():
    """The other direction, and the reason the layering order is load-bearing. ``not_evaluated``
    describes the run that produced ``checks``; carrying the stale one would pair a fresh verdict
    with an old account of what it omits."""
    before = dict(check_model_openability(_parts()))
    before["not_evaluated"] = [{"check": "stale_from_the_pre_wrap_run"}]
    out = _recheck(before)
    assert out["not_evaluated"] != [{"check": "stale_from_the_pre_wrap_run"}], \
        "the merge kept the pre-wrap not_evaluated over the fresh one"


def test_the_computed_fields_are_still_merged_not_copied():
    """The re-check must not become a plain overwrite: ok is ANDed, checks are ANDed per name, and
    issues are unioned. A `dict(post)` with no overrides would pass the key tests and lose all
    three."""
    before = dict(check_model_openability(_parts()))
    before["ok"] = False
    before["checks"] = dict(before["checks"], invented_check=False)
    before["issues"] = [{"check": "invented_check", "detail": "from the pre-wrap run"}]
    out = _recheck(before)
    assert out["ok"] is False, "ok must be ANDed, not taken from the post run"
    assert out["checks"]["invented_check"] is False, \
        "a check absent from the post run must keep its original verdict"
    assert {"check": "invented_check", "detail": "from the pre-wrap run"} in out["issues"], \
        "pre-wrap issues must be preserved"


def test_a_check_that_fails_only_AFTER_the_wrap_still_fails():
    """The direction the re-check exists for -- the wrap adds measures, so a defect can appear that
    the pre-wrap payload could not have seen."""
    parts = _parts()
    before = dict(check_model_openability(parts))
    assert before["ok"] is True, "baseline model is not clean -- fixture invalid"
    broken = dict(parts)
    broken["definition/tables/_Measures.tmdl"] = _MEAS.replace(
        "COUNTROWS('T')", "COUNTROWS('T') + 'T'[NO_SUCH_COLUMN]")
    out = _recheck(before, broken)
    assert out["ok"] is False
    assert out["checks"]["dax_references_resolve"] is False


def test_no_prior_payload_falls_back_to_the_post_run_whole():
    res = {}
    _recheck_openability_after_wrap(res, _parts())
    out = res.get("openability_selfcheck")
    assert isinstance(out, dict)
    assert "not_evaluated" in out and "reference_case_mismatches" in out
