"""Tests for date_window_flag: the parameter-driven positional-date-band keep-flag recognizer."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from date_window_flag import (  # noqa: E402
    parse_band_case,
    recognize_date_window_flag,
    build_dax,
    build_date_window_flags,
)


# ---- pilot ground truth (from Comcast Test.twbx) -------------------------------------------
PILOT_FORMULA = (
    "case [Parameters].[Parameter 0014172370878491] \n"
    "when 15 then [Calculation_0014172370616346] <= 15 \n"
    "when 30 then [Calculation_0014172370616346] <= 30 "
    "and [Calculation_0014172370616346] >= 15 \n"
    "when 41 then [Calculation_0014172370616346] <= 41 \n"
    "END"
)

LOCKED_DAX = (
    "VAR anchor = CALCULATE(MAX('Orders'[Order Date]), ALL('Orders'))\n"
    "VAR d = SELECTEDVALUE('Date'[Date])\n"
    "VAR sel = SELECTEDVALUE('Date Selection'[Date Selection], 15)\n"
    "RETURN\n"
    "SWITCH(\n"
    "    TRUE(),\n"
    "    sel = 15, IF(d > anchor - 15, 1),\n"
    "    sel = 30, IF(d > anchor - 30 && d <= anchor - 15, 1),\n"
    "    sel = 41, 1\n"
    ")"
)


def _calcs():
    return [
        {"name": "Date Filter", "internal_name": "Calculation_0014172371238940",
         "formula": PILOT_FORMULA},
        {"name": "last", "internal_name": "Calculation_0014172370616346",
         "formula": "LAST()"},
    ]


def _parameters():
    return [{
        "caption": "Date Selection",
        "internal_name": "[Parameter 0014172370878491]",
        "datatype": "real", "domain": "list", "default": "15.",
        "members": ["15.", "30.", "41."],
        "aliases": {"15.": "Current Orders", "30.": "Previous Orders", "41.": "All Orders"},
    }]


def _consumed():
    return [{
        "caption": "Date Selection",
        "internal_name": "[Parameter 0014172370878491]",
        "table": "Date Selection",
        "measure": "Date Selection Value",
        "picker_column": "Date Selection Label",
    }]


def _kw(**over):
    base = dict(date_name="Date", active_date_cols={("Orders", "Order Date")})
    base.update(over)
    return base


# ---- parse ---------------------------------------------------------------------------------
def test_parse_band_case_pilot():
    parsed = parse_band_case(PILOT_FORMULA)
    assert parsed is not None
    assert parsed["controller"] == "Parameter 0014172370878491"
    whens = [b["when"] for b in parsed["branches"]]
    assert whens == ["15", "30", "41"]
    assert parsed["branches"][0]["lo"] is None         # first single
    assert parsed["branches"][1]["lo"] == "15"          # middle bounded, lo == prev
    assert parsed["branches"][-1]["lo"] is None          # last single


def test_parse_band_case_accepts_reversed_bound_order():
    f = ("case [Parameters].[P] when 15 then [x] <= 15 "
         "when 30 then [x] >= 15 and [x] <= 30 when 41 then [x] <= 41 END")
    parsed = parse_band_case(f)
    assert parsed is not None
    assert parsed["branches"][1]["hi"] == "30"
    assert parsed["branches"][1]["lo"] == "15"


def test_parse_band_case_rejects_non_case():
    assert parse_band_case("SUM([Sales])") is None
    assert parse_band_case("") is None


# ---- recognize (happy path) ----------------------------------------------------------------
def test_recognize_pilot():
    calcs = _calcs()
    recog = recognize_date_window_flag(
        calcs[0], calcs, _parameters(), _consumed(), **_kw())
    assert recog is not None
    assert recog["anchor_table"] == "Orders"
    assert recog["anchor_col"] == "Order Date"
    assert recog["value_table"] == "Date Selection"
    assert recog["value_col"] == "Date Selection"
    assert recog["bands"] == ["15", "30", "41"]
    assert recog["default"] == "15"


def test_build_dax_matches_locked_target():
    calcs = _calcs()
    recog = recognize_date_window_flag(
        calcs[0], calcs, _parameters(), _consumed(), **_kw())
    assert build_dax(recog) == LOCKED_DAX


# ---- build_date_window_flags ---------------------------------------------------------------
def test_build_flags_emits_one_measure_and_binding():
    reserved = {"orders", "order date", "date", "date selection",
                "date filter", "last", "_measures"}
    flags, bindings = build_date_window_flags(
        _calcs(), _parameters(), _consumed(),
        reserved_names=reserved, **_kw())
    assert len(flags) == 1
    fm = flags[0]
    assert fm["measure"] == "Date Filter"        # natural name (source calc itself excluded)
    assert fm["dax"] == LOCKED_DAX
    assert fm["source_calc_name"] == "Date Filter"
    assert fm["source_calc_id"] == "Calculation_0014172371238940"
    assert fm["report_row"]["status"] == "translated"
    assert fm["report_row"]["source"]["model_table"] == "_Measures"
    assert fm["report_row"]["source"]["calc_instance_token"] == "Calculation_0014172371238940"

    assert "Date Filter" in bindings
    b = bindings["Date Filter"]
    assert b["measure_name"] == "Date Filter"
    assert b["model_table"] == "_Measures"
    assert b["value"] == 1
    assert b["calc_id"] == "Calculation_0014172371238940"
    assert b["param_internal"] == "Parameter 0014172370878491"


def test_uniquify_rename_branch():
    from date_window_flag import _uniquify
    # candidate clashes with a reserved name that is NOT excluded -> shifts to "(flag)".
    assert _uniquify("Date Filter", {"date filter", "_measures"}) == "Date Filter (flag)"
    # excluding the clashing name frees it.
    assert _uniquify("Date Filter", {"date filter"},
                     exclude={"date filter"}) == "Date Filter"
    # cascading clash -> numeric suffix.
    assert _uniquify("X", {"x", "x (flag)"}) == "X (flag) 2"


# ---- fail-closed ---------------------------------------------------------------------------
def test_fail_closed_inner_not_last():
    calcs = _calcs()
    calcs[1]["formula"] = "FIRST()"
    assert recognize_date_window_flag(
        calcs[0], calcs, _parameters(), _consumed(), **_kw()) is None


def test_fail_closed_non_ascending_bands():
    f = ("case [Parameters].[Parameter 0014172370878491] "
         "when 30 then [Calculation_0014172370616346] <= 30 "
         "when 15 then [Calculation_0014172370616346] <= 15 "
         "and [Calculation_0014172370616346] >= 30 END")
    calcs = [{"name": "Date Filter", "internal_name": "C", "formula": f},
             {"name": "last", "internal_name": "Calculation_0014172370616346",
              "formula": "LAST()"}]
    assert recognize_date_window_flag(
        calcs[0], calcs, _parameters(), _consumed(), **_kw()) is None


def test_fail_closed_member_mismatch():
    params = _parameters()
    params[0]["members"] = ["15.", "30."]           # 2 members vs 3 bands
    assert recognize_date_window_flag(
        _calcs()[0], _calcs(), params, _consumed(), **_kw()) is None


def test_fail_closed_param_not_consumed():
    # Stage 1 (value table) never fired -> no consumed entry -> no flag.
    assert recognize_date_window_flag(
        _calcs()[0], _calcs(), _parameters(), [], **_kw()) is None


def test_fail_closed_missing_anchor():
    assert recognize_date_window_flag(
        _calcs()[0], _calcs(), _parameters(), _consumed(),
        **_kw(active_date_cols=set())) is None


def test_fail_closed_multiple_anchors():
    assert recognize_date_window_flag(
        _calcs()[0], _calcs(), _parameters(), _consumed(),
        **_kw(active_date_cols={("Orders", "Order Date"), ("Returns", "Return Date")})) is None


def test_fail_closed_no_date_dim():
    assert recognize_date_window_flag(
        _calcs()[0], _calcs(), _parameters(), _consumed(),
        **_kw(date_name=None)) is None


def test_fail_closed_last_branch_bounded():
    # last branch bounded (not the open "all" band) -> shape rejected
    f = ("case [Parameters].[Parameter 0014172370878491] "
         "when 15 then [Calculation_0014172370616346] <= 15 "
         "when 30 then [Calculation_0014172370616346] <= 30 "
         "and [Calculation_0014172370616346] >= 15 "
         "when 41 then [Calculation_0014172370616346] <= 41 "
         "and [Calculation_0014172370616346] >= 30 END")
    calcs = [{"name": "Date Filter", "internal_name": "C", "formula": f},
             {"name": "last", "internal_name": "Calculation_0014172370616346",
              "formula": "LAST()"}]
    assert recognize_date_window_flag(
        calcs[0], calcs, _parameters(), _consumed(), **_kw()) is None


def test_build_flags_no_match_returns_empty():
    calcs = [{"name": "Sales", "internal_name": "C", "formula": "SUM([Sales])"}]
    flags, bindings = build_date_window_flags(
        calcs, _parameters(), _consumed(), reserved_names=set(), **_kw())
    assert flags == []
    assert bindings == {}
