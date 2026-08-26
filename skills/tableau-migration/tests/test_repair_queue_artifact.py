"""The stub repair work order is its own artifact, and its guidance is hoisted (#170).

WHY THIS EXISTS. The repair payload already existed, in
``model_translation_handoff.requests[]``. Reaching it meant knowing which of ``report.json``'s 12
top-level keys held it, which workbook owned the stub, and which of **two same-length sibling
lists** carried the fields needed to fix it -- because ``needs_review[]`` and ``requests[]``
describe the same calcs and only the second has ``formula``, ``fields`` and ``target_table``. A
reader who found the first got a plausible, complete-looking list of exactly the right
calculations and could satisfy a "record every stub" requirement **while repairing none**.

Two properties are pinned here and they fail in opposite directions:

* **The queue exists as a file**, so it is reachable by name rather than by path-through-a-shape.
* **``report.json`` is UNCHANGED**, so every existing consumer of the inline ``category_guidance``
  keeps working. A "de-duplication" that removed the inline copy would be a schema removal, and
  this repo's report schema is additive-only.

The hoist itself needs a word, because it looks like the misplaced-disclosure defect this repo
keeps finding and is its inverse. ``category_guidance`` is a CLASS-level document -- measured on
the 34-workbook corpus there is exactly **one distinct string per category**, 7 categories,
4,481 chars in total, repeated to **44,407** across 69 requests. Duplicated inline it does not
put guidance where the reader is; it puts ~900 characters of text identical to the block above it
*between* every pair of genuinely per-object records. Hoisting it to a map the request's own
``category`` keys into moves the reader toward the payload, not away from it.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from migrate_estate import _pending_gates, _write_repair_queue  # noqa: E402

_G = {"dax_language_gap": "GUIDANCE-A" * 40, "unresolved_reference": "GUIDANCE-B" * 40}


def _req(name, cat):
    return {"name": name, "role": "measure", "target_table": "_Measures",
            "formula": "IF x THEN 1 END", "fields": [{"table": "T", "column": "c"}],
            "fallback_reason": "unsupported function X", "category": cat,
            "category_guidance": _G[cat], "blocked_by": [], "has_suggestion": False}


def _report():
    return {
        "generated_at": "2026-01-01T00:00:00Z",
        "workbooks": [{"name": "WB", "pbip_folder": "pbip/WB",
                       "model_translation_handoff": {"requests": [
                           _req("A", "dax_language_gap"),
                           _req("B", "dax_language_gap"),
                           _req("C", "unresolved_reference")]}}],
        "datasources": [{"name": "DS",
                         "translation_handoff": {"requests": [_req("D", "dax_language_gap")]}}],
    }


def test_the_queue_is_written_as_its_own_file(tmp_path):
    rep = _report()
    _write_repair_queue(rep, str(tmp_path))
    path = tmp_path / "repair-queue.json"
    assert path.exists(), "the work order must be reachable by NAME, not by path through a shape"
    assert rep["repair_queue"]["status"] == "written"
    assert rep["repair_queue"]["path"] == "repair-queue.json"


def test_guidance_is_emitted_once_per_category_not_once_per_request(tmp_path):
    rep = _report()
    _write_repair_queue(rep, str(tmp_path))
    q = json.loads((tmp_path / "repair-queue.json").read_text(encoding="utf-8"))
    assert q["summary"]["requests"] == 4
    assert sorted(q["guidance"]) == ["dax_language_gap", "unresolved_reference"]
    # 3 of the 4 requests share one category; inline that is 3 copies, hoisted it is 1.
    assert rep["repair_queue"]["guidance_bytes_inline"] == sum(len(_G[c]) for c in
                                                               ["dax_language_gap"] * 3 +
                                                               ["unresolved_reference"])
    assert rep["repair_queue"]["guidance_bytes_deduplicated"] == sum(len(v) for v in _G.values())


def test_every_request_key_resolves_in_the_guidance_map(tmp_path):
    """The hoist is only safe if the key is never dangling. A request whose ``category`` is absent
    from the map has had its guidance REMOVED rather than relocated."""
    rep = _report()
    _write_repair_queue(rep, str(tmp_path))
    q = json.loads((tmp_path / "repair-queue.json").read_text(encoding="utf-8"))
    reqs = [r for s in q["subjects"] for r in s["requests"]]
    assert reqs, "vacuous -- no requests in the emitted queue"
    dangling = [r["name"] for r in reqs if r.get("category") not in q["guidance"]]
    assert not dangling, "guidance key does not resolve for %s" % dangling


def test_each_request_keeps_the_fields_that_make_it_repairable(tmp_path):
    """The whole point of preferring ``requests[]`` over ``needs_review[]``. If the hoist dropped
    these the file would be the useless list wearing the useful list's name."""
    rep = _report()
    _write_repair_queue(rep, str(tmp_path))
    q = json.loads((tmp_path / "repair-queue.json").read_text(encoding="utf-8"))
    for r in [r for s in q["subjects"] for r in s["requests"]]:
        for k in ("formula", "fields", "target_table", "fallback_reason", "category"):
            assert k in r, "%s lost %r" % (r["name"], k)
        assert "category_guidance" not in r, "inline copy leaked back into the hoisted file"


def test_the_original_handoff_is_untouched(tmp_path):
    """ADDITIVE. Removing the inline guidance from report.json would be a schema removal and would
    break any consumer reading it there -- including the reporter's own wrapper."""
    rep = _report()
    before = json.dumps(rep["workbooks"], sort_keys=True)
    _write_repair_queue(rep, str(tmp_path))
    assert json.dumps(rep["workbooks"], sort_keys=True) == before
    inline = [r for w in rep["workbooks"]
              for r in w["model_translation_handoff"]["requests"] if r.get("category_guidance")]
    assert len(inline) == 3, "the original slice lost its inline guidance"


def test_both_workbook_and_datasource_stubs_reach_the_queue(tmp_path):
    """Two attachment sites use two different key names (``model_translation_handoff`` vs
    ``translation_handoff``). Covering only one would silently halve the queue."""
    rep = _report()
    _write_repair_queue(rep, str(tmp_path))
    q = json.loads((tmp_path / "repair-queue.json").read_text(encoding="utf-8"))
    assert sorted(s["kind"] for s in q["subjects"]) == ["datasource", "workbook"]
    assert sorted(r["name"] for s in q["subjects"] for r in s["requests"]) == ["A", "B", "C", "D"]


def test_no_file_and_no_key_when_there_is_nothing_to_repair(tmp_path):
    """A clean run's artifacts stay exactly as they were."""
    rep = {"generated_at": "t", "workbooks": [{"name": "WB"}], "datasources": []}
    _write_repair_queue(rep, str(tmp_path))
    assert not (tmp_path / "repair-queue.json").exists()
    assert "repair_queue" not in rep


def test_a_write_failure_is_recorded_and_never_raised(tmp_path):
    """A repair aid must not fail a migration that otherwise built."""
    rep = _report()
    blocked = tmp_path / "nope"          # a FILE where the output dir should be
    blocked.write_text("x", encoding="utf-8")
    _write_repair_queue(rep, str(blocked))
    assert rep["repair_queue"]["status"] == "error"


def test_the_second_compiler_gate_names_the_work_order():
    """Placement. The gate is where someone is ASKED to repair the stubs, so it is where the path
    to the work order has to be -- otherwise the file exists and is never found, which is the same
    outcome as not writing it."""
    gates = _pending_gates({"needs_review_total": 7})
    gate = [g for g in gates if g["gate"] == "second_compiler"]
    assert gate, "vacuous -- the second-compiler gate did not fire"
    assert gate[0]["work_order"] == "repair-queue.json"
    assert "repair-queue.json" in gate[0]["offer"]
