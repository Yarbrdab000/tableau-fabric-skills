"""Unit tests for the executive CSV / XLSX export in ``export.py`` (standard-library only)."""
import csv
import io
import zipfile
import xml.dom.minidom as minidom

import export


def _result(matches=None, summary=None):
    base_summary = {
        "tableau_total": 2, "fabric_total": 3,
        "by_tier": {"Exact": 1, "Strong": 0, "Partial": 0, "Weak": 0, "None": 1},
        "already_exist": 1, "partial": 0, "rebuild": 1,
        "distinct_fabric_matched": 1,
        "assignment": {"already_exist": 1, "partial": 0, "rebuild": 1},
        "fabric_coverage": {
            "fabric_total": 3, "matched_models": 1, "unmatched_models": 2,
            "unmatched_model_names": [
                {"fabric_name": "Net New", "workspace": "WS1"},
                {"fabric_name": "Another", "workspace": "WS2"},
            ],
        },
        "logic_parity": {"none": 0, "likely": 1, "partial": 0, "unverified": 1, "review_needed": 1},
        "by_migration_priority": {"P1 - migrate first": 1, "Reuse (already in Fabric)": 1},
    }
    if summary:
        base_summary.update(summary)
    base_matches = matches if matches is not None else [
        {
            "tableau_name": "Azure SQL - Superstore", "project": "default",
            "tier": "Exact", "score": 0.912, "bucket": "already_exists",
            "source_compared": True, "priority": "High",
            "migration_priority": "Reuse (already in Fabric)",
            "usage": {"workbook_count": 12},
            "best_match": {"fabric_name": "Azure SQL - Superstore", "workspace": "GH-WS",
                           "shared_column_count": 18},
            "logic_parity": {"status": "partial", "tableau_calc_count": 3, "matched": 2},
            "verification": {"verdict": "verified"},
            "confidence": {"level": "High", "drivers": [], "cautions": [], "margin": 0.2,
                           "corroborating_signals": 3, "reciprocal_best": True},
            "reason": "exact name -- Exact.",
        },
        {
            "tableau_name": "Orders & \"Returns\"", "project": "Sales",
            "tier": "None", "score": 0.0, "bucket": "rebuild",
            "source_compared": False, "priority": "Unknown",
            "migration_priority": "P1 - migrate first",
            "usage": {"workbook_count": None},
            "best_match": None, "logic_parity": None, "verification": {},
            "reason": "no comparable model -- None.",
        },
    ]
    return {"summary": base_summary, "matches": base_matches}


# --------------------------------------------------------------------------------------
# Detail rows
# --------------------------------------------------------------------------------------
def test_build_detail_rows_header_and_one_row_per_match():
    rows = export.build_detail_rows(_result())
    assert rows[0] == [h for h, _ in export._DETAIL_COLUMNS]
    assert len(rows) == 3  # header + 2 datasources


def test_detail_row_friendly_verdict_and_rounded_score():
    rows = export.build_detail_rows(_result())
    header = rows[0]
    first = dict(zip(header, rows[1]))
    assert first["Verdict"] == "Already in Fabric"
    assert first["Score"] == 0.912
    assert first["Best Fabric match"] == "Azure SQL - Superstore"
    assert first["Fabric workspace"] == "GH-WS"
    assert first["Source compared"] is True
    assert first["Shared columns"] == 18
    assert first["Usage (workbooks)"] == 12
    assert first["Logic parity"] == "partial"
    assert first["Calc fields"] == 3
    assert first["Calcs matched as measures"] == 2
    assert first["Verification"] == "verified"
    assert first["Confidence"] == "High"


def test_detail_row_handles_no_match_and_null_logic_parity():
    rows = export.build_detail_rows(_result())
    second = dict(zip(rows[0], rows[2]))
    assert second["Verdict"] == "Needs rebuild"
    assert second["Best Fabric match"] == ""
    assert second["Fabric workspace"] == ""
    assert second["Source compared"] is False
    assert second["Shared columns"] is None
    assert second["Usage (workbooks)"] is None  # workbook_count None stays None
    assert second["Logic parity"] == ""
    assert second["Calc fields"] is None


def test_build_detail_rows_empty_matches_is_header_only():
    rows = export.build_detail_rows({"summary": {}, "matches": []})
    assert len(rows) == 1


# --------------------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------------------
def test_to_csv_round_trips_and_renders_yes_no():
    text = export.to_csv(_result())
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0][0] == "Tableau datasource"
    row1 = dict(zip(parsed[0], parsed[1]))
    assert row1["Source compared"] == "Yes"
    row2 = dict(zip(parsed[0], parsed[2]))
    assert row2["Source compared"] == "No"
    # empty cells for the no-match row
    assert row2["Best Fabric match"] == ""


def test_to_csv_quotes_embedded_quotes_and_specials():
    text = export.to_csv(_result())
    parsed = list(csv.reader(io.StringIO(text)))
    names = [r[0] for r in parsed[1:]]
    assert 'Orders & "Returns"' in names  # csv module unescapes the doubled quotes


def test_write_csv_uses_bom_for_excel(tmp_path):
    p = tmp_path / "out.csv"
    export.write_csv(_result(), str(p))
    raw = p.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM so Excel detects encoding


# --------------------------------------------------------------------------------------
# Summary / coverage
# --------------------------------------------------------------------------------------
def test_build_summary_rows_has_headline_metrics():
    rows, bold = export.build_summary_rows(_result())
    flat = {r[0]: r[1] for r in rows if r[0]}
    assert flat["Tableau datasources"] == 2
    assert flat["Already in Fabric (reuse)"] == 1
    assert flat["Needs rebuild"] == 1
    assert flat["Already in Fabric %"] == "50%"
    assert flat["Needs rebuild %"] == "50%"
    assert flat["Logic-parity review needed"] == 1
    assert flat["Net-new Fabric models (unmatched)"] == 2
    # section headers are marked bold
    assert 0 in bold
    header_labels = {rows[i][0] for i in bold}
    assert "Estate migration sizing" in header_labels
    assert "By tier" in header_labels


def test_summary_percentages_safe_when_zero_datasources():
    rows, _ = export.build_summary_rows({"summary": {"tableau_total": 0}})
    flat = {r[0]: r[1] for r in rows if r[0]}
    assert flat["Already in Fabric %"] == "0%"


def test_summary_includes_confidence_metrics_when_present():
    rows, _ = export.build_summary_rows(_result(summary={
        "confidence": {"high": 4, "medium": 1, "low": 2,
                       "high_confidence_already_exists": 3, "low_confidence_review": 2}}))
    flat = {r[0]: r[1] for r in rows if r[0]}
    assert flat["High-confidence verdicts"] == 4
    assert flat["Low-confidence (review)"] == 2

    rows2, _ = export.build_summary_rows(_result())
    assert "High-confidence verdicts" not in {r[0] for r in rows2}


def test_summary_includes_verification_block_only_when_enabled():
    rows, _ = export.build_summary_rows(_result(summary={
        "verification": {"enabled": True, "verified": 2, "compatible": 0,
                         "mismatch": 1, "inconclusive": 0}}))
    labels = {r[0] for r in rows}
    assert "Empirical verification" in labels
    assert "verified" in labels

    rows2, _ = export.build_summary_rows(_result())
    assert "Empirical verification" not in {r[0] for r in rows2}


def test_build_coverage_rows_lists_unmatched_models():
    rows = export.build_coverage_rows(_result())
    assert rows[0] == ["Fabric model", "Workspace"]
    assert ["Net New", "WS1"] in rows


def test_coverage_rows_placeholder_when_full_coverage():
    rows = export.build_coverage_rows({"summary": {"fabric_coverage": {"unmatched_model_names": []}}})
    assert len(rows) == 2
    assert "every Fabric model" in rows[1][0]


# --------------------------------------------------------------------------------------
# XLSX assembly
# --------------------------------------------------------------------------------------
def test_to_xlsx_bytes_is_valid_zip_with_expected_parts():
    data = export.to_xlsx_bytes(_result())
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.testzip() is None
    names = set(zf.namelist())
    for required in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                     "xl/_rels/workbook.xml.rels", "xl/styles.xml",
                     "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml",
                     "xl/worksheets/sheet3.xml"):
        assert required in names


def test_xlsx_all_xml_parts_are_well_formed():
    data = export.to_xlsx_bytes(_result())
    zf = zipfile.ZipFile(io.BytesIO(data))
    for name in zf.namelist():
        if name.endswith(".xml") or name.endswith(".rels"):
            minidom.parseString(zf.read(name))  # raises if malformed


def test_xlsx_workbook_declares_three_named_sheets():
    data = export.to_xlsx_bytes(_result())
    zf = zipfile.ZipFile(io.BytesIO(data))
    wb = zf.read("xl/workbook.xml").decode("utf-8")
    for sheet in ("Summary", "Datasources", "Fabric coverage"):
        assert ('name="%s"' % sheet) in wb


def test_xlsx_numbers_written_as_numeric_strings_are_escaped():
    # the datasource name has an embedded quote -> must be XML-escaped in the sheet
    data = export.to_xlsx_bytes(_result())
    zf = zipfile.ZipFile(io.BytesIO(data))
    sheet2 = zf.read("xl/worksheets/sheet2.xml").decode("utf-8")
    assert "&quot;Returns&quot;" in sheet2
    # score 0.912 is a numeric cell (no inlineStr wrapper for it)
    assert "<v>0.912</v>" in sheet2


def test_xlsx_handles_empty_estate():
    data = export.to_xlsx_bytes({"summary": {"tableau_total": 0}, "matches": []})
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.testzip() is None
    for name in zf.namelist():
        if name.endswith(".xml") or name.endswith(".rels"):
            minidom.parseString(zf.read(name))


def test_write_xlsx_writes_binary_file(tmp_path):
    p = tmp_path / "estate.xlsx"
    export.write_xlsx(_result(), str(p))
    raw = p.read_bytes()
    assert raw[:2] == b"PK"  # zip signature
    zipfile.ZipFile(io.BytesIO(raw)).testzip()


# --------------------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------------------
def test_col_letter():
    assert export._col_letter(0) == "A"
    assert export._col_letter(25) == "Z"
    assert export._col_letter(26) == "AA"
    assert export._col_letter(27) == "AB"


def test_xml_escape_handles_specials_and_strips_control_chars():
    assert export._xml_escape("a&b<c>\"d'") == "a&amp;b&lt;c&gt;&quot;d&apos;"
    assert export._xml_escape("ok\x00bad") == "okbad"  # NUL stripped, tab/newline kept


def test_sanitize_sheet_name_truncates_and_strips_illegal():
    assert export._sanitize_sheet_name("a/b:c*d?e[f]") == "a_b_c_d_e_f_"
    assert len(export._sanitize_sheet_name("x" * 50)) == 31


# --------------------------------------------------------------------------------------
# Importance + connected assets (Phase 2)
# --------------------------------------------------------------------------------------
def _telemetry_result():
    """A result whose first match carries importance + connected-asset telemetry."""
    return _result(
        matches=[
            {
                "tableau_name": "Sales Orders", "project": "Sales",
                "tier": "Exact", "score": 0.95, "bucket": "already_exists",
                "source_compared": True, "priority": "High",
                "migration_priority": "Reuse (already in Fabric)",
                "usage": {
                    "workbook_count": 9, "dashboard_count": 4, "view_count": 1800,
                    "certified": True, "has_quality_warning": False,
                    "extract_last_refresh": "2026-06-20T03:00:00Z",
                    "connected_assets": {
                        "workbooks": [{"name": "Exec KPIs", "luid": "w1"},
                                      {"name": "Pipeline", "luid": "w2"}],
                        "dashboards": [{"name": "Daily Sales"}],
                    },
                },
                "best_match": {"fabric_name": "Sales Orders", "workspace": "WS"},
                "importance": {"level": "Critical", "score": 0.88, "drivers": ["certified"]},
                "reason": "exact name -- Exact.",
            },
            {
                "tableau_name": "Tiny DS", "project": "Misc",
                "tier": "None", "score": 0.0, "bucket": "rebuild",
                "source_compared": False, "priority": "Low",
                "migration_priority": "P3",
                "usage": {"workbook_count": 0, "dashboard_count": 0, "view_count": 2,
                          "certified": False, "connected_assets": None},
                "best_match": None,
                "importance": {"level": "Low", "score": 0.01, "drivers": []},
                "reason": "no comparable model -- None.",
            },
        ],
        summary={"importance": {
            "by_level": {"Critical": 1, "High": 0, "Moderate": 0, "Low": 1, "Unknown": 0},
            "critical": 1, "high": 0, "total_views": 1802,
            "certified_datasources": 1, "datasources_with_quality_warning": 0,
        }},
    )


def test_detail_rows_have_importance_views_and_certified():
    rows = export.build_detail_rows(_telemetry_result())
    header = rows[0]
    assert "Importance" in header and "Views" in header and "Certified" in header
    first = dict(zip(header, rows[1]))
    assert first["Importance"] == "Critical"
    assert first["Views"] == 1800
    assert first["Certified"] is True


def test_summary_includes_importance_metrics_when_present():
    rows, _ = export.build_summary_rows(_telemetry_result())
    flat = {r[0]: r[1] for r in rows if r[0]}
    assert flat["Critical-importance datasources"] == 1
    assert flat["Total views (estate)"] == 1802
    assert flat["Certified datasources"] == 1
    # absent when no importance rollup
    rows2, _ = export.build_summary_rows(_result())
    assert "Critical-importance datasources" not in {r[0] for r in rows2}


def test_connected_assets_rows_one_row_per_asset():
    rows = export.build_connected_assets_rows(_telemetry_result())
    assert rows[0] == ["Datasource", "Importance", "Views", "Asset type",
                       "Asset name", "Last refreshed"]
    body = rows[1:]
    names = {r[4] for r in body}
    assert {"Exec KPIs", "Pipeline", "Daily Sales"} <= names
    types = {r[3] for r in body}
    assert types == {"Workbook", "Dashboard"}
    # the importance-ordered datasource shows its refresh date truncated to a day
    assert body[0][5] == "2026-06-20"


def test_connected_assets_rows_placeholder_when_no_telemetry():
    rows = export.build_connected_assets_rows(_result())
    assert len(rows) == 2
    assert "no connected-asset telemetry" in rows[1][0]


def test_xlsx_adds_connected_assets_sheet_only_with_telemetry():
    with_tel = export.to_xlsx_bytes(_telemetry_result())
    wb = zipfile.ZipFile(io.BytesIO(with_tel)).read("xl/workbook.xml").decode("utf-8")
    assert 'name="Connected assets"' in wb
    # four declared sheets -> sheet4.xml present and well-formed
    zf = zipfile.ZipFile(io.BytesIO(with_tel))
    assert "xl/worksheets/sheet4.xml" in zf.namelist()
    for name in zf.namelist():
        if name.endswith(".xml") or name.endswith(".rels"):
            minidom.parseString(zf.read(name))

    without = zipfile.ZipFile(io.BytesIO(export.to_xlsx_bytes(_result())))
    wb2 = without.read("xl/workbook.xml").decode("utf-8")
    assert 'name="Connected assets"' not in wb2
    assert "xl/worksheets/sheet4.xml" not in without.namelist()
