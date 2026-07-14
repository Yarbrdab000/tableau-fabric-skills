"""Tests for the model-aware reference gate (structural half of the second-compiler gate).

``check_candidate_dax`` (translation_router) proves a candidate is well-formed DAX; the
reconciliation oracle proves it is numerically faithful. NEITHER proves that every ``[Measure]`` /
``'Table'[Column]`` the candidate names actually EXISTS in the generated model. That structural
proof is what this gate provides -- catching the ``(copy)_NNNN`` duplicate-name trap and any dangling
reference before a well-formed-but-wrong candidate lands. The contract under test: BLOCK a reference
to a non-existent object (with a did-you-mean), WARN on an inert/unqualified/ambiguous resolution,
and NEVER raise.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import reference_gate as rg  # noqa: E402
from reference_gate import build_model_surface, check_candidate_references  # noqa: E402


# -- fixtures ------------------------------------------------------------------------------------
def _manifest():
    """A model_manifest-shaped dict (the surface source) exercising every gate branch:
    base + calc columns on Orders, a same-named column on a 2nd table (ambiguity), an inert calc
    column, live + inert measures, and two ``(copy)`` duplicate-name families (the trap)."""
    columns = [
        {"model_table": "Orders", "model_name": "Sales", "tableau_field": "Sales",
         "source_column": "Sales", "type": "double", "calculated": False},
        {"model_table": "Orders", "model_name": "Profit", "tableau_field": "Profit",
         "source_column": "Profit", "type": "double", "calculated": False},
        {"model_table": "Orders", "model_name": "Region", "tableau_field": "Region",
         "source_column": "Region", "type": "string", "calculated": False},
        {"model_table": "Orders", "model_name": "Order Date", "tableau_field": "Order Date",
         "source_column": "Order Date", "type": "dateTime", "calculated": False},
        {"model_table": "Orders", "model_name": "Margin", "tableau_field": "Margin",
         "source_column": None, "type": None, "calculated": True, "status": "translated"},
        {"model_table": "Orders", "model_name": "DraftCol", "tableau_field": "DraftCol",
         "source_column": None, "type": None, "calculated": True, "status": "stub"},
        # Region also lives on Contact (Intake) -> a bare [Region] is ambiguous.
        {"model_table": "Contact (Intake)", "model_name": "Id", "tableau_field": "Id",
         "source_column": "Id", "type": "string", "calculated": False},
        {"model_table": "Contact (Intake)", "model_name": "Stage", "tableau_field": "Stage",
         "source_column": "Stage", "type": "string", "calculated": False},
        {"model_table": "Contact (Intake)", "model_name": "Region", "tableau_field": "Region",
         "source_column": "Region", "type": "string", "calculated": False},
        {"model_table": "Date", "model_name": "Date", "tableau_field": "Date",
         "source_column": "Date", "type": "dateTime", "calculated": False},
        {"model_table": "Date", "model_name": "Year", "tableau_field": "Year",
         "source_column": "Year", "type": "int64", "calculated": False},
    ]
    measures = [
        {"model_table": "_Measures", "model_name": "Total Sales", "status": "translated",
         "source": {}},
        {"model_table": "_Measures", "model_name": "Profit Ratio", "status": "assisted-approved",
         "source": {}},
        # The (copy) duplicate-name trap: this LIVE measure is what a candidate that names the
        # bare "Quantity Difference vs Previous Year" is really trying to reach.
        {"model_table": "_Measures",
         "model_name": "Quantity Difference vs Previous Year (copy)_1", "status": "translated",
         "source": {}},
        {"model_table": "_Measures", "model_name": "Draft Measure", "status": "stub",
         "source": {}},
        {"model_table": "_Measures", "model_name": "Suggested Measure",
         "status": "assisted-suggested", "source": {}},
        # A live/inert near-duplicate pair on a distinct name family.
        {"model_table": "_Measures", "model_name": "Sales Target", "status": "translated",
         "source": {}},
        {"model_table": "_Measures", "model_name": "Sales Target (copy)_1", "status": "stub",
         "source": {}},
    ]
    return {"tables": ["Orders", "Contact (Intake)", "Date"], "columns": columns,
            "measures": measures, "date": {"generated": True, "table": "Date"},
            "row_count": {}, "parameters": [], "naming": {}}


def _surface():
    return build_model_surface(model_manifest=_manifest())


def _lower_warns(v):
    return " || ".join(v["warnings"]).lower()


def _lower_issues(v):
    return " || ".join(v["issues"]).lower()


# -- surface construction ------------------------------------------------------------------------
def test_build_surface_from_manifest_includes_measures_table():
    s = _surface()
    assert "orders" in s["tables"]
    assert "contact (intake)" in s["tables"]
    # _Measures is excluded from manifest["tables"] but IS the default measure table -> present.
    assert "_measures" in s["tables"]
    assert s["source"] == "manifest"


def test_build_model_surface_requires_a_source():
    try:
        build_model_surface()
    except ValueError:
        return
    raise AssertionError("expected ValueError when neither manifest nor tmdl_parts is given")


# -- valid references ----------------------------------------------------------------------------
def test_valid_qualified_column_ok():
    v = check_candidate_references("SUM('Orders'[Sales])", _surface())
    assert v["ok"] is True, v
    assert v["issues"] == []
    assert len(v["references"]) >= 1


def test_valid_bare_measure_ok():
    v = check_candidate_references("[Total Sales] * 2", _surface())
    assert v["ok"] is True, v
    assert v["issues"] == []


def test_bare_table_qualified_column_ok():
    v = check_candidate_references("SUMX(Orders, Orders[Sales])", _surface())
    assert v["ok"] is True, v


def test_references_list_nonempty_for_multi_ref_candidate():
    v = check_candidate_references("DIVIDE(SUM('Orders'[Profit]), [Total Sales])", _surface())
    assert v["ok"] is True, v
    assert len(v["references"]) >= 2


def test_standalone_table_reference_ok():
    v = check_candidate_references("CALCULATE([Total Sales], ALL('Orders'))", _surface())
    assert v["ok"] is True, v


# -- blocking references -------------------------------------------------------------------------
def test_unknown_table_blocks_with_suggestion():
    v = check_candidate_references("SUM('Ordrs'[Sales])", _surface())
    assert v["ok"] is False, v
    iss = _lower_issues(v)
    assert "unknown table" in iss
    assert "orders" in iss  # did-you-mean the real table


def test_unknown_column_blocks_with_suggestion():
    v = check_candidate_references("SUM('Orders'[Salez])", _surface())
    assert v["ok"] is False, v
    iss = _lower_issues(v)
    assert "not found" in iss
    assert "sales" in iss  # did-you-mean the real column


def test_unknown_bare_reference_blocks():
    v = check_candidate_references("[Nonexistent Thing] + 1", _surface())
    assert v["ok"] is False, v
    assert "no such measure or column" in _lower_issues(v)


def test_copy_duplicate_name_trap_blocks():
    # Names the base measure WITHOUT the "(copy)_1" suffix -- the exact v1.43 AAR trap.
    v = check_candidate_references("[Quantity Difference vs Previous Year] + 1", _surface())
    assert v["ok"] is False, v
    iss = _lower_issues(v)
    assert "no such measure or column" in iss
    assert "copy" in iss  # suggests the real (copy)_1 sibling
    assert "duplicate-name trap" in iss


# -- warning (resolves, but advisory) ------------------------------------------------------------
def test_bare_ref_to_stub_measure_ok_with_inert_warning():
    v = check_candidate_references("[Draft Measure] + 1", _surface())
    assert v["ok"] is True, v
    assert "inert" in _lower_warns(v)


def test_unqualified_column_bare_ref_warns_qualify():
    v = check_candidate_references("SUMX('Orders', [Sales])", _surface())
    assert v["ok"] is True, v
    assert "unqualified" in _lower_warns(v)


def test_ambiguous_bare_column_warns():
    v = check_candidate_references("SUMX('Orders', [Region])", _surface())
    assert v["ok"] is True, v
    assert "ambiguous" in _lower_warns(v)


def test_inert_calc_column_bare_ref_warns():
    v = check_candidate_references("SUMX('Orders', [DraftCol])", _surface())
    assert v["ok"] is True, v
    assert "inert" in _lower_warns(v)


def test_table_qualified_measure_ref_ok_with_warning():
    v = check_candidate_references("'_Measures'[Total Sales] * 2", _surface())
    assert v["ok"] is True, v
    assert "table-qualified" in _lower_warns(v)


def test_inert_measure_with_live_near_duplicate_strong_warn():
    # A bare ref to the INERT "Sales Target (copy)_1" while a LIVE "Sales Target" exists.
    v = check_candidate_references("[Sales Target (copy)_1] + 1", _surface())
    assert v["ok"] is True, v
    w = _lower_warns(v)
    assert "inert" in w
    assert "near-duplicate" in w


# -- string masking / extension columns ----------------------------------------------------------
def test_string_literal_not_extracted_as_reference():
    # [Not A Ref] lives INSIDE a string literal -> must not be extracted or blocked.
    v = check_candidate_references('IF(\'Orders\'[Region] = "[Not A Ref]", 1, 0)', _surface())
    assert v["ok"] is True, v
    refs = " ".join(str(r) for r in v["references"]).lower()
    assert "not a ref" not in refs


def test_addcolumns_extension_column_downgraded_to_warning():
    # The ADDCOLUMNS extension column [@value] matches its own "@value" string literal -> WARN,
    # never a BLOCK (it is a locally-defined column, invisible to the model surface).
    cand = ('SUMX(ADDCOLUMNS(\'Orders\', "@value", CALCULATE(SUM(\'Orders\'[Sales]))), [@value])')
    v = check_candidate_references(cand, _surface())
    assert v["ok"] is True, v
    assert "string literal" in _lower_warns(v)


# -- never raises --------------------------------------------------------------------------------
def test_never_raises_on_none():
    v = check_candidate_references(None, _surface())
    assert isinstance(v, dict)
    assert set(v) >= {"ok", "issues", "warnings", "references"}


def test_never_raises_on_garbage():
    v = check_candidate_references("(((('Orders'[", _surface())
    assert isinstance(v, dict)
    assert isinstance(v["ok"], bool)


# -- TMDL-parts surface path ---------------------------------------------------------------------
_TMDL_ORDERS = (
    "table Orders\n"
    "\tlineageTag: 11111111-1111-1111-1111-111111111111\n"
    "\tcolumn Sales\n"
    "\t\tdataType: double\n"
    "\t\tsummarizeBy: sum\n"
    "\tcolumn 'Order Date'\n"
    "\t\tdataType: dateTime\n"
    "\tcolumn Margin = 'Orders'[Sales] * 0.1\n"
    "\t\tdataType: double\n"
)
_TMDL_MEASURES = (
    "table _Measures\n"
    "\tmeasure 'Total Sales' = SUM('Orders'[Sales])\n"
    "\t\tlineageTag: 22222222-2222-2222-2222-222222222222\n"
    "\tmeasure 'Draft Measure' = 0\n"
    "\t\tlineageTag: 33333333-3333-3333-3333-333333333333\n"
)


def test_build_surface_from_tmdl_parts_validates_ref():
    s = build_model_surface(tmdl_parts={"Orders.tmdl": _TMDL_ORDERS,
                                         "_Measures.tmdl": _TMDL_MEASURES})
    assert s["source"] == "tmdl"
    assert "orders" in s["tables"]
    assert "_measures" in s["tables"]
    ok = check_candidate_references("SUM('Orders'[Sales]) + [Total Sales]", s)
    assert ok["ok"] is True, ok
    bad = check_candidate_references("SUM('Orders'[Nope])", s)
    assert bad["ok"] is False, bad


def test_tmdl_stub_measure_is_inert():
    s = build_model_surface(tmdl_parts={"Orders.tmdl": _TMDL_ORDERS,
                                        "_Measures.tmdl": _TMDL_MEASURES})
    v = check_candidate_references("[Draft Measure] + 1", s)
    assert v["ok"] is True, v
    assert "inert" in _lower_warns(v)


def test_tmdl_calc_column_reference_ok():
    s = build_model_surface(tmdl_parts={"Orders.tmdl": _TMDL_ORDERS,
                                        "_Measures.tmdl": _TMDL_MEASURES})
    v = check_candidate_references("SUMX('Orders', 'Orders'[Margin])", s)
    assert v["ok"] is True, v
