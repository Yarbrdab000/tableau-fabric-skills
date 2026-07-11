"""Tests for the Spec-4 SECOND-COMPILER LANDING DRIVER (``second_compiler.land_second_compiler``).

The driver invents no DAX -- it only runs the loop a human otherwise hand-writes each migration:
seed keystones (explicit ``authored`` overrides + Spec-7 idiom-detector defaults), gate-and-land
them, then fix-point-cascade every dependent calc back through the engine's OWN translator (each
still gated). These tests lock the faithful-by-construction guarantees:

  * a measure-of-measure chain lands whole in one call off a keystone (authored OR detector);
  * a deliberately unfaithful base is gate-rejected and its dependents stay stubs;
  * a purely-deterministic chain is EXCLUDED (the normal build owns it, keeping ``deterministic``
    provenance) so the driver is a no-op there;
  * the cascade depth honours the ``rounds`` cap;
  * dimension-role calcs route through the column translator.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

import second_compiler as SC  # noqa: E402
from translation_router import check_candidate_dax  # noqa: E402


def _wb(calc_cols):
    """A single-datasource workbook (SQL Server) whose resolver binds Sales/Region/Order Date."""
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


# A base whose deterministic translation FAILS (SCRIPT_REAL has no faithful DAX) -> a genuine stub
# that only lands via a keystone. Its dependents form the measure-of-measure chain.
_STUB_CHAIN = "\n".join([
    _col("Base", "Calculation_base", 'SCRIPT_REAL(&quot;x&quot;, SUM([Sales]))'),
    _col("Plus", "Calculation_plus", "[Base] + 1"),
    _col("Ratio", "Calculation_ratio", "[Base] / [Plus]"),
])


def test_authored_keystone_lands_whole_measure_chain():
    # The spec's headline: ONE call lands the entire chain via the native translator once its
    # keystone base is supplied. Base is authored; Plus and Ratio cascade off it.
    out = SC.land_second_compiler(_wb(_STUB_CHAIN), authored={"Base": "SUM('Orders'[Sales])"})
    assert set(out) == {"Base", "Plus", "Ratio"}
    assert out["Base"] == "SUM('Orders'[Sales])"
    assert "[Base]" in out["Plus"]                       # cascaded reference to the keystone
    assert "[Base]" in out["Ratio"] and "[Plus]" in out["Ratio"]
    # Everything returned is gate-clean (faithful-by-construction).
    for dax in out.values():
        assert check_candidate_dax(dax)["ok"]


def test_detector_idiom_supplies_default_keystone_and_cascades():
    # The native improvement over a hand-written driver: a Spec-7 idiom the engine's OWN detectors
    # recognize becomes a default keystone with no explicit ``authored`` entry, and the chain
    # cascades off it. (Year-gated: IF YEAR([d]) = 2023 THEN [x] END.)
    chain = "\n".join([
        _col("Base", "Calculation_base", "IF YEAR([Order Date]) = 2023 THEN [Sales] END"),
        _col("Plus", "Calculation_plus", "[Base] + 1"),
        _col("Ratio", "Calculation_ratio", "[Base] / [Plus]"),
    ])
    detail = SC._land(_wb(chain))
    assert detail["detectors"] == ["Base"]               # auto-keystoned, not authored
    assert detail["authored"] == []
    out = detail["approved"]
    assert set(out) == {"Base", "Plus", "Ratio"}
    assert "CALCULATE(" in out["Base"] and "YEAR(" in out["Base"]
    assert "[Base]" in out["Plus"] and "[Plus]" in out["Ratio"]


def test_unfaithful_authored_base_is_gate_rejected_chain_stays_stub():
    # A deliberately unfaithful base (malformed DAX -- unbalanced paren) must be gate-rejected, and
    # because the cascade seeds measure_refs from APPROVED names only, the dependents cannot land.
    bad = "SUM('Orders'[Sales]"
    assert check_candidate_dax(bad)["ok"] is False       # precondition: the gate rejects it
    detail = SC._land(_wb(_STUB_CHAIN), authored={"Base": bad})
    assert detail["approved"] == {}                      # nothing lands
    assert "Base" in detail["gate_failures"]
    assert detail["cascaded"] == []                      # chain stays stub


def test_deterministic_chain_is_excluded_to_preserve_provenance():
    # A measure-of-measure chain the base translator handles on its own is NOT re-emitted here --
    # the normal build owns it (keeping ``deterministic`` provenance), so the driver is a no-op.
    chain = "\n".join([
        _col("Base", "Calculation_base", "SUM([Sales])"),
        _col("Plus", "Calculation_plus", "[Base] + 1"),
        _col("Ratio", "Calculation_ratio", "[Base] / [Plus]"),
    ])
    detail = SC._land(_wb(chain))
    assert detail["approved"] == {}
    assert detail["plain_count"] == 3                    # all three landed in the plain closure


def test_no_keystones_is_a_noop_empty_dict():
    # No idiom, no authored override, only a deterministic leaf -> empty supplement. This is the
    # guarantee that wiring the driver in is byte-unchanged when there is nothing to add.
    out = SC.land_second_compiler(_wb(_col("Solo", "Calculation_solo", "SUM([Sales])")))
    assert out == {}


def test_rounds_cap_limits_cascade_depth():
    # rounds=1 seeds refs from the keystone only, so the DIRECT dependent lands but the grandchild
    # (which needs the dependent approved) does not until a later round.
    shallow = SC._land(_wb(_STUB_CHAIN), authored={"Base": "SUM('Orders'[Sales])"}, rounds=1)
    assert "Plus" in shallow["approved"]                 # direct dependent of the keystone
    assert "Ratio" not in shallow["approved"]            # grandchild needs another round
    deep = SC._land(_wb(_STUB_CHAIN), authored={"Base": "SUM('Orders'[Sales])"}, rounds=12)
    assert {"Base", "Plus", "Ratio"} <= set(deep["approved"])


def test_dimension_role_keystone_uses_column_translator():
    # A dimension-role stub authored as a keystone lands (routed through the column translator, not
    # the measure translator). Its dependent dimension does NOT cascade off a measure ref -- the
    # column translator has no measure_refs seam -- so only the keystone itself is returned.
    dim = "\n".join([
        _col("DimBase", "Calculation_db", 'SCRIPT_STR(&quot;y&quot;)', role="dimension"),
        _col("DimDep", "Calculation_dd", "[DimBase]", role="dimension"),
    ])
    authored = {"DimBase": "IF('Orders'[Region] = \"West\", \"W\", \"E\")"}
    out = SC.land_second_compiler(_wb(dim), authored=authored)
    assert out.get("DimBase") == authored["DimBase"]
    assert "DimDep" not in out                           # no measure-ref cascade in column mode


def test_accepts_bytes_input():
    # _load_twb decodes bytes (utf-8-sig) so a caller can pass raw workbook bytes.
    raw = _wb(_STUB_CHAIN).encode("utf-8-sig")
    out = SC.land_second_compiler(raw, authored={"Base": "SUM('Orders'[Sales])"})
    assert set(out) == {"Base", "Plus", "Ratio"}
