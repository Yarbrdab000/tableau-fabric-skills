"""Tier-3 work order generator.

The document earns its place only if every line is something the reader CANNOT SEE. Measured
2026-08-06: the same downstream agent reached near-perfect fidelity in 1h40m working from the
reference image alone, but took 3h and produced a WORSE report when handed a comprehensive "audit"
work order. So these tests are mostly about restraint -- that the document keeps the few facts the
engine uniquely knows, drops everything visible, and never asserts a visual is correct.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import work_order as W  # noqa: E402


# ---------------------------------------------------------------------------------------------
# selection -- what earns a place in the document
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("reason", [
    "nested formula table calc routed to review ([* CY CSAT]: unsupported character '='); the visual "
    "is emitted with the base value only",
    "view-only quick table calc routed to review (ordering scope 'CellInPane' does not decompose)",
])
def test_a_plausible_but_wrong_number_is_kept(reason):
    """The worst class in the document: something rendered, it looks fine, the value is not the
    value. Nothing about it announces itself, so it cannot be left to the reader's eye."""
    _, wrong = W.partition_items([{"reason": reason}])
    assert len(wrong) == 1


@pytest.mark.parametrize("reason", [
    "combo visual has no usable field bindings (skipped)",
    "mark class 'Circle' / shelf layout not supported -> no visual emitted",
    "filter card federated.abc.none:*Global Team:nk resolved to no model field (slicer not rebuilt)",
    "1 author-hidden zone(s) not rebuilt (Tableau 'hidden-by-user' show/hide toggle)",
])
def test_a_construct_that_was_never_rebuilt_is_kept(reason):
    """The gap is visible; WHAT is missing and why the deterministic route refused is not."""
    missing, _ = W.partition_items([{"reason": reason}])
    assert len(missing) == 1


@pytest.mark.parametrize("reason", [
    "categorical mark colours deferred (the line visual type does not carry a per-member mark colour)",
    "the table visual type does not carry a per-member mark colour",
    "axis title not applied",
    "chart type used a fallback approximation",
])
def test_anything_visible_in_the_image_is_dropped(reason):
    """The reader takes colour, axis titles and chart shape off the reference image faster and more
    accurately than we can describe them. Listing these is what made the previous version both
    slower and worse, so their exclusion is a behaviour worth locking down."""
    missing, wrong = W.partition_items([{"reason": reason}])
    assert (missing, wrong) == ([], [])


def test_the_same_finding_stated_twice_is_deduplicated():
    items = [
        {"worksheet": "S", "visual": "v1", "reason": "combo visual has no usable field bindings"},
        {"worksheet": "S", "visual": "v1", "reason": "combo visual has no usable field bindings"},
    ]
    missing, _ = W.partition_items(items)
    assert len(missing) == 1


# ---------------------------------------------------------------------------------------------
# PART B must never come back
# ---------------------------------------------------------------------------------------------
def test_the_module_makes_no_verified_correct_claim(tmp_path):
    """The regression that mattered. The previous version derived "checked and CORRECT -- do not
    re-audit" from "the worklist did not flag it", but the worklist records TRANSLATION failures and
    has no opinion on whether a visual LOOKS right. The correlation even runs backwards: a visual
    that translated cleanly is the one still wearing the default theme. That section named the three
    visuals the solo agent rebuilt, and telling it not to touch them is how the document made it
    worse."""
    assert not hasattr(W, "verified_correct")
    text = _doc(tmp_path)
    assert "do not re-audit" not in text
    assert "checked and CORRECT" not in text
    # The one visual in the fixture translated cleanly apart from a colour deferral, so the old
    # version would have listed it as verified. Nothing may vouch for it now.
    assert "v1" not in text.split("## 1.")[0]


# ---------------------------------------------------------------------------------------------
# stubs -- the highest-value facts, and the join that makes them actionable
# ---------------------------------------------------------------------------------------------
def test_stub_requests_are_read_from_the_handoff():
    wb = {"model_translation_handoff": {"requests": [{"name": "* CY CSAT", "formula": "TOTAL(...)"}]}}
    assert [s["name"] for s in W.stub_requests(wb)] == ["* CY CSAT"]


def test_a_workbook_with_no_handoff_yields_no_stubs():
    assert W.stub_requests({}) == []


def test_a_stub_no_visual_projects_sorts_below_one_that_is_rendered(tmp_path):
    """A stub nothing displays cannot change the picture. Leading with it would put the reader's
    first action on the one item that provably alters nothing."""
    order = _build(tmp_path)
    used = [bool(s["used_by"]) for s in order["stubs"]]
    assert used == sorted(used, reverse=True)


def test_the_stub_to_visual_join_names_the_tile_that_is_lying(tmp_path):
    """Converts "8 measures failed to translate" (a fact about a model, which the reader cannot act
    on) into "THIS card shows a placeholder" (a fact about the picture, which they can)."""
    order = _build(tmp_path)
    lying = [s for s in order["stubs"] if s["used_by"]]
    assert lying and lying[0]["used_by"][0]["name"] == "v1"


# ---------------------------------------------------------------------------------------------
# item routing
# ---------------------------------------------------------------------------------------------
def test_a_dashboard_scope_item_stamped_with_the_page_id_is_not_dropped(tmp_path):
    """The engine stamps ``page_display`` with EITHER the dashboard name or the emitted PBIR page id
    depending on which layer raised the item. Accepting one form silently dropped five real findings,
    and a silent drop is indistinguishable from "nothing was wrong"."""
    order = _build(tmp_path)
    assert any("filter card" in (i.get("reason") or "") for i in order["not_rebuilt"])


def test_only_an_exact_visual_match_is_printed_as_a_path():
    """A type fallback narrows to a candidate SET. Printing it as the answer sends the reader to edit
    visuals that are not broken."""
    page = {"visuals": [{"name": "v1", "type": "lineChart", "path": "/x/v1", "objects": []},
                        {"name": "v2", "type": "lineChart", "path": "/x/v2", "objects": []}]}
    by_name, by_type = W._visual_lookup(page)
    assert W.locate({"visual": "v2", "visual_type": "lineChart"}, by_name, by_type) == (["/x/v2"], True)
    _, exact = W.locate({"visual": None, "visual_type": "lineChart"}, by_name, by_type)
    assert exact is False


def test_a_stale_visual_name_does_not_resolve_to_some_other_file():
    page = {"visuals": [{"name": "v1", "type": "lineChart", "path": "/x/v1", "objects": []}]}
    by_name, by_type = W._visual_lookup(page)
    _, exact = W.locate({"visual": "GONE", "visual_type": "lineChart"}, by_name, by_type)
    assert exact is False


# ---------------------------------------------------------------------------------------------
# text handling
# ---------------------------------------------------------------------------------------------
def test_clipping_lands_on_a_word_boundary_and_marks_the_cut():
    """A hard character slice ends instructions mid-word, which reads as a CORRUPTED document rather
    than an abbreviated one -- and a reader who cannot tell has to go find the full text."""
    assert W._clip("alpha beta gamma delta", 14) == "alpha beta ..."
    assert W._clip("alpha beta", 80) == "alpha beta"


def test_a_corpus_string_is_not_iterated_character_by_character():
    """Some cards store the field as a STRING; iterating yields one bullet per character, which
    renders as plausible markdown rather than an error, so it would ship silently."""
    assert W._as_list("valueAxis NORMAL") == ["valueAxis NORMAL"]
    assert W._as_list(None) == []


# ---------------------------------------------------------------------------------------------
# whole-document invariants
# ---------------------------------------------------------------------------------------------
def _run_dir(tmp_path):
    out = tmp_path / "out"
    vis = (out / "pbip" / "WB" / "WB.Report" / "definition" / "pages" / "p1" / "visuals")
    (vis / "v1").mkdir(parents=True)
    (out / "reference_images").mkdir(parents=True)
    (out / "pbip" / "WB" / "WB.pbip").write_text("{}", encoding="utf-8")
    (vis.parent / "page.json").write_text(
        json.dumps({"name": "p1", "displayName": "D1"}), encoding="utf-8")
    (vis / "v1" / "visual.json").write_text(json.dumps({
        "visual": {"visualType": "multiRowCard",
                   "query": {"queryState": {"Values": {"projections": [
                       {"queryRef": "_Measures.* CY CSAT"}]}}}}}), encoding="utf-8")
    (out / "report.json").write_text(json.dumps({"workbooks": [{
        "name": "WB",
        "pbip_folder": str(out / "pbip" / "WB" / "WB.pbip"),
        "model_translation_handoff": {"requests": [
            {"name": "* CY CSAT", "formula": "TOTAL(COUNTD(...))",
             "fallback_reason": "unsupported function TOTAL", "role": "measure",
             "fields": [{"table": "Sheet2", "column": "Fiscal_Year"}]},
            {"name": "*Max Date", "formula": "DATEPARSE(...)",
             "fallback_reason": "unsupported function DATEPARSE", "role": "measure"},
        ]},
        "remediation_worklist": {"items": [
            {"reason": "categorical mark colours deferred (theme colours used)",
             "worksheet": "S1", "visual": "v1", "visual_type": "multiRowCard",
             "page_display": "D1"},
            {"reason": "filter card federated.abc.none:Team:nk resolved to no model field "
                       "(slicer not rebuilt)", "page_display": "p1"},
            {"reason": "nested formula table calc routed to review ([* CY CSAT]); the visual is "
                       "emitted with the base value only",
             "worksheet": "S1", "visual": "v1", "visual_type": "multiRowCard",
             "page_display": "D1"},
        ]},
    }]}), encoding="utf-8")
    (out / "reference_images" / "manifest.json").write_text(json.dumps({
        "schema": "reference_images/1", "mode": "capture",
        "declared_dashboards": ["D1"],
        "images": [{"dashboard": "D1", "png": "D1.png", "confidence": "content"}],
        "missing": [], "warnings": [],
    }), encoding="utf-8")
    return str(tmp_path)


def _build(tmp_path):
    run = _run_dir(tmp_path)
    data = W.load_run(run)
    wb = W.workbook_entries(data["report"])[0]
    pages = W.index_visuals(W.find_report_dir(wb, run))
    return W.build_order(wb, "D1", pages, data["references"], None, run)


def _doc(tmp_path):
    written = W.generate(_run_dir(tmp_path), corpus_root=None)
    assert written, "generate wrote nothing"
    return open(written[0][1], encoding="utf-8").read()


def test_the_document_says_the_image_is_the_specification(tmp_path):
    text = _doc(tmp_path)
    assert "not an audit" in text
    assert "IMPOSSIBLE TO SEE" in text


def test_the_document_denies_being_a_definition_of_done(tmp_path):
    """The anchoring counterweight. A list of N facts will otherwise be read as "fix these N and
    stop", which caps the result at whatever the engine happened to detect."""
    assert "Not when this document is exhausted" in _doc(tmp_path)


def test_the_document_states_no_time_or_step_budget(tmp_path):
    """A budget buys speed by shipping a worse report -- the same failure class as a false PASS."""
    assert "no time limit and no step budget" in _doc(tmp_path).lower()


def test_the_document_warns_that_absence_is_not_approval(tmp_path):
    """Everything the previous version got wrong, stated as its opposite."""
    assert "Nothing here is a claim that any other part of the report is correct" in _doc(tmp_path)


def test_a_visible_only_finding_never_reaches_the_document(tmp_path):
    assert "mark colours deferred" not in _doc(tmp_path)


def test_the_reference_image_is_given_as_an_absolute_path(tmp_path):
    """The one file the reader must open first. A bare filename makes them hunt for it, which is the
    exact cost this document exists to remove."""
    assert os.path.join("reference_images", "D1.png") in _doc(tmp_path)


def test_generation_survives_a_run_with_no_reference_image(tmp_path):
    """Reference images are additive by charter -- absence must degrade, never fail."""
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "report.json").write_text(json.dumps({"workbooks": [{
        "name": "WB", "remediation_worklist": {"items": [
            {"reason": "mark class 'Circle' not supported -> no visual emitted",
             "page_display": "D9", "worksheet": "S"}]}}]}), encoding="utf-8")
    written = W.generate(str(tmp_path), corpus_root=None)
    assert written
    assert "No reference image was captured" in open(written[0][1], encoding="utf-8").read()
