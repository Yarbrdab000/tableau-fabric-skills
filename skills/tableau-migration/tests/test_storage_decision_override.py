"""`needs-storage-decision` was terminal on the batch path (issue #116).

The message correctly demanded a choice —

    the column schema IS readable -- what is missing is a storage-mode choice: default to a
    direct-to-source Import rebuild once a connection can be supplied, or opt in to land-to-Delta
    + DirectLake (never auto-selected)

— and there was nowhere to put the answer. The reporter traced both halves in code rather than
inferring them from behaviour, and both were exactly right:

* `select_storage_mode(descriptor)` took only a descriptor, and no `migrate_estate.py` flag carried
  a storage decision, so a caller who *had* made the choice had nowhere to put it;
* `FALLBACK_LAND_TO_DELTA` was **read** at `assemble_model.py` and **assigned by nothing**, so the
  documented DirectLake opt-in branched on a value the engine could not produce.

Measured at 14 of 38 workbooks — 37% of a real estate — ending with no model and no report.

What is deliberately NOT changed: refusing to AUTO-select DirectLake. Silently landing a customer's
data in Delta because a CSV path went stale is a far worse failure than stopping. Nothing here fires
without an explicit operator answer.

Verified end to end on the reported shape: `--accept-recommended-storage` takes a previously terminal
workbook 0/1 → 1/1, and `--storage-decision {"*": "DirectLake"}` writes a real 8-table landing plan
to `landing_plans/<wb>.landing_plan.json` — because a warning that promises a plan and writes nothing
is the same silent gap in a new place.
"""
import json

import pytest

import migrate_estate as M
import storage_mode as S


READABLE_BUT_UNDECIDED = {
    "connection_class": "textscan",
    "relations": [{"kind": "table", "name": "A", "columns": [{"remote_name": "x"}]},
                  {"kind": "table", "name": "B", "columns": []}],
    "unsupported_reasons": ["relation 'B' has no resolvable columns"],
}
SCHEMA_NOT_VISIBLE = {
    "connection_class": "textscan",
    "relations": [{"kind": "table", "name": "A", "columns": []}],
    "unsupported_reasons": [],
}
CONFIDENT = {
    "connection_class": "excel-direct",
    "relations": [{"kind": "table", "name": "A", "columns": [{"remote_name": "x"}]}],
    "unsupported_reasons": [],
}


def test_the_directlake_opt_in_is_reachable_at_all():
    """The whole second half of the bug: nothing could produce this value.

    ``assemble_model`` branches on ``FALLBACK_LAND_TO_DELTA`` to emit a landing plan, and no code
    path assigned it -- the documented opt-in was unreachable by construction.
    """
    decided = S.select_storage_mode(READABLE_BUT_UNDECIDED, storage_decision="DirectLake")
    assert decided["fallback"] == S.FALLBACK_LAND_TO_DELTA
    assert decided["mode"] is None       # a plan, never an auto-landed model
    assert decided["storage_decision_applied"] is True


def test_omitting_a_decision_is_exactly_todays_behaviour():
    """The default run must be byte-identical, or this feature is a regression for everyone else."""
    for d in (READABLE_BUT_UNDECIDED, SCHEMA_NOT_VISIBLE, CONFIDENT):
        assert S.select_storage_mode(d) == S._select_storage_mode(d)
        assert S.select_storage_mode(d, storage_decision=None) == S._select_storage_mode(d)


@pytest.mark.parametrize("answer,mode", [("Import", "Import"), ("DirectQuery", "DirectQuery")])
def test_a_supplied_mode_resolves_the_fallback(answer, mode):
    decided = S.select_storage_mode(READABLE_BUT_UNDECIDED, storage_decision=answer)
    assert decided["mode"] == mode
    assert decided["fallback"] is None
    assert "OPERATOR DECISION" in decided["rationale"]
    # the original demand survives, so the summary still says why a decision was needed
    assert "Direct-upstream rebuild not safe" in decided["rationale"]


def test_recommended_applies_the_engines_own_recommendation():
    natural = S.select_storage_mode(READABLE_BUT_UNDECIDED)
    decided = S.select_storage_mode(READABLE_BUT_UNDECIDED, storage_decision="recommended")
    assert decided["mode"] == natural["recommended_mode"] == "Import"


def test_a_confident_decision_is_never_second_guessed():
    """This seam supplies a MISSING decision; it does not override one the engine made."""
    for answer in ("DirectQuery", "DirectLake", "recommended"):
        decided = S.select_storage_mode(CONFIDENT, storage_decision=answer)
        assert decided["mode"] == "Import"
        assert decided["storage_decision_applied"] is False
        assert "did not need a storage decision" in decided["storage_decision_note"]


def test_an_unreadable_schema_refuses_every_answer():
    """'Import' is not a choice you can make about a model that cannot be typed at all."""
    for answer in ("Import", "DirectQuery", "DirectLake", "recommended"):
        decided = S.select_storage_mode(SCHEMA_NOT_VISIBLE, storage_decision=answer)
        assert decided["mode"] is None
        assert decided["fallback"] == S.FALLBACK_NEEDS_DECISION
        assert decided["storage_decision_applied"] is False
        assert "supply a connection" in decided["storage_decision_note"]


@pytest.mark.parametrize("written,canonical", [
    ("Import", "Import"), ("import", "Import"),
    ("DirectQuery", "DirectQuery"), ("direct-query", "DirectQuery"), ("live", "DirectQuery"),
    ("DirectLake", "DirectLake"), ("direct_lake", "DirectLake"), ("land-to-delta", "DirectLake"),
    ("recommended", "recommended"), ("default", "recommended"),
])
def test_an_answer_is_accepted_as_the_operator_would_write_it(written, canonical):
    assert S.normalize_storage_decision(written) == canonical


def test_no_answer_means_no_answer():
    assert S.normalize_storage_decision(None) is None
    assert S.normalize_storage_decision("") is None


def test_a_typo_raises_rather_than_reproducing_the_dead_end():
    """Silently ignoring a misspelt answer would recreate the very stop the flag exists to clear."""
    for bad in ("improt", "DirectLakehouse!", "yes"):
        with pytest.raises(ValueError):
            S.normalize_storage_decision(bad)


def _write(tmp_path, obj):
    p = tmp_path / "sd.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_the_file_maps_datasources_to_answers(tmp_path):
    got = M._load_storage_decisions(_write(tmp_path, {"Big Data Source": "DirectLake"}))
    assert M._storage_decision_for("Big Data Source", got) == "DirectLake"
    assert M._storage_decision_for("big data source", got) == "DirectLake"
    assert M._storage_decision_for("Something Else", got) is None


def test_the_star_key_is_a_default_and_an_explicit_entry_beats_it(tmp_path):
    got = M._load_storage_decisions(_write(tmp_path, {"*": "Import", "Odd One": "DirectLake"}))
    assert M._storage_decision_for("Anything", got) == "Import"
    assert M._storage_decision_for("Odd One", got) == "DirectLake"


def test_the_blanket_flag_is_exactly_a_recommended_default(tmp_path):
    assert M._load_storage_decisions(None, True) == {"*": "recommended"}
    # an explicit file entry still wins over the blanket opt-in
    got = M._load_storage_decisions(_write(tmp_path, {"Keep": "DirectLake"}), True)
    assert M._storage_decision_for("Keep", got) == "DirectLake"
    assert M._storage_decision_for("Other", got) == "recommended"


def test_supplying_nothing_loads_nothing():
    assert M._load_storage_decisions(None) is None
    assert M._load_storage_decisions(None, False) is None
    assert M._storage_decision_for("Anything", None) is None


def test_a_bad_file_fails_fast(tmp_path):
    with pytest.raises(ValueError):
        M._load_storage_decisions(str(tmp_path / "missing.json"))
    with pytest.raises(ValueError):
        M._load_storage_decisions(_write(tmp_path, ["not", "an", "object"]))
    with pytest.raises(ValueError):
        M._load_storage_decisions(_write(tmp_path, {"ds": "Improt"}))


def test_the_cli_exposes_both_routes():
    """Suggestion 1 (per-datasource) and 2 (blanket) from the issue; either alone closes it."""
    import argparse
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        M.main(["--help"])
    help_text = buf.getvalue()
    assert "--storage-decision" in help_text
    assert "--accept-recommended-storage" in help_text


def test_every_estate_entry_point_accepts_the_decisions():
    """A flag that stops halfway down the call chain is the same dead end one frame lower."""
    import inspect
    for fn in (M.migrate_estate, M.migrate_workbook, M._migrate_one_workbook,
               M._migrate_one_datasource, M._attach_workbook_pbip, M._build_datasource_pbip):
        assert "storage_decisions" in inspect.signature(fn).parameters, fn.__name__
    import assemble_model as A
    for fn in (A.migrate_datasource, A.assemble_import_model, A.migrate_tds_to_semantic_model):
        assert "storage_decision" in inspect.signature(fn).parameters, fn.__name__
