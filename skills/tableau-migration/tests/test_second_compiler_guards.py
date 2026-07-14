"""Tests for the GUARD-ONLY model-aware gates wired into the second-compiler landing driver.

Two optional gates layer onto the existing landing chokepoint (``_gate_ok``) purely as ADDITIONAL
rejection filters -- they never author or alter a candidate:

  * the REFERENCE gate -- every ``[Measure]``/``'Table'[Column]`` a candidate names must exist in the
    generated model (catches the ``(copy)_NNNN`` duplicate-name trap); a bare reference to an inert
    SIBLING measure (the dependency graph's own job) only WARNS, never blocks; and
  * the RECONCILIATION oracle -- a candidate whose value diverges from the Tableau formula over landed
    data is rejected (PASS and INCONCLUSIVE both land -- a false PASS is worse than a stub).

The load-bearing invariant: ``guards=None`` is byte-identical to the unguarded driver, so the 2467
existing tests are untouched and these only exercise the additive rejection behaviour + telemetry.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

import second_compiler as SC  # noqa: E402
import reference_gate as _reference_gate  # noqa: E402
from translation_router import check_candidate_dax  # noqa: E402


# --- fixtures (same synthetic-workbook style as test_second_compiler.py) -------------------------
def _wb(calc_cols):
    """Single-datasource SQL Server workbook; resolver binds Sales/Region/Order Date on 'Orders'."""
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook><datasources>
 <datasource formatted-name='Superstore' caption='Sales' name='fed.sales' version='18.1'>
  <connection class='federated'>
   <named-connections><named-connection caption='myserver' name='sqlserver.0a1b2c'>
     <connection authentication='sqlserver' class='sqlserver' dbname='Superstore'
                 server='myserver.database.windows.net' username='svc' />
   </named-connection></named-connections>
   <relation connection='sqlserver.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
   <metadata-records>
    <metadata-record class='column'><remote-name>Sales</remote-name><local-name>[Sales]</local-name><parent-name>[Orders]</parent-name><local-type>real</local-type></metadata-record>
    <metadata-record class='column'><remote-name>Region</remote-name><local-name>[Region]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
    <metadata-record class='column'><remote-name>Order Date</remote-name><local-name>[Order Date]</local-name><parent-name>[Orders]</parent-name><local-type>date</local-type></metadata-record>
   </metadata-records>
  </connection>
  {calc_cols}
 </datasource>
</datasources></workbook>"""


def _col(caption, name, formula, role="measure"):
    return (f"<column caption='{caption}' name='[{name}]' role='{role}'>"
            f"<calculation class='tableau' formula='{formula}'/></column>")


# Base is a genuine stub (SCRIPT_REAL has no faithful DAX); Plus/Ratio cascade off the keystone.
_STUB_CHAIN = "\n".join([
    _col("Base", "Calculation_base", 'SCRIPT_REAL(&quot;x&quot;, SUM([Sales]))'),
    _col("Plus", "Calculation_plus", "[Base] + 1"),
    _col("Ratio", "Calculation_ratio", "[Base] / [Plus]"),
])


def _manifest():
    """The generated-model surface: real Orders columns + the three calc measures as inert stubs."""
    return {
        "tables": ["Orders", "_Measures"],
        "columns": [
            {"model_table": "Orders", "model_name": "Sales", "status": "physical"},
            {"model_table": "Orders", "model_name": "Region", "status": "physical"},
            {"model_table": "Orders", "model_name": "Order Date", "status": "physical"},
        ],
        "measures": [
            {"model_table": "_Measures", "model_name": "Base", "status": "stub"},
            {"model_table": "_Measures", "model_name": "Plus", "status": "stub"},
            {"model_table": "_Measures", "model_name": "Ratio", "status": "stub"},
        ],
    }


# --- (a) guards=None is byte-identical to the unguarded driver -----------------------------------
def test_guards_none_is_byte_identical_to_default():
    wb = _wb(_STUB_CHAIN)
    a = SC.land_second_compiler(wb, authored={"Base": "SUM('Orders'[Sales])"})
    b = SC.land_second_compiler(wb, authored={"Base": "SUM('Orders'[Sales])"}, guards=None)
    assert a == b
    assert set(a) == {"Base", "Plus", "Ratio"}


def test_land_detail_default_is_unguarded_with_empty_verdicts():
    detail = SC._land(_wb(_STUB_CHAIN), authored={"Base": "SUM('Orders'[Sales])"})
    assert detail["guarded"] is False
    assert detail["guard_verdicts"] == {}


# --- (b) the reference gate blocks the (copy) trap: a nonexistent-table keystone ------------------
def test_reference_gate_blocks_nonexistent_table_keystone():
    guards = SC.build_guards(model_manifest=_manifest())
    bad = "SUM('Orders Copy'[Sales])"          # syntactically fine, but 'Orders Copy' is not a table
    assert check_candidate_dax(bad)["ok"] is True     # precondition: the SYNTACTIC gate passes it
    detail = SC._land(_wb(_STUB_CHAIN), authored={"Base": bad}, guards=guards)
    assert detail["approved"] == {}                    # reference gate rejected the keystone
    assert "Base" in detail["gate_failures"]
    assert detail["cascaded"] == []                    # so the whole chain stays stub


def test_reference_gate_off_would_land_the_same_bad_ref():
    # Control: without guards the SAME bad-ref keystone lands (proving the block is the guard's doing).
    out = SC.land_second_compiler(_wb(_STUB_CHAIN), authored={"Base": "SUM('Orders Copy'[Sales])"})
    assert "Base" in out


# --- (c) a clean keystone lands AND cascades with guards active -----------------------------------
def test_clean_keystone_lands_and_cascades_with_guards():
    guards = SC.build_guards(model_manifest=_manifest())
    detail = SC._land(_wb(_STUB_CHAIN), authored={"Base": "SUM('Orders'[Sales])"}, guards=guards)
    assert set(detail["approved"]) == {"Base", "Plus", "Ratio"}
    assert detail["guarded"] is True


# --- (d) the "graph solves dependencies" guarantee: a sibling/stub ref only WARNS, never blocks ---
def test_sibling_measure_reference_is_not_blocked_only_warns():
    surface = _reference_gate.build_model_surface(model_manifest=_manifest())
    res = _reference_gate.check_candidate_references("[Base] + 1", surface)
    assert res["ok"] is True                            # inert sibling measure -> resolves, warns at most
    assert res["warnings"]                              # (it does warn -- the graph owns the dependency)


# --- (e) the oracle rejects a candidate that numerically FAILS ------------------------------------
def _num_tables():
    return {"Orders": {"columns": ["Sales", "Profit"],
                       "rows": [{"Sales": 10, "Profit": 1}, {"Sales": 20, "Profit": 2}]}}


def _num_resolver(caption):
    return {"sales": ("Orders", "Sales"), "profit": ("Orders", "Profit")}.get((caption or "").lower())


def test_gate_ok_oracle_fail_rejects_and_pass_lands():
    guards = {"surface": None, "tables": _num_tables(), "resolver": _num_resolver}
    # candidate diverges from the Tableau formula (Sales total 30 != Profit total 3) -> FAIL -> reject
    assert SC._gate_ok("SUM('Orders'[Profit])", True, guards=guards,
                       tableau_formula="SUM([Sales])") is False
    # faithful candidate agrees -> lands
    assert SC._gate_ok("SUM('Orders'[Sales])", True, guards=guards,
                       tableau_formula="SUM([Sales])") is True


# --- (f) PASS and INCONCLUSIVE both land (a false PASS is worse than a stub) ----------------------
def test_gate_ok_oracle_inconclusive_lands():
    guards = {"surface": None, "tables": {"Orders": {"columns": ["Sales"], "rows": [{"Sales": 10}]}},
              "resolver": lambda _c: None}
    # out-of-subset Tableau formula -> INCONCLUSIVE -> must NOT block a syntactically clean candidate
    assert SC._gate_ok("SUM('Orders'[Sales])", True, guards=guards,
                       tableau_formula="SCRIPT_REAL('x', SUM([Sales]))") is True


# --- (g) no landed data -> the oracle is skipped (surface-only guards) ----------------------------
def test_gate_ok_no_tables_skips_oracle():
    guards = SC.build_guards(model_manifest=_manifest())
    assert guards["tables"] is None
    assert SC._gate_ok("SUM('Orders'[Sales])", True, guards=guards,
                       tableau_formula="SUM([Sales])") is True


# --- (h) build_guards fails closed to None when nothing usable is supplied ------------------------
def test_build_guards_returns_none_when_nothing_usable():
    assert SC.build_guards() is None
    assert SC.build_guards(model_manifest=None, tmdl_parts=None, tables=None) is None


def test_build_guards_surface_only_has_no_tables():
    guards = SC.build_guards(model_manifest=_manifest())
    assert guards is not None
    assert guards["surface"] is not None
    assert guards["tables"] is None


def test_build_guards_tables_only_has_no_surface():
    guards = SC.build_guards(tables=_num_tables(), resolver=_num_resolver)
    assert guards is not None
    assert guards["surface"] is None
    assert guards["tables"] == _num_tables()


# --- (i) guard_verdicts telemetry is present per landed calc when guards are active ---------------
def test_guard_verdicts_telemetry_present_for_landed_calcs():
    guards = SC.build_guards(model_manifest=_manifest())
    detail = SC._land(_wb(_STUB_CHAIN), authored={"Base": "SUM('Orders'[Sales])"}, guards=guards)
    assert detail["guarded"] is True
    gv = detail["guard_verdicts"]
    assert set(gv) == {"Base", "Plus", "Ratio"}
    for nm in ("Base", "Plus", "Ratio"):
        assert gv[nm]["reference"] == "ok"             # all references resolve in the model surface
        assert gv[nm]["oracle"] == "skipped"           # no landed data -> oracle not consulted
