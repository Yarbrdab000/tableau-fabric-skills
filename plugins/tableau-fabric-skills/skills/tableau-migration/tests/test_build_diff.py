"""``build_diff`` must classify the known non-substantive terms and never hide one.

Comparing two migration output trees is a normal task -- after an engine upgrade, when reproducing
a defect, or to check a change did what it claimed. A handful of terms differ between any two runs
for reasons that carry no information, and a naive diff drowns in them.

WHY THIS IS A SHIPPED FUNCTION AND NOT A HABIT. In one day, five independent probes -- written by
two people who had each explicitly told the other about these terms -- re-implemented this
normalisation and got it wrong:

    missed lineageTag + root      reported 85 changed model files for a pill-binding change
    missed the JSON-ESCAPED root  reported 1 "unexplained" residual that looked like a defect
    missed the output root        reported 82 files of "non-determinism" that were two dir names
    masked the root INSIDE one    left every FRESH probe blind, because the mask lived in the tool
    tool                          rather than anywhere reusable

A normalisation that lives in one tool does not protect the next tool you write.

CLASSIFY, DO NOT HIDE. Each term is counted and reported separately, so "0 GUID differences" is
distinguishable from "GUIDs were masked". A differ that silently masks a term makes its own blind
spot invisible, and the blind spot then survives every comparison anyone runs with it. That is not
hypothetical: one differ here masked the build root from the day it was written, so its corpus
comparisons were always clean and the blindness only surfaced when a fresh probe was written
without the mask.

Validated against real trees: on two builds at the same revision it reports 80 root / 4 timestamp /
0 GUID / 0 substantive, and across the 2.358.0 fix it reports 26 GUID files (63 tokens) and exactly
the 2 genuinely-changed ``visual.json`` -- figures independently derived by hand beforehand.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest  # noqa: E402

import build_diff as B  # noqa: E402


def _tree(base, files):
    for rel, text in files.items():
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    return base


GUID_A = "11111111-2222-3333-4444-555555555555"
GUID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ------------------------------------------------------------------ the normaliser's own hit counts

def test_normalize_reports_how_many_of_each_term_it_matched():
    """The counts are returned so a caller can assert a normaliser actually engaged.

    A normaliser that matches nothing has told you about its predicate, not the data -- and it
    fails silently, because un-normalised text simply compares unequal and reads as a real change.
    """
    text = 'x C:\\t\\out\\a lineageTag: %s at 2026-09-03T21:06:10Z' % GUID_A
    out, hits = B.normalize(text, "C:\\t\\out")
    assert hits["root"] == 1 and hits["guid"] == 1 and hits["timestamp"] == 1
    assert B.ROOT_TOKEN in out and B.GUID_TOKEN in out and B.TIMESTAMP_TOKEN in out


def test_the_json_escaped_root_spelling_is_handled():
    """The specific miss that produced a phantom "unexplained residual".

    A JSON file escapes the separator, so ``C:\\t\\out`` is written ``C:\\\\t\\\\out``. A normaliser
    matching only the single-backslash form leaves every ``.json`` path un-normalised.
    """
    escaped = '{"model_folder": "C:\\\\t\\\\out\\\\pbip"}'
    assert "C:\\\\t\\\\out" in escaped
    out, hits = B.normalize(escaped, "C:\\t\\out")
    assert hits["root"] == 1, "the escaped spelling was not matched"
    assert "out" not in out.replace(B.ROOT_TOKEN, ""), out


def test_root_spellings_puts_the_longest_first():
    """Order matters: the escaped form must be consumed before the plain form can match half of it."""
    forms = B.root_spellings("C:\\t\\out")
    assert forms == sorted(forms, key=len, reverse=True)
    assert "C:\\\\t\\\\out" in forms and "C:\\t\\out" in forms and "C:/t/out" in forms


# ----------------------------------------------------------------------------- classification

def test_identical_trees_have_nothing_substantive(tmp_path):
    a = _tree(str(tmp_path / "a"), {"m/x.tmdl": "lineageTag: %s\nvalue: 1\n" % GUID_A})
    b = _tree(str(tmp_path / "b"), {"m/x.tmdl": "lineageTag: %s\nvalue: 1\n" % GUID_A})
    res = B.compare(a, b)
    assert res["changed"] == [] and res["substantive"] == []


def test_a_build_root_difference_is_not_substantive(tmp_path):
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    _tree(a, {"m/x.tmdl": 'Source = File.Contents("%s\\data\\f.xlsx")\n' % a})
    _tree(b, {"m/x.tmdl": 'Source = File.Contents("%s\\data\\f.xlsx")\n' % b})
    res = B.compare(a, b)
    assert res["by_reason"]["root"] == ["m/x.tmdl".replace("/", os.sep)]
    assert res["substantive"] == []


def test_a_guid_difference_is_reported_but_not_substantive(tmp_path):
    a = _tree(str(tmp_path / "a"), {"m/x.tmdl": "lineageTag: %s\n" % GUID_A})
    b = _tree(str(tmp_path / "b"), {"m/x.tmdl": "lineageTag: %s\n" % GUID_B})
    res = B.compare(a, b)
    assert len(res["by_reason"]["guid"]) == 1
    assert res["guid_tokens_differing"] == 1, "the token count must be REPORTED, not hidden"
    assert res["substantive"] == []


def test_a_timestamp_difference_is_not_substantive(tmp_path):
    a = _tree(str(tmp_path / "a"), {"r.json": '{"generated_at": "2026-09-03T21:06:10Z"}'})
    b = _tree(str(tmp_path / "b"), {"r.json": '{"generated_at": "2026-09-03T22:07:11Z"}'})
    res = B.compare(a, b)
    assert len(res["by_reason"]["timestamp"]) == 1
    assert res["substantive"] == []


def test_a_real_change_IS_substantive(tmp_path):
    """The discriminating case. Without it, a differ that classified everything as noise would pass."""
    a = _tree(str(tmp_path / "a"), {"v.json": '{"role": "Values", "kind": "Aggregation"}'})
    b = _tree(str(tmp_path / "b"), {"v.json": '{"role": "Values", "kind": "Column"}'})
    res = B.compare(a, b)
    assert res["by_reason"]["substantive"] == ["v.json"]
    assert res["substantive"] == ["v.json"]


def test_noise_does_not_mask_a_real_change_in_the_same_file(tmp_path):
    """A file carrying BOTH a GUID change and a content change must read as substantive.

    Attribution is cumulative and stops at the narrowest explanation, so a real change riding along
    with noise must not be absorbed into the noise bucket.
    """
    a = _tree(str(tmp_path / "a"), {"x.tmdl": "lineageTag: %s\nkind: Aggregation\n" % GUID_A})
    b = _tree(str(tmp_path / "b"), {"x.tmdl": "lineageTag: %s\nkind: Column\n" % GUID_B})
    res = B.compare(a, b)
    assert res["by_reason"]["substantive"] == ["x.tmdl"]
    assert res["by_reason"]["guid"] == []


def test_added_and_removed_are_substantive(tmp_path):
    a = _tree(str(tmp_path / "a"), {"keep.tmdl": "x\n", "gone.tmdl": "y\n"})
    b = _tree(str(tmp_path / "b"), {"keep.tmdl": "x\n", "new.tmdl": "z\n"})
    res = B.compare(a, b)
    assert res["added"] == ["new.tmdl"] and res["removed"] == ["gone.tmdl"]
    assert set(res["substantive"]) == {"new.tmdl", "gone.tmdl"}


# ------------------------------------------------------------------------ refusing a vacuous answer

def test_an_empty_tree_raises_rather_than_reporting_a_clean_comparison(tmp_path):
    """A comparison of nothing and a comparison that found nothing print identically.

    The first is the likely outcome of a wrong path, so it must be an error, not a clean result.
    """
    a = _tree(str(tmp_path / "a"), {"x.tmdl": "x\n"})
    empty = str(tmp_path / "empty")
    os.makedirs(empty, exist_ok=True)
    with pytest.raises(ValueError):
        B.compare(a, empty)
    with pytest.raises(ValueError):
        B.compare(empty, a)


def test_the_reason_partition_accounts_for_every_changed_file(tmp_path):
    """Each changed file lands in exactly one bucket, so a substantive change cannot hide in noise."""
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    _tree(a, {"root.tmdl": 'p("%s\\d")' % a, "g.tmdl": "lineageTag: %s" % GUID_A,
              "t.json": '{"at": "2026-09-03T21:06:10Z"}', "s.json": '{"k": 1}'})
    _tree(b, {"root.tmdl": 'p("%s\\d")' % b, "g.tmdl": "lineageTag: %s" % GUID_B,
              "t.json": '{"at": "2026-09-03T22:07:11Z"}', "s.json": '{"k": 2}'})
    res = B.compare(a, b)
    assert sum(len(v) for v in res["by_reason"].values()) == len(res["changed"]) == 4
    for reason in ("root", "guid", "timestamp", "substantive"):
        assert len(res["by_reason"][reason]) == 1, reason


# ------------------------------------------------------------------------------------ the CLI

def test_the_cli_exit_code_distinguishes_clean_from_differing_from_broken(tmp_path):
    """0 clean / 1 differs / 2 instrument error -- so a bad path can never read as a clean run."""
    a = _tree(str(tmp_path / "a"), {"x.json": '{"k": 1}'})
    same = _tree(str(tmp_path / "same"), {"x.json": '{"k": 1}'})
    diff = _tree(str(tmp_path / "diff"), {"x.json": '{"k": 2}'})
    assert B.main([a, same]) == 0
    assert B.main([a, diff]) == 1
    assert B.main([a, str(tmp_path / "nope")]) == 2


def test_strict_promotes_guid_churn_to_a_failure(tmp_path):
    a = _tree(str(tmp_path / "a"), {"x.tmdl": "lineageTag: %s" % GUID_A})
    b = _tree(str(tmp_path / "b"), {"x.tmdl": "lineageTag: %s" % GUID_B})
    assert B.main([a, b]) == 0
    assert B.main([a, b, "--strict"]) == 1
