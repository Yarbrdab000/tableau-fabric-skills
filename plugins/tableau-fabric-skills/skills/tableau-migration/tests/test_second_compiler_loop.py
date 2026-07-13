"""End-to-end regression test for the Tier-0 -> Tier-1 SECOND-COMPILER loop.

Unlike the unit tests (which lock one component each), this exercises the whole pipeline on a
single varied datasource so the components stay COHERENT together:

  1. the orchestrator translates the clean calcs LIVE and hands off the rest;
  2. the ROUTER labels every fallback with the expected charter category;
  3. the agent-facing GATE (``check_candidate_dax``) accepts a well-formed candidate and blocks a
     malformed / un-translated one;
  4. ``approved_calc_dax`` lands ONLY a gate-approved candidate as ``assisted-approved``;
  5. the ``model_object_parameter`` path the playbook documents actually works: the deterministic
     ``emit_value_parameters`` emitter + its ``param_resolver`` let Tier 0 finish a what-if calc.

It asserts on categories / statuses / gate verdicts (stable contract) rather than exact DAX text.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

import assemble_model as A  # noqa: E402
import translation_router as R  # noqa: E402
import parameters as P  # noqa: E402
from calc_to_dax import translate_tableau_calc_to_dax  # noqa: E402


_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='myserver' name='sqlserver.0a1b2c'>
        <connection authentication='sqlserver' class='sqlserver' dbname='Superstore'
                    server='myserver.database.windows.net' username='svc' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order ID</remote-name><local-name>[Order ID]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Order Date</remote-name><local-name>[Order Date]</local-name>
        <parent-name>[Orders]</parent-name><local-type>datetime</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Profit</remote-name><local-name>[Profit]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>City</remote-name><local-name>[City]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Measure-role calcs spanning the router taxonomy plus two clean controls.
_CALCS = [
    {"name": "Total Sales", "formula": "SUM([Sales])"},                       # control -> live
    {"name": "Profit Ratio", "formula": "SUM([Profit]) / SUM([Sales])"},      # control -> live
    {"name": "Sales Rank", "formula": "RANK(SUM([Sales]))"},                  # -> addressing
    {"name": "Regional Mix",
     "formula": "SUM([Sales]) / { INCLUDE [Region] : SUM([Sales]) }"},         # -> outer aggregation
    {"name": "San Flag", "formula": "REGEXP_MATCH([City], '^San')"},          # -> dax language gap
    {"name": "Grown Sales",
     "formula": "SUM([Sales]) * (1 + [Parameters].[Growth Rate])"},           # -> model object param
]


def _run(approved=None):
    return A.migrate_tds_to_semantic_model(
        _TDS, model_name="LoopTest", calcs=_CALCS, approved_calc_dax=approved)


def test_loop_translates_controls_live_and_hands_off_the_rest():
    ho = _run()["report"]["translation_handoff"]
    s = ho["summary"]
    assert s["total"] == 6
    assert s["live"] == 2                       # Total Sales + Profit Ratio
    assert s["needs_review"] == 4


def test_loop_router_categorizes_every_fallback():
    ho = _run()["report"]["translation_handoff"]
    cat = {r["name"]: r["category"] for r in ho["requests"]}
    assert cat["Sales Rank"] == R.MISSING_ADDRESSING_INTENT
    assert cat["Regional Mix"] == R.MISSING_OUTER_AGGREGATION
    assert cat["San Flag"] == R.DAX_LANGUAGE_GAP
    assert cat["Grown Sales"] == R.MODEL_OBJECT_PARAMETER
    # the summary category histogram agrees with the per-request labels
    cats = ho["summary"]["categories"]
    assert cats[R.MISSING_ADDRESSING_INTENT] == 1
    assert cats[R.MODEL_OBJECT_PARAMETER] == 1
    assert sum(cats.values()) == ho["summary"]["needs_review"]


def test_loop_gate_accepts_good_and_blocks_bad_candidates():
    ho = _run()["report"]["translation_handoff"]
    reqs = {r["name"]: r for r in ho["requests"]}

    good = R.check_candidate_dax(
        "DIVIDE(SUM('Orders'[Sales]), "
        "CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[Region])))",
        request=reqs["Regional Mix"])
    assert good["ok"] is True and good["issues"] == []

    unbalanced = R.check_candidate_dax(
        "RANKX(ALLSELECTED('Orders'), CALCULATE(SUM('Orders'[Sales]))",
        request=reqs["Sales Rank"])
    assert unbalanced["ok"] is False
    assert any("unclosed" in i for i in unbalanced["issues"])

    leftover = R.check_candidate_dax("IF([Parameters].[x], 1, 0)", request=reqs["San Flag"])
    assert leftover["ok"] is False
    assert any("Tableau idiom" in i for i in leftover["issues"])


def test_loop_lands_only_gate_approved_candidate():
    candidate = ("DIVIDE(SUM('Orders'[Sales]), "
                 "CALCULATE(SUM('Orders'[Sales]), ALLEXCEPT('Orders', 'Orders'[Region])))")
    assert R.check_candidate_dax(candidate)["ok"] is True   # gate clears it first

    report = _run(approved={"Regional Mix": candidate})["report"]
    by_name = {m["measure"]: m for m in report["measures"]}
    assert by_name["Regional Mix"]["status"] == "assisted-approved"
    assert by_name["Regional Mix"]["dax"] == candidate
    # an un-approved fallback stays inert
    assert by_name["San Flag"]["status"] == "stub"


def test_loop_model_object_parameter_path_via_deterministic_emitter():
    # The model_object_parameter playbook: reuse parameters.py rather than hand-author. A declared
    # what-if parameter + emit_value_parameters yields a resolver that lets Tier 0 finish the calc.
    growth = {"caption": "Growth Rate", "internal_name": "[Parameter 1]", "datatype": "real",
              "domain": "range", "default": "0.0", "format": "p0.00%",
              "range": {"min": "-0.5", "max": "0.5", "step": "0.01"}, "members": [], "aliases": {}}
    grown = {"name": "Grown Sales", "role": "measure",
             "formula": "SUM([Sales]) * (1 + [Parameters].[Growth Rate])"}
    res = P.emit_value_parameters([growth], calcs=[grown], reserved_names={"orders", "sales"})
    assert res["measure_names"] == ["Growth Rate Value"]
    assert res["param_resolver"]("Growth Rate") == ("[Growth Rate Value]", "number")

    def _resolver(caption):
        return {"Sales": ("Orders", "Sales", "double")}.get(caption)

    dax, reason, tables = translate_tableau_calc_to_dax(
        grown["formula"], _resolver, param_resolver=res["param_resolver"])
    assert reason == "ok"
    assert "[Growth Rate Value]" in dax        # the selection is inlined, not stubbed
    assert tables == {"Orders"}                 # disconnected param table is NOT joined in
