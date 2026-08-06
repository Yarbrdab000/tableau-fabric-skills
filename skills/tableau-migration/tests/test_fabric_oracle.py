"""Tests for the ``fabric_oracle`` seam (#96) -- the wiring that makes the Tier-1 loop reachable.

The load-bearing property is that verification can only ever move a record toward the truth: with no
oracle, or no ground truth, or a broken executor, every record must read ``not-evaluated`` -- never
``verified``. A false ``verified`` is the worst outcome in this system, so most of what is locked
below is the *refusal* to claim one.
"""
import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

import fabric_oracle as FO  # noqa: E402
import translation_reconcile as TR  # noqa: E402


# --- (a) normalize_result delegates to the ONE parser --------------------------------------------
def test_normalize_reads_every_contract_legal_shape():
    assert FO.normalize_result(7) == {"value": 7, "error": None}
    assert FO.normalize_result({"value": 7}) == {"value": 7, "error": None}
    assert FO.normalize_result({"rows": [{"[value]": 7}]}) == {"value": 7, "error": None}
    assert FO.normalize_result([{"[value]": 7}]) == {"value": 7, "error": None}
    env = {"results": [{"tables": [{"rows": [{"[value]": 7}]}]}]}
    assert FO.normalize_result(env) == {"value": 7, "error": None}


def test_normalize_surfaces_an_error_without_a_value():
    out = FO.normalize_result({"error": "credentials required"})
    assert out["value"] is None
    assert "credentials" in out["error"]


def test_normalize_never_raises_on_junk():
    out = FO.normalize_result(object())
    assert out["value"] is None and out["error"]


# --- (b) conforms(): an executor self-certifies offline -------------------------------------------
def test_conforms_accepts_a_minimal_legal_oracle():
    res = FO.conforms(lambda _dax: {"value": 1})
    assert res["ok"] is True
    assert res["failures"] == []
    assert res["checks"]["does_not_raise"] is True


def test_conforms_rejects_a_non_callable():
    res = FO.conforms("not a function")
    assert res["ok"] is False
    assert res["checks"]["callable"] is False


def test_conforms_rejects_an_oracle_that_raises():
    def boom(_dax):
        raise RuntimeError("connection refused")

    res = FO.conforms(boom)
    assert res["ok"] is False
    assert res["checks"]["does_not_raise"] is False
    assert "connection refused" in res["failures"][0]


def test_conforms_rejects_a_wrong_signature():
    res = FO.conforms(lambda: 1)
    assert res["ok"] is False
    assert res["checks"]["accepts_dax_string"] is False


def test_conforms_reports_an_erroring_probe_as_not_certified_but_legal():
    res = FO.conforms(lambda _dax: {"error": "model not loaded"})
    assert res["ok"] is False
    assert res["checks"]["readable_result"] is True     # it reported honestly...
    assert res["checks"]["probe_answered"] is False     # ...but proved nothing


def test_conforms_rejects_an_unreadable_shape():
    res = FO.conforms(lambda _dax: object())
    assert res["ok"] is False
    assert res["checks"]["readable_result"] is False


# --- (c) subprocess adapter -----------------------------------------------------------------------
def _runner(code=0, out='{"value": 42}', err=""):
    seen = {}

    def run(cmd, payload, timeout):
        seen["cmd"], seen["payload"], seen["timeout"] = cmd, payload, timeout
        return code, out, err

    return run, seen


def test_subprocess_oracle_passes_the_query_and_reads_json():
    run, seen = _runner()
    oracle = FO.subprocess_oracle(["my-exec"], runner=run, timeout=99)
    assert FO.normalize_result(oracle("EVALUATE ROW(\"value\", 1)"))["value"] == 42
    assert seen["cmd"] == ["my-exec"]
    assert "EVALUATE" in seen["payload"]
    assert seen["timeout"] == 99


def test_subprocess_oracle_turns_failures_into_errors_not_exceptions():
    for run, expect in (
        (_runner(code=3, out="", err="boom")[0], "exited 3"),
        (_runner(out="")[0], "no output"),
        (_runner(out="not json")[0], "not JSON"),
    ):
        oracle = FO.subprocess_oracle("cmd", runner=run)
        res = oracle("EVALUATE 1")
        assert res["error"] and expect in res["error"]


def test_subprocess_oracle_survives_a_runner_that_raises():
    def run(*_a):
        raise OSError("no such file")

    res = FO.subprocess_oracle("cmd", runner=run)("EVALUATE 1")
    assert "no such file" in res["error"]


# --- (d) persistent adapter (an expensive-startup executor stays alive) ---------------------------
class _FakeStream:
    def __init__(self, lines=None):
        self.lines = list(lines or [])
        self.written = []
        self.closed = False

    def write(self, s):
        self.written.append(s)

    def flush(self):
        pass

    def readline(self):
        return self.lines.pop(0) if self.lines else ""

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, lines):
        self.stdin = _FakeStream()
        self.stdout = _FakeStream(lines)
        self.stderr = _FakeStream()
        self.waited = False
        self.killed = False

    def wait(self, timeout=None):
        self.waited = True

    def kill(self):
        self.killed = True


def test_persistent_oracle_reuses_one_process_across_queries():
    proc = _FakeProc(['{"value": 1}\n', '{"value": 2}\n'])
    spawns = []

    def spawn(cmd):
        spawns.append(cmd)
        return proc

    with FO.persistent_oracle("exec", spawn=spawn) as oracle:
        assert FO.normalize_result(oracle("Q1"))["value"] == 1
        assert FO.normalize_result(oracle("Q2"))["value"] == 2
    assert len(spawns) == 1                                  # started ONCE, not per query
    assert json.loads(proc.stdin.written[0])["dax"] == "Q1"
    assert proc.stdin.closed and proc.waited                 # context manager closed it


def test_persistent_oracle_reports_a_dead_process_instead_of_raising():
    proc = _FakeProc([])                                     # stdout immediately EOF
    oracle = FO.persistent_oracle("exec", spawn=lambda _c: proc)
    res = oracle("Q1")
    assert "closed its output" in res["error"]
    assert "closed its output" in oracle("Q2")["error"]       # stays dead, still no raise


def test_persistent_oracle_reports_a_spawn_failure():
    def spawn(_cmd):
        raise OSError("executor missing")

    res = FO.persistent_oracle("exec", spawn=spawn)("Q1")
    assert "could not start" in res["error"] and "executor missing" in res["error"]


def test_persistent_oracle_reports_non_json_output():
    proc = _FakeProc(["<html>error page</html>\n"])
    res = FO.persistent_oracle("exec", spawn=lambda _c: proc)("Q1")
    assert "not JSON" in res["error"]


# --- (e) reading the emitted model (both sides come from the artifact) ---------------------------
_TMDL = """table _Measures
\tlineageTag: abc

\tmeasure 'Gross Profit Ratio' = DIVIDE(SUM('Orders$'[Profit]), SUM('Orders$'[Sales]))
\t\tformatString: 0.0%
\t\tlineageTag: c511b60e
\t\tannotation TableauFormula = SUM([Profit])/SUM([Sales])
\t\tannotation TranslatedBy = deterministic

\tmeasure Stubbed = 0
\t\tannotation TableauFormula = SCRIPT_REAL("x", SUM([Sales]))

\tpartition _Measures = calculated
\t\tmode: import
"""


def test_measures_from_tmdl_reads_dax_and_the_preserved_tableau_formula():
    rows = FO.measures_from_tmdl(_TMDL)
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"Gross Profit Ratio", "Stubbed"}
    assert by_name["Gross Profit Ratio"]["dax"].startswith("DIVIDE(SUM(")
    assert by_name["Gross Profit Ratio"]["tableau_formula"] == "SUM([Profit])/SUM([Sales])"
    assert by_name["Stubbed"]["dax"] == "0"   # the sibling partition block must not leak in
    assert "SCRIPT_REAL" in by_name["Stubbed"]["tableau_formula"]


def test_measures_from_tmdl_ignores_columns_partitions_and_junk():
    assert FO.measures_from_tmdl("") == []
    assert FO.measures_from_tmdl(None) == []
    assert FO.measures_from_tmdl("table T\n\tcolumn C\n\t\tdataType: string\n") == []


def test_measures_from_model_dir_walks_tmdl_files(tmp_path):
    tables = tmp_path / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "_Measures.tmdl").write_text(_TMDL, encoding="utf-8")
    rows = FO.measures_from_model_dir(str(tmp_path))
    assert {r["name"] for r in rows} == {"Gross Profit Ratio", "Stubbed"}
    assert rows[0]["source_file"].endswith("_Measures.tmdl")


def test_measures_from_model_dir_on_a_missing_folder_is_empty_not_an_error():
    assert FO.measures_from_model_dir(os.path.join(HERE, "does-not-exist")) == []


# --- (f) THE SAFETY PROPERTY: nothing reads VERIFIED without both sides ---------------------------
_ROWS = [{"name": "M", "dax": "SUM('Orders'[Sales])", "tableau_formula": "SUM([Sales])"}]


def test_no_oracle_means_every_record_is_not_evaluated_by_construction():
    rep = FO.verify_model("x", measures=_ROWS)
    assert rep["oracle_attached"] is False
    assert rep["summary"][FO.SUM_VERIFIED] == 0
    assert rep["summary"][FO.SUM_NOT_EVALUATED] == 1
    assert "BY CONSTRUCTION" in FO.format_verification(rep)


def test_an_oracle_with_no_ground_truth_still_cannot_verify():
    rep = FO.verify_model("x", measures=_ROWS, fabric_oracle=lambda _q: {"value": 100})
    assert rep["oracle_attached"] is True
    assert rep["ground_truth_attached"] is False
    assert rep["summary"][FO.SUM_VERIFIED] == 0            # a value with nothing to compare proves nothing
    assert rep["summary"][FO.SUM_NOT_EVALUATED] == 1


def test_null_oracle_is_honestly_uniformly_not_evaluated():
    rep = FO.verify_model("x", measures=_ROWS, fabric_oracle=FO.null_oracle,
                          tableau_values={"M": 100})
    assert rep["summary"][FO.SUM_VERIFIED] == 0
    assert rep["summary"][FO.SUM_NOT_EVALUATED] == 1


def test_both_sides_present_and_agreeing_verifies():
    rep = FO.verify_model("x", measures=_ROWS, fabric_oracle=lambda _q: {"value": 100},
                          tableau_values={"M": 100})
    assert rep["ground_truth_attached"] is True
    assert rep["summary"][FO.SUM_VERIFIED] == 1


def test_both_sides_present_and_disagreeing_is_a_mismatch():
    rep = FO.verify_model("x", measures=_ROWS, fabric_oracle=lambda _q: {"value": 7},
                          tableau_values={"M": 100})
    assert rep["summary"][FO.SUM_MISMATCH] == 1
    assert "MISMATCH" in FO.format_verification(rep)


def test_an_oracle_that_raises_downgrades_to_not_evaluated_never_verified():
    def boom(_q):
        raise RuntimeError("XMLA down")

    rep = FO.verify_model("x", measures=_ROWS, fabric_oracle=boom, tableau_values={"M": 100})
    assert rep["summary"][FO.SUM_VERIFIED] == 0
    assert rep["summary"][FO.SUM_NOT_EVALUATED] == 1


def test_an_oracle_erroring_does_not_become_a_zero():
    """A fabricated 0 would compare equal to a real 0 and produce a FALSE verified."""
    rep = FO.verify_model("x", measures=[{"name": "M", "dax": "SUM('Orders'[Sales])",
                                          "tableau_formula": "SUM([Sales])"}],
                          fabric_oracle=lambda _q: {"error": "credentials required"},
                          tableau_values={"M": 0})
    assert rep["summary"][FO.SUM_VERIFIED] == 0
    assert rep["summary"][FO.SUM_NOT_EVALUATED] == 1


# --- (g) the seam is genuinely reachable end-to-end -----------------------------------------------
def test_end_to_end_from_tmdl_on_disk_through_an_external_process_adapter(tmp_path):
    tables = tmp_path / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "_Measures.tmdl").write_text(
        "table _Measures\n\tmeasure Total = SUM('Orders'[Sales])\n"
        "\t\tannotation TableauFormula = SUM([Sales])\n", encoding="utf-8")

    def run(_cmd, _payload, _timeout):
        return 0, json.dumps({"results": [{"tables": [{"rows": [{"[value]": 250}]}]}]}), ""

    oracle = FO.subprocess_oracle("my-desktop-executor", runner=run)
    assert FO.conforms(oracle)["ok"] is True
    rep = FO.verify_model(str(tmp_path), fabric_oracle=oracle, tableau_values={"Total": 250})
    assert rep["summary"][FO.SUM_VERIFIED] == 1
    assert rep["records"][0]["tableau_formula"] == "SUM([Sales])"


def test_verify_model_reports_the_reconcile_states_verbatim():
    """The seam must not invent its own vocabulary -- it reuses translation_reconcile's states."""
    assert (FO.VERIFIED, FO.MISMATCH, FO.NOT_EVALUATED) == (TR.VERIFIED, TR.MISMATCH, TR.NOT_EVALUATED)


# --- (h) round-trip against the REAL emitter, not a hand-written fixture -------------------------
# The corpus happens to contain zero multi-line measure bodies, so a fixture-only test would leave
# `_tmdl_assignment`'s BLOCK form (`<decl> =` alone, body indented one level deeper) unproven. These
# generate TMDL with the actual emitter and read it back.
import tmdl_generate as TG  # noqa: E402


def _roundtrip(name, tableau_formula, dax, **kw):
    tmdl = "table _Measures\n" + TG.generate_measure_tmdl(name, tableau_formula, dax, **kw)
    rows = FO.measures_from_tmdl(tmdl)
    assert len(rows) == 1, tmdl
    return rows[0]


def test_roundtrips_a_single_line_measure_from_the_real_emitter():
    row = _roundtrip("Ratio", "SUM([Profit])/SUM([Sales])",
                     "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))", format_string="0.0%")
    assert row["dax"] == "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))"
    assert row["tableau_formula"] == "SUM([Profit])/SUM([Sales])"


def test_roundtrips_a_MULTI_LINE_var_return_body_from_the_real_emitter():
    dax = "VAR _t = SUM('Orders'[Sales])\nVAR _p = SUM('Orders'[Profit])\nRETURN DIVIDE(_p, _t)"
    row = _roundtrip("Margin", "SUM([Profit])/SUM([Sales])", dax)
    assert row["dax"] == dax                       # every continuation line, in order, nothing else
    assert row["tableau_formula"] == "SUM([Profit])/SUM([Sales])"


def test_roundtrips_an_inert_stub_and_keeps_its_preserved_formula():
    row = _roundtrip("Scripted", 'SCRIPT_REAL("x", SUM([Sales]))', None)
    assert row["dax"] == "0"                       # the inert stub, not a guess
    assert "SCRIPT_REAL" in row["tableau_formula"]


def test_a_multi_line_measure_followed_by_another_block_does_not_swallow_it():
    dax = "VAR _a = 1\nRETURN _a"
    tmdl = ("table _Measures\n"
            + TG.generate_measure_tmdl("First", "1", dax)
            + TG.generate_measure_tmdl("Second", "2", "2")
            + "\n\tpartition _Measures = calculated\n\t\tmode: import\n"
              "\t\tsource = Row(\"Value\", BLANK())\n")
    rows = {r["name"]: r["dax"] for r in FO.measures_from_tmdl(tmdl)}
    assert rows == {"First": dax, "Second": "2"}