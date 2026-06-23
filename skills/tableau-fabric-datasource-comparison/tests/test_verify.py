"""Offline tests for the empirical-verification (Tier-2) layer.

Every test injects fake probe callables, so the windowed-overlap verdict logic is exercised with no
network. The probes are keyed by ``(function_token, name, windowed)`` so a test can make one side a
superset of the other and confirm that still *verifies* instead of looking like a mismatch.
"""
import copy

import pytest

import verify
from verify import (
    FUNC_DISTINCT,
    FUNC_MAX,
    FUNC_MIN,
    FUNC_SUM,
    build_dax,
    build_vds_query,
    column_kind,
    compare_values,
    parse_executequeries_scalar,
    parse_vds_scalar,
    plan_probes,
    vds_function,
    verify_estate,
    verify_match,
    verification_note,
)


# ---------------------------------------------------------------------------------------
# Fake probe builders. Keys: (token, name, windowed_bool).
#   tableau token = VDS token (MIN/MAX/SUM/COUNTD); name = field caption.
#   fabric token  = canonical function (MIN/MAX/SUM/DISTINCTCOUNT); name = column.
# ---------------------------------------------------------------------------------------
def make_tableau_probe(values, errors=None):
    errors = errors or {}

    def probe(luid, field, vds_func, window=None):
        key = (vds_func, field, window is not None)
        if key in errors:
            return (None, errors[key])
        if key in values:
            return (values[key], None)
        return (None, "no-tableau-data")

    return probe


def make_fabric_probe(values, errors=None):
    errors = errors or {}

    def probe(workspace_id, dataset_id, table, column, function, window=None):
        key = (function, column, window is not None)
        if key in errors:
            return (None, errors[key])
        if key in values:
            return (values[key], None)
        return (None, "no-fabric-data")

    return probe


def always_error(msg="boom"):
    def probe(*args, **kwargs):
        return (None, msg)
    return probe


# ======================================================================================
# column_kind
# ======================================================================================
def test_column_kind_date_when_both_date():
    assert column_kind("DATETIME", "datetime") == "date"


def test_column_kind_numeric_when_both_numeric():
    assert column_kind("REAL", "double") == "numeric"


def test_column_kind_other_when_types_disagree():
    assert column_kind("REAL", "string") == "other"


def test_column_kind_other_when_both_unknown():
    assert column_kind(None, None) == "other"


def test_column_kind_unknown_one_side_inherits_other():
    # an unknown/blank on one side is treated as compatible with the known kind
    assert column_kind("DATE", "") == "date"
    assert column_kind("", "double") == "numeric"


# ======================================================================================
# plan_probes
# ======================================================================================
def _ds(fields):
    return {"name": "DS", "luid": "t-1", "fields": fields}


def _model(columns):
    return {"name": "M", "id": "m-1", "workspaceId": "w-1", "columns": columns}


def test_plan_probes_date_window_first_and_sum_for_numeric():
    ds = _ds([{"name": "Order Date", "dataType": "DATE"}, {"name": "Sales", "dataType": "REAL"}])
    model = _model([
        {"name": "Order Date", "dataType": "datetime", "table": "Orders"},
        {"name": "Sales", "dataType": "double", "table": "Orders"},
    ])
    plan = plan_probes(ds, model)
    kinds = [c["kind"] for c in plan["window_candidates"]]
    assert kinds and kinds[0] == "date"  # date preferred for windowing
    funcs = {(p["column"], p["function"]) for p in plan["equality_probes"]}
    assert ("sales", FUNC_SUM) in funcs            # SUM only for the numeric column
    assert ("sales", FUNC_DISTINCT) in funcs
    assert ("order date", FUNC_SUM) not in funcs   # never SUM a date


def test_plan_probes_skips_fabric_column_without_table():
    ds = _ds([{"name": "Sales", "dataType": "REAL"}])
    model = _model([{"name": "Sales", "dataType": "double"}])  # no "table"
    plan = plan_probes(ds, model)
    assert plan["window_candidates"] == []
    assert plan["equality_probes"] == []


def test_plan_probes_no_shared_columns_is_empty():
    ds = _ds([{"name": "Alpha", "dataType": "REAL"}])
    model = _model([{"name": "Beta", "dataType": "double", "table": "T"}])
    plan = plan_probes(ds, model)
    assert plan == {"window_candidates": [], "equality_probes": []}


def test_plan_probes_string_column_distinct_only():
    ds = _ds([{"name": "Region", "dataType": "STRING"}])
    model = _model([{"name": "Region", "dataType": "string", "table": "Geo"}])
    plan = plan_probes(ds, model)
    assert plan["window_candidates"] == []  # strings are not window candidates
    assert [p["function"] for p in plan["equality_probes"]] == [FUNC_DISTINCT]


def test_plan_probes_caps_at_max_cols_distinctive_first():
    cols = [{"name": n, "dataType": "REAL"} for n in ("id", "amount", "qty")]
    fcols = [{"name": n, "dataType": "double", "table": "T"} for n in ("id", "amount", "qty")]
    plan = plan_probes(_ds(cols), _model(fcols), max_cols=1)
    used = {p["column"] for p in plan["equality_probes"]}
    assert len(used) == 1
    assert "id" not in used  # generic token deprioritised, distinctive column kept


def test_plan_probes_excludes_numeric_measure_from_window():
    # A numeric *measure* must never be a window axis (self-referential filtering); a numeric
    # *dimension* (e.g. a year key) is a valid fallback axis. Both still get equality probes.
    ds = _ds([
        {"name": "Sales", "dataType": "REAL", "role": "measure"},
        {"name": "Fiscal Year", "dataType": "INTEGER", "role": "dimension"},
    ])
    model = _model([
        {"name": "Sales", "dataType": "double", "table": "Orders"},
        {"name": "Fiscal Year", "dataType": "int64", "table": "Orders"},
    ])
    plan = plan_probes(ds, model)
    win_cols = {c["column"] for c in plan["window_candidates"]}
    assert win_cols == {"fiscalyear"}                  # measure excluded, dimension kept
    funcs = {(p["column"], p["function"]) for p in plan["equality_probes"]}
    assert ("sales", FUNC_SUM) in funcs                 # measure still summed within the window


def test_plan_probes_measure_only_yields_no_window():
    # When every shared numeric column is a measure, establish no window -> safe containment mode
    # rather than a bogus self-referential window that could flag a Fabric superset as a mismatch.
    ds = _ds([
        {"name": "Sales", "dataType": "REAL", "role": "measure"},
        {"name": "Profit", "dataType": "REAL", "role": "measure"},
    ])
    model = _model([
        {"name": "Sales", "dataType": "double", "table": "Orders"},
        {"name": "Profit", "dataType": "double", "table": "Orders"},
    ])
    plan = plan_probes(ds, model)
    assert plan["window_candidates"] == []
    assert {(p["column"], p["function"]) for p in plan["equality_probes"]} >= {
        ("sales", FUNC_SUM), ("profit", FUNC_SUM)}


def test_measure_only_superset_is_compatible_not_false_mismatch():
    # End-to-end of the user's scenario: same datasource, Fabric just has more rows, and the only
    # shared numeric column is the measure SUM(Sales). With measures barred as window axes we drop
    # to containment, where a one-directional volume difference reads "compatible" -- never a
    # mismatch. (Had Sales been used as a window axis, the windowed sums could falsely disagree.)
    ds = _ds([{"name": "Sales", "dataType": "REAL", "role": "measure"}])
    model = _model([{"name": "Sales", "dataType": "double", "table": "Orders"}])
    plan = plan_probes(ds, model)
    assert plan["window_candidates"] == []
    t = make_tableau_probe({("SUM", "Sales", False): 1000})
    f = make_fabric_probe({("SUM", "Sales", False): 1500})  # Fabric superset
    v = verify_match("t-1", "w-1", "m-1", plan, t, f)
    assert v["verdict"] == "compatible"
    assert v["method"] == "containment"
    assert v["probes_disagreed"] == 0


# ======================================================================================
# request builders + envelope parsers
# ======================================================================================
def test_build_vds_query_unwindowed():
    q = build_vds_query("Sales", "SUM")
    assert q["fields"] == [{"fieldCaption": "Sales", "function": "SUM", "fieldAlias": "a0"}]
    assert "filters" not in q


def test_build_vds_query_windowed_date_filter():
    window = {"kind": "date", "min": "2021-01-01", "max": "2026-12-31", "tableau_field": "Order Date"}
    q = build_vds_query("Sales", "SUM", window)
    flt = q["filters"][0]
    assert flt["filterType"] == "QUANTITATIVE_DATE"
    assert flt["quantitativeFilterType"] == "RANGE"
    assert flt["minDate"] == "2021-01-01" and flt["maxDate"] == "2026-12-31"
    assert flt["field"]["fieldCaption"] == "Order Date"


def test_build_dax_unwindowed_scalar():
    dax = build_dax(FUNC_SUM, "Orders", "Sales")
    assert dax == 'EVALUATE ROW("v", SUM(\'Orders\'[Sales]))'


def test_build_dax_windowed_date_calculate():
    window = {"kind": "date", "min": "2021-01-01", "max": "2026-12-31",
              "fabric_table": "Orders", "fabric_column": "OrderDate"}
    dax = build_dax(FUNC_SUM, "Orders", "Sales", window)
    assert "CALCULATE(SUM('Orders'[Sales])" in dax
    assert "DATE(2021,1,1)" in dax and "DATE(2026,12,31)" in dax


def test_build_dax_escapes_quotes_and_brackets():
    dax = build_dax(FUNC_SUM, "O'Brien", "Amt]x")
    assert "'O''Brien'" in dax and "Amt]]x" in dax


def test_vds_function_maps_distinct_to_countd():
    assert vds_function(FUNC_DISTINCT) == "COUNTD"
    assert vds_function(FUNC_SUM) == "SUM"
    assert vds_function(FUNC_MIN) == "MIN"


def test_parse_vds_scalar_happy_and_edge():
    assert parse_vds_scalar([{"a0": 5}]) == (5, None)
    assert parse_vds_scalar([{"justone": 7}]) == (7, None)
    assert parse_vds_scalar(None)[0] is None
    assert parse_vds_scalar([{"a0": 1}, {"a0": 2}])[0] is None  # not aggregatable


def test_parse_executequeries_scalar_happy_and_edge():
    env = {"results": [{"tables": [{"rows": [{"[v]": 9}]}]}]}
    assert parse_executequeries_scalar(env) == (9, None)
    assert parse_executequeries_scalar({"error": {"message": "boom"}}) == (None, "boom")
    assert parse_executequeries_scalar({"results": [{"tables": [{"rows": []}]}]})[0] is None
    assert parse_executequeries_scalar({})[0] is None


# ======================================================================================
# range relationship / overlap math
# ======================================================================================
def test_range_equal_dates():
    rel, lo, hi = verify._range_relationship(
        "2020-01-01", "2026-01-01", "2020-01-01", "2026-01-01", kind="date", rtol=0.01)
    assert rel == "equal" and lo == "2020-01-01" and hi == "2026-01-01"


def test_range_tableau_subset_of_fabric():
    rel, lo, hi = verify._range_relationship(
        "2021-01-01", "2026-12-31", "2019-01-01", "2026-12-31", kind="date", rtol=0.01)
    assert rel == "subset" and lo == "2021-01-01" and hi == "2026-12-31"


def test_range_tableau_superset_of_fabric():
    rel, _, _ = verify._range_relationship(
        "2019-01-01", "2026-12-31", "2021-01-01", "2026-12-31", kind="date", rtol=0.01)
    assert rel == "superset"


def test_range_partial_overlap():
    rel, lo, hi = verify._range_relationship(
        "2018-01-01", "2022-06-30", "2020-01-01", "2026-12-31", kind="date", rtol=0.01)
    assert rel == "partial" and lo == "2020-01-01" and hi == "2022-06-30"


def test_range_disjoint():
    rel, lo, hi = verify._range_relationship(
        "2010-01-01", "2012-12-31", "2020-01-01", "2022-12-31", kind="date", rtol=0.01)
    assert rel == "disjoint" and lo is None and hi is None


def test_range_numeric_equal_within_tolerance():
    rel, _, _ = verify._range_relationship(0, 1000, 0, 1000.5, kind="numeric", rtol=0.01)
    assert rel == "equal"


def test_range_inverted_bounds_are_swapped():
    rel, _, _ = verify._range_relationship(
        "2026-01-01", "2021-01-01", "2019-01-01", "2026-12-31", kind="date", rtol=0.01)
    assert rel == "subset"


# ======================================================================================
# compare_values
# ======================================================================================
def test_compare_values_numeric_within_and_outside_tolerance():
    assert compare_values(FUNC_SUM, 100, 100.5, rtol=0.01) == "agree"
    assert compare_values(FUNC_SUM, 100, 200, rtol=0.01) == "disagree"


def test_compare_values_none_handling():
    assert compare_values(FUNC_SUM, 100, None) == "inconclusive"
    assert compare_values(FUNC_SUM, None, None) == "agree"


def test_compare_values_string_equality():
    assert compare_values(FUNC_MIN, "2021-01-01", "2021-01-01") == "agree"
    assert compare_values(FUNC_MIN, "2021-01-01", "2022-01-01") == "disagree"


# ======================================================================================
# verify_match -- the heart (windowed-overlap + containment)
# ======================================================================================
def _windowed_plan():
    return {
        "window_candidates": [{
            "column": "order date", "tableau_field": "Order Date",
            "fabric_table": "Orders", "fabric_column": "OrderDate", "kind": "date",
        }],
        "equality_probes": [{
            "column": "sales", "tableau_field": "Sales",
            "fabric_table": "Orders", "fabric_column": "Sales", "kind": "numeric", "function": FUNC_SUM,
        }],
    }


def test_verify_match_superset_still_verifies():
    # Fabric holds 2019-2026, Tableau 2021-2026 (same source, more history) -> overlap SUM agrees.
    t = make_tableau_probe({
        ("MIN", "Order Date", False): "2021-01-01",
        ("MAX", "Order Date", False): "2026-12-31",
        ("SUM", "Sales", True): 1000,
    })
    f = make_fabric_probe({
        ("MIN", "OrderDate", False): "2019-01-01",
        ("MAX", "OrderDate", False): "2026-12-31",
        ("SUM", "Sales", True): 1000,
    })
    v = verify_match("t-1", "w-1", "m-1", _windowed_plan(), t, f)
    assert v["verdict"] == "verified"
    assert v["relationship"] == "subset"      # Fabric is the superset
    assert v["method"] == "windowed"
    assert v["probes_disagreed"] == 0 and v["probes_agreed"] == 1


def test_verify_match_disagreement_on_overlap_is_mismatch():
    t = make_tableau_probe({
        ("MIN", "Order Date", False): "2021-01-01",
        ("MAX", "Order Date", False): "2026-12-31",
        ("SUM", "Sales", True): 1000,
    })
    f = make_fabric_probe({
        ("MIN", "OrderDate", False): "2021-01-01",
        ("MAX", "OrderDate", False): "2026-12-31",
        ("SUM", "Sales", True): 5000,
    })
    v = verify_match("t-1", "w-1", "m-1", _windowed_plan(), t, f)
    assert v["verdict"] == "mismatch"
    assert v["probes_disagreed"] == 1


def test_verify_match_disjoint_ranges_short_circuit_mismatch():
    t = make_tableau_probe({
        ("MIN", "Order Date", False): "2010-01-01",
        ("MAX", "Order Date", False): "2012-12-31",
        ("SUM", "Sales", True): 1000,
    })
    f = make_fabric_probe({
        ("MIN", "OrderDate", False): "2020-01-01",
        ("MAX", "OrderDate", False): "2022-12-31",
        ("SUM", "Sales", True): 1000,
    })
    v = verify_match("t-1", "w-1", "m-1", _windowed_plan(), t, f)
    assert v["verdict"] == "mismatch"
    assert v["relationship"] == "disjoint"
    assert v["probes"] == []  # equality probes never ran


def test_verify_match_containment_compatible_when_one_side_bigger():
    # No window column; a string distinct-count differs only by volume -> compatible, not mismatch.
    plan = {"window_candidates": [], "equality_probes": [{
        "column": "region", "tableau_field": "Region",
        "fabric_table": "Geo", "fabric_column": "Region", "kind": "other", "function": FUNC_DISTINCT,
    }]}
    t = make_tableau_probe({("COUNTD", "Region", False): 40})
    f = make_fabric_probe({("DISTINCTCOUNT", "Region", False): 50})
    v = verify_match("t-1", "w-1", "m-1", plan, t, f)
    assert v["verdict"] == "compatible"
    assert v["method"] == "containment"


def test_verify_match_containment_exact_equal_is_verified():
    plan = {"window_candidates": [], "equality_probes": [{
        "column": "region", "tableau_field": "Region",
        "fabric_table": "Geo", "fabric_column": "Region", "kind": "other", "function": FUNC_DISTINCT,
    }]}
    t = make_tableau_probe({("COUNTD", "Region", False): 50})
    f = make_fabric_probe({("DISTINCTCOUNT", "Region", False): 50})
    v = verify_match("t-1", "w-1", "m-1", plan, t, f)
    assert v["verdict"] == "verified"
    assert v["relationship"] == "equal"


def test_verify_match_all_errors_inconclusive():
    v = verify_match("t-1", "w-1", "m-1", _windowed_plan(), always_error(), always_error())
    assert v["verdict"] == "inconclusive"


def test_verify_match_fabric_all_errored_is_unreadable():
    # Tableau returns real values; every Fabric probe errors with a generic DAX failure
    # (e.g. a DirectQuery model whose source connection isn't configured) -> fabric_unreadable.
    t = make_tableau_probe({
        ("MIN", "Order Date", False): "2021-01-01",
        ("MAX", "Order Date", False): "2026-12-31",
        ("SUM", "Sales", True): 1000,
        ("SUM", "Sales", False): 1000,
    })
    f = make_fabric_probe({}, errors={
        ("MIN", "OrderDate", False): "Failed to execute the DAX query.",
        ("MAX", "OrderDate", False): "Failed to execute the DAX query.",
        ("SUM", "Sales", True): "Failed to execute the DAX query.",
        ("SUM", "Sales", False): "Failed to execute the DAX query.",
    })
    v = verify_match("t-1", "w-1", "m-1", _windowed_plan(), t, f)
    assert v["verdict"] == "inconclusive"
    assert v["reason_code"] == "fabric_unreadable"
    assert "could not be queried" in v["notes"][0].lower()
    assert "re-run --verify" in verification_note(v).lower()


def test_verify_match_empty_plan_inconclusive():
    v = verify_match("t-1", "w-1", "m-1", {"window_candidates": [], "equality_probes": []},
                     always_error(), always_error())
    assert v["verdict"] == "inconclusive"
    assert v["probes_run"] == 0


# ======================================================================================
# Fabric "no data / not refreshed" detection (live dry-test hardening)
# ======================================================================================
def _null_fabric_probe():
    """Fabric returned 200 + null for every aggregate (an unrefreshed/empty model)."""
    def probe(workspace_id, dataset_id, table, column, function, window=None):
        return (None, None)
    return probe


def test_is_no_data_error_recognizes_as_refresh_phrase():
    assert verify.is_no_data_error(
        "The expression referenced column 'Orders'[Sales] which does not hold any data "
        "because it needs to be recalculated or refreshed.")
    assert verify.is_no_data_error("Table has not been processed")
    assert not verify.is_no_data_error("executeQueries unauthorized (401)")
    assert not verify.is_no_data_error(None)
    assert not verify.is_no_data_error("")


def test_extract_executequeries_error_digs_nested_detail():
    payload = {"error": {"code": "DatasetExecuteQueriesError", "pbi.error": {
        "code": "DatasetExecuteQueriesError", "details": [
            {"code": "DetailsMessage", "detail": {"type": 1, "value": "needs to be refreshed"}}]}}}
    assert verify.extract_executequeries_error(payload) == "needs to be refreshed"
    # message fallback
    assert verify.extract_executequeries_error({"error": {"message": "boom"}}) == "boom"
    # code fallback
    assert verify.extract_executequeries_error({"error": {"code": "X"}}) == "X"
    # junk-safe
    assert verify.extract_executequeries_error({}) is None
    assert verify.extract_executequeries_error(None) is None
    assert verify.extract_executequeries_error("not-a-dict") is None


def test_verify_match_null_fabric_is_no_data_not_mismatch():
    # Tableau returns real values; Fabric returns null everywhere (unrefreshed mirror model).
    t = make_tableau_probe({
        ("MIN", "Order Date", False): "2021-01-01",
        ("MAX", "Order Date", False): "2026-12-31",
        ("SUM", "Sales", True): 1000,
        ("SUM", "Sales", False): 1000,
    })
    v = verify_match("t-1", "w-1", "m-1", _windowed_plan(), t, _null_fabric_probe())
    assert v["verdict"] == "inconclusive"          # cannot verify against an empty model
    assert v["reason_code"] == "fabric_no_data"    # ...but we say *why*, actionably
    assert "refresh" in v["notes"][0].lower()
    assert "refresh" in verification_note(v).lower()


def test_verify_match_fabric_as_refresh_error_is_no_data():
    msg = ("The expression referenced column 'Orders'[Sales] which does not hold any data "
           "because it needs to be recalculated or refreshed.")
    t = make_tableau_probe({
        ("MIN", "Order Date", False): "2021-01-01",
        ("MAX", "Order Date", False): "2026-12-31",
        ("SUM", "Sales", True): 1000,
        ("SUM", "Sales", False): 1000,
    })
    f = make_fabric_probe({}, errors={
        ("MIN", "OrderDate", False): msg, ("MAX", "OrderDate", False): msg,
        ("SUM", "Sales", True): msg, ("SUM", "Sales", False): msg,
    })
    v = verify_match("t-1", "w-1", "m-1", _windowed_plan(), t, f)
    assert v["verdict"] == "inconclusive"
    assert v["reason_code"] == "fabric_no_data"


def test_verify_match_tableau_down_is_not_flagged_as_fabric_no_data():
    # If Tableau also returns nothing (VDS down), we must NOT blame Fabric for "no data".
    v = verify_match("t-1", "w-1", "m-1", _windowed_plan(), always_error(), _null_fabric_probe())
    assert v["verdict"] == "inconclusive"
    assert v.get("reason_code") is None


def test_verify_estate_rolls_up_fabric_no_data():
    tableau = [{"name": "Superstore", "project": "S", "luid": "t-1", "fields": [
        {"name": "Order Date", "dataType": "DATE"}, {"name": "Sales", "dataType": "REAL"}]}]
    fabric = [{"name": "Superstore", "workspace": "WS", "workspaceId": "w-1", "id": "m-1",
               "tables": ["Orders"], "columns": [
                   {"name": "Order Date", "dataType": "datetime", "table": "Orders"},
                   {"name": "Sales", "dataType": "double", "table": "Orders"}]}]
    result = compare.compare_inventories(tableau, fabric)

    def t(luid, field, vds_func, window=None):
        vals = {("MIN", "Order Date"): "2021-01-01", ("MAX", "Order Date"): "2026-12-31",
                ("SUM", "Sales"): 1000, ("COUNTD", "Sales"): 50, ("COUNTD", "Order Date"): 365}
        return (vals.get((vds_func, field)), None)
    out = verify_estate(result, tableau, fabric, t, _null_fabric_probe())
    assert out["summary"]["verification"]["fabric_no_data"] >= 1
    assert "fabric_unreadable" in out["summary"]["verification"]
    flagged = [m for m in out["matches"] if (m.get("verification") or {}).get("reason_code") == "fabric_no_data"]
    assert flagged
    assert "refresh" in (flagged[0]["verification_note"]).lower()


# ======================================================================================
# verification_note
# ======================================================================================
def test_verification_note_phrasing():
    assert "verified" in verification_note(
        {"verdict": "verified", "relationship": "subset", "probes_agreed": 2, "probes_disagreed": 0}).lower()
    assert verification_note({"verdict": "mismatch", "notes": ["SUM(sales) on overlap: 1 vs 2"]}).startswith(
        "VERIFY MISMATCH")
    assert "compatible" in verification_note({"verdict": "compatible", "relationship": "subset"}).lower()


# ======================================================================================
# verify_estate (additive orchestration)
# ======================================================================================
def _estate():
    result = {
        "summary": {"already_exist": 1, "partial": 0, "rebuild": 1},
        "matches": [
            {"tableau_name": "DS1", "tableau_luid": "t-1", "bucket": "already_exists",
             "tier": "Exact", "score": 0.95,
             "best_match": {"fabric_name": "M1", "fabric_id": "m-1",
                            "workspace": "WS", "workspace_id": "w-1"}},
            {"tableau_name": "DS2", "tableau_luid": "t-2", "bucket": "rebuild",
             "tier": "None", "score": 0.10, "best_match": None},
        ],
    }
    tableau_inv = [{"name": "DS1", "luid": "t-1", "fields": [
        {"name": "Order Date", "dataType": "DATE"}, {"name": "Sales", "dataType": "REAL"}]}]
    fabric_inv = [{"name": "M1", "id": "m-1", "workspace": "WS", "workspaceId": "w-1", "columns": [
        {"name": "Order Date", "dataType": "datetime", "table": "Orders"},
        {"name": "Sales", "dataType": "double", "table": "Orders"}]}]
    return result, tableau_inv, fabric_inv


def _estate_probes():
    t = make_tableau_probe({
        ("MIN", "Order Date", False): "2021-01-01",
        ("MAX", "Order Date", False): "2026-12-31",
        ("COUNTD", "Order Date", True): 365,
        ("SUM", "Sales", True): 1000,
        ("COUNTD", "Sales", True): 50,
    })
    f = make_fabric_probe({
        ("MIN", "Order Date", False): "2019-01-01",
        ("MAX", "Order Date", False): "2026-12-31",
        ("DISTINCTCOUNT", "Order Date", True): 365,
        ("SUM", "Sales", True): 1000,
        ("DISTINCTCOUNT", "Sales", True): 50,
    })
    return t, f


def test_verify_estate_attaches_to_eligible_skips_rebuild():
    result, tinv, finv = _estate()
    t, f = _estate_probes()
    out = verify_estate(result, tinv, finv, t, f)
    ds1 = out["matches"][0]
    ds2 = out["matches"][1]
    assert ds1["verification"]["verdict"] == "verified"
    assert "verification_note" in ds1
    assert "verification" not in ds2  # clear rebuild skipped


def test_verify_estate_does_not_change_tier_or_score():
    result, tinv, finv = _estate()
    before = [(m["tier"], m["score"], m["bucket"]) for m in result["matches"]]
    t, f = _estate_probes()
    out = verify_estate(result, tinv, finv, t, f)
    after = [(m["tier"], m["score"], m["bucket"]) for m in out["matches"]]
    assert before == after


def test_verify_estate_summary_rollup():
    result, tinv, finv = _estate()
    t, f = _estate_probes()
    out = verify_estate(result, tinv, finv, t, f)
    roll = out["summary"]["verification"]
    assert roll["enabled"] is True
    assert roll["attempted"] == 1
    assert roll["verified"] == 1
    assert roll["mismatch"] == 0
    assert roll["probes_run"] >= 1


def test_verify_estate_is_idempotent():
    result, tinv, finv = _estate()
    t, f = _estate_probes()
    once = verify_estate(copy.deepcopy(result), tinv, finv, t, f)
    twice = verify_estate(once, tinv, finv, t, f)
    assert once == twice


def test_verify_estate_top_n_cap():
    result, tinv, finv = _estate()
    # three eligible already_exists matches; top_n=2 -> only the two highest scores attempted
    result["matches"] = [
        {"tableau_name": f"DS{i}", "tableau_luid": f"t-{i}", "bucket": "already_exists",
         "tier": "Exact", "score": s,
         "best_match": {"fabric_name": "M1", "fabric_id": "m-1", "workspace": "WS", "workspace_id": "w-1"}}
        for i, s in ((1, 0.9), (2, 0.8), (3, 0.7))
    ]
    tinv = [{"name": f"DS{i}", "luid": f"t-{i}", "fields": tinv[0]["fields"]} for i in (1, 2, 3)]
    t, f = _estate_probes()
    out = verify_estate(result, tinv, finv, t, f, top_n=2)
    verified_flags = [("verification" in m) for m in out["matches"]]
    assert sum(verified_flags) == 2
    assert out["summary"]["verification"]["attempted"] == 2


def test_verify_estate_unresolved_entry_is_inconclusive():
    result = {
        "summary": {},
        "matches": [{"tableau_name": "DS1", "tableau_luid": "t-1", "bucket": "already_exists",
                     "tier": "Exact", "score": 0.9,
                     "best_match": {"fabric_name": "ghost", "fabric_id": "missing", "workspace": "WS"}}],
    }
    t, f = _estate_probes()
    out = verify_estate(result, [], [], t, f)
    assert out["matches"][0]["verification"]["verdict"] == "inconclusive"
    assert out["summary"]["verification"]["inconclusive"] == 1


# ======================================================================================
# report rendering (additive section)
# ======================================================================================
import compare  # noqa: E402


def _real_result_with_verification(fabric_sales_overlap=1000):
    tableau = [{"name": "Superstore", "project": "S", "luid": "t-1", "fields": [
        {"name": "Order Date", "dataType": "DATE"}, {"name": "Sales", "dataType": "REAL"}]}]
    fabric = [{"name": "Superstore", "workspace": "WS", "workspaceId": "w-1", "id": "m-1",
               "tables": ["Orders"], "columns": [
                   {"name": "Order Date", "dataType": "datetime", "table": "Orders"},
                   {"name": "Sales", "dataType": "double", "table": "Orders"}]}]
    result = compare.compare_inventories(tableau, fabric)
    t = make_tableau_probe({
        ("MIN", "Order Date", False): "2021-01-01", ("MAX", "Order Date", False): "2026-12-31",
        ("COUNTD", "Order Date", True): 365, ("SUM", "Sales", True): 1000, ("COUNTD", "Sales", True): 50,
    })
    f = make_fabric_probe({
        ("MIN", "Order Date", False): "2019-01-01", ("MAX", "Order Date", False): "2026-12-31",
        ("DISTINCTCOUNT", "Order Date", True): 365, ("SUM", "Sales", True): fabric_sales_overlap,
        ("DISTINCTCOUNT", "Sales", True): 50,
    })
    verify_estate(result, tableau, fabric, t, f)
    return result


def test_render_includes_verification_section_and_verdict():
    md = compare.render_markdown(_real_result_with_verification())
    assert "## Empirical verification" in md
    assert "Verified" in md
    assert "overlapping data window" in md  # explains the superset-friendly model


def test_render_skip_line_when_disabled():
    tableau = [{"name": "DS1", "luid": "t-1", "fields": [{"name": "Sales", "dataType": "REAL"}]}]
    fabric = [{"name": "DS1", "workspace": "WS", "workspaceId": "w-1", "id": "m-1",
               "columns": [{"name": "Sales", "dataType": "double", "table": "T"}]}]
    result = compare.compare_inventories(tableau, fabric)
    result["summary"]["verification"] = {"enabled": False, "reason": "no Power BI token"}
    md = compare.render_markdown(result)
    assert "## Empirical verification" in md
    assert "Requested but skipped" in md


def test_render_no_section_when_no_verification():
    tableau = [{"name": "DS1", "luid": "t-1", "fields": [{"name": "Sales", "dataType": "REAL"}]}]
    fabric = [{"name": "DS1", "workspace": "WS", "workspaceId": "w-1", "id": "m-1",
               "columns": [{"name": "Sales", "dataType": "double", "table": "T"}]}]
    md = compare.render_markdown(compare.compare_inventories(tableau, fabric))
    assert "## Empirical verification" not in md


def test_render_mismatch_callout():
    md = compare.render_markdown(_real_result_with_verification(fabric_sales_overlap=9999))
    assert "MISMATCH" in md


