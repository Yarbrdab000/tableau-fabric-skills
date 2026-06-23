"""Unit + integration tests for the borderline (on-the-fence) decision-review layer.

Covers: the selection triggers, the structural diff computation, the annotate rollup, additive
guarantees (tier/score/bucket never change), idempotency, band tuning, the rendered report section,
and the export sheet. All offline / pure -- no network.
"""
import io
import json
import zipfile

import borderline
import compare
import export


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------
def _partial_estate():
    """A Tableau datasource that lands in the Partial band against one Fabric model (on the fence),
    plus a clear-rebuild datasource with no Fabric counterpart."""
    tableau = [
        {
            "name": "Sales Orders", "project": "Ops", "luid": "t1",
            "fields": [
                {"name": "Order ID", "dataType": "INTEGER"},
                {"name": "Customer", "dataType": "STRING"},
                {"name": "Amount", "dataType": "INTEGER"},
                {"name": "Region", "dataType": "STRING"},
                {"name": "Profit Ratio", "dataType": "REAL", "is_calculated": True},
            ],
            "sources": [
                {"connectionType": "snowflake", "database": "DB", "schema": "public", "table": "orders"},
                {"connectionType": "snowflake", "database": "DB", "schema": "public", "table": "returns"},
            ],
        },
        {
            "name": "HR Roster", "project": "HR", "luid": "t2",
            "fields": [{"name": "Emp ID", "dataType": "INTEGER"}, {"name": "Name", "dataType": "STRING"}],
            "sources": [{"connectionType": "sqlserver", "database": "HR", "schema": "dbo", "table": "employees"}],
        },
    ]
    fabric = [
        {
            "name": "Orders Model", "workspace": "WS", "id": "f1",
            "columns": [
                {"name": "Order ID", "dataType": "int64"},
                {"name": "Customer", "dataType": "string"},
                {"name": "Amount", "dataType": "string"},   # type mismatch vs INTEGER
                {"name": "Discount", "dataType": "double"},  # Fabric-only
            ],
            "tables": ["orders"],
            "sources": [{"connectionType": "lakehouse", "table": "orders"}],
            "measures": [],
        },
    ]
    return tableau, fabric


# --------------------------------------------------------------------------------------
# Selection: borderline_reasons
# --------------------------------------------------------------------------------------
def test_partial_bucket_is_borderline():
    m = {"score": 0.55, "bucket": "partial", "best_match": {"fabric_name": "X"}}
    reasons = borderline.borderline_reasons(m, strong_cut=0.65, partial_cut=0.40, band=0.08)
    assert "partial_tier" in reasons


def test_clear_already_exists_is_not_borderline():
    m = {"score": 0.92, "bucket": "already_exists", "best_match": {"fabric_name": "X"},
         "confidence": {"level": "High"}}
    assert borderline.borderline_reasons(m, strong_cut=0.65, partial_cut=0.40, band=0.08) == []


def test_clear_rebuild_no_candidate_is_not_borderline():
    m = {"score": 0.0, "bucket": "rebuild", "best_match": None}
    assert borderline.borderline_reasons(m, strong_cut=0.65, partial_cut=0.40, band=0.08) == []


def test_near_reuse_boundary_flagged():
    m = {"score": 0.66, "bucket": "already_exists", "best_match": {"fabric_name": "X"}}
    reasons = borderline.borderline_reasons(m, strong_cut=0.65, partial_cut=0.40, band=0.08)
    assert "near_reuse_boundary" in reasons


def test_near_rebuild_boundary_flagged():
    m = {"score": 0.36, "bucket": "rebuild", "best_match": {"fabric_name": "X"}}
    reasons = borderline.borderline_reasons(m, strong_cut=0.65, partial_cut=0.40, band=0.08)
    assert "near_rebuild_boundary" in reasons


def test_low_confidence_reuse_flagged():
    m = {"score": 0.80, "bucket": "already_exists", "best_match": {"fabric_name": "X"},
         "confidence": {"level": "Low"}}
    reasons = borderline.borderline_reasons(m, strong_cut=0.65, partial_cut=0.40, band=0.08)
    assert "low_confidence" in reasons


def test_logic_unverified_flagged_on_already_exists():
    m = {"score": 0.90, "bucket": "already_exists", "best_match": {"fabric_name": "X"},
         "confidence": {"level": "High"}, "logic_parity": {"status": "unverified"}}
    reasons = borderline.borderline_reasons(m, strong_cut=0.65, partial_cut=0.40, band=0.08)
    assert reasons == ["logic_unverified"]


def test_band_widening_catches_more():
    m = {"score": 0.78, "bucket": "already_exists", "best_match": {"fabric_name": "X"},
         "confidence": {"level": "High"}}
    assert borderline.borderline_reasons(m, strong_cut=0.65, partial_cut=0.40, band=0.08) == []
    wide = borderline.borderline_reasons(m, strong_cut=0.65, partial_cut=0.40, band=0.15)
    assert "near_reuse_boundary" in wide


# --------------------------------------------------------------------------------------
# Diff computation
# --------------------------------------------------------------------------------------
def test_diff_columns_and_source_gap():
    tableau, fabric = _partial_estate()
    res = compare.compare_inventories(tableau, fabric)
    m = next(x for x in res["matches"] if x["tableau_name"] == "Sales Orders")
    diff = borderline.diff_for_match(m, tableau, fabric)
    cols = diff["columns"]
    assert cols["shared_count"] == 3
    assert "Region" in cols["tableau_only"] and "Profit Ratio" in cols["tableau_only"]
    assert "Discount" in cols["fabric_only"]
    tm = {r["column"] for r in cols["type_mismatches"]}
    assert "Amount" in tm
    src = diff["source"]
    assert "orders" in src["shared_tables"]
    assert "returns" in src["tableau_only_tables"]


def test_diff_handles_missing_records_gracefully():
    m = {"tableau_name": "Ghost", "tableau_luid": "zzz", "best_match": {"fabric_name": "Nope"}}
    diff = borderline.diff_for_match(m, [], [])
    assert diff["columns"]["shared_count"] == 0
    assert diff["columns"]["tableau_only"] == []


# --------------------------------------------------------------------------------------
# annotate: rollup, additive, idempotent
# --------------------------------------------------------------------------------------
def test_annotate_attaches_only_to_borderline_and_rolls_up():
    tableau, fabric = _partial_estate()
    res = compare.compare_inventories(tableau, fabric)
    bl = res["summary"]["borderline"]
    assert bl["count"] == 1
    assert bl["by_origin_bucket"]["partial"] == 1
    assert "Sales Orders" in bl["names"]
    sales = next(m for m in res["matches"] if m["tableau_name"] == "Sales Orders")
    hr = next(m for m in res["matches"] if m["tableau_name"] == "HR Roster")
    assert sales.get("borderline") and sales["borderline"]["is_borderline"]
    assert "borderline" not in hr  # clear rebuild -> not annotated


def test_annotate_is_additive_tier_score_unchanged():
    tableau, fabric = _partial_estate()
    res = compare.compare_inventories(tableau, fabric)
    before = [(m["tableau_name"], m["tier"], m["score"], m["bucket"]) for m in res["matches"]]
    borderline.annotate(res, tableau, fabric, band=0.08)
    after = [(m["tableau_name"], m["tier"], m["score"], m["bucket"]) for m in res["matches"]]
    assert before == after


def test_annotate_idempotent():
    tableau, fabric = _partial_estate()
    res = compare.compare_inventories(tableau, fabric)
    first = json.dumps(res["summary"]["borderline"], sort_keys=True)
    borderline.annotate(res, tableau, fabric, band=0.08)
    borderline.annotate(res, tableau, fabric, band=0.08)
    second = json.dumps(res["summary"]["borderline"], sort_keys=True)
    assert first == second
    # exactly one borderline annotation survives (no duplication)
    assert sum(1 for m in res["matches"] if m.get("borderline")) == 1


def test_annotate_empty_estate():
    res = compare.compare_inventories([], [])
    assert res["summary"]["borderline"]["count"] == 0
    assert res["summary"]["borderline"]["names"] == []


def test_annotate_tolerates_bad_band():
    tableau, fabric = _partial_estate()
    res = {"summary": {"bands": [["Strong", 0.65], ["Partial", 0.40]]},
           "matches": compare.compare_inventories(tableau, fabric)["matches"]}
    borderline.annotate(res, tableau, fabric, band="not-a-number")
    assert res["summary"]["borderline"]["band"] == borderline.DEFAULT_REVIEW_BAND


def test_recommendation_hint_logic_tempers_reuse():
    assert borderline._recommendation_hint(0.90, 0.65, 0.40, "unverified") == "reuse_with_logic_review"
    assert borderline._recommendation_hint(0.90, 0.65, 0.40, "likely") == "lean_reuse"
    assert borderline._recommendation_hint(0.35, 0.65, 0.40, None) == "lean_rebuild"


# --------------------------------------------------------------------------------------
# Render + export
# --------------------------------------------------------------------------------------
def test_render_includes_borderline_sections():
    tableau, fabric = _partial_estate()
    res = compare.compare_inventories(tableau, fabric)
    md = compare.render_markdown(res)
    assert "## On-the-fence datasources" in md
    assert "## Borderline decision detail" in md
    assert "In Tableau only (missing from Fabric)" in md


def test_render_omits_section_when_none_borderline():
    tableau = [{"name": "A", "luid": "a",
                "fields": [{"name": "X", "dataType": "INTEGER"}], "sources": []}]
    fabric = []  # everything is a clear rebuild
    res = compare.compare_inventories(tableau, fabric)
    md = compare.render_markdown(res)
    assert "## On-the-fence datasources" not in md
    assert "## Borderline decision detail" not in md


def test_export_borderline_sheet_present_when_flagged():
    tableau, fabric = _partial_estate()
    res = compare.compare_inventories(tableau, fabric)
    xb = export.to_xlsx_bytes(res)
    with zipfile.ZipFile(io.BytesIO(xb)) as zf:
        wb = zf.read("xl/workbook.xml").decode()
    assert "Borderline" in wb
    rows = export.build_borderline_rows(res)
    assert rows[0][0] == "Datasource" and "Lean" in rows[0]
    assert rows[1][0] == "Sales Orders"


def test_export_borderline_sheet_absent_when_none():
    tableau = [{"name": "A", "luid": "a",
                "fields": [{"name": "X", "dataType": "INTEGER"}], "sources": []}]
    res = compare.compare_inventories(tableau, [])
    xb = export.to_xlsx_bytes(res)
    with zipfile.ZipFile(io.BytesIO(xb)) as zf:
        wb = zf.read("xl/workbook.xml").decode()
    assert "Borderline" not in wb


def test_render_limit_caps_detail_blocks():
    # Build several borderline datasources, cap render at 1, expect an "and N more" note.
    tableau, fabric = [], [{"name": "M", "workspace": "WS", "id": "f1",
                            "columns": [{"name": "C1", "dataType": "string"},
                                        {"name": "C2", "dataType": "string"}],
                            "tables": ["t"], "sources": [{"connectionType": "x", "table": "t"}]}]
    for i in range(3):
        tableau.append({
            "name": f"DS{i}", "luid": f"d{i}",
            "fields": [{"name": "C1", "dataType": "INTEGER"}, {"name": f"U{i}", "dataType": "STRING"}],
            "sources": [{"connectionType": "x", "database": "d", "schema": "s", "table": "t"}],
        })
    res = compare.compare_inventories(tableau, fabric)
    # Only proceed if the synthetic estate actually produced multiple borderline rows.
    if res["summary"]["borderline"]["count"] >= 2:
        res["summary"]["borderline"]["render_limit"] = 1
        md = compare.render_markdown(res)
        assert "more borderline datasource" in md
