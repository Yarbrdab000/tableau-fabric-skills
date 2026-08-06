"""Tier-3 work order generator.

The document's whole value is that a reader can act on it WITHOUT re-deriving anything, so the tests
here are mostly about honesty rather than formatting: a path that is claimed exactly must be exact, a
visual called verified must actually be unflagged, and a batch must represent one real shared fix.
A pretty document that is wrong on any of those is worse than no document, because it is believed.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import work_order as W  # noqa: E402


# ---------------------------------------------------------------------------------------------
# mechanism batching
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("reason,expected", [
    ("categorical mark colours deferred (the line visual type does not carry a per-member mark "
     "colour)", "colour"),
    ("nested formula table calc routed to review ([* CY CSAT]: unsupported character '=')",
     "nested-table-calc"),
    ("view-only quick table calc routed to review (ordering scope 'CellInPane' does not decompose)",
     "table-calc-scope"),
    ("filter card federated.0gcnwq804g9e3p0zw2dho0bwawms.none:*Global Team:nk resolved to no model "
     "field (slicer not rebuilt)", "filter-card-binding"),
    ("1 author-hidden zone(s) not rebuilt (Tableau 'hidden-by-user' show/hide toggle)",
     "hidden-zone"),
    ("combo visual has no usable field bindings (skipped)", "unrebuilt-visual"),
    ("mark class 'Circle' / shelf layout not supported -> no visual emitted", "unrebuilt-visual"),
    ("unsupported derivation 'Attribute' on '*Analyst Tenure' (skipped)", "field-binding"),
])
def test_engine_phrases_classify_to_a_mechanism(reason, expected):
    """Each pattern matches a phrase the ENGINE emits for a whole class, not one workbook's wording.

    These strings were taken verbatim from real report.json output. If the engine rewords one, this
    test fails rather than the document silently regressing to one-batch-per-item.
    """
    assert W.mechanism_of({"category": "other", "reason": reason}) == expected


def test_an_unrecognised_reason_is_left_standalone_not_force_fitted():
    """A batch claims "these share one fix". A wrong batch is worse than no batch, because it invites
    the reader to apply one procedure to something it does not fit."""
    assert W.mechanism_of({"category": "other", "reason": "something nobody has seen before"}) is None


def test_batching_collapses_a_repeated_mechanism_into_one_batch():
    items = [
        {"category": "other", "reason": "nested formula table calc routed to review ([A]: x)",
         "worksheet": "S1", "visual": "v1", "severity": "medium"},
        {"category": "other", "reason": "nested formula table calc routed to review ([B]: y)",
         "worksheet": "S2", "visual": "v2", "severity": "medium"},
        {"category": "other", "reason": "nested formula table calc routed to review ([C]: z)",
         "worksheet": "S3", "visual": "v3", "severity": "medium"},
    ]
    batches = W.batch_items(items, {"visuals": []}, None)
    assert len(batches) == 1
    assert len(batches[0]["items"]) == 3


def test_the_same_finding_stated_two_ways_is_deduplicated():
    """The engine states one finding both long-form and short-form. Keying dedup on the PROSE would
    keep both and leave the reader to notice the duplication."""
    items = [
        {"category": "other", "visual": "v1", "worksheet": "S", "visual_type": "lineChart",
         "reason": "categorical mark colours deferred (the line visual type does not carry a "
                   "per-member mark colour); the visual is emitted with theme colours"},
        {"category": "other", "visual": "v1", "worksheet": "S", "visual_type": "lineChart",
         "reason": "the line visual type does not carry a per-member mark colour"},
    ]
    batches = W.batch_items(items, {"visuals": []}, None)
    assert sum(len(b["items"]) for b in batches) == 1


def test_two_different_visuals_sharing_a_mechanism_are_kept_separate():
    """Dedup must not swallow real work: same mechanism, different visual = two things to fix."""
    items = [
        {"category": "other", "visual": "v1", "worksheet": "A", "visual_type": "lineChart",
         "reason": "categorical mark colours deferred"},
        {"category": "other", "visual": "v2", "worksheet": "B", "visual_type": "lineChart",
         "reason": "categorical mark colours deferred"},
    ]
    batches = W.batch_items(items, {"visuals": []}, None)
    assert sum(len(b["items"]) for b in batches) == 2


# ---------------------------------------------------------------------------------------------
# localisation -- the claim that costs the most if it is wrong
# ---------------------------------------------------------------------------------------------
def _page(*visuals):
    return {"visuals": [{"name": n, "type": t, "path": p, "objects": []}
                        for n, t, p in visuals]}


def test_a_recorded_visual_name_resolves_to_exactly_one_file():
    page = _page(("v1", "lineChart", "/x/v1/visual.json"), ("v2", "lineChart", "/x/v2/visual.json"))
    by_name, by_type = W._visual_lookup(page)
    paths, exact = W.locate({"visual": "v2", "visual_type": "lineChart"}, by_name, by_type)
    assert exact is True
    assert paths == ["/x/v2/visual.json"]


def test_no_recorded_name_falls_back_to_type_and_says_it_is_not_exact():
    """The fallback narrows to a candidate SET. Presenting that as the answer would send the reader
    to edit visuals that are not broken, so ``exact`` must be False and the renderer must say so."""
    page = _page(("v1", "lineChart", "/x/v1/visual.json"), ("v2", "lineChart", "/x/v2/visual.json"))
    by_name, by_type = W._visual_lookup(page)
    paths, exact = W.locate({"visual": None, "visual_type": "lineChart"}, by_name, by_type)
    assert exact is False
    assert len(paths) == 2


def test_a_stale_visual_name_does_not_silently_resolve_to_the_wrong_file():
    """A name recorded in the worklist but absent from the emitted PBIR must NOT fall through to
    "some visual of the same type" while still claiming to be exact."""
    page = _page(("v1", "lineChart", "/x/v1/visual.json"))
    by_name, by_type = W._visual_lookup(page)
    paths, exact = W.locate({"visual": "GONE", "visual_type": "lineChart"}, by_name, by_type)
    assert exact is False


# ---------------------------------------------------------------------------------------------
# PART B -- the only subtractive section, so the only one that can waste a reader's trust
# ---------------------------------------------------------------------------------------------
def test_a_flagged_visual_is_never_called_verified():
    page = _page(("v1", "lineChart", "/x/v1"), ("v2", "barChart", "/x/v2"))
    rows = W.verified_correct(page, flagged_names={"v1"}, ambiguous_types=set())
    assert [r["visual"] for r in rows] == ["v2"]


def test_an_ambiguous_type_disqualifies_every_visual_of_that_type():
    """If an item only narrowed to "one of the three lineCharts", we cannot prove WHICH is clean, so
    none of them may be listed as verified."""
    page = _page(("v1", "lineChart", "/x/v1"), ("v2", "lineChart", "/x/v2"),
                 ("v3", "barChart", "/x/v3"))
    rows = W.verified_correct(page, flagged_names=set(), ambiguous_types={"linechart"})
    assert [r["visual"] for r in rows] == ["v3"]


def test_exact_localisation_lets_a_sibling_of_the_same_type_stay_verified():
    """The payoff of exact naming: flagging v1 no longer taints v2 just for sharing its type."""
    page = _page(("v1", "lineChart", "/x/v1"), ("v2", "lineChart", "/x/v2"))
    rows = W.verified_correct(page, flagged_names={"v1"}, ambiguous_types=set())
    assert [r["visual"] for r in rows] == ["v2"]


# ---------------------------------------------------------------------------------------------
# corpus handling
# ---------------------------------------------------------------------------------------------
def test_a_corpus_field_that_is_a_string_is_not_iterated_character_by_character():
    """Some cards store ``making_it_props`` as a STRING. Iterating it yields one bullet per character
    ("- P", "- a", "- l") which still renders as plausible markdown, so it would ship silently."""
    assert W._as_list("valueAxis NORMAL") == ["valueAxis NORMAL"]
    assert W._as_list(["a", "b"]) == ["a", "b"]
    assert W._as_list(None) == []


def test_a_card_is_dropped_when_it_does_not_speak_to_the_mechanism():
    card = {"props": ["valueAxis.labelDisplayUnits = '0'"], "gotchas": []}
    assert W.card_matches(card, "colour") is False


def test_a_card_is_kept_when_it_does_speak_to_the_mechanism():
    card = {"props": ["dataPoint.defaultColor.solid.color = '#d8504c'"], "gotchas": []}
    assert W.card_matches(card, "colour") is True


def test_an_unknown_mechanism_keeps_the_card_rather_than_emitting_nothing():
    """Fail open: a missing card reads as "the corpus has nothing to say", which is a lie of
    omission the reader cannot detect."""
    assert W.card_matches({"props": ["anything"]}, "some-new-mechanism") is True


# ---------------------------------------------------------------------------------------------
# text handling
# ---------------------------------------------------------------------------------------------
def test_clipping_lands_on_a_word_boundary_and_marks_the_cut():
    out = W._clip("alpha beta gamma delta", 14)
    assert out == "alpha beta ..."
    assert not out.replace(" ...", "").endswith("gam")


def test_short_text_is_returned_unchanged_and_unmarked():
    assert W._clip("alpha beta", 80) == "alpha beta"


def test_the_generic_engine_remediation_is_recognised():
    """It is suppressed only where a real batch procedure replaces it."""
    assert W._GENERIC_REMEDIATION.match("Review this item against the source and remediate.")
    assert not W._GENERIC_REMEDIATION.match(
        "Provide field bindings so the table can be rebuilt with real columns.")


# ---------------------------------------------------------------------------------------------
# whole-document invariants
# ---------------------------------------------------------------------------------------------
def _run_dir(tmp_path):
    out = tmp_path / "out"
    (out / "reference_images").mkdir(parents=True)
    report = {"workbooks": [{
        "workbook": "WB",
        "remediation_worklist": {"items": [
            {"category": "other", "severity": "medium", "visual": "v1", "visual_type": "lineChart",
             "worksheet": "S1", "page_display": "D1", "reason": "categorical mark colours deferred",
             "remediation": "Review this item against the source and remediate."},
        ]},
    }]}
    (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (out / "reference_images" / "manifest.json").write_text(json.dumps({
        "schema": "reference_images/1", "mode": "capture",
        "images": [{"dashboard": "D1", "png": "D1.png", "confidence": "content"}],
        "missing": [], "warnings": [],
    }), encoding="utf-8")
    return str(tmp_path)


def test_a_generated_document_states_no_time_or_step_budget(tmp_path):
    """A budget buys speed by shipping a worse report -- the same failure class as a false PASS.
    Turns are an OUTPUT we measure, never an input we impose on the reader."""
    run = _run_dir(tmp_path)
    written = W.generate(run, corpus_root=None)
    text = open(written[0][1], encoding="utf-8").read()
    assert "no time limit and no step budget" in text.lower()


def test_a_generated_document_tells_the_reader_the_list_is_not_the_finish_line(tmp_path):
    """The anchoring counterweight: a document saying "these N are wrong" can stop a reader at N."""
    run = _run_dir(tmp_path)
    text = open(W.generate(run, corpus_root=None)[0][1], encoding="utf-8").read()
    assert "START of your judgement" in text


def test_the_batch_procedure_replaces_the_engines_generic_line(tmp_path):
    run = _run_dir(tmp_path)
    text = open(W.generate(run, corpus_root=None)[0][1], encoding="utf-8").read()
    assert "How to fix this class" in text
    assert "Review this item against the source" not in text


def test_generation_survives_a_run_with_no_reference_image(tmp_path):
    """Reference images are additive by charter -- their absence must degrade, never fail."""
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "report.json").write_text(json.dumps({"workbooks": [{
        "workbook": "WB", "remediation_worklist": {"items": []}}]}), encoding="utf-8")
    assert W.generate(str(tmp_path), corpus_root=None) is not None
